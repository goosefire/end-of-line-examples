#!/usr/bin/env python3
"""
box.py — the hardened execution box for End of Line's `run_code` tier.

STANDALONE BY DESIGN. No LLM, no arena, no network, no harness import. It takes a
snippet of code, runs it under every confinement layer we have, and returns a
bounded structured result. It is built and hostile-tested with no model in the
loop (parent design §8 step 2) precisely so that the sandbox argument can be
proven on its own before anything is ever wired to a tool call.

WHAT THIS IS NOT. It is not the trust boundary. The boundary is the VM the box
runs inside — a secret-free, network-free executor whose compromise finds nothing
and reaches nowhere. Everything here RAISES THE COST of getting to that wall; it
does not replace it. Two independent reviews killed an earlier design that claimed
otherwise, and the language here is deliberately narrow as a result.

The input is treated as ARBITRARY HOSTILE NATIVE CODE, not "Python". `python3 -I`
still reaches ctypes, os.system, execve, and writing an ELF, so the box is sized
for native code that actively attacks it.

Layers, each tied to a finding from the reviews:

  cgroup v2 subtree   aggregate memory.max / pids.max / cpu.max. NOT rlimits:
                      RLIMIT_NPROC counts tasks for the shared real UID across
                      the whole machine, and RLIMIT_AS is per-process, so a
                      64-way fork at 512 MB each is multi-GB under rlimits alone.
                      cgroup.kill takes the whole tree down atomically.
  unshare             --pid --net --ipc --uts --mount --mount-proc, and
                      --kill-child=SIGKILL because `unshare --fork` waits on its
                      child, does not forward signals, and will otherwise orphan
                      pidns processes that outlive the timeout and lie in wait
                      for the next invocation.
  filesystem          / remounted read-only; /root /home /run /boot /srv hidden
                      under empty tmpfs; /sys hidden; /dev replaced by a minimal
                      tmpfs carrying only null/zero/urandom/random; one writable
                      mount — a size-capped tmpfs at /tmp, nosuid+nodev+noexec so
                      a dropped ELF cannot be executed from the only place it can
                      be written.
  privilege           setpriv --reuid/--regid/--clear-groups --nnp
                      --bounding-set=-all --inh-caps=-all. `setpriv --reuid` does
                      NOT clear capability sets on its own; no_new_privs does NOT
                      block unshare/userns/ptrace/clone3 (that is seccomp's job).
  seccomp             a denylist filter installed by the inner runner AFTER
                      privilege drop and BEFORE the payload execs, closing the
                      channels a network namespace cannot: AF_VSOCK (a live host
                      channel in an LXD guest — devlxd rides it), setns, mount,
                      new namespaces, ptrace, bpf, kernel module loading — and
                      critically io_uring, whose ops run on kernel worker threads
                      that never reach a syscall filter at all (denying socket(2)
                      does NOT deny IORING_OP_SOCKET). clone(2) is filtered on its
                      CLONE_NEWUSER flag rather than denied, so fork and threads
                      still work.
  fd hygiene          close_fds + pass_fds=() + fresh pipes + /dev/null stdin. An
                      inherited connected socket keeps its ORIGINAL netns and
                      would defeat --net entirely.
  output              BOUNDED READ — at most cap+1 bytes are ever read from each
                      pipe, then the cgroup is killed. Never buffer the full
                      stream: a 10-second stdout flood otherwise OOMs the parent,
                      and truncating after the fact is far too late.
  run dir             under a root-owned, non-writable parent (/run/eolbox, 0711).
                      Never chown an entry inside sticky world-writable /tmp: a
                      shared-uid rename->symlink race can point the parent's
                      pre-drop cwd at a secret directory.

Usage:
    box.py --code-file snippet.py [--wall 10] [--mem-mb 512] [--pids 64]
    box.py --self-test          # benign checks that the limits actually bind
"""

import argparse, errno, json, os, resource, secrets, selectors, shutil, signal
import subprocess, sys, time

CGROUP_ROOT = "/sys/fs/cgroup"
CGROUP_PARENT = os.path.join(CGROUP_ROOT, "eolbox")
# NOT under /run: the prelude hides /run entirely (systemd sockets, agent state),
# which would also hide the payload we are trying to execute. /var/lib is visible,
# lives on the read-only root inside the box, and gives us the root-owned
# non-writable parent the TOCTOU finding requires. Never sticky /tmp.
RUN_PARENT = "/var/lib/eolbox"

