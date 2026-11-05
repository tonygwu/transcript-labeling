# Contextual speaker review

This workflow proposes speakers from the whole conversation, checks each turn
against the original words, and lets a person correct the result. A proposal
never becomes a human label until the person confirms or changes it.

The original text is the coordinate system. Whitespace tokens include source
headers and timestamps, but only speech tokens can receive contextual
suggestions. Grouping captions on the page does not change the text, token
positions, original intervals, or correction overlays.

## Prepare a private request

Keep transcripts, requests, model responses, suggestion bundles and exports
outside every Git checkout. The commands refuse file outputs inside Git,
including ignored paths. The model receives original speech and supplied
speaker identities. Existing suggestions, focus passages, corrections and
human labels are excluded by an explicit field allowlist.

Write a private speaker identity file, using the dataset's actual speaker ids:

```json
[
  {"id":"jane","description":"Jane Roe, interview candidate"},
  {"id":"sam","description":"Sam Poe, interviewer"}
]
```

The identities are recording metadata, not a mapping of transcriber tags.
Names and roles are configurable; neither interviews nor two speakers are
hardcoded. Configure the review labels separately as `Me` and `Interviewer`
with keys `1` and `2`, and use the uncertainty entry with key `u`.

```sh
tl contextual prepare --from-file /private/data/original.txt \
  --speakers-file /private/data/identities.json --unclear unclear \
  --out /private/data/request.json
```

For an existing item, use `--item-file /private/data/item.json` instead. This
preserves its source geometry. The authenticated `GET /api/v1/item` route
returns `{item, config, suggestion_bundle_sha256}`; write only its `item` field to that file. MCP
`contextual_prepare` reads the existing item and writes the request privately,
returning its path and hashes rather than text.

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
`source: segment_label`. Existing items use a separate operation:

```sh
tl suggestions set project/review --item calls/example \
  --from-file /private/data/suggestions.json \
  --expected-bundle-sha256 CURRENT_DIGEST
```

Read suggestion_bundle_sha256 from `GET /api/v1/item`. The update checks source
hash, token count, speaker ids, ordered non-overlapping speech ranges and the
current bundle digest, including provenance, while holding the dataset lock. A competing
update fails; repeating the identical completed update is a no-op. Only the
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

Choose `reveal: always` for correction work or `after_answer` for blind review.
Both CLI `new` and MCP `new` expose this choice. Under `after_answer`, the
owner can reveal after saving at least one range; this does not mean the
whole item has been reviewed.

Conversation view groups consecutive speech by its displayed speaker and
provenance. Human marks take precedence. Source labels only group otherwise
unattributed speech; they are not confirmed identities. Expand a turn's
source captions to inspect literal original timestamps and headers, or use
Source captions view. Speaker changes inside a caption retain that caption's
original interval. Missing end times remain missing. A turn boundary never
creates a new timestamp. Caption corrections remain separately recorded.

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

Keep current imports working while replacing their model boundary one caller
at a time. Existing `suggest_map`, sidecar construction, ranges and correction
exports remain available. No consumer is silently switched to a new model or
prompt, and the old inference implementation is not removed by this change.

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
