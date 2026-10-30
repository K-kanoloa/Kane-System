//! Local-only streaming transport. Original Harness providers own protocol mapping.
use crate::{AnyResult, service};
use serde_json::{Value, json};
use std::{
    fs,
    io::{BufRead, BufReader, Read, Write},
    os::unix::{
        fs::{MetadataExt, PermissionsExt},
        net::{UnixListener, UnixStream},
    },
    path::Path,
    process::{Child, Command, Stdio},
    thread,
    time::Duration,
};

struct ChildGuard(Child);
impl Drop for ChildGuard {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}
fn curl(endpoint: &str) -> Command {
    let mut c = Command::new("/usr/bin/curl");
    // Only literal numeric-loopback endpoints approved by Core. No environment
    // credentials/proxies, redirects, URL schemes or retries from Agent input.
    c.env_clear().env("PATH", "/usr/bin:/bin").args([
        "--silent",
        "--show-error",
        "--fail",
        "--no-buffer",
        "--retry",
        "0",
        "--connect-timeout",
        "2",
        "--max-time",
        "45",
        "--noproxy",
        "127.0.0.1",
        "--url",
        endpoint,
    ]);
    c
}
pub fn health(endpoint: &str, model: &str) -> AnyResult<()> {
    let output = curl(&format!("{endpoint}/health"))
        .args(["--max-time", "2"])
        .output()?;
    if !output.status.success() {
        return Err("model health failed".into());
    }
    let value: Value = serde_json::from_slice(&output.stdout)?;
    if value["status"] != "ok" {
        return Err("backend health not ready".into());
    }
    let output = curl(&format!("{endpoint}/v1/models"))
        .args(["--max-time", "2"])
        .output()?;
    let value: Value = serde_json::from_slice(&output.stdout)?;
    if !output.status.success()
        || !value["data"]
            .as_array()
            .is_some_and(|xs| xs.iter().any(|v| v["id"] == model))
    {
        return Err("backend model identity differs".into());
    }
    Ok(())
}
fn header_line(reader: &mut impl BufRead) -> AnyResult<String> {
    let mut bytes = Vec::new();
    loop {
        let available = reader.fill_buf()?;
        if available.is_empty() {
            return Err("incomplete HTTP headers".into());
        }
        let end = available
            .iter()
            .position(|b| *b == b'\n')
            .map(|i| i + 1)
            .unwrap_or(available.len());
        if bytes.len() + end > 16384 {
            return Err("HTTP header line bound".into());
        }
        bytes.extend_from_slice(&available[..end]);
        reader.consume(end);
        if bytes.last() == Some(&b'\n') {
            return Ok(String::from_utf8(bytes)?);
        }
    }
}
fn handle(core: &Path, mut client: UnixStream) -> AnyResult<()> {
    client.set_read_timeout(Some(Duration::from_secs(5)))?;
    client.set_write_timeout(Some(Duration::from_secs(3)))?;
    let mut reader = BufReader::new(client.try_clone()?);
    let mut line = header_line(&mut reader)?;
    if line.trim_end() != "POST /v1/chat/completions HTTP/1.1" {
        return Err("gateway method/path refused".into());
    }
    let mut token = None;
    let mut length = None;
    let mut headers_size = line.len();
    loop {
        line = header_line(&mut reader)?;
        headers_size += line.len();
        if headers_size > 16384 || line.is_empty() {
            return Err("gateway header bounds".into());
        }
        if line == "\r\n" {
            break;
        }
        let (name, value) = line.split_once(':').ok_or("invalid header")?;
        if name.eq_ignore_ascii_case("authorization") {
            if token.is_some() {
                return Err("duplicate authorization".into());
            }
            token = Some(
                value
                    .trim()
                    .strip_prefix("Bearer ")
                    .ok_or("permit required")?
                    .to_owned(),
            );
        }
        if name.eq_ignore_ascii_case("content-length") {
            if length.is_some() {
                return Err("duplicate length".into());
            }
            length = Some(value.trim().parse::<usize>()?);
        }
        if name.eq_ignore_ascii_case("transfer-encoding") {
            return Err("chunked request unsupported".into());
        }
    }
    let length = length
        .filter(|n| *n > 0 && *n <= 1_500_000)
        .ok_or("bounded JSON body required")?;
    let token = token.ok_or("single-use permit required")?;
    let mut bytes = vec![0; length];
    reader.read_exact(&mut bytes)?;
    let mut body: Value = serde_json::from_slice(&bytes)?;
    // Durable single-use claim and live policy checks precede every backend call.
    let permit = service::request(
        core,
        &json!({"op":"claim_gateway","token":token,"body":body}),
    )?;
    let result = (|| -> AnyResult<Value> {
        let endpoint = permit["endpoint"].as_str().ok_or("endpoint missing")?;
        body["model"] = permit["backend_model"].clone();
        body["stream_options"] = json!({"include_usage":true});
        if permit["forced_tool"] == "required" {
            body["tool_choice"] = json!("required");
            let path = permit["required_read_path"]
                .as_str()
                .ok_or("required read path missing")?;
            if let Some(messages) = body["messages"].as_array_mut() {
                messages.push(json!({"role":"system","content":format!("The workspace was edited by its human Owner. All previous file observations are stale. Your next response MUST call the read tool with arguments {}. Do not answer in text until that new tool result is available. Never reuse old file content.",serde_json::to_string(&json!({"file_path":path}))?)}));
            }
            if let Some(tools) = body["tools"].as_array_mut() {
                tools.retain(|t| t["type"] == "function" && t["function"]["name"] == "read");
            }
        }
        // Cache is explicitly disabled at the actual backend boundary; do not
        // expose opaque agent request fields such as slots, URLs or cache IDs.
        let allowed = [
            "model",
            "messages",
            "tools",
            "tool_choice",
            "temperature",
            "top_p",
            "max_tokens",
            "stream",
            "stream_options",
            "stop",
            "seed",
            "presence_penalty",
            "frequency_penalty",
        ];
        body.as_object_mut()
            .ok_or("object required")?
            .retain(|k, _| allowed.contains(&k.as_str()));
        body["cache_prompt"] = json!(false);
        let mut child = ChildGuard(
            curl(&format!("{endpoint}/v1/chat/completions"))
                .args([
                    "-H",
                    "Content-Type: application/json",
                    "--data-binary",
                    "@-",
                    "--max-time",
                    &permit["timeout_seconds"].as_u64().unwrap_or(45).to_string(),
                ])
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(Stdio::null())
                .spawn()?,
        );
        child
            .0
            .stdin
            .take()
            .ok_or("stdin")?
            .write_all(&serde_json::to_vec(&body)?)?;
        client.write_all(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n")?;
        let mut stdout = BufReader::new(child.0.stdout.take().ok_or("stdout")?);
        let mut usage = None;
        let mut done = false;
        let mut finish = false;
        let mut total = 0;
        loop {
            let mut line = Vec::new();
            let n = Read::by_ref(&mut stdout)
                .take(1_500_001)
                .read_until(b'\n', &mut line)?;
            if n == 0 {
                break;
            }
            total += n;
            if n > 1_500_000 || total > 8_000_000 {
                return Err("backend response bound".into());
            }
            if let Some(data) = line.strip_prefix(b"data: ") {
                let text = std::str::from_utf8(data)?.trim();
                if text == "[DONE]" {
                    done = true;
                    continue;
                }
                let chunk: Value = serde_json::from_str(text)?;
                if !chunk["usage"].is_null() {
                    usage = Some(chunk["usage"].clone());
                }
                if chunk["choices"].as_array().is_some_and(|xs| {
                    xs.iter().any(|c| {
                        matches!(
                            c["finish_reason"].as_str(),
                            Some("stop" | "length" | "tool_calls")
                        )
                    })
                }) {
                    finish = true;
                }
            }
            client.write_all(&line)?;
        }
        let status = child.0.wait()?;
        if !status.success() || !done || !finish {
            return Err("incomplete provider stream".into());
        }
        let u = usage.ok_or("final usage absent")?;
        let input = u["prompt_tokens"].as_u64().ok_or("invalid input usage")?;
        let output = u["completion_tokens"]
            .as_u64()
            .ok_or("invalid output usage")?;
        let sum = u["total_tokens"].as_u64().ok_or("invalid total usage")?;
        if sum > 9_007_199_254_740_991 || input.checked_add(output) != Some(sum) {
            return Err("inconsistent usage".into());
        }
        Ok(
            json!({"outcome":"completed","usage":{"inputTokens":input,"outputTokens":output,"totalTokens":sum}}),
        )
    })();
    if result.is_err()
        && health(
            permit["endpoint"].as_str().ok_or("endpoint")?,
            permit["backend_model"].as_str().ok_or("backend")?,
        )
        .is_err()
    {
        let _ = service::request(
            core,
            &json!({"op":"model_lifecycle","id":permit["model_instance_id"],"generation":permit["generation"],"state":"Failed","evidence":"gateway observed backend health loss; no replay or resource refund"}),
        );
    }
    let receipt = result
        .as_ref()
        .cloned()
        .unwrap_or_else(|_| json!({"outcome":"unknown","usage":null}));
    let settled = service::request(
        core,
        &json!({"op":"gateway_result","token":token,"receipt":receipt}),
    )?;
    if result.is_err() || settled["state"] != "Settled" {
        return Err("gateway outcome unresolved; no retry/refund".into());
    }
    client.write_all(b"data: [DONE]\n\n")?;
    Ok(())
}
pub fn serve(core: &Path, socket: &Path) -> AnyResult<()> {
    if !socket.is_absolute()
        || fs::metadata(socket.parent().ok_or("directory")?)?
            .permissions()
            .mode()
            & 0o077
            != 0
    {
        return Err("private gateway directory required".into());
    }
    // No unlink of existing sockets, no silent takeover or fallback.
    let listener = UnixListener::bind(socket)?;
    fs::set_permissions(socket, fs::Permissions::from_mode(0o600))?;
    let metadata = fs::metadata(socket)?;
    service::request(
        core,
        &json!({"op":"register_model_listener","path":socket,"pid":std::process::id(),"device":metadata.dev(),"inode":metadata.ino()}),
    )?;
    for connection in listener.incoming() {
        let client = connection?;
        let core = core.to_owned();
        let peer = rustix::net::sockopt::socket_peercred(&client)?;
        if peer.uid != rustix::process::geteuid() {
            continue;
        }
        // The durable shared queue bounds in-flight permits; limit simultaneous
        // parsing threads independently, including unauthorized local clients.
        static ACTIVE: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
        use std::sync::atomic::Ordering;
        if ACTIVE.fetch_add(1, Ordering::SeqCst) >= 32 {
            ACTIVE.fetch_sub(1, Ordering::SeqCst);
            continue;
        }
        thread::spawn(move || {
            if let Err(error) = handle(&core, client) {
                eprintln!("model gateway rejected or incomplete: {error}");
            }
            ACTIVE.fetch_sub(1, Ordering::SeqCst);
        });
    }
    Ok(())
}
