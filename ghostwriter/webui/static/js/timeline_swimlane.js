// Timeline tab: track manager + chrono-order swimlane visualization for
// non-linear books (Pattern A/B - see memory project_ghostwriter_multitrack_timeline_design).
// Renders entirely from GET .../timeline/chrono-view (lanes grouped by
// track, sorted by chrono_order) rather than state.bible.timeline directly,
// since the backend already does the track-grouping/sort work.
import { $, api, state, uiConfirm, uiPrompt, wireClick, withInlineFeedback } from "./api.js";
import { refreshBible } from "./projects.js";
import { selectItem } from "./editor.js";

const CROSSPOINT_STYLE = {
  cause_effect: { stroke: "#4a90d9", dash: "" },
  shared_location: { stroke: "#7cb342", dash: "4,3" },
  shared_object: { stroke: "#c48a2e", dash: "2,2" },
  paradox_loop: { stroke: "#c0392b", dash: "6,3" },
};

let wired = false;

export async function renderTimelineTab() {
  renderTrackManager();
  await renderSwimlane();
  if (!wired) {
    wired = true;
    wireClick("tt-add", addTrack);
  }
}

function renderTrackManager() {
  const list = $("tt-list");
  list.innerHTML = "";
  const tracks = state.bible?.timeline_tracks || [];
  if (!tracks.length) {
    list.innerHTML = '<p class="modal-hint">No tracks yet - events with no track render on the "main" row below.</p>';
    return;
  }
  for (const t of tracks) {
    const row = document.createElement("div");
    row.className = "settings-row tt-row";
    const swatch = document.createElement("span");
    swatch.className = "tt-swatch";
    swatch.style.background = t.color || "#4a90d9";
    row.appendChild(swatch);
    const nameSpan = document.createElement("span");
    nameSpan.className = "tt-name";
    nameSpan.textContent = t.name;
    row.appendChild(nameSpan);
    const renameBtn = document.createElement("button");
    renameBtn.textContent = "Rename";
    renameBtn.addEventListener("click", () => withInlineFeedback(renameBtn, () => renameTrack(t)));
    row.appendChild(renameBtn);
    const recolorBtn = document.createElement("input");
    recolorBtn.type = "color";
    recolorBtn.value = t.color || "#4a90d9";
    recolorBtn.title = "Recolor track";
    recolorBtn.addEventListener("change", () => recolorTrack(t, recolorBtn.value));
    row.appendChild(recolorBtn);
    const delBtn = document.createElement("button");
    delBtn.textContent = "Delete";
    delBtn.addEventListener("click", () => withInlineFeedback(delBtn, () => deleteTrack(t)));
    row.appendChild(delBtn);
    list.appendChild(row);
  }
}

async function addTrack() {
  const name = $("tt-new-name").value.trim();
  if (!name) return;
  const color = $("tt-new-color").value;
  await api(`/api/projects/${encodeURIComponent(state.slug)}/timeline/tracks`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, color }),
  });
  $("tt-new-name").value = "";
  await refreshBible();
  await renderTimelineTab();
}

async function renameTrack(track) {
  const name = await uiPrompt("New track name:", track.name);
  if (!name || name === track.name) return;
  await api(`/api/projects/${encodeURIComponent(state.slug)}/timeline/tracks/${encodeURIComponent(track.id)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  await refreshBible();
  await renderTimelineTab();
}

async function recolorTrack(track, color) {
  await api(`/api/projects/${encodeURIComponent(state.slug)}/timeline/tracks/${encodeURIComponent(track.id)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ color }),
  });
  await refreshBible();
  await renderTimelineTab();
}

async function deleteTrack(track) {
  const ok = await uiConfirm(`Delete track "${track.name}"? Its events move back to "main" (no track), nothing is deleted.`);
  if (!ok) return;
  await api(`/api/projects/${encodeURIComponent(state.slug)}/timeline/tracks/${encodeURIComponent(track.id)}`, {
    method: "DELETE",
  });
  await refreshBible();
  await renderTimelineTab();
}

