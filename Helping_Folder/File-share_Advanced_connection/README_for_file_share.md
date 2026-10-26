# FileShare

Send files and folders between two computers with **one script**.
Works on the same Wi-Fi or over the internet with **Pinggy**, **ngrok** or **Cloudflare**.
Just paste an address: the connection type is detected automatically.

There are two versions that talk to each other, so each user picks one:

| Version | Start it with | Needs |
|---|---|---|
| 🐍 `file-share.py` | `python file-share.py` | Python 3.6+ (Linux, macOS, Windows, phones) |
| 🪟 `file-share.cs` | double-click `file-share.bat` | Nothing on Windows 10/11 |

Keep `file-share.bat` next to `file-share.cs`. The guides below say `python file-share.py`; a C# user runs `file-share.bat` at that step instead.

<details>
<summary><b>✨ Features</b></summary>

- 📁 **Any file, any size**: videos, apps, ZIPs, multi-GB files and whole folders
- 📦 **Many files at once**: `upload a.mp4 b.zip "My Photos"` or `upload *.jpg`
- ➕ **Send more anytime**: upload new files while others are still moving
- 🔁 **Both directions at the same time**: download and upload together
- 🤖 **Fully automatic**: detects TCP, HTTP, HTTPS, WebSocket and TLS by itself
- 🔢 **No port changes**: the port in the tunnel address is used automatically
- ✅ **One double-check question per upload**, even for many files
- 📊 **Live progress**: percent, speed and time left
- ⏸️ **Ctrl+Z** shows a numbered list so you can stop one, several or all transfers. **Ctrl+C** quits.
- 🌐 **Smart reconnect**: waits patiently if the tunnel is up before the other app, and spares ngrok's free quota
- ♻️ **Resume**: if the connection drops or the tunnel restarts, the transfer continues where it stopped
- 🛡️ **Safe**: every block is checksummed, nothing is overwritten, and free disk space is checked first
- 💻 **Cross-platform**: Windows, macOS, Linux, Android (Termux), iOS (a-Shell / Pythonista)
- 🤝 **Python ↔ C#**: `file-share.py` and `file-share.cs` connect to each other over every connection type

</details>

## 🚀 Choose your connection

Pick **one** method, then open its guide under **Setup guides**. Click any ▶ title to open or close it.

| Method | Best for |
|---|---|
| **A. Same Wi-Fi (LAN)** | Both computers on the same network: fastest |
| **B. Pinggy TCP** | Over the internet: recommended |
| **C. Pinggy HTTP / HTTPS** | Over the internet when TCP is blocked |
| **D. ngrok TCP** | ngrok users with a verified account |
| **E. ngrok HTTP / HTTPS** | ngrok users, small files |
| **F. Cloudflare Quick Tunnel** | Free, no account needed |

**Who does what**

- 👤 **User 1** runs the tunnel and **sends the address** to User 2.
- 👤 **User 2** puts that address in `file-share.py` (or `file-share.cs`).
- Only User 2 edits the file. User 1 changes nothing.

<details>
<summary><b>🪟 Using the C# version (file-share.cs)</b></summary>

**Windows: nothing to install**

1. Put `file-share.cs` and `file-share.bat` in the same folder.
2. Double-click `file-share.bat`.

The first time, it builds `file-share.exe` with the C# compiler that is already inside Windows (about 2 seconds), then starts it. If Windows Firewall asks, click **Allow access**.

**Putting in the address (User 2 only)**

Either open `file-share.cs` in Notepad and fill in the line near the top:
```
public static string PEER_ADDRESS = "tcp://abc.a.free.pinggy.link:41234";
```
or skip editing and start it from Command Prompt with the address:
```
file-share.bat tcp://abc.a.free.pinggy.link:41234
```

**Other ways to run it**

| Where | Command |
|---|---|
| Any OS with the .NET 10 SDK | `dotnet run file-share.cs` |
| Linux / macOS with Mono | `mcs file-share.cs` then `mono file-share.exe` |

`dotnet run file-share.cs` works only when the .NET 10 SDK is installed; Windows doesn't include it. `file-share.bat` uses it automatically when it's there.

Everything else is the same as the Python version: the commands, Ctrl+Z, the y/n question, resume and the downloads folder ("FileShare Downloads" next to the file).

</details>

## 🔌 Setup guides

<details>
<summary><b>A. Same Wi-Fi (LAN)</b></summary>

**Step 1: User 1 starts the program**
```
python file-share.py
```
The screen shows your LAN IP, for example `192.168.0.113`.

**Step 2: User 1 sends the IP to User 2**

📨 Send `192.168.0.113` to User 2 (WhatsApp, Messenger, email, anything).

**Step 3: User 2 pastes it and runs the program**

Open `file-share.py`, find this line near the top, and put the IP inside the quotes:
```python
PEER_ADDRESS = '192.168.0.113'
```
Save, then run:
```
python file-share.py
```

