#!/usr/bin/env python3
"""Actual installed-OS power loss with an original Kanaloa request in flight.
Only local disposable QEMU, real Owner login and existing Core/Adapter contracts.
No direct database writes, fake provider receipts, automatic retries or refunds.
"""
import argparse,json,time,traceback
from pathlib import Path
from native_preflight import require_native_storage
from validate_native_import import Vm,ROOT
from importlib.util import spec_from_file_location,module_from_spec
s=spec_from_file_location('native_exercise',Path(__file__).with_name('exercise-native-system.py'));m=module_from_spec(s);s.loader.exec_module(m)

def main():
 ap=argparse.ArgumentParser(description=__doc__)
 for n in ['acceptance','installer','scratch','assets','output']:ap.add_argument('--'+n,type=Path,required=True)
 ap.add_argument('--memory-mb',type=int,choices=[2048,3072],default=3072)
 ap.add_argument('--cpu-model',choices=['max','Nehalem'],default='Nehalem')
 ap.add_argument('--name',default='nativefault')
 ap.add_argument('--continue-cut',type=Path,help='Continue only context recovery of an explicitly retained actual cut; no new injection or prompt, prior failure remains unchanged')
 ap.add_argument('--reuse-public-weights',type=Path,help='Explicit existing immutable public GGUF; default copies the pinned fixture input')
 a=ap.parse_args();require_native_storage(output=a.output, scratch=a.scratch, workspace=ROOT);assert all((ROOT/'bench/phase6'/f).stat().st_mode&0o004 for f in ['native-reference-workflow.py','native-resume-session.py']),'Public original-Adapter caller must be readable by dedicated Core UID; refuse before VM mutation';out=a.output.resolve();out.mkdir(mode=0o700,parents=True,exist_ok=False);e=out/'evidence';e.mkdir(mode=0o700)
 import re
 assert re.fullmatch('[a-z][a-z0-9-]{0,39}',a.name)
 job_path='/etc/kane-system/reference-'+a.name+'.json'
 credentials=json.loads((a.acceptance/'credentials.json').read_text());vm=None;result={'status':'IN_PROGRESS','phase6_closed':False,'guest_memory_mb':a.memory_mb,'scope':'Real local CPU backend, original Kanaloa, abrupt QEMU SIGKILL and new native boot'}
 def command(script,**kw):
  script=script.replace('/etc/kane-system/reference-nativefault.json',job_path).replace("'--name','nativefault'","'--name',"+repr(a.name))
  pair="'--reuse-public-weights','/var/lib/kane-models/native021deadline.gguf',"
  script=script.replace(pair,("'--reuse-public-weights',"+repr(str(a.reuse_public_weights))+",") if a.reuse_public_weights else '')
  return vm.command(script,**kw)
 def boot(label):
  v=Vm(a.scratch.resolve(),a.installer.resolve(),e,ROOT/'bench/phase6',label,a.assets.resolve(),memory_mb=a.memory_mb,cpu_model=a.cpu_model);v.boot();m.login(v,credentials)
  v.command("import pathlib,subprocess\nfor tag,path in [('evidence','/exchange'),('assets','/assets'),('fixtures','/fixtures')]:\n pathlib.Path(path).mkdir(exist_ok=True);subprocess.run(['mount','-t','9p','-o','trans=virtio,version=9p2000.L'+(',ro' if tag!='evidence' else ''),tag,path],check=True)")
  return v
 try:
  if a.continue_cut:
   import shutil,hashlib
   origin=a.continue_cut.resolve();meta=json.loads((origin/'actual-power-cut.json').read_text());assert meta['injected'] and meta['method'].startswith('SIGKILL')
   originals={}
   for name in ['before-cut.json','completed-before.json','actual-power-cut.json']:
    body=(origin/name).read_bytes();(e/name).write_bytes(body);originals[name]=hashlib.sha256(body).hexdigest()
   before=json.loads((e/'before-cut.json').read_text());prior=json.loads((e/'completed-before.json').read_text());result['prior_done_tasks']=sum(x['state']=='Done' for x in prior['tasks'])
   assert before['job']['agent']==json.loads((origin/'job.json').read_text())['agent']
   result.update(new_cut_injected=False,actual_cut_origin=str(origin),origin_file_sha256=originals,previous_failed_attempt_unchanged=True,scope='Explicit continuation of the SAME actual retained cut; not a newly green end-to-end injection run')
   cut=time.time()
  else:
   vm=boot('01-before-powercut')
   command("""import pathlib,subprocess,json,time
 q=['kanesys','--config','/etc/kane-system/core.env','host-request']
 def rpc(op,**kw):return json.loads(subprocess.check_output(q,input=json.dumps({'op':op,**kw}),text=True))
 units=subprocess.check_output(['systemctl','list-units','--no-legend','--state=active','kane-agent@*.service','kane-model@*.service'],text=True)
 for line in units.splitlines():subprocess.run(['systemctl','stop',line.split()[0]],check=True)
 subprocess.run(['systemctl','start','kane-docker.service'],check=True)
 for _docker_probe in range(120):
  _docker_ready=subprocess.run(['/usr/local/bin/docker','info','--format','{{.ServerVersion}}'],capture_output=True,text=True)
  if _docker_ready.returncode==0:break
  time.sleep(.5)
 else:raise RuntimeError('Container backend unavailable: physical absence is NOT proven; refuse reconcile')

 # On a new kernel, old physical resource claims need the ORIGINAL absence
 # proof. This does NOT settle/erase any unresolved model or tool usage.
 held=rpc('recovery_queue');budgets={t['spec']['budget_id']:rpc('model_budget',id=t['spec']['budget_id']) for t in rpc('tasks')}
 for claim in rpc('host_resources')['claims']:
  if claim['held']:subprocess.run(['kanesys','--config','/etc/kane-system/core.env','reconcile-runtime','--agent',claim['agent_id']],check=True)
 for row in rpc('model_instances'):
  if row.get('held'):subprocess.run(['kanesys','--config','/etc/kane-system/core.env','model-reconcile',row['id']],check=True)
 assert rpc('recovery_queue')==held and all(rpc('model_budget',id=k)==v for k,v in budgets.items())
 pathlib.Path('/exchange/prior-unknown-preserved.json').write_text(json.dumps({'recovery_queue':held,'budgets':budgets,'automatic_replays':0,'automatic_refunds':0},indent=2))

 subprocess.run(['systemctl','start','kane-docker.service'],check=True)
 subprocess.run(['/usr/local/bin/docker','load','--input','/var/tmp/kane-reference-image.tar'],check=True)
 config=pathlib.Path('/etc/kane-system/reference-nativefault.json')
 if config.exists():
  prior=json.loads(config.read_text());budget=rpc('model_budget',id=prior['budget'])
  assert rpc('task',id=prior['task'])['state'] in ['Pending','Ready']
  assert budget['active_calls']==0 and budget['reserved_tokens']==0 and budget['used_tokens']==0
  assert not any(x['task_id']==prior['task'] for x in rpc('model_queue')) and not rpc('artifacts',id=prior['task'])
  assert not any(x['kind']=='prompt_dispatch' and x['object_id']==prior['task'] for x in rpc('events'))
  pathlib.Path('/exchange/unstarted-job-reuse-proof.json').write_text(json.dumps({'task':prior['task'],'same_bindings':True,'no_prior_prompt_queue_artifact_or_usage':True}))
 else:
  subprocess.run(['python3','/fixtures/native-reference-workflow.py','configure','--name','nativefault','--mode','hello','--public-simple-task','--public-echo-prompt','--retain-host-resource-policy','--expected-output','HELLO','--model-cpu-millis','1300','--home-capacity-mib','8','--reuse-public-weights','/var/lib/kane-models/native021deadline.gguf','--weights','/assets/SmolLM2-135M-Instruct-Q4_K_M.gguf','--input','/assets/input.txt'],check=True)
 i=json.loads(pathlib.Path('/etc/kane-system/reference-nativefault.json').read_text())
 subprocess.run(['systemctl','start','kane-model@'+i['instance']+'.service'],check=True)
 for _ in range(240):
  row=next(x for x in rpc('model_instances') if x['id']==i['instance'])
  if row['state']=='Ready':break
  if subprocess.run(['systemctl','is-failed','--quiet','kane-model@'+i['instance']+'.service']).returncode==0:raise RuntimeError('Model failed; no power-cut fixture admitted')
  time.sleep(.5)
 else:raise RuntimeError('Model never ready')
 subprocess.run(['systemctl','start','kane-model-gateway.service'],check=True)
 subprocess.run(['python3','/fixtures/prepare-agent-gateway.py',i['agent']],check=True)
 spec=json.loads((pathlib.Path('/etc/kane-system/runtimes')/(i['agent']+'.json')).read_text())
 subprocess.run(['/usr/local/bin/docker','image','inspect',spec['sandbox']['image']],check=True,stdout=subprocess.DEVNULL,timeout=60)
 subprocess.run(['systemctl','start','kane-agent@'+i['agent']+'.service'],check=True)
 for _ in range(360):
  state=rpc('runtime_status',id=i['agent'])
  if state['agent']['state']=='Running' and pathlib.Path('/run/kane-system',i['agent']+'.runtime.sock').is_socket():break
  if subprocess.run(['systemctl','is-failed','--quiet','kane-agent@'+i['agent']+'.service']).returncode==0:raise RuntimeError('Supervisor not ready; no prompt/resume admitted')
  time.sleep(.5)
 else:raise RuntimeError('Supervisor/control readiness deadline')
 pathlib.Path('/exchange/job.json').write_text(json.dumps(i,indent=2))
 pathlib.Path('/exchange/completed-before.json').write_text(json.dumps({'tasks':rpc('tasks'),'artifacts':{t['id']:rpc('artifacts',id=t['id']) for t in rpc('tasks')}}))
 """,timeout=600)
   command("""import pathlib,subprocess,json,os,signal,time
 q=['kanesys','--config','/etc/kane-system/core.env','host-request']
 def rpc(op,**kw):return json.loads(subprocess.check_output(q,input=json.dumps({'op':op,**kw}),text=True))
 i=json.loads(pathlib.Path('/etc/kane-system/reference-nativefault.json').read_text());row=next(x for x in rpc('model_instances') if x['id']==i['instance'])
 assert row['state']=='Ready' and row['pid']>1 and b'llama-server' in pathlib.Path('/proc',str(row['pid']),'cmdline').read_bytes()
 log=open('/exchange/fault-execute.log','xb',buffering=0)
 p=subprocess.Popen(['runuser','-u','kane-system','--','python3','/fixtures/native-reference-workflow.py','execute','/etc/kane-system/reference-nativefault.json','--retry-uninitialized','--acp-initialize-timeout-seconds','600'],stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT)
 # Observe at low priority without flooding Core CLI during original ACP cold initialization.
 os.nice(10)
 _observe_deadline=time.monotonic()+750
 while time.monotonic()<_observe_deadline:
  rows=[x for x in rpc('model_queue') if x['task_id']==i['task']]
  if rows and rows[0]['state']=='Dispatched':
   # Initialize the original ACP/Session first. Pausing before initialization
   # would merely test a health-check timeout instead of an in-flight request.
   os.kill(row['pid'],signal.SIGSTOP)
   manager=int(subprocess.check_output(['systemctl','show','--value','--property=MainPID','kane-model@'+i['instance']+'.service'],text=True));assert manager>1 and manager!=row['pid']
   os.kill(manager,signal.SIGSTOP)
   assert next(x for x in rpc('model_queue') if x['call_id']==rows[0]['call_id'])['state']=='Dispatched','Completion raced fault injection; no Unknown claim'
   break
  if p.poll() is not None:
   os.fsync(log.fileno())
   pathlib.Path('/exchange/pre-dispatch-exit.json').write_text(json.dumps({'returncode':p.returncode,'task':rpc('task',id=i['task']),'budget':rpc('model_budget',id=i['budget']),'agent':rpc('runtime_status',id=i['agent']),'queue':rpc('model_queue'),'events':rpc('events')},indent=2))
   pathlib.Path('/exchange/pre-dispatch-journal.log').write_text(subprocess.check_output(['journalctl','-b','--no-pager'],text=True))
   raise RuntimeError('Original Kanaloa stopped before a real in-flight request; see actual exit log; no cut injected')
  time.sleep(.5)
 else:raise RuntimeError('No genuine in-flight model request; no power-cut PASS')
 budget=rpc('model_budget',id=i['budget']);assert budget['reserved_tokens']>0 and budget['used_tokens']==0
 before={'job':i,'boot_id':pathlib.Path('/proc/sys/kernel/random/boot_id').read_text(),'machine_id':pathlib.Path('/etc/machine-id').read_text(),'binding':rpc('runtime_status',id=i['agent'])['agent'],'queue':rows[0],'budget':budget,'model':row,'events':rpc('events'),'fault_injection':{'backend_sigstop_after_dispatch':row['pid'],'manager_sigstop':manager,'reason':'Keep a genuinely dispatched request unresolved until the actual whole-kernel cut; no fabricated provider usage'}}
 f=pathlib.Path('/exchange/before-cut.json').open('x');json.dump(before,f,indent=2);f.flush();os.fsync(f.fileno());f.close()
 """,timeout=900)
   before=json.loads((e/'before-cut.json').read_text())
   prior=json.loads((e/'completed-before.json').read_text());result['prior_done_tasks']=sum(x['state']=='Done' for x in prior['tasks'])
   cut=time.time();vm.stop(powercut=True)
   (e/'actual-power-cut.json').write_text(json.dumps({'injected':True,'method':'SIGKILL local QEMU container; live native kernel, Core, Supervisor, Agent, actual model','host_time':cut},indent=2)+'\n')
  vm=boot('02-after-powercut')
  command("""import pathlib,subprocess,json
q=['kanesys','--config','/etc/kane-system/core.env','host-request']
def rpc(op,**kw):return json.loads(subprocess.check_output(q,input=json.dumps({'op':op,**kw}),text=True))
b=json.loads(pathlib.Path('/exchange/before-cut.json').read_text());i=b['job'];queue=next(x for x in rpc('model_queue') if x['call_id']==b['queue']['call_id']);budget=rpc('model_budget',id=i['budget']);task=rpc('task',id=i['task']);binding=rpc('runtime_status',id=i['agent'])['agent']
assert pathlib.Path('/proc/sys/kernel/random/boot_id').read_text()!=b['boot_id']
assert pathlib.Path('/etc/machine-id').read_text()==b['machine_id']
assert queue['state']=='Unknown' and task['state']=='Blocked'
assert budget['reserved_tokens']==b['budget']['reserved_tokens'] and budget['used_tokens']==0
for name in ['id','task_id','workspace_id','budget_id','model_id']:assert binding[name]==b['binding'][name]
prior=json.loads(pathlib.Path('/exchange/completed-before.json').read_text())
for old in prior['tasks']:
 if old['state']=='Done':assert rpc('task',id=old['id'])==old and rpc('artifacts',id=old['id'])==prior['artifacts'][old['id']]
events=rpc('events');prompts=[x for x in events if x['kind']=='prompt_dispatch' and x['object_id']==i['task']];assert len(prompts)==1
assert not subprocess.check_output(['systemctl','list-units','--no-legend','--state=active','kane-agent@*.service','kane-model@*.service'],text=True)
pathlib.Path('/exchange/recovered.json').write_text(json.dumps({'task':task,'queue':queue,'budget':budget,'binding':binding,'model_instances':rpc('model_instances'),'host_resources':rpc('host_resources'),'events':events,'prompt_dispatches':len(prompts),'automatic_prompt_replays':0,'automatic_refunds':0},indent=2))
pathlib.Path('/exchange/recovery-journal.log').write_text(subprocess.check_output(['journalctl','-b','--no-pager'],text=True))
""",timeout=240)
  command("""import pathlib,subprocess,json,time
q=['kanesys','--config','/etc/kane-system/core.env','host-request']
def rpc(op,**kw):return json.loads(subprocess.check_output(q,input=json.dumps({'op':op,**kw}),text=True))
b=json.loads(pathlib.Path('/exchange/before-cut.json').read_text());i=b['job'];budget=rpc('model_budget',id=i['budget'])
subprocess.run(['systemctl','start','kane-docker.service'],check=True)
for _docker_probe in range(120):
 _docker_ready=subprocess.run(['/usr/local/bin/docker','info','--format','{{.ServerVersion}}'],capture_output=True,text=True)
 if _docker_ready.returncode==0:break
 time.sleep(.5)
else:raise RuntimeError('Container backend unavailable: physical absence is NOT proven; refuse reconcile')
subprocess.run(['kanesys','--config','/etc/kane-system/core.env','reconcile-runtime','--agent',i['agent']],check=True)
physical=next(x for x in rpc('model_instances') if x['id']==i['instance'])
if physical.get('held'):
 subprocess.run(['kanesys','--config','/etc/kane-system/core.env','model-reconcile',i['instance']],check=True)
else:
 assert physical['state']=='Unloaded','No held claim is NOT permission to ignore an inconsistent live instance'
 pathlib.Path('/exchange/already-reconciled-physical-instance.json').write_text(json.dumps({'instance':physical,'source':'prior original Owner stop/absence reconciliation already released physical resources','unknown_budget_preserved':rpc('model_budget',id=i['budget'])==budget},indent=2))
assert rpc('model_budget',id=i['budget'])==budget
subprocess.run(['systemctl','start','kane-model@'+i['instance']+'.service'],check=True)
for _ in range(240):
 row=next(x for x in rpc('model_instances') if x['id']==i['instance'])
 if row['state']=='Ready':break
 if subprocess.run(['systemctl','is-failed','--quiet','kane-model@'+i['instance']+'.service']).returncode==0:raise RuntimeError('Reload failed; never reset Unknown to manufacture recovery')
 time.sleep(.5)
else:raise RuntimeError('Reload did not become ready')
assert row['generation']!=b['model']['generation']
assert next(x for x in rpc('model_queue') if x['call_id']==b['queue']['call_id'])['state']=='Unknown'
subprocess.run(['systemctl','start','kane-model-gateway.service'],check=True)
subprocess.run(['python3','/fixtures/prepare-agent-gateway.py',i['agent']],check=True)
spec=json.loads((pathlib.Path('/etc/kane-system/runtimes')/(i['agent']+'.json')).read_text())
subprocess.run(['/usr/local/bin/docker','image','inspect',spec['sandbox']['image']],check=True,stdout=subprocess.DEVNULL,timeout=60)
subprocess.run(['systemctl','start','kane-agent@'+i['agent']+'.service'],check=True)
for _ in range(360):
 state=rpc('runtime_status',id=i['agent'])
 if state['agent']['state']=='Running' and pathlib.Path('/run/kane-system',i['agent']+'.runtime.sock').is_socket():break
 if subprocess.run(['systemctl','is-failed','--quiet','kane-agent@'+i['agent']+'.service']).returncode==0:raise RuntimeError('Supervisor not ready; no prompt/resume admitted')
 time.sleep(.5)
else:raise RuntimeError('Supervisor/control readiness deadline')
p=subprocess.run(['runuser','-u','kane-system','--','python3','/fixtures/native-resume-session.py','/etc/kane-system/reference-nativefault.json'],capture_output=True,text=True)
pathlib.Path('/exchange/native-resume.log').write_text(p.stdout+p.stderr);print(p.stdout,p.stderr);p.check_returncode()
assert rpc('model_budget',id=i['budget'])==budget and rpc('task',id=i['task'])['state']=='Blocked'
pathlib.Path('/exchange/session-recovered.json').write_bytes((pathlib.Path(i['root'])/'native-session-recovered.json').read_bytes())
pathlib.Path('/exchange/model-reloaded.json').write_text(json.dumps(row,indent=2))
for unit in ['kane-agent@'+i['agent']+'.service','kane-model@'+i['instance']+'.service']:subprocess.run(['systemctl','stop',unit],check=True)
""",timeout=1000)
  result.update(status='PASS',recovery_wall_seconds=time.time()-cut,scope='In-flight original Kanaloa request retained Unknown after abrupt native-kernel loss; binding, budget, route freeze and no auto-replay; prior completed Artifact preservation only when prior_done_tasks > 0; NOT physical power-loss or SLA proof')
  if a.continue_cut:
   result['scope']='Same retained actual cut: original context recovery after public helper permission repair; prior cut/failure linked, zero new prompt or refund; not a single fresh injection run'
   result['seconds_since_original_actual_cut']=time.time()-meta['host_time']
  vm.send('sudo -n systemctl poweroff');vm.expect(__import__('pexpect').EOF,timeout=180)
 except Exception:
  result.update(status='FAIL',failure=traceback.format_exc())
  try:
   command("import pathlib,subprocess;pathlib.Path('/exchange/failure-journal.log').write_text(subprocess.check_output(['journalctl','-b','--no-pager'],text=True));p=pathlib.Path('/var/log/syslog');pathlib.Path('/exchange/failure-syslog.log').write_bytes(p.read_bytes()[-2000000:] if p.exists() else b'no rsyslog file')",timeout=180)
  except Exception:pass
  raise
 finally:
  (out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
  if vm:vm.stop()
if __name__=='__main__':main()
