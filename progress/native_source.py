"""Direct, explicitly bound Forgejo observations for Progress planning.

Clients and repository references are supplied by the caller. This module chooses
no credentials or process defaults, and reads no shared state service.
"""

import base64
import re
from dataclasses import dataclass
from urllib.parse import quote, urlencode

from tauceti_tools.ci_candidate import RepositoryIdentity
from tauceti_tools.forge import Forge, ForgeError, Incomplete, RepositoryRef
from tauceti_tools.forge_read import project, pull_record
from tauceti_tools.source_identity import SourceActor


def _number(value, label):
    if type(value) is not int or value < 1:
        raise Incomplete(f"Progress source {label} is unavailable")
    return value


def _sha(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value) is None:
        raise Incomplete("Progress source merge commit is unavailable")
    return value


class NativeRepository:
    """One repository and one origin-bound API, retained for this planning round."""

    def __init__(self, repository, api):
        if not isinstance(repository, RepositoryRef) or repository.forge.kind != "forgejo":
            raise ValueError("Progress native reads require a qualified Forgejo repository")
        if getattr(api, "forge", None) != repository.forge:
            raise ValueError("Progress source client belongs to another origin")
        self.repository, self.api = repository, api
        self.identity = RepositoryIdentity(repository, _number(api.repository(repository).get("id"), "repository ID"))

    def _check_binding(self):
        if getattr(self.api, "forge", None) != self.repository.forge:
            raise Incomplete("Progress source client origin changed")

    @property
    def path(self):
        self._check_binding()
        return "repos/" + self.repository.name

    def revalidate(self):
        self._check_binding()
        row = self.api.repository(self.repository)
        if _number(row.get("id"), "repository ID") != self.identity.native_id:
            raise Incomplete("Progress source repository identity changed during the round")

    def recent_commits(self, limit=30):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("commit prefix must be between 1 and 1000")
        rows = []
        for page in self.api.pages(self.path + "/commits?stat=false&verification=false&files=false"):
            for row in page:
                try:
                    date = row["commit"]["committer"]["date"]
                    message = row["commit"]["message"]
                    if not isinstance(date, str) or not date or not isinstance(message, str):
                        raise ValueError
                except (TypeError, KeyError, ValueError):
                    raise Incomplete("Progress source commit metadata is unavailable") from None
                rows.append((date, message.split("\n", 1)[0]))
                if len(rows) == limit:
                    self.revalidate()
                    return rows
        self.revalidate()
        return rows

    def _area_label_ids(self, labels):
        """Resolve repository/shared label names before asking for their union."""
        catalog = self.api.collection(self.path + "/labels", counted=True)
        wanted = set(labels)
        by_id = {}

        def include(rows):
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("name"), str):
                    raise Incomplete("Progress label catalog is unavailable")
                name = row["name"].casefold()
                native_id = _number(row.get("id"), "label ID")
                if native_id in by_id and by_id[native_id] != name:
                    raise Incomplete("Progress label catalog changed during pagination")
                by_id[native_id] = name

        include(catalog)
        owner = quote(self.repository.name.split("/")[0], safe="")
        try:
            # Shared and repository labels can have the same name but distinct
            # IDs. Both catalogs are needed even if every name has a local match.
            include(self.api.collection("orgs/" + owner + "/labels", counted=True))
        except ForgeError as error:
            if error.status not in {403, 404}:
                raise
            # A hidden/inaccessible org catalog cannot prove a complete ID union.
            # Read the complete authorized repository history instead; never
            # turn this catalog failure into an empty roadmap history.
            return None
        return sorted(native_id for native_id, name in by_id.items() if name in wanted)

    def labelled_histories(self, areas):
        """Labelled candidates in every state; local Git decides which landed.

        A landed queue PR may have been reopened. Reading all states also keeps
        close/reopen events from shifting the membership of a paginated scan.
        Labels narrow the read; closure and editable titles never prove landing.
        """
        histories = {area: {} for area in dict.fromkeys(areas)}
        if not histories:
            return histories
        labels = {("roadmap/" + area).casefold(): area for area in histories}
        if len(labels) != len(histories):
            raise ValueError("roadmap areas have ambiguous case-insensitive labels")
        label_ids = self._area_label_ids(labels)
        if label_ids == []:
            self.revalidate()
            return histories
        seen = {}
        for page in self._history_pages(label_ids):
            for row in page:
                if not isinstance(row, dict):
                    raise Incomplete("Progress labelled history contains a non-object")
                number = _number(row.get("number"), "PR number")
                if row.get("state") not in {"open", "closed"} or type(row.get("merged")) is not bool:
                    raise Incomplete("Progress labelled history has an unavailable closure or merge state")
                if "labels" not in row:
                    raise Incomplete("Progress labelled history labels are unavailable")
                raw_labels = row["labels"]
                if raw_labels is None:
                    raw_labels = []
                if not isinstance(raw_labels, list) or any(
                    not isinstance(label, dict) or not isinstance(label.get("name"), str) for label in raw_labels
                ):
                    raise Incomplete("Progress labelled history labels are unavailable")
                sha = _sha(row.get("merge_commit_sha")) if row["merged"] else None
                signature = (sha, frozenset(label["name"].casefold() for label in raw_labels))
                if number in seen and seen[number] != signature:
                    raise Incomplete("Progress labelled history changed during pagination")
                seen[number] = signature
                for label in signature[1]:
                    if label in labels:
                        histories[labels[label]][number] = sha
        self.revalidate()
        return histories

    def _history_pages(self, label_ids):
        # Forgejo 17's repeated label-ID filter is OR, which is exactly the
        # requested union. Keep URLs bounded and split large unions, not one
        # full repository history read per roadmap. Client-side grouping still
        # checks the returned names and deduplicates PRs shared across batches.
        if label_ids is None:
            yield from self.api.pages(self.path + "/pulls?state=all", counted=True)
            return
        for offset in range(0, len(label_ids), 50):
            query = urlencode([("state", "all"), *[("labels", str(n)) for n in label_ids[offset : offset + 50]]])
            yield from self.api.pages(self.path + "/pulls?" + query, counted=True)

    def pr_details(self, numbers):
        self._check_binding()
        result = []
        for number in numbers:
            if isinstance(number, str):
                raise ValueError(f"{number} is commit-attributed: use commit_attribution.commentary")
            _number(number, "PR number")
            row = self.api.pull(self.repository, number)
            if "body" not in row:
                raise Incomplete("Progress PR commentary is unavailable")
            if any(not isinstance(row.get(key), str) for key in ("title", "body")):
                # Forgejo represents an absent body as null, which is an empty comment.
                if not isinstance(row.get("title"), str) or row.get("body") is not None:
                    raise Incomplete("Progress PR commentary is unavailable")
            if type(row.get("merged")) is not bool:
                raise Incomplete("Progress PR merge state is unavailable")
            result.append(
                {
                    "number": number,
                    "title": row["title"],
                    "body": row.get("body") or "",
                    "merged_at": row.get("merged_at") if row["merged"] else None,
                    "url": self.repository.pull_url(number),
                    "repository": self.identity.record(),
                }
            )
        self.revalidate()
        return result

    def open_reports(self):
        self._check_binding()
        fields = ["number", "headRefName", "title", "url", "createdAt", "headRepositoryOwner", "body", "headRepository"]
        result = []
        for raw in self.api.pulls(self.repository, state="open", limit=None):
            row = project(pull_record(raw, self.repository, fields), fields)
            if not row["headRefName"].startswith("progress/"):
                continue
            head = raw["head"]["repo"]
            row["source_head_repository"] = (
                None
                if head is None
                else RepositoryIdentity(
                    RepositoryRef(self.repository.forge, head.get("full_name")),
                    _number(head.get("id"), "head repository ID"),
                ).record()
            )
            result.append(row)
        self.revalidate()
        return result

    def report_owner_filter(self):
        self._check_binding()
        actor = SourceActor.from_native(self.repository.forge, self.api.current_user())
        actor_id = f"{actor.principal.forge.key}/users/{actor.principal.native_id}"
        self.revalidate()

        def owns(row):
            if "source_head_repository" not in row:
                raise Incomplete("Progress open report lacks a qualified head repository")
            head = row["source_head_repository"]
            if head is None:
                return False
            identity = RepositoryIdentity.parse(head)
            if identity.repository.forge != self.repository.forge:
                raise Incomplete("Progress report head belongs to another origin")
            if identity.native_id == self.identity.native_id and identity.repository.key == self.repository.key:
                return True
            owner = row.get("headRepositoryOwner")
            return isinstance(owner, dict) and owner.get("id") == actor_id

        return owns

    def file_at(self, path, ref="main"):
        if not isinstance(path, str) or not path or path.startswith("/") or ".." in path.split("/") or "\\" in path:
            raise ValueError("source file path must stay inside the repository")
        if not isinstance(ref, str) or not ref or any(c in ref for c in "\0\r\n"):
            raise ValueError("source file ref is invalid")
        commit = self.api.request("GET", self.path + "/git/commits/" + quote(ref, safe="")
                                  + "?stat=false&verification=false&files=false")
        if not isinstance(commit, dict):
            raise Incomplete("Progress cursor ref is unavailable")
        revision = _sha(commit.get("sha"))
        try:
            row = self.api.request(
                "GET", self.path + "/contents/" + quote(path, safe="/") + "?" + urlencode({"ref": revision})
            )
        except ForgeError as error:
            if error.status == 404:
                # A forbidden or deleted repository must not become an absent cursor.
                self.revalidate()
                return None
            raise
        if not isinstance(row, dict) or row.get("type") != "file" or row.get("encoding") != "base64":
            raise Incomplete("Progress source file content is unavailable")
        try:
            text = base64.b64decode("".join(row["content"].split()), validate=True).decode("utf-8")
        except (KeyError, ValueError, UnicodeError, AttributeError):
            raise Incomplete("Progress source file encoding is invalid") from None
        self.revalidate()
        return text


