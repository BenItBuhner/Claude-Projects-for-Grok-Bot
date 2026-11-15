# Windows host by hand (WSL2)

Use this when the plugin should run on an always-on Windows PC and `hosts\windows\setup.ps1` isn't used (you only have
a shell on the PC, the script failed halfway, or the user wants to see each step). The script does exactly these
steps; if it's there, prefer `powershell -ExecutionPolicy Bypass -File hosts\windows\setup.ps1 [-BotName NAME] [-Ref REF] [-AtStartup] [-WebDownload] [-NoSleep] [-DryRun]`.
For a pre-release, add `-Ref release-1.0.2` (a branch, a tag or a full commit sha; `$env:CCP_REPO_REF = 'release-1.0.2'`
works too, and the parameter wins).

## Why WSL

The plugin drives the Claude desktop app through the **Linux** accessibility tree (AT-SPI) on a private headless X
display. The Windows Claude app can't be driven that way, so the plugin runs the Linux build of the app in its own
WSL2 distro. The Windows Claude app and the user's other WSL distros are never touched. An always-on Linux machine is
still the recommended host; a PC works as long as it stays on, signed in (or with the at-startup task) and awake.

Needs: Windows 10 22H2 or Windows 11 with virtualization on, WSL 2.4.4 or newer (for `wsl --install --name`), about
4 GB RAM and 20 GB disk free. Commands below are PowerShell on Windows unless they say "inside the distro".

## 1. WSL

```powershell
wsl --status                        # fails -> not installed
wsl --install --no-distribution     # ADMINISTRATOR PowerShell, then reboot (add --web-download if the Store is blocked)
wsl --version                       # first line: WSL version 2.4.4.0 or newer; else:
wsl --update                        # or: wsl --update --web-download
```

## 2. A dedicated distro with systemd

```powershell
wsl --install Ubuntu-24.04 --name ClaudeProjects --no-launch      # add --web-download if the Store is blocked
```

Give it an unprivileged user `ccp` (no sudo on purpose: root steps run from Windows with `-u root`, so nothing inside
the distro can become root on its own), turn systemd on, and make `ccp` the default user.

**Passing scripts to WSL safely.** Don't send a bash command with quotes or `$` through `wsl.exe -- ...`: `wsl.exe`
expands `$name` against the Linux environment before bash runs, Windows PowerShell 5.1 mangles embedded double
quotes in native arguments, and PowerShell 7 turns a bare `~` into a Windows path. Send scripts as base64 through
`--exec` instead (this is what the setup script does):

```powershell
function Invoke-Distro([string]$User, [string]$Script) {
  $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("cd ~`n" + ($Script -replace "`r", "")))
  wsl.exe -d ClaudeProjects -u $User --exec /bin/bash -c "echo $b64 | base64 -d | bash -l -s"
  if ($LASTEXITCODE -ne 0) { throw "failed ($LASTEXITCODE)" }
}

Invoke-Distro root @'
id ccp >/dev/null 2>&1 || useradd -m -s /bin/bash ccp
printf '[boot]\nsystemd=true\n[user]\ndefault=ccp\n' > /etc/wsl.conf
'@
wsl --terminate ClaudeProjects       # the next start boots with systemd
```

## 3. Root steps from Windows: linger, git

```powershell
Invoke-Distro root @'
for i in $(seq 1 30); do case "$(systemctl is-system-running 2>/dev/null)" in running|degraded) break;; esac; sleep 2; done
for i in $(seq 1 15); do loginctl enable-linger ccp && break; sleep 2; done
apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git ca-certificates
'@
```

## 4. The plugin (same guided steps as Linux)

