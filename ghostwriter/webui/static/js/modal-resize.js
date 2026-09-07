// Makes every .modal popup drag-to-reposition and resize-to-taste, and
// remembers each modal's geometry (by element id) across sessions. A small
// reset button restores the default size/position. Modals without an id
// (the ephemeral uiConfirm/uiPrompt dialogs from api.js) still get native
// CSS resize, they just don't persist geometry or offer a drag handle.
const STORE_PREFIX = "gw-modal-geom:";

function loadGeom(id) {
  if (!id) return null;
  try {
    const raw = localStorage.getItem(STORE_PREFIX + id);
    return raw ? JSON.parse(raw) : null;
  } catch { return null; }
}

function saveGeom(id, geom) {
  if (!id) return;
  try { localStorage.setItem(STORE_PREFIX + id, JSON.stringify(geom)); } catch { /* ignore */ }
}

function clearGeom(id) {
  if (!id) return;
  try { localStorage.removeItem(STORE_PREFIX + id); } catch { /* ignore */ }
}

function currentTranslate(modal) {
  const m = new DOMMatrixReadOnly(getComputedStyle(modal).transform);
  return { dx: m.m41, dy: m.m42 };
}

function resetGeom(modal) {
  modal.style.width = "";
  modal.style.height = "";
  modal.style.transform = "";
  clearGeom(modal.id);
}

function setupModal(modal) {
  if (modal.dataset.resizableSetup || modal.classList.contains("palette")) return;
  modal.dataset.resizableSetup = "1";
  modal.classList.add("resizable-modal");

  const geom = loadGeom(modal.id);
  if (geom) {
    if (geom.width) modal.style.width = geom.width;
    if (geom.height) modal.style.height = geom.height;
    if (geom.dx || geom.dy) modal.style.transform = `translate(${geom.dx || 0}px, ${geom.dy || 0}px)`;
  }

  if (modal.id) {
    new ResizeObserver(() => {
      if (modal.dataset.dragging) return;
      const rect = modal.getBoundingClientRect();
      const prev = loadGeom(modal.id) || {};
      saveGeom(modal.id, { ...prev, width: rect.width + "px", height: rect.height + "px" });
    }).observe(modal);
  }

  const handle = modal.querySelector(":scope > h3");
  if (handle) {
    handle.classList.add("modal-drag-handle");
    handle.addEventListener("mousedown", (e) => {
      e.preventDefault();
      const startX = e.clientX, startY = e.clientY;
      const base = currentTranslate(modal);
      modal.dataset.dragging = "1";
      document.body.style.userSelect = "none";
      function onMove(ev) {
        modal.style.transform = `translate(${base.dx + ev.clientX - startX}px, ${base.dy + ev.clientY - startY}px)`;
      }
      function onUp() {
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        document.body.style.userSelect = "";
        delete modal.dataset.dragging;
        if (modal.id) {
          const prev = loadGeom(modal.id) || {};
          saveGeom(modal.id, { ...prev, ...currentTranslate(modal) });
        }
      }
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    });
  }

  const resetBtn = document.createElement("button");
  resetBtn.type = "button";
  resetBtn.className = "modal-reset-btn";
  resetBtn.title = "Reset window size & position";
  resetBtn.textContent = "↺";
  resetBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    resetGeom(modal);
  });
  modal.appendChild(resetBtn);
}

function scanBackdrop(backdrop) {
  backdrop.querySelectorAll(":scope > .modal").forEach(setupModal);
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".modal-backdrop").forEach((bd) => {
    scanBackdrop(bd);
    new MutationObserver(() => scanBackdrop(bd)).observe(bd, { childList: true });
  });
  // uiConfirm/uiPrompt build a fresh .modal-backdrop and append it straight
  // to <body>, so watch for those too.
  new MutationObserver((mutations) => {
    for (const m of mutations) {
      for (const node of m.addedNodes) {
        if (node.nodeType === 1 && node.classList?.contains("modal-backdrop")) scanBackdrop(node);
      }
    }
  }).observe(document.body, { childList: true });
});
