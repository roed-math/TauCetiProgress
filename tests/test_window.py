"""Tests for window arithmetic and the plan decision.

The git-touching parts are exercised against a real throwaway repository built in a temp dir, so
the ancestry and half-open-range behaviour is tested against actual git rather than a mock that
could encode the same mistake twice.
"""

import datetime
import os
import pathlib
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from progress import files, plan, window  # noqa: E402

failures = []


def check(name, fn):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        failures.append(name)
        print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    else:
        print(f"ok   {name}")


def raises(exc_type, fn, needle=None):
    try:
        fn()
    except exc_type as exc:
        if needle and needle not in str(exc):
            raise AssertionError(f"expected {needle!r} in {str(exc)!r}") from None
        return
    raise AssertionError(f"expected {exc_type.__name__}, none raised")


# ----- subject parsing -------------------------------------------------------------------------


def test_pr_number_of_subject():
    assert window.pr_number_of_subject("feat: add products (#1433)") == 1433
    assert window.pr_number_of_subject("Merge pull request #62 from TauCetiProject/x") == 62
    assert window.pr_number_of_subject("chore: no number here") is None
    # A number in the middle is not a merge marker; only the trailing form counts.
    assert window.pr_number_of_subject("fix: handle (#12) in parser") is None


SOURCE = "Source-PR: TauCetiProject/TauCeti#"


def test_pr_number_of_commit_reads_the_source_citation():
    number = window.pr_number_of_commit
    assert number("feat: queue landed", f"Prose.\n\n{SOURCE}7\n") == 7
    assert number("feat: queue landed (#7)", f"{SOURCE}7") == 7
    assert number("feat: x", "Source-PR: github:https://github.com/TauCetiProject/TauCeti#7") == 7
    assert number("feat: x", "source-pr:\ttaucetiproject/tauceti#7") == 7
    # Repair notes are appended below the citation, so it is not in git's trailer block.
    assert number("feat: x (#7)", f"{SOURCE}7\nAccepted-Head: abc\n\nRepair explanation.") == 7
    assert number("feat: x", f"{SOURCE}7\n{SOURCE}7") == 7
    assert number("feat: plain squash (#9)", "No citation here.") == 9
    assert number("chore: nothing", "") is None
    # Prose and lookalike fields are not citations.
    for body in ("Source-PR trailers now carry identity", "Source-PR-Notes: x", "Source-PR : x",
                 "Source-PR:", "Source-PR: ", "  Source-PR: TauCetiProject/TauCeti#7"):
        assert number("feat: x (#9)", body) == 9, body


def test_pr_number_of_commit_refuses_what_it_cannot_attribute():
    number = window.pr_number_of_commit
    raises(window.GitError, lambda: number("feat: x", "Source-PR: invalid"), "unreadable")
    raises(window.GitError, lambda: number("feat: x", "Source-PR: TauCetiProject/TauCeti#07"), "unreadable")
    raises(window.GitError, lambda: number("feat: x", f"{SOURCE}7\n{SOURCE}8"), "more than one")
    raises(window.GitError, lambda: number("feat: x (#8)", f"{SOURCE}7"), "subject says #8")
    raises(window.GitError, lambda: number("feat: x", "Source-PR: other/Repo#7"), "other/repo")
    raises(window.GitError,
           lambda: number("feat: x", "Source-PR: forgejo:https://forge.example/team/Math#7"), "unreadable")


# ----- a real git repo -------------------------------------------------------------------------


def make_repo(tmp, subjects):
    """A repo whose mainline is one commit per subject, oldest first. Returns [sha] aligned to it."""
    subprocess.run(["git", "init", "-q", "-b", "main", tmp], check=True, capture_output=True)
    # Inherit the real environment (git must stay on PATH) and only pin identity and dates, so
    # commit SHAs are reproducible without breaking the tool lookup.
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@e",
        "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@e",
        "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
    }
    shas = []
    for i, subject in enumerate(subjects):
        (pathlib.Path(tmp) / f"f{i}").write_text(str(i))
        subprocess.run(["git", "-C", tmp, "add", "-A"], check=True, capture_output=True, env=env)
        subprocess.run(["git", "-C", tmp, "commit", "-q", "-m", subject],
                       check=True, capture_output=True, env=env)
        shas.append(window.git(["rev-parse", "HEAD"], tmp).strip())
    return shas


