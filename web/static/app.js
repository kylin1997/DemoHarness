// DemoClaw 观察面前端（原生 JS，无框架）。仪表盘三栏 + 实时 SSE。
// 后端：GET /api/sessions、GET /api/sessions/<sid>/history、GET /api/bots、POST /api/bots/<name>/run、GET /api/events（SSE）

let currentSession = null;
let sse = null;

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

async function json(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

function elapsed(ts) {
  if (!ts) return "";
  const s = Math.round(Date.now() / 1000 - ts);
  return s < 60 ? `${s}s前` : `${Math.floor(s / 60)}m前`;
}

// ── 会话列表（左栏） ──
async function loadSessions() {
  const sessions = await json("/api/sessions");
  const box = $("sessions");
  box.innerHTML = "";
  for (const s of sessions) {
    const row = document.createElement("div");
    row.className = "sess" + (s.id === currentSession ? " active" : "");
    const tag = s.running_task_id
      ? '<span class="tag tag-running">运行</span>'
      : '<span class="tag tag-idle">空闲</span>';
    row.innerHTML = `${tag}${esc(s.id)}<div class="muted">bot=${esc(s.bot_id)} · ${esc(elapsed(s.created_at))}</div>`;
    row.onclick = () => { currentSession = s.id; loadHistory(); };
    box.appendChild(row);
  }
}

// ── 历史（中栏） ──
const KIND_LABEL = { user: "用户", assistant: "助手", tool_call: "工具调用", tool_result: "工具结果", compaction: "压缩", reset: "重置", interrupted: "中断" };

async function loadHistory() {
  if (!currentSession) return;
  $("hist-title").textContent = `· ${currentSession}`;
  let entries;
  try { entries = await json(`/api/sessions/${currentSession}/history`); } catch { entries = []; }
  const box = $("history");
  if (!entries.length) { box.innerHTML = '<div class="muted" style="padding:14px">无历史</div>'; return; }
  box.innerHTML = "";
  for (const e of entries) {
    const div = document.createElement("div");
    div.className = "entry " + e.kind;
    const label = KIND_LABEL[e.kind] || e.kind;
    let content = typeof e.content === "string" ? e.content : JSON.stringify(e.content);
    if (content.length > 300) content = content.slice(0, 300) + "…";
    div.innerHTML = `<span class="kind">${esc(label)}</span>${esc(content)}`;
    box.appendChild(div);
  }
  box.scrollTop = box.scrollHeight;
}

// ── 实时事件流（右栏，SSE） ──
function connectSSE() {
  if (sse) sse.close();
  sse = new EventSource("/api/events");
  sse.onopen = () => $("conn").textContent = "SSE 已连接";
  sse.onmessage = (msg) => {
    const ev = JSON.parse(msg.data);
    renderEvent(ev);
    // 事件涉及当前会话则刷新历史与会话列表
    if (ev.session_id === currentSession) loadHistory();
    if (ev.type === "task_finished" || ev.type === "task_started") loadSessions();
  };
  sse.onerror = () => $("conn").textContent = "SSE 断线";
}

function renderEvent(ev) {
  const box = $("events");
  const div = document.createElement("div");
  div.className = "evt " + (ev.type.startsWith("tool") ? "tool" : ev.type === "task_finished" ? "done" : ev.type === "phase_changed" ? "phase" : "");
  div.innerHTML = `<span class="t">${esc(ev.type)}</span> <span class="muted">sess=${esc(ev.session_id)}</span>`;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
  // 任务状态卡
  updateTaskStat(ev);
}

function updateTaskStat(ev) {
  const st = $("taskstat");
  if (ev.type === "task_started") {
    st.textContent = `任务 ${ev.task_id} 运行中`;
  } else if (ev.type === "tool_call_started") {
    st.textContent = `调用工具：${ev.data.name}`;
  } else if (ev.type === "task_finished") {
    st.textContent = `完成（${ev.data.stop_reason}）· 工具 ${ev.data.tools_used} 次`;
  }
}

// ── 发任务（POST /run，后台异步，不阻塞） ──
async function runTask() {
  const bot = $("bot").value.trim() || "architect";
  const task = $("task").value.trim();
  if (!task) { $("status").textContent = "请输入任务"; return; }
  $("status").textContent = "已提交…";
  try {
    const r = await json(`/api/bots/${encodeURIComponent(bot)}/run`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task }),
    });
    $("status").textContent = `已提交（task ${r.task_id}）`;
    currentSession = r.session_id;
    await loadSessions();
    loadHistory();
  } catch (e) {
    $("status").textContent = `错误：${e.message}`;
  }
}

$("run").onclick = runTask;
$("task").addEventListener("keydown", (e) => { if (e.key === "Enter") runTask(); });

loadSessions();
connectSSE();