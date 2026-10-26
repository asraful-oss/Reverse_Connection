#!/usr/bin/env python3
"""
autorunner.py - Cross-platform silent auto-start setup.

Supported: Omarchy, Arch, Ubuntu, Mint, Kali, Zorin, Fedora, RHEL,
           Pop!_OS, Nobara, Windows 10/11.
"""
import argparse
import os
import platform
import shutil
import subprocess
import sys
import tempfile


# ---------------------------------------------------------------- helpers

def run_cmd(cmd, check=True, capture=True):
    try:
        return subprocess.run(
            cmd,
            shell=isinstance(cmd, str),
            check=check,
            capture_output=capture,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"[-] Command failed: {cmd}")
        if e.stderr:
            print(f"    {e.stderr.strip()}")
        return None
    except FileNotFoundError:
        print(f"[-] Command not found: {cmd}")
        return None


def get_os_info():
    os_type = platform.system().lower()
    if os_type == "linux":
        try:
            with open("/etc/os-release") as f:
                info = dict(
                    line.strip().split("=", 1)
                    for line in f
                    if "=" in line
                )
            name = info.get("NAME", "Generic Linux").strip('"')
            return f"Linux ({name})"
        except OSError:
            return "Linux (Unknown Distro)"
    if os_type == "windows":
        return f"Windows {platform.release()}"
    return platform.system()


def is_omarchy():
    if platform.system().lower() != "linux":
        return False
    try:
        with open("/etc/os-release") as f:
            return "omarchy" in f.read().lower()
    except OSError:
        return False


def _pythonw_path():
    """Locate pythonw.exe (the GUI-subsystem Python interpreter)."""
    exe = sys.executable or ""
    if exe:
        d = os.path.dirname(exe)
        for name in ("pythonw.exe", "pythonw"):
            cand = os.path.join(d, name)
            if os.path.exists(cand):
                return cand
    for name in ("pythonw.exe", "pythonw"):
        p = shutil.which(name)
        if p:
            return p
    return None


# ---------------------------------------------------------------- Linux

def _is_systemd():
    if not shutil.which("systemctl"):
        return False
    r = run_cmd(["systemctl", "is-system-running"], check=False)
    if r is None:
        return False
    return r.returncode == 0 or "running" in r.stdout or "degraded" in r.stdout


def _linux_service_body(exec_cmd, work_dir, binary_name, silent, log_file,
                        user_line, omarchy):
    if log_file:
        out = f"append:{os.path.abspath(log_file)}"
    elif silent:
        out = "null"
    else:
        out = "journal"

    if omarchy:
        after, wanted = "graphical-session.target", "graphical-session.target"
    else:
        after, wanted = "network.target", "multi-user.target"

    return f"""[Unit]
Description=Auto-run for {binary_name}
After={after}

[Service]
Type=simple
{user_line}ExecStart={exec_cmd}
WorkingDirectory={work_dir}
Restart=always
RestartSec=3
StandardInput=null
StandardOutput={out}
StandardError={out}

[Install]
WantedBy={wanted}
"""


def setup_linux(target_path, binary_name, silent, log_file):
    if not _is_systemd():
        print("[-] systemd is not available/running on this system.")
        return False

    abs_path = os.path.abspath(target_path)
    if target_path.lower().endswith(".py"):
        py = (shutil.which("python3")
              or shutil.which("python")
              or "/usr/bin/python3")
        exec_cmd = f"{py} {abs_path}"
    else:
        os.chmod(abs_path, 0o755)
        exec_cmd = abs_path

    work_dir = os.path.dirname(abs_path)
    service_name = f"autorun-{binary_name}.service"
    omarchy = is_omarchy()

    # --- Omarchy: user-level unit, no sudo needed ---
    if omarchy:
        svc_dir = os.path.expanduser("~/.config/systemd/user")
        os.makedirs(svc_dir, exist_ok=True)
        svc_path = os.path.join(svc_dir, service_name)
        try:
            with open(svc_path, "w") as f:
                f.write(_linux_service_body(exec_cmd, work_dir, binary_name,
                                            silent, log_file, "", True))
        except OSError as e:
            print(f"[-] Cannot write service: {e}")
            return False

        run_cmd(["systemctl", "--user", "daemon-reload"], check=False)
        if run_cmd(["systemctl", "--user", "enable", "--now", service_name],
                   check=False) is None:
            return False
        print(f"[+] Omarchy user service: {svc_path}")
        print("[+] Starts with your graphical session, no terminal window.")
        return True

    # --- Standard Linux: system-level unit ---
    if os.geteuid() != 0:
        print("[-] Run as root (sudo) to install a system service.")
        return False

    run_user = os.environ.get("SUDO_USER", "root")
    user_line = "" if run_user == "root" else f"User={run_user}\n"
    if run_user == "root":
        print("[!] Service will run as root. Re-run from a normal user with sudo "
              "to run it as yourself.")

    svc_path = f"/etc/systemd/system/{service_name}"
    try:
        with open(svc_path, "w") as f:
            f.write(_linux_service_body(exec_cmd, work_dir, binary_name,
                                        silent, log_file, user_line, False))
    except OSError as e:
        print(f"[-] Cannot write service: {e}")
        return False

    if run_cmd(["systemctl", "daemon-reload"], check=False) is None:
        return False
    if run_cmd(["systemctl", "enable", "--now", service_name], check=False) is None:
        return False
    print(f"[+] systemd service: {svc_path}")
    print("[+] Runs at boot with no terminal window.")
    return True


