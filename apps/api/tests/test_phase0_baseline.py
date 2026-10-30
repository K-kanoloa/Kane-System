"""Fixed N=5 actual Kanaloa workflows; scripted model, independent byte oracle.

Complexity denotes number of tool/loop steps, never actual model difficulty.
All attempts (including failures) write a result; no automatic retry/filtering.
"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace

import pytest
from test_system_execution import execution, run_task
from test_supervised_runtime import ROOT

SUITE = json.loads((ROOT / 'bench/phase0/tasks.json').read_text())


def accept(task, artifact, actual_inputs):
    """External oracle: never use the provider's returned text as expected data."""
    expected = task['expected'].encode()
    return (
        artifact['format'] == 'text/markdown' and artifact['editable']
        and artifact['content'].encode() == expected
        and artifact['content_hash'] == hashlib.sha256(expected).hexdigest()
        and actual_inputs == {name: text.encode() for name, text in task['files'].items()}
    )


def test_phase0_external_oracle_rejects_seeded_defects():
    task = SUITE['tasks'][0]
    good = {'format': 'text/markdown', 'editable': True, 'content': task['expected'],
            'content_hash': hashlib.sha256(task['expected'].encode()).hexdigest()}
    inputs = {name: text.encode() for name, text in task['files'].items()}
    assert accept(task, good, inputs)
    for defect in ({'content': task['expected'] + 'invented'}, {'content_hash': '0' * 64},
                   {'editable': False}, {'format': 'image/png'}):
        assert not accept(task, good | defect, inputs)
    assert not accept(task, good, inputs | {'input.txt': b'overwritten'})


@pytest.mark.skipif(os.getenv('KANE_PHASE0_TEST') != '1', reason='opt-in Phase 0 Docker Direct baseline N=5/task')
@pytest.mark.asyncio
@pytest.mark.parametrize('task', SUITE['tasks'], ids=lambda t: t['id'])
@pytest.mark.parametrize('repeat', range(1, SUITE['runs_per_task'] + 1))
async def test_phase0_direct_baseline(task, repeat, tmp_path, monkeypatch):
    record = {'suite': SUITE['version'], 'task': task['id'], 'repeat': repeat,
              'mode': 'Direct', 'mock': True, 'verified_success': False,
              'provider_cost': None, 'cost_reason': 'synthetic usage; no provider invoice',
              'sampling_seed': None, 'sampling_reason': 'scripted deterministic provider',
              'budget_tokens': SUITE['budget_tokens']}
    started = time.perf_counter()
    generator = None
    adapter = None
    try:
        config = {'sandbox': True, 'shared_fifo': True, 'tool_grant': False, 'phase0_reads': task['reads'],
                  'budget_tokens': SUITE['budget_tokens'], 'task_goal': task['goal'],
                  'task_acceptance': ['Exact independently fixed UTF-8 Markdown; editable',
                                      'Every declared read executed once; all inputs unchanged']}
        generator = execution.__wrapped__(tmp_path, monkeypatch, SimpleNamespace(param=config))
        item = next(generator)
        r, store, adapter, core = item
        for name, content in task['files'].items():
            (r.workspace / name).write_bytes(content.encode())
            r.call({'op': 'grant_tool', 'id': r.agent, 'tool': 'read',
                    'arguments': {'file_path': name}, 'expires_at': int(time.time()) + 300})
        record['setup_ms'] = (time.perf_counter() - started) * 1000
        record['bindings'] = {'agent': r.agent, 'task': r.task, 'workspace': r.ws, 'budget': r.budget}
        execution_started = time.perf_counter()
        turn, _, state = await run_task(item)
        record['execution_ms'] = (time.perf_counter() - execution_started) * 1000
        assert state['state'] == 'Verifying', state
        artifacts = await core.request('artifacts', id=r.task)
        assert len(artifacts) == 1
        verify_started = time.perf_counter()
        assert accept(task, artifacts[0], {name: (r.workspace / name).read_bytes() for name in task['files']})
        calls = [json.loads(line) for line in (r.home / 'tool-invocations.jsonl').read_text().splitlines()]
        assert [c['call_id'] for c in calls] == [f'phase0-read-{i}' for i in range(len(task['reads']))]
        assert all(c['tool'] == 'read' for c in calls)
        events = await core.request('events')
        results = [e for e in events if e['kind'] == 'llm_call_result']
        assert len(results) == len(task['reads']) + 1
        dispatches = [e for e in events if e['kind'] == 'model_dispatched']
        queue = await core.request('model_queue')
        assert len(dispatches) == len(results) == len(queue)
        assert all(q['state'] == 'Completed' for q in queue)
        assert {q['call_id'] for q in queue} == {e['payload']['fact']['call_id'] for e in results}
        usage = sum(e['payload']['fact']['usage']['totalTokens'] for e in results)
        assert usage == 15 * len(task['reads']) + 27
        budget = await core.request('model_budget', id=r.budget)
        assert budget['used_tokens'] == usage and budget['reserved_tokens'] == 0, budget
        done = await core.request('verify', id=r.task, version=state['version'], passed=True,
                                  evidence=f"External phase0-fixed-workflows-v1 byte/hash/input/tool/usage checks; {task['id']} run {repeat}; mock only")
        assert done['state'] == 'Done'
        record['verification_ms'] = (time.perf_counter() - verify_started) * 1000
        # Read actual cgroup accounting without injecting a resource workload.
        node = json.loads(r.spec.read_text())['command'][0]
        code = "const f=require('fs');console.log(JSON.stringify(Object.fromEntries(['memory.peak','cpu.stat'].map(n=>[n,f.readFileSync('/sys/fs/cgroup/'+n,'utf8')]))))"
        counters = subprocess.check_output(['docker', 'exec', 'kane-' + r.agent, node, '-e', code], text=True)
        record.update(verified_success=True, synthetic_tokens=usage, model_calls=len(results),
                      tool_calls=len(calls), rework=0, errors=0, retries=0, budget=budget,
                      model_duration_ms=[e['payload']['fact']['duration_ms'] for e in results],
                      cgroup=json.loads(counters), artifact_hash=artifacts[0]['content_hash'],
                      artifact_id=artifacts[0]['id'], native_session=turn.native_session_ref,
                      scheduler='Core shared mock_fifo', queue=queue)
    except BaseException as error:
        record['error'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        try:
            if adapter is not None:
                await adapter.close()
            if generator is not None:
                try:
                    next(generator)
                except StopIteration:
                    pass
        finally:
            record['total_ms'] = (time.perf_counter() - started) * 1000
            (tmp_path / 'baseline-result.json').write_text(json.dumps(record, ensure_ascii=False, indent=2))
