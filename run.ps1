#!/usr/bin/env pwsh
# EVA sandbox wrapper - runs the organism inside the hardened Docker container.
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Command = "work",

    [Parameter(Position = 1, ValueFromRemainingArguments = $true)]
    [string[]]$Rest,

    # Run EVA in the FREE sandbox (writable rootfs + root + apt) instead of the default
    # hardened SAFE sandbox. The user decides the containment level.
    [switch]$Free
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

foreach ($d in "data/runtime", "data/state", "data/workspace", "data/local") {
    if (-not (Test-Path -LiteralPath $d)) {
        New-Item -ItemType Directory -Path $d -Force | Out-Null
    }
}

# Sandbox selection: SAFE (default) or FREE (-Free) - the user decides the containment
# level. FREE layers docker-compose.free.yml (writable rootfs + root + apt) on top.
$script:ComposeFiles = @("-f", "docker-compose.yml")
if ($Free) {
    $script:ComposeFiles += @("-f", "docker-compose.free.yml")
    Write-Host ""
    Write-Host "  !! EVA FREE sandbox: writable rootfs + root + apt inside the container." -ForegroundColor Yellow
    Write-Host "     Bigger blast radius (still contained to the container + ./data; ephemeral)." -ForegroundColor Yellow
    Write-Host "     Omit -Free for the default hardened SAFE sandbox." -ForegroundColor Yellow
    Write-Host ""
}

# First-run onboarding (interactive .env setup) runs just before dispatch, below.

function Invoke-Eva {
    param([string[]]$EvaArgs)
    $files = $script:ComposeFiles
    docker compose @files run --rm eva @EvaArgs
}

