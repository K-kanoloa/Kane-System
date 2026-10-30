"""Owner-only system control adapter. Existing Agent/Turn behavior is preserved."""
from __future__ import annotations

import asyncio
import hmac
import os
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ..settings_env import get_api_token
from ..store.base import BaseStore
from ..system.client import CoreRejected, CoreUnavailable, SystemClient
from .deps import get_store


def owner_guard(request: Request) -> None:
    if os.getenv("KANE_SYSTEM_ENABLED") != "1":
        raise HTTPException(503, "system_integration_disabled")
    expected = get_api_token()
    if not expected:
        raise HTTPException(503, "system_owner_auth_not_configured")
    provided = request.headers.get("X-Api-Key", "").strip()
    if not provided:
        auth = request.headers.get("Authorization", "")
        provided = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not hmac.compare_digest(provided.encode(), expected.encode()):
        raise HTTPException(401, "system_owner_auth_required")


router = APIRouter(prefix="/system", tags=["system"], dependencies=[Depends(owner_guard)])


def get_core(request: Request) -> SystemClient:
    if not hasattr(request.app.state, "system_client"):
        request.app.state.system_client = SystemClient()
    return request.app.state.system_client


async def call(core: SystemClient, op: str, **payload):
    try:
        return await core.request(op, **payload)
    except CoreRejected as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    except (CoreUnavailable, OSError) as exc:
        raise HTTPException(503, "system_core_unavailable") from exc
    except asyncio.TimeoutError as exc:
        raise HTTPException(504, "system_core_deadline") from exc


class WorkloadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    class_: Literal["inference", "generation", "execution", "training", "realtime"] = Field(alias="class")
    interruptible: bool = False
    resumable: Literal["none", "restart", "checkpoint"] = "none"
    realtime: bool = False
    max_memory_bytes: int = Field(gt=0, le=2**63 - 1)
    max_storage_bytes: int = Field(gt=0, le=2**63 - 1)
    checkpoint_verified: bool = False


class BindRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    turn_id: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    acceptance: list[Annotated[str, Field(min_length=1)]] = Field(min_length=1)
    workload: WorkloadRequest
    token_limit: int = Field(default=100_000, gt=0, le=2**63 - 1)


class VerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=0)
    passed: bool
    evidence: str = Field(min_length=1, max_length=10_000)


class UnverifiedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=10_000)


@router.get("/status")
async def status(core: SystemClient = Depends(get_core)):
    return {
        "name": "Kane System", "version": "0.3.0-dev", "complete": False,
        "integration": "existing Harness + Rust system registry",
        "enforcement": "Partial", "compatibility": "Unknown",
        "tasks": await call(core, "tasks"),
        "bindings": await call(core, "bindings"),
    }


@router.get("/tasks/{task_id}")
async def task(task_id: str, core: SystemClient = Depends(get_core)):
    return {"task": await call(core, "task", id=task_id), "artifacts": await call(core, "artifacts", id=task_id)}


@router.get("/ledger")
async def ledger(core: SystemClient = Depends(get_core)):
    return await call(core, "events")


@router.post("/bindings")
async def bind(body: BindRequest, store: BaseStore = Depends(get_store), core: SystemClient = Depends(get_core)):
    turn = store.get_turn(body.turn_id)
    if turn is None:
        raise HTTPException(404, "harness_turn_not_found")
    old = await call(core, "bound_task", turn_id=body.turn_id)
    workload = body.workload.model_dump(by_alias=True)
    if old is not None:
        if old["spec"]["goal"] != body.goal or old["spec"]["acceptance"] != body.acceptance or old["spec"]["workload"] != workload:
            raise HTTPException(409, "binding_specification_conflict")
        return old
    if body.workload.class_ in {"training", "realtime"} or body.workload.realtime:
        raise HTTPException(400, "workload_not_supported")
    # Provisioning is local and side-effect-free outside Registry. Failed binding can
    # leave unbound, audited workspace/budget records; no model/tool work is replayed.
    workspace = await call(core, "create_workspace", kind="document")
    budget = await call(core, "create_budget", tokens=body.token_limit)
    return await call(core, "bind", turn_id=body.turn_id, harness_agent_ref=turn.bound_agent_id, spec={
        "goal": body.goal, "acceptance": body.acceptance, "workload": workload,
        "workspace_id": workspace["id"], "budget_id": budget["id"], "cloud_allowed": False,
    })


