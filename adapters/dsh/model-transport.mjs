// Thin transport only: original DSH/pi-ai builds HTTP and parses SSE unchanged.
import { AsyncLocalStorage } from 'node:async_hooks';
import { Agent, fetch as wireFetch } from 'undici';
const scope=new AsyncLocalStorage();
let configured=null;
export function configureGateway(socket) {
  if(configured && configured!==socket) throw new Error('Conflicting model gateway');
  if(configured) return;
  configured=socket;
  const original=globalThis.fetch;
  const dispatcher=new Agent({connect:{socketPath:socket}});
  globalThis.fetch=async(input,init)=>{
    const request=new Request(input,init);
    const url=new URL(request.url);
    const token=scope.getStore();
    if(url.origin==='http://kane-model.invalid') {
      if(url.pathname!=='/v1/chat/completions' || request.method!=='POST' || !token) throw new Error('Gateway model request without Core permit');
      const headers=new Headers(request.headers);
      headers.set('Authorization',`Bearer ${token}`);
      return wireFetch(request.url,{dispatcher,headers:Object.fromEntries(headers),method:request.method,body:await request.arrayBuffer(),signal:request.signal});
    }
    if(token) throw new Error('Managed model cannot fall back to another endpoint');
    return original(request);
  };
}
export async function* gatewayStream(token,source) {
  const iterator=scope.run(token,()=>source()[Symbol.asyncIterator]());
  try {
    while(true) {
      const step=await scope.run(token,()=>iterator.next());
      if(step.done) return;
      yield step.value;
    }
  } finally {if(iterator.return) await scope.run(token,()=>iterator.return());}
}
