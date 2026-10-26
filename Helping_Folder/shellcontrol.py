#!/usr/bin/env python3
"""controller.py — runs where you sit (phone/Termux, Linux, macOS, Windows).
Usage: python3 controller.py [port]
"""
import os, socket, sys, struct

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 4444


def write_stdout(data):
    """Portable stdout write — survives redirected stdout."""
    try:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    except Exception:
        os.write(sys.stdout.fileno(), data)


def get_terminal_size():
    try:
        sz = os.get_terminal_size()
        return sz.lines, sz.columns
    except OSError:
        return 24, 80


# ---------------------------------------------------------------- Unix
def run_unix(conn):
    import select, termios, tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd, termios.TCSANOW)   # TCSANOW: don't discard pending input
        while True:
            r, _, _ = select.select([conn, sys.stdin], [], [])
            if sys.stdin in r:
                data = os.read(fd, 4096)
                if not data:
                    break
                conn.sendall(data)
            if conn in r:
                data = conn.recv(65536)
                if not data:
                    break
                write_stdout(data)
    except (KeyboardInterrupt, OSError):
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


# ------------------------------------------------------------- Windows
def run_windows(conn):
    import ctypes, msvcrt, threading, time

    k32 = ctypes.windll.kernel32
    h = k32.GetStdHandle(-10)             # STD_INPUT_HANDLE
    mode = ctypes.c_uint()
    k32.GetConsoleMode(h, ctypes.byref(mode))
    # Disable ENABLE_PROCESSED_INPUT so Ctrl+C is delivered as input.
    k32.SetConsoleMode(h, mode.value & ~0x0001)

    stop = threading.Event()

    def pump():
        try:
            while not stop.is_set():
                data = conn.recv(65536)
                if not data:
                    break
                write_stdout(data)
        except OSError:
            pass
        finally:
            stop.set()

    threading.Thread(target=pump, daemon=True).start()

    # Windows scan codes -> ANSI escape sequences
    arrow_map = {
        "H": b"\x1b[A",   # Up
        "P": b"\x1b[B",   # Down
        "M": b"\x1b[C",   # Right
        "K": b"\x1b[D",   # Left
        "S": b"\x1b[3~",  # Delete
        "G": b"\x1b[H",   # Home
        "O": b"\x1b[F",   # End
    }

    # NOTE: msvcrt.getwch() cannot read CTRL+C reliably (Microsoft docs).
    # We attempt to capture it as \x03; if your Windows console swallows it,
    # consider using Windows Terminal which may deliver it as input.
    try:
        while not stop.is_set():
            if not msvcrt.kbhit():
                time.sleep(0.005)
                continue
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):
                ch2 = msvcrt.getwch()
                seq = arrow_map.get(ch2)
                if seq:
                    conn.sendall(seq)
                continue
            if ch == "\r":
                conn.sendall(b"\r\n")
            elif ch == "\x03":            # Ctrl+C (if delivered)
                conn.sendall(b"\x03")
            else:
                conn.sendall(ch.encode("utf-8", "replace"))
    except (KeyboardInterrupt, OSError):
        pass
    finally:
        k32.SetConsoleMode(h, mode.value)
        conn.close()


def main():
    if not sys.stdin.isatty():
        print("Error: stdin is not a terminal — run this from a real console.",
              file=sys.stderr)
        sys.exit(1)

    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", PORT))
    srv.listen(1)
    print(f"[*] listening on 0.0.0.0:{PORT}", file=sys.stderr)

    while True:
        conn, addr = srv.accept()
        print(f"[+] {addr} connected", file=sys.stderr)

        # Terminal-size handshake (rows, cols as uint16 BE)
        rows, cols = get_terminal_size()
        conn.sendall(struct.pack("!HH", rows, cols))

        # Lower latency for interactive use
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

        try:
            if os.name == "nt":
                run_windows(conn)
            else:
                run_unix(conn)
        except Exception as e:
            print(f"[!] session error: {e}", file=sys.stderr)
        finally:
            try: conn.close()
            except Exception: pass


if __name__ == "__main__":
    main()
