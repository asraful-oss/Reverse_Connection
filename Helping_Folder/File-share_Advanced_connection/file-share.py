#!/usr/bin/env python3
"""
file-share.py - two-way file and folder sharing over any connection
-------------------------------------------------------------------
Run on BOTH computers with:   python file-share.py

One program does everything: it listens for the other user AND connects to
them, so it does not matter who started first or who runs the tunnel.

  * Same Wi-Fi / LAN: one user puts the other user's LAN IP in PEER_ADDRESS.
  * Internet: one user runs Pinggy or ngrok on port 4444 (TCP, HTTP or
    HTTPS tunnel); the other user pastes the address the tunnel printed into
    PEER_ADDRESS. The user who runs the tunnel can leave PEER_ADDRESS empty.

The connection type (raw TCP, TLS, WebSocket over HTTP/HTTPS, or plain HTTP
polling as a last resort) is detected automatically. Interrupted transfers
resume where they stopped, even after a tunnel expires or a restart.

Commands once connected:
  upload <files/folders>    offer one or more files/folders, any type and size
                            (upload a.mp4 b.zip "My Folder" *.jpg)
  Ctrl+Z   (or cancel)      pick which transfers to stop: 2, or 2 3 4, or all,
                            then confirm once with y/n; the connection stays
  cancel 2 3                stop transfers 2 and 3 (numbers shown by status)
  connect <address>         switch to a new address (e.g. a new tunnel URL)
  status                    connection and every transfer with its number
  help                      show the commands
  exit     (or Ctrl+C)      quit

Uploads and downloads run at the same time in both directions. You can
upload more while others are still running; the other user is asked y/n
once per upload command.

Cross-platform: Windows, macOS, Linux, Android (Termux), iOS (a-Shell,
Pythonista). Python 3.6+. Standard library only.
"""

import hashlib
import json
import os
import platform
import random
import re
import select
import shutil
import socket
import ssl
import struct
import sys
import tempfile
import threading
import time
import zlib
import base64
import glob
import http.client as http_client
from collections import deque

# ============================ EDIT THIS ============================
# The OTHER user's address. Leave '' if the other user connects to you
# (for example, you are the one running the Pinggy/ngrok tunnel).
# Accepts: 192.168.0.113 | 192.168.0.113:4444 | tcp://x.a.free.pinggy.link:41234
#          https://abcd.a.free.pinggy.link | https://abcd.ngrok-free.app
#          tcp://0.tcp.in.ngrok.io:12345 | any host, host:port or URL
PEER_ADDRESS = ''

PORT = 4444            # local port this program listens on (tunnel to this port)
DOWNLOAD_FOLDER = ''   # '' = a "FileShare Downloads" folder next to this script
# ===================================================================

# Optional overrides without editing the file:
#   python file-share.py <peer address>
#   environment variables FILESHARE_PEER, FILESHARE_PORT, FILESHARE_DIR

# Built-in TLS identity: lets two copies talk TLS directly on a LAN. A copy
# only trusts this exact certificate for that; public sites (Pinggy/ngrok
# HTTPS) are still verified normally.
_TLS_CERT = """-----BEGIN CERTIFICATE-----
MIIDNzCCAh+gAwIBAgIUAeA6SNShoQx/dNNRe93vuufF4DMwDQYJKoZIhvcNAQEL
BQAwFjEUMBIGA1UEAwwLY21vbi1zZW5kZXIwIBcNMjYxMDIxMTk0OTM3WhgPMjEy
NjA5MjcxOTQ5MzdaMBYxFDASBgNVBAMMC2Ntb24tc2VuZGVyMIIBIjANBgkqhkiG
9w0BAQEFAAOCAQ8AMIIBCgKCAQEApIxbuJpV9TJxfccLXMhnlu23XIuYKfK0l+xN
CbFip2yYpWJjoTUU2cYSE0mBy+cLK7J8KqvNLZMbubo2l9JojGH5HtltWCLcTghH
XAMeu5o5C7LmYiyCSiUR3m2/4YOIQUc2YWRw7rmRw8rYc21P4i1v4H91xUhObFqL
im9uJQvcZlnV8g816qBYMmZt7U+jl9/hdhV7AcJTg6EitD6d34WP4IzjFmIdqBre
h+MhOeUnq9OXhzrCJJbMJEPHFWNG/wJLTTZKEfUSiiEJP24s9EsaLf5+IitgX/ni
llwGEgxnAfjUl56TnHbBNYZ/w7qp8DIyScbE5obcaahCh3BmRwIDAQABo3sweTAd
BgNVHQ4EFgQUmtaLkXOnoyUUk4Xh4bgByxoiWqEwHwYDVR0jBBgwFoAUmtaLkXOn
oyUUk4Xh4bgByxoiWqEwDwYDVR0TAQH/BAUwAwEB/zAOBgNVHQ8BAf8EBAMCAqQw
FgYDVR0RBA8wDYILY21vbi1zZW5kZXIwDQYJKoZIhvcNAQELBQADggEBAF/4peGm
YRR7LQ9qJCZW2SZHKF5QJsrhNEpnmwosJUKarEqC5tUyJFM/wMeve+JgWiaMrTle
tBrAR49b7FNbQtUBZJdqAWaTeTVjZvkssMUpENaSTpLt7G6YmRXJ3OzkjteCZcQx
lYluQSbjc8SQo06zUYARbTQliMh+6B3Tq+tgZJ19NRirEfotyoATLNFpoIpwhFuI
VZsD3SJHCbf2P7gVEN5u1CfpYDT2owQTQxs4ELyBiZar54piVqPaOZYT26lnlPkq
xjCEGQUSr/7xPy92uCsdBqakIHLPb4nfAbt53iCOmxq+moXa5zNhEcDmyZTXlP73
To5MF7KGcoHCp9M=
-----END CERTIFICATE-----"""
_TLS_KEY = """-----BEGIN PRIVATE KEY-----
MIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKgwggSkAgEAAoIBAQCkjFu4mlX1MnF9
xwtcyGeW7bdci5gp8rSX7E0JsWKnbJilYmOhNRTZxhITSYHL5wsrsnwqq80tkxu5
ujaX0miMYfke2W1YItxOCEdcAx67mjkLsuZiLIJKJRHebb/hg4hBRzZhZHDuuZHD
ythzbU/iLW/gf3XFSE5sWouKb24lC9xmWdXyDzXqoFgyZm3tT6OX3+F2FXsBwlOD
oSK0Pp3fhY/gjOMWYh2oGt6H4yE55Ser05eHOsIklswkQ8cVY0b/AktNNkoR9RKK
IQk/biz0Sxot/n4iK2Bf+eKWXAYSDGcB+NSXnpOcdsE1hn/DuqnwMjJJxsTmhtxp
qEKHcGZHAgMBAAECggEAALive5hhoW4+hSNRmhne1RCRvzwgHKmjFQ9vfwgMu1iE
is/ASWf8LBWW5V1rI5kNuXv82LW2yT5d3sCW61omzzFsGtsv2YpoQNypw24tZ6Z+
HYkD7ZDtuU7UTI9an1h+SRr7fWMoA5lDWa0Sqd7kJ2+05TJp3RrmDR+QgQ41WMA6
FZ1hdN+1QlZ1pJKpO8+8k4Jru2oEZUvG4Nc64nq4aBibVunbEPz1r7SZoXDAViT8
r0TnJP/aBAAEg/V7lO6pFpwBLsyG2SNqEkshaA9nrqAF/Pm+bugw8XezPMeznPds
3jZlB2VM1LkAcZIcQPATZFSAlQrmcBELKIYxni3tEQKBgQDlMDzei1fvGJNptzwF
NLI/N6WK98NzB356K6mNMHkr+inMMBZl9USRHioXZDgATQx+EAcDXaIGWOHA9Fft
Uszju5HWA5Y0ySkJy4uRvaOe1334xSKi5V6i8eLQPvvKS5dEyk271NturMbs+1qG
MYeqWKgY9WdU4kH++TwOh1+XxQKBgQC3zET+5WGgeASiIJyfigPNlfwNlR1WZjag
Xc9wWouid8Jt/c052mj51IZ0iq20BmVJwGYMOx9A+gHM12v2fqjIS2ADH7Zf1CU9
RiAg6jNSsF4bLINJusIuloArdDzWzKj0zgFNz4lT1uOnK2xHoolshTiBre3b2fcm
4dqN82eamwKBgQDD1xlcTROh0o08m3+lLydVD7bcbeTMaU3xFVg3toEETCrlOP4a
t5+UpYLwJAoab7xKCGYFwT/PTqnx2Jk9yTAdNU1j5AK5Ll9r6EtmCPdEpv0U01dK
fNcGQ809h2Res4MaHAiahUPQacE+BXaQDsjPjhxt310h0oj0WIKyI4lSxQKBgQCv
YbPUpnzp6pxBOG07ry7B1gSV0VwVPaOSkvGGfGARv8U/v5c9RTQ2cJ18R3hI+HFe
VsEJmsDP98AP/t9+bo4P0hzj3jbgaka+mVO/FLu0g6bIt0jv6c2xNttGm7wSG2h9
cLW90fNwlPTWZ5EDKg2IB/dLhvQZfXthFtK98S1DUQKBgEdkixQIFMvfQ/7JLgN8
4syHME+8xH5B3DEpDPU1cKPW/2n70D2dPWQG2pelUDyyGhb3x72lwx0OHBGsTdoR
tAChgZmt/zrSxC/Si9hS5DQSlH2k5zKieQ4X5AqzK1DwmziJShv50GD7TQ/KLWlS
zP3pNXne29C+KThc0CXc5mW0
-----END PRIVATE KEY-----"""

APP_NAME = "FileShare"
APP_VERSION = 1
MAGIC = b"FSHR1\n"                 # first bytes each side sends on every connection
F_CTRL, F_DATA, F_PING = 1, 2, 3   # frame types
HDR = struct.Struct(">BI")         # frame header: type, payload length
DATA_HDR = struct.Struct(">IQI")   # data frame: transfer id, offset, crc32
MAX_FRAME = 64 * 1024 * 1024
CHUNK = 512 * 1024                 # file bytes per data frame
RECV_SIZE = 1024 * 1024
SOCK_BUF = 4 * 1024 * 1024
DATA_QUEUE_MAX = 4 * 1024 * 1024   # queued (not yet written) file bytes per connection
WS_MAX_FRAME = 1024 * 1024

PING_INTERVAL = 3.0                # send a heartbeat after this much send silence (s)
DEAD_AFTER = 15.0                  # nothing received this long = connection is dead (s)
POLL_PING_INTERVAL = 10.0          # HTTP polling fallback: fewer requests
POLL_DEAD_AFTER = 45.0
POLL_WAIT = 15.0                   # long-poll hold time (s)
POLL_BATCH = 4 * 1024 * 1024
POLL_GIVE_UP = 30.0
CONNECT_TIMEOUT = 8.0
HANDSHAKE_TIMEOUT = 10.0
SNIFF_TIMEOUT = 10.0
HTTP_IDLE = 60.0
PROGRESS_EVERY = 0.5
HEALTHY_WINDOW = 5.0
RETRY_MAX = 5.0
KEEPALIVE_IDLE, KEEPALIVE_INTVL, KEEPALIVE_CNT = 20, 5, 3
MAX_STREAMS = 3                    # uploads streaming at once; the rest wait their turn
MAX_BATCH = 1000                   # items per upload command (a folder counts as one item)
GROUP_WAIT = 1.5                   # wait this long for the rest of a multi-file offer (s)

TUNNEL_HEADERS = {
    "User-Agent": "file-share/1",
    "X-Pinggy-No-Screen": "1",            # Pinggy free: skip the browser warning
    "ngrok-skip-browser-warning": "1",    # ngrok free: skip the interstitial
    "Cache-Control": "no-cache",
}
MARK_HEADER = "X-FShare"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

INSTANCE_ID = os.urandom(8).hex()
try:
    HOSTNAME = re.sub(r"[^\w-]", "", (socket.gethostname() or "").split(".")[0])[:40] or "other user"
except Exception:
    HOSTNAME = "other user"
IS_WINDOWS = os.name == "nt"
IS_IOS = sys.platform == "ios" or platform.machine().startswith(("iPhone", "iPad", "iPod"))


# ======================================================================
# Small helpers
# ======================================================================
def human(n):
    n = float(max(0, n))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.2f %s" % (n, unit))
        n /= 1024.0
    return "%d B" % n


def fmt_eta(sec):
    if sec is None or sec < 0 or sec > 360000:
        return "--:--"
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return ("%d:%02d:%02d" % (h, m, s)) if h else ("%02d:%02d" % (m, s))


def short(name, width=24):
    return name if len(name) <= width else name[:width - 3] + "..."


