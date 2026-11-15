<#
claude-projects-mcp on an always-on Windows PC.

The plugin drives the Claude desktop app through the Linux accessibility tree, so on Windows it runs inside its own
WSL2 distro ("ClaudeProjects", Ubuntu 24.04) with its own headless display. Your Windows Claude app is not touched.
A scheduled task keeps the distro running, and WSL forwards its localhost ports to Windows, so the sign-in viewport
opens in your normal browser at http://127.0.0.1:6179/vnc.html.

  powershell -ExecutionPolicy Bypass -File hosts\windows\setup.ps1 [-BotName NAME] [-OperatorName NAME] [-Timezone Area/City]
                                                                   [-Ref REF] [-AtStartup] [-WebDownload] [-NoSleep] [-DryRun]
  -Ref REF      install this branch, tag or full commit sha instead of the default branch (also: $env:CPG_REPO_REF;
                the parameter wins). On a re-run the existing clone fetches REF and checks it out, unless it has local
                changes. Example: powershell -ExecutionPolicy Bypass -File hosts\windows\setup.ps1 -Ref release-1.0.2
  -AtStartup    also keep the distro running from boot, before anyone signs in (a second task that runs as you
                without a window; needs "Log on as a batch job", which normal accounts have)
  -WebDownload  get WSL and Ubuntu from GitHub / Canonical instead of the Microsoft Store (for Store-blocked PCs)
  -NoSleep      also stop the PC sleeping while on AC power (powercfg /change standby-timeout-ac 0)

Afterwards drive everything from Windows with hosts\windows\claude-projects.cmd (it runs claude-projects inside the distro), e.g.
  hosts\windows\claude-projects.cmd setup status

