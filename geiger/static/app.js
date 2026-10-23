"use strict";

const RANGES = { "10m": 600, "1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800, "30d": 2592000, all: null };
const TAPE_SECONDS = 120;
const TAPE_MAX_TICKS = 40; // per second; busier seconds draw as a solid block
const SAVE_MODES = {
  0: "Off",
  1: "Every second",
  2: "Every minute",
  3: "Hourly average",
  4: "Every second, above threshold",
  5: "Every minute, above threshold",
};
const STATE_TEXT = {
  starting: "Starting",
  connecting: "Connecting",
  live: "Live",
  syncing: "Reading device history",
  offline: "Counter not connected",
  stopped: "Logger stopped",
  disconnected: "Logger not reachable",
};

const $ = (id) => document.getElementById(id);
const prefs = {
  get(key, fallback) {
    try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem(key, value); } catch { /* private mode */ }
  },
};
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");

const app = {
  status: null,
  connected: false,
  reading: null,
  arrival: 0, // performance.now() when the latest reading arrived
  tape: new Map(), // ts -> counts in that second
  range: prefs.get("geiger.range", "1h") in RANGES ? prefs.get("geiger.range", "1h") : "1h",
  unit: prefs.get("geiger.unit", "usv") === "cpm" ? "cpm" : "usv",
  series: null,
  seriesRequest: 0,
  baseline: null,
  toUsv: null,
  toCpm: null,
};

// calibration (mirrors geiger/calibration.py)

function makeCalibration(points) {
  const all = [[0, 0], ...points];
  const segments = [];
  for (let i = 0; i + 1 < all.length; i++) {
    const [c0, u0] = all[i];
    const [c1, u1] = all[i + 1];
    if (c1 > c0) {
      const m = (u1 - u0) / (c1 - c0);
      segments.push([c1, m, u1 - m * c1]);
    }
  }
  const last = segments[segments.length - 1];
  const toUsv = (cpm) => {
    for (const [max, m, b] of segments) if (cpm <= max) return cpm * m + b;
    return cpm * last[1] + last[2];
  };
  const toCpm = (usv) => {
    for (const [max, m, b] of segments) if (usv <= max * m + b) return (usv - b) / m;
    return (usv - last[2]) / last[1];
  };
  return { toUsv, toCpm };
}

// formatting

const timeFmt = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" });
const timeSecFmt = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit", second: "2-digit" });
const dayFmt = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" });
const dayYearFmt = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", year: "numeric" });
const dayTimeFmt = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });

const toDate = (ts) => new Date(ts * 1000);
const tzOffset = () => -new Date().getTimezoneOffset() * 60;

function fmtUsv(v) {
  if (v == null || !isFinite(v)) return "–";
  if (v < 1) return v.toFixed(3);
  if (v < 10) return v.toFixed(2);
  if (v < 100) return v.toFixed(1);
  return Math.round(v).toLocaleString();
}

// a configured setting rather than a measurement: no padded decimals
const fmtLevel = (v) => v.toLocaleString(undefined, { maximumFractionDigits: 3 });

function fmtCpm(v, decimals = 1) {
  if (v == null || !isFinite(v)) return "–";
  if (v >= 1000) return Math.round(v).toLocaleString();
  return v.toLocaleString(undefined, { maximumFractionDigits: decimals });
}

function fmtValue(cpm, unit = app.unit) {
  if (cpm == null) return "–";
  return unit === "usv" && app.toUsv ? `${fmtUsv(app.toUsv(cpm))} µSv/h` : `${fmtCpm(cpm)} CPM`;
}

function otherUnit(cpm) {
  if (cpm == null || !app.toUsv) return "";
  return app.unit === "usv" ? `${fmtCpm(cpm)} CPM` : `${fmtUsv(app.toUsv(cpm))} µSv/h`;
}

function fmtDose(usv) {
  if (usv == null) return "–";
  if (usv >= 1000) return `${(usv / 1000).toFixed(2)} mSv`;
  if (usv >= 10) return `${usv.toFixed(1)} µSv`;
  if (usv >= 1) return `${usv.toFixed(2)} µSv`;
  return `${usv.toFixed(3)} µSv`;
}

function fmtDuration(seconds) {
  if (seconds < 90) return `${Math.round(seconds)} s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min`;
  if (seconds < 172800) return `${(seconds / 3600).toFixed(1).replace(/\.0$/, "")} h`;
  if (seconds < 400 * 86400) return `${Math.round(seconds / 86400)} days`;
  return `${(seconds / 31557600).toFixed(1).replace(/\.0$/, "")} years`;
}

function fmtWhen(ts) {
  const d = toDate(ts);
  const today = new Date();
  if (d.toDateString() === today.toDateString()) return timeFmt.format(d);
  return dayTimeFmt.format(d);
}

// status

function onStatus(status) {
  const previous = app.status?.state;
  app.status = status;
  if (status.calibration) {
    const first = !app.toUsv;
    const { toUsv, toCpm } = makeCalibration(status.calibration.points);
    app.toUsv = toUsv;
    app.toCpm = toCpm;
    if (first && app.series) { // history arrived before the calibration did
      renderChart();
      renderStats();
    }
  }
  if (status.reading && (!app.reading || status.reading.ts > app.reading.ts)) {
    app.reading = status.reading;
  }
  renderStatus();
  renderReading();
  if (status.state === "live" && previous !== "live" && previous !== "syncing") loadSettings(); // just connected
  if (previous === undefined && app.range === "all") loadSeries(); // now the first reading's date is known
  if (previous === "syncing" && status.state !== "syncing") {
    loadSeries();
    loadBaseline();
  }
}

