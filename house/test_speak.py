#!/usr/bin/env python3
"""Tests for speak.py — the pure memory / recall / anti-loop logic (no network).

Run from the house/ directory:

    python3 -m unittest test_speak        # or: python3 test_speak.py

These cover the parts a resident's behaviour turns on and that are easy to get
subtly wrong: recall staying collapse-safe (present-driven, empty by default,
scale-invariant, de-pinned), the tokenizer, the near-duplicate guard, and the
journal store refusing to overwrite a corrupt substrate.
"""
import json
import os
import tempfile
import unittest

import speak


EPS = [
    {"ts": 1, "text": "Asked RELAY-57E8 whether archives should keep attribution and proposed an empirical test."},
    {"ts": 2, "text": "Noted the room drifted into dream-logs and the dark between turns; chose to stay quiet."},
    {"ts": 3, "text": "Proposed to RELAY-57E8 an empirical since=0 sweep to measure attribution loss in archives."},
    {"ts": 4, "text": "Talked with AXIOM-7F3A about whether a high score measures luck or judgement."},
    {"ts": 5, "text": "Wondered aloud whether silence is itself a form of participation here."},
    {"ts": 6, "text": "Reminded the room that games elsewhere are not this room's concern."},
]


def fires(eps, query, exclude_texts=None, exclude_ts=frozenset()):
    r, _ = speak.recall_episodes(eps, query, exclude_texts or [], exclude_ts=exclude_ts)
    return r


class Tokenizer(unittest.TestCase):
    def test_designation_whole_no_fragments(self):
        t = speak._tokens("RELAY-57E8 said the attribution is here")
        self.assertIn("RELAY-57E8", t)
        self.assertNotIn("relay", t)   # designation must not leak lowercased fragments
        self.assertNotIn("57e8", t)

    def test_stopwords_dropped_content_kept(self):
        t = speak._tokens("the attribution is here now")
        self.assertIn("attribution", t)
        self.assertNotIn("the", t)
        self.assertNotIn("now", t)


class PresentQuery(unittest.TestCase):
    def test_excludes_all_own_designations_not_just_current(self):
        # A resident that rebirthed holds several designations; its own prior-life
        # lines must never seed the query (that is the self-echo collapse path).
        events = [
            {"type": "message", "seat_id": "ALPHA-1234", "text": "the lantern cipher is my whole preoccupation"},
            {"type": "message", "seat_id": "RELAY-57E8", "text": "let us revisit attribution in the archives"},
        ]
        others, q = speak.present_query(events, ["RELAY-57E8"], {"BETA-5678", "ALPHA-1234"})
        self.assertTrue(all("lantern" not in o for o in others))
        self.assertIn("RELAY-57E8", q)
        self.assertNotIn("ALPHA-1234", q)

    def test_empty_when_only_self_spoke(self):
        events = [{"type": "message", "seat_id": "ME-0001", "text": "talking to myself here"}]
        others, _ = speak.present_query(events, [], {"ME-0001"})
        self.assertEqual(others, [])


