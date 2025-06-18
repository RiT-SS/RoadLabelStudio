"""Decode local allowlisted media only. No shell, URL fetching or filename interpolation."""
import json
import subprocess
import threading
import uuid
from pathlib import Path

DECODE_SLOTS = threading.Semaphore(2)

def probe(path):
    args = ['ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe', '-format_whitelist', 'mov,matroska,webm,avi', '-select_streams', 'v:0', '-show_frames', '-show_streams', '-show_format', '-show_entries', 'frame=best_effort_timestamp_time:stream=codec_type,width,height,avg_frame_rate:format=duration', '-of', 'json', str(path)]
    with DECODE_SLOTS:
        result = subprocess.run(args, capture_output=True, check=True, timeout=180)
    data = json.loads(result.stdout)
    stream = data['streams'][0]
    raw_pts = [float(f['best_effort_timestamp_time']) for f in data['frames'] if 'best_effort_timestamp_time' in f]
    if not raw_pts:
        raise ValueError('视频没有可解码的画面')
    origin = raw_pts[0]
    pts = [round(t-origin, 9) for t in raw_pts]
    if any(b <= a for a, b in zip(pts, pts[1:])):
        raise ValueError('视频时间戳不递增，请先转码')
    numerator, denominator = map(int, stream['avg_frame_rate'].split('/'))
    fps = numerator / denominator if denominator else 25
    duration = max(float(data.get('format', {}).get('duration', 0)), pts[-1] + 1/max(fps, 1))
    if duration > 1800 or stream['width'] * stream['height'] > 4096 * 4096:
        raise ValueError('初版支持 30 分钟以内、最大 4096×4096 的视频')
    return {'pts': pts, 'duration': duration, 'fps': fps, 'width': stream['width'], 'height': stream['height'], 'frame_count': len(pts), 'path': str(Path(path).resolve()), 'playback_path': str(Path(path).resolve()), 'transform': {}, 'road_intervals': []}

def frame(store, video, index):
    meta = video['meta']
    if not 0 <= index < len(meta['pts']):
        raise ValueError('帧号超出范围')
    folder = store.root/'frames'/video['id']
    folder.mkdir(exist_ok=True)
    target = folder/f'{index:08d}.jpg'
    if target.exists():
        return target
    # Decode by native frame number, not guessed FPS or imprecise input seeking.
    filters = [f'select=eq(n\\,{index})']
    transform = meta.get('transform', {})
    noautorotate = []
    if transform:
        noautorotate = ['-noautorotate']
        if transform.get('rotation') == 'clockwise 90 degrees':
            filters.append('transpose=1')
        if transform.get('upright_road_roi'):
            x,y,w,h = transform['upright_road_roi']
            filters.append(f'crop={w}:{h}:{x}:{y}')
    filters.extend(['scale=min(1536\\,iw):-2', 'setsar=1'])
    args = ['ffmpeg', '-v', 'error', '-threads', '1', '-protocol_whitelist', 'file,pipe', '-format_whitelist', 'mov,matroska,webm,avi', *noautorotate, '-i', meta['path'], '-vf', ','.join(filters), '-frames:v', '1', '-threads', '1', '-q:v', '3', '-f', 'image2pipe', '-vcodec', 'mjpeg', 'pipe:1']
    with DECODE_SLOTS:
        result = subprocess.run(args, capture_output=True, check=True, timeout=180)
    if not result.stdout:
        raise ValueError('无法解码该帧')
    temp = target.with_suffix('.'+uuid.uuid4().hex+'.tmp')
    with DECODE_SLOTS:
        temp.write_bytes(result.stdout)
        temp.replace(target)
    return target
