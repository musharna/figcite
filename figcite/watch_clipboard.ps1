param(
  [string]$StagingDir = "$env:USERPROFILE\.figcite\staging",
  [int]$PollMs = 800,
  [double]$MaxHours = 8,
  [switch]$CaptureExisting
)
# Long-lived clipboard watcher. One PowerShell process polls internally rather
# than WSL spawning powershell.exe every second (which costs ~300ms a pop).
Add-Type -AssemblyName System.Windows.Forms,System.Drawing
Add-Type -TypeDefinition @'
using System; using System.Runtime.InteropServices; using System.Text;
public class FgWin {
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern int GetWindowThreadProcessId(IntPtr h, out uint pid);
  public static string Title(){ IntPtr h=GetForegroundWindow(); StringBuilder sb=new StringBuilder(2048); GetWindowText(h,sb,2048); return sb.ToString(); }
  public static uint Pid(){ IntPtr h=GetForegroundWindow(); uint p; GetWindowThreadProcessId(h,out p); return p; }
}
'@
New-Item -ItemType Directory -Force -Path $StagingDir | Out-Null
$deadline = (Get-Date).AddHours($MaxHours)
$last = ""
$md5 = [System.Security.Cryptography.MD5]::Create()

# Prime the hash with whatever is ALREADY on the clipboard so it is not captured
# as a fresh snip. The foreground window right now is the app that happened to be
# focused when the watcher started -- not the app the image came from -- so
# capturing it would attach a confidently wrong source. -CaptureExisting opts in.
if (-not $CaptureExisting) {
  try {
    $pre = [System.Windows.Forms.Clipboard]::GetImage()
    if ($pre -ne $null) {
      $pms = New-Object System.IO.MemoryStream
      $pre.Save($pms, [System.Drawing.Imaging.ImageFormat]::Png)
      $last = [System.BitConverter]::ToString($md5.ComputeHash($pms.ToArray())).Replace("-","")
      $pms.Dispose(); $pre.Dispose()
      Write-Output "WATCH_SKIPPED_PREEXISTING_CLIPBOARD_IMAGE"
    }
  } catch { }
}
Write-Output "WATCH_START $StagingDir"
while ((Get-Date) -lt $deadline) {
  try {
    $img = [System.Windows.Forms.Clipboard]::GetImage()
    if ($img -ne $null) {
      $ms = New-Object System.IO.MemoryStream
      $img.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
      $bytes = $ms.ToArray(); $ms.Dispose()
      $hash = [System.BitConverter]::ToString($md5.ComputeHash($bytes)).Replace("-","")
      if ($hash -ne $last) {
        $last = $hash
        $stamp = (Get-Date).ToString("yyyyMMdd-HHmmss")
        $base  = Join-Path $StagingDir ("clip-" + $stamp + "-" + $hash.Substring(0,8))
        [System.IO.File]::WriteAllBytes($base + ".png", $bytes)
        # Foreground window at paste time is the app that was snipped from.
        $fgpid = [FgWin]::Pid()
        $proc  = Get-Process -Id $fgpid -ErrorAction SilentlyContinue
        $info = [ordered]@{
          png            = $base + ".png"
          title          = [FgWin]::Title()
          process        = $(if($proc){$proc.ProcessName}else{""})
          process_path   = $(if($proc){try{$proc.Path}catch{""}}else{""})
          captured_local = (Get-Date).ToString("o")
          width          = $img.Width
          height         = $img.Height
        }
        $info | ConvertTo-Json -Compress | Set-Content -Encoding UTF8 ($base + ".capture.json")
        Write-Output ("CAPTURED " + $base + ".png")
      }
      $img.Dispose()
    }
  } catch { }
  Start-Sleep -Milliseconds $PollMs
}
Write-Output "WATCH_END"
