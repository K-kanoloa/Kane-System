#!/usr/bin/env python3
"""Offline native reference image builder using a clean official Linux base.
Builds in a disposable local VM. Never contacts a remote Docker daemon or installs
on the development host. Building an image does not constitute acceptance.
"""
from __future__ import annotations
import argparse,base64,hashlib,json,os,shutil,subprocess,tarfile,time
from pathlib import Path
from native_preflight import require_native_storage
ROOT=Path(__file__).resolve().parents[1]
def sha(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  while b:=f.read(1048576):h.update(b)
 return h.hexdigest()
def main():
 ap=argparse.ArgumentParser(description=__doc__)
 for arg in ['base','binary','debs','output','scratch','signing-key','public-key']:
  ap.add_argument('--'+arg,type=Path,required=True)
 ap.add_argument('--version',default='0.3.0-reference.1');ap.add_argument('--sequence',type=int,default=1)
 ap.add_argument('--source-date-epoch',type=int,required=True)
 ap.add_argument('--agent-runtime-archive',type=Path)
 ap.add_argument('--model-runtime-archive',type=Path)
 ap.add_argument('--compact-output',action='store_true',help='Keep exact payload inside the verified installer; remove redundant external copy before image conversion')
 a=ap.parse_args();require_native_storage(output=a.output, scratch=a.scratch, workspace=ROOT);out=a.output.resolve();scratch=a.scratch.resolve()
 out.mkdir(mode=0o700,parents=True,exist_ok=False);scratch.mkdir(mode=0o700,parents=True,exist_ok=False)
 env=os.environ.copy()
 for key in ['DOCKER_HOST','DOCKER_CONTEXT','DOCKER_TLS','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH']:env.pop(key,None)
 def run(cmd,**kw):return subprocess.run(cmd,env=env,check=True,**kw)
 debs=sorted(a.debs.resolve().glob('*.deb'));assert debs,'offline Ubuntu packages required'
 inputs={'base':{'path':str(a.base.resolve()),'sha256':sha(a.base)},'binary_sha256':sha(a.binary),'offline_debs':[{'name':p.name,'sha256':sha(p),'bytes':p.stat().st_size} for p in debs], 'version':a.version,'sequence':a.sequence,'epoch':a.source_date_epoch,'mode':'offline local QEMU builder; not acceptance'}
 for name,p in [('agent_archive',a.agent_runtime_archive),('model_archive',a.model_runtime_archive)]:
  if p:inputs[name]={'sha256':sha(p),'bytes':p.stat().st_size}
 # Only source owned by this project; no worktree reset/index write/private caches.
 names=subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard','-z'],cwd=ROOT).decode().split('\0')
 prefixes=('core/','models/','supervisor/','adapters/','packages/kanaloa-tools/','apps/api/app/','deploy/native/','deploy/systemd/','deploy/defaults/','bench/phase6/')
 selected=sorted(n for n in set(names) if n and (n.startswith(prefixes) or n in ['Cargo.lock','Cargo.toml','LICENSE','package.json','package-lock.json','pnpm-lock.yaml'] or (n.startswith('scripts/') and 'native' in Path(n).name)) and (ROOT/n).is_file())
 manifest={n:sha(ROOT/n) for n in selected};inputs['source_fingerprint']=hashlib.sha256(json.dumps(manifest,sort_keys=True,separators=(',',':')).encode()).hexdigest()
 (out/'source-files.json').write_text(json.dumps(manifest,indent=2)+'\n');(out/'inputs.json').write_text(json.dumps(inputs,indent=2)+'\n')
 with tarfile.open(out/'source.tar','w') as t:
  for n in selected:t.add(ROOT/n,arcname=n,recursive=False)
 shutil.copyfile(a.binary,out/'kanesys');(out/'kanesys').chmod(0o755);shutil.copyfile(a.public_key,out/'release.pem')
 spec={**inputs,'source_date_epoch':a.source_date_epoch};(out/'build-spec.json').write_text(json.dumps(spec))
 guest=(ROOT/'scripts/build-native-os-guest.py').read_bytes()
 user={'users':[],'ssh_pwauth':False,'disable_root':True,'package_update':False,'package_upgrade':False,'cloud_init_modules':['seed_random','write_files','growpart','resizefs','set_hostname'],'cloud_config_modules':['runcmd'],'cloud_final_modules':['scripts_user','final_message'],'write_files':[{'path':'/usr/local/libexec/kane-native-builder.py','permissions':'0700','encoding':'b64','content':base64.b64encode(guest).decode()}],'runcmd':[['python3','/usr/local/libexec/kane-native-builder.py'],['systemctl','poweroff']]}
 (scratch/'user-data').write_text('#cloud-config\n'+json.dumps(user));(scratch/'meta-data').write_text(json.dumps({'instance-id':f'kane-builder-{a.sequence}-{out.name}','local-hostname':'kane-builder'}));(scratch/'network-config').write_text('version: 2\nethernets:\n  builder:\n    match: {name: "en*"}\n    dhcp4: true\n    optional: true\n')
 prefix=['docker','--host=unix:///var/run/docker.sock','run','--rm','--network=none','--cap-drop=ALL','--security-opt=no-new-privileges','--pids-limit=128','--memory=6g','--cpus=2','--user',f'{os.getuid()}:{os.getgid()}','--mount',f'type=bind,src={scratch},dst=/work','--mount',f'type=bind,src={out},dst=/exchange','--mount',f'type=bind,src={a.base.resolve()},dst=/base.img,readonly','--mount',f'type=bind,src={a.debs.resolve()},dst=/debs,readonly','--workdir=/work']
 for tag,p in [('agent_archive',a.agent_runtime_archive),('model_archive',a.model_runtime_archive)]:
  if p:prefix+=['--mount',f'type=bind,src={p.resolve()},dst=/inputs/{tag}.tar.gz,readonly']
 image='kane-system-validation:20261018'
 run([*prefix,image,'genisoimage','-quiet','-output','seed.iso','-volid','cidata','-joliet','-rock','user-data','meta-data','network-config'])
 run([*prefix,image,'qemu-img','create','-f','qcow2','-F','qcow2','-b','/base.img','builder.qcow2','12G'])
 cmd=[*prefix,image,'qemu-system-x86_64','-machine','q35','-accel','tcg,thread=multi','-cpu','max','-smp','2','-m','1792','-drive','file=builder.qcow2,if=virtio,format=qcow2,cache=none,discard=unmap,detect-zeroes=unmap','-drive','file=seed.iso,media=cdrom,readonly=on','-virtfs','local,path=/exchange,mount_tag=build,security_model=none','-virtfs','local,path=/debs,mount_tag=debs,security_model=none,readonly=on','-display','none','-serial','stdio','-monitor','none','-nic','user,model=virtio-net-pci,restrict=on','-no-reboot']
 if a.agent_runtime_archive or a.model_runtime_archive:cmd+=['-virtfs','local,path=/inputs,mount_tag=runtime-dependencies,security_model=none,readonly=on']
 before=time.monotonic();m=None
 # Two-phase build handshake: the private signing key never enters the VM.
 with (out/'builder-console.log').open('w') as log:
  process=subprocess.Popen(cmd,env=env,stdout=log,stderr=subprocess.STDOUT)
  try:
   while process.poll() is None:
    if (out/'payload-ready.json').exists() and m is None:
     payload=out/'rootfs.tar.zst'
     m={'format':1,'product':'Kane System','version':a.version,'sequence':a.sequence,'architecture':'x86_64','payload':payload.name,'payload_sha256':sha(payload),'payload_bytes':payload.stat().st_size,'core_schema_min':9,'core_schema_max':9,'source_fingerprint':inputs['source_fingerprint'],'source_date_epoch':a.source_date_epoch}
     (out/'manifest.json').write_text(json.dumps(m,sort_keys=True,separators=(',',':'))+'\n')
     run(['openssl','pkeyutl','-sign','-inkey',str(a.signing_key.resolve()),'-rawin','-in',str(out/'manifest.json'),'-out',str(out/'manifest.sig')])
    if time.monotonic()-before>3000:raise TimeoutError('builder timed out; retain raw console and disk')
    time.sleep(1)
   assert process.returncode==0, f'builder QEMU exited {process.returncode}'
  finally:
   if process.poll() is None:process.terminate();process.wait(timeout=30)
 assert json.loads((out/'builder-result.json').read_text())['passed'], 'builder failure; inspect unmodified log'
 assert m is not None
 # Convert the finished installer to a standalone image: NO backing dependency.
 if a.compact_output:
  assert m['payload_sha256'] in (out/'builder-console.log').read_text() and 'KANE_NATIVE_ROOT_BUILT' in (out/'builder-console.log').read_text()
  (out/'payload-disposition.json').write_text(json.dumps({'external_duplicate_removed':True,'retained_inside_installer':'/opt/kane-installer/bundle/rootfs.tar.zst','payload_sha256':m['payload_sha256'],'guest_full_verification_logged':True})+'\n')
  (out/'rootfs.tar.zst').unlink()
 run([*prefix,image,'qemu-img','convert','-O','qcow2','-c','builder.qcow2','/exchange/installer.qcow2'])
 run([*prefix,image,'qemu-img','compare','-f','qcow2','-F','qcow2','builder.qcow2','/exchange/installer.qcow2'])
 inputs['installer_sha256']=sha(out/'installer.qcow2')
 (out/'installer.sha256').write_text(inputs['installer_sha256']+'  installer.qcow2\n')
 (out/'build-result.json').write_text(json.dumps({'payload_built':True,'seconds':time.monotonic()-before,'manifest':m,'installed_os_acceptance':False},indent=2)+'\n')
 print(json.dumps({'bundle':str(out),'payload_sha256':m['payload_sha256'],'installer':str(out/'installer.qcow2'),'next':'independent empty-disk install and cold-boot acceptance required'}))
if __name__=='__main__':main()
