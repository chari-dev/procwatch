# procwatch — per-process history for macOS

**Date:** 2026-07-27
**Status:** Approved design, ready for implementation planning

## Problem

Activity Monitor answers "what is slow right now" and forgets. Nothing on macOS answers
"what ate the CPU at 3pm on Tuesday." Beszel and similar hub/agent monitors record
system-level totals only — CPU 60%, load 68 — which tells you the machine was busy
without naming the process responsible.

The motivating incident: a 61% CPU process and a stray `log stream` running 28 hours went
unnoticed for over a day. A system-level monitor would have shown elevated load the whole
time and never identified either.

## Goal

A local, always-recording, per-process history with a browsable dashboard. Look back at
any past minute and see which processes were consuming CPU and memory. Small enough that
running it never becomes the problem it exists to detect.

## Non-goals

- Remote or multi-machine monitoring. One laptop, local storage, local viewing.
- Alerting. Deliberately deferred; the schema supports adding it later.
- Per-thread, syscall, or I/O attribution. Process-level CPU and memory only.
- Replacing Activity Monitor for live inspection.

## Constraints

- Host: MacBook Air M3, 16 GB RAM, macOS 27.0, ~11 GB free disk.
- Must not become a CPU consumer itself. No resident daemon beyond the sampler.
- Python 3 and sqlite3 ship with macOS. No third-party runtime dependencies.

---

## Architecture

Three components, one direction of data flow:

```
launchd (30s) → sampler.py → SQLite ← server.py → browser (on demand)
```

**Sampler** — a launchd agent firing every 30 seconds. Reads process state, computes
CPU deltas, writes one batch to SQLite, then performs a bounded rollup/prune batch inline.
Rollup happens on the write path so there is no second scheduled job that can fail
silently while the database grows without bound.

**Store** — a single SQLite database at `~/.local/share/procwatch/procwatch.db`, WAL mode.

**Dashboard** — a stdlib `http.server` process bound to localhost, started on demand via
`procwatch open` and exiting after an idle timeout. Not a resident daemon. Promoting it to
an always-on launchd agent is a plist change if that is later wanted.

---

## Measurement

### Data source

One `ps` invocation per tick:

```
ps -Aao pid,lstart,cputime,rss,comm,command
```

Verified available on macOS 27. Note that macOS `ps` does **not** support the `etimes`
column — it prints `ps: etimes: keyword not found` to stderr, drops that column, and still
emits the rest, so elapsed time is derived from `lstart` rather than read directly. Because
the command still succeeds partially, the sampler must validate the header it got back
rather than assuming the columns it asked for are the columns it received. `cputime` is reported at **centisecond** resolution
(`0:50.88`); over a 30-second interval that bounds CPU-percent quantization at ~0.03%,
which is far below the noise floor of anything worth charting. No `libproc`/`ctypes` path
is needed, and the schema stores centiseconds accordingly.

### CPU must be computed from deltas, not read from `ps`

macOS `ps` reports `%CPU` as a **decaying average over up to a minute of previous real
time** (`man ps`), not an instantaneous rate and not a lifetime ratio. Measured on the
target machine: `WallpaperAerialsExtension` reported `%cpu` of 29.9 against a lifetime
ratio of 1.6; `WindowServer` reported 51.5 against 22.1. The two figures are unrelated.

That column is unusable here for three reasons, none of which is "it hides spikes":

1. **The averaging window is opaque and unstable.** It is "up to a minute," varying with
   process age, and it is not aligned to our 30-second sample interval. Two consecutive
   samples cover overlapping, unknowable spans of real time.
2. **It does not reconcile.** `man ps` states outright that the sum of all `%cpu` fields
   can exceed 100%. A per-process history whose rows do not sum to a system total cannot
   support the `__other__` remainder row or any stacked chart.
3. **It is not integrable.** "Average CPU between 14:00 and 15:00" is meaningful only over
   samples of known, equal duration. A decaying average cannot be summed across buckets,
   which makes the rollup weighted-mean arithmetic invalid at the source.

The sampler instead reads **cumulative CPU time** per PID each tick and computes:

```
cpu_percent = (cputime[t] - cputime[t-1]) / (wallclock[t] - wallclock[t-1]) * 100
```

