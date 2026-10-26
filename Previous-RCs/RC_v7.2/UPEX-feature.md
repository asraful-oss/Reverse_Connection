# 🚀 UPEX — Upload & Execute

> **One command to upload an app, optionally upload an auto launcher, and run it on the connected client.**

<p align="center">
  <code>upex app.py</code> &nbsp;·&nbsp;
  <code>upex auto.js app.ts</code> &nbsp;·&nbsp;
  <code>upex -h auto.py app.py</code>
</p>

---

## ✨ At a Glance

| Rule | Command | What gets uploaded | What runs |
|---|---|---|---|
| **1** | `upex app.<type>` | The app file | The app directly |
| **2** | `upex auto.<type> app.<type>` | The app **and** one auto launcher | The auto launcher with the app filename as its argument |
| **3** | `upex -h auto.<type> app.<type>` | Same as Rule 2 | Same as Rule 2, plus `--hidden` |

> 💡 `upex` works from the controller prompt. The controller uploads the file(s) to the client's **current working directory**, then tells the client how to compile or run them.

> 🧭 Run `show options` at the controller prompt to see the `upex` rows alongside every other command.

> 🧰 `upex` assumes the required interpreters and compilers are already installed on the client. If one is missing, the client returns a clear error instead of guessing.

> [!NOTE]
> `upex` executes code on the connected machine. Use it only on systems you own or have explicit permission to test.

---

## 🧭 How It Works

```text
controller> upex auto.py app.py
      │
      ├─ 1. Parse the command and validate the local file(s)
      ├─ 2. Detect the app type from the extension or file contents
      ├─ 3. Detect the target OS from the session environment
      ├─ 4. Find the requested auto launcher
      ├─ 5. Upload app first, then auto (streamed transfer)
      ├─ 6. Send one "upex" request to the client
      │
      ▼
   client
      ├─ 7. Verify the uploaded files exist
      ├─ 8. Choose the native interpreter / compiler / runner
      ├─ 9. Compile first when required, then run
      └─ 10. Return output + exit code to the controller
```

<details>
<summary><strong>🔍 What happens under the hood</strong></summary>

<br>

### Controller side

1. **Parse** the command into the app path and optional auto launcher.
2. **Check** that the app is a real local file.
3. **Detect the app type**:
   - extension first (`.py`, `.c`, `.cpp`, `.cs`, `.js`, `.sh`, etc.);
   - file magic or shebang for extensionless files.
4. **Read the target OS** from the connected client's session environment. The `session` command lists each client's OS and address.
5. **Validate** that the app type is allowed for the selected mode.
6. **Find the auto launcher** when Rule 2 or Rule 3 is used.
7. **Upload** the app, then the auto launcher, using the existing streamed transfer protocol.
8. **Send** a compact request:

```json
{
  "type": "upex",
  "app": "app.py",
  "app_type": "py",
  "auto": "auto.py",
  "auto_type": "py",
  "hidden": false
}
```

9. **Print** the client's output and return to the prompt.

### Client side

1. Verify the named files exist in the current directory.
2. Confirm the app type.
3. Build native commands for **this** operating system.
4. Compile if needed. Try fallback compilers only when compilation fails.
5. Run the app or auto launcher.
6. Return combined output and, when available, an `exit_code`.

The client never prints anything by itself. Its console output is sent back to the controller as normal command output.

</details>

---

## 🚀 Quick Start

```bash
# Rule 1 — upload and run an app
upex app.py
upex /home/me/projects/app.c
upex "My App.js"
upex app.elf
upex app.macho
upex app.sh

# Rule 2 — upload an auto launcher and an app
upex auto.py app.py
upex auto.cs app.ts
upex auto.js app.ts
upex auto.sh app.sh

# Rule 3 — hidden auto mode
upex -h auto.py app.py
upex --hidden auto.cs app.cs
```

> 📁 Paths may be relative to the controller's current directory or absolute. Quote paths that contain spaces.

---

## 🧩 Rule 1 — App Mode

### Syntax

```text
upex app.<type>
upex /path/to/dir/app.<type>
```

The controller detects the type, uploads **only the app**, and runs it directly.

### Supported app types

