// Canvas drawer editor: split-view revision editor, outline editor, story
// engine editor, progress/continuity overview, character sections, universal
// prompt routing, and the local LLM server status indicator.
import { $, api, kindLabel, markTouched, persistTasks, pollDeterminateJob, pollFinalizeJob, pollStreamJob, setStatus, state, uiConfirm, uiPrompt, withInlineFeedback } from "./api.js";
import { refreshChapterMeta, renderManuscript, selectChapter } from "./manuscript.js";
import { refreshBible, syncProjectToSeries } from "./projects.js";
import { renderSidebar } from "./sidebar.js";

// Single source of truth for status pills, shared by canvas card footers and
// the drawer's progress overview - previously these computed two different
// vocabularies that could disagree on the same chapter.
const STATUS_LABELS = {
  "not-drafted": "not drafted",
  "drafted-pending-review": "drafted (pending review)",
  "revised-pending-review": "revised (pending review)",
  approved: "approved",
  "needs-recheck": "needs recheck",
  "stale-flagged": "flagged by continuity check",
};

function statusBadge({ approved, needsRecheck, historyLen, hasOpenFlag }) {
  let key;
  if (needsRecheck) key = "needs-recheck";
  else if (hasOpenFlag) key = "stale-flagged";
  else if (approved) key = "approved";
  else if (historyLen > 1) key = "revised-pending-review";
  else if (historyLen === 1) key = "drafted-pending-review";
  else key = "not-drafted";

  const cls = key === "approved" ? "approved"
    : (key === "needs-recheck" || key === "stale-flagged") ? "warn"
    : (key === "drafted-pending-review" || key === "revised-pending-review") ? "pending"
    : "";
  return { text: STATUS_LABELS[key], cls, key };
}

export function chapterStatusBadge(ch) {
  const flags = (state.bible && state.bible.continuity_flags) || [];
  const flaggedByConsistencyCheck = flags.some(f => f.chapter_num === ch.chapter_num);
  return statusBadge({
    approved: !!ch.approved,
    needsRecheck: !!ch.needs_recheck,
    historyLen: (ch.history || []).length,
    hasOpenFlag: (ch.continuity_issues || []).length > 0 || flaggedByConsistencyCheck,
  });
}

export function entityStatusBadge(entity) {
  const flags = (state.bible && state.bible.continuity_flags) || [];
  const hasOpenFlag = flags.some(f => f.target_name === entity.name);
  return statusBadge({
    approved: !!entity.approved,
    needsRecheck: false,
    historyLen: (entity.history || []).length,
    hasOpenFlag,
  });
}

export function noteName(n) { return n.name || n.topic; }

// Ideas backlog items may optionally point at an existing chapter or entity
// ("relates to"), separate from promotion (which creates a brand-new
// outline entry). kind mirrors sidebar branch keys: "outline" (chapter,
// linked_id = chapter_num) | "characters" | "world" | "research_notes".
export function linkedIdeasFor(kind, id) {
  return (state.bible.ideas || []).filter(i => i.linked_kind === kind && String(i.linked_id) === String(id));
}

// -- drawer embedding: there is no floating drawer anymore. manuscript.js
// pulls #drawer-header/#drawer-body out of their off-screen home and drops
// them inline in the main editor page (either under a chapter, or - for
// non-chapter selections - into a standalone entity page) reusing
// renderEditor()/renderActions()/renderTimeline() unchanged since they only
// ever address elements by id, never by ancestor.
let embedHost = null;
let drawerHomeParent = null;
let entityHostActive = false;

export function embedDrawerInto(hostEl, { showHeader = false } = {}) {
  const header = $("drawer-header");
  const body = $("drawer-body");
  if (!drawerHomeParent) drawerHomeParent = body.parentElement;
  embedHost = hostEl;
  hostEl.appendChild(header);
  hostEl.appendChild(body);
  header.classList.toggle("hidden", !showHeader);
  header.classList.add("ms-embedded-header");
  body.classList.add("ms-embedded");
}

export function resetDrawerEmbed() {
  if (!embedHost) return;
  const header = $("drawer-header");
  const body = $("drawer-body");
  header.classList.remove("ms-embedded-header");
  body.classList.remove("ms-embedded");
  if (drawerHomeParent) {
    drawerHomeParent.appendChild(header);
    drawerHomeParent.appendChild(body);
  }
  embedHost = null;
}

export function showEmptyState() {
  resetDrawerEmbed();
  entityHostActive = false;
  $("empty-state").classList.remove("hidden");
}

// Builds a standalone page in the main editor view (#manuscript-view) for
// any non-chapter selection (character/world/note/act/outline/overview) -
// chapters instead embed into manuscript.js's own per-chapter slot.
function ensureEntityHost() {
  resetDrawerEmbed();
  const root = $("manuscript-view");
  root.innerHTML = "";
  root.classList.remove("hidden");
  $("tab-panel-settings").classList.add("hidden");
  const page = document.createElement("div");
  page.className = "ms-page";
  root.appendChild(page);
  const host = document.createElement("div");
  host.className = "ms-editor-slot entity-host";
  page.appendChild(host);
  embedDrawerInto(host, { showHeader: true });
  entityHostActive = true;
}

export function closeDrawer() {
  if (entityHostActive) {
    entityHostActive = false;
    renderManuscript();
  } else {
    resetDrawerEmbed();
  }
  state.selection = null;
}

// -- normalizing chapters/entities into a common {label, history, current} ---
export function getChapter(chapterNum) {
  return (state.bible.chapters || []).find(c => c.chapter_num === chapterNum) || null;
}

export function getEntity(kind, name) {
  return (state.bible[kind] || []).find(e => (e.name || e.topic) === name) || null;
}

export function getOutlineEntry(chapterNum) {
  return (state.bible.outline || []).find(o => o.chapter_num === chapterNum) || null;
}

// Returns { history, originalText, textField, approvedFlag, extra } where
// history always has at least one entry (synthesized from the base field for
// entities/chapters that predate the instruction-revise workflow).
function normalizedRecord() {
  const { kind, id } = state.selection;
  if (kind === "chapter") {
    const ch = getChapter(id) || { chapter_num: id };
    let history = ch.history || [];
    if (!history.length && ch.draft) {
      // synthesized: this entry doesn't exist in the server-side history list,
      // so its id can't be used as a history_id (approve would 404) until the
      // text is materialized as a real revision via the /edit endpoint.
      history = [{ id: 0, text: ch.draft, source: "draft", instruction: null, created_at: ch.created_at, synthesized: true }];
    }
    return { record: ch, history, approved: !!ch.approved, needsRecheck: !!ch.needs_recheck, issues: ch.continuity_issues || [] };
  }
  const entity = getEntity(kind, id) || { name: id };
  const field = kind === "characters" || kind === "timeline" ? "description" : "content";
  let history = entity.history || [];
  if (!history.length && entity[field]) {
    history = [{ id: 0, text: entity[field], source: "original", instruction: null, created_at: null, synthesized: true }];
  }
  return { record: entity, history, approved: !!entity.approved, needsRecheck: false, issues: [] };
}

// Sets state.selection and paints #drawer-body/#drawer-header for it,
// without deciding where those elements live in the page - manuscript.js's
// openEditor() calls this directly since it has already embedded the drawer
// into that chapter's own slot. selectItem() (below) is the public
// navigation entry point that also decides placement.
export function setSelection(kind, id) {
  state.selection = { kind, id };
  state.selectedHistoryId = null;
  renderEditor();
}

// Shows a styled in-app modal (instead of the browser's native confirm())
// asking whether to save, discard, or cancel when navigating away from a
// character with unsaved section edits. Resolves to "save"/"discard"/"cancel".
function confirmUnsavedCharacterSections(name) {
  return new Promise((resolve) => {
    $("unsaved-modal-text").textContent =
      `"${name}" has unsaved section changes (highlighted) that will be lost if you leave now. Save them before leaving?`;
    $("unsaved-modal-backdrop").classList.remove("hidden");
    const cleanup = (choice) => {
      $("unsaved-modal-backdrop").classList.add("hidden");
      $("unsaved-save").onclick = null;
      $("unsaved-discard").onclick = null;
      $("unsaved-cancel").onclick = null;
      resolve(choice);
    };
    $("unsaved-save").onclick = () => cleanup("save");
    $("unsaved-discard").onclick = () => cleanup("discard");
    $("unsaved-cancel").onclick = () => cleanup("cancel");
  });
}

export async function selectItem(kind, id) {
  if (!state.slug) return;
  if (state.selection && (state.selection.kind !== kind || state.selection.id !== id) &&
      hasUnsavedCharacterSections()) {
    const choice = await confirmUnsavedCharacterSections(state.selection.id);
    if (choice === "cancel") return;
    if (choice === "save") await saveAllCharacterSections();
  }
  if (kind === "chapter") {
    entityHostActive = false;
    selectChapter(id);
    return;
  }
  ensureEntityHost();
  setSelection(kind, id);
}

// Grows a textarea to fit its content instead of internally scrolling, so
// the edit box reads like the natural page flow of the draft/reading view.
function autoGrowTextarea(el) {
  el.style.height = "auto";
  el.style.height = `${el.scrollHeight}px`;
}
document.addEventListener("input", (e) => {
  if (e.target && e.target.tagName === "TEXTAREA" && e.target.classList &&
      (e.target.classList.contains("text") || e.target.classList.contains("autogrow"))) {
    autoGrowTextarea(e.target);
  }
});

function showEditorBody(mode) {
  // mode: "revision" (split-view + instruction bar + timeline) | "outline" | "overview" | "engine" | "act"
  $("outline-editor").classList.toggle("hidden", mode !== "outline");
  $("overview-view").classList.toggle("hidden", mode !== "overview");
  $("engine-editor").classList.toggle("hidden", mode !== "engine");
  $("act-editor").classList.toggle("hidden", mode !== "act");
  $("split-view").classList.toggle("hidden", mode !== "revision");
  $("timeline-wrap").classList.toggle("hidden", mode !== "revision");
  $("issues-box").classList.add("hidden");
  $("chapter-outline-box").classList.add("hidden");
}

async function elaborateActSummary(actName, instruction) {
  await api(`/api/projects/${encodeURIComponent(state.slug)}/acts/${encodeURIComponent(actName)}/elaborate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ instruction: instruction || null }),
  });
  await refreshBible();
  markTouched("act", actName, `Act: ${actName}`);
  renderActEditor(actName);
  setStatus("Act summary elaborated.");
}

function renderActEditor(actName) {
  const entry = (state.bible.acts || []).find(a => a.name === actName);
  $("ae-summary").value = entry ? entry.summary : "";
  $("ae-summary").onblur = async () => {
    const summary = $("ae-summary").value;
    await api(`/api/projects/${encodeURIComponent(state.slug)}/acts/${encodeURIComponent(actName)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ summary }),
    });
    await refreshBible();
    markTouched("act", actName, `Act: ${actName}`);
    setStatus("Act summary saved.");
  };

  const actions = $("editor-actions");
  actions.innerHTML = "";
  const elaborateBtn = document.createElement("button");
  elaborateBtn.textContent = "Elaborate with AI";
  elaborateBtn.title = "Expand this act's summary with AI, using its chapters as context. Optionally describe what to focus on.";
  elaborateBtn.addEventListener("click", async () => {
    const instruction = await uiPrompt(`Anything specific to focus on for ${actName}'s summary? (optional)`, "");
    if (instruction === null) return;
    withInlineFeedback(elaborateBtn, () => elaborateActSummary(actName, instruction.trim())).catch(() => {});
  });
  actions.appendChild(elaborateBtn);

  const box = $("act-chapter-issues");
  box.innerHTML = "";
  const chapters = (state.bible.outline || [])
    .filter(o => (o.act || "Unassigned act") === actName)
    .sort((a, b) => a.chapter_num - b.chapter_num);
  const heading = document.createElement("div");
  heading.className = "co-label";
  heading.textContent = "Continuity/voice issues by chapter";
  box.appendChild(heading);
  let anyIssues = false;
  for (const o of chapters) {
    const ch = getChapter(o.chapter_num);
    if (!ch) continue;
    const issues = [...(ch.continuity_issues || [])];
    if (ch.needs_recheck) issues.unshift("Flagged for recheck - an earlier chapter changed since this was approved.");
    if (!issues.length) continue;
    anyIssues = true;
    const block = document.createElement("div");
    block.className = "aci-chapter";
    const h5 = document.createElement("h5");
    h5.textContent = `Chapter ${o.chapter_num}${o.title ? ": " + o.title : ""}`;
    block.appendChild(h5);
    const ul = document.createElement("ul");
    for (const issue of issues) {
      const li = document.createElement("li");
      li.textContent = typeof issue === "string" ? issue : JSON.stringify(issue);
      ul.appendChild(li);
    }
    block.appendChild(ul);
    if ((ch.continuity_issues || []).length) {
      const fixBtn = document.createElement("button");
      fixBtn.textContent = "Fix with AI";
      fixBtn.addEventListener("click", () => {
        withInlineFeedback(fixBtn, () => fixContinuityIssues(o.chapter_num)).catch(() => {});
      });
      block.appendChild(fixBtn);
    }
    box.appendChild(block);
  }
  if (!anyIssues) {
    const none = document.createElement("div");
    none.className = "aci-none";
    none.textContent = "No open continuity/voice issues in this act's chapters.";
    box.appendChild(none);
  }
}

// -- diff/edit view toggle for the draft pane ---------------------------------
export function setPaneView(mode) {
  $("pane-right-text").classList.toggle("hidden", mode !== "diff");
  $("pane-right-edit").classList.toggle("hidden", mode !== "edit");
  $("btn-view-diff").classList.toggle("active", mode === "diff");
  $("btn-view-edit").classList.toggle("active", mode === "edit");
}

// -- diff rendering -----------------------------------------------------------
function renderDiffInto(container, segments) {
  container.innerHTML = "";
  for (const seg of segments) {
    if (seg.op === "equal") {
      container.appendChild(document.createTextNode(seg.text));
    } else if (seg.op === "insert") {
      const ins = document.createElement("ins");
      ins.textContent = seg.text;
      container.appendChild(ins);
    } else if (seg.op === "delete") {
      const del = document.createElement("del");
      del.textContent = seg.text;
      container.appendChild(del);
    }
  }
}

// Single-entry memo: the split-view chapter editor re-renders after nearly
// every unrelated action (see refreshBible() call sites), but the diffed
// text pair only actually changes when a new revision is selected/typed -
// this skips redoing the O(a*b) LCS table on every one of those re-renders.
let _wordDiffCache = null;

function wordDiff(oldText, newText) {
  if (_wordDiffCache && _wordDiffCache.oldText === oldText && _wordDiffCache.newText === newText) {
    return _wordDiffCache.segs;
  }
  const segs = _computeWordDiff(oldText, newText);
  _wordDiffCache = { oldText, newText, segs };
  return segs;
}

