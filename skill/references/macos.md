# macOS host by hand (Lima VM)

Use this when the plugin should run on an always-on Mac and `hosts/macos/setup.sh` isn't used (you only have a shell
on the Mac, the script failed halfway, or the user wants to see each step). The script does exactly these steps; if
it's there, prefer `bash hosts/macos/setup.sh [--bot-name NAME] [--no-sleep] [--dry-run]`.

## Why a VM

The plugin drives the Claude desktop app through the **Linux** accessibility tree (AT-SPI) on a private headless X
display. The Mac's own Claude app can't be driven that way, so the plugin runs the Linux build of the app inside a
small Ubuntu VM. The Mac's own Claude app, files and sign-in are never touched: the VM gets no access to the Mac's
files and is signed in separately. An always-on Linux machine is still the recommended host; a Mac works as long as it
stays awake, logged in and on power.

Needs: macOS 13 or newer, Apple silicon or Intel, about 4 GB RAM and 20 GB disk free for the VM, Homebrew.

## 1. Lima

```bash
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"     # Homebrew: /opt/homebrew (Apple silicon) or /usr/local (Intel)
command -v limactl || brew install lima                  # ask before installing Homebrew itself (https://brew.sh)
limactl --version
```

## 2. The VM

One instance named `ccp`, Ubuntu 24.04 (it has systemd, so the plugin's normal user services work), no Mac folders
mounted:

```bash
limactl create --name=ccp --cpus=2 --memory=4 --disk=20 --mount-none --tty=false template://ubuntu-24.04
limactl start --tty=false ccp
limactl list --format '{{.Status}}' ccp                  # -> Running (wait and re-check; the first boot takes a minute)
```

Ports: Lima forwards TCP ports that the guest binds on its localhost to the Mac's `127.0.0.1` on its own. The noVNC
viewer (`6080+N`, `6179` for display `:99`) and the HTTP MCP server (`8791`, or `--mcp-port N`) therefore show up on
the Mac at `127.0.0.1` with no extra configuration. Nothing is exposed to the network.

## 3. Inside the VM: linger, git, the plugin

Everything plugin-related runs inside the VM. Lima's default VM user has passwordless sudo inside the VM (Lima's
design), so the package step needs no password from the user.

```bash
limactl shell ccp -- bash -lc 'sudo apt-get update -qq && sudo apt-get install -y -qq git ca-certificates'
limactl shell ccp -- bash -lc 'sudo loginctl enable-linger "$USER"'      # services keep running with no shell open
limactl shell ccp -- bash -lc 'git clone https://github.com/BenItBuhner/Claude-Projects-for-Grok-Bot ~/claude-projects-mcp'
```

Then the same guided steps as on Linux (Part A of the skill), run inside the VM. Define a helper once:

```bash
ccpvm() { limactl shell ccp -- bash -lc 'cd ~/claude-projects-mcp && exec ./ccp "$@"' ccp "$@"; }   # = hosts/macos/ccp
ccpvm setup choose macos_runtime --label "VM on $(scutil --get LocalHostName)"
ccpvm setup preflight --host-kind macos-vm
ccpvm setup install --bot-name "<bot name>" --timezone "$(readlink /etc/localtime | sed 's#.*/zoneinfo/##')"
ccpvm setup status
```

The single quotes matter: `$@` must expand inside the VM's bash, not on the Mac.

## 4. Start the VM at login

The VM stops when the Mac restarts. Start it from a LaunchAgent (runs in the user's login session; keep the user
logged in, or turn on automatic login for an always-on Mac). Newer Lima versions also have
`limactl start-at-login ccp` (see `limactl --help`); the LaunchAgent below works with any version.

```bash
mkdir -p ~/Library/LaunchAgents ~/Library/Logs "$HOME/Library/Application Support/claude-projects-for-grok-bot"
L="$(command -v limactl)"
cat > "$HOME/Library/Application Support/claude-projects-for-grok-bot/start-vm.sh" <<SH
#!/bin/bash
[ "\$("$L" list --format '{{.Status}}' ccp 2>/dev/null)" = Running ] && exit 0
exec "$L" start --tty=false ccp
SH
chmod 700 "$HOME/Library/Application Support/claude-projects-for-grok-bot/start-vm.sh"
```

