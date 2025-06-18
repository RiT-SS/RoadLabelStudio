"""Own the local Qwen child process; HTTP workers never import CUDA libraries."""
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
    """3B FP16 weights, with extra headroom for CUDA and image activations."""
    rows = sorted(rows, key=lambda row: row['free_mib'], reverse=True)
    if rows and rows[0]['free_mib'] >= 10240:
        return rows[:1], 8.0
    if len(rows) >= 2 and rows[0]['free_mib'] >= 5632 and rows[1]['free_mib'] >= 4608:
        return rows[:2], 3.5
    return [], 0


class ModelRuntime:
    def __init__(self, store):
        self.store = store
        self.enabled = os.getenv('QWEN_AUTO_MANAGE', '0') == '1'
        self.model = os.getenv('QWEN_MODEL', 'Qwen2.5-VL-3B-Instruct')
        self.url = os.getenv('QWEN_BASE_URL', '').rstrip('/')
        self.idle_seconds = max(1, int(os.getenv('QWEN_IDLE_SECONDS', '600')))
        self.offline_seconds = max(30, int(os.getenv('STUDIO_PRESENCE_SECONDS', '90')))
        self.load_timeout = max(30, int(os.getenv('QWEN_LOAD_TIMEOUT_SECONDS', '300')))
        self.process = None
        self.instance = None
        self.gpus = []
        self.idle_since = None
        self.status = 'unloaded' if self.enabled else 'external'
        self.message = '有任务时自动加载' if self.enabled else '由外部模型服务管理'
        self.lock = threading.RLock()
        self.stop = threading.Event()
        if self.enabled:
            parsed = urlsplit(self.url)
            if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.path != '/v1' or parsed.username or parsed.query or parsed.fragment:
                raise ValueError('自动管理只支持 http://127.0.0.1:端口/v1 的本机 Qwen 服务')
            if self.model != 'Qwen2.5-VL-3B-Instruct':
                raise ValueError('当前自动显存调度适配 Qwen2.5-VL-3B-Instruct')
            self.port = parsed.port or 80
            self.health_url = f'http://127.0.0.1:{self.port}/health'
            self._state(self.status, self.message)

    def _state(self, status, message):
        with self.lock:
            self.status, self.message = status, message
            if self.enabled:
                state = {'status': status, 'message': message, 'model': self.model,
                         'pid': self.process.pid if self.process else None, 'gpus': self.gpus,
                         'instance': self.instance, 'managed': True, 'updated': time.time()}
                target = self.store.root/'qwen_server.json'
                temporary = target.with_suffix('.tmp')
                temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2))
                temporary.replace(target)

    def snapshot(self):
        with self.lock:
            return {'managed': self.enabled, 'status': self.status, 'message': self.message,
                    'model': self.model, 'gpus': list(self.gpus),
                    'online_sessions': self.store.online_sessions(self.offline_seconds),
                    'idle_seconds': self.idle_seconds, 'offline_seconds': self.offline_seconds}

    def _gpu_rows(self):
        result = subprocess.run(['nvidia-smi', '--query-gpu=index,uuid,memory.free',
                                 '--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=8, check=True)
        permitted = os.getenv('QWEN_GPU_IDS', 'auto')
        allowed = None if permitted == 'auto' else set(permitted.split(','))
        rows = []
        for line in result.stdout.splitlines():
            index, gpu_uuid, free = [v.strip() for v in line.split(',')]
            if allowed is None or index in allowed or gpu_uuid in allowed:
                rows.append({'index': int(index), 'uuid': gpu_uuid, 'free_mib': int(free)})
        return rows

    def _launch(self, rows, budget):
        with self.lock:
            if self.stop.is_set():
                return
            self._launch_locked(rows, budget)

    def _launch_locked(self, rows, budget):
        # Never adopt or terminate a process merely because it owns this port.
        with socket.socket() as probe:
            try:
                probe.bind(('127.0.0.1', self.port))
            except OSError as exc:
                raise ValueError('本地模型端口被其他服务占用；未停止该服务') from exc
        self.instance = uuid.uuid4().hex
        env = {**os.environ, 'CUDA_VISIBLE_DEVICES': ','.join(row['uuid'] for row in rows),
               'OMP_NUM_THREADS': '2', 'TOKENIZERS_PARALLELISM': 'false',
               'QWEN_PARENT_PID': str(os.getpid())}
        command = [sys.executable, str(ROOT/'scripts/serve_qwen.py'), '--port', str(self.port),
                   '--memory-gib', str(budget), '--model-name', self.model,
                   '--model-path', os.getenv('QWEN_MODEL_PATH', str(ROOT/'models/Qwen2.5-VL-3B-Instruct')),
                   '--instance-id', self.instance]
        with (self.store.root/'qwen.log').open('ab', buffering=0) as log:
            self.process = subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        self.gpus = [row['index'] for row in rows]
        self._state('loading', '正在加载本地模型，完成后自动开始分析')

    def _healthy(self):
        try:
            with httpx.Client(timeout=2, trust_env=False) as client:
                response = client.get(self.health_url)
                return (response.status_code == 200 and response.json().get('ready') is True
                        and response.json().get('instance') == self.instance
                        and response.json().get('model') == self.model)
        except (httpx.HTTPError, ValueError):
            return False

    def _release(self, reason):
        with self.lock:
            if self.process:
                self._state('unloading', '正在释放模型显存')
                if self.process.poll() is None:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=5)
                self.process = None
            self.gpus, self.instance, self.idle_since = [], None, None
            self._state('unloaded', reason)

    def ensure_ready(self, provider, cancelled, progress):
        if not self.enabled:
            return True
        if provider['url'].rstrip('/') != self.url or provider['model'] != self.model:
            raise ValueError('任务的模型配置与当前自动管理服务不一致，请重新创建任务')
        self.idle_since = None
        launched_at = time.monotonic() if self.process else None
        retry_at = 0
        try:
            while not self.stop.is_set() and not cancelled():
                if self.process:
                    if self.process.poll() is not None:
                        raise ValueError('本地模型加载或运行失败，请查看模型日志后重试')
                    if self._healthy():
                        self._state('ready', '模型已就绪')
                        return True
                    if time.monotonic() - launched_at > self.load_timeout:
                        raise ValueError('本地模型加载超时，请稍后重试')
                    progress('loading_model')
                elif time.monotonic() >= retry_at:
                    try:
                        rows, budget = choose_gpus(self._gpu_rows())
                    except (OSError, ValueError, subprocess.SubprocessError):
                        rows, budget = [], 0
                    if rows:
                        self._launch(rows, budget)
                        launched_at = time.monotonic()
                        progress('loading_model')
                    else:
                        self._state('waiting_resources', '等待足够的空闲显存，资源可用后自动开始')
                        progress('waiting_resources')
                        retry_at = time.monotonic() + 10
                self.stop.wait(1)
            self._release('任务已停止，模型显存已释放')
            return False
        except Exception:
            self._release('模型未能就绪，显存已释放；可重新创建任务')
            raise

    def maintain(self):
        if not self.enabled:
            return
        with self.store.db() as db:
            rows = db.execute("SELECT config FROM jobs WHERE status IN ('queued','running','cancelling')").fetchall()
        if any(json.loads(row['config']).get('provider') == 'qwen' for row in rows):
            self.idle_since = None
            return
        if not self.process:
            if self.status == 'waiting_resources':
                self._state('unloaded', '有任务时自动加载')
            return
        if self.process.poll() is not None:
            self._release('模型进程已退出，下次任务会重新加载')
            return
        current = time.monotonic()
        if self.idle_since is None:
            self.idle_since = current
        if not self.store.online_sessions(self.offline_seconds):
            self._release('无人在线且无待处理任务，已释放显存')
        elif current - self.idle_since >= self.idle_seconds:
            self._release('队列空闲超时，已释放显存')

    def close(self):
        self.stop.set()
        if self.enabled:
            self._release('网站服务关闭，已释放模型显存')