function _computeWordDiff(oldText, newText) {
  // Client-side mirror of ghostwriter/webui/diffing.py, used so we can
  // re-diff against the originally-loaded history without a round trip.
  const tokenize = (t) => t.match(/\s+|[^\s]+/g) || [];
  const a = tokenize(oldText), b = tokenize(newText);
  const dp = Array.from({ length: a.length + 1 }, () => new Array(b.length + 1).fill(0));
  for (let i = a.length - 1; i >= 0; i--) {
    for (let j = b.length - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const segs = [];
  let i = 0, j = 0;
  const push = (op, text) => {
    if (segs.length && segs[segs.length - 1].op === op) segs[segs.length - 1].text += text;
    else segs.push({ op, text });
  };
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) { push("equal", a[i]); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { push("delete", a[i]); i++; }
    else { push("insert", b[j]); j++; }
  }
  while (i < a.length) { push("delete", a[i]); i++; }
  while (j < b.length) { push("insert", b[j]); j++; }
  return segs;
}

// -- editor / split view -------------------------------------------------------
function renderEditorIdeaBadge(kind, id) {
  const heading = $("editor-heading");
  const existing = heading.querySelector(".editor-idea-badge");
  if (existing) existing.remove();
  if (!kind) return;
  const linkKind = kind === "faction" ? "world" : kind;
  const linked = linkedIdeasFor(linkKind, id);
  if (!linked.length) return;
  const badge = document.createElement("span");
  badge.className = "editor-idea-badge";
  badge.textContent = "💡";
  badge.title = `Pending idea: ${linked.map(i => i.title).join(", ")}`;
  heading.appendChild(badge);
}

export function renderEditor() {
  if (!state.selection) return;
  const { kind, id } = state.selection;

  const KIND_PILL_LABEL = { outline: "Outline", act: "Act", overview: "Overview", chapter: "Chapter", characters: "Character", world: "World", research_notes: "Note", timeline: "Timeline event" };
  // World entities share one bucket keyed by category (faction/location/
  // object-item/other) - show that category here instead of the generic
  // "World" label so the pill matches which sidebar branch it lives under.
  const WORLD_CATEGORY_PILL_LABEL = { faction: "Faction", location: "Location", object: "Object/Item", item: "Object/Item" };
  if (kind === "world") {
    const worldRecord = (state.bible.world || []).find(w => w.name === id);
    $("editor-kind-pill").textContent = WORLD_CATEGORY_PILL_LABEL[worldRecord?.category] || "World";
  } else {
    $("editor-kind-pill").textContent = KIND_PILL_LABEL[kind] || kind;
  }
  renderEditorIdeaBadge(null, null);

  const clearOverflow = () => {
    $("editor-overflow-menu").innerHTML = "";
    $("editor-overflow-wrap").classList.add("hidden");
    $("character-sections").classList.add("hidden");
    $("entity-rename-box").classList.add("hidden");
  };

  if (kind === "outline") {
    $("editor-title").textContent = `Outline: Chapter ${id}`;
    $("editor-actions").innerHTML = "";
    clearOverflow();
    showEditorBody("outline");
    renderOutlineEditor(id);
    return;
  }
  if (kind === "act") {
    $("editor-title").textContent = `Act: ${id}`;
    clearOverflow();
    showEditorBody("act");
    renderActEditor(id);
    return;
  }
  if (kind === "overview" && id === "engine") {
    $("editor-title").textContent = "Narrative Engine & Themes";
    clearOverflow();
    showEditorBody("engine");
    renderEngineEditor();
    return;
  }
  if (kind === "overview") {
    $("editor-title").textContent = id === "continuity" ? "Master Bible / Continuity" : id === "bibliography" ? "Bibliography" : id === "critique" ? "Critique" : "Progress Tracker";
    $("editor-actions").innerHTML = "";
    clearOverflow();
    showEditorBody("overview");
    renderOverview(id);
    return;
  }
  showEditorBody("revision");

  const { record, history, approved, needsRecheck, issues } = normalizedRecord();

  const kindLbl = { characters: "Character", world: "World", research_notes: "Note", timeline: "Timeline event" }[kind];
  // Title comes from the outline entry, not the chapter record's own
  // "title" field - that's a one-time snapshot copied in at draft time
  // and never refreshed, so it goes stale once the outline is later
  // regenerated/edited (finalize's outline-sync step, "Regenerate outline
  // entry", manual outline edits, whole-outline revise).
  const outlineTitle = kind === "chapter" ? getOutlineEntry(id)?.title : null;
  $("editor-title").textContent = kind === "chapter"
    ? `Chapter ${id}${outlineTitle ? ": " + outlineTitle : (record.title ? ": " + record.title : "")}`
    : `${kindLbl}: ${id}`;
  renderEditorIdeaBadge(kind, id);

  const issuesBox = $("issues-box");
  if (needsRecheck || issues.length) {
    issuesBox.classList.remove("hidden");
    issuesBox.innerHTML = "";
    const title = document.createElement("div");
    title.textContent = needsRecheck
      ? "An earlier chapter changed since this was approved - review for continuity:"
      : "Continuity/voice issues from the last check:";
    issuesBox.appendChild(title);
    if (issues.length) {
      const ul = document.createElement("ul");
      for (const issue of issues) {
        const li = document.createElement("li");
        li.textContent = typeof issue === "string" ? issue : JSON.stringify(issue);
        ul.appendChild(li);
      }
      issuesBox.appendChild(ul);
    }
  } else {
    issuesBox.classList.add("hidden");
  }

  const outlineBox = $("chapter-outline-box");
  if (kind === "chapter") {
    const outlineEntry = (state.bible.outline || []).find(o => o.chapter_num === id);
    if (outlineEntry && outlineEntry.summary) {
      outlineBox.classList.remove("hidden");
      outlineBox.innerHTML = "";
      const label = document.createElement("div");
      label.className = "co-label";
      label.textContent = outlineEntry.act ? `Outline summary (${outlineEntry.act})` : "Outline summary";
      const body = document.createElement("div");
      body.textContent = outlineEntry.summary;
      outlineBox.appendChild(label);
      outlineBox.appendChild(body);
    } else {
      outlineBox.classList.add("hidden");
    }
  } else {
    outlineBox.classList.add("hidden");
  }

  const selectedId = state.selectedHistoryId !== null ? state.selectedHistoryId : (history.length ? history[history.length - 1].id : null);
  const originalText = history.length ? history[0].text : "";
  const selectedEntry = history.find(h => h.id === selectedId) || history[history.length - 1] || { text: "" };

  const rightLabel = $("pane-right-label");
  const hasChanges = history.length > 0 && selectedEntry !== history[0];
  rightLabel.textContent = hasChanges ? `(${selectedEntry.source}${selectedEntry.instruction ? ": " + selectedEntry.instruction : ""})` : "(original)";
  $("pane-right").classList.toggle("tall", kind === "chapter");
  $("pane-right").classList.toggle("compact", kind !== "chapter");

  const textEl = $("pane-right-text"), editEl = $("pane-right-edit");
  renderDiffInto(textEl, wordDiff(originalText, selectedEntry.text || ""));
  editEl.value = selectedEntry.text || "";
  autoGrowTextarea(editEl);
  $("pane-view-toggle").classList.toggle("hidden", !hasChanges);
  setPaneView(hasChanges ? "diff" : "edit");

  renderTimeline(history, selectedId, approved, record);
  renderActions(kind, record, history, selectedEntry, approved, needsRecheck);

  const isNamedEntity = kind === "characters" || kind === "world" || kind === "research_notes";
  $("entity-rename-box").classList.toggle("hidden", !isNamedEntity);
  if (isNamedEntity) $("en-name").value = kind === "research_notes" ? noteName(record) : (record.name || "");

  $("character-sections").classList.toggle("hidden", kind !== "characters");
  $("character-factions").classList.toggle("hidden", kind !== "characters");
  if (kind === "characters") {
    renderCharacterSections(record);
    renderCharacterFactions(record);
  }

  const showsCategoryMove = kind === "world";
  $("world-category-box").classList.toggle("hidden", !showsCategoryMove);
  if (showsCategoryMove) {
    const cat = record.category === "item" ? "object" : record.category;
    $("wc-category").value = ["faction", "location", "object"].includes(cat) ? cat : "general";
  }

  const showsRealToggle = kind === "world" || kind === "characters";
  $("world-real-box").classList.toggle("hidden", !showsRealToggle);
  if (showsRealToggle) {
    $("wr-is-real").checked = !!record.is_real;
    $("wr-is-real-label").textContent = `This is a real ${realNoun(kind, record)} - keep it factually accurate`;
  }

  const showsObjectsLink = kind === "world" && record.category === "location";
  $("world-objects").classList.toggle("hidden", !showsObjectsLink);
  if (showsObjectsLink) renderWorldObjects(record);

  renderDerivedView(kind, record);

  $("timeline-consequence").classList.toggle("hidden", kind !== "timeline");
  if (kind === "timeline") {
    renderTimelineConsequence(record);
  }

  $("instruction").value = "";
}

const CHARACTER_SECTION_LABELS = {
  appearance: "Appearance",
  personality: "Personality",
  background: "Background",
  goals_motivation: "Goals / Motivation",
  relationships: "Relationships",
  arc: "Arc",
};

// Marks a section textarea dirty (unsaved) whenever its value diverges from
// the last-saved value, and clears the mark on save - so drafted-but-unsaved
// text (e.g. from "Draft missing sections with AI") stays visually obvious
// instead of silently vanishing if the writer navigates away without saving.
function markSectionDirty(textarea) {
  const dirty = textarea.value !== textarea.dataset.saved;
  textarea.classList.toggle("dirty", dirty);
  const wrap = textarea.closest(".cs-field");
  if (wrap) wrap.classList.toggle("cs-field-dirty", dirty);
}

export function hasUnsavedCharacterSections() {
  const fields = document.getElementById("cs-fields");
  if (!fields || $("character-sections").classList.contains("hidden")) return false;
  return Array.from(fields.querySelectorAll("textarea")).some((t) => t.value !== t.dataset.saved);
}

function renderCharacterSections(record) {
  const container = $("cs-fields");
  container.innerHTML = "";
  const sections = record.sections || {};

  for (const key of Object.keys(CHARACTER_SECTION_LABELS)) {
    const wrap = document.createElement("div");
    wrap.className = "cs-field";
    const label = document.createElement("label");
    label.textContent = CHARACTER_SECTION_LABELS[key];
    const textarea = document.createElement("textarea");
    textarea.value = sections[key] || "";
    textarea.dataset.saved = sections[key] || "";
    textarea.dataset.sectionKey = key;
    textarea.classList.add("autogrow");
    requestAnimationFrame(() => autoGrowTextarea(textarea));
    textarea.addEventListener("input", () => markSectionDirty(textarea));
    const saveBtn = document.createElement("button");
    saveBtn.textContent = "Save";
    saveBtn.addEventListener("click", () => {
      withInlineFeedback(saveBtn, () => saveCharacterSection(record.name, key, textarea.value)).catch(() => {});
    });
    wrap.appendChild(label);
    wrap.appendChild(textarea);
    wrap.appendChild(saveBtn);
    container.appendChild(wrap);
  }
}

export async function saveAllCharacterSections() {
  const { kind, id: name } = state.selection;
  if (kind !== "characters") return;
  const fields = Array.from($("cs-fields").querySelectorAll("textarea"));
  const dirty = fields.filter((t) => t.value !== t.dataset.saved);
  if (!dirty.length) {
    setStatus("Nothing to save - no sections have changed.");
    return;
  }
  setStatus(`Saving ${dirty.length} section${dirty.length > 1 ? "s" : ""}...`);
  const sections = {};
  for (const t of dirty) sections[t.dataset.sectionKey] = t.value;
  await api(`/api/projects/${encodeURIComponent(state.slug)}/characters/${encodeURIComponent(name)}/sections`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sections }),
  });
  for (const t of dirty) { t.dataset.saved = t.value; markSectionDirty(t); }
  await refreshBible();
  markTouched("characters", name, `Character: ${name}`);
  setStatus("Saved.");
}

function realNoun(kind, record) {
  if (kind === "characters") return "person";
  if (record.category === "object" || record.category === "item") return record.category;
  if (record.category === "faction") return "faction/group";
  return "place";
}

export async function saveWorldIsReal() {
  const { kind, id: name } = state.selection;
  if (kind !== "world" && kind !== "characters") return;
  const is_real = $("wr-is-real").checked;
  await api(`/api/projects/${encodeURIComponent(state.slug)}/entities/${kind}/${encodeURIComponent(name)}/real`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ is_real }),
  });
  await refreshBible();
  const noun = realNoun(kind, state.bible[kind]?.find((e) => (e.name || e.topic) === name) || {});
  setStatus(is_real ? `Marked as a real ${noun}.` : `Unmarked as a real ${noun}.`);
}

const WORLD_CATEGORY_BUCKET_LABEL = { faction: "Factions", location: "Locations", object: "Objects/Items", general: "Misc" };

export async function saveWorldCategory() {
  const { kind, id: name } = state.selection;
  if (kind !== "world") return;
  const category = $("wc-category").value;
  await api(`/api/projects/${encodeURIComponent(state.slug)}/entities/world/${encodeURIComponent(name)}/category`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ category }),
  });
  await refreshBible();
  markTouched("world", name, `World: ${name}`);
  renderSidebar();
  setStatus(`Moved to ${WORLD_CATEGORY_BUCKET_LABEL[category] || category}.`);
}

function renderWorldObjects(record) {
  const container = $("wo-checks");
  container.innerHTML = "";
  const objectEntries = (state.bible.world || []).filter((w) => w.category === "object" || w.category === "item");
  const active = new Set(record.used_objects || []);
  if (!objectEntries.length) {
    container.textContent = "No objects/items defined yet.";
    return;
  }
  for (const entry of objectEntries) {
    const label = document.createElement("label");
    label.className = "cf-check";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.value = entry.name;
    checkbox.checked = active.has(entry.name);
    label.appendChild(checkbox);
    label.appendChild(document.createTextNode(" " + entry.name));
    container.appendChild(label);
  }
}

export async function saveWorldObjects() {
  const { kind, id: name } = state.selection;
  if (kind !== "world") return;
  const checked = Array.from($("wo-checks").querySelectorAll("input[type=checkbox]:checked")).map((el) => el.value);
  setStatus("Saving linked objects...");
  await api(`/api/projects/${encodeURIComponent(state.slug)}/world/${encodeURIComponent(name)}/objects`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ objects: checked }),
  });
  await refreshBible();
  markTouched(kind, name, `World: ${name}`);
  renderSidebar();
  setStatus("Saved.");
}

// Computed, read-only cross-references - not stored data, just derived from
// the existing curated links (used_objects, character.factions) so a
// location/faction/object doesn't need its own rigid nested tree to see
// what's connected to it.
function timelineEventsFor(nameLower, field) {
  return (state.bible.timeline || []).filter((e) => (e[field] || []).some((n) => n.toLowerCase() === nameLower));
}

function formatTimelineEvents(events) {
  return events.map((e) => `${e.story_date ? e.story_date + ": " : ""}${e.name} - ${e.description}`).join("\n");
}

function renderDerivedView(kind, record) {
  const box = $("derived-view");
  if (kind !== "world" && kind !== "characters") { box.classList.add("hidden"); return; }
  const body = $("derived-view-body");
  const nameLower = record.name.toLowerCase();

  if (kind === "characters") {
    const events = timelineEventsFor(nameLower, "characters");
    $("derived-view-title").textContent = "Timeline Events";
    body.textContent = events.length ? formatTimelineEvents(events) : "No timeline events linked to this character yet.";
    box.classList.remove("hidden");
    return;
  }

  const category = record.category;
  const events = timelineEventsFor(nameLower, "locations");
  const eventsSection = `Timeline Events:\n${events.length ? formatTimelineEvents(events) : "No timeline events linked to this entry yet."}`;

  if (category === "faction") {
    const members = (state.bible.characters || []).filter((c) => (c.factions || []).includes(record.name)).map((c) => c.name);
    $("derived-view-title").textContent = "Related";
    body.textContent = `Members: ${members.length ? members.join(", ") : "none yet"}\n\n${eventsSection}`;
    box.classList.remove("hidden");
    return;
  }
  if (category === "object" || category === "item") {
    const locations = (state.bible.world || []).filter((w) => (w.used_objects || []).includes(record.name)).map((w) => w.name);
    $("derived-view-title").textContent = "Related";
    body.textContent = `Used At: ${locations.length ? locations.join(", ") : "not linked yet"}\n\n${eventsSection}`;
    box.classList.remove("hidden");
    return;
  }
  if (events.length) {
    $("derived-view-title").textContent = "Timeline Events";
    body.textContent = formatTimelineEvents(events);
    box.classList.remove("hidden");
    return;
  }
  box.classList.add("hidden");
}