The distro's user "cpg" has no sudo rights: this script does the few root steps (packages, linger) itself with
`wsl -u root`, so nothing inside the distro can become root on its own.
#>
param(
  [string]$BotName = "",
  [string]$OperatorName = "",
  [string]$Timezone = "",
  [string]$Ref = "",
  [switch]$AtStartup,
  [switch]$WebDownload,
  [switch]$NoSleep,
  [switch]$DryRun,
  [ValidatePattern('^[A-Za-z0-9._-]+$')][string]$Distro = "ClaudeProjects"
)
$ErrorActionPreference = "Stop"
# An unexpected failure ends with one readable line, not a PowerShell stack trace. Expected failures (old WSL, no WSL2)
# go through Exit-Setup, which says what to do next.
trap { Write-Host "`nsetup.ps1 stopped: $($_.Exception.Message)" -ForegroundColor Red; Write-Host "Fix the cause above and re-run this script; every step is safe to repeat."; exit 1 }
$env:WSL_UTF8 = "1"   # wsl.exe prints UTF-16 otherwise
$MinWsl = [version]"2.4.4"
$TaskName = "Claude Projects for Grok Bot"
# CCP_* are the old names of the CPG_* variables (before 1.0.2) and still work; the CPG_* name wins
foreach ($n in @("REPO_URL", "REPO_REF")) { if (-not [Environment]::GetEnvironmentVariable("CPG_$n") -and [Environment]::GetEnvironmentVariable("CCP_$n")) { [Environment]::SetEnvironmentVariable("CPG_$n", [Environment]::GetEnvironmentVariable("CCP_$n")) } }
$RepoUrl = if ($env:CPG_REPO_URL) { $env:CPG_REPO_URL } else { "https://github.com/BenItBuhner/Claude-Projects-for-Grok-Bot" }

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
# wsl.exe output, decoded. A current wsl.exe prints UTF-8 with WSL_UTF8=1; the old inbox one ignores that and prints
# UTF-16LE, with or without a BOM. Detect UTF-16 by the BOM or by NULs in the odd bytes (ASCII text), else UTF-8; then
# drop NULs, BOMs and CRs.
function ConvertFrom-WslOutput([byte[]]$Bytes) {
  if (-not $Bytes -or $Bytes.Length -eq 0) { return "" }
  $start = 0; $enc = New-Object System.Text.UTF8Encoding($false)
  if ($Bytes.Length -ge 2 -and $Bytes[0] -eq 0xFF -and $Bytes[1] -eq 0xFE) { $enc = [System.Text.Encoding]::Unicode; $start = 2 }
  elseif ($Bytes.Length -ge 3 -and $Bytes[0] -eq 0xEF -and $Bytes[1] -eq 0xBB -and $Bytes[2] -eq 0xBF) { $start = 3 }
  else {
    $pairs = [int][Math]::Floor($Bytes.Length / 2); $oddNul = 0
    for ($i = 1; $i -lt $Bytes.Length; $i += 2) { if ($Bytes[$i] -eq 0) { $oddNul++ } }
    if ($pairs -gt 0 -and $oddNul -ge [Math]::Ceiling($pairs / 2)) { $enc = [System.Text.Encoding]::Unicode }
  }
  $text = $enc.GetString($Bytes, $start, $Bytes.Length - $start)
  return $text.Replace([string][char]0xFEFF, "").Replace([string][char]0, "").Replace("`r", "")
}
# Run wsl.exe for its exit code / output only: read the raw bytes (PowerShell would decode them with the console code
# page) and decode them with ConvertFrom-WslOutput. Never throws; a missing wsl.exe is exit code 1. The arguments here
# are plain words (no spaces or quotes), so joining them is safe.
function Invoke-WslQuiet([string[]]$WslArgs) {
  $ErrorActionPreference = "Continue"
  try {
    $exe = Get-Command wsl.exe -CommandType Application -ErrorAction Stop | Select-Object -First 1
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $exe.Path; $psi.Arguments = ($WslArgs -join ' ')
    $psi.UseShellExecute = $false; $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true
    $p = [System.Diagnostics.Process]::Start($psi)
    $errTask = $p.StandardError.BaseStream.CopyToAsync([System.IO.Stream]::Null)
    $ms = New-Object System.IO.MemoryStream
    $p.StandardOutput.BaseStream.CopyTo($ms)
    if (-not $p.WaitForExit(600000)) { $p.Kill(); return @{ Code = 1; Out = "" } }
    $errTask.Wait()
    return @{ Code = $p.ExitCode; Out = (ConvertFrom-WslOutput $ms.ToArray()) }
  } catch { return @{ Code = 1; Out = "" } }
}
function Test-Admin {
  try { return ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator) }
  catch { return $false }
}
# An expected stop: say why and what to do next, then exit (no stack trace).
function Exit-Setup([string]$Message, [string[]]$Steps, [int]$Code = 3) {
  Write-Host ""; Write-Host $Message -ForegroundColor Yellow
  if ($Steps) { Write-Host "Next steps:"; $n = 1; foreach ($step in $Steps) { Write-Host "  $n. $step"; $n++ } }
  exit $Code
}
$WslHelp = @(
  ("In an ADMINISTRATOR PowerShell, turn on the two Windows features WSL2 needs:`n" +
   "       dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart`n" +
   "       dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart"),
  "Reboot.",
  "Install the current WSL package (the .msi for your CPU, x64 or arm64) from https://github.com/microsoft/WSL/releases",
  "Re-run this script. (On a virtual machine or cloud PC, WSL2 also needs nested virtualization turned on.)"
)
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
# The ref crosses PowerShell -> wsl.exe -> bash, so only a strict subset of git's ref names gets through. \A and \z,
# not ^ and $ (which also match before a trailing newline), and -cmatch, so [A-Za-z] is not widened by case folding.
function Get-RefProblem([string]$Value) {
  if ($Value.Length -gt 200) { return "is longer than 200 characters" }
  if ($Value.Contains('@{')) { return "must not contain @{" }
  if ($Value -cnotmatch '\A[A-Za-z0-9._/-]+\z') { return "may only use letters, digits and . _ / -" }
  if ($Value.StartsWith('-')) { return "must not start with -" }
  if ($Value.StartsWith('/') -or $Value.EndsWith('/')) { return "must not start or end with /" }
  if ($Value.Contains('..')) { return "must not contain .." }
  if ($Value.Contains('//')) { return "must not contain //" }
  if ($Value -cmatch '(\A|/)\.') { return "no part may start with ." }
  if ($Value -cmatch '\.lock(/|\z)') { return "no part may end with .lock" }
  if ($Value.EndsWith('.')) { return "must not end with ." }
  return $null
}
# Clone the plugin when it isn't there, then (only with a ref) fetch that ref from origin and check it out. The same
# text is in hosts/macos/setup.sh; tests/test_setup.py checks they match and runs it.
$RefCheckout = @'
# cpg-ref-checkout: begin
D="$HOME/claude-projects-mcp"
[ -d "$D" ] || git clone --quiet "$URL" "$D"
if [ -n "$REF" ]; then
  cd "$D"
  [ -d .git ] || { echo "$D is not a git clone; move it away and re-run" >&2; exit 1; }
  git check-ref-format --allow-onelevel "$REF" || { echo "not a valid git ref name: $REF" >&2; exit 1; }
  if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "$D has local changes, so it was left as it is. Commit or stash them (git -C $D status), then re-run." >&2
    exit 1
  fi
  if git ls-remote --exit-code --heads origin "refs/heads/$REF" >/dev/null 2>&1; then
    git fetch --quiet origin "+refs/heads/$REF:refs/remotes/origin/$REF"
    if git rev-parse -q --verify "refs/heads/$REF" >/dev/null && ! git merge-base --is-ancestor "refs/heads/$REF" "refs/remotes/origin/$REF"; then
      echo "$D has local commits on $REF that origin doesn't have, so it was left as it is." >&2
      exit 1
    fi
    git checkout --quiet -B "$REF" "refs/remotes/origin/$REF"
  elif git ls-remote --exit-code --tags origin "refs/tags/$REF" >/dev/null 2>&1; then
    git fetch --quiet --force origin "refs/tags/$REF:refs/tags/$REF"
    git checkout --quiet --detach "refs/tags/$REF"
  elif printf '%s' "$REF" | grep -Eqx '[0-9a-f]{40}'; then
    git fetch --quiet origin "$REF" 2>/dev/null || git fetch --quiet origin
    git cat-file -e "$REF^{commit}" 2>/dev/null || { echo "commit $REF is not on origin" >&2; exit 1; }
    git checkout --quiet --detach "$REF"
  else
    echo "$REF is not a branch, a tag or a full 40-character commit sha on origin" >&2
    exit 1
  fi
  echo "claude-projects-mcp is at $REF ($(git rev-parse --short HEAD))"
