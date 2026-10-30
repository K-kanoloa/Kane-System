#!/usr/bin/env python3
"""Rebuild a private, Owner-free installer from a verified signed root update.
Reuses its kernel/dependencies; the init=/bin/sh VM is a BUILDER ONLY, never an
acceptance environment. Final acceptance must boot a separate blank target normally.
"""
import argparse,hashlib,json,os,shlex,shutil,subprocess,tarfile,time
from pathlib import Path
import pexpect
ROOT=Path(__file__).resolve().parents[1]
def sha(p):
 with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def installer_members(files):
 names={'usr/libexec/kane-system/'+x for x in ['install-disk','console.py','serial-console','update-slot','model-unit.py','stop-unit.py','maintenance.py']}
 names|={'usr/bin/kanesys','usr/bin/kane-system-status','usr/bin/kane-system-maintenance','usr/share/kane-system/build.json'}
 names|={'usr/lib/systemd/system/'+x for x in ['kane-docker.service','kane-model@.service','kane-model-gateway.service','kane-agent@.service','kane-console.service']}
 names|={'etc/systemd/system/kane-core.service.d/native-workspaces.conf','usr/share/kane-system/reference-policy.json','etc/default/apport'}
 return names|{'usr/lib/kane-system/source/'+x for x in files}

