"""Check a report's two prose bodies before anything is committed: `tauceti-progress check`.

`apply` already refuses a report whose files are malformed. This catches what is well-formed and
still wrong, each item a defect found in a report that landed or nearly did (2026-09-26/27):

* **The prompt's own limits.** `files` enforces hard ceilings (900 and 450 words). The prompt asks
  for 750 and 300 and a fixed pair of `##` headings, and nothing checked those.
* **Links the model built.** Every documentation URL must be copied from the facts file or from the
  area's previous STATUS.md/PROGRESS.md. A URL assembled from a module path looks right and goes
  nowhere.
* **Links that died.** A URL copied from an old STATUS.md can name a declaration that has since been
  renamed or moved; refactors made this common. Each anchor is looked up on its module page.
* **Assessments lost.** A layer the previous report judged (`done`, `partial`, `untouched`) must not
  become `unassessed` against the same README: the prompt says to carry it forward, and one report
  that marked every layer `unassessed` would have turned an assessed row on the Progress page back
  into an unassessed one. A report that assesses nothing at all is refused for the same reason.

The writing model is meant to run this itself and fix what it reports; the worker runs it again
before `apply`. Exit 0 when every check passes, 1 otherwise. Warnings do not fail the check.
"""

import html
import json
import pathlib
import re
import urllib.parse

from . import apply as apply_mod
from . import files, layers as layers_mod
from .docs import DOCS_BASE, Docs, DocsError, DocsNotFound

# The prompt's limits (progress/prompts/progress.md), stricter than the hard ceilings in `files`.
STATUS_WORDS = 750
SECTION_WORDS = 300
SECTION_PARAGRAPHS = 3
STATUS_HEADINGS = ["## Where this roadmap stands", "## The frontier"]

DOCS_URL_RE = re.compile(r"https://taucetiproject\.github\.io/[^\s)>\]\"'`]+")
ASSESSED = ("done", "partial", "untouched")


def _read(path):
    p = pathlib.Path(path)
    return p.read_text(encoding="utf-8") if p.is_file() else None


def _words(text):
    return len(text.split())


def _paragraphs(text):
    return len([p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()])


def known_urls(facts, old_status, old_progress):
    """Every documentation URL the model was given: the facts' declaration links, and whatever the
    previous STATUS.md and PROGRESS.md already carried."""
    out = set()
    for d in facts.get("declarations") or []:
        if d.get("url"):
            out.add(d["url"])
    for text in (old_status or "", old_progress or ""):
        out.update(DOCS_URL_RE.findall(text))
    # The facts file may also carry URLs in other fields (a PR's entries); any URL in it was supplied.
    out.update(DOCS_URL_RE.findall(json.dumps(facts)))
    return out


def dead_links(urls, docs):
    """`[(url, why)]` for documentation links whose anchor is not on its module page.

    Only a missing page (HTTP 404) or a missing anchor is a dead link. A page that cannot be read for
    any other reason says nothing about the link, so it is returned separately as unverifiable.
    """
    dead, unverified = [], []
    prefix = DOCS_BASE.rstrip("/") + "/"
    for url in sorted(set(urls)):
        if not url.startswith(prefix) or "#" not in url:
            continue
        page, frag = url[len(prefix):].split("#", 1)
        try:
            ids = set(docs.declarations(page))
        except DocsNotFound:
            dead.append((url, "the module page does not exist"))
            continue
        except DocsError as exc:
            unverified.append((url, str(exc)[:120]))
            continue
        if html.unescape(urllib.parse.unquote(frag)) not in ids:
            dead.append((url, "no such declaration on the page"))
    return dead, unverified


def coverage_states(status_text):
    """`{roadmap_id: (readme_sha, {layer_id: state})}` from a STATUS.md's coverage headers."""
    out = {}
    if not status_text:
        return out
    for obj in files.parse_headers(status_text, files.COVERAGE_MARKER):
        layers = obj.get("layers") or []
        out[obj.get("roadmap")] = (obj.get("readme_sha"), {e.get("id"): e.get("state") for e in layers})
    return out


