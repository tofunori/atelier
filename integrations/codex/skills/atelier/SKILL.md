---
name: atelier
description: Open the local Atelier artifact gallery in Codex, receive explicitly sent image or text annotations, apply requested fixes, and refresh regenerated artifacts.
---

# Atelier for Codex

## Open and connect

1. Call `atelier_open` with the repository root and a concise task label.
2. Use the returned local URL; never invent a port.
3. Call `atelier_connect` before waiting so this task becomes the destination
   shown in Atelier.

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
