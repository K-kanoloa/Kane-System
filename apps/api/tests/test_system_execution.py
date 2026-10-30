"""PR-2 real Supervisor + Kanaloa loop, deterministic model, existing read tool."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import tempfile
import subprocess
import signal
import time
import httpx

import pytest

from test_supervised_runtime import Rig, ROOT, RUNTIME_ASSETS_ROOT, wait_for
from app.system.client import SystemClient, CoreRejected, CoreUnavailable
from app.system.execution import ExecutionBridge
from app.domain.models import Conversation, Turn, Message
from app.harness.coordinator import HarnessCoordinator
from app.harness.mailbox import MailboxManager
from app.store.sqlite_store import SQLiteStore

pytestmark=pytest.mark.skipif(os.getenv('KANE_PR2_TEST')!='1',reason='opt-in PR-2 native deterministic integration')

@pytest.fixture
def execution(tmp_path, monkeypatch, request):
    config=dict(getattr(request,'param',{}))
    phase2_model_spec=config.pop('phase2_model_spec',None)
    logical_model_id=config.pop('logical_model_id','kane-fixture/deterministic-v1')
    sandboxed=config.pop('sandbox',False)
    native_file_proxy=config.pop('native_file_proxy',False)
    native_read_fencing=config.pop('native_read_fencing',False)
    max_restarts=config.pop('max_restarts',None)
    shared_core=config.pop('core_socket',None)
    budget_tokens=config.pop('budget_tokens',10000)
    shared_fifo=config.pop('shared_fifo',False)
    task_memory_bytes=config.pop('task_memory_bytes',536870912 if sandboxed else 268435456)
    task_goal=config.pop('task_goal','Read isolated input.txt and deliver Markdown preserving its marker')
    task_acceptance=config.pop('task_acceptance',['Markdown exactly equals the independent fixture', 'Input file remains unchanged', 'Result is submitted to Verifying before independent validation'])
    budget_hook=config.pop('budget_hook',True)
    tool_hook=config.pop('tool_hook',True)
    tool_grant=config.pop('tool_grant',True)
    bounded_home=config.pop('bounded_home',False)
    managed_host=config.pop('host_resources',False)
    defer_start=config.pop('defer_start',False)
    runtime_home=config.pop('runtime_home',None)
    initialize_timeout=config.pop('initialize_timeout_seconds',30)
    sandbox_cpu_millis=config.pop('sandbox_cpu_millis',500)
    sandbox_memory_bytes=config.pop('sandbox_memory_bytes',536870912)
    storage_options=({'runtime_home':runtime_home or os.environ['KANE_STORAGE_HOME'],'task_storage':(256 if managed_host else 128)*1024*1024} if bounded_home else {})
    runtime_root=os.getenv('KANE_TEST_RUNTIME_ROOT')
    model_spec={'provider':'kane-fixture','model':'deterministic-v1','backend':'deterministic in-process test adapter','cloud':False,'budget_profile':{'provider':'kane-fixture','model':'deterministic-v1','upper_bound_tokens':32,'bound_evidence':'Deterministic fixture emits at most 27 synthetic tokens per call; includes input and output'}}
    if phase2_model_spec is not None:
        model_spec=phase2_model_spec
    if shared_fifo:
        model_spec['scheduling']={'mode':'mock_fifo','backend_kind':'mock'}
    r=Rig(Path(tempfile.mkdtemp(prefix='ks-pr2-',dir=runtime_root)), **storage_options, budget_tokens=budget_tokens, core_socket=shared_core, task_memory=task_memory_bytes, model_id=logical_model_id, model_spec=model_spec, goal=task_goal, acceptance=task_acceptance)
    r.sandboxed=sandboxed
    patch=(Path(tempfile.mkdtemp(prefix='ks-code-',dir=runtime_root)) if sandboxed else r.path)/'fixture.yml'
    gateway=r.path/'gateway'
    if sandboxed: gateway.mkdir(mode=0o700)
    execution_socket=str(gateway/'gateway.sock') if sandboxed else str(r.socket)+'.budget.sock'
    log_root=r.home if sandboxed else r.path
    config['invocation_log']=str(log_root/'model-invocations.jsonl')
    config['tool_invocation_log']=str(log_root/'tool-invocations.jsonl')
    entries=[
      {'id':'agent-default-model','config':{'provider':'kane-fixture','model':'deterministic-v1'}},
      {'id':'acp','config':{'provider':'kane-fixture','model':'deterministic-v1'}},
      {'insert':[{'id':'kane-fixture','name':str(RUNTIME_ASSETS_ROOT/'bench/pr2/mock-model.mjs'),'config':config},
                 {'id':'kane-model-observer','name':str(RUNTIME_ASSETS_ROOT/'adapters/dsh/model-observer.mjs')},
                 {'id':'kane-model-budget','name':str(RUNTIME_ASSETS_ROOT/'adapters/dsh/model-budget.mjs'),'config':{'socket':execution_socket,'require_fifo':shared_fifo}}]}]
    if tool_hook:
        entries[-1]['insert'].append({'id':'kane-tool-control','name':str(RUNTIME_ASSETS_ROOT/'adapters/dsh/tool-control.mjs'),'config':{'socket':execution_socket,'native_file_proxy':native_file_proxy,'native_read_fencing':native_read_fencing}})
    if not budget_hook:
        entries[-1]['insert']=[p for p in entries[-1]['insert'] if p['id']!='kane-model-budget']
    patch.write_text(json.dumps(entries))
    spec=json.loads(r.spec.read_text())
    if max_restarts is not None:
        spec['max_restarts']=max_restarts
    spec['command']+=['--patch',str(patch)]
    if sandboxed:
        image=subprocess.check_output(['docker','image','inspect','ubuntu:24.04','--format','{{.Id}}'],text=True).strip()
        paths=[Path(spec['command'][0]),RUNTIME_ASSETS_ROOT/'node_modules',RUNTIME_ASSETS_ROOT/'adapters/dsh',RUNTIME_ASSETS_ROOT/'bench/pr2',RUNTIME_ASSETS_ROOT/'packages/kanaloa-tools',RUNTIME_ASSETS_ROOT/'apps/api/app/adapters/kanaloa-persona.patch.yml',patch]
        spec['sandbox']={'image':image,'cpu_millis':sandbox_cpu_millis,'memory_bytes':sandbox_memory_bytes,'pids':96,'read_only':[str(p.resolve()) for p in paths],'gateway_dir':str(gateway)}
        if bounded_home:
            spec['sandbox']['home_capacity_bytes']=64*1024*1024
        if managed_host:
            spec['sandbox']['host_resources']=True
        if native_file_proxy:
            spec['sandbox']['native_file_proxy']=True
    r.spec.write_text(json.dumps(spec))
    (r.workspace/'input.txt').write_text('KANE_PR2_FIXTURE\n')
    (r.home/'.dsh').mkdir()
    (r.home/'.dsh/settings.yaml').write_text(json.dumps({'agent-default-model':{'provider':'kane-fixture','model':'deterministic-v1'}}))
    monkeypatch.setenv('KANE_SYSTEM_SOCKET',str(r.core_socket))
    store=SQLiteStore(str(r.path/'harness.db'))
    coordinator=HarnessCoordinator(store,MailboxManager())
    adapter=r.adapter(initialize_timeout_seconds=initialize_timeout)
    if bounded_home:
        async def capture_fixture_stderr():
            with (tmp_path/'native-stderr.log').open('ab') as log:
                while line := await adapter._process.stderr.readline():
                    log.write(line)
                    log.flush()
        adapter._read_stderr_loop=capture_fixture_stderr
        original_ensure=adapter._ensure_process
        async def capture_startup_error():
            try:
                return await original_ensure()
            except BaseException as exc:
                (tmp_path/'startup-error.txt').write_text(f'{type(exc).__name__}: {exc}')
                raise
        adapter._ensure_process=capture_startup_error
    adapter.execution_socket=execution_socket
    coordinator.register_adapter('kanaloa',adapter)
    core=SystemClient()
    adapter.execution_bridge=ExecutionBridge(store,r.agent,core)
    reference_window=int(config.get('reference_authorization_s',300))
    assert 60 <= reference_window <= 1800
    if tool_grant:
        r.tool_cap=r.call({'op':'grant_tool','id':r.agent,'tool':'write' if config.get('test_write') else 'read','arguments':{'file_path':'output.txt','content':'KANE_PR3B_SAFE_FIXTURE\n'} if config.get('test_write') else {'file_path':'input.txt'},'expires_at':int(time.time())+reference_window})['id']
    if native_file_proxy:
        r.call({'op':'transfer_lease','id':r.ws,'epoch':0,'holder':r.agent,'expires_at':int(time.time())+reference_window})
        r.modify_cap=r.call({'op':'grant_workspace_modify','id':r.agent,'expires_at':int(time.time())+reference_window})['id']
    if not defer_start:
        r.start();wait_for(r.status)
    try:
        yield r,store,adapter,core
    finally:
        (tmp_path/'evidence.json').write_text(json.dumps({'task':r.call({'op':'task','id':r.task}),'artifacts':r.call({'op':'artifacts','id':r.task}),'events':r.call({'op':'events'}),'runtime':r.call({'op':'runtime_status','id':r.agent}),'tool_invocations':(log_root/'tool-invocations.jsonl').read_text() if (log_root/'tool-invocations.jsonl').exists() else '', 'budget':r.call({'op':'model_budget','id':r.budget}),'model_invocations':(log_root/'model-invocations.jsonl').read_text() if (log_root/'model-invocations.jsonl').exists() else ''},indent=2))
        r.close();store.close()
        (tmp_path/'runtime.log').write_text((r.path/'runtime.log').read_text())

async def run_task(execution, wait=True, prompt=None):
    r,store,adapter,core=execution
    suffix=('-'+r.agent) if getattr(r,'sandboxed',False) else ''
    c=Conversation(conversation_id='pr2'+suffix,bound_agent_id='kanaloa')
    t=Turn(turn_id='pr2-turn'+suffix,conversation_id=c.conversation_id,bound_agent_id='kanaloa')
    m=Message(message_id='pr2-prompt'+suffix,conversation_id=c.conversation_id,turn_id=t.turn_id,sender='user',content=prompt or 'Read input.txt and return its marker as Markdown.')
    store.save_conversation(c);store.save_turn(t);store.append_message(m)
    original=adapter._send_request
    async def capture(method, params=None):
        response=await original(method,params)
        if "error" in response:
            print("Native test error:",response["error"])
        return response
    adapter._send_request=capture
    await adapter.send(t,m,[])
    if not wait:
        return t,m,None
    async with asyncio.timeout(25):
        while True:
            state=await core.request('task',id=r.task)
            if state['state'] in ['Verifying','Blocked','Failed']:
                return t,m,state
            await asyncio.sleep(.05)

@pytest.mark.asyncio
async def test_actual_tool_task_artifact_and_independent_verification(execution):
    r,store,adapter,core=execution
    try:
        t,m,state=await run_task(execution)
        assert state['state']=='Verifying', store.get_turn(t.turn_id)
        artifacts=await core.request('artifacts',id=r.task)
        assert len(artifacts)==1
        expected='# Verified fixture\n\nKANE_PR2_FIXTURE\n'
        assert artifacts[0]['content']==expected
        assert (r.workspace/'input.txt').read_text()=='KANE_PR2_FIXTURE\n'
        assert artifacts[0]['content_hash']==hashlib.sha256(expected.encode()).hexdigest()
        events=await core.request('events')
        starts=[e for e in events if e['kind']=='llm_call_start']
        results=[e for e in events if e['kind']=='llm_call_result']
        assert len(starts)==len(results)==2
        assert sum(e['payload']['fact']['usage']['totalTokens'] for e in results)==42
        assert any(e['kind']=='tool_call' and e['payload']['fact'].get('status')=='completed' for e in events)
        assert next(e['seq'] for e in events if e['kind']=='prompt_dispatch') < starts[0]['seq']
        assert len([m for m in store.get_messages('pr2') if m.sender=='agent'])==1
        # Reconcile persisted Message twice; no duplicate artifact or model execution.
        await adapter.execution_bridge.sync(t.turn_id)
        await adapter.execution_bridge.sync(t.turn_id)
        assert len(await core.request('artifacts',id=r.task))==1
        with pytest.raises(CoreRejected):
            await adapter.execution_bridge.admit(t.turn_id,m,t.native_session_ref)
        # Acceptance is outside the mock/Agent, with exact independent fixture+hash.
        done=await core.request('verify',id=r.task,version=state['version'],passed=True,evidence='Independent fixture equality and SHA-256 assertions; deterministic test only')
        assert done['state']=='Done'
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_lost_dispatch_response_blocks_without_model_call(execution):
    r,store,adapter,core=execution
    original=core.request
    async def lose_reply(op,**kw):
        result=await original(op,**kw)
        if op=="dispatch_harness":
            raise CoreUnavailable("injected lost committed response")
        return result
    core.request=lose_reply
    try:
        t,m,state=await run_task(execution)
        assert state["state"]=="Blocked" and state["block_reason"]=="reconciliation"
        events=await original("events")
        assert len([e for e in events if e["kind"]=="prompt_dispatch"])==1
        assert not any(e["kind"].startswith("llm_call") for e in events)
        with pytest.raises(CoreRejected):
            await original("dispatch_harness",id=r.agent,turn_id=t.turn_id,message_id=m.message_id,content=m.content,native_session_ref=t.native_session_ref)
    finally:
        await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("execution",[{"delay_ms":2000}],indirect=True)
async def test_runtime_crash_during_model_blocks_task_without_replay(execution):
    r,store,adapter,core=execution
    try:
        t,m,_=await run_task(execution,wait=False)
        async with asyncio.timeout(10):
            while not any(e["kind"]=="llm_call_start" for e in await core.request("events")):
                await asyncio.sleep(.02)
        original_pid=r.status()["pid"]
        os.kill(original_pid,signal.SIGKILL)
        async with asyncio.timeout(10):
            while (state:=await core.request("task",id=r.task))["state"]!="Blocked":
                await asyncio.sleep(.02)
        assert state["block_reason"]=="reconciliation"
        await asyncio.sleep(.4)
        assert r.status()["pid"]!=original_pid
        assert r.status()["registry"]["agent"]["task_id"]==r.task
        assert not await core.request("artifacts",id=r.task)
        assert len([e for e in await core.request("events") if e["kind"]=="prompt_dispatch"])==1
        with pytest.raises(CoreRejected):
            await adapter.execution_bridge.admit(t.turn_id,m,t.native_session_ref)
    finally:
        await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("execution",[{"denied_tool":True}],indirect=True)
async def test_test_profile_rejects_write_and_preserves_fixture(execution):
    r,store,adapter,core=execution
    try:
        t,m,state=await run_task(execution)
        assert state["state"]=="Blocked"
        assert (r.workspace/'input.txt').read_text()=='KANE_PR2_FIXTURE\n'
        assert not await core.request("artifacts",id=r.task)
        assert any(e["kind"]=="tool_call" and e["payload"]["fact"].get("status")=="failed" for e in await core.request("events"))
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_owner_http_dispatch_and_duplicate_rejection(execution,monkeypatch):
    from app.main import create_app
    r,store,adapter,core=execution
    monkeypatch.setenv("KANE_SYSTEM_ENABLED","1")
    monkeypatch.setenv("OCTOPUS_API_TOKEN","pr2-owner")
    monkeypatch.delenv("OCTOPUS_API_KEY",raising=False)
    app=create_app()
    app.state.store=store
    app.state.kanaloa_adapter=adapter
    app.state.system_client=core
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
            path=f"/api/v1/system/tasks/{r.task}/dispatch"
            assert (await client.post(path,json={"prompt":"read input.txt"})).status_code==401
            response=await client.post(path,json={"prompt":"read input.txt"},headers={"X-Api-Key":"pr2-owner"})
            assert response.status_code==200,response.text
            assert (await client.post(path,json={"prompt":"read input.txt"},headers={"X-Api-Key":"pr2-owner"})).status_code==409
            async with asyncio.timeout(10):
                while (await core.request("task",id=r.task))["state"]!="Verifying":
                    await asyncio.sleep(.05)
            assert len(await core.request("artifacts",id=r.task))==1
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_lost_completion_reply_keeps_one_artifact_and_no_prompt_retry(execution):
    r,store,adapter,core=execution
    original=core.request
    lost=False
    async def lose_completion(op,**kw):
        nonlocal lost
        result=await original(op,**kw)
        if op=="sync" and kw.get("status")=="finished" and not lost:
            lost=True
            raise CoreUnavailable("injected lost artifact commit response")
        return result
    core.request=lose_completion
    try:
        t,m,state=await run_task(execution)
        assert state["state"]=="Verifying"
        await asyncio.sleep(.05)
        assert lost
        final=[m for m in store.get_messages('pr2') if m.sender=='agent'][0]
        # Recovery uses stored facts and idempotent receipt, without new inference.
        await original('sync',turn_id=t.turn_id,status='finished',native_session_ref=t.native_session_ref,
                       message={'message_id':final.message_id,'content':final.content})
        assert len(await original('artifacts',id=r.task))==1
        events=await original('events')
        assert len([e for e in events if e['kind']=='prompt_dispatch'])==1
        assert len([e for e in events if e['kind']=='llm_call_start'])==2
    finally:
        await adapter.close()
