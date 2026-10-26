# controller.py - v17 (Ctrl+B background operations, Ctrl+Z stops a command,
#                   a screen share or a screenshot; jobs/stop; fast send/receive)

import base64
import http.server
import io
import json
import os
import select
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

HOST = '0.0.0.0'
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 4444
MAX_FILE = 50 * 1024 * 1024    # 50 MB
MAX_MSG = 96 * 1024 * 1024     # sanity bound for one protocol message
OUTPUT_PRINT_LIMIT = 256 * 1024    # bigger replies: print head+tail, save the rest
OUTPUT_PRINT_PART = 48 * 1024


def recv_exact(conn, n):
    chunks = []
    while n > 0:
        chunk = conn.recv(min(n, 262144))
        if not chunk:
            return None
        chunks.append(chunk)
        n -= len(chunk)
    return b''.join(chunks)


def send_msg(conn, obj):
    payload = json.dumps(obj).encode('utf-8')
    conn.sendall(len(payload).to_bytes(4, 'big') + payload)


def recv_msg(conn):
    try:
        header = recv_exact(conn, 4)
        if header is None:
            return None
        size = int.from_bytes(header, 'big')
        if size > MAX_MSG:              # desynced/garbage stream - reconnect
            return None
        payload = recv_exact(conn, size)
        if payload is None:
            return None
        return json.loads(payload.decode('utf-8'))
    except (OSError, ValueError):
        return None


def recv_any(conn):
    """Read one message from the client: a JSON dict, or a screen-share frame
    (marked with b'\\x01') as {'type': 'frame', 'jpeg': ...}. None = the
    connection is gone or the stream is out of sync."""
    try:
        header = recv_exact(conn, 4)
        if header is None:
            return None
        size = int.from_bytes(header, 'big')
        if size > MAX_MSG:
            return None
        payload = recv_exact(conn, size)
        if payload is None:
            return None
        if payload[:1] == b'\x01':
            return {'type': 'frame', 'jpeg': payload[1:]}
        return json.loads(payload.decode('utf-8'))
    except (OSError, ValueError):
        return None


def split_args(line):
    """Split a command line into arguments: quotes group words together.

    Backslashes are kept as-is, so Windows paths (C:\\Users\\boss\\my shot.png)
    survive when quoted, and a single quote only groups when it starts an
    argument, so names like O'Brien still work unquoted."""
    args, current, quote, has = [], '', None, False
    for ch in line:
        if quote:
            if ch == quote:
                quote = None
            else:
                current += ch
                has = True
        elif ch == '"' or (ch == "'" and not current and not has):
            quote = ch
            has = True
        elif ch.isspace():
            if current or has:
                args.append(current)
                current, has = '', False
        else:
            current += ch
            has = True
    if current or has:
        args.append(current)
    return args


CLIENT_ENV = {}          # environment report from the connected client (hello)

CAPTURE_HELPERS = {
    'pillow': 'Pillow',
    'pyautogui': 'pyautogui',
    'grim': 'grim',
    'spectacle': 'spectacle',
    'gnome_dbus': 'gnome-shell (built-in)',
    'gnome_screenshot': 'gnome-screenshot',
    'imagemagick': 'ImageMagick',
    'scrot': 'scrot',
    'maim': 'maim',
}

# Install package name per helper and package-manager family
CAPTURE_PACKAGES = {
    'pacman': {'pillow': 'python-pillow', 'grim': 'grim', 'spectacle': 'spectacle',
               'gnome_screenshot': 'gnome-screenshot', 'imagemagick': 'imagemagick'},
    'apt': {'pillow': 'python3-pil', 'grim': 'grim', 'spectacle': 'spectacle',
            'gnome_screenshot': 'gnome-screenshot', 'imagemagick': 'imagemagick'},
    'dnf': {'pillow': 'python3-pillow', 'grim': 'grim', 'spectacle': 'spectacle',
            'gnome_screenshot': 'gnome-screenshot', 'imagemagick': 'ImageMagick'},
    'zypper': {'pillow': 'python3-Pillow', 'grim': 'grim', 'spectacle': 'spectacle',
               'gnome_screenshot': 'gnome-screenshot', 'imagemagick': 'ImageMagick'},
}

PM_COMMAND = {
    'pacman': 'sudo pacman -S {}',
    'apt': 'sudo apt install {}',
    'dnf': 'sudo dnf install {}',
    'zypper': 'sudo zypper install {}',
}

# distro ID (from the client's /etc/os-release) -> package-manager family
DISTRO_FAMILY = {
    'arch': 'pacman', 'omarchy': 'pacman', 'manjaro': 'pacman', 'endeavouros': 'pacman',
    'garuda': 'pacman', 'cachyos': 'pacman', 'artix': 'pacman',
    'ubuntu': 'apt', 'debian': 'apt', 'kali': 'apt', 'linuxmint': 'apt', 'lmde': 'apt',
    'pop': 'apt', 'zorin': 'apt', 'elementary': 'apt', 'raspbian': 'apt', 'devuan': 'apt',
    'fedora': 'dnf', 'nobara': 'dnf', 'rhel': 'dnf', 'centos': 'dnf', 'rocky': 'dnf',
    'almalinux': 'dnf', 'ol': 'dnf',
    'opensuse-leap': 'zypper', 'opensuse-tumbleweed': 'zypper', 'opensuse': 'zypper',
    'sles': 'zypper',
}


def capture_ok(env):
    """Best guess whether screen capture can work on the client right now."""
    if not env:
        return True            # unknown (older client) - do not warn
    if (env.get('os') or '') == 'linux' and (env.get('session') or '') == 'none':
        return False
    helpers = env.get('helpers') or {}
    if any(helpers.get(k) for k in ('pillow', 'pyautogui', 'grim', 'spectacle',
                                    'gnome_screenshot', 'imagemagick', 'scrot', 'maim')):
        return True
    return 'gnome' in (env.get('desktop') or '').lower() and bool(helpers.get('gnome_dbus'))