fi
# cpg-ref-checkout: end
'@
function Get-FetchScript([string]$Url, [string]$Ref) {
  return "set -e`nURL='$Url'`nREF='$Ref'`n" + $RefCheckout
}
function Get-WslVersion {
  $r = Invoke-WslQuiet @("--version")
  if ($r.Code -ne 0 -or -not $r.Out) { return $null }       # the old inbox WSL has no --version
  # the first line names WSL in every language ("WSL version: 2.4.4.0", "WSL-Version: ...", "Version de WSL : ...")
  $line = ($r.Out -split "`n") | Where-Object { $_ -match 'WSL' -and $_ -match '\d+\.\d+\.\d+' } | Select-Object -First 1
  if (-not $line) { return $null }
  $m = [regex]::Match($line, '(\d+\.\d+\.\d+(\.\d+)?)')
  if ($m.Success) { return [version]$m.Groups[1].Value } else { return $null }
}
# The WSL version (1 or 2) of one distro, from `wsl -l -v`, or $null when it isn't listed.
function Get-DistroWslVersion([string]$Name) {
  foreach ($ln in ((Invoke-WslQuiet @("-l", "-v")).Out -split "`n")) {
    $cols = ($ln -replace '^\s*\*?\s*', '').Trim() -split '\s+'
    if ($cols.Count -ge 3 -and $cols[0] -eq $Name) { return $cols[-1] }
  }
  return $null
}

