import importlib.util
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("slate", os.path.join(HERE, "..", "slate.py"))
slate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(slate)


class UrlFor(unittest.TestCase):
    def test_ordinary_ids_are_unchanged(self):
        slate.MODE = "serve"
        self.assertEqual(slate.url_for("issue", "SF-12"), "/issue/SF-12")
        self.assertEqual(slate.url_for("day", "2026-11-20"), "/day/2026-11-20")
        slate.MODE = "static"
        self.assertEqual(slate.url_for("issue", "SF-12"), "SF-12.html")

    def test_a_crafted_id_cannot_leave_the_attribute(self):
        for mode in ("serve", "static"):
            slate.MODE = mode
            url = slate.url_for("issue", 'x" onmouseover="alert(1)')
            self.assertNotIn('"', url)
            self.assertNotIn(" ", url)
            self.assertNotIn("<", slate.url_for("issue", "<script>"))


if __name__ == "__main__":
    unittest.main()
