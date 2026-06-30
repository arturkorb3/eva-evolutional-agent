#!/usr/bin/env pwsh
# EVA sandbox wrapper - runs the organism inside the hardened Docker container.
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Command = "work",

    [Parameter(Position = 1, ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

foreach ($d in "data/runtime", "data/state", "data/workspace", "data/local") {
    if (-not (Test-Path -LiteralPath $d)) {
        New-Item -ItemType Directory -Path $d -Force | Out-Null
    }
}

if (($Command -notin @("build", "help")) -and (-not (Test-Path -LiteralPath ".env"))) {
    Write-Warning "No .env file found. Copy .env.example to .env and set your LLM credentials."
}

function Invoke-Eva {
    param([string[]]$EvaArgs)
    docker compose run --rm eva @EvaArgs
}

function Start-ClipboardWatcher {
    # Seamless paste: while a session runs, watch the host clipboard and auto-stage
    # any NEW screenshot into data/workspace/ as clip-<timestamp>.png. Inside the
    # chat, `/paste` then attaches the most recent one. The container never touches
    # the clipboard - only this host helper does. Disable with $env:EVA_NO_CLIP_WATCH=1.
    if ($env:EVA_NO_CLIP_WATCH -eq "1") { return $null }
    $env:EVA_CLIP_DIR = (Resolve-Path "data/workspace").Path
    $loop = @'
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
$dir = $env:EVA_CLIP_DIR
$last = ''
while ($true) {
    try {
        if ([System.Windows.Forms.Clipboard]::ContainsImage()) {
            $img = [System.Windows.Forms.Clipboard]::GetImage()
            if ($null -ne $img) {
                $ms = New-Object System.IO.MemoryStream
                $img.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
                $bytes = $ms.ToArray(); $ms.Dispose()
                $h = [BitConverter]::ToString([System.Security.Cryptography.SHA256]::Create().ComputeHash($bytes))
                if ($h -ne $last) {
                    $last = $h
                    $name = 'clip-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff') + '.png'
                    [System.IO.File]::WriteAllBytes((Join-Path $dir $name), $bytes)
                }
            }
        }
    } catch {}
    Start-Sleep -Milliseconds 800
}
'@
    $enc = [Convert]::ToBase64String([System.Text.Encoding]::Unicode.GetBytes($loop))
    $p = Start-Process -FilePath "powershell.exe" -WindowStyle Hidden -PassThru `
        -ArgumentList @("-NoProfile", "-STA", "-WindowStyle", "Hidden", "-EncodedCommand", $enc)
    Write-Host "(clipboard watch on: screenshots auto-stage to data/workspace; type /paste in chat to attach the latest)"
    return $p
}

function Stop-ClipboardWatcher {
    param($Proc)
    if ($Proc -and -not $Proc.HasExited) { try { $Proc.Kill() } catch {} }
    Remove-Item Env:EVA_CLIP_DIR -ErrorAction SilentlyContinue
}

switch ($Command) {
    "build" { docker compose build }
    "status" { Invoke-Eva @("status") }
    "rollback" { Invoke-Eva @("rollback") }
    "reseed" {
        # Drop the materialized runtime so the next start re-seeds v001 from
        # seed/ (mounted), without an image rebuild. State/workspace are kept.
        if (Test-Path -LiteralPath "data/runtime") {
            Remove-Item -Recurse -Force -LiteralPath "data/runtime"
        }
        Invoke-Eva @("status")
    }
    "work" {
        $w = Start-ClipboardWatcher
        try { Invoke-Eva (@("work") + $Rest) } finally { Stop-ClipboardWatcher $w }
    }
    "improve" {
        $w = Start-ClipboardWatcher
        try { Invoke-Eva (@("improve") + $Rest) } finally { Stop-ClipboardWatcher $w }
    }
    "review" { Invoke-Eva (@("review") + $Rest) }
    "paste" {
        # Bridge the host clipboard into the sandbox: save a clipboard screenshot
        # into data/workspace/ so you can reference it in a chat message. The
        # container cannot read the host clipboard and terminals accept only text,
        # so this host-side step stages the image as a file. Clipboard image access
        # needs an STA thread + several formats, so we shell out to powershell.exe -STA.
        New-Item -ItemType Directory -Path "data/workspace" -Force | Out-Null
        $name = "clip-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".png"
        $env:EVA_PASTE_OUT = Join-Path (Resolve-Path "data/workspace").Path $name
        $inner = @'
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
$out = $env:EVA_PASTE_OUT
$img = $null
if ([System.Windows.Forms.Clipboard]::ContainsImage()) { $img = [System.Windows.Forms.Clipboard]::GetImage() }
if ($null -eq $img) {
    $ido = [System.Windows.Forms.Clipboard]::GetDataObject()
    foreach ($fmt in @('PNG', 'image/png')) {
        if ($ido -and $ido.GetDataPresent($fmt)) {
            $s = $ido.GetData($fmt)
            if ($s -is [System.IO.Stream]) { $img = [System.Drawing.Image]::FromStream($s); break }
        }
    }
}
if ($null -eq $img -and [System.Windows.Forms.Clipboard]::ContainsFileDropList()) {
    foreach ($f in [System.Windows.Forms.Clipboard]::GetFileDropList()) {
        if ($f -match '\.(png|jpg|jpeg|gif|webp|bmp)$') { $img = [System.Drawing.Image]::FromFile($f); break }
    }
}
if ($null -eq $img) { Write-Output 'NOIMAGE'; exit 1 }
$img.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
Write-Output 'OK'
'@
        $enc = [Convert]::ToBase64String([System.Text.Encoding]::Unicode.GetBytes($inner))
        $res = & powershell.exe -NoProfile -STA -EncodedCommand $enc 2>$null
        Remove-Item Env:EVA_PASTE_OUT -ErrorAction SilentlyContinue
        if ($res -match 'OK') {
            Write-Host "Saved clipboard image -> data/workspace/$name"
            Write-Host ("Reference it in your next message, e.g.:  ![]({0})   or simply:  {0}" -f $name)
        }
        else {
            Write-Warning "No usable image found in the clipboard. Take a screenshot (Win+Shift+S), then run: .\run.ps1 paste"
        }
    }
    "evolve" {
        $rounds = "1"
        $extra = @($Rest)
        if ($extra.Count -ge 1 -and $extra[0] -match '^\d+$') {
            $rounds = $extra[0]
            $extra = @($extra | Select-Object -Skip 1)
        }
        Invoke-Eva (@("evolve", "--rounds", $rounds) + $extra)
    }
    "shell" { docker compose run --rm --entrypoint /bin/sh eva }
    default {
        @"
EVA - Evolutional Agent (Docker sandbox)

Usage: .\run.ps1 <command> [args]

  build                 Build (or rebuild) the sandbox image
  status                Show current / last-good release
  work    [task]        Useful work in workspace/ (default mode)
  improve [task]        Evolve a candidate release
  review  [task]        Read-only inspection
  paste                 Save a clipboard screenshot into data/workspace/ to attach in chat
  evolve  [N] [flags]   Run N autonomous evolution rounds
  rollback              Roll back to the last good release
  reseed                Re-seed v001 from seed/ (after editing the genome; no rebuild)
  shell                 Open a shell inside the container (debug)

Autonomous (no per-step approval; safe because Docker contains it):
  .\run.ps1 evolve 3 --yes --allow-shell

Everything the organism evolves is on the host under .\data\
"@ | Write-Host
    }
}
