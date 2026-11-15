# Hosts: where the plugin runs

The plugin drives the real Claude desktop app through the Linux accessibility tree (AT-SPI) on a private headless
display, so the app it drives always runs on Linux. Which computer that Linux lives on is your choice. It has to stay
on, because your agent reaches your Projects through it.

| host | how | setup | always-on notes |
|---|---|---|---|
| **Always-on Linux** (recommended) | natively | `./claude-projects setup preflight`, then `./claude-projects setup install` | `preflight` warns if the machine can still suspend |
| **Always-on Mac** (macOS 13+, not yet tried on a real Mac) | a small Ubuntu 24.04 VM (Lima, instance `claude-projects`, 2 CPUs, 4 GB, no access to your files) | `bash hosts/macos/setup.sh` | a LaunchAgent starts the VM at login; keep the user logged in (or turn on automatic login); `--no-sleep` sets `pmset -c sleep 0` |
| **Always-on Windows PC** | a dedicated WSL2 distro `ClaudeProjects` (Ubuntu 24.04, user `cpg` without sudo, systemd on); needs WSL 2.4.4+ (the script updates it) | `powershell -ExecutionPolicy Bypass -File hosts\windows\setup.ps1` | a scheduled task keeps the distro running from sign-in, so turn on automatic sign-in, or add `-AtStartup` to also start it at boot; `-WebDownload` for PCs without the Microsoft Store; `-NoSleep` turns off sleep on AC |
| Laptop (any OS) | as above | as above | works only while it's awake and plugged in |
| Your agent's own box | natively, stdio on the same machine | as Linux | **fallback**: works only while the box is up |

On Windows, root steps (packages, linger, app upgrades) run from Windows with `wsl.exe -d ClaudeProjects -u root`, so
nothing inside the distro can become root on its own. On a Mac, Lima's default VM user has passwordless sudo inside the VM
(Lima's own design); the VM has no access to your Mac's files.

On a Mac or PC the plugin never touches the Claude app you use yourself: the VM or distro has its own copy, signed in
separately. Lima and WSL forward the VM's localhost ports to the host, so the sign-in viewport opens in your normal
browser at `http://127.0.0.1:6179/vnc.html` (display `:99`), and the MCP server is at `127.0.0.1:8791`.

Setting it up by hand instead of with the scripts (or finishing a run that stopped halfway) is described step by step,
with verify commands and common failures, in [skill/references/macos.md](../skill/references/macos.md) and
[skill/references/windows.md](../skill/references/windows.md).

### Installing a release, branch or commit

The scripts install the repository's default branch. To pin a release or try a branch, give them a tag, a branch or
a full 40-character commit sha:

```bash
bash hosts/macos/setup.sh --ref v1.0.2                                            # Mac
powershell -ExecutionPolicy Bypass -File hosts\windows\setup.ps1 -Ref v1.0.2     # Windows
```

Both scripts also read `CPG_REPO_REF` (the flag wins). On a fresh install they clone and check out that ref; on a
re-run, where `~/claude-projects-mcp` already exists in the VM or distro, they fetch the ref from origin and check it
out, and they refuse (leaving the clone as it is) when it has uncommitted changes or local commits that origin doesn't
have. Without a ref nothing changes: a missing clone gets the default branch and an existing one is left alone, so to go
back to the default branch run the script again with `--ref main` / `-Ref main`. The ref is checked before anything runs:
letters, digits and `. _ / -` only, at most 200 characters, no leading `-`, no `..`, `//`, `@{`, trailing `/` or `.`,
no part starting with `.` or ending in `.lock`, and `git check-ref-format` where git is installed.

After setup, run every `claude-projects` command from the host side through the wrappers:

```bash
hosts/macos/claude-projects setup status            # Mac: runs ./claude-projects inside the VM
hosts\windows\claude-projects.cmd setup status      # Windows: runs ./claude-projects inside the distro
```

## Connecting an agent

| host | same machine | over SSH |
|---|---|---|
| Linux | `~/claude-projects-mcp/.venv/bin/python ~/claude-projects-mcp/cpg_mcp.py --stdio` | `ssh you@host <that command>` |
| Mac | `limactl shell claude-projects -- bash -lc '<that command>'` (single quotes: `~` must expand inside the VM) | `ssh you@mac "limactl shell claude-projects -- bash -lc '<that command>'"` (Remote Login on) |
| Windows | `wsl.exe -d ClaudeProjects -u cpg --exec /home/cpg/claude-projects-mcp/.venv/bin/python /home/cpg/claude-projects-mcp/cpg_mcp.py --stdio` | `ssh you@pc <that command>` (OpenSSH Server on) |

Or HTTP through your own tunnel or VPN to `127.0.0.1:8791`, with `bin/grok-bot-bridge` (see [Manual install](manual-install.md)).
`./claude-projects setup connect` prints the exact commands for the host it runs on.

## Why not drive the native Mac or Windows app directly?

That would need a second and third UI driver (macOS Accessibility, Windows UI Automation) for all 24 tools, each
tracking its own app build. One Linux driver behind a small runtime keeps every host on the same, tested code.
