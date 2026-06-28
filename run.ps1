#!/usr/bin/env pwsh
# EVA sandbox wrapper - runs the organism inside the hardened Docker container.
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Command = "help",

    [Parameter(Position = 1, ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

foreach ($d in "data/runtime", "data/state", "data/workspace") {
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

switch ($Command) {
    "build" { docker compose build }
    "status" { Invoke-Eva @("status") }
    "rollback" { Invoke-Eva @("rollback") }
    "work" { Invoke-Eva (@("work") + $Rest) }
    "improve" { Invoke-Eva (@("improve") + $Rest) }
    "review" { Invoke-Eva (@("review") + $Rest) }
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
  evolve  [N] [flags]   Run N autonomous evolution rounds
  rollback              Roll back to the last good release
  shell                 Open a shell inside the container (debug)

Autonomous (no per-step approval; safe because Docker contains it):
  .\run.ps1 evolve 3 --yes --allow-shell

Everything the organism evolves is on the host under .\data\
"@ | Write-Host
    }
}
