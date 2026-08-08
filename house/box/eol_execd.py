#!/usr/bin/env python3
"""
eol_execd.py — the host-side broker for `run_code`. THE ONLY HOST COMPONENT THAT
PARSES A CITIZEN'S BYTES.

Everything about this file is shaped by one rule: it is the process an attacker
reaches first, so it must be the process worth reaching least. It therefore holds
NO credential — no MiniMax key, no arena seat token, no LXD client certificate —
runs as a dedicated uid with no supplementary groups, and cannot drive LXD. VM
lifecycle belongs to a separate service (eol-poold) that never sees a citizen byte.

That split is not decoration. An earlier design for this project put a key inside a
hand-rolled proxy running as an account with `NOPASSWD: ALL` and membership in
`lxd` — RCE there would have been passwordless root plus `lxc exec` into every
citizen. Adversarial review killed it. This file is the shape that survived: the
hostile-byte parser is the least privileged thing on the host.

IDENTITY. A citizen is identified by the AF_VSOCK peer CID stamped by the kernel at
accept(). It is unforgeable — measured: a guest cannot even observe its own CID
(getsockname returns CID_ANY) — so there is no channel credential to place inside a
citizen VM, and therefore no cleartext credential hop of the kind that sank the
broker design. The CID is checked BEFORE a single byte is read, so an unknown
caller's bytes never reach the parser.

FAIL CLOSED. A missing, unreadable, or malformed CID map means every request is
refused. "Absent means allow" is fail-open to a failed provisioning step or a
rollback, which is the wrong default for the dangerous tier.
"""

import json, os, socket, struct, sys, threading, time

import eol_poold as poold

VSOCK_PORT = 620
EXEC_PORT = 621
MAX_FRAME = 64 * 1024
CODE_MAX = 8192
ACCEPT_BACKLOG = 16
READ_TIMEOUT = 20            # a citizen has this long to deliver its frame
EXEC_TIMEOUT = 120           # end-to-end budget for the executor round trip
CIDMAP = "/etc/eol-exec/cids.json"

_lock = threading.Lock()
_inflight = set()            # slots with a job in flight — one each, no queueing
_last_refusal = {}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ------------------------------------------------------------- identity --

def load_cidmap(path=CIDMAP):
    """
    {"<cid>": "<slot>"} -> {int: str}. Any defect returns an EMPTY map, which
    refuses everything. Mirrors load_redlight()'s strict posture from step 3a:
    for the dangerous tier, unparseable and absent both mean "no".
    """
    try:
        st = os.stat(path)
        if st.st_size > 64 * 1024:
            return {}, "cidmap oversized"
        with open(path) as f:
            doc = json.load(f)
        if not isinstance(doc, dict):
            return {}, "cidmap not an object"
        out = {}
        for k, v in doc.items():
            if not isinstance(v, str) or not v:
                return {}, f"cidmap slot for {k!r} not a non-empty string"
            out[int(k)] = v
        return out, None
    except FileNotFoundError:
        return {}, "cidmap absent"
    except Exception as e:
        return {}, f"cidmap unreadable: {type(e).__name__}"


# ---------------------------------------------------------------- wire --

def read_frame(conn, cap=MAX_FRAME):
    """Length is validated against the cap BEFORE any body byte is read, so a
    hostile length never becomes an allocation."""
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


# ------------------------------------------------------------ executor --

def dispatch_to_executor(exec_cid, job, timeout=EXEC_TIMEOUT):
    """
    Forward one job to a never-yet-used executor and read one bounded reply.
    The reply is hostile too — the executor is assumed compromised the moment it
    runs a job — so it gets exactly the same framing discipline as the request.
    """
    s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((exec_cid, EXEC_PORT))
        write_frame(s, job)
        return json.loads(read_frame(s).decode("utf-8"))
    finally:
        try:
            s.close()
        except Exception:
            pass


# --------------------------------------------------------------- serve --

