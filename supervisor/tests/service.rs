#![cfg(unix)]
use serde_json::{Value, json};
use std::{
    fs,
    io::{Read, Write},
    os::unix::{fs::PermissionsExt, net::UnixStream},
    path::Path,
    process::{Child, Command, Stdio},
    thread,
    time::{Duration, Instant},
};

const BIN: &str = env!("CARGO_BIN_EXE_kanesys");
struct Server(Child);
impl Drop for Server {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}
fn start(db: &Path, socket: &Path) -> Server {
    // The Core correctly refuses a socket in a group/world-accessible parent.
    // Test runners use different umasks, so establish the required boundary
    // explicitly instead of depending on the host's tempfile permissions.
    fs::set_permissions(
        socket.parent().expect("socket parent"),
        fs::Permissions::from_mode(0o700),
    )
    .unwrap();
    let child = Command::new(BIN)
        .args([
            "--db",
            db.to_str().unwrap(),
            "--socket",
            socket.to_str().unwrap(),
            "serve",
        ])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let mut server = Server(child);
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        assert!(server.0.try_wait().unwrap().is_none(), "service exited");
        if UnixStream::connect(socket).is_ok() {
            break;
        }
        assert!(Instant::now() < deadline, "service did not start");
        thread::sleep(Duration::from_millis(10));
    }
    server
}
fn call(socket: &Path, body: Value) -> Value {
    let mut stream = UnixStream::connect(socket).unwrap();
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    let bytes = serde_json::to_vec(&body).unwrap();
    stream
        .write_all(&(bytes.len() as u32).to_be_bytes())
        .unwrap();
    stream.write_all(&bytes).unwrap();
    let mut header = [0; 4];
    stream.read_exact(&mut header).unwrap();
    let size = u32::from_be_bytes(header) as usize;
    assert!(size < 16_000_000);
    let mut bytes = vec![0; size];
    stream.read_exact(&mut bytes).unwrap();
    serde_json::from_slice(&bytes).unwrap()
}
fn request(socket: &Path, body: Value) -> Value {
    call(socket, json!({"protocol":1,"request":body}))
}

#[test]
fn service_retains_state_after_sigkill_and_backup_restores_offline() {
    let temp = tempfile::tempdir().unwrap();
    let db = temp.path().join("state.db");
    let sock = temp.path().join("core.sock");
    let server = start(&db, &sock);
    assert_eq!(
        fs::metadata(&sock).unwrap().permissions().mode() & 0o777,
        0o600
    );
    let ws = request(&sock, json!({"op":"create_workspace","kind":"document"}));
    let id = ws["result"]["id"].as_str().unwrap();
    assert_eq!(ws["ok"], true);
    let backup = temp.path().join("snapshot.db");
    assert_eq!(
        request(&sock, json!({"op":"backup","destination":backup}))["ok"],
        true
    );
    // Drop kills the service rather than arranging graceful SQLite shutdown.
    drop(server);
    let server = start(&db, &sock);
    assert_eq!(
        request(&sock, json!({"op":"workspace","id":id}))["result"]["id"],
        id
    );
    drop(server);
    let restored = temp.path().join("restored.db");
    let result = Command::new(BIN)
        .args([
            "--db",
            restored.to_str().unwrap(),
            "restore",
            backup.to_str().unwrap(),
        ])
        .output()
        .unwrap();
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    let _server = start(&restored, &sock);
    assert_eq!(
        request(&sock, json!({"op":"workspace","id":id}))["result"]["id"],
        id
    );
}

#[test]
fn bad_protocol_identity_and_oversized_requests_do_not_stop_core() {
    let temp = tempfile::tempdir().unwrap();
    let db = temp.path().join("state.db");
    let sock = temp.path().join("core.sock");
    let _server = start(&db, &sock);
    assert_eq!(
        call(&sock, json!({"protocol":2,"request":{"op":"health"}}))["ok"],
        false
    );
    assert_eq!(
        request(&sock, json!({"op":"tasks","actor":"Owner"}))["ok"],
        false
    );
    let missing = request(
        &sock,
        json!({"op":"workspace","id":"secret-value-not-in-error"}),
    );
    assert_eq!(missing["error"]["status"], 404);
    assert!(!missing.to_string().contains("secret-value"));
    let mut stream = UnixStream::connect(&sock).unwrap();
    stream.write_all(&2_000_001u32.to_be_bytes()).unwrap();
    let mut header = [0; 4];
    stream.read_exact(&mut header).unwrap();
    let mut bytes = vec![0; u32::from_be_bytes(header) as usize];
    stream.read_exact(&mut bytes).unwrap();
    assert_eq!(
        serde_json::from_slice::<Value>(&bytes).unwrap()["ok"],
        false
    );
    assert_eq!(request(&sock, json!({"op":"health"}))["ok"], true);
}

#[test]
fn duplicate_service_and_unsafe_socket_directory_fail_closed() {
    let temp = tempfile::tempdir().unwrap();
    let db = temp.path().join("state.db");
    let sock = temp.path().join("core.sock");
    let _server = start(&db, &sock);
    let second = Command::new(BIN)
        .args([
            "--db",
            db.to_str().unwrap(),
            "--socket",
            temp.path().join("other.sock").to_str().unwrap(),
            "serve",
        ])
        .output()
        .unwrap();
    assert!(!second.status.success());
    assert_eq!(request(&sock, json!({"op":"health"}))["ok"], true);
    let unsafe_dir = temp.path().join("public");
    fs::create_dir(&unsafe_dir).unwrap();
    fs::set_permissions(&unsafe_dir, fs::Permissions::from_mode(0o755)).unwrap();
    let unsafe_result = Command::new(BIN)
        .args([
            "--db",
            temp.path().join("other.db").to_str().unwrap(),
            "--socket",
            unsafe_dir.join("core.sock").to_str().unwrap(),
            "serve",
        ])
        .output()
        .unwrap();
    assert!(!unsafe_result.status.success());
    let collision = temp.path().join("keep-me");
    fs::write(&collision, b"keep").unwrap();
    let result = Command::new(BIN)
        .args([
            "--db",
            temp.path().join("another.db").to_str().unwrap(),
            "--socket",
            collision.to_str().unwrap(),
            "serve",
        ])
        .output()
        .unwrap();
    assert!(!result.status.success());
    assert_eq!(fs::read(&collision).unwrap(), b"keep");
}

#[test]
fn configured_socket_does_not_fall_back_to_database() {
    let temp = tempfile::tempdir().unwrap();
    let db = temp.path().join("must-not-exist.db");
    let mut child = Command::new(BIN)
        .args([
            "--db",
            db.to_str().unwrap(),
            "--socket",
            temp.path().join("missing.sock").to_str().unwrap(),
            "host-request",
        ])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    child
        .stdin
        .take()
        .unwrap()
        .write_all(b"{\"op\":\"tasks\"}")
        .unwrap();
    assert!(!child.wait().unwrap().success());
    assert!(!db.exists());
}

#[test]
fn incomplete_frame_has_a_deadline_and_does_not_hold_service_forever() {
    let temp = tempfile::tempdir().unwrap();
    let db = temp.path().join("state.db");
    let sock = temp.path().join("core.sock");
    let _server = start(&db, &sock);
    let mut incomplete = UnixStream::connect(&sock).unwrap();
    incomplete.write_all(&100u32.to_be_bytes()).unwrap();
    incomplete.write_all(b"{").unwrap();
    let started = Instant::now();
    assert_eq!(request(&sock, json!({"op":"health"}))["ok"], true);
    assert!(started.elapsed() < Duration::from_secs(4));
}