def fix_capture_lines(env, category=None):
    """'How to fix screen capture on the client' lines, tailored to its OS,
    distro, session and the helpers it is missing."""
    env = env or {}
    os_name = (env.get('os') or '').lower()
    distro = (env.get('distro') or '').lower()
    name = env.get('distro_name') or 'the client'
    session = env.get('session') or ''
    desktop = (env.get('desktop') or '').lower()
    helpers = env.get('helpers') or {}
    lines = []

    if os_name == 'windows':
        lines.append("Screen capture needs Pillow on the Windows client.")
        lines.append("Run on the client:  py -m pip install pillow")
        lines.append("Then run the command again.")
        return lines

    if os_name and os_name != 'linux':
        lines.append("Install Pillow on the client (pip install pillow), then retry.")
        return lines

    if session == 'none':
        lines.append("The client has no graphical session (DISPLAY / WAYLAND_DISPLAY are not set).")
        lines.append("Start the client from a terminal inside the logged-in desktop, then retry.")
        return lines

    wanted = ['pillow']
    if session == 'wayland':
        if 'kde' in desktop or 'plasma' in desktop:
            wanted.append('spectacle')
        elif 'gnome' in desktop:
            wanted.append('gnome_screenshot')
        else:
            wanted.append('grim')          # Hyprland, Sway, ...
    else:
        wanted.append('imagemagick')
    missing = [k for k in wanted if not helpers.get(k)]

    if category == 'all_failed':
        lines.append("Every capture method on the client failed (see the attempts above).")
    elif missing:
        lines.append("Screen capture dependencies are missing on the client ({}).".format(name))
    else:
        lines.append("Screen capture is failing on the client ({}) - see the attempts above.".format(name))

    if missing:
        family = DISTRO_FAMILY.get(distro)
        table = CAPTURE_PACKAGES.get(family, {})
        pkgs = [table[k] for k in missing if k in table]
        if pkgs and family in PM_COMMAND:
            lines.append("To fix, on the client:  {}".format(PM_COMMAND[family].format(' '.join(pkgs))))
        if 'pillow' in missing:
            lines.append("(without sudo:  python3 -m pip install --break-system-packages pillow)")

    if session == 'wayland':
        if 'kde' in desktop or 'plasma' in desktop:
            lines.append("Tip: KDE captures via spectacle (it ships with Plasma).")
        elif 'gnome' in desktop:
            lines.append("Tip: GNOME screenshots work via gnome-screenshot / the built-in API.")
        else:
            lines.append("Tip: on Wayland, Hyprland/Sway need grim; KDE and GNOME have their own tools.")
    else:
        lines.append("Tip: on X11, Pillow alone is enough (scrot or ImageMagick also work).")

    lines.append("After installing, run the command again - no client restart needed.")
    return lines


def print_capture_fix(diag, out=None):
    """Show what the client tried, plus how to fix it."""
    out = out or sys.stdout
    env = diag.get('env') or CLIENT_ENV or {}
    print("[!] Screen capture failed on the client. What was tried:", file=out)
    for attempt in diag.get('attempts') or []:
        print("      - {}".format(attempt), file=out)
    for line in fix_capture_lines(env, diag.get('category')):
        print("    " + line, file=out)


def print_client_error(msg, out=None):
    """One error line, plus the fix block when it was screen capture."""
    out = out or sys.stdout
    print("[!] Client error: {}".format(msg.get('data', '')), file=out)
    diag = msg.get('diag') or {}
    if diag.get('kind') == 'capture':
        print_capture_fix(diag, out)


def relevant_helpers(env):
    """Which capture helpers matter for this client's session and desktop
    (so the connect banner does not list tools that are irrelevant, like
    grim on a GNOME box)."""
    session = env.get('session') or ''
    desktop = (env.get('desktop') or '').lower()
    keys = ['pillow', 'pyautogui']
    if session == 'wayland':
        if 'kde' in desktop or 'plasma' in desktop:
            keys.append('spectacle')
        elif 'gnome' in desktop:
            keys += ['gnome_dbus', 'gnome_screenshot']
        else:
            keys.append('grim')            # Hyprland, Sway, ...
    elif session == 'x11':
        keys += ['imagemagick', 'scrot', 'maim']
    return keys


def print_client_env(env, out=None):
    """Show what the client reported on connect, warning early when screen
    capture is missing dependencies."""
    out = out or sys.stdout
    if not env:
        return
    name = env.get('distro_name') or env.get('os') or 'unknown'
    session = env.get('session') or '?'
    desktop = env.get('desktop') or ''
    label = session
    if session == 'wayland' and desktop:
        label = 'wayland [{}]'.format(desktop)
    print("[+] Client environment: {} ({})".format(name, env.get('distro') or '?'), file=out)
    print("    Session: {} | Python {}".format(label, env.get('python') or '?'), file=out)
    helpers = env.get('helpers') or {}
    relevant = relevant_helpers(env)
    have = [pretty for key, pretty in CAPTURE_HELPERS.items() if helpers.get(key)]
    missing = [pretty for key, pretty in CAPTURE_HELPERS.items()
               if key in relevant and not helpers.get(key)]
    print("    Capture helpers: {} | missing: {}".format(
        ', '.join(have) if have else 'none',
        ', '.join(missing) if missing else 'none'), file=out)
    if not capture_ok(env):
        print("[!] Heads-up: screen capture is not ready on this client:", file=out)
        for line in fix_capture_lines(env, None):
            print("    " + line, file=out)

def _ingest_hello(msg):
    """Store and show the environment report a client sends on connect.
    (A late hello arrives on the reader thread, so it is queued like any
    other message that shows up on its own.)"""
    global CLIENT_ENV
    CLIENT_ENV = msg.get('env') or {}
    _async_print(_capture_text(print_client_env, CLIENT_ENV))


def read_hello(conn):
    """Read the hello a fresh client sends right after connecting. A short
    timeout keeps older clients (which send none) from stalling us. The
    timeout only covers *waiting for the first byte* - once the message has
    started arriving it is read completely (a half-read hello would desync
    the protocol)."""
    global CLIENT_ENV
    CLIENT_ENV = {}
    try:
        readable, _, _ = select.select([conn], [], [], 1.5)
    except OSError:
        return
    if conn not in readable:
        return
    try:
        conn.settimeout(10)
        msg = recv_msg(conn)
    except OSError:
        msg = None
    finally:
        try:
            conn.settimeout(None)
        except OSError:
            pass
    if isinstance(msg, dict) and msg.get('type') == 'hello':
        _ingest_hello(msg)


def _show_output(text, out=None):
    """Print a command's output.

    A really large blob (a flood, a big 'cat', a 20 MB build log, ...) is
    printed as its first and last part and saved in full to a file. Blasting
    megabytes at a terminal is slow on every OS/window manager - that was
    making the controller look like a slow receiver."""
    out = out or sys.stdout
    if len(text) <= OUTPUT_PRINT_LIMIT:
        print("[+] Received output: {}".format(text), file=out)
        return
    name = os.path.join(tempfile.gettempdir(),
                        time.strftime('controller_output_%Y%m%d_%H%M%S.txt'))
    saved = None
    try:
        with open(name, 'w', encoding='utf-8') as f:
            f.write(text)
        saved = os.path.abspath(name)
    except OSError:
        saved = None
    head = text[:OUTPUT_PRINT_PART]
    tail = text[-OUTPUT_PRINT_PART:]
    print("[+] Received output: {} bytes - showing the first and last part".format(len(text)), file=out)
    print(head, file=out)
    print("... [{} bytes omitted] ...".format(len(text) - len(head) - len(tail)), file=out)
    print(tail, file=out)
    if saved:
        print("[+] Full output saved: {}".format(saved), file=out)


