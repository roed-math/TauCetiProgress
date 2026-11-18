"""PR attribution from mainline commits, with an explicit namespace for history.

These are source citations, not review or merge authority. Imported PR numbers
only become current-source numbers through a supplied migration map.
"""

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit

from tauceti_tools.ci_candidate import read_json
from tauceti_tools.forge import GITHUB, Forge, RepositoryRef

_TRAILER_RE = re.compile(r"^(?P<token>[A-Za-z][A-Za-z0-9-]*):[ \t]+(?P<value>\S.*?)\s*$")
MAX_MAP_BYTES = 16 * 1024 * 1024
COMMIT_SOURCES_ENV = "TAUCETI_PROGRESS_COMMIT_SOURCES"
# The Forgejo queue has landed Tau Ceti pull requests beside Bors since 2026-11-15.
DEFAULT_COMMIT_SOURCES = "forgejo:https://git.taucetiproject.org/TauCetiProject/TauCeti@1"


class ProvenanceError(ValueError):
    """Commit attribution is ambiguous; no report cursor may advance."""


@dataclass(frozen=True, eq=False)
class PullSource:
    repository: RepositoryRef
    number: int
    native_id: int | None = None

    def __post_init__(self):
        if (
            not isinstance(self.repository, RepositoryRef)
            or type(self.number) is not int
            or not 0 < self.number < 2**63
            or not (self.native_id is None or (type(self.native_id) is int and 0 < self.native_id < 2**63))
        ):
            raise ProvenanceError("source citation needs a qualified repository and positive PR number")

    @property
    def namespace(self):
        """The repository this number belongs to: its URL, and its native ID when cited."""
        return self.repository.key + ("" if self.native_id is None else f"@{self.native_id}")

    @property
    def key(self):
        return f"{self.namespace}#{self.number}"

    def __hash__(self):
        return hash(self.key)

    def __eq__(self, other):
        return self.key == other.key if isinstance(other, PullSource) else NotImplemented

    @classmethod
    def parse(cls, value):
        try:
            if not isinstance(value, str):
                raise TypeError
            repository, separator, number = value.rpartition("#")
            if not separator or re.fullmatch(r"[1-9][0-9]*", number) is None:
                raise ValueError
            native_id = None
            if repository.startswith(("github:", "forgejo:")):
                kind, _, location = repository.partition(":")
                parsed = urlsplit(location)
                if "?" in location or "#" in location or parsed.path.count("/") != 2:
                    raise ValueError
                # A queue names its repository by native ID as well (`.../TauCeti@1`), so a
                # deleted and recreated repository of the same name is a different source.
                name, at, native = parsed.path.removeprefix("/").partition("@")
                if at:
                    if re.fullmatch(r"[1-9][0-9]*", native) is None:
                        raise ValueError
                    native_id = int(native)
                forge = Forge(kind, parsed.scheme + "://" + parsed.netloc)
                reference = RepositoryRef(forge, name)
            else:
                reference = RepositoryRef(GITHUB, repository)
            return cls(reference, int(number), native_id)
        except (ValueError, TypeError):
            raise ProvenanceError("invalid Source-PR citation") from None


def commit_sources(value=None):
    """`{(repository key, native ID)}` whose merged commits are attributed to roadmaps directly.

    Pull requests from these repositories carry no `roadmap/<Area>` label, so their citations are
    kept as qualified keys rather than refused as foreign; `commit_attribution` classifies the
    merged commits instead. Entries are whitespace-separated qualified repositories with the native
    ID the queue writes (`forgejo:https://forge.example/team/Math@7`); an empty value disables this.
    """
    raw = os.environ.get(COMMIT_SOURCES_ENV, DEFAULT_COMMIT_SOURCES) if value is None else value
    out = set()
    for item in raw.split():
        try:
            source = PullSource.parse(f"{item}#1")
        except ProvenanceError:
            source = None
        if source is None or not item.startswith(("github:", "forgejo:")) or source.native_id is None:
            raise ProvenanceError(f"invalid {COMMIT_SOURCES_ENV} entry {item!r}: a qualified repository "
                                  f"with its native ID is required")
        out.add((source.repository.key, source.native_id))
    return frozenset(out)


