# Contextual transcript review

This source model describes code in the repository. Existing means implemented in source, not installed or deployed. All data examples are synthetic.

Start with the overview, then choose the question you want to explore.

## 1. From original conversation to reviewed labels

How does source text become tentative speaker turns and then human-reviewed output?

Start with original text and speaker identities. A caller-selected model proposes turns; deterministic code resolves exact words. A separate import attaches proposals. The person reviews the readable conversation, and exports retain the difference between suggestions and human decisions. Existing means implemented in this source, not deployed. The external adapter is an unverified caller dependency.

[Mermaid diagram](sources/overview.mmd) · [FigJam source](sources/overview.figjam.mmd) · [D2](sources/overview.d2)

[Data examples and table schemas](data/overview.html) · [Native canvas card text](data/overview.json)

The diagrams use color and shape for component roles; status words identify what exists or is planned.

- **Prepare original speech** (Program step - fixed rules; existing): Parse source captions without changing text; build a prompt from original speech and supplied identities.
- **Run one configured attempt** (Program step - fixed rules; existing): Launch caller-selected executable arguments without a shell; retain raw receipts and cache successful results.
- **Caller model operation** (Model call - one bounded task; uncertain): Caller supplies a bounded inference adapter that returns exact response text and identity evidence; provider execution and isolation are outside this package.
- **Resolve exact turn starts** (Program step - fixed rules; existing): Require unique exact anchors in marker windows and emit ordered speech-only ranges with hashes and model provenance.
- **Validate and save review data** (Program step - fixed rules; existing): Accept validated tentative bundles and authenticated human edits through separate routes; enforce source identity and stale-write checks.
- **Read and correct the conversation** (Client application; existing): Group displayed speech by speaker and attribution source while preserving original caption intervals and word anchors.
- **Person reviewing the transcript** (Person; existing): Confirm or change speakers, mark uncertainty, and correct wording through the browser.
- **Export labels with their origin** (Program step - fixed rules; existing): Export human labels, correction overlays and tentative provenance separately; fill from suggestions only when explicitly requested.

## 2. Preserve source, model evidence and human edits

Which records change, and which evidence survives a new suggestion or wording correction?

Original text fixes token coordinates. Private inference artifacts retain the prompt and raw response for offline resolution. A guarded item update replaces only tentative fields and saves draft history. Human labels and wording overlays stay separate. Exports expose each origin and the source hashes; no suggestion becomes human truth automatically. New confirmed ranges retain their own draft and bundle hashes. Legacy annotation evidence is marked separately, and absent historical links remain absent after later saves.

[Mermaid diagram](sources/provenance.mmd) · [FigJam source](sources/provenance.figjam.mmd) · [D2](sources/provenance.d2)

[Data examples and table schemas](data/provenance.html) · [Native canvas card text](data/provenance.json)

The diagrams use color and shape for component roles; status words identify what exists or is planned.

- **Prepare original speech** (Program step - fixed rules; existing): Parse source captions without changing text; build a prompt from original speech and supplied identities.
- **Resolve exact turn starts** (Program step - fixed rules; existing): Require unique exact anchors in marker windows and emit ordered speech-only ranges with hashes and model provenance.
- **Validate and save review data** (Program step - fixed rules; existing): Accept validated tentative bundles and authenticated human edits through separate routes; enforce source identity and stale-write checks.
- **Private dataset files** (Saved data; existing): Keep original items, tentative draft history, human answers, corrections and review events in separate private JSON files.
- **Read and correct the conversation** (Client application; existing): Group displayed speech by speaker and attribution source while preserving original caption intervals and word anchors.
- **Export labels with their origin** (Program step - fixed rules; existing): Export human labels, correction overlays and tentative provenance separately; fill from suggestions only when explicitly requested.
- **Private inference artifacts** (Saved data; existing): Retain prepared requests, raw adapter stdout and stderr, attempt records, derived bundles and success markers outside Git.

