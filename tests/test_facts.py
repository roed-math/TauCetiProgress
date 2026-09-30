"""Tests for the factual spine.

Declaration names, kinds, links and source positions come from doc-gen4; git blame decides what
belongs to the window. Neither requires understanding Lean, which is the point -- an earlier version
tried to derive names by tracking `namespace`/`section`/`end` in Python and got them subtly wrong,
which is the worst outcome for something whose output becomes a link.

The documentation is stubbed here with a fake `Docs` so the tests are hermetic; the parsing of real
doc-gen4 markup is covered in test_docs.py against a captured page.
"""

import json
import os
import pathlib
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from progress import cli, facts, window  # noqa: E402
from progress.docs import Docs, DocsError, DocsNotFound, INDEX_PATH  # noqa: E402
from progress.facts import FactsError  # noqa: E402

failures = []


def check(name, fn):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        failures.append(name)
        print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    else:
        print(f"ok   {name}")


ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@e",
    "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@e",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
}


def commit(tmp, subject, files):
    for rel, text in files.items():
        p = pathlib.Path(tmp) / rel
        if text is None:
            p.unlink()
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
    subprocess.run(["git", "-C", tmp, "add", "-A"], check=True, capture_output=True, env=ENV)
    subprocess.run(["git", "-C", tmp, "commit", "-q", "-m", subject],
                   check=True, capture_output=True, env=ENV)
    return window.git(["rev-parse", "HEAD"], tmp).strip()


class FakeDocs:
    """Stands in for the published documentation.

    `pages` maps a module page to `{full_name: (kind, file, start, end)}`, which is exactly what the
    real reader extracts from doc-gen4's markup.
    """

    base = "https://docs.example/docs"

    def __init__(self, pages, source_commit):
        self._pages = pages
        self._commit = source_commit

    def source_commit(self):
        return self._commit

    def declarations(self, page):
        out = {}
        for name, (kind, file, start, end) in self._pages.get(page, {}).items():
            out[name] = {
                "kind": kind, "file": file, "start": start, "end": end,
                "commit": self._commit, "url": f"{self.base}/{page}#{name}",
            }
        return out


class ReadingDocs(FakeDocs):
    """A `FakeDocs` that fails the way the real reader does.

    A page the site does not have raises `DocsNotFound`, as a 404 from the real transport does, and a
    page named in `unreadable` raises the refusal `Docs.declarations` gives for a page from another
    build while the site redeploys.
    """

    def __init__(self, pages, source_commit, unreadable=()):
        super().__init__(pages, source_commit)
        self._unreadable = set(unreadable)

    def declarations(self, page):
        if page in self._unreadable:
            raise DocsError(f"{page} was built from bbbbbbb, not aaaaaaa; the site is redeploying "
                            f"and this run cannot describe one build")
        if page not in self._pages:
            raise DocsNotFound(f"{self.base}/{page} does not exist (HTTP 404)")
        return super().declarations(page)


ALPHA = """namespace TauCeti
/-- **Alpha's theorem.** It states alpha. And more besides. -/
theorem alpha : True := trivial
end TauCeti
"""

ALPHA_AND_BETA = """namespace TauCeti
/-- **Alpha's theorem.** It states alpha. And more besides. -/
theorem alpha : True := trivial

/-- **Beta's theorem.** It states beta. -/
theorem beta : True := trivial
end TauCeti
"""


def site(pages, base):
    """A transport serving `pages`, answering 404 (as the real site does) for anything else."""
    def opener(url):
        rel = url.removeprefix(base + "/")
        if rel not in pages:
            raise DocsNotFound(f"{url} does not exist (HTTP 404)")
        return pages[rel]
    return opener


def repo_with_two_prs(tmp):
    """Root, then a PR adding `alpha`, then a PR adding `beta` to the same file."""
    subprocess.run(["git", "init", "-q", "-b", "main", tmp], check=True, capture_output=True)
    root = commit(tmp, "init", {"README.md": "x"})
    first = commit(tmp, "feat: alpha (#101)", {"TauCeti/A.lean": ALPHA})
    second = commit(tmp, "feat: beta (#102)", {"TauCeti/A.lean": ALPHA_AND_BETA})
    return root, first, second


# `alpha` occupies lines 2-3 in both versions; `beta` occupies lines 5-6 of the second.
PAGE = "TauCeti/A.html"