def show_reply(reply, save_as=None, out=None):
    """Print one client reply (the display half of an answer)."""
    out = out or sys.stdout
    rtype = reply.get('type')
    if rtype == 'output':
        _show_output(reply.get('data', ''), out)
    elif rtype == 'error':
        print_client_error(reply, out)
    elif rtype == 'file':
        name = save_as or reply.get('name') or 'downloaded.bin'
        try:
            data = base64.b64decode(reply.get('data', ''))
            with open(name, 'wb') as f:
                f.write(data)
            print("[+] File saved: {} ({} bytes)".format(os.path.abspath(name), len(data)), file=out)
        except Exception as e:
            print("[!] Could not save file: {}".format(e), file=out)
    elif rtype == 'image':
        name = save_as or time.strftime('screenshot_%Y%m%d_%H%M%S.jpg')
        try:
            data = base64.b64decode(reply.get('data', ''))
            with open(name, 'wb') as f:
                f.write(data)
            path = os.path.abspath(name)
            print("[+] Screenshot saved: {} ({} bytes)".format(path, len(data)), file=out)
            if not os.environ.get('SCREEN_NO_BROWSER'):
                open_default_app(path)
        except Exception as e:
            print("[!] Could not save the screenshot: {}".format(e), file=out)
    else:
        print("[?] Unknown reply: {}".format(reply), file=out)


# ---------------------------------------------------------------------------
# Messages that arrive on their own (answers to background operations, notices
# from the reader thread) must not be printed in the middle of the prompt: GNU
# readline redraws the line on every keypress and would wipe them out. They are
# queued instead and flushed as soon as the screen belongs to the terminal
# again - before/after the prompt, and whenever a foreground operation ends.
# ---------------------------------------------------------------------------

ASYNC_LOCK = threading.Lock()
ASYNC_QUEUE = []
PROMPT_ACTIVE = False           # read_command_line() is showing the prompt
SHELL_ACTIVE = False            # an interactive shell session owns the terminal
_BELLED = False                 # already rang for the queued messages


def _capture_text(func, *args):
    """Run a display function and return the text it would have printed."""
    buf = io.StringIO()
    func(*args, out=buf)
    return buf.getvalue().rstrip('\n')


def _quiet_write(text):
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except Exception:
        pass


def _async_print(text):
    """Show a message that came in on its own (background output, notes).

    A background operation must not have to wait for Enter before the user can
    see its output, so the message is printed straight away - on a new line, so
    the line GNU readline is editing is not wiped out (the half-typed command
    itself survives: readline keeps it and returns it on Enter). Only during an
    interactive shell session, where the terminal belongs to the remote shell,
    is the message kept back until the session ends."""
    global _BELLED
    if not text:
        return
    if SHELL_ACTIVE:
        with ASYNC_LOCK:
            ASYNC_QUEUE.append(text)
        if not _BELLED:                 # so the user knows something happened
            _BELLED = True
            _quiet_write('\a')
        return
    if PROMPT_ACTIVE:
        _quiet_write('\n')              # do not collide with the prompt line
    print(text)


def flush_async():
    """Print everything that was queued while the terminal was busy."""
    global _BELLED
    with ASYNC_LOCK:
        items = ASYNC_QUEUE[:]
        del ASYNC_QUEUE[:]
        _BELLED = False
    for text in items:
        try:
            print(text)
        except Exception:
            pass


BROWSER_PAGE = b"""<!DOCTYPE html>
<html><head><title>Client screen (live)</title>
<style>
 body { margin:0; background:#101010; color:#ddd; font-family:sans-serif; text-align:center; }
 img { width:100%; max-width:1280px; }
 button { margin:10px; padding:8px 18px; font-size:14px; }
</style></head><body>
<h3>Live screen of the client</h3>
<img src="/stream" onerror="document.querySelector('h3').textContent='Stream ended - you can close this tab';">
<p><button onclick="fetch('/stop').then(()=>{document.querySelector('h3').textContent='stopping...'})">Stop sharing</button></p>
</body></html>
"""


class BrowserViewer:
    """Tiny local web server. Serves the newest received frame as an MJPEG
    stream so the controller's own browser can watch the client's screen."""

    def __init__(self):
        self.jpeg = None            # newest frame (already JPEG)
        self.seq = 0                # bumped on every new frame
        self.stop_requested = False
        self.cond = threading.Condition()
        self.server = None
        self.url = None

    def feed(self, jpeg):
        with self.cond:
            self.jpeg = jpeg
            self.seq += 1
            self.cond.notify_all()

    def start(self):
        viewer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass                # keep the console quiet

            def do_GET(self):
                if self.path.startswith('/stream'):
                    self.send_response(200)
                    self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame')
                    self.send_header('Cache-Control', 'no-store')
                    self.end_headers()
                    self.connection.settimeout(15)
                    last = -1
                    try:
                        while not viewer.stop_requested:
                            with viewer.cond:
                                viewer.cond.wait_for(lambda: viewer.seq != last or viewer.stop_requested, timeout=1.0)
                                last = viewer.seq
                                frame = viewer.jpeg
                            if frame is None:
                                continue
                            self.wfile.write(b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: '
                                             + str(len(frame)).encode() + b'\r\n\r\n')
                            self.wfile.write(frame)
                            self.wfile.write(b'\r\n')
                    except OSError:
                        pass        # browser tab closed
                    return
                if self.path.startswith('/stop'):
                    viewer.stop_requested = True
                    with viewer.cond:
                        viewer.cond.notify_all()
                    body = b'<html><body><h3>Stopped. You can close this tab.</h3></body></html>'
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(BROWSER_PAGE)))
                self.end_headers()
                self.wfile.write(BROWSER_PAGE)

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.url = 'http://127.0.0.1:{}/'.format(self.server.server_address[1])
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.stop_requested = True
        with self.cond:
            self.cond.notify_all()
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception:
            pass