Then a plist at `~/Library/LaunchAgents/com.github.claude-projects-for-grok-bot.vm.plist` with `ProgramArguments`
`/bin/bash <that script>`, `RunAtLoad` true, `StartInterval` 600 (re-checks every 10 minutes, quietly), a `PATH`
that contains the folder of `limactl`, and `StandardOutPath`/`StandardErrorPath` in `~/Library/Logs`. Load it:

```bash
launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.github.claude-projects-for-grok-bot.vm.plist 2>/dev/null
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.github.claude-projects-for-grok-bot.vm.plist
```

Inside the VM, systemd starts the plugin's services on boot (linger is on), so nothing else is needed.

## 5. Sleep and laptops

```bash
pmset -g | awk '$1=="sleep"{print $2}'        # 0 = never sleeps on the current power source
pmset -g batt | grep -q InternalBattery && echo laptop
sudo pmset -c sleep 0                          # stop sleeping on power: ask the user first (it asks for their password)
```

A sleeping Mac pauses the plugin. A MacBook works only while it's awake and plugged in: say so before installing.

## 6. Sign-in, viewer link and password file

`ccpvm setup signin start` turns the viewer on and prints the link. Lima forwards it, so the user opens it in their
normal Mac browser: `http://127.0.0.1:6179/vnc.html?autoconnect=1&resize=scale` (display `:99`). The viewer password is
saved **inside the VM** at `~/.config/claude-projects-mcp/vncpass.txt`; the user reads it with
`limactl shell ccp -- bash -lc 'cat ~/.config/claude-projects-mcp/vncpass.txt'` (never paste it into chat yourself). In the app
they click "Get started" (older versions: "Sign in"). Then `ccpvm setup signin wait`, `ccpvm setup check-beta`.

## 7. Connect the agent and verify

| agent runs | command |
|---|---|
| on the Mac | `limactl shell ccp -- bash -lc 'exec ~/claude-projects-mcp/.venv/bin/python ~/claude-projects-mcp/ccp_mcp.py --stdio'` |
| elsewhere, SSH to the Mac (Remote Login on) | `ssh <user>@<mac> "limactl shell ccp -- bash -lc 'exec ~/claude-projects-mcp/.venv/bin/python ~/claude-projects-mcp/ccp_mcp.py --stdio'"` |
| HTTP | `http://127.0.0.1:8791/mcp` on the Mac, bearer token from the VM's `~/.config/claude-projects-mcp/mcp.token` |

```bash
ccpvm setup verify --live          # or claude_setup(action="verify", live=true) through the connector
limactl list                       # Status Running
curl -s http://127.0.0.1:8791/healthz   # from the Mac: ok (when the HTTP server is on)
launchctl print "gui/$(id -u)/com.github.claude-projects-for-grok-bot.vm" | grep -E 'state|last exit'
```

## Common failures

| symptom | fix |
|---|---|
| `limactl: command not found` (in scripts, launchd, SSH) | Homebrew isn't on that shell's PATH: use `/opt/homebrew/bin/limactl` or `/usr/local/bin/limactl`, or `eval "$(brew shellenv)"` |
| `limactl start` returns but the VM isn't `Running` | `limactl list`; logs in `~/.lima/ccp/*.log`; free disk and memory, then start again |
| viewer link doesn't load on the Mac | the guest service isn't up yet (`ccpvm setup status`) or the port isn't bound inside: `limactl shell ccp -- ss -tln \| grep 6179` |
| services gone after a Mac restart | the LaunchAgent isn't loaded (`launchctl print ...` above), or nobody is logged in: turn on automatic login |
| `preflight` says `claude_download` fails | the VM can't reach downloads.claude.ai: Mac VPN/proxy/firewall; `limactl shell ccp -- curl -sI https://downloads.claude.ai/claude-desktop/key.asc` |
| `$@`, `$USER` or `~` wrong (a `/Users/...` path inside the VM) | they expanded on the Mac: put the command in single quotes after `bash -lc` |
| plugin pauses at night | the Mac sleeps: `sudo pmset -c sleep 0` (ask first) |

Remove: `ccpvm setup uninstall`, then `limactl stop ccp && limactl delete ccp`, `launchctl bootout` and delete the plist
(ask the user first; it deletes the VM's sign-in).
