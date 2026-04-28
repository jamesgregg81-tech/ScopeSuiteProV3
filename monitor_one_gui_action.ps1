$ErrorActionPreference = 'Stop'
$root = Join-Path $env:USERPROFILE 'Desktop\FlukeScopeSuite_Captures'
$watchPattern = '(?i)Opening serial port|Port opened|TX:|ACK:|RX|ID|QP|GR|GL|PC |PNG|byte count|Raw screen saved|PNG saved|Capture PASS|Screen Capture PASS|Releasing|Serial port closed|Port closed|cleanup|release|timeout|ERROR|failed|rejected|Instrument|Meter identified|Active transfer baud|Connection test passed|New session folder|Capture command|Binary transfer|saved|verified'
$errorPattern = '(?i)ERROR|Timeout|timed out|failed|rejected|METER LOCKED|Capture Error|Traceback|Exception|Release failed'
function LatestLog {
    Get-ChildItem $root -Recurse -Filter session_log.txt -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
}
$latest = LatestLog
$currentPath = $null
$lineIndex = 0
if ($latest) {
    $currentPath = $latest.FullName
    $existing = Get-Content -Path $currentPath -ErrorAction SilentlyContinue
    $lineIndex = $existing.Count
}
$deadline = (Get-Date).AddMinutes(30)
$collected = New-Object System.Collections.Generic.List[string]
$lastNew = $null
while ((Get-Date) -lt $deadline) {
    $latest = LatestLog
    if ($latest) {
        if ($currentPath -ne $latest.FullName) {
            $currentPath = $latest.FullName
            $lineIndex = 0
            $collected.Add("--- New log file: $currentPath ---")
        }
        $lines = Get-Content -Path $currentPath -ErrorAction SilentlyContinue
        if ($lines.Count -gt $lineIndex) {
            for ($i = $lineIndex; $i -lt $lines.Count; $i++) {
                $line = $lines[$i]
                if ($line -match $watchPattern -or $line -match $errorPattern) {
                    $collected.Add($line)
                }
            }
            $lineIndex = $lines.Count
            $lastNew = Get-Date
        }
        if ($lastNew -and ((Get-Date) - $lastNew).TotalSeconds -ge 4) {
            if ($collected.Count -gt 0) {
                $collected | ForEach-Object { Write-Output $_ }
            } else {
                Write-Output "(new log entries detected, but none matched protocol watch patterns)"
            }
            exit 0
        }
    }
    Start-Sleep -Milliseconds 500
}
exit 124
