# API

The service listens on `127.0.0.1:<port>` (8790 by default, from
`$TL_HOME/config.json`). It has three kinds of caller:

- **Agents** use `/api/v1/` with the bearer token. Claude Code agents call it
  through `tl`; Codex agents through `tl mcp`.
- **The owner's browser** uses the page routes with a session id it got from a
  one-time login link.
- **Anyone** may read `/healthz`.

Most agents never call HTTP directly. Use `tl` (below) or the MCP tools, which
check arguments and turn every refusal into a message.

## Rules for every request

- The `Host` header must be `127.0.0.1:<port>`, `localhost:<port>`,
  `[::1]:<port>`, or a host listed in `allowed_hosts` in the config. Any other
  host gets 421. This stops a web page that rebinds its DNS name to 127.0.0.1.
- A POST must send `Content-Type: application/json` (415 otherwise). A POST from
  a browser must come from the server's own origin (403 otherwise). No CORS
  headers are ever sent.
- Every response, including every refusal, carries the security headers: a
  Content-Security-Policy that allows only this origin's scripts and
  connections and no framing, `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: no-referrer` and `Cache-Control: no-store`.
- A refusal is JSON, `{"error": "<what to do>"}`, and closes the connection.
  A refused contextual review link also includes `readiness` details.
  An unexpected failure is a 500 that names only the exception's type, because
  an exception's text can quote a transcript.
- A GET must not carry a body.
- Body limits: 8 MiB for an answer save, 256 KiB for other browser writes,
  and 32 MiB for an agent call.

## Agents: `/api/v1/`

Every call sends `Authorization: Bearer <token>`, where the token is the
content of `$TL_HOME/token` (mode 0600). A missing or wrong token gets 401.
`X-TL-Client: mcp` marks a call from the MCP server; such a call may not create
or write a git-private project (403).

Every write names the calling repository as `repo: "owner/name"`. A project
lists the repositories allowed to write it (`writers`). A write from any other
repository gets 409 unless it sends `owner_override: true`, which an agent
should send only when the owner asked for it. The writers list guards against
mistakes; it is not authentication, because a caller can name any repository.

| Call | Body or query | Returns |
|---|---|---|
| `POST new` | `project, dataset, repo, items`; `create_project`, `speakers`, `modes`, `title`, `config` | the dataset, its counts and review readiness. `url` is present only when the review is ready. Missing contextual suggestions leave a successfully created dataset staged without a login link. Local-only projects only. An unknown project is refused unless `create_project: true`. An actual creation failure removes the dataset, and the project if this call made it and it holds no dataset by then; a project that stays is named in the server log. |
| `POST projects` | `name, writers, repo`; `title` | the new local-only project. Git-private projects are made with `tl project create` only. |
| `POST datasets` | `project, dataset, config, repo`; `path` (required for a git-private project) | the dataset's folder |
| `POST items` | `project, dataset, repo, items`; `upsert` | counts. The whole batch is checked before anything is written. An existing key needs `upsert`, and even then a changed text, changed header ranges or a moved or dropped answered passage under saved answers is refused. |
| `GET item` | `project, dataset, key` | `{item, config, suggestion_bundle_sha256}`: original source item and `{speakers, unclear, reveal}`, plus `review_mode` when stored. This agent-authenticated response includes current suggestions and history, even during blind review. Keep it private. |
| `POST suggestions` | `project, dataset, repo, key, suggestions, expected_bundle_sha256` | `{key, changed, suggestions, suggestions_sha256, suggestion_bundle_sha256}`: replace one item's contextual drafts under the dataset lock. Preserve source, catalog, human answers, corrections, notes, reveals, Done and Finish. |
| `POST url` | `project, dataset`; `item` | `url`, a new one-time login link. Refuses an unready contextual review, including when `item` names an individually ready item. |
| `GET status` | `project, dataset` | review readiness, items, items with ranges or corrections, done marks, passages answered, notes, reveals, Finish state, the event count, and the commit state |
| `GET answers` | `project, dataset` | the answers with passage results derived from the ranges, plus `review` (notes, done marks, reveals, Finish) |
| `GET export` | `project, dataset, kind`; `no_text` (`1`, `true`, `0`, `false`), `fill` (`suggestions`) | one export, or all of them for `kind=all` |
| `GET verify` | `project, dataset`; `expect_answers_sha256` | a report of every check; `ok` is true only when all passed |
| `GET schema` | `name`: `item`, `dataset`, `answers` or `export` | the shape of that object |
| `GET projects` | | the registry |

