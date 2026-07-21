// register.mjs — installs the `.js` -> `.ts` resolve hook (see hooks.mjs).
//
// Created 2026-07-21 (CS-1). Passed via `--import` so the hook is registered
// before the entry module is resolved. It has to be its own file: Node runs
// module-customization hooks on a separate thread and loads them by specifier,
// so they cannot be declared inline in the entry.

import { register } from 'node:module';

register('./hooks.mjs', import.meta.url);
