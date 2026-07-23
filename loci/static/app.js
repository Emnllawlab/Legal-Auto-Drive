'use strict';

// ── Helpers ───────────────────────────────────────────────────
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  return res.json();
}

function fmtBytes(b) {
  if (b < 1024)     return b + ' B';
  if (b < 1048576)  return (b / 1024).toFixed(0) + ' KB';
  return (b / 1048576).toFixed(1) + ' MB';
}

// ── Toast ─────────────────────────────────────────────────────
let _toastTimer;
function toast(msg, err = false) {
  const el = $('toast');
  el.textContent = msg;
  el.style.background = err ? 'var(--red)' : 'var(--text)';
  el.classList.add('show');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => el.classList.remove('show'), 2600);
}

// ── Modal ─────────────────────────────────────────────────────
function showModal(title, body) {
  $('modal-title').textContent = title;
  $('modal-body').textContent  = body;
  $('overlay').classList.add('open');
}
function closeModal() { $('overlay').classList.remove('open'); }

// ── Navigation ─────────────────────────────────────────────────
let _currentSec = 'dashboard';
let _logTimer   = null;

function nav(name) {
  document.querySelectorAll('.sec').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));

  const sec = $('sec-' + name);
  if (sec) sec.classList.add('active');
  const btn = document.querySelector(`[data-sec="${name}"]`);
  if (btn) btn.classList.add('active');

  _currentSec = name;
  clearInterval(_logTimer);

  if (name === 'dashboard') loadDashboard();
  if (name === 'inbox')     loadInbox();
  if (name === 'cases')     loadCases();
  if (name === 'log')       startLogPoll();
  if (name === 'rules')     loadRules();
  if (name === 'review')    loadReview();
  if (name === 'desk')      loadDesk();
  if (name === 'new-case')  renderIntake();
}

// ── Log rendering ──────────────────────────────────────────────
function renderLogLine(line) {
  const ok   = /✅|→ /.test(line) && !/⚠/.test(line);
  const warn = /⚠|⚡/.test(line);
  const err  = /❌/.test(line);
  const cls  = err ? 'err' : warn ? 'warn' : ok ? 'ok' : '';

  const m    = line.match(/\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s*\w+\s*\|?\s*/);
  const time = m ? m[1].slice(5, 16) : '';
  const text = m ? line.slice(m[0].length) : line;

  return `<div class="log-line ${cls}"><span class="log-t">${esc(time)}</span><span>${esc(text)}</span></div>`;
}

// ── Dashboard ──────────────────────────────────────────────────
async function loadDashboard() {
  const [st, lg] = await Promise.all([api('/api/status'), api('/api/log')]);

  // sidebar pill
  const pill  = $('sidebar-daemon');
  const label = $('sidebar-daemon-label');
  if (st.daemon.running) {
    pill.querySelector('.dot').className  = 'dot dot-green';
    label.textContent = `PID ${st.daemon.pid}`;
  } else {
    pill.querySelector('.dot').className  = 'dot dot-gray';
    label.textContent = '守护进程';
  }

  // stat cards
  const daemonEl  = $('stat-daemon');
  const daemonBtn = $('btn-daemon');
  if (st.daemon.running) {
    daemonEl.innerHTML = `<span class="dot dot-green"></span> 运行中 <span style="color:var(--faint);font-size:11px;font-family:var(--mono)">PID ${st.daemon.pid}</span>`;
    daemonBtn.textContent = '停止';
    daemonBtn.className   = 'btn btn-xs btn-danger mt8';
  } else {
    daemonEl.innerHTML = `<span class="dot dot-gray"></span> 未运行`;
    daemonBtn.textContent = '启动';
    daemonBtn.className   = 'btn btn-xs btn-ghost mt8';
  }

  const n = st.inbox_count;
  $('stat-inbox').textContent = n ? `${n} 个待处理` : '空';
  $('stat-inbox-path').textContent = st.paths.inbox;
  $('stat-cases').textContent = `${st.case_count} 个`;
  $('stat-cases-path').textContent = st.paths.cases;

  // badge
  const badge = $('nav-badge');
  const caseCount = $('nav-case-count');
  if (n > 0) { badge.textContent = n; badge.style.display = ''; }
  else          badge.style.display = 'none';
  caseCount.textContent = st.case_count || '';

  // recent log
  const panel = $('dash-log');
  const lines = lg.lines.slice(-40).reverse();
  panel.innerHTML = lines.length
    ? lines.map(renderLogLine).join('')
    : '<div class="empty">暂无活动记录</div>';
}

// ── Upload to inbox ────────────────────────────────────────────
async function uploadToInbox(files, meta, metas) {
  toast(`上传中… ${files.length} 个文件`);
  const form = new FormData();
  files.forEach(f => form.append('files', f));
  if (metas && metas.length)               form.append('metas', JSON.stringify(metas));
  else if (meta && Object.keys(meta).length) form.append('meta', JSON.stringify(meta));
  try {
    const res  = await fetch('/api/inbox/upload', { method: 'POST', body: form });
    const data = await res.json();
    if (data.ok) {
      toast(`✓ 已上传 ${data.count} 个文件到收件箱`);
      if (_currentSec === 'inbox') loadInbox();
      else                         loadDashboard();
    } else {
      toast('上传失败', true);
    }
  } catch (e) {
    toast('上传失败: ' + e.message, true);
  }
}

// ── Pre-fill (投放后人工预分类) ─────────────────────────────────
let _pendingFiles = [];
let _meta = { cases: [], targets: [] };

async function loadMeta() {
  try { _meta = await api('/api/meta'); }
  catch { _meta = { cases: [], targets: [] }; }
}

// 案件场景 → 预填里的国家/程序（仅用于联动显示）
const _SCEN_UI = {
  '中国民事': { country: 'cn', proc: 'civil' },
  '仲裁':     { country: 'cn', proc: 'arb' },
  '中国刑事': { country: 'cn', proc: 'criminal' },
  '美国民事': { country: 'us', proc: 'us' },
  '刑民交叉': { country: 'cn', proc: 'civil' },
};

function targetScenario(t) {
  if (t.startsWith('US_')) return { country: 'us', proc: 'us' };
  if (t.startsWith('CR_')) return { country: 'cn', proc: 'criminal' };
  if (t.includes('仲裁') || t.includes('裁决')) return { country: 'cn', proc: 'arb' };
  return { country: 'cn', proc: 'civil' };
}

// 未选案件时的兜底：用规则 target（按场景过滤）
function populatePfTargets() {
  const country = $('pf-country').value;
  const proc    = $('pf-proc').value;
  let ts = _meta.targets || [];
  if (proc)         ts = ts.filter(t => targetScenario(t).proc === proc);
  else if (country) ts = ts.filter(t => targetScenario(t).country === country);
  renderTargetOptions(ts, '（先选案件可列出完整文件夹）');
}

// 把目标文件夹列表渲染成"按顶层分组"的下拉
function renderTargetOptions(folders, placeholder) {
  const sel = $('pf-target');
  const cur = sel.value;
  const groups = {};
  (folders || []).forEach(f => {
    const top = f.split('/')[0];
    (groups[top] = groups[top] || []).push(f);
  });
  let html = `<option value="">（自动识别 / 不指定）</option>`;
  Object.keys(groups).forEach(top => {
    html += `<optgroup label="${esc(top)}">`;
    groups[top].forEach(f => {
      const indent = '  '.repeat(Math.max(0, f.split('/').length - 1));
      html += `<option value="${esc(f)}">${indent}${esc(f.split('/').slice(1).join('/') || f)}</option>`;
    });
    html += '</optgroup>';
  });
  sel.innerHTML = html;
  if (cur && (folders || []).includes(cur)) sel.value = cur;
  $('pf-target-hint').textContent = (folders || []).length
    ? `共 ${folders.length} 个文件夹` : (placeholder || '');
}

async function loadCaseFolders(key) {
  if (!key) { populatePfTargets(); return; }
  try {
    const r = await api(`/api/cases/${encodeURIComponent(key)}/folders`);
    if (r && r.folders && r.folders.length) renderTargetOptions(r.folders);
    else populatePfTargets();
  } catch { populatePfTargets(); }
}

