"""What actually landed in a window, established from git and the published documentation.

This module exists because of a specific failure mode. PR descriptions in this project are good, but
they are *self-reported*: an author writes "this PR proves Cauchy's integral formula" and the
description is what a reporting model would otherwise read. A description can overstate, can be
edited after the fact, and -- since anyone may open a PR -- can contain text written to steer a
model. A report built from descriptions alone can announce a headline theorem the merged diff does
not contain.

The ground truth is assembled from two authorities, neither of which requires understanding Lean:

* **doc-gen4** says which declarations exist, what they are called, what kind they are, which page
  documents them, and exactly which file and lines define them. It computes that from the elaborated
  environment, which is the only way to get it right; see `docs.py` for why a text scanner cannot.
* **git blame** says who wrote those lines. A declaration belongs to this window when the commits
  that wrote its defining lines are in the window. That is an exact question with an exact answer.

The one piece of text this module reads directly is the comment block sitting immediately above a
line number the documentation supplied -- a declaration's docstring. Reading a known location is not
parsing.
"""

import re

from . import window

LEAN_PREFIX = "TauCeti/"
LEAN_SUFFIX = ".lean"

# Per-PR caps. A single PR adding hundreds of declarations is real (a big port), but a report does
# not need all of them, and an unbounded list would blow the model's context on the bootstrap
# window. Truncation is always recorded, so a report can never read as complete when it is not.
MAX_DECLS_PER_PR = 40
MAX_DOC_CHARS = 400

# Reading a module page costs a request, so a window touching an enormous number of modules is capped
# rather than left to run away. The cap is reported when it bites.
MAX_MODULES = 200

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
            ["diff", "--name-only", "--diff-filter=AMR", f"{commit}^", commit, "--", LEAN_PREFIX],
            repo_dir,
        )
        files.update(p for p in (line.strip() for line in out.splitlines())
                     if p.endswith(LEAN_SUFFIX))
    return sorted(files)


def blame_commits(repo_dir, sha, path):
    """`{line_number: commit}` for a file at a commit.

    One blame per file rather than one per declaration: a window can carry hundreds of declarations
    across a few dozen files, and the per-line answer is the same either way.
    """
    out = window.git(["blame", "--line-porcelain", "-l", sha, "--", path], repo_dir)
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


def docstring_in(text, start_line, end_line):
    """The `/-- ... -/` docstring a declaration opens with, flattened, or "".

    doc-gen4's source range BEGINS at the docstring when there is one -- `L61-L73` for a declaration
    whose `/--` is on line 61 -- so this reads from a position the documentation supplied rather than
    searching for the declaration itself. If the range does not open with `/--`, there is no
    docstring and that is all this needs to know.
    """
    src = text.splitlines()
    i = start_line - 1  # 1-based to 0-based
    if i < 0 or i >= len(src) or not src[i].lstrip().startswith("/--"):
        return ""
    collected = []
    limit = min(end_line, len(src))
    while i < limit:
        collected.append(src[i])
        if "-/" in src[i] and (len(collected) > 1 or src[i].strip() != "/--"):
            break
        i += 1
    body = "\n".join(collected).strip()
    if body.startswith("/--"):
        body = body[3:]
    cut = body.find("-/")
    if cut >= 0:
        body = body[:cut]
    return _first_sentence(" ".join(body.split()))


def unchanged_since(repo_dir):
    """`accept_older` for `Docs`: a page built at `commit` stands for `build` when `commit` is an
    ancestor of `build` and `path` is unchanged between them. Its line spans are then the lines at
    `build`, which is where `collect` blames them. Anything git cannot confirm is refused."""
    def ok(commit, path, build):
        try:
            if not (window.has_commit(repo_dir, commit) and window.is_ancestor(repo_dir, commit, build)):
                return False
            return not window.git(["diff", "--name-only", commit, build, "--", path], repo_dir).strip()
        except window.GitError:
            return False
    return ok


def collect(repo_dir, from_sha, to_sha, pr_numbers=None, docs=None):
    """The factual spine of a window.

    `to_sha` is the documented window end chosen by the plan. The documentation must still describe
    that commit when facts are collected: accepting an older build would advance the report's cursor
    past pull requests whose declarations were never inspected.
    """
    from .docs import Docs, DocsError, DocsNotFound

    docs = docs or Docs(accept_older=unchanged_since(repo_dir))
    try:
        docs_sha = docs.source_commit()
    except DocsError as exc:
        raise FactsError(f"could not determine the documented commit: {exc}") from exc

    if docs_sha != to_sha:
        raise FactsError(
            f"the documentation changed from planned commit {to_sha[:7]} to {docs_sha[:7]} "
            "before facts were collected; replan the window"
        )

    numbers = (window.window_prs(repo_dir, from_sha, docs_sha)
               if pr_numbers is None else list(pr_numbers))
    wanted = set(numbers)

    # Which commit belongs to which PR, so a blamed line can be attributed.
    pr_of_commit = {}
    log = window.git(["log", "--first-parent", "--format=%H%x09%s", f"{from_sha}..{docs_sha}"],
                     repo_dir)
    for line in log.splitlines():
        sha, _, subject = line.partition("\t")
        n = window.pr_number_of_subject(subject.strip())
        if n is not None and n in wanted:
            pr_of_commit[sha.strip()] = n

    files = changed_lean_files(repo_dir, pr_of_commit)
    truncated_modules = 0
    if len(files) > MAX_MODULES:
        truncated_modules = len(files) - MAX_MODULES
        files = files[:MAX_MODULES]

    flat = {}
    per_pr = {}
    unpublished = 0
    for path in files:
        page = module_page_for_file(path)
        if not page:
            continue
        try:
            documented = docs.declarations(page)
        except DocsNotFound as exc:
            # The docs build imports every TauCeti module. A 404 for a file still present at its
            # source commit therefore came from a different build (or a broken deploy), even if
            # another cached page has already contributed declarations. Only a file removed or
            # renamed before that commit can lack a page without making this window incoherent.
            if window.git(["ls-tree", "--name-only", docs_sha, "--", path], repo_dir).strip():
                raise FactsError(
                    f"the documentation has no page for {path}, which exists at {docs_sha[:7]}; "
                    "the site may be redeploying"
                ) from exc
            unpublished += 1
            continue
        except DocsError as exc:
            # Anything else means the documentation could not be READ, which is not the same as it
            # having nothing to say. This used to be skipped like a missing page, so a run that
            # straddled a deploy -- every page after it refused as "from another build" -- reported
            # a 72-pull-request window as having added no declarations at all.
            raise FactsError(f"could not read the documentation for {path}: {exc}") from exc
        if not documented:
            continue
        try:
            blame = blame_commits(repo_dir, docs_sha, path)
            source = window.git(["show", f"{docs_sha}:{path}"], repo_dir)
        except window.GitError:
            continue

        for name, info in documented.items():
            if info["file"] != path or info["start"] is None:
                continue
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
                "doc": docstring_in(source, info["start"], info["end"])[:MAX_DOC_CHARS],
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
        prs.append({"number": number, "declarations": keep,
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
            f"changed, {unpublished} with no published page); refusing to report the window as empty"
        )
    return {
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
