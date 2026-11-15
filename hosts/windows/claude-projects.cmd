@echo off
rem Run claude-projects inside the WSL distro that hosts the plugin on this PC:  hosts\windows\claude-projects.cmd setup status
setlocal
set "DISTRO=%CPG_WSL_DISTRO%"
rem CCP_WSL_DISTRO is the old name of CPG_WSL_DISTRO
if "%DISTRO%"=="" set "DISTRO=%CCP_WSL_DISTRO%"
if "%DISTRO%"=="" set "DISTRO=ClaudeProjects"
rem --exec: no shell in between, so nothing in the arguments is expanded on the way in
wsl.exe -d %DISTRO% -u cpg --exec /home/cpg/claude-projects-mcp/claude-projects %*
