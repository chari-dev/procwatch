// Enough of a browser to press one button.
//
// harness.mjs answers every DOM call with a stand-in and proves only that the
// page loads. That was written after three releases shipped a script that died
// on its first line, and it does that job -- but it cannot tell whether
// anything the page does actually works, because every element it hands out is
// a different object and nothing that is set can be read back.
//
// This one keeps a registry: the same id returns the same element every time,
// listeners are recorded, and a click can be dispatched. Enough to assert that
// pressing the alert icon opens the explanation, anchors it to the icon rather
// than to the middle of the screen, and that pressing it again puts it away.
import fs from 'fs';

const code = fs.readFileSync(process.argv[2], 'utf8');
let whyCalls = 0;
const problems = [];
const listeners = {document: {}, window: {}};

const registry = {};
const make = (id) => {
  const self = {
    // Real events always carry a target with a tag name; the page checks it to
    // avoid stealing keystrokes from the search box.
    id, tagName: 'DIV', dataset: {}, style: {}, children: [], options: [],
    attrs: {},
    // false, as in HTML: an element is visible unless the markup says
    // otherwise. Defaulting to hidden made every element look folded away, so
    // code that simply never unhid something appeared to be working.
    _html: '', textContent: '', value: '', hidden: false,
    // A real classList, so what the page does to an element can be observed.
    // Stubbed out, every class change looked identical to none at all -- which
    // is how "clicking a row does nothing" and "clicking a row works" became
    // indistinguishable to every test here.
    classes2: new Set(),
    classList: {
      toggle(name, on) {
        var want = on === undefined ? !self.classes2.has(name) : !!on;
        if (want) { self.classes2.add(name); } else { self.classes2.delete(name); }
        return want;
      },
      add(name) { self.classes2.add(name); },
      remove(name) { self.classes2.delete(name); },
      contains: (name) => self.classes2.has(name)
    },
    appendChild(){}, removeEventListener(){}, focus(){}, blur(){}, select(){},
    scrollIntoView(){},
    handlers: {},
    addEventListener(kind, fn){ (self.handlers[kind] =
      self.handlers[kind] || []).push(fn); },
    setAttribute(k, v){ self.attrs[k] = v; },
    getAttribute: (k) => (k in self.attrs ? self.attrs[k] : null),
    removeAttribute(k){ delete self.attrs[k]; },
    // Elements the page just wrote into itself, handed back so the page's own
    // wiring can bind to them and a test can press them. Parsed out of the
    // assigned innerHTML, cached per string so the objects the page bound to
    // are the objects the test fires -- rebuilding them per call would bind
    // handlers to one set and press another.
    _cache: {}, _cacheHtml: null,
    querySelectorAll(sel) {
      const html = String(self.innerHTML);
      // Emptied when the content changes, not on every query. Clearing per call
      // meant the page bound its handlers to one set of stubs and the test
      // pressed a different, freshly-made set -- so every press did nothing and
      // looked exactly like the feature being broken.
      if (html !== self._cacheHtml) { self._cache = {}; self._cacheHtml = html; }
      const key = sel;
      if (self._cache[key]) { return self._cache[key]; }
      // Any [data-x] selector, rather than a list of the two that happened to
      // be needed first. The list was why the disk-space rows came back empty
      // and looked like a page that had not drawn them.
      const attr = (sel.match(/\[(data-[a-z-]+)\]/) || [])[1] || null;
      let found = [];
      if (attr) {
        const key = attr.slice(5).replace(/-([a-z])/g,
                                         (m, c) => c.toUpperCase());
        const re = new RegExp(attr + '="([^"]*)"', 'g');
        const seen = new Set();
        let m;
        while ((m = re.exec(html)) !== null) {
          if (seen.has(m[1])) { continue; }
          seen.add(m[1]);
          const node = make(attr + ':' + m[1]);
          node.dataset[key] = m[1];
          node.attrs[attr] = m[1];
          found.push(node);
        }
      }
      self._cache[key] = found;
      return found;
    },
    // Stable per selector: the page writes to the element it finds here, and a
    // fresh object each call would throw those writes away -- which reads as the
    // page never having made them.
    querySelector: (sel) => byId(id + ' ' + sel),
    closest: (sel) => (self.matches(sel) ? self : null),
    classes: new Set(),
    matches: (sel) => sel === '#' + id ||
      (sel.startsWith('.') && self.classes.has(sel.slice(1))) ||
      (sel.startsWith('[data-what') && 'what' in self.dataset),
    // The icon sits low and to the right, so a panel placed anywhere near the
    // middle of the window is placed wrong.
    offsetHeight: 400, scrollLeft: 0,
    getBoundingClientRect: () => ({left: 900, top: 700, right: 917,
                                   bottom: 717, width: 17, height: 17})
  };
  /* Assigning innerHTML destroys the children, as it does in a browser.

     Without this the stand-in handed the same child object back before and
     after a rebuild, so anything the page restores after replacing its contents
     -- a scroll position, a selection -- appeared to survive on its own and the
     code that restores it could be deleted with no test noticing. */
  Object.defineProperty(self, 'innerHTML', {
    get() { return self._html; },
    set(value) {
      self._html = value;
      self._cache = {};
      self._cacheHtml = null;
      Object.keys(registry).forEach((key) => {
        if (key.startsWith(id + ' ')) { delete registry[key]; }
      });
    }
  });
  return self;
};

const byId = (id) => (registry[id] = registry[id] || make(id));

