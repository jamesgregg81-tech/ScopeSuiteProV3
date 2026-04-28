Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class NativeInput2 {
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int X, int Y);
  [DllImport("user32.dll")] public static extern void mouse_event(uint dwFlags, uint dx, uint dy, uint dwData, UIntPtr dwExtraInfo);
}
"@
$proc = Get-Process FlukeScopeSuiteV2AutoTune | Where-Object {$_.MainWindowHandle -ne 0} | Select-Object -First 1
if (-not $proc) { throw "No ScopeSuite EXE window found" }
[NativeInput2]::ShowWindow($proc.MainWindowHandle, 9) | Out-Null
[NativeInput2]::SetForegroundWindow($proc.MainWindowHandle) | Out-Null
Start-Sleep -Milliseconds 500
function ClickAt([int]$x,[int]$y,[string]$label) {
  Write-Output "Click $label at $x,$y"
  [NativeInput2]::SetCursorPos($x,$y) | Out-Null
  Start-Sleep -Milliseconds 120
  [NativeInput2]::mouse_event(0x0002,0,0,0,[UIntPtr]::Zero)
  Start-Sleep -Milliseconds 80
  [NativeInput2]::mouse_event(0x0004,0,0,0,[UIntPtr]::Zero)
  Start-Sleep -Milliseconds 350
}
ClickAt 246 201 "Serial Port field"
[System.Windows.Forms.SendKeys]::SendWait('^a')
Start-Sleep -Milliseconds 100
[System.Windows.Forms.SendKeys]::SendWait('COM10')
Start-Sleep -Milliseconds 200
[System.Windows.Forms.SendKeys]::SendWait('{ENTER}')
Start-Sleep -Milliseconds 500
ClickAt 548 201 "Test/Connect button"
Write-Output "Waiting 30s for Connect/Test worker"
Start-Sleep -Seconds 30
ClickAt 248 584 "Capture Screen button #1"
Write-Output "Waiting 95s for capture #1"
Start-Sleep -Seconds 95
ClickAt 248 584 "Capture Screen button #2"
Write-Output "Waiting 95s for capture #2"
Start-Sleep -Seconds 95
Write-Output "GUI action sequence completed"
