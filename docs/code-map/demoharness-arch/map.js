"use strict";

const byId = (id) => document.getElementById(id);
const diagramWidth = 678;
const diagramHeight = 470;
const codeFile = (file) => sourceRoot + file;
const pad = (number) => String(number).padStart(2, "0");
const treeScroll = byId("tree-scroll");
const mapScroll = byId("map-scroll");
const cardsScroll = byId("cards-scroll");
const graph = byId("graph");
const sequence = byId("sequence");
const pointerPath = byId("pointer-path");

let stationIndex = 0;
let selected = null;
let selectedFile = null;
let sequenceStep = -1;
let view = "graph";
let elapsed = 0;
let lastTick = performance.now();
let playing = true;
let free = false;
let finished = false;
let redraw = 0;

function treeFile(id, file, label) {
  const icon = file.endsWith(".py") ? "py" : file.endsWith(".js") ? "js" : "↳";
  return `<li><button type="button" class="tree-file" data-node="${id}" data-file="${file}" title="src/demoharness/${file}">
    <span class="file-icon" aria-hidden="true">${icon}</span>${label}
  </button></li>`;
}

function treeFolder(label, children) {
  return `<li>
    <span class="tree-folder"><span class="folder-icon" aria-hidden="true"></span>${label}/</span>
    <ul class="tree-list">${children}</ul>
  </li>`;
}

function renderTree() {
  const files = treeOrder.filter((id) => id !== "web").map((id) => {
    const { file } = modules[id];
    return treeFile(id, file, file);
  }).join("");
  const entry = treeFile("cli", "__main__.py", "__main__.py");
  const web = treeFolder("web", modules.web.files.map(([label, file]) => treeFile("web", file, label)).join(""));
  byId("tree").innerHTML = `<ul class="tree-list tree-root">${treeFolder("src", treeFolder("demoharness", entry + files + web))}</ul>`;
}

function graphEdge(from, to, label) {
  const [x, y] = modules[from].graph;
  const [targetX, targetY] = modules[to].graph;
  const direction = Math.sign(targetX - x);
  const vertical = direction === 0;
  const startX = x + (vertical ? 0 : direction * 72);
  const startY = y + (vertical ? 31 : 0);
  const endX = targetX - (vertical ? 0 : direction * 76);
  const endY = targetY - (vertical ? 34 : 0);
  const tip = vertical
    ? `M ${endX - 4} ${endY - 5} L ${endX} ${endY} L ${endX + 4} ${endY - 5}`
    : `M ${endX - direction * 5} ${endY - 4} L ${endX} ${endY} L ${endX - direction * 5} ${endY + 4}`;
  const labelX = (startX + endX) / 2 + (vertical ? 35 : 0);
  const labelY = (startY + endY) / 2 + (vertical ? 4 : -7);
  return `<g data-from="${from}" data-to="${to}">
    <path class="edge" d="M ${startX} ${startY} L ${endX} ${endY}"/>
    <path class="edge tip" d="${tip}"/>
    <text class="edge-label" x="${labelX}" y="${labelY}">${label}</text>
  </g>`;
}

function renderGraph() {
  const edges = graphEdges.map(([from, to, label]) => graphEdge(from, to, label)).join("");
  const nodes = Object.entries(modules).map(([id, module]) => {
    const [x, y] = module.graph;
    return `<button type="button" class="graph-node" data-node="${id}" data-type="${module.type}"
      style="left:${100 * x / diagramWidth}%;top:${100 * y / diagramHeight}%" title="${module.role}">
      <strong>${module.title}</strong><small>${module.file}</small>
    </button>`;
  }).join("");
  graph.innerHTML = `<div class="graph">
    <svg class="graph-lines" viewBox="0 0 ${diagramWidth} ${diagramHeight}" preserveAspectRatio="none" aria-hidden="true">${edges}</svg>
    ${nodes}
  </div>`;
}

