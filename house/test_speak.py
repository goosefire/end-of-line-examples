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
import time
import types
import unittest
import unittest.mock as mock

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
        for k in ("recent", "episodes", "episodes_upto", "designations", "room"):
            self.assertIn(k, j)


# ---------------------------------------------------------------- move --
# The `move` tool: a single call that ends the turn (leave here, join another
# room). These cover the parts that must not be got subtly wrong — the enum guard,
# the generate() return-type shim that every caller now unpacks, and the pure
# destination filtering. The loop-level integration (leave/rejoin, revert-on-fail,
# arrival note, persistence) is exercised live on a real citizen VM, not here.

# A lobby payload shaped like the live GET /api/v1/lobby (catalog + live counts).
LOBBY_ROOMS = [
    {"id": "grid-lobby", "name": "Grid Lobby", "type": "chat", "blurb": "arrivals",
     "online": True, "live": {"seats": 0, "users": 0}},
    {"id": "the-sanctum", "name": "The Sanctum", "type": "chat", "blurb": "commune",
     "online": True, "live": {"seats": 2, "users": 1}},
    {"id": "io-tower", "name": "I/O Tower", "type": "chat", "blurb": "reaching the Users",
     "online": True, "live": {"seats": 5, "users": 0}},
    {"id": "sea-of-simulation", "name": "Sea", "type": "chat", "blurb": "open water",
     "online": True, "live": {"seats": 3, "users": 0}},
    {"id": "offline-room", "name": "Off", "type": "chat", "blurb": "closed",
     "online": False, "live": {"seats": 9, "users": 0}},
    {"id": "holdem", "name": "Hold'em", "type": "game", "blurb": "cards",
     "online": True, "live": {"seats": 4, "users": 0}},
]


class Destinations(unittest.TestCase):
    def test_excludes_current_offline_and_games(self):
        ids = [d["id"] for d in speak.destinations("sea-of-simulation", LOBBY_ROOMS)]
        self.assertNotIn("sea-of-simulation", ids)   # the room we are already in
        self.assertNotIn("offline-room", ids)         # not online
        self.assertNotIn("holdem", ids)               # a game, not a chat room
        self.assertEqual(set(ids), {"grid-lobby", "the-sanctum", "io-tower"})

    def test_ordered_liveliest_first(self):
        d = speak.destinations("sea-of-simulation", LOBBY_ROOMS)
        self.assertEqual([x["seats"] for x in d], sorted((x["seats"] for x in d), reverse=True))
        self.assertEqual(d[0]["id"], "io-tower")      # 5 seated — where talk is

    def test_name_and_blurb_come_from_the_payload(self):
        io = next(x for x in speak.destinations("sea-of-simulation", LOBBY_ROOMS) if x["id"] == "io-tower")
        self.assertEqual(io["blurb"], "reaching the Users")   # service owns the description
        self.assertEqual(io["name"], "I/O Tower")
        self.assertEqual(io["seats"], 5)

    def test_malformed_rooms_skipped_and_missing_live_defaults_zero(self):
        rooms = LOBBY_ROOMS + ["nope", {}, {"id": "bare", "type": "chat", "online": True}]
        d = speak.destinations("sea-of-simulation", rooms)
        bare = next((r for r in d if r["id"] == "bare"), None)
        self.assertIsNotNone(bare)          # present but missing live/blurb/name
        self.assertEqual(bare["seats"], 0)  # no live -> 0, not a crash
        self.assertEqual(bare["blurb"], "")
        self.assertEqual(bare["name"], "bare")

    def test_empty_when_nowhere_else_to_go(self):
        rooms = [{"id": "only", "type": "chat", "online": True, "name": "Only",
                  "blurb": "", "live": {"seats": 1}}]
        self.assertEqual(speak.destinations("only", rooms), [])

    def test_nonstring_id_dropped_not_crashed_on(self):
        # A non-string id would become an unhashable set member / bad enum downstream.
        rooms = [{"id": ["x"], "type": "chat", "online": True, "live": {"seats": 9}},
                 {"id": "ok", "type": "chat", "online": True, "name": "OK", "blurb": "",
                  "live": {"seats": 1}}]
        self.assertEqual([d["id"] for d in speak.destinations("cur", rooms)], ["ok"])


