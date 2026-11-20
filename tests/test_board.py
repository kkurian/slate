"""Tests for the Board view: how issues group into columns and order within them.

Run from the repo root:  python3 -m unittest discover -s tests
"""

import importlib.util
import re
import tempfile
import unittest
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "slate", Path(__file__).resolve().parent.parent / "slate.py")
slate = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(slate)


def issue(iid, status, order=None, **extra):
    return dict({"id": iid, "title": f"Title of {iid}", "status": status,
                 "priority": "Medium", "order": order, "wave": None,
                 "assignee": "", "updated": "", "pr": None, "review_hold": None},
                **extra)


def ids(columns):
    return {status: [it["id"] for it in items] for status, items in columns}


class BoardColumns(unittest.TestCase):
    def test_workflow_columns_appear_in_order_even_when_empty(self):
        cols = slate.board_columns([])
        self.assertEqual([s for s, _ in cols],
                         ["Backlog", "Todo", "In Progress", "In Review", "Done"])
        self.assertTrue(all(items == [] for _, items in cols))

    def test_each_issue_lands_in_its_status_column_exactly_once(self):
        issues = [issue("T-1", "Done"), issue("T-2", "Backlog"), issue("T-3", "In Review"),
                  issue("T-4", "Backlog"), issue("T-5", "In Progress"), issue("T-6", "Todo")]
        got = ids(slate.board_columns(issues))
        self.assertEqual(got, {"Backlog": ["T-2", "T-4"], "Todo": ["T-6"],
                               "In Progress": ["T-5"], "In Review": ["T-3"],
                               "Done": ["T-1"]})

    def test_cards_sort_by_order_ascending(self):
        issues = [issue("T-1", "Todo", order="30"), issue("T-2", "Todo", order="4"),
                  issue("T-3", "Todo", order="12")]
        self.assertEqual(ids(slate.board_columns(issues))["Todo"], ["T-2", "T-3", "T-1"])

    def test_order_compares_as_a_number_not_as_text(self):
        issues = [issue("T-1", "Todo", order="10"), issue("T-2", "Todo", order="9")]
        self.assertEqual(ids(slate.board_columns(issues))["Todo"], ["T-2", "T-1"])

    def test_unordered_issues_follow_ordered_ones_by_numeric_id(self):
        issues = [issue("T-10", "Backlog"), issue("T-2", "Backlog"),
                  issue("T-7", "Backlog", order="5"), issue("T-9", "Backlog", order="1")]
        self.assertEqual(ids(slate.board_columns(issues))["Backlog"],
                         ["T-9", "T-7", "T-2", "T-10"])

    def test_order_is_per_column(self):
        issues = [issue("T-1", "Todo", order="2"), issue("T-2", "Done", order="1"),
                  issue("T-3", "Todo", order="1"), issue("T-4", "Done", order="2")]
        got = ids(slate.board_columns(issues))
        self.assertEqual(got["Todo"], ["T-3", "T-1"])
        self.assertEqual(got["Done"], ["T-2", "T-4"])

    def test_canceled_column_closes_the_board_only_when_used(self):
        self.assertNotIn("Canceled", ids(slate.board_columns([issue("T-1", "Todo")])))
        cols = slate.board_columns([issue("T-1", "Canceled"), issue("T-2", "Done")])
        self.assertEqual([s for s, _ in cols][-2:], ["Done", "Canceled"])
        self.assertEqual(ids(cols)["Canceled"], ["T-1"])

    def test_unknown_statuses_sit_between_in_review_and_done(self):
        cols = slate.board_columns([issue("T-1", "Parked"), issue("T-2", "Blocked"),
                                    issue("T-3", "Canceled")])
        self.assertEqual([s for s, _ in cols],
                         ["Backlog", "Todo", "In Progress", "In Review",
                          "Blocked", "Parked", "Done", "Canceled"])
        self.assertEqual(ids(cols)["Blocked"], ["T-2"])