# ---------------------------------------------------------------- Windows

def _make_vbs(executable, arg_path, binary_name):
    """Create a VBS launcher that starts a process with a hidden window."""
    base = os.path.join(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(),
                        "AutoRunner")
    os.makedirs(base, exist_ok=True)
    vbs_path = os.path.join(base, f"AutoRun_{binary_name}.vbs")

    inner = f'"{executable}"'
    if arg_path:
        inner += f' "{arg_path}"'
    escaped = inner.replace('"', '""')

    content = (
        'Set WshShell = CreateObject("WScript.Shell")\r\n'
        f'WshShell.Run "{escaped}", 0, False\r\n'
    )
    with open(vbs_path, "w", encoding="utf-8") as f:
        f.write(content)
    return vbs_path


def _windows_command(target_path, binary_name, silent):
    """Return (execute, arguments) for the Windows launcher."""
    abs_path = os.path.abspath(target_path)
    is_py = target_path.lower().endswith((".py", ".pyw"))

    if silent:
        if is_py:
            pyw = _pythonw_path()
            if pyw:
                return pyw, f'"{abs_path}"'
            py = shutil.which("python") or sys.executable or "python"
            vbs = _make_vbs(py, abs_path, binary_name)
            return "wscript.exe", f'"{vbs}"'
        vbs = _make_vbs(abs_path, None, binary_name)
        return "wscript.exe", f'"{vbs}"'

    if is_py:
        py = shutil.which("python") or sys.executable or "python"
        return py, f'"{abs_path}"'
    return abs_path, ""


def _register_task_powershell(task_name, execute, arguments):
    ps = "\n".join([
        "$ErrorActionPreference = 'Stop'",
        f"$exe = '{execute.replace(chr(39), chr(39) * 2)}'",
        f"$arg = '{arguments.replace(chr(39), chr(39) * 2)}'",
        ("$action = New-ScheduledTaskAction -Execute $exe -Argument $arg"
         if arguments else
         "$action = New-ScheduledTaskAction -Execute $exe"),
        "$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME",
        "$settings = New-ScheduledTaskSettingsSet -Hidden "
        "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        "-StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero)",
        "$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME "
        "-LogonType Interactive -RunLevel Limited",
        f"Register-ScheduledTask -TaskName '{task_name}' -Action $action "
        "-Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null",
    ])
    fd, path = tempfile.mkstemp(suffix=".ps1", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(ps)
        return run_cmd(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                        "-File", path], check=False)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _register_task_schtasks(task_name, execute, arguments):
    tr = f'"{execute}" {arguments}'.strip()
    return run_cmd(["schtasks", "/Create", "/TN", task_name, "/TR", tr,
                    "/SC", "ONLOGON", "/RL", "LIMITED", "/F"], check=False)


def _windows_registry_fallback(task_name, execute, arguments):
    try:
        import winreg
    except ImportError:
        print("[-] winreg unavailable.")
        return False

    cmd = f'"{execute}" {arguments}' if arguments else f'"{execute}"'
    run_key = r"Software\Microsoft\Windows\CurrentVersion\Run"
    approved_key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"

    try:
        k = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, run_key, 0,
                               winreg.KEY_SET_VALUE)
        winreg.SetValueEx(k, task_name, 0, winreg.REG_SZ, cmd)
        winreg.CloseKey(k)

        # Clear any "disabled" flag that Task Manager might have set
        try:
            ka = winreg.OpenKey(winreg.HKEY_CURRENT_USER, approved_key, 0,
                                winreg.KEY_SET_VALUE)
            winreg.SetValueEx(ka, task_name, 0, winreg.REG_BINARY,
                              bytes([0x02, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]))
            winreg.CloseKey(ka)
        except FileNotFoundError:
            pass

        print(f"[+] Registry fallback: HKCU\\{run_key}\\{task_name}")
        return True
    except OSError as e:
        print(f"[-] Registry error: {e}")
        return False


