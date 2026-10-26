// Offline regression harness: execute the exact transformed module with fake I/O dependencies.
import { readFileSync } from 'node:fs';
import { SourceTextModule, SyntheticModule } from 'node:vm';
import { patchModule } from '../patch.mjs';

const input = JSON.parse(readFileSync(0, 'utf8'));
const fixtures = JSON.parse(readFileSync(new URL('./candidate-modules.json', import.meta.url)));
const calls = [];
const date = (value, format) => new Date(format === 'X' ? Number(value) * 1000 : value);
const jsx = (type, props) => ({ type, props });
function render(node) {
  if (node === null || node === undefined || node === false) return '';
  if (Array.isArray(node)) return node.map(render).join('');
  if (typeof node !== 'object') return String(node);
  return render(node.props?.children);
}
async function got(options) {
  calls.push(options.url);
  if (options.url.includes('/information-flow?')) return { data: { data: { items: input.items ?? [] } } };
  const id = options.url.match(/\/(?:articles|lives)\/([^?]+)/)?.[1];
  const detail = input.details?.[id];
  if (!detail) return { data: { code: 404, data: {} } };
  return { data: { code: 20000, data: detail } };
}
const dependencies = {
  './cache-CtYX8KBG.mjs': { t: { tryGet: async (_key, work) => work() } },
  './parse-date-LfqgAehp.mjs': { t: date },
  './got-CzLN7yeY.mjs': { t: got },
  'p-map': { default: async (items, work) => Promise.all(items.map(work)) },
  './route-limit-BCAfTOfE.mjs': { t: () => 50 },
  './ofetch-DgM1MIL7.mjs': { t: async (url, options) => {
    calls.push({ url, options });
    return { data: { roll_data: input.items ?? [] } };
  } },
  './invalid-parameter-C-Mj6Tow.mjs': { t: class InvalidParameterError extends Error {} },
  './cls-utils-YYIXZzci.mjs': { n: 'https://www.cls.cn', t: (value) => value },
  'hono/jsx/jsx-runtime': { Fragment: Symbol('Fragment'), jsx, jsxs: jsx },
  'hono/jsx/dom/server': { renderToString: render },
};
const compiled = Buffer.from(fixtures[input.name]);
const source = input.unpatched ? compiled : patchModule(input.name, compiled);
const module = new SourceTextModule(source.toString('utf8'));
await module.link(async (specifier) => {
  if (specifier === './not-found-CaYyolkS.mjs') {
    const errorModule = new SourceTextModule(fixtures['not-found-CaYyolkS.mjs']);
    await errorModule.link(() => { throw new Error('Unexpected error dependency'); });
    return errorModule;
  }
  const exports = dependencies[specifier];
  if (!exports) throw new Error(`Unexpected offline dependency: ${specifier}`);
  return new SyntheticModule(Object.keys(exports), function () {
    for (const [key, value] of Object.entries(exports)) this.setExport(key, value);
  });
});
await module.evaluate();
const context = { req: { param: () => input.category, query: () => input.limit } };
try {
  const result = await module.namespace.route.handler(context);
  console.log(JSON.stringify({ ok: true, calls, result }));
} catch (error) {
  console.log(JSON.stringify({ ok: false, calls, error_name: error.name, error_message: error.message }));
}
