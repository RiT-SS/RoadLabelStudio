"""One web process; intranet mode listens on loopback and a specific LAN address."""
import argparse
import ipaddress
import os
import socket
import uvicorn

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8780)
    parser.add_argument('--intranet-address', help='Also listen on this private IPv4 address, keeping one job worker')
    args = parser.parse_args()
    if args.intranet_address:
        try:
            address = ipaddress.IPv4Address(args.intranet_address)
        except ValueError:
            parser.error('内网地址必须是有效的 IPv4 地址')
        private_ranges = [ipaddress.ip_network(x) for x in ['10.0.0.0/8','172.16.0.0/12','192.168.0.0/16']]
        if not any(address in network for network in private_ranges):
            parser.error('内网模式只绑定明确的内网地址')
        if args.host != '127.0.0.1' or len(os.getenv('STUDIO_PASSWORD','')) < 12:
            parser.error('内网模式保留本机监听，并要求至少 12 位团队密码')
        os.environ['STUDIO_ENV'] = 'intranet'
        os.environ['STUDIO_INTRANET_ADDRESS'] = str(address)
        os.environ['STUDIO_PORT'] = str(args.port)
    if args.host not in ['127.0.0.1', 'localhost', '::1'] and not os.getenv('STUDIO_PASSWORD'):
        parser.error('对外监听前必须配置 STUDIO_PASSWORD')
    # One process owns the durable job queue; never use multiple web workers here.
    if args.intranet_address:
        sockets = []
        try:
            for address in ['127.0.0.1', args.intranet_address]:
                listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sockets.append(listener)
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind((address,args.port))
                listener.listen(2048)
            config = uvicorn.Config('studio.app:create_app', factory=True, port=args.port, proxy_headers=False)
            uvicorn.Server(config).run(sockets=sockets)
        finally:
            for listener in sockets:
                listener.close()
    else:
        uvicorn.run('studio.app:create_app', factory=True, host=args.host, port=args.port, proxy_headers=False)
