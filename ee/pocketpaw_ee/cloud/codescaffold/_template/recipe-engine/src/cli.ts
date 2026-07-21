#!/usr/bin/env tsx
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { applyToSourceMap } from './index.js';
import { readSourceMap, writeSourceMap } from './sourcemap.js';
import { RecipeError, type PlanChange } from './types.js';

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');

interface Args {
	command: string;
	recipes: string[];
	base: string;
	out: string | null;
	recipesDir: string;
	dryRun: boolean;
}

function parseArgs(argv: string[]): Args {
	const args: Args = {
		command: argv[0] ?? '',
		recipes: [],
		base: path.join(REPO_ROOT, 'base'),
		out: null,
		recipesDir: path.join(REPO_ROOT, 'recipes'),
		dryRun: false
	};
	for (let i = 1; i < argv.length; i++) {
		const a = argv[i];
		if (a === '--dry-run') args.dryRun = true;
		else if (a === '--base') args.base = path.resolve(argv[++i]);
		else if (a === '--out') args.out = path.resolve(argv[++i]);
		else if (a === '--recipes') args.recipesDir = path.resolve(argv[++i]);
		else if (a.startsWith('--')) throw new RecipeError(`unknown flag ${a}`);
		else args.recipes.push(a);
	}
	return args;
}

function printPlan(plan: PlanChange[]): void {
	const width = Math.max(...plan.map((c) => c.path.length), 4);
	for (const c of plan) {
		const mark = c.noop ? '·' : '+';
		const kind = c.kind.padEnd(9);
		console.log(`  ${mark} ${kind} ${c.path.padEnd(width)}  ${c.detail}  [${c.recipe}]`);
	}
	const changed = plan.filter((c) => !c.noop).length;
	console.log(`\n  ${changed} change(s), ${plan.length - changed} no-op(s).`);
}

const USAGE = `Usage:
  recipe apply <ids...> [--base <dir>] [--out <dir>] [--dry-run] [--recipes <dir>]

  Applies recipes (and their dependencies) to the base source map.
  --base       source template dir            (default: ./base)
  --out        write the composed site here   (required unless --dry-run)
  --dry-run    print the plan; write nothing
  --recipes    recipes directory              (default: ./recipes)

Examples:
  recipe apply auth stripe --dry-run
  recipe apply auth stripe --out ./out`;

async function main(): Promise<void> {
	const args = parseArgs(process.argv.slice(2));

	if (args.command !== 'apply' || args.recipes.length === 0) {
		console.log(USAGE);
		process.exit(args.command ? 1 : 0);
	}

	const source = await readSourceMap(args.base);
	const result = await applyToSourceMap(source, args.recipesDir, args.recipes);

	console.log(`\nApply order: ${result.order.join(' -> ')}`);
	if (result.secrets.length) {
		console.log(`Secrets required (names only, provisioned out of band): ${result.secrets.join(', ')}`);
	}
	console.log('');
	printPlan(result.plan);

	if (args.dryRun) {
		console.log('\nDry run — nothing written.');
		return;
	}
	if (!args.out) {
		throw new RecipeError('refusing to write in place: pass --out <dir> (or --dry-run).');
	}
	await writeSourceMap(args.out, result.sourceMap);
	console.log(`\nWrote composed site to ${args.out}`);
}

main().catch((err) => {
	if (err instanceof RecipeError) {
		console.error(`\nRecipe error: ${err.message}`);
	} else {
		console.error(err);
	}
	process.exit(1);
});
