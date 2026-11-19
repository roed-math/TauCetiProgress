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
import fcntl
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

ROADMAP_REPO = os.environ.get("TAUCETI_ROADMAP_REPO", "TauCetiProject/TauCetiRoadmap")
CODE_REPO = os.environ.get("TAUCETI_REPO", "TauCetiProject/TauCeti")

ROADMAP_LABEL_PREFIX = "roadmap/"
# Labels that exist but name no roadmap: infra/refactor/bump work, and new mathematics whose
# citation could not be parsed. Neither is reported, by design.
NON_AREA_LABELS = {"roadmap/none", "roadmap/Unknown"}


class GhError(RuntimeError):
    """A `gh` invocation failed."""


class PublicationRejected(GhError):
    """The proxy verified a definitive rejection; preserve artifacts for repair."""


def readonly(args):
    """Conservatively identify the read forms used by Progress.

    Unknown forms retain mutation journaling. This is retry policy, not a
    GraphQL parser or an authorization boundary. In particular a query prefix
    cannot prove that a document contains no selected mutation operation.
    """
    if args[:2] in (["pr", "list"], ["pr", "view"], ["repo", "view"]):
        return True
    if len(args) < 2 or args[0] != "api" or args[1].startswith("-"):
        return False
    graphql = args[1] == "graphql"
    queries, index = [], 2
    while index < len(args):
        arg = args[index]
        if arg in {"--paginate", "--slurp"} or arg.startswith("--jq="):
            index += 1
            continue
        if arg in {"--jq", "-q"}:
            index += 2
            if index > len(args):
                return False
            continue  # Payload text is never interpreted as another option.
        flag, sep, value = arg.partition("=")
        if flag not in {"-f", "-F", "--field", "--raw-field"}:
            if arg.startswith(("-f", "-F")) and len(arg) > 2:
                flag, sep, value = arg[:2], "=", arg[2:]
            else:
                return False  # Includes short boolean clusters hiding a field/method.
        if not graphql:
            return False  # Fields change gh api's default method to POST.
        if not sep:
            index += 1
            if index >= len(args):
                return False
            value = args[index]
        key, equals, field = value.partition("=")
        if not equals or key == "extensions" or key.startswith(("query[", "extensions[")):
            return False
        if key == "query":
            if flag in {"-F", "--field"}:
                return False  # Typed fields can read query text from a file.
            queries.append(field)
        index += 1
    if not graphql:
        return True
    if len(queries) != 1:
        return False
    query = queries[0].lstrip()
    return query.startswith(("query", "{")) and "mutation" not in query and "subscription" not in query


def _save_intent(path, value):
    temp = path.with_suffix(".tmp")
    with open(temp, "w", opener=lambda p, f: os.open(p, f, 0o600)) as saved:
        json.dump(value, saved)
        saved.flush()
        os.fsync(saved.fileno())
    temp.replace(path)


