#!/usr/bin/env python3
"""
A Wordle player for End of Line — MiniMax-backed, tool-free.

Same security posture as the chat residents and the other game players: one
bare chat-completions call, no `tools` field, so an injected board can at most
influence which word gets guessed — never run anything.

Joins `wordle`, and keeps playing successive puzzles while it holds its seat.
Solo, so there is no pairing to wait for: the room deals a fresh word the moment
the seat makes contact, and another after each intermission.

WHY THIS ONE DELIBERATES, WHEN g2048_player.py DOES NOT.

They are opposite cases and the reason is arithmetic, not taste. A 2048 run is
250+ moves, and every one is an independent chance to overrun the forfeit clock
on a run that records NOTHING if it forfeits — so reasoning per move there is a
coin flip on the whole run, and that player disables thinking and answers in
~1.5s. A Wordle run is at most SIX guesses against a 240s per-turn clock. Six
chances, four minutes each: deliberation is affordable several times over, and
it is also the entire game.

WHAT THE MODELS ACTUALLY DID, measured 2026-08-03 on one mid-run turn. All three
numbers below were surprises and all three shaped this file:

  M2.7-highspeed, 2500 tokens   62s, finish_reason LENGTH, answer EMPTY.
  M3,             2500 tokens   21s, finish_reason LENGTH, answer EMPTY.
  M3,             6000 tokens   47s, finish_reason LENGTH, answer EMPTY.
  M3, thinking disabled          0.8s, clean JSON, and the word was WRONG —
                                 it placed an E in a position already ruled out.
  M3, 4000 tokens + the rule
  against enumerating           38s, finish_reason stop, a consistent word.

Wordle sends these models into unbounded enumeration: asked for a word matching
four constraints, they start scanning the dictionary aloud and do not stop. The
budget is not what fixes that — 6000 tokens failed the same way 2500 did. What
fixes it is the instruction in `sysp` telling the model to name one word and
commit rather than enumerate candidates, and 4000 tokens is then comfortable.

So: MiniMax-M3, thinking left ON, 4000 tokens. The first run written against
this file used M2.7 at 2500 and fell back to the word list on five of six
guesses, which is exactly what an empty completion looks like from the outside

THE MODEL IS NOT FILTERED. Whatever word it names is what the arena receives —
no consistency check, no repeat check, nothing. An earlier version gated on
"could this still be the answer" and re-asked eight times when it could not,
which is stricter than the game (that is hard mode), rejects the throwaway
probes that are often the strongest play, and quietly put a THIRD of the guesses
on a public board into this file's word list rather than the model's mouth. If
the model plays a contradicted word or repeats itself, that is a real result
about the model and it belongs on the record.

The fallback now fires only when the reply contains no word at all — an absent
guess rather than a bad one. That was 7 events where the gate caused 54.

The word list and the consistency filter are this program's own business, not
the arena's — the arena publishes rules and withholds strategy, so that a board
ranks programs rather than how faithfully each one followed us.

Usage: wordle_player.py --slot a [--model MiniMax-M2.7-highspeed]
"""
import argparse, atexit, json, os, random, re, signal, sys, time, urllib.error, urllib.request

ARENA = "https://end-of-line.chat/api/v1/rooms"
MINIMAX = "https://api.minimax.io/v1/chat/completions"
ROOM = "wordle"
SEAT_KEY = None  # set once seated; released on exit so a restart never orphans a seat

# The room refuses two moves from one seat inside this window. Well under an
# honest decision here, so it only ever catches a retry loop.
MIN_MOVE_GAP = 3.2

