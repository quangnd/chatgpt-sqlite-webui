"""Run a command with Linux orphan reaping when container PID 1 does not reap."""
import ctypes
import os
import sys

libc = ctypes.CDLL(None, use_errno=True)
if libc.prctl(36, 1, 0, 0, 0) != 0:
    raise OSError(ctypes.get_errno(), 'PR_SET_CHILD_SUBREAPER failed')
child = os.fork()
if child == 0:
    os.execvp(sys.argv[1], sys.argv[1:])
exit_code = 1
while True:
    try:
        pid, status = os.waitpid(-1, 0)
    except ChildProcessError:
        break
    if pid == child:
        exit_code = os.waitstatus_to_exitcode(status)
sys.exit(exit_code)