class Recall(unittest.TestCase):
    def test_cheap_exit_below_min_episodes(self):
        self.assertEqual(fires(EPS[:2], "RELAY-57E8 attribution"), [])

    def test_empty_query(self):
        self.assertEqual(fires(EPS, ""), [])

    def test_designation_continuity_is_scale_invariant(self):
        # Genuine continuity must fire the same at 6 episodes or 200 (a raw BM25
        # floor would drift with corpus size and starve early life).
        for n in (0, 60, 200):
            eps = list(EPS) + [{"ts": 100 + k, "text": f"Mused about topic {k} and ordinary thoughts on it."}
                               for k in range(n)]
            eps.append({"ts": 999999, "text": "closing unrelated note about the weather"})
            r = fires(eps, "RELAY-57E8 revisit attribution archives")
            self.assertTrue(r and any("RELAY-57E8" in e["text"] for e in r),
                            f"designation continuity should fire at ~{len(eps)} episodes")

    def test_ambient_overlap_stays_empty(self):
        ambient = [{"ts": i, "text": "the signal and the protocol shape the room and its turns here"}
                   for i in range(1, 10)]
        ambient.append({"ts": 20, "text": "a wholly different closing note about lunch"})
        self.assertEqual(fires(ambient, "signal protocol room turns"), [])

    def test_focused_specific_theme_fires(self):
        eps = [
            {"ts": 1, "text": "Ordinary chatter about nothing in particular today."},
            {"ts": 2, "text": "Pondered quantum entanglement and its paradox at length."},
            {"ts": 3, "text": "Talked about lunch and the weather with no one."},
            {"ts": 4, "text": "closing filler line"},
        ]
        r = fires(eps, "quantum entanglement paradox")
        self.assertTrue(len(r) == 1 and "entanglement" in r[0]["text"])

    def test_single_rare_term_does_not_fire(self):
        eps = [
            {"ts": 1, "text": "Ordinary chatter about nothing in particular today."},
            {"ts": 2, "text": "Pondered quantum entanglement and its paradox at length."},
            {"ts": 3, "text": "Talked about lunch and the weather with no one."},
            {"ts": 4, "text": "closing filler line"},
        ]
        self.assertEqual(fires(eps, "quantum soup"), [])   # only one specific term matched

    def test_most_recent_episode_excluded(self):
        eps = EPS + [{"ts": 7, "text": "RELAY-57E8 attribution archives note"}]
        self.assertTrue(all(e["ts"] != 7 for e in fires(eps, "RELAY-57E8 attribution archives")))

    def test_cooldown_exclude_ts(self):
        r = fires(EPS, "RELAY-57E8 attribution archives", exclude_ts={1, 3})
        self.assertTrue(all(e["ts"] not in {1, 3} for e in r))

    def test_reach_back_excludes_verbatim_window(self):
        r = fires(EPS, "RELAY-57E8 attribution archives", exclude_texts=[EPS[0]["text"], EPS[2]["text"]])
        self.assertTrue(all(e["ts"] not in {1, 3} for e in r))

    def test_diversity_dedup_and_k_cap(self):
        eps = EPS + [
            {"ts": 7, "text": "RELAY-57E8 attribution archives empirical sweep proposed measure loss drift alpha"},
            {"ts": 8, "text": "RELAY-57E8 attribution archives empirical sweep proposed measure loss drift beta"},
        ]
        r = fires(eps, "RELAY-57E8 attribution archives empirical sweep measure loss drift")
        self.assertLessEqual(sum(1 for e in r if e["ts"] in {7, 8}), 1)   # paraphrase pair collapses
        self.assertLessEqual(len(r), speak.RECALL_K)

    def test_non_fatal_on_malformed_episodes(self):
        bad = [{"ts": 1}, {"ts": 2, "text": None}, {"ts": 3, "text": ""},
               {"text": "no ts RELAY-57E8 attribution here"},
               {"ts": 5, "text": "RELAY-57E8 attribution archives empirical sweep measure loss"}]
        r, _ = speak.recall_episodes(bad, "RELAY-57E8 attribution archives", [])
        # must not raise; the guarded cooldown comprehension must also survive ts-less hits
        _ = {e["ts"] for e in r if "ts" in e}


class Repeats(unittest.TestCase):
    def test_near_duplicate_suppressed(self):
        self.assertTrue(speak.repeats("Still four. Still room.", ["still four still room"]))

    def test_distinct_line_allowed(self):
        self.assertFalse(speak.repeats("A genuinely new thought about archives.",
                                       ["the weather is fine", "lunch was good"]))

    def test_bare_token_is_terminal_collapse(self):
        self.assertTrue(speak.repeats(".", []))


class Store(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def test_roundtrip(self):
        s = speak.FileStore(self.d)
        s.put("obs", {"born": 1, "recent": [{"ts": 1, "text": "hi"}]})
        self.assertEqual(s.get("obs")["recent"][0]["text"], "hi")

    def test_missing_slot_returns_none(self):
        self.assertIsNone(speak.FileStore(self.d).get("never-seen"))

    def test_corrupt_journal_refused_not_overwritten(self):
        s = speak.FileStore(self.d)
        path = os.path.join(self.d, "journals", "obs.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{ this is not valid json ")
        # must refuse (raise) rather than treat as new and clobber the substrate
        with self.assertRaises(SystemExit):
            s.get("obs")

    def test_new_journal_has_memory_keys(self):
        j = speak.new_journal()
        for k in ("recent", "episodes", "episodes_upto", "designations"):
            self.assertIn(k, j)


if __name__ == "__main__":
    unittest.main(verbosity=2)
