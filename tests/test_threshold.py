"""Tests for the `threshold` strategy, the active-over-archived area rule, and the label cache.

The rule: N PRs in a roadmap's window, T days since its last report. It qualifies when N > 0 and it
is declared complete, N + T > THRESHOLD, or it is not yet assessed; the gate's per-roadmap interval
still applies; unassessed roadmaps go first, then the largest N + T. The roadmap checkout is a real git repository, because "when was it last
reported" is read from git exactly as the gate's collector reads it from the API.
"""

import datetime
import json
import os
import pathlib
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from progress import gh, plan  # noqa: E402

failures = []


def check(name, fn):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        failures.append(name)
        print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    else:
        print(f"ok   {name}")


NOW = datetime.datetime(2026, 9, 30, 12, 0, 0, tzinfo=datetime.timezone.utc)


def ago(days=0.0, hours=0.0):
    return NOW - datetime.timedelta(days=days, hours=hours)


# ----- assess: the rule for one roadmap --------------------------------------------------------


def test_no_new_prs_never_qualifies():
    ok, _days, note, when = plan.assess(0, ago(days=40), False, NOW)
    assert not ok and when is None and "no new" in note
    ok, *_ = plan.assess(0, None, True, NOW)
    assert not ok, "a complete roadmap still needs something to report"


def test_n_plus_t_must_exceed_the_threshold():
    ok, days, note, _ = plan.assess(4, ago(days=6.5), False, NOW)
    assert ok and abs(days - 6.5) < 1e-9 and "10.5 > 10" in note, note
    ok, _days, note, when = plan.assess(7, ago(days=3), False, NOW)
    assert not ok, "N + T = 10 is not > 10"
    assert when == ago(days=3) + datetime.timedelta(days=3), when
    assert "needs > 10" in note


def test_a_busy_roadmap_qualifies_quickly():
    ok, *_ = plan.assess(11, ago(hours=7), False, NOW)
    assert ok


def test_a_quiet_roadmap_qualifies_after_a_while():
    ok, _days, _note, when = plan.assess(1, ago(days=8.5), False, NOW)
    assert not ok and when == ago(days=8.5) + datetime.timedelta(days=9), when
    ok, *_ = plan.assess(1, ago(days=8.5), False, NOW + datetime.timedelta(days=0.51))
    assert ok, "one PR and just over nine days"


def test_a_complete_roadmap_needs_only_one_pr():
    ok, _days, note, _ = plan.assess(1, ago(days=1), True, NOW)
    assert ok and note == "declared complete"


def test_an_unassessed_roadmap_needs_only_one_pr():
    ok, days, note, _ = plan.assess(1, ago(days=1), False, NOW, unassessed="Area's README changed since its report")
    assert ok and note == "not yet assessed: Area's README changed since its report", note
    ok, *_ = plan.assess(1, ago(days=1), False, NOW)
    assert not ok, "the same roadmap, assessed, waits for N + T > 10"


def test_a_never_reported_roadmap_counts_t_from_its_readme():
    ok, days, note, _ = plan.assess(3, None, False, NOW, unassessed="no report yet", added=ago(days=20))
    assert ok and abs(days - 20) < 1e-9 and "no report yet" in note, (ok, days, note)
    ok, days, _note, _ = plan.assess(1, None, False, NOW, unassessed="no report yet")
    assert ok and days is None


def test_the_gate_interval_holds_for_an_unassessed_roadmap_too():
    ok, _days, note, when = plan.assess(5, ago(hours=1), False, NOW, unassessed="README changed")
    assert not ok and "merge gate" in note and when == ago(hours=1) + datetime.timedelta(hours=6), (note, when)


def test_the_gate_interval_holds_for_every_roadmap():
    """The merge gate refuses a second report for a roadmap within MIN_REPORT_INTERVAL_HOURS, so the
    planner must not choose one, however many PRs it has and even when it is complete."""
    for complete in (False, True):
        ok, _days, note, when = plan.assess(80, ago(hours=2), complete, NOW)
        assert not ok and "merge gate" in note, note
        assert when == ago(hours=2) + datetime.timedelta(hours=plan.MIN_REPORT_INTERVAL_HOURS), when
    ok, *_ = plan.assess(80, ago(hours=6.01), False, NOW)
    assert ok


def test_the_gate_wait_and_the_rule_wait_combine():
    # 2 PRs, reported 1 h ago: the gate allows it at +6 h but the rule only at +8 days.
    ok, _days, _note, when = plan.assess(2, ago(hours=1), False, NOW)
    assert not ok and when == ago(hours=1) + datetime.timedelta(days=8), when


