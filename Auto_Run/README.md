# 🆕 Big upgrade — `auto.cs` & `auto.js`: one command, any language, zero setup

> **`auto.py` is no longer the only version.** The tool now ships in three flavors — **`auto.py`** (Python), **`auto.cs`** (C#/.NET), and **`auto.js`** (Node.js) — and the two new ones are **fully automatic**. You install *any* script with a single command, and they take care of everything else. No "which runtime?" question, no notifications, no admin or root password, ever.

```bash
auto.js  myprogram.py  --hidden      # or .js .ts .cs .sh — one command for all of them
auto.cs  myprogram.py  --hidden
```

**Note: auto.sh no longer need extra auto.js, it can generate auto.js**

That's it. The script now starts on every reboot, shutdown + power-on, and re-login, silently in the background.

### ✨ What makes them "just work"

- **Any language, same command.** The extension decides how it runs:
  | Your file | Runs with |
  |-----------|-----------|
  | `.py` `.pyw` | Python |
  | `.js` `.mjs` `.cjs` | Node.js |
  | `.ts` | Node.js (strips the TypeScript types itself — no `tsc`, no `ts-node`) |
  | `.cs` `.csx` | the .NET SDK |
  | `.sh` `.bash` | your system shell |
- **Missing runtime? It installs it for you — no password.** If the machine doesn't have the runtime a script needs, `auto.js`/`auto.cs` quietly download an **official, portable** copy into `~/.configs/runtime` (inside your own home folder) and use that. It never touches system folders, never runs `sudo`, never prompts, and never needs Administrator.
  - **Node** comes from `nodejs.org` (also used to run `.ts`).
  - **Python** comes from the `python-build-standalone` project.
  - **.NET** comes from Microsoft's official per-user `dotnet-install` script.
  - **Shell** uses the `bash`/`sh` already on every Unix system.
- **No pop-ups, no noise.** Hidden mode runs with no window and no console flash on every OS. The one-time banner some runtimes print on first run is suppressed.
- **Solves the chicken-and-egg for `.js`.** A `.js` file can't start if the machine has *no* JavaScript engine at all, so tiny starters are included — **`auto.sh`** (Linux/macOS), **`auto.command`** (double-click on macOS), and **`auto.cmd`**/**`auto.ps1`** (Windows). They fetch Node first (no admin), then hand off to `auto.js`. After that first run, everything is automatic.
- **Clean removal.** `--stop` kills the process and removes every autostart entry, launcher, and copy it made.

### ❓ Does it survive reboot, full shutdown + power-on, and re-login?

**Yes — all three, on every supported OS.** Each autostart entry fires at **login**, which happens after a reboot, after a cold power-on, and on a plain log-out/log-in alike.

| Event | Survives? | Why |
|-------|-----------|-----|
| **Reboot** | ✅ | Autostart entry re-fires at next login |
| **Shutdown → power on** | ✅ | Same login trigger on cold boot |
| **Re-login (log out / log in)** | ✅ | XDG autostart / LaunchAgent / Run key / Termux:Boot all run at login |

Mechanisms used, all per-user and **no root/admin**: Linux XDG autostart (desktop), Hyprland/Omarchy config, systemd **user** unit or cron `@reboot` (servers), macOS **LaunchAgent**, Windows **`HKCU\…\Run`** key + a WSH/JScript logon launcher, Android **Termux:Boot**. **iOS is unsupported by platform design** (Apple does not allow background user scripts to persist).

### ▶️ How to run each version

```bash
# Node version — the easy path. The starter grabs Node if it's missing:
./auto.sh   myprogram.cs  --hidden      # Linux / macOS
auto.cmd    myprogram.cs  --hidden      # Windows
#   (or, if Node is already installed:  node auto.js myprogram.cs --hidden)

# C# version — needs a .NET SDK to launch auto.cs itself:
dotnet run auto.cs -- myprogram.py --hidden      # .NET 10+ runs the file directly
#   (or build once on .NET 8+ and run the binary: auto myprogram.py --hidden)
```

> **One honest caveat:** something has to run the launcher itself. For `auto.js` that's Node — and the included `auto.sh`/`auto.cmd` starters fetch it for you with no admin, so this is handled. For `auto.cs` you need a .NET SDK present to start it; once it's running, it auto-installs every *other* runtime your scripts need.

---

<div align="center">

# 🚀 auto.py

**Install any Python script to start automatically — on every reboot, on every OS.**

Visible when you want to see it. Silent when you don't. One command to install, one command to remove.

[![Python](https://img.shields.io/badge/Python-3.8%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/downloads/)
[![Windows](https://img.shields.io/badge/Windows-10%20%7C%2011%20(no%20admin)-0078D4?style=for-the-badge&logo=windows&logoColor=white)](https://www.microsoft.com/windows)
[![Linux](https://img.shields.io/badge/Linux-desktop%20%7C%20server-FCC624?style=for-the-badge&logo=linux&logoColor=black)](https://www.kernel.org/)
[![macOS](https://img.shields.io/badge/macOS-Sequoia%20%7C%20Tahoe-000000?style=for-the-badge&logo=apple&logoColor=white)](https://www.apple.com/macos/)
[![Android](https://img.shields.io/badge/Android-9%E2%80%9316%20(Termux)-3DDC84?style=for-the-badge&logo=android&logoColor=white)](https://termux.dev/)
[![License](https://img.shields.io/badge/License-MIT-22c55e?style=for-the-badge)](./LICENSE)

[**Quick Start**](#-quick-start) · [**Features**](#-features) · [**Usage**](#-usage) · [**How It Works**](#-how-it-works) · [**FAQ**](#-faq)

</div>

---

> [!TIP]
> **TL;DR** — Run `python auto.py myprogram.py` once. Your script now starts on every reboot, visibly. Add `--hide` to run it silently, or `--hidden` to run it silently **and** hide its install folder. Add `--stop` to remove it forever.

> [!NOTE]
> **Windows users — no Administrator needed.** `auto.py` registers a per-user startup entry (the `HKCU\…\CurrentVersion\Run` key), so a normal Command Prompt runs every feature. It survives reboot, shutdown, and re-login. No `schtasks`, no elevation, no VBScript.

> [!NOTE]
> **Linux users — no `sudo` or root needed.** Everything runs at the user level via XDG Autostart.

> [!NOTE]
>**Linux server (headless) — no `sudo` for install.** The program runs as a systemd **user** service (background only). For boot persistence without a login, `auto.py` runs `loginctl enable-linger` for you (systemd's default policy allows self-linger with no password); if site policy forbids it, it falls back automatically to a per-user `cron @reboot` entry — still no root. On Ubuntu 24.04 + systemd 255 it prints the known-regression check (`loginctl show-user "$USER" -p Linger`) and the documented workaround.

> [!NOTE]
> **macOS users — no `sudo`, root, or password needed.** LaunchAgents run at the user level in `~/Library/LaunchAgents/` and start at every login (`RunAtLoad`). Running `auto.py` with `sudo` would load the job into the `system` domain instead of your GUI session, where it misbehaves — so don't. Visible mode opens Terminal with `open -a Terminal` (a `.command` launcher), which needs **no** Automation permission, so there's no login-time prompt it can't answer. macOS may show a one-time "Background Items Added" notice — keep the item **on** under **System Settings → General → Login Items**.

> [!NOTE]
> **iOS users — unsupported by platform design.** `auto.py` detects iOS (`$HOME` starts with `/private/var/mobile`) and exits with a full explanation; it never writes a plist, never touches `~/Library/LaunchAgents`, and never calls `launchctl`. No elevation, entitlement, or provisioning profile can make a user script run persistently in the background on iOS.

> [!NOTE]
> **Android 9–16 users — Termux + Termux:Boot from F-Droid, plus two settings.** Install **Termux** and **Termux:Boot** from **F-Droid only** (mixing F-Droid and Play Store builds breaks signatures and boot execution), open Termux:Boot **once** so Android registers it, and disable **battery optimisation for both apps**. Visible mode uses Termux's `RUN_COMMAND` API (needs `allow-external-apps=true` in `~/.termux/termux.properties`) and falls back to a foreground service, then a background run, so it still starts. On **Android 14+** also enable **Settings → System → Developer options → Disable child process restrictions**; on **Android 12–13** use the `adb` commands from the Termux wiki if the process dies with signal 9. On **Android 15 and newer** the OS may still terminate the process despite all of the above (Termux issue #5150) — the Developer-options toggle is the only mitigation where it exists. `auto.py` prints this warning only on Android 15+ (API 35+).

> **Tested on: Windows 10/11 (no admin), Arch, Kali, Fedora, Linux Mint, Omarchy/Hyprland, Ubuntu & RHEL servers, macOS, Android/Termux**

> **Linux Mint reboot issue — fixed.** Mint's visible-terminal launch and the autostart entry now survive reboot.


---

## 📑 Table of Contents

<details open>
<summary><b>Click to expand / collapse</b></summary>

- [Why auto.py?](#-why-autopy)
- [Quick Start](#-quick-start)
- [Features](#-features)
- [Supported Operating Systems](#-supported-operating-systems)
- [Installation](#-installation)
- [Usage](#-usage)
  - [Visible mode](#-visible-mode-default)
  - [Hidden mode](#-hidden-mode---hide)
  - [Hidden + hidden folder](#-hidden--hidden-folder---hidden)
  - [Stop / Uninstall](#-stop--uninstall---stop)
- [Behavior Matrix](#-behavior-matrix)
- [How It Works](#-how-it-works)
  - [Windows internals](#windows)
  - [Linux internals](#linux)
- [File Layout](#-file-layout)
- [Troubleshooting](#-troubleshooting)
- [FAQ](#-faq)
- [Prompt for Future Updates](#-prompt-for-future-updates)
- [License](#-license)

</details>

---

## 🤔 Why auto.py?

You wrote a Python script — a bot, a watcher, a server, a monitor — and you want it to **start on every boot**, across **Linux desktops and servers, Windows, macOS, and Android**, with **one command**, without touching `systemd`, the registry, LaunchAgents, or `.desktop` files by hand — and **without admin, root, sudo, or a password**.

`auto.py` does exactly that:

```bash
python auto.py myprogram.py            # visible, survives reboot
python auto.py myprogram.py --hide     # silent, survives reboot
python auto.py myprogram.py --hidden   # silent + hides the install folder, survives reboot
python auto.py myprogram.py --stop     # remove permanently
```

**One command. Five platforms. Zero elevation, zero config files to edit.**

---

## 🤖 Prompt for Future Updates

OSes evolve. When Windows 12 ships, or when Fedora changes its default terminal again, paste the block below into any AI agent along with this file.

<details>
<summary><b>📋 Click to expand the full prompt</b></summary>

```text
You are a senior cross-platform systems engineer. Update the provided `auto.py` script so it remains fully compatible with the latest documented behavior of ALL platforms listed below. Treat this prompt as the single source of truth for the script’s contract.

## Supported platforms (must all be preserved)

**Windows:** Windows 10 (all supported builds), Windows 11 (23H2, 24H2, 25H2). Windows 12 has not shipped as of October 2026 — Microsoft has made no announcement and explicitly denied plans for a 2026 release. Add Windows 12 / latest version code paths only if windows 12  or more latest version officially released ; also auto.py should be in a way that it doesn't require cmd in administrative mode to run it. I mean cmd in normal mode can use auto.py and all its feature . It should survive reboot or power off and on . and relogin. 

**macOS:** macOS Sequoia (15), macOS Tahoe (26). The script uses LaunchAgents (`~/Library/LaunchAgents/com.visiblebackground.*.plist`) with `RunAtLoad=true` for login persistence. macOS 26 Tahoe has known `launchctl bootstrap` reliability issues; the fallback to legacy `launchctl load -w` must be preserved.

**iOS:** Unsupported by platform design — see the dedicated iOS-specific rules below. Do not attempt to add iOS support.

**Android (9 through 15+):** Supported only via Termux and Termux:Boot (installed from F-Droid, not Google Play). The script writes boot scripts to `~/.termux/boot/`. Critical known issue: On Android 15, the OS aggressively kills Termux background processes even with `termux-wake-lock` active and battery optimization disabled — this is a confirmed upstream bug tracked in Termux issue #5150. The script must preserve `termux-wake-lock` and print a prominent warning that Android 15 may still terminate the process, advising the user to enable “Disable child process restrictions” in Developer Options if available. Do not promise reliability that the platform does not provide.

**Linux desktop (all distributions using XDG Autostart):** Arch Linux, Omarchy Linux, Manjaro, Ubuntu Desktop (LTS + interim), Kali Linux, Linux Mint (Cinnamon, XFCE, MATE), Fedora Workstation, Pop!_OS (GNOME + COSMIC), Zorin OS, and any major Debian/Ubuntu/Fedora/Arch derivative. The script writes `~/.config/autostart/<name>.desktop`. No sudo or root is ever required on Linux.

**Linux server (headless — Ubuntu Server and Red Hat Enterprise Linux Server):** Ubuntu Server 22.04 LTS, 24.04 LTS, 25.04, and later; RHEL 9 and RHEL 10 (and any rebuild such as Rocky Linux / AlmaLinux that tracks the same systemd behaviour). These platforms have **no graphical session** (`DISPLAY` and `WAYLAND_DISPLAY` are both unset), so XDG Autostart never runs. The script must detect this condition and install a **systemd user unit** at `~/.config/systemd/user/<name>.service` instead. No root is required for the unit itself; the only potentially privileged step is `loginctl enable-linger`, which the script attempts without sudo first and then via non-interactive sudo, falling back to a printed instruction. See the dedicated “Linux server rules” section below.

### Terminal-emulator detection order (must be preserved — desktop Linux only)
`xdg-terminal-exec` → `x-terminal-emulator` → `ptyxis` → `kgx` → `gnome-terminal` → `konsole` → `xfce4-terminal` → `mate-terminal` → `cosmic-term` → `ghostty` → `foot` → `xterm`

If none is found, launch detached and print a warning — do not fail silently.

Current documented defaults (for verification):
- Fedora Workstation 41+ uses `ptyxis` as the default terminal. Ubuntu 25.10+ also defaults to `ptyxis`.
- Linux Mint Cinnamon uses `gnome-terminal`; Linux Mint MATE uses `mate-terminal`.
- Pop!_OS COSMIC uses `cosmic-term`.

## What the script must do (unchanged CLI contract)

1. `python auto.py myprogram.py` → copies `myprogram.py` into `~/.configs/`, installs an autostart entry that runs the copy visibly on every reboot/login.
2. `python auto.py myprogram.py --hide` → same copy, but runs silently in the background on every reboot/login.
3. `python auto.py myprogram.py --stop` → kills the running copy, removes the autostart entry, and deletes the copy permanently.
4. Manual kill (Task Manager / kill / Activity Monitor) must only stop the current session; the autostart entry must survive so the program returns on next login.

## Windows-specific rules (must be preserved exactly)

- Admin elevation required. `schtasks /Create` and `schtasks /Delete` must be run from an elevated Command Prompt. Detect non-elevated execution via `ctypes.windll.shell32.IsUserAnAdmin()` and print a clear instruction before attempting `schtasks`.
- `.py` input without `--hide` → saved as `.py`, run with `python.exe` (visible console), task created with `/SC ONLOGON /IT`.
- `.pyw` input OR `--hide` → saved as `.pyw`, run with `pythonw.exe`, no console window, task created with `/SC ONLOGON /IT`.
- Hidden subprocess flags: `CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW | CREATE_BREAKAWAY_FROM_JOB`. Must NOT include `DETACHED_PROCESS` — MSDN documents that `CREATE_NO_WINDOW` is ignored when combined with `DETACHED_PROCESS`.
- For Windows 11 build ≥ 26100 (24H2 = 26100, 25H2 = 26200), prepend `timeout /t 5 /nobreak >nul &&` to the task action (not inside the VBS launcher) for “Delayed Desktop Initialization.”
- `--stop` must work regardless of whether the original argument was `.py` or `.pyw`; use the sanitized base name to locate the copy.
- Also auto.py should be in a way that it doesn't require cmd in administrative mode to run it. I mean cmd in normal mode can use auto.py and all its feature . It should survive reboot or power off and on . and relogin. 

## macOS-specific rules

- Write `~/Library/LaunchAgents/com.visiblebackground.<name>.plist` with `RunAtLoad=true`.
- Load via `launchctl bootstrap gui/$(id -u) <plist>` first. If it fails (macOS 26 Tahoe is known to return EIO/exit 5 on valid plists), fall back to `launchctl load -w <plist>`. On `--stop`, try `launchctl bootout gui/$(id -u)/<label>` first, then `launchctl unload -w`.
- Visible mode: open Terminal via `osascript -e 'tell application "Terminal" to do script "..."'`.
- Hidden mode: run Python directly with `stdout=DEVNULL`, `stderr=DEVNULL`, `start_new_session=True`.
- Also auto.py should be in a way that it doesn't require password to run it. I mean without password, can use auto.py and all its feature . It should survive reboot or power off and on . and relogin. 


## iOS-specific rules

- iOS is unsupported. The script must detect iOS and exit with a clear, documented explanation instead of attempting an install that the OS will silently break.
- Detection: `platform.system() == "darwin"` and `$HOME` starts with `/private/var/mobile`.
- The explanation must state the following facts, each backed by Apple’s own documentation:
  - iOS suspends third-party apps shortly after they move to the background. This suspension prevents the process from running any code.
  - There is no general-purpose mechanism for running code continuously, at a specific time, at a guaranteed interval, or in response to a network/IPC request in the background.
  - `BGAppRefreshTask` is opportunistic and time-bounded to roughly 30 seconds. `BGProcessingTask` is also opportunistic and typically runs only when the device is idle and plugged in, for about 60 seconds (sometimes a few minutes). Neither is a substitute for a persistent autostart daemon.
  - Scheduling is decided by `dasd` (Duet Activity Scheduler Daemon), not by the app. Battery level below ~20%, Low Power Mode, thermal pressure, and low user engagement can all cause a submitted task to be dropped entirely.
  - Once the user force-quits an app, iOS will not restart it in the background. No third-party library can bypass this.
  - A Jailbroken device or an MDM-supervised device does not change this contract for ordinary user scripts; do not suggest jailbreak or MDM workarounds as part of the script.
- The script must not write any files to `~/Library/LaunchAgents` on iOS, must not call `launchctl`, and must not create any plist.
- The same `ios_unsupported()` guard must run at the top of both `install()` and `uninstall()` so that neither `--hide` nor `--stop` can ever reach platform-specific code on iOS.
- If the user asks for a best-effort alternative, the script may print (but must not implement) that the only Apple-sanctioned path is a Shortcuts automation triggered by “Device Unlocked” or “Wi-Fi connected” after boot. This is a user-configured workaround, not something a Python script can install. The script must not pretend it has configured this for the user.
- Also auto.py should be in a way that it doesn't require password to run it. I mean without password, can use auto.py and all its feature . It should survive reboot or power off and on . and relogin. 

## Android / Termux rules

- Require Termux (F-Droid build) and the Termux:Boot add-on. Refuse to install if `$PREFIX` does not start with `/data/data/com.termux`.
- Write boot scripts to `~/.termux/boot/start-<name>.sh` with `termux-wake-lock` as the first command after the shebang.
- Visible mode: use `am start --user 0 -n com.termux/.app.TermuxActivity -a android.intent.action.RUN` with the `RUN_COMMAND_PATH` / `RUN_COMMAND_ARGUMENTS` extras so a terminal window appears.
- Hidden mode: background the Python process with output redirected to `/dev/null`.
- Print a warning that Android 15 may still kill the process despite the wake-lock (upstream issue #5150) and instruct the user to disable child-process restrictions if the option exists.
- Also auto.py should be in a way that it doesn't require password to run it. I mean without password, can use auto.py and all its feature . It should survive reboot or power off and on . and relogin. 

## Linux desktop rules (XDG Autostart)

- No sudo / root. Ever.
- Write `~/.config/autostart/<name>.desktop`.
- Always wrap `Exec=` in `sh -c 'cd "<copy_dir>" && …'` because XFCE and KDE Plasma ignore the `Path=` key.
- Exec-key quoting must follow the Desktop Entry Specification: escape `\`, `"`, `` ` ``, `$`, and literal `%` (as `%%`) inside double quotes.
- Hidden mode: `subprocess.Popen` with `stdout=DEVNULL`, `stderr=DEVNULL`, `start_new_session=True`, `cwd=copy_dir`.
- Visible mode: explicitly launch a terminal emulator from the detection list. Do not rely on `Terminal=true` alone — many desktop environments ignore it.
- If no terminal is found, fall back to detached launch and print a warning.
- Also auto.py should be in a way that it doesn't require password to run it. I mean without password, can use auto.py and all its feature . It should survive reboot or power off and on . and relogin. 

## Linux server rules (Ubuntu Server, RHEL Server, and any headless systemd host)

- **Detection.** Treat the host as a server when `platform.system() == "linux"` and neither `DISPLAY` nor `WAYLAND_DISPLAY` is set. XDG Autostart requires a graphical session and is silently ignored on a server; do not write a `.desktop` file as the primary mechanism.
- **Mechanism.** Write a systemd **user** unit to `~/.config/systemd/user/<name>.service`. Use `WantedBy=default.target` in the `[Install]` section — `multi-user.target` does not exist in the user instance.
- **Boot persistence.** A systemd user unit starts at login by default. To start at boot without a login, lingering must be enabled with `loginctl enable-linger <username>`. The script must attempt, in this order:
  1. `loginctl enable-linger` (self-linger; supported on systemd ≥ 232, which covers Ubuntu 22.04+ and RHEL 9+).
  2. `sudo -n loginctl enable-linger <username>` (non-interactive sudo; do not prompt).
  3. If both fail, print the exact command `sudo loginctl enable-linger <username>` and explain that without it the service starts on first login, not at boot.
- **Environment.** `systemctl --user` requires `XDG_RUNTIME_DIR`. When the variable is missing (common in SSH sessions), set `XDG_RUNTIME_DIR=/run/user/<uid>` for every `systemctl --user` and `loginctl` invocation.
- **Command.** Run `systemctl --user daemon-reload`, then `systemctl --user enable --now <name>.service`. If `enable --now` fails, fall back to separate `enable` and `start` calls and report the error.
- **Visible mode is impossible.** A headless server has no terminal emulator. If the user did not pass `--hide`, print a clear warning that visible mode is unavailable on a headless host and install in background mode (`hidden=True`).
- **`--stop`.** Run `systemctl --user stop <name>.service`, `systemctl --user disable <name>.service`, remove the unit file, and run `systemctl --user daemon-reload`. Also remove any XDG `.desktop` entry left from a previous desktop install, and kill any remaining PIDs.
- **Ubuntu 24.04 / systemd 255 caveat.** systemd 255.4-1ubuntu8.x has a known regression where logind’s garbage collector kills `user@<uid>.service` on the last session logout even when `Linger=yes`. This is upstream bug systemd/systemd#43361 and Launchpad bug #2163239, fixed in systemd v256.3 (PR #33786) but not backported to v255. The script must **not** silently promise boot persistence on this exact combination. If `systemctl --version` reports 255 and `/etc/os-release` identifies Ubuntu 24.04, print a warning that the user should verify with `loginctl show-user "$USER" -p Linger` and, if the service disappears after logout, apply the documented workaround in `/etc/systemd/logind.conf`: `UserStopDelaySec=infinity` and `KillUserProcesses=no`, then `systemctl restart systemd-logind`. Do not edit `/etc/systemd/logind.conf` automatically — that requires root and changes global login behaviour.
- **Unit file quoting.** Use double quotes and escape `\` and `"`; write literal `%` as `%%` to avoid systemd specifier expansion. Do not use `shlex.quote` for `ExecStart=` — systemd does not understand shell single quotes.
- **Journal.** Direct `StandardOutput=null` and `StandardError=null` in the unit so the user service does not rely on a journal that may not be readable from an SSH session; optionally mention `journalctl --user -u <name>.service` in the success message.
- Also auto.py should be in a way that it doesn't require password to run it. I mean without password, can use auto.py and all its feature . It should survive reboot or power off and on . and relogin. 

## Mandatory verification steps for every update

Before changing any code, the agent must consult the latest official sources:

1. Microsoft Learn: `schtasks` syntax, `/IT` flag, elevation requirements, session-0 isolation, Windows 11 25H2 desktop-init behavior.
2. Apple Developer Documentation: `launchd.plist` man page, `launchctl bootstrap` / `bootout` domain targets, macOS 26 Tahoe release notes.
3. Apple Developer Forums: iOS Background Execution Limits, `BGTaskScheduler` time budgets, `dasd` scheduling behavior, force-quit restrictions.
4. Termux Wiki: Termux:Boot setup, `~/.termux/boot/` script requirements, Android 15 background restrictions.
5. freedesktop.org: XDG Autostart Specification (current version), Desktop Entry Specification (Exec key quoting rules), `systemd.service(5)` and `loginctl(1)` man pages (`enable-linger`, user manager lifetime).
6. XFCE Bugzilla / KDE Bugzilla: status of `Path=` key support in autostart entries.
7. Distribution release notes: default terminal emulator changes for Fedora (Ptyxis), Ubuntu (Ptyxis 25.10+), Linux Mint (gnome-terminal / mate-terminal), Pop!_OS COSMIC.
8. Python `subprocess` docs: current `creationflags` constants on Windows.
9. **Red Hat documentation:** RHEL 9 and RHEL 10 “Enabling systemd services” — the official statement that `loginctl enable-linger <username>` is required for user services to start at boot and persist after logout (docs.redhat.com, “Building, running, and managing containers” § 13.2 / 14.2).
10. **Ubuntu Server documentation:** Ubuntu Server Guide and systemd 255 release notes; Launchpad bug #2163239 and systemd/systemd issue #43361 (logind GC kills `user@.service` on last logout despite `Linger=yes` in systemd 255.4-1ubuntu8.x; fixed in v256.3, not backported to v255). Confirm the current status before shipping the Ubuntu 24.04 warning.
11. **systemd upstream:** `loginctl(1)` (“If enabled for a specific user, a user manager is spawned for the user at boot and kept around after logouts”), `systemd-user-sessions.service`, `pam_systemd(8)` (`XDG_RUNTIME_DIR`), and `systemd.special(7)` (`default.target` for user instances).
12. **Headless SSH environment:** confirm that `XDG_RUNTIME_DIR` is not always set by `pam_systemd` in SSH sessions and that `systemctl --user` returns `Failed to connect to bus` without it; verify the `/run/user/<uid>` fallback.
13. **Also auto.py should be in a way that it doesn't require password to run it. I mean without password,or without administrative mode , it can use auto.py and all its feature . It should survive reboot or power off and on . and relogin.**

Compare each documented behavior against the current script. Fix every mismatch, deprecated flag, or newly documented bug. If a platform has no confirmed changes, state that explicitly instead of guessing.

## What the agent must return

1. The complete corrected `auto.py` script.
2. A table with one row per bug found, showing:
   - The bug (with exact location in the old script).
   - The fix applied.
   - The authoritative documentation that justifies the fix (URL or document title).
3. An explicit statement of which platforms had **no confirmed changes** in this update.

Do not add features that were not requested. Do not change the `.py` / `.pyw` naming rules on Windows or the `--hide` semantics. Do not introduce elevation requirements on Linux. Do not change --hidden feature rule and folder hidden mechanism . Re-check the script again and again, find bugs and fix them all. it should work very smoothly without causing any issues.on  every supported Linux distro, and  linux server, latest windows 10/ 11 or latest windows and latest mac os , latest ios and android 9 to 16 or latest available version .

```

**Then again give this prompt:**

```text
Re-check the script again & again, find bugs and fix them all. Always read the latest documentation and guidelines of every supported Linux distro, and linux server, latest windows 10/ 11 or latest windows and latest mac os , latest ios and android 9 to 15 or latest available version . So make sure that on every distro and operating system , it perfectly works, even after reboot and even after shutdown and start operating system again. Fix all the bugs and errors. Please re-check it perfectly again. Don't make any errors, it should work very smoothly without causing any issues. Also auto.py should be in a way that it doesn't require password to run it. I mean without password,or without administrative mode , it can use auto.py and all its feature . It should survive reboot or power off and on . and relogin. you can do online deep research for latest documentation for every supported Linux distro, and linux server, latest windows 10/ 11 or latest windows and latest mac os , latest ios and android 9 to 15 or latest available version . double check everything and make it perfect . 
```

</details>

---


## ⚡ Quick Start

> [!IMPORTANT]
> The **four commands are identical on every platform** — `install`, `--hide`, `--hidden`, `--stop`. Only *where you type them* and *which terminal you use* changes. No `sudo`, no Administrator, no password, anywhere.

**🪟 Windows 10 / 11 — normal Command Prompt or PowerShell (no admin):**

```cmd
cd C:\path\to\auto
python auto.py C:\path\to\myprogram.py            :: visible on every login
python auto.py C:\path\to\myprogram.py --hide     :: silent
python auto.py C:\path\to\myprogram.py --hidden   :: silent + hide the folder
python auto.py myprogram.py --stop                :: remove
```

**🐧 Linux desktop (Arch · Ubuntu · Mint · Fedora · Kali · Omarchy · Pop!_OS · Zorin) — normal terminal, no `sudo`:**

```bash
git clone https://github.com/boss-defender/Auto-Py.git && cd auto
python3 auto.py /path/to/myprogram.py             # visible on every login
python3 auto.py /path/to/myprogram.py --hide      # silent
python3 auto.py /path/to/myprogram.py --hidden    # silent + hide the folder
python3 auto.py myprogram.py --stop               # remove
```

**🖥️ Linux server (Ubuntu Server · RHEL · Rocky · Alma — headless), no `sudo`:**

```bash
# Same commands. Visible mode is impossible with no display, so it installs
# in the background automatically (as if --hide). It uses a systemd user unit,
# or a cron @reboot entry when linger isn't allowed.
python3 auto.py /path/to/myprogram.py             # -> background systemd/cron
python3 auto.py myprogram.py --stop
```

**🍎 macOS (Sequoia 15 · Tahoe 26) — Terminal, no `sudo`/password:**

```bash
python3 auto.py /path/to/myprogram.py             # visible: opens Terminal on login
python3 auto.py /path/to/myprogram.py --hide      # silent LaunchAgent
python3 auto.py /path/to/myprogram.py --hidden    # silent + hide the folder
python3 auto.py myprogram.py --stop               # remove
```

**🤖 Android 9–16 (inside Termux) — needs Termux + Termux:Boot from F-Droid, no root:**

```bash
pkg install python          # once, inside Termux
python auto.py ~/myprogram.py            # visible: opens a Termux window on boot
python auto.py ~/myprogram.py --hide     # silent background on boot
python auto.py ~/myprogram.py --hidden   # silent + hide the folder
python auto.py myprogram.py --stop       # remove
```

**📱 iOS / iPadOS — not supported by the platform:**

```bash
python3 auto.py myprogram.py
# -> auto.py detects iOS and exits with an explanation. It writes nothing and
#    changes nothing. Apple does not allow a user script to run persistently in
#    the background, so there is no install to run — on any command.
```

That's it. No `pip install`, no virtualenv, no compiled extensions.

---

## ✨ Features

<table>
<tr>
<td width="50%">

**🔁 Reboot persistence**
Auto-starts on every login/reboot until you `--stop` it.

**👁️ Visible or hidden**
Show a terminal window, or run silently with no UI.

**📦 Self-contained**
Copies your script to `~/.configs/` — keeps running even if the original is deleted.

**🙈 `--hidden` — hide the folder too**
Everything `--hide` does, plus hides the install folder: the **hidden attribute** on Windows, a **leading dot** on Linux/macOS/Android (renamed only if it isn't dotted already).

**You can change .configs to any folder name like .msdxs or visible-folder etc**

**🐍 `.py` / `.pyw` aware on Windows**
Automatically swaps to `pythonw.exe` + `.pyw` when `--hide`/`--hidden` is used.

</td>
<td width="50%">

**🛡️ Session-safe kill**
Killing via Task Manager / `kill` stops only the *current* session. Next reboot brings it back.

**🧹 One-shot uninstall**
`--stop` kills the process, removes the autostart entry, and deletes the copy.

**🔓 No admin · no root · no password**
Windows uses a per-user `Run` key; Linux/macOS/Android stay entirely at user level.

**🌐 Everywhere at user level**
Windows · Linux desktop (GNOME · KDE · XFCE · Cinnamon · MATE · Hyprland · COSMIC) · Linux server · macOS · Android/Termux.

**⚡ Modern terminal support**
`xdg-terminal-exec`, `ptyxis`, `kgx`, `ghostty`, `foot`, `gnome-terminal`, `konsole`, `xfce4-terminal`, `xterm`, `x-terminal-emulator`.

</td>
</tr>
</table>

---

## 💻 Supported Operating Systems

| OS | Version Tested | Autostart Mechanism | Elevation Required? | Notes |
|:---|:---|:---|:---:|:---|
| **Arch Linux** | Rolling | XDG Autostart | ❌ no | Any DE / WM |
| **Omarchy Linux** | Rolling (Hyprland) | Hyprland config (`autostart.lua` / `exec-once`) | ❌ no | Ghostty / Foot / Alacritty / Kitty |
| **Ubuntu** | 22.04 · 24.04 · 25.04 | XDG Autostart | ❌ no | GNOME |
| **Kali Linux** | Latest | XDG Autostart | ❌ no | XFCE / GNOME |
| **Linux Mint** | 21 · 22 | XDG Autostart | ❌ no | Cinnamon / XFCE / MATE |
| **Fedora Workstation** | 39 · 40 · 41 | XDG Autostart | ❌ no | GNOME / KDE / XFCE |
| **Pop!_OS** | 22.04 · 24.04 (COSMIC) | XDG Autostart | ❌ no | Auto-detects `ptyxis` / `kgx` |
| **Zorin OS** | 17 · 18 | XDG Autostart | ❌ no | GNOME-based |
| **Ubuntu Server** | 22.04 · 24.04 · 25.04+ | systemd user unit → cron `@reboot` | ❌ no | Headless; linger for boot start |
| **RHEL / Rocky / Alma** | 9 · 10 | systemd user unit → cron `@reboot` | ❌ no | Headless; linger for boot start |
| **Windows 10** | 22H2 | Per-user `Run` key | ❌ **no** | `HKCU\…\CurrentVersion\Run` |
| **Windows 11** | 23H2 · 24H2 · 25H2 | Per-user `Run` key | ❌ **no** | No admin, no `schtasks` |
| **macOS** | Sequoia (15) · Tahoe (26) | LaunchAgent (`RunAtLoad`) | ❌ no | `open -a Terminal` for visible mode |
| **Android** | 9–16 (Termux) | Termux:Boot script | ❌ no | F-Droid Termux + Termux:Boot |
| **iOS** | — | *unsupported by design* | — | Exits with a documented explanation |

---

## 📥 Installation

<details>
<summary><b>Prerequisites</b></summary>

| Requirement | Linux | Windows |
|:---|:---|:---|
| **Python 3.8+** | ✅ required | ✅ required |
| **`pythonw.exe`** | — | ✅ ships with the standard installer |
| **Desktop environment** | ✅ any XDG-compliant DE (servers run headless) | — (per-user `Run` key) |
| **Elevation** | ❌ **not needed** — user-level autostart | ❌ **not needed** — per-user `Run` key |

> [!NOTE]
> **No elevation anywhere.** Windows writes a per-user `Run` key (`HKCU`), which any normal account may set — no Administrator, no `schtasks`. Linux, macOS, and Android stay inside your own `$HOME`.

</details>

<details>
<summary><b>Steps — Linux</b></summary>

```bash
git clone https://github.com/<your-username>/auto.git
cd auto

# Verify Python
python3 --version
```

`auto.py` uses **only the Python standard library** — no third-party packages, no compilation, no virtualenv required.

</details>

<details>
<summary><b>Steps — Windows</b></summary>

1. Open a **normal** Command Prompt (no "Run as administrator" needed).
2. Clone and run:

```cmd
git clone https://github.com/<your-username>/auto.git
cd auto

python --version
```

</details>

---

## 🎯 Usage

> [!IMPORTANT]
> Run every command below from a **normal** terminal — no elevation, no `sudo`, no password, on any platform.

### Visible mode (default)

```bash
python auto.py myprogram.py
```

1. Copies `myprogram.py` → `~/.configs/myprogram.py`
2. Registers it to run **with a terminal window** on every login
3. Starts it immediately so you don't have to reboot

### Hidden mode (`--hide`)

```bash
python auto.py myprogram.py --hide
```

1. Copies `myprogram.py` → `~/.configs/myprogram.pyw` **(Windows only)**
2. Registers it to run **with no window** on every login
3. Starts it immediately in the background

### Hidden + hidden folder (`--hidden`)

```bash
python auto.py myprogram.py --hidden
```

Everything `--hide` does, and it also **hides the install folder**:

- **Windows** → sets the folder's **hidden attribute** (a leading dot means nothing to Explorer).
- **Linux · Linux server · macOS · Android** → a folder is hidden by a **leading dot**. If the name already starts with a dot (the default `~/.configs` does), nothing changes; if it doesn't, the folder is renamed to add the dot — done **before** anything is written in, so every autostart entry points at the final path.

### Stop / Uninstall (`--stop`)

```bash
python auto.py myprogram.py --stop
```

1. Kills the running copy
2. Deletes the autostart entry (per-user `Run` key, `.desktop`, systemd unit, cron line, LaunchAgent, or Termux boot script)
3. Removes the copy from `~/.configs/`

> [!IMPORTANT]
> After `--stop`, the program **will not come back** on reboot. On **Windows** this also needs no Administrator — removing a per-user `Run` key is a normal-user operation.

<details>
<summary><b>Handling <code>.pyw</code> files on Windows</b></summary>

```bash
python auto.py myprogram.pyw            # background, .pyw saved
python auto.py myprogram.pyw --hide     # same result (redundant)
python auto.py myprogram.pyw --stop     # remove by base name
```

The **base name** (`myprogram`) is what `--stop` uses to find the copy, so `--stop` works regardless of whether you originally passed `.py` or `.pyw`.

</details>

---

## 📊 Behavior Matrix

| Command | Saved as | Runs with | Window? | Survives reboot? | Elevation? |
|:---|:---|:---|:---:|:---:|:---:|
| `auto.py app.py` *(Linux)* | `app.py` | `python3` | ✅ terminal | ✅ yes | ❌ no |
| `auto.py app.py --hide` *(Linux)* | `app.py` | `python3` *(detached)* | ❌ none | ✅ yes | ❌ no |
| `auto.py app.py` *(Windows)* | `app.py` | `python.exe` | ✅ console | ✅ yes | ❌ **no** |
| `auto.py app.py --hide` *(Windows)* | `app.pyw` | `pythonw.exe` | ❌ none | ✅ yes | ❌ **no** |
| `auto.py app.py --hidden` | `app.pyw`* | `pythonw.exe`* | ❌ none | ✅ yes | ❌ no |
| `auto.py app.pyw` *(Windows)* | `app.pyw` | `pythonw.exe` | ❌ none | ✅ yes | ❌ **no** |
| `auto.py app.py --stop` | — | — | — | ❌ removed | ❌ no |

<sub>*`--hidden` also hides the install folder; the `.pyw`/`pythonw.exe` detail applies on Windows.</sub>

### Session kill vs. permanent stop

| Action | Effect | Elevation? |
|:---|:---|:---:|
| Task Manager → End task | ❌ stops current session only — **returns next login** | ❌ no |
| `kill <pid>` *(Linux)* | ❌ stops current session only — **returns next login** | ❌ no |
| `auto.py app.py --stop` | ✅ **permanent** — autostart entry deleted, will never return | ❌ no |

> [!NOTE]
> This is by design — it mirrors how real "always-on" services behave. Only an explicit `--stop` removes the autostart entry.

---

## ⚙️ How It Works

### Windows

Uses the **per-user `Run` registry key** — no Administrator, no `schtasks`, no VBScript:

```
HKCU\Software\Microsoft\Windows\CurrentVersion\Run
```

| Part | Purpose |
|:---|:---|
| `HKCU\…\Run` value | Fires at every user logon — the standard, documented per-user startup location |
| `pythonw.exe` + `.pyw` launcher | Starts the program with **no** console flash |
| `StartupApproved\Run` cleanup | Clears the "disabled" flag so a re-enabled entry actually runs |

> [!NOTE]
> **Why the change from Task Scheduler?** `schtasks /Create /SC ONLOGON` is refused from a non-elevated prompt (`ERROR: Access is denied`), which forced users into an Administrator window. The `Run` key is writable by any normal account, so `auto.py` now needs **no elevation at all**. If an old admin-only scheduled task is found, `auto.py` removes it (and tells you if deleting it needs one last elevated step).

<details>
<summary><b>How the entry is built</b></summary>

`auto.py` writes a tiny generated launcher — `~/.configs/_vb_launch_<name>.pyw` — and points the `Run` value at it:

```
"C:\...\pythonw.exe" "C:\Users\you\.configs\_vb_launch_<name>.pyw"
```

The launcher sets the working directory and starts your real program:

- **visible** → `python.exe` with `CREATE_NEW_CONSOLE`
- **hidden** (`--hide` / `--hidden`) → `pythonw.exe` with `CREATE_NO_WINDOW`

No VBScript (Microsoft is phasing VBScript out of Windows) and no `.cmd` wrapper (which flashes a console).

</details>

<details>
<summary><b>Immediate-start subprocess flags</b></summary>

For the start-now call, the launcher uses:

```python
CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW   # (+ CREATE_BREAKAWAY_FROM_JOB when allowed)
```

> [!WARNING]
> `DETACHED_PROCESS` is **intentionally omitted** — per MSDN, `CREATE_NO_WINDOW` is silently ignored when combined with `DETACHED_PROCESS`, which would let a console flash. `CREATE_BREAKAWAY_FROM_JOB` lets the program outlive the installer's terminal; if the host forbids breakaway (some IDEs/CI run inside a job object), `auto.py` retries without it instead of crashing.

</details>

<details>
<summary><b>Hiding the folder on Windows (<code>--hidden</code>)</b></summary>

`--hidden` sets `FILE_ATTRIBUTE_HIDDEN` on `~/.configs`, preserving every other attribute (it reads the current flags and ORs the hidden bit in — it never clobbers them). A leading dot means nothing to Explorer, so the attribute is the only thing that hides a folder on Windows.

</details>

---

### Linux

**Desktop** uses the **XDG Autostart specification** — a `.desktop` file at `~/.config/autostart/<name>.desktop`. Two cases are handled specially:

- **Hyprland / Omarchy** don't run XDG Autostart reliably, so `auto.py` writes a marked block to the compositor config instead (`~/.config/hypr/autostart.lua` via `o.launch_on_start`, or `exec-once` in the legacy `.conf`), and skips the `.desktop` to avoid a double start.
- **Headless servers** (no `DISPLAY`/`WAYLAND_DISPLAY`) have no graphical session, so XDG never runs — see the server mechanism below.

> [!TIP]
> **No `sudo`, no root, no password.** Everything is written inside the user's own `$HOME`, the spec-compliant location for per-user autostart.

<details>
<summary><b>Linux server (headless) — systemd user unit, with cron fallback</b></summary>

On a headless host `auto.py` installs a **systemd user unit** at `~/.config/systemd/user/<name>.service` and enables it with `systemctl --user`. To start at boot without a login it enables **linger** (`loginctl enable-linger`) — systemd's default polkit policy allows a user to self-linger with no password.

If the user bus is unreachable (e.g. `su` without a login session) or site policy forbids linger, it falls back to a per-user **`cron @reboot`** entry — still **no root**. On Ubuntu 24.04 + systemd 255 it prints the known logind-regression check and the documented workaround (it never edits `/etc/systemd/logind.conf` itself).

</details>

<details>
<summary><b>Example generated <code>.desktop</code> file</b></summary>

```ini
[Desktop Entry]
Type=Application
Name=myprogram
Exec=sh -c 'cd "/home/user/.configs" && "/usr/bin/python3" "/home/user/.configs/myprogram.py"'
Path=/home/user/.configs
Terminal=true
X-GNOME-Autostart-enabled=true
Hidden=false
```

</details>

<details>
<summary><b>Why the <code>sh -c 'cd … && …'</code> wrapper?</b></summary>

The spec allows a `Path=` key for the working directory, but **XFCE and KDE Plasma ignore it** (confirmed, long-standing bugs in both trackers). The shell wrapper guarantees the working directory is correct on every DE.

</details>

<details>
<summary><b>Terminal selection order (visible mode)</b></summary>

| # | Terminal | Used by |
|:---:|:---|:---|
| 1 | `xdg-terminal-exec` | Omarchy, Arch, Fedora 40+ |
| 2 | `x-terminal-emulator` | Debian alternatives — Ubuntu, Mint, Kali |
| 3 | `ptyxis` | Fedora 41+, Ubuntu 25.10+, GNOME 47+ |
| 4 | `kgx` | GNOME Console |
| 5 | `gnome-terminal` | Ubuntu, Fedora, Zorin, Mint Cinnamon |
| 6 | `konsole` | KDE Plasma |
| 7 | `xfce4-terminal` | XFCE |
| 8 | `mate-terminal` | Linux Mint MATE |
| 9 | `cosmic-term` | Pop!_OS COSMIC |
| 10 | `ghostty` | Omarchy fallback |
| 11 | `foot` | Wayland-native fallback |
| 12 | `xterm` | Last resort |

If none are found, the script launches detached and prints a warning — it never fails silently.

</details>

<details>
<summary><b>Hidden mode on Linux — no terminal needed</b></summary>

```python
subprocess.Popen(
    [python, script],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    start_new_session=True,
    cwd=copy_dir,
)
```

`start_new_session=True` detaches the process from the parent so killing the terminal does not kill the script.

</details>

---

## 🗑️ Uninstalling

```bash
python auto.py myprogram.py --stop
```

> [!IMPORTANT]
> Runs from a **normal** terminal on every platform — no `sudo`, no Administrator, no password.

> [!NOTE]
> This single command kills every running instance, deletes the autostart entry, removes the copy, and cleans up `state.json`.

<details>
<summary><b>Nuke everything auto.py ever created</b></summary>

**Linux** *(no `sudo`)*:

```bash
rm -rf ~/.configs
rm -f ~/.config/autostart/*.desktop            # inspect first!
rm -f ~/.config/systemd/user/*.service         # servers — inspect first!
```

**Windows (normal PowerShell — no Administrator):**

```powershell
Remove-Item -Recurse -Force "$env:USERPROFILE\.configs"
Get-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" |
  Select-Object -Property "VisibleBackground_*"   # inspect, then Remove-ItemProperty
```

</details>

---

## 📁 File Layout

After installing `myprogram.py`, your home directory contains:

```text
~/
├── .configs/
│   ├── myprogram.py        ← the copied script
│   ├── state.json          ← registry of installed scripts
│   └── ...                 ← other scripts you installed
└── .config/
    └── autostart/
        └── myprogram.desktop   ← Linux only
```

On Windows, the autostart entry lives in the registry — a per-user value named `VisibleBackground_<name>` under `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`, pointing at a generated `~/.configs/_vb_launch_<name>.pyw` launcher.

<details>
<summary><b>What does <code>state.json</code> look like?</b></summary>

```json
{
  "myprogram": {
    "source":    "/home/user/projects/myprogram.py",
    "copy":      "/home/user/.configs/myprogram.py",
    "hidden":    false,
    "platform":  "linux",
    "mechanism": "xdg"
  }
}
```

`mechanism` records exactly how it was installed — `xdg`, `hyprland`, `systemd`, `cron` (Linux), `run-key` (Windows), `launchagent` (macOS), or `termux-boot` (Android) — so `--stop` cleans up the right thing.

</details>

---

## 🩺 Troubleshooting

<details>
<summary><b>🪟 Windows issues</b></summary>

### Nothing appears after reboot / the program doesn't start

`auto.py` needs **no Administrator** — run it from a normal prompt. To check the entry:

- Confirm the `Run` value exists: `reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v VisibleBackground_myprogram`
- Confirm it's enabled in **Task Manager → Startup apps** (a disabled entry won't run; reinstalling re-enables it)
- Confirm `python.exe` / `pythonw.exe` are on `PATH` — run `where python` and `where pythonw`

### Hidden process shows a console flash at boot

- Make sure the saved file has the `.pyw` extension (use `--hide` or `--hidden`)
- Verify the `Run` value references `pythonw.exe`, not `python.exe`
- Don't add `DETACHED_PROCESS` to the launcher — it breaks `CREATE_NO_WINDOW`

### An old version left a scheduled task

Earlier releases used Task Scheduler. `auto.py` deletes that task automatically; if Windows says deleting it needs elevation, run once:
`schtasks /Delete /TN VisibleBackground_myprogram /F` (as Administrator).

</details>

<details>
<summary><b>🐧 Linux issues</b></summary>

> [!TIP]
> **Linux never needs `sudo` for `auto.py`.** If any guide or error message tells you to `sudo`, something is wrong — the tool only writes into `$HOME`.

### No terminal opens in visible mode

- Run `which xdg-terminal-exec ptyxis kgx gnome-terminal konsole xfce4-terminal` to see what's installed
- `auto.py` prints `[+] Started now in a visible terminal (X)` with the one it chose
- If nothing is printed, it fell back to detached mode

### `Path=` is ignored

- Expected on **XFCE and KDE Plasma**
- The `sh -c 'cd … && …'` wrapper compensates automatically — **no action needed**

### Autostart entry exists but doesn't run

- Check the `.desktop` file syntax: `desktop-file-validate ~/.config/autostart/myprogram.desktop`
- Ensure the file is executable: `chmod +x ~/.config/autostart/myprogram.desktop`
- Restart your session or run the DE's autostart refresh command (e.g., `systemctl --user restart plasma-plasmashell` on KDE)

### Killing the terminal kills the script

- This happens only if you launch the script **manually** from a terminal
- When launched by `auto.py` via `.desktop`, the process is owned by the DE and survives

</details>

---

## ❓ FAQ

<details>
<summary><b>Do I need to run <code>auto.py</code> as Administrator on Windows?</b></summary>

**No — not for any command** (`install`, `--hide`, `--hidden`, or `--stop`).

`auto.py` writes a per-user `Run` key under `HKCU`, which any normal account may set and remove. There's no `schtasks`, so there's nothing that needs elevation. It survives reboot, shutdown, and re-login.

</details>

<details>
<summary><b>Do I need <code>sudo</code>, root, or a password anywhere?</b></summary>

**No.** Windows uses a per-user `Run` key; Linux writes only inside `$HOME`; macOS uses a user LaunchAgent; Android uses Termux. On a Linux server the one potentially privileged step, `loginctl enable-linger`, is allowed for your own user without a password by systemd's default policy — and if a site blocks it, `auto.py` falls back to a user `cron @reboot` entry. Running with `sudo` would put files under `root`'s home and break your user's autostart.

</details>

<details>
<summary><b>What happens if I delete the original <code>myprogram.py</code>?</b></summary>

The copy in `~/.configs/` keeps running. `--stop` still works because it uses the copy.

</details>

<details>
<summary><b>Can I install multiple scripts?</b></summary>

Yes. Each script gets its own autostart entry (`VisibleBackground_<name>` `Run` value on Windows, `<name>.desktop` / unit / boot script elsewhere). `state.json` tracks all of them.

</details>

<details>
<summary><b>Can I reinstall the same script with different <code>--hide</code> / <code>--hidden</code> settings?</b></summary>

Yes. Re-running tears down the old entry and installs the new one, and it kills the already-running instance first so you never end up with two copies running.

</details>

<details>
<summary><b>Does <code>--stop</code> need the exact original filename?</b></summary>

No — only the **base name** matters. `--stop myprogram.py` and `--stop myprogram.pyw` do the same thing.

</details>

<details>
<summary><b>Can I run this on a server (no desktop)?</b></summary>

Yes. On a headless host `auto.py` installs a **systemd user unit** (falling back to `cron @reboot`) automatically — you don't configure `systemd` by hand. Visible mode isn't possible without a display, so it installs in background mode there.

</details>

<details>
<summary><b>Is it safe to edit the <code>.desktop</code> file manually?</b></summary>

Yes, but re-running `auto.py` will overwrite it.

</details>

<details>
<summary><b>Where does the "program" actually run from?</b></summary>

Always from `~/.configs/`, **never** from its original location. This means relative paths inside your script resolve relative to that folder.

</details>

<details>
<summary><b>What Python interpreter is used?</b></summary>

Whatever `sys.executable` points to when you run `auto.py`. If you use a virtualenv, the venv's Python is used. On Windows, the `.exe` is swapped between `python.exe` and `pythonw.exe` as needed.

</details>

---


## 📜 License

**MIT License** — free to use, modify, and redistribute. See [`LICENSE`](./LICENSE) for details.

---

## 🙏 Acknowledgements

- [freedesktop.org XDG Autostart Specification](https://specifications.freedesktop.org/autostart-spec/latest/)
- [Microsoft Learn — `schtasks` reference](https://learn.microsoft.com/windows-server/administration/windows-commands/schtasks)
- [Python docs — `subprocess` creationflags](https://docs.python.org/3/library/subprocess.html#windows-constants)

---

<div align="center">

**⭐ If this saved you time, consider giving the repo a star.**

<sub>Built for people who want their scripts to *Just Keep Running*.</sub>

</div>