def open_default_app(target):
    """Open a URL or file with the desktop's default app. False if that failed."""
    try:
        subprocess.Popen(['xdg-open', target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        pass
    try:
        import webbrowser
        return webbrowser.open(target)
    except Exception:
        return False


def announce_viewer(viewer, seconds=None, out=None):
    """Print the viewer's URL and try to open the browser - called only after
    the first frame arrived, so the page can never load into a dead server."""
    out = out or sys.stdout
    print("[+] Live view: {}".format(viewer.url), file=out)
    print("[+] No browser opened? Paste that URL into any browser to watch the live screen.", file=out)
    if not os.environ.get('SCREEN_NO_BROWSER'):
        open_default_app(viewer.url)      # silent attempt to pop the default browser
    print("[+] Streaming - press the Stop button in the page to stop.", file=out)
    if seconds:
        print("[+] Auto-stop after {} seconds.".format(seconds), file=out)


# ---------------------------------------------------------------------------
# Requests and their answers.
#
# Every request carries an id and the client echoes it back, so one reader
# thread can own the control socket and hand each message to the operation it
# belongs to. That is what makes Ctrl+B possible: an operation keeps running
# (and keeps being answered) while the prompt is free, and a late answer from
# a canceled operation can never be mistaken for the answer to a newer one.
# ---------------------------------------------------------------------------

_ID_LOCK = threading.Lock()
_ID_SEQ = [0]


def next_request_id():
    with _ID_LOCK:
        _ID_SEQ[0] += 1
        return _ID_SEQ[0]


def _stray_message(msg):
    """Text for a message from the client that nothing is waiting for."""
    mtype = msg.get('type')
    if mtype == 'frame':
        return "[?] A screen frame arrived with no screen share running - ignored."
    data = msg.get('data')
    if isinstance(data, str) and len(data) > 300:
        data = data[:300] + '...'
    if mtype in ('output', 'error'):
        return "[?] Late answer from the client: {}".format(data)
    return "[?] Unexpected message from the client: {}".format(msg)


class Op(object):
    """One request sent to the client, waiting for its answer.

    The reader thread calls handle(); the prompt waits on done. Ctrl+B turns
    it into a background job (it keeps running, the prompt comes back) and
    Ctrl+Z cancels it on the client."""

    def __init__(self, kind, req_id, label=None, save_as=None):
        self.kind = kind            # cmd / simple / screenshot / download / screenshare
        self.id = req_id
        self.label = label or kind
        self.save_as = save_as
        self.done = threading.Event()
        self.background = False     # the prompt is not waiting for it any more
        self.canceled = False       # the user stopped it (Ctrl+Z)
        self.ok = True              # False: the client is gone
        self.link = None
        # screen share bookkeeping
        self.viewer = None
        self.frames = 0
        self.viewer_announced = False
        self.seconds = None
        self.stream_start = time.time()
        self.stopping = False
        self.stop_sent_at = None

    # -- called by the reader thread ---------------------------------------
    def handle(self, msg):
        """Consume one message from the client. False = it is not for us."""
        mtype = msg.get('type')
        if mtype == 'frame':
            return self._frame(msg)
        if not self._is_mine(msg):
            return False
        if self.kind == 'screenshare':
            if mtype not in ('output', 'error'):
                return False
            if mtype == 'error':
                _async_print(_capture_text(print_client_error, msg))
            elif not (self.stopping or self.canceled):
                _async_print(_capture_text(show_reply, msg))
            self._end_stream()
            return True
        if mtype in ('output', 'error') or \
                (mtype == 'file' and self.kind == 'download') or \
                (mtype == 'image' and self.kind == 'screenshot'):
            _async_print(self._tag(_capture_text(show_reply, msg, self.save_as)))
            self.done.set()
            return True
        return False

    def _is_mine(self, msg):
        rid = msg.get('id')
        if rid is None:
            return True             # older client: answers arrive in order
        return self.id is not None and rid == self.id

    def _tag(self, text):
        """Mark answers that arrive after the user moved on."""
        if self.background or self.canceled:
            return '[background] {}'.format(text)
        return text

    # -- screen shares ------------------------------------------------------
    def _frame(self, msg):
        if self.kind != 'screenshare':
            return False
        self.frames += 1
        jpeg = msg.get('jpeg')
        if self.viewer is None or self.stopping:
            return True                     # draining frames still in flight
        if jpeg:
            self.viewer.feed(jpeg)
        if not self.viewer_announced:
            self.viewer_announced = True
            _async_print(_capture_text(announce_viewer, self.viewer, self.seconds))
        if self.viewer.stop_requested:      # the Stop button in the page
            self.ask_stop()
        elif self.seconds and (time.time() - self.stream_start) >= self.seconds:
            self.ask_stop()
        return True

    def ask_stop(self):
        """Tell the client to stop streaming (frames still in flight are
        drained until its closing message arrives)."""
        if self.stopping:
            return
        self.stopping = True
        self.stop_sent_at = time.time()
        try:
            self.link.send({'type': 'screenshare_stop'})
        except OSError:
            self.link.mark_lost()

    def _end_stream(self):
        if self.viewer is not None:
            self.viewer.stop()
            self.viewer = None
        if self.frames:
            _async_print("[+] Screen share ended ({} frames).".format(self.frames))
        self.done.set()

    # -- what the user can do to a running operation -------------------------
    def detach(self):
        """Ctrl+B: keep it running on the client, give the prompt back."""
        if self.background:
            return
        self.background = True
        _async_print("[+] Moved to the background: {}. It keeps running on the "
                     "client - 'jobs' shows it, 'stop' ends it.".format(self.label))
        if self.kind == 'screenshare':
            _async_print("[+] The live view keeps updating. 'stop' (or Ctrl+Z then "
                         "Enter) ends the screen share.")

    def cancel(self, why='Ctrl+Z'):
        """Stop this operation on the client and give the prompt back."""
        if self.canceled:
            return
        self.canceled = True
        self.background = True          # the prompt is not waiting for it any more
        what = 'the screen share' if self.kind == 'screenshare' else 'this operation'
        _async_print("[+] {}: stopping {} on the client...".format(why, what))
        if self.kind == 'screenshare':
            self.ask_stop()
            return
        try:
            self.link.send({'type': 'cancel', 'id': self.id, 'why': why})
        except OSError:
            self.link.mark_lost()

    def describe(self):
        if self.done.is_set():
            state = 'finished'
        elif self.canceled:
            state = 'stopping'
        elif self.background:
            state = 'background'
        else:
            state = 'running'
        if self.kind == 'screenshare':
            state += ', {} frames'.format(self.frames)
        return '{} ({})'.format(self.label, state)


class ClientLink(object):
    """The control connection plus the reader thread that serves it.

    Messages go to the waiting operations in the order their requests were
    sent; the request id decides. Older clients (no ids) simply get the
    front-most operation that accepts that kind of message, which is exactly
    the order their answers arrive in."""

    def __init__(self, conn):
        self.conn = conn
        self.lock = threading.Lock()
        self.send_lock = threading.Lock()   # the reader thread can send too
        self.ops = []                   # in request order
        self.dead = False
        self.notified = False
        self.reader = threading.Thread(target=self._read_loop, name='client-reader')
        self.reader.daemon = True
        self.reader.start()

    # -- sending -------------------------------------------------------------
    def send(self, msg):
        # The reader thread sends as well (the Stop button in the live view
        # ends a share), so two threads could be writing at the same time -
        # without the lock their length headers and payloads would interleave.
        with self.send_lock:
            send_msg(self.conn, msg)

    # -- operation bookkeeping -------------------------------------------------
    def add(self, op):
        op.link = self
        with self.lock:
            self.ops.append(op)
        return op

    def drop(self, op):
        with self.lock:
            if op in self.ops:
                self.ops.remove(op)

    def snapshot(self):
        with self.lock:
            return list(self.ops)

    def jobs(self):
        """Operations running in the background (finished ones excluded)."""
        return [op for op in self.snapshot() if op.background and not op.done.is_set()]

    def mark_lost(self, note=True):
        """The client is gone: finish every operation waiting for it."""
        with self.lock:
            if self.dead:
                return
            self.dead = True
            pending = self.ops
            self.ops = []
            notify = note and not self.notified
            self.notified = True
        for op in pending:
            op.ok = False
            if op.viewer is not None:
                op.viewer.stop()
                op.viewer = None
            op.done.set()
        if notify:
            _async_print("[!] Client disconnected.")
        _wake_main()

    def close(self):
        """Shut the connection down without calling it a surprise."""
        self.mark_lost(note=False)
        try:
            self.conn.close()
        except OSError:
            pass

    # -- the reader thread -----------------------------------------------------
    def _read_loop(self):
        while True:
            try:
                msg = recv_any(self.conn)
            except OSError:
                msg = None
            if msg is None:
                self.mark_lost()
                return
            if msg.get('type') == 'hello':          # a late environment report
                _ingest_hello(msg)
                continue
            consumed = False
            for op in self.snapshot():
                try:
                    if op.handle(msg):
                        consumed = True
                        break
                except Exception as e:
                    _async_print("[!] Internal error while handling a client message: {}".format(e))
            if not consumed:
                _async_print(_stray_message(msg))
                continue
            for op in self.snapshot():
                if op.done.is_set():
                    self.drop(op)
            _wake_main()


# ---------------------------------------------------------------------------
# Interactive shell ("shell" command): shellcontrol.py injected here. A
# temporary listener opens, the client connects a fresh PTY to it, and this
# terminal switches to raw mode and pipes keystrokes until the session ends.
# ---------------------------------------------------------------------------

SHELL_PORT = 0                  # 0 = pick a free port; set a fixed one behind a firewall
SHELL_ACCEPT_TIMEOUT = 15       # seconds to wait for the client's shell connection
SHELL_ESCAPE = b'\x1d'          # Ctrl+] here: force the shell session closed


def get_terminal_size():
    try:
        size = os.get_terminal_size()
        return size.lines, size.columns
    except OSError:
        return 24, 80


def _shell_stdout(data):
    """Portable stdout write - survives redirected stdout."""
    try:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    except Exception:
        os.write(sys.stdout.fileno(), data)


def _shell_session_unix(conn):
    """Raw-mode pipeline: this terminal <-> the client's PTY (shellcontrol.py)."""
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd, termios.TCSANOW)         # TCSANOW: don't discard pending input
        while True:
            readable, _, _ = select.select([conn, sys.stdin], [], [])
            if sys.stdin in readable:
                data = os.read(fd, 4096)
                if not data:
                    break
                if SHELL_ESCAPE in data:        # Ctrl+]: leave from this side
                    before = data.split(SHELL_ESCAPE, 1)[0]
                    if before:
                        conn.sendall(before)
                    print("\r\n[+] Shell session closed (Ctrl+]).")
                    break
                conn.sendall(data)
            if conn in readable:
                data = conn.recv(65536)
                if not data:                    # shell exited on the client
                    break
                _shell_stdout(data)
    except (KeyboardInterrupt, OSError):
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _shell_session_windows(conn):
    """Windows console equivalent: read keys with msvcrt, print the replies."""
    import ctypes
    import msvcrt

    k32 = ctypes.windll.kernel32
    handle = k32.GetStdHandle(-10)              # STD_INPUT_HANDLE
    mode = ctypes.c_uint()
    k32.GetConsoleMode(handle, ctypes.byref(mode))
    # Disable ENABLE_PROCESSED_INPUT so Ctrl+C is delivered as input.
    k32.SetConsoleMode(handle, mode.value & ~0x0001)

    # Enable ENABLE_VIRTUAL_TERMINAL_PROCESSING on the console so the client's
    # ANSI output (colours, cursor moves, clear screen) renders properly.
    out_handle = k32.GetStdHandle(-11)          # STD_OUTPUT_HANDLE
    out_mode = ctypes.c_uint()
    vt_enabled = bool(k32.GetConsoleMode(out_handle, ctypes.byref(out_mode)))
    if vt_enabled:
        k32.SetConsoleMode(out_handle, out_mode.value | 0x0004)

    stop = threading.Event()

    def pump():
        try:
            while not stop.is_set():
                data = conn.recv(65536)
                if not data:
                    break
                _shell_stdout(data)
        except OSError:
            pass
        finally:
            stop.set()

    threading.Thread(target=pump, daemon=True).start()

    # Windows scan codes -> ANSI escape sequences
    arrow_map = {
        'H': b'\x1b[A',     # Up
        'P': b'\x1b[B',     # Down
        'M': b'\x1b[C',     # Right
        'K': b'\x1b[D',     # Left
        'S': b'\x1b[3~',    # Delete
        'G': b'\x1b[H',     # Home
        'O': b'\x1b[F',     # End
    }

    try:
        while not stop.is_set():
            if not msvcrt.kbhit():
                time.sleep(0.005)
                continue
            ch = msvcrt.getwch()
            if ch in ('\x00', '\xe0'):
                seq = arrow_map.get(msvcrt.getwch())
                if seq:
                    conn.sendall(seq)
                continue
            if ch == '\x1d':                    # Ctrl+]: leave from this side
                print('\n[+] Shell session closed (Ctrl+]).')
                break
            if ch == '\r':
                conn.sendall(b'\r\n')
            elif ch == '\x03':                  # Ctrl+C (if delivered)
                conn.sendall(b'\x03')
            else:
                conn.sendall(ch.encode('utf-8', 'replace'))
    except (KeyboardInterrupt, OSError):
        pass
    finally:
        stop.set()
        k32.SetConsoleMode(handle, mode.value)
        if vt_enabled:
            k32.SetConsoleMode(out_handle, out_mode.value)


def run_shell_session(conn):
    """Terminal-size handshake, then pipe this terminal to the client's PTY."""
    try:
        rows, cols = get_terminal_size()
        conn.sendall(struct.pack('!HH', rows, cols))
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        print("[!] The client's shell connection went away.")
        return
    if os.name == 'nt':
        _shell_session_windows(conn)
    else:
        _shell_session_unix(conn)


def start_interactive_shell(link):
    """Open an interactive shell on the client (shellcontrol.py, injected).

    A temporary port is opened and the client is asked to connect back with a
    PTY. Type 'exit' (or Ctrl+D) in that shell to return to this prompt; press
    Ctrl+] here to force the session closed. Returns False only when the
    client's main connection is gone (like the other commands)."""
    global SHELL_ACTIVE
    if not sys.stdin.isatty():
        print("[!] The interactive shell needs a real terminal (stdin is not a TTY).")
        return True
    STOP_REQUEST.clear()                # a stray Ctrl+Z must not stop a later job

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    op = None
    try:
        listener.bind((HOST, SHELL_PORT))
        listener.listen(1)
        port = listener.getsockname()[1]

        # The answer to this request (a refusal, e.g. "a shell is already
        # running") arrives on the reader thread like any other answer.
        op = link.add(Op('simple', next_request_id(), label='interactive shell'))
        try:
            link.send({'type': 'shell', 'id': op.id, 'port': port})
        except OSError:
            link.mark_lost()
            return False

        print("[+] Opening an interactive shell on the client...")
        conn = None
        deadline = time.time() + SHELL_ACCEPT_TIMEOUT
        while conn is None:
            if link.dead:
                return False
            if op.done.is_set():            # the client refused (see the note above)
                flush_async()
                return True
            remaining = deadline - time.time()
            if remaining <= 0:
                print("[!] The client did not connect back (port {} blocked?).".format(port))
                return True
            readable, _, _ = select.select([listener], [], [], min(0.2, remaining))
            if listener in readable:
                conn, addr = listener.accept()

        print("[+] Shell connected from {} - everything you type now goes to".format(addr))
        print("    the client's shell. Type 'exit' (or Ctrl+D) there to come back.")
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        SHELL_ACTIVE = True                 # keep background notes off the screen
        try:
            run_shell_session(conn)
        finally:
            SHELL_ACTIVE = False
            try:
                conn.close()
            except OSError:
                pass
            flush_async()
        print("[+] Interactive shell ended - back at the controller prompt.")
        return True
    except OSError as e:
        print("[!] Shell session failed: {}".format(e))
        return True
    finally:
        STOP_REQUEST.clear()
        if op is not None:
            link.drop(op)                   # the shell session is its own channel
        try:
            listener.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Command control: while an operation runs on the client, Ctrl+B moves it to
# the background and Ctrl+Z stops it - both without pressing Enter. On POSIX
# the terminal goes into cbreak mode for that wait (single keys arrive at
# once) while ISIG stays on, so Ctrl+Z still raises SIGTSTP (turned into a
# "stop that" request here) and Ctrl+C still interrupts. On Windows the keys
# are read with msvcrt instead.
# ---------------------------------------------------------------------------

PENDING_INPUT = ''                # type-ahead typed while something was running
STOP_REQUEST = threading.Event()  # set by Ctrl+Z (SIGTSTP) while something runs
WAKEUP_R = WAKEUP_W = None        # self-pipe: makes select() obey Ctrl+Z instantly
OP_R = OP_W = None                # self-pipe: the reader thread wakes the prompt


def _make_pipe():
    """A (read, write) pair of non-blocking pipes, or (None, None)."""
    try:
        r, w = os.pipe()
        os.set_blocking(r, False)
        os.set_blocking(w, False)
        return r, w
    except OSError:
        return None, None


def _setup_wakeup():
    """POSIX: Ctrl+Z must stop the running operation *now*.

    The terminal delivers Ctrl+Z as SIGTSTP. Python retries an interrupted
    select() automatically (PEP 475), so without help the loop would only
    notice the request when its timeout expired (up to 300 ms). A self-pipe
    wired to signal.set_wakeup_fd() makes select() return the instant the
    signal arrives, so the stop request is handled right away."""
    global WAKEUP_R, WAKEUP_W
    if os.name == 'nt':
        return
    WAKEUP_R, WAKEUP_W = _make_pipe()
    if WAKEUP_W is not None:
        try:
            signal.set_wakeup_fd(WAKEUP_W)
        except (AttributeError, OSError, ValueError):
            WAKEUP_R = WAKEUP_W = None


def _setup_op_pipe():
    """The reader thread writes here when an operation finishes, so the prompt
    notices at once instead of on its next poll tick. (Not on Windows: select()
    cannot watch pipes there, so the prompt polls instead.)"""
    global OP_R, OP_W
    if os.name == 'nt':
        return
    OP_R, OP_W = _make_pipe()


def _wake_main():
    """Wake the prompt's select() from the reader thread."""
    if OP_W is None:
        return
    try:
        os.write(OP_W, b'x')
    except OSError:
        pass


def _drain_op_pipe():
    if OP_R is None:
        return
    try:
        while os.read(OP_R, 4096):
            pass
    except (BlockingIOError, InterruptedError):
        pass
    except OSError:
        pass


def _drain_wakeup():
    """Throw away the wakeup bytes a signal left in the self-pipe."""
    if WAKEUP_R is None:
        return
    try:
        while os.read(WAKEUP_R, 4096):
            pass
    except (BlockingIOError, InterruptedError):
        pass
    except OSError:
        pass


def _handle_tstp(signum, frame):
    """Ctrl+Z: remember the request instead of suspending the controller."""
    STOP_REQUEST.set()


def _poll_keyboard():
    """On Windows: the keyboard text typed since the last call ('' = none).
    On POSIX nothing is read here - the wait loop reads the terminal itself
    (cbreak mode), so keys such as Ctrl+B arrive without pressing Enter."""
    if os.name != 'nt':
        return ''
    try:
        import msvcrt
    except ImportError:
        return ''
    try:
        if not msvcrt.kbhit():
            return ''
        chars = []
        while msvcrt.kbhit():
            ch = msvcrt.getwch()
            if ch in ('\x00', '\xe0'):      # arrow/function key: skip its code
                msvcrt.getwch()
                continue
            if ch == '\x08':                # backspace
                if chars:
                    chars.pop()
                continue
            if ch == '\r':
                ch = '\n'
            chars.append(ch)
        return ''.join(chars)
    except OSError:
        return ''


def read_command_line():
    """input('controller> '), but type-ahead caught while an operation was
    running is used first, so nothing the user typed is lost."""
    global PENDING_INPUT, PROMPT_ACTIVE
    flush_async()                       # show what the background produced
    if PENDING_INPUT:
        line, _, rest = PENDING_INPUT.partition('\n')
        PENDING_INPUT = rest
        text = line.rstrip('\r').strip()
        print("controller> {}".format(text))
        return text
    PROMPT_ACTIVE = True
    try:
        # GNU readline is only used when stdout is a terminal; with the output
        # redirected Python reads plain lines, and then a stray Ctrl+B/Ctrl+Z
        # pressed at the prompt would end up *inside* the command. They mean
        # nothing here (and SIGTSTP is already handled), so drop them.
        line = input("controller> ")
        if '\x02' in line or '\x1a' in line:
            line = line.replace('\x02', '').replace('\x1a', '')
        return line.strip()
    finally:
        PROMPT_ACTIVE = False
        flush_async()


def _type_ahead(text):
    """Echo what the user types while an operation runs and keep it for the
    next prompt. The terminal is in cbreak mode, so it does not echo itself -
    and escape sequences (arrow keys, function keys) are swallowed."""
    global PENDING_INPUT
    out = []
    i = 0
    while i < len(text):
        ch = text[i]
        i += 1
        if ch == '\x1b':                        # arrow/function key sequence
            if i < len(text) and text[i] == '[':
                i += 1
                while i < len(text) and not ('@' <= text[i] <= '~'):
                    i += 1
                i += 1
            elif i < len(text) and text[i] == 'O':
                i += 2
            continue
        if ch == '\x7f' or ch == '\x08':        # backspace
            if PENDING_INPUT and not PENDING_INPUT.endswith('\n'):
                PENDING_INPUT = PENDING_INPUT[:-1]
                out.append('\b \b')
            continue
        if ch == '\x15':                        # Ctrl+U: drop the typed line
            keep, sep, tail = PENDING_INPUT.rpartition('\n')
            if tail:
                PENDING_INPUT = keep + sep
                out.append('\b \b' * len(tail))
            continue
        if ch == '\x17':                        # Ctrl+W: drop the last word
            keep, sep, tail = PENDING_INPUT.rpartition('\n')
            trimmed = tail.rstrip(' ')
            word = trimmed.rsplit(' ', 1)[-1]
            removed = len(tail) - len(trimmed) + len(word)
            if removed:
                PENDING_INPUT = PENDING_INPUT[:-removed]
                out.append('\b \b' * removed)
            continue
        if ch == '\r' or ch == '\n':
            PENDING_INPUT += '\n'
            out.append('\n')
            continue
        if ch < ' ':
            continue                            # other control keys: ignore
        PENDING_INPUT += ch
        out.append(ch)
    if out:
        try:
            sys.stdout.write(''.join(out))
            sys.stdout.flush()
        except Exception:
            pass


def _enter_cbreak():
    """Single keys (Ctrl+B, type-ahead) must arrive without pressing Enter, so
    the terminal goes into cbreak mode while an operation runs. ISIG stays on:
    Ctrl+C still interrupts and Ctrl+Z still raises SIGTSTP. Returns the saved
    terminal settings (None: not a POSIX TTY)."""
    if os.name == 'nt' or not sys.stdin.isatty():
        return None
    try:
        import termios
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        new = list(termios.tcgetattr(fd))
        new[3] = new[3] & ~(termios.ICANON | termios.ECHO)     # lflag
        new[6] = list(new[6])                                  # cc
        new[6][termios.VMIN] = 1
        new[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSADRAIN, new)
        return old
    except Exception:
        return None


def _leave_cbreak(old):
    if old is None or os.name == 'nt':
        return
    try:
        import termios
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old)
    except Exception:
        pass


