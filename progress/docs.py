"""The generated API documentation, read as the authority on what declarations exist.

This module replaced a hand-written Lean scanner, and the reason is worth recording so nobody
reintroduces one.

Deciding what a Lean file declares cannot be done by reading the text. Names are qualified by an
enclosing `namespace`, which interacts with `section`, with `end` closing either, with `open ... in`,
and with `_root_`; and many real declarations are never written down at all -- structure projections,
constructors, instances and `deriving` output are produced during elaboration. A Python
approximation of that gets *most* names right, which is the worst possible outcome: the wrong ones
are indistinguishable from the right ones, and a documentation link built from a wrong name is a
plausible-looking dead link. The first version of this code produced
`ContinuousLinearMap.IsFredholm.of_continuousLinearEquiv` for a declaration the compiler calls
`TauCeti.IsFredholm.of_continuousLinearEquiv`.

doc-gen4 already publishes the answer, computed from the elaborated environment:

* `declarations/declaration-data.bmp` -- every declaration, with its kind and the page it lives on.
* each module page -- for every declaration, a `gh_link` giving the exact source commit, file and
  line range.

So names, kinds, links and source positions are all read from there. What remains for this project's
own code is a question git can answer exactly -- "were these lines written during this window?" --
and reading a comment block that sits immediately above a line number the documentation supplied.
Neither requires knowing any Lean grammar.
"""

import json
import os
import pathlib
import re
import tempfile
import time
import urllib.error
import urllib.request

DOCS_BASE = "https://taucetiproject.github.io/TauCeti/docs"
INDEX_PATH = "declarations/declaration-data.bmp"
# The commit the deployed tree documents. TauCeti's deploy stamps it into the tree (pages.yml, "Fold
# the API docs into the site"), and says that anything needing certainty should read it rather than
# the `docgen` branch, which is best-effort.
SOURCE_SHA_PATH = "SOURCE_SHA"
_SHA_RE = re.compile(r"\A[0-9a-f]{40}\Z")

# How long a cached page may be reused across runs.
#
# The site is static within one build but the builds keep coming, so a cache with no expiry pins
# every later run to whichever build it first saw. That is not hypothetical: a worker's
# `/tmp/tauceti-docs-cache` held an index from five days earlier, `source_commit()` therefore
# returned a five-day-old commit, and every window `plan` could close ended there. An area whose
# first pull request merged after that commit then had a cursor outside the documented history, and
# the whole plan aborted -- so no progress report was written at all until someone deleted the
# directory by hand.
#
# An hour is far shorter than the deploy cadence and far longer than a single run, so the cache
# still does its actual job (one bootstrap reads hundreds of module pages) while never outliving
# the build it describes by more than a deploy or two.
DOCS_CACHE_TTL = int(os.environ.get("TAUCETI_DOCS_TTL", "3600"))

# `<div class="decl" id="Full.Name">` opens a declaration; the `gh_link` inside it names the commit,
# file and lines. Both are doc-gen4's own markup, so this is reading a published format rather than
# guessing at one -- and `declarations()` fails loudly if the markup stops matching.
_DECL_RE = re.compile(r'<div class="decl" id="([^"]+)">')
_GH_LINK_RE = re.compile(
    r'<div class="gh_link"><a href="https://github\.com/[^/]+/[^/]+/blob/([0-9a-f]{40})/([^"#]+)#L(\d+)-L(\d+)"'
)
# doc-gen4 puts a source link in every module page's navigation, including pages with no
# declarations. Those pages still need a build identity: otherwise a newer, empty HTTP 200 page
# could be mistaken for an empty page from the build selected by source_commit().
_NAV_LINK_RE = re.compile(
    r'<p class="gh_nav_link"><a href="https://github\.com/[^/]+/[^/]+/blob/([0-9a-f]{40})/[^"#]+">'
)
_KIND_RE = re.compile(r'<span class="decl_kind">([a-z ]+)</span>')


class DocsError(RuntimeError):
    """The documentation could not be read, or does not look like doc-gen4 output."""


class DocsNotFound(DocsError):
    """The site answered, and the page does not exist (HTTP 404).

    `facts.collect` checks this against the chosen source commit before deciding whether the
    missing page is benign. A 404 from another build is not evidence that the module is absent.
    """