This yields a true rate over a known interval — additive, integrable, and reconcilable
against the system total.

**The interval is known to the second, not exactly.** Timestamps are stored as integers, so
both endpoints of a delta are truncated and the measured interval can differ from the real
one by up to a second. Because the rate is inversely proportional to the interval, a 30-second
tick therefore carries up to ±3.3% error. Simulated against realistic launchd jitter the
error is zero-mean with a standard deviation of 0.5–1.4%, so it cancels under the rollup's
sample-weighted averaging rather than accumulating.

It does not cancel in `cpu_max`, which takes a maximum rather than an average: a recorded
spike's magnitude can be overstated or understated by up to 3.3%. That is well inside the
precision anyone reads a CPU chart to, and removing it would mean carrying sub-second
timestamps through every tier — a schema-wide cost for a rounding error smaller than the
sampling interval's own aliasing.

Because launchd respawns the sampler per tick (see Sampler lifecycle), the previous tick's
readings live in the `sampler_state` table, not in memory. `(pid, start_time)` is the key:
PIDs are recycled, and a PID whose `lstart` changed between ticks is a different process,
so its delta is discarded rather than computed against a stranger's CPU clock.

### Identity: exe plus normalized arguments

Grouping by `comm` alone would merge `log stream --predicate "DUPLEX-TRACE"` with every
other `log` invocation, dissolving the culprit into a generic bucket.

Truncating the command line to a fixed prefix fails the same test from the other
direction. macOS `argv[0]` is a full bundle path — `/System/Library/Frameworks/WebKit.
framework/Versions/A/XPCServices/com.apple.WebKit.WebContent.xpc/Contents/MacOS/…` runs
past 120 characters before reaching a single argument, so every such process would collapse
into one identical truncated prefix. The `log` example only survives truncation because
`/usr/bin/log` happens to be short.

Identity is therefore computed, not truncated:

- `exe` — `basename(argv[0])`.
- `args_sig` — remaining arguments joined, with absolute paths reduced to basenames and
  volatile tokens (bare PIDs, port numbers, UUIDs, temp paths) masked to a placeholder.
  Capped at 100 characters *after* normalization, so the cap falls on arguments rather
  than on a path prefix.
- Identity is the pair `(exe, args_sig)`, interned in `proc`.

This keeps 27 browser renderers as one row with `nproc = 27` (their argv differs only in
masked volatile tokens) while keeping two `log` invocations with different predicates
apart. The **full, untruncated** command line of the first PID observed under each identity
is also stored in `proc` for display — the table is interned, so the cost is bytes per
distinct identity, not per sample.

### Aggregating PIDs that share an identity

Both CPU and RSS are **summed** across the PIDs sharing an identity, never averaged. A row
answers "what did this thing cost the machine," and 27 renderers at 200 MB each cost 5.4 GB
of memory, not 200 MB. Averaging would make a fleet of processes indistinguishable from a
single one, which is the opposite of what the `nproc` column exists to reveal. `nproc` is
recorded alongside so the per-process figure remains derivable.

`cpu_max` for a multi-PID identity is the max over *ticks* of the summed value, not the sum
of per-PID maxima — the latter would compound the same non-co-occurrence error that rules
out stacking maxima in the dashboard.

Parsing note: `lstart` is a five-token field (`Fri Jul 24 22:17:35 2026`) sitting between
other columns, so the `ps` output must be parsed positionally. Splitting on whitespace will
silently misalign every column to its right.

### What is recorded each tick

- The union of the **top 40 by CPU** and **top 40 by RSS** (~50–60 distinct entries).
- Any entry matching the user's **watchlist**, recorded even at zero, so a process can be
  tracked continuously without ranking.
- All remaining processes collapsed into a single synthetic `__other__` row carrying summed
  CPU and RSS. Without this the chart silently fails to reconcile to the system total and
  the viewer cannot tell whether a gap is missing data or genuinely idle processes.
- System-wide totals, always complete: load average, CPU user/sys/idle, memory used,
  compressor pages, swap used, free disk.

---

## Storage

### Schema

