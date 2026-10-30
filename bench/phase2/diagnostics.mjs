import { appendFileSync } from 'node:fs';
// Disposable public-input test diagnostics. No replacement model/Agent logic.
export const name='phase2-test-diagnostics';
export const inject=['llm','tools'];
export function apply(ctx,config={}){
 if(config.read_task || config.no_tools) ctx.on('agent/created',({agent})=>{agent.ctx.tools.restrict({allow:config.read_task?['read']:[]});});
 ctx.tools.guard(exec=>config.read_task && exec.name==='read' && exec.arguments?.file_path==='input.txt' ? undefined : 'PHASE2_NO_PRODUCTION_TOOLS');
 ctx.on('llm/stream',async function*(options,next){
  const record=value=>{if(config.read_task)appendFileSync(process.env.HOME+'/p2-diagnostics.jsonl',JSON.stringify(value)+'\n');};
  record({tools:options.tools?.map(t=>t.name)});
  try{for await(const chunk of next()){
   record({chunk});
   if(chunk.type==='finish' && chunk.reason.failure) console.error('PHASE2_PUBLIC_TEST_FAILURE',JSON.stringify(chunk.reason.failure));
   yield chunk;
  }}catch(error){record({error:String(error),stack:error.stack});console.error('PHASE2_PUBLIC_TEST_EXCEPTION',String(error));throw error;}
 },{global:true});
}
