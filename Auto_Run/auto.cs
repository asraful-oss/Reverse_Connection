// auto.cs — Install a script to run on every reboot/login, and remove it with --stop.
//
// Cross-platform, cross-language successor to auto.py. Instead of only Python,
// it detects the language from the target file's extension and makes it just run:
//
//     auto  app.py   --hidden      # Python
//     auto  app.js   --hidden      # JavaScript
//     auto  app.ts   --hidden      # TypeScript
//     auto  app.cs   --hidden      # C#
//     auto  app.sh   --hidden      # POSIX shell
//
// If the matching runtime is not already installed, auto.cs quietly fetches an
// official portable copy into ~/.configs/runtime (the user's home dir, no admin,
// no prompts) and uses that. Node runs .js and .ts; Python runs .py; the .NET
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
// The script COPY keeps its original extension (so a .js stays a .js). Window
// visibility is handled by the launcher, not by renaming the file.
//
// Target framework: .NET 8+. Single file. No external NuGet packages.

using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;

namespace AutoRun
{
    internal static class Program
    {
        // --------------------------------------------------------------
        // Paths (BaseDir / StateFile may be re-pointed by HideInstallDir)
        // --------------------------------------------------------------
        internal static string BaseDir = NormPath(ExpandUser("~/.configs"));
        internal static string StateFile = Path.Combine(NormPath(ExpandUser("~/.configs")), "state.json");

        internal static readonly string LinuxAutostartDir = ExpandUser("~/.config/autostart");
        internal static readonly string LinuxSystemdUserDir = ExpandUser("~/.config/systemd/user");
        internal static readonly string MacLaunchAgentsDir = ExpandUser("~/Library/LaunchAgents");
        internal static readonly string TermuxBootDir = ExpandUser("~/.termux/boot");
        internal static readonly string HyprDir = ExpandUser("~/.config/hypr");

        private const string WinRunKey =
            @"Software\Microsoft\Windows\CurrentVersion\Run";
        private const string WinApprovedKey =
            @"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run";

        // Known script extensions, longest/most-specific first (sanitize strips them).
        private static readonly string[] KnownExts =
            { ".pyw", ".py", ".mjs", ".cjs", ".js", ".csx", ".cs", ".ts", ".bash", ".sh" };

        private static readonly JsonSerializerOptions JsonOpts = new()
        {
            WriteIndented = true,
            PropertyNameCaseInsensitive = true,
        };

        // ==============================================================
        // Entry point
        // ==============================================================
        internal static int Main(string[] argv)
        {
            try
            {
                var (script, hide, hidden, stop) = ParseArgs(argv);
                if (stop) Uninstall(script);
                else Install(script, hidden: hide || hidden, hideDir: hidden);
                return 0;
            }
            catch (ExitException ex)
            {
                return ex.Code;
            }
        }

        private sealed class ExitException : Exception
        {
            public int Code { get; }
            public ExitException(int code) { Code = code; }
        }

        private static void Die(string msg, int code = 1)
        {
            Console.Error.WriteLine(msg);
            throw new ExitException(code);
        }

        private sealed class Entry
        {
            public string source { get; set; }
            public string copy { get; set; }
            public bool hidden { get; set; }
            public string platform { get; set; }
            public string mechanism { get; set; }
            public string language { get; set; }
        }

        // ==============================================================
        // CLI parsing
        // ==============================================================
        private static (string script, bool hide, bool hidden, bool stop) ParseArgs(string[] argv)
        {
            bool hide = false, hidden = false, stop = false;
            string script = null;
            foreach (var a in argv)
            {
                switch (a)
                {
                    case "--hide": hide = true; break;
                    case "--hidden": hidden = true; break;
                    case "--stop": stop = true; break;
                    case "-h":
                    case "--help":
                        PrintUsage();
                        throw new ExitException(0);
                    default:
                        if (a.StartsWith("-"))
                        {
                            Console.Error.WriteLine($"[-] Unknown option: {a}");
                            PrintUsage();
                            throw new ExitException(2);
                        }
                        if (script == null) script = a;
                        else Die($"[-] Unexpected extra argument: {a}", 2);
                        break;
                }
            }
            if (string.IsNullOrEmpty(script))
            {
                Console.Error.WriteLine("[-] Missing SCRIPT argument.");
                PrintUsage();
                throw new ExitException(2);
            }
            return (script, hide, hidden, stop);
        }