```powershell
Invoke-Distro ccp 'git clone https://github.com/BenItBuhner/Claude-Projects-for-Grok-Bot ~/claude-projects-mcp'
Invoke-Distro ccp "cd ~/claude-projects-mcp && ./ccp setup choose windows_runtime --label 'WSL on $env:COMPUTERNAME' && ./ccp setup preflight --host-kind windows-wsl"
Invoke-Distro root '/home/ccp/claude-projects-mcp/install.sh --apt-only'          # packages + the Claude app, as root
Invoke-Distro ccp 'cd ~/claude-projects-mcp && ./ccp setup install --no-apt --bot-name "<bot name>"'
Invoke-Distro ccp 'cd ~/claude-projects-mcp && ./ccp setup status'
```

**A pre-release branch, tag or commit** (what `-Ref` / `CCP_REPO_REF` do). Only use a ref made of letters, digits and
`. _ / -` (no leading `-`, no `..`). `Invoke-Distro` carries the script as base64, so quoting is safe, but a strict ref
keeps the git commands unambiguous. Check the clone is clean first; if the first line prints anything, stop and ask the
user instead of overwriting their changes:

```powershell
Invoke-Distro ccp 'cd ~/claude-projects-mcp && git status --porcelain --untracked-files=no'   # must print nothing
# a branch:
Invoke-Distro ccp 'cd ~/claude-projects-mcp && git fetch origin +refs/heads/release-1.0.2:refs/remotes/origin/release-1.0.2 && git checkout -B release-1.0.2 origin/release-1.0.2'
# or a tag:
Invoke-Distro ccp 'cd ~/claude-projects-mcp && git fetch --force origin refs/tags/v1.0.1:refs/tags/v1.0.1 && git checkout --detach v1.0.1'
```

Then run the `install.sh --apt-only` and `./ccp setup install --no-apt` lines again (re-running `setup.ps1 -Ref
release-1.0.2` does all of this).

When a result says `needs_sudo` or gives an `as_root` line, run that line through `Invoke-Distro root` from Windows:
`ccp` itself has no sudo. Afterwards `hosts\windows\ccp.cmd setup <command>` (from cmd) does the same as
`Invoke-Distro ccp 'cd ~/claude-projects-mcp && ./ccp setup <command>'`.

## 5. Keep the distro running

WSL shuts a distro down soon after its last Windows-side process exits, which stops the plugin. A hidden scheduled
task keeps one `sleep` running in it from sign-in (and, optionally, from boot):

```powershell
$keep = "-NoProfile -NonInteractive -WindowStyle Hidden -Command `"& wsl.exe -d ClaudeProjects -u ccp --exec /bin/sleep infinity`""
$set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
       -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
$act = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $keep
Register-ScheduledTask -TaskName "Claude Projects for Grok Bot" -Action $act -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME) -Settings $set -Force
Start-ScheduledTask -TaskName "Claude Projects for Grok Bot"
# optional, before anyone signs in (runs as the user without a window; needs "Log on as a batch job"):
# Register-ScheduledTask -TaskName "Claude Projects for Grok Bot (startup)" -Action $act -Trigger (New-ScheduledTaskTrigger -AtStartup) `
#   -Principal (New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Limited) -Settings $set -Force
```

Without the startup task, turn on automatic sign-in for an always-on PC. Inside the distro, systemd starts the
plugin's services (linger is on).

## 6. Sleep and laptops

```powershell
powercfg /query SCHEME_CURRENT SUB_SLEEP STANDBYIDLE     # AC value 0x00000000 = never sleeps on power
(Get-CimInstance Win32_Battery | Measure-Object).Count  # > 0 = laptop
powercfg /change standby-timeout-ac 0                    # stop sleeping on AC: ask the user first
```

A sleeping PC pauses the plugin. A laptop works only while it's awake and plugged in: say so before installing.

## 7. Sign-in, viewer link and password file

WSL forwards ports the distro binds on its localhost to Windows `127.0.0.1` (the default `localhostForwarding`), so
after `./ccp setup signin start` the user opens `http://127.0.0.1:6179/vnc.html?autoconnect=1&resize=scale` (display
`:99`) in their normal Windows browser. The viewer password is saved **inside the distro** at
`/home/ccp/.config/claude-projects-mcp/vncpass.txt`; the user reads it with
`wsl.exe -d ClaudeProjects -u ccp --exec cat /home/ccp/.config/claude-projects-mcp/vncpass.txt` (never paste it into
chat yourself). In the app they click "Get started" (older versions: "Sign in"). Then `signin wait` and `check-beta`.

