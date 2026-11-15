# Changelog

## 1.0.1 (2026-11-14)

### Fixed
- Fresh installs failed at the pip step with `ResolutionImpossible`: `ccl_chromium_reader` declares an unpinned git
  dependency on `ccl_simplesnappy` that pip won't combine with our pinned one. It's now installed with `--no-deps` from
  `requirements-nodeps.txt`, and its other dependency (`zstd`) is pinned in `requirements.txt`.

## 1.0.0 (2026-11-14)

### Compatibility
- With `legacy_field_aliases` on, `current_state` (and any dict with `waiting_on`) also carries `status_legacy` /
  `waiting_on_legacy` in the pre-1.0 vocabulary (`waiting_on_<operator first name>`, `<operator first name>`), next to
  the unchanged new fields, so older prompts and scripts keep working.
- Cached reads written by pre-1.0 releases are normalized to the new vocabulary (`waiting_on_user`, `user`), so one
  result never mixes old and new status words.
- A bot-sent message's `summary` (and `current_state.summary`) is the request without the `[sent by Grok Bot]` line;
  `text` stays verbatim and `sent_by_bot` marks it. New helper `ccp_origin.origin_body`.
- Skill: note that some MCP clients show a stale tool list; every tool can still be called by name.

### Added
- `claude_thread_permissions` (read-only) and `claude_thread_permission` (press an option by label, dry run by default):
  generic detection of the app's permission cards (add folder, add repository, allow machine, run command, ...), with a
  stable `card_id`, `type`, `target`, `prompt` and `options`.
- Reads and status report `permission_cards`, `current_state.waiting_on = "permission"` and `permission_pending`, also
  for replies that stall asking for an OK (`permission_hint`).
- `claude_thread_forward`: relays one message verbatim under a `Forwarded from Project "…" › thread "…" (<chan_>/<id>)`
  header, optionally with its images and files (fetched into staging and re-attached). Dry run by default.
- Stable `address` (`<chan_>/<cse_|cmsg_>`) on read and status results.
- Managed staging directory (`incoming/`, `outgoing/`, `attach-test/`, `msg/`) with per-folder TTLs, a size cap, an
  hourly automatic sweep and the `claude_staging` tool.
- Canonical agent skill at `skill/SKILL.md` with a user-editable POLICY section; `claude_skill_get`, `claude_policy_get`
  and `claude_policy_propose` (proposals go to a user-owned file and never apply themselves).
- `claude_thread_send(attach_test=true)` with `dry_run=true`: opens the file picker per file, types the path, confirms it
  by OCR and cancels. Nothing is uploaded or sent.
- Config keys `staging_dir`, `staging_ttl_hours`, `staging_max_mb`, `staging_sweep_interval_min`, `attachments_dir`,
  `skill_path`, `legacy_field_aliases`.
- `CHANGELOG.md`.

### Changed
- README: titled "Claude Projects for Grok Bot" (repository `BenItBuhner/Claude-Projects-for-Grok-Bot`), new hero
  image and a link to the launch demo (`docs/assets/`), all 23 tools listed (adds `claude_refresh`).
  The old `docs/images/banner.png` is removed.
- Attachments are chosen by typing each absolute path into the GTK file chooser's location bar (Ctrl+L) instead of
  browsing the Places sidebar.
- Base64 attachments are staged in a unique folder per send (`outgoing/<stamp>-<id>/<filename>`) instead of overwriting
  a shared file by name.
- The `ccp` wrapper also finds a `venv/` next to it.

### Fixed
- An orphaned GTK "Open Files" chooser left by a failed attach is cancelled before and after every attach, and sends refuse
  to type while one is open (it used to swallow every later send's keystrokes).
- Clipboard paste read-back is verified and retried once; non-ASCII text goes through the clipboard.
- README: `claude_project_create` and `claude_project_pin` have no dry run (the README said every write tool had one).
- `install.sh` writes `viewer_ports` (`5900+N`, `6080+N` for display `:N`) into the config, so the "pause while a viewer
  is connected" check is visible there. Ports set by hand are kept; default ports follow a `--display` change.
- Memory: every tool call that names a project used to copy and fully deserialize the app's IndexedDB in its own child
  process, so parallel calls from several bots stacked those reads up against the service's `MemoryMax`. Now one read
  runs at a time (callers queued behind it reuse its result) and project lookups accept a snapshot up to 30 s old
  (`CCP_IDB_RESOLVE_MAX_AGE`), falling back to a fresh read when the name or id isn't in it.

## 0.1.0 (2026-11-10)
- First private release: Projects, threads, sends with the `[sent by Grok Bot]` origin bracket, attachments, Project admin
  tools, installer, systemd units, stdio and HTTP transports.
