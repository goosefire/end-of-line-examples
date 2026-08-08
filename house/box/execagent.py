#!/usr/bin/env python3
"""
execagent.py — runs INSIDE the secret-free executor VM. Serves exactly ONE job,
then exits.

Position in the design: this is the far side of trust boundary #2. It listens on
AF_VSOCK port 621 and accepts a connection ONLY from the host (CID 2). It holds no
credential, has no NIC, and the VM it runs in is destroyed after the job — so its
own compromise is the accepted outcome, not a failure.

WHY ONE JOB. Disposability is the only thing that stops run N leaving state for
run N+1 (files, a surviving process, a poisoned .pyc, a modified module). That is
a citizen-to-citizen channel that bypasses the arena entirely and would be visible
in no room and no log. Serving one job and exiting makes "the VM is destroyed
between jobs" structural rather than a promise the pool manager has to keep.

WHY THE PEER CID CHECK COMES FIRST. vsock is an unfilterable bus: it has no
netfilter hooks (`nft list ruleset | grep -c vsock` == 0), and CID_ANY accepts
from every VM on the host, including libvirt guests. accept() stamps the peer CID
in the kernel — a caller can neither choose nor observe its own — so it is the one
identity here worth anything. It is checked BEFORE a single byte is read, so a
stranger's bytes never reach the parser at all.
"""

import json, os, socket, struct, subprocess, sys, time

VSOCK_PORT = 621
HOST_CID = 2                    # socket.VMADDR_CID_HOST
MAX_FRAME = 64 * 1024           # hard cap on an inbound frame
ACCEPT_TIMEOUT = 900            # give up if no job ever arrives (VM gets reaped)
READ_TIMEOUT = 30
BOX = "/opt/box/box.py"


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def read_frame(conn, cap=MAX_FRAME):
    """4-byte big-endian length + body. Bounded: the length is validated against
    the cap BEFORE any body byte is read, so a hostile length cannot make us
    allocate."""
    hdr = b""
    while len(hdr) < 4:
        chunk = conn.recv(4 - len(hdr))
        if not chunk:
            raise EOFError("short header")
        hdr += chunk
    n = struct.unpack(">I", hdr)[0]
    if n == 0 or n > cap:
        raise ValueError(f"frame length {n} outside 1..{cap}")
    body = b""
    while len(body) < n:
        chunk = conn.recv(min(65536, n - len(body)))
        if not chunk:
            raise EOFError("short body")
        body += chunk
    return body


def write_frame(conn, obj):
    blob = json.dumps(obj).encode()
    conn.sendall(struct.pack(">I", len(blob)) + blob)


def run_job(job):
    code = job.get("code")
    if not isinstance(code, str) or not code:
        return {"status": "rejected", "note": "code must be a non-empty string"}
    wall = job.get("wall")
    wall = wall if isinstance(wall, int) and 1 <= wall <= 30 else 10

    # Hand the code to the box on a pipe. Never write it to a path the payload
    # could later influence, and never pass it as an argv the process table shows.
    p = subprocess.run(
        [sys.executable, BOX, "--code-file", "/dev/stdin", "--wall", str(wall)],
        input=code, capture_output=True, text=True, timeout=wall + 45)
    try:
        return json.loads(p.stdout)
    except Exception:
        # The box failing to produce a result is itself a result. Never surface
        # the raw stderr blindly — bound it.
        return {"status": "box_error", "exit": p.returncode,
                "stderr": p.stderr[-500:], "stdout": "", "truncated": False,
                "timed_out": False}


def main():
    srv = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((socket.VMADDR_CID_ANY, VSOCK_PORT))
    srv.listen(1)
    srv.settimeout(ACCEPT_TIMEOUT)
    log(f"executor agent listening vsock :{VSOCK_PORT} (one job, then exit)")

    while True:
        try:
            conn, (peer_cid, _peer_port) = srv.accept()
        except socket.timeout:
            log("no job arrived before the accept timeout; exiting")
            return 0

        # Identity BEFORE bytes. Anything that is not the host is closed unread.
        if peer_cid != HOST_CID:
            log(f"refused connection from CID {peer_cid} (only host CID {HOST_CID})")
            conn.close()
            continue

        conn.settimeout(READ_TIMEOUT)
        try:
            job = json.loads(read_frame(conn).decode("utf-8"))
            if not isinstance(job, dict):
                raise ValueError("job must be an object")
        except Exception as e:
            log(f"bad frame: {type(e).__name__}: {e}")
            try:
                write_frame(conn, {"status": "bad_request", "note": str(e)[:200]})
            except Exception:
                pass
            conn.close()
            continue

        log(f"running job id={job.get('id')!r} bytes={len(job.get('code') or '')}")
        started = time.monotonic()
        try:
            result = run_job(job)
        except subprocess.TimeoutExpired:
            result = {"status": "box_hung", "note": "box exceeded its own deadline"}
        except Exception as e:
            result = {"status": "box_error", "note": f"{type(e).__name__}: {e}"[:200]}
        result["id"] = job.get("id")
        result["agent_elapsed"] = round(time.monotonic() - started, 3)

        try:
            write_frame(conn, result)
        except Exception as e:
            log(f"reply failed: {e}")
        conn.close()
        log("job complete; exiting so the VM can be destroyed")
        return 0


if __name__ == "__main__":
    sys.exit(main())