def setup_windows(target_path, binary_name, silent):
    task_name = f"AutoRun_{binary_name}"
    execute, arguments = _windows_command(target_path, binary_name, silent)

    r = _register_task_powershell(task_name, execute, arguments)
    if r is not None and r.returncode == 0:
        print(f"[+] Scheduled Task '{task_name}' created (hidden, at logon).")
    else:
        print("[!] PowerShell task creation failed; trying schtasks...")
        r2 = _register_task_schtasks(task_name, execute, arguments)
        if r2 is not None and r2.returncode == 0:
            print(f"[+] Scheduled Task '{task_name}' created via schtasks.")
        else:
            print("[!] schtasks failed; falling back to registry.")
            if not _windows_registry_fallback(task_name, execute, arguments):
                return False

    run_cmd(["schtasks", "/Run", "/TN", task_name], check=False)
    return True


# ---------------------------------------------------------------- remove

def remove_persistence(binary_name):
    os_type = platform.system().lower()
    service_name = f"autorun-{binary_name}.service"
    task_name = f"AutoRun_{binary_name}"

    if os_type == "linux":
        if is_omarchy():
            run_cmd(["systemctl", "--user", "disable", "--now", service_name],
                    check=False)
            p = os.path.expanduser(f"~/.config/systemd/user/{service_name}")
            if os.path.exists(p):
                os.remove(p)
            run_cmd(["systemctl", "--user", "daemon-reload"], check=False)
        else:
            if os.geteuid() != 0:
                print("[-] Need root to remove the system service.")
                return False
            run_cmd(["systemctl", "disable", "--now", service_name], check=False)
            p = f"/etc/systemd/system/{service_name}"
            if os.path.exists(p):
                os.remove(p)
            run_cmd(["systemctl", "daemon-reload"], check=False)
        print("[+] Linux persistence removed.")
        return True

    if os_type == "windows":
        run_cmd(["schtasks", "/Delete", "/TN", task_name, "/F"], check=False)
        try:
            import winreg
            for path in (
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run",
            ):
                try:
                    k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0,
                                       winreg.KEY_SET_VALUE)
                    try:
                        winreg.DeleteValue(k, task_name)
                    except FileNotFoundError:
                        pass
                    winreg.CloseKey(k)
                except FileNotFoundError:
                    pass
        except ImportError:
            pass

        vbs = os.path.join(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(),
                           "AutoRunner", f"AutoRun_{binary_name}.vbs")
        if os.path.exists(vbs):
            try:
                os.remove(vbs)
            except OSError:
                pass
        print("[+] Windows persistence removed.")
        return True

    print(f"[-] Unsupported OS: {os_type}")
    return False


# ---------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser(
        description="Install a program to run silently at boot/logon."
    )
    parser.add_argument("target", help="Path to the target program/script")
    parser.add_argument("--visible", action="store_true",
                        help="Show window/output (default: hidden/silent)")
    parser.add_argument("--log-file", default=None,
                        help="Linux: append output to this file instead of discarding")
    parser.add_argument("--remove", action="store_true",
                        help="Remove the existing autorun setup")
    args = parser.parse_args()

    target_file = args.target
    binary_name = os.path.splitext(os.path.basename(target_file))[0]
    silent = not args.visible

    if args.remove:
        sys.exit(0 if remove_persistence(binary_name) else 1)

    if not os.path.exists(target_file):
        print(f"[-] File not found: {target_file}")
        sys.exit(1)

    print(f"[*] OS:   {get_os_info()}")
    print(f"[*] Mode: {'silent (no window)' if silent else 'visible'}")

    os_type = platform.system().lower()
    if os_type == "linux":
        ok = setup_linux(target_file, binary_name, silent, args.log_file)
    elif os_type == "windows":
        ok = setup_windows(target_file, binary_name, silent)
    else:
        print(f"[-] Unsupported OS: {platform.system()}")
        sys.exit(1)

    if ok:
        print("[+] Done. It will start automatically after reboot/logon.")
        print("    Nothing will appear on screen.")
    else:
        print("[-] Setup failed.")
        sys.exit(1)


if __name__ == "__main__":
    main()