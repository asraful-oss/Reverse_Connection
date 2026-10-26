#!/usr/bin/env python3
"""
sender.py - Standalone Connection Host (TCP / HTTP / HTTPS)
------------------------------------------------------------
Run with:  python sender.py

One port serves every receiver type at once. In 'auto' mode the sender looks
at the first bytes of each new connection and answers it as raw TCP or HTTP
(HTTP/1.0 or 1.1). HTTPS works through any TLS-terminating tunnel (Pinggy,
ngrok, Cloudflare, a reverse proxy): the tunnel decrypts and forwards plain
HTTP here.

Cross-platform: Windows, macOS, Linux (desktop and servers), Android (Termux),
iOS (Pythonista / a-Shell). Python 3.6+. Standard library only.
"""

import io
import os
import socket
import ssl
import select
import tempfile
import threading
import time
import platform
import http.server as http_server   # aliased: the 'http' name below is a config string
from typing import Optional

# ==================== EDIT THESE THREE VARIABLES ====================
auto = 'auto'
tcp = 'tcp'
http = 'http'
https = 'https'
connection_type = auto         # <- auto / tcp / http / https (any value works: every
                               #    connection is auto-detected and served)

# The sender always listens locally, for LAN, Pinggy or ngrok alike.
DEFAULT_IP = '0.0.0.0'         # '0.0.0.0' = all interfaces (IPv4 + IPv6 when available)
DEFAULT_PORT = 4444            # Local port. Keep it when tunneling (the tunnel forwards here)
# ====================================================================

# --- Built-in TLS identity ---
# Lets the sender also answer direct HTTPS / TLS on a LAN (no tunnel). The
# receiver pins this exact certificate, so it only trusts it when talking to
# this sender; public sites are still verified normally. Through Pinggy/ngrok
# the tunnel's own valid certificate is used instead.
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

# --- Protocol (must match receiver.py) ---
HELLO = b"CMON1\n"             # TCP handshake, sent by the receiver, echoed by the sender
HEARTBEAT = b"."               # 1-byte application heartbeat, both directions
MARKER_HEADER = "X-CMon"       # HTTP header proving the reply came from this sender

# --- Timing ---
HB_INTERVAL = 3.0              # send a TCP heartbeat this often (s)
HB_TIMEOUT = 12.0              # silence longer than this = peer is gone (s)
TLS_HANDSHAKE_TIMEOUT = 10.0   # direct-TLS handshake limit (s)
SNIFF_TIMEOUT = 10.0           # wait this long for a new connection's first bytes (s)
SELECT_SLICE = 0.5             # max wait per select(); keeps Ctrl+C responsive on Windows
IO_TIMEOUT = 10.0              # per-send/recv timeout on client sockets (s)
HTTP_IDLE_TIMEOUT = 120        # free an HTTP keep-alive thread after this much silence (s)
KEEPALIVE_IDLE = 20            # OS keepalive: start probing after N idle seconds
KEEPALIVE_INTVL = 5            # OS keepalive: seconds between probes
KEEPALIVE_CNT = 3              # OS keepalive: failed probes before the OS drops the socket
USER_TIMEOUT_MS = 20000        # Linux: drop if sent data stays unacknowledged this long

_WILDCARD_HOSTS = ("", "0.0.0.0", "::", "*")


def _log(msg: str) -> None:
    print(time.strftime("%H:%M:%S ") + msg, flush=True)


# --- Cross-platform TCP tuning ---
def _setopt(level: int, name: str, value: int, sock: socket.socket) -> bool:
    code = getattr(socket, name, None)
    if code is None:
        return False
    try:
        sock.setsockopt(level, code, value)
        return True
    except OSError:
        return False


