# controller.py - v20 (automatic TCP / HTTP / HTTPS transport; one port accepts
#                   every client and every tunnel; direct TLS; Pinggy and ngrok
#                   aware; Ctrl+B background operations, Ctrl+Z stops a command,
#                   a screen share or a screenshot; jobs/stop; several clients
#                   as numbered sessions with 'session' / 'connect session N';
#                   'show options' prints a two-column command cheat sheet;
#                   'exit' leaves only this controller (clients reconnect),
#                   'exit session N' / 'exit session all' stop those clients;
#                   macOS / Android(Termux) / iOS-viewer aware; --viewer-lan
#                   lets an iPhone/iPad watch a share in Safari; fast send/receive)

import base64
import hashlib
from http import server as http_server
import io
import ipaddress
import json
import os
import queue
import secrets
import select
import shutil
import signal
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from urllib.parse import unquote

# ---------------------------------------------------------------------------
# Transport selection.  The controller accepts every transport on the same
# port, so this value is only a preference/advertisement:
#     auto   detect TCP, HTTP/1.1, HTTPS, WebSocket, and tunnel traffic
#     tcp    prefer raw TCP on the client (the controller still auto-detects)
#     http   prefer HTTP/1.1 on the client (the controller still auto-detects)
#     https  prefer HTTPS on the client (the controller still auto-detects)
# Both bare names and quoted strings are accepted.  The client can be given a
# bare IP, host:port, or the full URL printed by Pinggy / ngrok; it detects
# the rest itself.  TCP is the fastest LAN choice; public tunnels normally use
# their HTTPS URL (ngrok/Pinggy terminate TLS at the tunnel edge).
# ---------------------------------------------------------------------------
auto = 'auto'
tcp = 'tcp'
http = 'http'
https = 'https'
connection_type = auto         # <- auto / https / http / tcp

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
CONNECTION_TYPE = str(os.environ.get('TUNNEL_TYPE') or connection_type).strip().lower()
VALID_CONNECTION_TYPES = ('auto', 'tcp', 'http', 'https')
HTTP_HANDSHAKE_TIMEOUT = 10    # seconds for HTTP headers / TLS handshake
HTTP_SEND_STALL_TIMEOUT = 10   # tolerate a slow-but-moving mobile link
HTTP_MAX_HEADER = 32 * 1024    # reject a peer that sends an endless header
HTTP_MAX_LINE = 4096           # one chunk-size/trailer line
HTTP_MAX_CHUNK = MAX_MSG       # one HTTP chunk cannot claim unlimited memory
RECV_STALL_TIMEOUT = 60        # seconds one recv() may stall mid-message
MIN_TRANSFER_RATE = 64 * 1024  # bytes/second a whole transfer must average
TRANSFER_GRACE = 60            # seconds of allowance on top of size / MIN_TRANSFER_RATE
OUTPUT_PRINT_LIMIT = 256 * 1024    # bigger replies: print head+tail, save the rest
OUTPUT_PRINT_PART = 48 * 1024

# ---------------------------------------------------------------------------
# Universal connection protocol (shared with client.py)
#
# Raw TCP is deliberately not guessed from application JSON: a new client
# starts with a short line that also carries the shell token.  HTTP and HTTPS
# are recognised from the HTTP method/headers; direct TLS is recognised from
# the TLS ClientHello.  A legacy client that starts with the old 4-byte JSON
# length still works because it is passed through untouched.
# ---------------------------------------------------------------------------
CMON_MAGIC = b'CMON2\n'
CMON_ACK = b'CMON2 OK\n'
CMON_SHELL_PREFIX = b'CMON2 SHELL '
CMON_MARKER_HEADER = 'X-CMon'
CMON_MARKER_VALUE = '1'
CMON_VERSION = '2'
CMON_USER_AGENT = 'cmon-controller/2'
CMON_WS_PROTOCOL = 'cmon.v1'
CMON_WS_MAX_FRAME = 512 * 1024
CMON_WS_PING = 20.0
CMON_WS_DEAD = 65.0
TUNNEL_PROBE_TIMEOUT = 12.0    # controller must answer the app hello through a tunnel
CMON_HTTP_PATHS = ('/tunnel', '/shell', '/ws', '/shell-ws', '/up', '/down',
                   '/probe', '/health', '/')


def _socket_pending(conn):
    """Bytes already decrypted/buffered above the kernel (TLS, adapters).

    select() only sees the raw socket.  An SSLSocket can hold a complete
    application message in its internal buffer while the underlying socket is
    not readable, which used to make a direct-TLS hello look like a timeout.
    """
    # PrefixSocket has already pulled bytes off the raw socket during protocol
    # detection; select() on the raw socket cannot see them.
    prefix = getattr(conn, '_prefix', None)
    if prefix:
        return len(prefix)
    for candidate in (conn, getattr(conn, 'raw', None)):
        if candidate is None:
            continue
        try:
            return int(candidate.pending())
        except (AttributeError, OSError, ValueError):
            continue
    return 0


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
        if timeout is not None and _socket_pending(conn) <= 0:
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


# ---------------------------------------------------------------------------
# HTTP / HTTPS transport
#
# The wire protocol above is unchanged.  For http/https it is carried inside a
# single HTTP/1.1 request body and a single chunked response body:
#
#   client ---- POST /tunnel (chunked request body) ----> controller
#   client <--- 200 OK (chunked response body) ---------- controller
#
# Both directions are live at the same time.  The controller answers the HTTP
# headers immediately, so there is no request/response round trip for every
# command.  A small socketpair adapter gives the rest of the program ordinary
# socket-shaped objects, which means commands, uploads, downloads, screenshots,
# screen frames, keepalive and the interactive shell all use the same code.
# ---------------------------------------------------------------------------

def _http_connection_name():
    """Human-readable form of the selected transport."""
    if CONNECTION_TYPE == 'auto':
        return 'AUTO (TCP / HTTP / HTTPS)'
    return CONNECTION_TYPE.upper()


def _read_http_head(sock, timeout=HTTP_HANDSHAKE_TIMEOUT):
    """Read an HTTP header block, returning (text_head, extra_body_bytes).

    The peer may send the first request-body chunk in the same TCP packet as
    the headers; those bytes are returned rather than discarded.  The header
    is bounded so a malicious peer cannot make this allocate forever.
    """
    try:
        sock.settimeout(timeout)
    except (AttributeError, OSError):
        pass
    data = b''
    try:
        while b'\r\n\r\n' not in data:
            if len(data) > HTTP_MAX_HEADER:
                raise OSError('HTTP headers are too large')
            chunk = sock.recv(min(4096, HTTP_MAX_HEADER + 1 - len(data)))
            if not chunk:
                raise OSError('HTTP peer closed during the handshake')
            data += chunk
    except (socket.timeout, TimeoutError):
        raise OSError('HTTP handshake timed out')
    finally:
        try:
            sock.settimeout(None)
        except (AttributeError, OSError):
            pass
    head, _sep, extra = data.partition(b'\r\n\r\n')
    if len(head) > HTTP_MAX_HEADER:
        raise OSError('HTTP headers are too large')
    return head.decode('latin-1', 'replace'), extra


def _http_headers(lines):
    headers = {}
    for line in lines:
        if not line:
            continue
        if line[:1] in (' ', '\t'):
            raise OSError('folded HTTP headers are not accepted')
        if ':' not in line:
            raise OSError('malformed HTTP header')
        key, value = line.split(':', 1)
        key = key.strip().lower()
        if not key:
            raise OSError('empty HTTP header name')
        # Repeated headers are legal.  For the few we use, a comma is the
        # correct combination (notably Transfer-Encoding).
        old = headers.get(key)
        headers[key] = (old + ',' + value.strip()) if old else value.strip()
    return headers


def _http_send(sock, code, reason, body=b'', extra_headers=(),
               version='HTTP/1.1', connection='close'):
    """Send a small normal HTTP response (health check or an error)."""
    headers = [
        ('{} {} {}'.format(version, code, reason), None),
        ('Content-Type', 'text/plain; charset=utf-8'),
        ('Content-Length', str(len(body))),
        ('Connection', connection),
        ('Cache-Control', 'no-store'),
        (CMON_MARKER_HEADER, CMON_MARKER_VALUE),
        ('X-CMon-Version', CMON_VERSION),
    ]
    headers.extend((k, v) for k, v in extra_headers)
    text = ''.join('{}\r\n'.format(line if value is None else '{}: {}'.format(line, value))
                   for line, value in headers)
    sock.sendall(text.encode('latin-1') + b'\r\n' + body)


