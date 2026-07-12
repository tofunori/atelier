//! Per-project event bus (broadcast + SSE).

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::VecDeque,
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
    time::{SystemTime, UNIX_EPOCH},
};
use tokio::sync::broadcast;
use uuid::Uuid;

const CHANNEL_CAP: usize = 256;
const RING_CAP: usize = 512;

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
    /// Monotonic sequence per project (for Last-Event-ID resume).
    pub seq: u64,
}

#[derive(Clone)]
pub struct ProjectEventBus {
    project_key: String,
    daemon_instance: String,
    tx: broadcast::Sender<ProjectEvent>,
    seq: Arc<AtomicU64>,
    ring: Arc<std::sync::Mutex<VecDeque<ProjectEvent>>>,
}

impl ProjectEventBus {
    pub fn new(project_key: impl Into<String>, daemon_instance: impl Into<String>) -> Self {
        let (tx, _) = broadcast::channel(CHANNEL_CAP);
        Self {
            project_key: project_key.into(),
            daemon_instance: daemon_instance.into(),
            tx,
            seq: Arc::new(AtomicU64::new(0)),
            ring: Arc::new(std::sync::Mutex::new(VecDeque::with_capacity(RING_CAP))),
        }
    }

    pub fn subscribe(&self) -> broadcast::Receiver<ProjectEvent> {
        self.tx.subscribe()
    }

    pub fn publish(&self, event_type: &str, revision: u64, payload: Value) -> ProjectEvent {
        let seq = self.seq.fetch_add(1, Ordering::SeqCst) + 1;
        let event = ProjectEvent {
            id: Uuid::new_v4().to_string(),
            project_key: self.project_key.clone(),
            daemon_instance: self.daemon_instance.clone(),
            revision,
            event_type: event_type.to_string(),
            timestamp: now_secs(),
            payload,
            seq,
        };
        if let Ok(mut ring) = self.ring.lock() {
            if ring.len() >= RING_CAP {
                ring.pop_front();
            }
            ring.push_back(event.clone());
        }
        let _ = self.tx.send(event.clone());
        event
    }

    pub fn ready_event(&self, revision: u64) -> ProjectEvent {
        self.publish("daemon.ready", revision, json!({}))
    }

    /// Events with seq > last_seq (for Last-Event-ID replay).
    pub fn since(&self, last_seq: u64) -> Vec<ProjectEvent> {
        self.ring
            .lock()
            .map(|ring| ring.iter().filter(|e| e.seq > last_seq).cloned().collect())
            .unwrap_or_default()
    }

    pub fn current_seq(&self) -> u64 {
        self.seq.load(Ordering::SeqCst)
    }
}

fn now_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}
