"""Predeclared same-input/oracle/budget N=5 Direct versus actual collaboration.
All raw attempts preserved. Synthetic provider tokens never become a real bill.
"""
import asyncio,hashlib,json,os,time
from pathlib import Path
import pytest
from test_phase5_collaboration import agents,plan,coordinator,finish
from test_supervised_runtime import ROOT

SUITE=json.loads((ROOT/'bench/phase5/tasks.json').read_text())
pytestmark=pytest.mark.skipif(os.getenv('KANE_PHASE5_TEST')!='1',reason='opt-in actual Kanaloa fixed N5 collaboration comparison')


def cases():
    result=[]
    for task in SUITE['tasks']:
        for mode in SUITE['modes']:
            if mode=='CollaborationReduced' and len(task['markers'])<2:continue
            for repeat in range(5):
                markers=task['markers'];multi=mode!='Direct';reduced=mode=='CollaborationReduced';count=2 if reduced else (len(markers)+1 if multi else 1)
                configs=[]
                for i in range(count):
                    config={'sandbox_cpu_millis':1000//count,
                            'sandbox_memory_bytes':1024**3//count,'task_memory_bytes':1024**3//count,'tool_grant':False,
                            'task_goal':f"Read public data and deliver exactly {task['expected']}",
                            'task_acceptance':[SUITE['acceptance']]}
                    if reduced and i==0:
                        config['phase0_reads']=[{'path':'input.txt','marker':markers[1]}]
                        config['phase5_merge_context']=markers
                    elif multi and i==0:config['phase5_aggregate']=markers
                    else:
                        values=[markers[i-1]] if multi else markers
                        if multi:config['task_goal']='Read public input.txt and deliver its single marker '+markers[i-1]
                        config['phase0_reads']=[{'path':f'input{j+1 if j else ""}.txt','marker':m} for j,m in enumerate(values)]
                    configs.append(config)
                result.append(pytest.param({'count':count,'configs':configs,'benchmark':{'task':task,'mode':mode,'repeat':repeat}},id=f'{task["id"]}-{mode}-{repeat+1}'))
    return result


def cgroup_paths(item):
    import subprocess
    name='kane-'+item[0].agent
    obj=json.loads(subprocess.check_output(['docker','inspect',name]))[0]
    assert obj['State']['Running'] and obj['Config']['Labels']['kane.system.agent']==item[0].agent
    return name,json.loads(item[0].spec.read_text())["command"][0]


def read_resources(paths):
    import subprocess
    cpu=0;memory=0;raw=[]
    script="const f=require('fs');let o={};for(const n of ['cpu.stat','memory.peak','cpu.max','memory.max'])o[n]=f.readFileSync('/sys/fs/cgroup/'+n,'utf8');console.log(JSON.stringify(o));"
    for name,node in paths:
        data=json.loads(subprocess.check_output(['docker','exec',name,node,'-e',script],text=True))
        cpu+=int(next(x.split()[1] for x in data['cpu.stat'].splitlines() if x.startswith('usage_usec ')))
        memory+=int(data['memory.peak']);raw.append({'container':name,**data})
    return cpu,memory,raw


def host_counters():
    # Read-only whole-host cgroup counters include Core, coordinator and model
    # work. Per-Agent Docker probes are outside this timing/counter interval.
    stat=(Path('/sys/fs/cgroup')/'cpu.stat').read_text()
    return {'cpu_usec':int(next(line.split()[1] for line in stat.splitlines() if line.startswith('usage_usec '))),
            'memory_current':int(Path('/sys/fs/cgroup/memory.current').read_text()),'cpu.stat':stat}


async def check_outcome(item,expected):
    r,store,adapter,core=item;state=await finish(item);assert state['state']=='Verifying',state
    artifacts=await core.request('artifacts',id=r.task);assert len(artifacts)==1
    artifact=artifacts[0];assert artifact['content']==expected
    assert artifact['content_hash']==hashlib.sha256(expected.encode()).hexdigest() and artifact['editable']
    for entry in (r.home/'tool-invocations.jsonl').read_text().splitlines() if (r.home/'tool-invocations.jsonl').exists() else []:
        assert json.loads(entry)['tool']=='read'
    await core.request('verify',id=r.task,version=state['version'],passed=True,evidence='Fixed Phase5 external UTF-8 oracle and hash equality, source immutability checked by test')
    return artifact


