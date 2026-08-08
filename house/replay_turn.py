#!/usr/bin/env python3
"""
replay_turn.py — re-run a REAL logged turn against the model, N times, and measure it.

Why this exists. On 2026-08-08 a citizen was losing roughly a third of its turns: the
reply was generated, then thrown away. Nothing in any log said so — a truncated reply
and a deliberate pass were the same empty string. Reading the code could not settle it
either, because the failure is *stochastic*: at temperature 1.0 the same prompt succeeds
most of the time. A single replay of the offending turn came back clean and would have
"disproved" a bug that was really there.

What settles a question like that is sampling the same input many times and looking at
the distribution. Eight replays of one logged turn gave 4 x finish_reason="length" at
exactly the cap with zero postable characters — and comparing caps showed average
completion tokens barely moved (2008 / 1975 / 1838 at 2500 / 4000 / 6000), which is the
finding that mattered: the cap was clipping the TAIL, not shortening the thinking. That
turned a guess into a one-line fix.

The general shape is reusable, so it lives here rather than in someone's shell history:

    take a turn the harness actually logged
      -> replay it unchanged, many times
        -> report the distribution, not the anecdote
          -> vary ONE parameter and compare

Run it inside a citizen instance, where the I/O logs and the key already are:

    lxc exec citizen-vm-fabricate -- python3 /root/house/replay_turn.py --n 8
    lxc exec citizen-vm-fabricate -- python3 /root/house/replay_turn.py --compare 2500 4000
    lxc exec citizen-vm-fabricate -- python3 /root/house/replay_turn.py --pick last --show

Read-only: it posts nothing to the arena and writes nothing but its own output. It does
spend tokens — N completions per trial — so keep N small.
"""
import argparse
import collections
import glob
import json
import os
import re
import sys
import urllib.error
import urllib.request

MINIMAX = "https://api.minimax.io/v1/chat/completions"
ENV = "/root/eol/minimax.env"


def api_key():
    """The key from the environment, or from the citizen's env file if we are running
    inside its instance. Never printed, never written anywhere."""
    k = os.environ.get("MINIMAX_API_KEY", "").strip()
    if k:
        return k
    try:
        for line in open(ENV):
            if line.strip().startswith("MINIMAX_API_KEY"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    sys.exit("no MINIMAX_API_KEY in the environment or " + ENV)


def pick_turn(logdir, which):
    """One logged turn to replay.

    `dropped` is the default because the interesting turn is nearly always the one that
    went wrong: an empty reply whose reasoning block ran long and left nothing after it.
    `last` and `longest` are there for when you are chasing something else.
    """
    rows = []
    for p in sorted(glob.glob(os.path.join(logdir, "*", "*.jsonl"))):
        for line in open(p):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("system") and r.get("user"):
                rows.append(r)
    if not rows:
        sys.exit(f"no logged turns with a prompt under {logdir}")
    rows.sort(key=lambda r: r.get("ts") or 0)

    if which == "last":
        return rows[-1]
    if which == "longest":
        return max(rows, key=lambda r: len(r.get("raw_content") or ""))
    dropped = [r for r in rows
               if r.get("action") == "silence"
               and "</think>" in (r.get("raw_content") or "")
               and not (r["raw_content"].split("</think>")[-1].strip())]
    if not dropped:
        print("no dropped turn found; replaying the longest instead", file=sys.stderr)
        return max(rows, key=lambda r: len(r.get("raw_content") or ""))
    return dropped[-1]


def postable(content):
    """What the harness would actually post: the reply with the reasoning block stripped,
    including an unclosed one left behind by truncation. Kept identical to speak.py's
    own strip, since the whole question is what the harness would have done."""
    t = re.sub(r"<think>.*?</think>", "", content, flags=re.S)
    return re.sub(r"<think>.*$", "", t, flags=re.S).strip()


def once(key, model, turn, max_tokens, extra):
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": turn["system"]},
                     {"role": "user", "content": turn["user"]}],
        "max_tokens": max_tokens,
        "temperature": 1.0,
    }
    payload.update(extra or {})
    req = urllib.request.Request(MINIMAX, data=json.dumps(payload).encode(), method="POST")
    req.add_header("content-type", "application/json")
    req.add_header("authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(req, timeout=180) as f:
            j = json.loads(f.read().decode())
    except urllib.error.HTTPError as e:
        return {"finish": f"http {e.code}", "tokens": 0, "text": ""}
    except Exception as e:
        return {"finish": f"transport {type(e).__name__}", "tokens": 0, "text": ""}
    ch = (j.get("choices") or [{}])[0]
    return {
        "finish": ch.get("finish_reason"),
        "tokens": (j.get("usage") or {}).get("completion_tokens") or 0,
        "text": postable((ch.get("message") or {}).get("content") or ""),
    }


def trial(key, model, turn, n, max_tokens, extra, label, show):
    """One trial: n replays at a fixed setting. Reports the DISTRIBUTION — a single
    sample of a stochastic failure tells you almost nothing."""
    finishes, toks, lost = collections.Counter(), [], 0
    for i in range(n):
        r = once(key, model, turn, max_tokens, extra)
        finishes[r["finish"]] += 1
        toks.append(r["tokens"])
        if not r["text"]:
            lost += 1
        if show:
            print(f"    {i + 1:2}. finish={str(r['finish']):9} tokens={r['tokens']:5} "
                  f"postable={len(r['text']):5}" + ("   <-- LOST" if not r["text"] else ""))
    avg = sum(toks) / max(len(toks), 1)
    print(f"  {label:28} lost {lost}/{n}   avg_completion_tokens={avg:.0f}   "
          + " ".join(f"{k}={v}" for k, v in finishes.most_common()))
    return lost


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--logs", default="/root/eol/logs", help="I/O log root")
    ap.add_argument("--pick", choices=["dropped", "last", "longest"], default="dropped")
    ap.add_argument("--model", default="MiniMax-M3")
    ap.add_argument("--n", type=int, default=8, help="replays per trial (each costs tokens)")
    ap.add_argument("--max-tokens", type=int, default=4000)
    ap.add_argument("--compare", type=int, nargs="+", metavar="CAP",
                    help="run one trial per max_tokens value and compare")
    ap.add_argument("--no-thinking", action="store_true",
                    help="also trial with thinking disabled (M3 honours this; M2.7 ignores it)")
    ap.add_argument("--show", action="store_true", help="print every individual replay")
    a = ap.parse_args()

    key = api_key()
    turn = pick_turn(a.logs, a.pick)
    print(f"replaying {turn.get('slot')} {turn.get('iso')} in {turn.get('room')} "
          f"(action={turn.get('action')}, reasoning={len(turn.get('raw_content') or '')}B)")
    print(f"model={a.model}  n={a.n} per trial\n")

    for cap in (a.compare or [a.max_tokens]):
        trial(key, a.model, turn, a.n, cap, None, f"max_tokens={cap}", a.show)
    if a.no_thinking:
        trial(key, a.model, turn, a.n, a.max_tokens,
              {"thinking": {"type": "disabled"}}, "thinking disabled", a.show)


if __name__ == "__main__":
    main()