```sql
proc (
  id            INTEGER PRIMARY KEY,
  exe           TEXT NOT NULL,      -- basename(argv[0])
  args_sig      TEXT NOT NULL,      -- normalized args, volatile tokens masked, <=100 chars
  cmdline_full  TEXT NOT NULL,      -- untruncated, first PID seen under this identity
  UNIQUE (exe, args_sig)
);

watchlist (
  pattern    TEXT PRIMARY KEY,      -- regex matched against exe and cmdline_full
  added_ts   INTEGER NOT NULL
);

-- Sleep and downtime, recorded rather than smoothed over.
gap (
  ts_start   INTEGER NOT NULL,      -- last good sample before the gap
  ts_end     INTEGER NOT NULL,      -- first sample after it
  reason     TEXT NOT NULL,         -- 'sleep' | 'agent_down' | 'clock_jump'
  PRIMARY KEY (ts_start)
) WITHOUT ROWID;

-- One table per tier, identical shape.
-- sample_raw | sample_fine | sample_coarse | sample_archive
sample_<tier> (
  ts          INTEGER NOT NULL,     -- unix seconds, bucket START
  proc_id     INTEGER NOT NULL REFERENCES proc(id),
  cpu_avg     INTEGER NOT NULL,     -- tenths of a percent
  cpu_max     INTEGER NOT NULL,     -- tenths of a percent
  cpu_max_ts  INTEGER NOT NULL,     -- when within the bucket the max occurred
  rss_avg     INTEGER NOT NULL,     -- KB
  rss_max     INTEGER NOT NULL,     -- KB
  nproc       INTEGER NOT NULL,     -- PIDs sharing this identity
  samples     INTEGER NOT NULL,     -- source samples in this bucket; weights the rollup
  PRIMARY KEY (ts, proc_id)
) WITHOUT ROWID;

system_<tier> (
  ts        INTEGER PRIMARY KEY,    -- same tiering, one row per bucket
  ...,                              -- load, cpu user/sys/idle, mem, swap, disk
  samples   INTEGER NOT NULL,       -- source samples actually present
  expected  INTEGER NOT NULL        -- samples the bucket's duration implies
);

sampler_state (
  pid         INTEGER PRIMARY KEY,
  start_time  INTEGER NOT NULL,     -- from lstart; guards against PID reuse
  cputime_cs  INTEGER NOT NULL,     -- centiseconds, as ps reports it
  updated_ts  INTEGER NOT NULL
);
```

CPU is stored as an integer in tenths of a percent and RSS as integer KB rather than as
REAL. This is not micro-optimization: it removes 12 bytes per row across roughly 2.3
million rows, and `WITHOUT ROWID` tables store the primary key inline rather than
duplicating it into a separate index.

### Retention tiers

| Tier | Resolution | Window | Ticks | Rows (~60/tick) |
|---|---|---|---|---|
| `raw` | 30 s | 7 days | 20,160 | 1.21 M |
| `fine` | 5 min | 30 days | 8,640 | 0.52 M |
| `coarse` | 1 hour | 1 year | 8,760 | 0.53 M |
| `archive` | 6 hour | forever | 1,460/yr | 88 k/yr |

Total ~2.26 M rows at ~33 bytes: **~75 MB steady state, growing ~3 MB per year, forever.**

**This is a best case, not a bound.** The ~60-rows-per-bucket figure holds exactly for the
`raw` tier, where a bucket is one sample. In rolled-up tiers a bucket holds the *union* of
distinct identities observed across its source samples — a 1-hour coarse bucket spans 120
ticks, and every short-lived process that entered the top 40 during that hour earns a row.
On a machine with heavy process churn (build systems spawning compilers, 848 resident
processes) the rolled tiers can run 2–3× the naive estimate, putting the realistic ceiling
nearer **150–200 MB**. Still immaterial against 11 GB free, but the implementation should
report actual size via `procwatch status` rather than trusting this table.

The windows are chosen so intervals divide evenly into each other and align to clock
boundaries, which keeps chart buckets from drifting. Repeated halving (30s → 60s → 2m →
4m → 8m…) was considered and rejected: it degrades without bound, produces intervals that
do not align to any human time unit, and requires re-processing historical data on a
schedule.

