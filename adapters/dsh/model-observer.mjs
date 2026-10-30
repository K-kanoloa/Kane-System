// Optional ACP observation extension. No Core credentials, authorization or loop.
import { randomUUID } from 'node:crypto';
import { budgetCallIds } from './model-budget.mjs';
export const name = 'kane-model-observer';
export const inject = ['llm'];
export function apply(ctx) {
  const notify = params => process.stdout.write(JSON.stringify({jsonrpc:'2.0',method:'kanesys/model_observation',params})+'\n');
  ctx.on('llm/stream', async function* (options, next) {
    const base = {call_id:budgetCallIds.get(options) ?? randomUUID(),native_session_ref:options.sessionId,
      provider:options.provider,model:options.model};
    if (!base.native_session_ref) { yield* next(); return; }
    const started=performance.now();
    let usage=null, outcome='unknown';
    notify({...base,stage:'start'});
    try {
      for await (const chunk of next()) {
        if (chunk.type==='usage') usage=chunk.usage;
        if (chunk.type==='finish' && ['stop','tool-calls'].includes(chunk.reason.kind)) outcome='completed';
        yield chunk;
      }
    } catch (error) { outcome='failed'; throw error; }
    finally { notify({...base,stage:'result',outcome,usage,duration_ms:performance.now()-started}); }
  });
}
