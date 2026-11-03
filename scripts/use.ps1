# Daily-use launcher -- for when you just want JARVIS running, not troubleshooting.
# Equivalent to: .\scripts\run.ps1 -Prod -Quick
# No visible backend/frontend windows -- output goes to logs\backend.log and
# logs\frontend.log instead. Alt+J opens the app window; Ctrl+C in THIS
# terminal is still what stops everything (same graceful shutdown as run.ps1:
# memory flush, sleep marker, Qdrant left running for next time).
#
# For dev/troubleshooting (visible windows, hot reload, etc.),
# use .\scripts\run.ps1 directly instead.
param(
    [switch]$NoWake            # keyboard/typed input only, no microphone
)
$ErrorActionPreference = "Continue"

# Hashtable splatting, not array splatting -- an array of raw strings like
# @("-Prod", "-Quick") does NOT reliably bind switch parameters when splatted
# (confirmed empirically: -Prod/-Quick silently came out $false). A hashtable
# maps each key straight to its named parameter with no token-order ambiguity.
$runArgs = @{
    Prod  = $true
    Quick = $true
}
if ($NoWake) { $runArgs["NoWake"] = $true }

& (Join-Path $PSScriptRoot "run.ps1") @runArgs
