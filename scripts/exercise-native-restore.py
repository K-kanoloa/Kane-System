#!/usr/bin/env python3
"""Fresh native-install restore with actual Owner authentication and original checkpoints.
Recovery password stays in a private read-only input mount, never evidence/log text.
No SQL writes, Unknown settlement or automatic Task/Agent replay.
"""
import argparse,json,traceback,hashlib
from pathlib import Path
from native_preflight import require_native_storage
from validate_native_import import Vm,ROOT
from importlib.util import spec_from_file_location,module_from_spec
s=spec_from_file_location('native_exercise',Path(__file__).with_name('exercise-native-system.py'));m=module_from_spec(s);s.loader.exec_module(m)
def main():
 ap=argparse.ArgumentParser(description=__doc__)
 for n in ['acceptance','installer','scratch','backup','output']:ap.add_argument('--'+n,type=Path,required=True)
 ap.add_argument('--source-acceptance',type=Path,required=True,help='Private original Owner credential reference, outside encrypted export/evidence')
 ap.add_argument('--memory-mb',type=int,choices=[2048,3072],default=2048)
 a=ap.parse_args();require_native_storage(output=a.output, scratch=a.scratch, workspace=ROOT);out=a.output.resolve();out.mkdir(mode=0o700,parents=True,exist_ok=False);e=out/'evidence';e.mkdir(mode=0o700)
 assert json.loads((a.backup/'result.json').read_text())['status']=='PASS'
 creds=json.loads((a.acceptance/'credentials.json').read_text());record=json.loads((a.backup/'evidence/backup-result.json').read_text());pin=record['trusted_public_key_sha256']
 before=json.loads((a.backup/'evidence/before-backup.json').read_text())
 vm=Vm(a.scratch.resolve(),a.installer.resolve(),e,ROOT/'bench/phase6','native-fresh-restore',a.backup.resolve(),memory_mb=a.memory_mb);result={'status':'IN_PROGRESS','phase6_closed':False,'guest_memory_mb':a.memory_mb,'automatic_replay':False}
 try:
  vm.boot();m.login(vm,creds)
  vm.command(f"""import pathlib,subprocess,json,shutil,hashlib,time
for tag,path in [('evidence','/exchange'),('assets','/private-recovery-input')]:
 pathlib.Path(path).mkdir(exist_ok=True);subprocess.run(['mount','-t','9p','-o','trans=virtio,version=9p2000.L'+(',ro' if tag=='assets' else ''),tag,path],check=True)
q=['kanesys','--config','/etc/kane-system/core.env','host-request']
def rpc(op,**kw):return json.loads(subprocess.check_output(q,input=json.dumps({{'op':op,**kw}}),text=True))
def start_core():
 subprocess.run(['systemctl','start','kane-system.target'],check=True)
 for attempt in range(100):
  p=subprocess.run(q,input=json.dumps({{'op':'health'}}),text=True,capture_output=True)
  if p.returncode==0:return
  time.sleep(0.2)
 raise RuntimeError('Core did not become healthy within 20 seconds')
assert not rpc('tasks') and not any(pathlib.Path('/var/lib/kane-runtime').iterdir()),'restore only into fresh native installation'
pw=pathlib.Path('/run/kane-recovery-password');shutil.copyfile('/private-recovery-input/private-inputs/password',pw);pw.chmod(0o600)
bundle='/private-recovery-input/evidence/encrypted-backup'
base=['kane-system-maintenance','restore','--source',bundle,'--expected-key-sha256',{pin!r},'--confirm','RESTORE-PRESERVING-UNKNOWN','--password-file',str(pw)]
# Real negative paths must refuse before replacing identity or installing data.
wrong=base.copy();wrong[wrong.index('--expected-key-sha256')+1]='0'*64
p=subprocess.run(wrong,capture_output=True,text=True);pathlib.Path('/exchange/wrong-key.log').write_text(p.stdout+p.stderr);assert p.returncode!=0 and not rpc('tasks')
corrupt=pathlib.Path('/var/tmp/kane-corrupt-backup');shutil.copytree(bundle,corrupt)
with (corrupt/'checkpoint.enc').open('ab') as f:f.write(b'CORRUPTED')
wrong=base.copy();wrong[wrong.index('--source')+1]=str(corrupt)
p=subprocess.run(wrong,capture_output=True,text=True);pathlib.Path('/exchange/corrupt-backup.log').write_text(p.stdout+p.stderr);assert p.returncode!=0 and not rpc('tasks')
shutil.rmtree(corrupt)
# Wrong password may stop services, but must never install any recovered files.
bad=pathlib.Path('/run/kane-wrong-password');bad.write_text('explicit-wrong-reference-password\\n');bad.chmod(0o600)
wrong=base.copy();wrong[-1]=str(bad)
p=subprocess.run(wrong,capture_output=True,text=True);pathlib.Path('/exchange/wrong-password.log').write_text(p.stdout+p.stderr);assert p.returncode!=0
start_core();assert not rpc('tasks') and not any(pathlib.Path('/var/lib/kane-runtime').iterdir())
p=subprocess.run(base,capture_output=True,text=True);pathlib.Path('/exchange/restore.log').write_text(p.stdout+p.stderr);print(p.stdout,p.stderr);p.check_returncode()
# The human-facing maintenance CLI emits child progress before its compact final record.
pathlib.Path('/exchange/restore-stdout.log').write_text(p.stdout)
pathlib.Path('/exchange/restore-stderr.log').write_text(p.stderr)
restored=json.loads(p.stdout.strip().splitlines()[-1]);assert restored['tasks_quarantined_by_original_Rust_checkpoint_restore'] and not restored['automatic_runtime_launch']
pathlib.Path('/exchange/restore-result.json').write_text(json.dumps(restored,indent=2))
start_core()
pathlib.Path('/exchange/after-restore.json').write_text(json.dumps({{'tasks':rpc('tasks'),'artifacts':{{t['id']:rpc('artifacts',id=t['id']) for t in rpc('tasks')}},'budgets':{{t['spec']['budget_id']:rpc('model_budget',id=t['spec']['budget_id']) for t in rpc('tasks')}},'agents':{{p.stem:rpc('runtime_status',id=p.stem) for p in pathlib.Path('/etc/kane-system/runtimes').glob('*.json')}},'events':rpc('events'),'recovery_queue':rpc('recovery_queue')}},indent=2))
assert not subprocess.check_output(['systemctl','list-units','--no-legend','--state=active','kane-agent@*.service','kane-model@*.service'],text=True)
privacy=[]
for directory in pathlib.Path('/var/lib/kane-runtime').iterdir():
 if directory.name=='system-identity' or directory.name.startswith('system-identity-previous-'):
  assert directory.stat().st_uid==0 and directory.stat().st_mode&0o777==0o700
  for name in ['shadow','private-marker']:
   f=directory/name
   if f.exists():
    deny=subprocess.run(['runuser','-u','kane-system','--','cat',str(f)],capture_output=True);assert deny.returncode!=0
    privacy.append({{'path':str(f),'root_owned':True,'core_read_denied':True}})
pathlib.Path('/exchange/restored-identity-privacy.json').write_text(json.dumps(privacy,indent=2))
pw.unlink();bad.unlink()
""",timeout=1500)
  after=json.loads((e/'after-restore.json').read_text())
  assert {t['id'] for t in after['tasks']}=={t['id'] for t in before['tasks']}
  assert after['budgets']==before['budgets'] and after['artifacts']==before['artifacts']
  assert after['recovery_queue']==before['recovery_queue'],'restore hid or changed unresolved effects'
  terminal={'Done','Failed','Cancelled','ClosedUnverified'}
  by_id={t['id']:t for t in after['tasks']}
  for old in before['tasks']:
   if old['state'] in terminal:assert by_id[old['id']]==old,'terminal result changed during restore'
   else:assert by_id[old['id']]['state']=='Blocked' and by_id[old['id']]['block_reason']=='restore-review'
  for id,old in before['agents'].items():
   for key in ['id','task_id','workspace_id','budget_id','model_id']:assert after['agents'][id]['agent'][key]==old['agent'][key]
  assert any(x['kind']=='checkpoint_restored' for x in after['events'])
  result.update(status='PASS',scope='fresh native install, original Rust encrypted checkpoint restore, identity/budget/artifact preservation and quarantine; no Task replay; negative key/cipher/password paths')
  vm.send('sudo -n systemctl poweroff');vm.expect(__import__('pexpect').EOF,timeout=180);vm.stop()
  # Verify restored account credentials through real PAM on another normal boot.
  old_credentials=json.loads((a.source_acceptance/'credentials.json').read_text())
  vm=Vm(a.scratch.resolve(),a.installer.resolve(),e,ROOT/'bench/phase6','native-restored-cold-boot',memory_mb=a.memory_mb)
  vm.boot();m.login(vm,old_credentials)
  vm.command("""import pathlib,subprocess,json
pathlib.Path('/exchange').mkdir(exist_ok=True);subprocess.run(['mount','-t','9p','-o','trans=virtio,version=9p2000.L','evidence','/exchange'],check=True)
q=['kanesys','--config','/etc/kane-system/core.env','host-request']
def rpc(op,**kw):return json.loads(subprocess.check_output(q,input=json.dumps({'op':op,**kw}),text=True))
r={'tasks':rpc('tasks'),'artifacts':{t['id']:rpc('artifacts',id=t['id']) for t in rpc('tasks')},'budgets':{t['spec']['budget_id']:rpc('model_budget',id=t['spec']['budget_id']) for t in rpc('tasks')},'events':rpc('events')}
assert not subprocess.check_output(['systemctl','list-units','--no-legend','--state=active','kane-agent@*.service','kane-model@*.service'],text=True)
pathlib.Path('/exchange/restored-cold-boot.json').write_text(json.dumps(r,indent=2))
""",timeout=300)
  cold=json.loads((e/'restored-cold-boot.json').read_text());assert cold['budgets']==before['budgets'] and cold['artifacts']==before['artifacts']
  assert cold['tasks']==after['tasks'],'cold boot changed restored authoritative Task state'
  result['restored_Owner_PAM_and_normal_reboot_verified']=True
  vm.send('sudo -n systemctl poweroff');vm.expect(__import__('pexpect').EOF,timeout=180)
 except Exception:result.update(status='FAIL',failure=traceback.format_exc());raise
 finally:
  (out/'result.json').write_text(json.dumps(result,indent=2)+'\n');vm.stop()
if __name__=='__main__':main()
