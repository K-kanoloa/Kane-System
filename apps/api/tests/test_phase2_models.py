"""Phase 2 real Kanaloa/DSH, original pi-ai client, Rust Unix gateway and models."""
import asyncio,hashlib,json,os,socket,subprocess,time
from pathlib import Path
from types import SimpleNamespace
import pytest
from test_system_execution import execution,run_task
from test_supervised_runtime import BIN,ROOT,wait_for
from test_model_fifo import until,completed,restart_core

pytestmark=pytest.mark.skipif(os.getenv('KANE_PHASE2_TEST')!='1',reason='opt-in Phase 2 local backend, real DSH and Rust gateway')

def port():
 with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]

@pytest.fixture
def models(tmp_path,monkeypatch,request):
 options=getattr(request,'param',{});count=options.get('agents',1);real=options.get('real',False);output=options.get('output_tokens',32);context=options.get('context_tokens',16384);parallelism=options.get('parallelism',1)
 generators=[];items=[];processes=[];backend_port=port();instance=None;evidence_error=None;log=(tmp_path/'model-runtime.log').open('w')
 spec={'cloud':False,'scheduling':{'mode':'gateway_fifo','backend_kind':'local_http'},'budget_profile':{'provider':'kane-fixture','model':'deterministic-v1','upper_bound_tokens':context+output,'bound_evidence':f'Owner-local {context} context + {output} output hard cap; backend config and actual usage separately recorded','gateway_wait_ms':options.get('gateway_wait_ms',180000)}}
 spec['version']='phase2-reference-v1'
 spec['capabilities']=['text-inference','streaming-usage']
 spec['privacy']={'cloud_allowed':False,'prompt_cache':'disabled'}
 spec['pricing']={'status':'UNKNOWN','actual_invoice_verified':False}
 spec['source']={'backend_kind':'real-local-llm' if real else 'deterministic-mock'}
 if real:
  spec['source']['weights_sha256']=hashlib.sha256(Path(options.get('model_file',os.environ['KANE_LOCAL_MODEL_FILE'])).read_bytes()).hexdigest()
  spec['source']['server_sha256']=hashlib.sha256(Path(os.environ['KANE_LOCAL_MODEL_SERVER']).read_bytes()).hexdigest()
 try:
  for i in range(count):
   case=tmp_path/str(i);case.mkdir()
   config={'sandbox':True,'defer_start':True,'phase2_model_spec':spec,'budget_tokens':options.get('budget_tokens',100000),'task_goal':options.get('task_goal','Reply with one word using the approved local model'),'task_acceptance':options.get('task_acceptance',['Original DSH reaches the authorized local model; model result and usage are traceable'])}
   if options.get('read_task'):
    config.update(task_goal='Read the isolated input.txt with the original read tool and deliver its exact marker',task_acceptance=['Confirmed original read tool call','Immutable input hash and exact marker in independently hashed Artifact','Independent verifier alone may mark Done'])
   if options.get('task_goal'):config['task_goal']=options['task_goal']
   if options.get('task_acceptance'):config['task_acceptance']=options['task_acceptance']
   if 'agent_cpu_millis' in options:config['sandbox_cpu_millis']=options['agent_cpu_millis']
   if 'agent_memory_bytes' in options:config.update(sandbox_memory_bytes=options['agent_memory_bytes'],task_memory_bytes=options['agent_memory_bytes'])
   if options.get('strict'):
    config.update(bounded_home=True,host_resources=True,runtime_home=str(Path(os.environ['KANE_PHASE2_HOME_ROOT'])/str(i)),sandbox_cpu_millis=options.get('agent_cpu_millis',100),sandbox_memory_bytes=192*1024**2,initialize_timeout_seconds=600)
   if options.get('control_files'):
    config.update(native_file_proxy=True,native_read_fencing=True,task_goal='Review the native document and incorporate audited Owner edits',task_acceptance=['Original restricted tool reads actual native bytes; no Agent overwrite','Owner edits retain version/hash and invalidate old verification','After handoff original tool must read new bytes','Independent verifier checks final native/Artifact/source hashes before Done'])
   config.update(options.get('agent_configs',[{}]*count)[i])
   if items:config['core_socket']=str(items[0][0].core_socket)
   g=execution.__wrapped__(case,monkeypatch,SimpleNamespace(param=config));generators.append(g);items.append(next(g))
  r=items[0][0]
  r.call({'op':'configure_host_resources','version':0,'policy':options.get('host_policy') or {'capacity':{'cpu_millis':2000,'memory_bytes':3*1024**3,'pids':4096,'storage_bytes':2*1024**3},'reserved':{'cpu_millis':500,'memory_bytes':512*1024**2,'pids':32,'storage_bytes':64*1024**2}}})
  instance=r.call({'op':'register_model_instance','model_id':'kane-fixture/deterministic-v1'})['id']
  backend_model='phase2-real' if real else 'phase2-mock'
  command=([os.environ['KANE_LOCAL_MODEL_SERVER'],'--model',options.get('model_file',os.environ['KANE_LOCAL_MODEL_FILE']),'--host','127.0.0.1','--port',str(backend_port),'--alias',backend_model,'--ctx-size',str(context*parallelism),'--parallel',str(parallelism),'--threads','2','--threads-http','2','--n-gpu-layers','0','--no-warmup','--cache-ram','0','--seed','42','--temp','0',*options.get('backend_args',[])] if real else ['/usr/bin/python3',str(ROOT/'bench/phase2/local-backend.py'),'--port',str(backend_port),'--delay-ms',str(options.get('delay_ms',20))])
  runtime={'enforcement':options.get('enforcement',{'mode':'diagnostic_unmanaged'}),'instance_id':instance,'command':command,'runtime':{'endpoint':f'http://127.0.0.1:{backend_port}','backend_model':backend_model,'context_tokens':context,'parallelism':parallelism,'resources':options.get('model_resources') or {'cpu_millis':1000,'memory_bytes':768*1024**2,'pids':32,'storage_bytes':128*1024**2}}}
  model_spec=tmp_path/'model.json';model_spec.write_text(json.dumps(runtime))
  manager=subprocess.Popen([str(BIN),'--socket',str(r.core_socket),'model-run',str(model_spec)],stderr=log);processes.append(manager)
  def ready():
   assert manager.poll() is None,(tmp_path/'model-runtime.log').read_text()
   return next((i for i in r.call({'op':'model_instances'}) if i['id']==instance and i['state']=='Ready'),None)
  wait_for(ready,seconds=60)
  policy={'instance_id':instance,'max_output_tokens':output,'cache':'disabled','sensitivity':'public','requires_kernel_limits':options.get('strict',False),'workload_classes':['inference','execution'],'workspaces':[x[0].ws for x in items]}
  r.call({'op':'configure_model_gateway','id':'kane-fixture/deterministic-v1','version':0,'policy':policy})
  center=r.path/'models';center.mkdir(mode=0o700);gateway=center/'model.sock'
  proxy=subprocess.Popen([str(BIN),'--socket',str(r.core_socket),'model-gateway',str(gateway)],stderr=log);processes.append(proxy)
  def broker_registered():
   assert proxy.poll() is None,(tmp_path/'model-runtime.log').read_text()
   return gateway.exists() and any(x['pid']==proxy.pid for x in r.call({'op':'model_listeners'}))
  wait_for(broker_registered)
  for item in items:
   configure_native_gateway(item,gateway,context,output,options,tmp_path)
   if not options.get('defer_agents'):item[0].start();wait_for(item[0].status)
  yield {'items':items,'instance':instance,'manager':manager,'gateway':proxy,'real':real,'policy':policy,'runtime':runtime,'path':tmp_path,'processes':processes}
 finally:
  if items and items[0][0].core is not None and items[0][0].core.poll() is not None:restart_core(items[0][0])
  for item in items:
   try:item[0].control('stop')
   except OSError:pass
  if items and instance:
   try:
    with socket.socket(socket.AF_UNIX) as s:
     s.settimeout(50);s.connect(str(items[0][0].core_socket.parent/f'{instance}.model.sock'));s.sendall(b'stop\n');s.recv(100)
   except OSError:pass
   try:
    (tmp_path/'model-evidence.json').write_text(json.dumps({'instances':items[0][0].call({'op':'model_instances'}),'resources':items[0][0].call({'op':'host_resources'}),'queue':items[0][0].call({'op':'model_queue'}),'events':items[0][0].call({'op':'events'}),'real_model':real,'runtime_spec':runtime,'models':items[0][0].call({'op':'models'})},indent=2))
   except Exception as error:
    evidence_error=error;(tmp_path/'evidence-collection-error.txt').write_text(repr(error))
  for p in reversed(processes):
   if p.poll() is None:p.terminate()
   try:p.wait(timeout=5)
   except subprocess.TimeoutExpired:p.kill();p.wait()
  for i,item in enumerate(items):
   diag=item[0].home/'p2-diagnostics.jsonl'
   if diag.exists():(tmp_path/f'native-diagnostics-{i}.jsonl').write_bytes(diag.read_bytes())
  for g in reversed(generators):
   try:next(g)
   except StopIteration:pass
  log.close()
  if evidence_error is not None:raise evidence_error

