$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir

$ServerUrl = if ($env:SERVER_URL) { $env:SERVER_URL } else { "http://127.0.0.1:18766" }
$RoomId = if ($env:ROOM_ID) { $env:ROOM_ID } else { "doubao" }
$Trigger = if ($env:TRIGGER) { $env:TRIGGER } else { "archive" }
$Transport = if ($env:TRANSPORT) { $env:TRANSPORT } else { "stream" }
$RequestTimeoutSeconds = if ($env:REQUEST_TIMEOUT_SECONDS) { $env:REQUEST_TIMEOUT_SECONDS } else { "12" }
$StreamMaxTimeSeconds = if ($env:STREAM_MAX_TIME_SECONDS) { $env:STREAM_MAX_TIME_SECONDS } else { "90" }
$IntervalSeconds = if ($env:INTERVAL_SECONDS) { $env:INTERVAL_SECONDS } else { "0.25" }
$CurlResolve = if ($env:CURL_RESOLVE) { $env:CURL_RESOLVE } else { "" }
$Python = if ($env:PYTHON) { $env:PYTHON } else { "python" }

if ([bool]$env:CF_ACCESS_CLIENT_ID -xor [bool]$env:CF_ACCESS_CLIENT_SECRET) {
    throw "Set both CF_ACCESS_CLIENT_ID and CF_ACCESS_CLIENT_SECRET using a local credential store."
}

Set-Location $ProjectRoot

Write-Host "Starting Windows clipboard helper"
Write-Host "server_url=$ServerUrl"
Write-Host "room_id=$RoomId"
Write-Host "trigger=$Trigger"
Write-Host "request_timeout_seconds=$RequestTimeoutSeconds"
Write-Host "stream_max_time_seconds=$StreamMaxTimeSeconds"
Write-Host "interval_seconds=$IntervalSeconds"
Write-Host "curl_resolve=$CurlResolve"
Write-Host "transport=$Transport"
Write-Host "mode=clipboard"
if ($env:CF_ACCESS_CLIENT_ID) {
    Write-Host "cloudflare_access=service_token"
}

$HelperArgs = @(
    "scripts/windows_paste_helper.py",
    "--server-url", $ServerUrl,
    "--room-id", $RoomId,
    "--interval-seconds", $IntervalSeconds,
    "--mode", "clipboard",
    "--trigger", $Trigger,
    "--transport", $Transport,
    "--request-timeout-seconds", $RequestTimeoutSeconds,
    "--stream-max-time-seconds", $StreamMaxTimeSeconds
)

if ($CurlResolve) {
    $HelperArgs += @("--curl-resolve", $CurlResolve)
}

& $Python @HelperArgs
exit $LASTEXITCODE
