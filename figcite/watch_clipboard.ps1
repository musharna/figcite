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
using System.Threading; using System.Windows.Forms; using System.Drawing;

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

// Commands from the Python side, one per line on stdin.
//
// The main thread sleeps on WaitAny over this and the clipboard event, so a
// command wakes it the same way a clipboard change does. Console.In is read on
// its own thread because its "async" read is synchronous on .NET Framework and
// would block the loop. EOF (the consumer went away) just stops the reader.
public class StdinLines {
  public AutoResetEvent Ready = new AutoResetEvent(false);
  // Set once the consumer has gone away. The watcher then exits cleanly rather
  // than being killed, because a killed owner leaves a live clipboard empty.
  public volatile bool Closed;
  public System.Collections.Concurrent.ConcurrentQueue<string> Lines =
    new System.Collections.Concurrent.ConcurrentQueue<string>();
  public void Start() {
    Thread t = new Thread(() => {
      try {
        string line;
        while ((line = Console.In.ReadLine()) != null) { Lines.Enqueue(line); Ready.Set(); }
      } catch { }
      Closed = true; Ready.Set();
    });
    t.IsBackground = true;
    t.Start();
  }
}

// The handback's clipboard offer, tailored to the app in front.
//
// One offer cannot suit every app. Offered the tagged file (CF_HDROP), Affinity
// places the file and PowerPoint stores it byte-identical -- but PowerPoint then
// shows a bare picture. Offered HTML (an <img> of the same file plus a caption),
// PowerPoint pastes the picture AND a caption text box -- but Affinity takes only
// the bitmap and loses the file. Measured 2026-09-16/17.
//
// Answering each paste request on the fly does NOT work: Windows fixes the list
// of formats on offer when the clipboard is set, and PowerPoint reads that list,
// asks for the file it sees there, and gives up when refused -- measured
// 2026-09-17. So the offer is re-made when the app in front changes, while the
// clipboard is still ours: PowerPoint in front means picture + caption, anything
// else means the file. Each offer is an ordinary rendered copy, so it outlives
// this process, and none of them is added to clipboard history.
public class LiveClip {
  [DllImport("user32.dll")] static extern IntPtr GetClipboardOwner();
  [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] static extern int GetWindowThreadProcessId(IntPtr h, out uint pid);
  delegate void WinEventProc(IntPtr hook, uint ev, IntPtr hwnd, int obj, int child, uint thread, uint time);
  [DllImport("user32.dll")] static extern IntPtr SetWinEventHook(uint min, uint max, IntPtr mod,
    WinEventProc proc, uint pid, uint thread, uint flags);
  [DllImport("user32.dll")] static extern bool UnhookWinEvent(IntPtr hook);
  const uint EVENT_SYSTEM_FOREGROUND = 3, WINEVENT_OUTOFCONTEXT = 0;

  static uint PidOf(IntPtr h) { uint p; GetWindowThreadProcessId(h, out p); return p; }
  static readonly uint Self = (uint)System.Diagnostics.Process.GetCurrentProcess().Id;

  // Our own write, recognised by who owns the clipboard, not by its content.
  public static bool OwnedByUs() {
    IntPtr o = GetClipboardOwner();
    return o != IntPtr.Zero && PidOf(o) == Self;
  }

  static bool IsPowerPoint(IntPtr hwnd) {
    try {
      var name = System.Diagnostics.Process.GetProcessById((int)PidOf(hwnd)).ProcessName;
      return string.Equals(name, "POWERPNT", StringComparison.OrdinalIgnoreCase);
    } catch { return false; }
  }

  Control ctl;
  WinEventProc hookProc;     // held so the GC cannot collect the callback
  IntPtr hook;
  DataObject forPpt, forOthers, applied;
  public string StartupError;
  public volatile string LastError;

  public bool Start(int timeoutMs) {
    var ready = new ManualResetEventSlim(false);
    var t = new Thread(() => {
      try {
        ctl = new Control(); IntPtr h = ctl.Handle;
        hookProc = OnForeground;
        hook = SetWinEventHook(EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, IntPtr.Zero,
                               hookProc, 0, 0, WINEVENT_OUTOFCONTEXT);
        if (hook == IntPtr.Zero) StartupError = "SetWinEventHook failed";
      } catch (Exception e) { StartupError = e.GetType().Name + ": " + e.Message; }
      ready.Set();
      if (StartupError == null) { Application.Run(); UnhookWinEvent(hook); }
    });
    t.IsBackground = true;
    t.SetApartmentState(ApartmentState.STA);
    t.Start();
    if (!ready.Wait(timeoutMs)) { StartupError = "handback thread did not start within " + timeoutMs + "ms"; return false; }
    return StartupError == null;
  }

