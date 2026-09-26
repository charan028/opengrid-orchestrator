<#
.SYNOPSIS
  Sync this repository to the base server into an isolated workspace and run a command there.

.DESCRIPTION
  This PC has no Python, so all code runs on 192.168.5.35 (host 'basepower').
  The repo is packed with tar, copied with scp, and extracted into /opt/opengrid/work/<Ws> as user opengrid.
  The command runs as user opengrid in that directory, with:
    OG_WS=<Ws>  OG_DB=og_t_<Ws>  OG_MQTT_ROOT=ogtest/<Ws>
    /etc/opengrid/secrets.env and /etc/opengrid/api_keys.env loaded WITHOUT any OG_MQTT_* credential, and the
    workspace's own MQTT user from /opt/opengrid/work/<Ws>/.mqtt.env (tools/ws_env.sh; provisioned by
    deploy/mosquitto/provision_ws_users.py). Production MQTT credentials are never exported into a workspace.
    PATH containing /opt/opengrid/venv/bin (orchestrator) — use /opt/ogsim/venv/bin/python for integration-sims.
  The command is sent as a script file (never inline) to avoid PowerShell 5.1 quoting problems.

.EXAMPLE
  powershell -File tools\remote.ps1 -Ws feeds -Cmd "cd orchestrator && python -m pytest tests/unit/feeds -q"
.EXAMPLE
  powershell -File tools\remote.ps1 -Ws sims -Cmd "cd integration-sims && /opt/ogsim/venv/bin/python -m pytest -q" -NoSync
#>
param(
  [Parameter(Mandatory = $true)][ValidatePattern('^[a-z0-9_]+$')][string]$Ws,
  [Parameter(Mandatory = $true)][string]$Cmd,
  [switch]$NoSync,
  [int]$TimeoutSec = 900
)
$ErrorActionPreference = 'Stop'
$Key = Join-Path $env:USERPROFILE '.ssh\base'
$HostSpec = 'root@192.168.5.35'
$Repo = Split-Path -Parent $PSScriptRoot
$Tmp = Join-Path $env:TEMP ("og_" + $Ws + "_" + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Force $Tmp | Out-Null
$SshOpts = @('-i', $Key, '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15')

try {
  if (-not $NoSync) {
    $Tgz = Join-Path $Tmp 'repo.tgz'
    & tar.exe -czf $Tgz -C $Repo --exclude=.git --exclude=__pycache__ --exclude=.pytest_cache --exclude=.ruff_cache --exclude=*.pyc .
    if ($LASTEXITCODE -ne 0) { throw "tar failed" }
    & scp.exe -q @SshOpts $Tgz "${HostSpec}:/tmp/og_$Ws.tgz"
    if ($LASTEXITCODE -ne 0) { throw "scp failed" }
  }

  $body = @"
set -eo pipefail
WS=$Ws
DIR=/opt/opengrid/work/`$WS
if [ "$([int](-not $NoSync))" = "1" ]; then
  rm -rf "`$DIR.new" && install -d -o opengrid -g opengrid "`$DIR.new"
  tar -xzf /tmp/og_`$WS.tgz -C "`$DIR.new" && rm -f /tmp/og_`$WS.tgz
  rm -f "`$DIR.new/.mqtt.env"
  chown -R opengrid:opengrid "`$DIR.new"
  # The workspace MQTT credentials live outside the synced tree's content: carry them across the swap.
  if [ -f "`$DIR/.mqtt.env" ]; then cp -p "`$DIR/.mqtt.env" "`$DIR.new/.mqtt.env"; fi
  rm -rf "`$DIR" && mv "`$DIR.new" "`$DIR"
fi
cat > /tmp/og_cmd_`$WS.sh <<'OGCMD'
. /opt/opengrid/work/$Ws/tools/ws_env.sh
og_ws_env $Ws
export PYTHONPATH=/opt/opengrid/work/$Ws/orchestrator/src
export OG_CONFIG=/opt/opengrid/work/$Ws/orchestrator/config/test.toml
export PATH=/opt/opengrid/venv/bin:`$PATH
cd /opt/opengrid/work/$Ws
$Cmd
OGCMD
chmod 644 /tmp/og_cmd_`$WS.sh
timeout $TimeoutSec runuser -u opengrid -- bash /tmp/og_cmd_`$WS.sh
"@
  $Script = Join-Path $Tmp 'run.sh'
  [IO.File]::WriteAllText($Script, ($body -replace "`r", ""), (New-Object Text.UTF8Encoding $false))
  & scp.exe -q @SshOpts $Script "${HostSpec}:/tmp/og_run_$Ws.sh"
  if ($LASTEXITCODE -ne 0) { throw "scp of run script failed" }
  & ssh.exe @SshOpts $HostSpec "bash /tmp/og_run_$Ws.sh; rc=`$?; rm -f /tmp/og_run_$Ws.sh /tmp/og_cmd_$Ws.sh; exit `$rc"
  exit $LASTEXITCODE
}
finally {
  Remove-Item -Recurse -Force $Tmp -ErrorAction SilentlyContinue
}