function renderStatus() {
  const s = app.status;
  const state = app.connected ? s?.state ?? "starting" : "disconnected";
  const pill = $("state");
  pill.dataset.state = state;
  $("state-text").textContent = STATE_TEXT[state] ?? state;

  const device = s?.device;
  $("model").textContent = device?.model ?? "Geiger counter";
  $("ident-detail").textContent = device ? `Firmware ${device.version.replace(device.model, "").trim()} on ${device.port}` : "";
  renderStatusTitle();

  const notice = $("notice");
  notice.classList.remove("error");
  let text = "";
  if (state === "disconnected") {
    text = "Can't reach the logger. Check that geiger run (or the launchd agent) is still running; this page reconnects on its own.";
    notice.classList.add("error");
  } else if (state === "offline") {
    text = `No counter connected (${s.error ?? "no reply"}). Plug it in and switch it on to see live readings and copy what it recorded while away; this page picks it up on its own. Stored history stays browsable below.`;
  } else if (state === "syncing") {
    const p = s.sync;
    const pct = p ? Math.round((100 * p.bytes) / p.total) : 0;
    text = `Reading the counter's stored history (${(p?.bytes ?? 0) / 1024 | 0} KiB so far, ${pct}% of its memory). Live readings resume when it finishes.`;
  }
  notice.hidden = !text;
  notice.textContent = text;

  const sync = $("sync");
  const busy = state === "syncing" || s?.sync_pending;
  sync.disabled = busy || state !== "live";
  sync.textContent = busy ? "Syncing…" : "Sync device history";

  const last = s?.last_sync;
  let syncText = "";
  if (last?.error) syncText = `Last sync failed ${fmtWhen(last.at)}: ${last.error}`;
  else if (last) {
    syncText = `Last synced ${fmtWhen(last.at)}. ${last.imported.toLocaleString()} new readings added from ${last.records.toLocaleString()} stored on the counter.`;
  } else syncText = "Copies readings from the counter's memory into any gaps here, such as while this Mac was asleep.";
  $("sync-status").textContent = syncText;

  renderFacts();
  renderMemory();
  updateSettingsUi();
}

function renderFacts() {
  const s = app.status;
  const facts = $("facts");
  facts.replaceChildren();
  if (!s?.device) return;
  const add = (label, value, hint) => {
    const wrap = document.createElement("div");
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = value;
    if (hint) {
      const h = document.createElement("span");
      h.className = "hint";
      h.textContent = hint;
      dd.append(h);
    }
    wrap.append(dt, dd);
    facts.append(wrap);
  };
  const { device, config, calibration } = s;
  add("Firmware", device.version);
  add("Serial number", device.serial);
  add("Connection", `${device.port} at ${device.baudrate.toLocaleString()} baud`);
  if (s.voltage) add("Battery", `${s.voltage.toFixed(1)} V`, "Read when the logger connects");
  if (s.clock_offset != null) {
    const off = s.clock_offset;
    const text = Math.abs(off) < 2 ? "In step with this Mac" : `${Math.abs(off).toFixed(0)} s ${off < 0 ? "ahead of" : "behind"} this Mac`;
    const hint = s.sets_clock
      ? "The logger resets it to this Mac's time when it drifts more than 3 s"
      : "Imported history is corrected for any difference";
    add("Counter clock", text, hint);
  }
  if (calibration) {
    const [cpm, usv] = calibration.points[0];
    add("Calibration", `${cpm.toLocaleString()} CPM = ${usv} µSv/h`, `${calibration.usv_per_cpm.toPrecision(3)} µSv/h per CPM, from the counter's settings`);
  }
  if (config) add("Alarm", config.Alarm ? `On at ${alarmLevel().label}` : "Off");
  if (s.bounds) add("Stored here", `Since ${dayYearFmt.format(toDate(s.bounds[0]))}`);
}

// the counter's alarm as set: in CPM or µSv/h, whichever it triggers on
function alarmLevel() {
  const c = app.status?.config;
  if (!c?.Alarm) return null;
  if (c.AlarmType === 0) {
    return { cpm: c.AlarmCPM, usv: app.toUsv?.(c.AlarmCPM) ?? null, label: `${fmtLevel(c.AlarmCPM)} CPM` };
  }
  return { usv: c.AlarmValue_uSv, cpm: app.toCpm?.(c.AlarmValue_uSv) ?? null, label: `${fmtLevel(c.AlarmValue_uSv)} µSv/h` };
}

// live reading

function onReading(r) {
  app.reading = r;
  app.arrival = performance.now();
  app.tape.set(r.ts, r.cps);
  for (const ts of app.tape.keys()) if (ts < r.ts - TAPE_SECONDS - 5) app.tape.delete(ts);
  renderReading();
  if (reducedMotion.matches) drawTape();
}

const CHECK = '<svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="7" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M4.8 8.2l2.1 2.1 4.3-4.5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const WARN = '<svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M8 1.8l6.6 12H1.4z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><path d="M8 6.2v3.6" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/><circle cx="8" cy="11.9" r="0.9" fill="currentColor"/></svg>';

