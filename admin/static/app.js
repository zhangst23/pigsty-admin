'use strict';

const state = {
  config: null,
  pendingOp: null,   // 待确认的危险操作 {op, params}
};

const $ = (sel) => document.querySelector(sel);
const statusOutput = $('#statusOutput');
const opOutput = $('#opOutput');

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
  const cfg = await api('/api/config');
  state.config = cfg;

  $('#env').innerHTML =
    `Pigsty: <b>${esc(cfg.pigsty_home)}</b><br>` +
    `监听: <b>${esc(cfg.host)}:${cfg.port}</b><br>` +
    `危险操作: <b style="color:${cfg.danger_enabled?'var(--danger)':'var(--muted)'}">` +
    `${cfg.danger_enabled ? '已启用' : '已禁用（只读）'}</b>`;

  $('#dangerHint').textContent = cfg.danger_enabled
    ? '（需要二次确认）' : '（当前为只读模式，需 ADMIN_DANGER=1 启动）';

  renderStatus(cfg.status);
  renderOps(cfg.ops);
  refreshLog();

  $('#refreshLog').addEventListener('click', refreshLog);
  $('#modalCancel').addEventListener('click', closeModal);
  $('#modalConfirm').addEventListener('click', doPendingOp);
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
  const r = await api('/api/status/' + s.id);
  statusOutput.textContent = (r.text || '(空)') +
    (r.error ? '\n\n[错误] ' + r.error : '');
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
}

async function doPendingOp() {
  if (!state.pendingOp) return closeModal();
  const { op, params } = state.pendingOp;
  $('#modal').hidden = true;
  opOutput.textContent = `执行中: ${op.name} ...\n`;
  const r = await api('/api/op/' + op.id, {
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
// 日志
// ---------------------------------------------------------------------------
async function refreshLog() {
  const r = await api('/api/log');
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
