# dev/scripts/gen-keys.ps1 -- Windows equivalent of gen-keys.sh. Generates the dev
# guardian/safestop Ed25519 keypairs into dev/keys/ (gitignored) using the orchestrator's own
# keygen CLIs, so the dev stack's key format is guaranteed identical to production's.
#
# Idempotent-ish: skips a keypair that already exists. Pass -Force to regenerate both anyway.
param(
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$KeysDir = Join-Path $RepoRoot "dev\keys"
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { $Python = "python" }

$env:PYTHONPATH = Join-Path $RepoRoot "orchestrator\src"

New-Item -ItemType Directory -Force -Path $KeysDir | Out-Null

$guardianKey = Join-Path $KeysDir "guardian-dev.key"
if ($Force -or -not (Test-Path $guardianKey)) {
    & $Python -m opengrid.guardian keygen --out $KeysDir --key-id guardian-dev
    if ($LASTEXITCODE -ne 0) { throw "guardian keygen failed" }
} else {
    Write-Host "gen-keys.ps1: $guardianKey already exists, skipping (use -Force to regenerate)"
}

$safestopKey = Join-Path $KeysDir "safestop-dev.key"
$safestopPub = Join-Path $KeysDir "safestop-dev.pub"
if ($Force -or -not (Test-Path $safestopKey)) {
    & $Python -m opengrid.safestop.keys keygen `
        --key-id safestop-dev `
        --key-out $safestopKey `
        --pubkey-out $safestopPub
    if ($LASTEXITCODE -ne 0) { throw "safestop keygen failed" }
} else {
    Write-Host "gen-keys.ps1: $safestopKey already exists, skipping (use -Force to regenerate)"
}

Write-Host "gen-keys.ps1: dev/keys/ ready:"
Get-ChildItem $KeysDir
