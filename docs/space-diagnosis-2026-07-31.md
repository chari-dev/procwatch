# "Where the disk went" — diagnosis, 31 July 2026

> **Status, same day.** Sections 1, 3 and 4 are fixed and shipped. Section 2 is
> fixed as far as it can be: the causes are named, and none of them carries a
> size because none of them *has* a size anything here can measure — a folder
> that refused to be read weighs nothing precisely because it refused. Section
> 5 (coverage: container caches, Docker, simulators via `simctl`, dev caches,
> browsers, other volumes, duplicates, large-and-old) is **not** done and is
> what remains between this and replacing a cleaner.
>
> One defect in section 4 stands unfixed and is not fixable by walking: APFS
> clones are two inodes sharing extents, and `stat` reports full size for both.
> Hard links, which *are* detectable, are now counted once.

Question asked: is this feature finished, and is it a credible alternative to a
Mac cleaner? Answer: it is a better *explainer* than any cleaner and a much
weaker *reclaimer*. Two numbers on this machine say it plainly — it explains
140 GB of a 198 GB data volume, and it offers a button that moves the Trash
into the Trash.

Everything below was measured on this Mac unless marked as read from the code.

## 1. The headline number is wrong

`volumes()` calls `statvfs("/System/Volumes/Data")` and reports
`total - free` as used: **228.4 GB**. The Data volume actually consumes
**198.5 GB** (`diskutil apfs list`). APFS shares free space across the whole
container, so `statvfs` charges this volume for every volume in it:

| Volume | Consumed |
|---|---|
| Data | 198.5 GB |
| Preboot | 12.5 GB |
| System | 12.4 GB |
| VM | 3.2 GB |
| Recovery | 1.5 GB |
| **Total** | **228.1 GB** ≈ the 228.4 GB shown |

So `reconcile()` reports **88.2 GB missing (38.6%)** when ~30 GB of that is
other volumes that can never be scanned and are not the user's to manage. The
honest gap inside the user's own data is ~58 GB.

Fix: take used from the volume's own consumption, and if the container view is
wanted, show it as a separate line with the sibling volumes named.

## 2. The remaining ~58 GB is unexplained, and the code already knows why

- `unreadable` counted **836 directories** on the last scan. It is stored in
  `space_scan`, returned by `latest()`, and rendered **nowhere** — the panel
  never says "836 folders were refused". A count is also the wrong unit: the
  interesting quantity is the bytes behind them, which is never estimated.
- `GUARDED` is a hard-coded list of six paths. Anything else macOS refuses
  reads as an empty folder and weighs zero silently. Confirmed: Mail,
  Messages, Photos library and `MobileSync` all measure 0 from an unprivileged
  process.
- `snapshots()` returns **names only, never sizes** (0 snapshots here, so not a
  factor on this machine — but on a Mac where it matters, the panel names the
  cause and cannot size it).
- Purgeable space is not modelled at all. This is the single biggest reason
  Finder, `df` and About This Mac disagree, and it is the question the feature
  exists to answer.
- Compressed files (decmpfs) report near-zero `st_blocks`; most of
  `/Applications` and `/opt` is compressed, so the scan is biased low in the
  same direction as `missing`.

The reconcile note is the most valuable thing in this feature and no commercial
tool does it honestly. It is currently a lump sum with a vague sentence. It
should be a breakdown: refused folders (with sizes), other volumes in the
container, snapshots (with sizes), purgeable, and only then "unaccounted".

## 3. Safety bugs

- **`~/.Trash` is on the safe-to-clear list** (`space.py:651`) and appears with
  a Trash button — 4.69 GB on this machine. Pressing it asks Finder to delete
  the Trash; on the hand-move fallback it attempts
  `shutil.move(~/.Trash, ~/.Trash/.Trash)`. It should be an "Empty Trash"
  action, not a move.
- `_protected()` allows `~/Library/CloudStorage` (Dropbox, OneDrive, Google
  Drive) and `~/Library/Mobile Documents` (iCloud Drive). Trashing there
  propagates the deletion to the cloud and to every other device.
- `_group_containers()` matches `name.endswith("." + tail)` where `tail` is the
  bundle id minus its first component. For a two-part id such as
  `net.whatsapp`, `tail` is one word, so any `group.*.whatsapp` matches — the
  "never on a substring" rule in the module comment is not kept here.
- `LEAVINGS` name-based rules match `Application Support/<Display Name>`. An app
  whose display name is its vendor takes the vendor folder shared with its
  siblings.
- `_by_hand()` aborts the batch on the first `OSError` and reports that first
  error against every remaining path.