def test_the_threshold_is_a_parameter():
    ok, *_ = plan.assess(3, ago(days=3), False, NOW, threshold=5)
    assert ok
    ok, *_ = plan.assess(3, ago(days=3), False, NOW, threshold=6)
    assert not ok


# ----- discover_areas: an area under both parents ---------------------------------------------


def _roadmap_repo():
    d = pathlib.Path(tempfile.mkdtemp(prefix="threshold-roadmap-"))
    subprocess.run(["git", "init", "-q", "-b", "main", str(d)], check=True)
    return d


def _commit(repo, path, text, when, message="c"):
    f = repo / path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@x", "GIT_AUTHOR_DATE": when.isoformat(), "GIT_COMMITTER_DATE": when.isoformat()}
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", message], check=True, env=env)


def test_the_active_roadmap_wins_over_an_archived_one_of_the_same_name():
    repo = _roadmap_repo()
    _commit(repo, "Completed/IntegralLattices/README.md", "# old\n", ago(days=30))
    _commit(repo, "TauCetiRoadmap/IntegralLattices/README.md", "# new\n", ago(days=20))
    _commit(repo, "Completed/EffectiveBounds/README.md", "# done\n", ago(days=20))
    areas = plan.discover_areas(repo)
    assert areas["IntegralLattices"] == "TauCetiRoadmap/IntegralLattices", areas
    assert areas["EffectiveBounds"] == "Completed/EffectiveBounds", areas
    assert plan.is_complete(areas["EffectiveBounds"]) and not plan.is_complete(areas["IntegralLattices"])


def test_last_report_is_the_newest_commit_touching_progress_md():
    repo = _roadmap_repo()
    _commit(repo, "TauCetiRoadmap/A/README.md", "# A\n", ago(days=50))
    assert plan.last_report_at(repo, "TauCetiRoadmap/A") is None
    _commit(repo, "TauCetiRoadmap/A/PROGRESS.md", "one\n", ago(days=12))
    _commit(repo, "TauCetiRoadmap/A/README.md", "# A, edited\n", ago(days=2))  # not a report
    assert plan.last_report_at(repo, "TauCetiRoadmap/A") == ago(days=12)


# ----- _choose_by_threshold: ranking and the table ---------------------------------------------


def _candidate(area, n, rel_dir=None, status="a report"):
    """A candidate whose README (no layer headings in `_setup`) counts as assessed by any report."""
    return {"area": area, "rel_dir": rel_dir or f"TauCetiRoadmap/{area}", "from_sha": "a" * 40,
            "prs": list(range(n)), "bootstrapped": False, "status_text": status}


def _setup(reports):
    """A roadmap repo where each `(area, rel_dir, days_ago or None)` has a README and, unless None, a
    PROGRESS.md last committed that long ago."""
    repo = _roadmap_repo()
    areas = {}
    for area, rel_dir, days in reports:
        _commit(repo, f"{rel_dir}/README.md", f"# {area}\n", ago(days=90))
        if days is not None:
            _commit(repo, f"{rel_dir}/PROGRESS.md", f"{area}\n", ago(days=days))
        areas[area] = rel_dir
    return repo, areas


def test_the_largest_n_plus_t_wins_among_the_qualifying():
    repo, areas = _setup([("Busy", "TauCetiRoadmap/Busy", 0.1),     # 40 PRs but reported 2.4 h ago
                          ("Big", "TauCetiRoadmap/Big", 3.0),       # 30 PRs + 3 days = 33
                          ("Old", "TauCetiRoadmap/Old", 40.0),      # 5 PRs + 40 days = 45
                          ("Small", "TauCetiRoadmap/Small", 1.0)])  # 3 PRs, 1 day: 4 < 10
    cands = [_candidate("Busy", 40), _candidate("Big", 30), _candidate("Old", 5), _candidate("Small", 3)]
    best, ranked, reason = plan._choose_by_threshold(areas, cands, {}, repo, "b" * 40, NOW, 10.0, 6.0, None)
    assert best["area"] == "Old", best["area"]
    assert [c["area"] for c in ranked] == ["Old", "Big"]
    assert "Old has 5 PR(s)" in reason and "N+T (45.0)" in reason and "2 qualifying" in reason, reason


