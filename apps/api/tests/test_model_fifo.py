"""P0-07: three real Kanaloa/DSH containers, one Rust shared Mock FIFO.

No production provider, new Agent loop, tool implementation or network fallback.
FIFO is asserted against Core enqueue sequence, never Python task spawn order.
"""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace

import pytest
from app.system.client import CoreRejected, CoreUnavailable
from test_system_execution import execution, run_task
from test_supervised_runtime import BIN

pytestmark = pytest.mark.skipif(os.getenv('KANE_FIFO_TEST') != '1', reason='opt-in three Kanaloa Rust shared Mock FIFO')


@pytest.fixture
def fifo_agents(tmp_path, monkeypatch, request):
    overrides = getattr(request, 'param', [{}, {}, {}])
    generators = []
    items = []
    try:
        for i in range(3):
            case = tmp_path / str(i)
            case.mkdir()
            config = {'sandbox': True, 'shared_fifo': True, 'delay_ms': 2000 if i == 0 else 50} | overrides[i]
            if items:
                config['core_socket'] = str(items[0][0].core_socket)
            g = execution.__wrapped__(case, monkeypatch, SimpleNamespace(param=config))
            generators.append(g)
            items.append(next(g))
        assert len({str(x[0].core_socket) for x in items}) == 1
        yield items
    finally:
        # A fault test may have killed Core. Restore the SAME DB so teardown and
        # recovery evidence use its durable Unknown state, never a replacement DB.
        if items and items[0][0].core.poll() is not None:
            restart_core(items[0][0])
        for g in reversed(generators):
            try:
                next(g)
            except StopIteration:
                pass


def restart_core(r):
    r.core = subprocess.Popen([str(BIN), '--db', str(r.path / 'core.db'), '--socket', str(r.core_socket), 'serve'], stderr=r.log)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            r.call({'op': 'health'})
            return
        except subprocess.CalledProcessError:
            time.sleep(.03)
    raise AssertionError('Core did not restart')


def invocations(item):
    path = item[0].home / 'model-invocations.jsonl'
    return [json.loads(s) for s in path.read_text().splitlines()] if path.exists() else []


async def until(fn, seconds=20):
    async with asyncio.timeout(seconds):
        while True:
            value = await fn()
            if value:
                return value
            await asyncio.sleep(.025)


async def for_agent(core, agent, state):
    return [r for r in await core.request('model_queue') if r['agent_id'] == agent and r['state'] == state]


async def begin_in_order(items):
    await asyncio.gather(*(x[2].activate() for x in items))
    core = items[0][3]
    turns = []
    for i, item in enumerate(items):
        turn, _, _ = await run_task(item, wait=False)
        turns.append(turn)
        await until(lambda item=item: for_agent(core, item[0].agent, 'Dispatched' if i == 0 else 'Queued'))
    return turns


async def completed(item):
    async def check():
        state = await item[3].request('task', id=item[0].task)
        return state if state['state'] in ['Verifying', 'Blocked', 'Failed'] else None
    return await until(check)


async def save(items, path, scenario):
    core = items[0][3]
    rows = await core.request('model_queue')
    events = await core.request('events')
    result = {'scenario': scenario, 'mock': True, 'rows': rows, 'events': events,
              'agents': [{'agent': x[0].agent, 'task': x[0].task, 'workspace': x[0].ws, 'budget': x[0].budget,
                          'task_state': await x[3].request('task', id=x[0].task),
                          'budget_state': await x[3].request('model_budget', id=x[0].budget),
                          'provider_entries': invocations(x)} for x in items]}
    (path / 'fifo-evidence.json').write_text(json.dumps(result, indent=2))
    return result


async def close_all(items):
    await asyncio.gather(*(x[2].close() for x in items))


