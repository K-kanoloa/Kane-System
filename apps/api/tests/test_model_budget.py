"""PR-3A: original Kanaloa runtime, restricted original read, synthetic model only."""
import asyncio
import json
import os
import signal
import subprocess

import pytest

from test_system_execution import execution, run_task
from test_supervised_runtime import BIN, wait_for
from app.system.client import CoreRejected, CoreUnavailable

pytestmark = pytest.mark.skipif(os.getenv('KANE_PR3A_TEST') != '1', reason='opt-in real Kanaloa model-budget integration')

def invocations(r):
    path = r.path/'model-invocations.jsonl'
    return path.read_text().splitlines() if path.exists() else []

async def budget(r, core):
    return await core.request('model_budget', id=r.budget)

@pytest.mark.asyncio
async def test_final_usage_debits_once_and_context_is_not_billing(execution):
    r, store, adapter, core = execution
    try:
        t, _, state = await run_task(execution)
        assert state['state'] == 'Verifying'
        b = await budget(r, core)
        assert (b['used_tokens'], b['reserved_tokens'], b['active_calls']) == (42, 0, 0)
        events = await core.request('events')
        reservations = [e for e in events if e['kind']=='model_reserved']
        settlements = [e for e in events if e['kind']=='model_settled']
        assert len(reservations)==len(settlements)==len(invocations(r))==2
        assert all(a['seq'] < z['seq'] for a,z in zip(reservations,settlements))
        starts=[e for e in events if e['kind']=='llm_call_start']
        assert {e['payload']['call_id'] for e in reservations} == {e['payload']['fact']['call_id'] for e in starts}
        assert all(a['seq'] < b['seq'] for a,b in zip(reservations,starts))
        for event in settlements:
            p=event['payload']
            await core.request('settle_model',turn_id=t.turn_id,call_id=p['call_id'],receipt=p['receipt'])
        assert (await budget(r,core))['used_tokens']==42
    finally:
        await adapter.close()

@pytest.mark.parametrize('execution',[{'budget_tokens':31},{'budget_tokens':46}],indirect=True)
@pytest.mark.asyncio
async def test_insufficient_quota_rejects_before_backend_entry(execution):
    r, _, adapter, core=execution
    try:
        _, _, state=await run_task(execution)
        assert state['state']=='Blocked'
        b=await budget(r,core)
        expected=0 if b['token_limit']==31 else 1
        assert len(invocations(r))==expected
        assert b['used_tokens']==15*expected and b['reserved_tokens']==0
        assert not await core.request('artifacts',id=r.task)
        assert any(e['kind']=='model_budget_denied' for e in await core.request('events'))
    finally:
        await adapter.close()

@pytest.mark.asyncio
async def test_committed_permit_reply_loss_never_calls_model(execution):
    r, _, adapter, core=execution
    original=core.request
    async def lost(op,**kw):
        result=await original(op,**kw)
        if op=='reserve_model':
            raise CoreUnavailable('injected lost permit reply')
        return result
    core.request=lost
    try:
        t, _, state=await run_task(execution)
        assert state['state']=='Blocked' and not invocations(r)
        b=await budget(r,core)
        assert b['reserved_tokens']==32 and b['used_tokens']==0
        event=next(e for e in await original('events') if e['kind']=='model_reserved')
        with pytest.raises(CoreRejected):
            await original('reserve_model',turn_id=t.turn_id,call_id=event['payload']['call_id'],native_session_ref=t.native_session_ref,provider='kane-fixture',model='deterministic-v1')
    finally:
        await adapter.close()

@pytest.mark.asyncio
async def test_lost_settlement_reply_charges_once_and_stops_next_call(execution):
    r, _, adapter, core=execution
    original=core.request
    saved=[]
    async def lost(op,**kw):
        result=await original(op,**kw)
        if op=='settle_model':
            saved.append(kw)
            raise CoreUnavailable('injected lost settlement reply')
        return result
    core.request=lost
    try:
        _, _, state=await run_task(execution)
        assert state['state']=='Blocked'
        assert len(invocations(r))==1
        assert (await budget(r,core))['used_tokens']==15
        assert len(saved)==1
        await original('settle_model',**saved[0])
        assert (await budget(r,core))['used_tokens']==15
    finally:
        await adapter.close()

