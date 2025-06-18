"""Start a durable loopback preview, keeping logs and PID in the app data folder."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request

ROOT=Path(__file__).resolve().parents[1]

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=8780)
    args=parser.parse_args()
    data=ROOT/'data';data.mkdir(exist_ok=True)
    state_path=data/'preview_server.json'
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    if state_path.exists():
        state=json.loads(state_path.read_text())
        pid=state.get('pid',0)
        try:
            cmd=Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
            if str(ROOT/'run.py').encode() in cmd and state.get('port')==args.port:
                with opener.open(f'http://127.0.0.1:{args.port}/health',timeout=2) as r:
                    if json.load(r).get('ok'):
                        print(json.dumps(state));return
        except (OSError,ValueError):
            pass
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        probe.bind(('127.0.0.1',args.port))
    log=(data/'preview.log').open('ab',buffering=0)
    process=subprocess.Popen([sys.executable,str(ROOT/'run.py'),'--port',str(args.port)],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL,start_new_session=True)
    for _ in range(50):
        if process.poll() is not None:
            raise RuntimeError('预览服务启动失败，请查看 data/preview.log')
        try:
            with opener.open(f'http://127.0.0.1:{args.port}/health',timeout=1) as r:
                if json.load(r).get('ok'):
                    state={'pid':process.pid,'port':args.port,'url':f'http://localhost:{args.port}','local_only':True}
                    state_path.write_text(json.dumps(state,indent=2));print(json.dumps(state));return
        except OSError:
            time.sleep(.2)
    process.terminate()
    raise RuntimeError('预览服务没有在预期时间内响应')

if __name__=='__main__':main()
