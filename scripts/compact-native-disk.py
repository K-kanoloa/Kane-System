#!/usr/bin/env python3
"""Lossless offline compaction of this task's disposable native reference disk.
No guest code/DB mutation. All logical sectors must compare equal before replacing
this generated QCOW container. Never use unsafe shared-open flags or a live VM.
"""
import argparse,hashlib,json,os,shutil,subprocess
from pathlib import Path
ap=argparse.ArgumentParser(description=__doc__)
for n in ['scratch','temporary','output']:ap.add_argument('--'+n,type=Path,required=True)
a=ap.parse_args();work=a.scratch.resolve(strict=True);source=work/'target.qcow2';assert source.is_file() and not source.is_symlink() and source.stat().st_uid==os.getuid();out=a.output.resolve();out.mkdir(mode=0o700,parents=True,exist_ok=False);temp=a.temporary.resolve();temp.mkdir(mode=0o700,parents=True,exist_ok=False)
env={k:v for k,v in os.environ.items() if not k.startswith('DOCKER_')}
assert not subprocess.check_output(['docker','--host=unix:///var/run/docker.sock','ps','--filter','name=kane-p6-','--format','{{.ID}}'],env=env,text=True).strip(),'stop native VM first'
def sha(p):
 with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
old=sha(source);record={'source':str(source),'original_container_sha256':old,'original_bytes':source.stat().st_size,'scope':'Host artifact compaction only; not Task recovery or native OS acceptance','logical_sector_comparison':False}
(out/'record.json').write_text(json.dumps(record,indent=2)+'\n')
prefix=['docker','--host=unix:///var/run/docker.sock','run','--rm','--network=none','--cap-drop=ALL','--security-opt=no-new-privileges','--pids-limit=64','--memory=4g','--cpus=2','--user',f'{os.getuid()}:{os.getgid()}','--mount',f'type=bind,src={work},dst=/source,readonly','--mount',f'type=bind,src={temp},dst=/compressed','kane-system-validation:20261018']
with (out/'qemu-img.log').open('w') as log:
 subprocess.run([*prefix,'qemu-img','convert','-c','-O','qcow2','-o','compression_type=zstd','/source/target.qcow2','/compressed/target.qcow2'],env=env,check=True,stdout=log,stderr=subprocess.STDOUT)
 subprocess.run([*prefix,'qemu-img','compare','-f','qcow2','-F','qcow2','/source/target.qcow2','/compressed/target.qcow2'],env=env,check=True,stdout=log,stderr=subprocess.STDOUT)
compact=temp/'target.qcow2';new=sha(compact);assert sha(source)==old,'source changed during offline operation; preserve both'
record.update(logical_sector_comparison=True,compressed_container_sha256=new,compressed_bytes=compact.stat().st_size,compression='standard QCOW2 zstd; QEMU8.2; independent of guest filesystem/permissions')
(out/'record.json').write_text(json.dumps(record,indent=2)+'\n')
source.unlink();shutil.move(compact,source);source.chmod(0o600);assert sha(source)==new
record['replaced_generated_container_after_full_sector_compare']=True;(out/'record.json').write_text(json.dumps(record,indent=2)+'\n');print(json.dumps(record))