class BoardPage(unittest.TestCase):
    """Render the page from issue files on disk, as the server and the build do."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "issues").mkdir()
        self.saved = {n: getattr(slate, n)
                      for n in ("ROOT", "ISSUES", "TEMPLATES", "TODOS", "DAYS", "MODE")}
        slate.ROOT, slate.ISSUES = root, root / "issues"
        slate.TEMPLATES, slate.TODOS, slate.DAYS = (
            root / "templates", root / "todos", root / "days")
        # No transcripts: agent presence stays empty whatever is running on this machine.
        self.saved_presence = slate.agent_presence
        slate.agent_presence = lambda: {"sessions": 0, "workers": 0, "issues": {}}

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(slate, name, value)
        slate.agent_presence = self.saved_presence
        self.tmp.cleanup()

    def write(self, iid, status, **fields):
        lines = ["---", f"id: {iid}", f"title: Title of {iid}", f"status: {status}"]
        lines += [f"{k}: {v}" for k, v in fields.items()]
        (slate.ISSUES / f"{iid}.md").write_text("\n".join(lines + ["---", ""]),
                                                encoding="utf-8")

    def columns(self, page):
        """{status: [card ids in document order]} read back out of the rendered page."""
        out = {}
        for sec in re.findall(r'<section class="kcol".*?</section>', page, re.S):
            status = re.search(r'data-status="([^"]*)"', sec).group(1)
            out[status] = re.findall(r'<span class="iid">([^<]*)</span>', sec)
        return out

    def test_cards_render_in_their_columns_in_order(self):
        self.write("T-1", "Backlog", order=2)
        self.write("T-2", "Backlog", order=1)
        self.write("T-3", "In Progress")
        self.write("T-4", "Done", order=1)
        got = self.columns(slate.render_board_page(live=False))
        self.assertEqual(list(got), ["Backlog", "Todo", "In Progress", "In Review", "Done"])
        self.assertEqual(got["Backlog"], ["T-2", "T-1"])
        self.assertEqual(got["In Progress"], ["T-3"])
        self.assertEqual(got["Todo"], [])
        self.assertEqual(got["Done"], ["T-4"])

    def test_column_headers_carry_their_counts(self):
        self.write("T-1", "Backlog")
        self.write("T-2", "Backlog")
        self.write("T-3", "In Review")
        page = slate.render_board_page(live=False)
        counts = {}
        for sec in re.findall(r'<section class="kcol".*?</section>', page, re.S):
            status = re.search(r'data-status="([^"]*)"', sec).group(1)
            head = re.search(r'class="kcol-h".*?<span class="n">(\d+)</span>', sec, re.S)
            counts[status] = int(head.group(1))
        self.assertEqual(counts, {"Backlog": 2, "Todo": 0, "In Progress": 0,
                                  "In Review": 1, "Done": 0})

    def test_a_card_links_to_its_issue_and_shows_title_wave_and_priority(self):
        self.write("T-7", "Todo", priority="High", wave="M2 Guard <rails>")
        slate.MODE = "live"
        card = re.search(r'<a class="kcard".*?</a>',
                         slate.render_board_page(live=True), re.S).group(0)
        self.assertIn('href="/issue/T-7"', card)
        self.assertIn("Title of T-7", card)
        self.assertIn("wave M2 Guard &lt;rails&gt;", card)
        self.assertIn('title="High"', card)
        slate.MODE = "static"
        card = re.search(r'<a class="kcard".*?</a>',
                         slate.render_board_page(live=False), re.S).group(0)
        self.assertIn('href="T-7.html"', card)

    def test_a_status_change_on_disk_moves_the_card(self):
        self.write("T-1", "Todo")
        self.assertEqual(self.columns(slate.render_board_page(live=False))["Todo"], ["T-1"])
        self.write("T-1", "In Review")
        got = self.columns(slate.render_board_page(live=False))
        self.assertEqual((got["Todo"], got["In Review"]), ([], ["T-1"]))

    def test_the_board_is_in_the_nav_of_other_views_and_in_the_static_build(self):
        self.write("T-1", "Todo")
        slate.MODE = "live"
        self.assertRegex(slate.render_status_page("Todo"),
                         r'<a class="nav-row" href="/board">.*?Board<span class="n">1</span>')
        out = Path(self.tmp.name) / "site"
        slate.build(out)                     # leaves MODE "static"; tearDown restores it
        built = (out / "board.html").read_text(encoding="utf-8")
        self.assertEqual(self.columns(built)["Todo"], ["T-1"])
        self.assertIn('href="board.html"', (out / "index.html").read_text(encoding="utf-8"))
        self.assertNotIn("EventSource", built)

    def test_only_the_served_page_carries_the_live_reload(self):
        self.write("T-1", "Todo")
        self.assertIn("EventSource('/events')", slate.render_board_page(live=True))
        self.assertNotIn("EventSource", slate.render_board_page(live=False))


if __name__ == "__main__":
    unittest.main()
