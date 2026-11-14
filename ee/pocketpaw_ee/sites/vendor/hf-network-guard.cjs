// hf-network-guard.cjs — cuts the HyperFrames render browser off the network.
//
// Preloaded into the HyperFrames CLI with NODE_OPTIONS=--require by
// pocketpaw_ee/sites/motion_video.py. The composition it renders is written by
// an LLM, so the headless Chrome that renders it must reach NOTHING except the
// CLI's own project file server (http://localhost:<port>, serving only the temp
// project dir). HyperFrames exposes no way to pass Chrome flags, so this wraps
// every child_process launcher (spawn, spawnSync, execFile, execFileSync, exec,
// execSync, fork). A launch whose executable basename matches /chrom|headless/i
// gets, with any proxy/resolver flag it already had stripped:
//   --proxy-server=http://127.0.0.1:9            every request goes to a dead proxy
//   --proxy-bypass-list=<-loopback>;localhost:P  ...except the ports this process
//                                                is listening on AT THAT MOMENT
//                                                (loopback is no longer implicit)
//   --host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost   no DNS either
// IP literals (169.254.169.254, 127.0.0.1:8888) hit the dead proxy.
//
// $POCKETPAW_HF_GUARD_FILE gets one line per browser launch: the PID of a
// rewritten async launch, `SYNC <cmd>` for a rewritten synchronous one (already
// exited), or `UNGUARDED <what>` when the launch could not be rewritten (no argv
// array, a shell string, a fork) — and such a launch is REFUSED (throws).
// Python discards the render unless the file has a PID and no UNGUARDED line,
// and uses the PIDs to kill Chrome on timeout: puppeteer launches it detached
// (its own process group on POSIX), so killing the CLI's group misses it.
//
// Upstream note: HyperFrames' render file server (@hono/node-server `serve`
// with no hostname) listens on all interfaces, so the temp project is readable
// from the network for the length of a render. Run renders on a host whose
// inbound ports are firewalled (the enterprise container publishes only 8888).
"use strict";

const childProcess = require("node:child_process");
const fs = require("node:fs");
const net = require("node:net");
const path = require("node:path");
const { syncBuiltinESMExports } = require("node:module");

const guardFile = process.env.POCKETPAW_HF_GUARD_FILE;
const listening = new Set();

const originalListen = net.Server.prototype.listen;
net.Server.prototype.listen = function patchedListen(...args) {
  this.once("listening", () => {
    const address = this.address();
    if (!address || typeof address !== "object" || !address.port) return;
    const port = address.port;
    listening.add(port);
    this.once("close", () => listening.delete(port));
  });
  return originalListen.apply(this, args);
};

const BROWSER_RE = /chrom|headless/i;
const OVERRIDDEN_RE = /^--(proxy-server|proxy-bypass-list|proxy-pac-url|host-resolver-rules|proxy-auto-detect)(=|$)/;

function record(line) {
  if (!guardFile) return;
  try {
    fs.appendFileSync(guardFile, `${line}\n`);
  } catch (_err) {
    // An unwritable guard file reads as "not applied" and fails the render.
  }
}

function isBrowser(command) {
  return typeof command === "string" && BROWSER_RE.test(path.basename(command.replace(/^["']|["']$/g, "")));
}

function guardedArgs(args) {
  const bypass = ["<-loopback>"];
  for (const port of listening) bypass.push(`localhost:${port}`);
  return [
    ...args.filter((arg) => !OVERRIDDEN_RE.test(String(arg))),
    "--proxy-server=http://127.0.0.1:9",
    `--proxy-bypass-list=${bypass.join(";")}`,
    "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost",
    "--dns-prefetch-disable",
    "--disable-background-networking",
    "--no-pings",
  ];
}

function refuse(what) {
  record(`UNGUARDED ${String(what).slice(0, 200)}`);
  throw new Error(`hf-network-guard: refused an unguarded browser launch (${what})`);
}

// Launchers that take (file, args[], options): rewrite args when it is a browser.
for (const name of ["spawn", "spawnSync", "execFile", "execFileSync"]) {
  const original = childProcess[name];
  const sync = name.endsWith("Sync");
  childProcess[name] = function guarded(command, args, ...rest) {
    if (!isBrowser(command)) return original.call(this, command, args, ...rest);
    if (!Array.isArray(args)) refuse(command);
    const result = original.call(this, command, guardedArgs(args), ...rest);
    if (sync) {
      record(`SYNC ${path.basename(command)}`);
    } else {
      record(result && result.pid ? String(result.pid) : `UNGUARDED ${command} (no pid)`);
    }
    return result;
  };
}

// Launchers that take a shell string or a module path: a browser launched this
// way cannot be rewritten safely, so it is refused.
for (const name of ["exec", "execSync", "fork"]) {
  const original = childProcess[name];
  childProcess[name] = function guarded(command, ...rest) {
    const head = typeof command === "string" ? (command.trim().match(/^("[^"]*"|'[^']*'|\S+)/) || [""])[0] : "";
    if (isBrowser(head)) refuse(command);
    return original.call(this, command, ...rest);
  };
}

syncBuiltinESMExports();
