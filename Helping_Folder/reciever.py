#!/usr/bin/env python3
"""
receiver.py - Standalone Connection Monitor (TCP / HTTP / HTTPS)
-----------------------------------------------------------------
Run with:  python receiver.py

Fully automatic: paste whatever address you have into DEFAULT_IP (a LAN IP,
a hostname, "host:port", or a full URL such as tcp://x.a.pinggy.link:41234,
https://abc.ngrok-free.app). The receiver works out the protocol on its own,
tries the others if the first does not fit, remembers what worked, and
reconnects by itself forever.

Cross-platform: Windows, macOS, Linux (desktop and servers), Android (Termux),
iOS (Pythonista / a-Shell). Python 3.6+. Standard library only.
"""

import socket
import ssl
import select
import threading
import time
import platform
import random
import http.client as http_client   # aliased: the 'http' name below is a config string
from typing import Optional, List, Tuple

# ==================== EDIT THESE THREE VARIABLES ====================
auto = 'auto'
tcp = 'tcp'
http = 'http'
https = 'https'
connection_type = auto         # <- auto (recommended) / tcp / http / https
                               #    (only a first guess: the receiver switches
                               #     automatically if another protocol fits)

# LAN IP, hostname, "host:port", or the full URL your tunnel prints
DEFAULT_IP = '192.168.0.113'
DEFAULT_PORT = 4444            # used when DEFAULT_IP does not include a port
# ====================================================================

# --- Protocol (must match sender.py) ---
HELLO = b"CMON1\n"
HEARTBEAT = b"."
MARKER_HEADER = "X-CMon"

# --- Timing ---
HB_INTERVAL = 3.0              # TCP heartbeat send interval (s)
HB_TIMEOUT = 12.0              # silence longer than this = sender gone (s)
HTTP_PROBE_INTERVAL = 5.0      # HTTP(S) check interval (s)
NGROK_PROBE_INTERVAL = 150.0   # ngrok free = 20k HTTP requests/month; this lasts the month
CONNECT_TIMEOUT = 6.0          # TCP connect / TLS handshake / first reply (s)
SELECT_SLICE = 0.5             # watch granularity; keeps Ctrl+C responsive on Windows
RECONNECT_BASE_DELAY = 1.0
RECONNECT_MAX_DELAY = 30.0
STABLE_AFTER = 10.0            # a link this old counts as healthy (resets backoff)
KEEPALIVE_IDLE = 20
KEEPALIVE_INTVL = 5
KEEPALIVE_CNT = 3
USER_TIMEOUT_MS = 20000

_NGROK_SUFFIXES = (".ngrok-free.app", ".ngrok-free.dev", ".ngrok-free.pizza",
                   ".ngrok.app", ".ngrok.dev", ".ngrok.io", ".ngrok.pizza")

_PROBE_HEADERS = {
    "Connection": "keep-alive",
    "User-Agent": "connection-monitor/3",   # non-browser UA: no tunnel warning pages
    "X-Pinggy-No-Screen": "1",              # Pinggy free: skip browser screening
    "ngrok-skip-browser-warning": "1",      # ngrok free: skip interstitial
    "Cache-Control": "no-cache",
    "Accept": "*/*",
}

