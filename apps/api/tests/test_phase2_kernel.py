"""Actual kernel tests; must run inside the guarded offline reference VM."""
import asyncio,json,os,signal,socket,subprocess,time
from pathlib import Path
import pytest
from test_phase2_models import models,finish
from test_system_execution import run_task
from test_supervised_runtime import BIN,wait_for,dead
from test_model_fifo import until
M=1024**2
pytestmark=pytest.mark.skipif(os.getenv('KANE_PHASE2_KERNEL_TEST')!='1',reason='opt-in disposable Linux VM model cgroups/strict ten Kanaloa')
POLICY={'capacity':{'cpu_millis':2000,'memory_bytes':3584*M,'pids':4096,'storage_bytes':4096*M},'reserved':{'cpu_millis':250,'memory_bytes':512*M,'pids':64,'storage_bytes':256*M}}
def options(real=True,agents=1,output=8):
 return {'real':real,'agents':agents,'output_tokens':output,'strict':True,'defer_agents':True,'host_policy':POLICY,'gateway_wait_ms':900000,'poll_interval_ms':100,'enforcement':{'mode':'cgroup_v2','root':os.environ.get('KANE_PHASE2_CGROUP_ROOT','/no-delegation')},'context_tokens':2048,'agent_cpu_millis':1000 if agents==1 else 125,'model_resources':{'cpu_millis':750 if agents==1 else 500,'memory_bytes':512*M,'pids':64,'storage_bytes':128*M},'backend_args':['--threads','1','--threads-batch','1','--batch-size','128','--ubatch-size','64']}
def counter(text,key):return int(dict(s.split() for s in text.splitlines())[key])
def snapshot(path):
 return {n:(path/n).read_text().strip() for n in ['cpu.max','cpu.stat','memory.max','memory.current','memory.events','pids.max','pids.current','pids.events','cgroup.procs']}
