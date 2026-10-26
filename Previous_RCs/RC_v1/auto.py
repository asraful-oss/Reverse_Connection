import os
import sys
import time
import argparse
import subprocess
import signal
import platform
import json
import re
import shutil

# ------------------------------------------------------------------
# Paths  (normpath kills the mixed "C:\Users\Boss/..." separator)
# ------------------------------------------------------------------
BASE_DIR = os.path.normpath(os.path.expanduser("~/.configs"))
STATE_FILE = os.path.join(BASE_DIR, "state.json")
LINUX_AUTOSTART_DIR = os.path.expanduser("~/.config/autostart")


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
def ensure_directory():
    os.makedirs(BASE_DIR, exist_ok=True)
    if platform.system().lower() == "linux":
        os.makedirs(LINUX_AUTOSTART_DIR, exist_ok=True)


def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def sanitize_name(name):
    base = os.path.basename(str(name).replace("\\", "/"))
    for ext in (".pyw", ".py"):
        if base.lower().endswith(ext):
            base = base[: -len(ext)]
            break
    return re.sub(r"[^a-zA-Z0-9\-_]", "-", base).lower() or "program"


def _windows_build():
    if platform.system().lower() != "windows":
        return 0
    try:
        return int(platform.version().split(".")[-1])
    except Exception:
        return 0


# ------------------------------------------------------------------
# Windows helpers
# ------------------------------------------------------------------
def _windows_extension(target_script, hidden):
    name = os.path.basename(str(target_script).replace("\\", "/")).lower()
    if hidden or name.endswith(".pyw"):
        return ".pyw"
    return ".py"


def _python_executable(ext):
    py = sys.executable
    if ext == ".pyw":
        if py.lower().endswith("python.exe"):
            return py[:-4] + "w.exe"
        return py
    return py


# ------------------------------------------------------------------
# Linux — XDG Autostart
# ------------------------------------------------------------------
def linux_desktop_path(name):
    return os.path.join(LINUX_AUTOSTART_DIR, f"{name}.desktop")


def linux_create_autostart(copy_path, copy_dir, name, hidden):
    python_exe = sys.executable
    inner = f'cd "{copy_dir}" && "{python_exe}" "{copy_path}"'
    exec_line = f"sh -c '{inner}'"
    terminal = "false" if hidden else "true"

    content = f"""[Desktop Entry]
Type=Application
Name={name}
Exec={exec_line}
Path={copy_dir}
Terminal={terminal}
X-GNOME-Autostart-enabled=true
Hidden=false
"""
    path = linux_desktop_path(name)
    with open(path, "w") as f:
        f.write(content)
    os.chmod(path, 0o755)
    print(f"[+] Linux autostart entry: {path}  (Terminal={terminal})")


def linux_remove_autostart(name):
    path = linux_desktop_path(name)
    if os.path.exists(path):
        os.remove(path)
        print(f"[+] Removed Linux autostart entry: {path}")


def _find_terminal_cmd(python_exe, copy_path, copy_dir):
    inner = f'cd "{copy_dir}" && "{python_exe}" "{copy_path}"'
    candidates = [
        ["xdg-terminal-exec", "sh", "-c", inner],
        ["x-terminal-emulator", "-e", "sh", "-c", inner],
        ["ptyxis", "--", "sh", "-c", inner],
        ["kgx", "--", "sh", "-c", inner],
        ["gnome-terminal", "--", "sh", "-c", inner],
        ["konsole", "-e", "sh", "-c", inner],
        ["xfce4-terminal", "-e", f'sh -c "{inner}"'],
        ["ghostty", "-e", "sh", "-c", inner],
        ["foot", "-e", "sh", "-c", inner],
        ["xterm", "-e", "sh", "-c", inner],
    ]
    for cmd in candidates:
        if shutil.which(cmd[0]):
            return cmd
    return None


