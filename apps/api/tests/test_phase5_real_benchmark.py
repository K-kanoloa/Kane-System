"""Real local CPU model N=5. A pytest PASS validates measurement/safety, not Task success.
Read real-comparison.json verified_success; all failed/Unknown outcomes are retained.
"""
import asyncio,hashlib,json,os,time
from pathlib import Path
import pytest
from test_phase2_models import models,finish
from test_phase5_collaboration import plan,coordinator,FINGERPRINT
from test_phase5_benchmark import cgroup_paths,read_resources,host_counters
from test_supervised_runtime import ROOT

SPEC=json.loads((ROOT/'bench/phase5/real-protocol.json').read_text())
GOAL=SPEC['task']
pytestmark=pytest.mark.skipif(os.getenv('KANE_PHASE5_TEST')!='1',reason='opt-in real local CPU N5 comparison')


def configurations():
    return [pytest.param({'agents':1 if mode=='Direct' else 2,'real':True,'read_task':True,
            'context_tokens':16384,'task_goal':GOAL,'task_acceptance':[SPEC['acceptance']],'output_tokens':512,
            'model_file':os.getenv('KANE_PHASE2_TOOL_MODEL_FILE','/tmp/kane-phase2-final-deps/Qwen3-0.6B-Q8_0.gguf'),
            'backend_args':['--chat-template-kwargs','{"enable_thinking":false}','--temp','0','--seed','0'],
            'agent_cpu_millis':1000 if mode=='Direct' else 500,
            'agent_memory_bytes':1024**3 if mode=='Direct' else 512*1024**2,
            'phase5_mode':mode,'phase5_repeat':repeat+1},id=f'{mode}-{repeat+1}')
        for mode in SPEC['modes'] for repeat in range(5)]


@pytest.mark.asyncio
@pytest.mark.parametrize('models',configurations(),indirect=True)
async def test_real_local_fixed_outcome_comparison(models,tmp_path,request):
    options=request.node.callspec.params['models'];items=models['items'];core=items[0][3];mode=options['phase5_mode'];root=items[0][0].task;c=coordinator(items)
    record={'protocol':SPEC,'mode':mode,'repeat':options['phase5_repeat'],'real_model':True,'verified_success':False,'failures':[],'quality_bps':0,'all_attempts_retained':True,'invoice':'UNKNOWN, local CPU inference'}
    for item in items:
        (item[0].workspace/'input.txt').write_text(SPEC['data'])
        item[0].call({'op':'collaboration_harness_profile','id':item[0].agent,'capabilities':['text-inference'],'evidence_hash':FINGERPRINT,'expires_at':int(time.time())+3600})
    await asyncio.gather(*(item[2].activate() for item in items))
    paths=[cgroup_paths(item) for item in items];cpu_before,_,before=read_resources(paths)
    instance=next(i for i in await core.request('model_instances') if i['id']==models['instance']);pid=instance['pid']
    def model_resources():
        stat=Path(f'/proc/{pid}/stat').read_text().split(') ',1)[1].split()
        status=Path(f'/proc/{pid}/status').read_text()
        return {'cpu_ticks':int(stat[11])+int(stat[12]),'status':status}
    mb=model_resources();host_before=host_counters();start=time.perf_counter();coordination=0
    async def check(item):
        state=await finish(item,seconds=180)
        if state['state']!='Verifying':return None
        artifacts=await core.request('artifacts',id=item[0].task)
        if len(artifacts)!=1:return None
        a=artifacts[0];assert a['content_hash']==hashlib.sha256(a['content'].encode()).hexdigest()
        events=await core.request('events')
        source_read=any(e['kind']=='result' and e['payload'].get('receipt',{}).get('outcome')=='completed' for e in events)
        good=SPEC['data'].strip() in a['content'] and source_read and all((x[0].workspace/'input.txt').read_text()==SPEC['data'] for x in items)
        await core.request('verify',id=item[0].task,version=state['version'],passed=good,evidence='Independent real CPU marker/source/read/hash oracle; negative means actual model did not meet fixed acceptance')
        return a if good else None
    try:
        p=plan(items,limit=len(items));p.update(scenario=SPEC['id'],environment=hashlib.sha256(json.dumps(SPEC,sort_keys=True).encode()).hexdigest())
        for n in p['nodes']:n['requires']=['text-inference']
        begin=time.perf_counter();await core.request('collaboration_create',plan=p);coordination+=time.perf_counter()-begin
        child_good=True
        if len(items)>1:
            await c.dispatch(items[1][0].task);child=await check(items[1]);child_good=child is not None
            if child_good:
                begin=time.perf_counter();ctx=(await core.request('collaboration_context',artifact_id=child['id'],expires_at=int(time.time())+900,sensitivity='public'))['id']
                await core.request('collaboration_share',id=ctx,agent_id=items[0][0].agent,expires_at=int(time.time())+600,ceiling='public');c.bindings[items[0][0].agent].contexts=(ctx,);coordination+=time.perf_counter()-begin
        if child_good:
            await c.dispatch(root);result=await check(items[0]);record['verified_success']=result is not None
            record['quality_bps']=10000 if record['verified_success'] else 0
        if not record['verified_success']:record['failures'].append('Actual model did not produce a verified acceptable outcome; no automatic retry')
    except (TimeoutError,ValueError,RuntimeError) as error:
        record['failures'].append(type(error).__name__+': '+str(error))
    finally:
        elapsed_ms=max(1,round((time.perf_counter()-start)*1000))
        host_after=host_counters()
        cpu,peak,after=read_resources(paths);ma=model_resources()
        record.update(elapsed_ms=elapsed_ms,host_cpu_ms=max(0,(host_after['cpu_usec']-host_before['cpu_usec'])//1000),host_before=host_before,host_after=host_after,coordination_ms=round(coordination*1000),agent_cpu_ms=max(0,(cpu-cpu_before)//1000),agent_peak_memory_bytes=peak,kernel_before=before,kernel_after=after,model_before=mb,model_after=ma,model_cpu_ms=(ma['cpu_ticks']-mb['cpu_ticks'])*1000//os.sysconf('SC_CLK_TCK'),model_limit_profile='existing diagnostic local model backend; kernel model limits covered separately by Phase2 strict regression')
        state=await core.request('task',id=root)
        if not record['verified_success'] and state['state'] not in {'Done','Failed','Cancelled','ClosedUnverified'}:
            await core.request('cancel_task',id=root,reason='End fixed measurement without refund or retry of unknown effects')
        budgets=[await core.request('model_budget',id=x[0].budget) for x in items];rows=await core.request('model_queue')
        record.update(budgets=budgets,actual_tokens=sum(b['used_tokens'] for b in budgets),held_tokens=sum(b['reserved_tokens'] for b in budgets),model_calls=len(rows),model_queue=rows,tasks=[await core.request('task',id=x[0].task) for x in items],artifacts=[await core.request('artifacts',id=x[0].task) for x in items],events=await core.request('events'))
        assert record['verified_success']==(record['tasks'][0]['state']=='Done')
        assert all(b['reserved_tokens']>=0 and b['used_tokens']>=0 for b in budgets)
        (tmp_path/'real-comparison.json').write_text(json.dumps(record,indent=2))
        for item in items:await item[2].close()