def coverage_problems(old_status, new_status):
    """`(failures, warnings)` comparing the new coverage headers with the previous report's."""
    failures, warnings = [], []
    new = coverage_states(new_status)
    try:
        old = coverage_states(old_status)
    except files.FormatError:
        old = {}
    states = [s for _sha, layers in new.values() for s in layers.values()]
    if states and all(s == "unassessed" for s in states):
        failures.append(
            f"every one of the {len(states)} layers is `unassessed`; judge each layer from the facts, "
            f"the previous reports and the README (`untouched` when nothing for it has landed)"
        )
    for roadmap, (sha, layers) in sorted(new.items()):
        before = old.get(roadmap)
        if before is None or before[0] != sha:
            continue  # a new README: its layers may have changed, so there is nothing to carry forward
        for layer, state in layers.items():
            prev = before[1].get(layer)
            if prev in ASSESSED and state == "unassessed":
                failures.append(
                    f"{roadmap} {layer} was `{prev}` in the previous report and is now `unassessed`; "
                    f"carry the earlier verdict forward unless this window gives a reason to change it"
                )
            elif prev == "done" and state in ("partial", "untouched"):
                warnings.append(
                    f"{roadmap} {layer} moves from `done` to `{state}`; make sure the prose says why"
                )
    return failures, warnings


def run_checks(plan, facts, status_body, section_body, old_status, old_progress, docs=None):
    """`(failures, warnings, info)`: every check, as lists of human-readable lines."""
    failures, warnings, info = [], [], []
    try:
        status_text, _progress_text, _header = apply_mod.render_update(
            plan, status_body, section_body, old_status, old_progress
        )
    except files.FormatError as exc:
        return [f"the report does not render: {exc}"], warnings, info

    prose, _block = layers_mod.split_block(status_body)
    sw, cw, cp = _words(prose), _words(section_body), _paragraphs(section_body)
    info.append(f"status prose: {sw} words (at most {STATUS_WORDS})")
    info.append(f"section: {cw} words in {cp} paragraph(s) (at most {SECTION_WORDS} in {SECTION_PARAGRAPHS})")
    if sw > STATUS_WORDS:
        failures.append(f"the status prose is {sw} words; the limit is {STATUS_WORDS}")
    if cw > SECTION_WORDS:
        failures.append(f"the section is {cw} words; the limit is {SECTION_WORDS}")
    if cp > SECTION_PARAGRAPHS:
        failures.append(f"the section has {cp} paragraphs; the limit is {SECTION_PARAGRAPHS}")
    headings = re.findall(r"^## .*$", prose, re.M)
    if [h.strip() for h in headings] != STATUS_HEADINGS:
        failures.append(f"the status prose's `##` headings are {headings}; they must be exactly {STATUS_HEADINGS}")
    if re.search(r"^# ", status_body + "\n" + section_body, re.M):
        failures.append("a body has a top-level `# ` heading; the scripts add those")

    urls = DOCS_URL_RE.findall(status_body + "\n" + section_body)
    supplied = known_urls(facts, old_status, old_progress)
    for url in sorted(set(urls) - supplied):
        failures.append(f"documentation URL not copied from the facts or the previous report: {url}")
    if docs is not None:
        dead, unverified = dead_links(set(urls) & supplied, docs)
        for url, why in dead:
            failures.append(f"dead documentation link ({why}): {url}")
        for url, why in unverified:
            warnings.append(f"could not check {url}: {why}")

    cov_fail, cov_warn = coverage_problems(old_status, status_text)
    failures += cov_fail
    warnings += cov_warn
    return failures, warnings, info


def run(plan_file, facts_file, status_body_file, section_body_file, roadmap_dir, links=True):
    """The CLI entry point. Returns the process exit code."""
    plan = json.loads(pathlib.Path(plan_file).read_text(encoding="utf-8"))
    facts = json.loads(pathlib.Path(facts_file).read_text(encoding="utf-8"))
    status_body = _read(status_body_file)
    section_body = _read(section_body_file)
    missing = [str(f) for f, t in ((status_body_file, status_body), (section_body_file, section_body)) if not t]
    if missing:
        print(f"FAIL: missing or empty: {', '.join(missing)}")
        print("NOT OK")
        return 1
    roadmap = pathlib.Path(roadmap_dir)
    old_status = _read(roadmap / plan["status_path"])
    old_progress = _read(roadmap / plan["progress_path"])
    failures, warnings, info = run_checks(
        plan, facts, status_body, section_body, old_status, old_progress, docs=Docs() if links else None
    )
    for line in info:
        print(line)
    for line in warnings:
        print(f"WARN: {line}")
    for line in failures:
        print(f"FAIL: {line}")
    print("OK" if not failures else "NOT OK")
    return 0 if not failures else 1
