"""A2A 0.3 JSON-RPC reference transport, independent of the internal Task DAG.

Only Owner-registered local HTTPS peers, exact Capability grants, no retries.
Remote/billable peers remain unavailable until their data/usage policies can be
verified. The remote 'completed' state never verifies a Kane System Task.
"""
import hashlib,json,ssl
from urllib.parse import urlparse
import httpx
from ..system.client import CoreRejected

class A2AReference:
    def __init__(self, origin, ca_file, token):
        url=urlparse(origin)
        if url.scheme!='https' or url.hostname not in {'localhost','127.0.0.1','::1'} or url.username or url.password or url.path not in {'','/'} or url.query or url.fragment or not token:
            raise ValueError('Owner-authorized local HTTPS origin and credential required')
        self.origin=origin.rstrip('/')
        self.client=httpx.AsyncClient(verify=ssl.create_default_context(cafile=ca_file),
            headers={'Authorization':'Bearer '+token},timeout=5,follow_redirects=False,trust_env=False)
        self.card=None;self.counter=0
        self.server_sha256=hashlib.sha256((self.origin+':A2A-0.3-local-reference').encode()).hexdigest()

    async def discover(self):
        response=await self.client.get(self.origin+'/.well-known/agent-card.json')
        response.raise_for_status();card=response.json()
        if card.get('protocolVersion')!='0.3.0' or card.get('preferredTransport')!='JSONRPC' or card.get('url')!=self.origin+'/a2a' or not card.get('skills'):
            raise CoreRejected('Unsupported A2A version, transport, capability or redirect')
        self.card=card
        return {'card':card,'verified_transport':'HTTPS/JSONRPC reference','steer':'unsupported','resume':'unsupported','checkpoint':'unsupported','task_semantic_recovery':'unsupported'}

    async def call(self,core,turn,native,key,method,params):
        if self.card is None or method not in {'message/send','tasks/get','tasks/cancel'}:
            raise CoreRejected('Undiscovered or unsupported A2A method; no silent fallback')
        action=await core.request('external_prepare',turn_id=turn,native_session_ref=native,key=key,
            protocol='a2a',server_sha256=self.server_sha256,tool=method,arguments=params)
        if action['state']!='Pending':raise CoreRejected('Inspect A2A outcome; never replay')
        await core.request('external_claim',id=action['id'])
        try:
            self.counter+=1
            response=await self.client.post(self.origin+'/a2a',json={'jsonrpc':'2.0','id':self.counter,'method':method,'params':params})
            response.raise_for_status();data=response.json()
            if data.get('id')!=self.counter or data.get('jsonrpc')!='2.0' or 'error' in data or 'result' not in data:
                raise CoreRejected('Uncertain A2A response; reconciliation required')
            result=data['result']
            await core.request('external_result',id=action['id'],evidence=json.dumps({'peer':self.server_sha256,'method':method,'result_sha256':hashlib.sha256(json.dumps(result,sort_keys=True).encode()).hexdigest(),'task_success':'not independently verified'}))
            return result
        except BaseException:
            import asyncio
            try:await asyncio.shield(core.request('external_unknown',id=action['id']))
            except Exception:pass
            raise

    async def close(self):await self.client.aclose()