@pytest.mark.asyncio
async def test_core_process_unavailable_fails_before_backend(execution):
    r, _, adapter, core=execution
    original=core.request
    failed=asyncio.Event()
    async def stop(op,**kw):
        if op=='reserve_model':
            r.core.terminate();r.core.wait(timeout=5)
            try:
                return await original(op,**kw)
            finally:
                failed.set()
        return await original(op,**kw)
    core.request=stop
    try:
        await run_task(execution,wait=False)
        await asyncio.wait_for(failed.wait(),10)
        await asyncio.sleep(.2)
        assert not invocations(r)
    finally:
        core.request=original
        r.core=subprocess.Popen([str(BIN),'--db',str(r.path/'core.db'),'--socket',str(r.core_socket),'serve'],stderr=r.log)
        # Socket path may exist already while previous service is down.
        def healthy():
            try:
                return r.call({'op':'health'})
            except subprocess.CalledProcessError:
                return False
        wait_for(healthy)
        await adapter.close()
    assert (await budget(r,core))['used_tokens']==0
    assert not invocations(r)

@pytest.mark.parametrize('execution',[{'mode':'missing_usage'},{'mode':'throw_after_usage'},{'mode':'known_failure'},{'mode':'overrun'}],indirect=True)
@pytest.mark.asyncio
async def test_usage_failure_never_refunds_unknown_or_hides_overrun(execution):
    r, _, adapter, core=execution
    try:
        _, _, state=await run_task(execution)
        assert state['state']=='Blocked'
        assert len(invocations(r))==1
        b=await budget(r,core)
        mode=json.loads((r.path/'fixture.yml').read_text())[-1]['insert'][0]['config']['mode']
        if mode=='overrun':
            assert b['used_tokens']==45 and b['frozen'] is True and b['reserved_tokens']==0
        else:
            assert b['used_tokens']==0 and b['reserved_tokens']==32
        assert not await core.request('artifacts',id=r.task)
    finally:
        await adapter.close()

@pytest.mark.parametrize('execution',[{'budget_hook':False}],indirect=True)
@pytest.mark.asyncio
async def test_missing_hook_is_refused_without_legacy_fallback(execution):
    r, store, adapter, core=execution
    try:
        t,_,_=await run_task(execution,wait=False)
        async with asyncio.timeout(10):
            while store.get_turn(t.turn_id).status not in {'failed','interrupted','finished'}:
                await asyncio.sleep(.05)
        assert not invocations(r)
        assert not any(e['kind']=='prompt_dispatch' for e in await core.request('events'))
    finally:
        await adapter.close()

@pytest.mark.parametrize('execution',[{'delay_ms':3000}],indirect=True)
@pytest.mark.asyncio
async def test_actual_runtime_kill_retains_reserved_quota(execution):
    r, _, adapter, core=execution
    try:
        await run_task(execution,wait=False)
        async with asyncio.timeout(10):
            while not invocations(r): await asyncio.sleep(.03)
        first=r.status()['pid'];os.kill(first,signal.SIGKILL)
        async with asyncio.timeout(10):
            while r.status()['pid']==first: await asyncio.sleep(.05)
        b=await budget(r,core)
        assert b['used_tokens']==0 and b['reserved_tokens']==32
        assert len(invocations(r))==1
        assert (await core.request('task',id=r.task))['state']=='Blocked'
    finally:
        await adapter.close()

@pytest.mark.asyncio
async def test_budget_relay_unavailable_has_no_provider_fallback(execution):
    r, _, adapter, core=execution
    original=adapter.execution_bridge.admit
    async def admit_then_close(*args):
        await original(*args)
        await adapter.execution_bridge.budget_relay.close()
    adapter.execution_bridge.admit=admit_then_close
    try:
        _,_,state=await run_task(execution)
        assert state['state']=='Blocked' and not invocations(r)
        assert (await budget(r,core))['reserved_tokens']==0
    finally:
        await adapter.close()

@pytest.mark.asyncio
async def test_unbound_or_owner_operations_cannot_use_narrow_relay(execution):
    r, _, adapter, core=execution
    try:
        await run_task(execution)
        for payload in [
            {'op':'create_budget','tokens':9999,'native_session_ref':'unknown','call_id':'attack'},
            {'op':'reserve','native_session_ref':'unknown','call_id':'attack','provider':'kane-fixture','model':'deterministic-v1'},
        ]:
            reader,writer=await asyncio.open_unix_connection(str(r.socket)+'.budget.sock')
            writer.write((json.dumps(payload)+'\n').encode());await writer.drain()
            reply=json.loads(await reader.readline());assert reply['ok'] is False
            writer.close();await writer.wait_closed()
        assert (await budget(r,core))['used_tokens']==42
        assert len(invocations(r))==2
    finally:
        await adapter.close()