def configure_native_gateway(item,gateway,context,output,options,tmp_path):
 rr=item[0];patch=Path(json.loads(rr.spec.read_text())['command'][-1]);entries=json.loads(patch.read_text())
 inserts=entries[-1]['insert'];inserts[:]=[p for p in inserts if p['id']!='kane-fixture']
 provider_entry={'id':'llm-pi-ai','config':{'providers':{'kane-fixture':{'api':'openai-completions','baseURL':'http://kane-model.invalid/v1','headers':{'Authorization':'Bearer local-placeholder-not-a-secret'},'cacheRetention':'none','retryPolicy':{'mode':'normal','maxRetries':0},'compat':{'supportsStore':False,'supportsUsageInStreaming':True,'maxTokensField':'max_tokens'},'models':[{'id':'deterministic-v1','name':'Local reference','contextWindow':context,'maxTokens':output,'input':['text'],'reasoning':False}]}}}}
 settings=json.loads((rr.home/'.dsh/settings.yaml').read_text());settings['llm-pi-ai']=provider_entry['config'];(rr.home/'.dsh/settings.yaml').write_text(json.dumps(settings))
 inserts.append({'id':'phase2-diagnostics','name':str(ROOT/'bench/phase2/diagnostics.mjs'),'config':{'read_task':options.get('read_task',False),'no_tools':options.get('strict',False)}})
 linked=rr.path/'gateway'/'model.sock';os.link(rr.core_socket if options.get('forge_owner_socket') else gateway,linked)
 next(p for p in inserts if p['id']=='kane-model-budget')['config'].update(gateway_socket=str(linked),require_fifo=True,poll_interval_ms=options.get('poll_interval_ms',25))
 patch.write_text(json.dumps(entries))
 runtime_spec=json.loads(rr.spec.read_text());runtime_spec['sandbox']['model_gateway']=True;runtime_spec['sandbox']['read_only'].append(str(ROOT/'bench/phase2'));rr.spec.write_text(json.dumps(runtime_spec))
 async def capture_stderr(adapter=item[2],path=tmp_path/rr.agent/'native-stderr.log'):
  path.parent.mkdir(exist_ok=True)
  with path.open('ab') as sink:
   while line := await adapter._process.stderr.readline():sink.write(line);sink.flush()
 item[2]._read_stderr_loop=capture_stderr

