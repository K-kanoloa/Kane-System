"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { createPasskey, authenticatePasskey } from "./passkey";

type Task = { id: string; state: string; verification_state: string; version: number; block_reason: string | null; spec: { goal: string; acceptance: string[] } };
type Workspace = { id: string; version: number; lease_epoch: number; holder: string | null; expires_at: number | null };
type Work = { task_id: string; workspace: Workspace; control: { phase: string } | null; files: Record<string, { hash: string; bytes: number }> | null; controlled_native: boolean; own_lease: boolean; unresolved: boolean };
type Artifact = { id: string; task_id: string; version: number; verification_state: string; content: string; content_hash: string; workspace_source: { workspace_version: number; files: Record<string, { hash: string }> } | null };
type Message = { message_id: string; turn_id: string; sender: string; sender_id?: string; content: string };
type Snapshot = { tasks: Task[]; workspaces: Work[]; artifacts: Artifact[]; bindings: { task_id: string; turn_id: string }[]; messages: Message[]; turns: { turn_id: string; status: string; partial_output: string | null }[]; cursor: number; edits: { id: string; task_id: string; path: string; state: string; hash: string }[]; permissions: { task_id: string; request_id: string; title: string }[]; events: { seq: number; kind: string }[] };
type Device = { id: string; label: string; revoked_at: number | null };
type FileState = { path: string; content: string; hash: string; workspace: Workspace };
const TERMINAL = new Set(["Done", "Failed", "Cancelled", "ClosedUnverified"]);
async function api<T>(path: string, body?: object): Promise<T> {
  const response = await fetch(`/control-api${path}`, { credentials: "same-origin", cache: "no-store", ...(body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json", "X-Kane-Intent": "owner-action" }, body: JSON.stringify(body) }) });
  const data = await response.json();
  if (!response.ok) throw new Error(`${response.status}: ${data.detail || data.reason || "操作未确认，请检查权威状态后决定是否重试"}`);
  return data as T;
}
export default function Control() {
  const [auth, setAuth] = useState(false), [connected, setConnected] = useState(false);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null), [selected, setSelected] = useState("");
  const [pairCode, setPairCode] = useState(""), [label, setLabel] = useState("我的设备");
  const [error, setError] = useState(""), [notice, setNotice] = useState(""), [busy, setBusy] = useState(false);
  const [file, setFile] = useState<FileState | null>(null), [draft, setDraft] = useState(""), [dirty, setDirty] = useState(false);
  const [prompt, setPrompt] = useState(""), [evidence, setEvidence] = useState("");
  const [credentials, setCredentials] = useState<{ id: string; revoked_at: number | null }[]>([]);
  const [devices, setDevices] = useState<Device[]>([]), [account, setAccount] = useState(false), [generatedCode, setGeneratedCode] = useState("");
  const cursor = useRef(0), authGeneration = useRef(0);
  const acceptSnapshot = useCallback((value: Snapshot) => {
    if (value.cursor < cursor.current) return;
    cursor.current = value.cursor; setSnapshot(value);
  }, []);
  const refresh = useCallback(async () => {
    const generation = authGeneration.current;
    const value = await api<Snapshot>("/snapshot");
    if (generation === authGeneration.current) acceptSnapshot(value);
  }, [acceptSnapshot]);
  const action = async (fn: () => Promise<void>) => {
    setBusy(true); setError(""); setNotice("");
    try { await fn(); } catch (e) { setError(e instanceof Error ? e.message : "操作未确认"); }
    finally { setBusy(false); }
  };
  useEffect(() => {
    let live = true;
    api("/session").then(() => { if (live) setAuth(true); }).catch(() => {});
    if ("serviceWorker" in navigator) navigator.serviceWorker.register("/kane-control-sw.js", { scope: "/control" }).catch(() => {});
    return () => { live = false; };
  }, []);
  useEffect(() => {
    ++authGeneration.current;
    if (!auth) { cursor.current = 0; return; }
    let live = true, socket: WebSocket | null = null, timer: ReturnType<typeof setTimeout> | undefined;
    let attempts = 0, lastFrame = Date.now();
    const retire = () => {
      const previous = socket; socket = null;
      if (previous) { previous.onopen = null; previous.onmessage = null; previous.onclose = null; previous.onerror = null; previous.close(); }
    };
    const connect = () => {
      if (!live || !navigator.onLine || socket?.readyState === WebSocket.CONNECTING || socket?.readyState === WebSocket.OPEN) return;
      clearTimeout(timer);
      const channel = new WebSocket(`${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/control-api/events`);
      socket = channel;
      channel.onopen = () => { if (live && socket === channel) { attempts = 0; channel.send(JSON.stringify({ cursor: cursor.current })); } };
      channel.onmessage = event => {
        if (!live || socket !== channel || !navigator.onLine) return;
        const frame = JSON.parse(event.data);
        if (frame.type === "snapshot") { lastFrame = Date.now(); acceptSnapshot(frame.authority); setConnected(true); }
      };
      channel.onclose = event => {
        if (!live || socket !== channel) return;
        socket = null; setConnected(false);
        if (event.code === 4401) { setAuth(false); setSnapshot(null); setError("会话已过期或设备已撤销，请重新登录。"); return; }
        if (navigator.onLine) timer = setTimeout(connect, Math.min(1000 * 2 ** attempts++, 15000));
      };
      channel.onerror = () => { if (live && socket === channel) channel.close(); };
    };
    const offline = () => { setConnected(false); clearTimeout(timer); retire(); };
    const online = () => { clearTimeout(timer); connect(); };
    window.addEventListener("offline", offline); window.addEventListener("online", online);
    const heartbeat = setInterval(() => { if (Date.now() - lastFrame > 5000) setConnected(false); }, 2000);
    connect();
    return () => { live = false; ++authGeneration.current; clearTimeout(timer); clearInterval(heartbeat); window.removeEventListener("offline", offline); window.removeEventListener("online", online); retire(); };
  }, [auth, acceptSnapshot]);
  const task = snapshot?.tasks.find(t => t.id === selected) ?? snapshot?.tasks[0];
  const work = snapshot?.workspaces.find(w => w.task_id === task?.id);
  const lease = work?.workspace;
  const humanLeaseLive = work?.control?.phase === "Human" && (lease?.expires_at ?? 0) * 1000 > Date.now();
  const editable = connected && work?.own_lease && humanLeaseLive && !work?.unresolved;
  const version = () => ({ epoch: lease?.lease_epoch, version: lease?.version });
  const load = async (path: string) => {
    if (dirty && !window.confirm("当前有未保存草稿。确认放弃草稿并打开文件？")) return;
    const value = await api<FileState>(`/tasks/${task!.id}/files/${encodeURIComponent(path)}`);
    setFile(value); setDraft(value.content); setDirty(false);
  };
  const downloadArtifact = (artifact: Artifact) => {
    const url = URL.createObjectURL(new Blob([artifact.content], { type: "text/markdown;charset=utf-8" }));
    const link = document.createElement("a"); link.href = url; link.download = `${artifact.id}.md`; link.hidden = true;
    document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  const doPasskey = (register: boolean) => action(async () => {
    if (!window.PublicKeyCredential) throw new Error("此浏览器未提供 Passkey，请使用一次性配对码。");
    const options = await api<{ challenge_id: string; publicKey: PublicKeyCredentialCreationOptions & PublicKeyCredentialRequestOptions }>(`/auth/passkey/${register ? "register" : "login"}/options`, {});
    const credential = register ? await createPasskey(options.publicKey) : await authenticatePasskey(options.publicKey);
    await api(`/auth/passkey/${register ? "register" : "login"}/finish`, { challenge_id: options.challenge_id, credential });
    if (register) { setNotice("Passkey 已登记。"); setCredentials(await api("/credentials")); } else setAuth(true);
  });
  if (!auth) return <main className="control-login control-root" lang="zh-CN"><section className="control-pair"><div className="control-brand">K / Kane System</div><h1>连接你的工作区</h1><p>使用主机或已授权设备提供的一次性配对码。无需云账号。</p><form onSubmit={e => { e.preventDefault(); void action(async () => { await api("/auth/pair", { pairing_code: pairCode.trim(), label }); setPairCode(""); setAuth(true); }); }}><label>设备名称<input autoComplete="off" value={label} onChange={e => setLabel(e.target.value)} maxLength={100} required /></label><label>一次性配对码<input aria-label="一次性配对码" autoComplete="off" value={pairCode} onChange={e => setPairCode(e.target.value)} minLength={64} maxLength={64} required /></label><button className="control-primary" disabled={busy}>配对并进入</button></form><div className="control-divider">已配对的设备</div><button disabled={busy} onClick={() => void doPasskey(false)}>使用 Passkey 登录</button>{error && <p role="alert" className="control-error">{error}</p>}<small>使用 HTTPS 连接。设备授权可以随时撤销。</small></section></main>;
  const binding = snapshot?.bindings.find(b => b.task_id === task?.id);
  const messages = snapshot?.messages.filter(m => m.turn_id === binding?.turn_id) ?? [];
  return <main className="control-root control-shell" lang="zh-CN">
    <header className="control-header"><strong>Kane System <span>工作区</span></strong><div><span role="status" className={connected ? "control-live" : "control-disconnected"}>{connected ? "● 已同步" : "○ 连接中 · 写入已停用"}</span><button aria-expanded={account} onClick={() => { setAccount(!account); void action(async () => { const [d,c] = await Promise.all([api<Device[]>("/devices"),api<{ id: string; revoked_at: number | null }[]>("/credentials")]); setDevices(d); setCredentials(c); }); }}>设备与账户</button></div></header>
    {error && <div role="alert" className="control-error control-banner">{error}<button onClick={() => setError("")} aria-label="关闭错误">×</button></div>}
    {notice && <div role="status" className="control-notice control-banner">{notice}</div>}
    {account && <section className="control-account" aria-label="设备与账户"><h2>本地 Owner</h2><p>新设备需要一次性配对；已配对设备可使用 Passkey 重新登录。</p><div className="control-actions"><button disabled={busy} onClick={() => void doPasskey(true)}>登记 Passkey</button><button disabled={busy} onClick={() => void action(async () => { const r = await api<{ pairing_code: string }>("/auth/pair-code", {}); setGeneratedCode(r.pairing_code); })}>生成配对码</button><button onClick={() => void action(async () => { await api("/auth/logout", {}); setAuth(false); setSnapshot(null); })}>退出登录</button></div>{generatedCode && <label>5 分钟内有效，仅可使用一次<input readOnly value={generatedCode} aria-label="新设备配对码" /></label>}<ul>{devices.map(d => <li key={d.id}>{d.label} <small>{d.revoked_at ? "已撤销" : "已授权"}</small>{!d.revoked_at && <button onClick={() => void action(async () => { await api(`/devices/${d.id}/revoke`, {}); setDevices(await api<Device[]>("/devices")); })}>撤销 {d.label}</button>}</li>)}</ul>{credentials.filter(c => !c.revoked_at).map(c => <p key={c.id}>Passkey …{c.id.slice(-8)} <button onClick={() => void action(async () => { await api(`/credentials/${encodeURIComponent(c.id)}/revoke`, {}); setCredentials(await api("/credentials")); })}>撤销此 Passkey</button></p>)}</section>}
    <div className="control-body"><nav className="control-tasks" aria-label="任务列表"><h2>任务</h2>{snapshot?.tasks.map(t => <button key={t.id} aria-current={task?.id === t.id ? "page" : undefined} onClick={() => { if (dirty && !confirm("放弃未保存的草稿并切换任务？")) return; setSelected(t.id); setFile(null); setDirty(false); }}><strong>{t.spec.goal}</strong><small>{t.state} · {t.verification_state}</small></button>)}{!snapshot?.tasks.length && <p>没有已注册任务。主机创建任务后将在这里显示。</p>}</nav>
    {task && <section className="control-work" aria-label="任务工作区"><div className="control-task-heading"><div><small className="mono">{task.id}</small><h1>{task.spec.goal}</h1><p>{task.state} · 验证：{task.verification_state}{task.block_reason && ` · ${task.block_reason}`}</p></div><button disabled={busy || !connected} onClick={() => void action(refresh)}>刷新状态</button></div>
      {work?.control?.phase === "Human" && !humanLeaseLive && <p className="control-error">人工租约已过期，请重新接管。草稿不会自动提交。</p>}
      {work?.unresolved && <p className="control-error">存在未决动作。写入与交还已阻断，请在主机核对。不会自动重放。</p>}
      {snapshot?.edits.filter(e => e.task_id === task.id && ["Unknown", "Attempting"].includes(e.state)).map(edit => <section key={edit.id} className="control-approval"><h2>文件写入待核对</h2><p>{edit.path} · {edit.state} · 请求 {edit.id}</p><p>核对实际文件内容后选择判定。系统检查实际 hash；不会重新写入或自动重试。</p><div className="control-actions">{["Confirmed", "NotExecuted"].map(outcome => <button key={outcome} disabled={busy || !connected} onClick={() => void action(async () => { await api(`/edits/${encodeURIComponent(edit.id)}/reconcile`, { outcome }); await refresh(); setNotice("文件核对已记录；未重新执行写入。"); })}>{outcome === "Confirmed" ? "核对已写入目标版本" : "核对未执行，仍为原版本"}</button>)}</div></section>)}
      <div className="control-columns"><section className="control-conversation"><h2>对话与执行</h2><div className="control-messages" aria-live="polite">{messages.map(m => <article key={m.message_id}><small>{m.sender === "user" ? "Owner" : (m.sender_id ?? "Agent")}</small><p>{m.content}</p></article>)}{snapshot?.turns.filter(t => t.turn_id === binding?.turn_id && t.status === "running" && t.partial_output).map(t => <article key={`partial-${t.turn_id}`}><small>Agent · 正在生成，尚未验证</small><p>{t.partial_output}</p></article>)}{!messages.length && <p className="control-muted">输入目标，交由现有 Agent 执行。</p>}</div>
      {snapshot?.permissions.filter(p => p.task_id === task.id).map(p => <section key={p.request_id} className="control-approval"><p>{p.title}</p>{["allow-once", "reject-once"].map(decision => <button key={decision} disabled={busy || !connected} onClick={() => void action(async () => { await api(`/tasks/${task.id}/approval`, { request_id: p.request_id, decision }); await refresh(); })}>{decision === "allow-once" ? "允许本次" : "拒绝本次"}</button>)}</section>)}
      <label>指令<textarea aria-label="任务指令" rows={3} value={prompt} onChange={e => setPrompt(e.target.value)} placeholder={work?.control?.phase === "Human" ? "交还时，要求 Agent 读取修改后的文件…" : "描述任务目标…"} /></label><button className="control-primary" disabled={busy || !connected || !prompt.trim() || !["Pending", "Ready"].includes(task.state) || !!binding} onClick={() => void action(async () => { await api(`/tasks/${task.id}/dispatch`, { content: prompt }); setPrompt(""); await refresh(); })}>发送任务</button></section>
      <section className="control-files"><div className="control-section-heading"><h2>原生文件</h2><small>{work?.control?.phase === "Human" ? (work.own_lease ? "由你编辑" : "其他设备编辑中") : work?.control?.phase === "TakingOver" ? "正在停止 Agent" : "Agent 控制"}</small></div>{!work?.controlled_native ? <p>此工作区尚未提供已验证的原生编辑边界。</p> : <><p className="control-muted">文档与数据 · 版本 {lease?.version} · epoch {lease?.lease_epoch}</p><div className="control-file-tabs">{Object.keys(work.files ?? {}).map(path => <button key={path} aria-pressed={file?.path === path} onClick={() => void action(() => load(path))}>{path}</button>)}</div>
      {file && <><div className="control-file-meta"><strong>{file.path}</strong><a href={`/control-api/tasks/${task.id}/download/${encodeURIComponent(file.path)}`} download>下载原文件</a></div><small className="mono">SHA256 {file.hash}</small>{file.hash !== work.files?.[file.path]?.hash && <p className="control-error">服务端文件已改变。草稿未覆盖，请重新打开并核对。</p>}<textarea className="control-editor" aria-label="原生文件内容" value={draft} readOnly={!editable} spellCheck={false} onChange={e => { setDraft(e.target.value); setDirty(true); }} /><p className="control-muted">{dirty ? "有未保存草稿；断线后不会自动发送。" : "内容已载入；只有有效人工租约可写入。"}</p></>}
      <div className="control-actions"><button disabled={busy || !connected || TERMINAL.has(task.state) || work.unresolved || humanLeaseLive} onClick={() => void action(async () => { await api(`/tasks/${task.id}/takeover`, version()); await refresh(); setNotice("已确认 Agent 停止并取得人工租约。打开文件开始编辑。"); })}>接管编辑</button><button disabled={busy || !editable || !file || !dirty} className="control-primary" onClick={() => void action(async () => { await api(`/tasks/${task.id}/save`, { ...version(), request_id: crypto.randomUUID(), path: file!.path, content: draft, base_hash: file!.hash }); setDirty(false); const r = await api<FileState>(`/tasks/${task.id}/files/${encodeURIComponent(file!.path)}`); setFile(r); setDraft(r.content); await refresh(); setNotice("原生文件已保存，版本与事件已确认。"); })}>保存文件</button><button disabled={busy || !editable} onClick={() => void action(async () => { await api(`/tasks/${task.id}/renew`, version()); await refresh(); setNotice("人工租约已续期。"); })}>续期租约</button><button disabled={busy || !editable || dirty || !file || !prompt.trim()} onClick={() => void action(async () => { await api(`/tasks/${task.id}/handoff`, { ...version(), read_path: file!.path, message_id: crypto.randomUUID(), content: prompt }); setPrompt(""); setFile(null); await refresh(); setNotice("控制权已交还，Agent 必须先读取新版本。结果仍需独立验证。"); })}>交还并继续</button></div><small>交还前保存文件，并在「指令」中填写继续要求。租约过期后重新接管；不会自动交还或覆盖。</small></>}
      </section></div>
      <section className="control-results"><h2>成果与独立验证</h2><ul className="control-acceptance">{task.spec.acceptance.map((a, i) => <li key={i}>{a}</li>)}</ul>{snapshot?.artifacts.filter(a => a.task_id === task.id).map(a => <details key={a.id}><summary>成果 v{a.version} · {a.verification_state} · 工作区版本 {a.workspace_source?.workspace_version ?? "未关联"}</summary><p className="mono">SHA256 {a.content_hash}</p><pre>{a.content}</pre><button onClick={() => downloadArtifact(a)}>下载成果 v{a.version}（Markdown）</button></details>)}{task.state === "Verifying" && <div><label>独立验收依据<textarea aria-label="独立验收依据" value={evidence} onChange={e => setEvidence(e.target.value)} rows={2} placeholder="核对原文件、内容、来源和验收条件后，记录判断依据。" /></label><div className="control-actions">{[true, false].map(passed => <button key={String(passed)} disabled={busy || !connected || !evidence.trim() || work?.unresolved} onClick={() => void action(async () => { await api(`/tasks/${task.id}/verify`, { version: task.version, passed, evidence }); setEvidence(""); await refresh(); })}>{passed ? "独立验证通过" : "验证失败，退回修改"}</button>)}</div></div>}</section>
    </section>}</div><footer>本地 Owner · 受控文件编辑与人工接管</footer>
  </main>;
}