def commit_messages(tmp, messages):
    """Empty mainline commits with full messages (subject and body), oldest first. Returns [sha]."""
    subprocess.run(["git", "init", "-q", "-b", "main", tmp], check=True, capture_output=True)
    env = {**os.environ, "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@e",
           "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@e"}
    shas = []
    for message in messages:
        subprocess.run(["git", "-C", tmp, "commit", "-q", "--allow-empty", "-F", "-"],
                       input=message, text=True, check=True, capture_output=True, env=env)
        shas.append(window.git(["rev-parse", "HEAD"], tmp).strip())
    return shas


def test_a_commit_cited_only_by_source_pr_is_in_its_window():
    """The queue's first batch (2026-11-05): no `(#N)` in the subject, only the citation.

    Read from subjects alone, #2 is in no window, and since windows only move forward no later
    report picks it up (TauCetiProgress#58).
    """
    with tempfile.TemporaryDirectory() as tmp:
        shas = commit_messages(tmp, [
            "init",
            "feat: a (#1)",
            f"feat: landed by the queue\n\nA long description.\n\nRoadmap: X\n{SOURCE}2\n",
            f"feat: repaired by the queue (#3)\n\n{SOURCE}3\n\nRepair notes.\n",
            "chore: no pull request",
        ])
        assert window.first_parent_prs(tmp, "main") == [(shas[3], 3), (shas[2], 2), (shas[1], 1)]
        assert window.window_prs(tmp, shas[0], shas[4]) == [3, 2, 1]
        assert window.window_prs(tmp, shas[1], shas[2]) == [2]
        assert window.find_merge_commit(tmp, 2, ref="main") == shas[2]
        assert window.earliest_merged(tmp, [2, 3], ref="main") == (2, shas[2])
        # GitHub shows such a PR as closed, not merged, so it has no native merge commit.
        assert plan.unaccounted_prs(tmp, {2: None, 3: None}, ref="main") == []
        assert plan.area_window(tmp, {2: None}, shas[0], shas[4]) == [2]


def test_a_pr_landed_twice_in_one_window_is_listed_once():
    """98 of that batch were empty second landings of PRs Bors had merged the day before."""
    with tempfile.TemporaryDirectory() as tmp:
        shas = commit_messages(tmp, ["init", "feat: a (#1)", "feat: b (#2)", f"feat: a\n\n{SOURCE}1\n"])
        assert window.window_prs(tmp, shas[0], shas[3]) == [1, 2]
        assert window.earliest_merged(tmp, [1], ref="main") == (1, shas[1])


def test_an_unattributable_commit_refuses_the_whole_read_and_names_itself():
    with tempfile.TemporaryDirectory() as tmp:
        shas = commit_messages(tmp, ["init", f"feat: a (#1)\n\n{SOURCE}2\n", "feat: b (#3)"])
        raises(window.GitError, lambda: window.window_prs(tmp, shas[0], shas[2]), shas[1][:10])
        assert window.window_prs(tmp, shas[1], shas[2]) == [3]


def test_window_is_half_open_and_excludes_from_sha():
    with tempfile.TemporaryDirectory() as tmp:
        shas = make_repo(tmp, ["init", "a (#1)", "b (#2)", "c (#3)"])
        # (shas[1], shas[3]] must contain #2 and #3, and must NOT contain #1.
        got = window.window_prs(tmp, shas[1], shas[3])
        assert got == [3, 2], got
        # The whole history from the root's parent is not expressible, which is exactly why
        # bootstrap uses first_parent_before on the earliest *merge*, not on the root.
        got_all = window.window_prs(tmp, shas[0], shas[3])
        assert got_all == [3, 2, 1], got_all


def test_bootstrap_parent_includes_the_first_pr():
    with tempfile.TemporaryDirectory() as tmp:
        shas = make_repo(tmp, ["init", "a (#1)", "b (#2)"])
        merge = window.find_merge_commit(tmp, 1, ref="main")
        assert merge == shas[1], merge
        from_sha = window.first_parent_before(tmp, merge)
        assert from_sha == shas[0]
        # With the parent as cursor, #1 is reported. With the merge itself, it would be lost.
        assert window.window_prs(tmp, from_sha, shas[2]) == [2, 1]
        assert window.window_prs(tmp, merge, shas[2]) == [2]


