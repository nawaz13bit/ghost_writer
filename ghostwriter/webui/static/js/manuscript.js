// Single-chapter manuscript view (Manuscript tab): renders exactly one
// chapter's prose at a time, picked from the tree sidebar's Outline branch
// (or via the prev/next nav here). "Revise" embeds the shared editor body
// (#drawer-body) inline beneath the chapter - renderEditor()/renderActions()/
// renderTimeline() are reused completely unchanged, since they only ever
// address elements by id, never by ancestor. Non-chapter tree selections
// (characters/world/notes/acts/overview) instead get their own standalone
// page in this same view, built by editor.js's ensureEntityHost() - there is
// no floating drawer panel anywhere in the UI.
import { $, state, withInlineFeedback } from "./api.js";
import { chapterStatusBadge, embedDrawerInto, fixContinuityIssues, getChapter, linkedIdeasFor, recheckMessage, resetDrawerEmbed, selectItem, setSelection } from "./editor.js";

let currentChapterNum = null;
let editorOpen = false;

function currentChapterText(ch) {
  const history = ch.history || [];
  if (history.length) return history[history.length - 1].text;
  return ch.draft || "";
}

function wordCount(text) {
  const trimmed = (text || "").trim();
  return trimmed ? trimmed.split(/\s+/).length : 0;
}

function sortedOutline() {
  return (state.bible.outline || []).slice().sort((a, b) => a.chapter_num - b.chapter_num);
}

// Nonfiction drafts cite sourced research notes inline as [^note-id] markers
// (see AuthorAgent.draft_chapter / build_collaborative_system_prompt) - this
// finds which note ids a chapter's text actually references, for the Sources
// panel below and for turning the markers themselves into jump-links.
const CITATION_RE = /\[\^([\w-]+)\]/g;
function extractCitedNoteIds(text) {
  const ids = new Set();
  CITATION_RE.lastIndex = 0;
  let m;
  while ((m = CITATION_RE.exec(text || ""))) ids.add(m[1]);
  return [...ids];
}

// Word count + continuity/voice issues for the chapter in view - used to
// live in a standalone #util-rail, now rendered straight into the chapter
// card in renderManuscript() below.
function renderChapterMeta(container, ch, entry) {
  const badge = chapterStatusBadge(ch);
  const words = wordCount(currentChapterText(ch));

  const block = document.createElement("div");
  block.className = "util-block";
  const label = document.createElement("div");
  label.className = "util-label";
  label.textContent = `Chapter ${entry.chapter_num}`;
  const count = document.createElement("div");
  count.className = "util-count";
  count.textContent = words.toLocaleString();
  const sub = document.createElement("div");
  sub.className = "util-sub";
  sub.textContent = `words · ${badge.text}`;
  block.appendChild(label);
  block.appendChild(count);
  block.appendChild(sub);
  container.appendChild(block);

  const chapterIssues = [...(ch.continuity_issues || [])];
  const consistencyFlags = (state.bible.continuity_flags || []).filter(f => f.chapter_num === ch.chapter_num);
  for (const f of consistencyFlags) chapterIssues.push(f.issue || f.instruction || "Flagged by consistency check.");
  if (ch.needs_recheck) chapterIssues.unshift(recheckMessage(ch));
  if (chapterIssues.length) {
    const box = document.createElement("div");
    box.className = "util-issues-box";
    const label2 = document.createElement("div");
    label2.className = "util-label";
    label2.textContent = "Continuity/voice issues";
    box.appendChild(label2);
    const ul = document.createElement("ul");
    for (const issue of chapterIssues) {
      const li = document.createElement("li");
      li.textContent = typeof issue === "string" ? issue : JSON.stringify(issue);
      ul.appendChild(li);
    }
    box.appendChild(ul);
    if ((ch.continuity_issues || []).length) {
      const fixBtn = document.createElement("button");
      fixBtn.textContent = "Fix with AI";
      fixBtn.title = "Revise this chapter to address its own flagged continuity/voice issues, then re-check.";
      fixBtn.addEventListener("click", () => {
        withInlineFeedback(fixBtn, () => fixContinuityIssues(ch.chapter_num)).catch(() => {});
      });
      box.appendChild(fixBtn);
    }
    container.appendChild(box);
  }

  if (state.bible.book_type === "nonfiction") {
    const citedIds = extractCitedNoteIds(currentChapterText(ch));
    const notes = (state.bible.research_notes || []).filter(n => citedIds.includes(n.id));
    if (notes.length) {
      const srcBox = document.createElement("div");
      srcBox.className = "util-issues-box";
      const srcLabel = document.createElement("div");
      srcLabel.className = "util-label";
      srcLabel.textContent = "Sources";
      srcBox.appendChild(srcLabel);
      const srcList = document.createElement("ul");
      for (const n of notes) {
        const li = document.createElement("li");
        li.id = `src-${n.id}`;
        const strong = document.createElement("strong");
        strong.textContent = n.topic || n.name;
        li.appendChild(strong);
        for (const s of n.sources || []) {
          const div = document.createElement("div");
          const a = document.createElement("a");
          a.href = s.url;
          a.target = "_blank";
          a.rel = "noopener noreferrer";
          a.textContent = s.title || s.url;
          div.appendChild(a);
          li.appendChild(div);
        }
        srcList.appendChild(li);
      }
      srcBox.appendChild(srcList);
      container.appendChild(srcBox);
    }
  }

  const openFlagCount = (state.bible.continuity_flags || []).length;
  const allIssuesBtn = document.createElement("button");
  allIssuesBtn.className = "util-all-issues-btn";
  allIssuesBtn.textContent = openFlagCount ? `All continuity/voice/world issues (${openFlagCount})` : "All continuity/voice/world issues";
  allIssuesBtn.title = "Open the Master Bible / Continuity view - every open issue across chapters, characters, world, and notes";
  allIssuesBtn.addEventListener("click", () => selectItem("overview", "continuity"));
  container.appendChild(allIssuesBtn);
}

