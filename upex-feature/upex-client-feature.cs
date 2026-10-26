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


