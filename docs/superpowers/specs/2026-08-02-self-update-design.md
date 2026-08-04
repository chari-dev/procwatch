# Self-update from GitHub, and one-press cleanup — design

Date: 2026-08-02

## Goal

1. Procwatch can say "a newer version exists", and install it from
   https://github.com/chari-dev/procwatch in one or two clicks from the
   dashboard.
2. A Mole-style (github.com/tw93/Mole) one-press Clean up: refillable
   caches plus what removed applications left behind, itemised, confirmed
   once, moved to the Trash.

## Constraints

- Stdlib only ("Requires macOS and Python 3.9+. Nothing else").
- Two install shapes exist: a git checkout, and the generated single-file
  `procwatch.py` bundle. Both must be updatable.
- `config.VERSION` is the one place the version is written down; the check
  compares against the same constant on `main` in the repo.
- Mutating endpoints go through the existing CSRF guard.

## Approach chosen

Fetch `procwatch/config.py` raw from GitHub `main` and parse `VERSION`.
No dependence on tags or GitHub Releases (none exist), no API rate limits
worth worrying about, one HTTPS GET.

Rejected: GitHub Releases API (repo cuts no releases); `git ls-remote`
(useless for bundle installs, and a commit hash cannot say "newer").

## Components

### `procwatch/selfupdate.py` (new)

- `mode()` — `"bundle"` when this module's `__file__` carries the
  `<procwatch bundle>` marker, `"git"` when the checkout root has `.git`,
  else `"none"`.
- `check(force=False)` — returns `{current, latest, newer, mode, error,
  checked_ts}`. Result cached in memory for 6 hours; `force` bypasses.
  Network failure → `error` string, `newer` false. Never raises.
- `apply()` — returns `{ok, mode, from, to, restart, error}`.
  - git: `git fetch origin main` + `git merge --ff-only origin/main` at the
    checkout root. A dirty or diverged tree fails with git's own message.
  - bundle: download raw `procwatch.py` from `main`, refuse it unless it
    compiles and carries a VERSION newer than ours, then atomically replace
    the running file (`os.replace`, mode 0755 preserved).
  - Always ends with `restart: true` on success: the running server keeps
    executing old code, so the UI says "quit and reopen Procwatch".

### `procwatch/server.py`

- GET `/api/upgrade` → `selfupdate.check(force=params)` — read-only, also
  how the page learns the current version.
- POST `/api/upgrade` → `selfupdate.apply()`, added to the POST whitelist so
  the CSRF guard covers it.

### `procwatch/static/index.html`

- On load and every 6 hours, the page asks its own server (`/api/upgrade`,
  never relayed to a peer) and, when `newer`, shows an "Update · vX.Y.Z"
  pill in the toolbar.
- Click one arms it ("Install vX.Y.Z?"), click two POSTs the update; the
  pill reports progress and the banner says "Updated — quit and reopen
  Procwatch" (or the error).
- Settings sheet gains a row: current version, "Check for updates" button
  (forces a fresh check), and the same install button when one is found.

### `tools/bundle.py`

`selfupdate` joins MODULES (after `config`) so the single-file build carries
it.

## One-press cleanup

Built on what `space.py` already guards: `caches()` (the safe list, measured
now) and `trash()` (Finder move, refusals, per-path truth). New:

- `space.orphans(installed_idents=None)` — leavings of applications no
  longer installed. Matched by bundle identifier only (reverse-DNS, ≥3
  components), only in recreatable folders: `~/Library/Caches`, `Logs`,
  `Saved Application State`. Skips `com.apple.*`, installed identifiers, and
  anything sharing an installed identifier's first two components (a
  vendor's helpers are not orphans). A wrong match costs a cache, never
  data — Application Support and Containers are deliberately excluded.
- `space.cleanup_plan()` — caches group + orphans group, path-deduped, with
  a total.
- GET `/api/cleanup` returns the plan; POST `/api/cleanup` recomputes it
  server-side and trashes exactly that (a crafted body cannot widen it),
  reporting per-path results and bytes freed.
- UI: "Clean up…" in the burger menu → itemised confirm (the uninstall
  pattern) → one POST → toast with bytes moved. The disk sheet's existing
  per-cache buttons stay.

## Testing

`tests/test_selfupdate.py`, unittest + mock like the rest of the suite:
version parsing/comparison, check caching and failure paths, bundle apply
(atomic replace, refusal of garbage), git apply (subprocess mocked).

## Out of scope

Auto-restart of the server after update; CLI `upgrade` command; signature
verification beyond "compiles and is newer" (the download is HTTPS from the
canonical repo).
