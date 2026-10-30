"""Actual original Kanaloa/DSH + Rust shared FIFO, no new Agent implementation.
Deterministic model evidence cannot prove real-model collaboration quality.
"""
import asyncio, hashlib, json, os, signal, time
from pathlib import Path
from types import SimpleNamespace
import pytest
from test_system_execution import execution, run_task
from test_model_fifo import until, invocations, restart_core
from app.system.client import CoreRejected
from app.collaboration.coordinator import CollaborationCoordinator, HarnessBinding

pytestmark=pytest.mark.skipif(os.getenv('KANE_PHASE5_TEST')!='1',reason='opt-in Phase5 actual Kanaloa/Docker collaboration')
FINGERPRINT=hashlib.sha256(b'phase5-fixed-public-marker-v1:mock:10000tokens:sharedFIFO:byteoracle').hexdigest()

@pytest.fixture
def agents(tmp_path,monkeypatch,request):
    options=getattr(request,'param',{});generators=[];items=[]
    try:
        for i in range(options.get('count',3)):
            path=tmp_path/str(i);path.mkdir()
            config={'sandbox':True,'shared_fifo':True,'delay_ms':options.get('delay_ms',30),
                    'phase2_model_spec':{'provider':'kane-fixture','model':'deterministic-v1','cloud':False,
                       'capabilities':['read-public-text'],'scheduling':{'mode':'mock_fifo','backend_kind':'mock'},
                       'budget_profile':{'provider':'kane-fixture','model':'deterministic-v1','upper_bound_tokens':32,'bound_evidence':'original deterministic fixed read+reply, maximum27 tokens per call'}},
                    'budget_tokens':options.get('root_tokens',10000) if i==0 else 10000,
                    'task_goal':'Read input.txt with the original read tool and deliver exact KANE_PR2_FIXTURE Markdown',
                    'task_acceptance':['Exact UTF-8 Markdown equality, source unchanged, independent hash and verifier']}
            if i in options.get('fail_agents',[]):config['mode']='throw_after_usage'
            if options.get('benchmark') and os.getenv('KANE_PHASE5_BENCHMARK_CORE'):config['core_socket']=os.environ['KANE_PHASE5_BENCHMARK_CORE']
            if items:config['core_socket']=str(items[0][0].core_socket)
            config|=options.get('configs',[{}]*options.get('count',3))[i]
            generator=execution.__wrapped__(path,monkeypatch,SimpleNamespace(param=config))
            generators.append(generator);items.append(next(generator))
            item=items[-1]
            item[0].call({'op':'collaboration_harness_profile','id':item[0].agent,'capabilities':['read-public-text'],'evidence_hash':hashlib.sha256(b'Phase1 inherited ACP and original read; Phase5 integration rechecks actual execution').hexdigest(),'expires_at':int(time.time())+300})
        yield items
    finally:
        if items and items[0][0].core is not None and items[0][0].core.poll() is not None:restart_core(items[0][0])
        for item in items:
            try:item[0].control('stop')
            except OSError:pass
        for generator in reversed(generators):
            try:next(generator)
            except StopIteration:pass

def plan(items,limit=2):
    root=items[0][0].task
    return {'root':root,'scenario':'public-read','environment':FINGERPRINT,'max_agents':limit,
            'experimental':True,'reason':'Owner-authorized controlled comparison; no assumed net benefit',
            'nodes':[{'task_id':r.task,'agent_id':r.agent,'role':'independent public read' if i else 'aggregate after child verification',
                      'requires':['read-public-text'],'depends_on':[x[0].task for x in items[1:]] if i==0 else []}
                     for i,(r,*_) in enumerate(items)]}

def coordinator(items):
    return CollaborationCoordinator(items[0][3],{x[0].agent:HarnessBinding(x[2],x[1],'kanaloa') for x in items})

async def finish(item):
    async def status():
        t=await item[3].request('task',id=item[0].task)
        return t if t['state'] in {'Verifying','Blocked','Failed'} else None
    return await until(status,seconds=40)

