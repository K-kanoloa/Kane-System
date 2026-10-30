#!/usr/bin/python3
"""Runs ONLY in the disposable native image builder, never on the host."""
import hashlib,json,os,shutil,subprocess,tarfile,traceback,time
from pathlib import Path
OUT=Path('/exchange');SRC=Path('/usr/lib/kane-system/source')
def run(*args,**kw):
 print('BUILD',*args,flush=True)
 return subprocess.run(args,check=True,env={**os.environ,'DEBIAN_FRONTEND':'noninteractive','LC_ALL':'C.UTF-8'},**kw)
def main():
 assert os.geteuid()==0 and Path('/proc/1/comm').read_text().strip()=='systemd'
 OUT.mkdir(exist_ok=True);run('mount','-t','9p','-o','trans=virtio,version=9p2000.L','build',str(OUT))
 debs=Path('/mnt/offline-debs');debs.mkdir(exist_ok=True);run('mount','-t','9p','-o','trans=virtio,version=9p2000.L,ro','debs',str(debs))
 spec=json.loads((OUT/'build-spec.json').read_text())
 for item in spec['offline_debs']:
  assert hashlib.sha256((debs/item['name']).read_bytes()).hexdigest()==item['sha256']
 installed=dict(line.split('\t',1) for line in subprocess.check_output(['dpkg-query','-W','-f=${binary:Package}\t${Version}\n'],text=True).splitlines())
 missing=[]
 for x in spec['offline_debs']:
  package=debs/x['name'];fields=subprocess.check_output(['dpkg-deb','-f',str(package),'Package','Version'],text=True).splitlines();values=dict(v.split(': ',1) for v in fields)
  if installed.get(values['Package'],installed.get(values['Package']+':amd64'))!=values['Version']:missing.append(str(package))
 if missing:run('dpkg','-i',*missing)
 SRC.mkdir(parents=True)
 run('tar','-xf',str(OUT/'source.tar'),'-C',str(SRC))
 dependencies=Path('/mnt/offline-runtime');dependencies.mkdir(exist_ok=True)
 if 'agent_archive' in spec or 'model_archive' in spec:run('mount','-t','9p','-o','trans=virtio,version=9p2000.L,ro','runtime-dependencies',str(dependencies))
 for key,path in [('agent_archive',dependencies/'agent_archive.tar.gz'),('model_archive',dependencies/'model_archive.tar.gz')]:
  if key not in spec:continue
  h=hashlib.sha256()
  with path.open('rb') as stream:
   while b:=stream.read(1048576):h.update(b)
  assert h.hexdigest()==spec[key]['sha256']
  if key=='agent_archive':
   run('tar','-xzf',str(path),'-C','/','--transform=s,^workspace/Kane-System/node_modules,usr/lib/kane-system/source/node_modules,','workspace/Kane-System/node_modules','usr/local/bin','opt/kane-test-python','var/tmp/kane-reference-image.tar')
  else:
   run('tar','-xzf',str(path),'-C','/','--exclude=*.gguf','opt/kane-local-model','usr/lib/x86_64-linux-gnu/libgomp.so.1','usr/lib/x86_64-linux-gnu/libgomp.so.1.0.0')
   lock=json.loads((SRC/'deploy/native/libgomp-lock.json').read_text())
   for name in ['usr/lib/x86_64-linux-gnu/libgomp.so.1','usr/lib/x86_64-linux-gnu/libgomp.so.1.0.0']:
    assert hashlib.sha256(Path('/'+name).read_bytes()).hexdigest()==lock['origin_dependency_record']['files'][name]
    os.chown('/'+name,0,0,follow_symlinks=False)
   notice=Path('/usr/share/doc/kane-native-libgomp');notice.mkdir(parents=True,exist_ok=True)
   shutil.copyfile(SRC/'deploy/native/notices/libgomp-copyright',notice/'copyright')
   Path('/usr/share/kane-system').mkdir(parents=True,exist_ok=True)
   shutil.copyfile(SRC/'deploy/native/libgomp-lock.json','/usr/share/kane-system/native-extras.json')
 # Public immutable source/dependencies must be readable by dedicated service UID901.
 # Never apply this normalization to Owner credentials, DB, HOME, Workspace or model data.
 for tree in [SRC,Path('/opt/kane-test-python'),Path('/opt/kane-local-model')]:
  if not tree.exists():continue
  for parent,dirs,files in os.walk(tree,followlinks=False):
   directory=Path(parent);os.chown(directory,0,0);directory.chmod(0o755)
   for name in files:
    f=directory/name
    if f.is_symlink():continue
    executable=bool(f.stat().st_mode&0o111);os.chown(f,0,0);f.chmod(0o755 if executable else 0o644)
 # Keep dependency and licensing provenance distinct from the new system code.
 Path('/usr/share/kane-system').mkdir(parents=True,exist_ok=True)
 Path('/usr/share/kane-system/offline-components.json').write_text(json.dumps({k:spec[k] for k in ['agent_archive','model_archive'] if k in spec},indent=2)+'\n')

 # Fixed UIDs make service/data ownership portable across signed slot replacement.
 run('groupadd','--system','--gid','901','kane-system')
 run('groupadd','--system','--gid','902','kane-runtime-docker')
 run('useradd','--system','--uid','901','--gid','901','--home-dir','/var/lib/kane-system','--shell','/usr/sbin/nologin','kane-system')
 for p in ['/usr/libexec/kane-system','/usr/share/kane-system/trust','/etc/kane-system','/var/lib/kane-system','/var/lib/kane-runtime','/var/lib/kane-models']:
  Path(p).mkdir(parents=True,exist_ok=True)
 for p in ['/var/lib/kane-system','/var/lib/kane-runtime','/var/lib/kane-models']:
  os.chown(p,901,901);os.chmod(p,0o700)
 shutil.copy2(OUT/'kanesys','/usr/bin/kanesys');os.chmod('/usr/bin/kanesys',0o755)
 shutil.copyfile(OUT/'release.pem','/usr/share/kane-system/trust/release.pem')
 native=SRC/'deploy/native'
 for n in ['install-disk','console.py','serial-console','update-slot','model-unit.py','stop-unit.py','maintenance.py']:
  shutil.copyfile(native/n,'/usr/libexec/kane-system/'+n);os.chmod('/usr/libexec/kane-system/'+n,0o755)
 for src,dest in [('status','kane-system-status'),('maintenance','kane-system-maintenance')]:
  shutil.copyfile(native/src,'/usr/bin/'+dest);os.chmod('/usr/bin/'+dest,0o755)
 shutil.copyfile(SRC/'deploy/defaults/core.env','/etc/kane-system/core.env')
 for unit in ['kane-docker.service','kane-agent@.service','kane-model@.service','kane-model-gateway.service','kane-console.service']:
  shutil.copyfile(native/unit,'/usr/lib/systemd/system/'+unit)
 drop=Path('/etc/systemd/system/kane-core.service.d');drop.mkdir(parents=True,exist_ok=True)
 shutil.copyfile(native/'kane-core-native.conf',drop/'native-workspaces.conf')
 for n in ['kane-core.service','kane-system.target']:
  shutil.copyfile(SRC/'deploy/systemd'/n,'/usr/lib/systemd/system/'+n)
 override=Path('/etc/systemd/system/serial-getty@ttyS0.service.d');override.mkdir(parents=True,exist_ok=True)
 (override/'kane.conf').write_text('[Service]\nExecStart=\nExecStart=/usr/libexec/kane-system/serial-console\n')
 for p in (native/'initramfs').rglob('*'):
  if p.is_file():
   dest=Path('/etc/initramfs-tools')/p.relative_to(native/'initramfs');dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(p,dest);dest.chmod(0o755)
 Path('/usr/share/kane-system/role').write_text('installed\n')
 Path('/usr/share/kane-system/build.json').write_text(json.dumps({'version':spec['version'],'sequence':spec['sequence'],'source_fingerprint':spec['source_fingerprint'],'reference_only':True,'base_os':'Ubuntu 24.04 LTS','architecture':'x86_64','real_hardware_verified':False},sort_keys=True)+'\n')
 Path('/usr/share/kane-system/phase-status.json').write_text(json.dumps({'phase0_closed':True,'phase1_closed':True,'phase2_closed':True,'phase3_closed':True,'phase4_closed':False,'phase5_closed':True,'phase6_closed':False,'phase3_performance_backlog_seconds':223.665})+'\n')
 shutil.copyfile(SRC/'LICENSE','/usr/share/kane-system/LICENSE.source')
 run('dpkg-query','-W','-f=${Package}\t${Version}\t${Architecture}\n',stdout=open('/usr/share/kane-system/packages.tsv','w'))
 run('passwd','-l','root')
 # Fresh image has NO preapproved Owner, SSH keys, task DB or machine identity.
 for service in ['ssh.service','ssh.socket','cloud-init.service','cloud-config.service','cloud-final.service','cloud-init-local.service','apt-daily.timer','apt-daily-upgrade.timer']:
  run('systemctl','--root=/','mask',service)
 policy=json.loads((native/'reference-policy.json').read_text())
 for service in policy['masked_services']:run('systemctl','--root=/','mask',service)
 Path('/etc/default/apport').write_text('enabled=0\n')
 shutil.copyfile(native/'reference-policy.json','/usr/share/kane-system/reference-policy.json')
 Path('/etc/cloud/cloud-init.disabled').touch()
 Path('/etc/hostname').write_text('kane-system\n')
 Path('/etc/machine-id').write_text('')
 for p in Path('/etc/ssh').glob('ssh_host_*'):p.unlink()
 for p in [Path('/var/lib/dbus/machine-id'),Path('/var/lib/systemd/random-seed')]:
  if p.exists() or p.is_symlink():p.unlink()
 for p in [Path('/root/.ssh'),Path('/home/ubuntu/.ssh')]:
  if p.exists():shutil.rmtree(p)
 # Actual system branding; retain base provenance fields and licenses.
 Path('/etc/os-release').unlink()
 Path('/etc/os-release').write_text('NAME="Kane System"\nPRETTY_NAME="Kane System '+spec['version']+' (Linux reference)"\nID=kane-system\nID_LIKE=ubuntu\nVERSION_ID="'+spec['version']+'"\nKANE_BASE_ID=ubuntu\nKANE_BASE_VERSION_ID=24.04\n')
 Path('/etc/issue').write_text('Kane System — Native Linux reference\\n\\l\n')
 run('systemctl','--root=/','enable','kane-console.service','kane-system.target')
 run('update-initramfs','-u','-k','all')
 run('systemd-analyze','verify','kane-core.service','kane-system.target','kane-console.service')
 # Disable live builder log writers before deterministic root capture.
 run('systemctl','stop','rsyslog.service')
 for p in [Path('/var/lib/cloud'),Path('/var/log/journal'),Path('/var/cache/apt/archives')]:
  if p.exists():shutil.rmtree(p)
 for p in Path('/var/log').rglob('*'):
  if p.is_file():p.write_bytes(b'')
 # Root tar includes the SEPARATE /boot filesystem as a second explicit operand.
 exclude=['./proc','./sys','./dev','./run','./tmp','./mnt','./exchange','./lost+found','./boot','boot/efi','./usr/local/libexec/kane-native-builder.py','./var/log/*','./var/cache/*','./var/lib/cloud/*']
 cmd=['tar','--sort=name','--mtime=@'+str(spec['epoch']),'--numeric-owner','--xattrs','--acls','--one-file-system',*[f'--exclude={p}' for p in exclude],'-C','/','-cf','-','.','boot']
 payload=OUT/'rootfs.tar.zst'
 with payload.open('wb') as f:
  tar=subprocess.Popen(cmd,stdout=subprocess.PIPE)
  z=subprocess.Popen(['zstd','-3','-T1','-c'],stdin=tar.stdout,stdout=f);tar.stdout.close()
  assert z.wait()==0 and tar.wait()==0
 payload.chmod(0o600)
 with (OUT/'payload-ready.json').open('x') as f:json.dump({'ready':True},f);f.flush();os.fsync(f.fileno())
 for _ in range(1800):
  sig=OUT/'manifest.sig'
  if sig.exists() and sig.stat().st_size==64:break
  time.sleep(1)
 else:raise RuntimeError('host signature not supplied; no installer assembled')
 bundle=Path('/opt/kane-installer/bundle');bundle.mkdir(mode=0o700,parents=True)
 for name in ['rootfs.tar.zst','manifest.json','manifest.sig']:shutil.copyfile(OUT/name,bundle/name);(bundle/name).chmod(0o600)
 run('/usr/bin/kanesys','os','verify',str(bundle),'--key','/usr/share/kane-system/trust/release.pem')
 # The signed installed root retains optional runtimes. The installer needs only
 # its kernel/systemd/Python/Rust/filesystem tools, so do not duplicate those
 # optional binaries outside the signed payload. This touches only this builder.
 for p in [SRC/'node_modules',Path('/opt/kane-test-python'),Path('/opt/kane-local-model')]:
  if p.exists():shutil.rmtree(p)
 for p in Path('/usr/local/bin').iterdir():
  if p.is_file() or p.is_symlink():p.unlink()
 Path('/usr/share/kane-system/role').write_text('installer\n')
 run('systemctl','--root=/','disable','kane-system.target')
 # This is an actual bootable installer root, without a seed or cloud-init dependency.
 Path('/etc/default/grub.d/kane.cfg').write_text('GRUB_DISTRIBUTOR="Kane System Installer"\nGRUB_CMDLINE_LINUX_DEFAULT="console=tty0 console=ttyS0,115200n8"\nGRUB_TIMEOUT=2\n')
 run('update-grub')
 run('sync')
 run('fstrim','-v','/')
 run('sync')
 (OUT/'builder-result.json').write_text(json.dumps({'passed':True,'payload_built':True,'source_fingerprint':spec['source_fingerprint'],'not_installation_acceptance':True})+'\n')
 print('KANE_NATIVE_ROOT_BUILT',flush=True)
if __name__=='__main__':
 try:main()
 except Exception:
  error=traceback.format_exc();print(error,flush=True)
  if OUT.is_mount():(OUT/'builder-error.txt').write_text(error)
