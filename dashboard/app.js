"use strict";

const POLL_MS = 2000;
const state = { events: new Map(), timer: null, loading: true, pollVersion: 0 };

const elements = {
  timeline: document.querySelector("#timeline"),
  empty: document.querySelector("#empty"),
  count: document.querySelector("#count"),
  connection: document.querySelector("#connection"),
  type: document.querySelector("#type-filter"),
  search: document.querySelector("#search"),
  clearFilters: document.querySelector("#clear-filters"),
  clearEvents: document.querySelector("#clear-events"),
  error: document.querySelector("#load-error"),
  errorText: document.querySelector("#load-error-text"),
  retry: document.querySelector("#retry"),
};

function safeStringify(value) {
  try {
    const serialized = JSON.stringify(value, null, 2);
    return serialized === undefined ? String(value) : serialized;
  }
  catch (error) { return `[Could not serialize value: ${String(error)}]`; }
}

function normalizedEvent(source, index) {
  const value = source && typeof source === "object" ? source : { data: source };
  const knownTypes = new Set(["model_request", "model_response", "tool_call", "tool_result", "error", "unknown"]);
  return {
    ...value,
    id: String(value.id ?? `invalid-${index}-${safeStringify(source).slice(0, 40)}`),
    type: knownTypes.has(value.type) ? value.type : "unknown",
    summary: typeof value.summary === "string" ? value.summary : "Unrecognized event",
    data: Object.hasOwn(value, "data") ? value.data : source,
  };
}

function setConnection(kind, text) {
  elements.connection.className = `status ${kind}`;
  elements.connection.replaceChildren();
  const dot = document.createElement("span");
  dot.className = "status-dot";
  elements.connection.append(dot, document.createTextNode(text));
}

function formatTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Time unavailable";
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function addSection(parent, title, value) {
  if (value === undefined || value === null) return;
  const section = document.createElement("section");
  section.className = "section";
  const heading = document.createElement("h3");
  heading.className = "section-title";
  heading.textContent = title;
  const pre = document.createElement("pre");
  pre.textContent = typeof value === "string" ? value : safeStringify(value);
  section.append(heading, pre);
  parent.append(section);
}

function semanticDetails(event, parent) {
  const data = event.data && typeof event.data === "object" ? event.data : {};
  if (event.type === "tool_call") addSection(parent, "Tool call and arguments", event.data);
  if (event.type === "tool_result") addSection(parent, "Tool result", event.data);
  const body = data.body && typeof data.body === "object" ? data.body : null;
  if (!body) return;

  if (Object.hasOwn(body, "system")) addSection(parent, "System prompt", body.system);
  if (Object.hasOwn(body, "developer")) addSection(parent, "Developer instructions", body.developer);
  if (Object.hasOwn(body, "instructions")) addSection(parent, "Instructions", body.instructions);
  if (Array.isArray(body.messages)) {
    body.messages.forEach((message, index) => {
      const role = message && typeof message === "object" && message.role ? String(message.role) : "unknown";
      addSection(parent, `${index + 1}. ${role} message`, message);
    });
  }
  if (Array.isArray(body.input)) {
    body.input.forEach((message, index) => {
      const role = message && typeof message === "object" && message.role ? String(message.role) : "input";
      addSection(parent, `${index + 1}. ${role}`, message);
    });
  }
  if (Object.hasOwn(body, "tools")) addSection(parent, "Complete tool definitions", body.tools);
  if (Array.isArray(body.content)) addSection(parent, "Response content", body.content);
  if (Object.hasOwn(body, "output")) addSection(parent, "Response output", body.output);
  if (Object.hasOwn(body, "choices")) addSection(parent, "Response choices", body.choices);
  if (body.stream && Array.isArray(body.events)) addSection(parent, "Stream events", body.events);
}

function chip(text) {
  const item = document.createElement("span");
  item.className = "chip";
  item.textContent = text;
  return item;
}

function eventCallId(event) {
  if (event.metadata?.callId) return String(event.metadata.callId);
  if (!event.data || typeof event.data !== "object") return null;
  if (event.type === "tool_result") return String(event.data.tool_use_id ?? event.data.call_id ?? "") || null;
  if (event.type === "tool_call") return String(event.data.id ?? event.data.call_id ?? "") || null;
  return null;
}

function pairedCallIds(events) {
  const callTypes = new Map();
  events.forEach((source, index) => {
    const event = normalizedEvent(source, index);
    const callId = eventCallId(event);
    if (!callId || !["tool_call", "tool_result"].includes(event.type)) return;
    if (!callTypes.has(callId)) callTypes.set(callId, new Set());
    callTypes.get(callId).add(event.type);
  });
  return new Set([...callTypes].filter(([, types]) => types.size === 2).map(([callId]) => callId));
}