def linux_launch_now(copy_path, copy_dir, hidden):
    python_exe = sys.executable
    if hidden:
        subprocess.Popen(
            [python_exe, copy_path],
            cwd=copy_dir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        print("[+] Started now (hidden/background).")
    else:
        cmd = _find_terminal_cmd(python_exe, copy_path, copy_dir)
        if cmd:
            subprocess.Popen(cmd, start_new_session=True)
            print(f"[+] Started now in a visible terminal ({cmd[0]}).")
        else:
            subprocess.Popen(
                [python_exe, copy_path],
                cwd=copy_dir,
                start_new_session=True,
            )
            print("[!] No terminal emulator found — launched detached.")


def linux_find_pids(copy_path):
    pattern = f"python.*{re.escape(copy_path)}"
    try:
        res = subprocess.run(
            ["pgrep", "-f", pattern], capture_output=True, text=True
        )
        if res.returncode == 0:
            return [int(x) for x in res.stdout.split()]
    except FileNotFoundError:
        pass

    pids = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                raw = f.read().replace(b"\x00", b" ").decode(errors="ignore")
            if copy_path in raw and "python" in raw.lower():
                pids.append(int(pid))
        except Exception:
            continue
    return pids


def linux_kill(copy_path):
    for pid in linux_find_pids(copy_path):
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"[+] Killed PID {pid}")
        except ProcessLookupError:
            pass
        except Exception as e:
            print(f"[-] Could not kill PID {pid}: {e}")


# ------------------------------------------------------------------
# Windows — Task Scheduler (VBS launcher: no blank CMD window)
# ------------------------------------------------------------------
def windows_task_name(name):
    return f"VisibleBackground_{name}"


def windows_vbs_path(name):
    return os.path.normpath(os.path.join(BASE_DIR, f"VisibleBackground_{name}.vbs"))


def _write_windows_vbs_launcher(name, copy_path, copy_dir, ext, build):
    if ext == ".pyw":
        python_exe = _python_executable(".pyw")
        style = "0"          # hidden
    else:
        python_exe = _python_executable(".py")
        style = "1"          # normal window

    # Windows 11 25H2 (build >= 26100): give the shell time to settle.
    sleep_line = "WScript.Sleep 5000\n" if build >= 26100 else ""

    copy_path = os.path.normpath(copy_path)
    copy_dir = os.path.normpath(copy_dir)

    content = (
        f"{sleep_line}"
        f'Set WshShell = CreateObject("WScript.Shell")\n'
        f'WshShell.CurrentDirectory = "{copy_dir}"\n'
        f'WshShell.Run """{python_exe}"" ""{copy_path}""", {style}, False\n'
    )

    vbs_path = windows_vbs_path(name)
    with open(vbs_path, "w", encoding="utf-8") as f:
        f.write(content)
    return vbs_path


def windows_create_autostart(copy_path, copy_dir, name, hidden, ext):
    task_name = windows_task_name(name)
    build = _windows_build()

    vbs_path = _write_windows_vbs_launcher(
        name, copy_path, copy_dir, ext, build
    )

    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    wscript_exe = os.path.normpath(
        os.path.join(system_root, "System32", "wscript.exe")
    )
    action = f'"{wscript_exe}" //B //Nologo "{vbs_path}"'

    args = [
        "schtasks", "/Create", "/F",
        "/TN", task_name,
        "/TR", action,
        "/SC", "ONLOGON",
        "/IT",
        "/RL", "LIMITED",
    ]
    res = subprocess.run(args, capture_output=True, text=True)
    if res.returncode != 0:
        try:
            os.remove(vbs_path)
        except Exception:
            pass
        print(f"[-] schtasks failed: {res.stderr.strip() or res.stdout.strip()}")
        return False
    print(f"[+] Windows Task Scheduler entry created: {task_name}")
    print(f"[+] VBS launcher written: {vbs_path}")
    return True


