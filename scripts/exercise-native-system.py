#!/usr/bin/env python3
"""Run the original Kanaloa/model chain after an actual Owner-authenticated OS boot.
Existing installed target only; no cloud-init, remote host or automatic Unknown replay.
"""
import argparse,json,shlex,traceback
from pathlib import Path
from native_preflight import require_native_storage
from validate_native_import import Vm,ROOT

def login(vm,credentials):
 vm.expect('login:');vm.send(credentials['username']);vm.expect('Password:');vm.send(credentials['password']);vm.expect(r'\$ ')
 vm.send('sudo -v');vm.expect('password for');vm.send(credentials['password']);vm.expect(r'\$ ')

def main():
 ap=argparse.ArgumentParser(description=__doc__)
 for k in ['acceptance','installer','scratch','assets','output']:ap.add_argument('--'+k,type=Path,required=True)
 ap.add_argument('--public-simple-task',action='store_true');ap.add_argument('--expected-output',choices=['HELLO','KANE'],default='HELLO');ap.add_argument('--reconcile-absent-resources',action='store_true')
 ap.add_argument('--acp-initialize-timeout-seconds',type=int,choices=[180,600],default=180)
 ap.add_argument('--model-request-timeout-seconds',type=int,choices=[45,180],help='Explicit bounded Owner deadline for a new Task; default stays 45s')
 ap.add_argument('--home-capacity-mib',type=int,choices=[8,64],default=64)
 ap.add_argument('--context-tokens',type=int,choices=[4096,8192,16384])
 ap.add_argument('--cpu-model',choices=['max','qemu64','Nehalem'],default='max')
 ap.add_argument('--reuse-public-weights',type=Path)
 ap.add_argument('--retain-host-resource-policy',action='store_true')
 ap.add_argument('--backend-warmup',action='store_true')
 ap.add_argument('--public-echo-prompt',action='store_true')
 ap.add_argument('--memory-mb',type=int,choices=[2048,3072],default=3072);ap.add_argument('--existing-job',type=Path,help='Explicitly reuse a previously configured, never-dispatched job; no implicit replay');ap.add_argument('--name',default='nativehello');ap.add_argument('--mode',choices=['read','hello'],default='hello');ap.add_argument('--weights',default='SmolLM2-135M-Instruct-Q4_K_M.gguf')
 ap.add_argument('--reclaim-obsolete-os-roots',action='store_true',help='Explicit Owner maintenance of this disposable reference only: verify obsolete roots, keep current A/B and all data')
 ap.add_argument('--retry-uninitialized',action='store_true',help='Only a proved never-dispatched/no-native-session turn may retry original ACP initialization; never replay Unknown')
 ap.add_argument('--model-cpu-millis',type=int,choices=[1000,1300],help='Explicit CPU reference-profile adjustment; fixed Host Pool remains 2000m with 200m reserve and 500m Agent')
 ap.add_argument('--install-console-service',action='store_true',help='Authenticated Owner installs the dedicated reference console for NEXT normal boot; same agetty/PAM, no auto-login')
 ap.add_argument('--model-load-timeout-seconds',type=int,choices=[45,180],help='Explicit bounded Owner loading budget;180 requires the corrected signed reference sequence10 or newer')
 a=ap.parse_args();require_native_storage(output=a.output, scratch=a.scratch, workspace=ROOT);assert not (a.existing_job and a.model_request_timeout_seconds is not None),'Never alter unsettled calls';out=a.output.resolve();out.mkdir(mode=0o700,parents=True,exist_ok=False);evidence=out/'evidence';evidence.mkdir(mode=0o700)
 creds=json.loads((a.acceptance/'credentials.json').read_text());vm=Vm(a.scratch.resolve(),a.installer.resolve(),evidence,ROOT/'bench/phase6','native-model',a.assets.resolve(),memory_mb=a.memory_mb,cpu_model=a.cpu_model);result={'status':'IN_PROGRESS','model_kind':'real local CPU model','guest_memory_mb':a.memory_mb,'phase6_closed':False}
 try:
  vm.boot();login(vm,creds)
  if a.install_console_service:
   unit=(ROOT/'deploy/native/kane-console.service').read_text()
   vm.command(f"""import pathlib,subprocess,hashlib,json
p=pathlib.Path('/etc/systemd/system/kane-console.service');content={unit!r};p.write_text(content);p.chmod(0o644)
subprocess.run(['systemd-analyze','verify',str(p)],check=True)
subprocess.run(['systemctl','daemon-reload'],check=True)
subprocess.run(['systemctl','enable','kane-console.service'],check=True)
# Do not stop the current authenticated login or start a second tty reader.
subprocess.run(['systemctl','mask','serial-getty@ttyS0.service'],check=True)
""",timeout=180)
  setup="""import pathlib,subprocess,json
for tag in ['fixtures','assets','evidence']:
 path=pathlib.Path('/'+tag if tag!='evidence' else '/exchange');path.mkdir(exist_ok=True)
 subprocess.run(['mount','-t','9p','-o','trans=virtio,version=9p2000.L'+(',ro' if tag!='evidence' else ''),tag,str(path)],check=True)
subprocess.run(['systemctl','start','kane-docker.service'],check=True)
# Type=exec alone does not mean dockerd has published its socket yet. Probe the
# actual daemon, keeping every isolation boundary enabled and never falling back.
import time
for _ in range(120):
 p=subprocess.run(['/usr/local/bin/docker','info','--format','{{.ServerVersion}}'],capture_output=True,text=True)
 if p.returncode==0:break
 time.sleep(.5)
else:
 pathlib.Path('/exchange/docker-startup.log').write_text(subprocess.check_output(['journalctl','-u','kane-docker.service','--no-pager'],text=True))
 raise RuntimeError('Container backend never became healthy; no Agent permitted')
subprocess.run(['/usr/local/bin/docker','load','--input','/var/tmp/kane-reference-image.tar'],check=True)
"""
  if a.reclaim_obsolete_os_roots:
   marker="subprocess.run(['systemctl','start','kane-docker.service'],check=True)"
   setup=setup.replace(marker,"subprocess.run(['python3','/fixtures/reclaim-reference-os-roots.py'],check=True)\n"+marker)
  if a.reconcile_absent_resources:
   setup+='''
q=['kanesys','--config','/etc/kane-system/core.env','host-request']
def rpc(op,**kw):return json.loads(subprocess.check_output(q,input=json.dumps({'op':op,**kw}),text=True))
assert not subprocess.check_output(['systemctl','list-units','--no-legend','--state=active','kane-agent@*.service','kane-model@*.service'],text=True)
before=rpc('recovery_queue');budgets={t['spec']['budget_id']:rpc('model_budget',id=t['spec']['budget_id']) for t in rpc('tasks')}
for claim in rpc('host_resources')['claims']:
 if claim['held']:subprocess.run(['kanesys','--config','/etc/kane-system/core.env','reconcile-runtime','--agent',claim['agent_id']],check=True)
for row in rpc('model_instances'):
 if row.get('held'):subprocess.run(['kanesys','--config','/etc/kane-system/core.env','model-reconcile',row['id']],check=True)
assert rpc('recovery_queue')==before
assert all(rpc('model_budget',id=id)==value for id,value in budgets.items())
pathlib.Path('/exchange/physical-cleanup-preserved-unknown.json').write_text(json.dumps({'recovery_queue':before,'budgets':budgets,'automatic_refund':False,'automatic_prompt_replay':False},indent=2))
'''
  vm.command(setup,timeout=600)
  if a.install_console_service:
   vm.command("import pathlib,hashlib,json;p=pathlib.Path('/etc/systemd/system/kane-console.service');pathlib.Path('/exchange/next-boot-console.json').write_text(json.dumps({'unit_sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'authentication':'unchanged original Owner + agetty/PAM','next_boot_only':True},indent=2))")
  config=str(a.existing_job) if a.existing_job else '/etc/kane-system/reference-'+a.name+'.json'
  code=f"""import subprocess,pathlib,json
p=subprocess.run(['python3','/fixtures/native-reference-workflow.py','configure','--name',{a.name!r},'--mode',{a.mode!r},'--weights',{'/assets/'+a.weights!r},'--input','/assets/input.txt','--expected-output',{a.expected_output!r},'--home-capacity-mib',{str(a.home_capacity_mib)!r}]+{((['--retain-host-resource-policy'] if a.retain_host_resource_policy else [])+(['--context-tokens',str(a.context_tokens)] if a.context_tokens is not None else [])+(['--reuse-public-weights',str(a.reuse_public_weights)] if a.reuse_public_weights is not None else [])+(['--public-simple-task'] if a.public_simple_task else [])+(['--public-echo-prompt'] if a.public_echo_prompt else [])+(['--backend-warmup'] if a.backend_warmup else [])+(['--model-request-timeout-seconds',str(a.model_request_timeout_seconds)] if a.model_request_timeout_seconds is not None else []))!r},capture_output=True,text=True)
pathlib.Path('/exchange/configure.log').write_text(p.stdout+p.stderr);print(p.stdout,p.stderr);p.check_returncode()
i=json.loads(pathlib.Path({config!r}).read_text())
q=['kanesys','--config','/etc/kane-system/core.env','host-request']
rows=json.loads(subprocess.check_output(q,input=json.dumps({{'op':'model_instances'}}),text=True));previous=next(x for x in rows if x['id']==i['instance'])
if previous.get('held') and previous['state']=='Failed':
 # Explicit authenticated Owner cleanup proves old physical residency absent.
 # The original Rust reconciler never settles usage or unfreezes Unknown calls.
 p=subprocess.run(['kanesys','--config','/etc/kane-system/core.env','model-reconcile',i['instance']],capture_output=True,text=True)
 pathlib.Path('/exchange/model-absence-reconciliation.log').write_text(p.stdout+p.stderr);p.check_returncode()
subprocess.run(['systemctl','start','kane-model@'+i['instance']+'.service'],check=True)
import time
for _ in range(240):
 instances=json.loads(subprocess.check_output(q,input=json.dumps({{'op':'model_instances'}}),text=True));instance=next(x for x in instances if x['id']==i['instance'])
 if instance['state']=='Ready':break
 if subprocess.run(['systemctl','is-failed','--quiet','kane-model@'+i['instance']+'.service']).returncode==0:raise RuntimeError('Real ModelInstance unit failed before readiness; no Agent/model call admitted')
 time.sleep(.5)
else:raise RuntimeError('Real ModelInstance did not pass runtime/health readiness')
subprocess.run(['systemctl','start','kane-model-gateway.service'],check=True)
subprocess.run(['python3','/fixtures/prepare-agent-gateway.py',i['agent']],check=True)
# A Type=exec start is not Supervisor/ACP readiness. Warm the SAME pinned
# local image and then require authoritative registration + control socket.
spec=json.loads((pathlib.Path('/etc/kane-system/runtimes')/(i['agent']+'.json')).read_text())
probe=subprocess.run(['/usr/local/bin/docker','image','inspect',spec['sandbox']['image']],capture_output=True,text=True,timeout=60)
pathlib.Path('/exchange/pre-start-image-inspect.log').write_text(probe.stdout+probe.stderr);probe.check_returncode()
subprocess.run(['systemctl','start','kane-agent@'+i['agent']+'.service'],check=True)
for _ in range(360):
 state=json.loads(subprocess.check_output(q,input=json.dumps({{'op':'runtime_status','id':i['agent']}}),text=True))
 if state['agent']['state']=='Running' and pathlib.Path('/run/kane-system',i['agent']+'.runtime.sock').is_socket():break
 if subprocess.run(['systemctl','is-failed','--quiet','kane-agent@'+i['agent']+'.service']).returncode==0:raise RuntimeError('Supervisor startup failed; Type=exec is not readiness')
 time.sleep(.5)
else:raise RuntimeError('No authoritative Supervisor/control readiness; no prompt admitted')
pathlib.Path('/exchange/job.json').write_text(json.dumps(i,indent=2))
k=subprocess.run(['python3','/fixtures/kernel-metrics.py',{config!r}],capture_output=True,text=True);pathlib.Path('/exchange/kernel-before-execution.json').write_text(k.stdout);pathlib.Path('/exchange/kernel-before-execution.stderr').write_text(k.stderr)
"""
  if a.existing_job:
   start=code.index('i=json.loads(');code=code[start:]
   code="import subprocess,pathlib,json\n"+code
  if a.model_load_timeout_seconds is not None:
   marker="subprocess.run(['systemctl','start','kane-model@'+i['instance']+'.service'],check=True)"
   load_budget=f"""import os
manifest=json.loads(pathlib.Path('/usr/share/kane-system/installed-manifest.json').read_text());assert {a.model_load_timeout_seconds}==45 or manifest['sequence']>=10
model_file=pathlib.Path('/etc/kane-system/models')/(i['instance']+'.json');spec=json.loads(model_file.read_text());old=spec.get('health_timeout_seconds',45);spec['health_timeout_seconds']={a.model_load_timeout_seconds}
temporary=model_file.with_suffix('.loading-budget-new');temporary.write_text(json.dumps(spec));temporary.chmod(0o640);os.chown(temporary,0,901)
with temporary.open('rb') as f:os.fsync(f.fileno())
os.replace(temporary,model_file)
pathlib.Path('/exchange/model-loading-budget.json').write_text(json.dumps({{'old_seconds':old,'new_seconds':{a.model_load_timeout_seconds},'default_policy_seconds':45,'scope':'Explicit bounded cold-load budget; actual health/endpoint ownership and every kernel/token check still required'}},indent=2))
"""
   code=code.replace(marker,load_budget+'\n'+marker)
  if a.model_cpu_millis is not None:
   marker="subprocess.run(['systemctl','start','kane-model@'+i['instance']+'.service'],check=True)"
   adjustment=f"""import os,hashlib
model_file=pathlib.Path('/etc/kane-system/models')/(i['instance']+'.json');original=model_file.read_bytes();spec=json.loads(original);assert spec['runtime']['resources']['cpu_millis']==1000
policy=json.loads(subprocess.check_output(q,input=json.dumps({{'op':'host_resources'}}),text=True))['policy'];assert policy['capacity']['cpu_millis']==2000 and policy['reserved']['cpu_millis']==200
spec['runtime']['resources']['cpu_millis']={a.model_cpu_millis};saved=model_file.with_suffix('.before-cpu-profile.json');assert not saved.exists();saved.write_bytes(original);saved.chmod(0o640);os.chown(saved,0,901)
temporary=model_file.with_suffix('.owner-new');temporary.write_text(json.dumps(spec));temporary.chmod(0o640);os.chown(temporary,0,901)
with temporary.open('rb') as f:os.fsync(f.fileno())
os.replace(temporary,model_file)
pathlib.Path('/exchange/model-cpu-profile.json').write_text(json.dumps({{'old_cpu_millis':1000,'new_cpu_millis':{a.model_cpu_millis},'host_pool_cpu_millis':2000,'host_reserved_cpu_millis':200,'agent_cpu_millis':500,'same_total_budget':True,'memory_and_token_limits_unchanged':True,'reason':'Actual cold TCG load exceeded 45s; bounded CPU adjustment inside existing fixed Host capacity, no bypass of health or kernel enforcement'}},indent=2))
"""
   code=code.replace(marker,adjustment+'\n'+marker)
  if a.retry_uninitialized:
   # Reference-plugin packaging repair, before launch, with independent old/new
   # bytes and authoritative non-dispatch evidence. No Agent Loop or tool rewrite.
   marker="subprocess.run(['systemctl','start','kane-model@'+i['instance']+'.service'],check=True)"
   repair="""import hashlib,shutil,os
task=json.loads(subprocess.check_output(q,input=json.dumps({'op':'task','id':i['task']}),text=True));events=json.loads(subprocess.check_output(q,input=json.dumps({'op':'events'}),text=True));assert task['state'] in ['Pending','Ready'] and not any(e['kind']=='prompt_dispatch' and e['object_id']==i['task'] for e in events)
guard=pathlib.Path(i['root'])/'tool-scope.mjs';old=guard.read_bytes();new=pathlib.Path('/fixtures/tool-scope.mjs').read_bytes();assert b'export default { name, inject, apply }' in new
saved=pathlib.Path(i['root'])/'tool-scope.before-explicit-owner-repair.mjs'
if saved.exists():assert old==new,'Reference changed after repair; inspect instead of overwriting history'
else:saved.write_bytes(old);saved.chmod(0o400);os.chown(saved,901,901)
guard.chmod(0o600);guard.write_bytes(new);guard.chmod(0o400)
pathlib.Path('/exchange/plugin-repair.json').write_text(json.dumps({'before_bytes':len(old),'before_sha256':hashlib.sha256(old).hexdigest(),'after_bytes':len(new),'after_sha256':hashlib.sha256(new).hexdigest(),'scope':'same restricted test guard with explicit export interop; old file retained; no task/model dispatch'},indent=2))
"""
   code=code.replace(marker,repair+'\n'+marker)
  vm.command(code,timeout=600)
  code=f"""import subprocess,pathlib,json,time
p=subprocess.run(['runuser','-u','kane-system','--','python3','/fixtures/native-reference-workflow.py','execute',{config!r}]+{(['--retry-uninitialized'] if a.retry_uninitialized else [])!r}+['--acp-initialize-timeout-seconds',{str(a.acp_initialize_timeout_seconds)!r}],capture_output=True,text=True)
pathlib.Path('/exchange/execute.log').write_text(p.stdout+p.stderr);print(p.stdout,p.stderr)
i=json.loads(pathlib.Path({config!r}).read_text())
for name in ['execution-evidence.json','execution-failure.json','initialization-retry-proof.json','acp-initialized.json','verification.json']:
 f=pathlib.Path(i['root'])/name
 if f.exists():pathlib.Path('/exchange/'+name).write_bytes(f.read_bytes())
d=subprocess.run(['/usr/local/bin/docker','logs','--timestamps','kane-'+i['agent']],capture_output=True,text=True,timeout=30)
pathlib.Path('/exchange/docker-agent.log').write_text(d.stdout+d.stderr)
pathlib.Path('/exchange/system-journal.log').write_text(subprocess.check_output(['journalctl','--no-pager','-b'],text=True))
k=subprocess.run(['python3','/fixtures/kernel-metrics.py',{config!r}],capture_output=True,text=True);pathlib.Path('/exchange/kernel-metrics.json').write_text(k.stdout);pathlib.Path('/exchange/kernel-metrics.stderr').write_text(k.stderr)
p.check_returncode()
p=subprocess.run(['python3','/fixtures/native-reference-workflow.py','verify',{config!r}],capture_output=True,text=True)
pathlib.Path('/exchange/verify.log').write_text(p.stdout+p.stderr);print(p.stdout,p.stderr)
f=pathlib.Path(i['root'])/'verification.json'
if f.exists():pathlib.Path('/exchange/verification.json').write_bytes(f.read_bytes())
p.check_returncode()
"""
  vm.command(code,timeout=1000)
  result['status']='PASS';result['scope']='native boot, original Kanaloa, real model, independent verification'
  vm.send('sudo -n systemctl poweroff');vm.expect(__import__('pexpect').EOF,timeout=180)
 except Exception:
  result.update(status='FAIL',failure=traceback.format_exc())
  try:vm.command("import pathlib,subprocess;pathlib.Path('/exchange/failure-journal.log').write_text(subprocess.check_output(['journalctl','-b','--no-pager'],text=True))",timeout=90)
  except Exception:pass
  raise
 finally:
  (out/'result.json').write_text(json.dumps(result,indent=2)+'\n');vm.stop()
if __name__=='__main__':main()
