#!/usr/bin/env python3
"""Export only hash-pinned project sources from the signed native payload.
Never exports runtime HOME, Owner credentials, registry data or dependency binaries.
"""
import argparse,hashlib,io,json,os,subprocess,tarfile
from pathlib import Path
ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--build',type=Path,required=True);ap.add_argument('--payload',type=Path,required=True);ap.add_argument('--output',type=Path,required=True);a=ap.parse_args()
manifest=json.loads((a.build/'manifest.json').read_text());files=json.loads((a.build/'source-files.json').read_text())
with a.payload.open('rb') as f:assert hashlib.file_digest(f,'sha256').hexdigest()==manifest['payload_sha256']
assert all(not Path(n).is_absolute() and '..' not in Path(n).parts for n in files)
seen=set();read=subprocess.Popen(['zstd','-dc',str(a.payload)],stdout=subprocess.PIPE)
with a.output.open('xb') as f:
 os.fchmod(f.fileno(),0o600);write=subprocess.Popen(['zstd','-3','-T1','-c'],stdin=subprocess.PIPE,stdout=f)
 with tarfile.open(fileobj=read.stdout,mode='r|') as source,tarfile.open(fileobj=write.stdin,mode='w|') as target:
  for member in source:
   n=member.name.removeprefix('./');prefix='usr/lib/kane-system/source/'
   if not n.startswith(prefix):continue
   n=n[len(prefix):]
   if n not in files:continue
   assert member.isfile() and member.size<=8_000_000
   data=source.extractfile(member).read();assert hashlib.sha256(data).hexdigest()==files[n],n
   m=tarfile.TarInfo(n);m.size=len(data);m.uid=m.gid=0;m.mode=0o755 if member.mode&0o111 else 0o644;m.mtime=manifest['source_date_epoch'];target.addfile(m,io.BytesIO(data));seen.add(n)
 write.stdin.close();assert write.wait()==0 and read.wait()==0
assert seen==set(files),set(files)-seen
with a.output.open('rb') as f:digest=hashlib.file_digest(f,'sha256').hexdigest()
a.output.with_suffix(a.output.suffix+'.json').write_text(json.dumps({'sha256':digest,'files':len(files),'source_fingerprint':manifest['source_fingerprint'],'payload_sha256':manifest['payload_sha256'],'scope':'exact project source only, immutable dependency inputs are separately pinned'},indent=2)+'\n');print(digest)