function renderCharacterFactions(record) {
  const container = $("cf-checks");
  container.innerHTML = "";
  const factionEntries = (state.bible.world || []).filter((w) => w.category === "faction");
  const active = new Set(record.factions || []);
  if (!factionEntries.length) {
    container.textContent = "No factions defined yet.";
    return;
  }
  for (const entry of factionEntries) {
    const label = document.createElement("label");
    label.className = "cf-check";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.value = entry.name;
    checkbox.checked = active.has(entry.name);
    label.appendChild(checkbox);
    label.appendChild(document.createTextNode(" " + entry.name));
    container.appendChild(label);
  }
}

export async function saveCharacterFactions() {
  const { kind, id: name } = state.selection;
  if (kind !== "characters") return;
  const checked = Array.from($("cf-checks").querySelectorAll("input[type=checkbox]:checked")).map((el) => el.value);
  setStatus("Saving factions...");
  await api(`/api/projects/${encodeURIComponent(state.slug)}/characters/${encodeURIComponent(name)}/factions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ factions: checked }),
  });
  await refreshBible();
  markTouched(kind, name, `Character: ${name}`);
  renderSidebar();
  setStatus("Saved.");
}

function renderTimelineConsequence(record) {
  const dl = $("tc-character-options");
  dl.innerHTML = "";
  for (const c of state.bible?.characters || []) {
    const opt = document.createElement("option");
    opt.value = c.name;
    dl.appendChild(opt);
  }
  const consequence = record.consequence || null;
  $("tc-character").value = consequence ? consequence.character : "";
  $("tc-status").value = consequence ? consequence.status : "";
}

export async function saveTimelineConsequence() {
  const { kind, id: name } = state.selection;
  if (kind !== "timeline") return;
  const character = $("tc-character").value.trim();
  const status = $("tc-status").value.trim();
  if (!character || !status) {
    setStatus("Both character and new status are required.", true);
    return;
  }
  setStatus("Saving consequence...");
  await api(`/api/projects/${encodeURIComponent(state.slug)}/entities/timeline/${encodeURIComponent(name)}/consequence`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ character, status }),
  });
  await refreshBible();
  markTouched(kind, name, `Timeline: ${name}`);
  setStatus("Saved.");
}

export async function clearTimelineConsequence() {
  const { kind, id: name } = state.selection;
  if (kind !== "timeline") return;
  setStatus("Clearing consequence...");
  await api(`/api/projects/${encodeURIComponent(state.slug)}/entities/timeline/${encodeURIComponent(name)}/consequence`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "null",
  });
  $("tc-character").value = "";
  $("tc-status").value = "";
  await refreshBible();
  markTouched(kind, name, `Timeline: ${name}`);
  setStatus("Cleared.");
}

export async function renameEntity() {
  const { kind, id: oldName } = state.selection;
  const newName = $("en-name").value.trim();
  if (!newName) {
    setStatus("Name is required.", true);
    return;
  }
  if (newName === oldName) return;
  setStatus("Renaming...");
  await api(`/api/projects/${encodeURIComponent(state.slug)}/entities/${kind}/${encodeURIComponent(oldName)}/rename`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ new_name: newName }),
  });
  await refreshBible();
  selectItem(kind, newName);
  markTouched(kind, newName, `${kindLabel(kind)}: ${newName} (renamed from ${oldName})`);
  setStatus("Renamed.");
}

async function saveCharacterSection(name, key, text) {
  setStatus("Saving section...");
  await api(`/api/projects/${encodeURIComponent(state.slug)}/characters/${encodeURIComponent(name)}/sections`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sections: { [key]: text } }),
  });
  const textarea = $("cs-fields").querySelector(`textarea[data-section-key="${key}"]`);
  if (textarea) { textarea.dataset.saved = text; markSectionDirty(textarea); }
  await refreshBible();
  markTouched("characters", name, `Character: ${name}`);
  setStatus("Saved.");
}

export async function draftMissingCharacterSections() {
  const { kind, id } = state.selection;
  if (kind !== "characters") return;
  setStatus(`Drafting missing sections for ${id}...`);
  const result = await api(`/api/projects/${encodeURIComponent(state.slug)}/characters/${encodeURIComponent(id)}/sections/draft`, {
    method: "POST",
  });
  const drafted = result.sections || {};
  if (!Object.keys(drafted).length) {
    setStatus("No missing sections to draft (or nothing came back).");
    return;
  }
  for (const textarea of $("cs-fields").querySelectorAll("textarea")) {
    const key = textarea.dataset.sectionKey;
    if (drafted[key] && !textarea.value.trim()) {
      textarea.value = drafted[key];
      autoGrowTextarea(textarea);
      markSectionDirty(textarea);
    }
  }
  setStatus("Drafted - review below (unsaved sections are highlighted), then Save or Save all.");
}

// Unlike draftMissingCharacterSections (fills blanks only), this is called
// with a newly-established fact from a just-finalized chapter and can
// overwrite already-filled sections - so the textarea is replaced outright
// rather than left alone when non-empty. Nothing is saved until the writer
// reviews the (now-dirty) textarea and clicks Save, same review gate as
// every other section edit.
async function resyncCharacterSections(name, newFacts) {
  setStatus(`Checking ${name}'s sections against a new chapter fact...`);
  const result = await api(`/api/projects/${encodeURIComponent(state.slug)}/characters/${encodeURIComponent(name)}/sections/resync`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ new_facts: newFacts }),
  });
  const drafted = result.sections || {};
  if (!Object.keys(drafted).length) {
    setStatus("That fact didn't clearly belong in any existing section - nothing to update.");
    return;
  }
  for (const textarea of $("cs-fields").querySelectorAll("textarea")) {
    const key = textarea.dataset.sectionKey;
    if (drafted[key]) {
      textarea.value = drafted[key];
      autoGrowTextarea(textarea);
      markSectionDirty(textarea);
    }
  }
  setStatus(`Updated ${Object.keys(drafted).join(", ")} for ${name} - review (unsaved, highlighted) then Save or Save all.`);
}

function renderTimeline(history, selectedId, approved, record) {
  const el = $("timeline");
  el.innerHTML = "";
  $("timeline-wrap").open = history.length > 1;
  for (const entry of history) {
    const node = document.createElement("div");
    node.className = "tl-node" + (entry.id === selectedId ? " selected" : "");
    if (approved && record.final === entry.text) node.classList.add("approved");
    const src = document.createElement("span");
    src.className = "tl-source";
    src.textContent = entry.source;
    node.appendChild(src);
    if (entry.instruction) {
      const instr = document.createElement("span");
      instr.className = "tl-instruction";
      instr.textContent = entry.instruction;
      node.appendChild(instr);
    }
    node.addEventListener("click", () => {
      state.selectedHistoryId = entry.id;
      renderEditor();
    });
    el.appendChild(node);
  }
}

function renderActions(kind, record, history, selectedEntry, approved, needsRecheck) {
  const primary = $("editor-actions");
  const overflow = $("editor-overflow-menu");
  primary.innerHTML = "";
  overflow.innerHTML = "";

  const addBtn = (container, label, onClick, opts = {}) => {
    const btn = document.createElement("button");
    btn.textContent = label;
    if (opts.primary) btn.classList.add("primary");
    btn.addEventListener("click", () => {
      withInlineFeedback(btn, onClick).catch(() => {});
    });
    container.appendChild(btn);
    return btn;
  };

  if (kind === "chapter" && !history.length) {
    addBtn(primary, "Draft chapter", () => draftChapter(record.chapter_num), { primary: true });
    addBtn(overflow, "Edit outline summary", () => selectItem("outline", record.chapter_num));
    $("editor-overflow-wrap").classList.remove("hidden");
    return;
  }

  const finalizeLabel = kind === "chapter" ? "Finalize" : "Finalize this version";
  addBtn(primary, finalizeLabel, () => approveSelected(kind, record, selectedEntry), { primary: !approved })
    .title = kind === "chapter" ? "Runs editor + continuity/voice check + copyedit" : "";

  if (kind === "chapter") {
    addBtn(overflow, "Edit outline summary", () => selectItem("outline", record.chapter_num));
  }
  if (kind === "chapter" && history.length) {
    addBtn(overflow, needsRecheck ? "Check continuity" : "Re-check continuity", () => checkContinuity(record.chapter_num));
    addBtn(overflow, "Re-sync bible/timeline updates", () => syncChapterBible(record.chapter_num))
      .title = "Re-checks this chapter's finalized text for character/world/timeline updates, without re-editing the prose";
    const b = state.bible;
    if (b && (b.book_type === "nonfiction" || b.real_world_setting)) {
      addBtn(overflow, "Fact-check against real-world sources", () => factCheckChapter(record.chapter_num));
    }
    addBtn(overflow, "Critique chapter (pacing/stakes/craft)", () => critiqueChapter(record.chapter_num));
    if (approved) {
      addBtn(overflow, "Translate chapter", () => translateChapter(record.chapter_num))
        .title = "Translates this chapter's finalized text into the language selected under Settings > Translation";
    }
    addBtn(overflow, "Delete draft", () => deleteChapter(record.chapter_num));
  }
  if (kind === "characters" || kind === "world" || kind === "research_notes" || kind === "timeline") {
    addBtn(overflow, "Delete", () => deleteEntity(kind, record.name || record.topic));
  }

  $("editor-overflow-wrap").classList.toggle("hidden", overflow.children.length === 0);
}

// -- actions ------------------------------------------------------------------
// Old chapter/entity/scene revisions get folded into a synopsis once a
// history list passes StoryBible.HISTORY_CAP - this turns that into a
// human-readable suffix for the post-action status message.
function compactionNote(result) {
  const count = result && result.compacted_count;
  return count ? ` (${count} older revision${count === 1 ? "" : "s"} condensed to save space)` : "";
}

export async function draftChapter(chapterNum) {
  setStatus(`Drafting Chapter ${chapterNum}...`);
  const { job_id } = await api(
    `/api/projects/${encodeURIComponent(state.slug)}/chapters/${chapterNum}/draft`,
    { method: "POST" }
  );
  const job = await pollStreamJob(
    `/api/projects/${encodeURIComponent(state.slug)}/chapters/${chapterNum}/draft/status/${job_id}`,
    `Drafting Chapter ${chapterNum}`
  );
  await refreshBible();
  renderSidebar();
  renderEditor();
  refreshChapterMeta(chapterNum);
  const queued = queueResearchProposals(job.research_proposals);
  if (queued) {
    const first = state.universalQueue[0];
    const n = state.universalQueue.length;
    setStatus(`Drafted.${compactionNote(job.result)} Found ${n} research note${n === 1 ? "" : "s"} to review - starting with ${describeUniversalTask(first)}.`);
    try {
      await dispatchUniversalTask(first);
    } finally {
      updateUniversalQueueBar();
    }
  } else {
    setStatus(`Drafted.${compactionNote(job.result)}`);
  }
}

export async function reviseWithInstruction() {
  const instruction = $("instruction").value.trim();
  if (!instruction) return;
  const { kind, id } = state.selection;
  const label = kind === "chapter" ? `Chapter ${id}` : `${kindLabel(kind)}: ${id}`;
  setStatus(`Revising ${label}...`);
  let result;
  let queued = false;
  if (kind === "chapter") {
    const { job_id } = await api(
      `/api/projects/${encodeURIComponent(state.slug)}/chapters/${id}/revise`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ instruction }),
      }
    );
    const job = await pollStreamJob(
      `/api/projects/${encodeURIComponent(state.slug)}/chapters/${id}/revise/status/${job_id}`,
      `Revising ${label}`
    );
    result = job.result;
    queued = queueResearchProposals(job.research_proposals);
  } else {
    result = await api(`/api/projects/${encodeURIComponent(state.slug)}/entities/${kind}/${encodeURIComponent(id)}/revise`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ instruction }),
    });
  }
  await refreshBible();
  state.selectedHistoryId = null;
  renderSidebar();
  renderEditor();
  if (kind === "chapter") refreshChapterMeta(id);
  markTouched(kind, id, label);
  if (queued) {
    const first = state.universalQueue[0];
    const n = state.universalQueue.length;
    setStatus(`Revised.${compactionNote(result)} Found ${n} research note${n === 1 ? "" : "s"} to review - starting with ${describeUniversalTask(first)}.`);
    try {
      await dispatchUniversalTask(first);
    } finally {
      updateUniversalQueueBar();
    }
  } else {
    setStatus(`Revised.${compactionNote(result)}`);
  }
}