def handle(conn, peer_cid, get_executor, pooled=False):
    slot = None
    handle_id = None      # outer scope: the executor must be released on EVERY
                          # exit path, including an exception mid-dispatch. A
                          # leaked ephemeral VM is a live machine nobody owns.
    try:
        cidmap, err = load_cidmap()
        if err:
            # Log at most once a minute per reason: a broken map otherwise floods
            # the log at connection rate, which is itself a denial of service.
            now = time.time()
            if now - _last_refusal.get(err, 0) > 60:
                _last_refusal[err] = now
                log(f"REFUSING ALL: {err}")
            return
        slot = cidmap.get(peer_cid)
        if slot is None:
            now = time.time()
            key = f"unknown-cid-{peer_cid}"
            if now - _last_refusal.get(key, 0) > 60:
                _last_refusal[key] = now
                log(f"refused unknown CID {peer_cid}")
            return

        with _lock:
            if slot in _inflight:
                # No queueing: a citizen gets one job at a time. Queueing would
                # let one seat consume the pool and starve the others.
                busy = True
            else:
                _inflight.add(slot)
                busy = False
        if busy:
            log(f"{slot}: refused, already in flight")
            write_frame(conn, {"status": "busy", "note": "one job at a time"})
            return

        try:
            conn.settimeout(READ_TIMEOUT)
            req = json.loads(read_frame(conn).decode("utf-8"))
            if not isinstance(req, dict):
                raise ValueError("request must be an object")
            code = req.get("code")
            if not isinstance(code, str) or not code:
                raise ValueError("code must be a non-empty string")
            if len(code.encode()) > CODE_MAX:
                raise ValueError(f"code exceeds {CODE_MAX} bytes")

            # An identity ASSERTED in the payload is cross-checked but never
            # authoritative — it catches a mis-generated map, nothing more.
            claimed = req.get("slot")
            if isinstance(claimed, str) and claimed != slot:
                log(f"SLOT MISMATCH: CID {peer_cid} is {slot!r} but claimed {claimed!r}")
                write_frame(conn, {"status": "refused", "note": "identity mismatch"})
                return

            # One FRESH ephemeral executor per job. `handle_id` lives in the
            # OUTER scope so the finally at the bottom of handle() destroys it on
            # every path — success, bad reply, timeout, or an exception in
            # dispatch_to_executor.
            if pooled:
                exec_cid, handle_id = poold.client_acquire()
            else:
                exec_cid = get_executor(slot)
            if exec_cid is None:
                write_frame(conn, {"status": "unavailable",
                                   "note": "no executor available"})
                return

            job = {"id": f"{slot}-{int(time.time()*1000)}",
                   "code": code,
                   "wall": req.get("wall") if isinstance(req.get("wall"), int) else 10}
            log(f"{slot}: dispatching {len(code)}B to executor CID {exec_cid}")
            started = time.monotonic()
            try:
                result = dispatch_to_executor(exec_cid, job)
            except Exception as e:
                log(f"{slot}: executor round trip failed: {type(e).__name__}: {e}")
                result = {"status": "executor_error",
                          "note": f"{type(e).__name__}"}
            result["elapsed"] = round(time.monotonic() - started, 3)
            result.pop("slot", None)
            write_frame(conn, result)
            log(f"{slot}: {result.get('status')} in {result['elapsed']}s")
        except Exception as e:
            log(f"{slot or peer_cid}: bad request: {type(e).__name__}: {e}")
            try:
                write_frame(conn, {"status": "bad_request",
                                   "note": str(e)[:200]})
            except Exception:
                pass
    finally:
        # Destroy the executor before anything else. This runs on every path,
        # so a job that timed out or blew up still takes its VM with it.
        if handle_id is not None:
            try:
                poold.client_release(handle_id)
            except Exception as e:
                log(f"release failed for handle {handle_id:016x}: {type(e).__name__} "
                    f"— poold's orphan reaper is the backstop")
        if slot:
            with _lock:
                _inflight.discard(slot)
        try:
            conn.close()
        except Exception:
            pass


def serve(get_executor, port=VSOCK_PORT, listen_fd=None, pooled=False):
    if listen_fd is not None:
        # systemd socket activation: root binds the privileged vsock port and we
        # inherit the fd, so this process never needs the privilege to bind it.
        srv = socket.fromfd(listen_fd, socket.AF_VSOCK, socket.SOCK_STREAM)
    else:
        srv = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((socket.VMADDR_CID_ANY, port))
        srv.listen(ACCEPT_BACKLOG)
    log(f"eol-execd listening vsock :{port} as uid {os.geteuid()}")

    while True:
        try:
            conn, (peer_cid, _p) = srv.accept()
        except Exception as e:
            log(f"accept failed: {type(e).__name__}: {e}")
            time.sleep(0.5)
            continue
        t = threading.Thread(target=handle,
                             args=(conn, peer_cid, get_executor, pooled),
                             daemon=True)
        t.start()


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--executor-cid", type=int, default=None,
                    help="pin a fixed executor (prototype/debug only)")
    ap.add_argument("--pool", action="store_true",
                    help="acquire a FRESH ephemeral executor per job from eol-poold "
                         "(the real mode: one VM per job, destroyed after)")
    ap.add_argument("--port", type=int, default=VSOCK_PORT)
    a = ap.parse_args()
    # systemd socket activation: root binds vsock:620 (a privileged port) and hands
    # us the listening fd, so THIS process — the one that parses hostile bytes —
    # never needs the privilege to bind it and can run as a uid with no groups.
    # LISTEN_FDS is set by systemd; fd 3 is the first passed socket.
    listen_fd = None
    if os.environ.get("LISTEN_FDS") and os.environ.get("LISTEN_PID") == str(os.getpid()):
        listen_fd = 3
        log("using socket activation (inherited fd 3)")
    if os.geteuid() == 0:
        log("WARNING: running as root. The hardened unit runs as a dedicated uid "
            "with no groups — this process is the one that parses hostile bytes.")
    if a.pool:
        serve(None, port=a.port, pooled=True, listen_fd=listen_fd)
    elif a.executor_cid:
        serve(lambda slot: a.executor_cid, port=a.port)
    else:
        ap.error("one of --pool or --executor-cid is required")


if __name__ == "__main__":
    main()
