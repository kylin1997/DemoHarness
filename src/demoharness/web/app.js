'use strict';

const byId = (id) => document.getElementById(id);
const ui = Object.fromEntries([
  'app-status', 'new-session', 'session-list', 'session-count', 'session-error',
  'chat-heading', 'chat-mode', 'messages', 'message-content', 'composer-status',
  'composer', 'prompt', 'profile', 'reasoning', 'mode', 'send', 'run-select',
  'trace-panel', 'plan-count', 'child-count', 'context-panel', 'compaction-panel',
].map((id) => [id, byId(id)]));
const defaults = { profile: 'demo', reasoning: 'default', mode: 'react' };
const state = {
  config: { profiles: [{ id: 'demo', label: '离线演示' }], reasoning: ['default', 'low', 'medium', 'high'], modes: ['react', 'plan'], defaults },
  sessions: new Map(), selected: '', draft: '', options: {},
  creating: null, submitting: false, loading: true, bootError: '', createError: '', tab: 'sequence',
};
const selectable = ['profile', 'reasoning', 'mode'];
const modeLabels = { react: 'ReAct', plan: 'Plan' };
const reasoningLabels = { default: '默认', low: '低', medium: '中', high: '高' };
const prompts = [
  ['核对文件', '工具调用与返回', '核对 workspace 里的 notes.md'],
  ['审批权限', '命令执行授权', '请用命令打印 hello'],
  ['委派任务', '独立子 Agent', '让子 Agent 读取 notes.md 并总结'],
  ['制定计划', '计划步骤与修订', '请生成一个可执行计划并检查 notes.md', 'plan'],
];
const stages = [['input', '输入'], ['context', '上下文'], ['loop', '执行'], ['output', '输出'], ['finish', '结束']];
const decisionLabels = { allow: '允许一次', trust: '本会话信任', deny: '拒绝' };
const phaseLabels = { loop: '执行', plan: '计划', compact: '压缩' };
const number = new Intl.NumberFormat('zh-CN');
const dateFormat = new Intl.DateTimeFormat('zh-CN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
const palette = ['#398b64', '#7c9e69', '#7091a0', '#ada268', '#7d83b0', '#b8826c', '#94a39c'];

function node(tag, className, content) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (content !== undefined) element.textContent = content;
  return element;
}

function button(label, handler, className) {
  const element = node('button', className, label);
  element.type = 'button';
  element.addEventListener('click', handler);
  return element;
}

function syncOptions(select, options, value) {
  const key = JSON.stringify(options);
  if (key !== select.dataset.options) {
    select.replaceChildren(...options.map(([id, label, title]) => {
      const option = new Option(label, id);
      if (title) option.title = title;
      return option;
    }));
    select.dataset.options = key;
  }
  select.value = value;
}

function choose(label, options, value, onChange) {
  const select = node('select', 'small-select');
  select.setAttribute('aria-label', label);
  syncOptions(select, options, value);
  select.addEventListener('change', () => onChange(select.value));
  return select;
}

function displayNumber(value) {
  return Number.isFinite(value) ? number.format(value) : '—';
}

function asDate(value) {
  if (typeof value === 'number') return new Date(value < 1e12 ? value * 1000 : value);
  return new Date(value || 0);
}

function displayDate(value) {
  const date = asDate(value);
  return value && Number.isFinite(date.getTime()) ? dateFormat.format(date) : '';
}

function displayValue(value) {
  return typeof value === 'string' ? value : JSON.stringify(value, null, 2) ?? '';
}

function detailBlock(label, value) {
  const box = node('div');
  box.append(node('p', 'detail-label', label), node('pre', 'detail-code', displayValue(value)));
  return box;
}

function apiError(data, fallback) {
  return typeof data?.error === 'string' && data.error.trim() ? data.error : fallback;
}

