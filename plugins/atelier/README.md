# Atelier Codex plugin

This package connects Codex to the local Atelier artifact gallery. It contains
the plugin manifest, the Atelier workflow skill, and the stdio MCP bridge.

Atelier itself must be installed first so the `atelier` command is available:

```bash
bash install.sh
```

For local development, register this repository as a marketplace and install
the plugin:

```bash
codex plugin marketplace add .
codex plugin add atelier@atelier
```

For a published install, replace `.` with the Git repository URL.

The plugin remains local-only: the MCP process launches Atelier on loopback and
does not require OAuth or a hosted service.