def _handle_keys(typed, op, cancelable=True, backgroundable=True):
    """React to what the user typed while an operation runs."""
    stop = STOP_REQUEST.is_set() or '\x1a' in typed        # Ctrl+Z
    background = '\x02' in typed                           # Ctrl+B
    text = typed.replace('\x1a', '').replace('\x02', '')
    if text:
        _type_ahead(text)
    STOP_REQUEST.clear()
    if stop:
        if cancelable and not op.background:
            op.cancel()
        return
    if background and backgroundable:
        op.detach()


def wait_foreground(op, cancelable=True, backgroundable=True):
    """Wait for one operation while staying responsive to the keyboard:
      Ctrl+B -> move it to the background (the prompt comes back),
      Ctrl+Z -> stop it on the client and come back to the prompt,
      anything else -> type-ahead for the next prompt.
    Returns 'done', 'background' (it keeps running as a background job) or
    'dead' (the client is gone)."""
    link = op.link
    STOP_REQUEST.clear()
    _drain_wakeup()
    _drain_op_pipe()
    watch_stdin = os.name != 'nt' and sys.stdin.isatty()
    old_attrs = _enter_cbreak()
    try:
        # op.done: the client answered. op.background: the user moved it to
        # the background (Ctrl+B) or stopped it (Ctrl+Z) - either way the
        # prompt comes back *now* and the operation is handled as a job.
        while not op.done.is_set() and not op.background:
            typed = ''
            if not watch_stdin:
                # No terminal to watch (Windows, or stdin is a pipe): poll the
                # keyboard and the operation's state.
                typed = _poll_keyboard()
                _handle_keys(typed, op, cancelable, backgroundable)
                if op.done.is_set() or op.background or link.dead:
                    break
                time.sleep(0.02)
                continue

            watch = [sys.stdin]
            if OP_R is not None:
                watch.append(OP_R)
            if WAKEUP_R is not None:
                watch.append(WAKEUP_R)
            readable, _, _ = select.select(watch, [], [], 0.2)

            if OP_R is not None and OP_R in readable:
                _drain_op_pipe()
            if WAKEUP_R is not None and WAKEUP_R in readable:
                _drain_wakeup()
                time.sleep(0.001)               # let the Python handler run
            if sys.stdin in readable:
                try:
                    data = os.read(sys.stdin.fileno(), 4096)
                except OSError:
                    data = b''
                if data:
                    typed = data.decode('utf-8', 'replace')
                else:
                    watch_stdin = False         # stdin closed: keep waiting
            _handle_keys(typed, op, cancelable, backgroundable)
    finally:
        _leave_cbreak(old_attrs)

    if link.dead:
        return 'dead'
    if op.background:
        return 'background'
    return 'done'


