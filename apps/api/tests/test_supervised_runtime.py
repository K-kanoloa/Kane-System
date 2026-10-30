"""PR-1 Linux integration: actual bundled DSH, no model/provider/tool invocation."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import time
import tempfile

import pytest

from app.adapters.kanaloa_adapter import KanaloaAdapter

ROOT = Path(__file__).resolve().parents[3]
BIN = ROOT / "target/debug/kanesys"
# An isolated checkout may deliberately reuse the audited dependency tree from
# another checkout. Keep source imports rooted here while allowing container
# fixtures to mount that canonical runtime asset tree explicitly.
RUNTIME_ASSETS_ROOT = Path(os.getenv("KANE_TEST_RUNTIME_ASSETS_ROOT", ROOT)).resolve()
DSH = (RUNTIME_ASSETS_ROOT / "node_modules/@deepseek-ai/dsh/lib/bin.js").resolve()
pytestmark = pytest.mark.skipif(os.getenv("KANE_PR1_TEST") != "1", reason="opt-in Linux Rust + bundled DSH integration")


def wait_for(fn, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            result = fn()
            if result:
                return result
        except (OSError, ValueError, KeyError, subprocess.CalledProcessError):
            pass
        time.sleep(.04)
    raise AssertionError("condition timed out")


def dead(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1].startswith("Z")
    except FileNotFoundError:
        return True


class Rig:
    def __init__(self, path, model_id="unconfigured-test", model_spec=None, goal="PR-1 lifecycle only", acceptance=None, budget_tokens=10000, core_socket=None, task_memory=268435456, task_storage=1048576, runtime_home=None):
        self.path = path
        path.chmod(0o700)
        self.core_socket = Path(core_socket) if core_socket else path / "core.sock"
        self.log = (path / "runtime.log").open("w")
        self.core = None if core_socket else subprocess.Popen([str(BIN), "--db", str(path / "core.db"), "--socket", str(self.core_socket), "serve"], stderr=self.log)
        self.supervisors = []
        wait_for(self.core_socket.exists)
        self.ws = self.call({"op":"create_workspace", "kind":"document"})["id"]
        self.budget = self.call({"op":"create_budget", "tokens":budget_tokens})["id"]
        if core_socket is None:
            self.call({"op":"register_model", "id":model_id, "spec":model_spec or {"verified":False}})
        self.task = self.call({"op":"create_task", "spec":{"goal":goal, "acceptance":acceptance or ["ACP initialized; bindings stable"], "workload":{"class":"execution", "max_memory_bytes":task_memory, "max_storage_bytes":task_storage}, "budget_id":self.budget,"workspace_id":self.ws}})["id"]
        self.agent = self.call({"op":"register_agent", "role":"ACP runtime", "task_id":self.task,"model_id":model_id})["id"]
        self.socket = self.core_socket.parent / f"{self.agent}.runtime.sock"
        self.workspace = path / "workspace"
        self.home = Path(runtime_home) if runtime_home is not None else path / "home"
        self.workspace.mkdir()
        if runtime_home is None:
            self.home.mkdir()
        elif not self.home.is_dir():
            raise ValueError("external test runtime home must already exist")
        self.spec = path / "runtime.json"
        self.spec.write_text(json.dumps({"agent_id":self.agent,"command":[shutil.which("node"),str(DSH),"--profile","acp","--patch", str(RUNTIME_ASSETS_ROOT / "apps/api/app/adapters/kanaloa-persona.patch.yml")],"cwd":str(self.workspace),"runtime_home":str(self.home),"max_restarts":3,"restart_delay_ms":100}))

    def call(self, request):
        result = subprocess.run([str(BIN),"--socket",str(self.core_socket),"host-request"], input=json.dumps(request),text=True,capture_output=True,check=True)
        return json.loads(result.stdout)

    def start(self):
        p = subprocess.Popen([str(BIN),"--socket",str(self.core_socket),"supervise",str(self.spec)],stderr=self.log)
        self.supervisors.append(p)
        return p

    def control(self, op, timeout=None):
        # Stop waits for EOF/exit, including the Supervisor's 100 x 20ms
        # grace period. Keep status probes short, but allow bounded cleanup ACK.
        timeout = (10 if op in {"stop", "pause", "finish", "terminate"} else 2) if timeout is None else timeout
        began = time.monotonic()
        evidence = {"operation": op, "agent_id": self.agent, "timeout_s": timeout}
        try:
            with socket.socket(socket.AF_UNIX) as s:
                s.settimeout(timeout)
                s.connect(str(self.socket))
                s.sendall(json.dumps({"op":op}).encode()+b"\n")
                response=s.makefile("rb").readline()
                if not response:
                    raise ConnectionError("Supervisor disconnected before control response; outcome unknown")
                result = json.loads(response)
                evidence["response"] = result
                return result
        except Exception as error:
            evidence["error"] = type(error).__name__
            raise
        finally:
            evidence["elapsed_s"] = time.monotonic() - began
            with (self.path / "control-timing.jsonl").open("a") as log:
                log.write(json.dumps(evidence)+"\n")

    def status(self):
        return self.control("status")

    def adapter(self, **options):
        return KanaloaAdapter(**options,cwd=str(self.workspace),process_mode="supervised",supervisor_socket=str(self.socket),system_agent_id=self.agent)

    def close(self):
        try:
            self.control("stop")
        except OSError:
            pass
        for p in self.supervisors:
            if p.poll() is None:
                p.kill()
            p.wait(timeout=5)
        if self.core is not None:
            self.core.terminate()
            self.core.wait(timeout=5)
        self.log.close()


@pytest.fixture
def rig(tmp_path):
    r=Rig(Path(tempfile.mkdtemp(prefix="ks-pr1-")))
    try:
        yield r
    finally:
        try:
            evidence = {"status": r.call({"op":"runtime_status", "id":r.agent}),
                        "events": r.call({"op":"events"}),
                        "scope":"real bundled DSH lifecycle; no model or tools called"}
            (tmp_path / "evidence.json").write_text(json.dumps(evidence, indent=2))
        finally:
            r.close()
            shutil.copy(r.path / "runtime.log", tmp_path / "runtime.log")
            if (r.path / "control-timing.jsonl").exists():
                shutil.copy(r.path / "control-timing.jsonl", tmp_path / "control-timing.jsonl")


@pytest.mark.asyncio
async def test_real_runtime_stop_no_python_respawn(rig):
    supervisor=rig.start()
    status=wait_for(rig.status)
    adapter=rig.adapter()
    await adapter.activate()  # Actual DSH ACP initialize, not synthetic Agent fixture.
    assert adapter.is_alive()
    assert adapter._process.pid == status["pid"]
    assert os.path.samefile(f"/proc/{status['pid']}/exe", shutil.which("node"))
    assert str(DSH).encode() in Path(f"/proc/{status['pid']}/cmdline").read_bytes()
    response=await adapter._send_request("session/new", {"cwd":str(rig.workspace),"mcpServers":[]})
    assert "not configured" in response["error"]["message"]
    assert rig.control("stop")["ok"]
    supervisor.wait(timeout=5)
    await asyncio.sleep(.1)
    with pytest.raises((RuntimeError, OSError)):
        await adapter.activate()
    assert dead(status["pid"])
    assert rig.call({"op":"runtime_status","id":rig.agent})["agent"]["state"] == "Suspended"
    await adapter.close()


@pytest.mark.asyncio
async def test_real_runtime_crash_restart_and_bindings(rig):
    rig.start()
    first=wait_for(rig.status)
    adapter=rig.adapter()
    await adapter.activate()
    os.kill(first["pid"],signal.SIGKILL)
    second=wait_for(lambda: (s if (s:=rig.status())["pid"] != first["pid"] else None))
    assert second["registry"]["agent"]["id"] == rig.agent
    for key,expected in [("task_id",rig.task),("workspace_id",rig.ws),("budget_id",rig.budget)]:
        assert second["registry"]["agent"][key] == expected
    await asyncio.sleep(.1)
    await adapter.activate()  # Reattach and reinitialize; never replay a prompt.
    assert adapter._process.pid == second["pid"]
    events=rig.call({"op":"events"})
    assert any(e["kind"]=="runtime_lifecycle" and e["payload"]["detail"]["event"]=="process_exited" for e in events)
    assert second["registry"]["last"]["detail"]["restart_count"] == 1
    await adapter.close()


@pytest.mark.parametrize("termination", [signal.SIGKILL, signal.SIGTERM])
def test_supervisor_kill_no_orphan_or_duplicate(rig, termination):
    first_owner=rig.start()
    first=wait_for(rig.status)
    duplicate=rig.start()
    assert duplicate.wait(timeout=5) != 0
    assert not dead(first["pid"])
    first_owner.send_signal(termination)
    first_owner.wait(timeout=5)
    wait_for(lambda: dead(first["pid"]))
    rig.start()
    second=wait_for(lambda: (s if (s:=rig.status())["pid"] != first["pid"] else None))
    assert second["registry"]["agent"]["id"]==rig.agent
    assert second["registry"]["last"]["detail"]["attempt"]==2


def test_restart_budget_persistent_and_invalid_executable(rig):
    spec=json.loads(rig.spec.read_text())
    spec["command"]=["/does/not/exist"]
    spec["max_restarts"]=1
    rig.spec.write_text(json.dumps(spec))
    p=rig.start()
    assert p.wait(timeout=10) != 0
    status=rig.call({"op":"runtime_status","id":rig.agent})
    assert status["last"]["detail"]["attempt"]==2
    assert status["agent"]["state"]=="Failed"
    p=rig.start()
    assert p.wait(timeout=5) != 0
    assert rig.call({"op":"runtime_status","id":rig.agent})["last"]["detail"]["attempt"]==2


@pytest.mark.asyncio
async def test_detach_stops_and_second_attachment_rejected(rig):
    p=rig.start()
    status=wait_for(rig.status)
    adapter=rig.adapter()
    await adapter.activate()
    other=rig.adapter()
    with pytest.raises(RuntimeError):
        await other.activate()
    assert rig.status()["pid"]==status["pid"]
    await adapter.close()
    assert p.wait(timeout=5)==0
    assert dead(status["pid"])


def test_never_restart_policy(rig):
    spec=json.loads(rig.spec.read_text())
    spec["max_restarts"]=0
    rig.spec.write_text(json.dumps(spec))
    p=rig.start()
    first=wait_for(rig.status)
    os.kill(first["pid"],signal.SIGKILL)
    assert p.wait(timeout=5)!=0
    assert rig.call({"op":"runtime_status","id":rig.agent})["last"]["detail"]["attempt"]==1


@pytest.mark.asyncio
async def test_missing_transport_never_spawns(tmp_path, monkeypatch):
    async def forbidden(*a, **kw):
        raise AssertionError("Python tried to spawn")
    monkeypatch.setattr(asyncio,"create_subprocess_exec",forbidden)
    adapter=KanaloaAdapter(cwd=str(tmp_path),process_mode="supervised",supervisor_socket=str(tmp_path/"missing.sock"),system_agent_id="agent-test")
    with pytest.raises(RuntimeError,match="no local fallback"):
        await adapter.activate()
    with pytest.raises(ValueError):
        KanaloaAdapter(process_mode="standalone",supervisor_socket="/tmp/socket")


@pytest.mark.asyncio
async def test_p101_real_dsh_clean_exit_preserves_restartable_agent(rig):
    supervisor = rig.start()
    first = wait_for(rig.status)
    adapter = rig.adapter()
    await adapter.activate()
    # Original DSH installs a SIGTERM handler which disposes and exits 0.
    os.kill(first['pid'], signal.SIGTERM)
    assert supervisor.wait(timeout=10) == 0
    state = rig.call({'op': 'runtime_status', 'id': rig.agent})
    assert state['agent']['state'] == 'Suspended'
    assert state['last']['detail']['event'] == 'process_exited'
    assert state['last']['detail']['success'] is True
    assert state['last']['detail']['exit_code'] == 0
    assert state['control']['attempt'] == 1
    assert rig.call({'op':'task','id':rig.task})['state'] == 'Pending'
    await adapter.close()
    p = rig.start()
    second = wait_for(rig.status)
    assert second['pid'] != first['pid'] and second['registry']['control']['attempt'] == 2
    rig.control('pause')
    assert p.wait(timeout=5) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('operation,expected', [('finish','Exited'),('terminate','Terminated')])
async def test_p101_explicit_logical_terminal_never_completes_task_or_restarts(rig, operation, expected):
    p = rig.start()
    first = wait_for(rig.status)
    adapter = rig.adapter()
    await adapter.activate()
    rig.control(operation)
    assert p.wait(timeout=10) == 0 and dead(first['pid'])
    state = rig.call({'op':'runtime_status','id':rig.agent})
    assert state['agent']['state'] == expected
    assert state['control']['phase'] == 'closed'
    for key, value in [('id',rig.agent),('task_id',rig.task),('workspace_id',rig.ws),('budget_id',rig.budget)]:
        assert state['agent'][key] == value
    assert rig.call({'op':'task','id':rig.task})['state'] == 'Pending'
    events = rig.call({'op':'events'})
    lifecycle = [x['payload']['to'] for x in events if x['kind']=='runtime_lifecycle']
    if operation == 'terminate':
        assert lifecycle[-2:] == ['Cancelling','Terminated']
    again = rig.start()
    assert again.wait(timeout=5) != 0
    assert rig.call({'op':'runtime_status','id':rig.agent}) == state
    await adapter.close()


@pytest.mark.asyncio
async def test_p101_fresh_version_cannot_reauthorize_old_real_process_attempt(rig):
    rig.start()
    first = wait_for(rig.status)
    adapter = rig.adapter()
    await adapter.activate()
    old = first['registry']['control']
    os.kill(first['pid'], signal.SIGKILL)
    second = wait_for(lambda: (s if (s:=rig.status())['pid'] != first['pid'] else None))
    state = second['registry']
    assert state['control']['attempt'] == 2
    with pytest.raises(subprocess.CalledProcessError):
        rig.call({'op':'runtime_event','id':rig.agent,'version':state['agent']['version'],
                  'data':{'event':'process_exited','success':False,'owner_token':old['owner_token'],'attempt':old['attempt']}})
    assert rig.call({'op':'runtime_status','id':rig.agent}) == state
    assert not dead(second['pid'])
    await adapter.close()


@pytest.mark.asyncio
async def test_p101_failed_agent_stays_failed_after_core_restart_and_owner_relaunch(rig):
    spec = json.loads(rig.spec.read_text())
    spec['max_restarts'] = 0
    rig.spec.write_text(json.dumps(spec))
    p = rig.start()
    first = wait_for(rig.status)
    adapter = rig.adapter()
    await adapter.activate()
    os.kill(first['pid'],signal.SIGKILL)
    assert p.wait(timeout=10) != 0
    state = rig.call({'op':'runtime_status','id':rig.agent})
    assert state['agent']['state'] == 'Failed' and state['control']['attempt'] == 1
    rig.core.kill()
    rig.core.wait(timeout=5)
    rig.core = subprocess.Popen([str(BIN),'--db',str(rig.path/'core.db'),'--socket',str(rig.core_socket),'serve'],stderr=rig.log)
    wait_for(lambda: rig.call({'op':'health'}))
    again = rig.start()
    assert again.wait(timeout=5) != 0
    assert rig.call({'op':'runtime_status','id':rig.agent}) == state
    # Policy is immutable, including an Owner attempt to enlarge the allowance.
    spec['max_restarts'] = 100
    rig.spec.write_text(json.dumps(spec))
    assert rig.start().wait(timeout=5) != 0
    assert rig.call({'op':'runtime_status','id':rig.agent}) == state
    await adapter.close()


def test_status_peer_disconnect_does_not_terminate_runtime_owner(rig):
    owner=rig.start();first=wait_for(rig.status)
    for _ in range(8):
        with socket.socket(socket.AF_UNIX) as peer:
            peer.connect(str(rig.socket));peer.sendall(b'{"op":"status"}\n')
            peer.shutdown(socket.SHUT_RDWR)
        time.sleep(.04)
    assert owner.poll() is None
    after=rig.status()
    assert after['pid']==first['pid']
    for key in ['id','task_id','workspace_id','budget_id']:
        assert after['registry']['agent'][key]==first['registry']['agent'][key]
    assert after['registry']['last']['detail']['restart_count']==0