@pytest.mark.asyncio
async def test_three_real_agents_use_shared_fifo_with_call_identity_and_wait_metrics(fifo_agents, tmp_path):
    items = fifo_agents
    try:
        await begin_in_order(items)
        outcomes = await asyncio.gather(*(completed(x) for x in items))
        assert all(x['state'] == 'Verifying' for x in outcomes), outcomes
        result = await save(items, tmp_path, 'fifo-normal')
        rows = result['rows']
        assert len(rows) == 6 and all(x['state'] == 'Completed' for x in rows)
        dispatched = [e for e in result['events'] if e['kind'] == 'model_dispatched']
        order = [e['payload']['queue_seq'] for e in dispatched]
        assert order == sorted(order) == [x['queue_seq'] for x in rows]
        assert {x['model_id'] for x in rows} == {'kane-fixture/deterministic-v1'}
        assert len({x['agent_id'] for x in rows}) == 3
        assert rows[1]['queue_wait_ms'] > 500 and rows[2]['queue_wait_ms'] > 500
        for row in rows:
            call = row['call_id']
            starts = [e for e in result['events'] if e['kind'] == 'llm_call_start' and e['payload']['fact']['call_id'] == call]
            ends = [e for e in result['events'] if e['kind'] == 'llm_call_result' and e['payload']['fact']['call_id'] == call]
            dispatch = next(e for e in dispatched if e['payload']['call_id'] == call)
            enqueue = next(e for e in result['events'] if e['kind'] == 'model_enqueued' and e['payload']['call_id'] == call)
            assert len(starts) == len(ends) == 1
            assert enqueue['seq'] < dispatch['seq'] < starts[0]['seq'] < ends[0]['seq']
            assert row['native_session_ref'] == starts[0]['payload']['fact']['native_session_ref']
            actual = [p for a in result['agents'] for p in a['provider_entries'] if p.get('call_id') == call]
            assert len(actual) == 1
            assert actual[0]['entered_ms'] >= row['dispatched_ms']
        for item, state in zip(items, outcomes):
            artifacts = await item[3].request('artifacts', id=item[0].task)
            assert len(artifacts) == 1 and artifacts[0]['content'] == '# Verified fixture\n\nKANE_PR2_FIXTURE\n'
            budget = await item[3].request('model_budget', id=item[0].budget)
            assert (budget['used_tokens'], budget['reserved_tokens']) == (42, 0)
            done = await item[3].request('verify', id=item[0].task, version=state['version'], passed=True,
                                         evidence='External fixed Markdown and six FIFO call/reservation/usage correlations; Mock only')
            assert done['state'] == 'Done'
    finally:
        await close_all(items)


@pytest.mark.asyncio
async def test_cancel_waiting_native_call_does_not_charge_or_block_next_agent(fifo_agents, tmp_path):
    items = fifo_agents
    try:
        turns = await begin_in_order(items)
        await items[1][2].cancel(items[1][1].get_turn(turns[1].turn_id))
        await until(lambda: for_agent(items[0][3], items[1][0].agent, 'Cancelled'))
        good = await asyncio.gather(completed(items[0]), completed(items[2]))
        assert all(x['state'] == 'Verifying' for x in good)
        assert not invocations(items[1])
        budget = await items[1][3].request('model_budget', id=items[1][0].budget)
        assert (budget['used_tokens'], budget['reserved_tokens']) == (0, 0)
        await save(items, tmp_path, 'cancel-queued')
    finally:
        await close_all(items)


@pytest.mark.asyncio
@pytest.mark.parametrize('fifo_agents', [[{}, {'budget_tokens': 31}, {}]], indirect=True)
async def test_insufficient_budget_is_denied_before_actual_backend_and_queue_advances(fifo_agents, tmp_path):
    items = fifo_agents
    try:
        await begin_in_order(items)
        outcomes = await asyncio.gather(*(completed(x) for x in items))
        assert outcomes[0]['state'] == outcomes[2]['state'] == 'Verifying'
        assert outcomes[1]['state'] == 'Blocked'
        assert not invocations(items[1])
        denied = await for_agent(items[0][3], items[1][0].agent, 'Denied')
        assert len(denied) == 1 and denied[0]['dispatched_ms'] is None
        budget = await items[1][3].request('model_budget', id=items[1][0].budget)
        assert (budget['used_tokens'], budget['reserved_tokens']) == (0, 0)
        await save(items, tmp_path, 'quota-denied')
    finally:
        await close_all(items)