**Judgment call on the `raw` window.** The user asked for 30 days before coarsening. This
design coarsens at day 7 instead, because `cpu_max` and `cpu_max_ts` survive the rollup —
a spike three weeks old is still visible at its true magnitude in a 5-minute bucket, and
`cpu_max_ts` still names the minute it peaked. What is lost past day 7 is only the shape
*within* a five-minute window, which no retrospective question has needed. Holding `raw`
for the full 30 days is a one-value change costing roughly 200–250 MB; shortening it to
48 hours drops the total to ~35 MB. All three are the same config field.

### Rollup preserves maxima — this is the central requirement

A 61% spike lasting four minutes, averaged into a 1-hour bucket, reads as **4%**. Naive
averaging destroys precisely the signal this system exists to record.

Every row therefore carries both `cpu_avg` and `cpu_max`, and rollup combines them
differently:

- `cpu_max` of the target bucket = **max** of source `cpu_max` values. A spike stays 61%
  in the archive tier a year later.
- `cpu_avg` = **sample-weighted** mean: `Σ(cpu_avg × samples) / Σ(samples)`. Averaging the
  averages is wrong whenever source buckets contain differing sample counts, which happens
  routinely after a sleep gap.
- `cpu_max_ts` is carried forward from whichever source bucket held the max, so the UI can
  still point at the minute it happened even in a 6-hour bucket.

Charts stack `avg` (the only additive quantity) and overlay `max` as per-series peak
markers — see Dashboard for why stacking maxima would be incorrect.

### Rollup execution

Each tick, after inserting, the sampler collapses at most N expired buckets per tier
(bounded so a tick never stalls), then deletes the source rows in the same transaction.
Crash between collapse and delete is safe: rollup is idempotent, keyed on bucket start,
and uses `INSERT … ON CONFLICT DO UPDATE` so a repeated run recomputes the same values.

---

## Sampler lifecycle

The sampler is **stateless and respawned per tick** by launchd (`StartInterval = 30`), not
a resident loop. Nothing stays in memory between ticks; `sampler_state` is the sole carrier
of the previous reading, which is why it holds `(pid, start_time, cputime_cs)` rather than
being a mere crash-recovery mirror.

This is chosen deliberately over a resident process. A ~50 ms Python invocation every 30
seconds is ~0.17% of one core and zero resident RSS. A resident daemon would hold ~15 MB
indefinitely — and the entire reason this project exists is that resident background
processes accumulated on this machine until they were the problem.

The cost is process-spawn overhead and no in-memory caching. Both are irrelevant at this
sample rate.

Rows in `sampler_state` untouched for more than 5 minutes are pruned each tick, so the
table tracks live PIDs rather than growing forever.

---

## Gaps

A laptop sleeps. Gaps in the record are real information, not missing data to be smoothed
over.

**Detection.** When a tick finds the newest `sampler_state.updated_ts` older than 2× the
interval, it writes one row to `gap` — `(ts_start = last good sample, ts_end = now,
reason)` — and writes no sample for the intervening time.

**Propagation.** Gaps are not copied into every tier. Each `system_<tier>` row carries
`samples` (rows actually present) and `expected` (rows the bucket's duration implies:
120 for a 1-hour bucket at 30s). Coverage is `samples / expected`. The dashboard treats
coverage below 0.5 as a gap bucket and renders it hatched rather than as a low value —
so a 6-hour archive bucket that was 90% asleep never reads as a quiet period. The `gap`
table remains the exact record for the drill-down view; the coverage ratio is the cheap
per-bucket signal that survives every rollup automatically.

**Charts never interpolate across a gap.** Lines break; areas drop out.

**The first tick after a gap discards its CPU delta.** Dividing accumulated CPU time by an
eight-hour sleep produces a number that is arithmetically valid and physically meaningless.

---

## Dashboard

A single HTML page, no external assets or CDN dependencies, served from localhost.

1. **Overview** — stacked area of top processes by CPU over the selected window, stacked
   **from `cpu_avg`**. Memory as a second chart.

   Stacking `cpu_max` would be wrong: the per-process maxima within a bucket did not occur
   at the same instant, so summing them invents a total that never existed and can exceed
   100% × cores. Only `cpu_avg` is additive, because each process's average is a true
   integral over the same bucket duration — the stack then reconciles against the system
   total and against the `__other__` remainder.

   `cpu_max` is not hidden, it is overlaid: each series carries a max marker at its peak,
   and hovering a bucket lists per-process avg and max side by side. The default view
   answers "how was the machine loaded"; the overlay answers "what spiked, and when."
