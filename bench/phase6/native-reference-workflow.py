#!/usr/bin/python3
"""Optional offline read-only reference workflow using the ORIGINAL Kanaloa stack.
Provisioning is an explicit local Owner action. Core owns objects/state/permissions;
this module has no Agent loop, model client, scheduler, memory store or SQL fallback.
"""
import argparse,asyncio,hashlib,json,os,re,shutil,socket,subprocess,sys,time,uuid
from pathlib import Path
SRC=Path('/usr/lib/kane-system/source')
sys.path[:0]=[str(SRC/'apps/api'),'/opt/kane-test-python']
os.environ.update(KANE_SYSTEM_SOCKET='/run/kane-system/core.sock',KANE_SYSTEM_BINARY='/usr/bin/kanesys')
from app.system.client import SystemClient

def call(op,**kw):return asyncio.run(SystemClient().request(op,**kw))
def run(*args):return subprocess.run(args,check=True,text=True,capture_output=True).stdout.strip()
def write(path,data,uid=0,gid=901,mode=0o640):
 path.parent.mkdir(parents=True,exist_ok=True)
 with path.open('x') as f:os.fchmod(f.fileno(),mode);f.write(data);f.flush();os.fsync(f.fileno())
 os.chown(path,uid,gid)
def configure(a):
 assert os.geteuid()==0 and Path('/usr/share/kane-system/role').read_text().strip()=='installed'
 assert Path('/etc/kane-system/owner-initialized.json').is_file(),'initialize the local Owner first'
 assert not a.public_echo_prompt or (a.public_simple_task and a.mode=='hello')
 name=a.name;assert re.fullmatch('[a-z][a-z0-9-]{0,39}',name)
 config=Path('/etc/kane-system')/('reference-'+name+'.json');assert not config.exists(),'existing workflow: inspect, never silently replay/reconfigure'
 weights=a.weights.resolve(strict=True);assert weights.is_file() and not a.weights.is_symlink()
 model_root=Path('/var/lib/kane-models');weights_copy=model_root/(name+'.gguf')
 if a.reuse_public_weights:
  candidate=a.reuse_public_weights
  assert a.public_simple_task and candidate.resolve(strict=True)==candidate and candidate.parent==model_root and not candidate.is_symlink()
  assert candidate.stat().st_mode&0o222==0 and candidate.stat().st_size==weights.stat().st_size
  with candidate.open('rb') as f,weights.open('rb') as g:assert hashlib.file_digest(f,'sha256').digest()==hashlib.file_digest(g,'sha256').digest()
  weights_copy=candidate
 else:
  assert not weights_copy.exists();shutil.copyfile(weights,weights_copy);os.chown(weights_copy,901,901);weights_copy.chmod(0o400)
 source=a.input.resolve(strict=True).read_bytes();assert 0<len(source)<=65536
 expected_hash=hashlib.sha256(source).hexdigest()
 total_memory=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemTotal:')))*1024
 policy={'capacity':{'cpu_millis':2000,'memory_bytes':min(3*1024**3,total_memory),'pids':2048,'storage_bytes':2*1024**3},'reserved':{'cpu_millis':200,'memory_bytes':512*1024**2,'pids':128,'storage_bytes':128*1024**2}}
 actual=call('host_resources')
 if actual.get('policy') is None:call('configure_host_resources',version=0,policy=policy)
 elif actual.get('policy')!=policy:
  existing=actual['policy']
  # A different explicit virtual CPU profile can change the kernel's MemTotal
  # by a few pages. Retain a MORE conservative existing Owner limit; never
  # expand/reset the pool or any unresolved call to make the fixture run.
  if a.retain_host_resource_policy:
   assert existing['reserved']==policy['reserved']
   assert all(existing['capacity'][k]==policy['capacity'][k] for k in ['cpu_millis','pids','storage_bytes'])
   assert 0<existing['capacity']['memory_bytes']<=total_memory, (existing,policy,total_memory)
  else:raise RuntimeError('Existing Host Resource Pool differs; inspect before changing policy')
 model='local/'+name;context=a.context_tokens or (16384 if a.mode=='read' else 8192);output=512 if a.mode=='read' else 32
 call('register_model',id=model,spec={'version':('native-public-echo-literal-v2' if a.public_echo_prompt else ('native-public-echo-minimal-v1' if a.public_simple_task else 'native-reference-v1')),'cloud':False,'scheduling':{'mode':'gateway_fifo','backend_kind':'local_http'},'capabilities':['text-inference','streaming-usage'],'privacy':{'cloud_allowed':False,'prompt_cache':'disabled'},'pricing':{'status':'UNKNOWN','actual_invoice_verified':False},'budget_profile':{'provider':'local','model':name,'upper_bound_tokens':context+output,'bound_evidence':'Explicit local context/output limits; actual provider usage settles separately','gateway_wait_ms':180000},'source':{'backend_kind':'real-local-llm','weights_sha256':hashlib.file_digest(weights_copy.open('rb'),'sha256').hexdigest()}})
 workspace=call('create_workspace',kind='document')['id'];budget=call('create_budget',tokens=100000)['id']
 task=call('create_task',spec={'goal':'Read the authorized native input file and report its exact contents' if a.mode=='read' else 'Return exactly the single word '+a.expected_output+' using the real local model','acceptance':['Original read tool is confirmed','Native input hash unchanged','Artifact contains exact independently known input','Only independent verification may mark Done'] if a.mode=='read' else ['Real local usage settles, no unresolved reservation','Artifact text is '+a.expected_output+' (case-insensitive, optional final punctuation)','Only independent verifier may mark Done'],'workload':{'class':'execution','max_memory_bytes':384*1024**2,'max_storage_bytes':128*1024**2},'budget_id':budget,'workspace_id':workspace,'cloud_allowed':False})['id']
 agent=call('register_agent',role='optional Kanaloa read-only reference',task_id=task,model_id=model)['id']
 instance=call('register_model_instance',model_id=model)['id']
 root=Path('/var/lib/kane-runtime')/agent;root.mkdir(mode=0o700);os.chown(root,901,901)
 for component in ['workspace','home']:(root/component).mkdir(mode=0o700);os.chown(root/component,901,901)
 native=root/'workspace/input.txt';write(native,source.decode(),uid=901,mode=0o400)
 home=root/'home';image=root/'home.ext4';run('truncate','-s',str(a.home_capacity_mib)+'M',str(image));run('mkfs.ext4','-q','-F','-m','0',str(image))
 mount_name=run('systemd-escape','--path','--suffix=mount',str(home))
 write(Path('/etc/systemd/system')/mount_name,f'[Unit]\nDescription=Persistent bounded HOME for {agent}\nPartOf=kane-system.target\nBefore=kane-agent@{agent}.service\n[Mount]\nWhat={image}\nWhere={home}\nType=ext4\nOptions=loop,nosuid,nodev,noexec\n[Install]\nWantedBy=kane-system.target\n',gid=0,mode=0o644)
 run('systemctl','daemon-reload');run('systemctl','enable','--now',mount_name);os.chown(home,901,901);home.chmod(0o700)
 (home/'.dsh').mkdir(mode=0o700);os.chown(home/'.dsh',901,901)
 provider={'providers':{'local':{'api':'openai-completions','baseURL':'http://kane-model.invalid/v1','headers':{'Authorization':'Bearer local-placeholder-not-a-secret'},'cacheRetention':'none','retryPolicy':{'mode':'normal','maxRetries':0},'compat':{'supportsStore':False,'supportsUsageInStreaming':True,'maxTokensField':'max_tokens'},'models':[{'id':name,'name':'Owner local CPU model','contextWindow':context,'maxTokens':output,'input':['text'],'reasoning':False}]}}}
 settings={'agent-default-model':{'provider':'local','model':name},'llm-pi-ai':provider};write(home/'.dsh/settings.yaml',json.dumps(settings),uid=901,mode=0o600)
 gate=Path('/run/kane-gateways')/agent;execution=str(gate/'gateway.sock')
 profile=root/'profile.json'
 write(profile,json.dumps([{'id':'agent-default-model','config':{'provider':'local','model':name}},{'id':'acp','config':{'provider':'local','model':name}},{'id':'llm-pi-ai','config':provider},{'insert':[{'id':'kane-model-observer','name':str(SRC/'adapters/dsh/model-observer.mjs')},{'id':'kane-model-budget','name':str(SRC/'adapters/dsh/model-budget.mjs'),'config':{'socket':execution,'gateway_socket':str(gate/'model.sock'),'require_fifo':True}},{'id':'kane-tool-control','name':str(SRC/'adapters/dsh/tool-control.mjs'),'config':{'socket':execution}}]}]),uid=901,mode=0o400)
 diagnostic=Path(__file__).resolve().with_name('tool-scope.mjs')
 restriction=root/'tool-scope.mjs';guard=diagnostic.read_bytes();assert guard and b'export function apply' in guard
 restriction.write_bytes(guard);assert restriction.read_bytes()==guard
 os.chown(restriction,901,901);restriction.chmod(0o400)
 entries=json.loads(profile.read_text());entries[-1]['insert'].append({'id':'phase2-diagnostics','name':str(restriction),'config':{'read_task':a.mode=='read','no_tools':a.mode=='hello','minimal_public_prompt':a.public_simple_task,'public_echo_prompt':a.public_echo_prompt}});profile.chmod(0o600);profile.write_text(json.dumps(entries));profile.chmod(0o400)
 image_id=run('/usr/local/bin/docker','image','inspect','ubuntu:24.04','--format','{{.Id}}')
 runtime={'agent_id':agent,'command':['/usr/local/bin/node',str(SRC/'node_modules/@deepseek-ai/dsh/lib/bin.js'),'--profile','acp','--patch',str(SRC/'apps/api/app/adapters/kanaloa-persona.patch.yml'),'--patch',str(profile)],'cwd':str(root/'workspace'),'runtime_home':str(home),'max_restarts':3,'restart_delay_ms':1000,'sandbox':{'image':image_id,'cpu_millis':500,'memory_bytes':384*1024**2,'pids':96,'read_only':['/usr/local/bin/node',str(SRC/'node_modules'),str(SRC/'adapters/dsh'),str(SRC/'packages/kanaloa-tools'),str(SRC/'apps/api/app/adapters/kanaloa-persona.patch.yml'),str(profile),str(restriction)],'gateway_dir':str(gate),'home_capacity_bytes':a.home_capacity_mib*1024**2,'host_resources':True,'model_gateway':True}}
 write(Path('/etc/kane-system/runtimes')/(agent+'.json'),json.dumps(runtime))
 backend={'instance_id':instance,'command':['/opt/kane-local-model/llama-server','--model',str(weights_copy),'--host','127.0.0.1','--port','18181','--alias','native-reference','--ctx-size',str(context),'--parallel','1','--threads','2','--threads-http','2','--n-gpu-layers','0','--no-warmup','--cache-ram','0','--seed','0','--temp','0','--chat-template-kwargs','{"enable_thinking":false}'],'runtime':{'endpoint':'http://127.0.0.1:18181','backend_model':'native-reference','context_tokens':context,'parallelism':1,'resources':{'cpu_millis':a.model_cpu_millis,'memory_bytes':(1792 if a.mode=='read' else 1024)*1024**2,'pids':64,'storage_bytes':(768 if a.mode=='read' else 128)*1024**2}}}
 if a.backend_warmup:backend['command'].remove('--no-warmup')
 if a.model_load_timeout_seconds is not None:backend['health_timeout_seconds']=a.model_load_timeout_seconds
 write(Path('/etc/kane-system/models')/(instance+'.json'),json.dumps(backend))
 for d in ['runtimes','models']:
  p=Path('/etc/kane-system')/d;os.chown(p,0,901);p.chmod(0o750)
 call('configure_model_gateway',id=model,version=0,policy={'instance_id':instance,'max_output_tokens':output,'cache':'disabled','sensitivity':'public','requires_kernel_limits':True,'workload_classes':['inference','execution'],'workspaces':[workspace],**({'timeout_seconds':a.model_request_timeout_seconds} if a.model_request_timeout_seconds is not None else {})})
 if a.mode=='read':call('grant_tool',id=agent,tool='read',arguments={'file_path':'input.txt'},expires_at=int(time.time())+1800)
 info={'agent':agent,'task':task,'workspace':workspace,'budget':budget,'instance':instance,'root':str(root),'home':str(home),'gateway':str(gate),'input_sha256':expected_hash,'turn_id':'native-'+uuid.uuid4().hex,'message_id':'native-'+uuid.uuid4().hex,'conversation_id':'native-'+uuid.uuid4().hex,'read_only_reference':True,'mode':a.mode,'expected_output':a.expected_output,'public_simple_task':a.public_simple_task,'test_scope_sha256':hashlib.sha256(guard).hexdigest(),'home_capacity_mib':a.home_capacity_mib,'model_request_timeout_seconds':a.model_request_timeout_seconds or 45,'public_echo_prompt':a.public_echo_prompt,'backend_warmup':a.backend_warmup}
 write(config,json.dumps(info,indent=2));print(json.dumps(info))