| Type | Extensions / aliases | Notes |
|---|---|---|
| 🐍 Python | `.py`, `.pyw` | Runs through Python |
| ⚙️ C | `.c` | Compiles first |
| ⚙️ C++ | `.cpp`, `.c++`, `.cc`, `.cxx` | Compiles first |
| 🟣 C# | `.cs` | Runs through .NET or `csc` fallback |
| 🟨 JavaScript | `.js`, `.mjs`, `.cjs` | Runs through Node.js |
| 🍎 Swift | `.swift` | Runs directly or compiles with `swiftc` |
| 🐚 Shell | `.sh`, `.bash` | `chmod +x` and run |
| 🪟 Batch | `.bat`, `.cmd` | Windows only |
| 🪟 Executable | `.exe` | Windows only |
| 🐧 ELF | `.elf` | Linux only |
| 🍏 Mach-O | `.macho` | macOS only |
| 🧱 Extensionless | no extension | ELF, Mach-O, or shell on Unix; PE on Windows |

> ⚠️ **TypeScript (`.ts`) is not a Rule 1 app type.** Use an auto launcher: `upex auto.js app.ts`.

<details>
<summary><strong>🖥️ Rule 1 native command matrix</strong></summary>

<br>

| App type | 🐧 Linux | 🍎 macOS | 🪟 Windows |
|---|---|---|---|
| Python | `python3 app.py` | `python3 app.py` | `py app.py` |
| C | `gcc app.c -o app` → `./app` | `clang app.c -o app` → `./app` | `cl app.c` → `.\app.exe` |
| C++ | `g++ app.cpp -o app` → `./app` | `clang++ app.cpp -o app` → `./app` | `cl /EHsc app.cpp` → `.\app.exe` |
| C# | `dotnet run --file app.cs` | `dotnet run --file app.cs` | `dotnet run --file app.cs` |
| JavaScript | `node app.js` | `node app.js` | `node app.js` |
| Swift | `swift app.swift` or `swiftc` | `swift app.swift` or `swiftc` | `swift app.swift` or `swiftc` |
| Shell | `chmod +x app.sh` → `./app.sh` | `chmod +x app.sh` → `./app.sh` | `bash app.sh` / `sh app.sh` |
| Batch | — | — | `.\app.bat` |
| EXE | — | — | `.\app.exe` |
| ELF | `chmod +x app.elf` → `./app.elf` | — | — |
| Mach-O | — | `chmod +x app.macho` → `./app.macho` | — |
| Extensionless | `chmod +x app` → `./app` | `chmod +x app` → `./app` | `.\app` for a PE; otherwise use `.exe` |

The controller rejects obviously incompatible combinations, such as `.bat` on Linux or `.elf` on Windows/macOS.

</details>

<details>
<summary><strong>🔁 Compiler and interpreter fallback</strong></summary>

<br>

### C and C++

The client checks which compilers are actually available, then tries them in priority order. It runs the produced binary only after a compile succeeds.

| Platform | C order | C++ order |
|---|---|---|
| Linux | `gcc` → `clang` | `g++` → `clang++` |
| macOS | `clang` → `gcc` | `clang++` → `g++` |
| Windows | `cl` → `gcc` | `cl /EHsc` → `g++` |

- If the first compiler succeeds, the second is **not** run.
- If the first compiler fails, the next available compiler is tried.
- If all compilers fail, the combined compiler output is returned.

### C#

`dotnet` is preferred. On Windows, `csc` is the fallback when `dotnet` is not available.

### Python and JavaScript

- Python: `python3` → `python` on Unix; `py` → `python` on Windows.
- JavaScript: `node` → `nodejs`.
- The first available interpreter is used.

### Swift

- Use `swift app.swift` when `swift` is available.
- Otherwise compile with `swiftc` and run the binary.

### Shell

- Unix: `chmod +x` and run directly.
- Windows: use `bash` or `sh` when available.

</details>

<details>
<summary><strong>🧱 Extensionless files</strong></summary>

<br>

The controller inspects the first bytes of an extensionless file:

- `\x7fELF` → ELF executable
- Mach-O magic → Mach-O executable
- `MZ` → Windows PE executable
- `#!` shebang → Python, Node.js, or shell script
- otherwise on Unix → generic executable (`bin`)

On Linux and macOS the client runs:

```bash
chmod +x app && ./app
```

On Windows, an extensionless `MZ`/PE file is treated as an EXE and run as `.\app`. Unknown extensionless files on Windows are rejected; use an explicit `.exe`.

