"""Reserve GPUs atomically and own independent, on-demand Qwen processes."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
import uuid

import httpx

ROOT = Path(__file__).resolve().parents[1]


def choose_gpus(rows):
    rows = sorted(rows, key=lambda row: row['free_mib'], reverse=True)
    if rows and rows[0]['free_mib'] >= 10240:
        return rows[:1], 8.0
    if len(rows) >= 2 and rows[0]['free_mib'] >= 5632 and rows[1]['free_mib'] >= 4608:
        return rows[:2], 3.5
    return [], 0


class CapacityUnavailable(ValueError):
    """Transient memory pressure: keep observations and return the job to the queue."""


@dataclass
class ModelSlot:
    id: str
    port: int
    rows: list
    budget: float
    job_id: str | None
    process: object = None
    status: str = 'reserved'
    idle_since: float | None = None
    started_at: float | None = None

    @property
    def url(self):
        return f'http://127.0.0.1:{self.port}/v1'

    @property
    def gpus(self):
        return [r['index'] for r in self.rows]


class ModelRuntime:
    def __init__(self, store):
        self.store = store
        self.enabled = os.getenv('QWEN_AUTO_MANAGE', '0') == '1'
        self.model = os.getenv('QWEN_MODEL', 'Qwen2.5-VL-3B-Instruct')
        self.url = os.getenv('QWEN_BASE_URL', '').rstrip('/')
        self.max_instances = max(1, min(8, int(os.getenv('QWEN_MAX_INSTANCES', '4'))))
        self.idle_seconds = max(1, int(os.getenv('QWEN_IDLE_SECONDS', '600')))
        self.offline_seconds = max(30, int(os.getenv('STUDIO_PRESENCE_SECONDS', '90')))
        self.load_timeout = max(30, int(os.getenv('QWEN_LOAD_TIMEOUT_SECONDS', '300')))
        self.slots = {}
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.cooldown = {}
        if self.enabled:
            parsed = urlsplit(self.url)
            if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.path != '/v1' or parsed.username or parsed.query or parsed.fragment:
                raise ValueError('自动管理只支持 http://127.0.0.1:端口/v1 的本机 Qwen 服务')
            if self.model != 'Qwen2.5-VL-3B-Instruct':
                raise ValueError('当前自动显存调度适配 Qwen2.5-VL-3B-Instruct')
            self.port = parsed.port or 80
            self._persist()

    def _summary(self):
        slots = list(self.slots.values())
        status = ('external' if not self.enabled else 'ready' if any(s.status == 'ready' for s in slots)
                  else 'loading' if any(s.status in ['reserved', 'loading'] for s in slots)
                  else 'unloading' if slots else 'unloaded')
        messages = {'external':'由外部模型服务管理', 'ready':'独立模型实例可用', 'loading':'正在准备模型实例',
                    'unloading':'正在释放模型显存', 'unloaded':'有任务且显存足够时自动加载'}
        return {'managed':self.enabled, 'model':self.model, 'status':status, 'message':messages[status],
                'gpus':sorted({i for s in slots for i in s.gpus}), 'max_instances':self.max_instances,
                'active_instances':sum(s.job_id is not None for s in slots),
                'loaded_instances':sum(s.process is not None for s in slots),
                'instances':[{'id':s.id, 'job_id':s.job_id, 'gpus':s.gpus, 'status':s.status} for s in slots],
                'idle_seconds':self.idle_seconds, 'offline_seconds':self.offline_seconds}

    def _persist(self):
        if not self.enabled:
            return
        with self.lock:
            state = self._summary()
            state['processes'] = [{'id':s.id, 'pid':s.process.pid if s.process else None, 'port':s.port} for s in self.slots.values()]
            state['updated'] = time.time()
            target = self.store.root/'qwen_server.json'
            temporary = target.with_suffix('.tmp')
            temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2))
            temporary.replace(target)

    def snapshot(self):
        # Never hold a model lock while entering SQLite or waiting for a process.
        with self.lock:
            result = self._summary()
        result['online_sessions'] = self.store.online_sessions(self.offline_seconds)
        return result

    def _gpu_rows(self):
        result = subprocess.run(['nvidia-smi', '--query-gpu=index,uuid,memory.free',
                                 '--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=8, check=True)
        permitted = os.getenv('QWEN_GPU_IDS', 'auto')
        allowed = None if permitted == 'auto' else set(permitted.split(','))
        rows = []
        for line in result.stdout.splitlines():
            index, gpu_uuid, free = [v.strip() for v in line.split(',')]
            if allowed is None or index in allowed or gpu_uuid in allowed:
                rows.append({'index':int(index), 'uuid':gpu_uuid, 'free_mib':int(free)})
        return rows

    @staticmethod
    def _headroom(slot, inventory):
        free = {r['uuid']:r['free_mib'] for r in inventory}
        return all(free.get(row['uuid'], 0) >= (2048 if i == 0 else 1024) for i, row in enumerate(slot.rows))

    def _available_port(self):
        used = {s.port for s in self.slots.values()}
        for port in range(self.port, min(65536, self.port + 32)):
            if port in used:
                continue
            with socket.socket() as probe:
                try:
                    probe.bind(('127.0.0.1', port))
                    return port
                except OSError:
                    continue
        raise ValueError('本地模型端口均被占用；未停止其他服务')

    def try_acquire(self, job_id, provider):
        if provider['url'].rstrip('/') != self.url or provider['model'] != self.model:
            raise ValueError('任务模型配置与当前服务不一致，请重新创建任务')
        # The actual free memory is probed before every admission, including reuse.
        try:
            inventory = self._gpu_rows()
        except (OSError, ValueError, subprocess.SubprocessError):
            return None, 'waiting_resources'
        with self.lock:
            if self.stop.is_set():
                return None, 'waiting_slot'
            for slot in self.slots.values():
                if slot.job_id == job_id:
                    return slot, ''
                if (slot.job_id is None and slot.status == 'ready' and slot.process is not None
                        and slot.process.poll() is None and self._headroom(slot, inventory)):
                    slot.job_id, slot.idle_since = job_id, None
                    self._persist()
                    return slot, ''
            if len(self.slots) >= self.max_instances:
                return None, 'waiting_slot'
            # Reservation includes loading and idle instances, even before CUDA allocates.
            reserved = {r['uuid'] for s in self.slots.values() for r in s.rows}
            rows, budget = choose_gpus([r for r in inventory if r['uuid'] not in reserved
                                       and self.cooldown.get(r['uuid'], 0) <= time.monotonic()])
            if not rows:
                return None, 'waiting_resources'
            slot = ModelSlot(uuid.uuid4().hex, self._available_port(), rows, budget, job_id)
            self.slots[slot.id] = slot
            self._persist()
            return slot, ''

    def _launch(self, slot):
        # Recheck after admission: other users may allocate memory in the meantime.
        rows = {r['uuid']:r for r in self._gpu_rows()}
        requirements = [10240] if len(slot.rows) == 1 else [5632, 4608]
        if any(rows.get(r['uuid'], {}).get('free_mib', 0) < minimum for r, minimum in zip(slot.rows, requirements)):
            raise CapacityUnavailable('启动前显存发生变化，任务继续等待')
        with self.lock:
            if self.stop.is_set() or self.slots.get(slot.id) is not slot or slot.status != 'reserved':
                return
            with socket.socket() as probe:
                try:
                    probe.bind(('127.0.0.1', slot.port))
                except OSError as exc:
                    raise ValueError('模型端口被占用，未停止其他服务') from exc
            env = {**os.environ, 'CUDA_VISIBLE_DEVICES':','.join(r['uuid'] for r in slot.rows),
                   'OMP_NUM_THREADS':'2', 'TOKENIZERS_PARALLELISM':'false', 'QWEN_PARENT_PID':str(os.getpid())}
            command = [sys.executable, str(ROOT/'scripts/serve_qwen.py'), '--port', str(slot.port),
                       '--memory-gib', str(slot.budget), '--model-name', self.model,
                       '--model-path', os.getenv('QWEN_MODEL_PATH', str(ROOT/'models/Qwen2.5-VL-3B-Instruct')),
                       '--instance-id', slot.id]
            with (self.store.root/f'qwen-{slot.id}.log').open('ab', buffering=0) as log:
                slot.process = subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            slot.status, slot.started_at = 'loading', time.monotonic()
            self._persist()

    def _healthy(self, slot):
        try:
            with httpx.Client(timeout=2, trust_env=False) as client:
                response = client.get(slot.url.replace('/v1', '/health'))
                data = response.json()
                return (response.status_code == 200 and data.get('ready') is True
                        and data.get('instance') == slot.id and data.get('model') == self.model)
        except (httpx.HTTPError, ValueError):
            return False

    def _oom_on_load(self, slot):
        try:
            with (self.store.root/f'qwen-{slot.id}.log').open('rb') as log:
                log.seek(0, 2);log.seek(max(0, log.tell()-16000))
                tail = log.read().decode(errors='replace').lower()
            return 'out of memory' in tail and ('cuda' in tail or 'torch' in tail)
        except OSError:
            return False

    def ensure_ready(self, slot, cancelled, progress):
        try:
            if slot.status == 'reserved':
                if cancelled() or self.stop.is_set():
                    self.discard(slot)
                    return False
                self._launch(slot)
            while not self.stop.is_set() and not cancelled():
                if slot.process is None or slot.process.poll() is not None:
                    if self._oom_on_load(slot):
                        raise CapacityUnavailable('模型加载显存不足，保留进度后重新排队')
                    raise ValueError('本地模型实例运行失败，请查看对应实例日志')
                if self._healthy(slot):
                    with self.lock:
                        slot.status = 'ready'
                        self._persist()
                    return True
                if time.monotonic() - slot.started_at > self.load_timeout:
                    raise ValueError('本地模型实例加载超时')
                progress('loading_model')
                self.stop.wait(.5)
            self.discard(slot)
            return False
        except Exception:
            self.discard(slot)
            raise

    def release(self, slot, broken=False, pressure=False):
        if pressure:
            with self.lock:
                for row in slot.rows:
                    self.cooldown[row['uuid']] = time.monotonic() + 30
        if broken or slot.process is None or slot.process.poll() is not None:
            self.discard(slot)
            return
        with self.lock:
            if self.slots.get(slot.id) is slot:
                slot.job_id, slot.idle_since = None, time.monotonic()
                self._persist()

    def discard(self, slot):
        with self.lock:
            if self.slots.get(slot.id) is not slot or slot.status == 'unloading':
                return
            slot.status = 'unloading'
            process = slot.process
            self._persist()
        try:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        finally:
            with self.lock:
                # GPU reservations remain held until this process has actually exited.
                if process is None or process.poll() is not None:
                    self.slots.pop(slot.id, None)
                else:
                    slot.status = 'error'
                self._persist()

    def maintain(self):
        if not self.enabled:
            return
        with self.store.db() as db:
            pending = any(json.loads(r['config']).get('provider') == 'qwen'
                          for r in db.execute("SELECT config FROM jobs WHERE status='queued'"))
        online = self.store.online_sessions(self.offline_seconds)
        with self.lock:
            idle = [s for s in self.slots.values() if s.job_id is None]
        for slot in idle:
            if (slot.process is None or slot.process.poll() is not None
                    or (slot.idle_since is not None and time.monotonic()-slot.idle_since >= self.idle_seconds)
                    or (not online and not pending)):
                self.discard(slot)

    def close(self):
        self.stop.set()
        with self.lock:
            slots = list(self.slots.values())
        for slot in slots:
            self.discard(slot)