def lan_ips():
    ips = []
    for target in ("8.8.8.8", "1.1.1.1"):
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((target, 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
        except OSError:
            pass
        finally:
            if s is not None:
                s.close()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    return ips[:3]


def is_private_host(host):
    h = host.lower()
    if h in ("localhost",) or h.endswith(".local") or h.endswith(".lan"):
        return True
    try:
        import ipaddress
        ip = ipaddress.ip_address(h)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except Exception:
        return False


_WIN_RESERVED = set(["CON", "PRN", "AUX", "NUL"] + ["COM%d" % i for i in range(1, 10)]
                    + ["LPT%d" % i for i in range(1, 10)])
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_component(part):
    part = _BAD_CHARS.sub("_", str(part)).strip().rstrip(". ")
    if part in ("", ".", ".."):
        part = "_"
    if part.split(".")[0].upper() in _WIN_RESERVED:
        part = "_" + part
    if len(part) > 180:
        root, ext = os.path.splitext(part)
        part = root[:180 - len(ext[:20])] + ext[:20]
    return part


def safe_relpath(rel):
    parts = [p for p in re.split(r"[\\/]+", str(rel)) if p not in ("", ".")]
    if not parts:
        return "_"
    return "/".join(safe_component(p) for p in parts)


def unique_path(folder, name):
    path = os.path.join(folder, name)
    if not os.path.lexists(path):
        return path
    root, ext = os.path.splitext(name)
    if root == "" or ext == name:
        root, ext = name, ""
    i = 1
    while True:
        cand = os.path.join(folder, "%s (%d)%s" % (root, i, ext))
        if not os.path.lexists(cand):
            return cand
        i += 1


def clean_path_arg(arg):
    """Accept typed, quoted, or drag-and-dropped paths on every platform."""
    s = arg.strip()
    if s.startswith("& "):                       # PowerShell drag-and-drop: & 'C:\x y'
        s = s[2:].strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1]
    if s.startswith("file://"):
        try:
            from urllib.parse import unquote
            s = unquote(s[7:])
            if IS_WINDOWS and re.match(r"^/[A-Za-z]:", s):
                s = s[1:]
        except Exception:
            pass
    s = os.path.expanduser(s)
    if not os.path.exists(s) and not IS_WINDOWS and "\\" in s:
        unescaped = re.sub(r"\\(.)", r"\1", s)    # macOS/Linux drag: My\ File.mp4
        if os.path.exists(unescaped):
            s = unescaped
    return s


def parse_paths(arg):
    """Split an upload argument into paths: quoted, escaped, dragged, several, or wildcards.
    Returns (existing paths, missing names)."""
    s = arg.strip()
    if not s:
        return [], []
    one = clean_path_arg(s)
    if os.path.exists(one):
        return [one], []
    if s.startswith("& "):
        s = s[2:].strip()
    if IS_WINDOWS:
        pat = r'"([^"]*)"|\'([^\']*)\'|(\S+)'
    else:
        pat = r'"([^"]*)"|\'([^\']*)\'|((?:\\.|\S)+)'
    toks = []
    for m in re.finditer(pat, s):
        if m.group(1) is not None:
            toks.append((m.group(1), True))
        elif m.group(2) is not None:
            toks.append((m.group(2), True))
        else:
            toks.append((m.group(3), False))
    found, missing = [], []
    i = 0
    while i < len(toks):
        text, quoted = toks[i]
        p = clean_path_arg(text)
        if os.path.exists(p):
            found.append(p)
            i += 1
            continue
        hit = None
        if not quoted:                     # unquoted name with spaces: join the next words
            j, acc = i, text
            while j + 1 < len(toks) and not toks[j + 1][1] and j - i < 30:
                j += 1
                acc = acc + " " + toks[j][0]
                cand = clean_path_arg(acc)
                if os.path.exists(cand):
                    hit = (cand, j)
                    break
        if hit is not None:
            found.append(hit[0])
            i = hit[1] + 1
            continue
        if any(c in p for c in "*?["):
            matches = sorted(glob.glob(p))
            if matches:
                found.extend(matches)
                i += 1
                continue
        missing.append(p)
        i += 1
    seen, out = set(), []
    for p in found:
        k = os.path.normcase(os.path.abspath(p))
        if k not in seen:
            seen.add(k)
            out.append(p)
    return out, missing


def lp(path):
    """Windows: allow paths longer than 260 characters (deep folders)."""
    if not IS_WINDOWS:
        return path
    path = os.path.abspath(path)
    if path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


def disk_free(path):
    try:
        return shutil.disk_usage(path).free
    except Exception:
        return None


class Meter(object):
    """Transfer speed over the last few seconds."""
    def __init__(self):
        self.samples = deque()

    def add(self, total):
        now = time.monotonic()
        self.samples.append((now, total))
        while len(self.samples) > 2 and now - self.samples[0][0] > 6.0:
            self.samples.popleft()

    def rate(self):
        if len(self.samples) < 2:
            return 0.0
        (t0, b0), (t1, b1) = self.samples[0], self.samples[-1]
        return (b1 - b0) / (t1 - t0) if t1 > t0 else 0.0

    def reset(self):
        self.samples.clear()


# ======================================================================
# Console: messages, one live status line, and the input line
# ======================================================================
class Console(object):
    def __init__(self):
        self.lock = threading.RLock()
        try:
            self.tty = sys.stdout.isatty()
        except Exception:
            self.tty = False
        self.prompt = "> "
        self.status = ""
        self.typed = ""
        self.drawn = 0
        self.last_plain_status = 0.0
        try:
            sys.stdout.reconfigure(errors="replace")
        except Exception:
            pass

    def _out(self, s):
        try:
            sys.stdout.write(s)
            sys.stdout.flush()
        except UnicodeEncodeError:
            try:
                enc = sys.stdout.encoding or "ascii"
                sys.stdout.write(s.encode(enc, "replace").decode(enc, "replace"))
                sys.stdout.flush()
            except Exception:
                pass
        except (OSError, ValueError):
            pass

    def _width(self):
        try:
            return max(20, shutil.get_terminal_size((80, 24)).columns - 1)
        except Exception:
            return 79

    def _line(self):
        width = self._width()
        right = self.prompt + self.typed
        if len(right) > width:
            keep = max(1, width - len(self.prompt) - 1)
            right = self.prompt + "<" + self.typed[-keep:]
        left = self.status
        if left:
            avail = width - len(right) - 3
            left = (left[:avail] + " | ") if avail >= 12 else ""
        return left + right

    def _clear(self):
        if self.tty and self.drawn:
            self._out("\r" + " " * self.drawn + "\r")
        self.drawn = 0

    def _draw(self):
        if not self.tty:
            return
        line = self._line()
        pad = max(0, self.drawn - len(line))
        self._out("\r" + line + (" " * pad + "\r" + line if pad else ""))
        self.drawn = len(line)

    def say(self, text):
        with self.lock:
            self._clear()
            self._out(text + "\n")
            if self.tty:
                self._draw()

    def set_prompt(self, prompt, announce=False):
        with self.lock:
            self.prompt = prompt
            if self.tty:
                self._clear()
                self._draw()
            elif announce:
                self._out(prompt + "\n")

    def set_status(self, text):
        with self.lock:
            if text == self.status:
                return
            self.status = text
            if self.tty:
                self._draw()
            elif text and time.monotonic() - self.last_plain_status > 5.0:
                self.last_plain_status = time.monotonic()
                self._out(text + "\n")

    def add_char(self, ch):
        with self.lock:
            self.typed += ch
            line = self._line()
            if self.tty and len(line) == self.drawn + len(ch) and line.endswith(self.typed):
                self._out(ch)
                self.drawn = len(line)
            else:
                self._draw()

    def set_typed(self, text):
        with self.lock:
            self.typed = text
            self._draw()

    def line_done(self, echo_newline):
        with self.lock:
            if echo_newline and self.tty:
                self._out("\n")
            self.typed = ""
            self.drawn = 0
            if self.tty:
                self._draw()


CONSOLE = Console()


def say(text):
    CONSOLE.say(text)


# ======================================================================
# Keyboard input: our own line editor so Ctrl+Z works on every OS
# ======================================================================
class KeyReader(object):
    """Returns one typed character at a time (or None after a short wait)."""
    def __init__(self):
        self.mode = "line"
        self.fd = None
        self.saved = None
        self.decoder = None
        self.pending = deque()
        try:
            interactive = sys.stdin.isatty()
        except Exception:
            interactive = False
        if not interactive or IS_IOS:
            return
        if IS_WINDOWS:
            try:
                import msvcrt
                self.msvcrt = msvcrt
                self.mode = "win"
            except ImportError:
                pass
            return
        try:
            import termios
            import codecs
            self.termios = termios
            self.fd = sys.stdin.fileno()
            self.saved = termios.tcgetattr(self.fd)
            new = termios.tcgetattr(self.fd)
            new[3] = new[3] & ~(termios.ICANON | termios.ECHO)   # keep ISIG: Ctrl+C/Ctrl+Z signals
            new[6][termios.VMIN] = 1
            new[6][termios.VTIME] = 0
            termios.tcsetattr(self.fd, termios.TCSADRAIN, new)
            self.decoder = codecs.getincrementaldecoder(sys.stdin.encoding or "utf-8")("replace")
            self.mode = "posix"
        except Exception:
            self.restore()
            self.mode = "line"

    def restore(self):
        if self.saved is not None and self.fd is not None:
            try:
                self.termios.tcsetattr(self.fd, self.termios.TCSADRAIN, self.saved)
            except Exception:
                pass
            self.saved = None

    def get(self, timeout=0.1):
        if self.pending:
            return self.pending.popleft()
        if self.mode == "win":
            end = time.monotonic() + timeout
            while True:
                if self.msvcrt.kbhit():
                    return self.msvcrt.getwch()
                if time.monotonic() >= end:
                    return None
                time.sleep(0.015)
        try:
            r, _, _ = select.select([self.fd], [], [], timeout)
        except (OSError, ValueError):
            return None
        if not r:
            return None
        data = os.read(self.fd, 1024)
        if not data:
            raise EOFError
        for ch in self.decoder.decode(data):
            self.pending.append(ch)
        return self.pending.popleft() if self.pending else None


class LineEditor(object):
    def __init__(self, on_ctrl_z):
        self.keys = KeyReader()
        self.on_ctrl_z = on_ctrl_z

    def close(self):
        self.keys.restore()

    def _skip_escape(self, first):
        if first in ("\x00", "\xe0"):          # Windows function/arrow key prefix
            self.keys.get(0.05)
            return
        ch = self.keys.get(0.05)               # ESC [ ... final byte
        if ch in ("[", "O"):
            for _ in range(8):
                ch = self.keys.get(0.05)
                if ch is None or ("@" <= ch <= "~" and ch not in "[;"):
                    return

    def readline(self):
        if self.keys.mode == "line":
            line = input()
            CONSOLE.line_done(False)
            return line
        typed = ""
        while True:
            ch = self.keys.get(0.1)
            if ch is None:
                continue
            if ch in ("\r", "\n"):
                CONSOLE.line_done(True)
                return typed
            if ch == "\x03":
                raise KeyboardInterrupt
            if ch == "\x1a":
                self.on_ctrl_z()
                continue
            if ch == "\x04" and not typed:
                raise EOFError
            if ch in ("\x08", "\x7f"):
                typed = typed[:-1]
                CONSOLE.set_typed(typed)
                continue
            if ch == "\x15":
                typed = ""
                CONSOLE.set_typed(typed)
                continue
            if ch in ("\x1b", "\x00", "\xe0"):
                self._skip_escape(ch)
                continue
            if ch == "\t":
                ch = " "
            if ch < " ":
                continue
            typed += ch
            CONSOLE.add_char(ch)


# ======================================================================
# Sockets
# ======================================================================
def _setopt(sock, level, name, value):
    code = getattr(socket, name, None)
    if code is None:
        return False
    try:
        sock.setsockopt(level, code, value)
        return True
    except OSError:
        return False


def tune_socket(sock):
    _setopt(sock, socket.IPPROTO_TCP, "TCP_NODELAY", 1)
    _setopt(sock, socket.SOL_SOCKET, "SO_KEEPALIVE", 1)
    _setopt(sock, socket.SOL_SOCKET, "SO_SNDBUF", SOCK_BUF)
    _setopt(sock, socket.SOL_SOCKET, "SO_RCVBUF", SOCK_BUF)
    system = platform.system()
    if system == "Darwin":
        try:
            sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, "TCP_KEEPALIVE", 0x10), KEEPALIVE_IDLE)
        except OSError:
            pass
    else:
        _setopt(sock, socket.IPPROTO_TCP, "TCP_KEEPIDLE", KEEPALIVE_IDLE)
    _setopt(sock, socket.IPPROTO_TCP, "TCP_KEEPINTVL", KEEPALIVE_INTVL)
    _setopt(sock, socket.IPPROTO_TCP, "TCP_KEEPCNT", KEEPALIVE_CNT)
    if system == "Windows" and hasattr(socket, "SIO_KEEPALIVE_VALS"):
        try:
            sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, KEEPALIVE_IDLE * 1000, KEEPALIVE_INTVL * 1000))
        except (OSError, ValueError, AttributeError):
            pass


def close_sock(sock):
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except (OSError, ValueError):
        pass
    try:
        sock.close()
    except (OSError, ValueError):
        pass


def make_listener(port):
    last = None
    families = []
    if socket.has_ipv6:
        families.append((socket.AF_INET6, ("::", port)))
    families.append((socket.AF_INET, ("0.0.0.0", port)))
    for family, addr in families:
        srv = None
        try:
            srv = socket.socket(family, socket.SOCK_STREAM)
            if IS_WINDOWS:
                _setopt(srv, socket.SOL_SOCKET, "SO_EXCLUSIVEADDRUSE", 1)
            else:
                _setopt(srv, socket.SOL_SOCKET, "SO_REUSEADDR", 1)
            if family == socket.AF_INET6:
                try:
                    srv.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                except (OSError, AttributeError):
                    pass
            srv.bind(addr)
            srv.listen(16)
            return srv
        except OSError as exc:
            last = exc
            if srv is not None:
                srv.close()
    raise last or OSError("cannot listen on port %s" % port)


class NotUs(Exception):
    """Something answered, but it is not file-share."""


class Busy(Exception):
    pass


class PortDown(Exception):
    """Nothing accepts connections on that host:port."""


class SoftEOF(NotUs):
    """Accepted, then closed at once: a tunnel whose app behind it is not ready yet,
    or a server that silently drops a protocol it does not speak."""


class TunnelNoApp(SoftEOF):
    """The tunnel answered (HTTP 502/503/504) but nothing runs behind it yet."""
    pass


class HostDown(PortDown):
    """The host name does not resolve."""


# ---- TLS ----
_TLS_LOCK = threading.Lock()
_TLS_CACHE = {}


def _alpn(ctx):
    try:
        ctx.set_alpn_protocols(["http/1.1"])
    except (NotImplementedError, AttributeError, ssl.SSLError):
        pass


def verified_ctx():
    with _TLS_LOCK:
        if "verified" not in _TLS_CACHE:
            ctx = ssl.create_default_context()
            _alpn(ctx)
            try:
                if not ctx.get_ca_certs():
                    import certifi  # optional, helps on some macOS/iOS installs
                    ctx.load_verify_locations(cafile=certifi.where())
            except Exception:
                pass
            _TLS_CACHE["verified"] = ctx
        return _TLS_CACHE["verified"]


def pinned_ctx():
    with _TLS_LOCK:
        if "pinned" not in _TLS_CACHE:
            try:
                ctx = ssl.SSLContext(getattr(ssl, "PROTOCOL_TLS_CLIENT", ssl.PROTOCOL_SSLv23))
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_REQUIRED
                ctx.load_verify_locations(cadata=_TLS_CERT)
                _alpn(ctx)
                _TLS_CACHE["pinned"] = ctx
            except Exception:
                _TLS_CACHE["pinned"] = None
        return _TLS_CACHE["pinned"]


def insecure_ctx():
    """Last resort when this computer has no usable CA certificates (common on fresh
    python.org macOS installs): still encrypted, just not verified."""
    with _TLS_LOCK:
        if "insecure" not in _TLS_CACHE:
            try:
                ctx = ssl.SSLContext(getattr(ssl, "PROTOCOL_TLS_CLIENT", ssl.PROTOCOL_SSLv23))
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                _alpn(ctx)
                _TLS_CACHE["insecure"] = ctx
            except Exception:
                _TLS_CACHE["insecure"] = None
        return _TLS_CACHE["insecure"]


_WARNED_INSECURE = []


def note_ctx(ctx):
    if ctx is not None and ctx is _TLS_CACHE.get("insecure") and not _WARNED_INSECURE:
        _WARNED_INSECURE.append(1)
        say("[!] This computer could not verify the tunnel's HTTPS certificate (its certificate "
            "store is missing or outdated). The link is still encrypted. On macOS, run "
            "'Install Certificates.command' from your Python folder to fix this.")


def server_ctx():
    with _TLS_LOCK:
        if "server" not in _TLS_CACHE:
            path = None
            ctx = None
            try:
                ctx = ssl.SSLContext(getattr(ssl, "PROTOCOL_TLS_SERVER", ssl.PROTOCOL_SSLv23))
                _alpn(ctx)
                fd, path = tempfile.mkstemp(suffix=".pem")
                with os.fdopen(fd, "w") as f:
                    f.write(_TLS_CERT + "\n" + _TLS_KEY + "\n")
                ctx.load_cert_chain(path)
            except Exception:
                ctx = None
            finally:
                if path:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
            _TLS_CACHE["server"] = ctx
        return _TLS_CACHE["server"]


def is_verify_error(exc):
    cls = getattr(ssl, "SSLCertVerificationError", None)
    if cls is not None and isinstance(exc, cls):
        return True
    return "CERTIFICATE_VERIFY_FAILED" in str(exc)


def connect_tcp(host, port):
    try:
        sock = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
    except socket.gaierror:
        raise HostDown(host)
    except OSError as exc:
        raise PortDown(str(exc))
    tune_socket(sock)
    return sock


def connect_tls(host, port, holder):
    """Public sites: full certificate checks. Direct LAN peer: its pinned certificate."""
    ctxs = [holder[0]] if holder else [verified_ctx(), pinned_ctx(), insecure_ctx()]
    last = None
    for ctx in ctxs:
        if ctx is None:
            continue
        raw = connect_tcp(host, port)
        try:
            raw.settimeout(CONNECT_TIMEOUT)
            s = ctx.wrap_socket(raw, server_hostname=host)
            if not holder:
                holder.append(ctx)
                note_ctx(ctx)
            return s
        except (ssl.SSLEOFError, ConnectionResetError, BrokenPipeError) as exc:
            close_sock(raw)
            if "EOF" in str(exc) or isinstance(exc, (ConnectionResetError, BrokenPipeError)):
                raise SoftEOF("closed at once")
            last = exc
            break
        except Exception as exc:
            close_sock(raw)
            last = exc
            if not is_verify_error(exc):
                break
    raise NotUs("TLS failed: %s" % last)