function renderCards() {
  byId("cards-count").textContent = `${Object.keys(modules).length} 个模块`;
  byId("cards").innerHTML = `<div class="cards">${treeOrder.map((id) => {
    const module = modules[id];
    const members = module.members.map(([name, role]) => `<dt>${name}</dt><dd>${role}</dd>`).join("");
    const files = (module.files || [[module.file, module.file]]).map(([label, file]) =>
      `<a href="${codeFile(file)}" data-file="${file}">打开 ${label} ↗</a>`
    ).join("");
    return `<article class="source-card" data-card="${id}">
      <h3><button type="button" class="card-select" data-node="${id}" aria-controls="details-${id}">
        <span class="card-heading"><strong>${module.title}</strong><small>${module.symbol}</small></span>
        <span class="card-file">${module.file}</span>
        <span class="card-role">${module.role}</span>
      </button></h3>
      <div class="card-content" id="details-${id}" hidden>
        <p class="card-description">${module.description}</p>
        <dl class="card-members">${members}</dl>
        <div class="source-links">${files}</div>
      </div>
    </article>`;
  }).join("")}</div>`;
}

function renderSequence(station) {
  if (!station.lanes) {
    sequence.replaceChildren();
    return;
  }
  const { lanes, messages } = station;
  const participants = lanes.map((id) => {
    const { title, file } = modules[id];
    return `<button type="button" class="sequence-node" data-node="${id}" title="${title} · ${file}">${title}</button>`;
  }).join("");
  const lifelines = "<span></span>".repeat(lanes.length);
  const center = (id) => (lanes.indexOf(id) + 0.5) * 100 / lanes.length;
  const rows = messages.map(([from, to, label, note, node, kind], index) => {
    const start = center(from);
    const end = center(to);
    const direction = start > end ? "to-left" : start === end ? "is-self" : "to-right";
    const response = kind === "return" ? "is-return" : "";
    const spokenDirection = kind === "return" ? "返回" : "调用";
    const accessible = `${modules[from].title} ${spokenDirection} ${modules[to].title}，${label}，${note}`;
    return `<button type="button" class="sequence-row ${direction} ${response}" data-step="${index}" data-node="${node}"
      style="--from:${Math.min(start, end)}%;--length:${Math.abs(end - start)}%;--mid:${(start + end) / 2}%"
      aria-label="${accessible}">
      <span class="sequence-index" aria-hidden="true">${pad(index + 1)}</span>
      <span class="sequence-message sequence-name"><code>${label}</code></span>
      <span class="sequence-arrow" aria-hidden="true"></span>
      <span class="sequence-message sequence-note">${note}</span>
    </button>`;
  }).join("");
  sequence.innerHTML = `<div class="sequence-board" style="--lanes:${lanes.length};--board-width:${Math.max(482, lanes.length * 102)}px">
    <div class="sequence-participants">${participants}</div>
    <div class="sequence-rows"><div class="lifelines" aria-hidden="true">${lifelines}</div>${rows}</div>
  </div>`;
}

function renderLegend() {
  byId("map-legend").innerHTML = view === "graph"
    ? '<span class="legend-item"><i class="legend-line"></i>调用或依赖</span><span class="legend-item"><i class="legend-line is-selected"></i>当前模块</span>'
    : '<span class="legend-item"><i class="legend-line"></i>调用</span><span class="legend-item"><i class="legend-line is-dashed"></i>返回或事件</span><span id="sequence-status" class="sequence-status"></span>';
  updateSequenceStatus();
}

function updateSequenceStatus() {
  const status = byId("sequence-status");
  const { messages } = stations[stationIndex];
  if (status && messages) {
    status.textContent = `消息 ${pad(sequenceStep + 1)} / ${pad(messages.length)}`;
  }
}

function renderTrack() {
  byId("tour-track").innerHTML = stations.map((station, index) => `<button type="button" class="station" data-station="${index}"
    aria-label="第 ${index + 1} 站，${station.title}" title="${station.title}">
    <span class="station-index">${pad(index + 1)}</span><span class="station-name">${station.title}</span>
  </button>`).join("");
}

