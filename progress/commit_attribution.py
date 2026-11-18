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

LABEL_PREFIX = "roadmap/"
CHUNK = 500
MAX_CACHED = 100_000
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


def area_prs(repo_dir, rows, areas):
    """`{area: {key: earliest_sha}}` for the commit-attributed keys among `first_parent_prs` rows.

    Rows are newest first, and only string keys are commit-attributed. A pull request the queue
    landed as several commits is classified on the union of their paths, once per commit message,
    and is kept only when every message names the same roadmap. `roadmap/none` and
    `roadmap/Unknown` name none, exactly as those labels do.
    """
    groups = {}
    for sha, key in rows:
        if isinstance(key, str):
            groups.setdefault(key, []).append(sha)
    if not groups:
        return {}
    commits = read_commits(repo_dir, [sha for group in groups.values() for sha in group])
    areas = set(areas)
    classify = _labeller().classify
    out = {}
    for key, group in groups.items():
        paths = sorted({path for sha in group for path in commits[sha][2]})
        labels = {classify(commits[sha][0], commits[sha][1], paths, areas) for sha in group}
        if len(labels) != 1:
            continue
        label = labels.pop()
        area = label[len(LABEL_PREFIX):] if label.startswith(LABEL_PREFIX) else None
        if area in areas:
            out.setdefault(area, {})[key] = group[-1]
    return out