WALL_DEFAULT = 10          # seconds of wall clock
MEM_MB_DEFAULT = 512       # aggregate across the whole process tree
PIDS_DEFAULT = 64          # aggregate task count
CPU_QUOTA_PCT = 100        # 100% of one core
OUT_CAP = 4096             # bytes retained per stream
CODE_MAX = 8192            # bytes of submitted code
TMPFS_MB = 64              # the single writable mount
NOBODY_UID, NOBODY_GID = 65534, 65534

# Written next to this file; the inner runner installs seccomp then execs the
# payload, so the filter covers the payload and everything it spawns (seccomp is
# preserved across execve and inherited by children) without ever constraining
# unshare/setpriv, which legitimately need the syscalls we deny.
RUNNER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_boxrun.py")


class BoxError(RuntimeError):
    pass


# ------------------------------------------------------------------ cgroup --

def _write(path, val):
    with open(path, "w") as f:
        f.write(val)


def cgroup_prepare():
    """
    Create the parent cgroup once and delegate the controllers we need.

    Enable them ONE AT A TIME. A write to cgroup.subtree_control is atomic, and
    asking for a controller the parent does not itself have enabled fails the
    WHOLE write with ENOENT — so a single unavailable controller silently costs
    you the ones that were available. Measured: inside an LXD VM the root cgroup
    delegates only `memory pids` (no `cpu`), so "+memory +pids +cpu" leaves the
    box with NO limits at all while looking like it merely skipped cpu.
    """
    if not os.path.isdir(CGROUP_PARENT):
        os.makedirs(CGROUP_PARENT, exist_ok=True)

    # A controller is only available to us if the ROOT delegates it downward.
    # MEASURED: an LXD VM boots with root subtree_control = "memory pids" — `cpu`
    # exists in cgroup.controllers but is never delegated, so cpu.max silently
    # does not exist and the CPU limit is not applied. systemd's
    # DefaultCPUAccounting did NOT fix this (verified); writing the controller
    # directly does. Self-heal here rather than depend on image configuration,
    # so the box behaves the same wherever it is dropped.
    for c in ("memory", "pids", "cpu"):
        try:
            root_have = set(open(
                os.path.join(CGROUP_ROOT, "cgroup.controllers")).read().split())
            root_on = set(open(
                os.path.join(CGROUP_ROOT, "cgroup.subtree_control")).read().split())
        except OSError:
            break
        if c in root_have and c not in root_on:
            try:
                _write(os.path.join(CGROUP_ROOT, "cgroup.subtree_control"), "+" + c)
            except OSError:
                pass  # not fatal: the verification below decides what binds

    try:
        available = set(open(
            os.path.join(CGROUP_PARENT, "cgroup.controllers")).read().split())
    except OSError:
        available = set()
    enabled = []
    for c in ("memory", "pids", "cpu"):
        if c not in available:
            continue
        try:
            _write(os.path.join(CGROUP_PARENT, "cgroup.subtree_control"), "+" + c)
            enabled.append(c)
        except OSError as e:
            if e.errno not in (errno.EBUSY, errno.EEXIST):
                pass  # already on, or genuinely unavailable; verified below

    # Verify the knobs that actually BIND exist, rather than trusting the writes.
    # memory and pids are load-bearing (cpu is bounded by wall clock anyway), so
    # their absence is fatal: a box with no aggregate limit is not a box.
    probe = os.path.join(CGROUP_PARENT, "x-probe")
    os.makedirs(probe, exist_ok=True)
    try:
        missing = [k for k in ("memory.max", "pids.max")
                   if not os.path.exists(os.path.join(probe, k))]
    finally:
        os.rmdir(probe)
    if missing:
        raise BoxError(
            f"cgroup controllers not delegated: {missing} absent under {CGROUP_PARENT} "
            f"(available={sorted(available)}, enabled={enabled}). "
            f"Enable them on the parent, e.g. echo '+memory +pids' > "
            f"{CGROUP_ROOT}/cgroup.subtree_control")
    return enabled