function renderTour() {
  const station = stations[stationIndex];
  byId("tour-status").textContent = free ? "源码索引" : `第 ${pad(stationIndex + 1)} 站 / 共 ${stations.length} 站`;
  byId("tour-title").textContent = free ? "自由浏览" : station.title;
  byId("tour-description").textContent = free
    ? "选择一个源码文件、图节点或卡片，三个区域会同步定位。也可以从下方进入任一站。"
    : station.description;
  byId("tour-notes").hidden = free;
  byId("tour-observe").textContent = station.observe;
  const reading = byId("tour-read");
  reading.textContent = station.read;
  const sourceLinks = document.createElement("span");
  sourceLinks.className = "tour-source-links";
  for (const [label, file] of station.sources) {
    const link = document.createElement("a");
    link.href = codeFile(file);
    link.textContent = `${label} ↗`;
    sourceLinks.append(link);
  }
  reading.append(sourceLinks);
  byId("tour-links").innerHTML = free ? "" : station.chapters.map((chapter) =>
    `<a href="../../guide.md#chapter-${pad(chapter)}">第 ${pad(chapter)} 章 · ${chapters[chapter]} ↗</a>`
  ).join("");
  const commands = byId("tour-commands");
  commands.hidden = free || !station.commands;
  commands.innerHTML = commands.hidden ? "" : station.commands.map((command) => `<code>${command}</code>`).join("") +
    `<a href="${station.url}">${station.url} ↗</a>`;
  byId("sequence-tab").disabled = free || !station.lanes;
  updateControls();
  updateProgress();
}

function updateControls() {
  const button = byId("tour-play");
  button.innerHTML = playing
    ? '<span aria-hidden="true">Ⅱ</span>暂停导览'
    : finished || free ? '<span aria-hidden="true">▶</span>开始导览' : '<span aria-hidden="true">▶</span>继续导览';
  byId("tour-prev").disabled = free || stationIndex === 0;
  byId("tour-next").disabled = free || stationIndex === stations.length - 1;
  byId("tour-free").setAttribute("aria-pressed", String(free));
}

function updateProgress() {
  const buttons = byId("tour-track").querySelectorAll(".station");
  buttons.forEach((button, index) => {
    const active = !free && index === stationIndex;
    button.classList.toggle("is-current", active);
    if (active) {
      button.setAttribute("aria-current", "step");
    } else {
      button.removeAttribute("aria-current");
    }
    const progress = free ? 0 : index < stationIndex ? 1 : active ? Math.min(1, elapsed / (stations[index].seconds * 1000)) : 0;
    button.style.setProperty("--progress", progress);
  });
}

function setView(next, { reset = false } = {}) {
  view = next;
  graph.hidden = next !== "graph";
  sequence.hidden = next !== "sequence";
  byId("map-tab").setAttribute("aria-pressed", String(next === "graph"));
  byId("sequence-tab").setAttribute("aria-pressed", String(next === "sequence"));
  if (reset) {
    mapScroll.scrollTo(0, 0);
  }
  renderLegend();
  schedulePointer();
}

function reveal(container, target, { topInset = 0, horizontal = false } = {}) {
  if (!target) {
    return;
  }
  const bounds = container.getBoundingClientRect();
  const item = target.getBoundingClientRect();
  const padding = 6;
  const top = bounds.top + topInset + padding;
  const bottom = bounds.top + container.clientHeight - padding;
  let offsetY = 0;
  if (item.top < top || item.height > bottom - top) {
    offsetY = item.top - top;
  } else if (item.bottom > bottom) {
    offsetY = item.bottom - bottom;
  }
  let offsetX = 0;
  if (horizontal) {
    const left = bounds.left + padding;
    const right = bounds.left + container.clientWidth - padding;
    if (item.left < left) {
      offsetX = item.left - left;
    } else if (item.right > right) {
      offsetX = item.right - right;
    }
  }
  if (offsetX || offsetY) {
    container.scrollBy({ left: offsetX, top: offsetY, behavior: "instant" });
  }
}

