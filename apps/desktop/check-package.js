#!/usr/bin/env node
/*
 * Packaging guard for the LabCare Windows installer.
 *
 * Why this exists: electron-builder's `build.files` is an *allowlist*. The
 * 1.1.0 installer shipped without `bubble.html` / `bubble-preload.js`, so the
 * alert bubble — the thing that links an alert to the LabCare site — rendered
 * blank in the installed app while working fine from source. Nothing else
 * caught it, because `npm start` reads the files straight off disk.
 *
 * This check computes the exact set of files electron-builder will put into
 * app.asar and fails if anything main.js loads at runtime is missing.
 *
 *   node check-package.js          # also runs as `npm run verify:package`
 *
 * It is wired into `npm run pack` and into the release workflow, so a future
 * file can never be silently dropped from the installer again.
 */
"use strict";

const fs = require("fs");
const path = require("path");

const APP_DIR = __dirname;
const pkg = JSON.parse(fs.readFileSync(path.join(APP_DIR, "package.json"), "utf8"));

// Files main.js / the renderer / the bubble load at runtime, relative to the
// app dir. Keep in sync with main.js — the source scan below also catches
// anything new that is referenced but not listed here.
const REQUIRED = [
  "main.js",
  "preload.js",
  "bubble.html",
  "bubble-preload.js",
  "renderer/index.html",
  "package.json",
  "assets/tray.png",
  "assets/icon.png",
  "assets/chime.wav",
  "assets/bell.wav",
  "assets/beep.wav",
  "assets/alarm.wav",
];

// ---------------------------------------------------------------------------
// Which files will electron-builder actually put in app.asar?
// ---------------------------------------------------------------------------

/** Walk the app dir (skipping node_modules and build output). */
function walk(dir, out) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (entry.name === "node_modules") continue;
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (full === path.join(APP_DIR, "dist") || full === path.join(APP_DIR, "build")) continue;
      walk(full, out);
    } else {
      out.push(full);
    }
  }
  return out;
}

/**
 * Faithful reproduction of app-builder-lib's getMainFileMatchers():
 * user `files` patterns, plus package.json, plus the default exclusions.
 * Note the important detail — because `files` is non-empty, the implicit
 * "everything" default is NOT added, so anything unlisted is excluded.
 */
function effectivePatterns() {
  const user = (pkg.build && pkg.build.files) || ["**/*"];
  const patterns = user.slice();
  if (!patterns.some((p) => p === "package.json")) patterns.push("package.json");
  patterns.push("!**/node_modules", "!build{,/**/*}", "!dist{,/**/*}");
  return patterns;
}

function packagedFiles() {
  const patterns = effectivePatterns();
  const destination = path.join(APP_DIR, "dist", "probe", "resources", "app");

  // Prefer electron-builder's own matcher when it is installed (CI runs
  // `npm ci` first, so it normally is) — that is the authoritative answer.
  try {
    const { FileMatcher } = require("app-builder-lib/out/fileMatcher");
    const { createFilter } = require("app-builder-lib/out/util/filter");
    const matcher = new FileMatcher(APP_DIR, destination, (s) => s, patterns);
    const parsed = [];
    matcher.computeParsedPatterns(parsed);
    const filter = createFilter(matcher.from, parsed, matcher.excludePatterns);
    return walk(APP_DIR, [])
      .filter((f) => {
        let stat;
        try { stat = fs.statSync(f); } catch (e) { return false; }
        try { return filter(f, stat); } catch (e) { return false; }
      })
      .map((f) => path.relative(APP_DIR, f).split(path.sep).join("/"))
      .filter((rel) => rel !== "check-package.js");
  } catch (e) {
    // Fallback: a small glob matcher with the same semantics.
    const toRegExp = (glob) => new RegExp(
      "^" + glob
        .replace(/[.+^${}()|[\]\\]/g, "\\$&")
        .replace(/\{,\/\*\*\}/g, "(/.*)?")           // {,/**/*}
        .replace(/\*\*\//g, "\u0000")                 // **/  -> placeholder
        .replace(/\*\*/g, "\u0001")                   // **   -> placeholder
        .replace(/\*/g, "[^/]*")
        .replace(/\u0000/g, "(?:.*/)?")
        .replace(/\u0001/g, ".*") + "$"
    );
    const include = patterns.filter((p) => !p.startsWith("!")).map(toRegExp);
    const exclude = patterns.filter((p) => p.startsWith("!")).map((p) => toRegExp(p.slice(1)));
    return walk(APP_DIR, [])
      .map((f) => path.relative(APP_DIR, f).split(path.sep).join("/"))
      .filter((rel) => rel !== "check-package.js")
      .filter((rel) => include.some((re) => re.test(rel)) && !exclude.some((re) => re.test(rel)));
  }
}

// ---------------------------------------------------------------------------
// Also derive references straight from the source, so a newly added file that
// is loaded at runtime is checked even if REQUIRED was not updated.
// ---------------------------------------------------------------------------
function referencedInSource() {
  const found = new Set();
  const sources = ["main.js", "preload.js", "bubble-preload.js"]
    .map((f) => path.join(APP_DIR, f))
    .filter((f) => fs.existsSync(f));

  for (const file of sources) {
    const src = fs.readFileSync(file, "utf8");
    // path.join(__dirname, "renderer", "index.html")  /  path.join(__dirname, "bubble.html")
    const re = /path\.join\(\s*__dirname\s*((?:,\s*"[^"]+"\s*)+)\)/g;
    let m;
    while ((m = re.exec(src))) {
      const parts = m[1].split(",").map((s) => s.trim().replace(/^"|"$/g, "")).filter(Boolean);
      if (parts.length) found.add(parts.join("/"));
    }
    // path.join(__dirname, "assets", fileName) — the whole assets dir
    if (/path\.join\(\s*__dirname\s*,\s*"assets"/.test(src)) {
      for (const f of walk(path.join(APP_DIR, "assets"), [])) {
        found.add(path.relative(APP_DIR, f).split(path.sep).join("/"));
      }
    }
  }

  // bubble.html plays assets/<sound>.wav relative to itself
  const bubbleHtml = path.join(APP_DIR, "bubble.html");
  if (fs.existsSync(bubbleHtml) && /assets\/"\s*\+\s*s\s*\+\s*"\.wav|"assets\/"/.test(fs.readFileSync(bubbleHtml, "utf8"))) {
    for (const f of walk(path.join(APP_DIR, "assets"), [])) {
      found.add(path.relative(APP_DIR, f).split(path.sep).join("/"));
    }
  }
  return [...found];
}

// ---------------------------------------------------------------------------
// Run
// ---------------------------------------------------------------------------
const packaged = new Set(packagedFiles());
const required = new Set([...REQUIRED, ...referencedInSource()]);

const missing = [...required].filter((f) => !packaged.has(f)).sort();

console.log("build.files =", JSON.stringify((pkg.build && pkg.build.files) || []));
console.log(`\nPackaged into app.asar (${packaged.size} files):`);
[...packaged].sort().forEach((f) => console.log("  " + f));

if (missing.length) {
  console.error("\n\u2716 These runtime files are EXCLUDED by build.files:");
  missing.forEach((f) => console.error("    " + f));
  console.error("\n  The installed EXE would fail to load them (blank window / no site link).");
  console.error("  Add them to build.files in apps/desktop/package.json.\n");
  process.exit(1);
}

console.log("\n\u2714 Every runtime file main.js loads is packaged into the installer.");