def test_ancestry_is_asserted():
    with tempfile.TemporaryDirectory() as tmp:
        shas = make_repo(tmp, ["init", "a (#1)"])
        assert window.is_ancestor(tmp, shas[0], shas[1]) is True
        assert window.is_ancestor(tmp, shas[1], shas[0]) is False
        # A cursor that is not an ancestor means rewritten history or a foreign cursor. Refuse.
        raises(window.GitError, lambda: window.window_prs(tmp, shas[1], shas[0]), "not an ancestor")


def test_consecutive_windows_tile_with_no_gap_or_overlap():
    with tempfile.TemporaryDirectory() as tmp:
        shas = make_repo(tmp, ["init", "a (#1)", "b (#2)", "c (#3)", "d (#4)"])
        w1 = window.window_prs(tmp, shas[0], shas[2])
        w2 = window.window_prs(tmp, shas[2], shas[4])
        assert w1 == [2, 1], w1
        assert w2 == [4, 3], w2
        assert not set(w1) & set(w2), "windows must not overlap"
        assert set(w1) | set(w2) == {1, 2, 3, 4}, "windows must leave no gap"


def test_head_sha_and_commit_date():
    with tempfile.TemporaryDirectory() as tmp:
        shas = make_repo(tmp, ["init", "a (#1)"])
        assert window.head_sha(tmp, ref="main") == shas[-1]
        assert window.commit_date(tmp, shas[-1]).startswith("2026-")


# ----- cadence ---------------------------------------------------------------------------------


NOW = datetime.datetime(2026, 7, 30, 12, 0, tzinfo=datetime.timezone.utc)


def test_cadence_never_updated():
    reason = plan.check_cadence([("2026-07-30T11:00:00Z", "feat: something else (#1)")], now=NOW)
    assert "ever" in reason, reason


def test_cadence_too_recent_raises():
    commits = [("2026-07-30T09:00:00Z", "progress: PDE 2026-07-30 (#9)")]
    raises(plan.NotDue, lambda: plan.check_cadence(commits, now=NOW), "3.0h ago")


def test_cadence_old_enough_passes():
    commits = [("2026-07-28T12:00:00Z", "progress: PDE (#9)")]
    reason = plan.check_cadence(commits, now=NOW)
    assert "48.0h" in reason, reason


def test_cadence_ignores_non_progress_commits():
    commits = [
        ("2026-07-30T11:00:00Z", "doc: sharpen a target (#91)"),
        ("2026-07-27T12:00:00Z", "progress: PDE (#9)"),
    ]
    reason = plan.check_cadence(commits, now=NOW)
    assert "72.0h" in reason, reason


# ----- area discovery --------------------------------------------------------------------------


def test_discover_areas_matches_canonical_rule():
    """Shaped after the real origin/main tree: nested RepresentationTheory, a Completed area, a
    references/ dir that must NOT count, and an area with no Suggested.lean."""
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        for rel in [
            "TauCetiRoadmap/PDE",
            "TauCetiRoadmap/ContourIntegration",
            "TauCetiRoadmap/OneParameterSemigroups",     # README only, no Suggested.lean
            "TauCetiRoadmap/RepresentationTheory",
            "TauCetiRoadmap/RepresentationTheory/RootSystems",   # nested: not a top-level area
            "TauCetiRoadmap/GeometricTopology",
            "TauCetiRoadmap/GeometricTopology/references",       # has a README but is not an area
            "Completed/EffectiveBounds",
        ]:
            (root / rel).mkdir(parents=True)
            (root / rel / "README.md").write_text("#")
        (root / "TauCetiRoadmap/PDE/Suggested.lean").write_text("-- x")
        # A directory with no README is not an area.
        (root / "TauCetiRoadmap/NotAnArea").mkdir()

        areas = plan.discover_areas(root)
        assert set(areas) == {
            "PDE", "ContourIntegration", "OneParameterSemigroups", "RepresentationTheory",
            "GeometricTopology", "EffectiveBounds",
        }, sorted(areas)
        assert areas["PDE"] == "TauCetiRoadmap/PDE"
        assert areas["EffectiveBounds"] == "Completed/EffectiveBounds"
        assert "RootSystems" not in areas, "nested sub-roadmaps are not separate labelled areas"
        assert "references" not in areas