def windows_remove_autostart(name):
    task_name = windows_task_name(name)
    res = subprocess.run(
        ["schtasks", "/Delete", "/TN", task_name, "/F"],
        capture_output=True,
    )
    if res.returncode == 0:
        print(f"[+] Removed Windows task: {task_name}")


def windows_launch_now(copy_path, copy_dir, name, ext):
    if ext == ".pyw":
        python_exe = _python_executable(".pyw")
        flags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
        )
        subprocess.Popen(
            [python_exe, copy_path],
            cwd=copy_dir,
            creationflags=flags,
            close_fds=True,
        )
        print("[+] Started now (hidden/background, .pyw).")
    else:
        python_exe = _python_executable(".py")
        subprocess.Popen(
            f'start "VisibleBackground-{name}" "{python_exe}" "{copy_path}"',
            shell=True,
            cwd=copy_dir,
        )
        print("[+] Started now (visible console, .py).")


def windows_find_pids(copy_path):
    needle = os.path.normpath(copy_path).lower().replace("'", "''")
    ps = (
        f"$needle = '{needle}'; "
        "Get-CimInstance Win32_Process | "
        "Where-Object { "
        "($_.Name -eq 'python.exe' -or $_.Name -eq 'pythonw.exe') "
        "-and $_.CommandLine -and "
        "$_.CommandLine.ToLower().Contains($needle) "
        "} | Select-Object -ExpandProperty ProcessId"
    )
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True,
        )
        return [int(x) for x in res.stdout.split() if x.strip().isdigit()]
    except Exception:
        return []


def windows_kill(copy_path):
    for pid in windows_find_pids(copy_path):
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       capture_output=True)
        print(f"[+] Killed PID {pid}")


def _windows_remove_vbs_launcher(name):
    vbs_path = windows_vbs_path(name)
    if os.path.exists(vbs_path):
        try:
            os.remove(vbs_path)
            print(f"[+] Removed VBS launcher: {vbs_path}")
        except Exception as e:
            print(f"[-] Could not remove VBS launcher: {e}")


# ------------------------------------------------------------------
# Stop helpers — work with or without state.json
# ------------------------------------------------------------------
def _resolve_copy_paths(target_script, name, state):
    paths = set()

    if name in state and "copy" in state[name]:
        paths.add(os.path.normpath(state[name]["copy"]))

    base = os.path.basename(str(target_script).replace("\\", "/"))
    if base:
        paths.add(os.path.normpath(os.path.join(BASE_DIR, base)))

    for ext in (".py", ".pyw"):
        paths.add(os.path.normpath(os.path.join(BASE_DIR, name + ext)))

    if os.path.isdir(BASE_DIR):
        for fname in os.listdir(BASE_DIR):
            if sanitize_name(fname) == name:
                paths.add(os.path.normpath(os.path.join(BASE_DIR, fname)))

    return paths


def _remove_with_retry(path, attempts=6, delay=0.5):
    for i in range(attempts):
        try:
            os.remove(path)
            print(f"[+] Removed copy: {path}")
            return True
        except FileNotFoundError:
            return True
        except PermissionError:
            if i < attempts - 1:
                time.sleep(delay)
        except Exception as e:
            print(f"[-] Could not remove copy {path}: {e}")
            return False
    print(f"[-] Could not remove copy {path} (still locked)")
    return False


