#!/usr/bin/env python3
"""Reproducibly rebuild a signed full-root update from a verified baseline payload.
No filesystem extraction or Agent rewrite. Only the explicitly named runtime/source
and build record are replaced. Owner data never enters a system payload.
"""
import argparse,hashlib,io,json,os,re,subprocess,tarfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def sha(p):
 with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def main():
 ap=argparse.ArgumentParser(description=__doc__)
 for name in ['base-build','base-payload','binary','output','key','public-key']:ap.add_argument('--'+name,type=Path,required=True)
 ap.add_argument('--native-fixes',action='store_true',help='Include explicit native helper fixes and maintenance commands from current source');ap.add_argument('--add-status-command',action='store_true');ap.add_argument('--version',required=True);ap.add_argument('--sequence',type=int,required=True)
 ap.add_argument('--runtime-extras',type=Path,help='Explicit hash-pinned optional CPU backend library/notice input')
 a=ap.parse_args();base=json.loads((a.base_build/'manifest.json').read_text())
 assert a.sequence>base['sequence'] and sha(a.base_payload)==base['payload_sha256']
 subprocess.run(['openssl','pkeyutl','-verify','-pubin','-inkey',str(a.public_key),'-rawin','-in',str(a.base_build/'manifest.json'),'-sigfile',str(a.base_build/'manifest.sig')],check=True)
 a.output.mkdir(mode=0o700,parents=True,exist_ok=False)
 files=json.loads((a.base_build/'source-files.json').read_text())
 # Preserve correspondence for EVERY changed compiled/runtime source, including
 # recovery helpers. Updating a binary with only one hard-coded source delta
 # would give a misleading fingerprint after later Rust changes.
 names=subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard','-z'],cwd=ROOT).decode().split('\0')
 additional=[n for n in names if n and (n.startswith(('deploy/native/','bench/phase6/')) or (n.startswith('scripts/') and 'native' in Path(n).name)) and (ROOT/n).is_file()]
 source_updates={}
 for name in sorted(set(files)|set(additional)):
  path=ROOT/name;assert path.is_file() and not path.is_symlink(),name
  digest=sha(path)
  if files.get(name)!=digest:source_updates['usr/lib/kane-system/source/'+name]=path.read_bytes()
  files[name]=digest
 native_map={}
 if a.native_fixes:
  for name in ['install-disk','console.py','serial-console','update-slot','model-unit.py','stop-unit.py','maintenance.py']:
   native_map['usr/libexec/kane-system/'+name]='deploy/native/'+name
  native_map.update({'usr/bin/kane-system-status':'deploy/native/status','usr/bin/kane-system-maintenance':'deploy/native/maintenance','etc/systemd/system/kane-core.service.d/native-workspaces.conf':'deploy/native/kane-core-native.conf','usr/share/kane-system/reference-policy.json':'deploy/native/reference-policy.json'})
  for unit in ['kane-docker.service','kane-model@.service','kane-model-gateway.service','kane-agent@.service','kane-console.service']:native_map['usr/lib/systemd/system/'+unit]='deploy/native/'+unit
  for name in native_map.values():files[name]=sha(ROOT/name)
 if a.runtime_extras:
  for name in ['deploy/native/libgomp-lock.json','deploy/native/notices/libgomp-copyright']:files[name]=sha(ROOT/name)
 if a.add_status_command:files['deploy/native/status']=sha(ROOT/'deploy/native/status')
 fingerprint=hashlib.sha256(json.dumps(files,sort_keys=True,separators=(',',':')).encode()).hexdigest()
 replacements={'usr/bin/kanesys':a.binary.read_bytes(),**source_updates}
 for dest,src in native_map.items():
  replacements[dest]=(ROOT/src).read_bytes();replacements['usr/lib/kane-system/source/'+src]=(ROOT/src).read_bytes()
 extras={}
 if a.runtime_extras:
  extras=json.loads((a.runtime_extras/'files.json').read_text())
  allowed={'usr/lib/x86_64-linux-gnu/libgomp.so.1','usr/lib/x86_64-linux-gnu/libgomp.so.1.0.0','usr/share/doc/kane-native-libgomp/copyright','usr/share/kane-system/native-extras.json'}
  assert set(extras)==allowed,'only the reviewed optional runtime dependency may be added'
  for name,digest in extras.items():
   path=a.runtime_extras/name;assert sha(path)==digest;replacements[name]=path.read_bytes()
  for name in ['deploy/native/libgomp-lock.json','deploy/native/notices/libgomp-copyright']:replacements['usr/lib/kane-system/source/'+name]=(ROOT/name).read_bytes()
 generated={'etc/default/apport':b'enabled=0\n'} if a.native_fixes else {}
 masks=set('etc/systemd/system/'+n for n in json.loads((ROOT/'deploy/native/reference-policy.json').read_text())['masked_services']) if a.native_fixes else set()
 replacements.update(generated)
 replaced=set();read=subprocess.Popen(['zstd','-dc',str(a.base_payload)],stdout=subprocess.PIPE)
 with (a.output/'rootfs.tar.zst').open('xb') as out:
  os.fchmod(out.fileno(),0o600);write=subprocess.Popen(['zstd','-3','-T1','-c'],stdin=subprocess.PIPE,stdout=out)
  with tarfile.open(fileobj=read.stdout,mode='r|') as source,tarfile.open(fileobj=write.stdin,mode='w|',format=tarfile.PAX_FORMAT) as target:
   for member in source:
    name=member.name.removeprefix('./');data=None
    if name in masks:continue
    if name in replacements:data=replacements[name];replaced.add(name)
    elif name=='etc/os-release':
     text=source.extractfile(member).read().decode();text=re.sub(r'^VERSION_ID=.*$',f'VERSION_ID="{a.version}"',text,flags=re.MULTILINE);text=re.sub(r'^PRETTY_NAME=.*$',f'PRETTY_NAME="Kane System {a.version} (Linux reference)"',text,flags=re.MULTILINE);data=text.encode()
    elif name=='usr/share/kane-system/build.json':
     old=json.loads(source.extractfile(member).read());old.update(version=a.version,sequence=a.sequence,source_fingerprint=fingerprint,runtime_binary_sha256=sha(a.binary));data=(json.dumps(old,sort_keys=True)+'\n').encode();replaced.add(name)
    if data is not None:
     member.size=len(data);member.uid=member.gid=0;member.uname=member.gname='root';member.mtime=base['source_date_epoch'];member.mode=0o755 if name=='usr/bin/kanesys' or name.startswith(('usr/libexec/','usr/bin/')) else 0o644
     member.pax_headers={k:v for k,v in member.pax_headers.items() if k not in ['mtime','atime','ctime','size']};target.addfile(member,io.BytesIO(data))
    else:target.addfile(member,source.extractfile(member) if member.isfile() else None)
   for name in sorted(set(replacements)-replaced):
    data=replacements[name];m=tarfile.TarInfo('./'+name);m.size=len(data);m.uid=m.gid=0;m.uname=m.gname='root';m.mode=0o755 if name.startswith(('usr/libexec/','usr/bin/')) else 0o644;m.mtime=base['source_date_epoch'];target.addfile(m,io.BytesIO(data));replaced.add(name)
   for name in sorted(masks):
    m=tarfile.TarInfo('./'+name);m.uid=m.gid=0;m.uname=m.gname='root';m.mode=0o777;m.mtime=base['source_date_epoch'];m.type=tarfile.SYMTYPE;m.linkname='/dev/null';target.addfile(m)
   if a.native_fixes:
    m=tarfile.TarInfo('./etc/systemd/system/getty.target.wants/kane-console.service');m.uid=m.gid=0;m.mode=0o777;m.mtime=base['source_date_epoch'];m.type=tarfile.SYMTYPE;m.linkname='/usr/lib/systemd/system/kane-console.service';target.addfile(m)
   if a.add_status_command:
    for name in ['usr/bin/kane-system-status','usr/lib/kane-system/source/deploy/native/status']:
     data=(ROOT/'deploy/native/status').read_bytes();m=tarfile.TarInfo('./'+name);m.size=len(data);m.uid=m.gid=0;m.uname=m.gname='root';m.mode=0o755;m.mtime=base['source_date_epoch'];target.addfile(m,io.BytesIO(data))
  write.stdin.close();assert write.wait()==0 and read.wait()==0
 assert replaced==set(replacements)|{'usr/share/kane-system/build.json'},replaced
 manifest={**base,'sequence':a.sequence,'version':a.version,'source_fingerprint':fingerprint,'payload_sha256':sha(a.output/'rootfs.tar.zst'),'payload_bytes':(a.output/'rootfs.tar.zst').stat().st_size}
 (a.output/'manifest.json').write_text(json.dumps(manifest,sort_keys=True,separators=(',',':'))+'\n')
 subprocess.run(['openssl','pkeyutl','-sign','-inkey',str(a.key),'-rawin','-in',str(a.output/'manifest.json'),'-out',str(a.output/'manifest.sig')],check=True)
 (a.output/'source-files.json').write_text(json.dumps(files,indent=2)+'\n')
 (a.output/'repack.json').write_text(json.dumps({'baseline_payload_sha256':base['payload_sha256'],'replacements':sorted(replaced),'binary_sha256':sha(a.binary),'payload_sha256':manifest['payload_sha256'],'no_user_data':True,'source_fingerprint':fingerprint,'generated_branding':'etc/os-release','repack_script_sha256':sha(Path(__file__))},indent=2)+'\n');print(json.dumps(manifest))
if __name__=='__main__':main()