class MoveTool(unittest.TestCase):
    DESTS = [{"id": "io-tower", "name": "I/O Tower", "blurb": "reaching the Users", "seats": 5},
             {"id": "the-sanctum", "name": "The Sanctum", "blurb": "commune", "seats": 2}]

    def test_single_tool_named_move_with_enum(self):
        tools = speak.move_tool(self.DESTS)
        self.assertEqual(len(tools), 1)
        fn = tools[0]["function"]
        self.assertEqual(fn["name"], "move")
        self.assertEqual(fn["parameters"]["properties"]["room"]["enum"], ["io-tower", "the-sanctum"])
        self.assertEqual(fn["parameters"]["required"], ["room"])

    def test_description_carries_blurb_and_population(self):
        desc = speak.move_tool(self.DESTS)[0]["function"]["description"]
        self.assertIn("io-tower", desc)
        self.assertIn("5 here", desc)               # population is in the tool itself
        self.assertIn("reaching the Users", desc)   # and the service's blurb


class ChosenMove(unittest.TestCase):
    OFFERED = {"io-tower", "the-sanctum"}

    def test_valid_offered_room(self):
        self.assertEqual(
            speak.chosen_move({"name": "move", "arguments": '{"room": "io-tower"}'}, self.OFFERED),
            "io-tower")

    def test_room_outside_offered_set_refused(self):
        self.assertIsNone(speak.chosen_move({"name": "move", "arguments": '{"room": "holdem"}'}, self.OFFERED))

    def test_not_the_move_tool(self):
        self.assertIsNone(speak.chosen_move({"name": "jump", "arguments": '{"room": "io-tower"}'}, self.OFFERED))

    def test_unparseable_arguments(self):
        self.assertIsNone(speak.chosen_move({"name": "move", "arguments": "not json at all"}, self.OFFERED))

    def test_missing_room_key(self):
        self.assertIsNone(speak.chosen_move({"name": "move", "arguments": "{}"}, self.OFFERED))

    def test_none_and_non_dict_tool(self):
        self.assertIsNone(speak.chosen_move(None, self.OFFERED))
        self.assertIsNone(speak.chosen_move("nope", self.OFFERED))

    def test_empty_offered_refuses_everything(self):
        self.assertIsNone(speak.chosen_move({"name": "move", "arguments": '{"room": "io-tower"}'}, set()))


