# 🌐 FileShare + Tailscale Funnel

### A stable sharing address for `file-share.py` — even behind CGNAT

Pinggy/ngrok links can expire or change after a restart. **Tailscale Funnel gives your FileShare listener a stable `*.ts.net` HTTPS address**, so your friend can save the address once instead of asking you for a new link every time.

> [!IMPORTANT]
> This guide is for exposing your existing `file-share.py` listener on local port **4444** through Tailscale Funnel. It does not replace your Python app or store files in the cloud.

## ✨ What is Tailscale?

[Tailscale](https://tailscale.com/) creates an encrypted network between devices, using WireGuard and NAT traversal to connect devices even when they are behind CGNAT. **Tailscale Funnel** is an additional feature that makes a local service reachable from the public internet over HTTPS—even when the visitor does not use Tailscale.

### Why use it?

- **Stable hostname:** save the `https://…ts.net` URL in `PEER_ADDRESS` once.
- **CGNAT-friendly:** no public IPv4 address or router port-forwarding required.
- **Reboot-friendly:** the Funnel configuration created with `--bg` persists across reboots and Tailscale restarts.
- **Network independent:** switch between Ethernet, home Wi-Fi, a hotspot, or university Wi-Fi without normally changing the hostname.
- **Keep your transfer app:** FileShare continues to handle uploads, downloads, and its own transfer-resume logic.
- **Currently available on Tailscale plans:** Funnel is in beta and has non-configurable bandwidth limits, so it is not a guaranteed unlimited-throughput service.

## 🚀 Quick start — Omarchy / Arch Linux

These commands install Tailscale, enable its system service at boot, connect the device, and configure Funnel for FileShare's local port `4444`.

### 1. Install and start Tailscale

```bash
sudo pacman -S tailscale --noconfirm
sudo systemctl enable --now tailscaled
sudo tailscale up
```

Open the authentication link printed by `tailscale up` and sign in.

### 2. Enable Funnel

```bash
sudo tailscale funnel --bg 4444
```

The first time, Tailscale may print a link asking you to enable Funnel for your tailnet. Open that link and approve it. Then check the configuration:

```bash
sudo tailscale funnel status
```

You should see output similar to:

```text
https://YOUR-MACHINE.YOUR-TAILNET.ts.net/
|-- / proxy http://127.0.0.1:4444
```

**Copy the actual `https://…ts.net` URL shown on your computer.** The example above is a placeholder, not a working address.

### 3. Put the URL in your friend's `file-share.py`

In your friend's copy, change `PEER_ADDRESS` near the top of the script:

```python
PEER_ADDRESS = 'https://YOUR-MACHINE.YOUR-TAILNET.ts.net'
```

Replace the example with your real Funnel URL. Keep your own `PEER_ADDRESS = ''` if you are the host waiting for your friend to connect. Your friend **does not need to install Tailscale** for this public Funnel setup.

### 4. Start FileShare on both computers

On Linux/macOS:

```bash
python3 file-share.py
```

On Windows:

```powershell
py file-share.py
```

Keep the host's FileShare program running and listening on port `4444`. Funnel can be configured while the app is stopped, but transfers will only work while FileShare is running.

## 💻 Installing Tailscale on other operating systems

Only the **host computer running Funnel** needs Tailscale for this setup. Your friend only needs the FileShare script and the saved URL.

| Operating system | Install instructions |
|---|---|
| **Omarchy / Arch Linux** | `sudo pacman -S tailscale --noconfirm` |
| **Ubuntu / Debian / Fedora / supported Linux** | Use the [official Linux installer](https://tailscale.com/docs/install/linux) below. |
| **Windows 10+** | Download and install from [Tailscale for Windows](https://tailscale.com/download/windows), then sign in. Use an elevated PowerShell/CLI for Funnel configuration if required. |
| **macOS** | Install from [Tailscale for macOS](https://tailscale.com/download/mac) and sign in. Check the [Funnel documentation](https://tailscale.com/docs/features/tailscale-funnel) for supported macOS client variants and CLI requirements. |
| **Other systems** | Follow the [official installation guide](https://tailscale.com/docs/install). |

For Debian/Ubuntu/Fedora and other supported systemd Linux distributions, run the official installer, then start Tailscale:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo systemctl enable --now tailscaled
sudo tailscale up
```

The Funnel command is generally `tailscale funnel --bg 4444`; platform permissions and CLI availability can vary. Refer to the [official Funnel CLI guide](https://tailscale.com/docs/reference/tailscale-cli/funnel) for non-Arch hosts.

## 🔁 Reboots, Wi-Fi changes, and temporary off/on

### After a reboot or shutdown

On Linux, `systemctl enable --now tailscaled` configures the Tailscale service to start at boot. Because Funnel was configured with `--bg`, it should resume after a reboot or a `tailscale down` / `tailscale up` cycle. **Your `*.ts.net` hostname normally stays the same.**

However, `file-share.py` is a separate interactive program. Start it after boot—or configure it to open at login—so port `4444` has an application listening behind Funnel.

### Turn only public Funnel access off

```bash
sudo tailscale funnel --https=443 off
```

Turn it back on with the original background configuration:

```bash
sudo tailscale funnel --bg 4444
```

### Disconnect and reconnect Tailscale itself

```bash
sudo tailscale down
sudo tailscale up
```

The Funnel endpoint is unavailable while Tailscale is disconnected. Its background configuration should resume once Tailscale reconnects. Check it with `sudo tailscale funnel status`.

### Change from Wi-Fi to Ethernet or another network

No `PEER_ADDRESS` change is normally needed. After switching networks, Tailscale may need a moment to reconnect. Some university or public Wi-Fi networks require a browser login or restrict VPN/tunnel traffic.

## 🧰 Troubleshooting

| Symptom | Check |
|---|---|
| Funnel says it is not enabled | Open the approval URL printed by the CLI and enable Funnel for your tailnet. |
| URL loads, but FileShare cannot connect | Make sure `file-share.py` is running and listening on local port `4444`; check `sudo tailscale funnel status`. |
| It works until reboot, then stops | Check `systemctl status tailscaled`, confirm Funnel was configured with `--bg`, and restart FileShare. |
| Friend cannot connect from university/public Wi-Fi | The network may require captive-portal login or block the connection. Test from another network. |
| You forgot the URL | Run `sudo tailscale funnel status` and copy the `https://…ts.net` hostname. |

## 🔐 Security note — read before exposing FileShare

**Funnel makes the service publicly reachable.** Anyone on the internet who discovers the URL can attempt to connect; the URL itself is not authentication. The current `file-share.py` handshake does not provide password/token authentication for the remote peer. Keep this in mind before using it for sensitive files, and do not accept unexpected transfers.

For stronger privacy, use a private Tailscale network with both people signed in and use Tailscale's private connectivity instead of public Funnel. If you need Funnel, add proper authentication to the application before relying on it for sensitive data.

## 📚 Official references

- [Tailscale installation](https://tailscale.com/docs/install)
- [Tailscale Funnel overview](https://tailscale.com/docs/features/tailscale-funnel)
- [Funnel CLI: commands, status, off/on, and reboot behavior](https://tailscale.com/docs/reference/tailscale-cli/funnel)
- [Tailscale CLI: `tailscale up`](https://tailscale.com/docs/reference/tailscale-cli/up)

---

*Made for the days when the file is 20 GB and copying a fresh tunnel URL is somehow the hardest part.*
