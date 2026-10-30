"""PR-4 strict reference profile; only the disposable two-boot Linux VM."""
import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import time
from types import SimpleNamespace

import pytest

from app.harness.coordinator import HarnessCoordinator
from app.harness.mailbox import MailboxManager
from app.store.sqlite_store import SQLiteStore
from app.system.client import SystemClient
from app.system.execution import ExecutionBridge
from test_supervised_runtime import BIN, Rig, wait_for
from test_system_execution import execution
from test_storage_runtime import execute_and_observe, runtime_ready
from test_sandbox_runtime import inspect, node

pytestmark=pytest.mark.skipif(os.getenv('KANE_PR4_CLOSE_TEST')!='1',reason='opt-in disposable two-boot PR-4 reference VM')
CONTROL=Path('/var/tmp/kane-pr4-control')
M=1024*1024

@pytest.fixture(autouse=True)
def disposable_vm():
    assert Path('/etc/kane-storage-runtime-validation').read_text()=='disposable-vm\n'
    assert Path('/mnt/kane-pr4-home-0').is_mount()

def pool(r): return r.call({'op':'host_resources'})
def configure_pool(r):
    r.call({'op':'configure_host_resources','version':0,'policy':{
        'capacity':{'cpu_millis':2000,'memory_bytes':2560*M,'pids':2048,'storage_bytes':4096*M},
        'reserved':{'cpu_millis':500,'memory_bytes':1024*M,'pids':128,'storage_bytes':1024*M}}})
def fixture(tmp_path,monkeypatch,index,core=None):
    case=tmp_path/str(index);case.mkdir()
    config={'sandbox':True,'bounded_home':True,'host_resources':True,'defer_start':True,
            'runtime_home':f'/mnt/kane-pr4-home-{index}',
            'sandbox_cpu_millis':1500 if index==4 else 500}
    if core: config['core_socket']=str(core)
    generator=execution.__wrapped__(case,monkeypatch,SimpleNamespace(param=config))
    item=next(generator)
    return generator,item
async def daemon(action):
    reply=CONTROL/'reply.json'
    if reply.exists(): reply.unlink()
    temporary=CONTROL/'request.tmp';temporary.write_text(json.dumps({'action':action}))
    temporary.replace(CONTROL/'request.json')
    async with asyncio.timeout(120):
        while not reply.exists(): await asyncio.sleep(.1)
    response=json.loads(reply.read_text());reply.unlink();assert response['ok'],response
    return response
def held(r):
    return next(v for v in pool(r)['claims'] if v['agent_id']==r.agent)['held']

