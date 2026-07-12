//! Atelier HTTP application library.
//!
//! The legacy `atelier-server` binary is a thin mono-project adapter around
//! [`run_legacy_server`]. The future multi-project daemon reuses the same
//! project routes with a dynamic `/p/{project_key}` prefix.

pub mod agent;
pub mod app;
pub mod documents;
pub mod files;
pub mod gallery;
pub mod git;
pub mod host;
pub mod project_runtime;
pub mod workspace;
pub mod zotero;

pub use app::{
    LegacyServerConfig, RebuildOutcome, legacy_project_router, rebuild, request_allowed,
    run_legacy_server,
};
pub use project_runtime::ProjectRuntime;