function Repair-DataPermissions {
    # A FREE (root) session can leave root-owned files in ./data that the default non-root
    # SAFE container then cannot write (seen as PermissionError on state/backlog.jsonl).
    # Reset ownership to the sandbox uid via a one-shot root (free) container.
    docker compose -f docker-compose.yml -f docker-compose.free.yml run --rm `
        --entrypoint chown eva -R 10001:10001 /eva/runtime /eva/state /eva/workspace /eva/.local | Out-Null
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

# --------------------------------------------------------------------------- #
# First-run onboarding: interactively create or complete .env (provider + creds).
# EVA reads its config from .env (via docker-compose substitution), so this runs
# host-side, BEFORE the container starts. API keys are entered masked and written
# to the gitignored .env in plain text (standard for .env). Skip with EVA_NO_SETUP=1.
# --------------------------------------------------------------------------- #
function Read-EnvMap {
    param([string]$Path)
    $map = @{}
    if (Test-Path -LiteralPath $Path) {
        foreach ($line in Get-Content -LiteralPath $Path) {
            $t = $line.Trim()
            if ($t -eq "" -or $t.StartsWith("#")) { continue }
            $i = $t.IndexOf("=")
            if ($i -lt 1) { continue }
            $map[$t.Substring(0, $i).Trim()] = $t.Substring($i + 1).Trim()
        }
    }
    return $map
}

function Save-EnvLines {
    param([string[]]$Lines)
    $enc = New-Object System.Text.UTF8Encoding($false)   # no BOM (docker compose-safe)
    [System.IO.File]::WriteAllLines($script:EnvFull, [string[]]$Lines, $enc)
}

function Set-EnvVar {
    param([string]$Name, [string]$Value)
    $lines = @()
    if (Test-Path -LiteralPath $script:EnvFull) { $lines = @(Get-Content -LiteralPath $script:EnvFull) }
    $out = @()
    $found = $false
    foreach ($l in $lines) {
        if ($l -match "^\s*$([regex]::Escape($Name))\s*=") { $out += "$Name=$Value"; $found = $true }
        else { $out += $l }
    }
    if (-not $found) { $out += "$Name=$Value" }
    Save-EnvLines $out
}

function Test-Placeholder {
    param([string]$Value)
    return [string]::IsNullOrWhiteSpace($Value) -or ($Value -match "replace-me|^sk-replace|^sk-ant-replace")
}

function Read-Secret {
    param([string]$Prompt)
    $sec = Read-Host -Prompt $Prompt -AsSecureString
    $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
    try { return [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

function Show-EnvTip {
    Write-Host "Tip: .env.example documents every option (model timeout, context budget," -ForegroundColor DarkGray
    Write-Host "     autonomous mode, streaming, prompt caching, TUI...). Edit .env any time," -ForegroundColor DarkGray
    Write-Host "     or copy .env.example over it for the fully-commented template." -ForegroundColor DarkGray
}

function Invoke-EvaOnboarding {
    if ($env:EVA_NO_SETUP -eq "1") { return }
    $fresh = -not (Test-Path -LiteralPath $script:EnvFull)
    $did = $fresh

    if ($fresh) {
        Write-Host ""
        Write-Host "I don't see a .env config yet." -ForegroundColor Yellow
        $ans = Read-Host "Shall I set up EVA with you now? [Y/n]"
        if ($ans -match '^\s*(n|no)\s*$') {
            Write-Warning "Skipping setup - EVA may not reach a model without credentials."
            return
        }
        Save-EnvLines @("# EVA configuration (created by run.ps1 setup)")
    }

    $map = Read-EnvMap $script:EnvFull
    $provider = $map["EVA_PROVIDER"]

    if ($fresh -or [string]::IsNullOrWhiteSpace($provider)) {
        Write-Host ""
        Write-Host "Which model provider should EVA use?"
        Write-Host "  [1] OpenAI-compatible  (OpenAI / Azure / Ollama / LM Studio / vLLM / OpenRouter)"
        Write-Host "  [2] Anthropic Claude   (Messages API: native tools + prompt caching)"
        Write-Host "  [3] Offline 'fake'     (no API key; smoke tests / dry runs)"
        $choice = (Read-Host "Choose [1/2/3] (default 1)").Trim()
        switch ($choice) {
            "2" { $provider = "anthropic" }
            "3" { $provider = "fake" }
            default { $provider = "openai_chat" }
        }
        Set-EnvVar "EVA_PROVIDER" $provider
        $map = Read-EnvMap $script:EnvFull
        $did = $true
    }

    if ($provider -eq "fake") {
        if ($did) { Write-Host "Provider 'fake' needs no credentials - you're set." -ForegroundColor Green; Show-EnvTip; Write-Host "" }
        return
    }

    if ($provider -eq "anthropic") {
        if ((Test-Placeholder $map["EVA_MODEL"]) -and (Test-Placeholder $map["LLM_MODEL"])) {
            Write-Host ""
            Write-Host "Which Claude model?"
            Write-Host "  [1] claude-opus-4-8    (frontier; long-running agents & coding)"
            Write-Host "  [2] claude-opus-4-6    (frontier; long-running agents & coding)"
            Write-Host "  [3] claude-sonnet-4-6  (best speed / intelligence balance)"
            Write-Host "  [4] claude-haiku-4-5   (fastest, near-frontier)"
            Write-Host "  [5] other              (type a custom model id)"
            $mc = (Read-Host "Choose [1-5] (default 3)").Trim()
            switch ($mc) {
                "1" { $m = "claude-opus-4-8" }
                "2" { $m = "claude-opus-4-6" }
                "4" { $m = "claude-haiku-4-5" }
                "5" { $m = (Read-Host "Model id").Trim() }
                default { $m = "claude-sonnet-4-6" }
            }
            if (-not [string]::IsNullOrWhiteSpace($m)) { Set-EnvVar "EVA_MODEL" $m; $did = $true }
        }
        if ((Test-Placeholder $map["EVA_API_KEY"]) -and (Test-Placeholder $map["ANTHROPIC_API_KEY"])) {
            $k = Read-Secret "Anthropic API key (sk-ant-...) [input hidden]"
            if (-not [string]::IsNullOrWhiteSpace($k)) { Set-EnvVar "EVA_API_KEY" $k.Trim(); $did = $true }
        }
    }
    elseif ($provider -eq "openai_chat") {
        if ((Test-Placeholder $map["EVA_ENDPOINT"]) -and (Test-Placeholder $map["LLM_ENDPOINT"])) {
            $e = Read-Host "Chat Completions endpoint (Enter for https://api.openai.com/v1/chat/completions)"
            if ([string]::IsNullOrWhiteSpace($e)) { $e = "https://api.openai.com/v1/chat/completions" }
            Set-EnvVar "EVA_ENDPOINT" $e.Trim(); $did = $true
        }
        if ((Test-Placeholder $map["EVA_MODEL"]) -and (Test-Placeholder $map["LLM_MODEL"])) {
            $m = Read-Host "Model name (e.g. gpt-5.5, llama3.1)"
            if (-not [string]::IsNullOrWhiteSpace($m)) { Set-EnvVar "EVA_MODEL" $m.Trim(); $did = $true }
        }
        if ((Test-Placeholder $map["EVA_API_KEY"]) -and (Test-Placeholder $map["LLM_API_KEY"]) -and (Test-Placeholder $map["OPENAI_API_KEY"])) {
            $k = Read-Secret "API key [input hidden] (type 'ollama' for a local server that ignores it)"
            if (-not [string]::IsNullOrWhiteSpace($k)) { Set-EnvVar "EVA_API_KEY" $k.Trim(); $did = $true }
        }
    }

    if ($did) { Write-Host "Saved to .env - starting EVA..." -ForegroundColor Green; Show-EnvTip; Write-Host "" }
}

$script:EnvFull = Join-Path $PSScriptRoot ".env"
if ($Command -in @("work", "improve", "review", "evolve")) {
    Invoke-EvaOnboarding
}

switch ($Command) {
    "build" { docker compose build }
    "install" {
        # One-shot setup: put `eva` on PATH (via bin/) + enable tab-completion, and
        # optionally build the image. All reversible via `uninstall`.
        $dir = $PSScriptRoot
        $bin = Join-Path $dir "bin"
        $comp = Join-Path $dir "completions\eva.ps1"
        $begin = "# >>> eva tab-completion >>>"
        $end = "# <<< eva tab-completion <<<"

        # 1) PATH -> bin/ only (keeps run.ps1 / Dockerfile / compose off your PATH).
        $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
        $parts = @($userPath -split ';' | Where-Object { $_ -ne '' })
        if ($parts -notcontains $bin) {
            [Environment]::SetEnvironmentVariable("Path", ((@($parts) + $bin) -join ';'), "User")
            Write-Host "PATH        : added $bin" -ForegroundColor Green
        }
        else {
            Write-Host "PATH        : already present" -ForegroundColor DarkGray
        }
        if (($env:Path -split ';') -notcontains $bin) { $env:Path = "$env:Path;$bin" }

        # 2) Tab-completion: refresh a marked block in the profile.
        $profilePath = $PROFILE.CurrentUserAllHosts
        $pdir = Split-Path -Parent $profilePath
        if (-not (Test-Path -LiteralPath $pdir)) { New-Item -ItemType Directory -Path $pdir -Force | Out-Null }
        $kept = New-Object System.Collections.Generic.List[string]
        $skip = $false
        if (Test-Path -LiteralPath $profilePath) {
            foreach ($l in @(Get-Content -LiteralPath $profilePath)) {
                if ($l -eq $begin) { $skip = $true; continue }
                if ($l -eq $end) { $skip = $false; continue }
                if (-not $skip) { $kept.Add($l) }
            }
        }
        $kept.Add($begin); $kept.Add(". `"$comp`""); $kept.Add($end)
        Set-Content -LiteralPath $profilePath -Value $kept -Encoding UTF8
        Write-Host "Completion  : wired into $profilePath" -ForegroundColor Green
        . $comp   # load completion into THIS session too

        # 3) Offer to build the sandbox image if it isn't there yet.
        $haveImage = $false
        try { docker image inspect eva:latest *> $null; if ($LASTEXITCODE -eq 0) { $haveImage = $true } } catch {}
        Write-Host ""
        if ($haveImage) {
            Write-Host "Image       : eva:latest present" -ForegroundColor DarkGray
        }
        elseif ($env:EVA_NO_SETUP -ne "1") {
            $ans = Read-Host "Build the sandbox image now? (needs Docker running) [Y/n]"
            if ($ans -notmatch '^\s*(n|no)\s*$') { docker compose build }
            else { Write-Host "Skipped - build later with:  eva build" }
        }
        Write-Host ""
        Write-Host "Done. Open a NEW terminal (or run: . `$PROFILE) and try:  eva <Tab>" -ForegroundColor Green
        Write-Host "Then just:  eva"
    }
    "uninstall" {
        $dir = $PSScriptRoot
        $bin = Join-Path $dir "bin"
        $begin = "# >>> eva tab-completion >>>"
        $end = "# <<< eva tab-completion <<<"
        # Drop bin/ (and, for anyone upgrading, an older whole-repo entry) from PATH.
        $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
        $parts = @($userPath -split ';' | Where-Object { $_ -ne '' -and $_ -ne $bin -and $_ -ne $dir })
        [Environment]::SetEnvironmentVariable("Path", ($parts -join ';'), "User")
        $profilePath = $PROFILE.CurrentUserAllHosts
        if (Test-Path -LiteralPath $profilePath) {
            $kept = New-Object System.Collections.Generic.List[string]
            $skip = $false
            foreach ($l in @(Get-Content -LiteralPath $profilePath)) {
                if ($l -eq $begin) { $skip = $true; continue }
                if ($l -eq $end) { $skip = $false; continue }
                if (-not $skip) { $kept.Add($l) }
            }
            Set-Content -LiteralPath $profilePath -Value $kept -Encoding UTF8
        }
        Write-Host "Removed eva from PATH and tab-completion from the profile (open a new terminal)."
    }
    "status" { Invoke-Eva @("status") }
    "rollback" { Invoke-Eva (@("rollback") + $Rest) }
    "unlock" { Invoke-Eva @("unlock") }
    "fix-perms" {
        Write-Host "Resetting ./data ownership to the sandbox user (10001) - undoes a prior free-mode run..."
        Repair-DataPermissions
        Write-Host "Done."
    }
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
    "shell" { $files = $script:ComposeFiles; docker compose @files run --rm --entrypoint /bin/sh eva }
    default {
        @"
EVA - Evolvable Virtual Agent (Docker sandbox)

Usage: .\run.ps1 <command> [args]   (or, after `install`, simply:  eva <command> [args])

  build                 Build (or rebuild) the sandbox image
  install               Set up the `eva` command: PATH + tab-completion (+ optional build)
  uninstall             Undo `install` (remove from PATH + tab-completion)
  status                Show current / last-good release
  work    [task]        Useful work in workspace/ (default mode)
  improve [task]        Evolve a candidate release
  review  [task]        Read-only inspection
  paste                 Save a clipboard screenshot into data/workspace/ to attach in chat
  evolve  [N] [flags]   Run N autonomous evolution rounds
  rollback [--force]    Roll back to the last good release
  unlock                Clear a dead evolution lock (single-writer guard)
  reseed                Re-seed v001 from seed/ (after editing the genome; no rebuild)
  shell                 Open a shell inside the container (debug)
  fix-perms             Reset ./data ownership to the sandbox user (after a -Free run)

Sandbox mode:
  -Free <command>       Run in the FREE sandbox (writable rootfs + root + apt) instead of
                        the default hardened SAFE sandbox. Lets EVA install system packages
                        (e.g. browser libs). Bigger blast radius - use only when needed.
                        e.g.  .\run.ps1 -Free work

Autonomous (no per-step approval; safe because Docker contains it):
  .\run.ps1 evolve 3 --yes --allow-shell

Everything the organism evolves is on the host under .\data\
"@ | Write-Host
    }
}

# A FREE (root) session can leave root-owned files in ./data that break the next SAFE run;
# reset ownership afterwards so safe mode keeps working.
if ($Free -and $Command -in @("work", "improve", "review", "evolve", "shell")) {
    Repair-DataPermissions
}