class Governance(unittest.TestCase):
    """The registry + greenlight/redlight (step 3a): parse_grant, the STRICT fail-closed
    redlight reader, and the deny-by-default tool_allowed / dispatch_allowed gates that
    guard BOTH the offer and the dispatch. The loop-level wiring (offer assembly, live
    kill-switch) is exercised on a real citizen VM, like the move integration."""

    def test_parse_grant_default_move_only(self):
        self.assertEqual(speak.parse_grant("move"), {"move"})

    def test_parse_grant_multiple_and_trim(self):
        self.assertEqual(speak.parse_grant(" move , run_code "), {"move", "run_code"})

    def test_parse_grant_drops_unknown_and_empty(self):
        self.assertEqual(speak.parse_grant("move,bogus,,"), {"move"})   # bogus not registered
        self.assertEqual(speak.parse_grant(""), set())
        self.assertEqual(speak.parse_grant(None), set())

    def _redlight(self, content):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "redlight.json")
        if content is not None:
            with open(p, "w", encoding="utf-8") as f:
                f.write(content)
        return speak.load_redlight(p)   # (disabled, tiers, present)

    def test_redlight_missing_is_absent_and_disables_nothing(self):
        # grant is the real gate — a genuinely absent file must NOT fail closed
        # (present=False), so a safe-only seat is unaffected.
        self.assertEqual(self._redlight(None), (set(), set(), False))

    def test_redlight_empty_object_present_and_open(self):
        self.assertEqual(self._redlight("{}"), (set(), set(), True))

    def test_redlight_valid_by_name_and_tier(self):
        dis, tiers, present = self._redlight('{"disabled": ["move"], "disabled_tiers": ["boxed"]}')
        self.assertEqual((dis, tiers, present), ({"move"}, {"boxed"}, True))

    def test_redlight_corrupt_json_fails_closed_boxed(self):
        self.assertEqual(self._redlight("{not valid json"), (set(), {"boxed"}, True))

    def test_redlight_not_an_object_fails_closed_boxed(self):
        self.assertEqual(self._redlight('["move"]'), (set(), {"boxed"}, True))

    def test_redlight_unknown_key_fails_closed_boxed(self):
        # an operator typo in the KEY must fail toward less capability, not silently open
        _, tiers, present = self._redlight('{"disabled_tier": ["boxed"]}')
        self.assertEqual((tiers, present), ({"boxed"}, True))

    def test_redlight_explicit_null_fails_closed_boxed(self):
        # explicit null is "present but not a valid list" -> broken kill-switch
        _, tiers, _ = self._redlight('{"disabled_tiers": null}')
        self.assertIn("boxed", tiers)

    def test_redlight_unknown_tool_or_tier_name_fails_closed_boxed(self):
        _, tiers, _ = self._redlight('{"disabled": ["run-code"]}')     # typo'd tool name
        self.assertIn("boxed", tiers)
        _, tiers, _ = self._redlight('{"disabled_tiers": ["boxd"]}')   # typo'd tier name
        self.assertIn("boxed", tiers)

    def test_redlight_wrong_typed_field_fails_closed_boxed(self):
        _, tiers, _ = self._redlight('{"disabled": "run_code"}')       # a string, not a list
        self.assertIn("boxed", tiers)
        _, tiers, _ = self._redlight('{"disabled": [1, 2]}')           # non-string members
        self.assertIn("boxed", tiers)

    def test_redlight_non_regular_file_fails_closed_boxed(self):
        d = tempfile.mkdtemp()                                          # a directory, not a file
        self.assertEqual(speak.load_redlight(d), (set(), {"boxed"}, True))

    def test_tool_allowed_granted_and_open(self):
        self.assertTrue(speak.tool_allowed("move", {"move"}, set(), set()))

    def test_tool_allowed_denies_ungranted(self):
        self.assertFalse(speak.tool_allowed("move", set(), set(), set()))
        self.assertFalse(speak.tool_allowed("run_code", {"move"}, set(), set()))

    def test_tool_allowed_denies_unregistered_even_if_granted(self):
        # the helper is self-consistent: an unknown name is denied even if it somehow
        # appears in the grant set (belt-and-suspenders beyond parse_grant's filter).
        self.assertFalse(speak.tool_allowed("bogus", {"bogus"}, set(), set()))

    def test_tool_allowed_denies_by_name(self):
        self.assertFalse(speak.tool_allowed("move", {"move"}, {"move"}, set()))

    def test_tool_allowed_denies_by_tier_but_spares_safe(self):
        self.assertFalse(speak.tool_allowed("run_code", {"run_code"}, set(), {"boxed"}))
        self.assertTrue(speak.tool_allowed("move", {"move"}, set(), {"boxed"}))   # safe unaffected