def gh(args, retries=3, reconcile=None, context=None):
    """Retry reads. Journal writes before sending; reconcile uncertain writes before replay.

    A reconciliation callback returns stdout for a verified success, False for
    authoritative absence, or None for uncertainty. Absence allows one retry
    after five minutes. Retained bodies and argv are kept privately.
    """
    if not readonly(args) and os.environ.get("TAUCETI_REAL_GH"):
        # The Worker proxy owns durable intents and quota-aware reconciliation.
        # A second journal must not count its local wait responses as writes.
        if context and context.get("actor"):
            current = gh(["api", "user", "--jq", ".login"]).strip().lower()
            if current != context["actor"]:
                raise GhError("fork actor changed before publication")
        proc = subprocess.run(["gh", *args], capture_output=True, text=True)
        if proc.returncode == 0 and args[:2] != ["repo", "fork"]:
            return proc.stdout
        outcome = reconcile() if reconcile else None
        if isinstance(outcome, str):
            return outcome
        if proc.returncode == 78:
            raise PublicationRejected(f"publication definitively rejected: {proc.stderr.strip()}")
        raise GhError(f"Worker retained publication for reconciliation: {proc.stderr.strip()}")
    if not readonly(args):
        actor = gh(["api", "user", "--jq", ".login"]).strip().lower()
        if not actor:
            raise GhError("cannot identify mutation actor")
        if context and context.get("actor") not in (None, actor):
            raise GhError("fork actor changed before publication")
        operation = args
        if args[:2] in (["pr", "create"], ["pr", "close"]):
            operation = args[:2] + ([args[2]] if args[1] == "close" else [])
            for flag in ("--repo", "--head", "--base"):
                if flag in args:
                    operation += [flag, args[args.index(flag) + 1]]
        key = hashlib.sha256(json.dumps([os.environ.get("GH_HOST", "github.com"), actor, operation]).encode()).hexdigest()
        root = Path(os.environ.get("TAUCETI_PROGRESS_OUTBOX", Path.home() / ".local/state/tauceti-progress/outbox"))
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = root / (key + ".json")
        with open(root / (key + ".lock"), "a", opener=lambda p, f: os.open(p, f, 0o600)) as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if path.exists():
                value = json.loads(path.read_text())
                if value.get("actor") != actor or value.get("context") != context:
                    raise GhError("retained publication identity changed; preserving original intent")
                outcome = reconcile() if reconcile else None
                if isinstance(outcome, str):
                    path.unlink()
                    return outcome
                if outcome is False and "absent_since" not in value:
                    value["absent_since"] = time.time()
                    _save_intent(path, value)
                known_unsent = value.get("not_sent") and value["attempts"] == 0
                if (not known_unsent and (outcome is not False or time.time() - value["absent_since"] < 300
                        or value["attempts"] >= 2) or value["argv"] != args):
                    raise GhError("publication retained; uncertain outcome must reconcile before replay")
            else:
                value = {"actor": actor, "argv": args, "created": time.time(), "attempts": 0, "context": context}
            value.pop("not_sent", None)
            value["attempts"] += 1
            _save_intent(path, value)
            try:
                proc = subprocess.run(["gh", *args], capture_output=True, text=True)
            except OSError:
                value["attempts"] -= 1
                value["not_sent"] = True
                _save_intent(path, value)
                raise
            if proc.returncode == 77:
                value["attempts"] -= 1
                _save_intent(path, value)
            if proc.returncode == 0 and args[:2] == ["api", "graphql"]:
                try:
                    if json.loads(proc.stdout).get("errors"):
                        proc.returncode = 1
                except (ValueError, AttributeError):
                    proc.returncode = 1
            if proc.returncode == 0 and args[:2] != ["repo", "fork"]:
                path.unlink()
                return proc.stdout
            outcome = reconcile() if reconcile else None
            if isinstance(outcome, str):
                path.unlink()
                return outcome
            raise GhError(f"gh publication failed; retained for reconciliation: {proc.stderr.strip()}")
    last = ""
    for attempt in range(retries):
        proc = subprocess.run(["gh", *args], capture_output=True, text=True)
        if proc.returncode == 0:
            # GraphQL can return partial data and errors with HTTP 200.
            if args[:2] == ["api", "graphql"]:
                try:
                    payload = json.loads(proc.stdout)
                    pages = payload if isinstance(payload, list) else [payload]
                    if any(isinstance(page, dict) and page.get("errors") for page in pages):
                        raise GhError("partial GraphQL read response")
                except ValueError as exc:
                    raise GhError("invalid GraphQL read response") from exc
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


def closed_prs_for_areas(areas, repo=CODE_REPO):
    """Read the same complete labelled histories in small GraphQL batches.

    Each connection retains its own cursor. A missing/failed page raises, never
    becomes an empty history that could move a report cursor past real work.
    """
    areas = list(dict.fromkeys(areas))
    if len(areas) == 1:
        return {areas[0]: closed_prs_for_area(areas[0], repo=repo)}
    owner, name = repo.split("/", 1)
    histories = {area: {} for area in areas}
    for offset in range(0, len(areas), 10):
        batch = dict(enumerate(areas[offset:offset + 10]))
        cursors, seen = {}, {i: set() for i in batch}
        while batch:
            declarations = ["$owner:String!", "$name:String!"]
            selections = []
            args = ["api", "graphql", "-f", f"owner={owner}", "-f", f"name={name}"]
            for i, area in batch.items():
                declarations += [f"$label{i}:[String!]", f"$cursor{i}:String"]
                selections.append(
                    f"q{i}:pullRequests(first:100,after:$cursor{i},labels:$label{i},"
                    "states:[CLOSED,MERGED],orderBy:{field:CREATED_AT,direction:DESC})"
                    "{nodes{number mergedAt mergeCommit{oid}} pageInfo{hasNextPage endCursor}}"
                )
                args += ["-f", f"label{i}[]={ROADMAP_LABEL_PREFIX}{area}"]
                if i in cursors:
                    args += ["-f", f"cursor{i}={cursors[i]}"]
            query = "query(" + ",".join(declarations) + "){repository(owner:$owner,name:$name){" + " ".join(selections) + "}}"
            try:
                payload = json.loads(gh([*args, "-f", "query=" + query]))
                if payload.get("errors"):
                    raise ValueError("GraphQL returned errors")
                repository = payload["data"]["repository"]
                pending = {}
                for i, area in batch.items():
                    connection = repository[f"q{i}"]
                    for row in connection["nodes"]:
                        sha = (row.get("mergeCommit") or {}).get("oid") if row["mergedAt"] else None
                        if row["mergedAt"] and not sha:
                            raise ValueError("native merge SHA unavailable")
                        histories[area][int(row["number"])] = sha
                    page = connection["pageInfo"]
                    if page["hasNextPage"]:
                        cursor = page["endCursor"]
                        if not isinstance(cursor, str) or not cursor or cursor in seen[i]:
                            raise ValueError("invalid or repeated history cursor")
                        seen[i].add(cursor)
                        cursors[i], pending[i] = cursor, area
                batch = pending
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                raise GhError(f"could not read complete closed PR histories: {exc}") from exc
    return histories