@pytest.mark.asyncio
@pytest.mark.parametrize('agents',cases(),indirect=True)
async def test_fixed_n5_independent_comparison(agents,tmp_path,request):
    options=request.node.callspec.params['agents']['benchmark'];task=options['task'];mode=options['mode'];markers=task['markers']
    configuration={'suite':SUITE,'task':task};fingerprint=hashlib.sha256(json.dumps(configuration,sort_keys=True).encode()).hexdigest()
    root=agents[0][0].task;core=agents[0][3];c=coordinator(agents)
    p=plan(agents,limit=min(len(agents),2));p.update(scenario=task['id'],environment=fingerprint)
    record={'task':task['id'],'mode':mode,'repeat':options['repeat']+1,'environment':fingerprint,'mock':True,'verified_success':False,'attempt_complete':False,'failures':[],'limits':SUITE['fixed_constraints']}
    setup_start=time.perf_counter()
    await asyncio.gather(*(item[2].activate() for item in agents))
    record['acp_setup_ms']=(time.perf_counter()-setup_start)*1000
    paths=[cgroup_paths(item) for item in agents];cpu_before,_,before=read_resources(paths)
    coordination=0;host_before=host_counters();start=time.perf_counter()
    try:
        for i,item in enumerate(agents):
            values=markers if mode=='Direct' else ([markers[1]] if mode=='CollaborationReduced' and i==0 else ([] if i==0 else [markers[i-1]]))
            for j,marker in enumerate(values):
                name=f'input{j+1 if j else ""}.txt';(item[0].workspace/name).write_text(marker+'\n')
                item[0].call({'op':'grant_tool','id':item[0].agent,'tool':'read','arguments':{'file_path':name},'expires_at':int(time.time())+120})
        begin=time.perf_counter();await core.request('collaboration_create',plan=p);coordination+=time.perf_counter()-begin
        if mode!='Direct':
            await asyncio.gather(*(c.dispatch(x[0].task) for x in agents[1:]))
            artifacts=await asyncio.gather(*(check_outcome(x,'# Phase 0 fixture\n\n'+markers[i]+'\n') for i,x in enumerate(agents[1:])))
            refs=[];begin=time.perf_counter()
            for artifact in artifacts:
                context=(await core.request('collaboration_context',artifact_id=artifact['id'],expires_at=int(time.time())+120,sensitivity='public'))['id']
                await core.request('collaboration_share',id=context,agent_id=agents[0][0].agent,expires_at=int(time.time())+60,ceiling='public');refs.append(context)
            c.bindings[agents[0][0].agent].contexts=tuple(refs);coordination+=time.perf_counter()-begin
        await c.dispatch(root);artifact=await check_outcome(agents[0],task['expected'])
        assert artifact['content']==task['expected']  # identical oracle both modes
        for i,item in enumerate(agents):
            values=markers if mode=='Direct' else ([markers[1]] if mode=='CollaborationReduced' and i==0 else ([] if i==0 else [markers[i-1]]))
            for j,marker in enumerate(values):assert (item[0].workspace/f'input{j+1 if j else ""}.txt').read_text()==marker+'\n'
        record['verified_success']=True;record['quality_bps']=10000
    except BaseException as error:
        record['failures'].append(repr(error));record['quality_bps']=0
        raise
    finally:
        elapsed=(time.perf_counter()-start)*1000
        host_after=host_counters()
        cpu,peak,raw=read_resources(paths)
        rows=await core.request('model_queue');budgets=[await core.request('model_budget',id=x[0].budget) for x in agents]
        record.update(elapsed_ms=max(1,round(elapsed)),coordination_ms=round(coordination*1000),cpu_ms=max(0,(host_after['cpu_usec']-host_before['cpu_usec'])//1000),agent_cpu_ms_with_probe=max(0,(cpu-cpu_before)//1000),host_before=host_before,host_after=host_after,peak_memory_bytes=peak,actual_tokens=sum(b['used_tokens'] for b in budgets),model_calls=len(rows),queue=rows,budgets=budgets,kernel_before=before,kernel_after=raw,coordination_scope='Core plan+Context RPC time; whole-host CPU interval includes Core/Harness/model/oracle and excludes Docker resource probes; per-Agent CPU includes probe overhead separately',model_resource='in-process deterministic fixture, charged inside Agent cgroups; not a separate ModelInstance',duplicate_dispatches=0,attempt_complete=True)
        (tmp_path/'comparison.json').write_text(json.dumps(record,indent=2))
        if record['verified_success']:
            sample={'task_id':root,'scenario':task['id'],'environment':fingerprint,'mode':'Direct' if mode=='Direct' else 'Collaboration','elapsed_ms':record['elapsed_ms'],'coordination_ms':record['coordination_ms'],'cpu_ms':record['cpu_ms'],'peak_memory_bytes':peak,'quality_bps':10000,'evidence_hash':hashlib.sha256((tmp_path/'comparison.json').read_bytes()).hexdigest()}
            record['registered_sample']=await core.request('allocation_sample',sample=sample)
            (tmp_path/'comparison.json').write_text(json.dumps(record,indent=2))
        for item in agents:await item[2].close()


@pytest.fixture(scope='module',autouse=True)
def shared_benchmark_history(tmp_path_factory,request):
    if os.getenv('KANE_PHASE5_TEST')!='1':yield;return
    import subprocess,tempfile,socket
    from test_supervised_runtime import BIN,wait_for
    base=Path(tempfile.mkdtemp(prefix='p5-history-'));base.chmod(0o700)
    sock=base/'core.sock';log=(base/'core.log').open('w')
    process=subprocess.Popen([str(BIN),'--db',str(base/'core.db'),'--socket',str(sock),'serve'],stderr=log)
    def call(body):
        result=subprocess.run([str(BIN),'--socket',str(sock),'host-request'],input=json.dumps(body),text=True,capture_output=True,check=True)
        return json.loads(result.stdout)
    original=os.getenv('KANE_PHASE5_BENCHMARK_CORE')
    try:
        wait_for(sock.exists)
        model={'provider':'kane-fixture','model':'deterministic-v1','cloud':False,'capabilities':['read-public-text'],'scheduling':{'mode':'mock_fifo','backend_kind':'mock'},'budget_profile':{'provider':'kane-fixture','model':'deterministic-v1','upper_bound_tokens':32,'bound_evidence':'original deterministic fixed read+reply, maximum27 tokens per call'}}
        call({'op':'register_model','id':'kane-fixture/deterministic-v1','spec':model})
        os.environ['KANE_PHASE5_BENCHMARK_CORE']=str(sock)
        yield
        decisions=[]
        for task in SUITE['tasks']:
            fingerprint=hashlib.sha256(json.dumps({'suite':SUITE,'task':task},sort_keys=True).encode()).hexdigest()
            decisions.append(call({'op':'allocation_decide','scenario':task['id'],'environment':fingerprint,'max_agents':3}))
        output=tmp_path_factory.getbasetemp()/'phase5-history';output.mkdir()
        (output/'decisions.json').write_text(json.dumps({'decisions':decisions,'events':call({'op':'events'}),'model_queue':call({'op':'model_queue'}),'retained_db':str(base/'core.db')},indent=2))
        assert all(d['mode']=='Direct' or any(x['delta_u']>0.05 and x['mode']==d['mode'] and x['agents']==d['max_agents'] for x in d['comparisons']) for d in decisions),decisions
        expected={t['id']:0 for t in SUITE['tasks']}
        for item in request.session.items:
            config=getattr(getattr(item,'callspec',None),'params',{}).get('agents',{})
            if config.get('benchmark'):expected[config['benchmark']['task']['id']]+=1
        assert [len(d['evidence']) for d in decisions]==[expected[t['id']] for t in SUITE['tasks']],decisions
    finally:
        if original is None:os.environ.pop('KANE_PHASE5_BENCHMARK_CORE',None)
        else:os.environ['KANE_PHASE5_BENCHMARK_CORE']=original
        process.terminate();process.wait(timeout=10);log.close()
