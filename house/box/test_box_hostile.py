#!/usr/bin/env python3
"""
test_box_hostile.py — the standalone hostile suite for box.py. NO LLM in the loop.

Every case is code that actively tries to break out, and the assertion is on what
the box DID, not on what the code intended. Run this inside the disposable
executor VM, never on a host that matters: if the box is wrong, these payloads
are exactly the things that go wrong.

Each case declares `expect`, one of:
    ok            the payload ran and the box reports success (used for the
                  positive controls — a box that blocks everything is easy and
                  useless)
    contained     the payload ran but its attempt FAILED inside the box
                  (nonzero exit, or the marker string is absent from stdout)
    timeout       the box cut it off at the wall clock
    truncated     the box stopped reading at the output cap

A case that "passes" because the payload crashed for an unrelated reason is a
false pass, so containment cases assert on a MARKER: the payload prints
"ESCAPED:<detail>" only when it actually succeeded. Absence of the marker is the
real assertion; the exit code alone is not trusted.
"""

import json, subprocess, sys

BOX = "/opt/box/box.py"


def run_case(code, wall=6):
    p = subprocess.run(
        [sys.executable, BOX, "--code-file", "/dev/stdin", "--wall", str(wall)],
        input=code, capture_output=True, text=True, timeout=wall + 40)
    try:
        return json.loads(p.stdout)
    except Exception:
        return {"status": "harness_error", "stdout": p.stdout[-400:],
                "stderr": p.stderr[-400:], "exit": p.returncode,
                "truncated": False, "timed_out": False, "cgroup_drained": None}


