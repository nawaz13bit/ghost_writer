// Project list/select/create/import/export/delete/rename, the new-project
// and import-manuscript wizards, and adding a project to a series.
import { $, api, renderTouchedBar, restoreTasks, setStatus, state, uiConfirm, uiPrompt, withInlineFeedback } from "./api.js";
import { renderEditor, showEmptyState, updateUniversalQueueBar } from "./editor.js";
import { renderSidebar } from "./sidebar.js";
import { hideTabBar, showTabBar } from "./tabs.js";

export function updateProjectPill() {
  const name = state.slug || "Select project";
  $("project-pill-name").textContent = name;
  $("btn-project-menu").title = `Switch project (current: ${name})`;
  for (const btn of $("project-menu-list").querySelectorAll("button")) {
    btn.classList.toggle("active", btn.textContent === state.slug);
  }
}

export async function loadProjects() {
  const slugs = await api("/api/projects");
  const list = $("project-menu-list");
  list.innerHTML = "";
  if (!slugs.length) {
    const empty = document.createElement("div");
    empty.className = "project-menu-empty";
    empty.textContent = "No projects yet";
    list.appendChild(empty);
  }
  for (const slug of slugs) {
    const btn = document.createElement("button");
    btn.className = "project-menu-item";
    btn.textContent = slug;
    btn.addEventListener("click", () => {
      $("project-menu").classList.add("hidden");
      selectProject(slug);
    });
    list.appendChild(btn);
  }
  updateProjectPill();
}

export async function selectProject(slug) {
  if (!slug) {
    state.slug = null;
    state.bible = null;
    state.selection = null;
    state.touchedSections = [];
    state.universalQueue = [];
    state.universalQueueIndex = 0;
    renderTouchedBar();
    updateUniversalQueueBar();
    updateProjectPill();
    hideTabBar();
    showEmptyState();
    return;
  }
  setStatus("Loading...");
  state.slug = slug;
  state.bible = await api(`/api/projects/${encodeURIComponent(slug)}`);
  state.selection = null;
  state.selectedHistoryId = null;
  restoreTasks(slug);
  renderTouchedBar();
  updateUniversalQueueBar();
  updateProjectPill();
  showTabBar();
  setStatus("");
}

// Fetches the export as a blob and, when the browser supports it (Chrome/Edge
// over a secure context - localhost counts), opens a native Save As dialog so
// the writer can pick the destination folder instead of it silently landing
// in the browser's default Downloads folder. Firefox/Safari don't implement
// showSaveFilePicker, so they fall back to the old direct-navigation download.
async function downloadWithPicker(url, filename, mimeType, pickerTypeLabel) {
  if (typeof window.showSaveFilePicker !== "function") {
    window.location.href = url;
    return;
  }
  let handle;
  try {
    handle = await window.showSaveFilePicker({
      suggestedName: filename,
      types: [{ description: pickerTypeLabel, accept: { [mimeType]: [`.${filename.split(".").pop()}`] } }],
    });
  } catch (err) {
    if (err.name === "AbortError") return; // writer cancelled the dialog
    setStatus(err.message, true);
    return;
  }
  setStatus("Exporting...");
  const resp = await fetch(url);
  if (!resp.ok) { setStatus(`Export failed: ${resp.status}`, true); return; }
  const writable = await handle.createWritable();
  await resp.body.pipeTo(writable);
  setStatus("Exported.");
}

export function exportManuscript() {
  if (!state.slug) { setStatus("Select a project first.", true); return; }
  downloadWithPicker(`/api/projects/${encodeURIComponent(state.slug)}/export`, `${state.slug}.md`, "text/markdown", "Markdown file");
}

export function exportEpub() {
  if (!state.slug) { setStatus("Select a project first.", true); return; }
  downloadWithPicker(`/api/projects/${encodeURIComponent(state.slug)}/export-epub`, `${state.slug}.epub`, "application/epub+zip", "EPUB file");
}

export function exportPdf() {
  if (!state.slug) { setStatus("Select a project first.", true); return; }
  downloadWithPicker(`/api/projects/${encodeURIComponent(state.slug)}/export-pdf`, `${state.slug}.pdf`, "application/pdf", "PDF file");
}