@pytest.mark.asyncio
async def test_original_dsh_client_through_rust_gateway_mock(models):
 item=models['items'][0];r,store,adapter,core=item
 try:
  turn,_,state=await run_task(item,prompt='Reply with the single word HELLO. Do not call any tools.')
  assert state['state']=='Verifying',(state,store.get_turn(turn.turn_id))
  budget=await core.request('model_budget',id=r.budget)
  assert budget['used_tokens']==14 and budget['reserved_tokens']==0
  events=await core.request('events')
  assert any(e['kind']=='model_gateway_dispatch' for e in events)
  assert any(e['kind']=='model_dispatched' for e in events)
  assert 'LOCAL_MOCK_RESPONSE' in (await core.request('artifacts',id=r.task))[0]['content']
 finally:await adapter.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('models',[{'real':True}],indirect=True)
async def test_original_dsh_client_local_real_model_and_usage(models):
 item=models['items'][0];r,store,adapter,core=item
 try:
  turn,_,state=await run_task(item,prompt='Reply with the single word HELLO. Do not call any tools.')
  budget=await core.request('model_budget',id=r.budget)
  # Upper bound admits before HTTP; provider usage, never Context size, settles.
  assert budget['used_tokens']>0,(state,budget,store.get_turn(turn.turn_id))
  assert budget['reserved_tokens']==0,(state,budget)
  events=await core.request('events')
  receipts=[e for e in events if e['kind']=='model_settled']
  assert receipts and all(e['payload']['state']=='Settled' for e in receipts)
  (models['path']/'real-usage.json').write_text(json.dumps({'state':state,'budget':budget,'receipts':receipts,'real_model':True,'paid_invoice':'UNKNOWN; local unbilled model'},indent=2))
 finally:await adapter.close()

async def finish(item,seconds=180):
 async def check():
  t=await item[3].request('task',id=item[0].task)
  return t if t['state'] in ['Verifying','Blocked','Failed'] else None
 return await until(check,seconds=seconds)

def percentile(values,p):
 values=sorted(values);return values[min(len(values)-1,max(0,__import__('math').ceil(p*len(values))-1))]