# Must equal _TLS_CERT in sender.py: lets the receiver trust a sender that
# answers TLS directly on a LAN, without weakening checks for anything else.
_SENDER_CERT = """-----BEGIN CERTIFICATE-----
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

OK, MISMATCH, DOWN = "ok", "mismatch", "down"


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
    if system == "Darwin":
        try:
            sock.setsockopt(socket.IPPROTO_TCP,
                            getattr(socket, "TCP_KEEPALIVE", 0x10), KEEPALIVE_IDLE)
        except OSError:
            pass
        _setopt(socket.IPPROTO_TCP, "TCP_KEEPINTVL", KEEPALIVE_INTVL, sock)
        _setopt(socket.IPPROTO_TCP, "TCP_KEEPCNT", KEEPALIVE_CNT, sock)
        return
    _setopt(socket.IPPROTO_TCP, "TCP_KEEPIDLE", KEEPALIVE_IDLE, sock)
    _setopt(socket.IPPROTO_TCP, "TCP_KEEPINTVL", KEEPALIVE_INTVL, sock)
    _setopt(socket.IPPROTO_TCP, "TCP_KEEPCNT", KEEPALIVE_CNT, sock)
    _setopt(socket.IPPROTO_TCP, "TCP_USER_TIMEOUT", USER_TIMEOUT_MS, sock)


def _shutdown_close(sock) -> None:
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


# --- Address parsing ---
def parse_target(raw: str, default_port: int, preferred: str) -> Tuple[str, int, Optional[str], bool]:
    """Accept IP, hostname, host:port, [v6]:port, or scheme://host[:port][/path].

    Returns (host, port, scheme_hint, port_is_explicit). A scheme in the URL wins over the
    preferred type; a port in the address wins over DEFAULT_PORT.
    """
    s = str(raw).strip()
    scheme = None
    if "://" in s:
        scheme, s = s.split("://", 1)
        scheme = scheme.strip().lower()
        if scheme in ("ws",):
            scheme = "http"
        elif scheme in ("wss", "tls", "ssl"):
            scheme = "https"
        elif scheme not in ("tcp", "http", "https"):
            scheme = None
    s = s.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if "@" in s:
        s = s.rsplit("@", 1)[1]

    host, port = s, None
    if s.startswith("["):                      # [IPv6]:port
        end = s.find("]")
        host = s[1:end] if end > 0 else s.strip("[]")
        rest = s[end + 1:] if end > 0 else ""
        if rest.startswith(":") and rest[1:].isdigit():
            port = int(rest[1:])
    elif s.count(":") == 1:                    # host:port
        h, p = s.split(":")
        if p.isdigit():
            host, port = h, int(p)
    host = host.strip().rstrip(".")

    explicit = port is not None or scheme is None
    if port is None:
        if scheme == "https":
            port = 443
        elif scheme == "http":
            port = 80
        else:
            port = int(default_port)
    if scheme is None and preferred in (tcp, http, https):
        scheme = preferred
    return host, port, scheme, explicit


def candidate_order(port: int, hint: Optional[str], explicit: bool = True) -> List[Tuple[str, int]]:
    """(protocol, port) pairs to try, most likely first.

    'tcps' = the TCP heartbeat over TLS. When the port only came from a URL
    scheme (https://host), http and https each use their standard port.
    """
    if port == 443:
        base = ["https", "tcps", "http", "tcp"]
    elif port == 80:
        base = ["http", "https", "tcp", "tcps"]
    else:
        base = ["tcp", "http", "https", "tcps"]
    if hint == "tcp":
        base = ["tcp", "tcps"] + [b for b in base if b not in ("tcp", "tcps")]
    elif hint in ("http", "https"):
        base = [hint] + [b for b in base if b != hint]
    if explicit:
        return [(b, port) for b in base]
    std = {"http": 80, "https": 443, "tcps": 443}
    return [(b, std.get(b, port)) for b in base]


# --- TLS ---
_CTX_LOCK = threading.Lock()
_CTX_VERIFIED = None  # type: Optional[ssl.SSLContext]
_CTX_PINNED = None    # type: Optional[ssl.SSLContext]


def _alpn(ctx: ssl.SSLContext) -> None:
    try:
        ctx.set_alpn_protocols(["http/1.1"])
    except (NotImplementedError, AttributeError, ssl.SSLError):
        pass


def _verified_ctx() -> ssl.SSLContext:
    """System-trusted TLS (negotiates the best version both sides support)."""
    global _CTX_VERIFIED
    with _CTX_LOCK:
        if _CTX_VERIFIED is None:
            ctx = ssl.create_default_context()
            _alpn(ctx)
            try:
                if not ctx.get_ca_certs():   # some installs ship no CA bundle
                    import certifi  # optional
                    ctx.load_verify_locations(cafile=certifi.where())
            except Exception:
                pass
            _CTX_VERIFIED = ctx
        return _CTX_VERIFIED


def _pinned_ctx() -> Optional[ssl.SSLContext]:
    """Trusts only the sender's built-in certificate (for direct LAN TLS)."""
    global _CTX_PINNED
    with _CTX_LOCK:
        if _CTX_PINNED is None:
            try:
                ctx = ssl.SSLContext(getattr(ssl, "PROTOCOL_TLS_CLIENT", ssl.PROTOCOL_SSLv23))
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_REQUIRED
                ctx.load_verify_locations(cadata=_SENDER_CERT)
                _alpn(ctx)
                _CTX_PINNED = ctx
            except Exception:
                return None
        return _CTX_PINNED


def _is_verify_error(exc: BaseException) -> bool:
    cls = getattr(ssl, "SSLCertVerificationError", None)
    if cls is not None and isinstance(exc, cls):
        return True
    if isinstance(exc, getattr(ssl, "CertificateError", ())):
        return True
    return "CERTIFICATE_VERIFY_FAILED" in str(exc)


def _connect_tcp(host: str, port: int) -> socket.socket:
    sock = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
    _tune_tcp_socket(sock)
    return sock


def _connect_tls(host: str, port: int, ctx_holder: list) -> ssl.SSLSocket:
    """TLS with full verification; falls back to the pinned sender cert only."""
    ctx = ctx_holder[0] if ctx_holder else _verified_ctx()
    raw = _connect_tcp(host, port)
    try:
        s = ctx.wrap_socket(raw, server_hostname=host)
        if not ctx_holder:
            ctx_holder.append(ctx)
        return s
    except Exception as exc:
        _shutdown_close(raw)
        pinned = _pinned_ctx()
        if ctx_holder or pinned is None or not _is_verify_error(exc):
            raise
    raw = _connect_tcp(host, port)
    try:
        s = pinned.wrap_socket(raw, server_hostname=host)
        ctx_holder.append(pinned)
        return s
    except Exception:
        _shutdown_close(raw)
        raise


def _friendly(exc: BaseException) -> str:
    """Short, human wording for a connection problem."""
    if isinstance(exc, socket.gaierror):
        return "host name not found"
    if isinstance(exc, ConnectionRefusedError):
        return "connection refused (sender or tunnel not running)"
    if isinstance(exc, socket.timeout) or "timed out" in str(exc):
        return "no reply in time"
    if isinstance(exc, ssl.SSLError) or _is_verify_error(exc):
        return "TLS handshake failed"
    if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
        return "connection closed by the other side"
    if isinstance(exc, _NotSender):
        return str(exc)
    text = str(exc).strip()
    return text or exc.__class__.__name__


def _classify(exc: BaseException) -> str:
    """DOWN = nothing there (stop trying other protocols); MISMATCH = try next."""
    if isinstance(exc, (socket.gaierror, ConnectionRefusedError)):
        return DOWN
    if isinstance(exc, socket.timeout):
        return DOWN
    if isinstance(exc, OSError) and getattr(exc, "errno", None) in (
            101, 113, 10051, 10065, 51, 65):            # network/host unreachable
        return DOWN
    return MISMATCH


# --- TCP client (plain or TLS) ---
class TCPClient:
    label = "TCP"

    def __init__(self, host: str, port: int, use_tls: bool = False):
        self.host, self.port, self.use_tls = host, port, use_tls
        self.label = "TCP over TLS" if use_tls else "TCP"
        self.sock = None
        self._stop = threading.Event()
        self._thread = None  # type: Optional[threading.Thread]
        self._ctx = []       # type: list
        self.detail = ""

    def connect(self) -> str:
        try:
            if self.use_tls:
                sock = _connect_tls(self.host, self.port, self._ctx)
            else:
                sock = _connect_tcp(self.host, self.port)
        except Exception as exc:
            self.detail = _friendly(exc)
            return _classify(exc)
        try:
            sock.settimeout(CONNECT_TIMEOUT)
            sock.sendall(HELLO)
            buf = b""
            deadline = time.monotonic() + CONNECT_TIMEOUT
            while HELLO not in buf:
                if time.monotonic() > deadline:
                    raise socket.timeout("no handshake reply")
                chunk = sock.recv(64)
                if not chunk:
                    raise _NotSender("endpoint answered, but the sender behind it is not running")
                buf += chunk
                if len(buf) > 4096 or not (HELLO.startswith(buf[:len(HELLO)])
                                           or HELLO in buf):
                    raise _NotSender("endpoint answered, but it is not the sender")
        except socket.timeout as exc:
            _shutdown_close(sock)
            self.detail = _friendly(exc)
            return DOWN       # connected but silent: hung/busy, other protocols won't help
        except Exception as exc:
            _shutdown_close(sock)
            self.detail = _friendly(exc)
            return MISMATCH   # something answered, but not the sender
        self.sock = sock
        self._stop.clear()
        self._thread = threading.Thread(target=self._watch, daemon=True)
        self._thread.start()
        return OK

    @staticmethod
    def _readable(s) -> bool:
        if isinstance(s, ssl.SSLSocket) and s.pending():
            return True
        r, _, _ = select.select([s], [], [], SELECT_SLICE)
        return bool(r)

    def _watch(self) -> None:
        try:
            self._watch_loop()
        except Exception as exc:   # never die with a traceback
            self._end(str(exc) or exc.__class__.__name__)

    def _watch_loop(self) -> None:
        sock = self.sock   # local ref: close() may clear self.sock concurrently
        if sock is None:
            return
        last_rx = last_tx = time.monotonic()
        while not self._stop.is_set():
            try:
                readable = self._readable(sock)
            except (OSError, ValueError, TypeError):
                self._end("connection closed")
                return
            now = time.monotonic()
            if readable:
                try:
                    data = sock.recv(4096)
                except (socket.timeout, getattr(ssl, "SSLWantReadError", socket.timeout)):
                    data = None
                except (OSError, ValueError):
                    self._end("connection lost")
                    return
                if data == b"":
                    self._end("sender closed the connection")
                    return
                if data:
                    last_rx = now
            if now - last_tx >= HB_INTERVAL:
                try:
                    sock.sendall(HEARTBEAT)
                    last_tx = now
                except (OSError, ValueError):
                    self._end("connection lost")
                    return
            if now - last_rx > HB_TIMEOUT:
                self._end("no heartbeat for %.0f s" % HB_TIMEOUT)
                return

    def _end(self, reason: str) -> None:
        if not self._stop.is_set():
            self.detail = reason
            self._stop.set()

    def is_alive(self) -> bool:
        return (self.sock is not None and not self._stop.is_set()
                and self._thread is not None and self._thread.is_alive())

    def describe(self) -> str:
        v = ""
        if isinstance(self.sock, ssl.SSLSocket):
            try:
                v = ", " + (self.sock.version() or "TLS")
            except Exception:
                pass
        return "%s%s" % (self.label, v)

    def close(self) -> None:
        self._stop.set()
        _shutdown_close(self.sock)
        self.sock = None


# --- HTTP / HTTPS client ---
class _NotSender(http_client.HTTPException):
    pass


class WebClient:
    def __init__(self, host: str, port: int, use_tls: bool):
        self.host, self.port, self.use_tls = host, port, use_tls
        self.label = "HTTPS" if use_tls else "HTTP"
        self.conn = None
        self._ctx = []       # type: list
        self._stop = threading.Event()
        self._thread = None  # type: Optional[threading.Thread]
        self._alive = False
        self.detail = ""
        self.interval = (NGROK_PROBE_INTERVAL
                         if self.host.lower().endswith(_NGROK_SUFFIXES)
                         else HTTP_PROBE_INTERVAL)

    def _new_conn(self):
        if not self.use_tls:
            c = http_client.HTTPConnection(self.host, self.port, timeout=CONNECT_TIMEOUT)
            c.connect()
            _tune_tcp_socket(c.sock)
        else:
            c = http_client.HTTPSConnection(self.host, self.port, timeout=CONNECT_TIMEOUT,
                                            context=_verified_ctx())
            # Do the TLS ourselves so the pinned-cert fallback applies too.
            c.sock = _connect_tls(self.host, self.port, self._ctx)
        try:
            c.sock.settimeout(CONNECT_TIMEOUT)
        except Exception:
            pass
        return c

    def _probe(self, conn) -> bool:
        """One HEAD. True = server asked to close. Raises if it isn't the sender."""
        conn.request("HEAD", "/", headers=_PROBE_HEADERS)
        resp = conn.getresponse()
        resp.read()
        if resp.getheader(MARKER_HEADER) != "1":
            raise _NotSender("endpoint replied HTTP %s %s, not the sender" % (resp.status, resp.reason))
        return bool(resp.will_close)

    def connect(self) -> str:
        conn = None
        try:
            conn = self._new_conn()
        except Exception as exc:
            self.detail = _friendly(exc)
            return _classify(exc)
        try:
            if self._probe(conn):
                conn.close()
        except Exception as exc:
            try:
                conn.close()
            except Exception:
                pass
            self.detail = _friendly(exc)
            return DOWN if isinstance(exc, socket.timeout) else MISMATCH
        self.conn = conn
        self._alive = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._watch, daemon=True)
        self._thread.start()
        return OK

    def _socket_dead(self) -> bool:
        """Between probes: did the server/tunnel close our keep-alive socket?"""
        s = getattr(self.conn, "sock", None)
        if s is None:
            return False
        try:
            if isinstance(s, ssl.SSLSocket) and s.pending():
                return False
            r, _, _ = select.select([s], [], [], 0)
            if not r:
                return False
            if isinstance(s, ssl.SSLSocket):
                return True   # unsolicited TLS data on idle HTTP = close_notify/EOF
            return s.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True

    def _watch(self) -> None:
        try:
            self._watch_loop()
        except Exception as exc:   # never die with a traceback
            self.detail = str(exc) or exc.__class__.__name__
            self._alive = False
            self._stop.set()

    def _watch_loop(self) -> None:
        next_probe = time.monotonic() + self.interval
        while not self._stop.is_set():
            if self._stop.wait(SELECT_SLICE):
                return
            if time.monotonic() < next_probe and not self._socket_dead():
                continue
            ok = False
            # A reused socket can be silently dropped by tunnels/proxies: one
            # retry on a fresh connection before declaring the sender gone.
            for attempt in (0, 1):
                try:
                    old = self.conn
                    if attempt == 1 or old is None or old.sock is None:
                        try:
                            if old is not None:
                                old.close()
                        except Exception:
                            pass
                        if self._stop.is_set():
                            return
                        new = self._new_conn()
                        if self._stop.is_set():
                            new.close()
                            return
                        self.conn = new
                    conn = self.conn
                    if self._stop.is_set() or conn is None:
                        return
                    if self._probe(conn):
                        conn.close()
                    ok = True
                    break
                except Exception as exc:
                    self.detail = _friendly(exc)
                    if isinstance(exc, socket.timeout):
                        break      # a hang won't be fixed by a fresh connection
            if not ok:
                self._alive = False
                self._stop.set()
                return
            next_probe = time.monotonic() + self.interval

    def is_alive(self) -> bool:
        return (self._alive and not self._stop.is_set()
                and self._thread is not None and self._thread.is_alive())

    def describe(self) -> str:
        v = ""
        s = getattr(self.conn, "sock", None)
        if isinstance(s, ssl.SSLSocket):
            try:
                v = ", " + (s.version() or "TLS")
            except Exception:
                pass
        extra = ", check every %.0f s" % self.interval
        return "%s (HTTP/1.1%s%s)" % (self.label, v, extra)

    def close(self) -> None:
        self._stop.set()
        self._alive = False
        try:
            if self.conn is not None:
                self.conn.close()
        except Exception:
            pass
        self.conn = None


