#!/usr/bin/env node
/*
 * `npx github:ayaangazali/ratchet <repo-url>`
 *
 * Ratchet is Python. This shim exists so that the first thing anybody runs is one
 * line with no prerequisites beyond Node, which is the difference between a project
 * somebody tries and a project somebody bookmarks.
 *
 * It does exactly three things: find or install `uv`, hand the arguments to the
 * Python CLI through it, and default a bare repository URL to the `go` subcommand.
 * uv is doing the real work -- it resolves a Python 3.11+ interpreter, downloading
 * one if the machine has none, and builds this package into a cached environment.
 * There is no second copy of the CLI's argument parsing here on purpose.
 */

"use strict";

const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "..");
const UV_INSTALL = "https://astral.sh/uv/install.sh";

function works(cmd) {
  const r = spawnSync(cmd, ["--version"], { stdio: "ignore" });
  return !r.error && r.status === 0;
}

function findUv() {
  if (works("uv")) return "uv";
  for (const p of [
    path.join(os.homedir(), ".local", "bin", "uv"),
    path.join(os.homedir(), ".cargo", "bin", "uv"),
    "/opt/homebrew/bin/uv",
    "/usr/local/bin/uv",
  ]) {
    if (fs.existsSync(p) && works(p)) return p;
  }
  return null;
}

function installUv() {
  if (process.platform === "win32") {
    console.error(
      "ratchet needs uv. Install it with:\n" +
        '  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.sh | iex"\n' +
        "then run this command again."
    );
    process.exit(1);
  }
  console.error("ratchet: installing uv (one time, into ~/.local/bin)…");
  const r = spawnSync("/bin/sh", ["-c", `curl -LsSf ${UV_INSTALL} | sh`], { stdio: "inherit" });
  if (r.status !== 0) {
    console.error("ratchet: could not install uv. Install it yourself and re-run:");
    console.error(`  curl -LsSf ${UV_INSTALL} | sh`);
    process.exit(1);
  }
  const uv = findUv();
  if (!uv) {
    console.error("ratchet: uv installed but not on PATH. Open a new shell and try again.");
    process.exit(1);
  }
  return uv;
}

/* A bare URL or `owner/repo` means "onboard this repository", which is the whole
 * point of the one-liner. Anything that looks like a subcommand is passed through
 * untouched so `npx ratchet verify --task …` still works. */
const KNOWN = new Set([
  "go", "run", "tree", "rewind", "diff", "verify", "ship", "replay",
  "bench-snapshot", "redteam", "audit", "evals", "console", "dashboard", "demo",
]);

function withSubcommand(args) {
  if (args.length === 0) return ["--help"];
  if (KNOWN.has(args[0]) || args[0].startsWith("-")) return args;
  return ["go", ...args];
}

const uv = findUv() || installUv();
const args = withSubcommand(process.argv.slice(2));
const r = spawnSync(uv, ["tool", "run", "--from", ROOT, "ratchet", ...args], { stdio: "inherit" });
process.exit(r.status === null ? 1 : r.status);