def _http_send_tunnel_ok(sock, version='HTTP/1.1'):
    """Start the streaming response side of a tunnel."""
    response = (
        '{} 200 OK\r\n'
        'Content-Type: application/octet-stream\r\n'
        'Transfer-Encoding: chunked\r\n'
        'Cache-Control: no-store\r\n'
        'X-Accel-Buffering: no\r\n'
        'X-Content-Type-Options: nosniff\r\n'
        '{marker}: {value}\r\n'
        'X-CMon-Version: {ver}\r\n'
        'Connection: keep-alive\r\n'
        '\r\n'
    ).format(version, marker=CMON_MARKER_HEADER, value=CMON_MARKER_VALUE,
             ver=CMON_VERSION).encode('ascii')
    sock.sendall(response)


def _http_absolute_path(target):
    path = target.split('?', 1)[0]
    if path.startswith('http://') or path.startswith('https://'):
        # An HTTP proxy may send an absolute URI in the request line.
        rest = path.split('://', 1)[1]
        path = '/' + rest.split('/', 1)[1] if '/' in rest else '/'
    return path


def _http_wants_keep_alive(headers):
    """True only when the peer explicitly requested connection reuse.

    The tunnel client sends 'Connection: keep-alive' on its persistent /up
    connection.  Requiring the explicit token keeps ordinary HTTP/1.1 health
    checks, probes and older clients on the simple one-request path."""
    tokens = [item.strip().lower() for item in
              (headers or {}).get('connection', '').split(',') if item.strip()]
    return 'keep-alive' in tokens and 'close' not in tokens


def _half_up_duplicate(session, headers):
    """Return True when this is a retry of the last /up request.

    A keep-alive POST can fail after the server has already accepted its
    body (for example when a tunnel drops just before the response).  The
    client retries with the same sequence number; dropping the duplicate here
    makes that retry safe instead of duplicating shell input or file bytes."""
    value = (headers or {}).get('x-cmon-up-seq', '').strip()
    if not value:
        return False
    try:
        sequence = int(value)
    except ValueError:
        return False
    with session.seq_lock:
        if session.last_up_seq is not None and sequence <= session.last_up_seq:
            return True
        session.last_up_seq = sequence
    return False


# ---------------------------------------------------------------------------
# Half-duplex HTTP fallback
#
# Some proxies buffer a complete request body before forwarding it, so the
# streaming POST above cannot work there.  This fallback uses two ordinary
# HTTP exchanges: a long-lived GET /down response for controller->client data
# and short POST /up requests for client->controller data.  Each POST has a
# Content-Length, so even a buffering proxy forwards it.  The body of /down is
# a tiny frame stream (type, length, payload) so HTTP/1.0 close-delimited
# bodies and HTTP/1.1 chunked bodies carry the same bytes.
# ---------------------------------------------------------------------------

HALF_SESSIONS = {}
HALF_LOCK = threading.Lock()
HALF_MAX_SESSIONS = 64
HALF_MAX_UP = 8 * 1024 * 1024
HALF_UP_TIMEOUT = 30            # seconds for one tunneled /up body


class HalfTunnel(object):
    """Controller-side socket-shaped adapter for the half-duplex fallback."""

    def __init__(self, sid, role='control', token='', addr=None):
        self.sid = sid
        self.role = role
        self.token = token
        self.addr = addr
        self._in, self._out = socket.socketpair()
        self._in.settimeout(None)
        self._out.settimeout(1.0)
        try:
            for sock in (self._in, self._out):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024 * 1024)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
        except OSError:
            pass
        self.out_q = queue.Queue(maxsize=128)
        self._closed = False
        self._write_closed = False
        self._down_active = False
        self._resources_closed = False
        self.seq_lock = threading.Lock()
        self.last_up_seq = None

    def fileno(self):
        return self._in.fileno()

    def __getattr__(self, name):
        return getattr(self._in, name)

    def send(self, data):
        if not data:
            return 0
        if self._closed or self._write_closed:
            raise OSError('half-duplex tunnel is closed')
        try:
            self.out_q.put(bytes(data), timeout=30)
        except queue.Full:
            raise OSError('half-duplex tunnel send queue is full')
        return len(data)

    def sendall(self, data):
        self.send(data)

    def feed_in(self, data):
        """Deliver bytes from POST /up to the application side (recv)."""
        if self._closed:
            raise OSError('half-duplex tunnel is closed')
        view = memoryview(data)
        while view:
            try:
                sent = self._out.send(view)
            except (socket.timeout, TimeoutError):
                if self._closed:
                    raise OSError('half-duplex tunnel is closed')
                continue
            if sent <= 0:
                raise OSError('half-duplex tunnel is closed')
            view = view[sent:]

    def attach_down(self):
        if self._down_active:
            raise OSError('a /down stream is already attached')
        self._down_active = True

    def down_finished(self):
        self._down_active = False
        self.close()

    def shutdown(self, how):
        if how in (socket.SHUT_WR, socket.SHUT_RDWR):
            if not self._write_closed:
                self._write_closed = True
                try:
                    self.out_q.put_nowait(None)
                except queue.Full:
                    pass
        if how in (socket.SHUT_RD, socket.SHUT_RDWR):
            try:
                self._in.shutdown(socket.SHUT_RD)
            except OSError:
                pass

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.out_q.put_nowait(None)
        except queue.Full:
            pass
        with HALF_LOCK:
            if HALF_SESSIONS.get(self.sid) is self:
                del HALF_SESSIONS[self.sid]
        if self._resources_closed:
            return
        self._resources_closed = True
        for sock in (self._in, self._out):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass


def _half_get_session(sid, role='control', token='', addr=None):
    with HALF_LOCK:
        session = HALF_SESSIONS.get(sid)
        if session is not None and not session._closed:
            return session
        if len(HALF_SESSIONS) >= HALF_MAX_SESSIONS:
            raise OSError('too many half-duplex sessions')
        session = HalfTunnel(sid, role=role, token=token, addr=addr)
        HALF_SESSIONS[sid] = session
        return session


def _half_write_chunk(sock, chunked, data):
    if chunked:
        sock.sendall(('{:x}\r\n'.format(len(data))).encode('ascii') + data + b'\r\n')
    else:
        sock.sendall(data)


def _half_down_loop(sock, session, chunked):
    """Stream controller->client frames until the session closes."""
    try:
        session.attach_down()
        next_heartbeat = time.monotonic() + 15
        while not session._closed:
            try:
                item = session.out_q.get_nowait()
            except queue.Empty:
                # A write-only loop cannot see the client closing /down.  Poll
                # for EOF so ending a shell (or closing the client) promptly
                # closes the controller-side app socket instead of leaking it.
                try:
                    readable, _, _ = select.select([sock], [], [], 0.5)
                except (OSError, ValueError):
                    break
                if readable:
                    try:
                        incoming = sock.recv(1)
                    except OSError:
                        break
                    if not incoming:
                        break
                    # GET /down has no request body; ignore anything else.
                if time.monotonic() < next_heartbeat:
                    continue
                next_heartbeat = time.monotonic() + 15
                frame = b'\x00'                  # transport heartbeat
            else:
                if item is None:
                    _half_write_chunk(sock, chunked, b'\x02')
                    break
                if not isinstance(item, (bytes, bytearray)):
                    continue
                frame = b'\x01' + len(item).to_bytes(4, 'big') + bytes(item)
            _half_write_chunk(sock, chunked, frame)
        if chunked:
            try:
                sock.sendall(b'0\r\n\r\n')
            except OSError:
                pass
    except OSError:
        pass
    finally:
        if session._down_active:
            session.down_finished()
        try:
            sock.close()
        except OSError:
            pass


def _half_down_response(sock, session, version):
    chunked = version != 'HTTP/1.0'
    headers = [
        '{} 200 OK'.format(version),
        'Content-Type: application/octet-stream',
        'Cache-Control: no-store',
        'X-Accel-Buffering: no',
        'X-Content-Type-Options: nosniff',
        '{marker}: {value}'.format(marker=CMON_MARKER_HEADER, value=CMON_MARKER_VALUE),
        'X-CMon-Version: {}'.format(CMON_VERSION),
    ]
    if chunked:
        headers.append('Transfer-Encoding: chunked')
        headers.append('Connection: keep-alive')
    else:
        headers.append('Connection: close')
    sock.sendall(('\r\n'.join(headers) + '\r\n\r\n').encode('latin-1'))
    thread = threading.Thread(target=_half_down_loop,
                              args=(sock, session, chunked),
                              name='half-down', daemon=True)
    thread.start()