def ask(link, op, request, cancelable=True, backgroundable=True):
    """Send one request and wait for its answer.

    Returns 'done', 'background' (it is still running - the prompt is free
    again) or 'dead'."""
    request = dict(request)
    request['id'] = op.id
    link.add(op)
    try:
        link.send(request)
    except OSError:
        link.drop(op)
        link.mark_lost()
        return 'dead'
    return wait_foreground(op, cancelable=cancelable, backgroundable=backgroundable)


def _print_jobs(link):
    jobs = link.jobs()
    if not jobs:
        print("[+] Nothing is running in the background.")
        return
    print("[+] Background operations:")
    for index, op in enumerate(jobs, 1):
        print("     {}. {}".format(index, op.describe()))
    print("[+] 'stop' ends the newest one, 'stop all' ends every one.")


def _stop_jobs(link, which=None):
    """End background operations ('stop', 'stop all', 'stop <number>')."""
    jobs = link.jobs()
    if not jobs:
        print("[+] Nothing is running in the background.")
        return
    if which in (None, '', 'last', 'newest'):
        targets = [jobs[-1]]
    elif which == 'all':
        targets = jobs
    else:
        try:
            index = int(which)
        except ValueError:
            print("[!] Usage: stop [all | <job number>]")
            return
        if not 1 <= index <= len(jobs):
            print("[!] No background operation {}".format(index))
            return
        targets = [jobs[index - 1]]
    for op in targets:
        print("[+] Ending: {}".format(op.describe()))
        op.cancel('stop')


