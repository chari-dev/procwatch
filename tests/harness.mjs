// Enough of a browser to execute the dashboard's script once.
//
// Not a rendering test: it answers every DOM call with a stand-in and asserts
// only that the page can be loaded without throwing. Three separate releases
// have shipped a page that died on the first line it reached -- a missing
// element, a variable used above its definition, a stale reference after a
// rewrite -- and each one looked to the reader like "everything says Loading".
import fs from 'fs';

const code = fs.readFileSync(process.argv[2], 'utf8');
const el = () => new Proxy({
  dataset: {}, style: {}, children: [], options: [],
  classList: {toggle(){}, add(){}, remove(){}, contains: () => false},
  appendChild(){}, addEventListener(){}, removeEventListener(){},
  setAttribute(){}, getAttribute: () => 0, removeAttribute(){},
  querySelectorAll: () => [], querySelector: () => null, closest: () => null,
  getBoundingClientRect: () => ({left:0, top:0, right:0, bottom:0, width:800, height:200}),
  innerHTML: '', textContent: '', value: '', hidden: false,
  focus(){}, blur(){}, select(){}, scrollIntoView(){}
}, {get: (t, k) => k in t ? t[k] : el(), set: (t, k, v) => { t[k] = v; return true; }});

globalThis.document = {
  getElementById: () => el(), querySelectorAll: () => [], querySelector: () => el(),
  createElement: () => el(), createElementNS: () => el(),
  addEventListener(){}, body: el(), documentElement: el()
};
globalThis.window = {addEventListener(){}, devicePixelRatio: 2, innerWidth: 1400,
                     innerHeight: 900, location: {href: ''}, confirm: () => false};
globalThis.matchMedia = () => ({matches: false, addEventListener(){}, addListener(){}});
globalThis.getComputedStyle = () => ({getPropertyValue: (n) => n});
globalThis.localStorage = {getItem: () => null, setItem(){}};
globalThis.fetch = async () => ({
  ok: true, status: 200,
  json: async () => ({series: [], system: [], gaps: [], tier: 'raw',
                      rules: [], events: [], metrics: {}, groups: []}),
  text: async () => ''
});
globalThis.requestAnimationFrame = (f) => f();
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
