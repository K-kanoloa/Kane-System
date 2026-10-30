"""Use the existing Supervisor protocol and existing Harness, not an Agent loop."""
import asyncio
import json
import os
from pathlib import Path
import socket

class ControlRuntime:
    def __init__(self, app):
        self.app=app
        self.children={}
        self.start_locks={}

    async def adapter(self, task):
        core=self.app.state.control_core
        rows=await core.request('bindings')
        binding=next((b for b in rows if b['task_id']==task),None)
        state=await core.request('task',id=task)
        agents=getattr(self.app.state.coordinator,'_adapters',{})
        for adapter in agents.values():
            agent=getattr(adapter,'system_agent_id',None)
            if getattr(adapter,'process_mode',None)=='supervised' and agent:
                status=await core.request('runtime_status',id=agent)
                if status['agent']['task_id']==task:return adapter,binding,state
        raise ValueError('No verified supervised Harness adapter for this Task')

    async def ensure_running(self, adapter):
        key=adapter.system_agent_id
        lock=self.start_locks.setdefault(key,asyncio.Lock())
        async with lock:
            async def live():
                try:
                    reader,writer=await asyncio.wait_for(asyncio.open_unix_connection(adapter.supervisor_socket),1)
                    writer.write(b'{"op":"status"}\n');await writer.drain();data=await asyncio.wait_for(reader.readline(),1);writer.close();await writer.wait_closed();return json.loads(data).get('registry',{}).get('agent',{}).get('id')==key
                except (OSError,ValueError,TimeoutError):return False
            if await live():return
            specs=getattr(self.app.state,'control_runtime_specs',None)
            if specs is None:specs=json.loads(os.getenv('KANE_CONTROL_RUNTIME_SPECS','{}'))
            path=Path(specs.get(key,''))
            if not path.is_absolute() or not path.is_file() or path.is_symlink() or path.parent.stat().st_mode&0o077:raise ValueError('Private registered Supervisor spec required; no host fallback')
            declared=json.loads(path.read_text())
            registered=await self.app.state.control_core.request('runtime_status',id=key)
            if declared.get('agent_id')!=key or any(declared.get(k)!=registered['config'].get(k) for k in ['command','cwd','runtime_home','sandbox']):raise ValueError('Supervisor spec differs from registered authority')
            proc=await asyncio.create_subprocess_exec(str(self.app.state.control_core.binary),'--socket',self.app.state.control_core.socket,'supervise',str(path),stdout=asyncio.subprocess.DEVNULL,stderr=None)
            self.children[key]=proc
            async with asyncio.timeout(30):
                while not await live():
                    if proc.returncode is not None:raise ValueError('Supervisor refused managed runtime')
                    await asyncio.sleep(.05)

    async def pause(self,task):
        adapter,_,_=await self.adapter(task)
        await adapter.close()  # Existing supervised transport stop/DSH close, no permanent Task cancel.
        async with asyncio.timeout(30):
            while True:
                status=await self.app.state.control_core.request('runtime_status',id=adapter.system_agent_id)
                if status['agent']['state'] in {'Suspended','Ready','Created'} and status['control']['phase'] not in {'launching','running','cancelling'}:break
                await asyncio.sleep(.05)

    async def handoff(self,task,body,token):
        from ..domain.models import Message
        from ..system.execution import ExecutionBridge
        core=self.app.state.control_core
        adapter,binding,_=await self.adapter(task)
        if not binding:raise ValueError('Native bound Session is required')
        store=self.app.state.store;turn=store.get_turn(binding['turn_id'])
        if turn is None:raise ValueError('Original Harness Turn missing')
        if any(m.message_id==body['message_id'] for m in store.get_messages(turn.conversation_id)):raise ValueError('Explicit message already exists; inspect instead of replay')
        await self.ensure_running(adapter)
        adapter.execution_bridge=ExecutionBridge(store,adapter.system_agent_id,core)
        await adapter.resume(turn,[])
        await core.request('control_handoff_ready',token=token,id=task)
        await self.app.state.coordinator.emit_resumed(turn.turn_id,reason='Owner handed back checked native version')
        message=Message(message_id=body['message_id'],conversation_id=turn.conversation_id,turn_id=turn.turn_id,sender='user',content=body['content'])
        store.append_message(message)
        await adapter.send(store.get_turn(turn.turn_id),message,[])
        return {'submitted':True,'turn_id':turn.turn_id,'completion':'not_verified'}

    async def close(self):
        for proc in self.children.values():
            if proc.returncode is None:
                try:await asyncio.wait_for(proc.wait(),10)
                except TimeoutError:
                    proc.terminate();await proc.wait()
