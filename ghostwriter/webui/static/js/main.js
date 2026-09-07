// Bootstrap: wires every DOM control to its handler in the other modules,
// then kicks off the initial project list load. This is the only module
// referenced by index.html.
import { $, clearTouched, emergencyStopLLM, setStatus, wireClick } from "./api.js";
import {
  advanceUniversalQueue, applyOutlineRevision, cancelBulkFix, checkConsistency, closeDrawer,
  closeEntityModal, closeIdeaModal, closeIntegrateIdeaModal, closeOutlineModal, closeReviseOutlineModal, createEntity,
  createOutlineEntry, draftFromScenes, draftMissingCharacterSections, draftOutlineRevision, integrateIdeaIntoChapter,
  migrateImportNotes, planScenesWithAI,
  loadLlmModels, loadLlmFolders, saveLlmFolders, openReviseOutlineModal, pollLlmStatus, renameEntity, reviseEngine, generateBlurb,
  reviseWithInstruction, runUniversalPrompt, saveAllCharacterSections, saveCharacterFactions, addCharacterReveal, suggestCharacterReveal, filterIdeaRelatesTo, saveIdea, saveManualEdit, savePrompt,
  clearTimelineConsequence, saveTimelineConsequence, saveTimelinePlacement, addTimelineCrosspoint, saveWorldCategory, saveWorldIsReal, saveWorldObjects,
  setPaneView, showPromptFor, showReviseOutlineStep, skipRemainingUniversalTasks,
  startLlm, stopLlm, switchModel, suggestEntity, suggestOutlineEntry,
  setUniversalMode, clearUniversalDiscuss, useDiscussSuggestion, translateBook,
  loadTranslateLanguages, translateChapter,
} from "./editor.js";
import {
  analyzeConcept, applyBookTypeFields, applyLengthPlaceholders, closeImportModal, closeModal,
  closeSeriesBibleModal, closeSeriesModal, confirmAddToSeries, createProject, deleteProject,
  exportEpub, exportManuscript, exportPdf, importManuscript, loadProjects, openImportModal,
  moveProjectPrompt, moveSeriesPrompt, openModal, openSeriesBibleModal, openSeriesModal, renameProject, renameProjectPrompt,
  showModalStep, syncProjectToSeries,
  exportManuscriptTranslated, exportEpubTranslated, exportPdfTranslated,
} from "./projects.js";
import { openPalette } from "./palette.js";
import { setActiveTab } from "./tabs.js";
import "./modal-resize.js";

function wireDropdownMenu(btnId, menuId) {
  const btn = $(btnId), menu = $(menuId);
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    menu.classList.toggle("hidden");
  });
  document.addEventListener("click", (e) => {
    if (!menu.classList.contains("hidden") && !menu.contains(e.target) && e.target !== btn) {
      menu.classList.add("hidden");
    }
  });
  // Delegated so buttons added to the menu after wiring (e.g. rebuilt on
  // every render) still close it on click.
  menu.addEventListener("click", (e) => {
    if (e.target.tagName === "BUTTON") menu.classList.add("hidden");
  });
}
function wireSubmitShortcut(inputId, buttonId) {
  $(inputId).addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      $(buttonId).click();
    }
  });
}

$("np-length").addEventListener("change", () => applyLengthPlaceholders("np"));
$("im-length").addEventListener("change", () => applyLengthPlaceholders("im"));

$("im-files-mode").addEventListener("change", (e) => {
  $("im-files").toggleAttribute("webkitdirectory", !e.target.checked);
  $("im-files").value = "";
});

$("btn-settings-close").addEventListener("click", () => setActiveTab("manuscript"));

$("btn-start-llm").addEventListener("click", () => startLlm());
$("btn-stop-llm").addEventListener("click", () => stopLlm());
$("btn-emergency-stop").addEventListener("click", () => { emergencyStopLLM(); cancelBulkFix(); });
wireClick("btn-switch-model", switchModel);
wireClick("btn-save-llm-folders", saveLlmFolders);
pollLlmStatus();
loadLlmModels().catch(() => {});
loadLlmFolders().catch(() => {});
setInterval(pollLlmStatus, 5000);