// The state the parsed markup would have produced. test_dashboard.py asserts
// separately that the markup really does declare these, so the two cannot
// drift apart without something failing.
['whypanel', 'eventsbody', 'whatpop', 'burgermenu', 'qresults', 'banner',
 'settings', 'devlist'].forEach((id) => { byId(id).hidden = true; });
byId('whytoggle').attrs['aria-expanded'] = 'false';
byId('eventstoggle').attrs['aria-expanded'] = 'false';

// dataset and getAttribute have to agree. They did not, and the disagreement
// was invisible until the code under test started reading the attribute.
const iconFor = (id, attrs) => {
  const node = make(id);
  Object.keys(attrs).forEach((k) => {
    node.attrs['data-' + k] = attrs[k];
    node.dataset[k] = attrs[k];
  });
  return node;
};
const icon = iconFor('icon', {what: 'mds_stores',
                              advice: 'Nothing. It stops on its own.'});
// A finding that names no process still has advice to give, and the icon is
// the only place left to put it.
const bare = iconFor('bare', {what: '', advice: 'Quit what you are not using.'});
// A second one, because two findings that name no process both carry an empty
// name -- and anything that identifies an icon by its name treats them as the
// same icon, so pressing the second closes the first instead of switching.
const bare2 = iconFor('bare2', {what: '', advice: 'Leave it plugged in.'});

globalThis.document = {
  getElementById: byId,
  /* A document-level query finds the same elements a container query does.
     Returning [] for everything meant applyPinStyle -- which reaches for
     document.querySelectorAll("[data-series]") -- appeared to do its work on a
     page with no rows in it, so a row that was never marked selected and a row
     that was looked exactly the same from here. */
  querySelectorAll: (sel) => (
    sel === '.whatq' ? [icon, bare, bare2]
    : /data-series/.test(sel) ? byId('livebody').querySelectorAll('[data-series]')
    : []),
  querySelector: (sel) => (sel.includes('data-what') ? icon : byId('any')),
  createElement: make, createElementNS: make,
  addEventListener: (kind, fn) => { (listeners.document[kind] =
    listeners.document[kind] || []).push(fn); },
  body: Object.assign(make('body'), {contains: () => true}),
  documentElement: make('html'),
  readyState: 'complete'
};
globalThis.window = {
  addEventListener: (kind, fn) => { (listeners.window[kind] =
    listeners.window[kind] || []).push(fn); },
  devicePixelRatio: 2, innerWidth: 1400, innerHeight: 900,
  location: {href: ''}, confirm: () => false
};
globalThis.matchMedia = () => ({matches: false, addEventListener(){}, addListener(){}});
globalThis.getComputedStyle = () => ({getPropertyValue: (n) => n});
globalThis.localStorage = {getItem: () => null, setItem(){}};
// A discharge: 80% down to 74% while unplugged, then plugged in and flat. Two
// processes with energy in every bucket, one of them twice the other.
// Shaped so each way of getting this wrong produces a different answer:
//
//   80 79 80 -- 78 77 75   unplugged, then
//   79 83 87              plugged in and charging
//
// The rise from 79 to 80 catches summing absolute differences instead of falls.
// The missing reading catches a curve that dives to zero where a sample was
// dropped. The charging run catches counting a climb as consumption.
// Falls while unplugged: 1 + 1 + 2 = 4% of 50000 mWh = 2.00 Wh.
const LEVELS = [80, 79, 80, null, 78, 77, 75];
const CHARGING = [79, 83, 87];
const BATT = (() => {
  const system = [], a = [], b = [];
  const bucket = (i, pct, ac) => {
    const ts = 1750000000 + i * 300;
    system.push({ts, cpu_busy: 10, load1: 1, mem_used_kb: 1, mem_comp_kb: 1,
                 swap_used_kb: 0, disk_free_kb: 1, expected: 10, samples: 10,
                 coverage: 1, batt_pct: pct, batt_draw_mw: ac ? -1 : 5000,
                 batt_full_mwh: 50000, on_ac: ac});
    a.push({ts, cpu_avg: 10, cpu_max: 10, rss_avg: 1, rss_max: 1, nproc: 1,
            energy: 200, net_in: 0, net_out: 0, disk_read: 0, disk_write: 0});
    b.push({ts, cpu_avg: 5, cpu_max: 5, rss_avg: 1, rss_max: 1, nproc: 1,
            energy: 100, net_in: 0, net_out: 0, disk_read: 0, disk_write: 0});
  };
  LEVELS.forEach((pct, i) => bucket(i, pct, false));
  CHARGING.forEach((pct, i) => bucket(LEVELS.length + i, pct, true));
  const list = [
    {exe: 'Heavy', app: 'Heavy', is_system: false, is_other: false, points: a},
    {exe: 'Light', app: 'Light', is_system: false, is_other: false, points: b}];
  // The battery chart reads the energy-ranked list, not the CPU-ranked one.
  // Given a different list here, a chart that reaches for the wrong one draws
  // the wrong processes -- which is exactly the bug this ranking was added for.
  return {tier: 'raw', gaps: [], system, series: [
    {exe: 'Peaky', app: 'Peaky', is_system: false, is_other: false,
     points: a.map((p) => Object.assign({}, p, {energy: 0}))}],
    energy_series: list};
})();