def _read_content_length_body(sock, headers, extra, limit=HALF_MAX_UP):
    """Read a complete Content-Length body (the portable POST /up form)."""
    try:
        length = int(headers.get('content-length', '0'))
    except ValueError:
        raise OSError('invalid Content-Length')
    if length < 0 or length > limit:
        raise OSError('POST /up body is too large')
    data = bytearray(extra[:length])
    remaining = length - len(data)
    while remaining > 0:
        chunk = sock.recv(min(remaining, 262144))
        if not chunk:
            raise OSError('POST /up body ended early')
        data += chunk
        remaining -= len(chunk)
    return bytes(data)


def _http_accept(sock, head=None, extra=b''):
    """Parse one client request and turn it into a tunnel.

    Returns (kind, path, tunnel, headers):
      kind 'tunnel'  full-duplex HTTP/1.1 chunked POST body
      kind 'ws'      RFC 6455 WebSocket (preferred through tunnels)
      kind 'health'  a plain health response was sent
      kind 'probe'   a streaming request-body probe was answered
      kind None      an error response was sent
    The caller owns the socket and must close it on failure.
    """
    try:
        if head is None:
            head, extra = _read_http_head(sock)
        lines = head.split('\r\n')
        if not lines or not lines[0]:
            raise OSError('empty HTTP request')
        parts = lines[0].split()
        if len(parts) < 3:
            raise OSError('malformed HTTP request line')
        method, target, version = parts[0].upper(), parts[1], parts[2].upper()
        headers = _http_headers(lines[1:])
        path = _http_absolute_path(target)
        # A shell token may be in a header or a query parameter.  Some
        # tunnels expose custom headers fine, but a query string is the most
        # portable fallback and is never used for the control channel.
        if '?' in target:
            for pair in target.split('?', 1)[1].split('&'):
                key, _sep, value = pair.partition('=')
                if key == 'token' and not headers.get('x-cmon-token'):
                    headers['x-cmon-token'] = unquote(value)
                elif key == 'session' and not headers.get('x-cmon-session'):
                    headers['x-cmon-session'] = unquote(value)
        if path in ('/health', '/') and method in ('GET', 'HEAD'):
            # Do not use str.format() on this literal: the JSON braces would
            # be parsed as replacement fields.
            body = ('{"ok":true,"transport":"' + CONNECTION_TYPE
                    + '","protocol":"HTTP/1.1","websocket":true,'
                    '"version":"' + CMON_VERSION + '"}\n').encode('utf-8')
            if method == 'HEAD':
                body = b''
            _http_send(sock, 200, 'OK', body, version=version)
            return 'health', path, None, headers

        if path in ('/down', '/up'):
            sid = (headers.get('x-cmon-session') or '').strip()
            role = (headers.get('x-cmon-role') or 'control').strip().lower()
            token = (headers.get('x-cmon-token') or '').strip()
            if not sid:
                _http_send(sock, 400, 'Bad Request',
                           b'a half-duplex request needs X-CMon-Session\n',
                           version=version)
                return None, None, None, None
            if path == '/down':
                if method not in ('GET', 'HEAD'):
                    _http_send(sock, 405, 'Method Not Allowed', b'use GET /down\n',
                               version=version)
                    return None, None, None, None
                try:
                    session = _half_get_session(sid, role=role, token=token)
                except OSError as e:
                    _http_send(sock, 503, 'Service Unavailable',
                               ('{}\n'.format(e)).encode('utf-8'), version=version)
                    return None, None, None, None
                if session._down_active:
                    session.close()
                    _http_send(sock, 409, 'Conflict',
                               b'that half-duplex session is already attached\n',
                               version=version)
                    return None, None, None, None
                try:
                    _half_down_response(sock, session, version)
                except OSError:
                    session.close()
                    raise
                return 'half', path, session, headers
            # /up is a short POST with a Content-Length body.
            if method != 'POST':
                _http_send(sock, 405, 'Method Not Allowed', b'use POST /up\n',
                           version=version)
                return None, None, None, None
            with HALF_LOCK:
                session = HALF_SESSIONS.get(sid)
            if session is None or session._closed:
                _http_send(sock, 404, 'Not Found',
                           b'no /down stream for that session\n', version=version)
                return None, None, None, None
            if role == 'shell' and (not token or not secrets.compare_digest(
                    str(session.token), str(token))):
                _http_send(sock, 403, 'Forbidden', b'bad shell token\n',
                           version=version)
                return None, None, None, None
            keep_alive = _http_wants_keep_alive(headers)
            try:
                sock.settimeout(HALF_UP_TIMEOUT)
                body = _read_content_length_body(sock, headers, extra)
                if not _half_up_duplicate(session, headers):
                    session.feed_in(body)
            except OSError as e:
                _http_send(sock, 400, 'Bad Request',
                           ('{}\n'.format(e)).encode('utf-8'), version=version)
                return None, None, None, None
            finally:
                try:
                    sock.settimeout(None)
                except (AttributeError, OSError):
                    pass
            _http_send(sock, 200, 'OK', b'ok\n', version=version,
                       connection='keep-alive' if keep_alive else 'close')
            return 'half-up', path, None, headers

        # A WebSocket is a normal HTTP/1.1 upgrade.  It is the safest way to
        # carry a long-lived duplex stream through ngrok, Pinggy, nginx and
        # other reverse proxies: their HTTP request buffering cannot swallow
        # the request body, and the proxy keeps one HTTP request open.
        connection_tokens = [t.strip().lower() for t in
                             headers.get('connection', '').split(',') if t.strip()]
        upgrade = headers.get('upgrade', '').strip().lower()
        if (method == 'GET' and path in ('/tunnel', '/ws', '/shell', '/shell-ws')
                and upgrade == 'websocket' and 'upgrade' in connection_tokens):
            tunnel = _ws_server_upgrade(sock, headers, extra)
            shell = path in ('/shell', '/shell-ws')
            return 'ws', ('/shell' if shell else '/tunnel'), tunnel, headers
        if upgrade == 'websocket':
            _http_send(sock, 404, 'Not Found', b'unknown websocket path\n',
                       (('Allow', 'GET /tunnel, GET /shell'),), version=version)
            return None, None, None, None

        # This endpoint only proves that a proxy forwards a request body
        # before the request is finished.  A client can use it to decide
        # between a streaming-request tunnel and the half-duplex fallback.
        if path == '/probe' and method in ('POST', 'PUT'):
            sock.settimeout(HTTP_HANDSHAKE_TIMEOUT)
            if not extra:
                first = sock.recv(1)
                if not first:
                    raise OSError('probe closed before its first body byte')
            _http_send(sock, 200, 'OK', b'probe ok\n', version=version)
            return 'probe', path, None, headers

        if method != 'POST':
            _http_send(sock, 405, 'Method Not Allowed',
                       b'use POST /tunnel (or POST /shell) with chunked encoding\n',
                       (('Allow', 'POST /tunnel, POST /shell'),), version=version)
            return None, None, None, None
        if version == 'HTTP/1.0':
            # HTTP/1.0 has no chunked request body.  The half-duplex /up and
            # /down endpoints are used by the client fallback instead.
            _http_send(sock, 426, 'Upgrade Required',
                       b'HTTP/1.1 with Transfer-Encoding: chunked is required for this endpoint\n',
                       version=version)
            return None, None, None, None
        if path not in ('/tunnel', '/shell'):
            _http_send(sock, 404, 'Not Found', b'unknown tunnel path\n', version=version)
            return None, None, None, None
        transfer_tokens = [item.strip() for item in
                           headers.get('transfer-encoding', '').lower().split(',')
                           if item.strip()]
        if (not transfer_tokens or transfer_tokens[-1] != 'chunked'
                or any(item not in ('identity', 'chunked')
                       for item in transfer_tokens[:-1])):
            _http_send(sock, 411, 'Length Required',
                       b'Transfer-Encoding: chunked is required\n', version=version)
            return None, None, None, None
        encoding = headers.get('content-encoding', '').strip().lower()
        if encoding not in ('', 'identity'):
            _http_send(sock, 415, 'Unsupported Media Type',
                       b'a compressed request body cannot carry a tunnel\n',
                       version=version)
            return None, None, None, None
        _http_send_tunnel_ok(sock, version=version)
        return 'tunnel', path, HttpTunnel(sock, extra), headers

    except (OSError, ValueError) as e:
        try:
            _http_send(sock, 400, 'Bad Request', ('{}\n'.format(e)).encode('utf-8'))
        except OSError:
            pass
        return None, None, None, None