class LabelReader:
    """`label_cache`'s reader for a native repository, whose histories hold labelled pull requests
    in every state (see `NativeRepository.labelled_histories`).

    Forgejo lists pull requests by creation time, which an import can set out of number order, and
    cannot search by close or update time, so no listing prefix proves that nothing older changed.
    Each run therefore reads the complete labelled listing of the areas it plans, as an uncached
    plan does; the file keeps the cache's binding and format.
    """

    def __init__(self, repository):
        self.repository = repository
        self.binding = f"{repository.repository.key}#{repository.identity.native_id}"

    def full(self, areas):
        return self.repository.labelled_histories(areas) if areas else {}

    def start(self, areas):
        return None

    def read(self, path, areas, now=None):
        from . import label_cache

        return label_cache.read(path, areas, self, self.binding, now=now)

    def refresh(self, path, area):
        from . import label_cache

        return label_cache.refresh(path, area, self, self.binding)


@dataclass(frozen=True)
class NativeSource:
    content: NativeRepository
    roadmap: NativeRepository
    legacy_content: RepositoryRef | None = None
    legacy_through: str | None = None

    def __post_init__(self):
        if not isinstance(self.content, NativeRepository) or not isinstance(self.roadmap, NativeRepository):
            raise ValueError("Progress source needs explicit content and roadmap readers")
        if self.legacy_content is not None and not isinstance(self.legacy_content, RepositoryRef):
            raise ValueError("Progress legacy history namespace must be qualified")
        _validate_boundary(self.legacy_content, self.legacy_through)

    @property
    def history_options(self):
        return {"repository": self.content.repository, "repository_id": self.content.identity.native_id,
                "legacy_repository": self.legacy_content, "legacy_through": self.legacy_through}

    def record(self):
        legacy = self.legacy_content
        return {
            "schema": "tauceti-progress.source/v1",
            "content": self.content.identity.record(),
            "roadmap": self.roadmap.identity.record(),
            "legacy_through": self.legacy_through,
            "legacy_content": None
            if legacy is None
            else {"forge": {"kind": legacy.forge.kind, "url": legacy.forge.url}, "name": legacy.name},
        }