@pytest.mark.asyncio
async def test_duplicate_enqueue_claim_and_receipt_do_not_repeat_actual_calls(fifo_agents, tmp_path):
    items = fifo_agents
    original = items[1][3].request
    checked = {'enqueue': 0, 'claim': 0, 'receipt': 0}
    async def duplicate(op, **kw):
        response = await original(op, **kw)
        if op == 'reserve_model':
            again = await original(op, **kw)
            assert again == response and response['state'] == 'Queued'
            checked['enqueue'] += 1
        if op == 'claim_model' and response.get('permit'):
            with pytest.raises(CoreRejected):
                await original(op, **kw)
            checked['claim'] += 1
        if op == 'settle_model':
            assert await original(op, **kw) == response
            checked['receipt'] += 1
        return response
    items[1][3].request = duplicate
    try:
        await begin_in_order(items)
        assert all(s['state'] == 'Verifying' for s in await asyncio.gather(*(completed(x) for x in items)))
        assert checked == {'enqueue': 2, 'claim': 2, 'receipt': 2}
        result = await save(items, tmp_path, 'duplicate-receipts')
        assert len(result['rows']) == 6
        assert all(len(invocations(x)) == 2 for x in items)
        assert (await original('model_budget', id=items[1][0].budget))['used_tokens'] == 42
    finally:
        await close_all(items)


@pytest.mark.asyncio
async def test_lost_claim_reply_keeps_unknown_slot_and_full_hold_without_backend_call(fifo_agents, tmp_path):
    items = fifo_agents
    original = items[1][3].request
    lost = False
    async def lose(op, **kw):
        nonlocal lost
        response = await original(op, **kw)
        if op == 'claim_model' and response.get('permit') and not lost:
            lost = True
            raise CoreUnavailable('injected lost committed dispatch permit')
        return response
    items[1][3].request = lose
    try:
        await begin_in_order(items)
        unknown = (await until(lambda: for_agent(items[0][3], items[1][0].agent, 'Unknown')))[0]
        assert lost
        with pytest.raises(CoreRejected):
            await original('claim_model', turn_id='pr2-turn-' + items[1][0].agent,
                           call_id=unknown['call_id'], native_session_ref=unknown['native_session_ref'])
        await asyncio.sleep(.3)
        assert not invocations(items[1]) and not invocations(items[2])
        budget = await original('model_budget', id=items[1][0].budget)
        assert (budget['used_tokens'], budget['reserved_tokens']) == (0, 32)
        assert (await completed(items[1]))['state'] == 'Blocked'
        await save(items, tmp_path, 'lost-claim-reply')
    finally:
        await close_all(items)


@pytest.mark.asyncio
async def test_lost_settlement_reply_does_not_double_charge_and_other_agents_continue(fifo_agents, tmp_path):
    items = fifo_agents
    original = items[1][3].request
    lost = False
    async def lose(op, **kw):
        nonlocal lost
        response = await original(op, **kw)
        if op == 'settle_model' and not lost:
            lost = True
            assert await original(op, **kw) == response
            raise CoreUnavailable('injected lost committed settlement reply')
        return response
    items[1][3].request = lose
    try:
        await begin_in_order(items)
        outcomes = await asyncio.gather(*(completed(x) for x in items))
        assert outcomes[0]['state'] == outcomes[2]['state'] == 'Verifying'
        assert lost and outcomes[1]['state'] == 'Blocked'
        assert len(invocations(items[1])) == 1
        budget = await original('model_budget', id=items[1][0].budget)
        assert (budget['used_tokens'], budget['reserved_tokens']) == (15, 0)
        rows = await for_agent(items[0][3], items[1][0].agent, 'Completed')
        assert len(rows) == 1
        await save(items, tmp_path, 'lost-settlement-reply')
    finally:
        await close_all(items)


