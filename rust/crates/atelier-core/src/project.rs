//! Stable project identity shared by server, daemon, MCP and CLI.

use crate::CoreError;
use sha2::{Digest, Sha256};
use std::{fs, path::Path};

/// Canonical project key: first 24 hex chars of SHA-256(canonical root path).
///
/// The key is identity only — it grants no rights.
pub fn project_key(root: &Path) -> Result<String, CoreError> {
    let canonical = fs::canonicalize(root).map_err(|error| {
        if error.kind() == std::io::ErrorKind::NotFound {
            CoreError::Io(std::io::Error::new(
                std::io::ErrorKind::NotFound,
                format!("project root missing: {}", root.display()),
            ))
        } else {
            CoreError::Io(error)
        }
    })?;
    // Hash the OS path as lossy UTF-8 only for the digest — never reopen from this string.
    let mut digest = Sha256::new();
    digest.update(canonical.to_string_lossy().as_bytes());
    let hex = hex::encode(digest.finalize());
    Ok(hex[..24].to_string())
}

/// Display name from the final path component of a (preferably canonical) root.
pub fn project_display_name(root: &Path) -> String {
    root.file_name()
        .and_then(|name| name.to_str())
        .filter(|name| !name.is_empty())
        .unwrap_or("project")
        .to_string()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn temp_dir(label: &str) -> std::path::PathBuf {
        let path = std::env::temp_dir().join(format!(
            "atelier-key-{}-{}-{}",
            label,
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&path).unwrap();
        path
    }

    #[test]
    fn key_is_stable_for_same_canonical_root() {
        let root = temp_dir("stable");
        let a = project_key(&root).unwrap();
        let b = project_key(&root).unwrap();
        assert_eq!(a, b);
        assert_eq!(a.len(), 24);
        assert!(a.chars().all(|c| c.is_ascii_hexdigit()));
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn spaces_and_accents_are_accepted() {
        let parent = temp_dir("parent");
        let root = parent.join("projet été 2026");
        fs::create_dir_all(&root).unwrap();
        let key = project_key(&root).unwrap();
        assert_eq!(key.len(), 24);
        let _ = fs::remove_dir_all(parent);
    }

    #[test]
    fn missing_root_errors() {
        let path = std::env::temp_dir().join(format!(
            "atelier-missing-{}",
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let err = project_key(&path).unwrap_err();
        assert!(matches!(err, CoreError::Io(_)));
    }

    #[cfg(unix)]
    #[test]
    fn symlink_to_same_inode_shares_key() {
        let real = temp_dir("real");
        let link_parent = temp_dir("link-parent");
        let link = link_parent.join("alias");
        std::os::unix::fs::symlink(&real, &link).unwrap();
        let a = project_key(&real).unwrap();
        let b = project_key(&link).unwrap();
        assert_eq!(a, b);
        let _ = fs::remove_dir_all(real);
        let _ = fs::remove_dir_all(link_parent);
    }
}
