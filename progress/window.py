"""Commit windows: turning a range of TauCeti history into the set of PRs it contains.

A window is the half-open commit range `(from_sha, to_sha]` on the docs-tracking branch (see
`CODE_REF`). `from_sha` is the previous section's `to_sha`, so consecutive windows tile exactly with
no gap and no overlap, and the identity of a window does not depend on anyone's clock.

Timestamps are used only for display and for the "is an update due" cadence, never as the cursor.
A cursor made of timestamps is wrong in a way that loses work silently: a worker whose clock runs
fast stores a cursor in the future, and PRs merged in the interval then carry merge times *before*
the stored cursor and are never reported by any later window.

Pure functions take text and lists; the two that shell out to `git` are marked.
"""

import re
import subprocess

from .gh import CODE_REPO

# Squash-merge subjects made by GitHub end in `(#1234)`. The roadmap repo also has a few true
# merge commits whose subject is `Merge pull request #62 from ...`. Both forms appear in real
# history, so both are recognised; anything else contributes no PR number.
_SQUASH_RE = re.compile(r"\(#(\d+)\)\s*\Z")
_MERGE_RE = re.compile(r"\AMerge pull request #(\d+)\b")

# The TauCeti queue cites the pull request a commit lands as a `Source-PR: owner/repo#N` line, and
# its first batch (1,197 commits on 2026-11-05) put the number nowhere else. Same grammar as
# `provenance.commit_source` upstream (TauCetiProgress#52).
_TRAILER_RE = re.compile(r"^(?P<token>[A-Za-z][A-Za-z0-9-]*):[ \t]+(?P<value>\S.*?)\s*$")
_CITATION_RE = re.compile(
    r"\A(?:github:https://github\.com/)?(?P<repo>[^/\s#:]+/[^/\s#]+)#(?P<number>[1-9][0-9]*)\Z"
)


# Windows track the `docgen` branch of TauCeti, NOT `main`.
#
# `docgen` follows the most recent commit on `main` for which the API documentation has actually been
# published. Reporting against it means every declaration a report can mention already has a page, so
# a link to it is guaranteed to resolve. Reporting against `main` would routinely name results whose
# documentation had not been built yet, and every such link would 404 until the next docs build.
#
# The cost is latency: a report describes the project as of the last published docs build rather than
# the tip. That is the right trade for a document whose whole purpose is to be readable, and the
# header records the exact commit either way, so nothing is misdated.
CODE_REF = "origin/docgen"


class GitError(RuntimeError):
    """A git invocation failed. Callers treat this as "cannot decide", never as "nothing to do"."""


def git(args, repo_dir):
    """Run git in `repo_dir` and return stdout. Raises GitError on a non-zero exit."""
    proc = subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=True,
        text=True,
        errors="replace",  # commit bodies are read now, and one stray byte must not abort a plan
    )
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.returncode}")
    return proc.stdout


def pr_number_of_subject(subject):
    """The PR number a commit subject records, or None."""
    m = _SQUASH_RE.search(subject)
    if m:
        return int(m.group(1))
    m = _MERGE_RE.match(subject)
    if m:
        return int(m.group(1))
    return None


def pr_number_of_commit(subject, body="", repository=CODE_REPO):
    """The PR a mainline commit lands, or None: its `Source-PR` citation, else its subject.

    The whole body is scanned, not git's trailer block, because the queue appends repair notes
    below the citation. A citation that is malformed, repeated with different values, from another
    repository, or at odds with the subject raises GitError: a guess here moves a cursor past work.
    """
    cited = set()
    for line in body.splitlines():
        m = _TRAILER_RE.match(line)
        if m and m["token"].lower() == "source-pr":
            c = _CITATION_RE.match(m["value"])
            if c is None:
                raise GitError(f"unreadable Source-PR citation {m['value']!r}")
            cited.add((c["repo"].lower(), int(c["number"])))
    number = pr_number_of_subject(subject)
    if not cited:
        return number
    if len(cited) != 1:
        raise GitError("more than one Source-PR citation")
    repo, source = next(iter(cited))
    if repo != repository.lower():
        raise GitError(f"Source-PR cites {repo}, not {repository}")
    if number is not None and number != source:
        raise GitError(f"subject says #{number} but Source-PR says #{source}")
    return source


def first_parent_commits(repo_dir, rev, repository=CODE_REPO):
    """`(sha, pr_number, in_subject)` for every first-parent commit of `rev`, newest first.

    `pr_number` is None for a commit that lands no PR; `in_subject` says whether the subject carries
    the number, which is False for a commit that cites its PR by `Source-PR` alone.

    `--first-parent` is deliberate: it walks the mainline only, so a PR's own internal commits
    (which may themselves mention other PR numbers) never leak into the result.
    """
    out = git(["log", "--first-parent", "-z", "--format=%H%x00%s%x00%b", rev], repo_dir)
    fields = out.split("\0")
    rows = []
    for i in range(0, len(fields) - 2, 3):
        sha, subject, body = fields[i].strip(), fields[i + 1].strip(), fields[i + 2]
        try:
            number = pr_number_of_commit(subject, body, repository)
        except GitError as exc:
            raise GitError(f"commit {sha[:10]}: {exc}") from None
        rows.append((sha, number, pr_number_of_subject(subject) is not None))
    return rows


