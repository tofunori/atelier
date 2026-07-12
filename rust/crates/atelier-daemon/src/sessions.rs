//! Browser sessions, one-time open tickets, and cookie validation.
#![allow(dead_code)]

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs, io,
    path::{Path, PathBuf},
    sync::Arc,
    time::{SystemTime, UNIX_EPOCH},
};
use tokio::sync::RwLock;
use uuid::Uuid;

#[cfg(unix)]
use std::os::unix::fs::PermissionsExt;

const MAX_TICKETS_GLOBAL: usize = 512;
const MAX_TICKETS_PER_PROJECT: usize = 32;
const MAX_SESSIONS_GLOBAL: usize = 500;
const MAX_SESSIONS_PER_PROJECT: usize = 50;

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
#[serde(rename_all = "camelCase")]
pub struct SessionsFile {
    #[serde(default = "schema_v1")]
    pub schema_version: u32,
    #[serde(default)]
    pub sessions: BTreeMap<String, SessionRecord>,
}

fn schema_v1() -> u32 {
    1
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SessionRecord {
    pub project_key: String,
    pub consumer_id: Option<String>,
    pub created_at: u64,
    pub expires_at: u64,
}

#[derive(Debug, Clone)]
struct TicketRecord {
    project_key: String,
    consumer_id: Option<String>,
    expires_at: u64,
    used: bool,
    theme: Option<String>,
    native_fs: bool,
}

#[derive(Clone)]
pub struct SessionStore {
    path: PathBuf,
    sessions: Arc<RwLock<SessionsFile>>,
    tickets: Arc<RwLock<BTreeMap<String, TicketRecord>>>,
    ticket_ttl_secs: u64,
    session_ttl_secs: u64,
}

impl SessionStore {
    pub fn load(path: PathBuf, ticket_ttl_secs: u64, session_ttl_secs: u64) -> io::Result<Self> {
        let sessions = if path.is_file() {
            match fs::read_to_string(&path) {
                Ok(raw) => serde_json::from_str(&raw).unwrap_or_default(),
                Err(_) => SessionsFile {
                    schema_version: 1,
                    sessions: BTreeMap::new(),
                },
            }
        } else {
            SessionsFile {
                schema_version: 1,
                sessions: BTreeMap::new(),
            }
        };
        let store = Self {
            path,
            sessions: Arc::new(RwLock::new(sessions)),
            tickets: Arc::new(RwLock::new(BTreeMap::new())),
            ticket_ttl_secs,
            session_ttl_secs,
        };
        Ok(store)
    }

    pub fn empty(path: PathBuf) -> Self {
        Self {
            path,
            sessions: Arc::new(RwLock::new(SessionsFile {
                schema_version: 1,
                sessions: BTreeMap::new(),
            })),
            tickets: Arc::new(RwLock::new(BTreeMap::new())),
            ticket_ttl_secs: 30,
            session_ttl_secs: 2_592_000,
        }
    }

    pub async fn mint_ticket(
        &self,
        project_key: &str,
        consumer_id: Option<String>,
        theme: Option<String>,
        native_fs: bool,
    ) -> Result<String, String> {
        self.purge_expired().await;
        let mut tickets = self.tickets.write().await;
        let global = tickets.len();
        let per_project = tickets
            .values()
            .filter(|t| t.project_key == project_key && !t.used)
            .count();
        if global >= MAX_TICKETS_GLOBAL || per_project >= MAX_TICKETS_PER_PROJECT {
            return Err("ticket capacity reached".into());
        }
        let raw = format!("{}{}", Uuid::new_v4().simple(), Uuid::new_v4().simple());
        let hash = hash_token(&raw);
        tickets.insert(
            hash,
            TicketRecord {
                project_key: project_key.to_string(),
                consumer_id,
                expires_at: now_secs() + self.ticket_ttl_secs,
                used: false,
                theme,
                native_fs,
            },
        );
        Ok(raw)
    }

    pub async fn consume_ticket(
        &self,
        raw_ticket: &str,
    ) -> Result<(String, String, Option<String>, bool), String> {
        self.purge_expired().await;
        let hash = hash_token(raw_ticket);
        let mut tickets = self.tickets.write().await;
        let Some(ticket) = tickets.get_mut(&hash) else {
            return Err("ticket not found".into());
        };
        if ticket.used {
            return Err("ticket already used".into());
        }
        if ticket.expires_at < now_secs() {
            return Err("ticket expired".into());
        }
        ticket.used = true;
        let project_key = ticket.project_key.clone();
        let consumer = ticket.consumer_id.clone();
        let theme = ticket.theme.clone();
        let native_fs = ticket.native_fs;
        drop(tickets);

        let session_raw = format!("{}{}", Uuid::new_v4().simple(), Uuid::new_v4().simple());
        let session_hash = hash_token(&session_raw);
        let now = now_secs();
        {
            let mut sessions = self.sessions.write().await;
            self.enforce_session_caps(&mut sessions, &project_key);
            sessions.sessions.insert(
                session_hash,
                SessionRecord {
                    project_key: project_key.clone(),
                    consumer_id: consumer,
                    created_at: now,
                    expires_at: now + self.session_ttl_secs,
                },
            );
        }
        self.persist().await.map_err(|e| e.to_string())?;
        Ok((session_raw, project_key, theme, native_fs))
    }

    pub async fn validate_session(&self, raw_cookie: &str, project_key: &str) -> bool {
        self.purge_expired().await;
        let hash = hash_token(raw_cookie);
        let sessions = self.sessions.read().await;
        match sessions.sessions.get(&hash) {
            Some(record) => record.project_key == project_key && record.expires_at >= now_secs(),
            None => false,
        }
    }

    pub async fn count(&self) -> usize {
        self.sessions.read().await.sessions.len()
    }

    pub fn path(&self) -> &PathBuf {
        &self.path
    }

    async fn purge_expired(&self) {
        let now = now_secs();
        self.tickets
            .write()
            .await
            .retain(|_, t| !t.used && t.expires_at >= now);
        let mut sessions = self.sessions.write().await;
        let before = sessions.sessions.len();
        sessions.sessions.retain(|_, s| s.expires_at >= now);
        if sessions.sessions.len() != before {
            drop(sessions);
            let _ = self.persist().await;
        }
    }

    fn enforce_session_caps(&self, sessions: &mut SessionsFile, project_key: &str) {
        while sessions.sessions.len() >= MAX_SESSIONS_GLOBAL {
            if let Some(oldest) = sessions
                .sessions
                .iter()
                .min_by_key(|(_, s)| s.created_at)
                .map(|(k, _)| k.clone())
            {
                sessions.sessions.remove(&oldest);
            } else {
                break;
            }
        }
        let mut project_keys: Vec<(String, u64)> = sessions
            .sessions
            .iter()
            .filter(|(_, s)| s.project_key == project_key)
            .map(|(k, s)| (k.clone(), s.created_at))
            .collect();
        project_keys.sort_by_key(|(_, created)| *created);
        while project_keys.len() >= MAX_SESSIONS_PER_PROJECT {
            if let Some((oldest, _)) = project_keys.first().cloned() {
                sessions.sessions.remove(&oldest);
                project_keys.remove(0);
            } else {
                break;
            }
        }
    }

    async fn persist(&self) -> io::Result<()> {
        let snapshot = self.sessions.read().await.clone();
        atomic_write_json(&self.path, &snapshot)
    }
}

fn hash_token(raw: &str) -> String {
    let mut digest = Sha256::new();
    digest.update(raw.as_bytes());
    hex::encode(digest.finalize())
}

fn now_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}

