// Regular Playwright on the actual reported OS. Viewports and virtual authenticators do not prove mobile hardware.
import { chromium } from 'playwright-core';
import os from 'node:os';
const platform=os.platform();
const scope=platform==='win32'?'Windows actual Edge/Chromium':'Linux actual Chromium';
import assert from 'node:assert/strict';
import { mkdir,writeFile,readFile } from 'node:fs/promises';
const origin=process.env.KANE_CONTROL_ORIGIN;
const output=process.env.KANE_CONTROL_BROWSER_EVIDENCE;
const code=process.env.KANE_CONTROL_TEST_PAIRING;
if(!origin || !output || !code)throw Error('Explicit local test inputs required');
await mkdir(output,{recursive:true});
// Trust a test CA explicitly; never disable certificate validation. Windows uses
// its current-user certificate store; Linux uses a disposable NSS HOME.
const launch={headless:true};
if(process.env.KANE_CONTROL_BROWSER_EXECUTABLE)launch.executablePath=process.env.KANE_CONTROL_BROWSER_EXECUTABLE;
if(platform==='linux'){
  const home=process.env.KANE_CONTROL_TLS_HOME;
  if(!home)throw Error('Explicit disposable NSS HOME required for verified test TLS');
  launch.env={...process.env,HOME:home};
}
const browser=await chromium.launch(launch);
const context=await browser.newContext({viewport:{width:1440,height:1000}});
const page=await context.newPage(),checks=[],errors=[],appErrors=[],requests=[];
page.setDefaultTimeout(180000);page.setDefaultNavigationTimeout(300000);
const progress=setInterval(()=>writeFile(output+'/progress.json',JSON.stringify({checks,errors,appErrors,requests,utc:new Date().toISOString()})).catch(()=>{}),15000);progress.unref();
page.on('pageerror',e=>{errors.push(e.message);appErrors.push(e.message);});
page.on('console',m=>{if(m.type()==='error')errors.push(m.text());});
const authFinishes={};
page.on('request',r=>{const path=new URL(r.url()).pathname;if(path.endsWith('/passkey/login/finish')||path.endsWith('/passkey/register/finish'))authFinishes[path]=r.postDataJSON();});
page.on('response',r=>{if(r.status()>=400)requests.push({path:new URL(r.url()).pathname,status:r.status()});});
async function post(path,body={}) {
  return page.evaluate(async ({path,body})=>{
    const p=await fetch('/control-api'+path,{method:'POST',headers:{'Content-Type':'application/json','X-Kane-Intent':'owner-action'},body:JSON.stringify(body)});
    return {status:p.status,value:await p.json()};
  },{path,body});
}
async function snapshot(){return page.evaluate(()=>fetch('/control-api/snapshot').then(r=>r.json()));}
async function waitState(name) {
  await page.waitForFunction(state=>document.querySelector('.control-task-heading p')?.textContent?.startsWith(state),name,{timeout:90000});
}
try {
  if(process.env.KANE_CONTROL_TEST_EXPIRY_SIGNAL)await page.addInitScript(()=>{
    const Native=window.WebSocket;window.__kaneTestChannels={sockets:[],frames:[]};
    window.WebSocket=class extends Native {
      constructor(...args){super(...args);window.__kaneTestChannels.sockets.push(this);this.addEventListener('message',e=>{try{if(JSON.parse(e.data).type==='snapshot'&&window.__kaneTestChannels.frames.length<8)window.__kaneTestChannels.frames.push(e.data);}catch{}});}
    };
  });
  const cdp=await context.newCDPSession(page);
  await cdp.send('WebAuthn.enable');
  await cdp.send('WebAuthn.addVirtualAuthenticator',{options:{protocol:'ctap2',transport:'internal',hasResidentKey:true,hasUserVerification:true,isUserVerified:true,automaticPresenceSimulation:true}});
  const html=await page.goto(origin+'/control');
  assert.equal(await page.evaluate(()=>isSecureContext),true);
  const security=await html.securityDetails();
  assert(security && /TLS/.test(security.protocol),'Negotiated verified TLS required');
  checks.push('TLS chain/hostname validation enabled; secure context; '+security.protocol);
  await page.screenshot({path:output+'/entry.png',fullPage:true});
  const icon=await page.evaluate(()=>fetch('/icon.png').then(r=>r.status));assert.equal(icon,200);
  assert.equal(html.status(),200);
  assert.match(await page.title(),/Kane System/);
  const csp=html.headers()['content-security-policy'];
  assert.match(csp,/nonce-/);assert(!csp.includes('unsafe-eval'));
  await page.getByLabel('一次性配对码',{exact:true}).fill(code);
  await page.getByRole('button',{name:'配对并进入'}).click();
  await page.getByRole('status').filter({hasText:'已同步'}).waitFor();
  assert.equal(await page.evaluate(()=>document.cookie),'');
  checks.push('actual HTTPS, strict CSP hydration, Secure HttpOnly session, WSS authoritative snapshot');
  await page.getByRole('button',{name:'设备与账户'}).click();
  await page.getByRole('button',{name:'登记 Passkey'}).click();
  await page.getByRole('status').filter({hasText:'Passkey 已登记'}).waitFor();
  await page.getByRole('button',{name:'退出登录'}).click();
  await page.getByRole('button',{name:'使用 Passkey 登录'}).click();
  await page.getByRole('status').filter({hasText:'已同步'}).waitFor();
  const replay=await post('/auth/passkey/login/finish',authFinishes['/control-api/auth/passkey/login/finish']);assert.equal(replay.status,403);
  checks.push('one-use WebAuthn assertion replay rejected');
  checks.push('WebAuthn virtual authenticator: real registration/assertion crypto/UV, logout then sign-in on paired '+scope+' (no physical Passkey claim)');
  if(await page.getByRole('button',{name:'设备与账户'}).getAttribute('aria-expanded')==='true')await page.getByRole('button',{name:'设备与账户'}).click();
  await page.getByLabel('任务指令',{exact:true}).fill('Read input.txt through the original read tool and return its actual content as Markdown.');
  await page.getByRole('button',{name:'发送任务',exact:true}).click();
  await page.getByRole('button',{name:'允许本次',exact:true}).click();
  checks.push('original live ACP permission appears over WSS and is approved through Owner control UI');
  await waitState('Verifying');
  await page.getByRole('button',{name:'input.txt',exact:true}).click();
  await page.getByLabel('原生文件内容',{exact:true}).waitFor();
  assert.match(await page.getByLabel('原生文件内容',{exact:true}).inputValue(),/KANE_PR2_FIXTURE/);
  await page.getByRole('button',{name:'接管编辑',exact:true}).click();
  await page.getByText('由你编辑',{exact:true}).waitFor();
  await page.getByLabel('原生文件内容',{exact:true}).fill('KANE_BROWSER_HUMAN_BYTES\n');
  await context.setOffline(true);
  await page.getByText('○ 连接中 · 写入已停用',{exact:true}).waitFor({timeout:15000});
  assert(await page.getByRole('button',{name:'保存文件',exact:true}).isDisabled());
  assert.equal(await page.getByLabel('原生文件内容',{exact:true}).inputValue(),'KANE_BROWSER_HUMAN_BYTES\n');
  await context.setOffline(false);
  await page.getByRole('status').filter({hasText:'已同步'}).waitFor({timeout:120000});
  let state=await snapshot();const task=state.tasks[0].id;
  assert.equal(state.workspaces[0].workspace.version,0,'offline draft must not auto-submit');
  await page.getByRole('button',{name:'续期租约',exact:true}).click();
  await page.getByRole('status').filter({hasText:'人工租约已续期'}).waitFor();
  await page.getByRole('button',{name:'保存文件',exact:true}).click();
  await page.getByRole('status').filter({hasText:'原生文件已保存'}).waitFor();
  const downloadPromise=page.waitForEvent('download');
  await page.getByRole('link',{name:'下载原文件',exact:true}).click();
  const download=await downloadPromise;
  await download.saveAs(output+'/native-input.txt');
  checks.push('original Kanaloa tool/Artifact; physical takeover, native edit, offline no replay, WSS reconnect, saved file download');
  if(process.env.KANE_CONTROL_TEST_EXPIRY_SIGNAL){
    const before=await snapshot();
    const fault=await page.evaluate(()=>{
      const {sockets,frames}=window.__kaneTestChannels;const old=sockets.find(s=>s.readyState===WebSocket.CLOSED),current=sockets.find(s=>s.readyState===WebSocket.OPEN);
      if(!old||!current||!frames.length)throw Error('Real closed/reconnected channels and earlier frames required');
      old.dispatchEvent(new Event('open'));old.dispatchEvent(new CloseEvent('close',{code:4401}));
      current.dispatchEvent(new MessageEvent('message',{data:frames[0]}));
      return {active:sockets.filter(s=>s.readyState===WebSocket.OPEN).length,replayed_cursor:JSON.parse(frames[0]).authority.cursor};
    });
    await page.waitForTimeout(100);
    assert.equal(fault.active,1);assert(fault.replayed_cursor<before.cursor);
    const prior=before.workspaces[0].workspace;
    assert(await page.getByText(`文档与数据 · 版本 ${prior.version} · epoch ${prior.lease_epoch}`,{exact:true}).isVisible());
    assert(await page.getByRole('button',{name:'设备与账户'}).isVisible());
    checks.push('real WSS reconnect has one active channel; late retired-channel open/revoke and earlier genuine snapshot cannot regress current state');
    const previous=(await snapshot()).workspaces[0].workspace;
    await writeFile(process.env.KANE_CONTROL_TEST_EXPIRY_SIGNAL,'expire');
    await page.getByRole('button',{name:'接管编辑',exact:true}).waitFor();
    await page.waitForFunction(()=>!Array.from(document.querySelectorAll('button')).find(b=>b.textContent==='接管编辑')?.disabled);
    assert(await page.getByRole('button',{name:'保存文件',exact:true}).isDisabled());
    await page.getByRole('button',{name:'接管编辑',exact:true}).click();
    await page.getByRole('status').filter({hasText:'已确认 Agent 停止'}).waitFor();
    const recovered=(await snapshot()).workspaces[0].workspace;assert(recovered.lease_epoch>previous.lease_epoch);
    checks.push('private expiry fault: real Core deadline expires, writes stop; explicit Owner reacquisition increases epoch; no automatic renewal');
  }
  await page.getByRole('button',{name:'续期租约',exact:true}).click();await page.getByRole('status').filter({hasText:'人工租约已续期'}).waitFor();
  // Two separate paired browser profiles, not a claim of two physical devices.
  const pair=await post('/auth/pair-code');assert.equal(pair.status,200);
  const secondContext=await browser.newContext();const second=await secondContext.newPage();second.setDefaultTimeout(180000);second.setDefaultNavigationTimeout(300000);
  await second.goto(origin+'/control');await second.getByLabel('一次性配对码',{exact:true}).fill(pair.value.pairing_code);await second.getByRole('button',{name:'配对并进入'}).click();await second.getByRole('status').filter({hasText:'已同步'}).waitFor();
  const other=await second.evaluate(()=>fetch('/control-api/snapshot').then(r=>r.json()));assert.equal(other.workspaces[0].own_lease,false);
  const invalidSave=await second.evaluate(async({task,w,hash})=>{const r=await fetch(`/control-api/tasks/${task}/save`,{method:'POST',headers:{'Content-Type':'application/json','X-Kane-Intent':'owner-action'},body:JSON.stringify({epoch:w.lease_epoch,version:w.version,request_id:'other-profile-write',path:'input.txt',base_hash:hash,content:'MUST_NOT_WRITE'})});return r.status;},{task,w:other.workspaces[0].workspace,hash:(await page.evaluate(task=>fetch(`/control-api/tasks/${task}/files/input.txt`).then(r=>r.json()),task)).hash});assert.equal(invalidSave,409);
  const devicesBefore=await page.evaluate(()=>fetch('/control-api/devices').then(r=>r.json()));assert.equal(devicesBefore.length,2);
  // Revoke the device identified by its paired second profile session.
  const deviceSession=await second.evaluate(()=>fetch('/control-api/session').then(r=>r.json()));
  assert(deviceSession.device_id);assert.equal((await post(`/devices/${deviceSession.device_id}/revoke`)).status,200);
  await second.getByRole('button',{name:'配对并进入'}).waitFor();assert.equal((await second.evaluate(()=>fetch('/control-api/snapshot').then(r=>r.status))),401);
  await secondContext.close();
  await page.getByRole('button',{name:'续期租约',exact:true}).click();await page.getByRole('status').filter({hasText:'人工租约已续期'}).waitFor();
  checks.push('separately paired browser profile cannot use another session lease; denied concurrent write; revoked HTTP/WSS session cannot resume');
  for(const [width,height,label] of [[390,844,'phone-layout'],[820,1180,'tablet-layout'],[1440,1000,'desktop']]) {
    await page.setViewportSize({width,height});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    await page.screenshot({path:`${output}/${label}.png`,fullPage:true});
  }
  checks.push('three viewports on '+scope+'; responsive layout only, no mobile device claim');
  await page.getByLabel('任务指令',{exact:true}).fill('Read input.txt again with the original read tool. Deliver the human-edited native content in a new Markdown result.');
  await page.getByRole('button',{name:'交还并继续',exact:true}).click();
  await page.getByRole('button',{name:'允许本次',exact:true}).click();
  await waitState('Verifying');
  await page.waitForFunction(()=>document.querySelectorAll('.control-results details').length===2);
  state=await snapshot();
  const artifact=state.artifacts.find(a=>a.verification_state==='pending');
  assert.match(artifact.content,/KANE_BROWSER_HUMAN_BYTES/);
  const file=await page.evaluate(task=>fetch(`/control-api/tasks/${task}/files/input.txt`).then(r=>r.json()),task);
  assert.equal(file.content,'KANE_BROWSER_HUMAN_BYTES\n');
  assert.equal(artifact.workspace_source.files['input.txt'].hash,file.hash);
  await page.getByLabel('独立验收依据',{exact:true}).fill('Independent browser test checked native download/current file bytes, Artifact human marker and native source SHA256/version after original read. Virtual-browser/Mock-model reference only.');
  await page.getByRole('button',{name:'独立验证通过',exact:true}).click();
  await waitState('Done');
  const resultPanel=page.locator('.control-results details').last();await resultPanel.locator('summary').click();
  const artifactDownload=page.waitForEvent('download');await resultPanel.getByRole('button',{name:/下载成果/}).click();
  const artifactFile=await artifactDownload;const downloaded=await readFile(await artifactFile.path(),'utf8');assert.equal(downloaded,artifact.content);
  await artifactFile.saveAs(output+'/editable-artifact.md');
  checks.push('editable original Markdown Artifact downloaded with exact authoritative content, separately from native source file');
  checks.push('same native Session resumed; fresh original read fenced, new Artifact/provenance, independent verdict -> Done');
  const manifest=await page.evaluate(async()=>{const r=await fetch('/control/manifest.webmanifest');return {status:r.status,value:await r.json()}});assert.equal(manifest.status,200);assert.equal(manifest.value.start_url,'/control');
  assert(await page.evaluate(()=>Promise.race([navigator.serviceWorker.ready.then(r=>r.scope.endsWith('/control')),new Promise((_,reject)=>setTimeout(()=>reject(Error('Service worker registration deadline')),8000))])));
  checks.push('PWA manifest and minimal service worker; no API cache or background write queue');
  // A captured/malformed assertion must fail the trusted verifier before Core session issuance.
  const invalid=await post('/auth/passkey/login/options');assert.equal(invalid.status,200);
  const forged=await post('/auth/passkey/login/finish',{challenge_id:invalid.value.challenge_id,credential:{id:'forged',type:'public-key',response:{}}});assert.equal(forged.status,403);
  checks.push('forged public assertion denied; no client-supplied verified flag');
  // Device revoke invalidates the running WSS as well as subsequent HTTP.
  const devices=await page.evaluate(()=>fetch('/control-api/devices').then(r=>r.json()));
  const current=await page.evaluate(()=>fetch('/control-api/session').then(r=>r.json()));
  const revoked=await post(`/devices/${current.device_id}/revoke`);assert.equal(revoked.status,200);
  await page.getByRole('button',{name:'配对并进入'}).waitFor({timeout:10000});
  checks.push('device revocation closes existing WSS and requires explicit pairing again');
  await writeFile(output+'/result.json',JSON.stringify({checks,errors,appErrors,requests,actual_platform:{platform,release:os.release(),arch:os.arch(),browser:browser.version()},device_scope:scope+' + CDP virtual WebAuthn; physical hardware Passkey not tested',TLS:'Explicitly trusted test certificate; default browser chain/hostname verification ON; end-to-end TLS/WSS, not public Internet certification'},null,2));
  assert.deepEqual(appErrors,[], 'Every actual page exception must fail, including WebSocket lifecycle errors');
  assert.deepEqual(errors.filter(e=>!e.includes('403')&&!e.includes('ERR_INTERNET_DISCONNECTED')&&!e.includes('WebSocket')&&!e.includes('401')),[]);
  console.log(JSON.stringify({passed:checks.length,evidence:output,scope:scope+', verified TLS/WSS, real Kanaloa, deterministic Mock; no inference about other platforms'}));
} catch(e) {
  await writeFile(output+'/failure-context.json',JSON.stringify({checks,errors,appErrors,requests},null,2));
  await writeFile(output+'/failure.txt',await page.locator('body').innerText().catch(()=>''));
  await page.screenshot({path:output+'/failure.png',fullPage:true}).catch(()=>{});
  console.error(String(e));throw e;
} finally {clearInterval(progress);await browser.close();}