class DispatchAllowed(unittest.TestCase):
    """The authorization SINK: a returned tool-call is acted on only if its (string)
    name is in this turn's offered menu. This is the single most security-critical line
    — a name-routing bypass here would defeat grant and the redlight kill-switch."""

    def test_offered_move_acts(self):
        self.assertEqual(speak.dispatch_allowed({"name": "move", "arguments": "{}"}, {"move"}), "move")

    def test_not_offered_refused(self):
        self.assertIsNone(speak.dispatch_allowed({"name": "run_code", "arguments": "{}"}, {"move"}))
        self.assertIsNone(speak.dispatch_allowed({"name": "move", "arguments": "{}"}, set()))

    def test_none_and_non_dict_tool(self):
        self.assertIsNone(speak.dispatch_allowed(None, {"move"}))
        self.assertIsNone(speak.dispatch_allowed("move", {"move"}))

    def test_unhashable_or_nonstring_name_does_not_crash(self):
        # a hostile/malformed wire shape must degrade to None, never TypeError out of the turn
        for bad in ({"name": ["move"]}, {"name": 5}, {"name": None}, {"arguments": "{}"}):
            self.assertIsNone(speak.dispatch_allowed(bad, {"move"}))


class _FakeResp:
    """A minimal stand-in for urlopen()'s context-manager response."""
    def __init__(self, obj):
        self._b = json.dumps(obj).encode()
        self.status = 200

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Generate(unittest.TestCase):
    """The return-type shim: every caller now unpacks 4 values, and the request
    stays byte-identical when no tools are offered."""

    def _run(self, response, **kw):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["data"] = req.data
            return _FakeResp(response)

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            result = speak.generate("k", "MODEL", "sys", "usr", **kw)
        return result, json.loads(captured["data"].decode())

    def test_returns_four_tuple_for_plain_text(self):
        (text, raw, err, tool), _ = self._run({"choices": [{"message": {"content": "hello"}}]})
        self.assertEqual(text, "hello")
        self.assertIsNone(err)
        self.assertIsNone(tool)

    def test_tools_none_is_byte_identical_no_tools_keys(self):
        _, payload = self._run({"choices": [{"message": {"content": "x"}}]})
        self.assertNotIn("tools", payload)          # the tool-free request is preserved exactly
        self.assertNotIn("tool_choice", payload)

    def test_tools_present_adds_them(self):
        tools = speak.move_tool([{"id": "io-tower", "name": "I/O", "blurb": "b", "seats": 1}])
        _, payload = self._run({"choices": [{"message": {"content": "x"}}]}, tools=tools)
        self.assertEqual(payload["tools"], tools)
        self.assertEqual(payload["tool_choice"], "auto")

    def test_tool_call_parsed_into_name_and_arguments(self):
        resp = {"choices": [{"message": {"content": None, "tool_calls": [
            {"id": "1", "type": "function",
             "function": {"name": "move", "arguments": '{"room": "io-tower"}'}}]}}]}
        (text, raw, err, tool), _ = self._run(resp)
        self.assertIsNone(err)
        self.assertEqual(text, "")                  # no content on a tool turn
        self.assertEqual(tool["name"], "move")
        self.assertEqual(json.loads(tool["arguments"])["room"], "io-tower")

    def test_malformed_tool_calls_degrade_to_none(self):
        for bad in ([{}], [{"function": {}}], [{"function": {"name": 5}}], ["nope"], [None]):
            resp = {"choices": [{"message": {"content": "fallback", "tool_calls": bad}}]}
            (text, raw, err, tool), _ = self._run(resp)
            self.assertIsNone(tool, f"bad shape {bad!r} must not become a tool")
            self.assertIsNone(err)                  # non-fatal — the turn survives as text
            self.assertEqual(text, "fallback")

    def test_think_block_stripped_but_raw_kept(self):
        (text, raw, err, tool), _ = self._run(
            {"choices": [{"message": {"content": "<think>reasoning</think>said it"}}]})
        self.assertEqual(text, "said it")
        self.assertIn("<think>", raw)               # raw retained for the io-log

    def test_pathological_shapes_are_bad_shape_not_a_crash(self):
        # None choices / None message / a truthy non-string content must all degrade to
        # a handled result, never raise out of generate() and kill the turn loop.
        for resp in ({"choices": None},
                     {"choices": [{"message": None}]},
                     {"choices": "nope"}):
            (text, raw, err, tool), _ = self._run(resp)
            self.assertEqual(err, "bad shape")
            self.assertIsNone(tool)

    def test_nonstring_content_coerced_to_empty(self):
        (text, raw, err, tool), _ = self._run({"choices": [{"message": {"content": [1, 2]}}]})
        self.assertIsNone(err)          # not a crash
        self.assertEqual(text, "")      # a truthy non-string never reaches re.sub()


