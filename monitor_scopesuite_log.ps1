$ErrorActionPreference = 'Stop'
$root = Join-Path $env:USERPROFILE 'Desktop\FlukeScopeSuite_Captures'
$deadline = (Get-Date).AddMinutes(15)
$currentPath = $null
$lineIndex = 0
$capturePassCount = 0
$errorPattern = '(?i)ERROR|Timeout|timed out|failed|rejected|METER LOCKED|Capture Error|Traceback|Exception'
$watchPattern = '(?i)Opening serial port|Port opened|TX:|ACK:|QP|PNG|byte count|Raw screen saved|PNG saved|Capture PASS|Releasing|Serial port closed|Port closed|cleanup|release|timeout|ERROR|failed|rejected'
Write-Output "Monitoring ScopeSuite logs under: $root"
Write-Output "Waiting for manual GUI actions. Stop condition: error/timeout/rejection/lock, two capture PASS lines, or 15 minute timeout."
while ((Get-Date) -lt $deadline) {
    $latest = Get-ChildItem $root -Recurse -Filter session_log.txt -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if ($latest) {
        if ($currentPath -ne $latest.FullName) {
            $currentPath = $latest.FullName
            $lineIndex = 0
            Write-Output "--- Monitoring log: $currentPath ---"
        }
        $lines = Get-Content -Path $currentPath -ErrorAction SilentlyContinue
        if ($lines.Count -gt $lineIndex) {
            for ($i = $lineIndex; $i -lt $lines.Count; $i++) {
                $line = $lines[$i]
                if ($line -match $watchPattern) {
                    Write-Output $line
                }
                if ($line -match '(?i)Screen Capture PASS') {
                    $capturePassCount += 1
                }
                if ($line -match $errorPattern) {
                    Write-Output "--- STOP: error condition detected at log line $($i + 1) ---"
                    exit 2
                }
            }
            $lineIndex = $lines.Count
        }
        if ($capturePassCount -ge 2) {
            Write-Output "--- STOP: observed two Screen Capture PASS lines ---"
            exit 0
        }
    }
    Start-Sleep -Seconds 1
}
Write-Output "--- STOP: monitor timeout reached without error condition ---"
exit 0