class HttpTunnel(object):
    """A socket-shaped, full-duplex adapter over one HTTP/1.1 exchange.

    A reader thread parses HTTP chunks from the raw connection and forwards
    decoded bytes to one end of a socketpair.  The public object is the other
    end, so select(), recv(), send(), sendall(), shutdown() and the usual
    socket options keep working unchanged.  A chunk is written atomically
    (under a lock), keeping multiple sender threads from interleaving HTTP
    framing.
    """

    def __init__(self, raw, initial=b'', name='http-tunnel'):
        self.raw = raw
        self.name = name
        self._in, self._out = socket.socketpair()
        self._in.settimeout(None)
        # See the client copy: a timeout here only makes close() able to stop
        # a pump whose application side has stopped reading.
        self._out.settimeout(1.0)
        try:
            for sock in (self._in, self._out):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024 * 1024)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
        except OSError:
            pass
        self._buf = bytearray(initial)
        self._send_lock = threading.Lock()
        self._closed = False
        self._resources_closed = False
        self._write_closed = False
        self._reader = threading.Thread(target=self._pump,
                                        name=name + '-reader', daemon=True)
        self._reader.start()

    # -- socket-shaped public API -----------------------------------------
    def fileno(self):
        return self._in.fileno()

    def __getattr__(self, name):
        return getattr(self._in, name)

    def send(self, data):
        if not data:
            return 0
        with self._send_lock:
            if self._closed or self._write_closed:
                raise OSError('HTTP tunnel is closed')
            try:
                self._raw_send(('{:x}\r\n'.format(len(data))).encode('ascii'))
                self._raw_send(data)
                self._raw_send(b'\r\n')
            except OSError:
                self._broken()
                raise
        return len(data)

    def sendall(self, data):
        self.send(data)

    def _end_write(self):
        with self._send_lock:
            if self._closed or self._write_closed:
                return
            self._write_closed = True
            try:
                self._raw_send(b'0\r\n\r\n')
            except OSError:
                self._broken()

    def shutdown(self, how):
        if how in (socket.SHUT_WR, socket.SHUT_RDWR):
            self._end_write()
        if how in (socket.SHUT_RD, socket.SHUT_RDWR):
            try:
                self._in.shutdown(socket.SHUT_RD)
            except OSError:
                pass

    def close(self):
        # _broken() marks the stream closed early, but the descriptors still
        # have to be released: a send failure used to leak the raw socket and
        # both socketpair ends on every reconnect.
        self._closed = True
        if self._resources_closed:
            return
        self._resources_closed = True
        try:
            self.raw.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.raw.close()
        except OSError:
            pass
        for sock in (self._in, self._out):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def _broken(self):
        self._closed = True
        try:
            self.raw.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    # -- raw HTTP body writer ---------------------------------------------
    def _raw_send(self, data):
        view = memoryview(data)
        last_progress = time.time()
        while view:
            try:
                sent = self.raw.send(view)
            except (socket.timeout, TimeoutError):
                # SO_SNDTIMEO bounds one kernel wait.  Treat several waits as
                # one slow transfer instead of dropping a mobile link that is
                # still draining, but still fail when there is no progress.
                if time.time() - last_progress > HTTP_SEND_STALL_TIMEOUT:
                    raise OSError('HTTP tunnel send stalled')
                time.sleep(0.02)
                continue
            if sent <= 0:
                raise OSError('HTTP tunnel closed while sending')
            view = view[sent:]
            last_progress = time.time()

    # -- HTTP body reader / socketpair feeder -----------------------------
    def _raw_read(self, n):
        while len(self._buf) < n:
            try:
                chunk = self.raw.recv(65536)
            except (socket.timeout, TimeoutError):
                raise OSError('HTTP tunnel receive stalled')
            if not chunk:
                raise ConnectionError('HTTP tunnel closed')
            self._buf += chunk
        data = bytes(self._buf[:n])
        del self._buf[:n]
        return data

    def _read_line(self):
        while b'\r\n' not in self._buf:
            if len(self._buf) > HTTP_MAX_LINE:
                raise OSError('HTTP chunk line is too large')
            try:
                chunk = self.raw.recv(65536)
            except (socket.timeout, TimeoutError):
                raise OSError('HTTP tunnel receive stalled')
            if not chunk:
                raise ConnectionError('HTTP tunnel closed')
            self._buf += chunk
        line, _sep, rest = self._buf.partition(b'\r\n')
        if len(line) > HTTP_MAX_LINE:
            raise OSError('HTTP chunk line is too large')
        self._buf = bytearray(rest)
        return bytes(line)

    def _write_local(self, data):
        """Feed decoded body bytes to the application side of the pair."""
        view = memoryview(data)
        while view:
            try:
                sent = self._out.send(view)
            except (socket.timeout, TimeoutError):
                if self._closed:
                    raise ConnectionError('HTTP tunnel closed')
                continue
            if sent <= 0:
                raise ConnectionError('HTTP tunnel closed')
            view = view[sent:]

    def _pump(self):
        try:
            while not self._closed:
                line = self._read_line()
                if not line:
                    # Empty lines between chunks are not part of RFC 7230,
                    # but tolerating them costs nothing and helps with a
                    # permissive proxy.
                    continue
                size_text = line.split(b';', 1)[0].strip()
                try:
                    size = int(size_text, 16)
                except ValueError:
                    raise OSError('invalid HTTP chunk size')
                if size < 0 or size > HTTP_MAX_CHUNK:
                    raise OSError('HTTP chunk is too large')
                if size == 0:
                    # Consume optional trailers, then signal EOF on our side.
                    trailer_bytes = 0
                    trailer_count = 0
                    while True:
                        trailer = self._read_line()
                        trailer_bytes += len(trailer) + 2
                        if trailer_bytes > HTTP_MAX_HEADER:
                            raise OSError('HTTP trailers are too large')
                        if not trailer:
                            break
                        trailer_count += 1
                        if trailer_count > 128:
                            raise OSError('too many HTTP trailers')
                    break
                remaining = size
                while remaining:
                    piece = self._raw_read(min(remaining, 65536))
                    if not piece:
                        raise ConnectionError('HTTP chunk ended early')
                    self._write_local(piece)
                    remaining -= len(piece)
                if self._raw_read(2) != b'\r\n':
                    raise OSError('missing HTTP chunk terminator')
        except (OSError, ValueError, ConnectionError):
            pass
        finally:
            try:
                self._out.shutdown(socket.SHUT_WR)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# WebSocket transport
#
# RFC 6455 is intentionally implemented here with the standard library only.
# A WebSocket is an HTTP/1.1 upgrade, so it passes through ngrok, Pinggy and
# ordinary reverse proxies as one long-lived request.  Unlike a streaming POST
# body, it cannot be delayed by a proxy that buffers request bodies.  Binary
# frames carry the unchanged application stream; ping/pong frames keep middle
# boxes and mobile NATs alive without polluting the application protocol.
# ---------------------------------------------------------------------------

WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'


