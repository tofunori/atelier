//! Persistent project registry (atomic JSON).

use atelier_core::{project_display_name, project_key};
use serde::{Deserialize, Serialize};
use std::{
    collections::BTreeMap,
    fs,
    io,
    path::{Path, PathBuf},
    sync::Arc,
    time::{SystemTime, UNIX_EPOCH},
};
use tokio::sync::RwLock;

#[cfg(unix)]
use std::os::unix::fs::PermissionsExt;

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
#[serde(rename_all = "camelCase")]
pub struct RegistryFile {
    #[serde(default = "schema_v1")]
    pub schema_version: u32,
    #[serde(default)]
    pub projects: BTreeMap<String, RegistryProject>,
}

fn schema_v1() -> u32 {
    1
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct RegistryProject {
    pub canonical_root: String,
    pub display_name: String,
    pub registered_at: u64,
    pub last_opened_at: u64,
    pub last_activity_at: u64,
    pub pinned: bool,
}

#[derive(Clone)]
pub struct ProjectRegistry {
    path: PathBuf,
    inner: Arc<RwLock<RegistryFile>>,
}

impl ProjectRegistry {
    pub fn load(path: PathBuf) -> io::Result<Self> {
        let file = if path.is_file() {
            match fs::read_to_string(&path) {
                Ok(raw) => match serde_json::from_str::<RegistryFile>(&raw) {
                    Ok(mut file) => {
                        if file.schema_version == 0 {
                            file.schema_version = 1;
                        }
                        file
                    }
                    Err(error) => {
                        let corrupt = path.with_extension(format!(
                            "corrupt-{}.json",
                            now_secs()
                        ));
                        let _ = fs::rename(&path, &corrupt);
                        tracing::error!(
                            error = %error,
                            corrupt = %corrupt.display(),
                            "registry.json corrupt; trying backup"
                        );
                        load_backup(&path).unwrap_or_else(|| RegistryFile {
                            schema_version: 1,
                            projects: BTreeMap::new(),
                        })
                    }
                },
                Err(_) => RegistryFile {
                    schema_version: 1,
                    projects: BTreeMap::new(),
                },
            }
        } else {
            RegistryFile {
                schema_version: 1,
                projects: BTreeMap::new(),
            }
        };
        Ok(Self {
            path,
            inner: Arc::new(RwLock::new(file)),
        })
    }

    pub async fn project_count(&self) -> usize {
        self.inner.read().await.projects.len()
    }

    pub async fn list(&self) -> Vec<(String, RegistryProject)> {
        self.inner
            .read()
            .await
            .projects
            .iter()
            .map(|(k, v)| (k.clone(), v.clone()))
            .collect()
    }

    pub async fn get(&self, key: &str) -> Option<RegistryProject> {
        self.inner.read().await.projects.get(key).cloned()
    }

    /// Register a root. Does not scan. Returns (key, entry).
    pub async fn register(&self, root: &Path) -> Result<(String, RegistryProject), String> {
        let canonical = fs::canonicalize(root).map_err(|error| error.to_string())?;
        let key = project_key(&canonical).map_err(|error| error.to_string())?;
        let now = now_secs();
        let mut guard = self.inner.write().await;
        let entry = guard
            .projects
            .entry(key.clone())
            .and_modify(|existing| {
                existing.last_opened_at = now;
                existing.last_activity_at = now;
            })
            .or_insert_with(|| RegistryProject {
                canonical_root: canonical.to_string_lossy().into_owned(),
                display_name: project_display_name(&canonical),
                registered_at: now,
                last_opened_at: now,
                last_activity_at: now,
                pinned: false,
            })
            .clone();
        drop(guard);
        self.persist().await.map_err(|error| error.to_string())?;
        Ok((key, entry))
    }

    pub async fn touch_activity(&self, key: &str) {
        let mut guard = self.inner.write().await;
        if let Some(entry) = guard.projects.get_mut(key) {
            entry.last_activity_at = now_secs();
        }
    }

    pub async fn forget(&self, key: &str) -> bool {
        let removed = self.inner.write().await.projects.remove(key).is_some();
        if removed {
            let _ = self.persist().await;
        }
        removed
    }

    pub async fn persist(&self) -> io::Result<()> {
        let snapshot = self.inner.read().await.clone();
        atomic_write_json(&self.path, &snapshot)
    }
}

fn now_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}

fn load_backup(path: &Path) -> Option<RegistryFile> {
    let bak = path.with_extension("json.bak");
    let raw = fs::read_to_string(bak).ok()?;
    serde_json::from_str(&raw).ok()
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
        #[cfg(unix)]
        let _ = fs::set_permissions(&bak, fs::Permissions::from_mode(0o600));
    }
    fs::rename(&tmp, path)?;
    #[cfg(unix)]
    fs::set_permissions(path, fs::Permissions::from_mode(0o600))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    #[tokio::test]
    async fn register_persists_and_reloads() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("registry.json");
        let project = dir.path().join("proj");
        fs::create_dir_all(&project).unwrap();
        let reg = ProjectRegistry::load(path.clone()).unwrap();
        let (key, entry) = reg.register(&project).await.unwrap();
        assert_eq!(key.len(), 24);
        assert!(entry.canonical_root.contains("proj"));
        drop(reg);
        let reg2 = ProjectRegistry::load(path).unwrap();
        assert_eq!(reg2.project_count().await, 1);
        assert!(reg2.get(&key).await.is_some());
    }
}