function renderReading() {
  const r = app.reading;
  const live = app.connected && app.status?.state === "live";
  const dose = $("dose");
  dose.parentElement.classList.toggle("stale", !live);
  if (!r) {
    dose.textContent = "–";
    $("cpm").textContent = "–";
    $("window-note").textContent = "";
  } else {
    const usv = r.usv_h ?? app.toUsv?.(r.cpm);
    dose.textContent = fmtUsv(usv);
    // a second or two missing (e.g. just after a history sync) barely moves the rate
    const full = r.window >= 55;
    $("cpm").textContent = full ? fmtCpm(r.cpm, 0) : `≈ ${fmtCpm(r.cpm, 0)}`;
    $("window-note").textContent = live
      ? full ? "over the last minute" : `estimated from ${r.window} s`
      : `last reading ${fmtWhen(r.ts)}`;

    const alarm = alarmLevel();
    const verdict = $("verdict");
    // compare in the unit the counter's alarm uses, as the counter does
    const over = alarm && (app.status.config.AlarmType === 0 ? r.cpm >= alarm.cpm : usv != null && usv >= alarm.usv);
    verdict.hidden = !alarm || (app.status.config.AlarmType !== 0 && usv == null);
    if (!verdict.hidden) {
      verdict.classList.toggle("alarm", over);
      verdict.innerHTML = over ? WARN : CHECK;
      verdict.append(` ${over ? "At or above" : "Below"} the counter's alarm level of ${alarm.label}`);
    }
  }
  renderLevels(r && (r.usv_h ?? app.toUsv?.(r.cpm)), live);
  const b = app.baseline;
  $("baseline").textContent = b != null
    ? `7-day average ${fmtCpm(b)} CPM${app.toUsv ? `, ${fmtUsv(app.toUsv(b))} µSv/h` : ""}`
    : "";
  renderStatusTitle();
}

// mark the radiation-levels row the latest reading falls in
function renderLevels(usv, live) {
  let found = false;
  for (const row of $("levels").tBodies[0].rows) {
    const here = usv != null && !found && usv < Number(row.dataset.below);
    found ||= here;
    row.classList.toggle("current", here);
    const label = row.querySelector(".level-now");
    const text = here ? `${live ? "Now" : "Last reading"}: ${fmtUsv(usv)} µSv/h` : "";
    if (label.textContent !== text) label.textContent = text;
    if (here) row.setAttribute("aria-current", "true");
    else row.removeAttribute("aria-current");
  }
}

function renderStatusTitle() {
  const model = app.status?.device?.model ?? "Geiger";
  const r = app.reading;
  document.title = r && app.connected && app.status?.state === "live" ? `${fmtUsv(r.usv_h)} µSv/h, ${model}` : model;
}

// click tape: one tick per detected particle, scrolling like a chart recorder

const tape = { canvas: $("tape"), ctx: $("tape").getContext("2d"), rgb: "", last: 0 };

function readTapeColor() {
  tape.rgb = getComputedStyle(document.documentElement).getPropertyValue("--tick").trim();
  tape.grid = getComputedStyle(document.documentElement).getPropertyValue("--grid").trim();
}

function jitter(ts, i) {
  // stable position within the second, so ticks don't hop between frames
  let x = (ts * 2654435761 + i * 40503) >>> 0;
  x ^= x >>> 13;
  x = Math.imul(x, 1274126177) >>> 0;
  return (x % 1000) / 1000;
}

function drawTape() {
  const { canvas, ctx } = tape;
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);

  ctx.fillStyle = tape.grid;
  ctx.fillRect(0, h - 1, w, 1);
  const r = app.reading;
  if (!r) return;

  // the newest second ends at r.ts + 1; glide forward until the next reading lands
  const glide = reducedMotion.matches ? 0 : Math.min((performance.now() - app.arrival) / 1000, 1.25);
  const head = r.ts + 1 + glide;
  const px = w / TAPE_SECONDS;
  for (let s = 30; s < TAPE_SECONDS; s += 30) {
    ctx.fillRect(Math.round(w - (head - Math.floor(head) + s) * px), h - 5, 1, 4);
  }
  let total = 0;
  for (const [ts, count] of app.tape) {
    const age = head - ts;
    if (age > TAPE_SECONDS + 1 || age < 0) continue;
    if (age <= TAPE_SECONDS) total += count;
    const alpha = 0.3 + 0.7 * Math.max(0, 1 - age / TAPE_SECONDS);
    ctx.fillStyle = `rgba(${tape.rgb}, ${alpha.toFixed(3)})`;
    if (count > TAPE_MAX_TICKS) {
      ctx.fillRect(w - age * px, 6, px, h - 12);
      continue;
    }
    for (let i = 0; i < count; i++) {
      const x = w - (age - jitter(ts, i)) * px;
      ctx.fillRect(Math.round(x * dpr) / dpr, 6, 1.5, h - 14);
    }
  }
  const label = `${total.toLocaleString()} ${total === 1 ? "count" : "counts"} in 2 min`;
  if ($("tape-count").textContent !== label) $("tape-count").textContent = label;
}

function tapeLoop(now) {
  if (now - tape.last > 33) { // ~30 fps is plenty for a 1 px/s scroll
    tape.last = now;
    drawTape();
  }
  if (!reducedMotion.matches) requestAnimationFrame(tapeLoop);
}

// history chart

const SVG = "http://www.w3.org/2000/svg";
function el(tag, attrs, parent) {
  const node = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (parent) parent.append(node);
  return node;
}

function niceStep(span, count) {
  const raw = span / count;
  const p = 10 ** Math.floor(Math.log10(raw));
  const f = raw / p;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p;
}

const TIME_TICKS = [60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 604800, 2592000];

function currentWindow() {
  const end = Math.floor(Date.now() / 1000);
  const span = RANGES[app.range];
  if (span) return { start: end - span, end };
  const first = app.status?.bounds?.[0];
  return { start: first ? Math.min(first, end - 600) : end - 3600, end };
}

