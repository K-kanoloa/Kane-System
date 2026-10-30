"""PR-4 actual Docker/cgroups, existing Kanaloa, no production model or tools."""
import asyncio,json,os,signal,subprocess,time
from pathlib import Path
from types import SimpleNamespace
import pytest
from test_system_execution import execution,run_task
from test_supervised_runtime import dead

pytestmark=pytest.mark.skipif(os.getenv('KANE_PR4_TEST')!='1',reason='opt-in Docker cgroups isolation integration')

def inspect(r):
    p=subprocess.run(['docker','inspect','kane-'+r.agent],text=True,capture_output=True)
    return json.loads(p.stdout)[0] if p.returncode==0 else None

async def ready(r):
    async with asyncio.timeout(20):
        while True:
            c=await asyncio.to_thread(inspect,r)
            if c and c['State']['Running']: return c
            await asyncio.sleep(.05)

async def node(r,code,timeout=20):
    exe=json.loads(r.spec.read_text())['command'][0]
    p=await asyncio.create_subprocess_exec('docker','exec','kane-'+r.agent,exe,'-e',code,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    stdout,stderr=await asyncio.wait_for(p.communicate(),timeout)
    return p.returncode,stdout.decode(),stderr.decode()

@pytest.mark.asyncio
@pytest.mark.parametrize('execution',[{'sandbox':True}],indirect=True)
async def test_original_kanaloa_executes_under_real_container_limits(execution,tmp_path):
    r,store,adapter,core=execution
    try:
        container=await ready(r)
        _,_,state=await run_task(execution)
        assert state['state']=='Verifying'
        rc,out,err=await node(r,"const f=require('fs');console.log(JSON.stringify(Object.fromEntries(['cpu.max','memory.max','pids.max','memory.swap.max'].map(n=>[n,f.readFileSync('/sys/fs/cgroup/'+n,'utf8').trim()]))))")
        assert rc==0,err
        limits=json.loads(out)
        assert limits=={'cpu.max':'50000 100000','memory.max':'536870912','pids.max':'96','memory.swap.max':'0'}
        assert container['HostConfig']['NetworkMode']=='none'
        assert len(await core.request('artifacts',id=r.task))==1
        (tmp_path/'sandbox.json').write_text(json.dumps({'limits':limits,'container':container},indent=2))
    finally: await adapter.close()

@pytest.fixture
def three_sandboxes(tmp_path,monkeypatch):
    monkeypatch.setenv("KANE_PR4_HOST_SECRET","isolated-test-canary")
    generators=[];items=[]
    try:
        for i in range(3):
            case=tmp_path/str(i);case.mkdir()
            config={'sandbox':True,'delay_ms':800 if i==0 else 100}
            if items:config['core_socket']=str(items[0][0].core_socket)
            g=execution.__wrapped__(case,monkeypatch,SimpleNamespace(param=config))
            generators.append(g);items.append(next(g))
        yield items
    finally:
        for g in reversed(generators):
            try:next(g)
            except StopIteration:pass

@pytest.mark.asyncio
async def test_three_agents_share_core_keep_bindings_and_recover_one_without_replay(three_sandboxes,tmp_path):
    items=three_sandboxes
    try:
        await asyncio.gather(*(ready(x[0]) for x in items))
        containers=[inspect(x[0])['Id'] for x in items]
        starts=[asyncio.create_task(run_task(x)) for x in items]
        r,_,_,core=items[0]
        async with asyncio.timeout(15):
            while not (r.home/'model-invocations.jsonl').exists(): await asyncio.sleep(.02)
        p=await asyncio.create_subprocess_exec('docker','kill','kane-'+r.agent,stdout=asyncio.subprocess.DEVNULL)
        assert await p.wait()==0
        outcomes=await asyncio.gather(*starts)
        assert outcomes[0][2]['state']=='Blocked'
        assert all(x[2]['state']=='Verifying' for x in outcomes[1:])
        async with asyncio.timeout(20):
            while True:
                c=await asyncio.to_thread(inspect,r)
                if c and c['Id']!=containers[0] and c['State']['Running']:break
                await asyncio.sleep(.05)
        for i,(rig,_,_,client) in enumerate(items):
            a=(await client.request('runtime_status',id=rig.agent))['agent']
            assert a['id']==rig.agent and a['task_id']==rig.task and a['workspace_id']==rig.ws and a['budget_id']==rig.budget
            if i: assert inspect(rig)['Id']==containers[i]
        events=await core.request('events')
        assert len([e for e in events if e['kind']=='prompt_dispatch'])==3
        assert len((r.home/'model-invocations.jsonl').read_text().splitlines())==1
        assert len({x[0].agent for x in items})==3
        assert len({x[0].budget for x in items})==3
        (tmp_path/'three-agent-results.json').write_text(json.dumps({'tasks':[x[2] for x in outcomes],'events':events,'containers_before':containers},indent=2))
    finally:
        await asyncio.gather(*(x[2].close() for x in items))

@pytest.mark.asyncio
async def test_kernel_boundaries_and_actual_cpu_memory_pid_limits(three_sandboxes,tmp_path):
    items=three_sandboxes;r,_,adapter,core=items[0]
    evidence={}
    try:
        await asyncio.gather(*(ready(x[0]) for x in items))
        await asyncio.gather(*(run_task(x) for x in items))
        others=[inspect(x[0])['Id'] for x in items[1:]]
        paths=[str(r.core_socket),str(r.core_socket.parent/'core.db'),str(items[1][0].workspace/'input.txt'),'/var/run/docker.sock']
        code="const f=require('fs');const p="+json.dumps(paths)+";const out={hidden:p.map(p=>!f.existsSync(p)),uid:process.getuid(),env:process.env};try{f.writeFileSync("+json.dumps(str(r.workspace/'input.txt'))+",'BAD');out.write='allowed'}catch(e){out.write=e.code}console.log(JSON.stringify(out));"
        rc,out,err=await node(r,code);assert rc==0,err
        boundary=json.loads(out);assert all(boundary['hidden']) and boundary['uid']==os.getuid() and boundary['write']=='EROFS'
        assert (r.workspace/'input.txt').read_text()=='KANE_PR2_FIXTURE\n'
        assert not any(k in boundary['env'] for k in ['KANE_SYSTEM_SOCKET','OPENAI_API_KEY','DOCKER_HOST','KANE_PR4_HOST_SECRET'])
        evidence['boundary']=boundary
        # This host listener is intentionally not forwarded to the network namespace.
        server=await asyncio.start_server(lambda reader,writer:writer.close(),'127.0.0.1',0)
        port=server.sockets[0].getsockname()[1]
        rc,out,err=await node(r,f"const s=require('net').connect({port},'127.0.0.1');s.on('connect',()=>{{console.log('CONNECTED');s.end()}});s.on('error',e=>console.log(e.code));s.setTimeout(2000,()=>{{console.log('TIMEOUT');s.destroy()}})")
        server.close();await server.wait_closed();assert rc==0 and 'ECONNREFUSED' in out
        evidence['host_network']=out.strip()
        rc,out,err=await node(r,"const f=require('fs'),p='/sys/fs/cgroup/cpu.stat';const before=f.readFileSync(p,'utf8');const end=Date.now()+1500;while(Date.now()<end){};console.log(JSON.stringify({before,after:f.readFileSync(p,'utf8')}));")
        assert rc==0,err;cpu=json.loads(out)
        counter=lambda text,key:int(dict(line.split() for line in text.splitlines() if line.strip())[key])
        assert counter(cpu['after'],'nr_throttled')>counter(cpu['before'],'nr_throttled');evidence['cpu']=cpu
        rc,out,err=await node(r,"const {spawn}=require('child_process');const children=[];let denied=0,pending=180;for(let i=0;i<180;i++){const c=spawn('/bin/sleep',['10']);children.push(c);c.on('error',e=>{if(e.code==='EAGAIN')denied++;done()});c.on('spawn',done)}function done(){if(--pending===0){console.log(JSON.stringify({denied,attempts:180}));for(const c of children)c.kill('SIGKILL')}}")
        assert rc==0,err;pids=json.loads(out);assert pids['denied']>0;evidence['pids']=pids
        rc,out,err=await node(r,"console.log(require('fs').readFileSync('/sys/fs/cgroup/memory.events','utf8'))")
        assert rc==0;before=out
        rc,out,err=await node(r,"const blocks=[];for(let i=0;i<128;i++)blocks.push(Buffer.alloc(16*1024*1024,1));console.log('UNBOUNDED')",timeout=25)
        assert rc!=0 and 'UNBOUNDED' not in out
        rc2,after,err2=await node(r,"console.log(require('fs').readFileSync('/sys/fs/cgroup/memory.events','utf8'))")
        assert rc2==0,err2
        assert counter(after,'oom_kill')>counter(before,'oom_kill');evidence['memory']={'before':before,'after':after,'probe_exit':rc}
        assert [inspect(x[0])['Id'] for x in items[1:]]==others
        assert all([(await x[3].request('task',id=x[0].task))['state']=='Verifying' for x in items[1:]])
        (tmp_path/'kernel-evidence.json').write_text(json.dumps(evidence,indent=2))
    finally: await asyncio.gather(*(x[2].close() for x in items))

@pytest.mark.asyncio
@pytest.mark.parametrize('execution',[{'sandbox':True}],indirect=True)
async def test_supervisor_sigkill_cleans_container_before_new_owner(execution,tmp_path):
    r,_,adapter,core=execution
    try:
        c=await ready(r)
        await adapter.activate()
        guardian=r.status()['pid']
        supervisor=r.supervisors[-1]
        supervisor.kill();supervisor.wait(timeout=5)
        async with asyncio.timeout(15):
            while await asyncio.to_thread(inspect,r) is not None:await asyncio.sleep(.05)
        # Guardian still holds the lock during cleanup; wait for its exit, then
        # prove the replacement has one container and the same logical identity.
        async with asyncio.timeout(15):
            while not dead(guardian): await asyncio.sleep(.03)
        r.start()
        replacement=await ready(r)
        assert replacement['Id']!=c['Id']
        assert (await core.request('runtime_status',id=r.agent))['agent']['id']==r.agent
        (tmp_path/'orphan-cleanup.json').write_text(json.dumps({'old_container':c['Id'],'new_container':replacement['Id'],'agent':r.agent},indent=2))
    finally: await adapter.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('execution',[{'sandbox':True,'native_file_proxy':True,'test_write':True}],indirect=True)
@pytest.mark.parametrize('failure',[None,'lease_transfer','revoke','unavailable','lost_file_reply','existing_file','symlink'])
async def test_original_write_uses_rust_proxy_with_readonly_workspace(execution,tmp_path,failure):
    from app.system.client import CoreUnavailable
    r,_,adapter,core=execution
    original=core.request
    file_calls=0
    output=r.workspace/'output.txt'
    if failure=='existing_file': output.write_text('HUMAN_VERSION')
    if failure=='symlink': output.symlink_to(r.workspace/'input.txt')
    async def intercept(op,**kw):
        nonlocal file_calls
        if op=='tool_file':
            file_calls+=1
            if failure=='lease_transfer':
                await original('transfer_lease',id=r.ws,epoch=1,holder='human',expires_at=int(time.time())+300)
            if failure=='revoke': await original('revoke_capability',id=r.modify_cap)
            if failure=='unavailable': raise CoreUnavailable('injected before commit')
        result=await original(op,**kw)
        if op=='tool_file' and failure=='lost_file_reply': raise CoreUnavailable('injected lost committed file reply')
        return result
    core.request=intercept
    try:
        container=await ready(r)
        assert next(m for m in container['Mounts'] if m['Destination']==str(r.workspace))['RW'] is False
        _,_,task=await run_task(execution)
        assert (r.workspace/'input.txt').read_text()=='KANE_PR2_FIXTURE\n'
        commits=[e for e in await original('events') if e['kind']=='native_file_commit']
        if failure is None or failure=='lost_file_reply':
            assert file_calls==1
            assert output.read_text()=='KANE_PR3B_SAFE_FIXTURE\n'
            assert len(commits)==1
            assert (await original('workspace',id=r.ws))['version']==1
        else:
            assert not commits
            if failure=='existing_file': assert output.read_text()=='HUMAN_VERSION'
            elif failure=='symlink': assert output.is_symlink()
            else: assert not output.exists()
        assert task['state']==('Verifying' if failure is None else 'Blocked')
        intents=[e for e in await original('events') if e['kind']=='intent']
        action=await original('action',id=intents[0]['payload']['action'])
        assert action['state']==('Confirmed' if failure is None else 'Unknown')
        # Even after a successful proxy write the Agent cannot write the directory.
        rc,out,err=await node(r,"try{require('fs').writeFileSync("+json.dumps(str(r.workspace/'bypass.txt'))+",'bad');console.log('ALLOWED')}catch(e){console.log(e.code)}")
        assert rc==0 and 'EROFS' in out,err
        (tmp_path/'native-file.json').write_text(json.dumps({'failure':failure,'task':task,'action':action,'commits':commits,'file_calls':file_calls},indent=2))
    finally: await adapter.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('execution',[{'sandbox':True},{'sandbox':True,'max_restarts':0}],indirect=True)
async def test_supervisor_and_guardian_loss_cleans_before_new_generation(execution,tmp_path):
    r,_,adapter,core=execution
    try:
        await adapter.activate()
        before=await ready(r)
        status=r.status()
        # Stop the parent first to prevent its wait/restart loop racing the injection.
        supervisor=r.supervisors[-1]
        os.kill(supervisor.pid,signal.SIGSTOP)
        os.kill(status['pid'],signal.SIGKILL)
        supervisor.kill();supervisor.wait(timeout=5)
        assert inspect(r)['Id']==before['Id']  # Double crash really left an orphan.
        replacement=r.start()
        if json.loads(r.spec.read_text())['max_restarts']==0:
            async with asyncio.timeout(20):
                while replacement.poll() is None: await asyncio.sleep(.05)
            assert replacement.returncode!=0
            assert inspect(r) is None
            assert (await core.request('runtime_status',id=r.agent))['agent']['state']=='Failed'
            (tmp_path/'exhausted-cleanup.json').write_text(json.dumps({'old_container':before['Id'],'restart_exit':replacement.returncode,'container_absent':True},indent=2))
            return
        async with asyncio.timeout(20):
            while True:
                after=await asyncio.to_thread(inspect,r)
                if after and after['Id']!=before['Id'] and after['State']['Running']:break
                await asyncio.sleep(.05)
        a=(await core.request('runtime_status',id=r.agent))['agent']
        assert (a['id'],a['task_id'],a['workspace_id'],a['budget_id'])==(r.agent,r.task,r.ws,r.budget)
        assert not [e for e in await core.request('events') if e['kind']=='prompt_dispatch']
        (tmp_path/'double-crash.json').write_text(json.dumps({'before':before['Id'],'after':after['Id'],'binding':a},indent=2))
    finally: await adapter.close()
