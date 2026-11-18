"""The decision: is an update due, which roadmap does it cover, and which PRs are in the window.

Everything here is script logic. No model is started until this has produced a plan, which is the
design rule for the whole tool: the parts that can be decided mechanically are decided
mechanically, and the model is left with prose.

Two thresholds, both overridable so they can be raised as the project grows:

* `IDLE_HOURS` -- an update is due only if *no* area has been updated for this long. This paces the
  whole project as a whole rather than per area: at 8 hours, about three reports a day across
  fourteen roadmaps.
* `MIN_PRS` -- the winning area must have at least this many PRs in its window. Without a floor, a
  quiet day produces a padded report about three commits.

Both belong to the default strategy, `busiest`: the area with the most PRs in its window wins.

The `threshold` strategy decides per roadmap and has no project-wide cadence. Let N be the PRs in a
roadmap's window and T the days since its last report landed (since its README was added, for one
never reported). A roadmap qualifies when N > 0 and one of these holds: N + T > `THRESHOLD`; it is
archived under `Completed/`; or it is not yet assessed (`assessment_gap`). A busy roadmap therefore
qualifies quickly and a quiet one after a while. Among those that qualify the unassessed go first,
then the largest N + T. The merge gate's per-roadmap interval (`gate.MIN_REPORT_INTERVAL_HOURS`)
is applied too, so the planner does not choose a report the gate would refuse.
"""

import os

import datetime
import json
import pathlib
import re

from . import files, gh, layers as layers_mod, window
from .gate import MIN_REPORT_INTERVAL_HOURS
from .window import CODE_REF

IDLE_HOURS = 8.0
MIN_PRS = 10
STRATEGIES = ("busiest", "threshold")
THRESHOLD = 10.0
# How long an open progress pull request keeps marking its area in flight; past this it is assumed
# stuck and the area is reported again. The gate's run waits in the roadmap repository's Actions
# queue, which in November 2026 held 29 hours of work, and a replacement rejoins the back of it.
# A caller still waiting for the roadmap `build` makes that two passes.
STALE_PR_HOURS = 72.0

# The commit-subject prefix that marks a merged progress update. `apply` uses it as the PR title
# prefix, and a squash merge carries the PR title into the commit subject, so this one string links
# the cheap `due` check to the update mechanism.
COMMIT_PREFIX = "progress:"

STATUS_NAME = "STATUS.md"
PROGRESS_NAME = "PROGRESS.md"

# Where areas live in the roadmap repo. `Completed/` holds finished roadmaps (EffectiveBounds).
AREAS_DIR = os.environ.get("TAUCETI_ROADMAP_DIR", "TauCetiRoadmap")
COMPLETED_DIR = os.environ.get("TAUCETI_COMPLETED_DIR", "Completed")


class NotDue(Exception):
    """No update is due. Carries the human-readable reason; the CLI exits 75 on it, which is the
    worker's `EX_NOPROGRESS`, so a round falls through to other work."""


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def _parse_iso(text):
    # GitHub returns `...Z`; `fromisoformat` wants an offset on older Pythons.
    return datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))


def discover_areas(roadmap_dir):
    """`{area: relative_dir}` for every roadmap area in a checkout.

    An area is a top-level directory under `TauCetiRoadmap/` that contains a `README.md` -- exactly
    the rule `TauCeti/scripts/roadmap_label.py:canonical_areas` uses, so the areas here and the
    `roadmap/<Area>` labels can never disagree. Archived roadmaps under `Completed/` are included so
    their existing status files are still found, but they are excluded from selection unless new
    PRs arrive for them.

    Deliberately keyed on README presence and nothing else: an area may or may not carry a
    `Suggested.lean`, the file has been renamed before, and nested sub-roadmaps (RepresentationTheory)
    have their own READMEs one level down while remaining a single labelled area.
    """
    root = pathlib.Path(roadmap_dir)
    inner = root / AREAS_DIR
    base = inner if inner.is_dir() else root
    found = {}
    for parent, prefix in ((base, AREAS_DIR if base is inner else ""),
                           (root / COMPLETED_DIR, COMPLETED_DIR)):
        if not parent.is_dir():
            continue
        for child in sorted(parent.iterdir()):
            if child.is_dir() and (child / "README.md").is_file():
                found[child.name] = f"{prefix}/{child.name}" if prefix else child.name
    return found


def read_area_layers(roadmap_dir, rel_dir):
    """`(layers, readme_sha)` for one area: the README's layer headings and the hash of its text.

    Both go into the plan so the writing model is told exactly which layers to assess, and so the
    assessment it produces is bound to the README it was made against rather than to whatever
    README is current when someone reads it.
    """
    text = (pathlib.Path(roadmap_dir) / rel_dir / "README.md").read_text(encoding="utf-8")
    return layers_mod.headings(text), layers_mod.readme_sha(text)