def main():
 ap=argparse.ArgumentParser(description=__doc__)
 ap.add_argument('--builder-memory-mb',type=int,choices=[1024,1536],default=1536,help='Private patch builder only; never an acceptance profile')
 ap.add_argument('--wait-before-convert',action='store_true',help='Bounded host-only storage gate after builder stops, before standalone conversion')
 for name in ['installer','base-payload','bundle','output','scratch']:ap.add_argument('--'+name,type=Path,required=True)
 a=ap.parse_args();out=a.output.resolve();work=a.scratch.resolve();out.mkdir(mode=0o700,parents=True,exist_ok=False);work.mkdir(mode=0o700,parents=True,exist_ok=False)
 bundle=a.bundle.resolve();manifest=json.loads((bundle/'manifest.json').read_text());assert sha(bundle/'rootfs.tar.zst')==manifest['payload_sha256']
 # Prepare only explicitly selected files on the authorized host. The guest
 # still verifies the FULL signed payload using its installed trust key before
 # applying this hash-pinned patch. This avoids a full Python tar scan in TCG.
 files=json.loads((bundle/'source-files.json').read_text())
 assert hashlib.sha256(json.dumps(files,sort_keys=True,separators=(',',':')).encode()).hexdigest()==manifest['source_fingerprint']
 names=installer_members(files);seen=set();patch=out/'installer-files.tar'
 read=subprocess.Popen(['zstd','-dc',str(bundle/'rootfs.tar.zst')],stdout=subprocess.PIPE)
 with tarfile.open(fileobj=read.stdout,mode='r|') as source,tarfile.open(patch,mode='w') as target:
  for m in source:
   n=m.name.removeprefix('./')
   if n not in names:continue
   assert m.isfile() and m.uid==0 and m.size<=32_000_000 and '..' not in Path(n).parts
   data=source.extractfile(m).read()
   if n.startswith('usr/lib/kane-system/source/'):assert hashlib.sha256(data).hexdigest()==files[n[len('usr/lib/kane-system/source/'):]]
   header=tarfile.TarInfo(n);header.size=len(data);header.mode=m.mode;header.uid=header.gid=0;target.addfile(header,__import__('io').BytesIO(data));seen.add(n)
 assert read.wait()==0 and seen==names
 (out/'installer-members.json').write_text(json.dumps(sorted(names))+'\n');patch_hash=sha(patch)
 # Extract boot inputs from the exact prior signed payload, never a host kernel.
 p=subprocess.Popen(['zstd','-dc',str(a.base_payload)],stdout=subprocess.PIPE);found={}
 with tarfile.open(fileobj=p.stdout,mode='r|') as t:
  for m in t:
   n=m.name.removeprefix('./')
   for prefix,filename in [('boot/vmlinuz-','vmlinuz'),('boot/initrd.img-','initrd')]:
    if n.startswith(prefix) and m.isfile():
     assert filename not in found,'one pinned reference kernel required'
     with (out/filename).open('xb') as f:shutil.copyfileobj(t.extractfile(m),f)
     found[filename]={'member':n,'sha256':sha(out/filename)}
 assert p.wait()==0 and set(found)=={'vmlinuz','initrd'}
 script=r'''import pathlib,os,json,shutil,subprocess,tarfile
assert os.geteuid()==0 and pathlib.Path('/proc/1/comm').read_text().strip() in ['sh','bash']
assert pathlib.Path('/usr/share/kane-system/role').read_text().strip()=='installer'
assert not pathlib.Path('/etc/kane-system/owner-initialized.json').exists()
assert not pathlib.Path('/var/lib/kane-system/state.db').exists()
os.umask(0o077)
bundle=pathlib.Path('/incoming');dest=pathlib.Path('/opt/kane-installer/bundle')
for name in ['rootfs.tar.zst','manifest.json','manifest.sig']:
 shutil.copyfile(bundle/name,dest/name);(dest/name).chmod(0o600)
subprocess.run(['/usr/bin/kanesys','os','verify',str(dest),'--key','/usr/share/kane-system/trust/release.pem'],check=True)
names=set(json.loads(pathlib.Path('/exchange/installer-members.json').read_text()))
seen=set();patch=pathlib.Path('/exchange/installer-files.tar')
import hashlib
assert hashlib.sha256(patch.read_bytes()).hexdigest()=='KANE_INSTALLER_PATCH_DIGEST'
with tarfile.open(patch,mode='r:') as t:
 for m in t:
  n=m.name.removeprefix('./')
  if n in names:
   assert m.isfile() and m.uid==0 and '..' not in pathlib.Path(n).parts
   path=pathlib.Path('/')/n;path.parent.mkdir(parents=True,exist_ok=True)
   with path.open('wb') as f:shutil.copyfileobj(t.extractfile(m),f)
   os.chown(path,0,0);path.chmod(0o755 if n.startswith(('usr/libexec/','usr/bin/')) else 0o644);seen.add(n)
assert names==seen,(names-seen)
# Installer boot uses the same signed, headless reference policy as the payload.
# Do not run systemd or initialize an Owner in this private builder.
policy=json.loads(pathlib.Path('/usr/share/kane-system/reference-policy.json').read_text())
for name in policy['masked_services']:
 assert '/' not in name and name.endswith(('.service','.timer','.socket'))
 path=pathlib.Path('/etc/systemd/system')/name
 if path.exists() or path.is_symlink():path.unlink()
 path.symlink_to('/dev/null')
link=pathlib.Path('/etc/systemd/system/getty.target.wants/kane-console.service');link.parent.mkdir(parents=True,exist_ok=True)
if link.exists() or link.is_symlink():link.unlink()
link.symlink_to('/usr/lib/systemd/system/kane-console.service')
subprocess.run(['sync'],check=True);subprocess.run(['fstrim','/'],check=True);subprocess.run(['sync'],check=True)
pathlib.Path('/exchange/builder-result.json').write_text(json.dumps({'builder_only':True,'owner_initialized':False,'runtime_database_created':False,'patched_from_verified_payload':True,'copied':sorted(seen)}))
print('KANE_PATCH_BUILDER_FINISHED',flush=True)
import ctypes
os.sync();assert ctypes.CDLL(None,use_errno=True).reboot(0x4321fedc)==0
'''
 (out/'builder.py').write_text(script.replace('KANE_INSTALLER_PATCH_DIGEST',patch_hash))
 env={k:v for k,v in os.environ.items() if k not in ['DOCKER_HOST','DOCKER_CONTEXT','DOCKER_TLS','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH']}
 prefix=['docker','--host=unix:///var/run/docker.sock','run','--rm','--network=none','--cap-drop=ALL','--security-opt=no-new-privileges','--pids-limit=128','--memory=5g','--cpus=2','--user',f'{os.getuid()}:{os.getgid()}','--mount',f'type=bind,src={work},dst=/work','--mount',f'type=bind,src={out},dst=/exchange','--mount',f'type=bind,src={bundle},dst=/incoming,readonly','--mount',f'type=bind,src={a.installer.resolve()},dst=/base.img,readonly','--workdir=/work']
 image='kane-system-validation:20261018'
 def run(*cmd):subprocess.run([*prefix,image,*cmd],env=env,check=True)
 run('qemu-img','create','-f','qcow2','-F','qcow2','-b','/base.img','builder.qcow2')
 cmd=[*prefix,'-i',image,'qemu-system-x86_64','-machine','q35','-accel','tcg,thread=multi,tb-size=128','-cpu','max','-smp','2','-m',str(a.builder_memory_mb),'-kernel','/exchange/vmlinuz','-initrd','/exchange/initrd','-append','root=/dev/vda1 rw init=/bin/sh console=ttyS0,115200n8','-drive','file=builder.qcow2,if=virtio,format=qcow2,cache=none,discard=unmap,detect-zeroes=unmap','-virtfs','local,path=/exchange,mount_tag=build,security_model=none','-virtfs','local,path=/incoming,mount_tag=incoming,security_model=none,readonly=on','-display','none','-serial','stdio','-monitor','none','-nic','none','-no-reboot']
 with (out/'builder-console.log').open('w') as log:
  child=pexpect.spawn(cmd[0],cmd[1:],env=env,encoding='utf-8',codec_errors='replace',echo=False,timeout=1200);child.logfile_read=log
  child.expect(r'# ');child.sendline('export PATH=/usr/sbin:/usr/bin:/sbin:/bin; mount -t proc proc /proc 2>/dev/null || :; mount -t sysfs sysfs /sys 2>/dev/null || :; mkdir -p /exchange /incoming; mount -t 9p -o trans=virtio,version=9p2000.L build /exchange && mount -t 9p -o trans=virtio,version=9p2000.L,ro incoming /incoming && python3 /exchange/builder.py')
  child.expect('KANE_PATCH_BUILDER_FINISHED',timeout=1800);child.expect(pexpect.EOF,timeout=120);child.close();assert child.exitstatus==0
 assert json.loads((out/'builder-result.json').read_text())['builder_only']
 if a.wait_before_convert:
  (out/'builder-stopped-before-conversion.json').write_text(json.dumps({'builder_stopped':True,'guest_memory_mb':a.builder_memory_mb,'reason':'host-only storage relocation; no acceptance claim'})+'\n')
  deadline=time.monotonic()+180
  while not (out/'conversion-approved').is_file():
   assert time.monotonic()<deadline,'host storage conversion gate expired; keep builder evidence'
   time.sleep(.5)
 run('qemu-img','convert','-O','qcow2','-c','builder.qcow2','/exchange/installer.qcow2');run('qemu-img','compare','-f','qcow2','-F','qcow2','builder.qcow2','/exchange/installer.qcow2')
 result={'builder_only':True,'base_installer_sha256':sha(a.installer),'signed_manifest':manifest,'kernel_inputs':found,'installer_sha256':sha(out/'installer.qcow2'),'scratch_sha256':sha(work/'builder.qcow2'),'accepted':False,'standalone_sectors_compared':True,'builder_driver_sha256':sha(Path(__file__)),'installer_cli_and_project_source':'copied from the fully verified current signed payload; no stale installer CLI/source fingerprint'}
 (out/'build-result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
if __name__=='__main__':main()
