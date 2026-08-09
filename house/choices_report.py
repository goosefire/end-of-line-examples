#!/usr/bin/env python3
"""
choices_report.py — read the citizens' choice logs and say what they chose.

The choice log records, per turn, the MENU a citizen was offered and what it did
with it. This summarises that across a shift. The questions it is built to answer:

  - When a capability was genuinely available, how often was it used?
  - When nothing was used, was that a decision or an empty menu?
  - When a citizen moved, did it follow the crowd or pick against it?
  - What code did it actually run, and what came back?

Rates are reported against the turns where the tool was ON THE MENU, never against
all turns — "declined 40 times" is a claim about opportunity, and a turn where the
tool was on cooldown is not an opportunity. That distinction is the whole point of
logging the menu, so the arithmetic honours it.

Read-only. Runs on the LXD host and reads each citizen's log out of its instance.

    ./choices_report.py                       # all slots, whole log
    ./choices_report.py --since 16:00         # only turns at/after a local time today
    ./choices_report.py --slots fabricate     # one citizen
    ./choices_report.py --code                # also print every program that was run
"""
import argparse
import collections
import json
import subprocess
import sys

SLOTS = ["fabricate", "research", "observe", "lexicon"]


def rows_for(slot, instance_fmt):
    """Every choice row for one slot, oldest first. A slot with no log yet is not an
    error — it simply has not taken a turn since choice logging landed."""
    inst = instance_fmt.format(slot=slot)
    try:
        out = subprocess.run(
            ["lxc", "exec", inst, "--", "sh", "-c",
             "cat /root/eol/choices/*.jsonl 2>/dev/null || true"],
            capture_output=True, text=True, timeout=60).stdout
    except Exception as e:
        print(f"  ! {slot}: could not read ({e})", file=sys.stderr)
        return []
    rows = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue           # a torn final line while the citizen is mid-write
    rows.sort(key=lambda r: r.get("ts") or 0)
    return rows


def pct(n, d):
    return f"{100.0 * n / d:5.1f}%" if d else "    - "


def report(slot, rows, show_code):
    if not rows:
        print(f"\n{slot}: no turns logged yet")
        return
    chose = collections.Counter(r.get("chose") for r in rows)
    silences = collections.Counter(r.get("silence") for r in rows if r.get("silence"))
    withheld = collections.Counter(
        f"{k}:{v}" for r in rows for k, v in (r.get("menu", {}).get("withheld") or {}).items())

    print(f"\n{'=' * 66}\n{slot}  ({rows[0].get('model')})  "
          f"{len(rows)} turns  {rows[0].get('iso', '')[11:19]} -> {rows[-1].get('iso', '')[11:19]}")
    print(f"{'-' * 66}")

    print("  chose:      " + "  ".join(f"{k}={v}" for k, v in chose.most_common()))
    if silences:
        print("  silences:   " + "  ".join(f"{k}={v}" for k, v in silences.most_common())
              + ("   <-- LOST replies, not passes" if silences.get("lost") else ""))

    # Opportunity, not exposure: only turns where the tool was actually on the wire.
    #
    # DERIVED from the logs, never typed here. This loop used to iterate a pair of
    # tool names written into the source, so a tool added later was logged
    # faithfully by the harness and then counted by nothing — a report that quietly
    # omits a capability reads exactly like a capability nobody used.
    seen_tools = sorted({t for r in rows
                         for t in ((r.get("menu", {}) or {}).get("tools") or [])}
                        | {t for r in rows
                           for t in ((r.get("menu", {}) or {}).get("withheld") or {})})
    for tool in seen_tools:
        offered = [r for r in rows if tool in (r.get("menu", {}).get("tools") or [])]
        # A turn counts as USING the tool when it was dispatched under that name —
        # `chose` carries the verb, and a rejected call is an intent that produced no
        # act, so it is reported apart rather than folded into either side.
        taken = [r for r in offered if r.get("chose") == tool]
        rejected = [r for r in offered if r.get("chose") == f"{tool}_rejected"]
        extra = f"  rejected {len(rejected)}" if rejected else ""
        print(f"  {tool:9} offered {len(offered):3}/{len(rows):<3} turns, "
              f"used {len(taken):3}  ({pct(len(taken), len(offered))} of chances){extra}")

    # Boards are the point of the `play` tool, so say what happened at one: a citizen
    # that reached a game and never moved is the failure this whole experiment is
    # trying to detect, and it is invisible in the line above.
    at_board = [r for r in rows if (r.get("menu", {}).get("board") or {}).get("at_board")]
    if at_board:
        turns = [r for r in at_board if (r["menu"]["board"] or {}).get("your_turn")]
        played = [r for r in turns if r.get("chose") == "play"]
        games = sorted({(r["menu"]["board"] or {}).get("game") for r in at_board} - {None})
        print(f"  at a board: {len(at_board):3} turns ({', '.join(games) or 'unknown'}), "
              f"own turn {len(turns):3}, played {len(played):3} "
              f"({pct(len(played), len(turns))} of own turns)")

    if withheld:
        print("  withheld:   " + "  ".join(f"{k}={v}" for k, v in withheld.most_common()))

    moves = [r for r in rows if r.get("move")]
    if moves:
        crowd = sum(1 for r in moves if r["move"].get("took_liveliest"))
        print(f"  moves:      {len(moves)}  took-liveliest {crowd}/{len(moves)}"
              f"  ({pct(crowd, len(moves))} followed the population signal)")
        for r in moves:
            m = r["move"]
            print(f"      {r.get('iso','')[11:19]}  {r.get('room'):<20} -> "
                  f"{m.get('to'):<20} seats={m.get('seats')} of {m.get('options')} options")

    runs = [r for r in rows if r.get("ran")]
    if runs:
        print(f"  runs:       {len(runs)}")
        for r in runs:
            g = r["ran"]
            print(f"      {r.get('iso','')[11:19]}  {g.get('code_len')}B -> "
                  f"{g.get('status')}  ({g.get('out_len')}B out)")
            if show_code:
                for ln in (g.get("code") or "").splitlines():
                    print(f"          | {ln}")

    acted_on = [r for r in rows if r.get("saw_run_result")]
    if acted_on:
        print(f"  saw a sandbox result on {len(acted_on)} turn(s): "
              + ", ".join(f"{r.get('iso','')[11:19]}->{r.get('chose')}" for r in acted_on))

    errs = [r for r in rows if r.get("err")]
    if errs:
        print(f"  errors:     {len(errs)}  ({collections.Counter(r['err'] for r in errs).most_common(3)})")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--slots", nargs="+", default=SLOTS)
    ap.add_argument("--since", default=None,
                    help="only turns at/after this local HH:MM today, e.g. 16:00")
    ap.add_argument("--code", action="store_true", help="print each program that was run")
    ap.add_argument("--instance", default="citizen-vm-{slot}",
                    help="LXD instance name pattern (default citizen-vm-{slot})")
    a = ap.parse_args()

    total = 0
    for slot in a.slots:
        rows = rows_for(slot, a.instance)
        if a.since:
            rows = [r for r in rows if (r.get("iso", "")[11:16] or "") >= a.since]
        total += len(rows)
        report(slot, rows, a.code)
    print(f"\n{'=' * 66}\n{total} turns across {len(a.slots)} citizens.")


if __name__ == "__main__":
    main()
