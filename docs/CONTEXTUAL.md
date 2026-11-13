# Contextual speaker review

This workflow proposes speakers from the whole conversation, checks each turn
against the original words, and lets a person correct the result. A proposal
never becomes a human label until the person confirms or changes it.

New speaker reviews use this workflow by default. `new` creates the dataset,
but withholds its login link until contextual suggestions cover every item's
speech. Preparing a request does not execute inference. The agent must
explicitly run an approved model, validate its response, attach its suggestions
and check readiness before asking the owner to review.

The original text is the coordinate system. Whitespace tokens include source
headers and timestamps, but only speech tokens can receive contextual
suggestions. Grouping captions on the page does not change the text, token
positions, original intervals, or correction overlays.

## Create the review before handing it over

```sh
tl new project/review --from-file /private/data/original.txt --create-project \
  --name example --group calls --speakers "jane=Jane Roe:1,sam=Sam Poe:2"
```

Without a ready contextual bundle, this creates `calls/example`, exits 0, and
prints `Review staged; do not send a link yet.` The equivalent MCP `new`
response has no `send_the_owner_this_link`. Both return readiness information
so the agent can identify missing contextual coverage. If a validated bundle
is already available, pass `--suggestions PRIVATE_FILE` or MCP
`suggestions_file` when creating the review.

For MCP callers, the complete sequence is:

1. Call `new`, then `contextual_prepare` with the staged item's key and a
   private file describing every speaker's identity and role.
2. Run the prepared prompt through an explicitly selected, approved model and
   transport. Use `tl contextual run` or an external runner. No MCP preparation
   tool performs this call for you.
3. If the external runner has not already resolved the result, call
   `contextual_resolve` with the retained request and response. Inspect the raw
   response and model identity evidence before using the bundle.
4. Call `suggestions_set` with the validated bundle and the suggestion bundle
   digest captured during preparation. Repeat these steps for every item.
5. Check `status.readiness.ready`, call `url`, and send the successful link.
   After the owner finishes, export the labels and run `verify`.

If no model or transport has been approved, ask for that choice before sending
private text. If execution, identity validation, alignment or attachment fails,
report the error and keep the dataset staged. Never invent suggestions, fill
everything with uncertainty just to pass readiness, or switch to manual mode
to hide a failed model run. Genuine model uncertainty is a valid suggestion.

## Prepare a private request

Keep transcripts, requests, model responses, suggestion bundles and exports
outside every Git checkout. The commands refuse file outputs inside Git,
including ignored paths. The model receives original speech and supplied
speaker identities. Existing suggestions, focus passages, corrections and
human labels are excluded by an explicit field allowlist.

Write a private speaker identity file, using exactly the dataset's actual
speaker ids, excluding its separate uncertain entry:

```json
[
  {"id":"jane","description":"Jane Roe, interview candidate"},
  {"id":"sam","description":"Sam Poe, interviewer"}
]
```

Describe each person's identity and conversational role using known recording
metadata. For example, explain who is interviewing whom; a UI label such as
`Me` alone does not identify that person to the model. Do not infer identities
from transcriber tags. Names and roles are configurable; neither interviews
nor two speakers are hardcoded. Review labels and keyboard shortcuts are
configured separately. The default uncertainty entry is `unclear`, labeled
`Unsure`, with key `u`.

```sh
tl contextual prepare --from-file /private/data/original.txt \
  --speakers-file /private/data/identities.json --unclear unclear \
  --out /private/data/request.json
```

For an existing item, use `--item-file /private/data/item.json` instead. This
preserves its source geometry. The authenticated `GET /api/v1/item` route
returns `{item, config, suggestion_bundle_sha256}`; write only its `item` field to that file. MCP
`contextual_prepare` reads the existing item and writes the request privately,
returning its path and hashes rather than text. It checks the speaker IDs
against the dataset, returns `model_executed: false`, and includes the current
`suggestion_bundle_sha256` for the later update. Save that digest. This is a
prepared request, not model output.

The request contains all speech. There is no length cap, chunking, source-tag
prior, or minimum turn length. Markers occur every 20 speech words; each turn
uses an exact eight-word anchor. Both are explicit prepare options and part of
the request hash. An anchor at the end contains only the remaining words.
Anchors may cross subsequent speaker boundaries to reach eight words.

## Select and run a model adapter

The package has no provider dependency, credentials, account routing, or
default model. An adapter is an executable argument list saved in a private
JSON file. It receives this object on stdin:

```json
{"prompt":"the prepared whole-transcript prompt","response_schema":{},"requested_model":"chosen-model"}
```

It must return this envelope on stdout, preserving the model response string:

```json
{
  "response":"{\"turns\":[{\"marker\":0,\"first_words\":\"exact starting words\",\"speaker\":\"jane\"}]}",
  "requested_model":"chosen-model",
  "served_model":null,
  "model_identity":"unverified",
  "transport":{"receipt":"private transport evidence"}
}
```

