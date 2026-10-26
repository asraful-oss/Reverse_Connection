// file-share.cs - two-way file and folder sharing over any connection
// ---------------------------------------------------------------------
// The C# twin of file-share.py. Both talk the same protocol, so one user can run
// file-share.py (Linux, macOS, Android...) and the other file-share.cs (Windows...).
//
// Windows, nothing to install:   double-click file-share.bat
//   (it compiles this file once with the C# compiler built into Windows, then runs it)
// Any OS with the .NET 10 SDK:   dotnet run file-share.cs
// Mono (Linux/macOS):            mcs file-share.cs && mono file-share.exe
//
// One program does everything: it listens for the other user AND connects to
// them, so it does not matter who started first or who runs the tunnel.
//   * Same Wi-Fi / LAN: one user puts the other user's LAN IP in PEER_ADDRESS.
//   * Internet: one user runs Pinggy, ngrok or Cloudflare on port 4444; the other
//     user pastes the address the tunnel printed into PEER_ADDRESS.
// The connection type (TCP, TLS, WebSocket over HTTP/HTTPS, or HTTP polling) is
// detected automatically. Interrupted transfers resume where they stopped.
//
// Commands once connected:
//   upload <files/folders>    offer one or more files/folders (upload a.mp4 "My Folder" *.jpg)
//   Ctrl+Z   (or cancel)      pick which transfers to stop: 2, or 2 3 4, or all, then y/n
//   cancel 2 3                stop transfers 2 and 3 (numbers shown by status)
//   connect <address>         switch to a new address (e.g. a new tunnel URL)
//   status                    connection and every transfer with its number
//   help                      show the commands
//   exit     (or Ctrl+C)      quit
//
// Written in C# 5 on purpose: it compiles with the csc.exe that ships with every
// Windows 10/11 (.NET Framework 4.x), with Mono, and with any newer .NET SDK.
#pragma warning disable

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Security;
using System.Net.Sockets;
using System.Reflection;
using System.Security.Authentication;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;

namespace FileShareApp
{
    static class Settings
    {
        // ============================ EDIT THIS ============================
        // The OTHER user's address. Leave "" if the other user connects to you
        // (for example, you are the one running the Pinggy/ngrok tunnel).
        // Accepts: 192.168.0.113 | 192.168.0.113:4444 | tcp://x.a.free.pinggy.link:41234
        //          https://abcd.a.free.pinggy.link | https://abcd.ngrok-free.app
        //          tcp://0.tcp.in.ngrok.io:12345 | any host, host:port or URL
        public static string PEER_ADDRESS = "";

        public static int PORT = 4444;            // local port this program listens on (tunnel to this port)
        public static string DOWNLOAD_FOLDER = ""; // "" = a "FileShare Downloads" folder next to this file
        // ===================================================================
        // Optional overrides without editing the file:
        //   file-share.bat <peer address>     (or: file-share.exe <peer address>)
        //   environment variables FILESHARE_PEER, FILESHARE_PORT, FILESHARE_DIR
    }

    static class K
    {
        public const string APP_NAME = "FileShare";
        public const int APP_VERSION = 1;
        public static readonly byte[] MAGIC = Encoding.ASCII.GetBytes("FSHR1\n");
        public const byte F_CTRL = 1, F_DATA = 2, F_PING = 3;
        public const int DATA_HDR = 16;                 // transfer id u32, offset u64, crc32 u32
        public const int MAX_FRAME = 64 * 1024 * 1024;
        public const int CHUNK = 512 * 1024;
        public const int RECV_SIZE = 1024 * 1024;
        public const int SOCK_BUF = 4 * 1024 * 1024;
        public const long DATA_QUEUE_MAX = 4 * 1024 * 1024;
        public const int WS_MAX_FRAME = 1024 * 1024;
        public const double PING_INTERVAL = 3.0, DEAD_AFTER = 15.0;
        public const double POLL_PING_INTERVAL = 10.0, POLL_DEAD_AFTER = 45.0, POLL_WAIT = 15.0;
        public const int POLL_BATCH = 4 * 1024 * 1024;
        public const double POLL_GIVE_UP = 30.0;
        public const double CONNECT_TIMEOUT = 8.0, HANDSHAKE_TIMEOUT = 10.0, SNIFF_TIMEOUT = 10.0;
        public const double HTTP_IDLE = 60.0, PROGRESS_EVERY = 0.5, HEALTHY_WINDOW = 5.0, RETRY_MAX = 5.0;
        public const int KEEPALIVE_IDLE = 20, KEEPALIVE_INTVL = 5, KEEPALIVE_CNT = 3;
        public const int MAX_STREAMS = 3, MAX_BATCH = 1000;
        public const double GROUP_WAIT = 1.5;
        public const string MARK_HEADER = "X-FShare";
        public const string WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
        public static readonly string[][] TUNNEL_HEADERS = new string[][] {
            new string[] { "User-Agent", "file-share/1" },
            new string[] { "X-Pinggy-No-Screen", "1" },
            new string[] { "ngrok-skip-browser-warning", "1" },
            new string[] { "Cache-Control", "no-cache" },
        };
        public static readonly string INSTANCE_ID = U.RandHex(8);
        public static readonly string HOSTNAME = U.MakeHostName();
        public static readonly bool IS_WINDOWS = Path.DirectorySeparatorChar == '\\';

        // Built-in TLS identity (the same certificate file-share.py carries), so two
        // copies can talk TLS directly on a LAN. Public sites are verified normally.
        public static readonly string TLS_CERT_DER =
        "MIIDNzCCAh+gAwIBAgIUAeA6SNShoQx/dNNRe93vuufF4DMwDQYJKoZIhvcNAQELBQAwFjEUMBIG" +
        "A1UEAwwLY21vbi1zZW5kZXIwIBcNMjYxMDIxMTk0OTM3WhgPMjEyNjA5MjcxOTQ5MzdaMBYxFDAS" +
        "BgNVBAMMC2Ntb24tc2VuZGVyMIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEApIxbuJpV" +
        "9TJxfccLXMhnlu23XIuYKfK0l+xNCbFip2yYpWJjoTUU2cYSE0mBy+cLK7J8KqvNLZMbubo2l9Jo" +
        "jGH5HtltWCLcTghHXAMeu5o5C7LmYiyCSiUR3m2/4YOIQUc2YWRw7rmRw8rYc21P4i1v4H91xUhO" +
        "bFqLim9uJQvcZlnV8g816qBYMmZt7U+jl9/hdhV7AcJTg6EitD6d34WP4IzjFmIdqBreh+MhOeUn" +
        "q9OXhzrCJJbMJEPHFWNG/wJLTTZKEfUSiiEJP24s9EsaLf5+IitgX/nillwGEgxnAfjUl56TnHbB" +
        "NYZ/w7qp8DIyScbE5obcaahCh3BmRwIDAQABo3sweTAdBgNVHQ4EFgQUmtaLkXOnoyUUk4Xh4bgB" +
        "yxoiWqEwHwYDVR0jBBgwFoAUmtaLkXOnoyUUk4Xh4bgByxoiWqEwDwYDVR0TAQH/BAUwAwEB/zAO" +
        "BgNVHQ8BAf8EBAMCAqQwFgYDVR0RBA8wDYILY21vbi1zZW5kZXIwDQYJKoZIhvcNAQELBQADggEB" +
        "AF/4peGmYRR7LQ9qJCZW2SZHKF5QJsrhNEpnmwosJUKarEqC5tUyJFM/wMeve+JgWiaMrTletBrA" +
        "R49b7FNbQtUBZJdqAWaTeTVjZvkssMUpENaSTpLt7G6YmRXJ3OzkjteCZcQxlYluQSbjc8SQo06z" +
        "UYARbTQliMh+6B3Tq+tgZJ19NRirEfotyoATLNFpoIpwhFuIVZsD3SJHCbf2P7gVEN5u1CfpYDT2" +
        "owQTQxs4ELyBiZar54piVqPaOZYT26lnlPkqxjCEGQUSr/7xPy92uCsdBqakIHLPb4nfAbt53iCO" +
        "mxq+moXa5zNhEcDmyZTXlP73To5MF7KGcoHCp9M=";
        public static readonly string TLS_PFX =
        "MIIJeQIBAzCCCT8GCSqGSIb3DQEHAaCCCTAEggksMIIJKDCCA98GCSqGSIb3DQEHBqCCA9AwggPM" +
        "AgEAMIIDxQYJKoZIhvcNAQcBMBwGCiqGSIb3DQEMAQMwDgQIlFYmFw+u5mcCAggAgIIDmK2FJS1N" +
        "Q7vGI1T1y2b0F1QpR1jLKctr3UhOAm6EvZGrSW1DxlMmXs9eEzphylCb6ezbMbhJyAxVxWW8cj2o" +
        "rpT5f8sNM4eTzSxpI274eIHV25SvI2Fk+oStldJL9364FbVqLkYPKYRGK1u/K6G5RA6fPXdficjE" +
        "6DW629tC0gCMp8QLYiopepKfh0EKGrq2pJxnFDY59ToPC6+rP7U4JBkzVF/X2X1uGnjIKvgMBtCe" +
        "wjJ9nErXFJB94ucoBwnmqWPg4hdthWBfXqeXpRvYCKa2mN0u2yAy6QS/ziEY4qDTjNdNmJvyp8Ko" +
        "eZ1yle1HDeAiiU+UPDxZUg6+bIVV9Drg3bAy+VQIpw8QMWCLmyx4lmD/2or1qiUtyoiTGGu88bgM" +
        "EwxgSA4e/1Ni2QLY4B2OSu41NkJl1Jm/dqQ8eIUTXb6694WQQDGuzpPxB4Gr/nGKYVOXeFWq9Mya" +
        "5CKZYCPVxpt14FKVcs9MkSyWk3vzsemqXmciGSke4Ca9IYQZABj2OgiEYzkyEbrzaZX37t62R1xj" +
        "k13zdMBWQe4RwsoId/k2owumZR8muhocWji1DwclJAbu9cm3BstOS+0NXr5UuJigDwNve3Lzu2h+" +
        "PcgNOwt74rxrtuTawBm9AdVfPcpufT3XsYmyIZj2XpAWA9r/B+YqNIqAvPuBt5ZYiNck8xEV6urQ" +
        "ob1XYsWWHOoQrQdug9DO2Tvu+K8xbS8kvVZm9Kykf5Jc518fpK/IA/qCPf/unuUgm2Ze0TihGd4b" +
        "ze6CGR0gRivJ/tJI/zTMVu95lWAp2I3tUmK0bSRDpVPJ9I1yIrYeFqPbZK1iZRilSwd7hrI8yQAR" +
        "FUvuM6v/XJ2Adc3lFZSe9F46rKAQbjiJVrdG60whEVgRmmuyL6BmDjAhJig/GQLl2VAEfLW9H7qD" +
        "w4DzxZc8OvfFlC3qn7kAq2O1Roq7a7snGqTixxpuCgeGeUHb/tEwlPM6gBQq5y/eEXhycB5DEJuy" +
        "VGDizdfz0YoWfD2m5X9z8LiVGSEikxJLOdhdQ3tMHivP0EDwaYYpwo1N1CrwdWgbgo2ipwgcnmU2" +
        "H/jnDv9zyAhAEdkOmAT//XBqCbdZVSInCIcR4qcUxETZE0I7o2aEiPkCJMQxFgRwEmKro5xtvoOe" +
        "ZaeKWDC1K71Q9IlFCtsRbY2VNwymYRVpPEmHUsr1kYCiDjZFJN+urGhgycwUIsL4S0IxwEZJ+Dg9" +
        "TMMFMIIFQQYJKoZIhvcNAQcBoIIFMgSCBS4wggUqMIIFJgYLKoZIhvcNAQwKAQKgggTuMIIE6jAc" +
        "BgoqhkiG9w0BDAEDMA4ECOuiq0WugC5ZAgIIAASCBMjWF1Ji0LOxzmlqWzdFn1zfbIfel2a4QEon" +
        "Ueu6SCOXEbzfO1CMNIKb2AhZbtislcWro57H1Yeobt2aDSk03TQ8xJgGl/WG/Z4vHW/8P76ha7K1" +
        "90Q9OAgM6AwXvKA5U6iBThYS1Cee6PVvXKliirn2Ln6CLO4iyJxK5fBt2YGRYWi5br9ZWNY9BunH" +
        "d+VYOmilA41bjHgmkLrRqdVb6uh7DvisFQUgRXzcxpyVKj95L2CDaY5k8QVSo/7dut5agkvK/KjA" +
        "KuFlfLQ0jXlwnfq3wwyj3AmaHS8/pjisVyV4jlc834y3iyrleoX+0U42Laoi4/K3QZERTwY5Eads" +
        "BlIZaloJzjfnccPe6odwpsDT76HnOXY2F0H+IZnbIddSLsFY4f39yQ2SU6CzGL3JOoP1l5WfgbP0" +
        "ryP2pCED3tJ7abblNjJ9jxzNTvhKOw7uCTFFJNzKacQdVcoEaSCkISIG4gUvRRTBArx0NPGTYY8J" +
        "rK9zcksdltjh6REudq/3GeCovbKfdZQFYs/mnfQE34+I7HsZRRTyN6VaYNW9IYm3B2E9yqzD3cm7" +
        "B2R4f3VuxL6oTauN3EgWBhGB6ew89wJwfaDe+arQ13pgd4kEDdZX2RM0Z1U9kQP9h3D1BVYVVTP0" +
        "hGxUPpYZNH2zPr3nUcGLKvGYvpTNHxQwAq9Hv63wgaExNR0aPzaS0jdGz2ErWGXQXnmqnRvJI8/E" +
        "SkasORJjL9wH+BJZ1CqykQGtXMrCLqnNG+kcmjX6fxTi7e/jXA/9Foitoq608WDCv1pnu33b/nst" +
        "+dkQBK8A3XGNHJ+tRI+mx9pJpDonRqV5UulBIln6Yiufc9mLqs7O2NXK505DvzgIFw2dw25eljqv" +
        "dERbHvEhMyHgIgYm1fgnjgQC9lpZ+EPe4rxJPxnaE76O6dcvbpDQsbO9uTT3GoYwT/4ULjgHCPNa" +
        "Nxw32ex+EYQhSg+sW8d19tYtoMKXcoIt34UzPOQDlF6ki+W+xPqVqtmnlOeXVvSai6BvdUs+9KMS" +
        "K3YSwizgSIqUWdwLv8BbEZqd1xSNdIKrN3/f7p7udQEFstUcoDt2j9v/dV8wkmW8sKOkw+5IufPv" +
        "ygZNgTikryIQYnsWpoTIqIRYMIrael3RBZcBlVYMr9CpMIWButRFoWy8Rf4bLFB+plXDEvmA7IX0" +
        "KR+59jA4JyPwkuTeJApRIO1b+QjsHvQXm4Y2Lfuvymfrc9aM0Pd2LWUBvNosQFkFVICv0F4klskt" +
        "lbsySUQcVpHMutWNXJkamAB8bHDBTofOn8/MLn8ye/NBOYYMbzuW3Iqhic2/MCe4M2mgUDyAz5QY" +
        "51KaGkfZqOmsdCTd62cJisTMszwxqj77tMCVC2S5DJVZNg/lLBlTFP+O1GTgTgl88wJT6WJuJMdv" +
        "q36uEEMoTxhz8ONgJhHpC7hL1eUiAwF6ZeVA0XGNULV1mzW4nwfSDlPkufJtsMz1MuycUeB5+pQ8" +
        "JjDsFwHwGz9XtJWU7OD3jRoU/8eyKo/RNFfDrFSK8ASZ0l9tc8gLf09CqI+paZRBiBC80xLt91BF" +
        "gMi8Aa7jX1rwzC6dJNvXxxjkpmai7OYounZtHAhfLPCNUY6QiQzeMp3I224aIWSTDUfeTAYQkUx3" +
        "x/kxJTAjBgkqhkiG9w0BCRUxFgQUhMbRtxUE46CCCdiwuu11UBpLn4swMTAhMAkGBSsOAwIaBQAE" +
        "FOl2jyZijsaGAq7aY3cBPk+W+GVjBAgBbI+dlqNUHQICCAA=";
    }

    // ======================================================================
    // Small helpers
    // ======================================================================
    static class U
    {
        static readonly Stopwatch clock = Stopwatch.StartNew();
        static readonly object rlock = new object();
        static readonly RandomNumberGenerator rng = RandomNumberGenerator.Create();

        public static double Now() { return clock.Elapsed.TotalSeconds; }

        public static byte[] RandBytes(int n)
        {
            byte[] b = new byte[n];
            lock (rlock) rng.GetBytes(b);
            return b;
        }

        public static string Hex(byte[] b)
        {
            StringBuilder sb = new StringBuilder(b.Length * 2);
            foreach (byte x in b) sb.Append(x.ToString("x2"));
            return sb.ToString();
        }

        public static string RandHex(int nbytes) { return Hex(RandBytes(nbytes)); }

        public static uint RandUInt()
        {
            return BitConverter.ToUInt32(RandBytes(4), 0);
        }

        public static string MakeHostName()
        {
            try
            {
                string h = (Environment.MachineName ?? "").Split('.')[0];
                h = Regex.Replace(h, @"[^\w-]", "");
                if (h.Length > 40) h = h.Substring(0, 40);
                return h.Length > 0 ? h : "other user";
            }
            catch { return "other user"; }
        }

        public static string F1(double v) { return v.ToString("0.0", System.Globalization.CultureInfo.InvariantCulture); }
        public static string F2(double v) { return v.ToString("0.00", System.Globalization.CultureInfo.InvariantCulture); }

        public static string Human(double n)
        {
            n = Math.Max(0, n);
            string[] units = { "B", "KB", "MB", "GB", "TB" };
            foreach (string unit in units)
            {
                if (n < 1024 || unit == "TB")
                    return unit == "B" ? ((long)n).ToString() + " B" : F2(n) + " " + unit;
                n /= 1024.0;
            }
            return ((long)n).ToString() + " B";
        }

        public static string FmtEta(double sec)
        {
            if (double.IsNaN(sec) || sec < 0 || sec > 360000) return "--:--";
            long s = (long)sec;
            long h = s / 3600, rem = s % 3600, m = rem / 60, ss = rem % 60;
            return h > 0 ? string.Format("{0}:{1:00}:{2:00}", h, m, ss) : string.Format("{0:00}:{1:00}", m, ss);
        }

        public static string Short(string name, int width)
        {
            return name.Length <= width ? name : name.Substring(0, width - 3) + "...";
        }

        public static string Pad(string s, int width)
        {
            return s.Length >= width ? s : s + new string(' ', width - s.Length);
        }

        public static string PadL(string s, int width)
        {
            return s.Length >= width ? s : new string(' ', width - s.Length) + s;
        }

        public static string Commas(long v) { return v.ToString("#,0", System.Globalization.CultureInfo.InvariantCulture); }

        public static List<string> LanIps()
        {
            List<string> ips = new List<string>();
            foreach (string target in new string[] { "8.8.8.8", "1.1.1.1" })
            {
                try
                {
                    using (Socket s = new Socket(AddressFamily.InterNetwork, SocketType.Dgram, ProtocolType.Udp))
                    {
                        s.Connect(IPAddress.Parse(target), 80);
                        string ip = ((IPEndPoint)s.LocalEndPoint).Address.ToString();
                        if (ip != "0.0.0.0" && !ip.StartsWith("127.") && !ips.Contains(ip)) ips.Add(ip);
                    }
                }
                catch { }
            }
            try
            {
                foreach (IPAddress a in Dns.GetHostAddresses(Dns.GetHostName()))
                {
                    if (a.AddressFamily != AddressFamily.InterNetwork) continue;
                    string ip = a.ToString();
                    if (!ip.StartsWith("127.") && !ips.Contains(ip)) ips.Add(ip);
                }
            }
            catch { }
            return ips.Take(3).ToList();
        }

        public static bool IsPrivateHost(string host)
        {
            string h = host.ToLowerInvariant();
            if (h == "localhost" || h.EndsWith(".local") || h.EndsWith(".lan")) return true;
            IPAddress ip;
            if (!IPAddress.TryParse(h.Trim('[', ']'), out ip)) return false;
            if (IPAddress.IsLoopback(ip)) return true;
            if (ip.AddressFamily == AddressFamily.InterNetworkV6)
            {
                if (ip.IsIPv6LinkLocal || ip.IsIPv6SiteLocal) return true;
                byte[] b6 = ip.GetAddressBytes();
                if ((b6[0] & 0xFE) == 0xFC) return true;   // fc00::/7
                bool mapped = true;
                for (int i = 0; i < 10; i++) if (b6[i] != 0) mapped = false;
                if (mapped && b6[10] == 0xFF && b6[11] == 0xFF)
                    return IsPrivateHost(string.Format("{0}.{1}.{2}.{3}", b6[12], b6[13], b6[14], b6[15]));
                return false;
            }
            byte[] b = ip.GetAddressBytes();
            return b[0] == 10 || b[0] == 127 || (b[0] == 172 && b[1] >= 16 && b[1] <= 31) ||
                   (b[0] == 192 && b[1] == 168) || (b[0] == 169 && b[1] == 254) || b[0] == 0;
        }

        public static bool IsLanPeer(string host)
        {
            string h = host.Replace("::ffff:", "").ToLowerInvariant().Trim('[', ']');
            if (h == "localhost" || h == "::1" || h.StartsWith("127.")) return false;
            return IsPrivateHost(h);
        }

        static readonly HashSet<string> WIN_RESERVED = MakeReserved();
        static HashSet<string> MakeReserved()
        {
            HashSet<string> s = new HashSet<string> { "CON", "PRN", "AUX", "NUL" };
            for (int i = 1; i < 10; i++) { s.Add("COM" + i); s.Add("LPT" + i); }
            return s;
        }
        static readonly Regex BAD_CHARS = new Regex("[<>:\"/\\\\|?*\\x00-\\x1f]");

        public static string Ext(string name)
        {
            // like os.path.splitext: ".bashrc" has no extension
            int slash = Math.Max(name.LastIndexOf('/'), name.LastIndexOf('\\'));
            int dot = name.LastIndexOf('.');
            if (dot <= slash + 1) return "";
            int k = slash + 1;
            while (k < dot && name[k] == '.') k++;
            if (k == dot) return "";
            return name.Substring(dot);
        }

        public static string SafeComponent(string part)
        {
            part = BAD_CHARS.Replace(part ?? "", "_").Trim().TrimEnd('.', ' ');
            if (part == "" || part == "." || part == "..") part = "_";
            if (WIN_RESERVED.Contains(part.Split('.')[0].ToUpperInvariant())) part = "_" + part;
            if (part.Length > 180)
            {
                string ext = Ext(part);
                string root = part.Substring(0, part.Length - ext.Length);
                if (ext.Length > 20) ext = ext.Substring(0, 20);
                part = root.Substring(0, Math.Min(root.Length, 180 - ext.Length)) + ext;
            }
            return part;
        }

        public static string SafeRelPath(string rel)
        {
            List<string> parts = Regex.Split(rel ?? "", @"[\\/]+").Where(p => p != "" && p != ".").ToList();
            if (parts.Count == 0) return "_";
            return string.Join("/", parts.Select(p => SafeComponent(p)).ToArray());
        }

        public static string JoinRel(string root, string rel)
        {
            string p = root;
            foreach (string part in rel.Split('/')) p = Path.Combine(p, part);
            return p;
        }

        public static bool Exists(string p)
        {
            try { return File.Exists(Lp(p)) || Directory.Exists(Lp(p)); } catch { return false; }
        }

        public static string UniquePath(string folder, string name)
        {
            string path = Path.Combine(folder, name);
            if (!Exists(path)) return path;
            string ext = Ext(name);
            string root = name.Substring(0, name.Length - ext.Length);
            for (int i = 1; ; i++)
            {
                string cand = Path.Combine(folder, root + " (" + i + ")" + ext);
                if (!Exists(cand)) return cand;
            }
        }

        public static string ExpandUser(string s)
        {
            if (s == "~" || s.StartsWith("~/") || s.StartsWith("~\\"))
            {
                string home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
                if (string.IsNullOrEmpty(home)) home = Environment.GetEnvironmentVariable("HOME") ?? "";
                return home + s.Substring(1);
            }
            return s;
        }