def read_sub_roadmaps(roadmap_dir, area, rel_dir):
    """The sub-roadmaps of an umbrella area, each with its own layers and README hash.

    A sub-roadmap is a directory directly below the area that is itself a roadmap: a `README.md`
    and a `Suggested.lean`, which keeps a `references/` folder with a README of its own out. That is
    the consumer's rule (`read_roadmaps` in TauCeti's `scripts/roadmap_progress.py`). The area's
    label covers all of them, so its report is the only account of them, and each gets its own
    coverage header, bound to its own README. One with no layer headings, or whose directory name
    the header format cannot carry, is left out: there is nothing it could say about it.
    """
    base = pathlib.Path(roadmap_dir) / rel_dir
    out = []
    for child in sorted(p for p in base.iterdir() if p.is_dir()):
        if not ((child / "README.md").is_file() and (child / "Suggested.lean").is_file()):
            continue
        if not re.match(r"\A[A-Za-z0-9]+\Z", child.name):
            print(f"{area}/{child.name}: not an alphanumeric directory name; its layers stay unassessed")
            continue
        text = (child / "README.md").read_text(encoding="utf-8")
        found = layers_mod.headings(text)
        if found:
            out.append({"roadmap": files.sub_roadmap_id(area, child.name),
                        "readme": f"{rel_dir}/{child.name}/README.md",
                        "readme_sha": layers_mod.readme_sha(text), "layers": found})
    return out


def read_area_files(roadmap_dir, rel_dir):
    """`(status_text_or_None, progress_text_or_None)` for one area."""
    base = pathlib.Path(roadmap_dir) / rel_dir
    status = base / STATUS_NAME
    progress = base / PROGRESS_NAME
    return (
        status.read_text(encoding="utf-8") if status.is_file() else None,
        progress.read_text(encoding="utf-8") if progress.is_file() else None,
    )


def last_update_age_hours(commits, now=None):
    """Hours since the newest merged progress update, or None if there has never been one.

    Reads the roadmap repo's own commit history, so the cadence is measured against when reports
    actually *landed* -- not against the TauCeti window they describe, and not against any local
    state a fleet could not share.
    """
    now = now or _utcnow()
    newest = None
    for date, subject in commits:
        if subject.startswith(COMMIT_PREFIX):
            ts = _parse_iso(date)
            if newest is None or ts > newest:
                newest = ts
    if newest is None:
        return None
    return (now - newest).total_seconds() / 3600.0


def check_cadence(commits, idle_hours=IDLE_HOURS, now=None):
    """Raise NotDue unless enough time has passed since the last landed update."""
    age = last_update_age_hours(commits, now=now)
    if age is None:
        return "no progress update has ever landed"
    if age < idle_hours:
        raise NotDue(f"last progress update landed {age:.1f}h ago (< {idle_hours:g}h)")
    return f"last progress update landed {age:.1f}h ago"


def roadmap_commits(roadmap_dir, ref):
    """Read the caller's freshly fetched ref, never its unpublished worktree.

    The worker already fetches a full Roadmap clone before planning. An explicit
    ref opts into that observation; standalone callers retain the API check.
    """
    if window.git(["rev-parse", "--is-shallow-repository"], roadmap_dir).strip() != "false":
        raise window.GitError("roadmap cadence requires complete Git history")
    sha = window.git(["rev-parse", "--verify", "--end-of-options", ref + "^{commit}"], roadmap_dir).strip()
    rows = window.git(["log", "--format=%cI%x09%s", sha, "--"], roadmap_dir)
    return [tuple(line.split("\t", 1)) for line in rows.splitlines() if "\t" in line]


def is_complete(rel_dir):
    """Maintainers declare a roadmap complete by archiving it under `Completed/`."""
    return rel_dir.rpartition("/")[0] == COMPLETED_DIR.strip("/")


def report_dates(roadmap_dir, ref=None):
    """`(reported, added)`, each `{path: datetime}`, from the roadmap history at `ref` (default HEAD).

    `reported` gives each `PROGRESS.md` the committer date of the newest commit touching it, which is
    what the merge gate's collector reads for its per-roadmap interval. `added` gives each
    `README.md` the commit that created it at that path. A shallow history is refused: its boundary
    commit appears to have written every file.
    """
    if window.git(["rev-parse", "--is-shallow-repository"], roadmap_dir).strip() != "false":
        raise window.GitError("the threshold strategy requires complete roadmap history")
    rev = window.git(["rev-parse", "--verify", "--end-of-options", (ref or "HEAD") + "^{commit}"],
                     roadmap_dir).strip()

    def log(name, *options):
        out = window.git(["log", "--format=%x00%cI", "--name-only", "--no-renames", *options, rev,
                          "--", f":(glob)**/{name}"], roadmap_dir)
        for entry in out.split("\0")[1:]:
            date, *paths = entry.split("\n")
            for path in filter(None, paths):
                yield path, _parse_iso(date)

    reported = {}
    for path, date in log(PROGRESS_NAME):
        reported.setdefault(path, date)
    # The log is newest first, so `dict` keeps each README's oldest addition.
    return reported, dict(log("README.md", "--diff-filter=A"))


