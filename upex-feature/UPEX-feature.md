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

> 🧰 `upex` assumes the required interpreters and compilers are already installed on the client. It does **not** probe `PATH`; it runs the exact command from the spec, and a missing tool produces that command's normal “not found” error.

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
4. Compile if needed. Try the next documented command only after the previous command actually fails.
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

> 📁 Paths may be relative to the controller's current directory or absolute. Quote a controller path that contains spaces; the uploaded basename itself must not contain spaces because the client runs the exact unquoted command.

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
| 🐧 ELF | `.elf` | Linux/Android |
| 🍏 Mach-O | `.macho` | macOS/iOS |
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

The controller rejects obviously incompatible combinations, such as `.bat` on Linux or `.elf` on Windows/macOS/iOS.

</details>

<details>
<summary><strong>🔁 Compiler and interpreter fallback</strong></summary>

<br>

`upex` does **not** check whether a command exists before running it. It runs
the command written in `New-Feature-Add.txt`; a fallback is attempted only after
that command actually fails.

### C and C++

| Platform | C order | C++ order |
|---|---|---|
| Linux | `gcc` | `g++` |
| macOS | `clang` → `gcc` | `clang++` |
| Windows | `cl` → `gcc` | `cl /EHsc` → `g++` |
| Android/Termux | `gcc` → `clang` | `g++` → `clang++` |
| iOS | `clang` → `gcc` | `clang++` |

- If the first command succeeds, the next one is **not** run.
- If the first command fails, the next documented command is tried.
- The produced binary is run only after a compile succeeds.
- If all documented compilers fail, the combined compiler output is returned.
- Android/Termux and iOS keep their existing clang fallbacks; they are not
  priority platforms, but they are not disabled.

### C#

`dotnet run --file app.cs` is tried first. If that command fails, `csc app.cs`
is tried and the resulting `app.exe` is run.

### Python and JavaScript

- Rule 1 Python: `python3 app.py` on Linux/macOS, `py app.py` on Windows.
- Auto Python: `python auto.py app.py` on Linux/macOS, `py auto.py app.py` on
  Windows.
- Rule 1 JavaScript: `node app.js`; auto JavaScript: `node auto.js app.js`.
- There is no interpreter-name probing and no `nodejs` substitution.

### Swift

- Try `swift app.swift` first.
- If that fails, compile with `swiftc app.swift -o app` and run the binary.

### Shell

- Unix: `chmod +x app.sh && ./app.sh`.
- Windows: try `bash app.sh`, then `sh app.sh`.

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
| `auto.py` | Windows: `py auto.py app.py`; Linux/macOS: `python auto.py app.py` |
| `auto.cs` | `dotnet run auto.cs -- app.py` |
| `auto.js` | `node auto.js app.py` |
| `auto.sh` | `bash auto.sh app.py` → `sh auto.sh app.py`; on Unix, then `chmod +x auto.sh && ./auto.sh app.py` |

The client tries the `auto.sh` commands in that order and stops at the first
success. It does not probe which shell is installed first.

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
| Spaces / Unicode | Unicode names are supported. App/auto filenames must not contain spaces because the client runs the exact unquoted command; rename the file first if its basename contains spaces. |
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
| `Not a local file` | Controller path is wrong | Check the path; quote controller paths with spaces |
| `TypeScript needs an auto launcher` | `.ts` was used with Rule 1 | Use `upex auto.js app.ts` or another auto launcher |
| `Auto mode supports only auto.py...` | Unsupported auto type | Use `auto.py`, `auto.cs`, `auto.js`, or `auto.sh` |
| `auto mode accepts app.py...` | App type is not allowed with auto mode | Use `.py`, `.js`, `.cs`, `.ts`, or `.sh` |
| `auto.py was not found...` | Auto file is missing | Add `auto/auto.py` or accept Rule 1 fallback |
| `Hidden mode needs an auto launcher` | `-h` used without auto | Add an auto launcher or remove `-h` |
| `The app and auto launcher cannot have the same name` | Basename collision | Rename one file |
| `The uploaded app and auto launcher collided` | Remote-name collision | Rename one file and retry |
| `Could not upload ... running the app normally` | Auto upload failed | Check connection/permissions; app still runs |
| `Upload failed; nothing was executed` | App upload failed | Check the client connection and write permission |
| `python: command not found` / `'python' is not recognized` | The literal `python` command is not on the client's `PATH` | Install Python or fix `PATH`; `upex` does not substitute another name |
| `python3: command not found` / `'python3' is not recognized` | Rule 1 Python command is missing | Install Python or fix `PATH` |
| `node: command not found` / `'node' is not recognized` | The literal `node` command is missing | Install Node.js or fix `PATH` |
| `dotnet: command not found` / `'dotnet' is not recognized` | `dotnet` failed; the client may try `csc` next | Install .NET or let the `csc` fallback run |
| `csc: command not found` / `'csc' is not recognized` | Both C# commands failed | Install the .NET SDK or the classic C# compiler |
| `gcc: command not found` / `g++: command not found` | The documented compiler is missing | Install the compiler for that platform |
| `clang: command not found` / `clang++: command not found` | macOS compiler is missing | Install the Xcode command-line tools |
| `swift: command not found` / `swiftc: command not found` | Swift toolchain is missing | Install Swift |
| `[upex] ... failed (exit N); trying the next option` | A documented fallback is being attempted | Check the next command's output; no action is needed unless all fail |
| `[upex] auto launcher exited with status N` | The auto launcher, or every documented `auto.sh` shell attempt, failed | Read the command output above the note |
| `.bat files run on Windows only` | Wrong target OS | Run on Windows |
| `.exe files run on Windows only` | Wrong target OS | Run on Windows |
| `.elf files run on Linux/Android only` | Wrong target OS | Run on Linux or Android |
| `.macho files run on macOS/iOS only` | Wrong target OS | Run on macOS or iOS |
| `extensionless executable needs Linux/macOS` | Unknown extensionless file on Windows | Use an explicit `.exe` |
| App runs but no output | App may be a GUI app | Use Ctrl+B or inspect the app |

> [!NOTE]
> Older clients could report `'"C:\WINDOWS\system32\python"' is not recognized`
> because they resolved `python` through `PATH` and then quoted an extensionless
> absolute path. Current clients never do that. On Windows, `auto.py` runs
> `py auto.py ...`; on Linux/macOS it runs `python auto.py ...`. No filename
> quotes are added.

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