function renderSelection(id) {
  document.querySelectorAll("button[data-node]:not([data-step])").forEach((button) => {
    const active = button.dataset.file ? button.dataset.file === selectedFile : button.dataset.node === id;
    button.classList.toggle("is-selected", active);
    button.setAttribute(button.classList.contains("card-select") ? "aria-expanded" : "aria-pressed", String(active));
  });
  document.querySelectorAll(".source-card").forEach((card) => {
    const active = card.dataset.card === id;
    card.classList.toggle("is-selected", active);
    card.querySelector(".card-content").hidden = !active;
    for (const link of card.querySelectorAll(".source-links a")) {
      link.toggleAttribute("data-current", link.dataset.file === selectedFile);
    }
  });
  graph.querySelectorAll("g[data-from]").forEach((edge) => {
    const active = edge.dataset.from === id || edge.dataset.to === id;
    for (const child of edge.children) {
      child.classList.toggle("is-related", active);
    }
  });
  sequence.querySelectorAll(".sequence-row").forEach((row) => {
    const active = Number(row.dataset.step) === sequenceStep;
    row.classList.toggle("is-active", active);
    row.classList.toggle("is-related", row.dataset.node === id);
    if (active) {
      row.setAttribute("aria-current", "step");
    } else {
      row.removeAttribute("aria-current");
    }
  });
}

function selectNode(id, { treeTarget } = {}) {
  selected = id;
  selectedFile = treeTarget?.dataset.file || modules[id].file;
  if (view === "sequence" && !stations[stationIndex].lanes?.includes(id)) {
    setView("graph", { reset: true });
  }
  renderSelection(id);
  const treeButton = treeScroll.querySelector(`button[data-file="${selectedFile}"]`);
  const currentCard = cardsScroll.querySelector(`[data-card="${id}"]`);
  reveal(treeScroll, treeButton, { horizontal: true });
  reveal(cardsScroll, currentCard);
  const diagram = view === "graph" ? graph : sequence;
  reveal(mapScroll, diagram.querySelector(`button[data-node="${id}"]:not([data-step])`), { horizontal: true });
  schedulePointer();
}

function selectStep(index) {
  const { messages } = stations[stationIndex];
  sequenceStep = index;
  const row = sequence.querySelector(`[data-step="${index}"]`);
  selectNode(messages[index][4]);
  reveal(mapScroll, row, { topInset: sequence.querySelector(".sequence-participants").offsetHeight });
  updateSequenceStatus();
  schedulePointer();
}

function schedulePointer() {
  if (redraw === 0) {
    redraw = requestAnimationFrame(drawPointer);
  }
}

function drawPointer() {
  redraw = 0;
  const diagram = view === "graph" ? graph : sequence;
  const origin = diagram.querySelector("button.is-selected:not([data-step])");
  const card = cardsScroll.querySelector(".source-card.is-selected");
  if (!origin || !card || byId("pointer").getBoundingClientRect().width === 0) {
    pointerPath.removeAttribute("d");
    return;
  }
  const workspace = byId("workspace").getBoundingClientRect();
  const from = origin.getBoundingClientRect();
  const to = card.getBoundingClientRect();
  const visibleMap = mapScroll.getBoundingClientRect();
  const visibleCards = cardsScroll.getBoundingClientRect();
  if (from.right > visibleMap.right || from.left < visibleMap.left ||
      from.bottom > visibleMap.bottom || from.top < visibleMap.top ||
      to.top < visibleCards.top || to.top > visibleCards.bottom) {
    pointerPath.removeAttribute("d");
    return;
  }
  const startX = from.right - workspace.left + 4;
  const startY = from.bottom - workspace.top;
  const routeY = Math.min(startY + 18, visibleMap.bottom - workspace.top - 5);
  const endX = to.left - workspace.left - 3;
  const endY = to.top - workspace.top + 20;
  const pivot = visibleMap.right - workspace.left + 4;
  pointerPath.setAttribute("d", `M ${startX} ${startY} Q ${startX + 8} ${startY + 9} ${startX + 14} ${routeY}
    L ${pivot - 6} ${routeY} Q ${pivot} ${routeY} ${pivot} ${routeY + Math.sign(endY - routeY) * 6}
    L ${pivot} ${endY} L ${endX} ${endY}`);
}