// 法院/仲裁机构名缩写（进文件名用），与后端 _court_abbr 保持一致
function courtAbbr(full) {
  let s = (full || '').replace(/人民法院$/, '法院').replace(/人民检察院$/, '检察院');
  s = s.replace(/^(北京市|上海市|天津市|重庆市)/, '');
  s = s.replace(/^.{2,3}省/, '');
  s = s.replace(/^.{2,4}(自治区|特别行政区)/, '');
  return s.trim() || full;
}

// 来源主体下拉：当事人(我方/对方/第三人) + 法院/仲裁机构，均进文件名
const _PARTY_SIDE = { party_us: '我方', party_op: '对方', party_third: '第三人' };
function populatePfParties(key) {
  const c = (_meta.cases || []).find(x => x.key === key);
  let html = '<option value="">（不指定）</option>';
  if (c) {
    ['party_us', 'party_op', 'party_third'].forEach(side => {
      const list = c[side] || [];
      if (list.length) {
        html += `<optgroup label="${_PARTY_SIDE[side]}">`;
        list.forEach(p => { html += `<option value="${esc(p)}">${esc(p)}</option>`; });
        html += '</optgroup>';
      }
    });
    const courts = c.party_court || [];
    if (courts.length) {
      html += `<optgroup label="法院 / 仲裁机构">`;
      courts.forEach(full => {
        const ab = courtAbbr(full);
        html += `<option value="${esc(ab)}" title="${esc(full)}">${esc(ab)}</option>`;
      });
      html += '</optgroup>';
    }
  }
  $('pf-party').innerHTML = html;
}

// 选了主体 → 自动带出来源（我方/对方/法院）
function partySideOf(key, party) {
  const c = (_meta.cases || []).find(x => x.key === key);
  if (!c) return '';
  if ((c.party_us || []).includes(party))                  return 'ours';
  if ((c.party_op || []).includes(party))                  return 'opposing';
  if ((c.party_court || []).some(e => courtAbbr(e) === party)) return 'court';
  return '';
}

// 每个文件单独的逐项预填值（target/doc_type/party/batch/source/version/notes）
let _pfPer = [];
let _pfIdx = 0;

// 逐项字段（每文件各一份）。案件/国家/程序/日期为整批共享。
const _PF_PER_FIELDS = {
  'pf-target': 'target', 'pf-docname': 'doc_type', 'pf-party': 'party',
  'pf-batch': 'batch', 'pf-source': 'source', 'pf-version': 'version', 'pf-notes': 'notes',
};

function savePfPer(i) {
  const m = {};
  for (const [id, key] of Object.entries(_PF_PER_FIELDS)) {
    const v = $(id).value; if (v && v.trim && v.trim()) m[key] = v.trim(); else if (v) m[key] = v;
  }
  if ($('pf-translation').checked) m.translation = true;
  const also = [...$('pf-also').selectedOptions].map(o => o.value).filter(Boolean);
  if (also.length) m.also_cases = also;
  _pfPer[i] = m;
}
function loadPfPer(i) {
  const m = _pfPer[i] || {};
  for (const [id, key] of Object.entries(_PF_PER_FIELDS)) $(id).value = m[key] || '';
  $('pf-translation').checked = !!m.translation;
  [...$('pf-also').options].forEach(o => o.selected = (m.also_cases || []).includes(o.value));
}
function populatePfAlso(excludeKey) {
  $('pf-also').innerHTML = (_meta.cases || []).filter(c => c.key !== excludeKey)
    .map(c => `<option value="${esc(c.key)}">${esc(c.key)} · ${esc(c.name)}</option>`).join('');
}
function pfFilled(i) { return _pfPer[i] && Object.keys(_pfPer[i]).length > 0; }

function renderPfFiles() {
  $('prefill-files').innerHTML = _pendingFiles.map((f, i) =>
    `<div class="pf-file${i === _pfIdx ? ' active' : ''}" onclick="switchPfFile(${i})">
       ${i === _pfIdx ? '▸ ' : '　'}📄 ${esc(f.name)}${pfFilled(i) ? ' <span class="pf-done">✓</span>' : ''}
     </div>`).join('');
}
function switchPfFile(i) {
  if (i === _pfIdx) return;
  savePfPer(_pfIdx);
  _pfIdx = i;
  loadPfPer(i);
  renderPfFiles();
}
function copyPfToAll() {
  savePfPer(_pfIdx);
  const m = { ..._pfPer[_pfIdx] };
  _pfPer = _pendingFiles.map(() => ({ ...m }));
  renderPfFiles();
  toast(`已复制到全部 ${_pendingFiles.length} 个文件`);
}

async function openPrefill(files) {
  _pendingFiles = files;
  _pfPer = files.map(() => ({}));
  _pfIdx = 0;
  if (!(_meta.cases || []).length && !(_meta.targets || []).length) await loadMeta();

  $('pf-case').innerHTML = '<option value="">（自动识别）</option>' +
    (_meta.cases || []).map(c => `<option value="${esc(c.key)}">${esc(c.key)} · ${esc(c.name)}</option>`).join('');
  $('pf-case').value = '';
  $('pf-country').value = '';
  $('pf-proc').value = '';
  const _pfDefaultDate = files[0]?.lastModified
    ? new Date(files[0].lastModified).toISOString().slice(0, 10)
    : new Date().toISOString().slice(0, 10);
  $('pf-date').value = _pfDefaultDate;
  populatePfTargets();
  populatePfParties('');
  populatePfAlso('');
  loadPfPer(0);                  // 清空逐项字段
  $('pf-multi').style.display = files.length > 1 ? '' : 'none';
  renderPfFiles();

  $('prefill-overlay').classList.add('open');
}

function confirmPrefill() {
  savePfPer(_pfIdx);
  const sharedCase = $('pf-case').value;
  const sharedDate = $('pf-date').value;
  const metas = _pendingFiles.map((f, i) => {
    const p = _pfPer[i] || {}, m = {};
    if (sharedCase)  m.case_key = sharedCase;
    if (sharedDate)  m.date     = sharedDate;
    if (p.target)    m.target   = p.target;
    if (p.doc_type)  m.doc_type = p.doc_type;
    if (p.party)     m.party    = p.party;
    if (p.batch)     m.batch    = parseInt(p.batch);
    if (p.source)    m.source   = p.source;
    if (p.version)   m.version  = p.version;
    if (p.notes)     m.notes    = p.notes;
    if (p.translation) m.translation = true;
    if (p.also_cases && p.also_cases.length) m.also_cases = p.also_cases;
    return m;
  });
  const files = _pendingFiles;
  closePrefill();
  if (files.length) uploadToInbox(files, null, metas);
}

function skipPrefill() {
  const files = _pendingFiles;
  closePrefill();
  if (files.length) uploadToInbox(files, null);
}

function closePrefill() {
  $('prefill-overlay').classList.remove('open');
  _pendingFiles = [];
}

// ── Inbox ──────────────────────────────────────────────────────
async function loadInbox() {
  const data = await api('/api/inbox');
  const list = $('inbox-list');
  if (!data.files.length) {
    list.innerHTML = `<div class="inbox-empty-drop">
      <div class="inbox-drop-icon">📥</div>
      <div>收件箱为空</div>
      <div class="inbox-drop-hint">将文件拖放到窗口任意位置，或点击"上传文件"</div>
    </div>`;
    return;
  }
  list.innerHTML = data.files.map(f => `
    <div class="file-row">
      <span class="file-ext">${esc(f.ext.slice(0,4))}</span>
      <span class="file-name">${esc(f.name)}</span>
      <span class="file-meta">${esc(f.mtime)}</span>
      <span class="file-meta">${fmtBytes(f.size)}</span>
    </div>`).join('');
}

// ── Cases ──────────────────────────────────────────────────────
let _casesData = [];

