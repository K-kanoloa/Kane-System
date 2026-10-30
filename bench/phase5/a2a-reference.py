#!/usr/bin/env python3
"""Authenticated HTTPS A2A wire reference peer; no reasoning Agent/model.
No arbitrary network, paths or tools. No production-peer compatibility claim.
"""
import argparse,json,ssl
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
p=argparse.ArgumentParser();p.add_argument('--port',type=int,required=True);p.add_argument('--cert',required=True);p.add_argument('--key',required=True);p.add_argument('--lose-result',action='store_true');a=p.parse_args()
origin=f'https://localhost:{a.port}'
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def permitted(self):
  if self.headers.get('Authorization')!='Bearer ephemeral-public-reference':self.send_error(403);return False
  return True
 def reply(self,value):
  raw=json.dumps(value).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
 def do_GET(self):
  if not self.permitted():return
  if self.path!='/.well-known/agent-card.json':self.send_error(404);return
  self.reply({'protocolVersion':'0.3.0','preferredTransport':'JSONRPC','name':'Kane public reference peer','description':'Fixed public test marker','url':origin+'/a2a','version':'1','capabilities':{'streaming':False,'pushNotifications':False,'stateTransitionHistory':False},'defaultInputModes':['text/plain'],'defaultOutputModes':['text/plain'],'skills':[{'id':'public-marker','name':'Public marker','description':'Fixed public data only','tags':['public','test']}],'securitySchemes':{'owner':{'type':'http','scheme':'bearer'}},'security':[{'owner':[]}]})
 def do_POST(self):
  if not self.permitted():return
  req=json.loads(self.rfile.read(int(self.headers.get('Content-Length',0))))
  if self.path!='/a2a' or req['method'] not in {'message/send','tasks/get','tasks/cancel'}:self.send_error(400);return
  if a.lose_result:self.close_connection=True;return
  state='canceled' if req['method']=='tasks/cancel' else 'completed'
  self.reply({'jsonrpc':'2.0','id':req['id'],'result':{'kind':'task','id':'public-reference-task','contextId':'public-reference-context','status':{'state':state},'artifacts':[{'artifactId':'public-reference-artifact','parts':[{'kind':'text','text':'KANE_PUBLIC_A2A_MARKER'}]}]}})
server=ThreadingHTTPServer(('127.0.0.1',a.port),Handler);context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(a.cert,a.key);server.socket=context.wrap_socket(server.socket,server_side=True);server.serve_forever()
