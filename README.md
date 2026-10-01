# TauCetiProgress

Progress reporting for [Tau Ceti](https://github.com/TauCetiProject/TauCeti): what has actually
been achieved on each roadmap, written for a human to read in a minute.

Each roadmap directory in
[TauCetiRoadmap](https://github.com/TauCetiProject/TauCetiRoadmap) carries two generated files:

- **`STATUS.md`** — a snapshot, rewritten whole on each update. It says which parts of the roadmap
  are done and sketches the frontier, headed by the commit it describes.
- **`PROGRESS.md`** — an append-only log. Each section covers one window of merged PRs as a few
  holistic paragraphs, emphasising named theorems rather than listing every PR.

New `PROGRESS.md` sections are announced in the **Tau Ceti > Progress logs** Zulip topic.

## Why this repo exists

The rubrics-and-machinery split of
[TauCetiReview](https://github.com/TauCetiProject/TauCetiReview), applied to reporting: the
prompts and the tooling live here, the output lands in TauCetiRoadmap, and
[TauCetiWorker](https://github.com/TauCetiProject/TauCetiWorker) drives it.

The design rule is that **a model only ever writes prose**. Every decision — whether an update is
due, which roadmap it covers, which PRs are in the window, and what mathematics actually landed —
is made by tested Python before any model starts, and the git and pull-request work afterwards is
done by tested Python too.

## The commands

```
tauceti-progress due                      is an update due? (one API call, no clone)
tauceti-progress plan   --roadmap-dir DIR pick the roadmap and the PR window
tauceti-progress facts  --plan FILE       what declarations actually landed in the window
tauceti-progress check  --plan FILE ...   check the model's two bodies before anything is committed
tauceti-progress apply  --plan FILE ...   write the files, open the PR (resumable)
tauceti-progress announce --section FILE  post a new section to Zulip (idempotent)
tauceti-progress docs-commit              the commit the published documentation describes
```

`due` is the only one that runs often; it exits 75 ("no progress") when nothing is due, matching
the worker's convention. `plan` runs at most once a day.

### Choosing the roadmap

`plan --strategy busiest` (the default) waits until no report has landed anywhere for 8 hours, then
takes the roadmap with the most PRs in its window, if it has at least 10. With dozens of roadmaps
this starves the quieter ones.

`plan --strategy threshold` decides per roadmap instead. With N the PRs in a roadmap's window and T
the days since its last report (since its README was added, if it has never been reported), a
roadmap qualifies when N > 0 and one of these holds: it is declared complete (archived under
`Completed/`), N + T > 10 (`--threshold`), or it is not yet assessed, meaning it has no report, or
its report carries no coverage header bound to the README as it now stands (for it and each
sub-roadmap). The merge gate's six-hour interval per roadmap still applies. Among the qualifying
roadmaps the one with the largest N + T wins. There is no project-wide cadence, so this suits a
worker that polls for work: `--table FILE` records every roadmap's standing, including when the next
one will qualify, and `--label-cache FILE` keeps each area's merged PRs between runs, so a run makes
one search for what merged since the last instead of a query per area.

`check` holds a report to the prompt's own limits (750 and 300 words, the two status headings), and
refuses a documentation link that was not copied from the facts or the previous report, one whose
declaration is no longer on its page, a layer the previous report assessed that is now
`unassessed` against the same README, and a report that assesses no layer at all. The writing model
is meant to run it and fix what it reports.

## The window cursor is a SHA, on the docs-tracking branch

A window is the half-open commit range `(from_sha, to_sha]` on TauCeti's **`docgen`** branch, where
`from_sha` is the `to_sha` of the previous `PROGRESS.md` section.

`docgen` nominates the most recent commit on `main` whose API documentation has been published, and
the window ends at **the commit the published documentation actually reports** — read from the site
itself, since the deploy is independent and the branch can sit ahead of it. Ending at the branch tip
instead would record a cursor covering work the report never described, and because the next window
starts after that cursor, the work in between would never be reported at all.

The cost is latency: a report describes the project as of the last published docs build rather than
the tip. That is the right trade for a document whose whole purpose is to be read, and the header
records the exact commit, so nothing is misdated.

Links are not computed by this project at all, and neither are declaration names. Both are read from
doc-gen4's own published output — the declaration index and each module page, which carry the exact
name, kind, source file and line range — so a link resolves because it was read from the page it
points at. `git blame` over those line ranges is what decides whether a declaration belongs to the
window.

There is deliberately no Lean parser here. Qualifying a name correctly means resolving `namespace`
against `section`, `end`, `open ... in` and `_root_`, and many real declarations (projections,
constructors, `deriving` output) are never written in the source at all. An approximation gets most
names right, which is the worst outcome available: the wrong ones are indistinguishable from the
right ones, and a link built from a wrong name is a plausible dead link. PR numbers come from the squash-merge commit
subjects in that range and are attributed by their `roadmap/<Area>` label.

Wall-clock time is used only for display. A cursor made of timestamps would be wrong: a worker
clock running fast advances it past PRs whose merge times then fall *before* the stored cursor, and
those PRs are never reported at all.

## The coverage header

A status snapshot may carry a second machine header beside `tauceti-status:v1`:

```text
<!--tauceti-status:v1 {"roadmap":"EllipticCurves","to_sha":"…","ts":"…"}-->
<!--tauceti-coverage:v1 {"layers":[{"id":"Layer 0","remaining":"Weil reciprocity","state":"partial"},{"id":"Layer 1","state":"done"},…],"readme_sha":"…","roadmap":"EllipticCurves","to_sha":"…"}-->
# Status: EllipticCurves
```

It is the report's verdict on each layer of the roadmap, in a form a script can read; the prose
says the same things, but nothing can aggregate prose across forty roadmaps. The consumer is the
TauCeti site's Progress page (`scripts/roadmap_progress.py` in the TauCeti repository).

**Wire schema.** `roadmap` is the status header's area, or `Area/Child` for a sub-roadmap (below);
`to_sha` equals the status header's; `readme_sha` is the SHA-256 of the README the layers were read
from; `layers` is a non-empty list (at most 64) of `{"id", "state", "remaining"?}` with `id` a short
label (`Layer 3`, `Lane G`, `L0A`), `state` one of `done`, `partial`, `untouched`, `unassessed` (the
material said nothing), and `remaining` an optional one-line note of at most 200 characters with no
angle brackets. No other keys anywhere. Keys are sorted and the JSON is compact;
`files.require_coverage` is the one definition, and both the worker and the gate run it.

**Trust boundary.** The model supplies only the assessments, as JSON inside a fenced
```` ```coverage ```` block at the end of its status prose. Code supplies everything else: `plan`
records the README's layer headings (`Layer` / `Lane` / `Part` / `Stage`, or `L0A`-style labels)
and `readme_sha` in the plan; `apply` removes the block, validates the payload against the schema,
checks it names every listed layer exactly once and nothing else, and writes the header; the gate
treats the headers as part of the canonical prefix (the lines after the status header, nowhere else)
and re-validates them. The gate proves the header's shape, never its truth, and cannot check
`readme_sha` (it never checks out the roadmap repository): the header is a claim, not a certificate.

**Missing block.** A README with no layer headings gives a plan with no layers and a report with no
header. When the plan lists layers, the worker refuses a body with no block, just as it refuses a
block that is malformed, not JSON, or does not fit the plan: a new report changes the report hash,
which retires the site's hand transcription of the old report, so publishing it headerless would
turn assessed layers into unassessed ones. The model always has `unassessed` for a layer it cannot
judge. The gate is deliberately more lenient and still accepts a status file without the header,
which is what every report written before the header existed looks like.

**README hash.** `readme_sha` binds an assessment to the specification it was made against: layer
ids alone do not, since a layer's requirements can change under an unchanged heading. The consumer
refuses a header whose hash does not match the README it reads and shows those layers as
unassessed with that reason.

**Umbrella areas.** An area whose directories include sub-roadmaps (RepresentationTheory's twelve)
is one labelled area with one report, so that report is the only account of its children. A
sub-roadmap is a directory directly below the area with a `README.md` and a `Suggested.lean`, the
consumer's rule. `plan` lists each one with layer headings under `sub_roadmaps`, with its
`Area/Child` id, its README path and its own `readme_sha`; the model reads those READMEs and answers
with a JSON object keyed by roadmap id instead of an array; and `apply` writes one coverage header
per sub-roadmap, each bound to its own README, after the area's own header (if its README has
layers) and in ascending order of name. The gate holds each to the same schema, requires
`Area/Child` with an alphanumeric child directly below the status file's own area, caps them at 32,
and treats all of them as part of the canonical prefix. A consumer that does not know sub-roadmap
headers ignores them: their `roadmap` never equals a row's own name.

**Rollout.** Merge here, then bump the two pins in TauCetiRoadmap's `progress-*.yml` workflows and
the worker's `PROGRESS_REF` to the same SHA, together: the generator and the gate must run one
version. Reports written before the bump simply have no header.

**Leaving the hand transcriptions.** Today the site reads layer states from a hand-transcribed
file in the TauCeti repository (`scripts/roadmap_coverage.json`), each entry bound to the exact
report it was read from. Nothing is backfilled: an area's transcription stays valid until its next
report, which after the bump carries the headers (the worker refuses one without them when there
are layers), and the site then reads those instead and the transcription is retired. That holds
for the RepresentationTheory children too, all at once, at the umbrella's next report, provided
the site reads sub-roadmap headers by then. Each entry can be deleted from the transcription file
once its roadmap's report carries a header.

## Trust boundary

`STATUS.md` and `PROGRESS.md` are **machine-owned, and their prose is not security-validated**.

The merge gate proves the *shape* of a generated update — its paths, its cursor, that it is a
byte-exact append, and that the head being merged is the head that was validated. It cannot prove
that the prose is true. Anyone may open a Tau Ceti PR whose description contains prompt-injection
text; once that PR merges legitimately, its description reaches the writing model.

The mitigations reduce the risk and are not claimed to remove it: the model is grounded in
mechanically-extracted declaration names rather than author prose, PR bodies are delimited and
size-capped, reserved `tauceti-*:v1` markers are rejected in model output, and Zulip mentions are
defused. Read these two files as a machine's summary, not as reviewed roadmap content.

**The blast radius is two markdown files AND a Zulip message.** Every merged section is posted to
**Tau Ceti > Progress logs** automatically, so accepted prose reaches an audience outside the
repository. The post is treated as data -- mentions and bare `#123` linkifiers are defused, the
message is size-capped, it links both the appended log and the current roadmap status, and it is
idempotent on a stable per-window id -- but it is a second sink and the threat model has to say so.

## Licence

Apache-2.0.