def docs_for(commit_sha, with_beta=True):
    decls = {"TauCeti.alpha": ("theorem", "TauCeti/A.lean", 2, 3)}
    if with_beta:
        decls["TauCeti.beta"] = ("theorem", "TauCeti/A.lean", 5, 6)
    return FakeDocs({PAGE: decls}, commit_sha)


def test_names_kinds_and_urls_come_from_the_documentation():
    """Nothing is derived from the source text: the qualified name, the kind and the link are all
    what doc-gen4 published."""
    with tempfile.TemporaryDirectory() as tmp:
        root, first, second = repo_with_two_prs(tmp)
        got = facts.collect(tmp, root, second, docs=docs_for(second))
        by = {d["name"]: d for d in got["declarations"]}
        assert set(by) == {"TauCeti.alpha", "TauCeti.beta"}, sorted(by)
        assert by["TauCeti.alpha"]["kind"] == "theorem"
        assert by["TauCeti.alpha"]["url"] == "https://docs.example/docs/TauCeti/A.html#TauCeti.alpha"


def test_blame_attributes_each_declaration_to_its_pull_request():
    with tempfile.TemporaryDirectory() as tmp:
        root, first, second = repo_with_two_prs(tmp)
        got = facts.collect(tmp, root, second, docs=docs_for(second))
        by = {d["name"]: d for d in got["declarations"]}
        assert by["TauCeti.alpha"]["pr"] == 101, by["TauCeti.alpha"]
        assert by["TauCeti.beta"]["pr"] == 102, by["TauCeti.beta"]


def test_declarations_predating_the_window_are_not_reported():
    """`alpha` was written before this window opens, so it is real but not news."""
    with tempfile.TemporaryDirectory() as tmp:
        root, first, second = repo_with_two_prs(tmp)
        got = facts.collect(tmp, first, second, docs=docs_for(second))
        names = {d["name"] for d in got["declarations"]}
        assert names == {"TauCeti.beta"}, sorted(names)


def test_the_pr_filter_is_honoured():
    with tempfile.TemporaryDirectory() as tmp:
        root, first, second = repo_with_two_prs(tmp)
        got = facts.collect(tmp, root, second, pr_numbers=[101], docs=docs_for(second))
        names = {d["name"] for d in got["declarations"]}
        assert names == {"TauCeti.alpha"}, sorted(names)


def test_docstrings_are_read_from_a_known_line():
    """doc-gen4's source range BEGINS at the docstring, so it is read from a position the
    documentation supplied rather than found by parsing."""
    with tempfile.TemporaryDirectory() as tmp:
        root, first, second = repo_with_two_prs(tmp)
        got = facts.collect(tmp, root, second, docs=docs_for(second))
        by = {d["name"]: d for d in got["declarations"]}
        assert by["TauCeti.alpha"]["doc"] == "**Alpha's theorem.** It states alpha.", by
        assert by["TauCeti.beta"]["doc"] == "**Beta's theorem.** It states beta."


def test_a_declaration_with_no_docstring_reports_none():
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["git", "init", "-q", "-b", "main", tmp], check=True, capture_output=True)
        root = commit(tmp, "init", {"README.md": "x"})
        head = commit(tmp, "feat: bare (#7)", {"TauCeti/A.lean": "theorem bare : True := trivial\n"})
        docs = FakeDocs({PAGE: {"bare": ("theorem", "TauCeti/A.lean", 1, 1)}}, head)
        got = facts.collect(tmp, root, head, docs=docs)
        assert got["declarations"][0]["doc"] == ""


def test_a_removed_module_can_have_no_published_page():
    """A file touched in the window but removed before the documented commit has no page.
    The rest of the window is still reported."""
    with tempfile.TemporaryDirectory() as tmp:
        root, first, second = repo_with_two_prs(tmp)
        third = commit(tmp, "feat: add B (#103)",
                       {"TauCeti/B.lean": "theorem later : True := trivial\n"})
        fourth = commit(tmp, "remove B (#104)", {"TauCeti/B.lean": None})
        docs = ReadingDocs({PAGE: {"TauCeti.alpha": ("theorem", "TauCeti/A.lean", 2, 3)}}, fourth)
        got = facts.collect(tmp, root, fourth, docs=docs)
        names = {d["name"] for d in got["declarations"]}
        assert names == {"TauCeti.alpha"}, sorted(names)
        assert got["counts"]["files"] == 2, got["counts"]


