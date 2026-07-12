//! Per-project runtime state shared by legacy mono-server and multi-project daemon.

use crate::{
    agent::AgentStore,
    gallery::EventStore,
    workspace::BoardQueue,
    zotero::ZoteroCache,
};
use atelier_core::{WatcherStatus, artifact_snapshot};
use std::{
    path::{Path, PathBuf},
    sync::Arc,
};
use tokio::sync::{Mutex, RwLock, Semaphore};

/// Isolated state for one project root.
///
/// Legacy `atelier-server` holds exactly one of these. The daemon will hold many.
#[derive(Clone)]
pub struct ProjectRuntime {
    pub(crate) root: PathBuf,
    pub(crate) port: u16,
    pub(crate) revision: Arc<RwLock<u64>>,
    pub(crate) watcher: Arc<RwLock<WatcherStatus>>,
    pub(crate) agent: Arc<Mutex<AgentStore>>,
    pub(crate) rebuild_lock: Arc<Mutex<()>>,
    pub(crate) agent_token: String,
    pub(crate) remote: bool,
    /// Toast events for GET/POST /agent-events (cap 100, like Python).
    pub(crate) events: Arc<Mutex<EventStore>>,
    /// Concurrency caps for thumbnail tools and headless Chrome.
    pub(crate) thumb_sem: Arc<Semaphore>,
    pub(crate) chrome_sem: Arc<Semaphore>,
    /// Whiteboard command queue (drained by GET /board/poll).
    pub(crate) board: Arc<Mutex<BoardQueue>>,
    /// Serialize notes/board disk writes.
    pub(crate) workspace_lock: Arc<Mutex<()>>,
    /// Zotero readonly copy mtime cache.
    pub(crate) zotero: Arc<std::sync::Mutex<ZoteroCache>>,
}

impl ProjectRuntime {
    /// Build a mono-project runtime for the legacy server adapter.
    pub fn new_legacy(
        root: PathBuf,
        port: u16,
        agent_token: String,
        remote: bool,
        watch_enabled: bool,
    ) -> Self {
        let initial_revision = artifact_snapshot(&root)
            .map(|snapshot| snapshot.len() as u64)
            .unwrap_or_default();
        let cpu = std::thread::available_parallelism()
            .map(|n| n.get())
            .unwrap_or(4);
        let thumb_permits = cpu.clamp(2, 8);
        Self {
            root: root.clone(),
            port,
            revision: Arc::new(RwLock::new(initial_revision)),
            watcher: Arc::new(RwLock::new(WatcherStatus {
                enabled: watch_enabled,
                ..Default::default()
            })),
            agent: Arc::new(Mutex::new(AgentStore::load(&root))),
            rebuild_lock: Arc::new(Mutex::new(())),
            agent_token,
            remote,
            events: Arc::new(Mutex::new(EventStore::new())),
            thumb_sem: Arc::new(Semaphore::new(thumb_permits)),
            chrome_sem: Arc::new(Semaphore::new(2)),
            board: Arc::new(Mutex::new(BoardQueue::default())),
            workspace_lock: Arc::new(Mutex::new(())),
            zotero: Arc::new(std::sync::Mutex::new(ZoteroCache::default())),
        }
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    pub fn revision(&self) -> Arc<RwLock<u64>> {
        self.revision.clone()
    }

    pub fn watcher(&self) -> Arc<RwLock<WatcherStatus>> {
        self.watcher.clone()
    }

    /// Shared access for daemon control methods (annotations, consumers).
    pub fn agent(&self) -> Arc<Mutex<AgentStore>> {
        self.agent.clone()
    }

    pub fn rebuild_lock(&self) -> Arc<Mutex<()>> {
        self.rebuild_lock.clone()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::time::{SystemTime, UNIX_EPOCH};

    #[test]
    fn runtime_binds_to_the_given_root() {
        let root = std::env::temp_dir().join(format!(
            "atelier-runtime-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        fs::write(root.join("notes.md"), b"# t\n").unwrap();
        let runtime = ProjectRuntime::new_legacy(root.clone(), 9360, String::new(), false, false);
        assert_eq!(runtime.root(), root.as_path());
        assert!(!runtime.remote);
        assert_eq!(runtime.port, 9360);
        let _ = fs::remove_dir_all(root);
    }
}