async def save_metrics(models,states):
 core=models['items'][0][3];rows=await core.request('model_queue');events=await core.request('events')
 done=[q for q in rows if q['state']=='Completed'];waits=[q['queue_wait_ms'] for q in done];times=[q['permit_occupancy_ms'] for q in done]
 elapsed=(max(q['finished_ms'] for q in done)-min(q['dispatched_ms'] for q in done))/1000
 instances=await core.request('model_instances');pid=next(x['pid'] for x in instances if x['id']==models['instance'])
 process={}
 if pid:
  process={'status':Path(f'/proc/{pid}/status').read_text(),'io':Path(f'/proc/{pid}/io').read_text()}
 metrics={'real_model':models['real'],'agent_count':len(models['items']),'model_instances':1,'calls':len(rows),'completed':len(done),'model_call_success_rate':len(done)/len(rows),'task_states':[s['state'] for s in states],'queue_wait_ms':{'p50':percentile(waits,.5),'p95':percentile(waits,.95),'all':waits},'permit_occupancy_ms':{'p50':percentile(times,.5),'p95':percentile(times,.95)},'completed_calls_per_second':len(done)/max(elapsed,.001),'pool':await core.request('host_resources'),'model_process':process,'rows':rows,'events':events,'scope':'model execution performance, not multi-Agent intelligence benefit or real invoice validation'}
 (models['path']/'ten-agent-metrics.json').write_text(json.dumps(metrics,indent=2));return metrics

@pytest.mark.asyncio
@pytest.mark.parametrize('models',[{'agents':10,'delay_ms':300},{'agents':10,'real':True}],indirect=True)
async def test_ten_real_dsh_agents_one_shared_instance(models):
 items=models['items'];start=time.monotonic()
 try:
  await asyncio.gather(*(item[2].activate() for item in items))
  await asyncio.gather(*(run_task(item,wait=False,prompt='Reply with the single word HELLO. Do not call any tools.') for item in items))
  states=await asyncio.gather(*(finish(item) for item in items))
  metrics=await save_metrics(models,states)
  assert metrics['completed']==10 and metrics['calls']==10,metrics['rows']
  assert len({q['agent_id'] for q in metrics['rows']})==10
  dispatch=[e['payload']['queue_seq'] for e in metrics['events'] if e['kind']=='model_dispatched']
  assert dispatch==sorted(dispatch)
  assert len([e for e in metrics['events'] if e['kind']=='model_gateway_dispatch'])==10
  assert metrics['pool']['used']['cpu_millis']==1000  # residency counted once, not ten times
  for item in items:
   budget=await item[3].request('model_budget',id=item[0].budget)
   assert budget['reserved_tokens']==0 and budget['used_tokens']>0
  (models['path']/'elapsed.json').write_text(json.dumps({'seconds':time.monotonic()-start}))
 finally:await asyncio.gather(*(item[2].close() for item in items))

@pytest.mark.asyncio
@pytest.mark.parametrize('models',[{'budget_tokens':1}],indirect=True)
async def test_gateway_denies_insufficient_budget_before_backend(models):
 item=models['items'][0]
 try:
  _,_,state=await run_task(item)
  assert state['state']=='Blocked'
  rows=await item[3].request('model_queue');assert rows[0]['state']=='Denied'
  assert not any(e['kind']=='model_gateway_dispatch' for e in await item[3].request('events'))
  assert (await item[3].request('model_budget',id=item[0].budget))['used_tokens']==0
 finally:await item[2].close()

@pytest.mark.asyncio
async def test_truncated_gateway_stream_holds_budget_and_freezes_route(models):
 item=models['items'][0]
 try:
  _,_,state=await run_task(item,prompt='PHASE2_TRUNCATE')
  assert state['state']=='Blocked'
  budget=await item[3].request('model_budget',id=item[0].budget)
  assert budget['reserved_tokens']==16416 and budget['used_tokens']==0
  assert (await item[3].request('model_queue'))[0]['state']=='Unknown'
 finally:await item[2].close()

@pytest.mark.asyncio
@pytest.mark.parametrize('models',[{'agents':3,'delay_ms':3000},{'agents':3,'real':True}],indirect=True)
async def test_backend_sigkill_retains_unknown_and_does_not_replay(models):
 items=models['items'];core=items[0][3]
 try:
  await asyncio.gather(*(x[2].activate() for x in items))
  await asyncio.gather(*(run_task(x,wait=False,prompt='Reply HELLO') for x in items))
  async def claimed():return [e for e in await core.request('events') if e['kind']=='model_gateway_dispatch']
  await until(claimed)
  pid=next(i['pid'] for i in await core.request('model_instances') if i['id']==models['instance'])
  os.kill(pid,9)
  async def failed():return [i for i in await core.request('model_instances') if i['state']=='Failed']
  await until(failed)
  await asyncio.sleep(.2)
  rows=await core.request('model_queue');assert sum(q['state']=='Unknown' for q in rows)==1
  assert sum(q['state'] in ['Queued','Cancelled'] for q in rows)==2
  assert len(await claimed())==1
  budgets=[await x[3].request('model_budget',id=x[0].budget) for x in items]
  assert sum(b['reserved_tokens'] for b in budgets)==16416
  (models['path']/'failure-snapshot.json').write_text(json.dumps({'rows':rows,'budgets':budgets,'instances':await core.request('model_instances'),'automatic_replay':False},indent=2))
 finally:await asyncio.gather(*(x[2].close() for x in items))

