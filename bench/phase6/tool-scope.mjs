import { appendFileSync } from 'node:fs';
// Disposable public-input test diagnostics. No replacement model/Agent logic.
export const name='phase2-test-diagnostics';
export const inject=['llm','tools','systemPrompt'];
export function apply(ctx,config={}){
 if(config.minimal_public_prompt===true){
  if(!config.no_tools) throw new Error('Minimal public fixture requires no tools');
  ctx.systemPrompt.section({name:'native:public-echo-reference',order:0,complete:true,text:config.public_echo_prompt===true?'Echo exactly.':'You are Kanaloa. Follow the user requested output exactly.'});
  ctx.systemPrompt.suppressRuntimeContext();
 }
 if(config.read_task || config.no_tools) ctx.on('agent/created',({agent})=>{agent.ctx.tools.restrict({allow:config.read_task?['read']:[]});});
 ctx.tools.guard(exec=>config.read_task && exec.name==='read' && exec.arguments?.file_path==='input.txt' ? undefined : 'PHASE2_NO_PRODUCTION_TOOLS');
 ctx.on('llm/stream',async function*(options,next){
  if(config.minimal_public_prompt===true) console.error('PHASE6_PUBLIC_REQUEST_SHAPE',JSON.stringify({
   option_keys:Object.keys(options),
   messages:options.messages?.map(m=>({role:m.role,serialized_content_bytes:Buffer.byteLength(JSON.stringify(m.content??''))})),
   system_bytes:typeof options.system==='string'?Buffer.byteLength(options.system):null
  }));
  const record=value=>{if(config.read_task)appendFileSync(process.env.HOME+'/p2-diagnostics.jsonl',JSON.stringify(value)+'\n');};
  record({tools:options.tools?.map(t=>t.name)});
  try{for await(const chunk of next()){
   record({chunk});
   if(chunk.type==='finish' && chunk.reason.failure) console.error('PHASE2_PUBLIC_TEST_FAILURE',JSON.stringify(chunk.reason.failure));
   yield chunk;
  }}catch(error){record({error:String(error),stack:error.stack});console.error('PHASE2_PUBLIC_TEST_EXCEPTION',String(error));throw error;}
 },{global:true});
}
// Explicit module interop for isolated, out-of-package reference plugin paths.
// The same guard/stream functions are used; this grants no additional tools.
export default { name, inject, apply };