def test_read_area_files_missing_is_none():
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / "TauCetiRoadmap/PDE").mkdir(parents=True)
        s, p = plan.read_area_files(root, "TauCetiRoadmap/PDE")
        assert s is None and p is None
        (root / "TauCetiRoadmap/PDE/PROGRESS.md").write_text("hi")
        s, p = plan.read_area_files(root, "TauCetiRoadmap/PDE")
        assert s is None and p == "hi"


# ----- attribution and duplicate rejection -----------------------------------------------------


def test_area_window_is_git_range_intersect_label_set():
    """Attribution is an intersection: git says which PRs are in the range, one label query per
    area says which PRs belong to it. #2 here is `roadmap/none`, so it is absent from the set."""
    with tempfile.TemporaryDirectory() as tmp:
        shas = make_repo(tmp, ["init", "a (#1)", "b (#2)", "c (#3)"])
        area_prs = {1, 3}
        got = plan.area_window(tmp, area_prs, shas[0], shas[3])
        assert got == [3, 1], got
        # A PR labelled for the area but outside the window must not appear.
        got_narrow = plan.area_window(tmp, area_prs, shas[2], shas[3])
        assert got_narrow == [3], got_narrow


def test_already_reported_prs_are_excluded():
    """A relabel after a section landed must not double-report. The section header records the PR
    numbers, and `reported_prs` is the authority."""
    log = files.new_progress_file("PDE") + files.render_section("PDE", "a" * 40, "b" * 40, [1, 2],
                                                                "w", "x")
    assert files.reported_prs(log) == {1, 2}
    fresh = [n for n in [3, 2, 1] if n not in files.reported_prs(log)]
    assert fresh == [3], fresh



def test_earliest_merged_is_by_merge_order_not_by_number():
    """The bug this exists to prevent, reproduced exactly.

    Numbers are assigned when a pull request is OPENED. If #100 opens first but merges after #101,
    starting a roadmap from the commit before #100's merge puts the cursor past #101 -- and windows
    only move forward, so #101 becomes unreportable for good. Two of the fourteen live roadmaps had
    this shape (RepresentationTheory #1227 vs #1228, OneParameterSemigroups #273 vs #276).
    """
    import tempfile, subprocess, os
    with tempfile.TemporaryDirectory() as d:
        def run(*args):
            subprocess.run(args, cwd=d, check=True, capture_output=True)
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e")
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=d, check=True, capture_output=True)
        for subject in ("root", "feat: later-numbered merges first (#101)",
                        "feat: lower-numbered merges second (#100)"):
            subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", subject],
                           cwd=d, check=True, capture_output=True, env=env)
        got = window.earliest_merged(d, [100, 101], ref="main")
        assert got is not None and got[0] == 101, got
        # And the cursor derived from it is the commit BEFORE #101, so #101 is inside the window.
        cursor = window.first_parent_before(d, got[1])
        assert 101 in window.window_prs(d, cursor, "main")
        assert 100 in window.window_prs(d, cursor, "main")


def test_earliest_merged_ignores_unlabelled_pull_requests():
    import tempfile, subprocess, os
    with tempfile.TemporaryDirectory() as d:
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e")
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=d, check=True, capture_output=True)
        for subject in ("root", "chore: unrelated (#7)", "feat: ours (#9)"):
            subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", subject],
                           cwd=d, check=True, capture_output=True, env=env)
        assert window.earliest_merged(d, [9], ref="main")[0] == 9
        assert window.earliest_merged(d, [], ref="main") is None
        assert window.earliest_merged(d, [12345], ref="main") is None