function renderEvent(source, index, pairedIds) {
  const event = normalizedEvent(source, index);
  const details = document.createElement("details");
  details.className = "event";
  details.dataset.type = event.type;
  const callId = eventCallId(event);
  if (callId && pairedIds.has(callId)) details.classList.add("paired");

  const summary = document.createElement("summary");
  const mark = document.createElement("span");
  mark.className = "event-mark";
  mark.setAttribute("aria-hidden", "true");
  const main = document.createElement("div");
  main.className = "event-main";
  const top = document.createElement("div");
  top.className = "event-topline";
  const type = document.createElement("span");
  type.className = "type";
  type.textContent = event.type.replaceAll("_", " ");
  top.append(type);
  const label = document.createElement("div");
  label.className = "summary";
  label.textContent = event.summary;
  main.append(top, label);

  const chips = document.createElement("div");
  chips.className = "chips";
  if (event.metadata?.model) chips.append(chip(String(event.metadata.model)));
  if (event.requestId) chips.append(chip(`request · ${event.requestId}`));
  if (callId) {
    const pairChip = chip(
      pairedIds.has(callId)
        ? `${event.type === "tool_call" ? "paired result" : "result for call"} · ${callId}`
        : `call · ${callId}`
    );
    pairChip.classList.add("pair-chip");
    chips.append(pairChip);
  }
  if (chips.childElementCount) main.append(chips);

  const time = document.createElement("time");
  time.className = "event-time";
  time.textContent = formatTime(event.timestamp);
  summary.append(mark, main, time);

  const content = document.createElement("div");
  content.className = "details";
  const semantic = document.createElement("div");
  semantic.className = "semantic";
  try { semanticDetails(event, semantic); }
  catch (error) {
    const failure = document.createElement("p");
    failure.className = "render-error";
    failure.textContent = `Structured view unavailable: ${String(error)}`;
    semantic.append(failure);
  }
  const rawTitle = document.createElement("h3");
  rawTitle.className = "raw-title";
  rawTitle.textContent = "Complete redacted raw item";
  const raw = document.createElement("pre");
  raw.className = "raw";
  raw.textContent = safeStringify(event);
  content.append(semantic, rawTitle, raw);
  details.append(summary, content);
  return details;
}

function filteredEvents() {
  const type = elements.type.value;
  const search = elements.search.value.trim().toLocaleLowerCase();
  return [...state.events.values()].filter((event) => {
    if (type !== "all" && normalizedEvent(event, 0).type !== type) return false;
    if (!search) return true;
    const item = normalizedEvent(event, 0);
    return `${item.summary}\n${item.requestId ?? ""}\n${safeStringify(item)}`.toLocaleLowerCase().includes(search);
  }).reverse();
}

function render() {
  const events = filteredEvents();
  const pairedIds = pairedCallIds([...state.events.values()]);
  elements.timeline.replaceChildren();
  events.forEach((event, index) => {
    try { elements.timeline.append(renderEvent(event, index, pairedIds)); }
    catch (error) {
      const fallback = document.createElement("pre");
      fallback.className = "event render-error";
      fallback.textContent = `Item could not be rendered: ${String(error)}\n${safeStringify(event)}`;
      elements.timeline.append(fallback);
    }
  });
  elements.count.textContent = `${events.length} / ${state.events.size} events`;
  elements.empty.hidden = events.length !== 0;
  const filtered = state.events.size > 0 && events.length === 0;
  elements.empty.querySelector("h2").textContent = filtered ? "No events match these filters." : "No traffic captured yet.";
  elements.empty.querySelector("p").textContent = filtered ? "Clear or adjust the filters." : "Send a request through the proxy.";
}

async function loadEvents() {
  window.clearTimeout(state.timer);
  const pollVersion = ++state.pollVersion;
  try {
    const response = await fetch("/_monitor/events", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    if (!payload || !Array.isArray(payload.events)) throw new Error("Invalid monitor response");
    if (pollVersion !== state.pollVersion) return;
    for (const event of payload.events) {
      const normalized = normalizedEvent(event, state.events.size);
      state.events.set(normalized.id, normalized);
    }
    const serverIds = new Set(payload.events.map((event, index) => normalizedEvent(event, index).id));
    for (const id of state.events.keys()) {
      if (!serverIds.has(id)) state.events.delete(id);
    }
    state.loading = false;
    elements.error.hidden = true;
    setConnection("polling", "Polling");
    render();
  } catch (error) {
    if (pollVersion !== state.pollVersion) return;
    setConnection("disconnected", "Disconnected");
    elements.errorText.textContent = state.loading
      ? `Could not load captured traffic: ${String(error)}`
      : `Live update failed; displayed events were kept. ${String(error)}`;
    elements.error.hidden = false;
  } finally {
    if (pollVersion === state.pollVersion) {
      window.clearTimeout(state.timer);
      state.timer = window.setTimeout(loadEvents, POLL_MS);
    }
  }
}

elements.type.addEventListener("change", render);
elements.search.addEventListener("input", render);
elements.clearFilters.addEventListener("click", () => {
  elements.type.value = "all";
  elements.search.value = "";
  render();
});
elements.retry.addEventListener("click", loadEvents);
elements.clearEvents.addEventListener("click", async () => {
  if (!window.confirm("Clear all captured events from server memory?")) return;
  window.clearTimeout(state.timer);
  ++state.pollVersion;
  try {
    const response = await fetch("/_monitor/events", { method: "DELETE" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state.events.clear();
    elements.error.hidden = true;
    setConnection("polling", "Polling");
    render();
  } catch (error) {
    setConnection("disconnected", "Disconnected");
    elements.errorText.textContent = `Could not clear events: ${String(error)}`;
    elements.error.hidden = false;
  } finally {
    window.clearTimeout(state.timer);
    state.timer = window.setTimeout(loadEvents, POLL_MS);
  }
});

render();
loadEvents();