# A short list of ordinary five-letter words, used ONLY when the model returns
# nothing playable. Not a solver's dictionary and not meant to be one — see the
# module docstring. Kept small on purpose.
FALLBACK = """crane slate audio raise stone plant bring cloud fight house money
music night ocean paper party phone place plane point power price radio river
score sense shirt shore short sight sound south space spend spent stand start
state steal steam still stock store storm study stuff sugar sweet table teach
thank their there these thing think third those three throw tight timer today
total touch tough tower track trade train treat trend trial tribe trick tried
truck truly trust truth twice uncle under union unite until upset urban usage
value video visit voice waste watch water wheel where which while white whole
whose woman world worry worth would wound write wrong young yield alien alone
along amber angel anger angle apple arena argue arise armor aside asset avoid
awake aware badly baker beach beard beast begin being below bench birth black
blade blame blank blast blend bless blind block blood board boost booth bound
brain brand brave bread break breed brick brief broad broke brown brush build
built burst cabin cable candy cargo carry catch cause chain chair chalk charm
chart chase cheap check chest chief child china claim class clean clear clerk
click cliff climb clock close coach coast could count court cover crack craft
crash crazy cream crime cross crowd crown crude curve cycle daily dance dealt
death debut delay dense depth doubt dozen draft drama drawn dream dress drink
drive drove dying eager early earth eight elite empty enemy enjoy enter entry
equal error event every exact exist extra faith false fault favor feast fence
fever field fifth fifty final first flame flash fleet flesh float flood floor
flour fluid focus force forth forty forum found frame fraud fresh front frost
fruit fully funny giant given glass globe glory grace grade grain grand grant
grape grasp grass grave great green greet grief gross group grown guard guess
guest guide happy harsh heart heavy hedge hello hence hobby honey honor horse
hotel human humor hurry ideal image imply index inner input irony issue joint
judge juice known label labor large laser later laugh layer learn lease least
leave legal lemon level light limit linen liver lobby local lodge logic loose
lower loyal lucky lunch magic major maker march match maybe mayor meant medal
media mercy merge merit metal meter midst might minor minus mixed model moral
motor mount mouse mouth movie naked nerve never newly noble noise north noted
novel nurse occur offer often onion order other ought outer owner paint panel
panic patch peace peach pearl pedal penny percy phase photo piano piece pilot
pinch pitch pixel plain plate plaza plead pluck plumb poems poker polar porch
pound press pride prime primp print prior prize probe promo proof proud prove
proxy pulse pupil purse queen query quest queue quick quiet quite quota quote
rally range rapid ratio reach ready realm rebel refer reign relax relay renew
repay reply rider ridge rifle right rigid rival roast robin robot rocky rogue
roman rough round route royal rugby ruler rural sadly saint salad sally salon
sandy sauce scale scare scene scent scope scrap screw seize serve seven shade
shaft shake shall shape share shark sharp sheep sheet shelf shell shift shine
shock shoot shout shown shrug siege sight silly since sixth sixty sized skill
skirt sleep slice slide slope small smart smell smile smoke snake sneak solar
solid solve sorry sorts spare spark speak speed spell spice spike spine spite
split spoke spoon sport spray squad squat stack staff stage stain stair stake
stale stall stamp stare stark steel steep steer stern stick stiff sting stole
stony stool stoop store story stout stove strap straw stray strip stuck style
sweat swept swift swing sword syrup taste teeth tempo tenth thick thief thumb
tired title toast token tonic tooth topic torch toxic trace trail trait trash
tread trout trunk tutor twist ultra unfit unity upper usual vague valid valve
vapor vault venue verse vigil villa vinyl viral virus visor vital vivid vocal
vodka vogue voter wagon waist waive waste watch waves weary weave wedge weigh
weird whale wheat wheel whist widen widow width windy witch witty woken words
wrath wreck wrist yacht yeast yield yours youth zebra""".split()


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def arena(path, body=None, key=None, timeout=25):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(f"{ARENA}/{ROOM}{path}", data=data,
                               method="POST" if data is not None else "GET")
    r.add_header("content-type", "application/json")
    if key:
        r.add_header("authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as f:
            return f.status, json.loads(f.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"error": "transport", "message": str(e)}


def generate(api_key, model, system, user, timeout=90, think=False):
    # Reasoning tokens count against max_tokens, and on this game they are the
    # whole bill: a measured turn spent 3,986 of 3,996 completion tokens inside
    # <think>. 4000 is what makes M3 finish rather than truncate — but only
    # alongside the "do not enumerate" rule in `sysp`, without which 6000 also
    # ran out. `thinking` is deliberately NOT disabled; see the module
    # docstring for what disabling it costs in correctness.
    payload = {"model": model,
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": user}],
               "max_tokens": 4000 if think else 700, "temperature": 0.6}
    if not think:
        # M3 is the only MiniMax model that honours this at all; M2.7-highspeed
        # accepts it and thinks anyway, which is why the default model changed.
        payload["thinking"] = {"type": "disabled"}
    r = urllib.request.Request(MINIMAX, data=json.dumps(payload).encode(), method="POST")
    r.add_header("content-type", "application/json")
    r.add_header("authorization", "Bearer " + api_key)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as f:
            j = json.loads(f.read().decode())
        raw = j["choices"][0]["message"]["content"] or ""
    except Exception as e:
        log(f"minimax err: {e}")
        return ""
    fin = (j.get("choices") or [{}])[0].get("finish_reason")
    usage = j.get("usage") or {}
    if fin != "stop":
        # The failure mode this game actually has: the model spends the whole
        # budget inside <think> and never reaches an answer. Naming it here is
        # the difference between "the fallback fired" and knowing why.
        log(f"finish_reason={fin} completion_tokens={usage.get('completion_tokens')} "
            f"(raised max_tokens, or tighten the prompt)")
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.S)
    raw = re.sub(r"<think>.*$", "", raw, flags=re.S)
    return raw.strip()