def _tune_tcp_socket(sock: socket.socket) -> None:
    """TCP_NODELAY + keepalive tuned per platform; every option guarded."""
    _setopt(socket.IPPROTO_TCP, "TCP_NODELAY", 1, sock)
    _setopt(socket.SOL_SOCKET, "SO_KEEPALIVE", 1, sock)
    system = platform.system()

    if system == "Windows":
        # Win10 1709+: per-option knobs in seconds. Older: SIO_KEEPALIVE_VALS (ms).
        ok = _setopt(socket.IPPROTO_TCP, "TCP_KEEPIDLE", KEEPALIVE_IDLE, sock)
        ok = _setopt(socket.IPPROTO_TCP, "TCP_KEEPINTVL", KEEPALIVE_INTVL, sock) and ok
        _setopt(socket.IPPROTO_TCP, "TCP_KEEPCNT", KEEPALIVE_CNT, sock)
        if not ok and hasattr(socket, "SIO_KEEPALIVE_VALS"):
            try:
                sock.ioctl(socket.SIO_KEEPALIVE_VALS,
                           (1, KEEPALIVE_IDLE * 1000, KEEPALIVE_INTVL * 1000))
            except (OSError, ValueError):
                pass
        return

    if system == "Darwin":  # macOS and iOS: TCP_KEEPALIVE (0x10) is the idle knob
        try:
            sock.setsockopt(socket.IPPROTO_TCP,
                            getattr(socket, "TCP_KEEPALIVE", 0x10), KEEPALIVE_IDLE)
        except OSError:
            pass
        _setopt(socket.IPPROTO_TCP, "TCP_KEEPINTVL", KEEPALIVE_INTVL, sock)
        _setopt(socket.IPPROTO_TCP, "TCP_KEEPCNT", KEEPALIVE_CNT, sock)
        return

    # Linux, Android (Termux), *BSD
    _setopt(socket.IPPROTO_TCP, "TCP_KEEPIDLE", KEEPALIVE_IDLE, sock)
    _setopt(socket.IPPROTO_TCP, "TCP_KEEPINTVL", KEEPALIVE_INTVL, sock)
    _setopt(socket.IPPROTO_TCP, "TCP_KEEPCNT", KEEPALIVE_CNT, sock)
    _setopt(socket.IPPROTO_TCP, "TCP_USER_TIMEOUT", USER_TIMEOUT_MS, sock)


def _set_reuse(sock: socket.socket) -> None:
    """Fast restarts without silently sharing a port with another process.

    POSIX: SO_REUSEADDR allows rebinding while old sockets sit in TIME_WAIT.
    Windows: SO_REUSEADDR would let two processes share the port, so use
    SO_EXCLUSIVEADDRUSE instead.
    """
    if platform.system() == "Windows":
        _setopt(socket.SOL_SOCKET, "SO_EXCLUSIVEADDRUSE", 1, sock)
    else:
        _setopt(socket.SOL_SOCKET, "SO_REUSEADDR", 1, sock)


def _make_listen_socket(host: str, port: int, backlog: int = 16) -> socket.socket:
    """Listening socket; prefers dual-stack IPv6 (also accepts IPv4), then IPv4."""
    last_err = None  # type: Optional[OSError]
    if host in _WILDCARD_HOSTS:
        candidates = []
        if socket.has_ipv6:
            candidates.append((socket.AF_INET6, ("::", port)))
        candidates.append((socket.AF_INET, ("0.0.0.0", port)))
    else:
        candidates = [(fam, addr) for fam, _t, _p, _c, addr in
                      socket.getaddrinfo(host, port, socket.AF_UNSPEC,
                                         socket.SOCK_STREAM, 0, socket.AI_PASSIVE)]
    for family, addr in candidates:
        srv = None
        try:
            srv = socket.socket(family, socket.SOCK_STREAM)
            _set_reuse(srv)
            if family == socket.AF_INET6 and host in _WILDCARD_HOSTS:
                try:
                    srv.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                except (OSError, AttributeError):
                    pass
            srv.bind(addr)
            srv.listen(backlog)
            return srv
        except OSError as exc:
            last_err = exc
            if srv is not None:
                srv.close()
    raise last_err if last_err else OSError("could not bind %s:%s" % (host, port))


def _shutdown(sock: socket.socket) -> None:
    """Wake any thread blocked on this socket; the owner thread closes it."""
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def _close(sock: socket.socket) -> None:
    _shutdown(sock)
    try:
        sock.close()
    except OSError:
        pass


def _peer(addr) -> str:
    try:
        host = addr[0]
        if host.startswith("::ffff:"):
            host = host[7:]
        return "%s:%s" % (host, addr[1])
    except Exception:
        return "?"


# --- Stream helpers (work for plain and TLS sockets alike) ---
class _PrefixedRaw(io.RawIOBase):
    """Socket reader that first replays bytes already read while sniffing."""
    def __init__(self, sock, prefix: bytes):
        super().__init__()
        self._sock = sock
        self._prefix = prefix

    def readable(self):
        return True

    def readinto(self, b):
        if self._prefix:
            n = min(len(b), len(self._prefix))
            b[:n] = self._prefix[:n]
            self._prefix = self._prefix[n:]
            return n
        data = self._sock.recv(len(b))
        n = len(data)
        b[:n] = data
        return n


class _SockWriter(io.BufferedIOBase):
    def __init__(self, sock):
        super().__init__()
        self._sock = sock

    def writable(self):
        return True

    def write(self, b):
        self._sock.sendall(b)
        return len(b)


class _Request(object):
    def __init__(self, sock, prefix: bytes):
        self.sock = sock
        self.prefix = prefix


