# Manual install

Most people don't need this page: paste the prompt from the [README](../README.md) into your agent and it runs the
guided setup for you (skill Part A). This is the same flow by hand, for people who'd rather type it themselves.

## 1. Requirements

- A computer that stays on: **Ubuntu 22.04+ or Debian 12+** (x86_64 or arm64) with user-level systemd and `sudo` for apt.
  On an always-on Mac or Windows PC, run [`hosts/macos/setup.sh`](../hosts/macos/setup.sh) or
  [`hosts/windows/setup.ps1`](../hosts/windows/setup.ps1) instead of steps 2 and 3's install (see [Hosts](hosts.md)).
- A Claude account with Claude Code Projects (a beta). `./ccp setup check-beta` tells you after sign-in.
- `git` and `python3` (3.10 or newer). The installer adds the rest: Xvfb, openbox, xdotool, AT-SPI, a VNC/noVNC
  viewport and the official Claude desktop app.
- An MCP client: Grok Bot, Cursor, Claude Desktop, or anything that speaks MCP over stdio or Streamable HTTP.
  For HTTP through `mcp-remote` you also need Node.js 18+ (`npx`) on the client side.

## 2. Install (on the Linux computer)

```bash
git clone https://github.com/BenItBuhner/Claude-Projects-for-Grok-Bot ~/claude-projects-mcp
cd ~/claude-projects-mcp
./install.sh --operator-name "Alex" --bot-name "Scout" --timezone "America/New_York"
```

`./install.sh --dry-run` prints every command first. Re-running is safe and keeps your token and sign-in.
`--operator-name` is only wording (how responses refer to you; default "the user"). `--bot-name` goes into the
`[sent by Grok Bot · Scout]` tag.

## 3. Sign in to Claude once

```bash
./ccp setup signin start       # prints the noVNC link, an `ssh -L ...` line for another computer, and the password file path
./ccp setup signin wait        # waits until you're signed in and have closed the tab, then turns the viewport off
./ccp setup check-beta         # does this account have Claude Code Projects?
```

Open the link, enter the password from that file, click "Get started" (older versions say "Sign in") in the app window
and log in in the browser that opens inside it. If it stops on a `claude://...` link, run `bin/claude-open-url '<link>'`.

## 4. Configure

Settings live in `~/.config/claude-projects-mcp/config.json` (every key also has a `CCP_*` environment variable):

| key | default | what |
|---|---|---|
| `operator_name` | `"the user"` | how responses refer to you |
| `bot_name` | `""` | name inside the `[sent by Grok Bot · <name>]` tag |
| `timezone` | machine zone | zone for `*_local` timestamps |
| `mcp_host` / `mcp_port` | `127.0.0.1` / `8791` | HTTP listener (`install.sh --mcp-port N`) |
| `mcp_http` | `true` | `false` = no HTTP server, stdio only (`install.sh --no-http`; the box default) |
| `service_mode` | picked by install.sh | `systemd` (user units) or `process` (the plugin's own supervisor, `ccp_proc.py`, on hosts without user systemd) |
| `keyring_data_dir` | private dir (new installs) | where the plugin's own gnome-keyring keeps its keyring, apart from a desktop login keyring |
| `allowed_hosts` | `[]` | extra Host headers (your tunnel hostname) |
| `viewer_ports` | `5900+N`, `6080+N` | VNC/noVNC ports; UI actions refuse while a viewer is connected |
| `viewport_mode` | `on-demand` (new installs) | `on-demand`: the noVNC viewport is only on during sign-in; `always`: left on |
| `legacy_field_aliases` | `false` | also emit pre-1.0 field names for older agent prompts |

Your rules for agents go in `~/.config/claude-projects-mcp/policy.md` (see [Safety and policy](#safety-and-policy)).

## 5. Run as a service

The installer enables user services (and `loginctl enable-linger`, so they survive logout). The MCP server is `ccp-mcp`:

```bash
systemctl --user status ccp-mcp
systemctl --user restart ccp-mcp            # after a config change or `git pull`
journalctl --user -u ccp-mcp -n 50
curl -s http://127.0.0.1:8791/healthz       # -> ok
```

On a host without user systemd (for example a container whose PID 1 is tini) the installer picks process mode: the same
unit files, run by `ccp_proc.py`, with its own D-Bus session bus. Logs are in `~/.local/state/claude-projects-mcp/proc/logs/`.

```bash
python3 ccp_proc.py status                  # what runs
./ccp setup start                           # start everything (also after the host restarted)
python3 ccp_proc.py restart ccp-mcp
```

The MCP server starts the supervisor when it isn't running, so an agent that connects over stdio brings the services
back by itself.

## 6. Connect an MCP client

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
[skill/SKILL.md](../skill/SKILL.md), or let it read the live copy with `claude_skill_get`.

Check it read-only: `./ccp setup verify --live` on the host, or `claude_setup(action="verify", live=true)` from the
client. Nothing is typed or sent.
Some MCP clients keep the tool list they saw first; every tool can still be called by name.

## Keep it healthy

```bash
./ccp setup status              # where things stand and what to do next
./ccp setup verify --live       # read-only end-to-end checks (a send dry run types nothing)
./ccp setup tidy [--apply]      # leftovers: staging, stale lock record, cache copies, file pickers, viewport
./ccp setup reconfigure --bot-name NEW   # re-renders config and services; restarts only what changed
./ccp setup uninstall [--purge]
```

## Safety and policy

- **Private by default.** The HTTP server binds `127.0.0.1`, requires a random bearer token (chmod 600) and keeps
  DNS-rebinding protection on. Don't expose it without a tunnel or VPN in front. No Claude tokens or cookies are read.
- **Writes are explicit.** Sending, forwarding, creating threads or Projects, pressing permission cards and editing
  Project settings each need a separate tool call, act as you, and spend your Claude usage. All of them except
  `claude_project_create` and `claude_project_pin` have a `dry_run`; permission presses and forwards default to it.
- **Labeled.** Every message the plugin types starts with `[sent by Grok Bot]`, and reads mark those messages
  `sent_by_bot: true`. The tag is a label, not authentication.
- **Your policy.** The default POLICY is in [skill/SKILL.md](../skill/SKILL.md). Override it in
  `~/.config/claude-projects-mcp/policy.md` with three headings: `### Pre-approved`, `### Surface to the user first`,
  `### Never`. `claude_policy_get` shows the effective policy; `claude_policy_propose` only records a suggestion for you.
- **Hands off when you're there.** UI actions refuse when the composer holds an unsent draft, and while someone is
  connected to a port listed in `viewer_ports` (add your viewport's VNC and noVNC ports, e.g. `[5999, 6179]` for
  display `:99`), unless the caller passes `force=true`.