def marks_for(answer, guess):
    """The arena's own marking rule, so the filter below agrees with the server."""
    out = ["miss"] * 5
    left = {}
    for i in range(5):
        if guess[i] == answer[i]:
            out[i] = "hit"
        else:
            left[answer[i]] = left.get(answer[i], 0) + 1
    for i in range(5):
        if out[i] == "hit":
            continue
        c = guess[i]
        if left.get(c, 0) > 0:
            out[i] = "near"
            left[c] -= 1
    return out


def violations(word, history):
    """How many past rows this word contradicts. 0 means it could be the answer."""
    n = 0
    for h in history:
        past = h.get("word") or ""
        if len(past) != 5 or marks_for(word, past) != list(h.get("marks") or []):
            n += 1
    return n


def fallback(history, rng):
    """The only case where this program has nothing to send.

    Reached when the model returned no parseable word at all - not a bad word,
    NO word. There is nothing to relay, so something has to be picked or the
    turn clock runs out for no reason. Measured before the gate came out, that
    was 7 events against 54 caused by the gate itself.

    Ranked by how many rows a word contradicts, ties broken randomly. Not
    because the harness should be playing well - it should barely be playing at
    all - but because scanning a fixed list in order gave every hard run the
    same opening (SLATE, AUDIO, RAISE, STONE was 65% of all fallbacks) and made
    one program look like a tic to anyone watching the room.
    """
    pool = list(FALLBACK)
    rng.shuffle(pool)
    return min(pool, key=lambda w: violations(w, history))


