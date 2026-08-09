// sidecar.mjs — RESIDENT render driver. One of the two arms of the SG-1
// sidecar-vs-per-render measurement.
// Created for SG-1 (sites proving harness).
//
// WHAT: imports the once-built bundle ONCE, then serves render requests forever
// over NDJSON on stdin/stdout. Request `{id, spec, formAction?}` ->
// response `{id, ok, head, body}` or `{id, ok:false, error}`. On startup it
// emits one `{ready:true, meta}` line so the caller can time cold start
// separately from a warm render.
//
// WHY stdout is protected: Ripple warns to the console (out-of-catalog node
// types, Svelte SSR notices) and any stray `console.log` would land mid-NDJSON
// and corrupt the stream. console.* is rebound to stderr before the bundle is
// imported, so stdout carries protocol frames ONLY.
import { createInterface } from 'node:readline';
import { pathToFileURL } from 'node:url';

const ENTRY = process.argv[2];
if (!ENTRY) {
  process.stderr.write('usage: node sidecar.mjs <path-to-dist/entry.js>\n');
  process.exit(2);
}

const out = (obj) => process.stdout.write(`${JSON.stringify(obj)}\n`);

for (const level of ['log', 'info', 'warn', 'debug', 'trace']) {
  console[level] = (...args) => process.stderr.write(`${args.join(' ')}\n`);
}

const mod = await import(pathToFileURL(ENTRY).href);
out({ ready: true, meta: mod.rendererMeta ?? {} });

const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
for await (const line of lines) {
  const text = line.trim();
  if (!text) continue;

  let req;
  try {
    req = JSON.parse(text);
  } catch (err) {
    out({ id: null, ok: false, error: `bad request json: ${err.message}` });
    continue;
  }
  if (req.shutdown) break;

  try {
    const { head, body } = mod.renderPage(req.spec, { formAction: req.formAction });
    out({ id: req.id, ok: true, head, body });
  } catch (err) {
    out({ id: req.id, ok: false, error: err instanceof Error ? err.message : String(err) });
  }
}