def _ws_apply_mask(data, mask):
    """Byte-wise RFC 6455 XOR using one big-integer operation.

    A Python per-byte loop (and even a per-32-bit-word loop) was the limiting
    factor for downloads.  int.from_bytes/xor/to_bytes runs in C and keeps the
    same bytewise semantics on little- and big-endian hosts.  WebSocket frames
    are already split to CMON_WS_MAX_FRAME by send(), so this stays bounded.
    """
    if not data:
        return b''
    if len(mask) != 4:
        raise OSError('invalid WebSocket mask')
    length = len(data)
    pattern = mask * (length // 4) + mask[:length % 4]
    return (int.from_bytes(data, 'big') ^
            int.from_bytes(pattern, 'big')).to_bytes(length, 'big')

def _ws_accept_value(key):
    return base64.b64encode(hashlib.sha1((key + WS_GUID).encode('ascii')).digest()).decode('ascii')


def _ws_server_upgrade(sock, headers, initial=b''):
    """Finish a server-side WebSocket handshake and return the adapter."""
    key = headers.get('sec-websocket-key', '').strip()
    version = headers.get('sec-websocket-version', '').strip()
    if not key or version not in ('13', '8'):
        raise OSError('missing or unsupported Sec-WebSocket key/version')
    try:
        if len(base64.b64decode(key.encode('ascii'), validate=True)) != 16:
            raise ValueError
    except Exception:
        raise OSError('invalid Sec-WebSocket-Key')
    protocols = [p.strip() for p in headers.get('sec-websocket-protocol', '').split(',') if p.strip()]
    selected = CMON_WS_PROTOCOL if CMON_WS_PROTOCOL in protocols else ''
    response = (
        'HTTP/1.1 101 Switching Protocols\r\n'
        'Upgrade: websocket\r\n'
        'Connection: Upgrade\r\n'
        'Sec-WebSocket-Accept: {accept}\r\n'
        '{protocol}'
        'Cache-Control: no-store\r\n'
        '{marker}: {value}\r\n'
        'X-CMon-Version: {ver}\r\n'
        '\r\n'
    ).format(accept=_ws_accept_value(key),
             protocol=('Sec-WebSocket-Protocol: ' + selected + '\r\n') if selected else '',
             marker=CMON_MARKER_HEADER, value=CMON_MARKER_VALUE,
             ver=CMON_VERSION).encode('ascii')
    sock.sendall(response)
    return WebSocketTunnel(sock, server_side=True, initial=initial)


class WebSocketTunnel(object):
    """Socket-shaped duplex adapter over RFC 6455 binary frames.

    The application sees only the byte stream.  Frame boundaries, ping/pong,
    close and masking are transport details.  A local socketpair lets the
    existing controller/client code keep using select(), recv(), sendall(),
    shutdown() and socket options without knowing about WebSockets.
    """

    def __init__(self, raw, server_side=False, initial=b'', name='websocket'):
        self.raw = raw
        self.server_side = server_side
        self.name = name
        self._in, self._out = socket.socketpair()
        self._in.settimeout(None)
        self._out.settimeout(1.0)
        try:
            for sock in (self._in, self._out):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024 * 1024)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
        except OSError:
            pass
        self._buf = bytearray(initial)
        self._send_lock = threading.Lock()
        self._closed = False
        self._resources_closed = False
        self._write_closed = False
        self._last_rx = time.monotonic()
        self._next_ping = self._last_rx + CMON_WS_PING
        try:
            self.raw.settimeout(1.0)
        except (AttributeError, OSError):
            pass
        self._reader = threading.Thread(target=self._pump,
                                        name=name + '-reader', daemon=True)
        self._reader.start()

    # -- socket-shaped public API -----------------------------------------
    def fileno(self):
        return self._in.fileno()

    def __getattr__(self, name):
        return getattr(self._in, name)

    def send(self, data):
        if not data:
            return 0
        with self._send_lock:
            if self._closed or self._write_closed:
                raise OSError('WebSocket tunnel is closed')
            try:
                view = memoryview(data)
                while view:
                    piece = view[:CMON_WS_MAX_FRAME]
                    self._raw_send(self._frame(piece, 0x2))
                    view = view[len(piece):]
            except OSError:
                self._broken()
                raise
        return len(data)

    def sendall(self, data):
        self.send(data)

    def _frame(self, payload, opcode):
        payload = bytes(payload)
        length = len(payload)
        if length > MAX_MSG:
            raise OSError('WebSocket frame is too large')
        first = 0x80 | (opcode & 0x0f)      # FIN + opcode
        mask_bit = 0x00 if self.server_side else 0x80
        if length < 126:
            header = struct.pack('!BB', first, mask_bit | length)
        elif length < 65536:
            header = struct.pack('!BBH', first, mask_bit | 126, length)
        else:
            header = struct.pack('!BBQ', first, mask_bit | 127, length)
        if self.server_side:
            return header + payload
        mask = os.urandom(4)
        return header + mask + _ws_apply_mask(payload, mask)

    def _end_write(self):
        with self._send_lock:
            if self._closed or self._write_closed:
                return
            self._write_closed = True
            try:
                self._raw_send(self._frame(b'', 0x8))
            except OSError:
                self._broken()

    def shutdown(self, how):
        if how in (socket.SHUT_WR, socket.SHUT_RDWR):
            self._end_write()
        if how in (socket.SHUT_RD, socket.SHUT_RDWR):
            try:
                self._in.shutdown(socket.SHUT_RD)
            except OSError:
                pass

    def close(self):
        self._closed = True
        self._end_write()
        if self._resources_closed:
            return
        self._resources_closed = True
        try:
            self.raw.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.raw.close()
        except OSError:
            pass
        for sock in (self._in, self._out):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def _broken(self):
        self._closed = True
        try:
            self.raw.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    # -- raw socket writer / reader ---------------------------------------
    def _raw_send(self, data):
        view = memoryview(data)
        last_progress = time.time()
        while view:
            try:
                sent = self.raw.send(view)
            except (socket.timeout, TimeoutError):
                if time.time() - last_progress > HTTP_SEND_STALL_TIMEOUT:
                    raise OSError('WebSocket send stalled')
                time.sleep(0.02)
                continue
            if sent <= 0:
                raise OSError('WebSocket closed while sending')
            view = view[sent:]
            last_progress = time.time()

    def _send_control(self, opcode, payload=b''):
        with self._send_lock:
            if self._closed:
                return
            try:
                self._raw_send(self._frame(payload, opcode))
            except OSError:
                self._broken()

    def _raw_read(self, n):
        while len(self._buf) < n:
            if self._closed:
                raise ConnectionError('WebSocket tunnel closed')
            now = time.monotonic()
            if now >= self._next_ping:
                self._next_ping = now + CMON_WS_PING
                self._send_control(0x9, b'cmon')
            if now - self._last_rx > CMON_WS_DEAD:
                raise OSError('WebSocket peer stopped answering')
            try:
                chunk = self.raw.recv(65536)
            except (socket.timeout, TimeoutError):
                continue
            if not chunk:
                raise ConnectionError('WebSocket tunnel closed')
            self._buf += chunk
            self._last_rx = time.monotonic()
        data = bytes(self._buf[:n])
        del self._buf[:n]
        return data

    def _write_local(self, data):
        view = memoryview(data)
        while view:
            try:
                sent = self._out.send(view)
            except (socket.timeout, TimeoutError):
                if self._closed:
                    raise ConnectionError('WebSocket tunnel closed')
                continue
            if sent <= 0:
                raise ConnectionError('WebSocket tunnel closed')
            view = view[sent:]

    def _read_frame(self):
        first, second = struct.unpack('!BB', self._raw_read(2))
        fin = bool(first & 0x80)
        opcode = first & 0x0f
        masked = bool(second & 0x80)
        length = second & 0x7f
        if length == 126:
            length = struct.unpack('!H', self._raw_read(2))[0]
        elif length == 127:
            length = struct.unpack('!Q', self._raw_read(8))[0]
        if length > MAX_MSG:
            raise OSError('WebSocket peer sent an oversized frame')
        mask = self._raw_read(4) if masked else b''
        payload = self._raw_read(length) if length else b''
        if masked:
            payload = _ws_apply_mask(payload, mask)
        self._last_rx = time.monotonic()
        return fin, opcode, payload

    def _pump(self):
        try:
            while not self._closed:
                fin, opcode, payload = self._read_frame()
                if opcode == 0x8:              # close
                    self._send_control(0x8, payload[:125])
                    break
                if opcode == 0x9:              # ping
                    self._send_control(0xa, payload[:125])
                    continue
                if opcode == 0xa:              # pong
                    continue
                if opcode in (0x0, 0x1, 0x2):
                    if payload:
                        self._write_local(payload)
                    if fin:
                        continue
                    continue
                # Reserved control/data opcodes are ignored; the connection
                # can continue because our stream framing does not depend on
                # message boundaries.
        except (OSError, ValueError, ConnectionError, struct.error):
            pass
        finally:
            try:
                self._out.shutdown(socket.SHUT_WR)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# HTTPS certificate handling.  A self-signed certificate is generated once if
# OpenSSL is available; explicit PEM files can be supplied with
# CONTROLLER_TLS_CERT / CONTROLLER_TLS_KEY (or CONTROLLER_TLS_DIR).
# ---------------------------------------------------------------------------

def _tls_paths():
    base = (os.environ.get('CONTROLLER_TLS_DIR')
            or os.path.join(os.path.expanduser('~'), '.controller-tls'))
    cert = os.environ.get('CONTROLLER_TLS_CERT') or os.path.join(base, 'cert.pem')
    key = os.environ.get('CONTROLLER_TLS_KEY') or os.path.join(base, 'key.pem')
    return os.path.abspath(cert), os.path.abspath(key)