async function loadCases() {
  const data = await api('/api/cases');
  _casesData = data.cases || [];
  const list = $('cases-list');
  if (!_casesData.length) {
    list.innerHTML = '<div class="empty">尚无案件 — 点击「新建案件」开始</div>';
    return;
  }
  list.innerHTML = _casesData.map((c, i) => {
    const tags = (c.keywords || []).slice(0, 3)
      .map(k => `<span class="tag">${esc(k)}</span>`).join('');
    const pathHint = c.cases_dir
      ? `<span class="tag" style="color:var(--blue);border-color:#bfdbfe">${esc(c.cases_dir.split('/').pop())}</span>`
      : '';
    const numBadge = c.case_number
      ? `<span class="case-number">${esc(c.case_number)}</span>` : '';
    const closed = c.status === 'closed';
    const closedTag = closed ? `<span class="case-closed-tag">已关闭</span>` : '';
    return `<div class="case-row${closed ? ' case-closed' : ''}">
      <span class="case-key">${esc(c.key)}</span>
      <span class="case-name">${esc(c.name)}</span>${numBadge}${closedTag}
      <div class="case-tags">${tags}${pathHint}</div>
      <div class="rule-actions">
        <button class="icon-btn" onclick="openCaseFolderDirect(${i})" title="在 Finder 中打开">打开</button>
        <button class="icon-btn" onclick="showFileTree(${i})" title="查看材料清单">材料</button>
        <button class="icon-btn" onclick="desensitizeCase(${i})" title="整案脱敏 → 生成脱敏版案件空间">脱敏</button>
        <button class="icon-btn" onclick="openCaseEditor(${i})" title="编辑案件信息">编辑</button>
        <button class="icon-btn" onclick="toggleCaseStatus(${i})" title="${closed ? '重新激活' : '关闭案件（停止自动归档）'}">${closed ? '激活' : '关闭'}</button>
      </div>
    </div>`;
  }).join('');
}

// ── 整案脱敏 → 脱敏版案件空间 ──────────────────────────────────
let _desensTimer = null;

async function desensitizeCase(idx) {
  const c = _casesData[idx];
  if (!confirm(`将对案件「${c.name}」整案脱敏，生成「脱敏版」到 cases_脱敏/${c.name}/（归类层次一致、全案化名一致）。\n大案可能需数分钟，确定开始？`)) return;
  const res = await api(`/api/cases/${encodeURIComponent(c.key)}/desensitize`, { method: 'POST' });
  if (!res.ok) { toast('✗ ' + res.error, true); return; }
  toast('脱敏已开始…');
  clearInterval(_desensTimer);
  _desensTimer = setInterval(async () => {
    const s = await api('/api/desensitize/status');
    if (s.running) {
      toast(`脱敏中 ${s.i}/${s.total}… ${s.current.split('/').pop()}`);
    } else if (s.done) {
      clearInterval(_desensTimer);
      if (s.error) { toast('✗ 脱敏失败：' + s.error, true); return; }
      const r = s.report || {};
      toast(`✓ 完成：处理 ${r.processed} 份 · 命中 ${r.hits} · 实体 ${r.entities} → cases_脱敏/${s.case}`);
    }
  }, 1500);
}

// ── Case folder & file tree ────────────────────────────────────
let _ftKey = null;

async function toggleCaseStatus(idx) {
  const c = _casesData[idx];
  const closing = c.status !== 'closed';
  if (closing && !confirm(`关闭案件「${c.key}」？关闭后不再自动归档（手工仍可指定）。`)) return;
  const res = await api('/api/cases/status', {
    method: 'POST',
    body: JSON.stringify({ key: c.key, status: closing ? 'closed' : 'active' }),
  });
  if (res.ok) { toast(closing ? '✓ 案件已关闭' : '✓ 案件已激活'); loadCases(); }
  else        toast(res.error || '失败', true);
}

async function openCaseFolderDirect(idx) {
  const c = _casesData[idx];
  const res = await api('/api/cases/open', {
    method: 'POST',
    body: JSON.stringify({ key: c.key }),
  });
  if (!res.ok) toast(res.error || '文件夹不存在', true);
}

async function showFileTree(idx) {
  const c = _casesData[idx];
  _ftKey = c.key;
  $('ft-case-name').textContent = c.name;
  $('ft-path').textContent = '加载中…';
  $('ft-total').textContent = '';
  $('filetree-body').innerHTML = '<div class="ft-loading">加载中…</div>';
  $('filetree-overlay').classList.add('open');

  const data = await api(`/api/cases/${encodeURIComponent(c.key)}/files`);

  if (!data.ok) {
    $('filetree-body').innerHTML = `<div class="ft-noexist">${esc(data.error || '加载失败')}</div>`;
    return;
  }
  if (!data.exists) {
    $('ft-path').textContent = data.path;
    $('filetree-body').innerHTML = `<div class="ft-noexist">文件夹尚不存在<br><span style="font-size:11px;font-family:var(--mono)">${esc(data.path)}</span></div>`;
    return;
  }

  $('ft-path').textContent = data.path;
  $('ft-total').textContent = `共 ${data.total_files} 个文件`;
  $('filetree-body').innerHTML = data.tree.length
    ? renderTree(data.tree, 0)
    : '<div class="ft-noexist">文件夹为空</div>';
}

function countFiles(node) {
  let n = 0;
  for (const c of node.children || [])
    n += c.type === 'file' ? 1 : countFiles(c);
  return n;
}

function renderTree(nodes, depth) {
  return nodes.map(n => {
    if (n.type === 'dir') {
      const count  = countFiles(n);
      const isEmpty = count === 0;
      const startCollapsed = depth >= 1;
      const childHtml = n.children.length ? renderTree(n.children, depth + 1) : '';
      return `<div class="ft-node ft-dir${isEmpty ? ' ft-empty-dir' : ''}${startCollapsed ? ' ft-collapsed' : ''}" style="--d:${depth}">
        <div class="ft-label" style="padding-left:${16 + depth * 20}px" onclick="toggleDir(this)">
          <span class="ft-toggle">${startCollapsed ? '▸' : '▾'}</span>
          <span class="ft-icon">${isEmpty ? '📂' : '📁'}</span>
          <span class="ft-name">${esc(n.name)}</span>
          ${count ? `<span class="ft-count">${count}</span>` : ''}
        </div>
        <div class="ft-children" style="${startCollapsed ? 'display:none' : ''}">${childHtml}</div>
      </div>`;
    } else {
      return `<div class="ft-node ft-file">
        <div class="ft-label" style="padding-left:${16 + depth * 20 + 15}px">
          <span class="ft-icon">${fileIcon(n.name)}</span>
          <span class="ft-name">${esc(n.name)}</span>
          <span class="ft-meta">${n.mtime}</span>
          <span class="ft-meta">${fmtBytes(n.size)}</span>
        </div>
      </div>`;
    }
  }).join('');
}

function fileIcon(name) {
  const ext = name.split('.').pop().toLowerCase();
  if (['pdf'].includes(ext))                           return '📕';
  if (['doc', 'docx'].includes(ext))                  return '📝';
  if (['xls', 'xlsx'].includes(ext))                  return '📊';
  if (['ppt', 'pptx'].includes(ext))                  return '📋';
  if (['jpg', 'jpeg', 'png', 'gif', 'tiff'].includes(ext)) return '🖼';
  if (['mp3', 'wav', 'm4a', 'aac'].includes(ext))     return '🎵';
  if (['mp4', 'mov', 'avi', 'mkv'].includes(ext))     return '🎬';
  if (['zip', 'rar', '7z', 'tar'].includes(ext))      return '📦';
  return '📄';
}

function toggleDir(labelEl) {
  const node     = labelEl.parentElement;
  const children = node.querySelector('.ft-children');
  const toggle   = labelEl.querySelector('.ft-toggle');
  const collapsed = node.classList.contains('ft-collapsed');
  node.classList.toggle('ft-collapsed', !collapsed);
  children.style.display = collapsed ? '' : 'none';
  toggle.textContent     = collapsed ? '▾' : '▸';
}

function closeFileTree() {
  $('filetree-overlay').classList.remove('open');
  _ftKey = null;
}

// ── Case editor ─────────────────────────────────────────────────
let _editCaseKw = [];

async function openCaseEditor(idx) {
  const key = _casesData[idx]?.key;
  await loadCases();                        // 打开前先刷新，保证显示磁盘最新数据
  const c = _casesData.find(x => x.key === key);
  if (!c) return;
  $('ce-key').value         = c.key;
  $('ce-name').textContent  = c.name;
  $('ce-number').value      = c.case_number || '';
  $('ce-scenario').value    = c.scenario    || '';
  $('ce-path').value        = c.cases_dir   || '';
  $('ce-party-us').value    = (c.party_us    || []).join(', ');
  $('ce-party-op').value    = (c.party_op    || []).join(', ');
  $('ce-party-third').value = (c.party_third || []).join(', ');
  $('ce-party-court').value = (c.party_court || []).join(', ');
  _editCaseKw = [...(c.keywords || [])];
  renderCaseKwTags();
  $('case-overlay').classList.add('open');
}

