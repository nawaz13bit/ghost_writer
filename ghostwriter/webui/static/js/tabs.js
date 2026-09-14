// Icon-rail router (Manuscript / Plan / Settings): Bible content (Characters/
// Factions/World/Notes) has no tab of its own - it's reached through the tree
// sidebar + drawer from any tab. Plan is the existing engine-editor (opened
// via the normal drawer selectItem flow), Manuscript is the single-chapter
// reading/revising view (manuscript.js), and Settings consolidates
// LLM/prompts/project controls.
import { $, state } from "./api.js";
import { closeDrawer, loadPrompts, loadTranslateLanguages, selectItem } from "./editor.js";
import { renderManuscript } from "./manuscript.js";
import { showSidebar, hideSidebar } from "./sidebar.js";
import { renderTimelineTab } from "./timeline_swimlane.js";

let activeTab = "manuscript";

export function showTabBar() {
  $("empty-state").classList.add("hidden");
  applyTab();
}

export function hideTabBar() {
  $("empty-state").classList.remove("hidden");
  $("manuscript-view").classList.add("hidden");
  $("tab-panel-settings").classList.add("hidden");
  $("tab-panel-timeline").classList.add("hidden");
  hideSidebar();
  activeTab = "manuscript";
}

function applyTab() {
  for (const btn of document.querySelectorAll(".tab-btn")) {
    btn.classList.toggle("active", btn.dataset.tab === activeTab);
  }

  $("manuscript-view").classList.add("hidden");
  $("tab-panel-settings").classList.add("hidden");
  $("tab-panel-timeline").classList.add("hidden");
  closeDrawer();
  hideSidebar();

  if (!state.slug) {
    if (activeTab === "settings") {
      $("tab-panel-settings").classList.remove("hidden");
      loadPrompts().catch(() => {});
      loadTranslateLanguages().catch(() => {});
    }
    return;
  }

  if (activeTab === "manuscript") {
    $("manuscript-view").classList.remove("hidden");
    renderManuscript();
    showSidebar();
  } else if (activeTab === "plan") {
    showSidebar();
    selectItem("overview", "engine");
  } else if (activeTab === "continuity") {
    selectItem("overview", "continuity");
  } else if (activeTab === "timeline") {
    $("tab-panel-timeline").classList.remove("hidden");
    renderTimelineTab();
  } else if (activeTab === "settings") {
    $("tab-panel-settings").classList.remove("hidden");
    loadPrompts().catch(() => {});
    loadTranslateLanguages().catch(() => {});
  }
}

export function setActiveTab(tab) {
  activeTab = tab === "settings" && activeTab === "settings" ? "manuscript" : tab;
  applyTab();
}
