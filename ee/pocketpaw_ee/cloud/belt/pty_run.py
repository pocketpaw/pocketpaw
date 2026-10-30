# ee/pocketpaw_ee/cloud/belt/pty_run.py — run one command under a pseudo-terminal.
#
# `claude --cloud` refuses to start without a TTY, and the factory's subprocess
# runner gives children pipes. The cloud develop station (`cloud_station.py`)
# therefore launches `python pty_run.py <claude> --cloud <brief>`: this script
# forks the command onto a pty and copies its output to stdout chunk by chunk,
# flushed, so the caller holds everything printed so far even if it kills us on
# its timeout. It stops reading at EOF or after 120 s of quiet, then exits with
# the command's code.
#
# If the CLI shows its folder-trust prompt ("Yes, I trust this folder") it would
# wait forever, so the child is killed and this exits 3. The prompt is never
# answered: trusting a folder is the owner's call. Matching runs on a rolling
# tail with escape codes and non-letters dropped, since the TUI splits words
# with cursor moves and chunks split anywhere.
#
# Stdlib only; run as a script, never imported. pty.fork puts the child in its
# own session, so the caller's process-group kill does not reach it directly.
import os
import pty
import re
import select
import signal
import sys

_ESCAPES = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]")
_TRUST_PROMPT = b"trustthisfolder"

pid, fd = pty.fork()
if pid == 0:
    os.execvp(sys.argv[1], sys.argv[1:])
tail = b""
while True:
    try:
        r, _, _ = select.select([fd], [], [], 120)
        if not r:
            break
        data = os.read(fd, 4096)
        if not data:
            break
    except OSError:
        break
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()
    tail = (tail + data)[-8192:]
    if _TRUST_PROMPT in re.sub(rb"[^a-z]", b"", _ESCAPES.sub(b"", tail).lower()):
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        sys.exit(3)
_, status = os.waitpid(pid, 0)
sys.exit(os.waitstatus_to_exitcode(status))
