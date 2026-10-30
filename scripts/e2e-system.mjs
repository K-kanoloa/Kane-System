// Browser acceptance reuses the original Kanaloa ACP peer fixture. No live model.
// No Browser plugin is available in this session; use installed Playwright Chromium.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const output = await mkdtemp(path.join(tmpdir(), "kane-system-browser-"));
const apiPort = Number(process.env.SYSTEM_TEST_API_PORT ?? 8120);
const webPort = Number(process.env.SYSTEM_TEST_WEB_PORT ?? 3120);
const apiBase = `http://127.0.0.1:${apiPort}`;
const webBase = `http://127.0.0.1:${webPort}`;
const token = "system-browser-fixture-only";
const children = [];
let browser;
let page;
const checks = [];

function start(command, args, cwd, environment) {
  const child = spawn(command, args, { cwd, env: { ...process.env, ...environment }, stdio: ["ignore", "pipe", "pipe"] });
  child.log = "";
  for (const stream of [child.stdout, child.stderr]) stream.on("data", data => { child.log = (child.log + data.toString()).slice(-6000); });
  children.push(child); return child;
}
async function ready(url, child) {
  const start = Date.now();
  while (Date.now() - start < 60000) {
    if (child.exitCode !== null) throw new Error(child.log);
    try { if ((await fetch(url, { signal: AbortSignal.timeout(1000) })).ok) return; } catch {}
    await new Promise(resolve => setTimeout(resolve, 150));
  }
  throw new Error(`server deadline: ${url}`);
}
async function request(endpoint, body) {
  const response = await fetch(`${apiBase}${endpoint}`, { method: body ? "POST" : "GET", headers: { "X-Api-Key": token, "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
  assert.equal(response.status, 200, `${endpoint}: ${await response.clone().text()}`); return response.json();
}
try {
  const fixture = await readFile(path.join(root, "apps/api/tests/ui_fixture.py"), "utf8");
  await writeFile(path.join(output, "system_fixture.py"), fixture.replace('"stopReason": "endTurn"', '"stopReason": "end_turn", "_meta": {"kaneNativeEndKind": "completed"}'));
  const backend = start(path.join(root, "apps/api/.venv/bin/python"), ["-m", "uvicorn", "system_fixture:create_ui_app", "--factory", "--app-dir", output, "--host", "127.0.0.1", "--port", String(apiPort)], path.join(root, "apps/api"), {
    PYTHONPATH: path.join(root, "apps/api"), KANE_UI_TEST: "1", KANE_SQLITE_PATH: path.join(output, "harness.db"),
    KANE_SYSTEM_ENABLED: "1", KANE_SYSTEM_DB: path.join(output, "system.db"), KANE_SYSTEM_BINARY: path.join(root, "target/debug/kanesys"), OCTOPUS_API_TOKEN: token,
  });
  const web = start(process.execPath, [path.join(root, "node_modules/next/dist/bin/next"), "start", "--hostname", "127.0.0.1", "--port", String(webPort)], path.join(root, "apps/web"), { KANE_API_BASE_URL: apiBase, NEXT_TELEMETRY_DISABLED: "1" });
  await Promise.all([ready(`${apiBase}/health`, backend), ready(`${webBase}/system`, web)]);
  const conversation = await request("/api/v1/conversations", { title: "System reuse acceptance", bound_agent_id: "kanaloa" });
  const execution = await request(`/api/v1/conversations/${conversation.conversation_id}/messages`, { content: "Produce the existing test-peer report" });
  let turn;
  for (let attempt = 0; attempt < 50; attempt++) {
    turn = await request(`/api/v1/turns/${execution.turn.turn_id}`);
    if (turn.status === "finished") break;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  assert.equal(turn.status, "finished");
  const callsBefore = await request("/__ui__/calls");
  browser = await chromium.launch({ headless: true });
  page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto(`${webBase}/system`);
  await page.getByLabel("Owner 访问令牌").fill(token);
  await page.getByRole("button", { name: "连接控制面" }).click();
  await page.getByLabel("对话", { exact: true }).selectOption(conversation.conversation_id);
  await page.getByLabel("已有工作", { exact: true }).selectOption(execution.turn.turn_id);
  await page.getByLabel("成果目标", { exact: true }).fill("Reuse Kanaloa report without replay");
  await page.getByLabel("验收条件（每行一条）").fill("The original report is readable and editable\nThe original native prompt is not repeated");
  await page.getByRole("button", { name: "纳入系统管理" }).click();
  await page.getByRole("button", { name: "核对最新执行结果" }).click();
  await page.getByText("待核验 · 验证：pending", { exact: true }).waitFor();
  checks.push("existing completed Turn becomes Verifying, not Done");
  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: "下载可编辑文件" }).click();
  const download = await downloadPromise;
  const downloaded = await readFile(await download.path(), "utf8");
  assert.match(downloaded, /The boundaries are clear/);
  checks.push("editable Markdown download contains original Agent reply");
  await page.getByLabel("独立核验依据").fill("Browser test independently checked downloaded Markdown and unchanged ACP prompt count");
  await page.getByRole("button", { name: "确认通过验收" }).click();
  await page.getByText("已验证完成 · 验证：passed", { exact: true }).waitFor();
  checks.push("Owner evidence moves Verifying to Done");
  const callsAfter = await request("/__ui__/calls");
  assert.equal(callsAfter.filter(call => call.method === "session/prompt").length, callsBefore.filter(call => call.method === "session/prompt").length);
  checks.push("binding/sync/verification did not replay native Agent work");
  await page.screenshot({ path: path.join(output, "system-desktop.png"), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth), false);
  await page.screenshot({ path: path.join(output, "system-mobile.png"), fullPage: true });
  checks.push("1440px and 390px layout, no horizontal overflow");
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ passed: checks, errors, evidence: output, environment: "Linux Chromium; existing ACP test peer, not real-model/cross-platform acceptance" }, null, 2));
} catch (error) {
  if (page) {
    const body=await page.locator("body").innerText().catch(()=>"");
    await writeFile(path.join(output,"failure.txt"),body);
    await page.screenshot({path:path.join(output,"failure.png"),fullPage:true}).catch(()=>{});
    console.error(JSON.stringify({evidence:output,visibleError:body}));
  }
  throw error;
} finally {
  await browser?.close();
  for (const child of children.reverse()) {
    if (child.exitCode !== null) continue;
    child.kill("SIGTERM");
    await Promise.race([new Promise(resolve => child.once("exit", resolve)), new Promise(resolve => setTimeout(resolve, 5000))]);
    if (child.exitCode === null && child.signalCode === null) child.kill("SIGKILL");
  }
}
