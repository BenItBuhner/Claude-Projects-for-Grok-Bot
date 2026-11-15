# Parse every PowerShell script in the repo (and run PSScriptAnalyzer when it is installed). Exit 1 on any error.
#   pwsh -NoProfile -File tools/check-ps1.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$bad = 0
foreach ($f in Get-ChildItem -Path $root -Recurse -Filter *.ps1 | Where-Object { $_.FullName -notmatch '[\\/]\.(git|venv)[\\/]' }) {
  $tokens = $null; $errors = $null
  [System.Management.Automation.Language.Parser]::ParseFile($f.FullName, [ref]$tokens, [ref]$errors) | Out-Null
  if ($errors) { $bad++; $errors | ForEach-Object { Write-Host "PARSE $($f.Name):$($_.Extent.StartLineNumber): $($_.Message)" } }
  else { Write-Host "parse ok  $($f.Name)" }
  if (Get-Module -ListAvailable PSScriptAnalyzer) {
    $issues = Invoke-ScriptAnalyzer -Path $f.FullName -Severity Error, Warning -ExcludeRule PSAvoidUsingWriteHost
    foreach ($i in $issues) { Write-Host "PSSA $($i.Severity) $($f.Name):$($i.Line) $($i.RuleName): $($i.Message)" }
    if ($issues | Where-Object Severity -eq 'Error') { $bad++ }
  }
}
exit [int]($bad -gt 0)
