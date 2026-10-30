"""Phase 2 final gaps, original Kanaloa and tools. Explicit reference tests only."""
import asyncio,hashlib,json,os
from pathlib import Path
import pytest
from test_phase2_models import models,finish
from test_system_execution import run_task
pytestmark=pytest.mark.skipif(os.getenv('KANE_PHASE2_FINAL_TEST')!='1',reason='opt-in Phase 2 final reference acceptance')

@pytest.mark.asyncio
@pytest.mark.parametrize('models',[{'real':True,'output_tokens':512,'read_task':True,'model_file':os.getenv('KANE_PHASE2_TOOL_MODEL_FILE','/tmp/kane-phase2-final-deps/Qwen3-0.6B-Q8_0.gguf'),'backend_args':['--chat-template-kwargs','{"enable_thinking":false}','--temp','0','--seed','0']}],indirect=True)
async def test_real_tool_outcome(models):
 item=models['items'][0];r,store,adapter,core=item
 source='KANE_REAL_MARKER_Q7X9\n';(r.workspace/'input.txt').write_text(source)
 try:
  turn,_,_=await run_task(item,wait=False,prompt='Use the read tool to read input.txt with arguments {"file_path":"input.txt"}. Then return only the exact text you read. Do not guess the file content. Do not use any other tool.')
  state=await finish(item,seconds=120)
  events=await core.request('events');artifacts=await core.request('artifacts',id=r.task)
  (models['path']/'real-outcome-attempt.json').write_text(json.dumps({'task':state,'events':events,'artifacts':artifacts,'turn':str(store.get_turn(turn.turn_id))},indent=2))
  assert state['state']=='Verifying',state
  assert len(artifacts)==1 and source.strip() in artifacts[0]['content']
  assert artifacts[0]['content_hash']==hashlib.sha256(artifacts[0]['content'].encode()).hexdigest()
  assert (r.workspace/'input.txt').read_text()==source
  assert any(e['kind']=='result' and e['payload'].get('receipt',{}).get('outcome')=='completed' for e in events),[(e['kind'],e['payload']) for e in events if 'tool' in e['kind']]
  for e in events:
   if e['kind']=='result' and e['payload'].get('receipt',{}).get('outcome')=='completed':
    assert (await core.request('action',id=e['payload']['action']))['state']=='Confirmed'
  assert (await core.request('model_budget',id=r.budget))['reserved_tokens']==0
  done=await core.request('verify',id=r.task,version=state['version'],passed=True,evidence='Independent public fixture: original read confirmed, marker read from file (not prompt), immutable input and artifact SHA256 match')
  assert done['state']=='Done'
  (models['path']/'real-outcome.json').write_text(json.dumps({'task':done,'artifact':artifacts[0],'events':await core.request('events'),'independent_expected':source,'test_type':'real local inference, original read tool, no model output substitution'},indent=2))
 finally:await adapter.close()

@pytest.mark.asyncio
async def test_missing_model_delegation_refuses_before_backend_start(models):
 import subprocess
 item=models['items'][0];r,_,adapter,core=item
 try:
  instance=(await core.request('register_model_instance',model_id='kane-fixture/deterministic-v1'))['id']
  marker=models['path']/'must-not-start'
  spec={'instance_id':instance,'runtime':models['runtime']['runtime'],'command':['/usr/bin/touch',str(marker)],'enforcement':{'mode':'cgroup_v2','root':str(models['path'])}}
  path=models['path']/'denied-limits.json';path.write_text(json.dumps(spec))
  from test_supervised_runtime import BIN
  p=await asyncio.create_subprocess_exec(str(BIN),'--socket',str(r.core_socket),'model-run',str(path),stderr=asyncio.subprocess.PIPE)
  _,err=await p.communicate()
  assert p.returncode!=0 and not marker.exists()
  events=await core.request('events');assert any(e['object_id']==instance and e['kind']=='model_kernel_limits_denied' for e in events)
  assert (await core.request('host_resources'))['used']['cpu_millis']==1000
  (models['path']/'kernel-refusal.json').write_text(json.dumps({'exit_code':p.returncode,'stderr':err.decode(),'backend_never_started':True,'events':events},indent=2))
 finally:await adapter.close()
