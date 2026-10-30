"""Task DAG with actual original DSH HTTP provider -> shared Rust Model Gateway.
Uses existing local mock ModelInstance, independent of in-process FIFO benchmark.
"""
import asyncio,hashlib,json,os,time
import pytest
from test_phase2_models import models,finish
from test_phase5_collaboration import plan,coordinator,FINGERPRINT
pytestmark=pytest.mark.skipif(os.getenv('KANE_PHASE5_TEST')!='1',reason='opt-in Phase5 shared ModelInstance integration')

@pytest.mark.asyncio
@pytest.mark.parametrize('models',[{'agents':3,'task_goal':'Deliver the fixed local public reference document','task_acceptance':['Exact independently fixed local fixture content and SHA256; Core receipts and independent validation']}],indirect=True)
async def test_collaboration_uses_existing_rust_gateway_and_shared_model_instance(models,tmp_path):
    items=models['items'];core=items[0][3];c=coordinator(items);root=items[0][0].task
    for item in items:
        item[0].call({'op':'collaboration_harness_profile','id':item[0].agent,'capabilities':['text-inference'],'evidence_hash':FINGERPRINT,'expires_at':int(time.time())+300})
    p=plan(items)
    for n in p['nodes']:n['requires']=['text-inference']
    await core.request('collaboration_create',plan=p)
    async def checked(item):
        state=await finish(item);assert state['state']=='Verifying',state
        artifacts=await core.request('artifacts',id=item[0].task)
        expected='# Verified test document\n\nLOCAL_MOCK_RESPONSE a b c d e f g h i\n'
        assert len(artifacts)==1 and artifacts[0]['content']==expected
        assert artifacts[0]['content_hash']==hashlib.sha256(expected.encode()).hexdigest()
        await core.request('verify',id=item[0].task,version=state['version'],passed=True,evidence='Independent fixed local wire fixture byte equality and hash; not real model quality')
        return artifacts[0]
    try:
        await asyncio.gather(*(c.dispatch(x[0].task) for x in items[1:]))
        artifacts=await asyncio.gather(*(checked(x) for x in items[1:]))
        refs=[]
        for a in artifacts:
            ctx=(await core.request('collaboration_context',artifact_id=a['id'],sensitivity='public',expires_at=int(time.time())+120))['id']
            await core.request('collaboration_share',id=ctx,agent_id=items[0][0].agent,expires_at=int(time.time())+60,ceiling='public');refs.append(ctx)
        c.bindings[items[0][0].agent].contexts=tuple(refs)
        await c.dispatch(root);await checked(items[0])
        events=await core.request('events');queue=await core.request('model_queue')
        calls=[e for e in events if e['kind']=='model_gateway_dispatch']
        assert len(calls)==3 and len(queue)==3 and all(q['state']=='Completed' for q in queue)
        (tmp_path/'shared-gateway-collaboration.json').write_text(json.dumps({'events':events,'queue':queue,'instances':await core.request('model_instances'),'scope':'actual three Kanaloa original HTTP providers; one existing Rust-managed local mock instance; not real intelligence benefit'},indent=2))
    finally:
        for item in items:await item[2].close()
