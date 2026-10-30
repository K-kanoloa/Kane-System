// Test transport only. Relays opaque TLS bytes through the already authorized
// Secure MCP Tunnel; no HTTP termination, credentials, TLS/auth/epoch bypass.
const fs=require('fs'),net=require('net'),path=require('path');
const dir=process.argv[3];
if(process.argv[2]==='serve'){
  const sockets=new Map();let id=0,index=0;const queued=[];
  const out=path.join(dir,'out.json'),input=path.join(dir,'in.json');
  function flush(){if(!queued.length||fs.existsSync(out))return;let n=0,batch=[];while(queued.length&&n<48000){const item=queued.shift();batch.push(item);n+=(item.data?.length||0);}fs.writeFileSync(out+'.tmp',JSON.stringify(batch));fs.renameSync(out+'.tmp',out);}
  const server=net.createServer(s=>{const key=String(++id);sockets.set(key,s);queued.push({id:key,open:true});s.on('data',b=>{for(let j=0;j<b.length;j+=12000)queued.push({id:key,data:b.subarray(j,j+12000).toString('base64')});});s.on('close',()=>{queued.push({id:key,end:true});sockets.delete(key)});s.on('error',()=>{});});
  server.listen(9443,'127.0.0.1',()=>fs.writeFileSync(path.join(dir,'listening.json'),JSON.stringify({pid:process.pid,address:'127.0.0.1',port:9443,transport:'opaque end-to-end TLS relay'})));
  setInterval(()=>{if(fs.existsSync(input)){const frames=JSON.parse(fs.readFileSync(input));fs.unlinkSync(input);for(const f of frames){const s=sockets.get(f.id);if(s){if(f.data)s.write(Buffer.from(f.data,'base64'));if(f.end)s.end();}}}flush();},20);
}else if(process.argv[2]==='exchange'){
  const incoming=JSON.parse(process.argv[4]?.startsWith('@')?fs.readFileSync(process.argv[4].slice(1),'utf8'):Buffer.from(process.argv[4]||'W10=','base64').toString());
  if(incoming.length){const p=path.join(dir,'in.json');if(fs.existsSync(p))throw Error('prior input not consumed');fs.writeFileSync(p+'.tmp',JSON.stringify(incoming));fs.renameSync(p+'.tmp',p);}
  const p=path.join(dir,'out.json');let result=[];if(fs.existsSync(p)){result=JSON.parse(fs.readFileSync(p));fs.unlinkSync(p);}console.log(JSON.stringify({frames:result,browser_finished:fs.existsSync(path.join(dir,'browser-finished.json'))}));
}