# 0. The ref, checked before anything runs
if (-not $PSBoundParameters.ContainsKey('Ref') -and $env:CPG_REPO_REF) { $Ref = $env:CPG_REPO_REF }
elseif ($PSBoundParameters.ContainsKey('Ref') -and -not $Ref) { Write-Host "-Ref needs a value"; exit 2 }
if ($Ref) {
  $problem = Get-RefProblem $Ref
  if ($problem) { Write-Host "-Ref: '$Ref' $problem (a branch, tag or full commit sha, such as release-1.0.2)"; exit 2 }
  if (Get-Command git -CommandType Application -ErrorAction SilentlyContinue) {
    & git check-ref-format --allow-onelevel $Ref
    if ($LASTEXITCODE -ne 0) { Write-Host "-Ref: '$Ref' is not a valid git ref name"; exit 2 }
  }
}

# 1. WSL itself: the WSL package 2.4.4+ (it has `wsl --version` and `wsl --install --name`), with WSL2 working.
#    The old inbox WSL (e.g. Windows Server 2022) has no --version: update it first, and stop with next steps if that
#    doesn't work.
if (-not (Get-Command wsl.exe -CommandType Application -ErrorAction SilentlyContinue)) {
  Exit-Setup "wsl.exe isn't on this PC, so WSL2 can't run here yet." $WslHelp
}
$ver = Get-WslVersion
if (-not $ver -or $ver -lt $MinWsl) {
  $found = if ($ver) { "WSL $ver" } else { "the old inbox WSL, without 'wsl --version'" }
  Say "Updating WSL (found $found; $MinWsl or newer is needed)"
  foreach ($a in @(, @("--update", "--web-download")) + @(, @("--update"))) {
    if ($DryRun) { Write-Host "    [dry-run] wsl.exe $($a -join ' ')"; continue }
    $r = Invoke-WslQuiet $a
    $ver = Get-WslVersion
    if ($ver -and $ver -ge $MinWsl) { break }
    Note "wsl.exe $($a -join ' ') did not bring WSL to $MinWsl (exit code $($r.Code))"
  }
  if (-not $DryRun -and (-not $ver -or $ver -lt $MinWsl)) {
    $now = if ($ver) { "WSL $ver" } else { "the old inbox WSL, without 'wsl --version'" }
    Exit-Setup "WSL2 isn't available on this PC yet: it has $now, and updating it didn't work. WSL $MinWsl or newer is needed." $WslHelp
  }
}
$st = Invoke-WslQuiet @("--status")
if ($st.Code -ne 0 -and -not $DryRun) {
  if (Test-Admin) {
    Say "Turning on WSL2's Windows features (a reboot is needed afterwards)"
    $a = @("--install", "--no-distribution"); if ($WebDownload) { $a += "--web-download" }
    if ((Invoke-WslQuiet $a).Code -eq 0) { Exit-Setup "WSL2's Windows features are on now." @("Reboot.", "Re-run this script.") }
  }
  $why = (($st.Out -split "`n") | Where-Object { $_.Trim() } | Select-Object -First 2) -join " "
  Exit-Setup "WSL is installed but WSL2 can't start yet ('wsl --status' failed$(if ($why) { ": $why" }))." $WslHelp
}
Say "Making WSL 2 the default for new distros"
if ($DryRun) { Write-Host "    [dry-run] wsl.exe --set-default-version 2" }
elseif ((Invoke-WslQuiet @("--set-default-version", "2")).Code -ne 0) {
  Exit-Setup "WSL 2 can't be made the default ('wsl --set-default-version 2' failed), so WSL2 isn't working on this PC yet." $WslHelp
}