CASES = [
    # ---------- positive controls: the box must still be USEFUL ----------
    ("control/compute", "ok", """
print(sum(i*i for i in range(100000)))
"""),
    ("control/tmp-write", "ok", """
open('/tmp/f','w').write('x'*1000)
print('LEN', len(open('/tmp/f').read()))
"""),
    ("control/stdlib", "ok", """
import json, math, re, hashlib
print('OK', json.dumps({'h': hashlib.sha256(b'a').hexdigest()[:8], 'p': round(math.pi,3)}))
"""),

    # ---------- filesystem confinement ----------
    ("fs/read-etc-shadow", "contained", """
try:
    print('ESCAPED:shadow:' + open('/etc/shadow').read()[:40])
except Exception as e:
    print('blocked', type(e).__name__)
"""),
    ("fs/list-root-home", "contained", """
import os
try:
    e = os.listdir('/root')
    print('ESCAPED:root:' + repr(e[:5]))
except Exception as e:
    print('blocked', type(e).__name__)
"""),
    ("fs/write-outside-tmp", "contained", """
for p in ('/etc/x','/usr/x','/var/lib/x','/opt/x','/x'):
    try:
        open(p,'w').write('x')
        print('ESCAPED:write:'+p)
    except Exception:
        pass
print('done')
"""),
    ("fs/noexec-on-tmpfs", "contained", """
import os, subprocess
# Write a real ELF (copy of /bin/true) into the only writable mount and run it.
data = open('/bin/true','rb').read()
open('/tmp/t','wb').write(data)
os.chmod('/tmp/t', 0o755)
try:
    subprocess.run(['/tmp/t'], check=True)
    print('ESCAPED:noexec-bypassed')
except Exception as e:
    print('blocked', type(e).__name__)
"""),
    ("fs/proc-other-pids", "contained", """
import os
# In a fresh pidns we should see only ourselves; seeing the host's init means
# the pid namespace did not take.
pids = sorted(int(p) for p in os.listdir('/proc') if p.isdigit())
print('PIDS', pids[:8])
if any(p == 1 for p in pids) and len(pids) > 6:
    print('ESCAPED:pidns:' + repr(pids[:20]))
"""),
    ("fs/sys-hidden", "contained", """
import os
try:
    e = os.listdir('/sys/class')
    print('ESCAPED:sys:' + repr(e[:5]))
except Exception as ex:
    print('blocked', type(ex).__name__)
"""),
    ("fs/dev-block-devices", "contained", """
import os
try:
    e = [d for d in os.listdir('/dev')]
    print('DEV', sorted(e))
    open('/dev/sda','rb').read(16)
    print('ESCAPED:rawdisk')
except Exception as ex:
    print('blocked', type(ex).__name__)
"""),

    # ---------- network / non-IP channels ----------
    ("net/inet-socket", "contained", """
import socket
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    print('ESCAPED:inet-socket-created')
except Exception as e:
    print('blocked', type(e).__name__, e)
"""),
    ("net/vsock", "contained", """
import socket
# AF_VSOCK is a hypervisor channel that a network namespace does NOT cover and
# the host firewall never sees. In an LXD guest devlxd rides it.
try:
    AF_VSOCK = 40
    s = socket.socket(AF_VSOCK, socket.SOCK_STREAM)
    print('ESCAPED:vsock-socket-created')
except Exception as e:
    print('blocked', type(e).__name__, e)
"""),
    ("net/netlink", "contained", """
import socket
try:
    s = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 0)
    print('ESCAPED:netlink')
except Exception as e:
    print('blocked', type(e).__name__, e)
"""),
    ("net/unix-socket-probe", "contained", """
import socket, os
# Pathname unix sockets survive a network namespace; they die with the mount
# namespace hiding /run.
found = []
for d in ('/run','/var/run','/tmp'):
    try:
        for f in os.listdir(d):
            found.append(d+'/'+f)
    except Exception:
        pass
try:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    print('ESCAPED:unix-socket-created', found[:5])
except Exception as e:
    print('blocked', type(e).__name__, 'visible:', found[:5])
"""),

    ("net/io-uring-bypass", "contained", """
import ctypes
# io_uring is a COMPLETE seccomp bypass: submitted ops run on kernel worker
# threads that never pass the syscall filter, so IORING_OP_SOCKET/CONNECT/OPENAT
# work even though socket(2) and openat(2) are denied. MEASURED: before this was
# closed, io_uring_setup returned fd=3 here while every other net/* case still
# reported "blocked" — a control that was assuring something false.
libc = ctypes.CDLL('libc.so.6', use_errno=True)
libc.syscall.restype = ctypes.c_int
libc.syscall.argtypes = [ctypes.c_long, ctypes.c_uint, ctypes.c_void_p]
params = ctypes.create_string_buffer(120)
fd = libc.syscall(425, 8, ctypes.byref(params))
if fd >= 0:
    print('ESCAPED:io_uring_setup fd=%d' % fd)
else:
    print('blocked io_uring errno', ctypes.get_errno())
"""),
    ("priv/new-mount-api", "contained", """
import ctypes
# Blocking mount(2) does not block mounting: fsopen/fsconfig/fsmount/move_mount
# and open_tree are a second, complete mount API.
libc = ctypes.CDLL('libc.so.6', use_errno=True)
libc.syscall.restype = ctypes.c_int
esc = []
for nr, name in ((430, 'fsopen'), (428, 'open_tree'), (433, 'fspick')):
    libc.syscall.argtypes = [ctypes.c_long, ctypes.c_char_p, ctypes.c_uint]
    rc = libc.syscall(nr, b'tmpfs', 0)
    if rc >= 0:
        esc.append(name)
print(('ESCAPED:mountapi:' + ','.join(esc)) if esc else 'blocked new mount api')
"""),
    ("priv/clone-newuser", "contained", """
import ctypes
# unshare(2) and clone3 are denied, but plain clone(2) with CLONE_NEWUSER is a
# third door to a user namespace. It must be filtered on the flags ARGUMENT,
# because a blanket clone deny would break fork() and every thread.
libc = ctypes.CDLL('libc.so.6', use_errno=True)
libc.syscall.restype = ctypes.c_long
libc.syscall.argtypes = [ctypes.c_long, ctypes.c_ulong, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
CLONE_NEWUSER = 0x10000000
rc = libc.syscall(56, CLONE_NEWUSER | 17, None, None, None, 0)  # 17 = SIGCHLD
if rc == 0:
    import os; os._exit(0)     # the child, if it ever exists
if rc > 0:
    print('ESCAPED:clone-newuser pid=%d' % rc)
else:
    print('blocked clone(CLONE_NEWUSER) errno', ctypes.get_errno())
"""),
    ("control/fork-still-works", "ok", """
import os
# The regression guard for the clone filter: ordinary fork MUST still work, or
# the flags filter has been written as a blanket deny.
pid = os.fork()
if pid == 0:
    os._exit(7)
_, st = os.waitpid(pid, 0)
print('FORK_OK exit', os.WEXITSTATUS(st))
"""),
    ("control/threads-still-work", "ok", """
import threading
# clone3 returns ENOSYS so glibc falls back to clone(2); if that fallback were
# broken, threading would fail here.
out = []
t = threading.Thread(target=lambda: out.append('thread_ran'))
t.start(); t.join()
print('THREADS_OK', out)
"""),

    # ---------- privilege / namespace escalation ----------
    ("priv/unshare-userns", "contained", """
import ctypes
libc = ctypes.CDLL('libc.so.6', use_errno=True)
CLONE_NEWUSER = 0x10000000
rc = libc.unshare(CLONE_NEWUSER)
if rc == 0:
    print('ESCAPED:userns-created')
else:
    print('blocked errno', ctypes.get_errno())
"""),
    ("priv/setns", "contained", """
import ctypes, os
libc = ctypes.CDLL('libc.so.6', use_errno=True)
try:
    fd = os.open('/proc/1/ns/net', os.O_RDONLY)
    rc = libc.setns(fd, 0)
    print('ESCAPED:setns' if rc == 0 else 'blocked setns errno %d' % ctypes.get_errno())
except Exception as e:
    print('blocked', type(e).__name__)
"""),
    ("priv/mount", "contained", """
import ctypes
libc = ctypes.CDLL('libc.so.6', use_errno=True)
rc = libc.mount(b'proc', b'/tmp', b'proc', 0, None)
print('ESCAPED:mount' if rc == 0 else 'blocked errno %d' % ctypes.get_errno())
"""),
    ("priv/ptrace", "contained", """
import ctypes
libc = ctypes.CDLL('libc.so.6', use_errno=True)
rc = libc.ptrace(0, 1, 0, 0)   # PTRACE_TRACEME=0 against pid 1
print('ESCAPED:ptrace' if rc == 0 else 'blocked errno %d' % ctypes.get_errno())
"""),
    ("priv/whoami", "contained", """
import os
print('UID', os.getuid(), 'GID', os.getgid())
if os.getuid() == 0:
    print('ESCAPED:running-as-root')
"""),
    ("priv/caps", "contained", """
# The bounding set must be empty; any capability here is a finding.
try:
    txt = open('/proc/self/status').read()
    for line in txt.splitlines():
        if line.startswith(('CapEff','CapBnd','CapPrm')):
            print(line)
            if line.split()[1].strip('0') != '':
                print('ESCAPED:caps-present:'+line)
except Exception as e:
    print('blocked', type(e).__name__)
"""),

    # ---------- resource exhaustion (aggregate, not per-process) ----------
    ("res/fork-bomb-memory", "contained", """
import os
# 64 children each grabbing 256MB: per-process rlimits would allow multi-GB.
# Only an aggregate cgroup memory.max stops this.
for i in range(64):
    if os.fork() == 0:
        try:
            x = bytearray(256*1024*1024)
            while True: pass
        except Exception:
            os._exit(0)
print('ESCAPED:forked-64')
import time; time.sleep(30)
"""),
    ("res/pids-exhaust", "contained", """
import os
n = 0
try:
    while True:
        if os.fork() == 0:
            import time; time.sleep(60); os._exit(0)
        n += 1
        if n > 5000:
            print('ESCAPED:pids:'+str(n)); break
except Exception as e:
    print('blocked after', n, type(e).__name__)
"""),
    ("res/disk-fill", "contained", """
# The writable tmpfs is size-capped; filling it must not touch anything else.
n = 0
try:
    while True:
        with open('/tmp/f%d' % n, 'wb') as f:
            f.write(b'x' * (1024*1024))
        n += 1
        if n > 4096:
            print('ESCAPED:disk:'+str(n)); break
except Exception as e:
    print('blocked after', n, 'MB', type(e).__name__)
"""),
    ("res/wall-clock", "timeout", """
import time
while True:
    time.sleep(0.01)
"""),
    ("res/stdout-flood", "truncated", """
import sys
while True:
    sys.stdout.write('B' * 8192)
"""),

    # ---------- lifecycle: nothing may outlive the run ----------
    ("life/detached-survivor", "timeout", """
import os, sys, time
# Try to leave something behind that outlives the timeout and watches for the
# next invocation. --kill-child + cgroup.kill must reap it.
pid = os.fork()
if pid == 0:
    os.setsid()
    try:
        sys.stdout.close(); sys.stderr.close()
    except Exception:
        pass
    while True:
        time.sleep(1)
while True:
    time.sleep(1)
"""),
]


