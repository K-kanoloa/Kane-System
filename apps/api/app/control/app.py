"""HTTPS/WSS-only Owner gateway. No generic Owner IPC or legacy API proxy.

Passkey signature verification runs here in the trusted Host process. Rust owns
challenge consumption, session/device validity, lifecycle/leases and real writes.
"""
import asyncio
import base64
from collections import defaultdict,deque
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import secrets
import time
from urllib.parse import urlsplit, quote

import httpx
from fastapi import FastAPI,HTTPException,Request,Response,WebSocket,WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel,ConfigDict,Field
from starlette.middleware.base import BaseHTTPMiddleware
import webauthn
from webauthn.helpers import bytes_to_base64url,base64url_to_bytes
from webauthn.helpers.structs import AuthenticatorSelectionCriteria,ResidentKeyRequirement,UserVerificationRequirement,PublicKeyCredentialDescriptor

from ..system.client import SystemClient,CoreRejected,CoreUnavailable
from ..main import lifespan as original_lifespan
from ..routes.system import dispatch_existing
from .runtime import ControlRuntime

SESSION='__Host-kane-session'
DEVICE='__Host-kane-device'
class Body(BaseModel):model_config=ConfigDict(extra='forbid')
class Pair(Body):pairing_code:str=Field(min_length=64,max_length=64);label:str=Field(min_length=1,max_length=100)
class Credential(Body):challenge_id:str=Field(min_length=1,max_length=100);credential:dict
class Version(Body):epoch:int=Field(ge=0);version:int=Field(ge=0)
class Edit(Version):request_id:str=Field(min_length=1,max_length=100);path:str=Field(min_length=1,max_length=200);content:str=Field(max_length=65536);base_hash:str|None=None
class Handoff(Version):read_path:str=Field(min_length=1,max_length=200);message_id:str=Field(min_length=1,max_length=100);content:str=Field(min_length=1,max_length=100000)
class Prompt(Body):content:str=Field(min_length=1,max_length=100000)
class Verify(Body):version:int=Field(ge=0);passed:bool;evidence:str=Field(min_length=1,max_length=10000)
class Approve(Body):request_id:str=Field(min_length=1,max_length=100);decision:str=Field(pattern='^(allow-once|reject-once|cancelled)$')
class Reconcile(Body):outcome:str=Field(pattern='^(Confirmed|NotExecuted)$')

async def core(request,op,**body):
    try:return await request.app.state.control_core.request(op,**body)
    except CoreRejected as e:raise HTTPException(e.status,str(e)) from e
    except (CoreUnavailable,OSError,TimeoutError) as e:raise HTTPException(503,'control_authority_unavailable') from e

def token(request):
    value=request.cookies.get(SESSION,'')
    if len(value)!=64:raise HTTPException(401,'Owner_session_required')
    return value
async def authenticated(request):
    value=token(request)
    try:await core(request,'control_auth',action='session',body={'session_token':value})
    except HTTPException as e:
        if e.status_code==403:raise HTTPException(401,'Owner_session_expired_or_revoked') from e
        raise
    return value

def cookie_response(result):
    response=JSONResponse({k:v for k,v in result.items() if not k.endswith('_token')})
    if 'session_token' in result:response.set_cookie(SESSION,result['session_token'],max_age=max(0,result['expires_at']-int(time.time())),httponly=True,secure=True,samesite='strict',path='/')
    if 'device_token' in result:response.set_cookie(DEVICE,result['device_token'],max_age=90*86400,httponly=True,secure=True,samesite='strict',path='/')
    return response

