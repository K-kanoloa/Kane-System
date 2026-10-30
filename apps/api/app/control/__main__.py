"""Explicit local Owner initialization and HTTPS launcher; no cloud service."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import stat
from urllib.parse import urlsplit
import uvicorn
from ..system.client import SystemClient

def main():
    parser=argparse.ArgumentParser(description='Kane System local Owner HTTPS control')
    sub=parser.add_subparsers(dest='command',required=True)
    for name in ('init','pair-code'):
        p=sub.add_parser(name);p.add_argument('--pairing-file',required=True,type=Path,help='New private file for one-use code (never overwritten)')
        if name=='init':p.add_argument('--origin',required=True,help='Fixed https://hostname:port; certificate must match')
    p=sub.add_parser('serve');p.add_argument('--cert',required=True,type=Path);p.add_argument('--key',required=True,type=Path);p.add_argument('--listen',default='127.0.0.1');p.add_argument('--port',type=int,default=9443)
    args=parser.parse_args();client=SystemClient()
    if not client.socket:parser.error('KANE_SYSTEM_SOCKET required; no DB fallback')
    if args.command!='serve':
        if args.pairing_file.parent.stat().st_mode&0o077:parser.error('Pairing output requires private directory (0700)')
        # Reserve destination before consuming an init/code request; do not lose a code to a typo.
        fd=os.open(args.pairing_file,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        async def issue():return await client.request('control_auth',action='init' if args.command=='init' else 'pair_code',body={'origin':args.origin} if args.command=='init' else {})
        with os.fdopen(fd,'w') as stream:
            result=asyncio.run(issue())
            json.dump(result,stream);stream.write('\n');stream.flush();os.fsync(stream.fileno())
        print(f'One-use pairing grant saved to private file {args.pairing_file}; expires in 5 minutes. Do not publish it.')
        return
    origin=os.getenv('KANE_CONTROL_ORIGIN','');parsed=urlsplit(origin)
    if parsed.scheme!='https' or (parsed.port or 443)!=args.port:parser.error('KANE_CONTROL_ORIGIN must match HTTPS listener port')
    for path in (args.cert,args.key):
        if path.is_symlink() or not path.is_file():parser.error('TLS cert/key must be regular files')
    if args.key.stat().st_uid!=os.getuid() or stat.S_IMODE(args.key.stat().st_mode)&0o077:parser.error('TLS private key must be owned by current Host UID and mode 0600')
    uvicorn.run('app.control.app:create_control_app',factory=True,host=args.listen,port=args.port,ssl_certfile=str(args.cert),ssl_keyfile=str(args.key),proxy_headers=False,access_log=False,server_header=False,ws_max_size=16384)
if __name__=='__main__':main()