// -- translated exports (final-pass translation, see translateBook in editor.js) --
function selectedTranslateLanguage() {
  const sel = $("tr-language");
  return sel && sel.value ? sel.value : null;
}

export function exportManuscriptTranslated() {
  if (!state.slug) { setStatus("Select a project first.", true); return; }
  const lang = selectedTranslateLanguage();
  if (!lang) { setStatus("Select a translation language first.", true); return; }
  downloadWithPicker(
    `/api/projects/${encodeURIComponent(state.slug)}/export?language=${encodeURIComponent(lang)}`,
    `${state.slug}.${lang}.md`, "text/markdown", "Markdown file"
  );
}

export function exportEpubTranslated() {
  if (!state.slug) { setStatus("Select a project first.", true); return; }
  const lang = selectedTranslateLanguage();
  if (!lang) { setStatus("Select a translation language first.", true); return; }
  downloadWithPicker(
    `/api/projects/${encodeURIComponent(state.slug)}/export-epub?language=${encodeURIComponent(lang)}`,
    `${state.slug}.${lang}.epub`, "application/epub+zip", "EPUB file"
  );
}

export function exportPdfTranslated() {
  if (!state.slug) { setStatus("Select a project first.", true); return; }
  const lang = selectedTranslateLanguage();
  if (!lang) { setStatus("Select a translation language first.", true); return; }
  downloadWithPicker(
    `/api/projects/${encodeURIComponent(state.slug)}/export-pdf?language=${encodeURIComponent(lang)}`,
    `${state.slug}.${lang}.pdf`, "application/pdf", "PDF file"
  );
}

export async function deleteProject() {
  if (!state.slug) return;
  if (!(await uiConfirm(`Delete project "${state.slug}" and all its data? This cannot be undone.`))) return;
  setStatus("Deleting project...");
  await api(`/api/projects/${encodeURIComponent(state.slug)}`, { method: "DELETE" });
  state.slug = null;
  state.bible = null;
  state.selection = null;
  await loadProjects();
  hideTabBar();
  showEmptyState();
  setStatus("Project deleted.");
}

export async function refreshBible() {
  state.bible = await api(`/api/projects/${encodeURIComponent(state.slug)}`);
}

// -- sidebar ------------------------------------------------------------------
async function doRenameProject(newTitle) {
  if (!newTitle) { setStatus("Title is required.", true); return; }
  if (newTitle === state.bible.title) { setStatus("Title unchanged."); return; }
  if (!(await uiConfirm(`Rename "${state.bible.title}" to "${newTitle}"? This also renames the project folder on disk.`))) return;
  setStatus("Renaming...");
  const { slug } = await api(`/api/projects/${encodeURIComponent(state.slug)}/rename`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ new_title: newTitle }),
  });
  state.slug = slug;
  await loadProjects();
  await refreshBible();
  renderSidebar();
  renderEditor();
  setStatus("Renamed.");
}

export async function renameProject() {
  await doRenameProject($("ov-title").value.trim());
}

export async function renameProjectPrompt() {
  if (!state.slug) { setStatus("Select a project first.", true); return; }
  const newTitle = await uiPrompt("New project title:", state.bible?.title || "");
  if (newTitle === null) return;
  await doRenameProject(newTitle.trim());
}

export async function moveProjectPrompt() {
  if (!state.slug) { setStatus("Select a project first.", true); return; }
  const destination = await uiPrompt(
    "Move this project's folder to (absolute parent folder, e.g. D:\\my-books):", ""
  );
  if (destination === null) return;
  const dest = destination.trim();
  if (!dest) { setStatus("A destination folder is required.", true); return; }
  if (!(await uiConfirm(`Move "${state.bible.title}"'s data to ${dest}? The project stays reachable at the same place in this app either way.`))) return;
  setStatus("Moving project...");
  const { location } = await api(`/api/projects/${encodeURIComponent(state.slug)}/move`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ destination: dest }),
  });
  setStatus(`Moved to ${location}.`);
}