        private static void PrintUsage()
        {
            Console.WriteLine(
@"Usage: auto SCRIPT [--hide | --hidden] [--stop]

Install SCRIPT to run automatically on every reboot/login. The language is
detected from the file extension (.py .js .ts .cs .sh).

  --hide     run silently in the background (no window)
  --hidden   like --hide, and also hide the install folder (~/.configs)
  --stop     stop and permanently remove the autostart entry");
        }

        // ==============================================================
        // Platform detection
        // ==============================================================
        internal static bool IsWindows() => RuntimeInformation.IsOSPlatform(OSPlatform.Windows);
        internal static bool IsMacHost() => RuntimeInformation.IsOSPlatform(OSPlatform.OSX);
        internal static bool IsLinuxHost() => RuntimeInformation.IsOSPlatform(OSPlatform.Linux);

        internal static bool IsIOS()
        {
            if (!IsMacHost()) return false;
            var home = Environment.GetEnvironmentVariable("HOME") ?? "";
            return home.StartsWith("/private/var/mobile") || home.StartsWith("/var/mobile");
        }

        internal static bool IsMacOS() => IsMacHost() && !IsIOS();

        internal static bool IsTermux()
        {
            var prefix = Environment.GetEnvironmentVariable("PREFIX") ?? "";
            if (prefix.StartsWith("/data/data/com.termux")) return true;
            return Environment.GetEnvironmentVariable("TERMUX_VERSION") != null;
        }

        internal static bool IsAndroid()
        {
            if (!IsLinuxHost()) return false;
            return Environment.GetEnvironmentVariable("ANDROID_ROOT") != null
                   || File.Exists("/system/build.prop")
                   || IsTermux();
        }

        internal static bool IsLinux() => IsLinuxHost() && !IsAndroid();

        internal static bool IsLinuxHeadless()
        {
            if (!IsLinux()) return false;
            return string.IsNullOrEmpty(Environment.GetEnvironmentVariable("DISPLAY"))
                && string.IsNullOrEmpty(Environment.GetEnvironmentVariable("WAYLAND_DISPLAY"));
        }

        private static string SysNameLower()
        {
            if (IsWindows()) return "windows";
            if (IsMacHost()) return "darwin";
            if (IsAndroid()) return "android";
            if (IsLinuxHost()) return "linux";
            return (RuntimeInformation.OSDescription ?? "unknown").ToLowerInvariant();
        }

        private static string Release()
        {
            try
            {
                if (File.Exists("/proc/sys/kernel/osrelease"))
                    return File.ReadAllText("/proc/sys/kernel/osrelease").Trim();
            }
            catch { }
            return RuntimeInformation.OSDescription ?? "";
        }

        // ==============================================================
        // Path / fs helpers
        // ==============================================================
        internal static string HomeDir()
        {
            var h = Environment.GetEnvironmentVariable("HOME");
            if (!string.IsNullOrEmpty(h)) return h;
            var up = Environment.GetEnvironmentVariable("USERPROFILE");
            if (!string.IsNullOrEmpty(up)) return up;
            var drive = Environment.GetEnvironmentVariable("HOMEDRIVE");
            var hp = Environment.GetEnvironmentVariable("HOMEPATH");
            if (!string.IsNullOrEmpty(drive) && !string.IsNullOrEmpty(hp)) return drive + hp;
            return Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        }

        internal static string ExpandUser(string path)
        {
            if (path == "~") return HomeDir();
            if (path.StartsWith("~/") || path.StartsWith("~\\"))
                return Path.Combine(HomeDir(), path.Substring(2));
            return path;
        }

        internal static string NormPath(string p)
        {
            if (string.IsNullOrEmpty(p)) return p;
            try { return Path.GetFullPath(p); }
            catch { return p; }
        }

        private static string BaseName(string p)
        {
            if (string.IsNullOrEmpty(p)) return p ?? "";
            p = p.Replace("\\", "/");
            int i = p.LastIndexOf('/');
            return i < 0 ? p : p.Substring(i + 1);
        }

        private static void WriteText(string path, string content) =>
            File.WriteAllText(path, content, new UTF8Encoding(false));

        private static void ChmodExec(string path)
        {
            if (IsWindows()) return;
            try
            {
                File.SetUnixFileMode(path,
                    UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute |
                    UnixFileMode.GroupRead | UnixFileMode.GroupExecute |
                    UnixFileMode.OtherRead | UnixFileMode.OtherExecute);
            }
            catch { }
        }

        private static void Chmod644(string path)
        {
            if (IsWindows()) return;
            try
            {
                File.SetUnixFileMode(path,
                    UnixFileMode.UserRead | UnixFileMode.UserWrite |
                    UnixFileMode.GroupRead | UnixFileMode.OtherRead);
            }
            catch { }
        }

        private static void TryRemoveFile(string path, string label)
        {
            if (!File.Exists(path)) return;
            try { File.Delete(path); Console.WriteLine($"[+] Removed {label}: {path}"); }
            catch (Exception e) { Console.WriteLine($"[-] Could not remove {label} {path}: {e.Message}"); }
        }

        // ==============================================================
        // Quoting helpers
        // ==============================================================
        internal static string ShQuote(string s)
        {
            if (s == null) return "''";
            if (s.Length == 0) return "''";
            if (s.All(c => char.IsLetterOrDigit(c) || "@%_+=:,./-".IndexOf(c) >= 0)) return s;
            return "'" + s.Replace("'", "'\"'\"'") + "'";
        }

        private static string QuoteExecArg(string arg)
        {
            // freedesktop Desktop Entry Exec quoting: general escape THEN exec quoting.
            arg = arg.Replace("\\", "\\\\\\\\"); // 1 backslash -> 4
            arg = arg.Replace("\"", "\\\\\"");    // " -> 2 backslashes + "
            arg = arg.Replace("`", "\\\\`");
            arg = arg.Replace("$", "\\\\$");
            arg = arg.Replace("%", "%%");
            return "\"" + arg + "\"";
        }

        private static string BuildExecLine(IEnumerable<string> cmd) =>
            string.Join(" ", cmd.Select(QuoteExecArg));

        private static string SystemdQuote(string arg)
        {
            arg = (arg ?? "").Replace("\\", "\\\\").Replace("\"", "\\\"").Replace("%", "%%");
            return "\"" + arg + "\"";
        }

        private static string LuaQuote(string s) =>
            "\"" + (s ?? "").Replace("\\", "\\\\").Replace("\"", "\\\"").Replace("\n", "\\n") + "\"";

        private static string JoinSh(IEnumerable<string> toks) => string.Join(" ", toks.Select(ShQuote));

        private static string RunnerPlain(List<string> runner, string script)
        {
            var sb = new StringBuilder();
            for (int i = 0; i < runner.Count; i++) { if (i > 0) sb.Append(' '); sb.Append(ShQuote(runner[i])); }
            sb.Append(' ').Append(ShQuote(script));
            return sb.ToString();
        }

        private static string RunnerExecLine(List<string> runner, string script) =>
            "exec " + RunnerPlain(runner, script);

        // ==============================================================
        // Process helpers
        // ==============================================================
        internal sealed class Proc { public int Code; public string Out = ""; public string Err = ""; }

        internal static Proc RunProc(IEnumerable<string> cmd, string input = null,
            IDictionary<string, string> env = null, int timeoutSec = 0, bool stdinNull = false)
        {
            var list = cmd.ToList();
            var p = new Proc();
            if (list.Count == 0) { p.Code = 127; p.Err = "empty command"; return p; }

            var psi = new ProcessStartInfo
            {
                FileName = list[0],
                UseShellExecute = false,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
            };
            for (int i = 1; i < list.Count; i++) psi.ArgumentList.Add(list[i]);
            bool redirectIn = input != null || stdinNull;
            if (redirectIn) psi.RedirectStandardInput = true;
            if (env != null) foreach (var kv in env) psi.Environment[kv.Key] = kv.Value;

            Process proc;
            try { proc = Process.Start(psi); }
            catch (Win32Exception) { p.Code = 127; p.Err = $"{list[0]}: not found"; return p; }
            catch (Exception e) { p.Code = 126; p.Err = e.Message; return p; }
            if (proc == null) { p.Code = 126; p.Err = "failed to start"; return p; }

            Task<string> outT = proc.StandardOutput.ReadToEndAsync();
            Task<string> errT = proc.StandardError.ReadToEndAsync();
            if (redirectIn)
            {
                try { if (input != null) proc.StandardInput.Write(input); proc.StandardInput.Close(); }
                catch { }
            }

            bool exited = timeoutSec > 0 ? proc.WaitForExit(timeoutSec * 1000) : true;
            if (timeoutSec <= 0) proc.WaitForExit();
            if (!exited)
            {
                try { proc.Kill(true); } catch { }
                p.Code = 124; p.Out = SafeResult(outT); p.Err = $"{list[0]}: timed out"; return p;
            }
            p.Out = SafeResult(outT);
            p.Err = SafeResult(errT);
            p.Code = proc.ExitCode;
            return p;
        }

        private static string SafeResult(Task<string> t)
        {
            try { return t.GetAwaiter().GetResult() ?? ""; } catch { return ""; }
        }

        private static string Trimmed(Proc r)
        {
            if (r == null) return "";
            var e = (r.Err ?? "").Trim();
            return e.Length > 0 ? e : (r.Out ?? "").Trim();
        }

        internal static string Which(string name)
        {
            if (string.IsNullOrEmpty(name)) return null;
            string[] pathext = IsWindows()
                ? (Environment.GetEnvironmentVariable("PATHEXT") ?? ".EXE;.CMD;.BAT;.COM")
                    .Split(';', StringSplitOptions.RemoveEmptyEntries)
                : Array.Empty<string>();
            bool hasExt = Path.GetExtension(name).Length > 0;

            if (name.Contains('/') || name.Contains('\\'))
            {
                if (File.Exists(name)) return name;
                if (IsWindows() && !hasExt)
                    foreach (var e in pathext) if (File.Exists(name + e)) return name + e;
                return null;
            }

            var paths = (Environment.GetEnvironmentVariable("PATH") ?? "")
                .Split(Path.PathSeparator);
            foreach (var dir in paths)
            {
                if (string.IsNullOrEmpty(dir)) continue;
                string full;
                try { full = Path.Combine(dir, name); } catch { continue; }
                if (File.Exists(full)) return full;
                if (IsWindows() && !hasExt)
                    foreach (var e in pathext) { var c = full + e; if (File.Exists(c)) return c; }
            }
            return null;
        }

        private static int DotnetMajor(string dotnet)
        {
            try
            {
                var r = RunProc(new[] { dotnet, "--version" }, timeoutSec: 20);
                if (r.Code != 0) return 0;
                var m = Regex.Match(r.Out ?? "", @"(\d+)");
                return m.Success && int.TryParse(m.Groups[1].Value, out var v) ? v : 0;
            }
            catch { return 0; }
        }

        private static string FirstExe(params string[] names)
        {
            foreach (var n in names) { var w = Which(n); if (w != null) return w; }
            return null;
        }

        private static bool HasBinary(string name) => Which(name) != null;

        private static bool HasSystemd() =>
            HasBinary("systemctl") && Directory.Exists("/run/systemd/system");

        // ---- libc P/Invoke (unix only; resolved at call time) --------
        [DllImport("libc", EntryPoint = "getuid")] private static extern uint c_getuid();
        [DllImport("libc", EntryPoint = "geteuid")] private static extern uint c_geteuid();
        [DllImport("libc", EntryPoint = "kill", SetLastError = true)] private static extern int c_kill(int pid, int sig);

        private static int GetUid()
        {
            try { return (int)c_getuid(); }
            catch
            {
                var r = RunProc(new[] { "id", "-u" });
                if (r.Code == 0 && int.TryParse((r.Out ?? "").Trim(), out var u)) return u;
                return 0;
            }
        }

        private static bool RunningAsRootViaSudo()
        {
            if (IsWindows()) return false;
            try { return c_geteuid() == 0 && Environment.GetEnvironmentVariable("SUDO_USER") != null; }
            catch { return false; }
        }

        private static void UnixKillPid(int pid)
        {
            try
            {
                int rc = c_kill(pid, 15); // SIGTERM
                if (rc == 0) { Console.WriteLine($"[+] Killed PID {pid}"); return; }
            }
            catch
            {
                var r = RunProc(new[] { "kill", "-TERM", pid.ToString() });
                if (r.Code == 0) Console.WriteLine($"[+] Killed PID {pid}");
            }
        }

        private static IDictionary<string, string> LinuxUserEnv()
        {
            var d = new Dictionary<string, string>();
            if (string.IsNullOrEmpty(Environment.GetEnvironmentVariable("XDG_RUNTIME_DIR")))
                d["XDG_RUNTIME_DIR"] = $"/run/user/{GetUid()}";
            return d;
        }

        private static void UnixSpawnDetached(string shellCmd, string cwd)
        {
            var full = cwd != null ? $"cd {ShQuote(cwd)} && {shellCmd}" : shellCmd;
            var psi = new ProcessStartInfo { UseShellExecute = false };
            var setsid = Which("setsid");
            if (setsid != null)
            {
                psi.FileName = setsid;
                psi.ArgumentList.Add("sh"); psi.ArgumentList.Add("-c"); psi.ArgumentList.Add(full);
            }
            else
            {
                psi.FileName = "sh";
                psi.ArgumentList.Add("-c"); psi.ArgumentList.Add(full);
            }
            try { Process.Start(psi); } catch { }
        }

        // ==============================================================
        // State
        // ==============================================================
        private static Dictionary<string, Entry> LoadState()
        {
            if (!File.Exists(StateFile)) return new();
            try
            {
                var txt = File.ReadAllText(StateFile);
                return JsonSerializer.Deserialize<Dictionary<string, Entry>>(txt, JsonOpts) ?? new();
            }
            catch { return new(); }
        }

        private static void SaveState(Dictionary<string, Entry> state)
        {
            Directory.CreateDirectory(BaseDir);
            var tmp = StateFile + ".tmp";
            File.WriteAllText(tmp, JsonSerializer.Serialize(state, JsonOpts), new UTF8Encoding(false));
            File.Move(tmp, StateFile, true);
        }

        private static string SanitizeName(string name)
        {
            string b = BaseName((name ?? "").Replace("\\", "/"));
            string low = b.ToLowerInvariant();
            foreach (var ext in KnownExts)
                if (low.EndsWith(ext)) { b = b.Substring(0, b.Length - ext.Length); break; }
            b = Regex.Replace(b, "[^a-zA-Z0-9\\-_]", "-").ToLowerInvariant();
            return b.Length == 0 ? "program" : b;
        }

        // ==============================================================
        // Language / runtime resolution
        // ==============================================================
        private static string DetectLanguage(string scriptPath)
        {
            switch (Path.GetExtension(scriptPath).ToLowerInvariant())
            {
                case ".py":
                case ".pyw": return "python";
                case ".js":
                case ".mjs":
                case ".cjs": return "node";
                case ".ts": return "typescript";
                case ".cs":
                case ".csx": return "csharp";
                case ".sh":
                case ".bash": return "shell";
                default: return "unknown";
            }
        }

        // -------- Runtime auto-provisioning (per-user, NO admin, NO prompts) ----
        // Every language is made to "just run": we use whatever the system has,
        // and if it is missing we quietly fetch an official portable runtime into
        // BaseDir/runtime (the user's home dir — never a system path) and bake its
        // absolute path into the launcher.  If even that fails (no network) we fall
        // back to the bare command so the entry still works once a runtime appears.
        private static string RuntimeRoot()
        {
            var d = Path.Combine(BaseDir, "runtime");
            Directory.CreateDirectory(d);
            return d;
        }
        private static string HostArch()
        {
            switch (RuntimeInformation.OSArchitecture)
            {
                case Architecture.X64: return "x64";
                case Architecture.Arm64: return "arm64";
                case Architecture.X86: return "x86";
                default: return RuntimeInformation.OSArchitecture.ToString().ToLowerInvariant();
            }
        }
        private static bool _provNote = false;
        private static void ProvisionNote()
        {
            if (_provNote) return; _provNote = true;
            Console.WriteLine("[*] Setting up a private runtime in your home folder (no admin needed)...");
        }

        // Download with redirect-following via HttpClient. Returns true on success.
        private static bool DownloadFile(string url, string dest)
        {
            try
            {
                var handler = new System.Net.Http.HttpClientHandler { AllowAutoRedirect = true, MaxAutomaticRedirections = 12 };
                using var hc = new System.Net.Http.HttpClient(handler);
                hc.Timeout = TimeSpan.FromMinutes(15);
                hc.DefaultRequestHeaders.Add("User-Agent", "auto-cs");
                using var resp = hc.GetAsync(url, System.Net.Http.HttpCompletionOption.ResponseHeadersRead).GetAwaiter().GetResult();
                if (!resp.IsSuccessStatusCode) return false;
                using var fs = File.Create(dest);
                resp.Content.CopyToAsync(fs).GetAwaiter().GetResult();
                fs.Flush();
                return new FileInfo(dest).Length > 0;
            }
            catch { return false; }
        }
        private static string DownloadString(string url)
        {
            try
            {
                var handler = new System.Net.Http.HttpClientHandler { AllowAutoRedirect = true, MaxAutomaticRedirections = 12 };
                using var hc = new System.Net.Http.HttpClient(handler);
                hc.Timeout = TimeSpan.FromMinutes(2);
                hc.DefaultRequestHeaders.Add("User-Agent", "auto-cs");
                return hc.GetStringAsync(url).GetAwaiter().GetResult();
            }
            catch { return null; }
        }
        // Extract .tar.gz/.tar.xz/.zip via system tar (bsdtar reads zip on win/mac);
        // zip falls back to Expand-Archive/unzip.
        private static bool ExtractArchive(string archive, string destDir, int strip)
        {
            Directory.CreateDirectory(destDir);
            var args = new List<string> { "tar", "-xf", archive, "-C", destDir };
            if (strip > 0) args.Add("--strip-components=" + strip);
            var r = RunProc(args.ToArray(), timeoutSec: 900);
            if (r.Code == 0) return true;
            if (archive.ToLowerInvariant().EndsWith(".zip"))
            {
                if (IsWindows())
                {
                    r = RunProc(new[] { "powershell", "-NoProfile", "-NonInteractive", "-Command",
                        $"Expand-Archive -Force -LiteralPath '{archive}' -DestinationPath '{destDir}'" }, timeoutSec: 900);
                    if (r.Code == 0) { if (strip > 0) FlattenOneLevel(destDir); return true; }
                }
                r = RunProc(new[] { "unzip", "-o", archive, "-d", destDir }, timeoutSec: 900);
                if (r.Code == 0) { if (strip > 0) FlattenOneLevel(destDir); return true; }
            }
            return false;
        }
        private static void FlattenOneLevel(string destDir)
        {
            try
            {
                var items = Directory.GetFileSystemEntries(destDir);
                if (items.Length != 1 || !Directory.Exists(items[0])) return;
                var inner = items[0];
                foreach (var f in Directory.GetFileSystemEntries(inner))
                    Directory.Move(f, Path.Combine(destDir, Path.GetFileName(f)));
                Directory.Delete(inner);
            }
            catch { }
        }
        private static void TryUnlink(string p) { try { File.Delete(p); } catch { } }

        private static (int maj, int min)? NodeVersionOf(string bin)
        {
            try { var r = RunProc(new[] { bin, "-v" }, timeoutSec: 20); var m = Regex.Match(r.Out ?? "", @"v?(\d+)\.(\d+)"); if (m.Success) return (int.Parse(m.Groups[1].Value), int.Parse(m.Groups[2].Value)); }
            catch { }
            return null;
        }
        // Node >= 22.18 (and all 23/24+) strips TypeScript types with no flag.
        private static List<string> NodeTsArgs(string bin)
        {
            var v = NodeVersionOf(bin);
            if (v.HasValue && (v.Value.maj > 22 || (v.Value.maj == 22 && v.Value.min >= 18))) return new List<string> { bin };
            return new List<string> { bin, "--experimental-strip-types" };
        }

        // ---- Node ----
        private static string EnsureNode()
        {
            var sys = FirstExe("node", "nodejs");
            if (sys != null) return sys;
            var dir = Path.Combine(RuntimeRoot(), "node");
            var bin = IsWindows() ? Path.Combine(dir, "node.exe") : Path.Combine(dir, "bin", "node");
            if (File.Exists(bin)) return bin;
            ProvisionNote();
            if (DownloadNode(dir) && File.Exists(bin)) return bin;
            return null;
        }
        private static string LatestNodeLTS()
        {
            var s = DownloadString("https://nodejs.org/dist/index.json");
            if (s == null) return null;
            try
            {
                using var doc = JsonDocument.Parse(s);
                foreach (var el in doc.RootElement.EnumerateArray())
                {
                    if (el.TryGetProperty("lts", out var lts) && lts.ValueKind != JsonValueKind.False)
                        return el.GetProperty("version").GetString();
                }
                var first = doc.RootElement.EnumerateArray().FirstOrDefault();
                return first.ValueKind == JsonValueKind.Object ? first.GetProperty("version").GetString() : null;
            }
            catch { return null; }
        }
        private static bool DownloadNode(string dir)
        {
            var v = LatestNodeLTS(); if (v == null) return false;
            var a = HostArch();
            string file;
            if (IsWindows()) file = $"node-{v}-win-{(a == "x86" ? "x86" : "x64")}.zip";
            else if (IsMacHost()) file = $"node-{v}-darwin-{a}.tar.gz";
            else file = $"node-{v}-linux-{a}.tar.gz";
            var url = $"https://nodejs.org/dist/{v}/{file}";
            var tmp = Path.Combine(RuntimeRoot(), file);
            if (!DownloadFile(url, tmp)) return false;
            var ok = ExtractArchive(tmp, dir, 1);
            TryUnlink(tmp);
            return ok;
        }

        // ---- Python (python-build-standalone, install_only) ----
        private static string EnsurePython(bool hidden)
        {
            var sys = IsWindows() ? FirstExe("python", "python3") : FirstExe("python3", "python");
            if (sys != null) return IsWindows() ? (hidden ? WindowsPythonw(sys) : WindowsPython(sys)) : sys;
            var dir = Path.Combine(RuntimeRoot(), "python");
            var bin = IsWindows() ? Path.Combine(dir, "python.exe") : Path.Combine(dir, "bin", "python3");
            if (File.Exists(bin)) return IsWindows() && hidden ? WindowsPythonw(bin) : bin;
            ProvisionNote();
            if (DownloadPython() && File.Exists(bin)) return IsWindows() && hidden ? WindowsPythonw(bin) : bin;
            return null;
        }
        private static string PythonTriple()
        {
            var a = HostArch();
            if (IsWindows()) return (a == "arm64" ? "aarch64" : "x86_64") + "-pc-windows-msvc-shared";
            if (IsMacHost()) return (a == "arm64" ? "aarch64" : "x86_64") + "-apple-darwin";
            return (a == "arm64" ? "aarch64" : "x86_64") + "-unknown-linux-gnu";
        }
        private static bool DownloadPython()
        {
            var s = DownloadString("https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest");
            if (s == null) return false;
            var urls = new List<string>();
            try
            {
                using var doc = JsonDocument.Parse(s);
                if (doc.RootElement.TryGetProperty("assets", out var assets))
                    foreach (var a in assets.EnumerateArray())
                        if (a.TryGetProperty("browser_download_url", out var u)) urls.Add(u.GetString());
            }
            catch { return false; }
            var triple = PythonTriple();
            var cands = urls.Where(u => u != null && Regex.IsMatch(u, @"cpython-3\.") && u.Contains(triple) && u.EndsWith("install_only.tar.gz")).ToList();
            cands.Sort(); cands.Reverse();
            if (cands.Count == 0) return false;
            var tmp = Path.Combine(RuntimeRoot(), "python.tar.gz");
            if (!DownloadFile(cands[0], tmp)) return false;
            var ok = ExtractArchive(tmp, RuntimeRoot(), 0); // archive top dir is "python/"
            TryUnlink(tmp);
            return ok;
        }

        // ---- .NET (Microsoft dotnet-install, per-user) ----
        private static List<string> EnsureDotnet()
        {
            var sys = Which("dotnet");
            if (sys != null && DotnetMajor(sys) >= 10) return new List<string> { sys, "run" };
            var ds = Which("dotnet-script"); if (ds != null) return new List<string> { ds };
            var dir = Path.Combine(RuntimeRoot(), "dotnet");
            var bin = IsWindows() ? Path.Combine(dir, "dotnet.exe") : Path.Combine(dir, "dotnet");
            if (File.Exists(bin) && DotnetMajor(bin) >= 10) return new List<string> { bin, "run" };
            ProvisionNote();
            if (DownloadDotnet(dir) && File.Exists(bin) && DotnetMajor(bin) >= 10) return new List<string> { bin, "run" };
            return null;
        }
        private static bool DownloadDotnet(string dir)
        {
            Directory.CreateDirectory(dir);
            if (IsWindows())
            {
                var ps1 = Path.Combine(RuntimeRoot(), "dotnet-install.ps1");
                if (!DownloadFile("https://dot.net/v1/dotnet-install.ps1", ps1)) return false;
                var r = RunProc(new[] { "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps1,
                    "-Channel", "10.0", "-InstallDir", dir, "-NoPath" }, timeoutSec: 1800);
                TryUnlink(ps1);
                return r.Code == 0;
            }
            var sh = Path.Combine(RuntimeRoot(), "dotnet-install.sh");
            if (!DownloadFile("https://dot.net/v1/dotnet-install.sh", sh)) return false;
            ChmodExec(sh);
            var bash = FirstExe("bash", "sh") ?? "/bin/sh";
            var rr = RunProc(new[] { bash, sh, "--channel", "10.0", "--install-dir", dir, "--no-path" }, timeoutSec: 1800);
            TryUnlink(sh);
            return rr.Code == 0;
        }

        // Resolve a runner, provisioning its runtime if needed. Never prompts,
        // never dies: returns an absolute-path argv, or a bare fallback command.
        private static List<string> ResolveRunner(string scriptPath, bool hidden)
        {
            switch (DetectLanguage(scriptPath))
            {
                case "python":
                    {
                        var p = EnsurePython(hidden);
                        return new List<string> { p ?? (IsWindows() ? (hidden ? "pythonw" : "python") : "python3") };
                    }
                case "node":
                    {
                        var n = EnsureNode();
                        return new List<string> { n ?? "node" };
                    }
                case "typescript":
                    {
                        var n = EnsureNode();
                        return n != null ? NodeTsArgs(n) : new List<string> { "node" };
                    }
                case "csharp":
                    {
                        return EnsureDotnet() ?? new List<string> { "dotnet", "run" };
                    }
                case "shell":
                    {
                        if (IsWindows()) { var b = Which("bash"); return new List<string> { b ?? "bash" }; }
                        var sh = FirstExe("bash", "sh");
                        return new List<string> { sh ?? "/bin/sh" };
                    }
                default:
                    return new List<string> { "sh" };
            }
        }

        private static List<string> RequireRunner(string scriptPath, bool hidden) => ResolveRunner(scriptPath, hidden);

        // ==============================================================
        // Shared fs setup
        // ==============================================================
        private static void EnsureDirectory()
        {
            Directory.CreateDirectory(BaseDir);
            if (IsLinux())
            {
                Directory.CreateDirectory(LinuxAutostartDir);
                Directory.CreateDirectory(LinuxSystemdUserDir);
            }
            else if (IsMacOS())
            {
                Directory.CreateDirectory(MacLaunchAgentsDir);
            }
        }

        // ---- Windows hidden attribute (P/Invoke) ---------------------
        private const uint FILE_ATTRIBUTE_HIDDEN = 0x02;
        private const uint INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF;
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern uint GetFileAttributesW(string lpFileName);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool SetFileAttributesW(string lpFileName, uint dwFileAttributes);

        private static bool WindowsSetHidden(string path)
        {
            uint current;
            try { current = GetFileAttributesW(path); }
            catch (Exception e) { Console.WriteLine($"[!] Could not read attributes to hide the folder: {e.Message}"); return false; }
            if (current == INVALID_FILE_ATTRIBUTES) current = 0;
            if ((current & FILE_ATTRIBUTE_HIDDEN) != 0) return true;
            if (SetFileAttributesW(path, current | FILE_ATTRIBUTE_HIDDEN)) return true;
            Console.WriteLine("[!] Could not set the hidden attribute on the install folder.");
            return false;
        }

        private static void HideInstallDir()
        {
            var path = BaseDir;
            if (!Directory.Exists(path)) Directory.CreateDirectory(path);

            if (IsWindows())
            {
                if (WindowsSetHidden(path))
                    Console.WriteLine($"[+] Hid the install folder (hidden attribute): {path}");
                return;
            }

            var trimmed = path.TrimEnd(Path.DirectorySeparatorChar, '/');
            var parent = Path.GetDirectoryName(trimmed) ?? "";
            var baseName = BaseName(trimmed);
            if (baseName.StartsWith("."))
            {
                Console.WriteLine($"[+] Install folder already starts with a dot (hidden): {path}");
                return;
            }
            var hiddenPath = Path.Combine(parent, "." + baseName);
            if (Directory.Exists(hiddenPath) || File.Exists(hiddenPath))
            {
                Console.WriteLine($"[!] Cannot rename '{path}' to '{hiddenPath}': target exists.");
                Console.WriteLine("    Leaving the folder visible so nothing is overwritten.");
                return;
            }
            try { Directory.Move(path, hiddenPath); }
            catch (Exception e) { Console.WriteLine($"[!] Could not rename '{path}' to hide it: {e.Message}"); return; }

            BaseDir = hiddenPath;
            StateFile = Path.Combine(BaseDir, "state.json");
            Console.WriteLine($"[+] Hid the install folder by renaming: {path} -> {hiddenPath}");
        }

        // ==============================================================
        // Linux — launcher + XDG autostart
        // ==============================================================
        private static string LinuxDesktopPath(string name) => Path.Combine(LinuxAutostartDir, $"{name}.desktop");
        private static string LinuxSystemdUnitPath(string name) => Path.Combine(LinuxSystemdUserDir, $"{name}.service");
        private static string LinuxLauncherPath(string name) => Path.Combine(BaseDir, $"run-{name}.sh");

        // Quiet, self-contained env for a provisioned .NET runner so hidden runs
        // don't print the first-run banner and have a writable DOTNET_ROOT.
        private static List<(string, string)> DotnetEnv(List<string> runner)
        {
            var b = (runner != null && runner.Count > 0) ? runner[0] : "";
            if (!Regex.IsMatch(b, @"dotnet(\.exe)?$", RegexOptions.IgnoreCase)) return new List<(string, string)>();
            var root = Path.GetDirectoryName(b) ?? "";
            return new List<(string, string)>
            {
                ("DOTNET_CLI_TELEMETRY_OPTOUT", "1"),
                ("DOTNET_NOLOGO", "1"),
                ("DOTNET_SKIP_FIRST_TIME_EXPERIENCE", "1"),
                ("DOTNET_ROOT", root),
            };
        }

        private static string LinuxWriteLauncher(string copyPath, string copyDir, string name, List<string> runner)
        {
            var path = LinuxLauncherPath(name);
            var sb = new StringBuilder();
            sb.Append("#!/bin/sh\n");
            sb.Append($"cd {ShQuote(copyDir)} || exit 1\n");
            foreach (var (k, v) in DotnetEnv(runner)) sb.Append($"export {k}={ShQuote(v)}\n");
            sb.Append("exec");
            foreach (var t in runner) sb.Append(' ').Append(ShQuote(t));
            sb.Append(' ').Append(ShQuote(copyPath)).Append(" \"$@\"\n");
            WriteText(path, sb.ToString());
            ChmodExec(path);
            return path;
        }

        private static void LinuxRemoveLauncher(string name)
        {
            var path = LinuxLauncherPath(name);
            if (File.Exists(path))
            {
                try { File.Delete(path); Console.WriteLine($"[+] Removed launcher script: {path}"); }
                catch (Exception e) { Console.WriteLine($"[-] Could not remove launcher {path}: {e.Message}"); }
            }
        }

        private static void LinuxCreateAutostart(string copyPath, string copyDir, string name, bool hidden, List<string> runner)
        {
            var launcher = LinuxWriteLauncher(copyPath, copyDir, name, runner);
            string execLine;
            const string terminal = "false";
            if (hidden)
            {
                execLine = BuildExecLine(new[] { launcher });
            }
            else
            {
                var cmd = FindLinuxTerminal(launcher);
                if (cmd != null) execLine = BuildExecLine(cmd);
                else
                {
                    execLine = BuildExecLine(new[] { launcher });
                    Console.WriteLine("[!] No terminal emulator found for autostart entry; "
                        + "the program will run without a visible window.");
                }
            }
            var content =
                "[Desktop Entry]\n" +
                "Type=Application\n" +
                $"Name={name}\n" +
                $"Exec={execLine}\n" +
                $"Path={copyDir}\n" +
                $"Terminal={terminal}\n" +
                "X-GNOME-Autostart-enabled=true\n" +
                "Hidden=false\n";
            var path = LinuxDesktopPath(name);
            WriteText(path, content);
            ChmodExec(path);
            Console.WriteLine($"[+] Linux autostart entry: {path}  (Terminal={terminal})");
        }

        private static void LinuxRemoveAutostart(string name)
        {
            var path = LinuxDesktopPath(name);
            if (File.Exists(path)) { File.Delete(path); Console.WriteLine($"[+] Removed Linux autostart entry: {path}"); }
        }

        private static List<string> FindLinuxTerminal(string launcher)
        {
            var candidates = new List<string[]>
            {
                new[] { "xdg-terminal-exec", launcher },
                new[] { "ptyxis", "--", launcher },
                new[] { "kgx", "--", launcher },
                new[] { "gnome-terminal", "--", launcher },
                new[] { "konsole", "-e", launcher },
                new[] { "xfce4-terminal", "-x", launcher },
                new[] { "mate-terminal", "-x", launcher },
                new[] { "cosmic-term", "-e", launcher },
                new[] { "alacritty", "-e", launcher },
                new[] { "kitty", launcher },
                new[] { "ghostty", "-e", launcher },
                new[] { "foot", launcher },
                new[] { "wezterm", "start", "--", launcher },
                new[] { "x-terminal-emulator", "-e", launcher },
                new[] { "xterm", "-e", launcher },
            };
            foreach (var cmd in candidates)
                if (Which(cmd[0]) != null) return cmd.ToList();
            return null;
        }

        private static bool IsHyprland()
        {
            if (!string.IsNullOrEmpty(Environment.GetEnvironmentVariable("HYPRLAND_INSTANCE_SIGNATURE"))) return true;
            var cur = ((Environment.GetEnvironmentVariable("XDG_CURRENT_DESKTOP") ?? "") + " " +
                       (Environment.GetEnvironmentVariable("XDG_SESSION_DESKTOP") ?? "") + " " +
                       (Environment.GetEnvironmentVariable("DESKTOP_SESSION") ?? "")).ToLowerInvariant();
            return cur.Contains("hyprland") || cur.Contains("omarchy");
        }

        private static bool IsWlrootsWayland()
        {
            if (Environment.GetEnvironmentVariable("WAYLAND_DISPLAY") == null) return false;
            var cur = ((Environment.GetEnvironmentVariable("XDG_CURRENT_DESKTOP") ?? "") + " " +
                       (Environment.GetEnvironmentVariable("XDG_SESSION_DESKTOP") ?? "") + " " +
                       (Environment.GetEnvironmentVariable("DESKTOP_SESSION") ?? "")).ToLowerInvariant();
            if (new[] { "gnome", "kde", "plasma" }.Any(de => cur.Contains(de))) return false;
            return new[] { "sway", "river", "wayfire", "niri", "hyprland", "labwc", "wlroots" }
                .Any(wm => cur.Contains(wm));
        }

        // ==============================================================
        // Linux — Hyprland / Omarchy
        // ==============================================================
        private static (string begin, string end) HyprMarker(string name, string comment) =>
            ($"{comment} VisibleBackground {name} BEGIN", $"{comment} VisibleBackground {name} END");

        private static (string path, string kind) HyprTarget()
        {
            var luaMain = Path.Combine(HyprDir, "hyprland.lua");
            var luaAuto = Path.Combine(HyprDir, "autostart.lua");
            var confMain = Path.Combine(HyprDir, "hyprland.conf");
            var confAuto = Path.Combine(HyprDir, "autostart.conf");

            bool Mentions(string path, string needle)
            {
                try { return File.Exists(path) && File.ReadAllText(path).Contains(needle); }
                catch { return false; }
            }

            if (File.Exists(luaMain))
            {
                if (File.Exists(luaAuto) && (
                        Mentions(luaMain, "autostart") || Mentions(luaAuto, "o.launch")
                        || Directory.Exists("/usr/share/omarchy")
                        || Directory.Exists(ExpandUser("~/.local/share/omarchy"))))
                    return (luaAuto, "omarchy-lua");
                return (luaMain, "lua");
            }
            if (File.Exists(confMain))
            {
                if (File.Exists(confAuto) && Mentions(confMain, "autostart.conf"))
                    return (confAuto, "conf");
                return (confMain, "conf");
            }
            return (null, null);
        }

        private static string StripBlock(string text, string begin, string end)
        {
            var outLines = new List<string>();
            bool skip = false;
            foreach (var ln in (text ?? "").Split('\n'))
            {
                var t = ln.Trim();
                if (t == begin) { skip = true; continue; }
                if (t == end) { skip = false; continue; }
                if (!skip) outLines.Add(ln);
            }
            return string.Join("\n", outLines);
        }

        private static bool HyprlandCreateAutostart(string copyPath, string copyDir, string name, bool hidden, List<string> runner)
        {
            var (path, kind) = HyprTarget();
            if (path == null)
            {
                Console.WriteLine("[!] Hyprland detected but no ~/.config/hypr/hyprland.(lua|conf) found.");
                return false;
            }
            var launcher = LinuxWriteLauncher(copyPath, copyDir, name, runner);
            List<string> argv = hidden ? new List<string> { launcher } : (FindLinuxTerminal(launcher) ?? new List<string> { launcher });
            var cmd = JoinSh(argv);

            var comment = (kind == "omarchy-lua" || kind == "lua") ? "--" : "#";
            var (begin, end) = HyprMarker(name, comment);
            string body = kind == "omarchy-lua"
                ? $"o.launch_on_start({LuaQuote(cmd)})"
                : kind == "lua"
                    ? $"hl.on(\"hyprland.start\", function() hl.exec_cmd({LuaQuote(cmd)}) end)"
                    : $"exec-once = {cmd}";

            string text;
            try { text = File.ReadAllText(path); } catch { text = ""; }
            text = StripBlock(text, begin, end).TrimEnd('\n');
            text += $"\n\n{begin}\n{body}\n{end}\n";

            var tmp = path + ".vbtmp";
            WriteText(tmp, text);
            File.Move(tmp, path, true);
            Console.WriteLine($"[+] Hyprland autostart added to {path} ({kind}).");
            return true;
        }

        private static void HyprlandRemoveAutostart(string name)
        {
            foreach (var (fname, comment) in new[]
            {
                ("autostart.lua", "--"), ("hyprland.lua", "--"),
                ("autostart.conf", "#"), ("hyprland.conf", "#"),
            })
            {
                var path = Path.Combine(HyprDir, fname);
                if (!File.Exists(path)) continue;
                var (begin, end) = HyprMarker(name, comment);
                string text;
                try { text = File.ReadAllText(path); } catch { continue; }
                if (!text.Contains(begin)) continue;
                var updated = StripBlock(text, begin, end).TrimEnd('\n') + "\n";
                var tmp = path + ".vbtmp";
                WriteText(tmp, updated);
                File.Move(tmp, path, true);
                Console.WriteLine($"[+] Removed Hyprland autostart entry from {path}");
            }
        }

        // ==============================================================
        // Linux — launch / find / kill
        // ==============================================================
        private static void LinuxLaunchNow(string copyPath, string copyDir, string name, bool hidden, List<string> runner)
        {
            if (IsLinuxHeadless())
            {
                if (HasSystemd() && File.Exists(LinuxSystemdUnitPath(name)))
                {
                    var res = RunProc(new[] { "systemctl", "--user", "start", $"{name}.service" }, env: LinuxUserEnv());
                    if (res.Code == 0) { Console.WriteLine("[+] Started now via systemd user service (headless/background)."); return; }
                    Console.WriteLine($"[!] systemctl --user start failed: {Trimmed(res)}");
                }
                UnixSpawnDetached(RunnerExecLine(runner, copyPath) + " >/dev/null 2>&1", copyDir);
                Console.WriteLine("[+] Started now detached (headless fallback).");
                return;
            }

            if (hidden)
            {
                UnixSpawnDetached(RunnerExecLine(runner, copyPath) + " >/dev/null 2>&1", copyDir);
                Console.WriteLine("[+] Started now (hidden/background).");
            }
            else
            {
                var launcher = LinuxLauncherPath(name);
                if (!File.Exists(launcher)) launcher = LinuxWriteLauncher(copyPath, copyDir, name, runner);
                var cmd = FindLinuxTerminal(launcher);
                if (cmd != null)
                {
                    UnixSpawnDetached(JoinSh(cmd) + " >/dev/null 2>&1", null);
                    Console.WriteLine($"[+] Started now in a visible terminal ({cmd[0]}).");
                }
                else
                {
                    UnixSpawnDetached(RunnerExecLine(runner, copyPath), copyDir);
                    Console.WriteLine("[!] No terminal emulator found — launched detached.");
                }
            }
        }

        private static List<int> UnixFindPids(string copyPath)
        {
            var pids = new List<int>();
            var r = RunProc(new[] { "pgrep", "-f", Regex.Escape(copyPath) });
            if (r.Code == 0)
            {
                foreach (var x in (r.Out ?? "").Split((char[])null, StringSplitOptions.RemoveEmptyEntries))
                    if (int.TryParse(x, out var pid) && pid != Environment.ProcessId) pids.Add(pid);
                return pids;
            }
            // /proc fallback (Linux/Android)
            try
            {
                if (Directory.Exists("/proc"))
                    foreach (var d in Directory.GetDirectories("/proc"))
                    {
                        var pidName = BaseName(d);
                        if (pidName.Length == 0 || !pidName.All(char.IsDigit)) continue;
                        try
                        {
                            var raw = File.ReadAllText($"/proc/{pidName}/cmdline").Replace('\0', ' ');
                            if (raw.Contains(copyPath) && int.TryParse(pidName, out var pid) && pid != Environment.ProcessId)
                                pids.Add(pid);
                        }
                        catch { }
                    }
            }
            catch { }
            return pids;
        }

        private static void LinuxKill(string copyPath, string name)
        {
            if (name != null && HasSystemd())
                RunProc(new[] { "systemctl", "--user", "stop", $"{name}.service" }, env: LinuxUserEnv());
            foreach (var pid in UnixFindPids(copyPath)) UnixKillPid(pid);
        }

        // ==============================================================
        // Linux — systemd user service
        // ==============================================================
        private static bool LinuxCreateSystemdService(string copyPath, string copyDir, string name, bool hidden, List<string> runner)
        {
            CheckUbuntu2404Systemd255();
            Directory.CreateDirectory(LinuxSystemdUserDir);
            var env = LinuxUserEnv();
            if (RunProc(new[] { "systemctl", "--user", "show-environment" }, env: env).Code != 0)
            {
                Console.WriteLine("[!] systemd user manager not reachable from this shell.");
                return false;
            }
            if (!LinuxEnableLinger()) return false;

            var unitPath = LinuxSystemdUnitPath(name);
            var parts = new List<string>();
            foreach (var t in runner) parts.Add(SystemdQuote(t));
            parts.Add(SystemdQuote(copyPath));
            var execStart = string.Join(" ", parts);
            var workingDir = SystemdQuote(copyDir);
            var envLines = new StringBuilder();
            foreach (var (k, v) in DotnetEnv(runner)) envLines.Append($"Environment={k}={SystemdQuote(v)}\n");

            var content =
                "[Unit]\n" +
                $"Description=VisibleBackground {name}\n\n" +
                "[Service]\n" +
                "Type=simple\n" +
                $"WorkingDirectory={workingDir}\n" +
                envLines.ToString() +
                $"ExecStart={execStart}\n" +
                "Restart=on-failure\n" +
                "RestartSec=5\n" +
                "StandardOutput=null\n" +
                "StandardError=null\n\n" +
                "[Install]\n" +
                "WantedBy=default.target\n";
            WriteText(unitPath, content);
            Chmod644(unitPath);
            Console.WriteLine($"[+] systemd user unit written: {unitPath}");

            RunProc(new[] { "systemctl", "--user", "daemon-reload" }, env: env);
            var res = RunProc(new[] { "systemctl", "--user", "enable", $"{name}.service" }, env: env);
            if (res.Code != 0)
            {
                Console.WriteLine($"[!] systemctl --user enable failed: {Trimmed(res)}");
                return false;
            }
            Console.WriteLine($"[+] systemd user service enabled: {name}.service");
            return true;
        }

        private static void CheckUbuntu2404Systemd255()
        {
            try
            {
                const string osr = "/etc/os-release";
                if (!File.Exists(osr)) return;
                var info = new Dictionary<string, string>();
                foreach (var raw in File.ReadAllLines(osr))
                {
                    var line = raw.Trim();
                    int eq = line.IndexOf('=');
                    if (eq > 0) info[line.Substring(0, eq)] = line.Substring(eq + 1).Trim('"');
                }
                if (!info.TryGetValue("ID", out var id) || id != "ubuntu") return;
                if (!info.TryGetValue("VERSION_ID", out var vid) || !vid.StartsWith("24.04")) return;

                var res = RunProc(new[] { "systemctl", "--version" });
                if (res.Code != 0 || string.IsNullOrEmpty(res.Out)) return;
                var m = Regex.Match(res.Out, @"systemd\s+(\d+)");
                if (!m.Success || m.Groups[1].Value != "255") return;

                Console.WriteLine();
                Console.WriteLine("[!] Ubuntu 24.04 with systemd 255 detected.");
                Console.WriteLine("    A known logind regression (Launchpad bug #2163239,");
                Console.WriteLine("    systemd issue #43361) may kill the user service manager");
                Console.WriteLine("    (user@<uid>.service) on the last logout even when Linger=yes.");
                Console.WriteLine("    This can prevent the service from surviving logout or reboot.");
                Console.WriteLine("    Verify with:");
                Console.WriteLine("      loginctl show-user \"$USER\" -p Linger");
                Console.WriteLine("    If the service disappears after logout, apply the documented");
                Console.WriteLine("    workaround in /etc/systemd/logind.conf (requires root):");
                Console.WriteLine("      UserStopDelaySec=infinity");
                Console.WriteLine("      KillUserProcesses=no");
                Console.WriteLine("    Then restart logind:");
                Console.WriteLine("      sudo systemctl restart systemd-logind");
                Console.WriteLine("    Do not edit /etc/systemd/logind.conf automatically.");
                Console.WriteLine();
            }
            catch { }
        }

        private static bool LinuxEnableLinger()
        {
            var env = LinuxUserEnv();
            string user = Environment.GetEnvironmentVariable("USER")
                          ?? Environment.GetEnvironmentVariable("LOGNAME")
                          ?? Environment.UserName
                          ?? GetUid().ToString();

            if (File.Exists($"/var/lib/systemd/linger/{user}")) return true;
            var chk = RunProc(new[] { "loginctl", "show-user", user, "-p", "Linger", "--value" }, env: env);
            if (chk.Code == 0 && (chk.Out ?? "").Trim() == "yes") return true;

            var res = RunProc(new[] { "loginctl", "enable-linger" }, env: env, stdinNull: true);
            if (res.Code == 0) { Console.WriteLine("[+] loginctl enable-linger succeeded (no root needed)."); return true; }

            res = RunProc(new[] { "sudo", "-n", "loginctl", "enable-linger", user }, env: env);
            if (res.Code == 0) { Console.WriteLine($"[+] loginctl enable-linger {user} succeeded via sudo -n."); return true; }

            Console.WriteLine("[!] Could not enable linger (site policy forbids it); falling back");
            Console.WriteLine("    to cron @reboot, which needs no root.");
            return false;
        }

        private static void LinuxRemoveSystemdService(string name)
        {
            var unitPath = LinuxSystemdUnitPath(name);
            if (HasSystemd())
            {
                var env = LinuxUserEnv();
                RunProc(new[] { "systemctl", "--user", "stop", $"{name}.service" }, env: env);
                RunProc(new[] { "systemctl", "--user", "disable", $"{name}.service" }, env: env);
            }
            if (File.Exists(unitPath)) { File.Delete(unitPath); Console.WriteLine($"[+] Removed systemd user unit: {unitPath}"); }
            if (HasSystemd()) RunProc(new[] { "systemctl", "--user", "daemon-reload" }, env: LinuxUserEnv());
        }

        // ==============================================================
        // Linux — cron @reboot fallback
        // ==============================================================
        private static string CronMarker(string name) => $"# VisibleBackground {name}";

        private static bool LinuxCreateCronAutostart(string copyPath, string copyDir, string name, List<string> runner)
        {
            if (!HasBinary("crontab"))
            {
                Console.WriteLine("[!] No systemd and no crontab found on this host.");
                Console.WriteLine("    Cannot register a boot entry automatically.  To start the");
                Console.WriteLine("    program at boot, add an init/rc service for your system that");
                Console.WriteLine("    runs:");
                Console.WriteLine($"      cd {ShQuote(copyDir)} && {RunnerPlain(runner, copyPath)}");
                return false;
            }
            var marker = CronMarker(name);
            var line = $"@reboot cd {ShQuote(copyDir)} && {RunnerPlain(runner, copyPath)} > /dev/null 2>&1  {marker}";

            var existing = RunProc(new[] { "crontab", "-l" });
            var lines = new List<string>();
            if (existing.Code == 0 && !string.IsNullOrEmpty(existing.Out))
                foreach (var ln in existing.Out.Split('\n'))
                    if (!ln.Contains(marker)) lines.Add(ln);
            while (lines.Count > 0 && lines[^1].Length == 0) lines.RemoveAt(lines.Count - 1);
            lines.Add(line);
            var newCron = string.Join("\n", lines) + "\n";

            var res = RunProc(new[] { "crontab", "-" }, input: newCron);
            if (res.Code == 0)
            {
                Console.WriteLine($"[+] cron @reboot entry installed for '{name}'.");
                if (!Directory.Exists("/run/systemd/system"))
                {
                    Console.WriteLine("[!] Make sure the cron daemon is enabled and started at boot");
                    Console.WriteLine("    (e.g. `rc-update add cron` + `rc-service cron start` on");
                    Console.WriteLine("    OpenRC, or `sv enable cron` on runit).");
                }
                return true;
            }
            Console.WriteLine($"[!] Could not write crontab: {Trimmed(res)}");
            return false;
        }

        private static void LinuxRemoveCronAutostart(string name)
        {
            if (!HasBinary("crontab")) return;
            var marker = CronMarker(name);
            var existing = RunProc(new[] { "crontab", "-l" });
            if (existing.Code != 0 || string.IsNullOrEmpty(existing.Out)) return;
            var all = existing.Out.Split('\n').ToList();
            var kept = all.Where(ln => !ln.Contains(marker)).ToList();
            if (kept.Count == all.Count) return;
            while (kept.Count > 0 && kept[^1].Length == 0) kept.RemoveAt(kept.Count - 1);
            var newCron = kept.Count > 0 ? string.Join("\n", kept) + "\n" : "";
            var res = RunProc(new[] { "crontab", "-" }, input: newCron);
            if (res.Code == 0) Console.WriteLine($"[+] Removed cron @reboot entry for '{name}'.");
        }

        // ==============================================================
        // macOS — LaunchAgent
        // ==============================================================
        private static string MacPlistPath(string name) => Path.Combine(MacLaunchAgentsDir, $"com.visiblebackground.{name}.plist");
        private static string MacCommandPath(string name) => Path.Combine(BaseDir, $"run-{name}.command");

        private static string MacWriteCommand(string copyPath, string copyDir, string name, List<string> runner)
        {
            var path = MacCommandPath(name);
            var sb = new StringBuilder();
            sb.Append("#!/bin/sh\n");
            sb.Append($"cd {ShQuote(copyDir)} || exit 1\n");
            foreach (var (k, v) in DotnetEnv(runner)) sb.Append($"export {k}={ShQuote(v)}\n");
            sb.Append(RunnerExecLine(runner, copyPath)).Append("\n");
            WriteText(path, sb.ToString());
            ChmodExec(path);
            return path;
        }

        private static string XmlEscape(string s) =>
            (s ?? "").Replace("&", "&amp;").Replace("<", "&lt;").Replace(">", "&gt;");

        private static void MacWritePlist(string plistPath, string label, List<string> programArgs,
            string workingDir, bool hidden, string outLog, string errLog, List<(string, string)> env = null)
        {
            var sb = new StringBuilder();
            sb.Append("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n");
            sb.Append("<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n");
            sb.Append("<plist version=\"1.0\">\n<dict>\n");
            sb.Append($"  <key>Label</key>\n  <string>{XmlEscape(label)}</string>\n");
            sb.Append("  <key>ProgramArguments</key>\n  <array>\n");
            foreach (var a in programArgs) sb.Append($"    <string>{XmlEscape(a)}</string>\n");
            sb.Append("  </array>\n");
            if (env != null && env.Count > 0)
            {
                sb.Append("  <key>EnvironmentVariables</key>\n  <dict>\n");
                foreach (var (k, v) in env) sb.Append($"    <key>{XmlEscape(k)}</key>\n    <string>{XmlEscape(v)}</string>\n");
                sb.Append("  </dict>\n");
            }
            sb.Append($"  <key>WorkingDirectory</key>\n  <string>{XmlEscape(workingDir)}</string>\n");
            sb.Append("  <key>RunAtLoad</key>\n  <true/>\n");
            sb.Append("  <key>KeepAlive</key>\n  <false/>\n");
            sb.Append($"  <key>ProcessType</key>\n  <string>{(hidden ? "Background" : "Interactive")}</string>\n");
            sb.Append($"  <key>StandardOutPath</key>\n  <string>{XmlEscape(outLog)}</string>\n");
            sb.Append($"  <key>StandardErrorPath</key>\n  <string>{XmlEscape(errLog)}</string>\n");
            sb.Append("</dict>\n</plist>\n");

            var tmp = plistPath + ".tmp";
            WriteText(tmp, sb.ToString());
            Chmod644(tmp);
            File.Move(tmp, plistPath, true);
        }

        private static bool MacosCreateAutostart(string copyPath, string copyDir, string name, bool hidden, List<string> runner)
        {
            var label = $"com.visiblebackground.{name}";
            List<string> programArgs;
            if (hidden)
            {
                programArgs = new List<string>();
                programArgs.AddRange(runner);
                programArgs.Add(copyPath);
            }
            else
            {
                programArgs = new List<string> { "/usr/bin/open", "-a", "Terminal", MacWriteCommand(copyPath, copyDir, name, runner) };
            }

            Directory.CreateDirectory(BaseDir);
            Directory.CreateDirectory(MacLaunchAgentsDir);
            var plistPath = MacPlistPath(name);
            MacWritePlist(plistPath, label, programArgs, copyDir, hidden,
                Path.Combine(BaseDir, $"{name}.out.log"), Path.Combine(BaseDir, $"{name}.err.log"), DotnetEnv(runner));
            Console.WriteLine($"[+] macOS LaunchAgent written: {plistPath}");

            var uid = GetUid();
            var domain = $"gui/{uid}";
            RunProc(new[] { "launchctl", "bootout", $"{domain}/{label}" });
            RunProc(new[] { "launchctl", "enable", $"{domain}/{label}" });

            Proc res = null;
            for (int i = 0; i < 3; i++)
            {
                res = RunProc(new[] { "launchctl", "bootstrap", domain, plistPath });
                if (res.Code == 0)
                {
                    Console.WriteLine("[+] LaunchAgent loaded (it will also start at every login).");
                    Console.WriteLine("[!] macOS may show a 'Background Items Added' notice; that is");
                    Console.WriteLine("    expected.  Keep it ON in System Settings > General > Login Items.");
                    return true;
                }
                Thread.Sleep(500);
            }
            Console.WriteLine($"[!] launchctl bootstrap failed: {Trimmed(res)}; trying legacy launchctl load -w");
            res = RunProc(new[] { "launchctl", "load", "-w", plistPath });
            var err = (res.Err ?? "").ToLowerInvariant();
            if (res.Code == 0 && !err.Contains("failed") && !err.Contains("error")) return true;

            Console.WriteLine("[!] Could not load the LaunchAgent now (common over SSH, where no");
            Console.WriteLine("    GUI session exists).  It will still load at your next login.");
            return false;
        }

        private static void MacosRemoveAutostart(string name)
        {
            var plistPath = MacPlistPath(name);
            var uid = GetUid();
            var label = $"com.visiblebackground.{name}";
            var res = RunProc(new[] { "launchctl", "bootout", $"gui/{uid}/{label}" });
            if (res.Code != 0 && File.Exists(plistPath))
                RunProc(new[] { "launchctl", "unload", "-w", plistPath });

            if (File.Exists(plistPath)) { File.Delete(plistPath); Console.WriteLine($"[+] Removed macOS LaunchAgent: {plistPath}"); }
            var cmd = MacCommandPath(name);
            if (File.Exists(cmd)) { try { File.Delete(cmd); } catch { } }
            foreach (var suffix in new[] { ".out.log", ".err.log" })
            {
                var log = Path.Combine(BaseDir, $"{name}{suffix}");
                if (File.Exists(log)) { try { File.Delete(log); } catch { } }
            }
        }

        private static void MacosLaunchNow(string copyPath, string copyDir, string name, bool hidden, List<string> runner)
        {
            if (hidden)
            {
                UnixSpawnDetached(RunnerExecLine(runner, copyPath) + " >/dev/null 2>&1", copyDir);
                Console.WriteLine("[+] Started now (hidden/background).");
            }
            else
            {
                var cmd = MacWriteCommand(copyPath, copyDir, name, runner);
                RunProc(new[] { "/usr/bin/open", "-a", "Terminal", cmd });
                Console.WriteLine("[+] Started now in Terminal (visible).");
            }
        }

        private static void MacosKill(string copyPath)
        {
            foreach (var pid in UnixFindPids(copyPath)) UnixKillPid(pid);
        }

        // ==============================================================
        // Android — Termux:Boot
        // ==============================================================
        private static int AndroidSdk()
        {
            var r = RunProc(new[] { "getprop", "ro.build.version.sdk" });
            return int.TryParse((r.Out ?? "").Trim(), out var v) ? v : 0;
        }

        private static bool _androidWarned;
        private static void Android15Warning()
        {
            if (_androidWarned || AndroidSdk() < 35) return;
            _androidWarned = true;
            Console.WriteLine();
            Console.WriteLine("[!] Android 15+ known issue:");
            Console.WriteLine("    Android 15 and newer may still kill Termux background processes even with");
            Console.WriteLine("    termux-wake-lock active and battery optimisation disabled.  This is");
            Console.WriteLine("    an upstream Android limitation, tracked in Termux issue #5150.");
            Console.WriteLine("    If your device exposes it, enable:");
            Console.WriteLine("      Settings → System → Developer options →");
            Console.WriteLine("      Disable child process restrictions");
            Console.WriteLine("    Do not expect the same background reliability as on Android 14 or older.");
            Console.WriteLine();
        }

        private static void AndroidCreateAutostart(string copyPath, string copyDir, string name, bool hidden, List<string> runner)
        {
            if (!IsTermux())
            {
                Console.WriteLine("[-] Android support requires Termux.");
                Console.WriteLine("    Install Termux from F-Droid, then install Termux:Boot,");
                Console.WriteLine("    and run this script inside Termux.");
                throw new ExitException(1);
            }
            Android15Warning();
            Directory.CreateDirectory(TermuxBootDir);
            var bootScript = Path.Combine(TermuxBootDir, $"start-{name}.sh");
            string content;
            if (hidden)
            {
                var inner = RunnerPlain(runner, copyPath);
                content = "#!/data/data/com.termux/files/usr/bin/sh\n" +
                          "termux-wake-lock\n" +
                          $"cd {ShQuote(copyDir)} || exit 1\n" +
                          $"nohup {inner} > /dev/null 2>&1 &\n";
            }
            else
            {
                var pathArg = runner[0];
                var argList = new List<string>();
                for (int i = 1; i < runner.Count; i++) argList.Add(runner[i]);
                argList.Add(copyPath);
                var esaArg = string.Join(",", argList.Select(a => a.Replace(",", "\\,")));
                var extras =
                    "-n com.termux/com.termux.app.RunCommandService " +
                    "-a com.termux.RUN_COMMAND " +
                    $"--es com.termux.RUN_COMMAND_PATH {ShQuote(pathArg)} " +
                    $"--esa com.termux.RUN_COMMAND_ARGUMENTS {ShQuote(esaArg)} " +
                    $"--es com.termux.RUN_COMMAND_WORKDIR {ShQuote(copyDir)} " +
                    "--ez com.termux.RUN_COMMAND_BACKGROUND false";
                content = "#!/data/data/com.termux/files/usr/bin/sh\n" +
                          "termux-wake-lock\n" +
                          "sleep 5\n" +
                          $"am startservice --user 0 {extras} >/dev/null 2>&1 || \\\n" +
                          $"am start-foreground-service --user 0 {extras} >/dev/null 2>&1 || \\\n" +
                          $"( cd {ShQuote(copyDir)} && {RunnerPlain(runner, copyPath)} >/dev/null 2>&1 & )\n";
            }
            WriteText(bootScript, content);
            ChmodExec(bootScript);
            Console.WriteLine($"[+] Android Termux boot script written: {bootScript}");
            Console.WriteLine("[!] Make sure Termux:Boot is installed and has been opened once.");
            Console.WriteLine("[!] Disable battery optimisation for Termux and Termux:Boot.");
            Console.WriteLine("[!] Grant Termux the 'Draw over other apps' permission on Android 10+");
            if (!hidden)
            {
                Console.WriteLine("[!] Visible mode uses the Termux RUN_COMMAND API, which needs");
                Console.WriteLine("    `allow-external-apps=true` in ~/.termux/termux.properties.");
                Console.WriteLine("    If the window does not open at boot, use --hide instead.");
            }
        }

        private static void AndroidRemoveAutostart(string name)
        {
            var bootScript = Path.Combine(TermuxBootDir, $"start-{name}.sh");
            if (File.Exists(bootScript)) { File.Delete(bootScript); Console.WriteLine($"[+] Removed Termux boot script: {bootScript}"); }
        }

        private static void AndroidLaunchNow(string copyPath, string copyDir, bool hidden, List<string> runner)
        {
            if (!IsTermux()) { Console.WriteLine("[-] Cannot launch now: not running inside Termux."); return; }
            Android15Warning();
            if (hidden)
            {
                UnixSpawnDetached(RunnerExecLine(runner, copyPath) + " >/dev/null 2>&1", copyDir);
                Console.WriteLine("[+] Started now (hidden/background).");
                return;
            }
            var pathArg = runner[0];
            var argList = new List<string>();
            for (int i = 1; i < runner.Count; i++) argList.Add(runner[i]);
            argList.Add(copyPath);
            var esaArg = string.Join(",", argList.Select(a => a.Replace(",", "\\,")));
            var extras = new List<string>
            {
                "-n", "com.termux/com.termux.app.RunCommandService",
                "-a", "com.termux.RUN_COMMAND",
                "--es", "com.termux.RUN_COMMAND_PATH", pathArg,
                "--esa", "com.termux.RUN_COMMAND_ARGUMENTS", esaArg,
                "--es", "com.termux.RUN_COMMAND_WORKDIR", copyDir,
                "--ez", "com.termux.RUN_COMMAND_BACKGROUND", "false",
            };
            bool ok = false;
            foreach (var verb in new[] { "startservice", "start-foreground-service" })
            {
                var cmd = new List<string> { "am", verb, "--user", "0" };
                cmd.AddRange(extras);
                var res = RunProc(cmd, timeoutSec: 20);
                var outp = ((res.Out ?? "") + (res.Err ?? "")).ToLowerInvariant();
                if (res.Code == 0 && !outp.Contains("error") && !outp.Contains("exception")) { ok = true; break; }
            }
            if (ok) Console.WriteLine("[+] Started now in a new visible Termux session.");
            else
            {
                UnixSpawnDetached(RunnerExecLine(runner, copyPath) + " >/dev/null 2>&1", copyDir);
                Console.WriteLine("[!] Could not open a Termux session (is allow-external-apps=true");
                Console.WriteLine("    set in ~/.termux/termux.properties?); started in background.");
            }
        }

        private static void AndroidKill(string copyPath)
        {
            foreach (var pid in UnixFindPids(copyPath)) UnixKillPid(pid);
        }

        // ==============================================================
        // iOS — unsupported
        // ==============================================================
        private static void IosUnsupported()
        {
            Console.WriteLine("[-] iOS does not allow arbitrary background scripts to run persistently.");
            Console.WriteLine();
            Console.WriteLine("    Apple's own documentation establishes the following platform contract:");
            Console.WriteLine();
            Console.WriteLine("    • iOS suspends third-party apps shortly after they move to the background.");
            Console.WriteLine("      Suspension prevents the process from executing any code.");
            Console.WriteLine("    • There is no general-purpose mechanism for running code continuously,");
            Console.WriteLine("      at a specific time, at a guaranteed interval, or in response to a");
            Console.WriteLine("      network/IPC request in the background.");
            Console.WriteLine("    • BGAppRefreshTask is opportunistic and capped at roughly 30 seconds.");
            Console.WriteLine("      BGProcessingTask is also opportunistic and typically runs only when");
            Console.WriteLine("      the device is idle and plugged in, for about 60 seconds.");
            Console.WriteLine("    • Scheduling is decided by dasd (Duet Activity Scheduler Daemon), not");
            Console.WriteLine("      by the app. Low battery, Low Power Mode, thermal pressure, and low");
            Console.WriteLine("      engagement can all cause a submitted task to be dropped entirely.");
            Console.WriteLine("    • Once the user force-quits an app, iOS will not restart it in the");
            Console.WriteLine("      background. No third-party library can bypass this.");
            Console.WriteLine("    • Jailbroken or MDM-supervised devices do not change this contract for");
            Console.WriteLine("      ordinary user scripts.");
            Console.WriteLine();
            Console.WriteLine("    The only Apple-sanctioned best-effort alternative is a user-configured");
            Console.WriteLine("    Shortcuts automation triggered by \"Device Unlocked\" or \"Wi-Fi connected\"");
            Console.WriteLine("    after boot.  This tool cannot install that automation for you.");
            throw new ExitException(1);
        }

        // ==============================================================
        // Windows — Run key + WSH launcher (no admin)
        // ==============================================================
        private static string WindowsTaskName(string name) => $"VisibleBackground_{name}";
        private static string WindowsLauncherPath(string name) => NormPath(Path.Combine(BaseDir, $"_vb_launch_{name}.js"));
        private static string WindowsLegacyPywPath(string name) => NormPath(Path.Combine(BaseDir, $"_vb_launch_{name}.pyw"));
        private static string WindowsVbsPath(string name) => NormPath(Path.Combine(BaseDir, $"VisibleBackground_{name}.vbs"));
        private static string WindowsCmdWrapperPath(string name) => NormPath(Path.Combine(BaseDir, $"VisibleBackground_{name}_launcher.cmd"));

        private static string WindowsPythonw(string py)
        {
            var d = Path.GetDirectoryName(py) ?? "";
            var b = Path.GetFileName(py);
            var low = b.ToLowerInvariant();
            if (low.StartsWith("pythonw")) return py;
            if (low.StartsWith("python") && low.EndsWith(".exe"))
            {
                var c = Path.Combine(d, "pythonw" + b.Substring(6));
                if (File.Exists(c)) return c;
                c = Path.Combine(d, "pythonw.exe");
                if (File.Exists(c)) return c;
            }
            return py;
        }

        private static string WindowsPython(string py)
        {
            var d = Path.GetDirectoryName(py) ?? "";
            var b = Path.GetFileName(py);
            if (b.ToLowerInvariant().StartsWith("pythonw"))
            {
                var c = Path.Combine(d, "python" + b.Substring(7));
                if (File.Exists(c)) return c;
                c = Path.Combine(d, "python.exe");
                if (File.Exists(c)) return c;
            }
            return py;
        }

        private static string WinQuote(string s)
        {
            if (s.Length > 0 && !s.Any(ch => char.IsWhiteSpace(ch) || ch == '"')) return s;
            return "\"" + s.Replace("\"", "\\\"") + "\"";
        }

        private static string JsStr(string s)
        {
            var sb = new StringBuilder("\"");
            foreach (var ch in s)
            {
                switch (ch)
                {
                    case '\\': sb.Append("\\\\"); break;
                    case '"': sb.Append("\\\""); break;
                    case '\n': sb.Append("\\n"); break;
                    case '\r': sb.Append("\\r"); break;
                    case '\t': sb.Append("\\t"); break;
                    default: sb.Append(ch); break;
                }
            }
            sb.Append('"');
            return sb.ToString();
        }

        private static string WindowsCommandLine(List<string> runner, string copyPath)
        {
            var sb = new StringBuilder();
            for (int i = 0; i < runner.Count; i++) { if (i > 0) sb.Append(' '); sb.Append(WinQuote(runner[i])); }
            sb.Append(' ').Append(WinQuote(copyPath));
            return sb.ToString();
        }

        private static string WriteWindowsLauncher(string name, string copyPath, string copyDir, bool hidden, List<string> runner)
        {
            int style = hidden ? 0 : 1; // WScript.Shell.Run window style: 0 hidden, 1 normal
            var cmd = WindowsCommandLine(runner, copyPath);
            var sb = new StringBuilder();
            sb.Append("// Auto-generated by auto.cs - starts the installed program at logon (WSH/JScript).\n");
            sb.Append("// Invoked via: wscript.exe //B //Nologo //E:jscript \"<this file>\"\n");
            sb.Append("var CMD = ").Append(JsStr(cmd)).Append(";\n");
            sb.Append("var CWD = ").Append(JsStr(copyDir)).Append(";\n");
            sb.Append("var STYLE = ").Append(style).Append(";\n");
            sb.Append("try {\n");
            sb.Append("  var sh = new ActiveXObject(\"WScript.Shell\");\n");
            var denv = DotnetEnv(runner);
            if (denv.Count > 0)
            {
                sb.Append("  try { var env = sh.Environment(\"PROCESS\");\n");
                foreach (var (k, v) in denv) sb.Append("    env(").Append(JsStr(k)).Append(") = ").Append(JsStr(v)).Append(";\n");
                sb.Append("  } catch (e2) {}\n");
            }
            sb.Append("  try { var fso = new ActiveXObject(\"Scripting.FileSystemObject\"); if (CWD && fso.FolderExists(CWD)) { sh.CurrentDirectory = CWD; } } catch (e1) {}\n");
            sb.Append("  sh.Run(CMD, STYLE, false);\n");
            sb.Append("} catch (e) {}\n");
            var path = WindowsLauncherPath(name);
            WriteText(path, sb.ToString());
            return path;
        }

        private static string WindowsRunCommand(string name) =>
            $"wscript.exe //B //Nologo //E:jscript \"{WindowsLauncherPath(name)}\"";

        // ---- registry P/Invoke (advapi32) ----------------------------
        private static readonly IntPtr HKCU = new IntPtr(unchecked((int)0x80000001));
        private const int KEY_SET_VALUE = 0x0002;
        private const int REG_SZ = 1;
        [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern int RegCreateKeyExW(IntPtr hKey, string lpSubKey, int Reserved, string lpClass,
            int dwOptions, int samDesired, IntPtr lpSecurityAttributes, out IntPtr phkResult, out int lpdwDisposition);
        [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern int RegOpenKeyExW(IntPtr hKey, string lpSubKey, int ulOptions, int samDesired, out IntPtr phkResult);
        [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern int RegSetValueExW(IntPtr hKey, string lpValueName, int Reserved, int dwType, byte[] lpData, int cbData);
        [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern int RegDeleteValueW(IntPtr hKey, string lpValueName);
        [DllImport("advapi32.dll")]
        private static extern int RegCloseKey(IntPtr hKey);

        private static bool RegWriteHkcuSz(string subkey, string valueName, string value)
        {
            int rc = RegCreateKeyExW(HKCU, subkey, 0, null, 0, KEY_SET_VALUE, IntPtr.Zero, out var h, out _);
            if (rc != 0) return false;
            try
            {
                var data = Encoding.Unicode.GetBytes(value + "\0");
                return RegSetValueExW(h, valueName, 0, REG_SZ, data, data.Length) == 0;
            }
            finally { RegCloseKey(h); }
        }

        private static bool RegDeleteHkcuValue(string subkey, string valueName)
        {
            int rc = RegOpenKeyExW(HKCU, subkey, 0, KEY_SET_VALUE, out var h);
            if (rc != 0) return false;
            try { return RegDeleteValueW(h, valueName) == 0; }
            finally { RegCloseKey(h); }
        }

        private static bool WindowsCreateAutostart(string copyPath, string copyDir, string name, bool hidden, List<string> runner)
        {
            WriteWindowsLauncher(name, copyPath, copyDir, hidden, runner);
            var valueName = WindowsTaskName(name);
            var command = WindowsRunCommand(name);
            if (!RegWriteHkcuSz(WinRunKey, valueName, command))
            {
                Console.WriteLine("[-] Could not write the Run registry value.");
                return false;
            }
            RegDeleteHkcuValue(WinApprovedKey, valueName); // clear any "disabled" flag

            WindowsRemoveLegacyTask(name, quiet: true);
            WindowsRemoveVbsLauncher(name);
            WindowsRemoveCmdWrapper(name);
            TryRemoveFile(WindowsLegacyPywPath(name), "old Python launcher");

            Console.WriteLine($"[+] Windows autostart registered (no admin needed): HKCU\\{WinRunKey}\\{valueName}");
            return true;
        }

        private static void WindowsRemoveLegacyTask(string name, bool quiet = false)
        {
            var taskName = WindowsTaskName(name);
            var q = RunProc(new[] { "schtasks", "/Query", "/TN", taskName });
            if (q.Code != 0) return;
            var res = RunProc(new[] { "schtasks", "/Delete", "/TN", taskName, "/F" });
            if (res.Code == 0) Console.WriteLine($"[+] Removed old scheduled task: {taskName}");
            else if (!quiet)
            {
                Console.WriteLine($"[!] An old scheduled task '{taskName}' from a previous version");
                Console.WriteLine("    exists but needs an elevated prompt to delete.  Remove it once");
                Console.WriteLine($"    with:  schtasks /Delete /TN {taskName} /F   (as Administrator)");
            }
        }

        private static void WindowsRemoveAutostart(string name)
        {
            var valueName = WindowsTaskName(name);
            if (RegDeleteHkcuValue(WinRunKey, valueName))
                Console.WriteLine($"[+] Removed Windows autostart entry: {valueName}");
            RegDeleteHkcuValue(WinApprovedKey, valueName);
            WindowsRemoveLegacyTask(name);
            TryRemoveFile(WindowsLauncherPath(name), "launcher");
            TryRemoveFile(WindowsLegacyPywPath(name), "old Python launcher");
        }

        private static void WindowsLaunchNow(string copyPath, string copyDir, string name)
        {
            var launcher = WindowsLauncherPath(name);
            try
            {
                var psi = new ProcessStartInfo
                {
                    FileName = "wscript.exe",
                    UseShellExecute = false,
                    WorkingDirectory = Directory.Exists(copyDir) ? copyDir : "",
                };
                psi.ArgumentList.Add("//B");
                psi.ArgumentList.Add("//Nologo");
                psi.ArgumentList.Add("//E:jscript");
                psi.ArgumentList.Add(launcher);
                Process.Start(psi);
                Console.WriteLine("[+] Started now via the logon launcher.");
            }
            catch (Exception)
            {
                Console.WriteLine("[-] Could not start the program now; it will start at next logon.");
            }
        }

        private static List<int> WindowsFindPids(string copyPath)
        {
            var needle = NormPath(copyPath).ToLowerInvariant().Replace("'", "''");
            var ps =
                $"$n = '{needle}'; " +
                "Get-CimInstance Win32_Process | " +
                "Where-Object { " +
                "$_.CommandLine -and " +
                "$_.CommandLine.ToLower().Contains($n) " +
                $"-and $_.ProcessId -ne {Environment.ProcessId} " +
                "} | Select-Object -ExpandProperty ProcessId";
            var res = RunProc(new[] { "powershell", "-NoProfile", "-NonInteractive", "-Command", ps });
            var pids = new List<int>();
            foreach (var x in (res.Out ?? "").Split((char[])null, StringSplitOptions.RemoveEmptyEntries))
                if (int.TryParse(x.Trim(), out var pid)) pids.Add(pid);
            return pids;
        }

        private static void WindowsKill(string copyPath)
        {
            foreach (var pid in WindowsFindPids(copyPath))
            {
                RunProc(new[] { "taskkill", "/F", "/T", "/PID", pid.ToString() });
                Console.WriteLine($"[+] Killed PID {pid}");
            }
        }

        private static void WindowsRemoveVbsLauncher(string name)
        {
            var vbs = WindowsVbsPath(name);
            if (File.Exists(vbs))
            {
                try { File.Delete(vbs); Console.WriteLine($"[+] Removed old VBS launcher: {vbs}"); }
                catch (Exception e) { Console.WriteLine($"[-] Could not remove VBS launcher: {e.Message}"); }
            }
        }

        private static void WindowsRemoveCmdWrapper(string name)
        {
            var wrapper = WindowsCmdWrapperPath(name);
            if (File.Exists(wrapper))
            {
                try { File.Delete(wrapper); Console.WriteLine($"[+] Removed old CMD wrapper: {wrapper}"); }
                catch (Exception e) { Console.WriteLine($"[-] Could not remove CMD wrapper: {e.Message}"); }
            }
        }

        // ==============================================================
        // Stop helpers
        // ==============================================================
        private static HashSet<string> ResolveCopyPaths(string targetScript, string name, Dictionary<string, Entry> state)
        {
            var paths = new HashSet<string>();
            if (state.TryGetValue(name, out var e) && !string.IsNullOrEmpty(e.copy))
                paths.Add(NormPath(e.copy));
            var baseN = BaseName((targetScript ?? "").Replace("\\", "/"));
            if (!string.IsNullOrEmpty(baseN)) paths.Add(NormPath(Path.Combine(BaseDir, baseN)));
            foreach (var ext in KnownExts) paths.Add(NormPath(Path.Combine(BaseDir, name + ext)));
            if (Directory.Exists(BaseDir))
                foreach (var f in Directory.GetFileSystemEntries(BaseDir))
                {
                    var fn = BaseName(f);
                    if (SanitizeName(fn) == name) paths.Add(NormPath(Path.Combine(BaseDir, fn)));
                }
            return paths;
        }

        private static bool RemoveWithRetry(string path, int attempts = 6, int delayMs = 500)
        {
            for (int i = 0; i < attempts; i++)
            {
                try { File.Delete(path); Console.WriteLine($"[+] Removed copy: {path}"); return true; }
                catch (FileNotFoundException) { return true; }
                catch (DirectoryNotFoundException) { return true; }
                catch (UnauthorizedAccessException) { if (i < attempts - 1) Thread.Sleep(delayMs); }
                catch (IOException) { if (i < attempts - 1) Thread.Sleep(delayMs); }
                catch (Exception ex) { Console.WriteLine($"[-] Could not remove copy {path}: {ex.Message}"); return false; }
            }
            Console.WriteLine($"[-] Could not remove copy {path} (still locked)");
            return false;
        }

        // ==============================================================
        // Install / Uninstall
        // ==============================================================
        private static void CopyScript(string target, string copyPath)
        {
            try
            {
                if (NormPath(target) == NormPath(copyPath)) return; // same file
                File.Copy(target, copyPath, true);
                Console.WriteLine($"[+] Copied '{target}' -> '{copyPath}'");
            }
            catch (UnauthorizedAccessException)
            {
                for (int i = 0; i < 6; i++)
                {
                    Thread.Sleep(500);
                    try { File.Copy(target, copyPath, true); Console.WriteLine($"[+] Copied '{target}' -> '{copyPath}'"); return; }
                    catch (UnauthorizedAccessException) { }
                    catch (IOException) { }
                }
                Die($"[-] Could not overwrite {copyPath}: file is locked.");
            }
            catch (IOException)
            {
                for (int i = 0; i < 6; i++)
                {
                    Thread.Sleep(500);
                    try { File.Copy(target, copyPath, true); Console.WriteLine($"[+] Copied '{target}' -> '{copyPath}'"); return; }
                    catch (IOException) { }
                }
                Die($"[-] Could not overwrite {copyPath}: file is locked.");
            }
        }

        private static void KillExisting(string name, string copyPath)
        {
            try
            {
                if (IsAndroid()) AndroidKill(copyPath);
                else if (IsMacOS()) MacosKill(copyPath);
                else if (IsWindows()) WindowsKill(copyPath);
                else LinuxKill(copyPath, name);
            }
            catch { }
        }

        private static void Install(string targetScript, bool hidden, bool hideDir)
        {
            if (IsIOS()) IosUnsupported();
            if (!File.Exists(targetScript)) Die($"[-] File not found: {targetScript}");
            targetScript = Path.GetFullPath(targetScript);

            var lang = DetectLanguage(targetScript);
            if (lang == "unknown")
                Die($"[-] Unsupported file type: {BaseName(targetScript)} (supported: .py .js .ts .cs .sh).");

            EnsureDirectory();
            if (hideDir) HideInstallDir();

            var name = SanitizeName(targetScript);
            var system = SysNameLower();
            var prevState = LoadState();
            if (prevState.TryGetValue(name, out var prev) && !string.IsNullOrEmpty(prev.copy))
                KillExisting(name, prev.copy);

            // Resolve the runtime up front so we fail clearly before copying.
            var runner = RequireRunner(targetScript, hidden);

            var copyPath = NormPath(Path.Combine(BaseDir, BaseName(targetScript)));
            var copyDir = BaseDir;
            string plat, mechanism;
            bool effectiveHidden;

            if (IsAndroid())
            {
                if (!IsTermux())
                {
                    Console.WriteLine("[-] Android support requires Termux.  Install Termux from F-Droid");
                    Console.WriteLine("    and run this script inside Termux.");
                    throw new ExitException(1);
                }
                KillExisting(name, copyPath);
                CopyScript(targetScript, copyPath);
                AndroidCreateAutostart(copyPath, copyDir, name, hidden, runner);
                AndroidLaunchNow(copyPath, copyDir, hidden, runner);
                plat = "android"; mechanism = "termux-boot"; effectiveHidden = hidden;
            }
            else if (IsMacOS())
            {
                KillExisting(name, copyPath);
                CopyScript(targetScript, copyPath);
                if (!MacosCreateAutostart(copyPath, copyDir, name, hidden, runner))
                    MacosLaunchNow(copyPath, copyDir, name, hidden, runner);
                plat = "macos"; mechanism = "launchagent"; effectiveHidden = hidden;
            }
            else if (IsWindows())
            {
                KillExisting(name, copyPath);
                CopyScript(targetScript, copyPath);
                if (!WindowsCreateAutostart(copyPath, copyDir, name, hidden, runner))
                    Die("[-] Could not create autostart entry.");
                WindowsLaunchNow(copyPath, copyDir, name);
                plat = "windows"; mechanism = "run-key"; effectiveHidden = hidden;
            }
            else if (IsLinux())
            {
                if (RunningAsRootViaSudo())
                {
                    Console.WriteLine("[!] Running under sudo: the autostart entry will be created for");
                    Console.WriteLine("    root, not for your desktop user.  Run without sudo instead.");
                }
                if (Release().ToLowerInvariant().Contains("microsoft"))
                {
                    Console.WriteLine("[!] WSL detected: Linux autostart inside WSL only runs when the");
                    Console.WriteLine("    WSL distro itself starts, not when Windows boots.  For a");
                    Console.WriteLine("    program that must start with Windows, install it on Windows");
                    Console.WriteLine("    (run auto.cs there) instead.");
                }
                KillExisting(name, copyPath);
                CopyScript(targetScript, copyPath);

                string linuxMech;
                if (IsLinuxHeadless())
                {
                    if (!hidden)
                    {
                        Console.WriteLine("[!] Headless Linux detected: no DISPLAY/WAYLAND_DISPLAY.");
                        Console.WriteLine("    Visible terminal mode is unavailable; installing in");
                        Console.WriteLine("    background mode.");
                    }
                    if (HasSystemd() && LinuxCreateSystemdService(copyPath, copyDir, name, true, runner))
                    {
                        linuxMech = "systemd";
                        LinuxRemoveCronAutostart(name);
                    }
                    else
                    {
                        LinuxRemoveSystemdService(name);
                        Console.WriteLine("[!] Using a cron @reboot entry for boot persistence.");
                        LinuxCreateCronAutostart(copyPath, copyDir, name, runner);
                        linuxMech = "cron";
                    }
                    LinuxLaunchNow(copyPath, copyDir, name, true, runner);
                }
                else if (IsHyprland() && HyprlandCreateAutostart(copyPath, copyDir, name, hidden, runner))
                {
                    LinuxRemoveAutostart(name);
                    linuxMech = "hyprland";
                    LinuxLaunchNow(copyPath, copyDir, name, hidden, runner);
                }
                else
                {
                    if (IsWlrootsWayland())
                    {
                        Console.WriteLine("[!] This Wayland compositor may not run XDG autostart");
                        Console.WriteLine("    entries.  If the program does not start after login,");
                        Console.WriteLine($"    add `{LinuxLauncherPath(name)}` to your compositor's");
                        Console.WriteLine("    startup config (e.g. `exec` in sway's config).");
                    }
                    LinuxCreateAutostart(copyPath, copyDir, name, hidden, runner);
                    linuxMech = "xdg";
                    LinuxLaunchNow(copyPath, copyDir, name, hidden, runner);
                }
                plat = "linux"; mechanism = linuxMech; effectiveHidden = IsLinuxHeadless() ? true : hidden;
            }
            else
            {
                Die($"[-] Unsupported OS: {(string.IsNullOrEmpty(system) ? "unknown" : system)}");
                return;
            }

            var state = LoadState();
            state[name] = new Entry
            {
                source = Path.GetFullPath(targetScript),
                copy = copyPath,
                hidden = effectiveHidden,
                platform = plat,
                mechanism = mechanism,
                language = lang,
            };
            SaveState(state);
            Console.WriteLine($"[+] '{name}' installed. It will auto-start on every reboot/login.");
        }

        private static void Uninstall(string targetScript)
        {
            if (IsIOS()) IosUnsupported();
            EnsureDirectory();
            var name = SanitizeName(targetScript);
            var state = LoadState();

            var copyPaths = ResolveCopyPaths(targetScript, name, state);
            Console.WriteLine($"[*] Resolved {copyPaths.Count} candidate path(s) for '{name}'.");

            if (IsAndroid()) foreach (var cp in copyPaths) AndroidKill(cp);
            else if (IsMacOS()) foreach (var cp in copyPaths) MacosKill(cp);
            else if (IsWindows()) foreach (var cp in copyPaths) WindowsKill(cp);
            else foreach (var cp in copyPaths) LinuxKill(cp, name);

            if (IsAndroid()) AndroidRemoveAutostart(name);
            else if (IsMacOS()) MacosRemoveAutostart(name);
            else if (IsWindows())
            {
                WindowsRemoveAutostart(name);
                WindowsRemoveVbsLauncher(name);
                WindowsRemoveCmdWrapper(name);
            }
            else
            {
                LinuxRemoveAutostart(name);
                LinuxRemoveSystemdService(name);
                LinuxRemoveCronAutostart(name);
                HyprlandRemoveAutostart(name);
                LinuxRemoveLauncher(name);
            }

            foreach (var cp in copyPaths) if (File.Exists(cp)) RemoveWithRetry(cp);

            if (state.Remove(name)) SaveState(state);
            Console.WriteLine($"[+] '{name}' stopped permanently.");
        }
    }
}
