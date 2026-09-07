// Left tree sidebar (Manuscript + Bible tabs): Outline / Characters /
// Factions / Locations / Objects-Items / Misc / Research notes.
// Factions, Locations, Objects/Items, and Misc are all client-side
// filters over the single World entity list (by category) - no new backend
// concept, no new bucket. toEntityKind() maps each of those branch keys back
// to "world" wherever the actual entity kind is needed (API calls, lookups).
// Quickadd reuses the existing entity/outline modals; rename/delete call the
// existing entity-rename/delete and outline-delete routes directly, since
// editor.js's own renameEntity()/deleteEntity() are wired to drawer state
// (state.selection + drawer inputs) rather than being callable standalone.
import { $, api, kindLabel, markTouched, setStatus, state, uiConfirm, uiPrompt } from "./api.js";
import { chapterStatusBadge, checkConsistencyForItem, closeDrawer, entityStatusBadge, getChapter, ideaStatusBadge, linkedIdeasFor, openEntityModal, openIdeaModal, openIntegrateIdeaModal, openOutlineModal, openPromoteIdeaModal, renderEditor, selectItem, setNewEntityCategory } from "./editor.js";
import { selectChapter } from "./manuscript.js";
import { refreshBible } from "./projects.js";
import { setActiveTab } from "./tabs.js";

// Ideas + Outline + Timeline live in the left sidebar (#tree-sidebar);
// every other entity bucket lives in the right sidebar (#entity-sidebar).
// Timeline has no ribbon/swimlane UI (deferred - see memory), so it's just
// another entity branch, grouped left alongside the story-progression views.
const LEFT_BRANCHES = [
  { key: "ideas", label: "Ideas" },
  { key: "outline", label: "Outline" },
  { key: "timeline", label: "Timeline" },
];
const RIGHT_BRANCHES = [
  { key: "characters", label: "Characters" },
  { key: "faction", label: "Factions" },
  { key: "location", label: "Locations" },
  { key: "object", label: "Objects/Items" },
  { key: "world", label: "Misc" },
  { key: "research_notes", label: "Research notes" },
];

// Branch keys that are really the "world" bucket filtered by category.
const WORLD_BRANCHES = new Set(["faction", "location", "object", "world"]);
function toEntityKind(branchKey) {
  return WORLD_BRANCHES.has(branchKey) ? "world" : branchKey;
}

const collapsed = new Set();
const collapsedActs = new Set();
let timelineTrackFilter = ""; // "" = all tracks; "" also matches events with no track set

export function showSidebar() {
  $("tree-sidebar").classList.remove("hidden");
  $("entity-sidebar").classList.remove("hidden");
  renderSidebar();
}

export function hideSidebar() {
  $("tree-sidebar").classList.add("hidden");
  $("entity-sidebar").classList.add("hidden");
}

