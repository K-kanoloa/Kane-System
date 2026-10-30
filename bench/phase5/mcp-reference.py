#!/usr/bin/env python3
"""Disposable MCP protocol peer, not an Agent or replacement Kanaloa tool.
One deterministic read-only public resource; no arbitrary paths/network/code.
"""
import json,sys,time
for line in sys.stdin:
    req=json.loads(line);method=req.get('method')
    if 'id' not in req:continue
    if method=='initialize':result={'protocolVersion':'2025-06-18','capabilities':{'tools':{}},'serverInfo':{'name':'kane-public-reference','version':'1'}}
    elif method=='tools/list':result={'tools':[{'name':'public_marker','description':'Read fixed public test data','inputSchema':{'type':'object','properties':{'resource':{'const':'public-marker'}},'required':['resource'],'additionalProperties':False}}]}
    elif method=='tools/call':
        if req['params']!={'name':'public_marker','arguments':{'resource':'public-marker'}}:
            print(json.dumps({'jsonrpc':'2.0','id':req['id'],'error':{'code':-32602,'message':'scope'}}),flush=True);continue
        if '--lose-result' in sys.argv:sys.exit(0)
        result={'content':[{'type':'text','text':'KANE_PUBLIC_MCP_MARKER'}],'isError':False}
    else:
        print(json.dumps({'jsonrpc':'2.0','id':req['id'],'error':{'code':-32601,'message':'unsupported'}}),flush=True);continue
    print(json.dumps({'jsonrpc':'2.0','id':req['id'],'result':result}),flush=True)
