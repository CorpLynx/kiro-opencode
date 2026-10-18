import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_real_data as guard  # noqa: E402


# Built at run time, so this file never holds a literal that looks like real data.
ACCOUNT = "8" + "00012345678"
OTHER_ACCOUNT = "7" + "00098765432"
ORG = "o-" + "q7x2k9m4zw"
ROOT = "r-" + "k3v9"
OU = "ou-" + "k3v9-" + "m2x7q8zt"


def found(text):
    return [(what, token) for _, what, token in guard.scan_text(text)]


class ScanTest(unittest.TestCase):
    def test_real_looking_ids_are_flagged(self):
        self.assertEqual(found(f'"account_id": "{ACCOUNT}"'), [("AWS account ID", ACCOUNT)])
        self.assertEqual(found(f"org {ORG} root {ROOT} {OU}"),
                         [("AWS Organizations ID", ORG), ("AWS Organizations ID", ROOT),
                          ("AWS Organizations ID", OU)])

    def test_made_up_ids_pass(self):
        text = ("123456789012 111122223333 100000000042 o-exampleorgid o-0000000001 "
                "ou-0001-00000001 r-0001 o-aaaaaaaaaa")
        self.assertEqual(found(text), [])

    def test_longer_numbers_and_words_are_not_ids(self):
        self.assertEqual(found("20261018155046 1234567890123 for-the-record vpc-0123abcd"), [])

    def test_allowlist_and_files(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "a.txt")
            with open(path, "w") as f:
                f.write(f"one {ACCOUNT}\ntwo {OTHER_ACCOUNT}\n")
            problems = guard.check([path], {ACCOUNT})
            self.assertEqual(len(problems), 1)
            self.assertIn("a.txt:2", problems[0])

    def test_binary_files_are_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "a.png")
            with open(path, "w") as f:
                f.write(ACCOUNT)
            self.assertEqual(guard.check([path], set()), [])

    def test_the_repository_is_clean(self):
        self.assertEqual(guard.check(guard.tracked_files(), guard.load_allowlist()), [])


if __name__ == "__main__":
    unittest.main()