def test_an_unassessed_roadmap_qualifies_and_ranks_by_n_plus_t():
    repo, areas = _setup([("Stale", "TauCetiRoadmap/Stale", 2.0), ("New", "TauCetiRoadmap/New", None),
                          ("Quiet", "TauCetiRoadmap/Quiet", 1.0)])
    cands = [_candidate("Stale", 2, status=None), _candidate("New", 1, status=None), _candidate("Quiet", 2)]
    best, ranked, _ = plan._choose_by_threshold(areas, cands, {}, repo, "b" * 40, NOW, 10.0, 6.0, None)
    # New was added 90 days ago (its README's commit in `_setup`), so its N + T is 91; Stale's is 4.
    assert [c["area"] for c in ranked] == ["New", "Stale"], [c["area"] for c in ranked]
    assert "never reported, added 90.0 days ago" in plan._choose_by_threshold(
        areas, cands, {}, repo, "b" * 40, NOW, 10.0, 6.0, None)[2]


def test_an_unassessed_roadmap_goes_before_any_assessed_one():
    repo, areas = _setup([("Busy", "TauCetiRoadmap/Busy", 3.0), ("Edited", "TauCetiRoadmap/Edited", 0.5)])
    # Edited's README names layers and changed after its report, as a roadmap PR does to it.
    _commit(repo, "TauCetiRoadmap/Edited/README.md", README_WITH_LAYERS + "\nA new item.\n", ago(hours=2))
    cands = [_candidate("Busy", 30), _candidate("Edited", 1, status=_status_for(README_WITH_LAYERS, "Edited"))]
    best, ranked, reason = plan._choose_by_threshold(areas, cands, {}, repo, "b" * 40, NOW, 10.0, 6.0, None)
    assert [c["area"] for c in ranked] == ["Edited", "Busy"], [c["area"] for c in ranked]
    assert "Edited's README changed since its report" in reason, reason
    assert "unassessed roadmaps go first" in reason and "1 unassessed among 2 qualifying" in reason, reason


def test_ties_go_to_the_roadmap_reported_longest_ago():
    repo, areas = _setup([("A", "TauCetiRoadmap/A", 5.0), ("B", "TauCetiRoadmap/B", 9.0),
                          ("C", "TauCetiRoadmap/C", None)])
    cands = [_candidate("A", 12), _candidate("B", 12), _candidate("C", 12)]
    _best, ranked, _ = plan._choose_by_threshold(areas, cands, {}, repo, "b" * 40, NOW, 10.0, 6.0, None)
    assert [c["area"] for c in ranked] == ["C", "B", "A"], [c["area"] for c in ranked]


def test_the_table_accounts_for_every_roadmap_and_is_written_when_nothing_qualifies():
    repo, areas = _setup([("Quiet", "TauCetiRoadmap/Quiet", 2.0), ("Idle", "TauCetiRoadmap/Idle", 4.0),
                          ("Done", "Completed/Done", 30.0)])
    out = pathlib.Path(tempfile.mkdtemp()) / "table.json"
    try:
        plan._choose_by_threshold(areas, [_candidate("Quiet", 3)], {"Idle": "nothing new since abc1234"},
                                  repo, "b" * 40, NOW, 10.0, 6.0, out)
    except plan.NotDue as exc:
        assert "no roadmap qualifies yet" in str(exc) and "Quiet" in str(exc), exc
    else:
        raise AssertionError("nothing qualifies, so NotDue")
    table = json.loads(out.read_text())
    rows = {r["area"]: r for r in table["rows"]}
    assert set(rows) == {"Quiet", "Idle", "Done"}, rows
    assert rows["Idle"]["note"] == "nothing new since abc1234" and rows["Idle"]["prs"] == 0
    assert rows["Done"]["complete"] is True and rows["Done"]["qualifies"] is False
    # Quiet: 3 PRs, reported 2 days ago, qualifies once T > 7, i.e. five days from now.
    assert rows["Quiet"]["qualifies_from"] == (NOW + datetime.timedelta(days=5)).isoformat(timespec="seconds")
    assert table["next_qualifies_at"] == rows["Quiet"]["qualifies_from"]
    assert table["chosen"] is None


def test_a_complete_roadmap_with_one_pr_is_chosen():
    repo, areas = _setup([("Done", "Completed/Done", 30.0), ("Quiet", "TauCetiRoadmap/Quiet", 1.0)])
    best, _r, _ = plan._choose_by_threshold(areas, [_candidate("Done", 1, "Completed/Done"), _candidate("Quiet", 2)],
                                            {}, repo, "b" * 40, NOW, 10.0, 6.0, None)
    assert best["area"] == "Done"