def subject_number(subject):
    """Historical GitHub squash or merge subject; its namespace is supplied separately."""
    match = re.search(r"\(#(\d+)\)\s*\Z", subject) or re.match(r"\AMerge pull request #(\d+)\b", subject)
    return int(match.group(1)) if match else None


def commit_source(message, *, legacy_repository=None, subject=None):
    """One source citation, preserving repaired messages whose trailers precede notes.

    Queue messages retain their Source-PR line when repair notes are appended, so
    inspect the entire message. Conflicting or malformed source lines refuse
    rather than falling back to the unqualified subject. Identical duplicates
    retain the queue chain reader's semantics.
    """
    values = []
    lines = message.splitlines()
    for line in lines:
        match = _TRAILER_RE.match(line)
        if match and match["token"].lower() == "source-pr":
            values.append(match["value"])
    # Real history callers supply git's %s: it joins the entire first paragraph.
    number = subject_number(subject if subject is not None else lines[0] if lines else "")
    if values:
        citations = set()
        for value in values:
            citations.add(PullSource.parse(value))
        if len(citations) != 1:
            raise ProvenanceError("ambiguous Source-PR citation")
        return resolve_source(citations.pop(), number, legacy_repository)
    return resolve_source(None, number, legacy_repository)


def resolve_source(citation, number, legacy_repository):
    if citation is not None:
        if number is not None and number != citation.number:
            raise ProvenanceError("merge subject conflicts with Source-PR citation")
        return citation
    if number is None:
        return None
    if not isinstance(legacy_repository, RepositoryRef):
        raise ProvenanceError("an explicit legacy repository is required for unqualified merge subjects")
    return PullSource(legacy_repository, number)


class MigrationMap:
    """An explicit one-to-one import/rename map; no number-preserving assumption."""

    def __init__(self, rows=(), *, commits=None):
        self._mapping = {}
        targets = set()
        for before, after in rows:
            if not isinstance(before, PullSource) or not isinstance(after, PullSource):
                raise ProvenanceError("migration entries must be qualified source citations")
            if before in self._mapping or after in targets:
                raise ProvenanceError("migration map contains duplicate source or destination citations")
            if before.namespace == after.namespace:
                raise ProvenanceError("migration entries must cross repository namespaces")
            self._mapping[before] = after
            targets.add(after)
        if set(self._mapping) & targets:
            raise ProvenanceError("migration destinations must not be migration sources")
        overrides = {}
        if commits is not None and not isinstance(commits, dict):
            raise ProvenanceError("commit overrides must be keyed by immutable SHA")
        for sha, citation in (commits or {}).items():
            if not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha) is None:
                raise ProvenanceError("commit overrides require immutable SHA keys")
            if citation is not None and not isinstance(citation, PullSource):
                raise ProvenanceError("commit overrides require a citation or explicit null")
            overrides[sha] = citation
        self.commits = MappingProxyType(overrides)

    @classmethod
    def parse(cls, row):
        if (
            not isinstance(row, dict)
            or not {"schema", "mappings"} <= set(row) <= {"schema", "mappings", "commits"}
            or row["schema"] != "tauceti-progress.migration/v1"
        ):
            raise ProvenanceError("invalid progress migration schema")
        if not isinstance(row["mappings"], list):
            raise ProvenanceError("invalid progress migration entries")
        entries = []
        for pair in row["mappings"]:
            if not isinstance(pair, dict) or set(pair) != {"from", "to"}:
                raise ProvenanceError("invalid progress migration entry")
            entries.append((PullSource.parse(pair["from"]), PullSource.parse(pair["to"])))
        commits = row.get("commits", {})
        if not isinstance(commits, dict):
            raise ProvenanceError("invalid commit overrides")
        return cls(
            entries,
            commits={sha: PullSource.parse(value) if value is not None else None for sha, value in commits.items()},
        )

    def record(self):
        return {
            "schema": "tauceti-progress.migration/v1",
            "mappings": [
                {"from": before.key, "to": after.key}
                for before, after in sorted(self._mapping.items(), key=lambda pair: pair[0].key)
            ],
            "commits": {sha: citation.key if citation else None for sha, citation in self.commits.items()},
        }

    @property
    def digest(self):
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    def canonical_bytes(self):
        raw = json.dumps(self.record(), sort_keys=True, separators=(",", ":")).encode()
        if len(raw) > MAX_MAP_BYTES:
            raise ProvenanceError("canonical history migration exceeds the size limit")
        return raw

    @property
    def empty(self):
        return not self._mapping and not self.commits

    def number(self, citation, repository, repository_id=None):
        if same_source(citation, repository, repository_id):
            return citation.number
        mapped = self._mapping.get(citation)
        if mapped is None or not same_source(mapped, repository, repository_id):
            raise ProvenanceError("foreign source citation needs an explicit migration to the report repository")
        return mapped.number