  // The window the event names, not GetForegroundWindow() at callback time:
  // by then focus may have moved again (measured 2026-09-17 -- the event named
  // PowerPoint while a re-query already returned the terminal).
  void OnForeground(IntPtr hk, uint ev, IntPtr hwnd, int obj, int child, uint thread, uint time) {
    if (forOthers == null) return;
    // Someone else has copied since: the clipboard is theirs, not ours to change.
    if (!OwnedByUs()) { forPpt = forOthers = applied = null; return; }
    try { Apply(IsPowerPoint(hwnd)); } catch (Exception e) { LastError = e.GetType().Name + ": " + e.Message; }
  }

  void Apply(bool pptInFront) {
    DataObject want = (forPpt != null && pptInFront) ? forPpt : forOthers;
    if (want == applied) return;
    Clipboard.SetDataObject(want, true, 10, 150);
    applied = want;
  }

  // Everything outside ASCII as a numeric character reference. Sent as raw
  // UTF-8, an en dash in an author list vanished from PowerPoint's caption
  // while the entity-encoded u-umlaut survived (measured 2026-09-17); pure ASCII
  // leaves no charset for a reader to guess, and no byte/char offset drift.
  static string AsciiHtml(string s) {
    var sb = new StringBuilder();
    for (int i = 0; i < s.Length; i++) {
      int cp = char.ConvertToUtf32(s, i);
      if (char.IsHighSurrogate(s[i])) i++;
      if (cp < 128) sb.Append((char)cp); else sb.Append("&#").Append(cp).Append(';');
    }
    return sb.ToString();
  }

  // CF_HTML's offsets count BYTES; the document is ASCII, so bytes = chars.
  public static string CfHtml(string file, string caption, int w, int h) {
    int dw = Math.Min(w, 640), dh = (int)Math.Round(h * (dw / (double)w));
    string cap = AsciiHtml(System.Net.WebUtility.HtmlEncode(caption));
    cap = System.Text.RegularExpressions.Regex.Replace(cap, @"https://doi\.org/\S+",
      m => "<a href=\"" + m.Value + "\">" + m.Value + "</a>");
    string frag = "<div><img src=\"" + AsciiHtml(new Uri(file).AbsoluteUri) + "\" width=\"" + dw
                + "\" height=\"" + dh + "\"><p>" + cap + "</p></div>";
    string pre = "<html><body><!--StartFragment-->", post = "<!--EndFragment--></body></html>";
    string hdr = "Version:0.9\r\nStartHTML:{0:D10}\r\nEndHTML:{1:D10}\r\nStartFragment:{2:D10}\r\nEndFragment:{3:D10}\r\n";
    var u = Encoding.UTF8;
    int hl = u.GetByteCount(string.Format(hdr, 0, 0, 0, 0));
    int sF = hl + u.GetByteCount(pre), eF = sF + u.GetByteCount(frag), eH = eF + u.GetByteCount(post);
    return string.Format(hdr, hl, eH, sF, eF) + pre + frag + post;
  }

  static void KeepOutOfHistory(DataObject d) {
    // Windows' documented opt-outs; each takes a DWORD 0.
    d.SetData("CanIncludeInClipboardHistory", new System.IO.MemoryStream(BitConverter.GetBytes(0)));
    d.SetData("CanUploadToCloudClipboard", new System.IO.MemoryStream(BitConverter.GetBytes(0)));
  }

  // caption null/empty = no PowerPoint offer: an unconfirmed source gets no
  // caption, so every app is offered the file.
  public void Publish(string file, string caption) {
    Exception err = null;
    ctl.Invoke((MethodInvoker)delegate {
      try {
        Bitmap bmp;
        using (Image src = Image.FromFile(file)) bmp = new Bitmap(src);   // file not held open
        var others = new DataObject();
        others.SetImage(bmp);
        var files = new System.Collections.Specialized.StringCollection();
        files.Add(file);
        others.SetFileDropList(files);
        KeepOutOfHistory(others);
        DataObject ppt = null;
        if (!string.IsNullOrEmpty(caption)) {
          ppt = new DataObject();
          ppt.SetImage(bmp);
          ppt.SetData(DataFormats.Html, CfHtml(file, caption, bmp.Width, bmp.Height));
          ppt.SetText(caption);
          KeepOutOfHistory(ppt);
        }
        forPpt = ppt; forOthers = others; applied = null;
        Apply(IsPowerPoint(GetForegroundWindow()));
      } catch (Exception e) { err = e; }
    });
    if (err != null) throw new InvalidOperationException(err.Message, err);
  }

