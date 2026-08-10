'use strict';

const state = {
  config: null,
  pendingOp: null,   // 待确认的危险操作 {op, params}
  pendingShell: null, // 待确认的命令字符串
};

const $ = (sel) => document.querySelector(sel);
const statusOutput = $('#statusOutput');
const opOutput = $('#opOutput');
const shellOutput = $('#shellOutput');

// 所有请求均使用相对路径，使页面可部署在子路径（如 /admin/）下
async function api(path, opts) {
  const res = await fetch(path, opts);
  return res.json();
}

function esc(s) {
  return String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
}

// ---------------------------------------------------------------------------
// 初始化
// ---------------------------------------------------------------------------
async function init() {
  // 先把事件监听绑定好（不依赖 config），避免 config 加载异常导致按钮失效
  $('#refreshLog').addEventListener('click', refreshLog);
  $('#modalCancel').addEventListener('click', closeModal);
  $('#modalConfirm').addEventListener('click', doPendingConfirm);
  $('#copyStatus').addEventListener('click', () => copyOutput(statusOutput, $('#copyStatus')));
  $('#copyOp').addEventListener('click', () => copyOutput(opOutput, $('#copyOp')));
  $('#copyShell').addEventListener('click', () => copyOutput(shellOutput, $('#copyShell')));

  // 左侧导航切换
  document.querySelectorAll('.nav-item').forEach(btn => {
    btn.addEventListener('click', () => switchNav(btn.dataset.view));
  });
  // 命令操作
  $('#shellRun').addEventListener('click', onShellRun);

  let cfg;
  try {
    cfg = await api('api/config');
  } catch (e) {
    opOutput.textContent = '加载配置失败: ' + e + '\n请刷新页面重试。';
    return;
  }
  state.config = cfg;

  $('#env').innerHTML =
    `Pigsty: <b>${esc(cfg.pigsty_home)}</b><br>` +
    `监听: <b>${esc(cfg.host)}:${cfg.port}</b><br>` +
    `危险操作: <b style="color:${cfg.danger_enabled?'var(--danger)':'var(--muted)'}">` +
    `${cfg.danger_enabled ? '已启用' : '已禁用（只读）'}</b>`;

  const hint = cfg.danger_enabled
    ? '（需要二次确认）' : '（当前为只读模式，需 ADMIN_DANGER=1 启动）';
  $('#dangerHint').textContent = hint;
  $('#shellHint').textContent = hint;
  if (!cfg.danger_enabled) {
    $('#shellCmd').disabled = true;
    $('#shellRun').disabled = true;
    $('#shellCmd').placeholder = '危险操作已禁用，请以 ADMIN_DANGER=1 启动服务';
  }

  renderStatus(cfg.status || []);
  renderOps(cfg.ops || []);
  refreshLog();
}

// ---------------------------------------------------------------------------
// 左侧导航切换
// ---------------------------------------------------------------------------
function switchNav(view) {
  document.querySelectorAll('.nav-item').forEach(b => {
    b.classList.toggle('active', b.dataset.view === view);
  });
  document.querySelectorAll('.view').forEach(v => {
    v.classList.toggle('hidden', v.id !== 'view-' + view);
  });
}

// ---------------------------------------------------------------------------
// 状态查看（只读）
// ---------------------------------------------------------------------------
function renderStatus(statusList) {
  const grid = $('#statusGrid');
  grid.innerHTML = '';
  statusList.forEach(s => {
    const b = document.createElement('button');
    b.className = 'btn';
    b.textContent = s.name;
    b.onclick = () => runStatus(s);
    grid.appendChild(b);
  });
}

async function runStatus(s) {
  statusOutput.textContent = '加载中...';
  const r = await api('api/status/' + s.id);
  statusOutput.textContent = (r.text || '(空)') +
    (r.error ? '\n\n[错误] ' + r.error : '');
}

async function copyOutput(target, btn) {
  const text = target.textContent || '';
  if (!text.trim()) return;
  try {
    await navigator.clipboard.writeText(text);
  } catch (e) {
    // 回退方案：execCommand
    const ta = document.createElement('textarea');
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
  }
  const old = btn.textContent;
  btn.textContent = '✓ 已复制';
  btn.classList.add('copied');
  setTimeout(() => { btn.textContent = old; btn.classList.remove('copied'); }, 1500);
}

