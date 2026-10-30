// Existing DSH streaming seam; no new Agent loop, model client or tools.
// Missing/denied/lost budget replies never call next(). No automatic retry/refund.
import { createConnection } from 'node:net';
import { randomUUID } from 'node:crypto';
import { configureGateway, gatewayStream } from './model-transport.mjs';
import { setTimeout as pause } from 'node:timers/promises';
// Object identity links observation to the same permit without mutating frozen requests.
export const budgetCallIds = new WeakMap();
export const name = 'kane-model-budget';
export const inject = ['llm'];
export function budgetRequest(socket, request) {
  return new Promise((resolve, reject) => {
    const client = createConnection({path:socket});
    let data='', done=false;
    const finish=(error,result)=>{if(done)return;done=true;client.destroy();error?reject(error):resolve(result);};
    client.setTimeout(18000,()=>finish(new Error('Model budget response timeout; no retry')));
    client.on('error',()=>finish(new Error('Model budget service unavailable')));
    client.on('end',()=>finish(new Error('Model budget response lost; no retry')));
    client.on('connect',()=>client.write(JSON.stringify(request)+'\n'));
    client.on('data',chunk=>{
      data+=chunk.toString();
      if(data.length>65536) return finish(new Error('Model budget response too large'));
      if(!data.includes('\n'))return;
      try {const reply=JSON.parse(data.slice(0,data.indexOf('\n')));
        if(reply.ok!==true) throw new Error('Model budget denied or unavailable');
        finish(null,reply.result);
      } catch(error) {finish(error);}
    });
  });
}
export function apply(ctx, config={}) {
  if (typeof config.socket!=='string' || !config.socket.startsWith('/')) throw new Error('Explicit budget socket required');
  const pollMs=config.poll_interval_ms??25;
  if(!Number.isSafeInteger(pollMs) || pollMs<25 || pollMs>1000) throw new Error('Bounded model queue poll interval required');
  if(config.gateway_socket) configureGateway(config.gateway_socket);
  process.stdout.write(JSON.stringify({jsonrpc:'2.0',method:'kanesys/budget_ready',params:{socket:config.socket}})+'\n');
  ctx.on('llm/stream',async function* (options,next) {
    if (!options.sessionId) throw new Error('Unbound model calls are not authorized');
    const base={call_id:randomUUID(),native_session_ref:options.sessionId};
    let permit, queued=false;
    try {
      permit=await budgetRequest(config.socket,{...base,op:'reserve',provider:options.provider,model:options.model});
      if(config.require_fifo===true && permit.state!=='Queued') throw new Error('Required shared FIFO unavailable; no direct-provider fallback');
      queued=permit.state==='Queued';
      // The persistent Core deadline is authoritative; a lost/duplicate reply
      // cannot extend it. Legacy Mock wait remains bounded at 15 seconds.
      const waitMs=config.gateway_socket && Number.isSafeInteger(permit.wait_timeout_ms) ? permit.wait_timeout_ms : (config.gateway_socket?180000:15000);
      if(waitMs<1 || waitMs>900000) throw new Error('Invalid bounded Core queue deadline');
      const deadline=performance.now()+waitMs;
      while(permit.state==='Queued') {
        options.signal?.throwIfAborted();
        if(performance.now()>=deadline) throw new Error('Shared model FIFO wait expired; no fallback');
        await pause(pollMs,undefined,{signal:options.signal});
        permit=await budgetRequest(config.socket,{...base,op:'claim'});
      }
      options.signal?.throwIfAborted();
    } catch(error) {
      // Cancelling an unissued queue item is safe; after a lost claim reply Core
      // conservatively keeps the issued permit/hold Unknown. Never execute next().
      if(queued) {
        try {await budgetRequest(config.socket,{...base,op:'cancel'});} catch {}
      }
      throw error;
    }
    if(permit.permit!==true || permit.call_id!==base.call_id) throw new Error('Invalid model permit');
    if(typeof permit.gateway_token==='string' && !config.gateway_socket) {
      try {await budgetRequest(config.socket,{...base,op:'cancel'});} catch {}
      throw new Error('Gateway route requires its protected transport; no direct-provider fallback');
    }
    if(config.gateway_socket && typeof permit.gateway_token!=='string') throw new Error('Trusted model gateway permit missing');
    budgetCallIds.set(options,base.call_id);
    let usage=null, finish=null, failure=null;
    try {
      for await (const chunk of config.gateway_socket ? gatewayStream(permit.gateway_token,next) : next()) {
        if(chunk.type==='usage') usage=chunk.usage;
        if(chunk.type==='finish') finish=chunk;
        else yield chunk;
      }
    } catch(error) {failure=error;}
    finally {
      // A truncated/throwing stream may have emitted partial usage: retain the hold.
      // DSH can convert provider exceptions to error finish chunks. Even a usage
      // chunk before that error may be partial, so only clean completion settles.
      const outcome=!failure && finish && ['stop','tool-calls'].includes(finish.reason.kind) ? 'completed' : 'unknown';
      const receipt={outcome,usage:outcome==='unknown'?null:usage};
      try {
        const settled=await budgetRequest(config.socket,config.gateway_socket ? {...base,op:'gateway_status'} : {...base,op:'settle',receipt});
        if(settled.state!=='Settled') throw new Error('Model usage unresolved; reservation retained');
      } finally { budgetCallIds.delete(options); }
    }
    if(failure) throw failure;
    if(finish) yield finish;
  },{global:true,prepend:true});
}