## 4. Accuracy bugs worth fixing

- No inode dedup anywhere (`scan`, `_folder_size`, `_bundle_bytes`, `caches`).
  Hard links are counted once per link. APFS clones are counted at full size on
  both sides — `stat` cannot see shared extents, so the module docstring's
  claim that "a cloned file costs almost nothing" does not hold for the numbers
  this tool prints.
- `biggest_dirs` and `applications` build SQL `LIKE` patterns without `ESCAPE`,
  so `_` is a wildcard: `my_app` also matches `myXapp`.
- `applications()` keys leftovers by app *name*, so two apps of the same name in
  `/Applications` and `~/Applications` share one total; `~/Applications` has no
  owner rule at all and always reports zero leftovers.
- `/Applications/Utilities/*` is attributed to a phantom owner "Utilities".
- `biggest_files` and `biggest_dirs` are served from the stored scan and never
  re-checked against disk, so a deleted file still shows with a Trash button.
- External and network volumes are excluded from the walk and from `volumes()`,
  while the UI says "the disk".

Passed checks: the walk uses `lstat` only, so iCloud dataless files are *not*
materialised (a naive walk would download them and increase usage); symlinks
are not followed; `/System` and `/Volumes` are skipped, so firmlinked `/Users`
is not counted twice; removals only ever move to the Trash.

## 5. Coverage against what people use a cleaner for

Present: app caches (12-entry hand list), Xcode DerivedData / iOS
DeviceSupport / CoreSimulator Devices, `~/.cache`, `~/.npm/_cacache`, pip,
Homebrew cache, `~/Library/Logs`, per-app totals, biggest folders and files,
per-format breakdown, and an uninstaller that takes leftovers — which is the
AppCleaner use case and the strongest part of the feature.

Absent, roughly in order of what it costs a developer:

| Missing | Where | Note |
|---|---|---|
| Sandboxed app caches | `~/Library/Containers/*/Data/Library/Caches` | unreachable from the current safe list |
| Docker / OrbStack disk images | `~/Library/Containers/com.docker.docker/.../Docker.raw`, `~/.orbstack` | needs `docker prune` then host shrink, not `rm` |
| Simulator runtimes / unavailable devices | `~/Library/Developer/CoreSimulator`, `/Library/Developer/CoreSimulator` | must go through `simctl`, not the filesystem |
| Xcode Archives, DocumentationCache, iOS device logs | `~/Library/Developer/Xcode/*` | Archives hold shipped dSYMs — offer, never auto-clear |
| Dev caches | `~/go/pkg/mod`, `~/.gradle`, `~/.m2`, `~/.cargo`, Yarn/pnpm, CocoaPods, SwiftPM, Carthage | `go clean -modcache` needed; modcache is read-only |
| `node_modules` / `.venv` by age | project trees | needs "has a lockfile + untouched for N months" |
| Browser caches and service workers | Safari, Chrome, Firefox profiles | |
| iOS device backups | `~/Library/Application Support/MobileSync/Backup` | may be someone's only backup — report, do not offer |
| Trash on other volumes | `/Volumes/*/.Trashes/<uid>` | |
| Downloads, large-and-old files, duplicates | anywhere | duplicates need content hashing |
| Time Machine local snapshot sizes | `tmutil` | named today, never sized |
| Purgeable / iCloud-evicted space | n/a | the "About This Mac disagrees" answer |

Deliberately not recommended: stripping `.lproj` localisations (breaks code
signatures) and anything that deletes inside a Photos library.

## 6. What to do, in order

1. **P0 — safety:** take `~/.Trash` off the cache list and give it an Empty
   Trash action instead; refuse `CloudStorage` and `Mobile Documents`; tighten
   `_group_containers` to full-identifier matching.
2. **P0 — honesty:** fix `used` to the volume's own consumption; turn the
   reconcile lump into a named breakdown; surface the 836 refused folders with
   an estimate and a Full Disk Access prompt (the button already exists in
   `/api/settings`).
3. **P1 — accuracy:** dedup inodes across every measurement; escape `LIKE`;
   key app leftovers by path, not name; re-check stored rows before offering a
   button.
4. **P1 — coverage:** container caches, browser caches, dev caches, Xcode
   Archives, Docker, simulators via `simctl`, other volumes and their trashes.
5. **P2 — the cleaner features people expect:** large-and-old, duplicates,
   snapshot and purgeable sizing.

The differentiator to keep pushing is item 2. Every cleaner shows a pie chart;
none of them will tell you honestly what they could not see.
