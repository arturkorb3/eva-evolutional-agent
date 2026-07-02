@echo off
rem Thin shim so you can type `eva <command>` instead of `.\run.ps1 <command>`.
rem Forwards every argument to run.ps1 in the repo root (one level up). Prefers
rem PowerShell 7 (pwsh) and falls back to Windows PowerShell. This folder is put
rem on PATH by `.\run.ps1 install` (or `eva install`).
setlocal
where pwsh >nul 2>nul
if %ERRORLEVEL%==0 (
  pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\run.ps1" %*
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\run.ps1" %*
)
exit /b %ERRORLEVEL%
