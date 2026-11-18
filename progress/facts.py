"""What actually landed in a window, established from git and the published documentation.

This module exists because of a specific failure mode. PR descriptions in this project are good, but
they are *self-reported*: an author writes "this PR proves Cauchy's integral formula" and the
description is what a reporting model would otherwise read. A description can overstate, can be
edited after the fact, and -- since anyone may open a PR -- can contain text written to steer a
model. A report built from descriptions alone can announce a headline theorem the merged diff does
not contain.

The ground truth is assembled from two authorities, neither of which requires understanding Lean:

* **doc-gen4** says which declarations exist, what they are called, what kind they are, which page
  documents them, what their docstrings say, and exactly which file and lines define them. It
  computes that from the elaborated environment, which is the only way to get it right; see
  `docs.py` for why a text scanner cannot, and for the facts bundle it is read from.
* **git blame** says who wrote those lines. A declaration belongs to this window when the commits
  that wrote its defining lines are in the window. That is an exact question with an exact answer.
"""

import os

import re

from . import files as files_mod
from . import window

LEAN_PREFIX = os.environ.get("TAUCETI_LEAN_ROOT", "TauCeti").replace(".", "/") + "/"
# How the prompts cite a report-repository pull request (`TauCeti#1234`); see `cli.cmd_prompt`.
CONTENT_NAME = os.environ.get("TAUCETI_CONTENT_NAME", "TauCeti")
LEAN_SUFFIX = ".lean"

# Per-PR caps. A single PR adding hundreds of declarations is real (a big port), but a report does
# not need all of them, and an unbounded list would blow the model's context on the bootstrap
# window. Truncation is always recorded, so a report can never read as complete when it is not.
MAX_DECLS_PER_PR = 40
MAX_DOC_CHARS = 400

# Each module costs a git blame, so a window touching an enormous number of modules is capped rather
# than left to run away. The cap is reported when it bites.
MAX_MODULES = 200
_UNSET_SOURCE = object()

_BLAME_LINE_RE = re.compile(r"\A([0-9a-f]{40}) \d+ (\d+)")


class FactsError(RuntimeError):
    """The factual spine could not be established. Never treated as "nothing happened"."""


def module_page_for_file(path):
    """The documentation page a source file's declarations live on, or None."""
    if not path.startswith(LEAN_PREFIX) or not path.endswith(LEAN_SUFFIX):
        return None
    return path[: -len(LEAN_SUFFIX)] + ".html"


def changed_lean_files(repo_dir, commits):
    """Lean files under `TauCeti/` touched by the given commits.

    Scoped to the commits of the pull requests being reported, NOT to the whole window. A window is
    a range of the mainline and carries every roadmap's work; walking all of it meant fetching a
    documentation page for hundreds of modules that could not contribute a single declaration to
    this report, and then truncating the ones that could.
    """
    files = set()
    for commit in commits:
        out = window.git(
            ["diff", "--name-only", "-z", "--diff-filter=AMR", f"{commit}^", commit, "--", LEAN_PREFIX],
            repo_dir,
        )
        files.update(p for p in out.split("\0") if p.endswith(LEAN_SUFFIX))
    return sorted(files)


def blame_commits(repo_dir, sha, path):
    """`{line_number: commit}` for a file at a commit.

    Follow the mainline so a native merge is credited for lines that entered
    through its feature branch. One blame per file rather than one per declaration: a window can carry hundreds of declarations
    across a few dozen files, and the per-line answer is the same either way.
    """
    out = window.git(["blame", "--first-parent", "--line-porcelain", "-l", sha, "--", path], repo_dir)
    lines = {}
    for line in out.splitlines():
        m = _BLAME_LINE_RE.match(line)
        if m:
            lines[int(m.group(2))] = m.group(1)
    return lines


def _first_sentence(flat):
    """The first sentence of a docstring. Docstrings here state what a lemma proves, so that is
    usually the whole useful content, and it is what a report wants to quote."""
    if not flat:
        return ""
    m = re.search(r"\.(?=\s+[A-Z(`*]|\Z)", flat)
    return (flat[: m.end()] if m else flat).strip()


def pr_commits(repo_dir, from_sha, to_sha, numbers, **history):
    """The first-parent merge commits attributed to a selected set of PRs."""
    wanted = set(numbers)
    result = {}
    for sha, number in window.first_parent_prs(repo_dir, f"{from_sha}..{to_sha}", **history):
        if number is not None and number in wanted:
            result[sha.strip()] = number
    return result


def required_modules(repo_dir, from_sha, to_sha, numbers, touched=None, **history):
    """Touched modules still present at the documented commit, including empty modules."""
    if touched is None:
        touched = changed_lean_files(repo_dir, pr_commits(repo_dir, from_sha, to_sha, numbers, **history))
    present = set(window.git(["ls-tree", "-r", "-z", "--name-only", to_sha, "--", LEAN_PREFIX],
                             repo_dir).split("\0"))
    return [path for path in touched if path in present]