// Two applications, each with two processes. Alpha leads on CPU and Beta on
// memory, so sorting by a different column has to change the order -- and the
// second snapshot flips the CPU order, so a frozen table has to ignore it.
const proc = (exe, app, cpu, rss, ports) => ({
  exe, app, args: '', command: '/' + exe, is_system: false, cpu, rss_kb: rss,
  pids: [1], ports: ports || [], disk_read: 0, disk_write: 0, net_in: 0,
  net_out: 0, energy: 0, waiting: 0, nproc: 1, lead_pid: 1
});
const snapshot = (flip) => ({
  ts: 1750000000, interval: 2, warming_up: false,
  battery: {percent: 80, on_ac: false, draw_mw: 5000, full_mwh: 50000},
  network_age: 0,
  system: {cpu_busy: 200, load1: 2, mem_used_kb: 4000000, mem_comp_kb: 500000,
           swap_used_kb: 0, disk_free_kb: 100000000},
  groups: [
    // The children swap places too when the snapshot flips, so a group whose
    // own position is held can still be caught reordering inside itself.
    proc('AlphaMain', 'Alpha', flip ? 5 : 90, 1000),
    proc('AlphaHelp', 'Alpha', flip ? 50 : 10, 500),
    proc('BetaMain', 'Beta', flip ? 90 : 5, 900000, [8080]),
    proc('BetaHelp', 'Beta', flip ? 10 : 1, 400000)
  ].concat(liveExtra ? [proc('GammaMain', 'Gamma', 999, 10)] : [])
});
let liveFlip = false;
let liveExtra = false;   // a process that starts while the table is frozen

globalThis.fetch = async (url) => ({
  ok: true, status: 200,
  json: async () => (String(url).includes('/api/what')
    ? {name: 'Spotlight', process: 'mds_stores', known: true,
       does: 'Writes the index.', high: 'A rebuild.', advice: 'Nothing.',
       usual: {cpu_avg: 2.6, cpu_peak: 152, memory_mb: 57,
               memory_peak_mb: 870, samples: 12, first_seen: 1750000000}}
    // Honouring the parameter, like the real endpoint: the second ranking is
    // sent only when it is asked for. A page that stops asking has to break.
    : String(url).includes('/api/prefs')
    ? {findings_enabled: '1', findings_notify: 'causes'}
    : String(url).includes('/api/storage')
    ? [{app: 'Claude', total: 11783786496, ts: 1750000000,
        bundle: 789995520, support: 10993790976, caches: 0}]
    : String(url).includes('/api/now') ? snapshot(liveFlip)
    : String(url).includes('/api/series')
    ? (String(url).includes('energy=1')
       ? BATT
       : Object.assign({}, BATT, {energy_series: undefined}))
    : String(url).includes('/api/why')
    ? (whyCalls++ > 0
      ? {verdict: 'Nothing was wrong.', start: 0, end: 0, findings: []}
      : {verdict: 'Something happened.', start: 0, end: 0, findings: [
        {kind: 'system-known', severity: 'cost', headline: 'Spotlight was busy',
         detail: 'It held 190% of a core.', advice: 'Nothing to do.',
         evidence: {}, about: {process: 'mds_stores', name: 'Spotlight',
                               known: true, does: 'Writes the index.'}},
        {kind: 'memory-pressure', severity: 'cause',
         headline: 'Your Mac ran out of memory', detail: 'Swap grew.',
         advice: 'Quit what you are not using.', evidence: {}, about: null}]})
    // A month of sleeps is not four hundred things to look at. The count on
    // the button has to be what is actually behind it.
    : String(url).includes('/api/events')
    ? {summary: 'Two things happened.', counts: {sleep: 181},
       patterns: [{severity: 'cost', headline: 'a', count: 3, says: 'thrice'}],
       firsts: [],
       episodes: [{severity: 'note', headline: 'b', start: 0, also: [], more: 0},
                  {severity: 'note', headline: 'c', start: 0, also: [], more: 0}],
       timeline: new Array(40).fill({severity: 'note', kind: 'sleep', ts: 0,
                                     headline: 'slept', subject: ''})}
    : {series: [], system: [], gaps: [], tier: 'raw', rules: [], events: [],
       metrics: {}, groups: [], findings: [], patterns: [], episodes: [],
       firsts: [], counts: {}, timeline: [], summary: ''}),
  text: async () => ''
});
// Deferred, like a real frame. Running the callback inline meant anything that
// sets a style and clears it on the next frame appeared never to have set it --
// the height hold below is exactly that shape.
const frames = [];
globalThis.requestAnimationFrame = (f) => { frames.push(f); return frames.length; };
globalThis.__frame = () => {
  frames.splice(0).forEach((f) => { try { f(0); } catch (e) { /* drawing */ } });
};
// Timers the page sets are remembered rather than dropped, so a test can make
// two seconds pass. Nothing here needs a production hook: this is what a
// browser does with them.
const timers = [];
globalThis.setInterval = (fn, ms) => { timers.push({fn, ms}); return timers.length; };
globalThis.clearInterval = () => {};
globalThis.__tick = async () => {
  timers.forEach((t) => { try { t.fn(); } catch (e) { /* unrelated poller */ } });
  for (let i = 0; i < 4; i++) { await new Promise((r) => setImmediate(r)); }
  globalThis.__frame();
};
globalThis.setTimeout = (f) => { if (typeof f === 'function') { f(); } return 0; };
globalThis.clearTimeout = () => {};