@pytest.mark.asyncio
async def test_pool_and_real_daemon_restart(tmp_path,monkeypatch):
    generators=[];items=[];frozen=False
    try:
        for i in range(4):
            g,item=fixture(tmp_path,monkeypatch,i,items[0][0].core_socket if items else None)
            generators.append(g);items.append(item)
        r=items[0][0];configure_pool(r)
        for item in items[:3]: item[0].start();wait_for(item[0].status,30)
        admitted=pool(r)
        assert admitted['used']['memory_bytes']==1536*M and admitted['used']['cpu_millis']==1500
        rejected=items[3][0].start()
        assert await asyncio.to_thread(rejected.wait,30)!=0
        assert inspect(items[3][0]) is None
        assert items[3][0].call({'op':'task','id':items[3][0].task})['state']=='Pending'
        containers=await asyncio.gather(*(runtime_ready(x[0],300) for x in items[:3]))
        results=[]
        for item in items[:3]:
            rig=item[0]
            # Refresh the fixture grant after deliberately slow emulated startup.
            rig.call({'op':'grant_tool','id':rig.agent,'tool':'read',
                      'arguments':{'file_path':'input.txt'},'expires_at':int(time.time())+600})
            turn,_,task=await execute_and_observe(item)
            assert task['state']=='Verifying',task
            results.append({'agent':rig.agent,'task':task,'native_session':turn.native_session_ref})
        rc,out,err=await node(r,"const f=require('fs');console.log(JSON.stringify(Object.fromEntries(['cpu.max','memory.max','pids.max','memory.swap.max'].map(n=>[n,f.readFileSync('/sys/fs/cgroup/'+n,'utf8').trim()]))))")
        assert rc==0,err
        assert json.loads(out)=={'cpu.max':'50000 100000','memory.max':'536870912','pids.max':'96','memory.swap.max':'0'}
        before=pool(r);old_pid=r.supervisors[-1].pid
        await daemon('freeze');frozen=True
        os.kill(old_pid,signal.SIGKILL);await asyncio.to_thread(r.supervisors[-1].wait,15)
        # Cleanup is unknown, so a new Supervisor cannot acquire the guardian's
        # kernel lock, and the Core cannot reallocate the old volatile resources.
        duplicate=r.start();assert await asyncio.to_thread(duplicate.wait,30)!=0
        assert held(r) and pool(r)['used']==before['used']
        await daemon('stop');frozen=False
        stopped_pool=pool(r)
        assert held(r),stopped_pool
        await daemon('start')
        async with asyncio.timeout(90):
            while await asyncio.to_thread(inspect,r) is not None: await asyncio.sleep(.3)
        # Agent-only reconciliation: confirmed absence under the lock. It never
        # settles model reservations or resends the Task's original prompt.
        async with asyncio.timeout(60):
            while True:
                result=await asyncio.create_subprocess_exec(str(BIN),'--socket',str(r.core_socket),
                    'reconcile-runtime','--agent',r.agent,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
                stdout,stderr=await result.communicate()
                if result.returncode==0: break
                await asyncio.sleep(.3)
        assert not held(r)
        assert pool(r)['used']['memory_bytes']<=1024*M
        assert pool(r)['used']['storage_bytes']==3*256*M
        for item in items[:3]:
            a=item[0].call({'op':'runtime_status','id':item[0].agent})['agent']
            assert (a['id'],a['task_id'],a['workspace_id'],a['budget_id'])==(item[0].agent,item[0].task,item[0].ws,item[0].budget)
            assert item[0].call({'op':'task','id':item[0].task})['state']=='Verifying'
            assert len(item[0].call({'op':'artifacts','id':item[0].task}))==1
        assert len([e for e in r.call({'op':'events'}) if e['kind']=='prompt_dispatch'])==3
        (tmp_path/'pool-daemon.json').write_text(json.dumps({'results':results,'admitted':admitted,'before_outage':before,
            'while_daemon_stopped':stopped_pool,'after_reconciliation':pool(r),'old_supervisor':old_pid,
            'containers':containers,'events':r.call({'op':'events'})},indent=2))
    finally:
        if frozen: await daemon('stop');await daemon('start')
        for item in items: await item[2].close()
        for g in reversed(generators):
            try: next(g)
            except StopIteration: pass

@pytest.mark.asyncio
async def test_prepare_whole_host_reboot(tmp_path,monkeypatch):
    g,item=fixture(tmp_path,monkeypatch,4)
    r,store,adapter,core=item;configure_pool(r);r.start();wait_for(r.status,30)
    container=await runtime_ready(r,180)
    r.call({'op':'grant_tool','id':r.agent,'tool':'read','arguments':{'file_path':'input.txt'},'expires_at':int(time.time())+600})
    turn,_,task=await execute_and_observe(item)
    assert task['state']=='Verifying'
    artifacts=await core.request('artifacts',id=r.task);assert len(artifacts)==1
    native=turn.native_session_ref;assert native
    data={'path':str(r.path),'core_socket':str(r.core_socket),'agent':r.agent,'task':r.task,'ws':r.ws,'budget':r.budget,
          'runtime_home':str(r.home),'turn_id':turn.turn_id,'native_session':native,
          'container':container['Id'],'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
          'pool':pool(r),'artifacts':artifacts,'events':r.call({'op':'events'}),'prepared':True}
    (CONTROL/'reboot.json').write_text(json.dumps(data,indent=2));os.sync()
    # Intentionally leave the real Core/Supervisor/Agent running for VM shutdown.
    # No Python fixture stop, no fabricated Agent restart, no graceful pool release.
    os._exit(0)

@pytest.mark.asyncio
async def test_after_whole_host_reboot(tmp_path,monkeypatch):
    data=json.loads((CONTROL/'reboot.json').read_text());assert data['prepared']
    boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip();assert boot!=data['boot_id']
    r=object.__new__(Rig);r.path=Path(data['path']);r.core_socket=Path(data['core_socket'])
    for name in ('agent','task','ws','budget'): setattr(r,name,data[name])
    r.socket=r.core_socket.parent/f'{r.agent}.runtime.sock';r.workspace=r.path/'workspace'
    r.home=Path(data['runtime_home']);r.spec=r.path/'runtime.json';r.supervisors=[]
    r.log=(r.path/'runtime.log').open('a');r.core=None
    # Socket pathname is not a live identity; Core takes its unchanged private
    # DB/lock, validates the stale endpoint and recreates it on the new boot.
    r.core=subprocess.Popen([str(BIN),'--db',str(r.path/'core.db'),'--socket',str(r.core_socket),'serve'],stderr=r.log)
    async with asyncio.timeout(30):
        while True:
            try:
                if r.call({'op':'health'})['protocol']==1: break
            except subprocess.CalledProcessError: pass
            await asyncio.sleep(.2)
    assert pool(r)==data['pool']
    assert held(r), 'a reboot must not fabricate confirmed cleanup'
    store=SQLiteStore(str(r.path/'harness.db'));turn=store.get_turn(data['turn_id']);assert turn
    coordinator=HarnessCoordinator(store,MailboxManager());adapter=r.adapter()
    adapter.execution_socket=str(r.path/'gateway'/'gateway.sock')
    monkeypatch.setenv('KANE_SYSTEM_SOCKET',str(r.core_socket));core=SystemClient()
    adapter.execution_bridge=ExecutionBridge(store,r.agent,core);coordinator.register_adapter('kanaloa',adapter)
    try:
        r.start();await runtime_ready(r,180)
        await adapter.resume(turn,[])
        assert adapter.get_native_session(turn.turn_id)==data['native_session']
        assert inspect(r)['Id']!=data['container']
        assert (await core.request('artifacts',id=r.task))==data['artifacts']
        assert data['artifacts'][0]['content']=='# Verified fixture\n\nKANE_PR2_FIXTURE\n'
        assert len((r.home/'model-invocations.jsonl').read_text().splitlines())==2
        events=await core.request('events');assert len([e for e in events if e['kind']=='prompt_dispatch'])==1
        binding=(await core.request('runtime_status',id=r.agent))['agent']
        assert (binding['id'],binding['task_id'],binding['workspace_id'],binding['budget_id'])==(r.agent,r.task,r.ws,r.budget)
        state=await core.request('task',id=r.task);assert state['state']=='Verifying'
        done=await core.request('verify',id=r.task,version=state['version'],passed=True,
                                evidence='Independent exact fixture Artifact and native ACP context after whole-host reboot')
        assert done['state']=='Done'
        (tmp_path/'whole-host-reboot.json').write_text(json.dumps({'old_boot':data['boot_id'],'new_boot':boot,
            'binding':binding,'task':done,'session':data['native_session'],'pool':pool(r),'events':events},indent=2))
    finally:
        await adapter.close();r.close();store.close()


@pytest.mark.asyncio
async def test_normal_stop_releases_runtime_resources(tmp_path,monkeypatch):
    generator,item=fixture(tmp_path,monkeypatch,4)
    r,store,adapter,core=item
    try:
        configure_pool(r);r.start();wait_for(r.status,30)
        await runtime_ready(r,180)
        turn,_,state=await execute_and_observe(item)
        assert state['state']=='Verifying'
        artifacts=await core.request('artifacts',id=r.task)
        assert len(artifacts)==1 and artifacts[0]['content']=='# Verified fixture\n\nKANE_PR2_FIXTURE\n'
        assert held(r)
        assert r.control('stop',timeout=30)['ok']
        # Keep Core alive while the Supervisor finishes its ResourceLease Drop.
        # A stop acknowledgement alone is not evidence that teardown completed.
        assert await asyncio.to_thread(r.supervisors[-1].wait,30)==0
        after=pool(r)
        assert not held(r) and after['used']=={'cpu_millis':0,'memory_bytes':0,'pids':0,'storage_bytes':256*M}
        assert inspect(r) is None
        budget=await core.request('model_budget',id=r.budget)
        assert budget['used_tokens']==42 and budget['reserved_tokens']==0
        state=await core.request('task',id=r.task)
        done=await core.request('verify',id=r.task,version=state['version'],passed=True,
            evidence='Independent fixed Artifact after confirmed normal Supervisor exit and resource release')
        assert done['state']=='Done'
        (tmp_path/'normal-stop.json').write_text(json.dumps({'pool':after,'budget':budget,'task':done,
            'binding':r.call({'op':'runtime_status','id':r.agent}),'events':r.call({'op':'events'})},indent=2))
    finally:
        await adapter.close()
        try: next(generator)
        except StopIteration: pass
