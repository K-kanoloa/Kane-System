// Exercise the existing provider's real contract, not a replacement clamp/model.
import assert from 'node:assert/strict';
import { buildBaseOptions } from '../../node_modules/@earendil-works/pi-ai/dist/api/simple-options.js';
const context={systemPrompt:'Echo exactly.',messages:[{role:'user',content:[{type:'text',text:'HELLO'}],timestamp:0}]};
const old=buildBaseOptions({contextWindow:4096,maxTokens:32},context,{maxTokens:32});
const corrected=buildBaseOptions({contextWindow:8192,maxTokens:32},context,{maxTokens:32});
assert.equal(old.maxTokens,1);
assert.equal(corrected.maxTokens,32);
console.log(JSON.stringify({status:'PASS',scope:'actual provider configuration regression, no model quality or OS acceptance claim',old_context:4096,old_actual_max_tokens:old.maxTokens,corrected_context:8192,corrected_actual_max_tokens:corrected.maxTokens,model_and_gateway_max_output_unchanged:32}));