async function loadSeries() {
  const id = ++app.seriesRequest;
  const { start, end } = currentWindow();
  const width = $("chart").clientWidth || 800;
  const points = Math.max(120, Math.min(1500, Math.round(width / 1.5)));
  $("chart-wrap").classList.add("loading");
  try {
    const res = await fetch(`/api/series?start=${start}&end=${end}&points=${points}&tz=${tzOffset()}&fit=1`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    if (id !== app.seriesRequest) return; // a newer request replaced this one
    app.series = data;
    renderChart();
    renderStats();
    renderTable();
    $("export").href = `/api/export.csv?start=${start}&end=${end}`;
  } catch (e) {
    if (id === app.seriesRequest && !app.series) showEmpty("Couldn't load history from the logger. Retrying shortly.");
  } finally {
    if (id === app.seriesRequest) $("chart-wrap").classList.remove("loading");
  }
}

async function loadBaseline() {
  const end = Math.floor(Date.now() / 1000);
  try {
    const res = await fetch(`/api/series?start=${end - 604800}&end=${end}&points=1&tz=${tzOffset()}`);
    const data = await res.json();
    app.baseline = data.summary.seconds >= 3600 ? data.summary.mean_cpm : null;
    renderReading();
  } catch { /* the live readout stands without it */ }
}

function showEmpty(text) {
  const empty = $("chart-empty");
  empty.textContent = text;
  empty.hidden = false;
}

function seriesPoints() {
  return (app.series?.points ?? []).map(([t, cpm, lo, hi]) => ({ t, cpm, lo, hi }));
}

function unitValue(cpm) {
  return app.unit === "usv" && app.toUsv ? app.toUsv(cpm) : cpm;
}

const chart = { pts: [], x: null, y: null, m: null, w: 0, h: 0, index: -1 };

function renderChart() {
  const svg = $("chart");
  svg.replaceChildren();
  $("chart-empty").hidden = true;
  $("tooltip").hidden = true;
  const data = app.series;
  if (!data) return;
  const w = svg.clientWidth;
  const h = svg.clientHeight;
  if (!w || !h) return; // not laid out yet; the resize observer renders again
  const m = { l: 48, r: 14, t: 12, b: 26 };
  const pts = seriesPoints();
  chart.pts = pts;
  chart.w = w;
  chart.h = h;
  chart.m = m;

  const span = data.end - data.start;
  const x = (t) => m.l + ((t - data.start) / span) * (w - m.l - m.r);
  chart.x = x;

  const level = alarmLevel();
  const alarm = level ? (app.unit === "usv" && app.toUsv ? level.usv : level.cpm) ?? null : null;
  let top = 0;
  for (const p of pts) top = Math.max(top, unitValue(p.hi ?? p.cpm), unitValue(p.cpm));
  const showAlarm = alarm != null && top > 0 && alarm <= top * 1.6;
  if (showAlarm) top = Math.max(top, alarm);
  if (top <= 0) top = app.unit === "usv" ? 0.2 : 30;
  const step = niceStep(top * 1.08, 4);
  const yTop = Math.ceil((top * 1.08) / step) * step;
  const y = (v) => h - m.b - (v / yTop) * (h - m.t - m.b);
  chart.y = y;

  // y grid and labels
  const decimals = Math.max(0, -Math.floor(Math.log10(step)) + (step / 10 ** Math.floor(Math.log10(step)) === 2.5 ? 1 : 0));
  const grid = el("g", { class: "grid" }, svg);
  for (let v = step; v <= yTop + step / 1000; v += step) {
    el("line", { x1: m.l, x2: w - m.r, y1: y(v), y2: y(v) }, grid);
    el("text", { x: m.l - 8, y: y(v) + 4, "text-anchor": "end", class: "tick-label" }, svg).textContent = v.toFixed(decimals);
  }
  el("line", { x1: m.l, x2: w - m.r, y1: y(0), y2: y(0), class: "baseline-axis" }, svg);
  el("text", { x: m.l - 8, y: y(0) + 4, "text-anchor": "end", class: "tick-label" }, svg).textContent = "0";

  // time ticks, aligned to local clock boundaries
  const maxTicks = Math.max(2, Math.floor((w - m.l - m.r) / 110));
  const interval = TIME_TICKS.find((s) => span / s <= maxTicks) ?? TIME_TICKS[TIME_TICKS.length - 1];
  const tz = tzOffset();
  const unitStep = Math.min(interval, 86400);
  let t = Math.ceil((data.start + tz) / unitStep) * unitStep - tz;
  for (; t <= data.end; t += unitStep) {
    const local = t + tz;
    if (interval > 86400 && Math.round(local / 86400) % (interval / 86400) !== 0) continue;
    const d = toDate(t);
    const midnight = local % 86400 === 0;
    const label = interval >= 86400 || (midnight && span >= 86400) ? dayFmt.format(d) : timeFmt.format(d);
    const tx = x(t);
    if (tx < m.l + 16 || tx > w - m.r - 16) continue;
    el("line", { x1: tx, x2: tx, y1: h - m.b, y2: h - m.b + 4, class: "baseline-axis" }, svg);
    el("text", { x: tx, y: h - 6, "text-anchor": "middle", class: "tick-label" }, svg).textContent = label;
  }

  if (!pts.length) {
    showEmpty(app.status?.bounds ? "Nothing recorded in this period." : "No readings yet. They appear here as soon as the counter is connected.");
    setCaption();
    return;
  }

  // runs of points without gaps
  const gap = data.step * 1.5 + 1;
  const runs = [];
  for (const p of pts) {
    const run = runs[runs.length - 1];
    if (run && p.t - run[run.length - 1].t <= gap) run.push(p);
    else runs.push([p]);
  }

  for (const run of runs) {
    const banded = run.filter((p) => p.lo != null && p.hi != null);
    if (banded.length > 1 && banded.length === run.length) {
      const upper = run.map((p) => `${x(p.t).toFixed(1)},${y(unitValue(p.hi)).toFixed(1)}`);
      const lower = run.slice().reverse().map((p) => `${x(p.t).toFixed(1)},${y(unitValue(p.lo)).toFixed(1)}`);
      el("path", { d: `M${upper.join("L")}L${lower.join("L")}Z`, class: "band" }, svg);
    }
  }
  for (const run of runs) {
    if (run.length === 1) {
      el("circle", { cx: x(run[0].t), cy: y(unitValue(run[0].cpm)), r: 2.5, fill: "var(--series)" }, svg);
      continue;
    }
    const d = run.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(unitValue(p.cpm)).toFixed(1)}`).join("");
    el("path", { d, class: "line" }, svg);
  }

  if (showAlarm) {
    el("line", { x1: m.l, x2: w - m.r, y1: y(alarm), y2: y(alarm), class: "alarm-line" }, svg);
    el("text", { x: w - m.r, y: y(alarm) - 6, "text-anchor": "end", class: "alarm-label" }, svg).textContent = "Counter alarm level";
  }

  chart.cross = el("line", { y1: m.t, y2: h - m.b, class: "crosshair", visibility: "hidden" }, svg);
  chart.dot = el("circle", { r: 4.5, class: "hover-dot", visibility: "hidden" }, svg);
  setCaption(showAlarm, alarm);
  if (chart.hoverT != null) showPoint(nearestIndex(chart.hoverT)); // keep the readout through refreshes
}

function setCaption(showAlarm, alarm) {
  const data = app.series;
  if (!data) return;
  const unit = app.unit === "usv" && app.toUsv ? "µSv/h" : "counts per minute";
  let text = data.step < 60
    ? `Rolling one-minute rate in ${unit}, as the counter displays it.`
    : `Average ${unit} per ${fmtDuration(data.step)}; the shaded band spans the quietest to the busiest minute.`;
  // the server fits the chart to the readings; say so when that left out part of the range
  const zoomed = data.start - data.range_start > 60 || data.range_end - data.end > 60;
  if (zoomed && chart.pts.length) {
    text += ` Zoomed to the readings in this range, ${fmtWhen(data.start)} to ${fmtWhen(data.end)}.`;
  }
  const level = alarmLevel();
  if (level && !showAlarm && chart.pts.length) {
    text += ` The counter's alarm level (${level.label}) is far above this range.`;
  }
  $("chart-caption").textContent = text;
}

