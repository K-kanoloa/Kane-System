"""Actual two-boot reference VM, including abrupt QEMU loss with live processes."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import time
from types import SimpleNamespace

import pytest
from test_phase3_recovery import execution,offline,spec,command,start_core,attach,EXPECTED
from test_system_execution import run_task, execution as execution_fixture
from test_storage_runtime import runtime_ready
from test_sandbox_runtime import inspect
from test_supervised_runtime import Rig,BIN,ROOT,wait_for
from test_phase2_models import configure_native_gateway

pytestmark=pytest.mark.skipif(os.getenv('KANE_PHASE3_BOOT_TEST')!='1',reason='requires marked two-boot power-cut VM')
CONTROL=Path('/var/tmp/kane-phase3-control')
M=1048576

@pytest.fixture(autouse=True)
def disposable_only():
    assert Path('/etc/kane-storage-runtime-validation').read_text()=='disposable-vm\n'
    assert Path('/mnt/kane-p3-home').is_mount()

def port():
    with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]

def model_start(r,model_path):
    log=(r.path/'phase3-real-model.log').open('a')
    manager=subprocess.Popen([str(BIN),'--socket',str(r.core_socket),'model-run',str(model_path)],stderr=log)
    def ready():
        assert manager.poll() is None,(r.path/'phase3-real-model.log').read_text()[-2000:]
        rows=r.call({'op':'model_instances'})
        return next((v for v in rows if v['id']==json.loads(model_path.read_text())['instance_id'] and v['state']=='Ready'),None)
    row=wait_for(ready,180)
    return manager,log,row

@pytest.mark.asyncio
@pytest.mark.parametrize('execution',[{'sandbox':True,'bounded_home':True,'runtime_home':'/mnt/kane-p3-home','host_resources':True,'defer_start':True,'sandbox_cpu_millis':500,'max_restarts':10}],indirect=True)
async def test_prepare_live_power_cut(execution,tmp_path,monkeypatch):
    r,store,adapter,core=execution
    r.call({'op':'configure_host_resources','version':0,'policy':{'capacity':{'cpu_millis':2000,'memory_bytes':2560*M,'pids':2048,'storage_bytes':2048*M},'reserved':{'cpu_millis':250,'memory_bytes':512*M,'pids':64,'storage_bytes':256*M}}})
    r.start();await runtime_ready(r,300);print('phase3 stage: original Kanaloa runtime ready',flush=True)
    r.call({'op':'grant_tool','id':r.agent,'tool':'read','arguments':{'file_path':'input.txt'},'expires_at':int(time.time())+1200})
    turn,_,task=await run_task(execution)
    assert task['state']=='Verifying';print('phase3 stage: original read outcome Verifying',flush=True)
    artifacts=await core.request('artifacts',id=r.task);assert artifacts[0]['content']==EXPECTED
    await offline(r,store,adapter)
    made=command(r,'checkpoint','--spec',spec(r),r.path/'checkpoint');print('phase3 stage: quiescent native checkpoint published',flush=True)
    start_core(r);store,adapter,core=await attach(r);turn=store.get_turn(turn.turn_id)
    await runtime_ready(r,300);await adapter.resume(turn,[])
    real_model='phase3-reference-cpu'
    weights=Path(os.environ['KANE_LOCAL_MODEL_FILE']);server=Path(os.environ['KANE_LOCAL_MODEL_SERVER'])
    real_spec={'cloud':False,'scheduling':{'mode':'gateway_fifo','backend_kind':'local_http'},'source':{'backend_kind':'real-local-llm','weights_sha256':hashlib.sha256(weights.read_bytes()).hexdigest()},'budget_profile':{'provider':'kane-fixture','model':'deterministic-v1','upper_bound_tokens':2560,'bound_evidence':'2048 context + 512 hard output; actual local backend and Rust gateway','gateway_wait_ms':180000}}
    r.call({'op':'register_model','id':real_model,'spec':real_spec})
    instance=r.call({'op':'register_model_instance','model_id':real_model})['id'];backend_port=port()
    model=r.path/'phase3-model.json'
    model.write_text(json.dumps({'enforcement':{'mode':'cgroup_v2','root':'/sys/fs/cgroup/kane-phase3-validation/models'},'instance_id':instance,'command':[str(server),'--model',str(weights),'--host','127.0.0.1','--port',str(backend_port),'--alias','phase3-real','--ctx-size','2048','--parallel','1','--threads','1','--threads-http','1','--batch-size','128','--ubatch-size','64','--n-gpu-layers','0','--no-warmup','--cache-ram','0'],'runtime':{'endpoint':f'http://127.0.0.1:{backend_port}','backend_model':'phase3-real','context_tokens':2048,'parallelism':1,'resources':{'cpu_millis':500,'memory_bytes':512*M,'pids':64,'storage_bytes':128*M}}}))
    manager,log,row=await asyncio.to_thread(model_start,r,model);print('phase3 stage: real cgroup CPU model Ready',flush=True)
    # A second ORIGINAL Kanaloa sends a genuine provider request through the
    # same Core/Rust FIFO/Gateway. Cut while it is dispatched, not merely healthy.
    second_case=tmp_path/'inflight-real';second_case.mkdir()
    gen=execution_fixture.__wrapped__(second_case,monkeypatch,SimpleNamespace(param={'sandbox':True,'bounded_home':True,'runtime_home':'/mnt/kane-p3-inflight','host_resources':True,'defer_start':True,'sandbox_cpu_millis':500,'max_restarts':10,'core_socket':str(r.core_socket),'logical_model_id':real_model,'phase2_model_spec':real_spec,'initialize_timeout_seconds':180}))
    second=next(gen);r2,store2,adapter2,core2=second
    r.call({'op':'configure_model_gateway','id':real_model,'version':0,'policy':{'instance_id':instance,'cache':'disabled','max_output_tokens':512,'workspaces':[r2.ws],'workload_classes':['execution'],'sensitivity':'public','timeout_seconds':45,'requires_kernel_limits':True}})
    gateway=r.path/'phase3-model-gateway.sock'
    proxy=subprocess.Popen([str(BIN),'--socket',str(r.core_socket),'model-gateway',str(gateway)],stderr=log)
    wait_for(lambda:gateway.exists(),30)
    configure_native_gateway(second,gateway,2048,512,{'strict':True},second_case)
    r2.start();await runtime_ready(r2,300);print('phase3 stage: second original Kanaloa ready',flush=True)
    turn2,_,_=await run_task(second,wait=False,prompt='Write a long detailed numbered account of the planets. Keep writing until the output limit and do not call tools.')
    async with asyncio.timeout(180):
        while True:
            calls=[q for q in await core2.request('model_queue') if q['task_id']==r2.task]
            if calls:
                if calls[0]['state']=='Dispatched':break
                assert calls[0]['state']=='Queued',calls
            await asyncio.sleep(.1)
    print('phase3 stage: real model request Dispatched, preparing actual cut',flush=True)
    unknown_before={'path':str(r2.path),'agent':r2.agent,'task':r2.task,'workspace':r2.ws,'budget':r2.budget,'native_session':turn2.native_session_ref,'queue':calls[0],'budget_before_cut':await core2.request('model_budget',id=r2.budget),'binding':(await core2.request('runtime_status',id=r2.agent))['agent']}
    assert unknown_before['budget_before_cut']['reserved_tokens']==2560
    boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    data={'path':str(r.path),'core_socket':str(r.core_socket),'agent':r.agent,'task':r.task,'ws':r.ws,'budget':r.budget,'home':str(r.home),'turn':turn.turn_id,'native_session':turn.native_session_ref,'boot_id':boot,'checkpoint':made['id'],'model_file':str(model),'model':row,'model_manager_pid':manager.pid,'core_pid':r.core.pid,'supervisor_pid':r.supervisors[-1].pid,'agent_container':inspect(r),'artifacts':artifacts,'pool':r.call({'op':'host_resources'}),'model_calls':(r.home/'model-invocations.jsonl').read_text(),'inflight_real':unknown_before}
    CONTROL.mkdir(mode=0o700,exist_ok=True)
    (CONTROL/'before.json').write_text(json.dumps(data,indent=2));os.sync()
    marker=Path('/exchange/phase3-power-cut-ready.json');temporary=marker.with_suffix('.tmp')
    with temporary.open('w') as f:json.dump({'ready':True,'boot_id':boot,'state_file':str(CONTROL/'before.json'),'core_pid':r.core.pid,'supervisor_pid':r.supervisors[-1].pid,'model_pid':row['pid'],'task':r.task,'state':'Verifying'},f);f.flush();os.fsync(f.fileno())
    temporary.replace(marker)
    # No fixture teardown: driver cuts the live kernel while Core commits more
    # real object+ledger transactions. The second boot verifies their atomicity.
    while True:
        r.call({'op':'create_budget','tokens':17})
        await asyncio.sleep(.001)

@pytest.mark.asyncio
async def test_recover_after_abrupt_whole_host_loss(tmp_path,monkeypatch):
    data=json.loads((CONTROL/'before.json').read_text());boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip();assert boot!=data['boot_id']
    assert Path('/exchange/power-cut.json').is_file()
    r=object.__new__(Rig);r.path=Path(data['path']);r.core_socket=Path(data['core_socket']);r.socket=r.core_socket.parent/f"{data['agent']}.runtime.sock"
    for name in ['agent','task','ws','budget']:setattr(r,name,data[name])
    r.home=Path(data['home']);r.workspace=r.path/'workspace';r.spec=r.path/'runtime.json';r.supervisors=[];r.core=None;r.log=(r.path/'runtime.log').open('a');r.sandboxed=True
    monkeypatch.setenv('KANE_SYSTEM_SOCKET',str(r.core_socket));start_core(r)
    before=r.call({'op':'host_resources'});assert before['used']==data['pool']['used']
    row=next(x for x in r.call({'op':'model_instances'}) if x['id']==data['model']['id']);assert row['state']=='Failed' and row['held'] is True
    reconciled=subprocess.run([str(BIN),'--socket',str(r.core_socket),'model-reconcile',row['id']],text=True,capture_output=True,timeout=60);assert reconciled.returncode==0,reconciled.stderr
    released=next(x for x in r.call({'op':'model_instances'}) if x['id']==row['id']);assert released['state']=='Unloaded' and not released['held']
    assert (r.home/'model-invocations.jsonl').read_text()==data['model_calls']
    uncertain=data['inflight_real']
    unknown_budget=r.call({'op':'model_budget','id':uncertain['budget']});assert unknown_budget['reserved_tokens']==2560 and unknown_budget['used_tokens']==0
    assert r.call({'op':'task','id':uncertain['task']})['state']=='Blocked'
    frozen=next(q for q in r.call({'op':'model_queue'}) if q['call_id']==uncertain['queue']['call_id']);assert frozen['state']=='Unknown'
    binding2=r.call({'op':'runtime_status','id':uncertain['agent']})['agent']
    for field in ['id','task_id','workspace_id','budget_id','model_id']:assert binding2[field]==uncertain['binding'][field]
    absence=subprocess.run([str(BIN),'--socket',str(r.core_socket),'reconcile-runtime','--agent',uncertain['agent']],text=True,capture_output=True,timeout=60);assert absence.returncode==0,absence.stderr
    assert r.call({'op':'model_budget','id':uncertain['budget']})==unknown_budget
    manager,model_log,reloaded=await asyncio.to_thread(model_start,r,Path(data['model_file']))
    assert reloaded['id']==row['id'] and reloaded['generation']!=data['model']['generation']
    assert next(q for q in r.call({'op':'model_queue'}) if q['call_id']==uncertain['queue']['call_id'])['state']=='Unknown'
    assert r.call({'op':'model_budget','id':uncertain['budget']})==unknown_budget
    with socket.socket(socket.AF_UNIX) as ctl:
        ctl.settimeout(30);ctl.connect(str(r.core_socket.parent/(row['id']+'.model.sock')));ctl.sendall(b'stop\n');answer=ctl.makefile('rb').readline();assert answer==b'stopped\n',answer
    assert await asyncio.to_thread(manager.wait,60)==0
    model_log.close()
    store,adapter,core=await attach(r)
    try:
        await runtime_ready(r,300);turn=store.get_turn(data['turn']);await adapter.resume(turn,[])
        assert adapter.get_native_session(turn.turn_id)==data['native_session']
        state=await core.request('task',id=r.task);assert state['state']=='Verifying'
        artifacts=await core.request('artifacts',id=r.task);assert artifacts==data['artifacts']
        assert artifacts[0]['content']==EXPECTED and artifacts[0]['content_hash']==hashlib.sha256(EXPECTED.encode()).hexdigest()
        assert (r.home/'model-invocations.jsonl').read_text()==data['model_calls']
        events=await core.request('events');assert len([x for x in events if x['kind']=='prompt_dispatch' and x['object_id']==r.task])==1
        done=await core.request('verify',id=r.task,version=state['version'],passed=True,evidence='Independent exact Artifact/hash and original read result after actual QEMU power cut, WAL recovery, new kernel boot and native Session resume')
        assert done['state']=='Done'
        binding=(await core.request('runtime_status',id=r.agent))['agent']
        for key,name in [('id','agent'),('task_id','task'),('workspace_id','ws'),('budget_id','budget')]:assert binding[key]==data[name]
        # Actual post-cut rows and creation events must both exist, or neither.
        db=sqlite3.connect(r.path/'core.db');assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok';assert db.execute('PRAGMA foreign_key_check').fetchall()==[]
        budgets=db.execute('SELECT COUNT(*) FROM resource_budget').fetchone()[0];budget_events=db.execute("SELECT COUNT(*) FROM ledger WHERE kind='budget_created'").fetchone()[0];assert budgets==budget_events;db.close()
        command(r,'checkpoint-check',r.path/'checkpoint')
        (tmp_path/'whole-host-semantic-recovery.json').write_text(json.dumps({'before_boot':data['boot_id'],'after_boot':boot,'binding':binding,'task':done,'native_session':data['native_session'],'model_before':data['model'],'model_invalidated':row,'model_reconciled':released,'model_reloaded':reloaded,'real_inflight_unknown_budget':unknown_budget,'real_inflight_queue_after_boot':frozen,'real_agent_binding_after_boot':binding2,'resource_pool_before_explicit_reclaim':before,'budget_rows':budgets,'budget_events':budget_events,'events':events,'automatic_prompt_replays':0,'recovery_wall_seconds':time.time()-json.loads(Path('/exchange/power-cut.json').read_text())['host_time'],'timing_scope':'one software-emulated QEMU scenario; no performance SLA'},indent=2))
    finally:
        await adapter.close();store.close();r.close()


def test_checkpoint_enospc_never_publishes_a_checkpoint(tmp_path):
    from test_phase3_recovery import isolated_data
    r,data,declaration=isolated_data(tmp_path)
    good=tmp_path/'before-full';command(r,'checkpoint','--spec',declaration,good)
    mount=Path('/mnt/kane-p3-full');assert mount.is_mount()
    count=0
    with (mount/'phase3-capacity-fill').open('xb') as f:
        try:
            while count<80*M:count+=f.write(b'x'*65536)
            raise AssertionError('dedicated snapshot filesystem did not enforce its size')
        except OSError as e:assert e.errno==28
        try:f.flush();os.fsync(f.fileno())
        except OSError as e:assert e.errno==28
        # Leave enough for staging/data but deliberately less than the Registry
        # snapshot. Intent/checkpoint metadata can commit while publication fails.
        f.truncate(max(0,count-65536));f.flush();os.fsync(f.fileno())
    db=sqlite3.connect(tmp_path/'core.db');before=db.execute("SELECT COUNT(*) FROM ledger WHERE kind='checkpoint'").fetchone()[0];db.close()
    refused=command(r,'checkpoint','--spec',declaration,mount/'should-not-publish',ok=False)
    assert refused.returncode!=0 and not (mount/'should-not-publish').exists()
    db=sqlite3.connect(tmp_path/'core.db');after=db.execute("SELECT COUNT(*) FROM ledger WHERE kind='checkpoint'").fetchone()[0];assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok';db.close();assert after>before,'fault did not reach committed metadata before failed snapshot publication'
    command(r,'checkpoint-check',good)
    (tmp_path/'checkpoint-enospc.json').write_text(json.dumps({'errno':28,'dedicated_filesystem':str(mount),'written_bytes':count,'checkpoint_not_published':True,'prior_checkpoint_valid':True,'checkpoint_events_before':before,'checkpoint_events_after':after}))