def test_a_404_from_another_build_refuses_a_partial_report():
    """One cached page may contribute declarations before an uncached page returns 404 from a
    newer build. A nonempty partial report must not advance past that missing module."""
    with tempfile.TemporaryDirectory() as tmp:
        root, first, second = repo_with_two_prs(tmp)
        third = commit(tmp, "feat: B (#103)",
                       {"TauCeti/B.lean": "theorem later : True := trivial\n"})
        docs = ReadingDocs(docs_for(second)._pages, third)
        try:
            facts.collect(tmp, root, third, docs=docs)
        except FactsError as exc:
            assert "TauCeti/B.lean" in str(exc) and "redeploying" in str(exc), str(exc)
        else:
            raise AssertionError("expected a refusal, not a partial report")


def test_an_empty_page_from_another_build_refuses_a_partial_report():
    """An HTTP 200 page with no declarations can still name the newer build in its navigation.
    A contributing cached page must not let the window advance past that newer page."""
    with tempfile.TemporaryDirectory() as tmp:
        root, first, second = repo_with_two_prs(tmp)
        third = commit(tmp, "feat: B (#103)",
                       {"TauCeti/B.lean": "theorem later : True := trivial\n"})
        base = "https://docs.example/docs"
        gh_link = (f"https://github.com/TauCetiProject/TauCeti/blob/{third}/"
                   "TauCeti/A.lean#L2-L3")
        def b_nav(sha):
            return ('<p class="gh_nav_link"><a href="https://github.com/'
                    f'TauCetiProject/TauCeti/blob/{sha}/TauCeti/B.lean">source</a></p>')
        a_page = (f'<div class="decl" id="TauCeti.alpha"><span class="decl_kind">theorem'
                  f'</span><div class="gh_link"><a href="{gh_link}">source</a></div></div>')
        pages = {
            INDEX_PATH: json.dumps({"declarations": {
                "TauCeti.alpha": {"docLink": "./TauCeti/A.html#TauCeti.alpha"}}}),
            "TauCeti/A.html": a_page,
            "TauCeti/B.html": b_nav(third),
        }
        docs = Docs(base=base, cache_dir=pathlib.Path(tmp) / "cache",
                    opener=site(pages, base))
        assert docs.source_commit() == third  # caches the probe page from this build
        pages["TauCeti/B.html"] = b_nav("b" * 40)
        try:
            facts.collect(tmp, root, third, docs=docs)
        except FactsError as exc:
            assert "TauCeti/B.lean" in str(exc) and "redeploying" in str(exc), str(exc)
        else:
            raise AssertionError("expected a refusal, not a partial report")


def test_a_genuinely_empty_module_does_not_block_another_declaration():
    """An imports-only module has a nav source link even though it has no declarations."""
    with tempfile.TemporaryDirectory() as tmp:
        root, first, second = repo_with_two_prs(tmp)
        third = commit(tmp, "feat: imports (#103)",
                       {"TauCeti/B.lean": "import TauCeti.A\n"})
        a_link = (f"https://github.com/TauCetiProject/TauCeti/blob/{third}/"
                  "TauCeti/A.lean#L2-L3")
        b_link = (f"https://github.com/TauCetiProject/TauCeti/blob/{third}/"
                  "TauCeti/B.lean")
        base = "https://docs.example/docs"
        pages = {
            INDEX_PATH: json.dumps({"declarations": {
                "TauCeti.alpha": {"docLink": "./TauCeti/A.html#TauCeti.alpha"}}}),
            "TauCeti/A.html": (f'<div class="decl" id="TauCeti.alpha">'
                                   f'<div class="gh_link"><a href="{a_link}">source</a></div></div>'),
            "TauCeti/B.html": (f'<p class="gh_nav_link"><a href="{b_link}">source</a></p>'),
        }
        docs = Docs(base=base, cache_dir=pathlib.Path(tmp) / "cache",
                    opener=site(pages, base))
        got = facts.collect(tmp, root, third, docs=docs)
        assert [d["name"] for d in got["declarations"]] == ["TauCeti.alpha"]
        assert got["counts"]["files"] == 2