        public static string CleanPathArg(string arg)
        {
            string s = arg.Trim();
            if (s.StartsWith("& ")) s = s.Substring(2).Trim();      // PowerShell drag-and-drop: & 'C:\x y'
            if (s.Length >= 2 && s[0] == s[s.Length - 1] && (s[0] == '"' || s[0] == '\'')) s = s.Substring(1, s.Length - 2);
            if (s.StartsWith("file://"))
            {
                try
                {
                    s = Uri.UnescapeDataString(s.Substring(7));
                    if (K.IS_WINDOWS && Regex.IsMatch(s, "^/[A-Za-z]:")) s = s.Substring(1);
                }
                catch { }
            }
            s = ExpandUser(s);
            if (!Exists(s) && !K.IS_WINDOWS && s.Contains("\\"))
            {
                string un = Regex.Replace(s, @"\\(.)", "$1");          // macOS/Linux drag: My\ File.mp4
                if (Exists(un)) s = un;
            }
            return s;
        }

        static List<string> Glob(string pattern)
        {
            List<string> res = new List<string>();
            try
            {
                string dir = Path.GetDirectoryName(pattern);
                string pat = Path.GetFileName(pattern);
                if (string.IsNullOrEmpty(dir)) dir = ".";
                if (dir.IndexOfAny(new char[] { '*', '?' }) >= 0 || string.IsNullOrEmpty(pat)) return res;
                string[] found = Directory.GetFileSystemEntries(dir, pat);
                Array.Sort(found, StringComparer.Ordinal);
                foreach (string f in found)
                {
                    string fn = Path.GetFileName(f);
                    if (fn.StartsWith(".") && !pat.StartsWith(".")) continue;
                    res.Add(pattern.StartsWith("." + Path.DirectorySeparatorChar) || Path.IsPathRooted(pattern) || Path.GetDirectoryName(pattern) != "" ? f : fn);
                }
            }
            catch { }
            return res;
        }

        public static string NormKey(string p)
        {
            string full;
            try { full = Path.GetFullPath(p); } catch { full = p; }
            return K.IS_WINDOWS ? full.ToLowerInvariant().Replace('/', '\\') : full;
        }

        /// Split an upload argument into paths: quoted, escaped, dragged, several, or wildcards.
        public static void ParsePaths(string arg, out List<string> found, out List<string> missing)
        {
            found = new List<string>();
            missing = new List<string>();
            string s = arg.Trim();
            if (s == "") return;
            string one = CleanPathArg(s);
            if (Exists(one)) { found.Add(one); return; }
            if (s.StartsWith("& ")) s = s.Substring(2).Trim();
            string pat = K.IS_WINDOWS ? "\"([^\"]*)\"|'([^']*)'|(\\S+)" : "\"([^\"]*)\"|'([^']*)'|((?:\\\\.|\\S)+)";
            List<KeyValuePair<string, bool>> toks = new List<KeyValuePair<string, bool>>();
            foreach (Match m in Regex.Matches(s, pat))
            {
                if (m.Groups[1].Success) toks.Add(new KeyValuePair<string, bool>(m.Groups[1].Value, true));
                else if (m.Groups[2].Success) toks.Add(new KeyValuePair<string, bool>(m.Groups[2].Value, true));
                else toks.Add(new KeyValuePair<string, bool>(m.Groups[3].Value, false));
            }
            List<string> raw = new List<string>();
            int i = 0;
            while (i < toks.Count)
            {
                string text = toks[i].Key;
                bool quoted = toks[i].Value;
                string p = CleanPathArg(text);
                if (Exists(p)) { raw.Add(p); i++; continue; }
                string hit = null; int hitj = -1;
                if (!quoted)
                {
                    int j = i; string acc = text;
                    while (j + 1 < toks.Count && !toks[j + 1].Value && j - i < 30)
                    {
                        j++;
                        acc = acc + " " + toks[j].Key;
                        string cand = CleanPathArg(acc);
                        if (Exists(cand)) { hit = cand; hitj = j; break; }
                    }
                }
                if (hit != null) { raw.Add(hit); i = hitj + 1; continue; }
                if (p.IndexOfAny(new char[] { '*', '?' }) >= 0)
                {
                    List<string> matches = Glob(p);
                    if (matches.Count > 0) { raw.AddRange(matches); i++; continue; }
                }
                missing.Add(p);
                i++;
            }
            HashSet<string> seen = new HashSet<string>();
            foreach (string p in raw)
                if (seen.Add(NormKey(p))) found.Add(p);
        }