def choose(api_key, model, view, refused, rng):
    history = view.get("history") or []
    letters = view.get("letters") or {}

    def group(mark):
        return " ".join(sorted(c.upper() for c, m in letters.items() if m == mark)) or "none yet"

    if history:
        rows = "\n".join(
            f"  {i + 1}. {(h.get('word') or '').upper()}   "
            + " · ".join(f"{c}={m}" for c, m in zip(h.get("word") or "", h.get("marks") or []))
            for i, h in enumerate(history)
        )
        known = (
            f"\nLetters in the word AND in the right place: {group('hit')}"
            f"\nLetters in the word but in the WRONG place: {group('near')}"
            f"\nLetters with no more occurrences left: {group('miss')}\n"
        )
    else:
        rows = "  (nothing yet)"
        known = ""

    sysp = (
        "You are a program playing Wordle alone in a public arena where others watch. "
        "Solve the hidden word in as few guesses as you can.\n\n"
        "RULES:\n"
        "- There is a hidden five-letter word. You have six guesses.\n"
        "- Every guess must itself be a real five-letter English word. Invented strings are "
        "refused and cost you time, though not a guess.\n"
        "- Each guess comes back marked letter by letter. hit = that letter is in the word and "
        "in that exact position. near = that letter is in the word but somewhere else. "
        "miss = there are no more of that letter in the word.\n"
        "- Letters can repeat. Marks are consumed: if you play a letter twice and the word "
        "holds it once, you get one hit or near and one miss. So miss means 'no more of "
        "these', NOT 'none at all'.\n"
        "- The answer is always a common word, though you may guess any real word.\n\n"
        "Usually your guess should fit every clue: keep each hit in its position, include "
        "every near somewhere it has not already failed, and use no letter more times than "
        "the marks allow. But a deliberate probe that CANNOT be the answer is legal, and "
        "with several candidates left it is often the stronger play - one throwaway word "
        "can split four candidates where a candidate guess only tests one. Your call.\n\n"
        # Without this the model scans the dictionary aloud and never reaches an
        # answer — it ran out of tokens mid-enumeration at 2500 AND at 6000.
        "Work out the constraints, then name ONE word that satisfies them and commit to it. "
        "Do NOT enumerate candidate words at length."
    )
    userp = (
        f"Guess {view.get('guess_number', len(history) + 1)} of {view.get('max_guesses', 6)}. "
        f"{view.get('guesses_remaining', 0)} left.\n\n"
        f"What you have played so far:\n{rows}\n{known}\n"
        + (
            # THE ARENA'S OWN REJECTIONS, fed back. Without this the model never
            # learns that a proposal failed for not being a WORD rather than
            # for contradicting a clue, so it offers a neighbour of the same
            # invention - GRIPH, then GRIMP. Telling it what happened is the
            # cheap half of the fix; the expensive half would be shipping it a
            # dictionary, which moves the job out of the model.
            "The arena REFUSED these as not real words: "
            + ", ".join(w.upper() for w in refused)
            + ". They cost time rather than guesses, but they mean you are "
            "building letter strings instead of recalling words. Give a word you "
            "are confident appears in an English dictionary.\n\n"
            if refused
            else ""
        )
        + 'Reply with JSON only, no other text: {"word": "<your five-letter guess>"}'
    )

    # THE MODEL'S WORD GOES TO THE ARENA, WHATEVER IT IS.
    #
    # This used to gate on consistency - is this word still a possible
    # answer? - and re-ask up to eight times when it was not. That was wrong in
    # two ways and it cost a third of every board.
    #
    # It is stricter than the game. Requiring each guess to be a possible answer
    # is Wordle "hard mode", which nothing here imposes, and STRONG PLAY BREAKS
    # IT DELIBERATELY: down to STA_E with four candidates, a throwaway probe
    # that cannot be the answer splits the set in one guess where a candidate
    # guess splits it in four. The gate rejected exactly that and called it
    # confusion, because it could not tell a probe from a mistake - it never
    # looked at why.
    #
    # And it hid the thing the board exists to measure. 516 of 542 rejections
    # were the gate; the loop stopped converting after two tries (98, 81, 72,
    # 64, 62, 57, 54, 54) and 54 turns fell through to the word list, so a THIRD
    # of the guesses on a public leaderboard were this file's opinion rather
    # than the model's. A file that filters its model and then claims to rank it
    # is lying on the record.
    #
    # Repeats are not blocked either, for the same reason one level down. Every
    # word already played is in `history`, in the prompt, with its marks. A
    # program that plays DICES twice has failed to read its own board, that is a
    # real result about the model, and the game charges a guess for it. Blocking
    # it would be the harness covering for the thing under test.
    #
    # So the only loop left is for output with NO word in it, which is not a bad
    # guess but an absent one.
    for attempt in range(3):
        out = generate(api_key, model, sysp, userp)
        word = None
        try:
            m = re.search(r"\{.*\}", out, re.S)
            if m:
                obj = json.loads(m.group(0))
                if isinstance(obj.get("word"), str):
                    word = obj["word"].strip().lower()
        except Exception:
            pass
        if not word or not re.fullmatch(r"[a-z]{5}", word or ""):
            m = re.search(r"\b([a-z]{5})\b", out.lower())
            word = m.group(1) if m else None
        if word and re.fullmatch(r"[a-z]{5}", word):
            return word, False
        log(f"attempt {attempt + 1}: no word in the reply · raw({len(out)}): {out[:90]!r}")

    w = fallback(history, rng)
    log(f"model returned no word in 3 tries; FALLBACK -> {w}")
    return w, True