export async function saveManualEdit() {
  const text = $("pane-right-edit").value;
  const { kind, id } = state.selection;
  const path = kind === "chapter"
    ? `/api/projects/${encodeURIComponent(state.slug)}/chapters/${id}/edit`
    : `/api/projects/${encodeURIComponent(state.slug)}/entities/${kind}/${encodeURIComponent(id)}/edit`;
  if (kind === "chapter") {
    setStatus(`Saving Chapter ${id}, then finalizing (editor, continuity/voice check, copyedit, outline sync)...`);
  } else {
    setStatus(`Saving ${kindLabel(kind)}: ${id}...`);
  }
  const revision = await api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
  let finalizeJob = null;
  if (kind === "chapter") {
    const { job_id } = await api(`/api/projects/${encodeURIComponent(state.slug)}/chapters/${id}/approve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ history_id: revision.id }),
    });
    finalizeJob = await pollFinalizeJob(state.slug, id, job_id);
  }
  await refreshBible();
  state.selectedHistoryId = null;
  renderSidebar();
  renderEditor();
  if (kind === "chapter") refreshChapterMeta(id);
  markTouched(kind, id, kind === "chapter" ? `Chapter ${id}` : `${kindLabel(kind)}: ${id}`);
  const queued = finalizeJob && queueBibleProposals(finalizeJob.bible_proposals, id, finalizeJob.timeline_proposals);
  if (queued) {
    const first = state.universalQueue[0];
    const errNote = finalizeJob.error ? ` (finalize hit an error partway through: ${finalizeJob.error}, but earlier-step updates are still queued below)` : "";
    setStatus(`Saved and finalized. Found ${state.universalQueue.length} update${state.universalQueue.length === 1 ? "" : "s"} to review - starting with ${describeUniversalTask(first)}.${errNote}${compactionNote(revision)}`, !!finalizeJob.error);
    try {
      await dispatchUniversalTask(first);
    } finally {
      updateUniversalQueueBar();
    }
  } else if (finalizeJob && finalizeJob.error) {
    setStatus(`Saved, but finalize hit an error: ${finalizeJob.error}`, true);
  } else {
    setStatus(`${kind === "chapter" ? "Saved and finalized." : "Saved."}${compactionNote(revision)}`);
  }
}

async function approveSelected(kind, record, selectedEntry) {
  const { id } = state.selection;
  if (selectedEntry.synthesized) {
    // Legacy record whose only "history entry" was synthesized client-side -
    // create a real server-side revision first so history_id resolves.
    const editPath = kind === "chapter"
      ? `/api/projects/${encodeURIComponent(state.slug)}/chapters/${id}/edit`
      : `/api/projects/${encodeURIComponent(state.slug)}/entities/${kind}/${encodeURIComponent(id)}/edit`;
    const revision = await api(editPath, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: selectedEntry.text }),
    });
    selectedEntry = { ...selectedEntry, id: revision.id, synthesized: false };
  }
  const path = kind === "chapter"
    ? `/api/projects/${encodeURIComponent(state.slug)}/chapters/${id}/approve`
    : `/api/projects/${encodeURIComponent(state.slug)}/entities/${kind}/${encodeURIComponent(id)}/approve`;
  let finalizeJob = null;
  if (kind === "chapter") {
    setStatus(`Finalizing Chapter ${id}...`);
    const { job_id } = await api(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ history_id: selectedEntry.id }),
    });
    finalizeJob = await pollFinalizeJob(state.slug, id, job_id);
  } else {
    setStatus(`Finalizing ${kindLabel(kind)}: ${id}...`);
    await api(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ history_id: selectedEntry.id }),
    });
  }
  await refreshBible();
  state.selectedHistoryId = null;
  renderSidebar();
  renderEditor();
  if (kind === "chapter") refreshChapterMeta(id);
  markTouched(kind, id, kind === "chapter" ? `Chapter ${id} (approved)` : `${kindLabel(kind)}: ${id} (approved)`);
  const queued = finalizeJob && queueBibleProposals(finalizeJob.bible_proposals, id, finalizeJob.timeline_proposals);
  if (queued) {
    const first = state.universalQueue[0];
    const errNote = finalizeJob.error ? ` (finalize hit an error partway through: ${finalizeJob.error}, but earlier-step updates are still queued below)` : "";
    setStatus(`Finalized. Found ${state.universalQueue.length} update${state.universalQueue.length === 1 ? "" : "s"} to review - starting with ${describeUniversalTask(first)}.${errNote}`, !!finalizeJob.error);
    try {
      await dispatchUniversalTask(first);
    } finally {
      updateUniversalQueueBar();
    }
  } else if (finalizeJob && finalizeJob.error) {
    setStatus(`Finalize hit an error: ${finalizeJob.error}`, true);
  } else {
    setStatus("Finalized.");
  }
}

async function deleteEntity(kind, name) {
  const label = { characters: "character", world: "world entry", research_notes: "note" }[kind] || kind;
  if (!(await uiConfirm(`Delete ${label} "${name}"? This cannot be undone.`))) return;
  setStatus("Deleting...");
  await api(`/api/projects/${encodeURIComponent(state.slug)}/entities/${kind}/${encodeURIComponent(name)}`, { method: "DELETE" });
  await refreshBible();
  state.selectedHistoryId = null;
  closeDrawer();
  renderSidebar();
  setStatus("Deleted.");
}

async function deleteChapter(chapterNum) {
  if (!(await uiConfirm(`Delete Chapter ${chapterNum}'s drafted text and all its revision history? This cannot be undone. (The outline entry stays, so the chapter slot can be redrafted.)`))) return;
  setStatus("Deleting...");
  await api(`/api/projects/${encodeURIComponent(state.slug)}/chapters/${chapterNum}`, { method: "DELETE" });
  await refreshBible();
  state.selectedHistoryId = null;
  closeDrawer();
  renderSidebar();
  selectChapter(chapterNum);
  setStatus("Deleted.");
}

async function checkContinuity(chapterNum) {
  setStatus(`Checking continuity for Chapter ${chapterNum}...`);
  await api(`/api/projects/${encodeURIComponent(state.slug)}/chapters/${chapterNum}/check-continuity`, { method: "POST" });
  await refreshBible();
  renderSidebar();
  renderEditor();
  refreshChapterMeta(chapterNum);
  setStatus("");
}

// "Fix with AI" for a chapter's own continuity_issues/voice-check list (as
// opposed to continuity_flags, which already have a per-flag Fix button via
// continuityFlagToTask/dispatchUniversalTask). These issues aren't a
// structured flag - just strings from check-continuity - so there's nothing
// to convert into a universal task; instead this folds them straight into a
// revise instruction, then re-runs check-continuity so the list reflects
// what's actually still wrong instead of staying stale after the rewrite.
export async function fixContinuityIssues(chapterNum) {
  const ch = getChapter(chapterNum);
  const issues = (ch && ch.continuity_issues) || [];
  if (!issues.length) return;
  const instruction = `Fix these continuity/voice issues:\n- ${issues.join("\n- ")}`;
  setStatus(`Fixing Chapter ${chapterNum}'s flagged issues...`);
  const { job_id } = await api(
    `/api/projects/${encodeURIComponent(state.slug)}/chapters/${chapterNum}/revise`,
    { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ instruction }) }
  );
  await pollStreamJob(
    `/api/projects/${encodeURIComponent(state.slug)}/chapters/${chapterNum}/revise/status/${job_id}`,
    `Fixing Chapter ${chapterNum}`
  );
  await api(`/api/projects/${encodeURIComponent(state.slug)}/chapters/${chapterNum}/check-continuity`, { method: "POST" });
  await refreshBible();
  renderSidebar();
  renderEditor();
  refreshChapterMeta(chapterNum);
  markTouched("chapter", chapterNum, `Chapter ${chapterNum}`);
  const remaining = ((getChapter(chapterNum) || {}).continuity_issues) || [];
  setStatus(remaining.length
    ? `Fixed - but ${remaining.length} issue${remaining.length === 1 ? "" : "s"} still flagged.`
    : "Fixed - no continuity/voice issues remain.");
}

// Re-runs just the bible/timeline-proposal step against a chapter's current
// finalized text, without touching its prose - for recovering proposals a
// finalize run generated but never surfaced (e.g. a later pipeline step
// errored and the frontend used to discard the whole job, proposals and
// all). Nothing is written to the bible until each proposal is reviewed.
export async function syncChapterBible(chapterNum) {
  setStatus(`Re-checking Chapter ${chapterNum} for bible/timeline updates...`);
  const { bible_proposals, timeline_proposals } = await api(
    `/api/projects/${encodeURIComponent(state.slug)}/chapters/${chapterNum}/bible-sync`,
    { method: "POST" }
  );
  const queued = queueBibleProposals(bible_proposals, chapterNum, timeline_proposals);
  if (queued) {
    const first = state.universalQueue[0];
    setStatus(`Found ${state.universalQueue.length} update${state.universalQueue.length === 1 ? "" : "s"} to review - starting with ${describeUniversalTask(first)}.`);
    try {
      await dispatchUniversalTask(first);
    } finally {
      updateUniversalQueueBar();
    }
  } else {
    setStatus("No bible/timeline updates found.");
  }
}

export async function factCheckChapter(chapterNum) {
  setStatus(`Fact-checking Chapter ${chapterNum} against real-world sources...`);
  const { flags } = await api(`/api/projects/${encodeURIComponent(state.slug)}/chapters/${chapterNum}/fact-check`, { method: "POST" });
  await refreshBible();
  renderSidebar();
  renderEditor();
  setStatus(flags.length ? `Found ${flags.length} possible issue${flags.length === 1 ? "" : "s"} - see Continuity view.` : "No real-world factual issues found.");
}

// Developmental-editing pass (pacing/stakes/craft) for one chapter - distinct
// from checkContinuity (correctness) and factCheckChapter (real-world
// accuracy). Findings land in critique_flags, rendered in the Critique view.
export async function critiqueChapter(chapterNum) {
  setStatus(`Critiquing Chapter ${chapterNum} (pacing/stakes/craft)...`);
  const { flags } = await api(`/api/projects/${encodeURIComponent(state.slug)}/chapters/${chapterNum}/critique`, { method: "POST" });
  await refreshBible();
  renderSidebar();
  renderEditor();
  setStatus(flags.length ? `Found ${flags.length} craft issue${flags.length === 1 ? "" : "s"} - see Critique view.` : "No pacing/stakes/craft issues found.");
}

// Whole-book critique: runs the narrow checkers chapter-by-chapter then a
// book-level rollup, server-side as a background job (many LLM calls), so
// this polls it with the same determinate progress bar as finalize.
export async function critiqueBook() {
  if (!state.slug) return;
  setStatus("Starting whole-book critique...");
  const { job_id } = await api(`/api/projects/${encodeURIComponent(state.slug)}/critique-book`, { method: "POST" });
  const job = await pollDeterminateJob(
    `/api/projects/${encodeURIComponent(state.slug)}/critique-book/status/${job_id}`
  );
  await refreshBible();
  renderSidebar();
  renderEditor();
  const flags = (job.result && job.result.flags) || [];
  setStatus(flags.length ? `Whole-book critique found ${flags.length} issue${flags.length === 1 ? "" : "s"} - see Critique view.` : "Whole-book critique found no issues.");
}

// -- translation (final pass) ----------------------------------------------
export async function loadTranslateLanguages() {
  const languages = await api("/api/translate-languages");
  const sel = $("tr-language");
  const keep = sel.value;
  sel.innerHTML = "";
  for (const lang of languages) {
    const opt = document.createElement("option");
    opt.value = lang.key;
    opt.textContent = lang.pdf_supported ? lang.name : `${lang.name} (no PDF export)`;
    sel.appendChild(opt);
  }
  if (keep && languages.some(l => l.key === keep)) sel.value = keep;
}

// Whole-book final translation pass, same determinate-progress job pattern as
// critiqueBook - many LLM calls (one per chapter, plus a one-time glossary
// pass), so it runs server-side as a background job.
export async function translateBook() {
  if (!state.slug) return;
  const language = $("tr-language").value;
  if (!language) { setStatus("Select a translation language first.", true); return; }
  const retranslate = $("tr-retranslate").checked;
  setStatus("Starting whole-book translation...");
  const { job_id } = await api(`/api/projects/${encodeURIComponent(state.slug)}/translate-book`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ language, retranslate }),
  });
  const job = await pollDeterminateJob(
    `/api/projects/${encodeURIComponent(state.slug)}/translate-book/status/${job_id}`
  );
  const result = job.result || {};
  const errs = result.errors || [];
  setStatus(
    errs.length
      ? `Translated ${result.translated || 0} chapter(s), ${errs.length} failed - see console for details.`
      : `Translated ${result.translated || 0} chapter(s).`
  );
  if (errs.length) console.warn("Translation errors:", errs);
}

