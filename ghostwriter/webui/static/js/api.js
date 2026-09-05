// Vanilla JS frontend for ghost_writer's local web UI. No build step, no
// framework - fetch() against the FastAPI backend in app.py.
//
// Shared core: global state, DOM/status/busy helpers, and the fetch wrapper.
// Imported by every other module.
import { selectItem } from "./editor.js";

export const state = {
  slug: null,
  bible: null,
  selection: null,      // { kind: "chapter" | "characters" | "world", id: number | string }
  selectedHistoryId: null,
  universalQueue: [],         // pending tasks from a multi-task universal prompt, in order
  universalQueueIndex: 0,     // index of the task currently being reviewed
  touchedSections: [],        // { key, kind, id, label } - sections changed since last cleared, for the "click to review" bar
  universalMode: "do",        // "do" | "discuss" - the universal prompt bar's toggle
  discussHistory: [],         // [{ role: "writer"|"assistant", text }] - in-memory only, not persisted (scratch conversation, not book data)
};

export function kindLabel(kind) {
  return { chapter: "Chapter", characters: "Character", world: "World", research_notes: "Note", outline: "Outline", timeline: "Timeline event" }[kind] || kind;
}

// Persists the touched-sections bar and the universal-prompt task queue per
// project, so navigating away or reloading the page doesn't silently drop
// pending review work - both previously lived only in the in-memory state
// object and vanished on refresh.
export function persistTasks() {
  if (!state.slug) return;
  localStorage.setItem(`gw-tasks:${state.slug}`, JSON.stringify({
    touchedSections: state.touchedSections || [],
    universalQueue: state.universalQueue || [],
    universalQueueIndex: state.universalQueueIndex || 0,
  }));
}

export function restoreTasks(slug) {
  let saved = null;
  try {
    saved = JSON.parse(localStorage.getItem(`gw-tasks:${slug}`) || "null");
  } catch (_) {
    saved = null;
  }
  state.touchedSections = (saved && saved.touchedSections) || [];
  state.universalQueue = (saved && saved.universalQueue) || [];
  state.universalQueueIndex = (saved && saved.universalQueueIndex) || 0;
}

export function markTouched(kind, id, label) {
  const key = `${kind}:${id}`;
  state.touchedSections = (state.touchedSections || []).filter((t) => t.key !== key);
  state.touchedSections.push({ key, kind, id, label: label || `${kindLabel(kind)}: ${id}` });
  renderTouchedBar();
  persistTasks();
}

export function renderTouchedBar() {
  const bar = $("universal-touched-bar");
  const list = $("universal-touched-list");
  if (!bar || !list) return;
  list.innerHTML = "";
  const items = state.touchedSections || [];
  if (!items.length) { bar.classList.add("hidden"); return; }
  bar.classList.remove("hidden");
  for (const t of items) {
    const chip = document.createElement("span");
    chip.className = "touched-chip";
    chip.textContent = t.label;
    chip.addEventListener("click", () => selectItem(t.kind, t.id));
    list.appendChild(chip);
  }
}

export function clearTouched() {
  state.touchedSections = [];
  renderTouchedBar();
  persistTasks();
}

export function $(id) { return document.getElementById(id); }

// In-app replacements for window.confirm()/prompt() - the browser-native
// versions render as OS-chrome popups that look out of place and can't be
// styled, block the whole tab, and are flagged by some embedders. These
// build the same .modal/.modal-backdrop markup used elsewhere (see
// #unsaved-modal-backdrop) on the fly and resolve like their native
// counterparts (confirm -> boolean, prompt -> string or null on cancel).
function buildModal(bodyHtml, buttons) {
  const backdrop = document.createElement("div");
  backdrop.className = "modal-backdrop";
  const modal = document.createElement("div");
  modal.className = "modal";
  modal.innerHTML = bodyHtml;
  const actions = document.createElement("div");
  actions.className = "modal-actions";
  const els = {};
  for (const b of buttons) {
    const btn = document.createElement("button");
    btn.textContent = b.text;
    if (b.primary) btn.className = "primary";
    actions.appendChild(btn);
    els[b.key] = btn;
  }
  modal.appendChild(actions);
  backdrop.appendChild(modal);
  document.body.appendChild(backdrop);
  return { backdrop, modal, els };
}

