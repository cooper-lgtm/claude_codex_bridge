use std::io::{Read, Write};
#[cfg(unix)]
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::time::Duration;

use serde_json::json;

use crate::model::ProjectViewResponse;

#[derive(Debug)]
pub struct CcbdClient {
    socket_path: std::path::PathBuf,
    timeout: Duration,
}

impl CcbdClient {
    pub fn new(socket_path: impl Into<std::path::PathBuf>) -> Self {
        Self {
            socket_path: socket_path.into(),
            timeout: Duration::from_secs(3),
        }
    }

    pub fn project_view(&self) -> Result<ProjectViewResponse, String> {
        let payload = self.request("project_view", json!({"schema_version": 1}))?;
        serde_json::from_value(payload)
            .map_err(|err| format!("invalid project_view response: {err}"))
    }

    pub fn focus_window(&self, window: &str, namespace_epoch: Option<u64>) -> Result<(), String> {
        let mut request = json!({"window": window});
        if let Some(epoch) = namespace_epoch {
            request["namespace_epoch"] = json!(epoch);
        }
        self.request("project_focus_window", request).map(|_| ())
    }

    pub fn focus_agent(&self, agent: &str, namespace_epoch: Option<u64>) -> Result<(), String> {
        let mut request = json!({"agent": agent});
        if let Some(epoch) = namespace_epoch {
            request["namespace_epoch"] = json!(epoch);
        }
        self.request("project_focus_agent", request).map(|_| ())
    }

    pub fn comms_recover(
        &self,
        job_id: &str,
        reply_delivery_job_id: Option<&str>,
        block_reason: Option<&str>,
    ) -> Result<(), String> {
        let mut request = json!({"job_id": job_id});
        if let Some(reply_job_id) = reply_delivery_job_id {
            request["reply_delivery_job_id"] = json!(reply_job_id);
        }
        if let Some(reason) = block_reason {
            request["block_reason"] = json!(reason);
        }
        self.request("comms_recover", request).map(|_| ())
    }

    pub fn cancel(&self, job_id: &str) -> Result<(), String> {
        self.request("cancel", json!({"job_id": job_id}))
            .map(|_| ())
    }

    pub fn dismiss_comms(&self, comms_id: &str) -> Result<(), String> {
        self.request("project_view_dismiss_comms", json!({"id": comms_id}))
            .map(|_| ())
    }

    pub fn restart_panes(&self) -> Result<(), String> {
        self.request("project_restart_panes", json!({})).map(|_| ())
    }

    fn request(&self, op: &str, request: serde_json::Value) -> Result<serde_json::Value, String> {
        request_unix_socket(&self.socket_path, self.timeout, op, request)
    }
}

#[cfg(unix)]
fn request_unix_socket(
    socket_path: &Path,
    timeout: Duration,
    op: &str,
    request: serde_json::Value,
) -> Result<serde_json::Value, String> {
    let mut stream = UnixStream::connect(socket_path)
        .map_err(|err| format!("connect {}: {err}", socket_path.display()))?;
    stream
        .set_read_timeout(Some(timeout))
        .map_err(|err| format!("set read timeout: {err}"))?;
    stream
        .set_write_timeout(Some(timeout))
        .map_err(|err| format!("set write timeout: {err}"))?;
    let request = json!({"api_version": 2, "op": op, "request": request});
    stream
        .write_all(format!("{request}\n").as_bytes())
        .map_err(|err| format!("write request: {err}"))?;
    let mut data = Vec::new();
    stream
        .read_to_end(&mut data)
        .map_err(|err| format!("read response: {err}"))?;
    let first_line = data
        .split(|byte| *byte == b'\n')
        .next()
        .filter(|line| !line.is_empty())
        .ok_or_else(|| "empty response from ccbd".to_string())?;
    let response: serde_json::Value =
        serde_json::from_slice(first_line).map_err(|err| format!("decode response: {err}"))?;
    if response.get("ok").and_then(|value| value.as_bool()) != Some(true) {
        let error = response
            .get("error")
            .and_then(|value| value.as_str())
            .unwrap_or("ccbd request failed");
        return Err(error.to_string());
    }
    let mut payload = response
        .as_object()
        .cloned()
        .ok_or_else(|| "ccbd response is not an object".to_string())?;
    payload.remove("api_version");
    payload.remove("ok");
    Ok(serde_json::Value::Object(payload))
}

