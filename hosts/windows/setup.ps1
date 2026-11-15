<#
claude-projects-mcp on an always-on Windows PC.

The plugin drives the Claude desktop app through the Linux accessibility tree, so on Windows it runs inside its own
WSL2 distro ("ClaudeProjects", Ubuntu 24.04) with its own headless display. Your Windows Claude app is not touched.
A scheduled task keeps the distro running, and WSL forwards its localhost ports to Windows, so the sign-in viewport
opens in your normal browser at http://127.0.0.1:6179/vnc.html.

  powershell -ExecutionPolicy Bypass -File hosts\windows\setup.ps1 [-BotName NAME] [-OperatorName NAME] [-Timezone Area/City]
                                                                   [-AtStartup] [-WebDownload] [-NoSleep] [-DryRun]
  -AtStartup    also keep the distro running from boot, before anyone signs in (a second task that runs as you
                without a window; needs "Log on as a batch job", which normal accounts have)
  -WebDownload  get WSL and Ubuntu from GitHub / Canonical instead of the Microsoft Store (for Store-blocked PCs)
  -NoSleep      also stop the PC sleeping while on AC power (powercfg /change standby-timeout-ac 0)

Afterwards drive everything from Windows with hosts\windows\ccp.cmd (it runs ccp inside the distro), e.g.
  hosts\windows\ccp.cmd setup status

The distro's user "ccp" has no sudo rights: this script does the few root steps (packages, linger) itself with
`wsl -u root`, so nothing inside the distro can become root on its own.
#>
param(
  [string]$BotName = "",
  [string]$OperatorName = "",
  [string]$Timezone = "",
  [switch]$AtStartup,
  [switch]$WebDownload,
  [switch]$NoSleep,
  [switch]$DryRun,
  [ValidatePattern('^[A-Za-z0-9._-]+$')][string]$Distro = "ClaudeProjects"
)
$ErrorActionPreference = "Stop"
$env:WSL_UTF8 = "1"   # wsl.exe prints UTF-16 otherwise
$MinWsl = [version]"2.4.4"
$TaskName = "Claude Projects for Grok Bot"
$RepoUrl = if ($env:CCP_REPO_URL) { $env:CCP_REPO_URL } else { "https://github.com/BenItBuhner/Claude-Projects-for-Grok-Bot" }

function Say([string]$m) { Write-Host "==> $m" -ForegroundColor White }
function Note([string]$m) { Write-Host "    note: $m" -ForegroundColor Yellow }
function Invoke-Wsl([string[]]$WslArgs) {
  if ($DryRun) { Write-Host "    [dry-run] wsl.exe $($WslArgs -join ' ')"; return }
  & wsl.exe @WslArgs
  if ($LASTEXITCODE -ne 0) { throw "wsl.exe $($WslArgs -join ' ') exited $LASTEXITCODE" }
}
# Values that end up inside a single-quoted bash string: keep a conservative character set, never a quote.
function ConvertTo-BashSafe([string]$Value, [string]$Pattern) {
  $clean = ($Value -replace "[`r`n']", "").Trim()
  if ($clean -and $clean -notmatch $Pattern) { Note "ignoring '$clean': unexpected characters"; return "" }
  return $clean
}
# Run wsl.exe for its exit code / output only. Under Windows PowerShell 5.1 with $ErrorActionPreference = "Stop",
# native stderr redirected with 2> can become a terminating error, so relax it locally.
function Invoke-WslQuiet([string[]]$WslArgs) {
  $ErrorActionPreference = "Continue"
  try { $out = & wsl.exe @WslArgs 2>$null; return @{ Code = $LASTEXITCODE; Out = (@($out) -join "`n") } }
  catch { return @{ Code = 1; Out = "" } }
}
# Run a bash script inside the distro. The script never travels as a command-line string with quotes or $ in it:
# `wsl.exe -- ...` expands $name against the guest environment before bash sees it, and Windows PowerShell 5.1 does not
# escape embedded double quotes in native arguments. Instead it is base64 (only A-Z a-z 0-9 + / =), decoded by bash,
# and wsl.exe is called with --exec so nothing in between is a shell.
function Invoke-WslScript([string]$User, [string]$Script, [switch]$Login) {
  $body = "cd ~`n" + ($Script -replace "`r", "")   # no --cd: PowerShell 7 may expand a bare ~ to a Windows path
  $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($body))
  $bash = if ($Login) { "bash -l -s" } else { "bash -s" }
  if ($DryRun) { Write-Host "    [dry-run] wsl.exe -d $Distro -u $User --exec /bin/bash -c '<base64 script> | base64 -d | $bash'"; Write-Host ($body -replace '(?m)^', '      | '); return }
  Invoke-Wsl @("-d", $Distro, "-u", $User, "--exec", "/bin/bash", "-c", "echo $b64 | base64 -d | $bash")
}
function Get-WslVersion {
  $r = Invoke-WslQuiet @("--version")
  if ($r.Code -ne 0 -or -not $r.Out) { return $null }       # the old in-box WSL has no --version
  $m = [regex]::Match($r.Out, '(\d+\.\d+\.\d+(\.\d+)?)')
  if ($m.Success) { return [version]$m.Groups[1].Value } else { return $null }
}