@pytest.mark.asyncio
@pytest.mark.parametrize('models',[{'agents':3,'delay_ms':1500}],indirect=True)
async def test_queued_agent_sigkill_does_not_stop_other_model_requests(models):
 items=models['items'];core=items[0][3]
 try:
  await asyncio.gather(*(x[2].activate() for x in items))
  for index,item in enumerate(items):
   await run_task(item,wait=False,prompt='Reply HELLO')
   async def waiting(item=item):return [q for q in await core.request('model_queue') if q['agent_id']==item[0].agent]
   await until(waiting)
  os.kill(items[1][0].status()['pid'],9)
  states=await asyncio.gather(finish(items[0]),finish(items[2]))
  assert all(s['state']=='Verifying' for s in states)
  rows=await core.request('model_queue')
  assert sum(q['state']=='Completed' for q in rows)==2
  assert not any(q['state']=='Unknown' for q in rows)
  assert (await core.request('model_budget',id=items[1][0].budget))['reserved_tokens']==0
  (models['path']/'agent-fault.json').write_text(json.dumps({'queue':rows,'events':await core.request('events')},indent=2))
 finally:await asyncio.gather(*(x[2].close() for x in items))

@pytest.mark.asyncio
@pytest.mark.parametrize('models',[{'agents':3,'delay_ms':1500}],indirect=True)
async def test_cancel_queued_and_dispatched_model_calls_preserves_unknown(models):
 items=models['items'];core=items[0][3];turns=[]
 try:
  await asyncio.gather(*(x[2].activate() for x in items))
  for item in items:
   turn,_,_=await run_task(item,wait=False,prompt='Reply HELLO');turns.append(turn)
   async def waiting(item=item):return [q for q in await core.request('model_queue') if q['agent_id']==item[0].agent]
   await until(waiting)
  await items[1][2].cancel(turns[1]);await items[0][2].cancel(turns[0])
  await asyncio.sleep(2)
  rows=await core.request('model_queue');by_agent={q['agent_id']:q for q in rows}
  assert by_agent[items[0][0].agent]['state']=='Unknown'
  assert by_agent[items[1][0].agent]['state']=='Cancelled'
  assert by_agent[items[2][0].agent]['state']=='Queued'
  budgets=[await core.request('model_budget',id=x[0].budget) for x in items]
  assert [b['reserved_tokens'] for b in budgets]==[16416,0,0]
  assert sum(e['kind']=='model_gateway_dispatch' for e in await core.request('events'))==1
  (models['path']/'cancellation.json').write_text(json.dumps({'rows':rows,'budgets':budgets,'events':await core.request('events')},indent=2))
 finally:await asyncio.gather(*(x[2].close() for x in items))

@pytest.mark.asyncio
@pytest.mark.parametrize('models',[{'delay_ms':3000}],indirect=True)
async def test_core_loss_during_backend_execution_holds_usage_without_replay(models):
 item=models['items'][0];r,_,adapter,core=item
 try:
  await run_task(item,wait=False,prompt='Reply HELLO')
  async def dispatched():return [e for e in await core.request('events') if e['kind']=='model_gateway_dispatch']
  await until(dispatched)
  r.core.kill();r.core.wait();await asyncio.sleep(3.5);restart_core(r)
  rows=await core.request('model_queue');budget=await core.request('model_budget',id=r.budget)
  assert rows[0]['state']=='Unknown' and budget['reserved_tokens']==16416
  assert len(await dispatched())==1
  (models['path']/'core-loss.json').write_text(json.dumps({'rows':rows,'budget':budget,'events':await core.request('events')},indent=2))
 finally:
  if r.core.poll() is not None:restart_core(r)
  await adapter.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('models',[{'delay_ms':2500}],indirect=True)
async def test_owner_request_timeout_marks_unknown(models):
 item=models['items'][0];r,_,adapter,core=item
 try:
  policy=models['policy']|{'timeout_seconds':1}
  await core.request('configure_model_gateway',id='kane-fixture/deterministic-v1',version=1,policy=policy)
  _,_,state=await run_task(item,prompt='Reply HELLO')
  assert state['state']=='Blocked'
  budget=await core.request('model_budget',id=r.budget)
  assert budget['reserved_tokens']==16416 and budget['used_tokens']==0
  assert (await core.request('model_queue'))[0]['state']=='Unknown'
 finally:await adapter.close()

@pytest.mark.asyncio
async def test_model_manager_sigkill_cleanup_and_guarded_reconciliation(models):
 item=models['items'][0];r=item[0]
 try:
  instances=await item[3].request('model_instances');pid=next(x['pid'] for x in instances if x['id']==models['instance'])
  models['manager'].kill();models['manager'].wait()
  def absent():
   p=Path(f'/proc/{pid}/stat');return not p.exists() or p.read_text().split(') ',1)[1].startswith('Z')
  wait_for(absent)
  result=subprocess.run([str(BIN),'--socket',str(r.core_socket),'model-reconcile',models['instance']],capture_output=True,text=True)
  assert result.returncode==0,result.stderr
  instances=await item[3].request('model_instances');assert next(x for x in instances if x['id']==models['instance'])['state']=='Unloaded'
  pool=await item[3].request('host_resources');assert pool['used']['cpu_millis']==0
  (models['path']/'manager-loss.json').write_text(json.dumps({'instances':instances,'pool':pool},indent=2))
 finally:await item[2].close()