def assessment_gap(roadmap_dir, area, rel_dir, status_text):
    """Why the roadmap counts as not yet assessed, or None when its latest report assesses it.

    A roadmap's layers read as unassessed unless its STATUS.md carries a coverage header bound to
    the README as it now stands, for the roadmap and for each of its sub-roadmaps. A README that
    names no layers has nothing to assess, so any report counts.
    """
    if not status_text:
        return "no report yet"
    own_layers, own_sha = read_area_layers(roadmap_dir, rel_dir)
    needed = ([(area, own_sha)] if own_layers else []) + [
        (sub["roadmap"], sub["readme_sha"]) for sub in read_sub_roadmaps(roadmap_dir, area, rel_dir)
    ]
    try:
        headers = {h.get("roadmap"): h for h in files.parse_headers(status_text, files.COVERAGE_MARKER)}
    except files.FormatError:
        return "its report's coverage header cannot be read"
    for roadmap, sha in needed:
        if roadmap not in headers:
            return f"its report does not assess {roadmap}"
        if headers[roadmap].get("readme_sha") != sha:
            return f"{roadmap}'s README changed since its report"
    return None


def assess(n_prs, last_report, complete, now, threshold=THRESHOLD, unassessed=None, added=None,
           min_interval_hours=MIN_REPORT_INTERVAL_HOURS):
    """The `threshold` rule for one roadmap: `(qualifies, days, note, qualifies_from)`.

    `days` is T: the days since `last_report`, or since `added` for a roadmap never reported, or
    None when neither is known. `unassessed` is `assessment_gap`'s reason. `qualifies_from` is the
    earliest time the roadmap qualifies with the PRs it has now, when that is known and later than
    `now`; more PRs arriving can only bring it forward.
    """
    since = last_report if last_report is not None else added
    days = None if since is None else (now - since).total_seconds() / 86400.0
    if n_prs <= 0:
        return False, days, "no new pull requests", None
    score = n_prs + (days or 0.0)
    rule_from = None
    if complete:
        rule = "declared complete"
    elif unassessed:
        rule = f"not yet assessed: {unassessed}"
    elif score > threshold:
        rule = f"N+T = {score:.1f} > {threshold:g}"
    else:
        rule = f"N+T = {score:.1f}, needs > {threshold:g}"
        if since is None:
            return False, days, rule, None  # no date to count from: only another PR can help
        rule_from = since + datetime.timedelta(days=threshold - n_prs)
    if last_report is not None and days * 24.0 < min_interval_hours:
        gate_from = last_report + datetime.timedelta(hours=min_interval_hours)
        note = (f"reported {days * 24.0:.1f}h ago; the merge gate allows one report per "
                f"{min_interval_hours:g}h ({rule})")
        return False, days, note, max(gate_from, rule_from or gate_from)
    return rule_from is None, days, rule, rule_from