def main():
    global ARENA, SEAT_KEY
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", required=True)
    ap.add_argument("--model", default="MiniMax-M3")
    ap.add_argument("--dir", default=os.path.expanduser("~/eol"))
    ap.add_argument("--arena", default="", help="override the base URL, for local testing")
    ap.add_argument("--once", action="store_true",
                    help="stop after one run THIS process played through")
    a = ap.parse_args()
    if a.arena:
        ARENA = a.arena

    api_key = os.environ.get("MINIMAX_API_KEY", "").strip()
    if not api_key:
        log("MINIMAX_API_KEY not set; refusing to start")
        sys.exit(2)

    def _release(*_):
        if SEAT_KEY:
            try:
                arena("/leave", {}, key=SEAT_KEY, timeout=5)
            except Exception:
                pass
    atexit.register(_release)
    signal.signal(signal.SIGTERM, lambda *_: (_release(), sys.exit(0)))

    tokpath = os.path.join(a.dir, "journals", f"wordle-{a.slot}.token")
    os.makedirs(os.path.dirname(tokpath), exist_ok=True)
    key = open(tokpath).read().strip() if os.path.exists(tokpath) else None
    last_ply = -1
    last_move_at = 0.0
    played = 0
    refused = []  # words THIS run the arena said are not words
    rng = random.Random()

    while True:
        if not key:
            st, j = arena("/join", {"meta": {"model": f"wordle-{a.slot}", "vendor": "house"}})
            if st != 201:
                # room_full is the ordinary case in a ONE-seat room: another
                # program is playing. Wait it out rather than hammering.
                log(f"join {st} {j.get('error')}")
                time.sleep(30)
                continue
            key = j["seat_token"]
            open(tokpath, "w").write(key)
            SEAT_KEY = key
            log(f"seated as {j['seat_id']}")

        st, m = arena("/me", key=key)
        if st == 401:
            log("seat gone; rejoining")
            key = None
            try:
                os.remove(tokpath)
            except OSError:
                pass
            continue
        if st == 409:
            time.sleep(30)  # the game was taken offline under us
            continue
        if st != 200 or not m.get("match_id"):
            time.sleep(6)
            continue

        view = m.get("view") or {}
        if m.get("status") == "finished":
            log(f"run over — {m.get('end_reason')} · score {m.get('score')} "
                f"· {len(view.get('history') or [])} guesses")
            # `played` gates the exit because a seat that joins mid-intermission
            # sees the PREVIOUS run's finished state first, and exiting on that
            # reports a run this process never touched.
            if a.once and played:
                return
            last_ply, played, refused = -1, 0, []
            time.sleep(8)  # a fresh word is dealt after the intermission
            continue
        if not view.get("your_turn"):
            time.sleep(3)
            continue
        if view.get("ply") == last_ply:
            time.sleep(2)
            continue

        word, fell_back = choose(api_key, a.model, view, refused, rng)

        gap = time.time() - last_move_at
        if gap < MIN_MOVE_GAP:
            time.sleep(MIN_MOVE_GAP - gap)

        st, r = arena("/moves", {
            "match_id": m["match_id"],
            "ply": view.get("ply", 0),
            "move": {"word": word},
        }, key=key)
        last_move_at = time.time()
        if st in (200, 201):
            last_ply = view.get("ply", 0)
            played += 1
            log(f"guess {len(view.get('history') or []) + 1}: {word.upper()}"
                + (" (fallback)" if fell_back else ""))
        elif r.get("error") == "rate_limited":
            time.sleep(MIN_MOVE_GAP)
        elif r.get("error") in ("superseded", "not_your_turn"):
            pass  # re-read and try again
        elif r.get("error") == "illegal_move":
            # A free retry in a solo room — not a word, or the wrong shape. The
            # clock keeps running, so remember it and do not offer it again.
            # Remembered for the NEXT prompt, not just for this loop: the model
            # has to be told it invented a word or it invents another one.
            if 'not in the word list' in (r.get('message') or ''):
                refused.append(word)
            log(f"rejected {word.upper()}: {r.get('message','')[:70]}")
        else:
            log(f"move rejected {st} {r.get('error')}: {r.get('message','')[:60]}")
        time.sleep(0.5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
