"use strict";

const modulesEl = document.getElementById("modules");
const simSelect = document.getElementById("sim-select");
const receiptEl = document.getElementById("receipt");
const simMeta = document.getElementById("sim-meta");
const bannerEl = document.getElementById("banner");
const serverStatus = document.getElementById("server-status");
const printerState = document.getElementById("printer-state");

let state = null;
let selected = "total";
let refreshing = false;
let bannerTimer = null;
let logEntries = [];
let nextLogSeq = null;
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
  const download = make("a", "", "Download");
  download.href = `/api/output/${encodeURIComponent(module.id)}`;
  download.target = "_blank";
  download.rel = "noopener";
  actions.append(save, run, download);

  const error = make("p", "card-error", "");
  const details = make("details");
  details.append(make("summary", "", "Debug output"));
  details.append(make("h3", "", "Latest receipt"));
  const output = make("pre", "module-output", "");
  details.append(output);
  details.append(make("h3", "", "Logs"));
  const logs = make("ul", "logs");
  details.append(logs);

  root.append(head, badges, fields, actions, error, details);
  modulesEl.append(root);

  const entry = {
    root, title, desc, enabled, badge, meta, interval,
    fieldEntries, save, run, download, error, output, logs, module, busy: false,
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
      await refresh();
    });
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
  parts.push(`${runtime.items} item${runtime.items === 1 ? "" : "s"}`);
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
  entry.output.textContent = module.output.text || "No output yet.";
  entry.download.href = `/api/output/${encodeURIComponent(module.id)}`;
  entry.logs.replaceChildren();
  const moduleLogs = logEntries.filter((item) => item.module === module.id).slice(-40);
  for (const item of moduleLogs) {
    const li = make("li", `lvl-${item.level}`, `${item.ts} ${item.message}`);
    entry.logs.append(li);
  }
  if (!moduleLogs.length) entry.logs.append(make("li", "muted", "No logs yet."));
}

function updatePrinter() {
  const printer = state.printer;
  setIfIdle(document.getElementById("printer-enabled"), printer.enabled);
  setIfIdle(document.getElementById("printer-auto"), printer.auto_print);
  setValueIfIdle(document.getElementById("printer-width"), printer.width);
  setValueIfIdle(document.getElementById("printer-device"), printer.device);
  const bits = [printer.enabled ? "physical printing enabled" : "simulator only"];
  if (printer.last_print_at) bits.push(`last print ${printer.last_print_at}`);
  if (printer.disabled_reason) bits.push(printer.disabled_reason);
  if (printer.last_error) bits.push(`error: ${printer.last_error}`);
  printerState.textContent = bits.join(" · ");
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

  let text = "";
  let meta = "";
  if (selected === "total") {
    text = state.total.text || "No output from enabled modules.";
    meta = `${state.total.module_ids.length} enabled module(s) · ${state.total.generated_at || "no output yet"}`;
  } else {
    const module = state.modules.find((item) => item.id === selected);
    if (module) {
      text = module.output.text || "No output yet for this module.";
      meta = module.output.generated_at || "no output yet";
    }
  }
  receiptEl.textContent = text;
  receiptEl.style.width = `${Math.max(28, Number(state.printer.width) + 4)}ch`;
  simMeta.textContent = meta;
}

function updateAll() {
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
          width: Number(document.getElementById("printer-width").value),
          device: document.getElementById("printer-device").value,
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

function bindSimulator() {
  simSelect.addEventListener("change", () => {
    selected = simSelect.value;
    if (state) updateSimulator();
  });
}

async function init() {
  bindPrinterControls();
  bindSimulator();
  await refresh();
  setInterval(refresh, 2000);
}

document.addEventListener("DOMContentLoaded", init);
