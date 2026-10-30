"""Reuse actual Phase4 native edit / original DSH Session handoff inside a Task DAG."""
import hashlib,json,os,time
import pytest
from test_system_execution import execution
from test_phase4_control import PROFILE,control,state
from test_phase5_collaboration import FINGERPRINT
pytestmark=pytest.mark.skipif(os.getenv('KANE_PHASE5_TEST')!='1',reason='opt-in real collaboration Human Takeover')
CONFIG=PROFILE|{'shared_fifo':True,'phase2_model_spec':{'provider':'kane-fixture','model':'deterministic-v1','cloud':False,'capabilities':['read-public-text'],'scheduling':{'mode':'mock_fifo','backend_kind':'mock'},'budget_profile':{'provider':'kane-fixture','model':'deterministic-v1','upper_bound_tokens':32,'bound_evidence':'fixed bounded document fixture'}}}

@pytest.mark.asyncio
@pytest.mark.parametrize('execution',[CONFIG],indirect=True)
async def test_collaboration_fenced_during_real_native_owner_takeover(execution,monkeypatch,tmp_path):
    import asyncio
    from app.system.client import CoreRejected
    r,store,adapter,core=execution
    ws=r.call({'op':'create_workspace','kind':'document'})['id'];budget=r.call({'op':'create_budget','tokens':10000})['id']
    child=r.call({'op':'task','id':r.task})
    root=r.call({'op':'create_task','spec':child['spec']|{'workspace_id':ws,'budget_id':budget,'goal':'Aggregate independently confirmed human-edited child document'}})['id']
    parent=r.call({'op':'register_agent','role':'aggregate','task_id':root,'model_id':'kane-fixture/deterministic-v1'})['id']
    for agent in [parent,r.agent]:r.call({'op':'collaboration_harness_profile','id':agent,'capabilities':['read-public-text'],'evidence_hash':FINGERPRINT,'expires_at':int(time.time())+300})
    p={'root':root,'scenario':'human-document','environment':FINGERPRINT,'max_agents':2,'experimental':True,'reason':'Owner-authorized document handoff integration','nodes':[{'task_id':root,'agent_id':parent,'role':'aggregate','requires':['read-public-text'],'depends_on':[r.task]},{'task_id':r.task,'agent_id':r.agent,'role':'native document','requires':['read-public-text'],'depends_on':[]}]}
    await core.request('collaboration_create',plan=p);await core.request('collaboration_claim',id=r.task)
    async def current():
        snapshot=await state(client)
        t=next(t for t in snapshot['tasks'] if t['id']==r.task)
        w=next(w['workspace'] for w in snapshot['workspaces'] if w['workspace']['id']==r.ws)
        return snapshot,t,{'epoch':w['lease_epoch'],'version':w['version']}
    async def waiting():
        async with asyncio.timeout(45):
            while True:
                v=await current()
                if v[1]['state'] in {'Verifying','Failed'} or v[1].get('block_reason')=='reconciliation':return v
                await asyncio.sleep(.05)
    async with control(execution,monkeypatch) as (client,app):
        response=await client.post(f'/control-api/tasks/{r.task}/dispatch',json={'content':'Read actual native input.txt and produce Markdown.'});assert response.status_code==200,response.text
        first,t,v=await waiting();assert t['state']=='Verifying'
        await core.request('collaboration_policy',id=root,version=0,max_agents=2,paused=True,reason='Owner takes native child workspace; block all new collaborative calls')
        with pytest.raises(CoreRejected):await core.request('collaboration_claim',id=root)
        taken=await client.post(f'/control-api/tasks/{r.task}/takeover',json=v);assert taken.status_code==200,taken.text
        read=(await client.get(f'/control-api/tasks/{r.task}/files/input.txt')).json()
        body={**v,'request_id':'p5-human-edit','path':'input.txt','content':'P5_REAL_HUMAN_NATIVE_EDIT\n','base_hash':read['hash']}
        # Takeover changes the authoritative epoch.
        _,_,v=await current();body.update(v)
        saved=await client.post(f'/control-api/tasks/{r.task}/save',json=body);assert saved.status_code==200,saved.text
        assert (await core.request('collaboration_status',id=root))['paused']
        _,_,v=await current()
        # Explicit Owner reopens group admission; the existing per-workspace
        # Human/Returning fence still enforces the actual handoff commit point.
        await core.request('collaboration_policy',id=root,version=1,max_agents=2,paused=False,reason='Owner finished edits, explicitly authorizes handoff under native workspace fence')
        handed=await client.post(f'/control-api/tasks/{r.task}/handoff',json={**v,'read_path':'input.txt','message_id':'p5-human-handoff','content':'Read input.txt again and incorporate actual human edit.'});assert handed.status_code==200,handed.text
        final,t,_=await waiting();assert t['state']=='Verifying',t
        artifacts=[a for a in final['artifacts'] if a['task_id']==r.task and a['verification_state']=='pending']
        assert len(artifacts)==1 and 'P5_REAL_HUMAN_NATIVE_EDIT' in artifacts[0]['content']
        assert hashlib.sha256((r.workspace/'input.txt').read_bytes()).hexdigest()==artifacts[0]['workspace_source']['files']['input.txt']['hash']
        done=await client.post(f'/control-api/tasks/{r.task}/verify',json={'version':t['version'],'passed':True,'evidence':'Phase5 independent actual human bytes, original read ACK, source/hash and fencing checks'});assert done.status_code==200,done.text
        assert (await core.request('task',id=r.task))['state']=='Done'
        events=await core.request('events')
        assert len([e for e in events if e['kind']=='handoff_read_ack'])==1
        (tmp_path/'collaboration-human-takeover.json').write_text(json.dumps({'root':root,'child':r.task,'snapshot':final,'events':events,'scope':'Linux ASGI authenticated control; actual native file, Rust, Docker and original DSH Session; no new platform claim'},indent=2))
    await core.request('cancel_task',id=root,reason='Stop unexecuted aggregate after focused handoff integration')
