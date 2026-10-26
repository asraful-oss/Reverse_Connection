# controller.py - v19 (Ctrl+B background operations, Ctrl+Z stops a command,
#                   a screen share or a screenshot; jobs/stop; several clients
#                   as numbered sessions with 'session' / 'connect session N';
#                   'show options' prints a two-column command cheat sheet;
#                   'exit' leaves only this controller (clients reconnect),
#                   'exit session N' / 'exit session all' stop those clients;
#                   macOS / Android(Termux) / iOS-viewer aware; --viewer-lan
#                   lets an iPhone/iPad watch a share in Safari; fast send/receive)

import base64
import http.server
import io
import json
import os
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import textwrap
import threading
import time

HOST = '0.0.0.0'
_port_args = [arg for arg in sys.argv[1:] if not arg.startswith('--')]
try:
    PORT = int(_port_args[0]) if _port_args else 4444
except ValueError:
    print("[!] Invalid port {!r}; using 4444.".format(_port_args[0]))
    PORT = 4444
VIEWER_LAN = ('--viewer-lan' in sys.argv[1:]
              or os.environ.get('SCREEN_VIEWER_LAN') == '1')
MAX_FILE = 50 * 1024 * 1024    # 50 MB
MAX_MSG = 96 * 1024 * 1024     # sanity bound for one protocol message
RECV_STALL_TIMEOUT = 60        # seconds one recv() may stall mid-message
MIN_TRANSFER_RATE = 64 * 1024  # bytes/second a whole transfer must average
TRANSFER_GRACE = 60            # seconds of allowance on top of size / MIN_TRANSFER_RATE
OUTPUT_PRINT_LIMIT = 256 * 1024    # bigger replies: print head+tail, save the rest
OUTPUT_PRINT_PART = 48 * 1024


def recv_exact(conn, n, deadline=None, stall_timeout=None):
    """Read exactly n bytes.

    No timeout (the default) means 'wait for the first byte forever' - an idle
    client is normal. A `deadline` bounds a whole message (the HELLO read);
    `stall_timeout` bounds each wait of a message that has already started, so
    a peer that sends half a message and stops cannot hold the reader thread
    (and therefore the session) forever.

    select() rather than conn.settimeout(): the socket is shared with sender
    threads, and a per-thread timeout would also apply to their send() calls.
    The socket itself stays blocking the whole time."""
    chunks = []
    while n > 0:
        timeout = None
        if deadline is not None:
            timeout = deadline - time.time()
            if timeout <= 0:
                return None
            if stall_timeout is not None:
                timeout = min(timeout, stall_timeout)
        elif stall_timeout is not None:
            timeout = stall_timeout
        if timeout is not None:
            try:
                readable, _, _ = select.select([conn], [], [], timeout)
            except (OSError, ValueError):
                return None
            if not readable:
                return None
        try:
            chunk = conn.recv(min(n, 262144))
        except OSError:
            return None
        if not chunk:
            return None
        chunks.append(chunk)
        n -= len(chunk)
    return b''.join(chunks)


SEND_CHUNK = 256 * 1024          # bounded writes: a stalled peer cannot freeze the prompt
SEND_STALL_TIMEOUT = 5           # seconds a send may make no progress
SEND_TIMEOUT = 2                 # seconds one blocking send() may park in the kernel


def send_msg(conn, obj):
    """Send one length-prefixed JSON message in bounded chunks.

    A 50 MB upload sent as one sendall() could freeze the whole controller on
    a client that stopped reading (the OS retransmits for ~15 minutes). Now
    every chunk re-checks the socket: no progress within SEND_STALL_TIMEOUT
    raises OSError and the caller treats the link as dead."""
    payload = json.dumps(obj).encode('utf-8')
    view = memoryview(len(payload).to_bytes(4, 'big') + payload)
    last_progress = time.time()
    total_deadline = time.time() + TRANSFER_GRACE + len(payload) / float(MIN_TRANSFER_RATE)
    while view:
        if time.time() > total_deadline:
            raise OSError('send timed out: transfer made too little progress')
        try:
            sent = conn.send(view[:SEND_CHUNK])
        except (socket.timeout, TimeoutError, BlockingIOError):
            sent = 0
        if sent:
            view = view[sent:]
            last_progress = time.time()
            continue
        if time.time() - last_progress > SEND_STALL_TIMEOUT:
            raise OSError('send stalled: the client stopped reading')
        time.sleep(0.02)


def recv_msg(conn, deadline=None):
    try:
        header = recv_exact(conn, 4, deadline)
        if header is None:
            return None
        size = int.from_bytes(header, 'big')
        if size > MAX_MSG:              # desynced/garbage stream - reconnect
            return None
        body_deadline = time.time() + TRANSFER_GRACE + size / float(MIN_TRANSFER_RATE)
        if deadline is not None:
            body_deadline = min(body_deadline, deadline)
        payload = recv_exact(conn, size, deadline=body_deadline,
                             stall_timeout=RECV_STALL_TIMEOUT)
        if payload is None:
            return None
        return json.loads(payload.decode('utf-8'))
    except (OSError, ValueError, RecursionError):
        return None


def recv_any(conn):
    """Read one message from the client: a JSON dict, or a screen-share frame
    (marked with b'\\x01') as {'type': 'frame', 'jpeg': ...}. None = the
    connection is gone or the stream is out of sync."""
    try:
        # The first byte may take forever (an idle client is normal). Once a
        # message has started, the remaining bytes have to arrive: a peer that
        # sends a header and then stops must not hold the reader forever.
        first = recv_exact(conn, 1)
        if first is None:
            return None
        rest = recv_exact(conn, 3, deadline=time.time() + 10)
        if rest is None:
            return None
        size = int.from_bytes(first + rest, 'big')
        if size > MAX_MSG:
            return None
        body_deadline = time.time() + TRANSFER_GRACE + size / float(MIN_TRANSFER_RATE)
        payload = recv_exact(conn, size, stall_timeout=RECV_STALL_TIMEOUT,
                             deadline=body_deadline)
        if payload is None:
            return None
        if payload[:1] == b'\x01':
            return {'type': 'frame', 'jpeg': payload[1:]}
        msg = json.loads(payload.decode('utf-8'))
        return msg if isinstance(msg, dict) else None   # garbage -> dead link
    except (OSError, ValueError, RecursionError):
        return None


def _sanitize_env(env):
    """Never trust the JSON a client sent. Non-string scalars become strings
    and 'helpers' must be a dict of flags, so a malformed hello cannot crash a
    printer or the accept thread (it used to: one bad field disabled all
    future connections)."""
    if not isinstance(env, dict):
        return {}
    clean = {}
    for key in ('os', 'distro', 'distro_name', 'session', 'desktop', 'python'):
        value = env.get(key)
        clean[key] = value if isinstance(value, str) else ('' if value is None else str(value))
    helpers = env.get('helpers')
    clean['helpers'] = ({str(k): bool(v) for k, v in helpers.items()}
                        if isinstance(helpers, dict) else {})
    return clean


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


CLIENT_ENV = {}          # environment report of the active client (hello)

# Sessions: every connected client is a numbered session. All of them stay
# connected; commands are sent to the active one and 'connect session N'
# switches. This is controller-side only - the client, the protocol, the
# request ids and the command hot paths are untouched, so nothing gets slower.
SESSIONS = {}            # number -> ClientLink (live sessions)
SESSIONS_COND = threading.Condition()
CURRENT_LINK = [None]    # the session commands are sent to (None = no client)
SESSION_COUNTER = [0]    # monotonic session numbers; a number is never reused
STOPPING = False         # the controller is exiting: no disconnect notices
KEEPALIVE_IDLE = 5       # notice a powered-off client/VM in ~15 seconds
KEEPALIVE_INTERVAL = 3
KEEPALIVE_COUNT = 3

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
    'screencapture': 'screencapture (macOS)',
    'screencap': 'screencap (Android, root)',
}

