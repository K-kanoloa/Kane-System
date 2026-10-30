"use client";

import { useState } from "react";
import Link from "next/link";
import { api, setApiAccessToken, type Conversation, type SystemArtifact, type SystemStatus, type SystemTask, type Turn } from "@/lib/api";
import styles from "./system.module.css";

const states: Record<string, string> = { Pending: "待处理", Ready: "已就绪", Running: "执行中", Blocked: "等待处理", Verifying: "待核验", Done: "已验证完成", Failed: "失败", Cancelled: "已取消", ClosedUnverified: "未验证关闭" };

export default function SystemPage() {
  const [token, setToken] = useState("");
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [conversation, setConversation] = useState("");
  const [turn, setTurn] = useState("");
  const [goal, setGoal] = useState("");
  const [acceptance, setAcceptance] = useState("");
  const [workload, setWorkload] = useState("execution");
  const [selected, setSelected] = useState<SystemTask | null>(null);
  const [artifacts, setArtifacts] = useState<SystemArtifact[]>([]);
  const [evidence, setEvidence] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function action(work: () => Promise<void>) {
    setBusy(true); setError("");
    try { await work(); } catch (error) { setError(error instanceof Error ? error.message : "操作失败"); }
    finally { setBusy(false); }
  }
  async function refresh() {
    const [next, chats] = await Promise.all([api.systemStatus(), api.conversations()]);
    setStatus(next); setConversations(chats);
  }
  async function inspect(taskId: string) {
    const detail = await api.systemTask(taskId);
    setSelected(detail.task); setArtifacts(detail.artifacts); setEvidence("");
  }
  function download(artifact: SystemArtifact) {
    const url = URL.createObjectURL(new Blob([artifact.content], { type: "text/markdown;charset=utf-8" }));
    const link = document.createElement("a"); link.href = url; link.download = `${artifact.id}.md`; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  return <main className={styles.page}>
    <header className={styles.header}><div><small>MULTI-AI NATIVE OS · 开发中</small><h1>Kane System</h1><p>检查任务、查看真实成果，再决定是否通过验收。</p></div><Link href="/">返回 Kane 对话 →</Link></header>
    <div className={styles.notice}>当前为开发中的系统接入。现有 Harness 保留原有能力；系统级隔离、跨平台接管和可安装发行版尚未完成验收。</div>
    <form className={styles.auth} onSubmit={event => { event.preventDefault(); void action(async () => { if (token) { setApiAccessToken(token); setToken(""); } await refresh(); }); }}>
      <label>Owner 访问令牌<input type="password" autoComplete="off" value={token} onChange={event => setToken(event.target.value)} placeholder="使用当前标签页已保存的令牌，或输入新令牌" /></label>
      <button disabled={busy} type="submit">{status ? "刷新" : "连接控制面"}</button>
    </form>
    {error && <p role="alert" className={styles.error}>操作未完成：{error}</p>}
    {status && <>
      <div className={styles.metrics}><div><strong>{status.tasks.length}</strong><span>系统任务</span></div><div><strong>{status.tasks.filter(task => task.state === "Verifying").length}</strong><span>等待核验</span></div><div><strong>{status.tasks.filter(task => task.state === "Done").length}</strong><span>已验证成果</span></div><div><strong>{status.enforcement}</strong><span>当前强制边界</span></div></div>
      <div className={styles.columns}>
        <section className={styles.panel}><h2>接入现有工作</h2><p>从原项目的对话任务创建系统记录，保留原有执行和会话。</p>
          <form onSubmit={event => { event.preventDefault(); void action(async () => { const task = await api.systemBind({ turn_id: turn, goal, acceptance: acceptance.split("\n").map(line => line.trim()).filter(Boolean), workload: { class: workload, max_memory_bytes: 536870912, max_storage_bytes: 104857600 } }); await refresh(); await inspect(task.id); }); }}>
            <label>对话<select aria-label="对话" required disabled={busy} value={conversation} onChange={event => { const value = event.target.value; setConversation(value); setTurn(""); setTurns([]); if (value) void action(async () => setTurns(await api.turns(value))); }}><option value="">选择已有对话</option>{conversations.map(chat => <option key={chat.conversation_id} value={chat.conversation_id}>{chat.title}</option>)}</select></label>
            <label>已有工作<select aria-label="已有工作" required value={turn} onChange={event => setTurn(event.target.value)}><option value="">选择工作</option>{turns.map(item => <option key={item.turn_id} value={item.turn_id}>{item.title || item.turn_id} · {item.status}</option>)}</select></label>
            <label>成果目标<input required value={goal} onChange={event => setGoal(event.target.value)} /></label>
            <label>验收条件（每行一条）<textarea required value={acceptance} onChange={event => setAcceptance(event.target.value)} /></label>
            <label>工作类型<select aria-label="工作类型" value={workload} onChange={event => setWorkload(event.target.value)}><option value="execution">执行 / 文件与工具</option><option value="inference">推理 / 理解与分析</option><option value="generation">生成 / 接入生成后端</option></select></label>
            <button disabled={busy || !turn || !acceptance.trim()}>纳入系统管理</button>
          </form>
          <h2 className={styles.listTitle}>任务记录</h2>
          {status.tasks.length === 0 && <p>尚无系统任务。上方可接入现有工作。</p>}
          {status.tasks.map(task => <button className={styles.task} disabled={busy} aria-pressed={selected?.id === task.id} key={task.id} onClick={() => void action(() => inspect(task.id))}><strong>{task.spec.goal}</strong><span>{states[task.state] || task.state}</span></button>)}
        </section>
        <section className={styles.panel} aria-live="polite"><h2>{selected?.spec.goal || "成果与验证"}</h2>
          {!selected ? <p>选择一个任务，查看验收条件、成果版本及验证状态。</p> : <>
            <p className={styles.state}>{states[selected.state] || selected.state} · 验证：{selected.verification_state}</p>
            {selected.block_reason && <p>等待原因：{selected.block_reason}</p>}
            <ul>{selected.spec.acceptance.map((item, index) => <li key={index}>{item}</li>)}</ul>
            <button disabled={busy || !status.bindings.some(binding => binding.task_id === selected.id)} onClick={() => void action(async () => { const binding = status.bindings.find(item => item.task_id === selected.id); if (binding) { await api.systemSync(binding.turn_id); await refresh(); await inspect(selected.id); } })}>核对最新执行结果</button>
            {artifacts.length === 0 && <p className={styles.empty}>尚无正式提交的成果。Agent 的完成消息需要核对后才进入待验证状态。</p>}
            {artifacts.map(artifact => <article key={artifact.id} className={styles.artifact}><div><strong>Markdown 成果</strong><button onClick={() => download(artifact)}>下载可编辑文件</button></div><pre>{artifact.content}</pre><small>验证：{artifact.verification_state} · SHA-256：{artifact.content_hash}</small></article>)}
            {selected.state === "Verifying" && <form onSubmit={event => event.preventDefault()}><label>独立核验依据<textarea required value={evidence} onChange={event => setEvidence(event.target.value)} placeholder="阅读成果，对照验收条件，记录实际检查与结果" /></label><div className={styles.actions}>{[true, false].map(passed => <button key={String(passed)} disabled={busy || !evidence.trim()} onClick={() => void action(async () => { await api.systemVerify(selected.id, selected.version, passed, evidence); await refresh(); await inspect(selected.id); })}>{passed ? "确认通过验收" : "退回修改"}</button>)}</div></form>}
          </>}
        </section>
      </div>
    </>}
  </main>;
}
