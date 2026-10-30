// build.mjs — builds the site renderer ONCE.
// Created for SG-1 (sites proving harness).
//
// WHAT: produces a reusable server-render bundle of
// `<form action=…><Ripple {spec} {onEvent} /></form>` plus the whole Ripple
// widget registry, compiled for `svelte/server`. Run it once; every subsequent
// render is `import(dist/entry.js)` + `renderPage(spec)`, with NO installer and
// NO compiler in the render path.
//
// WHY a generated build dir (`node/.build/`, gitignored): the vendored Ripple
// tarball lives OUTSIDE the pocketpaw repo (workspace root
// `deploy/coolify/vendor/ripple-pkg/`), so a committed package.json can't name
// it with a stable relative path. build.mjs resolves it at build time and writes
// package.json into .build/, which keeps the committed source to this script
// plus the Svelte/JS sources — nothing machine-specific enters git.
//
// The build tries the self-contained shape first (`ssr.noExternal: true`, one
// file, no node_modules at render time) and falls back to the curated
// noExternal list from paw-sites' vite.config.ts.tmpl if that bundle won't
// import or won't render. Whichever wins is recorded in renderer-manifest.json,
// because "single file" vs "bundle + resident node_modules" changes what a
// later slice has to ship to the edge.
//
// Usage:
//   node build.mjs                        # auto-resolve ripple, auto shape
//   PAW_RIPPLE_PKG=<path> node build.mjs  # pin the ripple tarball or dir
//   PAW_SSR_NO_EXTERNAL=list node build.mjs   # skip the self-contained attempt
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import {
  cpSync,
  existsSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const BUILD_DIR = join(HERE, '.build');

/**
 * A spec small enough to smoke-render, big enough to produce real markup.
 *
 * MUST be `{ui: <node>}`, not a bare `{type, children}` node: ripple's
 * normalizeSpec only recognizes a UniversalSpec (has `intent`) or a legacy
 * UISpec (has `ui`), and a bare node falls through to an empty container that
 * renders NOTHING — observed here as a silent empty body, exactly the trap
 * paw-sites/src/spec-embed.ts documents. The Python `render()` wraps bare nodes
 * the same way; this constant carries the invariant into the build so a broken
 * renderer can never pass the smoke check by rendering nothing.
 */
const SMOKE_SPEC = {
  ui: {
    type: 'container',
    children: [{ type: 'heading', props: { text: 'renderer smoke' } }],
  },
};

function log(msg) {
  process.stdout.write(`[build] ${msg}\n`);
}

/**
 * Find the Ripple package to build against.
 *
 * Preference order, and why:
 *  1. $PAW_RIPPLE_PKG — an explicit pin always wins.
 *  2. The vendored tarball under `deploy/coolify/vendor/ripple-pkg/` — this is
 *     the FROZEN artifact production vendoring installs, so building against it
 *     is what the shipped image would do.
 *  3. A tarball sitting in the sibling `ripple/` source repo.
 *  4. The `ripple/` repo itself (`file:` dep). Last resort: its `dist/` is a
 *     working tree and can drift from what was vendored.
 */
function resolveRipple() {
  const pinned = process.env.PAW_RIPPLE_PKG;
  if (pinned) {
    if (!existsSync(pinned)) {
      throw new Error(`PAW_RIPPLE_PKG does not exist: ${pinned}`);
    }
    return { path: resolve(pinned), source: 'PAW_RIPPLE_PKG' };
  }

  const roots = [];
  let cursor = HERE;
  while (true) {
    roots.push(cursor);
    // A git worktree lives outside the workspace, so also probe siblings.
    roots.push(join(cursor, '..', 'paw-workspace'));
    const up = dirname(cursor);
    if (up === cursor) break;
    cursor = up;
  }

  for (const root of roots) {
    const vendor = join(root, 'deploy', 'coolify', 'vendor', 'ripple-pkg');
    const tgz = existsSync(vendor)
      ? readdirSync(vendor).filter((f) => f.endsWith('.tgz')).sort()
      : [];
    if (tgz.length) {
      return { path: resolve(join(vendor, tgz[tgz.length - 1])), source: 'vendored-tarball' };
    }
  }

  for (const root of roots) {
    const repo = join(root, 'ripple');
    if (!existsSync(join(repo, 'package.json'))) continue;
    const tgz = readdirSync(repo).filter((f) => f.endsWith('.tgz')).sort();
    if (tgz.length) {
      return { path: resolve(join(repo, tgz[tgz.length - 1])), source: 'source-repo-tarball' };
    }
    return { path: resolve(repo), source: 'source-repo-dir' };
  }

  throw new Error(
    'could not find @ripple-ui/svelte. Set PAW_RIPPLE_PKG to the .tgz or the ripple repo.',
  );
}

function run(cmd, args, opts = {}) {
  // shell:true ONLY for bare command names (`bun`), which on Windows resolve
  // through .cmd/.exe shims. Never for an absolute path — cmd.exe splits
  // `C:\Program Files\nodejs\node.exe` at the space and the build dies with
  // "'C:\Program' is not recognized".
  const res = spawnSync(cmd, args, {
    cwd: BUILD_DIR,
    stdio: 'inherit',
    shell: process.platform === 'win32' && !/[\\/]/.test(cmd),
    ...opts,
  });
  if (res.error) throw res.error;
  return res.status ?? 1;
}

function stageSources(ripple) {
  rmSync(join(BUILD_DIR, 'src'), { recursive: true, force: true });
  mkdirSync(BUILD_DIR, { recursive: true });
  cpSync(join(HERE, 'src'), join(BUILD_DIR, 'src'), { recursive: true });
  cpSync(join(HERE, 'vite.config.mjs'), join(BUILD_DIR, 'vite.config.mjs'));

  // Versions mirror paw-sites/templates/package.json.tmpl's devDependencies for
  // svelte / vite / vite-plugin-svelte. SvelteKit, the adapter, Tailwind,
  // @noble/hashes and valibot are NOT here: Kit and the adapter are the per-site
  // project machinery this slice removes, Tailwind is a later slice, and the two
  // runtime libs belong to the submit endpoint, not the renderer.
  writeFileSync(
    join(BUILD_DIR, 'package.json'),
    `${JSON.stringify(
      {
        name: 'paw-sites-ssr-renderer',
        private: true,
        type: 'module',
        dependencies: {
          '@ripple-ui/svelte': `file:${ripple.path.replace(/\\/g, '/')}`,
        },
        devDependencies: {
          '@sveltejs/vite-plugin-svelte': '^6.0.0',
          svelte: '^5.0.0',
          vite: '^6.0.0',
        },
      },
      null,
      2,
    )}\n`,
  );
}

function installedRippleVersion() {
  const pkg = join(BUILD_DIR, 'node_modules', '@ripple-ui', 'svelte', 'package.json');
  if (!existsSync(pkg)) throw new Error('@ripple-ui/svelte did not install');
  return JSON.parse(readFileSync(pkg, 'utf8')).version;
}

function writeRippleVersion(version, source) {
  writeFileSync(
    join(BUILD_DIR, 'src', 'ripple-version.js'),
    '// Generated by build.mjs from the ACTUALLY INSTALLED @ripple-ui/svelte.\n' +
      `export const RIPPLE_VERSION = ${JSON.stringify(version)};\n` +
      `export const RIPPLE_SOURCE = ${JSON.stringify(source)};\n`,
  );
}

/** Build, then prove the artifact imports AND renders. Returns null on success. */
async function buildAndSmoke(shape) {
  const vite = join(BUILD_DIR, 'node_modules', 'vite', 'bin', 'vite.js');
  if (!existsSync(vite)) throw new Error('vite did not install');

  const status = run(process.execPath, [vite, 'build'], {
    env: { ...process.env, PAW_SSR_NO_EXTERNAL: shape },
  });
  if (status !== 0) return `vite build exited ${status}`;

  const entry = join(BUILD_DIR, 'dist', 'entry.js');
  if (!existsSync(entry)) return 'vite build produced no dist/entry.js';

  try {
    const mod = await import(`${pathToFileURL(entry).href}?t=${Date.now()}`);
    const { body } = mod.renderPage(SMOKE_SPEC);
    if (!body || !body.includes('renderer smoke')) {
      return 'smoke render produced no content';
    }
  } catch (err) {
    return `smoke render threw: ${err instanceof Error ? err.message : String(err)}`;
  }
  return null;
}

function copyAssets() {
  // Ripple owns its CSS (Contract clause 5 — sites does not apply CSS itself),
  // so the bundle carries ripple's stylesheets as assets rather than a
  // Tailwind-compiled sheet. Tailwind utility generation is a later slice.
  const from = join(BUILD_DIR, 'node_modules', '@ripple-ui', 'svelte', 'dist');
  const to = join(BUILD_DIR, 'dist', 'assets');
  mkdirSync(to, { recursive: true });
  const copied = [];
  for (const name of ['theme.css', 'styles.css']) {
    if (existsSync(join(from, name))) {
      cpSync(join(from, name), join(to, name));
      copied.push(`assets/${name}`);
    }
  }
  return copied;
}

function sha256(path) {
  return createHash('sha256').update(readFileSync(path)).digest('hex');
}

async function main() {
  const ripple = resolveRipple();
  log(`ripple: ${ripple.path} (${ripple.source})`);

  stageSources(ripple);

  // bun, not npm: ~/.npmrc sets ignore-scripts=true, which would skip esbuild's
  // postinstall and leave vite broken. Supply-chain minimum-release-age stays
  // enforced by ~/.bunfig.toml — never overridden here.
  log('bun install (ONE TIME — no installer runs during a render)');
  const installed = run('bun', ['install', '--no-save']);
  if (installed !== 0) throw new Error(`bun install exited ${installed}`);

  const version = installedRippleVersion();
  writeRippleVersion(version, ripple.source);
  log(`installed @ripple-ui/svelte ${version}`);

  const attempts = [];
  const shapes = process.env.PAW_SSR_NO_EXTERNAL === 'list' ? ['list'] : ['all', 'list'];
  let shape = null;
  for (const candidate of shapes) {
    log(`building SSR bundle (noExternal=${candidate})`);
    const failure = await buildAndSmoke(candidate);
    attempts.push({ shape: candidate, ok: failure === null, failure });
    if (failure === null) {
      shape = candidate;
      break;
    }
    log(`  -> rejected: ${failure}`);
  }
  if (shape === null) {
    writeFileSync(
      join(BUILD_DIR, 'renderer-manifest.json'),
      `${JSON.stringify({ ok: false, attempts }, null, 2)}\n`,
    );
    throw new Error(`no SSR bundle shape worked: ${JSON.stringify(attempts)}`);
  }

  const assets = copyAssets();
  const entry = join(BUILD_DIR, 'dist', 'entry.js');
  const bundleSha = sha256(entry);

  const manifest = {
    ok: true,
    // renderer_version pins BOTH inputs that can change the output: the ripple
    // version and the exact bytes of the built bundle.
    renderer_version: `ripple-${version}+bundle-${bundleSha.slice(0, 12)}`,
    ripple_version: version,
    ripple_source: ripple.source,
    ripple_path: ripple.path,
    bundle_shape: shape === 'all' ? 'self-contained' : 'curated-noExternal',
    // 'list' leaves non-curated deps external, so node_modules must stay next to
    // the bundle at render time. A later slice needs to know this to ship it.
    needs_node_modules: shape !== 'all',
    bundle_sha256: bundleSha,
    entry: 'dist/entry.js',
    assets,
    attempts,
    built_at: new Date().toISOString(),
    node_version: process.version,
  };
  writeFileSync(
    join(BUILD_DIR, 'renderer-manifest.json'),
    `${JSON.stringify(manifest, null, 2)}\n`,
  );
  log(`done: ${manifest.renderer_version} (${manifest.bundle_shape})`);
}

main().catch((err) => {
  process.stderr.write(`[build] FAILED: ${err?.stack ?? err}\n`);
  process.exit(1);
});