async def execute(path,retry_uninitialized=False,initialize_timeout=180):
 from app.adapters.kanaloa_adapter import KanaloaAdapter
 from app.system.execution import ExecutionBridge
 from app.store.sqlite_store import SQLiteStore
 from app.harness.coordinator import HarnessCoordinator
 from app.harness.mailbox import MailboxManager
 from app.domain.models import Conversation,Turn,Message
 info=json.loads(Path(path).read_text());root=Path(info['root']);store=SQLiteStore(str(root/'harness.db'));core=SystemClient()
 coordinator=HarnessCoordinator(store,MailboxManager())
 adapter=KanaloaAdapter(cwd=str(root/'workspace'),process_mode='supervised',supervisor_socket='/run/kane-system/'+info['agent']+'.runtime.sock',system_agent_id=info['agent'],initialize_timeout_seconds=initialize_timeout)
 coordinator.register_adapter('kanaloa',adapter);adapter.execution_socket=str(Path(info['gateway'])/'gateway.sock');adapter.execution_bridge=ExecutionBridge(store,info['agent'],core)
 c=Conversation(conversation_id=info['conversation_id'],bound_agent_id='kanaloa');t=Turn(turn_id=info['turn_id'],conversation_id=c.conversation_id,bound_agent_id='kanaloa')
 m=Message(message_id=info['message_id'],conversation_id=c.conversation_id,turn_id=t.turn_id,sender='user',content=('Use the read tool to read input.txt with arguments {"file_path":"input.txt"}. Then return only the exact text you read. Do not guess the file content. Do not use any other tool.' if info['mode']=='read' else (info.get('expected_output','HELLO') if info.get('public_echo_prompt') else 'Reply with only the single word '+info.get('expected_output','HELLO')+'. Do not use tools.')))
 # Persistent IDs deliberately survive interruption: Core rejects a repeated dispatch.
 existing=store.get_turn(t.turn_id)
 if existing:
  if not retry_uninitialized:raise RuntimeError('Turn already exists; inspect/reconcile instead of replaying')
  task=await core.request('task',id=info['task']);budget=await core.request('model_budget',id=info['budget']);events=await core.request('events');queue=await core.request('model_queue')
  assert task['state'] in ['Pending','Ready'] and not existing.native_session_ref
  assert budget['used_tokens']==0 and budget['reserved_tokens']==0 and budget['active_calls']==0
  assert not any(e['kind']=='prompt_dispatch' and e['object_id']==info['task'] for e in events)
  assert not any(q['task_id']==info['task'] for q in queue) and not await core.request('artifacts',id=info['task'])
  original=store.get_message(m.message_id);assert original and original.content==m.content and original.turn_id==existing.turn_id
  t=existing;m=original
  (root/'initialization-retry-proof.json').write_text(json.dumps({'task':task,'budget':budget,'no_core_prompt_dispatch':True,'no_model_queue_or_artifact':True,'native_session_never_created':True,'existing_turn_and_message_retained':True},indent=2))
 else:
  store.save_conversation(c);store.save_turn(t);store.append_message(m)
 try:
  await adapter.activate()
  (root/'acp-initialized.json').write_text(json.dumps({'initialized':True,'initialize_timeout_seconds':initialize_timeout,'same_original_Adapter':True}))
  await adapter.send(t,m,[])
  async with asyncio.timeout(600):
   while True:
    task=await core.request('task',id=info['task'])
    if task['state'] in ['Verifying','Blocked','Failed','Cancelled']:break
    await asyncio.sleep(.2)
  evidence={name:await core.request(name,id=info[key]) for name,key in [('task','task'),('artifacts','task'),('model_budget','budget'),('runtime_status','agent')]}
  evidence['events']=await core.request('events');evidence['model_instances']=await core.request('model_instances');evidence['host_resources']=await core.request('host_resources');evidence['model_queue']=await core.request('model_queue')
  (root/'execution-evidence.json').write_text(json.dumps(evidence,indent=2));print(json.dumps({'task':task,'evidence':str(root/'execution-evidence.json')}))
 except BaseException as error:
  facts={name:await core.request(name,id=info[key]) for name,key in [('task','task'),('model_budget','budget'),('runtime_status','agent')]}
  facts.update(error_type=type(error).__name__,model_queue=await core.request('model_queue'),events=await core.request('events'),native_session_ref=store.get_turn(t.turn_id).native_session_ref)
  (root/'execution-failure.json').write_text(json.dumps(facts,indent=2));raise
 finally:await adapter.close();store.close()