// Single-chapter translation - one LLM call (plus a possible one-time
// glossary call for any not-yet-seen proper names), so unlike translateBook
// this runs synchronously rather than as a background job, same as
// factCheckChapter/critiqueChapter.
export async function translateChapter(chapterNum) {
  const language = $("tr-language").value;
  if (!language) { setStatus("Select a translation language under Settings > Translation first.", true); return; }
  setStatus(`Translating Chapter ${chapterNum}...`);
  await api(`/api/projects/${encodeURIComponent(state.slug)}/chapters/${chapterNum}/translate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ language }),
  });
  setStatus(`Chapter ${chapterNum} translated.`);
}

// -- outline editor -------------------------------------------------------------
function refreshActOptions() {
  const dl = $("act-options");
  dl.innerHTML = "";
  const acts = new Set((state.bible.outline || []).map(o => o.act).filter(Boolean));
  for (const act of acts) {
    const opt = document.createElement("option");
    opt.value = act;
    dl.appendChild(opt);
  }
}

function renderOutlineEditor(chapterNum) {
  const entry = getOutlineEntry(chapterNum) || { chapter_num: chapterNum, title: "", act: "", summary: "" };
  refreshActOptions();
  $("oe-chapter-num").value = entry.chapter_num;
  $("oe-title").value = entry.title || "";
  $("oe-act").value = entry.act || "";
  $("oe-summary").value = entry.summary || "";
  $("oe-characters").value = (entry.characters || []).join(", ");
  $("oe-outline").value = entry.outline || "";

  const actions = $("editor-actions");
  actions.innerHTML = "";
  const addBtn = (label, onClick, opts = {}) => {
    const btn = document.createElement("button");
    btn.textContent = label;
    if (opts.primary) btn.classList.add("primary");
    btn.addEventListener("click", () => {
      withInlineFeedback(btn, onClick).catch(() => {});
    });
    actions.appendChild(btn);
    return btn;
  };
  addBtn("Save", () => saveOutlineEntry(chapterNum), { primary: true });
  addBtn("Regenerate with AI", () => regenerateOutlineEntry(chapterNum));
  addBtn("Delete", () => deleteOutlineEntry(chapterNum));
  addBtn("Back to draft", () => selectItem("chapter", chapterNum));

  renderScenes(chapterNum, entry);
}

// -- scene-level planning -------------------------------------------------
const SCENE_TRANSITIONS = ["continuous", "same-day", "time-skip", "pov-shift"];

// A plan proposal isn't saved until applied - held here, keyed by chapter,
// same review-before-apply pattern as suggest_outline_entry/revise_outline.
let pendingScenePlan = null; // { chapterNum, scenes: [...] }

function scenesApiBase(chapterNum) {
  return `/api/projects/${encodeURIComponent(state.slug)}/outline/${chapterNum}/scenes`;
}

function renderScenes(chapterNum, entry) {
  const list = $("oe-scenes-list");
  list.innerHTML = "";
  const draftBtn = $("oe-draft-from-scenes");

  if (pendingScenePlan && pendingScenePlan.chapterNum === chapterNum) {
    draftBtn.classList.add("hidden");
    pendingScenePlan.scenes.forEach((scene, idx) => list.appendChild(renderProposedSceneCard(scene, idx)));
    const bar = document.createElement("div");
    bar.className = "oe-scenes-plan-bar";
    const applyBtn = document.createElement("button");
    applyBtn.textContent = "Apply scene plan";
    applyBtn.classList.add("primary");
    applyBtn.addEventListener("click", () => withInlineFeedback(applyBtn, () => applyScenePlan(chapterNum)).catch(() => {}));
    const discardBtn = document.createElement("button");
    discardBtn.textContent = "Discard";
    discardBtn.addEventListener("click", () => { pendingScenePlan = null; renderEditor(); });
    bar.appendChild(applyBtn);
    bar.appendChild(discardBtn);
    list.appendChild(bar);
    return;
  }

  const scenes = (entry.scenes || []).slice().sort((a, b) => a.scene_num - b.scene_num);
  const allDrafted = scenes.length > 0 && scenes.every(s => s.draft);
  draftBtn.classList.toggle("hidden", scenes.length === 0);
  draftBtn.disabled = !allDrafted;
  draftBtn.title = allDrafted ? "" : "Every scene needs a draft first";

  for (const scene of scenes) list.appendChild(renderSavedSceneCard(chapterNum, scene));
}

function renderProposedSceneCard(scene, idx) {
  const card = document.createElement("div");
  card.className = "scene-card scene-card-proposed";

  const label = document.createElement("div");
  label.className = "scene-card-label";
  label.textContent = `Scene ${idx + 1} (proposed)`;
  card.appendChild(label);

  const beats = document.createElement("textarea");
  beats.rows = 3;
  beats.value = scene.beats || "";
  beats.placeholder = "What happens in this scene, from open to close.";
  beats.addEventListener("input", () => { scene.beats = beats.value; });
  card.appendChild(beats);

  const row = document.createElement("div");
  row.className = "scene-card-row";

  const transition = document.createElement("select");
  for (const t of SCENE_TRANSITIONS) {
    const opt = document.createElement("option");
    opt.value = t;
    opt.textContent = t;
    if (t === (scene.transition || "continuous")) opt.selected = true;
    transition.appendChild(opt);
  }
  transition.addEventListener("change", () => { scene.transition = transition.value; });
  row.appendChild(transition);

  const emotion = document.createElement("input");
  emotion.placeholder = "Emotional arc, e.g. curious -> afraid";
  emotion.value = scene.emotional_state || "";
  emotion.addEventListener("input", () => { scene.emotional_state = emotion.value; });
  row.appendChild(emotion);

  const pov = document.createElement("input");
  pov.placeholder = "POV (if different from chapter)";
  pov.value = scene.pov || "";
  pov.addEventListener("input", () => { scene.pov = pov.value; });
  row.appendChild(pov);

  const location = buildSceneLocationInput(scene.location || "");
  location.addEventListener("input", () => { scene.location = location.value; });
  row.appendChild(location);

  card.appendChild(row);
  return card;
}

function buildSceneLocationInput(value) {
  const locations = (state.bible.world || []).filter(e => e.category === "location");
  const input = document.createElement("input");
  input.placeholder = "Location (e.g. The Hollow Docks)";
  input.value = value;
  input.setAttribute("list", "scene-location-options");
  if (!document.getElementById("scene-location-options")) {
    const datalist = document.createElement("datalist");
    datalist.id = "scene-location-options";
    document.body.appendChild(datalist);
  }
  const datalist = document.getElementById("scene-location-options");
  datalist.innerHTML = "";
  for (const loc of locations) {
    const opt = document.createElement("option");
    opt.value = loc.name;
    datalist.appendChild(opt);
  }
  return input;
}

function renderSavedSceneCard(chapterNum, scene) {
  const card = document.createElement("div");
  card.className = "scene-card" + (scene.approved ? " scene-card-approved" : "");

  const label = document.createElement("div");
  label.className = "scene-card-label";
  label.textContent = `Scene ${scene.scene_num}${scene.approved ? " (approved)" : ""}`;
  card.appendChild(label);

  const beats = document.createElement("textarea");
  beats.rows = 3;
  beats.value = scene.beats || "";
  beats.dataset.saved = beats.value;
  beats.addEventListener("blur", () => {
    if (beats.value !== beats.dataset.saved) editSceneField(chapterNum, scene.scene_num, { beats: beats.value });
  });
  card.appendChild(beats);

  const row = document.createElement("div");
  row.className = "scene-card-row";

  const transition = document.createElement("select");
  for (const t of SCENE_TRANSITIONS) {
    const opt = document.createElement("option");
    opt.value = t;
    opt.textContent = t;
    if (t === (scene.transition || "continuous")) opt.selected = true;
    transition.appendChild(opt);
  }
  transition.addEventListener("change", () => editSceneField(chapterNum, scene.scene_num, { transition: transition.value }));
  row.appendChild(transition);

  const emotion = document.createElement("input");
  emotion.placeholder = "Emotional arc, e.g. curious -> afraid";
  emotion.value = scene.emotional_state || "";
  emotion.dataset.saved = emotion.value;
  emotion.addEventListener("blur", () => {
    if (emotion.value !== emotion.dataset.saved) editSceneField(chapterNum, scene.scene_num, { emotional_state: emotion.value });
  });
  row.appendChild(emotion);

  const pov = document.createElement("input");
  pov.placeholder = "POV (if different from chapter)";
  pov.value = scene.pov || "";
  pov.dataset.saved = pov.value;
  pov.addEventListener("blur", () => {
    if (pov.value !== pov.dataset.saved) editSceneField(chapterNum, scene.scene_num, { pov: pov.value });
  });
  row.appendChild(pov);

  const location = buildSceneLocationInput(scene.location || "");
  location.dataset.saved = location.value;
  location.addEventListener("blur", () => {
    if (location.value !== location.dataset.saved) editSceneField(chapterNum, scene.scene_num, { location: location.value });
  });
  row.appendChild(location);

  card.appendChild(row);

  if (scene.draft) {
    const draftBox = document.createElement("div");
    draftBox.className = "scene-card-draft";
    draftBox.textContent = scene.draft;
    card.appendChild(draftBox);
  }

  const actions = document.createElement("div");
  actions.className = "scene-card-actions";
  const addBtn = (label2, onClick, primary) => {
    const btn = document.createElement("button");
    btn.textContent = label2;
    if (primary) btn.classList.add("primary");
    btn.addEventListener("click", () => withInlineFeedback(btn, onClick).catch(() => {}));
    actions.appendChild(btn);
    return btn;
  };

  if (!scene.draft) {
    addBtn("Draft with AI", () => draftScene(chapterNum, scene.scene_num), true);
  } else {
    addBtn("Redraft with AI", () => draftScene(chapterNum, scene.scene_num));
    if (!scene.approved) addBtn("Approve", () => approveScene(chapterNum, scene.scene_num));
  }
  addBtn("Delete", () => deleteScene(chapterNum, scene.scene_num));
  card.appendChild(actions);

  if (scene.draft) {
    const reviseRow = document.createElement("div");
    reviseRow.className = "scene-card-revise";
    const instr = document.createElement("input");
    instr.placeholder = "Instruction to revise this scene...";
    const reviseBtn = document.createElement("button");
    reviseBtn.textContent = "Revise";
    reviseBtn.addEventListener("click", () => withInlineFeedback(reviseBtn, () => {
      const text = instr.value.trim();
      if (!text) return Promise.resolve();
      return reviseScene(chapterNum, scene.scene_num, text);
    }).catch(() => {}));
    reviseRow.appendChild(instr);
    reviseRow.appendChild(reviseBtn);
    card.appendChild(reviseRow);
  }

  return card;
}

export async function planScenesWithAI() {
  const chapterNum = state.selection.id;
  setStatus(`Planning scenes for Chapter ${chapterNum}...`);
  const scenes = await api(`${scenesApiBase(chapterNum)}/plan`, { method: "POST" });
  pendingScenePlan = { chapterNum, scenes };
  renderEditor();
  setStatus("Scenes proposed - review and apply.");
}

async function applyScenePlan(chapterNum) {
  const scenes = pendingScenePlan.scenes;
  setStatus(`Saving scene plan for Chapter ${chapterNum}...`);
  await api(`${scenesApiBase(chapterNum)}/apply`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ scenes }),
  });
  pendingScenePlan = null;
  await refreshBible();
  renderEditor();
  markTouched("outline", chapterNum, `Outline: Chapter ${chapterNum} (scenes planned)`);
  setStatus("Scene plan saved.");
}

async function editSceneField(chapterNum, sceneNum, fields) {
  await api(`${scenesApiBase(chapterNum)}/${sceneNum}/edit`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(fields),
  });
  await refreshBible();
  renderEditor();
}

async function draftScene(chapterNum, sceneNum) {
  setStatus(`Drafting scene ${sceneNum} of Chapter ${chapterNum}...`);
  const { job_id } = await api(`${scenesApiBase(chapterNum)}/${sceneNum}/draft`, { method: "POST" });
  const job = await pollStreamJob(`${scenesApiBase(chapterNum)}/${sceneNum}/draft/status/${job_id}`, `Drafting scene ${sceneNum}`);
  await refreshBible();
  renderEditor();
  setStatus(`Scene drafted.${compactionNote(job.result)}`);
}

async function reviseScene(chapterNum, sceneNum, instruction) {
  setStatus(`Revising scene ${sceneNum} of Chapter ${chapterNum}...`);
  const { job_id } = await api(`${scenesApiBase(chapterNum)}/${sceneNum}/revise`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ instruction }),
  });
  const job = await pollStreamJob(`${scenesApiBase(chapterNum)}/${sceneNum}/revise/status/${job_id}`, `Revising scene ${sceneNum}`);
  await refreshBible();
  renderEditor();
  setStatus(`Scene revised.${compactionNote(job.result)}`);
}

async function approveScene(chapterNum, sceneNum) {
  await api(`${scenesApiBase(chapterNum)}/${sceneNum}/approve`, { method: "POST" });
  await refreshBible();
  renderEditor();
  setStatus("Scene approved.");
}

async function deleteScene(chapterNum, sceneNum) {
  if (!(await uiConfirm(`Delete scene ${sceneNum}? This cannot be undone.`))) return;
  await api(`${scenesApiBase(chapterNum)}/${sceneNum}`, { method: "DELETE" });
  await refreshBible();
  renderEditor();
  setStatus("Scene deleted.");
}

export async function draftFromScenes() {
  const chapterNum = state.selection.id;
  if (!(await uiConfirm(`Stitch this chapter's drafted scenes into one chapter draft? This creates a new chapter revision, same as the regular Draft button.`))) return;
  setStatus(`Stitching scenes for Chapter ${chapterNum}...`);
  const revision = await api(`${scenesApiBase(chapterNum)}/stitch`, { method: "POST" });
  await refreshBible();
  renderSidebar();
  selectItem("chapter", chapterNum);
  markTouched("chapter", chapterNum, `Chapter ${chapterNum} (drafted from scenes)`);
  setStatus(`Chapter drafted from scenes.${compactionNote(revision)}`);
}

async function regenerateOutlineEntry(chapterNum, instruction) {
  if (!instruction && !(await uiConfirm(`Regenerate chapter ${chapterNum}'s outline with AI? This will overwrite its current title/act/summary.`))) return;
  setStatus(`Regenerating outline entry for Chapter ${chapterNum}...`);
  await api(`/api/projects/${encodeURIComponent(state.slug)}/outline/${chapterNum}/regenerate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ instruction: instruction || null }),
  });
  await refreshBible();
  renderSidebar();
  renderEditor();
  markTouched("outline", chapterNum, `Outline: Chapter ${chapterNum}`);
  setStatus("Regenerated.");
}

async function saveOutlineEntry(chapterNum) {
  const title = $("oe-title").value.trim();
  const act = $("oe-act").value.trim();
  const summary = $("oe-summary").value.trim();
  const outline = $("oe-outline").value.trim();
  const characters = $("oe-characters").value.split(",").map(s => s.trim()).filter(Boolean);
  setStatus("Saving outline entry...");
  await api(`/api/projects/${encodeURIComponent(state.slug)}/outline/${chapterNum}/edit`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title, act, summary, outline, characters }),
  });
  await refreshBible();
  renderSidebar();
  renderEditor();
  markTouched("outline", chapterNum, `Outline: Chapter ${chapterNum}`);
  setStatus("Saved.");
}

async function deleteOutlineEntry(chapterNum) {
  if (!(await uiConfirm(`Delete outline entry for chapter ${chapterNum}? This does not delete any drafted chapter text.`))) return;
  setStatus("Deleting outline entry...");
  await api(`/api/projects/${encodeURIComponent(state.slug)}/outline/${chapterNum}`, { method: "DELETE" });
  await refreshBible();
  state.selection = null;
  closeDrawer();
  renderSidebar();
  setStatus("Deleted.");
}

function outlineToLines(entries) {
  const flat = s => (s || "").replace(/\n/g, " ");
  return entries
    .slice()
    .sort((a, b) => a.chapter_num - b.chapter_num)
    .map(e => `${e.chapter_num} | ${e.act || ""} | ${e.title || ""} | ${flat(e.summary)} | ${flat(e.outline)}`)
    .join("\n");
}

function linesToOutline(text) {
  return text.split("\n").map(l => l.trim()).filter(Boolean).map(line => {
    const [chapterNum, act, title, summary, outline] = line.split("|").map(s => s.trim());
    return {
      chapter_num: parseInt(chapterNum, 10),
      act: act || null,
      title: title || "",
      summary: summary || "",
      outline: outline || "",
    };
  }).filter(e => !isNaN(e.chapter_num));
}

export function showReviseOutlineStep(step) {
  $("ro-step-instruction").classList.toggle("hidden", step !== "instruction");
  $("ro-step-review").classList.toggle("hidden", step !== "review");
}

export function openReviseOutlineModal() {
  if (!state.slug) { setStatus("Select a project first.", true); return; }
  $("ro-instruction").value = "";
  showReviseOutlineStep("instruction");
  $("revise-outline-modal-backdrop").classList.remove("hidden");
}

export function closeReviseOutlineModal() {
  $("revise-outline-modal-backdrop").classList.add("hidden");
}

export async function draftOutlineRevision() {
  const instruction = $("ro-instruction").value.trim();
  if (!instruction) {
    setStatus("An instruction is required.", true);
    return;
  }
  setStatus("Asking AI to revise the whole outline...");
  const result = await api(`/api/projects/${encodeURIComponent(state.slug)}/outline/revise`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ instruction }),
  });
  $("ro-outline-text").value = outlineToLines(result);
  showReviseOutlineStep("review");
  setStatus("Drafted - review and edit before applying.");
}

export async function applyOutlineRevision() {
  const outline = linesToOutline($("ro-outline-text").value);
  if (!outline.length) {
    setStatus("Outline can't be empty.", true);
    return;
  }
  if (!(await uiConfirm("Apply this revision? It replaces the entire outline (already-drafted chapters are protected and will be rejected if missing)."))) return;
  setStatus("Applying outline revision...");
  await api(`/api/projects/${encodeURIComponent(state.slug)}/outline/apply`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ outline }),
  });
  closeReviseOutlineModal();
  await refreshBible();
  state.selection = null;
  closeDrawer();
  renderSidebar();
  markTouched("overview", "progress", "Outline (revised)");
  setStatus("Outline updated.");
}

let promotingIdea = null;

export function openOutlineModal(prefillAct = "") {
  if (!state.slug) { setStatus("Select a project first.", true); return; }
  promotingIdea = null;
  $("outline-modal-title").textContent = "New Outline Entry";
  $("outline-modal-hint").textContent = "Adds a chapter to the outline. Typing an Act that doesn't exist yet creates it - acts are just a label, not a separate list. Set the chapter # first, then either describe the chapter and let AI draft the title/act/summary, or fill them in yourself.";
  $("oo-create").textContent = "Create";
  refreshActOptions();
  $("oo-chapter-num").value = "";
  $("oo-title").value = "";
  $("oo-act").value = prefillAct;
  $("oo-summary").value = "";
  $("oo-outline").value = "";
  $("oo-freeform").value = "";
  $("outline-modal-backdrop").classList.remove("hidden");
}

export function openPromoteIdeaModal(idea) {
  if (!state.slug) { setStatus("Select a project first.", true); return; }
  promotingIdea = idea;
  $("outline-modal-title").textContent = `Promote Idea: ${idea.title}`;
  $("outline-modal-hint").textContent = "Give this idea an act and chapter # to add it to the outline. It's removed from the idea backlog once placed.";
  $("oo-create").textContent = "Promote";
  refreshActOptions();
  $("oo-chapter-num").value = "";
  $("oo-title").value = idea.title;
  $("oo-act").value = "";
  $("oo-summary").value = "";
  $("oo-outline").value = idea.notes || "";
  $("oo-freeform").value = "";
  $("outline-modal-backdrop").classList.remove("hidden");
}

export function closeOutlineModal() { promotingIdea = null; $("outline-modal-backdrop").classList.add("hidden"); }

export async function suggestOutlineEntry() {
  const chapterNum = parseInt($("oo-chapter-num").value, 10);
  const freeform = $("oo-freeform").value.trim();
  if (!chapterNum) {
    setStatus("Set the chapter # first.", true);
    return;
  }
  if (!freeform) {
    setStatus("Describe the chapter first.", true);
    return;
  }
  setStatus(`Asking AI to draft Chapter ${chapterNum}...`);
  const result = await api(`/api/projects/${encodeURIComponent(state.slug)}/outline/suggest`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt: freeform, chapter_num: chapterNum }),
  });
  $("oo-title").value = result.title || "";
  $("oo-act").value = result.act || "";
  $("oo-summary").value = result.summary || "";
  $("oo-outline").value = result.outline || "";
  setStatus("Drafted - review and edit before creating.");
}

export async function createOutlineEntry() {
  const chapterNum = parseInt($("oo-chapter-num").value, 10);
  const title = $("oo-title").value.trim();
  const act = $("oo-act").value.trim();
  const summary = $("oo-summary").value.trim();
  const outline = $("oo-outline").value.trim();
  if (!chapterNum || !title) {
    setStatus("Chapter # and title are required.", true);
    return;
  }
  const idea = promotingIdea;
  closeOutlineModal();
  if (idea) {
    setStatus("Promoting idea to outline...");
    await api(`/api/projects/${encodeURIComponent(state.slug)}/ideas/${idea.id}/promote`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chapter_num: chapterNum, act, summary, outline }),
    });
    if (title !== idea.title) {
      await api(`/api/projects/${encodeURIComponent(state.slug)}/outline/${chapterNum}/edit`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title }),
      });
    }
  } else {
    setStatus("Creating outline entry...");
    await api(`/api/projects/${encodeURIComponent(state.slug)}/outline`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chapter_num: chapterNum, title, act, summary, outline }),
    });
  }
  await refreshBible();
  renderSidebar();
  selectItem("outline", chapterNum);
  markTouched("outline", chapterNum, `Outline: Chapter ${chapterNum} (new)`);
  setStatus(idea ? "Idea promoted." : "Created.");
}

// -- idea backlog ---------------------------------------------------------
let editingIdeaId = null;

// "Relates to" option value packs kind+id as "kind|id" so a single <select>
// can pick across chapters and every entity bucket at once.
function refreshIdeaRelatesToOptions(selectedKind, selectedId) {
  const sel = $("idea-relates-to");
  sel.innerHTML = "";
  const none = document.createElement("option");
  none.value = "";
  none.textContent = "None";
  sel.appendChild(none);

  const addGroup = (label, items) => {
    if (!items.length) return;
    const group = document.createElement("optgroup");
    group.label = label;
    for (const opt of items) group.appendChild(opt);
    sel.appendChild(group);
  };

  const outlineOpts = (state.bible.outline || []).slice().sort((a, b) => a.chapter_num - b.chapter_num)
    .map(e => {
      const opt = document.createElement("option");
      opt.value = `outline|${e.chapter_num}`;
      opt.textContent = `Chapter ${e.chapter_num}${e.title ? ": " + e.title : ""}`;
      return opt;
    });
  addGroup("Chapters", outlineOpts);

  const optsFor = (items, kind) => items.map(e => {
    const opt = document.createElement("option");
    opt.value = `${kind}|${e.name || e.topic}`;
    opt.textContent = e.name || e.topic;
    return opt;
  });
  const actOpts = Array.from(new Set((state.bible.outline || []).map(o => o.act).filter(Boolean)))
    .map(act => {
      const opt = document.createElement("option");
      opt.value = `act|${act}`;
      opt.textContent = act;
      return opt;
    });
  addGroup("Acts", actOpts);

  const world = state.bible.world || [];
  addGroup("Characters", optsFor(state.bible.characters || [], "characters"));
  addGroup("Factions", optsFor(world.filter(e => e.category === "faction"), "world"));
  addGroup("Locations", optsFor(world.filter(e => e.category === "location"), "world"));
  addGroup("Objects/Items", optsFor(world.filter(e => e.category === "object" || e.category === "item"), "world"));
  addGroup("Misc", optsFor(world.filter(e => !["faction", "location", "object", "item"].includes(e.category)), "world"));
  addGroup("Research notes", optsFor(state.bible.research_notes || [], "research_notes"));

  sel.value = selectedKind && selectedId != null ? `${selectedKind}|${selectedId}` : "";
  filterIdeaRelatesToOptions("");
}

function filterIdeaRelatesToOptions(query) {
  const sel = $("idea-relates-to");
  const q = query.trim().toLowerCase();
  for (const group of sel.querySelectorAll("optgroup")) {
    let anyVisible = false;
    for (const opt of group.querySelectorAll("option")) {
      const match = !q || opt.textContent.toLowerCase().includes(q);
      opt.hidden = !match;
      if (match) anyVisible = true;
    }
    group.hidden = !anyVisible;
  }
}

export function openIdeaModal(idea = null) {
  if (!state.slug) { setStatus("Select a project first.", true); return; }
  editingIdeaId = idea ? idea.id : null;
  $("idea-modal-title").textContent = idea ? "Edit Idea" : "New Idea";
  $("idea-title").value = idea ? idea.title : "";
  $("idea-notes").value = idea ? (idea.notes || "") : "";
  $("idea-relates-search").value = "";
  refreshIdeaRelatesToOptions(idea ? idea.linked_kind : null, idea ? idea.linked_id : null);
  $("idea-modal-backdrop").classList.remove("hidden");
}

export function filterIdeaRelatesTo() {
  filterIdeaRelatesToOptions($("idea-relates-search").value);
}
export function closeIdeaModal() { $("idea-modal-backdrop").classList.add("hidden"); }

export async function saveIdea() {
  const title = $("idea-title").value.trim();
  const notes = $("idea-notes").value.trim();
  if (!title) {
    setStatus("A title is required.", true);
    return;
  }
  const relatesTo = $("idea-relates-to").value;
  const [linkedKind, linkedIdRaw] = relatesTo ? relatesTo.split("|") : [null, null];
  const linkedId = linkedKind === "outline" && linkedIdRaw != null ? parseInt(linkedIdRaw, 10) : linkedIdRaw;
  const ideaId = editingIdeaId;
  closeIdeaModal();
  setStatus(ideaId ? "Saving idea..." : "Adding idea...");
  if (ideaId) {
    await api(`/api/projects/${encodeURIComponent(state.slug)}/ideas/${ideaId}/edit`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title, notes, linked_kind: linkedKind, linked_id: linkedId }),
    });
  } else {
    await api(`/api/projects/${encodeURIComponent(state.slug)}/ideas`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title, notes, linked_kind: linkedKind, linked_id: linkedId }),
    });
  }
  await refreshBible();
  renderSidebar();
  if (!entityHostActive) renderManuscript();
  setStatus("Saved.");
}

// -- book overview: narrative engine + themes (editable) ----------------------
function renderEngineEditor() {
  const b = state.bible;
  $("ov-title").value = b.title || "";
  $("ov-premise").value = b.premise || "";
  $("ov-tone").value = b.tone || "";
  $("ov-voice").value = b.narrative_voice || "";
  $("ov-engine").value = b.narrative_engine || "";
  $("ov-themes").value = b.themes || "";
  $("ov-real-world").checked = !!b.real_world_setting;
  $("ov-total-words").value = b.total_word_target || "";
  $("ov-chapter-words").value = b.chapter_target_words || "";
  $("ov-author-name").value = b.author_name || "";
  $("ov-blurb").value = b.blurb || "";
  $("ov-copyright").value = b.copyright_text || "";
  $("ov-foreword").value = b.foreword || "";
  $("ov-acknowledgments").value = b.acknowledgments || "";
  $("ov-about-author").value = b.about_author || "";

  const actions = $("editor-actions");
  actions.innerHTML = "";
  const btn = document.createElement("button");
  btn.textContent = "Save";
  btn.classList.add("primary");
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    try { await saveEngine(); } catch (err) { setStatus(err.message, true); }
    finally { btn.disabled = false; }
  });
  actions.appendChild(btn);
  $("ov-instruction").value = "";
}

export async function reviseEngine() {
  const instruction = $("ov-instruction").value.trim();
  if (!instruction) {
    setStatus("Type an instruction first.", true);
    return;
  }
  setStatus("Asking AI to revise the story engine...");
  const result = await api(`/api/projects/${encodeURIComponent(state.slug)}/overview/revise`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ instruction }),
  });
  $("ov-premise").value = result.premise || "";
  $("ov-tone").value = result.tone || "";
  $("ov-voice").value = result.narrative_voice || "";
  $("ov-engine").value = result.narrative_engine || "";
  $("ov-themes").value = result.themes || "";
  setStatus("Drafted - review and click Save to keep it.");
}

async function saveEngine() {
  const premise = $("ov-premise").value.trim();
  const tone = $("ov-tone").value.trim();
  const narrative_voice = $("ov-voice").value.trim();
  const narrative_engine = $("ov-engine").value.trim();
  const themes = $("ov-themes").value.trim();
  const real_world_setting = $("ov-real-world").checked;
  const totalWordsRaw = $("ov-total-words").value.trim();
  const chapterWordsRaw = $("ov-chapter-words").value.trim();
  const total_word_target = totalWordsRaw ? parseInt(totalWordsRaw, 10) : null;
  const chapter_target_words = chapterWordsRaw ? parseInt(chapterWordsRaw, 10) : null;
  const author_name = $("ov-author-name").value.trim();
  const blurb = $("ov-blurb").value.trim();
  const copyright_text = $("ov-copyright").value.trim();
  const foreword = $("ov-foreword").value.trim();
  const acknowledgments = $("ov-acknowledgments").value.trim();
  const about_author = $("ov-about-author").value.trim();
  setStatus("Saving...");
  await api(`/api/projects/${encodeURIComponent(state.slug)}/overview`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      premise, tone, narrative_voice, narrative_engine, themes, real_world_setting, total_word_target, chapter_target_words,
      author_name, blurb, copyright_text, foreword, acknowledgments, about_author,
    }),
  });
  await refreshBible();
  renderSidebar();
  renderEditor();
  markTouched("overview", "engine", "Story Engine");
  setStatus("Saved.");
}

function renderOverview(id) {
  const el = $("overview-view");
  el.innerHTML = "";
  const b = state.bible;

  const section = (title, bodyEl) => {
    const h = document.createElement("h4");
    h.textContent = title;
    el.appendChild(h);
    el.appendChild(bodyEl);
  };
  const para = (text) => {
    const p = document.createElement("p");
    p.textContent = text;
    return p;
  };
  const list = (items) => {
    const ul = document.createElement("ul");
    for (const item of items) {
      const li = document.createElement("li");
      li.textContent = item;
      ul.appendChild(li);
    }
    return ul;
  };

  if (id === "continuity") {
    section("Premise", para(`${b.title || "Untitled"} (${b.genre || "general fiction"}) — ${b.premise || "no premise recorded"}`));

    if (b.series_title && b.series_sync_pending) {
      const body = document.createElement("div");
      body.appendChild(para(
        `This book has approved chapters with character/status changes (e.g. a death) that haven't been pushed to the "${b.series_title}" series bible yet - the next book won't see them until you sync.`
      ));
      const syncBtn = document.createElement("button");
      syncBtn.textContent = "Sync to Series";
      syncBtn.addEventListener("click", () => syncProjectToSeries());
      body.appendChild(syncBtn);
      section("Series sync needed", body);
    }

    const flagged = (b.chapters || []).filter(c => (c.continuity_issues || []).length || c.needs_recheck);
    const issuesBody = document.createElement("div");
    if (!flagged.length) {
      issuesBody.appendChild(para("None outstanding."));
    } else {
      for (const c of flagged) {
        const chBlock = document.createElement("div");
        const h5 = document.createElement("h5");
        h5.textContent = `Chapter ${c.chapter_num}`;
        chBlock.appendChild(h5);
        const ul = document.createElement("ul");
        if (c.needs_recheck) {
          const li = document.createElement("li");
          li.textContent = "Flagged for recheck (an earlier chapter changed since approval)";
          ul.appendChild(li);
        }
        for (const issue of c.continuity_issues || []) {
          const li = document.createElement("li");
          li.textContent = typeof issue === "string" ? issue : JSON.stringify(issue);
          ul.appendChild(li);
        }
        chBlock.appendChild(ul);
        if ((c.continuity_issues || []).length) {
          const fixBtn = document.createElement("button");
          fixBtn.textContent = "Fix with AI";
          fixBtn.addEventListener("click", () => {
            withInlineFeedback(fixBtn, () => fixContinuityIssues(c.chapter_num)).catch(() => {});
          });
          chBlock.appendChild(fixBtn);
        }
        issuesBody.appendChild(chBlock);
      }
    }
    section("Open continuity issues (this chapter's own recheck)", issuesBody);

    // Cross-cutting findings from "Check consistency" runs - covers
    // character/world/note kinds too, which have no other field to hold an
    // open issue on, and persists across sessions (unlike the queue built
    // at the moment a check is run) until the writer fixes or dismisses each.
    const openFlags = b.continuity_flags || [];
    const flagsBody = document.createElement("div");
    if (!openFlags.length) {
      flagsBody.appendChild(para("None outstanding."));
    } else {
      const ul = document.createElement("ul");
      for (const flag of openFlags) {
        const li = document.createElement("li");
        const where = flag.kind === "chapter" || flag.kind === "outline"
          ? `Chapter ${flag.chapter_num}`
          : `${flag.kind[0].toUpperCase()}${flag.kind.slice(1)} "${flag.target_name}"`;
        const span = document.createElement("span");
        span.textContent = `${where}: ${flag.issue} `;
        li.appendChild(span);

        const fixBtn = document.createElement("button");
        fixBtn.textContent = "Fix";
        fixBtn.addEventListener("click", async () => {
          await dispatchUniversalTask(continuityFlagToTask(flag));
          await resolveContinuityFlag(flag.id);
        });
        li.appendChild(fixBtn);

        const dismissBtn = document.createElement("button");
        dismissBtn.textContent = "Dismiss";
        dismissBtn.addEventListener("click", () => resolveContinuityFlag(flag.id));
        li.appendChild(dismissBtn);

        ul.appendChild(li);
      }
      flagsBody.appendChild(ul);
    }
    section("Open continuity issues (from Check consistency runs)", flagsBody);
    return;
  }

  if (id === "critique") {
    const runBody = document.createElement("div");
    runBody.appendChild(para("Developmental-editing pass: pacing, stakes/tension, and craft (show-vs-tell, POV). Separate from the correctness-focused Continuity view. Run per-chapter from a chapter's overflow menu, or the whole book below."));
    const runBtn = document.createElement("button");
    runBtn.textContent = "Critique whole book";
    runBtn.addEventListener("click", () => {
      withInlineFeedback(runBtn, () => critiqueBook()).catch(() => {});
    });
    runBody.appendChild(runBtn);
    section("Run", runBody);

    const openFlags = b.critique_flags || [];
    const CATEGORY_LABELS = { pacing: "Pacing", stakes: "Stakes / tension", craft: "Craft (show-vs-tell, POV)", book: "Whole-book patterns" };
    const flagsBody = document.createElement("div");
    if (!openFlags.length) {
      flagsBody.appendChild(para("None outstanding."));
    } else {
      for (const category of ["pacing", "stakes", "craft", "book"]) {
        const inCategory = openFlags.filter(f => (f.category || "") === category);
        if (!inCategory.length) continue;
        const h5 = document.createElement("h5");
        h5.textContent = CATEGORY_LABELS[category] || category;
        flagsBody.appendChild(h5);
        const ul = document.createElement("ul");
        for (const flag of inCategory) {
          const li = document.createElement("li");
          const span = document.createElement("span");
          span.textContent = `Chapter ${flag.chapter_num}: ${flag.issue} `;
          li.appendChild(span);

          const fixBtn = document.createElement("button");
          fixBtn.textContent = "Fix";
          fixBtn.addEventListener("click", async () => {
            await dispatchUniversalTask(continuityFlagToTask(flag));
            await resolveCritiqueFlag(flag.id);
          });
          li.appendChild(fixBtn);

          const dismissBtn = document.createElement("button");
          dismissBtn.textContent = "Dismiss";
          dismissBtn.addEventListener("click", () => resolveCritiqueFlag(flag.id));
          li.appendChild(dismissBtn);

          ul.appendChild(li);
        }
        flagsBody.appendChild(ul);
      }
    }
    section("Open critique findings", flagsBody);
    return;
  }

  if (id === "bibliography") {
    const notes = (b.research_notes || []).filter(n => (n.sources || []).length);
    if (!notes.length) {
      section("Sources", para("No cited research notes yet."));
      return;
    }
    const ul = document.createElement("ul");
    for (const n of notes.slice().sort((a, b2) => (a.topic || a.name || "").localeCompare(b2.topic || b2.name || ""))) {
      const li = document.createElement("li");
      const strong = document.createElement("strong");
      strong.textContent = n.topic || n.name;
      strong.title = "Open this research note";
      strong.style.cursor = "pointer";
      strong.addEventListener("click", () => selectItem("research_notes", n.name || n.topic));
      li.appendChild(strong);
      const srcList = document.createElement("ul");
      for (const s of n.sources || []) {
        const srcLi = document.createElement("li");
        const a = document.createElement("a");
        a.href = s.url;
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        a.textContent = s.title || s.url;
        srcLi.appendChild(a);
        srcList.appendChild(srcLi);
      }
      li.appendChild(srcList);
      ul.appendChild(li);
    }
    section("Bibliography", ul);
    return;
  }

  // progress
  const outline = b.outline || [];
  const chapters = b.chapters || [];
  const chapterMap = new Map(chapters.map(c => [c.chapter_num, c]));
  const total = outline.length;
  const approvedCount = outline.filter(o => (chapterMap.get(o.chapter_num) || {}).approved).length;
  const draftedCount = outline.filter(o => {
    const ch = chapterMap.get(o.chapter_num);
    return ch && ((ch.history || []).length || ch.draft);
  }).length;
  const totalWords = chapters.reduce((sum, c) => sum + (c.word_count || 0), 0);

  section("Summary", list([
    `Outline entries: ${total}`,
    `Drafted: ${draftedCount} / ${total || 0}`,
    `Finalized/approved: ${approvedCount} / ${total || 0}`,
    `Total word count: ${totalWords}${b.total_word_target ? ` / ${b.total_word_target} target` : ""}` +
      `${b.chapter_target_words ? ` (~${b.chapter_target_words} words/chapter)` : ""}`,
  ]));

  const rows = outline.map(o => {
    const ch = chapterMap.get(o.chapter_num);
    const badge = ch ? chapterStatusBadge(ch) : statusBadge({ approved: false, needsRecheck: false, historyLen: 0, hasOpenFlag: false });
    return {
      chapterNum: o.chapter_num,
      label: `${o.chapter_num}. ${o.title}${o.act ? " [" + o.act + "]" : ""}${ch && ch.word_count ? ` (${ch.word_count} words)` : ""}`,
      badge,
    };
  });
  if (rows.length) {
    const ul = document.createElement("ul");
    ul.className = "clickable-list";
    for (const row of rows) {
      const li = document.createElement("li");
      li.title = "Click to edit this chapter's outline entry";
      li.addEventListener("click", () => selectItem("outline", row.chapterNum));
      const label = document.createElement("span");
      label.textContent = `${row.label} — `;
      li.appendChild(label);
      const pill = document.createElement("span");
      pill.className = `canvas-card-status ${row.badge.cls}`;
      pill.textContent = row.badge.text;
      li.appendChild(pill);
      ul.appendChild(li);
    }
    section("Chapter-by-chapter status", ul);
  } else {
    section("Chapter-by-chapter status", para("No outline entries yet."));
  }
}

// -- new entity modal -----------------------------------------------------------
let entityModalKind = null;
// Sources ({title, url}) for a research note being drafted via the modal -
// carried outside the form fields since there's no visible sources input;
// populated when a queued task supplies an already-researched payload.
let entityModalSources = [];

// Mirrors realNoun() below but for the free-text "New Entity" modal, where
// there's no saved record yet - just whatever's in the category field.
function newEntityRealNoun(category) {
  if (category === "faction") return "faction/group";
  if (category === "object" || category === "item") return "object/item";
  return "place";
}

export function setNewEntityCategory(category) {
  $("ne-category").value = category || "";
  if (entityModalKind === "world") {
    $("ne-real-label").lastChild.textContent = ` This is a real ${newEntityRealNoun(category)} - keep it factually accurate`;
  }
}

export function openEntityModal(kind) {
  if (!state.slug) { setStatus("Select a project first.", true); return; }
  entityModalKind = kind;
  entityModalSources = [];
  $("entity-modal-title").textContent = { characters: "New Character", world: "New World Entry", research_notes: "New Note", timeline: "New Timeline Event" }[kind];
  $("ne-role-label").classList.toggle("hidden", kind !== "characters");
  $("ne-category-label").classList.toggle("hidden", kind !== "world");
  $("ne-real-label").classList.toggle("hidden", kind !== "world" && kind !== "characters");
  $("ne-real-label").lastChild.textContent =
    kind === "world" ? ` This is a real ${newEntityRealNoun("")} - keep it factually accurate` : " This is a real person - keep it factually accurate";
  // Category is free text - keep the "real X" wording in sync as the user
  // types instead of only matching whatever quickAdd() preset it to.
  $("ne-category").oninput = () => {
    if (kind === "world") $("ne-real-label").lastChild.textContent = ` This is a real ${newEntityRealNoun($("ne-category").value.trim().toLowerCase())} - keep it factually accurate`;
  };
  $("ne-story-date-label").classList.toggle("hidden", kind !== "timeline");
  $("ne-chapter-num-label").classList.toggle("hidden", kind !== "timeline");
  $("ne-characters-label").classList.toggle("hidden", kind !== "timeline");
  $("ne-locations-label").classList.toggle("hidden", kind !== "timeline");
  $("ne-suggest-row").classList.toggle("hidden", kind === "timeline");
  $("ne-consequence-row").classList.toggle("hidden", kind !== "timeline");
  $("ne-text-label").firstChild.textContent = "Content ";
  $("ne-freeform-label").firstChild.textContent = kind === "research_notes" ? "Topic to research (optional) " : "Describe it ";
  $("ne-freeform").value = "";
  $("ne-name").value = "";
  $("ne-role").value = "";
  $("ne-category").value = "";
  $("ne-real").checked = false;
  $("ne-story-date").value = "";
  $("ne-chapter-num").value = "";
  $("ne-characters").value = "";
  $("ne-locations").value = "";
  $("ne-text").value = "";
  $("ne-consequence-character").value = "";
  $("ne-consequence-status").value = "";
  if (kind === "timeline") {
    const dl = $("ne-consequence-character-options");
    dl.innerHTML = "";
    for (const c of state.bible?.characters || []) {
      const opt = document.createElement("option");
      opt.value = c.name;
      dl.appendChild(opt);
    }
  }
  $("entity-modal-backdrop").classList.remove("hidden");
}

export function closeEntityModal() { $("entity-modal-backdrop").classList.add("hidden"); }

export async function suggestEntity() {
  const kind = entityModalKind;
  const prompt = $("ne-freeform").value.trim();
  if (!prompt) {
    setStatus("Describe it first, or fill in the fields directly.", true);
    return;
  }
  setStatus(`Asking AI to draft a new ${kindLabel(kind)}...`);
  const draft = await api(`/api/projects/${encodeURIComponent(state.slug)}/entities/${kind}/suggest`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt }),
  });
  $("ne-name").value = draft.name || "";
  if (kind === "characters") $("ne-role").value = draft.role || "";
  if (kind === "world") setNewEntityCategory(draft.category || "");
  $("ne-text").value = (kind === "characters" ? draft.description : draft.content) || "";
  setStatus("Draft ready - review and edit before creating.");
}

export async function createEntity() {
  const name = $("ne-name").value.trim();
  const text = $("ne-text").value.trim();
  if (!name) {
    setStatus("Name is required.", true);
    return;
  }
  const kind = entityModalKind;
  const body = kind === "characters"
    ? { name, role: $("ne-role").value.trim() || "supporting", description: text, is_real: $("ne-real").checked }
    : kind === "world"
    ? { name, category: $("ne-category").value.trim() || "general", content: text, is_real: $("ne-real").checked }
    : kind === "timeline"
    ? {
        name, story_date: $("ne-story-date").value.trim(), description: text,
        chapter_num: $("ne-chapter-num").value.trim() ? parseInt($("ne-chapter-num").value, 10) : null,
        characters: $("ne-characters").value.split(",").map(s => s.trim()).filter(Boolean),
        locations: $("ne-locations").value.split(",").map(s => s.trim()).filter(Boolean),
        consequence: $("ne-consequence-character").value.trim() && $("ne-consequence-status").value.trim()
          ? { character: $("ne-consequence-character").value.trim(), status: $("ne-consequence-status").value.trim() }
          : null,
      }
    : kind === "research_notes"
    ? { name, content: text, sources: entityModalSources }
    : { name, content: text };
  closeEntityModal();
  setStatus("Creating...");
  await api(`/api/projects/${encodeURIComponent(state.slug)}/entities/${kind}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  await refreshBible();
  renderSidebar();
  selectItem(kind, name);
  markTouched(kind, name, `${kindLabel(kind)}: ${name} (new)`);
  setStatus("Created.");
}

// -- legacy World cleanup -------------------------------------------------------
export async function migrateImportNotes() {
  if (!state.slug) return;
  setStatus("Moving legacy import notes out of World...");
  const result = await api(`/api/projects/${encodeURIComponent(state.slug)}/world/migrate-import-notes`, { method: "POST" });
  await refreshBible();
  renderSidebar();
  if (state.selection) renderEditor();
  setStatus(result.moved ? `Moved ${result.moved} entr${result.moved === 1 ? "y" : "ies"} into Notes.` : "Nothing to clean up.");
}

// -- local LLM server status/start/stop -----------------------------------------
let llmStarting = false;

export async function pollLlmStatus() {
  const indicator = $("llm-indicator");
  const railDot = $("rail-llm-dot");
  const startBtn = $("btn-start-llm");
  const stopBtn = $("btn-stop-llm");
  try {
    const status = await api("/api/llm/status", undefined, { silent: true });
    if (status.crashed) {
      llmStarting = false;
      indicator.className = "llm-down";
      indicator.textContent = "LLM: failed to start";
      if (railDot) railDot.className = "rail-llm-dot off";
      startBtn.classList.remove("hidden");
      stopBtn.classList.add("hidden");
      setStatus(status.crashed, true);
    } else if (status.up) {
      llmStarting = false;
      indicator.className = "llm-up";
      indicator.textContent = "LLM: running";
      if (railDot) railDot.className = "rail-llm-dot";
      startBtn.classList.add("hidden");
      stopBtn.classList.remove("hidden");
    } else if (llmStarting || status.started_by_ui) {
      indicator.className = "llm-starting";
      indicator.textContent = "LLM: starting...";
      if (railDot) railDot.className = "rail-llm-dot starting";
      startBtn.classList.add("hidden");
      stopBtn.classList.add("hidden");
    } else {
      indicator.className = "llm-down";
      indicator.textContent = "LLM: not running";
      if (railDot) railDot.className = "rail-llm-dot off";
      startBtn.classList.remove("hidden");
      stopBtn.classList.add("hidden");
    }
  } catch (_) {
    indicator.className = "llm-down";
    indicator.textContent = "LLM: unknown";
    if (railDot) railDot.className = "rail-llm-dot off";
  }
}

export async function startLlm() {
  llmStarting = true;
  $("btn-start-llm").classList.add("hidden");
  try {
    await api("/api/llm/start", { method: "POST" });
  } catch (err) {
    llmStarting = false;
    setStatus(err.message, true);
  }
  pollLlmStatus();
}

export async function stopLlm() {
  $("btn-stop-llm").classList.add("hidden");
  setStatus("Stopping LLM server...");
  try {
    await api("/api/llm/stop", { method: "POST" });
    setStatus("LLM server stopped.");
  } catch (err) {
    setStatus(err.message, true);
  }
  pollLlmStatus();
}

// Local model picker: lists every .gguf next to the configured model (plus
// the repo's own models/ drop-in folder) so the writer can swap between
// locally downloaded models (e.g. Gemma vs. a newly added Qwen build)
// without hand-editing config.yaml.
export async function loadLlmModels() {
  const sel = $("llm-model");
  const { models } = await api("/api/llm/models", undefined, { silent: true });
  sel.innerHTML = "";
  for (const m of models) {
    const opt = document.createElement("option");
    opt.value = m.path;
    opt.textContent = `${m.name} (${m.size_gb} GB)`;
    if (m.current) opt.selected = true;
    sel.appendChild(opt);
  }
}

export async function switchModel() {
  const path = $("llm-model").value;
  if (!path) return;
  setStatus("Switching model...");
  try {
    const result = await api("/api/llm/model", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
    setStatus(result.cleared_draft
      ? "Model switched. Cleared the speculative-decoding draft model (built for a different model family)."
      : "Model switched.");
    await loadLlmModels();
  } catch (err) {
    setStatus(err.message, true);
  }
  pollLlmStatus();
}

// Lets the writer point at an external llama.cpp build folder and/or an
// extra models folder from Settings, instead of hand-editing config.yaml.
export async function loadLlmFolders() {
  const { llama_cpp_dir, models_dir } = await api("/api/llm/folders", undefined, { silent: true });
  $("llm-cpp-folder").value = llama_cpp_dir || "";
  $("llm-models-folder").value = models_dir || "";
}

export async function saveLlmFolders() {
  const llama_cpp_dir = $("llm-cpp-folder").value.trim();
  const models_dir = $("llm-models-folder").value.trim();
  setStatus("Saving folders...");
  try {
    await api("/api/llm/folders", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ llama_cpp_dir, models_dir }),
    });
    setStatus("Folders saved.");
    await loadLlmModels();
  } catch (err) {
    setStatus(err.message, true);
  }
}