# ======================================================================
# Connections: a byte pipe with send()/recv()/close()
# ======================================================================
class SockConn(object):
    """Plain or TLS socket, non-blocking so reading and writing never deadlock."""
    def __init__(self, sock, label, pre=b""):
        self.sock = sock
        self.label = label
        self.pre = bytes(pre)
        self.is_ssl = isinstance(sock, ssl.SSLSocket)
        self.io = threading.Lock()
        self.wlock = threading.Lock()
        self.closed = False
        self.ping_interval = PING_INTERVAL
        self.dead_after = DEAD_AFTER
        self.handshake_timeout = 6.0     # our peer answers at once; silence = something else
        sock.setblocking(False)

    def send(self, data):
        view = memoryview(data)
        if view.ndim != 1 or view.itemsize != 1:
            view = view.cast("B")
        with self.wlock:
            while len(view):
                if self.closed:
                    raise ConnectionError("closed")
                try:
                    if self.is_ssl:
                        with self.io:
                            n = self.sock.send(view[:262144])
                    else:
                        n = self.sock.send(view[:RECV_SIZE])
                    if n:
                        view = view[n:]
                        continue
                    want = "w"
                except ssl.SSLWantReadError:
                    want = "r"
                except (ssl.SSLWantWriteError, BlockingIOError, InterruptedError):
                    want = "w"
                except ssl.SSLError as exc:
                    raise ConnectionError(str(exc))
                try:
                    if want == "r":
                        select.select([self.sock], [], [], 0.05)
                    else:
                        select.select([], [self.sock], [], 1.0)
                except (OSError, ValueError):
                    raise ConnectionError("closed")

    def recv(self, timeout=1.0):
        """bytes = data, b'' = closed, None = nothing yet."""
        if self.pre:
            data, self.pre = self.pre, b""
            return data
        if self.closed:
            return b""
        want = "r"
        try:
            if self.is_ssl:
                with self.io:
                    return self.sock.recv(RECV_SIZE)
            return self.sock.recv(RECV_SIZE)
        except (ssl.SSLWantReadError, BlockingIOError, InterruptedError):
            pass
        except ssl.SSLWantWriteError:
            want = "w"
        except (ssl.SSLZeroReturnError, ssl.SSLEOFError):
            return b""
        except ssl.SSLError:
            return b""
        except (OSError, ValueError):
            return b""
        try:
            if want == "r":
                select.select([self.sock], [], [], timeout)
            else:
                select.select([], [self.sock], [], timeout)
        except (OSError, ValueError):
            return b""
        return None

    def close(self):
        self.closed = True
        close_sock(self.sock)


class Reader(object):
    """Blocking-style helpers over a conn's recv(), with a deadline."""
    def __init__(self, conn, buf=b""):
        self.conn = conn
        self.buf = bytearray(buf)

    def _fill(self, deadline):
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise NotUs("no reply")
            data = self.conn.recv(min(1.0, left))
            if data is None:
                continue
            if not data:
                raise EOFError("closed")
            self.buf += data
            return

    def until(self, delim, limit, deadline):
        while True:
            i = self.buf.find(delim)
            if i >= 0:
                out = bytes(self.buf[:i + len(delim)])
                del self.buf[:i + len(delim)]
                return out
            if len(self.buf) > limit:
                raise NotUs("header too large")
            self._fill(deadline)

    def exact(self, n, deadline):
        while len(self.buf) < n:
            self._fill(deadline)
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out