// -- book overview: master bible/continuity + progress tracker (client-side) ----
export let lengthCategoriesLoaded = false;
let lengthCategories = {};

export function applyLengthPlaceholders(prefix) {
  const info = lengthCategories[$(`${prefix}-length`).value];
  if (!info) return;
  $(`${prefix}-total-words`).placeholder = String(info.chapters * info.words_per_chapter);
  $(`${prefix}-chapter-words`).placeholder = String(info.words_per_chapter);
  if (prefix === "np") $("np-chapters").placeholder = String(info.chapters);
}

export async function loadLengthCategories() {
  const { categories, default: defaultKey } = await api("/api/length-categories");
  lengthCategories = categories;
  for (const id of ["np-length", "im-length"]) {
    const sel = $(id);
    sel.innerHTML = "";
    for (const [key, info] of Object.entries(categories)) {
      const opt = document.createElement("option");
      opt.value = key;
      opt.textContent = info.label;
      sel.appendChild(opt);
    }
    sel.value = defaultKey;
  }
  applyLengthPlaceholders("np");
  applyLengthPlaceholders("im");
  lengthCategoriesLoaded = true;
}

// Non-fiction projects don't have characters/POV, so hide those fields
// rather than asking the writer to fill in fields the drafting pipeline
// never reads for a non-fiction book_type.
export function applyBookTypeFields() {
  $("np-fiction-fields").classList.toggle("hidden", $("np-book-type").value === "nonfiction");
}

export function showModalStep(step) {
  $("np-step-prompt").classList.toggle("hidden", step !== "prompt");
  $("np-step-review").classList.toggle("hidden", step !== "review");
}

export function openModal() {
  if (!lengthCategoriesLoaded) loadLengthCategories().catch(err => setStatus(err.message, true));
  else applyLengthPlaceholders("np");
  $("np-freeform").value = "";
  for (const id of ["np-title", "np-genre", "np-premise", "np-tone", "np-voice", "np-engine", "np-themes", "np-characters", "np-world", "np-total-words", "np-chapters", "np-chapter-words", "np-location"]) {
    $(id).value = "";
  }
  $("np-book-type").value = "fiction";
  applyBookTypeFields();
  showModalStep("prompt");
  $("modal-backdrop").classList.remove("hidden");
}
export function closeModal() { $("modal-backdrop").classList.add("hidden"); }

function suggestTitleFromPremise(premise) {
  const words = premise.split(/\s+/).filter(Boolean).slice(0, 5).join(" ");
  return words ? words.replace(/[.,;:!?]+$/, "") : `Untitled ${new Date().toISOString().slice(0, 10)}`;
}

function entitiesToLines(entities, thirdKey) {
  // The analyzer sometimes omits a field; without the fallbacks a literal
  // "undefined" ends up in the textarea and then saved into the project file.
  return entities.map(e => {
    const second = e[thirdKey === "content" ? "category" : "role"] || "";
    const third = e[thirdKey] || e.description || e.content || "";
    return `${e.name} | ${second} | ${third}`;
  }).join("\n");
}

export async function analyzeConcept() {
  const prompt = $("np-freeform").value.trim();
  if (!prompt) {
    setStatus("Write or paste something about the book first, or use Skip.", true);
    return;
  }
  setStatus("Analyzing concept with AI...");
  const draft = await api("/api/projects/analyze", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt }),
  });
  $("np-title").value = draft.title || "";
  $("np-genre").value = draft.genre || "";
  $("np-premise").value = draft.premise || "";
  $("np-tone").value = draft.tone || "";
  $("np-voice").value = draft.narrative_voice || "";
  $("np-engine").value = draft.narrative_engine || "";
  $("np-themes").value = draft.themes || "";
  $("np-characters").value = entitiesToLines(draft.characters || [], "description");
  $("np-world").value = entitiesToLines(draft.world || [], "content");
  showModalStep("review");
  setStatus("Draft ready - review and edit before creating.");
}

function parseEntityLines(text) {
  return text.split("\n").map(l => l.trim()).filter(Boolean).map(line => {
    const [name, second, ...rest] = line.split("|").map(s => s.trim());
    return { name, second: second || "", third: rest.join("|").trim() };
  }).filter(e => e.name);
}

