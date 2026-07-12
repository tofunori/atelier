//! In-memory multi-project runtime host.
#![allow(dead_code)]

use crate::{
    events::ProjectEventBus,
    lifecycle::ProjectLifecycle,
    registry::ProjectRegistry,
};
use atelier_server::ProjectRuntime;
use serde_json::{Value, json};
use std::{
    collections::HashMap,
    path::PathBuf,
    sync::Arc,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use tokio::sync::RwLock;

#[derive(Clone)]
pub struct LiveProject {
    pub key: String,
    pub runtime: ProjectRuntime,
    pub lifecycle: ProjectLifecycle,
    pub last_activity: Instant,
    pub fault: Option<String>,
    pub events: ProjectEventBus,
}

#[derive(Clone)]
pub struct ProjectHost {
    registry: ProjectRegistry,
    live: Arc<RwLock<HashMap<String, LiveProject>>>,
    idle_grace: Duration,
    suspend_after: Duration,
    daemon_instance: String,
}

impl ProjectHost {
    pub fn new(
        registry: ProjectRegistry,
        idle_grace_secs: u64,
        suspend_after_secs: u64,
        daemon_instance: impl Into<String>,
    ) -> Self {
        Self {
            registry,
            live: Arc::new(RwLock::new(HashMap::new())),
            idle_grace: Duration::from_secs(idle_grace_secs),
            suspend_after: Duration::from_secs(suspend_after_secs),
            daemon_instance: daemon_instance.into(),
        }
    }

    pub async fn event_bus(&self, key: &str) -> Option<ProjectEventBus> {
        self.live.read().await.get(key).map(|e| e.events.clone())
    }

    pub async fn publish(
        &self,
        key: &str,
        event_type: &str,
        payload: Value,
    ) -> Option<crate::events::ProjectEvent> {
        let live = self.live.read().await;
        let entry = live.get(key)?;
        let revision = *entry.runtime.revision().read().await;
        Some(entry.events.publish(event_type, revision, payload))
    }

    pub async fn activate(&self, key: &str) -> Result<ProjectRuntime, HostError> {
        {
            let mut live = self.live.write().await;
            if let Some(entry) = live.get_mut(key) {
                match entry.lifecycle {
                    ProjectLifecycle::Faulted => {
                        return Err(HostError::Faulted(
                            entry.fault.clone().unwrap_or_else(|| "faulted".into()),
                        ));
                    }
                    ProjectLifecycle::Suspended | ProjectLifecycle::RegisteredIdle => {
                        entry.lifecycle = ProjectLifecycle::Active;
                    }
                    ProjectLifecycle::Starting => {
                        entry.lifecycle = ProjectLifecycle::Active;
                    }
                    ProjectLifecycle::Active | ProjectLifecycle::IdleGrace => {}
                    ProjectLifecycle::Removed => {
                        return Err(HostError::NotFound);
                    }
                }
                entry.last_activity = Instant::now();
                self.registry.touch_activity(key).await;
                return Ok(entry.runtime.clone());
            }
        }

        let Some(meta) = self.registry.get(key).await else {
            return Err(HostError::NotFound);
        };
        let root = PathBuf::from(&meta.canonical_root);
        if !root.is_dir() {
            let mut live = self.live.write().await;
            live.insert(
                key.to_string(),
                LiveProject {
                    key: key.to_string(),
                    runtime: ProjectRuntime::new_legacy(
                        root.clone(),
                        0,
                        String::new(),
                        false,
                        false,
                    ),
                    lifecycle: ProjectLifecycle::Faulted,
                    last_activity: Instant::now(),
                    fault: Some(format!("root missing: {}", root.display())),
                    events: ProjectEventBus::new(key, &self.daemon_instance),
                },
            );
            return Err(HostError::RootMissing(root.display().to_string()));
        }

        let runtime = ProjectRuntime::new_legacy(root, 0, String::new(), false, false);
        let mut live = self.live.write().await;
        live.insert(
            key.to_string(),
            LiveProject {
                key: key.to_string(),
                runtime: runtime.clone(),
                lifecycle: ProjectLifecycle::Active,
                last_activity: Instant::now(),
                fault: None,
                events: ProjectEventBus::new(key, &self.daemon_instance),
            },
        );
        self.registry.touch_activity(key).await;
        Ok(runtime)
    }

    pub async fn suspend(&self, key: &str) -> Result<(), HostError> {
        let mut live = self.live.write().await;
        let Some(entry) = live.get_mut(key) else {
            return if self.registry.get(key).await.is_some() {
                Ok(())
            } else {
                Err(HostError::NotFound)
            };
        };
        entry.lifecycle = ProjectLifecycle::Suspended;
        Ok(())
    }

    pub async fn forget(&self, key: &str) -> bool {
        self.live.write().await.remove(key);
        self.registry.forget(key).await
    }

    pub async fn status(&self, key: &str) -> Option<Value> {
        let meta = self.registry.get(key).await?;
        let live = self.live.read().await;
        let (state, watcher_running, revision, fault) = if let Some(entry) = live.get(key) {
            let revision = *entry.runtime.revision().read().await;
            let watcher = entry.runtime.watcher().read().await.clone();
            (
                lifecycle_name(entry.lifecycle),
                watcher.running,
                revision,
                entry.fault.clone(),
            )
        } else {
            ("registeredIdle", false, 0, None)
        };
        Some(json!({
            "key": key,
            "displayName": meta.display_name,
            "state": state,
            "revision": revision,
            "watcher": {
                "running": watcher_running,
                "lastEventAt": null,
                "lastError": fault,
            },
            "clients": {
                "browserSessions": 0,
                "codexConsumers": 0,
            },
            "jobs": {
                "reconcile": false,
                "latex": 0,
                "thumbnail": 0,
            },
            "pinned": meta.pinned,
            "lastActivityAt": meta.last_activity_at,
        }))
    }

    pub async fn list_public(&self) -> Vec<Value> {
        let mut out = Vec::new();
        for (key, meta) in self.registry.list().await {
            let state = self
                .live
                .read()
                .await
                .get(&key)
                .map(|entry| lifecycle_name(entry.lifecycle))
                .unwrap_or("registeredIdle");
            out.push(json!({
                "key": key,
                "displayName": meta.display_name,
                "state": state,
                "pinned": meta.pinned,
                "lastOpenedAt": meta.last_opened_at,
            }));
        }
        out
    }

    /// Sweep idle projects into IdleGrace / Suspended (no auto-suspend for pinned).
    pub async fn sweep_idle(&self) {
        let now = Instant::now();
        let mut live = self.live.write().await;
        for (key, entry) in live.iter_mut() {
            let pinned = self
                .registry
                .get(key)
                .await
                .map(|meta| meta.pinned)
                .unwrap_or(false);
            if pinned {
                continue;
            }
            let idle = now.duration_since(entry.last_activity);
            match entry.lifecycle {
                ProjectLifecycle::Active if idle >= self.idle_grace => {
                    entry.lifecycle = ProjectLifecycle::IdleGrace;
                }
                ProjectLifecycle::IdleGrace if idle >= self.suspend_after => {
                    entry.lifecycle = ProjectLifecycle::Suspended;
                }
                _ => {}
            }
        }
    }

    pub fn idle_grace(&self) -> Duration {
        self.idle_grace
    }

    pub fn suspend_after(&self) -> Duration {
        self.suspend_after
    }
}

#[derive(Debug)]
pub enum HostError {
    NotFound,
    RootMissing(String),
    Faulted(String),
}

impl HostError {
    pub fn code(&self) -> &'static str {
        match self {
            Self::NotFound => "PROJECT_NOT_FOUND",
            Self::RootMissing(_) => "PROJECT_ROOT_MISSING",
            Self::Faulted(_) => "PROJECT_FAULTED",
        }
    }

    pub fn message(&self) -> String {
        match self {
            Self::NotFound => "project not found".into(),
            Self::RootMissing(path) => format!("project root missing: {path}"),
            Self::Faulted(reason) => reason.clone(),
        }
    }

    pub fn http_status(&self) -> u16 {
        match self {
            Self::NotFound => 404,
            Self::RootMissing(_) => 409,
            Self::Faulted(_) => 503,
        }
    }
}

fn lifecycle_name(state: ProjectLifecycle) -> &'static str {
    match state {
        ProjectLifecycle::RegisteredIdle => "registeredIdle",
        ProjectLifecycle::Starting => "starting",
        ProjectLifecycle::Active => "active",
        ProjectLifecycle::IdleGrace => "idleGrace",
        ProjectLifecycle::Suspended => "suspended",
        ProjectLifecycle::Faulted => "faulted",
        ProjectLifecycle::Removed => "removed",
    }
}

#[allow(dead_code)]
fn now_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}