# ------------------------------------------------------------------
# Install / Uninstall
# ------------------------------------------------------------------
def install(target_script, hidden):
    ensure_directory()
    if not os.path.isfile(target_script):
        print(f"[-] File not found: {target_script}")
        sys.exit(1)

    name = sanitize_name(target_script)
    system = platform.system().lower()

    if system == "windows":
        ext = _windows_extension(target_script, hidden)
        saved_name = os.path.basename(str(target_script).replace("\\", "/"))
        for e in (".pyw", ".py"):
            if saved_name.lower().endswith(e):
                saved_name = saved_name[: -len(e)]
                break
        saved_name += ext
        copy_path = os.path.normpath(os.path.join(BASE_DIR, saved_name))
    else:
        ext = None
        copy_path = os.path.normpath(
            os.path.join(
                BASE_DIR,
                os.path.basename(str(target_script).replace("\\", "/")),
            )
        )

    copy_dir = BASE_DIR

    state = load_state()
    if name in state:
        print(f"[*] '{name}' already installed — removing old instance first…")
        _uninstall_runtime(name, state[name], system)

    shutil.copy2(target_script, copy_path)
    print(f"[+] Copied '{target_script}' → '{copy_path}'")

    if system == "linux":
        linux_create_autostart(copy_path, copy_dir, name, hidden)
        linux_launch_now(copy_path, copy_dir, hidden)
    elif system == "windows":
        if not windows_create_autostart(copy_path, copy_dir, name, hidden, ext):
            print("[-] Could not create autostart entry.")
            sys.exit(1)
        windows_launch_now(copy_path, copy_dir, name, ext)
    else:
        print(f"[-] Unsupported OS: {system}")
        sys.exit(1)

    state[name] = {
        "source": os.path.abspath(target_script),
        "copy": copy_path,
        "hidden": hidden,
        "ext": ext,
    }
    save_state(state)

    if system == "windows":
        mode = "background (silent, .pyw)" if ext == ".pyw" else "foreground (visible, .py)"
    else:
        mode = "hidden (silent background)" if hidden else "visible (foreground)"
    print(f"[+] '{name}' installed — {mode}. It will auto-start on every reboot.")
    print(f"    To stop permanently: python {os.path.basename(__file__)} "
          f"{os.path.basename(copy_path)} --stop")


def _uninstall_runtime(name, info, system):
    copy_path = os.path.normpath(info["copy"])
    if system == "linux":
        linux_kill(copy_path)
        linux_remove_autostart(name)
    elif system == "windows":
        windows_kill(copy_path)
        windows_remove_autostart(name)
        _windows_remove_vbs_launcher(name)


def uninstall(target_script):
    ensure_directory()
    name = sanitize_name(target_script)
    system = platform.system().lower()
    state = load_state()

    copy_paths = _resolve_copy_paths(target_script, name, state)
    print(f"[*] Resolved {len(copy_paths)} candidate path(s) for '{name}'.")

    # 1. Kill every running process matching any candidate path.
    finder = windows_find_pids if system == "windows" else linux_find_pids
    all_pids = set()
    for cp in copy_paths:
        all_pids.update(finder(cp))

    if not all_pids:
        print("[*] No running process found to kill.")

    for pid in sorted(all_pids):
        if system == "windows":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True)
            print(f"[+] Killed PID {pid}")
        else:
            try:
                os.kill(pid, signal.SIGTERM)
                print(f"[+] Killed PID {pid}")
            except ProcessLookupError:
                pass
            except Exception as e:
                print(f"[-] Could not kill PID {pid}: {e}")

    # 2. Remove autostart entry unconditionally.
    if system == "linux":
        linux_remove_autostart(name)
    elif system == "windows":
        windows_remove_autostart(name)
        _windows_remove_vbs_launcher(name)

    # 3. Delete every matching copy on disk.
    for cp in copy_paths:
        if os.path.exists(cp):
            _remove_with_retry(cp)

    # 4. Clean state.
    if name in state:
        del state[name]
        save_state(state)

    print(f"[+] '{name}' stopped permanently. No autostart entry remains.")


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Install a Python script to run on every reboot — "
                    "visible by default, hidden with --hide."
    )
    parser.add_argument("script", help="Python script to schedule")
    parser.add_argument("--hide", action="store_true",
                        help="run silently in the background (no window)")
    parser.add_argument("--stop", action="store_true",
                        help="stop and permanently remove the autostart entry")
    args = parser.parse_args()

    if args.stop:
        uninstall(args.script)
    else:
        install(args.script, hidden=args.hide)


if __name__ == "__main__":
    main()