@router.post("/bindings/{turn_id}/sync")
async def sync(turn_id: str, store: BaseStore = Depends(get_store), core: SystemClient = Depends(get_core)):
    turn = store.get_turn(turn_id)
    if turn is None:
        raise HTTPException(404, "harness_turn_not_found")
    messages = [m for m in store.get_messages(turn.conversation_id) if m.turn_id == turn_id and m.sender == "agent" and m.sender_id == turn.bound_agent_id]
    final = messages[-1] if messages and turn.status == "finished" else None
    return await call(core, "sync", turn_id=turn_id, status=turn.status,
                      native_session_ref=turn.native_session_ref,
                      message={"message_id": final.message_id, "content": final.content} if final else None)


@router.post("/tasks/{task_id}/verify")
async def verify(task_id: str, body: VerificationRequest, core: SystemClient = Depends(get_core)):
    # Owner is an independent human verifier. The API does not invent automated evidence.
    return await call(core, "verify", id=task_id, **body.model_dump())


@router.post("/tasks/{task_id}/verification/resume")
async def resume_verification(task_id: str, body: UnverifiedRequest, core: SystemClient = Depends(get_core)):
    return await call(core, "transition", id=task_id, version=body.version, state="Verifying", reason=body.reason)


@router.post("/tasks/{task_id}/close-unverified")
async def close_unverified(task_id: str, body: UnverifiedRequest, core: SystemClient = Depends(get_core)):
    return await call(core, "close_unverified", id=task_id, **body.model_dump())



class DispatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=100_000)


@router.post("/tasks/{task_id}/dispatch")
async def dispatch(task_id: str, body: DispatchRequest, request: Request,
                   store: BaseStore = Depends(get_store), core: SystemClient = Depends(get_core)):
    from ..adapters.kanaloa_adapter import KanaloaAdapter
    from ..domain.models import Conversation, Turn, Message
    from ..system.execution import ExecutionBridge
    adapter = getattr(request.app.state,"kanaloa_adapter",None)
    if not isinstance(adapter,KanaloaAdapter) or adapter.process_mode != "supervised":
        raise HTTPException(409,"supervised_runtime_required")
    return await dispatch_existing(task_id,body.prompt,adapter,"kanaloa",store,core)


async def dispatch_existing(task_id,prompt,adapter,agent_ref,store,core):
    """One existing Harness submission shared by local and authenticated L5 routes."""
    from ..domain.models import Conversation,Turn,Message
    from ..system.execution import ExecutionBridge
    state = await call(core,"runtime_status",id=adapter.system_agent_id)
    task = await call(core,"task",id=task_id)
    if state["agent"]["task_id"] != task_id or task["state"] not in {"Pending","Ready"}:
        raise HTTPException(409,"task_dispatch_conflict")
    # Stable identities make HTTP retries inspectable. Never enqueue an automatic resend.
    turn_id=f"system-{task_id}"
    if store.get_turn(turn_id) is not None:
        raise HTTPException(409,"dispatch_exists_inspect_before_retry")
    conv=Conversation(conversation_id=f"system-conv-{task_id}",bound_agent_id=agent_ref)
    turn=Turn(turn_id=turn_id,conversation_id=conv.conversation_id,bound_agent_id=agent_ref)
    message=Message(message_id=f"system-prompt-{task_id}",conversation_id=conv.conversation_id,
                    turn_id=turn_id,sender="user",content=prompt)
    store.save_conversation(conv)
    store.save_turn(turn)
    store.append_message(message)
    adapter.execution_bridge=ExecutionBridge(store,adapter.system_agent_id,core)
    try:
        await adapter.send(turn,message,[])
    except Exception as exc:
        raise HTTPException(503,"dispatch_not_confirmed_inspect_state") from exc
    return {"task_id":task_id,"turn_id":turn_id,"status":"submitted_to_adapter","completion":"not_verified"}