✅ Done. Both screens show `Connected`.

</details>

<details>
<summary><b>B. Pinggy TCP</b></summary>

**Step 1: User 1 starts the tunnel and the program**

Terminal 1:
```bash
ssh -p 443 -o ServerAliveInterval=30 -R0:localhost:4444 tcp@a.pinggy.io
```
Terminal 2:
```
python file-share.py
```
Terminal 1 shows a **TCP address** like this:
```
tcp://abcde.a.free.pinggy.link:41234
```

**Step 2: User 1 sends the TCP address to User 2**

📨 Copy the **whole** address, including `tcp://` and the port at the end, and send it to User 2.

**Step 3: User 2 pastes it and runs the program**

Open `file-share.py` and put the address inside the quotes:
```python
PEER_ADDRESS = 'tcp://abcde.a.free.pinggy.link:41234'
```
Save, then run:
```
python file-share.py
```

✅ Done. You don't need to change any port.

> ⏱️ Free Pinggy tunnels last 60 minutes. User 1 restarts the tunnel and sends the **new** address. User 2 types `connect <new address>` in the running program, and the transfer resumes by itself.

</details>

<details>
<summary><b>C. Pinggy HTTP / HTTPS</b></summary>

**Step 1: User 1 starts the tunnel and the program**

Terminal 1 (no `tcp@` this time):
```bash
ssh -p 443 -o ServerAliveInterval=30 -R0:localhost:4444 a.pinggy.io
```
Terminal 2:
```
python file-share.py
```
Terminal 1 shows **two addresses**:
```
http://abcde.a.free.pinggy.link
https://abcde.a.free.pinggy.link
```

**Step 2: User 1 sends the HTTPS address to User 2**

