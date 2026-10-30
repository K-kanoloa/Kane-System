#!/usr/bin/env python3
"""Read an immutable installer in a private kernel for exact offline inventory.
This is a read-only build/inventory operation, NEVER installation/Owner acceptance.
No Owner, registry, runtime, network or mutable installer overlay is created.
"""
import argparse,hashlib,json,os,subprocess,traceback
from pathlib import Path
import pexpect

def sha(p):
 with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def main():
 p=argparse.ArgumentParser(description=__doc__)
 for n in ['build','output','payload-directory']:p.add_argument('--'+n,type=Path,required=True)
 a=p.parse_args();b=a.build.resolve();o=a.output.resolve();o.mkdir(mode=0o700,parents=True,exist_ok=False);d=a.payload_directory.resolve();d.mkdir(mode=0o700,parents=True,exist_ok=False)
 m=json.loads((b/'manifest.json').read_text());r={'status':'IN_PROGRESS','scope':'read-only inventory extraction, not OS or Owner acceptance','manifest':m}
 e=os.environ.copy()
 for k in list(e):
  if k.startswith('DOCKER_'):e.pop(k)
 name='kane-p6-inventory-'+os.urandom(6).hex();child=None
 c=['docker','--host=unix:///var/run/docker.sock','run','--rm','-i','--name',name,'--network=none','--cap-drop=ALL','--security-opt=no-new-privileges','--memory=3g','--cpus=2','--pids-limit=128','--user',f'{os.getuid()}:{os.getgid()}','--mount',f'type=bind,src={b},dst=/build,readonly','--mount',f'type=bind,src={d},dst=/inventory','kane-system-validation:20261018','qemu-system-x86_64','-machine','q35','-accel','tcg,thread=multi,tb-size=128','-cpu','max','-smp','2','-m','512','-kernel','/build/installer-build/vmlinuz','-initrd','/build/installer-build/initrd','-append','root=/dev/vda1 ro rootflags=noload fsck.mode=skip init=/bin/sh console=ttyS0,115200n8','-drive','file=/build/installer-build/installer.qcow2,if=virtio,format=qcow2,readonly=on','-virtfs','local,path=/inventory,mount_tag=inventory,security_model=none','-display','none','-serial','stdio','-monitor','none','-nic','none','-no-reboot']
 (o/'arguments.json').write_text(json.dumps(c,indent=2))
 try:
  with (o/'console.log').open('w') as log:
   child=pexpect.spawn(c[0],c[1:],env=e,encoding='utf-8',codec_errors='replace',timeout=600,maxread=65536,echo=False);child.logfile_read=log;child.expect(r'# ')
   code="import pathlib,os,subprocess,shutil; assert pathlib.Path('/proc/1/comm').read_text().strip() in ['sh','bash']; assert pathlib.Path('/usr/share/kane-system/role').read_text().strip()=='installer'; assert not pathlib.Path('/etc/kane-system/owner-initialized.json').exists(); assert not pathlib.Path('/var/lib/kane-system/state.db').exists(); subprocess.run(['/usr/bin/kanesys','os','verify','/opt/kane-installer/bundle','--key','/usr/share/kane-system/trust/release.pem'],check=True); shutil.copyfile('/opt/kane-installer/bundle/rootfs.tar.zst','/exchange/rootfs.tar.zst'); os.chown('/exchange/rootfs.tar.zst',"+str(os.getuid())+","+str(os.getgid())+"); os.chmod('/exchange/rootfs.tar.zst',384); os.sync(); print('KANE_INVENTORY_'+'COPIED',flush=True)"
   program=d/'inventory-program.py';program.write_text(code+'\n');program.chmod(0o400)
   (o/'program-sha256.json').write_text(json.dumps({'sha256':sha(program),'delivery':'short UART command plus exact code file over existing local 9P; not a long serial paste'}))
   child.sendline("export PATH=/usr/sbin:/usr/bin:/sbin:/bin; mount -t 9p -o trans=virtio,version=9p2000.L inventory /exchange && printf 'INVENTORY_' && printf 'MOUNT_READY\\n'")
   child.expect('INVENTORY_MOUNT_READY',timeout=120)
   child.sendline('python3 /exchange/inventory-program.py');child.expect('KANE_INVENTORY_COPIED',timeout=1200)
   child.sendline('sync; /sbin/poweroff -f');child.expect(pexpect.EOF,timeout=60)
  assert sha(d/'rootfs.tar.zst')==m['payload_sha256'];r.update(status='PASS',payload_sha256=m['payload_sha256'],payload=str(d/'rootfs.tar.zst'),read_only_installer=True,Owner_initialized=False)
 except Exception:r.update(status='FAIL',failure=traceback.format_exc());raise
 finally:
  subprocess.run(['docker','--host=unix:///var/run/docker.sock','kill',name],env=e,capture_output=True)
  if child:child.close()
  (o/'result.json').write_text(json.dumps(r,indent=2)+'\n')
if __name__=='__main__':main()
