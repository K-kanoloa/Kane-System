"""PR-3B original Kanaloa/DSH tools, bounded mock, disposable local files only."""
import asyncio
import json
import os
import signal
import subprocess

import pytest
from test_system_execution import execution, run_task
from test_supervised_runtime import BIN, wait_for
from app.system.client import CoreRejected, CoreUnavailable

pytestmark=pytest.mark.skipif(os.getenv('KANE_PR3B_TEST')!='1',reason='opt-in managed original tool integration')

def entries(r):
    p=r.path/'tool-invocations.jsonl'
    return [json.loads(s) for s in p.read_text().splitlines()] if p.exists() else []

async def actions(core):
    events=await core.request('events')
    return [await core.request('action',id=e['payload']['action']) for e in events if e['kind']=='intent']

@pytest.mark.asyncio
@pytest.mark.parametrize('execution',[{}, {'test_write':True}],indirect=True)
async def test_original_tools_have_durable_intent_claim_result_and_duplicate_receipt(execution):
    r,store,adapter,core=execution
    original=core.request
    async def check_before_claim(op,**kw):
        if op=='tool_claim':
            assert not entries(r)
            pending=await actions(core)
            assert len(pending)==1 and pending[0]['state']=='Pending'
        result=await original(op,**kw)
        if op=='tool_result':
            assert len(entries(r))==1
            assert await original(op,**kw)==result
        return result
    core.request=check_before_claim
    try:
        turn,_,state=await run_task(execution)
        assert state['state']=='Verifying'
        a=(await actions(core))[0]
        assert a['state']=='Confirmed'
        assert len(entries(r))==1
        events=await original('events')
        seq=[next(e['seq'] for e in events if e['kind']==k) for k in ['intent','dispatch','result','artifact_submitted']]
        assert seq==sorted(seq)
        assert r.ws in a['payload']['target'] and r.task in a['payload']['target']
        assert a['payload']['body']['native_session_ref']==turn.native_session_ref
        with pytest.raises(CoreRejected):
            await original('tool_claim',turn_id=turn.turn_id,native_session_ref=turn.native_session_ref,call_id='fixture-read')
        if (r.workspace/'output.txt').exists():
            assert (r.workspace/'output.txt').read_text()=='KANE_PR3B_SAFE_FIXTURE\n'
    finally:
        await adapter.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('execution',[{'tool_grant':False,'test_write':True}],indirect=True)
async def test_no_permission_or_missing_hook_never_enters_tool(execution):
    r,store,adapter,core=execution
    try:
        _,_,state=await run_task(execution)
        assert state['state']=='Blocked'
        assert not entries(r) and not (r.workspace/'output.txt').exists()
        assert not await actions(core)
    finally:
        await adapter.close()

@pytest.mark.asyncio
async def test_revoked_between_intent_and_claim_denies_new_dispatch(execution):
    r,store,adapter,core=execution
    original=core.request
    async def revoke(op,**kw):
        if op=='tool_claim':
            await original('revoke_capability',id=r.tool_cap)
        return await original(op,**kw)
    core.request=revoke
    try:
        _,_,state=await run_task(execution)
        assert state['state']=='Blocked'
        assert not entries(r)
        a=await actions(core)
        assert len(a)==1 and a[0]['state']=='NotExecuted'
        assert any(e['kind']=='tool_denied' for e in await original('events'))
    finally:
        await adapter.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('failure',['prepare_before','prepare_after','claim_before','claim_after','result_before','result_after'])