// Functions worth testing directly rather than through the DOM. The page opens
// with "use strict", under which a var in an indirect eval stays in the eval's
// own scope instead of landing on globalThis -- so it hands them over itself.
// A test-only epilogue appended to the source, not a change to the page.
const HANDOVER = "\n;globalThis.__page = {squarify: typeof squarify === " +
  "'function' ? squarify : null, colourFor: typeof colourFor === 'function' ? " +
  "colourFor : null};";

try {
  // Indirect eval rather than new Function, so the page runs in one scope the
  // way a <script> does rather than inside a wrapper of the harness's making.
  (0, eval)(code + HANDOVER);
} catch (error) {
  console.log('THREW while loading: ' + error.message);
  process.exit(1);
}

// The verdict renders on load. Both of its findings must offer the icon --
// including the one that names no process, whose advice has nowhere else to go.
await new Promise((r) => setImmediate(r));
await new Promise((r) => setImmediate(r));
const rendered = String(byId('findings').innerHTML);
if (!rendered.includes('Spotlight was busy')) {
  problems.push('the verdict did not render: ' + rendered.slice(0, 120));
}
const icons = (rendered.match(/class="whatq"/g) || []).length;
if (icons !== 2) {
  problems.push('expected an icon on both findings, found ' + icons);
}
if (!rendered.includes('Quit what you are not using')) {
  problems.push("a finding with no process lost its advice");
}
if (rendered.includes('why-do')) {
  problems.push('the advice is still printed inline on the card');
}
if (rendered.includes('Writes the index')) {
  problems.push('the catalogue prose is still printed inline on the card');
}

// ---- the verdict, in the toolbar ------------------------------------------
//
// It was the tallest card on the page and the first, so a bad day pushed every
// chart below the fold. It is a toolbar dropdown now: the count is always
// visible, the reading is one press away.
const press = (el) => (el.handlers.click || []).forEach((fn) => fn({
  target: el, stopPropagation(){}, preventDefault(){}
}));
const key = (name) => (listeners.document.keydown || []).forEach((fn) => fn({
  key: name, target: byId('somewhere'), stopPropagation(){}, preventDefault(){}
}));

const whyToggle = byId('whytoggle');
const whyPanel = byId('whypanel');
if (whyToggle.hidden) {
  problems.push('the toolbar offers no way to reach the verdict');
}
if (!whyPanel.hidden) {
  problems.push('the verdict panel is open before anybody asked');
}
if (byId('whycount').textContent !== '2') {
  problems.push('the toolbar button does not carry the count: ' +
                JSON.stringify(byId('whycount').textContent));
}
// One of the two fixtures is a cause, so the button has to show it.
if (!String(whyToggle.className).includes('cause')) {
  problems.push('the button does not carry the worst severity behind it: ' +
                whyToggle.className);
}
if (!String(whyToggle.getAttribute('title')).includes('2 findings')) {
  problems.push('the button has no readable title: ' +
                whyToggle.getAttribute('title'));
}
if (whyToggle.getAttribute('aria-expanded') !== 'false') {
  problems.push('the closed button does not report itself collapsed');
}
press(whyToggle);
if (whyPanel.hidden) { problems.push('pressing the button did not open it'); }
if (whyToggle.getAttribute('aria-expanded') !== 'true') {
  problems.push('the open button does not report itself expanded');
}
press(whyToggle);
if (!whyPanel.hidden) { problems.push('pressing it again did not close it'); }

// Escape closes it, like the menu beside it.
press(whyToggle);
key('Escape');
if (!whyPanel.hidden) { problems.push('Escape did not close the verdict'); }

// The two dropdowns in the toolbar must not both be open.
press(whyToggle);
press(byId('burger'));
if (!whyPanel.hidden) {
  problems.push('opening the menu left the verdict open underneath it');
}

// The history's button counts what is behind it -- one repeat and two
// incidents -- not the forty sleeps on the timeline.
const eventsToggle = byId('eventstoggle');
if (eventsToggle.hidden) {
  problems.push('the history offers no way to see its detail');
}
if (!String(eventsToggle.innerHTML).includes('3 things to look at')) {
  problems.push('the history button miscounts: ' +
                JSON.stringify(String(eventsToggle.innerHTML)));
}
if (!String(byId('eventssum').innerHTML).includes('Two things happened')) {
  problems.push('the summary paragraph was folded away with the rest');
}

// A second look that finds nothing must take the button away rather than
// offer to unfold an empty panel.
const spanBtn = make('span6h');
spanBtn.classes.add('whybtn');
spanBtn.dataset.span = '21600';
(byId('whypanel').handlers.click || []).forEach((fn) => fn({
  target: spanBtn, stopPropagation(){}, preventDefault(){}
}));
await new Promise((r) => setImmediate(r));
await new Promise((r) => setImmediate(r));
if (byId('whytoggle').hidden) {
  problems.push('the verdict button disappeared when the news was good');
}
if (byId('whycount').textContent !== '0') {
  problems.push('a quiet window does not read as zero: ' +
                JSON.stringify(byId('whycount').textContent));
}
if (String(byId('whytoggle').className).match(/cause|cost/)) {
  problems.push('a quiet window still wears a severity: ' +
                byId('whytoggle').className);
}
if (!String(byId('findings').innerHTML).includes('Nothing else worth reporting')) {
  problems.push('a quiet window says nothing at all when opened');
}

