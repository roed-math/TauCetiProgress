"""Roadmap attribution for pull requests that merge without a roadmap label.

A pull request landed from a commit-attributed source (`provenance.commit_sources`) carries no
`roadmap/<Area>` label: the forge that merged it keeps none, and its queue closes rather than merges
it. Its mainline commit holds the description the labeller reads, and that commit's first-parent
diff is the pull request's diff, so the labeller's own `classify` runs on them here. The result is the
label the pull request would have received, read from Git alone.
"""

import functools
import os
import subprocess

from . import git_history, window
from .provenance import ProvenanceError, commit_source, commit_sources

LABEL_PREFIX = "roadmap/"
CHUNK = 500
MAX_CACHED = 100_000
# Longest run of consecutive commits one queued pull request is read across at a range boundary.
MAX_SPLIT = 32
_cache = {}


@functools.lru_cache(maxsize=1)
def _labeller():
    from tauceti_tools.templates import import_shared

    return import_shared("roadmap_label.py", "progress_roadmap_label")


def read_commits(repo_dir, shas):
    """`{sha: (subject, body, files)}` with each commit's first-parent changed paths.

    Rename detection stays off: it is not needed for a path list, and in a partial clone it fetches
    file contents one by one, which is forty times slower.
    """
    root = os.path.realpath(repo_dir)
    wanted = list(dict.fromkeys(shas))
    found = {sha: _cache[(root, sha)] for sha in wanted if (root, sha) in _cache}
    missing = [sha for sha in wanted if sha not in found]
    for start in range(0, len(missing), CHUNK):
        chunk = missing[start:start + CHUNK]
        proc = subprocess.run(
            ["git", "-C", str(repo_dir), "log", "--no-walk=unsorted", "--no-show-signature", "--encoding=UTF-8",
             "--no-renames", "--diff-merges=first-parent", "--name-only", "-z", "--format=%x01%H%x00%B%x00",
             "--end-of-options", *chunk, "--"],
            capture_output=True,
            env=git_history.environment(history=True),
        )
        if proc.returncode:
            detail = proc.stderr.decode("utf-8", errors="replace").strip()[-4096:]
            raise window.GitError(f"git could not read commit-attributed pull requests: {detail or proc.returncode}")
        records = proc.stdout.split(b"\x01")[1:]
        if len(records) != len(chunk):
            raise window.GitError("invalid framing while reading commit-attributed pull requests")
        for record, sha in zip(records, chunk):
            fields = record.split(b"\0")
            if len(fields) < 2 or fields[0] != sha.encode("ascii"):
                raise window.GitError(f"commit {sha[:12]}: invalid framing in its message or paths")
            try:
                message = fields[1].decode("utf-8")
                # `-z` ends the message record with a newline before the first path.
                paths = [path.decode("utf-8").lstrip("\n") for path in fields[2:]]
            except UnicodeError:
                raise window.GitError(f"commit {sha[:12]}: commit text is not valid UTF-8") from None
            subject, _, body = message.partition("\n\n")
            found[sha] = (subject, body, sorted({path for path in paths if path}))
    if len(_cache) + len(missing) > MAX_CACHED:
        _cache.clear()
    _cache.update(((root, sha), found[sha]) for sha in missing)
    return found


class Attribution:
    """Commit attribution for one planning round, read from `first_parent_prs` rows.

    Only the history a report needs is read: its window, or for a roadmap's first report all history
    through the documented commit. With no commit source configured nothing is read and every answer
    is empty. Each pull request is classified once a round, however many windows it falls in.
    """

    def __init__(self, repo_dir, areas, *, tip=None, history=None):
        try:
            self.enabled = bool(commit_sources())
        except ProvenanceError as error:
            raise window.GitError(str(error)) from None
        self.repo_dir, self.areas, self.tip = repo_dir, set(areas), tip
        self.history = dict(history or {})
        self._area_of = {}

    def in_window(self, area, from_sha, to_sha):
        """`{key: earliest commit}` for the area's commit-attributed pull requests in `(from_sha, to_sha]`."""
        if not self.enabled:
            return {}
        rows = window.first_parent_prs(self.repo_dir, f"{from_sha}..{to_sha}", **self.history)
        return self._select(rows, area, start=from_sha, end=to_sha)

    def through(self, area, upto):
        """The same over all first-parent history through `upto`, to place a roadmap's first window."""
        if not self.enabled:
            return {}
        rows = window.first_parent_prs(self.repo_dir, upto, **self.history)
        return self._select(rows, area, start=None, end=upto)

    def _select(self, rows, area, *, start, end):
        groups = {}
        for sha, key in rows:
            if isinstance(key, str):
                groups.setdefault(key, []).append(sha)
        pending = {key: shas for key, shas in groups.items() if key not in self._area_of}
        if pending:
            self._classify(pending, rows, start, end)
        # Rows are newest first, so each group ends with its earliest commit.
        return {key: shas[-1] for key, shas in groups.items() if self._area_of[key] == area}

    def _classify(self, groups, rows, start, end):
        # The queue lands a split pull request as consecutive commits, which can cross either end of
        # the range. Reading the rest of that run makes every window classify it the same way.
        if rows:
            oldest_sha, oldest_key = rows[-1]
            if start is not None and oldest_key in groups and window.first_parent_before(
                    self.repo_dir, oldest_sha) == window.immutable_revision(self.repo_dir, start):
                groups[oldest_key] = groups[oldest_key] + self._run(oldest_key, self._before(start))
            newest_sha, newest_key = rows[0]
            if self.tip and newest_key in groups and newest_sha == window.immutable_revision(self.repo_dir, end):
                groups[newest_key] = self._run(newest_key, self._after(end)) + groups[newest_key]
        commits = read_commits(self.repo_dir, [sha for shas in groups.values() for sha in shas])
        classify = _labeller().classify
        for key, shas in groups.items():
            paths = sorted({path for sha in shas for path in commits[sha][2]})
            labels = {classify(commits[sha][0], commits[sha][1], paths, self.areas) for sha in shas}
            label = labels.pop() if len(labels) == 1 else ""
            area = label[len(LABEL_PREFIX):] if label.startswith(LABEL_PREFIX) else None
            self._area_of[key] = area if area in self.areas else None

    def _before(self, start):
        return window.git(["rev-list", "--first-parent", f"--max-count={MAX_SPLIT}", start, "--"],
                          self.repo_dir).split()

    def _after(self, end):
        shas = window.git(["rev-list", "--first-parent", "--reverse", f"{end}..{self.tip}", "--"],
                          self.repo_dir).split()[:MAX_SPLIT]
        if shas and window.first_parent_before(self.repo_dir, shas[0]) != window.immutable_revision(self.repo_dir, end):
            return []
        return shas

    def _run(self, key, shas):
        """The leading commits of `shas` that cite `key`, in the same order. Anything else ends the run,
        including a commit whose citation does not parse: it lies outside the reported range."""
        commits = read_commits(self.repo_dir, shas)
        overrides = (self.history.get("migration") or window.current_migration()).commits
        run = []
        for sha in shas:
            try:
                subject, body, _paths = commits[sha]
                citation = (overrides[sha] if sha in overrides
                            else commit_source(f"{subject}\n\n{body}" if body else subject))
            except ProvenanceError:
                break
            if citation is None or citation.key != key:
                break
            run.append(sha)
        return run
