#!/usr/bin/env python3
"""
eol_poold.py — the executor VM pool manager. HOLDS THE LXD CREDENTIAL AND PARSES
NOTHING A CITIZEN EVER TOUCHED.

The split from eol-execd is the point of this file, not an implementation detail.
eol-execd is the process an attacker reaches first, so it holds no credential and
cannot drive LXD. This process CAN drive LXD — launch and delete VMs — so it is
kept away from hostile input entirely: it speaks a FIXED 24-BYTE BINARY protocol
with no strings, no JSON, and no variable-length field. There is nothing here for
a crafted payload to influence. The only bytes it reads are an opcode and an
opaque handle it minted itself.

DISPOSABILITY. One VM per job, launched `--ephemeral` so LXD deletes it on stop.
That is what makes "run N cannot leave state for run N+1" structural rather than a
promise: no filesystem carry-over, no surviving process, no poisoned bytecode, no
modified module. A ~20s launch against a ~240s citizen turn cadence is affordable,
so the strong property is also the cheap one.

The executor is launched from the `eol-exec-base` image with NO NIC AT ALL and
`security.devlxd=false`. Both are asserted after launch, not assumed — a VM that
comes up with a network device is condemned rather than used.
"""

import os, socket, struct, subprocess, sys, threading, time

SOCK_PATH = "/run/eol-exec/poold.sock"
IMAGE = "eol-exec-base"
# The NIC-less profile: a root disk and NOTHING else. This is what makes an
# executor network-free — there is no nic device to attach. assert_sealed()
# re-checks it per launch rather than trusting the profile to still be right.
PROFILE = "eol-exec"
VSOCK_AGENT_PORT = 621
LAUNCH_TIMEOUT = 90           # boot -> agent answering on vsock
MAX_LIVE = 2                  # concurrent executors; a hard ceiling on host load

# Fixed binary protocol. No strings in either direction — nothing to parse.
REQ = ">B7xQ8x"               # op, handle              = 24 bytes
RES = ">B3xIQ8x"              # status, cid, handle     = 24 bytes
OP_ACQUIRE, OP_RELEASE = 1, 2
ST_OK, ST_BUSY, ST_FAIL = 0, 1, 2

_lock = threading.Lock()
_live = {}                    # handle -> instance name


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def lxc(*args, timeout=120):
    return subprocess.run(["lxc", *args], capture_output=True, text=True,
                          timeout=timeout)