function nearestIndex(t) {
  const pts = chart.pts;
  let lo = 0;
  let hi = pts.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (pts[mid].t < t) lo = mid;
    else hi = mid;
  }
  return Math.abs(pts[lo].t - t) <= Math.abs(pts[hi].t - t) ? lo : hi;
}

function showPoint(index) {
  const p = chart.pts[index];
  if (!p || !chart.cross) return;
  chart.index = index;
  chart.hoverT = p.t;
  const px = chart.x(p.t);
  const py = chart.y(unitValue(p.cpm));
  chart.cross.setAttribute("x1", px);
  chart.cross.setAttribute("x2", px);
  chart.cross.setAttribute("visibility", "visible");
  chart.dot.setAttribute("cx", px);
  chart.dot.setAttribute("cy", py);
  chart.dot.setAttribute("visibility", "visible");

  const tip = $("tooltip");
  tip.replaceChildren();
  const value = document.createElement("strong");
  const key = document.createElement("span");
  key.className = "key";
  value.append(key, fmtValue(p.cpm));
  tip.append(value);
  const line = (text) => {
    if (!text) return;
    const div = document.createElement("div");
    div.textContent = text;
    tip.append(div);
  };
  line(otherUnit(p.cpm));
  if (p.lo != null && p.hi != null) line(`Minutes ranged ${fmtCpm(p.lo, 0)}–${fmtCpm(p.hi, 0)} CPM`);
  const step = app.series.step;
  if (step < 60) line(`Minute ending ${timeSecFmt.format(toDate(p.t))}`);
  else if (step >= 86400) line(dayFmt.format(toDate(p.t - step / 2)));
  else {
    const a = toDate(p.t - step / 2);
    const b = toDate(p.t + step / 2);
    line(`${a.toDateString() === new Date().toDateString() ? "" : `${dayFmt.format(a)}, `}${timeFmt.format(a)} – ${timeFmt.format(b)}`);
  }
  tip.hidden = false;
  const width = tip.offsetWidth;
  const left = px + 14 + width > chart.w ? px - 14 - width : px + 14;
  tip.style.transform = `translate(${Math.max(0, left)}px, ${chart.m.t}px)`;
}

function hideTooltip() {
  $("tooltip").hidden = true;
  chart.cross?.setAttribute("visibility", "hidden");
  chart.dot?.setAttribute("visibility", "hidden");
  chart.index = -1;
  chart.hoverT = null;
}

function setupChartHover() {
  const svg = $("chart");
  const track = (e) => {
    if (!chart.pts.length || !app.series) return;
    const rect = svg.getBoundingClientRect();
    const frac = (e.clientX - rect.left - chart.m.l) / (chart.w - chart.m.l - chart.m.r);
    const t = app.series.start + frac * (app.series.end - app.series.start);
    showPoint(nearestIndex(t));
  };
  svg.addEventListener("pointermove", track);
  svg.addEventListener("pointerdown", track); // a tap on a touch screen
  // a lifted finger "leaves" too; keep a tapped readout until the chart loses focus
  svg.addEventListener("pointerleave", (e) => { if (e.pointerType !== "touch") hideTooltip(); });
  svg.addEventListener("blur", hideTooltip);
  svg.addEventListener("keydown", (e) => {
    if (!chart.pts.length) return;
    const last = chart.pts.length - 1;
    const moves = { ArrowLeft: -1, ArrowRight: 1, PageUp: -10, PageDown: 10 };
    if (e.key in moves) {
      const start = chart.index < 0 ? last : chart.index;
      showPoint(Math.max(0, Math.min(last, start + moves[e.key])));
      e.preventDefault();
    } else if (e.key === "Home" || e.key === "End") {
      showPoint(e.key === "Home" ? 0 : last);
      e.preventDefault();
    } else if (e.key === "Escape") hideTooltip();
  });
}

