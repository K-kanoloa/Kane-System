#!/usr/bin/env python3
"""Read-only Owner-authenticated observation after an interrupted native OS update.
A 2 GiB maintenance VM admits no Agent/model workload. It never changes Task state,
replays a request, refunds usage, removes a stage, or rewrites the boot selector.
"""
import argparse,json,traceback
from pathlib import Path
import pexpect
from validate_native_import import Vm,ROOT
from importlib.util import spec_from_file_location,module_from_spec
s=spec_from_file_location('native_exercise',Path(__file__).with_name('exercise-native-system.py'));m=module_from_spec(s);s.loader.exec_module(m)
ap=argparse.ArgumentParser(description=__doc__)
for n in ['acceptance','installer','scratch','output','before']:ap.add_argument('--'+n,type=Path,required=True)
a=ap.parse_args();out=a.output.resolve();out.mkdir(mode=0o700,parents=True,exist_ok=False);e=out/'evidence';e.mkdir(mode=0o700);creds=json.loads((a.acceptance/'credentials.json').read_text());v=Vm(a.scratch.resolve(),a.installer.resolve(),e,ROOT/'bench/phase6','recovery-observation',memory_mb=2048);result={'status':'IN_PROGRESS','phase6_closed':False,'profile':'2GiB maintenance, no Agent/model workload admitted'}
try:
 v.boot();m.login(v,creds)
 v.command("""import pathlib,subprocess,json
pathlib.Path('/exchange').mkdir(exist_ok=True);subprocess.run(['mount','-t','9p','-o','trans=virtio,version=9p2000.L','evidence','/exchange'],check=True)
q=['kanesys','--config','/etc/kane-system/core.env','host-request']
def rpc(op,**kw):return json.loads(subprocess.check_output(q,input=json.dumps({'op':op,**kw}),text=True))
subprocess.run(['openssl','pkeyutl','-verify','-pubin','-inkey','/usr/share/kane-system/trust/release.pem','-rawin','-in','/usr/share/kane-system/installed-manifest.json','-sigfile','/usr/share/kane-system/installed-manifest.sig'],check=True)
active=subprocess.check_output(['findmnt','-n','-o','FSROOT','/'],text=True).strip();boot=subprocess.check_output(['grub-editenv','/kane-boot/grub/grubenv','list'],text=True);meta=json.loads(pathlib.Path('/usr/share/kane-system/installed-manifest.json').read_text());j=json.loads(pathlib.Path('/.kane-data/os-update.json').read_text())
assert active in ['/@slots/A','/@slots/B'];assert 'saved_entry='+('0' if active.endswith('A') else '1') in boot
assert not subprocess.check_output(['systemctl','list-units','--no-legend','--state=active','kane-agent@*.service','kane-model@*.service'],text=True)
tasks=rpc('tasks');agents=[rpc('runtime_status',id=p.stem)['agent'] for p in sorted(pathlib.Path('/etc/kane-system/runtimes').glob('*.json'))]
budgets={t['spec']['budget_id']:rpc('model_budget',id=t['spec']['budget_id']) for t in tasks}
r={'manifest':meta,'active_root':active,'boot_env':boot,'update_journal':j,'tasks':tasks,'agents':agents,'budgets':budgets,'artifact_sets':{t['id']:rpc('artifacts',id=t['id']) for t in tasks},'machine_id':pathlib.Path('/etc/machine-id').read_text(),'health':rpc('health'),'model_instances':rpc('model_instances'),'model_queue':rpc('model_queue')}
pathlib.Path('/exchange/recovered.json').write_text(json.dumps(r,indent=2));pathlib.Path('/exchange/recovery-journal.log').write_text(subprocess.check_output(['journalctl','-b','--no-pager'],text=True))
""",timeout=240)
 old=json.loads(a.before.read_text());new=json.loads((e/'recovered.json').read_text())
 assert new['tasks']==old['tasks'] and new['budgets']==old['budgets'] and new['artifact_sets']==old['artifact_sets'] and new['machine_id']==old['machine_id']
 fields=['id','task_id','workspace_id','budget_id','model_id'];assert [{k:x[k] for k in fields} for x in new['agents']]==[{k:x[k] for k in fields} for x in old['agents']]
 result.update(status='PASS',scope='actual interrupted-OS-update boot and registered data/identity preserved; NOT an in-flight model/Task recovery claim',active_root=new['active_root'],installed_sequence=new['manifest']['sequence'])
 v.send('sudo -n systemctl poweroff');v.expect(pexpect.EOF,timeout=240)
except Exception:result.update(status='FAIL',failure=traceback.format_exc());raise
finally:
 (out/'result.json').write_text(json.dumps(result,indent=2)+'\n');v.stop()