## 8. Connect the agent and verify

| agent runs | command |
|---|---|
| on the PC | `wsl.exe -d ClaudeProjects -u ccp --exec /home/ccp/claude-projects-mcp/.venv/bin/python /home/ccp/claude-projects-mcp/ccp_mcp.py --stdio` |
| elsewhere, SSH to the PC (OpenSSH Server on) | `ssh <user>@<pc> wsl.exe -d ClaudeProjects -u ccp --exec /home/ccp/claude-projects-mcp/.venv/bin/python /home/ccp/claude-projects-mcp/ccp_mcp.py --stdio` |
| HTTP | `http://127.0.0.1:8791/mcp` on the PC, bearer token from `/home/ccp/.config/claude-projects-mcp/mcp.token` in the distro |

```powershell
Invoke-Distro ccp 'cd ~/claude-projects-mcp && ./ccp setup verify --live'
wsl -l -v                                            # ClaudeProjects  Running  2
Get-ScheduledTask -TaskName "Claude Projects for Grok Bot" | Select-Object State
curl.exe -s http://127.0.0.1:8791/healthz            # ok (when the HTTP server is on)
```

## Common failures

| symptom | fix |
|---|---|
| `wsl --version` isn't recognized, `--install --no-distribution` isn't either, `Default Version: 1` (old inbox WSL, e.g. Windows Server 2022) | `wsl --update --web-download` (or `wsl --update`); if `wsl --version` still fails: as admin `dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart` and the same for `VirtualMachinePlatform`, reboot, install the `.msi` from https://github.com/microsoft/WSL/releases, then `wsl --set-default-version 2` |
| `wsl.exe` output looks like `W S L` with gaps, or parsing it finds nothing | the old wsl.exe prints UTF-16LE and ignores `WSL_UTF8`: decode the raw bytes as UTF-16LE (and drop NULs), as `setup.ps1` does |
| `wsl --install` asks for admin or says WSL is missing | run it once in an administrator PowerShell, reboot, re-run |
| `--name` is an unknown option | WSL older than 2.4.4: `wsl --update` (or `--web-download`) |
| Store download fails or is blocked | add `--web-download` to `--install` and `--update` |
| `systemctl --user` fails / `System has not been booted with systemd` | `/etc/wsl.conf` lacks `[boot] systemd=true`, or the distro wasn't restarted: `wsl --terminate ClaudeProjects` |
| `loginctl enable-linger` fails right after the first start | systemd is still booting: wait for `systemctl is-system-running` to say running/degraded, retry |
| `$HOME`, `$USER` or quotes arrive empty or broken | the command went through `wsl.exe -- ...` or PowerShell 5.1 quoting: use the base64 `Invoke-Distro` above |
| a path like `C:\Users\...` shows up inside the distro | PowerShell 7 expanded a bare `~`: don't use `--cd ~`; put `cd ~` inside the script |
| plugin stops a few minutes after the window closes | the keep-alive task isn't running: `Start-ScheduledTask -TaskName "Claude Projects for Grok Bot"` |
| `127.0.0.1:6179` doesn't load in Windows | the service isn't up (`./ccp setup status`), or `localhostForwarding=false` / a custom `networkingMode` in `%UserProfile%\.wslconfig` |
| `needs_sudo` loops | `ccp` has no sudo by design: run the printed line with `Invoke-Distro root` |

Remove: `./ccp setup uninstall` inside the distro, then
`Get-ScheduledTask -TaskName "Claude Projects for Grok Bot*" | Unregister-ScheduledTask -Confirm:$false` and
`wsl --unregister ClaudeProjects` (ask the user first; it deletes the distro and its sign-in).
