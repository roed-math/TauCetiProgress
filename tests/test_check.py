"""Tests for `tauceti-progress check`: the prompt's limits, link provenance, dead links, and carried-
forward assessments. The documentation site is replaced by a fake that knows a few module pages."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from progress import apply as apply_mod, check  # noqa: E402
from progress.docs import DOCS_BASE, DocsError, DocsNotFound  # noqa: E402

failures = []


def run_check(name, fn):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        failures.append(name)
        print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    else:
        print(f"ok   {name}")


Z, A, B = "0" * 40, "a1b2c3d" + "0" * 33, "b9c8d7e" + "0" * 33
LAYERS = [{"id": "Layer 0", "title": "Layer 0: curves", "line": 10},
          {"id": "Layer 1", "title": "Layer 1: cycles", "line": 20}]
README_SHA = "1" * 64
URL = f"{DOCS_BASE}/TauCeti/Contour/Basic.html#TauCeti.harnack"
OLD_URL = f"{DOCS_BASE}/TauCeti/Contour/Old.html#TauCeti.meanValue"


def plan(from_sha, to_sha, readme_sha=README_SHA):
    return {"roadmap": "ContourIntegration", "rel_dir": "TauCetiRoadmap/ContourIntegration",
            "from_sha": from_sha, "to_sha": to_sha, "prs": [966, 1244],
            "from_date": "2026-07-28T09:11:00+00:00", "to_date": "2026-07-30T11:34:41+00:00",
            "status_path": "TauCetiRoadmap/ContourIntegration/STATUS.md",
            "progress_path": "TauCetiRoadmap/ContourIntegration/PROGRESS.md",
            "layers": LAYERS, "readme_sha": readme_sha}


def block(s0, s1):
    return f'\n\n```coverage\n[{{"id": "Layer 0", "state": "{s0}"}}, {{"id": "Layer 1", "state": "{s1}"}}]\n```\n'


def status(link=URL, extra=""):
    return (
        "## Where this roadmap stands\n\n**At a glance.** Harnack's inequality for nonnegative harmonic "
        f"functions on a disc is proved, as [the Harnack inequality]({link}). The mean-value machinery "
        "it rests on is in place, and the homological Cauchy theorem has not begun.\n\n"
        "## The frontier\n\n- **The homological version** remains: cycles and winding numbers.\n" + extra
    )


SECTION = (f"Harnack's inequality landed, as [the Harnack inequality]({URL}), in both the two-sided "
           "comparison with the centre value and the pairwise form on a closed subdisc.\n")

FACTS = {"declarations": [{"name": "TauCeti.harnack", "url": URL}]}


class FakeDocs:
    def __init__(self, pages):
        self.pages = pages

    def declarations(self, page):
        got = self.pages.get(page)
        if isinstance(got, Exception):
            raise got
        if got is None:
            raise DocsNotFound(f"{page} does not exist (HTTP 404)")
        return {name: {} for name in got}


DOCS = FakeDocs({"TauCeti/Contour/Basic.html": ["TauCeti.harnack"],
                 "TauCeti/Contour/Old.html": ["TauCeti.meanValue"]})


def previous(s0="done", s1="partial", readme_sha=README_SHA, link=OLD_URL):
    """The previous report's STATUS.md and PROGRESS.md, rendered by the real renderer."""
    st, pr, _ = apply_mod.render_update(plan(Z, A, readme_sha), status(link) + block(s0, s1), SECTION, None, None)
    return st, pr


def run(status_body, section=SECTION, old=None, facts=FACTS, docs=DOCS, readme_sha=README_SHA):
    old_status, old_progress = old if old else (None, None)
    return check.run_checks(plan(Z if old is None else A, B, readme_sha), facts, status_body, section,
                            old_status, old_progress, docs=docs)


def test_a_sound_report_passes():
    fails, warns, info = run(status() + block("done", "partial"), old=previous())
    assert fails == [] and warns == [], (fails, warns)
    assert any("status prose" in line for line in info)


def test_the_prompts_word_limits_are_enforced():
    long_section = " ".join(["word"] * 320) + "\n"
    fails, *_ = run(status() + block("done", "partial"), section=long_section)
    assert any("the section is 320 words" in f for f in fails), fails
    long_status = status(extra="\n".join(["- more " * 20] * 19) + "\n")
    fails, *_ = run(long_status + block("done", "partial"))
    assert any("the status prose is" in f for f in fails), fails


def test_the_status_needs_exactly_its_two_headings():
    body = status().replace("## The frontier", "## What is next") + block("done", "partial")
    fails, *_ = run(body)
    assert any("`##` headings" in f for f in fails), fails


def test_a_built_url_is_refused():
    made_up = f"{DOCS_BASE}/TauCeti/Contour/Basic.html#TauCeti.harnack_ineq"
    fails, *_ = run(status(link=made_up) + block("done", "partial"))
    assert any("not copied" in f and made_up in f for f in fails), fails


def test_a_url_from_the_previous_report_is_allowed_but_must_still_exist():
    fails, *_ = run(status(link=OLD_URL) + block("done", "partial"), old=previous())
    assert fails == [], fails
    renamed = FakeDocs({"TauCeti/Contour/Basic.html": ["TauCeti.harnack"],
                        "TauCeti/Contour/Old.html": ["TauCeti.meanValue'"]})
    fails, *_ = run(status(link=OLD_URL) + block("done", "partial"), old=previous(), docs=renamed)
    assert any("dead documentation link" in f and "no such declaration" in f for f in fails), fails
    gone = FakeDocs({"TauCeti/Contour/Basic.html": ["TauCeti.harnack"]})
    fails, *_ = run(status(link=OLD_URL) + block("done", "partial"), old=previous(), docs=gone)
    assert any("does not exist" in f for f in fails), fails


def test_an_unreadable_page_is_a_warning_not_a_dead_link():
    flaky = FakeDocs({"TauCeti/Contour/Basic.html": DocsError("the site is redeploying")})
    fails, warns, _ = run(status() + block("done", "partial"), docs=flaky)
    assert fails == [] and any("could not check" in w for w in warns), (fails, warns)


def test_an_assessed_layer_may_not_become_unassessed():
    fails, *_ = run(status() + block("done", "unassessed"), old=previous())
    assert any("Layer 1 was `partial`" in f for f in fails), fails


def test_a_new_readme_frees_the_assessments():
    fails, *_ = run(status() + block("done", "unassessed"), old=previous(readme_sha="2" * 64))
    assert fails == [], fails


def test_a_report_that_assesses_nothing_is_refused():
    fails, *_ = run(status() + block("unassessed", "unassessed"))
    assert any("every one of the 2 layers" in f for f in fails), fails


def test_a_done_layer_moving_down_is_a_warning():
    fails, warns, _ = run(status() + block("partial", "partial"), old=previous())
    assert fails == [] and any("Layer 0 moves from `done` to `partial`" in w for w in warns), (fails, warns)


def test_a_report_that_does_not_render_says_so():
    fails, *_ = run(status())  # the plan lists layers, and there is no coverage block
    assert len(fails) == 1 and "does not render" in fails[0], fails


for _name, _fn in sorted(globals().items()):
    if _name.startswith("test_") and callable(_fn):
        run_check(_name, _fn)

print()
if failures:
    print(f"{len(failures)} failure(s): {', '.join(failures)}")
    sys.exit(1)
print("all tests passed")
