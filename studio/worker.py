import base64
import json
import os
import threading
import time
import uuid
import httpx
from .domain import CLASSES, KEYS, Scores
from .media import frame
from .storage import encode, now

def providers():
    return {
        'deepseek': {'name': 'DeepSeek · 云端视觉', 'model': os.getenv('DEEPSEEK_MODEL', 'deepseek-flash'), 'url': os.getenv('DEEPSEEK_BASE_URL', 'https://api.deepseek.com'), 'key': os.getenv('DEEPSEEK_API_KEY', ''), 'configured': bool(os.getenv('DEEPSEEK_API_KEY')), 'external': True},
        'qwen': {'name': 'Qwen-VL · 本地推理', 'model': os.getenv('QWEN_MODEL', 'Qwen2.5-VL-3B-Instruct'), 'url': os.getenv('QWEN_BASE_URL', ''), 'key': os.getenv('QWEN_API_KEY', ''), 'configured': bool(os.getenv('QWEN_BASE_URL')), 'external': False},
    }

def prompt(skills):
    rules = '\n'.join(f'【{s["name"]}】{s["instructions"]}' for s in skills)
    return ('你是一名道路巡检视频初标助手，只分析给定帧的可见证据，优先保留可能的异常。'
            '路段背景与异常独立；不要从道路背景、车辆颠簸推断病害。'
            '每类单独给出 0～1 置信度，不归一化，不宣称统计校准。'
            '画面看不清不能标为正常。正常>=0.5 时其他类别必须<0.5。'
            '仅输出 JSON 对象，含 scores（必须包含以下全部英文键）、note（中文可见证据）。'
            f'类别映射：{encode(CLASSES)}。未启用的异常类别给 0。当前启用规则：\n{rules}')

def infer(client, provider, instruction, image):
    payload = {
        'model': provider['model'], 'temperature': 0, 'max_tokens': 1200,
        'messages': [{'role': 'user', 'content': [
            {'type': 'text', 'text': instruction},
            {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + base64.b64encode(image).decode()}},
        ]}],
    }
    headers = {'Authorization': 'Bearer '+provider['key']} if provider['key'] else {}
    for attempt in range(3):
        response = client.post(provider['url'].rstrip('/')+'/chat/completions', headers=headers, json=payload)
        if response.status_code in [429, 500, 502, 503, 504] and attempt < 2:
            time.sleep(1 + attempt)
            continue
        if response.status_code != 200:
            raise ValueError(f'模型接口返回 HTTP {response.status_code}，请检查服务、视觉模型名称及凭据')
        break
    try:
        content = response.json()['choices'][0]['message']['content'].strip()
        if content.startswith('```'):
            content = content.split('\n', 1)[1].rsplit('```', 1)[0].strip()
        return Scores.model_validate(json.loads(content))
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ValueError('模型未返回符合标注规范的 JSON，本帧未被伪造为正常') from exc

def sampled_indices(pts, mode, fps):
    if mode == 'every_frame':
        return list(range(len(pts)))
    result, next_t = [], 0.
    for i, t in enumerate(pts):
        if t + 1e-9 >= next_t:
            result.append(i)
            next_t = t + 1/fps
    return result

class Worker:
    def __init__(self, store, runtime=None):
        self.store, self.stop = store, threading.Event()
        self.runtime = runtime
        self.thread = None

    def start(self):
        with self.store.db() as db:
            db.execute("UPDATE jobs SET status='interrupted', error='服务重启，已有初标已保留；请新建任务重试',updated=? WHERE status IN ('running','cancelling')", (now(),))
        self.thread = threading.Thread(target=self.loop, name='annotation-worker', daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=3)

    def loop(self):
        while not self.stop.is_set():
            if self.runtime:
                self.runtime.maintain()
            with self.store.db() as db:
                row = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
                if row:
                    db.execute("UPDATE jobs SET status='running',updated=? WHERE id=?", (now(), row['id']))
            if row:
                self.run(dict(row))
            else:
                self.stop.wait(.5)

    def run(self, job):
        try:
            config = json.loads(job['config'])
            provider = {**providers()[config['provider']], **config['provider_snapshot']}
            if not provider['configured']:
                raise ValueError('模型尚未配置')
            def cancelled():
                with self.store.db() as db:
                    status = db.execute('SELECT status FROM jobs WHERE id=?', (job['id'],)).fetchone()[0]
                return self.stop.is_set() or status == 'cancelling'
            def progress(stage):
                with self.store.db() as db:
                    db.execute('UPDATE jobs SET stage=?,updated=? WHERE id=?', (stage,now(),job['id']))
            if config['provider'] == 'qwen' and self.runtime:
                if not self.runtime.ensure_ready(provider, cancelled, progress):
                    self.finish(job['id'], 'cancelled', '已停止，已有结果保留')
                    return
            progress('analyzing')
            video = self.store.video(job['video_id'])
            with httpx.Client(timeout=httpx.Timeout(120, connect=10), follow_redirects=False, trust_env=False) as client:
                for index in config['indices']:
                    with self.store.db() as db:
                        status = db.execute('SELECT status FROM jobs WHERE id=?', (job['id'],)).fetchone()[0]
                    if self.stop.is_set() or status == 'cancelling':
                        self.finish(job['id'], 'cancelled', '已停止，部分结果保留在本次任务中')
                        return
                    image = frame(self.store, video, index).read_bytes()
                    prediction = infer(client, provider, config['prompt'], image)
                    if any(prediction.scores[k] > 0 for k in KEYS[:7] if k not in config['skill_ids']):
                        raise ValueError('模型为未启用类别输出了分数，请检查策略或模型输出')
                    with self.store.db() as db:
                        db.execute('INSERT INTO observations VALUES(?,?,?,?,?)', (job['id'], index, encode(prediction.scores), prediction.note, 'model_observed'))
                        db.execute('UPDATE jobs SET done=done+1,updated=? WHERE id=?', (now(), job['id']))
            with self.store.db() as db:
                status = db.execute('SELECT status FROM jobs WHERE id=?', (job['id'],)).fetchone()[0]
                if status == 'cancelling':
                    db.execute("UPDATE jobs SET status='cancelled',updated=? WHERE id=?", (now(), job['id']))
                else:
                    db.execute("UPDATE jobs SET status='completed',stage='',updated=? WHERE id=?", (now(), job['id']))
                    db.execute('UPDATE videos SET active_run=? WHERE id=?', (job['id'], job['video_id']))
        except Exception as exc:
            message = str(exc) if isinstance(exc, ValueError) else '任务失败：'+type(exc).__name__+'；请检查解码器或模型服务后重试'
            self.finish(job['id'], 'failed', message[:500])

    def finish(self, job_id, status, error=''):
        with self.store.db() as db:
            db.execute("UPDATE jobs SET status=?,error=?,stage='',updated=? WHERE id=?", (status, error, now(), job_id))
