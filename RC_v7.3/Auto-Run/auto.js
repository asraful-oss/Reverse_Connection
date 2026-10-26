#!/usr/bin/env node
// auto.js — Install a script to run on every reboot/login, and remove it with --stop.
//
// Node.js port of auto.cs, with the same cross-platform, cross-language behaviour.
// It detects the language from the target file's extension and makes it "just run":
//
//     auto.js  app.py   --hidden      # Python
//     auto.js  app.js   --hidden      # JavaScript
//     auto.js  app.ts   --hidden      # TypeScript
//     auto.js  app.cs   --hidden      # C#
//     auto.js  app.sh   --hidden      # POSIX shell
//
// If the matching runtime is not already installed, auto.js quietly fetches an
// official portable copy into ~/.configs/runtime (the user's home dir, no admin,
// no prompts) and uses that.  Node runs .js and .ts; Python runs .py; the .NET
// SDK runs .cs; the system shell runs .sh.
//
// Autostart mechanisms (no admin / root anywhere):
//   Linux desktop (GNOME/KDE/XFCE/MATE/Cinnamon/Budgie) : XDG ~/.config/autostart
//   Linux Hyprland / Omarchy / wlroots                  : compositor config / systemd
//   Linux headless server                               : systemd user unit, else cron @reboot
//   macOS                                               : per-user LaunchAgent
//   Windows 10/11                                       : HKCU ...\Run key + generated WSH launcher
//   Android (Termux)                                    : Termux:Boot script
//   iOS                                                 : explicitly unsupported (platform contract)
//
// The script COPY keeps its original extension; window visibility is handled by the
// launcher, not by renaming the file. Requires Node.js to run auto.js itself.

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const cp = require('child_process');

// ================================================================
// Paths (BaseDir / StateFile may be re-pointed by hideInstallDir)
// ================================================================
let BaseDir = normPath(expandUser('~/.configs'));
let StateFile = path.join(BaseDir, 'state.json');

const LinuxAutostartDir = expandUser('~/.config/autostart');
const LinuxSystemdUserDir = expandUser('~/.config/systemd/user');
const MacLaunchAgentsDir = expandUser('~/Library/LaunchAgents');
const TermuxBootDir = expandUser('~/.termux/boot');
const HyprDir = expandUser('~/.config/hypr');

const WIN_RUN_KEY = 'HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run';
const WIN_APPROVED_KEY =
  'HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\StartupApproved\\Run';

// Known script extensions, longest/most-specific first (sanitize strips them).
const KNOWN_EXTS = ['.pyw', '.py', '.mjs', '.cjs', '.js', '.csx', '.cs', '.ts', '.bash', '.sh'];

class ExitError extends Error {
  constructor(code) { super('exit ' + code); this.code = code; }
}
function die(msg, code = 1) { process.stderr.write(msg + '\n'); throw new ExitError(code); }
function log(msg) { process.stdout.write(msg + '\n'); }

// ================================================================
// Entry point
// ================================================================
function main(argv) {
  const { script, hide, hidden, stop } = parseArgs(argv);
  if (stop) uninstall(script);
  else install(script, hide || hidden, hidden);
}

function parseArgs(argv) {
  let hide = false, hidden = false, stop = false, script = null;
  for (const a of argv) {
    switch (a) {
      case '--hide': hide = true; break;
      case '--hidden': hidden = true; break;
      case '--stop': stop = true; break;
      case '-h':
      case '--help': printUsage(); throw new ExitError(0);
      default:
        if (a.startsWith('-')) { process.stderr.write(`[-] Unknown option: ${a}\n`); printUsage(); throw new ExitError(2); }
        if (script === null) script = a;
        else die(`[-] Unexpected extra argument: ${a}`, 2);
    }
  }
  if (!script) { process.stderr.write('[-] Missing SCRIPT argument.\n'); printUsage(); throw new ExitError(2); }
  return { script, hide, hidden, stop };
}

function printUsage() {
  log(`Usage: auto.js SCRIPT [--hide | --hidden] [--stop]

Install SCRIPT to run automatically on every reboot/login. The language is
detected from the file extension (.py .js .ts .cs .sh).

  --hide     run silently in the background (no window)
  --hidden   like --hide, and also hide the install folder (~/.configs)
  --stop     stop and permanently remove the autostart entry`);
}

// ================================================================
// Platform detection
// ================================================================
function isWindows() { return process.platform === 'win32'; }
function isMacHost() { return process.platform === 'darwin'; }
function isLinuxHost() { return process.platform === 'linux'; }

function isIOS() {
  if (!isMacHost()) return false;
  const home = process.env.HOME || '';
  return home.startsWith('/private/var/mobile') || home.startsWith('/var/mobile');
}
function isMacOS() { return isMacHost() && !isIOS(); }

function isTermux() {
  const prefix = process.env.PREFIX || '';
  if (prefix.startsWith('/data/data/com.termux')) return true;
  return process.env.TERMUX_VERSION != null;
}
function isAndroid() {
  if (!isLinuxHost()) return false;
  return process.env.ANDROID_ROOT != null || fs.existsSync('/system/build.prop') || isTermux();
}
function isLinux() { return isLinuxHost() && !isAndroid(); }
function isLinuxHeadless() {
  if (!isLinux()) return false;
  return !process.env.DISPLAY && !process.env.WAYLAND_DISPLAY;
}
function sysNameLower() {
  if (isWindows()) return 'windows';
  if (isMacHost()) return 'darwin';
  if (isAndroid()) return 'android';
  if (isLinuxHost()) return 'linux';
  return (process.platform || 'unknown').toLowerCase();
}
function release() {
  try { if (fs.existsSync('/proc/sys/kernel/osrelease')) return fs.readFileSync('/proc/sys/kernel/osrelease', 'utf8').trim(); }
  catch (e) { /* ignore */ }
  return os.release() || '';
}

// ================================================================
// Path / fs helpers
// ================================================================
function homeDir() {
  return process.env.HOME || process.env.USERPROFILE ||
    ((process.env.HOMEDRIVE && process.env.HOMEPATH) ? process.env.HOMEDRIVE + process.env.HOMEPATH : os.homedir());
}
function expandUser(p) {
  if (p === '~') return homeDir();
  if (p.startsWith('~/') || p.startsWith('~\\')) return path.join(homeDir(), p.slice(2));
  return p;
}
function normPath(p) { if (!p) return p; try { return path.resolve(p); } catch (e) { return p; } }
function baseName(p) {
  if (!p) return p || '';
  p = String(p).replace(/\\/g, '/');
  const i = p.lastIndexOf('/');
  return i < 0 ? p : p.slice(i + 1);
}
function extOf(p) {
  const b = baseName(p);
  const i = b.lastIndexOf('.');
  return i <= 0 ? '' : b.slice(i).toLowerCase();
}
function writeText(p, content) { fs.writeFileSync(p, content, { encoding: 'utf8' }); }
function chmodExec(p) { if (!isWindows()) { try { fs.chmodSync(p, 0o755); } catch (e) {} } }
function chmod644(p) { if (!isWindows()) { try { fs.chmodSync(p, 0o644); } catch (e) {} } }
function mkdirp(p) { try { fs.mkdirSync(p, { recursive: true }); } catch (e) {} }
function exists(p) { try { return fs.existsSync(p); } catch (e) { return false; } }
function isFile(p) { try { return fs.statSync(p).isFile(); } catch (e) { return false; } }
function tryRemoveFile(p, label) {
  if (!exists(p)) return;
  try { fs.unlinkSync(p); log(`[+] Removed ${label}: ${p}`); }
  catch (e) { log(`[-] Could not remove ${label} ${p}: ${e.message}`); }
}

