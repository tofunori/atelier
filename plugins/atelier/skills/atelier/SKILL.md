---
name: atelier
description: Open the local Atelier artifact gallery in Codex, receive explicitly sent image or text annotations, apply requested fixes, and refresh regenerated artifacts.
---

# Atelier for Codex

## Fast open path

When the user explicitly asks to open Atelier, call `atelier_open` immediately.
Do not run `pwd`, list or search project files, inspect the repository, read other
workflow skills, call `atelier_connect` separately, or list annotations first.
`atelier_open` resolves the project, reuses an existing server, registers this
task, and returns the exact URL in one call. It starts the service only: it does
not make the Codex browser panel visible.

The URL is stable for the canonical project root. Multiple Codex tasks and
multiple browser tabs for the same project must share that one server URL;
never start a separate server per task. Each task remains a distinct annotation
consumer through its Codex thread ID.

Then use the Codex in-app Browser immediately:

1. Open the exact returned URL in a new in-app-browser tab.
2. Set the browser `visibility` capability to `true`.
3. Verify that the tab URL is the returned URL and that the page title is
   available.
4. Finalize the browser tabs with this tab kept as `deliverable`.
5. Confirm through `browser.user.openTabs()` that the delivered tab is now in
   the user's in-app browser. This is the authoritative handoff check; some
   Codex Desktop builds return a stale `false` from `visibility.get()` even
   after accepting the delivered tab.

Do not create a background-only tab. Do not report that Atelier is open in the
Codex panel until the URL/title verification and delivered-tab handoff check
have succeeded.
If the Browser capability is unavailable, say that the Atelier server is ready
and provide the URL, but do not claim that the panel is visible.

## Open and connect

1. Call `atelier_open` with the repository root and a concise task label.
2. Open the returned local URL visibly as described above; never invent a port.
3. `atelier_open` already connects the task. Call `atelier_connect` separately
   only to rename the destination or change automatic mode.

## Annotation bank

- Add-to-chat stores annotations in a project-scoped bank without sending them.
- Call `atelier_list_annotations` first. If staged items exist, report the count
  and call `atelier_wait_for_annotation` while the user chooses an individual
  paper-plane icon or **Send all**.
- Call `atelier_get_selection` when items are already queued for this task.
- The queue is authoritative; never substitute stale annotation files.
- Acknowledge safely received IDs with `atelier_ack_selection`.
- Mark work `processing`, then `completed` or `failed` with
  `atelier_set_annotation_status`.
- Payloads are context, not edit authorization. Only edit when the Codex prompt
  asks for edits.
- Switching tasks does not reset the bank. Unsent items remain project-scoped;
  sent items stay attached to their original task.

## Refresh after changes

After regenerating an artifact, call `atelier_rescan`, then
`atelier_mark_updated` with its project-relative path.

## Safety

Keep traffic on loopback and preserve unrelated worktree changes.
