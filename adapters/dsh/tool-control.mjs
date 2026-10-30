// Around the ORIGINAL DSH tool body. No replacement tool/Agent implementation.
// This host-cooperative seam is not Linux sandbox enforcement.
import { createHash } from 'node:crypto';
import { AsyncLocalStorage } from 'node:async_hooks';
import { budgetRequest } from './model-budget.mjs';
export const name='kane-tool-control';
export const inject=['tools','fs'];
const hash=value=>createHash('sha256').update(JSON.stringify(value)).digest('hex');
export function apply(ctx,config={}) {
  if(typeof config.socket!=='string' || !config.socket.startsWith('/')) throw new Error('Explicit tool-control socket required');
  const active = new AsyncLocalStorage();
  if(config.native_file_proxy === true) {
    // Keep the ORIGINAL write tool (schema, policy, observation and rendering).
    // Only its filesystem provider boundary delegates to the trusted Rust Core.
    ctx.fs.writeText = async (_target, content, _intent, signal) => {
      signal?.throwIfAborted();
      const bound=active.getStore();
      if(!bound || bound.exec.name!=='write' || content!==bound.exec.arguments.content)
        throw new Error('Unbound native file write denied');
      return budgetRequest(config.socket,{...bound.base,op:'tool_file',arguments:bound.exec.arguments});
    };
  }
  if(config.native_read_fencing === true) {
    const read = ctx.fs.readText.bind(ctx.fs);
    ctx.fs.readText = async (target, signal) => {
      const content = await read(target, signal);
      const bound = active.getStore();
      if(bound?.exec.name === 'read') bound.readHash = createHash('sha256').update(content).digest('hex');
      return content;
    };
  }
  process.stdout.write(JSON.stringify({jsonrpc:'2.0',method:'kanesys/tool_ready',params:{socket:config.socket,read_fencing:config.native_read_fencing===true}})+'\n');
  ctx.on('tools/execute',async (exec,next)=>{
    const native=exec.agent?.session?.id;
    if(!native || !exec.callId) throw new Error('Unbound tool execution denied');
    const base={native_session_ref:native,call_id:exec.callId};
    const intent=await budgetRequest(config.socket,{...base,op:'tool_prepare',tool:exec.name,arguments:exec.arguments});
    if(intent.state!=='Pending') throw new Error('Tool already dispatched or unresolved; no replay');
    const permit=await budgetRequest(config.socket,{...base,op:'tool_claim'});
    if(permit.permit!==true || permit.id!==intent.id || permit.call_id!==exec.callId) throw new Error('Invalid tool permit');
    // No retries, even if a claim reply was lost before reaching this point.
    let result, failure;
    const bound={base,exec};
    try { result=await active.run(bound,next); } catch(error) {failure=error;}
    const receipt={outcome:!failure && result?.isError===false?'completed':'unknown',result_hash:hash(result??{outcome:'thrown'})};
    const persisted=await budgetRequest(config.socket,{...base,op:'tool_result',receipt});
    if(persisted.state!=='Confirmed') throw new Error('Tool outcome awaiting reconciliation');
    if(failure) throw failure;
    if(config.native_read_fencing === true && exec.name==='read') {
      if(!bound.readHash) throw new Error('Original native read evidence missing');
      await budgetRequest(config.socket,{...base,op:'tool_read_ack',hash:bound.readHash});
    }
    return result;
  },{global:true,prepend:true});
}