function renderProse(container, text) {
  container.innerHTML = "";
  const paras = (text || "").split(/\n{2,}/).map(p => p.trim()).filter(Boolean);
  if (!paras.length) {
    const p = document.createElement("p");
    p.className = "ms-empty";
    p.textContent = "Not drafted yet.";
    container.appendChild(p);
    return;
  }
  for (const para of paras) {
    const p = document.createElement("p");
    CITATION_RE.lastIndex = 0;
    let lastIndex = 0;
    let match;
    while ((match = CITATION_RE.exec(para))) {
      if (match.index > lastIndex) p.appendChild(document.createTextNode(para.slice(lastIndex, match.index)));
      const sup = document.createElement("sup");
      sup.className = "ms-citation";
      const a = document.createElement("a");
      a.href = `#src-${match[1]}`;
      a.textContent = `[${match[1]}]`;
      a.title = "Jump to source";
      sup.appendChild(a);
      p.appendChild(sup);
      lastIndex = CITATION_RE.lastIndex;
    }
    if (lastIndex < para.length) p.appendChild(document.createTextNode(para.slice(lastIndex)));
    container.appendChild(p);
  }
}

function closeOpenEditor(reject) {
  if (!editorOpen) return;
  const chapterNum = currentChapterNum;
  const slot = document.getElementById("ms-slot");
  const prose = document.getElementById("ms-prose");
  resetDrawerEmbed();
  if (slot) slot.classList.add("hidden");
  if (prose) {
    prose.classList.remove("hidden");
    if (reject) {
      // Discard any un-finalized revision from view - fall back to the last
      // approved text (or the original draft if never approved). Nothing is
      // deleted server-side; the rejected revision just stops being shown.
      const ch = getChapter(chapterNum) || {};
      renderProse(prose, ch.final || ch.draft || "");
    }
  }
  const reviseBtn = document.querySelector(".ms-revise-btn");
  if (reviseBtn) reviseBtn.textContent = "Revise";
  const rejectBtn = document.querySelector(".ms-reject-btn");
  if (rejectBtn) rejectBtn.classList.add("hidden");
  editorOpen = false;
}

function openEditor(chapterNum, slot, prose, btn, rejectBtn) {
  editorOpen = true;
  prose.classList.add("hidden");
  slot.classList.remove("hidden");
  embedDrawerInto(slot);
  setSelection("chapter", chapterNum);
  if (btn) btn.textContent = "Close editor";
  if (rejectBtn) rejectBtn.classList.remove("hidden");
}

function toggleRevise(chapterNum, slot, prose, btn, rejectBtn) {
  if (editorOpen) {
    closeOpenEditor();
    return;
  }
  openEditor(chapterNum, slot, prose, btn, rejectBtn);
}

export function selectChapter(chapterNum) {
  currentChapterNum = chapterNum;
  renderManuscript();
}

// Refresh just the word-count/continuity-issues block after an action
// (draft/revise/save/approve) instead of a full renderManuscript(), which
// would also re-embed the drawer and lose in-progress editor state. No-op
// if this chapter isn't the one currently in view.
export function refreshChapterMeta(chapterNum) {
  if (chapterNum == null || chapterNum !== currentChapterNum) return;
  const meta = document.getElementById("ms-chapter-meta");
  if (!meta) return;
  const outline = sortedOutline();
  const entry = outline.find(e => e.chapter_num === chapterNum);
  if (!entry) return;
  const ch = getChapter(chapterNum) || { chapter_num: chapterNum };
  meta.innerHTML = "";
  renderChapterMeta(meta, ch, entry);
}