export function uiConfirm(message, { okText = "OK", cancelText = "Cancel" } = {}) {
  return new Promise((resolve) => {
    const { backdrop, els } = buildModal(`<p class="modal-hint">${escapeHtml(message)}</p>`, [
      { key: "cancel", text: cancelText },
      { key: "ok", text: okText, primary: true },
    ]);
    const finish = (result) => { backdrop.remove(); resolve(result); };
    els.cancel.onclick = () => finish(false);
    els.ok.onclick = () => finish(true);
    els.ok.focus();
  });
}

export function uiPrompt(message, defaultValue = "") {
  return new Promise((resolve) => {
    const { backdrop, modal, els } = buildModal(
      `<p class="modal-hint">${escapeHtml(message)}</p><label><input type="text" id="ui-prompt-input"></label>`,
      [
        { key: "cancel", text: "Cancel" },
        { key: "ok", text: "OK", primary: true },
      ]
    );
    const input = modal.querySelector("#ui-prompt-input");
    input.value = defaultValue;
    const finish = (result) => { backdrop.remove(); resolve(result); };
    els.cancel.onclick = () => finish(null);
    els.ok.onclick = () => finish(input.value);
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") finish(input.value);
      if (e.key === "Escape") finish(null);
    });
    input.focus();
    input.select();
  });
}

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = String(s);
  return div.innerHTML;
}

export function setStatus(msg, isError = false) {
  const el = $("status-msg");
  el.textContent = msg;
  el.style.color = isError ? "#c0392b" : "";
}

let busyCount = 0;
export function showBusy() {
  busyCount++;
  $("busy-overlay").classList.remove("hidden");
  $("busy-bar").classList.remove("hidden");
  // Callers set a descriptive status message (e.g. "Drafting...") right
  // before making the request, so mirror it onto the bar as a label -
  // gives every AI call a labeled progress indicator for free, with no
  // per-call-site changes needed.
  $("busy-bar-label").textContent = $("status-msg").textContent;
}
// Emergency stop: aborts whatever LLM call is in flight right now (server
// stays up, unlike the llama-server start/stop toggle) - wired to the button
// in the busy bar, which is only visible while an AI call is running.
export async function emergencyStopLLM() {
  try {
    const res = await api("/api/llm/cancel", { method: "POST" }, { silent: true });
    setStatus(res.cancelled ? "Stop requested - the current AI call will halt shortly." : "Nothing in flight to stop.");
  } catch (err) {
    setStatus(err.message, true);
  }
}

export function hideBusy() {
  busyCount = Math.max(0, busyCount - 1);
  if (busyCount === 0) {
    $("busy-overlay").classList.add("hidden");
    $("busy-bar").classList.add("hidden");
    $("busy-bar-label").textContent = "";
  }
}

export function showFinalizeProgress() {
  $("finalize-progress").classList.remove("hidden");
  showBusy();
  updateFinalizeProgress({ step_index: 0, total_steps: 1, label: "Starting..." });
}
export function updateFinalizeProgress(job) {
  const pct = job.total_steps ? Math.round((job.step_index / job.total_steps) * 100) : 0;
  $("finalize-progress-fill").style.width = `${pct}%`;
  const label = `Finalizing: ${job.label} (${job.step_index}/${job.total_steps})`;
  $("finalize-progress-label").textContent = label;
  $("busy-bar-label").textContent = label;
}
export function hideFinalizeProgress() {
  $("finalize-progress").classList.add("hidden");
  hideBusy();
}

// Polls the finalize-pipeline job until it completes, updating the
// determinate progress bar as each of the 8 serial steps advances -
// the pipeline runs in a background thread server-side so this doesn't
// hold one long HTTP request open. Always returns the full job, even when
// job.error is set - an error on a late step (e.g. outline sync) doesn't
// mean earlier steps failed, and job.bible_proposals/timeline_proposals from
// those earlier steps must still reach the caller for review instead of
// being silently discarded. Callers should check job.error themselves and
// report it, but still process any proposals present.
export async function pollFinalizeJob(slug, chapterNum, jobId) {
  showFinalizeProgress();
  try {
    for (;;) {
      const job = await api(
        `/api/projects/${encodeURIComponent(slug)}/chapters/${chapterNum}/approve/status/${jobId}`,
        {},
        { silent: true }
      );
      updateFinalizeProgress(job);
      if (job.done) return job;
      await new Promise((resolve) => setTimeout(resolve, 800));
    }
  } finally {
    hideFinalizeProgress();
  }
}

