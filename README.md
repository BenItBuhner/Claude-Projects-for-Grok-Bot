# claude-code-projects-mcp

<p align="center"><img src="docs/assets/claude-code-projects-plugin-hero.webp" width="100%" alt="The Claude Code mascot linked to the Grok Bot orb by a dotted line on a dark background."></p>

<p align="center"><a href="docs/assets/plugin-launch-v7-1080p.mp4"><strong>▶ Watch the demo</strong></a> (25 s, 1080p)</p>

**Grok Bot, working in your Claude Code Projects.**

It works like Grok Bot's Cursor Projects integration, but for Claude Code. You bring your own Claude
subscription, agents and sessions, wherever they run, and Grok Bot works with them for you: on its own, or
alongside you.

- **Hand off work.** "Ask the docs project to fix the broken links" goes straight into that Project's thread.
- **Keep track.** Grok Bot reads your Projects and their threads, sees what's done or waiting on you, and tells you.
- **Pick up results.** It waits for replies, fetches files and PRs, and reports back wherever you're chatting with it.
- **Unblock threads.** Projects run in Auto permission mode but still stop on cards like "Claude would like to add this
  folder to the session". Grok Bot sees them, approves what's clearly part of your task, and asks you about the rest.
- **Relay between Projects.** It forwards a message (and its files) from one thread to another, verbatim, with a header
  saying where it came from.
- **Your rules.** A plain-text policy file says what Grok Bot may send or approve on its own and what it must bring to you.

Everything Grok Bot sends starts with a `[sent by Grok Bot]` tag, so you and Claude can always tell its messages from yours.

## How it works

```
Grok Bot ──MCP──▶ ccp_mcp.py ──▶ ccp (CLI) ──AT-SPI + xdotool──▶ Claude desktop app on a headless X display
                                       └──── private copy of the app's IndexedDB (project list, sessions)
```

There is no Claude API involved. The plugin drives the real Claude desktop app running on a Linux machine you own, on its
own virtual display (Xvfb): `claude://` deep links open Projects and threads, the accessibility tree (AT-SPI) is read
for transcripts and buttons, and xdotool types into the app's own composer. No tokens or cookies are read.

Honest limits:

- **One UI action at a time.** Calls take 5–30 s and queue behind a shared lock. It is a remote control, not an API.
- **Fragile by nature.** An app update can rename the accessibility labels it depends on.
- **Linux only** (Ubuntu 22.04+ / Debian 12+, where the Claude desktop app ships), one Claude account per install.
- **It acts as you.** Sends spend your Claude usage and appear in the app like messages you typed (with the tag).

More in [Limits and known friction](docs/limits.md).

## Tools

Read: `claude_projects_list`, `claude_project_get`, `claude_thread_read`, `claude_thread_status`, `claude_thread_wait`,
`claude_thread_attachment_get`, `claude_thread_permissions`, `claude_project_env`, `claude_app_status`, `claude_staging`,
`claude_skill_get`, `claude_policy_get`, `claude_refresh`.

Write (each has a dry run): `claude_thread_send`, `claude_thread_forward`, `claude_thread_permission`, `claude_thread_create`,
`claude_project_add_repo`, `claude_project_remove_repo`, `claude_project_set_env`.

Write, no dry run: `claude_project_create` and `claude_project_pin` act as soon as they are called, and
`claude_policy_propose` writes a proposal file only.

## Install with your bot

Ask your Grok Bot:

> Install claude-projects-mcp from github.com/BenItBuhner/claude-code-projects-mcp

It follows [INSTALL.md](INSTALL.md) and handles the setup. You do two things: pick the computer that runs Claude
Code (Ubuntu or Debian), and sign in to Claude once. Then give your bot the skill in [skill/SKILL.md](skill/SKILL.md)
(or let it read it live with `claude_skill_get`) and, if you like, write your own rules into
`~/.config/claude-projects-mcp/policy.md`.

## Quick start

### 1. Requirements

- A Linux computer that stays on: **Ubuntu 22.04+ or Debian 12+** (x86_64 or arm64) with user-level systemd and `sudo` for apt.
- A Claude account that can use Claude Code Projects in the Claude desktop app.
- `git` and `python3` (3.10 or newer). The installer adds the rest: Xvfb, openbox, xdotool, AT-SPI, a VNC/noVNC
  viewport and the official Claude desktop app.
- An MCP client: Grok Bot, Cursor, Claude Desktop, or anything that speaks MCP over stdio or Streamable HTTP.
  For HTTP through `mcp-remote` you also need Node.js 18+ (`npx`) on the client side.

### 2. Install (on the Linux computer)

```bash
git clone https://github.com/BenItBuhner/claude-code-projects-mcp ~/claude-projects-mcp
cd ~/claude-projects-mcp
./install.sh --operator-name "Alex" --bot-name "Scout" --timezone "America/New_York"
```

`./install.sh --dry-run` prints every command first. Re-running is safe and keeps your token and sign-in.
`--operator-name` is only wording (how responses refer to you; default "the user"). `--bot-name` goes into the
`[sent by Grok Bot · Scout]` tag.

### 3. Sign in to Claude once

The installer prints a noVNC link (bound to `127.0.0.1`), an `ssh -L ...` line for opening it from another computer,
and the path of the VNC password file. Open it, sign in to Claude in the app window, then close the tab. Check:

