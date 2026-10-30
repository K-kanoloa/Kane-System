"""Offline disposable VM: original Kanaloa on a real bounded persistent home."""
import asyncio
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from test_system_execution import execution, run_task
from test_sandbox_runtime import inspect, node
from test_supervised_runtime import BIN

pytestmark = pytest.mark.skipif(
    os.getenv("KANE_STORAGE_TEST") != "1",
    reason="opt-in disposable Linux VM with dedicated ext4 test home",
)

@pytest.fixture(autouse=True)
def disposable_vm_only():
    marker = Path("/etc/kane-storage-runtime-validation")
    assert marker.read_text() == "disposable-vm\n"
    assert os.environ["KANE_STORAGE_HOME"] == "/mnt/kane-home"
    assert Path("/mnt/kane-home").is_mount()


def file_evidence(home):
    return {
        str(p.relative_to(home)): {
            "bytes": p.stat().st_size,
            "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
        }
        for p in home.rglob("*")
        if p.is_file() and p.name != "capacity-fill"
    }


async def runtime_ready(r, timeout=120):
    """Software-emulated VM startup, separate from production ACP handshake QoS."""
    start = time.monotonic()
    async with asyncio.timeout(timeout):
        while True:
            process = await asyncio.create_subprocess_exec(
                'docker', 'inspect', 'kane-'+r.agent,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                out, _ = await asyncio.wait_for(process.communicate(), 10)
            except TimeoutError:
                process.kill()
                await process.wait()
                continue
            if process.returncode == 0:
                container = json.loads(out)[0]
                if container['State']['Running']:
                    logs = await asyncio.create_subprocess_exec(
                        'docker', 'logs', '--tail', '100', 'kane-'+r.agent,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    )
                    try:
                        stdout, _ = await asyncio.wait_for(logs.communicate(), 10)
                    except TimeoutError:
                        logs.kill()
                        await logs.wait()
                        continue
                    if all(name in stdout for name in (b'kanesys/budget_ready', b'kanesys/tool_ready')):
                        r.startup_observations = getattr(r, 'startup_observations', []) + [time.monotonic()-start]
                        return container
            await asyncio.sleep(.5)


async def execute_and_observe(execution):
    turn, message, _ = await run_task(execution, wait=False)
    r, _, _, core = execution
    async with asyncio.timeout(90):
        while True:
            state = await core.request('task', id=r.task)
            if state['state'] in ('Verifying', 'Blocked', 'Failed'):
                return turn, message, state
            await asyncio.sleep(.2)


async def restart_and_resume(r, adapter, turn):
    await adapter.close()
    async with asyncio.timeout(30):
        while inspect(r) is not None:
            await asyncio.sleep(.1)
    r.start()
    await runtime_ready(r)
    # Original ACP session/resume restores context, never resends the prompt.
    await adapter.resume(turn, [])


@pytest.mark.asyncio
@pytest.mark.parametrize("execution", [{"sandbox": True, "bounded_home": True, "sandbox_cpu_millis": 2000}], indirect=True)
async def test_bounded_home_persists_original_session_without_replay(execution, tmp_path):
    r, store, adapter, core = execution
    try:
        before = await runtime_ready(r)
        check = json.loads(subprocess.check_output(
            [str(BIN), "storage-check", "--path", str(r.home), "--max-bytes", str(64*1024*1024)],
            text=True,
        ))
        turn, _, task = await execute_and_observe(execution)
        assert task["state"] == "Verifying"
        session = turn.native_session_ref
        saved = file_evidence(r.home)
        assert session and any(session in path for path in saved), saved
        await restart_and_resume(r, adapter, turn)
        assert inspect(r)["Id"] != before["Id"]
        assert adapter.get_native_session(turn.turn_id) == session
        assert len((r.home/"model-invocations.jsonl").read_text().splitlines()) == 2
        events = await core.request("events")
        assert len([e for e in events if e["kind"] == "prompt_dispatch"]) == 1
        binding = (await core.request("runtime_status", id=r.agent))["agent"]
        assert (binding["id"], binding["task_id"], binding["workspace_id"], binding["budget_id"]) == (
            r.agent, r.task, r.ws, r.budget,
        )
        artifacts = await core.request("artifacts", id=r.task)
        assert len(artifacts) == 1 and artifacts[0]["content"] == "# Verified fixture\n\nKANE_PR2_FIXTURE\n"
        state = await core.request("task", id=r.task)
        assert state["state"] == "Verifying"
        done = await core.request("verify", id=r.task, version=state["version"], passed=True,
                                  evidence="Independent fixture equality after bounded-home session restore")
        assert done["state"] == "Done"
        (tmp_path/"bounded-session.json").write_text(json.dumps({
            "storage": check, "session": session, "saved_before_restart": saved,
            "files_after_resume": file_evidence(r.home), "binding": binding, "task": done,
            "old_container": before["Id"], "new_container": inspect(r)["Id"],
            "emulated_startup_seconds": r.startup_observations,
        }, indent=2))
    finally:
        await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("execution", [{"sandbox": True, "bounded_home": True, "sandbox_cpu_millis": 2000}], indirect=True)
async def test_enospc_keeps_task_uncertain_and_does_not_replay(execution, tmp_path):
    r, store, adapter, core = execution
    original = core.request
    fill = None

    async def request(op, **kw):
        nonlocal fill
        result = await original(op, **kw)
        if op == "reserve_model" and fill is None:
            # Fill only the admitted dedicated filesystem, using the same UID
            # and container mount as the real Agent. No host disk is filled.
            assert json.loads(r.spec.read_text())["sandbox"]["home_capacity_bytes"] == 64*1024*1024
            code = """
const f=require('fs'),p=process.env.HOME+'/capacity-fill';let n=0;
const fd=f.openSync(p,'wx');try{
  while(n<128*1024*1024)n+=f.writeSync(fd,Buffer.alloc(65536,1));
  throw Error('capacity not enforced');
}catch(e){if(e.code!=='ENOSPC')throw e;console.log(JSON.stringify({errno:e.code,written:n}))}
finally{f.closeSync(fd)}
"""
            rc, out, err = await node(r, code, timeout=45)
            assert rc == 0, err
            fill = json.loads(out)
            assert fill["errno"] == "ENOSPC" and fill["written"] < 64*1024*1024
        return result

    core.request = request
    try:
        await runtime_ready(r)
        turn, _, task = await execute_and_observe(execution)
        assert fill is not None
        assert task["state"] == "Blocked", task
        assert not await original("artifacts", id=r.task)
        budget = await original("model_budget", id=r.budget)
        assert budget["reserved_tokens"] == 32 and budget["used_tokens"] == 0
        events = await original("events")
        assert len([e for e in events if e["kind"] == "model_reserved"]) == 1
        assert len([e for e in events if e["kind"] == "prompt_dispatch"]) == 1
        saved = file_evidence(r.home)
        # Explicit Owner maintenance of our fixture, not an automatic refund or retry.
        (r.home/"capacity-fill").unlink()
        await restart_and_resume(r, adapter, turn)
        assert (await original("task", id=r.task))["state"] == "Blocked"
        assert not await original("artifacts", id=r.task)
        assert (await original("model_budget", id=r.budget)) == budget
        assert len([e for e in await original("events") if e["kind"] == "prompt_dispatch"]) == 1
        (tmp_path/"bounded-enospc.json").write_text(json.dumps({
            "fill": fill, "task": task, "budget": budget, "session": turn.native_session_ref,
            "files_at_failure": saved, "files_after_resume": file_evidence(r.home),
            "events": await original("events"),
            "emulated_startup_seconds": r.startup_observations,
        }, indent=2))
    finally:
        await adapter.close()