</details>

---

## 🪄 Rule 2 — Auto Mode

### Syntax

```text
upex auto.<py|cs|js|sh> app.<py|js|cs|ts|sh>
upex auto.<py|cs|js|sh> /path/to/dir/app.<py|js|cs|ts|sh>
```

The controller uploads **two files**:

1. the selected auto launcher;
2. the app.

Then the client runs the auto launcher and passes the app filename as its argument.

### Allowed combinations

| Auto launcher | Allowed app types |
|---|---|
| `auto.py` | `.py`, `.js`, `.cs`, `.ts`, `.sh` |
| `auto.cs` | `.py`, `.js`, `.cs`, `.ts`, `.sh` |
| `auto.js` | `.py`, `.js`, `.cs`, `.ts`, `.sh` |
| `auto.sh` | `.py`, `.js`, `.cs`, `.ts`, `.sh` |

> ⚠️ Auto mode does **not** accept `.exe`, `.bat`, `.elf`, or `.macho`.

> 🔤 Aliases such as `.pyw`, `.mjs`, `.cjs`, and `.bash` are normalized to their base types before validation.

<details>
<summary><strong>📁 Where the controller finds the auto launcher</strong></summary>

<br>

The controller searches next to `controller.py` in this order:

```text
1. <controller_dir>/auto/auto.<type>
2. <controller_dir>/auto.<type>
```

Examples:

```text
/home/me/controller/auto/auto.py   ← preferred
/home/me/controller/auto.py        ← fallback location
```

- The user chooses **which** auto launcher to use by typing `auto.py`, `auto.cs`, `auto.js`, or `auto.sh`.
- The controller uploads **only that one** auto file.
- If the file is missing, `upex` falls back to Rule 1.
- Treat the real `auto/` files as read-only inputs. `upex` only reads and uploads them; do not edit or replace them.

</details>

<details>
<summary><strong>⚡ Auto launcher command matrix</strong></summary>

<br>

| Auto type | Command |
|---|---|
| `auto.py` | `python3 auto.py app.py` on Unix; `python auto.py app.py` on Windows |
| `auto.cs` | `dotnet run auto.cs -- app.py` |
| `auto.js` | `node auto.js app.py` |
| `auto.sh` | `bash auto.sh app.py` → `sh auto.sh app.py` → `chmod +x auto.sh && ./auto.sh app.py` |

The client prefers the first available shell option for `auto.sh`.

> 🔑 The `--` separator is used **only** for `auto.cs`. It is not used for `auto.py`, `auto.js`, or `auto.sh`.

</details>

<details>
<summary><strong>🪫 Missing auto file and fallback behavior</strong></summary>

<br>

If `auto.py` is requested but no auto file exists:

```text
[i] auto.py was not found in the controller's auto/ folder;
    falling back to normal upex.
```

Then the app is uploaded and run using **Rule 1**.

Special cases:

- If the app is `.ts`, the fallback fails because Rule 1 does not support TypeScript.
- If the auto file exists but fails to upload, the app is already uploaded and is run normally when possible; hidden mode is dropped.
- If the auto upload is canceled with **Ctrl+Z**, the app is **not** run.
- If the app and auto launcher have the same basename, `upex` refuses to execute.

</details>

---

## 🥷 Rule 3 — Hidden Mode

### Syntax

```text
upex -h auto.<py|cs|js|sh> app.<py|js|cs|ts|sh>
upex --hidden auto.<py|cs|js|sh> app.<py|js|cs|ts|sh>
```

Hidden mode is exactly Rule 2 with one extra argument appended to the auto launcher command:

```text
--hidden
```

It must be written as one word, with one separating space:

```text
app.py --hidden      ✅  one space, one flag
app.py  --hidden     ❌  two spaces
app.py -- hidden     ❌  split flag
```

### Examples

```bash
upex -h auto.py app.py
upex -h auto.cs app.cs
upex -h auto.js app.ts
upex -h auto.sh app.sh
```

<details>
<summary><strong>🥷 Hidden mode rules</strong></summary>

<br>

- `--hidden` is supported **only** with an auto launcher.
- `upex -h app.py` is invalid; it shows usage.
- `--hidden` is appended **after** the app filename.
- No `--` separator is added for Python, JavaScript, or shell auto launchers.
- For C#, the command remains `dotnet run auto.cs -- app.cs --hidden`.
- If the auto launcher is missing, hidden mode is ignored and the app runs normally.