// -- wiring ---------------------------------------------------------------------
wireClick("btn-delete-project", deleteProject);
$("btn-drawer-close").addEventListener("click", closeDrawer);
$("btn-export").addEventListener("click", exportManuscript);
$("btn-export-epub").addEventListener("click", exportEpub);
$("btn-export-pdf").addEventListener("click", exportPdf);
wireClick("btn-translate-book", translateBook);
$("btn-export-tr").addEventListener("click", exportManuscriptTranslated);
$("btn-export-epub-tr").addEventListener("click", exportEpubTranslated);
$("btn-export-pdf-tr").addEventListener("click", exportPdfTranslated);
$("btn-new-project").addEventListener("click", openModal);
$("np-cancel").addEventListener("click", closeModal);
$("np-cancel2").addEventListener("click", closeModal);
$("np-skip-analyze").addEventListener("click", () => showModalStep("review"));
$("np-back").addEventListener("click", () => showModalStep("prompt"));
$("np-book-type").addEventListener("change", applyBookTypeFields);
wireClick("np-analyze", analyzeConcept);
wireClick("np-create", createProject);

$("btn-import").addEventListener("click", openImportModal);
$("im-cancel").addEventListener("click", closeImportModal);
wireClick("im-import", importManuscript);
wireClick("btn-revise", reviseWithInstruction);
wireClick("btn-save-edit", saveManualEdit);
$("btn-view-diff").addEventListener("click", () => setPaneView("diff"));
$("btn-view-edit").addEventListener("click", () => setPaneView("edit"));
wireClick("en-name-save", renameEntity);

// -- overflow-style dropdown menus (project actions, outline tools) --
wireDropdownMenu("btn-project-menu", "project-menu");
wireDropdownMenu("btn-editor-overflow", "editor-overflow-menu");

// -- universal prompt bar: hidden by default, expands in place -------------
function toggleUniversalBar(forceOpen) {
  const bar = $("universal-bar");
  const open = forceOpen !== undefined ? forceOpen : bar.classList.contains("hidden");
  bar.classList.toggle("hidden", !open);
  if (open) $("universal-prompt").focus();
}
$("btn-universal-toggle").addEventListener("click", (e) => {
  e.stopPropagation();
  toggleUniversalBar();
});
document.addEventListener("click", (e) => {
  const bar = $("universal-bar");
  if (!bar.classList.contains("hidden") && !bar.contains(e.target) && e.target !== $("btn-universal-toggle")) {
    bar.classList.add("hidden");
  }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { $("universal-bar").classList.add("hidden"); return; }
  const typing = ["TEXTAREA", "INPUT"].includes(document.activeElement.tagName);
  if (typing) return;
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "a") {
    // #pane-right-text is a read-only display <div>, not a form field, so it
    // never gets the browser's native "Ctrl+A scopes to this field" behavior -
    // without this it falls through to selecting the whole page. Scope it
    // manually to whichever box the current selection is inside.
    const box = $("pane-right-text");
    const sel = window.getSelection();
    const anchor = sel.anchorNode;
    if (box && anchor && box.contains(anchor)) {
      e.preventDefault();
      const range = document.createRange();
      range.selectNodeContents(box);
      sel.removeAllRanges();
      sel.addRange(range);
      return;
    }
  }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
    e.preventDefault();
    openPalette();
    return;
  }
  if (e.key === "/") {
    e.preventDefault();
    toggleUniversalBar(true);
  }
});
document.addEventListener("open-universal-bar", () => toggleUniversalBar(true));

// -- icon rail -----------------------------------------------------------------
for (const btn of document.querySelectorAll(".tab-btn")) {
  btn.addEventListener("click", () => setActiveTab(btn.dataset.tab));
}

$("ne-cancel").addEventListener("click", closeEntityModal);
wireClick("ne-create", createEntity);
wireClick("ne-suggest", suggestEntity);

$("idea-cancel").addEventListener("click", closeIdeaModal);
wireClick("idea-save", saveIdea);

$("ii-cancel").addEventListener("click", closeIntegrateIdeaModal);
wireClick("ii-integrate", integrateIdeaIntoChapter);