def choose_by_threshold(areas, candidates, skipped, dates, roadmap_dir, to_sha, now,
                        threshold=THRESHOLD, table_path=None, held_until=None):
    """The `threshold` strategy's choice: `(ranked, reason)` with the winner first, or NotDue.

    The table gets a row for every roadmap, those that are not candidates with their reason from
    `skipped`. It is written before NotDue is raised, so a caller polling for work learns from
    `next_qualifies_at` when to look again. `held_until` maps a roadmap held by an open report to
    the time that report goes stale; the row gives that time as its `qualifies_from`.
    """
    reported, added = dates
    by_area = {c["area"]: c for c in candidates}
    rows, qualifying = [], []
    for area, rel_dir in areas.items():
        last = reported.get(f"{rel_dir}/{PROGRESS_NAME}")
        row = {"area": area, "rel_dir": rel_dir, "complete": is_complete(rel_dir), "prs": 0,
               "assessed": None, "qualifies": False, "qualifies_from": None}
        c = by_area.get(area)
        if c is None:
            days = None if last is None else (now - last).total_seconds() / 86400.0
            row["note"] = next((m[len(area) + 2:] for m in reversed(skipped) if m.startswith(f"{area}: ")),
                               "not a candidate")
            if (held_until or {}).get(area) is not None:
                row["qualifies_from"] = held_until[area].astimezone(datetime.timezone.utc).isoformat(
                    timespec="seconds")
        else:
            gap = assessment_gap(roadmap_dir, area, rel_dir, c["status_text"])
            ok, days, note, qualifies_from = assess(
                len(c["prs"]), last, row["complete"], now, threshold=threshold, unassessed=gap,
                added=added.get(f"{rel_dir}/README.md"))
            c.update(days=days, note=note, reported=last is not None, assessed=gap is None)
            row.update(prs=len(c["prs"]), assessed=gap is None, qualifies=ok, note=note)
            if qualifies_from is not None:
                row["qualifies_from"] = qualifies_from.astimezone(datetime.timezone.utc).isoformat(
                    timespec="seconds")
            if ok:
                qualifying.append(c)
        row["days"] = None if days is None else round(days, 2)
        row["score"] = round(row["prs"] + (days or 0.0), 2)
        rows.append(row)

    def score(c):
        return len(c["prs"]) + (c["days"] or 0.0)

    # Among equals the most PRs, then by name, so the choice is deterministic.
    ranked = sorted(qualifying, key=lambda c: (c["assessed"], -score(c), -len(c["prs"]), c["area"]))
    order = {c["area"]: i for i, c in enumerate(ranked)}
    rows.sort(key=lambda r: (order.get(r["area"], len(order)), -r["score"], r["area"]))
    upcoming = [r["qualifies_from"] for r in rows if r["qualifies_from"]]
    if table_path:
        from .docs import ArtifactError, store

        table = {"version": 1, "strategy": "threshold", "threshold": threshold,
                 "min_interval_hours": MIN_REPORT_INTERVAL_HOURS,
                 "generated_at": now.isoformat(timespec="seconds"), "to_sha": to_sha,
                 "chosen": ranked[0]["area"] if ranked else None,
                 "next_qualifies_at": min(upcoming) if upcoming else None, "rows": rows}
        raw = (json.dumps(table, indent=2, sort_keys=True) + "\n").encode("utf-8")
        if not store(pathlib.Path(table_path), raw):
            raise ArtifactError(f"could not atomically retain the threshold table at {table_path}")
    if not ranked:
        waiting = [r for r in rows if r["prs"] > 0]
        closest = ", ".join(f"{r['area']} ({r['note']})" for r in waiting[:3])
        raise NotDue(f"no roadmap qualifies yet: {len(waiting)} have new pull requests"
                     + (f"; closest: {closest}" if closest else ""))
    best = ranked[0]
    if best["days"] is None:
        since = "never reported"
    elif not best["reported"]:
        since = f"never reported, added {best['days']:.1f} days ago"
    else:
        since = f"last reported {best['days']:.1f} days ago"
    rank = f"the largest N+T ({score(best):.1f}) of {len(ranked)} qualifying roadmap(s)"
    if not best["assessed"]:
        unassessed = sum(1 for c in ranked if not c["assessed"])
        rank = (f"unassessed roadmaps go first; the largest N+T ({score(best):.1f}) of {unassessed} "
                f"unassessed among {len(ranked)} qualifying roadmap(s)")
    return ranked, (f"{best['area']} has {len(best['prs'])} PR(s) since {best['from_sha'][:7]}, "
                    f"{since} ({best['note']}); {rank}")


def area_window(repo_dir, area_prs, from_sha, to_sha, commit_prs=(), **history):
    """PR numbers in `(from_sha, to_sha]` that belong to an area, newest first.

    Attribution is an intersection: git says which PRs are in the range, one label query says
    which PRs belong to the area. Order comes from git (newest first). `commit_prs` are the area's
    commit-attributed keys (`commit_attribution.area_prs`), which stand in for labels.
    """
    revision = window.immutable_revision(repo_dir, f"{from_sha}..{to_sha}")
    from_sha, to_sha = revision.split("..")
    numbers = window.window_prs(repo_dir, from_sha, to_sha, **history)
    # Native API merge SHAs must agree with the actual first-parent attribution
    # in this window, including after an area has already acquired its cursor.
    # Otherwise a later recognized PR could advance past an invisible merge.
    if isinstance(area_prs, dict) and any(area_prs.values()):
        rows = dict(window.first_parent_prs(repo_dir, revision, include_uncited=True, **history))
        unaccounted = _unaccounted_merges(area_prs, rows, window.reachable_commits(repo_dir, revision),
                                         migration=history.get("migration"))
        if unaccounted:
            raise window.GitError("native merge has no matching source provenance in this window: "
                                  + ", ".join(f"#{n}" for n in unaccounted[:5]))
    wanted = set(area_prs) | set(commit_prs)
    return [n for n in numbers if n in wanted]


