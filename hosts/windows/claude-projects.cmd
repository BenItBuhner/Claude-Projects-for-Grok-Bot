@echo off
rem Run claude-projects inside the WSL distro that hosts the plugin on this PC:  hosts\windows\claude-projects.cmd setup status
setlocal
set "DISTRO=%CLAUDE_PROJECTS_WSL_DISTRO%"
if "%DISTRO%"=="" set "DISTRO=ClaudeProjects"
rem --exec: no shell in between, so nothing in the arguments is expanded on the way in
for /f %%U in ('wsl.exe -d %DISTRO% --exec id -un') do set "RUNTIME_USER=%%U"
if "%RUNTIME_USER%"=="" exit /b 1
wsl.exe -d %DISTRO% --exec /home/%RUNTIME_USER%/claude-projects-mcp/claude-projects %*