async function renderSwimlane() {
  const container = $("timeline-swimlane");
  if (!state.slug) { container.innerHTML = ""; return; }
  const view = await api(
    `/api/projects/${encodeURIComponent(state.slug)}/timeline/chrono-view`,
    undefined,
    { silent: true },
  );
  container.innerHTML = "";
  const lanes = view.lanes || [];
  if (!lanes.length || lanes.every((l) => !l.events.length)) {
    container.innerHTML = '<p class="modal-hint">No timeline events placed on a chronology yet - open a timeline event and set its track/chrono order.</p>';
    return;
  }

  const dotPositions = {}; // event name -> {x, y}
  const laneHeight = 64;
  const dotSpacing = 140;
  const leftMargin = 160;

  const svgHeight = lanes.length * laneHeight + 20;
  const maxEvents = Math.max(1, ...lanes.map((l) => l.events.length));
  const svgWidth = leftMargin + maxEvents * dotSpacing + 40;

  const wrap = document.createElement("div");
  wrap.style.position = "relative";
  wrap.style.overflowX = "auto";
  wrap.style.width = "100%";

  const inner = document.createElement("div");
  inner.style.position = "relative";
  inner.style.width = `${svgWidth}px`;
  inner.style.height = `${svgHeight}px`;

  lanes.forEach((lane, laneIdx) => {
    const y = laneIdx * laneHeight + laneHeight / 2;
    const label = document.createElement("div");
    label.textContent = lane.track ? lane.track.name : "(main)";
    label.style.position = "absolute";
    label.style.left = "0";
    label.style.top = `${y - 10}px`;
    label.style.width = `${leftMargin - 16}px`;
    label.style.fontWeight = "600";
    label.style.color = lane.track?.color || "#888";
    inner.appendChild(label);

    lane.events.forEach((ev, i) => {
      const x = leftMargin + i * dotSpacing;
      dotPositions[ev.name] = { x, y };

      const dot = document.createElement("button");
      dot.className = "tl-swimlane-dot";
      dot.style.position = "absolute";
      dot.style.left = `${x - 6}px`;
      dot.style.top = `${y - 6}px`;
      dot.style.width = "12px";
      dot.style.height = "12px";
      dot.style.borderRadius = "50%";
      dot.style.border = "none";
      dot.style.cursor = "pointer";
      dot.style.background = lane.track?.color || "#4a90d9";
      dot.title = ev.name;
      dot.addEventListener("click", () => selectItem("timeline", ev.name));
      inner.appendChild(dot);

      const label2 = document.createElement("div");
      label2.style.position = "absolute";
      label2.style.left = `${x - dotSpacing / 2}px`;
      label2.style.top = `${y + 10}px`;
      label2.style.width = `${dotSpacing}px`;
      label2.style.textAlign = "center";
      label2.style.fontSize = "0.8em";
      label2.innerHTML = `${escapeHtml(ev.name)}${ev.chapter_num ? `<br><span class="modal-hint">ch.${ev.chapter_num}</span>` : ""}`;
      inner.appendChild(label2);
    });
  });

  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("width", String(svgWidth));
  svg.setAttribute("height", String(svgHeight));
  svg.style.position = "absolute";
  svg.style.left = "0";
  svg.style.top = "0";
  svg.style.pointerEvents = "none";

  for (const cp of view.crosspoints || []) {
    const a = dotPositions[cp.from_event];
    const b = dotPositions[cp.to_event];
    if (!a || !b) continue;
    const style = CROSSPOINT_STYLE[cp.type] || CROSSPOINT_STYLE.cause_effect;
    const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
    line.setAttribute("x1", String(a.x));
    line.setAttribute("y1", String(a.y));
    line.setAttribute("x2", String(b.x));
    line.setAttribute("y2", String(b.y));
    line.setAttribute("stroke", style.stroke);
    if (style.dash) line.setAttribute("stroke-dasharray", style.dash);
    line.setAttribute("stroke-width", "2");
    svg.appendChild(line);
  }

  inner.prepend(svg);
  wrap.appendChild(inner);
  container.appendChild(wrap);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
