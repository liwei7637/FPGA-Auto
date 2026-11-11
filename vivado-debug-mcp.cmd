@echo off
setlocal DisableDelayedExpansion
"%~dp0tools\python.cmd" "%~dp0mcp_server.py" %*