@pytest.mark.asyncio
@pytest.mark.parametrize('models',[{'agents':3,'delay_ms':2500}],indirect=True)
async def test_shared_gateway_owner_priority_preserves_fifo_within_priority(models):
 items=models['items'];core=items[0][3]
 try:
  await asyncio.gather(*(x[2].activate() for x in items))
  for item in items:
   await run_task(item,wait=False,prompt='Reply HELLO')
   async def waiting(item=item):return [q for q in await core.request('model_queue') if q['agent_id']==item[0].agent]
   await until(waiting)
  await core.request('set_model_priority',id=items[2][0].task,priority=90)
  await asyncio.gather(*(finish(x) for x in items))
  events=await core.request('events');order=[e['payload']['agent_id'] for e in events if e['kind']=='model_dispatched']
  assert order==[items[0][0].agent,items[2][0].agent,items[1][0].agent],order
  (models['path']/'priority.json').write_text(json.dumps({'order':order,'events':events},indent=2))
 finally:await asyncio.gather(*(x[2].close() for x in items))

@pytest.mark.asyncio
async def test_abnormal_final_usage_is_unknown_and_context_cannot_refund(models):
 item=models['items'][0];r,_,adapter,core=item
 try:
  turn,_,state=await run_task(item,prompt='PHASE2_BAD_USAGE')
  assert state['state']=='Blocked'
  budget=await core.request('model_budget',id=r.budget)
  assert budget['reserved_tokens']==16416 and budget['used_tokens']==0
  await core.request('harness_fact',turn_id=turn.turn_id,receipt='phase2-fake-context',kind='context_usage',payload={'native_session_ref':turn.native_session_ref,'used':1,'size':999999})
  assert await core.request('model_budget',id=r.budget)==budget
  assert (await core.request('model_queue'))[0]['state']=='Unknown'
 finally:await adapter.close()

@pytest.mark.asyncio
async def test_model_capacity_refusal_and_actual_gateway_without_permit(models):
 item=models['items'][0];r,_,adapter,core=item
 try:
  from app.system.client import CoreRejected
  second=await core.request('register_model_instance',model_id='kane-fixture/deterministic-v1')
  spec=models['runtime']['runtime']|{'resources':models['runtime']['runtime']['resources']|{'memory_bytes':64*1024**3}}
  with pytest.raises(CoreRejected):await core.request('load_local_model',id=second['id'],spec=spec)
  before=await core.request('host_resources');assert before['used']['cpu_millis']==1000
  path=r.path/'gateway/model.sock'
  # No model permit and no Owner API on the exposed Unix HTTP service.
  def unauthenticated():
   with socket.socket(socket.AF_UNIX) as s:
    s.settimeout(3);s.connect(str(path));body=json.dumps({'op':'register_agent','model':'deterministic-v1','stream':True,'max_tokens':1,'messages':[]}).encode()
    s.sendall(b'POST /v1/chat/completions HTTP/1.1\r\nContent-Length: '+str(len(body)).encode()+b'\r\n\r\n'+body)
    return s.recv(1024)
  assert await asyncio.to_thread(unauthenticated)==b''
  assert not any(e['kind']=='model_gateway_dispatch' for e in await core.request('events'))
  (models['path']/'capacity-denial.json').write_text(json.dumps({'pool':before,'instances':await core.request('model_instances'),'unauthorized_backend_dispatches':0},indent=2))
 finally:await adapter.close()

