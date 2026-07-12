//! Persistent project registry (atomic JSON). Stub for Phase 2; filled in Phase 3.
#![allow(dead_code)]

use serde::{Deserialize, Serialize};
use serde_json::json;
use std::{
    collections::BTreeMap,
    fs,
    io,
    path::{Path, PathBuf},
    sync::Arc,
};
use tokio::sync::RwLock;

#[cfg(unix)]
use std::os::unix::fs::PermissionsExt;

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
#[serde(rename_all = "camelCase")]
pub struct RegistryFile {
    pub schema_version: u32,
    pub projects: BTreeMap<String, RegistryProject>,
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

#[derive(Clone, Default)]
pub struct ProjectRegistry {
    path: PathBuf,
    inner: Arc<RwLock<RegistryFile>>,
}

impl ProjectRegistry {
    pub fn load(path: PathBuf) -> io::Result<Self> {
        let file = if path.is_file() {
            match fs::read_to_string(&path) {
                Ok(raw) => match serde_json::from_str::<RegistryFile>(&raw) {
                    Ok(file) => file,
                    Err(error) => {
                        let corrupt = path.with_extension(format!(
                            "corrupt-{}.json",
                            std::time::SystemTime::now()
                                .duration_since(std::time::UNIX_EPOCH)
                                .unwrap_or_default()
                                .as_secs()
                        ));
                        let _ = fs::rename(&path, &corrupt);
                        tracing::error!(
                            error = %error,
                            corrupt = %corrupt.display(),
                            "registry.json corrupt; starting empty"
                        );
                        load_backup(&path).unwrap_or_default()
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

    pub async fn persist(&self) -> io::Result<()> {
        let snapshot = self.inner.read().await.clone();
        atomic_write_json(&self.path, &snapshot)
    }
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
    let _ = json!({}); // keep serde_json used for pretty helpers in tests
    Ok(())
}