// ---- battery, and what spent it ------------------------------------------
//
// The line is the charge that was left; the area under it is divided by each
// process's share of the energy macOS attributed. The numbers below check the
// two things that make it honest rather than decorative.
const chart = String(byId('energy').innerHTML);
if (!chart.includes('<svg')) {
  problems.push('the battery chart drew nothing: ' + chart.slice(0, 140));
}
// The stack is clipped to the curve, so no band may reach the top of a chart
// whose highest reading is 80%. A full-height band means the battery was
// ignored and the old share view is still being drawn.
const bands = (chart.match(/class="band"/g) || []).length;
if (bands < 2) { problems.push('expected a band per process, found ' + bands); }

const note = String(byId('battnote').innerHTML);
if (!note.includes('2.00 Wh')) {
  problems.push('the charge spent is wrong or missing: ' + note.slice(0, 160));
}
// Heavy took two thirds of the attributed energy, so two thirds of 2000 mWh.
const legend = String(byId('energyleg').innerHTML);
if (legend.includes('Peaky')) {
  problems.push('the battery chart is drawing the CPU-ranked list');
}
if (!legend.includes('Heavy')) {
  problems.push('the battery chart is not drawing the energy-ranked list: ' +
                legend.slice(0, 160));
}
if (!legend.includes('66.7%')) {
  problems.push("the shares are wrong: " + legend.slice(0, 200));
}
if (!legend.includes('~1.33 Wh')) {
  problems.push('the apportionment is wrong or unmarked: ' + legend.slice(0, 240));
}
// The battery line itself, over the top of the bands it paid for.
if (!/fill="none" stroke=/.test(chart)) {
  problems.push('the battery line is missing');
}
// And the strip marking the stretches when nothing was being drained.
if (!/<rect x="[\d.]+" y="\d+" width="[\d.]+" height="3"/.test(chart)) {
  problems.push('the plugged-in stretches are not marked');
}

// ---- geometry -------------------------------------------------------------
//
// The point of the chart is that the stack is bounded by the curve. That is a
// claim about coordinates, and only coordinates can check it: with the highest
// reading at 87% of a 100% axis, nothing may reach the top of the frame.
// h = 250, PT = 12, PB = 26, so yAt(v) = 224 - 2.12v: the top of the frame is
// y = 12, and 87% is y = 39.6.
const points = (d) => {
  const out = [];
  const re = /[ML]([\d.]+) ([\d.]+)/g;
  let m;
  while ((m = re.exec(d)) !== null) { out.push([+m[1], +m[2]]); }
  return out;
};
const bandDs = [...chart.matchAll(/class="band"[^>]*?d="([^"]+)"/g)].map((m) => m[1]);
if (!bandDs.length) { problems.push('no band geometry to check'); }
const highest = Math.min(...bandDs.flatMap((d) => points(d).map((pt) => pt[1])));
if (!(highest > 20)) {
  problems.push('a band reaches y=' + highest.toFixed(1) +
                ', so the stack is not bounded by the battery curve');
}

// The bands have to tile the area, not float in it. areaPath walks the top
// left-to-right then the bottom right-to-left inside one closed subpath, so
// each band's points split in half -- and the lower band's top must be exactly
// the upper band's bottom. Clipping only the tops leaves a wedge of empty
// space between them, which reads as energy nobody spent.
if (bandDs.length >= 2) {
  const halves = bandDs.map((d) => {
    const pts = points(d);
    const half = pts.length / 2;
    return {top: pts.slice(0, half).map((pt) => pt[1]),
            bottom: pts.slice(half).reverse().map((pt) => pt[1])};
  });
  // Bands come out bottom-first, so band 1's bottom sits on band 0's top.
  const lower = halves[0], upper = halves[1];
  const drift = Math.max(...upper.bottom.map(
    (y, i) => Math.abs(y - lower.top[i])));
  if (!(drift < 0.01)) {
    problems.push('the bands do not meet: up to ' + drift.toFixed(1) +
                  'px of gap between one and the next');
  }
}

// And the curve itself never touches the floor. A bucket with no reading
// carries the last one forward; zeroing it would draw a cliff to y = 224 and
// invent a battery that emptied and refilled.
const lineD = (chart.match(/d="([^"]+)" fill="none" stroke=/) ||
               chart.match(/fill="none" stroke="[^"]*" stroke-width="2"[^>]*/) ||
               [])[1];
const curve = lineD ? points(lineD) : [];
if (!curve.length) {
  problems.push('could not read the battery curve back');
} else {
  const lowest = Math.max(...curve.map((pt) => pt[1]));
  if (lowest > 150) {
    problems.push('the battery curve falls to y=' + lowest.toFixed(1) +
                  ', which is a reading that was dropped, not one that happened');
  }
  // The claim the whole chart rests on: the top of the stack IS the battery
  // curve, at every bucket. Scaling the bands by one bucket's level instead of
  // each bucket's passes every check above -- the stack is still bounded, still
  // tiled, still under the frame -- and draws a flat lid over a moving curve.
  if (bandDs.length) {
    const top = points(bandDs[bandDs.length - 1]);
    const half = top.length / 2;
    const lid = top.slice(0, half).map((pt) => pt[1]);
    if (lid.length !== curve.length) {
      problems.push('the stack has ' + lid.length + ' points and the curve ' +
                    curve.length);
    } else {
      const drift = Math.max(...lid.map((y, i) => Math.abs(y - curve[i][1])));
      if (!(drift < 0.01)) {
        problems.push('the top of the stack is up to ' + drift.toFixed(1) +
                      'px away from the battery curve it is supposed to fill');
      }
    }
  }
}

// ---- the live table: sorting, and holding still while it is read ----------
const live = byId('livebody');
const order = () => [...String(live.innerHTML)
  .matchAll(/data-series="([^"]+)"/g)].map((m) => m[1]);