async def start(items):
 for item in items:
  item[0].start()
  def running(item=item):
   assert item[0].supervisors[-1].poll() is None,(item[0].path/'runtime.log').read_text()
   return item[0].control('status',timeout=10)
  await asyncio.to_thread(wait_for,running,180)
  # Serialize sandbox creation; all ten remain online for concurrent requests.
  async def container_running(item=item):
   process=await asyncio.create_subprocess_exec('docker','inspect','kane-'+item[0].agent,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
   out,_=await asyncio.wait_for(process.communicate(),20)
   return process.returncode==0 and json.loads(out)[0]['State']['Running']
  await until(container_running,seconds=180)
 # Native initialize itself is the readiness contract; hooks can load lazily.
 # The explicit bounded VM timeout leaves the production default unchanged.
 # Initialization is an original Kanaloa ACP request, never a mock Worker.
 await asyncio.gather(*(asyncio.wait_for(x[2].activate(),620) for x in items))
 for item in items:
  item[0].call({'op':'grant_tool','id':item[0].agent,'tool':'read','arguments':{'file_path':'input.txt'},'expires_at':int(time.time())+1200})
async def stop_agent(item):
 r,_,_,core=item;reply='confirmed'
 try:await asyncio.to_thread(r.control,'stop',timeout=30)
 except (OSError,ValueError):reply='unknown'
 for process in r.supervisors:await asyncio.to_thread(process.wait,120)
 async def absent():
  process=await asyncio.create_subprocess_exec('docker','ps','-a','--filter','name=^/kane-'+r.agent+'$','--format','{{.ID}}',stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
  out,_=await process.communicate()
  return process.returncode==0 and not out.strip()
 await until(absent,seconds=120)
 # Existing Owner maintenance verifies the kernel lock and absence again. It
 # releases only physical compute; never refunds/replays Unknown model work.
 before=await core.request('host_resources')
 process=await asyncio.create_subprocess_exec(str(BIN),'--socket',str(r.core_socket),'reconcile-runtime','--agent',r.agent,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
 out,err=await process.communicate();assert process.returncode==0,err.decode()
 proof={'agent_id':r.agent,'stop_reply':reply,'all_supervisor_exit_codes':[p.returncode for p in r.supervisors],'daemon_confirmed_absence':True,'owner_kernel_lock_reconcile':True,'resources_before':before,'resources_after':await core.request('host_resources')}
 (r.path/'cleanup-proof.json').write_text(json.dumps(proof,indent=2));return proof

async def model_stop(models):
 r=models['items'][0][0]
 def stop():
  with socket.socket(socket.AF_UNIX) as s:s.settimeout(50);s.connect(str(r.core_socket.parent/f'{models["instance"]}.model.sock'));s.sendall(b'stop\n');return s.recv(100)
 assert await asyncio.to_thread(stop)==b'stopped\n'
 await asyncio.to_thread(models['manager'].wait,10)

@pytest.mark.asyncio
@pytest.mark.parametrize('models',[options(output=256)],indirect=True)
async def test_actual_model_kernel_cpu_pids_and_oom(models):
 items=models['items'];r,_,adapter,core=items[0];group=Path(os.environ['KANE_PHASE2_CGROUP_ROOT'])/f'model-{models["instance"]}'
 try:
  await start(items);before=snapshot(group)
  assert before['cpu.max']=='75000 100000' and before['memory.max']==str(512*M) and before['pids.max']=='64'
  # Fixed Owner probe tests PID creation in the actual model cgroup; it does not replace inference.
  probe="import os,time,json;children=[];denied=False\nfor _ in range(100):\n try:p=os.fork()\n except BlockingIOError:denied=True;break\n if p==0:time.sleep(1);os._exit(0)\n children.append(p)\nfor p in children:os.waitpid(p,0)\nprint(json.dumps({'pid_denied':denied,'forked':len(children)}))"
  p=await asyncio.create_subprocess_exec(str(BIN),'model-exec','--parent',str(os.getpid()),'--cgroup',str(group),'--','/usr/bin/python3','-c',probe,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
  out,err=await p.communicate();assert p.returncode==0,err.decode();assert json.loads(out)['pid_denied']
  pid_proof=snapshot(group);assert counter(pid_proof['pids.events'],'max')>counter(before['pids.events'],'max')
  await run_task(items[0],wait=False,prompt='Write a detailed short paragraph about the sky.')
  async def dispatched():return [e for e in await core.request('events') if e['kind']=='model_gateway_dispatch']
  await until(dispatched,seconds=120)
  await asyncio.sleep(2)
  active=snapshot(group);assert counter(active['cpu.stat'],'nr_throttled')>0
  # Fault injection lowers the real backend's memory cap below its working set.
  # The kernel must kill the real model, not a stand-in memory Worker.
  row=next(i for i in await core.request('model_instances') if i['id']==models['instance']);pid=row['pid']
  (group/'memory.max').write_text(str(32*M))
  async def failed():return [i for i in await core.request('model_instances') if i['id']==models['instance'] and i['state']=='Failed']
  await until(failed,seconds=60)
  async def unknown():return [q for q in await core.request('model_queue') if q['state']=='Unknown']
  await until(unknown,seconds=60);assert dead(pid)
  events=await core.request('events');exits=[e['payload']['evidence'] for e in events if e['kind']=='model_kernel_limits' and e['object_id']==models['instance'] and e['payload']['evidence'].get('phase')=='process_exit']
  assert exits and counter(exits[-1]['memory.events'],'oom_kill')>0
  held=await core.request('host_resources');assert held['used']['cpu_millis']==1750
  budget=await core.request('model_budget',id=r.budget);assert budget['reserved_tokens']==2048+256
  await asyncio.to_thread(models['manager'].wait,30)
  reclaim=await asyncio.create_subprocess_exec(str(BIN),'--socket',str(r.core_socket),'model-reconcile',models['instance']);assert await reclaim.wait()==0
  after=await core.request('host_resources');assert after['used']['cpu_millis']==1000
  assert (await core.request('model_budget',id=r.budget))['reserved_tokens']==2048+256
  assert len(await dispatched())==1
  (models['path']/'model-kernel-proof.json').write_text(json.dumps({'before':before,'pid_probe':json.loads(out),'after_pid_probe':pid_proof,'active_real_model':active,'real_model_pid_killed':pid,'kernel_exit_evidence':exits,'held_before_reconcile':held,'after_confirmed_absence':after,'budget_unknown_retained':budget,'events':await core.request('events')},indent=2))
 finally:
  await stop_agent(items[0])
  await asyncio.wait_for(adapter.close(),30)

@pytest.mark.asyncio
@pytest.mark.parametrize('real,fault',[(False,False),(True,False),(True,True)])
async def test_ten_kanaloa_strict_resources(real,fault,tmp_path,monkeypatch):
 from types import SimpleNamespace
 generator=models.__wrapped__(tmp_path,monkeypatch,SimpleNamespace(param=options(real=real,agents=11)))
 m=next(generator);items=m['items'];running=items[:10];r=items[0][0];core=items[0][3]
 try:
  await start(running)
  pool=await core.request('host_resources');assert pool['used']['cpu_millis']==1750 and pool['used']['memory_bytes']==(512+10*192)*M
  rejected=items[10][0].start();assert await asyncio.to_thread(rejected.wait,120)!=0
  assert subprocess.run(['docker','inspect','kane-'+items[10][0].agent],capture_output=True).returncode!=0
  kernel=[]
  for item in running:
   name='kane-'+item[0].agent
   info=json.loads(subprocess.check_output(['docker','inspect',name],text=True))[0]
   proof=subprocess.check_output(['docker','exec',name,'/bin/sh','-c','cat /sys/fs/cgroup/cpu.max /sys/fs/cgroup/memory.max /sys/fs/cgroup/pids.max /sys/fs/cgroup/cpu.stat /sys/fs/cgroup/memory.events'],text=True)
   assert proof.splitlines()[:3]==['12500 100000',str(192*M),'96']
   assert info['HostConfig']['ReadonlyRootfs'] and info['HostConfig']['NetworkMode']=='none'
   assert info['HostConfig']['PidsLimit']==96 and info['HostConfig']['Memory']==192*M
   kernel.append({'agent_id':item[0].agent,'container_id':info['Id'],'kernel':proof,'home':str(item[0].home),'mounts':info['Mounts']})
  # Force one real Agent process death before dispatch, while the other nine stay alive.
  binding=(await core.request('runtime_status',id=running[-1][0].agent))['agent']
  before_status=running[-1][0].status();old_pid=before_status['pid'];subprocess.run(['docker','kill','--signal','KILL','kane-'+running[-1][0].agent],check=True,capture_output=True)
  def restarted():
   try:
    s=running[-1][0].status();return s if s['pid']!=old_pid else None
   except OSError:return None
  await asyncio.to_thread(wait_for,restarted,180)
  assert (await core.request('runtime_status',id=running[-1][0].agent))['agent']['id']==binding['id']
  # Adapter explicitly reconnects to the new supervised runtime; no double launch.
  await running[-1][2].activate()
  await asyncio.gather(*(run_task(x,wait=False,prompt='Reply HELLO. Do not use tools.') for x in running))
  group=Path(os.environ['KANE_PHASE2_CGROUP_ROOT'])/f'model-{m["instance"]}'
  if fault:
   async def inflight():
    rows=await core.request('model_queue')
    return rows if len(rows)==10 and any(q['state']=='Dispatched' for q in rows) else None
   await until(inflight,seconds=300)
   model_kernel=snapshot(group)
   model_pid=next(x['pid'] for x in await core.request('model_instances') if x['id']==m['instance'])
   os.kill(model_pid,signal.SIGKILL)
   async def unknown():return [q for q in await core.request('model_queue') if q['state']=='Unknown']
   await until(unknown,seconds=90)
   rows=await core.request('model_queue');assert len(rows)==10 and len([q for q in rows if q['state']=='Unknown'])==1
   dispatch_count=len([e for e in await core.request('events') if e['kind']=='model_gateway_dispatch'])
   await asyncio.sleep(3)
   assert len([e for e in await core.request('events') if e['kind']=='model_gateway_dispatch'])==dispatch_count
   assert (await core.request('host_resources'))['used']['cpu_millis']==1750
   await asyncio.to_thread(m['manager'].wait,30)
   reclaim=await asyncio.create_subprocess_exec(str(BIN),'--socket',str(r.core_socket),'model-reconcile',m['instance']);assert await reclaim.wait()==0
   after_model=await core.request('host_resources');assert after_model['used']['cpu_millis']==1250
   budgets=[await core.request('model_budget',id=x[0].budget) for x in running]
   assert any(b['reserved_tokens']>0 for b in budgets)
   states=[await core.request('task',id=x[0].task) for x in running]
  else:
   states=await asyncio.gather(*(finish(x,seconds=1000) for x in running))
   rows=await core.request('model_queue');assert len(rows)==10 and all(q['state']=='Completed' for q in rows),rows
   for x in running:assert (await core.request('model_budget',id=x[0].budget))['reserved_tokens']==0
   shared=await core.request('host_resources');assert shared['used']==pool['used']
   model_kernel=snapshot(group)
   await model_stop(m);after_model=await core.request('host_resources');assert after_model['used']['cpu_millis']==1250
  events=await core.request('events');seqs=[e['payload']['queue_seq'] for e in events if e['kind']=='model_dispatched'];assert seqs==sorted(seqs)
  files=[]
  for i,x in enumerate(running):
   await stop_agent(x)
   files.append({'agent_id':x[0].agent,'home_device':x[0].home.stat().st_dev,'owner_uid':x[0].home.stat().st_uid,'sessions':[str(p.relative_to(x[0].home)) for p in x[0].home.rglob('*.jsonl')]})
  end=await core.request('host_resources');assert end['used']['cpu_millis']==0 and end['used']['memory_bytes']==0 and end['used']['storage_bytes']>0
  assert len({x['home_device'] for x in files})==10
  done=[q for q in rows if q['state']=='Completed'];waits=sorted(q['queue_wait_ms'] for q in done)
  elapsed=((max(q['finished_ms'] for q in done)-min(q['dispatched_ms'] for q in done))/1000) if done else None
  metrics={'completed':len(done),'model_call_success_rate':len(done)/len(rows),'wait_ms_p50':waits[(len(waits)-1)//2] if waits else None,'wait_ms_p95':waits[min(len(waits)-1,__import__('math').ceil(.95*len(waits))-1)] if waits else None,'completed_calls_per_second':len(done)/max(elapsed,.001) if done else None,'scope':'N=1 reference, fault run separately; not intelligence quality or collaborative benefit'}
  (tmp_path/'strict-ten-proof.json').write_text(json.dumps({'metrics':metrics,'real_model':real,'injected_model_fault':fault,'agents':10,'registered_capacity_probe_agent':items[10][0].agent,'shared_instances':1,'pool_live':pool,'agent_kernel':kernel,'model_kernel':model_kernel,'queue':rows,'events':events,'task_states':states,'after_model_stop':after_model,'final_pool':end,'persistent_homes':files,'fault_agent':{'old_pid':old_pid,'binding_before':binding,'binding_after':(await core.request('runtime_status',id=running[-1][0].agent))['agent']}},indent=2))
 finally:
  cleanup_errors=[]
  try:
   for x in running:
    try:await stop_agent(x)
    except Exception as error:cleanup_errors.append(repr(error))
   closed=await asyncio.gather(*(asyncio.wait_for(x[2].close(),30) for x in items),return_exceptions=True)
   cleanup_errors.extend(repr(x) for x in closed if isinstance(x,BaseException))
  finally:
   try:next(generator)
   except StopIteration:pass
  (tmp_path/'cleanup-errors.json').write_text(json.dumps(cleanup_errors,indent=2))
  assert not cleanup_errors,cleanup_errors
