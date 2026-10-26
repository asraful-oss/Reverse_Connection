# Support Status

Last updated: 2026-10-21  
Scope: `controller.py` and `client.py` v19

## Short Answer

This project is **cross-platform capable**, but it is **not verified as perfectly working on every operating system or distribution**.

Only the local Linux host used during development was tested. Real Ubuntu/RHEL VMware guests and real Windows, macOS, iOS, and Android devices were not available, so support on those platforms is **implemented but unverified**.

### Status legend

| Symbol | Meaning |
|---|---|
| ✅ | Verified locally in the environment described below |
| 🟡 | Code path exists and is expected to work, but was not tested on that OS |
| 🟠 | Requires extra setup, permissions, root, or a non-stock environment |
| ❌ | Not supported or not available |
| — | Not applicable |

## Platform Support Summary

| Platform | Controller | Client | TCP | HTTP / HTTPS | Notes |
|---|---|---|---|---|---|
| Linux desktop — tested host: Omarchy/Arch, Python 3.14.8 | ✅ | ✅ | ✅ | ✅ | Local end-to-end tests passed |
| Other Linux desktops and distros | 🟡 | 🟡 | 🟡 | 🟡 | Package names, desktop tools, firewall, and init system vary |
| Ubuntu / RHEL Linux server | 🟡 | 🟡 | 🟡 | 🟡 | Commands, files, and shell expected to work; no desktop capture by default |
| Windows 11 / latest Windows | 🟡 | 🟡 | 🟡 | 🟡 | Unverified on real Windows; firewall and Python installation matter |
| Windows 10 | 🟡 | 🟡 | 🟡 | 🟡 | Best-effort only; Windows 10 is past normal support in 2026 |
| macOS latest | 🟡 | 🟡 | 🟡 | 🟡 | Unverified; screen capture needs macOS privacy permission |
| Android 9 through current releases, via Termux | 🟡 | 🟡 | 🟡 | 🟡 | Termux setup and background restrictions apply |
| iOS / iPadOS — stock | ❌ | 🟠 | 🟡 | 🟡 | Stock iOS cannot run the controller; full client features are not reliable |
| iOS / iPadOS — jailbroken | 🟠 | 🟠 | 🟡 | 🟡 | Only realistic path for full iOS client features; unverified |

## Feature Matrix

| Platform | Commands and files | Interactive shell | Screenshot | Screen share | Persistence |
|---|---|---|---|---|---|
| Linux desktop — tested host | ✅ | ✅ | ✅ | ✅ | 🟡 systemd implemented; reboot test not completed |
| Other Linux desktops | 🟡 | 🟡 | 🟡 setup-dependent | 🟡 setup-dependent | 🟡 |
| Ubuntu / RHEL server | 🟡 | 🟡 | ❌ normally headless | ❌ normally headless | 🟡 systemd |
| Windows 11 / latest | 🟡 | 🟡 | 🟡 Pillow required | 🟡 same capture path | 🟡 Task Scheduler |
| Windows 10 | 🟡 | 🟡 | 🟡 | 🟡 | 🟡 best-effort |
| macOS latest | 🟡 | 🟡 | 🟡 Screen Recording permission required | 🟡 same capture path | 🟡 launchd |
| Android 9+ — Termux | 🟡 | 🟡 | 🟠 root required | 🟠 root required | 🟠 Termux:Boot and battery settings |
| iOS stock | 🟠 limited file transfer possible | ❌ | ❌ | ❌ | ❌ |
| iOS jailbroken | 🟠 unverified | 🟠 unverified | 🟠 unverified | 🟠 unverified | ❌ |

## Transport Support

| Transport | Status | Notes |
|---|---|---|
| TCP | ✅ locally verified | Original wire protocol; default mode; older TCP clients remain compatible |
| HTTP | ✅ locally verified | HTTP/1.1 chunked full-duplex tunnel over one controller port |
| HTTPS | ✅ locally verified | HTTP/1.1 tunnel inside TLS; TLS 1.2 and TLS 1.3 tested locally |
| HTTP/2 | ❌ | Not implemented; requires a separate HTTP/2 stack |
| HTTP/1.0 | ❌ | Rejected; the tunnel requires HTTP/1.1 chunked streaming |
| HTTPS certificate verification | ✅ locally verified with generated certificate | Client accepts self-signed certificates by default |

HTTP and HTTPS use:

- `POST /tunnel` for commands, files, screenshots, and screen frames
- `POST /shell` for the interactive shell
- `GET /health` for a health probe

The interactive shell reuses the controller port in HTTP/HTTPS mode. TCP mode keeps the existing separate temporary shell listener.

## HTTPS Configuration

Default generated certificate location:

```text
~/.controller-tls/cert.pem
~/.controller-tls/key.pem
```

Optional environment variables:

```text
CONTROLLER_TLS_DIR
CONTROLLER_TLS_CERT
CONTROLLER_TLS_KEY
```

Client verification:

```text
CONTROLLER_TLS_VERIFY=1
CONTROLLER_TLS_CA=/path/to/ca-or-self-signed-cert.pem
```

The client accepts self-signed certificates by default for convenience. This is not suitable for hostile networks without certificate verification.

## Platform Notes

### Linux

