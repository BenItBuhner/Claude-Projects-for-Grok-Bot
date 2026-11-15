@echo off
rem Run ccp inside the WSL distro that hosts the plugin on this PC:  hosts\windows\ccp.cmd setup status
if "%CCP_WSL_DISTRO%"=="" set CCP_WSL_DISTRO=ClaudeProjects
wsl.exe -d %CCP_WSL_DISTRO% -u ccp --cd ~ -- ./claude-projects-mcp/ccp %*