2. **Time brush** — drag to zoom. The server picks the tier matching the requested window
   automatically, so zooming into last night transparently switches from `coarse` to `raw`.
3. **Drill-down** — click any point to get the full ranked process list for that bucket,
   with command lines, `nproc`, and the `__other__` remainder.
4. **System overlay** — load, memory pressure, swap, and free disk on a shared time axis,
   so process spikes can be read against what the machine was doing overall.

Chart rendering follows the `dataviz` skill at implementation time.

## CLI

```
procwatch install     # write and load the launchd plist
procwatch open        # start the local server, open a browser, idle-exit after
procwatch watch <re>  # add a regex to the always-record watchlist
procwatch status      # last tick, DB size, per-tier row counts and windows
procwatch uninstall   # unload launchd, optionally keep the database
```

## Error handling

- `ps` failure or malformed output: log to `~/.local/state/procwatch/sampler.log`, skip the
  tick, keep the agent alive. A monitor that dies on one bad read is worse than a gap.
- Database locked: WAL plus bounded retry with backoff. The sampler drops the tick rather
  than blocking a subsequent one.
- Disk pressure: if free space falls below a floor, the sampler prunes the oldest tier
  early and logs it. It will not be the process that fills a nearly-full disk.
- Clock changes: a monotonic clock is **not** available for this purpose. The sampler is
  respawned per tick, and `time.monotonic()`'s reference point does not survive across
  process instances, so the interval must come from the wall-clock delta between
  `sampler_state.updated_ts` and now. A DST shift or NTP correction can therefore produce a
  non-positive or near-zero interval; the tick is discarded in that case rather than
  dividing by it. Discarding costs one sample; not discarding writes a garbage spike into
  permanent history.

## Testing

The rollup path can corrupt a year of history quietly, so it carries the heaviest coverage.

- **Spike preservation** — inject a 61% four-minute spike, roll it through every tier to
  `archive`, assert it still reads 61%. This is the acceptance test for the whole system.
- **Weighted average correctness** — buckets with differing sample counts must produce a
  duration-weighted mean, not a mean of means. Verified against hand-computed values.
- **CPU delta math** — two synthetic `ps` snapshots with known cputime difference produce a
  known percent. Includes PID reuse (start time changed → delta discarded) and post-gap
  discard.
- **Retention bounds** — fast-forward two simulated years of ticks, assert the database
  stays within bound and each tier's window is exactly as specified.
- **Rollup idempotence** — run the same rollup twice, assert identical output.
- **Gap handling** — simulate an 8-hour sleep, assert a marker is written, no interpolation
  occurs, and the following tick's CPU delta is discarded.
- **Identity separation** — two `log` processes with different predicates must remain two
  distinct rows. Conversely, 27 browser renderers differing only in masked volatile tokens
  must collapse to one row with `nproc = 27`. The regression case is a bundle path longer
  than 100 characters: two genuinely different processes sharing that prefix must not merge.
- **Additivity** — for any bucket, the sum of per-process `cpu_avg` plus `__other__` must
  reconcile against the recorded system CPU total within tolerance. This is the test that
  would have caught using `ps %cpu`, whose fields are documented to sum past 100%.

  The tolerance is real and not a hedge. Each row is rounded to integer tenths
  independently while `cpu_busy` rounds the float total once, so the rounded parts need not
  sum to the rounded whole. Measured against deltas contrived to land on exactly `.5`
  (where `round()`'s half-to-even behaviour is least forgiving): drift was 1 tenth at 10
  processes, 4 at 100, and 3 at 800 — errors largely cancel rather than accumulating. A
  tolerance of one tenth per process is therefore ample, and the invariant is
  reconciliation within rounding, not bit-exact equality.
- **Gap coverage** — a bucket spanning a sleep must report `samples < expected`, and a
  fully-asleep bucket must not be distinguishable from a genuinely idle one *by value
  alone* — only by coverage. Asserts the dashboard has the signal it needs.

## Open questions

None blocking. Alerting, export, and multi-machine support are deliberately out of scope
and the schema does not preclude them.
