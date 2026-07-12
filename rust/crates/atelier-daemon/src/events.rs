//! Per-project event bus (Phase 7). Placeholder types for early integration.

use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ProjectEvent {
    pub id: String,
    pub project_key: String,
    pub daemon_instance: String,
    pub revision: u64,
    #[serde(rename = "type")]
    pub event_type: String,
    pub timestamp: u64,
    pub payload: Value,
}
