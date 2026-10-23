// Regenerates docs/dashboard.png, the dark-mode collage at the top of the README.
//
// Starts `geiger demo` (simulated counter, made-up history) and headless Chrome,
// captures the dashboard at 2x, then frames the captures with collage.html.
// Needs Google Chrome, uv, and Node 22+; no npm packages. From the repo root:
//
//   node scripts/readme-image/make.mjs
//
// Set CHROME to use another Chromium build, KEEP=1 to keep the working folder.

import { spawn } from "node:child_process";
import { copyFileSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..", "..");
const out = join(root, "docs", "dashboard.png");
const CHROME = process.env.CHROME ?? "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const DEMO_PORT = 8399;
const DEBUG_PORT = 9399;
const work = mkdtempSync(join(tmpdir(), "geiger-readme-"));

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function waitFor(url, what) {
  for (let i = 0; i < 120; i++) {
    try {
      const res = await fetch(url);
      if (res.ok) return res;
    } catch { /* not up yet */ }
    await sleep(500);
  }
  throw new Error(`timed out waiting for ${what}`);
}

// a minimal Chrome DevTools Protocol client for one tab
class Tab {
  static async open() {
    const res = await fetch(`http://127.0.0.1:${DEBUG_PORT}/json/new?about:blank`, { method: "PUT" });
    const tab = new Tab();
    await tab.#connect((await res.json()).webSocketDebuggerUrl);
    await tab.send("Page.enable");
    await tab.send("Emulation.setEmulatedMedia", {
      features: [{ name: "prefers-color-scheme", value: "dark" }],
    });
    return tab;
  }

  #connect(url) {
    this.next = 0;
    this.pending = new Map();
    this.waiters = [];
    return new Promise((resolve, reject) => {
      this.ws = new WebSocket(url);
      this.ws.onopen = resolve;
      this.ws.onerror = reject;
      this.ws.onmessage = ({ data }) => {
        const msg = JSON.parse(data);
        const call = this.pending.get(msg.id);
        if (call) {
          this.pending.delete(msg.id);
          msg.error ? call.reject(new Error(msg.error.message)) : call.resolve(msg.result);
        } else if (msg.method) {
          for (const w of this.waiters) if (w.method === msg.method) w.resolve(msg.params);
          this.waiters = this.waiters.filter((w) => w.method !== msg.method);
        }
      };
    });
  }

  send(method, params = {}) {
    const id = ++this.next;
    this.ws.send(JSON.stringify({ id, method, params }));
    return new Promise((resolve, reject) => this.pending.set(id, { resolve, reject }));
  }

  event(method) {
    return new Promise((resolve) => this.waiters.push({ method, resolve }));
  }

  async viewport(width, height, mobile = false) {
    await this.send("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 2, mobile });
  }

  async goto(url) {
    const loaded = this.event("Page.loadEventFired");
    await this.send("Page.navigate", { url });
    await loaded;
  }

  async eval(expression) {
    const r = await this.send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description ?? r.exceptionDetails.text);
    return r.result.value;
  }

  async shot(file, clip) {
    const { data } = await this.send("Page.captureScreenshot", {
      format: "png",
      captureBeyondViewport: true,
      clip: { ...clip, scale: 1 },
    });
    writeFileSync(join(work, file), Buffer.from(data, "base64"));
  }

  // dashboard loaded: fonts in, history drawn, a live reading shown, the tape scrolling
  async settle() {
    await this.eval(`(async () => {
      await document.fonts.ready;
      for (let i = 0; i < 100 && !(app.series && app.reading && app.status); i++) {
        await new Promise((r) => setTimeout(r, 100));
      }
    })()`);
    await sleep(2500);
  }
}

// its own process group, so stopping it also stops the Python process uv starts
const demo = spawn("uv", ["run", "geiger", "demo", "--db", join(work, "demo.db"), "--http-port", String(DEMO_PORT)], {
  cwd: root,
  stdio: "ignore",
  detached: true,
});
const chrome = spawn(CHROME, [
  "--headless=new",
  `--remote-debugging-port=${DEBUG_PORT}`,
  `--user-data-dir=${join(work, "chrome")}`,
  "--no-first-run",
  "--no-default-browser-check",
  "--hide-scrollbars",
  "--force-color-profile=srgb",
  "about:blank",
], { stdio: "ignore" });

try {
  await waitFor(`http://127.0.0.1:${DEMO_PORT}/api/status`, "the demo server");
  await waitFor(`http://127.0.0.1:${DEBUG_PORT}/json/version`, "Chrome");
  const tab = await Tab.open();
  const dashboard = `http://localhost:${DEMO_PORT}/`;

  // desktop: the last week, with the readout on a rain bump two days back
  await tab.viewport(1280, 1000);
  await tab.goto(dashboard);
  await tab.eval(`localStorage.setItem("geiger.range", "7d"); localStorage.setItem("geiger.unit", "usv")`);
  await tab.goto(dashboard);
  await tab.settle();
  await tab.eval(`(() => {
    const d = new Date();
    d.setDate(d.getDate() - 2);
    d.setHours(15, 30, 0, 0);
    showPoint(nearestIndex(d.getTime() / 1000));
  })()`);
  const bottom = await tab.eval(`document.getElementById("stats").getBoundingClientRect().bottom + scrollY`);
  await tab.shot("desktop.png", { x: 0, y: 0, width: 1280, height: Math.ceil(bottom + 22) });

  // phone: the current reading, then the radiation levels table
  await tab.viewport(390, 844, true);
  await tab.eval(`localStorage.setItem("geiger.range", "24h")`);
  await tab.goto(dashboard);
  await tab.settle();
  await tab.shot("phone-reading.png", { x: 0, y: 0, width: 390, height: 844 });
  const top = await tab.eval(`document.getElementById("levels-heading").getBoundingClientRect().top + scrollY - 24`);
  await tab.shot("phone-levels.png", { x: 0, y: Math.round(top), width: 390, height: 844 });

  // frame the captures
  copyFileSync(join(here, "collage.html"), join(work, "collage.html"));
  const size = { width: 1740, height: 940 }; // matches collage.html
  await tab.viewport(size.width, size.height);
  await tab.goto(`file://${join(work, "collage.html")}`);
  await tab.eval(`Promise.all([...document.images].map((i) => i.decode()))`);
  await tab.shot("collage.png", { x: 0, y: 0, ...size });
  mkdirSync(dirname(out), { recursive: true });
  copyFileSync(join(work, "collage.png"), out);
  console.log(`wrote ${out}`);
} finally {
  chrome.kill();
  process.kill(-demo.pid, "SIGTERM");
  if (process.env.KEEP) console.log(`working files in ${work}`);
  else rmSync(work, { recursive: true, force: true });
}