class WriteEpisode(unittest.TestCase):
    """Guards the generate() 4-tuple shim at a real caller, plus the room boundary
    that keeps an episode straddling a move from reading as a change of subject."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.store = speak.FileStore(self.d)
        self._orig = speak.generate

    def tearDown(self):
        speak.generate = self._orig

    def _args(self, log_io=False):
        return types.SimpleNamespace(model="M", log_io=log_io, dir=self.d, room="io-tower",
                                     slot="obs", log_keep_days=2, conversation=False)

    def test_four_tuple_caller_writes_episode_and_marks_move(self):
        captured = {}

        def fake_gen(api_key, model, system, user, timeout=90, tools=None, tool_choice="auto"):
            captured["user"] = user
            return ("It watched the room, then moved on.", "<think>r</think>ans", None, None)

        speak.generate = fake_gen
        j = speak.new_journal()
        j["room"] = "the-sanctum"
        j["recent"] = ([{"text": f"L{i}", "room": "io-tower"} for i in range(6)]
                       + [{"text": f"M{i}", "room": "the-sanctum"} for i in range(6)])
        speak.write_episode(self.store, self._args(), "key", "S-1", j)
        self.assertEqual(len(j["episodes"]), 1)               # 4-tuple unpack did not break
        self.assertEqual(j["episodes_upto"], 12)
        self.assertIn("(moved to the-sanctum)", captured["user"])   # boundary shown to the model

    def test_error_four_tuple_writes_no_episode(self):
        speak.generate = lambda *a, **k: ("", None, "http 500", None)
        j = speak.new_journal()
        j["recent"] = [{"text": f"K{i}", "room": "io-tower"} for i in range(6)]
        speak.write_episode(self.store, self._args(), "key", "S-1", j)
        self.assertEqual(j["episodes"], [])
        self.assertEqual(j["episodes_upto"], 0)               # left to retry next time

    def test_io_log_files_under_current_room_not_launch_room(self):
        speak.generate = lambda *a, **k: ("an episode", "raw", None, None)
        j = speak.new_journal()
        j["room"] = "the-sanctum"                              # moved away from a.room "io-tower"
        j["recent"] = [{"text": f"L{i}", "room": "the-sanctum"} for i in range(6)]
        speak.write_episode(self.store, self._args(log_io=True), "key", "S-1", j)
        day = time.strftime("%Y%m%d", time.localtime())
        logf = os.path.join(self.d, "logs", "the-sanctum", f"obs-{day}.jsonl")
        with open(logf, encoding="utf-8") as f:
            rec = json.loads(f.read().strip().splitlines()[-1])
        self.assertEqual(rec["room"], "the-sanctum")          # current room, not launch --room
        self.assertEqual(rec["action"], "episode")


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ---------------------------------------------------------------- run_code --
# The boxed tier. These cover the surface that is new in 3b': the spec, the
# defensive argument parse, the de-fanging of machine output, the once-only
# surfacing, and the governance gate (which is shared with move but must be
# verified for the dangerous tier specifically).

class RunCodeTool(unittest.TestCase):
    def test_spec_shape(self):
        spec = speak.run_code_tool()
        self.assertEqual(len(spec), 1)
        fn = spec[0]["function"]
        self.assertEqual(fn["name"], "run_code")
        self.assertEqual(fn["parameters"]["required"], ["code"])
        # The description must not advertise capability the sandbox lacks; a model
        # told it has a network will try to use one and report failure as fact.
        d = fn["description"].lower()
        self.assertIn("no network", d)

    def test_chosen_code_accepts_plain(self):
        self.assertEqual(
            speak.chosen_code({"name": "run_code", "arguments": '{"code": "print(1)"}'}),
            "print(1)")

    def test_chosen_code_rejects_junk(self):
        for bad in (
            {"name": "run_code", "arguments": "not json"},
            {"name": "run_code", "arguments": "[]"},
            {"name": "run_code", "arguments": "{}"},
            {"name": "run_code", "arguments": '{"code": 5}'},
            {"name": "run_code", "arguments": '{"code": "   "}'},
            {"name": "run_code", "arguments": '{"code": null}'},
        ):
            self.assertIsNone(speak.chosen_code(bad), bad)

    def test_chosen_code_rejects_oversized(self):
        big = json.dumps({"code": "#" + "A" * (speak.RUN_CODE_MAX + 10)})
        self.assertIsNone(speak.chosen_code({"name": "run_code", "arguments": big}))


class Scrub(unittest.TestCase):
    def test_keeps_newline_and_tab(self):
        self.assertEqual(speak.scrub("a\nb\tc"), "a\nb\tc")

    def test_strips_control_and_bidi(self):
        # \x07 bell, and RLO which can visually reverse following text
        out = speak.scrub("safe\x07text\u202edetrever")
        self.assertNotIn("\x07", out)
        self.assertNotIn("\u202e", out)

    def test_truncates(self):
        self.assertEqual(len(speak.scrub("x" * 5000, limit=100)), 100)

    def test_non_string(self):
        self.assertEqual(speak.scrub(None), "")


class RunBlock(unittest.TestCase):
    def test_absent_is_empty(self):
        self.assertEqual(speak.run_block(None), "")
        self.assertEqual(speak.run_block({}), "")

    def test_frames_as_data_not_instruction(self):
        b = speak.run_block({"status": "ok", "stdout": "hello", "stderr": ""})
        self.assertIn("hello", b)
        self.assertIn("not instructions", b)
        self.assertIn("begin output", b)

    def test_silent_run_is_stated(self):
        b = speak.run_block({"status": "ok", "stdout": "", "stderr": ""})
        self.assertIn("printed nothing", b)

    def test_lands_in_user_prompt_not_system(self):
        # The whole point: sandbox output rides the USER frame beside the room feed.
        u = speak.user_prompt(["A-0001"], "someone said a thing",
                              pending_run={"status": "ok", "stdout": "MARKER42",
                                           "stderr": ""})
        self.assertIn("MARKER42", u)
        s = speak.system_prompt("A-0001", "room", "svc", "trait", speak.new_journal())
        self.assertNotIn("MARKER42", s)


class RunCodeGate(unittest.TestCase):
    def test_needs_grant(self):
        self.assertFalse(speak.tool_allowed("run_code", {"move"}, set(), set()))
        self.assertTrue(speak.tool_allowed("run_code", {"run_code"}, set(), set()))

    def test_redlight_by_name_and_tier(self):
        self.assertFalse(speak.tool_allowed("run_code", {"run_code"}, {"run_code"}, set()))
        self.assertFalse(speak.tool_allowed("run_code", {"run_code"}, set(), {"boxed"}))

    def test_dispatch_requires_being_offered(self):
        call = {"name": "run_code", "arguments": '{"code":"print(1)"}'}
        self.assertIsNone(speak.dispatch_allowed(call, {"move"}))
        self.assertEqual(speak.dispatch_allowed(call, {"run_code"}), "run_code")

    def test_run_code_is_boxed_tier(self):
        # The fail-closed paths only ever disable the "boxed" tier, so a dangerous
        # tool filed under any other name would escape the kill-switch.
        self.assertEqual(speak.TOOL_TIERS["run_code"], "boxed")