class Docs:
    """A cached reader for one published documentation site."""

    def __init__(self, base=DOCS_BASE, cache_dir=None, opener=None, ttl=None, accept_older=None):
        self.base = base.rstrip("/")
        self.cache_dir = pathlib.Path(
            cache_dir or os.environ.get("TAUCETI_DOCS_CACHE") or "/tmp/tauceti-docs-cache"
        )
        self.ttl = DOCS_CACHE_TTL if ttl is None else ttl
        self._opener = opener or self._fetch
        self._index = None
        self._pages = {}
        self._source_commit = None
        # `accept_older(page_commit, source_file, build_commit) -> bool`: may a page built at an
        # earlier commit stand for the build? See `declarations`. None refuses every such page.
        self.accept_older = accept_older

    # ----- transport -------------------------------------------------------------------------

    def _fetch(self, url):
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            # `HTTPError` is a `URLError`, so it has to be caught first. Only a 404 says the page is
            # absent; a 403, 429 or 5xx says nothing about the page and everything about the request.
            if exc.code == 404:
                raise DocsNotFound(f"{url} does not exist (HTTP 404)") from exc
            raise DocsError(f"fetching {url} failed: {exc}") from exc
        except urllib.error.URLError as exc:
            raise DocsError(f"fetching {url} failed: {exc}") from exc

    def _fresh(self, path):
        """Is a cached file young enough to reuse? A missing or unreadable one never is.

        The age is required to be non-NEGATIVE as well as small. An mtime in the future -- a corrected
        clock, or a shared filesystem whose skew runs the other way -- otherwise reads as "fresh" for
        as long as the skew lasts, which is the immortal-cache outage rebuilt out of arithmetic.
        """
        if self.ttl <= 0:
            return False
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            return False
        return 0 <= age < self.ttl

    def _store(self, path, text):
        """Put `text` at `path` so a concurrent reader sees either the old file or the whole new one.

        Several workers share one cache directory. `write_text` truncates first, so a reader arriving
        mid-write finds a fresh mtime on a partial file: a half-written index raises a JSON error, and
        a half-written module page parses as ZERO declarations, which reads as an honest "nothing was
        documented here" and is silently reported as such. Write a sibling temp file and rename.
        """
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.cache_dir, prefix=".tmp-")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(text)
                os.replace(tmp, path)
            except OSError:
                pathlib.Path(tmp).unlink(missing_ok=True)
                raise
        except OSError:
            pass  # a cache that cannot be written is not an error

    def _get(self, rel, refetch=False):
        """Fetch `rel` relative to the docs root, memoised in process and on disk.

        The disk cache is keyed by URL and is an optimisation for the bootstrap case, which reads many
        module pages. It expires after `ttl` because the site itself does not stand still -- see
        DOCS_CACHE_TTL for the outage that taught us. Expiry alone does NOT make a run coherent, since
        two URLs cross their TTLs independently; `declarations` is what enforces one build per run.

        `refetch` bypasses both caches, for re-reading a page that turned out to be from a stale build.
        """
        if rel in self._pages and not refetch:
            return self._pages[rel]
        path = self.cache_dir / rel.replace("/", "__")
        text = None
        if not refetch and self._fresh(path):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                text = None  # unreadable or vanished under us: fall through to the transport
        if text is None:
            text = self._opener(f"{self.base}/{rel}")
            self._store(path, text)
        self._pages[rel] = text
        return text

    # ----- the declaration index -------------------------------------------------------------

    def index(self):
        """`{full_name: {"kind", "docLink"}}` for every declaration the site documents."""
        if self._index is None:
            try:
                data = json.loads(self._get(INDEX_PATH))
            except json.JSONDecodeError as exc:
                raise DocsError(f"{INDEX_PATH} is not JSON: {exc}") from exc
            decls = data.get("declarations")
            if not isinstance(decls, dict) or not decls:
                raise DocsError(f"{INDEX_PATH} has no declarations map")
            self._index = decls
        return self._index

    def module_of(self, name):
        """The module page a declaration lives on, as a site-relative path, or None."""
        entry = self.index().get(name)
        if not entry:
            return None
        link = entry.get("docLink") or ""
        return link[2:].split("#", 1)[0] if link.startswith("./") else None

    # ----- module pages ----------------------------------------------------------------------

    @staticmethod
    def _page_commit(html):
        """The build commit named by a module page's source links, if any.

        doc-gen4 writes a source link in the navigation even when the page has no declarations.
        Declaration links and the navigation must name the same build when both are present.
        """
        nav = _NAV_LINK_RE.search(html)
        decl = _GH_LINK_RE.search(html)
        if nav and decl and nav.group(1) != decl.group(1):
            raise DocsError("a module page has source links from different builds")
        link = nav or decl
        return link.group(1) if link else None

    def declarations(self, module_page):
        """Every declaration documented on a module page.

        Returns `{full_name: {"kind", "url", "file", "start", "end", "commit"}}`. A declaration with
        no `gh_link` (there are a few, for compiler-generated entries) is reported without a source
        position, and callers simply cannot decide whether it is new.

        Every page is checked against the build this reader has already committed to. Expiry is
        per-URL, so without the check a run can straddle a deploy: the probe page expires and fixes
        `source_commit()` at the NEW build while another page is still served, in-TTL, from the old
        one. `facts.collect` then reads line spans from one build and blames them at the other's
        commit, which shifts a declaration's span onto whatever now occupies those lines -- reporting
        work that did not land in the window, or missing work that did, with the cursor advancing past
        it either way. A disagreeing page is re-fetched once; if the site is still moving, refuse.
        """
        html = self._get(module_page)
        if self._source_commit is not None:
            seen = self._page_commit(html)
            if seen != self._source_commit and not self._unchanged(seen, module_page):
                html = self._get(module_page, refetch=True)
                seen = self._page_commit(html)
                if seen is None:
                    # A successful response can be an empty module page from a newer build. With
                    # no source link it cannot be tied to the build chosen by source_commit(), so
                    # accepting it would let a nonempty but incomplete report advance the cursor.
                    raise DocsError(
                        f"{module_page} has no source link to verify its build against "
                        f"{self._source_commit[:7]}; refusing an incoherent documentation window"
                    )
                if seen != self._source_commit and not self._unchanged(seen, module_page):
                    raise DocsError(
                        f"{module_page} was built from {seen[:7]}, not {self._source_commit[:7]}, and its "
                        f"module has changed since; the site is redeploying and this run cannot "
                        f"describe one build"
                    )
        out = {}
        marks = [(m.start(), m.group(1)) for m in _DECL_RE.finditer(html)]
        if not marks:
            return out
        for i, (pos, name) in enumerate(marks):
            end = marks[i + 1][0] if i + 1 < len(marks) else len(html)
            seg = html[pos:end]
            gh = _GH_LINK_RE.search(seg)
            kind = _KIND_RE.search(seg)
            out[name] = {
                "kind": (kind.group(1).strip() if kind else ""),
                "url": f"{self.base}/{module_page}#{name}",
                "commit": gh.group(1) if gh else None,
                "file": gh.group(2) if gh else None,
                "start": int(gh.group(3)) if gh else None,
                "end": int(gh.group(4)) if gh else None,
            }
        return out

    def _unchanged(self, page_commit, module_page):
        """Does a page built at `page_commit` still describe the build? Since TauCeti#9636 the docs
        are built incrementally: a run re-analyzes only the modules whose oleans changed, so a page
        for a module untouched since an earlier build keeps that build's source links. Such a page is
        exact for the current build provided the module's source file is unchanged in between, which
        only git can say; `accept_older` (given by `facts`, which has the checkout) says it."""
        if not page_commit or self.accept_older is None or not module_page.endswith(".html"):
            return False
        return bool(self.accept_older(page_commit, module_page[: -len(".html")] + ".lean", self._source_commit))

    def _marker_commit(self):
        """The commit in the deployed tree's SOURCE_SHA marker, read fresh, or None for a site that
        has none (built before the marker existed)."""
        try:
            text = self._get(SOURCE_SHA_PATH, refetch=True)
        except DocsNotFound:
            return None
        sha = text.strip()
        if not _SHA_RE.match(sha):
            raise DocsError(f"{SOURCE_SHA_PATH} does not hold a commit: {sha[:60]!r}")
        return sha

    def source_commit(self, probe_module=None):
        """The TauCeti commit the published documentation was built from.

        Read from the site itself rather than assumed, because the docs deploy independently of the
        branch that nominates them: at the time of writing the published build was several commits
        behind the branch tip. Reporting links against the branch tip would then produce dead links
        for anything newer, so the commit stated by the documentation is what everything is anchored
        to.

        The deployed tree's SOURCE_SHA marker when there is one. A module page's source links are the
        fallback, for a site without the marker: once the docs are built incrementally, a page names
        the build that last re-analyzed its module, which can be any earlier one (on 2026-09-30 the
        live site's pages named four different commits).
        """
        if self._source_commit is None:
            marked = self._marker_commit()
            if marked is not None:
                self._source_commit = marked
                return marked
            module = probe_module or self._any_module()
            for info in self.declarations(module).values():
                if info["commit"]:
                    self._source_commit = info["commit"]
                    break
            else:
                raise DocsError(f"no source link found on {module}; cannot date the documentation")
        return self._source_commit

    def _any_module(self):
        """Some module page, for probing the build's source commit."""
        for name in self.index():
            page = self.module_of(name)
            if page:
                return page
        raise DocsError("the declaration index names no module pages")
