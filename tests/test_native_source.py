"""Direct native planning and factual attribution; no gh or shared-state fallback."""

import base64
import contextlib
import gzip
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tauceti_tools.forge import GITHUB, Forge, ForgeError, Forgejo, Incomplete, RepositoryRef  # noqa: E402

from progress import apply, cli, context, facts, files, gh, plan, window  # noqa: E402
from progress.docs import Docs, ReplanRequired  # noqa: E402
from progress.native_source import NativeRepository, NativeSource, history_options_from_record  # noqa: E402
from progress.provenance import MigrationMap, PullSource  # noqa: E402


class NativePlanning(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="progress-native-")
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        self.requests, self.failures = [], {}
        self.closed, self.open = [], []
        self.label_ids = {"roadmap/Area": 1, "roadmap/Other": 2}
        self.org_labels = []
        self.pull_counts = {}
        self.omit_pull_count = False
        self.repository_ids = {"team/Code": 11, "team/Roadmap": 12}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                outer.requests.append((self.path, self.headers.get("Authorization")))
                path = urlsplit(self.path)
                name = path.path.removeprefix("/api/v1/")
                query = parse_qs(path.query)
                status, body, total = 200, None, None
                if name in outer.failures:
                    status, body = outer.failures[name], {"message": "fixture refusal"}
                elif name == "settings/api":
                    body = {"max_response_items": 2}
                elif name == "orgs/team/labels":
                    if outer.org_labels is None:
                        status, body = 404, {"message": "unknown organization"}
                    else:
                        page = int(query.get("page", ["1"])[0])
                        body = outer.org_labels[(page - 1) * 2 : page * 2]
                        total = len(outer.org_labels)
                elif name == "user":
                    body = {"id": 21, "login": "operator"}
                elif name.startswith("repos/"):
                    rest = name.removeprefix("repos/")
                    owner, repo, *tail = rest.split("/")
                    repository = owner + "/" + repo
                    endpoint = "/".join(tail)
                    if not endpoint:
                        body = {"full_name": repository, "id": outer.repository_ids[repository]}
                    elif endpoint == "labels":
                        total = len(outer.label_ids)
                        catalog = [{"name": name, "id": native_id} for name, native_id in outer.label_ids.items()]
                        page = int(query.get("page", ["1"])[0])
                        body = catalog[(page - 1) * 2 : page * 2]
                    elif endpoint == "pulls":
                        rows = outer.open if query.get("state") == ["open"] else outer.closed + outer.open
                        if query.get("labels"):
                            requested = set(map(int, query["labels"]))
                            rows = [
                                row
                                for row in rows
                                if "labels" not in row
                                or any(
                                    label.get("id", outer.label_ids.get(label["name"])) in requested
                                    for label in row.get("labels") or []
                                )
                            ]
                        page = int(query.get("page", ["1"])[0])
                        total = None if outer.omit_pull_count else outer.pull_counts.get(page, len(rows))
                        body = rows[(page - 1) * 2 : page * 2]  # server cap below requested limit
                    elif endpoint.startswith("pulls/"):
                        number = int(endpoint.split("/")[1])
                        body = next((row for row in outer.closed + outer.open if row["number"] == number), None)
                    elif endpoint.startswith("git/commits/"):
                        body = {"sha": "a" * 40}
                    elif endpoint == "commits":
                        body = (
                            [{"commit": {"committer": {"date": "2020-01-01T00:00:00Z"}, "message": "initial"}}]
                            if query.get("page") == ["1"]
                            else []
                        )
                    elif endpoint.startswith("contents/"):
                        body = {
                            "type": "file",
                            "encoding": "base64",
                            "content": base64.b64encode(b"saved cursor").decode(),
                        }
                if body is None:
                    status, body = 404, {"message": "not found"}
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                if total is not None:
                    self.send_header("X-Total-Count", str(total))
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.forge = Forge("forgejo", f"http://127.0.0.1:{server.server_port}")
        self.api = Forgejo(self.forge, token="fixture-native-token")
        self.content = NativeRepository(RepositoryRef(self.forge, "team/Code"), self.api)
        self.roadmap = NativeRepository(RepositoryRef(self.forge, "team/Roadmap"), self.api)
        self.source = NativeSource(self.content, self.roadmap)
        self.code = self.root / "code"
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.code)], check=True, capture_output=True)
        self.git_env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Fixture",
            "GIT_COMMITTER_NAME": "Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        }
        self.initial = self.commit("Initial")
        self.requests.clear()

    def commit(self, message, file=None):
        if file:
            path = self.code / file
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("theorem alpha : True := trivial\n")
            subprocess.run(["git", "-C", str(self.code), "add", file], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(self.code), "commit", "--allow-empty", "-qm", message],
            check=True,
            capture_output=True,
            env=self.git_env,
        )
        return window.git(["rev-parse", "HEAD"], self.code).strip()

    def row(self, number, area="Area", merged=False, sha=None):
        return {
            "number": number,
            "state": "closed",
            "merged": merged,
            "merge_commit_sha": sha,
            "labels": [{"name": "roadmap/" + area}],
        }

    def docs(self, sha):
        base = "https://docs.example/docs"
        data = {
            "schema_version": 1,
            "source_commit": sha,
            "provenance": {"toolchain": "leanprover/lean4:v4.0.0", "doc_gen": "fixture"},
            "modules": {
                "TauCeti/A.lean": {
                    "alpha": {
                        "kind": "theorem",
                        "file": "TauCeti/A.lean",
                        "start": 1,
                        "end": 1,
                        "doc": "Alpha.",
                        "url": base + "/TauCeti/A.html#alpha",
                    }
                }
            },
        }
        return Docs(base=base, raw=gzip.compress(json.dumps(data).encode()), cache_dir=self.root / "facts-cache")

    def test_one_complete_scan_combines_closed_unmerged_and_native_merges_for_all_areas(self):
        self.closed = [self.row(5, "Other"), self.row(4, merged=True, sha="a" * 40), self.row(3), self.row(2, "Other")]
        self.assertEqual(
            self.content.labelled_histories(["Area", "Other"]),
            {"Area": {4: "a" * 40, 3: None}, "Other": {5: None, 2: None}},
        )
        paths = [path for path, _ in self.requests if "/pulls?" in path]
        self.assertEqual(len(paths), 2)  # both counted data pages, independent of area count
        self.assertTrue(all("state=all" in path for path in paths))
        self.assertTrue(all(token == "token fixture-native-token" for _, token in self.requests))

    def test_targeted_history_does_not_fetch_unrelated_closed_prs(self):
        self.closed = [self.row(99, "Other"), self.row(7), self.row(98, "Other")]
        self.assertEqual(self.content.labelled_histories(["Area"]), {"Area": {7: None}})
        requests = [path for path, _ in self.requests if "/pulls?" in path]
        self.assertEqual(len(requests), 1)
        self.assertTrue(all(parse_qs(urlsplit(path).query)["labels"] == ["1"] for path in requests))

    def test_large_label_union_is_batched_and_shared_pr_is_deduplicated(self):
        areas = [f"Area{n}" for n in range(52)]
        self.label_ids = {"roadmap/" + area: n + 1 for n, area in enumerate(areas)}
        row = self.row(7, "Area0")
        row["labels"].append({"name": "roadmap/Area51"})
        self.closed = [row]
        histories = self.content.labelled_histories(areas)
        self.assertEqual(histories["Area0"], {7: None})
        self.assertEqual(histories["Area51"], {7: None})
        self.assertEqual(sum(bool(value) for value in histories.values()), 2)
        requests = [path for path, _ in self.requests if "/pulls?" in path]
        self.assertEqual(len(requests), 2)
        self.assertTrue(all(len(parse_qs(urlsplit(path).query)["labels"]) <= 50 for path in requests))

    def test_shared_and_repository_labels_with_same_name_are_both_included(self):
        self.org_labels = [{"id": 3, "name": "ROADMAP/AREA"}]
        shared = self.row(7)
        shared["labels"] = [{"id": 3, "name": "ROADMAP/AREA"}]
        self.closed = [shared, self.row(6)]
        self.assertEqual(self.content.labelled_histories(["Area"]), {"Area": {7: None, 6: None}})
        requests = [path for path, _ in self.requests if "/pulls?" in path]
        self.assertTrue(all(parse_qs(urlsplit(path).query)["labels"] == ["1", "3"] for path in requests))

    def test_unknown_shared_catalog_uses_complete_authorized_history_not_empty(self):
        self.closed = [self.row(7)]
        for status in (403, 404):
            self.requests.clear()
            self.failures["orgs/team/labels"] = status
            self.assertEqual(self.content.labelled_histories(["Area"]), {"Area": {7: None}})
            requests = [path for path, _ in self.requests if "/pulls?" in path]
            self.assertTrue(requests)
            self.assertTrue(all("labels" not in parse_qs(urlsplit(path).query) for path in requests))

    def test_missing_label_catalog_has_no_pr_scan_but_forbidden_catalog_refuses(self):
        self.assertEqual(self.content.labelled_histories(["Unlabelled"]), {"Unlabelled": {}})
        self.assertFalse(any("/pulls?" in path for path, _ in self.requests))
        self.failures["repos/team/Code/labels"] = 403
        with self.assertRaises(ForgeError):
            self.content.labelled_histories(["Area"])

    def test_real_git_plan_and_facts_keep_native_identity_when_github_has_same_number(self):
        sha = self.commit("Queue accepted\n\nSource-PR: " + self.content.repository.key + "#7", "TauCeti/A.lean")
        self.closed = [self.row(7)]
        roadmap = self.root / "roadmap"
        (roadmap / "TauCetiRoadmap/Area").mkdir(parents=True)
        (roadmap / "TauCetiRoadmap/Area/README.md").write_text("# Area\n")
        docs = self.docs(sha)
        with patch.object(gh, "gh", side_effect=AssertionError("native work must not fall back to gh")):
            result = plan.build_plan(roadmap, self.code, source=self.source, ref="main", min_prs=1, docs=docs)
            collected = facts.collect(
                self.code, result["from_sha"], sha, pr_numbers=result["prs"], docs=docs, source_record=result["source"]
            )
        self.assertEqual(result["prs"], [7])
        self.assertEqual(result["from_sha"], self.initial)
        self.assertEqual(result["source"]["content"]["forge"]["url"], self.forge.url)
        self.assertEqual(result["source"]["content"]["id"], 11)
        self.assertEqual(collected["source"], result["source"])
        self.assertEqual([(d["name"], d["pr"]) for d in collected["declarations"]], [("alpha", 7)])
        wrong = {
            **result["source"],
            "content": {**result["source"]["content"], "forge": {"kind": "forgejo", "url": "https://other.example"}},
        }
        with self.assertRaisesRegex(window.GitError, "explicit migration"):
            facts.collect(self.code, result["from_sha"], sha, pr_numbers=[7], docs=docs, source_record=wrong)

    def test_native_merge_credits_the_commit_that_landed_lines_on_main(self):
        subprocess.run(["git", "-C", str(self.code), "checkout", "-qb", "proof"], check=True, capture_output=True)
        self.commit("Feature commit", "TauCeti/A.lean")
        subprocess.run(["git", "-C", str(self.code), "checkout", "-q", "main"], check=True, capture_output=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(self.code),
                "merge",
                "--no-ff",
                "-qm",
                "Native merge (#7)\n\nSource-PR: " + self.content.repository.key + "#7",
                "proof",
            ],
            env=self.git_env,
            check=True,
            capture_output=True,
        )
        sha = window.git(["rev-parse", "HEAD"], self.code).strip()
        self.closed = [self.row(7, merged=True, sha=sha)]
        roadmap = self.root / "roadmap"
        (roadmap / "TauCetiRoadmap/Area").mkdir(parents=True)
        (roadmap / "TauCetiRoadmap/Area/README.md").write_text("# Area\n")
        docs = self.docs(sha)
        with patch.object(gh, "gh", side_effect=AssertionError("native work reached gh")):
            result = plan.build_plan(roadmap, self.code, source=self.source, ref="main", min_prs=1, docs=docs)
            collected = facts.collect(
                self.code, result["from_sha"], sha, pr_numbers=[7], docs=docs, source_record=result["source"]
            )
        self.assertEqual([(d["name"], d["pr"]) for d in collected["declarations"]], [("alpha", 7)])

    def test_open_report_ownership_projects_native_head_and_owner_identities(self):
        self.open = [
            {
                "number": 3,
                "state": "open",
                "merged": False,
                "title": "Report",
                "body": "prose",
                "created_at": "2020-01-01T00:00:00Z",
                "head": {
                    "label": "progress/aaaaaaa-bbbbbbb/Area",
                    "repo": {
                        "id": 99,
                        "name": "Roadmap",
                        "full_name": "operator/Roadmap",
                        "owner": {"id": 99, "login": "operator"},
                    },
                },
            }
        ]
        rows = self.roadmap.open_reports()
        owns = self.roadmap.report_owner_filter()
        self.assertFalse(owns(rows[0]))  # same login, another authenticated server-local user ID
        self.open[0]["head"]["repo"]["owner"]["id"] = 21
        self.assertTrue(owns(self.roadmap.open_reports()[0]))
        blocked, _ = plan.in_flight_areas(rows, owner_filter=owns)
        self.assertEqual(blocked, {})

    def test_pr_commentary_url_remains_on_source_origin_and_missing_body_refuses(self):
        self.closed = [{**self.row(7), "title": "Proof", "body": None}]
        details = self.content.pr_details([7])
        self.assertEqual(details[0]["url"], self.content.repository.pull_url(7))
        self.assertEqual(details[0]["body"], "")
        self.assertEqual(details[0]["repository"]["id"], 11)
        del self.closed[0]["body"]
        with self.assertRaises(Incomplete):
            self.content.pr_details([7])

    def test_unavailable_or_partial_history_never_becomes_empty(self):
        self.failures["repos/team/Code/pulls"] = 403
        with self.assertRaises(ForgeError):
            self.content.labelled_histories(["Area"])
        self.failures.clear()
        self.closed = [self.row(7, merged=True)]
        with self.assertRaises(Incomplete):
            self.content.labelled_histories(["Area"])
        self.closed = [{"number": 7, "state": "closed", "merged": False}]
        with self.assertRaises(Incomplete):
            self.content.labelled_histories(["Area"])

    def test_duplicate_changed_pages_and_ambiguous_area_labels_refuse(self):
        self.closed = [self.row(7), self.row(6), self.row(7, merged=True, sha="a" * 40)]
        with self.assertRaisesRegex(Incomplete, "changed during pagination"):
            self.content.labelled_histories(["Area"])
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.content.labelled_histories(["Area", "area"])

    def test_changed_membership_at_page_boundary_refuses_instead_of_skipping_work(self):
        self.closed = [self.row(9), self.row(8), self.row(7)]
        self.pull_counts = {1: 3, 2: 2}
        with self.assertRaisesRegex(Incomplete, "count changed"):
            self.content.labelled_histories(["Area"])
        self.pull_counts.clear()
        self.omit_pull_count = True
        with self.assertRaisesRegex(Incomplete, "count is unavailable"):
            self.content.labelled_histories(["Area"])

    def test_reopened_landed_queue_pr_remains_in_the_native_history(self):
        self.closed = [self.row(9), self.row(8)]
        reopened = {**self.row(7), "state": "open"}
        self.open = [reopened]
        histories = self.content.labelled_histories(["Area"])
        self.assertEqual(histories, {"Area": {9: None, 8: None, 7: None}})
        self.assertTrue(all("state=all" in path for path, _ in self.requests if "/pulls?" in path))

    def test_steady_state_cursor_cannot_advance_past_unrecognized_native_merge(self):
        before = self.commit("First accepted\n\nSource-PR: " + self.content.repository.key + "#7")
        missing = self.commit("Merge pull request 'Missing proof' (#8) from member:proof into main")
        tip = self.commit("Queue accepted\n\nSource-PR: " + self.content.repository.key + "#9", "TauCeti/A.lean")
        self.closed = [self.row(9), self.row(8, merged=True, sha=missing), self.row(7)]
        roadmap = self.root / "roadmap"
        area = roadmap / "TauCetiRoadmap/Area"
        area.mkdir(parents=True)
        (area / "README.md").write_text("# Area\n")
        (area / "PROGRESS.md").write_text(
            files.new_progress_file("Area")
            + files.render_section("Area", self.initial, before, [7], "earlier", "Earlier report.")
        )
        docs = self.docs(tip)
        with self.assertRaisesRegex(window.GitError, "no matching source provenance"):
            plan.build_plan(roadmap, self.code, source=self.source, ref="main", min_prs=1, docs=docs)
        repaired = MigrationMap(commits={missing: PullSource(self.content.repository, 8)})
        with window.using_migration(repaired):
            result = plan.build_plan(roadmap, self.code, source=self.source, ref="main", min_prs=1, docs=docs)
        self.assertEqual(result["prs"], [9, 8])
        with window.using_migration(MigrationMap(commits={missing: None})):
            result = plan.build_plan(roadmap, self.code, source=self.source, ref="main", min_prs=1, docs=docs)
        self.assertEqual(result["prs"], [9])  # explicit operator exclusion is respected

    def test_native_injected_legacy_open_rows_refuse_before_plan_selection(self):
        row = {"headRefName": "progress/aaaaaaa-bbbbbbb/Area", "headRepositoryOwner": {"login": "operator"}}
        with self.assertRaisesRegex(Incomplete, "qualified head"):
            plan.build_plan(self.root, self.code, source=self.source, commits=[], open_prs=[row])
        with self.assertRaisesRegex(ValueError, "either"):
            plan.in_flight_areas([], owners=set(), owner_filter=lambda _: True)

    def test_github_native_merge_also_credits_the_mainline_landing(self):
        subprocess.run(["git", "-C", str(self.code), "checkout", "-qb", "proof"], check=True, capture_output=True)
        self.commit("Feature commit", "TauCeti/A.lean")
        subprocess.run(["git", "-C", str(self.code), "checkout", "-q", "main"], check=True, capture_output=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(self.code),
                "merge",
                "--no-ff",
                "-qm",
                "Merge pull request #7 from member/proof",
                "proof",
            ],
            env=self.git_env,
            check=True,
            capture_output=True,
        )
        sha = window.git(["rev-parse", "HEAD"], self.code).strip()
        collected = facts.collect(self.code, self.initial, sha, pr_numbers=[7], docs=self.docs(sha))
        self.assertEqual([(d["name"], d["pr"]) for d in collected["declarations"]], [("alpha", 7)])

    def test_imported_github_history_needs_explicit_namespace_and_number_mapping(self):
        sha = self.commit("Imported proof (#7)", "TauCeti/A.lean")
        self.closed = [self.row(103)]
        old = RepositoryRef(GITHUB, "old/Math")
        source = NativeSource(self.content, self.roadmap, legacy_content=old, legacy_through=sha)
        mapping = MigrationMap([(PullSource(old, 7), PullSource(self.content.repository, 103))])
        with window.using_migration(mapping):
            collected = facts.collect(
                self.code, self.initial, sha, pr_numbers=[103], docs=self.docs(sha), source_record=source.record()
            )
        self.assertEqual([(d["name"], d["pr"]) for d in collected["declarations"]], [("alpha", 103)])
        bad = source.record()
        bad["legacy_content"]["forge"]["url"] = 123
        with self.assertRaises(ReplanRequired):
            facts.collect(self.code, self.initial, sha, docs=self.docs(sha), source_record=bad)

    def test_repository_replacement_and_client_origin_change_refuse(self):
        self.repository_ids["team/Code"] = 99
        with self.assertRaisesRegex(Incomplete, "identity changed"):
            self.content.revalidate()
        before = len(self.requests)
        self.api.forge = Forge("forgejo", "https://other.example")
        with self.assertRaisesRegex(Incomplete, "origin changed"):
            self.content.labelled_histories(["Area"])
        self.assertEqual(len(self.requests), before)

    def test_empty_content_and_missing_repository_are_distinct(self):
        self.assertEqual(self.content.file_at("Area/PROGRESS.md"), "saved cursor")
        requests = [url for url, _ in self.requests if "/git/commits/" in url]
        self.assertEqual(parse_qs(urlsplit(requests[-1]).query),
                         {"stat": ["false"], "verification": ["false"], "files": ["false"]})
        content_requests = [url for url, _ in self.requests if "/contents/" in url]
        self.assertEqual(parse_qs(urlsplit(content_requests[-1]).query)["ref"], ["a" * 40])
        self.failures["repos/team/Code/git/commits/gone"] = 404
        with self.assertRaises(ForgeError):
            self.content.file_at("Area/PROGRESS.md", ref="gone")
        self.failures["repos/team/Code/contents/Area/PROGRESS.md"] = 404
        self.assertIsNone(self.content.file_at("Area/PROGRESS.md"))
        self.failures["repos/team/Code"] = 403
        with self.assertRaises(ForgeError):
            self.content.file_at("Area/PROGRESS.md")

    def test_qualified_saved_plan_never_routes_to_legacy_github_publication(self):
        saved = self.root / "native-plan.json"
        native_plan = {"source": self.source.record()}
        saved.write_text(json.dumps(native_plan))
        with (
            patch.object(gh, "gh", side_effect=AssertionError("qualified publication reached legacy gh")),
            contextlib.redirect_stderr(io.StringIO()) as errors,
        ):
            with self.assertRaisesRegex(apply.ApplyError, "qualified source publisher"):
                apply.run(native_plan, "paid-status", "paid-section", self.root)
            with self.assertRaisesRegex(apply.ApplyError, "qualified source publisher"):
                apply.sweep(native_plan, "unused", "unused")
            self.assertEqual(
                cli.main(
                    [
                        "apply",
                        "--plan",
                        str(saved),
                        "--status-body",
                        "paid-status",
                        "--section-body",
                        "paid-section",
                        "--roadmap-dir",
                        str(self.root),
                    ]
                ),
                78,
            )
        self.assertIn("preserve the prepared report", errors.getvalue())

    def test_saved_source_schema_and_legacy_namespace_are_explicit(self):
        record = self.source.record()
        self.assertEqual(history_options_from_record(record)["repository"], self.content.repository)
        old = RepositoryRef(GITHUB, "team/Code")
        source = NativeSource(self.content, self.roadmap, legacy_content=old, legacy_through=self.initial)
        self.assertEqual(history_options_from_record(source.record())["legacy_repository"], old)
        bad = {**record, "content": {**record["content"], "id": True}}
        with self.assertRaises(ValueError):
            history_options_from_record(bad)
        with self.assertRaises(ReplanRequired):
            facts.collect(self.code, self.initial, self.initial, source_record=bad)

    def test_cutover_disambiguates_reused_bare_numbers_and_survives_retention(self):
        old = RepositoryRef(GITHUB, "old/Math")
        earlier = self.commit("Earlier imported proof (#199)", "earlier.txt")
        boundary = self.commit("Imported proof (#200)", "old.txt")
        native = self.commit("Native proof (#200)", "new.txt")
        source = NativeSource(self.content, self.roadmap, legacy_content=old, legacy_through=boundary)
        history = history_options_from_record(json.loads(json.dumps(source.record())))
        migration_rows = [(PullSource(old, 200), PullSource(self.content.repository, 7)),
                          (PullSource(old, 199), PullSource(self.content.repository, 6))]
        mapping = MigrationMap(migration_rows)
        @window.history_round
        def observations():
            return [window.window_prs(self.code, self.initial, tip, migration=mapping, **history)
                    for tip in (native, boundary, earlier, native)]
        self.assertEqual(observations(), [[200, 7, 6], [7, 6], [6], [200, 7, 6]])
        @window.history_round
        def bootstrap_reads():
            for _ in range(3):
                self.assertEqual(plan.unaccounted_prs(self.code, {6: None, 7: None, 200: None}, ref=native,
                                                     migration=mapping, **history), [])
                self.assertEqual(window.earliest_merged(self.code, [6, 7, 200], ref=native,
                                                        migration=mapping, **history), (6, earlier))
        # Scaled analogue of the 29k history: one chain, messages and reachable
        # ancestry fit; retaining a second full chain would evict the messages.
        with patch.object(window, "MAX_HISTORY_ROWS", 12), \
                patch.object(window.git_history, "read", wraps=window.git_history.read) as reads:
            bootstrap_reads()
            self.assertEqual(reads.call_count, 1)
        self.assertEqual(window.window_prs(self.code, boundary, native, migration=mapping, **history), [200])
        excluded_boundary = MigrationMap(migration_rows, commits={boundary: None})
        self.assertEqual(window.window_prs(self.code, self.initial, native,
                                           migration=excluded_boundary, **history), [200, 6])
        with self.assertRaises(window.GitError):
            window.window_prs(self.code, boundary, native, repository=self.content.repository)
        with self.assertRaises(ValueError):
            NativeSource(self.content, self.roadmap, legacy_content=old)
        with self.assertRaises(ValueError):
            NativeSource(self.content, self.roadmap, legacy_content=old, legacy_through="main")
        branch = window.git(["branch", "--show-current"], self.code).strip()
        subprocess.run(["git", "-C", str(self.code), "checkout", "-qb", "side-cursor", self.initial], check=True)
        side = self.commit("Side change", "side.txt")
        subprocess.run(["git", "-C", str(self.code), "checkout", "-q", branch], check=True)
        subprocess.run(["git", "-C", str(self.code), "merge", "--no-ff", "-qm", "Land side change", "side-cursor"],
                       env=self.git_env, check=True)
        merged = window.head_sha(self.code, ref="HEAD")
        self.assertEqual(window.window_prs(self.code, side, merged, migration=mapping, **history), [200, 7, 6])
        subprocess.run(["git", "-C", str(self.code), "checkout", "-qb", "diverged", self.initial], check=True)
        diverged = self.commit("Other branch (#200)", "diverged.txt")
        with self.assertRaisesRegex(window.GitError, "cutover commit"):
            window.window_prs(self.code, self.initial, diverged, migration=mapping, **history)

    def test_nested_landed_merge_is_checked_in_bootstrap_and_existing_window(self):
        branch = window.git(["branch", "--show-current"], self.code).strip()
        subprocess.run(["git", "-C", str(self.code), "checkout", "-qb", "nested"], check=True)
        nested = self.commit("Unrecognized landed change", "nested.txt")
        subprocess.run(["git", "-C", str(self.code), "checkout", "-q", branch], check=True)
        self.commit("Independent change", "main.txt")
        subprocess.run(["git", "-C", str(self.code), "merge", "--no-ff", "-qm", "Aggregate", "nested"],
                       env=self.git_env, check=True)
        head = self.commit("Recognized change (#9)", "later.txt")
        self.assertEqual(plan.unaccounted_prs(self.code, {8: nested, 9: head}, ref=head), [8])
        with self.assertRaisesRegex(window.GitError, "no matching source provenance"):
            plan.area_window(self.code, {8: nested, 9: head}, self.initial, head)
        excluded = MigrationMap([], commits={nested: None})
        self.assertEqual(plan.unaccounted_prs(self.code, {8: nested, 9: head}, ref=head, migration=excluded), [])
        self.assertEqual(plan.area_window(self.code, {8: nested, 9: head}, self.initial, head,
                                          migration=excluded), [9])

    def test_explicit_null_source_facts_never_falls_back_to_github(self):
        with self.assertRaises(ReplanRequired):
            facts.collect(self.code, self.initial, self.initial, source_record=None)
        saved = self.root / "null-source.json"
        saved.write_text(json.dumps({"source": None}))
        with patch.object(facts, "collect", side_effect=AssertionError("null source reached legacy facts")):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["facts", "--plan", str(saved), "--code-dir", str(self.code)]), 78)

    def test_writer_context_requires_matching_qualified_facts_and_commentary(self):
        p = {"source": self.source.record(), "roadmap": "Area", "from_sha": self.initial,
             "to_sha": self.initial, "prs": [7], "documentation": {"facts_sha256": "test", "bundle_file": "saved.gz"}}
        f = {"source": self.source.record(), "declarations": [],
             "from_sha": self.initial, "to_sha": self.initial, "documentation": {"facts_sha256": "test"},
             "prs": [{"number": 7}],
             "counts": {"prs": 1, "declarations": 0, "new": 0, "documented": 0, "files": 0}}
        detail = {"number": 7, "title": "Proof", "body": "", "repository": self.content.identity.record()}
        self.assertIn("Proof", context.render(p, f, [detail]))
        for facts_record, details in [
            ({k: v for k, v in f.items() if k != "source"}, [detail]),
            (f, [{k: v for k, v in detail.items() if k != "repository"}]),
            (f, [{**detail, "repository": self.roadmap.identity.record()}]),
            (f, [{**detail, "number": 8}]),
            ({**f, "from_sha": "a" * 40}, [detail]),
            ({**f, "documentation": {"facts_sha256": "other"}}, [detail]),
            ({**f, "prs": []}, [detail]),
            (f, [detail, detail]),
            (f, []),
        ]:
            with self.assertRaises(ReplanRequired):
                context.render(p, facts_record, details)

    def test_a_configured_foreign_contribution_is_planned_collected_and_given_commentary(self):
        from progress import commit_attribution
        from progress.provenance import COMMIT_SOURCES_ENV

        old = f"forgejo:{self.forge.url}/team/Code@10"  # the content repository's predecessor
        self.commit("Native proof (#7)\n\nSource-PR: " + self.content.repository.key + "#7", "docs/seven.md")
        sha = self.commit(f"feat: old work (#31)\n\nRoadmap: Area\nSource-PR: {old}#31", "TauCeti/A.lean")
        key = PullSource.parse(f"{old}#31").key
        self.closed = [{**self.row(7), "title": "Native proof", "body": "Native body"}]
        roadmap = self.root / "roadmap"
        (roadmap / "TauCetiRoadmap/Area").mkdir(parents=True)
        (roadmap / "TauCetiRoadmap/Area/README.md").write_text("# Area\n")
        docs = self.docs(sha)
        with patch.dict(os.environ, {COMMIT_SOURCES_ENV: old}), \
                patch.object(gh, "gh", side_effect=AssertionError("native work must not fall back to gh")):
            result = plan.build_plan(roadmap, self.code, source=self.source, ref="main", min_prs=1, docs=docs)
            collected = facts.collect(self.code, result["from_sha"], sha, pr_numbers=result["prs"], docs=docs,
                                      source_record=result["source"])
            history = history_options_from_record(result["source"])
            details = self.content.pr_details([7]) + commit_attribution.commentary(
                self.code, result["from_sha"], result["to_sha"], [key], **history)
        self.assertEqual(result["prs"], [key, 7])
        self.assertEqual([(d["name"], d["pr"]) for d in collected["declarations"]], [("alpha", key)])
        self.assertEqual((details[1]["title"], details[1]["repository"]["id"]), ("feat: old work", 10))
        text = context.render(result, collected, details)
        self.assertIn(f"### {files.pr_reference(key, 'TauCeti')}: feat: old work", text)
        self.assertIn("Native body", text)
        named = {**details[1], "repository": {**details[1]["repository"], "name": "team/Code"}}
        self.assertEqual(context.render(result, collected, [details[0], named]), text)
        # Each row must come from the repository its pull request cites.
        for wrong in ({**details[1], "repository": self.content.identity.record()},
                      {**details[0], "repository": details[1]["repository"]}):
            rows = [wrong, details[1]] if wrong["number"] == 7 else [details[0], wrong]
            with self.assertRaises(ReplanRequired):
                context.render(result, collected, rows)
        with self.assertRaisesRegex(ValueError, "commit-attributed"):
            self.content.pr_details([key])

    def test_native_in_flight_ownership_uses_ids_not_reused_logins(self):
        actor = {"login": "operator", "id": self.forge.key + "/users/21"}
        head = {"forge": {"kind": "forgejo", "url": self.forge.url}, "name": "operator/Roadmap", "id": 88}
        owns = self.roadmap.report_owner_filter()
        self.assertTrue(owns({"source_head_repository": head, "headRepositoryOwner": actor}))
        self.assertFalse(
            owns({"source_head_repository": head, "headRepositoryOwner": {**actor, "id": self.forge.key + "/users/99"}})
        )
        self.assertTrue(owns({"source_head_repository": self.roadmap.identity.record(), "headRepositoryOwner": {}}))

    def open_row(self, number, area="Area"):
        return {**self.row(number, area), "state": "open"}

    def test_the_label_cache_reads_the_complete_labelled_listing_each_run(self):
        from progress.native_source import LabelReader

        cache = self.root / "labels.json"
        reader = LabelReader(self.content)
        self.open = [self.open_row(9), self.open_row(8, "Other")]
        self.closed = [self.row(7, merged=True, sha="a" * 40), self.row(6), self.row(5, "Other")]
        self.assertEqual(reader.read(cache, ["Area", "Other"]),
                         {"Area": {9: None, 7: "a" * 40, 6: None}, "Other": {8: None, 5: None}})
        self.assertEqual(json.loads(cache.read_text())["binding"], f"{self.content.repository.key}#11")
        # 9 merged; 11 was opened and merged; 10 was labelled while open; 8 lost its label.
        self.open = [self.open_row(10)]
        self.closed = [self.row(11, merged=True, sha="b" * 40), self.row(9, merged=True, sha="c" * 40),
                       {**self.row(8), "labels": []}, *self.closed]
        expected = {"Area": {11: "b" * 40, 10: None, 9: "c" * 40, 7: "a" * 40, 6: None}, "Other": {5: None}}
        self.assertEqual(reader.read(cache, ["Area", "Other"]), expected)
        self.assertEqual(self.content.labelled_histories(["Area", "Other"]), expected)

    def test_the_label_cache_finds_an_imported_pr_listed_after_older_numbers(self):
        from progress.native_source import LabelReader

        cache, reader = self.root / "labels.json", LabelReader(self.content)
        self.closed = [self.row(9), self.row(7)]
        reader.read(cache, ["Area"])
        # #40 was imported with its original, older creation date, so it lists after #7.
        self.closed = [self.row(9), self.row(7), self.row(40)]
        self.assertEqual(reader.read(cache, ["Area"]), {"Area": {9: None, 7: None, 40: None}})

    def test_a_github_label_cache_is_never_read_for_a_native_repository(self):
        from progress.native_source import LabelReader

        cache = self.root / "labels.json"
        with patch.object(gh, "closed_prs_for_areas", lambda areas, repo=None: {a: {1: None} for a in areas}):
            gh.cached_closed_prs_for_areas(cache, ["Area"])
        self.closed = [self.row(7)]
        self.assertEqual(LabelReader(self.content).read(cache, ["Area"]), {"Area": {7: None}})
        self.assertTrue(json.loads(cache.read_text())["binding"].startswith("forgejo:"))
        with patch.object(gh, "closed_prs_for_areas", lambda areas, repo=None: {a: {1: None} for a in areas}), \
             patch.object(gh, "gh", side_effect=AssertionError("a native file is not searched on GitHub")):
            self.assertEqual(gh.cached_closed_prs_for_areas(cache, ["Area"]), {"Area": {1: None}})

    def test_a_bound_plan_reads_through_the_label_cache_and_rereads_its_choice(self):
        from progress.native_source import LabelReader

        self.commit("Earlier proof\n\nSource-PR: " + self.content.repository.key + "#7", "TauCeti/A.lean")
        sha = self.commit("Queue accepted\n\nSource-PR: " + self.content.repository.key + "#9", "TauCeti/A.lean")
        roadmap = self.root / "roadmap"
        (roadmap / "TauCetiRoadmap/Area").mkdir(parents=True)
        (roadmap / "TauCetiRoadmap/Area/README.md").write_text("# Area\n")
        cache, docs = self.root / "labels.json", self.docs(sha)
        self.closed = [self.row(9), self.row(6)]
        LabelReader(self.content).read(cache, ["Area"])
        # #7 closed before the cursor and is labelled only now: no incremental read can see it.
        self.closed = [self.row(9), self.row(7), self.row(6)]
        with patch.object(gh, "gh", side_effect=AssertionError("native work must not fall back to gh")):
            result = plan.build_plan(roadmap, self.code, source=self.source, ref="main", min_prs=1, docs=docs,
                                     label_cache=cache)
        self.assertEqual(result["prs"], [9, 7])
        self.assertIn("7", json.loads(cache.read_text())["areas"]["Area"])

if __name__ == "__main__":
    unittest.main()