function leavesFor(branchKey) {
  if (branchKey === "ideas") {
    return (state.bible.ideas || []).slice().sort((a, b) => a.id - b.id)
      .map(e => ({ id: e.id, name: e.title, record: e }));
  }
  if (branchKey === "outline") {
    return (state.bible.outline || []).slice().sort((a, b) => a.chapter_num - b.chapter_num)
      .map(e => ({ id: e.chapter_num, name: `${e.chapter_num}. ${e.title || "Untitled"}`, act: e.act || "Unassigned act", record: e }));
  }
  if (branchKey === "faction") {
    return (state.bible.world || []).filter(e => e.category === "faction")
      .map(e => ({ id: e.name, name: e.name, record: e }));
  }
  if (branchKey === "location") {
    return (state.bible.world || []).filter(e => e.category === "location")
      .map(e => ({ id: e.name, name: e.name, record: e }));
  }
  if (branchKey === "object") {
    return (state.bible.world || []).filter(e => e.category === "object" || e.category === "item")
      .map(e => ({ id: e.name, name: e.name, record: e }));
  }
  if (branchKey === "world") {
    return (state.bible.world || []).filter(e => !["faction", "location", "object", "item"].includes(e.category))
      .map(e => ({ id: e.name, name: e.name, record: e }));
  }
  if (branchKey === "timeline") {
    // Sort by the event's place in the book (chapter_num), not creation
    // order - chapters are often drafted out of sequence, so an event from
    // a later chapter drafted first must not appear before an earlier
    // chapter's event drafted afterward. Undated/no-chapter events sort
    // first; "order" only breaks ties within the same chapter.
    return (state.bible.timeline || [])
      .filter(e => !timelineTrackFilter || (e.track_id || "") === timelineTrackFilter)
      .slice().sort((a, b) => {
      const ca = a.chapter_num ?? -1, cb = b.chapter_num ?? -1;
      return ca !== cb ? ca - cb : (a.order || 0) - (b.order || 0);
    }).map(e => {
      const chronoBadge = e.chrono_order != null ? `⧗${e.chrono_order} ` : "";
      const label = e.story_date ? `${e.story_date}: ${e.name}` : e.name;
      return { id: e.name, name: `${chronoBadge}${label}`, record: e };
    });
  }
  return (state.bible[branchKey] || []).map(e => ({ id: e.name || e.topic, name: e.name || e.topic, record: e }));
}

function leafStatusBadge(branchKey, leaf) {
  if (branchKey === "ideas") return ideaStatusBadge(leaf.record);
  if (branchKey === "outline") {
    const ch = getChapter(leaf.id);
    return ch ? chapterStatusBadge(ch) : null;
  }
  return entityStatusBadge(leaf.record);
}

function quickAdd(branchKey) {
  if (branchKey === "ideas") { openIdeaModal(); return; }
  if (branchKey === "outline") { openOutlineModal(); return; }
  if (branchKey === "faction") {
    openEntityModal("world");
    $("entity-modal-title").textContent = "New Faction";
    setNewEntityCategory("faction");
    return;
  }
  if (branchKey === "location") {
    openEntityModal("world");
    $("entity-modal-title").textContent = "New Location";
    setNewEntityCategory("location");
    return;
  }
  if (branchKey === "object") {
    openEntityModal("world");
    $("entity-modal-title").textContent = "New Object/Item";
    setNewEntityCategory("object");
    return;
  }
  if (branchKey === "world") {
    openEntityModal("world");
    $("entity-modal-title").textContent = "New World Entry";
    return;
  }
  openEntityModal(branchKey);
}

