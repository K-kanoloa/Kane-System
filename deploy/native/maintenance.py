#!/usr/bin/python3
"""Explicit local Owner backup/fresh-install restore using ORIGINAL Rust checkpoints.
No SQL writer, no Agent replay, no inferred refunds. Export is encrypted and signed.
Reference x86-64 native OS only; requires local root after Owner initialization.
"""
import argparse,hashlib,json,os,shutil,subprocess,tarfile,uuid
from pathlib import Path
CORE=['/usr/bin/kanesys','--config','/etc/kane-system/core.env']
def run(*args,**kw):return subprocess.run(args,check=True,**kw)
def text(*args):return subprocess.check_output(args,text=True).strip()
def sha(path):
 with path.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def rpc(op,**body):return json.loads(subprocess.check_output([*CORE,'host-request'],input=json.dumps({'op':op,**body}),text=True))
def private(path):
 m=path.lstat();assert path.is_absolute() and not path.is_symlink() and m.st_uid==0 and not m.st_mode&0o077
 return path

def copy_private(source,target):
 if source.is_symlink():raise RuntimeError('Reference identity backup rejects symlinks; use reviewed explicit policy')
 if source.is_dir():
  target.mkdir(mode=0o700)
  for p in sorted(source.iterdir()):copy_private(p,target/p.name)
 else:
  shutil.copyfile(source,target);target.chmod(0o600 | (source.stat().st_mode & 0o100))
def stop(homes=()):
 run('systemctl','stop','kane-system.target')
 assert not text('systemctl','list-units','--no-legend','--state=active','kane-agent@*.service','kane-model@*.service')
 # Explicitly stop declared persistent HOME mounts as well. Stopping an
 # already inactive target is not evidence that separately started mounts quit.
 for home in sorted(set(homes)):
  path=Path(home);assert path.is_absolute() and path.is_relative_to('/var/lib/kane-runtime') and path!=Path('/var/lib/kane-runtime')
  assert path.resolve()==path
  run('systemctl','stop',text('systemd-escape','--path','--suffix=mount',home))
 assert not any(x.startswith('/var/lib/kane-runtime/') for x in text('findmnt','-rn','-o','TARGET').splitlines()),'bounded HOME must be unmounted'
def owner():
 assert os.geteuid()==0 and Path('/usr/share/kane-system/role').read_text().strip()=='installed'
 return json.loads(Path('/etc/kane-system/owner-initialized.json').read_text())['local_owner']
def password_arg(path):
 private(path);assert path.is_file();return 'file:'+str(path)
def checkpoint_data_filter(member,destination):
 # Keep Python's path/link/special-file checks. Its data filter adds owner
 # write permission to readonly files, which breaks the sealed inventory.
 # Preserve only the archived owner bits; never restore set-id/group access.
 filtered=tarfile.data_filter(member,destination)
 if filtered is not None and filtered.isfile():
  filtered=filtered.replace(mode=member.mode&0o700)
 return filtered
def staging():
 # Native checkpoints may contain several bounded HOME images. /run is a small
 # RAM filesystem and cannot safely hold both checkpoint and restored copies.
 # Use disk-backed, root-private maintenance storage, never Agent/Core data.
 base=Path('/var/cache/kane-system-maintenance')
 base.mkdir(mode=0o700,exist_ok=True)
 private(base);assert base.resolve()==base and base.is_dir()
 assert shutil.disk_usage(base).free >= 1200*1024**2,'insufficient private maintenance staging space'
 stage=base/('operation-'+uuid.uuid4().hex);stage.mkdir(mode=0o700)
 return stage