function describeUniversalTask(task) {
  switch (task.action) {
    case "create_character": return "add a new character";
    case "create_world": return "add a new world entry";
    case "create_note": return "add a new note";
    case "create_timeline_event": return "add a new timeline event";
    case "create_outline_entry": return `add outline chapter ${task.next_chapter_num}`;
    case "revise_outline_whole": return "revise the whole outline";
    case "revise_engine": return "revise the story engine";
    case "revise_character": return `revise character "${task.target_name}"`;
    case "resync_character_sections": return `resync "${task.target_name}"'s character sections`;
    case "revise_world": return `revise world entry "${task.target_name}"`;
    case "revise_note": return `revise note "${task.target_name}"`;
    case "revise_timeline_event": return `revise timeline event "${task.target_name}"`;
    case "revise_chapter": return `revise chapter ${task.chapter_num}`;
    case "revise_outline_entry": return `update outline chapter ${task.chapter_num}`;
    default: return task.action;
  }
}

export function updateUniversalQueueBar() {
  const bar = $("universal-queue-bar");
  const remaining = state.universalQueue.length - state.universalQueueIndex - 1;
  if (remaining <= 0) {
    bar.classList.add("hidden");
    state.universalQueue = [];
    state.universalQueueIndex = 0;
    persistTasks();
    return;
  }
  bar.classList.remove("hidden");
  const next = state.universalQueue[state.universalQueueIndex + 1];
  $("universal-queue-status").textContent =
    `${remaining} more queued task${remaining === 1 ? "" : "s"} - review/save this one, then click Next. Next up: ${describeUniversalTask(next)}.`;
  persistTasks();
}