async function api(path, method = 'GET', body) {
  const response = await fetch(path, {
    method,
    ...(body === undefined ? {} : { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(apiError(data, `请求失败（${response.status}）`));
  return data;
}

function sessionPath(id) {
  return `/api/sessions/${encodeURIComponent(id)}`;
}

function currentSession() {
  return state.sessions.get(state.selected);
}

function supportedOptions(options) {
  const allowed = {
    profile: state.config.profiles.map((profile) => profile.id),
    reasoning: state.config.reasoning,
    mode: state.config.modes,
  };
  return Object.fromEntries(selectable.map((key) => [key, allowed[key].includes(options?.[key]) ? options[key] : state.config.defaults[key]]));
}

function applySession(session, record) {
  if (record) {
    record.session = { ...record.session, ...session };
    if (!record.settingWrites) record.options = supportedOptions(session.options);
    return record;
  }
  record = {
    session, options: supportedOptions(session.options), draft: '', loaded: false, loading: null,
    error: '', settingError: '', approvalError: '', settingWrites: 0, settingsQueue: Promise.resolve(), scrolling: 0,
    turns: [], runs: [], active: null, selectedRun: '', approving: new Set(),
  };
  state.sessions.set(session.id, record);
  return record;
}

function newRun(id) {
  return {
    id, events: [], start: null, terminal: null, preview: '', deltaRound: null,
    round: '', row: null, childRows: new Map(), openTools: new Set(), openChildren: new Set(),
    planRevision: '', compactionRevision: '',
  };
}

function appendEvent(run, event) {
  const isAgent = !event.actor || event.actor === 'agent';
  if (event.type === 'delta') {
    if (isAgent && typeof event.text === 'string') {
      if (run.deltaRound !== event.round) run.preview = '';
      run.deltaRound = event.round;
      run.preview += event.text;
    }
    return;
  }
  run.events.push(event);
  if (event.type === 'run_started') run.start = event;
  if (!isAgent) return;
  if (event.type === 'model_call' && (!event.phase || event.phase === 'loop') && run.deltaRound !== event.round) {
    run.preview = '';
    run.deltaRound = event.round;
  }
  if (event.type === 'guard' && event.scope === 'output' && !event.allowed) run.preview = '';
  if (event.type === 'done' || event.type === 'error') run.terminal = event;
}

function hydrate(record, messages, events) {
  const runs = new Map();
  for (const event of events) {
    if (!event.run_id || event.type === 'delta') continue;
    if (!runs.has(event.run_id)) runs.set(event.run_id, newRun(event.run_id));
    appendEvent(runs.get(event.run_id), event);
  }
  record.runs = [...runs.values()];
  record.turns = [];
  let turn;
  for (const message of messages) {
    if (message.role === 'user') {
      turn = { prompt: displayValue(message.content), reply: null, run: record.runs[record.turns.length], pending: false };
      record.turns.push(turn);
    } else if (message.role === 'assistant') {
      if (!turn) {
        turn = { prompt: null, run: null, pending: false };
        record.turns.push(turn);
      }
      turn.reply = displayValue(message.content);
    }
  }
}

function saveSelected(id) {
  try { localStorage.setItem('demoharness.selectedSession', id); } catch { /* storage may be disabled */ }
}

function readSelected() {
  try { return localStorage.getItem('demoharness.selectedSession'); } catch { return null; }
}

async function loadSession(record) {
  if (record.loaded) return;
  if (record.loading) return record.loading;
  record.error = '';
  const savedOptions = record.options;
  record.loading = (async () => {
    try {
      const detail = await api(sessionPath(record.session.id));
      const optionsChanged = savedOptions !== record.options;
      const latestOptions = record.options;
      applySession(detail.session, record);
      if (optionsChanged) record.options = latestOptions;
      hydrate(record, detail.messages ?? [], detail.events ?? []);
      record.loaded = true;
    } catch (error) {
      record.error = `无法读取会话。${error.message}`;
    } finally {
      record.loading = null;
      renderSidebar();
      if (currentSession() === record) renderWorkspace(true);
    }
  })();
  if (currentSession() === record) renderWorkspace();
  return record.loading;
}

function selectSession(id, focusChat = true) {
  const outgoing = currentSession();
  if (outgoing) outgoing.scrolling = ui.messages.scrollTop;
  if (outgoing) outgoing.draft = ui.prompt.value;
  else state.draft = ui.prompt.value;
  state.selected = id;
  saveSelected(id);
  const next = currentSession();
  ui.prompt.value = next ? next.draft : state.draft;
  if (focusChat) setView('chat');
  renderSidebar();
  renderWorkspace(true);
  if (next && !next.loaded) void loadSession(next);
}

async function createSession(copyDraft) {
  if (!state.creating) {
    state.createError = '';
    state.creating = (async () => {
      try {
        const record = applySession(await api('/api/sessions', 'POST', {}));
        record.loaded = true;
        if (copyDraft) {
          record.draft = state.draft;
          record.options = supportedOptions(state.options);
        }
        return record;
      } catch (error) {
        state.createError = `无法新建会话。${error.message}`;
        return null;
      } finally {
        state.creating = null;
        renderSidebar();
        renderComposer();
      }
    })();
    renderSidebar();
    renderComposer();
  }
  return state.creating;
}

async function boot() {
  state.loading = true;
  state.bootError = '';
  renderSidebar();
  const [config, sessions] = await Promise.allSettled([api('/api/config'), api('/api/sessions')]);
  if (config.status === 'fulfilled') {
    state.config = config.value;
    for (const record of state.sessions.values()) record.options = supportedOptions(record.options);
  }
  if (sessions.status === 'fulfilled') {
    for (const session of sessions.value.sessions) {
      if (!state.sessions.has(session.id)) applySession(session);
    }
  }
  if (config.status === 'rejected' || sessions.status === 'rejected') {
    state.bootError = '部分连接未能加载。';
  }
  state.loading = false;
  if (!currentSession() && state.sessions.size) {
    const previous = readSelected();
    selectSession(state.sessions.has(previous) ? previous : state.sessions.keys().next().value, false);
  } else {
    renderSidebar();
    renderWorkspace();
  }
}

function renderSidebar() {
  const records = [...state.sessions.values()].sort((a, b) => asDate(b.session.updated_at) - asDate(a.session.updated_at));
  const scroll = ui['session-list'].scrollTop;
  ui['session-count'].textContent = records.length ? records.length : '';
  ui['new-session'].disabled = Boolean(state.creating) || state.submitting;
  const items = records.map((record) => {
    const item = button('', () => selectSession(record.session.id), 'session-item');
    item.setAttribute('aria-current', String(record === currentSession()));
    const label = node('span', 'session-label');
    const title = node('strong', '', record.session.title || '新会话');
    title.title = title.textContent;
    label.append(title, node('small', '', record.active ? '运行中' : displayDate(record.session.updated_at || record.session.created_at)));
    item.append(node('span', 'session-icon'), label);
    return item;
  });
  if (!items.length) items.push(node('p', 'empty-side', state.loading ? '正在读取会话…' : '开始对话后，会话会保存在这里。'));
  ui['session-list'].replaceChildren(...items);
  ui['session-list'].scrollTop = scroll;
  const error = ui['session-error'];
  error.hidden = !state.bootError;
  if (state.bootError) error.replaceChildren(state.bootError, button('重新连接', boot, 'text-button'));
  const activeCount = records.filter((record) => record.active).length;
  ui['app-status'].dataset.state = state.bootError ? 'error' : activeCount ? 'running' : 'ready';
  ui['app-status'].lastElementChild.textContent = state.loading ? '连接中' : state.bootError ? '部分连接异常' : activeCount ? `${activeCount} 次运行中` : '运行端已连接';
}

function setView(view) {
  document.body.dataset.view = view;
  for (const toggle of document.querySelectorAll('.view-nav button')) {
    toggle.setAttribute('aria-pressed', String(toggle.dataset.view === view));
  }
}

function settingsChanged(event) {
  const key = event.target.id;
  const value = event.target.value;
  const record = currentSession();
  if (!record) {
    state.options = { ...state.options, [key]: value };
    renderComposer();
    return;
  }
  record.options = { ...record.options, [key]: value };
  record.settingError = '';
  record.settingWrites++;
  record.settingsQueue = record.settingsQueue.then(async () => {
    try {
      const updated = await api(sessionPath(record.session.id), 'PATCH', { options: { [key]: value } });
      record.session = { ...record.session, ...updated };
    } catch (error) {
      record.settingError = `设置保存失败。${error.message}`;
    } finally {
      record.settingWrites--;
      if (currentSession() === record) renderComposer();
    }
  });
  renderComposer();
}

function renderComposer() {
  const record = currentSession();
  const options = record?.options ?? supportedOptions(state.options);
  syncOptions(ui.profile, state.config.profiles.map(({ id, label, model }) => [id, label || id, model]), options.profile);
  syncOptions(ui.reasoning, state.config.reasoning.map((key) => [key, reasoningLabels[key] || key]), options.reasoning);
  syncOptions(ui.mode, state.config.modes.map((key) => [key, modeLabels[key] || key]), options.mode);
  ui['chat-heading'].textContent = record?.session.title || '新会话';
  ui['chat-mode'].textContent = modeLabels[options.mode] || options.mode;
  ui['chat-mode'].title = '下一次运行的模式';
  ui.send.disabled = !ui.prompt.value.trim() || Boolean(record && (!record.loaded || record.active)) || state.submitting || Boolean(state.creating);
  ui.send.querySelector('.send-label').textContent = record?.active ? '运行中' : '发送';
  const error = record?.error || record?.approvalError || record?.settingError || state.createError;
  ui['composer-status'].hidden = !error;
  if (error) {
    ui['composer-status'].replaceChildren(error);
    if (record?.error && !record.loaded) {
      ui['composer-status'].append(' ', button('重试', () => loadSession(record), 'text-button'));
    }
  }
}

async function* parseEventStream(stream) {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let data = [];
  let type = '';
  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      if (done && buffer) buffer += '\n\n';
      let newline;
      while ((newline = buffer.indexOf('\n')) !== -1) {
        const line = buffer.slice(0, newline).replace(/\r$/, '');
        buffer = buffer.slice(newline + 1);
        if (!line) {
          if (data.length) {
            const event = JSON.parse(data.join('\n'));
            yield { ...event, type: event.type || type };
          }
          data = [];
          type = '';
        } else if (line.startsWith('data:')) {
          data.push(line.slice(5).replace(/^ /, ''));
        } else if (line.startsWith('event:')) {
          type = line.slice(6).trim();
        }
      }
      if (done) break;
    }
  } finally {
    reader.releaseLock();
  }
}

function receiveEvent(record, turn, event) {
  if (!event.run_id || (event.session_id && event.session_id !== record.session.id)) return;
  if (!turn.run) {
    turn.run = newRun(event.run_id);
    record.runs.push(turn.run);
  }
  if (turn.run.id !== event.run_id) return;
  const first = !turn.run.events.length;
  appendEvent(turn.run, event);
  if (event.type === 'run_started') {
    record.session.updated_at = event.at;
    if (record.session.title === '新会话') record.session.title = [...turn.prompt.replace(/\s+/g, ' ')].slice(0, 32).join('');
    renderSidebar();
  }
  if (event.type === 'approval_resolved') {
    record.approving.delete(event.approval_id);
    record.approvalError = '';
  }
  if (currentSession() !== record) return;
  if (event.type === 'delta') {
    scheduleDraft();
    return;
  }
  if (first || ['model_call', 'tool_call', 'tool_result', 'approval', 'approval_resolved', 'guard', 'done', 'error'].includes(event.type)) renderChat();
  renderInspector();
  if (first || event.type === 'approval_resolved') renderComposer();
}

async function send(event) {
  event.preventDefault();
  if (ui.send.disabled) return;
  const message = ui.prompt.value.trim();
  if (!message) return;
  let record = currentSession();
  if (!record) {
    state.draft = ui.prompt.value;
    const previousSelection = state.selected;
    state.submitting = true;
    try {
      record = await createSession(true);
      if (!record) return;
      if (state.selected === previousSelection) selectSession(record.session.id);
    } finally {
      state.submitting = false;
      renderSidebar();
      renderComposer();
    }
  }
  if (record.active || !record.loaded) return;
  const options = { ...record.options };
  const settingsReady = record.settingsQueue;
  const turn = { prompt: message, reply: null, run: null, pending: true };
  record.active = turn;
  record.turns.push(turn);
  record.selectedRun = '';
  record.error = '';
  record.approvalError = '';
  record.draft = '';
  if (currentSession() === record) {
    ui.prompt.value = '';
    renderWorkspace(true);
  }
  renderSidebar();
  try {
    await settingsReady;
    const response = await fetch('/api/chat', {
      method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify({ session_id: record.session.id, message, ...options }),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => null);
      throw new Error(apiError(body, `请求失败（${response.status}）`));
    }
    if (!response.body) throw new Error('未收到事件流。');
    for await (const incoming of parseEventStream(response.body)) receiveEvent(record, turn, incoming);
    if (!turn.run?.terminal) throw new Error('事件流在运行结束前断开。');
  } catch (error) {
    record.error = error.message;
    if (!turn.run) {
      record.turns.pop();
      if (!record.draft) record.draft = message;
      if (currentSession() === record && !ui.prompt.value) ui.prompt.value = record.draft;
    }
  } finally {
    turn.pending = false;
    record.active = null;
    renderSidebar();
    if (currentSession() === record) renderWorkspace();
  }
}

function welcome() {
  const hero = node('div', 'welcome');
  const icon = node('div', 'welcome-icon');
  icon.setAttribute('aria-hidden', 'true');
  const mark = node('span', 'brand-mark');
  mark.append(node('i'), node('i'), node('i'));
  icon.append(mark);
  hero.append(icon, node('h3', '', '向 Agent 提问'), node('p', '', '选一个示例，观察模型、工具和防护如何协作。'));
  const grid = node('div', 'example-grid');
  for (const [title, caption, prompt, mode] of prompts) {
    const action = button('', () => {
      ui.prompt.value = prompt;
      setView('chat');
      if (mode) {
        ui.mode.value = mode;
        ui.mode.dispatchEvent(new Event('change'));
      }
      updateDraft();
      ui.prompt.focus();
    }, 'example');
    const arrow = node('span', '', '↗');
    arrow.setAttribute('aria-hidden', 'true');
    action.append(node('strong', '', title), arrow, node('small', '', caption));
    grid.append(action);
  }
  hero.append(grid);
  return hero;
}

function inlineText(parent, text) {
  const parts = text.split(/(`[^`\n]+`|\*\*[^*\n]+\*\*)/g);
  for (const part of parts) {
    if (part.startsWith('`') && part.endsWith('`') && part.length > 2) parent.append(node('code', '', part.slice(1, -1)));
    else if (part.startsWith('**') && part.endsWith('**') && part.length > 4) parent.append(node('strong', '', part.slice(2, -2)));
    else parent.append(part);
  }
}

function markdown(text) {
  const fragment = document.createDocumentFragment();
  let list = null;
  let code = null;
  let paragraph = null;
  for (const line of text.split('\n')) {
    if (/^\s*```/.test(line)) {
      paragraph = null;
      list = null;
      if (code) {
        const pre = node('pre');
        pre.append(node('code', '', code.join('\n')));
        fragment.append(pre);
        code = null;
      } else code = [];
      continue;
    }
    if (code) {
      code.push(line);
      continue;
    }
    const item = line.match(/^\s*(?:([-*])|\d+\.)\s+(.+)$/);
    const heading = line.match(/^\s*#{1,6}\s+(.+)$/);
    if (item) {
      paragraph = null;
      const tag = item[1] ? 'ul' : 'ol';
      if (list?.localName !== tag) {
        list = node(tag);
        fragment.append(list);
      }
      const li = node('li');
      inlineText(li, item[2]);
      list.append(li);
    } else if (heading) {
      paragraph = null;
      list = null;
      const title = node('h4');
      inlineText(title, heading[1]);
      fragment.append(title);
    } else if (line.trim()) {
      list = null;
      if (!paragraph) {
        paragraph = node('p');
        fragment.append(paragraph);
      } else paragraph.append(node('br'));
      inlineText(paragraph, line);
    } else {
      list = null;
      paragraph = null;
    }
  }
  if (code) {
    const pre = node('pre');
    pre.append(node('code', '', code.join('\n')));
    fragment.append(pre);
  }
  return fragment;
}

function actorEvents(run) {
  return run?.events.filter((event) => !event.actor || event.actor === 'agent') ?? [];
}

function keepOpen(details, collection, key, initiallyOpen = false) {
  details.open = collection.has(key) || initiallyOpen;
  details.addEventListener('toggle', () => {
    if (!details.isConnected) return;
    if (details.open) collection.add(key);
    else collection.delete(key);
  });
  return details;
}

function activity(record, turn) {
  const container = node('div', 'message-activity');
  const items = new Map();
  const approvals = new Map();
  const resolved = new Map();
  for (const event of actorEvents(turn.run)) {
    if (event.type === 'tool_call' || event.type === 'tool_result') {
      const key = `tool:${event.call_id || event.name}`;
      if (!items.has(key)) items.set(key, { name: event.name });
      items.get(key)[event.type] = event;
    }
    if (event.type === 'approval') {
      const key = `approval:${event.approval_id}`;
      approvals.set(key, event);
      items.set(key, event);
    }
    if (event.type === 'approval_resolved') resolved.set(`approval:${event.approval_id}`, event);
  }
  for (const [key, item] of items) {
    if (approvals.has(key)) {
      const result = resolved.get(key);
      const pending = turn.pending && !turn.run.terminal && !result;
      const title = node('div', 'approval-title');
      title.append(node('span', '', `授权请求 · ${item.name}`), node('span', '', result ? decisionLabels[result.decision] || '已结束' : pending ? '待决定' : '已结束'));
      const entry = node('section', 'approval-item');
      entry.append(title);
      if (item.args !== undefined) entry.append(node('pre', 'detail-code', displayValue(item.args)));
      if (result?.reason) entry.append(node('p', 'detail-label', result.reason));
      if (pending) {
        const actions = node('div', 'approval-actions');
        for (const [decision, label] of Object.entries(decisionLabels)) {
          const action = button(label, () => approve(record, item.approval_id, decision));
          action.disabled = record.approving.has(item.approval_id);
          action.dataset.focus = `${key}:${decision}`;
          actions.append(action);
        }
        entry.append(actions);
      }
      container.append(entry);
      continue;
    }
    const call = item.tool_call;
    const result = item.tool_result;
    const details = keepOpen(node('details', 'tool-output'), turn.run.openTools, key);
    const summary = node('summary');
    const status = result ? result.is_error ? '失败' : '返回结果' : turn.pending ? '调用中' : '已中断';
    summary.dataset.focus = key;
    summary.append(node('code', '', item.name), node('span', `tool-state${result?.is_error ? ' error' : ''}`, status));
    const body = node('div', 'tool-detail');
    if (call?.args !== undefined) body.append(detailBlock('调用参数', call.args));
    if (result) body.append(detailBlock('工具结果', result.result));
    details.append(summary);
    if (result?.result) {
      const preview = node('span', 'tool-preview', displayValue(result.result).slice(0, 240));
      summary.append(preview);
    }
    details.append(body);
    container.append(details);
  }
  return container;
}

async function approve(record, id, decision) {
  if (record.approving.has(id)) return;
  record.approving.add(id);
  record.approvalError = '';
  if (record === currentSession()) renderChat();
  try {
    await api('/api/approve', 'POST', { session_id: record.session.id, approval_id: id, decision });
  } catch (error) {
    record.approving.delete(id);
    record.approvalError = `审批提交失败。${error.message}`;
    if (record === currentSession()) {
      renderChat();
      renderComposer();
    }
  }
}

function chatMessage(role, time, content) {
  const row = node('div', 'chat-message');
  const avatar = node('div', `avatar ${role}`, role === 'agent' ? 'A' : '你');
  avatar.setAttribute('aria-hidden', 'true');
  const body = node('div', 'message-body');
  const label = node('div', 'message-label', role === 'agent' ? 'Agent' : '你');
  if (time) label.append(node('span', '', time));
  body.append(label, ...content);
  row.append(avatar, body);
  return row;
}

function renderTurn(record, turn) {
  const row = node('article', 'turn');
  const run = turn.run;
  if (turn.prompt !== null) {
    row.append(chatMessage('user', displayDate(run?.start?.at), [node('div', 'message-text plain', turn.prompt)]));
  }
  const final = run?.terminal;
  const text = final?.type === 'done' ? final.text ?? turn.reply : turn.reply;
  const contents = [activity(record, turn)];
  if (text) {
    const answer = node('div', 'message-text');
    answer.append(markdown(text));
    contents.push(answer);
  } else if (final?.type === 'error') {
    contents.push(node('p', 'waiting', final.message ?? '运行失败。'));
  } else if (turn.pending && !final) {
    contents.push(node('div', 'message-text plain live-draft', run?.preview || ''));
    if (!run?.preview) {
      const status = node('span', 'waiting', '正在处理…');
      status.id = 'draft-wait';
      contents.push(status);
    }
  } else if (run && !final) {
    contents.push(node('span', 'waiting', '运行已中断。'));
  } else if (final?.type === 'done') {
    contents.push(node('span', 'waiting', '运行已结束。'));
  }
  if (contents.length > 1 || contents[0].childElementCount) row.append(chatMessage('agent', '', contents));
  return row;
}

function preserveFocus(container, update) {
  const active = document.activeElement;
  const key = container.contains(active) ? active.dataset.focus : null;
  update();
  if (key) {
    for (const candidate of container.querySelectorAll('[data-focus]')) {
      if (key === candidate.dataset.focus) {
        candidate.focus({ preventScroll: true });
        break;
      }
    }
  }
}

function renderChat(reset = false) {
  const record = currentSession();
  const list = ui['message-content'];
  const scroll = ui.messages;
  const previous = scroll.scrollTop;
  const atBottom = scroll.scrollHeight - previous - scroll.clientHeight < 55;
  const pending = record && !record.loaded && !record.error;
  const turns = record?.turns ?? [];
  list.classList.toggle('is-empty', Boolean(pending || !turns.length));
  preserveFocus(list, () => {
    if (pending) list.replaceChildren(node('p', 'waiting', '正在读取会话记录…'));
    else if (!turns.length) list.replaceChildren(welcome());
    else list.replaceChildren(...turns.map((turn) => renderTurn(record, turn)));
  });
  if (reset && record?.scrolling && !record.active) scroll.scrollTop = record.scrolling;
  else if (reset || (record?.active && atBottom)) scroll.scrollTop = scroll.scrollHeight;
  else scroll.scrollTop = previous;
}

let draftFrame = 0;
function scheduleDraft() {
  if (draftFrame) return;
  draftFrame = requestAnimationFrame(() => {
    draftFrame = 0;
    const text = ui['message-content'].querySelector('.live-draft');
    const preview = currentSession()?.active?.run?.preview;
    if (!text || !preview) return;
    const scroll = ui.messages;
    const atBottom = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 55;
    text.textContent = preview;
    byId('draft-wait')?.remove();
    if (atBottom) scroll.scrollTop = scroll.scrollHeight;
  });
}

function eventDescription(event) {
  const phase = phaseLabels[event.phase] ?? '执行';
  const scope = { input: '输入', tool: '工具', output: '输出' }[event.scope] ?? '防护';
  switch (event.type) {
    case 'model_call': return [0, 1, `请求模型 · ${phase}`, 'blue', event.tools?.length ? event.tools.join('、') : '本次请求没有公开工具。'];
    case 'model_result': return [1, 0, `模型返回 · ${phase}`, 'blue', `输入 ${displayNumber(event.input_tokens)} Token\n输出 ${displayNumber(event.output_tokens)} Token`];
    case 'tool_call': return [0, 2, `调用 · ${event.name}`, '', event.args];
    case 'tool_result': return [2, 0, `${event.is_error ? '失败' : '返回'} · ${event.name}`, event.is_error ? 'orange' : '', event.result];
    case 'guard': return event.allowed ? [3, 0, `${scope}防护 · 通过`, '', event.message] : [3, 0, `${scope}防护 · 拦截`, 'orange', event.message];
    case 'approval': return [3, 0, `等待审批 · ${event.name}`, 'orange', event.args];
    case 'approval_resolved': return [0, 3, `审批 · ${decisionLabels[event.decision] || '结束'}`, 'orange', event.reason];
    case 'tools_loaded': return [2, 0, `发现 ${event.names?.length ?? 0} 个工具`, '', event.names?.join('、')];
    case 'plan': return [0, 0, '更新计划', '', event.items?.map((item) => item.title).join('\n')];
    case 'subagent_start': return [0, 2, '委派给子 Agent', '', event.task];
    case 'subagent_end': return [2, 0, '子 Agent 返回', '', event.result];
    case 'error': return [0, 0, '运行失败', 'orange', event.message];
    default: return null;
  }
}

function timeline(events, run, childId = '', follow = false) {
  const frame = node('div', 'sequence');
  const header = node('div', 'sequence-head');
  for (const label of [childId ? '子 Agent' : 'Harness', '模型', '工具', '防护']) header.append(node('span', '', label));
  const body = node('div', 'sequence-body');
  body.dataset.timeline = childId || 'main';
  frame.append(header, body);
  let selected = null;
  let count = 0;
  for (const [index, event] of events.entries()) {
    const description = eventDescription(event);
    if (!description || (!childId && run.round && String(event.round) !== run.round)) continue;
    count++;
    const [from, to, label, tone, detail] = description;
    const key = `${childId}:${index}`;
    const isSelected = (childId ? run.childRows.get(childId) : run.row) === key;
    const row = button('', () => {
      if (childId) run.childRows.set(childId, isSelected ? null : key);
      else run.row = isSelected ? null : key;
      renderInspector();
    }, `sequence-row ${tone}`);
    row.dataset.focus = key;
    row.setAttribute('aria-pressed', String(isSelected));
    row.setAttribute('aria-label', `${event.round ? `第 ${event.round} 轮，` : ''}${label}`);
    row.title = label;
    const labelBox = node('span', 'sequence-label');
    labelBox.append(node('span', '', label));
    const arrow = node('span', `sequence-arrow${from === to ? ' self' : ''}${from > to ? ' reverse return' : ''}`);
    arrow.style.setProperty('--arrow-left', `${Math.min(from, to) * 25 + 12.5}%`);
    arrow.style.setProperty('--arrow-width', `${Math.abs(from - to) * 25}%`);
    row.append(labelBox, arrow);
    body.append(row);
    if (isSelected) selected = { label, detail, round: event.round };
  }
  if (!count) body.append(node('p', 'empty-panel timeline-empty', '当前范围暂无调用记录。'));
  if (follow) body.dataset.follow = 'true';
  const box = node('div');
  box.append(frame);
  if (selected) {
    const detail = node('div', 'trace-detail');
    detail.append(node('strong', '', `${selected.round ? `第 ${selected.round} 轮 · ` : ''}${selected.label}`));
    if (selected.detail !== undefined) detail.append(node('pre', 'detail-code', displayValue(selected.detail)));
    box.append(detail);
  }
  return box;
}

function renderSequence(events, run, active) {
  const pane = node('div');
  const reached = events.filter((event) => event.type === 'stage').map((event) => event.stage);
  const recentStage = reached.at(-1);
  if (recentStage) {
    const track = node('div', 'stage-track');
    track.setAttribute('aria-label', '运行阶段');
    for (const [key, label] of stages) {
      const stage = node('span', `stage ${key === recentStage ? 'current' : reached.includes(key) ? 'complete' : ''}`, label);
      if (key === recentStage) stage.setAttribute('aria-current', 'step');
      track.append(stage);
    }
    pane.append(track);
  }
  const options = [['', '全部轮次']];
  for (const round of new Set(events.filter(eventDescription).map((event) => event.round))) {
    if (Number.isInteger(round)) options.push([String(round), round ? `第 ${round} 轮` : '执行前']);
  }
  const control = node('div', 'trace-control');
  const roundSelect = choose('选择轮次', options, run.round, (value) => {
    run.round = value;
    run.row = null;
    renderInspector();
  });
  roundSelect.dataset.focus = 'round-select';
  control.append(node('span', '', '实线调用 / 虚线返回'), roundSelect);
  pane.append(control, timeline(events, run, '', active && !run.round && !run.row));
  return pane;
}

function renderPlan(records, run) {
  if (!records.length) return node('p', 'empty-panel', '当前运行暂无计划。');
  const pane = node('div');
  const revision = run.planRevision === '' ? records.length - 1 : Number(run.planRevision);
  const select = choose('选择计划修订', [['', '最新修订'], ...records.map((_, i) => [String(i), `修订 ${i + 1}`])], run.planRevision, (value) => {
    run.planRevision = value;
    renderInspector();
  });
  select.dataset.focus = 'plan-revision';
  const control = node('div', 'trace-control');
  control.append(node('span', '', `版本 ${revision + 1} / ${records.length}`), select);
  const statusLabels = { pending: '待执行', in_progress: '执行中', done: '已完成' };
  const list = node('ol', 'plan-list');
  for (const [index, item] of (records[revision]?.items ?? []).entries()) {
    const entry = node('li', `plan-item ${item.status}`);
    entry.append(node('span', 'step-num', String(index + 1).padStart(2, '0')), node('span', '', item.title), node('span', 'step-status', statusLabels[item.status] ?? item.status));
    list.append(entry);
  }
  pane.append(control, list);
  return pane;
}

function renderChildren(children, run) {
  if (!children.length) return node('p', 'empty-panel', '当前运行暂无子 Agent。');
  const pane = node('div');
  for (const [index, child] of children.entries()) {
    const id = child.child_id;
    const events = run.events.filter((event) => event.actor === `subagent:${id}`);
    const result = run.events.find((event) => event.type === 'subagent_end' && event.child_id === id);
    const details = keepOpen(node('details', 'child-trace'), run.openChildren, id);
    const summary = node('summary');
    summary.dataset.focus = `child:${id}`;
    summary.append(node('strong', 'child-name', `子 Agent ${index + 1}`), node('span', 'tool-state', result ? '已返回' : run.terminal ? '已结束' : '处理中'));
    const inner = node('div', 'child-inner');
    if (child.task) inner.append(node('p', 'child-task', child.task));
    inner.append(timeline(events, run, id, !result && !run.childRows.get(id)));
    if (result?.result !== undefined) inner.append(detailBlock('子 Agent 结果', result.result));
    details.append(summary, inner);
    pane.append(details);
  }
  return pane;
}

function cardHeading(label, meta) {
  const row = node('div', 'card-heading');
  row.append(node('h3', '', label));
  if (meta) row.append(node('span', 'card-meta', meta));
  return row;
}

function stat(label, value) {
  const cell = node('div', 'context-stat');
  cell.append(node('strong', '', displayNumber(value)), node('span', '', label));
  return cell;
}

function renderContext(events, round) {
  const contexts = events.filter((event) => event.type === 'context');
  const context = contexts.filter((event) => !round || event.round <= Number(round)).at(-1);
  const usage = events.filter((event) => event.type === 'model_result' && (!round || String(event.round) === round)).at(-1);
  const panel = ui['context-panel'];
  const heading = cardHeading('上下文构成', context?.round ? `第 ${context.round} 轮` : '');
  if (!context) {
    panel.replaceChildren(heading, node('p', 'empty-panel', '发送消息后查看模型收到的上下文。'));
    return;
  }
  const stats = node('div', 'context-summary');
  stats.append(stat('条消息', context.messages), stat('估算 Token', context.estimated_tokens));
  const bar = node('div', 'context-bar');
  bar.setAttribute('aria-hidden', 'true');
  const legend = node('div', 'context-segments');
  for (const [index, part] of (context.parts ?? []).entries()) {
    const color = palette[index % palette.length];
    if (part.tokens > 0) {
      const segment = node('span');
      segment.style.flexGrow = String(part.tokens);
      segment.style.background = color;
      bar.append(segment);
    }
    const entry = node('div', 'context-part');
    const chip = node('i');
    chip.style.background = color;
    chip.setAttribute('aria-hidden', 'true');
    const text = Number.isFinite(part.count) ? `${part.label || part.key} · ${part.count} 条` : part.label || part.key;
    entry.append(chip, node('span', '', text), node('b', '', displayNumber(part.tokens)));
    legend.append(entry);
  }
  panel.replaceChildren(heading, stats);
  if (bar.childElementCount) panel.append(bar);
  if (legend.childElementCount) panel.append(legend);
  if (usage) {
    const label = phaseLabels[usage.phase] ?? '执行';
    panel.append(node('div', 'token-usage', `最近模型返回（${label}）　输入 ${displayNumber(usage.input_tokens)} / 输出 ${displayNumber(usage.output_tokens)} Token`));
  }
}

function compactionRow(label, count, tokens, maxTokens, className = '') {
  const row = node('div', `compaction-row ${className}`);
  const title = node('div', 'compaction-label');
  title.append(node('span', '', label), node('b', '', `${displayNumber(count)} 条 · ${displayNumber(tokens)} Token`));
  const track = node('div', 'compaction-track');
  track.setAttribute('aria-hidden', 'true');
  const fill = node('i');
  fill.style.width = `${Number.isFinite(tokens) ? 100 * tokens / maxTokens : 0}%`;
  track.append(fill);
  row.append(title, track);
  return row;
}

function renderCompaction(events, run) {
  const panel = ui['compaction-panel'];
  const wasOpen = panel.querySelector('details')?.open;
  const records = events.filter((event) => event.type === 'compaction');
  const selected = run?.compactionRevision ?? '';
  const item = selected === '' ? records.at(-1) : records[Number(selected)];
  if (!item) {
    panel.replaceChildren(cardHeading('上下文压缩'), node('p', 'empty-panel', '当前运行尚未触发压缩。'));
    return;
  }
  const heading = cardHeading('上下文压缩', records.length === 1 ? '估算 Token' : '');
  if (records.length > 1) {
    const select = choose('选择压缩记录', [['', '最新压缩'], ...records.map((_, i) => [String(i), `压缩 ${i + 1}`])], selected, (value) => {
      run.compactionRevision = value;
      renderInspector();
    });
    select.dataset.focus = 'compaction-revision';
    heading.append(select);
  }
  const max = Math.max(item.before_tokens || 0, item.after_tokens || 0, 1);
  const chart = node('div', 'compaction-chart');
  chart.append(compactionRow('压缩前', item.before, item.before_tokens, max), compactionRow('压缩后', item.after, item.after_tokens, max, 'after'));
  const detail = [];
  if (Number.isFinite(item.kept)) detail.push(`保留最近 ${item.kept} 条`);
  if (Number.isFinite(item.threshold)) detail.push(`阈值 ${item.threshold} 条`);
  preserveFocus(panel, () => {
    panel.replaceChildren(heading, chart);
    if (detail.length) panel.append(node('p', 'token-usage', detail.join(' · ')));
    if (item.summary) {
      const summary = node('details', 'summary-text');
      summary.open = Boolean(wasOpen);
      const title = node('summary', '', '查看压缩摘要');
      title.dataset.focus = 'compaction-summary';
      summary.append(title, node('p', '', item.summary));
      panel.append(summary);
    }
  });
}

function renderInspector() {
  const record = currentSession();
  const runs = record?.runs ?? [];
  const active = record?.active;
  const run = runs.find((item) => item.id === record.selectedRun) ?? runs.at(-1);
  const profile = run?.start?.profile;
  const model = state.config.profiles.find((entry) => entry.id === profile);
  const options = runs.map((item, index) => [item.id, `运行 ${index + 1} · ${modeLabels[item.start?.mode] || displayDate(item.start?.at) || '对话'}`]);
  syncOptions(ui['run-select'], options.length ? options.reverse() : [['', '暂无运行']], run?.id ?? '');
  ui['run-select'].disabled = !runs.length;
  ui['run-select'].title = model?.label || profile || '选择运行';
  const events = actorEvents(run);
  const plans = events.filter((event) => event.type === 'plan');
  const children = events.filter((event) => event.type === 'subagent_start');
  ui['plan-count'].textContent = plans.length || '';
  ui['child-count'].textContent = children.length || '';
  const panel = ui['trace-panel'];
  const oldScroll = new Map([...panel.querySelectorAll('[data-timeline]')].map((timeline) => [timeline.dataset.timeline, timeline.scrollTop]));
  panel.setAttribute('aria-labelledby', `tab-${state.tab}`);
  preserveFocus(panel, () => {
    if (!run) panel.replaceChildren(node('p', 'empty-panel', '运行开始后显示阶段和调用时序。'));
    else if (state.tab === 'plan') panel.replaceChildren(renderPlan(plans, run));
    else if (state.tab === 'child') panel.replaceChildren(renderChildren(children, run));
    else panel.replaceChildren(renderSequence(events, run, active?.run === run && !run.terminal));
  });
  for (const scroll of panel.querySelectorAll('[data-timeline]')) {
    scroll.scrollTop = scroll.dataset.follow ? scroll.scrollHeight : oldScroll.get(scroll.dataset.timeline) ?? 0;
  }
  renderContext(events, run?.round);
  renderCompaction(events, run);
}

function renderWorkspace(reset = false) {
  renderComposer();
  renderChat(reset);
  renderInspector();
}

function updateDraft() {
  const record = currentSession();
  if (record) record.draft = ui.prompt.value;
  else state.draft = ui.prompt.value;
  renderComposer();
}

function selectTab(key, focus = false) {
  state.tab = key;
  for (const tab of document.querySelectorAll('.trace-tabs button')) {
    const selected = tab.dataset.tab === key;
    tab.setAttribute('aria-selected', String(selected));
    tab.tabIndex = selected ? 0 : -1;
    if (focus && selected) tab.focus();
  }
  renderInspector();
}

ui['new-session'].addEventListener('click', async () => {
  const previous = state.selected;
  const record = await createSession(false);
  if (record && state.selected === previous) {
    selectSession(record.session.id);
    ui.prompt.focus();
  }
});
for (const toggle of document.querySelectorAll('.view-nav button')) {
  toggle.addEventListener('click', () => setView(toggle.dataset.view));
}
for (const control of selectable) ui[control].addEventListener('change', settingsChanged);
ui.prompt.addEventListener('input', updateDraft);
ui.prompt.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
    event.preventDefault();
    if (!ui.send.disabled) ui.composer.requestSubmit();
  }
});
ui.composer.addEventListener('submit', send);
ui['run-select'].addEventListener('change', () => {
  const record = currentSession();
  if (record) record.selectedRun = ui['run-select'].value;
  renderInspector();
});
const tabs = [...document.querySelectorAll('.trace-tabs button')];
for (const [index, tab] of tabs.entries()) {
  tab.addEventListener('click', () => selectTab(tab.dataset.tab));
  tab.addEventListener('keydown', (event) => {
    const target = { ArrowLeft: (index + tabs.length - 1) % tabs.length, ArrowRight: (index + 1) % tabs.length, Home: 0, End: tabs.length - 1 }[event.key];
    if (target !== undefined) {
      event.preventDefault();
      selectTab(tabs[target].dataset.tab, true);
    }
  });
}

renderWorkspace();
void boot();