export async function createProject() {
  const premise = $("np-premise").value.trim();
  if (!premise) {
    setStatus("A premise is required - even a rough one-liner is enough to start.", true);
    return;
  }
  const title = $("np-title").value.trim() || suggestTitleFromPremise(premise);
  const book_type = $("np-book-type").value;
  const genre = $("np-genre").value.trim() || "general fiction";
  const length_category = $("np-length").value;
  const characters = book_type === "nonfiction" ? [] : parseEntityLines($("np-characters").value).map(e => ({ name: e.name, role: e.second || "supporting", description: e.third }));
  const world = book_type === "nonfiction" ? [] : parseEntityLines($("np-world").value).map(e => ({ name: e.name, category: e.second || "general", content: e.third }));
  const totalWordsRaw = $("np-total-words").value.trim();
  const chaptersRaw = $("np-chapters").value.trim();
  const chapterWordsRaw = $("np-chapter-words").value.trim();
  const body = {
    title, genre, premise, book_type, length_category,
    tone: $("np-tone").value.trim(),
    narrative_voice: $("np-voice").value.trim(),
    narrative_engine: $("np-engine").value.trim(),
    themes: $("np-themes").value.trim(),
    characters, world,
    total_word_target: totalWordsRaw ? parseInt(totalWordsRaw, 10) : null,
    num_chapters: chaptersRaw ? parseInt(chaptersRaw, 10) : null,
    chapter_target_words: chapterWordsRaw ? parseInt(chapterWordsRaw, 10) : null,
    location: $("np-location").value.trim() || null,
  };
  closeModal();
  setStatus("Creating project (outline)... this may take a moment.");
  const result = await api("/api/projects/finalize", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  await loadProjects();
  await selectProject(result.slug);
  setStatus("Project created.");
}

// -- import manuscript modal ---------------------------------------------------
export function openImportModal() {
  if (!lengthCategoriesLoaded) loadLengthCategories().catch(err => setStatus(err.message, true));
  else applyLengthPlaceholders("im");
  // Prefill from the currently open project, since adding more files to an
  // existing project (matched by exact title) is the common case - typing a
  // different/blank title here would create a new project instead.
  if (state.bible) {
    $("im-title").value = state.bible.title || "";
    $("im-genre").value = state.bible.genre || "";
    $("im-premise").value = "";
  } else {
    $("im-title").value = "";
    $("im-genre").value = "";
    $("im-premise").value = "";
  }
  $("im-total-words").value = "";
  $("im-chapter-words").value = "";
  $("im-files-mode").checked = false;
  $("im-files").toggleAttribute("webkitdirectory", true);
  $("im-files").value = "";
  $("import-modal-backdrop").classList.remove("hidden");
}
export function closeImportModal() { $("import-modal-backdrop").classList.add("hidden"); }

export async function importManuscript() {
  const premise = $("im-premise").value.trim() || "Update from files";
  const title = $("im-title").value.trim() || suggestTitleFromPremise(premise);
  const genre = $("im-genre").value.trim() || "general fiction";
  const length_category = $("im-length").value;
  const files = $("im-files").files;
  if (!files || !files.length) {
    setStatus("Choose at least one file (or folder) to import.", true);
    return;
  }
  closeImportModal();
  setStatus("Importing manuscript... this may take a while if the LLM is classifying content.");
  const formData = new FormData();
  formData.append("title", title);
  formData.append("genre", genre);
  formData.append("premise", premise);
  formData.append("length_category", length_category);
  const totalWordsRaw = $("im-total-words").value.trim();
  const chapterWordsRaw = $("im-chapter-words").value.trim();
  if (totalWordsRaw) formData.append("total_word_target", totalWordsRaw);
  if (chapterWordsRaw) formData.append("chapter_target_words", chapterWordsRaw);
  for (const file of files) {
    formData.append("files", file, file.webkitRelativePath || file.name);
  }
  const result = await api("/api/import", { method: "POST", body: formData });
  await loadProjects();
  await selectProject(result.slug);
  setStatus(
    `Imported: ${result.chapters} chapter(s) (${result.pending_chapters} pending review), ` +
    `${result.outline_entries} outline entr${result.outline_entries === 1 ? "y" : "ies"}, ` +
    `${result.new_characters} character(s), ${result.new_world_entries} world/lore entr${result.new_world_entries === 1 ? "y" : "ies"}, ` +
    `${result.note_entries} unsorted note(s). Review pending chapters and unsorted notes in the sidebar.`
  );
}

// -- new character/world entry modal ------------------------------------------
export function openSeriesModal() {
  if (!state.slug) { setStatus("Select a project first.", true); return; }
  if (state.bible.series_title) {
    setStatus(`This project is already part of the series "${state.bible.series_title}".`, true);
    return;
  }
  $("sm-series-title").value = "";
  $("sm-book-num").value = "1";
  $("series-modal-backdrop").classList.remove("hidden");
}
export function closeSeriesModal() { $("series-modal-backdrop").classList.add("hidden"); }

export async function confirmAddToSeries() {
  const series_title = $("sm-series-title").value.trim();
  const book_num = parseInt($("sm-book-num").value, 10) || 1;
  if (!series_title) {
    setStatus("Series name is required.", true);
    return;
  }
  closeSeriesModal();
  setStatus("Adding project to series...");
  const result = await api(`/api/projects/${encodeURIComponent(state.slug)}/series`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ series_title, book_num }),
  });
  await loadProjects();
  await selectProject(result.slug);
  setStatus(`Added to series "${result.series_title}" as book ${result.series_book_num}.`);
}

