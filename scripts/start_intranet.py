"""Persistent intranet service with a private credential file and two explicit listeners."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
MODEL_ENV_KEYS = {
    'QWEN_BASE_URL', 'QWEN_MODEL', 'QWEN_API_KEY',
    'DEEPSEEK_BASE_URL', 'DEEPSEEK_MODEL', 'DEEPSEEK_API_KEY',
    'QWEN_AUTO_MANAGE', 'QWEN_IDLE_SECONDS', 'QWEN_LOAD_TIMEOUT_SECONDS',
    'QWEN_GPU_IDS', 'QWEN_MODEL_PATH', 'STUDIO_PRESENCE_SECONDS',
    'QWEN_MAX_INSTANCES', 'STUDIO_MAX_CONCURRENT_JOBS',
}

def model_environment(config):
    values = config.get('model_environment', {})
    if not isinstance(values, dict) or set(values) - MODEL_ENV_KEYS:
        raise ValueError('model_environment 仅接受 Qwen / DeepSeek 的模型连接配置')
    if any(not isinstance(value, str) or '\0' in value for value in values.values()):
        raise ValueError('模型连接配置必须是有效字符串')
    return values

def private_write(path, text):
    # Refuse symlinks and keep passwords out of process arguments and logs.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(text)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--address', required=True)
    parser.add_argument('--port', type=int, default=8780)
    args = parser.parse_args()
    address = ipaddress.IPv4Address(args.address)
    if not any(address in ipaddress.ip_network(n) for n in ['10.0.0.0/8','172.16.0.0/12','192.168.0.0/16']):
        parser.error('请指定服务器实际使用的内网地址')
    if not 1 <= args.port <= 65535:
        parser.error('端口无效')
    data = ROOT/'data'
    data.mkdir(exist_ok=True)
    credentials = data/'intranet_config.json'
    state_path = data/'intranet_server.json'
    config = json.loads(credentials.read_text()) if credentials.exists() else {'password':secrets.token_urlsafe(18)}
    model_env = model_environment(config)
    if len(config.get('password','')) < 12:
        raise ValueError('团队密码至少需要 12 位')
    config.update(address=str(address), port=args.port)
    private_write(credentials, json.dumps(config, indent=2))
    url = f'http://{address}:{args.port}'
    private_write(data/'内网访问凭据.txt', f'路见 · 内网访问\n\n访问地址：{url}\n团队密码：{config["password"]}\n\n同一团队使用此密码登录。无需公网域名或 SSH 端口转发。\n')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    if state_path.exists():
        state = json.loads(state_path.read_text())
        try:
            cmd = Path(f'/proc/{state["pid"]}/cmdline').read_bytes().split(b'\0')
            if (str(ROOT/'run.py').encode() in cmd and b'--intranet-address' in cmd
                    and state.get('address') == str(address) and state.get('port') == args.port):
                with opener.open(url+'/health', timeout=2) as response:
                    if json.load(response).get('ok'):
                        print(json.dumps(state));return
        except (OSError,KeyError,ValueError):
            pass
    # Do not stop any unknown listener or launch a second worker on the same DB.
    for host in ['127.0.0.1',str(address)]:
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            probe.bind((host,args.port))
    env = {**os.environ, **model_env, 'STUDIO_ENV':'intranet','STUDIO_INTRANET_ADDRESS':str(address),'STUDIO_PASSWORD':config['password']}
    with (data/'intranet.log').open('ab',buffering=0) as log:
        process = subprocess.Popen([sys.executable,str(ROOT/'run.py'),'--port',str(args.port),'--intranet-address',str(address)],cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL,start_new_session=True)
    for _ in range(50):
        if process.poll() is not None:
            raise RuntimeError('启动失败，请查看 data/intranet.log')
        try:
            with opener.open(url+'/health',timeout=1) as response:
                if json.load(response).get('ok'):
                    state={'pid':process.pid,'address':str(address),'port':args.port,'url':url,'loopback_url':f'http://localhost:{args.port}','access_mode':'intranet','local_only':False}
                    state_path.write_text(json.dumps(state,indent=2))
                    (data/'preview_server.json').write_text(json.dumps(state,indent=2))
                    print(json.dumps(state));return
        except OSError:
            time.sleep(.2)
    process.terminate()
    raise RuntimeError('服务未能启动，请查看 data/intranet.log')

if __name__=='__main__':
    main()