def _make_self_signed_cryptography(cert, key, ips, dns_names=None):
    """Optional pure-Python certificate generation for machines without the
    openssl executable (notably a default Windows Python install).

    Returns True when it generated the pair, False when `cryptography` is not
    installed.  The same SANs as the OpenSSL path are used, so a client can
    verify the certificate with CONTROLLER_TLS_VERIFY=1 and the generated cert
    as its CA bundle."""
    try:
        import datetime
        import ipaddress
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    except ImportError:
        return False

    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    key_obj = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, 'controller'),
    ])
    san = [x509.DNSName(value) for value in (dns_names or ['localhost'])]
    for value in ips or ['127.0.0.1']:
        try:
            san.append(x509.IPAddress(ipaddress.ip_address(value)))
        except ValueError:
            continue
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key_obj.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(digital_signature=True, content_commitment=False,
                          key_encipherment=True, data_encipherment=False,
                          key_agreement=False, key_cert_sign=False,
                          crl_sign=False, encipher_only=False,
                          decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
                       critical=False)
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
    )
    cert_obj = builder.sign(key_obj, hashes.SHA256())
    cert_pem = cert_obj.public_bytes(serialization.Encoding.PEM)
    key_pem = key_obj.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption())
    with open(cert, 'wb') as f:
        f.write(cert_pem)
    with open(key, 'wb') as f:
        f.write(key_pem)
    try:
        os.chmod(key, 0o600)
    except OSError:
        pass
    try:
        os.chmod(cert, 0o644)
    except OSError:
        pass
    return True


def _make_self_signed(cert, key):
    """Generate a self-signed certificate with openssl, falling back to the
    optional `cryptography` module when the openssl executable is absent.

    OpenSSL 1.0+ accepts the configuration form used here.  The fallback makes
    the one-line HTTPS switch usable on a stock Windows Python install too;
    neither path requires a third-party package in the normal server case."""
    for path in (cert, key):
        base = os.path.dirname(path)
        try:
            os.makedirs(base, mode=0o700, exist_ok=True)
        except OSError as e:
            raise OSError('cannot create TLS directory {}: {}'.format(base, e))

    ips = []
    for value in ['127.0.0.1'] + local_ipv4_addresses():
        value = str(value)
        if value and value not in ips:
            ips.append(value)
    ips = ips or ['127.0.0.1']
    dns_names = []
    for value in ('localhost', socket.gethostname(), socket.getfqdn()):
        value = str(value or '').strip()
        if value and value not in dns_names:
            dns_names.append(value)

    openssl = shutil.which('openssl')
    if not openssl:
        if _make_self_signed_cryptography(cert, key, ips, dns_names):
            return
        raise OSError(
            'HTTPS needs a certificate, but openssl was not found and the '
            'Python cryptography module is not installed. Install OpenSSL, '
            'install cryptography (pip install cryptography), or set '
            'CONTROLLER_TLS_CERT and CONTROLLER_TLS_KEY to PEM files (for '
            'example from an internal CA).')
    alt = ['DNS.{} = {}'.format(index, value)
           for index, value in enumerate(dns_names or ['localhost'], 1)]
    for index, value in enumerate(ips, 1):
        alt.append('IP.{} = {}'.format(index, value))
    config = (
        '[req]\n'
        'prompt = no\n'
        'distinguished_name = distinguished_name\n'
        'x509_extensions = v3_req\n'
        '[distinguished_name]\n'
        'CN = controller\n'
        '[v3_req]\n'
        'basicConstraints = CA:FALSE\n'
        'keyUsage = digitalSignature,keyEncipherment\n'
        'extendedKeyUsage = serverAuth\n'
        'subjectAltName = @alt_names\n'
        '[alt_names]\n'
        + '\n'.join(alt) + '\n'
    )
    cfg_fd, cfg_path = tempfile.mkstemp(prefix='controller-tls-', suffix='.cnf')
    try:
        with os.fdopen(cfg_fd, 'w') as f:
            f.write(config)
        cmd = [openssl, 'req', '-x509', '-newkey', 'rsa:2048', '-sha256',
               '-days', '3650', '-nodes', '-keyout', key, '-out', cert,
               '-config', cfg_path]
        try:
            result = subprocess.run(cmd, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, timeout=45)
        except (OSError, subprocess.SubprocessError) as e:
            raise OSError('could not run openssl: {}'.format(e))
        if result.returncode != 0:
            detail = result.stderr.decode('utf-8', 'replace').strip()
            raise OSError('openssl could not create the HTTPS certificate: {}'
                          .format(detail[-400:] or 'unknown error'))
    except OSError:
        # OpenSSL may be present but broken/minimal (some Windows packages).
        # Use the optional Python implementation as a second chance.
        if _make_self_signed_cryptography(cert, key, ips, dns_names):
            return
        raise
    finally:
        try:
            os.remove(cfg_path)
        except OSError:
            pass
    try:
        os.chmod(key, 0o600)
    except OSError:
        pass
    try:
        os.chmod(cert, 0o644)
    except OSError:
        pass


def _server_tls_context():
    """Load (or create) the HTTPS server certificate and return a context."""
    cert, key = _tls_paths()
    if not (os.path.isfile(cert) and os.path.isfile(key)):
        _make_self_signed(cert, key)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        context.minimum_version = ssl.TLSVersion.TLSv1_2
    except AttributeError:
        pass
    # Set the same safe modern defaults on every platform, and keep the
    # negotiated version dynamic (TLS 1.2 and 1.3 are both accepted).
    try:
        context.options |= ssl.OP_NO_COMPRESSION
    except AttributeError:
        pass
    try:
        context.load_cert_chain(cert, key)
    except (OSError, ssl.SSLError) as e:
        raise OSError('cannot load HTTPS certificate {} / {}: {}'
                      .format(cert, key, e))
    try:
        context.set_alpn_protocols(['http/1.1'])
    except (AttributeError, NotImplementedError):
        pass
    return context, cert, key


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


def read_hello(conn, timeout=1.5, require=False):
    """Read the hello a fresh client sends right after connecting and return
    its environment report ({} when an older client sends none).  New
    transports use a longer budget and require the hello: this is also the
    proof that a tunnel really forwards the request body in both directions.
    The 10 s read budget is for the whole message: a peer sending one byte
    every few seconds used to hold the only accept thread for minutes."""
    if _socket_pending(conn) <= 0:
        try:
            readable, _, _ = select.select([conn], [], [], timeout)
        except OSError:
            return None if require else {}
        if conn not in readable:
            return None if require else {}
    try:
        msg = recv_msg(conn, deadline=time.time() + max(10, timeout))
    except OSError:
        msg = None
    finally:
        try:
            conn.settimeout(None)
        except OSError:
            pass
    if isinstance(msg, dict) and msg.get('type') == 'hello':
        return _sanitize_env(msg.get('env') or {})
    return None if require else {}


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

        class Handler(http_server.BaseHTTPRequestHandler):
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

        self.server = http_server.ThreadingHTTPServer(
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
                # Do not leave an HttpTunnel's raw socket, reader thread and
                # socketpair open until the garbage collector happens to run.
                self.close()
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
                self.close()
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
SHELL_CHANNELS = queue.Queue(maxsize=8)  # http/https shell channels on PORT


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
        try:
            # Raw input must not imply raw *output*.  Without OPOST/ONLCR a
            # client shell's newline moves down but leaves the cursor in the
            # old column, so prompts and command output overwrite each other.
            # Keep output post-processing on; only input is raw.
            attrs = termios.tcgetattr(fd)
            attrs[1] |= termios.OPOST | termios.ONLCR
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
        except (AttributeError, termios.error):
            pass
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

    The shell always reuses the public endpoint the client already reached:
    raw TCP/TLS opens a second connection with an authenticated CMON2 line,
    while HTTP/HTTPS/WebSocket uses /shell on the same listener.  No temporary
    listener is created, so the same code works on a LAN, through a VMware NAT,
    and through a Pinggy/ngrok TCP or HTTP tunnel.  Type 'exit' (or Ctrl+D) in
    that shell to return to this prompt; press Ctrl+] here to force it closed.
    Returns False only when the client's main connection is gone."""
    global SHELL_ACTIVE
    if not sys.stdin.isatty():
        print("[!] The interactive shell needs a real terminal (stdin is not a TTY).")
        return True
    STOP_REQUEST.clear()                # a stray Ctrl+Z must not stop a later job

    op = None
    token = secrets.token_urlsafe(24)
    try:
        # A stale channel from an earlier timed-out request must not be
        # mistaken for this shell.  The token check below is the real guard;
        # closing stale adapters also keeps their descriptors from leaking.
        while True:
            try:
                stale = SHELL_CHANNELS.get_nowait()
            except queue.Empty:
                break
            try:
                stale[0].close()
            except (OSError, IndexError):
                pass

        # The answer to this request (a refusal, e.g. "a shell is already
        # running") arrives on the reader thread like any other answer.
        op = link.add(Op('simple', next_request_id(), label='interactive shell'))
        try:
            link.send({'type': 'shell', 'id': op.id, 'token': token,
                       'version': 2})
        except OSError:
            link.mark_lost()
            return False

        print("[+] Opening an interactive shell on the client...")
        conn = None
        addr = None
        deadline = time.time() + SHELL_ACCEPT_TIMEOUT
        while conn is None:
            if link.dead:
                return False
            if op.done.is_set():            # the client refused (see the note above)
                flush_async()
                return True
            remaining = deadline - time.time()
            if remaining <= 0:
                print("[!] The client did not open the shell channel. "
                      "A tunnel may have dropped the second connection.")
                return True
            try:
                candidate, candidate_addr, candidate_token = SHELL_CHANNELS.get(
                    timeout=min(0.2, remaining))
            except (queue.Empty, ValueError):
                continue
            if not candidate_token or not secrets.compare_digest(
                    str(candidate_token), token):
                # A shell channel must prove it belongs to this control link.
                # Address matching alone is useless behind a tunnel, where
                # every connection appears to come from 127.0.0.1.
                try:
                    candidate.close()
                except OSError:
                    pass
                print("[!] Refused an unauthenticated shell connection.")
                continue
            conn, addr = candidate, candidate_addr

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