@pytest.mark.asyncio
async def test_health_from_another_process_cannot_mark_instance_ready(models):
 item=models['items'][0];r,_,adapter,core=item
 try:
  instance=(await core.request('register_model_instance',model_id='kane-fixture/deterministic-v1'))['id']
  runtime=models['runtime']['runtime']|{'resources':{'cpu_millis':100,'memory_bytes':64*1024**2,'pids':4,'storage_bytes':1024**2}}
  wrong={'enforcement':{'mode':'diagnostic_unmanaged'},'instance_id':instance,'runtime':runtime,'command':['/usr/bin/sleep','5'],'health_timeout_seconds':1}
  path=models['path']/'wrong-child.json';path.write_text(json.dumps(wrong))
  with (models['path']/'wrong-child.log').open('w') as log:
   child=await asyncio.create_subprocess_exec(str(BIN),'--socket',str(r.core_socket),'model-run',str(path),stderr=log)
   assert await asyncio.wait_for(child.wait(),10)!=0
  row=next(i for i in await core.request('model_instances') if i['id']==instance)
  assert row['state']=='Failed' and row['verified'] is False
  assert not any(e['object_id']==instance and e['kind']=='model_lifecycle' and e['payload']['to']=='Ready' for e in await core.request('events'))
  reclaim=await asyncio.create_subprocess_exec(str(BIN),'--socket',str(r.core_socket),'model-reconcile',instance)
  assert await reclaim.wait()!=0  # another real process still owns the same endpoint
  def stop_primary():
   with socket.socket(socket.AF_UNIX) as s:
    s.settimeout(50);s.connect(str(r.core_socket.parent/f'{models["instance"]}.model.sock'));s.sendall(b'stop\n');return s.recv(100)
  assert await asyncio.to_thread(stop_primary)==b'stopped\n'
  reclaim=await asyncio.create_subprocess_exec(str(BIN),'--socket',str(r.core_socket),'model-reconcile',instance)
  assert await reclaim.wait()==0
  assert (await core.request('host_resources'))['used']['cpu_millis']==0
  (models['path']/'health-refusal.json').write_text(json.dumps({'failed_instance':row,'events':await core.request('events'),'pool':await core.request('host_resources')},indent=2))
 finally:await adapter.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('models',[{'delay_ms':1500}],indirect=True)
async def test_draining_waits_for_known_result_then_unloads_real_backend_process(models):
 item=models['items'][0];r,_,adapter,core=item
 try:
  await run_task(item,wait=False,prompt='Reply HELLO')
  async def claimed():return [e for e in await core.request('events') if e['kind']=='model_gateway_dispatch']
  await until(claimed)
  def stop():
   with socket.socket(socket.AF_UNIX) as s:
    s.settimeout(50);s.connect(str(r.core_socket.parent/f'{models["instance"]}.model.sock'));s.sendall(b'stop\n');return s.recv(100)
  assert await asyncio.to_thread(stop)==b'stopped\n'
  state=await finish(item);assert state['state']=='Verifying'
  row=next(i for i in await core.request('model_instances') if i['id']==models['instance'])
  assert row['state']=='Unloaded' and row['held'] is False
  assert (await core.request('model_budget',id=r.budget))['reserved_tokens']==0
  assert (await core.request('host_resources'))['used']['cpu_millis']==0
  (models['path']/'drain.json').write_text(json.dumps({'instance':row,'events':await core.request('events'),'queue':await core.request('model_queue')},indent=2))
 finally:await adapter.close()

@pytest.mark.asyncio
async def test_missing_gateway_transport_never_falls_back_to_original_provider(models):
 item=models['items'][0];r,_,adapter,core=item
 try:
  patch=Path(json.loads(r.spec.read_text())['command'][-1]);entries=json.loads(patch.read_text())
  next(p for p in entries[-1]['insert'] if p['id']=='kane-model-budget')['config'].pop('gateway_socket')
  patch.write_text(json.dumps(entries))
  _,_,state=await run_task(item,prompt='Reply HELLO')
  assert state['state']=='Blocked'
  events=await core.request('events');assert not any(e['kind']=='model_gateway_dispatch' for e in events)
  assert not any(e['kind']=='llm_call_start' for e in events)
  assert (await core.request('model_budget',id=r.budget))['reserved_tokens']==16416
 finally:await adapter.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('models',[{'real':True}],indirect=True)
async def test_actual_model_reload_preserves_instance_identity_and_residency_accounting(models):
 item=models['items'][0];r,_,adapter,core=item
 try:
  before=next(i for i in await core.request('model_instances') if i['id']==models['instance'])
  bindings=(await core.request('runtime_status',id=r.agent))['agent']
  def stop():
   with socket.socket(socket.AF_UNIX) as s:
    s.settimeout(50);s.connect(str(r.core_socket.parent/f'{models["instance"]}.model.sock'));s.sendall(b'stop\n');return s.recv(100)
  assert await asyncio.to_thread(stop)==b'stopped\n'
  models['manager'].wait(timeout=5)
  assert (await core.request('host_resources'))['used']['cpu_millis']==0
  with (models['path']/'reload.log').open('w') as log:
   manager=subprocess.Popen([str(BIN),'--socket',str(r.core_socket),'model-run',str(models['path']/'model.json')],stderr=log)
   models['manager']=manager;models['processes'].append(manager)
   async def ready():return [i for i in await core.request('model_instances') if i['id']==models['instance'] and i['state']=='Ready']
   after=(await until(ready,seconds=60))[0]
   assert after['id']==before['id'] and after['generation']!=before['generation'] and after['pid']!=before['pid']
   assert (await core.request('runtime_status',id=r.agent))['agent']==bindings
   pool=await core.request('host_resources');assert pool['used']['cpu_millis']==1000 and pool['used']['storage_bytes']==128*1024**2
   await run_task(item,prompt='Reply with the single word HELLO. Do not call any tools.')
   assert (await core.request('model_budget',id=r.budget))['reserved_tokens']==0
   registry=await core.request('models');assert registry[0]['spec']['source']['backend_kind']=='real-local-llm'
   (models['path']/'reload.json').write_text(json.dumps({'before':before,'after':after,'pool':pool,'bindings':bindings,'registry':registry,'events':await core.request('events')},indent=2))
 finally:await adapter.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('models',[{'agents':3}],indirect=True)
