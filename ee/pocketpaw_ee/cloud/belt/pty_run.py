# ee/pocketpaw_ee/cloud/belt/pty_run.py — run one command under a pseudo-terminal.
#
# `claude --cloud` refuses to start without a TTY, and the factory's subprocess
# runner gives children pipes. The cloud develop station (`cloud_station.py`)
# therefore launches `python pty_run.py <claude> --cloud <brief>`: this script
# forks the command onto a pty, collects everything it prints until it exits (or
# goes quiet for 120 s), writes that to stdout and exits with the command's code.
# Stdlib only; run as a script, never imported. The caller's timeout and
# process-group kill still bound it (a CLI stuck on a prompt goes quiet, then
# waitpid blocks until the caller kills the group).
import os
import pty
import select
import sys

pid, fd = pty.fork()
if pid == 0:
    os.execvp(sys.argv[1], sys.argv[1:])
out = b""
while True:
    try:
        r, _, _ = select.select([fd], [], [], 120)
        if not r:
            break
        data = os.read(fd, 4096)
        if not data:
            break
        out += data
    except OSError:
        break
_, status = os.waitpid(pid, 0)
sys.stdout.write(out.decode(errors="replace"))
sys.exit(os.waitstatus_to_exitcode(status))
