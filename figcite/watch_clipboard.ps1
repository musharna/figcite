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

# Exactly one watcher may own a staging directory.
#
# Capture files are named clip-<second>-<md5[0..7]>, so two watchers that see the
# SAME clipboard image in the same second compute the SAME path and race to write
# the sidecar. Measured 2026-08-16: three watchers had accumulated, the collision
# failed Set-Content with "Stream was not readable", and the capture was lost.
#
# The mutex sits here rather than in the Python launcher because that is the layer
# that can actually enforce it: the launcher's guard could only ever check whether
# a *supervisor* was running, which is not the thing being duplicated. Named per
# staging directory, so a test using a private dir is unaffected by production.
$mutexName = "Global\figcite-watch-" + ([System.BitConverter]::ToString(
  [System.Security.Cryptography.MD5]::Create().ComputeHash(
    [System.Text.Encoding]::UTF8.GetBytes($StagingDir.ToLower()))).Replace("-",""))
$createdNew = $false
$mutex = New-Object System.Threading.Mutex($true, $mutexName, [ref]$createdNew)
if (-not $createdNew) {
  Write-Output "WATCH_ALREADY_RUNNING $StagingDir"
  exit 3
}

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
        # Announce ONLY after both files are on disk. Previously the sidecar
        # write could fail while CAPTURED was emitted anyway, so the consumer
        # was told about a capture whose context did not exist -- and because
        # PowerShell's error text shares this stream, it spliced itself into the
        # middle of the CAPTURED line and produced a corrupted path.
        # WriteAllText rather than Set-Content: Set-Content is what failed with
        # "Stream was not readable" under the concurrent-writer race.
        $ok = $true
        try {
          [System.IO.File]::WriteAllText(
            $base + ".capture.json",
            ($info | ConvertTo-Json -Compress),
            (New-Object System.Text.UTF8Encoding($false)))
        } catch {
          $ok = $false
          Write-Output ("CAPTURE_FAILED " + $base + ".png :: " + $_.Exception.Message)
          Remove-Item -Force -ErrorAction SilentlyContinue ($base + ".png")
          $last = ""   # not captured, so let the same image be retried
        }
        if ($ok) { Write-Output ("CAPTURED " + $base + ".png") }
      }
      $img.Dispose()
    }
  } catch { }
  Start-Sleep -Milliseconds $PollMs
}
Write-Output "WATCH_END"
