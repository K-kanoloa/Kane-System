// Test-only deterministic provider for the existing DSH LLM API, not an Agent.
import { createHash } from 'node:crypto';
import { LlmAdapter } from '@deepseek-ai/dsh-llm';
import { appendFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { setTimeout } from 'node:timers/promises';
import { budgetCallIds } from '../../adapters/dsh/model-budget.mjs';
export const name='kane-pr2-deterministic-model';
export const inject=['llm','tools'];
class FixtureModel extends LlmAdapter {
  constructor(config) { super(); this.config=config; }
  async resolveModel(provider,model) { return {provider,id:model,name:model,contextWindow:32000}; }
  async *stream(options) {
    options.signal?.throwIfAborted();
    if(this.config.invocation_log) appendFileSync(this.config.invocation_log,JSON.stringify({session:options.sessionId,model:options.model,call_id:budgetCallIds.get(options),entered_ms:Date.now()})+'\n');
    if(this.config.mode==='throw_after_usage') {
      yield {type:'usage',usage:{inputTokens:2,outputTokens:1,totalTokens:3}};
      throw new Error('Injected truncated provider stream');
    }
    if(this.config.mode==='known_failure') {
      yield {type:'usage',usage:{inputTokens:2,outputTokens:1,totalTokens:3}};
      yield {type:'finish',reason:{kind:'error',failure:{code:'FIXTURE',message:'injected final failure'}}};
      return;
    }

    if (this.config.delay_ms) await setTimeout(this.config.delay_ms,undefined,{signal:options.signal});
    // Phase 0 fixed workflow baseline only. The existing Agent loop still owns
    // iteration and calls the original read tool; this adapter supplies model
    // responses, never executes tools or accepts arbitrary paths.
    if (this.config.phase5_aggregate) {
      // Scripted benchmark response only; the original Agent loop still submits
      // the Artifact. Reject absent child data instead of fabricating a merge.
      const input=JSON.stringify(options.messages);
      for (const marker of this.config.phase5_aggregate) {
        if (!input.includes(marker) || !input.includes('artifact_hash') || !input.includes('source_task')) throw new Error('Missing authorized child Context');
      }
      const text='# Phase 0 fixture\n\n'+this.config.phase5_aggregate.join('\n')+'\n';
      yield {type:'block-start',index:0,blockType:'text'};
      yield {type:'text-delta',index:0,text};
      yield {type:'block-end',index:0,block:{type:'text',text}};
      yield {type:'usage',usage:{inputTokens:20,outputTokens:7,totalTokens:27}};
      yield {type:'finish',reason:{kind:'stop'}};
      return;
    }
    if (this.config.phase0_reads) {
      const reads=this.config.phase0_reads;
      if (!Array.isArray(reads) || reads.length<1 || reads.length>3 || reads.some(r=>!/^input(?:[23])?\.txt$/.test(r.path) || typeof r.marker!=='string')) throw new Error('invalid Phase 0 fixture');
      const results=options.messages.flatMap(m=>m.content??[]).filter(b=>b.type==='tool-result');
      for (let i=0;i<reads.length;i++) {
        const result=results.find(b=>b.toolCallId===`phase0-read-${i}`);
        if (!result) {
          const args=JSON.stringify({file_path:reads[i].path});
          yield {type:'block-start',index:0,blockType:'tool-call'};
          yield {type:'tool-call-delta',index:0,id:`phase0-read-${i}`,name:'read',argumentsDelta:args};
          yield {type:'block-end',index:0,block:{type:'tool-call',id:`phase0-read-${i}`,name:'read',arguments:args}};
          yield {type:'usage',usage:{inputTokens:10,outputTokens:5,totalTokens:15}};
          yield {type:'finish',reason:{kind:'tool-calls'}};
          return;
        }
        if (result.isError || !JSON.stringify(result).includes(reads[i].marker)) throw new Error('Phase 0 read failed');
      }
      if(this.config.phase5_merge_context && (!JSON.stringify(options.messages).includes(this.config.phase5_merge_context[0]) || !JSON.stringify(options.messages).includes('artifact_hash'))) throw new Error('Missing authorized ablation Context');
      const text='# Phase 0 fixture\n\n'+(this.config.phase5_merge_context ?? reads.map(r=>r.marker)).join('\n')+'\n';
      yield {type:'block-start',index:0,blockType:'text'};
      yield {type:'text-delta',index:0,text};
      yield {type:'block-end',index:0,block:{type:'text',text}};
      yield {type:'usage',usage:{inputTokens:20,outputTokens:7,totalTokens:27}};
      yield {type:'finish',reason:{kind:'stop'}};
      return;
    }
    if(this.config.phase4_document) {
      const user=options.messages.filter(m=>m.role==='user' && !(m.content??[]).some(b=>b.type==='tool-result')).at(-1);
      if(!user) throw new Error('Phase4 fixture requires original user message');
      const id='control-read-'+createHash('sha256').update(JSON.stringify(user)).digest('hex').slice(0,16);
      const result=options.messages.flatMap(m=>m.content??[]).find(b=>b.type==='tool-result' && b.toolCallId===id);
      if(!result) {
        const args=JSON.stringify({file_path:'input.txt'});
        yield {type:'block-start',index:0,blockType:'tool-call'};
        yield {type:'tool-call-delta',index:0,id,name:'read',argumentsDelta:args};
        yield {type:'block-end',index:0,block:{type:'tool-call',id,name:'read',arguments:args}};
        yield {type:'usage',usage:{inputTokens:10,outputTokens:5,totalTokens:15}};
        yield {type:'finish',reason:{kind:'tool-calls'}};
      } else {
        if(result.isError) throw new Error('Original document read failed');
        const text='# Native document\n\n'+JSON.stringify(result)+'\n';
        yield {type:'block-start',index:0,blockType:'text'};
        yield {type:'text-delta',index:0,text};
        yield {type:'block-end',index:0,block:{type:'text',text}};
        yield {type:'usage',usage:{inputTokens:20,outputTokens:7,totalTokens:27}};
        yield {type:'finish',reason:{kind:'stop'}};
      }
      return;
    }
    const tool=this.config.denied_tool || this.config.test_write ? 'write' : 'read';
    const args=this.config.test_write ? {file_path:'output.txt',content:'KANE_PR3B_SAFE_FIXTURE\n'} : this.config.denied_tool ? {file_path:'input.txt',content:'FORBIDDEN'} : {file_path:'input.txt'};
    const result=options.messages.flatMap(m=>m.content??[]).find(b=>b.type==='tool-result' && b.toolCallId==='fixture-read');
    if (!result) {
      yield {type:'block-start',index:0,blockType:'tool-call'};
      yield {type:'tool-call-delta',index:0,id:'fixture-read',name:tool,argumentsDelta:JSON.stringify(args)};
      yield {type:'block-end',index:0,block:{type:'tool-call',id:'fixture-read',name:tool,arguments:JSON.stringify(args)}};
      if(this.config.mode!=='missing_usage') yield {type:'usage',usage:{inputTokens:this.config.mode==='overrun'?40:10,outputTokens:5,totalTokens:this.config.mode==='overrun'?45:15}};
      yield {type:'finish',reason:{kind:'tool-calls'}};
    } else {
      if (result.isError || (!this.config.test_write && !JSON.stringify(result).includes('KANE_PR2_FIXTURE'))) throw new Error('fixture read failed');
      yield {type:'block-start',index:0,blockType:'text'};
      yield {type:'text-delta',index:0,text:'# Verified fixture\n\nKANE_PR2_FIXTURE\n'};
      yield {type:'block-end',index:0,block:{type:'text',text:'# Verified fixture\n\nKANE_PR2_FIXTURE\n'}};
      yield {type:'usage',usage:{inputTokens:20,outputTokens:7,totalTokens:27}};
      yield {type:'finish',reason:{kind:'stop'}};
    }
  }
}
export function apply(ctx,config={}) {
  ctx.llm.registerAdapter(['kane-fixture'],new FixtureModel(config));
  // Test evidence of entry into ORIGINAL tool dispatch after the Core permit.
  ctx.on('tools/execute',async(exec,next)=>{
    if(config.require_approval) {
      const decision=await ctx.get('approval').request({agent:exec.agent,toolName:exec.name,callId:exec.callId,signal:exec.signal,reason:'Disposable Phase 1 read fixture'});
      if(decision!=='allowed-once') throw new Error('Phase 1 fixture approval rejected');
    }
    if(config.tool_invocation_log) appendFileSync(config.tool_invocation_log,JSON.stringify({call_id:exec.callId,tool:exec.name})+'\n');
    return next();
  },{global:true});
  // Only exact isolated fixtures; never enable arbitrary production tools.

  const readPaths=config.phase0_reads ? config.phase0_reads.map(r=>r.path).filter(p=>/^input(?:[23])?\.txt$/.test(p)) : ['input.txt'];
  ctx.tools.guard(exec => (config.test_write && exec.name==='write' && resolve(exec.arguments?.file_path??'')===resolve('output.txt') && exec.arguments?.content==='KANE_PR3B_SAFE_FIXTURE\n') || exec.name==='read' && readPaths.some(p=>resolve(exec.arguments?.file_path??'')===resolve(p))
    ? undefined : 'PR2_TEST_TOOL_DENIED');
}