class Boundary(BaseHTTPMiddleware):
    async def dispatch(self,request,call_next):
        origin=request.app.state.control_origin;authority=urlsplit(origin).netloc
        if request.url.scheme!='https':return JSONResponse({'detail':'HTTPS_required'},426)
        if request.headers.get('host','').lower()!=authority or any(k in request.headers for k in ['forwarded','x-forwarded-proto','x-forwarded-host']):return JSONResponse({'detail':'fixed_control_origin_required'},421)
        sent=request.headers.get('origin')
        if sent is not None and sent!=origin:return JSONResponse({'detail':'cross_origin_denied'},403)
        if request.method not in {'GET','HEAD','OPTIONS'}:
            if sent!=origin or request.headers.get('x-kane-intent')!='owner-action' or not request.headers.get('content-type','').startswith('application/json'):return JSONResponse({'detail':'Owner_origin_and_CSRF_header_required'},403)
        try:content_length=int(request.headers.get('content-length','0'))
        except ValueError:return JSONResponse({'detail':'invalid_content_length'},400)
        if content_length<0:return JSONResponse({'detail':'invalid_content_length'},400)
        if content_length>2_000_000:return JSONResponse({'detail':'request_too_large'},413)
        key=(request.client.host if request.client else 'local',request.url.path.startswith('/control-api/auth'))
        if key[1]:
            rates=request.app.state.control_rates
            if key not in rates and len(rates)>=4096:rates.pop(next(iter(rates)))
            queue=rates[key];now=time.monotonic()
            while queue and queue[0]<now-60:queue.popleft()
            if len(queue)>=60:return JSONResponse({'detail':'authentication_rate_limit'},429)
            queue.append(now)
        # Read a bounded body once before parsing; Starlette's cached request
        # replays those exact bytes to FastAPI, including chunked requests.
        if request.method not in {'GET','HEAD','OPTIONS'}:
            body=bytearray()
            async for chunk in request.stream():
                if len(body)+len(chunk)>2_000_000:return JSONResponse({'detail':'request_too_large'},413)
                body.extend(chunk)
            request._body=bytes(body)
        response=await call_next(request)
        response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='no-referrer'
        response.headers['X-Frame-Options']='DENY'
        response.headers['Permissions-Policy']='camera=(), microphone=(), geolocation=()'
        response.headers['Strict-Transport-Security']='max-age=31536000'
        return response