def _retire_screenshares(link):
    """Only one live stream fits this controller: end background shares first
    (the client stops the old stream before it starts the new one)."""
    now = time.time()
    for op in link.jobs():
        if op.kind != 'screenshare':
            continue
        if not op.stopping:
            print("[+] Ending the background screen share first...")
            op.cancel('stop')
        elif op.stop_sent_at and (now - op.stop_sent_at) > 5:
            # Its closing note never arrived. Stop waiting for it, or it would
            # swallow the frames of the stream that starts now.
            link.drop(op)


def serve(link):
    """Serve commands until the client disconnects or the user types exit.
    Returns True if the user asked to stop, False if the client disconnected."""
    print("[+] Shell commands run on the client (cd persists now).")
    print("[+] Extra commands:  shell                          (interactive shell on the client)")
    print("[+]                   screenshare [fps] [seconds]   (live view opens in your browser)")
    print("[+]                   screenshot [save_as]           (one picture from the client)")
    print("[+]                   persist | unpersist           (client auto-starts after reboot/logon)")
    print("[+]                   download <client_path> [save_as]  |  upload <local_path>")
    print("[+]                   exit                             (stops the client *and* this controller)")
    print("[+]                   jobs | stop [all | <number>]   (operations in the background)")
    print("[+] Ctrl+B moves a running command / screen share / screenshot to the background.")
    print("[+] Ctrl+Z stops it and gives this prompt back; Ctrl+C leaves this controller.")
    print("[+] The client keeps running and reconnects when this controller is gone.")

    while True:
        if link.dead:
            flush_async()               # show the disconnect note first
            return False
        try:
            line = read_command_line()
        except EOFError:
            return True  # stdin closed -> shut down

        if not line:
            if STOP_REQUEST.is_set():
                # Ctrl+Z pressed at the prompt, then Enter: stop what runs there.
                STOP_REQUEST.clear()
                _stop_jobs(link, None)
            continue
        STOP_REQUEST.clear()

        if line in ('exit', 'quit'):
            op = Op('simple', next_request_id(), label='exit')
            link.add(op)
            try:
                link.send({'type': 'exit', 'id': op.id})
            except OSError:
                link.mark_lost()
                return False
            wait_foreground(op, cancelable=False, backgroundable=False)
            return True

        if line in ('jobs', 'ps'):
            _print_jobs(link)
            continue

        if line == 'stop' or line.startswith('stop '):
            parts = split_args(line)
            _stop_jobs(link, parts[1] if len(parts) > 1 else None)
            continue

        if line.startswith('download '):
            parts = split_args(line)
            if len(parts) < 2:
                print("[!] Usage: download <client_path> [save_as]")
                continue
            if len(parts) > 3:
                print("[!] Too many arguments - wrap paths with spaces in double quotes.")
                continue
            remote_path = parts[1]
            save_as = parts[2] if len(parts) > 2 else None
            op = Op('download', next_request_id(), label='download {}'.format(remote_path),
                    save_as=save_as)
            if ask(link, op, {'type': 'download', 'path': remote_path}) == 'dead':
                return False
            continue

        if line.startswith('upload '):
            parts = split_args(line)
            if len(parts) < 2:
                print("[!] Usage: upload <local_path>")
                continue
            candidates = [parts[1]]
            if len(parts) > 2:      # also accept an unquoted path with spaces
                candidates.append(line.split(maxsplit=1)[1].strip().strip('"\''))
            local_path = next((c for c in candidates if os.path.exists(c)), candidates[0])
            try:
                size = os.path.getsize(local_path)
            except OSError as e:
                print("[!] Could not read local file: {}".format(e))
                continue
            if size > MAX_FILE:
                print("[!] File too large to send (max {} MB).".format(MAX_FILE // (1024 * 1024)))
                continue
            try:
                with open(local_path, 'rb') as f:
                    data = base64.b64encode(f.read()).decode('ascii')
            except OSError as e:
                print("[!] Could not read local file: {}".format(e))
                continue
            name = os.path.basename(local_path)
            op = Op('simple', next_request_id(), label='upload {}'.format(name))
            if ask(link, op, {'type': 'upload', 'name': name, 'data': data}) == 'dead':
                return False
            continue

        if line == 'screenshot' or line.startswith('screenshot '):
            parts = split_args(line)
            save_as = parts[1] if len(parts) > 1 else None
            if save_as and '.' not in os.path.basename(save_as):
                save_as += '.jpg'
            op = Op('screenshot', next_request_id(), label='screenshot', save_as=save_as)
            if ask(link, op, {'type': 'screenshot'}) == 'dead':
                return False
            continue

        if line in ('persist', 'unpersist'):
            op = Op('simple', next_request_id(), label=line)
            if ask(link, op, {'type': line}, cancelable=False, backgroundable=False) == 'dead':
                return False
            continue

        if line == 'shell':
            try:
                if not start_interactive_shell(link):
                    return False
            except OSError as e:
                print("[!] Connection lost: {}".format(e))
                return False
            continue

        if line == 'screenshare' or line.startswith('screenshare '):
            parts = split_args(line)
            fps = None
            seconds = None
            if len(parts) > 1:
                try:
                    fps = int(parts[1])
                except ValueError:
                    fps = None
            if len(parts) > 2:
                try:
                    seconds = float(parts[2])
                    if seconds <= 0:
                        seconds = None
                except ValueError:
                    seconds = None
            _retire_screenshares(link)
            viewer = BrowserViewer()
            try:
                viewer.start()
            except Exception as e:
                print("[!] Could not start the local viewer: {}".format(e))
                continue
            op = Op('screenshare', next_request_id(), label='screenshare')
            op.seconds = seconds
            op.viewer = viewer
            request = {'type': 'screenshare'}
            if fps:
                request['fps'] = fps
            print("[+] Waiting for the first frame from the client...")
            status = ask(link, op, request)
            if status == 'dead':
                viewer.stop()
                return False
            continue

        # anything else is a shell command for the client
        label = line if len(line) <= 60 else line[:57] + '...'
        op = Op('cmd', next_request_id(), label=label)
        if ask(link, op, {'type': 'cmd', 'data': line}) == 'dead':
            return False


def main():
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    # Allow rebinding even when a previous connection is stuck in TIME-WAIT,
    # otherwise restarting quickly fails with "[Errno 98] Address already in use"
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    try:
        server_socket.bind((HOST, PORT))
    except OSError as e:
        print("[!] Could not bind port {}: {}".format(PORT, e))
        print("[!] Another controller is probably still running on this machine.")
        print("[!] Find it with:  pgrep -af controller.py")
        print("[!] Stop it with:  kill <PID>")
        return

    server_socket.listen(5)
    print("[+] Controller v17 listening on port {}".format(PORT))

    # Ctrl+Z normally suspends the process. While an operation runs on the
    # client it must instead act as "stop that", so SIGTSTP is caught here.
    # The self-pipes make that - and every finished operation - take effect
    # immediately instead of on the next poll tick.
    old_tstp = None
    if os.name != 'nt':
        try:
            old_tstp = signal.signal(signal.SIGTSTP, _handle_tstp)
        except (AttributeError, ValueError, OSError):
            old_tstp = None
    _setup_wakeup()
    _setup_op_pipe()

    try:
        # Outer loop: keep accepting clients forever (Ctrl+C or 'exit' to stop)
        while True:
            print("[+] Waiting for a client to connect...")
            client_socket, addr = server_socket.accept()
            try:                            # small replies must not wait for ACKs
                client_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            print("[+] Client connected: {}".format(addr))
            read_hello(client_socket)
            link = ClientLink(client_socket)
            try:
                stop = serve(link)
            finally:
                link.close()
            if stop:
                break
            # client disconnected -> just wait for the next one
    except KeyboardInterrupt:
        print("\n[!] Interrupted by user.")
    finally:
        if WAKEUP_W is not None:
            try:
                signal.set_wakeup_fd(-1)
            except (AttributeError, ValueError, OSError):
                pass
        if old_tstp is not None:
            try:
                signal.signal(signal.SIGTSTP, old_tstp)
            except (AttributeError, ValueError, OSError):
                pass
        for fd in (WAKEUP_R, WAKEUP_W, OP_R, OP_W):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        server_socket.close()
        print("[+] Controller closed.")


if __name__ == "__main__":
    main()