// stats and table

function renderStats() {
  const s = app.series?.summary;
  if (!s) return;
  const set = (id, text) => { $(id).textContent = text; };
  const big = (cpm) => (cpm == null ? "–" : fmtValue(cpm));
  set("s-mean", big(s.mean_cpm));
  set("s-mean-sub", otherUnit(s.mean_cpm));
  set("s-peak", big(s.peak_cpm));
  set("s-peak-sub", s.peak_ts ? `${otherUnit(s.peak_cpm)}${app.toUsv ? ", " : ""}${fmtWhen(s.peak_ts)}` : "");
  set("s-min", big(s.min_cpm));
  set("s-min-sub", otherUnit(s.min_cpm));
  set("s-dose", fmtDose(s.dose_usv));
  set("s-dose-sub", s.seconds ? `over ${fmtDuration(s.seconds)} recorded` : "");
  const pct = s.coverage * 100;
  set("s-coverage", s.seconds ? (pct < 1 ? "Under 1%" : `${Math.round(pct)}%`) : "–");
  set("s-coverage-sub", `${s.counts.toLocaleString()} counts`);
}

function renderTable() {
  const details = $("table-details");
  if (!details.open || !app.series) return;
  $("table-unit").textContent = app.toUsv ? "µSv/h" : "CPM";
  const body = details.querySelector("tbody");
  const rows = seriesPoints().reverse().slice(0, 1500);
  const step = app.series.step;
  body.replaceChildren(...rows.map((p) => {
    const tr = document.createElement("tr");
    const when = step < 60 ? timeSecFmt.format(toDate(p.t)) : step >= 86400 ? dayFmt.format(toDate(p.t - step / 2)) : dayTimeFmt.format(toDate(p.t - step / 2));
    const cells = [
      when,
      app.toUsv ? fmtUsv(app.toUsv(p.cpm)) : fmtCpm(p.cpm),
      fmtCpm(p.cpm),
      p.lo != null ? `${fmtCpm(p.lo, 0)}–${fmtCpm(p.hi, 0)}` : "",
    ];
    for (const c of cells) {
      const td = document.createElement("td");
      td.textContent = c;
      tr.append(td);
    }
    return tr;
  }));
}

// counter memory

const SECONDS_PER_RECORD = { 1: 1, 2: 60, 3: 3600 };

function fmtBytes(n) {
  if (n < 1024) return `${n} bytes`;
  if (n < 1048576) return `${(n / 1024).toFixed(n < 10240 ? 1 : 0)} KB`;
  return `${(n / 1048576).toFixed(1).replace(/\.0$/, "")} MB`;
}

function renderMemory() {
  const s = app.status;
  const sync = s?.last_sync;
  const meter = $("memory-meter");
  const actions = $("memory-actions");
  if (!sync || sync.used == null) {
    $("memory-summary").textContent = sync?.error
      ? `Its memory couldn't be read: ${sync.error}`
      : "Sync the counter to see what its memory holds.";
    $("memory-detail").textContent = "";
    $("erase-check").textContent = "";
    actions.hidden = true;
    return;
  }
  const pct = (100 * sync.used) / sync.capacity;
  $("memory-fill").style.width = `${Math.min(100, pct)}%`;
  meter.setAttribute("aria-valuenow", pct.toFixed(1));
  meter.classList.toggle("full", sync.wrapped);
  $("memory-summary").textContent = sync.wrapped
    ? `Full: ${fmtBytes(sync.capacity)}, and the counter is overwriting its oldest records`
    : `${fmtBytes(sync.used)} of ${fmtBytes(sync.capacity)} used (${pct < 1 ? "under 1" : Math.round(pct)}%)`;

  const parts = [];
  if (sync.records) {
    const span = sync.first && sync.last ? `, ${dayFmt.format(toDate(sync.first))} to ${dayFmt.format(toDate(sync.last))}` : "";
    parts.push(`${sync.records.toLocaleString()} records${span}.`);
  }
  const mode = s.config?.SaveDataType;
  const perRecord = SECONDS_PER_RECORD[mode];
  if (mode === 0) parts.push("Saving is off, so it records nothing while it's away from the Mac.");
  else if (mode === 4 || mode === 5) parts.push("It only saves readings above its threshold, so how long the memory lasts depends on how often that happens.");
  else if (perRecord && sync.records && sync.used) {
    const bytesPerRecord = sync.used / sync.records;
    const setting = SAVE_MODES[mode].toLowerCase();
    if (sync.wrapped) {
      const holds = (sync.capacity / bytesPerRecord) * perRecord;
      parts.push(`Saving ${setting}, it holds about ${fmtDuration(holds)}; sync at least that often so nothing is lost.`);
    } else {
      const left = ((sync.capacity - sync.used) / bytesPerRecord) * perRecord;
      parts.push(`Saving ${setting}, it has room for about ${fmtDuration(left)} more before it starts overwriting its oldest records.`);
    }
  }
  parts.push(`As of the last sync, ${fmtWhen(sync.at)}.`);
  $("memory-detail").textContent = parts.join(" ");
  actions.hidden = !sync.bytes;

  const live = app.connected && s.state === "live";
  $("erase-check").textContent = live
    ? `Everything it held at ${fmtWhen(sync.at)} is stored here, and readings since then are being recorded live, so erasing loses nothing.`
    : `Everything it held at ${fmtWhen(sync.at)} is stored here. If it has recorded more since, connect and sync it before erasing.`;
}

// counter settings

const settingsState = { data: null, draft: {}, invalid: new Set(), busy: false };