class PrefixSocket(object):
    """A socket with a few bytes already consumed by protocol detection.

    recv() returns the prefix first, then delegates to the real socket.  Every
    other operation (send, fileno, settimeout, shutdown, socket options, ...)
    goes straight to the raw socket, so HttpTunnel/ClientLink and select() see
    the same object they always did.
    """

    def __init__(self, raw, prefix=b''):
        self.raw = raw
        self._prefix = bytearray(prefix)

    def recv(self, size, flags=0):
        if self._prefix:
            if flags & getattr(socket, 'MSG_PEEK', 0):
                return bytes(self._prefix[:size])
            data = bytes(self._prefix[:size])
            del self._prefix[:size]
            return data
        return self.raw.recv(size, flags)

    def __getattr__(self, name):
        return getattr(self.raw, name)

    def close(self):
        try:
            self.raw.close()
        except OSError:
            pass

    def shutdown(self, how):
        try:
            self.raw.shutdown(how)
        except OSError:
            pass


def _read_initial(conn, want=8, timeout=HTTP_HANDSHAKE_TIMEOUT):
    """Read enough bytes to identify the first protocol without blocking a
    normal client forever.  The bytes are not discarded; PrefixSocket replays
    them to the selected handler."""
    data = bytearray()
    try:
        conn.settimeout(timeout)
    except (AttributeError, OSError):
        pass
    try:
        while len(data) < want:
            chunk = conn.recv(want - len(data))
            if not chunk:
                break
            data += chunk
            # "CMON2\n" is only six bytes; waiting for eight would deadlock
            # until the handshake timeout because the client waits for our ack.
            if data.startswith(b'CMON2') and b'\n' in data:
                break
    except (socket.timeout, TimeoutError):
        pass
    finally:
        try:
            conn.settimeout(None)
        except (AttributeError, OSError):
            pass
    return bytes(data)


def _read_magic_line(prefixed):
    buf = bytearray(prefixed._prefix)
    while b'\n' not in buf:
        if len(buf) > 2048:
            raise OSError('CMON2 handshake line is too long')
        chunk = prefixed.raw.recv(256)
        if not chunk:
            raise OSError('client closed during the CMON2 handshake')
        buf += chunk
    end = buf.index(b'\n') + 1
    line = bytes(buf[:end])
    prefixed._prefix = bytearray(buf[end:])
    return line


def _looks_like_http(data):
    return any(data.startswith(method) for method in
               (b'GET ', b'POST ', b'HEAD ', b'PUT ', b'OPTIONS ',
                b'CONNECT ', b'PATCH ', b'DELETE ', b'PRI '))


def _finish_client_link(client_socket, addr, env, transport_label, new_protocol):
    """Create/register one control link after its hello has been read."""
    link = ClientLink(client_socket, addr)
    link.number = _next_session_number()
    link.env = env
    link.on_dead = _link_died
    if new_protocol:
        # This acknowledgement is the final end-to-end proof: it only reaches
        # the client after its hello arrived here.  A proxy that buffers
        # request bodies fails here and the client falls back to half-duplex.
        link.send({'type': 'hello_ack', 'transport': transport_label,
                   'version': CMON_VERSION})
    _async_print("[+] Client connected: {} [{}]".format(addr, transport_label))
    try:
        env_text = _capture_text(print_client_env, env)
    except Exception as e:
        env_text = "[!] Bad environment report from the client: {}".format(e)
    if env_text:
        link.say(env_text)
    if link.dead:
        link.close()
        return link
    became_active = _register_link(link)
    if link.dead:
        link.close()
        return link
    if became_active:
        return link                # the prompt shows the help right away
    current = _active_link()
    _async_print("[+] Session {} is ready (active: session {}). "
                 "'session {}' switches to it.".format(
                     link.number, current.number if current else '?', link.number))
    return link


def _finish_control_link(client_socket, addr, transport_label, new_protocol,
                         timeout):
    """Read one control hello in a worker, never in the accept loop.

    A buffering HTTP proxy may hold a POST body for its full timeout.  If the
    accept loop waited for that hello, it could not accept the /down and /up
    requests needed by the client's half-duplex fallback (or a second client).
    """
    try:
        env = read_hello(client_socket, timeout=timeout, require=new_protocol)
        if env is None:
            raise OSError('client did not complete the application hello')
        _finish_client_link(client_socket, addr, env, transport_label,
                            new_protocol)
    except Exception as e:
        try:
            client_socket.close()
        except OSError:
            pass
        _async_print("[!] Dropped a bad connection from {}: {}".format(addr, e))
    finally:
        _CONTROL_WORKERS.release()


MAX_CONNECTION_WORKERS = 256
_CONTROL_WORKERS = threading.BoundedSemaphore(MAX_CONNECTION_WORKERS)
_CONNECTION_WORKERS = threading.BoundedSemaphore(MAX_CONNECTION_WORKERS)


