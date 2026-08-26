param(
  [string]$StagingDir = "$env:USERPROFILE\.figcite\staging",
  [double]$MaxHours = 8,
  [switch]$CaptureExisting
)
# Long-lived clipboard watcher.
#
# WHY THIS IS NOT A POLL LOOP. It used to be: one PowerShell process asked
# `Clipboard::GetImage()` every 800ms -- 75 times a minute, 108,000 times a day
# -- and for all but a handful of those the answer was "nothing new". Each ask
# OPENS the clipboard, and while it is open no other application can. So an idle
# watcher was buying nothing and charging the whole desktop for it.
#
# Windows already offers the inverse: AddClipboardFormatListener registers a
# window to be TOLD, via WM_CLIPBOARDUPDATE, when the clipboard changes.
# Microsoft's own guidance is that new clipboard-monitoring programs should use
# it, and explicitly that the clipboard sequence number must not be polled as a
# substitute. So the watcher now sleeps on an event and wakes only when there is
# genuinely something to look at.
#
# There is deliberately NO polling fallback. A fallback would put back exactly
# the mechanism this removes, and put it back in the one situation nobody is
# watching -- so a failure to subscribe is announced and fatal instead.
Add-Type -AssemblyName System.Windows.Forms,System.Drawing
Add-Type -ReferencedAssemblies System.Windows.Forms,System.Drawing -TypeDefinition @'
using System; using System.Runtime.InteropServices; using System.Text;
using System.Threading; using System.Windows.Forms;

public class FgWin {
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern int GetWindowThreadProcessId(IntPtr h, out uint pid);
  public static string Title(){ IntPtr h=GetForegroundWindow(); StringBuilder sb=new StringBuilder(2048); GetWindowText(h,sb,2048); return sb.ToString(); }
  public static uint Pid(){ IntPtr h=GetForegroundWindow(); uint p; GetWindowThreadProcessId(h,out p); return p; }
}

// Asks Windows to tell us when the clipboard changes, instead of asking Windows
// 75 times a minute whether it has.
//
// The window is message-only (its parent is HWND_MESSAGE): it is never painted,
// never appears on the taskbar, receives no input, and exists solely to have a
// message queue that WM_CLIPBOARDUPDATE can be delivered to. It lives on its own
// STA thread running a message pump, and hands the main thread a plain event.
//
// The main thread therefore keeps ALL the clipboard and file work -- reading the
// bitmap, hashing, writing the pair of files -- exactly where it was. Only the
// question "is it worth looking yet?" moved.
public class ClipListener {
  const int WM_CLIPBOARDUPDATE = 0x031D;
  const int WM_CLOSE = 0x0010;
  static readonly IntPtr HWND_MESSAGE = new IntPtr(-3);

  [DllImport("user32.dll", SetLastError=true)] static extern bool AddClipboardFormatListener(IntPtr hwnd);
  [DllImport("user32.dll", SetLastError=true)] static extern bool RemoveClipboardFormatListener(IntPtr hwnd);
  [DllImport("user32.dll", SetLastError=true)] static extern bool PostMessage(IntPtr hwnd, int msg, IntPtr w, IntPtr l);

  class Sink : NativeWindow {
    readonly AutoResetEvent changed;
    // The event is passed IN rather than assigned after construction: creating
    // the handle can deliver a message before the next statement runs, and a
    // WM_CLIPBOARDUPDATE that arrives while the field is still null is a
    // dropped capture that would reproduce roughly never.
    public Sink(AutoResetEvent e) {
      changed = e;
      CreateParams cp = new CreateParams();
      cp.Parent = HWND_MESSAGE;
      CreateHandle(cp);
    }
    protected override void WndProc(ref Message m) {
      if (m.Msg == WM_CLIPBOARDUPDATE) { changed.Set(); return; }
      if (m.Msg == WM_CLOSE) { Application.ExitThread(); return; }
      base.WndProc(ref m);
    }
  }

  // AutoResetEvent, not a counter: several WM_CLIPBOARDUPDATE messages can
  // describe ONE copy (an application that publishes three formats, or renders
  // one of them lazily). Collapsing them into "there is something to look at"
  // is correct, and the content hash below settles whether it is new.
  public AutoResetEvent Changed = new AutoResetEvent(false);
  public string StartupError;              // null once the subscription is live

  Sink sink;
  Thread pump;
  readonly ManualResetEventSlim ready = new ManualResetEventSlim(false);

  public bool Start(int timeoutMs) {
    pump = new Thread(Run);
    pump.IsBackground = true;
    pump.SetApartmentState(ApartmentState.STA);
    pump.Start();
    if (!ready.Wait(timeoutMs)) {
      StartupError = "the listener thread did not come up within " + timeoutMs + "ms";
      return false;
    }
    return StartupError == null;
  }

