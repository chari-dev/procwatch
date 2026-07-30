# Images on the landing page

Every file `docs/index.html` references exists. This records where each came
from and what a replacement would need to show.

## In place

| File | What it shows | Where |
|---|---|---|
| `hero.gif` | Installing it, then the dashboard | The hero. A real recording, 760×489. Also used by the repository README. |
| `verdict.png` | The verdict open in the toolbar, with an explanation beside it | *It reads the chart so you don't have to* |
| `explain.png` | The `!` popover on WindowServer — what to do, what it is | *It knows what mds_stores is* |
| `battery.png` | The battery curve with the area under it divided between processes | *Not "energy impact". Actual watt-hours* |
| `year.png` | The full-year activity grid | *When this machine is actually busy* |
| `updates.png` | Three applications and their version histories | *The update that made it worse* |
| `sleep.png` | What woke it, what kept it awake, what it cost | *Why it was warm in the bag* |
| `storage.png` | Disk space split between app, data and caches | *The app is small. Its data is not* |
| `ports.png` | Listening ports with the process behind each | *What is on 3000, and who started it* |
| `devices.png` | Adding another Mac by address and three-word key | *Watch the other one from this one* |
| `live.png` | The live table, sorted by power | *Grouped by app. Still while you read it* |
| `history.png` | A stacked CPU chart with a point open | *Any minute, named processes* |
| `charts.png` | Network by process, with the crosshair tooltip | *Charts that name the process* |
| `icon*.png` | The app icon | Favicon, header, footer |

All the screenshots are the menu bar panel, cropped to the same rectangle so
they sit at one size and one crop across the page.

## Still wanted

**`events.png`** — the *What happened* card, unfolded, ideally with entries under
both *Keeps happening* and *Incidents*. Its section is written to stand without a
picture until one arrives, so nothing is broken meanwhile; send one and it
becomes a two-column spread like its neighbours.

**A social preview** — 1200×630 for link unfurls. `verdict.png` stands in, and it
is a screenshot, which renders badly at thumbnail size. The icon and the words
"Procwatch — remember what ate your Mac" on a dark background would be better.

**A terminal shot** — `procwatch why` and `procwatch what` answering. The install
section had a slot and it has been removed rather than left broken; it can come
back.

## Taking a replacement

Dark mode, 2×, and crop to the panel rather than the desktop. The existing crop
is `(86, 26) → (1508, 922)` out of a 1542×984 screenshot, which is the menu bar
panel with its notch and rounded corners removed.