function renderCaseKwTags() {
  const empty = '<span style="font-size:11px;color:var(--faint);padding:2px 4px">尚无关键词</span>';
  $('ce-kw-tags').innerHTML = _editCaseKw.length
    ? _editCaseKw.map((k, i) =>
        `<span class="kw-tag">${esc(k)}<button class="kw-tag-x" onclick="removeCaseKw(${i})">×</button></span>`
      ).join('')
    : empty;
}

function addCaseKw() {
  const vals = $('ce-kw-input').value.split(/[,，\s]+/).map(s => s.trim()).filter(Boolean);
  vals.forEach(v => { if (!_editCaseKw.includes(v)) _editCaseKw.push(v); });
  $('ce-kw-input').value = '';
  renderCaseKwTags();
}

function removeCaseKw(idx) {
  _editCaseKw.splice(idx, 1);
  renderCaseKwTags();
}

async function confirmCase() {
  const pending = $('ce-kw-input').value.trim();
  if (pending) { $('ce-kw-input').value = pending; addCaseKw(); }

  toast('保存中…');
  const splitParties = id => $(id).value.split(/[,，;；、\s]+/).map(s => s.trim()).filter(Boolean);
  const res = await api('/api/cases/update', {
    method: 'POST',
    body: JSON.stringify({
      key:         $('ce-key').value,
      keywords:    _editCaseKw,
      case_number: $('ce-number').value.trim(),
      scenario:    $('ce-scenario').value,
      cases_dir:   $('ce-path').value.trim(),
      party_us:    splitParties('ce-party-us'),
      party_op:    splitParties('ce-party-op'),
      party_third: splitParties('ce-party-third'),
      party_court: splitParties('ce-party-court'),
    }),
  });
  if (res.ok) {
    toast('✓ 案件已更新');
    closeCaseModal();
    loadCases();
  } else {
    toast('保存失败: ' + (res.error || ''), true);
  }
}

function closeCaseModal() {
  $('case-overlay').classList.remove('open');
}

// ── Log polling ────────────────────────────────────────────────
let _logLastLen = 0;

function startLogPoll() {
  _logLastLen = 0;
  pollLog();
  _logTimer = setInterval(pollLog, 2500);
}

async function pollLog() {
  const data  = await api('/api/log');
  const panel = $('log-panel');
  const lines = data.lines;

  if (lines.length === _logLastLen) return;
  _logLastLen = lines.length;

  panel.innerHTML = lines.map(renderLogLine).join('') || '<div class="empty">暂无日志</div>';

  const follow = $('log-follow');
  if (follow && follow.checked) panel.scrollTop = panel.scrollHeight;
}

// ── Process inbox ──────────────────────────────────────────────
async function runOnce(dry) {
  toast(dry ? '试运行中…' : '归档处理中…');
  const data = await api('/api/once', { method: 'POST', body: JSON.stringify({ dry_run: dry }) });
  showModal(dry ? '试运行结果' : '处理结果', data.output || '（无输出）');
  loadDashboard();
}

// ── Intake form ────────────────────────────────────────────────
const PROC_OPTS = [
  { v: 'civil',       l: '中国民事诉讼' },
  { v: 'arbitration', l: '仲裁（国内 / 国际）' },
  { v: 'criminal',    l: '中国刑事程序' },
  { v: 'us_civil',    l: '美国民事诉讼\nFederal / State Court' },
  { v: 'mixed',       l: '刑民交叉' },
];

const ROLE_OPTS = {
  civil:       [{ v:'plaintiff',   l:'原告方' },
                { v:'defendant',   l:'被告方（含反诉）' },
                { v:'third-party', l:'第三人' }],
  us_civil:    [{ v:'plaintiff',   l:'Plaintiff / 原告方' },
                { v:'defendant',   l:'Defendant / 被告方' },
                { v:'third-party', l:'Third-party / 第三人' }],
  arbitration: [{ v:'claimant',    l:'仲裁申请人' },
                { v:'respondent',  l:'仲裁被申请人（含反请求）' }],
  criminal:    [{ v:'suspect',     l:'犯罪嫌疑人/被告人方\n（辩护）' },
                { v:'complainant', l:'被害人/报案人方\n（代理）' }],
  mixed:       [{ v:'plaintiff+suspect',     l:'民事原告 + 刑事嫌疑人' },
                { v:'defendant+suspect',     l:'民事被告 + 刑事嫌疑人' },
                { v:'plaintiff+complainant', l:'民事原告 + 刑事报案人' }],
};

let _intake = {};

function resetIntake() { _intake = {}; renderIntake(); }

function collectFields() {
  const g = id => { const el = $(id); return el ? el.value.trim() : ''; };
  if ($('f-name'))   _intake.case_name   = g('f-name');
  if ($('f-abbr'))   _intake.abbr        = g('f-abbr');
  if ($('f-number')) _intake.case_number = g('f-number');
  if ($('f-cause')) _intake.cause       = g('f-cause');
  if ($('f-stage')) _intake.stage       = g('f-stage');
  if ($('f-us'))    _intake.parties_us  = g('f-us').split(',').map(s=>s.trim()).filter(Boolean);
  if ($('f-op'))    _intake.parties_op  = g('f-op').split(',').map(s=>s.trim()).filter(Boolean);
  if ($('f-raw'))   _intake.raw_text    = $('f-raw').value;
  if ($('f-path'))  _intake.cases_dir   = g('f-path');
  if ($('f-notes')) _intake.notes       = g('f-notes');
}

function selProc(v) { collectFields(); _intake.proc_type = v; _intake.role = null; renderIntake(); }
function selRole(v) { collectFields(); _intake.role = v; renderIntake(); }

function optButtons(opts, selected, fn) {
  return `<div class="opts-grid${opts.length >= 4 ? '' : ' cols3'}">` +
    opts.map(o =>
      `<button class="opt-btn${o.v === selected ? ' sel' : ''}" onclick="${fn}('${o.v}')" style="white-space:pre-line">${esc(o.l)}</button>`
    ).join('') + '</div>';
}

function renderPreview() {
  const pt = _intake.proc_type || '—';
  const rl = _intake.role || '—';
  const ab = _intake.abbr || 'ABBR';
  const d  = new Date().toISOString().slice(2,10).replace(/-/g,'');
  return `<div class="preview-box">
<span class="hl">案件</span>  ${esc(_intake.case_name || '（未填写）')}
<span class="hl">简称</span>  ${esc(ab)}　 <span class="hl">程序</span>  ${esc(pt)}　 <span class="hl">角色</span>  ${esc(rl)}

<span class="hl">命名示例</span>
<span class="green">  ${d} ${ab} Complaint v0 1.docx</span>
<span class="green">  ${d} ${ab} Court Order.pdf</span>
<span class="green">  ${d} ${ab} 会议纪要 v0 1.docx</span>
</div>`;
}