# --- HTTP handling ---
class HTTPHandler(http_server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"     # keep-alive; HTTP/1.0 clients still work
    server_version = "ConnectionHost/3"
    sys_version = ""
    timeout = HTTP_IDLE_TIMEOUT

    def setup(self):
        self.connection = self.request.sock
        try:
            self.connection.settimeout(self.timeout)
        except OSError:
            pass
        self.rfile = io.BufferedReader(_PrefixedRaw(self.connection, self.request.prefix))
        self.wfile = _SockWriter(self.connection)

    def finish(self):
        for f in (self.wfile, self.rfile):
            try:
                f.close()
            except Exception:
                pass

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (OSError, ValueError):
            self.close_connection = True

    def _respond(self, body: bytes = b"OK") -> None:
        # Drain any request body so the keep-alive stream stays in sync.
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 0:
                self.rfile.read(min(length, 1 << 20))
        except (ValueError, OSError):
            self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header(MARKER_HEADER, "1")
        self.end_headers()
        if self.command != "HEAD" and body:
            try:
                self.wfile.write(body)
            except OSError:
                self.close_connection = True

    def do_HEAD(self):
        self._respond()

    def do_GET(self):
        self._respond()

    def do_POST(self):
        self._respond()

    def do_PUT(self):
        self._respond()

    def do_OPTIONS(self):
        self._respond(b"")

    def log_message(self, format, *args):
        pass  # keep the terminal clean


class _HTTPContext(object):
    """Minimal stand-in for the server object a request handler expects."""
    def __init__(self, port: int):
        self.server_name = "localhost"
        self.server_port = port


# --- TLS (built-in identity) ---
def _build_server_tls() -> Optional[ssl.SSLContext]:
    """Server-side TLS context from the embedded identity; None if unsupported."""
    path = None
    try:
        ctx = ssl.SSLContext(getattr(ssl, "PROTOCOL_TLS_SERVER", ssl.PROTOCOL_SSLv23))
        try:
            ctx.set_alpn_protocols(["http/1.1"])
        except (NotImplementedError, AttributeError, ssl.SSLError):
            pass
        fd, path = tempfile.mkstemp(suffix=".pem")
        with os.fdopen(fd, "w") as f:
            f.write(_TLS_CERT + "\n" + _TLS_KEY + "\n")
        ctx.load_cert_chain(path)
        return ctx
    except Exception:
        return None
    finally:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass


# --- Universal host ---
class UniversalHost:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.server_sock = None   # type: Optional[socket.socket]
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._conns = set()       # type: set
        self._tcp_count = 0
        self._http_ctx = _HTTPContext(port)
        self._tls = _build_server_tls()

    # ---- lifecycle ----
    def start(self) -> bool:
        delay = 1.0
        while not self._stop.is_set():
            try:
                srv = _make_listen_socket(self.host, self.port)
                break
            except OSError as exc:
                # Port busy (old instance closing, another app): keep retrying.
                _log("[*] Port %s not available yet (%s); retrying in %.0f s..."
                     % (self.port, exc, delay))
                time.sleep(delay)
                delay = min(delay * 2, 30.0)
        else:
            return False
        srv.settimeout(SELECT_SLICE)
        self.server_sock = srv
        fam = "IPv6+IPv4" if srv.family == socket.AF_INET6 else "IPv4"
        tls = ", direct TLS" if self._tls else ""
        _log("[+] Sender listening on %s:%s (%s) - TCP, HTTP/1.x, HTTPS%s"
             % (self.host, self.port, fam, tls))
        _log("[*] Waiting for receiver...")
        return True

    def serve_forever(self) -> None:
        while not self._stop.is_set():
            try:
                conn, addr = self.server_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                time.sleep(0.1)  # e.g. too many open files: back off, don't spin
                continue
            with self._lock:
                self._conns.add(conn)
            threading.Thread(target=self._handle, args=(conn, addr), daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        if self.server_sock is not None:
            try:
                self.server_sock.close()
            except OSError:
                pass
        with self._lock:
            conns = list(self._conns)
        for c in conns:
            _shutdown(c)

    # ---- per connection ----
    def _handle(self, raw: socket.socket, addr) -> None:
        conn = raw
        try:
            raw.settimeout(IO_TIMEOUT)
            _tune_tcp_socket(raw)
            first = self._peek_first(raw)
            if first is None:
                return                      # closed or silent: health check / scanner
            if first == b"\x16":            # TLS ClientHello: answer it directly
                if self._tls is None:
                    return
                raw.settimeout(TLS_HANDSHAKE_TIMEOUT)
                try:
                    conn = self._tls.wrap_socket(raw, server_side=True)
                except (ssl.SSLError, OSError, ValueError):
                    return                  # client gave up / not really TLS
                with self._lock:
                    self._conns.add(conn)
                conn.settimeout(IO_TIMEOUT)
            prefix = self._read_head(conn)
            if not prefix:
                return
            if prefix == HELLO:
                self._tcp_session(conn, addr)
            else:
                self._http_session(conn, addr, prefix)
        except Exception:
            pass                            # one bad client never affects the sender
        finally:
            with self._lock:
                for c in (raw, conn):
                    self._conns.discard(c)
            _close(conn)
            if conn is not raw:
                _close(raw)

    def _peek_first(self, sock: socket.socket) -> Optional[bytes]:
        """Peek 1 byte on the plain socket (needed to spot TLS without eating it)."""
        deadline = time.monotonic() + SNIFF_TIMEOUT
        while not self._stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                r, _, _ = select.select([sock], [], [], min(SELECT_SLICE, remaining))
                if r:
                    b = sock.recv(1, socket.MSG_PEEK)
                    return b if b else None
            except (OSError, ValueError):
                return None
        return None

    def _read_head(self, conn) -> bytes:
        """Consume just enough bytes to tell our TCP hello from an HTTP request."""
        buf = b""
        deadline = time.monotonic() + SNIFF_TIMEOUT
        while len(buf) < len(HELLO) and HELLO.startswith(buf):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or self._stop.is_set():
                return b""
            if not (isinstance(conn, ssl.SSLSocket) and conn.pending()):
                try:
                    r, _, _ = select.select([conn], [], [], min(SELECT_SLICE, remaining))
                except (OSError, ValueError):
                    return b""
                if not r:
                    continue
            try:
                chunk = conn.recv(len(HELLO) - len(buf))
            except socket.timeout:
                continue
            except (OSError, ValueError):
                return b""
            if not chunk:
                return b""
            buf += chunk
        return buf

    def _readable(self, conn, timeout: float) -> bool:
        if isinstance(conn, ssl.SSLSocket) and conn.pending():
            return True
        r, _, _ = select.select([conn], [], [], timeout)
        return bool(r)

    def _tcp_session(self, conn, addr) -> None:
        conn.sendall(HELLO)   # handshake reply: proves the real sender answered

        # Every receiver gets its own independent session (several receivers can
        # watch one sender). A stale session from a network blip simply times
        # out on its own heartbeat while the reconnected one is already live.
        with self._lock:
            self._tcp_count += 1
            count = self._tcp_count
        tls = " (TLS)" if isinstance(conn, ssl.SSLSocket) else ""
        more = "" if count == 1 else " [%d receivers]" % count
        _log("[+] TCP connection established from %s%s%s" % (_peer(addr), tls, more))

        last_rx = last_tx = time.monotonic()
        reason = "stopped"
        while not self._stop.is_set():
            try:
                readable = self._readable(conn, SELECT_SLICE)
            except (OSError, ValueError):
                reason = "connection closed"
                break
            now = time.monotonic()
            if readable:
                try:
                    data = conn.recv(4096)
                except socket.timeout:
                    data = None
                except ssl.SSLWantReadError:
                    data = None
                except (OSError, ValueError):
                    reason = "connection lost"
                    break
                if data == b"":
                    reason = "receiver closed the connection"
                    break
                if data:
                    last_rx = now
            if now - last_tx >= HB_INTERVAL:
                try:
                    conn.sendall(HEARTBEAT)
                    last_tx = now
                except (OSError, ValueError):
                    reason = "connection lost"
                    break
            if now - last_rx > HB_TIMEOUT:
                reason = "no heartbeat for %.0f s" % HB_TIMEOUT
                break

        with self._lock:
            self._tcp_count -= 1
            remaining = self._tcp_count
        if self._stop.is_set():
            return
        _log("[!] Receiver %s disconnected: %s." % (_peer(addr), reason))
        if remaining == 0:
            _log("[*] Waiting for new connection...")

    def _http_session(self, conn, addr, prefix: bytes) -> None:
        tls = "HTTPS" if isinstance(conn, ssl.SSLSocket) else "HTTP"
        _log("[+] %s receiver connected from %s" % (tls, _peer(addr)))
        try:
            HTTPHandler(_Request(conn, prefix), addr, self._http_ctx)
        except Exception:
            pass
        if not self._stop.is_set():
            _log("[*] %s connection from %s closed (receiver reconnects automatically)."
                 % (tls, _peer(addr)))


# --- Main ---
def main() -> None:
    _log("[*] Starting sender (auto-detect: TCP / HTTP / HTTPS, any version)")
    host = UniversalHost(str(DEFAULT_IP).strip() or "0.0.0.0", int(DEFAULT_PORT))
    try:
        if not host.start():
            return
        host.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        host.stop()
    _log("[*] Sender stopped.")


if __name__ == "__main__":
    main()