def test_under_threshold_anyone_s_fresh_report_holds_its_area():
    """Checked through build_plan's own call, with the network reads replaced: a stranger's report
    two hours old holds its area, one twenty hours old does not."""
    from progress import window as window_mod
    fresh = {"number": 519, "headRefName": "progress/ae69ef9-163ce80/Busy", "createdAt": "2026-09-30T10:00:00Z",
             "headRepositoryOwner": {"login": "ldct"}}
    stale = {"number": 493, "headRefName": "progress/6bc3780-b1ab119/Old", "createdAt": "2026-09-29T16:00:00Z",
             "headRepositoryOwner": {"login": "ldct"}}
    repo, _areas = _setup([("Busy", "TauCetiRoadmap/Busy", 20.0), ("Old", "TauCetiRoadmap/Old", 20.0)])
    saved = (plan.docs_source_commit, window_mod.head_sha, window_mod.is_ancestor, plan.area_window,
             gh.merged_prs_for_area, plan.read_area_files, plan.files.cursor, window_mod.commit_date)
    plan.docs_source_commit = lambda: "b" * 40
    window_mod.head_sha = lambda *a, **k: "b" * 40
    window_mod.is_ancestor = lambda *a, **k: True
    plan.area_window = lambda code_dir, prs, f, t: list(prs)
    gh.merged_prs_for_area = lambda area, repo=None: [1, 2, 3]
    plan.read_area_files = lambda roadmap_dir, rel_dir: ("status", "log")
    plan.files.cursor = lambda text: "a" * 40
    window_mod.commit_date = lambda *a, **k: "2026-09-30T00:00:00+00:00"
    try:
        try:
            p = plan.build_plan(repo, repo, open_prs=[fresh, stale], now=NOW, strategy="threshold")
        except plan.NotDue as exc:
            raise AssertionError(f"Old should qualify: {exc}")
        assert p["roadmap"] == "Old", p["roadmap"]
        assert any("Busy: PR #519 is still open" in s for s in p["skipped"]), p["skipped"]
        assert any("#493" in s and "no longer marks the area in flight" in s for s in p["skipped"]), p["skipped"]
    finally:
        (plan.docs_source_commit, window_mod.head_sha, window_mod.is_ancestor, plan.area_window,
         gh.merged_prs_for_area, plan.read_area_files, plan.files.cursor, window_mod.commit_date) = saved


# ----- assessment_gap: what the Progress page shows as unassessed --------------------------------


README_WITH_LAYERS = "# Area\n\n## Layer 0: foundations\n\n## Layer 1: the summit\n"


def _status_for(readme, roadmap="Area"):
    from progress import layers as layers_mod
    header = {"roadmap": roadmap, "to_sha": "b" * 40, "readme_sha": layers_mod.readme_sha(readme),
              "layers": [{"id": "Layer 0", "state": "done"}, {"id": "Layer 1", "state": "partial"}]}
    return f"<!--tauceti-coverage:v1 {json.dumps(header)}-->\n# Status: {roadmap}\n"


def test_assessment_gap_reads_the_coverage_header_against_the_readme():
    repo = _roadmap_repo()
    _commit(repo, "TauCetiRoadmap/Area/README.md", README_WITH_LAYERS, ago(days=30))
    assert plan.assessment_gap(repo, "Area", "TauCetiRoadmap/Area", None) == "no report yet"
    assert plan.assessment_gap(repo, "Area", "TauCetiRoadmap/Area", "# Status\nprose only\n") == \
        "its report does not assess Area"
    assert plan.assessment_gap(repo, "Area", "TauCetiRoadmap/Area", _status_for(README_WITH_LAYERS)) is None
    _commit(repo, "TauCetiRoadmap/Area/README.md", README_WITH_LAYERS + "\nA clarification.\n", ago(days=1))
    assert plan.assessment_gap(repo, "Area", "TauCetiRoadmap/Area", _status_for(README_WITH_LAYERS)) == \
        "Area's README changed since its report"


def test_assessment_gap_covers_every_sub_roadmap():
    repo = _roadmap_repo()
    _commit(repo, "TauCetiRoadmap/Area/README.md", "# Area\n\nAn index.\n", ago(days=30))
    _commit(repo, "TauCetiRoadmap/Area/Sub/README.md", README_WITH_LAYERS, ago(days=30))
    _commit(repo, "TauCetiRoadmap/Area/Sub/Suggested.lean", "-- sub\n", ago(days=30))
    assert plan.assessment_gap(repo, "Area", "TauCetiRoadmap/Area", "# Status\n") == \
        "its report does not assess Area/Sub"
    assert plan.assessment_gap(repo, "Area", "TauCetiRoadmap/Area", _status_for(README_WITH_LAYERS, "Area/Sub")) is None