def verify(path):
 info=json.loads(Path(path).read_text());root=Path(info['root']);data=(root/'workspace/input.txt').read_bytes();artifacts=call('artifacts',id=info['task']);task=call('task',id=info['task']);events=call('events')
 passed=task['state']=='Verifying' and len(artifacts)==1 and hashlib.sha256(data).hexdigest()==info['input_sha256']
 passed=passed and (data.decode().strip() in artifacts[0]['content'] if info['mode']=='read' else artifacts[0]['content'].strip().strip('.!').upper()==info.get('expected_output','HELLO')) and hashlib.sha256(artifacts[0]['content'].encode()).hexdigest()==artifacts[0]['content_hash']
 actions=[e['payload']['action'] for e in events if e['kind']=='result' and e['object_id']==info['task'] and e['payload'].get('receipt',{}).get('outcome')=='completed']
 passed=passed and (bool(actions) if info['mode']=='read' else True) and all(call('action',id=x)['state']=='Confirmed' for x in actions)
 budget=call('model_budget',id=info['budget']);passed=passed and budget['reserved_tokens']==0 and budget['used_tokens']>0
 if task['state']!='Verifying':raise RuntimeError('No verifiable outcome; preserve state and inspect evidence')
 result=call('verify',id=info['task'],version=task['version'],passed=bool(passed),evidence='Independent native-file SHA256, original read and confirmed actions with settled real usage' if info['mode']=='read' else 'Independent predeclared '+info.get('expected_output','HELLO')+' acceptance, actual local model dispatch and settled usage; no output substitution')
 (root/'verification.json').write_text(json.dumps({'passed':bool(passed),'task':result,'input_sha256':info['input_sha256'],'artifacts':artifacts,'budget':budget},indent=2));print(json.dumps(result))
 if not passed:raise SystemExit(1)