# 1. WSL itself: present, and new enough for `wsl --install --name` (2.4.4+)
if ((Invoke-WslQuiet @("--status")).Code -ne 0) {
  $admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
  if (-not $admin) {
    Write-Host "WSL is not installed. Run this once in an ADMINISTRATOR PowerShell, reboot, then re-run this script:"
    Write-Host "  wsl --install --no-distribution$(if ($WebDownload) { ' --web-download' })"
    exit 3
  }
  Say "Installing WSL (a reboot is needed afterwards)"
  $a = @("--install", "--no-distribution"); if ($WebDownload) { $a += "--web-download" }
  Invoke-Wsl $a
  Write-Host "Reboot, then re-run this script."; exit 3
}
$ver = Get-WslVersion
if (-not $ver -or $ver -lt $MinWsl) {
  Say "Updating WSL (found $(if ($ver) { $ver } else { 'the in-box version' }), need $MinWsl or newer)"
  try { Invoke-Wsl @("--update") }
  catch { Note "the Store update failed ($($_.Exception.Message)); retrying from GitHub"; Invoke-Wsl @("--update", "--web-download") }
  if (-not $DryRun) {
    $ver = Get-WslVersion
    if (-not $ver -or $ver -lt $MinWsl) {
      Write-Host "WSL is still older than $MinWsl ($ver). Update it (Microsoft Store 'Windows Subsystem for Linux', or 'wsl --update --web-download' in an administrator PowerShell), then re-run."
      exit 3
    }
  }
}

# 2. A dedicated distro, so nothing touches your other WSL distros
$have = ((Invoke-WslQuiet @("-l", "-q")).Out -split "`n") -replace "`0", "" | ForEach-Object { $_.Trim() } | Where-Object { $_ -eq $Distro }
if (-not $have) {
  Say "Creating the WSL distro $Distro (Ubuntu 24.04)"
  $a = @("--install", "Ubuntu-24.04", "--name", $Distro, "--no-launch")
  if ($WebDownload) { Invoke-Wsl ($a + "--web-download") }
  else {
    try { Invoke-Wsl $a }
    catch { Note "the Store download failed ($($_.Exception.Message)); retrying with --web-download"; Invoke-Wsl ($a + "--web-download") }
  }
  Say "Preparing ${Distro}: user 'ccp' (no sudo), systemd on"
  $prep = @'
id ccp >/dev/null 2>&1 || useradd -m -s /bin/bash ccp
printf '[boot]\nsystemd=true\n[user]\ndefault=ccp\n' > /etc/wsl.conf
'@
  Invoke-WslScript root $prep
  Invoke-Wsl @("--terminate", $Distro)
}

# 3. Root steps, done from here so the ccp user needs no sudo: wait for systemd, linger, packages
Say "Installing system packages in $Distro (as root)"
$rootSteps = @'
set -u
for i in $(seq 1 30); do
  s="$(systemctl is-system-running 2>/dev/null || true)"
  case "$s" in running|degraded) break;; esac
  sleep 2
done
for i in $(seq 1 15); do loginctl enable-linger ccp 2>/dev/null && break; sleep 2; done
loginctl show-user ccp -p Linger --value 2>/dev/null | grep -qx yes || { echo "could not turn on linger for ccp" >&2; exit 1; }
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq && apt-get install -y -qq git ca-certificates >/dev/null
'@
Invoke-WslScript root $rootSteps