</details>

---

## 📦 Upload Behavior

| Behavior | Detail |
|---|---|
| Upload method | Existing streamed transfer protocol; chunked, not a single base64 message |
| Upload order | App first, then auto launcher |
| Upload timing | Synchronous for `upex`; the prompt waits for the upload |
| Destination | The client's current working directory |
| Filename | Sanitized basename from the local file |
| Spaces / Unicode | Supported; controller paths with spaces should be quoted |
| Same-name collision | App and auto launcher with the same basename are rejected |
| Large files | Streamed in chunks; large apps are supported |
| Checksums | The existing transfer protocol verifies file integrity |

> 🧹 The controller does not create a temporary remote folder. Files are saved directly in the client's current directory, just like a normal `upload`.

---

## 🎛️ Controls and Lifecycle

| Action | During upload | During execution |
|---|---|---|
| **Ctrl+Z** | Cancels the upload and prevents execution | Stops the running app |
| **Ctrl+B** | Not available; upload is synchronous | Moves execution to the background |
| **Ctrl+C** | Cancels the upload (same effect as Ctrl+Z) | Stops the running app |
| **`jobs`** | — | Shows backgrounded execution |
| **`stop`** | — | Stops a background job |
| **`exit session`** | — | Stops the client and its process |

<details>
<summary><strong>🔄 Reconnect and disconnect behavior</strong></summary>

<br>

- `upex` uses the same control connection as normal commands.
- If the controller disconnects while an app is running, the client stops the command, just like a normal foreground command.
- If the client reconnects, the app is **not** automatically re-run.
- The controller's `exit` command leaves clients running and reconnecting; it does not kill the connected client.
- `exit session <N>` stops that client for good.

</details>

---

## 🧾 Output and Exit Codes

- The client returns the combined output from compilation and execution.
- If the app exits normally, the client includes an `exit_code` when available.
- If the command was stopped or the controller disconnected, `exit_code` is omitted.
- Compiler failures include the compiler name and exit status, followed by the compiler output.
- If compilation succeeds, the produced binary is executed and its output is appended.

Example:

```text
controller> upex app.c
[+] Uploading app.c...
[+] Running app.c on the client...
[+] Received output: Hello from C
```

---

## 🛠️ Troubleshooting

| Message / symptom | Meaning | Fix |
|---|---|---|
| `Not a local file` | Controller path is wrong | Check the path; quote spaces |
| `TypeScript needs an auto launcher` | `.ts` was used with Rule 1 | Use `upex auto.js app.ts` or another auto launcher |
| `Auto mode supports only auto.py...` | Unsupported auto type | Use `auto.py`, `auto.cs`, `auto.js`, or `auto.sh` |
| `auto mode accepts app.py...` | App type is not allowed with auto mode | Use `.py`, `.js`, `.cs`, `.ts`, or `.sh` |
| `auto.py was not found...` | Auto file is missing | Add `auto/auto.py` or accept Rule 1 fallback |
| `Hidden mode needs an auto launcher` | `-h` used without auto | Add an auto launcher or remove `-h` |
| `The app and auto launcher cannot have the same name` | Basename collision | Rename one file |
| `The uploaded app and auto launcher collided` | Remote-name collision | Rename one file and retry |
| `Could not upload ... running the app normally` | Auto upload failed | Check connection/permissions; app still runs |
| `Upload failed; nothing was executed` | App upload failed | Check the client connection and write permission |
| `Python interpreter not found` | No `python3`/`python` | Install Python or fix `PATH` |
| `Node.js not found` | No `node`/`nodejs` | Install Node.js or fix `PATH` |
| `dotnet not found` | No .NET SDK | Install .NET or use another app type |
| `no C compiler found` | No `gcc`/`clang` | Install a C compiler |
| `no C++ compiler found` | No `g++`/`clang++` | Install a C++ compiler |
| `swift not found` | No Swift toolchain | Install Swift |
| `.bat files run on Windows only` | Wrong target OS | Run on Windows |
| `.exe files run on Windows only` | Wrong target OS | Run on Windows |
| `.elf` rejected | Wrong target OS | Run on Linux |
| `.macho files run on macOS only` | Wrong target OS | Run on macOS |
| `extensionless executable needs Linux/macOS` | Unknown extensionless file on Windows | Use an explicit `.exe` |
| App runs but no output | App may be a GUI app | Use Ctrl+B or inspect the app |