def test_a_roadmap_without_layers_counts_as_assessed_by_any_report():
    repo = _roadmap_repo()
    _commit(repo, "TauCetiRoadmap/Area/README.md", "# Area\n\nNo layer headings.\n", ago(days=30))
    assert plan.assessment_gap(repo, "Area", "TauCetiRoadmap/Area", "# Status\nprose\n") is None


def test_roadmap_added_at_is_the_readme_s_first_commit():
    repo = _roadmap_repo()
    _commit(repo, "TauCetiRoadmap/Area/README.md", "# Area\n", ago(days=50))
    _commit(repo, "TauCetiRoadmap/Area/README.md", "# Area, edited\n", ago(days=2))
    assert plan.roadmap_added_at(repo, "TauCetiRoadmap/Area") == ago(days=50)


# ----- LabelCache -----------------------------------------------------------------------------


class Fake:
    def __init__(self, areas, merged=None):
        self.areas, self.merged = areas, merged or {}
        self.area_calls, self.since_calls = [], []

    def area_fn(self, area, repo=None):
        self.area_calls.append(area)
        return sorted(self.areas.get(area, []), reverse=True)

    def since_fn(self, since, repo=None, limit=None):
        self.since_calls.append(since)
        return dict(self.merged)


def _cache(path, fake, now=NOW):
    return gh.LabelCache(path, now=now, since_fn=fake.since_fn, area_fn=fake.area_fn)


def test_a_new_cache_fetches_each_area_once_and_searches_nothing():
    path = pathlib.Path(tempfile.mkdtemp()) / "labels.json"
    fake = Fake({"A": [3, 1], "B": [2]})
    c = _cache(path, fake)
    assert c.for_area("A") == [3, 1] and c.for_area("B") == [2] and c.for_area("A") == [3, 1]
    assert fake.area_calls == ["A", "B"] and fake.since_calls == []
    c.save()
    assert json.loads(path.read_text())["areas"] == {"A": [3, 1], "B": [2]}


def test_a_later_run_asks_only_for_what_merged_since():
    path = pathlib.Path(tempfile.mkdtemp()) / "labels.json"
    c = _cache(path, Fake({"A": [3, 1], "B": [2]}))
    c.for_area("A"), c.for_area("B")
    c.save()
    later = NOW + datetime.timedelta(hours=3)
    fake = Fake({}, merged={7: ["roadmap/A", "bug"], 8: ["roadmap/none"], 9: ["roadmap/C"], 3: ["roadmap/A"]})
    c2 = _cache(path, fake, now=later)
    assert c2.for_area("A") == [7, 3, 1], c2.for_area("A")
    assert c2.for_area("B") == [2]
    assert fake.since_calls == [NOW - gh.LabelCache.MARGIN], fake.since_calls
    assert fake.area_calls == []
    # An area the cache has never seen is fetched whole, not pieced together from the search.
    fake.areas = {"C": [9, 5]}
    assert c2.for_area("C") == [9, 5] and fake.area_calls == ["C"]


def test_a_full_search_page_starts_the_cache_over():
    path = pathlib.Path(tempfile.mkdtemp()) / "labels.json"
    c = _cache(path, Fake({"A": [1]}))
    c.for_area("A")
    c.save()
    fake = Fake({"A": [5, 1]}, merged={n: ["roadmap/A"] for n in range(gh.LabelCache.SEARCH_LIMIT)})
    c2 = _cache(path, fake, now=NOW + datetime.timedelta(hours=1))
    assert c2.for_area("A") == [5, 1] and fake.area_calls == ["A"]


def test_a_day_old_or_unreadable_cache_is_refetched():
    path = pathlib.Path(tempfile.mkdtemp()) / "labels.json"
    c = _cache(path, Fake({"A": [1]}))
    c.for_area("A")
    c.save()
    fake = Fake({"A": [4, 1]})
    c2 = _cache(path, fake, now=NOW + datetime.timedelta(hours=25))
    assert c2.for_area("A") == [4, 1] and fake.area_calls == ["A"] and fake.since_calls == []
    path.write_text("{not json")
    fake = Fake({"A": [4, 1]})
    assert _cache(path, fake).for_area("A") == [4, 1] and fake.area_calls == ["A"]


for _name, _fn in sorted(globals().items()):
    if _name.startswith("test_") and callable(_fn):
        check(_name, _fn)

print()
if failures:
    print(f"{len(failures)} failure(s): {', '.join(failures)}")
    sys.exit(1)
print("all tests passed")
