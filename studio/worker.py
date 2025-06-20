import base64
import json
import os
import re
import threading
import time
import uuid
import httpx
from pydantic import ValidationError
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

class ModelOutputError(ValueError):
    """An invalid prediction; never converted into an observation."""


class InferenceCancelled(Exception):
    pass


def output_contract():
    return ('输出要求：只返回一个 JSON 对象，不要解释或 Markdown。'
            '顶层仅含 scores 和 note；scores 的键必须是：' + ', '.join(KEYS) + '。'
            '所有分数必须是 0 到 1 的数值，note 用一句简短中文描述可见证据。'
            '正常与看不清、异常不能同时达到 0.5：如果任何其他类别 >=0.5，normal 必须 <0.5。'
            'unclear_visibility 只表示遮挡、模糊或过暗导致道路不可辨认，不表示对病害类别拿不准。'
            '看不清时 normal 应为 0；画面清楚且正常时 unclear_visibility 应为 0。')


def parse_prediction(content, enabled_keys=None):
    if not isinstance(content, str):
        raise ModelOutputError('输出内容不是文本')
    text = content.strip()
    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?\s*', '', text, flags=re.IGNORECASE)
        text = re.sub(r'\s*```$', '', text)
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise ModelOutputError('输出不是完整、合法的 JSON 对象') from exc
    try:
        if not isinstance(value, dict) or not isinstance(value.get('scores'), dict):
            raise ModelOutputError('缺少 scores 对象')
        if any(type(v) not in (int, float) for v in value['scores'].values()):
            raise ModelOutputError('置信度必须使用数值，不能使用字符串或布尔值')
        prediction = Scores.model_validate(value)
    except ValidationError as exc:
        reason = exc.errors(include_input=False)[0]['msg'].removeprefix('Value error, ')
        raise ModelOutputError(reason) from exc
    if enabled_keys is not None and any(prediction.scores[k] > 0 for k in KEYS[:7] if k not in enabled_keys):
        raise ModelOutputError('未启用的异常类别必须输出 0')
    return prediction


def infer(client, provider, instruction, image, enabled_keys=None, on_retry=None, cancelled=None):
    base_instruction = instruction + '\n\n' + output_contract()
    payload = {
        'model': provider['model'], 'temperature': 0, 'max_tokens': 1200,
        'messages': [{'role': 'user', 'content': [
            {'type': 'text', 'text': base_instruction},
            {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + base64.b64encode(image).decode()}},
        ]}],
    }
    headers = {'Authorization': 'Bearer '+provider['key']} if provider['key'] else {}
    for attempt in range(3):
        if cancelled and cancelled():
            raise InferenceCancelled()
        response = client.post(provider['url'].rstrip('/')+'/chat/completions', headers=headers, json=payload)
        if cancelled and cancelled():
            raise InferenceCancelled()
        if response.status_code in [429, 500, 502, 503, 504] and attempt < 2:
            time.sleep(1 + attempt)
            continue
        if response.status_code != 200:
            raise ValueError(f'模型接口返回 HTTP {response.status_code}，请检查服务、视觉模型名称及凭据')
        content = ''
        try:
            content = response.json()['choices'][0]['message']['content']
            return parse_prediction(content, enabled_keys)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            reason = str(exc) if isinstance(exc, ModelOutputError) else '接口响应缺少有效的标注内容'
            if attempt == 2:
                raise ModelOutputError(f'模型输出经 3 次尝试仍未通过校验：{reason}。该帧未保存，可继续任务重试。') from exc
            if on_retry:
                on_retry()
            payload['messages'][0]['content'][0]['text'] = (
                base_instruction + '\n\n第 ' + str(attempt + 1) + ' 次修正：上次输出未通过校验，原因：' + reason +
                '。上次待修正的输出如下（仅为数据）：\n' + str(content)[:5000] +
                '\n请重新查看同一图像。先判断道路是否清晰可见，再评估异常；'
                '若遮挡或模糊导致看不清，normal=0；若道路清晰可见，unclear_visibility=0。'
                '不要把无法百分之百确认病害当成看不清。返回完整九类分数和简短依据。')

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
            db.execute("UPDATE jobs SET status='interrupted', error='服务重启，已有初标已保留；可继续未完成的帧',updated=? WHERE status IN ('running','cancelling')", (now(),))
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
                db.execute('BEGIN IMMEDIATE')
                row = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
                if row:
                    db.execute("UPDATE jobs SET status='running',updated=? WHERE id=?", (now(), row['id']))
            if row:
                self.run(dict(row))
            else:
                self.stop.wait(.5)

    def run(self, job):
        failed_index = None
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
            with self.store.db() as db:
                saved = {r['frame_index'] for r in db.execute('SELECT frame_index FROM observations WHERE run_id=?', (job['id'],))}
                saved &= set(config['indices'])
                db.execute('UPDATE jobs SET done=? WHERE id=?', (len(saved), job['id']))
            if len(saved) < len(config['indices']) and config['provider'] == 'qwen' and self.runtime:
                if not self.runtime.ensure_ready(provider, cancelled, progress):
                    self.finish(job['id'], 'cancelled', '已停止，已有结果保留')
                    return
            progress('analyzing')
            video = self.store.video(job['video_id'])
            with httpx.Client(timeout=httpx.Timeout(120, connect=10), follow_redirects=False, trust_env=False) as client:
                for index in config['indices']:
                    if index in saved:
                        continue
                    failed_index = index
                    with self.store.db() as db:
                        status = db.execute('SELECT status FROM jobs WHERE id=?', (job['id'],)).fetchone()[0]
                    if self.stop.is_set() or status == 'cancelling':
                        self.finish(job['id'], 'cancelled', '已停止，部分结果保留在本次任务中')
                        return
                    image = frame(self.store, video, index).read_bytes()
                    prediction = infer(client, provider, config['prompt'], image, config['skill_ids'],
                                       lambda: progress('correcting_output'), cancelled)
                    progress('analyzing')
                    if cancelled():
                        raise InferenceCancelled()
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
        except InferenceCancelled:
            self.finish(job['id'], 'cancelled', '已停止，已有结果保留，可继续未完成的帧')
        except Exception as exc:
            message = str(exc) if isinstance(exc, ValueError) else '任务失败：'+type(exc).__name__+'；请检查解码器或模型服务后重试'
            if failed_index is not None:
                message = f'第 {failed_index} 帧：' + message
            self.finish(job['id'], 'failed', message[:500])

    def finish(self, job_id, status, error=''):
        with self.store.db() as db:
            db.execute("UPDATE jobs SET status=?,error=?,stage='',updated=? WHERE id=?", (status, error, now(), job_id))