@pytest.mark.asyncio
@pytest.mark.parametrize('fifo_agents', [[{}, {'mode': 'throw_after_usage'}, {}]], indirect=True)
async def test_truncated_provider_usage_retains_unknown_and_blocks_shared_route(fifo_agents, tmp_path):
    items = fifo_agents
    try:
        await begin_in_order(items)
        await until(lambda: for_agent(items[0][3], items[1][0].agent, 'Unknown'))
        await asyncio.sleep(.3)
        assert len(invocations(items[1])) == 1 and not invocations(items[2])
        budget = await items[1][3].request('model_budget', id=items[1][0].budget)
        assert (budget['used_tokens'], budget['reserved_tokens']) == (0, 32)
        assert (await completed(items[1]))['state'] == 'Blocked'
        await save(items, tmp_path, 'truncated-usage')
    finally:
        await close_all(items)


@pytest.mark.asyncio
async def test_cancel_after_provider_entry_retains_uncertainty_and_does_not_dispatch_next(fifo_agents, tmp_path):
    items = fifo_agents
    try:
        turns = await begin_in_order(items)
        assert len(invocations(items[0])) == 1
        await items[0][2].cancel(items[0][1].get_turn(turns[0].turn_id))
        await until(lambda: for_agent(items[0][3], items[0][0].agent, 'Unknown'))
        await asyncio.sleep(.3)
        assert not invocations(items[1]) and not invocations(items[2])
        budget = await items[0][3].request('model_budget', id=items[0][0].budget)
        assert budget['reserved_tokens'] == 32
        await save(items, tmp_path, 'cancel-after-entry')
    finally:
        await close_all(items)


@pytest.mark.asyncio
async def test_actual_core_loss_and_restart_is_fail_closed_with_no_model_replay(fifo_agents, tmp_path):
    items = fifo_agents
    first = items[0][0]
    try:
        await begin_in_order(items)
        assert len(invocations(items[0])) == 1
        first.core.kill()
        first.core.wait(timeout=5)
        # Stop the already-entered native call while Core is absent. No final
        # provider receipt exists, so recovery must retain its original hold.
        result = subprocess.run(['docker', 'kill', 'kane-' + first.agent], capture_output=True)
        assert result.returncode == 0
        await asyncio.sleep(.2)
        assert not invocations(items[1]) and not invocations(items[2])
        restart_core(first)
        rows = await items[0][3].request('model_queue')
        assert len(rows) == 3
        assert rows[0]['state'] == 'Unknown'
        assert rows[1]['state'] == rows[2]['state'] == 'Cancelled'
        assert (await items[0][3].request('model_budget', id=first.budget))['reserved_tokens'] == 32
        await asyncio.sleep(.3)
        assert len(invocations(items[0])) == 1
        assert not invocations(items[1]) and not invocations(items[2])
        assert len([e for e in await items[0][3].request('events') if e['kind'] == 'model_dispatched']) == 1
        await save(items, tmp_path, 'core-loss-restart')
    finally:
        if first.core.poll() is not None:
            restart_core(first)
        await close_all(items)


@pytest.mark.asyncio
async def test_required_fifo_refuses_a_direct_permit_reply_without_backend_fallback(fifo_agents, tmp_path):
    items = fifo_agents
    original = items[1][3].request
    async def legacy_reply(op, **kw):
        result = await original(op, **kw)
        if op == 'reserve_model':
            return {'permit': True, 'call_id': kw['call_id'], 'reserved_tokens': 32}
        return result
    items[1][3].request = legacy_reply
    try:
        await asyncio.gather(*(x[2].activate() for x in items))
        await run_task(items[0], wait=False)
        await until(lambda: for_agent(items[0][3], items[0][0].agent, 'Dispatched'))
        await run_task(items[1], wait=False)
        assert (await completed(items[1]))['state'] == 'Blocked'
        await run_task(items[2], wait=False)
        assert all(s['state'] == 'Verifying' for s in await asyncio.gather(completed(items[0]), completed(items[2])))
        assert not invocations(items[1])
        assert (await original('model_budget', id=items[1][0].budget))['reserved_tokens'] == 0
        await save(items, tmp_path, 'refuse-direct-permit-reply')
    finally:
        await close_all(items)
