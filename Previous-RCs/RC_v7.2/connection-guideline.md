# 📘 Connection Guideline: TCP / HTTP / HTTPS over LAN, Pinggy and ngrok

Both scripts detect everything automatically, so there's no mode to match.

* **`sender.py`** answers **every** kind of receiver on one port: raw TCP, HTTP/1.0, HTTP/1.1, HTTPS through a tunnel, and even direct HTTPS / TLS on a LAN (TLS 1.2 and 1.3).
* **`receiver.py`** accepts **any address you paste**: an IP, a hostname, `host:port`, or the full URL a tunnel prints. It picks the protocol itself, tries the others if the first doesn't fit, remembers what worked, and reconnects forever.

Requirements: Python 3.6+ (tested on 3.8, 3.9, 3.12 and 3.13). Standard library only. Runs on Windows, macOS, Linux (desktop and servers), Android (Termux) and iOS (Pythonista / a-Shell).
Run with `python sender.py` / `python receiver.py` (use `python3` on macOS/Linux/Termux if `python` isn't found; on Windows `py` also works).

---

## 0. Who edits what

| Machine | File | What to set |
| :--- | :--- | :--- |
| **Sender** | `sender.py` | Nothing. Keep `DEFAULT_IP = '0.0.0.0'` and `DEFAULT_PORT = 4444`. (`connection_type` is ignored: the sender serves everything.) |
| **Receiver** | `receiver.py` | Only `DEFAULT_IP` (paste the address), plus `DEFAULT_PORT` if the address has no port. Leave `connection_type = auto`. |

The sender always listens on its **own local port 4444**, for LAN, Pinggy and ngrok alike. Tunnels forward to that port.

---

## 1. Local Network (LAN)

1. **Sender:** `python sender.py`
   You'll see: `Sender listening on 0.0.0.0:4444 (IPv6+IPv4) - TCP, HTTP/1.x, HTTPS, direct TLS`
2. **Receiver:** in `receiver.py`
   ```python
   DEFAULT_IP = '192.168.0.113'   # the sender's LAN IP
   DEFAULT_PORT = 4444
   ```
   then `python receiver.py` → `[+] Connected to 192.168.0.113:4444 via TCP`

Want HTTP or HTTPS on the LAN instead? Paste `http://192.168.0.113:4444` or `https://192.168.0.113:4444`. Both work directly: the sender answers TLS with its built-in certificate, and the receiver trusts that exact certificate only.

Find the sender's LAN IP: Windows `ipconfig` · macOS `ipconfig getifaddr en0` · Linux `hostname -I` or `ip addr` · Termux `ifconfig`.

---

## 2. Internet via Pinggy (free, no account)

Uses the SSH client already built into Windows 10+, macOS and Linux (Termux: `pkg install openssh`; iOS: a-Shell has `ssh`).

**Order:** start `sender.py` → start the tunnel in a second terminal (sender machine) → paste the address into `receiver.py` → run it.

### Prompts
* `Are you sure you want to continue connecting?` → `yes` (first time only; `-o StrictHostKeyChecking=accept-new` below answers it for you).
* Password prompt → **just press Enter**.
* **Keep the SSH window open.** Free tunnels last **60 minutes** and get a **new address** every time. When one expires, rerun the command and paste the new address into the receiver.

### 🔹 TCP tunnel (fastest detection, about 3 s heartbeats)
**Sender machine:**
```bash
ssh -p 443 -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -R0:localhost:4444 tcp@a.pinggy.io -T
```
or,
```bash
ssh -p 443 -R0:localhost:4444 tcp@a.pinggy.io -T
```

**You can use tcp@free.pinggy.io instead of tcp@a.pinggy.io**

Pinggy prints something like `tcp://abcde.a.free.pinggy.link:41234`.

**Receiver**: paste it exactly:
```python
DEFAULT_IP = 'tcp://abcde.a.free.pinggy.link:41234'
```

### 🔹 HTTP / HTTPS tunnel
**Sender machine** (no `tcp@`):
```bash
ssh -p 443 -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -R0:localhost:4444 a.pinggy.io -T
```
or,

```bash
ssh -p 443 -R0:localhost:4444 a.pinggy.io -T
```

**You can use free.pinggy.io instead of a.pinggy.io**

Pinggy prints two URLs, e.g. `http://abcd-12-34-56-78.run.pinggy-free.link` and `https://abcd-12-34-56-78.run.pinggy-free.link`.

**Receiver**: paste either one (HTTPS recommended):
```python
DEFAULT_IP = 'https://abcd-12-34-56-78.run.pinggy-free.link'
```

Pinggy's browser "screening page" never affects the receiver: it sends a non-browser User-Agent and `X-Pinggy-No-Screen`.

---

## 3. Internet via ngrok (free account required)

### One-time setup (sender machine)
1. Sign up at https://dashboard.ngrok.com/signup and download the agent from https://ngrok.com/download (Windows, macOS, Linux, ARM / Termux).
2. Copy your authtoken from the dashboard, then run:
   ```bash
   ngrok config add-authtoken <YOUR_TOKEN>
   ```

### 🔹 HTTPS tunnel (free)
**Sender machine:** start `sender.py`, then:
```bash
ngrok http 4444
```
ngrok shows `Forwarding https://<your-name>.ngrok-free.app -> http://localhost:4444` (newer accounts may get `.ngrok-free.dev`). Your free dev domain stays the same between restarts.

**Receiver:**
```python
DEFAULT_IP = 'https://<your-name>.ngrok-free.app'
```

> ngrok Free allows **20,000 HTTP requests a month**. The receiver detects ngrok hostnames and checks every **150 s** so one month fits the quota. For second-level detection on ngrok, use the TCP tunnel below.
> ngrok's browser warning page never affects the receiver: it sends `ngrok-skip-browser-warning`.

### 🔹 TCP tunnel (needs a card on file, never charged)
ngrok requires a valid credit or debit card in the dashboard (Settings → verification) before TCP works on Free. Prepaid cards are not accepted.
```bash
ngrok tcp 4444
```
ngrok shows something like `tcp://0.tcp.in.ngrok.io:12345`. Paste it into the receiver:
```python
DEFAULT_IP = 'tcp://0.tcp.in.ngrok.io:12345'
```
ngrok closes TCP connections idle for 5 minutes. The 3-second heartbeats keep the link busy, so that never happens. Free includes 5,000 TCP connections a month, and each reconnect uses one.

---

## 4. Quick Reference

| Route | Sender machine runs | Paste into receiver `DEFAULT_IP` | Detects a dead link in |
| :--- | :--- | :--- | :--- |
| LAN TCP | `python sender.py` | `192.168.x.x` (port 4444) | instant on close, about 12 s on silent drop |
| LAN HTTP / HTTPS | `python sender.py` | `http://192.168.x.x:4444` / `https://...:4444` | about 5 to 11 s |
| Pinggy TCP | `ssh ... tcp@free.pinggy.io` | `tcp://xxx.a.free.pinggy.link:PORT` | instant on close, about 12 s on silent drop |
| Pinggy HTTP(S) | `ssh ... free.pinggy.io -T` | `https://xxx.run.pinggy-free.link` | about 5 to 11 s |
| ngrok HTTPS | `ngrok http 4444` | `https://xxx.ngrok-free.app` | up to about 2.5 min (quota-safe) |
| ngrok TCP | `ngrok tcp 4444` | `tcp://0.tcp.xx.ngrok.io:PORT` | instant on close, about 12 s on silent drop |

---

## 5. How it works (and why it's reliable)

* **One port, every protocol.** The sender peeks at each new connection's first bytes: a TLS handshake gets TLS, then it checks the inner stream. The `CMON1` hello means a TCP heartbeat session, and anything else is answered as HTTP/1.x (GET, HEAD, POST, PUT, OPTIONS; bodies drained). Several receivers can watch one sender at the same time.
* **End-to-end proof, not just a socket.** Through a tunnel, a connection or a reply can come from the tunnel even when the sender is off. So TCP needs the sender's own handshake reply plus heartbeats both ways every 3 s (silence over 12 s = gone), and HTTP needs the sender's `X-CMon` header. Tunnel error pages (`502`, `404 endpoint offline`) and silently dead machines behind a tunnel are always detected.
* **OS-level backup.** TCP keepalive is tuned per platform (Linux/Android `TCP_KEEPIDLE/INTVL/CNT` + `TCP_USER_TIMEOUT`, macOS/iOS `TCP_KEEPALIVE`, Windows `TCP_KEEP*` or `SIO_KEEPALIVE_VALS`). `TCP_NODELAY` is always on.
* **Automatic protocol choice.** Order: the URL's scheme first, then by port (443 → HTTPS first, 80 → HTTP first, otherwise TCP first), then the rest. The protocol that worked is tried first on every reconnect. If the host or port itself is unreachable, it doesn't waste time trying the others.
* **Secure TLS.** Public endpoints are fully verified (system CA store, or `certifi` if installed and the store is empty), with the best TLS version both sides support (1.3 when available). The only exception is the sender's own built-in certificate, which is pinned exactly.
* **Fast, calm reconnects.** Retries start at 1 s and back off to 30 s (with jitter). The backoff resets once a link has been stable for 10 s. Ctrl+C exits instantly on every OS.
* **IPv4 and IPv6.** The sender listens dual-stack, and the receiver resolves every address a name has.

---

## 6. Platform notes

| Platform | Notes |
| :--- | :--- |
| **Windows** | Allow Python through the firewall when prompted (Private networks) so LAN receivers can connect. OpenSSH Client is built in on Windows 10+ (Settings → Optional features if missing). |
| **macOS** | python.org installs: run *Install Certificates.command* once (in the Python folder) so HTTPS can verify certificates. |
| **Linux / servers** | No root needed (port 4444 is above 1024). To keep it running after logout: `nohup python3 sender.py &`, `tmux`, or a systemd service. |
| **Android (Termux)** | `pkg install python openssh`. Run `termux-wake-lock` and turn off battery optimisation for Termux, or Android may pause it in the background. |
| **iOS (Pythonista / a-Shell)** | Works while the app is in the foreground. iOS suspends background apps, so the receiver will report a disconnect and reconnect on its own when you return. Keep a sender on iOS in the foreground. |

---

## 7. Troubleshooting

| What you see | What it means / fix |
| :--- | :--- |
| `connection refused (sender or tunnel not running)` | Start `sender.py` (and the tunnel). On LAN, check the IP. |
| `endpoint answered, but the sender behind it is not running` | The tunnel is up but `sender.py` isn't running on the sender machine, or the tunnel points at a port other than 4444. |
| `host name not found` | Typo in the address, or the free tunnel expired and its hostname is gone. Paste the current address. |
| `no reply in time` | Network too slow or blocked, or the sender machine is frozen or asleep. The receiver keeps retrying automatically. |
| Worked, then stopped after about 1 hour (Pinggy) | The free tunnel expired. Rerun the SSH command and paste the new address. |
| `Port 4444 not available yet ... retrying` (sender) | Another program uses the port. The sender keeps retrying; to use another port, change `DEFAULT_PORT` in `sender.py` and the `4444` in the tunnel command. |
| `TLS handshake failed` on a public HTTPS URL | Your Python has no CA certificates (see the macOS note, or `pip install certifi`), or something on your network intercepts HTTPS. |
| ngrok: `ERR_NGROK_8013` or TCP refused | TCP needs a card on file in the ngrok dashboard. Use `ngrok http 4444` and an `https://` address instead. |
