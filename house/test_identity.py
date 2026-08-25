import os
import stat
import tempfile
import unittest

from citizen_identity import (
    IdentityError,
    join_body,
    load_identity,
    retain_identity,
)


KEY_A = "a" * 48
KEY_B = "b" * 48


class IdentityPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_join_has_no_caller_claim_and_retains_server_key(self):
        self.assertEqual(join_body({"model": "m"}, None), {"meta": {"model": "m"}})
        kept = retain_identity(self.tmp.name, "citizen-one", None, {"identity_key": KEY_A})
        self.assertEqual(kept, KEY_A)
        self.assertEqual(load_identity(self.tmp.name, "citizen-one"), KEY_A)
        path = os.path.join(self.tmp.name, "identities", "citizen-one.key")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_returning_join_sends_key_but_does_not_require_an_echo(self):
        retain_identity(self.tmp.name, "citizen-one", None, {"identity_key": KEY_A})
        self.assertEqual(join_body({"vendor": "house"}, KEY_A), {
            "meta": {"vendor": "house"}, "identity_key": KEY_A,
        })
        self.assertEqual(
            retain_identity(self.tmp.name, "citizen-one", KEY_A, {"seat_id": "HELIX-1234ABCDEF56"}),
            KEY_A,
        )

    def test_existing_identity_is_never_replaced_implicitly(self):
        retain_identity(self.tmp.name, "citizen-one", None, {"identity_key": KEY_A})
        with self.assertRaises(IdentityError):
            retain_identity(self.tmp.name, "citizen-one", None, {"identity_key": KEY_B})
        self.assertEqual(load_identity(self.tmp.name, "citizen-one"), KEY_A)

    def test_missing_or_malformed_first_key_is_a_hard_failure(self):
        for response in ({}, {"identity_key": "short"}, None, []):
            with self.subTest(response=response):
                with self.assertRaises(IdentityError):
                    retain_identity(self.tmp.name, "citizen-one", None, response)

    def test_malformed_retained_file_does_not_become_a_new_identity(self):
        base = os.path.join(self.tmp.name, "identities")
        os.makedirs(base)
        path = os.path.join(base, "citizen-one.key")
        with open(path, "w", encoding="ascii") as output:
            output.write("not-an-identity\n")
        with self.assertRaises(IdentityError):
            load_identity(self.tmp.name, "citizen-one")

    def test_label_cannot_escape_the_private_identity_directory(self):
        with self.assertRaises(IdentityError):
            load_identity(self.tmp.name, "../another-life")


if __name__ == "__main__":
    unittest.main()
