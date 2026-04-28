$ErrorActionPreference = 'Stop'
$root = Join-Path $env:USERPROFILE 'Desktop\FlukeScopeSuite_Captures'
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
                $collected.Add($lines[$i])
            }
            $lineIndex = $lines.Count
            $lastNew = Get-Date
        }
        if ($lastNew -and ((Get-Date) - $lastNew).TotalSeconds -ge 5) {
            Write-Output "--- MONITOR_ACTION_START ---"
            Write-Output "Log: $currentPath"
            $collected | ForEach-Object { Write-Output $_ }
            Write-Output "--- MONITOR_ACTION_END ---"
            exit 0
        }
    }
    Start-Sleep -Milliseconds 500
}
Write-Output "--- MONITOR_TIMEOUT_NO_ACTION ---"
exit 124
