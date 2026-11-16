# Contextual text correction evaluation

This evaluates whether a model proposes the smallest supported name correction
and leaves correct or ambiguous names alone. The public cases use
only invented names and a synthetic self-introduction. Private examples, model
requests, responses, and results must remain outside every Git checkout.

The three model-visible cases live in `cases.py`. Expected replacements live
only in `scoring.py`. Request preparation does not import the scorer. All cases
also require a correct speaker range over the unchanged original tokens.

Prepare private requests without making model calls:

```sh
uv run python -m evals.text_corrections.runner prepare --out-dir /private/tmp/text-eval/requests
```

Run each prepared request through an explicitly selected and approved isolated
adapter using `tl contextual run`. Obtain the required execution-plan review
before a model batch. Keep the model away from this checkout, expected answers,
tools, local files and network search. Retain the exact response envelope for
each case as `known_name.json`, `correct_name.json`, and `ambiguous_name.json` in
a private response directory. Inspect the full raw response and served-model
evidence before relying on the score. Selecting a model does not establish the
served identity when the transport does not report it.

```sh
uv run python -m evals.text_corrections.runner score \
  --responses /private/tmp/text-eval/responses --model SELECTED_MODEL
```

The scorer reports attempted, succeeded, failed and error categories. It rejects
missing cases, malformed responses and reported model substitutions. It reports
unverified model identity explicitly. It makes no provider requests and performs
no retries, truncation, sorting, semantic repair or fallback.

`uv run pytest tests/test_text_proposals.py -q` exercises the evaluator with
synthetic responses: empty corrections fail the known-name case; the exact
single-word change passes; applying that change to either control fails. It
also checks source preservation, accept/reject, human-edit conflicts, proposal
history and export. Browser tests check both transcript views and reloads.
These deterministic receipts test the application and scorer, not model quality.