class WSConn(object):
    """WebSocket (RFC 6455) used as a plain byte pipe; works through HTTP(S) tunnels."""
    def __init__(self, base, client, label, pre=b""):
        self.base = base
        self.client = client
        self.label = label
        self.buf = bytearray(pre)
        self.wlock = threading.Lock()
        self.closed = False
        self.key = os.urandom(4)
        self.mask_cache = {}
        self.handshake_timeout = 6.0
        self.ping_interval = PING_INTERVAL
        self.dead_after = DEAD_AFTER

    def _xor(self, data, key):
        n = len(data)
        if not n:
            return b""
        ck = (key, n)
        m = self.mask_cache.get(ck)
        if m is None:
            if len(self.mask_cache) > 8:
                self.mask_cache.clear()
            m = int.from_bytes((key * (n // 4 + 1))[:n], "little")
            self.mask_cache[ck] = m
        return (int.from_bytes(data, "little") ^ m).to_bytes(n, "little")

    def _frame(self, opcode, payload):
        n = len(payload)
        mbit = 0x80 if self.client else 0
        if n < 126:
            hdr = struct.pack(">BB", 0x80 | opcode, mbit | n)
        elif n < 65536:
            hdr = struct.pack(">BBH", 0x80 | opcode, mbit | 126, n)
        else:
            hdr = struct.pack(">BBQ", 0x80 | opcode, mbit | 127, n)
        if self.client:
            return hdr + self.key + self._xor(payload, self.key)
        return hdr + bytes(payload)

    def _send_frame(self, opcode, payload):
        with self.wlock:
            self.base.send(self._frame(opcode, payload))

    def send(self, data):
        view = memoryview(data)
        for i in range(0, len(view), WS_MAX_FRAME):
            self._send_frame(2, view[i:i + WS_MAX_FRAME])

    def recv(self, timeout=1.0):
        out = []
        while True:
            res = self._parse_one()
            if res is None:
                break
            if res == "closed":
                if out:
                    return b"".join(out)
                return b""
            if res:
                out.append(res)
        if out:
            return b"".join(out)
        data = self.base.recv(timeout)
        if data is None:
            return None
        if not data:
            return b""
        self.buf += data
        if self._frame_len() is None:
            return None
        return self.recv(0)

    def _frame_len(self):
        b = self.buf
        if len(b) < 2:
            return None
        n = b[1] & 0x7F
        pos = 2
        if n == 126:
            if len(b) < 4:
                return None
            n = struct.unpack_from(">H", b, 2)[0]
            pos = 4
        elif n == 127:
            if len(b) < 10:
                return None
            n = struct.unpack_from(">Q", b, 2)[0]
            pos = 10
        if n > MAX_FRAME:
            raise ConnectionError("websocket frame too large")
        if b[1] & 0x80:
            pos += 4
        if len(b) < pos + n:
            return None
        return pos, n

    def _parse_one(self):
        fl = self._frame_len()
        if fl is None:
            return None
        pos, n = fl
        b = self.buf
        opcode = b[0] & 0x0F
        masked = b[1] & 0x80
        if masked:
            key = bytes(b[pos - 4:pos])
            payload = self._xor(bytes(b[pos:pos + n]), key)
        else:
            payload = bytes(b[pos:pos + n])
        del self.buf[:pos + n]
        if opcode in (0, 1, 2):
            return payload
        if opcode == 8:
            try:
                self._send_frame(8, payload[:2])
            except Exception:
                pass
            return "closed"
        if opcode == 9:
            try:
                self._send_frame(10, payload)
            except Exception:
                pass
        return b""

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                if self.base.wlock.acquire(False):
                    self.base.wlock.release()
                    self.base.send(self._frame(8, b"\x03\xe8"))
            except Exception:
                pass
        self.base.close()


def http_head(lines):
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


def parse_head(raw):
    text = raw.decode("latin-1")
    lines = text.split("\r\n")
    first = lines[0].split(" ")
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return first, headers


def host_header(host, port, tls):
    h = "[%s]" % host if ":" in host else host
    if (tls and port == 443) or (not tls and port == 80):
        return h
    return "%s:%d" % (h, port)


def ws_client(host, port, tls, holder):
    sock = connect_tls(host, port, holder) if tls else connect_tcp(host, port)
    base = SockConn(sock, "ws")
    try:
        key = base64.b64encode(os.urandom(16)).decode()
        lines = ["GET /fs/ws HTTP/1.1",
                 "Host: " + host_header(host, port, tls),
                 "Upgrade: websocket", "Connection: Upgrade",
                 "Sec-WebSocket-Key: " + key, "Sec-WebSocket-Version: 13",
                 MARK_HEADER + ": 1"]
        lines += ["%s: %s" % kv for kv in TUNNEL_HEADERS.items()]
        base.send(http_head(lines))
        rd = Reader(base)
        try:
            head = rd.until(b"\r\n\r\n", 65536, time.monotonic() + CONNECT_TIMEOUT)
        except EOFError:
            if not rd.buf:
                raise SoftEOF("closed at once")
            raise NotUs("closed")
        first, headers = parse_head(head)
        if len(first) >= 2 and first[1] in ("502", "503", "504"):
            raise TunnelNoApp("tunnel is up, app not running yet")
        expect = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        if len(first) < 2 or first[1] != "101" or headers.get("sec-websocket-accept") != expect:
            raise NotUs("no websocket")
        return WSConn(base, True, "WebSocket over HTTPS" if tls else "WebSocket over HTTP",
                      bytes(rd.buf))
    except Exception:
        base.close()
        raise


class PollClientConn(object):
    """Last-resort transport: ordinary HTTP requests (long polling), reliable and ordered."""
    def __init__(self, host, port, tls, holder):
        self.host, self.port, self.tls, self.holder = host, port, tls, holder
        self.label = "HTTPS polling" if tls else "HTTP polling"
        self.cv = threading.Condition()
        self.outbuf = bytearray()
        self.inq = deque()
        self.closed = False
        self.sid = None
        self.ping_interval = POLL_PING_INTERVAL
        self.dead_after = POLL_DEAD_AFTER

    def _http(self, timeout):
        if self.tls:
            ctx = self.holder[0] if self.holder else verified_ctx()
            return http_client.HTTPSConnection(self.host, self.port, timeout=timeout, context=ctx)
        return http_client.HTTPConnection(self.host, self.port, timeout=timeout)

    def _request(self, conn, method, path, body=None):
        headers = dict(TUNNEL_HEADERS)
        headers[MARK_HEADER] = "1"
        headers["Content-Type"] = "application/octet-stream"
        headers["Content-Length"] = str(len(body) if body is not None else 0)
        conn.request(method, path, body=body if body is not None else b"", headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, resp.getheader(MARK_HEADER), resp.getheader("X-Seq"), data

    def open(self):
        last = None
        tries = [None] if (self.holder or not self.tls) else [verified_ctx(), pinned_ctx(), insecure_ctx()]
        for ctx in tries:
            if ctx is not None:
                self.holder[:] = [ctx]
            conn = self._http(CONNECT_TIMEOUT)
            try:
                try:
                    conn.connect()
                except socket.gaierror:
                    raise HostDown(self.host)
                except ssl.SSLError as exc:
                    raise NotUs(str(exc)) if not is_verify_error(exc) else exc
                except OSError as exc:
                    raise PortDown(str(exc))
                try:
                    status, mark, _seq, data = self._request(conn, "POST", "/fs/p/new")
                except (http_client.RemoteDisconnected, ConnectionResetError, BrokenPipeError):
                    raise SoftEOF("closed at once")
                if status in (502, 503, 504):
                    raise TunnelNoApp("tunnel is up, app not running yet")
                if status != 200 or mark != "1" or not re.match(r"^[0-9a-f]{16,64}$", data.decode("ascii", "replace").strip()):
                    raise NotUs("not file-share")
                self.sid = data.decode().strip()
                if self.tls and self.holder:
                    note_ctx(self.holder[0])
                break
            except Exception as exc:
                last = exc
                if self.tls and ctx is not None:
                    self.holder[:] = []      # only a context that worked is remembered
                    if is_verify_error(exc):
                        continue
                raise
            finally:
                conn.close()
        if self.sid is None:
            raise last or NotUs("not file-share")
        threading.Thread(target=self._up_loop, daemon=True).start()
        threading.Thread(target=self._down_loop, daemon=True).start()
        return self

    def _retry(self, fn):
        """Repeat one request until it succeeds; give up after POLL_GIVE_UP of failures."""
        fail_since = None
        conn = None
        while not self.closed:
            try:
                if conn is None:
                    conn = self._http(POLL_WAIT + 20)
                status, _m, seq, data = fn(conn)
                if status == 200:
                    return conn, seq, data
                if status in (404, 410):
                    break
                raise OSError("HTTP %s" % status)
            except Exception:
                if conn is not None:
                    conn.close()
                conn = None
                now = time.monotonic()
                fail_since = fail_since or now
                if now - fail_since > POLL_GIVE_UP:
                    break
                time.sleep(0.5)
        self.close()
        return None, None, None

    def _up_loop(self):
        seq = 0
        conn = None
        while not self.closed:
            with self.cv:
                while not self.outbuf and not self.closed:
                    self.cv.wait(1.0)
                if self.closed:
                    break
                batch = bytes(self.outbuf[:POLL_BATCH])
            seq += 1
            path = "/fs/p/%s/up?seq=%d" % (self.sid, seq)

            def do(c, path=path, batch=batch):
                return self._request(c, "POST", path, batch)
            conn, _s, _d = self._retry(do) if conn is None else self._once_or_retry(conn, do)
            if conn is None:
                break
            with self.cv:
                del self.outbuf[:len(batch)]
                self.cv.notify_all()
        if conn is not None:
            conn.close()

    def _down_loop(self):
        last = 0
        conn = None
        while not self.closed:
            path = "/fs/p/%s/down?ack=%d" % (self.sid, last)

            def do(c, path=path):
                return self._request(c, "GET", path)
            conn, seq, data = self._retry(do) if conn is None else self._once_or_retry(conn, do)
            if conn is None:
                break
            try:
                seq = int(seq or 0)
            except ValueError:
                seq = 0
            if seq == last + 1:
                last = seq
                if data:
                    with self.cv:
                        self.inq.append(data)
                        self.cv.notify_all()
        if conn is not None:
            conn.close()

    def _once_or_retry(self, conn, do):
        try:
            status, _m, seq, data = do(conn)
            if status == 200:
                return conn, seq, data
            if status in (404, 410):
                self.close()
                return None, None, None
        except Exception:
            pass
        conn.close()
        return self._retry(do)

    def send(self, data):
        with self.cv:
            while len(self.outbuf) > POLL_BATCH * 2 and not self.closed:
                self.cv.wait(1.0)
            if self.closed:
                raise ConnectionError("closed")
            self.outbuf += data
            self.cv.notify_all()

    def recv(self, timeout=1.0):
        with self.cv:
            if not self.inq and not self.closed:
                self.cv.wait(timeout)
            if self.inq:
                out = b"".join(self.inq)
                self.inq.clear()
                return out
            return b"" if self.closed else None

    def close(self):
        with self.cv:
            if self.closed:
                return
            self.closed = True
            self.cv.notify_all()
        sid = self.sid

        def bye():
            try:
                c = self._http(3)
                self._request(c, "POST", "/fs/p/%s/close" % sid)
                c.close()
            except Exception:
                pass
        if sid:
            threading.Thread(target=bye, daemon=True).start()


class PollServerConn(object):
    """Server side of the HTTP polling transport."""
    def __init__(self, label):
        self.sid = os.urandom(16).hex()
        self.label = label
        self.cv = threading.Condition()
        self.inq = deque()
        self.down = bytearray()
        self.up_expected = 1
        self.down_seq = 0
        self.last_batch = None
        self.closed = False
        self.last_seen = time.monotonic()
        self.ping_interval = POLL_PING_INTERVAL
        self.dead_after = POLL_DEAD_AFTER

    def handle_up(self, seq, body):
        with self.cv:
            self.last_seen = time.monotonic()
            if self.closed:
                return 410
            if seq == self.up_expected:
                if body:
                    self.inq.append(body)
                self.up_expected += 1
                self.cv.notify_all()
            return 200   # older seq = duplicate retry, already have it

    def handle_down(self, ack):
        with self.cv:
            self.last_seen = time.monotonic()
            if self.closed:
                return 410, 0, b""
            if self.last_batch is not None and ack >= self.last_batch[0]:
                self.last_batch = None
            if self.last_batch is not None:
                return 200, self.last_batch[0], self.last_batch[1]
            end = time.monotonic() + POLL_WAIT
            while not self.down and self.last_batch is None and not self.closed:
                left = end - time.monotonic()
                if left <= 0:
                    break
                self.cv.wait(min(left, 1.0))
            self.last_seen = time.monotonic()
            if self.closed:
                return 410, 0, b""
            if self.last_batch is not None:
                # another (stale) request built a batch meanwhile: hand out that one,
                # never a newer one, so nothing is skipped
                return 200, self.last_batch[0], self.last_batch[1]
            if not self.down:
                return 200, 0, b""
            data = bytes(self.down[:POLL_BATCH])
            del self.down[:len(data)]
            self.down_seq += 1
            self.last_batch = (self.down_seq, data)
            self.cv.notify_all()
            return 200, self.down_seq, data

    def send(self, data):
        with self.cv:
            while len(self.down) > POLL_BATCH * 2 and not self.closed:
                self.cv.wait(1.0)
            if self.closed:
                raise ConnectionError("closed")
            self.down += data
            self.cv.notify_all()

    def recv(self, timeout=1.0):
        with self.cv:
            if not self.inq and not self.closed:
                self.cv.wait(timeout)
            if time.monotonic() - self.last_seen > POLL_DEAD_AFTER:
                self.closed = True
            if self.inq:
                out = b"".join(self.inq)
                self.inq.clear()
                return out
            return b"" if self.closed else None

    def close(self):
        with self.cv:
            self.closed = True
            self.cv.notify_all()


# ======================================================================
# Session: one live connection to the other user (frames, heartbeats)
# ======================================================================
class Session(object):
    def __init__(self, app, conn, dialer):
        self.app = app
        self.conn = conn
        self.dialer = dialer
        self.label = conn.label
        self.peer_id = None
        self.peer_name = "other user"
        self.alive = True
        self.attached = False
        self.last_rx = time.monotonic()
        self.last_tx = time.monotonic()
        self.cv = threading.Condition()
        self.ctrl_q = deque()
        self.data_q = deque()
        self.data_bytes = 0
        self.buf = bytearray()
        self.bye = False
        self.busy = False
        self.peer_busy = ""
        self.remote_private = False

    # ---- sending: control first, file data second; the reader never blocks ----
    def send_ctrl(self, **msg):
        payload = json.dumps(msg, separators=(",", ":")).encode("utf-8")
        with self.cv:
            if not self.alive:
                return False
            self.ctrl_q.append(HDR.pack(F_CTRL, len(payload)) + payload)
            self.cv.notify_all()
        return True

    def send_data(self, tid, offset, chunk):
        frame = (HDR.pack(F_DATA, DATA_HDR.size + len(chunk))
                 + DATA_HDR.pack(tid, offset, zlib.crc32(chunk) & 0xFFFFFFFF) + chunk)
        with self.cv:
            while self.alive and self.data_bytes > DATA_QUEUE_MAX:
                self.cv.wait(1.0)
            if not self.alive:
                return False
            self.data_q.append((tid, frame))
            self.data_bytes += len(frame)
            self.cv.notify_all()
        return True

    def purge(self, tid):
        with self.cv:
            keep = deque(item for item in self.data_q if item[0] != tid)
            self.data_q = keep
            self.data_bytes = sum(len(f) for _t, f in keep)
            self.cv.notify_all()

    def _writer(self):
        try:
            while True:
                with self.cv:
                    while self.alive and not self.ctrl_q and not self.data_q:
                        left = self.conn.ping_interval - (time.monotonic() - self.last_tx)
                        if left <= 0:
                            break
                        self.cv.wait(min(left, 1.0))
                    if not self.alive:
                        return
                    if self.ctrl_q:
                        frame = self.ctrl_q.popleft()
                    elif self.data_q:
                        _tid, frame = self.data_q.popleft()
                        self.data_bytes -= len(frame)
                        self.cv.notify_all()
                    else:
                        frame = HDR.pack(F_PING, 0)
                self.conn.send(frame)
                self.last_tx = time.monotonic()
        except Exception:
            self.kill()

    # ---- receiving ----
    def _frames(self):
        buf = self.buf
        while len(buf) >= 5:
            ftype, n = HDR.unpack_from(buf, 0)
            if n > MAX_FRAME or ftype not in (F_CTRL, F_DATA, F_PING):
                raise ConnectionError("protocol error")
            if len(buf) < 5 + n:
                return
            payload = bytes(buf[5:5 + n])
            del buf[:5 + n]
            yield ftype, payload

    def handshake(self, magic_seen):
        hello = json.dumps({"t": "hello", "app": "file-share", "v": APP_VERSION,
                            "id": INSTANCE_ID, "name": HOSTNAME,
                            "busy": self.app.busy_with()}).encode()
        self.conn.send(MAGIC + HDR.pack(F_CTRL, len(hello)) + hello)
        need_magic = not magic_seen
        deadline = time.monotonic() + getattr(self.conn, "handshake_timeout", HANDSHAKE_TIMEOUT)
        while time.monotonic() < deadline:
            data = self.conn.recv(0.5)
            if data is None:
                continue
            if not data:
                if not self.buf and need_magic:
                    raise SoftEOF("closed at once (tunnel up, but nothing behind it yet)")
                raise NotUs("closed during handshake")
            self.buf += data
            if need_magic:
                if len(self.buf) < len(MAGIC):
                    if not MAGIC.startswith(bytes(self.buf)):
                        raise NotUs("not file-share")
                    continue
                if not self.buf.startswith(MAGIC):
                    raise NotUs("not file-share")
                del self.buf[:len(MAGIC)]
                need_magic = False
            if len(self.buf) >= 5:
                ftype, n = HDR.unpack_from(self.buf, 0)
                if ftype != F_CTRL or n > 65536:
                    raise NotUs("bad hello")
                if len(self.buf) >= 5 + n:
                    try:
                        msg = json.loads(bytes(self.buf[5:5 + n]).decode("utf-8"))
                    except ValueError:
                        raise NotUs("bad hello")
                    del self.buf[:5 + n]
                    if msg.get("t") != "hello" or msg.get("app") != "file-share" or not msg.get("id"):
                        raise NotUs("bad hello")
                    self.peer_id = str(msg["id"])[:64]
                    self.peer_name = str(msg.get("name") or "other user")[:40]
                    self.peer_busy = str(msg.get("busy") or "")
                    self.last_rx = time.monotonic()
                    return
        raise NotUs("handshake timed out")

    def start_writer(self):
        threading.Thread(target=self._writer, daemon=True).start()

    def run_reader(self):
        try:
            for ftype, payload in self._frames():   # bytes left over from the handshake
                self.app.on_frame(self, ftype, payload)
            while self.alive:
                data = self.conn.recv(1.0)
                if data is None:
                    continue
                if not data:
                    break
                self.last_rx = time.monotonic()
                self.buf += data
                for ftype, payload in self._frames():
                    self.app.on_frame(self, ftype, payload)
                    if not self.alive:
                        break
        except Exception:
            pass
        finally:
            self.kill()

    def dialer_id(self):
        return INSTANCE_ID if self.dialer else (self.peer_id or "")

    def kill(self):
        with self.cv:
            if not self.alive:
                return
            self.alive = False
            self.data_q.clear()
            self.ctrl_q.clear()
            self.data_bytes = 0
            self.cv.notify_all()
        try:
            self.conn.close()
        except Exception:
            pass
        self.app.on_session_end(self)

    def flush_and_close(self, timeout=1.0):
        """Best-effort: deliver queued control messages (e.g. 'bye') then close."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self.cv:
                if not self.ctrl_q or not self.alive:
                    break
            time.sleep(0.02)
        time.sleep(0.1)
        self.kill()


# ======================================================================
# Transfers
# ======================================================================
class FileChanged(Exception):
    pass


class Batch(object):
    """Files/folders offered with one upload command: the other user answers once for all."""
    def __init__(self, members):
        self.id = os.urandom(6).hex()
        self.members = members
        self.said = set()
        self.lock = threading.Lock()

    def once(self, key):
        with self.lock:
            if key in self.said:
                return False
            self.said.add(key)
            return True

    def describe(self):
        items = self.members
        names = ", ".join(short(t.name, 24) for t in items[:3])
        if len(items) > 3:
            names += " and %d more" % (len(items) - 3)
        return "%d items (%s total): %s" % (len(items), human(sum(t.size for t in items)), names)


class OutTransfer(object):
    """A file or folder this user offered."""
    def __init__(self, app, path):
        self.app = app
        self.path = os.path.abspath(path)
        self.tid = app.new_tid()
        self.name = os.path.basename(self.path.rstrip("\\/")) or "file"
        self.kind = "folder" if os.path.isdir(self.path) else "file"
        self.entries = []          # (absolute path, relative path, size)
        self.dirs = []
        self.size = 0
        self.fid = ""
        self.state = "preparing"   # preparing, waiting, offered, sending, done, refused, cancelled, failed
        self.cv = threading.Condition()
        self.go = None             # (session, offset) from the receiver
        self.offer_session = None
        self.accepted_once = False
        self.acked = 0
        self.sent = 0
        self.meter = Meter()
        self.started = None
        self.skipped = 0
        self.batch = None
        self.num = 0
        self.ready = False

    def terminal(self):
        return self.state in ("done", "refused", "cancelled", "failed")

    def solo(self):
        return self.batch is None or len(self.batch.members) <= 1

    def build_manifest(self):
        if self.kind == "file":
            st = os.stat(self.path)
            with open(self.path, "rb"):
                pass                        # readable?
            self.entries = [(self.path, self.name, st.st_size)]
            self.size = st.st_size
            ident = [self.path, self.name, "file", st.st_size, int(st.st_mtime)]
        else:
            entries, dirs = [], []
            for root, dnames, fnames in os.walk(self.path, followlinks=False):
                dnames.sort()
                rel_root = os.path.relpath(root, self.path)
                rel_root = "" if rel_root == "." else rel_root.replace(os.sep, "/")
                for d in dnames:
                    full = os.path.join(root, d)
                    if os.path.islink(full):
                        continue
                    dirs.append((rel_root + "/" + d) if rel_root else d)
                for fn in sorted(fnames):
                    full = os.path.join(root, fn)
                    try:
                        if not os.path.isfile(full):
                            continue
                        st = os.stat(full)
                        if not os.access(full, os.R_OK):
                            raise OSError("not readable")
                    except OSError:
                        self.skipped += 1
                        continue
                    rel = (rel_root + "/" + fn) if rel_root else fn
                    entries.append((full, rel, st.st_size, int(st.st_mtime)))
            self.entries = [(a, r, s) for a, r, s, _m in entries]
            self.dirs = dirs
            self.size = sum(e[2] for e in self.entries)
            ident = [self.path, self.name, "folder", self.size, [(r, s, m) for _a, r, s, m in entries], dirs]
        self.fid = hashlib.sha256(json.dumps(ident).encode("utf-8")).hexdigest()[:32]

    def offer_msg(self, resume):
        msg = {"t": "offer", "tid": self.tid, "fid": self.fid, "name": self.name,
               "kind": self.kind, "size": self.size, "count": len(self.entries),
               "resume": bool(resume)}
        if self.batch is not None:
            msg["batch"] = self.batch.id
            msg["bn"] = max(1, len([t for t in self.batch.members if not t.terminal()]))
        if self.kind == "folder":
            msg["entries"] = [[r, s] for _a, r, s in self.entries]
            msg["dirs"] = self.dirs
        return msg

    def run(self):
        try:
            self._run()
        except Exception as exc:
            if not self.terminal():
                self.state = "failed"
                self.app.send_cancel(self.tid, "up", "upload failed on the other side")
                say("[!] Upload of %s failed: %s" % (self.name, exc or exc.__class__.__name__))
        finally:
            self.app.transfer_ended()

    def prepare(self):
        """Read the file/folder list. Returns False (and says why) when it cannot be sent."""
        try:
            self.build_manifest()
        except OSError as exc:
            self.state = "failed"
            say("[!] Cannot read %s: %s" % (self.path, exc.strerror or exc))
            return False
        if self.skipped:
            say("[!] %d unreadable item(s) in %s will be skipped." % (self.skipped, self.name))
        if len(json.dumps(self.offer_msg(False))) > MAX_FRAME - 4096:
            self.state = "failed"
            say("[!] %s has too many files to send as one folder. Zip it first, then upload the zip." % self.name)
            return False
        self.ready = True
        self.state = "waiting"
        return True

    def _run(self):
        if not self.ready and not self.prepare():
            return
        what = ("folder, %d files" % len(self.entries)) if self.kind == "folder" else "file"
        if self.app.session is None and self.solo():
            say("[*] %s (%s, %s) will be offered as soon as the other user is connected."
                % (self.name, what, human(self.size)))
        while not self.terminal():
            sess = self.app.session
            if sess is None or not sess.alive:
                if self.state in ("offered", "sending", "queued"):
                    self.state = "waiting"
                self.app.wait_session(1.0)
                continue
            if self.offer_session is not sess:
                with self.cv:
                    self.go = None
                self.offer_session = sess
                if not sess.send_ctrl(**self.offer_msg(self.accepted_once)):
                    continue
                if self.state == "waiting" and not self.accepted_once and self.solo():
                    say("[*] Offered %s (%s, %s). Waiting for the other user to answer..."
                        % (self.name, what, human(self.size)))
                self.state = "offered" if not self.accepted_once else "waiting"
            with self.cv:
                while not self.terminal() and self.go is None and sess.alive and self.app.session is sess:
                    self.cv.wait(0.5)
                if self.terminal() or self.go is None:
                    continue
                go_sess, offset = self.go
                self.go = None
            if go_sess is not sess:
                continue
            self.state = "queued"
            if not self.app.acquire_slot(self, sess):
                continue
            try:
                with self.cv:
                    if self.go is not None and self.go[0] is sess:
                        offset = self.go[1]       # a newer resume point arrived while queued
                        self.go = None
                if self.terminal():
                    continue
                self.state = "sending"
                self._stream(sess, offset)
            finally:
                self.app.release_slot(self)
            # all bytes queued: wait for 'complete', a resend request, or a lost link
            with self.cv:
                while not self.terminal() and self.go is None and sess.alive and self.app.session is sess:
                    self.cv.wait(0.5)

    def _locate(self, offset):
        pos = offset
        for i, (_a, _r, size) in enumerate(self.entries):
            if pos < size:
                return i, pos
            pos -= size
        return len(self.entries), 0

    def _stream(self, sess, offset):
        idx, pos = self._locate(offset)
        f = None
        try:
            while offset < self.size:
                if self.terminal() or self.go is not None or not sess.alive:
                    return
                absp, _rel, fsize = self.entries[idx]
                if pos >= fsize:
                    if f is not None:
                        f.close()
                        f = None
                    idx, pos = idx + 1, 0
                    continue
                if f is None:
                    f = open(lp(absp), "rb")
                    if pos:
                        f.seek(pos)
                n = min(CHUNK, fsize - pos)
                data = f.read(n)
                if len(data) != n:
                    raise FileChanged("%s changed while it was being sent" % os.path.basename(absp))
                if not sess.send_data(self.tid, offset, data):
                    return
                offset += n
                pos += n
                self.sent = offset
        finally:
            if f is not None:
                f.close()

    # ---- events from the other user ----
    def on_answer(self, sess, ok, offset, reason):
        if self.terminal():
            return
        if not ok:
            self.state = "refused"
            extra = (" (%s)" % reason) if reason and reason != "refused" else ""
            if self.solo():
                say("[x] The other user refused to download %s.%s" % (self.name, extra))
            elif self.batch.once("refused" + extra):
                say("[x] The other user refused to download %s.%s" % (self.batch.describe(), extra))
            with self.cv:
                self.cv.notify_all()
            return
        try:
            offset = max(0, min(int(offset), self.size))
        except (TypeError, ValueError):
            offset = 0
        if not self.accepted_once:
            self.accepted_once = True
            self.started = time.monotonic()
            self.acked = offset
            if not self.solo():
                if self.batch.once("accepted"):
                    say("[+] Receiver accepted, files are uploading: %s" % self.batch.describe())
            elif offset:
                say("[+] Receiver accepted. Resuming %s from %.1f%% (%s already there)."
                    % (self.name, 100.0 * offset / max(1, self.size), human(offset)))
            else:
                say("[+] Receiver accepted, file is uploading: %s (%s)" % (self.name, human(self.size)))
        elif not self.solo():
            if sess is not getattr(self, "_last_go_sess", None) and self.batch.once("resumed%d" % id(sess)):
                say("[+] Uploads resumed.")
        elif sess is not getattr(self, "_last_go_sess", None):
            say("[+] Upload of %s resumed at %.1f%%." % (self.name, 100.0 * offset / max(1, self.size)))
        self._last_go_sess = sess
        self.meter.reset()
        sess.purge(self.tid)
        with self.cv:
            self.go = (sess, offset)
            self.cv.notify_all()

    def on_progress(self, got):
        try:
            self.acked = max(0, min(int(got), self.size))
        except (TypeError, ValueError):
            return
        self.meter.add(self.acked)

    def on_complete(self, saved):
        if self.terminal():
            return
        self.state = "done"
        self.acked = self.size
        took = time.monotonic() - (self.started or time.monotonic())
        speed = (" at %s/s" % human(self.size / took)) if took > 0.5 else ""
        if self.solo():
            say("[+] Upload complete: %s (%s) in %s%s. The other user saved it."
                % (self.name, human(self.size), fmt_eta(took), speed))
        else:
            say("[+] Upload complete: %s (%s)%s" % (self.name, human(self.size), speed))
        with self.cv:
            self.cv.notify_all()
        if not self.solo() and all(t.terminal() for t in self.batch.members) and self.batch.once("finished"):
            done = len([t for t in self.batch.members if t.state == "done"])
            say("[+] Finished: %d of %d items from that upload were sent." % (done, len(self.batch.members)))

    def on_remote_cancel(self, reason):
        if self.terminal():
            return
        was_offer = not self.accepted_once
        self.state = "cancelled"
        if reason:
            say("[x] Upload of %s stopped: %s." % (self.name, reason))
        elif was_offer:
            say("[x] The other user refused to download %s." % self.name)
        else:
            say("[x] File uploading stopped because the other user interrupted it (%s)." % self.name)
        with self.cv:
            self.cv.notify_all()

    def cancel_local(self):
        if self.terminal():
            return False
        was_active = self.accepted_once
        self.state = "cancelled"
        sess = self.app.session
        if sess is not None:
            sess.purge(self.tid)
        self.app.send_cancel(self.tid, "up", "")
        if was_active:
            say("[x] You interrupted the uploading of %s." % self.name)
        else:
            say("[x] You withdrew the offer of %s." % self.name)
        with self.cv:
            self.cv.notify_all()
        return True


class InTransfer(object):
    """A file or folder the other user offered to this user."""
    def __init__(self, app, peer_id, msg):
        self.app = app
        self.peer_id = peer_id
        self.tid = int(msg["tid"])
        self.fid = re.sub(r"[^0-9a-f]", "", str(msg.get("fid", "")).lower())[:64] or os.urandom(8).hex()
        self.kind = "folder" if msg.get("kind") == "folder" else "file"
        self.name = safe_component(msg.get("name") or "file")
        self.size = int(msg["size"])
        if self.size < 0:
            raise ValueError("bad size")
        self.entries = []
        self.dirs = []
        if self.kind == "folder":
            seen = set()
            total = 0
            for item in msg.get("entries") or []:
                rel, size = safe_relpath(item[0]), int(item[1])
                if size < 0:
                    raise ValueError("bad size")
                key = rel.lower()
                n = 1
                while key in seen:
                    root, ext = os.path.splitext(safe_relpath(item[0]))
                    rel = "%s (%d)%s" % (root, n, ext)
                    key = rel.lower()
                    n += 1
                seen.add(key)
                self.entries.append((rel, size))
                total += size
            if total != self.size:
                raise ValueError("size mismatch")
            self.dirs = [safe_relpath(d) for d in (msg.get("dirs") or [])][:200000]
        else:
            self.entries = [(self.name, self.size)]
        self.count = len(self.entries)
        self.state = "asking"        # asking, active, done, refused, cancelled, failed
        self.answers = []
        self.written = 0
        self.idx = 0
        self.pos = 0
        self.f = None
        self.opened = False
        self.last_progress = 0.0
        self.resync_sent = False
        self.meter = Meter()
        self.started = None
        self.have = 0
        self.num = 0
        self.group = None
        self.lock = threading.RLock()
        part_root = os.path.join(app.download_dir, ".fileshare-partial")
        self.part = os.path.join(part_root, self.fid + (".part" if self.kind == "file" else ""))
        self.part_root = part_root

    def terminal(self):
        return self.state in ("done", "refused", "cancelled", "failed")

    def existing_bytes(self):
        try:
            if self.kind == "file":
                return min(os.path.getsize(self.part), self.size) if os.path.isfile(self.part) else 0
            if os.path.isdir(self.part):
                return self._scan_folder()[0]
        except OSError:
            pass
        return 0

    def _scan_folder(self):
        done = 0
        for i, (rel, size) in enumerate(self.entries):
            p = lp(os.path.join(self.part, *rel.split("/")))
            if size == 0:
                continue
            have = os.path.getsize(p) if os.path.isfile(p) else 0
            if have == size:
                done += size
                continue
            if have > size:
                have = 0
            return done + have, i, have
        return done, len(self.entries), 0

    def prepare(self):
        """Open storage (resuming a partial download if there is one); returns the offset."""
        with self.lock:
            if self.opened:
                if self.f is not None:
                    self.f.flush()
                return self.written
            os.makedirs(self.part_root, exist_ok=True)
            if self.kind == "file":
                part = lp(self.part)
                have = os.path.getsize(part) if os.path.isfile(part) else 0
                if have > self.size:
                    have = 0
                if not have:
                    open(part, "wb").close()   # proves the folder is writable; opened on first data
                self.f = None
                self.written, self.idx, self.pos = have, 0, have
            else:
                os.makedirs(lp(self.part), exist_ok=True)
                for d in self.dirs:
                    os.makedirs(lp(os.path.join(self.part, *d.split("/"))), exist_ok=True)
                self.written, self.idx, self.pos = self._scan_folder()
                self.f = None
            self.opened = True
            return self.written

    def _open_current(self):
        rel, _size = self.entries[self.idx]
        path = lp(os.path.join(self.part, *rel.split("/")))
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        if self.pos:
            self.f = open(path, "r+b")
            self.f.truncate(self.pos)
            self.f.seek(self.pos)
        else:
            self.f = open(path, "wb")

    def write(self, data):
        if self.kind == "file":
            if self.f is None:             # opened lazily: many queued files never hold handles
                self.f = open(lp(self.part), "r+b")
                self.f.truncate(self.written)
                self.f.seek(self.written)
            self.f.write(data)
            self.written += len(data)
            self.pos = self.written
            return
        view = memoryview(data)
        while len(view):
            while self.idx < len(self.entries) and self.pos >= self.entries[self.idx][1]:
                if self.f is not None:
                    self.f.close()
                    self.f = None
                self.idx += 1
                self.pos = 0
            if self.idx >= len(self.entries):
                raise ValueError("more data than announced")
            if self.f is None:
                self._open_current()
            n = min(len(view), self.entries[self.idx][1] - self.pos)
            self.f.write(view[:n])
            self.pos += n
            self.written += n
            view = view[n:]

    def close_files(self):
        with self.lock:
            if self.f is not None:
                try:
                    self.f.close()
                except OSError:
                    pass
                self.f = None
            self.opened = False

    def discard(self):
        self.close_files()
        try:
            part = lp(self.part)
            if os.path.isdir(part):
                shutil.rmtree(part, ignore_errors=True)
            elif os.path.exists(part):
                os.remove(part)
        except OSError:
            pass
        self._tidy()

    def _tidy(self):
        try:
            os.rmdir(self.part_root)    # only succeeds when empty
        except OSError:
            pass

    def finalize(self):
        self.close_files()
        if self.kind == "folder":
            for rel, size in self.entries:
                if size == 0:
                    p = lp(os.path.join(self.part, *rel.split("/")))
                    parent = os.path.dirname(p)
                    if parent:
                        os.makedirs(parent, exist_ok=True)
                    open(p, "wb").close()
        else:
            if not os.path.exists(self.part):
                open(self.part, "wb").close()
        with self.app.finalize_lock:
            dest = unique_path(self.app.download_dir, self.name)
            os.rename(lp(self.part), lp(dest))
        self._tidy()
        return dest

    def on_data(self, sess, offset, crc, data):
        with self.lock:
            if self.state != "active" or not self.opened:
                return
            if offset != self.written:
                if offset > self.written and not self.resync_sent:
                    self.resync_sent = True
                    sess.send_ctrl(t="answer", tid=self.tid, ok=True, offset=self.written)
                return
            if zlib.crc32(data) & 0xFFFFFFFF != crc:
                if not self.resync_sent:
                    self.resync_sent = True
                    sess.send_ctrl(t="answer", tid=self.tid, ok=True, offset=self.written)
                return
            self.resync_sent = False
            try:
                self.write(data)
            except (OSError, ValueError) as exc:
                self.fail(sess, "disk error: %s" % (getattr(exc, "strerror", None) or exc))
                return
            self.meter.add(self.written)
            now = time.monotonic()
            if self.written >= self.size:
                self.complete(sess)
            elif now - self.last_progress >= PROGRESS_EVERY:
                self.last_progress = now
                sess.send_ctrl(t="progress", tid=self.tid, got=self.written)

    def complete(self, sess):
        with self.lock:
            if self.state != "active":
                return
            try:
                dest = self.finalize()
            except OSError as exc:
                self.fail(sess, "could not save: %s" % (exc.strerror or exc))
                return
            self.state = "done"
        took = time.monotonic() - (self.started or time.monotonic())
        speed = (" at %s/s" % human(self.size / took)) if took > 0.5 else ""
        if sess is not None:
            sess.send_ctrl(t="progress", tid=self.tid, got=self.size)
            sess.send_ctrl(t="complete", tid=self.tid, saved=os.path.basename(dest))
        g = self.group
        if g is not None and len(g.items) > 1:
            say("[+] Download complete: %s (%s)%s" % (self.name, human(self.size), speed))
            self.app.group_progress(g)
        else:
            say("[+] Download complete: %s (%s)%s\n    Saved to: %s" % (self.name, human(self.size), speed, dest))
        self.app.transfer_ended()

    def fail(self, sess, reason):
        with self.lock:
            if self.terminal():
                return
            self.state = "failed"
        self.close_files()
        self.app.send_cancel(self.tid, "down", reason, peer_id=self.peer_id)
        say("[!] Download of %s failed: %s. The partial file was kept; it resumes if sent again." % (self.name, reason))
        if self.group is not None and len(self.group.items) > 1:
            self.app.group_progress(self.group)
        self.app.transfer_ended()


# ======================================================================
# The application: connections, transfers, commands
# ======================================================================
class OfferGroup(object):
    """Everything offered by one upload command on the other side: one y/n question."""
    def __init__(self, peer_id, bid):
        self.peer_id = peer_id
        self.bid = bid
        self.items = []
        self.expected = 1
        self.state = "collecting"      # collecting, asking, decided
        self.decision = None
        self.answers = []
        self.last = time.monotonic()
        self.summarized = False

    def live(self):
        return [t for t in self.items if t.state == "asking"]

    def size(self):
        return sum(t.size for t in self.live())

    def title(self):
        items = self.live() or self.items
        return items[0].name if len(items) == 1 else "%d items" % len(items)


class App(object):
    def __init__(self, peer, port, download_dir):
        self.port = port
        self.download_dir = download_dir
        self.lock = threading.RLock()
        self.session_cv = threading.Condition(self.lock)
        self.session = None
        self.sessions = set()
        self.outgoing = {}             # tid -> OutTransfer
        self.incoming = {}             # (peer_id, tid) -> InTransfer
        self.prompts = deque()         # OfferGroup waiting for y/n, oldest first
        self.groups = {}               # (peer_id, batch id) -> OfferGroup
        self.serial = 0                # numbers shown by 'status', used by 'cancel <n>'
        self.slot_cv = threading.Condition()
        self.slot_wait = deque()       # accepted uploads waiting for a stream slot
        self.streaming = set()
        self.finalize_lock = threading.Lock()
        self.menu = None               # the Ctrl+Z "which transfers to stop" menu, when open
        self.pending_cancels = []      # (peer_id, tid, side, reason)
        self.stopping = False
        self.dialer = Dialer(self)
        self.listener = Listener(self)
        self.editor = None
        self.used_tids = set()
        self.ever_connected = False
        if peer:
            self.dialer.set_target(peer, announce=False)

    # ---- ids ----
    def new_tid(self):
        with self.lock:
            while True:
                tid = random.randint(1, 0xFFFFFFFF)
                if tid not in self.used_tids:
                    self.used_tids.add(tid)
                    return tid

    def next_num(self):
        with self.lock:
            self.serial += 1
            return self.serial

    # ---- upload stream slots: a few files stream at once, the rest queue in order ----
    def acquire_slot(self, t, sess):
        with self.slot_cv:
            if t not in self.slot_wait:
                self.slot_wait.append(t)
            got = False
            try:
                while True:
                    if t.terminal() or not sess.alive or self.session is not sess or self.stopping:
                        return False
                    while self.slot_wait and self.slot_wait[0] is not t and self.slot_wait[0].terminal():
                        self.slot_wait.popleft()
                    if len(self.streaming) < MAX_STREAMS and self.slot_wait and self.slot_wait[0] is t:
                        self.slot_wait.popleft()
                        self.streaming.add(t)
                        got = True
                        return True
                    self.slot_cv.wait(0.5)
            finally:
                if not got and t in self.slot_wait:
                    self.slot_wait.remove(t)
                self.slot_cv.notify_all()

    def release_slot(self, t):
        with self.slot_cv:
            self.streaming.discard(t)
            self.slot_cv.notify_all()

    # ---- sessions ----
    def wait_session(self, timeout):
        with self.session_cv:
            if self.session is None:
                self.session_cv.wait(timeout)

    def attach(self, sess):
        """Make sess the active connection. Returns False when it must be dropped."""
        if sess.peer_id == INSTANCE_ID:
            raise NotUs("self")
        with self.lock:
            if self.stopping:
                return False
            cur = self.session
            replaced = None
            if cur is not None and cur.alive:
                healthy = time.monotonic() - cur.last_rx < HEALTHY_WINDOW
                if cur.peer_id == sess.peer_id:
                    if healthy and sess.dialer_id() > cur.dialer_id():
                        sess.send_ctrl(t="dup")   # both dialed at once: keep the existing one
                        return False
                elif healthy:
                    sess.send_ctrl(t="busy")
                    return False
                replaced = cur
            same_peer = cur is not None and cur.peer_id == sess.peer_id
            self.session = sess
            sess.attached = True
            self.ever_connected = True
            self.session_cv.notify_all()
            peer_changed = self.last_peer_id is not None and self.last_peer_id != sess.peer_id
            self.last_quiet_peer = self.last_peer_id
            self.last_peer_id = sess.peer_id
            cancels = [c for c in self.pending_cancels if c[0] == sess.peer_id]
            self.pending_cancels = []
        if replaced is not None:
            if same_peer and replaced.alive:
                replaced.busy = True               # superseded twin: close it quietly on both sides
                replaced.send_ctrl(t="dup")
                threading.Thread(target=replaced.flush_and_close, args=(0.3,), daemon=True).start()
            else:
                replaced.kill()
        for _p, tid, side, reason in cancels:
            sess.send_ctrl(t="cancel", tid=tid, side=side, reason=reason)
        if peer_changed:
            self._peer_restarted()
        where = "LAN" if sess.remote_private else ""
        quiet = time.monotonic() < self.quiet_until and sess.peer_id == self.last_quiet_peer
        if (not same_peer or replaced is None) and not quiet:
            say("[+] Connected to %s via %s%s." % (sess.peer_name, sess.label, (" (" + where + ")") if where else ""))
        self.show_prompt()
        return True

    def busy_with(self):
        s = self.session
        if s is not None and s.alive and time.monotonic() - s.last_rx < HEALTHY_WINDOW:
            return s.peer_id or ""
        return ""

    last_peer_id = None
    last_quiet_peer = None
    quiet_until = 0.0

    def _peer_restarted(self):
        with self.lock:
            stale = [t for t in self.incoming.values() if t.state == "active"]
        for t in stale:
            t.close_files()
            t.state = "failed"
            say("[*] The other user restarted %s; %s is paused. If they send it again it resumes."
                % (APP_NAME, t.name))
        with self.lock:
            had_prompt = bool(self.prompts)
            self.prompts.clear()
            for g in self.groups.values():
                if g.state != "decided":
                    g.state, g.decision = "decided", "n"
                    for t in g.items:
                        if t.state == "asking":
                            t.state = "cancelled"
        if had_prompt:
            say("[*] The open question was dropped; the other user will offer again if needed.")
            CONSOLE.set_prompt("> ")

    def on_session_end(self, sess):
        with self.lock:
            self.sessions.discard(sess)
            was = self.session is sess
            if was:
                self.session = None
                self.session_cv.notify_all()
        for t in list(self.incoming.values()):
            if t.state == "active" and t.f is not None:
                try:
                    with t.lock:
                        if t.f is not None:
                            t.f.flush()
                except Exception:
                    pass
        if was and not self.stopping and not sess.busy:
            if sess.bye:
                say("[-] The other user closed %s." % APP_NAME)
            else:
                say("[-] Connection lost.")
            if self.dialer.has_target():
                say("[*] Reconnecting automatically... (transfers resume by themselves)")
            else:
                say("[*] Waiting for the other user to reconnect... (transfers resume by themselves)")
            self.dialer.wake()

    def session_alive(self):
        s = self.session
        return s is not None and s.alive

    def run_session(self, sess, magic_seen):
        """Handshake + attach + read loop, in the calling thread."""
        with self.lock:
            self.sessions.add(sess)
        try:
            sess.handshake(magic_seen)
            if sess.peer_id == INSTANCE_ID:
                raise NotUs("self")
            sess.start_writer()
            if not self.attach(sess):
                sess.flush_and_close(0.5)
                return "dup"
        except Exception:
            sess.alive = False
            try:
                sess.conn.close()
            except Exception:
                pass
            with self.lock:
                self.sessions.discard(sess)
            raise
        sess.run_reader()
        return "ok"

    # ---- frames ----
    def on_frame(self, sess, ftype, payload):
        if ftype == F_PING:
            return
        if ftype == F_DATA:
            if len(payload) < DATA_HDR.size:
                return
            tid, offset, crc = DATA_HDR.unpack_from(payload, 0)
            t = self.incoming.get((sess.peer_id, tid))
            if t is not None:
                t.on_data(sess, offset, crc, memoryview(payload)[DATA_HDR.size:])
            return
        try:
            msg = json.loads(payload.decode("utf-8"))
            kind = msg.get("t")
        except (ValueError, AttributeError):
            return
        try:
            self.on_ctrl(sess, kind, msg)
        except Exception as exc:
            say("[!] Ignored a bad message from the other user (%s)." % exc.__class__.__name__)

    def on_ctrl(self, sess, kind, msg):
        if kind == "offer":
            self.on_offer(sess, msg)
            return
        if kind == "busy":
            sess.busy = True
            self.dialer.note_busy()
            sess.kill()
            return
        if kind == "dup":
            sess.busy = True                  # a twin connection won; this one quietly closes
            self.quiet_until = time.monotonic() + 5.0
            sess.kill()
            return
        if kind == "bye":
            sess.bye = True
            sess.kill()
            return
        tid = msg.get("tid")
        if kind == "answer":
            t = self.outgoing.get(tid)
            if t is None or t.terminal():
                if msg.get("ok"):
                    sess.send_ctrl(t="cancel", tid=tid, side="up", reason="")
                return
            t.on_answer(sess, bool(msg.get("ok")), msg.get("offset", 0), str(msg.get("reason") or "")[:120])
        elif kind == "progress":
            t = self.outgoing.get(tid)
            if t is not None and not t.terminal():
                t.on_progress(msg.get("got", 0))
        elif kind == "complete":
            t = self.outgoing.get(tid)
            if t is not None:
                t.on_complete(msg.get("saved"))
                self.transfer_ended()
        elif kind == "cancel":
            reason = str(msg.get("reason") or "")[:120]
            if msg.get("side") == "down":
                t = self.outgoing.get(tid)
                if t is not None:
                    t.on_remote_cancel(reason)
                    sess.purge(tid)
                    self.transfer_ended()
            else:
                t = self.incoming.get((sess.peer_id, tid))
                if t is not None:
                    self.remote_cancelled_incoming(t, reason)

    def on_offer(self, sess, msg):
        tid = int(msg["tid"])
        key = (sess.peer_id, tid)
        t = self.incoming.get(key)
        if t is not None:
            if t.state == "active":
                try:
                    offset = t.prepare()
                except OSError as exc:
                    t.fail(sess, "disk error: %s" % (exc.strerror or exc))
                    return
                t.resync_sent = False
                sess.send_ctrl(t="answer", tid=tid, ok=True, offset=offset)
                if offset < t.size:
                    say("[*] Download of %s resumed at %.1f%%." % (t.name, 100.0 * offset / max(1, t.size)))
            elif t.state == "refused":
                sess.send_ctrl(t="answer", tid=tid, ok=False, reason="refused")
            elif t.state in ("cancelled", "failed"):
                sess.send_ctrl(t="cancel", tid=tid, side="down", reason="")
            elif t.state == "done":
                sess.send_ctrl(t="complete", tid=tid, saved=t.name)
            return                          # 'asking': the question is still open
        try:
            t = InTransfer(self, sess.peer_id, msg)
        except (KeyError, TypeError, ValueError, IndexError):
            sess.send_ctrl(t="answer", tid=tid, ok=False, reason="the offer was not valid")
            return
        with self.lock:
            clash = any(o.fid == t.fid and o.state in ("asking", "active") for o in self.incoming.values())
        if clash:
            sess.send_ctrl(t="answer", tid=tid, ok=False, reason="the same file is already being downloaded")
            return
        t.have = t.existing_bytes()
        bid = re.sub(r"[^0-9A-Za-z]", "", str(msg.get("batch") or ""))[:32] or ("solo%d" % tid)
        try:
            expected = max(1, min(int(msg.get("bn") or 1), 1000000))
        except (TypeError, ValueError):
            expected = 1
        gkey = (sess.peer_id, bid)
        with self.lock:
            t.num = self.next_num()
            self.incoming[key] = t
            g = self.groups.get(gkey)
            if g is None:
                g = OfferGroup(sess.peer_id, bid)
                self.groups[gkey] = g
            g.expected = expected
            g.last = time.monotonic()
            t.group = g
            state = g.state
            if state != "decided":
                g.items.append(t)
            ready = state == "collecting" and len(g.items) >= g.expected
        if state == "decided":
            self.apply_decision(t, g.decision, quiet=False)    # late part of an answered offer
        elif state == "asking":
            say("[*] One more item joined the offer: %s (%s). Your answer covers it too." % (t.name, human(t.size)))
        elif ready:
            self.present(g)

    def group_progress(self, g):
        """After an item of a multi-item offer ends: one summary when the whole offer is finished."""
        with self.lock:
            if g.state != "decided" or g.decision != "y" or g.summarized:
                return
            if any(not t.terminal() for t in g.items):
                return
            g.summarized = True
            done = len([t for t in g.items if t.state == "done"])
            total = len(g.items)
        say("[+] Finished: %d of %d items downloaded. Saved in: %s" % (done, total, self.download_dir))

    def collect_overdue(self):
        """Show offers whose remaining parts did not arrive in time."""
        now = time.monotonic()
        with self.lock:
            ready = [g for g in self.groups.values() if g.state == "collecting" and now - g.last > GROUP_WAIT]
        for g in ready:
            self.present(g)

    def present(self, g):
        with self.lock:
            if g.state != "collecting":
                return
            items = g.live()
            if not items:
                g.state, g.decision = "decided", "n"
                return
            need = sum(max(0, t.size - t.have) for t in items)
            free = disk_free(self.download_dir)
            nospace = free is not None and need > free
            if nospace:
                g.state, g.decision = "decided", "n"
            else:
                g.state = "asking"
                self.prompts.append(g)
                first = len(self.prompts) == 1
        if nospace:
            for t in items:
                self.apply_decision(t, "n", quiet=True, reason="not enough free disk space on the receiver")
            say("[x] %s (%s) was offered, but there is not enough free disk space (%s free). Refused."
                % (g.title(), human(need), human(free)))
            return
        if first and self.menu is None:
            self.show_prompt()
        else:
            say("[*] Another offer arrived: %s (%s). It will be asked %s."
                % (g.title(), human(g.size()), "after the current one" if not first else "when you finish here"))
            CONSOLE.set_prompt(CONSOLE.prompt)

    @staticmethod
    def type_text(t):
        if t.kind == "folder":
            return "folder, %d file(s)" % len(t.entries)
        return (os.path.splitext(t.name)[1].lstrip(".").upper() or "unknown") + " file"

    def show_prompt(self):
        if self.menu is not None:          # the stop menu is open; the question comes back after it
            return
        while True:
            with self.lock:
                g = self.prompts[0] if self.prompts else None
                items = g.live() if g is not None else []
                if g is not None and not items:      # everything in it was withdrawn
                    self.prompts.popleft()
                    g.state, g.decision = "decided", "n"
                    continue
                waiting = len(self.prompts) - 1
            break
        if g is None:
            CONSOLE.set_prompt("> ")
            return
        if not g.answers:
            who = self.session.peer_name if self.session else "the other user"
            lines = ["", "=" * 56]
            if len(items) == 1:
                t = items[0]
                lines += ["Incoming %s from %s:" % (t.kind, who),
                          "  Name : %s" % t.name,
                          "  Type : %s" % self.type_text(t),
                          "  Size : %s (%s bytes)" % (human(t.size), "{:,}".format(t.size))]
                if t.have:
                    lines.append("  Note : %.1f%% was already downloaded earlier; it will resume."
                                 % (100.0 * t.have / max(1, t.size)))
            else:
                lines.append("Incoming %d items from %s (%s total):" % (len(items), who, human(g.size())))
                for i, t in enumerate(items[:15], 1):
                    lines.append("  %2d. %-30s %-18s %10s" % (i, short(t.name, 30), short(self.type_text(t), 18),
                                                              human(t.size)))
                if len(items) > 15:
                    lines.append("      ... and %d more" % (len(items) - 15))
                resumed = len([t for t in items if t.have])
                if resumed:
                    lines.append("  Note : %d item(s) were partly downloaded earlier; they will resume." % resumed)
            if waiting:
                lines.append("  (%d more offer(s) waiting after this one)" % waiting)
            lines.append("=" * 56)
            say("\n".join(lines))
        CONSOLE.set_prompt(self.prompt_text(g, len(items)), announce=True)

    @staticmethod
    def prompt_text(g, count=1):
        it = "it" if count == 1 else "them"
        if not g.answers:
            return "Do you want to download %s? (y/n): " % it
        last = g.answers[-1]
        word = "download" if last == "y" else "refuse"
        if len(g.answers) == 1:
            return "Are you sure? Press %s again to %s %s (y/n): " % (last, word, it)
        return "You changed your answer. Press %s again to %s %s (y/n): " % (last, word, it)

    def answer_prompt(self, line):
        with self.lock:
            g = self.prompts[0] if self.prompts else None
        if g is None:
            return
        a = line.strip().lower()
        if a == "yes":
            a = "y"
        if a == "no":
            a = "n"
        if a not in ("y", "n"):
            say("Please type y or n.")
            CONSOLE.set_prompt(self.prompt_text(g, len(g.live())), announce=True)
            return
        g.answers.append(a)
        decision = None
        if len(g.answers) >= 2 and g.answers[-1] == g.answers[-2]:
            decision = a
        elif len(g.answers) >= 4:
            decision = "n"
            say("[x] The answers kept changing, so this counts as a refusal.")
        if decision is None:
            CONSOLE.set_prompt(self.prompt_text(g, len(g.live())), announce=True)
            return
        with self.lock:
            if self.prompts and self.prompts[0] is g:
                self.prompts.popleft()
            g.state, g.decision = "decided", decision
            items = g.live()
        self.decide(items, decision)
        self.show_prompt()

    def decide(self, items, decision):
        if not items:
            return
        reason = "refused"
        if decision == "y":
            need = sum(max(0, t.size - t.have) for t in items)
            free = disk_free(self.download_dir)
            if free is not None and need > free:
                decision, reason = "n", "not enough free disk space on the receiver"
                say("[x] Not enough free disk space anymore (%s needed, %s free). Refused." % (human(need), human(free)))
        many = len(items) > 1
        if many:
            if decision == "y":
                say("[+] Downloading %d items (%s). Press Ctrl+Z (or type cancel) to stop."
                    % (len(items), human(sum(t.size for t in items))))
            elif reason == "refused":
                say("[x] You refused to download %d items." % len(items))
        for t in items:
            self.apply_decision(t, decision, quiet=many, reason=reason)

    def apply_decision(self, t, decision, quiet=False, reason="refused"):
        sess = self.session if (self.session is not None and self.session.peer_id == t.peer_id) else None
        with self.lock:
            if t.state != "asking":
                return
        if decision == "y":
            free = disk_free(self.download_dir)
            if free is not None and t.size - t.have > free:
                decision, reason = "n", "not enough free disk space on the receiver"
                say("[x] Not enough free disk space for %s. Refused." % t.name)
        if decision == "y":
            try:
                offset = t.prepare()
            except OSError as exc:
                t.state = "failed"
                say("[!] Cannot write to %s: %s" % (self.download_dir, exc.strerror or exc))
                if sess:
                    sess.send_ctrl(t="answer", tid=t.tid, ok=False, reason="the receiver cannot write to its download folder")
                return
            t.state = "active"
            t.started = time.monotonic()
            if not quiet:
                say("[+] Downloading %s (%s)%s. Press Ctrl+Z (or type cancel) to stop."
                    % (t.name, human(t.size), (" from %.1f%%" % (100.0 * offset / max(1, t.size))) if offset else ""))
            if sess:
                sess.send_ctrl(t="answer", tid=t.tid, ok=True, offset=offset)
            if t.size == 0 or offset >= t.size:
                t.complete(sess)
        else:
            t.state = "refused"
            if not quiet and reason == "refused":
                say("[x] You refused to download %s." % t.name)
            if sess:
                sess.send_ctrl(t="answer", tid=t.tid, ok=False, reason=reason)
        self.transfer_ended()

    def remote_cancelled_incoming(self, t, reason):
        with self.lock:
            if t.terminal():
                return
            asking = t.state == "asking"
            t.state = "cancelled"
        if asking:
            say("[x] The other user withdrew the offer of %s." % t.name)
            with self.lock:
                g = self.prompts[0] if self.prompts else None
            if g is not None and t in g.items:
                self.show_prompt()
            return
        t.discard()
        if reason:
            say("[x] Download of %s stopped: %s." % (t.name, reason))
        else:
            say("[x] File downloading stopped because the other user interrupted it (%s)." % t.name)
        if t.group is not None and len(t.group.items) > 1:
            self.group_progress(t.group)
        self.transfer_ended()

    def send_cancel(self, tid, side, reason, peer_id=None):
        sess = self.session
        if sess is not None and sess.alive and (peer_id is None or sess.peer_id == peer_id):
            sess.send_ctrl(t="cancel", tid=tid, side=side, reason=reason)
        else:
            with self.lock:
                self.pending_cancels.append((peer_id or self.last_peer_id, tid, side, reason))

    # ---- user actions ----
    def cancel_incoming(self, t):
        with t.lock:
            if t.state != "active":
                return False
            t.state = "cancelled"
        t.discard()
        self.send_cancel(t.tid, "down", "", peer_id=t.peer_id)
        say("[x] You interrupted the downloading of %s." % t.name)
        if t.group is not None and len(t.group.items) > 1:
            self.group_progress(t.group)
        return True

    def cancel_all_now(self):
        """Stop everything without asking (used only by tests and scripts)."""
        for _d, t in self.running_list():
            self.stop_one(t)
        self.transfer_ended()

    def running_list(self):
        """Transfers that can be stopped, numbered 1.. in the order they started."""
        rows = []
        for t in list(self.outgoing.values()):
            if not t.terminal() and t.state != "preparing":
                rows.append(("SENDING", t))
        for t in list(self.incoming.values()):
            if t.state == "active":
                rows.append(("RECEIVING", t))
        rows.sort(key=lambda r: r[1].num)
        return rows

    def row_state(self, d, t):
        if d == "RECEIVING":
            return "%.1f%%" % (100.0 * t.written / t.size if t.size else 100.0)
        if t.state == "sending" or (t.accepted_once and t.state != "queued"):
            return "%.1f%%" % (100.0 * t.acked / t.size if t.size else 100.0)
        return {"waiting": "waiting for connection", "offered": "waiting for answer",
                "queued": "in line"}.get(t.state, t.state)

    def row_text(self, i, d, t):
        return "  %2d. %-9s  %-28s %-18s %10s   %s" % (i, d, short(t.name, 28), short(self.type_text(t), 18),
                                                     human(t.size), self.row_state(d, t))

    def stop_one(self, t):
        if t.terminal():
            return False
        if isinstance(t, OutTransfer):
            return t.cancel_local()
        return self.cancel_incoming(t)

    def open_stop_menu(self, preset=None):
        """Ctrl+Z / cancel: list running transfers, let the user pick, confirm once."""
        rows = self.running_list()
        if not rows:
            say("[*] Nothing is uploading or downloading right now.")
            return
        lines = ["", "-" * 56, "Running transfers:"]
        lines += [self.row_text(i, d, t) for i, (d, t) in enumerate(rows, 1)]
        lines.append("-" * 56)
        with self.lock:
            self.menu = {"stage": "pick", "rows": rows}
        if preset is None:
            lines.append("Type the numbers to stop (like 2, or 2 3 4), all for everything, or just Enter to keep them.")
            say("\n".join(lines))
            CONSOLE.set_prompt("Which to stop? ", announce=True)
        else:
            say("\n".join(lines))
            self.menu_input(preset)

    def menu_input(self, line):
        m = self.menu
        if m is None:
            return
        rows = m["rows"]
        if m["stage"] == "pick":
            text = line.strip().lower()
            if text in ("", "0", "q", "none", "back", "n", "no"):
                self.close_menu("[*] Nothing was stopped.")
                return
            if text in ("all", "a", "*"):
                picks = list(range(1, len(rows) + 1))
            else:
                picks, bad = [], []
                for tok in re.split(r"[\s,;]+", text.lstrip("#")):
                    if not tok:
                        continue
                    tok = tok.lstrip("#")
                    if "-" in tok and tok.replace("-", "").isdigit() and tok.count("-") == 1 and tok[0] != "-":
                        lo, hi = (int(x) for x in tok.split("-"))     # 2-4 means 2 3 4
                        nums = list(range(lo, hi + 1)) if lo <= hi else []
                    elif tok.isdigit():
                        nums = [int(tok)]
                    else:
                        nums = []
                    if not nums or any(n < 1 or n > len(rows) for n in nums):
                        bad.append(tok)
                        continue
                    for n in nums:
                        if n not in picks:
                            picks.append(n)
                if bad or not picks:
                    say("[!] %s is not on the list. Type numbers from 1 to %d with spaces between them, like 2 3."
                        % (", ".join(bad) or "That", len(rows)))
                    CONSOLE.set_prompt(CONSOLE.prompt, announce=True)
                    return
            picks.sort()
            chosen = [rows[n - 1] for n in picks]
            m["stage"], m["chosen"] = "confirm", chosen
            if len(chosen) == 1:
                d, t = chosen[0]
                q = "Stop %s %s? (y/n): " % ("sending" if d == "SENDING" else "receiving", short(t.name, 30))
            else:
                say("You picked:\n" + "\n".join(self.row_text(n, d, t) for n, (d, t) in zip(picks, chosen)))
                q = "Stop these %d transfers? (y/n): " % len(chosen)
            CONSOLE.set_prompt(q, announce=True)
            return
        a = line.strip().lower()
        if a in ("y", "yes"):
            chosen = m["chosen"]
            with self.lock:
                self.menu = None
            for d, t in chosen:
                if t.terminal():
                    say("[*] %s already finished, so it was not stopped." % t.name)
                else:
                    self.stop_one(t)
            self.transfer_ended()
            self.close_menu(None)
        elif a in ("n", "no"):
            self.close_menu("[*] Nothing was stopped. Transfers keep going.")
        else:
            say("Please type y or n.")
            CONSOLE.set_prompt(CONSOLE.prompt, announce=True)

    def close_menu(self, message):
        with self.lock:
            self.menu = None
        if message:
            say(message)
        self.show_prompt()               # back to the y/n download question, or the normal prompt

    def transfer_ended(self):
        CONSOLE.set_status(self.status_line())

    def upload(self, arg):
        if not arg.strip():
            say("Usage: upload <file or folder> [more files or folders...]")
            return
        paths, missing = parse_paths(arg)
        for m in missing:
            say("[!] Not found: %s" % m)
        if not paths:
            return
        if len(paths) > MAX_BATCH:
            say("[!] That is %d items; one upload can offer up to %d. Put them in a folder and upload the folder "
                "(any number of files), or upload them in parts." % (len(paths), MAX_BATCH))
            return
        with self.lock:
            busy = set(os.path.normcase(t.path) for t in self.outgoing.values() if not t.terminal())
        items = []
        for p in paths:
            ap = os.path.abspath(p)
            if os.path.normcase(ap) in busy:
                say("[!] %s is already being sent." % (os.path.basename(ap.rstrip("\\/")) or ap))
                continue
            busy.add(os.path.normcase(ap))
            items.append(OutTransfer(self, ap))
        if not items:
            return
        batch = Batch(items)
        with self.lock:
            for t in items:
                t.batch = batch
                t.num = self.next_num()
                self.outgoing[t.tid] = t
        threading.Thread(target=self._start_batch, args=(batch,), daemon=True).start()

    def _start_batch(self, batch):
        ok = [t for t in batch.members if t.prepare()]
        batch.members = ok
        if not ok:
            self.transfer_ended()
            return
        if len(ok) > 1:
            if self.session_alive():
                say("[*] Offered %s. Waiting for the other user to answer..." % batch.describe())
            else:
                say("[*] %s will be offered as soon as the other user is connected." % batch.describe())
        for t in ok:
            threading.Thread(target=t.run, daemon=True).start()

    @staticmethod
    def _agg(label, rows, note):
        size = sum(r[1] for r in rows)
        done = sum(r[2] for r in rows)
        rate = sum(r[3] for r in rows)
        pct = 100.0 * done / size if size else 100.0
        eta = (size - done) / rate if rate > 0 else None
        name = short(rows[0][0], 18) if len(rows) == 1 else "%d items" % len(rows)
        return "%s %s %.1f%% %s/%s %s/s ETA %s%s" % (label, name, pct, human(done), human(size),
                                                     human(rate), fmt_eta(eta), note)

    def status_line(self):
        alive = self.session_alive()
        ups = [t for t in list(self.outgoing.values()) if t.accepted_once and not t.terminal()]
        downs = [t for t in list(self.incoming.values()) if t.state == "active"]
        parts = []
        if ups:
            note = "" if alive else " paused"
            if alive and len(ups) == 1 and ups[0].state == "queued":
                note = " queued"
            parts.append(self._agg("UP", [(t.name, t.size, t.acked, t.meter.rate()) for t in ups], note))
        if downs:
            parts.append(self._agg("DOWN", [(t.name, t.size, t.written, t.meter.rate()) for t in downs],
                                   "" if alive else " paused"))
        return " | ".join(parts)

    def print_status(self):
        s = self.session
        if s is not None and s.alive:
            say("[*] Connected to %s via %s." % (s.peer_name, s.label))
        else:
            say("[*] Not connected. %s" % ("Trying %s" % self.dialer.raw if self.dialer.has_target()
                                            else "Waiting for the other user to connect."))
        say("[*] Listening on port %d. Downloads folder: %s" % (self.port, self.download_dir))
        rows = self.running_list()
        asking = [t for t in list(self.incoming.values()) if t.state == "asking"]
        if not rows and not asking:
            say("[*] Transfers: none running.")
            return
        if rows:
            say("[*] Running transfers (Ctrl+Z or cancel to stop some):")
            for i, (d, t) in enumerate(rows, 1):
                say(self.row_text(i, d, t))
        for t in asking:
            say("    waiting for your y/n answer: %s (%s)" % (t.name, human(t.size)))

    def help(self):
        say("Commands:\n"
            "  upload <files/folders>   offer one or more files/folders (any type, any size)\n"
            "                           e.g. upload movie.mp4 \"My Photos\" notes.pdf   or   upload *.jpg\n"
            "  Ctrl+Z  or cancel        choose which uploads/downloads to stop (2, or 2 3 4, or all)\n"
            "  cancel 2 3               stop transfers 2 and 3 directly (numbers from status)\n"
            "  connect <address>        connect to a new address (e.g. a new tunnel URL)\n"
            "  status                   connection and every transfer with its number\n"
            "  exit    or Ctrl+C        quit\n"
            "You can upload more at any time, even while other files are still moving.")

    def handle_line(self, line):
        if self.menu is not None:
            if line.strip().lower() in ("exit", "quit"):
                raise KeyboardInterrupt
            self.menu_input(line)
            return
        with self.lock:
            asking = bool(self.prompts)
        if asking:
            word = line.strip().split(" ", 1)[0].lower()
            if word not in ("cancel", "stop", "exit", "quit", "upload", "send", "status", "list", "help", "connect"):
                self.answer_prompt(line)
                return
        cmd, _, arg = line.strip().partition(" ")
        c = cmd.lower()
        if not c:
            return
        if c in ("y", "n", "yes", "no") and not arg:
            say("[*] No download question is open right now.")
        elif c in ("upload", "up", "send", "u"):
            self.upload(arg)
        elif c in ("cancel", "stop"):
            self.open_stop_menu(arg.strip() or None)
        elif c in ("exit", "quit", "q"):
            raise KeyboardInterrupt
        elif c in ("status", "list", "ls"):
            self.print_status()
        elif c == "connect":
            if arg.strip():
                self.dialer.set_target(arg.strip(), announce=True)
            else:
                say("Usage: connect <address or URL>")
        elif c in ("help", "?", "h"):
            self.help()
        else:
            paths, missing = parse_paths(line)
            if paths and not missing:
                self.upload(line)          # dragged files in without typing 'upload'
            else:
                say("Unknown command. Type help to see the commands.")

    # ---- lifecycle ----
    def ticker(self):
        while not self.stopping:
            time.sleep(1.0 if CONSOLE.tty else 5.0)
            try:
                CONSOLE.set_status(self.status_line())
            except Exception:
                pass

    def watchdog(self):
        while not self.stopping:
            time.sleep(0.5)
            now = time.monotonic()
            with self.lock:
                sessions = list(self.sessions)
            for s in sessions:
                if s.alive and s.attached and now - s.last_rx > s.conn.dead_after:
                    s.kill()
            try:
                self.collect_overdue()
            except Exception:
                pass

    def shutdown(self):
        self.stopping = True
        self.listener.stop()
        sess = self.session
        if sess is not None and sess.alive:
            sess.send_ctrl(t="bye")
            sess.flush_and_close(0.6)
        with self.lock:
            sessions = list(self.sessions)
        for s in sessions:
            s.kill()
        for t in list(self.incoming.values()):
            t.close_files()
        self.listener.stop()

    def run(self):
        threading.Thread(target=self.listener.run, daemon=True).start()
        threading.Thread(target=self.dialer.run, daemon=True).start()
        threading.Thread(target=self.ticker, daemon=True).start()
        threading.Thread(target=self.watchdog, daemon=True).start()
        self.editor = LineEditor(self.ctrl_z)
        try:
            while True:
                try:
                    line = self.editor.readline()
                except EOFError:
                    if self.editor.keys.mode != "line":
                        raise KeyboardInterrupt
                    while True:              # no keyboard (e.g. started in background): keep serving
                        time.sleep(3600)
                self.handle_line(line)
        finally:
            self.editor.close()

    def ctrl_z(self):
        threading.Thread(target=self.open_stop_menu, daemon=True).start()


# ======================================================================
# Listening side: one port that understands every connection type
# ======================================================================
HTTP_METHODS = (b"GET ", b"POST", b"HEAD", b"PUT ", b"OPTI", b"DELE", b"PATC", b"CONN")


class Listener(object):
    def __init__(self, app):
        self.app = app
        self.srv = None
        self.stopped = False
        self.polls = {}
        self.lock = threading.Lock()

    def stop(self):
        self.stopped = True
        if self.srv is not None:
            try:
                self.srv.close()
            except OSError:
                pass

    def run(self):
        delay, warned = 1.0, False
        while not self.stopped:
            try:
                self.srv = make_listener(self.app.port)
                break
            except OSError as exc:
                if not warned:
                    warned = True
                    say("[!] Port %d is busy (%s). Retrying in the background; "
                        "connecting out still works." % (self.app.port, exc.strerror or exc))
                time.sleep(delay)
                delay = min(delay * 1.5, 3.0)
        if self.stopped:
            return
        if warned:
            say("[+] Now listening on port %d." % self.app.port)
        self.srv.settimeout(1.0)
        while not self.stopped:
            try:
                sock, addr = self.srv.accept()
            except socket.timeout:
                continue
            except OSError:
                if self.stopped:
                    break
                time.sleep(0.2)
                continue
            threading.Thread(target=self.handle, args=(sock, addr), daemon=True).start()

    def handle(self, raw, addr):
        sock = raw
        try:
            tune_socket(raw)
            raw.settimeout(SNIFF_TIMEOUT)
            try:
                first = raw.recv(1, socket.MSG_PEEK)
            except (OSError, ValueError):
                return
            if not first:
                return
            tls = False
            if first == b"\x16":
                ctx = server_ctx()
                if ctx is None:
                    return
                try:
                    sock = ctx.wrap_socket(raw, server_side=True)
                except (ssl.SSLError, OSError, ValueError):
                    return
                tls = True
            buf = bytearray()
            while len(buf) < len(MAGIC):
                if len(buf) >= 4 and not MAGIC.startswith(bytes(buf)):
                    break
                d = sock.recv(64)
                if not d:
                    return
                buf += d
            remote_private = is_lan_peer(str(addr[0]))
            if bytes(buf[:len(MAGIC)]) == MAGIC:
                conn = SockConn(sock, "TLS-encrypted TCP" if tls else "TCP", bytes(buf[len(MAGIC):]))
                sess = Session(self.app, conn, dialer=False)
                sess.remote_private = remote_private
                try:
                    self.app.run_session(sess, magic_seen=True)
                except Exception:
                    pass
                return
            if bytes(buf[:4]) in HTTP_METHODS:
                self.http(sock, bytes(buf), tls, remote_private)
        except Exception:
            pass
        finally:
            close_sock(sock)
            if sock is not raw:
                close_sock(raw)

    # ---- HTTP: WebSocket upgrade, polling transport, and a friendly page ----
    def http(self, sock, pre, tls, remote_private):
        conn = SockConn(sock, "http", pre)
        rd = Reader(conn, b"")
        while not self.stopped:
            try:
                head = rd.until(b"\r\n\r\n", 65536, time.monotonic() + HTTP_IDLE)
            except Exception:
                return
            first, headers = parse_head(head)
            if len(first) < 3:
                return
            method, target, version = first[0].upper(), first[1], first[2].upper()
            body = self._read_body(rd, headers)
            if body is None:
                return
            path, _, query = target.partition("?")
            if "://" in path:                        # absolute-form request
                path = "/" + path.split("://", 1)[1].partition("/")[2]
            params = dict(p.partition("=")[::2] for p in query.split("&") if p)
            keep = version == "HTTP/1.1" and headers.get("connection", "").lower() != "close"
            if keep is False and "keep-alive" in headers.get("connection", "").lower():
                keep = True
            if path == "/fs/ws" and "websocket" in headers.get("upgrade", "").lower():
                key = headers.get("sec-websocket-key", "")
                accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
                conn.send(http_head(["HTTP/1.1 101 Switching Protocols", "Upgrade: websocket",
                                     "Connection: Upgrade", "Sec-WebSocket-Accept: " + accept,
                                     MARK_HEADER + ": 1"]))
                ws = WSConn(conn, False, "WebSocket over HTTPS" if tls else "WebSocket over HTTP", bytes(rd.buf))
                sess = Session(self.app, ws, dialer=False)
                sess.remote_private = remote_private
                try:
                    self.app.run_session(sess, magic_seen=False)
                except Exception:
                    pass
                return
            if path.startswith("/fs/p/"):
                status, seq, data = self._poll(path, params, method, body, tls, remote_private)
            else:
                status, seq, data = 200, None, ("%s is running. Use file-share.py to connect.\n" % APP_NAME).encode()
            reason = {200: "OK", 404: "Not Found", 410: "Gone", 400: "Bad Request"}.get(status, "OK")
            lines = ["HTTP/1.1 %d %s" % (status, reason),
                     "Content-Type: %s" % ("application/octet-stream" if path.startswith("/fs/") else "text/plain"),
                     "Content-Length: %d" % (len(data) if method != "HEAD" else 0),
                     "Cache-Control: no-store", MARK_HEADER + ": 1",
                     "Connection: %s" % ("keep-alive" if keep else "close")]
            if seq is not None:
                lines.append("X-Seq: %d" % seq)
            try:
                conn.send(http_head(lines) + (data if method != "HEAD" else b""))
            except Exception:
                return
            if not keep:
                return

    @staticmethod
    def _read_body(rd, headers):
        try:
            deadline = time.monotonic() + 120
            if "chunked" in headers.get("transfer-encoding", "").lower():
                out = bytearray()
                while True:
                    line = rd.until(b"\r\n", 1024, deadline)
                    size = int(line.split(b";")[0].strip() or b"0", 16)
                    if size == 0:
                        while rd.until(b"\r\n", 8192, deadline) != b"\r\n":
                            pass
                        return bytes(out)
                    if len(out) + size > MAX_FRAME:
                        return None
                    out += rd.exact(size, deadline)
                    rd.exact(2, deadline)
            n = int(headers.get("content-length", "0") or 0)
            if n < 0 or n > MAX_FRAME:
                return None
            return rd.exact(n, deadline) if n else b""
        except Exception:
            return None

    def _poll(self, path, params, method, body, tls, remote_private):
        parts = path.strip("/").split("/")     # fs, p, new | fs, p, sid, op
        if len(parts) == 3 and parts[2] == "new" and method == "POST":
            pc = PollServerConn("HTTPS polling" if tls else "HTTP polling")
            with self.lock:
                for sid in [s for s, c in self.polls.items() if c.closed]:
                    del self.polls[sid]
                self.polls[pc.sid] = pc
            sess = Session(self.app, pc, dialer=False)
            sess.remote_private = remote_private

            def go():
                try:
                    self.app.run_session(sess, magic_seen=False)
                except Exception:
                    pass
                finally:
                    pc.close()
            threading.Thread(target=go, daemon=True).start()
            return 200, None, pc.sid.encode()
        if len(parts) != 4:
            return 404, None, b""
        with self.lock:
            pc = self.polls.get(parts[2])
        if pc is None:
            return 404, None, b""
        try:
            if parts[3] == "up":
                return pc.handle_up(int(params.get("seq", "0")), body), None, b""
            if parts[3] == "down":
                status, seq, data = pc.handle_down(int(params.get("ack", "0")))
                return status, seq, data
            if parts[3] == "close":
                pc.close()
                return 200, None, b""
        except ValueError:
            return 400, None, b""
        return 404, None, b""


# ======================================================================
# Connecting side: works out the right protocol by itself
# ======================================================================
def parse_target(raw, default_port):
    """IP, hostname, host:port, [v6]:port, or scheme://host[:port]/... -> (host, port, scheme, explicit_port)."""
    s = str(raw).strip().strip("\"'")
    scheme = None
    if "://" in s:
        scheme, s = s.split("://", 1)
        scheme = scheme.strip().lower()
        scheme = {"ws": "http", "wss": "https", "tls": "https", "ssl": "https"}.get(scheme, scheme)
        if scheme not in ("tcp", "http", "https"):
            scheme = None
    s = s.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if "@" in s:
        s = s.rsplit("@", 1)[1]
    host, port = s, None
    if s.startswith("["):
        end = s.find("]")
        host = s[1:end] if end > 0 else s.strip("[]")
        rest = s[end + 1:] if end > 0 else ""
        if rest.startswith(":") and rest[1:].isdigit():
            port = int(rest[1:])
    elif s.count(":") == 1:
        h, p = s.split(":")
        if p.isdigit():
            host, port = h, int(p)
    host = host.strip().rstrip(".")
    explicit = port is not None
    if port is None:
        port = {"https": 443, "http": 80}.get(scheme, default_port)
    return host, port, scheme, explicit


def candidates(port, scheme, explicit):
    """(protocol, port) to try, most likely first."""
    if scheme == "tcp":
        order = ["tcp", "tls", "ws", "wss", "poll", "polls"]
    elif scheme == "http":
        order = ["ws", "poll", "wss", "polls", "tcp", "tls"]
    elif scheme == "https":
        order = ["wss", "polls", "tls", "ws", "poll", "tcp"]
    elif port == 443:
        order = ["wss", "tls", "polls", "tcp", "ws", "poll"]
    elif port == 80:
        order = ["ws", "poll", "tcp", "wss", "tls", "polls"]
    else:
        order = ["tcp", "tls", "ws", "wss", "poll", "polls"]
    out = [(p, port) for p in order]
    if scheme is None and not explicit:
        out += [("wss", 443), ("ws", 80), ("polls", 443), ("poll", 80)]   # bare tunnel hostname
    only = os.environ.get("FILESHARE_PROTO", "").strip().lower()     # testing aid: force one protocol
    if only:
        out = [c for c in out if c[0] in only.split(",")] or out
    return out


class Dialer(object):
    def __init__(self, app):
        self.app = app
        self.raw = ""
        self.host = None
        self.cands = []
        self.best = None
        self.holder = []
        self.event = threading.Event()
        self.self_target = False
        self.busy_noted = False
        self.lock = threading.Lock()
        self.gen = 0
        self.last_probe = time.monotonic()
        self.scheme = None
        self.fails = 0
        self.fail_since = None
        self.told_soft = False

    def has_target(self):
        return bool(self.host) and not self.self_target

    def metered(self):
        """Tunnels that count every connection/request against a monthly quota (ngrok free:
        5,000 TCP connections, 20,000 HTTP requests): retry more gently there."""
        return bool(self.host) and re.search(r"(^|\.)ngrok(-free)?\.(app|dev|io)$", self.host.lower()) is not None

    def target_private(self):
        return bool(self.host) and is_private_host(self.host)

    def wake(self):
        self.event.set()

    def note_busy(self):
        if not self.busy_noted:
            self.busy_noted = True
            say("[!] The other computer is already sharing with someone else. Will keep trying.")

    def set_target(self, raw, announce):
        host, port, scheme, explicit = parse_target(raw, self.app.port)
        if not host:
            say("[!] That address looks empty.")
            return
        with self.lock:
            self.raw = raw
            self.host = host
            self.cands = candidates(port, scheme, explicit)
            self.scheme = scheme
            self.fails = 0
            self.fail_since = None
            self.told_soft = False
            self.best = None
            self.holder = []
            self.self_target = False
            self.busy_noted = False
            self.gen += 1
        if announce:
            say("[*] Will connect to %s (protocol detected automatically)." % raw)
            s = self.app.session
            if s is not None and s.alive and s.dialer:
                s.kill()             # switch now; transfers resume on the new link
        self.event.set()

    def run(self):
        delay = 1.0
        announced = False
        while not self.app.stopping:
            if not self.has_target() or self.app.session_alive():
                self.maybe_upgrade()
                self.event.wait(1.0)
                self.event.clear()
                continue
            with self.lock:
                gen, host = self.gen, self.host
                order = list(self.cands)
                if self.best in order:
                    order.remove(self.best)
                    order.insert(0, self.best)
                    if self.fails and self.fails % 6:
                        order = [self.best]       # reconnecting: the type that worked; full scan every 6th try
            if not announced:
                announced = True
                say("[*] Connecting to %s ..." % self.raw)
            result = self.attempt(host, order, gen)
            if result == "ok" or result == "dup":
                delay = 1.0
                announced = True
                self.fails, self.fail_since, self.told_soft = 0, None, False
                self.event.wait(1.0)
                self.event.clear()
                continue
            if result == "self":
                say("[*] %s points to this computer itself (your own tunnel). Waiting for the other "
                    "user to connect to you." % self.raw)
                continue
            if result == "soft" and not self.told_soft:
                self.told_soft = True
                say("[*] The tunnel is up, but %s is not running on the other side yet. Waiting for it..." % APP_NAME)
            self.fails += 1
            if self.fail_since is None:
                self.fail_since = time.monotonic()
            cap = RETRY_MAX
            if self.metered():
                cap = 30.0 if time.monotonic() - self.fail_since < 600 else 60.0
            self.event.wait(delay)
            self.event.clear()
            delay = min(delay * 2, cap)

    def maybe_upgrade(self):
        """On the slow HTTP-polling fallback, check now and then whether a WebSocket works
        (e.g. the other app was still starting when we first connected) and switch to it."""
        s = self.app.session
        if s is None or not s.alive or not s.dialer or not isinstance(s.conn, PollClientConn):
            return
        now = time.monotonic()
        if now - self.last_probe < 60.0:
            return
        self.last_probe = now
        with self.lock:
            host, gen = self.host, self.gen
            order = [c for c in self.cands if c[0] in ("ws", "wss")]
        for proto, port in order:
            try:
                conn = ws_client(host, port, proto == "wss", list(self.holder))
            except Exception:
                continue
            conn.close()
            with self.lock:
                if gen != self.gen:
                    return
                self.best = (proto, port)
            say("[*] A faster connection type is available; switching (transfers continue).")
            s.kill()
            self.event.set()
            return

    def attempt(self, host, order, gen):
        down_ports = set()
        soft_eof = False
        for proto, port in order:
            if self.app.stopping or gen != self.gen or self.app.session_alive():
                return "stale"
            if port in down_ports:
                continue
            if soft_eof and proto in ("poll", "polls"):
                continue      # the app behind the tunnel may still be starting: retry soon instead
            try:
                conn = self.open(proto, host, port)
            except HostDown:
                return "down"
            except PortDown:
                down_ports.add(port)
                continue
            except TunnelNoApp:
                return "soft"                 # an HTTP tunnel answered for the missing app: stop here
            except SoftEOF:
                soft_eof = True
                if self.scheme == "tcp" and proto == "tcp":
                    return "soft"             # a TCP tunnel with nothing behind it: other types won't help
                continue
            except Exception:
                continue                      # something answered, but not this protocol
            sess = Session(self.app, conn, dialer=True)
            sess.remote_private = is_lan_peer(host)
            with self.app.lock:
                self.app.sessions.add(sess)
            try:
                sess.handshake(magic_seen=False)
                if sess.peer_id == INSTANCE_ID:
                    raise NotUs("self")
                if sess.peer_busy and sess.peer_busy != INSTANCE_ID:
                    raise Busy()
                if gen != self.gen:
                    raise NotUs("address changed meanwhile")
                sess.start_writer()
                ok = self.app.attach(sess)
            except Exception as exc:
                sess.alive = False
                try:
                    conn.close()
                except Exception:
                    pass
                with self.app.lock:
                    self.app.sessions.discard(sess)
                if sess.peer_id == INSTANCE_ID:
                    with self.lock:
                        if gen == self.gen:
                            self.self_target = True
                    return "self"
                if isinstance(exc, Busy):
                    self.note_busy()
                    return "busy"
                if isinstance(exc, SoftEOF):
                    soft_eof = True
                    if isinstance(exc, TunnelNoApp) or (self.scheme == "tcp" and proto == "tcp"):
                        return "soft"
                continue
            if not ok:
                sess.flush_and_close(0.3)
                return "dup"
            with self.lock:
                self.best = (proto, port)
                self.busy_noted = False
            threading.Thread(target=sess.run_reader, daemon=True).start()
            return "ok"
        return "soft" if soft_eof else "fail"

    def open(self, proto, host, port):
        if proto in ("tcp", "tls"):
            if proto == "tls":
                sock = connect_tls(host, port, self.holder)
                label = "TLS-encrypted TCP"
            else:
                sock = connect_tcp(host, port)
                label = "TCP"
            return SockConn(sock, label)
        if proto in ("ws", "wss"):
            return ws_client(host, port, proto == "wss", self.holder)
        return PollClientConn(host, port, proto == "polls", self.holder).open()


def is_lan_peer(host):
    """A real LAN neighbour (not this machine and not a local tunnel agent)."""
    h = host.replace("::ffff:", "").lower()
    if h in ("localhost", "::1") or h.startswith("127."):
        return False
    return is_private_host(h)


# ======================================================================
# Main
# ======================================================================
def pick_download_dir(configured):
    candidates_ = []
    if configured:
        candidates_.append(os.path.abspath(os.path.expanduser(configured)))
    try:
        here = os.path.dirname(os.path.abspath(sys.argv[0] if sys.argv and sys.argv[0] else __file__))
    except Exception:
        here = os.getcwd()
    candidates_ += [os.path.join(here, "FileShare Downloads"),
                    os.path.join(os.getcwd(), "FileShare Downloads"),
                    os.path.join(os.path.expanduser("~"), "FileShare Downloads"),
                    os.path.join(tempfile.gettempdir(), "FileShare Downloads")]
    for d in candidates_:
        try:
            os.makedirs(d, exist_ok=True)
            probe = os.path.join(d, ".write-test-%s" % INSTANCE_ID)
            with open(probe, "wb") as f:
                f.write(b"ok")
            os.remove(probe)
            return d
        except OSError:
            continue
    return candidates_[-1]


def install_ctrl_z(app):
    """POSIX terminals send SIGTSTP for Ctrl+Z: use it to cancel instead of suspending."""
    if IS_WINDOWS or not hasattr(__import__("signal"), "SIGTSTP"):
        return
    import signal
    try:
        signal.signal(signal.SIGTSTP, lambda signum, frame: app.ctrl_z())
    except (ValueError, OSError, RuntimeError):
        pass


def main():
    try:
        threading.stack_size(512 * 1024)   # many small threads stay light on phones
    except (ValueError, RuntimeError):
        pass
    peer = PEER_ADDRESS
    port = PORT
    folder = DOWNLOAD_FOLDER
    if len(sys.argv) > 1 and sys.argv[1].strip():
        peer = sys.argv[1].strip()
    peer = os.environ.get("FILESHARE_PEER", peer)
    try:
        port = int(os.environ.get("FILESHARE_PORT", port))
    except ValueError:
        pass
    folder = os.environ.get("FILESHARE_DIR", folder)

    download_dir = pick_download_dir(folder)
    app = App(peer.strip(), port, download_dir)
    install_ctrl_z(app)
    ips = lan_ips()
    say("=" * 60)
    say(" %s - send and receive files and folders of any size" % APP_NAME)
    say("=" * 60)
    say(" Listening on port %d (TCP, TLS, HTTP, HTTPS, WebSocket)" % port)
    if ips:
        say(" Same Wi-Fi? The other user can use: %s" % ", ".join("%s:%d" % (ip, port) for ip in ips))
    say(" Internet? Tunnel port %d with Pinggy or ngrok and share the address it prints." % port)
    say(" Downloads go to: %s" % download_dir)
    say(" Commands: upload <files/folders> | Ctrl+Z (stop) | status | connect <address> | help | exit (Ctrl+C)")
    say("=" * 60)
    if not peer.strip():
        say("[*] Waiting for the other user to connect...")
    try:
        app.run()
    except KeyboardInterrupt:
        pass
    finally:
        CONSOLE.set_status("")
        CONSOLE.prompt = ""
        say("\n[*] Closing %s... (unfinished downloads are kept and resume next time)" % APP_NAME)
        try:
            app.shutdown()
        except Exception:
            pass
        say("[*] Bye.")


if __name__ == "__main__":
    main()