`transport` is optional. If response telemetry independently reports the
served model, set `model_identity` to `reported` and `served_model` to that
model. A reported model substitution is rejected. Selecting a model in a CLI
argument alone does not establish which model served the request.

Adapters must isolate the model from held-out references, other local files,
tools and network search. The generic runner launches the selected executable
without a shell; it does not enforce the provider's tool policy or sandbox.
The caller also owns provider retries and request, token and spending limits.
One adapter subprocess attempt is not proof of one provider request or a cost cap.
Audit and probe that adapter before sending private data. It must fail if it
cannot enforce its declared isolation or if unexpected tool calls occur.

```sh
tl contextual run --request /private/data/request.json --model chosen-model \
  --adapter-file /private/data/adapter-argv.json --work-dir /private/data/runs \
  --timeout 1200
```

One invocation makes at most one adapter attempt. The timeout is explicit.
On timeout the runner sends SIGTERM, allows five seconds for adapter cleanup,
then kills its remaining process group. An adapter that creates separate
process groups must stop them in its SIGTERM handler. CLI cancellation uses
the same cleanup path. Interrupted raw files reserve their attempt numbers
and cannot be overwritten by a retry.
There are no automatic retries, fallback models, semantic repairs or output
caps. A failed attempt retains stdout, stderr and its error category; an
explicit rerun creates a new attempt. A successful request is cached by the
request hash, selected model and adapter arguments. Keep adapter arguments
versioned when changing the adapter implementation. A cache hit revalidates
the raw response and derived bundle without a model call.

The runner prints attempted, succeeded, failed, cached and error-category
counts. Run larger jobs under a detached supervisor with its own explicit
budget and retry policy. Inspect one full raw response and model telemetry
before scaling. Structural validation is not evidence of speaker accuracy.

If another application already runs the model, it can use
`tl contextual resolve --request ... --response ... --model ... --run-id ...
--out ...` or import `build_request` and `resolve_response` from
`transcript_labeling.contextual`. Raw artifacts allow offline re-resolution.

MCP `contextual_resolve` provides the same offline validation. It takes
`project`, `dataset`, absolute private `request_file` and `response_file` paths,
the explicitly selected `model`, and `run_id`. Optional `out` chooses the private
bundle path; otherwise the tool writes under the dataset's private exports
directory. It returns the path, range count and selected versus reported model
identity. It performs no model call. A reported model mismatch is rejected;
`unverified` means the transport did not establish the served model's identity.

## Validate and attach suggestions

The resolver requires a unique, case-sensitive, punctuation-sensitive anchor
within the named marker window. It rejects missing or repeated matches,
invalid speakers, invented markers, omitted initial speech, duplicate or
out-of-order starts and extra response fields. It never clamps or sorts
boundaries, guesses from a marker alone, or silently matches altered words.
Each turn is split around source metadata into speech-only token ranges.

An importable bundle has this shape:

```json
{
  "schema_version":1,
  "text_sha256":"64 lowercase hex characters",
  "token_count":123,
  "ranges":[{"start":4,"end":9,"speaker":"jane","source":"contextual_model"}],
  "provenance":{
    "method":"contextual_speaker_turns",
    "requested_model":"chosen-model","served_model":null,"model_identity":"unverified",
    "prompt_sha256":"64 lowercase hex characters",
    "request_sha256":"64 lowercase hex characters",
    "response_sha256":"64 lowercase hex characters","run_id":"private-run-id"
  }
}
```

New item specs accept this under `suggestions`, mutually exclusive with
`suggest_map`. The latter remains a deterministic source-tag mapping and uses
`source: segment_label`; it neither runs inference nor makes a contextual review
ready. Existing items use a separate operation:

```sh
tl suggestions set project/review --item calls/example \
  --from-file /private/data/suggestions.json \
  --expected-bundle-sha256 CURRENT_DIGEST
```

Use `suggestion_bundle_sha256` from preparation or `GET /api/v1/item`. The update checks source
hash, token count, speaker ids, ordered non-overlapping speech ranges and the
current bundle digest, including provenance, while holding the dataset lock. A competing
update fails and requires inspection; do not force an overwrite. Repeating the
identical completed update is a no-op. Only the
item's suggestion fields change. Earlier bundles are retained so human
confirmations can still refer to the suggestions they saw. Human answers,
corrections, notes, reveal events, focus passages and review completion state
remain unchanged. History is never sent to the browser, including blind mode.

New human confirmations retain both the range-list hash and bundle hash on
each range, with suggestion_evidence_scope: range. Older annotations kept only
an item-level suggestion hash, which a later edit could replace. Inherited
historical hashes use suggestion_evidence_scope: legacy_annotation and do not
prove which draft produced a specific older range. Missing hashes stay absent;
retaining draft history cannot reconstruct those missing historical links.

## Review and export

New speaker reviews default to `review_mode: contextual` and `reveal: always`.
After suggestions are attached, inspect readiness and request the link:

