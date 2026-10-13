import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("release_check", Path(__file__).resolve().parents[1] / "scripts/check_public_release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseHygieneTests(unittest.TestCase):
    def test_runtime_paths_blocked(self):
        for name in ["runs/evidence.txt", ".env.production", "data/archive.sqlite3-wal", "keys/host.pub", "helper.env"]:
            self.assertTrue(release.forbidden_path(name), name)
        self.assertFalse(release.forbidden_path(".env.example"))
        self.assertFalse(release.forbidden_path("scripts/helper_instance.py"))

    def test_private_values_are_detected_without_printing_them(self):
        secret = "ghp_" + "x" * 36
        content = ("/Users/" + "private-person/project " + secret + " 10." + "45.23.6").encode()
        result = release.inspect_content("example", content)
        self.assertEqual({v["rule"] for v in result}, {"personal-home-path", "credential-token", "non-example-ip"})
        self.assertNotIn(secret, str(result))

    def test_reserved_examples_are_allowed_and_private_denylist_works(self):
        self.assertEqual(release.inspect_content("example", b"127.0.0.1 0.0.0.0 192.0.2.1 https://input.example.com"), [])
        self.assertEqual(release.inspect_content("example", b"internal marker", ["internal"])[0]["rule"], "private-denylist")


if __name__ == "__main__":
    unittest.main()
