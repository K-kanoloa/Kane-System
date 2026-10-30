#!/usr/bin/env python3
"""Exercise installed native maintenance with Owner authentication and private input.
Copies only encrypted backup bytes, public recovery key, metadata and logs out.
The generated recovery password is NOT part of the evidence share or public logs.
"""
import argparse,hashlib,json,secrets,traceback,shutil
from pathlib import Path
from native_preflight import require_native_storage
from validate_native_import import Vm,ROOT
from importlib.util import spec_from_file_location,module_from_spec
s=spec_from_file_location('native_exercise',Path(__file__).with_name('exercise-native-system.py'));m=module_from_spec(s);s.loader.exec_module(m)

def main():
 ap=argparse.ArgumentParser(description=__doc__)
 for n in ['acceptance','installer','scratch','output']:ap.add_argument('--'+n,type=Path,required=True)
 ap.add_argument('--identity-permission-probe',action='store_true',help='Actual root-private synthetic file to verify archived-identity access after restore')
 ap.add_argument('--bootstrap-core',type=Path,help='Explicit conservation-only offline checkpoint CLI; never replaces the installed Core or counts as signed native acceptance')
 ap.add_argument('--memory-mb',type=int,choices=[2048,3072],default=3072)
 a=ap.parse_args();require_native_storage(output=a.output, scratch=a.scratch, workspace=ROOT);out=a.output.resolve();out.mkdir(mode=0o700,parents=True,exist_ok=False);e=out/'evidence';e.mkdir(mode=0o700);private=out/'private-inputs';private.mkdir(mode=0o700)
 if a.bootstrap_core:
  shutil.copyfile(a.bootstrap_core,private/'kanesys');(private/'kanesys').chmod(0o755)
  body=(ROOT/'deploy/native/maintenance.py').read_text().replace("CORE=['/usr/bin/kanesys'", "CORE=['/private-backup-input/kanesys'")
  (private/'maintenance-bootstrap.py').write_text(body);(private/'maintenance-bootstrap.py').chmod(0o600)
 secret=private/'password';secret.write_text(secrets.token_urlsafe(48)+'\n');secret.chmod(0o600)
 credentials=json.loads((a.acceptance/'credentials.json').read_text());vm=Vm(a.scratch.resolve(),a.installer.resolve(),e,ROOT/'bench/phase6','native-backup',private,memory_mb=a.memory_mb);result={'status':'IN_PROGRESS','phase6_closed':False,'guest_memory_mb':a.memory_mb,'scope':'encrypted native-system backup only; fresh-install restore is a separate required test'}
 try:
  vm.boot();m.login(vm,credentials)
  script="""import pathlib,subprocess,json,shutil,time
for tag,path in [('evidence','/exchange'),('assets','/private-backup-input')]:
 pathlib.Path(path).mkdir(exist_ok=True);subprocess.run(['mount','-t','9p','-o','trans=virtio,version=9p2000.L'+(',ro' if tag=='assets' else ''),tag,path],check=True)
shutil.copyfile('/private-backup-input/password','/run/kane-recovery-password');pathlib.Path('/run/kane-recovery-password').chmod(0o600)
q=['kanesys','--config','/etc/kane-system/core.env','host-request']
def rpc(op,**kw):return json.loads(subprocess.check_output(q,input=json.dumps({'op':op,**kw}),text=True))
subprocess.run(['systemctl','start','kane-docker.service'],check=True)
for _docker_probe in range(120):
 _docker_ready=subprocess.run(['/usr/local/bin/docker','info','--format','{{.ServerVersion}}'],capture_output=True,text=True)
 if _docker_ready.returncode==0:break
 time.sleep(.5)
else:raise RuntimeError('Container backend unavailable: physical absence is NOT proven; refuse reconcile')

for line in subprocess.check_output(['systemctl','list-units','--no-legend','--state=active','kane-agent@*.service','kane-model@*.service'],text=True).splitlines():subprocess.run(['systemctl','stop',line.split()[0]],check=True)
# Existing trusted physical-absence reconciliation may release dead process
# resources; it must never settle/refund Unknown model usage or replay Tasks.
held_before=rpc('recovery_queue')
for claim in rpc('host_resources')['claims']:
 if claim['held']:subprocess.run(['kanesys','--config','/etc/kane-system/core.env','reconcile-runtime','--agent',claim['agent_id']],check=True)
for row in rpc('model_instances'):
 if row.get('held'):subprocess.run(['kanesys','--config','/etc/kane-system/core.env','model-reconcile',row['id']],check=True)
assert rpc('recovery_queue')==held_before,'physical cleanup changed unresolved usage/actions'
before={'tasks':rpc('tasks'),'artifacts':{t['id']:rpc('artifacts',id=t['id']) for t in rpc('tasks')},'recovery_queue':rpc('recovery_queue'),'budgets':{t['spec']['budget_id']:rpc('model_budget',id=t['spec']['budget_id']) for t in rpc('tasks')},'agents':{p.stem:rpc('runtime_status',id=p.stem) for p in pathlib.Path('/etc/kane-system/runtimes').glob('*.json')}}
pathlib.Path('/exchange/before-backup.json').write_text(json.dumps(before,indent=2))
import uuid
root=pathlib.Path('/var/tmp')/('kane-native-backup-'+uuid.uuid4().hex);root.mkdir(mode=0o700)
p=subprocess.run(['kane-system-maintenance','backup','--output',str(root/'export'),'--password-file','/run/kane-recovery-password'],capture_output=True,text=True)
pathlib.Path('/exchange/backup.log').write_text(p.stdout+p.stderr);print(p.stdout,p.stderr);p.check_returncode()
record=json.loads(p.stdout);pathlib.Path('/exchange/backup-result.json').write_text(json.dumps(record,indent=2))
assert not subprocess.check_output(['systemctl','list-units','--no-legend','--state=active','kane-core.service','kane-agent@*.service','kane-model@*.service'],text=True)
shutil.copytree(root/'export','/exchange/encrypted-backup')
# Check the REAL encrypted export with the ORIGINAL Rust validator before any
# disposable source-disk retirement. Plaintext identity stays inside this guest.
check=root/'decrypt-check';check.mkdir(mode=0o700)
dec=subprocess.Popen(['openssl','enc','-d','-aes-256-cbc','-pbkdf2','-iter','200000','-pass','file:/run/kane-recovery-password','-in',str(root/'export/checkpoint.enc')],stdout=subprocess.PIPE)
tar=subprocess.Popen(['tar','-xzf','-','-C',str(check)],stdin=dec.stdout);dec.stdout.close();assert tar.wait()==0 and dec.wait()==0
v=subprocess.run(['kanesys','checkpoint-check',str(check/'checkpoint')],capture_output=True,text=True)
pathlib.Path('/exchange/decrypted-checkpoint-check.log').write_text(v.stdout+v.stderr);v.check_returncode()
shutil.rmtree(check)
pathlib.Path('/run/kane-recovery-password').unlink()
"""
  if a.identity_permission_probe:
   marker="held_before=rpc('recovery_queue')"
   probe="""import uuid
probe=pathlib.Path('/var/lib/kane-runtime')/('system-identity-previous-permission-probe-'+uuid.uuid4().hex);probe.mkdir(mode=0o700)
p=probe/'private-marker';p.write_text('public reference bytes; root-private access is the tested property\\n');p.chmod(0o600)
deny=subprocess.run(['runuser','-u','kane-system','--','cat',str(p)],capture_output=True);assert deny.returncode!=0
pathlib.Path('/exchange/private-probe-before.json').write_text(json.dumps({'root_uid':probe.stat().st_uid,'private_mode':probe.stat().st_mode&0o777,'core_read_denied':True}))
"""
   script=script.replace(marker,probe+marker)
  if a.bootstrap_core:
   script=script.replace("['kane-system-maintenance','backup'", "['python3','/private-backup-input/maintenance-bootstrap.py','backup'")
   script=script.replace("['kanesys','checkpoint-check'", "['/private-backup-input/kanesys','checkpoint-check'")
   hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in [private/'kanesys',private/'maintenance-bootstrap.py']}
   prefix="import pathlib,hashlib,json\n"
   for name,digest in hashes.items():prefix+=f"assert hashlib.sha256(pathlib.Path('/private-backup-input/{name}').read_bytes()).hexdigest()=={digest!r}\n"
   # Mounting occurs before the conservation-only helper's explicit byte check.
   marker="shutil.copyfile('/private-backup-input/password'"
   position=script.index(marker);script=script[:position]+prefix+script[position:]
   result['conservation_only_bootstrap_helpers']=hashes;result['signed_native_backup_acceptance']=False
  vm.command(script,timeout=1200)
  bundle=e/'encrypted-backup';manifest=json.loads((bundle/'manifest.json').read_text())
  with (bundle/'checkpoint.enc').open('rb') as f:assert hashlib.file_digest(f,'sha256').hexdigest()==manifest['cipher_sha256']
  result.update(status='PASS',backup_manifest=manifest,fresh_install_restore_tested=False,encrypted_export_decrypted_and_original_Rust_checkpoint_validated=True)
  vm.send('sudo -n systemctl poweroff');vm.expect(__import__('pexpect').EOF,timeout=180)
 except Exception:result.update(status='FAIL',failure=traceback.format_exc());raise
 finally:
  (out/'result.json').write_text(json.dumps(result,indent=2)+'\n');vm.stop()
if __name__=='__main__':main()