async def verify(item):
    r,store,adapter,core=item;state=await finish(item)
    assert state['state']=='Verifying',state
    artifacts=await core.request('artifacts',id=r.task)
    expected='# Verified fixture\n\nKANE_PR2_FIXTURE\n'
    assert len(artifacts)==1 and artifacts[0]['content']==expected
    assert artifacts[0]['content_hash']==hashlib.sha256(expected.encode()).hexdigest()
    assert (r.workspace/'input.txt').read_text()=='KANE_PR2_FIXTURE\n'
    assert len([x for x in (r.home/'tool-invocations.jsonl').read_text().splitlines() if json.loads(x)['tool']=='read'])==1
    done=await core.request('verify',id=r.task,version=state['version'],passed=True,evidence='Independent fixture byte equality, input immutability, original read trace, and SHA256')
    assert done['state']=='Done';return artifacts[0]

@pytest.mark.asyncio
async def test_actual_dag_context_dynamic_policy_and_independent_done(agents,tmp_path):
    core=agents[0][3];root=agents[0][0].task;c=coordinator(agents)
    route=await core.request('allocation_route',id=agents[1][0].task,requires=['read-public-text']);assert route['agent_id']==agents[1][0].agent
    with pytest.raises(CoreRejected):await core.request('allocation_route',id=agents[1][0].task,requires=['unverified-video-generation'])
    await core.request('collaboration_create',plan=plan(agents))
    with pytest.raises(CoreRejected):await c.dispatch(root)
    status=await core.request('collaboration_policy',id=root,version=0,max_agents=1,paused=False,reason='Start smallest sufficient experiment')
    await c.dispatch(agents[1][0].task)
    with pytest.raises(CoreRejected):await c.dispatch(agents[2][0].task)
    await core.request('collaboration_policy',id=root,version=status['version'],max_agents=2,paused=False,reason='Independent second public fragment in Owner experiment')
    await c.dispatch(agents[2][0].task)
    artifacts=await asyncio.gather(*(verify(x) for x in agents[1:]))
    stopped=await c.release_completed(root);assert set(stopped)=={x[0].agent for x in agents[1:]}
    assert all(x[0].call({'op':'runtime_status','id':x[0].agent})['agent']['state']=='Suspended' for x in agents[1:])
    refs=[]
    for artifact in artifacts:
        ctx=(await core.request('collaboration_context',artifact_id=artifact['id'],expires_at=int(time.time())+120,sensitivity='public'))['id']
        await core.request('collaboration_share',id=ctx,agent_id=agents[0][0].agent,expires_at=int(time.time())+60,ceiling='public');refs.append(ctx)
    c.bindings[agents[0][0].agent].contexts=tuple(refs)
    await core.request('collaboration_policy',id=root,version=2,max_agents=1,paused=False,reason='Release completed child claims; minimum aggregate')
    turn=await c.dispatch(root);await verify(agents[0])
    messages=agents[0][1].get_messages(turn.conversation_id)
    assert all(any(ctx in m.content for m in messages if m.sender=='user') for ctx in refs)
    events=await core.request('events');rows=await core.request('model_queue')
    assert len(rows)==6 and all(r['state']=='Completed' for r in rows)
    assert len({r['agent_id'] for r in rows})==3
    assert len([e for e in events if e['kind']=='dag_claim'])==3
    (tmp_path/'collaboration-result.json').write_text(json.dumps({'status':await core.request('collaboration_status',id=root),'contexts':refs,'queue':rows,'events':events,'model':'deterministic mock','quality_scope':'fixed public fixture only'},indent=2))
    for item in agents:await item[2].close()

@pytest.mark.asyncio
@pytest.mark.parametrize('agents',[{'delay_ms':1500,'fail_agents':[1]}],indirect=True)
async def test_unknown_child_is_not_reassigned_refunded_or_hidden_by_cancel(agents,tmp_path):
    core=agents[0][3];c=coordinator(agents);root=agents[0][0].task
    await core.request('collaboration_create',plan=plan(agents))
    await c.dispatch(agents[1][0].task)
    state=await finish(agents[1]);assert state['state']=='Blocked'
    b=await core.request('model_budget',id=agents[1][0].budget);assert b['reserved_tokens']==32
    with pytest.raises(CoreRejected):await c.dispatch(root)
    result=await c.cancel(root,'Fault experiment stop; preserve Unknown')
    assert result['state']=='Blocked'
    assert (await core.request('model_budget',id=agents[1][0].budget))['reserved_tokens']==32
    assert len(invocations(agents[1]))==1
    assert (await core.request('task',id=agents[2][0].task))['state']=='Cancelled'
    (tmp_path/'unknown-cancellation.json').write_text(json.dumps({'parent':result,'budget':b,'events':await core.request('events')},indent=2))