def test_a_labelled_pr_with_no_number_in_its_subject_fails_closed():
    """The hazard: `earliest_merged` finds pull requests by matching numbers in commit subjects.

    One whose merge subject omits its number is invisible, so the cursor would be computed from a
    LATER merge and skip it, permanently. Two implementations agreeing does not catch this -- they
    share the omission, which is how three earlier versions of this check passed review while wrong.
    """
    import os, subprocess, tempfile
    from progress import gh as gh_mod, plan as plan_mod
    with tempfile.TemporaryDirectory() as d:
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e")
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=d, check=True, capture_output=True)
        for subject in ("root", "feat: no number here at all", "feat: later one (#20)"):
            subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", subject],
                           cwd=d, check=True, capture_output=True, env=env)
        # #10 merged as the numberless commit; resolve it the way the real check does.
        numberless = subprocess.run(["git", "rev-parse", "HEAD~1"], cwd=d,
                                    capture_output=True, text=True).stdout.strip()
        candidates = {10: numberless, 20: None}
        assert plan_mod.unaccounted_prs(d, candidates, ref="main") == [10]
        raises(window.GitError,
               lambda: plan_mod.bootstrap_from_sha(d, "PDE", candidates, ref="main"),
               "no pull request number")


def test_a_merge_commit_this_checkout_never_fetched_is_not_fatal():
    """Hit for real while verifying this change: a pull request merged between the checkout's last
    fetch and the plan. `merge-base` FAILS on a commit it has never seen rather than answering "no",
    and that failure aborted the whole plan — every area — over one freshly merged pull request."""
    import os, subprocess, tempfile
    from progress import gh as gh_mod, plan as plan_mod
    with tempfile.TemporaryDirectory() as d:
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e")
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=d, check=True, capture_output=True)
        for subject in ("root", "feat: ours (#5)"):
            subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", subject],
                           cwd=d, check=True, capture_output=True, env=env)
        assert not window.has_commit(d, "0" * 40)
        assert window.has_commit(d, window.head_sha(d, ref="main"))
        assert plan_mod.unaccounted_prs(d, {5: None, 99: "0" * 40}, ref="main") == []


def test_a_labelled_pr_merged_after_the_tip_is_not_flagged():
    """Benign: it is not in this history yet, and a later window will cover it."""
    import os, subprocess, tempfile
    from progress import gh as gh_mod, plan as plan_mod
    with tempfile.TemporaryDirectory() as d:
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e")
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=d, check=True, capture_output=True)
        for subject in ("root", "feat: ours (#5)"):
            subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", subject],
                           cwd=d, check=True, capture_output=True, env=env)
        assert plan_mod.unaccounted_prs(d, {5: None, 99: None}, ref="main") == []


# ----- an area ahead of the published documentation --------------------------------------------


def make_roadmap(root, areas):
    """A roadmap checkout: `{area: progress_text_or_None}` under `TauCetiRoadmap/`."""
    for area, progress_text in areas.items():
        d = pathlib.Path(root) / "TauCetiRoadmap" / area
        d.mkdir(parents=True)
        (d / "README.md").write_text(f"# {area}\n", encoding="utf-8")
        if progress_text is not None:
            (d / "PROGRESS.md").write_text(progress_text, encoding="utf-8")


def plan_against(code_dir, roadmap_dir, docs_sha, area_prs, *, ref="main", merge_shas=None, **kw):
    """`build_plan` with the network stubbed: a fixed documented build and fixed area labels."""
    from progress import gh as gh_mod, plan as plan_mod
    orig_docs, orig_labels = plan_mod.docs_source_commit, gh_mod.closed_prs_for_area
    orig_login = plan_mod._own_login
    plan_mod.docs_source_commit = lambda: docs_sha
    plan_mod._own_login = lambda: "test-user"
    gh_mod.closed_prs_for_area = lambda area, **_: {
        n: (merge_shas or {}).get(n) for n in area_prs.get(area, [])}
    try:
        return plan_mod.build_plan(
            roadmap_dir, code_dir,
            commits=[{"commit": {"committedDate": "2026-01-01T00:00:00Z"},
                      "messageHeadline": "progress: X (2026-01-01)"}],
            open_prs=[], ref=ref, min_prs=1, **kw)
    finally:
        plan_mod.docs_source_commit, gh_mod.closed_prs_for_area = orig_docs, orig_labels
        plan_mod._own_login = orig_login