def _incremental_site(tmp, a_commit, build):
    """A deployed tree marked as documenting `build`, whose page for TauCeti/A.lean was built at
    `a_commit` (an incremental build that did not re-analyze A), and whose page for B was rebuilt."""
    base = "https://docs.example/docs"
    a_link = f"https://github.com/TauCetiProject/TauCeti/blob/{a_commit}/TauCeti/A.lean#L2-L3"
    b_link = f"https://github.com/TauCetiProject/TauCeti/blob/{build}/TauCeti/B.lean#L1-L1"
    pages = {
        "SOURCE_SHA": build + "\n",
        INDEX_PATH: json.dumps({"declarations": {
            "TauCeti.alpha": {"docLink": "./TauCeti/A.html#TauCeti.alpha"},
            "TauCeti.gamma": {"docLink": "./TauCeti/B.html#TauCeti.gamma"}}}),
        "TauCeti/A.html": (f'<div class="decl" id="TauCeti.alpha"><span class="decl_kind">theorem</span>'
                           f'<div class="gh_link"><a href="{a_link}">source</a></div></div>'),
        "TauCeti/B.html": (f'<div class="decl" id="TauCeti.gamma"><span class="decl_kind">theorem</span>'
                           f'<div class="gh_link"><a href="{b_link}">source</a></div></div>'),
    }
    return Docs(base=base, cache_dir=pathlib.Path(tmp) / "cache", opener=site(pages, base),
                accept_older=facts.unchanged_since(tmp))


def test_an_incremental_build_s_older_page_for_an_unchanged_module_is_used():
    """Since TauCeti#9636 a module page keeps the commit of the build that last re-analyzed it. When
    its file is unchanged since, its spans are the build's, and refusing it refused every window."""
    with tempfile.TemporaryDirectory() as tmp:
        root, first, _second = repo_with_two_prs(tmp)
        tip = commit(tmp, "feat: gamma (#103)", {"TauCeti/B.lean": "theorem gamma : True := trivial\n"})
        got = facts.collect(tmp, root, tip, pr_numbers=[101, 103],
                            docs=_incremental_site(tmp, _second, tip))
        by = {d["name"]: d["pr"] for d in got["declarations"]}
        assert by == {"TauCeti.alpha": 101, "TauCeti.gamma": 103}, by
        assert got["docs_sha"] == tip


def test_an_older_page_for_a_module_changed_since_is_refused():
    """A page from an earlier build whose file HAS changed since would put its spans on other lines."""
    with tempfile.TemporaryDirectory() as tmp:
        root, first, second = repo_with_two_prs(tmp)
        tip = commit(tmp, "feat: gamma (#103)", {"TauCeti/B.lean": "theorem gamma : True := trivial\n"})
        try:
            facts.collect(tmp, root, tip, docs=_incremental_site(tmp, first, tip))
        except FactsError as exc:
            assert "TauCeti/A.lean" in str(exc) and "changed since" in str(exc), str(exc)
        else:
            raise AssertionError("A.lean changed after the page's build; its spans cannot be trusted")


def test_a_page_that_cannot_be_read_refuses_the_window():
    """Regression: a run that straddled a deploy had every later page refused as "from another
    build", `collect` skipped each one as though it were unpublished, and a 72-pull-request window
    was reported as having added no declarations. An unreadable page is not an empty one."""
    with tempfile.TemporaryDirectory() as tmp:
        root, first, second = repo_with_two_prs(tmp)
        docs = ReadingDocs(docs_for(second)._pages, second, unreadable={PAGE})
        try:
            facts.collect(tmp, root, second, docs=docs)
        except FactsError as exc:
            assert "TauCeti/A.lean" in str(exc) and "redeploying" in str(exc), str(exc)
        else:
            raise AssertionError("expected a refusal, not a window with the page left out")


def test_a_window_with_no_attributable_declarations_is_refused():
    """The backstop: whatever loses them, a window whose pull requests yield no declarations at all
    must not reach a writing model, which would announce that nothing happened."""
    with tempfile.TemporaryDirectory() as tmp:
        root, first, second = repo_with_two_prs(tmp)
        try:
            facts.collect(tmp, root, second, docs=ReadingDocs({PAGE: {}}, second))
        except FactsError as exc:
            assert "2 pull request(s)" in str(exc), str(exc)
            assert "0 with no published page" in str(exc), str(exc)
        else:
            raise AssertionError("expected a refusal, not an empty window")


def test_revised_declarations_are_marked_not_new():
    """A declaration whose lines are only partly from this window existed before and was revised."""
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["git", "init", "-q", "-b", "main", tmp], check=True, capture_output=True)
        root = commit(tmp, "init", {"README.md": "x"})
        before = "/-- Doc. -/\ntheorem t : True := by\n  trivial\n"
        first = commit(tmp, "feat: add (#1)", {"TauCeti/A.lean": before})
        after = "/-- Doc. -/\ntheorem t : True := by\n  exact trivial\n"
        second = commit(tmp, "refactor: tweak (#2)", {"TauCeti/A.lean": after})
        docs = FakeDocs({PAGE: {"t": ("theorem", "TauCeti/A.lean", 2, 3)}}, second)
        got = facts.collect(tmp, root, second, docs=docs)
        assert got["declarations"][0]["new"] is True, "written entirely within the window"
        got2 = facts.collect(tmp, first, second, docs=docs)
        assert got2["declarations"][0]["new"] is False, "only the body line is from this window"