# Install package name per helper and package-manager family
CAPTURE_PACKAGES = {
    'pacman': {'pillow': 'python-pillow', 'grim': 'grim', 'spectacle': 'spectacle',
               'gnome_screenshot': 'gnome-screenshot', 'imagemagick': 'imagemagick'},
    # Debian 13 / Ubuntu 26.04 renamed the KDE tool package to kde-spectacle.
    'apt': {'pillow': 'python3-pil', 'grim': 'grim', 'spectacle': 'kde-spectacle',
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
    os_name = (env.get('os') or '').lower()
    if os_name in ('ios', 'android'):
        # Stock iOS cannot capture at all; Android needs root. print_client_env
        # explains both in one line instead of a repeated warning block.
        return True
    if os_name == 'linux' and (env.get('session') or '') == 'none':
        return False
    helpers = env.get('helpers') or {}
    if any(helpers.get(k) for k in ('pillow', 'pyautogui', 'grim', 'spectacle',
                                    'gnome_screenshot', 'imagemagick', 'scrot',
                                    'maim', 'screencapture')):
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

    if os_name == 'macos':
        lines.append("macOS screen capture needs Pillow and Screen & System Audio")
        lines.append("Recording permission (called Screen Recording on older macOS).")
        lines.append("On the client:  python3 -m pip install --break-system-packages --user pillow")
        lines.append("Then open System Settings > Privacy & Security > Screen & System")
        lines.append("Audio Recording, enable your terminal app (and Python, if it")
        lines.append("runs as a LaunchAgent), and quit/reopen it.")
        if session == 'ssh':
            lines.append("This client runs over SSH: macOS only captures the logged-in")
            lines.append("desktop, so start it from Terminal on the Mac (or persist it).")
        lines.append("Fallback tool:  /usr/sbin/screencapture -x -m -t jpg <file>")
        return lines

    if os_name == 'ios':
        lines.append("Stock iOS/iPadOS cannot capture the screen from a Python app,")
        lines.append("and an app is suspended about 30 seconds after backgrounding.")
        lines.append("Only a jailbroken device with a full Python + screencapture works.")
        lines.append("On stock iOS use the device's screenshot buttons. To watch a share,")
        lines.append("run controller.py --viewer-lan and open the LAN URL in Safari.")
        return lines

    if os_name == 'android':
        lines.append("Android 9+ blocks screen capture for normal apps (READ_FRAME_BUFFER")
        lines.append("is a system permission). Capture works only with root:")
        lines.append("  - root with Magisk/KernelSU, then allow Termux's su request")
        lines.append("  - install Pillow for JPEG conversion:  pkg install python-pillow")
        lines.append("Without root, use the phone's screenshot buttons or adb screencap.")
        return lines

    if os_name == 'windows':
        lines.append("Screen capture needs Pillow on the Windows client.")
        lines.append("Run on the client:  py -m pip install pillow")
        lines.append("Then run the command again.")
        return lines

    if os_name and os_name != 'linux':
        lines.append("Install Pillow on the client (pip install pillow), then retry.")
        return lines

    if session == 'none':
        lines.append("This is a headless session (DISPLAY / WAYLAND_DISPLAY are not")
        lines.append("set) - normal on Ubuntu Server, RHEL Server and SSH-only boxes.")
        lines.append("Screenshots and screen sharing need a real desktop; commands,")
        lines.append("upload/download and the interactive shell work normally.")
        return lines

    wanted = ['pillow']
    if session == 'wayland':
        if 'kde' in desktop or 'plasma' in desktop:
            wanted.append('spectacle')
        elif 'gnome' in desktop:
            pass          # GNOME 49+ allows only the portal - see the tip below
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
        if family == 'dnf' and distro in ('rhel', 'centos', 'rocky', 'almalinux', 'ol'):
            # Stock RHEL does not carry an epel-release package until EPEL is
            # enabled; point at the official release RPM instead.
            lines.append("RHEL: Pillow, ImageMagick and spectacle are in EPEL. Enable it")
            lines.append("with the epel-release RPM from dl.fedoraproject.org, then rerun.")
        if pkgs and family in PM_COMMAND:
            lines.append("To fix, on the client:  {}".format(PM_COMMAND[family].format(' '.join(pkgs))))
        if 'pillow' in missing:
            lines.append("(without sudo:  python3 -m pip install --break-system-packages pillow)")

    if session == 'wayland':
        if 'kde' in desktop or 'plasma' in desktop:
            lines.append("Tip: KDE captures via spectacle (it ships with Plasma).")
        elif 'gnome' in desktop:
            lines.append("Tip: GNOME 49+ captures only through the desktop portal:")
            lines.append("install python3-gi (Debian/Ubuntu) or python3-gobject (Fedora/RHEL).")
            lines.append("The first capture may ask once: 'Allow screenshots?'")
        else:
            lines.append("Tip: on Wayland, Hyprland/Sway need grim; KDE and GNOME have their own tools.")
    else:
        lines.append("Tip: on X11, Pillow alone is enough (scrot or ImageMagick also work).")

    lines.append("After installing, run the command again - no client restart needed.")
    return lines


def print_capture_fix(diag, out=None):
    """Show what the client tried, plus how to fix it."""
    out = out or sys.stdout
    env = _sanitize_env(diag.get('env') or CLIENT_ENV or {})
    print("[!] Screen capture failed on the client. What was tried:", file=out)
    for attempt in diag.get('attempts') or []:
        print("      - {}".format(attempt), file=out)
    for line in fix_capture_lines(env, diag.get('category')):
        print("    " + line, file=out)


def print_client_error(msg, out=None):
    """One error line, plus the fix block when it was screen capture."""
    out = out or sys.stdout
    data = msg.get('data', '')
    if not isinstance(data, str):
        data = str(data)
    print("[!] Client error: {}".format(data), file=out)
    diag = msg.get('diag') or {}
    if not isinstance(diag, dict):
        diag = {}
    if diag.get('kind') == 'capture':
        print_capture_fix(diag, out)


def relevant_helpers(env):
    """Which capture helpers matter for this client's session and desktop
    (so the connect banner does not list tools that are irrelevant, like
    grim on a GNOME box)."""
    os_name = (env.get('os') or '').lower()
    session = env.get('session') or ''
    desktop = (env.get('desktop') or '').lower()
    if os_name == 'macos':
        return ['pillow', 'screencapture', 'pyautogui']
    if os_name == 'ios':
        return ['pillow', 'screencapture']
    if os_name == 'android':
        return ['pillow', 'screencap']
    keys = ['pillow', 'pyautogui']
    if session == 'wayland':
        if 'kde' in desktop or 'plasma' in desktop:
            keys.append('spectacle')
        elif 'gnome' in desktop:
            pass          # GNOME 49+ captures only through the portal (PyGObject)
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
    os_name = (env.get('os') or '').lower()
    if os_name == 'android':
        print("    Note: screen capture needs root on Android; shell, commands and", file=out)
        print("          files work without it. Android 17+ needs Nearby devices access.", file=out)
    elif os_name == 'ios':
        print("    Note: stock iOS cannot run a background shell or capture the screen;", file=out)
        print("          a jailbreak is required. The live viewer works in Safari.", file=out)
    elif os_name == 'macos' and session == 'ssh':
        print("    Note: screenshots need the logged-in desktop session, not SSH.", file=out)
    elif os_name == 'linux' and session == 'none':
        print("    Note: headless server session - commands, files and the shell work;", file=out)
        print("          there is no screen to capture or share.", file=out)
    if not capture_ok(env) and session != 'none':
        print("[!] Heads-up: screen capture is not ready on this client:", file=out)
        for line in fix_capture_lines(env, None):
            print("    " + line, file=out)


def _ingest_hello(msg, link=None):
    """Store and show the environment report a client sends on connect (and
    again if an older client sends it late). It belongs to that session, not
    to whichever session happens to be active."""
    global CLIENT_ENV
    env = _sanitize_env(msg.get('env') or {})
    try:
        env_text = _capture_text(print_client_env, env)
    except Exception as e:
        env_text = "[!] Bad environment report from the client: {}".format(e)
    if link is not None:
        link.env = env
        if CURRENT_LINK[0] is link:
            CLIENT_ENV = env
        link.say(env_text)
        return
    CLIENT_ENV = env
    _async_print(env_text)


def read_hello(conn):
    """Read the hello a fresh client sends right after connecting and return
    its environment report ({} when an older client sends none). A short
    timeout keeps older clients (which send none) from stalling us, and the
    10 s read budget is for the whole message: a peer sending one byte every
    few seconds used to hold the only accept thread for minutes."""
    try:
        readable, _, _ = select.select([conn], [], [], 1.5)
    except OSError:
        return {}
    if conn not in readable:
        return {}
    try:
        msg = recv_msg(conn, deadline=time.time() + 10)
    except OSError:
        msg = None
    finally:
        try:
            conn.settimeout(None)
        except OSError:
            pass
    if isinstance(msg, dict) and msg.get('type') == 'hello':
        return _sanitize_env(msg.get('env') or {})
    return {}


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
    saved = None
    try:
        # mkstemp: two large outputs in the same second used to overwrite each
        # other (timestamp-only name).
        fd, name = tempfile.mkstemp(prefix='controller_output_',
                                    suffix='.txt', dir=tempfile.gettempdir())
        with os.fdopen(fd, 'w', encoding='utf-8',
                       errors='backslashreplace') as f:
            f.write(text)
        saved = os.path.abspath(name)
    except (OSError, UnicodeError):
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
        data = reply.get('data', '')
        if not isinstance(data, str):
            data = str(data)
        _show_output(data, out)
    elif rtype == 'error':
        print_client_error(reply, out)
    elif rtype == 'file':
        if save_as:
            name = os.path.expanduser(save_as)
        else:
            # The client picks this name: never let it write outside the
            # current directory (absolute paths, "..", backslashes).
            name = os.path.basename(str(reply.get('name') or '').replace('\\', '/'))
            name = name.replace('\x00', '_')
            if name in ('', '.', '..'):
                name = 'downloaded.bin'
            if os.name == 'nt':
                # A hostile client must not choose CON/NUL or an NTFS stream.
                stem = name.split('.', 1)[0].rstrip(' .').upper()
                reserved = {'CON', 'PRN', 'AUX', 'NUL'} \
                    | {'COM{}'.format(i) for i in range(1, 10)} \
                    | {'LPT{}'.format(i) for i in range(1, 10)}
                if stem in reserved or ':' in name:
                    name = '_' + name.replace(':', '_')
                name = name.rstrip(' .') or 'downloaded.bin'
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
            if not os.environ.get('SCREEN_NO_BROWSER') and desktop_available():
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


def _harden_console():
    """Make stdout/stderr able to print anything a client sends.

    JSON strings may contain lone surrogates and a terminal may be ASCII;
    without this, printing such a filename/reply raised UnicodeEncodeError,
    which killed a session (or the whole controller). backslashreplace shows
    the character as an escape instead of losing the rest of the line."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors='backslashreplace')
        except (AttributeError, OSError, ValueError):
            pass


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
    try:
        print(text)
    except UnicodeError:
        # _harden_console() normally makes this impossible; this keeps an
        # unusual redirected/closed stream from killing the reader thread.
        try:
            print(str(text).encode('ascii', 'backslashreplace').decode('ascii'))
        except OSError:
            pass
    except OSError:
        pass                        # stdout pipe closed (e.g. piped to head)


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
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Client screen (live)</title>
<style>
 body { margin:0; background:#101010; color:#ddd; font-family:-apple-system,system-ui,sans-serif; text-align:center; }
 img { display:block; width:100%; max-width:1280px; height:auto; margin:0 auto; }
 button { margin:14px; padding:12px 24px; font-size:16px; border-radius:10px; border:0; background:#2d7ef7; color:#fff; }
 p { margin:0 0 20px; }
</style></head><body>
<h3>Live screen of the client</h3>
<img src="/stream" alt="live screen" onerror="document.querySelector('h3').textContent='Stream ended - you can close this tab';">
<p><button onclick="fetch('/stop').then(()=>{document.querySelector('h3').textContent='stopping...'})">Stop sharing</button></p>
</body></html>
"""


def _lan_ip():
    """The address an iPhone/iPad on the same LAN should use (no packets are
    sent: a UDP connect only selects the default route)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(('192.0.2.1', 9))          # TEST-NET-1
        ip = sock.getsockname()[0]
        if ip and not ip.startswith('127.'):
            return ip
    except OSError:
        pass
    finally:
        sock.close()
    try:
        ip = socket.gethostbyname(socket.gethostname())
        return ip if ip and not ip.startswith('127.') else None
    except OSError:
        return None


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
        self.lan_url = None

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
                                if not viewer.cond.wait_for(
                                        lambda: viewer.seq != last or viewer.stop_requested,
                                        timeout=1.0):
                                    continue    # no new frame: never resend the same one
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

        self.server = http.server.ThreadingHTTPServer(
            ('0.0.0.0' if VIEWER_LAN else '127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        port = self.server.server_address[1]
        self.url = 'http://127.0.0.1:{}/'.format(port)
        if VIEWER_LAN:
            ip = _lan_ip()
            if ip:
                self.lan_url = 'http://{}:{}/'.format(ip, port)
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


def desktop_available():
    """True when opening a browser/file window makes sense on this machine.

    A headless controller (Ubuntu Server, RHEL Server, SSH-only box) has no
    GUI: launching a text browser for an image or a viewer URL can block the
    prompt, so there we only print the path/URL."""
    if os.name == 'nt' or sys.platform == 'darwin':
        return True
    if (os.environ.get('PREFIX') or '').startswith('/data/data/com.termux'):
        return True
    return bool(os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')
                or os.environ.get('BROWSER'))


def open_default_app(target):
    """Open a URL or file with the desktop's default app. False if that failed."""
    if sys.platform == 'darwin':
        commands = [['open', target]]
    elif (os.environ.get('PREFIX') or '').startswith('/data/data/com.termux'):
        commands = [['termux-open', target]]     # Android/Termux
    elif sys.platform.startswith('linux'):
        commands = [['xdg-open', target]]
    else:
        commands = []
    for cmd in commands:
        try:
            # stdin=DEVNULL: a content-sniffed handler (e.g. a text editor for
            # a mislabelled picture) must not eat the controller's input.
            subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except OSError:
            continue
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
    if getattr(viewer, 'lan_url', None):
        print("[+] iPhone/iPad view: {}".format(viewer.lan_url), file=out)
        print("[!] --viewer-lan is on: anyone on this network can watch/stop", file=out)
        print("    this share until it ends. Use it only on a trusted network.", file=out)
    elif VIEWER_LAN:
        print("[!] --viewer-lan: no LAN address found. On the Mac try:", file=out)
        print("    ipconfig getifaddr en0   (then open http://<that-ip>:<port>/)", file=out)
    if VIEWER_LAN and not desktop_available():
        # A server controller has no browser of its own; the viewer's ephemeral
        # port still has to be allowed before another machine can open it.
        try:
            view_port = viewer.url.rsplit(':', 1)[1].rstrip('/')
        except (AttributeError, IndexError):
            view_port = None
        if view_port:
            print("[i] Viewer not reachable from another machine? Allow TCP {}:".format(
                view_port), file=out)
            print("    RHEL: sudo firewall-cmd --add-port={}/tcp".format(view_port),
                  file=out)
            print("    Ubuntu: sudo ufw allow {}/tcp".format(view_port), file=out)
    print("[+] No browser opened? Paste that URL into any browser to watch the live screen.", file=out)
    if not os.environ.get('SCREEN_NO_BROWSER'):
        if desktop_available():
            open_default_app(viewer.url)  # silent attempt to pop the default browser
        elif not VIEWER_LAN:
            print("[i] No desktop here: that URL only works on this machine. Start", file=out)
            print("    controller.py with --viewer-lan to watch from another machine.", file=out)
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
                self.link.say(_capture_text(print_client_error, msg))
            elif not (self.stopping or self.canceled):
                self.link.say(_capture_text(show_reply, msg))
            self._end_stream()
            return True
        if mtype in ('output', 'error') or \
                (mtype == 'file' and self.kind == 'download') or \
                (mtype == 'image' and self.kind == 'screenshot'):
            self.link.say(self._tag(_capture_text(show_reply, msg, self.save_as)))
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
        if not isinstance(jpeg, (bytes, bytearray)):
            # A malformed frame cannot become a JPEG; ignore it instead of
            # handing a list/dict to the browser writer thread.
            return True
        if isinstance(jpeg, bytearray):
            jpeg = bytes(jpeg)
        viewer = self.viewer
        if viewer is None or self.stopping:
            # Frames still in flight from a stream that is stopping. Once the
            # stop is clearly old, let a *newer* screen share take them instead
            # of eating its first frames (which left its view blank/URL never
            # announced).
            if (self.stopping and self.stop_sent_at
                    and (time.time() - self.stop_sent_at) > 1.0
                    and self.link is not None
                    and any(op is not self and op.kind == 'screenshare'
                            and not op.done.is_set() for op in self.link.snapshot())):
                return False
            return True
        if jpeg:
            viewer.feed(jpeg)
        if not self.viewer_announced:
            self.viewer_announced = True
            self.link.say(_capture_text(announce_viewer, viewer, self.seconds))
        if viewer.stop_requested:           # the Stop button in the page
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
            # The id lets the client's "nothing is running" reply find *this*
            # operation; without it the reply could be claimed by a background
            # command that accepts output.
            self.link.send({'type': 'screenshare_stop', 'id': self.id})
        except OSError:
            self.link.mark_lost()

    def _end_stream(self):
        if self.viewer is not None:
            self.viewer.stop()
            self.viewer = None
        if self.frames:
            self.link.say("[+] Screen share ended ({} frames).".format(self.frames))
        self.done.set()

    # -- what the user can do to a running operation -------------------------
    def detach(self):
        """Ctrl+B: keep it running on the client, give the prompt back."""
        if self.background:
            return
        self.background = True
        self.link.say("[+] Moved to the background: {}. It keeps running on the "
                      "client - 'jobs' shows it, 'stop' ends it.".format(self.label))
        if self.kind == 'screenshare':
            self.link.say("[+] The live view keeps updating. 'stop' (or Ctrl+Z then "
                          "Enter) ends the screen share.")

    def cancel(self, why='Ctrl+Z'):
        """Stop this operation on the client and give the prompt back."""
        if self.canceled:
            return
        self.canceled = True
        self.background = True          # the prompt is not waiting for it any more
        what = 'the screen share' if self.kind == 'screenshare' else 'this operation'
        self.link.say("[+] {}: stopping {} on the client...".format(why, what))
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

    def __init__(self, conn, addr=None):
        self.conn = conn
        self.addr = addr
        self.number = None              # session number, assigned on accept
        self.env = {}                   # this client's hello (for 'session')
        self.on_dead = None             # registry hook: this session went away
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

    def say(self, text):
        """Print a note from this session, tagged when another session is active."""
        if not text:
            return
        current = CURRENT_LINK[0]
        if self.number is not None and current is not None and current is not self:
            first, sep, rest = text.partition('\n')
            text = '[session {}] {}{}{}'.format(self.number, first, sep, rest)
        _async_print(text)

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
        if notify and not STOPPING:
            try:
                self.say("[!] Client disconnected.")
            except Exception:
                pass            # a closed stdout must not break bookkeeping
        _wake_main()
        if self.on_dead is not None:
            try:
                self.on_dead(self)
            except Exception as e:
                _async_print("[!] Internal error while updating the session list: {}".format(e))

    def close(self):
        """Shut the connection down without calling it a surprise."""
        self.mark_lost(note=False)
        try:
            self.conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.conn.close()
        except OSError:
            pass

    # -- the reader thread -----------------------------------------------------
    def _read_loop(self):
        while True:
            try:
                msg = recv_any(self.conn)
            except Exception:
                # A parser failure (RecursionError from hostile JSON, ...) must
                # retire the link, never leave a zombie session whose reader
                # thread is gone and whose commands can never be answered.
                msg = None
            if msg is None:
                self.mark_lost()
                return
            try:
                if msg.get('type') == 'hello':      # a late environment report
                    _ingest_hello(msg, self)
                    continue
                consumed = False
                for op in self.snapshot():
                    try:
                        if op.handle(msg):
                            consumed = True
                            break
                    except Exception as e:
                        self.say("[!] Internal error while handling a client message: {}".format(e))
                if not consumed:
                    self.say(_stray_message(msg))
                    continue
                for op in self.snapshot():
                    if op.done.is_set():
                        self.drop(op)
                _wake_main()
            except Exception as e:
                # Whatever happens next, this link must not become a zombie:
                # without mark_lost() the session stayed in the list, no other
                # session could take over, and every later command waited for
                # an answer that could never come.
                try:
                    self.say("[!] Internal error while reading from a client: {}".format(e))
                except Exception:
                    pass
                self.mark_lost()
                return


# ---------------------------------------------------------------------------
# Session bookkeeping (controller side only). Commands are only ever sent to
# the active session, so an inactive client's reader thread just waits in
# recv() - no polling, no timers, nothing that could slow a command down.
# ---------------------------------------------------------------------------

def _fmt_addr(addr):
    if isinstance(addr, tuple) and len(addr) >= 2:
        return '{}:{}'.format(addr[0], addr[1])
    return str(addr) if addr else '?'


def _active_link():
    return CURRENT_LINK[0]


def _next_session_number():
    with SESSIONS_COND:
        SESSION_COUNTER[0] += 1
        return SESSION_COUNTER[0]


def _session_env_label(link):
    """A short one-line description of a session's machine."""
    env = link.env or {}
    name = env.get('distro_name') or env.get('os') or 'unknown'
    distro = env.get('distro') or ''
    label = '{} ({})'.format(name, distro) if distro else str(name)
    session = env.get('session') or ''
    desktop = env.get('desktop') or ''
    if session == 'wayland' and desktop:
        label += ' - wayland [{}]'.format(desktop)
    elif session and session not in ('none', 'desktop', 'android', 'ios'):
        label += ' - {}'.format(session)
    return label


def _register_link(link):
    """Add an accepted link to the session list. Returns True when it became
    the active session (the caller's prompt then shows the help)."""
    global CLIENT_ENV
    became_active = False
    with SESSIONS_COND:
        if link.dead or link.number is None:
            return False
        SESSIONS[link.number] = link
        if CURRENT_LINK[0] is None:
            CURRENT_LINK[0] = link
            CLIENT_ENV = link.env or {}
            became_active = True
        SESSIONS_COND.notify_all()
    return became_active


def _link_died(link):
    """A session's connection ended: drop it and make another one active."""
    global CLIENT_ENV, PENDING_INPUT
    if STOPPING:
        return
    replacement = None
    with SESSIONS_COND:
        if SESSIONS.get(link.number) is link:
            del SESSIONS[link.number]
        if CURRENT_LINK[0] is link:
            # Oldest *live* session: another one that died but whose callback
            # is still waiting for the lock must not be picked (the user would
            # see a bogus "now active" for a dead session).
            replacement = next((SESSIONS[n] for n in sorted(SESSIONS)
                                if not SESSIONS[n].dead), None)
            CURRENT_LINK[0] = replacement
            if replacement is not None:
                CLIENT_ENV = replacement.env or {}
            else:
                CLIENT_ENV = {}
        SESSIONS_COND.notify_all()
    if PENDING_LINK[0] is link:
        # Commands typed while this session was running belong to it. Running
        # them on whichever session takes over could hit the wrong machine.
        had_pending = bool(PENDING_INPUT)
        PENDING_INPUT = ''
        PENDING_LINK[0] = None
        if had_pending:
            _async_print("[i] Discarded a command typed for session {} - it "
                         "disconnected.".format(link.number))
    if replacement is not None:
        _async_print("[+] Session {} is now active ({}). "
                     "'session' lists all sessions.".format(
                         replacement.number, _fmt_addr(replacement.addr)))


def _wait_for_session():
    """Block until a client is connected.

    While waiting, complete lines are read as they are typed: 'exit'/'quit'
    leave the controller right away (before, they sat in the terminal queue and
    caused a surprise shutdown once a client connected), and any other command
    is queued for that client instead of being replayed as if just typed.
    Returns (link, leave); leave=True means the user typed exit/quit or
    stdin was closed."""
    global WAIT_FRAGMENT
    while True:
        eof = False
        with SESSIONS_COND:
            if CURRENT_LINK[0] is not None:
                # A partial line typed before the client connected was never
                # entered; do not let it merge with a later command.
                WAIT_FRAGMENT = ''
                return CURRENT_LINK[0], False
            # The timeout keeps Ctrl+C responsive on Windows consoles; it never
            # runs while commands are being served.
            SESSIONS_COND.wait(0.2)
            lines = _poll_waiting_lines()
            if lines is None:
                eof = True
                lines = []
        if eof:
            print("[i] Standard input closed - leaving the controller "
                  "(clients keep running).")
            return None, True
        # Print/queue outside the lock: a slow terminal must never block the
        # accept thread that wants to register a new session.
        for line in lines:
            text = line.strip()
            if text in ('exit', 'quit'):
                return None, True
            if _handle_local_command(text):
                continue
            if text:
                QUEUED_COMMANDS.append(text)


def _print_sessions():
    with SESSIONS_COND:
        current = CURRENT_LINK[0]
        rows = [(number, SESSIONS[number]) for number in sorted(SESSIONS)
                if not SESSIONS[number].dead]
    if not rows:
        print("[+] No sessions connected.")
        return
    print("[+] Sessions:")
    for number, link in rows:
        marker = 'active  ' if link is current else '        '
        print("     {}. {} {:<36} {:>22}  jobs: {}".format(
            number, marker, _session_env_label(link),
            _fmt_addr(link.addr), len(link.jobs())))
    print("[+] 'connect session <number>' (or 'session <number>') switches to that session.")


def _switch_session(number):
    """Make session `number` active. Returns (link, error_text)."""
    global CLIENT_ENV
    with SESSIONS_COND:
        link = SESSIONS.get(number)
        if link is None:
            return None, "[!] No session {}. Type 'session' to list them.".format(number)
        if link.dead:
            return None, "[!] Session {} just disconnected. Type 'session' to list them.".format(number)
        if CURRENT_LINK[0] is link:
            return link, None
        CURRENT_LINK[0] = link
        CLIENT_ENV = link.env or {}
    return link, None


def _send_exit_request(link, number):
    """Ask one client to stop for good (its reconnect loop then ends). Returns
    the Op, or None when the connection dropped while sending."""
    op = Op('simple', next_request_id(), label='exit session {}'.format(number))
    link.add(op)
    try:
        link.send({'type': 'exit', 'id': op.id})
    except OSError:
        link.mark_lost()
        return None
    return op


def _wait_stopped(pairs, timeout):
    """Wait (bounded - a wedged client must never park the prompt) until every
    (link, op) pair stopped. 'Stopped' means the client acknowledged the exit
    (its goodbye output) or the connection already ended. Returns the count."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(link.dead or op.done.is_set() for link, op in pairs):
            break
        time.sleep(0.05)
    return sum(1 for link, op in pairs if link.dead or op.done.is_set())


def _settle_dying_prompt(link):
    """After a stop request the served session may still be closing: give the
    EOF a moment (bounded - this never hangs) so the prompt is not left
    reading from a dying connection and eating the next command."""
    if link.dead:
        return
    deadline = time.monotonic() + 1.5
    while not link.dead and time.monotonic() < deadline:
        time.sleep(0.02)


def _stop_all_sessions(timeout=3.0):
    """'exit session all': stop every connected client. The controller itself
    keeps running and waits for new connections."""
    with SESSIONS_COND:
        targets = [(number, SESSIONS[number]) for number in sorted(SESSIONS)
                   if not SESSIONS[number].dead]
    if not targets:
        print("[+] No sessions connected.")
        return
    waiting = []
    for number, link in targets:
        op = _send_exit_request(link, number)
        if op is not None:
            waiting.append((link, op))
    stopped = _wait_stopped(waiting, timeout)
    print("[+] {}/{} client(s) stopped - they will not reconnect. This "
          "controller stays running ('exit' or Ctrl+C leaves it).".format(
              stopped, len(targets)))


def _shutdown_sessions():
    """Close every session on exit (clients then reconnect on their own)."""
    global STOPPING
    STOPPING = True
    with SESSIONS_COND:
        links = list(SESSIONS.values())
        SESSIONS.clear()
        CURRENT_LINK[0] = None
        SESSIONS_COND.notify_all()
    for link in links:
        link.close()


def _tune_link_socket(sock):
    """Low latency and dead-client detection (a powered-off VM sends no FIN).

    Without keepalives a session whose machine vanished would look alive
    forever - and a foreground command would wait for a reply that can never
    arrive. This only sets socket options; it costs nothing per command."""
    # select() only promises that *some* buffer space is free; the blocking
    # send() after it can still park inside the kernel for ~15 minutes when the
    # peer stopped reading. A short send timeout turns that into a clean
    # OSError (the caller drops the link) and leaves recv semantics alone.
    try:
        if os.name == 'nt':
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO,
                            SEND_TIMEOUT * 1000)              # Windows: milliseconds
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO,
                            struct.pack('ll', SEND_TIMEOUT, 0))   # POSIX: timeval
    except OSError:
        pass
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        return
    # Each knob independently: macOS has TCP_KEEPALIVE (idle) plus
    # TCP_KEEPINTVL/TCP_KEEPCNT, Linux has TCP_KEEPIDLE+INTVL+CNT. The old
    # if/elif left a vanished Mac with the default 75 s interval and count 8,
    # so its commands waited ~10 minutes instead of ~15 seconds.
    if hasattr(socket, 'TCP_KEEPIDLE'):            # Linux
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, KEEPALIVE_IDLE)
        except OSError:
            pass
    if hasattr(socket, 'TCP_KEEPALIVE'):           # macOS (legacy name for idle)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPALIVE, KEEPALIVE_IDLE)
        except OSError:
            pass
    if hasattr(socket, 'TCP_KEEPINTVL'):
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, KEEPALIVE_INTERVAL)
        except OSError:
            pass
    if hasattr(socket, 'TCP_KEEPCNT'):
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, KEEPALIVE_COUNT)
        except OSError:
            pass
    if os.name == 'nt':
        try:
            sock.ioctl(socket.SIO_KEEPALIVE_VALS,
                       (1, KEEPALIVE_IDLE * 1000, KEEPALIVE_INTERVAL * 1000))
        except (AttributeError, OSError):
            pass


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


def _shell_backspace_byte(client_os):
    """The byte the client's PTY erases with: 0x7f on Unix, 0x08 on Windows."""
    if client_os in ('linux', 'macos', 'ios', 'android'):
        return b'\x7f'
    return b'\x08'


def _shell_stdout(data):
    """Portable stdout write - survives redirected stdout."""
    try:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    except Exception:
        os.write(sys.stdout.fileno(), data)


def _shell_session_unix(conn, client_os=None):
    """Raw-mode pipeline: this terminal <-> the client's PTY (shellcontrol.py).

    client_os lets Backspace use the byte the destination PTY expects: a Unix
    PTY normally erases with 0x7f, a Windows console with 0x08."""
    import termios
    import tty

    fd = sys.stdin.fileno()
    try:
        old = termios.tcgetattr(fd)
    except termios.error as e:                  # not a terminal for termios
        print("\n[!] The shell needs a real terminal: {}".format(e))
        return
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
                if client_os == 'windows':
                    data = data.replace(b'\x7f', _shell_backspace_byte('windows'))
                elif client_os:
                    data = data.replace(b'\x08', _shell_backspace_byte(client_os))
                conn.sendall(data)
            if conn in readable:
                data = conn.recv(65536)
                if not data:                    # shell exited on the client
                    break
                _shell_stdout(data)
    except (KeyboardInterrupt, OSError, termios.error):
        pass
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except (OSError, termios.error):
            pass


def _shell_session_windows(conn, client_os=None):
    """Windows console equivalent: read keys with msvcrt, print the replies.

    Enter is sent as a lone CR (ICRNL turns it into one NL on a Unix client,
    and it is Enter on a Windows console). Backspace is 0x7f for Unix clients
    and 0x08 for Windows ones."""
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
                # One CR: ICRNL makes it one NL on Unix, and it is Enter on
                # Windows. Sending CRLF submitted an extra empty line on Unix.
                conn.sendall(b'\r')
            elif ch in ('\x08', '\x7f'):        # Backspace
                conn.sendall(_shell_backspace_byte(client_os))
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


def run_shell_session(conn, client_os=None):
    """Terminal-size handshake, then pipe this terminal to the client's PTY."""
    try:
        _tune_link_socket(conn)             # a vanished client errors in ~15 s
        rows, cols = get_terminal_size()
        conn.sendall(struct.pack('!HH', rows, cols))
    except OSError:
        print("[!] The client's shell connection went away.")
        return
    if os.name == 'nt':
        _shell_session_windows(conn, client_os)
    else:
        _shell_session_unix(conn, client_os)


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
                candidate, addr = listener.accept()
                if addr[0] != link.addr[0]:
                    # Only the machine that owns the control link may use the
                    # shell channel; close anyone else and keep waiting.
                    try:
                        candidate.close()
                    except OSError:
                        pass
                    print("[!] Refused a shell connection from {} - not the "
                          "client's address ({}).".format(addr[0], link.addr[0]))
                    continue
                conn = candidate

        client_os = ((link.env or {}).get('os') or '').lower()
        print("[+] Shell connected from {} - everything you type now goes to".format(addr))
        if client_os == 'windows':
            print("    the client's shell. Type 'exit' there to come back "
                  "(Ctrl+] forces it closed).")
        else:
            print("    the client's shell. Type 'exit' (or Ctrl+D) there to come back.")
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        SHELL_ACTIVE = True                 # keep background notes off the screen
        try:
            run_shell_session(conn, client_os)
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
PENDING_LINK = [None]             # which session that type-ahead was meant for
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


def _poll_keyboard(raw=False):
    """On Windows: the keyboard text typed since the last call ('' = none).
    On POSIX nothing is read here - the wait loop reads the terminal itself
    (cbreak mode), so keys such as Ctrl+B arrive without pressing Enter.
    raw=True leaves backspaces in the text for the caller's line buffer."""
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
            if ch == '\x08' and not raw:    # backspace (raw: caller keeps it)
                if chars:
                    chars.pop()
                continue
            if ch == '\r':
                ch = '\n'
            chars.append(ch)
        return ''.join(chars)
    except OSError:
        return ''


QUEUED_COMMANDS = []              # commands typed while waiting for a client
WAIT_FRAGMENT = ''                # partial line from _poll_waiting_lines


def _poll_waiting_lines():
    """Complete lines typed while no client is connected, without blocking.

    POSIX reads stdin only when select() says a line arrived (the terminal
    echoes it, and canonical mode hands over finished lines only). Windows
    reads msvcrt, whose characters the console has already echoed. A partial
    line stays buffered until its Enter arrives."""
    global WAIT_FRAGMENT
    if os.name == 'nt':
        chunk = _poll_keyboard(raw=True)
    else:
        try:
            if not select.select([sys.stdin], [], [], 0)[0]:
                return []
            chunk = os.read(sys.stdin.fileno(), 4096).decode('utf-8', 'replace')
        except (OSError, ValueError, AttributeError):
            return []
        if chunk == '':
            # EOF on stdin (Ctrl+D at an empty prompt, /dev/null, a closed
            # pipe). Without this the wait loop spun forever and only left
            # once a client connected.
            return None
    if not chunk:
        return []
    for ch in chunk:
        if ch in ('\x7f', '\x08'):
            WAIT_FRAGMENT = WAIT_FRAGMENT[:-1]
        elif ch == '\r':
            WAIT_FRAGMENT += '\n'
        else:
            WAIT_FRAGMENT += ch
    lines = WAIT_FRAGMENT.split('\n')
    WAIT_FRAGMENT = lines.pop()
    return lines


def read_command_line(link=None):
    """input('controller> '), but type-ahead caught while an operation was
    running - and commands typed while waiting for a client - are used first,
    so nothing the user typed is lost.

    Type-ahead belongs to the session that was active when it was typed: if
    that session died before the prompt returned, the line is discarded
    instead of being executed on the replacement session."""
    global PENDING_INPUT, PROMPT_ACTIVE
    flush_async()                       # show what the background produced
    if QUEUED_COMMANDS:
        text = QUEUED_COMMANDS.pop(0)
        print("controller> {}".format(text))
        return text
    while PENDING_INPUT:
        line, sep, rest = PENDING_INPUT.partition('\n')
        if not sep:
            # Only an unfinished line (no Enter) typed while an operation ran
            # and then stopped with Ctrl+Z/Ctrl+B. Ctrl+Z is not Enter: never
            # run half a command by surprise.
            PENDING_INPUT = ''
            PENDING_LINK[0] = None
            print("[i] Ignored an unfinished command line: {!r}".format(line))
            break
        origin = PENDING_LINK[0]
        PENDING_INPUT = rest
        if not rest:
            PENDING_LINK[0] = None
        text = line.rstrip('\r').strip()
        if origin is not None and link is not None and origin is not link:
            print("[i] Discarded a command typed for session {} - it "
                  "disconnected, so it was not sent to session {}.".format(
                      origin.number, link.number))
            continue
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


def _type_ahead(text, link=None):
    """Echo what the user types while an operation runs and keep it for the
    next prompt. The terminal is in cbreak mode, so it does not echo itself -
    and escape sequences (arrow keys, function keys) are swallowed.

    `link` records which session this text was typed for, so a session that
    dies cannot cause its queued command to run on another machine."""
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
    if not PENDING_INPUT:
        PENDING_LINK[0] = None
    elif PENDING_LINK[0] is None and link is not None:
        PENDING_LINK[0] = link
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
        _type_ahead(text, op.link)
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
        # link.dead: the session died in the moment between ask() adding this
        # op and this loop starting - without it the prompt waited forever.
        while not op.done.is_set() and not op.background and not link.dead:
            typed = ''
            if not watch_stdin:
                # No terminal to watch (Windows, or stdin is a pipe): poll the
                # keyboard and the operation's state.
                # raw=True: a Backspace often arrives in a later 20 ms poll
                # than the character it erases, so it must be applied to the
                # real PENDING_INPUT buffer by _type_ahead, not to this batch.
                typed = _poll_keyboard(raw=True)
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
            # swallow the frames of the stream that starts now. Its viewer
            # server must be stopped too, or the thread/socket leak.
            if op.viewer is not None:
                op.viewer.stop()
                op.viewer = None
            link.drop(op)


OPTION_ROWS = [
    ('session', 'List every connected client: number, OS and address.'),
    ('session <N>', 'Switch to client N (the number shown by "session").'),
    ('connect session <N>', 'Older name for "session <N>" - same switch.'),
    ('cd <dir>', "Change the client's working directory; it persists."),
    ('<shell command>', "Run it on the client, in that OS's own shell."),
    ('download <client_path> [save_as]', 'Copy a file from the client to this machine.'),
    ('upload <local_path>', 'Copy a file from this machine to the client.'),
    ('screenshot [save_as]', "Take one picture of the client's screen."),
    ('screenshare [fps] [secs]', 'Live view in your browser; Ctrl+B leaves it running.'),
    ('shell', 'Interactive shell on the client: type "exit" to come back '
              '(Ctrl+D on Unix; Ctrl+Z then Enter in cmd.exe), or Ctrl+] to '
              'force it closed.'),
    ('jobs | stop [all|<N>]', 'List or stop operations running in the background.'),
    ('persist | unpersist', 'Client starts again after reboot/logon; undo with unpersist.'),
    ('exit | quit', 'Leave the CONTROLLER only - clients keep running and '
                    'reconnect when it is back.'),
    ('exit session <N>', 'Stop client N for good - it exits and will NOT reconnect.'),
    ('exit session all', 'Stop every client for good; this controller keeps running.'),
    ('Ctrl+B', 'Move a running command / screenshot / share to the background.'),
    ('Ctrl+Z', 'Stop the running command / screenshot / share; prompt comes back.'),
    ('Ctrl+C', 'Leave the controller (clients keep running, same as exit).'),
    ('show options', 'This cheat sheet of every feature and its syntax.'),
]

SHELL_ROWS = [
    ('Windows client', 'Know Windows terminal commands: dir, type, copy, del, cd, where.'),
    ('Linux client', 'Know Linux shell commands: ls, cat, cp, rm, pwd, chmod, grep.'),
    ('Ubuntu/RHEL Server', 'Same Linux commands; no screen to capture (headless).'),
    ('macOS client', 'Know macOS commands: ls, cat, cp, rm, open, pbcopy '
                     '(BSD userland; plain commands run via /bin/sh, not zsh).'),
    ('Android client', 'Know Termux commands: ls, cat, cp, rm, termux-open. '
                       'Android 17+ Play build: allow Nearby devices.'),
    ('iOS client', 'a-Shell/iSH/Pyto: ls, cat, cp, rm (app sandbox); '
                   'Pythonista cannot run subprocesses.'),
]


def _print_table(rows, left_width=None):
    """Commands on the left, a short explanation on the right. The right
    column wraps to the terminal width (100 columns when it is not a TTY)."""
    try:
        columns = shutil.get_terminal_size(fallback=(100, 24)).columns
    except (OSError, ValueError):
        columns = 100
    needed = max(len(left) for left, _right in rows) if rows else 0
    left_width = max(left_width or 0, needed)
    right_width = max(28, max(60, columns) - 2 - left_width - 2)
    for left, right in rows:
        parts = textwrap.wrap(right, right_width) or ['']
        print('  {:<{}}  {}'.format(left, left_width, parts[0]))
        for part in parts[1:]:
            print('  {}  {}'.format(' ' * left_width, part))


def _print_options():
    """The 'show options' cheat sheet: every feature on the left, how to use
    it on the right, plus which client shell plain commands run in."""
    print()
    print('[+] Show options - every controller command, and how to use it')
    print()
    _print_table(OPTION_ROWS)
    print()
    print("[+] Plain commands are run by the CLIENT's own terminal/shell,")
    print('    so use the commands of that operating system:')
    print()
    _print_table(SHELL_ROWS, left_width=14)
    print()
    print('[+] After "exit session all" the controller keeps waiting; "exit" leaves it.')
    print('[+] The banner after "Client connected" shows the client OS and desktop.')
    print('[+] Ubuntu/RHEL Server: files, commands and the shell work; no screen capture.')
    print(r'[+] Quote paths with spaces: "C:\Program Files\app".')
    print('[+] iPhone/iPad: start the controller with --viewer-lan, then open its URL.')
    print()


def _handle_local_command(text):
    """The one controller-only phrase. Everything else (including 'options'
    and 'help') is a normal shell command for the client. True when handled."""
    if text.strip().lower() == 'show options':
        _print_options()
        return True
    return False


def _print_help():
    print("[+] Type 'show options' for a two-column cheat sheet of every")
    print("    command, plus the terminal commands of each client OS.")
    print("[+] Shell commands run on the client (cd persists now).")
    print("[+] Extra commands:  shell                          (interactive shell on the client)")
    print("[+]                   screenshare [fps] [seconds]   (live view opens in your browser)")
    print("[+]                   screenshot [save_as]           (one picture from the client)")
    print("[+]                   persist | unpersist           (client auto-starts after reboot/logon)")
    print("[+]                   download <client_path> [save_as]  |  upload <local_path>")
    print("[+]                   session                          (all connected clients)")
    print("[+]                   session <number>                 (switch to that client)")
    print("[+]                   connect session <number>         (same thing, older name)")
    print("[+]                   jobs | stop [all | <number>]   (operations in the background)")
    print("[+]                   exit session <number> | all    (stop that client - it will not reconnect)")
    print("[+]                   exit | quit                      (leave this controller; clients keep reconnecting)")
    print("[+] Ctrl+B moves a running command / screen share / screenshot to the background.")
    print("[+] Ctrl+Z stops it and gives this prompt back; Ctrl+C also leaves this controller.")
    print("[+] Clients keep retrying every second until stopped with 'exit session ...'.")
    print("[+] iPhone/iPad: start with  --viewer-lan  and open the LAN URL in Safari.")


def serve(link):
    """Serve the active session until it goes away, the user switches to
    another session, or the user leaves with 'exit'. Returns True to stop the
    controller, False to let the caller pick the (possibly changed) session."""
    while True:
        if link.dead or _active_link() is not link:
            flush_async()               # show the disconnect note first
            return False
        try:
            line = read_command_line(link)
        except EOFError:
            return True  # stdin closed -> shut down

        if not line:
            if STOP_REQUEST.is_set():
                # Ctrl+Z pressed at the prompt, then Enter: stop what runs there.
                STOP_REQUEST.clear()
                _stop_jobs(link, None)
            continue
        STOP_REQUEST.clear()

        # The session may have ended (or another one may have become active)
        # while the prompt was waiting for Enter. Never send that command to
        # a different machine by accident.
        if link.dead or _active_link() is not link:
            if _active_link() is None:
                print("[!] The active session disconnected while you were typing - "
                      "that command was not run. Waiting for a client...")
            else:
                print("[!] The active session changed while you were typing - that "
                      "command was not run. Please type it again.")
            return False

        if _handle_local_command(line):
            continue                # options/help are answered here, never sent

        if line in ('exit', 'quit'):
            # Controller-only, exactly like Ctrl+C: every client stays alive
            # and keeps reconnecting. 'exit session ...' is what stops clients.
            print("[+] Leaving the controller - every client keeps running and "
                  "reconnects when this controller is back.")
            return True

        if line.startswith(('exit session', 'quit session')):
            parts = split_args(line)
            target = parts[2] if len(parts) == 3 else None
            if target is None:
                print("[!] Usage: exit session <number> | exit session all   "
                      "('session' lists them)")
                continue
            if target.lower() == 'all':
                _stop_all_sessions()
                _settle_dying_prompt(link)
                continue
            if not target.isdigit():
                print("[!] '{}' is not a session number - use one from 'session', "
                      "or 'exit session all'.".format(target))
                continue
            number = int(target)
            with SESSIONS_COND:
                stopped_link = SESSIONS.get(number)
            if stopped_link is None or stopped_link.dead:
                print("[!] No session {}. Type 'session' to list them.".format(number))
                continue
            op = _send_exit_request(stopped_link, number)
            if op is None:
                print("[!] Session {} disconnected while it was being stopped.".format(number))
                continue
            if _wait_stopped([(stopped_link, op)], 3.0):
                print("[+] Session {} stopped - its client exited and will not "
                      "reconnect.".format(number))
            else:
                print("[+] Stop requested for session {} - its client will exit "
                      "as soon as it can.".format(number))
            if stopped_link is link:
                _settle_dying_prompt(link)
            continue

        # -- sessions (controller-side, never sent to the client) -----------
        if line == 'session':
            _print_sessions()
            continue

        if line.startswith('session '):
            # 'session <number>' switches directly after 'session' listed them.
            parts = split_args(line)
            target = parts[1] if len(parts) > 1 else ''
            if len(parts) == 2 and target.isdigit():
                new_link, error = _switch_session(int(target))
                if error:
                    print(error)
                    continue
                if new_link is link:
                    print("[+] Already on session {}.".format(target))
                    continue
                print("[+] Active session is now {} - {} ({}).".format(
                    int(target), _session_env_label(new_link), _fmt_addr(new_link.addr)))
                return False            # the caller serves the new session
            print("[!] Usage: session <number>   ('session' lists them)")
            continue

        if line == 'connect' or line.startswith('connect '):
            parts = line.split()
            target = None
            if len(parts) >= 2 and parts[1].lower() == 'session':
                if len(parts) != 3:
                    print("[!] Usage: connect session <number>   ('session' lists them)")
                    continue
                target = parts[2]
            elif len(parts) == 2 and parts[1].isdigit():
                target = parts[1]           # 'connect 2' works too
            elif len(parts) == 1:
                print("[!] Usage: connect session <number>   ('session' lists them)")
                continue
            if target is not None:
                try:
                    number = int(target)
                except ValueError:
                    print("[!] '{}' is not a session number. Type 'session' to "
                          "list them.".format(target))
                    continue
                new_link, error = _switch_session(number)
                if error:
                    print(error)
                    continue
                if new_link is link:
                    print("[+] Already on session {}.".format(number))
                    continue
                print("[+] Active session is now {} - {} ({}).".format(
                    number, _session_env_label(new_link), _fmt_addr(new_link.addr)))
                return False            # the caller serves the new session
            # any other 'connect ...' is just a shell command for the client

        if line in ('jobs', 'ps'):
            _print_jobs(link)
            continue

        if line == 'stop' or line.startswith('stop '):
            parts = split_args(line)
            _stop_jobs(link, parts[1] if len(parts) > 1 else None)
            continue

        if line == 'download' or line.startswith('download '):
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

        if line == 'upload' or line.startswith('upload '):
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
            # A POSIX basename keeps backslashes, but a Windows client treats
            # them as separators ('..\\..\\evil.dll' escaped its current dir).
            name = os.path.basename(local_path.replace('\\', '/'))
            if name in ('', '.', '..'):
                name = 'uploaded.bin'
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


def _accept_loop(server_socket):
    """Accept every client that connects; each one becomes a numbered session.

    Nothing a client sends may kill this thread: it is the only thread that
    accepts connections, so one malformed hello used to disable every future
    session for the lifetime of the controller."""
    while True:
        try:
            client_socket, addr = server_socket.accept()
        except OSError:
            return                      # the controller is shutting down
        link = None
        try:
            _async_print("[+] Client connected: {}".format(addr))
            _tune_link_socket(client_socket)
            env = read_hello(client_socket)
            link = ClientLink(client_socket, addr)
            link.number = _next_session_number()
            link.env = env
            link.on_dead = _link_died
            try:
                env_text = _capture_text(print_client_env, env)
            except Exception as e:
                env_text = "[!] Bad environment report from the client: {}".format(e)
            if env_text:
                link.say(env_text)
            if link.dead:
                continue                # vanished before it could be used
            became_active = _register_link(link)
            if link.dead:
                continue
            if became_active:
                continue                # the prompt shows the help right away
            current = _active_link()
            _async_print("[+] Session {} is ready (active: session {}). "
                         "'session {}' switches to it.".format(
                             link.number, current.number if current else '?', link.number))
        except Exception as e:
            _async_print("[!] Dropped a bad connection from {}: {}".format(addr, e))
            if link is not None:
                link.close()
            else:
                try:
                    client_socket.close()
                except OSError:
                    pass


def repl():
    """The prompt. It survives sessions coming and going; with no client it
    waits, watching stdin so 'exit' works and typed commands are queued."""
    waiting_printed = True              # main() printed the first one
    help_done = False
    while True:
        link = _active_link()
        if link is None:
            if not waiting_printed:
                print("[+] Waiting for a client to connect...")
            waiting_printed = True
            link, leave = _wait_for_session()
            if leave:
                print("[+] Leaving the controller - every client keeps running "
                      "and reconnects when this controller is back.")
                return True
            _print_help()
            help_done = True
        elif not help_done:
            # a client connected before this loop got going
            _print_help()
            help_done = True
        waiting_printed = False         # a session is served: announce next idle
        if serve(link):
            return True


def _ipv4_interfaces():
    """name -> IPv4 address for the local interfaces (POSIX only, {} on
    Windows); interfaces that are down are left out."""
    if os.name == 'nt':
        return {}
    try:
        import fcntl
    except ImportError:
        return {}
    result = {}
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for _index, name in socket.if_nameindex():
            try:
                with open('/sys/class/net/{}/operstate'.format(name)) as f:
                    if f.read().strip() == 'down':
                        continue        # e.g. a dormant docker0
            except OSError:
                pass                    # no sysfs (macOS): keep it
            try:
                data = fcntl.ioctl(probe.fileno(), 0x8915,     # SIOCGIFADDR
                                   struct.pack('256s', name[:15].encode('ascii')))
                result[name] = socket.inet_ntoa(data[20:24])
            except OSError:
                continue
    except OSError:
        pass
    finally:
        probe.close()
    return result


def local_ipv4_addresses():
    """The machine's non-loopback IPv4 addresses; the routed one comes first
    (that is the address a client on the LAN should use)."""
    addrs = []

    def add(ip):
        if ip and not ip.startswith('127.') and ip not in addrs:
            addrs.append(ip)

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # No packet is sent: the kernel only picks the outgoing source address.
        probe.connect(('8.8.8.8', 53))
        add(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            add(info[4][0])
    except OSError:
        pass
    return addrs


def _local_distro_id():
    """This machine's distro ID ('' when unknown, e.g. Windows/macOS)."""
    try:
        with open('/etc/os-release') as f:
            for line in f:
                if line.startswith('ID='):
                    return line.split('=', 1)[1].strip().strip('"').lower()
    except OSError:
        pass
    return ''


def _print_connect_hint(port):
    """Print the exact address another machine (or a VM) should connect to.

    Most 'it does not connect' reports are a wrong address or VM networking:
    a NAT guest reaches the host, but the host needs a NAT port-forward (or
    Bridged/Host-only) to reach the guest."""
    names = _ipv4_interfaces()
    addrs = local_ipv4_addresses()
    if names:
        # Only addresses of interfaces that are actually up (skips docker0...).
        addrs = [ip for ip in addrs if ip in names.values()]
    if not addrs:
        print("[i] No LAN address found - check 'ip addr' (Linux) / 'ipconfig' "
              "(Windows) before connecting from another machine.")
        return
    by_ip = {addr: name for name, addr in names.items()}
    print("[+] Start the client on the other machine (or VM) with one of these:")
    for ip in addrs:
        name = by_ip.get(ip)
        note = '   ({})'.format(name) if name else ''
        print("        python client.py {} {}{}".format(ip, port, note))
    print("[i] VMware/VirtualBox: a NAT guest reaches this machine on the NAT")
    print("    address above; for THIS machine to reach a NAT guest, add a port")
    print("    forward (Virtual Network Editor) or use Bridged/Host-only mode.")
    print("[i] Still failing? This machine's firewall must allow TCP {} inbound:".format(port))
    distro = _local_distro_id()
    if os.name == 'nt':
        print("    Windows: allow python.exe on private networks when Defender asks.")
    elif sys.platform == 'darwin':
        print("    macOS: allow incoming connections when the firewall asks.")
    elif (os.environ.get('PREFIX') or '').startswith('/data/data/com.termux'):
        print("    Termux: allow Nearby devices (Android 17+), or LAN is blocked.")
    elif distro == 'omarchy':
        print("    Omarchy enables ufw (deny incoming):  sudo ufw allow {}/tcp".format(port))
    elif distro in ('ubuntu', 'debian', 'kali', 'linuxmint', 'lmde', 'pop',
                    'zorin', 'elementary', 'raspbian', 'devuan'):
        print("    Ubuntu/Debian: only if ufw is enabled (it is off by default):")
        print("    sudo ufw allow {}/tcp".format(port))
    elif distro in ('fedora', 'nobara'):
        print("    Fedora Workstation allows ports >= 1025; Fedora Server does not.")
        print("    If firewalld is blocking it:")
        print("    sudo firewall-cmd --permanent --add-port={}/tcp && "
              "sudo firewall-cmd --reload".format(port))
    elif distro in ('rhel', 'centos', 'rocky', 'almalinux', 'ol'):
        print("    RHEL Server: firewalld is ON and blocks this port by default:")
        print("    sudo firewall-cmd --permanent --add-port={}/tcp && "
              "sudo firewall-cmd --reload".format(port))
    elif distro in ('arch', 'manjaro', 'endeavouros', 'garuda', 'cachyos', 'artix'):
        print("    Arch: no firewall by default; with ufw:  sudo ufw allow {}/tcp".format(port))
    else:
        print("    Linux: open TCP {} in ufw/firewalld.".format(port))


def main():
    _harden_console()
    if sys.platform == 'ios':
        try:
            import termios  # noqa: F401
        except ImportError:
            print("[-] Stock iOS/iPadOS cannot run controller.py: Python apps there")
            print("    have no real terminal (no termios/readline) and are suspended")
            print("    in the background.")
            print("    Run the controller on a Mac/Linux/Windows machine and connect")
            print("    from your iPhone/iPad with an SSH app (Termius, Blink, ...).")
            print("    To watch a live screen in Safari, start it with --viewer-lan")
            print("    and open the printed LAN URL.")
            return

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

    server_socket.listen(32)
    print("[+] Controller v19 listening on port {}".format(PORT))
    _print_connect_hint(PORT)
    print("[i] Type 'show options' at any prompt for the full command cheat sheet.")
    if (os.environ.get('PREFIX') or '').startswith('/data/data/com.termux'):
        print("[i] Termux: Android 17+ needs the Nearby devices permission for LAN;")
        print("    disable battery optimization/phantom trimming to stay reachable.")
    if VIEWER_LAN:
        print("[i] --viewer-lan: live shares will be reachable on this LAN (Safari on")
        print("    iPhone/iPad); anyone on the network can watch while sharing.")

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
        print("[+] Waiting for a client to connect...")
        acceptor = threading.Thread(target=_accept_loop, args=(server_socket,),
                                    name='session-acceptor')
        acceptor.daemon = True
        acceptor.start()
        repl()
    except KeyboardInterrupt:
        print("\n[!] Interrupted by user.")
    finally:
        _shutdown_sessions()
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
        try:
            server_socket.shutdown(socket.SHUT_RDWR)   # wake the accept thread
        except OSError:
            pass
        server_socket.close()
        print("[+] Controller closed.")


# Applied at import too, so even the first client-supplied string is safe
# (main() calls it again; this also covers tests and embedders).
_harden_console()


if __name__ == "__main__":
    main()