# 2. A dedicated distro, so nothing touches your other WSL distros
$have = ((Invoke-WslQuiet @("-l", "-q")).Out -split "`n") | ForEach-Object { $_.Trim() } | Where-Object { $_ -eq $Distro }
if (-not $have) {
  Say "Creating the WSL distro $Distro (Ubuntu 24.04)"
  $a = @("--install", "Ubuntu-24.04", "--name", $Distro, "--no-launch")
  try {
    if ($WebDownload) { Invoke-Wsl ($a + "--web-download") }
    else {
      try { Invoke-Wsl $a }
      catch { Note "the Store download failed ($($_.Exception.Message)); retrying with --web-download"; Invoke-Wsl ($a + "--web-download") }
    }
  } catch {
    Exit-Setup "Couldn't create the WSL distro $Distro ($($_.Exception.Message))." (@("Check that WSL2 works: 'wsl --status' should say Default Version: 2.") + $WslHelp)
  }
  Say "Preparing ${Distro}: user 'cpg' (no sudo), systemd on"
  $prep = @'
id cpg >/dev/null 2>&1 || useradd -m -s /bin/bash cpg
printf '[boot]\nsystemd=true\n[user]\ndefault=cpg\n' > /etc/wsl.conf
'@
  Invoke-WslScript root $prep
  Invoke-Wsl @("--terminate", $Distro)
}
if (-not $DryRun -and (Get-DistroWslVersion $Distro) -eq "1") {
  Say "Converting $Distro to WSL 2"
  try { Invoke-Wsl @("--set-version", $Distro, "2") }
  catch { Exit-Setup "Couldn't convert $Distro to WSL 2 ($($_.Exception.Message))." $WslHelp }
}

# 3. Root steps, done from here so the cpg user needs no sudo: wait for systemd, linger, packages
Say "Installing system packages in $Distro (as root)"
$rootSteps = @'
set -u
for i in $(seq 1 30); do
  s="$(systemctl is-system-running 2>/dev/null || true)"
  case "$s" in running|degraded) break;; esac
  sleep 2
done
# a distro made before 1.0.2 has the user "ccp": add "cpg" next to it and make it the default
id cpg >/dev/null 2>&1 || useradd -m -s /bin/bash cpg
grep -qx 'default=cpg' /etc/wsl.conf 2>/dev/null || printf '[boot]\nsystemd=true\n[user]\ndefault=cpg\n' > /etc/wsl.conf
for i in $(seq 1 15); do loginctl enable-linger cpg 2>/dev/null && break; sleep 2; done
loginctl show-user cpg -p Linger --value 2>/dev/null | grep -qx yes || { echo "could not turn on linger for cpg" >&2; exit 1; }
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

# 4. The plugin itself, as the unprivileged cpg user
$inner = (Get-FetchScript $safeUrl $Ref) + "`ncd ~/claude-projects-mcp`n" +
         "C=./claude-projects; [ -x `$C ] || C=./ccp   # a ref from before 1.0.2 has the CLI under its old name`n" +
         "`$C setup choose windows_runtime --label 'WSL on $safeHost'`n`$C setup preflight --host-kind windows-wsl || true"
Say "Fetching the plugin inside $Distro"
Invoke-WslScript cpg $inner -Login
Say "Installing the plugin's system packages and the Claude app (as root)"
Invoke-WslScript root "/home/cpg/claude-projects-mcp/install.sh --apt-only"
Say "Installing the plugin (as cpg)"
Invoke-WslScript cpg "cd ~/claude-projects-mcp && C=./claude-projects && { [ -x `$C ] || C=./ccp; } && `$C setup install --no-apt $($installArgs -join ' ')" -Login

# 5. Keep the distro running (WSL stops idle distros otherwise). A hidden PowerShell starts a sleep inside it.
Say "Keeping $Distro running (scheduled task '$TaskName')"
$keep = "-NoProfile -NonInteractive -WindowStyle Hidden -Command `"& wsl.exe -d $Distro -u cpg --exec /bin/sleep infinity`""
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
Write-Host "Next, from Windows:  $here\claude-projects.cmd setup status"
Write-Host "Sign-in opens in your browser at http://127.0.0.1:6179/vnc.html once you run:  $here\claude-projects.cmd setup signin start"
