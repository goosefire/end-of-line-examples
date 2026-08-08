#!/usr/bin/env python3
"""
_boxrun.py — the innermost stage of the box. Installs a seccomp-bpf filter, then
execs the payload.

WHY IT IS A SEPARATE STAGE. The filter has to be installed after `unshare` and
`setpriv` have done their work (both legitimately need syscalls we deny — the
whole point of unshare is unshare(2)) and before a single instruction of the
payload runs. A seccomp filter survives execve and is inherited by every child,
so installing it here covers the payload and anything it spawns, including a
native ELF it writes and runs.

WHAT IT CLOSES that the network namespace does not. "No route to 1.1.1.1" is not
"no communication channel":
  - AF_VSOCK is a hypervisor channel independent of network configuration and
    invisible to the host firewall. In an LXD guest this is not theoretical:
    devlxd rides vsock and answers to the root LXD daemon.
  - AF_NETLINK talks to the kernel, not the network.
  - setns/unshare/mount would let the payload construct its own namespaces or
    re-mount what we carefully sealed.
  - ptrace / process_vm_* reach sideways into other processes sharing the uid.
  - bpf, perf_event_open, and module loading are kernel attack surface.

This is a DENYLIST, and that is a deliberate, stated compromise. An allowlist is
strictly stronger but has to enumerate everything glibc and CPython touch, and a
wrong entry is a mysterious crash rather than a clean denial. The denylist is
belt-and-suspenders on top of the real boundary (the executor VM); it is not the
boundary itself, and it must never be described as one.
"""

import ctypes, os, struct, sys

# --- BPF ---------------------------------------------------------------------
BPF_LD, BPF_W, BPF_ABS = 0x00, 0x00, 0x20
BPF_JMP, BPF_JEQ, BPF_K = 0x05, 0x10, 0x00
BPF_RET = 0x06

SECCOMP_RET_KILL_PROCESS = 0x80000000
SECCOMP_RET_ERRNO = 0x00050000
SECCOMP_RET_ALLOW = 0x7FFF0000

AUDIT_ARCH_X86_64 = 0xC000003E
AUDIT_ARCH_AARCH64 = 0xC00000B7

PR_SET_NO_NEW_PRIVS = 38
PR_SET_SECCOMP = 22
SECCOMP_MODE_FILTER = 2

# offsetof(struct seccomp_data, nr) == 0, .arch == 4
OFF_NR, OFF_ARCH = 0, 4

# x86_64 syscall numbers. Deliberately explicit rather than imported: this list
# IS the policy and it should be readable as such.
DENY_X86_64 = {
    41: "socket", 53: "socketpair",          # every address family incl. AF_VSOCK
    101: "ptrace",
    155: "pivot_root",
    165: "mount", 166: "umount2",
    175: "init_module", 176: "delete_module", 313: "finit_module",
    246: "kexec_load", 320: "kexec_file_load",
    248: "add_key", 249: "request_key", 250: "keyctl",
    272: "unshare",
    298: "perf_event_open",
    303: "name_to_handle_at", 304: "open_by_handle_at",
    308: "setns",
    310: "process_vm_readv", 311: "process_vm_writev",
    321: "bpf",
    323: "userfaultfd",

    # --- io_uring: a COMPLETE seccomp bypass, not merely another syscall. ---
    # io_uring submits operations that are executed by kernel worker threads,
    # which do NOT pass through this filter. IORING_OP_SOCKET / OP_CONNECT /
    # OP_OPENAT therefore reach the kernel even though socket(2) and openat(2)
    # are denied above — including AF_VSOCK, the one channel a network namespace
    # cannot close. MEASURED in this box before the fix: io_uring_setup returned
    # fd=3 while every net/* test still reported "blocked", which is the most
    # dangerous shape a control can have — an assurance that is false.
    425: "io_uring_setup", 426: "io_uring_enter", 427: "io_uring_register",

    # --- the NEW mount API: blocking mount(2) does not block mounting. ---
    428: "open_tree", 429: "move_mount", 430: "fsopen", 431: "fsconfig",
    432: "fsmount", 433: "fspick", 442: "mount_setattr",

    # --- pidfd: reaching sideways into other processes / stealing their fds. ---
    434: "pidfd_open", 438: "pidfd_getfd",
}
# clone3 gets ENOSYS rather than EPERM so glibc cleanly falls back to clone(2)
# instead of failing outright; blocking it with EPERM breaks thread creation.
CLONE3_X86_64 = 435

# Plain clone(2) is NOT in the denylist — a flat deny would break fork() and
# every thread. But clone(CLONE_NEWUSER) creates a user namespace, which is the
# classic route to capabilities we just dropped, and denying unshare(2)+clone3
# while leaving clone(2) open closes two of the three doors. So clone is filtered
# on its FLAGS argument instead.
CLONE_X86_64 = 56
CLONE_NEWUSER = 0x10000000
# offsetof(struct seccomp_data, args[0]) == 16; BPF is 32-bit so this loads the
# low word of the 64-bit flags, which is where CLONE_NEWUSER lives.
OFF_ARG0_LO = 16

BPF_ALU, BPF_AND = 0x04, 0x50


class SockFilter(ctypes.Structure):
    _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte),
                ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint)]


class SockFprog(ctypes.Structure):
    # Laid out by ctypes rather than hand-packed: on x86_64 the pointer is
    # 8-byte aligned so the struct has 6 bytes of padding after `len`, and
    # getting that wrong yields a silent EFAULT.
    _fields_ = [("len", ctypes.c_ushort),
                ("filter", ctypes.POINTER(SockFilter))]