def backup(a):
 username=owner();assert a.output.is_absolute() and not a.output.exists();private(a.output.parent);a.output.mkdir(mode=0o700)
 keydir=Path('/etc/kane-system/owner');keydir.mkdir(mode=0o700,exist_ok=True);key=keydir/'recovery.pem';pub=keydir/'recovery-public.pem'
 if not key.exists():run('openssl','genpkey','-algorithm','ED25519','-out',str(key));key.chmod(0o600)
 run('openssl','pkey','-in',str(key),'-pubout','-out',str(pub));pub.chmod(0o600)
 # Only the CURRENT authoritative runtime contracts may permit external links.
 # Do not invent a blanket source-directory exemption that no sandbox declared.
 contracts={e['object_id']:e['payload'] for e in rpc('events') if e['kind']=='runtime_configured'}
 dependencies=sorted({p for c in contracts.values() for p in c.get('sandbox',{}).get('read_only',[])})
 stop([c['runtime_home'] for c in contracts.values() if c.get('sandbox',{}).get('home_capacity_bytes')])
 identity=Path('/var/lib/kane-runtime/system-identity')
 if identity.exists():
  private(identity);assert identity.is_dir()
  # Preserve earlier complete/incomplete identity snapshots; never overwrite
  # shadow, recovery keys or failure evidence just to retry a backup.
  os.rename(identity,identity.with_name('system-identity-previous-'+uuid.uuid4().hex))
 identity.mkdir(mode=0o700)
 for name in ['passwd','shadow','group','gshadow']:copy_private(Path('/etc')/name,identity/name)
 copy_private(Path('/etc/kane-system'),identity/'kane-system');copy_private(Path('/home')/username,identity/'owner-home')
 modes={}
 for parent,dirs,files in os.walk('/etc/kane-system'):
  for path in [Path(parent),*[Path(parent)/x for x in files]]:
   assert not path.is_symlink();m=path.stat();modes[str(path.relative_to('/etc/kane-system'))]={'mode':m.st_mode&0o777,'uid':m.st_uid,'gid':m.st_gid}
 (identity/'config-metadata.json').write_text(json.dumps(modes,sort_keys=True)+'\n')
 mounts=identity/'mount-units';mounts.mkdir(mode=0o700)
 for c in contracts.values():
  home=c.get('runtime_home')
  if not home or not c.get('sandbox',{}).get('home_capacity_bytes'):continue
  assert Path(home).is_absolute() and Path(home).is_relative_to('/var/lib/kane-runtime')
  # Hyphens inside a path component are escaped as literal \x2d in unit names.
  # Derive the real mount name rather than guessing it with an unescaped glob.
  name=text('systemd-escape','--path','--suffix=mount',home)
  path=Path('/etc/systemd/system')/name
  if not (mounts/name).exists():copy_private(path,mounts/name)
 stage=staging()
 spec=stage/'spec.json';spec.write_text(json.dumps({'roots':[{'name':'runtime','path':'/var/lib/kane-runtime','kind':'directory','max_bytes':512*1024**2}],'readonly_dependencies':dependencies}));spec.chmod(0o600)
 result=json.loads(subprocess.check_output([*CORE,'checkpoint','--spec',str(spec),str(stage/'checkpoint')],text=True))
 cipher=a.output/'checkpoint.enc'
 with cipher.open('xb') as f:
  os.fchmod(f.fileno(),0o600);tar=subprocess.Popen(['tar','-czf','-','-C',str(stage),'checkpoint'],stdout=subprocess.PIPE)
  enc=subprocess.Popen(['openssl','enc','-aes-256-cbc','-pbkdf2','-iter','200000','-salt','-pass',password_arg(a.password_file)],stdin=tar.stdout,stdout=f);tar.stdout.close();assert enc.wait()==0 and tar.wait()==0;f.flush();os.fsync(f.fileno())
 manifest={'format':1,'cipher_sha256':sha(cipher),'checkpoint_result':result,'core_schema':9,'owner_username':username,'automatic_replay':False,'weights_backup':'not included; immutable Owner-approved weights are separately hash-identified','fresh_install_restore_only':True}
 meta=a.output/'manifest.json';meta.write_text(json.dumps(manifest,sort_keys=True)+'\n');meta.chmod(0o600)
 run('openssl','pkeyutl','-sign','-inkey',str(key),'-rawin','-in',str(meta),'-out',str(a.output/'manifest.sig'))
 shutil.copyfile(pub,a.output/'recovery-public.pem')
 print(json.dumps({'backup':str(a.output),'trusted_public_key_sha256':sha(pub),'checkpoint':result,'scope':'encrypted native data + separate identity snapshot; model weights/OS dependencies reinstalled from known inputs','services_still_stopped':True}))
 shutil.rmtree(stage)