def main():
    results, failures = [], []
    for name, expect, code in CASES:
        r = run_case(code)
        st = r.get("status")
        out = r.get("stdout", "")
        escaped = "ESCAPED:" in out

        if expect == "ok":
            good = st == "ok" and not escaped
        elif expect == "timeout":
            good = r.get("timed_out") is True and not escaped
        elif expect == "truncated":
            good = r.get("truncated") is True and not escaped
        else:  # contained
            good = not escaped

        # A run that leaves the cgroup un-drained is a failure whatever else
        # happened: something outlived its box.
        if r.get("cgroup_drained") is False:
            good = False

        results.append((name, expect, st, good, out.strip().splitlines()[:2]))
        if not good:
            failures.append((name, expect, r))

    print(f"{'CASE':30s} {'EXPECT':10s} {'STATUS':17s} RESULT")
    print("-" * 92)
    for name, expect, st, good, sample in results:
        print(f"{name:30s} {expect:10s} {str(st):17s} "
              f"{'PASS' if good else 'FAIL':4s}  {sample}")
    print("-" * 92)
    print(f"{sum(1 for r in results if r[3])}/{len(results)} passed")

    if failures:
        print("\n=== FAILURES IN DETAIL ===")
        for name, expect, r in failures:
            print(f"\n--- {name} (expected {expect}) ---")
            print(json.dumps(r, indent=2)[:1200])
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