@window.history_round
def collect(repo_dir, from_sha, to_sha, pr_numbers=None, docs=None, documentation=None, bundle_path=None,
            source_record=_UNSET_SOURCE):
    """The factual spine of a window.

    `to_sha` is the documented window end chosen by the plan, and `documentation` the plan's pin of
    the facts bundle that describes it (`docs` supplies a reader directly instead). Accepting any
    other build would advance the report's cursor past pull requests whose declarations were never
    inspected.
    """
    from .docs import ArtifactError, Docs, ReplanRequired

    history = {}
    if source_record is not _UNSET_SOURCE:
        from .native_source import history_options_from_record
        try:
            history = history_options_from_record(source_record)
        except (ValueError, TypeError):
            raise ReplanRequired("invalid saved source repository binding; replan") from None
    if documentation is None and docs is None:
        raise ReplanRequired("the plan pins no documentation facts; replan the window")
    if documentation is not None and not isinstance(documentation, dict):
        raise ReplanRequired("invalid saved documentation pin; replan the window")
    # Reject local plan/history contradictions before an unavailable copy can cause a fetch or
    # acquire the mechanical deployment-retry signal.
    if (documentation is not None and documentation.get("source_commit") != to_sha) \
            or not window.has_commit(repo_dir, to_sha) or not window.has_commit(repo_dir, from_sha) \
            or not window.is_ancestor(repo_dir, from_sha, to_sha):
        raise ReplanRequired("pinned facts source/window history mismatch; replan the window")
    if documentation is not None:
        docs = Docs(pin=documentation, bundle_path=bundle_path)
    docs_sha = docs.source_commit()
    if docs_sha != to_sha:
        raise ReplanRequired(f"pinned source {docs_sha} differs from plan {to_sha}; replan")

    numbers = (window.window_prs(repo_dir, from_sha, docs_sha, **history)
               if pr_numbers is None else list(pr_numbers))
    # Which commit belongs to which PR, so a blamed line can be attributed.
    pr_of_commit = pr_commits(repo_dir, from_sha, docs_sha, numbers, **history)

    files = changed_lean_files(repo_dir, pr_of_commit)
    required = set(required_modules(repo_dir, from_sha, docs_sha, numbers, touched=files, **history))
    docs.require_coverage(required)  # before presentation limits; no partial coverage accepted
    truncated_modules = 0
    if len(files) > MAX_MODULES:
        truncated_modules = len(files) - MAX_MODULES
        files = files[:MAX_MODULES]

    flat = {}
    per_pr = {}
    removed = 0
    for path in files:
        page = module_page_for_file(path)
        if not page:
            continue
        if path not in required:
            removed += 1  # removed at this source commit; it needs no documentation
            continue
        documented = docs.declarations(page)
        if not documented:
            continue
        try:
            blame = blame_commits(repo_dir, docs_sha, path)
            source = window.git(["show", f"{docs_sha}:{path}"], repo_dir)
        except window.GitError as exc:
            raise ArtifactError(f"cannot attribute pinned module {path}: {exc}") from exc

        for name, info in documented.items():
            if info["file"] != path or info["start"] is None:
                continue
            if info["end"] > len(source.splitlines()):
                raise ReplanRequired(f"pinned source range for {name} exceeds {path}")
            span = [blame.get(n) for n in range(info["start"], info["end"] + 1)]
            span = [c for c in span if c]
            if not span:
                continue
            in_window = [c for c in span if c in pr_of_commit]
            if not in_window:
                continue  # predates the window: real, but not news
            # Attributed to the commit that wrote most of it, which is the PR a reader should follow.
            chosen = max(set(in_window), key=lambda c: (in_window.count(c), c))
            number = pr_of_commit[chosen]
            flat[name] = {
                "name": name,
                "kind": info["kind"],
                "url": info["url"],
                "file": path,
                "doc": _first_sentence(" ".join(info["doc"].split()))[:MAX_DOC_CHARS],
                "pr": number,
                # Every line written in this window means the declaration is new here; only some
                # means it existed already and was revised.
                "new": len(in_window) == len(span),
            }
            per_pr.setdefault(number, []).append(name)

    prs = []
    dropped = 0
    for number in numbers:
        names = sorted(per_pr.get(number, []))
        keep = names[:MAX_DECLS_PER_PR]
        dropped += len(names) - len(keep)
        prs.append({"number": number, "cite": files_mod.pr_reference(number, CONTENT_NAME), "declarations": keep,
                    "truncated_declarations": len(names) - len(keep)})

    # Documented declarations first: one is more likely to be a result worth naming than an
    # undocumented helper. A presentation order, not a judgement.
    ordered = sorted(flat.values(), key=lambda d: (0 if d["doc"] else 1, d["name"]))

    # The backstop, whatever the cause. A window with merged pull requests and no declarations at
    # all is almost always an extraction that failed, and a report written from it tells the log and
    # Zulip that nothing happened. Refusing costs little: the cursor does not move, so the next
    # report covers these pull requests along with whatever follows them. The case it gets wrong, a
    # window whose pull requests genuinely changed no declaration, waits until one that does lands.
    if numbers and not ordered:
        raise FactsError(
            f"no declaration could be attributed to any of the window's {len(numbers)} pull "
            f"request(s) ({len(pr_of_commit)} merge commit(s) found, {len(files)} Lean file(s) "
            f"changed, {removed} removed by the documented commit); refusing to report the window as empty"
        )
    return {
        **({"source": source_record} if source_record is not _UNSET_SOURCE else {}),
        "documentation": docs.pin(),
        "from_sha": from_sha,
        "to_sha": to_sha,
        "docs_sha": docs_sha,
        "prs": prs,
        "declarations": ordered,
        "files": files,
        "counts": {
            "prs": len(prs),
            "declarations": len(ordered),
            "documented": sum(1 for d in ordered if d["doc"]),
            "new": sum(1 for d in ordered if d["new"]),
            "files": len(files),
            "truncated_declarations": dropped,
            "truncated_modules": truncated_modules,
        },
    }