async function loadSettings() {
  try {
    settingsState.data = await (await fetch("/api/settings")).json();
  } catch {
    return;
  }
  settingsState.draft = {};
  settingsState.invalid.clear();
  renderSettings();
}

function settingField(key) {
  return settingsState.data?.fields.find((f) => f.key === key);
}

function settingValue(key) {
  return key in settingsState.draft ? settingsState.draft[key] : settingField(key)?.value;
}

function settingVisible(key) {
  const threshold = [4, 5].includes(settingValue("save_mode"));
  switch (key) {
    case "alarm_cpm": return settingValue("alarm_type") === 0;
    case "alarm_usv": return settingValue("alarm_type") !== 0;
    case "threshold_type": return threshold;
    case "threshold_cpm": return threshold && settingValue("threshold_type") === 0;
    case "threshold_usv": return threshold && settingValue("threshold_type") === 1;
    default: return true;
  }
}

function setDraft(f, value) {
  const original = f.value;
  const same = f.kind === "f32" ? Math.abs(value - original) < 1e-6 : value === original;
  if (same) delete settingsState.draft[f.key];
  else settingsState.draft[f.key] = value;
  updateSettingsUi();
}

function settingRow(f) {
  const row = document.createElement("div");
  row.className = "setting";
  row.dataset.key = f.key;
  const id = `setting-${f.key}`;
  const label = document.createElement("label");
  label.htmlFor = id;
  label.textContent = f.label;
  let control;
  if (f.kind === "bool") {
    control = document.createElement("input");
    control.type = "checkbox";
    control.className = "switch";
    control.setAttribute("role", "switch");
    control.id = id;
    control.checked = f.value === 1;
    control.indeterminate = !f.known; // a value this layout doesn't recognize: left alone unless changed
    control.addEventListener("change", () => setDraft(f, control.checked ? 1 : 0));
  } else if (f.kind === "choice") {
    control = document.createElement("select");
    control.id = id;
    for (const [value, text] of Object.entries(f.options)) control.add(new Option(text, value));
    if (!f.known) control.add(new Option(`Unknown (${f.value})`, String(f.value)));
    control.value = String(f.value);
    control.addEventListener("change", () => setDraft(f, Number(control.value)));
  } else {
    control = document.createElement("span");
    control.className = "number";
    const input = document.createElement("input");
    input.type = "number";
    input.id = id;
    input.min = f.min;
    input.max = f.max;
    const whole = f.kind !== "f32";
    input.step = whole ? "1" : f.key.startsWith("cal_") ? "0.001" : "0.01";
    input.value = f.value == null ? "" : f.kind === "f32" ? String(Number(f.value.toFixed(4))) : String(f.value);
    input.addEventListener("input", () => {
      const n = input.valueAsNumber;
      const ok = !Number.isNaN(n) && n >= f.min && n <= f.max && (!whole || Number.isInteger(n));
      input.setCustomValidity(ok ? "" : `Enter ${whole ? "a whole number" : "a number"} from ${f.min} to ${f.max}`);
      if (ok) {
        settingsState.invalid.delete(f.key);
        setDraft(f, n);
      } else {
        settingsState.invalid.add(f.key);
        updateSettingsUi();
      }
    });
    const unit = document.createElement("span");
    unit.textContent = f.unit;
    control.append(input, unit);
  }
  row.append(label, control);
  if (f.help) {
    const help = document.createElement("p");
    help.className = "help";
    help.textContent = f.help;
    row.append(help);
  }
  return row;
}

function renderSettings() {
  const form = $("settings-form");
  form.replaceChildren();
  const fields = settingsState.data?.fields ?? [];
  const groups = new Map();
  for (const f of fields) {
    if (!groups.has(f.group)) groups.set(f.group, []);
    groups.get(f.group).push(f);
  }
  for (const [name, members] of groups) {
    const set = document.createElement("fieldset");
    const legend = document.createElement("legend");
    legend.textContent = name;
    set.append(legend);
    let box = set;
    if (name === "Calibration") {
      set.classList.add("calibration");
      const help = document.createElement("p");
      help.className = "help muted";
      help.textContent = "Each point pairs a CPM reading with the dose rate it stands for; µSv/h readings everywhere follow these. Change them only against a known reference source.";
      box = document.createElement("div");
      box.className = "calibration-grid";
      set.append(help, box);
    }
    for (const f of members) box.append(settingRow(f));
    form.append(set);
  }
  updateSettingsUi();
}

function updateSettingsUi() {
  const data = settingsState.data;
  const live = app.connected && app.status?.state === "live";
  for (const row of $("settings-form").querySelectorAll(".setting")) {
    const key = row.dataset.key;
    row.hidden = !settingVisible(key);
    if (row.hidden && (key in settingsState.draft || settingsState.invalid.has(key))) {
      // an edit to a field that no longer applies (say, the CPM level after switching
      // the alarm to µSv/h) is dropped, not saved out of sight
      delete settingsState.draft[key];
      settingsState.invalid.delete(key);
      const fresh = settingRow(settingField(key));
      fresh.hidden = true;
      row.replaceWith(fresh);
      continue;
    }
    row.classList.toggle("changed", key in settingsState.draft);
  }
  const changes = Object.keys(settingsState.draft).length;
  for (const set of $("settings-form").querySelectorAll("fieldset")) set.disabled = !live || settingsState.busy;
  const save = $("settings-save");
  save.disabled = !live || settingsState.busy || !changes || settingsState.invalid.size > 0;
  save.textContent = changes ? `Save ${changes} ${changes === 1 ? "change" : "changes"} to counter` : "Save to counter";
  $("settings-discard").disabled = settingsState.busy || !changes;
  $("settings-refresh").disabled = !live || settingsState.busy;
  const status = $("settings-status");
  if (data?.problem) status.textContent = data.problem;
  else if (!data?.fields.length) status.textContent = "Connect the counter to read its settings.";
  else if (live) status.textContent = `Read from the counter ${fmtWhen(data.read_at)}. Nothing changes on it until you save.`;
  else status.textContent = `What the counter was set to ${fmtWhen(data.read_at)}. Connect it to make changes.`;
}