@pytest.mark.parametrize('execution',[{'test_write':True}],indirect=True)
async def test_lost_replies_never_replay_tool_and_unknown_is_reconciled(execution,failure):
    r,store,adapter,core=execution
    original=core.request
    stage,timing=failure.split('_')
    calls=0
    async def disconnect(op,**kw):
        nonlocal calls
        if op=='tool_'+stage:
            calls+=1
            if timing=='before': raise CoreUnavailable('injected unavailable before request')
        result=await original(op,**kw)
        if op=='tool_'+stage: raise CoreUnavailable('injected lost committed reply')
        return result
    core.request=disconnect
    try:
        turn,_,state=await run_task(execution)
        assert state['state']=='Blocked'
        assert calls==1
        expected=1 if stage=='result' else 0
        assert len(entries(r))==expected
        assert (r.workspace/'output.txt').exists()==bool(expected)
        a=await actions(core)
        if failure=='prepare_before': assert not a
        else:
            expected_state='Confirmed' if failure=='result_after' else 'Unknown' if failure in {'claim_after','result_before'} else 'NotExecuted'
            assert a[0]['state']==expected_state
            with pytest.raises(CoreRejected):
                await original('tool_claim',turn_id=turn.turn_id,native_session_ref=turn.native_session_ref,call_id='fixture-read')
            if expected_state=='Unknown':
                events=await original('events')
                assert any(e['kind']=='reconcile' and e['payload'].get('action')==a[0]['id'] for e in events)
        assert len(entries(r))==expected
        assert not await original('artifacts',id=r.task)
    finally:
        await adapter.close()

@pytest.mark.asyncio
async def test_core_restart_after_committed_claim_marks_unknown_and_prevents_body(execution):
    r,store,adapter,core=execution
    original=core.request
    async def restart(op,**kw):
        result=await original(op,**kw)
        if op=='tool_claim':
            r.core.kill();r.core.wait(timeout=5)
            r.core=subprocess.Popen([str(BIN),'--db',str(r.path/'core.db'),'--socket',str(r.core_socket),'serve'],stderr=r.log)
            for _ in range(100):
                try:
                    a=await actions(core)
                    if a and a[0]['state']=='Unknown': break
                except (CoreUnavailable,CoreRejected): pass
                await asyncio.sleep(.03)
            else: raise AssertionError('startup recovery missing')
            raise CoreUnavailable('lost claim across Core restart')
        return result
    core.request=restart
    try:
        await run_task(execution,wait=False)
        async with asyncio.timeout(15):
            while True:
                try:
                    state=await original('task',id=r.task)
                    if state['state']=='Blocked': break
                except CoreUnavailable: pass
                await asyncio.sleep(.03)
        assert state['state']=='Blocked'
        assert (await actions(core))[0]['state']=='Unknown'
        assert not entries(r)
    finally:
        await adapter.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('execution',[{'tool_hook':False}],indirect=True)
async def test_missing_tool_hook_denies_prompt_before_task_dispatch(execution):
    r,store,adapter,core=execution
    try:
        turn,_,_=await run_task(execution,wait=False)
        async with asyncio.timeout(10):
            while store.get_turn(turn.turn_id).status not in {'failed','interrupted','cancelled'}:
                await asyncio.sleep(.02)
        assert (await core.request('task',id=r.task))['state']=='Pending'
        assert not entries(r)
        assert not any(e['kind']=='prompt_dispatch' for e in await core.request('events'))
    finally:
        await adapter.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('execution',[{'test_write':True}],indirect=True)
async def test_real_agent_killed_after_original_write_keeps_unknown_without_replay(execution):
    r,store,adapter,core=execution
    original=core.request
    killed=asyncio.Event()
    async def kill_after_effect(op,**kw):
        if op=='tool_result':
            assert (r.workspace/'output.txt').read_text()=='KANE_PR3B_SAFE_FIXTURE\n'
            os.kill(r.status()['pid'],signal.SIGKILL)
            killed.set()
            raise CoreUnavailable('Agent lost after original tool effect')
        return await original(op,**kw)
    core.request=kill_after_effect
    try:
        old_pid=r.status()['pid']
        await run_task(execution,wait=False)
        await asyncio.wait_for(killed.wait(),10)
        async with asyncio.timeout(15):
            while True:
                a=await actions(core)
                status=r.status()
                if a and a[0]['state']=='Unknown' and status.get('pid') not in (None,old_pid): break
                await asyncio.sleep(.03)
        bound=status['registry']['agent']
        assert bound['id']==r.agent and bound['task_id']==r.task
        assert bound['workspace_id']==r.ws and bound['budget_id']==r.budget
        assert len(entries(r))==1
        assert (await core.request('task',id=r.task))['block_reason']=='reconciliation'
        assert not await core.request('artifacts',id=r.task)
        assert len([e for e in await core.request('events') if e['kind']=='prompt_dispatch'])==1
    finally:
        await adapter.close()
