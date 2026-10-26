# Specification

The transcript labeling service is one always-on server on the owner's Mac. A
coding agent creates a dataset, uploads transcripts, and sends the owner a login
link. The owner labels in the browser. The agent reads the labels back. This
file says what the service stores, what it promises, and why some rules exist.
[API.md](API.md) lists the calls, [DEPLOY.md](DEPLOY.md) the install, and
[HISTORY.md](HISTORY.md) where the code came from.

## Projects, datasets, items

- **A project** is a group of datasets with one privacy setting and a list of
  writers (the repositories allowed to write it).
  - `local-only`: its data lives under `$TL_HOME/data/<project>/`, so one
    sandbox rule on `$TL_HOME` covers every local transcript.
  - `git-private`: its datasets live in a private git checkout that the project
    names. Making one checks that every fetch and push remote of that checkout
    is a private GitHub repository, and refuses otherwise. Only `tl project
    create` makes one; neither the HTTP API nor MCP can.
- **A dataset** is one labeling task. Its `manifest.json` (schema version 3)
  holds the modes, the speakers list, the unclear entry, the mixed answer, an
  optional checklist, the reveal policy, an optional media link template, the
  file names, and each item's text hash and token count.
- **An item** is one transcript: its whitespace tokens, pinned by the text's
  sha256 and token count, plus its text when the source had one, mark times,
  caption segments, header ranges and model suggestions.
- **A focus passage** is a part of an item the owner should look at first, with
  an optional title, why and question.

Every offset is an end-exclusive index into an item's whitespace tokens. A save
whose hash or token count differs from the item's is refused.

## Modes

- `speakers`: who said which words. The owner marks word ranges with a speaker
  from the dataset's list, or as unclear. A passage's result is derived from the
  ranges on every save and every read; no button sets it, and a direct write of
  a result is refused. A passage covered by more than one speaker gets the
  dataset's mixed answer. Unmarked words leave a passage incomplete, never
  uncertain; only an unclear range says the owner was unsure.
- `captions`: what was actually said. A correction overlays the original tokens,
  so a change in word count never moves a speaker range. The page sends the
  revision it saw; a stale revision is refused, and the history is appended by
  the server. Original text is never changed.
- `checklist`: questions about each item, from the dataset's checklist. Fields
  with options take one key press.

Model suggestions are drawn dashed and count only when the owner confirms them,
which saves them with `origin: model_confirmed`.

## Blind first