// -- series bible page (viewed as a modal) ------------------------------------
export async function syncProjectToSeries() {
  if (!state.slug) { setStatus("Select a project first.", true); return; }
  if (!state.bible.series_title) {
    setStatus("This project isn't part of a series yet - use \"Add to Series\" first.", true);
    return;
  }
  setStatus("Syncing to series bible...");
  await api(`/api/projects/${encodeURIComponent(state.slug)}/series/sync`, { method: "POST" });
  setStatus(`Synced to series "${state.bible.series_title}".`);
}

let seriesBibleSlug = null;

export async function openSeriesBibleModal() {
  if (!state.slug) { setStatus("Select a project first.", true); return; }
  if (!state.bible.series_title) {
    setStatus("This project isn't part of a series yet - use \"Add to Series\" first.", true);
    return;
  }
  setStatus("Loading series bible...");
  // attach_to_series renames the project dir to "{series-slug}__{book-slug}",
  // so the series slug is already sitting in state.slug - no need to
  // re-derive it from the title (which could diverge from the real slug on
  // punctuation/unicode the two slugify implementations don't agree on).
  const slug = state.slug.split("__")[0];
  const data = await api(`/api/series/${encodeURIComponent(slug)}`);
  seriesBibleSlug = slug;
  $("sb-title").textContent = `Series Bible: ${data.series_title}`;
  renderSeriesBible(data);
  $("series-bible-modal-backdrop").classList.remove("hidden");
  setStatus("");
}

export function closeSeriesBibleModal() {
  $("series-bible-modal-backdrop").classList.add("hidden");
}

export async function moveSeriesPrompt() {
  if (!seriesBibleSlug) { setStatus("Open a series bible first.", true); return; }
  const destination = await uiPrompt(
    "Move this series' folder to (absolute parent folder, e.g. D:\\my-books):", ""
  );
  if (destination === null) return;
  const dest = destination.trim();
  if (!dest) { setStatus("A destination folder is required.", true); return; }
  if (!(await uiConfirm(`Move this series' data to ${dest}? The series stays reachable at the same place in this app either way.`))) return;
  setStatus("Moving series...");
  const { location } = await api(`/api/series/${encodeURIComponent(seriesBibleSlug)}/move`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ destination: dest }),
  });
  setStatus(`Moved to ${location}.`);
}