def same_source(citation, repository, repository_id=None):
    """Whether `citation` numbers a pull request of the report repository.

    The URL must match. A cited native ID must also equal `repository_id`, the report repository's
    own, so a recreated repository of the same name never inherits its predecessor's numbers; an
    unknown `repository_id` matches no cited ID. A citation without one (a bare subject, or a writer
    that records none) is matched by URL alone.
    """
    return citation.repository.key == repository.key and (
        citation.native_id is None or citation.native_id == repository_id
    )


def configured_migration():
    """Snapshot the operator-selected map; callers retain its record for later stages."""
    selected = os.environ.get("TAUCETI_PROGRESS_HISTORY_MAP")
    if not selected:
        return MigrationMap()
    try:
        path = Path(selected)
        if not path.is_absolute():
            raise ValueError
        with path.open("rb") as stream:
            raw = stream.read(MAX_MAP_BYTES + 1)
        if len(raw) > MAX_MAP_BYTES:
            raise ValueError
        migration = MigrationMap.parse(read_json(raw, max_bytes=MAX_MAP_BYTES))
        migration.canonical_bytes()
        return migration
    except ProvenanceError:
        raise
    except (OSError, ValueError, TypeError):
        raise ProvenanceError("configured history migration is unavailable or invalid") from None


def policy_digest(value):
    """Validate a retained policy identity without putting the table in the plan."""
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ProvenanceError("invalid saved history migration digest")
    return value


def retain_migration(migration, plan_path):
    """Retain the configuration beside the plan, like its existing facts bundle."""
    from .docs import ArtifactError, store

    target = Path(plan_path)
    sibling = target.with_name(target.name + ".history-" + migration.digest + ".json")
    raw = migration.canonical_bytes()
    if not store(sibling, raw):
        raise ArtifactError("could not retain history migration beside the plan")
    return sibling.name


def saved_migration(plan, plan_path):
    """Restore the plan's exact policy; current configuration cannot reinterpret it."""
    from .docs import FactsReplanRequired, ReplanRequired

    if "history_migration_sha256" not in plan:
        if "history_migration_file" in plan or "history_migration" in plan:
            raise ReplanRequired("saved history migration has no digest; replan")
        if not configured_migration().empty:
            raise FactsReplanRequired("legacy plan pins no configured history migration; replan")
        return MigrationMap()
    try:
        digest = policy_digest(plan["history_migration_sha256"])
        name = plan.get("history_migration_file")
        if digest == MigrationMap().digest:
            return MigrationMap()
        if (
            not isinstance(name, str)
            or name in ("", ".", "..")
            or "/" in name
            or "\\" in name
            or any(ord(c) < 32 for c in name)
        ):
            raise ProvenanceError("invalid saved migration filename")
        with (Path(plan_path).parent / name).open("rb") as stream:
            raw = stream.read(MAX_MAP_BYTES + 1)
        migration = MigrationMap.parse(read_json(raw, max_bytes=MAX_MAP_BYTES))
        if migration.digest != digest:
            raise ProvenanceError("saved migration digest differs from its record")
        return migration
    except FileNotFoundError:
        raise FactsReplanRequired("saved history migration is missing; replan") from None
    except (OSError, ValueError, TypeError):
        raise ReplanRequired("saved history migration is invalid; retain artifacts and replan") from None