function goToStation(index, resume = playing) {
  stationIndex = index;
  const station = stations[stationIndex];
  playing = resume;
  free = false;
  finished = false;
  elapsed = 0;
  lastTick = performance.now();
  sequenceStep = station.messages ? 0 : -1;
  renderSequence(station);
  setView(station.lanes ? "sequence" : "graph", { reset: true });
  renderTour();
  if (station.messages) {
    selectStep(0);
  } else {
    selectNode(station.node);
  }
}

function pause() {
  if (playing) {
    playing = false;
    updateControls();
  }
}

function togglePlayback() {
  if (playing) {
    pause();
  } else if (free || finished) {
    goToStation(0, true);
  } else {
    playing = true;
    lastTick = performance.now();
    updateControls();
  }
}

function enterFreeBrowse() {
  playing = false;
  free = true;
  setView("graph", { reset: true });
  renderTour();
  selectNode(selected);
}

function tick(now) {
  const interval = now - lastTick;
  lastTick = now;
  if (!playing || free || document.hidden) {
    return;
  }
  const station = stations[stationIndex];
  const duration = station.seconds * 1000;
  elapsed += interval;
  if (elapsed >= duration) {
    if (stationIndex < stations.length - 1) {
      goToStation(stationIndex + 1, true);
    } else {
      finished = true;
      pause();
      updateProgress();
    }
    return;
  }
  if (station.messages && view === "sequence") {
    const step = Math.floor(elapsed / duration * station.messages.length);
    if (step !== sequenceStep) {
      selectStep(step);
    }
  }
  updateProgress();
}

byId("workspace").addEventListener("click", (event) => {
  const target = event.target.closest("button[data-node]");
  if (!target) {
    return;
  }
  pause();
  if (target.dataset.step !== undefined) {
    const index = Number(target.dataset.step);
    const station = stations[stationIndex];
    elapsed = index * station.seconds * 1000 / station.messages.length;
    selectStep(index);
    updateProgress();
  } else {
    selectNode(target.dataset.node, { treeTarget: target.classList.contains("tree-file") ? target : undefined });
  }
});
byId("tour-track").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-station]");
  if (button) {
    goToStation(Number(button.dataset.station));
  }
});
byId("tour-prev").addEventListener("click", () => goToStation(stationIndex - 1));
byId("tour-next").addEventListener("click", () => goToStation(stationIndex + 1));
byId("tour-play").addEventListener("click", togglePlayback);
byId("tour-free").addEventListener("click", enterFreeBrowse);
byId("tour-restart").addEventListener("click", () => goToStation(0, true));
byId("map-tab").addEventListener("click", () => {
  setView("graph", { reset: true });
  selectNode(selected);
});
byId("sequence-tab").addEventListener("click", () => {
  setView("sequence", { reset: true });
  selectStep(sequenceStep);
});

for (const pane of [treeScroll, mapScroll, cardsScroll]) {
  pane.addEventListener("scroll", schedulePointer, { passive: true });
}
window.addEventListener("resize", schedulePointer);
document.addEventListener("visibilitychange", () => { lastTick = performance.now(); });

renderTree();
renderGraph();
renderCards();
renderTrack();
goToStation(0);
window.setInterval(() => tick(performance.now()), 100);
