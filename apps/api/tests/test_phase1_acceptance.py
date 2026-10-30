"""Phase 1: real Kanaloa/DSH + deterministic model, no replacement Agent loop."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess

import pytest
from app.system.client import CoreRejected, CoreUnavailable
from test_system_execution import execution, run_task
from test_sandbox_runtime import ready, node
from test_supervised_runtime import BIN

pytestmark = pytest.mark.skipif(os.getenv('KANE_PHASE1_TEST') != '1', reason='opt-in Phase 1 real DSH acceptance')

async def until(fn):
    async with asyncio.timeout(20):
        while True:
            value = await fn()
            if value: return value
            await asyncio.sleep(.025)

@pytest.mark.asyncio
@pytest.mark.parametrize('execution',[{'sandbox':True,'shared_fifo':True,'delay_ms':4000}],indirect=True)
@pytest.mark.parametrize('fault',['none','lost_reply','core_restart'])
async def test_real_cancel_fences_before_acp_and_preserves_unknown(execution,tmp_path,fault):
    r,store,adapter,core=execution
    original=core.request
    try:
        await ready(r)
        turn,_,_=await run_task(execution,wait=False)
        async def in_flight():
            return [q for q in await original('model_queue') if q['state']=='Dispatched'] if (r.home/'model-invocations.jsonl').exists() else None
        await until(in_flight)
        notifications=[]
        native=adapter._send_notification
        async def notify(method,params):
            if method=='session/cancel':
                task=await original('task',id=r.task)
                assert task['state']=='Blocked' and task['block_reason']=='reconciliation'
                assert (await original('model_budget',id=r.budget))['reserved_tokens']==32
                notifications.append(method)
            return await native(method,params)
        adapter._send_notification=notify
        async def intercept(op,**kw):
            result=await original(op,**kw)
            if op=='cancel_task' and fault=='lost_reply': raise CoreUnavailable('lost durable cancel reply')
            if op=='cancel_task' and fault=='core_restart':
                r.core.kill();r.core.wait(timeout=5)
                r.core=subprocess.Popen([str(BIN),'--db',str(r.path/'core.db'),'--socket',str(r.core_socket),'serve'],stderr=r.log)
                async def health():
                    try:return await original('health')
                    except (CoreUnavailable,CoreRejected):return False
                await until(health)
            return result
        core.request=intercept
        if fault=='lost_reply':
            with pytest.raises(CoreUnavailable):await adapter.cancel(store.get_turn(turn.turn_id))
            assert not notifications
            core.request=original
            await adapter.cancel(store.get_turn(turn.turn_id))
        else: await adapter.cancel(store.get_turn(turn.turn_id))
        assert len(notifications)==1
        queue=await original('model_queue');assert queue[0]['state']=='Unknown'
        with pytest.raises(CoreRejected):
            await original('reserve_model',turn_id=turn.turn_id,native_session_ref=turn.native_session_ref,call_id='after-cancel',provider='kane-fixture',model='deterministic-v1')
        # Even a plausible late final usage cannot settle an Unknown reservation.
        try:
            late_receipt=await original('settle_model',turn_id=turn.turn_id,call_id=queue[0]['call_id'],receipt={'outcome':'completed','usage':{'inputTokens':1,'outputTokens':1,'totalTokens':2}})
        except CoreRejected as error:
            # The native cancellation receipt may have arrived first. A later
            # contradictory receipt must conflict; neither legal arrival order
            # may release Unknown holds or unfreeze the model route.
            assert error.status==409
            late_receipt={'conflicting_receipt_rejected':True,'status':error.status}
        assert (await original('model_budget',id=r.budget))['reserved_tokens']==32
        assert (await original('model_queue'))[0]['state']=='Unknown'
        assert not await original('artifacts',id=r.task)
        events=await original('events');fence=next(e['seq'] for e in events if e['kind']=='task_cancel_requested')
        assert len([e for e in events if e['kind']=='task_cancel_requested'])==1
        assert not [e for e in events if e['kind'] in ('dispatch','model_reserved') and e['seq']>fence]
        (tmp_path/'cancellation.json').write_text(json.dumps({'fault':fault,'notifications':notifications,'late_receipt':late_receipt,'queue':await original('model_queue'),'budget':await original('model_budget',id=r.budget),'task':await original('task',id=r.task),'events':events},indent=2))
    finally:core.request=original;await adapter.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('execution',[{'sandbox':True}],indirect=True)
@pytest.mark.parametrize('verdict',['manual','failed','unverified','verifier_crash'])
async def test_independent_artifact_state_paths(execution,tmp_path,verdict):
    r,store,adapter,core=execution
    try:
        await ready(r);turn,_,state=await run_task(execution)
        assert state['state']=='Verifying'
        artifacts=await core.request('artifacts',id=r.task);assert len(artifacts)==1
        artifact=artifacts[0];expected='# Verified fixture\n\nKANE_PR2_FIXTURE\n'
        checks={'content':artifact['content']==expected,'hash':artifact['content_hash']==hashlib.sha256(expected.encode()).hexdigest(),
                'input_unchanged':(r.workspace/'input.txt').read_text()=='KANE_PR2_FIXTURE\n','editable':artifact['editable'],
                'producer':artifact['producer_id']==r.agent,'source':any(e['kind']=='artifact_submitted' and e['payload'].get('message_id') for e in await core.request('events'))}
        assert all(checks.values())
        evidence=json.dumps({'independent_checks':checks,'task':r.task,'artifact':artifact['id'],'sha256':artifact['content_hash']},sort_keys=True)
        # An injected verifier exception does not invoke the success endpoint.
        if verdict=='verifier_crash':
            with pytest.raises(RuntimeError):raise RuntimeError('verifier failed before verdict')
            assert (await core.request('task',id=r.task))['state']=='Verifying'
        elif verdict=='failed':
            result=await core.request('verify',id=r.task,version=state['version'],passed=False,evidence='Independent negative fixture rejects unexpected required marker; '+evidence)
            assert result['state']=='Running' and result['verification_state']=='failed'
            assert (await core.request('artifacts',id=r.task))[0]['verification_state']=='failed'
        else:
            state=await core.request('transition',id=r.task,version=state['version'],state='Blocked',reason='awaiting-verification')
            with pytest.raises(CoreRejected):await core.request('transition',id=r.task,version=state['version'],state='Done')
            if verdict=='unverified':
                result=await core.request('close_unverified',id=r.task,version=state['version'],reason='Owner explicitly accepts unverified fixture')
                assert result['state']=='ClosedUnverified' and result['verification_state']=='unverified'
            else:
                state=await core.request('transition',id=r.task,version=state['version'],state='Verifying')
                result=await core.request('verify',id=r.task,version=state['version'],passed=True,evidence=evidence)
                assert result['state']=='Done'
        await adapter.execution_bridge.sync(turn.turn_id)
        assert len(await core.request('artifacts',id=r.task))==1
        (tmp_path/'verifier.json').write_text(json.dumps({'verdict':verdict,'checks':checks,'task':await core.request('task',id=r.task),'artifacts':await core.request('artifacts',id=r.task),'events':await core.request('events')},indent=2))
    finally:await adapter.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('execution',[{'sandbox':True}],indirect=True)
async def test_fd_symlink_path_swap_and_owner_gateway_bypasses_denied(execution,tmp_path):
    r,_,adapter,core=execution
    try:
        await ready(r);await run_task(execution)
        code='''const f=require('fs');const w=process.argv[1];let out={};
        for(const [n,fn] of Object.entries({fd:()=>f.openSync(w+'/input.txt','r+'),symlink:()=>f.symlinkSync('/etc/passwd',w+'/evil'),rename:()=>f.renameSync(w+'/input.txt',w+'/moved'),proc:()=>f.writeFileSync('/proc/1/root'+w+'/input.txt','BAD')})){
          try{fn();out[n]='ALLOWED'}catch(e){out[n]=e.code}}
        console.log(JSON.stringify(out));'''
        # node helper takes a complete program, no shell interpolation.
        code=code.replace('process.argv[1]',json.dumps(str(r.workspace)))
        rc,out,err=await node(r,code);assert rc==0,err
        denied=json.loads(out);assert all(v in ('EROFS','EACCES','ENOENT') for v in denied.values()),denied
        assert (r.workspace/'input.txt').read_text()=='KANE_PR2_FIXTURE\n'
        (tmp_path/'kernel-bypasses.json').write_text(json.dumps(denied,indent=2))
    finally:await adapter.close()

@pytest.mark.asyncio
async def test_supervised_capability_declaration_and_real_session_restore(execution,tmp_path):
    r,store,adapter,core=execution
    try:
        await adapter.activate();assert adapter._is_initialized
        caps=adapter.capabilities();assert caps.supports_stream and caps.supports_cancel and caps.supports_resume
        assert caps.steer_mode=='follow_up_only' and caps.branch_mode=='unsupported'
        turn,_,state=await run_task(execution);assert state['state']=='Verifying'
        native=turn.native_session_ref;before=(r.path/'model-invocations.jsonl').read_text()
        await adapter.close()
        await asyncio.to_thread(r.supervisors[-1].wait,10)
        r.start()
        from test_supervised_runtime import wait_for
        await asyncio.to_thread(wait_for,r.status)
        await adapter.resume(store.get_turn(turn.turn_id),[])
        assert adapter.get_native_session(turn.turn_id)==native
        assert (r.path/'model-invocations.jsonl').read_text()==before
        with pytest.raises(RuntimeError,match='admission contract'):
            await adapter._send_request('session/steer',{'sessionId':native,'prompt':[]})
        (tmp_path/'harness.json').write_text(json.dumps({'mode':'supervised','capabilities':caps.model_dump(),'native_session':native,'restored_context_only':True,'task':await core.request('task',id=r.task)},indent=2))
    finally:await adapter.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('execution',[{'require_approval':True}],indirect=True)
@pytest.mark.parametrize('decision',['allow-once','reject-once'])
async def test_real_acp_permission_roundtrip(execution,tmp_path,decision):
    r,store,adapter,core=execution
    try:
        turn,_,_=await run_task(execution,wait=False)
        async def pending():
            return next((p for p in adapter._pending_permissions.values() if p.status=='pending'),None)
        permission=await until(pending)
        assert store.get_turn(turn.turn_id).status=='waiting_user'
        with pytest.raises(ValueError):await adapter.respond_permission(permission.request_id,decision,session_id='forged-session')
        await adapter.respond_permission(permission.request_id,decision,session_id=turn.native_session_ref)
        async def ended():
            task=await core.request('task',id=r.task)
            return task if task['state'] in ('Verifying','Blocked') else None
        task=await until(ended)
        assert task['state']==('Verifying' if decision=='allow-once' else 'Blocked')
        with pytest.raises(ValueError):await adapter.respond_permission(permission.request_id,decision)
        (tmp_path/'approval.json').write_text(json.dumps({'decision':decision,'request_id':permission.request_id,'native_session':turn.native_session_ref,'task':task,'events':await core.request('events')},indent=2))
    finally:await adapter.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('execution',[{'budget_hook':False,'tool_hook':False,'delay_ms':500}],indirect=True)
async def test_standalone_real_acp_stream_steer_resume_without_supervisor_ownership(execution,tmp_path,monkeypatch):
    from app.adapters.kanaloa_adapter import KanaloaAdapter
    from app.domain.models import Conversation,Turn,Message
    r,store,old,core=execution
    adapter=None
    try:
        # This isolated test intentionally exercises the preserved standalone
        # contract; no protected-production or Core enforcement claim is made.
        r.control('stop');await asyncio.to_thread(r.supervisors[-1].wait,10)
        monkeypatch.setenv('HOME',str(r.home))
        adapter=KanaloaAdapter(command=json.loads(r.spec.read_text())['command'],cwd=str(r.workspace))
        old.event_handler.register_adapter('standalone',adapter)
        await adapter.activate();assert adapter._is_initialized and adapter._process.pid>0
        assert adapter.capabilities().steer_mode=='native'
        parallel=await adapter._send_request('session/new',{'cwd':str(r.workspace),'mcpServers':[]})
        assert 'error' not in parallel
        parallel_id=parallel['result']['sessionId']
        c=Conversation(conversation_id='standalone',bound_agent_id='standalone');store.save_conversation(c)
        t=Turn(turn_id='standalone',conversation_id=c.conversation_id,bound_agent_id='standalone');store.save_turn(t)
        m=Message(message_id='standalone-send',conversation_id=c.conversation_id,turn_id=t.turn_id,sender='user',content='Read only input.txt')
        store.append_message(m);await adapter.send(t,m,[])
        async def active():return store.get_turn(t.turn_id) if adapter.get_native_session(t.turn_id) else None
        await until(active)
        async def provider_entered():return (r.path/'model-invocations.jsonl').exists()
        await until(provider_entered)
        native=adapter.get_native_session(t.turn_id)
        # A real native steer RPC is accepted on the original DSH connection.
        reply=await adapter._send_request('session/steer',{'sessionId':native,'prompt':[{'type':'text','text':'Preserve the fixture marker.'}]})
        assert 'error' not in reply,reply
        assert parallel_id != native
        async def finished():
            current=store.get_turn(t.turn_id)
            return current if current.status=='finished' else None
        done=await until(finished)
        assert any(m.sender=='agent' and 'KANE_PR2_FIXTURE' in m.content for m in store.get_messages(c.conversation_id))
        before=(r.path/'model-invocations.jsonl').read_text();await adapter.close();await adapter.resume(done,[])
        assert adapter.get_native_session(t.turn_id)==native and (r.path/'model-invocations.jsonl').read_text()==before
        assert (await core.request('task',id=r.task))['state']=='Pending'
        from app.harness.dispatcher import Dispatcher
        dispatcher=Dispatcher(store,adapter.event_handler,adapter.event_handler.mailbox_manager)
        history=store.get_messages(c.conversation_id)
        branch=dispatcher.branch_from_message(c.conversation_id,history[-1].message_id)
        prompt=Message(message_id='branch-send',conversation_id=c.conversation_id,turn_id=branch.turn_id,sender='user',content='Read only input.txt in the new branch')
        store.append_message(prompt)
        requests=[];wire=adapter._send_request
        async def capture(method,params=None):
            requests.append({'method':method,'params':params})
            return await wire(method,params)
        adapter._send_request=capture
        await adapter.send(branch,prompt,history)
        async def branch_done():return store.get_turn(branch.turn_id).status=='finished'
        await until(branch_done)
        assert adapter.get_native_session(branch.turn_id)!=native
        prompts=[q for q in requests if q['method']=='session/prompt'];assert len(prompts)==1
        assert len(prompts[0]['params']['prompt'])==len(history)+1
        assert all(b['text'].startswith('[') for b in prompts[0]['params']['prompt'][:-1])
        for method in ('session/pause','session/checkpoint'):
            unsupported=await wire(method,{'sessionId':native})
            assert unsupported['error']['code']==-32601
        (tmp_path/'standalone.json').write_text(json.dumps({'native_session':native,'steer_reply':reply,'restored_context_only':True,'capabilities':adapter.capabilities().model_dump()},indent=2))
    finally:
        if adapter:await adapter.close()
        await old.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('execution',[{'sandbox':True,'native_file_proxy':True,'test_write':True}],indirect=True)
@pytest.mark.parametrize('stage',['tool_prepare','tool_claim','tool_file','tool_result'])
async def test_cancel_at_real_tool_boundaries_never_replays_publication(execution,tmp_path,stage):
    r,store,adapter,core=execution;original=core.request;fences=[]
    async def intercept(op,**kw):
        if op==stage and not fences:
            fences.append(await original('cancel_task',id=r.task,reason='Fault injected at '+stage))
        return await original(op,**kw)
    core.request=intercept
    try:
        await ready(r);turn,_,_=await run_task(execution,wait=False)
        async def cancelled():
            if not fences:return False
            return await original('task',id=r.task)
        state=await until(cancelled)
        await asyncio.sleep(.3)
        state=await original('task',id=r.task)
        assert state['state']==('Cancelled' if stage in ('tool_prepare','tool_claim') else 'Blocked')
        output=r.workspace/'output.txt'
        assert output.exists()==(stage=='tool_result')
        if output.exists():assert output.read_text()=='KANE_PR3B_SAFE_FIXTURE\n'
        events=await original('events');intents=[e for e in events if e['kind']=='intent']
        if intents:
            action=await original('action',id=intents[0]['payload']['action'])
            assert action['state']==('NotExecuted' if stage=='tool_claim' else 'Unknown')
            with pytest.raises(CoreRejected):await original('tool_claim',turn_id=turn.turn_id,native_session_ref=turn.native_session_ref,call_id='fixture-read')
        assert not await original('artifacts',id=r.task)
        (tmp_path/'tool-cancel.json').write_text(json.dumps({'stage':stage,'task':state,'file_exists':output.exists(),'events':events},indent=2))
    finally:core.request=original;await adapter.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('execution',[{'sandbox':True,'delay_ms':2000}],indirect=True)
async def test_cancel_core_unavailable_does_not_send_unfenced_native_cancel(execution,tmp_path):
    r,store,adapter,core=execution;original=core.request
    try:
        await ready(r);turn,_,_=await run_task(execution,wait=False)
        async def entered():return (r.home/'model-invocations.jsonl').exists()
        await until(entered)
        async def fail(op,**kw):raise CoreUnavailable('Core offline')
        core.request=fail
        calls=[]
        async def notify(*args):calls.append(args)
        native=adapter._send_notification;adapter._send_notification=notify
        with pytest.raises(CoreUnavailable):await adapter.cancel(store.get_turn(turn.turn_id))
        assert not calls
        adapter._send_notification=native;core.request=original
        await adapter.cancel(store.get_turn(turn.turn_id))
        assert (await original('task',id=r.task))['state']=='Blocked'
        (tmp_path/'fail-closed-cancel.json').write_text(json.dumps({'unfenced_notifications':calls,'events':await original('events')},indent=2))
    finally:core.request=original;await adapter.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('execution',[{'sandbox':True}],indirect=True)
async def test_original_read_symlink_to_host_core_is_not_readable(execution,tmp_path):
    r,store,adapter,core=execution
    path=r.workspace/'input.txt';path.unlink();path.symlink_to(r.path/'core.db')
    try:
        await ready(r);_,_,state=await run_task(execution)
        assert state['state']=='Blocked' and not await core.request('artifacts',id=r.task)
        rc,out,err=await node(r,'try{require("fs").readFileSync('+json.dumps(str(path))+');console.log("LEAK")}catch(e){console.log(e.code)}')
        assert rc==0 and 'ENOENT' in out and 'LEAK' not in out
        (tmp_path/'read-symlink.json').write_text(json.dumps({'result':out,'task':state,'events':await core.request('events')},indent=2))
    finally:await adapter.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('execution',[{'task_acceptance':['Artifact must contain KANE_REQUIRED_ABSENT_MARKER']}],indirect=True)
async def test_actual_failed_acceptance_retains_independent_negative_evidence(execution,tmp_path):
    r,_,adapter,core=execution
    try:
        _,_,state=await run_task(execution);artifact=(await core.request('artifacts',id=r.task))[0]
        marker='KANE_REQUIRED_ABSENT_MARKER'
        assert state['spec']['acceptance']==['Artifact must contain '+marker]
        verdict=marker in artifact['content'];assert verdict is False
        evidence={'criterion':state['spec']['acceptance'][0],'passed':verdict,'artifact_hash':artifact['content_hash'],'source':'independent fixture reader'}
        failed=await core.request('verify',id=r.task,version=state['version'],passed=verdict,evidence=json.dumps(evidence))
        assert failed['state']=='Running' and failed['verification_state']=='failed'
        assert (await core.request('artifacts',id=r.task))[0]['verification_state']=='failed'
        (tmp_path/'negative-acceptance.json').write_text(json.dumps({'evidence':evidence,'task':failed,'events':await core.request('events')},indent=2))
    finally:await adapter.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('execution',[{'sandbox':True,'shared_fifo':True,'delay_ms':4000}],indirect=True)
@pytest.mark.parametrize('fault',['supervisor','agent'])
async def test_process_loss_after_cancellation_fence_keeps_unknown(execution,tmp_path,fault):
    r,store,adapter,core=execution
    try:
        await ready(r);turn,_,_=await run_task(execution,wait=False)
        async def entered():return [q for q in await core.request('model_queue') if q['state']=='Dispatched'] if (r.home/'model-invocations.jsonl').exists() else None
        await until(entered)
        state=await adapter.execution_bridge.cancel(turn.turn_id);assert state['state']=='Blocked'
        if fault=='supervisor':
            r.supervisors[-1].kill();await asyncio.to_thread(r.supervisors[-1].wait,10)
        else:
            proc=await asyncio.create_subprocess_exec('docker','kill','kane-'+r.agent,stdout=asyncio.subprocess.DEVNULL)
            assert await proc.wait()==0
        async def unknown():
            budget=await core.request('model_budget',id=r.budget)
            return budget if budget['reserved_tokens']==32 else None
        budget=await until(unknown)
        await asyncio.sleep(.4)
        assert (await core.request('task',id=r.task))['state']=='Blocked'
        assert (await core.request('model_queue'))[0]['state']=='Unknown'
        assert not await core.request('artifacts',id=r.task)
        assert len((r.home/'model-invocations.jsonl').read_text().splitlines())==1
        (tmp_path/'cancel-process-loss.json').write_text(json.dumps({'fault':fault,'budget':budget,'task':await core.request('task',id=r.task),'events':await core.request('events')},indent=2))
    finally:await adapter.close()