def test_documentation_behind_the_planned_window_refuses_to_advance():
    """A different docs build between plan and facts must not advance the planned cursor past
    pull requests whose declarations the extractor did not inspect."""
    with tempfile.TemporaryDirectory() as tmp:
        root, first, second = repo_with_two_prs(tmp)
        third = commit(tmp, "feat: later (#103)", {"TauCeti/B.lean": "theorem later : True := trivial\n"})
        try:
            facts.collect(tmp, root, third, docs=docs_for(second))
        except FactsError as exc:
            assert second[:7] in str(exc) and third[:7] in str(exc), str(exc)
            assert "replan" in str(exc), str(exc)
        else:
            raise AssertionError("expected a refusal, not a report that skips #103")


def test_documentation_from_a_foreign_history_is_refused():
    with tempfile.TemporaryDirectory() as tmp:
        root, first, second = repo_with_two_prs(tmp)
        try:
            facts.collect(tmp, root, second, docs=docs_for("0" * 40))
        except (FactsError, window.GitError) as exc:
            assert "0000000" in str(exc) or "not an ancestor" in str(exc), str(exc)
            return
        raise AssertionError("expected a refusal")


def test_module_page_for_file():
    assert facts.module_page_for_file("TauCeti/Analysis/Fredholm/Basic.lean") == \
        "TauCeti/Analysis/Fredholm/Basic.html"
    assert facts.module_page_for_file("scripts/x.py") is None
    assert facts.module_page_for_file("TauCeti/A.txt") is None


def test_cli_facts_passes_the_plan_filter():
    """Regression: the CLI once dropped the filter, so a one-roadmap report was grounded in every
    roadmap's work in the range. The failure is silent -- the output merely gets bigger."""
    with tempfile.TemporaryDirectory() as tmp:
        root, first, second = repo_with_two_prs(tmp)
        plan = {"roadmap": "Algebra", "from_sha": root, "to_sha": second, "prs": [101]}
        plan_file = pathlib.Path(tmp) / "plan.json"
        out_file = pathlib.Path(tmp) / "facts.json"
        plan_file.write_text(json.dumps(plan))
        # Point the reader at a stub by monkeypatching the module the CLI imports.
        import progress.docs as docs_mod
        real = docs_mod.Docs
        docs_mod.Docs = lambda *a, **k: docs_for(second)
        try:
            rc = cli.main(["facts", "--plan", str(plan_file), "--code-dir", tmp,
                           "--out", str(out_file)])
        finally:
            docs_mod.Docs = real
        assert rc == 0, rc
        got = json.loads(out_file.read_text())
        names = {d["name"] for d in got["declarations"]}
        assert names == {"TauCeti.alpha"}, f"filter dropped: got {sorted(names)}"


def test_cli_facts_reports_a_refusal_as_an_error():
    """A refusal has to stop the round before a model is started: exit 1 with the reason, and no
    facts file left for anything to read."""
    with tempfile.TemporaryDirectory() as tmp:
        root, first, second = repo_with_two_prs(tmp)
        plan = {"roadmap": "Algebra", "from_sha": root, "to_sha": second, "prs": [101, 102]}
        plan_file = pathlib.Path(tmp) / "plan.json"
        out_file = pathlib.Path(tmp) / "facts.json"
        plan_file.write_text(json.dumps(plan))
        import progress.docs as docs_mod
        real = docs_mod.Docs
        docs_mod.Docs = lambda *a, **k: ReadingDocs({}, second)
        try:
            rc = cli.main(["facts", "--plan", str(plan_file), "--code-dir", tmp,
                           "--out", str(out_file)])
        finally:
            docs_mod.Docs = real
        assert rc == 1, rc
        assert not out_file.exists(), "a refused window must leave no facts file behind"


for _name, _fn in sorted(globals().items()):
    if _name.startswith("test_") and callable(_fn):
        check(_name, _fn)

print()
if failures:
    print(f"{len(failures)} failure(s): {', '.join(failures)}")
    sys.exit(1)
print("all tests passed")
