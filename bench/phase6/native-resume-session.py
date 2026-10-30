#!/usr/bin/python3
"""Original ACP context resume only. Never sends a new prompt or retries a Task."""
import asyncio,json,os,sys
from pathlib import Path
SRC=Path('/usr/lib/kane-system/source');sys.path[:0]=[str(SRC/'apps/api'),'/opt/kane-test-python']
from app.adapters.kanaloa_adapter import KanaloaAdapter
from app.system.client import SystemClient
from app.system.execution import ExecutionBridge
from app.store.sqlite_store import SQLiteStore
from app.harness.coordinator import HarnessCoordinator
from app.harness.mailbox import MailboxManager

async def main():
 os.environ.update(KANE_SYSTEM_SOCKET='/run/kane-system/core.sock',KANE_SYSTEM_BINARY='/usr/bin/kanesys')
 info=json.loads(Path(sys.argv[1]).read_text());root=Path(info['root']);core=SystemClient()
 store=SQLiteStore(str(root/'harness.db'));turn=store.get_turn(info['turn_id']);assert turn and turn.native_session_ref
 original=turn.native_session_ref;before=await core.request('model_budget',id=info['budget']);queue_before=[q for q in await core.request('model_queue') if q['task_id']==info['task']]
 adapter=KanaloaAdapter(cwd=str(root/'workspace'),process_mode='supervised',supervisor_socket='/run/kane-system/'+info['agent']+'.runtime.sock',system_agent_id=info['agent'],initialize_timeout_seconds=600)
 adapter.execution_socket=str(Path(info['gateway'])/'gateway.sock');adapter.execution_bridge=ExecutionBridge(store,info['agent'],core)
 coordinator=HarnessCoordinator(store,MailboxManager());coordinator.register_adapter('kanaloa',adapter)
 try:
  await adapter.resume(turn,[]);assert adapter.get_native_session(turn.turn_id)==original
  assert await core.request('model_budget',id=info['budget'])==before
  queue_after=[q for q in await core.request('model_queue') if q['task_id']==info['task']]
  # at_ms is the Core query observation clock, not a persisted call field.
  # Compare every durable identity/state/timing field, retaining both raw reads.
  stable=lambda rows:[{k:v for k,v in q.items() if k!='at_ms'} for q in rows]
  assert stable(queue_after)==stable(queue_before)
  events=await core.request('events');assert len([e for e in events if e['kind']=='prompt_dispatch' and e['object_id']==info['task']])==1
  result={'native_session':original,'task':await core.request('task',id=info['task']),'budget':before,'queue_before':queue_before,'queue_after':queue_after,'excluded_observation_only_field':'at_ms','automatic_prompt_replays':0}
  (root/'native-session-recovered.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))
 finally:await adapter.close();store.close()
asyncio.run(main())