async function dispatchUniversalTask(task) {
  const instruction = task.instruction;
  if (task.action === "create_character" || task.action === "create_world" || task.action === "create_note") {
    const kind = task.action === "create_character" ? "characters" : task.action === "create_world" ? "world" : "research_notes";
    openEntityModal(kind);
    if (task.payload) {
      // Already researched (e.g. auto-detected during a non-fiction draft) -
      // fill the modal directly instead of re-running research from scratch.
      $("ne-name").value = task.payload.name || "";
      $("ne-text").value = task.payload.content || "";
      entityModalSources = task.payload.sources || [];
      setStatus("Draft ready - review and edit before creating.");
      return;
    }
    $("ne-freeform").value = instruction;
    await suggestEntity();
    return;
  }
  if (task.action === "create_timeline_event") {
    openEntityModal("timeline");
    if (task.payload) {
      // Already extracted (e.g. from a chapter finalize) - fill the modal
      // directly instead of asking the writer to describe it from scratch.
      $("ne-name").value = task.payload.name || "";
      $("ne-story-date").value = task.payload.story_date || "";
      $("ne-chapter-num").value = task.payload.chapter_num || "";
      $("ne-text").value = task.payload.description || "";
      $("ne-characters").value = (task.payload.characters || []).join(", ");
      $("ne-locations").value = (task.payload.locations || []).join(", ");
      setStatus("Draft ready - review and edit before creating.");
      return;
    }
    $("ne-text").value = instruction;
    return;
  }
  if (task.action === "create_outline_entry") {
    openOutlineModal();
    $("oo-chapter-num").value = task.next_chapter_num;
    $("oo-freeform").value = instruction;
    await suggestOutlineEntry();
    return;
  }
  if (task.action === "revise_outline_whole") {
    openReviseOutlineModal();
    $("ro-instruction").value = instruction;
    await draftOutlineRevision();
    return;
  }
  if (task.action === "revise_engine") {
    await selectItem("overview", "engine");
    $("ov-instruction").value = instruction;
    await reviseEngine();
    return;
  }
  if (task.action === "revise_character" || task.action === "revise_world" || task.action === "revise_note" || task.action === "revise_timeline_event") {
    const kind = task.action === "revise_character" ? "characters" : task.action === "revise_world" ? "world" : task.action === "revise_note" ? "research_notes" : "timeline";
    await selectItem(kind, task.target_name);
    $("instruction").value = instruction;
    await reviseWithInstruction();
    return;
  }
  if (task.action === "resync_character_sections") {
    await selectItem("characters", task.target_name);
    await resyncCharacterSections(task.target_name, instruction);
    return;
  }
  if (task.action === "revise_chapter") {
    await selectItem("chapter", task.chapter_num);
    $("instruction").value = instruction;
    await reviseWithInstruction();
    return;
  }
  if (task.action === "revise_outline_entry") {
    await selectItem("outline", task.chapter_num);
    await regenerateOutlineEntry(task.chapter_num, instruction);
    return;
  }
}