def _make_client(kind: str, host: str, port: int):
    if kind == "tcp":
        return TCPClient(host, port, use_tls=False)
    if kind == "tcps":
        return TCPClient(host, port, use_tls=True)
    if kind == "http":
        return WebClient(host, port, use_tls=False)
    return WebClient(host, port, use_tls=True)


# --- Monitor loop ---
def run_monitor() -> None:
    preferred = str(connection_type).strip().lower()
    host, port, hint, explicit = parse_target(DEFAULT_IP, DEFAULT_PORT, preferred)
    order = candidate_order(port, hint, explicit)
    last_good = None  # type: Optional[Tuple[str, int]]

    _log("[*] Monitoring %s (auto: %s)" % (
        host, " > ".join("%s:%s" % (k.upper(), p) for k, p in order)))
    _log("[*] Press Ctrl+C to stop.")

    conn = None
    connected_at = 0.0
    delay = RECONNECT_BASE_DELAY
    attempt = 0
    try:
        while True:
            if conn is not None:
                if conn.is_alive():
                    if connected_at and time.monotonic() - connected_at >= STABLE_AFTER:
                        delay = RECONNECT_BASE_DELAY
                        connected_at = 0.0
                    time.sleep(SELECT_SLICE)
                    continue
                reason = conn.detail or "connection lost"
                conn.close()
                conn = None
                _log("[!] Connection stopped: %s. Reconnecting..." % reason)

            attempt += 1
            tries = ([last_good] if last_good else []) + [o for o in order if o != last_good]
            result_detail = ""
            down_ports = set()
            for kind, kport in tries:
                if kport in down_ports:
                    continue       # that port is unreachable: other protocols won't help
                client = _make_client(kind, host, kport)
                result = client.connect()
                if result == OK:
                    conn = client
                    last_good = (kind, kport)
                    connected_at = time.monotonic()
                    attempt = 0
                    _log("[+] Connected to %s:%s via %s" % (host, kport, client.describe()))
                    break
                result_detail = client.detail
                if result == DOWN:
                    down_ports.add(kport)
            if conn is not None:
                continue

            wait = min(delay, RECONNECT_MAX_DELAY) + random.uniform(0, 0.5)
            if attempt == 1 or attempt % 5 == 0:
                _log("[*] Sender not reachable yet (%s). Retrying..." % (result_detail or "no answer"))
            time.sleep(wait)
            delay = min(delay * 2, RECONNECT_MAX_DELAY)
    except KeyboardInterrupt:
        print()
        _log("[*] Interrupted.")
    finally:
        if conn is not None:
            conn.close()
        _log("[*] Monitor stopped.")


def main() -> None:
    run_monitor()


if __name__ == "__main__":
    main()