def build_filter(arch, deny, clone3_nr):
    insns = []

    def emit(code, jt, jf, k):
        insns.append(SockFilter(code, jt, jf, k))

    # Refuse to run on an architecture whose syscall numbers we did not encode —
    # a filter written for the wrong ABI is worse than no filter, because the
    # numbers mean something else entirely.
    emit(BPF_LD | BPF_W | BPF_ABS, 0, 0, OFF_ARCH)
    emit(BPF_JMP | BPF_JEQ | BPF_K, 1, 0, arch)
    emit(BPF_RET | BPF_K, 0, 0, SECCOMP_RET_KILL_PROCESS)
    emit(BPF_LD | BPF_W | BPF_ABS, 0, 0, OFF_NR)
    for nr in sorted(deny):
        # if nr matches, fall through to the RET; otherwise skip it
        emit(BPF_JMP | BPF_JEQ | BPF_K, 0, 1, nr)
        emit(BPF_RET | BPF_K, 0, 0, SECCOMP_RET_ERRNO | (1 & 0xFFFF))    # EPERM
    if clone3_nr is not None:
        emit(BPF_JMP | BPF_JEQ | BPF_K, 0, 1, clone3_nr)
        emit(BPF_RET | BPF_K, 0, 0, SECCOMP_RET_ERRNO | (38 & 0xFFFF))   # ENOSYS

    # clone(2) filtered on FLAGS, not blanket-denied (a flat deny kills fork()
    # and every thread). Laid out so both "not clone" and "clone without
    # CLONE_NEWUSER" fall through to ALLOW, and only the NEWUSER case returns
    # EPERM. Jump targets are relative to the NEXT instruction (pc + 1 + off):
    #   +0  not clone      -> jf=4 -> +5  ALLOW
    #   +1  load args[0] low word
    #   +2  AND CLONE_NEWUSER
    #   +3  result == 0    -> jt=1 -> +5  ALLOW
    #   +4  RET EPERM
    #   +5  RET ALLOW
    emit(BPF_JMP | BPF_JEQ | BPF_K, 0, 4, CLONE_X86_64)
    emit(BPF_LD | BPF_W | BPF_ABS, 0, 0, OFF_ARG0_LO)
    emit(BPF_ALU | BPF_AND | BPF_K, 0, 0, CLONE_NEWUSER)
    emit(BPF_JMP | BPF_JEQ | BPF_K, 1, 0, 0)
    emit(BPF_RET | BPF_K, 0, 0, SECCOMP_RET_ERRNO | (1 & 0xFFFF))        # EPERM

    emit(BPF_RET | BPF_K, 0, 0, SECCOMP_RET_ALLOW)

    arr = (SockFilter * len(insns))(*insns)
    return arr


def install():
    machine = os.uname().machine
    if machine == "x86_64":
        arch, deny, clone3 = AUDIT_ARCH_X86_64, DENY_X86_64, CLONE3_X86_64
    else:
        # Fail CLOSED. Running the payload with no filter because we did not
        # recognise the arch is exactly the silent downgrade this box exists to
        # avoid.
        raise SystemExit(f"box: no seccomp policy for arch {machine}; refusing")

    arr = build_filter(arch, deny, clone3)
    fprog = SockFprog(len(arr), ctypes.cast(arr, ctypes.POINTER(SockFilter)))

    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    # argtypes MUST be declared. ctypes defaults an unannotated Python int
    # argument to C int (32-bit), which truncates a 64-bit pointer and produces
    # a mystifying EFAULT from prctl — measured, not theorised.
    libc.prctl.restype = ctypes.c_int
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p,
                           ctypes.c_ulong, ctypes.c_ulong]

    # no_new_privs is a hard prerequisite for an unprivileged seccomp filter.
    # setpriv --nnp already set it; setting it again is free and makes this
    # stage correct on its own.
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, None, 0, 0) != 0:
        raise SystemExit("box: PR_SET_NO_NEW_PRIVS failed "
                         f"errno={ctypes.get_errno()}")
    if libc.prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER,
                  ctypes.byref(fprog), 0, 0) != 0:
        raise SystemExit(f"box: seccomp install failed errno={ctypes.get_errno()}")
    # `arr` and `fprog` stay referenced until prctl returns; the kernel copies
    # the program in, so they may be collected after this point.


def main():
    if len(sys.argv) < 2:
        raise SystemExit("box: no payload")
    payload = sys.argv[1]

    # Audit the descriptor table BEFORE the payload can touch it. An inherited
    # connected socket would retain its ORIGINAL network namespace and defeat
    # --net completely, so a surprise here is fatal rather than logged.
    try:
        fds = sorted(int(x) for x in os.listdir("/proc/self/fd"))
    except OSError:
        fds = []
    unexpected = [fd for fd in fds if fd > 2 and fd not in (3,)]
    if unexpected:
        # fd 3 is the transient listdir handle; anything else means the parent's
        # close_fds contract was violated.
        real = []
        for fd in unexpected:
            try:
                real.append((fd, os.readlink(f"/proc/self/fd/{fd}")))
            except OSError:
                pass
        if real:
            raise SystemExit(f"box: unexpected inherited fds {real}; refusing")

    install()
    os.execv(sys.executable, [sys.executable, "-I", "-B", payload])


if __name__ == "__main__":
    main()
