"use strict";

const modulesEl = document.getElementById("module-list");
const simSelect = document.getElementById("sim-select");
const receiptImg = document.getElementById("receipt-img");
const receiptEmpty = document.getElementById("receipt-empty");
const simMeta = document.getElementById("sim-meta");
const simText = document.getElementById("sim-text");
const bannerEl = document.getElementById("banner");
const serverStatus = document.getElementById("server-status");
const serverInfo = document.getElementById("server-info");
const printerState = document.getElementById("printer-state");
const logFilter = document.getElementById("log-filter");
const logList = document.getElementById("log-list");

const DEFAULT_TAB = "instant";
const DEFAULT_PAPER = "80mm";
const LOG_SHOWN = 300;
const SERVER_LOGS = "__server__";

let state = null;
let selected = "total";
let refreshing = false;
let bannerTimer = null;
let logEntries = [];
let nextLogSeq = null;
let logView = "";        // key of what the log list currently shows
let imgUrl = null;       // receipt image URL currently requested
let imgFailed = null;    // receipt image URL that last failed to load
const cards = new Map();

function make(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function api(path, options = {}) {
  const opts = { ...options, headers: { ...(options.headers || {}) } };
  if (opts.body && typeof opts.body !== "string") {
    opts.body = JSON.stringify(opts.body);
    opts.headers["Content-Type"] = "application/json";
  }
  const response = await fetch(path, opts);
  const text = await response.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch { data = { error: text }; }
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

function showBanner(message) {
  bannerEl.textContent = message;
  bannerEl.classList.remove("hidden");
  if (bannerTimer) clearTimeout(bannerTimer);
  bannerTimer = setTimeout(() => bannerEl.classList.add("hidden"), 8000);
}

async function busy(button, fn) {
  if (button) button.disabled = true;
  try { await fn(); }
  catch (error) { showBanner(error.message); }
  finally { if (button) button.disabled = false; }
}

function setIfIdle(input, value) {
  if (document.activeElement !== input) input.checked = Boolean(value);
}

function setValueIfIdle(input, value) {
  if (document.activeElement === input) return;
  if (input.type === "checkbox") input.checked = Boolean(value);
  else input.value = value === null || value === undefined ? "" : String(value);
}

// ---- tabs ------------------------------------------------------------
// Tabs are discovered from the markup: every [role=tab] in the tablist
// with a data-tab name and aria-controls pointing at its panel. The URL
// hash (#about, #modules, ...) holds the current tab.

const tabs = [...document.querySelectorAll('.tabs [role="tab"]')];

function tabNames() {
  return tabs.map((tab) => tab.dataset.tab);
}

function tabFromHash() {
  const name = decodeURIComponent(location.hash.replace(/^#/, ""));
  return tabNames().includes(name) ? name : DEFAULT_TAB;
}

function showTab(name, { focus = false } = {}) {
  for (const tab of tabs) {
    const active = tab.dataset.tab === name;
    tab.setAttribute("aria-selected", active ? "true" : "false");
    tab.tabIndex = active ? 0 : -1;
    document.getElementById(tab.getAttribute("aria-controls")).hidden = !active;
    if (active && focus) tab.focus();
  }
}

// replace=true for arrow-key moves so they don't flood the history.
function goToTab(name, { focus = false, replace = false } = {}) {
  if (!tabNames().includes(name)) name = DEFAULT_TAB;
  if (location.hash !== `#${name}`) {
    if (replace) history.replaceState(null, "", `#${name}`);
    else history.pushState(null, "", `#${name}`);
  }
  showTab(name, { focus });
}

function bindTabs() {
  for (const [index, tab] of tabs.entries()) {
    tab.addEventListener("click", () => goToTab(tab.dataset.tab));
    tab.addEventListener("keydown", (event) => {
      let next = null;
      if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
      else if (event.key === "ArrowLeft") next = (index - 1 + tabs.length) % tabs.length;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = tabs.length - 1;
      if (next === null) return;
      event.preventDefault();
      goToTab(tabs[next].dataset.tab, { focus: true, replace: true });
    });
  }
  // In-page links such as <a data-goto="modules">.
  document.addEventListener("click", (event) => {
    const link = event.target.closest("[data-goto]");
    if (!link) return;
    event.preventDefault();
    goToTab(link.dataset.goto, { focus: true });
  });
  window.addEventListener("popstate", () => showTab(tabFromHash()));
  window.addEventListener("hashchange", () => showTab(tabFromHash()));
  showTab(tabFromHash());
}

// ---- module cards ----------------------------------------------------

function schemaInput(schema, value, key, kind) {
  let input;
  if (schema.type === "bool") {
    input = document.createElement("input");
    input.type = "checkbox";
    input.checked = Boolean(value);
  } else if (schema.type === "select") {
    input = document.createElement("select");
    for (const choice of schema.choices || []) {
      const option = document.createElement("option");
      option.value = String(choice.value ?? choice);
      option.textContent = String(choice.label ?? choice.value ?? choice);
      input.append(option);
    }
    input.value = value === null || value === undefined ? "" : String(value);
  } else {
    input = document.createElement("input");
    input.type = schema.type === "int" ? "number" : "text";
    if (schema.min !== undefined) input.min = schema.min;
    if (schema.max !== undefined) input.max = schema.max;
    if (schema.max_length !== undefined) input.maxLength = schema.max_length;
    input.value = value === null || value === undefined ? "" : String(value);
    input.spellcheck = false;
  }
  input.dataset.key = key;
  input.dataset.kind = kind;
  input.dataset.schemaType = schema.type;
  input.dataset.optional = schema.optional ? "1" : "0";
  return input;
}

function field(labelText, input) {
  const wrap = make("label", "field");
  wrap.append(make("span", "", labelText), input);
  return wrap;
}

function outputLink(key, suffix, text) {
  const link = make("a", "", text);
  link.href = `/api/output/${encodeURIComponent(key)}${suffix}`;
  link.target = "_blank";
  link.rel = "noopener";
  return link;
}

function buildCard(module) {
  const root = make("article", "card");
  root.dataset.moduleId = module.id;

  const head = make("div", "card-head");
  const titleBox = make("div");
  const title = make("h2", "", module.name);
  const desc = make("p", "desc", module.description);
  titleBox.append(title, desc);
  const toggleLabel = make("label", "switch");
  const enabled = document.createElement("input");
  enabled.type = "checkbox";
  enabled.className = "mod-enabled";
  toggleLabel.append(enabled, document.createTextNode("Enabled"));
  head.append(titleBox, toggleLabel);

  const badges = make("div", "badges");
  const badge = make("span", "badge never", "never");
  const meta = make("span", "meta", "");
  badges.append(badge, meta);

  const fields = make("div", "fields");
  const interval = document.createElement("input");
  interval.type = "number";
  interval.min = "5";
  interval.max = "604800";
  interval.dataset.key = "interval_seconds";
  interval.dataset.kind = "interval";
  fields.append(field("Interval (seconds)", interval));

  const fieldEntries = [];
  for (const [key, schema] of Object.entries(module.limits_schema || {})) {
    const input = schemaInput(schema, module.limits[key], key, "limit");
    fields.append(field(schema.label || key, input));
    fieldEntries.push({ input, schema, kind: "limit", key });
  }
  for (const [key, schema] of Object.entries(module.options_schema || {})) {
    const input = schemaInput(schema, module.options[key], key, "option");
    fields.append(field(schema.label || key, input));
    fieldEntries.push({ input, schema, kind: "option", key });
  }

  const actions = make("div", "actions");
  const save = make("button", "", "Save");
  save.type = "button";
  const run = make("button", "secondary", "Run now");
  run.type = "button";
  const links = make("span", "links");
  links.append(outputLink(module.id, "", "Text"), outputLink(module.id, ".png", "Image"));
  actions.append(save, run, links);

  const error = make("p", "card-error", "");
  const note = make("p", "card-note", "");
  const details = make("details");
  details.append(make("summary", "", "Debug output"));
  details.append(make("h3", "", "Latest receipt"));
  const output = make("pre", "module-output", "");
  details.append(output);
  const showLogs = make("button", "linkish", "Show logs for this module");
  showLogs.type = "button";
  details.append(showLogs);

  root.append(head, badges, fields, actions, error, note, details);
  modulesEl.append(root);

  const entry = {
    root, title, desc, enabled, badge, meta, interval,
    fieldEntries, save, run, error, note, output, module, busy: false,
  };
  cards.set(module.id, entry);

  enabled.addEventListener("change", () => {
    busy(save, async () => {
      await api(`/api/modules/${encodeURIComponent(module.id)}`, {
        method: "PATCH", body: { enabled: enabled.checked },
      });
      await refresh();
    });
  });

  save.addEventListener("click", () => {
    busy(save, async () => {
      await api(`/api/modules/${encodeURIComponent(module.id)}`, {
        method: "PATCH", body: collectPatch(entry),
      });
      await refresh();
    });
  });

  run.addEventListener("click", () => {
    busy(run, async () => {
      const result = await api(`/api/modules/${encodeURIComponent(module.id)}/run`, {
        method: "POST", body: {},
      });
      if (!result.ok) showBanner(`${module.name}: ${result.error}`);
      else if (result.print_skipped) showBanner(`${module.name}: not printed (${result.print_skipped})`);
      await refresh();
    });
  });

  showLogs.addEventListener("click", () => {
    logFilter.value = module.id;
    renderLogs();
    goToTab("printer", { focus: true });
  });

  return entry;
}

function readValue(input, schema) {
  if (schema.type === "bool") return input.checked;
  if (schema.type === "int") {
    if (input.value === "") return schema.optional ? null : 0;
    return Number(input.value);
  }
  return input.value;
}

function collectPatch(entry) {
  const patch = {
    interval_seconds: Number(entry.interval.value),
    limits: {},
    options: {},
  };
  for (const item of entry.fieldEntries) {
    const value = readValue(item.input, item.schema);
    if (item.kind === "limit") patch.limits[item.key] = value;
    else patch.options[item.key] = value;
  }
  return patch;
}

function updateCard(entry, module) {
  entry.module = module;
  entry.title.textContent = module.name;
  entry.desc.textContent = module.description;
  setIfIdle(entry.enabled, module.enabled);
  const runtime = module.runtime;
  let label = "never";
  let badgeClass = "badge never";
  if (runtime.running) { label = "running"; badgeClass = "badge running"; }
  else if (runtime.last_ok === true) { label = "ok"; badgeClass = "badge ok"; }
  else if (runtime.last_ok === false) { label = "error"; badgeClass = "badge error"; }
  entry.badge.textContent = label;
  entry.badge.className = badgeClass;
  const parts = [];
  if (runtime.last_run_at) parts.push(`last ${runtime.last_run_at}`);
  parts.push(`${runtime.run_count} run${runtime.run_count === 1 ? "" : "s"}`);
  if (runtime.next_due_seconds !== null && runtime.next_due_seconds !== undefined) {
    parts.push(`next in ${runtime.next_due_seconds}s`);
  }
  if (runtime.consecutive_failures) {
    parts.push(`${runtime.consecutive_failures} failure(s), backoff ${runtime.effective_interval_seconds}s`);
  }
  entry.meta.textContent = parts.join(" · ");
  setValueIfIdle(entry.interval, module.interval_seconds);
  for (const item of entry.fieldEntries) {
    const value = item.kind === "limit" ? module.limits[item.key] : module.options[item.key];
    setValueIfIdle(item.input, value);
  }
  entry.error.textContent = runtime.print_error || runtime.last_error || "";
  entry.note.textContent = runtime.print_skipped ? `Not printed: ${runtime.print_skipped}` : "";
  entry.output.textContent = module.output.text || "No output yet.";
}

// ---- server, printer, simulator --------------------------------------

function updateServer() {
  const server = state.server;
  const bits = [
    `v${server.version}`,
    `up ${server.uptime_seconds}s`,
    server.scheduler_running ? "scheduler on" : "scheduler off",
  ];
  if (server.scheduler_running && server.last_tick_age_seconds !== null) {
    bits.push(`tick ${server.last_tick_age_seconds}s ago`);
  }
  if (server.scheduler_stale) bits.push("SCHEDULER STALLED");
  const errors = state.registry_errors || [];
  if (errors.length) bits.push(`${errors.length} quarantined module(s)`);
  serverStatus.textContent = bits.join(" · ");
  serverStatus.title = errors.map((item) => `${item.module}: ${item.error}`).join("\n");
  serverStatus.classList.toggle("error", Boolean(server.scheduler_stale));

  const scheduler = [server.scheduler_running ? "running" : "stopped"];
  if (server.scheduler_running && server.last_tick_age_seconds !== null) {
    scheduler.push(`last tick ${server.last_tick_age_seconds}s ago`);
  }
  if (server.scheduler_stale) scheduler.push("STALLED");
  const rows = [
    ["Version", server.version],
    ["Address", server.host && server.port ? `${server.host}:${server.port}` : ""],
    ["Uptime", `${server.uptime_seconds}s`],
    ["Scheduler", scheduler.join(" · ")],
    ["Modules", `${state.modules.length} loaded, ${state.modules.filter((m) => m.enabled).length} enabled`],
    ["Config", server.config_path],
    ["Modules dir", server.modules_dir],
  ];
  for (const item of errors) rows.push(["Quarantined", `${item.module}: ${item.error}`]);
  serverInfo.replaceChildren();
  for (const [key, value] of rows) {
    if (value === undefined || value === null || value === "") continue;
    serverInfo.append(make("dt", "", key), make("dd", "", String(value)));
  }
}

function updatePrinter() {
  const printer = state.printer;
  setIfIdle(document.getElementById("printer-enabled"), printer.enabled);
  setIfIdle(document.getElementById("printer-auto"), printer.auto_print);
  setValueIfIdle(document.getElementById("printer-paper"), printer.paper || DEFAULT_PAPER);
  setValueIfIdle(document.getElementById("printer-device"), printer.device);
  setValueIfIdle(document.getElementById("printer-queue"), printer.cups_queue);
  const target = printer.cups_queue ? `CUPS queue ${printer.cups_queue}` : printer.device;
  const bits = [printer.enabled ? `printing to ${target}` : "simulator only"];
  if (printer.paper) bits.push(printer.width_px ? `${printer.paper} paper, ${printer.width_px}px` : `${printer.paper} paper`);
  if (printer.last_print_at) bits.push(`last print ${printer.last_print_at}`);
  if (printer.disabled_reason) bits.push(printer.disabled_reason);
  if (printer.last_error) bits.push(`error: ${printer.last_error}`);
  printerState.textContent = bits.join(" · ");
}

// Shows the image at url, or the placeholder when url is null or the image
// failed to load. Only touches img.src when the URL changes, so polling
// doesn't reload (and flicker) the same receipt.
function showReceipt(url, emptyText) {
  receiptEmpty.textContent = emptyText;
  if (!url || url === imgFailed) {
    receiptImg.hidden = true;
    receiptEmpty.hidden = false;
    return;
  }
  if (url !== imgUrl) {
    imgUrl = url;
    receiptImg.src = url;
  }
  // Keep showing the previous image until the new one has loaded.
  if (receiptImg.complete && receiptImg.naturalWidth > 0) {
    receiptImg.hidden = false;
    receiptEmpty.hidden = true;
  }
}

function bindReceiptImage() {
  receiptImg.addEventListener("load", () => {
    if (receiptImg.src.endsWith(imgUrl)) imgFailed = null;
    receiptImg.hidden = false;
    receiptEmpty.hidden = true;
  });
  receiptImg.addEventListener("error", () => {
    imgFailed = imgUrl;
    receiptImg.hidden = true;
    receiptEmpty.hidden = false;
  });
}

function updateSimulator() {
  const ids = state.modules.map((module) => module.id);
  const wanted = ["total", ...ids];
  const existing = [...simSelect.options].map((option) => option.value);
  if (existing.join(",") !== wanted.join(",")) {
    simSelect.replaceChildren();
    simSelect.append(new Option("Total output", "total"));
    for (const module of state.modules) simSelect.append(new Option(module.name, module.id));
  }
  if (!wanted.includes(selected)) selected = "total";
  simSelect.value = selected;

  let generatedAt = null;
  let empty = "";
  let meta = "";
  if (selected === "total") {
    generatedAt = state.total.generated_at;
    empty = "No output from enabled modules.";
    meta = `${state.total.module_ids.length} enabled module(s) · ${generatedAt || "no output yet"}`;
  } else {
    const module = state.modules.find((item) => item.id === selected);
    if (module) {
      generatedAt = module.output.generated_at;
      empty = "No output yet for this module.";
      meta = generatedAt || "no output yet";
    }
  }
  const key = encodeURIComponent(selected);
  const url = generatedAt ? `/api/output/${key}.png?v=${encodeURIComponent(generatedAt)}` : null;
  showReceipt(url, empty);
  receiptImg.alt = `Receipt preview: ${simSelect.selectedOptions[0]?.textContent || selected}`;
  simText.href = `/api/output/${key}`;
  simMeta.textContent = meta;
}

// ---- logs -------------------------------------------------------------

function updateLogFilter() {
  const wanted = [["", "All logs"], [SERVER_LOGS, "Server"]];
  for (const module of state.modules) wanted.push([module.id, module.name]);
  const existing = [...logFilter.options].map((option) => option.value);
  if (existing.join(",") === wanted.map(([value]) => value).join(",")) return;
  const current = logFilter.value;
  logFilter.replaceChildren();
  for (const [value, label] of wanted) logFilter.append(new Option(label, value));
  logFilter.value = wanted.some(([value]) => value === current) ? current : "";
}

function renderLogs() {
  const filter = logFilter.value;
  const last = logEntries.length ? logEntries[logEntries.length - 1].seq : 0;
  const view = `${filter}|${last}|${logEntries.length}`;
  if (view === logView) return;
  logView = view;

  let shown = logEntries;
  if (filter === SERVER_LOGS) shown = shown.filter((item) => !item.module);
  else if (filter) shown = shown.filter((item) => item.module === filter);
  shown = shown.slice(-LOG_SHOWN);

  const atBottom = logList.scrollHeight - logList.scrollTop - logList.clientHeight < 8;
  logList.replaceChildren();
  for (const item of shown) {
    const li = make("li", `lvl-${item.level}`);
    const source = item.module ? ` [${item.module}]` : "";
    li.append(
      document.createTextNode(`${item.ts} `),
      make("span", "lvl", item.level),
      document.createTextNode(`${source} ${item.message}`),
    );
    logList.append(li);
  }
  if (!shown.length) logList.append(make("li", "", "No logs yet."));
  if (atBottom) logList.scrollTop = logList.scrollHeight;
}

async function refreshLogs() {
  const since = nextLogSeq === null ? 0 : nextLogSeq;
  const data = await api(`/api/logs?since=${since}`);
  const entries = data.entries || [];
  if (entries.length) {
    logEntries.push(...entries);
    if (logEntries.length > 2000) logEntries = logEntries.slice(-2000);
  }
  nextLogSeq = data.next === undefined ? since : data.next;
}

// ---- polling ------------------------------------------------------------

function updateAll() {
  updateServer();
  updatePrinter();

  const seen = new Set();
  for (const module of state.modules) {
    seen.add(module.id);
    const entry = cards.get(module.id) || buildCard(module);
    updateCard(entry, module);
  }
  for (const [id, entry] of cards) {
    if (!seen.has(id)) {
      entry.root.remove();
      cards.delete(id);
    }
  }
  updateSimulator();
  updateInstant();
  updateLogFilter();
  renderLogs();
}

async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    state = await api("/api/state");
    await refreshLogs();
    updateAll();
  } catch (error) {
    serverStatus.textContent = "server unreachable";
    serverStatus.classList.add("error");
    showBanner(error.message);
  } finally {
    refreshing = false;
  }
}

function bindPrinterControls() {
  const save = document.getElementById("printer-save");
  const test = document.getElementById("printer-test");
  save.addEventListener("click", () => {
    busy(save, async () => {
      await api("/api/printer", {
        method: "PATCH",
        body: {
          enabled: document.getElementById("printer-enabled").checked,
          auto_print: document.getElementById("printer-auto").checked,
          paper: document.getElementById("printer-paper").value,
          device: document.getElementById("printer-device").value,
          cups_queue: document.getElementById("printer-queue").value.trim(),
        },
      });
      await refresh();
    });
  });
  test.addEventListener("click", () => {
    busy(test, async () => {
      await api("/api/printer/test", { method: "POST", body: {} });
      await refresh();
    });
  });
}

// ---- instant print ---------------------------------------------------
// ---- long prints -----------------------------------------------------
// Instant text and web pages print whole. Over CONFIRM_CHARS characters,
// a dialog asks first, since they use a lot of paper.
const CONFIRM_CHARS = 40000;
const confirmDialog = document.getElementById("confirm-print");
const confirmText = document.getElementById("confirm-print-text");

function paperLength(mm) {
  return mm >= 1000 ? `${(mm / 1000).toFixed(1)} m` : `${Math.max(1, Math.round(mm / 10))} cm`;
}

// Resolves true to print: at once for short prints, else when confirmed.
// ``what`` names the print, e.g. "This page"; ``print`` has chars, paper_mm.
function confirmLongPrint(what, print) {
  if (print.chars <= CONFIRM_CHARS) return Promise.resolve(true);
  confirmText.textContent =
    `${what} is ${print.chars.toLocaleString()} characters long and will use ` +
    `about ${paperLength(print.paper_mm)} of paper. Print all of it?`;
  confirmDialog.returnValue = "";
  confirmDialog.showModal();
  return new Promise((resolve) => {
    confirmDialog.addEventListener("close", () => resolve(confirmDialog.returnValue === "print"), { once: true });
  });
}

const INSTANT_DRAFT_KEY = "gpta.instant";
const instantText = document.getElementById("instant-text");
const instantSize = document.getElementById("instant-size");
const instantLayout = document.getElementById("instant-layout");
const instantFormat = document.getElementById("instant-format");
const instantImg = document.getElementById("instant-img");
const instantEmpty = document.getElementById("instant-empty");
const instantNote = document.getElementById("instant-note");
let instantTimer = null;
let instantSeq = 0;      // drops preview responses that arrive out of order
let instantPaper = null; // paper width the preview was drawn for
let instantShown = null; // the last preview, with the request it answers

function instantBody() {
  return {
    text: instantText.value,
    size: instantSize.value,
    layout: instantLayout.value,
    format: instantFormat.value,
  };
}

function saveInstantDraft() {
  try { localStorage.setItem(INSTANT_DRAFT_KEY, JSON.stringify(instantBody())); } catch { /* private mode */ }
}

function loadInstantDraft() {
  try {
    const draft = JSON.parse(localStorage.getItem(INSTANT_DRAFT_KEY) || "null");
    if (draft && typeof draft.text === "string") instantText.value = draft.text;
    const saved = [
      [instantSize, draft && draft.size],
      [instantLayout, draft && draft.layout],
      [instantFormat, draft && draft.format],
    ];
    for (const [select, value] of saved) {
      if ([...select.options].some((o) => o.value === value)) select.value = value;
    }
  } catch { /* nothing saved */ }
}

function showInstantCount(preview) {
  const chars = `${instantText.value.length.toLocaleString()} characters`;
  document.getElementById("instant-count").textContent = preview
    ? `${chars} · about ${paperLength(preview.paper_mm)} of paper`
    : chars;
}

// The preview for the current text and settings, fetched if needed.
async function currentInstantPreview() {
  const body = instantBody();
  const key = JSON.stringify(body);
  if (instantShown && instantShown.key === key) return instantShown.preview;
  const preview = await api("/api/instant/preview", { method: "POST", body });
  instantShown = { key, preview };
  return preview;
}

async function updateInstantPreview() {
  const seq = ++instantSeq;
  showInstantCount(null);
  if (!instantText.value.trim()) {
    instantImg.hidden = true;
    instantEmpty.hidden = false;
    return;
  }
  try {
    const preview = await currentInstantPreview();
    if (seq !== instantSeq) return;
    instantImg.src = preview.png;
    instantImg.hidden = false;
    instantEmpty.hidden = true;
    showInstantCount(preview);
  } catch (error) {
    if (seq === instantSeq) showBanner(error.message);
  }
}

function scheduleInstantPreview() {
  saveInstantDraft();
  clearTimeout(instantTimer);
  instantTimer = setTimeout(updateInstantPreview, 300);
}

function updateInstant() {
  const printer = state.printer;
  const disabled = "The printer is disabled. Turn it on in the Printer tab to print.";
  if (!printer.enabled) instantNote.textContent = disabled;
  else if (instantNote.textContent === disabled) instantNote.textContent = "";
  // Re-draw the preview when the paper width changes.
  if (instantPaper !== null && instantPaper !== printer.width_px) scheduleInstantPreview();
  instantPaper = printer.width_px;
}

function bindInstant() {
  loadInstantDraft();
  instantText.addEventListener("input", scheduleInstantPreview);
  instantSize.addEventListener("change", scheduleInstantPreview);
  instantLayout.addEventListener("change", scheduleInstantPreview);
  instantFormat.addEventListener("change", scheduleInstantPreview);
  const print = document.getElementById("instant-print");
  print.addEventListener("click", () => {
    busy(print, async () => {
      if (!instantText.value.trim()) throw new Error("Nothing to print yet.");
      if (instantText.value.length > CONFIRM_CHARS) {
        const preview = await currentInstantPreview();
        if (!(await confirmLongPrint("This text", preview))) return;
      }
      await api("/api/instant/print", { method: "POST", body: instantBody() });
      instantNote.textContent = `Printed at ${new Date().toLocaleTimeString()}.`;
      await refresh();
    });
  });
  document.getElementById("instant-clear").addEventListener("click", () => {
    instantText.value = "";
    scheduleInstantPreview();
    instantText.focus();
  });
  updateInstantPreview();
}

// ---- reader mode -----------------------------------------------------
// Whole pages print; long ones ask first (see confirmLongPrint).
const readerUrl = document.getElementById("reader-url");
const readerSize = document.getElementById("reader-size");
const readerLayout = document.getElementById("reader-layout");
const readerImg = document.getElementById("reader-img");
const readerEmpty = document.getElementById("reader-empty");
const readerMeta = document.getElementById("reader-meta");
const readerNote = document.getElementById("reader-note");
const readerPrint = document.getElementById("reader-print");
let readerPage = null; // the fetched page the preview shows; printed as-is

function readerSummary(page) {
  const chars = page.chars < page.total
    ? `the first ${page.chars.toLocaleString()} of ${page.total.toLocaleString()} characters`
    : `${page.chars.toLocaleString()} characters`;
  return `${chars} · about ${paperLength(page.paper_mm)} of paper`;
}

function showReaderPage(page) {
  readerPage = page;
  readerImg.src = page.png;
  readerImg.hidden = false;
  readerEmpty.hidden = true;
  readerPrint.disabled = false;
  readerMeta.textContent = `${page.title || page.url} · ${readerSummary(page)}`;
}

function bindReader() {
  const fetchButton = document.getElementById("reader-fetch");
  const fetchPage = (button = fetchButton) => busy(button, async () => {
    const url = readerUrl.value.trim();
    if (!url) throw new Error("Paste a page URL first.");
    readerMeta.textContent = "Fetching...";
    readerNote.textContent = "";
    try {
      showReaderPage(await api("/api/reader/fetch", {
        method: "POST",
        body: { url, size: readerSize.value, layout: readerLayout.value },
      }));
    } catch (error) {
      readerMeta.textContent = "";
      throw error;
    }
  });
  fetchButton.addEventListener("click", () => fetchPage());
  readerUrl.addEventListener("keydown", (event) => {
    if (event.key === "Enter") fetchPage();
  });
  // Re-fetch so the preview matches the new size (only after a fetch).
  for (const select of [readerSize, readerLayout]) {
    select.addEventListener("change", () => { if (readerPage) fetchPage(); });
  }
  readerPrint.addEventListener("click", () => {
    busy(readerPrint, async () => {
      if (!readerPage) throw new Error("Fetch a page first.");
      const page = readerPage;
      if (!(await confirmLongPrint("This page", page))) return;
      readerNote.textContent = `Printing ${readerSummary(page)}...`;
      try {
        await api("/api/reader/print", {
          method: "POST",
          body: { id: page.id, size: readerSize.value, layout: readerLayout.value },
        });
      } catch (error) {
        readerNote.textContent = "";
        throw error;
      }
      readerNote.textContent = `Printed at ${new Date().toLocaleTimeString()}.`;
      await refresh();
    });
  });
}

function bindSimulator() {
  simSelect.addEventListener("change", () => {
    selected = simSelect.value;
    if (state) updateSimulator();
  });
  bindReceiptImage();
}

function bindLogs() {
  logFilter.addEventListener("change", renderLogs);
}

async function init() {
  bindTabs();
  bindInstant();
  bindReader();
  bindPrinterControls();
  bindSimulator();
  bindLogs();
  await refresh();
  setInterval(refresh, 2000);
}

document.addEventListener("DOMContentLoaded", init);
