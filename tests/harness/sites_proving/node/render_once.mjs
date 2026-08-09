// render_once.mjs — SHORT-LIVED render driver. The other arm of the SG-1
// sidecar-vs-per-render measurement.
// Created for SG-1 (sites proving harness).
//
// WHAT: reads ONE request as JSON on stdin, imports the once-built bundle,
// renders, writes one JSON response on stdout, exits. Every render pays the
// process spawn + bundle import cost — which is precisely the number the
// measurement exists to put next to the sidecar's.
//
// The response shape is byte-compatible with sidecar.mjs so the Python side can
// drive either arm through one code path and compare like with like. stdout is
// protected from stray console output for the same reason as the sidecar.
import { pathToFileURL } from 'node:url';

const ENTRY = process.argv[2];
if (!ENTRY) {
  process.stderr.write('usage: node render_once.mjs <path-to-dist/entry.js>\n');
  process.exit(2);
}

for (const level of ['log', 'info', 'warn', 'debug', 'trace']) {
  console[level] = (...args) => process.stderr.write(`${args.join(' ')}\n`);
}

const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);

let req;
try {
  req = JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}');
} catch (err) {
  process.stdout.write(`${JSON.stringify({ id: null, ok: false, error: `bad request json: ${err.message}` })}\n`);
  process.exit(0);
}

const mod = await import(pathToFileURL(ENTRY).href);

try {
  const { head, body } = mod.renderPage(req.spec, { formAction: req.formAction });
  process.stdout.write(`${JSON.stringify({ id: req.id, ok: true, head, body, meta: mod.rendererMeta ?? {} })}\n`);
} catch (err) {
  process.stdout.write(
    `${JSON.stringify({ id: req.id, ok: false, error: err instanceof Error ? err.message : String(err) })}\n`,
  );
}
