# computer-permission.ps1: read, and press only on request, the computer-use card ("Claude wants to use: <apps>" [Deny]
# [Allow for this session]) in the Claude desktop app on the Windows PC that Claude controls. The grant is bound to that
# computer: the app on any other desktop can only send Deny, so Allow has to be pressed here. An agent runs this script on
# the PC (one call per action) and decides with claude-projects (`claude-projects computer-card decide`, see
# docs/how-it-works.md). It reads ONE JSON request on stdin and writes ONE JSON result on stdout.
#
#   {"action": "snapshot"}
#       Read-only. For each visible window of claude.exe: the window, and a node snapshot of every block that holds an
#       Allow / Deny style button ({id, parent, role, name, text, rid}). The decision is made by claude-projects, not here.
#   {"action": "press", "hwnd": 123, "rid": "42.1.2", "label": "Allow for this session", "title": "Claude wants to use"}
#       Presses ONE button with UI Automation's Invoke (no mouse move, no focus change, no keystrokes), and only when the
#       button is in a claude.exe window, its name is exactly `label`, its runtime id is `rid`, it is an Allow / Deny style
#       label, and a block around it still shows `title`. Anything else refuses and presses nothing.
#
# Nothing here uses the clipboard, types, moves windows, focuses anything, or starts or stops a process. It only sees a card
# the app is rendering (the conversation open in a visible window); it never navigates the app.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$A = [System.Windows.Automation.AutomationElement]
$TS = [System.Windows.Automation.TreeScope]
$CT = [System.Windows.Automation.ControlType]
$Walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
$OptionRe = '^(allow|allow once|allow for this session|allow in this session|allow session|always allow|allow always|deny|don.?t allow|do not allow|continue|not now)$'
$MaxUp = 8; $MaxNodes = 1500