async function settingsRequest(path, body, working, done) {
  const result = $("settings-result");
  settingsState.busy = true;
  updateSettingsUi();
  result.className = "settings-result";
  result.textContent = working;
  try {
    const res = await fetch(path, {
      method: "POST",
      headers: { "X-Geiger": "1", "Content-Type": "application/json" },
      body: JSON.stringify(body ?? {}),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error ?? `HTTP ${res.status}`);
    settingsState.data = data;
    settingsState.draft = {};
    settingsState.invalid.clear();
    settingsState.busy = false;
    renderSettings();
    result.classList.add("ok");
    result.textContent = done;
  } catch (e) {
    // keep the form as the user left it, edits and all, so a retry sends what they see
    settingsState.busy = false;
    updateSettingsUi();
    result.classList.add("error");
    const message = e.message.charAt(0).toUpperCase() + e.message.slice(1);
    result.textContent = Object.keys(settingsState.draft).length
      ? `${message}. Your edits are still here; Read from counter shows what it holds now.`
      : `${message}.`;
  }
}

function setupSettings() {
  $("settings-form").addEventListener("submit", (e) => {
    e.preventDefault();
    if (!Object.keys(settingsState.draft).length) return;
    settingsRequest("/api/settings", { changes: settingsState.draft }, "Writing to the counter…", "Saved to the counter.");
  });
  $("settings-discard").addEventListener("click", () => {
    settingsState.draft = {};
    settingsState.invalid.clear();
    $("settings-result").textContent = "";
    renderSettings();
  });
  $("settings-refresh").addEventListener("click", () =>
    settingsRequest("/api/settings/refresh", null, "Reading from the counter…", "Read from the counter."));
}

// controls

function setupControls() {
  const markRadios = (groupId, attr, value) => {
    for (const b of $(groupId).querySelectorAll("button")) {
      b.setAttribute("aria-checked", String(b.dataset[attr] === value));
      b.tabIndex = b.dataset[attr] === value ? 0 : -1;
    }
  };
  const radioKeys = (groupId, pick) => {
    $(groupId).addEventListener("keydown", (e) => {
      const buttons = [...$(groupId).querySelectorAll("button")];
      const i = buttons.indexOf(document.activeElement);
      const d = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key];
      if (i < 0 || !d) return;
      const next = buttons[(i + d + buttons.length) % buttons.length];
      next.focus();
      pick(next);
      e.preventDefault();
    });
  };

  const pickRange = (b) => {
    app.range = b.dataset.range;
    prefs.set("geiger.range", app.range);
    markRadios("ranges", "range", app.range);
    loadSeries();
    scheduleRefresh();
  };
  $("ranges").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) pickRange(b); });
  radioKeys("ranges", pickRange);
  markRadios("ranges", "range", app.range);

  const pickUnit = (b) => {
    app.unit = b.dataset.unit;
    prefs.set("geiger.unit", app.unit);
    markRadios("units", "unit", app.unit);
    renderChart();
    renderStats();
  };
  $("units").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) pickUnit(b); });
  radioKeys("units", pickUnit);
  markRadios("units", "unit", app.unit);

  $("table-details").addEventListener("toggle", renderTable);

  $("sync").addEventListener("click", async () => {
    $("sync").disabled = true;
    $("sync").textContent = "Syncing…";
    try {
      await fetch("/api/sync", { method: "POST", headers: { "X-Geiger": "1" } });
    } catch {
      renderStatus();
    }
  });
}

let refreshTimer = null;
function scheduleRefresh() {
  clearInterval(refreshTimer);
  const span = RANGES[app.range] ?? Infinity;
  const every = span <= 3600 ? 5000 : span <= 86400 ? 30000 : 120000;
  refreshTimer = setInterval(() => { if (!document.hidden) loadSeries(); }, every);
}

// startup

async function loadRecent() {
  try {
    const rows = await (await fetch(`/api/recent?seconds=${TAPE_SECONDS + 5}`)).json();
    for (const [ts, count] of rows) app.tape.set(ts, count); // merge: live events may land mid-fetch
  } catch { /* the tape fills from live readings */ }
}

function connectEvents() {
  const events = new EventSource("/api/events");
  events.addEventListener("open", () => {
    app.connected = true;
    loadRecent();
  });
  events.addEventListener("status", (e) => {
    app.connected = true;
    onStatus(JSON.parse(e.data));
  });
  events.addEventListener("reading", (e) => onReading(JSON.parse(e.data)));
  events.addEventListener("error", () => {
    app.connected = false; // EventSource keeps retrying on its own
    renderStatus();
    renderReading();
  });
}

function init() {
  readTapeColor();
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { readTapeColor(); drawTape(); });
  setupControls();
  setupSettings();
  setupChartHover();
  connectEvents();
  loadSeries();
  loadSettings();
  loadBaseline();
  scheduleRefresh();
  setInterval(loadBaseline, 600000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) loadSeries(); });

  let resizeTimer = null;
  new ResizeObserver(() => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { renderChart(); drawTape(); }, 100);
  }).observe($("chart-wrap"));

  if (reducedMotion.matches) drawTape();
  else requestAnimationFrame(tapeLoop);
  reducedMotion.addEventListener("change", () => { if (!reducedMotion.matches) requestAnimationFrame(tapeLoop); });
}

init();
