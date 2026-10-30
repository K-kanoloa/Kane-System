"""No reasoning loop, model scheduler or authoritative state lives here.

The Rust DAG issues one-shot claims; the original Harness stores and executes
Turns. Uncertain dispatch never retries. Restart means inspect Core, not resend.
This interface is local trusted Host IPC, not an untrusted network API.
"""
from dataclasses import dataclass
import uuid
import asyncio
from ..domain.models import Conversation, Turn, Message
from ..system.execution import ExecutionBridge
from ..system.client import CoreRejected


@dataclass
class HarnessBinding:
    adapter: object
    store: object
    harness_id: str
    # Context IDs come from explicit Owner sharing, never Agent-provided grants.
    contexts: tuple[str, ...] = ()


class CollaborationCoordinator:
    def __init__(self, core, bindings, ensure_running=None):
        self.core = core
        self.bindings = dict(bindings)  # stable system Agent ID -> generic Harness seam
        self.ensure_running = ensure_running

    async def dispatch(self, task_id):
        claim = await self.core.request('collaboration_claim', id=task_id)
        binding = self.bindings.get(claim['agent_id'])
        if binding is None:
            # The durable claim remains visible; no fallback Agent or retry.
            raise CoreRejected('Claimed Agent has no verified Host Harness binding')
        if getattr(binding.adapter, 'process_mode', None) != 'supervised':
            raise CoreRejected('A verified Supervisor/ExecutionBridge transport is required; no standalone fallback')
        state = await self.core.request('task', id=task_id)
        content = state['spec']['goal']
        for context in binding.contexts:
            value = await self.core.request('collaboration_context_read', id=context, agent_id=claim['agent_id'])
            import json
            content += '\nOwner-authorized reference DATA; no tool authority:\n' + json.dumps(value, ensure_ascii=False)
        adapter, store = binding.adapter, binding.store
        if self.ensure_running is not None:
            await self.ensure_running(adapter)  # original Rust Supervisor ownership
        adapter.execution_bridge = ExecutionBridge(store, claim['agent_id'], self.core)
        await adapter.activate()
        conversation_id, turn_id, message_id = ['collaboration-' + str(uuid.uuid4()) for _ in range(3)]
        # Harness product identity is separate from system Agent identity.
        harness_id = binding.harness_id
        conversation = Conversation(conversation_id=conversation_id, bound_agent_id=harness_id)
        turn = Turn(turn_id=turn_id, conversation_id=conversation_id, bound_agent_id=harness_id)
        message = Message(message_id=message_id, conversation_id=conversation_id, turn_id=turn_id,
                          sender='user', content=content)
        store.save_conversation(conversation)
        store.save_turn(turn)
        store.append_message(message)
        await adapter.send(turn, message, [])  # existing ACP, Agent Loop, tools and receipts
        return turn

    async def pause(self, root, reason):
        status = await self.core.request('collaboration_status', id=root)
        # Commit admission barrier BEFORE closing any Harness transport.
        result = await self.core.request('collaboration_policy', id=root, version=status['version'],
                                         max_agents=status['max_agents'], paused=True, reason=reason)
        for node in status['nodes']:
            binding = self.bindings.get(node['agent_id'])
            if binding and node['claimed'] and node['state'] not in {'Done','Failed','Cancelled','ClosedUnverified'}:
                await self._stop(binding, node['agent_id'])
        return result

    async def cancel(self, root, reason):
        # Core atomically cascades existing cancellation/Unknown rules first.
        result = await self.core.request('cancel_task', id=root, reason=reason)
        status = await self.core.request('collaboration_status', id=root)
        for node in status['nodes']:
            binding = self.bindings.get(node['agent_id'])
            if binding:
                await self._stop(binding, node['agent_id'])
        return result

    async def release_completed(self, root):
        """Return idle execution resources through the original Supervisor stop.

        Task Done is independent of Agent/Supervisor lifetime. No state is
        inferred from transport closure and no unfinished Task is replayed.
        """
        status = await self.core.request('collaboration_status', id=root)
        stopped = []
        for node in status['nodes']:
            binding = self.bindings.get(node['agent_id'])
            if binding and node['claimed'] and node['state'] in {'Done','Failed','Cancelled','ClosedUnverified'}:
                await self._stop(binding, node['agent_id'])
                stopped.append(node['agent_id'])
        return stopped


    async def _stop(self, binding, agent_id):
        if getattr(binding.adapter, 'process_mode', None) != 'supervised':
            raise CoreRejected('Cannot confirm runtime stop without verified Supervisor ownership')
        await binding.adapter.close()
        def stopped(status):
            return status['agent']['state'] in {'Suspended','Created','Ready','Exited','Failed','Terminated'} and status['control']['phase'] not in {'launching','running','cancelling'}
        async def control(operation):
            import json
            reader,writer=await asyncio.open_unix_connection(binding.adapter.supervisor_socket)
            try:
                writer.write(json.dumps({'op':operation}).encode()+b'\n');await writer.drain()
                raw=await reader.readline()
                if len(raw)>65536 or not raw:raise EOFError('Supervisor control reply absent')
                return json.loads(raw)
            finally:
                writer.close();await writer.wait_closed()
        # An unattached idle runtime has no ACP transport to close. Use the
        # existing authenticated private Supervisor protocol, never kill/spawn
        # an independently owned PID or infer termination from socket EOF.
        async with asyncio.timeout(30):
            status=await self.core.request('runtime_status',id=agent_id)
            if stopped(status):return status
            try:
                probe=await control('status')
                if probe.get('registry',{}).get('agent',{}).get('id')!=agent_id:
                    raise CoreRejected('Supervisor stop identity mismatch')
                reply=await control('stop')
                if reply.get('ok') is not True:raise CoreRejected('Supervisor stop not acknowledged')
            except (OSError,EOFError,asyncio.IncompleteReadError):
                pass  # A lost reply is inspected below, never sent again.
            while True:
                status=await self.core.request('runtime_status',id=agent_id)
                if stopped(status):return status
                await asyncio.sleep(.05)

    async def run(self, root, verifier, *, timeout=600, share_confirmed=None):
        """Drive an Owner-declared DAG; Rust alone admits dependencies/quotas.

        The verifier is trusted Host code independent of the Agent. A resumed
        driver never resends a pre-existing claim. Context sharing is opt-in
        Owner consent with an explicit label; no data is implicitly made public.
        """
        if not callable(verifier) or not 0 < timeout <= 3600:
            raise ValueError('Independent verifier and bounded execution timeout required')
        if share_confirmed is not None and share_confirmed not in {'public','internal','confidential','restricted'}:
            raise ValueError('Explicit Owner Context sensitivity required')
        import time
        owned=set();verified=set()
        async with asyncio.timeout(timeout):
            while True:
                status=await self.core.request('collaboration_status',id=root)
                if status['paused']:
                    return {'execution_status':'paused','authority':status}
                if all(n['state']=='Done' and n['verification_state']=='passed' for n in status['nodes']):
                    await self.release_completed(root)
                    return {'execution_status':'verified','authority':await self.core.request('collaboration_status',id=root)}
                rejected=[n for n in status['nodes'] if n['verification_state']=='failed']
                if rejected:return {'execution_status':'needs-rework','nodes':rejected,'automatic_replay':False}
                uncertain=[n for n in status['nodes'] if n['state'] in {'Blocked','Failed','Cancelled','ClosedUnverified'}]
                if uncertain:
                    return {'execution_status':'needs-owner-review','nodes':uncertain,'authority':status,'automatic_replay':False}
                for node in status['nodes']:
                    if node['state']=='Verifying' and node['task_id'] not in verified:
                        artifacts=await self.core.request('artifacts',id=node['task_id'])
                        decision=await verifier(node['task_id'],artifacts)
                        task=await self.core.request('task',id=node['task_id'])
                        if decision is None:
                            await self.core.request('transition',id=node['task_id'],version=task['version'],state='Blocked',reason='awaiting-verification')
                            return {'execution_status':'awaiting-independent-verification','task_id':node['task_id']}
                        if type(decision.get('passed')) is not bool or not decision.get('evidence'):
                            raise ValueError('Independent pass/fail and evidence required')
                        await self.core.request('verify',id=node['task_id'],version=task['version'],passed=decision['passed'],evidence=decision['evidence'])
                        verified.add(node['task_id'])
                        if decision['passed']:await self.release_completed(root)
                        if not decision['passed']:
                            return {'execution_status':'needs-rework','task_id':node['task_id'],'automatic_replay':False}
                status=await self.core.request('collaboration_status',id=root)
                active=sum(n['claimed'] and n['state'] not in {'Done','Failed','Cancelled','ClosedUnverified'} for n in status['nodes'])
                for node in status['nodes']:
                    if active>=status['max_agents']:break
                    if node['claimed'] or node['state'] not in {'Pending','Ready'}:
                        continue
                    # Readiness here is only a read optimization. Core repeats
                    # dependency/identity/budget/pause checks atomically at claim.
                    definition=next(n for n in status['spec']['nodes'] if n['task_id']==node['task_id'])
                    dependencies=[next(n for n in status['nodes'] if n['task_id']==d) for d in definition['depends_on']]
                    if any(n['state']!='Done' or n['verification_state']!='passed' for n in dependencies):
                        continue
                    if share_confirmed is not None and dependencies:
                        binding=self.bindings[node['agent_id']];refs=[]
                        for dep in dependencies:
                            for artifact in await self.core.request('artifacts',id=dep['task_id']):
                                if artifact['verification_state']!='passed':continue
                                context=(await self.core.request('collaboration_context',artifact_id=artifact['id'],sensitivity=share_confirmed,expires_at=int(time.time())+600))['id']
                                await self.core.request('collaboration_share',id=context,agent_id=node['agent_id'],ceiling=share_confirmed,expires_at=int(time.time())+300)
                                refs.append(context)
                        binding.contexts=tuple(refs)
                    # No retry after an uncertain claim/dispatch response. All
                    # such exceptions leave the durable state for inspection.
                    await self.dispatch(node['task_id'])
                    owned.add(node['task_id']);active+=1
                stranded=[n for n in status['nodes'] if n['claimed'] and n['state'] in {'Pending','Ready'} and n['task_id'] not in owned]
                if stranded:
                    return {'execution_status':'inspect-existing-claim','nodes':stranded,'automatic_replay':False}
                await asyncio.sleep(.05)
