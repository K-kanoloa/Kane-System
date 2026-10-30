"""Trusted host bridge; execution stays in the existing Kanaloa/DSH runtime."""
import hashlib
import json

from .client import SystemClient


class ExecutionBridge:
    def __init__(self, store, agent_id, core=None):
        self.store = store
        self.agent_id = agent_id
        self.core = core or SystemClient()
        if not self.core.socket:
            raise RuntimeError("Supervised execution requires the Core socket; no DB fallback")
        self.admitted = set()
        self.native_turns = {}
        self.runtime_sessions = {}
        self.budget_relay = None

    async def admit(self, turn_id, message, native_session):
        # Do not cache a permit for replay. Every prompt attempt goes through Core;
        # duplicate submissions are rejected even after a lost response.
        try:
            result = await self.core.request("dispatch_harness", id=self.agent_id,
                turn_id=turn_id, message_id=message.message_id, content=message.content,
                native_session_ref=native_session)
        except Exception:
            # A receipt is evidence for interruption only, never permission to
            # send after the acknowledgement was lost. Core outage stays closed.
            try:
                receipt = await self.core.request("dispatch_receipt", turn_id=turn_id,
                    message_id=message.message_id)
                if receipt.get("runtime_session_id"):
                    self.runtime_sessions[turn_id] = receipt["runtime_session_id"]
            except Exception:
                pass
            raise
        if isinstance(result, dict) and result.get("runtime_session_id"):
            self.runtime_sessions[turn_id] = result["runtime_session_id"]
        self.admitted.add(turn_id)
        await self.core.request("enable_model_budget", turn_id=turn_id)
        self.native_turns[native_session] = turn_id

    async def prepare_budget(self, path, announced_path):
        if announced_path != path:
            raise RuntimeError("Protected execution requires the configured model-budget hook")
        if self.budget_relay is None:
            from .model_budget import ModelBudgetRelay
            self.budget_relay = ModelBudgetRelay(self, path)
        await self.budget_relay.start()

    async def close(self):
        if self.budget_relay:
            await self.budget_relay.close()


    async def fact(self, turn_id, kind, payload, receipt=None):
        if turn_id not in self.admitted:
            raise RuntimeError("Fact without admitted dispatch")
        receipt = receipt or hashlib.sha256(json.dumps([kind, payload],sort_keys=True).encode()).hexdigest()
        payload = {**payload, "runtime_session_id": self.runtime_sessions.get(turn_id)}
        await self.core.request("harness_fact", turn_id=turn_id, kind=kind,
                                receipt=receipt, payload=payload)

    async def sync(self, turn_id):
        if turn_id not in self.admitted:
            return
        turn=self.store.get_turn(turn_id)
        messages=[m for m in self.store.get_messages(turn.conversation_id)
                  if m.turn_id==turn_id and m.sender=="agent" and m.sender_id==turn.bound_agent_id]
        final=messages[-1] if turn.status=="finished" and messages else None
        await self.core.request("sync", turn_id=turn_id, status=turn.status,
            native_session_ref=turn.native_session_ref,
            runtime_session_id=self.runtime_sessions.get(turn_id),
            message={"message_id":final.message_id,"content":final.content} if final else None)

    async def interrupt(self, turn_id):
        # May also reconcile an admission whose committed response was lost.
        if await self.core.request("bound_task", turn_id=turn_id) is not None:
            await self.core.request("sync", turn_id=turn_id,status="interrupted",
                                    native_session_ref=None,message=None,
                                    runtime_session_id=self.runtime_sessions.get(turn_id))

    async def cancel(self, turn_id):
        # Bound Task can exist even if admission's reply was lost. Never rely
        # solely on the Python in-memory admitted set for the cancellation fence.
        task = await self.core.request("bound_task", turn_id=turn_id)
        if task is not None:
            return await self.core.request("cancel_task", id=task["id"], reason="Owner ACP cancellation")