📨 Send only the **`https://`** one (it's encrypted). Both work, but one is enough.

**Step 3: User 2 pastes it and runs the program**

Open `file-share.py` and put the address inside the quotes:
```python
PEER_ADDRESS = 'https://abcde.a.free.pinggy.link'
```
Save, then run:
```
python file-share.py
```

✅ Done. It switches to full speed automatically.

> ⏱️ The 60-minute limit applies here too. See the note in section B.

</details>

<details>
<summary><b>D. ngrok TCP</b></summary>

> ngrok's free plan requires a card to be added to your ngrok account before TCP works.

**Step 1: User 1 starts the tunnel and the program**

Terminal 1:
```bash
ngrok tcp 4444
```
Terminal 2:
```
python file-share.py
```
Terminal 1 shows a **Forwarding** line with a **TCP address**:
```
tcp://0.tcp.in.ngrok.io:12345
```

**Step 2: User 1 sends the TCP address to User 2**

📨 Copy the **whole** address, including `tcp://` and the port, and send it to User 2.

**Step 3: User 2 pastes it and runs the program**
```python
PEER_ADDRESS = 'tcp://0.tcp.in.ngrok.io:12345'
```
Save, then run:
```
python file-share.py
```

✅ Done.

</details>

<details>
<summary><b>E. ngrok HTTP / HTTPS</b></summary>

**Step 1: User 1 starts the tunnel and the program**

Terminal 1:
```bash
ngrok http 4444
```
Terminal 2:
```
python file-share.py
```
Terminal 1 shows a **Forwarding** line with an **HTTPS address**:
```
https://abcd-12-34-56-78.ngrok-free.app
```

**Step 2: User 1 sends the HTTPS address to User 2**

📨 Copy the whole `https://...` address and send it to User 2.

**Step 3: User 2 pastes it and runs the program**
```python
PEER_ADDRESS = 'https://abcd-12-34-56-78.ngrok-free.app'
```
Save, then run:
```
python file-share.py
```

✅ Done.

> ⚠️ ngrok's free plan includes **1 GB of data per month**. For big files, use Pinggy or LAN.

</details>

<details>
<summary><b>F. Cloudflare Quick Tunnel</b></summary>

> Install `cloudflared` first from [Cloudflare's download page](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/downloads/). No account is needed.

**Step 1: User 1 starts the tunnel and the program**

Terminal 1:
```bash
cloudflared tunnel --url http://localhost:4444
```
Terminal 2:
```
python file-share.py
```
Terminal 1 shows an **HTTPS address** like this:
```
https://random-words-here.trycloudflare.com
```

**Step 2: User 1 sends the HTTPS address to User 2**

📨 Copy the whole `https://...trycloudflare.com` address and send it to User 2.

**Step 3: User 2 pastes it and runs the program**
```python
PEER_ADDRESS = 'https://random-words-here.trycloudflare.com'
```
Save, then run:
```
python file-share.py
```

✅ Done.

> ℹ️ The address changes every time `cloudflared` restarts. Send the new one, and User 2 types `connect <new address>`.

</details>

## 📖 How to use

Click a title to open it.

<details>
<summary><b>📤 Sending files</b></summary>

Type `upload` and one or more files or folders. You can also drag them into the terminal.
```
upload movie.mp4
upload movie.mp4 song.mp3 "My Photos"
upload *.jpg
```

The other user gets **one question** for everything in that upload:
```
Incoming 3 items from laptop (2.10 GB total):
   1. movie.mp4      MP4 file            1.42 GB
   2. song.mp3       MP3 file            8.00 MB
   3. My Photos      folder, 120 file(s) 690 MB
Do you want to download them? (y/n):
```

#### The double-check rule

| You type | Result |
|---|---|
| `y`, `y` | ✅ Download |
| `n`, `n` | ❌ Refuse |
| `n`, `y`, `y` | ✅ Download |
| `y`, `n`, `n` | ❌ Refuse |
| `y n y n` or `n y n y` | ❌ Refuse (answers kept changing) |

When it's accepted, both sides see live progress:
```
UP 3 items 45.2% 970 MB/2.10 GB 31.4 MB/s ETA 00:37
```

#### Sending more while files are moving

- Type `upload` again at any time. The other user gets a **new question** for the new files.
- If a question is already open, the new one waits its turn and appears right after.
- Both users can upload **at the same time**. Downloads and uploads run together.

Files are saved to the **`FileShare Downloads`** folder next to the script.

</details>

<details>
<summary><b>⏸️ Stopping files (Ctrl+Z)</b></summary>

Press **Ctrl+Z** (or type `cancel`) at any time. You see every running transfer with a number:
```
Running transfers:
   1. SENDING    movie.mp4     MP4 file      1.42 GB   45.2%
   2. RECEIVING  photos        folder, 120   690 MB    12.0%
   3. RECEIVING  song.mp3      MP3 file      8.00 MB   80.5%
Which to stop?
```

| You type | What stops |
|---|---|
| `2` | Only transfer 2 |
| `1 3` | Transfers 1 and 3 (put a space between numbers) |
| `1-3` | Transfers 1, 2 and 3 |
| `all` | Everything |
| just **Enter** | Nothing: go back |

Then answer **one** question:
```
Stop these 2 transfers? (y/n):
```
- `y` stops them. The other user sees which file was interrupted.
- `n` keeps everything going.

The connection always stays open. Shortcut: `cancel 2 3` jumps straight to the question.

> On iOS there is no Ctrl+Z key, so type `cancel` instead.

</details>

<details>
<summary><b>⌨️ Commands</b></summary>

| Command | What it does |
|---|---|
| `upload <paths>` | Send one or more files or folders |
| **Ctrl+Z** or `cancel` | Choose which transfers to stop |
| `cancel 2 3` | Stop transfers 2 and 3 (after one y/n) |
| `status` | Show the connection and every transfer with its number |
| `connect <address>` | Switch to a new address |
| `exit` or **Ctrl+C** | Quit |

</details>

<details>
<summary><b>🛠️ Troubleshooting</b></summary>

| Problem | Fix |
|---|---|
| Stuck on `Connecting...` | Check that User 1's tunnel and program are both running, and the address User 2 pasted is complete (with `tcp://` or `https://` and any port). |
| `points to this computer itself` | User 1 pasted their own address. Leave `PEER_ADDRESS = ''` on User 1 and send the address to User 2. |
| `The tunnel is up, but FileShare is not running` | User 1's tunnel works, but the program isn't started. Start `python file-share.py` on User 1's computer: it connects by itself. |
| `Port 4444 is busy` | Another copy is already running. Close it. |
| LAN won't connect | Allow Python (or `file-share.exe`) through the firewall, or use Pinggy. |
| `file-share.bat` says the compiler was not found | Turn on **.NET Framework 4.8** in *Turn Windows features on or off*, or install the .NET SDK. |
| `Could not rebuild` | FileShare is already open in another window. Close it, then start again. |
| `one upload can offer up to 1000` | Put the files in a folder and upload the folder: a folder can hold any number of files. |
| ngrok stopped working | The free plan allows 1 GB of data, 20,000 HTTP requests and 5,000 TCP connections a month. Switch to Pinggy or Cloudflare. |

</details>

<details>
<summary><b>🔒 Security</b></summary>

- Nothing is saved without the receiver's double **yes**.
- Files can never be written outside the download folder.
- Share your tunnel address only with the person you're sending to, and close the tunnel when you're done.

</details>

<details>
<summary><b>📋 Requirements</b></summary>

- Python version: Python 3.6 or newer, that's it.
- C# version: Windows 10 or 11 (nothing to install), or any OS with the .NET 10 SDK or Mono.
- Android: [Termux](https://termux.dev) with `pkg install python openssh`
- iOS: a-Shell or Pythonista

</details>
