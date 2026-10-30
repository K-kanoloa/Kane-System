"""Existing Harness -> real Rust subprocess. No live model or Agent rewrite."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
import os
import subprocess
import time

import httpx
import pytest

from app.domain.models import Conversation, Turn
from app.main import create_app
from app.store.sqlite_store import SQLiteStore

ROOT = Path(__file__).resolve().parents[3]
BINARY = ROOT / "target/debug/kanesys"
pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(not BINARY.is_file(), reason="build Rust workspace for host integration tests")]


@pytest.fixture(params=["subprocess", "service"], autouse=True)
def core_transport(tmp_path, monkeypatch, request):
    monkeypatch.delenv("KANE_SYSTEM_SOCKET", raising=False)
    if request.param == "subprocess":
        yield
        return
    os.chmod(tmp_path, 0o700)
    socket = tmp_path / "core.sock"
    monkeypatch.setenv("KANE_SYSTEM_SOCKET", str(socket))
    process = subprocess.Popen([str(BINARY), "--db", str(tmp_path / "system.db"), "--socket", str(socket), "serve"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 5
        while not socket.exists():
            assert process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.01)
        yield
    finally:
        process.kill()
        process.wait(timeout=5)


@asynccontextmanager
async def environment(tmp_path, monkeypatch, *, enabled=True, token="owner-fixture-token"):
    monkeypatch.setenv("KANE_SYSTEM_ENABLED", "1" if enabled else "0")
    monkeypatch.setenv("KANE_SYSTEM_DB", str(tmp_path / "system.db"))
    monkeypatch.setenv("KANE_SYSTEM_BINARY", str(BINARY))
    monkeypatch.delenv("OCTOPUS_API_KEY", raising=False)
    if token:
        monkeypatch.setenv("OCTOPUS_API_TOKEN", token)
    else:
        monkeypatch.delenv("OCTOPUS_API_TOKEN", raising=False)
    app = create_app()
    app.state.store = SQLiteStore(tmp_path / "harness.db")
    async with app.router.lifespan_context(app):
        conversation = Conversation(conversation_id="conversation", bound_agent_id="kanaloa")
        turn = Turn(turn_id="native-turn", conversation_id=conversation.conversation_id, bound_agent_id="kanaloa")
        app.state.store.save_conversation(conversation)
        app.state.store.save_turn(turn)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://kane", headers={"X-Api-Key": token} if token else {}) as client:
            yield app, client, turn


def binding():
    return {"turn_id": "native-turn", "goal": "Write editable report", "acceptance": ["Owner checks report"],
            "workload": {"class": "execution", "max_memory_bytes": 1048576, "max_storage_bytes": 1048576}}


async def test_existing_kanaloa_completion_enters_rust_verifying_and_owner_checks(tmp_path, monkeypatch):
    async with environment(tmp_path, monkeypatch) as (app, client, turn):
        created = await client.post("/api/v1/system/bindings", json=binding())
        assert created.status_code == 200, created.text
        task = created.json()
        # Exercise the existing coordinator, not a replacement Agent implementation.
        message = await app.state.coordinator.emit_message_complete(turn.turn_id, "# Editable report", "kanaloa")
        assert app.state.store.get_turn(turn.turn_id).status == "finished"
        synced = await client.post(f"/api/v1/system/bindings/{turn.turn_id}/sync")
        assert synced.status_code == 200, synced.text
        assert synced.json()["state"] == "Verifying"
        duplicate = await client.post(f"/api/v1/system/bindings/{turn.turn_id}/sync")
        assert duplicate.json()["version"] == synced.json()["version"]
        detail = await client.get(f"/api/v1/system/tasks/{task['id']}")
        assert len(detail.json()["artifacts"]) == 1
        assert detail.json()["artifacts"][0]["content"] == message.content
        checked = await client.post(f"/api/v1/system/tasks/{task['id']}/verify", json={
            "version": synced.json()["version"], "passed": True, "evidence": "Owner independently checked editable Markdown and acceptance"})
        assert checked.status_code == 200, checked.text
        assert checked.json()["state"] == "Done"
        assert app.state.store.get_turn(turn.turn_id).status == "finished"


async def test_system_endpoints_require_configured_owner_auth(tmp_path, monkeypatch):
    async with environment(tmp_path, monkeypatch, token="") as (_, client, _):
        assert (await client.get("/api/v1/system/status")).status_code == 503
        assert (await client.post("/api/v1/system/bindings", json=binding())).status_code == 503


async def test_no_anonymous_owner_impersonation(tmp_path, monkeypatch):
    async with environment(tmp_path, monkeypatch) as (_, client, _):
        response = await client.get("/api/v1/system/status", headers={"X-Api-Key": "forged"})
        assert response.status_code == 401
        body = {**binding(), "actor": "Owner"}
        assert (await client.post("/api/v1/system/bindings", json=body)).status_code == 422


async def test_binding_missing_workload_unknown_turn_and_training(tmp_path, monkeypatch):
    async with environment(tmp_path, monkeypatch) as (_, client, _):
        body=binding(); del body["workload"]
        assert (await client.post("/api/v1/system/bindings", json=body)).status_code == 422
        body=binding(); body["turn_id"]="unknown"
        assert (await client.post("/api/v1/system/bindings", json=body)).status_code == 404
        body=binding(); body["workload"]["class"]="training"
        assert (await client.post("/api/v1/system/bindings", json=body)).status_code == 400


async def test_interrupted_native_work_is_blocked_without_reexecution(tmp_path, monkeypatch):
    async with environment(tmp_path, monkeypatch) as (app, client, turn):
        created=await client.post("/api/v1/system/bindings",json=binding())
        assert created.status_code==200
        await app.state.coordinator.emit_interrupted(turn.turn_id,"native_tool_outcome_unknown")
        result=await client.post(f"/api/v1/system/bindings/{turn.turn_id}/sync")
        assert result.status_code==200
        assert result.json()["state"]=="Blocked"
        assert result.json()["block_reason"]=="reconciliation"
        assert app.state.store.get_turn(turn.turn_id).status=="interrupted"


async def test_verification_version_conflict_and_empty_evidence(tmp_path, monkeypatch):
    async with environment(tmp_path, monkeypatch) as (app, client, turn):
        task=(await client.post("/api/v1/system/bindings",json=binding())).json()
        await app.state.coordinator.emit_message_complete(turn.turn_id,"result","kanaloa")
        await client.post(f"/api/v1/system/bindings/{turn.turn_id}/sync")
        assert (await client.post(f"/api/v1/system/tasks/{task['id']}/verify",json={"version":999,"passed":True,"evidence":"checked"})).status_code==409
        assert (await client.post(f"/api/v1/system/tasks/{task['id']}/verify",json={"version":1,"passed":True,"evidence":""})).status_code==422


async def test_existing_harness_runs_when_system_integration_disabled(tmp_path, monkeypatch):
    async with environment(tmp_path, monkeypatch, enabled=False) as (app, client, turn):
        assert (await client.get("/api/v1/system/status")).status_code==503
        message=await app.state.coordinator.emit_message_complete(turn.turn_id,"preserved existing behavior","kanaloa")
        assert message.content=="preserved existing behavior"
        assert app.state.store.get_turn(turn.turn_id).status=="finished"


async def test_configured_socket_failure_never_opens_a_fallback_database(tmp_path, monkeypatch):
    fallback = tmp_path / "fallback-must-not-exist.db"
    monkeypatch.setenv("KANE_SYSTEM_SOCKET", str(tmp_path / "missing-core.sock"))
    async with environment(tmp_path, monkeypatch) as (_, client, _):
        monkeypatch.setenv("KANE_SYSTEM_DB", str(fallback))
        response = await client.get("/api/v1/system/status")
        assert response.status_code == 503
        assert not fallback.exists()
        assert "missing-core.sock" not in response.text