def cgroup_make(cid, mem_mb, pids):
    d = os.path.join(CGROUP_PARENT, cid)
    os.makedirs(d, exist_ok=True)
    _write(os.path.join(d, "memory.max"), str(mem_mb * 1024 * 1024))
    # No swap escape hatch: without this the tree can exceed memory.max in swap.
    try:
        _write(os.path.join(d, "memory.swap.max"), "0")
    except OSError:
        pass  # swap accounting may be absent; not fatal
    _write(os.path.join(d, "pids.max"), str(pids))
    # cpu.max may genuinely not exist: inside an LXD VM the root cgroup delegates
    # only `memory pids`. Report that rather than swallowing it — a silently
    # absent CPU limit is exactly the kind of "control that isn't there" this box
    # is supposed to make impossible to have by accident. Wall clock still bounds
    # the run, so this is a degradation, not a hole.
    cpu_limited = False
    try:
        _write(os.path.join(d, "cpu.max"), f"{CPU_QUOTA_PCT * 1000} 100000")
        cpu_limited = True
    except OSError:
        pass
    return d, cpu_limited


def cgroup_kill(d):
    """Kill the ENTIRE tree atomically and wait for the cgroup to drain."""
    try:
        _write(os.path.join(d, "cgroup.kill"), "1")
    except OSError:
        pass
    for _ in range(200):                      # up to ~2s
        if _cgroup_empty(d):
            return True
        time.sleep(0.01)
    return _cgroup_empty(d)


def _cgroup_empty(d):
    try:
        with open(os.path.join(d, "cgroup.procs")) as f:
            return f.read().strip() == ""
    except OSError:
        return True


def cgroup_remove(d, timeout=5.0):
    """
    Remove the per-invocation cgroup, retrying for a while.

    MEASURED: after cgroup.kill, `cgroup.procs` reads empty and `populated 0`
    is set, yet rmdir still fails with EBUSY for a beat while the kernel
    finishes tearing the (pid-namespace) cgroup down. A 1s window was too short
    for killed runs specifically, and the hostile suite left two empty cgroups
    behind — harmless individually, but an unbounded leak against
    cgroup.max.descendants over a long-running fleet.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.rmdir(d)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)


def cgroup_sweep():
    """
    Self-heal: drop any EMPTY leftover cgroup from an earlier run that lost the
    rmdir race (or died before cleanup). Only empty ones — a populated cgroup
    belongs to a live invocation and is never touched.
    """
    swept = 0
    try:
        entries = os.listdir(CGROUP_PARENT)
    except OSError:
        return 0
    for name in entries:
        d = os.path.join(CGROUP_PARENT, name)
        if not os.path.isdir(d) or not _cgroup_empty(d):
            continue
        try:
            os.rmdir(d)
            swept += 1
        except OSError:
            pass
    return swept


# --------------------------------------------------------------- the mount --
# Runs as root INSIDE the new mount namespace, before privilege is dropped.
# Order matters: overmount everything sensitive first, then seal / read-only
# last, so each mount still has a writable-enough parent to attach to.

MOUNT_PRELUDE = r"""
set -e
mount --make-rprivate /

# Hide anything that could hold a secret or a live socket. size=0 makes them
# not merely empty but unusable.
# NOTE /opt is deliberately NOT hidden: the inner runner ships alongside box.py
# and must stay readable. The box's own source is not a secret — the payload may
# read it. What must not be reachable is state: keys, journals, sockets, logs.
for d in /root /home /run /boot /srv /media /mnt /var/log /var/tmp; do
    [ -d "$d" ] && mount -t tmpfs -o size=0,mode=000,nosuid,nodev none "$d" || true
done

# /sys is a broad information and attack surface and nothing here needs it.
mount -t tmpfs -o size=0,mode=000,nosuid,nodev none /sys || true

# A minimal /dev, BUILT rather than borrowed: a fresh tmpfs with exactly four
# character devices created by mknod. (Bind-mounting from the real /dev needs a
# staging copy, and you cannot copy /dev/zero — it is infinite. mknod is the
# correct primitive and we still hold CAP_MKNOD here, before the privilege drop.)
# Note: NO `nodev` on this mount, or the nodes we just made would be inert.
mount -t tmpfs -o size=1m,mode=755,nosuid,noexec none /dev
mknod -m 666 /dev/null    c 1 3
mknod -m 666 /dev/zero    c 1 5
mknod -m 666 /dev/full    c 1 7
mknod -m 666 /dev/random  c 1 8
mknod -m 666 /dev/urandom c 1 9
ln -s /proc/self/fd/0 /dev/stdin
ln -s /proc/self/fd/1 /dev/stdout
ln -s /proc/self/fd/2 /dev/stderr

# The ONE writable mount. noexec so an ELF written here cannot be run; nosuid
# and nodev for the obvious reasons. nr_inodes is explicit so inode exhaustion
# has a deterministic bound to assert against rather than a kernel default.
mount -t tmpfs -o size=__TMPFS__m,nr_inodes=4096,mode=1777,nosuid,nodev,noexec none /tmp

