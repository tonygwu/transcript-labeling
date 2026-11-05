# Contextual transcript review

The workflow turns original transcript text into tentative speaker suggestions,
then lets a person confirm speakers and correct wording. Original text fixes
the word coordinates throughout. Model guesses and human decisions remain
separate in storage and exports.

This is a model of the repository's **current source implementation**, not a
deployment record. It covers preparation, one configured model attempt, exact
turn alignment, suggestion updates, browser review and exports. Authentication
internals, provider implementations, live installations and real-data accuracy
are outside this inspection. All payload examples are invented.

Start with the [overview](views/README.md#1-from-original-conversation-to-reviewed-labels)
and then the [source and provenance view](views/README.md#2-preserve-source-model-evidence-and-human-edits).
Both have generated diagram sources and adjacent data cards. The canonical
record is [model.jsonl](model.jsonl); the view files are derived from it.

A caller supplies original text or an existing item plus speaker identities.
The parser preserves source text, caption labels and intervals. Preparation
removes non-speech headers from the model prompt while retaining original word
indices. It excludes saved labels, previous suggestions, focus passages and
caption corrections. The prepared request stays in a private file.

The runner sends only the prompt, response schema and selected model to an
explicit adapter executable. It makes one attempt, keeps raw output and error
records, and caches a successful result. The adapter must supply its own
credentials, account selection, model transport and isolation. The package
does not enforce that external process's tool or network policy. The model
component in the diagrams therefore describes an uncertain caller dependency
with a bounded inference contract, not a verified provider installation.

The resolver checks each proposed start against exact source words in its
marker window. Missing, ambiguous, reordered or altered anchors fail. A valid
response becomes speech-only token ranges with source, prompt, response and
model identity evidence. Missing served-model telemetry remains explicitly
unverified. Structural alignment does not establish attribution accuracy.

A separate import or update command attaches the tentative bundle. Existing
items use a current-bundle digest under the dataset lock. An identical retry
is a no-op; a competing replacement fails. Only suggestion fields change, and
previous bundles remain available for resolving recorded confirmation hashes.
New confirmations keep range-level hashes, so a later edit cannot replace their
evidence. Older annotation-level evidence is marked `legacy_annotation` and
does not prove which draft produced a particular range. Missing historical
hashes remain absent across later saves; history cannot reconstruct them.

The page groups speech by displayed speaker and attribution source. Human
marks take precedence. Original caption headers remain inspectable, and a
turn inside a caption retains its original interval. Corrections replace only
the reading over an anchored range. Browser-authenticated saves create human
answers; agent suggestion updates do not. Exports preserve these distinctions.

| Contract | Source evidence |
|---|---|
| Original text and speech coordinates | [`formats.parse`](../src/transcript_labeling/formats.py), [`tokens.item_kinds`](../src/transcript_labeling/tokens.py) |
| Prompt allowlist and exact anchor resolution | [`contextual.build_request` and `resolve_response`](../src/transcript_labeling/contextual.py) |
| One attempt, raw receipts and cache identity | [`contextual_cli.run_adapter`](../src/transcript_labeling/contextual_cli.py) |
| Guarded suggestion replacement and retained history | [`suggestions.update_suggestions`](../src/transcript_labeling/suggestions.py) |
| Human-only saves and reveal policy | [`service.post_answer` and `transcript_view`](../src/transcript_labeling/service.py) |
| Readable turns and correction overlays | [`span_editor.js`](../src/transcript_labeling/page/span_editor.js), [`corrections.py`](../src/transcript_labeling/corrections.py) |
| Human versus tentative export origin | [`export3.export_dataset`](../src/transcript_labeling/export3.py) |

The cache key includes adapter arguments, not the adapter executable's bytes.
Version those arguments or choose a new work directory when changing adapter
code. Exact anchors can reject an otherwise plausible answer; callers must
report that failure rather than invent ranges. Provider isolation and model
identity require evidence from the selected adapter. Real attribution quality
requires a separate comparison with reviewed speech.

Existing deterministic source-tag imports remain supported. A caller can
adopt request preparation and exact resolution before changing its existing
review storage. See [the compatibility instructions](../docs/CONTEXTUAL.md#migrate-existing-callers)
and [the deployment procedure](../docs/DEPLOY.md). This architecture work
changes neither a caller nor a live service.

Validation uses:

```sh
python3 "$ARCH_SKILL_DIR/scripts/architecture.py" validate architecture/model.jsonl --source-root . --json
python3 "$ARCH_SKILL_DIR/scripts/architecture.py" bundle architecture/model.jsonl --output-dir architecture/views --renderer none
```

The validator returned `"valid": true`, `"errors": []`, and `"warnings": []`.
The bundle contains two views and reports `"renderer_used": "none"` with
`"renders": []`. No PNG rendering or model-accuracy claim is implied. The
synthetic suggestion and human-annotation examples also passed the package's
respective validators. Independent review remains recorded in
[REVIEW.md](REVIEW.md).
