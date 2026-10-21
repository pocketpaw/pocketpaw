// hf-network-guard.cjs — cuts the HyperFrames render browser off the network.
//
// Preloaded into the HyperFrames CLI with NODE_OPTIONS=--require by
// pocketpaw_ee/sites/motion_video.py. The composition it renders is written by
// an LLM, so the headless Chrome that renders it must reach NOTHING except the
// CLI's own project file server (http://localhost:<port>, serving only the temp
// project dir). HyperFrames exposes no way to pass Chrome flags, so this hooks
// child_process.spawn: when the spawned binary is a Chrome / headless shell it
// appends
//   --proxy-server=http://127.0.0.1:9            every request goes to a dead proxy
//   --proxy-bypass-list=<-loopback>;localhost:P  ...except this process's own
//                                                listening ports (loopback is NOT
//                                                implicitly bypassed any more)
//   --host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost   no DNS either
// and strips any proxy/resolver flag already present. Ports come from hooking
// net.Server#listen in this process (the file server listens before Chrome
// starts). IP literals (169.254.169.254, 127.0.0.1:8888) hit the dead proxy.
//
// Each guarded Chrome PID is appended to $POCKETPAW_HF_GUARD_FILE. Python treats
// an empty file after a render as "guard not applied" and refuses the output,
// and uses the PIDs to kill Chrome on timeout: puppeteer launches it detached
// (its own process group on POSIX), so killing the CLI's group misses it.
"use strict";

const childProcess = require("node:child_process");
const fs = require("node:fs");
const net = require("node:net");
const path = require("node:path");
const { syncBuiltinESMExports } = require("node:module");

const guardFile = process.env.POCKETPAW_HF_GUARD_FILE;
const ports = new Set();

const originalListen = net.Server.prototype.listen;
net.Server.prototype.listen = function patchedListen(...args) {
  this.once("listening", () => {
    const address = this.address();
    if (address && typeof address === "object" && address.port) ports.add(address.port);
  });
  return originalListen.apply(this, args);
};

const BROWSER_RE = /(chrome|chromium|headless[_-]shell)(\.exe)?$/i;
const OVERRIDDEN_RE = /^--(proxy-server|proxy-bypass-list|proxy-pac-url|host-resolver-rules|proxy-auto-detect)(=|$)/;

function guardedArgs(args) {
  const bypass = ["<-loopback>"];
  for (const port of ports) bypass.push(`localhost:${port}`);
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

const originalSpawn = childProcess.spawn;
childProcess.spawn = function guardedSpawn(command, args, options) {
  if (typeof command === "string" && Array.isArray(args) && BROWSER_RE.test(path.basename(command))) {
    const child = originalSpawn.call(this, command, guardedArgs(args), options);
    if (guardFile && child && child.pid) {
      try {
        fs.appendFileSync(guardFile, `${child.pid}\n`);
      } catch (_err) {
        // An unwritable guard file reads as "not applied" and fails the render.
      }
    }
    return child;
  }
  return originalSpawn.apply(this, arguments);
};

syncBuiltinESMExports();