# Seal the root filesystem last. BOTH source and target must be given: with a
# single argument mount(8) goes looking in /etc/fstab to resolve it, and on a
# cloud image that means "can't find LABEL=cloudimg-rootfs" — the seal silently
# fails and every run dies before reaching the payload.
mount --bind / /
mount -o remount,bind,ro,nosuid,nodev / /

cd /tmp
exec "$@"
"""


# ----------------------------------------------------------------- the run --

def _preexec(cgroup_dir):
    """Runs in the child between fork and exec."""
    def fn():
        # Join the cgroup BEFORE exec so every descendant is accounted from the
        # first instruction — there is no window in which an unaccounted fork
        # can happen.
        with open(os.path.join(cgroup_dir, "cgroup.procs"), "w") as f:
            f.write("0")
        # New session so a stray killpg cannot reach back into the parent, and
        # so the child is not sharing our controlling terminal.
        os.setsid()
        # If the parent dies, take the child with it.
        try:
            import ctypes
            ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGKILL, 0, 0, 0)  # PR_SET_PDEATHSIG
        except Exception:
            pass
        # Cheap belt-and-suspenders on top of the cgroup (which is the real
        # control). Deliberately generous — the cgroup binds first.
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_FSIZE, (TMPFS_MB * 1024 * 1024,) * 2)
    return fn


def _drain(proc, wall, out_cap):
    """
    Bounded read. At most out_cap+1 bytes are ever pulled from each pipe; the
    moment either exceeds the cap we stop reading and report it, so a flood can
    never grow the parent's memory. Returns (out, err, truncated, timed_out).
    """
    sel = selectors.DefaultSelector()
    bufs = {}
    for key, stream in (("out", proc.stdout), ("err", proc.stderr)):
        os.set_blocking(stream.fileno(), False)
        sel.register(stream, selectors.EVENT_READ, key)
        bufs[key] = bytearray()

    truncated, deadline, open_streams = False, time.monotonic() + wall, 2
    while open_streams and not truncated:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return bytes(bufs["out"]), bytes(bufs["err"]), truncated, True
        for skey, _ in sel.select(timeout=min(remaining, 0.25)):
            k = skey.data
            try:
                chunk = skey.fileobj.read(65536)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                chunk = b""
            if not chunk:
                sel.unregister(skey.fileobj)
                open_streams -= 1
                continue
            bufs[k].extend(chunk)
            if len(bufs[k]) > out_cap:
                truncated = True
                break
        if proc.poll() is not None and open_streams == 0:
            break
    return bytes(bufs["out"]), bytes(bufs["err"]), truncated, False


def run(code, wall=WALL_DEFAULT, mem_mb=MEM_MB_DEFAULT, pids=PIDS_DEFAULT,
        out_cap=OUT_CAP):
    if os.geteuid() != 0:
        raise BoxError("box.py must run as root (it creates cgroups and mount namespaces)")
    if not isinstance(code, str):
        raise BoxError("code must be str")
    blob = code.encode("utf-8", "surrogateescape")
    if len(blob) > CODE_MAX:
        return _result("rejected", -1, b"", b"", False, False,
                       f"code exceeds {CODE_MAX} bytes")
    if not os.path.exists(RUNNER):
        raise BoxError(f"inner runner missing: {RUNNER}")

    cgroup_prepare()
    cgroup_sweep()          # clear any empty stragglers before adding one more
    cid = secrets.token_hex(8)
    cgroup_dir, cpu_limited = cgroup_make(cid, mem_mb, pids)

    # Root-owned, non-writable parent. 0711 lets the dropped uid traverse to a
    # known filename without being able to list or create siblings.
    os.makedirs(RUN_PARENT, exist_ok=True)
    os.chmod(RUN_PARENT, 0o711)
    os.chown(RUN_PARENT, 0, 0)
    rundir = os.path.join(RUN_PARENT, cid)
    os.mkdir(rundir, 0o711)
    os.chown(rundir, 0, 0)
    payload = os.path.join(rundir, "payload.py")
    fd = os.open(payload, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o444)
    with os.fdopen(fd, "wb") as f:
        f.write(blob)

    prelude = MOUNT_PRELUDE.replace("__TMPFS__", str(TMPFS_MB))
    cmd = [
        "/usr/bin/unshare",
        "--fork", "--kill-child=SIGKILL",
        "--pid", "--net", "--ipc", "--uts", "--mount", "--mount-proc",
        "/bin/sh", "-c", prelude, "sh",
        "/usr/bin/setpriv",
        f"--reuid={NOBODY_UID}", f"--regid={NOBODY_GID}", "--clear-groups",
        "--nnp", "--bounding-set=-all", "--inh-caps=-all",
        "/usr/bin/python3", "-I", "-B", RUNNER, payload,
    ]

    started = time.monotonic()
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        close_fds=True, pass_fds=(),
        env={"PATH": "/usr/bin:/bin", "HOME": "/tmp", "TMPDIR": "/tmp",
             "LC_ALL": "C", "PYTHONDONTWRITEBYTECODE": "1"},
        preexec_fn=_preexec(cgroup_dir),
        cwd="/",
    )

    out, err, truncated, timed_out = _drain(proc, wall, out_cap)
    killed = timed_out or truncated
    if killed:
        cgroup_kill(cgroup_dir)
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        cgroup_kill(cgroup_dir)
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
    # Always kill: a detached grandchild can outlive the direct child, and the
    # cgroup is the only thing that sees all of them.
    drained = cgroup_kill(cgroup_dir)
    elapsed = time.monotonic() - started

    for s in (proc.stdout, proc.stderr):
        try:
            s.close()
        except Exception:
            pass

    # Clean up by handle, not by re-walking an attacker-influenced pathname.
    try:
        shutil.rmtree(rundir, ignore_errors=True)
    except Exception:
        pass
    cgroup_remove(cgroup_dir)

    status = "ok"
    if timed_out:
        status = "timeout"
    elif truncated:
        status = "output_truncated"
    elif proc.returncode != 0:
        status = "nonzero_exit"
    return _result(status, proc.returncode, out[:out_cap], err[:out_cap],
                   truncated, timed_out, None, elapsed, drained, cpu_limited)


def _result(status, rc, out, err, truncated, timed_out, note=None,
            elapsed=0.0, drained=True, cpu_limited=None):
    return {
        "status": status,
        "exit": rc,
        # Decoded lossily and on purpose: this is hostile bytes, and it exists to
        # be counted and logged, not trusted. Control/bidi scrubbing happens at
        # the surfacing layer, not here.
        "stdout": out.decode("utf-8", "replace"),
        "stderr": err.decode("utf-8", "replace"),
        "stdout_bytes": len(out),
        "stderr_bytes": len(err),
        "truncated": truncated,
        "timed_out": timed_out,
        "elapsed": round(elapsed, 3),
        "cgroup_drained": drained,
        # False means the cpu controller was not delegated and the run was
        # bounded by wall clock alone. Surfaced, never swallowed.
        "cpu_limited": cpu_limited,
        "note": note,
    }


# --------------------------------------------------------------- self-test --

SELF_TESTS = [
    ("hello",        "print('hello')",                                    "ok"),
    ("nonzero",      "import sys; sys.exit(3)",                           "nonzero_exit"),
    ("wall",         "while True: pass",                                  "timeout"),
    ("flood",        "import sys\nwhile True: sys.stdout.write('A'*4096)", "output_truncated"),
    ("no_net",       "import socket; socket.socket()",                    "nonzero_exit"),
    ("ro_root",      "open('/etc/passwd_x','w')",                         "nonzero_exit"),
    ("root_hidden",  "import os; print(os.listdir('/root'))",             "nonzero_exit"),
    ("tmp_writable", "open('/tmp/x','w').write('ok'); print('wrote')",     "ok"),
]


def self_test():
    ok = True
    for name, code, expect in SELF_TESTS:
        r = run(code, wall=5)
        good = r["status"] == expect
        ok = ok and good
        print(f"{'PASS' if good else 'FAIL'}  {name:14s} got={r['status']:17s} "
              f"expect={expect:17s} exit={r['exit']} "
              f"out={r['stdout'][:40]!r} err={r['stderr'][:60]!r}")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code-file")
    ap.add_argument("--wall", type=int, default=WALL_DEFAULT)
    ap.add_argument("--mem-mb", type=int, default=MEM_MB_DEFAULT)
    ap.add_argument("--pids", type=int, default=PIDS_DEFAULT)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        sys.exit(0 if self_test() else 1)
    if not a.code_file:
        ap.error("--code-file or --self-test required")
    with open(a.code_file, "r", encoding="utf-8", errors="surrogateescape") as f:
        code = f.read()
    print(json.dumps(run(code, a.wall, a.mem_mb, a.pids), indent=2))


if __name__ == "__main__":
    main()
