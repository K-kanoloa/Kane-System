#!/usr/bin/env python3
"""Test-only paired opaque TLS relay. Fixed local target, no remote exposure."""
import asyncio,base64,json,sys
from pathlib import Path
async def main():
    root=Path(sys.argv[1]);sockets={};queue=[]
    async def read(key,reader):
        try:
            while b:=await reader.read(12000):queue.append({'id':key,'data':base64.b64encode(b).decode()})
        finally:queue.append({'id':key,'end':True})
    async def handle(reader,writer):
        try:
            frames=json.loads(await reader.readline())
            for f in frames:
                key=f['id']
                if f.get('open'):
                    r,w=await asyncio.open_connection('127.0.0.1',9443);sockets[key]=w;asyncio.create_task(read(key,r))
                w=sockets.get(key)
                if w:
                    if 'data' in f:w.write(base64.b64decode(f['data']));await w.drain()
                    if f.get('end'):w.close()
            await asyncio.sleep(.03)
            outgoing=[];n=0
            while queue and n<48000:
                f=queue.pop(0);outgoing.append(f);n+=len(f.get('data',''))
            writer.write(json.dumps(outgoing).encode()+b'\n');await writer.drain()
        finally:writer.close()
    async with await asyncio.start_unix_server(handle,path=str(root/'relay.sock')) as server:await server.serve_forever()
asyncio.run(main())