def main():
 ap=argparse.ArgumentParser(description=__doc__);sp=ap.add_subparsers(dest='op',required=True)
 p=sp.add_parser('configure');p.add_argument('--weights',type=Path,required=True);p.add_argument('--input',type=Path,required=True);p.add_argument('--mode',choices=['read','hello'],default='read');p.add_argument('--name',default='reference')
 p.add_argument('--public-simple-task',action='store_true');p.add_argument('--expected-output',choices=['HELLO','KANE'],default='HELLO')
 p.add_argument('--model-cpu-millis',type=int,choices=[1000,1300],default=1000)
 p.add_argument('--model-load-timeout-seconds',type=int,choices=[45,180])
 p.add_argument('--home-capacity-mib',type=int,choices=[8,64],default=64,help='Explicit bounded reference HOME size; never disables quota')
 p.add_argument('--context-tokens',type=int,choices=[4096,8192,16384],help='Actual backend/Registry/provider context capacity, not charged usage')
 p.add_argument('--reuse-public-weights',type=Path,help='Explicit Owner existing immutable public GGUF, byte/hash checked; never shares Context or unfreezes a call')
 p.add_argument('--retain-host-resource-policy',action='store_true',help='Keep an existing conservative Owner memory capacity on another declared virtual CPU profile; never expand it')
 p.add_argument('--public-echo-prompt',action='store_true',help='Short literal public echo request; original loop and unchanged exact-word verifier')
 p.add_argument('--backend-warmup',action='store_true',help='Use the existing backend normal initialization warmup; no user prompt or cached Task output')
 p.add_argument('--model-request-timeout-seconds',type=int,choices=[45,180],help='Explicit Owner inference deadline; default Core 45s unchanged')
 for name in ['execute','verify']:
  p=sp.add_parser(name);p.add_argument('job',type=Path)
  if name=='execute':
   p.add_argument('--retry-uninitialized',action='store_true');p.add_argument('--acp-initialize-timeout-seconds',type=int,choices=[180,600],default=180)
 a=ap.parse_args()
 if a.op=='configure':configure(a)
 elif a.op=='execute':asyncio.run(execute(a.job,a.retry_uninitialized,a.acp_initialize_timeout_seconds))
 else:verify(a.job)
if __name__=='__main__':main()