function Out-Json($o) { [Console]::Out.Write(($o | ConvertTo-Json -Depth 8 -Compress)) }
function Rid($e) { try { ($e.GetRuntimeId() | ForEach-Object { $_ }) -join '.' } catch { '' } }
function Role($e) {
    $t = $e.Current.ControlType
    if ($t -eq $CT::Button) { return 'push button' }
    if ($t -eq $CT::Text) { return 'static' }
    if ($t -eq $CT::Hyperlink) { return 'link' }
    if ($t -eq $CT::Heading) { return 'heading' }
    if ($t -eq $CT::ListItem) { return 'list item' }
    return ($t.ProgrammaticName -replace '^ControlType\.', '').ToLower()
}
Add-Type @'
using System; using System.Collections.Generic; using System.Runtime.InteropServices;
public static class ClaudeProjectsWin {
    delegate bool EnumProc(IntPtr h, IntPtr l);
    [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc p, IntPtr l);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr h);
    public static List<long> Visible(int[] pids) {
        var set = new HashSet<int>(pids); var r = new List<long>();
        EnumWindows((h, l) => { uint p; GetWindowThreadProcessId(h, out p); if (set.Contains((int)p) && IsWindowVisible(h)) r.Add(h.ToInt64()); return true; }, IntPtr.Zero);
        return r;
    }
}
'@
function Claude-Windows {
    # top-level windows of claude.exe, found with EnumWindows (UI Automation's root does not always list a packaged app's window)
    $ids = [int[]]@(Get-Process -Name claude -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
    $out = @()
    if ($ids.Count -eq 0) { return $out }
    foreach ($h in [ClaudeProjectsWin]::Visible($ids)) {
        try { $w = $A::FromHandle([IntPtr]$h); if ($w -ne $null) { $out += $w } } catch { }
    }
    return $out
}
function Option-Buttons($w) {
    $cond = New-Object System.Windows.Automation.PropertyCondition($A::ControlTypeProperty, $CT::Button)
    $res = @()
    foreach ($b in $w.FindAll($TS::Descendants, $cond)) { $n = ("" + $b.Current.Name).Trim(); if ($n -match $OptionRe) { $res += $b } }
    return $res
}
function Snapshot-Block($top) {
    $nodes = New-Object System.Collections.ArrayList
    $stack = New-Object System.Collections.Stack
    $stack.Push(@($top, $null))
    while ($stack.Count -gt 0 -and $nodes.Count -lt $MaxNodes) {
        $pair = $stack.Pop(); $e = $pair[0]; $par = $pair[1]; $id = $nodes.Count
        $name = ''; try { $name = ("" + $e.Current.Name) } catch { }
        [void]$nodes.Add([ordered]@{ id = $id; parent = $par; role = (Role $e); name = $name.Substring(0, [Math]::Min(500, $name.Length)); text = ''; rid = (Rid $e) })
        $kids = @(); $c = $Walker.GetFirstChild($e)
        while ($c -ne $null) { $kids += $c; $c = $Walker.GetNextSibling($c) }
        for ($i = $kids.Count - 1; $i -ge 0; $i--) { $stack.Push(@($kids[$i], $id)) }
    }
    return , $nodes
}
function Block-Of($b) {
    # the block around a button: walk up MaxUp ancestors, staying inside the window
    $top = $b
    for ($i = 0; $i -lt $MaxUp; $i++) {
        $p = $Walker.GetParent($top)
        if ($p -eq $null -or $p.Current.ControlType -eq $CT::Window) { break }
        $top = $p
    }
    return $top
}
function Block-Text($top) {
    $parts = @(); foreach ($n in (Snapshot-Block $top)) { if ($n.role -ne 'push button' -and $n.name) { $parts += $n.name } }
    return ($parts -join ' ')
}

$req = [Console]::In.ReadToEnd() | ConvertFrom-Json
if ($req.action -eq 'snapshot') {
    $wins = @()
    foreach ($w in (Claude-Windows)) {
        $blocks = @(); $seen = @{}
        foreach ($b in (Option-Buttons $w)) {
            $top = Block-Of $b; $r = Rid $top
            if ($seen.ContainsKey($r)) { continue }; $seen[$r] = 1
            $blocks += , @{ rid = $r; nodes = (Snapshot-Block $top) }
            if ($blocks.Count -ge 8) { break }
        }
        $wins += @{ hwnd = $w.Current.NativeWindowHandle; pid = $w.Current.ProcessId; title = $w.Current.Name; blocks = $blocks }
    }
    Out-Json @{ ok = $true; action = 'snapshot'; host = $env:COMPUTERNAME; read_at = (Get-Date).ToString('o'); windows = $wins }
    exit 0
}
if ($req.action -eq 'press') {
    $label = ("" + $req.label).Trim(); $title = ("" + $req.title).Trim()
    if (-not ($label -match $OptionRe)) { Out-Json @{ ok = $false; pressed = $false; reason = "refused: '$label' is not an Allow / Deny label" }; exit 2 }
    if (-not $title) { Out-Json @{ ok = $false; pressed = $false; reason = 'refused: no prompt title to check' }; exit 2 }
    $w = $null
    foreach ($x in (Claude-Windows)) { if ($x.Current.NativeWindowHandle -eq [int]$req.hwnd) { $w = $x } }
    if ($w -eq $null) { Out-Json @{ ok = $false; pressed = $false; reason = 'refused: that window is gone or is not a Claude window' }; exit 3 }
    $btn = $null
    foreach ($b in (Option-Buttons $w)) { if (("" + $b.Current.Name).Trim() -eq $label -and (Rid $b) -eq $req.rid) { $btn = $b } }
    if ($btn -eq $null) { Out-Json @{ ok = $false; pressed = $false; reason = "refused: the '$label' button is not showing any more" }; exit 3 }
    $text = Block-Text (Block-Of $btn)
    if ($text.IndexOf($title, [StringComparison]::OrdinalIgnoreCase) -lt 0) { Out-Json @{ ok = $false; pressed = $false; reason = 'refused: the prompt around that button changed' }; exit 3 }
    $btn.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
    Start-Sleep -Milliseconds 1200
    $still = $false; foreach ($b in (Option-Buttons $w)) { if ((Rid $b) -eq $req.rid) { $still = $true } }
    Out-Json @{ ok = $true; pressed = $true; label = $label; prompt_gone = (-not $still); pressed_at = (Get-Date).ToString('o') }
    exit 0
}
Out-Json @{ ok = $false; reason = "unknown action '$($req.action)' (snapshot | press)" }
exit 2