Under `reveal: after_answer`, an item's suggestions stay on the server until the
owner has marked at least one range on it and asks to see them. The ranges
marked before the reveal are kept as the blind answer, and nothing can remove
that record. The bearer token cannot reveal, so an agent cannot break blindness.
An item's ranges saved while its suggestions were on screen are marked
`assisted` (the mark is on the item's whole set of ranges), and exports keep
it, so blind and assisted results can be reported apart.

## Storage

```
$TL_HOME/
  config.json  token (0600)  registry.json
  data/<project>/<dataset>/      local-only datasets
  state/<project>/<dataset>/     .tl.lock, events.jsonl, staging folders
  exports/<project>/<dataset>/   default export target
  logs/
<dataset folder>/
  manifest.json  catalog.json  items/<group>/<name>.json
  human_answers.json   labels, attribution, spans, corrections (verbatim's version-2 format)
  review.json          notes, done marks, reveals, Finish
```

- The answers file keeps verbatim's format byte for byte, so verbatim's own code
  and its recorded hashes keep working.
- Every save takes `flock` on `state/.../.tl.lock`, not on the answers file,
  because git and `os.replace` swap that file's inode.
- A page's writes carry the version of each file it loaded. The store refuses a
  write when something outside the server changed the file since that read,
  such as a `git pull` or a revert, and the page stops saving and asks for a
  reload. Saves made through the server, including another tab's, are not
  outside changes; see Known limits.
- Temp files get unique names. An item upload is staged under `state/` and
  renamed into place, with `catalog.json` written last.
- `events.jsonl` gets one line per browser write: time, kind, id, principal,
  login kind and the file's sha256 after the write. It never holds text.
- Transcripts reach the service only through `--from-file` (or an MCP tool's
  `file`), read with the caller's permissions. Apart from `tl init`, which makes
  the home's config and token, the only files `tl` and the MCP server write are
  exports: under `$TL_HOME/exports/`, or at `--out`.

## Commits in git-private projects

The service commits a git-private dataset's label files itself, after 5 minutes
without a save, at least every 30 minutes during a sitting, and right after
Finish. It never pushes. It commits only in a checkout whose other git workflow
has opted in to a shared lock (`git config tl.syncLock`), and it takes that lock,
`<git dir>/tl-sync.flock`, for each commit. Without the marker it saves files and
leaves committing to the checkout's owner. Before each commit it checks the
branch, that no rebase, merge or cherry-pick is running, that nothing else is
staged and that its paths are not in conflict. It commits with `--only` and
never `--no-verify`. On failure it unstages only its own paths and retries
later. `tl status` and `tl doctor` show waiting saves and the last git error.

## Security

The server binds to 127.0.0.1 and checks the `Host` header. Agents need the
bearer token in `$TL_HOME/token`, which only the owner can read. The browser gets
a session from a one-time link, and the page sends it in a header, not a cookie.
Pages are empty shells that fetch their data with that header, so no transcript
text is in any page another local account could fetch. The Content-Security-Policy
allows only this origin's scripts and connections.

## Rules that exist because something broke

- **Drive the page in a browser after any change to its JavaScript.** The page
  tests run the scripts under node against a stub page, with no layout and no
  real clicks. In verbatim, a module-level `store` shadowed a parameter of the
  same name and every save died with `store.put is not a function`; `node
  --check` passed it, and a browser found it in one keystroke. The suite's SHADOW
  check refuses that shape. The browser check (`uv run --group browser pytest -m
  browser`) drives the real page in Chromium, in light and dark, at phone width.
  It is outside the gate because it needs Playwright, so a commit that changes
  the page's JavaScript or CSS says whether it ran.
- **A page that has not seen an outside change cannot save.** The first store
  design compared file fingerprints taken at each read. It failed review twice:
  a page that loaded before an outside change could still save over it. Each
  page now carries the version it loaded.
- **`assisted` means suggestions were on screen.** In verbatim it was true for
  every save until 2026-10-18, including blind sessions; the old answers were not
  rewritten.
- **An item write under saved answers refuses changes that would move them.** In
  verbatim, a rebuild dropped every quote of a session and stranded six quote
  results, and a changed transcript turned a finished result stale. Here a changed text, changed header ranges, or an
  answered passage that is dropped or covers other words is refused, and the
  check runs on the manifest as re-read under the lock, because an early version
  used a copy read before the lock.
- **Fixtures are synthetic.** Two reviews found real names: once in test
  fixtures, once in a docstring example. The repository is public; invent names.
- **Text exports are written in place.** A temp-file-and-rename writer would
  replace a symlink inside a repository that points outside it with a regular
  file in the repository, which the text guard allows because the link's target
  is outside.
- **The sync lock is held by a helper process, never by git.** An early version
  passed the lock's file descriptor to git, so a hook's background job could keep
  the lock after the commit ended.
- **One broken dataset never stops the server.** A restart checks every
  committing dataset for labels that were saved and not committed; an early
  version stopped at the first dataset whose folder was missing.

## Known limits

- A second tab still showing an older copy of an item replaces that item's
  ranges, checklist label or note when it saves, because its saves go through
  the server. Caption corrections are protected by their revision check. Keep
  one tab per item.
- One person per machine can label, because the server binds to loopback.
- An agent running as the owner can read the token, mint a link and redeem it,
  so `checked_by` is trustworthy against other macOS users and web pages but not
  against the owner's own agents.
- The text guard does not see a pipe: `tl export ... | tee file` gets past it.
- A crash in the middle of an upsert can leave an item file whose hash differs
  from the manifest. The page refuses that item and `verify` reports it.
- A symlink inside a checkout that is pointed somewhere else after its dataset
  was registered is followed by later saves; the path checks run at registration.
- An agent's `git commit --amend` right after a label commit folds the labels
  into its own commit; the content survives, the message no longer says so.
- The event log is not rotated; `tl status` reports its line count.
- Five of verbatim-index's Astra callers (its leaderboard grader, resolver,
  extractor, dater and market matcher) run outside its judge sandbox, under
  Codex's read-only policy. That policy refuses their shell commands every
  network connection, so they cannot reach the port, but it lets them read
  every file the owner can, including `$TL_HOME`'s token, local-only
  transcripts and labels. In verbatim's stored records (measured 2026-10-26
  UTC), 9% of their calls that record a count ran a shell command: none of
  the leaderboard grader's 308, about one in ten of the resolver's and one in
  five of the dater's. The commands that were kept read only skill files and
  their own working folders, and every attempt to fetch something failed. Until verbatim-index's backlog item VI-58 is resolved, no verbatim
  dataset is registered here, and any local-only project is readable by them.
