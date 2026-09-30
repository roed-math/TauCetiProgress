"""The GitHub reads this tool needs, via the `gh` CLI.

Two design points worth stating, because both were defects in an earlier draft:

* **No search cap.** Area labels are read from the paginated repository pull-request connection,
  not GitHub search, whose 1000-result cap can silently discard the earliest merges needed for
  bootstrap. Native merge SHAs come back in the same query, avoiding per-PR bootstrap requests.

* **Failures raise.** A GitHub hiccup must never read as "nothing to report" -- that would let a
  transient error advance a cursor past real work. Every caller turns a raise into "cannot decide
  right now".
"""

import base64
import datetime
import json
import os
import pathlib
import subprocess
import time

ROADMAP_REPO = "TauCetiProject/TauCetiRoadmap"
CODE_REPO = "TauCetiProject/TauCeti"

ROADMAP_LABEL_PREFIX = "roadmap/"
# Labels that exist but name no roadmap: infra/refactor/bump work, and new mathematics whose
# citation could not be parsed. Neither is reported, by design.
NON_AREA_LABELS = {"roadmap/none", "roadmap/Unknown"}


class GhError(RuntimeError):
    """A `gh` invocation failed."""


def gh(args, retries=3):
    """Run `gh` and return stdout, retrying transient failures.

    Every call here is a read, so a retry is always safe. This mirrors the retry helper in
    `TauCeti/scripts/roadmap_label.py`.
    """
    last = ""
    for attempt in range(retries):
        proc = subprocess.run(["gh", *args], capture_output=True, text=True)
        if proc.returncode == 0:
            return proc.stdout
        last = proc.stderr.strip()
        if attempt + 1 < retries:
            time.sleep(2 ** attempt)
    raise GhError(f"gh {' '.join(args)} failed after {retries} attempts: {last}")


def _api(path, jq=None):
    args = ["api", path]
    if jq:
        args += ["--jq", jq]
    return gh(args)


def recent_roadmap_commits(limit=30, repo=ROADMAP_REPO):
    """`[(iso_date, subject)]` for the newest commits on the roadmap repo's default branch.

    One request, no clone. This is the whole of the `due` check: progress updates are the only
    commits whose subject starts with the reserved prefix, so the newest such commit's date is the
    last time any area was updated.
    """
    out = _api(
        f"repos/{repo}/commits?per_page={int(limit)}",
        '.[] | [.commit.committer.date, (.commit.message | split("\\n")[0])] | @tsv',
    )
    rows = []
    for line in out.splitlines():
        date, _, subject = line.partition("\t")
        if date:
            rows.append((date.strip(), subject.strip()))
    return rows


def closed_prs_for_area(area, repo=CODE_REPO):
    """`{number: native_merge_sha_or_None}` for all closed labelled PR candidates.

    GitHub-native merges and bors squash merges both close their pull requests, but bors
    fast-forwards the code branch and closes the PR without setting GitHub's merged state.
    Query closed PRs (which includes native merges), then let the first-parent commit history
    decide which actually landed. An editable title or a closure alone is not merge evidence.

    Attribution is per area rather than per PR. Pagination has no project-age cap: the oldest
    entries are exactly what bootstrap needs. `gh pr list --label` uses search and silently stops
    at 1000 results even with a larger --limit, so query the repository connection directly.
    Only native mergedAt makes mergeCommit authoritative; an unmerged PR can have a test merge SHA.
    """
    owner, name = repo.split("/", 1)
    query = """query($owner: String!, $name: String!, $label: [String!], $endCursor: String) {
      repository(owner: $owner, name: $name) {
        pullRequests(first: 100, after: $endCursor, labels: $label, states: [CLOSED, MERGED],
                     orderBy: {field: CREATED_AT, direction: DESC}) {
          nodes { number mergedAt mergeCommit { oid } }
          pageInfo { hasNextPage endCursor }
        }
      }
    }"""
    out = gh([
        "api", "graphql", "--paginate", "--slurp", "-f", f"query={query}",
        "-f", f"owner={owner}", "-f", f"name={name}",
        "-f", f"label[]={ROADMAP_LABEL_PREFIX}{area}",
    ])
    try:
        pages = json.loads(out)
        if not isinstance(pages, list) or not pages:
            raise GhError("closed PR query returned no response pages")
        if pages[-1]["data"]["repository"]["pullRequests"]["pageInfo"]["hasNextPage"]:
            raise GhError("closed PR query stopped before its final page")
        found = {}
        for page in pages:
            for row in page["data"]["repository"]["pullRequests"]["nodes"]:
                sha = (row.get("mergeCommit") or {}).get("oid") if row["mergedAt"] else None
                if row["mergedAt"] and not sha:
                    raise GhError(f"native merge SHA unavailable for PR #{row['number']}")
                found[int(row["number"])] = sha
        return found
    except (ValueError, KeyError, TypeError) as exc:
        raise GhError(f"could not parse closed PR query output: {exc}") from exc