@pytest.mark.asyncio
@pytest.mark.parametrize('agents',[{'root_tokens':31}],indirect=True)
async def test_group_budget_is_checked_before_any_provider_entry(agents):
    c=coordinator(agents);core=agents[0][3]
    await core.request('collaboration_create',plan=plan(agents))
    await c.dispatch(agents[1][0].task)
    state=await finish(agents[1]);assert state['state']=='Blocked'
    assert not invocations(agents[1])
    await c.cancel(agents[0][0].task,'No group quota')

@pytest.mark.asyncio
async def test_pause_persists_across_core_restart_and_unstarted_work_resumes(agents,tmp_path):
    c=coordinator(agents);core=agents[0][3];root=agents[0][0].task
    await core.request('collaboration_create',plan=plan(agents))
    await c.pause(root,'Prepare actual Owner takeover; fence admission first')
    with pytest.raises(CoreRejected):await c.dispatch(agents[1][0].task)
    r=agents[0][0];r.core.kill();r.core.wait();restart_core(r)
    state=await core.request('collaboration_status',id=root);assert state['paused']
    with pytest.raises(CoreRejected):await c.dispatch(agents[1][0].task)
    await core.request('collaboration_policy',id=root,version=state['version'],max_agents=1,paused=False,reason='Owner reviewed persistent state; no work dispatched')
    await c.dispatch(agents[1][0].task);await verify(agents[1])
    (tmp_path/'persistent-pause.json').write_text(json.dumps({'events':await core.request('events')},indent=2))
    await c.cancel(root,'Stop rest of independent experiment')

@pytest.mark.asyncio
@pytest.mark.parametrize('lost',[False,True],ids=['confirmed-no-replay','lost-result-unknown'])
@pytest.mark.parametrize('agents',[{'delay_ms':1200}],indirect=True)
async def test_mcp_discovery_capability_durable_call_and_result_loss(agents,tmp_path,lost):
    import sys
    from test_supervised_runtime import ROOT
    from app.collaboration.mcp import McpStdio
    item=agents[1];r,store,adapter,core=item;c=coordinator(agents)
    await core.request('collaboration_create',plan=plan(agents));turn=await c.dispatch(r.task)
    # send() admits synchronously; ACP session remains original Kanaloa's.
    native=store.get_turn(turn.turn_id).native_session_ref
    proxy=McpStdio([sys.executable,str(ROOT/'bench/phase5/mcp-reference.py')]+(['--lose-result'] if lost else []))
    try:
        discovered=await proxy.start();assert list(proxy.tools)==['public_marker']
        args={'resource':'public-marker'}
        with pytest.raises(CoreRejected):await proxy.call(core,turn.turn_id,native,'mcp-case','public_marker',args)
        assert proxy.counter==2
        cap=r.call({'op':'grant_tool','id':r.agent,'tool':'mcp','arguments':{'server_sha256':proxy.server_sha256,'tool':'public_marker','arguments':args},'expires_at':int(time.time())+60})['id']
        if lost:
            with pytest.raises(CoreRejected):await proxy.call(core,turn.turn_id,native,'mcp-case','public_marker',args)
            assert (await core.request('task',id=r.task))['state']=='Blocked'
        else:
            result=await proxy.call(core,turn.turn_id,native,'mcp-case','public_marker',args)
            assert result['content'][0]['text']=='KANE_PUBLIC_MCP_MARKER'
        before=proxy.counter
        with pytest.raises(CoreRejected):await proxy.call(core,turn.turn_id,native,'mcp-case','public_marker',args)
        assert proxy.counter==before
        r.call({'op':'revoke_capability','id':cap})
        with pytest.raises(CoreRejected):await proxy.call(core,turn.turn_id,native,'new-after-revoke','public_marker',args)
        assert proxy.counter==before
        events=await core.request('events')
        (tmp_path/'mcp-compatibility.json').write_text(json.dumps({'discovery':discovered,'lost_result':lost,'rpc_count':proxy.counter,'events':events,'scope':'actual local MCP stdio reference peer, not third-party production server or A2A'},indent=2))
    finally:
        await proxy.close();await c.cancel(agents[0][0].task,'MCP experiment finished')

