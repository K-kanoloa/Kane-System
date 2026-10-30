"""Predeclared real-model outcomes, one authoritative history, no scored fakes.

A measurement PASS is not a successful Task: inspect verified_success and the
actual allocation decision. No failed outcome is replaced or retried.
"""
import asyncio,hashlib,json,os,time
from pathlib import Path
from types import SimpleNamespace
import pytest
from test_phase2_models import models,finish
from test_phase5_collaboration import plan,coordinator,FINGERPRINT
from test_phase5_benchmark import cgroup_paths,read_resources,host_counters
from test_supervised_runtime import ROOT,wait_for

SPEC=json.loads((ROOT/'bench/phase5/parallel-protocol.json').read_text())
ORDER=[(x.split('-')[0],int(x.split('-')[1])) for x in SPEC['order'].split(',')]
PROFILE=json.loads(Path(os.environ['KANE_PHASE5_PARALLEL_PROFILE']).read_text()) if os.getenv('KANE_PHASE5_PARALLEL_PROFILE') else {'environment':'not separately characterized'}
ENV=hashlib.sha256(json.dumps({'protocol':SPEC,'environment':PROFILE},sort_keys=True).encode()).hexdigest()
pytestmark=pytest.mark.skipif(os.getenv('KANE_PHASE5_PARALLEL_TEST')!='1',reason='opt-in fixed real independent-document history acceptance')


