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

import os
import subprocess
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

from tauceti_tools.forge import GITHUB, RepositoryRef

from . import git_history
from .provenance import (MigrationMap, ProvenanceError, commit_sources, configured_migration, resolve_source,
                         subject_number)

_HISTORY_ROUND = ContextVar("progress_history_round", default=None)
_MIGRATION = ContextVar("progress_history_migration", default=None)
MAX_HISTORY_ENTRIES = 4
MAX_HISTORY_ROWS = 100_000


def history_round(function):
    """Memoize immutable local history only for one planning round, within fixed bounds."""
    @wraps(function)
    def run(*args, **kwargs):
        migration = current_migration()
        token = _HISTORY_ROUND.set({})
        policy = _MIGRATION.set(migration)
        try:
            return function(*args, **kwargs)
        finally:
            _HISTORY_ROUND.reset(token)
            _MIGRATION.reset(policy)
    return run


def current_migration():
    return _MIGRATION.get() if _MIGRATION.get() is not None else configured_migration()


@contextmanager
def using_migration(migration):
    if not isinstance(migration, MigrationMap):
        raise GitError("history needs an explicit migration snapshot")
    token = _MIGRATION.set(migration)
    try:
        yield
    finally:
        _MIGRATION.reset(token)


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
CODE_REF = os.environ.get("TAUCETI_DOCS_REF", "origin/docgen")


class GitError(RuntimeError):
    """A git invocation failed. Callers treat this as "cannot decide", never as "nothing to do"."""


def git(args, repo_dir):
    """Run git in `repo_dir` and return stdout. Raises GitError on a non-zero exit."""
    proc = subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=True,
        text=True,
        env=git_history.environment(),
    )
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.returncode}")
    return proc.stdout


def pr_number_of_subject(subject):
    """The PR number a commit subject records, or None."""
    return subject_number(subject)


def pr_numbers_from_log(log_text):
    """PR numbers from `git log --format=%s` output, in the order given, deduped.

    Order is preserved (newest first as git emits it) because the caller reports the newest
    window boundary from the first entry.
    """
    out = []
    seen = set()
    for line in log_text.splitlines():
        n = pr_number_of_subject(line.strip())
        if n is not None and n not in seen:
            seen.add(n)
            out.append(n)
    return out


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
        env=git_history.environment(),
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
        env=git_history.environment(),
    ).returncode == 0


def immutable_revision(repo_dir, revision):
    """Resolve each endpoint once before combining different history observations."""
    parts = revision.split("..")
    if len(parts) not in (1, 2) or any(not part for part in parts):
        raise GitError("history requires a commit or a two-commit range")
    return "..".join(git(["rev-parse", "--verify", "--end-of-options", part + "^{commit}"], repo_dir).strip()
                    for part in parts)


def reachable_commits(repo_dir, revision, *, first_parent=False):
    """Immutable ancestry, sharing the existing bounded round cache."""
    revision = immutable_revision(repo_dir, revision)
    cache = _HISTORY_ROUND.get()
    key = ("reachable", os.path.abspath(repo_dir), revision, first_parent)
    if cache is not None and key in cache:
        saved = cache.pop(key)
        cache[key] = saved
        return saved
    rows = frozenset(git(["rev-list", *(["--first-parent"] if first_parent else []), revision, "--"],
                         repo_dir).splitlines())
    if cache is not None and len(rows) <= MAX_HISTORY_ROWS \
            and git(["rev-parse", "--is-shallow-repository"], repo_dir).strip() == "false":
        while cache and (len(cache) >= MAX_HISTORY_ENTRIES or sum(map(len, cache.values())) + len(rows) > MAX_HISTORY_ROWS):
            del cache[next(iter(cache))]
        cache[key] = rows
    return rows


def first_parent_sources(repo_dir, revision, *, repository=None, legacy_repository=None, legacy_through=None,
                         migration=None, include_uncited=False):
    """Qualified citations from complete commit messages, newest first.

    NUL framing preserves body lines; a repair may have appended notes after its
    provenance trailers. A qualified native citation never loses its origin.
    """
    # Use the same revision contract with or without the round cache.
    revision = immutable_revision(repo_dir, revision)
    legacy_tip = None
    if legacy_through is not None:
        if not isinstance(repository, RepositoryRef) or not isinstance(legacy_repository, RepositoryRef):
            raise GitError("cutover history needs both qualified repository namespaces")
        if git(["rev-parse", "--is-shallow-repository"], repo_dir).strip() != "false":
            raise GitError("cutover history requires a full-history checkout")
        boundary = immutable_revision(repo_dir, legacy_through)
        if boundary != legacy_through:
            raise GitError("cutover requires an immutable commit SHA")
        tip = revision.split("..")[-1]
        # One chain suffices: either this is an older retained tip, or the
        # cutover must occur on its first-parent chain. Avoid retaining both
        # full ancestry sets alongside the message rows and merge guard set.
        legacy_tip = is_ancestor(repo_dir, tip, boundary)
        chain_tip, required = (boundary, tip) if legacy_tip else (tip, boundary)
        if required not in reachable_commits(repo_dir, chain_tip, first_parent=True):
            raise GitError("cutover commit does not belong to this first-parent history")
    cache = _HISTORY_ROUND.get()
    cache_key = None
    if cache is not None and git(["rev-parse", "--is-shallow-repository"], repo_dir).strip() == "false":
        cache_key = (os.path.abspath(repo_dir), revision, legacy_repository)
        if cache_key in cache:
            saved = cache.pop(cache_key)
            cache[cache_key] = saved
            return _sources(saved, legacy_repository, migration, include_uncited=include_uncited,
                            repository=repository, legacy_through=legacy_through, legacy_tip=legacy_tip)
    try:
        rows = git_history.read(repo_dir, revision)
    except (git_history.HistoryReadError, OSError) as error:
        raise GitError(str(error)) from None
    if cache_key is not None and len(rows) <= MAX_HISTORY_ROWS:
        while cache and (len(cache) >= MAX_HISTORY_ENTRIES or sum(map(len, cache.values())) + len(rows) > MAX_HISTORY_ROWS):
            del cache[next(iter(cache))]
        cache[cache_key] = tuple(rows)
    return _sources(rows, legacy_repository, migration, include_uncited=include_uncited,
                    repository=repository, legacy_through=legacy_through, legacy_tip=legacy_tip)


