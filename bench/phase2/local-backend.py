#!/usr/bin/env python3
"""Deterministic OpenAI wire fixture, never a real model quality/billing result."""
import argparse,json,time,subprocess
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
p=argparse.ArgumentParser();p.add_argument('--port',type=int,required=True);p.add_argument('--model',default='phase2-mock');p.add_argument('--delay-ms',type=int,default=20);a=p.parse_args()
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_GET(self):
  data={'status':'ok'} if self.path=='/health' else {'data':[{'id':a.model}]}
  raw=json.dumps(data).encode();self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
 def do_POST(self):
  body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
  if body.get('cache_prompt') is not False:self.send_error(400);return
  text=json.dumps(body.get('messages',[]));time.sleep(a.delay_ms/1000)
  if 'PHASE2_HTTP_ERROR' in text:self.send_error(503);return
  # Reuse the existing Rust DeterministicMock; this file only adapts OpenAI wire
  # framing and injects transport faults. The fixed input is a public fixture.
  mock={'task_id':body.get('user','phase2-wire-fixture'),'input':'LOCAL_MOCK_RESPONSE a b c d e f g h i','max_tokens':32}
  result=json.loads(subprocess.check_output([str(Path(__file__).resolve().parents[2]/'target/debug/kanesys'),'mock-infer'],input=json.dumps(mock).encode()))
  assert result['tokens']==14
  self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
  for chunk in [
   {'choices':[{'index':0,'delta':{'role':'assistant','content':result['content']},'finish_reason':None}]},
   {'choices':[{'index':0,'delta':{},'finish_reason':'stop'}]},
   {'choices':[],'usage':{'prompt_tokens':10,'completion_tokens':4,'total_tokens':13 if 'PHASE2_BAD_USAGE' in text else 14}},
  ]:
   self.wfile.write(('data: '+json.dumps({'id':'phase2-mock','object':'chat.completion.chunk','model':a.model,**chunk})+'\n\n').encode());self.wfile.flush()
   if 'PHASE2_TRUNCATE' in text:return
  self.wfile.write(b'data: [DONE]\n\n');self.wfile.flush()
ThreadingHTTPServer(('127.0.0.1',a.port),Handler).serve_forever()