def unaccounted_prs(repo_dir, area_prs, ref=CODE_REF, **history):
    """Labelled pull requests that are in `ref`'s history but were not recognised in it.

    `earliest_merged` finds pull requests by matching their number in commit subjects, so one whose
    merge subject does not carry its number is invisible -- and the starting point would then be
    computed from a later merge, silently skipping it. Two implementations agreeing does not catch
    this: they share the omission, which is exactly how three earlier versions of this check passed
    review while being wrong.

    Missing numbers include rejected PRs and merges after the documented tip. Only a GitHub-native
    merge has a trustworthy API merge commit: an unmerged PR's `merge_commit_sha` can name a test
    merge, even one reachable from the code branch. Bors squash merges are recognised by their
    numbered mainline subjects, not GitHub's merged flag or an editable title.

    Any native merge genuinely present but unrecognised is returned, and bootstrap refuses rather
    than guessing a cursor. Bors squash merges rely on bors's guaranteed numbered commit subjects;
    GitHub does not provide a trustworthy landed SHA for those closures.
    """
    revision = window.immutable_revision(repo_dir, ref)
    rows = dict(window.first_parent_prs(repo_dir, revision, include_uncited=True, **history))
    return _unaccounted_merges(area_prs, rows, window.reachable_commits(repo_dir, revision),
                               migration=history.get("migration"))


def _unaccounted_merges(area_prs, rows, reachable, *, migration=None):
    migration = migration if migration is not None else window.current_migration()
    seen = set(rows.values())
    return [number for number, sha in sorted(area_prs.items())
            if sha in reachable and (rows[sha] != number if sha in rows else number not in seen)
            and not (sha in migration.commits and migration.commits[sha] is None)]


def bootstrap_from_sha(repo_dir, area, area_prs, ref=CODE_REF, commit_prs=None, **history):
    """A `from_sha` for an unreported area, or None if no labelled merge is in `ref` yet.

    The window is half-open, so the cursor must be the *parent* of the area's earliest labelled
    merge; using the merge itself would drop that first PR from every area's first report.
    `commit_prs` maps the area's commit-attributed keys to their earliest commits in `ref`, and
    competes with the labelled merges for that first place.
    """
    numbers = list(area_prs)
    landed = list(dict.fromkeys((commit_prs or {}).values()))
    if not numbers and not landed:
        return None
    # A shallow history can contain a later labelled merge while omitting the first one. Refuse
    # before choosing a cursor or treating an unfetched earlier merge as documentation lag.
    if window.git(["rev-parse", "--is-shallow-repository"], repo_dir).strip() == "true":
        raise window.GitError(f"a full-history checkout is required to bootstrap an area ({area})")
    ref = window.immutable_revision(repo_dir, ref)
    if numbers:
        found = _earliest_labelled(repo_dir, area, area_prs, ref, **history)
        if found is not None:
            landed.append(found)
    if not landed:
        return None
    # Every candidate is on `ref`'s first-parent chain, where the earlier commit is the ancestor.
    earliest = landed[0]
    for sha in landed[1:]:
        if window.is_ancestor(repo_dir, sha, earliest):
            earliest = sha
    return window.first_parent_before(repo_dir, earliest)


def _earliest_labelled(repo_dir, area, area_prs, ref, **history):
    numbers = list(area_prs)
    # The earliest to MERGE, not the lowest-numbered. Numbers are assigned when a pull request is
    # opened, and pull requests do not merge in the order they were opened; starting from the lowest
    # number would put the cursor after any labelled pull request that opened later but merged
    # sooner, making that work unreportable for good. Two of the fourteen roadmaps had exactly that
    # shape when this was written.
    # Refuse rather than guess if any labelled pull request is in this history but was not recognised
    # in it: the cursor would then be computed from a later merge and skip it, permanently.
    unaccounted = unaccounted_prs(repo_dir, area_prs, ref=ref, **history)
    if unaccounted:
        raise window.GitError(
            f"{area} has labelled pull requests in {ref} whose merge commits carry no pull request "
            f"number ({', '.join(f'#{n}' for n in unaccounted[:5])}); the start of its history "
            f"cannot be determined from commit subjects"
        )
    found = window.earliest_merged(repo_dir, numbers, ref=ref, **history)
    # None: GitHub's label query follows merges before docgen necessarily reaches them. In a
    # complete checkout, unaccounted_prs already ruled out an unrecognised merge in this history.
    # Defer this area until one of its merges reaches ref; do not invent or advance a cursor.
    return None if found is None else found[1]


def _own_login():
    """The login this operator is authenticated as. Empty if it cannot be read: then only the
    organisation's own pull requests mark an area in flight, which errs toward doing work."""
    try:
        return gh.gh(["api", "user", "--jq", ".login"]).strip()
    except gh.GhError:
        return ""


def _pr_owner(pr):
    return ((pr.get("headRepositoryOwner") or {}).get("login") or "")


