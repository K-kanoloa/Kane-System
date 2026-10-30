"""Phase4 local Linux integration. ASGI tests do not prove TLS/device compatibility."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
import signal
import sqlite3
import time
from pathlib import Path

import httpx
import pytest
from app.control.app import create_control_app, SESSION, DEVICE
from test_system_execution import execution
from test_supervised_runtime import wait_for

pytestmark=pytest.mark.skipif(os.getenv('KANE_PHASE4_TEST')!='1',reason='opt-in Phase4 real Core/Kanaloa control tests')
ORIGIN='https://localhost:9443'
HEADERS={'Origin':ORIGIN,'X-Kane-Intent':'owner-action'}
PROFILE={'sandbox':True,'native_file_proxy':True,'native_read_fencing':True,'phase4_document':True,'max_restarts':10,
    'task_goal':'Review the native document and incorporate audited Owner edits',
    'task_acceptance':['Original restricted tool reads the actual native document; Agent does not overwrite it',
        'Explicit Owner edits retain version/hash and invalidate prior verification',
        'After handoff the original tool reads the new bytes before a new Artifact',
        'Independent verifier checks current file, Artifact/source hashes and provenance before Done']}

@asynccontextmanager
async def control(execution,monkeypatch):
    r,store,adapter,core=execution
    monkeypatch.setenv('KANE_CONTROL_ORIGIN',ORIGIN)
    code=r.call({'op':'control_auth','action':'init','body':{'origin':ORIGIN}})['pairing_code']
    app=create_control_app();app.state.store=store;app.state.coordinator=adapter.event_handler;app.state.control_runtime_specs={r.agent:str(r.spec)}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=ORIGIN,headers=HEADERS) as client:
            paired=await client.post('/control-api/auth/pair',json={'pairing_code':code,'label':'Linux ASGI test client'})
            assert paired.status_code==200,paired.text
            assert not any(k.endswith('_token') for k in paired.json())
            assert all('Secure' in v and 'HttpOnly' in v and 'SameSite=strict' in v for v in paired.headers.get_list('set-cookie'))
            yield client,app

async def state(client):
    p=await client.get('/control-api/snapshot');assert p.status_code==200,p.text
    return p.json()
async def until(client,predicate,seconds=45):
    async with asyncio.timeout(seconds):
        while True:
            snapshot=await state(client)
            if predicate(snapshot):return snapshot
            await asyncio.sleep(.05)

def workspace(snapshot):return snapshot['workspaces'][0]['workspace']
def version(snapshot):
    w=workspace(snapshot);return {'epoch':w['lease_epoch'],'version':w['version']}
async def dispatch(client,r):
    p=await client.post(f'/control-api/tasks/{r.task}/dispatch',json={'content':'Read input.txt through the original read tool and deliver the actual native document.'})
    assert p.status_code==200,p.text
    return await until(client,lambda s:s['tasks'][0]['state'] in {'Verifying','Blocked','Failed'})

@pytest.mark.asyncio
@pytest.mark.parametrize('execution',[PROFILE],indirect=True)
async def test_real_native_owner_edit_handoff_independent_verification(execution,monkeypatch,tmp_path):
    r,store,adapter,core=execution
    before=r.call({'op':'runtime_status','id':r.agent})['agent']
    async with control(execution,monkeypatch) as (client,app):
        first=await dispatch(client,r)
        assert first['tasks'][0]['state']=='Verifying',first
        assert 'KANE_PR2_FIXTURE' in first['artifacts'][0]['content']
        initial_pid=r.status()['pid']
        started=time.monotonic()
        taken=await client.post(f'/control-api/tasks/{r.task}/takeover',json=version(first));takeover_seconds=time.monotonic()-started;assert taken.status_code==200,taken.text
        assert taken.json()['phase']=='Human'
        assert not Path(f'/proc/{initial_pid}').exists()  # Real guardian/process stopped, not a UI flag.
        current=await state(client);assert current['workspaces'][0]['own_lease']
        read=await client.get(f'/control-api/tasks/{r.task}/files/input.txt');assert read.status_code==200
        body={**version(current),'request_id':'human-edit-1','path':'input.txt','content':'KANE_HUMAN_EDIT_ACTUAL_BYTES\n','base_hash':read.json()['hash']}
        saved=await client.post(f'/control-api/tasks/{r.task}/save',json=body);assert saved.status_code==200,saved.text
        assert (r.workspace/'input.txt').read_text()==body['content']
        duplicate=await client.post(f'/control-api/tasks/{r.task}/save',json=body);assert duplicate.status_code==200 and duplicate.json()==saved.json()
        stale=await client.post(f'/control-api/tasks/{r.task}/save',json={**body,'request_id':'stale-edit'});assert stale.status_code==409
        current=await state(client)
        download=await client.get(f'/control-api/tasks/{r.task}/download/input.txt');assert download.text==body['content']
        # Exact saved file is retained; original Artifact is explicitly invalidated.
        assert current['artifacts'][0]['verification_state']=='unverified'
        p=await client.post(f'/control-api/tasks/{r.task}/handoff',json={**version(current),'read_path':'input.txt','message_id':'human-handoff-1','content':'Read input.txt again with the original read tool. Use the new human-edited bytes in a new Markdown result.'})
        assert p.status_code==200,p.text
        final=await until(client,lambda s:s['tasks'][0]['state'] in {'Verifying','Failed'} or s['tasks'][0].get('block_reason')=='reconciliation')
        assert final['tasks'][0]['state']=='Verifying',final
        assert len(final['artifacts'])==2
        new=[a for a in final['artifacts'] if a['verification_state']=='pending'][0]
        assert 'KANE_HUMAN_EDIT_ACTUAL_BYTES' in new['content']
        expected=hashlib.sha256(body['content'].encode()).hexdigest()
        assert new['workspace_source']['files']['input.txt']['hash']==expected
        assert new['workspace_source']['workspace_version']==workspace(current)['version']
        events=await core.request('events')
        assert len([e for e in events if e['kind']=='human_edit'])==1
        assert len([e for e in events if e['kind']=='handoff_read_ack'])==1
        after=(await core.request('runtime_status',id=r.agent))['agent']
        for key in ['id','task_id','workspace_id','budget_id','model_id']:assert after[key]==before[key]
        assert len([e for e in events if e['kind']=='prompt_dispatch'])==2
        # Independent test reads actual native bytes and checks new source/content hashes.
        assert hashlib.sha256((r.workspace/'input.txt').read_bytes()).hexdigest()==expected
        assert hashlib.sha256(new['content'].encode()).hexdigest()==new['content_hash']
        done=await client.post(f'/control-api/tasks/{r.task}/verify',json={'version':final['tasks'][0]['version'],'passed':True,'evidence':'Independent Linux test verified actual edited bytes, new original-tool read ACK, Artifact source/version/content SHA-256; deterministic model only'})
        assert done.status_code==200 and done.json()['state']=='Done',done.text
        (tmp_path/'handoff-proof.json').write_text(json.dumps({'before':before,'after':after,'initial':first,'after_edit':current,'final':await state(client),'events':await core.request('events'),'native_sha256':expected,'transport':'ASGI HTTPS scheme; actual TLS tested separately','takeover_request_until_confirmed_physical_stop_and_Human_lease_seconds':takeover_seconds,'latency_N':1,'network':'in-process ASGI, actual Docker stop/Core IPC; no remote network claim'},indent=2))

@pytest.mark.asyncio
@pytest.mark.parametrize('execution',[PROFILE],indirect=True)
async def test_origin_pairing_revocation_legacy_route_and_fail_closed(execution,monkeypatch,tmp_path):
    r,_,_,core=execution
    async with control(execution,monkeypatch) as (client,app):
        assert (await client.get('/api/v1/system/tasks')).status_code==404
        for path in ['/control/%252e%252e/api/proxy/system','/_next/static/%252e%252e/%252e%252e/api/proxy/system']:
            assert (await client.get(path)).status_code==404
        assert (await client.post('/control-api/auth/pair-code',json={},headers={'Origin':'https://evil.invalid'})).status_code==403
        assert (await client.post('/control-api/auth/pair-code',json={},headers={'X-Kane-Intent':''})).status_code==403
        assert (await client.get('/control-api/snapshot',headers={'Host':'evil.invalid'})).status_code==421
        assert (await client.get('/control-api/snapshot',headers={'X-Forwarded-Proto':'https'})).status_code==421
        assert (await client.get('http://localhost:9443/control-api/health')).status_code==426
        p=await client.post('/control-api/auth/pair-code',json={});assert p.status_code==200
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=ORIGIN,headers=HEADERS) as second:
            body={'pairing_code':p.json()['pairing_code'],'label':'Second Linux test client'}
            paired=await second.post('/control-api/auth/pair',json=body);assert paired.status_code==200
            assert (await second.post('/control-api/auth/pair',json=body)).status_code==403
            assert (await second.get('/control-api/snapshot')).status_code==200
            device=paired.json()['device_id']
            assert (await client.post(f'/control-api/devices/{device}/revoke',json={})).status_code==200
            assert (await second.get('/control-api/snapshot')).status_code==401
        async def chunks():
            for _ in range(32):yield b'x'*65536
        assert (await client.post('/control-api/auth/pair',content=chunks(),headers={'Content-Type':'application/json'})).status_code==413
        # Public finish cannot turn invented credential metadata into a verified Passkey.
        options=await client.post('/control-api/auth/passkey/register/options',json={});assert options.status_code==200
        forged=await client.post('/control-api/auth/passkey/register/finish',json={'challenge_id':options.json()['challenge_id'],'credential':{'id':'forged','type':'public-key','response':{}}});assert forged.status_code==403
        r.core.kill();r.core.wait(timeout=5)
        assert (await client.get('/control-api/snapshot')).status_code==503
        p=await client.post(f'/control-api/tasks/{r.task}/takeover',json={'epoch':1,'version':0});assert p.status_code==503
        # Restore the same latest Core WAL for fixture evidence/physical cleanup; no Task replay.
        from test_phase3_recovery import start_core
        start_core(r)
        assert not any(e['kind']=='prompt_dispatch' for e in await core.request('events'))

@pytest.mark.asyncio
@pytest.mark.parametrize('execution',[{**PROFILE,'require_approval':True,'reference_authorization_s':1800}],indirect=True)
async def test_actual_https_wss_browser_passkey_and_native_flow(execution,monkeypatch,tmp_path):
    """Real Linux browser with virtual WebAuthn; no cross-platform/device claim."""
    import socket
    import subprocess
    import uvicorn
    from test_supervised_runtime import ROOT
    # Fail normally before creating Uvicorn tasks if a concurrent reference owns
    # these ports; SystemExit from an async startup can poison pytest teardown.
    for port in [9443,3144]:
        with socket.socket() as probe:probe.bind(('127.0.0.1',port))
    r,store,adapter,core=execution
    monkeypatch.setenv('KANE_CONTROL_ORIGIN',ORIGIN)
    monkeypatch.setenv('KANE_CONTROL_NEXT_URL','http://127.0.0.1:3144')
    code=r.call({'op':'control_auth','action':'init','body':{'origin':ORIGIN}})['pairing_code']
    app=create_control_app();app.state.store=store;app.state.coordinator=adapter.event_handler;app.state.control_runtime_specs={r.agent:str(r.spec)}
    private=r.path/'tls';private.mkdir(mode=0o700)
    if os.getenv('KANE_CONTROL_REFERENCE_TLS_DIR') and os.getenv('KANE_CONTROL_REMOTE_EXCHANGE'):
        # Retry the same authorized private test certificate; normal local tests
        # still generate fresh trust. No verification flag is changed.
        import shutil
        reference=Path(os.environ['KANE_CONTROL_REFERENCE_TLS_DIR'])
        for name in ['cert.pem','key.pem']:shutil.copy2(reference/name,private/name)
    else:
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(private/'key.pem'),'-out',str(private/'cert.pem'),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    (private/'key.pem').chmod(0o600)
    log=(tmp_path/'next.log').open('w')
    web=subprocess.Popen(['node',str(ROOT/'node_modules/next/dist/bin/next'),'start','--hostname','127.0.0.1','--port','3144'],cwd=ROOT/'apps/web',stdout=log,stderr=log)
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=9443,ssl_certfile=str(private/'cert.pem'),ssl_keyfile=str(private/'key.pem'),proxy_headers=False,access_log=False,log_level='warning',timeout_keep_alive=90,ws_ping_timeout=120))
    serve=asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(30):
            while not server.started:await asyncio.sleep(.05)
            async with httpx.AsyncClient(trust_env=False) as client:
                while True:
                    try:
                        if (await client.get('http://127.0.0.1:3144/control')).status_code==200:break
                    except httpx.HTTPError:pass
                    await asyncio.sleep(.1)
        # TLS certificate is validated by HTTP client against the ephemeral local cert.
        import ssl
        context=ssl.create_default_context(cafile=str(private/'cert.pem'))
        async with httpx.AsyncClient(verify=context,trust_env=False) as secure:
            assert (await secure.get(ORIGIN+'/control-api/health')).status_code==200
        expiry_signal=tmp_path/'lease-expiry-signal'
        async def inject_expiry():
            while not expiry_signal.exists():await asyncio.sleep(.05)
            db=sqlite3.connect(r.path/'core.db')
            db.execute('UPDATE workspace SET expires_at=? WHERE id=?',(int(time.time())-1,r.ws));db.commit();db.close()
            (tmp_path/'lease-expiry-injection.json').write_text(json.dumps({'fault':'deadline set in past by private test runner','workspace':r.ws,'public_fault_endpoint':False,'Task_success_fabricated':False}))
        expiry_task=asyncio.create_task(inject_expiry())
        env={**os.environ,'KANE_CONTROL_ORIGIN':ORIGIN,'KANE_CONTROL_TEST_PAIRING':code,'KANE_CONTROL_BROWSER_EVIDENCE':str(tmp_path/'browser'),'PLAYWRIGHT_BROWSERS_PATH':os.getenv('PLAYWRIGHT_BROWSERS_PATH','/dev/shm/kane-phase2-cache/playwright')}
        if os.getenv('KANE_CONTROL_REMOTE_EXCHANGE'):
            exchange=Path(os.environ['KANE_CONTROL_REMOTE_EXCHANGE']);exchange.mkdir(mode=0o700,exist_ok=True)
            ready={'origin':ORIGIN,'pairing_code':code,'certificate':str(private/'cert.pem'),'browser_script':str(ROOT/'bench/phase4/browser.mjs'),'test_evidence':str(tmp_path)}
            (exchange/'ready.json').write_text(json.dumps(ready));(exchange/'ready.json').chmod(0o600)
            async with asyncio.timeout(1800):
                while not (exchange/'result.json').exists():await asyncio.sleep(.2)
            result=json.loads((exchange/'result.json').read_text());assert result.get('passed'),result
        else:
            # Only this new temporary browser profile trusts the ephemeral test CA.
            # No certificate-error flags, context overrides or system trust edits.
            home=private/'browser-home';nss=home/'.pki/nssdb';nss.mkdir(parents=True)
            subprocess.run(['certutil','-N','--empty-password','-d','sql:'+str(nss)],check=True)
            subprocess.run(['certutil','-A','-d','sql:'+str(nss),'-n','Kane temporary test CA','-t','C,,','-i',str(private/'cert.pem')],check=True)
            env['KANE_CONTROL_TLS_HOME']=str(home)
            env['KANE_CONTROL_TEST_EXPIRY_SIGNAL']=str(expiry_signal)
            proc=await asyncio.create_subprocess_exec('node',str(ROOT/'bench/phase4/browser.mjs'),env=env,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT)
            output,_=await asyncio.wait_for(proc.communicate(),300);(tmp_path/'browser.log').write_bytes(output)
            assert proc.returncode==0,output.decode()
        expiry_task.cancel()
        events=await core.request('events');assert len([e for e in events if e['kind']=='prompt_dispatch'])==2
        assert len([e for e in events if e['kind']=='handoff_read_ack'])==1
        (tmp_path/'browser-core-evidence.json').write_text(json.dumps({'events':events,'task':await core.request('task',id=r.task),'artifacts':await core.request('artifacts',id=r.task),'budget':await core.request('model_budget',id=r.budget),'native_file':(r.workspace/'input.txt').read_text(),'kernel_scope':'actual Docker cgroups readonly-workspace reference','external_devices':'Authorized external browser evidence is recorded separately' if os.getenv('KANE_CONTROL_REMOTE_EXCHANGE') else 'Linux actual browser only; other platforms not inferred'},indent=2))
    finally:
        server.should_exit=True
        await asyncio.wait_for(serve,30)
        web.terminate();await asyncio.to_thread(web.wait,10);log.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('execution',[PROFILE],indirect=True)
@pytest.mark.parametrize('fault',['lost_reply','publish_before_commit','before_publish'])
async def test_native_edit_faults_core_restart_and_explicit_reconciliation(execution,monkeypatch,tmp_path,fault):
    from app.system.client import CoreUnavailable
    from test_phase3_recovery import start_core
    r,store,adapter,core=execution
    async with control(execution,monkeypatch) as (client,app):
        first=await dispatch(client,r);assert first['tasks'][0]['state']=='Verifying'
        take=await client.post(f'/control-api/tasks/{r.task}/takeover',json=version(first));assert take.status_code==200,take.text
        current=await state(client);read=await client.get(f'/control-api/tasks/{r.task}/files/input.txt')
        body={**version(current),'request_id':'fault-edit','path':'input.txt','content':'KANE_FAULT_NATIVE_BYTES\n','base_hash':read.json()['hash']}
        original=app.state.control_core.request
        if fault=='lost_reply':
            async def lose(op,**body):
                result=await original(op,**body)
                if op=='control_edit':raise CoreUnavailable('injected lost committed response')
                return result
            app.state.control_core.request=lose
        else:
            db=sqlite3.connect(r.path/'core.db')
            # Real filesystem publication then SQL rollback, or SQL failure before publication.
            statement="CREATE TRIGGER phase4_fault BEFORE INSERT ON ledger WHEN NEW.kind='human_edit' BEGIN SELECT RAISE(ABORT,'phase4 deliberate post-publication failure');END;" if fault=='publish_before_commit' else "CREATE TRIGGER phase4_fault BEFORE UPDATE ON workspace_file BEGIN SELECT RAISE(ABORT,'phase4 deliberate pre-publication failure');END;"
            db.execute(statement);db.commit();db.close()
        saved=await client.post(f'/control-api/tasks/{r.task}/save',json=body);assert saved.status_code==503,saved.text
        app.state.control_core.request=original
        expected=body['content'] if fault!='before_publish' else 'KANE_PR2_FIXTURE\n'
        assert (r.workspace/'input.txt').read_text()==expected
        # SIGKILL the actual Core and recover latest WAL; Agent remains physically stopped.
        r.core.kill();r.core.wait(timeout=5);start_core(r)
        if fault!='lost_reply':
            db=sqlite3.connect(r.path/'core.db');db.execute('DROP TRIGGER phase4_fault');db.commit();db.close()
        status=await client.get('/control-api/edits/fault-edit');assert status.status_code==200,status.text
        if fault=='lost_reply':
            assert status.json()['state']=='Committed'
            duplicate=await client.post(f'/control-api/tasks/{r.task}/save',json=body);assert duplicate.status_code==200
        else:
            assert status.json()['state']=='Unknown'
            assert (await state(client))['workspaces'][0]['unresolved']
            retry=await client.post(f'/control-api/tasks/{r.task}/save',json=body);assert retry.status_code==409
            verify=await client.post(f'/control-api/tasks/{r.task}/verify',json={'version':(await state(client))['tasks'][0]['version'],'passed':True,'evidence':'must not hide Unknown'});assert verify.status_code in (400,403,409)
            outcome='Confirmed' if fault=='publish_before_commit' else 'NotExecuted'
            reconciled=await client.post('/control-api/edits/fault-edit/reconcile',json={'outcome':outcome});assert reconciled.status_code==200,reconciled.text
            assert not (await state(client))['workspaces'][0]['unresolved']
            again=await client.post('/control-api/edits/fault-edit/reconcile',json={'outcome':outcome});assert again.status_code==200
            if outcome=='Confirmed':assert (await client.post(f'/control-api/tasks/{r.task}/save',json=body)).status_code==200
        assert (r.workspace/'input.txt').read_text()==expected
        assert len([e for e in await core.request('events') if e['kind']=='prompt_dispatch'])==1
        assert (await core.request('model_budget',id=r.budget))['used_tokens']==42
        events=await core.request('events')
        assert len([e for e in events if e['kind']=='human_edit'])==(0 if fault=='before_publish' else 1)
        (tmp_path/'fault-proof.json').write_text(json.dumps({'fault':fault,'save_http_status':saved.status_code,'final':await state(client),'events':events,'actual_native_hash':hashlib.sha256((r.workspace/'input.txt').read_bytes()).hexdigest(),'core_SIGKILL':True,'automatic_write_replay':False},indent=2))

@pytest.mark.asyncio
@pytest.mark.parametrize('execution',[PROFILE],indirect=True)
async def test_concurrent_devices_expiry_symlink_fifo_and_stale_agent_fences(execution,monkeypatch,tmp_path):
    r,store,adapter,core=execution
    async with control(execution,monkeypatch) as (client,app):
        first=await dispatch(client,r);assert first['tasks'][0]['state']=='Verifying'
        before=(r.home/'model-invocations.jsonl').read_text()
        granted=await client.post('/control-api/auth/pair-code',json={})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=ORIGIN,headers=HEADERS) as second:
            p=await second.post('/control-api/auth/pair',json={'pairing_code':granted.json()['pairing_code'],'label':'Concurrent Linux Owner'})
            assert p.status_code==200
            taken=await client.post(f'/control-api/tasks/{r.task}/takeover',json=version(first));assert taken.status_code==200
            current=await state(client);v=version(current)
            steal=await second.post(f'/control-api/tasks/{r.task}/takeover',json=v);assert steal.status_code==403
            read=(await client.get(f'/control-api/tasks/{r.task}/files/input.txt')).json()
            bodies=[{**v,'request_id':f'competing-edit-{i}','path':'input.txt','content':f'WINNER_{i}\n','base_hash':read['hash']} for i in range(2)]
            results=await asyncio.gather(*(client.post(f'/control-api/tasks/{r.task}/save',json=b) for b in bodies))
            assert sorted(p.status_code for p in results)==[200,409]
            assert workspace(await state(client))['version']==1
            # Directory traversal, symlink, open special file cannot escape or block authority.
            (r.workspace/'escape.txt').symlink_to('/etc/passwd')
            try:assert (await client.get(f'/control-api/tasks/{r.task}/files/escape.txt')).status_code in (400,403,503)
            finally:(r.workspace/'escape.txt').unlink()
            os.mkfifo(r.workspace/'pipe.txt')
            try:
                response=await asyncio.wait_for(client.get(f'/control-api/tasks/{r.task}/files/pipe.txt'),3)
                assert response.status_code==403
            finally:(r.workspace/'pipe.txt').unlink()
            db=sqlite3.connect(r.path/'core.db');db.execute('UPDATE workspace SET expires_at=? WHERE id=?',(int(time.time())-1,r.ws));db.commit();db.close()
            stale=await client.post(f'/control-api/tasks/{r.task}/save',json={**bodies[0],**version(await state(client)),'request_id':'expired-lease'});assert stale.status_code==409
            # The same still-authorized Owner must reacquire, never extend or
            # reuse an expired epoch. Kernel process remains stopped.
            expired=await state(client)
            reacquired=await client.post(f'/control-api/tasks/{r.task}/takeover',json=version(expired));assert reacquired.status_code==200,reacquired.text
            assert reacquired.json()['workspace']['lease_epoch']>v['epoch']
            stale_again=await client.post(f'/control-api/tasks/{r.task}/save',json={**bodies[0],'request_id':'pre-reacquire-epoch'});assert stale_again.status_code==409
            db=sqlite3.connect(r.path/'core.db');db.execute('UPDATE workspace SET expires_at=? WHERE id=?',(int(time.time())-1,r.ws));db.commit();db.close()
            # Explicit second-device takeover increments epoch; old human cannot write.
            current=await state(client)
            take=await second.post(f'/control-api/tasks/{r.task}/takeover',json=version(current));assert take.status_code==200,take.text
            assert take.json()['workspace']['lease_epoch']>v['epoch']
            old=await client.post(f'/control-api/tasks/{r.task}/save',json={**bodies[0],'request_id':'old-human'});assert old.status_code==409
            assert (r.home/'model-invocations.jsonl').read_text()==before
            assert len([e for e in await core.request('events') if e['kind']=='prompt_dispatch'])==1
            (tmp_path/'fencing-proof.json').write_text(json.dumps({'competing_HTTP_status':[p.status_code for p in results],'final':await state(second),'events':await core.request('events'),'extra_model_calls':0},indent=2))

from test_phase2_models import models
@pytest.mark.asyncio
@pytest.mark.parametrize('models',[{'real':True,'output_tokens':512,'read_task':True,'control_files':True,'model_file':os.getenv('KANE_PHASE2_TOOL_MODEL_FILE','/tmp/kane-phase2-final-deps/Qwen3-0.6B-Q8_0.gguf'),'backend_args':['--chat-template-kwargs','{"enable_thinking":false}','--temp','0','--seed','0']}],indirect=True)
async def test_real_local_model_native_human_handoff(models,monkeypatch,tmp_path):
    item=models['items'][0];r,store,adapter,core=item
    marker='KANE_REAL_HUMAN_P4_9T2\n'
    async with control(item,monkeypatch) as (client,app):
        first=await client.post(f'/control-api/tasks/{r.task}/dispatch',json={'content':'Use the read tool to read input.txt with arguments {"file_path":"input.txt"}. Then return only the exact text you read. Do not guess the file content. Do not use any other tool.'});assert first.status_code==200,first.text
        current=await until(client,lambda s:s['tasks'][0]['state'] in {'Verifying','Blocked','Failed'},150)
        assert current['tasks'][0]['state']=='Verifying',current
        assert 'KANE_PR2_FIXTURE' in current['artifacts'][0]['content']
        take=await client.post(f'/control-api/tasks/{r.task}/takeover',json=version(current));assert take.status_code==200,take.text
        current=await state(client);read=(await client.get(f'/control-api/tasks/{r.task}/files/input.txt')).json()
        saved=await client.post(f'/control-api/tasks/{r.task}/save',json={**version(current),'request_id':'real-human-edit','path':'input.txt','content':marker,'base_hash':read['hash']});assert saved.status_code==200,saved.text
        current=await state(client)
        handed=await client.post(f'/control-api/tasks/{r.task}/handoff',json={**version(current),'read_path':'input.txt','message_id':'real-human-continuation','content':'Read input.txt again NOW. The file was changed outside your session. You MUST invoke the read tool with {"file_path":"input.txt"} before answering. Previous read output is stale and cannot be reused. After the new tool result, return its text.'});assert handed.status_code==200,handed.text
        final=await until(client,lambda s:s['tasks'][0]['state'] in {'Verifying','Failed'} or s['tasks'][0].get('block_reason')=='reconciliation',150)
        (tmp_path/'real-model-attempt.json').write_text(json.dumps({'state':final,'events':await core.request('events'),'budget':await core.request('model_budget',id=r.budget)},indent=2))
        assert final['tasks'][0]['state']=='Verifying',final
        new=[a for a in final['artifacts'] if a['verification_state']=='pending'];assert len(new)==1
        assert marker.strip() in new[0]['content']
        assert new[0]['workspace_source']['files']['input.txt']['hash']==hashlib.sha256(marker.encode()).hexdigest()
        assert (r.workspace/'input.txt').read_text()==marker
        budget=await core.request('model_budget',id=r.budget);assert budget['reserved_tokens']==0 and budget['used_tokens']>0
        done=await client.post(f'/control-api/tasks/{r.task}/verify',json={'version':final['tasks'][0]['version'],'passed':True,'evidence':'Independent actual CPU model test: secret fixture marker absent from prompt, real native file bytes/hash and original post-handoff read/provenance checked; usage actual backend not Context; no paid invoice claim'})
        assert done.status_code==200 and done.json()['state']=='Done',done.text
        (tmp_path/'real-model-handoff.json').write_text(json.dumps({'task':done.json(),'artifact':new[0],'budget':budget,'events':await core.request('events'),'paid_invoice':'UNKNOWN; local unbilled CPU model','ModelInstance_kernel_enforcement':'existing diagnostic_unmanaged host test; not a new cgroups enforcement acceptance','real_model':True},indent=2))

@pytest.mark.asyncio
@pytest.mark.parametrize('execution',[{'sandbox':True,'native_file_proxy':True,'test_write':True,'max_restarts':10}],indirect=True)
async def test_takeover_waits_for_real_inflight_original_write_and_fences_late_commit(execution,monkeypatch,tmp_path):
    r,store,adapter,core=execution;original=core.request;at_commit=asyncio.Event();release=asyncio.Event();captured=[]
    async def held(op,**body):
        if op=='tool_file':captured.append(body);at_commit.set();await release.wait()
        return await original(op,**body)
    core.request=held  # Hold the actual original write tool's already claimed publication.
    try:
        async with control(execution,monkeypatch) as (client,app):
            app.state.control_core.request=held
            start=await client.post(f'/control-api/tasks/{r.task}/dispatch',json={'content':'Execute the restricted original write tool in this isolated fixture.'});assert start.status_code==200
            await asyncio.wait_for(at_commit.wait(),20)
            current=await state(client);assert current['tasks'][0]['state']=='Running'
            take=await client.post(f'/control-api/tasks/{r.task}/takeover',json=version(current));assert take.status_code==409
            assert take.json()['phase']=='TakingOver' and take.json()['unresolved']
            assert not (await state(client))['workspaces'][0]['own_lease']
            assert not (r.workspace/'output.txt').exists()
            release.set()
            async with asyncio.timeout(15):
                while True:
                    tools=(await original('recovery_queue'))['tools']
                    if any(a['state']=='Unknown' for a in tools):break
                    await asyncio.sleep(.025)
            assert not (r.workspace/'output.txt').exists()
            action=next(a for a in tools if a['state']=='Unknown')
            before=await original('model_budget',id=r.budget)
            await original('reconcile_action',id=action['id'],executed=False,evidence='Independent test: original write held before native_file_commit; Core denied after durable takeover barrier; no output file under actual readonly container mount. No replay.')
            current=await state(client)
            taken=await client.post(f'/control-api/tasks/{r.task}/takeover',json=version(current));assert taken.status_code==200,taken.text
            assert taken.json()['phase']=='Human'
            current=await state(client)
            saved=await client.post(f'/control-api/tasks/{r.task}/save',json={**version(current),'request_id':'human-after-old-write','path':'output.txt','content':'HUMAN_OWNS_NATIVE_VERSION\n','base_hash':None});assert saved.status_code==200,saved.text
            from app.system.client import CoreRejected
            with pytest.raises(CoreRejected):await original('tool_file',**captured[0])
            assert (r.workspace/'output.txt').read_text()=='HUMAN_OWNS_NATIVE_VERSION\n'
            assert (await original('model_budget',id=r.budget))==before
            assert len([e for e in await original('events') if e['kind']=='prompt_dispatch'])==1
            (tmp_path/'inflight-fencing.json').write_text(json.dumps({'events':await original('events'),'original_write_action':action['id'],'final':await state(client),'original_write_executed':False,'late_old_commit_rejected':True,'unknown_required_explicit_owner_reconciliation':True},indent=2))
    finally:release.set();core.request=original

@pytest.mark.asyncio
@pytest.mark.parametrize('execution',[{**PROFILE,'require_approval':True}],indirect=True)
@pytest.mark.parametrize('decision',['allow-once','reject-once'])
async def test_owner_approval_uses_original_pending_acp_request(execution,monkeypatch,tmp_path,decision):
    r,store,adapter,core=execution
    async with control(execution,monkeypatch) as (client,app):
        submitted=await client.post(f'/control-api/tasks/{r.task}/dispatch',json={'content':'Read the native input document using the restricted original read tool.'});assert submitted.status_code==200
        pending=await until(client,lambda s:bool(s['permissions']))
        permission=pending['permissions'][0]
        assert (await client.post(f'/control-api/tasks/{r.task}/approval',json={'request_id':'forged-pending','decision':decision})).status_code==409
        approved=await client.post(f'/control-api/tasks/{r.task}/approval',json={'request_id':permission['request_id'],'decision':decision});assert approved.status_code==200,approved.text
        final=await until(client,lambda s:s['tasks'][0]['state'] in {'Verifying','Blocked','Failed'})
        assert final['tasks'][0]['state']==('Verifying' if decision=='allow-once' else 'Blocked')
        again=await client.post(f'/control-api/tasks/{r.task}/approval',json={'request_id':permission['request_id'],'decision':decision});assert again.status_code==409
        events=await core.request('events');decisions=[e for e in events if e['kind']=='owner_approval'];assert len(decisions)==1
        assert decisions[0]['payload']['request_id']==permission['request_id']
        (tmp_path/'web-approval.json').write_text(json.dumps({'decision':decision,'permission':permission,'final':final,'events':events,'original_ACP':True},indent=2))

def test_schema7_migrates_and_old_reader_cannot_reopen_schema9(tmp_path):
    import subprocess
    from test_supervised_runtime import ROOT,BIN
    old=Path(os.getenv('KANE_PHASE3_SCHEMA7_BINARY',str(ROOT/'docs/reports/2026-10-23-phase3/validation/kanesys')))
    assert old.is_file(),'Historical Phase3 binary is a required immutable input for this upgrade regression'
    db=tmp_path/'core.db'
    before=subprocess.run([str(old),'--db',str(db),'init'],capture_output=True,text=True);assert before.returncode==0,before.stderr
    sql=sqlite3.connect(db);assert sql.execute('PRAGMA user_version').fetchone()[0]==7;sql.close()
    upgraded=subprocess.run([str(BIN),'--db',str(db),'host-request'],input=json.dumps({'op':'health'}),capture_output=True,text=True);assert upgraded.returncode==0,upgraded.stderr
    assert json.loads(upgraded.stdout)['schema']==9
    sql=sqlite3.connect(db);versions=sql.execute('SELECT version FROM schema_version ORDER BY version').fetchall();sql.close()
    rejected=subprocess.run([str(old),'--db',str(db),'host-request'],input=json.dumps({'op':'health'}),capture_output=True,text=True);assert rejected.returncode!=0
    sql=sqlite3.connect(db);assert sql.execute('SELECT version FROM schema_version ORDER BY version').fetchall()==versions;assert sql.execute('PRAGMA user_version').fetchone()[0]==9;sql.close()
    (tmp_path/'schema7-to-9.json').write_text(json.dumps({'before_binary_sha256':hashlib.sha256(old.read_bytes()).hexdigest(),'current_binary_sha256':hashlib.sha256(BIN.read_bytes()).hexdigest(),'old_reader_exit_code':rejected.returncode,'old_reader_stderr':rejected.stderr,'migration_versions':versions,'new_health':json.loads(upgraded.stdout)},indent=2))