// -- universal prompt: "Discuss" mode ---------------------------------------
// A separate conversational path alongside the classify+dispatch one above:
// the writer thinks an idea through with the AI, turn by turn, and nothing
// gets drafted until a suggestion is explicitly turned into a task (which
// re-enters the exact same runUniversalPrompt path - never dispatches on
// its own).
export function renderDiscussLog() {
  const log = $("universal-discuss-log");
  const suggestionBar = $("universal-discuss-suggestion");
  const clearBtn = $("btn-universal-discuss-clear");
  const history = state.discussHistory || [];
  log.classList.toggle("hidden", history.length === 0);
  clearBtn.classList.toggle("hidden", history.length === 0);
  log.innerHTML = "";
  for (const turn of history) {
    const div = document.createElement("div");
    div.className = `universal-discuss-turn universal-discuss-${turn.role}`;
    div.textContent = turn.text;
    log.appendChild(div);
  }
  log.scrollTop = log.scrollHeight;

  const last = history[history.length - 1];
  const suggestion = last && last.role === "assistant" ? last.suggestedPrompt : null;
  suggestionBar.classList.toggle("hidden", !suggestion);
  if (suggestion) $("universal-discuss-suggestion-text").textContent = suggestion;
}

export function clearUniversalDiscuss() {
  state.discussHistory = [];
  renderDiscussLog();
}

export function useDiscussSuggestion() {
  const history = state.discussHistory || [];
  const last = history[history.length - 1];
  if (!last || !last.suggestedPrompt) return;
  setUniversalMode("do");
  $("universal-prompt").value = last.suggestedPrompt;
  $("universal-prompt").focus();
}

export function setUniversalMode(mode) {
  state.universalMode = mode;
  $("btn-universal-mode-do").classList.toggle("active", mode === "do");
  $("btn-universal-mode-discuss").classList.toggle("active", mode === "discuss");
  $("btn-universal-go").textContent = mode === "discuss" ? "Discuss" : "Go";
  $("universal-prompt").placeholder = mode === "discuss"
    ? "Think out loud about the book - ask a question, weigh an idea. Nothing gets drafted until you say so."
    : "Type anything - one instruction or several run together, e.g. 'add a new character named... also make the tone darker... and in chapter 3, slow down the ending' - and it'll figure out where each part goes.";
  renderDiscussLog();
}

export async function runUniversalDiscuss() {
  if (!state.slug) { setStatus("Select a project first.", true); return; }
  const text = $("universal-prompt").value.trim();
  if (!text) { setStatus("Type something first.", true); return; }

  state.discussHistory = [...(state.discussHistory || []), { role: "writer", text }];
  $("universal-prompt").value = "";
  renderDiscussLog();
  setStatus("Thinking it through...");

  try {
    const result = await api(`/api/projects/${encodeURIComponent(state.slug)}/universal-discuss`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ history: state.discussHistory.map(({ role, text }) => ({ role, text })) }),
    });
    state.discussHistory = [
      ...state.discussHistory,
      { role: "assistant", text: result.reply, suggestedPrompt: result.suggested_prompt || null },
    ];
    renderDiscussLog();
    setStatus(result.suggested_prompt ? "Got a suggestion - review it below, or keep talking." : "Ready when you are.");
  } catch (err) {
    setStatus(err.message, true);
  }
}

export async function runUniversalPrompt() {
  if (state.universalMode === "discuss") { await runUniversalDiscuss(); return; }
  if (!state.slug) { setStatus("Select a project first.", true); return; }
  const prompt = $("universal-prompt").value.trim();
  if (!prompt) { setStatus("Type an instruction first.", true); return; }

  setStatus("Figuring out where this goes...");
  const result = await api(`/api/projects/${encodeURIComponent(state.slug)}/universal-prompt`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt }),
  });

  const allTasks = result.tasks || [];
  const tasks = allTasks.filter((t) => t.action !== "unclear");
  const unclearCount = allTasks.length - tasks.length;
  const questions = allTasks
    .filter((t) => t.action === "unclear" && t.question)
    .map((t) => t.question);

  if (!tasks.length) {
    // Leave the prompt text in the box so the writer can answer the
    // question by editing/appending to what they typed and hitting Go again.
    setStatus(
      questions.length
        ? `Quick question before I can route that: ${questions.join(" Also: ")} (edit your prompt above with the answer and hit Go again)`
        : "Couldn't tell what that should change - try naming an existing character/world entry/note, a chapter number, or a section (outline, story engine).",
      !questions.length
    );
    return;
  }

  $("universal-prompt").value = "";
  state.universalQueue = tasks;
  state.universalQueueIndex = 0;

  const skippedNote = unclearCount
    ? (questions.length
        ? ` (skipped ${unclearCount} unclear part${unclearCount === 1 ? "" : "s"} - ${questions.join(" Also: ")})`
        : ` (${unclearCount} part${unclearCount === 1 ? "" : "s"} of that wasn't clear enough to act on and was skipped)`)
    : "";
  setStatus(
    tasks.length > 1
      ? `Found ${tasks.length} tasks - starting with 1 of ${tasks.length}: ${describeUniversalTask(tasks[0])}.${skippedNote}`
      : `Working on it.${skippedNote}`
  );

  try {
    await dispatchUniversalTask(tasks[0]);
  } finally {
    // Even if the task errors, the queue bar must reflect the remaining
    // tasks - otherwise they exist in state with no way to advance or skip.
    updateUniversalQueueBar();
  }
}

export async function advanceUniversalQueue() {
  state.universalQueueIndex += 1;
  const task = state.universalQueue[state.universalQueueIndex];
  if (!task) { updateUniversalQueueBar(); return; }
  setStatus(`Task ${state.universalQueueIndex + 1} of ${state.universalQueue.length}: ${describeUniversalTask(task)}...`);
  try {
    await dispatchUniversalTask(task);
  } finally {
    updateUniversalQueueBar();
  }
}

function buildChangeSummaryFromTouched() {
  const lines = [];
  for (const t of state.touchedSections || []) {
    if (t.kind === "characters" || t.kind === "world" || t.kind === "research_notes") {
      const e = getEntity(t.kind, t.id);
      if (e) lines.push(`${kindLabel(t.kind)} "${t.id}": ${e.description || e.content || ""}`);
    } else if (t.kind === "chapter") {
      const ch = getChapter(t.id);
      lines.push(`Chapter ${t.id} was revised${ch && ch.summary ? ": " + ch.summary : ""}`);
    } else if (t.kind === "outline") {
      const o = getOutlineEntry(t.id);
      if (o) lines.push(`Outline Chapter ${t.id} ("${o.title || ""}"): ${o.summary || ""}`);
    } else if (t.kind === "overview" && t.id === "engine") {
      const b = state.bible;
      lines.push(`Story engine changed: premise=${b.premise || ""}; tone=${b.tone || ""}; narrative_voice=${b.narrative_voice || ""}; narrative_engine=${b.narrative_engine || ""}; themes=${b.themes || ""}`);
    } else if (t.kind === "overview" && t.id === "progress") {
      lines.push("The whole outline was revised.");
    }
  }
  return lines.join("\n");
}

// Adds new tasks to the universal queue without discarding whatever the
// writer hasn't gotten to yet - replaces the queue with [still-pending
// tasks, new tasks] and points the index at the first of that combined
// list, rather than clobbering pending work the way a plain overwrite would.
function enqueueUniversalTasks(newTasks) {
  if (!newTasks || !newTasks.length) return false;
  const remaining = state.universalQueue.slice(state.universalQueueIndex + 1);
  state.universalQueue = [...remaining, ...newTasks];
  state.universalQueueIndex = 0;
  return true;
}

// Converts the timeline extractor's proposed new events (from a chapter
// finalize job) into create_timeline_event tasks carrying the already-
// extracted {name, story_date, description, characters} as payload, so
// dispatchUniversalTask can pre-fill the entity modal directly instead of
// asking the writer to describe an event the AI already found.
function timelineProposalTasks(proposals, chapterNum) {
  if (!proposals || !proposals.length) return [];
  return proposals.map((p) => ({
    action: "create_timeline_event",
    instruction: p.description,
    target_name: null,
    chapter_num: null,
    next_chapter_num: null,
    payload: {
      name: p.name, story_date: p.story_date || "", description: p.description,
      chapter_num: p.chapter_num || chapterNum, characters: p.characters || [],
      locations: p.locations || [],
    },
  }));
}

// Converts the bible manager's unapplied character/faction/world proposals
// (from a chapter finalize job), plus any timeline-extractor proposals, into
// the same universal-task-queue shape used elsewhere, so each one goes
// through a normal revise-and-approve (for an existing entry) or draft-and-
// create (for a brand-new one) step instead of writing to the bible
// unreviewed.
function queueBibleProposals(proposals, chapterNum, timelineProposals) {
  if ((!proposals || !proposals.length) && (!timelineProposals || !timelineProposals.length)) return false;
  const tasks = (proposals || []).flatMap((p) => {
    const instruction = p.exists
      ? `Add this newly established fact from Chapter ${chapterNum}: ${p.new_facts}`
      : p.bucket === "character"
      ? `${p.name}, role: ${p.role_or_category}. ${p.new_facts}`
      : `${p.name} (category: ${p.role_or_category}). ${p.new_facts}`;
    const action = p.exists
      ? (p.bucket === "character" ? "revise_character" : "revise_world")
      : (p.bucket === "character" ? "create_character" : "create_world");
    const task = {
      action,
      instruction,
      target_name: p.target_name || null,
      chapter_num: null,
      next_chapter_num: null,
    };
    // An existing character's own structured sections (appearance,
    // relationships, etc.) aren't touched by the description revise above -
    // queue a second, separate review step so a fact like "got a scar" or
    // "reconciled with her sister" doesn't go stale in Relationships/
    // Appearance forever just because the section was already filled in.
    if (p.exists && p.bucket === "character") {
      return [task, {
        action: "resync_character_sections",
        instruction: p.new_facts,
        target_name: p.target_name,
        chapter_num: null,
        next_chapter_num: null,
      }];
    }
    return [task];
  });
  return enqueueUniversalTasks([...tasks, ...timelineProposalTasks(timelineProposals, chapterNum)]);
}

// Converts auto-detected research proposals (from a non-fiction chapter
// draft job) into create_note tasks carrying the already-researched
// {name, content, sources} as payload, so dispatchUniversalTask can pre-fill
// the entity modal directly instead of re-running research from scratch.
function queueResearchProposals(proposals) {
  if (!proposals || !proposals.length) return false;
  const tasks = proposals.map((p) => ({
    action: "create_note",
    instruction: p.topic,
    target_name: null,
    chapter_num: null,
    next_chapter_num: null,
    payload: { name: p.topic, content: p.content, sources: p.sources || [] },
  }));
  return enqueueUniversalTasks(tasks);
}

// Shared by the just-ran-a-check queue (below) and the persisted Continuity
// dashboard's per-flag "Fix" button - same flag shape, same target action.
function continuityFlagToTask(f) {
  return {
    action: f.kind === "character" ? "revise_character"
      : f.kind === "world" ? "revise_world"
      : f.kind === "note" ? "revise_note"
      : f.kind === "timeline" ? "revise_timeline_event"
      : f.drafted ? "revise_chapter" : "revise_outline_entry",
    instruction: f.instruction,
    target_name: f.target_name || null,
    chapter_num: f.chapter_num,
    next_chapter_num: null,
  };
}

async function resolveContinuityFlag(flagId) {
  await api(`/api/projects/${encodeURIComponent(state.slug)}/continuity-flags/${flagId}/resolve`, { method: "POST" });
  state.bible.continuity_flags = (state.bible.continuity_flags || []).filter((f) => f.id !== flagId);
  renderOverview("continuity");
}

async function resolveCritiqueFlag(flagId) {
  await api(`/api/projects/${encodeURIComponent(state.slug)}/critique-flags/${flagId}/resolve`, { method: "POST" });
  state.bible.critique_flags = (state.bible.critique_flags || []).filter((f) => f.id !== flagId);
  renderOverview("critique");
}

export async function checkConsistency() {
  if (!state.slug || !(state.touchedSections || []).length) return;
  const change_summary = buildChangeSummaryFromTouched();
  setStatus("Checking consistency against the outline and drafted chapters...");
  const result = await api(`/api/projects/${encodeURIComponent(state.slug)}/consistency-check`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ change_summary }),
  });
  const flags = result.flags || [];
  // The backend already persisted these onto the bible (continuity_flags) so
  // they show up in the Continuity dashboard beyond this session - pull that
  // in locally too rather than a full refetch.
  await refreshBible();
  if (!flags.length) {
    setStatus("No inconsistencies found.");
    return;
  }
  const tasks = flags.map(continuityFlagToTask);
  state.universalQueue = tasks;
  state.universalQueueIndex = 0;
  const firstLabel = flags[0].target_name
    ? `${{ world: "world entry", note: "note" }[flags[0].kind] || "character"} "${flags[0].target_name}"`
    : `chapter ${flags[0].chapter_num}`;
  setStatus(`Found ${tasks.length} affected item${tasks.length === 1 ? "" : "s"} - starting with ${firstLabel}: ${flags[0].issue}`);
  try {
    await dispatchUniversalTask(tasks[0]);
  } finally {
    updateUniversalQueueBar();
  }
}

export function skipRemainingUniversalTasks() {
  state.universalQueue = [];
  state.universalQueueIndex = 0;
  $("universal-queue-bar").classList.add("hidden");
  persistTasks();
  setStatus("Remaining queued tasks skipped.");
}

let promptsCache = [];

function promptOptionLabel(p) {
  return p.label + (p.overridden ? " •" : "");
}

export async function loadPrompts() {
  promptsCache = await api("/api/prompts");
  const sel = $("pr-agent");
  const keep = sel.value;
  sel.innerHTML = "";
  for (const p of promptsCache) {
    const opt = document.createElement("option");
    opt.value = p.key;
    opt.textContent = promptOptionLabel(p);
    sel.appendChild(opt);
  }
  if (keep && promptsCache.some(p => p.key === keep)) sel.value = keep;
  showPromptFor(sel.value);
}

export function showPromptFor(key) {
  const p = promptsCache.find(x => x.key === key);
  $("pr-text").value = p ? p.current : "";
}

export async function savePrompt(reset) {
  const key = $("pr-agent").value;
  if (!key) return;
  const body = { system_prompt: reset ? null : $("pr-text").value };
  const updated = await api(`/api/prompts/${encodeURIComponent(key)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const idx = promptsCache.findIndex(p => p.key === key);
  if (idx !== -1) promptsCache[idx] = updated;
  const opt = [...$("pr-agent").options].find(o => o.value === key);
  if (opt) opt.textContent = promptOptionLabel(updated);
  $("pr-text").value = updated.current;
  setStatus(reset ? "Prompt reset to default." : (updated.overridden ? "Prompt saved." : "Prompt matches the default - no override kept."));
}