def first_parent_prs(repo_dir, rev, repository=CODE_REPO):
    """`(sha, pr_number)` for every first-parent commit of `rev` that lands a PR, newest first."""
    return [(sha, n) for sha, n, _ in first_parent_commits(repo_dir, rev, repository) if n is not None]


def is_ancestor(repo_dir, maybe_ancestor, descendant):
    """Is `maybe_ancestor` an ancestor of `descendant`?

    Asserted before every window: if the stored cursor is not an ancestor of the observed head,
    the history was rewritten or the cursor refers to another repository, and computing a range
    from it would silently produce nonsense.
    """
    proc = subprocess.run(
        ["git", "-C", str(repo_dir), "merge-base", "--is-ancestor", maybe_ancestor, descendant],
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    raise GitError(f"git merge-base failed: {proc.stderr.strip() or proc.returncode}")


def has_commit(repo_dir, sha):
    """Is `sha` an object this checkout actually holds?

    Asked before any ancestry question about a SHA that came from GitHub rather than from git, because
    `merge-base` does not answer "no" for a commit it has never heard of -- it fails, and a failure
    here aborts a whole plan. A pull request that merged after the last fetch is exactly that case,
    and it is ordinary rather than exceptional.
    """
    return subprocess.run(
        ["git", "-C", str(repo_dir), "cat-file", "-e", f"{sha}^{{commit}}"],
        capture_output=True,
    ).returncode == 0


def window_prs(repo_dir, from_sha, to_sha):
    """PR numbers merged in `(from_sha, to_sha]`, newest first, each once."""
    if not is_ancestor(repo_dir, from_sha, to_sha):
        raise GitError(
            f"{from_sha[:7]} is not an ancestor of {to_sha[:7]}; the cursor does not belong to "
            f"this history (rewritten branch, or a cursor from another repository)"
        )
    return list(dict.fromkeys(n for _, n in first_parent_prs(repo_dir, f"{from_sha}..{to_sha}")))


def head_sha(repo_dir, ref=CODE_REF):
    """One observed SHA for `ref` (the docs-tracking branch by default). Read once per plan and reused
    everywhere downstream, so the PR set and the recorded `to_sha` describe the same history."""
    return git(["rev-parse", ref], repo_dir).strip()


def first_parent_before(repo_dir, sha):
    """The first parent of `sha`, for bootstrapping a window that should *include* `sha`.

    A window is half-open, so to report the earliest PR of an area its `from_sha` must be that
    commit's parent rather than the commit itself. Without this the first PR of every area would
    be silently dropped.
    """
    out = git(["rev-parse", f"{sha}^"], repo_dir).strip()
    return out


def commit_date(repo_dir, sha):
    """The committer date of `sha` as an ISO-8601 string, for display in headings."""
    return git(["log", "-1", "--format=%cI", sha], repo_dir).strip()


def earliest_merged(repo_dir, pr_numbers, ref=CODE_REF):
    """`(pr_number, merge_sha)` for whichever of `pr_numbers` merged EARLIEST, or None.

    Earliest by position on the first-parent chain, which is the only ordering that matters here.
    Not the lowest number: numbers are assigned when a pull request is *opened*, and pull requests do
    not merge in the order they were opened. Picking the lowest number and taking the commit before
    its merge as a roadmap's starting point puts that cursor *after* any labelled pull request that
    opened later but merged sooner -- and because windows only move forward, that work is then
    unreportable for good.

    This is not hypothetical. When it was written, two of the fourteen roadmaps had a lowest-numbered
    pull request that was not their first to merge (RepresentationTheory #1227 vs #1228,
    OneParameterSemigroups #273 vs #276), so both would have silently dropped real work.
    """
    wanted = {int(n) for n in pr_numbers}
    if not wanted:
        return None
    found = None
    for sha, number in first_parent_prs(repo_dir, ref):
        if number in wanted:
            found = (number, sha)  # keep overwriting: git emits newest first, so the last is oldest
    return found


def find_merge_commit(repo_dir, pr_number, ref=CODE_REF):
    """The mainline commit that merged `pr_number`, or None.

    Used only for bootstrap, where an area's earliest labelled PR must be located in history.
    """
    for sha, number in first_parent_prs(repo_dir, ref):
        if number == pr_number:
            return sha
    return None
