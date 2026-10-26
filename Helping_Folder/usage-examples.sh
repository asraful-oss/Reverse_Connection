# Default: silent (no window) persistence
python3 autorunner.py program.py
python3 autorunner.py myserver          # compiled binary, console-subsystem

# Linux only: keep a log file instead of discarding output
sudo python3 autorunner.py program.py --log-file /var/log/program.log

# Debug: allow a window/output (old behavior)
python3 autorunner.py program.py --visible

# Uninstall
python3 autorunner.py program.py --remove