if (order().length !== 2) {
  problems.push('the live table painted ' + order().length +
                ' applications, expected 2: ' + String(live.innerHTML).slice(0, 120));
}
if (order()[0] !== 'Alpha') {
  problems.push('the table does not sort by CPU to begin with: ' + order());
}

// Sixty rows come out and go back in. For the instant between, the document is
// hundreds of pixels shorter and the browser clamps the scroll -- which is why
// scrolling down and waiting two seconds went back to the top. The height is
// held across the swap and released on the next frame.
if (!/^\d+px$/.test(String(live.style.minHeight))) {
  problems.push('the table does not reserve its height while it is replaced: ' +
                JSON.stringify(live.style.minHeight));
}
// The charts matter more than the table for this: nine of them redraw together
// on the recorder's tick, and between them they are most of the page's height.
['cpu', 'mem', 'energy', 'busy'].forEach((chart) => {
  if (!/^\d+px$/.test(String(byId(chart).style.minHeight))) {
    problems.push('the ' + chart + ' chart does not reserve its height while ' +
                  'it is redrawn: ' + JSON.stringify(byId(chart).style.minHeight));
  }
});
globalThis.__frame();

// The table scrolls sideways, and losing that is the same loss of place as
// losing the page scroll.
// The table scrolls in both directions -- it is 60vh tall with overflow auto --
// and the vertical one is the one people actually use. Restoring only the
// sideways one is why it kept jumping back to the top.
live.querySelector('.tablewrap').scrollLeft = 240;
live.querySelector('.tablewrap').scrollTop = 380;
await globalThis.__tick();
// Re-queried, not remembered: the refresh replaced the element, so the object
// held before the tick is the old one and would report its own value forever.
const across = live.querySelector('.tablewrap').scrollLeft;
const down = live.querySelector('.tablewrap').scrollTop;
if (across !== 240) {
  problems.push('the sideways scroll of the table was thrown away on refresh: ' +
                across);
}
if (down !== 380) {
  problems.push('the table scrolled itself back to the top on refresh: ' + down);
}

if (live.style.minHeight) {
  problems.push('the reserved height was never released: ' +
                JSON.stringify(live.style.minHeight));
}

// Sorting by memory has to reverse it. Beta holds 1.3 GB against Alpha's 1.5 MB.
const heads = live.querySelectorAll('th[data-sort]');
const head = (key) => heads.filter((h) => h.dataset.sort === key)[0];
if (!head('rss_kb')) {
  problems.push('there is no memory column to sort by: ' +
                heads.map((h) => h.dataset.sort).join(','));
} else {
  press(head('rss_kb'));
  if (order()[0] !== 'Beta') {
    problems.push('sorting by memory did nothing: ' + order());
  }
  // And a column whose value comes from an accessor rather than a field. Ports
  // sorted its array as a string before, so 8080 lost to nothing at all.
  press(head('ports'));
  if (order()[0] !== 'Beta') {
    problems.push('sorting by ports did nothing: ' + order());
  }
  press(head('cpu'));
  if (order()[0] !== 'Alpha') {
    problems.push('sorting back to CPU did nothing: ' + order());
  }
}