An item spec in `items` is `{name, text}` plus optional `group` (the key is
`group/name`; default `item`), `format` (`zoom-notes`, `bracket-ts` or `plain`;
detected when absent), `row` (catalog fields such as `title` and `person`),
`focus` (passages to look at first, by token or character offsets),
`suggest_map` (`"Label=speaker_id,..."`: imported speaker tags become suggestions),
`suggestions` (the contextual envelope below, mutually exclusive with
`suggest_map`) and `source` (an object kept with the item, such as the file
name; its `format` is always the parser's). `GET schema?name=item` lists every
field.

### Text correction

Speaker and caption reviews include text correction by default, including
`modes: ["speakers"]`. This also applies to existing service-managed reviews.
The stored `modes` list and answers are not rewritten. When text correction is
enabled, the service includes `captions` in the session modes and permits
correction answers.

Optional `config.text_corrections` must be a JSON boolean. `false` disables
text correction even if stored modes include `captions`; `true` enables it,
including for a checklist-only task. Without the setting, checklist-only tasks
keep text correction disabled. Strings, numbers and `null` are refused.
Disabling text correction is also refused if no task would remain, as with
`modes: ["captions"]` and `text_corrections: false`.

Use `tl new --no-text-corrections` to opt out or `--text-corrections` to enable
it explicitly. MCP `new` accepts the boolean directly as `text_corrections` or
inside `config`; if both are supplied, they must agree. This setting controls
text editing independently of contextual, manual or blind speaker review.

### Review modes and readiness

`POST new` chooses `config.review_mode: "contextual"` when the dataset includes
speaker review and the caller has not selected a mode. Contextual reviews use
`reveal: "always"`. Creation does not run a model. Until every item's speech
has validated contextual suggestions, the dataset remains staged and no login
link is returned. A bundle supplied with the new items can make it ready at
creation time.

| `config.review_mode` | Intended use | Suggestions and reveal policy |
|---|---|---|
| `contextual` | Correct model-proposed speakers | Full contextual speech coverage is required before a link is issued; suggestions show immediately. |
| `manual` | Intentionally label without a model prerequisite | Suggestions are optional; the default reveal policy is `always`. |
| `blind` | Make an independent judgment before seeing suggestions | Suggestions are optional and use `after_answer`; reveal requires a saved range. |

If the caller omits `review_mode` but explicitly requests `reveal: "after_answer"`,
`new` selects blind mode for compatibility. Caption or checklist tasks without
speaker review default to manual mode. Incompatible contextual or blind reveal
policies are refused. Existing datasets and the low-level `POST datasets`
operation retain their earlier speaker suggestion and reveal policies when
`review_mode` is absent; there is no automatic migration to contextual review.

Contextual readiness requires source-aligned suggestions with contextual
provenance. Imported tags from `suggest_map` do not satisfy it. Every speech
token needs coverage; the configured uncertain speaker is a valid model
proposal. Coverage does not establish accuracy or human agreement. A request
file alone does not establish that inference ran, and the service cannot
independently attest to a caller's model claim.
An empty dataset or an item without speech remains unready for contextual review.

`status` and `new` include a `readiness` object:

```json
{
  "mode": "contextual",
  "requires_contextual": true,
  "ready": false,
  "reason": "<explanation of readiness>",
  "items": [
    {"key": "calls/example", "speech_words": 24, "contextual_words": 0, "ready": false}
  ]
}
```

`mode` is `contextual`, `manual`, `blind`, or `legacy` for a dataset without a
stored review mode. `url` checks the whole
dataset because an item link still allows sidebar navigation. If new items
are added without contextual suggestions, requesting another link is refused
until they are ready. Existing browser sessions are not revoked by this check.

If inference or validation fails, report that error and leave the review
staged. Never fabricate model provenance, label every word uncertain merely to
pass the check, or switch to manual mode to conceal failure. Manual and blind
modes are choices for the owner's task, not recovery paths for failed inference.

Export kinds: `spans` (the owner's ranges), `segments` (each caption split where
the owner's ranges split it), `focus` (passage results), `corrections`,
`labels` (checklist answers), `notes`, and `suggestions` (current drafts, source
identity and provenance, plus every previous draft bundle). With `no_text`, an export keeps only
a fixed list of structural fields per kind (offsets, ids, speaker ids, origins,
hashes, times, and checklist answers that are yes or no or one of a field's
options), so a field added later stays out until someone lists it. Unmarked words are unknown. Suggestions never become labels
unless `fill=suggestions` asks for it, and then each filled piece says
`origin: suggestion`, `suggestion_source` and `suggestions_sha256`. The source
is `segment_label`, `contextual_model`, or `legacy` when an older draft lacks
one of those source values. Human ranges stay separate: `human` means manually
marked; new `model_confirmed` ranges mean the owner accepted a contextual draft;
`suggestion_confirmed` means the owner accepted another suggestion. Existing
saved origins are never rewritten. Historical `model_confirmed` also covered
imported tags, so the origin name alone does not establish that a model ran.

### Contextual suggestion envelope

The same JSON object works in a new item spec, `POST suggestions`,
`tl new --suggestions FILE`, and the MCP `suggestions_file` argument:

```json
{
  "schema_version": 1,
  "text_sha256": "<SHA256 of the exact original UTF-8 text>",
  "token_count": 4,
  "ranges": [
    {"start": 0, "end": 2, "speaker": "a", "source": "contextual_model"},
    {"start": 2, "end": 4, "speaker": "unclear", "source": "contextual_model"}
  ],
  "provenance": {
    "method": "contextual_speaker_turns",
    "requested_model": "example-model",
    "served_model": null,
    "model_identity": "unverified",
    "prompt_sha256": "<SHA256>",
    "request_sha256": "<SHA256>",
    "response_sha256": "<SHA256>",
    "run_id": "run-1"
  }
}
```

Offsets are end-exclusive whitespace-token positions in the original text.
Ranges must be ordered, non-overlapping and contain speech tokens only. The
resolver splits turns around caption metadata, timestamps and other non-speech
tokens. A range cannot silently claim those tokens. Every speaker must be in
the dataset, including its configured uncertain speaker. Missing speech stays
unassigned and prevents a contextual review link; an uncertain suggestion is distinct from an original Unknown tag
and from the owner's Unsure answer. Ranges may also carry string `turn_id` and
`reason` fields. They cannot carry a human origin.

Every digest must be a lowercase SHA256. Model identity is `reported` only
when transport telemetry supplies a nonempty `served_model`; `unverified`
requires `served_model: null`. A reported model different from the requested
model is refused. Provenance may include a JSON `transport`
object for additional evidence. The service validates provenance shape and
source identity; it does not attest to the truth of a caller's model claim.

Read `GET item` before updating and send its
`suggestion_bundle_sha256` as `expected_bundle_sha256`. If another writer
changed the ranges or provenance, the update returns 409. This bundle digest
includes both; the separate item.suggestions_sha256 still identifies only the
ranges cited by existing human annotations. An exact retry of the already
installed ranges and provenance returns `changed: false` without rewriting
the file or duplicating history. The original text hash and token count are
checked again under the lock. Historical bundles retain their exact suggestion
hashes so existing human confirmations remain traceable. Updating suggestions
does not manufacture an owner event or change the reveal policy.

New confirmations keep suggestions_sha256 and suggestion_bundle_sha256 on each
range with suggestion_evidence_scope: range. Both hashes survive later human
edits and exports. Older annotations kept only an item-level hash, which later
saves could replace while retaining older ranges. When that older hash is copied
onto a range, suggestion_evidence_scope is legacy_annotation. It identifies the
old annotation's evidence, not proven evidence for that specific range. Missing
historical hashes stay absent; history cannot reconstruct the missing link.

Use this operation for an existing contextual item. Full-item `upsert` refuses
items with contextual provenance because it would discard draft history.
Legacy imports using only `suggest_map` keep their existing behavior. The
`suggestions` export is included in `all` for datasets with speaker review.
Its `no_text` form omits source metadata, model names, run identifiers, reasons
and transport details; full provenance exports belong in private storage.

## The browser

| Route | Who | Does |
|---|---|---|
| `GET /`, `GET /login`, `GET /p/<project>/<dataset>/` | anyone | an empty page shell and its scripts; no project name, item or text |
| `GET /static/...` | anyone | the page's scripts and styles |
| `POST /api/login` | anyone | `{code}` from the link's `#code=`; returns `{session, target}`. A used or expired code gets 409 with `target`, so a page that already holds a session can continue and say the link was used. |
| `GET /api/whoami`, `GET /api/projects` | session | the operator id; the projects and datasets for the landing list |
| `GET /p/<p>/<d>/api/page` | session | the page's data |
| `GET /p/<p>/<d>/api/answers` | session | the answers, the review state, and `versions: {answers, review}` |
| `GET /p/<p>/<d>/api/transcript?key=K` | session | one item's transcript; under `reveal: after_answer` its model suggestions stay on the server until the owner reveals them |
| `POST /p/<p>/<d>/api/answer` | session only | `{kind, id, value, versions}`: a span set, a correction or a checklist label. `checked_by` is set by the server. |
| `POST /p/<p>/<d>/api/note` | session only | `{id, text, versions}`: a note on an item or a passage |
| `POST /p/<p>/<d>/api/reveal` | session only | `{key, versions}`: show an item's model suggestions. Under `reveal: after_answer` this needs at least one saved range on the item first. The ranges marked before the reveal are kept, and nothing can remove that record. |
| `POST /p/<p>/<d>/api/done` | session only | `{key, done, versions}` for an item, or `{finish: true, versions}` for the whole dataset |

The session id travels in the `X-TL-Session` header, never in a cookie. Login
links work once, within 7 days; sessions last 30 days. The four writes refuse
the bearer token, so an agent cannot reveal suggestions or answer for the owner.

An answer save replaces one annotation for one recording. Speaker saves include
that recording's complete range list, even when only one passage changed; they
do not replace other recordings, labels or text corrections. Both the service
and the legacy `/s/<session>/api/answer` endpoint accept JSON bodies up to 8 MiB
(8,388,608 bytes). Larger bodies are refused without changing saved answers.
Login, note, reveal and done requests keep their 256 KiB limit; agent requests
keep their 32 MiB limit. Answer refusals drain at most 16 MiB before closing the
connection, so requests beyond that bound may receive a connection reset.

An ordinary failed speaker save keeps the latest edits in the open page; Retry
save resends the complete current range list. These edits are held in memory,
so closing or reloading the page loses edits that have not reached the server.
A restart invalidates the old page's write versions and requires a reload;
Retry cannot bypass that protection.

Every write sends back the `versions` that `api/answers` gave it. If something
outside the server changed the answers or review file since that read (a `git
pull`, a revert), the write gets 409 with `"stale": true` and the page stops
saving and asks for a reload. Another tab's saves go through the server and do
not count, so an older tab replaces what a newer one saved; keep one tab per
item. A write whose answer is saved but whose event log or commit
note then fails still returns 200, with a `warning`.

## `tl`

```
tl init | serve | doctor | mcp
tl new P/D --from-file PATH [--create-project] [--speakers "id=Label:key,..."] [--suggest-map ...]
           [--focus FILE] [--modes speakers,captions] [--name N] [--group G]
           [--config FILE] [--review-mode contextual|manual|blind]
           [--reveal always|after_answer] [--suggestions FILE]
           [--text-corrections | --no-text-corrections]
tl project create NAME --git-checkout ABS_PATH [--writer REPO ...] [--branch B]
tl project list | tl dataset list
tl dataset create P/D --config FILE [--path REL]
tl items add P/D --from-file PATH [--upsert] [--name N] [--group G] [--suggest-map ...] [--focus FILE]
tl suggestions set P/D --item GROUP/NAME --from-file PRIVATE_JSON --expected-bundle-sha256 HASH
tl url P/D [--item KEY] | tl status P/D [--json] | tl wait P/D --until done --timeout SECONDS
tl export P/D KIND [--out FILE] [--no-text] [--fill suggestions]
tl verify P/D [--expect-answers-sha256 H] | tl schema item|dataset|answers|export
```

- A write names the calling repository with `--repo owner/name`, or takes it
  from the `origin` of the checkout `tl` runs in.
- `tl new` stages a contextual speaker review by default. Complete the
  [prepare, model execution, validation and attachment steps](CONTEXTUAL.md)
  before `tl url` can return a link. `--review-mode manual` and
  `--review-mode blind` preserve intentional manual and blind tasks.
  Staged creation exits 0 because creation succeeded and prints
  `Review staged; do not send a link yet.` Requesting its link is refused
  with HTTP 409 until the review is ready.
- `tl wait` exits 0 once the owner pressed Finish and 3 on timeout (2 is a
  usage error).
- Exports of a local-only project go to `$TL_HOME/exports/<project>/<dataset>/`
  unless `--out` says otherwise; a git-private project's go to stdout. Text
  bound for a file, or for a redirected stdout, inside a git work tree is
  refused unless `--no-text`. A pipe gets past that check. Export files are
  readable by the owner only.
- `tl doctor` checks the home, that the running service has the installed
  version and accepts the token, and each git-private project's commit state.
  It exits 1 on any problem.

## MCP tools (`tl mcp`)

`new`, `items_add`, `suggestions_set`, `contextual_prepare`, `contextual_resolve`, `url`, `status`,
`wait`, `export`, `verify`, `schema` and `doctor`. Paths are absolute: `file`,
`focus_file`, `suggestions_file`, `speakers_file`, `request_file`, `response_file`,
`out`, and `repo`, which is the
caller's own checkout. `wait` returns `still_waiting` after at most 50 seconds;
call it again. `export` writes under `$TL_HOME/exports/` and returns the path,
size and counts, never the text. MCP calls cannot write git-private projects.

`new` accepts `config`, `review_mode: "contextual" | "manual" | "blind"`,
`reveal: "always" | "after_answer"`, and boolean `text_corrections`.
Text correction is enabled by default for speaker and caption reviews, even
when `modes` lists only `speakers`. If a direct argument and `config` both
specify a setting, they must agree. Speaker reviews default to contextual
mode. Until ready, the response contains no `send_the_owner_this_link` field.
Do not ask the owner to start labeling a staged correction review.

`contextual_prepare` takes `project`, `dataset`, `item`, and `speakers_file`.
The speaker file is a JSON list of `{id, description}` objects. It must name
exactly the dataset's speaker IDs, excluding the separate uncertain entry, with
clear identity and conversational role descriptions. UI labels such as Me and
Interviewer alone may not identify who those people are. This tool writes a
request from original source to a private file and returns its path and hashes,
including the current `suggestion_bundle_sha256`. It returns
`model_executed: false`, runs no model and includes no human labels or caption
corrections. Optional `out` selects the private destination.

Run the prepared prompt through the owner's approved model and transport.
`tl contextual run` supports an explicit model and adapter; an external runner
can instead retain the response envelope described in [CONTEXTUAL.md](CONTEXTUAL.md).
If no model or transport is approved, obtain that choice before sending private
text. Neither preparation nor successful process exit proves a model executed
correctly. Inspect the full raw response and its reported identity.

`contextual_resolve` takes `project`, `dataset`, private `request_file` and
`response_file` paths, the explicitly selected `model`, and a `run_id`. It validates
the retained model response and writes the suggestion bundle. It does not call
a model. A reported model substitution or invalid source alignment is an error.
Optional `out` chooses a private destination; the default is
`$TL_HOME/exports/<project>/<dataset>/contextual-<request_sha256>-suggestions.json`.
The response contains `path`, `ranges`, `requested_model`, `served_model`, and
`model_identity`, never transcript text.
`tl contextual run` already performs resolution after a successful adapter call.

`suggestions_set` takes `project`, `dataset`, `item`, `repo`, `suggestions_file`
and `expected_bundle_sha256` to install the validated bundle. Use the digest
captured by `contextual_prepare` or `GET item`; a conflicting update requires
inspection, not a forced overwrite. This operation preserves all human edits
and review history. Repeat preparation, execution, validation and attachment
for every item, inspect `status`, then call `url` and send the successful link.
After the owner finishes, use `wait` or `status`, `export`, then `verify`.
