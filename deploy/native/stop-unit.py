#!/usr/bin/python3
"""Graceful stop through original Supervisor/Model Runtime IPC; no task replay."""
import json,re,socket,sys
identity=sys.argv[2]
if not re.fullmatch('[A-Za-z0-9-]{1,100}',identity):raise SystemExit('invalid managed identity')
kind=sys.argv[1];assert kind in ['agent','model']
path='/run/kane-system/'+identity+('.runtime.sock' if kind=='agent' else '.model.sock')
with socket.socket(socket.AF_UNIX) as s:
 s.settimeout(25);s.connect(path);s.sendall((json.dumps({'op':'stop'})+'\n').encode() if kind=='agent' else b'stop\n')
 data=s.recv(16384)
 if not data:raise SystemExit('stop acknowledgement missing; inspect Core before retry')
 result=json.loads(data)
 if result.get('ok') is False:raise SystemExit('stop rejected; retain unresolved state')
print('Managed stop acknowledged; Task completion is independent')