def test_a_first_merge_beyond_docgen_does_not_block_other_areas():
    with tempfile.TemporaryDirectory() as code, tempfile.TemporaryDirectory() as roadmap:
        shas = make_repo(code, ["init", "old: a (#1)", "old: b (#2)", "new: c (#3)"])
        window.git(["update-ref", "refs/remotes/origin/docgen", shas[2]], code)
        make_roadmap(roadmap, {"Old": None, "New": None})
        got = plan_against(code, roadmap, shas[2], {"Old": [1, 2], "New": [3]},
                           ref="origin/docgen", merge_shas={3: shas[3]})
        assert got["roadmap"] == "Old", got
        assert got["prs"] == [2, 1], got
        assert got["to_sha"] == shas[2], got
        assert any("New" in s and "waiting for documentation" in s for s in got["skipped"]), got


def test_the_first_merge_is_reported_when_docgen_catches_up():
    with tempfile.TemporaryDirectory() as code, tempfile.TemporaryDirectory() as roadmap:
        shas = make_repo(code, ["init", "first (#3)", "second (#4)"])
        window.git(["update-ref", "refs/remotes/origin/docgen", shas[0]], code)
        make_roadmap(roadmap, {"New": None})
        kwargs = {"ref": "origin/docgen", "merge_shas": {3: shas[1], 4: shas[2]}}
        raises(plan.NotDue,
               lambda: plan_against(code, roadmap, shas[0], {"New": [3, 4]}, **kwargs),
               "waiting for documentation")
        assert not (pathlib.Path(roadmap) / "TauCetiRoadmap/New/PROGRESS.md").exists()
        window.git(["update-ref", "refs/remotes/origin/docgen", shas[2]], code)
        got = plan_against(code, roadmap, shas[2], {"New": [3, 4]}, **kwargs)
        assert got["roadmap"] == "New", got
        assert got["from_sha"] == shas[0], got
        assert got["prs"] == [4, 3], got


def test_missing_bootstrap_history_in_a_shallow_checkout_still_fails():
    from progress import gh as gh_mod
    with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as root:
        shas = make_repo(source, ["init", "first (#3)", "unrelated"])
        shallow = pathlib.Path(root) / "shallow"
        subprocess.run(["git", "clone", "-q", "--depth=1", pathlib.Path(source).as_uri(), str(shallow)],
                       check=True, capture_output=True)
        orig_gh = gh_mod.gh
        gh_mod.gh = lambda args, **_: shas[1] + "\n"
        try:
            raises(window.GitError,
                   lambda: plan.bootstrap_from_sha(shallow, "New", [3], ref="main"),
                   "full-history checkout is required")
        finally:
            gh_mod.gh = orig_gh


def test_a_visible_later_merge_cannot_hide_shallow_bootstrap_history():
    from progress import gh as gh_mod
    with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as root:
        shas = make_repo(source, ["init", "first (#3)", "unrelated", "second (#4)"])
        shallow = pathlib.Path(root) / "shallow"
        subprocess.run(["git", "clone", "-q", "--depth=2", pathlib.Path(source).as_uri(), str(shallow)],
                       check=True, capture_output=True)
        assert not window.has_commit(shallow, shas[1])
        assert window.earliest_merged(shallow, [3, 4], ref="main")[0] == 4
        orig_gh = gh_mod.gh
        calls = []
        gh_mod.gh = lambda args, **_: calls.append(args) or shas[1] + "\n"
        try:
            raises(window.GitError,
                   lambda: plan.bootstrap_from_sha(shallow, "New", [3, 4], ref="main"),
                   "full-history checkout is required")
            assert calls == [], calls
        finally:
            gh_mod.gh = orig_gh


def test_an_area_newer_than_the_documented_build_is_skipped_not_fatal():
    """The outage this prevents: a roadmap whose first pull request merged after the last
    documentation deploy bootstraps to a cursor ahead of `to_sha`. `window_prs` refuses on that,
    and the exception aborted the WHOLE plan -- so one new roadmap stopped every area's reporting
    until a human read the traceback. The new area waits; the others must still be reportable."""
    with tempfile.TemporaryDirectory() as code, tempfile.TemporaryDirectory() as roadmap:
        shas = make_repo(code, ["init", "old: a (#1)", "old: b (#2)", "later", "new: c (#3)"])
        make_roadmap(roadmap, {"Old": None, "New": None})
        # The documentation was built at shas[2]. New bootstraps to the parent of #3's merge,
        # shas[3], which is newer than that -- exactly the shape a roadmap added this week has.
        got = plan_against(code, roadmap, shas[2], {"Old": [1, 2], "New": [3]})
        assert got["roadmap"] == "Old", got["roadmap"]
        assert got["prs"] == [2, 1], got["prs"]
        assert any("New" in s and "not published yet" in s for s in got["skipped"]), got["skipped"]


