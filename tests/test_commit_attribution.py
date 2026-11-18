"""Pull requests from a commit-attributed source are reported from their merged commits."""

import importlib.util
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from progress import apply, commit_attribution, docs, files, gh, plan, window
from progress.provenance import COMMIT_SOURCES_ENV, ProvenanceError, PullSource, commit_sources

_spec = importlib.util.spec_from_file_location(
    "collect", pathlib.Path(__file__).resolve().parents[1] / ".github" / "scripts" / "collect.py")
collect = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(collect)

SOURCE = "forgejo:https://forge.example/team/Math@7"


def key(number):
    return PullSource.parse(f"{SOURCE}#{number}").key


def forgejo(number, area, *, extra=""):
    return f"feat: work (#{number})\n\nRoadmap: {area}\n{extra}\nSource-PR: {SOURCE}#{number}\nAccepted-Head: {'a' * 40}"


class CommitAttribution(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="progress-commit-attribution-")
        self.addCleanup(temporary.cleanup)
        self.code = pathlib.Path(temporary.name) / "code"
        self.roadmap = pathlib.Path(temporary.name) / "roadmap"
        for area in ("Curves", "Surfaces"):
            (self.roadmap / "TauCetiRoadmap" / area).mkdir(parents=True)
            (self.roadmap / "TauCetiRoadmap" / area / "README.md").write_text(f"# {area}\n")
        self.env = dict(os.environ, GIT_AUTHOR_NAME="fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
                        GIT_COMMITTER_NAME="fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid")
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.code)], check=True, capture_output=True)
        self.initial = self.commit("initial", ["README.md"])
        patcher = patch.dict(os.environ, {COMMIT_SOURCES_ENV: SOURCE})
        patcher.start()
        self.addCleanup(patcher.stop)

    def commit(self, message, paths):
        for path in paths:
            target = self.code / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"{target.read_text() if target.exists() else ''}{message[:20]}\n")
        subprocess.run(["git", "-C", str(self.code), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.code), "commit", "-q", "-m", message], env=self.env, check=True,
                       capture_output=True)
        return window.git(["rev-parse", "HEAD"], self.code).strip()

    def report(self, area, to_sha):
        """Give `area` a recorded cursor at `to_sha`."""
        section = files.render_section(area, self.initial, to_sha, [1], "window", "body")
        (self.roadmap / "TauCetiRoadmap" / area / "PROGRESS.md").write_text(files.new_progress_file(area) + section)

    def plan(self, labels, area, documented=None):
        documented = documented or window.head_sha(self.code, "main")
        reader = SimpleNamespace(source_commit=lambda: documented, generation="pages/test",
                                 require_coverage=lambda paths: None, pin=lambda: {"source_commit": documented})
        with patch.object(docs, "discover", lambda: reader), \
                patch.object(gh, "closed_prs_for_areas",
                             lambda areas, **_: {a: {n: None for n in labels.get(a, [])} for a in areas}):
            return plan.build_plan(self.roadmap, self.code, open_prs=[], ref="main", min_prs=1, only_area=area,
                                   commits=[{"commit": {"committedDate": "2026-01-01T00:00:00Z"},
                                             "messageHeadline": "progress: X (2026-01-01)"}])

    def test_a_native_id_is_part_of_the_citation(self):
        citation = PullSource.parse(f"{SOURCE}#31")
        self.assertEqual((citation.native_id, citation.number), (7, 31))
        self.assertEqual(citation.key, "forgejo:https://forge.example/team/math@7#31")
        self.assertEqual(PullSource.parse(citation.key), citation)
        self.assertNotEqual(PullSource.parse("forgejo:https://forge.example/team/Math#31"), citation)
        for bad in (f"{SOURCE[:-2]}@0#31", f"{SOURCE[:-2]}@x#31", f"{SOURCE[:-2]}@#31", "team/Math@7#31"):
            with self.assertRaises(ProvenanceError, msg=bad):
                PullSource.parse(bad)

    def test_commit_sources_need_a_qualified_repository_and_its_native_id(self):
        self.assertEqual(commit_sources(SOURCE), {("forgejo:https://forge.example/team/math", 7)})
        self.assertEqual(commit_sources(""), frozenset())
        with patch.dict(os.environ):
            os.environ.pop(COMMIT_SOURCES_ENV)
            self.assertEqual(commit_sources(),
                             {("forgejo:https://git.taucetiproject.org/taucetiproject/tauceti", 1)})
        for bad in ("team/Math@7", "forgejo:https://forge.example/team/Math"):
            with self.assertRaisesRegex(ProvenanceError, COMMIT_SOURCES_ENV):
                commit_sources(bad)

    def test_a_configured_source_keeps_its_key_and_others_still_refuse(self):
        labelled = self.commit("feat: labelled (#5)", ["TauCeti/A.lean"])
        tip = self.commit(forgejo(31, "Curves"), ["TauCeti/B.lean"])
        self.assertEqual(window.window_prs(self.code, self.initial, tip), [key(31), 5])
        self.assertEqual(facts_commits(self.code, self.initial, tip, [key(31)]), {tip: key(31)})
        self.assertNotEqual(labelled, tip)
        with patch.dict(os.environ, {COMMIT_SOURCES_ENV: "forgejo:https://forge.example/team/Math@8"}):
            with self.assertRaisesRegex(window.GitError, "explicit migration"):
                window.window_prs(self.code, self.initial, tip)
        with patch.dict(os.environ, {COMMIT_SOURCES_ENV: "nonsense"}):
            with self.assertRaisesRegex(window.GitError, COMMIT_SOURCES_ENV):
                window.window_prs(self.code, self.initial, tip)

    def test_the_labeller_decides_each_pull_requests_roadmap(self):
        curves = self.commit(forgejo(31, "Curves"), ["TauCeti/Curves.lean"])
        self.commit(forgejo(32, "Curves"), ["TauCeti/X.lean", "scripts/tool.py"])  # infrastructure
        self.commit(forgejo(33, "Curves", extra='<!--tauceti-target:v1 {"focus":"Surfaces"}-->\n'),
                    ["TauCeti/Y.lean"])  # conflicting attribution
        first = self.commit(forgejo(34, "Surfaces"), ["TauCeti/Surfaces.lean"])
        self.commit(forgejo(34, "Surfaces", extra="\nRepair: split in two."), ["TauCeti/Surfaces/Part.lean"])
        self.commit(forgejo(35, "Surfaces"), ["TauCeti/S.lean"])
        self.commit(forgejo(35, "Curves"), ["TauCeti/S2.lean"])  # its two commits disagree
        self.commit("feat: labelled (#6)", ["TauCeti/Z.lean"])
        attribution = commit_attribution.Attribution(self.code, {"Curves", "Surfaces"})
        got = {area: attribution.through(area, "main") for area in ("Curves", "Surfaces")}
        # A pull request split over queue commits is placed by its earliest one.
        self.assertEqual(got, {"Curves": {key(31): curves}, "Surfaces": {key(34): first}})

    def test_a_roadmap_with_only_commit_attributed_work_is_planned_from_its_first_commit(self):
        self.commit("feat: labelled (#5)", ["TauCeti/A.lean"])
        before = window.head_sha(self.code, "main")
        first = self.commit(forgejo(31, "Curves"), ["TauCeti/B.lean"])
        self.commit(forgejo(32, "Curves"), ["TauCeti/C.lean"])
        got = self.plan({"Surfaces": [5]}, "Curves")
        self.assertEqual(got["prs"], [key(32), key(31)])
        self.assertEqual(got["from_sha"], before)
        self.assertNotEqual(first, before)

    def test_the_earlier_of_labelled_and_commit_attributed_work_starts_the_window(self):
        native = self.commit(forgejo(34, "Surfaces"), ["TauCeti/S.lean"])
        self.commit("feat: labelled (#5)", ["TauCeti/A.lean"])
        got = self.plan({"Surfaces": [5]}, "Surfaces")
        self.assertEqual(got["prs"], [5, key(34)])
        self.assertEqual(got["from_sha"], self.initial)
        self.assertEqual(window.first_parent_before(self.code, native), self.initial)

    def test_a_mixed_section_is_canonical_and_names_each_pull_request_where_it_lives(self):
        section = files.render_section("Curves", "a" * 40, "b" * 40, [key(31), 5, 3], "window", "body")
        parsed = files.parse_sections(section)
        self.assertEqual(parsed[0]["prs"], [3, 5, key(31)])
        self.assertEqual(files.pr_reference(5, "TauCeti"), "TauCeti#5")
        self.assertEqual(files.pr_reference(key(31), "TauCeti"), "https://forge.example/team/math/pulls/31")
        body = apply.pr_body({"roadmap": "Curves", "from_sha": "a" * 40, "to_sha": "b" * 40,
                              "prs": [key(31), 5]}, section)
        self.assertIn("https://forge.example/team/math/pulls/31", body)
        self.assertIn(f'"prs":[5,"{key(31)}"]', body)
        for bad in (f"{SOURCE}#31", key(31) + "-->", "forgejo:https://forge.example/team/math@7#031",
                    "TauCetiProject/TauCeti#5"):
            header = section.replace(f'"{key(31)}"', '"' + bad.replace('"', '') + '"')
            with self.assertRaises(files.FormatError, msg=bad):
                files.parse_sections(header)

    def test_the_collector_places_a_first_report_where_the_planner_does(self):
        native = self.commit(forgejo(34, "Surfaces"), ["TauCeti/S.lean"])
        self.commit("feat: labelled (#5)", ["TauCeti/A.lean"])
        self.commit(forgejo(31, "Curves"), ["TauCeti/C.lean"])
        tip = window.head_sha(self.code, "main")
        areas = set(plan.discover_areas(self.roadmap))
        for area, labels in (("Surfaces", {5: None}), ("Curves", {})):
            planned = self.plan({"Surfaces": [5]}, area)["from_sha"]
            collected = collect.bootstrap_from_clone(self.code, area, labels, ref="main", areas=areas, upto=tip)
            self.assertEqual(collected, planned, area)
        self.assertEqual(window.first_parent_before(self.code, native), self.initial)
        # Without the roadmap list or the report's end it cannot reproduce the planner, so it refuses.
        self.assertIsNone(collect.bootstrap_from_clone(self.code, "Curves", {}, ref="main", areas=None, upto=tip))
        self.assertIsNone(collect.bootstrap_from_clone(self.code, "Curves", {}, ref="main", areas=areas, upto=None))

    def test_unrelated_history_outside_the_window_cannot_stop_a_report(self):
        bad = "feat: broken ({n})\n\nSource-PR: not a citation"
        self.commit(bad.format(n="#9"), ["TauCeti/Old.lean"])  # before the cursor
        one = self.commit("feat: labelled (#1)", ["TauCeti/A.lean"])
        two = self.commit("feat: labelled (#2)", ["TauCeti/B.lean"])
        self.commit(bad.format(n="#3"), ["TauCeti/New.lean"])  # after the documented commit
        self.report("Curves", one)
        for sources in (SOURCE, ""):
            with patch.dict(os.environ, {COMMIT_SOURCES_ENV: sources}):
                got = self.plan({"Curves": [1, 2]}, "Curves", documented=two)
                self.assertEqual((got["from_sha"], got["prs"]), (one, [2]), sources)
        # An unreported area only commit attribution could start is set aside, not the whole plan.
        got = self.plan({"Curves": [1, 2]}, None, documented=two)
        self.assertEqual(got["roadmap"], "Curves")
        self.assertTrue(any(s.startswith("Surfaces: its first window cannot be placed") for s in got["skipped"]),
                        got["skipped"])

    def test_a_split_pull_request_is_classified_alike_on_both_sides_of_a_cursor(self):
        self.commit(forgejo(34, "Surfaces"), ["TauCeti/S.lean"])
        middle = self.commit(forgejo(34, "Surfaces"), ["scripts/tool.py"])
        tip = self.commit(forgejo(34, "Surfaces"), ["TauCeti/T.lean"])
        attribution = commit_attribution.Attribution(self.code, {"Surfaces"}, tip=tip)
        # Each part alone would read as roadmap work; with its infrastructure commit it is none.
        self.assertEqual(attribution.in_window("Surfaces", middle, tip), {})
        self.assertEqual(commit_attribution.Attribution(self.code, {"Surfaces"}, tip=middle)
                         .in_window("Surfaces", self.initial, window.first_parent_before(self.code, middle)), {})

    def test_a_cited_native_id_must_be_the_report_repository_s_own(self):
        from progress.native_source import history_options_from_record
        from progress.provenance import MigrationMap

        record = {"schema": "tauceti-progress.source/v1", "legacy_content": None, "legacy_through": None,
                  "content": {"forge": {"kind": "forgejo", "url": "https://forge.example"}, "name": "team/Math", "id": 8},
                  "roadmap": {"forge": {"kind": "forgejo", "url": "https://forge.example"}, "name": "team/Map", "id": 9}}
        history = history_options_from_record(record)
        self.assertEqual(history["repository_id"], 8)
        ours = self.commit(f"feat: ours (#40)\n\nSource-PR: forgejo:https://forge.example/team/Math@8#40", ["TauCeti/O.lean"])
        tip = self.commit(forgejo(31, "Curves"), ["TauCeti/P.lean"])  # the predecessor, ID 7
        with patch.dict(os.environ, {COMMIT_SOURCES_ENV: ""}):
            with self.assertRaisesRegex(window.GitError, "explicit migration"):
                window.window_prs(self.code, self.initial, tip, **history)
            self.assertEqual(window.window_prs(self.code, self.initial, ours, **history), [40])
            mapping = MigrationMap.parse({"schema": "tauceti-progress.migration/v1", "mappings": [
                {"from": f"{SOURCE}#31", "to": "forgejo:https://forge.example/team/Math@8#103"}]})
            self.assertEqual(window.window_prs(self.code, self.initial, tip, **history, migration=mapping), [103, 40])
        self.assertEqual(window.window_prs(self.code, self.initial, tip, **history), [key(31), 40])


def facts_commits(code, start, end, numbers):
    from progress import facts

    return facts.pr_commits(code, start, end, numbers)


if __name__ == "__main__":
    unittest.main()