def merged_prs_since(since, repo=CODE_REPO, limit=1000):
    """`{number: [label, ...]}` for PRs merged at or after `since` (a UTC datetime). One search, which
    GitHub caps at `limit` results; the caller must treat a full page as "possibly more"."""
    stamp = since.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = gh([
        "pr", "list", "--repo", repo, "--state", "merged", "--search", f"merged:>={stamp}",
        "--limit", str(int(limit)), "--json", "number,labels",
    ])
    try:
        rows = json.loads(out)
    except json.JSONDecodeError as exc:
        raise GhError(f"could not parse gh pr list output: {exc}") from exc
    return {int(r["number"]): [lb.get("name") or "" for lb in r.get("labels") or []] for r in rows}


class LabelCache:
    """`merged_prs_for_area`, answered from a file kept between runs.

    A planner run asks for every area's merged PRs, one paginated query per area: about a hundred
    requests for fifty-odd roadmaps. That is fine every eight hours and wasteful for a caller that
    polls for work every few minutes. So the answers are kept, and each later run makes ONE search for
    the PRs merged since the last one and files them under their labels.

    Two things the incremental search cannot see: a label removed from a PR after it merged, and a
    label added to one that merged before the last search. Both are rare, and neither is permanent:
    the whole cache is refetched once `FULL_EVERY_HOURS` have passed, and whenever the search comes
    back full (more merged since the last run than one search returns).

    Only merged PRs are cached, and a merge is final, so a stale entry can only be a relabelling. A
    cache file this version does not understand is discarded and refetched, never trusted.
    """

    VERSION = 1
    FULL_EVERY_HOURS = 24.0
    # The search is by merge time at one-second precision; the margin absorbs clock skew between this
    # host and GitHub and a merge that was being recorded while the last search ran.
    MARGIN = datetime.timedelta(hours=1)
    SEARCH_LIMIT = 1000

    def __init__(self, path, repo=CODE_REPO, now=None, since_fn=None, area_fn=None):
        self.path = pathlib.Path(path)
        self.repo = repo
        self.now = now or datetime.datetime.now(datetime.timezone.utc)
        self._since_fn = since_fn or merged_prs_since
        self._area_fn = area_fn or merged_prs_for_area
        self._synced = False
        self.data = self._load()

    def _fresh(self):
        return {"version": self.VERSION, "repo": self.repo, "full_at": self.now.isoformat(timespec="seconds"),
                "synced_at": self.now.isoformat(timespec="seconds"), "areas": {}}

    def _load(self):
        try:
            obj = json.loads(self.path.read_text(encoding="utf-8"))
            full_at = datetime.datetime.fromisoformat(obj["full_at"])
            datetime.datetime.fromisoformat(obj["synced_at"])
            ok = (obj.get("version") == self.VERSION and obj.get("repo") == self.repo
                  and isinstance(obj.get("areas"), dict)
                  and 0 <= (self.now - full_at).total_seconds() < self.FULL_EVERY_HOURS * 3600)
        except (OSError, ValueError, KeyError, TypeError):
            ok = False
        if not ok:
            self._synced = True  # every area will be fetched in full this run; there is nothing to catch up
            return self._fresh()
        return obj

    def _sync(self):
        """Fold the PRs merged since the last run into the cached areas, once per run."""
        if self._synced:
            return
        self._synced = True
        since = datetime.datetime.fromisoformat(self.data["synced_at"]) - self.MARGIN
        started = self.now
        merged = self._since_fn(since, repo=self.repo, limit=self.SEARCH_LIMIT)
        if len(merged) >= self.SEARCH_LIMIT:
            # More than one search holds: start over, fetching every area in full as it is asked for.
            self.data = self._fresh()
            return
        areas = self.data["areas"]
        for number, labels in merged.items():
            for label in labels:
                if not label.startswith(ROADMAP_LABEL_PREFIX) or label in NON_AREA_LABELS:
                    continue
                area = label[len(ROADMAP_LABEL_PREFIX):]
                if area in areas and number not in areas[area]:
                    areas[area] = sorted(set(areas[area]) | {number}, reverse=True)
        self.data["synced_at"] = started.isoformat(timespec="seconds")

    def for_area(self, area):
        """Every merged PR number labelled `roadmap/<area>`, newest first (as `merged_prs_for_area`)."""
        self._sync()
        areas = self.data["areas"]
        if area not in areas:
            # First sight of this area (or a fresh cache): fetch it in full. That fetch is current, so
            # it needs nothing from the search, and later searches keep it up to date.
            areas[area] = list(self._area_fn(area, repo=self.repo))
        return list(areas[area])

    def save(self):
        """Write the cache back. Best effort: a cache that cannot be written costs the next run a full
        fetch, not a wrong answer."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass

def pr_details(numbers, repo=CODE_REPO):
    """`[{number, title, body, merged_at, url}]` for an explicit set of PR numbers.

    Bodies are commentary for the writing model, not ground truth: they are self-reported, and a
    body can claim more than its diff delivers. `facts` supplies the ground truth instead.
    """
    out = []
    for n in numbers:
        raw = _api(f"repos/{repo}/pulls/{int(n)}")
        obj = json.loads(raw)
        out.append(
            {
                "number": int(n),
                "title": obj.get("title") or "",
                "body": obj.get("body") or "",
                "merged_at": obj.get("merged_at"),
                "url": obj.get("html_url") or f"https://github.com/{repo}/pull/{n}",
            }
        )
    return out


def open_progress_prs(repo=ROADMAP_REPO, branch_prefix="progress/"):
    """Open PRs on the roadmap repo whose head branch is a progress branch.

    An open progress PR *is* the in-flight marker for its area. Until it merges, the area's cursor
    in `main` still points at the old window, so recomputing would produce the same window again;
    treating the PR as in-flight is what stops a duplicate being opened every day.

    `headRepositoryOwner` comes back so callers can tell whose work a pull request is. Anyone may
    open one on a `progress/*` branch, and branch names are a pure function of the window, so
    treating every one of them as an in-flight marker would let a stranger freeze a roadmap by
    opening one pull request a day.

    `createdAt` comes back too, because "in flight" has to expire. A pull request the merge check
    refuses permanently never merges and never closes itself, and without an age it would mark its
    area in flight forever, silently stopping that roadmap's reporting for every operator.
    """
    out = gh([
        "pr", "list", "--repo", repo, "--state", "open",
        "--limit", "200", "--json",
        "number,headRefName,title,url,createdAt,headRepositoryOwner,body",
    ])
    rows = json.loads(out)
    return [r for r in rows if (r.get("headRefName") or "").startswith(branch_prefix)]


def file_on_default_branch(path, repo=ROADMAP_REPO, ref="main"):
    """The text of `path` on `ref`, or None if it is not there.

    Read through the API rather than from a clone on purpose. A worker's checkout is a snapshot from
    whenever it started, and the questions this answers -- where is the cursor NOW, is this report
    still the live one -- are exactly the ones a stale snapshot gets wrong.
    """
    try:
        raw = gh(["api", f"repos/{repo}/contents/{path}?ref={ref}", "--jq", ".content"])
    except GhError:
        return None
    try:
        return base64.b64decode("".join(raw.split())).decode("utf-8", "surrogateescape")
    except (ValueError, UnicodeDecodeError):
        return None