def in_flight_areas(open_prs, now=None, stale_hours=STALE_PR_HOURS, owners=None, owner_filter=None):
    """`({area: pr}, [stale_note])` from the open progress pull requests.

    The branch is `progress/<from7>-<to7>/<Area>`, so the area is the last segment.

    An open progress pull request marks its area in flight, but only for a while. One that the merge
    check refuses permanently never merges and never closes itself, and treating it as in flight
    forever would stop that roadmap being reported by *anyone*, including the maintainer, with no
    signal beyond the area quietly never appearing again. Past `stale_hours` it stops blocking and is
    reported as a note instead, so the next round covers the area over a wider window and the
    abandoned pull request becomes visible rather than merely obstructive.

    A pull request with no or unreadable `createdAt` keeps blocking. Age is the only evidence that it
    is stuck, and without it the safe assumption is that it is still in flight: opening a duplicate is
    worse than waiting.

    `owners` restricts whose pull requests may mark an area in flight, and defaults to the roadmap
    organisation plus this operator. Anyone may open a pull request on a `progress/*` branch, so
    without this a stranger could freeze a roadmap indefinitely by opening one a day -- staleness
    expiry bounds a single one, but not a stream of them. A stranger's report still merges
    on its own merits; it simply does not stop anyone else from writing one. The cost is that two
    operators publishing from their own forks may duplicate a window, which wastes a round; being
    unable to report at all is the worse failure.
    """
    if owners is not None and owner_filter is not None:
        raise ValueError("choose either owner names or a qualified owner filter")
    now = now or _utcnow()
    if owners is None and owner_filter is None:
        # Only non-empty logins. A pull request whose `headRepositoryOwner` is absent -- a deleted
        # fork, say -- reads as `""`, so putting `""` in the trusted set (which is what an
        # unreadable `/user` would do) would let exactly those block an area.
        owners = {gh.ROADMAP_REPO.split("/")[0]} | {x for x in (_own_login(),) if x}
    elif owners is not None:
        owners = {x for x in owners if x}
    blocked, stale = {}, []
    for pr in open_prs:
        parts = (pr.get("headRefName") or "").split("/")
        if len(parts) < 3:
            continue
        area_name = parts[-1]
        if not (owner_filter(pr) if owner_filter is not None else _pr_owner(pr) in owners):
            # Not ours and not the organisation's: it may well be a genuine report from another
            # contributor, and it can merge, but it does not get to stop us writing one.
            continue
        age_hours = None
        created = pr.get("createdAt")
        if created:
            try:
                age_hours = (now - _parse_iso(created)).total_seconds() / 3600.0
            except ValueError:
                age_hours = None
        if age_hours is not None and age_hours > stale_hours:
            stale.append(
                f"{area_name}: PR #{pr.get('number')} has been open {age_hours / 24:.1f} days "
                f"without merging, so it no longer marks the area in flight -- close it if it is dead"
            )
            continue
        blocked[area_name] = pr
    return blocked, stale


