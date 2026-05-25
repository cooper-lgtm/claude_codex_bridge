use serde::Deserialize;
use serde_json::Value;

#[derive(Debug, Clone, Deserialize, Default, PartialEq)]
pub struct ProjectViewResponse {
    pub view: ProjectView,
    #[serde(default)]
    pub cache: ProjectViewCache,
}

#[derive(Debug, Clone, Deserialize, Default, PartialEq)]
pub struct ProjectViewCache {
    #[serde(default)]
    pub sequence: u64,
    #[serde(default)]
    pub ttl_ms: u64,
}

#[derive(Debug, Clone, Deserialize, Default, PartialEq)]
pub struct ProjectView {
    #[serde(default)]
    pub project: ProjectInfo,
    #[serde(default)]
    pub namespace: NamespaceInfo,
    #[serde(default)]
    pub windows: Vec<WindowView>,
    #[serde(default)]
    pub agents: Vec<AgentView>,
    #[serde(default)]
    pub comms: Vec<CommsItem>,
}

#[derive(Debug, Clone, Deserialize, Default, PartialEq)]
pub struct ProjectInfo {
    #[serde(default)]
    pub display_name: String,
    #[serde(default)]
    pub root: String,
}

#[derive(Debug, Clone, Deserialize, Default, PartialEq)]
pub struct NamespaceInfo {
    #[serde(default)]
    pub epoch: Option<u64>,
    #[serde(default)]
    pub active_window: Option<String>,
    #[serde(default)]
    pub entry_window: String,
}

#[derive(Debug, Clone, Deserialize, Default, PartialEq)]
pub struct WindowView {
    pub name: String,
    #[serde(default)]
    pub order: u64,
    #[serde(default)]
    pub active: bool,
    #[serde(default)]
    pub tmux_window_id: Option<String>,
    #[serde(default)]
    pub tmux_window_index: Option<u64>,
    #[serde(default)]
    pub sidebar_pane_id: Option<String>,
    #[serde(default)]
    pub agents: Vec<String>,
}

#[derive(Debug, Clone, Deserialize, Default, PartialEq)]
pub struct AgentView {
    pub name: String,
    #[serde(default)]
    pub provider: String,
    #[serde(default)]
    pub window: String,
    #[serde(default)]
    pub order: u64,
    #[serde(default)]
    pub pane_id: Option<String>,
    #[serde(default)]
    pub active: bool,
    #[serde(default)]
    pub activity_state: String,
    #[serde(default)]
    pub activity_symbol: Option<String>,
    #[serde(default)]
    pub activity_color: Option<String>,
    #[serde(default)]
    pub current_job_id: Option<String>,
    #[serde(default)]
    pub reason: Option<String>,
    #[serde(default)]
    pub queue_depth: u64,
}

#[derive(Debug, Clone, Deserialize, Default, PartialEq)]
pub struct CommsItem {
    pub id: String,
    #[serde(default)]
    pub short_id: String,
    #[serde(default)]
    pub sender: String,
    #[serde(default)]
    pub target: String,
    #[serde(default)]
    pub status: String,
    #[serde(default)]
    pub business_status: String,
    #[serde(default)]
    pub status_label: String,
    #[serde(default)]
    pub body_preview: String,
    #[serde(default)]
    pub reply_status: Option<String>,
    #[serde(default)]
    pub reply_delivery_job_id: Option<String>,
    #[serde(default)]
    pub short_reason: Option<String>,
    #[serde(default)]
    pub callback: bool,
    #[serde(default)]
    pub recoverable: bool,
    #[serde(default)]
    pub recover_target: Option<Value>,
    #[serde(default)]
    pub block_reason: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RowTarget {
    Window(String),
    Agent(String),
}

pub fn row_targets(view: &ProjectView) -> Vec<RowTarget> {
    let mut targets = Vec::new();
    for window in &view.windows {
        targets.push(RowTarget::Window(window.name.clone()));
        for agent in view
            .agents
            .iter()
            .filter(|agent| agent.window == window.name)
        {
            targets.push(RowTarget::Agent(agent.name.clone()));
        }
    }
    targets
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_project_view_response() {
        let payload = r#"{
          "view": {
            "project": {"display_name": "repo"},
            "namespace": {"epoch": 3, "active_window": "main"},
            "windows": [{"name": "main", "active": true, "agents": ["agent1"]}],
            "agents": [{"name": "agent1", "provider": "codex", "window": "main", "activity_state": "idle"}],
            "comms": [{
              "id": "job1",
              "sender": "user",
              "target": "agent1",
              "status": "running",
              "business_status": "replying",
              "status_label": "work",
              "body_preview": "work",
              "recoverable": true,
              "recover_target": {"job_id": "job1", "reply_delivery_job_id": null},
              "block_reason": "pane_dead"
            }]
          },
          "cache": {"sequence": 7, "ttl_ms": 1000}
        }"#;

        let response: ProjectViewResponse = serde_json::from_str(payload).unwrap();

        assert_eq!(response.cache.sequence, 7);
        assert_eq!(response.view.project.display_name, "repo");
        assert_eq!(response.view.comms[0].status_label, "work");
        assert_eq!(response.view.comms[0].body_preview, "work");
        assert!(response.view.comms[0].recoverable);
        assert_eq!(
            response.view.comms[0].block_reason.as_deref(),
            Some("pane_dead")
        );
        assert_eq!(
            row_targets(&response.view),
            vec![
                RowTarget::Window("main".into()),
                RowTarget::Agent("agent1".into())
            ]
        );
    }
}