        /// Windows: allow paths longer than 260 characters (deep folders).
        public static string Lp(string path)
        {
            if (!K.IS_WINDOWS || path.Length < 240) return path;
            try { path = Path.GetFullPath(path); } catch { return path; }
            if (path.StartsWith(@"\\?\")) return path;
            if (path.StartsWith(@"\\")) return @"\\?\UNC\" + path.Substring(2);
            return @"\\?\" + path;
        }

        public static long DiskFree(string path)
        {
            try
            {
                string full = Path.GetFullPath(path);
                DriveInfo best = null;
                foreach (DriveInfo d in DriveInfo.GetDrives())
                {
                    string root;
                    try { root = d.RootDirectory.FullName; } catch { continue; }
                    bool match = K.IS_WINDOWS ? full.StartsWith(root, StringComparison.OrdinalIgnoreCase) : full.StartsWith(root);
                    if (!K.IS_WINDOWS && root != "/" && match && full.Length > root.Length && full[root.Length] != '/' && !root.EndsWith("/")) match = false;
                    if (match && (best == null || root.Length > best.RootDirectory.FullName.Length)) best = d;
                }
                if (best == null) return -1;
                return best.AvailableFreeSpace;
            }
            catch { return -1; }
        }

        // ---- CRC32 (same as zlib.crc32), slicing-by-8 for speed ----
        static readonly uint[] crcT = MakeCrc();
        static uint[] MakeCrc()
        {
            uint[] t = new uint[8 * 256];
            for (uint i = 0; i < 256; i++)
            {
                uint c = i;
                for (int k = 0; k < 8; k++) c = (c & 1) != 0 ? 0xEDB88320u ^ (c >> 1) : c >> 1;
                t[i] = c;
            }
            for (int i = 0; i < 256; i++)
                for (int s = 1; s < 8; s++)
                    t[s * 256 + i] = (t[(s - 1) * 256 + i] >> 8) ^ t[t[(s - 1) * 256 + i] & 0xFF];
            return t;
        }

        public static uint Crc32(byte[] b, int off, int len)
        {
            uint[] t = crcT;
            uint c = 0xFFFFFFFFu;
            int i = off, end = off + len;
            while (end - i >= 8)
            {
                uint a = c ^ (uint)(b[i] | (b[i + 1] << 8) | (b[i + 2] << 16) | (b[i + 3] << 24));
                uint d = (uint)(b[i + 4] | (b[i + 5] << 8) | (b[i + 6] << 16) | (b[i + 7] << 24));
                c = t[7 * 256 + (a & 0xFF)] ^ t[6 * 256 + ((a >> 8) & 0xFF)] ^ t[5 * 256 + ((a >> 16) & 0xFF)] ^ t[4 * 256 + (a >> 24)]
                  ^ t[3 * 256 + (d & 0xFF)] ^ t[2 * 256 + ((d >> 8) & 0xFF)] ^ t[1 * 256 + ((d >> 16) & 0xFF)] ^ t[d >> 24];
                i += 8;
            }
            while (i < end) { c = t[(c ^ b[i]) & 0xFF] ^ (c >> 8); i++; }
            return c ^ 0xFFFFFFFFu;
        }

        public static void PutU32(byte[] b, int o, uint v)
        {
            b[o] = (byte)(v >> 24); b[o + 1] = (byte)(v >> 16); b[o + 2] = (byte)(v >> 8); b[o + 3] = (byte)v;
        }
        public static void PutU64(byte[] b, int o, ulong v) { PutU32(b, o, (uint)(v >> 32)); PutU32(b, o + 4, (uint)v); }
        public static uint GetU32(byte[] b, int o)
        {
            return ((uint)b[o] << 24) | ((uint)b[o + 1] << 16) | ((uint)b[o + 2] << 8) | b[o + 3];
        }
        public static ulong GetU64(byte[] b, int o) { return ((ulong)GetU32(b, o) << 32) | GetU32(b, o + 4); }

        public static byte[] Concat(params byte[][] parts)
        {
            int n = 0;
            foreach (byte[] p in parts) n += p.Length;
            byte[] r = new byte[n];
            int o = 0;
            foreach (byte[] p in parts) { Buffer.BlockCopy(p, 0, r, o, p.Length); o += p.Length; }
            return r;
        }

        public static bool StartsWith(byte[] buf, int len, byte[] prefix)
        {
            if (len < prefix.Length) return false;
            for (int i = 0; i < prefix.Length; i++) if (buf[i] != prefix[i]) return false;
            return true;
        }

        public static string Latin1(byte[] b, int o, int n)
        {
            char[] c = new char[n];
            for (int i = 0; i < n; i++) c[i] = (char)b[o + i];
            return new string(c);
        }

        public static byte[] Latin1Bytes(string s)
        {
            byte[] b = new byte[s.Length];
            for (int i = 0; i < s.Length; i++) b[i] = (byte)(s[i] < 256 ? s[i] : '?');
            return b;
        }

        public static void StartThread(ThreadStart fn)
        {
            Thread t = new Thread(fn, 256 * 1024);
            t.IsBackground = true;
            t.Start();
        }

        public static void Sleep(double sec) { Thread.Sleep((int)Math.Max(0, sec * 1000)); }

        public static string Sha256Hex(string s)
        {
            using (SHA256 h = SHA256.Create()) return Hex(h.ComputeHash(Encoding.UTF8.GetBytes(s)));
        }

        public static string WsAccept(string key)
        {
            using (SHA1 h = SHA1.Create())
                return Convert.ToBase64String(h.ComputeHash(Encoding.ASCII.GetBytes(key + K.WS_GUID)));
        }

        public static string ErrText(Exception e)
        {
            if (e == null) return "error";
            string m = e.Message;
            return string.IsNullOrEmpty(m) ? e.GetType().Name : m.TrimEnd('.');
        }
    }

    /// Growable byte buffer with a cheap "consume from the front".
    class ByteBuf
    {
        public byte[] Data = new byte[64 * 1024];
        public int Start, Count;

        public void Append(byte[] b, int off, int n)
        {
            if (n <= 0) return;
            if (Start + Count + n > Data.Length)
            {
                if (Count + n <= Data.Length && Start > 0)
                {
                    Buffer.BlockCopy(Data, Start, Data, 0, Count);
                    Start = 0;
                }
                else
                {
                    int cap = Data.Length;
                    while (cap < Count + n) cap *= 2;
                    byte[] nd = new byte[cap];
                    Buffer.BlockCopy(Data, Start, nd, 0, Count);
                    Data = nd;
                    Start = 0;
                }
            }
            Buffer.BlockCopy(b, off, Data, Start + Count, n);
            Count += n;
        }
        public void Append(byte[] b) { Append(b, 0, b.Length); }
        public void Consume(int n)
        {
            Start += n; Count -= n;
            if (Count == 0) Start = 0;
            if (Count == 0 && Data.Length > 8 * 1024 * 1024) Data = new byte[64 * 1024];
        }
        public byte this[int i] { get { return Data[Start + i]; } }
        public byte[] Take(int n)
        {
            byte[] r = new byte[n];
            Buffer.BlockCopy(Data, Start, r, 0, n);
            Consume(n);
            return r;
        }
        public byte[] ToArray() { byte[] r = new byte[Count]; Buffer.BlockCopy(Data, Start, r, 0, Count); return r; }
        public int IndexOf(byte[] delim)
        {
            for (int i = 0; i + delim.Length <= Count; i++)
            {
                bool ok = true;
                for (int j = 0; j < delim.Length; j++)
                    if (Data[Start + i + j] != delim[j]) { ok = false; break; }
                if (ok) return i;
            }
            return -1;
        }
    }

    // ======================================================================
    // Minimal JSON (objects, arrays, strings, numbers, true/false/null)
    // ======================================================================
    static class Json
    {
        public static string Write(object v)
        {
            StringBuilder sb = new StringBuilder();
            W(sb, v);
            return sb.ToString();
        }

        static void W(StringBuilder sb, object v)
        {
            if (v == null) { sb.Append("null"); return; }
            if (v is string) { Str(sb, (string)v); return; }
            if (v is bool) { sb.Append((bool)v ? "true" : "false"); return; }
            if (v is int || v is long || v is uint || v is ulong) { sb.Append(Convert.ToString(v, System.Globalization.CultureInfo.InvariantCulture)); return; }
            if (v is double) { sb.Append(((double)v).ToString("R", System.Globalization.CultureInfo.InvariantCulture)); return; }
            System.Collections.IDictionary d = v as System.Collections.IDictionary;
            if (d != null)
            {
                sb.Append('{');
                bool first = true;
                foreach (System.Collections.DictionaryEntry e in d)
                {
                    if (!first) sb.Append(',');
                    first = false;
                    Str(sb, e.Key.ToString());
                    sb.Append(':');
                    W(sb, e.Value);
                }
                sb.Append('}');
                return;
            }
            System.Collections.IEnumerable list = v as System.Collections.IEnumerable;
            if (list != null)
            {
                sb.Append('[');
                bool first = true;
                foreach (object x in list)
                {
                    if (!first) sb.Append(',');
                    first = false;
                    W(sb, x);
                }
                sb.Append(']');
                return;
            }
            Str(sb, v.ToString());
        }

        static void Str(StringBuilder sb, string s)
        {
            sb.Append('"');
            foreach (char c in s)
            {
                switch (c)
                {
                    case '"': sb.Append("\\\""); break;
                    case '\\': sb.Append("\\\\"); break;
                    case '\n': sb.Append("\\n"); break;
                    case '\r': sb.Append("\\r"); break;
                    case '\t': sb.Append("\\t"); break;
                    default:
                        if (c < 0x20 || c > 0x7e) sb.Append("\\u").Append(((int)c).ToString("x4"));
                        else sb.Append(c);
                        break;
                }
            }
            sb.Append('"');
        }

        public static object Parse(string s)
        {
            int i = 0;
            object v = Val(s, ref i);
            Ws(s, ref i);
            if (i != s.Length) throw new FormatException("json: trailing data");
            return v;
        }

        static void Ws(string s, ref int i)
        {
            while (i < s.Length && (s[i] == ' ' || s[i] == '\t' || s[i] == '\n' || s[i] == '\r')) i++;
        }

        static object Val(string s, ref int i)
        {
            Ws(s, ref i);
            if (i >= s.Length) throw new FormatException("json: end");
            char c = s[i];
            if (c == '{')
            {
                Dictionary<string, object> d = new Dictionary<string, object>();
                i++;
                Ws(s, ref i);
                if (i < s.Length && s[i] == '}') { i++; return d; }
                while (true)
                {
                    Ws(s, ref i);
                    if (i >= s.Length || s[i] != '"') throw new FormatException("json: key");
                    string k = StrVal(s, ref i);
                    Ws(s, ref i);
                    if (i >= s.Length || s[i] != ':') throw new FormatException("json: colon");
                    i++;
                    d[k] = Val(s, ref i);
                    Ws(s, ref i);
                    if (i < s.Length && s[i] == ',') { i++; continue; }
                    if (i < s.Length && s[i] == '}') { i++; return d; }
                    throw new FormatException("json: object");
                }
            }
            if (c == '[')
            {
                List<object> l = new List<object>();
                i++;
                Ws(s, ref i);
                if (i < s.Length && s[i] == ']') { i++; return l; }
                while (true)
                {
                    l.Add(Val(s, ref i));
                    Ws(s, ref i);
                    if (i < s.Length && s[i] == ',') { i++; continue; }
                    if (i < s.Length && s[i] == ']') { i++; return l; }
                    throw new FormatException("json: array");
                }
            }
            if (c == '"') return StrVal(s, ref i);
            if (string.CompareOrdinal(s, i, "true", 0, 4) == 0) { i += 4; return true; }
            if (string.CompareOrdinal(s, i, "false", 0, 5) == 0) { i += 5; return false; }
            if (string.CompareOrdinal(s, i, "null", 0, 4) == 0) { i += 4; return null; }
            int st = i;
            while (i < s.Length && "+-0123456789.eE".IndexOf(s[i]) >= 0) i++;
            string num = s.Substring(st, i - st);
            if (num.Length == 0) throw new FormatException("json: value");
            long lv;
            if (long.TryParse(num, System.Globalization.NumberStyles.AllowLeadingSign, System.Globalization.CultureInfo.InvariantCulture, out lv)) return lv;
            return double.Parse(num, System.Globalization.CultureInfo.InvariantCulture);
        }

        static string StrVal(string s, ref int i)
        {
            i++;
            StringBuilder sb = new StringBuilder();
            while (true)
            {
                if (i >= s.Length) throw new FormatException("json: string");
                char c = s[i++];
                if (c == '"') return sb.ToString();
                if (c != '\\') { sb.Append(c); continue; }
                if (i >= s.Length) throw new FormatException("json: escape");
                char e = s[i++];
                switch (e)
                {
                    case 'n': sb.Append('\n'); break;
                    case 'r': sb.Append('\r'); break;
                    case 't': sb.Append('\t'); break;
                    case 'b': sb.Append('\b'); break;
                    case 'f': sb.Append('\f'); break;
                    case 'u':
                        if (i + 4 > s.Length) throw new FormatException("json: \\u");
                        sb.Append((char)Convert.ToInt32(s.Substring(i, 4), 16));
                        i += 4;
                        break;
                    default: sb.Append(e); break;
                }
            }
        }

        // ---- typed access helpers (like Python's msg.get) ----
        public static object Get(Dictionary<string, object> d, string k)
        {
            object v;
            return d != null && d.TryGetValue(k, out v) ? v : null;
        }
        public static string Str(Dictionary<string, object> d, string k)
        {
            object v = Get(d, k);
            if (v == null) return "";
            if (v is bool) return (bool)v ? "True" : "";
            return Convert.ToString(v, System.Globalization.CultureInfo.InvariantCulture);
        }
        public static bool Truthy(object v)
        {
            if (v == null) return false;
            if (v is bool) return (bool)v;
            if (v is long) return (long)v != 0;
            if (v is double) return (double)v != 0;
            if (v is string) return ((string)v).Length > 0;
            System.Collections.ICollection c = v as System.Collections.ICollection;
            if (c != null) return c.Count > 0;
            return true;
        }
        public static long Long(object v)
        {
            if (v is long) return (long)v;
            if (v is double) return (long)(double)v;
            if (v is bool) return (bool)v ? 1 : 0;
            if (v is string) return long.Parse((string)v, System.Globalization.CultureInfo.InvariantCulture);
            throw new FormatException("not a number");
        }

        public static Dictionary<string, object> Obj(params object[] kv)
        {
            Dictionary<string, object> d = new Dictionary<string, object>();
            for (int i = 0; i + 1 < kv.Length; i += 2) d[(string)kv[i]] = kv[i + 1];
            return d;
        }
    }

    /// Transfer speed over the last few seconds.
    class Meter
    {
        readonly Queue<double[]> samples = new Queue<double[]>();
        double[] last;
        public void Add(long total)
        {
            lock (this)
            {
                double now = U.Now();
                last = new double[] { now, total };
                samples.Enqueue(last);
                while (samples.Count > 2 && now - samples.Peek()[0] > 6.0) samples.Dequeue();
            }
        }
        public double Rate()
        {
            lock (this)
            {
                if (samples.Count < 2) return 0.0;
                double[] a = samples.Peek(), b = last;
                return b[0] > a[0] ? (b[1] - a[1]) / (b[0] - a[0]) : 0.0;
            }
        }
        public void Reset() { lock (this) { samples.Clear(); last = null; } }
    }

    // ======================================================================
    // Console: messages, one live status line, and the input line
    // ======================================================================
    class Con
    {
        public static readonly Con I = new Con();
        public readonly object Lock = new object();
        public bool Tty;
        public string Prompt = "> ";
        string status = "";
        string typed = "";
        int drawn = 0;
        double lastPlain = -100;
        TextWriter outw;

        Con()
        {
            try { Tty = !Console.IsOutputRedirected; } catch { Tty = false; }
            try
            {
                if (K.IS_WINDOWS && Tty) Console.OutputEncoding = new UTF8Encoding(false);
            }
            catch { }
            try
            {
                Stream so = Console.OpenStandardOutput();
                StreamWriter w = new StreamWriter(so, Tty && K.IS_WINDOWS ? Console.OutputEncoding : new UTF8Encoding(false), 8192);
                w.AutoFlush = true;
                outw = w;
            }
            catch { outw = Console.Out; }
        }

        void Out(string s)
        {
            try { outw.Write(s); outw.Flush(); } catch { }
        }

        int Width()
        {
            try { int w = Console.WindowWidth; return Math.Max(20, (w > 0 ? w : 80) - 1); } catch { return 79; }
        }

        string Line()
        {
            int width = Width();
            string right = Prompt + typed;
            if (right.Length > width)
            {
                int keep = Math.Max(1, width - Prompt.Length - 1);
                right = Prompt + "<" + typed.Substring(Math.Max(0, typed.Length - keep));
            }
            string left = status;
            if (left.Length > 0)
            {
                int avail = width - right.Length - 3;
                left = avail >= 12 ? (left.Length > avail ? left.Substring(0, avail) : left) + " | " : "";
            }
            return left + right;
        }

        void Clear()
        {
            if (Tty && drawn > 0) Out("\r" + new string(' ', drawn) + "\r");
            drawn = 0;
        }

        void Draw()
        {
            if (!Tty) return;
            string line = Line();
            int pad = Math.Max(0, drawn - line.Length);
            Out("\r" + line + (pad > 0 ? new string(' ', pad) + "\r" + line : ""));
            drawn = line.Length;
        }

        public void Say(string text)
        {
            lock (Lock)
            {
                Clear();
                Out(text.Replace("\r\n", "\n").Replace("\n", Environment.NewLine) + Environment.NewLine);
                if (Tty) Draw();
            }
        }

        public void SetPrompt(string prompt, bool announce)
        {
            lock (Lock)
            {
                Prompt = prompt;
                if (Tty) { Clear(); Draw(); }
                else if (announce) Out(prompt + Environment.NewLine);
            }
        }

        public void SetStatus(string text)
        {
            lock (Lock)
            {
                if (text == status) return;
                status = text;
                if (Tty) Draw();
                else if (text.Length > 0 && U.Now() - lastPlain > 5.0)
                {
                    lastPlain = U.Now();
                    Out(text + Environment.NewLine);
                }
            }
        }

        public void AddChar(char ch)
        {
            lock (Lock)
            {
                typed += ch;
                string line = Line();
                if (Tty && line.Length == drawn + 1 && line.EndsWith(typed)) { Out(ch.ToString()); drawn = line.Length; }
                else Draw();
            }
        }

        public void SetTyped(string text) { lock (Lock) { typed = text; Draw(); } }

        public void LineDone(bool echoNewline)
        {
            lock (Lock)
            {
                if (echoNewline && Tty) Out(Environment.NewLine);
                typed = "";
                drawn = 0;
                if (Tty) Draw();
            }
        }

        public static void S(string text) { I.Say(text); }
    }

    // ======================================================================
    // Keyboard input: our own line editor so Ctrl+Z works on every OS
    // ======================================================================
    class LineEditor
    {
        public bool RawMode;
        readonly Action onCtrlZ;

        public LineEditor(Action onCtrlZ)
        {
            this.onCtrlZ = onCtrlZ;
            try { RawMode = !Console.IsInputRedirected; } catch { RawMode = false; }
            if (RawMode)
            {
                try { Console.TreatControlCAsInput = true; } catch { RawMode = false; }
            }
        }

        public void Close()
        {
            try { if (RawMode) Console.TreatControlCAsInput = false; } catch { }
        }

        /// Returns the typed line; null when the input ended.
        public string ReadLine()
        {
            if (!RawMode)
            {
                string l = Console.ReadLine();
                Con.I.LineDone(false);
                return l;
            }
            string typed = "";
            while (true)
            {
                ConsoleKeyInfo k;
                try { k = Console.ReadKey(true); }
                catch (InvalidOperationException) { RawMode = false; return ReadLine(); }
                char ch = k.KeyChar;
                bool ctrl = (k.Modifiers & ConsoleModifiers.Control) != 0;
                if (k.Key == ConsoleKey.Enter || ch == '\r' || ch == '\n') { Con.I.LineDone(true); return typed; }
                if (ch == '\x03' || (ctrl && k.Key == ConsoleKey.C)) throw new OperationCanceledException("ctrl+c");
                if (ch == '\x1a' || (ctrl && k.Key == ConsoleKey.Z)) { onCtrlZ(); continue; }
                if ((ch == '\x04' || (ctrl && k.Key == ConsoleKey.D)) && typed.Length == 0 && !K.IS_WINDOWS) throw new OperationCanceledException("ctrl+d");
                if (k.Key == ConsoleKey.Backspace || ch == '\b' || ch == '\x7f')
                {
                    if (typed.Length > 0) typed = typed.Substring(0, typed.Length - 1);
                    Con.I.SetTyped(typed);
                    continue;
                }
                if (ch == '\x15' || k.Key == ConsoleKey.Escape) { typed = ""; Con.I.SetTyped(typed); continue; }
                if (ch == '\t') ch = ' ';
                if (ch < ' ') continue;
                typed += ch;
                Con.I.AddChar(ch);
            }
        }
    }

    // ======================================================================
    // Sockets and connection errors
    // ======================================================================
    class NotUs : Exception { public NotUs(string m) : base(m) { } }
    class BusyEx : Exception { public BusyEx() : base("busy") { } }
    class PortDown : Exception { public PortDown(string m) : base(m) { } }
    class SoftEOF : NotUs { public SoftEOF(string m) : base(m) { } }
    class TunnelNoApp : SoftEOF { public TunnelNoApp(string m) : base(m) { } }
    class HostDown : PortDown { public HostDown(string m) : base(m) { } }
    class ClosedEx : IOException { public ClosedEx(string m) : base(m) { } }

    static class Net
    {
        public static readonly bool IsCore = typeof(object).Assembly.GetName().Name == "System.Private.CoreLib";
        static byte[] pinned;
        static X509Certificate2 serverCert;
        static bool serverCertTried;
        static readonly object certLock = new object();
        static int warnedInsecure = 0;

        public static SslProtocols Protocols()
        {
            if (IsCore) return SslProtocols.None;   // let the OS pick (TLS 1.2 / 1.3)
            return (SslProtocols)(3072 | 768 | 192); // TLS 1.2, 1.1, 1.0 for .NET Framework / Mono
        }

        static byte[] Pinned()
        {
            if (pinned == null) pinned = Convert.FromBase64String(K.TLS_CERT_DER);
            return pinned;
        }

        public static bool Validate(object sender, X509Certificate cert, X509Chain chain, SslPolicyErrors errors)
        {
            if (errors == SslPolicyErrors.None) return true;
            if (cert != null)
            {
                byte[] raw = cert.GetRawCertData();
                byte[] p = Pinned();
                if (raw != null && raw.Length == p.Length)
                {
                    bool same = true;
                    for (int i = 0; i < p.Length; i++) if (raw[i] != p[i]) { same = false; break; }
                    if (same) return true;      // the other file-share copy (direct LAN TLS)
                }
            }
            // Last resort, like file-share.py: still encrypted, just not verified.
            if (Interlocked.Exchange(ref warnedInsecure, 1) == 0)
                Con.S("[!] This computer could not verify the tunnel's HTTPS certificate (its certificate " +
                      "store is missing or outdated). The link is still encrypted.");
            return true;
        }

        public static X509Certificate2 ServerCert()
        {
            lock (certLock)
            {
                if (serverCertTried) return serverCert;
                serverCertTried = true;
                byte[] pfx = Convert.FromBase64String(K.TLS_PFX);
                X509KeyStorageFlags[] tries = {
                    X509KeyStorageFlags.Exportable,
                    X509KeyStorageFlags.UserKeySet | X509KeyStorageFlags.Exportable,
                    X509KeyStorageFlags.MachineKeySet | X509KeyStorageFlags.Exportable,
                };
                foreach (X509KeyStorageFlags f in tries)
                {
                    foreach (string pw in new string[] { "", null })
                    {
                        try
                        {
                            X509Certificate2 c = new X509Certificate2(pfx, pw, f);
                            if (c.HasPrivateKey) { serverCert = c; return c; }
                        }
                        catch { }
                    }
                }
                return null;
            }
        }

        static void TrySet(Socket s, SocketOptionLevel level, SocketOptionName name, int value)
        {
            try { s.SetSocketOption(level, name, value); } catch { }
        }

        public static void Tune(Socket s)
        {
            try { s.NoDelay = true; } catch { }
            TrySet(s, SocketOptionLevel.Socket, SocketOptionName.KeepAlive, 1);
            TrySet(s, SocketOptionLevel.Socket, SocketOptionName.SendBuffer, K.SOCK_BUF);
            TrySet(s, SocketOptionLevel.Socket, SocketOptionName.ReceiveBuffer, K.SOCK_BUF);
            if (K.IS_WINDOWS)
            {
                try
                {
                    byte[] v = new byte[12];
                    BitConverter.GetBytes((uint)1).CopyTo(v, 0);
                    BitConverter.GetBytes((uint)(K.KEEPALIVE_IDLE * 1000)).CopyTo(v, 4);
                    BitConverter.GetBytes((uint)(K.KEEPALIVE_INTVL * 1000)).CopyTo(v, 8);
                    s.IOControl(IOControlCode.KeepAliveValues, v, null);
                }
                catch { }
            }
            else if (IsCore)
            {
                TrySet(s, SocketOptionLevel.Tcp, (SocketOptionName)3, K.KEEPALIVE_IDLE);    // TcpKeepAliveTime
                TrySet(s, SocketOptionLevel.Tcp, (SocketOptionName)17, K.KEEPALIVE_INTVL);  // TcpKeepAliveInterval
                TrySet(s, SocketOptionLevel.Tcp, (SocketOptionName)16, K.KEEPALIVE_CNT);    // TcpKeepAliveRetryCount
            }
        }

        public static void CloseSock(Socket s)
        {
            if (s == null) return;
            try { s.Shutdown(SocketShutdown.Both); } catch { }
            try { s.Close(); } catch { }
        }

        public static Socket ConnectTcp(string host, int port)
        {
            IPAddress[] addrs;
            IPAddress lit;
            if (IPAddress.TryParse(host, out lit)) addrs = new IPAddress[] { lit };
            else
            {
                try
                {
                    IAsyncResult ar = Dns.BeginGetHostAddresses(host, null, null);
                    if (!ar.AsyncWaitHandle.WaitOne((int)(K.CONNECT_TIMEOUT * 1000))) throw new HostDown(host);
                    addrs = Dns.EndGetHostAddresses(ar);
                }
                catch (HostDown) { throw; }
                catch (Exception) { throw new HostDown(host); }
                if (addrs.Length == 0) throw new HostDown(host);
                addrs = addrs.Where(a => a.AddressFamily == AddressFamily.InterNetwork)
                             .Concat(addrs.Where(a => a.AddressFamily == AddressFamily.InterNetworkV6)).ToArray();
            }
            string last = "unreachable";
            foreach (IPAddress a in addrs.Take(4))
            {
                Socket s = null;
                try
                {
                    s = new Socket(a.AddressFamily, SocketType.Stream, ProtocolType.Tcp);
                    IAsyncResult ar = s.BeginConnect(a, port, null, null);
                    if (!ar.AsyncWaitHandle.WaitOne((int)(K.CONNECT_TIMEOUT * 1000)))
                    {
                        CloseSock(s);
                        last = "timed out";
                        continue;
                    }
                    s.EndConnect(ar);
                    Tune(s);
                    return s;
                }
                catch (Exception e)
                {
                    CloseSock(s);
                    last = U.ErrText(e);
                }
            }
            throw new PortDown(last);
        }

        public static bool EofLike(Exception e)
        {
            for (Exception x = e; x != null; x = x.InnerException)
            {
                SocketException se = x as SocketException;
                if (se != null && (se.SocketErrorCode == SocketError.ConnectionReset || se.SocketErrorCode == SocketError.ConnectionAborted
                                   || se.SocketErrorCode == SocketError.Shutdown)) return true;
                string m = (x.Message ?? "").ToLowerInvariant();
                if (m.Contains("0 bytes") || m.Contains("eof") || m.Contains("closed the transport") || m.Contains("connection reset")
                    || m.Contains("forcibly closed") || m.Contains("connection was closed") || m.Contains("end of stream")) return true;
                if (x is EndOfStreamException || x is ClosedEx) return true;
            }
            return false;
        }

        public static StreamConn ConnectTls(string host, int port, string label)
        {
            Socket s = ConnectTcp(host, port);
            SslStream ssl = null;
            try
            {
                s.ReceiveTimeout = s.SendTimeout = (int)(K.CONNECT_TIMEOUT * 1000);
                ssl = new SslStream(new NetworkStream(s, true), false, Validate);
                ssl.AuthenticateAsClient(host, null, Protocols(), false);
                s.ReceiveTimeout = s.SendTimeout = 0;
            }
            catch (Exception e)
            {
                try { if (ssl != null) ssl.Close(); } catch { }
                CloseSock(s);
                if (EofLike(e)) throw new SoftEOF("closed at once");
                throw new NotUs("TLS failed: " + U.ErrText(e));
            }
            return new StreamConn(s, ssl, label, null);
        }

        public static StreamConn PlainConn(Socket s, string label)
        {
            return new StreamConn(s, new NetworkStream(s, true), label, null);
        }

        public static byte[] HttpHead(List<string> lines)
        {
            return U.Latin1Bytes(string.Join("\r\n", lines.ToArray()) + "\r\n\r\n");
        }

        public static void ParseHead(byte[] raw, out string[] first, out Dictionary<string, string> headers)
        {
            string text = U.Latin1(raw, 0, raw.Length);
            string[] lines = text.Split(new string[] { "\r\n" }, StringSplitOptions.None);
            first = lines[0].Split(' ');
            headers = new Dictionary<string, string>();
            for (int i = 1; i < lines.Length; i++)
            {
                int c = lines[i].IndexOf(':');
                if (c > 0) headers[lines[i].Substring(0, c).Trim().ToLowerInvariant()] = lines[i].Substring(c + 1).Trim();
            }
        }

        public static string H(Dictionary<string, string> h, string k)
        {
            string v;
            return h.TryGetValue(k, out v) ? v : "";
        }

        public static string HostHeader(string host, int port, bool tls)
        {
            string h = host.Contains(":") ? "[" + host + "]" : host;
            if ((tls && port == 443) || (!tls && port == 80)) return h;
            return h + ":" + port;
        }

        public static IConn WsClient(string host, int port, bool tls)
        {
            StreamConn bc = tls ? ConnectTls(host, port, "ws") : PlainConn(ConnectTcp(host, port), "ws");
            try
            {
                string key = Convert.ToBase64String(U.RandBytes(16));
                List<string> lines = new List<string> {
                    "GET /fs/ws HTTP/1.1", "Host: " + HostHeader(host, port, tls),
                    "Upgrade: websocket", "Connection: Upgrade",
                    "Sec-WebSocket-Key: " + key, "Sec-WebSocket-Version: 13", K.MARK_HEADER + ": 1" };
                foreach (string[] kv in K.TUNNEL_HEADERS) lines.Add(kv[0] + ": " + kv[1]);
                byte[] req = HttpHead(lines);
                bc.Send(req, 0, req.Length);
                Reader rd = new Reader(bc);
                byte[] head;
                try { head = rd.Until(Encoding.ASCII.GetBytes("\r\n\r\n"), 65536, U.Now() + K.CONNECT_TIMEOUT); }
                catch (EndOfStreamException)
                {
                    if (rd.Buf.Count == 0) throw new SoftEOF("closed at once");
                    throw new NotUs("closed");
                }
                string[] first; Dictionary<string, string> headers;
                ParseHead(head, out first, out headers);
                if (first.Length >= 2 && (first[1] == "502" || first[1] == "503" || first[1] == "504"))
                    throw new TunnelNoApp("tunnel is up, app not running yet");
                if (first.Length < 2 || first[1] != "101" || H(headers, "sec-websocket-accept") != U.WsAccept(key))
                    throw new NotUs("no websocket");
                return new WSConn(bc, true, tls ? "WebSocket over HTTPS" : "WebSocket over HTTP", rd.Buf.ToArray());
            }
            catch
            {
                bc.Close();
                throw;
            }
        }
    }

    // ======================================================================
    // Connections: a byte pipe with Send()/Recv()/Close()
    // ======================================================================
    interface IConn
    {
        string Label { get; }
        double PingInterval { get; }
        double DeadAfter { get; }
        double HandshakeTimeout { get; }
        void Send(byte[] b, int off, int n);
        /// data = bytes, empty array = closed, null = nothing yet
        byte[] Recv(double timeout);
        void Close();
        bool IsClosed { get; }
    }

    static class Bytes { public static readonly byte[] Empty = new byte[0]; }

    /// Plain or TLS socket. A background reader thread fills a queue, so reading and
    /// writing never block each other.
    class StreamConn : IConn
    {
        readonly Socket sock;
        readonly Stream stream;
        readonly string label;
        readonly object wl = new object();
        readonly object ql = new object();
        readonly Queue<byte[]> inq = new Queue<byte[]>();
        long inBytes;
        bool eof;
        volatile bool closed;
        bool started;

        public StreamConn(Socket sock, Stream stream, string label, byte[] pre)
        {
            this.sock = sock;
            this.stream = stream;
            this.label = label;
            if (pre != null && pre.Length > 0) { inq.Enqueue(pre); inBytes = pre.Length; }
        }

        public string Label { get { return label; } }
        public double PingInterval { get { return K.PING_INTERVAL; } }
        public double DeadAfter { get { return K.DEAD_AFTER; } }
        public double HandshakeTimeout { get { return 6.0; } }
        public bool IsClosed { get { return closed; } }

        void EnsureReader()
        {
            if (started) return;
            started = true;
            U.StartThread(ReadLoop);
        }

        void ReadLoop()
        {
            byte[] buf = new byte[K.RECV_SIZE];
            try
            {
                while (!closed)
                {
                    int n = stream.Read(buf, 0, buf.Length);
                    if (n <= 0) break;
                    byte[] chunk = new byte[n];
                    Buffer.BlockCopy(buf, 0, chunk, 0, n);
                    lock (ql)
                    {
                        inq.Enqueue(chunk);
                        inBytes += n;
                        Monitor.PulseAll(ql);
                        while (inBytes > 8 * 1024 * 1024 && !closed) Monitor.Wait(ql, 1000);
                    }
                }
            }
            catch { }
            lock (ql) { eof = true; Monitor.PulseAll(ql); }
        }

        public byte[] Recv(double timeout)
        {
            lock (ql)
            {
                EnsureReader();
                if (inq.Count == 0 && !eof && !closed && timeout > 0) Monitor.Wait(ql, (int)(timeout * 1000));
                if (inq.Count > 0)
                {
                    byte[] r;
                    if (inq.Count == 1) r = inq.Dequeue();
                    else
                    {
                        r = new byte[inBytes];
                        int o = 0;
                        while (inq.Count > 0) { byte[] c = inq.Dequeue(); Buffer.BlockCopy(c, 0, r, o, c.Length); o += c.Length; }
                    }
                    inBytes = 0;
                    Monitor.PulseAll(ql);
                    return r;
                }
                return (eof || closed) ? Bytes.Empty : null;
            }
        }

        public void Send(byte[] b, int off, int n)
        {
            lock (wl)
            {
                if (closed) throw new ClosedEx("closed");
                try { stream.Write(b, off, n); }
                catch (Exception e) { throw new ClosedEx(U.ErrText(e)); }
            }
        }

        public bool TrySendNow(byte[] b)
        {
            if (!Monitor.TryEnter(wl)) return false;
            try { if (!closed) stream.Write(b, 0, b.Length); return true; }
            catch { return false; }
            finally { Monitor.Exit(wl); }
        }

        public void Close()
        {
            closed = true;
            Net.CloseSock(sock);
            try { stream.Close(); } catch { }
            lock (ql) Monitor.PulseAll(ql);
        }
    }

    /// Blocking-style helpers over a conn's Recv(), with a deadline.
    class Reader
    {
        readonly IConn conn;
        public readonly ByteBuf Buf = new ByteBuf();

        public Reader(IConn conn) { this.conn = conn; }

        void Fill(double deadline)
        {
            while (true)
            {
                double left = deadline - U.Now();
                if (left <= 0) throw new NotUs("no reply");
                byte[] d = conn.Recv(Math.Min(1.0, left));
                if (d == null) continue;
                if (d.Length == 0) throw new EndOfStreamException("closed");
                Buf.Append(d);
                return;
            }
        }

        public byte[] Until(byte[] delim, int limit, double deadline)
        {
            while (true)
            {
                int i = Buf.IndexOf(delim);
                if (i >= 0) return Buf.Take(i + delim.Length);
                if (Buf.Count > limit) throw new NotUs("header too large");
                Fill(deadline);
            }
        }

        public byte[] Exact(int n, double deadline)
        {
            while (Buf.Count < n) Fill(deadline);
            return Buf.Take(n);
        }
    }

    /// WebSocket (RFC 6455) used as a plain byte pipe; works through HTTP(S) tunnels.
    class WSConn : IConn
    {
        readonly StreamConn bc;
        readonly bool client;
        readonly string label;
        readonly ByteBuf buf = new ByteBuf();
        readonly object wlock = new object();
        readonly byte[] key = U.RandBytes(4);
        volatile bool closed;

        public WSConn(StreamConn bc, bool client, string label, byte[] pre)
        {
            this.bc = bc;
            this.client = client;
            this.label = label;
            if (pre != null) buf.Append(pre);
        }

        public string Label { get { return label; } }
        public double PingInterval { get { return K.PING_INTERVAL; } }
        public double DeadAfter { get { return K.DEAD_AFTER; } }
        public double HandshakeTimeout { get { return 6.0; } }
        public bool IsClosed { get { return closed || bc.IsClosed; } }

        byte[] Frame(int opcode, byte[] p, int off, int n)
        {
            int hl = n < 126 ? 2 : (n < 65536 ? 4 : 10);
            int ml = client ? 4 : 0;
            byte[] f = new byte[hl + ml + n];
            f[0] = (byte)(0x80 | opcode);
            byte mbit = (byte)(client ? 0x80 : 0);
            if (n < 126) f[1] = (byte)(mbit | n);
            else if (n < 65536) { f[1] = (byte)(mbit | 126); f[2] = (byte)(n >> 8); f[3] = (byte)n; }
            else { f[1] = (byte)(mbit | 127); U.PutU64(f, 2, (ulong)n); }
            int o = hl;
            if (client)
            {
                Buffer.BlockCopy(key, 0, f, o, 4);
                o += 4;
                for (int i = 0; i < n; i++) f[o + i] = (byte)(p[off + i] ^ key[i & 3]);
            }
            else Buffer.BlockCopy(p, off, f, o, n);
            return f;
        }

        void SendFrame(int opcode, byte[] p, int off, int n)
        {
            byte[] f = Frame(opcode, p, off, n);
            lock (wlock) bc.Send(f, 0, f.Length);
        }

        public void Send(byte[] b, int off, int n)
        {
            for (int i = 0; i < n; i += K.WS_MAX_FRAME)
                SendFrame(2, b, off + i, Math.Min(K.WS_MAX_FRAME, n - i));
        }

        // returns: null = need more bytes, Empty = control frame handled, CLOSED marker, or payload
        static readonly byte[] CLOSED = new byte[0];

        byte[] ParseOne()
        {
            if (buf.Count < 2) return null;
            long n = buf[1] & 0x7F;
            int pos = 2;
            if (n == 126)
            {
                if (buf.Count < 4) return null;
                n = (buf[2] << 8) | buf[3];
                pos = 4;
            }
            else if (n == 127)
            {
                if (buf.Count < 10) return null;
                n = 0;
                for (int i = 0; i < 8; i++) n = (n << 8) | buf[2 + i];
                pos = 10;
            }
            if (n < 0 || n > K.MAX_FRAME) throw new IOException("websocket frame too large");
            bool masked = (buf[1] & 0x80) != 0;
            if (masked) pos += 4;
            if (buf.Count < pos + n) return null;
            int opcode = buf[0] & 0x0F;
            byte[] payload = new byte[n];
            int s = buf.Start + pos;
            if (masked)
            {
                int ks = buf.Start + pos - 4;
                byte k0 = buf.Data[ks], k1 = buf.Data[ks + 1], k2 = buf.Data[ks + 2], k3 = buf.Data[ks + 3];
                byte[] km = { k0, k1, k2, k3 };
                for (int i = 0; i < n; i++) payload[i] = (byte)(buf.Data[s + i] ^ km[i & 3]);
            }
            else Buffer.BlockCopy(buf.Data, s, payload, 0, (int)n);
            buf.Consume(pos + (int)n);
            if (opcode == 0 || opcode == 1 || opcode == 2) return payload.Length > 0 ? payload : Bytes.Empty;
            if (opcode == 8)
            {
                try { SendFrame(8, payload, 0, Math.Min(2, payload.Length)); } catch { }
                return CLOSED;
            }
            if (opcode == 9)
            {
                try { SendFrame(10, payload, 0, payload.Length); } catch { }
            }
            return Bytes.Empty;
        }

        public byte[] Recv(double timeout)
        {
            List<byte[]> outl = new List<byte[]>();
            int total = 0;
            for (int round = 0; round < 2; round++)
            {
                while (true)
                {
                    byte[] r = ParseOne();
                    if (r == null) break;
                    if (ReferenceEquals(r, CLOSED))
                    {
                        closed = true;
                        return total > 0 ? Join(outl, total) : Bytes.Empty;
                    }
                    if (r.Length > 0) { outl.Add(r); total += r.Length; }
                }
                if (total > 0) return Join(outl, total);
                if (round == 1) return null;
                byte[] d = bc.Recv(timeout);
                if (d == null) return null;
                if (d.Length == 0) return Bytes.Empty;
                buf.Append(d);
            }
            return null;
        }

        static byte[] Join(List<byte[]> l, int total)
        {
            if (l.Count == 1) return l[0];
            byte[] r = new byte[total];
            int o = 0;
            foreach (byte[] x in l) { Buffer.BlockCopy(x, 0, r, o, x.Length); o += x.Length; }
            return r;
        }

        public void Close()
        {
            if (!closed)
            {
                closed = true;
                try
                {
                    byte[] code = { 0x03, 0xe8 };
                    if (Monitor.TryEnter(wlock))
                    {
                        try { bc.TrySendNow(Frame(8, code, 0, 2)); } finally { Monitor.Exit(wlock); }
                    }
                }
                catch { }
            }
            bc.Close();
        }
    }

    /// A tiny HTTP/1.1 client with keep-alive (for the polling transport).
    class HttpLite
    {
        readonly string host;
        readonly int port;
        readonly bool tls;
        readonly double timeout;
        Socket sock;
        Stream st;
        readonly ByteBuf buf = new ByteBuf();
        readonly byte[] rb = new byte[256 * 1024];
        public bool Dead;

        public HttpLite(string host, int port, bool tls, double timeout)
        {
            this.host = host; this.port = port; this.tls = tls; this.timeout = timeout;
        }

        public void Connect()
        {
            if (st != null) return;
            if (tls)
            {
                StreamConn sc = null;
                Socket s = Net.ConnectTcp(host, port);
                SslStream ssl = null;
                try
                {
                    s.ReceiveTimeout = s.SendTimeout = (int)(K.CONNECT_TIMEOUT * 1000);
                    ssl = new SslStream(new NetworkStream(s, true), false, Net.Validate);
                    ssl.AuthenticateAsClient(host, null, Net.Protocols(), false);
                }
                catch (Exception e)
                {
                    try { if (ssl != null) ssl.Close(); } catch { }
                    Net.CloseSock(s);
                    if (Net.EofLike(e)) throw new SoftEOF("closed at once");
                    throw new NotUs("TLS failed: " + U.ErrText(e));
                }
                sock = s; st = ssl;
            }
            else
            {
                sock = Net.ConnectTcp(host, port);
                st = new NetworkStream(sock, true);
            }
            sock.ReceiveTimeout = sock.SendTimeout = (int)(timeout * 1000);
        }

        void Fill()
        {
            int n = st.Read(rb, 0, rb.Length);
            if (n <= 0) throw new EndOfStreamException("closed");
            buf.Append(rb, 0, n);
        }

        byte[] Line(int limit)
        {
            byte[] crlf = { 13, 10 };
            while (true)
            {
                int i = buf.IndexOf(crlf);
                if (i >= 0) return buf.Take(i + 2);
                if (buf.Count > limit) throw new IOException("line too long");
                Fill();
            }
        }

        byte[] Exact(long n)
        {
            if (n > K.MAX_FRAME + 1024 * 1024) throw new IOException("body too large");
            while (buf.Count < n) Fill();
            return buf.Take((int)n);
        }

        /// Returns status, the mark header, X-Seq, body. Throws on any failure (connection then unusable).
        public int Request(string method, string path, byte[] body, out string mark, out string seq, out byte[] data, out bool gotAny)
        {
            gotAny = false;
            Connect();
            List<string> lines = new List<string> { method + " " + path + " HTTP/1.1", "Host: " + Net.HostHeader(host, port, tls) };
            foreach (string[] kv in K.TUNNEL_HEADERS) lines.Add(kv[0] + ": " + kv[1]);
            lines.Add(K.MARK_HEADER + ": 1");
            lines.Add("Content-Type: application/octet-stream");
            lines.Add("Content-Length: " + (body == null ? 0 : body.Length));
            lines.Add("Accept-Encoding: identity");
            lines.Add("Connection: keep-alive");
            byte[] head = Net.HttpHead(lines);
            if (body != null && body.Length > 0 && body.Length < 65536) head = U.Concat(head, body);
            else if (body != null && body.Length > 0) { st.Write(head, 0, head.Length); head = body; }
            st.Write(head, 0, head.Length);
            st.Flush();
            int status;
            Dictionary<string, string> h;
            while (true)
            {
                if (buf.Count == 0) Fill();
                gotAny = true;
                byte[] hb;
                byte[] dcrlf = { 13, 10, 13, 10 };
                while (true)
                {
                    int i = buf.IndexOf(dcrlf);
                    if (i >= 0) { hb = buf.Take(i + 4); break; }
                    if (buf.Count > 65536) throw new IOException("header too large");
                    Fill();
                }
                string[] first;
                Net.ParseHead(hb, out first, out h);
                if (first.Length < 2 || !int.TryParse(first[1], out status)) throw new IOException("bad response");
                if (status >= 100 && status < 200) continue;     // 100 Continue
                break;
            }
            mark = h.ContainsKey(K.MARK_HEADER.ToLowerInvariant()) ? h[K.MARK_HEADER.ToLowerInvariant()] : null;
            seq = h.ContainsKey("x-seq") ? h["x-seq"] : null;
            bool close = Net.H(h, "connection").ToLowerInvariant().Contains("close");
            if (method == "HEAD" || status == 204 || status == 304) data = Bytes.Empty;
            else if (Net.H(h, "transfer-encoding").ToLowerInvariant().Contains("chunked"))
            {
                ByteBuf outb = new ByteBuf();
                while (true)
                {
                    string ln = Encoding.ASCII.GetString(Line(1024)).Split(';')[0].Trim();
                    long size = Convert.ToInt64(ln.Length > 0 ? ln : "0", 16);
                    if (size == 0)
                    {
                        while (Line(8192).Length != 2) { }
                        break;
                    }
                    if (outb.Count + size > K.MAX_FRAME) throw new IOException("body too large");
                    outb.Append(Exact(size));
                    Exact(2);
                }
                data = outb.ToArray();
            }
            else if (h.ContainsKey("content-length"))
            {
                long n = long.Parse(h["content-length"].Trim());
                data = n > 0 ? Exact(n) : Bytes.Empty;
            }
            else
            {
                // body until the server closes
                try { while (true) { Fill(); if (buf.Count > K.MAX_FRAME) break; } } catch (EndOfStreamException) { }
                data = buf.ToArray();
                buf.Consume(buf.Count);
                close = true;
            }
            if (close) Close();
            return status;
        }

        public void Close()
        {
            Dead = true;
            Net.CloseSock(sock);
            try { if (st != null) st.Close(); } catch { }
            st = null; sock = null;
            buf.Consume(buf.Count);
        }
    }

    /// Last-resort transport: ordinary HTTP requests (long polling), reliable and ordered.
    class PollClientConn : IConn
    {
        readonly string host;
        readonly int port;
        readonly bool tls;
        readonly string label;
        readonly object cv = new object();
        readonly ByteBuf outbuf = new ByteBuf();
        readonly Queue<byte[]> inq = new Queue<byte[]>();
        volatile bool closed;
        string sid;

        public PollClientConn(string host, int port, bool tls)
        {
            this.host = host; this.port = port; this.tls = tls;
            label = tls ? "HTTPS polling" : "HTTP polling";
        }

        public string Label { get { return label; } }
        public double PingInterval { get { return K.POLL_PING_INTERVAL; } }
        public double DeadAfter { get { return K.POLL_DEAD_AFTER; } }
        public double HandshakeTimeout { get { return K.HANDSHAKE_TIMEOUT; } }
        public bool IsClosed { get { return closed; } }

        public PollClientConn Open()
        {
            HttpLite c = new HttpLite(host, port, tls, K.CONNECT_TIMEOUT);
            try
            {
                c.Connect();
                int status; string mark, seq; byte[] data; bool gotAny = false;
                try { status = c.Request("POST", "/fs/p/new", null, out mark, out seq, out data, out gotAny); }
                catch (Exception e)
                {
                    if (!gotAny && Net.EofLike(e)) throw new SoftEOF("closed at once");
                    throw new NotUs("not file-share: " + U.ErrText(e));
                }
                if (status == 502 || status == 503 || status == 504) throw new TunnelNoApp("tunnel is up, app not running yet");
                string text = Encoding.ASCII.GetString(data).Trim();
                if (status != 200 || mark != "1" || !Regex.IsMatch(text, "^[0-9a-f]{16,64}$")) throw new NotUs("not file-share");
                sid = text;
            }
            finally { c.Close(); }
            U.StartThread(UpLoop);
            U.StartThread(DownLoop);
            return this;
        }

        delegate int Req(HttpLite c, out string seq, out byte[] data);

        /// Repeat one request until it succeeds; give up after POLL_GIVE_UP of failures.
        HttpLite Retry(HttpLite conn, Req fn, out string seq, out byte[] data, bool firstTry)
        {
            double failSince = -1;
            seq = null; data = null;
            while (!closed)
            {
                try
                {
                    if (conn == null) conn = new HttpLite(host, port, tls, K.POLL_WAIT + 20);
                    int status = fn(conn, out seq, out data);
                    if (status == 200) return conn;
                    if (status == 404 || status == 410) break;
                    throw new IOException("HTTP " + status);
                }
                catch (Exception)
                {
                    if (conn != null) conn.Close();
                    conn = null;
                    if (firstTry) { firstTry = false; continue; }   // a stale keep-alive connection: retry at once
                    double now = U.Now();
                    if (failSince < 0) failSince = now;
                    if (now - failSince > K.POLL_GIVE_UP) break;
                    Thread.Sleep(500);
                }
            }
            if (conn != null) conn.Close();
            Close();
            return null;
        }

        void UpLoop()
        {
            long seqNo = 0;
            HttpLite conn = null;
            while (!closed)
            {
                byte[] batch;
                lock (cv)
                {
                    while (outbuf.Count == 0 && !closed) Monitor.Wait(cv, 1000);
                    if (closed) break;
                    int n = Math.Min(outbuf.Count, K.POLL_BATCH);
                    batch = new byte[n];
                    Buffer.BlockCopy(outbuf.Data, outbuf.Start, batch, 0, n);
                }
                seqNo++;
                string path = "/fs/p/" + sid + "/up?seq=" + seqNo;
                string s; byte[] d;
                conn = Retry(conn, delegate(HttpLite c, out string sq, out byte[] dt) {
                    string m; bool g;
                    return c.Request("POST", path, batch, out m, out sq, out dt, out g);
                }, out s, out d, conn != null);
                if (conn == null) break;
                lock (cv)
                {
                    outbuf.Consume(batch.Length);
                    Monitor.PulseAll(cv);
                }
            }
            if (conn != null) conn.Close();
        }

        void DownLoop()
        {
            long last = 0;
            HttpLite conn = null;
            while (!closed)
            {
                string path = "/fs/p/" + sid + "/down?ack=" + last;
                string seq; byte[] data;
                conn = Retry(conn, delegate(HttpLite c, out string sq, out byte[] dt) {
                    string m; bool g;
                    return c.Request("GET", path, null, out m, out sq, out dt, out g);
                }, out seq, out data, conn != null);
                if (conn == null) break;
                long sv = 0;
                long.TryParse(seq ?? "0", out sv);
                if (sv == last + 1)
                {
                    last = sv;
                    if (data != null && data.Length > 0)
                    {
                        lock (cv) { inq.Enqueue(data); Monitor.PulseAll(cv); }
                    }
                }
            }
            if (conn != null) conn.Close();
        }

        public void Send(byte[] b, int off, int n)
        {
            lock (cv)
            {
                while (outbuf.Count > K.POLL_BATCH * 2 && !closed) Monitor.Wait(cv, 1000);
                if (closed) throw new ClosedEx("closed");
                outbuf.Append(b, off, n);
                Monitor.PulseAll(cv);
            }
        }

        public byte[] Recv(double timeout)
        {
            lock (cv)
            {
                if (inq.Count == 0 && !closed && timeout > 0) Monitor.Wait(cv, (int)(timeout * 1000));
                if (inq.Count > 0) return DrainQ(inq);
                return closed ? Bytes.Empty : null;
            }
        }

        public static byte[] DrainQ(Queue<byte[]> q)
        {
            if (q.Count == 1) return q.Dequeue();
            int total = q.Sum(x => x.Length);
            byte[] r = new byte[total];
            int o = 0;
            while (q.Count > 0) { byte[] x = q.Dequeue(); Buffer.BlockCopy(x, 0, r, o, x.Length); o += x.Length; }
            return r;
        }

        public void Close()
        {
            lock (cv)
            {
                if (closed) return;
                closed = true;
                Monitor.PulseAll(cv);
            }
            string s = sid;
            if (s != null)
            {
                U.StartThread(delegate {
                    try
                    {
                        HttpLite c = new HttpLite(host, port, tls, 3);
                        string m, q; byte[] d; bool g;
                        c.Request("POST", "/fs/p/" + s + "/close", null, out m, out q, out d, out g);
                        c.Close();
                    }
                    catch { }
                });
            }
        }
    }

    /// Server side of the HTTP polling transport.
    class PollServerConn : IConn
    {
        public readonly string Sid = U.RandHex(16);
        readonly string label;
        readonly object cv = new object();
        readonly Queue<byte[]> inq = new Queue<byte[]>();
        readonly ByteBuf down = new ByteBuf();
        long upExpected = 1;
        long downSeq = 0;
        long lastBatchSeq = -1;
        byte[] lastBatch;
        volatile bool closed;
        double lastSeen = U.Now();

        public PollServerConn(string label) { this.label = label; }

        public string Label { get { return label; } }
        public double PingInterval { get { return K.POLL_PING_INTERVAL; } }
        public double DeadAfter { get { return K.POLL_DEAD_AFTER; } }
        public double HandshakeTimeout { get { return K.HANDSHAKE_TIMEOUT; } }
        public bool IsClosed { get { return closed; } }

        public int HandleUp(long seq, byte[] body)
        {
            lock (cv)
            {
                lastSeen = U.Now();
                if (closed) return 410;
                if (seq == upExpected)
                {
                    if (body.Length > 0) inq.Enqueue(body);
                    upExpected++;
                    Monitor.PulseAll(cv);
                }
                return 200;   // older seq = duplicate retry, already have it
            }
        }

        public int HandleDown(long ack, out long seq, out byte[] data)
        {
            lock (cv)
            {
                lastSeen = U.Now();
                seq = 0; data = Bytes.Empty;
                if (closed) return 410;
                if (lastBatch != null && ack >= lastBatchSeq) lastBatch = null;
                if (lastBatch != null) { seq = lastBatchSeq; data = lastBatch; return 200; }
                double end = U.Now() + K.POLL_WAIT;
                while (down.Count == 0 && lastBatch == null && !closed)
                {
                    double left = end - U.Now();
                    if (left <= 0) break;
                    Monitor.Wait(cv, (int)(Math.Min(left, 1.0) * 1000) + 1);
                }
                lastSeen = U.Now();
                if (closed) return 410;
                if (lastBatch != null) { seq = lastBatchSeq; data = lastBatch; return 200; }
                if (down.Count == 0) return 200;
                int n = Math.Min(down.Count, K.POLL_BATCH);
                data = down.Take(n);
                downSeq++;
                lastBatchSeq = downSeq;
                lastBatch = data;
                seq = downSeq;
                Monitor.PulseAll(cv);
                return 200;
            }
        }

        public void Send(byte[] b, int off, int n)
        {
            lock (cv)
            {
                while (down.Count > K.POLL_BATCH * 2 && !closed) Monitor.Wait(cv, 1000);
                if (closed) throw new ClosedEx("closed");
                down.Append(b, off, n);
                Monitor.PulseAll(cv);
            }
        }

        public byte[] Recv(double timeout)
        {
            lock (cv)
            {
                if (inq.Count == 0 && !closed && timeout > 0) Monitor.Wait(cv, (int)(timeout * 1000));
                if (U.Now() - lastSeen > K.POLL_DEAD_AFTER) closed = true;
                if (inq.Count > 0) return PollClientConn.DrainQ(inq);
                return closed ? Bytes.Empty : null;
            }
        }

        public void Close()
        {
            lock (cv) { closed = true; Monitor.PulseAll(cv); }
        }
    }

    // ======================================================================
    // Session: one live connection to the other user (frames, heartbeats)
    // ======================================================================
    class Session
    {
        public readonly App app;
        public readonly IConn conn;
        public readonly bool Dialer;
        public readonly string Label;
        public string PeerId;
        public string PeerName = "other user";
        public volatile bool Alive = true;
        public volatile bool Attached;
        public double LastRx = U.Now(), LastTx = U.Now();
        public readonly object cv = new object();
        readonly Queue<byte[]> ctrlQ = new Queue<byte[]>();
        readonly LinkedList<KeyValuePair<uint, byte[]>> dataQ = new LinkedList<KeyValuePair<uint, byte[]>>();
        long dataBytes;
        readonly ByteBuf buf = new ByteBuf();
        public volatile bool Bye, Busy;
        public string PeerBusy = "";
        public bool RemotePrivate;
        static readonly byte[] PING = { K.F_PING, 0, 0, 0, 0 };

        public Session(App app, IConn conn, bool dialer)
        {
            this.app = app;
            this.conn = conn;
            Dialer = dialer;
            Label = conn.Label;
        }

        static byte[] CtrlFrame(Dictionary<string, object> msg)
        {
            byte[] payload = Encoding.UTF8.GetBytes(Json.Write(msg));
            byte[] f = new byte[5 + payload.Length];
            f[0] = K.F_CTRL;
            U.PutU32(f, 1, (uint)payload.Length);
            Buffer.BlockCopy(payload, 0, f, 5, payload.Length);
            return f;
        }

        // ---- sending: control first, file data second; the reader never blocks ----
        public bool SendCtrl(params object[] kv) { return SendMsg(Json.Obj(kv)); }

        public bool SendMsg(Dictionary<string, object> msg)
        {
            byte[] f = CtrlFrame(msg);
            lock (cv)
            {
                if (!Alive) return false;
                ctrlQ.Enqueue(f);
                Monitor.PulseAll(cv);
            }
            return true;
        }

        public const int DATA_OFF = 5 + K.DATA_HDR;

        /// frame = new byte[DATA_OFF + n] with the file bytes already at DATA_OFF.
        public bool SendData(uint tid, long offset, byte[] frame)
        {
            int n = frame.Length - DATA_OFF;
            frame[0] = K.F_DATA;
            U.PutU32(frame, 1, (uint)(K.DATA_HDR + n));
            U.PutU32(frame, 5, tid);
            U.PutU64(frame, 9, (ulong)offset);
            U.PutU32(frame, 17, U.Crc32(frame, DATA_OFF, n));
            lock (cv)
            {
                while (Alive && dataBytes > K.DATA_QUEUE_MAX) Monitor.Wait(cv, 1000);
                if (!Alive) return false;
                dataQ.AddLast(new KeyValuePair<uint, byte[]>(tid, frame));
                dataBytes += frame.Length;
                Monitor.PulseAll(cv);
            }
            return true;
        }

        public void Purge(uint tid)
        {
            lock (cv)
            {
                LinkedListNode<KeyValuePair<uint, byte[]>> node = dataQ.First;
                while (node != null)
                {
                    LinkedListNode<KeyValuePair<uint, byte[]>> next = node.Next;
                    if (node.Value.Key == tid) { dataBytes -= node.Value.Value.Length; dataQ.Remove(node); }
                    node = next;
                }
                Monitor.PulseAll(cv);
            }
        }

        void Writer()
        {
            try
            {
                while (true)
                {
                    byte[] frame;
                    lock (cv)
                    {
                        while (Alive && ctrlQ.Count == 0 && dataQ.Count == 0)
                        {
                            double left = conn.PingInterval - (U.Now() - LastTx);
                            if (left <= 0) break;
                            Monitor.Wait(cv, (int)(Math.Min(left, 1.0) * 1000) + 1);
                        }
                        if (!Alive) return;
                        if (ctrlQ.Count > 0) frame = ctrlQ.Dequeue();
                        else if (dataQ.Count > 0)
                        {
                            frame = dataQ.First.Value.Value;
                            dataQ.RemoveFirst();
                            dataBytes -= frame.Length;
                            Monitor.PulseAll(cv);
                        }
                        else frame = PING;
                    }
                    conn.Send(frame, 0, frame.Length);
                    LastTx = U.Now();
                }
            }
            catch { Kill(); }
        }

        // ---- receiving ----
        bool NextFrame(out byte ftype, out byte[] payload)
        {
            ftype = 0; payload = null;
            if (buf.Count < 5) return false;
            ftype = buf[0];
            uint n = ((uint)buf[1] << 24) | ((uint)buf[2] << 16) | ((uint)buf[3] << 8) | buf[4];
            if (n > K.MAX_FRAME || (ftype != K.F_CTRL && ftype != K.F_DATA && ftype != K.F_PING)) throw new IOException("protocol error");
            if (buf.Count < 5 + n) return false;
            buf.Consume(5);
            payload = buf.Take((int)n);
            return true;
        }

        public void Handshake(bool magicSeen)
        {
            byte[] hello = Encoding.UTF8.GetBytes(Json.Write(Json.Obj("t", "hello", "app", "file-share", "v", K.APP_VERSION,
                "id", K.INSTANCE_ID, "name", K.HOSTNAME, "busy", app.BusyWith())));
            byte[] hdr = new byte[5];
            hdr[0] = K.F_CTRL;
            U.PutU32(hdr, 1, (uint)hello.Length);
            byte[] first = U.Concat(K.MAGIC, hdr, hello);
            conn.Send(first, 0, first.Length);
            bool needMagic = !magicSeen;
            double deadline = U.Now() + conn.HandshakeTimeout;
            while (U.Now() < deadline)
            {
                byte[] data = conn.Recv(0.5);
                if (data == null) continue;
                if (data.Length == 0)
                {
                    if (buf.Count == 0 && needMagic) throw new SoftEOF("closed at once (tunnel up, but nothing behind it yet)");
                    throw new NotUs("closed during handshake");
                }
                buf.Append(data);
                if (needMagic)
                {
                    int m = Math.Min(buf.Count, K.MAGIC.Length);
                    for (int i = 0; i < m; i++) if (buf[i] != K.MAGIC[i]) throw new NotUs("not file-share");
                    if (buf.Count < K.MAGIC.Length) continue;
                    buf.Consume(K.MAGIC.Length);
                    needMagic = false;
                }
                if (buf.Count >= 5)
                {
                    uint n = ((uint)buf[1] << 24) | ((uint)buf[2] << 16) | ((uint)buf[3] << 8) | buf[4];
                    if (buf[0] != K.F_CTRL || n > 65536) throw new NotUs("bad hello");
                    if (buf.Count >= 5 + n)
                    {
                        buf.Consume(5);
                        byte[] p = buf.Take((int)n);
                        Dictionary<string, object> msg;
                        try { msg = Json.Parse(Encoding.UTF8.GetString(p)) as Dictionary<string, object>; }
                        catch { throw new NotUs("bad hello"); }
                        if (msg == null || Json.Str(msg, "t") != "hello" || Json.Str(msg, "app") != "file-share" || !Json.Truthy(Json.Get(msg, "id")))
                            throw new NotUs("bad hello");
                        PeerId = Cut(Json.Str(msg, "id"), 64);
                        string nm = Json.Str(msg, "name");
                        PeerName = Cut(nm.Length > 0 ? nm : "other user", 40);
                        PeerBusy = Json.Str(msg, "busy");
                        LastRx = U.Now();
                        return;
                    }
                }
            }
            throw new NotUs("handshake timed out");
        }

        static string Cut(string s, int n) { return s.Length > n ? s.Substring(0, n) : s; }

        public void StartWriter() { U.StartThread(Writer); }

        public void RunReader()
        {
            try
            {
                byte ft; byte[] p;
                while (Alive && NextFrame(out ft, out p)) app.OnFrame(this, ft, p);
                while (Alive)
                {
                    byte[] data = conn.Recv(1.0);
                    if (data == null) continue;
                    if (data.Length == 0) break;
                    LastRx = U.Now();
                    buf.Append(data);
                    while (Alive && NextFrame(out ft, out p)) app.OnFrame(this, ft, p);
                }
            }
            catch { }
            finally { Kill(); }
        }

        public string DialerId() { return Dialer ? K.INSTANCE_ID : (PeerId ?? ""); }

        public void Kill()
        {
            lock (cv)
            {
                if (!Alive) return;
                Alive = false;
                dataQ.Clear();
                ctrlQ.Clear();
                dataBytes = 0;
                Monitor.PulseAll(cv);
            }
            try { conn.Close(); } catch { }
            app.OnSessionEnd(this);
        }

        /// Best-effort: deliver queued control messages (e.g. 'bye') then close.
        public void FlushAndClose(double timeout)
        {
            double end = U.Now() + timeout;
            while (U.Now() < end)
            {
                lock (cv) { if (ctrlQ.Count == 0 || !Alive) break; }
                Thread.Sleep(20);
            }
            Thread.Sleep(100);
            Kill();
        }
    }

    // ======================================================================
    // Transfers
    // ======================================================================
    class FileChanged : Exception { public FileChanged(string m) : base(m) { } }

    interface ITransfer
    {
        string Name { get; }
        string Kind { get; }
        long Size { get; }
        int Num { get; }
        bool Terminal();
        int EntryCount { get; }
    }

    /// Files/folders offered with one upload command: the other user answers once for all.
    class Batch
    {
        public readonly string Id = U.RandHex(6);
        public List<OutTransfer> Members;
        readonly HashSet<string> said = new HashSet<string>();

        public Batch(List<OutTransfer> members) { Members = members; }

        public bool Once(string key) { lock (said) return said.Add(key); }

        public string Describe()
        {
            List<OutTransfer> items = Members;
            string names = string.Join(", ", items.Take(3).Select(t => U.Short(t.Name, 24)).ToArray());
            if (items.Count > 3) names += " and " + (items.Count - 3) + " more";
            return items.Count + " items (" + U.Human(items.Sum(t => t.Size)) + " total): " + names;
        }
    }

    class GoInfo
    {
        public Session S; public long Off;
        public GoInfo(Session s, long off) { S = s; Off = off; }
    }

    class FileEntry
    {
        public string Abs, Rel; public long Size; public long Mtime;
    }

    /// A file or folder this user offered.
    class OutTransfer : ITransfer
    {
        readonly App app;
        public readonly string Path_;
        public readonly uint Tid;
        string name;
        string kind;
        public List<FileEntry> Entries = new List<FileEntry>();
        public List<string> Dirs = new List<string>();
        long size;
        public string Fid = "";
        public volatile string State = "preparing";  // preparing, waiting, offered, queued, sending, done, refused, cancelled, failed
        public GoInfo Go;
        Session offerSession;
        public volatile bool AcceptedOnce;
        public long Acked, Sent;
        public readonly Meter Meter = new Meter();
        public double Started = -1;
        public int Skipped;
        public Batch Batch;
        public int Num_;
        public bool Ready;
        Session lastGoSess;

        public OutTransfer(App app, string path)
        {
            this.app = app;
            Path_ = Path.GetFullPath(path);
            Tid = app.NewTid();
            name = Path.GetFileName(Path_.TrimEnd('\\', '/'));
            if (string.IsNullOrEmpty(name)) name = "file";
            kind = Directory.Exists(U.Lp(Path_)) ? "folder" : "file";
        }

        public string Name { get { return name; } }
        public string Kind { get { return kind; } }
        public long Size { get { return size; } }
        public int Num { get { return Num_; } }
        public int EntryCount { get { return Entries.Count; } }

        public bool Terminal() { string s = State; return s == "done" || s == "refused" || s == "cancelled" || s == "failed"; }
        public bool Solo() { return Batch == null || Batch.Members.Count <= 1; }

        static long Mtime(string p)
        {
            try { return (long)(File.GetLastWriteTimeUtc(U.Lp(p)) - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds; }
            catch { return 0; }
        }

        void BuildManifest()
        {
            StringBuilder ident = new StringBuilder();
            if (kind == "file")
            {
                FileInfo fi = new FileInfo(U.Lp(Path_));
                using (FileStream f = new FileStream(U.Lp(Path_), FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete)) { }
                FileEntry e = new FileEntry { Abs = Path_, Rel = name, Size = fi.Length, Mtime = Mtime(Path_) };
                Entries = new List<FileEntry> { e };
                size = fi.Length;
                ident.Append(Path_).Append('|').Append(name).Append("|file|").Append(size).Append('|').Append(e.Mtime);
            }
            else
            {
                List<FileEntry> entries = new List<FileEntry>();
                List<string> dirs = new List<string>();
                Walk(Path_, "", entries, dirs);
                Entries = entries;
                Dirs = dirs;
                size = entries.Sum(x => x.Size);
                ident.Append(Path_).Append('|').Append(name).Append("|folder|").Append(size);
                foreach (FileEntry e in entries) ident.Append('|').Append(e.Rel).Append(':').Append(e.Size).Append(':').Append(e.Mtime);
                foreach (string d in dirs) ident.Append("|d:").Append(d);
            }
            Fid = U.Sha256Hex(ident.ToString()).Substring(0, 32);
        }

        void Walk(string dir, string relRoot, List<FileEntry> entries, List<string> dirs)
        {
            string[] subdirs, files;
            try
            {
                subdirs = Directory.GetDirectories(U.Lp(dir));
                files = Directory.GetFiles(U.Lp(dir));
            }
            catch { Skipped++; return; }
            Array.Sort(subdirs, StringComparer.Ordinal);
            Array.Sort(files, StringComparer.Ordinal);
            foreach (string f in files)
            {
                string fn = Path.GetFileName(f);
                string full = Path.Combine(dir, fn);
                try
                {
                    FileInfo fi = new FileInfo(U.Lp(full));
                    if ((fi.Attributes & FileAttributes.ReparsePoint) != 0 && !fi.Exists) continue;
                    using (FileStream s = new FileStream(U.Lp(full), FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete)) { }
                    string rel = relRoot.Length > 0 ? relRoot + "/" + fn : fn;
                    entries.Add(new FileEntry { Abs = full, Rel = rel, Size = fi.Length, Mtime = Mtime(full) });
                }
                catch { Skipped++; }
            }
            foreach (string d in subdirs)
            {
                string dn = Path.GetFileName(d);
                string full = Path.Combine(dir, dn);
                try
                {
                    if ((new DirectoryInfo(U.Lp(full)).Attributes & FileAttributes.ReparsePoint) != 0) continue;   // symlinked folder
                }
                catch { continue; }
                string rel = relRoot.Length > 0 ? relRoot + "/" + dn : dn;
                dirs.Add(rel);
                Walk(full, rel, entries, dirs);
            }
        }

        public Dictionary<string, object> OfferMsg(bool resume)
        {
            Dictionary<string, object> msg = Json.Obj("t", "offer", "tid", (long)Tid, "fid", Fid, "name", name,
                "kind", kind, "size", size, "count", Entries.Count, "resume", resume);
            if (Batch != null)
            {
                msg["batch"] = Batch.Id;
                msg["bn"] = Math.Max(1, Batch.Members.Count(t => !t.Terminal()));
            }
            if (kind == "folder")
            {
                List<object> ents = new List<object>(Entries.Count);
                foreach (FileEntry e in Entries) ents.Add(new List<object> { e.Rel, e.Size });
                msg["entries"] = ents;
                msg["dirs"] = Dirs;
            }
            return msg;
        }

        public void Run()
        {
            try { RunInner(); }
            catch (Exception exc)
            {
                if (!Terminal())
                {
                    State = "failed";
                    app.SendCancel(Tid, "up", "upload failed on the other side", null);
                    Con.S("[!] Upload of " + name + " failed: " + U.ErrText(exc));
                }
            }
            finally { app.TransferEnded(); }
        }

        /// Read the file/folder list. Returns false (and says why) when it cannot be sent.
        public bool Prepare()
        {
            try { BuildManifest(); }
            catch (Exception exc)
            {
                State = "failed";
                Con.S("[!] Cannot read " + Path_ + ": " + U.ErrText(exc));
                return false;
            }
            if (Skipped > 0) Con.S("[!] " + Skipped + " unreadable item(s) in " + name + " will be skipped.");
            if (Json.Write(OfferMsg(false)).Length > K.MAX_FRAME - 4096)
            {
                State = "failed";
                Con.S("[!] " + name + " has too many files to send as one folder. Zip it first, then upload the zip.");
                return false;
            }
            Ready = true;
            State = "waiting";
            return true;
        }

        public string What()
        {
            return kind == "folder" ? "folder, " + Entries.Count + " files" : "file";
        }

        void RunInner()
        {
            if (!Ready && !Prepare()) return;
            if (app.Session == null && Solo())
                Con.S("[*] " + name + " (" + What() + ", " + U.Human(size) + ") will be offered as soon as the other user is connected.");
            while (!Terminal())
            {
                Session sess = app.Session;
                if (sess == null || !sess.Alive)
                {
                    if (State == "offered" || State == "sending" || State == "queued") State = "waiting";
                    app.WaitSession(1.0);
                    continue;
                }
                if (offerSession != sess)
                {
                    lock (this) Go = null;
                    offerSession = sess;
                    if (!sess.SendMsg(OfferMsg(AcceptedOnce))) continue;
                    if (State == "waiting" && !AcceptedOnce && Solo())
                        Con.S("[*] Offered " + name + " (" + What() + ", " + U.Human(size) + "). Waiting for the other user to answer...");
                    State = !AcceptedOnce ? "offered" : "waiting";
                }
                GoInfo g;
                lock (this)
                {
                    while (!Terminal() && Go == null && sess.Alive && app.Session == sess) Monitor.Wait(this, 500);
                    if (Terminal() || Go == null) continue;
                    g = Go;
                    Go = null;
                }
                if (g.S != sess) continue;
                long offset = g.Off;
                State = "queued";
                if (!app.AcquireSlot(this, sess)) continue;
                try
                {
                    lock (this)
                    {
                        if (Go != null && Go.S == sess) { offset = Go.Off; Go = null; }   // a newer resume point arrived while queued
                    }
                    if (Terminal()) continue;
                    State = "sending";
                    StreamData(sess, offset);
                }
                finally { app.ReleaseSlot(this); }
                // all bytes queued: wait for 'complete', a resend request, or a lost link
                lock (this)
                {
                    while (!Terminal() && Go == null && sess.Alive && app.Session == sess) Monitor.Wait(this, 500);
                }
            }
        }

        void Locate(long offset, out int idx, out long pos)
        {
            pos = offset;
            for (int i = 0; i < Entries.Count; i++)
            {
                if (pos < Entries[i].Size) { idx = i; return; }
                pos -= Entries[i].Size;
            }
            idx = Entries.Count; pos = 0;
        }

        void StreamData(Session sess, long offset)
        {
            int idx; long pos;
            Locate(offset, out idx, out pos);
            FileStream f = null;
            try
            {
                while (offset < size)
                {
                    if (Terminal() || Go != null || !sess.Alive) return;
                    FileEntry e = Entries[idx];
                    if (pos >= e.Size)
                    {
                        if (f != null) { f.Close(); f = null; }
                        idx++; pos = 0;
                        continue;
                    }
                    if (f == null)
                    {
                        f = new FileStream(U.Lp(e.Abs), FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete, 1 << 20, FileOptions.SequentialScan);
                        if (pos > 0) f.Position = pos;
                    }
                    int n = (int)Math.Min(K.CHUNK, e.Size - pos);
                    byte[] frame = new byte[Session.DATA_OFF + n];
                    int got = 0;
                    while (got < n)
                    {
                        int r = f.Read(frame, Session.DATA_OFF + got, n - got);
                        if (r <= 0) break;
                        got += r;
                    }
                    if (got != n) throw new FileChanged(Path.GetFileName(e.Abs) + " changed while it was being sent");
                    if (!sess.SendData(Tid, offset, frame)) return;
                    offset += n;
                    pos += n;
                    Sent = offset;
                }
            }
            finally { if (f != null) f.Close(); }
        }

        // ---- events from the other user ----
        public void OnAnswer(Session sess, bool ok, object offsetObj, string reason)
        {
            if (Terminal()) return;
            if (!ok)
            {
                State = "refused";
                string extra = (reason.Length > 0 && reason != "refused") ? " (" + reason + ")" : "";
                if (Solo()) Con.S("[x] The other user refused to download " + name + "." + extra);
                else if (Batch.Once("refused" + extra)) Con.S("[x] The other user refused to download " + Batch.Describe() + "." + extra);
                lock (this) Monitor.PulseAll(this);
                return;
            }
            long offset;
            try { offset = Math.Max(0, Math.Min(Json.Long(offsetObj), size)); } catch { offset = 0; }
            if (!AcceptedOnce)
            {
                AcceptedOnce = true;
                Started = U.Now();
                Acked = offset;
                if (!Solo())
                {
                    if (Batch.Once("accepted")) Con.S("[+] Receiver accepted, files are uploading: " + Batch.Describe());
                }
                else if (offset > 0)
                    Con.S("[+] Receiver accepted. Resuming " + name + " from " + U.F1(100.0 * offset / Math.Max(1, size)) + "% (" + U.Human(offset) + " already there).");
                else
                    Con.S("[+] Receiver accepted, file is uploading: " + name + " (" + U.Human(size) + ")");
            }
            else if (!Solo())
            {
                if (sess != lastGoSess && Batch.Once("resumed" + sess.GetHashCode())) Con.S("[+] Uploads resumed.");
            }
            else if (sess != lastGoSess)
                Con.S("[+] Upload of " + name + " resumed at " + U.F1(100.0 * offset / Math.Max(1, size)) + "%.");
            lastGoSess = sess;
            Meter.Reset();
            sess.Purge(Tid);
            lock (this)
            {
                Go = new GoInfo(sess, offset);
                Monitor.PulseAll(this);
            }
        }

        public void OnProgress(object got)
        {
            try { Acked = Math.Max(0, Math.Min(Json.Long(got), size)); } catch { return; }
            Meter.Add(Acked);
        }

        public void OnComplete()
        {
            if (Terminal()) return;
            State = "done";
            Acked = size;
            double took = U.Now() - (Started >= 0 ? Started : U.Now());
            string speed = took > 0.5 ? " at " + U.Human(size / took) + "/s" : "";
            if (Solo())
                Con.S("[+] Upload complete: " + name + " (" + U.Human(size) + ") in " + U.FmtEta(took) + speed + ". The other user saved it.");
            else
                Con.S("[+] Upload complete: " + name + " (" + U.Human(size) + ")" + speed);
            lock (this) Monitor.PulseAll(this);
            if (!Solo() && Batch.Members.All(t => t.Terminal()) && Batch.Once("finished"))
            {
                int done = Batch.Members.Count(t => t.State == "done");
                Con.S("[+] Finished: " + done + " of " + Batch.Members.Count + " items from that upload were sent.");
            }
        }

        public void OnRemoteCancel(string reason)
        {
            if (Terminal()) return;
            bool wasOffer = !AcceptedOnce;
            State = "cancelled";
            if (reason.Length > 0) Con.S("[x] Upload of " + name + " stopped: " + reason + ".");
            else if (wasOffer) Con.S("[x] The other user refused to download " + name + ".");
            else Con.S("[x] File uploading stopped because the other user interrupted it (" + name + ").");
            lock (this) Monitor.PulseAll(this);
        }

        public bool CancelLocal()
        {
            if (Terminal()) return false;
            bool wasActive = AcceptedOnce;
            State = "cancelled";
            Session sess = app.Session;
            if (sess != null) sess.Purge(Tid);
            app.SendCancel(Tid, "up", "", null);
            if (wasActive) Con.S("[x] You interrupted the uploading of " + name + ".");
            else Con.S("[x] You withdrew the offer of " + name + ".");
            lock (this) Monitor.PulseAll(this);
            return true;
        }
    }

    /// A file or folder the other user offered to this user.
    class InTransfer : ITransfer
    {
        readonly App app;
        public readonly string PeerId;
        public readonly uint Tid;
        public readonly string Fid;
        readonly string kind;
        readonly string name;
        readonly long size;
        public readonly List<KeyValuePair<string, long>> Entries = new List<KeyValuePair<string, long>>();
        public readonly List<string> Dirs = new List<string>();
        public volatile string State = "asking";      // asking, active, done, refused, cancelled, failed
        public long Written;
        int idx;
        long pos;
        public FileStream F;
        bool opened;
        double lastProgress;
        bool resyncSent;
        public readonly Meter Meter = new Meter();
        public double Started = -1;
        public long Have;
        public int Num_;
        public OfferGroup Group;
        public readonly object Lock = new object();
        readonly string part, partRoot;

        public InTransfer(App app, string peerId, Dictionary<string, object> msg)
        {
            this.app = app;
            PeerId = peerId;
            Tid = (uint)Json.Long(Json.Get(msg, "tid"));
            string fid = Regex.Replace(Json.Str(msg, "fid").ToLowerInvariant(), "[^0-9a-f]", "");
            if (fid.Length > 64) fid = fid.Substring(0, 64);
            Fid = fid.Length > 0 ? fid : U.RandHex(8);
            kind = Json.Str(msg, "kind") == "folder" ? "folder" : "file";
            string nm = Json.Str(msg, "name");
            name = U.SafeComponent(nm.Length > 0 ? nm : "file");
            object so = Json.Get(msg, "size");
            if (so == null) throw new FormatException("no size");
            size = Json.Long(so);
            if (size < 0) throw new FormatException("bad size");
            if (kind == "folder")
            {
                HashSet<string> seen = new HashSet<string>();
                long total = 0;
                List<object> ents = Json.Get(msg, "entries") as List<object> ?? new List<object>();
                foreach (object item in ents)
                {
                    List<object> it = (List<object>)item;
                    string orig = Convert.ToString(it[0]);
                    string rel = U.SafeRelPath(orig);
                    long sz = Json.Long(it[1]);
                    if (sz < 0) throw new FormatException("bad size");
                    string key = rel.ToLowerInvariant();
                    int n = 1;
                    while (seen.Contains(key))
                    {
                        string baseRel = U.SafeRelPath(orig);
                        string ext = U.Ext(baseRel);
                        rel = baseRel.Substring(0, baseRel.Length - ext.Length) + " (" + n + ")" + ext;
                        key = rel.ToLowerInvariant();
                        n++;
                    }
                    seen.Add(key);
                    Entries.Add(new KeyValuePair<string, long>(rel, sz));
                    total += sz;
                }
                if (total != size) throw new FormatException("size mismatch");
                List<object> ds = Json.Get(msg, "dirs") as List<object> ?? new List<object>();
                foreach (object d in ds.Take(200000)) Dirs.Add(U.SafeRelPath(Convert.ToString(d)));
            }
            else Entries.Add(new KeyValuePair<string, long>(name, size));
            partRoot = Path.Combine(app.DownloadDir, ".fileshare-partial");
            part = Path.Combine(partRoot, Fid + (kind == "file" ? ".part" : ""));
        }

        public string Name { get { return name; } }
        public string Kind { get { return kind; } }
        public long Size { get { return size; } }
        public int Num { get { return Num_; } }
        public int EntryCount { get { return Entries.Count; } }

        public bool Terminal() { string s = State; return s == "done" || s == "refused" || s == "cancelled" || s == "failed"; }

        static long FileLen(string p)
        {
            FileInfo fi = new FileInfo(U.Lp(p));
            return fi.Exists ? fi.Length : -1;
        }

        public long ExistingBytes()
        {
            try
            {
                if (kind == "file")
                {
                    long l = FileLen(part);
                    return l > 0 ? Math.Min(l, size) : 0;
                }
                if (Directory.Exists(U.Lp(part)))
                {
                    long done; int i; long have;
                    ScanFolder(out done, out i, out have);
                    return done;
                }
            }
            catch { }
            return 0;
        }

        void ScanFolder(out long done, out int index, out long have)
        {
            done = 0;
            for (int i = 0; i < Entries.Count; i++)
            {
                long sz = Entries[i].Value;
                if (sz == 0) continue;
                long h = Math.Max(0, FileLen(U.JoinRel(part, Entries[i].Key)));
                if (h == sz) { done += sz; continue; }
                if (h > sz) h = 0;
                index = i; have = h; done += h;
                return;
            }
            index = Entries.Count; have = 0;
        }

        /// Open storage (resuming a partial download if there is one); returns the offset.
        public long Prepare()
        {
            lock (Lock)
            {
                if (opened)
                {
                    if (F != null) F.Flush();
                    return Written;
                }
                Directory.CreateDirectory(U.Lp(partRoot));
                if (kind == "file")
                {
                    long have = Math.Max(0, FileLen(part));
                    if (have > size) have = 0;
                    if (have == 0) using (FileStream fs = new FileStream(U.Lp(part), FileMode.Create, FileAccess.Write)) { }
                    F = null;
                    Written = have; idx = 0; pos = have;
                }
                else
                {
                    Directory.CreateDirectory(U.Lp(part));
                    foreach (string d in Dirs) Directory.CreateDirectory(U.Lp(U.JoinRel(part, d)));
                    long done; int i; long have;
                    ScanFolder(out done, out i, out have);
                    Written = done; idx = i; pos = have;
                    F = null;
                }
                opened = true;
                return Written;
            }
        }

        static FileStream OpenAt(string path, long at)
        {
            FileStream f = new FileStream(U.Lp(path), FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.Read, 1 << 20);
            f.SetLength(at);
            f.Position = at;
            return f;
        }

        void OpenCurrent()
        {
            string path = U.JoinRel(part, Entries[idx].Key);
            string parent = Path.GetDirectoryName(path);
            if (!string.IsNullOrEmpty(parent)) Directory.CreateDirectory(U.Lp(parent));
            F = OpenAt(path, pos);
        }

        void Write(byte[] data, int off, int n)
        {
            if (kind == "file")
            {
                if (F == null) F = OpenAt(part, Written);   // opened lazily: many queued files never hold handles
                F.Write(data, off, n);
                Written += n;
                pos = Written;
                return;
            }
            while (n > 0)
            {
                while (idx < Entries.Count && pos >= Entries[idx].Value)
                {
                    if (F != null) { F.Close(); F = null; }
                    idx++;
                    pos = 0;
                }
                if (idx >= Entries.Count) throw new IOException("more data than announced");
                if (F == null) OpenCurrent();
                int k = (int)Math.Min(n, Entries[idx].Value - pos);
                F.Write(data, off, k);
                pos += k; Written += k; off += k; n -= k;
            }
        }

        public void CloseFiles()
        {
            lock (Lock)
            {
                if (F != null) { try { F.Close(); } catch { } F = null; }
                opened = false;
            }
        }

        public void Discard()
        {
            CloseFiles();
            try
            {
                if (Directory.Exists(U.Lp(part))) Directory.Delete(U.Lp(part), true);
                else if (File.Exists(U.Lp(part))) File.Delete(U.Lp(part));
            }
            catch { }
            Tidy();
        }

        void Tidy()
        {
            try { if (Directory.Exists(partRoot) && !Directory.EnumerateFileSystemEntries(partRoot).Any()) Directory.Delete(partRoot); } catch { }
        }

        string FinalizeFiles()
        {
            CloseFiles();
            if (kind == "folder")
            {
                foreach (KeyValuePair<string, long> e in Entries)
                {
                    if (e.Value != 0) continue;
                    string p = U.JoinRel(part, e.Key);
                    string parent = Path.GetDirectoryName(p);
                    if (!string.IsNullOrEmpty(parent)) Directory.CreateDirectory(U.Lp(parent));
                    using (FileStream fs = new FileStream(U.Lp(p), FileMode.Create, FileAccess.Write)) { }
                }
            }
            else if (!File.Exists(U.Lp(part)))
                using (FileStream fs = new FileStream(U.Lp(part), FileMode.Create, FileAccess.Write)) { }
            string dest;
            lock (app.FinalizeLock)
            {
                dest = U.UniquePath(app.DownloadDir, name);
                if (kind == "folder") Directory.Move(U.Lp(part), U.Lp(dest));
                else File.Move(U.Lp(part), U.Lp(dest));
            }
            Tidy();
            return dest;
        }

        public void OnData(Session sess, long offset, uint crc, byte[] data, int off, int n)
        {
            lock (Lock)
            {
                if (State != "active" || !opened) return;
                if (offset != Written)
                {
                    if (offset > Written && !resyncSent)
                    {
                        resyncSent = true;
                        sess.SendCtrl("t", "answer", "tid", (long)Tid, "ok", true, "offset", Written);
                    }
                    return;
                }
                if (U.Crc32(data, off, n) != crc)
                {
                    if (!resyncSent)
                    {
                        resyncSent = true;
                        sess.SendCtrl("t", "answer", "tid", (long)Tid, "ok", true, "offset", Written);
                    }
                    return;
                }
                resyncSent = false;
                try { Write(data, off, n); }
                catch (Exception exc)
                {
                    Fail(sess, "disk error: " + U.ErrText(exc));
                    return;
                }
                Meter.Add(Written);
                double now = U.Now();
                if (Written >= size) Complete(sess);
                else if (now - lastProgress >= K.PROGRESS_EVERY)
                {
                    lastProgress = now;
                    sess.SendCtrl("t", "progress", "tid", (long)Tid, "got", Written);
                }
            }
        }

        public void ResetResync() { lock (Lock) resyncSent = false; }

        public void Complete(Session sess)
        {
            string dest;
            lock (Lock)
            {
                if (State != "active") return;
                try { dest = FinalizeFiles(); }
                catch (Exception exc)
                {
                    Fail(sess, "could not save: " + U.ErrText(exc));
                    return;
                }
                State = "done";
            }
            double took = U.Now() - (Started >= 0 ? Started : U.Now());
            string speed = took > 0.5 ? " at " + U.Human(size / took) + "/s" : "";
            if (sess != null)
            {
                sess.SendCtrl("t", "progress", "tid", (long)Tid, "got", size);
                sess.SendCtrl("t", "complete", "tid", (long)Tid, "saved", Path.GetFileName(dest));
            }
            OfferGroup g = Group;
            if (g != null && g.Items.Count > 1)
            {
                Con.S("[+] Download complete: " + name + " (" + U.Human(size) + ")" + speed);
                app.GroupProgress(g);
            }
            else
                Con.S("[+] Download complete: " + name + " (" + U.Human(size) + ")" + speed + "\n    Saved to: " + dest);
            app.TransferEnded();
        }

        public void Fail(Session sess, string reason)
        {
            lock (Lock)
            {
                if (Terminal()) return;
                State = "failed";
            }
            CloseFiles();
            app.SendCancel(Tid, "down", reason, PeerId);
            Con.S("[!] Download of " + name + " failed: " + reason + ". The partial file was kept; it resumes if sent again.");
            if (Group != null && Group.Items.Count > 1) app.GroupProgress(Group);
            app.TransferEnded();
        }

        public void FlushFile()
        {
            try { lock (Lock) { if (F != null) F.Flush(); } } catch { }
        }
    }

    /// Everything offered by one upload command on the other side: one y/n question.
    class OfferGroup
    {
        public readonly string PeerId, Bid;
        public readonly List<InTransfer> Items = new List<InTransfer>();
        public long Expected = 1;
        public string State = "collecting";     // collecting, asking, decided
        public string Decision;
        public readonly List<string> Answers = new List<string>();
        public double Last = U.Now();
        public bool Summarized;

        public OfferGroup(string peerId, string bid) { PeerId = peerId; Bid = bid; }

        public List<InTransfer> Live() { return Items.Where(t => t.State == "asking").ToList(); }
        public long Size() { return Live().Sum(t => t.Size); }
        public string Title()
        {
            List<InTransfer> items = Live();
            if (items.Count == 0) items = Items;
            return items.Count == 1 ? items[0].Name : items.Count + " items";
        }
    }

    // ======================================================================
    // The application: connections, transfers, commands
    // ======================================================================
    class PendingCancel
    {
        public string PeerId; public uint Tid; public string Side, Reason;
    }

    class MenuState
    {
        public string Stage;
        public List<KeyValuePair<string, ITransfer>> Rows;
        public List<KeyValuePair<string, ITransfer>> Chosen;
        public List<int> Picks;
    }

    class App
    {
        public readonly int Port;
        public readonly string DownloadDir;
        public readonly object Lock = new object();
        public volatile Session Session;
        public readonly HashSet<Session> Sessions = new HashSet<Session>();
        public readonly Dictionary<uint, OutTransfer> Outgoing = new Dictionary<uint, OutTransfer>();
        public readonly Dictionary<string, InTransfer> Incoming = new Dictionary<string, InTransfer>();
        readonly LinkedList<OfferGroup> prompts = new LinkedList<OfferGroup>();
        readonly Dictionary<string, OfferGroup> groups = new Dictionary<string, OfferGroup>();
        int serial = 0;
        readonly object slotCv = new object();
        readonly List<OutTransfer> slotWait = new List<OutTransfer>();
        readonly HashSet<OutTransfer> streaming = new HashSet<OutTransfer>();
        public readonly object FinalizeLock = new object();
        public volatile MenuState Menu;
        List<PendingCancel> pendingCancels = new List<PendingCancel>();
        public volatile bool Stopping;
        public readonly Dialer Dialer;
        public readonly Listener Listener;
        public LineEditor Editor;
        readonly HashSet<uint> usedTids = new HashSet<uint>();
        string lastPeerId, lastQuietPeer;
        double quietUntil = 0;

        public App(string peer, int port, string downloadDir)
        {
            Port = port;
            DownloadDir = downloadDir;
            Dialer = new Dialer(this);
            Listener = new Listener(this);
            if (peer.Length > 0) Dialer.SetTarget(peer, false);
        }

        static string IKey(string peerId, uint tid) { return (peerId ?? "") + "|" + tid; }

        // ---- ids ----
        public uint NewTid()
        {
            lock (Lock)
            {
                while (true)
                {
                    uint tid = U.RandUInt();
                    if (tid != 0 && usedTids.Add(tid)) return tid;
                }
            }
        }

        public int NextNum() { lock (Lock) return ++serial; }

        List<OutTransfer> OutList() { lock (Lock) return Outgoing.Values.ToList(); }
        List<InTransfer> InList() { lock (Lock) return Incoming.Values.ToList(); }

        // ---- upload stream slots: a few files stream at once, the rest queue in order ----
        public bool AcquireSlot(OutTransfer t, Session sess)
        {
            lock (slotCv)
            {
                if (!slotWait.Contains(t)) slotWait.Add(t);
                bool got = false;
                try
                {
                    while (true)
                    {
                        if (t.Terminal() || !sess.Alive || Session != sess || Stopping) return false;
                        while (slotWait.Count > 0 && slotWait[0] != t && slotWait[0].Terminal()) slotWait.RemoveAt(0);
                        if (streaming.Count < K.MAX_STREAMS && slotWait.Count > 0 && slotWait[0] == t)
                        {
                            slotWait.RemoveAt(0);
                            streaming.Add(t);
                            got = true;
                            return true;
                        }
                        Monitor.Wait(slotCv, 500);
                    }
                }
                finally
                {
                    if (!got) slotWait.Remove(t);
                    Monitor.PulseAll(slotCv);
                }
            }
        }

        public void ReleaseSlot(OutTransfer t)
        {
            lock (slotCv) { streaming.Remove(t); Monitor.PulseAll(slotCv); }
        }

        // ---- sessions ----
        public void WaitSession(double timeout)
        {
            lock (Lock) { if (Session == null) Monitor.Wait(Lock, (int)(timeout * 1000)); }
        }

        /// Make sess the active connection. Returns false when it must be dropped.
        public bool Attach(Session sess)
        {
            if (sess.PeerId == K.INSTANCE_ID) throw new NotUs("self");
            Session replaced = null;
            bool samePeer, peerChanged;
            List<PendingCancel> cancels;
            lock (Lock)
            {
                if (Stopping) return false;
                Session cur = Session;
                if (cur != null && cur.Alive)
                {
                    bool healthy = U.Now() - cur.LastRx < K.HEALTHY_WINDOW;
                    if (cur.PeerId == sess.PeerId)
                    {
                        if (healthy && string.CompareOrdinal(sess.DialerId(), cur.DialerId()) > 0)
                        {
                            sess.SendCtrl("t", "dup");     // both dialed at once: keep the existing one
                            return false;
                        }
                    }
                    else if (healthy)
                    {
                        sess.SendCtrl("t", "busy");
                        return false;
                    }
                    replaced = cur;
                }
                samePeer = cur != null && cur.PeerId == sess.PeerId;
                Session = sess;
                sess.Attached = true;
                Monitor.PulseAll(Lock);
                peerChanged = lastPeerId != null && lastPeerId != sess.PeerId;
                lastQuietPeer = lastPeerId;
                lastPeerId = sess.PeerId;
                cancels = pendingCancels.Where(c => c.PeerId == sess.PeerId).ToList();
                pendingCancels = new List<PendingCancel>();
            }
            if (replaced != null)
            {
                if (samePeer && replaced.Alive)
                {
                    replaced.Busy = true;                  // superseded twin: close it quietly on both sides
                    replaced.SendCtrl("t", "dup");
                    Session r = replaced;
                    U.StartThread(delegate { r.FlushAndClose(0.3); });
                }
                else replaced.Kill();
            }
            foreach (PendingCancel c in cancels)
                sess.SendCtrl("t", "cancel", "tid", (long)c.Tid, "side", c.Side, "reason", c.Reason);
            if (peerChanged) PeerRestarted();
            string where = sess.RemotePrivate ? "LAN" : "";
            bool quiet = U.Now() < quietUntil && sess.PeerId == lastQuietPeer;
            if ((!samePeer || replaced == null) && !quiet)
                Con.S("[+] Connected to " + sess.PeerName + " via " + sess.Label + (where.Length > 0 ? " (" + where + ")" : "") + ".");
            ShowPrompt();
            return true;
        }

        public string BusyWith()
        {
            Session s = Session;
            if (s != null && s.Alive && U.Now() - s.LastRx < K.HEALTHY_WINDOW) return s.PeerId ?? "";
            return "";
        }

        void PeerRestarted()
        {
            List<InTransfer> stale = InList().Where(t => t.State == "active").ToList();
            foreach (InTransfer t in stale)
            {
                t.CloseFiles();
                t.State = "failed";
                Con.S("[*] The other user restarted " + K.APP_NAME + "; " + t.Name + " is paused. If they send it again it resumes.");
            }
            bool hadPrompt;
            lock (Lock)
            {
                hadPrompt = prompts.Count > 0;
                prompts.Clear();
                foreach (OfferGroup g in groups.Values)
                {
                    if (g.State != "decided")
                    {
                        g.State = "decided"; g.Decision = "n";
                        foreach (InTransfer t in g.Items) if (t.State == "asking") t.State = "cancelled";
                    }
                }
            }
            if (hadPrompt)
            {
                Con.S("[*] The open question was dropped; the other user will offer again if needed.");
                Con.I.SetPrompt("> ", false);
            }
        }

        public void OnSessionEnd(Session sess)
        {
            bool was;
            lock (Lock)
            {
                Sessions.Remove(sess);
                was = Session == sess;
                if (was) { Session = null; Monitor.PulseAll(Lock); }
            }
            foreach (InTransfer t in InList()) if (t.State == "active") t.FlushFile();
            if (was && !Stopping && !sess.Busy)
            {
                Con.S(sess.Bye ? "[-] The other user closed " + K.APP_NAME + "." : "[-] Connection lost.");
                if (Dialer.HasTarget()) Con.S("[*] Reconnecting automatically... (transfers resume by themselves)");
                else Con.S("[*] Waiting for the other user to reconnect... (transfers resume by themselves)");
                Dialer.Wake();
            }
        }

        public bool SessionAlive()
        {
            Session s = Session;
            return s != null && s.Alive;
        }

        /// Handshake + attach + read loop, in the calling thread.
        public string RunSession(Session sess, bool magicSeen)
        {
            lock (Lock) Sessions.Add(sess);
            try
            {
                sess.Handshake(magicSeen);
                if (sess.PeerId == K.INSTANCE_ID) throw new NotUs("self");
                sess.StartWriter();
                if (!Attach(sess))
                {
                    sess.FlushAndClose(0.5);
                    return "dup";
                }
            }
            catch
            {
                sess.Alive = false;
                try { sess.conn.Close(); } catch { }
                lock (Lock) Sessions.Remove(sess);
                throw;
            }
            sess.RunReader();
            return "ok";
        }

        // ---- frames ----
        public void OnFrame(Session sess, byte ftype, byte[] payload)
        {
            if (ftype == K.F_PING) return;
            if (ftype == K.F_DATA)
            {
                if (payload.Length < K.DATA_HDR) return;
                uint tid = U.GetU32(payload, 0);
                long offset = (long)U.GetU64(payload, 4);
                uint crc = U.GetU32(payload, 12);
                InTransfer t;
                lock (Lock) Incoming.TryGetValue(IKey(sess.PeerId, tid), out t);
                if (t != null) t.OnData(sess, offset, crc, payload, K.DATA_HDR, payload.Length - K.DATA_HDR);
                return;
            }
            Dictionary<string, object> msg;
            try { msg = Json.Parse(Encoding.UTF8.GetString(payload)) as Dictionary<string, object>; }
            catch { return; }
            if (msg == null) return;
            try { OnCtrl(sess, Json.Str(msg, "t"), msg); }
            catch (Exception exc) { Con.S("[!] Ignored a bad message from the other user (" + exc.GetType().Name + ")."); }
        }

        static bool TidOf(Dictionary<string, object> msg, out uint tid)
        {
            tid = 0;
            object v = Json.Get(msg, "tid");
            try { long l = Json.Long(v); if (l < 0 || l > uint.MaxValue) return false; tid = (uint)l; return true; }
            catch { return false; }
        }

        static string Reason(Dictionary<string, object> msg)
        {
            string r = Json.Str(msg, "reason");
            return r.Length > 120 ? r.Substring(0, 120) : r;
        }

        void OnCtrl(Session sess, string kind, Dictionary<string, object> msg)
        {
            if (kind == "offer") { OnOffer(sess, msg); return; }
            if (kind == "busy") { sess.Busy = true; Dialer.NoteBusy(); sess.Kill(); return; }
            if (kind == "dup")
            {
                sess.Busy = true;                  // a twin connection won; this one quietly closes
                quietUntil = U.Now() + 5.0;
                sess.Kill();
                return;
            }
            if (kind == "bye") { sess.Bye = true; sess.Kill(); return; }
            uint tid;
            bool hasTid = TidOf(msg, out tid);
            OutTransfer ot = null;
            if (hasTid) lock (Lock) Outgoing.TryGetValue(tid, out ot);
            if (kind == "answer")
            {
                if (!hasTid) return;
                if (ot == null || ot.Terminal())
                {
                    if (Json.Truthy(Json.Get(msg, "ok"))) sess.SendCtrl("t", "cancel", "tid", (long)tid, "side", "up", "reason", "");
                    return;
                }
                ot.OnAnswer(sess, Json.Truthy(Json.Get(msg, "ok")), Json.Get(msg, "offset") ?? (object)0L, Reason(msg));
            }
            else if (kind == "progress")
            {
                if (ot != null && !ot.Terminal()) ot.OnProgress(Json.Get(msg, "got") ?? (object)0L);
            }
            else if (kind == "complete")
            {
                if (ot != null) { ot.OnComplete(); TransferEnded(); }
            }
            else if (kind == "cancel")
            {
                string reason = Reason(msg);
                if (Json.Str(msg, "side") == "down")
                {
                    if (ot != null) { ot.OnRemoteCancel(reason); sess.Purge(tid); TransferEnded(); }
                }
                else if (hasTid)
                {
                    InTransfer it;
                    lock (Lock) Incoming.TryGetValue(IKey(sess.PeerId, tid), out it);
                    if (it != null) RemoteCancelledIncoming(it, reason);
                }
            }
        }

        void OnOffer(Session sess, Dictionary<string, object> msg)
        {
            uint tid;
            if (!TidOf(msg, out tid)) return;
            string key = IKey(sess.PeerId, tid);
            InTransfer t;
            lock (Lock) Incoming.TryGetValue(key, out t);
            if (t != null)
            {
                if (t.State == "active")
                {
                    long offset;
                    try { offset = t.Prepare(); }
                    catch (Exception exc) { t.Fail(sess, "disk error: " + U.ErrText(exc)); return; }
                    t.ResetResync();
                    sess.SendCtrl("t", "answer", "tid", (long)tid, "ok", true, "offset", offset);
                    if (offset < t.Size) Con.S("[*] Download of " + t.Name + " resumed at " + U.F1(100.0 * offset / Math.Max(1, t.Size)) + "%.");
                }
                else if (t.State == "refused") sess.SendCtrl("t", "answer", "tid", (long)tid, "ok", false, "reason", "refused");
                else if (t.State == "cancelled" || t.State == "failed") sess.SendCtrl("t", "cancel", "tid", (long)tid, "side", "down", "reason", "");
                else if (t.State == "done") sess.SendCtrl("t", "complete", "tid", (long)tid, "saved", t.Name);
                return;                              // 'asking': the question is still open
            }
            try { t = new InTransfer(this, sess.PeerId, msg); }
            catch
            {
                sess.SendCtrl("t", "answer", "tid", (long)tid, "ok", false, "reason", "the offer was not valid");
                return;
            }
            bool clash;
            lock (Lock) clash = Incoming.Values.Any(o => o.Fid == t.Fid && (o.State == "asking" || o.State == "active"));
            if (clash)
            {
                sess.SendCtrl("t", "answer", "tid", (long)tid, "ok", false, "reason", "the same file is already being downloaded");
                return;
            }
            t.Have = t.ExistingBytes();
            string bid = Regex.Replace(Json.Str(msg, "batch"), "[^0-9A-Za-z]", "");
            if (bid.Length > 32) bid = bid.Substring(0, 32);
            if (bid.Length == 0) bid = "solo" + tid;
            long expected = 1;
            try { object bn = Json.Get(msg, "bn"); if (Json.Truthy(bn)) expected = Math.Max(1, Math.Min(Json.Long(bn), 1000000)); } catch { expected = 1; }
            string gkey = sess.PeerId + "|" + bid;
            OfferGroup g;
            string state;
            bool ready;
            lock (Lock)
            {
                t.Num_ = NextNum();
                Incoming[key] = t;
                if (!groups.TryGetValue(gkey, out g)) { g = new OfferGroup(sess.PeerId, bid); groups[gkey] = g; }
                g.Expected = expected;
                g.Last = U.Now();
                t.Group = g;
                state = g.State;
                if (state != "decided") g.Items.Add(t);
                ready = state == "collecting" && g.Items.Count >= g.Expected;
            }
            if (state == "decided") ApplyDecision(t, g.Decision, false, "refused");     // late part of an answered offer
            else if (state == "asking") Con.S("[*] One more item joined the offer: " + t.Name + " (" + U.Human(t.Size) + "). Your answer covers it too.");
            else if (ready) Present(g);
        }

        public void GroupProgress(OfferGroup g)
        {
            int done, total;
            lock (Lock)
            {
                if (g.State != "decided" || g.Decision != "y" || g.Summarized) return;
                if (g.Items.Any(t => !t.Terminal())) return;
                g.Summarized = true;
                done = g.Items.Count(t => t.State == "done");
                total = g.Items.Count;
            }
            Con.S("[+] Finished: " + done + " of " + total + " items downloaded. Saved in: " + DownloadDir);
        }

        void CollectOverdue()
        {
            double now = U.Now();
            List<OfferGroup> ready;
            lock (Lock) ready = groups.Values.Where(g => g.State == "collecting" && now - g.Last > K.GROUP_WAIT).ToList();
            foreach (OfferGroup g in ready) Present(g);
        }

        void Present(OfferGroup g)
        {
            List<InTransfer> items;
            long need, free;
            bool nospace, first = false;
            lock (Lock)
            {
                if (g.State != "collecting") return;
                items = g.Live();
                if (items.Count == 0) { g.State = "decided"; g.Decision = "n"; return; }
                need = items.Sum(t => Math.Max(0, t.Size - t.Have));
                free = U.DiskFree(DownloadDir);
                nospace = free >= 0 && need > free;
                if (nospace) { g.State = "decided"; g.Decision = "n"; }
                else
                {
                    g.State = "asking";
                    prompts.AddLast(g);
                    first = prompts.Count == 1;
                }
            }
            if (nospace)
            {
                foreach (InTransfer t in items) ApplyDecision(t, "n", true, "not enough free disk space on the receiver");
                Con.S("[x] " + g.Title() + " (" + U.Human(need) + ") was offered, but there is not enough free disk space (" + U.Human(free) + " free). Refused.");
                return;
            }
            if (first && Menu == null) ShowPrompt();
            else
            {
                Con.S("[*] Another offer arrived: " + g.Title() + " (" + U.Human(g.Size()) + "). It will be asked " +
                      (!first ? "after the current one" : "when you finish here") + ".");
                Con.I.SetPrompt(Con.I.Prompt, false);
            }
        }

        public static string TypeText(ITransfer t)
        {
            if (t.Kind == "folder") return "folder, " + t.EntryCount + " file(s)";
            string ext = U.Ext(t.Name).TrimStart('.').ToUpperInvariant();
            return (ext.Length > 0 ? ext : "unknown") + " file";
        }

        public void ShowPrompt()
        {
            if (Menu != null) return;            // the stop menu is open; the question comes back after it
            OfferGroup g;
            List<InTransfer> items;
            int waiting;
            while (true)
            {
                lock (Lock)
                {
                    g = prompts.Count > 0 ? prompts.First.Value : null;
                    items = g != null ? g.Live() : new List<InTransfer>();
                    if (g != null && items.Count == 0)   // everything in it was withdrawn
                    {
                        prompts.RemoveFirst();
                        g.State = "decided"; g.Decision = "n";
                        continue;
                    }
                    waiting = prompts.Count - 1;
                }
                break;
            }
            if (g == null) { Con.I.SetPrompt("> ", false); return; }
            if (g.Answers.Count == 0)
            {
                Session s = Session;
                string who = s != null ? s.PeerName : "the other user";
                List<string> lines = new List<string> { "", new string('=', 56) };
                if (items.Count == 1)
                {
                    InTransfer t = items[0];
                    lines.Add("Incoming " + t.Kind + " from " + who + ":");
                    lines.Add("  Name : " + t.Name);
                    lines.Add("  Type : " + TypeText(t));
                    lines.Add("  Size : " + U.Human(t.Size) + " (" + U.Commas(t.Size) + " bytes)");
                    if (t.Have > 0) lines.Add("  Note : " + U.F1(100.0 * t.Have / Math.Max(1, t.Size)) + "% was already downloaded earlier; it will resume.");
                }
                else
                {
                    lines.Add("Incoming " + items.Count + " items from " + who + " (" + U.Human(g.Size()) + " total):");
                    int i = 1;
                    foreach (InTransfer t in items.Take(15))
                    {
                        lines.Add("  " + U.PadL(i.ToString(), 2) + ". " + U.Pad(U.Short(t.Name, 30), 30) + " " + U.Pad(U.Short(TypeText(t), 18), 18) + " " + U.PadL(U.Human(t.Size), 10));
                        i++;
                    }
                    if (items.Count > 15) lines.Add("      ... and " + (items.Count - 15) + " more");
                    int resumed = items.Count(t => t.Have > 0);
                    if (resumed > 0) lines.Add("  Note : " + resumed + " item(s) were partly downloaded earlier; they will resume.");
                }
                if (waiting > 0) lines.Add("  (" + waiting + " more offer(s) waiting after this one)");
                lines.Add(new string('=', 56));
                Con.S(string.Join("\n", lines.ToArray()));
            }
            Con.I.SetPrompt(PromptText(g, items.Count), true);
        }

        static string PromptText(OfferGroup g, int count)
        {
            string it = count == 1 ? "it" : "them";
            if (g.Answers.Count == 0) return "Do you want to download " + it + "? (y/n): ";
            string last = g.Answers[g.Answers.Count - 1];
            string word = last == "y" ? "download" : "refuse";
            if (g.Answers.Count == 1) return "Are you sure? Press " + last + " again to " + word + " " + it + " (y/n): ";
            return "You changed your answer. Press " + last + " again to " + word + " " + it + " (y/n): ";
        }

        void AnswerPrompt(string line)
        {
            OfferGroup g;
            lock (Lock) g = prompts.Count > 0 ? prompts.First.Value : null;
            if (g == null) return;
            string a = line.Trim().ToLowerInvariant();
            if (a == "yes") a = "y";
            if (a == "no") a = "n";
            if (a != "y" && a != "n")
            {
                Con.S("Please type y or n.");
                Con.I.SetPrompt(PromptText(g, g.Live().Count), true);
                return;
            }
            g.Answers.Add(a);
            string decision = null;
            int c = g.Answers.Count;
            if (c >= 2 && g.Answers[c - 1] == g.Answers[c - 2]) decision = a;
            else if (c >= 4)
            {
                decision = "n";
                Con.S("[x] The answers kept changing, so this counts as a refusal.");
            }
            if (decision == null)
            {
                Con.I.SetPrompt(PromptText(g, g.Live().Count), true);
                return;
            }
            List<InTransfer> items;
            lock (Lock)
            {
                if (prompts.Count > 0 && prompts.First.Value == g) prompts.RemoveFirst();
                g.State = "decided"; g.Decision = decision;
                items = g.Live();
            }
            Decide(items, decision);
            ShowPrompt();
        }

        void Decide(List<InTransfer> items, string decision)
        {
            if (items.Count == 0) return;
            string reason = "refused";
            if (decision == "y")
            {
                long need = items.Sum(t => Math.Max(0, t.Size - t.Have));
                long free = U.DiskFree(DownloadDir);
                if (free >= 0 && need > free)
                {
                    decision = "n"; reason = "not enough free disk space on the receiver";
                    Con.S("[x] Not enough free disk space anymore (" + U.Human(need) + " needed, " + U.Human(free) + " free). Refused.");
                }
            }
            bool many = items.Count > 1;
            if (many)
            {
                if (decision == "y")
                    Con.S("[+] Downloading " + items.Count + " items (" + U.Human(items.Sum(t => t.Size)) + "). Press Ctrl+Z (or type cancel) to stop.");
                else if (reason == "refused")
                    Con.S("[x] You refused to download " + items.Count + " items.");
            }
            foreach (InTransfer t in items) ApplyDecision(t, decision, many, reason);
        }

        void ApplyDecision(InTransfer t, string decision, bool quiet, string reason)
        {
            Session cur = Session;
            Session sess = (cur != null && cur.PeerId == t.PeerId) ? cur : null;
            lock (Lock) { if (t.State != "asking") return; }
            if (decision == "y")
            {
                long free = U.DiskFree(DownloadDir);
                if (free >= 0 && t.Size - t.Have > free)
                {
                    decision = "n"; reason = "not enough free disk space on the receiver";
                    Con.S("[x] Not enough free disk space for " + t.Name + ". Refused.");
                }
            }
            if (decision == "y")
            {
                long offset;
                try { offset = t.Prepare(); }
                catch (Exception exc)
                {
                    t.State = "failed";
                    Con.S("[!] Cannot write to " + DownloadDir + ": " + U.ErrText(exc));
                    if (sess != null) sess.SendCtrl("t", "answer", "tid", (long)t.Tid, "ok", false, "reason", "the receiver cannot write to its download folder");
                    return;
                }
                t.State = "active";
                t.Started = U.Now();
                if (!quiet)
                    Con.S("[+] Downloading " + t.Name + " (" + U.Human(t.Size) + ")" + (offset > 0 ? " from " + U.F1(100.0 * offset / Math.Max(1, t.Size)) + "%" : "") +
                          ". Press Ctrl+Z (or type cancel) to stop.");
                if (sess != null) sess.SendCtrl("t", "answer", "tid", (long)t.Tid, "ok", true, "offset", offset);
                if (t.Size == 0 || offset >= t.Size) t.Complete(sess);
            }
            else
            {
                t.State = "refused";
                if (!quiet && reason == "refused") Con.S("[x] You refused to download " + t.Name + ".");
                if (sess != null) sess.SendCtrl("t", "answer", "tid", (long)t.Tid, "ok", false, "reason", reason);
            }
            TransferEnded();
        }

        void RemoteCancelledIncoming(InTransfer t, string reason)
        {
            bool asking;
            lock (Lock)
            {
                if (t.Terminal()) return;
                asking = t.State == "asking";
                t.State = "cancelled";
            }
            if (asking)
            {
                Con.S("[x] The other user withdrew the offer of " + t.Name + ".");
                OfferGroup g;
                lock (Lock) g = prompts.Count > 0 ? prompts.First.Value : null;
                if (g != null && g.Items.Contains(t)) ShowPrompt();
                return;
            }
            t.Discard();
            if (reason.Length > 0) Con.S("[x] Download of " + t.Name + " stopped: " + reason + ".");
            else Con.S("[x] File downloading stopped because the other user interrupted it (" + t.Name + ").");
            if (t.Group != null && t.Group.Items.Count > 1) GroupProgress(t.Group);
            TransferEnded();
        }

        public void SendCancel(uint tid, string side, string reason, string peerId)
        {
            Session sess = Session;
            if (sess != null && sess.Alive && (peerId == null || sess.PeerId == peerId))
                sess.SendCtrl("t", "cancel", "tid", (long)tid, "side", side, "reason", reason);
            else
                lock (Lock) pendingCancels.Add(new PendingCancel { PeerId = peerId ?? lastPeerId, Tid = tid, Side = side, Reason = reason });
        }

        // ---- user actions ----
        bool CancelIncoming(InTransfer t)
        {
            lock (t.Lock)
            {
                if (t.State != "active") return false;
                t.State = "cancelled";
            }
            t.Discard();
            SendCancel(t.Tid, "down", "", t.PeerId);
            Con.S("[x] You interrupted the downloading of " + t.Name + ".");
            if (t.Group != null && t.Group.Items.Count > 1) GroupProgress(t.Group);
            return true;
        }

        List<KeyValuePair<string, ITransfer>> RunningList()
        {
            List<KeyValuePair<string, ITransfer>> rows = new List<KeyValuePair<string, ITransfer>>();
            foreach (OutTransfer t in OutList())
                if (!t.Terminal() && t.State != "preparing") rows.Add(new KeyValuePair<string, ITransfer>("SENDING", t));
            foreach (InTransfer t in InList())
                if (t.State == "active") rows.Add(new KeyValuePair<string, ITransfer>("RECEIVING", t));
            return rows.OrderBy(r => r.Value.Num).ToList();
        }

        string RowState(string d, ITransfer t)
        {
            if (d == "RECEIVING")
            {
                InTransfer it = (InTransfer)t;
                return U.F1(it.Size > 0 ? 100.0 * it.Written / it.Size : 100.0) + "%";
            }
            OutTransfer ot = (OutTransfer)t;
            if (ot.State == "sending" || (ot.AcceptedOnce && ot.State != "queued"))
                return U.F1(ot.Size > 0 ? 100.0 * ot.Acked / ot.Size : 100.0) + "%";
            if (ot.State == "waiting") return "waiting for connection";
            if (ot.State == "offered") return "waiting for answer";
            if (ot.State == "queued") return "in line";
            return ot.State;
        }

        string RowText(int i, string d, ITransfer t)
        {
            return "  " + U.PadL(i.ToString(), 2) + ". " + U.Pad(d, 9) + "  " + U.Pad(U.Short(t.Name, 28), 28) + " " +
                   U.Pad(U.Short(TypeText(t), 18), 18) + " " + U.PadL(U.Human(t.Size), 10) + "   " + RowState(d, t);
        }

        bool StopOne(ITransfer t)
        {
            if (t.Terminal()) return false;
            OutTransfer ot = t as OutTransfer;
            if (ot != null) return ot.CancelLocal();
            return CancelIncoming((InTransfer)t);
        }

        /// Ctrl+Z / cancel: list running transfers, let the user pick, confirm once.
        public void OpenStopMenu(string preset)
        {
            List<KeyValuePair<string, ITransfer>> rows = RunningList();
            if (rows.Count == 0) { Con.S("[*] Nothing is uploading or downloading right now."); return; }
            List<string> lines = new List<string> { "", new string('-', 56), "Running transfers:" };
            for (int i = 0; i < rows.Count; i++) lines.Add(RowText(i + 1, rows[i].Key, rows[i].Value));
            lines.Add(new string('-', 56));
            lock (Lock) Menu = new MenuState { Stage = "pick", Rows = rows };
            if (preset == null)
            {
                lines.Add("Type the numbers to stop (like 2, or 2 3 4), all for everything, or just Enter to keep them.");
                Con.S(string.Join("\n", lines.ToArray()));
                Con.I.SetPrompt("Which to stop? ", true);
            }
            else
            {
                Con.S(string.Join("\n", lines.ToArray()));
                MenuInput(preset);
            }
        }

        void MenuInput(string line)
        {
            MenuState m = Menu;
            if (m == null) return;
            List<KeyValuePair<string, ITransfer>> rows = m.Rows;
            if (m.Stage == "pick")
            {
                string text = line.Trim().ToLowerInvariant();
                if (text == "" || text == "0" || text == "q" || text == "none" || text == "back" || text == "n" || text == "no")
                {
                    CloseMenu("[*] Nothing was stopped.");
                    return;
                }
                List<int> picks = new List<int>();
                if (text == "all" || text == "a" || text == "*")
                {
                    for (int i = 1; i <= rows.Count; i++) picks.Add(i);
                }
                else
                {
                    List<string> bad = new List<string>();
                    foreach (string tok0 in Regex.Split(text.TrimStart('#'), @"[\s,;]+"))
                    {
                        if (tok0.Length == 0) continue;
                        string tok = tok0.TrimStart('#');
                        List<int> nums = new List<int>();
                        int lo, hi, one;
                        Match rm = Regex.Match(tok, @"^(\d+)-(\d+)$");
                        if (rm.Success && int.TryParse(rm.Groups[1].Value, out lo) && int.TryParse(rm.Groups[2].Value, out hi))
                        {
                            if (lo <= hi && hi - lo < 100000) for (int n = lo; n <= hi; n++) nums.Add(n);   // 2-4 means 2 3 4
                        }
                        else if (Regex.IsMatch(tok, @"^\d+$") && int.TryParse(tok, out one)) nums.Add(one);
                        if (nums.Count == 0 || nums.Any(n => n < 1 || n > rows.Count)) { bad.Add(tok); continue; }
                        foreach (int n in nums) if (!picks.Contains(n)) picks.Add(n);
                    }
                    if (bad.Count > 0 || picks.Count == 0)
                    {
                        Con.S("[!] " + (bad.Count > 0 ? string.Join(", ", bad.ToArray()) : "That") + " is not on the list. Type numbers from 1 to " +
                              rows.Count + " with spaces between them, like 2 3.");
                        Con.I.SetPrompt(Con.I.Prompt, true);
                        return;
                    }
                }
                picks.Sort();
                List<KeyValuePair<string, ITransfer>> chosen = picks.Select(n => rows[n - 1]).ToList();
                m.Stage = "confirm"; m.Chosen = chosen; m.Picks = picks;
                string q;
                if (chosen.Count == 1)
                    q = "Stop " + (chosen[0].Key == "SENDING" ? "sending" : "receiving") + " " + U.Short(chosen[0].Value.Name, 30) + "? (y/n): ";
                else
                {
                    List<string> ls = new List<string>();
                    for (int i = 0; i < chosen.Count; i++) ls.Add(RowText(picks[i], chosen[i].Key, chosen[i].Value));
                    Con.S("You picked:\n" + string.Join("\n", ls.ToArray()));
                    q = "Stop these " + chosen.Count + " transfers? (y/n): ";
                }
                Con.I.SetPrompt(q, true);
                return;
            }
            string a = line.Trim().ToLowerInvariant();
            if (a == "y" || a == "yes")
            {
                List<KeyValuePair<string, ITransfer>> chosen = m.Chosen;
                lock (Lock) Menu = null;
                foreach (KeyValuePair<string, ITransfer> kv in chosen)
                {
                    if (kv.Value.Terminal()) Con.S("[*] " + kv.Value.Name + " already finished, so it was not stopped.");
                    else StopOne(kv.Value);
                }
                TransferEnded();
                CloseMenu(null);
            }
            else if (a == "n" || a == "no") CloseMenu("[*] Nothing was stopped. Transfers keep going.");
            else
            {
                Con.S("Please type y or n.");
                Con.I.SetPrompt(Con.I.Prompt, true);
            }
        }

        void CloseMenu(string message)
        {
            lock (Lock) Menu = null;
            if (message != null) Con.S(message);
            ShowPrompt();                         // back to the y/n download question, or the normal prompt
        }

        public void TransferEnded()
        {
            try { Con.I.SetStatus(StatusLine()); } catch { }
        }

        void Upload(string arg)
        {
            if (arg.Trim().Length == 0) { Con.S("Usage: upload <file or folder> [more files or folders...]"); return; }
            List<string> paths, missing;
            U.ParsePaths(arg, out paths, out missing);
            foreach (string m in missing) Con.S("[!] Not found: " + m);
            if (paths.Count == 0) return;
            if (paths.Count > K.MAX_BATCH)
            {
                Con.S("[!] That is " + paths.Count + " items; one upload can offer up to " + K.MAX_BATCH + ". Put them in a folder and upload the folder " +
                      "(any number of files), or upload them in parts.");
                return;
            }
            HashSet<string> busy;
            lock (Lock) busy = new HashSet<string>(Outgoing.Values.Where(t => !t.Terminal()).Select(t => U.NormKey(t.Path_)));
            List<OutTransfer> items = new List<OutTransfer>();
            foreach (string p in paths)
            {
                string ap = Path.GetFullPath(p);
                string k = U.NormKey(ap);
                if (busy.Contains(k))
                {
                    string bn = Path.GetFileName(ap.TrimEnd('\\', '/'));
                    Con.S("[!] " + (string.IsNullOrEmpty(bn) ? ap : bn) + " is already being sent.");
                    continue;
                }
                busy.Add(k);
                items.Add(new OutTransfer(this, ap));
            }
            if (items.Count == 0) return;
            Batch batch = new Batch(items);
            lock (Lock)
            {
                foreach (OutTransfer t in items)
                {
                    t.Batch = batch;
                    t.Num_ = NextNum();
                    Outgoing[t.Tid] = t;
                }
            }
            U.StartThread(delegate { StartBatch(batch); });
        }

        void StartBatch(Batch batch)
        {
            List<OutTransfer> ok = batch.Members.Where(t => t.Prepare()).ToList();
            batch.Members = ok;
            if (ok.Count == 0) { TransferEnded(); return; }
            if (ok.Count > 1)
            {
                if (SessionAlive()) Con.S("[*] Offered " + batch.Describe() + ". Waiting for the other user to answer...");
                else Con.S("[*] " + batch.Describe() + " will be offered as soon as the other user is connected.");
            }
            foreach (OutTransfer t in ok)
            {
                OutTransfer tt = t;
                U.StartThread(tt.Run);
            }
        }

        static string Agg(string label, List<double[]> rows, string firstName, string note)
        {
            double size = rows.Sum(r => r[0]), done = rows.Sum(r => r[1]), rate = rows.Sum(r => r[2]);
            double pct = size > 0 ? 100.0 * done / size : 100.0;
            double eta = rate > 0 ? (size - done) / rate : -1;
            string name = rows.Count == 1 ? U.Short(firstName, 18) : rows.Count + " items";
            return label + " " + name + " " + U.F1(pct) + "% " + U.Human(done) + "/" + U.Human(size) + " " + U.Human(rate) + "/s ETA " + U.FmtEta(eta) + note;
        }

        string StatusLine()
        {
            bool alive = SessionAlive();
            List<OutTransfer> ups = OutList().Where(t => t.AcceptedOnce && !t.Terminal()).ToList();
            List<InTransfer> downs = InList().Where(t => t.State == "active").ToList();
            List<string> parts = new List<string>();
            if (ups.Count > 0)
            {
                string note = alive ? "" : " paused";
                if (alive && ups.Count == 1 && ups[0].State == "queued") note = " queued";
                parts.Add(Agg("UP", ups.Select(t => new double[] { t.Size, t.Acked, t.Meter.Rate() }).ToList(), ups[0].Name, note));
            }
            if (downs.Count > 0)
                parts.Add(Agg("DOWN", downs.Select(t => new double[] { t.Size, t.Written, t.Meter.Rate() }).ToList(), downs[0].Name, alive ? "" : " paused"));
            return string.Join(" | ", parts.ToArray());
        }

        void PrintStatus()
        {
            Session s = Session;
            if (s != null && s.Alive) Con.S("[*] Connected to " + s.PeerName + " via " + s.Label + ".");
            else Con.S("[*] Not connected. " + (Dialer.HasTarget() ? "Trying " + Dialer.Raw : "Waiting for the other user to connect."));
            Con.S("[*] Listening on port " + Port + ". Downloads folder: " + DownloadDir);
            List<KeyValuePair<string, ITransfer>> rows = RunningList();
            List<InTransfer> asking = InList().Where(t => t.State == "asking").ToList();
            if (rows.Count == 0 && asking.Count == 0) { Con.S("[*] Transfers: none running."); return; }
            if (rows.Count > 0)
            {
                Con.S("[*] Running transfers (Ctrl+Z or cancel to stop some):");
                for (int i = 0; i < rows.Count; i++) Con.S(RowText(i + 1, rows[i].Key, rows[i].Value));
            }
            foreach (InTransfer t in asking) Con.S("    waiting for your y/n answer: " + t.Name + " (" + U.Human(t.Size) + ")");
        }

        void Help()
        {
            Con.S("Commands:\n" +
                  "  upload <files/folders>   offer one or more files/folders (any type, any size)\n" +
                  "                           e.g. upload movie.mp4 \"My Photos\" notes.pdf   or   upload *.jpg\n" +
                  "  Ctrl+Z  or cancel        choose which uploads/downloads to stop (2, or 2 3 4, or all)\n" +
                  "  cancel 2 3               stop transfers 2 and 3 directly (numbers from status)\n" +
                  "  connect <address>        connect to a new address (e.g. a new tunnel URL)\n" +
                  "  status                   connection and every transfer with its number\n" +
                  "  exit    or Ctrl+C        quit\n" +
                  "You can upload more at any time, even while other files are still moving.");
        }

        static readonly HashSet<string> CMD_WORDS = new HashSet<string> { "cancel", "stop", "exit", "quit", "upload", "send", "status", "list", "help", "connect" };

        /// Returns false when the user wants to quit.
        public bool HandleLine(string line)
        {
            if (Menu != null)
            {
                string l = line.Trim().ToLowerInvariant();
                if (l == "exit" || l == "quit") return false;
                MenuInput(line);
                return true;
            }
            bool asking;
            lock (Lock) asking = prompts.Count > 0;
            if (asking)
            {
                string word = line.Trim().Split(' ')[0].ToLowerInvariant();
                if (!CMD_WORDS.Contains(word)) { AnswerPrompt(line); return true; }
            }
            string trimmed = line.Trim();
            int sp = trimmed.IndexOf(' ');
            string cmd = sp < 0 ? trimmed : trimmed.Substring(0, sp);
            string arg = sp < 0 ? "" : trimmed.Substring(sp + 1);
            string c = cmd.ToLowerInvariant();
            if (c.Length == 0) return true;
            if ((c == "y" || c == "n" || c == "yes" || c == "no") && arg.Length == 0) Con.S("[*] No download question is open right now.");
            else if (c == "upload" || c == "up" || c == "send" || c == "u") Upload(arg);
            else if (c == "cancel" || c == "stop") OpenStopMenu(arg.Trim().Length > 0 ? arg.Trim() : null);
            else if (c == "exit" || c == "quit" || c == "q") return false;
            else if (c == "status" || c == "list" || c == "ls") PrintStatus();
            else if (c == "connect")
            {
                if (arg.Trim().Length > 0) Dialer.SetTarget(arg.Trim(), true);
                else Con.S("Usage: connect <address or URL>");
            }
            else if (c == "help" || c == "?" || c == "h") Help();
            else
            {
                List<string> paths, missing;
                U.ParsePaths(line, out paths, out missing);
                if (paths.Count > 0 && missing.Count == 0) Upload(line);   // dragged files in without typing 'upload'
                else Con.S("Unknown command. Type help to see the commands.");
            }
            return true;
        }

        // ---- lifecycle ----
        void Ticker()
        {
            while (!Stopping)
            {
                Thread.Sleep(Con.I.Tty ? 1000 : 5000);
                try { Con.I.SetStatus(StatusLine()); } catch { }
            }
        }

        void Watchdog()
        {
            while (!Stopping)
            {
                Thread.Sleep(500);
                double now = U.Now();
                List<Session> ss;
                lock (Lock) ss = Sessions.ToList();
                foreach (Session s in ss)
                    if (s.Alive && s.Attached && now - s.LastRx > s.conn.DeadAfter) s.Kill();
                try { CollectOverdue(); } catch { }
            }
        }

        int shutdownDone = 0;
        public void Shutdown()
        {
            if (Interlocked.Exchange(ref shutdownDone, 1) == 1) return;
            Stopping = true;
            Listener.Stop();
            Session sess = Session;
            if (sess != null && sess.Alive)
            {
                sess.SendCtrl("t", "bye");
                sess.FlushAndClose(0.6);
            }
            List<Session> ss;
            lock (Lock) ss = Sessions.ToList();
            foreach (Session s in ss) s.Kill();
            foreach (InTransfer t in InList()) t.CloseFiles();
            Listener.Stop();
        }

        public void Run()
        {
            U.StartThread(Listener.Run);
            U.StartThread(Dialer.Run);
            U.StartThread(Ticker);
            U.StartThread(Watchdog);
            Editor = new LineEditor(CtrlZ);
            try
            {
                while (true)
                {
                    string line;
                    try { line = Editor.ReadLine(); }
                    catch (OperationCanceledException) { return; }
                    if (line == null)
                    {
                        if (Editor.RawMode) return;
                        while (true) Thread.Sleep(3600 * 1000);   // no keyboard (e.g. started in background): keep serving
                    }
                    if (!HandleLine(line)) return;
                }
            }
            finally { Editor.Close(); }
        }

        public void CtrlZ()
        {
            U.StartThread(delegate { OpenStopMenu(null); });
        }
    }

    // ======================================================================
    // Listening side: one port that understands every connection type
    // ======================================================================
    class Listener
    {
        readonly App app;
        Socket srv;
        volatile bool stopped;
        readonly Dictionary<string, PollServerConn> polls = new Dictionary<string, PollServerConn>();
        static readonly string[] HTTP_METHODS = { "GET ", "POST", "HEAD", "PUT ", "OPTI", "DELE", "PATC", "CONN" };

        public Listener(App app) { this.app = app; }

        public void Stop()
        {
            stopped = true;
            try { if (srv != null) srv.Close(); } catch { }
        }

        static Socket MakeListener(int port)
        {
            Exception last = null;
            for (int attempt = 0; attempt < 2; attempt++)
            {
                Socket s = null;
                try
                {
                    if (attempt == 0)
                    {
                        if (!Socket.OSSupportsIPv6) continue;
                        s = new Socket(AddressFamily.InterNetworkV6, SocketType.Stream, ProtocolType.Tcp);
                        try { s.DualMode = true; } catch { }
                    }
                    else s = new Socket(AddressFamily.InterNetwork, SocketType.Stream, ProtocolType.Tcp);
                    if (K.IS_WINDOWS) { try { s.ExclusiveAddressUse = true; } catch { } }
                    else { try { s.SetSocketOption(SocketOptionLevel.Socket, SocketOptionName.ReuseAddress, true); } catch { } }
                    s.Bind(new IPEndPoint(attempt == 0 ? IPAddress.IPv6Any : IPAddress.Any, port));
                    s.Listen(64);
                    return s;
                }
                catch (Exception e)
                {
                    last = e;
                    if (s != null) try { s.Close(); } catch { }
                }
            }
            throw last ?? new IOException("cannot listen on port " + port);
        }

        public void Run()
        {
            double delay = 1.0;
            bool warned = false;
            while (!stopped)
            {
                try { srv = MakeListener(app.Port); break; }
                catch (Exception exc)
                {
                    if (!warned)
                    {
                        warned = true;
                        Con.S("[!] Port " + app.Port + " is busy (" + U.ErrText(exc) + "). Retrying in the background; connecting out still works.");
                    }
                    U.Sleep(delay);
                    delay = Math.Min(delay * 1.5, 3.0);
                }
            }
            if (stopped) return;
            if (warned) Con.S("[+] Now listening on port " + app.Port + ".");
            while (!stopped)
            {
                Socket c;
                try { c = srv.Accept(); }
                catch
                {
                    if (stopped) break;
                    Thread.Sleep(200);
                    continue;
                }
                Socket cc = c;
                U.StartThread(delegate { Handle(cc); });
            }
        }

        void Handle(Socket raw)
        {
            Stream st = null;
            try
            {
                Net.Tune(raw);
                string addr = "";
                try { addr = ((IPEndPoint)raw.RemoteEndPoint).Address.ToString(); } catch { }
                raw.ReceiveTimeout = (int)(K.SNIFF_TIMEOUT * 1000);
                byte[] one = new byte[1];
                int got;
                try { got = raw.Receive(one, 0, 1, SocketFlags.Peek); } catch { return; }
                if (got <= 0) return;
                bool tls = false;
                if (one[0] == 0x16)
                {
                    X509Certificate2 cert = Net.ServerCert();
                    if (cert == null) return;
                    SslStream ssl = new SslStream(new NetworkStream(raw, true), false);
                    st = ssl;
                    raw.SendTimeout = (int)(K.SNIFF_TIMEOUT * 1000);
                    try { ssl.AuthenticateAsServer(cert, false, Net.Protocols(), false); } catch { return; }
                    tls = true;
                }
                else st = new NetworkStream(raw, true);
                byte[] buf = new byte[64];
                int n = 0;
                while (n < K.MAGIC.Length)
                {
                    if (n >= 4)
                    {
                        bool prefix = true;
                        for (int i = 0; i < n; i++) if (buf[i] != K.MAGIC[i]) { prefix = false; break; }
                        if (!prefix) break;
                    }
                    int r = st.Read(buf, n, buf.Length - n);
                    if (r <= 0) return;
                    n += r;
                }
                raw.ReceiveTimeout = 0;
                raw.SendTimeout = 0;
                bool remotePrivate = U.IsLanPeer(addr);
                if (U.StartsWith(buf, n, K.MAGIC))
                {
                    byte[] pre = new byte[n - K.MAGIC.Length];
                    Buffer.BlockCopy(buf, K.MAGIC.Length, pre, 0, pre.Length);
                    StreamConn conn = new StreamConn(raw, st, tls ? "TLS-encrypted TCP" : "TCP", pre);
                    st = null;
                    Session sess = new Session(app, conn, false);
                    sess.RemotePrivate = remotePrivate;
                    try { app.RunSession(sess, true); } catch { }
                    conn.Close();
                    return;
                }
                if (n >= 4 && HTTP_METHODS.Contains(Encoding.ASCII.GetString(buf, 0, 4)))
                {
                    byte[] pre = new byte[n];
                    Buffer.BlockCopy(buf, 0, pre, 0, n);
                    StreamConn conn = new StreamConn(raw, st, "http", pre);
                    st = null;
                    try { Http(conn, tls, remotePrivate); } finally { conn.Close(); }
                }
            }
            catch { }
            finally
            {
                try { if (st != null) st.Close(); } catch { }
                Net.CloseSock(raw);
            }
        }

        // ---- HTTP: WebSocket upgrade, polling transport, and a friendly page ----
        void Http(StreamConn conn, bool tls, bool remotePrivate)
        {
            Reader rd = new Reader(conn);
            byte[] dcrlf = Encoding.ASCII.GetBytes("\r\n\r\n");
            while (!stopped)
            {
                byte[] head;
                try { head = rd.Until(dcrlf, 65536, U.Now() + K.HTTP_IDLE); } catch { return; }
                string[] first; Dictionary<string, string> headers;
                Net.ParseHead(head, out first, out headers);
                if (first.Length < 3) return;
                string method = first[0].ToUpperInvariant(), target = first[1], version = first[2].ToUpperInvariant();
                byte[] body = ReadBody(rd, headers);
                if (body == null) return;
                int q = target.IndexOf('?');
                string path = q < 0 ? target : target.Substring(0, q);
                string query = q < 0 ? "" : target.Substring(q + 1);
                if (path.Contains("://"))                     // absolute-form request
                {
                    string rest = path.Substring(path.IndexOf("://") + 3);
                    int sl = rest.IndexOf('/');
                    path = "/" + (sl < 0 ? "" : rest.Substring(sl + 1));
                }
                Dictionary<string, string> prm = new Dictionary<string, string>();
                foreach (string p in query.Split('&'))
                {
                    if (p.Length == 0) continue;
                    int e = p.IndexOf('=');
                    if (e < 0) prm[p] = ""; else prm[p.Substring(0, e)] = p.Substring(e + 1);
                }
                string connHdr = Net.H(headers, "connection").ToLowerInvariant();
                bool keep = version == "HTTP/1.1" && connHdr != "close";
                if (!keep && connHdr.Contains("keep-alive")) keep = true;
                if (path == "/fs/ws" && Net.H(headers, "upgrade").ToLowerInvariant().Contains("websocket"))
                {
                    string accept = U.WsAccept(Net.H(headers, "sec-websocket-key"));
                    byte[] resp = Net.HttpHead(new List<string> { "HTTP/1.1 101 Switching Protocols", "Upgrade: websocket",
                        "Connection: Upgrade", "Sec-WebSocket-Accept: " + accept, K.MARK_HEADER + ": 1" });
                    conn.Send(resp, 0, resp.Length);
                    WSConn ws = new WSConn(conn, false, tls ? "WebSocket over HTTPS" : "WebSocket over HTTP", rd.Buf.ToArray());
                    Session sess = new Session(app, ws, false);
                    sess.RemotePrivate = remotePrivate;
                    try { app.RunSession(sess, false); } catch { }
                    return;
                }
                int status; long seq = -1; byte[] data;
                if (path.StartsWith("/fs/p/")) status = Poll(path, prm, method, body, tls, remotePrivate, out seq, out data);
                else { status = 200; data = Encoding.ASCII.GetBytes(K.APP_NAME + " is running. Use file-share.py or file-share.cs to connect.\n"); }
                string reason = status == 200 ? "OK" : status == 404 ? "Not Found" : status == 410 ? "Gone" : status == 400 ? "Bad Request" : "OK";
                List<string> lines = new List<string> {
                    "HTTP/1.1 " + status + " " + reason,
                    "Content-Type: " + (path.StartsWith("/fs/") ? "application/octet-stream" : "text/plain"),
                    "Content-Length: " + (method != "HEAD" ? data.Length : 0),
                    "Cache-Control: no-store", K.MARK_HEADER + ": 1",
                    "Connection: " + (keep ? "keep-alive" : "close") };
                if (seq >= 0) lines.Add("X-Seq: " + seq);
                try
                {
                    byte[] h = Net.HttpHead(lines);
                    byte[] all = method != "HEAD" && data.Length > 0 ? U.Concat(h, data) : h;
                    conn.Send(all, 0, all.Length);
                }
                catch { return; }
                if (!keep) return;
            }
        }

        static byte[] ReadBody(Reader rd, Dictionary<string, string> headers)
        {
            try
            {
                double deadline = U.Now() + 120;
                if (Net.H(headers, "transfer-encoding").ToLowerInvariant().Contains("chunked"))
                {
                    ByteBuf outb = new ByteBuf();
                    byte[] crlf = { 13, 10 };
                    while (true)
                    {
                        byte[] line = rd.Until(crlf, 1024, deadline);
                        string ln = Encoding.ASCII.GetString(line).Split(';')[0].Trim();
                        int size = Convert.ToInt32(ln.Length > 0 ? ln : "0", 16);
                        if (size == 0)
                        {
                            while (rd.Until(crlf, 8192, deadline).Length != 2) { }
                            return outb.ToArray();
                        }
                        if (outb.Count + (long)size > K.MAX_FRAME) return null;
                        outb.Append(rd.Exact(size, deadline));
                        rd.Exact(2, deadline);
                    }
                }
                string cl = Net.H(headers, "content-length");
                long n = cl.Length > 0 ? long.Parse(cl) : 0;
                if (n < 0 || n > K.MAX_FRAME) return null;
                return n > 0 ? rd.Exact((int)n, deadline) : Bytes.Empty;
            }
            catch { return null; }
        }

        int Poll(string path, Dictionary<string, string> prm, string method, byte[] body, bool tls, bool remotePrivate, out long seq, out byte[] data)
        {
            seq = -1; data = Bytes.Empty;
            string[] parts = path.Trim('/').Split('/');   // fs, p, new | fs, p, sid, op
            if (parts.Length == 3 && parts[2] == "new" && method == "POST")
            {
                PollServerConn pc = new PollServerConn(tls ? "HTTPS polling" : "HTTP polling");
                lock (polls)
                {
                    foreach (string sid in polls.Where(kv => kv.Value.IsClosed).Select(kv => kv.Key).ToList()) polls.Remove(sid);
                    polls[pc.Sid] = pc;
                }
                Session sess = new Session(app, pc, false);
                sess.RemotePrivate = remotePrivate;
                U.StartThread(delegate {
                    try { app.RunSession(sess, false); } catch { }
                    finally { pc.Close(); }
                });
                data = Encoding.ASCII.GetBytes(pc.Sid);
                return 200;
            }
            if (parts.Length != 4) return 404;
            PollServerConn c;
            lock (polls) polls.TryGetValue(parts[2], out c);
            if (c == null) return 404;
            try
            {
                string v;
                if (parts[3] == "up") return c.HandleUp(long.Parse(prm.TryGetValue("seq", out v) ? v : "0"), body);
                if (parts[3] == "down")
                {
                    long s; byte[] d;
                    int st = c.HandleDown(long.Parse(prm.TryGetValue("ack", out v) ? v : "0"), out s, out d);
                    seq = s; data = d;
                    return st;
                }
                if (parts[3] == "close") { c.Close(); return 200; }
            }
            catch (FormatException) { return 400; }
            catch (OverflowException) { return 400; }
            return 404;
        }
    }

    // ======================================================================
    // Connecting side: works out the right protocol by itself
    // ======================================================================
    class Cand
    {
        public string Proto; public int Port;
        public Cand(string p, int port) { Proto = p; Port = port; }
        public override bool Equals(object o) { Cand c = o as Cand; return c != null && c.Proto == Proto && c.Port == Port; }
        public override int GetHashCode() { return Proto.GetHashCode() ^ Port; }
    }

    class Dialer
    {
        readonly App app;
        public string Raw = "";
        string host;
        List<Cand> cands = new List<Cand>();
        Cand best;
        readonly AutoResetEvent ev = new AutoResetEvent(false);
        volatile bool selfTarget;
        bool busyNoted;
        readonly object lk = new object();
        volatile int gen;
        double lastProbe = U.Now();
        string scheme;
        int fails;
        double failSince = -1;
        bool toldSoft;

        public Dialer(App app) { this.app = app; }

        public bool HasTarget() { return !string.IsNullOrEmpty(host) && !selfTarget; }

        /// Tunnels that count every connection/request against a monthly quota (ngrok free):
        /// retry more gently there.
        bool Metered()
        {
            return !string.IsNullOrEmpty(host) && Regex.IsMatch(host.ToLowerInvariant(), @"(^|\.)ngrok(-free)?\.(app|dev|io)$");
        }

        public void Wake() { ev.Set(); }

        public void NoteBusy()
        {
            if (!busyNoted)
            {
                busyNoted = true;
                Con.S("[!] The other computer is already sharing with someone else. Will keep trying.");
            }
        }

        /// IP, hostname, host:port, [v6]:port, or scheme://host[:port]/...
        public static void ParseTarget(string raw, int defaultPort, out string host, out int port, out string scheme, out bool explicitPort)
        {
            string s = raw.Trim().Trim('"', '\'');
            scheme = null;
            int sep = s.IndexOf("://");
            if (sep >= 0)
            {
                scheme = s.Substring(0, sep).Trim().ToLowerInvariant();
                s = s.Substring(sep + 3);
                if (scheme == "ws") scheme = "http";
                else if (scheme == "wss" || scheme == "tls" || scheme == "ssl") scheme = "https";
                if (scheme != "tcp" && scheme != "http" && scheme != "https") scheme = null;
            }
            s = s.Split('/')[0].Split('?')[0].Split('#')[0];
            int at = s.LastIndexOf('@');
            if (at >= 0) s = s.Substring(at + 1);
            host = s;
            int p = -1;
            if (s.StartsWith("["))
            {
                int end = s.IndexOf(']');
                host = end > 0 ? s.Substring(1, end - 1) : s.Trim('[', ']');
                string rest = end > 0 ? s.Substring(end + 1) : "";
                int pv;
                if (rest.StartsWith(":") && int.TryParse(rest.Substring(1), out pv) && Regex.IsMatch(rest.Substring(1), @"^\d+$")) p = pv;
            }
            else if (s.Count(ch => ch == ':') == 1)
            {
                string[] hp = s.Split(':');
                int pv;
                if (Regex.IsMatch(hp[1], @"^\d+$") && int.TryParse(hp[1], out pv)) { host = hp[0]; p = pv; }
            }
            host = host.Trim().TrimEnd('.');
            explicitPort = p >= 0;
            if (p < 0) p = scheme == "https" ? 443 : scheme == "http" ? 80 : defaultPort;
            port = p;
        }

        /// (protocol, port) to try, most likely first.
        public static List<Cand> Candidates(int port, string scheme, bool explicitPort)
        {
            string[] order;
            if (scheme == "tcp") order = new string[] { "tcp", "tls", "ws", "wss", "poll", "polls" };
            else if (scheme == "http") order = new string[] { "ws", "poll", "wss", "polls", "tcp", "tls" };
            else if (scheme == "https") order = new string[] { "wss", "polls", "tls", "ws", "poll", "tcp" };
            else if (port == 443) order = new string[] { "wss", "tls", "polls", "tcp", "ws", "poll" };
            else if (port == 80) order = new string[] { "ws", "poll", "tcp", "wss", "tls", "polls" };
            else order = new string[] { "tcp", "tls", "ws", "wss", "poll", "polls" };
            List<Cand> outl = order.Select(p => new Cand(p, port)).ToList();
            if (scheme == null && !explicitPort)
            {
                outl.Add(new Cand("wss", 443)); outl.Add(new Cand("ws", 80));       // bare tunnel hostname
                outl.Add(new Cand("polls", 443)); outl.Add(new Cand("poll", 80));
            }
            string only = (Environment.GetEnvironmentVariable("FILESHARE_PROTO") ?? "").Trim().ToLowerInvariant();   // testing aid
            if (only.Length > 0)
            {
                string[] allowed = only.Split(',');
                List<Cand> f = outl.Where(c => allowed.Contains(c.Proto)).ToList();
                if (f.Count > 0) outl = f;
            }
            return outl;
        }

        public void SetTarget(string raw, bool announce)
        {
            string h, sc; int port; bool ex;
            ParseTarget(raw, app.Port, out h, out port, out sc, out ex);
            if (h.Length == 0) { Con.S("[!] That address looks empty."); return; }
            lock (lk)
            {
                Raw = raw;
                host = h;
                cands = Candidates(port, sc, ex);
                scheme = sc;
                fails = 0;
                failSince = -1;
                toldSoft = false;
                best = null;
                selfTarget = false;
                busyNoted = false;
                gen++;
            }
            if (announce)
            {
                Con.S("[*] Will connect to " + raw + " (protocol detected automatically).");
                Session s = app.Session;
                if (s != null && s.Alive && s.Dialer) s.Kill();     // switch now; transfers resume on the new link
            }
            ev.Set();
        }

        public void Run()
        {
            double delay = 1.0;
            bool announced = false;
            while (!app.Stopping)
            {
                if (!HasTarget() || app.SessionAlive())
                {
                    try { MaybeUpgrade(); } catch { }
                    ev.WaitOne(1000);
                    continue;
                }
                int g; string h; List<Cand> order;
                lock (lk)
                {
                    g = gen; h = host;
                    order = cands.ToList();
                    if (best != null && order.Contains(best))
                    {
                        order.Remove(best);
                        order.Insert(0, best);
                        if (fails > 0 && fails % 6 != 0) order = new List<Cand> { best };   // reconnecting: the type that worked; full scan every 6th try
                    }
                }
                if (!announced)
                {
                    announced = true;
                    Con.S("[*] Connecting to " + Raw + " ...");
                }
                string result;
                try { result = Attempt(h, order, g); } catch { result = "fail"; }
                if (result == "ok" || result == "dup")
                {
                    delay = 1.0;
                    announced = true;
                    fails = 0; failSince = -1; toldSoft = false;
                    ev.WaitOne(1000);
                    continue;
                }
                if (result == "self")
                {
                    Con.S("[*] " + Raw + " points to this computer itself (your own tunnel). Waiting for the other user to connect to you.");
                    continue;
                }
                if (result == "soft" && !toldSoft)
                {
                    toldSoft = true;
                    Con.S("[*] The tunnel is up, but " + K.APP_NAME + " is not running on the other side yet. Waiting for it...");
                }
                fails++;
                if (failSince < 0) failSince = U.Now();
                double cap = K.RETRY_MAX;
                if (Metered()) cap = U.Now() - failSince < 600 ? 30.0 : 60.0;
                ev.WaitOne((int)(delay * 1000));
                delay = Math.Min(delay * 2, cap);
            }
        }

        /// On the slow HTTP-polling fallback, check now and then whether a WebSocket works
        /// and switch to it.
        void MaybeUpgrade()
        {
            Session s = app.Session;
            if (s == null || !s.Alive || !s.Dialer || !(s.conn is PollClientConn)) return;
            double now = U.Now();
            if (now - lastProbe < 60.0) return;
            lastProbe = now;
            string h; int g; List<Cand> order;
            lock (lk) { h = host; g = gen; order = cands.Where(c => c.Proto == "ws" || c.Proto == "wss").ToList(); }
            foreach (Cand c in order)
            {
                IConn conn;
                try { conn = Net.WsClient(h, c.Port, c.Proto == "wss"); } catch { continue; }
                conn.Close();
                lock (lk)
                {
                    if (g != gen) return;
                    best = c;
                }
                Con.S("[*] A faster connection type is available; switching (transfers continue).");
                s.Kill();
                ev.Set();
                return;
            }
        }

        string Attempt(string h, List<Cand> order, int g)
        {
            HashSet<int> downPorts = new HashSet<int>();
            bool softEof = false;
            foreach (Cand c in order)
            {
                if (app.Stopping || g != gen || app.SessionAlive()) return "stale";
                if (downPorts.Contains(c.Port)) continue;
                if (softEof && (c.Proto == "poll" || c.Proto == "polls")) continue;   // the app behind the tunnel may still be starting
                IConn conn;
                try { conn = Open(c.Proto, h, c.Port); }
                catch (HostDown) { return "down"; }
                catch (PortDown) { downPorts.Add(c.Port); continue; }
                catch (TunnelNoApp) { return "soft"; }                 // an HTTP tunnel answered for the missing app
                catch (SoftEOF)
                {
                    softEof = true;
                    if (scheme == "tcp" && c.Proto == "tcp") return "soft";   // a TCP tunnel with nothing behind it
                    continue;
                }
                catch { continue; }                                   // something answered, but not this protocol
                Session sess = new Session(app, conn, true);
                sess.RemotePrivate = U.IsLanPeer(h);
                lock (app.Lock) app.Sessions.Add(sess);
                bool ok;
                try
                {
                    sess.Handshake(false);
                    if (sess.PeerId == K.INSTANCE_ID) throw new NotUs("self");
                    if (sess.PeerBusy.Length > 0 && sess.PeerBusy != K.INSTANCE_ID) throw new BusyEx();
                    if (g != gen) throw new NotUs("address changed meanwhile");
                    sess.StartWriter();
                    ok = app.Attach(sess);
                }
                catch (Exception exc)
                {
                    sess.Alive = false;
                    try { conn.Close(); } catch { }
                    lock (app.Lock) app.Sessions.Remove(sess);
                    if (sess.PeerId == K.INSTANCE_ID)
                    {
                        lock (lk) { if (g == gen) selfTarget = true; }
                        return "self";
                    }
                    if (exc is BusyEx) { NoteBusy(); return "busy"; }
                    if (exc is SoftEOF)
                    {
                        softEof = true;
                        if (exc is TunnelNoApp || (scheme == "tcp" && c.Proto == "tcp")) return "soft";
                    }
                    continue;
                }
                if (!ok)
                {
                    sess.FlushAndClose(0.3);
                    return "dup";
                }
                lock (lk) { best = c; busyNoted = false; }
                Session ss = sess;
                U.StartThread(ss.RunReader);
                return "ok";
            }
            return softEof ? "soft" : "fail";
        }

        IConn Open(string proto, string h, int port)
        {
            if (proto == "tcp") return Net.PlainConn(Net.ConnectTcp(h, port), "TCP");
            if (proto == "tls") return Net.ConnectTls(h, port, "TLS-encrypted TCP");
            if (proto == "ws" || proto == "wss") return Net.WsClient(h, port, proto == "wss");
            return new PollClientConn(h, port, proto == "polls").Open();
        }
    }

    // ======================================================================
    // Main
    // ======================================================================
    static class Program
    {
        static App app;

        static string ScriptDir()
        {
            // .NET 10 "dotnet run file-share.cs" tells us where the .cs file is
            try
            {
                object d = AppDomain.CurrentDomain.GetData("EntryPointFileDirectoryPath");
                if (d is string && ((string)d).Length > 0) return (string)d;
            }
            catch { }
            try
            {
                string b = AppDomain.CurrentDomain.BaseDirectory;
                if (!string.IsNullOrEmpty(b) && b.IndexOf("runfile", StringComparison.OrdinalIgnoreCase) < 0) return b;
            }
            catch { }
            return null;
        }

        static string PickDownloadDir(string configured)
        {
            List<string> cands = new List<string>();
            if (configured.Length > 0) { try { cands.Add(Path.GetFullPath(U.ExpandUser(configured))); } catch { } }
            string here = ScriptDir();
            if (here != null) cands.Add(Path.Combine(here, "FileShare Downloads"));
            cands.Add(Path.Combine(Directory.GetCurrentDirectory(), "FileShare Downloads"));
            string home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
            if (!string.IsNullOrEmpty(home)) cands.Add(Path.Combine(home, "FileShare Downloads"));
            cands.Add(Path.Combine(Path.GetTempPath(), "FileShare Downloads"));
            foreach (string d in cands)
            {
                try
                {
                    Directory.CreateDirectory(d);
                    string probe = Path.Combine(d, ".write-test-" + K.INSTANCE_ID);
                    File.WriteAllBytes(probe, new byte[] { 111, 107 });
                    File.Delete(probe);
                    return Path.GetFullPath(d);
                }
                catch { }
            }
            return cands[cands.Count - 1];
        }

        static readonly List<object> keep = new List<object>();

        /// Linux/macOS terminals send SIGTSTP for Ctrl+Z: use it to cancel instead of suspending.
        /// (.NET 6+ only; found by reflection so this file still compiles everywhere.)
        static void InstallCtrlZ()
        {
            if (K.IS_WINDOWS) return;
            try
            {
                Type reg = Type.GetType("System.Runtime.InteropServices.PosixSignalRegistration, System.Runtime.InteropServices", false);
                if (reg == null)
                    foreach (Assembly a in AppDomain.CurrentDomain.GetAssemblies())
                    {
                        reg = a.GetType("System.Runtime.InteropServices.PosixSignalRegistration", false);
                        if (reg != null) break;
                    }
                if (reg == null) return;
                Type sig = reg.Assembly.GetType("System.Runtime.InteropServices.PosixSignal");
                Type ctx = reg.Assembly.GetType("System.Runtime.InteropServices.PosixSignalContext");
                if (sig == null || ctx == null) return;
                MethodInfo create = reg.GetMethod("Create", BindingFlags.Public | BindingFlags.Static);
                Type actionType = typeof(Action<>).MakeGenericType(ctx);
                Delegate handler = Delegate.CreateDelegate(actionType, typeof(Program).GetMethod("OnTstp", BindingFlags.NonPublic | BindingFlags.Static));
                object r = create.Invoke(null, new object[] { Enum.Parse(sig, "SIGTSTP"), handler });
                keep.Add(r);
            }
            catch { }
        }

        static void OnTstp(object context)
        {
            try
            {
                PropertyInfo p = context.GetType().GetProperty("Cancel");
                if (p != null) p.SetValue(context, true, null);
            }
            catch { }
            if (app != null) app.CtrlZ();
        }

        static int exiting = 0;
        static void Exit(int code)
        {
            if (Interlocked.Exchange(ref exiting, 1) == 1) return;
            Con.I.SetStatus("");
            Con.I.Prompt = "";
            Con.S("\n[*] Closing " + K.APP_NAME + "... (unfinished downloads are kept and resume next time)");
            try { if (app != null) app.Shutdown(); } catch { }
            Con.S("[*] Bye.");
            try { if (app != null && app.Editor != null) app.Editor.Close(); } catch { }
            Environment.Exit(code);
        }

        static void Main(string[] args)
        {
            try { ServicePointManager.SecurityProtocol |= (SecurityProtocolType)(3072 | 768); } catch { }
            string peer = Settings.PEER_ADDRESS;
            int port = Settings.PORT;
            string folder = Settings.DOWNLOAD_FOLDER;
            if (args.Length > 0 && args[0].Trim().Length > 0) peer = args[0].Trim();
            string ev = Environment.GetEnvironmentVariable("FILESHARE_PEER");
            if (ev != null) peer = ev;
            int pv;
            ev = Environment.GetEnvironmentVariable("FILESHARE_PORT");
            if (ev != null && int.TryParse(ev, out pv)) port = pv;
            ev = Environment.GetEnvironmentVariable("FILESHARE_DIR");
            if (ev != null) folder = ev;
            ThreadPool.SetMinThreads(32, 32);

            string downloadDir = PickDownloadDir(folder);
            app = new App(peer.Trim(), port, downloadDir);
            InstallCtrlZ();
            Console.CancelKeyPress += delegate(object s, ConsoleCancelEventArgs e) { e.Cancel = true; U.StartThread(delegate { Exit(0); }); };
            AppDomain.CurrentDomain.ProcessExit += delegate { try { if (app != null) app.Shutdown(); } catch { } };
            List<string> ips = U.LanIps();
            string bar = new string('=', 60);
            Con.S(bar);
            Con.S(" " + K.APP_NAME + " - send and receive files and folders of any size");
            Con.S(bar);
            Con.S(" Listening on port " + port + " (TCP, TLS, HTTP, HTTPS, WebSocket)");
            if (ips.Count > 0) Con.S(" Same Wi-Fi? The other user can use: " + string.Join(", ", ips.Select(ip => ip + ":" + port).ToArray()));
            Con.S(" Internet? Tunnel port " + port + " with Pinggy or ngrok and share the address it prints.");
            Con.S(" Downloads go to: " + downloadDir);
            Con.S(" Commands: upload <files/folders> | Ctrl+Z (stop) | status | connect <address> | help | exit (Ctrl+C)");
            Con.S(bar);
            if (peer.Trim().Length == 0) Con.S("[*] Waiting for the other user to connect...");
            try { app.Run(); }
            catch (Exception e) { Con.S("[!] " + U.ErrText(e)); }
            Exit(0);
        }
    }
}
