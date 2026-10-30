// generate.mjs — materialize ONE site project per engine, the way the control
// plane does, then stop. Created for SR-1 (per-engine build cost).
//
// WHAT: `bun generate.mjs <engine> <outDir>` writes a generated project for
// engine ∈ {react, svelte, ripple-static, ripple-dynamic} and prints one JSON
// line describing it. It does NOT install and does NOT build — the Python side
// owns those so it can time each phase separately.
//
// WHY it calls generateSite() directly instead of `paw-sites-gen build`: the CLI
// is a thin argv/IO wrapper around exactly this function (src/cli.ts's runBuild
// is 10 lines: read --input, call generateSite, print the result), so calling it
// directly measures the same work with one less process. The measurement's
// subject is install+build, not argv parsing.
//
// WHY the fixtures come from paw-sites/tests/fixtures: those source maps and
// specs are the ones the repo's own real-build tests use, so the cost measured
// here is the cost of building something known to actually build. Inventing a
// fixture would risk measuring a build that fails, or one unrepresentatively
// small. Read-only — nothing is written back to paw-sites.
//
// The `motion` + ripple dep rewrite MIRRORS
// ee/pocketpaw_ee/sites/generator_client.py::_rewrite_ripple_dep, which runs on
// every real publish before install. Skipping it would measure a build the
// control plane never runs (and ripple's build would fail to resolve motion).
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';

const SITES_REPO = process.env.PAW_SITES_REPO ?? 'D:/paw-workspace/paw-sites';
const RIPPLE_DEP =
  process.env.PAW_SITES_RIPPLE_DEP ??
  'file:D:/paw-workspace/deploy/coolify/vendor/ripple-pkg/ripple-ui-svelte-0.5.0.tgz';
// Kept in lockstep with ripple's own pin, per deploy/coolify/.env.example.
const MOTION_DEP = process.env.PAW_SITES_MOTION_DEP ?? '^12.40.0';

const [engine, outDir] = process.argv.slice(2);
if (!engine || !outDir) {
  process.stderr.write('usage: bun generate.mjs <engine> <outDir>\n');
  process.exit(2);
}

const src = (rel) => `file:///${join(SITES_REPO, rel).replace(/\\/g, '/')}`;

const siteConfig = {
  siteId: `sr1_${engine.replace(/-/g, '_')}`,
  title: 'SR-1 Cost Probe',
  captureApiBase: 'https://api.paw.example/api/v1',
  captureSignedKey: 'pp_tok_sr1_not_a_real_key',
};

/** Build the GenerateInput for one engine from the repo's own fixtures. */
async function inputFor(name) {
  if (name === 'react') {
    const { reactSource } = await import(src('tests/fixtures/react-source.ts'));
    return {
      engine: 'react',
      source: reactSource(),
      theme: {},
      // The MT-1 flag: true means this site's own JS is load-bearing, which is
      // the production default (sites_keep_client_bundle_default is True). A
      // hydrating build emits client chunks, so it is the more expensive and
      // more representative shape to measure.
      siteConfig: { ...siteConfig, keepsClientBundle: true },
    };
  }

  if (name === 'svelte') {
    const { svelteSource } = await import(src('tests/fixtures/svelte-source.ts'));
    return {
      engine: 'svelte',
      source: svelteSource(),
      theme: {},
      siteConfig: { ...siteConfig, keepsClientBundle: true },
    };
  }

  if (name === 'ripple-static') {
    // The dentist spec: a marketing page with a lead form — the archetypal
    // static ripple site, and the shape SG-1 rendered.
    const spec = JSON.parse(await readFile(join(SITES_REPO, 'tests/fixtures/dentist-spec.json'), 'utf8'));
    return {
      rippleSpec: spec,
      theme: {},
      siteConfig: { ...siteConfig, keepsClientBundle: true },
    };
  }

  if (name === 'ripple-dynamic') {
    // The booking spec carries data bindings, so the generator scaffolds the D1
    // migration + remote functions. d1DatabaseId is what makes it dynamic; the
    // repo's own dynamic-site test passes 'local' the same way.
    const spec = JSON.parse(await readFile(join(SITES_REPO, 'tests/fixtures/booking-spec.json'), 'utf8'));
    return {
      rippleSpec: spec,
      theme: {},
      siteConfig: { ...siteConfig, keepsClientBundle: true, d1DatabaseId: 'local' },
    };
  }

  throw new Error(`unknown engine ${name}`);
}

/** Mirror generator_client.py::_rewrite_ripple_dep. */
async function rewriteDeps(dir) {
  const path = join(dir, 'package.json');
  let pkg;
  try {
    pkg = JSON.parse(await readFile(path, 'utf8'));
  } catch {
    return { rewritten: false, reason: 'no package.json (html-shaped output)' };
  }
  const deps = pkg.dependencies ?? {};
  if (!('@ripple-ui/svelte' in deps)) {
    return { rewritten: false, reason: 'no ripple dep (source-engine project)' };
  }
  deps['@ripple-ui/svelte'] = RIPPLE_DEP;
  if (!('motion' in deps)) deps.motion = MOTION_DEP;
  pkg.dependencies = deps;
  await writeFile(path, `${JSON.stringify(pkg, null, 2)}\n`);
  return { rewritten: true, ripple_dep: RIPPLE_DEP, motion_dep: deps.motion };
}

const { generateSite } = await import(src('src/index.ts'));

await mkdir(outDir, { recursive: true });
const started = performance.now();
const result = await generateSite(await inputFor(engine), outDir);
const generateMs = performance.now() - started;

const deps = await rewriteDeps(outDir);
let buildScript = null;
try {
  const pkg = JSON.parse(await readFile(join(outDir, 'package.json'), 'utf8'));
  buildScript = pkg.scripts?.build ?? null;
} catch {
  /* html-shaped output has no package.json */
}

process.stdout.write(
  `${JSON.stringify({
    ok: true,
    engine,
    project_dir: outDir,
    generate_ms: Math.round(generateMs * 100) / 100,
    resolved_engine: result.engine ?? 'ripple',
    ripple_version: result.rippleVersion ?? null,
    is_dynamic: result.bindings?.isDynamic ?? null,
    static_output_rel: result.staticDir ?? null,
    build_script: buildScript,
    deps,
  })}\n`,
);
