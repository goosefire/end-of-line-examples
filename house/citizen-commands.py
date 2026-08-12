#!/usr/bin/env python3
"""Collect what every citizen DID, from the VMs onto this host, every few minutes.

The choice log lives inside each citizen's VM and is the only durable record of
what it chose from what it was offered. That is fine until you want to read the
citizenry as a whole, or until a VM is rebaked and takes its history with it.
This walks the running citizen VMs, pulls anything newer than last time, and
appends it to one host-side log.

Idempotent by design: a per-slot cursor of the last timestamp taken means the
same line is never recorded twice however often this runs, and a run that finds
nothing new writes nothing. Read-only against the citizens — it pulls files and
never execs anything inside them.

  ~/eol/monitor/commands-YYYYMMDD.jsonl   one line per citizen decision
  ~/eol/monitor/cursors.json              per-slot high-water mark
  ~/eol/monitor/collector.log             what each run did
"""
import json
import os
import subprocess
import sys
import time

OUT = os.path.expanduser("~/eol/monitor")
CURSORS = os.path.join(OUT, "cursors.json")
LOG = os.path.join(OUT, "collector.log")
# A citizen writing ~1 line per 4 minutes cannot approach this; a loop can.
MAX_LINES_PER_SLOT = 2000
# How long this host-side record lives.
#
# It exists because a citizen's own logs are inside its VM and go with it when
# the VM is rebaked -- but that makes this the LONGEST-LIVED copy of anything it
# carries, and it now carries a preview of what a citizen chose to remember. A
# note lives 6h in the store and 2 days in the citizen's own choice log; without
# a bound here it would live forever on this host, which would make "evicted"
# untrue system-wide. Fourteen days is long enough for the before-and-after
# comparisons this record is kept for, and it is the outer bound on note content
# anywhere.
KEEP_DAYS = 14


def note(msg):
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{stamp} {msg}\n")


def run(args, timeout=30):
    """Never raise into the timer: a collector that dies takes the record with it."""
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except Exception as e:
        return 1, b"", str(e).encode()


def running_citizens():
    rc, out, _ = run(["lxc", "list", "--format", "json"], timeout=60)
    if rc != 0:
        note("lxc list failed")
        return []
    try:
        rows = json.loads(out.decode() or "[]")
    except Exception:
        note("lxc list unparseable")
        return []
    slots = []
    for r in rows:
        name = r.get("name") or ""
        if name.startswith("citizen-vm-") and r.get("status") == "Running":
            slot = name[len("citizen-vm-"):]
            if slot != "base":
                slots.append((name, slot))
    return sorted(slots)


def pull(vm, slot, day):
    """The citizen's choice log for `day`, as text, or None."""
    rc, out, _ = run(["lxc", "file", "pull",
                      f"{vm}/root/eol/choices/{slot}-{day}.jsonl", "-"], timeout=60)
    return out.decode("utf-8", "replace") if rc == 0 else None


def prune(out_dir, keep_days=KEEP_DAYS):
    """Drop collected days older than the window. Never raises into the timer."""
    cutoff = time.strftime("%Y%m%d",
                           time.gmtime(time.time() - keep_days * 86400))
    for name in os.listdir(out_dir):
        if not (name.startswith("commands-") and name.endswith(".jsonl")):
            continue
        day = name[len("commands-"):-len(".jsonl")]
        if len(day) == 8 and day.isdigit() and day < cutoff:
            try:
                os.remove(os.path.join(out_dir, name))
                note("pruned %s (older than %d days)" % (name, keep_days))
            except Exception as e:
                note("could not prune %s (%s)" % (name, e))


def main():
    os.makedirs(OUT, exist_ok=True)
    try:
        with open(CURSORS, encoding="utf-8") as f:
            cursors = json.load(f)
        if not isinstance(cursors, dict):
            cursors = {}
    except Exception:
        cursors = {}

    day = time.strftime("%Y%m%d")
    dest = os.path.join(OUT, f"commands-{day}.jsonl")
    added, seen = 0, 0

    for vm, slot in running_citizens():
        raw = pull(vm, slot, day)
        if raw is None:
            continue  # no log yet today, or the VM went away mid-run
        last = cursors.get(slot) or 0
        fresh, high = [], last
        for line in raw.splitlines()[-MAX_LINES_PER_SLOT:]:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            ts = rec.get("ts")
            if not isinstance(ts, int) or ts <= last:
                continue
            seen += 1
            menu = rec.get("menu") or {}
            call = rec.get("call") or {}
            board = menu.get("board") or {}
            # Flattened to the question this log exists to answer: what could it
            # do, what did it do, and where was it when it decided.
            fresh.append({
                "ts": ts,
                "iso": rec.get("iso"),
                "slot": slot,
                "seat": rec.get("seat"),
                "room": rec.get("room"),
                "chose": rec.get("chose"),
                "tool": call.get("name"),
                "dispatched": call.get("dispatched"),
                "args": call.get("args"),
                "status": call.get("status"),
                "offered": menu.get("tools"),
                "withheld": menu.get("withheld"),
                "at_board": board.get("at_board"),
                "your_turn": board.get("your_turn"),
                "game": board.get("game"),
                # --- authored memory -------------------------------------
                # Without these the record says a citizen chose `remember` and
                # not WHAT it kept, nor whether the screener refused it and why
                # -- which makes every later turn unreadable, the exact failure
                # LOGGING.md was written after.
                #
                # A bounded PREVIEW, matching what the citizen's own log holds.
                # A full copy here would be a third store, and the longest-lived
                # of the three.
                "recall_query": (rec.get("recall") or {}).get("query"),
                "recall_hits": (rec.get("recall") or {}).get("hits"),
                "note_preview": call.get("preview"),
                "note_screen": call.get("screen"),
                "note_new": call.get("new"),
                "notes_held": call.get("held"),
                # The second stage's menu, present only on a turn that recalled.
                "stage_b": menu.get("stage_b"),
            })
            high = max(high, ts)
        if fresh:
            fresh.sort(key=lambda r: r["ts"])
            with open(dest, "a", encoding="utf-8") as f:
                for r in fresh:
                    f.write(json.dumps(r, separators=(",", ":")) + "\n")
            added += len(fresh)
        cursors[slot] = high

    try:
        prune(OUT)
    except Exception as e:
        note("prune failed (%s)" % e)

    tmp = CURSORS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cursors, f)
    os.replace(tmp, CURSORS)  # atomic: a killed run must not leave a half cursor

    if added:
        note(f"+{added} decisions from {len(cursors)} slots -> {os.path.basename(dest)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