@window.history_round
def build_plan(
    roadmap_dir,
    code_dir,
    commits=None,
    open_prs=None,
    ref=CODE_REF,
    idle_hours=IDLE_HOURS,
    min_prs=MIN_PRS,
    stale_hours=STALE_PR_HOURS,
    now=None,
    only_area=None,
    docs=None,
    roadmap_ref=None,
    source=None,
    strategy="busiest",
    threshold=THRESHOLD,
    table_path=None,
    label_cache=None,
    skip_areas=(),
):
    """The whole decision. Returns a plan dict, or raises NotDue.

    `commits` and `open_prs` may be supplied by the caller (the worker already holds them, and the
    tests inject fixtures); otherwise they are fetched.

    `strategy` is one of STRATEGIES (see the module docstring). Under `threshold`, `idle_hours`,
    `min_prs` and `commits` are unused, the roadmap history is read at `roadmap_ref` (default
    HEAD), and `table_path`, if given, receives every roadmap's standing under the rule whether or
    not one qualifies. `label_cache` is a file for the `label_cache` module, read through the bound
    source when there is one, through GitHub otherwise. `skip_areas` are never chosen under either
    strategy.
    """
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown strategy {strategy!r}; one of {', '.join(STRATEGIES)}")
    if source is not None:
        from .native_source import NativeSource
        if not isinstance(source, NativeSource):
            raise ValueError("planning source must be explicitly bound")
    history = source.history_options if source else {}
    if label_cache is not None and source is not None:
        from .native_source import LabelReader

        labels_reader = LabelReader(source.content)
    now = now or _utcnow()
    if strategy == "busiest":
        if commits is None:
            commits = (roadmap_commits(roadmap_dir, roadmap_ref) if roadmap_ref is not None
                       else source.roadmap.recent_commits() if source else gh.recent_roadmap_commits())
        cadence_reason = check_cadence(commits, idle_hours=idle_hours, now=now)

    if open_prs is None:
        open_prs = source.roadmap.open_reports() if source else gh.open_progress_prs()
    owner_filter = source.roadmap.report_owner_filter() if source else None
    if strategy == "threshold":
        # A caller that polls would write a duplicate of every report another operator has just
        # opened, so any report whose head repository still exists holds its area until it is stale.
        owner_filter = ((lambda pr: pr.get("source_head_repository") is not None) if source
                        else (lambda pr: bool(_pr_owner(pr))))
    blocked, stale = in_flight_areas(open_prs, now=now, stale_hours=stale_hours, owner_filter=owner_filter)

    areas = discover_areas(roadmap_dir)
    all_areas = set(areas)
    if only_area:
        if only_area not in areas:
            raise NotDue(f"{only_area} is not a roadmap area in this checkout")
        areas = {only_area: areas[only_area]}

    # The window ends at the commit the PUBLISHED DOCUMENTATION describes, not at the branch tip.
    #
    # `docgen` nominates the most recent commit with a published build, but the deploy is
    # independent, so the branch can sit ahead of the site for a while. Ending the window at the tip
    # would record a cursor covering work the report never described -- the next window starts after
    # it, so that work would never be reported at all -- and would offer links for results whose
    # pages do not exist yet. Ending it at the documented commit makes the header, the prose and the
    # links describe one and the same state.
    from .docs import ArtifactError, discover

    if docs is None:
        docs = discover()
    docs_sha = docs.source_commit()
    tip = window.head_sha(code_dir, ref=ref)
    if not window.has_commit(code_dir, docs_sha) or not window.is_ancestor(code_dir, docs_sha, tip):
        raise ArtifactError(
            f"generation {docs.generation}: source {docs_sha} is not in {ref} history ({tip})"
        )
    to_sha = docs_sha

    candidates = []
    skipped = list(stale)
    held_until = {}
    eligible = {}
    for area, rel_dir in areas.items():
        if area in skip_areas:
            skipped.append(f"{area}: set aside with --skip-area")
            continue
        if area in blocked:
            skipped.append(f"{area}: PR #{blocked[area]['number']} is still open")
            try:
                held_until[area] = _parse_iso(blocked[area]["createdAt"]) + datetime.timedelta(hours=stale_hours)
            except (KeyError, TypeError, ValueError, AttributeError):
                pass  # an unreadable age holds the area with no end in sight
            continue
        status_text, progress_text = read_area_files(roadmap_dir, rel_dir)
        try:
            from_sha = files.cursor(progress_text) if progress_text else None
        except files.FormatError as exc:
            skipped.append(f"{area}: unparseable PROGRESS.md ({exc})")
            continue

        if from_sha == to_sha:
            skipped.append(f"{area}: already at {to_sha[:7]}")
            continue
        eligible[area] = (rel_dir, status_text, progress_text, from_sha)

    # Only histories used below need reading. Current and malformed cursors
    # must not turn a neighbour's useful plan into an all-area history scan.
    if not eligible:
        histories = {}
    elif label_cache is not None:
        histories = (labels_reader.read(label_cache, eligible, now=now) if source
                     else gh.cached_closed_prs_for_areas(label_cache, eligible, now=now))
    else:
        histories = source.content.labelled_histories(eligible) if source else gh.closed_prs_for_areas(eligible)

    # Commit-attributed pull requests have no label history; their commits are classified instead,
    # against every roadmap so that a pull request naming another area is not credited here.
    from .commit_attribution import area_prs as commit_area_prs

    attributed = commit_area_prs(code_dir, window.first_parent_prs(code_dir, tip, **history),
                                 all_areas) if eligible else {}

    def consider(area):
        rel_dir, status_text, progress_text, from_sha = eligible[area]
        area_prs = histories[area]
        commit_prs = attributed.get(area, {})
        if not area_prs and not commit_prs:
            skipped.append(f"{area}: no merged PRs yet")
            return

        bootstrapped = False
        if from_sha is None:
            from_sha = bootstrap_from_sha(code_dir, area, area_prs, ref=ref, commit_prs=commit_prs, **history)
            bootstrapped = True
            if from_sha is None:
                skipped.append(f"{area}: no labelled merges in {ref} yet; waiting for documentation")
                return
        if from_sha == to_sha:
            skipped.append(f"{area}: already at {to_sha[:7]}")
            return

        # An area bootstrapped from a pull request that merged after the documented build has a
        # cursor ahead of `to_sha`. Nothing is wrong: its window simply has not been published yet,
        # and a later run closes it once the documentation catches up. Skipping it here rather than
        # letting `window_prs` refuse is the difference between one area waiting a day and EVERY
        # area waiting indefinitely -- the exception aborts the whole plan, so a single new roadmap
        # used to stop all reporting for the fleet until someone read the traceback.
        #
        # The refusal itself stays: a cursor that is in neither the documented history nor `ref`'s
        # is the rewritten-branch case, and that must still be loud, so it falls through to
        # `area_window` and raises exactly as before.
        if not window.is_ancestor(code_dir, from_sha, to_sha) and window.is_ancestor(
            code_dir, from_sha, tip
        ):
            # Say WHICH of the two this is. A bootstrapped cursor ahead of the build is ordinary and
            # resolves itself on the next deploy. A RECORDED one ahead of it means the documentation
            # went backwards -- a rollback, or a cursor that should never have been written -- and the
            # two want different reactions from a reader, so they must not share a sentence.
            if bootstrapped:
                skipped.append(
                    f"{area}: its first pull request merged after the documented build "
                    f"{to_sha[:7]}; the window is not published yet"
                )
            else:
                skipped.append(
                    f"{area}: its recorded cursor {from_sha[:7]} is AHEAD of the documented build "
                    f"{to_sha[:7]} — the documentation went backwards, or that cursor is wrong; "
                    f"waiting rather than reporting a backwards window"
                )
            return

        # The SHA window is the authority on what belongs in a report, and deliberately the ONLY
        # authority. An earlier version also subtracted every PR number any previous section had
        # claimed, as a guard against a PR being relabelled after it was reported. That made a
        # section's `prs` list -- attacker-supplied metadata in a file anyone may append to -- able to
        # suppress real work permanently: a report claiming thousands of numbers would leave every
        # later window for that roadmap looking empty, with no error anywhere.
        #
        # The failure it guarded against is a PR appearing in two reports after someone changed its
        # label. That is cosmetic. Silently unreportable history is not, so the trade goes the other
        # way.
        fresh = area_window(code_dir, area_prs, from_sha, to_sha, commit_prs, **history)
        if not fresh:
            skipped.append(f"{area}: nothing new since {from_sha[:7]}")
            return

        candidates.append(
            {
                "area": area,
                "rel_dir": rel_dir,
                "from_sha": from_sha,
                "prs": fresh,
                "bootstrapped": bootstrapped,
                "status_text": status_text,
                "progress_text": progress_text,
            }
        )

    for area in eligible:
        consider(area)

    def choose():
        if strategy == "threshold":
            ranked, reason = choose_by_threshold(
                areas, candidates, skipped, dates, roadmap_dir, to_sha,
                now, threshold=threshold, table_path=table_path, held_until=held_until)
        else:
            if not candidates:
                raise NotDue("; ".join(skipped) or "no candidate areas")

            ranked = sorted(
                candidates,
                key=lambda c: (-len(c["prs"]), c["area"]),
            )
            best = ranked[0]
            if len(best["prs"]) < min_prs:
                raise NotDue(
                    f"{cadence_reason}, but the busiest area ({best['area']}) has only "
                    f"{len(best['prs'])} PR(s) in its window (< {min_prs})"
                )
            reason = f"{cadence_reason}; {best['area']} has {len(best['prs'])} PR(s) since {best['from_sha'][:7]}"
        return ranked, reason

    dates = report_dates(roadmap_dir, roadmap_ref) if strategy == "threshold" else None
    ranked, reason = choose()
    best = ranked[0]
    # The cache cannot see a label added to a PR that closed before its last search, and a cursor
    # recorded past that PR's merge would leave it out of every later window. So the chosen area is
    # read in full before its window is planned, and the choice is made again if that changed it.
    refreshed = set()
    while label_cache is not None and best["area"] not in refreshed:
        area = best["area"]
        refreshed.add(area)
        exact = (labels_reader.refresh(label_cache, area) if source
                 else gh.refresh_cached_area(label_cache, area))
        if exact == histories[area]:
            break
        histories[area] = exact
        candidates.remove(best)
        consider(area)
        ranked, reason = choose()
        best = ranked[0]
    area_layers, area_readme_sha = read_area_layers(roadmap_dir, best["rel_dir"])
    sub_roadmaps = read_sub_roadmaps(roadmap_dir, best["area"], best["rel_dir"])

    from .facts import required_modules

    docs.require_coverage(required_modules(code_dir, best["from_sha"], to_sha, best["prs"], **history))

    if source:
        source.content.revalidate()
        source.roadmap.revalidate()
    return {
        **({"source": source.record()} if source else {}),
        "documentation": docs.pin(),
        "history_migration_sha256": window.current_migration().digest,
        "roadmap": best["area"],
        "rel_dir": best["rel_dir"],
        "layers": area_layers,
        "readme_sha": area_readme_sha,
        "sub_roadmaps": sub_roadmaps,
        "from_sha": best["from_sha"],
        "to_sha": to_sha,
        "prs": best["prs"],
        "bootstrapped": best["bootstrapped"],
        "reason": reason,
        "skipped": skipped,
        "status_path": f"{best['rel_dir']}/{STATUS_NAME}",
        "progress_path": f"{best['rel_dir']}/{PROGRESS_NAME}",
        "from_date": window.commit_date(code_dir, best["from_sha"]),
        "to_date": window.commit_date(code_dir, to_sha),
        "runner_up": [
            {"area": c["area"], "prs": len(c["prs"])} for c in ranked[1:4]
        ],
    }


def plan_json(plan):
    return json.dumps(plan, indent=2, sort_keys=True)