$("oo-cancel").addEventListener("click", closeOutlineModal);
wireClick("oo-create", createOutlineEntry);
wireClick("oo-suggest", suggestOutlineEntry);
$("btn-revise-outline").addEventListener("click", openReviseOutlineModal);
$("ro-cancel").addEventListener("click", closeReviseOutlineModal);
$("ro-cancel2").addEventListener("click", closeReviseOutlineModal);
$("ro-back").addEventListener("click", () => showReviseOutlineStep("instruction"));
wireClick("ro-draft", draftOutlineRevision);
wireClick("ro-apply", applyOutlineRevision);
wireClick("oe-plan-scenes", planScenesWithAI);
wireClick("oe-draft-from-scenes", draftFromScenes);
wireClick("ov-revise", reviseEngine);
wireClick("ov-generate-blurb", generateBlurb);
wireClick("ov-rename-title", renameProject);
wireClick("btn-rename-project", renameProjectPrompt);
wireClick("btn-move-project", moveProjectPrompt);

wireClick("btn-migrate-notes", migrateImportNotes);

$("btn-series").addEventListener("click", openSeriesModal);
wireClick("btn-series-sync", syncProjectToSeries);
$("btn-series-bible").addEventListener("click", openSeriesBibleModal);
$("sb-close").addEventListener("click", closeSeriesBibleModal);
wireClick("btn-move-series", moveSeriesPrompt);
$("sm-cancel").addEventListener("click", closeSeriesModal);
wireClick("sm-confirm", confirmAddToSeries);

// -- universal prompt: classify a freeform instruction (possibly several
// distinct tasks run together) and work through them one at a time, routing
// each to whichever section's existing create/revise flow already handles
// it. Each task still gets its own draft/review/save step - multitasking
// just queues several of those steps instead of doing them unsupervised. ---
wireClick("btn-universal-next", advanceUniversalQueue);
wireClick("btn-universal-skip-rest", skipRemainingUniversalTasks);
wireClick("btn-universal-touched-clear", clearTouched);
wireClick("btn-universal-check-consistency", checkConsistency);
wireClick("cs-draft-missing", draftMissingCharacterSections);
wireClick("cs-save-all", saveAllCharacterSections);
wireClick("cf-save", saveCharacterFactions);
wireClick("cr-add", addCharacterReveal);
wireClick("cr-suggest", suggestCharacterReveal);
wireClick("wo-save", saveWorldObjects);
$("idea-relates-search").addEventListener("input", filterIdeaRelatesTo);
$("wr-is-real").addEventListener("change", saveWorldIsReal);
wireClick("wc-category-save", saveWorldCategory);
wireClick("tc-save", saveTimelineConsequence);
wireClick("tc-clear", clearTimelineConsequence);
wireClick("tp-save", saveTimelinePlacement);
wireClick("tp-cp-add", addTimelineCrosspoint);
$("btn-timeline-close").addEventListener("click", () => setActiveTab("manuscript"));
wireClick("btn-universal-go", runUniversalPrompt);
wireClick("btn-universal-mode-do", () => setUniversalMode("do"));
wireClick("btn-universal-mode-discuss", () => setUniversalMode("discuss"));
wireClick("btn-universal-discuss-clear", clearUniversalDiscuss);
wireClick("btn-universal-discuss-use", useDiscussSuggestion);

// -- AI prompts (inline in Settings tab) --------------------------------------
wireClick("pr-save", () => savePrompt(false));
wireClick("pr-reset", () => savePrompt(true));
$("pr-agent").addEventListener("change", (e) => showPromptFor(e.target.value));

// Ctrl/Cmd+Enter in an instruction box submits it, same as clicking its button.
wireSubmitShortcut("universal-prompt", "btn-universal-go");
wireSubmitShortcut("instruction", "btn-revise");
wireSubmitShortcut("ov-instruction", "ov-revise");

loadProjects().catch(err => setStatus(err.message, true));
// Loaded eagerly (not just when the Settings tab is opened) so the per-chapter
// "Translate chapter" overflow action has a language to read even if the
// writer hasn't visited Settings > Translation yet.
loadTranslateLanguages().catch(() => {});
