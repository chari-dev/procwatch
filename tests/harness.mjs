// Enough of a browser to execute the dashboard's script once.
//
// Not a rendering test: it answers every DOM call with a stand-in and asserts
// only that the page can be loaded without throwing. Three separate releases
// have shipped a page that died on the first line it reached -- a missing
// element, a variable used above its definition, a stale reference after a
// rewrite -- and each one looked to the reader like "everything says Loading".
import fs from 'fs';

const code = fs.readFileSync(process.argv[2], 'utf8');

// A 2D canvas context. The element proxy below answers an unknown property
// with another proxy, which is fine for `foo.bar` but not for `foo.bar()` --
// a proxy over an object is not callable, so `canvas.getContext('2d')` threw
// "object is not a function". The dashboard never hit it because its canvas
// work happens inside handlers the harness does not fire; the network monitor
// draws its globe at load, so it did.
const ctx2d = () => new Proxy({
  canvas: {width: 800, height: 400},
  measureText: () => ({width: 40}),
  createLinearGradient: () => ({addColorStop(){}}),
  createRadialGradient: () => ({addColorStop(){}}),
  createPattern: () => null,
  getImageData: () => ({data: new Uint8ClampedArray(4)}),
  isPointInPath: () => false,
  setLineDash(){}, getLineDash: () => []
}, {get: (t, k) => (k in t ? t[k] : () => undefined), set: () => true});

const el = () => new Proxy({
  getContext: () => ctx2d(),
  // Layout reads have to be numbers. The catch-all below answers an unknown
  // property with another element proxy, and an object in arithmetic makes
  // JavaScript look for Symbol.toPrimitive -- which the catch-all also
  // answered with an object, so `canvas.clientWidth * dpr` threw "object is
  // not a function" from inside the multiply. A browser returns 0 or a real
  // size here, never something you cannot do maths on.
  clientWidth: 800, clientHeight: 400, offsetWidth: 800, offsetHeight: 400,
  scrollWidth: 800, scrollHeight: 400, scrollTop: 0, scrollLeft: 0,
  width: 800, height: 400, offsetTop: 0, offsetLeft: 0,
  dataset: {}, style: {}, children: [], options: [],
  classList: {toggle(){}, add(){}, remove(){}, contains: () => false},
  appendChild(){}, addEventListener(){}, removeEventListener(){},
  setAttribute(){}, getAttribute: () => 0, removeAttribute(){},
  querySelectorAll: () => [], querySelector: () => null, closest: () => null,
  getBoundingClientRect: () => ({left:0, top:0, right:0, bottom:0, width:800, height:200}),
  innerHTML: '', textContent: '', value: '', hidden: false,
  focus(){}, blur(){}, select(){}, scrollIntoView(){}
}, {get: (t, k) => {
      if (k in t) { return t[k]; }
      // Symbols are the language asking a question, not the page reading a
      // property: Symbol.toPrimitive, Symbol.iterator, and the rest have to
      // come back undefined so the default behaviour applies.
      if (typeof k === 'symbol') { return undefined; }
      return el();
    },
    set: (t, k, v) => { t[k] = v; return true; }});

globalThis.document = {
  getElementById: () => el(), querySelectorAll: () => [], querySelector: () => el(),
  createElement: () => el(), createElementNS: () => el(),
  addEventListener(){}, body: el(), documentElement: el()
};
globalThis.window = {addEventListener(){}, devicePixelRatio: 2, innerWidth: 1400,
                     innerHeight: 900, location: {href: ''}, confirm: () => false};
// Bare `location`, not only `window.location`. A page may reach for either and
// a browser answers both; stubbing only the property left the network monitor
// throwing here on a line that works perfectly in a browser.
globalThis.location = {href: '', search: '', hash: '', pathname: '/',
                       protocol: 'http:', host: '127.0.0.1:8790'};
globalThis.matchMedia = () => ({matches: false, addEventListener(){}, addListener(){}});
globalThis.getComputedStyle = () => ({getPropertyValue: (n) => n});
globalThis.localStorage = {getItem: () => null, setItem(){}};
globalThis.fetch = async () => ({
  ok: true, status: 200,
  json: async () => ({series: [], system: [], gaps: [], tier: 'raw',
                      rules: [], events: [], metrics: {}, groups: []}),
  text: async () => ''
});
// Run a bounded number of frames. Calling the callback straight through is
// what makes the first draw actually execute -- which is the point -- but a
// page whose frame callback asks for the next frame then recurses until the
// stack runs out. A browser returns to the event loop between frames; this
// stops after a few instead, which exercises the loop without pretending to
// be one.
let frames = 0;
globalThis.requestAnimationFrame = (f) => {
  if (frames++ < 3) { f(performance.now()); }
  return frames;
};
globalThis.cancelAnimationFrame = () => {};
globalThis.performance = globalThis.performance || {now: () => 0};
globalThis.setInterval = () => 0; globalThis.clearInterval = () => {};
globalThis.setTimeout = () => 0; globalThis.clearTimeout = () => {};

try {
  new Function(code)();
  console.log('OK');
} catch (error) {
  console.log('THREW ' + error.constructor.name + ': ' + error.message);
  console.log((error.stack || '').split('\n').slice(1, 3).join('\n'));
  process.exit(1);
}