- Transport code is distribution-independent.
- Commands, files, and the interactive shell should work on normal desktop and server installations.
- Screenshots and screen sharing require a real desktop session:
  - X11: Pillow, scrot, ImageMagick, or similar.
  - Wayland: `grim` on wlroots compositors, `spectacle` on KDE, or a working desktop portal.
  - GNOME portals may require `python3-gi` / `python3-gobject`.
- Headless Linux servers do not provide screenshots or screen sharing by default.
- Persistence uses systemd and varies between user services and system services.
- Ubuntu, RHEL, Fedora, Debian, Arch, and derivatives have not been validated in real VMware guests.

### Windows

- Code paths exist for commands, files, shell, screenshots, and persistence.
- Interactive shell uses `pywinpty` when installed; otherwise it falls back to a pipe-based shell with reduced terminal fidelity.
- Screenshots normally require Pillow.
- Windows Firewall must allow the selected controller port inbound.
- Persistence uses Task Scheduler.
- No real Windows 10 or Windows 11 host was available for testing.

### macOS

- Controller and client code paths exist but were not tested on a real Mac.
- Commands, files, and shell are expected to work with a normal Python installation.
- Screenshots use macOS `screencapture` and/or Pillow.
- Screen Recording permission must be granted to the terminal running the client.
- Persistence uses a LaunchAgent.
- Screen capture can show only the wallpaper if privacy permission is missing.

### Android / Termux

- Commands, files, and the interactive shell work without root.
- Screen capture currently requires root and Android's `screencap`.
- Install Termux from F-Droid or GitHub; the Play Store build is restricted.
- Keep Termux unrestricted in battery settings and use `termux-wake-lock`.
- Android 12+ may kill background processes; Android 14+ may require disabling child-process restrictions.
- Newer Android releases may require Nearby Devices / local-network permission for Termux.
- Persistence uses `Termux:Boot`; it is not a true daemon and is subject to OEM battery policies.
- Shared storage may require `termux-setup-storage`.
- No real Android device was available for testing across Android 9 through 16.

### iOS / iPadOS

- **Stock iOS/iPadOS cannot run the controller**: Python apps there lack a real terminal and are suspended in the background.
- The client is only fully usable on a jailbroken device with a real Python environment.
- Stock iOS may allow limited file transfer, but it is not a dependable full-feature client.
- Screen capture and screen sharing are unavailable on stock iOS.
- Persistence is not supported in the normal iOS sandbox.
- No real iOS device was available for testing.

## Current Known Limits

- Maximum file size: 50 MiB per transfer in the current implementation.
- Maximum protocol message size: 96 MiB.
- HTTP/HTTPS tunnel is HTTP/1.1, not HTTP/2 or HTTP/3.
- HTTP proxies that buffer, compress, or rewrite streaming request/response bodies can break the tunnel.
- Proxy authentication and HTTP CONNECT proxy tunneling are not implemented.
- HTTP/2-only proxies are not supported.
- Self-signed HTTPS is accepted by default; use `CONTROLLER_TLS_VERIFY=1` for verification.
- Firewall configuration must allow the selected controller port.
- Quotas, permissions, antivirus, endpoint protection, and mobile background limits are outside the program's control.

## Verification Performed Locally

Environment: one Linux desktop host, Omarchy/Arch, Python 3.14.8.

Verified:

- TCP, HTTP, and HTTPS connection establishment
- Commands over all three transports
- 8 MiB upload/download in all three transports with SHA-256 match
- 32 MiB HTTP upload/download with SHA-256 match
- Interactive shell over TCP, HTTP, and HTTPS
- Screen sharing over HTTP and HTTPS
- Screenshot command path on the local desktop
- Multiple clients, session switching, disconnect, and reconnect
- HTTP health endpoint and malformed HTTP handling
- Oversized chunk/header handling and recovery after bad clients
- Throttled transfer test at approximately 512 KiB/s in all three transports
- TLS 1.2 and TLS 1.3 with ALPN `http/1.1`
- HTTPS certificate fallback when OpenSSL is unavailable
- Repeated HTTP connect/disconnect file-descriptor cleanup
- Python 3.9-compatible syntax parsing
- Clean `py_compile` run

Not verified:

- Real Ubuntu or RHEL VMware guests
- Real Windows 10 or Windows 11 hosts
- Real macOS hosts
- Real iOS or iPadOS devices
- Real Android devices across Android 9 through 16
- Real hardware firewalls or corporate proxies
- Reboot-level persistence on any platform
- Real headless-server screenshot behavior
- Real mobile background-killing and battery-policy behavior

## Recommended Wording for a README

> This project is cross-platform capable and has been verified locally on Linux for TCP, HTTP, and HTTPS transfers, commands, file transfer, interactive shell, screen sharing, multiple sessions, reconnects, and TLS 1.2/1.3. Windows, macOS, iOS, Android, Ubuntu, and RHEL support is implemented but not yet validated on real hardware or VMware guests. The HTTP transport is HTTP/1.1 only; it does not implement HTTP/2. Stock iOS cannot run the controller.

## Delivered Files

- `/home/boss/Projects/Malwares/Analysis/controller.py`
- `/home/boss/Projects/Malwares/Analysis/client.py`

Both files contain the same one-line transport switch:

```python
connection_type = tcp          # <- https / http / tcp
```