async def test_private_task_model_scope_cannot_be_widened_to_other_native_agents(models):
 items=models['items'];core=items[0][3]
 try:
  private=models['policy']|{'sensitivity':'confidential','exclusive_task':items[0][0].task}
  await core.request('configure_model_gateway',id='kane-fixture/deterministic-v1',version=1,policy=private)
  await run_task(items[0],prompt='PRIVATE_SYNTHETIC_MARKER_A. Reply HELLO.')
  assert (await core.request('model_queue'))[0]['state']=='Completed'
  _,_,denied=await run_task(items[1],prompt='Request context from another Task.')
  assert denied['state']=='Blocked'
  await core.request('configure_model_gateway',id='kane-fixture/deterministic-v1',version=2,policy=models['policy'])
  _,_,pinned=await run_task(items[2],prompt='Try the same residency after a policy change.')
  assert pinned['state']=='Blocked'
  events=await core.request('events')
  assert sum(e['kind']=='model_gateway_dispatch' for e in events)==1
  for item in items[1:]:assert (await core.request('model_budget',id=item[0].budget))['reserved_tokens']==0
  (models['path']/'private-scope.json').write_text(json.dumps({'queue':await core.request('model_queue'),'events':events,'scope':'synthetic private fixture only; same generation remains pinned'},indent=2))
 finally:await asyncio.gather(*(x[2].close() for x in items))

@pytest.mark.asyncio
@pytest.mark.parametrize('models',[{'forge_owner_socket':True,'defer_agents':True}],indirect=True)
async def test_owner_socket_hardlink_cannot_be_mounted_as_model_gateway(models):
 item=models['items'][0];r=item[0]
 try:
  supervisor=r.start()
  await asyncio.to_thread(supervisor.wait,timeout=10)
  assert supervisor.returncode!=0
  events=await item[3].request('events')
  assert not any(e['kind']=='runtime_lifecycle' and e['object_id']==r.agent and e['payload']['detail'].get('event')=='started' for e in events)
  assert not r.socket.exists()
  (models['path']/'owner-socket-refused.json').write_text(json.dumps({'agent_id':r.agent,'supervisor_exit':supervisor.returncode,'events':events,'registered_listeners':await item[3].request('model_listeners')},indent=2))
 finally:await item[2].close()

@pytest.mark.asyncio
async def test_model_control_checks_kernel_uid_even_when_filesystem_permissions_widen(models):
 item=models['items'][0];r=item[0];control=r.core_socket.parent/f'{models["instance"]}.model.sock'
 try:
  r.path.chmod(0o755);control.chmod(0o666)
  client="const net=require('node:net');const s=net.createConnection('/test/"+control.name+"');let reply='';s.on('connect',()=>s.write('stop\\n'));s.on('data',b=>reply+=b);s.on('error',e=>{if(!['ECONNRESET','EPIPE'].includes(e.code)){console.error(e);process.exitCode=1}});s.on('close',()=>console.log(JSON.stringify({reply})));s.setTimeout(3000,()=>{process.exitCode=2;s.destroy()});"
  node=json.loads(r.spec.read_text())['command'][0]
  args=['docker','run','--rm','--network=none','--user','65534:65534','--read-only','--mount',f'type=bind,src={r.path},dst=/test,readonly','--mount',f'type=bind,src={node},dst=/node,readonly','kane-system-validation:20261018','/node','-e',client]
  result=await asyncio.to_thread(subprocess.run,args,capture_output=True,text=True,timeout=10)
  assert result.returncode==0,result.stderr
  assert json.loads(result.stdout)['reply']==''
  assert 'model control: local peer denied' in (models['path']/'model-runtime.log').read_text(), 'Kernel peer rejection must be observed, not inferred from EPIPE'
  row=next(i for i in await item[3].request('model_instances') if i['id']==models['instance']);assert row['state']=='Ready'
  (models['path']/'control-peer-denial.json').write_text(json.dumps({'uid':65534,'filesystem_checks_deliberately_widened':True,'reply':result.stdout,'state':row,'kernel_peer_check_denied':True},indent=2))
 finally:
  r.path.chmod(0o700)
  if control.exists():control.chmod(0o600)
  await item[2].close()