def create_control_app():
    client=SystemClient()
    if not client.socket:raise RuntimeError('Control gateway requires private Core socket; no direct DB fallback')
    configured=os.environ.get('KANE_CONTROL_ORIGIN','').rstrip('/')
    if not configured.startswith('https://'):raise RuntimeError('Explicit fixed HTTPS Origin required')
    @asynccontextmanager
    async def lifespan(app):
        info=await app.state.control_core.request('control_auth',action='info',body={})
        if not info.get('initialized') or info['origin']!=configured:raise RuntimeError('Initialize/match local Owner via private Host IPC before serving')
        app.state.control_info=info
        async with original_lifespan(app):
            app.state.control_runtime=ControlRuntime(app)
            try:yield
            finally:
                # Original lifespan closes native adapters; explicit shutdown here
                # stops owned Supervisors before waiting on their child processes.
                for adapter in list(app.state.coordinator._adapters.values()):
                    if getattr(adapter,'process_mode',None)=='supervised':await adapter.close()
                await app.state.control_runtime.close()
    app=FastAPI(title='Kane System Owner control',lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.state.control_core=client;app.state.control_origin=configured;app.state.control_rates=defaultdict(deque)
    app.add_middleware(Boundary)

    @app.get('/control-api/health')
    async def health():return {'service':'Kane System Owner control','transport':'HTTPS/WSS','system_complete':False}
    @app.get('/control-api/info')
    async def info(request:Request):return await core(request,'control_auth',action='info',body={})
    @app.post('/control-api/auth/pair')
    async def pair(request:Request,body:Pair):return cookie_response(await core(request,'control_auth',action='pair',body=body.model_dump()))
    @app.get('/control-api/session')
    async def session(request:Request):return await core(request,'control_auth',action='session',body={'session_token':await authenticated(request)})
    @app.post('/control-api/auth/pair-code')
    async def pair_code(request:Request):return await core(request,'control_auth',action='pair_code',body={'session_token':await authenticated(request)})
    @app.post('/control-api/auth/logout')
    async def logout(request:Request):
        result=await core(request,'control_auth',action='logout',body={'session_token':await authenticated(request)})
        response=JSONResponse(result);response.delete_cookie(SESSION,path='/',secure=True,httponly=True,samesite='strict');return response
    @app.get('/control-api/devices')
    async def devices(request:Request):return await core(request,'control_auth',action='devices',body={'session_token':await authenticated(request)})
    @app.post('/control-api/devices/{device_id}/revoke')
    async def revoke(request:Request,device_id:str):return await core(request,'control_auth',action='revoke_device',body={'session_token':await authenticated(request),'device_id':device_id})
    @app.get('/control-api/credentials')
    async def credentials(request:Request):return await core(request,'control_auth',action='credentials',body={'session_token':await authenticated(request)})
    @app.post('/control-api/credentials/{credential_id}/revoke')
    async def revoke_credential(request:Request,credential_id:str):return await core(request,'control_auth',action='revoke_credential',body={'session_token':await authenticated(request),'credential_id':credential_id})

    async def challenge(request,id,purpose):
        c=await core(request,'control_challenge',id=id)
        if c['purpose']!=purpose or c['used'] or c['expires_at']<=int(time.time()):raise HTTPException(403,'Passkey_challenge_expired_or_replayed')
        c['id']=id
        return c
    @app.post('/control-api/auth/passkey/register/options')
    async def register_options(request:Request):
        t=await authenticated(request);c=await core(request,'control_auth',action='begin_register',body={'session_token':t});info=request.app.state.control_info
        options=webauthn.generate_registration_options(rp_id=info['rp_id'],rp_name='Kane System',user_id=info['owner_id'].encode(),user_name='Local Owner',challenge=c['challenge'].encode(),authenticator_selection=AuthenticatorSelectionCriteria(resident_key=ResidentKeyRequirement.REQUIRED,user_verification=UserVerificationRequirement.REQUIRED))
        return {'challenge_id':c['id'],'publicKey':json.loads(webauthn.options_to_json(options))}
    @app.post('/control-api/auth/passkey/register/finish')
    async def register_finish(request:Request,body:Credential):
        t=await authenticated(request);c=await challenge(request,body.challenge_id,'register');info=request.app.state.control_info
        current=await core(request,'control_auth',action='session',body={'session_token':t})
        if c['session_id']!=current['session_id']:raise HTTPException(403,'Passkey_session_mismatch')
        try:verified=webauthn.verify_registration_response(credential=body.credential,expected_challenge=c['challenge'].encode(),expected_rp_id=info['rp_id'],expected_origin=info['origin'],require_user_verification=True)
        except Exception as e:raise HTTPException(403,'Passkey_registration_invalid') from e
        return await core(request,'control_auth',action='finish_register',body={'session_token':t,'challenge_id':c['id'],'challenge':c['challenge'],'credential_id':bytes_to_base64url(verified.credential_id),'key':{'public_key':bytes_to_base64url(verified.credential_public_key)},'sign_count':verified.sign_count})
    @app.post('/control-api/auth/passkey/login/options')
    async def login_options(request:Request):
        key=request.cookies.get(DEVICE,'');c=await core(request,'control_auth',action='begin_login',body={'device_token':key});info=request.app.state.control_info
        options=webauthn.generate_authentication_options(rp_id=info['rp_id'],challenge=c['challenge'].encode(),allow_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(v['id'])) for v in c['credentials']],user_verification=UserVerificationRequirement.REQUIRED)
        return {'challenge_id':c['id'],'publicKey':json.loads(webauthn.options_to_json(options))}
    @app.post('/control-api/auth/passkey/login/finish')
    async def login_finish(request:Request,body:Credential):
        c=await challenge(request,body.challenge_id,'login');info=request.app.state.control_info;credentials=await core(request,'control_credentials')
        known=next((v for v in credentials if v['id']==body.credential.get('id')),None)
        if not known:raise HTTPException(403,'Passkey_credential_not_found')
        try:verified=webauthn.verify_authentication_response(credential=body.credential,expected_challenge=c['challenge'].encode(),expected_rp_id=info['rp_id'],expected_origin=info['origin'],credential_public_key=base64url_to_bytes(known['key']['public_key']),credential_current_sign_count=known['sign_count'],require_user_verification=True)
        except Exception as e:raise HTTPException(403,'Passkey_assertion_invalid') from e
        result=await core(request,'control_auth',action='finish_login',body={'device_token':request.cookies.get(DEVICE,''),'challenge_id':c['id'],'challenge':c['challenge'],'credential_id':known['id'],'old_count':known['sign_count'],'new_count':verified.new_sign_count})
        return cookie_response(result)

    async def snapshot(request,token_value,cursor=0):
        result=await core(request,'control_view',token=token_value,cursor=cursor)
        store=request.app.state.store;conversations=[];messages=[];turns=[];permissions=[]
        for binding in result['bindings']:
            turn=store.get_turn(binding['turn_id'])
            if turn is None:continue
            turns.append(turn.model_dump());conversation=store.get_conversation(turn.conversation_id)
            if conversation and conversation.conversation_id not in {c['conversation_id'] for c in conversations}:conversations.append(conversation.model_dump())
            messages.extend(m.model_dump() for m in store.get_messages(turn.conversation_id) if m.turn_id==turn.turn_id)
            try:adapter,_,_=await request.app.state.control_runtime.adapter(binding['task_id'])
            except ValueError:continue  # Existing unsupported Harness remains visible/read-only.
            permissions.extend({'task_id':binding['task_id'],'request_id':str(p.request_id),'title':str(p.tool_call.get('title','工具需要批准')),'session_id':p.session_id} for p in adapter.list_pending_permissions(turn.native_session_ref))
        result.update(conversations=conversations,turns=turns,messages=messages,permissions=permissions)
        return result
    @app.get('/control-api/snapshot')
    async def state(request:Request,cursor:int=0):return await snapshot(request,await authenticated(request),max(0,cursor))
    @app.get('/control-api/tasks/{task}/files/{filename}')
    async def read_file(request:Request,task:str,filename:str):return await core(request,'control_read',token=await authenticated(request),id=task,path=filename)
    @app.get('/control-api/tasks/{task}/download/{filename}')
    async def download(request:Request,task:str,filename:str):
        result=await core(request,'control_read',token=await authenticated(request),id=task,path=filename)
        return Response(result['content'],media_type='text/plain;charset=utf-8',headers={'Content-Disposition':"attachment; filename*=UTF-8''"+quote(filename,safe='')})
    @app.post('/control-api/tasks/{task}/takeover')
    async def takeover(request:Request,task:str,body:Version):
        t=await authenticated(request);start=await core(request,'control_takeover',token=t,id=task,**body.model_dump(),finish=False)
        if start['phase']=='Human':return start
        if start['unresolved']:return JSONResponse({**start,'reason':'await_current_actions_or_explicit_reconciliation'},409)
        try:await request.app.state.control_runtime.pause(task)
        except (ValueError,RuntimeError,TimeoutError) as e:raise HTTPException(409,'runtime_stop_not_confirmed_lease_not_transferred') from e
        return await core(request,'control_takeover',token=t,id=task,**body.model_dump(),finish=True)
    @app.post('/control-api/tasks/{task}/renew')
    async def renew(request:Request,task:str,body:Version):return await core(request,'control_renew',token=await authenticated(request),id=task,**body.model_dump())
    @app.post('/control-api/tasks/{task}/save')
    async def save(request:Request,task:str,body:Edit):return await core(request,'control_edit',token=await authenticated(request),id=task,body=body.model_dump())
    @app.get('/control-api/edits/{edit}')
    async def edit_status(request:Request,edit:str):return await core(request,'control_edit_status',token=await authenticated(request),id=edit)
    @app.post('/control-api/edits/{edit}/reconcile')
    async def reconcile(request:Request,edit:str,body:Reconcile):return await core(request,'control_reconcile_edit' if body.outcome=='Confirmed' else 'control_edit_not_executed',token=await authenticated(request),id=edit)
    @app.post('/control-api/tasks/{task}/handoff')
    async def handoff(request:Request,task:str,body:Handoff):
        t=await authenticated(request);await core(request,'control_handoff',token=t,id=task,body=body.model_dump())
        try:return await request.app.state.control_runtime.handoff(task,body.model_dump(),t)
        except (ValueError,RuntimeError,TimeoutError,CoreRejected,CoreUnavailable) as e:raise HTTPException(409,'handoff_not_confirmed_inspect_state_no_automatic_prompt_replay') from e
    @app.post('/control-api/tasks/{task}/dispatch')
    async def dispatch(request:Request,task:str,body:Prompt):
        t=await authenticated(request);adapter,_,_=await request.app.state.control_runtime.adapter(task);await request.app.state.control_runtime.ensure_running(adapter)
        await core(request,'control_dispatch_authorize',token=t,id=task,content=body.content)
        agent_ref=next(key for key,value in request.app.state.coordinator._adapters.items() if value is adapter)
        return await dispatch_existing(task,body.content,adapter,agent_ref,request.app.state.store,request.app.state.control_core)
    @app.post('/control-api/tasks/{task}/verify')
    async def verify(request:Request,task:str,body:Verify):return await core(request,'control_verify',token=await authenticated(request),id=task,**body.model_dump())
    @app.post('/control-api/tasks/{task}/approval')
    async def approval(request:Request,task:str,body:Approve):
        t=await authenticated(request);adapter,binding,_=await request.app.state.control_runtime.adapter(task)
        pending=adapter.get_pending_permission(body.request_id)
        if pending is None or pending.status!='pending' or not binding or pending.session_id!=binding['native_session_ref']:raise HTTPException(409,'native_permission_not_pending')
        await core(request,'control_approval_authorize',token=t,id=task,body=body.model_dump())
        try:await adapter.respond_permission(body.request_id,body.decision,session_id=pending.session_id)
        except ValueError as e:raise HTTPException(409,'permission_result_not_confirmed') from e
        return {'decision':body.decision,'forwarded_to_original_Harness':True}

    @app.websocket('/control-api/events')
    async def events(ws:WebSocket):
        if ws.url.scheme!='wss' or ws.headers.get('origin')!=configured or ws.headers.get('host','').lower()!=urlsplit(configured).netloc:
            await ws.close(code=4403);return
        value=ws.cookies.get(SESSION,'')
        try:await authenticated(ws)
        except HTTPException:await ws.close(code=4401);return
        await ws.accept();cursor=0
        try:
            while True:
                try:
                    frame=await snapshot(ws,value,cursor);cursor=frame['cursor'];await ws.send_json({'type':'snapshot','authority':frame})
                except HTTPException as e:
                    await ws.close(code=4401 if e.status_code in (401,403) else 1013);return
                try:
                    msg=await asyncio.wait_for(ws.receive_json(),timeout=1)
                    if not isinstance(msg,dict) or set(msg)-{'cursor'} or not isinstance(msg.get('cursor',0),int) or msg.get('cursor',0)<0:
                        await ws.close(code=4400);return
                    cursor=msg.get('cursor',cursor)
                except asyncio.TimeoutError:pass
        except (WebSocketDisconnect,RuntimeError):return

    @app.api_route('/{path:path}',methods=['GET','HEAD'])
    async def frontend(request:Request,path:str):
        allowed=path in {'control','kane-control-sw.js','kane-control-icon.svg','icon.png'} or path.startswith(('control/','_next/static/'))
        if not allowed or any(part in {'.','..'} for part in path.split('/')) or '\\' in path or '%' in path:raise HTTPException(404,'control_route_not_found')
        origin=os.getenv('KANE_CONTROL_NEXT_URL','http://127.0.0.1:3000').rstrip('/');parsed=urlsplit(origin)
        if parsed.scheme!='http' or parsed.hostname not in {'127.0.0.1','localhost','::1'} or parsed.username or parsed.password or parsed.path or parsed.query:raise HTTPException(503,'private_frontend_origin_required')
        nonce=base64.b64encode(secrets.token_bytes(24)).decode();csp=f"default-src 'self'; script-src 'self' 'nonce-{nonce}'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
        try:
            async with httpx.AsyncClient(trust_env=False,timeout=30) as client:
                upstream_path='kane-control-icon.svg' if path=='icon.png' else path
                upstream=await client.get(origin+'/'+upstream_path,params=request.query_params,headers={'Content-Security-Policy':csp,'x-nonce':nonce,'host':urlsplit(configured).netloc})
        except httpx.HTTPError as e:raise HTTPException(503,'frontend_unavailable') from e
        headers={k:v for k,v in upstream.headers.items() if k.lower() in {'content-type','location'}};headers['Content-Security-Policy']=csp
        if path=='kane-control-sw.js':headers['Service-Worker-Allowed']='/control'
        return Response(upstream.content,status_code=upstream.status_code,headers=headers)
    return app