  public void Stop() {
    if (ctl == null) return;
    try { ctl.Invoke((MethodInvoker)delegate { Application.ExitThread(); }); } catch { }
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

function Get-ImageHash($img) {
  $ms = New-Object System.IO.MemoryStream
  $img.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
  $h = [System.BitConverter]::ToString($md5.ComputeHash($ms.ToArray())).Replace("-","")
  $ms.Dispose()
  return $h
}

function Invoke-Handback([string]$capturedHash, [string]$captionB64, [string]$file) {
  <#
    Put the FILED figure back on the clipboard: to PowerPoint as picture plus
    caption, to everything else as the tagged file (see LiveClip).

    Only when the clipboard still holds the image that was captured: enrichment
    takes seconds, and anything copied in the meantime is the user's, not ours.
    #>
  if (-not $live) {
    Write-Output ("HANDBACK_FAILED " + $file + " :: the handback thread is not running"); return
  }
  if (-not [System.IO.File]::Exists($file)) {
    Write-Output ("HANDBACK_FAILED " + $file + " :: file not found"); return
  }
  $caption = $null
  if ($captionB64 -ne "-") {
    try { $caption = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($captionB64)) }
    catch { Write-Output ("HANDBACK_FAILED " + $file + " :: caption was not valid base64"); return }
  }
  $cur = Read-ClipboardImage
  if ($cur.state -ne "Image") {
    Write-Output ("HANDBACK_SKIPPED " + $file + " :: clipboard no longer holds the captured image ($($cur.state))"); return
  }
  $curHash = Get-ImageHash $cur.image
  $cur.image.Dispose()
  if ($curHash -ne $capturedHash) {
    Write-Output ("HANDBACK_SKIPPED " + $file + " :: a different image was copied since the capture"); return
  }
  try {
    $live.Publish($file, $caption)
  } catch {
    Write-Output ("HANDBACK_FAILED " + $file + " :: " + $_.Exception.Message); return
  }
  Write-Output ("HANDBACK_OK " + $file)
}

$stdin = New-Object StdinLines
$stdin.Start()
$live = New-Object LiveClip
if (-not $live.Start(10000)) {
  # Capture still works without it; say so rather than fail every handback silently.
  Write-Output ("HANDBACK_UNAVAILABLE " + $live.StartupError)
  $live = $null
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
    $woke = [System.Threading.WaitHandle]::WaitAny(
      [System.Threading.WaitHandle[]]@($listener.Changed, $stdin.Ready), $waitMs)
    if ($live -and $live.LastError) {
      Write-Output ("HANDBACK_SWITCH_FAILED " + $live.LastError); $live.LastError = $null
    }
    if ($woke -eq [System.Threading.WaitHandle]::WaitTimeout) { continue }
    if ($woke -eq 1) {
      $cmd = $null
      while ($stdin.Lines.TryDequeue([ref]$cmd)) {
        $parts = $cmd.Trim().Split(" ", 4)
        if ($parts.Length -eq 4 -and $parts[0] -eq "HANDBACK") {
          Invoke-Handback $parts[1].ToUpper() $parts[2] $parts[3]
        } elseif ($cmd.Trim()) {
          Write-Output ("UNKNOWN_COMMAND " + $cmd)
        }
      }
      if ($stdin.Closed) { Write-Output "WATCH_CONSUMER_GONE"; break }
      continue
    }
    # Our own handback -- first offered, or re-offered for the app now in
    # front -- is what woke us. Not a snip, and not a re-copy.
    if ($live -and [LiveClip]::OwnedByUs()) { continue }

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
    } elseif (-not [System.Windows.Forms.Clipboard]::ContainsFileDropList()) {
      # Copied AGAIN -- by another app, since our own handback is skipped by
      # sequence number above. Already filed, so not captured twice; but the
      # new copy is a bare bitmap, so the consumer may hand the file back. When
      # a file is already on the clipboard there is nothing to restore, which
      # also keeps a clipboard manager that re-sets our data from ping-ponging.
      Write-Output ("RECOPIED " + $hash + " " + $img.Width + " " + $img.Height)
    }
    $img.Dispose()
  }
} finally {
  if ($live) {
    if ($live.LastError) { Write-Output ("HANDBACK_SWITCH_FAILED " + $live.LastError) }
    $live.Stop()
  }
  $listener.Stop()
}
Write-Output "WATCH_END"
