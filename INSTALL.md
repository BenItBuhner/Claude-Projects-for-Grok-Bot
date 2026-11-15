# INSTALL.md — for the Grok Bot doing the install

Follow this end to end when your user says "install claude-projects-mcp". You do everything except the two steps
marked **[human]**. Never print, paste or upload the MCP token, the VNC password or the keyring password. Refer to
them by file path only.

## 0. Ask one question [human]

> "Which computer should run Claude Code? It needs Ubuntu 22.04+ or Debian 12+ and has to stay on. The Claude app
> will run there in its own private display."

You need to be able to run shell commands on that computer (a registered machine or SSH). If it isn't
Debian-family or has no user systemd, stop and say so. Nothing else is supported.

## 1. Install (on that computer)

```bash
git clone https://github.com/BenItBuhner/Claude-Projects-for-Grok-Bot ~/claude-projects-mcp
cd ~/claude-projects-mcp
./install.sh --bot-name "<your bot name>" --timezone "<user's IANA zone>"
```

- The script uses `sudo` for apt. If sudo needs a password you can't supply, ask the user to run this one line
  themselves, then re-run `./install.sh --no-apt ...` as yourself:
  `sudo apt-get install -y xvfb openbox x11vnc novnc websockify python3-venv python3-gi gir1.2-atspi-2.0 at-spi2-core xdotool xclip gnome-keyring libsecret-1-0 dbus-user-session libxss1 iproute2 poppler-utils curl gnupg git firefox-esr`
  plus the Claude desktop app (`install.sh` shows the exact apt-repo commands it would run; `--dry-run` prints them).
- Display `:99` is the default. If it's taken (`ls /tmp/.X11-unix/X99`), pass `--display :98`.
- The script is idempotent. Re-running it is safe and keeps the existing token and sign-in.
- Check that the self-check lines all say `active` / `ok`. If one doesn't, read `journalctl --user -u <unit> -n 50`.

## 2. Sign in to Claude [human]

Give the user exactly what the installer printed: the noVNC URL (or the `ssh -L` line when they're on another
computer) and the path of the VNC password file. Tell them:

> "Open this, enter the password from that file, sign in to Claude in the app window, then close the tab."

The login page opens in a browser inside the same viewport. If it ends on a `claude://...` link instead of
returning to the app, the user (or you) runs `~/claude-projects-mcp/bin/claude-open-url 'claude://...'`.

Then poll, at most every 30 s for up to 15 min:

```bash
~/claude-projects-mcp/ccp --json status      # want: "signed_in": true and "vnc_viewers": 0
~/claude-projects-mcp/ccp --json projects --refresh
```

## 3. Connect yourself to the MCP server

Pick the first option that works:

1. **stdio over SSH.** Use this when your computer can SSH to it. Add an MCP connector whose command is
   `ssh <user>@<host> ~/claude-projects-mcp/.venv/bin/python ~/claude-projects-mcp/ccp_mcp.py --stdio`.
2. **stdio, same computer:** `~/claude-projects-mcp/.venv/bin/python ~/claude-projects-mcp/ccp_mcp.py --stdio`.
3. **HTTP through the user's own tunnel or VPN** (Cloudflare Tunnel, Tailscale and so on, pointing at `127.0.0.1:8791`).
   Add the tunnel hostname to `allowed_hosts` in `~/.config/claude-projects-mcp/config.json`, then
   `systemctl --user restart ccp-mcp`. Copy the token file to your own computer privately (for example `scp` to a
   chmod-600 file). Register `bin/grok-bot-bridge https://<host>/mcp <token-file>` as a stdio connector. It reads
   the token from the file, so the token never appears in argv.

## 4. Verify (read-only)

1. `claude_app_status` shows signed in, no dialogs, no lock holder.
2. `claude_projects_list` returns the user's projects.
3. `claude_thread_send(project=<one the user names>, message="test", dry_run=true)` returns
   `"dry_run": true` and `"origin_header": "[sent by Grok Bot · <name>]"`. Nothing is typed or sent.

**Don't** send a real message, create a thread or project, or change settings to "test". Every write is visible to
the user and spends their usage. Do those only when the user asks for that specific action.

## 5. Load the skill and the user's policy

1. Read the skill from the server: `claude_skill_get`. Save it as your skill for this connector (or re-read it at the
   start of each task; its `sha256` tells you when it changed). The repo copy is `skill/SKILL.md`.
2. Ask the user whether they want to change the default POLICY (what you may send or approve on your own). If they do,
   write their rules to `~/.config/claude-projects-mcp/policy.md` on the server (same headings as the default:
   `### Pre-approved`, `### Surface to the user first`, `### Never`). `claude_policy_get` shows the effective result.
3. Files: attach existing files by path, anything else as base64; the plugin stages them under
   `~/.local/state/claude-projects-mcp/staging` and cleans up. `claude_staging(action="info")` shows the paths.

## 6. Optional

- **Bracket note for Claude Code:** `./install.sh --claude-md` appends [CLAUDE.md](CLAUDE.md) to `~/.claude/CLAUDE.md`
  (local sessions). For cloud sessions, suggest the user add it to the repo's own `CLAUDE.md`. Don't edit their repos unasked.
- **Machine names:** after a few reads, `environment_info.machine.environment_label` shows labels like `my-laptop`.
  Map them in `config.json` → `environment_labels` (`{"my-laptop": {"resolved": "the laptop", "machine_kind": "laptop"}}`).

## Using it afterwards

- Read before you write: `claude_project_get` → `claude_thread_read`.
- `claude_thread_send` / `claude_thread_create` add `[sent by Grok Bot]` themselves. Never type it.
- In transcripts, `sent_by_bot: true` marks bot messages. Everything else came from the user.
- On `Busy` or `Refused` (viewer connected or unsent draft), wait and retry later. Don't pass `force=true` unless the user says so.
- Uninstall: `./install.sh --uninstall` (keeps config and sign-in; it tells you what else to delete).