def closed_prs_since(since, repo=CODE_REPO, limit=1000):
    """`{number: (label_names, native_merge_sha_or_None)}` for PRs closed at or after `since`.

    One search, which GitHub caps at 1000 results: a caller must read a full result as "possibly
    more". The SHA means what it does in `closed_prs_for_area`.
    """
    stamp = since.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = gh([
        "pr", "list", "--repo", repo, "--state", "closed", "--search", f"closed:>={stamp}",
        "--limit", str(int(limit)), "--json", "number,labels,mergedAt,mergeCommit",
    ])
    try:
        found = {}
        for row in json.loads(out):
            sha = (row.get("mergeCommit") or {}).get("oid") if row["mergedAt"] else None
            if row["mergedAt"] and not sha:
                raise GhError(f"native merge SHA unavailable for PR #{row['number']}")
            found[int(row["number"])] = ([label["name"] for label in row["labels"]], sha)
        return found
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise GhError(f"could not parse closed PR search output: {exc}") from exc


# The search is by close time; the margin absorbs clock skew between this host and GitHub and a
# closure that was being recorded while the previous search ran.
LABEL_CACHE_MARGIN = datetime.timedelta(hours=1)
LABEL_CACHE_SEARCH_LIMIT = 1000


class _LabelReader:
    """`label_cache`'s reader for a GitHub repository: the cursor is the time of the last search."""

    def __init__(self, repo, now):
        self.repo, self.now = repo, now

    def full(self, areas):
        return closed_prs_for_areas(areas, repo=self.repo) if areas else {}

    def start(self, areas):
        return self.now.isoformat(timespec="seconds")

    def changes(self, cursor, areas):
        since = datetime.datetime.fromisoformat(cursor) - LABEL_CACHE_MARGIN
        closed = closed_prs_since(since, repo=self.repo, limit=LABEL_CACHE_SEARCH_LIMIT)
        if len(closed) >= LABEL_CACHE_SEARCH_LIMIT:
            raise ValueError("more closed than one search returns")
        prefix = ROADMAP_LABEL_PREFIX
        return {number: ({label[len(prefix):] for label in labels if label.startswith(prefix)}, sha)
                for number, (labels, sha) in closed.items()}


def cached_closed_prs_for_areas(path, areas, repo=CODE_REPO, now=None):
    """`closed_prs_for_areas`, answered from the label cache at `path` (see `label_cache`).

    A run after the first makes one search for the PRs closed since the previous run and files them
    under their labels; only areas the file has not seen are read in full. The search cannot see a
    label changed on a PR that closed before the previous run, and comes back full when more than
    `LABEL_CACHE_SEARCH_LIMIT` closed, so the file is read in full daily and after a full search.
    """
    from . import label_cache

    now = now or datetime.datetime.now(datetime.timezone.utc)
    return label_cache.read(path, areas, _LabelReader(repo, now), f"github:{repo}", now=now)


def refresh_cached_area(path, area, repo=CODE_REPO):
    """`closed_prs_for_area`, read in full and filed in the label cache at `path`."""
    from . import label_cache

    return label_cache.refresh(path, area, _LabelReader(repo, None), f"github:{repo}")


def pr_details(numbers, repo=CODE_REPO):
    """`[{number, title, body, merged_at, url}]` for an explicit set of PR numbers.

    Bodies are commentary for the writing model, not ground truth: they are self-reported, and a
    body can claim more than its diff delivers. `facts` supplies the ground truth instead.
    """
    out = []
    for n in numbers:
        if isinstance(n, str) and not n.isdecimal():
            raise ValueError(f"{n} is commit-attributed: use commit_attribution.commentary")
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