def vsock_ready(cid, timeout=LAUNCH_TIMEOUT):
    """Poll until the executor's agent answers. This is the real readiness signal
    — `lxc list` saying RUNNING only means the hypervisor started it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
        s.settimeout(2)
        try:
            s.connect((cid, VSOCK_AGENT_PORT))
            s.close()
            return True
        except Exception:
            try:
                s.close()
            except Exception:
                pass
            time.sleep(1)
    return False


def assert_sealed(name):
    """
    Verify the executor is actually secret-free and network-free BEFORE any job
    reaches it. Asserted, never assumed: a VM that comes up with a NIC is a
    configuration accident that would silently hand hostile code the internet.
    """
    r = lxc("config", "show", name, "--expanded")
    if r.returncode != 0:
        return False, "config show failed"
    cfg = r.stdout
    # A nic device of any kind disqualifies it.
    if "type: nic" in cfg:
        return False, "executor has a NIC device"
    if "security.devlxd: \"false\"" not in cfg and "security.devlxd: 'false'" not in cfg:
        return False, "security.devlxd is not false"
    return True, None


def acquire(handle):
    name = f"eol-exec-{handle:016x}"
    with _lock:
        if len(_live) >= MAX_LIVE:
            return ST_BUSY, 0
    r = lxc("launch", IMAGE, name, "--vm", "--ephemeral",
            "--profile", PROFILE,
            "-c", "security.devlxd=false",
            "-c", "limits.memory=2GiB", "-c", "limits.cpu=2")
    if r.returncode != 0:
        log(f"launch failed: {r.stderr.strip()[:200]}")
        return ST_FAIL, 0

    ok, why = assert_sealed(name)
    if not ok:
        log(f"CONDEMNING {name}: {why}")
        lxc("delete", name, "--force")
        return ST_FAIL, 0

    r = lxc("config", "get", name, "volatile.vsock_id")
    try:
        cid = int(r.stdout.strip())
    except Exception:
        log(f"no vsock id for {name}")
        lxc("delete", name, "--force")
        return ST_FAIL, 0

    if not vsock_ready(cid):
        log(f"{name} never answered on vsock :{VSOCK_AGENT_PORT}")
        lxc("delete", name, "--force")
        return ST_FAIL, 0

    with _lock:
        _live[handle] = name
    log(f"acquired {name} cid={cid}")
    return ST_OK, cid


def release(handle):
    with _lock:
        name = _live.pop(handle, None)
    if not name:
        return ST_OK, 0
    # --ephemeral means stop deletes it; --force is correct HERE and only here:
    # an executor holds no arena seat, so there is no graceful-release invariant
    # to honour. (Never do this to a citizen VM — that orphans its seat.)
    lxc("delete", name, "--force")
    log(f"released {name}")
    return ST_OK, 0


def serve(sock_path=SOCK_PATH):
    os.makedirs(os.path.dirname(sock_path), exist_ok=True)
    if os.path.exists(sock_path):
        os.unlink(sock_path)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    srv.bind(sock_path)
    # Only eol-execd may ask for an executor. Ownership IS the gate — there is no
    # authentication inside the protocol because there is nothing in the protocol to
    # authenticate. root:eol-execd 0660 means the unprivileged broker can ask, and
    # nothing else on the host can. (Leaving it root:root would silently break the
    # whole path the moment execd stopped running as root.)
    try:
        import grp
        os.chown(sock_path, 0, grp.getgrnam("eol-execd").gr_gid)
    except Exception as e:
        log(f"WARNING: could not set socket group to eol-execd ({type(e).__name__}); "
            f"the broker may be unable to connect")
    os.chmod(sock_path, 0o660)
    srv.listen(8)
    log(f"eol-poold listening {sock_path} (image={IMAGE} profile={PROFILE})")

    while True:
        try:
            conn, _ = srv.accept()
        except Exception as e:
            log(f"accept: {type(e).__name__}")
            time.sleep(0.5)
            continue
        threading.Thread(target=_handle, args=(conn,), daemon=True).start()


def _handle(conn):
    try:
        conn.settimeout(LAUNCH_TIMEOUT + 30)
        data = conn.recv(64)
        if len(data) != struct.calcsize(REQ):
            return                      # wrong size is not a request; say nothing
        op, handle = struct.unpack(REQ, data)
        if op == OP_ACQUIRE:
            st, cid = acquire(handle)
        elif op == OP_RELEASE:
            st, cid = release(handle)
        else:
            st, cid = ST_FAIL, 0
        conn.sendall(struct.pack(RES, st, cid, handle))
    except Exception as e:
        log(f"handler: {type(e).__name__}: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ------------------------------------------------------------------ client --

def client_acquire(sock_path=SOCK_PATH, timeout=LAUNCH_TIMEOUT + 30):
    """Used by eol-execd. Mints its own handle; poold never sees citizen data."""
    handle = int.from_bytes(os.urandom(8), "big")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    s.settimeout(timeout)
    try:
        s.connect(sock_path)
        s.sendall(struct.pack(REQ, OP_ACQUIRE, handle))
        st, cid, h = struct.unpack(RES, s.recv(64))
        return (cid if st == ST_OK else None), handle
    finally:
        s.close()


def client_release(handle, sock_path=SOCK_PATH, timeout=60):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    s.settimeout(timeout)
    try:
        s.connect(sock_path)
        s.sendall(struct.pack(REQ, OP_RELEASE, handle))
        s.recv(64)
    except Exception:
        pass
    finally:
        try:
            s.close()
        except Exception:
            pass


def reap_orphans():
    """Delete any executor left behind by a crash. Ephemeral VMs normally vanish
    on stop, but a poold killed mid-job leaves a RUNNING one with no owner."""
    r = lxc("list", "eol-exec-", "--format", "csv", "-c", "n")
    names = [n.strip() for n in r.stdout.splitlines() if n.strip().startswith("eol-exec-")]
    for n in names:
        log(f"reaping orphan executor {n}")
        lxc("delete", n, "--force")
    return len(names)


if __name__ == "__main__":
    if "--reap" in sys.argv:
        print(f"reaped {reap_orphans()}")
        sys.exit(0)
    reap_orphans()
    serve()