if (-not $Timezone) {
  try {
    $winTz = (Get-TimeZone).Id; $iana = $null
    if ([System.TimeZoneInfo]::TryConvertWindowsIdToIanaId($winTz, [ref]$iana)) { $Timezone = $iana }
  } catch { Write-Verbose "time zone lookup unavailable here (Windows PowerShell 5.1): $($_.Exception.Message); the distro's own zone is used" }
}
$safeBot = ConvertTo-BashSafe $BotName '^[\p{L}\p{N} ._-]+$'
$safeOp = ConvertTo-BashSafe $OperatorName '^[\p{L}\p{N} ._-]+$'
$safeTz = ConvertTo-BashSafe $Timezone '^[A-Za-z0-9_+/-]+$'
$safeUrl = ConvertTo-BashSafe $RepoUrl '^https://[A-Za-z0-9./_-]+$'
$safeHost = ConvertTo-BashSafe $env:COMPUTERNAME '^[A-Za-z0-9._-]+$'
$installArgs = @()
if ($safeBot) { $installArgs += "--bot-name '$safeBot'" }
if ($safeOp) { $installArgs += "--operator-name '$safeOp'" }
if ($safeTz) { $installArgs += "--timezone '$safeTz'" }

# 4. The plugin itself, as the unprivileged ccp user
$inner = "set -e; [ -d ~/claude-projects-mcp ] || git clone '$safeUrl' ~/claude-projects-mcp; cd ~/claude-projects-mcp; " +
         "./ccp setup choose windows_runtime --label 'WSL on $safeHost'; ./ccp setup preflight --host-kind windows-wsl || true"
Say "Fetching the plugin inside $Distro"
Invoke-WslScript ccp $inner -Login
Say "Installing the plugin's system packages and the Claude app (as root)"
Invoke-WslScript root "/home/ccp/claude-projects-mcp/install.sh --apt-only"
Say "Installing the plugin (as ccp)"
Invoke-WslScript ccp "cd ~/claude-projects-mcp && ./ccp setup install --no-apt $($installArgs -join ' ')" -Login

# 5. Keep the distro running (WSL stops idle distros otherwise). A hidden PowerShell starts a sleep inside it.
Say "Keeping $Distro running (scheduled task '$TaskName')"
$keep = "-NoProfile -NonInteractive -WindowStyle Hidden -Command `"& wsl.exe -d $Distro -u ccp --exec /bin/sleep infinity`""
if (-not $DryRun) {
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
              -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
  $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $keep
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME) -Settings $settings -Force | Out-Null
  Start-ScheduledTask -TaskName $TaskName
  if ($AtStartup) {
    try {
      $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Limited
      Register-ScheduledTask -TaskName "$TaskName (startup)" -Action $action -Trigger (New-ScheduledTaskTrigger -AtStartup) -Principal $principal -Settings $settings -Force | Out-Null
    } catch { Write-Warning "could not add the at-startup task ($($_.Exception.Message)); the at-sign-in task still works" }
  }
}

# 6. Always-on checks: warn, never abort after a good install
try {
  if ($NoSleep) { Say "Turning off sleep on AC power"; if (-not $DryRun) { & powercfg.exe /change standby-timeout-ac 0 } }
  else { Note "if this PC sleeps on AC power the plugin pauses. Fix: powercfg /change standby-timeout-ac 0 (or re-run with -NoSleep)." }
} catch { Write-Warning "could not change the sleep setting: $($_.Exception.Message)" }
try {
  if ((Get-CimInstance Win32_Battery -ErrorAction Stop | Measure-Object).Count -gt 0) { Note "this PC is a laptop: the plugin only works while it's awake and plugged in." }
} catch { Write-Warning "could not check for a battery: $($_.Exception.Message)" }

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Write-Host "`nDone. $Distro runs the plugin."
if ($AtStartup) { Write-Host "It starts at boot (and again when you sign in)." }
else { Write-Host "It starts when you sign in to Windows. For an always-on PC, turn on automatic sign-in, or re-run with -AtStartup." }
Write-Host "Next, from Windows:  $here\ccp.cmd setup status"
Write-Host "Sign-in opens in your browser at http://127.0.0.1:6179/vnc.html once you run:  $here\ccp.cmd setup signin start"