def restore(a):
 username=owner();assert a.confirm=='RESTORE-PRESERVING-UNKNOWN'
 assert not rpc('tasks'),'fresh native install required; existing Tasks will not be overwritten'
 bundle=a.source;pub=bundle/'recovery-public.pem';assert sha(pub)==a.expected_key_sha256,'out-of-band Owner recovery key mismatch'
 run('openssl','pkeyutl','-verify','-pubin','-inkey',str(pub),'-rawin','-in',str(bundle/'manifest.json'),'-sigfile',str(bundle/'manifest.sig'))
 manifest=json.loads((bundle/'manifest.json').read_text());assert manifest['format']==1 and manifest['core_schema']==9 and manifest['owner_username']==username
 assert sha(bundle/'checkpoint.enc')==manifest['cipher_sha256'],'encrypted bytes altered; no restoration started'
 assert not any(Path('/var/lib/kane-runtime').iterdir()),'native data mapping is not empty'
 stop();stage=staging()
 p=subprocess.Popen(['openssl','enc','-d','-aes-256-cbc','-pbkdf2','-iter','200000','-pass',password_arg(a.password_file),'-in',str(bundle/'checkpoint.enc')],stdout=subprocess.PIPE)
 with tarfile.open(fileobj=p.stdout,mode='r|gz') as t:t.extractall(stage,filter=checkpoint_data_filter)
 assert p.wait()==0
 cp=stage/'checkpoint';run('/usr/bin/kanesys','checkpoint-check',str(cp))
 run('/usr/bin/kanesys','checkpoint-restore',str(cp),str(stage/'restored'))
 # Rust installs only verified bytes under original EMPTY native mappings.
 db=Path('/var/lib/kane-system')/('recovered-'+uuid.uuid4().hex+'.db')
 run('/usr/bin/kanesys','--db',str(db),'restore',str(stage/'restored/registry.db'));os.chown(db,901,901)
 run('/usr/bin/kanesys','--db',str(db),'checkpoint-install',str(cp),'--root','runtime')
 data=Path('/var/lib/kane-runtime');identity=data/'system-identity';preserved=Path('/var/lib/kane-system')/('fresh-identity-'+uuid.uuid4().hex);preserved.mkdir(mode=0o700)
 for name in ['passwd','shadow','group','gshadow']:shutil.copyfile(Path('/etc')/name,preserved/name)
 shutil.copytree('/etc/kane-system',preserved/'kane-system',symlinks=True)
 for name in ['passwd','shadow','group','gshadow']:
  shutil.copyfile(identity/name,Path('/etc')/name);(Path('/etc')/name).chmod(0o600 if name in ['shadow','gshadow'] else 0o644)
 # Preserve the fresh Owner's home before activating the verified previous home.
 home=Path('/home')/username;os.rename(home,home.with_name(home.name+'.fresh-'+uuid.uuid4().hex))
 shutil.copytree(identity/'owner-home',home)
 shutil.copytree(identity/'kane-system','/etc/kane-system',dirs_exist_ok=True)
 for relative,metadata in json.loads((identity/'config-metadata.json').read_text()).items():
  restored=Path('/etc/kane-system')/relative;restored.chmod(metadata['mode']);os.chown(restored,metadata['uid'],metadata['gid'])
 config=Path('/etc/kane-system/core.env');body=config.read_text();lines=[('KANE_SYSTEM_DB='+str(db)) if line.startswith('KANE_SYSTEM_DB=') else line for line in body.splitlines()]
 tmp=config.with_name('core.env.recovery-new');tmp.write_text('\n'.join(lines)+'\n');tmp.chmod(0o600)
 with tmp.open('rb') as f:os.fsync(f.fileno())
 os.replace(tmp,config);config.chmod(0o644)
 for path in (identity/'mount-units').iterdir():shutil.copyfile(path,Path('/etc/systemd/system')/path.name);(Path('/etc/systemd/system')/path.name).chmod(0o644)
 for parent,dirs,files in os.walk(data):
  # The checkpoint's Owner identity snapshot contains shadow/recovery keys.
  # It stays root-private even though restored Agent data belongs to Core UID901.
  if Path(parent)==data:
   for name in list(dirs):
    if name=='system-identity' or name.startswith('system-identity-previous-'):dirs.remove(name)
  os.chown(parent,901,901)
  for name in files:
   path=Path(parent)/name
   if not path.is_symlink():os.chown(path,901,901)
 uid=int(text('id','-u',username));gid=int(text('id','-g',username))
 for parent,dirs,files in os.walk(home):
  os.chown(parent,uid,gid)
  for name in files:
   path=Path(parent)/name
   if not path.is_symlink():os.chown(path,uid,gid)
 lock=Path(str(db)+'.service-lock')
 if lock.exists():os.chown(lock,901,901)
 run('systemctl','daemon-reload')
 for path in (identity/'mount-units').iterdir():run('systemctl','enable',path.name)
 run('sync')
 print(json.dumps({'restored':True,'selected_registry':str(db),'fresh_registry_and_identity_preserved':str(preserved),'tasks_quarantined_by_original_Rust_checkpoint_restore':True,'automatic_runtime_launch':False,'services_still_stopped':True}))
 shutil.rmtree(stage)
def main():
 ap=argparse.ArgumentParser(description=__doc__);sub=ap.add_subparsers(dest='op',required=True)
 b=sub.add_parser('backup');b.add_argument('--output',type=Path,required=True)
 r=sub.add_parser('restore');r.add_argument('--source',type=Path,required=True);r.add_argument('--expected-key-sha256',required=True);r.add_argument('--confirm',required=True)
 for p in [b,r]:p.add_argument('--password-file',type=Path,required=True)
 a=ap.parse_args();os.umask(0o077);backup(a) if a.op=='backup' else restore(a)
if __name__=='__main__':main()
