// Command palette (Ctrl/Cmd+K): fuzzy jump-to-anything across the open
// project's chapters/characters/factions/world/notes/ideas, plus a fixed
// list of actions (new X, export, check consistency, open a tab). Purely
// client-side filtering, no LLM call - deliberately NOT the same thing as
// the "/" universal prompt bar, which is the conversational/freeform-
// instruction tool. Kept as its own module (not folded into sidebar.js)
// since it reaches across every section rather than rendering one tree.
import { $, state } from "./api.js";
import {
  checkConsistency, critiqueBook, openEntityModal, openIdeaModal, openOutlineModal, selectItem,
} from "./editor.js";
import { selectChapter } from "./manuscript.js";
import { exportEpub, exportManuscript, exportPdf } from "./projects.js";
import { setActiveTab } from "./tabs.js";

let items = [];
let activeIndex = -1;
// Built once per palette open (allItems() walks the whole outline/characters/
// world/notes/ideas list) and reused for every keystroke's filter instead of
// rebuilding it on each one - see openPalette/render.
let cachedItems = null;

function jumpItems() {
  if (!state.slug || !state.bible) return [];
  const out = [];
  for (const e of (state.bible.outline || []).slice().sort((a, b) => a.chapter_num - b.chapter_num)) {
    out.push({
      group: "Chapters", label: `${e.chapter_num}. ${e.title || "Untitled"}`,
      run: () => { setActiveTab("manuscript"); selectChapter(e.chapter_num); },
    });
  }
  for (const c of state.bible.characters || []) {
    out.push({ group: "Characters", label: c.name, run: () => { setActiveTab("manuscript"); selectItem("characters", c.name); } });
  }
  for (const w of state.bible.world || []) {
    const group = w.category === "faction" ? "Factions" : "World";
    out.push({ group, label: w.name, run: () => { setActiveTab("manuscript"); selectItem("world", w.name); } });
  }
  for (const n of state.bible.research_notes || []) {
    const name = n.name || n.topic;
    out.push({ group: "Research notes", label: name, run: () => { setActiveTab("manuscript"); selectItem("research_notes", name); } });
  }
  for (const idea of state.bible.ideas || []) {
    out.push({ group: "Ideas", label: idea.title || idea.name, run: () => { setActiveTab("manuscript"); openIdeaModal(idea); } });
  }
  return out;
}

function actionItems() {
  if (!state.slug) return [];
  return [
    { group: "New", label: "New Chapter (outline entry)", run: () => openOutlineModal() },
    { group: "New", label: "New Character", run: () => openEntityModal("characters") },
    { group: "New", label: "New Faction", run: () => { openEntityModal("world"); $("entity-modal-title").textContent = "New Faction"; $("ne-category").value = "faction"; } },
    { group: "New", label: "New World Entry", run: () => openEntityModal("world") },
    { group: "New", label: "New Research Note", run: () => openEntityModal("research_notes") },
    { group: "New", label: "New Idea", run: () => openIdeaModal() },
    { group: "Do", label: "Check consistency", run: () => checkConsistency() },
    { group: "Do", label: "Critique Whole Book", run: () => critiqueBook() },
    { group: "Do", label: "Export Manuscript (Markdown)", run: () => exportManuscript() },
    { group: "Do", label: "Export EPUB", run: () => exportEpub() },
    { group: "Do", label: "Export PDF", run: () => exportPdf() },
    { group: "Go", label: "Open Story Engine (Plan)", run: () => setActiveTab("plan") },
    { group: "Go", label: "Open Settings", run: () => setActiveTab("settings") },
    { group: "Go", label: "Ask AI...", run: () => document.dispatchEvent(new CustomEvent("open-universal-bar")) },
  ];
}

function allItems() {
  return [...actionItems(), ...jumpItems()];
}

function matches(item, query) {
  return `${item.group} ${item.label}`.toLowerCase().includes(query);
}

function render(query) {
  const results = $("palette-results");
  results.innerHTML = "";
  const q = query.trim().toLowerCase();
  const pool = q ? cachedItems.filter(it => matches(it, q)) : cachedItems;
  items = pool.slice(0, 60);
  activeIndex = items.length ? 0 : -1;

  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "palette-empty";
    empty.textContent = state.slug ? "No matches." : "Select a project first.";
    results.appendChild(empty);
    return;
  }

  let lastGroup = null;
  items.forEach((item, i) => {
    if (item.group !== lastGroup) {
      lastGroup = item.group;
      const label = document.createElement("div");
      label.className = "palette-group-label";
      label.textContent = item.group;
      results.appendChild(label);
    }
    const btn = document.createElement("button");
    btn.className = `palette-item${i === activeIndex ? " active" : ""}`;
    btn.textContent = item.label;
    btn.addEventListener("mouseenter", () => setActive(i));
    btn.addEventListener("click", () => pick(i));
    results.appendChild(btn);
  });
}

function setActive(i) {
  activeIndex = i;
  for (const [idx, el] of [...$("palette-results").querySelectorAll(".palette-item")].entries()) {
    el.classList.toggle("active", idx === activeIndex);
  }
  const el = $("palette-results").querySelectorAll(".palette-item")[activeIndex];
  if (el) el.scrollIntoView({ block: "nearest" });
}

function pick(i) {
  const item = items[i];
  if (!item) return;
  closePalette();
  item.run();
}

export function openPalette() {
  $("palette-backdrop").classList.remove("hidden");
  const input = $("palette-input");
  input.value = "";
  input.focus();
  cachedItems = allItems();
  render("");
}

export function closePalette() {
  $("palette-backdrop").classList.add("hidden");
}

export function togglePalette() {
  if ($("palette-backdrop").classList.contains("hidden")) openPalette();
  else closePalette();
}

$("palette-input").addEventListener("input", (e) => render(e.target.value));
$("palette-backdrop").addEventListener("click", (e) => {
  if (e.target === $("palette-backdrop")) closePalette();
});
$("palette-input").addEventListener("keydown", (e) => {
  if (e.key === "Escape") { e.preventDefault(); closePalette(); return; }
  if (e.key === "ArrowDown") { e.preventDefault(); if (activeIndex < items.length - 1) setActive(activeIndex + 1); return; }
  if (e.key === "ArrowUp") { e.preventDefault(); if (activeIndex > 0) setActive(activeIndex - 1); return; }
  if (e.key === "Enter") { e.preventDefault(); pick(activeIndex); return; }
});