---

## 🔐 Safety Notes

- `upex` runs arbitrary code on the client. Only use it on machines you control or are authorized to test.
- Auto launchers may install persistence or change system state. Read them before using them.
- Uploaded files can overwrite same-named files in the client's current directory.
- Hidden mode is intended for authorized, stealthy testing; do not use it to evade consent.
- There is no sandbox around the uploaded app.
- Prefer a disposable VM or test machine for untrusted apps.

---

## ❓ FAQ

<details>
<summary><strong>Does upex work if the auto file is in the controller folder instead of auto/?</strong></summary>

<br>

Yes. The search order is:

1. `controller_dir/auto/auto.<type>`
2. `controller_dir/auto.<type>`

The `auto/` folder wins when both exist.

</details>

<details>
<summary><strong>Can I run a TypeScript file without an auto launcher?</strong></summary>

<br>

No. Rule 1 does not support `.ts`. Use an auto launcher:

```bash
upex auto.js app.ts
upex auto.cs app.ts
upex auto.py app.ts
upex auto.sh app.ts
```

</details>

<details>
<summary><strong>Does upex pass arguments to the app?</strong></summary>

<br>

Not in Rule 1. In Rule 2 and Rule 3, the **app filename** is passed as the first argument to the auto launcher, followed by `--hidden` in hidden mode.

</details>

<details>
<summary><strong>What happens if I press Ctrl+Z while the app is uploading?</strong></summary>

<br>

The upload is canceled and the app is **not** executed.

</details>

<details>
<summary><strong>What happens if I press Ctrl+B while the app is uploading?</strong></summary>

<br>

Ctrl+B is ignored during upload because the upload is synchronous. Ctrl+B works after execution starts.

</details>

<details>
<summary><strong>Does upex work with large apps?</strong></summary>

<br>

Yes. It uses the same streamed transfer protocol as normal uploads, not a single-message base64 transfer.

</details>

<details>
<summary><strong>Can the app and auto launcher have the same name?</strong></summary>

<br>

No. `upex` refuses to execute when their basenames collide.

</details>

<details>
<summary><strong>Does upex change the client's current directory?</strong></summary>

<br>

No. Files are uploaded to the current directory, and the app runs there. Use `cd` before `upex` if you want a different working directory.

</details>

<details>
<summary><strong>Does upex affect existing upload, download, shell, screenshot, or persistence features?</strong></summary>

<br>

No. It reuses the existing transfer and command infrastructure. Existing features continue to work normally.

</details>

---

## 🗺️ File Map

| File | Role in upex |
|---|---|
| `controller.py` | Parses `upex`, validates types, finds auto files, uploads, dispatches |
| `client.py` | Python client execution: detection, compile/run plans, hidden mode |
| `csharp/Upex.cs` | C# client execution: detection, compile/run plans, hidden mode |
| `csharp/Commands.cs` | Command execution with exit-code return |
| `csharp/Program.cs` | Dispatches the `upex` request |
| `csharp/assemble_client_cs.py` | Regenerates the single-file `client.cs` deliverable |
| `client.cs` | Generated C# deliverable; do not edit directly |
| `auto/auto.py` | Optional Python auto launcher |
| `auto/auto.cs` | Optional C# auto launcher |
| `auto/auto.js` | Optional JavaScript auto launcher |
| `auto/auto.sh` | Optional shell auto launcher |

---

## ✅ Platform Status

| Platform | Status |
|---|---|
| 🐧 Linux desktop | Verified |
| 🖥️ Linux server | Same Linux client path; validate on the target server |
| 🍎 macOS | Implemented; validate on the target machine |
| 🪟 Windows | Implemented; validate on the target machine |
| 🤖 Android | Support retained; not a priority |
| 🍏 iOS | Support retained; not a priority |

> 📝 The real `auto/` launchers were not executed during validation because they install persistence. Their command shapes and upload paths were tested with harmless temporary copies.

---

<p align="center">
  <strong>UPEX</strong> — upload once, run natively. 🚀
</p>

<p align="center">
  <sub>Last updated: October 2026 · UPEX feature guide</sub>
</p>