export function renderManuscript() {
  resetDrawerEmbed();
  const root = $("manuscript-view");
  root.innerHTML = "";
  editorOpen = false;
  if (!state.slug) return;

  const outline = sortedOutline();
  if (!outline.length) {
    const empty = document.createElement("p");
    empty.className = "ms-empty";
    empty.textContent = "No chapters outlined yet.";
    root.appendChild(empty);
    return;
  }

  if (currentChapterNum == null || !outline.some(e => e.chapter_num === currentChapterNum)) {
    currentChapterNum = outline[0].chapter_num;
  }
  const idx = outline.findIndex(e => e.chapter_num === currentChapterNum);
  const entry = outline[idx];
  const ch = getChapter(entry.chapter_num) || { chapter_num: entry.chapter_num };
  const badge = chapterStatusBadge(ch);

  const page = document.createElement("div");
  page.className = "ms-page";
  root.appendChild(page);

  const nav = document.createElement("div");
  nav.className = "ms-chapter-nav";
  const prevBtn = document.createElement("button");
  prevBtn.textContent = "‹ Prev";
  prevBtn.disabled = idx <= 0;
  prevBtn.addEventListener("click", () => selectChapter(outline[idx - 1].chapter_num));
  const nextBtn = document.createElement("button");
  nextBtn.textContent = "Next ›";
  nextBtn.disabled = idx >= outline.length - 1;
  nextBtn.addEventListener("click", () => selectChapter(outline[idx + 1].chapter_num));
  nav.appendChild(prevBtn);
  nav.appendChild(nextBtn);
  page.appendChild(nav);

  const article = document.createElement("article");
  article.className = "ms-chapter";

  const eyebrow = document.createElement("div");
  eyebrow.className = "ms-eyebrow";
  eyebrow.textContent = entry.act ? `Chapter ${entry.chapter_num} · ${entry.act}` : `Chapter ${entry.chapter_num}`;
  article.appendChild(eyebrow);

  const heading = document.createElement("div");
  heading.className = "ms-chapter-head";

  const title = document.createElement("h2");
  title.className = "ms-chapter-title";
  title.textContent = entry.title || `Chapter ${entry.chapter_num}`;
  heading.appendChild(title);

  const status = document.createElement("span");
  status.className = `ms-status-pill${badge.cls ? " " + badge.cls : ""}`;
  status.textContent = badge.text;
  heading.appendChild(status);

  const linkedIdeas = linkedIdeasFor("outline", entry.chapter_num);
  if (linkedIdeas.length) {
    const ideaBadge = document.createElement("span");
    ideaBadge.className = "ms-idea-badge";
    ideaBadge.textContent = "💡";
    ideaBadge.title = `Pending idea: ${linkedIdeas.map(i => i.title).join(", ")}`;
    heading.appendChild(ideaBadge);
  }

  const reviseBtn = document.createElement("button");
  reviseBtn.className = "ms-revise-btn";
  reviseBtn.textContent = "Revise";
  heading.appendChild(reviseBtn);

  const rejectBtn = document.createElement("button");
  rejectBtn.className = "ms-reject-btn hidden";
  rejectBtn.textContent = "Reject";
  rejectBtn.title = "Discard this draft and go back to the last approved text";
  heading.appendChild(rejectBtn);

  article.appendChild(heading);

  const meta = document.createElement("div");
  meta.id = "ms-chapter-meta";
  renderChapterMeta(meta, ch, entry);
  article.appendChild(meta);

  const prose = document.createElement("div");
  prose.className = "ms-prose";
  prose.id = "ms-prose";
  renderProse(prose, currentChapterText(ch));
  article.appendChild(prose);

  const slot = document.createElement("div");
  slot.className = "ms-editor-slot hidden";
  slot.id = "ms-slot";
  article.appendChild(slot);

  reviseBtn.addEventListener("click", () => toggleRevise(entry.chapter_num, slot, prose, reviseBtn, rejectBtn));
  rejectBtn.addEventListener("click", () => closeOpenEditor(true));

  page.appendChild(article);

  // Editor drawer is the default view for a chapter now - "Close editor"
  // toggles back to plain prose. Open it immediately instead of waiting
  // for a click.
  openEditor(entry.chapter_num, slot, prose, reviseBtn, rejectBtn);
}