function renderIntake() {
  const pt = _intake.proc_type;
  const rl = _intake.role;

  $('intake-body').innerHTML = `

    <div class="intake-step">
      <div class="step-num">步骤 1 / 7 — 案件名称</div>
      <div class="step-title">案件名称与文件简称</div>
      <div class="step-hint">完整名称用于创建文件夹；简称用于所有文件名前缀，建议 4–6 字符，全团队统一。</div>
      <div class="field-row">
        <div class="field">
          <label>完整名称</label>
          <input type="text" id="f-name" placeholder="2026-ProjectEast-产品责任纠纷" value="${esc(_intake.case_name||'')}">
        </div>
        <div class="field">
          <label>文件简称</label>
          <input type="text" id="f-abbr" placeholder="ProjectEast" value="${esc(_intake.abbr||'')}">
        </div>
      </div>
      <div class="field" style="margin-top:10px">
        <label>案号 <span style="font-size:10.5px;color:var(--faint);font-weight:400">可选，如（2024）沪01民初1234号</span></label>
        <input type="text" id="f-number" placeholder="（2024）沪01民初1234号" value="${esc(_intake.case_number||'')}">
      </div>
    </div>

    <div class="intake-step">
      <div class="step-num">步骤 2 / 7 — 程序类型</div>
      <div class="step-title">程序类型</div>
      ${optButtons(PROC_OPTS, pt, 'selProc')}
    </div>

    ${pt ? `
    <div class="intake-step">
      <div class="step-num">步骤 3 / 7 — 我方角色</div>
      <div class="step-title">我方角色</div>
      ${optButtons(ROLE_OPTS[pt] || [], rl, 'selRole')}
    </div>` : ''}

    <div class="intake-step">
      <div class="step-num">步骤 4 / 7 — 案件信息</div>
      <div class="step-title">案由与当前阶段</div>
      <div class="field-row">
        <div class="field">
          <label>案由</label>
          <input type="text" id="f-cause" placeholder="产品责任纠纷 / Contract Breach" value="${esc(_intake.cause||'')}">
        </div>
        <div class="field">
          <label>当前阶段</label>
          <input type="text" id="f-stage" placeholder="一审庭前 / Pre-trial" value="${esc(_intake.stage||'')}">
        </div>
      </div>
    </div>

    <div class="intake-step">
      <div class="step-num">步骤 5 / 7 — 当事人</div>
      <div class="step-title">当事人信息</div>
      <div class="step-hint">多个主体以逗号分隔。</div>
      <div class="field-row">
        <div class="field">
          <label>我方主体</label>
          <input type="text" id="f-us" placeholder="中伦（代理）" value="${esc((_intake.parties_us||[]).join(', '))}">
        </div>
        <div class="field">
          <label>对方主体</label>
          <input type="text" id="f-op" placeholder="ABC Corporation" value="${esc((_intake.parties_op||[]).join(', '))}">
        </div>
      </div>
    </div>

    <div class="intake-step">
      <div class="step-num">步骤 6 / 7 — 识别关键词</div>
      <div class="step-title">关键词（用于自动识别本案文件）</div>
      <div class="step-hint">粘贴合同首页、起诉状摘要等文本，系统自动提取；或直接输入关键词（逗号分隔）。</div>
      <textarea id="f-raw" placeholder="粘贴相关文本…">${esc(_intake.raw_text||'')}</textarea>
    </div>

    <div class="intake-step">
      <div class="step-num">步骤 7 / 7 — 路径与备注</div>
      <div class="step-title">归档路径 & 备注</div>
      <div class="field-row full" style="margin-bottom:12px">
        <div class="field">
          <label>归档路径（留空使用 .env 默认路径）</label>
          <input type="text" id="f-path" placeholder="/Users/Admin/OneDrive/Cases" value="${esc(_intake.cases_dir||'')}">
        </div>
      </div>
      <div class="field">
        <label>备注（可选）</label>
        <input type="text" id="f-notes" placeholder="案件特殊情况、注意事项…" value="${esc(_intake.notes||'')}">
      </div>
    </div>

    ${pt && rl ? renderPreview() : ''}

    <div class="intake-submit">
      <button class="btn btn-blue" onclick="submitIntake()">创建案件</button>
      <button class="btn btn-ghost" onclick="resetIntake()">重置</button>
    </div>
  `;
}