def test_docs_catching_up_reports_the_waiting_area_with_no_history_stranded():
    """The skip must defer an area, not drop it. Once the documentation reaches the commit, the same
    area reports, and its FIRST pull request is inside that first window."""
    with tempfile.TemporaryDirectory() as code, tempfile.TemporaryDirectory() as roadmap:
        shas = make_repo(code, ["init", "old: a (#1)", "old: b (#2)", "later", "new: c (#3)"])
        make_roadmap(roadmap, {"Old": None, "New": None})
        got = plan_against(code, roadmap, shas[4], {"Old": [1, 2], "New": [3]}, only_area="New")
        assert got["roadmap"] == "New", got["roadmap"]
        assert got["prs"] == [3], got["prs"]


def test_a_recorded_cursor_ahead_of_the_docs_says_so_plainly():
    """A cursor written by an earlier report, now ahead of the published build: the documentation
    rolled back. Skipping is right — a backwards window would be nonsense — but a reader must not be
    told this is the ordinary "not published yet" case."""
    with tempfile.TemporaryDirectory() as code, tempfile.TemporaryDirectory() as roadmap:
        shas = make_repo(code, ["init", "a (#1)", "b (#2)", "c (#3)"])
        make_roadmap(roadmap, {"Old": files.new_progress_file("Old") + files.render_section(
            "Old", shas[0], shas[3], [1, 2, 3], "2026-01-01", "prose " * 60)})
        raises(plan.NotDue, lambda: plan_against(code, roadmap, shas[1], {"Old": [1, 2, 3]}),
               "went backwards")