@pytest.mark.asyncio
@pytest.mark.parametrize('agents',[{'delay_ms':1800}],indirect=True)
async def test_a2a_actual_authenticated_https_exact_grants_no_self_verified_success(agents,tmp_path):
    import socket,subprocess,sys,ssl,httpx
    from test_supervised_runtime import ROOT
    from app.collaboration.a2a import A2AReference
    with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
    cert=tmp_path/'cert.pem';key=tmp_path/'key.pem'
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    process=subprocess.Popen([sys.executable,str(ROOT/'bench/phase5/a2a-reference.py'),'--port',str(port),'--cert',str(cert),'--key',str(key)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    peer=A2AReference(f'https://localhost:{port}',str(cert),'ephemeral-public-reference');core=agents[0][3];c=coordinator(agents);r=agents[1][0]
    try:
        for _ in range(50):
            try:capabilities=await peer.discover();break
            except httpx.ConnectError:await asyncio.sleep(.05)
        else:raise AssertionError('HTTPS reference peer not ready')
        async with httpx.AsyncClient(verify=ssl.create_default_context(cafile=str(cert)),trust_env=False) as unauth:
            assert (await unauth.get(f'https://localhost:{port}/.well-known/agent-card.json')).status_code==403
        async with httpx.AsyncClient(trust_env=False) as untrusted:
            with pytest.raises(httpx.ConnectError):await untrusted.get(f'https://localhost:{port}/.well-known/agent-card.json')
        await core.request('collaboration_create',plan=plan(agents));turn=await c.dispatch(r.task);native=agents[1][1].get_turn(turn.turn_id).native_session_ref
        params={'message':{'kind':'message','messageId':'public-reference-message','role':'user','parts':[{'kind':'text','text':'Read fixed public marker'}]}}
        with pytest.raises(CoreRejected):await peer.call(core,turn.turn_id,native,'a2a-reference','message/send',params)
        r.call({'op':'grant_tool','id':r.agent,'tool':'a2a','arguments':{'server_sha256':peer.server_sha256,'tool':'message/send','arguments':params},'expires_at':int(time.time())+60})
        result=await peer.call(core,turn.turn_id,native,'a2a-reference','message/send',params)
        assert result['status']['state']=='completed'
        assert (await core.request('task',id=r.task))['state']!='Done'
        with pytest.raises(CoreRejected):await peer.call(core,turn.turn_id,native,'a2a-reference','message/send',params)
        with pytest.raises(CoreRejected):await peer.call(core,turn.turn_id,native,'unsupported','steer',params)
        assert peer.counter==1
        (tmp_path/'a2a-compatibility.json').write_text(json.dumps({'peer':capabilities,'result':result,'events':await core.request('events'),'tls':'normal certificate/hostname validation; unauthenticated and untrusted-root requests denied','scope':'local protocol peer; no production external reasoning Agent or billed model tested'},indent=2))
    finally:
        await peer.close();process.terminate();process.wait(timeout=5);await c.cancel(agents[0][0].task,'A2A reference completed')

@pytest.mark.asyncio
async def test_real_unexecuted_agent_reassignment_keeps_task_workspace_budget(agents,tmp_path):
    from test_supervised_runtime import wait_for
    from app.adapters.kanaloa_adapter import KanaloaAdapter
    core=agents[0][3];c=coordinator(agents);r,store,old_adapter,_=agents[1];old_id=r.agent
    await core.request('collaboration_create',plan=plan(agents))
    old_bindings=(await core.request('runtime_status',id=old_id))['agent']
    r.control('stop',timeout=20)
    async def stopped():
        s=await core.request('runtime_status',id=old_id)
        return s if s['agent']['state']=='Suspended' else None
    await until(stopped)
    replacement=await core.request('register_agent',role='verified replacement reader',task_id=r.task,model_id=old_bindings['model_id'])
    await core.request('collaboration_harness_profile',id=replacement['id'],capabilities=['read-public-text'],evidence_hash=FINGERPRINT,expires_at=int(time.time())+300)
    await core.request('collaboration_reassign',id=r.task,agent_id=replacement['id'],generation=0)
    # Existing original DSH command, workspace, HOME, read guard and Supervisor spec.
    spec=json.loads(r.spec.read_text());spec['agent_id']=replacement['id'];new_spec=r.path/'replacement-runtime.json';new_spec.write_text(json.dumps(spec))
    r.agent=replacement['id'];r.socket=r.core_socket.parent/f'{r.agent}.runtime.sock';r.spec=new_spec
    r.call({'op':'grant_tool','id':r.agent,'tool':'read','arguments':{'file_path':'input.txt'},'expires_at':int(time.time())+120})
    r.start();wait_for(r.status)
    adapter=r.adapter();adapter.execution_socket=old_adapter.execution_socket
    old_adapter.event_handler.register_adapter('kanaloa',adapter)
    c.bindings.pop(old_id);c.bindings[r.agent]=HarnessBinding(adapter,store,'kanaloa')
    try:
        await c.dispatch(r.task);await verify((r,store,adapter,core))
        current=(await core.request('runtime_status',id=r.agent))['agent']
        for key in ['task_id','workspace_id','budget_id']:assert current[key]==old_bindings[key]
        assert (await core.request('runtime_status',id=old_id))['agent']['task_id']==r.task
        with pytest.raises(CoreRejected):await core.request('collaboration_reassign',id=r.task,agent_id=old_id,generation=1)
        (tmp_path/'real-reassignment.json').write_text(json.dumps({'old_agent':old_bindings,'new_agent':current,'events':await core.request('events'),'automatic_replay':False},indent=2))
    finally:
        await adapter.close();await c.cancel(agents[0][0].task,'End focused unexecuted reassignment test')

@pytest.mark.asyncio
@pytest.mark.parametrize('limit',[1,2])
async def test_owner_declared_dag_driver_advances_only_after_independent_verification(agents,tmp_path,limit):
    core=agents[0][3];c=coordinator(agents);root=agents[0][0].task
    await core.request('collaboration_create',plan=plan(agents,limit=limit))
    calls=[]
    async def independent(task,artifacts):
        item=next(x for x in agents if x[0].task==task)
        expected='# Verified fixture\n\nKANE_PR2_FIXTURE\n'
        assert len(artifacts)==1 and artifacts[0]['content']==expected
        assert artifacts[0]['content_hash']==hashlib.sha256(expected.encode()).hexdigest()
        assert (item[0].workspace/'input.txt').read_text()=='KANE_PR2_FIXTURE\n'
        assert (await core.request('task',id=task))['state']=='Verifying'
        calls.append(task)
        return {'passed':True,'evidence':'Independent driver oracle: original actual bytes, immutable source, expected UTF-8 and hash'}
    result=await c.run(root,independent,timeout=90,share_confirmed='public')
    assert result['execution_status']=='verified'
    assert calls[-1]==root and len(calls)==3
    events=await core.request('events')
    assert len([e for e in events if e['kind']=='prompt_dispatch'])==3
    assert len([e for e in events if e['kind']=='context_share'])==2
    assert all(x[0].call({'op':'runtime_status','id':x[0].agent})['agent']['state']=='Suspended' for x in agents)
    (tmp_path/'automatic-dag-driver.json').write_text(json.dumps({'result':result,'verification_order':calls,'events':events,'planner':'Owner-declared typed plan; no new Agent reasoning loop or model scheduler'},indent=2))


@pytest.mark.asyncio
@pytest.mark.parametrize('verdict',[None,False])
async def test_dag_driver_never_promotes_missing_or_failed_independent_verdict(agents,verdict,tmp_path):
    core=agents[0][3];c=coordinator(agents);root=agents[0][0].task
    await core.request('collaboration_create',plan=plan(agents,limit=1))
    async def independent(task,artifacts):
        return None if verdict is None else {'passed':False,'evidence':'Independent seeded negative verdict; no self-evaluation'}
    result=await c.run(root,independent,timeout=90)
    assert result['execution_status']==('awaiting-independent-verification' if verdict is None else 'needs-rework')
    assert (await core.request('task',id=root))['state']!='Done'
    events=await core.request('events');assert len([e for e in events if e['kind']=='prompt_dispatch'])==1
    assert not [e for e in events if e['kind']=='verification' and e['payload']['passed']]
    (tmp_path/'driver-verification-negative.json').write_text(json.dumps({'result':result,'events':events},indent=2))
    await c.cancel(root,'Stop negative independent-verification fixture without replay')