// ================================================================
// Quoting helpers
// ================================================================
function shQuote(s) {
  if (s == null || s.length === 0) return "''";
  if (/^[A-Za-z0-9@%_+=:,./-]+$/.test(s)) return s;
  return "'" + s.replace(/'/g, "'\"'\"'") + "'";
}
function quoteExecArg(arg) {
  arg = String(arg)
    .replace(/\\/g, '\\\\\\\\') // 1 backslash -> 4
    .replace(/"/g, '\\\\"')
    .replace(/`/g, '\\\\`')
    .replace(/\$/g, '\\\\$')
    .replace(/%/g, '%%');
  return '"' + arg + '"';
}
function buildExecLine(cmd) { return cmd.map(quoteExecArg).join(' '); }
function systemdQuote(arg) {
  arg = String(arg || '').replace(/\\/g, '\\\\').replace(/"/g, '\\"').replace(/%/g, '%%');
  return '"' + arg + '"';
}
function luaQuote(s) {
  return '"' + String(s || '').replace(/\\/g, '\\\\').replace(/"/g, '\\"').replace(/\n/g, '\\n') + '"';
}
function joinSh(toks) { return toks.map(shQuote).join(' '); }
function runnerPlain(runner, script) { return runner.map(shQuote).join(' ') + ' ' + shQuote(script); }
function runnerExecLine(runner, script) { return 'exec ' + runnerPlain(runner, script); }
function escapeRegex(s) { return String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }

// ================================================================
// Process helpers
// ================================================================
function run(cmd, opts = {}) {
  const list = cmd.slice();
  if (list.length === 0) return { code: 127, out: '', err: 'empty command' };
  const spawnOpts = {
    encoding: 'utf8',
    env: opts.env ? Object.assign({}, process.env, opts.env) : process.env,
    timeout: opts.timeout ? opts.timeout * 1000 : undefined,
    maxBuffer: 64 * 1024 * 1024,
  };
  if (opts.input != null) spawnOpts.input = opts.input;
  const r = cp.spawnSync(list[0], list.slice(1), spawnOpts);
  if (r.error) {
    if (r.error.code === 'ENOENT') return { code: 127, out: '', err: `${list[0]}: not found` };
    return { code: 126, out: '', err: r.error.message };
  }
  return { code: r.status == null ? 124 : r.status, out: r.stdout || '', err: r.stderr || '' };
}

function trimmed(r) { if (!r) return ''; const e = (r.err || '').trim(); return e.length ? e : (r.out || '').trim(); }

function which(name) {
  if (!name) return null;
  const pathext = isWindows()
    ? (process.env.PATHEXT || '.EXE;.CMD;.BAT;.COM').split(';').filter(Boolean)
    : [];
  const hasExt = extOf(name).length > 0;
  if (name.includes('/') || name.includes('\\')) {
    if (isFile(name)) return name;
    if (isWindows() && !hasExt) for (const e of pathext) if (isFile(name + e)) return name + e;
    return null;
  }
  const dirs = (process.env.PATH || '').split(path.delimiter);
  for (const dir of dirs) {
    if (!dir) continue;
    let full;
    try { full = path.join(dir, name); } catch (e) { continue; }
    if (isFile(full)) return full;
    if (isWindows() && !hasExt) for (const e of pathext) { const c = full + e; if (isFile(c)) return c; }
  }
  return null;
}
function firstExe(...names) { for (const n of names) { const w = which(n); if (w) return w; } return null; }
function hasBinary(name) { return which(name) != null; }
function hasSystemd() { return hasBinary('systemctl') && exists('/run/systemd/system'); }

function getUid() {
  try { if (typeof process.getuid === 'function') return process.getuid(); } catch (e) {}
  const r = run(['id', '-u']);
  if (r.code === 0) { const v = parseInt((r.out || '').trim(), 10); if (!isNaN(v)) return v; }
  return 0;
}
function runningAsRootViaSudo() {
  if (isWindows()) return false;
  try { return typeof process.geteuid === 'function' && process.geteuid() === 0 && process.env.SUDO_USER != null; }
  catch (e) { return false; }
}
function unixKillPid(pid) {
  try { process.kill(pid, 'SIGTERM'); log(`[+] Killed PID ${pid}`); }
  catch (e) { if (e.code !== 'ESRCH') log(`[-] Could not kill PID ${pid}: ${e.message}`); }
}
function linuxUserEnv() {
  const d = {};
  if (!process.env.XDG_RUNTIME_DIR) d.XDG_RUNTIME_DIR = `/run/user/${getUid()}`;
  return d;
}
function unixSpawnDetached(shellCmd, cwd) {
  const full = cwd != null ? `cd ${shQuote(cwd)} && ${shellCmd}` : shellCmd;
  const setsid = which('setsid');
  try {
    let child;
    const o = { detached: true, stdio: 'ignore' };
    if (setsid) child = cp.spawn(setsid, ['sh', '-c', full], o);
    else child = cp.spawn('sh', ['-c', full], o);
    child.unref();
  } catch (e) { /* ignore */ }
}

// ================================================================
// State
// ================================================================
function loadState() {
  if (!exists(StateFile)) return {};
  try { const d = JSON.parse(fs.readFileSync(StateFile, 'utf8')); return (d && typeof d === 'object') ? d : {}; }
  catch (e) { return {}; }
}
function saveState(state) {
  mkdirp(BaseDir);
  const tmp = StateFile + '.tmp';
  fs.writeFileSync(tmp, JSON.stringify(state, null, 2), 'utf8');
  fs.renameSync(tmp, StateFile);
}
function sanitizeName(name) {
  let b = baseName(String(name || '').replace(/\\/g, '/'));
  const low = b.toLowerCase();
  for (const ext of KNOWN_EXTS) { if (low.endsWith(ext)) { b = b.slice(0, b.length - ext.length); break; } }
  b = b.replace(/[^a-zA-Z0-9\-_]/g, '-').toLowerCase();
  return b.length === 0 ? 'program' : b;
}

// ================================================================
// Language / runtime resolution
// ================================================================
function detectLanguage(scriptPath) {
  switch (extOf(scriptPath)) {
    case '.py': case '.pyw': return 'python';
    case '.js': case '.mjs': case '.cjs': return 'node';
    case '.ts': return 'typescript';
    case '.cs': case '.csx': return 'csharp';
    case '.sh': case '.bash': return 'shell';
    default: return 'unknown';
  }
}
function dotnetMajor(dotnet) {
  try {
    const r = run([dotnet, '--version'], { timeout: 20 });
    if (r.code !== 0) return 0;
    const m = /(\d+)/.exec(r.out || '');
    return m ? parseInt(m[1], 10) : 0;
  } catch (e) { return 0; }
}
// ================================================================
// Runtime auto-provisioning (per-user, NO admin / root, NO prompts)
//
// Every language is made to "just run".  We first use whatever the
// system already has; if it is missing, we quietly fetch an official
// portable runtime into BaseDir/runtime (the user's home dir — never
// a system path) and bake its absolute path into the launcher, so a
// reboot never depends on PATH.  If even that fails (e.g. no network)
// we fall back to the bare command name so the entry still works the
// moment a runtime appears.  Nothing here ever asks the user anything.
// ================================================================
function runtimeRoot() { const d = path.join(BaseDir, 'runtime'); mkdirp(d); return d; }
function hostArch() {
  switch (process.arch) {
    case 'x64': return 'x64';
    case 'arm64': return 'arm64';
    case 'ia32': return 'x86';
    default: return process.arch;
  }
}
let _provNote = false;
function provisionNote() {
  if (_provNote) return; _provNote = true;
  log('[*] Setting up a private runtime in your home folder (no admin needed)…');
}

// Download a URL to a file using the Node interpreter itself (follows
// redirects, no curl/wget dependency).  Returns true on success.
function downloadFile(url, dest) {
  const script =
    'const https=require("https"),http=require("http"),fs=require("fs"),{URL}=require("url");' +
    'function get(u,n){if(n>12){console.error("redirects");process.exit(3);}' +
    'const lib=u.indexOf("http:")===0?http:https;' +
    'lib.get(u,{headers:{"User-Agent":"auto-js"}},function(res){' +
    'if([301,302,303,307,308].indexOf(res.statusCode)>=0){res.resume();return get(new URL(res.headers.location,u).toString(),n+1);}' +
    'if(res.statusCode!==200){console.error("status "+res.statusCode);process.exit(4);}' +
    'const f=fs.createWriteStream(process.argv[2]);res.pipe(f);' +
    'f.on("finish",function(){f.close(function(){process.exit(0);});});' +
    'f.on("error",function(e){console.error(e.message);process.exit(5);});' +
    '}).on("error",function(e){console.error(e.message);process.exit(6);});}' +
    'get(process.argv[1],0);';
  const node = (process.execPath && isFile(process.execPath)) ? process.execPath : (firstExe('node', 'nodejs') || 'node');
  const r = run([node, '-e', script, url, dest], { timeout: 900 });
  try { return r.code === 0 && isFile(dest) && fs.statSync(dest).size > 0; } catch (e) { return false; }
}

// Extract .tar.gz / .tar.xz / .zip with the system `tar` (bsdtar on
// Windows/macOS reads zip too); zip falls back to PowerShell/unzip.
function extractArchive(archive, destDir, strip) {
  mkdirp(destDir);
  const lower = archive.toLowerCase();
  const args = ['-xf', archive, '-C', destDir];
  if (strip && strip > 0) args.push('--strip-components=' + strip);
  let r = run(['tar', ...args], { timeout: 900 });
  if (r.code === 0) return true;
  if (lower.endsWith('.zip')) {
    if (isWindows()) {
      r = run(['powershell', '-NoProfile', '-NonInteractive', '-Command',
        `Expand-Archive -Force -LiteralPath '${archive}' -DestinationPath '${destDir}'`], { timeout: 900 });
      if (r.code === 0) { if (strip) flattenOneLevel(destDir); return true; }
    }
    r = run(['unzip', '-o', archive, '-d', destDir], { timeout: 900 });
    if (r.code === 0) { if (strip) flattenOneLevel(destDir); return true; }
  }
  return false;
}
// Move the single top-level dir's contents up one level (zip strip).
function flattenOneLevel(destDir) {
  try {
    const items = fs.readdirSync(destDir);
    if (items.length !== 1) return;
    const inner = path.join(destDir, items[0]);
    if (!fs.statSync(inner).isDirectory()) return;
    for (const f of fs.readdirSync(inner)) fs.renameSync(path.join(inner, f), path.join(destDir, f));
    fs.rmdirSync(inner);
  } catch (e) {}
}

function nodeVersionOf(bin) {
  try { const r = run([bin, '-v'], { timeout: 20 }); const m = /v?(\d+)\.(\d+)/.exec(r.out || ''); if (m) return { maj: +m[1], min: +m[2] }; } catch (e) {}
  return null;
}
// Node >= 22.18 (and all of 23/24+) strips TypeScript types with no flag.
function nodeTsArgs(bin) {
  const v = nodeVersionOf(bin);
  if (v && (v.maj > 22 || (v.maj === 22 && v.min >= 18))) return [bin];
  return [bin, '--experimental-strip-types'];
}

// ---- Node ----
function ensureNode() {
  // The interpreter already running auto.js is a perfectly good Node.
  try { if (process.execPath && /node(\.exe)?$/i.test(process.execPath) && isFile(process.execPath)) return process.execPath; } catch (e) {}
  const sys = firstExe('node', 'nodejs'); if (sys) return sys;
  const dir = path.join(runtimeRoot(), 'node');
  const bin = isWindows() ? path.join(dir, 'node.exe') : path.join(dir, 'bin', 'node');
  if (isFile(bin)) return bin;
  provisionNote();
  if (downloadNode(dir) && isFile(bin)) return bin;
  return null;
}
function latestNodeLTS() {
  const idx = path.join(runtimeRoot(), 'node-index.json');
  if (!downloadFile('https://nodejs.org/dist/index.json', idx)) return null;
  try {
    const j = JSON.parse(fs.readFileSync(idx, 'utf8'));
    const lts = j.find((x) => x.lts); return lts ? lts.version : (j[0] && j[0].version) || null;
  } catch (e) { return null; }
}
function downloadNode(dir) {
  const v = latestNodeLTS(); if (!v) return false;
  const a = hostArch();
  let file;
  if (isWindows()) file = `node-${v}-win-${a === 'x86' ? 'x86' : 'x64'}.zip`;
  else if (isMacHost()) file = `node-${v}-darwin-${a}.tar.gz`;
  else file = `node-${v}-linux-${a}.tar.gz`;
  const url = `https://nodejs.org/dist/${v}/${file}`;
  const tmp = path.join(runtimeRoot(), file);
  if (!downloadFile(url, tmp)) return false;
  const ok = extractArchive(tmp, dir, 1);
  tryUnlink(tmp);
  return ok;
}

// ---- Python (python-build-standalone, install_only) ----
function ensurePython(hidden) {
  const sys = isWindows() ? firstExe('python', 'python3') : firstExe('python3', 'python');
  if (sys) return isWindows() ? (hidden ? windowsPythonw(sys) : windowsPython(sys)) : sys;
  const dir = path.join(runtimeRoot(), 'python');
  const bin = isWindows() ? path.join(dir, 'python.exe') : path.join(dir, 'bin', 'python3');
  if (isFile(bin)) return isWindows() && hidden ? windowsPythonw(bin) : bin;
  provisionNote();
  if (downloadPython() && isFile(bin)) return isWindows() && hidden ? windowsPythonw(bin) : bin;
  return null;
}
function pythonTriple() {
  const a = hostArch();
  if (isWindows()) return (a === 'arm64' ? 'aarch64' : 'x86_64') + '-pc-windows-msvc-shared';
  if (isMacHost()) return (a === 'arm64' ? 'aarch64' : 'x86_64') + '-apple-darwin';
  return (a === 'arm64' ? 'aarch64' : 'x86_64') + '-unknown-linux-gnu';
}
function downloadPython() {
  // Resolve the latest release asset list from GitHub.
  const meta = path.join(runtimeRoot(), 'pbs-latest.json');
  if (!downloadFile('https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest', meta)) return false;
  let assets;
  try { assets = (JSON.parse(fs.readFileSync(meta, 'utf8')).assets || []).map((x) => x.browser_download_url); } catch (e) { return false; }
  const triple = pythonTriple();
  // Prefer the newest cpython-3.x, install_only (no PGO/full suffix needed).
  const cands = assets.filter((u) => /cpython-3\./.test(u) && u.includes(triple) && u.endsWith('install_only.tar.gz'));
  cands.sort().reverse();
  if (!cands.length) return false;
  const url = cands[0];
  const tmp = path.join(runtimeRoot(), 'python.tar.gz');
  if (!downloadFile(url, tmp)) return false;
  // Archive top dir is "python/"; extract into runtimeRoot so we get runtime/python/...
  const ok = extractArchive(tmp, runtimeRoot(), 0);
  tryUnlink(tmp);
  return ok;
}

// ---- .NET (Microsoft dotnet-install, per-user) ----
function ensureDotnet() {
  const sys = which('dotnet');
  if (sys && dotnetMajor(sys) >= 10) return [sys, 'run'];
  const ds = which('dotnet-script'); if (ds) return [ds];
  const dir = path.join(runtimeRoot(), 'dotnet');
  const bin = isWindows() ? path.join(dir, 'dotnet.exe') : path.join(dir, 'dotnet');
  if (isFile(bin) && dotnetMajor(bin) >= 10) return [bin, 'run'];
  provisionNote();
  if (downloadDotnet(dir) && isFile(bin) && dotnetMajor(bin) >= 10) return [bin, 'run'];
  return null;
}
function downloadDotnet(dir) {
  mkdirp(dir);
  if (isWindows()) {
    const ps1 = path.join(runtimeRoot(), 'dotnet-install.ps1');
    if (!downloadFile('https://dot.net/v1/dotnet-install.ps1', ps1)) return false;
    const r = run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ps1,
      '-Channel', '10.0', '-InstallDir', dir, '-NoPath'], { timeout: 1800 });
    tryUnlink(ps1);
    return r.code === 0;
  }
  const sh = path.join(runtimeRoot(), 'dotnet-install.sh');
  if (!downloadFile('https://dot.net/v1/dotnet-install.sh', sh)) return false;
  chmodExec(sh);
  const bash = firstExe('bash', 'sh') || '/bin/sh';
  const r = run([bash, sh, '--channel', '10.0', '--install-dir', dir, '--no-path'], { timeout: 1800 });
  tryUnlink(sh);
  return r.code === 0;
}

function tryUnlink(p) { try { fs.unlinkSync(p); } catch (e) {} }

// Resolve a runner for a script, provisioning its runtime if needed.
// Never prompts; never dies.  Returns an absolute-path argv, or a bare
// fallback command so the launcher still works if a runtime shows up.
function resolveRunner(scriptPath, hidden) {
  switch (detectLanguage(scriptPath)) {
    case 'python': { const p = ensurePython(hidden); return [p || (isWindows() ? (hidden ? 'pythonw' : 'python') : 'python3')]; }
    case 'node': { const n = ensureNode(); return [n || 'node']; }
    case 'typescript': { const n = ensureNode(); return n ? nodeTsArgs(n) : ['node']; }
    case 'csharp': { return ensureDotnet() || ['dotnet', 'run']; }
    case 'shell': {
      if (isWindows()) { const b = which('bash'); return [b || 'bash']; }
      const sh = firstExe('bash', 'sh'); return [sh || '/bin/sh'];
    }
    default: return ['sh'];
  }
}
function requireRunner(scriptPath, hidden) { return resolveRunner(scriptPath, hidden); }

// ================================================================
// Shared fs setup
// ================================================================
function ensureDirectory() {
  mkdirp(BaseDir);
  if (isLinux()) { mkdirp(LinuxAutostartDir); mkdirp(LinuxSystemdUserDir); }
  else if (isMacOS()) mkdirp(MacLaunchAgentsDir);
}

function hideInstallDir() {
  const p = BaseDir;
  if (!exists(p)) mkdirp(p);
  if (isWindows()) {
    const r = run(['attrib', '+h', p]);
    if (r.code === 0) log(`[+] Hid the install folder (hidden attribute): ${p}`);
    else log('[!] Could not set the hidden attribute on the install folder.');
    return;
  }
  const trimmedP = p.replace(/[\/]+$/, '');
  const parent = path.dirname(trimmedP);
  const base = baseName(trimmedP);
  if (base.startsWith('.')) { log(`[+] Install folder already starts with a dot (hidden): ${p}`); return; }
  const hiddenPath = path.join(parent, '.' + base);
  if (exists(hiddenPath)) {
    log(`[!] Cannot rename '${p}' to '${hiddenPath}': target exists.`);
    log('    Leaving the folder visible so nothing is overwritten.');
    return;
  }
  try { fs.renameSync(p, hiddenPath); }
  catch (e) { log(`[!] Could not rename '${p}' to hide it: ${e.message}`); return; }
  BaseDir = hiddenPath;
  StateFile = path.join(BaseDir, 'state.json');
  log(`[+] Hid the install folder by renaming: ${p} -> ${hiddenPath}`);
}

// ================================================================
// Linux — launcher + XDG autostart
// ================================================================
function linuxDesktopPath(name) { return path.join(LinuxAutostartDir, `${name}.desktop`); }
function linuxSystemdUnitPath(name) { return path.join(LinuxSystemdUserDir, `${name}.service`); }
function linuxLauncherPath(name) { return path.join(BaseDir, `run-${name}.sh`); }

// Quiet, self-contained env for a provisioned .NET runner so hidden
// runs don't print the first-run banner and have a writable home.
function dotnetEnvLines(runner, forShell) {
  const b = (runner && runner[0]) || '';
  if (!/dotnet(\.exe)?$/i.test(b)) return [];
  const root = path.dirname(b);
  if (forShell) return [
    'export DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1 DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1',
    `export DOTNET_ROOT=${shQuote(root)}`,
  ];
  return [
    ['DOTNET_CLI_TELEMETRY_OPTOUT', '1'], ['DOTNET_NOLOGO', '1'],
    ['DOTNET_SKIP_FIRST_TIME_EXPERIENCE', '1'], ['DOTNET_ROOT', root],
  ];
}
function linuxWriteLauncher(copyPath, copyDir, name, runner) {
  const p = linuxLauncherPath(name);
  let s = '#!/bin/sh\n';
  s += `cd ${shQuote(copyDir)} || exit 1\n`;
  for (const line of dotnetEnvLines(runner, true)) s += line + '\n';
  s += 'exec';
  for (const t of runner) s += ' ' + shQuote(t);
  s += ' ' + shQuote(copyPath) + ' "$@"\n';
  writeText(p, s); chmodExec(p);
  return p;
}
function linuxRemoveLauncher(name) {
  const p = linuxLauncherPath(name);
  if (exists(p)) { try { fs.unlinkSync(p); log(`[+] Removed launcher script: ${p}`); } catch (e) { log(`[-] Could not remove launcher ${p}: ${e.message}`); } }
}

function linuxCreateAutostart(copyPath, copyDir, name, hidden, runner) {
  const launcher = linuxWriteLauncher(copyPath, copyDir, name, runner);
  let execLine;
  const terminal = 'false';
  if (hidden) execLine = buildExecLine([launcher]);
  else {
    const cmd = findLinuxTerminal(launcher);
    if (cmd) execLine = buildExecLine(cmd);
    else { execLine = buildExecLine([launcher]); log('[!] No terminal emulator found for autostart entry; the program will run without a visible window.'); }
  }
  const content =
    '[Desktop Entry]\n' +
    'Type=Application\n' +
    `Name=${name}\n` +
    `Exec=${execLine}\n` +
    `Path=${copyDir}\n` +
    `Terminal=${terminal}\n` +
    'X-GNOME-Autostart-enabled=true\n' +
    'Hidden=false\n';
  const p = linuxDesktopPath(name);
  writeText(p, content); chmodExec(p);
  log(`[+] Linux autostart entry: ${p}  (Terminal=${terminal})`);
}
function linuxRemoveAutostart(name) {
  const p = linuxDesktopPath(name);
  if (exists(p)) { fs.unlinkSync(p); log(`[+] Removed Linux autostart entry: ${p}`); }
}

function findLinuxTerminal(launcher) {
  const candidates = [
    ['xdg-terminal-exec', launcher],
    ['ptyxis', '--', launcher],
    ['kgx', '--', launcher],
    ['gnome-terminal', '--', launcher],
    ['konsole', '-e', launcher],
    ['xfce4-terminal', '-x', launcher],
    ['mate-terminal', '-x', launcher],
    ['cosmic-term', '-e', launcher],
    ['alacritty', '-e', launcher],
    ['kitty', launcher],
    ['ghostty', '-e', launcher],
    ['foot', launcher],
    ['wezterm', 'start', '--', launcher],
    ['x-terminal-emulator', '-e', launcher],
    ['xterm', '-e', launcher],
  ];
  for (const cmd of candidates) if (which(cmd[0])) return cmd.slice();
  return null;
}
function isHyprland() {
  if (process.env.HYPRLAND_INSTANCE_SIGNATURE) return true;
  const cur = ((process.env.XDG_CURRENT_DESKTOP || '') + ' ' + (process.env.XDG_SESSION_DESKTOP || '') + ' ' + (process.env.DESKTOP_SESSION || '')).toLowerCase();
  return cur.includes('hyprland') || cur.includes('omarchy');
}
function isWlrootsWayland() {
  if (!process.env.WAYLAND_DISPLAY) return false;
  const cur = ((process.env.XDG_CURRENT_DESKTOP || '') + ' ' + (process.env.XDG_SESSION_DESKTOP || '') + ' ' + (process.env.DESKTOP_SESSION || '')).toLowerCase();
  if (['gnome', 'kde', 'plasma'].some(de => cur.includes(de))) return false;
  return ['sway', 'river', 'wayfire', 'niri', 'hyprland', 'labwc', 'wlroots'].some(wm => cur.includes(wm));
}

// ================================================================
// Linux — Hyprland / Omarchy
// ================================================================
function hyprMarker(name, comment) { return [`${comment} VisibleBackground ${name} BEGIN`, `${comment} VisibleBackground ${name} END`]; }
function hyprTarget() {
  const luaMain = path.join(HyprDir, 'hyprland.lua');
  const luaAuto = path.join(HyprDir, 'autostart.lua');
  const confMain = path.join(HyprDir, 'hyprland.conf');
  const confAuto = path.join(HyprDir, 'autostart.conf');
  const mentions = (p, needle) => { try { return exists(p) && fs.readFileSync(p, 'utf8').includes(needle); } catch (e) { return false; } };
  if (exists(luaMain)) {
    if (exists(luaAuto) && (mentions(luaMain, 'autostart') || mentions(luaAuto, 'o.launch') || exists('/usr/share/omarchy') || exists(expandUser('~/.local/share/omarchy'))))
      return [luaAuto, 'omarchy-lua'];
    return [luaMain, 'lua'];
  }
  if (exists(confMain)) {
    if (exists(confAuto) && mentions(confMain, 'autostart.conf')) return [confAuto, 'conf'];
    return [confMain, 'conf'];
  }
  return [null, null];
}
function stripBlock(text, begin, end) {
  const out = []; let skip = false;
  for (const ln of String(text || '').split('\n')) {
    const t = ln.trim();
    if (t === begin) { skip = true; continue; }
    if (t === end) { skip = false; continue; }
    if (!skip) out.push(ln);
  }
  return out.join('\n');
}
function hyprlandCreateAutostart(copyPath, copyDir, name, hidden, runner) {
  const [p, kind] = hyprTarget();
  if (!p) { log('[!] Hyprland detected but no ~/.config/hypr/hyprland.(lua|conf) found.'); return false; }
  const launcher = linuxWriteLauncher(copyPath, copyDir, name, runner);
  const argv = hidden ? [launcher] : (findLinuxTerminal(launcher) || [launcher]);
  const cmd = joinSh(argv);
  const comment = (kind === 'omarchy-lua' || kind === 'lua') ? '--' : '#';
  const [begin, end] = hyprMarker(name, comment);
  let body;
  if (kind === 'omarchy-lua') body = `o.launch_on_start(${luaQuote(cmd)})`;
  else if (kind === 'lua') body = `hl.on("hyprland.start", function() hl.exec_cmd(${luaQuote(cmd)}) end)`;
  else body = `exec-once = ${cmd}`;
  let text; try { text = fs.readFileSync(p, 'utf8'); } catch (e) { text = ''; }
  text = stripBlock(text, begin, end).replace(/\n+$/, '');
  text += `\n\n${begin}\n${body}\n${end}\n`;
  const tmp = p + '.vbtmp';
  writeText(tmp, text); fs.renameSync(tmp, p);
  log(`[+] Hyprland autostart added to ${p} (${kind}).`);
  return true;
}
function hyprlandRemoveAutostart(name) {
  const files = [['autostart.lua', '--'], ['hyprland.lua', '--'], ['autostart.conf', '#'], ['hyprland.conf', '#']];
  for (const [fname, comment] of files) {
    const p = path.join(HyprDir, fname);
    if (!exists(p)) continue;
    const [begin, end] = hyprMarker(name, comment);
    let text; try { text = fs.readFileSync(p, 'utf8'); } catch (e) { continue; }
    if (!text.includes(begin)) continue;
    const updated = stripBlock(text, begin, end).replace(/\n+$/, '') + '\n';
    const tmp = p + '.vbtmp';
    writeText(tmp, updated); fs.renameSync(tmp, p);
    log(`[+] Removed Hyprland autostart entry from ${p}`);
  }
}

// ================================================================
// Linux — launch / find / kill
// ================================================================
function linuxLaunchNow(copyPath, copyDir, name, hidden, runner) {
  if (isLinuxHeadless()) {
    if (hasSystemd() && exists(linuxSystemdUnitPath(name))) {
      const res = run(['systemctl', '--user', 'start', `${name}.service`], { env: linuxUserEnv() });
      if (res.code === 0) { log('[+] Started now via systemd user service (headless/background).'); return; }
      log(`[!] systemctl --user start failed: ${trimmed(res)}`);
    }
    unixSpawnDetached(runnerExecLine(runner, copyPath) + ' >/dev/null 2>&1', copyDir);
    log('[+] Started now detached (headless fallback).');
    return;
  }
  if (hidden) {
    unixSpawnDetached(runnerExecLine(runner, copyPath) + ' >/dev/null 2>&1', copyDir);
    log('[+] Started now (hidden/background).');
  } else {
    let launcher = linuxLauncherPath(name);
    if (!exists(launcher)) launcher = linuxWriteLauncher(copyPath, copyDir, name, runner);
    const cmd = findLinuxTerminal(launcher);
    if (cmd) { unixSpawnDetached(joinSh(cmd) + ' >/dev/null 2>&1', null); log(`[+] Started now in a visible terminal (${cmd[0]}).`); }
    else { unixSpawnDetached(runnerExecLine(runner, copyPath), copyDir); log('[!] No terminal emulator found — launched detached.'); }
  }
}

function unixFindPids(copyPath) {
  const pids = [];
  const r = run(['pgrep', '-f', escapeRegex(copyPath)]);
  if (r.code === 0) {
    for (const x of (r.out || '').split(/\s+/)) { const pid = parseInt(x, 10); if (!isNaN(pid) && pid !== process.pid) pids.push(pid); }
    return pids;
  }
  try {
    if (exists('/proc')) {
      for (const d of fs.readdirSync('/proc')) {
        if (!/^\d+$/.test(d)) continue;
        try {
          const raw = fs.readFileSync(`/proc/${d}/cmdline`, 'utf8').replace(/\0/g, ' ');
          const pid = parseInt(d, 10);
          if (raw.includes(copyPath) && pid !== process.pid) pids.push(pid);
        } catch (e) { /* ignore */ }
      }
    }
  } catch (e) { /* ignore */ }
  return pids;
}
function linuxKill(copyPath, name) {
  if (name && hasSystemd()) run(['systemctl', '--user', 'stop', `${name}.service`], { env: linuxUserEnv() });
  for (const pid of unixFindPids(copyPath)) unixKillPid(pid);
}

// ================================================================
// Linux — systemd user service
// ================================================================
function linuxCreateSystemdService(copyPath, copyDir, name, hidden, runner) {
  checkUbuntu2404Systemd255();
  mkdirp(LinuxSystemdUserDir);
  const env = linuxUserEnv();
  if (run(['systemctl', '--user', 'show-environment'], { env }).code !== 0) {
    log('[!] systemd user manager not reachable from this shell.');
    return false;
  }
  if (!linuxEnableLinger()) return false;
  const unitPath = linuxSystemdUnitPath(name);
  const parts = runner.map(systemdQuote); parts.push(systemdQuote(copyPath));
  const execStart = parts.join(' ');
  const workingDir = systemdQuote(copyDir);
  const content =
    '[Unit]\n' +
    `Description=VisibleBackground ${name}\n\n` +
    '[Service]\n' +
    'Type=simple\n' +
    `WorkingDirectory=${workingDir}\n` +
    `ExecStart=${execStart}\n` +
    'Restart=on-failure\n' +
    'RestartSec=5\n' +
    'StandardOutput=null\n' +
    'StandardError=null\n\n' +
    '[Install]\n' +
    'WantedBy=default.target\n';
  writeText(unitPath, content); chmod644(unitPath);
  log(`[+] systemd user unit written: ${unitPath}`);
  run(['systemctl', '--user', 'daemon-reload'], { env });
  const res = run(['systemctl', '--user', 'enable', `${name}.service`], { env });
  if (res.code !== 0) { log(`[!] systemctl --user enable failed: ${trimmed(res)}`); return false; }
  log(`[+] systemd user service enabled: ${name}.service`);
  return true;
}
function checkUbuntu2404Systemd255() {
  try {
    const osr = '/etc/os-release';
    if (!exists(osr)) return;
    const info = {};
    for (const raw of fs.readFileSync(osr, 'utf8').split('\n')) {
      const line = raw.trim(); const eq = line.indexOf('=');
      if (eq > 0) info[line.slice(0, eq)] = line.slice(eq + 1).replace(/^"|"$/g, '');
    }
    if (info.ID !== 'ubuntu') return;
    if (!info.VERSION_ID || !info.VERSION_ID.startsWith('24.04')) return;
    const res = run(['systemctl', '--version']);
    if (res.code !== 0 || !res.out) return;
    const m = /systemd\s+(\d+)/.exec(res.out);
    if (!m || m[1] !== '255') return;
    log('');
    log('[!] Ubuntu 24.04 with systemd 255 detected.');
    log('    A known logind regression (Launchpad bug #2163239,');
    log('    systemd issue #43361) may kill the user service manager');
    log('    (user@<uid>.service) on the last logout even when Linger=yes.');
    log('    This can prevent the service from surviving logout or reboot.');
    log('    Verify with:');
    log('      loginctl show-user "$USER" -p Linger');
    log('    If the service disappears after logout, apply the documented');
    log('    workaround in /etc/systemd/logind.conf (requires root):');
    log('      UserStopDelaySec=infinity');
    log('      KillUserProcesses=no');
    log('    Then restart logind:');
    log('      sudo systemctl restart systemd-logind');
    log('    Do not edit /etc/systemd/logind.conf automatically.');
    log('');
  } catch (e) { /* ignore */ }
}
function linuxEnableLinger() {
  const env = linuxUserEnv();
  const user = process.env.USER || process.env.LOGNAME || os.userInfo().username || String(getUid());
  if (exists(`/var/lib/systemd/linger/${user}`)) return true;
  const chk = run(['loginctl', 'show-user', user, '-p', 'Linger', '--value'], { env });
  if (chk.code === 0 && (chk.out || '').trim() === 'yes') return true;
  let res = run(['loginctl', 'enable-linger'], { env, input: '' });
  if (res.code === 0) { log('[+] loginctl enable-linger succeeded (no root needed).'); return true; }
  res = run(['sudo', '-n', 'loginctl', 'enable-linger', user], { env });
  if (res.code === 0) { log(`[+] loginctl enable-linger ${user} succeeded via sudo -n.`); return true; }
  log('[!] Could not enable linger (site policy forbids it); falling back');
  log('    to cron @reboot, which needs no root.');
  return false;
}
function linuxRemoveSystemdService(name) {
  const unitPath = linuxSystemdUnitPath(name);
  if (hasSystemd()) {
    const env = linuxUserEnv();
    run(['systemctl', '--user', 'stop', `${name}.service`], { env });
    run(['systemctl', '--user', 'disable', `${name}.service`], { env });
  }
  if (exists(unitPath)) { fs.unlinkSync(unitPath); log(`[+] Removed systemd user unit: ${unitPath}`); }
  if (hasSystemd()) run(['systemctl', '--user', 'daemon-reload'], { env: linuxUserEnv() });
}

// ================================================================
// Linux — cron @reboot fallback
// ================================================================
function cronMarker(name) { return `# VisibleBackground ${name}`; }
function linuxCreateCronAutostart(copyPath, copyDir, name, runner) {
  if (!hasBinary('crontab')) {
    log('[!] No systemd and no crontab found on this host.');
    log('    Cannot register a boot entry automatically.  To start the');
    log('    program at boot, add an init/rc service for your system that');
    log('    runs:');
    log(`      cd ${shQuote(copyDir)} && ${runnerPlain(runner, copyPath)}`);
    return false;
  }
  const marker = cronMarker(name);
  const line = `@reboot cd ${shQuote(copyDir)} && ${runnerPlain(runner, copyPath)} > /dev/null 2>&1  ${marker}`;
  const existing = run(['crontab', '-l']);
  let lines = [];
  if (existing.code === 0 && existing.out) for (const ln of existing.out.split('\n')) if (!ln.includes(marker)) lines.push(ln);
  while (lines.length && lines[lines.length - 1] === '') lines.pop();
  lines.push(line);
  const newCron = lines.join('\n') + '\n';
  const res = run(['crontab', '-'], { input: newCron });
  if (res.code === 0) {
    log(`[+] cron @reboot entry installed for '${name}'.`);
    if (!exists('/run/systemd/system')) {
      log('[!] Make sure the cron daemon is enabled and started at boot');
      log('    (e.g. `rc-update add cron` + `rc-service cron start` on');
      log('    OpenRC, or `sv enable cron` on runit).');
    }
    return true;
  }
  log(`[!] Could not write crontab: ${trimmed(res)}`);
  return false;
}
function linuxRemoveCronAutostart(name) {
  if (!hasBinary('crontab')) return;
  const marker = cronMarker(name);
  const existing = run(['crontab', '-l']);
  if (existing.code !== 0 || !existing.out) return;
  const all = existing.out.split('\n');
  const kept = all.filter(ln => !ln.includes(marker));
  if (kept.length === all.length) return;
  while (kept.length && kept[kept.length - 1] === '') kept.pop();
  const newCron = kept.length ? kept.join('\n') + '\n' : '';
  const res = run(['crontab', '-'], { input: newCron });
  if (res.code === 0) log(`[+] Removed cron @reboot entry for '${name}'.`);
}

// ================================================================
// macOS — LaunchAgent
// ================================================================
function macPlistPath(name) { return path.join(MacLaunchAgentsDir, `com.visiblebackground.${name}.plist`); }
function macCommandPath(name) { return path.join(BaseDir, `run-${name}.command`); }
function macWriteCommand(copyPath, copyDir, name, runner) {
  const p = macCommandPath(name);
  let content = '#!/bin/sh\n' + `cd ${shQuote(copyDir)} || exit 1\n`;
  for (const line of dotnetEnvLines(runner, true)) content += line + '\n';
  content += runnerExecLine(runner, copyPath) + '\n';
  writeText(p, content); chmodExec(p);
  return p;
}
function xmlEscape(s) { return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
function macWritePlist(plistPath, label, programArgs, workingDir, hidden, outLog, errLog, env) {
  let s = '<?xml version="1.0" encoding="UTF-8"?>\n';
  s += '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n';
  s += '<plist version="1.0">\n<dict>\n';
  s += `  <key>Label</key>\n  <string>${xmlEscape(label)}</string>\n`;
  s += '  <key>ProgramArguments</key>\n  <array>\n';
  for (const a of programArgs) s += `    <string>${xmlEscape(a)}</string>\n`;
  s += '  </array>\n';
  if (env && env.length) {
    s += '  <key>EnvironmentVariables</key>\n  <dict>\n';
    for (const [k, v] of env) s += `    <key>${xmlEscape(k)}</key>\n    <string>${xmlEscape(v)}</string>\n`;
    s += '  </dict>\n';
  }
  s += `  <key>WorkingDirectory</key>\n  <string>${xmlEscape(workingDir)}</string>\n`;
  s += '  <key>RunAtLoad</key>\n  <true/>\n';
  s += '  <key>KeepAlive</key>\n  <false/>\n';
  s += `  <key>ProcessType</key>\n  <string>${hidden ? 'Background' : 'Interactive'}</string>\n`;
  s += `  <key>StandardOutPath</key>\n  <string>${xmlEscape(outLog)}</string>\n`;
  s += `  <key>StandardErrorPath</key>\n  <string>${xmlEscape(errLog)}</string>\n`;
  s += '</dict>\n</plist>\n';
  const tmp = plistPath + '.tmp';
  writeText(tmp, s); chmod644(tmp); fs.renameSync(tmp, plistPath);
}
function macosCreateAutostart(copyPath, copyDir, name, hidden, runner) {
  const label = `com.visiblebackground.${name}`;
  let programArgs;
  if (hidden) programArgs = runner.concat([copyPath]);
  else programArgs = ['/usr/bin/open', '-a', 'Terminal', macWriteCommand(copyPath, copyDir, name, runner)];
  mkdirp(BaseDir); mkdirp(MacLaunchAgentsDir);
  const plistPath = macPlistPath(name);
  macWritePlist(plistPath, label, programArgs, copyDir, hidden, path.join(BaseDir, `${name}.out.log`), path.join(BaseDir, `${name}.err.log`), dotnetEnvLines(runner, false));
  log(`[+] macOS LaunchAgent written: ${plistPath}`);
  const uid = getUid();
  const domain = `gui/${uid}`;
  run(['launchctl', 'bootout', `${domain}/${label}`]);
  run(['launchctl', 'enable', `${domain}/${label}`]);
  let res = null;
  for (let i = 0; i < 3; i++) {
    res = run(['launchctl', 'bootstrap', domain, plistPath]);
    if (res.code === 0) {
      log('[+] LaunchAgent loaded (it will also start at every login).');
      log("[!] macOS may show a 'Background Items Added' notice; that is");
      log('    expected.  Keep it ON in System Settings > General > Login Items.');
      return true;
    }
    sleepMs(500);
  }
  log(`[!] launchctl bootstrap failed: ${trimmed(res)}; trying legacy launchctl load -w`);
  res = run(['launchctl', 'load', '-w', plistPath]);
  const err = (res.err || '').toLowerCase();
  if (res.code === 0 && !err.includes('failed') && !err.includes('error')) return true;
  log('[!] Could not load the LaunchAgent now (common over SSH, where no');
  log('    GUI session exists).  It will still load at your next login.');
  return false;
}
function macosRemoveAutostart(name) {
  const plistPath = macPlistPath(name);
  const uid = getUid();
  const label = `com.visiblebackground.${name}`;
  const res = run(['launchctl', 'bootout', `gui/${uid}/${label}`]);
  if (res.code !== 0 && exists(plistPath)) run(['launchctl', 'unload', '-w', plistPath]);
  if (exists(plistPath)) { fs.unlinkSync(plistPath); log(`[+] Removed macOS LaunchAgent: ${plistPath}`); }
  const cmd = macCommandPath(name);
  if (exists(cmd)) { try { fs.unlinkSync(cmd); } catch (e) {} }
  for (const suffix of ['.out.log', '.err.log']) { const lg = path.join(BaseDir, `${name}${suffix}`); if (exists(lg)) { try { fs.unlinkSync(lg); } catch (e) {} } }
}
function macosLaunchNow(copyPath, copyDir, name, hidden, runner) {
  if (hidden) { unixSpawnDetached(runnerExecLine(runner, copyPath) + ' >/dev/null 2>&1', copyDir); log('[+] Started now (hidden/background).'); }
  else { const cmd = macWriteCommand(copyPath, copyDir, name, runner); run(['/usr/bin/open', '-a', 'Terminal', cmd]); log('[+] Started now in Terminal (visible).'); }
}
function macosKill(copyPath) { for (const pid of unixFindPids(copyPath)) unixKillPid(pid); }

// ================================================================
// Android — Termux:Boot
// ================================================================
function androidSdk() { const r = run(['getprop', 'ro.build.version.sdk']); const v = parseInt((r.out || '').trim(), 10); return isNaN(v) ? 0 : v; }
let _androidWarned = false;
function android15Warning() {
  if (_androidWarned || androidSdk() < 35) return;
  _androidWarned = true;
  log('');
  log('[!] Android 15+ known issue:');
  log('    Android 15 and newer may still kill Termux background processes even with');
  log('    termux-wake-lock active and battery optimisation disabled.  This is');
  log('    an upstream Android limitation, tracked in Termux issue #5150.');
  log('    If your device exposes it, enable:');
  log('      Settings → System → Developer options →');
  log('      Disable child process restrictions');
  log('    Do not expect the same background reliability as on Android 14 or older.');
  log('');
}
function androidCreateAutostart(copyPath, copyDir, name, hidden, runner) {
  if (!isTermux()) {
    log('[-] Android support requires Termux.');
    log('    Install Termux from F-Droid, then install Termux:Boot,');
    log('    and run this script inside Termux.');
    throw new ExitError(1);
  }
  android15Warning();
  mkdirp(TermuxBootDir);
  const bootScript = path.join(TermuxBootDir, `start-${name}.sh`);
  let content;
  if (hidden) {
    const inner = runnerPlain(runner, copyPath);
    content = '#!/data/data/com.termux/files/usr/bin/sh\n' + 'termux-wake-lock\n' + `cd ${shQuote(copyDir)} || exit 1\n` + `nohup ${inner} > /dev/null 2>&1 &\n`;
  } else {
    const pathArg = runner[0];
    const argList = runner.slice(1).concat([copyPath]);
    const esaArg = argList.map(a => a.replace(/,/g, '\\,')).join(',');
    const extras =
      '-n com.termux/com.termux.app.RunCommandService ' +
      '-a com.termux.RUN_COMMAND ' +
      `--es com.termux.RUN_COMMAND_PATH ${shQuote(pathArg)} ` +
      `--esa com.termux.RUN_COMMAND_ARGUMENTS ${shQuote(esaArg)} ` +
      `--es com.termux.RUN_COMMAND_WORKDIR ${shQuote(copyDir)} ` +
      '--ez com.termux.RUN_COMMAND_BACKGROUND false';
    content = '#!/data/data/com.termux/files/usr/bin/sh\n' + 'termux-wake-lock\n' + 'sleep 5\n' +
      `am startservice --user 0 ${extras} >/dev/null 2>&1 || \\\n` +
      `am start-foreground-service --user 0 ${extras} >/dev/null 2>&1 || \\\n` +
      `( cd ${shQuote(copyDir)} && ${runnerPlain(runner, copyPath)} >/dev/null 2>&1 & )\n`;
  }
  writeText(bootScript, content); chmodExec(bootScript);
  log(`[+] Android Termux boot script written: ${bootScript}`);
  log('[!] Make sure Termux:Boot is installed and has been opened once.');
  log('[!] Disable battery optimisation for Termux and Termux:Boot.');
  log("[!] Grant Termux the 'Draw over other apps' permission on Android 10+");
  if (!hidden) {
    log('[!] Visible mode uses the Termux RUN_COMMAND API, which needs');
    log('    `allow-external-apps=true` in ~/.termux/termux.properties.');
    log('    If the window does not open at boot, use --hide instead.');
  }
}
function androidRemoveAutostart(name) {
  const bootScript = path.join(TermuxBootDir, `start-${name}.sh`);
  if (exists(bootScript)) { fs.unlinkSync(bootScript); log(`[+] Removed Termux boot script: ${bootScript}`); }
}
function androidLaunchNow(copyPath, copyDir, hidden, runner) {
  if (!isTermux()) { log('[-] Cannot launch now: not running inside Termux.'); return; }
  android15Warning();
  if (hidden) { unixSpawnDetached(runnerExecLine(runner, copyPath) + ' >/dev/null 2>&1', copyDir); log('[+] Started now (hidden/background).'); return; }
  const pathArg = runner[0];
  const argList = runner.slice(1).concat([copyPath]);
  const esaArg = argList.map(a => a.replace(/,/g, '\\,')).join(',');
  const extras = [
    '-n', 'com.termux/com.termux.app.RunCommandService',
    '-a', 'com.termux.RUN_COMMAND',
    '--es', 'com.termux.RUN_COMMAND_PATH', pathArg,
    '--esa', 'com.termux.RUN_COMMAND_ARGUMENTS', esaArg,
    '--es', 'com.termux.RUN_COMMAND_WORKDIR', copyDir,
    '--ez', 'com.termux.RUN_COMMAND_BACKGROUND', 'false',
  ];
  let ok = false;
  for (const verb of ['startservice', 'start-foreground-service']) {
    const cmd = ['am', verb, '--user', '0'].concat(extras);
    const res = run(cmd, { timeout: 20 });
    const outp = ((res.out || '') + (res.err || '')).toLowerCase();
    if (res.code === 0 && !outp.includes('error') && !outp.includes('exception')) { ok = true; break; }
  }
  if (ok) log('[+] Started now in a new visible Termux session.');
  else {
    unixSpawnDetached(runnerExecLine(runner, copyPath) + ' >/dev/null 2>&1', copyDir);
    log('[!] Could not open a Termux session (is allow-external-apps=true');
    log('    set in ~/.termux/termux.properties?); started in background.');
  }
}
function androidKill(copyPath) { for (const pid of unixFindPids(copyPath)) unixKillPid(pid); }

// ================================================================
// iOS — unsupported
// ================================================================
function iosUnsupported() {
  log('[-] iOS does not allow arbitrary background scripts to run persistently.');
  log('');
  log("    Apple's own documentation establishes the following platform contract:");
  log('');
  log('    • iOS suspends third-party apps shortly after they move to the background.');
  log('      Suspension prevents the process from executing any code.');
  log('    • There is no general-purpose mechanism for running code continuously,');
  log('      at a specific time, at a guaranteed interval, or in response to a');
  log('      network/IPC request in the background.');
  log('    • BGAppRefreshTask is opportunistic and capped at roughly 30 seconds.');
  log('      BGProcessingTask is also opportunistic and typically runs only when');
  log('      the device is idle and plugged in, for about 60 seconds.');
  log('    • Scheduling is decided by dasd (Duet Activity Scheduler Daemon), not');
  log('      by the app. Low battery, Low Power Mode, thermal pressure, and low');
  log('      engagement can all cause a submitted task to be dropped entirely.');
  log('    • Once the user force-quits an app, iOS will not restart it in the');
  log('      background. No third-party library can bypass this.');
  log('    • Jailbroken or MDM-supervised devices do not change this contract for');
  log('      ordinary user scripts.');
  log('');
  log('    The only Apple-sanctioned best-effort alternative is a user-configured');
  log('    Shortcuts automation triggered by "Device Unlocked" or "Wi-Fi connected"');
  log('    after boot.  This tool cannot install that automation for you.');
  throw new ExitError(1);
}

// ================================================================
// Windows — Run key + WSH launcher (no admin)
// ================================================================
function windowsTaskName(name) { return `VisibleBackground_${name}`; }
function windowsLauncherPath(name) { return normPath(path.join(BaseDir, `_vb_launch_${name}.js`)); }
function windowsLegacyPywPath(name) { return normPath(path.join(BaseDir, `_vb_launch_${name}.pyw`)); }
function windowsVbsPath(name) { return normPath(path.join(BaseDir, `VisibleBackground_${name}.vbs`)); }
function windowsCmdWrapperPath(name) { return normPath(path.join(BaseDir, `VisibleBackground_${name}_launcher.cmd`)); }

function windowsPythonw(py) {
  const d = path.dirname(py); const b = path.basename(py); const low = b.toLowerCase();
  if (low.startsWith('pythonw')) return py;
  if (low.startsWith('python') && low.endsWith('.exe')) {
    let c = path.join(d, 'pythonw' + b.slice(6)); if (isFile(c)) return c;
    c = path.join(d, 'pythonw.exe'); if (isFile(c)) return c;
  }
  return py;
}
function windowsPython(py) {
  const d = path.dirname(py); const b = path.basename(py);
  if (b.toLowerCase().startsWith('pythonw')) {
    let c = path.join(d, 'python' + b.slice(7)); if (isFile(c)) return c;
    c = path.join(d, 'python.exe'); if (isFile(c)) return c;
  }
  return py;
}
function winQuote(s) {
  if (s.length > 0 && !/[\s"]/.test(s)) return s;
  return '"' + s.replace(/"/g, '\\"') + '"';
}
function jsStr(s) {
  let out = '"';
  for (const ch of String(s)) {
    switch (ch) {
      case '\\': out += '\\\\'; break;
      case '"': out += '\\"'; break;
      case '\n': out += '\\n'; break;
      case '\r': out += '\\r'; break;
      case '\t': out += '\\t'; break;
      default: out += ch;
    }
  }
  return out + '"';
}
function windowsCommandLine(runner, copyPath) {
  const parts = runner.map(winQuote); parts.push(winQuote(copyPath));
  return parts.join(' ');
}
function writeWindowsLauncher(name, copyPath, copyDir, hidden, runner) {
  const style = hidden ? 0 : 1; // WScript.Shell.Run window style: 0 hidden, 1 normal
  const cmd = windowsCommandLine(runner, copyPath);
  let s = '';
  s += '// Auto-generated by auto.js - starts the installed program at logon (WSH/JScript).\n';
  s += '// Invoked via: wscript.exe //B //Nologo //E:jscript "<this file>"\n';
  s += 'var CMD = ' + jsStr(cmd) + ';\n';
  s += 'var CWD = ' + jsStr(copyDir) + ';\n';
  s += 'var STYLE = ' + style + ';\n';
  s += 'try {\n';
  s += '  var sh = new ActiveXObject("WScript.Shell");\n';
  const denv = dotnetEnvLines(runner, false);
  if (denv.length) {
    s += '  try { var env = sh.Environment("PROCESS");\n';
    for (const [k, v] of denv) s += `    env(${jsStr(k)}) = ${jsStr(v)};\n`;
    s += '  } catch (e2) {}\n';
  }
  s += '  try { var fso = new ActiveXObject("Scripting.FileSystemObject"); if (CWD && fso.FolderExists(CWD)) { sh.CurrentDirectory = CWD; } } catch (e1) {}\n';
  s += '  sh.Run(CMD, STYLE, false);\n';
  s += '} catch (e) {}\n';
  const p = windowsLauncherPath(name);
  writeText(p, s);
  return p;
}
function windowsRunCommand(name) { return `wscript.exe //B //Nologo //E:jscript "${windowsLauncherPath(name)}"`; }

function windowsCreateAutostart(copyPath, copyDir, name, hidden, runner) {
  writeWindowsLauncher(name, copyPath, copyDir, hidden, runner);
  const valueName = windowsTaskName(name);
  const command = windowsRunCommand(name);
  const res = run(['reg', 'add', WIN_RUN_KEY, '/v', valueName, '/t', 'REG_SZ', '/d', command, '/f']);
  if (res.code !== 0) { log(`[-] Could not write the Run registry value: ${trimmed(res)}`); return false; }
  run(['reg', 'delete', WIN_APPROVED_KEY, '/v', valueName, '/f']); // clear any "disabled" flag
  windowsRemoveLegacyTask(name, true);
  windowsRemoveVbsLauncher(name);
  windowsRemoveCmdWrapper(name);
  tryRemoveFile(windowsLegacyPywPath(name), 'old Python launcher');
  log(`[+] Windows autostart registered (no admin needed): ${WIN_RUN_KEY}\\${valueName}`);
  return true;
}
function windowsRemoveLegacyTask(name, quiet) {
  const taskName = windowsTaskName(name);
  const q = run(['schtasks', '/Query', '/TN', taskName]);
  if (q.code !== 0) return;
  const res = run(['schtasks', '/Delete', '/TN', taskName, '/F']);
  if (res.code === 0) log(`[+] Removed old scheduled task: ${taskName}`);
  else if (!quiet) {
    log(`[!] An old scheduled task '${taskName}' from a previous version`);
    log('    exists but needs an elevated prompt to delete.  Remove it once');
    log(`    with:  schtasks /Delete /TN ${taskName} /F   (as Administrator)`);
  }
}
function windowsRemoveAutostart(name) {
  const valueName = windowsTaskName(name);
  const res = run(['reg', 'delete', WIN_RUN_KEY, '/v', valueName, '/f']);
  if (res.code === 0) log(`[+] Removed Windows autostart entry: ${valueName}`);
  run(['reg', 'delete', WIN_APPROVED_KEY, '/v', valueName, '/f']);
  windowsRemoveLegacyTask(name, false);
  tryRemoveFile(windowsLauncherPath(name), 'launcher');
  tryRemoveFile(windowsLegacyPywPath(name), 'old Python launcher');
}
function windowsLaunchNow(copyPath, copyDir, name) {
  const launcher = windowsLauncherPath(name);
  try {
    const child = cp.spawn('wscript.exe', ['//B', '//Nologo', '//E:jscript', launcher], {
      cwd: exists(copyDir) ? copyDir : undefined, detached: true, stdio: 'ignore',
    });
    child.unref();
    log('[+] Started now via the logon launcher.');
  } catch (e) { log('[-] Could not start the program now; it will start at next logon.'); }
}
function windowsFindPids(copyPath) {
  const needle = normPath(copyPath).toLowerCase().replace(/'/g, "''");
  const ps =
    `$n = '${needle}'; ` +
    'Get-CimInstance Win32_Process | ' +
    'Where-Object { $_.CommandLine -and $_.CommandLine.ToLower().Contains($n) ' +
    `-and $_.ProcessId -ne ${process.pid} } | ` +
    'Select-Object -ExpandProperty ProcessId';
  const res = run(['powershell', '-NoProfile', '-NonInteractive', '-Command', ps]);
  const pids = [];
  for (const x of (res.out || '').split(/\s+/)) { const pid = parseInt(x.trim(), 10); if (!isNaN(pid)) pids.push(pid); }
  return pids;
}
function windowsKill(copyPath) {
  for (const pid of windowsFindPids(copyPath)) { run(['taskkill', '/F', '/T', '/PID', String(pid)]); log(`[+] Killed PID ${pid}`); }
}
function windowsRemoveVbsLauncher(name) {
  const vbs = windowsVbsPath(name);
  if (exists(vbs)) { try { fs.unlinkSync(vbs); log(`[+] Removed old VBS launcher: ${vbs}`); } catch (e) { log(`[-] Could not remove VBS launcher: ${e.message}`); } }
}
function windowsRemoveCmdWrapper(name) {
  const wrapper = windowsCmdWrapperPath(name);
  if (exists(wrapper)) { try { fs.unlinkSync(wrapper); log(`[+] Removed old CMD wrapper: ${wrapper}`); } catch (e) { log(`[-] Could not remove CMD wrapper: ${e.message}`); } }
}

// ================================================================
// Stop helpers
// ================================================================
function resolveCopyPaths(targetScript, name, state) {
  const paths = new Set();
  if (state[name] && state[name].copy) paths.add(normPath(state[name].copy));
  const baseN = baseName(String(targetScript || '').replace(/\\/g, '/'));
  if (baseN) paths.add(normPath(path.join(BaseDir, baseN)));
  for (const ext of KNOWN_EXTS) paths.add(normPath(path.join(BaseDir, name + ext)));
  if (exists(BaseDir)) {
    try { for (const f of fs.readdirSync(BaseDir)) { if (sanitizeName(f) === name) paths.add(normPath(path.join(BaseDir, f))); } } catch (e) {}
  }
  return paths;
}
function removeWithRetry(p, attempts = 6, delayMs = 500) {
  for (let i = 0; i < attempts; i++) {
    try { fs.unlinkSync(p); log(`[+] Removed copy: ${p}`); return true; }
    catch (e) {
      if (e.code === 'ENOENT') return true;
      if ((e.code === 'EACCES' || e.code === 'EPERM' || e.code === 'EBUSY') && i < attempts - 1) { sleepMs(delayMs); continue; }
      if (i >= attempts - 1) { log(`[-] Could not remove copy ${p} (still locked)`); return false; }
    }
  }
  return false;
}
function sleepMs(ms) { try { cp.execSync(process.platform === 'win32' ? `ping -n ${Math.max(1, Math.round(ms / 1000) + 1)} 127.0.0.1 >NUL` : `sleep ${ms / 1000}`); } catch (e) {} }

// ================================================================
// Install / Uninstall
// ================================================================
function copyScript(target, copyPath) {
  if (normPath(target) === normPath(copyPath)) return;
  for (let i = 0; i < 6; i++) {
    try { fs.copyFileSync(target, copyPath); log(`[+] Copied '${target}' -> '${copyPath}'`); return; }
    catch (e) {
      if ((e.code === 'EACCES' || e.code === 'EPERM' || e.code === 'EBUSY') && i < 5) { sleepMs(500); continue; }
      if (i >= 5) die(`[-] Could not overwrite ${copyPath}: ${e.message}`);
    }
  }
}
function killExisting(name, copyPath) {
  try {
    if (isAndroid()) androidKill(copyPath);
    else if (isMacOS()) macosKill(copyPath);
    else if (isWindows()) windowsKill(copyPath);
    else linuxKill(copyPath, name);
  } catch (e) { /* ignore */ }
}

function install(targetScript, hidden, hideDir) {
  if (isIOS()) iosUnsupported();
  if (!isFile(targetScript)) die(`[-] File not found: ${targetScript}`);
  targetScript = path.resolve(targetScript);

  const lang = detectLanguage(targetScript);
  if (lang === 'unknown') die(`[-] Unsupported file type: ${baseName(targetScript)} (supported: .py .js .ts .cs .sh).`);

  ensureDirectory();
  if (hideDir) hideInstallDir();

  const name = sanitizeName(targetScript);
  const system = sysNameLower();
  const prevState = loadState();
  if (prevState[name] && prevState[name].copy) killExisting(name, prevState[name].copy);

  const runner = requireRunner(targetScript, hidden);

  const copyPath = normPath(path.join(BaseDir, baseName(targetScript)));
  const copyDir = BaseDir;
  let plat, mechanism, effectiveHidden;

  if (isAndroid()) {
    if (!isTermux()) {
      log('[-] Android support requires Termux.  Install Termux from F-Droid');
      log('    and run this script inside Termux.');
      throw new ExitError(1);
    }
    killExisting(name, copyPath);
    copyScript(targetScript, copyPath);
    androidCreateAutostart(copyPath, copyDir, name, hidden, runner);
    androidLaunchNow(copyPath, copyDir, hidden, runner);
    plat = 'android'; mechanism = 'termux-boot'; effectiveHidden = hidden;
  } else if (isMacOS()) {
    killExisting(name, copyPath);
    copyScript(targetScript, copyPath);
    if (!macosCreateAutostart(copyPath, copyDir, name, hidden, runner)) macosLaunchNow(copyPath, copyDir, name, hidden, runner);
    plat = 'macos'; mechanism = 'launchagent'; effectiveHidden = hidden;
  } else if (isWindows()) {
    killExisting(name, copyPath);
    copyScript(targetScript, copyPath);
    if (!windowsCreateAutostart(copyPath, copyDir, name, hidden, runner)) die('[-] Could not create autostart entry.');
    windowsLaunchNow(copyPath, copyDir, name);
    plat = 'windows'; mechanism = 'run-key'; effectiveHidden = hidden;
  } else if (isLinux()) {
    if (runningAsRootViaSudo()) {
      log('[!] Running under sudo: the autostart entry will be created for');
      log('    root, not for your desktop user.  Run without sudo instead.');
    }
    if (release().toLowerCase().includes('microsoft')) {
      log('[!] WSL detected: Linux autostart inside WSL only runs when the');
      log('    WSL distro itself starts, not when Windows boots.  For a');
      log('    program that must start with Windows, install it on Windows');
      log('    (run auto.js there) instead.');
    }
    killExisting(name, copyPath);
    copyScript(targetScript, copyPath);

    let linuxMech;
    if (isLinuxHeadless()) {
      if (!hidden) {
        log('[!] Headless Linux detected: no DISPLAY/WAYLAND_DISPLAY.');
        log('    Visible terminal mode is unavailable; installing in');
        log('    background mode.');
      }
      if (hasSystemd() && linuxCreateSystemdService(copyPath, copyDir, name, true, runner)) {
        linuxMech = 'systemd';
        linuxRemoveCronAutostart(name);
      } else {
        linuxRemoveSystemdService(name);
        log('[!] Using a cron @reboot entry for boot persistence.');
        linuxCreateCronAutostart(copyPath, copyDir, name, runner);
        linuxMech = 'cron';
      }
      linuxLaunchNow(copyPath, copyDir, name, true, runner);
    } else if (isHyprland() && hyprlandCreateAutostart(copyPath, copyDir, name, hidden, runner)) {
      linuxRemoveAutostart(name);
      linuxMech = 'hyprland';
      linuxLaunchNow(copyPath, copyDir, name, hidden, runner);
    } else {
      if (isWlrootsWayland()) {
        log('[!] This Wayland compositor may not run XDG autostart');
        log('    entries.  If the program does not start after login,');
        log(`    add \`${linuxLauncherPath(name)}\` to your compositor's`);
        log('    startup config (e.g. `exec` in sway\'s config).');
      }
      linuxCreateAutostart(copyPath, copyDir, name, hidden, runner);
      linuxMech = 'xdg';
      linuxLaunchNow(copyPath, copyDir, name, hidden, runner);
    }
    plat = 'linux'; mechanism = linuxMech; effectiveHidden = isLinuxHeadless() ? true : hidden;
  } else {
    die(`[-] Unsupported OS: ${system || 'unknown'}`);
    return;
  }

  const state = loadState();
  state[name] = {
    source: path.resolve(targetScript),
    copy: copyPath,
    hidden: effectiveHidden,
    platform: plat,
    mechanism: mechanism,
    language: lang,
  };
  saveState(state);
  log(`[+] '${name}' installed. It will auto-start on every reboot/login.`);
}

function uninstall(targetScript) {
  if (isIOS()) iosUnsupported();
  ensureDirectory();
  const name = sanitizeName(targetScript);
  const state = loadState();

  const copyPaths = resolveCopyPaths(targetScript, name, state);
  log(`[*] Resolved ${copyPaths.size} candidate path(s) for '${name}'.`);

  for (const cp2 of copyPaths) {
    if (isAndroid()) androidKill(cp2);
    else if (isMacOS()) macosKill(cp2);
    else if (isWindows()) windowsKill(cp2);
    else linuxKill(cp2, name);
  }

  if (isAndroid()) androidRemoveAutostart(name);
  else if (isMacOS()) macosRemoveAutostart(name);
  else if (isWindows()) { windowsRemoveAutostart(name); windowsRemoveVbsLauncher(name); windowsRemoveCmdWrapper(name); }
  else { linuxRemoveAutostart(name); linuxRemoveSystemdService(name); linuxRemoveCronAutostart(name); hyprlandRemoveAutostart(name); linuxRemoveLauncher(name); }

  for (const cp2 of copyPaths) if (exists(cp2)) removeWithRetry(cp2);

  if (state[name]) { delete state[name]; saveState(state); }
  log(`[+] '${name}' stopped permanently.`);
}

// ================================================================
// Bootstrap
// ================================================================
try {
  main(process.argv.slice(2));
  process.exit(0);
} catch (e) {
  if (e instanceof ExitError) process.exit(e.code);
  process.stderr.write(`[-] ${e && e.stack ? e.stack : e}\n`);
  process.exit(1);
}
