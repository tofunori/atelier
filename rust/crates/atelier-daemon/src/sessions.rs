//! Browser session store (opaque cookies). Expanded in Phase 4.
#![allow(dead_code)]

use serde::{Deserialize, Serialize};
use std::{collections::BTreeMap, path::PathBuf, sync::Arc};
use tokio::sync::RwLock;

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
#[serde(rename_all = "camelCase")]
pub struct SessionsFile {
    pub schema_version: u32,
    pub sessions: BTreeMap<String, SessionRecord>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SessionRecord {
    pub project_key: String,
    pub consumer_id: Option<String>,
    pub created_at: u64,
    pub expires_at: u64,
}

#[derive(Clone, Default)]
pub struct SessionStore {
    path: PathBuf,
    inner: Arc<RwLock<SessionsFile>>,
}

impl SessionStore {
    pub fn empty(path: PathBuf) -> Self {
        Self {
            path,
            inner: Arc::new(RwLock::new(SessionsFile {
                schema_version: 1,
                sessions: BTreeMap::new(),
            })),
        }
    }

    pub fn path(&self) -> &PathBuf {
        &self.path
    }

    pub async fn count(&self) -> usize {
        self.inner.read().await.sessions.len()
    }
}