// Selecting a row freezes the order. The next snapshot reverses the CPU
// standings; the table must not move, and the numbers must still change.
const rows = live.querySelectorAll('[data-series]');
const alpha = rows.filter((r) => r.dataset.series === 'Alpha')[0];
if (!alpha) {
  problems.push('the rows are not selectable');
} else {
  press(alpha);
  // Selecting a row has to look like something. The freeze below proves the
  // click was received; this proves it was answered.
  if (!alpha.classList.contains('pinned')) {
    problems.push('clicking a row did not mark it selected');
  }
  if (!alpha.classList.contains('lit')) {
    problems.push('clicking a row did not light it');
  }
  press(alpha);
  if (alpha.classList.contains('pinned')) {
    problems.push('clicking the row again did not deselect it');
  }
  press(alpha);
  liveFlip = true;
  // Two seconds pass and the machine reports a reversed CPU order.
  await globalThis.__tick();
  if (order()[0] !== 'Alpha') {
    problems.push('the table re-sorted itself while a row was selected: ' +
                  order());
  }
  if (!String(live.innerHTML).includes('90.0%')) {
    problems.push("the values stopped updating, which is not what was frozen");
  }
  // And the children of an expanded application hold their order too. Before,
  // the group stayed put while the process being read moved inside it.
  const kids = () => [...String(live.innerHTML)
    .matchAll(/<tr class="kid[^"]*"[^>]*><td>([A-Za-z]+)/g)].map((m) => m[1]);
  if (kids()[0] !== 'AlphaMain') {
    problems.push('the processes inside an application re-sorted while a row ' +
                  'was selected: ' + kids().join(','));
  }
  // Releasing it lets the table sort again.
  press(alpha);
  await globalThis.__tick();
  if (order()[0] !== 'Beta') {
    problems.push('releasing the selection did not let it re-sort: ' + order());
  }
  const after = [...String(live.innerHTML)
    .matchAll(/<tr class="kid[^"]*"[^>]*><td>([A-Za-z]+)/g)].map((m) => m[1]);
  if (after[0] !== 'BetaMain') {
    problems.push('releasing did not let the children re-sort either: ' +
                  after.join(','));
  }

  // Selecting again freezes the order that is on screen NOW, not the one held
  // last time. Releasing has to clear the old ranks, or the second selection
  // snaps the table back to how it looked during the first.
  const beta = live.querySelectorAll('[data-series]')
    .filter((r) => r.dataset.series === 'Beta')[0];
  if (!beta) {
    problems.push('the reordered table is not selectable');
  } else {
    press(beta);
    await globalThis.__tick();
    if (order()[0] !== 'Beta') {
      problems.push('the second selection restored a stale order: ' + order());
    }

    // A column heading is an instruction, not jitter: pressing one reorders the
    // table even with a row selected. The freeze is there to stop the table
    // moving on its own, not to stop it moving when told to.
    press(head('rss_kb'));
    if (order()[0] !== 'Beta') {
      problems.push('sorting by memory with a row selected did nothing: ' + order());
    }
    // By name, which is the one ordering the flipped values cannot also
    // produce -- Beta leads on both CPU and memory once they flip, so sorting
    // by either would have looked like it worked while doing nothing.
    press(head('exe'));
    if (order()[0] !== 'Alpha') {
      problems.push('sorting by name with a row selected did nothing: ' + order());
    }

    // Back to CPU order with nothing selected, then two things happen at once:
    // the values change, and a process starts. Neither has been painted yet.
    press(head('cpu'));
    press(beta);
    await globalThis.__tick();
    const painted = order();
    if (painted[0] !== 'Beta') {
      problems.push('expected the flipped CPU order before freezing: ' + painted);
    }
    liveFlip = false;      // Alpha leads on CPU again
    liveExtra = true;      // and Gamma appears, ahead of both on CPU
    const again = live.querySelectorAll('[data-series]')
      .filter((r) => r.dataset.series === 'Beta')[0];
    press(again);
    await globalThis.__tick();
    const held = order();
    // Taking a fresh sort here would put Gamma first and Alpha second: the
    // reader clicked on a table that showed neither.
    if (held[0] !== 'Beta' || held[1] !== 'Alpha') {
      problems.push('the freeze did not hold the painted order: ' + held);
    }
    // And something that started after the freeze waits at the end rather than
    // shoving the rows being read down the table.
    if (held[held.length - 1] !== 'Gamma') {
      problems.push('a process that started while frozen jumped the queue: ' +
                    held);
    }
  }
}

// ---- the disk-space breakdown ---------------------------------------------
//
// The three colours were in the bar and nowhere else. Hovering has to name all
// three, including the one that is zero -- "no cache" and "no answer" look the
// same on a bar and are not the same thing.
const store = byId('storagebody');
const storeRows = store.querySelectorAll('[data-storage]');
if (!storeRows.length) {
  problems.push('the disk-space rows are not hoverable: ' +
                String(store.innerHTML).slice(0, 140));
} else {
  const move = (storeRows[0].handlers.mousemove || []);
  if (!move.length) {
    problems.push('nothing listens for a hover on a disk-space row');
  } else {
    move.forEach((fn) => fn({clientX: 10, clientY: 10, target: storeRows[0]}));
    const shown = String(byId('tip').innerHTML);
    if (!shown.includes('Claude')) {
      problems.push('the breakdown does not name the app: ' + shown.slice(0, 120));
    }
    // 790 MB bundle, 11 GB of data, and a cache that is empty.
    if (!shown.includes('753 MB')) {
      problems.push("the bundle size is missing: " + shown.slice(0, 200));
    }
    if (!shown.includes('10.2 GB')) {
      problems.push('the data size is missing: ' + shown.slice(0, 240));
    }
    if (!/caches[\s\S]{0,80}\u2014/.test(shown)) {
      problems.push('an empty cache is not reported as empty: ' + shown.slice(0, 260));
    }
    if (!shown.includes('93%')) {
      problems.push('the shares are missing: ' + shown.slice(0, 260));
    }
  }
}

// ---- the treemap -----------------------------------------------------------
//
// Area is the claim: a folder twice the size gets twice the rectangle. That is
// arithmetic and can be checked as arithmetic, which is worth more than looking
// at it, because a layout can be plausible and wrong by a factor of two.
const squarify = (globalThis.__page || {}).squarify;
const colourFor = (globalThis.__page || {}).colourFor;
if (typeof squarify === 'function') {
  // A real disk, not five tidy numbers: a couple of giants and a long tail.
  // Five similar items land in one or two rows, where slicing and squarifying
  // produce the same picture and neither the aspect check nor the coverage
  // check can tell them apart.
  const items = [4200, 3100, 1800, 900, 640, 480, 310, 220, 150, 90, 60, 40,
                 25, 12, 6].map((v) => ({value: v}));
  const W = 1000, H = 460;
  const boxes = squarify(items, 0, 0, W, H);

  if (boxes.length !== items.length) {
    problems.push('the treemap dropped ' + (items.length - boxes.length) + ' items');
  }
  const total = items.reduce((a, i) => a + i.value, 0);
  const scale = (W * H) / total;
  boxes.forEach((b) => {
    const want = b.item.value * scale;
    const got = b.w * b.h;
    if (Math.abs(got - want) / want > 0.02) {
      problems.push('a tile is ' + (got / want).toFixed(2) +
                    'x the area its size calls for');
    }
  });
  // Filling the box: the areas summing correctly is not the same as the
  // rectangles covering the space they were given.
  const covered = boxes.reduce((a, b) => a + b.w * b.h, 0);
  if (Math.abs(covered - W * H) / (W * H) > 0.02) {
    problems.push('the tiles cover ' + (covered / (W * H) * 100).toFixed(1) +
                  '% of the frame');
  }
  // Inside it, and not overlapping.
  boxes.forEach((b) => {
    if (b.x < -0.5 || b.y < -0.5 || b.x + b.w > W + 0.5 || b.y + b.h > H + 0.5) {
      problems.push('a tile falls outside the frame');
    }
  });
  for (let i = 0; i < boxes.length; i++) {
    for (let j = i + 1; j < boxes.length; j++) {
      const a = boxes[i], b = boxes[j];
      const overlap = Math.max(0, Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x)) *
                      Math.max(0, Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y));
      if (overlap > 1) { problems.push('two tiles overlap by ' + overlap.toFixed(0) + 'px'); }
    }
  }
  // Squarified, not sliced: the whole point is that no tile is a sliver.
  const worst = Math.max(...boxes.map((b) => Math.max(b.w / b.h, b.h / b.w)));
  if (worst > 6) {
    problems.push('the worst tile is ' + worst.toFixed(0) +
                  ':1. Squarifying exists to stop that; slicing gives 40:1');
  }
  // And a colour follows the name rather than the order.
  if (typeof colourFor === 'function') {
    if (colourFor('Library') !== colourFor('Library')) {
      problems.push('a folder does not keep its colour');
    }
    // And different folders mostly get different colours. Keying on something
    // like the length of the name is stable and useless: every eight-letter
    // folder comes out identical.
    const names = ['Library', 'Downloads', 'Movies', 'Documents', 'Music',
                   'Pictures', 'Desktop', 'Developer', 'Applications',
                   'Containers', 'Caches', 'node_modules'];
    const distinct = new Set(names.map(colourFor)).size;
    if (distinct < 5) {
      problems.push('twelve folders share only ' + distinct + ' colours');
    }
    // The precise property: the colour comes from the name, not from something
    // incidental about it. Keying on the length is stable and passes every
    // check above while giving Movies and Caches the same colour for ever.
    if (colourFor('Movies') === colourFor('Caches')) {
      problems.push('two six-letter folders always share a colour');
    }
  }
} else {
  problems.push('there is no treemap');
}

