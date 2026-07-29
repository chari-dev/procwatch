# Contributing

## Running it

```sh
python3 -m procwatch.cli install    # start the launchd agent
python3 -m procwatch.cli open       # dashboard
python3 -m unittest discover -s tests
```

No dependencies, no virtualenv, no build step. macOS and Python 3.9+.

## The rules this codebase holds to

**Stdlib only.** Runtime and tests. If something needs a package, it does not
go in. This is what makes the single-file build possible and what makes the
tool safe to leave running for years.

**CPU is never read from `ps %cpu`.** That column is a decaying average over
an opaque window (`man ps`), its fields are documented to sum past 100%, and
it cannot be integrated across buckets — which is exactly what the rollup
does. Rates come from differencing cumulative `cputime`. A test enforces it.

**Rollup preserves maxima.** A 61% spike lasting four minutes, averaged into
an hour, reads as 4%. Every stored row carries `cpu_max` alongside `cpu_avg`,
and `cpu_max_ts` so a six-hour bucket can still name the minute. Averages are
sample-weighted; an average of averages is wrong whenever buckets hold
different sample counts, which happens after every sleep.

**Charts stack `cpu_avg`, never `cpu_max`.** Per-process maxima within a
bucket did not occur at the same instant, so summing them invents a total
that never existed.

**Fail loudly rather than plausibly.** A monitor that records a wrong number
nobody can later detect is worse than one that records a gap. Several bugs in
this project's history were of exactly that shape — a timestamp an hour off,
a memory reading of zero, history deleted instead of rolled up. Prefer an
exception and a skipped tick.

**A gap is information.** Asleep and idle are different facts. Coverage below
0.5 renders hatched; nothing interpolates across a gap.

## Tests

Write the failing test first and watch it fail. A test that asserts inside a
loop over an empty result never runs its assertion and passes against broken
code — that happened here, on the most serious bug in the project. Prefer
asserting on fetched scalars, and for anything pinning behaviour that was
once wrong, mutation-check it: break the implementation, confirm the test
fails, restore.

## Before opening a pull request

```sh
python3 -m unittest discover -s tests   # all green
python3 tools/bundle.py                 # single-file build still works
```