function renderSeriesBible(data) {
  const charsBox = $("sb-characters");
  charsBox.innerHTML = "";
  if (!data.characters.length) {
    charsBox.textContent = "No characters yet.";
  }
  for (const c of data.characters) {
    const row = document.createElement("div");
    row.className = "sb-row";
    const name = document.createElement("span");
    name.textContent = `${c.name} (${c.role})`;
    const status = document.createElement("span");
    const s = c.status || "alive";
    status.className = "sb-status" + (s !== "alive" ? " sb-status-dead" : "");
    status.textContent = s;
    row.appendChild(name);
    row.appendChild(status);
    charsBox.appendChild(row);
  }

  const worldBox = $("sb-world");
  worldBox.innerHTML = "";
  if (!data.world.length) {
    worldBox.textContent = "No world entries yet.";
  }
  for (const w of data.world) {
    const row = document.createElement("div");
    row.className = "sb-row";
    const name = document.createElement("span");
    name.textContent = `${w.name} [${w.category}]`;
    row.appendChild(name);
    worldBox.appendChild(row);
  }

  const booksBox = $("sb-books");
  booksBox.innerHTML = "";
  if (!data.books.length) {
    booksBox.textContent = "No books recorded yet.";
  }
  for (const b of data.books.slice().sort((a, b2) => a.book_num - b2.book_num)) {
    const row = document.createElement("div");
    row.className = "sb-row";
    const name = document.createElement("span");
    name.textContent = `Book ${b.book_num}: ${b.title || "(untitled)"}`;
    row.appendChild(name);
    booksBox.appendChild(row);
  }

  const eventsBox = $("sb-events");
  eventsBox.innerHTML = "";
  const events = (data.persistent_events || []).slice().sort((a, b) => b.id - a.id);
  if (!events.length) {
    eventsBox.textContent = "No persistent events recorded yet.";
  }
  for (const e of events) {
    eventsBox.appendChild(renderSeriesEventRow(e));
  }
}

function renderSeriesEventRow(e) {
  const row = document.createElement("div");
  row.className = "sb-event" + (e.active ? "" : " sb-event-inactive");

  const summary = document.createElement("div");
  summary.textContent = `Book ${e.book_num} - "${e.event_name}": ${e.character} -> ${e.status}` + (e.active ? "" : " (reverted)");
  row.appendChild(summary);

  if (e.description) {
    const desc = document.createElement("div");
    desc.className = "modal-hint";
    desc.textContent = e.description;
    row.appendChild(desc);
  }

  const actions = document.createElement("div");
  actions.className = "sb-event-actions";

  const toggleBtn = document.createElement("button");
  toggleBtn.textContent = e.active ? "Revert" : "Reapply";
  toggleBtn.addEventListener("click", () => {
    withInlineFeedback(toggleBtn, async () => {
      const path = e.active ? "revert" : "reapply";
      await api(`/api/series/${encodeURIComponent(seriesBibleSlug)}/events/${e.id}/${path}`, { method: "POST" });
      const data = await api(`/api/series/${encodeURIComponent(seriesBibleSlug)}`);
      renderSeriesBible(data);
    }).catch(() => {});
  });
  actions.appendChild(toggleBtn);

  const editBtn = document.createElement("button");
  editBtn.textContent = "Edit";
  actions.appendChild(editBtn);

  row.appendChild(actions);

  const editBox = document.createElement("div");
  editBox.className = "sb-event-edit hidden";
  const statusInput = document.createElement("input");
  statusInput.placeholder = "New status";
  statusInput.value = e.status;
  const descInput = document.createElement("input");
  descInput.placeholder = "Description";
  descInput.value = e.description || "";
  const saveBtn = document.createElement("button");
  saveBtn.textContent = "Save";
  saveBtn.addEventListener("click", () => {
    withInlineFeedback(saveBtn, async () => {
      await api(`/api/series/${encodeURIComponent(seriesBibleSlug)}/events/${e.id}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: statusInput.value, description: descInput.value }),
      });
      const data = await api(`/api/series/${encodeURIComponent(seriesBibleSlug)}`);
      renderSeriesBible(data);
    }).catch(() => {});
  });
  editBox.appendChild(statusInput);
  editBox.appendChild(descInput);
  editBox.appendChild(saveBtn);
  row.appendChild(editBox);

  editBtn.addEventListener("click", () => editBox.classList.toggle("hidden"));

  return row;
}