def test_a_cursor_in_no_history_at_all_is_still_refused():
    """The skip is narrow on purpose. A cursor that is in neither the documented history nor the
    branch is the rewritten-branch case, and staying loud there is the whole point of the check."""
    with tempfile.TemporaryDirectory() as code, tempfile.TemporaryDirectory() as roadmap:
        shas = make_repo(code, ["init", "a (#1)", "b (#2)"])
        # A commit off the mainline: reachable as an object, on no branch. That is what a cursor
        # written before a rewrite looks like, and it is an ancestor of nothing we report from.
        subprocess.run(["git", "-C", code, "checkout", "-q", "--detach", shas[0]],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", code, "commit", "-q", "--allow-empty", "-m", "rewritten (#9)"],
                       check=True, capture_output=True,
                       env={**os.environ, "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@e",
                            "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@e"})
        orphan = window.git(["rev-parse", "HEAD"], code).strip()
        subprocess.run(["git", "-C", code, "checkout", "-q", "main"], check=True, capture_output=True)
        # The cursor is the newest section's `to_sha`, and this one names history we cannot reach.
        make_roadmap(roadmap, {"Old": files.new_progress_file("Old") + files.render_section(
            "Old", shas[0], orphan, [1], "2026-01-01", "prose " * 60)})
        raises(window.GitError,
               lambda: plan_against(code, roadmap, shas[2], {"Old": [1, 2]}),
               "not an ancestor")


# ----- the layers the plan hands the model ------------------------------------------------------


def test_the_plan_records_the_selected_areas_layers_and_readme_hash_exactly():
    """The inventory `apply` binds the verdicts to: the README's layer headings in order, with ids
    and 1-based lines, and a hash of the README's text."""
    import hashlib
    with tempfile.TemporaryDirectory() as code, tempfile.TemporaryDirectory() as roadmap:
        shas = make_repo(code, ["init", "a (#1)", "b (#2)"])
        make_roadmap(roadmap, {"Curves": None})
        readme = ("# Curves\n\n## Layers\n\n### Layer 0: the group law (Silverman III.2)\ntext\n"
                  "### Layer 1: isogenies — the dual\n")
        (pathlib.Path(roadmap) / "TauCetiRoadmap" / "Curves" / "README.md").write_text(readme, encoding="utf-8")
        got = plan_against(code, roadmap, shas[2], {"Curves": [1, 2]})
        assert got["layers"] == [{"id": "Layer 0", "title": "Layer 0: the group law", "line": 5},
                                 {"id": "Layer 1", "title": "Layer 1: isogenies — the dual", "line": 7}], got["layers"]
        assert got["readme_sha"] == hashlib.sha256(readme.encode("utf-8")).hexdigest()


def test_an_umbrella_areas_sub_roadmaps_are_assessed_each_against_its_own_readme():
    """An umbrella area (RepresentationTheory) has an index README with no layer headings; its
    label covers every sub-roadmap, so the plan lists each one's layers and README hash under its
    `Area/Child` id. A directory without `Suggested.lean` (a `references/` folder) is not one, and
    a sub-roadmap with no layer headings has nothing to assess."""
    import hashlib
    with tempfile.TemporaryDirectory() as code, tempfile.TemporaryDirectory() as roadmap:
        shas = make_repo(code, ["init", "a (#1)", "b (#2)"])
        make_roadmap(roadmap, {"Umbrella": None})
        base = pathlib.Path(roadmap) / "TauCetiRoadmap" / "Umbrella"
        index = ("# Umbrella\n\n## The roadmaps\n\n- [Spin](SpinRepresentations/README.md)\n"
                 "- [Roots](RootSystems/README.md)\n")
        (base / "README.md").write_text(index, encoding="utf-8")
        readmes = {}
        for child, body in (("SpinRepresentations", "### Layer 0: basics\n### Layer 1: more\n"),
                            ("RootSystems", "### Layer 0: axioms\n"),
                            ("Unlayered", "Prose only.\n"),
                            ("references", "### Layer 9: not a roadmap\n")):
            (base / child).mkdir()
            readmes[child] = f"# {child}\n\n{body}"
            (base / child / "README.md").write_text(readmes[child], encoding="utf-8")
            if child != "references":
                (base / child / "Suggested.lean").write_text("-- suggestions\n", encoding="utf-8")
        got = plan_against(code, roadmap, shas[2], {"Umbrella": [1, 2]})
        assert got["roadmap"] == "Umbrella"
        assert got["layers"] == [], got["layers"]
        assert got["readme_sha"] == hashlib.sha256(index.encode("utf-8")).hexdigest()
        sha = lambda child: hashlib.sha256(readmes[child].encode("utf-8")).hexdigest()
        assert got["sub_roadmaps"] == [
            {"roadmap": "Umbrella/RootSystems", "readme": "TauCetiRoadmap/Umbrella/RootSystems/README.md",
             "readme_sha": sha("RootSystems"),
             "layers": [{"id": "Layer 0", "title": "Layer 0: axioms", "line": 3}]},
            {"roadmap": "Umbrella/SpinRepresentations",
             "readme": "TauCetiRoadmap/Umbrella/SpinRepresentations/README.md",
             "readme_sha": sha("SpinRepresentations"),
             "layers": [{"id": "Layer 0", "title": "Layer 0: basics", "line": 3},
                        {"id": "Layer 1", "title": "Layer 1: more", "line": 4}]},
        ], got["sub_roadmaps"]
        # The children are not areas of their own: labelled areas are top-level only.
        assert plan.discover_areas(roadmap) == {"Umbrella": "TauCetiRoadmap/Umbrella"}


def test_an_ordinary_area_has_no_sub_roadmaps():
    with tempfile.TemporaryDirectory() as code, tempfile.TemporaryDirectory() as roadmap:
        shas = make_repo(code, ["init", "a (#1)", "b (#2)"])
        make_roadmap(roadmap, {"Curves": None})
        refs = pathlib.Path(roadmap) / "TauCetiRoadmap" / "Curves" / "references"
        refs.mkdir()
        (refs / "README.md").write_text("### Layer 0: a paper\n", encoding="utf-8")
        assert plan_against(code, roadmap, shas[2], {"Curves": [1, 2]})["sub_roadmaps"] == []

for _name, _fn in sorted(globals().items()):
    if _name.startswith("test_") and callable(_fn):
        check(_name, _fn)

print()
if failures:
    print(f"{len(failures)} failure(s): {', '.join(failures)}")
    sys.exit(1)
print("all tests passed")