// ---------------------------------------------------------------------------
// 运维操作（危险）
// ---------------------------------------------------------------------------
function renderOps(ops) {
  const grid = $('#opsGrid');
  grid.innerHTML = '';
  if (!state.config.danger_enabled) {
    const tip = document.createElement('p');
    tip.className = 'hint';
    tip.style.padding = '6px 0';
    tip.textContent = '危险操作已禁用。请以 ADMIN_DANGER=1 启动本服务后再使用以下操作。';
    grid.appendChild(tip);
  }
  ops.forEach(o => {
    const card = document.createElement('div');
    card.className = 'op-card';
    let fieldsHtml = '';
    (o.fields || []).forEach(f => {
      fieldsHtml += `<div class="field">
        <label>${esc(f.label)}${f.required ? ' *' : ''}</label>
        <input type="text" data-key="${esc(f.key)}" placeholder="${esc(f.label)}">
      </div>`;
    });
    card.innerHTML = `<h4>${esc(o.name)}</h4>${fieldsHtml}
      <button class="btn danger" style="width:100%">执行</button>`;
    const btn = card.querySelector('button');
    btn.disabled = !state.config.danger_enabled;
    btn.onclick = () => {
      const params = {};
      card.querySelectorAll('input[data-key]').forEach(inp => {
        params[inp.dataset.key] = inp.value;
      });
      openModal(o, params);
    };
    grid.appendChild(card);
  });
}

function openModal(op, params) {
  state.pendingOp = { op, params };
  const desc = `即将执行：\n${op.name}\n` +
    Object.entries(params).filter(([, v]) => v).map(([k, v]) => `  ${k} = ${v}`).join('\n');
  $('#modalDesc').textContent = desc + '\n\n该操作会调用 Ansible Playbook，请在确认无误后点击「确认执行」。';
  $('#modal').hidden = false;
}

function closeModal() {
  $('#modal').hidden = true;
  state.pendingOp = null;
  state.pendingShell = null;
}

async function doPendingConfirm() {
  if (state.pendingShell !== null) {
    return doShellExec(state.pendingShell);
  }
  if (!state.pendingOp) return closeModal();
  const { op, params } = state.pendingOp;
  $('#modal').hidden = true;
  opOutput.textContent = `执行中: ${op.name} ...\n`;
  const r = await api('api/op/' + op.id, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  });
  opOutput.textContent = `[返回码 ${r.rc}]\n` + (r.text || '(无输出)') +
    (r.error ? '\n\n[错误] ' + r.error : '');
  state.pendingOp = null;
  refreshLog();
}

// ---------------------------------------------------------------------------
// 命令操作（危险）
// ---------------------------------------------------------------------------
function onShellRun() {
  const cmd = $('#shellCmd').value.trim();
  if (!cmd) return;
  state.pendingShell = cmd;
  $('#modalDesc').textContent =
    `即将执行命令：\n${cmd}\n\n该命令会直接在服务器上运行，请在确认无误后点击「确认执行」。\n（仅允许 Pigsty 相关命令，禁止管道/重定向/复合命令）`;
  $('#modal').hidden = false;
}

async function doShellExec(cmd) {
  $('#modal').hidden = true;
  shellOutput.textContent = `执行中: ${cmd}\n（耗时可能较长，请稍候）...\n`;
  $('#shellRun').disabled = true;
  try {
    const r = await api('api/shell', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ command: cmd }),
    });
    shellOutput.textContent = `[返回码 ${r.rc}]\n` + (r.text || '(无输出)') +
      (r.error ? '\n\n[错误] ' + r.error : '');
  } catch (e) {
    shellOutput.textContent = '请求失败: ' + e;
  } finally {
    $('#shellRun').disabled = false;
    state.pendingShell = null;
    refreshLog();
  }
}

// ---------------------------------------------------------------------------
// 日志
// ---------------------------------------------------------------------------
async function refreshLog() {
  const r = await api('api/log');
  const tbody = $('#logTable tbody');
  tbody.innerHTML = '';
  (r.log || []).forEach(row => {
    const tr = document.createElement('tr');
    const badge = row.kind === 'status'
      ? `<span class="badge status">查看</span>`
      : (row.ok ? `<span class="badge ok">成功</span>` : `<span class="badge fail">失败</span>`);
    tr.innerHTML = `<td>${esc(row.ts)}</td><td>${esc(row.kind)}</td>` +
      `<td>${esc(row.cmd)}</td><td>${badge}</td>`;
    tbody.appendChild(tr);
  });
}

init();