// Same determinate progress bar as pollFinalizeJob, but for any step_index/
// total_steps/label job - e.g. the whole-book critique job, which isn't a
// single chapter's approve pipeline so it can't use pollFinalizeJob's
// hardcoded status URL.
export async function pollDeterminateJob(statusUrl) {
  showFinalizeProgress();
  try {
    for (;;) {
      const job = await api(statusUrl, {}, { silent: true });
      updateFinalizeProgress(job);
      if (job.done) {
        if (job.error) throw new Error(job.error);
        return job;
      }
      await new Promise((resolve) => setTimeout(resolve, 800));
    }
  } finally {
    hideFinalizeProgress();
  }
}

// Same progress bar as the finalize pipeline, but for the two streaming
// single-call jobs (chapter draft/revise) which have no step list - just a
// growing partial_text - so the fill is an indeterminate pulse (CSS) and the
// label shows a live word count instead of "step X/Y".
export function showStreamProgress(label) {
  $("finalize-progress").classList.remove("hidden");
  $("finalize-progress").classList.add("indeterminate");
  showBusy();
  updateStreamProgress(label, "");
}
export function updateStreamProgress(label, partialText) {
  const words = partialText ? partialText.trim().split(/\s+/).filter(Boolean).length : 0;
  const text = words ? `${label} (${words} words so far)` : `${label}...`;
  $("finalize-progress-label").textContent = text;
  $("busy-bar-label").textContent = text;
}
export function hideStreamProgress() {
  $("finalize-progress").classList.add("hidden");
  $("finalize-progress").classList.remove("indeterminate");
  hideBusy();
}

// Polls a draft/revise job's status endpoint until done, updating the live
// word count as partial_text grows. Returns the full job (job.result is the
// chapter revision dict) or throws job.error.
export async function pollStreamJob(statusUrl, label) {
  showStreamProgress(label);
  try {
    for (;;) {
      const job = await api(statusUrl, {}, { silent: true });
      const phaseLabel = job.phase === "researching" ? `Researching sources for ${label}` : label;
      updateStreamProgress(phaseLabel, job.partial_text);
      if (job.done) {
        if (job.error) throw new Error(job.error);
        return job;
      }
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
  } finally {
    hideStreamProgress();
  }
}

// Wires a button so a click disables it (and shows the busy bar via api())
// until its handler settles, so a slow AI call can't be fired twice - and
// flashes a transient checkmark/x via withInlineFeedback so success is
// visible right at the button, not just in the far-away status bar.
export function wireClick(id, handler) {
  const btn = $(id);
  btn.addEventListener("click", () => {
    withInlineFeedback(btn, handler).catch(() => {});
  });
}

// Shows a brief checkmark/x right next to a button after its action settles,
// so a save's success is visible at the point of the click instead of relying
// solely on the global status bar (easy to miss since it's far from the
// button). Purely additive - existing setStatus(err.message, true) error
// reporting is untouched.
export async function withInlineFeedback(btn, fn) {
  btn.disabled = true;
  const flash = (cls, text) => {
    const span = document.createElement("span");
    span.className = `save-feedback ${cls}`;
    span.textContent = text;
    btn.insertAdjacentElement("afterend", span);
    setTimeout(() => span.remove(), 1200);
  };
  try {
    const result = await fn();
    flash("ok", "✓");
    return result;
  } catch (err) {
    flash("err", "✗");
    setStatus(err.message, true);
    throw err;
  } finally {
    btn.disabled = false;
  }
}

export async function api(path, options, { silent = false } = {}) {
  if (!silent) showBusy();
  try {
    const res = await fetch(path, options);
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch (_) { /* ignore */ }
      throw new Error(detail);
    }
    if (res.status === 204) return null;
    return res.json();
  } finally {
    if (!silent) hideBusy();
  }
}