def _sources(rows, legacy_repository, migration=None, *, include_uncited=False, repository=None,
             legacy_through=None, legacy_tip=None):
    out = []
    migration = migration if migration is not None else current_migration()
    legacy = legacy_through is None or legacy_tip
    for row in rows:
        # Rows are newest first. If a newer window excludes the boundary,
        # every remaining first-parent row is native. Otherwise the boundary
        # itself and all following rows belong to the legacy namespace.
        if row.sha == legacy_through:
            legacy = True
        try:
            if row.sha in migration.commits:
                citation = migration.commits[row.sha]
            else:
                if row.error:
                    raise ProvenanceError(row.error)
                namespace = legacy_repository if legacy else repository
                citation = resolve_source(row.citation, row.number, namespace)
        except ProvenanceError as error:
            raise GitError(f"commit {row.sha[:12]}: {error}") from None
        if citation is not None or include_uncited:
            out.append((row.sha, citation))
    return out


def first_parent_prs(repo_dir, revision, *, repository=None, legacy_repository=None, legacy_through=None,
                     migration=None, include_uncited=False):
    """Current-source numbers only after origin checking or an explicit import map.

    A citation from a commit-attributed source (`provenance.commit_sources`) has no report number;
    it is returned as its qualified key, a string, and never collides with an integer.
    """
    if repository is None:
        from .gh import CODE_REPO
        try:
            repository = RepositoryRef(GITHUB, CODE_REPO)
        except ValueError:
            raise GitError("configured report repository is invalid") from None
    if not isinstance(repository, RepositoryRef):
        raise GitError("report repository must be qualified")
    if legacy_repository is None and repository.forge == GITHUB:
        legacy_repository = repository
    migration = migration if migration is not None else current_migration()
    if not isinstance(migration, MigrationMap):
        raise GitError("report migration must be an explicit source map")
    try:
        sources = commit_sources()
    except ProvenanceError as error:
        raise GitError(str(error)) from None
    out = []
    seen = {}
    for sha, citation in first_parent_sources(repo_dir, revision, repository=repository,
                                               legacy_repository=legacy_repository, legacy_through=legacy_through,
                                               migration=migration, include_uncited=include_uncited):
        if citation is None:
            out.append((sha, None))
            continue
        try:
            try:
                number = migration.number(citation, repository)
            except ProvenanceError:
                if (citation.repository.key, citation.native_id) not in sources:
                    raise
                number = citation.key
            if number in seen and seen[number] != citation:
                raise ProvenanceError("distinct source citations map to the same report PR number")
            seen[number] = citation
            out.append((sha, number))
        except ProvenanceError as error:
            raise GitError(f"commit {sha[:12]}: {error}") from None
    return out


def window_prs(repo_dir, from_sha, to_sha, *, repository=None, legacy_repository=None, legacy_through=None, migration=None):
    """PR numbers merged in `(from_sha, to_sha]`, newest first.

    `--first-parent` is deliberate: it walks the mainline only, so a PR's own internal commits
    (which may themselves mention other PR numbers) never leak into the window.
    """
    if not is_ancestor(repo_dir, from_sha, to_sha):
        raise GitError(
            f"{from_sha[:7]} is not an ancestor of {to_sha[:7]}; the cursor does not belong to "
            f"this history (rewritten branch, or a cursor from another repository)"
        )
    rows = first_parent_prs(repo_dir, f"{from_sha}..{to_sha}", repository=repository,
                           legacy_repository=legacy_repository, legacy_through=legacy_through, migration=migration)
    return list(dict.fromkeys(number for _sha, number in rows))


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


def earliest_merged(repo_dir, pr_numbers, ref=CODE_REF, *, repository=None, legacy_repository=None,
                    legacy_through=None, migration=None):
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
    for sha, number in first_parent_prs(repo_dir, ref, repository=repository,
                                       legacy_repository=legacy_repository, legacy_through=legacy_through, migration=migration):
        if number in wanted:
            found = (number, sha)  # keep overwriting: git emits newest first, so the last is oldest
    return found


def find_merge_commit(repo_dir, pr_number, ref=CODE_REF, *, repository=None, legacy_repository=None,
                      legacy_through=None, migration=None):
    """The mainline commit that merged `pr_number`, or None.

    Used only for bootstrap, where an area's earliest labelled PR must be located in history.
    Qualified provenance and historical subjects use the same attribution as window and facts reads.
    """
    for sha, number in first_parent_prs(repo_dir, ref, repository=repository,
                                       legacy_repository=legacy_repository, legacy_through=legacy_through, migration=migration):
        if number == pr_number:
            return sha
    return None