async function submitIntake() {
  collectFields();
  const checks = [
    [_intake.case_name, '请填写案件名称'],
    [_intake.abbr,      '请填写文件简称'],
    [_intake.proc_type, '请选择程序类型'],
    [_intake.role,      '请选择我方角色'],
  ];
  for (const [v, msg] of checks) {
    if (!v) { toast(msg, true); return; }
  }
  if (/[\\/:*?"<>|：\s]/.test(_intake.abbr)) {
    toast('简称不能含空格或 / \\ : * ? 等字符（它会进每个文件名）', true);
    return;
  }

  toast('正在创建…');
  const res = await api('/api/intake', {
    method: 'POST',
    body: JSON.stringify({ ..._intake, parties_other: [] }),
  });

  if (res.ok) {
    const kwLine = res.keywords && res.keywords.length
      ? `\n关键词: ${res.keywords.join(' · ')}` : '';
    showModal('案件创建成功 ✓', `路径: ${res.path}${kwLine}\n\n案件已写入 config.yaml，现在可以将文件拖入收件箱开始归档。`);
    toast('✓ 案件已创建');
    _intake = {};
    renderIntake();
    loadCases();
  } else {
    showModal('创建失败', res.error + (res.trace ? '\n\n' + res.trace : ''));
    toast('创建失败', true);
  }
}

// ── Rules ──────────────────────────────────────────────────────
let _rules      = [];
let _rulesDirty = false;
let _editKw     = [];
let _editTkw    = [];

async function loadRules() {
  const data = await api('/api/rules');
  _rules = data.rules || [];
  _rulesDirty = false;
  renderRules();
}

function renderRules(filter) {
  if (filter === undefined) filter = ($('rules-search') || {}).value || '';
  filter = filter.trim().toLowerCase();

  $('btn-save-rules').textContent = _rulesDirty ? '保存修改 *' : '保存修改';

  const list = $('rules-list');
  if (!_rules.length) {
    list.innerHTML = '<div class="empty">暂无规则 — 点击「+ 新建」添加</div>';
    return;
  }

  const visible = _rules.map((r, i) => ({ r, i })).filter(({ r }) => {
    if (!filter) return true;
    const hay = [r.name, r.target, ...(r.keywords || []), ...(r.title_keywords || [])]
      .join(' ').toLowerCase();
    return hay.includes(filter);
  });

  if (!visible.length) {
    list.innerHTML = '<div class="empty">无匹配规则</div>';
    return;
  }

  const SRC_LABEL = { court: '法院', ours: '我方', opposing: '对方' };
  list.innerHTML = visible.map(({ r, i }) => {
    const kwTotal   = (r.keywords || []).length + (r.title_keywords || []).length;
    const dispName  = r.name || (r.target || '').split('/').pop() || '（未命名）';
    const srcBadge  = r.source
      ? `<span class="rule-source ${r.source}">${SRC_LABEL[r.source] || r.source}</span>` : '';
    return `<div class="rule-row">
      <span class="rule-idx">${i + 1}</span>
      <span class="rule-name" title="${esc(dispName)}">${esc(dispName)}</span>
      <span class="rule-folder" title="${esc(r.target || '')}">${esc(r.target || '')}</span>
      ${srcBadge}
      <span class="rule-kw-count">${kwTotal} 词</span>
      <div class="rule-actions">
        <button class="icon-btn" onclick="moveRule(${i},-1)" ${i===0?'disabled':''} title="上移">↑</button>
        <button class="icon-btn" onclick="moveRule(${i},1)"  ${i===_rules.length-1?'disabled':''} title="下移">↓</button>
        <button class="icon-btn" onclick="openRuleEditor(${i})">编辑</button>
        <button class="icon-btn danger" onclick="deleteRule(${i})">删除</button>
      </div>
    </div>`;
  }).join('');
}

function moveRule(idx, dir) {
  const to = idx + dir;
  if (to < 0 || to >= _rules.length) return;
  [_rules[idx], _rules[to]] = [_rules[to], _rules[idx]];
  _rulesDirty = true;
  renderRules();
}

function deleteRule(idx) {
  if (!confirm(`删除规则「${_rules[idx].name || idx}」？`)) return;
  _rules.splice(idx, 1);
  _rulesDirty = true;
  renderRules();
}

function addNewRule() { openRuleEditor(-1); }

function openRuleEditor(idx) {
  const r = idx >= 0 ? _rules[idx] : {};
  $('re-idx').value = idx;
  $('rule-modal-title').textContent = idx < 0 ? '新建规则' : '编辑规则';
  $('re-name').value   = r.name   || '';
  $('re-folder').value = r.target || '';
  $('re-source').value = r.source || '';
  _editKw  = [...(r.keywords       || [])];
  _editTkw = [...(r.title_keywords || [])];
  renderKwTags();
  $('rule-overlay').classList.add('open');
  setTimeout(() => $('re-name').focus(), 60);
}

function renderKwTags() {
  const empty = '<span style="font-size:11px;color:var(--faint);padding:2px 4px">尚无关键词</span>';
  $('re-kw-tags').innerHTML = _editKw.length
    ? _editKw.map((k, i) =>
        `<span class="kw-tag">${esc(k)}<button class="kw-tag-x" onclick="removeKw('kw',${i})">×</button></span>`
      ).join('')
    : empty;
  $('re-tkw-tags').innerHTML = _editTkw.length
    ? _editTkw.map((k, i) =>
        `<span class="kw-tag">${esc(k)}<button class="kw-tag-x" onclick="removeKw('tkw',${i})">×</button></span>`
      ).join('')
    : empty;
}

function addKw(type) {
  const inputId = type === 'kw' ? 're-kw-input' : 're-tkw-input';
  const vals = $(inputId).value.split(/[,，\s]+/).map(s => s.trim()).filter(Boolean);
  vals.forEach(v => {
    if (type === 'kw'  && !_editKw.includes(v))  _editKw.push(v);
    if (type === 'tkw' && !_editTkw.includes(v)) _editTkw.push(v);
  });
  $(inputId).value = '';
  renderKwTags();
}

function removeKw(type, idx) {
  if (type === 'kw')  _editKw.splice(idx, 1);
  else                _editTkw.splice(idx, 1);
  renderKwTags();
}

function confirmRule() {
  const kwPending  = $('re-kw-input').value.trim();
  const tkwPending = $('re-tkw-input').value.trim();
  if (kwPending)  { $('re-kw-input').value  = kwPending;  addKw('kw');  }
  if (tkwPending) { $('re-tkw-input').value = tkwPending; addKw('tkw'); }

  const name   = $('re-name').value.trim();
  const folder = $('re-folder').value.trim();
  if (!name)   { toast('请填写规则名称', true);   return; }
  if (!folder) { toast('请填写目标文件夹', true); return; }

  const idx  = parseInt($('re-idx').value);
  const rule = {
    ...(idx >= 0 ? _rules[idx] : {}),
    target: folder,
    keywords: [..._editKw],
  };
  if (name) rule.name = name;
  else      delete rule.name;
  const src = $('re-source').value;
  if (src) rule.source = src;
  else     delete rule.source;
  if (_editTkw.length) rule.title_keywords = [..._editTkw];
  else                 delete rule.title_keywords;

  if (idx < 0) _rules.push(rule);
  else         _rules[idx] = rule;

  _rulesDirty = true;
  closeRuleModal();
  renderRules();
}

function closeRuleModal() {
  $('rule-overlay').classList.remove('open');
}

async function saveAllRules() {
  if (!_rulesDirty && !confirm('规则未修改，仍要保存？')) return;
  toast('保存中…');
  const res = await api('/api/rules', {
    method: 'POST',
    body: JSON.stringify({ rules: _rules }),
  });
  if (res.ok) {
    _rulesDirty = false;
    renderRules();
    toast('✓ 规则已保存');
  } else {
    toast('保存失败: ' + (res.error || ''), true);
  }
}

// ── Review queue (待办复核) ─────────────────────────────────────
let _reviewData = [];

async function refreshReviewBadge() {
  try {
    const d = await api('/api/review');
    const badge = $('nav-review-badge');
    if (d.count > 0) { badge.textContent = d.count; badge.style.display = ''; }
    else             badge.style.display = 'none';
    return d;
  } catch { return { items: [], count: 0 }; }
}

async function loadReview() {
  const data = await refreshReviewBadge();
  _reviewData = data.items || [];
  const list = $('review-list');
  if (!_reviewData.length) {
    list.innerHTML = '<div class="empty">没有待复核的文件 ✓</div>';
    return;
  }
  list.innerHTML = _reviewData.map((it, i) => {
    const actions = it.need_source
      ? `<span class="hint-sm" style="margin-right:4px">来源?</span>
         <button class="icon-btn" onclick="confirmSource(${i},'我方证据')">我方</button>
         <button class="icon-btn" onclick="confirmSource(${i},'对方证据')">对方</button>
         <button class="icon-btn" onclick="openReclass(${i})">其它</button>`
      : `<button class="icon-btn" onclick="openReclass(${i})">重新归类</button>`;
    const reasonCls = it.need_source ? 'review-reason need-source' : 'review-reason';
    return `<div class="file-row">
      <span class="${reasonCls}">${esc(it.reason)}</span>
      <span class="file-name" title="${esc(it.path)}">${esc(it.name)}</span>
      <span class="file-meta">${esc(it.loc || '')}</span>
      <div class="rule-actions">${actions}</div>
    </div>`;
  }).join('');
}

async function confirmSource(i, side) {
  const it = _reviewData[i];
  const res = await api('/api/review/confirm_source', {
    method: 'POST',
    body: JSON.stringify({ path: it.path, side, pending_target: it.pending_target }),
  });
  if (res.ok) { toast('✓ 已确认来源并归位'); loadReview(); }
  else        toast(res.error || '失败', true);
}

async function openReclass(i) {
  const it = _reviewData[i];
  if (!(_meta.cases || []).length) await loadMeta();
  $('rc-path').value = it.path;
  $('rc-current').textContent = `${it.name}　（${it.loc || ''}）`;
  $('rc-name').value = it.name;
  $('rc-case').innerHTML = '<option value="">（选择案件）</option>' +
    (_meta.cases || []).map(c => `<option value="${esc(c.key)}">${esc(c.key)} · ${esc(c.name)}</option>`).join('');
  $('rc-case').value = '';
  $('rc-target').innerHTML = '<option value="">（先选案件）</option>';
  $('reclass-overlay').classList.add('open');
}

async function rcLoadFolders(key) {
  if (!key) { $('rc-target').innerHTML = '<option value="">（先选案件）</option>'; return; }
  const r = await api(`/api/cases/${encodeURIComponent(key)}/folders`);
  const fs = (r && r.folders) || [];
  $('rc-target').innerHTML = '<option value="">（选择文件夹）</option>' +
    fs.map(f => `<option value="${esc(f)}">${esc(f)}</option>`).join('');
}

async function confirmReclass() {
  const body = {
    path:     $('rc-path').value,
    case_key: $('rc-case').value,
    target:   $('rc-target').value,
    new_name: $('rc-name').value.trim(),
  };
  if (!body.case_key) { toast('请选择案件', true); return; }
  if (!body.target)   { toast('请选择目标文件夹', true); return; }
  toast('移动中…');
  const res = await api('/api/review/reclassify', { method: 'POST', body: JSON.stringify(body) });
  if (res.ok) { toast('✓ 已重新归类'); closeReclass(); loadReview(); }
  else        toast(res.error || '失败', true);
}

function closeReclass() { $('reclass-overlay').classList.remove('open'); }

// ── Init ───────────────────────────────────────────────────────
// ══════════════════════════════════════════════════════════════
// 脱敏工作台：选文件 → 识别 → 人工删补 → 左右预览 → 导出
// ══════════════════════════════════════════════════════════════
const DESK = { case: '', rel: '', src: '', spans: [], hidden: new Set(),
               applied: false, ner: true, vis: [] };

const DESK_LABEL_ZH = {
  COMPANY:'公司', COMPANY_SUFFIX:'公司', PERSON:'人名', NAME_ANON:'人名',
  ADDRESS:'地址', ADDRESS_RE:'地址', ID_CARD:'身份证', USCC:'统一码', BANKCARD:'银行卡',
  MOBILE:'电话', TEL:'电话', PHONE:'电话', EMAIL:'邮箱', PLATE:'车牌',
  CASE_NO:'案号', DOC_NO:'文号', MONEY:'金额', MONEY_CN:'金额', SECURITIES:'证券', WECHAT:'微信',
  SSN:'SSN', EIN:'EIN', OTHER:'其他', PASSPORT:'护照', HK_MO_TW:'港澳台',
  DRIVER_LICENSE:'驾驶证', SOCIAL_SEC:'社保公积金', QQ:'QQ', BIRTHDAY:'出生日期',
  IP:'IP', MAC:'MAC', IMEI:'IMEI', GPS:'坐标',
};
// 划选补标可选类别（顺序 = 浮层展示顺序）
const DESK_PICK = [
  ['PERSON','人名'],['COMPANY','公司'],['ADDRESS','地址'],['ID_CARD','身份证'],
  ['PASSPORT','护照'],['HK_MO_TW','港澳台'],['DRIVER_LICENSE','驾驶证'],['BANKCARD','银行卡'],
  ['USCC','统一码'],['SOCIAL_SEC','社保公积金'],['PHONE','电话'],['EMAIL','邮箱'],
  ['WECHAT','微信'],['QQ','QQ'],['CASE_NO','案号'],['MONEY','金额'],['PLATE','车牌'],
  ['BIRTHDAY','出生日期'],['IP','IP'],['MAC','MAC'],['IMEI','IMEI'],['GPS','坐标'],['OTHER','其他'],
];
const deskZh = lbl => DESK_LABEL_ZH[lbl] || lbl;
const deskMode = () => (document.querySelector('input[name="desk-mode"]:checked') || {}).value || 'alias';

async function loadDesk() {
  const data = await api('/api/cases');
  const cases = (data.cases || []).filter(c => c.status !== 'closed');
  const sel = $('desk-case');
  const prev = DESK.case;
  sel.innerHTML = '<option value="">选择案件…</option>' +
    cases.map(c => `<option value="${esc(c.key)}">${esc(c.name)}</option>`).join('');
  if (prev && cases.some(c => c.key === prev)) sel.value = prev;
}

async function deskLoadFiles(key) {
  const fs = $('desk-file');
  fs.innerHTML = '<option value="">加载中…</option>';
  const data = await api('/api/desensitize/files?case=' + encodeURIComponent(key));
  if (!data.ok) { fs.innerHTML = '<option value="">' + esc(data.error || '加载失败') + '</option>'; return; }
  if (!data.files.length) { fs.innerHTML = '<option value="">（无可脱敏文件）</option>'; return; }
  fs.innerHTML = '<option value="">选择文件…</option>' + data.files.map(f => {
    const label = (f.dir ? f.dir + '/' : '') + f.name;
    return `<option value="${esc(f.rel)}">${esc(label)}</option>`;
  }).join('');
}

async function deskAnalyze() {
  if (!DESK.case || !DESK.rel) { toast('请先选择案件与文件', true); return; }
  const src = $('desk-src');
  src.innerHTML = '<div class="empty">识别中…（首次加载模型 / OCR 可能较慢）</div>';
  $('desk-out').innerHTML = '<div class="empty">识别后点「生成脱敏」在此预览</div>';
  $('desk-cats').innerHTML = '';
  DESK.applied = false; $('desk-export').disabled = true;
  const r = await api('/api/desensitize/analyze', {
    method: 'POST',
    body: JSON.stringify({ case: DESK.case, rel: DESK.rel }),
  });
  if (!r.ok) { src.innerHTML = `<div class="empty">✗ ${esc(r.error || '识别失败')}</div>`; return; }
  DESK.src = r.text || '';
  DESK.spans = r.spans || [];
  DESK.hidden = new Set();
  DESK.ner = r.ner;
  if (!DESK.src.trim()) { src.innerHTML = '<div class="empty">未提取到文本内容</div>'; return; }
  deskRenderCats();
  deskRenderSrc();
  deskUpdateStat();
}

function deskUpdateStat() {
  const total = DESK.spans.length;
  const low = DESK.spans.filter(s => s.low_conf).length;
  const nerTxt = DESK.ner ? '语义+正则' : '仅正则（模型未连接）';
  $('desk-stat').innerHTML = `命中 <b>${total}</b> 处` +
    (low ? ` · <span class="desk-low-n">待确认 ${low}</span>` : '') +
    ` · ${nerTxt}`;
}

// 类别筛选 chips（点击隐藏/显示某类，隐藏的不脱敏）
function deskRenderCats() {
  const counts = {};
  DESK.spans.forEach(s => { const z = deskZh(s.label); counts[z] = (counts[z] || 0) + 1; });
  const zhs = Object.keys(counts).sort((a, b) => counts[b] - counts[a]);
  $('desk-cats').innerHTML = zhs.length
    ? '<span class="desk-cats-label">类别</span>' + zhs.map(z =>
        `<button class="cat-chip${DESK.hidden.has(z) ? ' off' : ''}" data-zh="${esc(z)}">${esc(z)} <span class="cat-n">${counts[z]}</span></button>`
      ).join('')
    : '';
}

// 高亮渲染：只渲染未被类别筛选隐藏的 span；低置信标黄
function deskRenderSrc() {
  const vis = DESK.spans
    .filter(s => !DESK.hidden.has(deskZh(s.label)))
    .sort((a, b) => a.start - b.start);
  DESK.vis = vis;
  let html = '', cur = 0;
  vis.forEach((s, i) => {
    if (s.start < cur) return;                       // 跳过重叠（应用时服务端再并）
    html += esc(DESK.src.slice(cur, s.start));
    const cls = 'dmark' + (s.low_conf ? ' dmark-low' : '');
    html += `<mark class="${cls}" data-i="${i}" data-zh="${esc(deskZh(s.label))}" title="点击删除此标记">${esc(DESK.src.slice(s.start, s.end))}</mark>`;
    cur = s.end;
  });
  html += esc(DESK.src.slice(cur));
  $('desk-src').innerHTML = html || '<div class="empty">（空）</div>';
}

// 编辑后：预览标记为过期
function deskStale() {
  DESK.applied = false;
  $('desk-export').disabled = true;
  const out = $('desk-out');
  if (out.dataset.has === '1')
    out.innerHTML = '<div class="empty">内容已修改 — 点「生成脱敏 →」刷新预览</div>';
  out.dataset.has = '';
}

async function deskApply(forExport) {
  if (!DESK.src) { toast('请先识别', true); return; }
  const spans = DESK.spans.filter(s => !DESK.hidden.has(deskZh(s.label)));
  const mode = deskMode();
  const out = $('desk-out');
  out.innerHTML = '<div class="empty">生成中…</div>';
  const r = await api('/api/desensitize/apply', {
    method: 'POST',
    body: JSON.stringify({ case: DESK.case, rel: DESK.rel, text: DESK.src,
                           spans, mode, save: !!forExport }),
  });
  if (!r.ok) { out.innerHTML = `<div class="empty">✗ ${esc(r.error || '脱敏失败')}</div>`; return; }
  out.textContent = r.masked;
  out.dataset.has = '1';
  DESK.applied = true;
  $('desk-export').disabled = false;
  deskRenderPending(r.pending || []);
  if (forExport && r.saved) toast('✓ 已导出：' + r.saved.split('/').slice(-2).join('/'));
}

function deskRenderPending(pending) {
  const bar = $('desk-pending');
  if (!pending.length) { bar.style.display = 'none'; bar.innerHTML = ''; return; }
  bar.style.display = '';
  bar.innerHTML = '<span class="desk-pending-label">⚠ 待人工确认（疑似同名归并）</span>' +
    pending.map(p => `<span class="pending-item">${esc(p.alias)} ← ${esc(p.canonical)}</span>`).join('');
}

async function deskReset() {
  if (!confirm('清空本案已建立的化名映射并重新编号？（不影响已识别结果）')) return;
  await api('/api/desensitize/reset', { method: 'POST', body: JSON.stringify({ case: DESK.case }) });
  DESK.applied = false;
  $('desk-out').innerHTML = '<div class="empty">已重置化名，点「生成脱敏」重新生成</div>';
  $('desk-out').dataset.has = '';
  $('desk-export').disabled = true;
  toast('✓ 化名已重置');
}

function deskInit() {
  $('desk-case').addEventListener('change', e => {
    DESK.case = e.target.value; DESK.rel = '';
    $('desk-file').innerHTML = '<option value="">选择文件…</option>';
    if (DESK.case) deskLoadFiles(DESK.case);
  });
  $('desk-file').addEventListener('change', e => { DESK.rel = e.target.value; });
  $('desk-analyze').addEventListener('click', deskAnalyze);
  $('desk-apply').addEventListener('click', () => deskApply(false));
  $('desk-export').addEventListener('click', () => {
    if (!DESK.rel) { toast('粘贴/无文件来源，无法导出到案件空间', true); return; }
    deskApply(true);
  });
  $('desk-reset').addEventListener('click', deskReset);

  // 脱敏方式切换：已生成则实时刷新预览
  document.querySelectorAll('input[name="desk-mode"]').forEach(r =>
    r.addEventListener('change', () => {
      $('desk-mode-tag').textContent = { alias:'化名', blackout:'黑框', asterisk:'星号' }[deskMode()];
      if (DESK.applied) deskApply(false);
    }));

  // 类别筛选
  $('desk-cats').addEventListener('click', e => {
    const chip = e.target.closest('.cat-chip'); if (!chip) return;
    const z = chip.dataset.zh;
    if (DESK.hidden.has(z)) DESK.hidden.delete(z); else DESK.hidden.add(z);
    deskRenderCats(); deskRenderSrc(); deskStale();
  });

  // 点高亮 → 删误标
  $('desk-src').addEventListener('click', e => {
    const m = e.target.closest('mark'); if (!m) return;
    const target = DESK.vis[+m.dataset.i];
    DESK.spans = DESK.spans.filter(s => s !== target);
    deskRenderCats(); deskRenderSrc(); deskUpdateStat(); deskStale();
  });

  // 划选 → 类别浮层 → 补标
  $('desk-src').addEventListener('mouseup', () => {
    const sel = window.getSelection();
    if (!sel.rangeCount || sel.isCollapsed) { deskHidePicker(); return; }
    const box = $('desk-src'); const r = sel.getRangeAt(0);
    if (!box.contains(r.commonAncestorContainer)) return;
    const pre = document.createRange();
    pre.selectNodeContents(box); pre.setEnd(r.startContainer, r.startOffset);
    const start = pre.toString().length;
    const end = start + r.toString().length;
    if (end <= start) { deskHidePicker(); return; }
    deskShowPicker(start, end, r.getBoundingClientRect());
  });

  // 补标类别浮层点击
  $('desk-picker').addEventListener('click', e => {
    const b = e.target.closest('button'); if (!b) return;
    const { start, end } = $('desk-picker')._range;
    DESK.spans.push({ start, end, text: DESK.src.slice(start, end),
                      label: b.dataset.label, source: 'manual', score: 1, low_conf: false });
    deskHidePicker(); window.getSelection().removeAllRanges();
    deskRenderCats(); deskRenderSrc(); deskUpdateStat(); deskStale();
  });
}

function deskShowPicker(start, end, rect) {
  const pk = $('desk-picker');
  pk._range = { start, end };
  pk.innerHTML = '<span class="picker-hint">标为：</span>' +
    DESK_PICK.map(([l, z]) => `<button data-label="${l}">${z}</button>`).join('');
  const host = $('sec-desk').getBoundingClientRect();
  pk.style.display = 'flex';
  let left = rect.left - host.left;
  let top = rect.bottom - host.top + 4;
  left = Math.max(8, Math.min(left, host.width - pk.offsetWidth - 8));
  top = Math.min(top, host.height - pk.offsetHeight - 8);
  pk.style.left = left + 'px';
  pk.style.top = top + 'px';
}
function deskHidePicker() { $('desk-picker').style.display = 'none'; }

document.addEventListener('DOMContentLoaded', () => {

  // sidebar nav
  document.querySelectorAll('.nav-btn[data-sec]').forEach(btn =>
    btn.addEventListener('click', () => nav(btn.dataset.sec))
  );

  deskInit();

  // dashboard buttons
  $('btn-daemon').addEventListener('click', async () => {
    const stopping = $('btn-daemon').classList.contains('btn-danger');
    toast(stopping ? '正在停止…' : '正在启动…');
    await api(stopping ? '/api/daemon/stop' : '/api/daemon/start', { method: 'POST' });
    setTimeout(loadDashboard, 1400);
  });

  $('btn-once').addEventListener('click',    () => runOnce(false));
  $('btn-dry').addEventListener('click',     () => runOnce(true));
  $('btn-process-inbox').addEventListener('click', () => runOnce(false));
  $('btn-refresh-inbox').addEventListener('click', loadInbox);

  // rules
  $('btn-add-rule').addEventListener('click',   addNewRule);
  $('btn-save-rules').addEventListener('click', saveAllRules);
  $('rules-search').addEventListener('input', e => renderRules(e.target.value));
  $('re-kw-input').addEventListener('keydown',  e => { if (e.key === 'Enter') { e.preventDefault(); addKw('kw');  } });
  $('re-tkw-input').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); addKw('tkw'); } });

  // modal close
  $('overlay').addEventListener('click', closeModal);
  $('rule-overlay').addEventListener('click', function(e) {
    if (e.target === this) closeRuleModal();
  });
  $('case-overlay').addEventListener('click', function(e) {
    if (e.target === this) closeCaseModal();
  });
  $('filetree-overlay').addEventListener('click', function(e) {
    if (e.target === this) closeFileTree();
  });
  $('ft-open-btn').addEventListener('click', async () => {
    if (!_ftKey) return;
    const res = await api('/api/cases/open', {
      method: 'POST',
      body: JSON.stringify({ key: _ftKey }),
    });
    if (!res.ok) toast(res.error || '文件夹不存在', true);
  });
  $('ce-kw-input').addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); addCaseKw(); }
  });

  // drag-and-drop upload (global)
  let _dragCount = 0;
  document.addEventListener('dragenter', e => {
    if (!e.dataTransfer || !e.dataTransfer.types.includes('Files')) return;
    _dragCount++;
    $('drag-overlay').classList.add('active');
  });
  document.addEventListener('dragleave', () => {
    _dragCount--;
    if (_dragCount <= 0) { _dragCount = 0; $('drag-overlay').classList.remove('active'); }
  });
  document.addEventListener('dragover',  e => { e.preventDefault(); });
  document.addEventListener('drop', e => {
    e.preventDefault();
    _dragCount = 0;
    $('drag-overlay').classList.remove('active');
    const files = [...(e.dataTransfer.files || [])];
    if (files.length) openPrefill(files);
  });

  // click-to-upload
  $('btn-upload-inbox').addEventListener('click', () => $('file-input').click());
  $('file-input').addEventListener('change', e => {
    const files = [...e.target.files];
    if (files.length) openPrefill(files);
    e.target.value = '';
  });

  // pre-fill cascading（已选案件则用案件文件夹，国家/程序仅在未选案件时过滤兜底列表）
  const refreshPfTargets = () => { if (!$('pf-case').value) populatePfTargets(); };
  $('pf-country').addEventListener('change', refreshPfTargets);
  $('pf-proc').addEventListener('change', refreshPfTargets);
  $('pf-case').addEventListener('change', () => {
    const key = $('pf-case').value;
    const c = (_meta.cases || []).find(x => x.key === key);
    const ui = _SCEN_UI[c && c.scenario] || {};
    if (ui.country) $('pf-country').value = ui.country;
    if (ui.proc)    $('pf-proc').value    = ui.proc;
    loadCaseFolders(key);   // 列出该案磁盘上真实的全部文件夹
    populatePfParties(key); // 列出该案当事人 + 法院
    populatePfAlso(key);    // 其他案件（用于跨案关联）
  });
  // 选了来源主体 → 自动带出来源（可再手改）
  $('pf-party').addEventListener('change', () => {
    const side = partySideOf($('pf-case').value, $('pf-party').value);
    if (side) $('pf-source').value = side;
  });
  $('prefill-overlay').addEventListener('click', function(e) {
    if (e.target === this) closePrefill();
  });

  // review queue
  $('btn-refresh-review').addEventListener('click', loadReview);
  $('btn-reprocess-uncat').addEventListener('click', async () => {
    const n = _reviewData.filter(it => it.reason === '未识别案件').length;
    if (!n) { toast('没有未识别案件的文件'); return; }
    if (!confirm(`将 ${n} 个未识别案件文件移回收件箱并重新归类？`)) return;
    toast('正在提交…');
    const res = await api('/api/review/reprocess_uncat', { method: 'POST' });
    if (res.ok) {
      toast(`✓ ${res.msg || '已提交重新归类'}`);
      setTimeout(loadReview, 3000);
    } else {
      toast('失败: ' + (res.error || ''), true);
    }
  });
  $('rc-case').addEventListener('change', () => rcLoadFolders($('rc-case').value));
  $('reclass-overlay').addEventListener('click', function (e) {
    if (e.target === this) closeReclass();
  });

  // initial load
  loadDashboard();
  refreshReviewBadge();
  setInterval(() => { if (_currentSec === 'dashboard') { loadDashboard(); refreshReviewBadge(); } }, 8000);
});