fn atomic_write_json(path: &Path, value: &impl Serialize) -> io::Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let tmp = path.with_extension("json.tmp");
    let bytes = serde_json::to_vec_pretty(value).map_err(io::Error::other)?;
    fs::write(&tmp, &bytes)?;
    #[cfg(unix)]
    {
        let file = fs::File::open(&tmp)?;
        file.sync_all()?;
        fs::set_permissions(&tmp, fs::Permissions::from_mode(0o600))?;
    }
    if path.is_file() {
        let bak = path.with_extension("json.bak");
        let _ = fs::copy(path, &bak);
    }
    fs::rename(tmp, path)?;
    #[cfg(unix)]
    fs::set_permissions(path, fs::Permissions::from_mode(0o600))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    #[tokio::test]
    async fn ticket_is_single_use() {
        let dir = tempdir().unwrap();
        let store = SessionStore::load(dir.path().join("sessions.json"), 30, 3600).unwrap();
        let ticket = store
            .mint_ticket("aaaaaaaaaaaaaaaaaaaaaaaa", None, None, true)
            .await
            .unwrap();
        let (session, key, _, _) = store.consume_ticket(&ticket).await.unwrap();
        assert_eq!(key, "aaaaaaaaaaaaaaaaaaaaaaaa");
        assert!(!session.is_empty());
        assert!(store.consume_ticket(&ticket).await.is_err());
        assert!(store.validate_session(&session, &key).await);
        assert!(
            !store
                .validate_session(&session, "bbbbbbbbbbbbbbbbbbbbbbbb")
                .await
        );
    }
}