  void Run() {
    try {
      sink = new Sink(Changed);
      if (!AddClipboardFormatListener(sink.Handle))
        StartupError = "AddClipboardFormatListener failed (win32 error "
                     + Marshal.GetLastWin32Error() + ")";
    } catch (Exception e) {
      StartupError = e.GetType().Name + ": " + e.Message;
    }
    ready.Set();
    if (StartupError != null) return;
    Application.Run();                     // sleeps until a message arrives
    try { RemoveClipboardFormatListener(sink.Handle); } catch { }
  }

  public void Stop() {
    if (sink == null) return;
    PostMessage(sink.Handle, WM_CLOSE, IntPtr.Zero, IntPtr.Zero);
    if (pump != null) pump.Join(2000);
  }
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

# How long to keep asking when the clipboard is there but will not open. Another
# application owns it for the moment it takes to finish publishing formats, so a
# first refusal means "not yet", not "nothing".
$RetryAttempts = 10
$RetryMs       = 150

function Read-ClipboardImage {
  <#
    THREE outcomes, never two.

      Image      -- the clipboard opened and holds a bitmap
      NoImage    -- the clipboard opened and holds no bitmap (text was copied)
      Unreadable -- the clipboard would not open; we do not know what is on it

    The loop this replaced wrapped everything in `catch { }`, which turned the
    third case into the second: a clipboard we could not read reported the same
    as a clipboard with nothing in it. That is an outage folded into an absence,
    which is the one thing this project exists not to do.
  #>
  $err = "no attempt was made"
  for ($i = 0; $i -lt $RetryAttempts; $i++) {
    try {
      if (-not [System.Windows.Forms.Clipboard]::ContainsImage()) {
        # Opened fine and there is genuinely no bitmap on it. Copying text lands
        # here, and it costs one format query rather than a decoded bitmap.
        return @{ state = "NoImage" }
      }
      $img = [System.Windows.Forms.Clipboard]::GetImage()
      if ($img -eq $null) { return @{ state = "NoImage" } }
      return @{ state = "Image"; image = $img }
    } catch {
      $err = $_.Exception.Message
      Start-Sleep -Milliseconds $RetryMs
    }
  }
  return @{ state = "Unreadable"; error = $err }
}

$listener = New-Object ClipListener
if (-not $listener.Start(10000)) {
  # Loud and fatal. Falling back to polling here would restore the very cost
  # this script exists to remove, silently, on the one machine nobody is
  # watching -- and the supervising launcher already retries with a backoff.
  Write-Output ("WATCH_FAILED could not subscribe to clipboard changes :: " + $listener.StartupError)
  exit 4
}

$deadline = (Get-Date).AddHours($MaxHours)
$last = ""
$md5 = [System.Security.Cryptography.MD5]::Create()

# Prime the hash with whatever is ALREADY on the clipboard so it is not captured
# as a fresh snip. The foreground window right now is the app that happened to be
# focused when the watcher started -- not the app the image came from -- so
# capturing it would attach a confidently wrong source. -CaptureExisting opts in.
if (-not $CaptureExisting) {
  $pre = Read-ClipboardImage
  if ($pre.state -eq "Image") {
    $pms = New-Object System.IO.MemoryStream
    $pre.image.Save($pms, [System.Drawing.Imaging.ImageFormat]::Png)
    $last = [System.BitConverter]::ToString($md5.ComputeHash($pms.ToArray())).Replace("-","")
    $pms.Dispose(); $pre.image.Dispose()
    Write-Output "WATCH_SKIPPED_PREEXISTING_CLIPBOARD_IMAGE"
  } elseif ($pre.state -eq "Unreadable") {
    # Say so rather than start with a silently empty hash: if there IS an image
    # we could not read, the next copy of that same image looks new.
    Write-Output ("CLIPBOARD_UNREADABLE at startup :: " + $pre.error)
  }
}
Write-Output "WATCH_START $StagingDir"
try {
  while ($true) {
    $remainingMs = ($deadline - (Get-Date)).TotalMilliseconds
    if ($remainingMs -le 0) { break }
    # Capped so the deadline is still honoured promptly; between wakeups this
    # thread is genuinely asleep and the process uses no CPU at all.
    $waitMs = [int][Math]::Min($remainingMs, 60000)
    if (-not $listener.Changed.WaitOne($waitMs)) { continue }

    $read = Read-ClipboardImage
    if ($read.state -eq "Unreadable") {
      Write-Output ("CLIPBOARD_UNREADABLE " + $read.error)
      continue
    }
    if ($read.state -eq "NoImage") { continue }

    $img = $read.image
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
} finally {
  $listener.Stop()
}
Write-Output "WATCH_END"
