# History

This package began as the annotation tool inside
[verbatim-index](https://github.com/tonygwu/verbatim-index), a public repository.
It was extracted on 2026-10-21 (Pacific time) so that any project, not only
verbatim, can have a person label transcripts through one always-on service.

## The extraction point

- **Source snapshot:** verbatim commit `d5a3a8bfaccc61f777837925f2768a38b651dc21`
  on `origin/main`, committed at 2026-10-20 23:12 -0700. Every copied file came
  from this commit.
- **The last verbatim commit that touched `scripts/annotations*` by then:**
  `0e16c19a232529bfecb281b2d194e756d0cc5671`, committed at 2026-10-19 14:28 -0700
  with the subject "annotations: review fixes to the blind answers". It was the
  ninth commit to touch those scripts, counting verbatim commit `1cf7853` itself.
  That commit renamed seven of the files to their `annotations_*` names, so their
  earlier history is under the old names; the other four were created under the
  new names.

A change made in verbatim after `0e16c19a` is not in this package unless it was
ported on purpose. Compare against that commit before porting a later fix.

## What was copied

The first port, commits `05345b0` through `8ba76ab` in this repository, copied the
generic parts of the tool. The table names the files as they stood at `8ba76ab`;
later work renames or replaces some of them.

| Here, at `8ba76ab` | From verbatim `scripts/` |
|---|---|
| `tokens.py` | `token_kinds` and `text_hash` (annotations_spans.py), `char_span_to_tokens` (annotations_select.py) |
| `timing.py` | `play_time`, `mark_times` and `excerpts` (annotations_text.py) |
| `spans.py` | `validate_annotation`, `quote_review`, `reconcile_quote_reviews`, `import_spans` and `sidecar_path` (annotations_spans.py) |
| `corrections.py`, `page/corrections.js` | annotations_corrections.py and annotations_corrections.js |
| `checklist.py` | `answered` and `to_labels` (annotations_page.py), `keep_assisted` (annotations_serve.py) |
| `store.py` | `Store`, `FileStore`, `load_answers` and `dump_answers` (annotations_store.py), `write_atomic` (atomicio.py) |
| `dataset.py` | `Session` and `manifest_errors` (annotations_store.py), `guard_answers` (annotations.py) |
| `export.py`, `verify.py` | the export logic around `export_result`, restructured, and `verify` (annotations.py) |
| `page/page.py`, `page/data.py` | `render_page` and `page_data` (annotations_page.py) |
| `page/template.html` | the `TEMPLATE` string in annotations_page.py, unchanged |
| `page/span_editor.js` | annotations_span_editor.js, unchanged |
| `server.py` | `make_handler` (annotations_serve.py) |
| `testing.py` | `write_session` (annotations_fixtures.py), with a synthetic default project name |

`tokenize` and `build_sidecar` are new code. `build_sidecar` is pinned byte for
byte to a sidecar that verbatim's own code wrote from the same input.

### Deliberate changes in the first port

The port kept verbatim's behaviour except for these deliberate changes:

- `manifest_errors` accepts any non-empty list of project names, because verbatim's
  fixed project vocabulary belongs to verbatim. It also names a malformed part
  instead of crashing on it.
- The labels export takes the checklist from the page's own session block instead
  of verbatim's project table, and refuses a checklist page that carries no spec.
  A label value that is not an object stops the export instead of being dropped.
  The export no longer reads an ArtifactData list or a `source` file.
- `verify` reproduces only the `labels`, `corrections` and `spans` exports unless
  the caller passes exporters for others. verbatim's built-in `quotes` export is
  not here, so verifying a verbatim session whose manifest lists `quotes` fails
  that check without one. verbatim's `--data` check, that every sidecar holds the
  text now in the data checkout, is not here either.
- The server identifies itself as `transcript-labeling/1` instead of
  `annotations/2`.
- The server no longer warns about pages built by verbatim's older page template,
  because only verbatim has such pages.
- Several refusal messages are shorter.

### How the copy was checked

- The converted verbatim tests pass here.
- A page rendered here from synthetic data equalled, byte for byte, a page that
  verbatim's own `render_page` wrote from the same data (commit `da07a5b`). That
  check is retired once the page changes on purpose for multiple projects.

verbatim keeps its own copies of these modules until it moves onto this package.