def _serve_connection(raw_socket, addr, tls_context=None):
    """Identify and serve one accepted connection.

    This runs in its own thread so a TLS handshake, a slow HTTP header, or a
    tunnel that buffers a request body can never stop the listener from
    accepting /down, /up, health checks, or another client.  The function
    returns after handing a live control/shell connection to its owner."""
    client_socket = raw_socket
    link = None
    keep_open = False
    new_protocol = False
    transport_label = 'TCP'
    try:
        _tune_link_socket(raw_socket)
        # Peek before wrapping: the TLS ClientHello starts with 0x16.
        # MSG_PEEK is only used on the plain socket (SSLSocket does not
        # consistently support it), and the client is still waiting for
        # our answer, so this cannot race with application data.
        first = b''
        try:
            raw_socket.settimeout(HTTP_HANDSHAKE_TIMEOUT)
            first = raw_socket.recv(1, socket.MSG_PEEK)
        except (socket.timeout, TimeoutError):
            first = b''
        if first == b'\x16':
            if tls_context is None:
                raise OSError('TLS connection but no server certificate is ready')
            try:
                client_socket = tls_context.wrap_socket(raw_socket,
                                                        server_side=True)
                client_socket.settimeout(None)
            except (OSError, ssl.SSLError) as e:
                raise OSError('TLS handshake failed: {}'.format(e))
            _tune_link_socket(client_socket)
            transport_label = 'HTTPS'
        initial = _read_initial(client_socket, 8, HTTP_HANDSHAKE_TIMEOUT)
        if not initial:
            raise OSError('client closed before sending anything')

        if initial.startswith(b'CMON2'):
            # Raw TCP/TLS.  A line (not a guessed JSON length) is used so
            # a shell channel can carry an unguessable token and so a
            # public TCP tunnel has an unambiguous handshake.
            new_protocol = True
            prefixed = PrefixSocket(client_socket, initial)
            client_socket = prefixed
            line = _read_magic_line(prefixed)
            text = line.decode('ascii', 'replace').strip()
            if text.startswith('CMON2 SHELL '):
                token = text[len('CMON2 SHELL '):].strip()
                if not token:
                    prefixed.sendall(b'CMON2 ERR missing-token\n')
                    raise OSError('shell handshake has no token')
                prefixed.sendall(CMON_ACK)
                try:
                    SHELL_CHANNELS.put_nowait((prefixed, addr, token))
                except queue.Full:
                    try:
                        prefixed.close()
                    except OSError:
                        pass
                else:
                    keep_open = True
                return
            if text == 'CMON2' or text.startswith('CMON2 CTRL'):
                prefixed.sendall(CMON_ACK)
                transport_label = 'TLS' if transport_label == 'HTTPS' else 'TCP'
            else:
                prefixed.sendall(b'CMON2 ERR unknown-command\n')
                raise OSError('unknown CMON2 handshake')

        elif _looks_like_http(initial):
            # HTTP/1.0, HTTP/1.1, HTTPS through a TLS-terminating tunnel,
            # health checks, WebSocket and the full-duplex tunnel body.
            prefixed = PrefixSocket(client_socket, initial)
            client_socket = prefixed
            kind, path, tunnel, headers = _http_accept(prefixed)
            if kind == 'half-up':
                # A /up connection may carry several Content-Length requests
                # (one persistent TLS handshake through a tunnel instead of a
                # fresh handshake for every keystroke).  Only loop when the
                # client explicitly asked for keep-alive.
                if not _http_wants_keep_alive(headers):
                    return
                while True:
                    kind, path, tunnel, headers = _http_accept(prefixed)
                    if (kind == 'half-up'
                            and _http_wants_keep_alive(headers)):
                        continue
                    return
            if kind in ('health', 'probe') or tunnel is None:
                return
            new_protocol = True
            if kind == 'half' and getattr(tunnel, 'role', '') == 'shell':
                token = getattr(tunnel, 'token', '')
                if not token:
                    tunnel.close()
                    raise OSError('half-duplex shell request has no token')
                try:
                    SHELL_CHANNELS.put_nowait((tunnel, addr, token))
                except queue.Full:
                    tunnel.close()
                else:
                    keep_open = True
                return
            if path == '/shell':
                token = (headers or {}).get('x-cmon-token', '')
                if not token:
                    tunnel.close()
                    raise OSError('HTTP shell request has no token')
                try:
                    SHELL_CHANNELS.put_nowait((tunnel, addr, token))
                except queue.Full:
                    tunnel.close()
                else:
                    keep_open = True
                return
            client_socket = tunnel
            if kind == 'half':
                transport_label = 'HTTP-half'
            else:
                transport_label = ('HTTPS' if transport_label == 'HTTPS'
                                   else 'HTTP')
        else:
            # Legacy client: it starts directly with the 4-byte length +
            # JSON hello.  Keep it working without a magic prefix.
            client_socket = PrefixSocket(client_socket, initial)
            transport_label = 'TCP-legacy'

        # Every control handshake runs in its own small worker.  That keeps
        # the single accept thread free for tunneled /up requests, health
        # probes and additional clients even when a middlebox buffers a
        # request body for several seconds.
        if not _CONTROL_WORKERS.acquire(blocking=False):
            raise OSError('too many pending control handshakes')
        keep_open = True
        try:
            threading.Thread(
                target=_finish_control_link,
                args=(client_socket, addr, transport_label, new_protocol,
                      TUNNEL_PROBE_TIMEOUT if new_protocol else 1.5),
                name='control-{}'.format(addr[1]), daemon=True).start()
        except Exception:
            keep_open = False
            _CONTROL_WORKERS.release()
            raise
        return
    except Exception as e:
        if link is not None:
            link.close()
        _async_print("[!] Dropped a bad connection from {}: {}".format(addr, e))
    finally:
        if not keep_open:
            try:
                client_socket.close()
            except OSError:
                pass
            if client_socket is not raw_socket:
                try:
                    raw_socket.close()
                except OSError:
                    pass



def _connection_worker(raw_socket, addr, tls_context):
    """Run one connection handler and release its concurrency slot."""
    try:
        _serve_connection(raw_socket, addr, tls_context)
    finally:
        _CONNECTION_WORKERS.release()


def _accept_loop(server_socket, tls_context=None):
    """Accept every connection and serve each one in its own worker thread.

    One listener carries raw TCP, direct TLS, HTTP/1.0/1.1, HTTPS, WebSocket,
    health probes, interactive shells, and traffic arriving through a Pinggy
    or ngrok tunnel.  Nothing a client sends may kill this thread: a malformed
    handshake closes only that one connection.
    """
    while True:
        try:
            raw_socket, addr = server_socket.accept()
        except OSError:
            return                      # the controller is shutting down
        if not _CONNECTION_WORKERS.acquire(blocking=False):
            try:
                raw_socket.close()
            except OSError:
                pass
            continue
        try:
            threading.Thread(
                target=_connection_worker,
                args=(raw_socket, addr, tls_context),
                name='conn-{}'.format(addr[1] if isinstance(addr, tuple)
                                      else addr),
                daemon=True).start()
        except Exception:
            _CONNECTION_WORKERS.release()
            try:
                raw_socket.close()
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
    print("[i] All of these are equivalent and auto-detected:")
    print("        python client.py tcp://{}:{}".format(addrs[0], port))
    print("        python client.py http://{}:{}".format(addrs[0], port))
    print("        python client.py https://{}:{}".format(addrs[0], port))
    print("[i] For a public link, start a tunnel to this port and give the client")
    print("    the URL/host:port the tunnel prints.  Examples:")
    print("        ssh -p 443 -R0:localhost:{} free.pinggy.io".format(port))
    print("        python client.py https://<name>.run.pinggy-free.link")
    print("        ssh -p 443 -R0:localhost:{} tcp@free.pinggy.io".format(port))
    print("        python client.py tcp://<host>:<port>   # Pinggy raw TCP URL")
    print("        ngrok http {}".format(port))
    print("        python client.py https://<name>.ngrok-free.app")
    print("        ngrok tcp {}".format(port))
    print("[i] Pinggy free HTTP URLs expire after 60 minutes.  Restart the")
    print("    tunnel and give the client the new URL; the controller stays up.")
    print("    Open WebSockets pass through both Pinggy and ngrok.")
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
    if CONNECTION_TYPE not in VALID_CONNECTION_TYPES:
        print("[!] Unknown connection_type {!r}. Use auto, tcp, http or https."
              .format(CONNECTION_TYPE))
        return
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

    tls_context = None
    tls_cert = tls_key = None
    try:
        tls_context, tls_cert, tls_key = _server_tls_context()
        print("[+] Direct-TLS certificate: {}".format(tls_cert))
    except OSError as e:
        # Plain TCP and plain HTTP remain fully usable.  A public tunnel
        # terminates TLS at its edge, so it does not need a local certificate
        # at all.
        print("[i] Direct TLS is unavailable on this machine: {}".format(e))
        print("    TCP and HTTP still work, and Pinggy/ngrok HTTPS URLs still work")
        print("    because those services terminate TLS at their edge.")
    if tls_context is not None:
        print("[i] TLS 1.2/1.3 are negotiated dynamically.  The tunnel inside")
        print("    TLS is HTTP/1.1 (the Python standard library has no HTTP/2).")
        print("[i] A client on a private LAN may accept this self-signed cert.")
        print("    Public tunnel certificates are verified normally.  For a")
        print("    verified direct-LAN certificate set CONTROLLER_TLS_VERIFY=1")
        print("    and CONTROLLER_TLS_CA to a CA bundle in the client environment.")

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

    server_socket.listen(64)
    print("[+] Controller v20 listening on port {} ({})".format(
        PORT, _http_connection_name()))
    print("[i] One port now accepts everything automatically: raw TCP, direct TLS,")
    print("    HTTP/1.0, HTTP/1.1, HTTPS, WebSocket, Pinggy and ngrok traffic.")
    print("[i] Start the client with any form it understands:  IP, host:port,")
    print("    tcp://host:port, http://..., https://..., or a full tunnel URL.")
    print("[i] No matching connection_type is required.  'auto' is recommended;")
    print("    tcp/http/https are only preferences shown by the client.")
    if tls_context is None:
        print("[i] Direct https://host:port is disabled until a certificate is ready;")
        print("    HTTPS tunnel URLs are unaffected.")
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
        acceptor = threading.Thread(target=_accept_loop,
                                    args=(server_socket, tls_context),
                                    name='session-acceptor')
        acceptor.daemon = True
        acceptor.start()
        repl()
    except KeyboardInterrupt:
        print("\n[!] Interrupted by user.")
    finally:
        _shutdown_sessions()
        while True:
            try:
                stale = SHELL_CHANNELS.get_nowait()
            except queue.Empty:
                break
            try:
                stale[0].close()
            except (OSError, IndexError):
                pass
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
