#!/usr/bin/env python3
"""Owner-authenticated observation of one existing native test job. No prompt/retry.
Only local authorized synthetic reference; no private identity/credential export.
"""
import argparse,json,traceback
from pathlib import Path
from validate_native_import import Vm,ROOT
from importlib.util import spec_from_file_location,module_from_spec
s=spec_from_file_location('native_exercise',Path(__file__).with_name('exercise-native-system.py'));m=module_from_spec(s);s.loader.exec_module(m)
p=argparse.ArgumentParser(description=__doc__)
for n in ['acceptance','installer','scratch','output']:p.add_argument('--'+n,type=Path,required=True)
p.add_argument('--job',default='/etc/kane-system/reference-nativefault.json');a=p.parse_args();o=a.output.resolve();o.mkdir(mode=0o700,parents=True,exist_ok=False);e=o/'evidence';e.mkdir(mode=0o700)
v=Vm(a.scratch.resolve(),a.installer.resolve(),e,ROOT/'bench/phase6','inspect-native-job',memory_mb=2048);r={'status':'IN_PROGRESS','scope':'Observation only, no Agent/Model start or prompt, not recovery acceptance'}
try:
 v.boot();m.login(v,json.loads((a.acceptance/'credentials.json').read_text()))
 v.command(f"""import pathlib,subprocess,json
pathlib.Path('/exchange').mkdir(exist_ok=True);subprocess.run(['mount','-t','9p','-o','trans=virtio,version=9p2000.L','evidence','/exchange'],check=True)
i=json.loads(pathlib.Path({a.job!r}).read_text());root=pathlib.Path(i['root']);q=['kanesys','--config','/etc/kane-system/core.env','host-request']
def rpc(op,**kw):return json.loads(subprocess.check_output(q,input=json.dumps({{'op':op,**kw}}),text=True))
facts={{'job':i,'task':rpc('task',id=i['task']),'budget':rpc('model_budget',id=i['budget']),'agent':rpc('runtime_status',id=i['agent']),'queue':rpc('model_queue'),'events':rpc('events'),'recovery':rpc('recovery_queue'),'models':rpc('model_instances'),'resources':rpc('host_resources')}}
pathlib.Path('/exchange/observed.json').write_text(json.dumps(facts,indent=2))
for n in ['powercut-execute.log','execution-failure.json','execution-evidence.json','acp-initialized.json']:
 f=root/n
 if f.is_file():pathlib.Path('/exchange',n).write_bytes(f.read_bytes())
# Prior boot's real kernel/unit diagnostics are kept by the installed journal.
p=subprocess.run(['journalctl','-b','-1','--no-pager'],capture_output=True,text=True);pathlib.Path('/exchange/prior-boot-journal.log').write_text(p.stdout+p.stderr)
""",timeout=240)
 r['status']='PASS';v.send('sudo -n systemctl poweroff');v.expect(__import__('pexpect').EOF,timeout=180)
except Exception:r.update(status='FAIL',failure=traceback.format_exc());raise
finally:
 (o/'result.json').write_text(json.dumps(r,indent=2)+'\n');v.stop()