```sh
tl status project/review --json
tl url project/review
```

`readiness` reports the mode, whether contextual suggestions are required,
whether the dataset is ready, a reason, and each item's speech and contextual
word counts. The URL operation checks the entire dataset, even for an item
link, because the owner can navigate to other items. If coverage is missing,
the dataset is empty, or an item has no speech, it refuses with HTTP 409.
Only send a successfully returned link. Existing
browser sessions are not revoked by this check.

Choose `--review-mode manual` or MCP `review_mode: "manual"` for intentional
manual labeling without a model prerequisite. Choose `blind` for independent
review with optional suggestions hidden by `reveal: after_answer`. Under that
policy the owner can reveal after saving at least one range; this does not mean
the whole item has been reviewed. If `new` receives an explicit `after_answer`
without a review mode, it selects blind mode for compatibility. Manual mode
defaults to `always`. Neither manual nor blind mode requires contextual
coverage before a link is issued.

Conversation view groups consecutive speech by its displayed speaker and
provenance. Human marks take precedence. Source labels only group otherwise
unattributed speech; they are not confirmed identities. Expand a turn's
source captions to inspect literal original timestamps and headers, or use
Source captions view. Speaker changes inside a caption retain that caption's
original interval. Missing end times remain missing. A turn boundary never
creates a new timestamp. Caption corrections remain separately recorded.

Both views add 45 seconds of surrounding dialogue on each side of the saved
passage window, including for existing reviews. Boundaries include complete
captions. Without source segments, each timestamp and its following words stay
together. Where timing is unavailable, the display adds 120 spoken words per
side instead. These are display choices; no stored passage or target offsets
are rewritten.

**Show more before** and **Show more after** add another 45 seconds from the
displayed edge, independently. A pause longer than that still reveals the next
caption. The controls indicate when the transcript's beginning or end is reached.
Your selection and reading position stay in place, and each excerpt keeps its
own expanded window during the page session. **Expand to full transcript** is
still available. Underlined words remain the review target; other words provide
context. Bulk confirmation always uses the original saved passage window,
including its original context, even in full transcript view. Additional context
does not change quote completion, suggestions, saved labels or text corrections.

Text correction is enabled by default for these reviews, even when `modes`
contains only `speakers`. Existing service-managed speaker reviews gain the
controls without changing their stored modes or answers. If the task
intentionally excludes text editing, use `tl new --no-text-corrections` or
`text_corrections: false` in config or MCP `new`. This choice does not change
speaker suggestions, readiness or reveal behavior.

Imported tags, contextual guesses and human reviews are labeled separately.
Confirming a contextual guess saves `model_confirmed`; confirming an imported
or generic suggestion saves `suggestion_confirmed`; manual marks save `human`.
Existing historical `model_confirmed` ranges retain their original value.
The source's `Unknown`, model uncertainty, and human `Unsure` are distinct.

Export kind `suggestions`, also included in `all`, carries current suggestions,
their source/model/prompt provenance and previous bundles. Filled segment
exports identify suggestion source and digest. Unreviewed suggestions never
appear as human ground truth. `--no-text` omits transcript text and free-form
provenance that could contain it.

## Migrate existing callers

Existing datasets retain their stored reveal policy and human state. They are
not migrated to contextual mode. Low-level dataset creation without a
`review_mode` also retains its earlier speaker suggestion and reveal policies.
New datasets created through `new` default to contextual mode when speaker
review is enabled, so callers
must handle staged creation and complete inference before requesting a link.
Caption and checklist tasks without speaker review default to manual mode.

Existing `suggest_map`, sidecar construction, ranges and correction exports
remain available. A caller that deliberately uses manual or blind review
should select that mode explicitly. No consumer is silently switched to a new
model or prompt, and the old inference implementation is not removed by this
change.

For a version-2 sidecar, supply its original text alongside tokens, hash,
count and header ranges to `build_request`. Preserve the original uncertain
speaker id and subject/other ids when that is the caller's schema. Pass the
prepared prompt to the caller's approved, isolated model transport, then wrap
the raw response and identity telemetry for `resolve_response`. Its `ranges`
can feed `make_item` or the validated HTTP import contract.

The older verbatim-index turn resolver allowed approximate word matching,
marker-only starts and sorted boundaries. This resolver deliberately rejects
those cases. Re-run that caller's synthetic tests and reviewed-data evaluation
before replacing it; historical accuracy claims do not transfer to this prompt.
Its judge isolation must also protect the labeling service and stored labels.

Before changing a consumer, confirm it accepts `suggestion_confirmed`, the
new provenance export, and explicit alignment failures. Migrate legacy
in-place sidecars to a service-owned dataset before using suggestion updates.
Do not rewrite their underlying files through this endpoint. Existing datasets
and human state require no migration to display conversation view.

Deployment remains the tagged-release procedure in [DEPLOY.md](DEPLOY.md).
Working checkouts never install or restart the shared live service.
