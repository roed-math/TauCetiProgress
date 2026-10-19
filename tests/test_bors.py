"""Native and bors merges share commit windows, including the gate's bootstrap cursor."""

import datetime
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from progress import files, gh, plan, window

spec = importlib.util.spec_from_file_location("collect", ROOT / ".github/scripts/collect.py")
collect = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collect)


class BorsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.code = pathlib.Path(self.tmp.name) / "code"
        self.roadmap = pathlib.Path(self.tmp.name) / "roadmap"
        self.env = dict(os.environ, GIT_AUTHOR_NAME="T", GIT_AUTHOR_EMAIL="t@e",
                        GIT_COMMITTER_NAME="T", GIT_COMMITTER_EMAIL="t@e")
        self.run_git("init", "-q", "-b", "main", str(self.code))
        self.root = self.commit("root")
        # Same (#number) subjects, different GitHub PR states.
        self.bors = self.commit("feat: bors squash (#101)")
        self.native = self.commit("feat: merge queue squash (#100)")
        self.run_git("-C", str(self.code), "checkout", "-q", "-b", "topic", self.native)
        self.commit("internal reference (#999)")
        self.run_git("-C", str(self.code), "checkout", "-q", "main")
        self.run_git("-C", str(self.code), "merge", "--no-ff", "-q", "topic",
                     "-m", "Merge pull request #102 from example/topic")
        self.tip = window.head_sha(self.code, ref="main")
        self.area = self.roadmap / "TauCetiRoadmap/PDE"
        self.area.mkdir(parents=True)
        (self.area / "README.md").write_text("# PDE\n")
        self.rows = [
            {"number": 100, "mergedAt": "2026-01-01T00:00:00Z",
             "mergeCommit": {"oid": self.native}},
            {"number": 101, "mergedAt": None, "mergeCommit": {"oid": self.root}},
            {"number": 102, "mergedAt": "2026-01-01T00:00:00Z",
             "mergeCommit": {"oid": self.tip}},
            # Even a misleading title cannot put an unlanded PR into a window.
            {"number": 103, "mergedAt": None, "mergeCommit": {"oid": self.root},
             "title": "[Merged by Bors] - rejected"},
            {"number": 999, "mergedAt": None, "state": "CLOSED"},
        ]
        self.calls = []

    def run_git(self, *args):
        return subprocess.run(["git", *args], check=True, capture_output=True,
                              text=True, env=self.env).stdout.strip()

    def commit(self, subject):
        self.run_git("-C", str(self.code), "commit", "-q", "--allow-empty", "-m", subject)
        return window.head_sha(self.code, ref="HEAD")

    def github(self, args, **kwargs):
        self.calls.append(args)
        if args[:2] == ["api", "graphql"]:
            self.assertIn("label[]=roadmap/PDE", args)
            self.assertIn("--paginate", args)
            self.assertIn("--slurp", args)
            query = next(arg for arg in args if arg.startswith("query="))
            self.assertIn("states: [CLOSED, MERGED]", query)
            self.assertIn("mergeCommit { oid }", query)
            # Emulate enough pages to cover the entire candidate set, including >1000 PRs.
            chunks = [self.rows[i:i + 100] for i in range(0, len(self.rows), 100)] or [[]]
            return json.dumps([
                {"data": {"repository": {"pullRequests": {
                    "nodes": chunk, "pageInfo": {"hasNextPage": i + 1 < len(chunks),
                                                  "endCursor": str(i)}}}}}
                for i, chunk in enumerate(chunks)])
        if args[:2] == ["api", "user"]:
            return "operator"
        if args[:2] == ["pr", "list"]:
            # The label cache's incremental search: every closure since its last run, merged or not.
            self.assertEqual(args[args.index("--state") + 1], "closed")
            self.assertTrue(args[args.index("--search") + 1].startswith("closed:>="))
            return json.dumps(self.closed_since)
        self.fail(f"unexpected network request: {args}")

    def build(self):
        with patch.object(gh, "gh", side_effect=self.github), \
             patch.object(plan, "docs_source_commit", return_value=self.tip):
            return plan.build_plan(self.roadmap, self.code, commits=[], open_prs=[],
                                   ref="main", min_prs=1)

    def test_first_report_includes_bors_and_native_merges_only(self):
        result = self.build()
        self.assertEqual(result["prs"], [102, 100, 101])
        self.assertEqual(result["from_sha"], self.root)

    def test_existing_cursor_keeps_half_open_window(self):
        (self.area / "PROGRESS.md").write_text(
            files.new_progress_file("PDE") + files.render_section(
                "PDE", self.root, self.bors, [101], "2026-01-01", "prose " * 60))
        result = self.build()
        self.assertEqual(result["prs"], [102, 100])
        self.assertEqual(result["from_sha"], self.bors)

    def test_only_rejected_prs_do_not_bootstrap(self):
        self.rows = [self.rows[3]]
        with self.assertRaises(plan.NotDue):
            self.build()

    def test_the_threshold_planner_counts_bors_merges_through_its_label_cache(self):
        """The planner as the progress workers run it. Its label cache once listed GitHub-native merges
        only, so a roadmap whose PRs all merged through bors read as "no merged PRs yet"."""
        self.run_git("init", "-q", "-b", "main", str(self.roadmap))
        self.run_git("-C", str(self.roadmap), "add", "-A")
        self.run_git("-C", str(self.roadmap), "commit", "-q", "-m", "PDE roadmap")
        labels = pathlib.Path(self.tmp.name) / "labels.json"
        start = datetime.datetime.now(datetime.timezone.utc)

        def run(now):
            cache = gh.LabelCache(labels, now=now)
            with patch.object(gh, "gh", side_effect=self.github), \
                 patch.object(plan, "docs_source_commit", return_value=self.tip):
                return plan.build_plan(self.roadmap, self.code, open_prs=[], ref="main",
                                       strategy="threshold", label_cache=cache)

        self.assertEqual(run(start)["prs"], [102, 100, 101])
        self.calls.clear()
        self.commit("feat: another bors squash (#104)")
        self.tip = window.head_sha(self.code, ref="main")
        self.closed_since = [{"number": 104, "labels": [{"name": "roadmap/PDE"}],
                              "mergedAt": None, "mergeCommit": None}]
        self.assertEqual(run(start + datetime.timedelta(hours=1))["prs"], [104, 102, 100, 101])
        self.assertEqual([c[:2] for c in self.calls], [["pr", "list"]])  # no per-area refetch

    def test_unmerged_reachable_test_merge_sha_is_ignored(self):
        with patch.object(gh, "gh", side_effect=self.github):
            candidates = gh.closed_prs_for_area("PDE")
        self.assertIsNone(candidates[101])
        self.assertIsNone(candidates[103])
        self.assertEqual(plan.unaccounted_prs(self.code, candidates, ref="main"), [])

    def test_many_rejected_prs_need_no_per_pr_requests_or_search_cap(self):
        self.rows += [{"number": n, "mergedAt": None, "mergeCommit": {"oid": self.root}}
                      for n in range(2000, 3200)]
        result = self.build()
        self.assertEqual(result["prs"], [102, 100, 101])
        self.assertEqual(len(self.calls), 2)  # login plus one paginated bulk invocation

    def test_numberless_native_merge_still_blocks_a_bors_bootstrap(self):
        numberless = self.commit("native merge without its PR number")
        self.tip = numberless
        self.rows.append({"number": 104, "mergedAt": "2026-01-01T00:00:00Z",
                          "mergeCommit": {"oid": numberless}})
        with self.assertRaises(window.GitError):
            self.build()

    def test_missing_native_merge_sha_fails_closed(self):
        self.rows[0]["mergeCommit"] = None
        with patch.object(gh, "gh", side_effect=self.github):
            with self.assertRaises(gh.GhError):
                gh.closed_prs_for_area("PDE")

    def test_incomplete_pagination_fails_closed(self):
        response = [{"data": {"repository": {"pullRequests": {
            "nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": "next"}}}}}]
        with patch.object(gh, "gh", return_value=json.dumps(response)):
            with self.assertRaises(gh.GhError):
                gh.closed_prs_for_area("PDE")

    def test_gate_and_planner_agree_on_bors_bootstrap(self):
        real_run = subprocess.run

        def run(args, **kwargs):
            if args[:2] == ["git", "clone"]:
                args = [*args[:-2], str(self.code), args[-1]]
            return real_run(args, **kwargs)

        with patch.object(gh, "gh", side_effect=self.github), \
             patch.object(subprocess, "run", side_effect=run):
            cursor = collect.bootstrap_cursor("PDE", ref="main")
        self.assertEqual(cursor, self.root)

    def test_query_failure_does_not_become_empty_history(self):
        with patch.object(gh, "gh", side_effect=gh.GhError("rate limit")):
            with self.assertRaises(gh.GhError):
                gh.closed_prs_for_area("PDE")
            self.assertIsNone(collect.bootstrap_cursor("PDE"))


if __name__ == "__main__":
    unittest.main()