#[cfg(not(unix))]
fn request_unix_socket(
    _socket_path: &Path,
    _timeout: Duration,
    _op: &str,
    _request: serde_json::Value,
) -> Result<serde_json::Value, String> {
    Err("Unix sockets are not supported on this platform".to_string())
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::io::{BufRead, BufReader};
    use std::os::unix::net::UnixListener;
    use std::thread;

    #[test]
    fn project_view_round_trips_over_ccbd_socket_protocol() {
        let (socket_path, handle) = spawn_one_response_server(|request| {
            assert_eq!(request["api_version"], 2);
            assert_eq!(request["op"], "project_view");
            assert_eq!(request["request"]["schema_version"], 1);
            json!({
                "api_version": 2,
                "ok": true,
                "view": {
                    "project": {"display_name": "repo"},
                    "namespace": {"epoch": 4, "active_window": "main"},
                    "windows": [{"name": "main", "active": true, "agents": ["agent1"]}],
                    "agents": [{"name": "agent1", "provider": "codex", "window": "main", "activity_state": "idle"}],
                    "comms": []
                },
                "cache": {"sequence": 9, "ttl_ms": 1000}
            })
        });
        let client = CcbdClient::new(socket_path);

        let response = client.project_view().unwrap();
        handle.join().unwrap();

        assert_eq!(response.cache.sequence, 9);
        assert_eq!(response.view.windows[0].name, "main");
        assert_eq!(response.view.agents[0].name, "agent1");
    }

    #[test]
    fn focus_window_sends_namespace_epoch() {
        let (socket_path, handle) = spawn_one_response_server(|request| {
            assert_eq!(request["op"], "project_focus_window");
            assert_eq!(request["request"]["window"], "ops");
            assert_eq!(request["request"]["namespace_epoch"], 7);
            json!({"api_version": 2, "ok": true, "focus": {"kind": "window"}})
        });
        let client = CcbdClient::new(socket_path);

        client.focus_window("ops", Some(7)).unwrap();
        handle.join().unwrap();
    }

    #[test]
    fn focus_agent_sends_namespace_epoch() {
        let (socket_path, handle) = spawn_one_response_server(|request| {
            assert_eq!(request["op"], "project_focus_agent");
            assert_eq!(request["request"]["agent"], "agent1");
            assert_eq!(request["request"]["namespace_epoch"], 8);
            json!({"api_version": 2, "ok": true, "focus": {"kind": "agent"}})
        });
        let client = CcbdClient::new(socket_path);

        client.focus_agent("agent1", Some(8)).unwrap();
        handle.join().unwrap();
    }

    #[test]
    fn comms_recover_sends_job_and_reply_delivery_target() {
        let (socket_path, handle) = spawn_one_response_server(|request| {
            assert_eq!(request["op"], "comms_recover");
            assert_eq!(request["request"]["job_id"], "job1");
            assert_eq!(request["request"]["reply_delivery_job_id"], "job2");
            assert!(request["request"].get("block_reason").is_none());
            json!({"api_version": 2, "ok": true, "status": "recovered"})
        });
        let client = CcbdClient::new(socket_path);

        client.comms_recover("job1", Some("job2"), None).unwrap();
        handle.join().unwrap();
    }

    #[test]
    fn comms_recover_sends_running_block_reason_hint() {
        let (socket_path, handle) = spawn_one_response_server(|request| {
            assert_eq!(request["op"], "comms_recover");
            assert_eq!(request["request"]["job_id"], "job1");
            assert_eq!(request["request"]["block_reason"], "provider_prompt_idle");
            json!({"api_version": 2, "ok": true, "status": "recovered"})
        });
        let client = CcbdClient::new(socket_path);

        client
            .comms_recover("job1", None, Some("provider_prompt_idle"))
            .unwrap();
        handle.join().unwrap();
    }

    #[test]
    fn cancel_sends_job_id() {
        let (socket_path, handle) = spawn_one_response_server(|request| {
            assert_eq!(request["op"], "cancel");
            assert_eq!(request["request"]["job_id"], "job1");
            json!({"api_version": 2, "ok": true, "status": "cancelled"})
        });
        let client = CcbdClient::new(socket_path);

        client.cancel("job1").unwrap();
        handle.join().unwrap();
    }

    #[test]
    fn dismiss_comms_sends_project_view_dismiss_request() {
        let (socket_path, handle) = spawn_one_response_server(|request| {
            assert_eq!(request["op"], "project_view_dismiss_comms");
            assert_eq!(request["request"]["id"], "job1");
            json!({"api_version": 2, "ok": true, "status": "dismissed"})
        });
        let client = CcbdClient::new(socket_path);

        client.dismiss_comms("job1").unwrap();
        handle.join().unwrap();
    }

    #[test]
    fn restart_panes_sends_project_restart_request() {
        let (socket_path, handle) = spawn_one_response_server(|request| {
            assert_eq!(request["op"], "project_restart_panes");
            assert_eq!(request["request"], json!({}));
            json!({"api_version": 2, "ok": true, "status": "scheduled"})
        });
        let client = CcbdClient::new(socket_path);

        client.restart_panes().unwrap();
        handle.join().unwrap();
    }

    fn spawn_one_response_server<F>(handler: F) -> (std::path::PathBuf, thread::JoinHandle<()>)
    where
        F: FnOnce(serde_json::Value) -> serde_json::Value + Send + 'static,
    {
        let dir = std::env::temp_dir().join(format!(
            "ccb-agent-sidebar-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let socket_path = dir.join("ccbd.sock");
        let listener = UnixListener::bind(&socket_path).unwrap();
        let path_for_thread = socket_path.clone();
        let handle = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut line = String::new();
            {
                let mut reader = BufReader::new(&stream);
                reader.read_line(&mut line).unwrap();
            }
            let request: serde_json::Value = serde_json::from_str(line.trim()).unwrap();
            let response = handler(request);
            stream
                .write_all(format!("{response}\n").as_bytes())
                .unwrap();
            drop(stream);
            let _ = std::fs::remove_file(path_for_thread);
            let _ = std::fs::remove_dir(dir);
        });
        (socket_path, handle)
    }
}