@pytest.fixture(scope='module')
def history(tmp_path_factory):
    if os.getenv('KANE_PHASE5_PARALLEL_TEST')!='1':yield None;return
    base=tmp_path_factory.mktemp('phase5-parallel-history');mp=pytest.MonkeyPatch();configs=[];groups=[]
    for mode,repeat in ORDER:
        first=len(configs);count=1 if mode=='Direct' else 3
        for i in range(count):
            goal=SPEC['direct_goal'] if mode=='Direct' else SPEC['aggregate_goal'] if i==0 else SPEC['reader_goal']
            configs.append({'task_goal':goal,'task_acceptance':[SPEC['acceptance']],
                            'sandbox_cpu_millis':1000//count,'sandbox_memory_bytes':1024**3//count,'task_memory_bytes':1024**3//count})
        groups.append((mode,repeat,first,count))
    options={'agents':len(configs),'agent_configs':configs,'defer_agents':True,'real':True,'read_task':True,
             'context_tokens':8192,'parallelism':2,'output_tokens':512,'budget_tokens':100000,
             'model_file':os.environ['KANE_PHASE2_TOOL_MODEL_FILE'],
             'backend_args':['--chat-template-kwargs','{"enable_thinking":false}','--temp','0','--seed','0']}
    generator=models.__wrapped__(base,mp,SimpleNamespace(param=options));data=next(generator)
    core=data['items'][0][3];records=[]
    try:
        cases={}
        for mode,repeat,first,count in groups:
            items=data['items'][first:first+count]
            for i,it in enumerate(items):
                content=''.join(SPEC['documents']) if mode=='Direct' else 'No new source; use authorized Context.\n' if i==0 else SPEC['documents'][i-1]
                (it[0].workspace/'input.txt').write_text(content)
                it[0].call({'op':'collaboration_harness_profile','id':it[0].agent,'capabilities':['text-inference'],'evidence_hash':FINGERPRINT,'expires_at':int(time.time())+7200})
            p=plan(items,limit=count);p.update(scenario=SPEC['id'],environment=ENV)
            for n in p['nodes']:n['requires']=['text-inference']
            items[0][0].call({'op':'collaboration_create','plan':p})
            cases[(mode,repeat)]=items
        (base/'predeclared.json').write_text(json.dumps({'protocol':SPEC,'environment_profile':PROFILE,'environment':ENV,'groups':groups,'runtime':data['runtime'],'plans':[data['items'][first][0].call({'op':'collaboration_status','id':data['items'][first][0].task}) for _,_,first,_ in groups]},indent=2))
        yield {'data':data,'base':base,'core':core,'cases':cases,'records':records}
        # These samples were registered from actual independently checked Tasks
        # in this same database. No SQL copying or synthetic historical scores.
        decision=data['items'][0][0].call({'op':'allocation_decide','scenario':SPEC['id'],'environment':ENV,'max_agents':3})
        limited=data['items'][0][0].call({'op':'allocation_decide','scenario':SPEC['id'],'environment':ENV,'max_agents':1})
        report={'decision':decision,'owner_one_agent_decision':limited,'attempts':records,'events':data['items'][0][0].call({'op':'events'}),'retained_db':str(data['items'][0][0].path/'core.db'),'environment':ENV,'all_attempts_retained':True}
        (base/'allocation-history.json').write_text(json.dumps(report,indent=2))
        assert len(records)==10 and limited['mode']=='Direct'
        assert len(decision['evidence'])+decision['ungraded_attempts']==10
        if decision['mode']!='Direct':
            assert decision['ungraded_attempts']==0 and any(x['delta_u']>0.05 and x['agents']==decision['max_agents'] for x in decision['comparisons'])
    finally:
        try:next(generator)
        except StopIteration:pass
        mp.undo()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,repeat',ORDER,ids=[f'{m}-{n}' for m,n in ORDER])
async def test_real_parallel_documents_score_into_one_core(history,tmp_path,mode,repeat):
    items=history['cases'][(mode,repeat)];core=history['core'];root=items[0][0].task;c=coordinator(items)
    record={'mode':mode,'repeat':repeat,'scenario':SPEC['id'],'environment':ENV,'verified_success':False,'quality_bps':0,'failures':[],'model':'actual public local CPU Qwen3; no Mock','sample_registered':False}
    sources={it[0].agent:(it[0].workspace/'input.txt').read_bytes() for it in items}
    setup=time.perf_counter()
    for it in items:
        # Each predeclared case receives the same bounded Owner authorization
        # window at its own start; later repeats must not inherit expired grants.
        it[0].call({'op':'grant_tool','id':it[0].agent,'tool':'read','arguments':{'file_path':'input.txt'},'expires_at':int(time.time())+1200})
        it[0].start();wait_for(it[0].status)
    await asyncio.gather(*(it[2].activate() for it in items))
    record['setup_ms']=round((time.perf_counter()-setup)*1000)
    paths=[cgroup_paths(x) for x in items];cpu0,_,kernel0=read_resources(paths)
    assert sum(int(x['cpu.max'].split()[0])/int(x['cpu.max'].split()[1])*1000 for x in kernel0)<=1000.001
    assert sum(int(x['memory.max']) for x in kernel0)<=1024**3
    host0=host_counters();start=time.perf_counter();coordination=0
    async def verify(it,identifiers,requires_read):
        state=await finish(it,seconds=180)
        if state['state']!='Verifying':return None
        artifacts=await core.request('artifacts',id=it[0].task)
        events=await core.request('events')
        read=any(e['object_id']==it[0].task and e['kind']=='result' and e['payload'].get('receipt',{}).get('outcome')=='completed' for e in events)
        artifact=artifacts[0] if len(artifacts)==1 else None
        passed=bool(artifact and artifact['editable'] and all(x in artifact['content'] for x in identifiers)
                    and artifact['content_hash']==hashlib.sha256(artifact['content'].encode()).hexdigest()
                    and (not requires_read or read) and all((x[0].workspace/'input.txt').read_bytes()==sources[x[0].agent] for x in items))
        result=await core.request('verify',id=it[0].task,version=state['version'],passed=passed,evidence='Independent fixed identifiers + editable content SHA256 + task-bound controlled read + immutable original input; same root standard in both modes')
        return artifact if passed and result['state']=='Done' else None
    try:
        children=True
        if mode=='Collaboration':
            begin=time.perf_counter();await asyncio.gather(*(c.dispatch(x[0].task) for x in items[1:]));coordination+=time.perf_counter()-begin
            outputs=await asyncio.gather(*(verify(x,[SPEC['documents'][i].strip()],True) for i,x in enumerate(items[1:])))
            children=all(outputs)
            if children:
                refs=[];begin=time.perf_counter()
                for artifact in outputs:
                    context=(await core.request('collaboration_context',artifact_id=artifact['id'],expires_at=int(time.time())+1200,sensitivity='public'))['id']
                    await core.request('collaboration_share',id=context,agent_id=items[0][0].agent,expires_at=int(time.time())+900,ceiling='public');refs.append(context)
                c.bindings[items[0][0].agent].contexts=tuple(refs);coordination+=time.perf_counter()-begin
        if children:
            begin=time.perf_counter();await c.dispatch(root);coordination+=time.perf_counter()-begin
            artifact=await verify(items[0],SPEC['identifiers'],mode=='Direct')
            record['verified_success']=artifact is not None;record['quality_bps']=10000 if artifact else 0
        if not record['verified_success']:record['failures'].append('Fixed independent oracle not satisfied; no replacement run')
    except (OSError,TimeoutError,ValueError,RuntimeError) as e:record['failures'].append(type(e).__name__+': '+str(e))
    finally:
        host1=host_counters();elapsed=max(1,round((time.perf_counter()-start)*1000))
        cpu,peak,kernel=read_resources(paths)
        record.update(elapsed_ms=elapsed,coordination_ms=round(coordination*1000),host_cpu_ms=max(0,(host1['cpu_usec']-host0['cpu_usec'])//1000),agent_cpu_ms_with_probe=max(0,(cpu-cpu0)//1000),agent_peak_memory_bytes=peak,kernel_before=kernel0,kernel_after=kernel,host_before=host0,host_after=host1)
        state=await core.request('task',id=root)
        if not record['verified_success'] and state['state'] not in {'Done','Failed','Cancelled','ClosedUnverified'}:
            await core.request('cancel_task',id=root,reason='End predeclared independent measurement; do not refund/replay Unknown')
        budgets=[await core.request('model_budget',id=x[0].budget) for x in items]
        ids={x[0].task for x in items};queue=[q for q in await core.request('model_queue') if q['task_id'] in ids]
        record.update(task_ids=sorted(ids),actual_tokens=sum(b['used_tokens'] for b in budgets),held_tokens=sum(b['reserved_tokens'] for b in budgets),budgets=budgets,model_queue=queue,model_calls=len(queue),tasks=[await core.request('task',id=x[0].task) for x in items],artifacts=[await core.request('artifacts',id=x[0].task) for x in items],instance=history['data']['instance'])
        assert record['verified_success']==(record['tasks'][0]['state']=='Done')
        assert record['actual_tokens']+record['held_tokens']<=100000
        (tmp_path/'measurement.json').write_text(json.dumps(record,indent=2))
        (tmp_path/'parallel-comparison.json').write_text(json.dumps(record,indent=2))
        evidence=hashlib.sha256((tmp_path/'measurement.json').read_bytes()).hexdigest()
        record['sealed_measurement_sha256']=evidence
        sample={'task_id':root,'scenario':SPEC['id'],'environment':ENV,'mode':mode,'elapsed_ms':elapsed,'coordination_ms':record['coordination_ms'],'cpu_ms':record['host_cpu_ms'],'peak_memory_bytes':peak,'quality_bps':record['quality_bps'],'evidence_hash':evidence}
        try:
            record['registered_sample']=await core.request('allocation_sample',sample=sample);record['sample_registered']=True
        except ValueError as e:record['grading_blocked']=str(e) # Unknown remains ungraded and blocks policy promotion.
        record['decision_after_attempt']=await core.request('allocation_decide',scenario=SPEC['id'],environment=ENV,max_agents=3)
        record['events']=[e for e in await core.request('events') if e['object_id'] in ids]
        (tmp_path/'parallel-comparison.json').write_text(json.dumps(record,indent=2));history['records'].append(record)
        for it in items:await it[2].close()
