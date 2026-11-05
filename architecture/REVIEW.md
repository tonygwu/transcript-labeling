# Architecture review ledger

The artifact under review is `architecture/model.jsonl` and its generated
`architecture/views/` bundle, together with `architecture/REPORT.md`. It models
the current source workflow and makes no deployment or model-accuracy claim.

Independent evidence and flow reviews returned `ship`. The operations review
returned `ship-with-notes`; its one documentation finding is resolved below.
These verdicts concern source contracts, not deployment or model accuracy.

| Review responsibility | Status | Findings and dispositions |
|---|---|---|
| Evidence: component existence, lifecycle and source references | Ship | No separate architecture finding. Components and symbols checked against source. |
| Flow and data: direction, schemas, source coordinates and retained state | Ship after correction | Added per-range suggestion and bundle hashes, range versus legacy annotation evidence, and absent-history limits to the canonical entities, examples and generated cards. |
| Operations and model boundary: caller isolation, attempts, caching and identity | Ship with note resolved | Explicitly assigned provider retries, request/token/cost budgets and usage evidence to the caller supervisor and adapter. A subprocess attempt is not a provider-request or spending cap. |

The author ran semantic validation and generated the two view bundles without
a renderer. The validator reported `"valid": true`, `"errors": []` and
`"warnings": []`. Synthetic examples were validated with
`suggestions.validate_bundle` and `spans.validate_annotation`.

The flow finding was should-fix and challenged `human-annotations`,
`export-document` and the report. Evidence came from `humanRanges`,
`confirmDrafts`, and the span/segment export projections. The revised cards
explain that retained history cannot reconstruct missing historical links.

The operations finding was should-fix and challenged `caller-model` and
`invoke-adapter`. `run_adapter` constrains elapsed time and a process invocation;
it cannot inspect an arbitrary adapter's provider requests. The canonical
invariant and retry note now state that limit explicitly. No unresolved
architecture findings remain.

The associated implementation reviews caught and resolved process cleanup,
interrupted raw-file overwrite, repeated historical-evidence inheritance and
quadratic suggestion counting. Independent verification of the corrected
source returned:

```text
uv run pytest
786 passed, 7 deselected in 120.52s (0:02:00)

uv run --group browser pytest -m browser
7 passed, 786 deselected in 11.25s
```

The browser reviewer inspected all 22 retained synthetic screenshots and the
README image. The evidence reviewer ran five isolated mutation probes; the
flow reviewer ran eight, including reversions of its fixes. The corrected
production sources passed and each intended mutation was caught.