// Acts aren't tied to an outline entry to exist - this creates one directly
// (empty summary) so a writer can plan an act's shape before drafting any of
// its chapters, closing the gap where an act only got a heading once a
// chapter already referenced it.
async function quickAddAct() {
  const input = await uiPrompt("New act name:");
  if (input === null) return;
  const name = input.trim();
  if (!name) return;
  const existing = (state.bible.acts || []).some(a => a.name === name);
  if (existing) { setStatus(`Act "${name}" already exists.`); return; }
  setStatus("Adding act...");
  await api(`/api/projects/${encodeURIComponent(state.slug)}/acts/${encodeURIComponent(name)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ summary: "" }),
  });
  await refreshBible();
  renderSidebar();
  setStatus("Act added.");
  selectItem("act", name);
}

async function renameLeaf(kind, oldName) {
  const input = await uiPrompt(`Rename "${oldName}" to:`, oldName);
  if (input === null) return;
  const newName = input.trim();
  if (!newName || newName === oldName) return;
  setStatus("Renaming...");
  await api(`/api/projects/${encodeURIComponent(state.slug)}/entities/${kind}/${encodeURIComponent(oldName)}/rename`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ new_name: newName }),
  });
  await refreshBible();
  markTouched(kind, newName, `${kindLabel(kind)}: ${newName} (renamed from ${oldName})`);
  if (state.selection && state.selection.kind === kind && state.selection.id === oldName) selectItem(kind, newName);
  renderSidebar();
  setStatus("Renamed.");
}

async function deleteLeaf(kind, name) {
  const label = { characters: "character", world: "world entry", research_notes: "note", timeline: "timeline event" }[kind] || kind;
  if (!(await uiConfirm(`Delete ${label} "${name}"? This cannot be undone.`))) return;
  setStatus("Deleting...");
  await api(`/api/projects/${encodeURIComponent(state.slug)}/entities/${kind}/${encodeURIComponent(name)}`, { method: "DELETE" });
  await refreshBible();
  if (state.selection && state.selection.kind === kind && state.selection.id === name) {
    closeDrawer();
  } else if (state.selection) {
    // A deleted world/faction/character entity can be cross-referenced by
    // whatever else is currently open (e.g. a character's faction checkbox
    // list, or a location's used-objects list) - refresh it in place so it
    // doesn't keep offering/showing the now-deleted entity.
    renderEditor();
  }
  renderSidebar();
  setStatus("Deleted.");
}

async function deleteOutlineLeaf(chapterNum) {
  if (!(await uiConfirm(`Delete Chapter ${chapterNum} from the outline? This cannot be undone.`))) return;
  setStatus("Deleting...");
  await api(`/api/projects/${encodeURIComponent(state.slug)}/outline/${chapterNum}`, { method: "DELETE" });
  await refreshBible();
  if (state.selection && state.selection.kind === "outline" && state.selection.id === chapterNum) closeDrawer();
  renderSidebar();
  selectChapter(chapterNum);
  setStatus("Deleted.");
}

async function deleteIdeaLeaf(ideaId, title) {
  if (!(await uiConfirm(`Delete idea "${title}"? This cannot be undone.`))) return;
  setStatus("Deleting...");
  await api(`/api/projects/${encodeURIComponent(state.slug)}/ideas/${ideaId}`, { method: "DELETE" });
  await refreshBible();
  renderSidebar();
  setStatus("Deleted.");
}

function jumpToOutlineLeaf(chapterNum) {
  setActiveTab("manuscript");
  selectChapter(chapterNum);
}

function closeLeafMenus() {
  for (const m of document.querySelectorAll(".sb-leaf-menu.open")) m.classList.remove("open");
}
document.addEventListener("click", closeLeafMenus);

// `leavesList` is the already-built sibling element holding this act's rows -
// toggling collapse just flips its "hidden" class + the caret glyph instead
// of calling renderSidebar() and rebuilding the whole tree (see buildActGroup).
function buildActHeading(act, leavesList) {
  const row = document.createElement("div");
  row.className = "sb-act-heading";

  const caret = document.createElement("span");
  caret.className = "sb-act-caret";
  caret.textContent = collapsedActs.has(act) ? "▸" : "▾";
  caret.addEventListener("click", (e) => {
    e.stopPropagation();
    if (collapsedActs.has(act)) collapsedActs.delete(act);
    else collapsedActs.add(act);
    caret.textContent = collapsedActs.has(act) ? "▸" : "▾";
    if (leavesList) leavesList.classList.toggle("hidden", collapsedActs.has(act));
  });
  row.appendChild(caret);

  const label = document.createElement("span");
  label.className = "sb-act-label";
  label.textContent = act;
  label.title = "Edit this act's outline";
  label.addEventListener("click", () => selectItem("act", act));
  row.appendChild(label);

  const addChapter = document.createElement("button");
  addChapter.className = "sb-add sb-add-chapter";
  addChapter.title = `Add chapter to ${act}`;
  addChapter.textContent = "+";
  addChapter.addEventListener("click", (e) => { e.stopPropagation(); openOutlineModal(act); });
  row.appendChild(addChapter);

  return row;
}

function buildLeaf(branchKey, leaf) {
  const row = document.createElement("div");
  row.className = "sb-leaf";

  const badge = leafStatusBadge(branchKey, leaf);
  const dot = document.createElement("span");
  dot.className = `sb-leaf-dot${badge ? ` ${badge.cls}` : ""}`;
  dot.title = badge ? badge.text : "Not drafted yet";
  row.appendChild(dot);

  if (branchKey !== "ideas") {
    const linkKind = branchKey === "outline" ? "outline" : toEntityKind(branchKey);
    const linkedIdeas = linkedIdeasFor(linkKind, leaf.id);
    if (linkedIdeas.length) {
      const ideaBadge = document.createElement("span");
      ideaBadge.className = "sb-leaf-idea";
      ideaBadge.textContent = "💡";
      ideaBadge.title = `Pending idea: ${linkedIdeas.map(i => i.title).join(", ")}`;
      row.appendChild(ideaBadge);
    }
  }

  const nameSpan = document.createElement("span");
  nameSpan.className = "sb-leaf-name";
  nameSpan.textContent = leaf.name;
  nameSpan.title = leaf.name;
  nameSpan.addEventListener("click", () => {
    if (branchKey === "ideas") openIdeaModal(leaf.record);
    else if (branchKey === "outline") jumpToOutlineLeaf(leaf.id);
    else selectItem(toEntityKind(branchKey), leaf.id);
  });
  row.appendChild(nameSpan);

  const menuWrap = document.createElement("span");
  menuWrap.className = "sb-leaf-menu-wrap";
  const menuBtn = document.createElement("button");
  menuBtn.className = "sb-leaf-menu-btn";
  menuBtn.title = "More actions";
  menuBtn.textContent = "⋯";
  const menu = document.createElement("div");
  menu.className = "sb-leaf-menu hidden dropdown-menu";

  if (branchKey === "ideas") {
    const promote = document.createElement("button");
    promote.textContent = "Promote to outline";
    promote.addEventListener("click", () => { closeLeafMenus(); openPromoteIdeaModal(leaf.record); });
    menu.appendChild(promote);
    const integrate = document.createElement("button");
    integrate.textContent = "Integrate into chapter";
    integrate.title = "Have the AI revise a drafted chapter to weave this idea in (review/approve like any other revision).";
    integrate.addEventListener("click", () => { closeLeafMenus(); openIntegrateIdeaModal(leaf.record); });
    menu.appendChild(integrate);
    const del = document.createElement("button");
    del.textContent = "Delete";
    del.addEventListener("click", () => { closeLeafMenus(); deleteIdeaLeaf(leaf.id, leaf.name); });
    menu.appendChild(del);
  } else if (branchKey === "outline") {
    const check = document.createElement("button");
    check.textContent = "Consistency check";
    check.title = "Check this chapter's storyline/outline (and drafted narrative, if any) against the rest of the bible.";
    check.addEventListener("click", () => { closeLeafMenus(); checkConsistencyForItem("outline", leaf.id, `Chapter ${leaf.id}`); });
    menu.appendChild(check);
    const del = document.createElement("button");
    del.textContent = "Delete";
    del.addEventListener("click", () => { closeLeafMenus(); deleteOutlineLeaf(leaf.id); });
    menu.appendChild(del);
  } else {
    const kind = toEntityKind(branchKey);
    const rename = document.createElement("button");
    rename.textContent = "Rename";
    rename.addEventListener("click", () => { closeLeafMenus(); renameLeaf(kind, leaf.id); });
    menu.appendChild(rename);
    const check = document.createElement("button");
    check.textContent = "Consistency check";
    check.title = kind === "characters"
      ? "Check this character - including its sections and reveals - against the rest of the bible."
      : "Check this item against the rest of the bible.";
    check.addEventListener("click", () => { closeLeafMenus(); checkConsistencyForItem(kind, leaf.id, leaf.name); });
    menu.appendChild(check);
    const del = document.createElement("button");
    del.textContent = "Delete";
    del.addEventListener("click", () => { closeLeafMenus(); deleteLeaf(kind, leaf.id); });
    menu.appendChild(del);
  }

  menuBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    const wasOpen = menu.classList.contains("open");
    closeLeafMenus();
    menu.classList.toggle("open", !wasOpen);
    menu.classList.toggle("hidden", wasOpen);
  });
  menuWrap.appendChild(menuBtn);
  menuWrap.appendChild(menu);
  row.appendChild(menuWrap);
  return row;
}

// Groups one act's heading + its chapter rows into a single element so the
// act's collapse toggle (buildActHeading) can hide/show just the rows
// sibling without touching the rest of the outline branch.
function buildActGroup(act, leaves) {
  const wrap = document.createElement("div");
  wrap.className = "sb-act-group";
  const list = document.createElement("div");
  list.className = "sb-act-leaves";
  if (collapsedActs.has(act)) list.classList.add("hidden");
  if (!leaves.length) {
    const empty = document.createElement("div");
    empty.className = "sb-empty";
    empty.textContent = "No chapters yet.";
    list.appendChild(empty);
  } else {
    for (const leaf of leaves) list.appendChild(buildLeaf("outline", leaf));
  }
  wrap.appendChild(buildActHeading(act, list));
  wrap.appendChild(list);
  return wrap;
}

export function renderSidebar() {
  renderOneSidebar("tree-sidebar", LEFT_BRANCHES);
  renderOneSidebar("entity-sidebar", RIGHT_BRANCHES);
  renderBibliographyLink();
  renderContinuityLink();
  renderCritiqueLink();
}

// Master Bible / Continuity dashboard - a standalone link like Bibliography
// and Critique below, so it's reachable from anywhere instead of only via
// the button buried in a chapter's own utility rail (which requires opening
// some chapter first). Count mirrors that button's: flagged chapters
// (continuity_issues or needs_recheck) plus open continuity_flags entries.
function renderContinuityLink() {
  const root = $("tree-sidebar");
  if (!root || !state.slug) return;
  const section = document.createElement("div");
  section.className = "sb-branch";
  const head = document.createElement("div");
  head.className = "sb-branch-head";
  const title = document.createElement("span");
  title.className = "sb-branch-title";
  const flaggedChapters = (state.bible.chapters || []).filter(
    ch => (ch.continuity_issues || []).length || ch.needs_recheck
  ).length;
  const openFlags = (state.bible.continuity_flags || []).length;
  const openCount = flaggedChapters + openFlags;
  title.textContent = openCount ? `Continuity (${openCount})` : "Continuity";
  head.appendChild(title);
  head.addEventListener("click", () => selectItem("overview", "continuity"));
  section.appendChild(head);
  root.appendChild(section);

  const railDot = $("rail-continuity-dot");
  if (railDot) {
    railDot.classList.toggle("hidden", !openCount);
    railDot.textContent = openCount || "";
  }
}

// Non-fiction-only book-level view of every cited research note, alongside
// the Research notes branch it draws from - not a real entity bucket (no
// leaves, no add/delete), so it's a standalone link rather than another
// branch in RIGHT_BRANCHES.
function renderBibliographyLink() {
  const root = $("entity-sidebar");
  if (!root || !state.slug || state.bible.book_type !== "nonfiction") return;
  const section = document.createElement("div");
  section.className = "sb-branch";
  const head = document.createElement("div");
  head.className = "sb-branch-head";
  const title = document.createElement("span");
  title.className = "sb-branch-title";
  title.textContent = "Bibliography";
  head.appendChild(title);
  head.addEventListener("click", () => selectItem("overview", "bibliography"));
  section.appendChild(head);
  root.appendChild(section);
}

// Developmental-editing findings (pacing/stakes/craft/book-level) - a
// standalone link like Bibliography above, but shown for every book (not
// nonfiction-gated) since critique applies to any genre.
function renderCritiqueLink() {
  const root = $("entity-sidebar");
  if (!root || !state.slug) return;
  const section = document.createElement("div");
  section.className = "sb-branch";
  const head = document.createElement("div");
  head.className = "sb-branch-head";
  const title = document.createElement("span");
  title.className = "sb-branch-title";
  const openCount = ((state.bible.critique_flags || [])).length;
  title.textContent = openCount ? `Critique (${openCount})` : "Critique";
  head.appendChild(title);
  head.addEventListener("click", () => selectItem("overview", "critique"));
  section.appendChild(head);
  root.appendChild(section);
}

function renderOneSidebar(rootId, branches) {
  const root = $(rootId);
  root.innerHTML = "";
  if (!state.slug) return;

  for (const branch of branches) {
    const leaves = leavesFor(branch.key);
    const section = document.createElement("div");
    section.className = "sb-branch";

    const head = document.createElement("div");
    head.className = "sb-branch-head";

    const caret = document.createElement("span");
    caret.className = "sb-caret";
    caret.textContent = collapsed.has(branch.key) ? "▸" : "▾";
    head.appendChild(caret);

    const title = document.createElement("span");
    title.className = "sb-branch-title";
    title.textContent = branch.label;
    head.appendChild(title);

    const count = document.createElement("span");
    count.className = "sb-branch-count";
    count.textContent = String(leaves.length);
    head.appendChild(count);

    const add = document.createElement("button");
    add.className = "sb-add";
    add.title = `Add ${branch.label.replace(/s$/, "")}`;
    add.textContent = "+";
    add.addEventListener("click", (e) => { e.stopPropagation(); quickAdd(branch.key); });
    head.appendChild(add);

    if (branch.key === "outline") {
      const addAct = document.createElement("button");
      addAct.className = "sb-add sb-add-act";
      addAct.title = "Add act";
      addAct.textContent = "A+";
      addAct.addEventListener("click", (e) => { e.stopPropagation(); quickAddAct(); });
      head.appendChild(addAct);
    }

    // Always build the leaf list and just toggle its visibility - avoids a
    // full renderSidebar() (and losing every other branch's collapse/menu
    // state) on a plain collapse/expand click.
    const list = document.createElement("div");
    list.className = "sb-leaves";
    if (collapsed.has(branch.key)) list.classList.add("hidden");

    if (branch.key === "timeline" && (state.bible.timeline_tracks || []).length) {
      const filterSel = document.createElement("select");
      filterSel.className = "sb-track-filter";
      const allOpt = document.createElement("option");
      allOpt.value = "";
      allOpt.textContent = "All tracks";
      filterSel.appendChild(allOpt);
      for (const t of state.bible.timeline_tracks) {
        const opt = document.createElement("option");
        opt.value = t.id;
        opt.textContent = t.name;
        filterSel.appendChild(opt);
      }
      filterSel.value = timelineTrackFilter;
      filterSel.addEventListener("click", (e) => e.stopPropagation());
      filterSel.addEventListener("change", () => {
        timelineTrackFilter = filterSel.value;
        renderSidebar();
      });
      list.appendChild(filterSel);
    }
    if (!leaves.length) {
      const empty = document.createElement("div");
      empty.className = "sb-empty";
      empty.textContent = "Nothing yet.";
      list.appendChild(empty);
    } else if (branch.key === "outline") {
      let lastAct;
      const actLeaves = [];
      const seenActs = new Set();
      const flushAct = () => { if (lastAct !== undefined) list.appendChild(buildActGroup(lastAct, actLeaves.splice(0))); };
      for (const leaf of leaves) {
        if (leaf.act !== lastAct) {
          flushAct();
          lastAct = leaf.act;
          seenActs.add(lastAct);
        }
        actLeaves.push(leaf);
      }
      flushAct();
      const emptyActs = (state.bible.acts || []).map(a => a.name).filter(name => !seenActs.has(name));
      for (const act of emptyActs) list.appendChild(buildActGroup(act, []));
    } else {
      for (const leaf of leaves) list.appendChild(buildLeaf(branch.key, leaf));
    }

    head.addEventListener("click", () => {
      if (collapsed.has(branch.key)) collapsed.delete(branch.key);
      else collapsed.add(branch.key);
      caret.textContent = collapsed.has(branch.key) ? "▸" : "▾";
      list.classList.toggle("hidden", collapsed.has(branch.key));
    });
    section.appendChild(head);
    section.appendChild(list);

    root.appendChild(section);
  }
}
