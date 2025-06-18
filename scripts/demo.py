"""Run an isolated UI demo with synthetic media and preset labels; no AI calls.

Requires Pillow and FFmpeg. Refuses a nonempty data directory. This is a UI
fixture, not a road-damage dataset or a model accuracy demonstration.
"""
import argparse
import os
from pathlib import Path
import random
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw
from studio.domain import KEYS
from studio.media import probe
from studio.storage import Store, encode


def picture(variant, index):
    image = Image.new('RGB', (960, 540), '#99afa0')
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 960, 155), fill='#d4e3e4')
    draw.polygon([(330, 155), (630, 155), (950, 540), (10, 540)], fill='#646d72')
    randomizer = random.Random(7)
    for _ in range(2800):
        x, y = randomizer.randrange(960), randomizer.randrange(156, 540)
        edge = 330 - (y - 155) * 320 / 385
        if edge < x < 960 - edge:
            draw.point((x, y), fill=randomizer.choice(['#717a7f', '#565f64', '#848b8c']))
    draw.line([(335, 164), (45, 540)], fill='#ececdf', width=5)
    draw.line([(625, 164), (915, 540)], fill='#ececdf', width=5)
    for y in range(185, 560, 92):
        y += (index % 24) * 2
        width = int(4 + (y - 150) / 50)
        draw.polygon([(480-width, y), (480+width, y), (482+width, y+35), (478-width, y+35)], fill='#e4d7a4')
    shift = int(12 * (index % 32) / 32)
    if variant == 0:
        points = [(568, 275+shift), (552, 310+shift), (575, 333+shift), (554, 359+shift), (568, 387+shift), (541, 430+shift)]
        draw.line(points, fill='#303537', width=5)
        draw.line([(575, 333+shift), (622, 348+shift), (646, 383+shift)], fill='#303537', width=3)
        draw.ellipse((260, 365, 371, 401), fill='#4b4f4f', outline='#8d918a', width=4)
    elif variant == 1:
        draw.ellipse((500, 329, 739, 401), fill='#8cabb9', outline='#bdcbd0', width=4)
        draw.arc((535, 338, 700, 380), 10, 160, fill='#d5e1e1', width=3)
    else:
        draw.line([(214, 395), (751, 395)], fill='#596267', width=3)
    draw.rounded_rectangle((22, 20, 510, 53), radius=8, fill='#193029')
    draw.text((35, 30), 'SYNTHETIC DEMO  /  NO REAL ROAD DATA', fill='#ffffff')
    return image


def seed(directory):
    if directory.exists() and any(directory.iterdir()):
        raise ValueError('Demo requires a new or empty directory; existing data is never modified.')
    store = Store(directory)
    (directory/'DEMO_ONLY').write_text('Synthetic media and preset labels. No model inference.\n')
    stamp = '2026-01-01T00:00:00+00:00'
    names = ['演示 A · 裂缝与破损', '演示 B · 路面积水', '演示 C · 待初标']
    for variant, name in enumerate(names):
        identifier = f'demo-{variant+1}'
        path = directory/'videos'/f'{identifier}.mp4'
        command = ['ffmpeg', '-v', 'error', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', '960x540',
                   '-r', '8', '-i', 'pipe:0', '-c:v', 'libx264', '-threads', '1', '-pix_fmt', 'yuv420p', str(path)]
        process = subprocess.Popen(command, stdin=subprocess.PIPE)
        try:
            for index in range(96):
                process.stdin.write(picture(variant, index).tobytes())
            process.stdin.close()
            if process.wait() != 0:
                raise RuntimeError('Demo media encoding failed')
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait()
        meta = probe(path)
        meta['source'] = 'demo'
        meta['road_intervals'] = [{'start_s': 0, 'end_s': 5, 'value': 0}, {'start_s': 5, 'end_s': 8, 'value': 1}, {'start_s': 8, 'end_s': 12, 'value': 0}]
        run = f'demo-run-{variant+1}'
        note = '演示数据：程序绘制画面与预设分数，仅展示操作流程，非模型推理结果。'
        with store.db() as db:
            skills = [dict(row) for row in db.execute('SELECT * FROM skills')]
            config = {'provider': 'qwen', 'model': 'Qwen2.5-VL-3B-Instruct', 'sampling': 'every_frame',
                      'fps': 8, 'skill_ids': [skill['id'] for skill in skills], 'skills_snapshot': skills, 'demo_only': True}
            db.execute('INSERT INTO videos VALUES(?,?,?,?,?)', (identifier, name, encode(meta), stamp, run if variant<2 else None))
            db.execute('INSERT INTO jobs(id,video_id,status,config,done,total,created,updated,stage) VALUES(?,?,?,?,?,?,?,?,?)',
                       (run, identifier, 'completed' if variant<2 else 'queued', encode(config), 96 if variant<2 else 0, 96, stamp, stamp, ''))
            if variant == 2:
                continue
            for index in range(96):
                scores = dict.fromkeys(KEYS, 0.)
                if 24 <= index < 45 or 54 <= index < 62:
                    scores['crack' if variant == 0 else 'water'] = .93
                    if variant == 0:
                        scores['surface_breakage'] = .74
                elif 70 <= index < 76:
                    scores['unclear_visibility'] = .7
                else:
                    scores['normal'] = .91
                db.execute('INSERT INTO observations VALUES(?,?,?,?,?)', (run, index, encode(scores), note, 'model_observed'))
                if index < (24 if variant == 0 else 8):
                    db.execute('INSERT INTO edits VALUES(?,?,?,?,?,?,?,?)', (identifier, index, encode(scores), note, 'confirmed', 1, stamp, 'demo'))
    return store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--port', type=int, default=18780)
    args = parser.parse_args()
    # Do not inherit live credentials or model connections into a demo process.
    for key in list(os.environ):
        if key.startswith(('STUDIO_', 'QWEN_', 'DEEPSEEK_')):
            os.environ.pop(key)
    os.environ.update(QWEN_AUTO_MANAGE='1', QWEN_BASE_URL='http://127.0.0.1:18790/v1')
    seed(args.data_dir.resolve())
    from studio.app import create_app
    import uvicorn
    # The queue is a UI fixture: nothing will execute or load a GPU model.
    app = create_app(args.data_dir, run_worker=False)
    uvicorn.run(app, host='127.0.0.1', port=args.port, access_log=False)


if __name__ == '__main__':
    main()
