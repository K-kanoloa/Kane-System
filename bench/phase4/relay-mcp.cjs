// Test-only MCP stdio adapter: one structured exchange avoids Windows command
// length limits. All forwarded payloads remain opaque TLS ciphertext.
const fs=require('fs'),readline=require('readline'),path=require('path');
const schema={type:'object',properties:{frames:{type:'array',items:{type:'object'}}},required:['frames']};
const send=(id,result)=>process.stdout.write(JSON.stringify({jsonrpc:'2.0',id,result})+'\n');
readline.createInterface({input:process.stdin}).on('line',line=>{
  const m=JSON.parse(line);if(m.id===undefined)return;
  if(m.method==='initialize')return send(m.id,{protocolVersion:m.params.protocolVersion,capabilities:{tools:{}},serverInfo:{name:'kane-private-tls-test-relay',version:'1.0'}});
  if(m.method==='tools/list')return send(m.id,{tools:[{name:'exchange',description:'Exchange opaque TLS frames with the authorized test relay',inputSchema:schema}]});
  if(m.method==='ping')return send(m.id,{});
  if(m.method==='tools/call'){
    try {
      if(m.params.name!=='exchange')throw Error('unsupported tool');
      const frames=m.params.arguments.frames;if(!Array.isArray(frames))throw Error('frames required');
      const input=path.resolve('in.json');if(frames.length){if(fs.existsSync(input))throw Error('prior frames not consumed');fs.writeFileSync(input+'.tmp',JSON.stringify(frames));fs.renameSync(input+'.tmp',input);}
      let output=[];if(fs.existsSync('out.json')){output=JSON.parse(fs.readFileSync('out.json'));fs.unlinkSync('out.json');}
      return send(m.id,{content:[{type:'text',text:JSON.stringify({frames:output,browser_finished:fs.existsSync('browser-finished.json')})}]});
    }catch(e){return send(m.id,{isError:true,content:[{type:'text',text:String(e)}]});}
  }
  process.stdout.write(JSON.stringify({jsonrpc:'2.0',id:m.id,error:{code:-32601,message:'unsupported method'}})+'\n');
});