const clicks = listeners.document.click || [];
if (!clicks.length) { problems.push('nothing listens for a click on the document'); }

const fire = (target) => clicks.forEach((fn) => fn({
  target, stopPropagation(){}, preventDefault(){}
}));

const pop = byId('whatpop');
fire(icon);
await new Promise((r) => setImmediate(r));
await new Promise((r) => setImmediate(r));

if (pop.hidden) { problems.push('pressing the icon did not open the explanation'); }
if (!pop.style.left || !pop.style.top) {
  problems.push('the explanation was not positioned at all');
}
// Anchored to the icon at (900,700), so anything near the middle of a
// 1400x900 window means it went back to being a centred dialog.
const left = parseInt(pop.style.left, 10);
if (left < 600) {
  problems.push('the explanation is at left=' + left + ', not under the icon');
}
if (byId('whattitle').textContent !== 'Spotlight') {
  problems.push('the explanation did not fill in: ' +
                JSON.stringify(byId('whattitle').textContent));
}
if (!String(byId('whatbody').innerHTML).includes('152')) {
  problems.push("this machine's own figures are missing from the explanation");
}
if (icon.getAttribute('aria-expanded') !== 'true') {
  problems.push('the icon does not report itself as expanded');
}
// The finding's own advice, ahead of anything the catalogue says: it is the
// part that knows about this machine at this moment.
const shown = String(byId('whatbody').innerHTML);
if (!shown.includes('It stops on its own')) {
  problems.push("the finding's advice is not in the panel");
}
if (shown.indexOf('It stops on its own') > shown.indexOf('Writes the index')) {
  problems.push('the advice is below the encyclopedia, not above it');
}

// Pressing it again puts it away.
fire(icon);
if (!pop.hidden) { problems.push('pressing the icon again did not close it'); }

// So does pressing anywhere else.
fire(icon);
await new Promise((r) => setImmediate(r));
fire(make('elsewhere'));
if (!pop.hidden) { problems.push('clicking outside it did not close it'); }

// A finding with no process still gets its advice, and does not sit there
// saying "Looking it up" about nothing.
fire(bare);
await new Promise((r) => setImmediate(r));
const bareText = String(byId('whatbody').innerHTML);
if (byId('whatpop').hidden) {
  problems.push('an icon with advice but no process did not open');
}
if (!bareText.includes('Quit what you are not using')) {
  problems.push('advice without a process was not shown');
}
if (bareText.includes('Looking it up')) {
  problems.push('it looked up a process that was never named');
}
// Asserted on the catalogue text rather than on the placeholder: a lookup with
// an empty name still resolves and overwrites the panel, and the placeholder
// never appears either way.
if (bareText.includes('Writes the index')) {
  problems.push('it looked up a process that was never named');
}

// Pressing a different advice-only icon switches to it rather than closing.
fire(bare2);
await new Promise((r) => setImmediate(r));
if (byId('whatpop').hidden) {
  problems.push('pressing a second advice-only icon closed the panel instead');
}
if (!String(byId('whatbody').innerHTML).includes('Leave it plugged in')) {
  problems.push('the second icon showed the first icon\'s advice');
}
fire(bare2);
if (!byId('whatpop').hidden) {
  problems.push('the advice-only panel would not close');
}

if (problems.length) {
  problems.forEach((p) => console.log('FAIL ' + p));
  process.exit(1);
}
console.log('OK');
