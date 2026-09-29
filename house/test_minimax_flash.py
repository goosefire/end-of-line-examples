import json
import unittest
from unittest.mock import patch
import speak, cf_player, g2048_player, wordle_player

MODEL = "MiniMax-M3.1-Flash-Preview"

class Response:
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self):
        return json.dumps({"choices": [{"finish_reason": "stop", "message": {
            "content": "OK", "reasoning_content": "PRIVATE REASONING"}}]}).encode()

class FlashCompatibility(unittest.TestCase):
    def test_all_callers_support_adaptive_reasoning_without_publishing_it(self):
        for mod in (speak, cf_player, g2048_player, wordle_player):
            with self.subTest(caller=mod.__name__):
                requests = []
                def send(request, **kwargs):
                    requests.append(json.loads(request.data))
                    return Response()
                with patch.object(mod.urllib.request, "urlopen", send):
                    result = mod.generate("test", MODEL, "system", "user")
                body = requests[0]
                self.assertNotIn("thinking", body)
                self.assertIn(body["reasoning_effort"], ("low", "medium"))
                self.assertGreaterEqual(body["max_tokens"], 2000)
                self.assertEqual(result[0] if mod is speak else result, "OK")
                if mod is speak: self.assertIn("PRIVATE REASONING", result[1])

    def test_timed_retry_keeps_optional_tools_and_reasoning_headroom(self):
        requests = []
        def send(request, **kwargs):
            requests.append(json.loads(request.data))
            return Response()
        with patch.object(speak.urllib.request, "urlopen", send):
            speak.generate("test", MODEL, "s", "u", think=False, max_tokens=300,
                           tools=[{"type": "function", "function": {"name": "play"}}])
        self.assertEqual(requests[0]["reasoning_effort"], "low")
        self.assertEqual(requests[0]["tool_choice"], "auto")
        self.assertGreaterEqual(requests[0]["max_tokens"], 2000)

    def test_m3_rollback_keeps_thinking_disabled(self):
        requests=[]
        def send(request, **kwargs):
            requests.append(json.loads(request.data))
            return Response()
        with patch.object(speak.urllib.request, "urlopen", send):
            speak.generate("test", "MiniMax-M3", "s", "u", think=False, max_tokens=300)
        self.assertEqual(requests[0]["thinking"], {"type": "disabled"})
        self.assertEqual(requests[0]["max_tokens"], 300)
        self.assertNotIn("reasoning_effort", requests[0])
