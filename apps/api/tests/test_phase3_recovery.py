"""Phase3: original Kanaloa/DSH, real local files/SQLite and explicit Owner recovery."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time

import pytest
from app.harness.coordinator import HarnessCoordinator
from app.harness.mailbox import MailboxManager
from app.store.sqlite_store import SQLiteStore
from app.system.client import CoreUnavailable, SystemClient
from app.system.execution import ExecutionBridge
from app.domain.models import Message
from test_supervised_runtime import BIN, wait_for
from test_system_execution import execution, run_task

pytestmark=pytest.mark.skipif(os.getenv('KANE_PHASE3_TEST')!='1',reason='opt-in Phase3 native-data/Owner reconciliation integration')
EXPECTED='# Verified fixture\n\nKANE_PR2_FIXTURE\n'

def command(r,*args,ok=True):
    p=subprocess.run([str(BIN),'--db',str(r.path/'core.db'),*map(str,args)],text=True,capture_output=True,timeout=90)
    with (r.path/'checkpoint-cli.log').open('a') as f:f.write(json.dumps({'argv':list(map(str,args)),'returncode':p.returncode,'stdout':p.stdout,'stderr':p.stderr})+'\n')
    if ok:assert p.returncode==0,p.stderr
    return json.loads(p.stdout) if p.returncode==0 else p

def start_core(r,db=None):
    if r.core is not None and r.core.poll() is None:return
    r.core=subprocess.Popen([str(BIN),'--db',str(db or r.path/'core.db'),'--socket',str(r.core_socket),'serve'],stderr=r.log)
    wait_for(lambda:r.call({'op':'health'}),20)

async def offline(r,store,adapter):
    await adapter.close()
    for p in r.supervisors:
        if p.poll() is None:assert await asyncio.to_thread(p.wait,20)==0
    store.close()
    if r.core and r.core.poll() is None:r.core.terminate();await asyncio.to_thread(r.core.wait,10)

def spec(r):
    p=r.path/'snapshot-spec.json'
    readonly=json.loads(r.spec.read_text()).get('sandbox',{}).get('read_only',[])
    p.write_text(json.dumps({'readonly_dependencies':readonly,'roots':[{'name':'workspace','path':str(r.workspace),'kind':'directory','max_bytes':1048576},{'name':'home','path':str(r.home),'kind':'directory','max_bytes':64*1048576},{'name':'harness','path':str(r.path/'harness.db'),'kind':'sqlite','max_bytes':16*1048576}]}))
    return p

def proof(checkpoint_id,evidence):
    text=json.dumps(evidence,sort_keys=True)
    return {'checkpoint_id':checkpoint_id,'post_checkpoint_effects_checked':True,'source_ref':'independent fixture/native files/Core receipts','sha256':hashlib.sha256(text.encode()).hexdigest()}

def archive_native_sources(r):
    # Only this test's isolated data. Keep original bytes; never overwrite the old version.
    archive=r.path/'archived-before-install';archive.mkdir(mode=0o700)
    for root,name in [(r.workspace,'workspace'),(r.home,'home')]:
        target=archive/name;target.mkdir(mode=0o700);root.chmod(0o700)
        for p in list(root.iterdir()):
            if p.name=='lost+found':continue
            shutil.move(str(p),str(target/p.name))
    for suffix in ['', '-wal','-shm']:
        p=r.path/('harness.db'+suffix)
        if p.exists():shutil.move(str(p),str(archive/p.name))
    return archive

def install(r,checkpoint):
    archive_native_sources(r)
    return [command(r,'checkpoint-install',checkpoint,'--root',name) for name in ['workspace','home','harness']]

async def attach(r):
    store=SQLiteStore(str(r.path/'harness.db'));core=SystemClient();adapter=r.adapter(initialize_timeout_seconds=180)
    adapter.execution_socket=str(r.path/'gateway'/'gateway.sock') if getattr(r,'sandboxed',False) else str(r.socket)+'.budget.sock'
    adapter.execution_bridge=ExecutionBridge(store,r.agent,core)
    HarnessCoordinator(store,MailboxManager()).register_adapter('kanaloa',adapter)
    r.start();wait_for(r.status,30)
    return store,adapter,core

@pytest.mark.asyncio
@pytest.mark.parametrize('execution',[{'sandbox':True,'max_restarts':10}],indirect=True)
async def test_quiescent_native_snapshot_restore_original_session_to_verified_outcome(execution,tmp_path):
    r,store,adapter,core=execution
    active_store=store;active_adapter=adapter
    try:
        turn,_,task=await run_task(execution)
        assert task['state']=='Verifying'
        artifacts=await core.request('artifacts',id=r.task);assert artifacts[0]['content']==EXPECTED
        original_binding=(await core.request('runtime_status',id=r.agent))['agent']
        before_calls=(r.home/'model-invocations.jsonl').read_text()
        snapshot=r.path/'checkpoint';p=spec(r)
        rejected=command(r,'checkpoint','--spec',p,snapshot,ok=False);assert rejected.returncode!=0
        await offline(r,store,adapter)
        made=command(r,'checkpoint','--spec',p,snapshot)
        command(r,'checkpoint-check',snapshot)
        # The independent original Artifact, native Session and harness DB are all actual bytes.
        assert (snapshot/'roots/workspace/input.txt').read_text()=='KANE_PR2_FIXTURE\n'
        assert turn.native_session_ref in '\n'.join(str(p) for p in (snapshot/'roots/home').rglob('*'))
        restored=command(r,'checkpoint-restore',snapshot,r.path/'restored')
        installed=install(r,snapshot)
        assert all(v['verified'] for v in installed)
        start_core(r,restored['db']);active_store,active_adapter,core=await attach(r)
        restored_turn=active_store.get_turn(turn.turn_id);assert restored_turn.native_session_ref==turn.native_session_ref
        await active_adapter.resume(restored_turn,[])
        state=await core.request('task',id=r.task);assert state['state']=='Blocked' and state['block_reason']=='restore-review'
        assert (r.home/'model-invocations.jsonl').read_text()==before_calls
        assert len([e for e in await core.request('events') if e['kind']=='prompt_dispatch'])==1
        assert (await core.request('artifacts',id=r.task))==artifacts
        reviewed=await core.request('review_restored_task',id=r.task,version=state['version'],evidence=proof(made['id'],{'installed':installed,'artifact_hash':artifacts[0]['content_hash'],'calls_not_replayed':True}))
        assert reviewed['state']=='Verifying'
        assert artifacts[0]['content']==EXPECTED and hashlib.sha256(EXPECTED.encode()).hexdigest()==artifacts[0]['content_hash']
        done=await core.request('verify',id=r.task,version=reviewed['version'],passed=True,evidence='Independent expected bytes/hash/original read evidence after native checkpoint install and ACP Session restore')
        assert done['state']=='Done'
        binding=(await core.request('runtime_status',id=r.agent))['agent']
        for key in ['id','task_id','workspace_id','budget_id','model_id']:assert binding[key]==original_binding[key]
        (tmp_path/'native-snapshot-outcome.json').write_text(json.dumps({'checkpoint':made,'restore':restored,'installed':installed,'task':done,'native_session':turn.native_session_ref,'binding':binding,'events':await core.request('events'),'budget':await core.request('model_budget',id=r.budget)},indent=2))
    finally:
        await active_adapter.close();active_store.close();start_core(r)

@pytest.mark.asyncio
@pytest.mark.parametrize('execution',[{'sandbox':True,'max_restarts':10}],indirect=True)
@pytest.mark.parametrize('lost_continuation_ack',[False,True])
async def test_lost_usage_receipt_requires_owner_evidence_then_explicit_new_continuation(execution,tmp_path,lost_continuation_ack):
    r,store,adapter,core=execution;original=core.request;captured=[];active_store=store;active_adapter=adapter
    async def request(op,**kw):
        if op=='settle_model' and kw['receipt'].get('usage',{}).get('totalTokens')==27:
            captured.append(json.loads(json.dumps(kw)));raise CoreUnavailable('deliberate lost result before settlement')
        return await original(op,**kw)
    core.request=request
    try:
        turn,message,task=await run_task(execution)
        assert task['state']=='Blocked' and captured
        calls=(r.home/'model-invocations.jsonl').read_text();assert len(calls.splitlines())==2
        before=await original('model_budget',id=r.budget);assert before['reserved_tokens']==32
        await offline(r,store,adapter);made=command(r,'checkpoint','--spec',spec(r),r.path/'checkpoint')
        restored=command(r,'checkpoint-restore',r.path/'checkpoint',r.path/'restored');installed=install(r,r.path/'checkpoint')
        start_core(r,restored['db']);active_store,active_adapter,core=await attach(r)
        restored_turn=active_store.get_turn(turn.turn_id);await active_adapter.resume(restored_turn,[])
        assert (await core.request('model_budget',id=r.budget))==before
        assert (r.home/'model-invocations.jsonl').read_text()==calls
        state=await core.request('task',id=r.task)
        with pytest.raises(Exception):await core.request('review_restored_task',id=r.task,version=state['version'],evidence=proof(made['id'],installed))
        receipt=captured[0];actual=receipt['receipt']['usage'];evidence=proof(made['id'],receipt)
        await core.request('reconcile_model_call',call_id=receipt['call_id'],result={'call_id':receipt['call_id'],'outcome':'Confirmed','usage':actual},evidence=evidence)
        budget=await core.request('model_budget',id=r.budget);assert budget['used_tokens']==42 and budget['reserved_tokens']==0
        assert (await core.request('task',id=r.task))['state']=='Blocked'
        fresh=Message(message_id='phase3-new-explicit-message',conversation_id=restored_turn.conversation_id,turn_id=restored_turn.turn_id,sender='user',content='Continue from the already confirmed read result. Return its original marker as Markdown; do not repeat the read.')
        review=proof(made['id'],{'receipt':receipt,'installed':installed,'confirmed_tool_effects':1})
        review['continuation']={'turn_id':turn.turn_id,'message_id':fresh.message_id,'content':fresh.content,'native_session_ref':turn.native_session_ref}
        state=await core.request('task',id=r.task)
        await core.request('review_restored_task',id=r.task,version=state['version'],evidence=review)
        r.call({'op':'grant_tool','id':r.agent,'tool':'read','arguments':{'file_path':'input.txt'},'expires_at':int(time.time())+600})
        if lost_continuation_ack:
            accepted=core.request
            async def lose_dispatch(op,**kw):
                result=await accepted(op,**kw)
                if op=='dispatch_harness' and kw.get('message_id')==fresh.message_id:
                    raise CoreUnavailable('deliberate lost new-continuation acknowledgement')
                return result
            core.request=lose_dispatch
        active_store.append_message(fresh)
        await active_adapter.event_handler.emit_resumed(turn.turn_id,reason="Owner-authorized fresh recovery message")
        await active_adapter.send(restored_turn,fresh,[])
        if lost_continuation_ack:
            async with asyncio.timeout(30):
                while (await core.request('task',id=r.task))['state']!='Blocked':await asyncio.sleep(.1)
            assert (r.home/'model-invocations.jsonl').read_text()==calls
            assert len((r.home/'tool-invocations.jsonl').read_text().splitlines())==1
            assert len([e for e in await core.request('events') if e['kind']=='prompt_dispatch'])==2
            (tmp_path/'lost-continuation-ack.json').write_text(json.dumps({'state':await core.request('task',id=r.task),'no_new_model_or_tool_execution':True,'events':await core.request('events')},indent=2))
            return
        async with asyncio.timeout(45):
            while (await core.request('task',id=r.task))['state']!='Verifying':await asyncio.sleep(.1)
        artifacts=await core.request('artifacts',id=r.task);assert len(artifacts)==1 and artifacts[0]['content']==EXPECTED
        events=await core.request('events');assert len([e for e in events if e['kind']=='prompt_dispatch'])==2
        assert len((r.home/'tool-invocations.jsonl').read_text().splitlines())==1
        state=await core.request('task',id=r.task)
        done=await core.request('verify',id=r.task,version=state['version'],passed=True,evidence='Independent fixed expected bytes and exactly one original read across checkpoint and new Owner-approved continuation')
        assert done['state']=='Done'
        (tmp_path/'explicit-continuation.json').write_text(json.dumps({'actual_lost_receipt':receipt,'budget_before':before,'budget_after':await core.request('model_budget',id=r.budget),'checkpoint':made,'restore':restored,'task':done,'events':events},indent=2))
    finally:
        await active_adapter.close();active_store.close();start_core(r)

@pytest.mark.asyncio
@pytest.mark.parametrize('execution',[{'sandbox':True,'native_file_proxy':True,'test_write':True,'max_restarts':10}],indirect=True)
async def test_native_file_commit_rollback_is_unknown_and_owner_hash_reconciliation_never_rewrites(execution,tmp_path):
    r,store,adapter,core=execution
    db=sqlite3.connect(r.path/'core.db')
    db.execute("CREATE TRIGGER phase3_file_commit_fault BEFORE INSERT ON ledger WHEN NEW.kind='native_file_commit' BEGIN SELECT RAISE(ABORT,'deliberate result commit loss'); END");db.commit()
    try:
        _,_,state=await run_task(execution)
        assert state['state']=='Blocked'
        output=r.workspace/'output.txt';assert output.read_text()=='KANE_PR3B_SAFE_FIXTURE\n'
        identity=(output.stat().st_ino,output.stat().st_mtime_ns,output.read_bytes())
        pending=await core.request('recovery_queue');actions=[a for a in pending['tools'] if a['state']=='Unknown'];assert len(actions)==1
        action=actions[0]['id'];assert not [e for e in await core.request('events') if e['kind']=='native_file_commit']
        before=await core.request('model_budget',id=r.budget)
        resolved=await core.request('reconcile_native_file',id=action)
        repeated=await core.request('reconcile_native_file',id=action)
        assert resolved['state']==repeated['state']=='Confirmed'
        assert (output.stat().st_ino,output.stat().st_mtime_ns,output.read_bytes())==identity
        assert not [a for a in (await core.request('recovery_queue'))['tools'] if a['id']==action]
        assert (await core.request('model_budget',id=r.budget))==before
        assert (await core.request('task',id=r.task))['state']=='Blocked'
        (tmp_path/'native-file-unknown.json').write_text(json.dumps({'action':action,'actual_file_hash':hashlib.sha256(output.read_bytes()).hexdigest(),'resolution':resolved,'budget_unchanged':before,'events':await core.request('events')},indent=2))
    finally:
        db.execute('DROP TRIGGER phase3_file_commit_fault');db.commit();db.close()


def isolated_data(tmp_path):
    from types import SimpleNamespace
    r=SimpleNamespace(path=tmp_path);tmp_path.chmod(0o700)
    subprocess.run([str(BIN),'--db',str(tmp_path/'core.db'),'init'],capture_output=True,check=True)
    data=tmp_path/'data';data.mkdir(mode=0o700);(data/'original.txt').write_text('durable native bytes\n')
    declaration=tmp_path/'spec.json';declaration.write_text(json.dumps({'roots':[{'name':'data','path':str(data),'kind':'directory','max_bytes':8*1048576}]}))
    return r,data,declaration


def test_checkpoint_seal_corruption_unsafe_link_and_no_overwrite(tmp_path):
    r,data,declaration=isolated_data(tmp_path)
    (data/'escape').symlink_to(tmp_path/'core.db')
    refused=command(r,'checkpoint','--spec',declaration,tmp_path/'bad',ok=False);assert refused.returncode!=0
    (data/'escape').unlink()
    healthy=tmp_path/'healthy';command(r,'checkpoint','--spec',declaration,healthy);command(r,'checkpoint-check',healthy)
    assert command(r,'checkpoint','--spec',declaration,healthy,ok=False).returncode!=0
    restored=tmp_path/'restored';command(r,'checkpoint-restore',healthy,restored)
    assert command(r,'checkpoint-restore',healthy,restored,ok=False).returncode!=0
    (healthy/'roots/data/original.txt').write_text('corrupt snapshot bytes\n')
    assert command(r,'checkpoint-check',healthy,ok=False).returncode!=0
    assert command(r,'checkpoint-restore',healthy,tmp_path/'rejected',ok=False).returncode!=0
    assert not (tmp_path/'rejected').exists()
    assert (data/'original.txt').read_text()=='durable native bytes\n'
    (tmp_path/'snapshot-refusals.json').write_text(json.dumps({'escaping_link_refused':True,'corruption_refused':True,'existing_destinations_preserved':True}))


def test_killed_checkpoint_has_no_published_seal_and_existing_checkpoint_survives(tmp_path):
    import signal
    r,data,declaration=isolated_data(tmp_path)
    good=tmp_path/'good';command(r,'checkpoint','--spec',declaration,good)
    for i in range(3000):(data/f'part-{i:04d}').write_bytes(b'x'*1024)
    destination=tmp_path/'interrupted'
    with (tmp_path/'interrupted-checkpoint.log').open('w') as log:
        child=subprocess.Popen([str(BIN),'--db',str(tmp_path/'core.db'),'checkpoint','--spec',str(declaration),str(destination)],stdout=log,stderr=subprocess.STDOUT)
        deadline=time.monotonic()+20;pending=None
        while time.monotonic()<deadline:
            assert child.poll() is None,'checkpoint completed before crash injection; test did not inject a fault'
            paths=[p for p in tmp_path.glob('.pending-*') if (p/'roots/data').is_dir() and any((p/'roots/data').iterdir())]
            if paths:pending=paths[-1];break
            time.sleep(.002)
        assert pending is not None
        os.kill(child.pid,signal.SIGKILL);assert child.wait(timeout=10)==-signal.SIGKILL
    assert not destination.exists() and not (pending/'seal.json').exists()
    assert command(r,'checkpoint-check',pending,ok=False).returncode!=0
    command(r,'checkpoint-check',good)
    db=sqlite3.connect(tmp_path/'core.db');assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok';db.close()
    (tmp_path/'checkpoint-crash.json').write_text(json.dumps({'killed_pid':child.pid,'staging':str(pending),'published':False,'old_checkpoint_valid':True}))


def test_schema_six_upgrade_and_old_binary_refusal_preserve_data(tmp_path):
    old=os.getenv('KANE_PREVIOUS_CORE_BINARY')
    if not old or not Path(old).is_file():pytest.skip('requires explicitly pinned preserved schema-6 binary')
    tmp_path.chmod(0o700);database=tmp_path/'registry.db'
    p=subprocess.run([old,'--db',str(database),'demo'],text=True,capture_output=True,timeout=30);assert p.returncode==0,p.stderr
    db=sqlite3.connect(database);assert db.execute('PRAGMA user_version').fetchone()[0]==6
    before=db.execute('SELECT id,state,budget_id,workspace_id FROM task').fetchall();db.close()
    upgraded=subprocess.run([str(BIN),'--db',str(database),'status'],text=True,capture_output=True,timeout=30);assert upgraded.returncode==0,upgraded.stderr
    db=sqlite3.connect(database);assert db.execute('PRAGMA user_version').fetchone()[0]==9;assert db.execute('SELECT id,state,budget_id,workspace_id FROM task').fetchall()==before;db.close()
    rejected=subprocess.run([old,'--db',str(database),'status'],text=True,capture_output=True,timeout=30);assert rejected.returncode!=0 and 'newer schema' in rejected.stderr
    db=sqlite3.connect(database);assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok';assert db.execute('SELECT id,state,budget_id,workspace_id FROM task').fetchall()==before;db.close()
    (tmp_path/'schema-compatibility.json').write_text(json.dumps({'old_binary_sha256':hashlib.sha256(Path(old).read_bytes()).hexdigest(),'from_schema':6,'to_schema':8,'bindings_preserved':before,'unsafe_old_binary_refused':True}))


def test_explicit_missing_registry_recovery_installs_checked_bytes_without_overwriting_corrupt_state(tmp_path):
    r,data,declaration=isolated_data(tmp_path);checkpoint=tmp_path/'good';made=command(r,'checkpoint','--spec',declaration,checkpoint)
    old=data/'original.txt';old.rename(tmp_path/'archived-original.txt')
    db=tmp_path/'core.db';corrupt=b'not a SQLite database; preserve the damaged original'
    db.write_bytes(corrupt)
    for suffix in ['-wal','-shm']:
        p=Path(str(db)+suffix)
        if p.exists():p.rename(tmp_path/('archived'+suffix))
    assert command(r,'checkpoint-install',checkpoint,'--root','data',ok=False).returncode!=0
    assert not (data/'original.txt').exists()
    installed=command(r,'checkpoint-install',checkpoint,'--root','data','--registry-unavailable')
    assert installed['verified'] and installed['latest_registry_unavailable_mode']
    assert (data/'original.txt').read_text()=='durable native bytes\n' and db.read_bytes()==corrupt
    restored=command(r,'checkpoint-restore',checkpoint,tmp_path/'new-registry')
    check=sqlite3.connect(restored['db']);assert check.execute('PRAGMA integrity_check').fetchone()[0]=='ok';check.close()
    (tmp_path/'missing-registry-recovery.json').write_text(json.dumps({'checkpoint':made['id'],'installed':installed,'corrupt_original_preserved':True,'restored_to_new_path':restored},indent=2))


def test_disaster_mode_cannot_bypass_a_newer_readable_registry_version(tmp_path):
    r,data,declaration=isolated_data(tmp_path);checkpoint=tmp_path/'good';command(r,'checkpoint','--spec',declaration,checkpoint)
    (data/'original.txt').rename(tmp_path/'archived.txt')
    db=sqlite3.connect(tmp_path/'core.db');db.execute('PRAGMA user_version=999');db.commit();db.close()
    denied=command(r,'checkpoint-install',checkpoint,'--root','data','--registry-unavailable',ok=False)
    assert denied.returncode!=0 and 'newer Registry schema' in denied.stderr
    assert not (data/'original.txt').exists()


def test_core_cannot_migrate_before_acquiring_exclusive_maintenance_lock(tmp_path):
    import fcntl
    old=os.environ['KANE_PREVIOUS_CORE_BINARY']
    database=tmp_path/'old.db';endpoint=tmp_path/'owner.sock'
    subprocess.run([old,'--db',str(database),'demo'],check=True,capture_output=True,text=True)
    db=sqlite3.connect(database);assert db.execute('PRAGMA user_version').fetchone()[0]==6;db.close()
    with Path(str(database)+'.service-lock').open('wb') as lock:
        os.chmod(lock.name,0o600);fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        rejected=subprocess.run([str(BIN),'--db',str(database),'--socket',str(endpoint),'serve'],capture_output=True,text=True,timeout=30)
        assert rejected.returncode!=0 and 'another core service owns' in rejected.stderr
        db=sqlite3.connect(database);assert db.execute('PRAGMA user_version').fetchone()[0]==6;db.close()
        assert not endpoint.exists()
    log=(tmp_path/'post-maintenance-core.log').open('w')
    process=subprocess.Popen([str(BIN),'--db',str(database),'--socket',str(endpoint),'serve'],stdout=log,stderr=subprocess.STDOUT)
    try:
        wait_for(endpoint.exists,20)
        db=sqlite3.connect(database);assert db.execute('PRAGMA user_version').fetchone()[0]==9;db.close()
    finally:
        process.terminate();assert process.wait(timeout=10)==-15;log.close()
    (tmp_path/'maintenance-lock.json').write_text(json.dumps({'before_schema':6,'while_locked_schema':6,'after_exclusive_acquire_schema':8,'no_socket_before_lock':True,'migration_serialized':True},indent=2))