def _validate_boundary(legacy, boundary):
    if (legacy is None) != (boundary is None):
        raise ValueError("legacy namespace and immutable cutover commit must be supplied together")
    if boundary is not None and (
        not isinstance(boundary, str) or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", boundary) is None
    ):
        raise ValueError("legacy cutover must be a full immutable commit SHA")


def history_options_from_record(value):
    """Parse retained repository identities without consulting environment defaults."""
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "content", "roadmap", "legacy_content", "legacy_through"}
        or value["schema"] != "tauceti-progress.source/v1"
    ):
        raise ValueError("invalid saved Progress source binding")
    content, roadmap = RepositoryIdentity.parse(value["content"]), RepositoryIdentity.parse(value["roadmap"])
    if any(identity.repository.forge.kind != "forgejo" for identity in (content, roadmap)):
        raise ValueError("native Progress source binding requires Forgejo repositories")
    legacy = value["legacy_content"]
    if legacy is not None:
        if not isinstance(legacy, dict) or set(legacy) != {"forge", "name"}:
            raise ValueError("invalid saved legacy source namespace")
        if not isinstance(legacy["forge"], dict) or set(legacy["forge"]) != {"kind", "url"}:
            raise ValueError("invalid saved legacy source origin")
        if not all(isinstance(value, str) for value in legacy["forge"].values()):
            raise ValueError("invalid saved legacy source origin")
        legacy = RepositoryRef(Forge(**legacy["forge"]), legacy["name"])
    _validate_boundary(legacy, value["legacy_through"])
    return {"repository": content.repository, "repository_id": content.native_id, "legacy_repository": legacy,
            "legacy_through": value["legacy_through"]}