```bash
./ccp --json status            # want "signed_in": true and "vnc_viewers": 0
./ccp --json projects --refresh
```

### 4. Configure

Settings live in `~/.config/claude-projects-mcp/config.json` (every key also has a `CCP_*` environment variable):

| key | default | what |
|---|---|---|
| `operator_name` | `"the user"` | how responses refer to you |
| `bot_name` | `""` | name inside the `[sent by Grok Bot · <name>]` tag |
| `timezone` | machine zone | zone for `*_local` timestamps |
| `mcp_host` / `mcp_port` | `127.0.0.1` / `8791` | HTTP listener |
| `allowed_hosts` | `[]` | extra Host headers (your tunnel hostname) |
| `viewer_ports` | `[]` | VNC/noVNC ports; UI actions refuse while a viewer is connected |
| `legacy_field_aliases` | `false` | also emit pre-1.0 field names for older agent prompts |

Your rules for agents go in `~/.config/claude-projects-mcp/policy.md` (see [Safety and policy](#safety-and-policy)).

### 5. Run as a service

The installer enables user services (and `loginctl enable-linger`, so they survive logout). The MCP server is `ccp-mcp`:

```bash
systemctl --user status ccp-mcp
systemctl --user restart ccp-mcp            # after a config change or `git pull`
journalctl --user -u ccp-mcp -n 50
curl -s http://127.0.0.1:8791/healthz       # -> ok
```

### 6. Connect an MCP client

**Same computer or over SSH (stdio, no token needed):**

```bash
~/claude-projects-mcp/.venv/bin/python ~/claude-projects-mcp/ccp_mcp.py --stdio
ssh you@your-host ~/claude-projects-mcp/.venv/bin/python ~/claude-projects-mcp/ccp_mcp.py --stdio
```

**Over HTTP with `mcp-remote`** (point your own tunnel or VPN at `127.0.0.1:8791`, add its hostname to
`allowed_hosts`, restart `ccp-mcp`, and copy `~/.config/claude-projects-mcp/mcp.token` privately to the client).
For example in Cursor's `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "claude-projects": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://your-host.example/mcp", "--transport", "http-only",
               "--header", "Authorization:${CCP_AUTH}"],
      "env": { "CCP_AUTH": "Bearer <contents of mcp.token>" }
    }
  }
}
```

**Grok Bot:** add a stdio connector whose command is
`~/claude-projects-mcp/bin/grok-bot-bridge https://your-host.example/mcp /path/to/mcp.token`. It wraps the same
`mcp-remote` call and reads the token from the file, so the token never appears in the command line. Then give the bot
[skill/SKILL.md](skill/SKILL.md), or let it read the live copy with `claude_skill_get`.

Check it read-only: `claude_app_status`, `claude_projects_list`, then
`claude_thread_send(project="<a project>", message="hello", dry_run=true)`, which types nothing.
Some MCP clients keep the tool list they saw first; every tool can still be called by name.

## Safety and policy

- **Private by default.** The HTTP server binds `127.0.0.1`, requires a random bearer token (chmod 600) and keeps
  DNS-rebinding protection on. Don't expose it without a tunnel or VPN in front. No Claude tokens or cookies are read.
- **Writes are explicit.** Sending, forwarding, creating threads or Projects, pressing permission cards and editing
  Project settings each need a separate tool call, act as you, and spend your Claude usage. All of them except
  `claude_project_create` and `claude_project_pin` have a `dry_run`; permission presses and forwards default to it.
- **Labeled.** Every message the plugin types starts with `[sent by Grok Bot]`, and reads mark those messages
  `sent_by_bot: true`. The tag is a label, not authentication.
- **Your policy.** The default POLICY is in [skill/SKILL.md](skill/SKILL.md). Override it in
  `~/.config/claude-projects-mcp/policy.md` with three headings: `### Pre-approved`, `### Surface to the user first`,
  `### Never`. `claude_policy_get` shows the effective policy; `claude_policy_propose` only records a suggestion for you.
- **Hands off when you're there.** UI actions refuse when the composer holds an unsent draft, and while someone is
  connected to a port listed in `viewer_ports` (add your viewport's VNC and noVNC ports, e.g. `[5999, 6179]` for
  display `:99`), unless the caller passes `force=true`.

## Limits

- Linux only (Ubuntu 22.04+ / Debian 12+), one Claude account per install.
- One UI action at a time; calls take 5–30 s and queue behind a shared lock. It is a remote control, not an API.
- It reads the app's accessibility labels and local cache, so a Claude app update can break it.
- Cached reads can be stale; each carries `cache_age_min`.
- Unofficial: not made or endorsed by Anthropic.

More in [Limits and known friction](docs/limits.md).

## Learn more

- [How it works](docs/how-it-works.md): architecture, ids, permission cards, files, transports, configuration
- [Agent skill and policy](skill/SKILL.md)
- [Changelog](CHANGELOG.md)
- [The `[sent by Grok Bot]` tag](docs/origin-bracket.md)
- [Limits and known friction](docs/limits.md)
- [Security](SECURITY.md)

Unofficial: not made or endorsed by Anthropic or by Grok Bot.

## License

MIT, see [LICENSE](LICENSE). The Claude and Grok Bot marks in the README image belong to their owners and aren't covered by this license.
