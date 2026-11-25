'use strict';

// ── Helpers ───────────────────────────────────────────────────
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');

// Loci uses a small inline SVG vocabulary so the interface remains sharp and
// fully offline.  Keeping the paths here also prevents platform-dependent
// emoji glyphs from changing the visual language of the workbench.
const ICON_PATHS = {
  file: '<path d="M6 2.75h7l4 4v14.5H6z"/><path d="M13 2.75v4h4M8.5 11h6M8.5 14h6M8.5 17h4"/>',
  pdf: '<path d="M6 2.75h7l4 4v14.5H6z"/><path d="M13 2.75v4h4M8.5 13h1.8a1.2 1.2 0 0 0 0-2.4H8.5v5M12.5 15.6v-5h1.1a2.5 2.5 0 0 1 0 5z"/>',
  doc: '<path d="M6 2.75h7l4 4v14.5H6z"/><path d="M13 2.75v4h4M8.5 11h6M8.5 14h6M8.5 17h4"/>',
  sheet: '<rect x="4" y="3" width="16" height="18" rx="1.5"/><path d="M4 8h16M4 13h16M4 17h16M10 8v13M15 8v13"/>',
  image: '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="8" cy="9" r="1.4"/><path d="m4.5 18 5-5 3.2 3 2.2-2 4.6 4"/>',
  media: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m10 9 5 3-5 3z"/>',
  archive: '<path d="M4 7h16v13H4zM3 4h18v3H3zM9 11h6"/>',
  folder: '<path d="M3 6.5h7l1.6 2H21v10.2a1.3 1.3 0 0 1-1.3 1.3H4.3A1.3 1.3 0 0 1 3 18.7z"/>',
  folderOpen: '<path d="m3 7 1.5 11.5h14L21 9.5H11L9.5 7z"/><path d="M4.5 18.5 7 12h14l-2.5 6.5"/>',
  inbox: '<path d="M4 4h16v15H4z"/><path d="M4 13h4l1.5 2h5L16 13h4M8 8h8"/>',
  upload: '<path d="M12 16V4m0 0L8 8m4-4 4 4M5 16v3h14v-3"/>',
  search: '<circle cx="10.8" cy="10.8" r="6.3"/><path d="m16 16 4 4"/>',
  refresh: '<path d="M19 8a7.5 7.5 0 1 0 1 5"/><path d="M19 3v5h-5"/>',
  external: '<path d="M14 4h6v6M20 4l-9 9"/><path d="M18 13v5a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h5"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  close: '<path d="m6 6 12 12M18 6 6 18"/>',
  check: '<path d="m5 12 4.2 4.2L19 6.5"/>',
  alert: '<path d="M12 4 21 20H3z"/><path d="M12 9v5M12 17.2v.2"/>',
  edit: '<path d="m4 16.5-.8 3.3 3.3-.8L18.8 6.7a2 2 0 0 0-2.8-2.8zM14.5 6.5l3 3"/>',
  trash: '<path d="M4 7h16M10 11v5M14 11v5M6 7l1 13h10l1-13M9 7V4h6v3"/>',
  chevron: '<path d="m9 5 7 7-7 7"/>',
  up: '<path d="m6 10 6-6 6 6M12 4v16"/>',
  down: '<path d="m6 14 6 6 6-6M12 20V4"/>',
  grid: '<rect x="4" y="4" width="6" height="6" rx="1"/><rect x="14" y="4" width="6" height="6" rx="1"/><rect x="4" y="14" width="6" height="6" rx="1"/><rect x="14" y="14" width="6" height="6" rx="1"/>',
  pulse: '<path d="M3 12h4l2-6 4 12 2-6h6"/>',
  shield: '<path d="M12 3 19 6v5c0 4.8-3 8-7 10-4-2-7-5.2-7-10V6z"/><path d="m9 12 2 2 4-4"/>',
};

function icon(name, cls = '') {
  return `<svg class="ui-icon ${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICON_PATHS[name] || ICON_PATHS.file}</svg>`;
}

async function api(path, opts = {}) {
  if (window.location.protocol === 'file:') {
    return { ok: false, error: '当前打开的是模板文件。请运行 loci/run.py 或 bash bin/filer.sh loci 后，从 http://127.0.0.1:5081 进入。' };
  }
  let res;
  try {
    res = await fetch(path, {
      headers: { 'Content-Type': 'application/json' },
      ...opts,
    });
  } catch (error) {
    return { ok: false, error: `无法连接本地服务：${error.message || error}` };
  }
  let body;
  try { body = await res.json(); }
  catch { body = { ok: false, error: `请求失败（HTTP ${res.status}）` }; }
  if (body && typeof body === 'object' && !Array.isArray(body)) {
    if (!res.ok && body.ok === undefined) body.ok = false;
    return body;
  }
  return { ok: res.ok, data: body };
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
  const text = (m ? line.slice(m[0].length) : line)
    .replace(/[✅❌⚠⚡]/g, '')
    .replace(/\s*→\s*/g, '  ')
    .trim();

  return `<div class="log-line ${cls}"><span class="log-t">${esc(time)}</span><span>${esc(text)}</span></div>`;
}

// ── Dashboard ──────────────────────────────────────────────────
async function loadDashboard() {
  const [st, lg] = await Promise.all([api('/api/status'), api('/api/log')]);
  const daemon = st.daemon || {};
  const starting = daemon.detail === 'launching' || daemon.detail === 'starting';
  const running = daemon.running === true && daemon.ready !== false;

  // sidebar pill
  const pill  = $('sidebar-daemon');
  const label = $('sidebar-daemon-label');
  if (starting) {
    pill.querySelector('.dot').className  = 'dot dot-amber';
    label.textContent = daemon.pid ? `启动中 · PID ${daemon.pid}` : '启动中';
  } else if (running) {
    pill.querySelector('.dot').className  = 'dot dot-green';
    label.textContent = `PID ${daemon.pid}`;
  } else {
    pill.querySelector('.dot').className  = 'dot dot-gray';
    label.textContent = '守护进程';
  }

  // stat cards
  const daemonEl  = $('stat-daemon');
  const daemonBtn = $('btn-daemon');
  if (starting) {
    daemonEl.innerHTML = `<span class="dot dot-amber"></span> 启动中${daemon.pid ? ` <span style="color:var(--faint);font-size:11px;font-family:var(--mono)">PID ${daemon.pid}</span>` : ''}`;
    daemonBtn.textContent = '停止';
    daemonBtn.className   = 'btn btn-xs btn-danger mt8';
    daemonBtn.dataset.daemonAction = 'stop';
    daemonBtn.disabled = false;
  } else if (running) {
    daemonEl.innerHTML = `<span class="dot dot-green"></span> 运行中 <span style="color:var(--faint);font-size:11px;font-family:var(--mono)">PID ${daemon.pid}</span>`;
    daemonBtn.textContent = '停止';
    daemonBtn.className   = 'btn btn-xs btn-danger mt8';
    daemonBtn.dataset.daemonAction = 'stop';
    daemonBtn.disabled = false;
  } else {
    daemonEl.innerHTML = `<span class="dot dot-gray"></span> 未运行`;
    daemonBtn.textContent = '启动';
    daemonBtn.className   = 'btn btn-xs btn-ghost mt8';
    daemonBtn.dataset.daemonAction = 'start';
    daemonBtn.disabled = false;
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
function relativePathOf(file) {
  return file._lociRelativePath || file.webkitRelativePath || file.name;
}

function newUploadBatchId() {
  const stamp = new Date().toISOString().replace(/[-:.TZ]/g, '').slice(0, 14);
  return `B${stamp}-${Math.random().toString(16).slice(2, 10)}`;
}

function uploadChunks(files) {
  const maxFiles = 80;
  const maxBytes = 220 * 1024 * 1024;
  const chunks = [];
  let chunk = [], bytes = 0;
  files.forEach((file, index) => {
    if (chunk.length && (chunk.length >= maxFiles || bytes + file.size > maxBytes)) {
      chunks.push(chunk); chunk = []; bytes = 0;
    }
    chunk.push({ file, index });
    bytes += file.size;
  });
  if (chunk.length) chunks.push(chunk);
  return chunks;
}

async function uploadToInbox(files, batchMeta = {}, metas = [], options = {}) {
  const chunks = uploadChunks(files);
  const batchId = newUploadBatchId();
  let uploaded = 0;
  const saved = [];
  try {
    for (let n = 0; n < chunks.length; n++) {
      const chunk = chunks[n];
      toast(`上传中 · ${uploaded}/${files.length}`);
      const form = new FormData();
      chunk.forEach(({ file }) => form.append('files', file, file.name));
      form.append('batch_id', batchId);
      form.append('batch_meta', JSON.stringify(batchMeta || {}));
      form.append('metas', JSON.stringify(chunk.map(({ index }) => metas[index] || {})));
      form.append('relative_paths', JSON.stringify(chunk.map(({ file }) => relativePathOf(file))));
      if (options.stageOnly) form.append('stage_only', '1');
      const res = await fetch('/api/inbox/upload', { method: 'POST', body: form });
      const data = await res.json();
      if (!res.ok || !data.ok) throw new Error(data.error || '未知错误');
      uploaded += data.count || 0;
      saved.push(...(data.saved || []));
    }
    toast(options.stageOnly
      ? `已暂存 ${uploaded} 个文件 · 等待确认`
      : `已上传 ${uploaded} 个文件 · 批次 ${batchId}`);
    if (_currentSec === 'inbox') loadInbox();
    else                         loadDashboard();
    return { ok: true, count: uploaded, batchId, saved };
  } catch (e) {
    const prefix = uploaded ? `已上传 ${uploaded} 个；后续批次失败：` : '上传失败：';
    toast(prefix + e.message, true);
    return { ok: false, error: e.message, count: uploaded, batchId, saved };
  }
}

// ── Pre-fill (投放后人工预分类) ─────────────────────────────────
let _pendingFiles = [];
let _meta = { cases: [], targets: [] };
let _lastDecision = null;
let _pfDecisions = [];
let _pendingStaged = [];
const _FAILED_BATCH_STORAGE_KEY = 'loci.failed-batches.v1';

function readFailedBatchStates() {
  try {
    const raw = globalThis.localStorage?.getItem(_FAILED_BATCH_STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed)
      ? parsed.filter(item => item && item.batch_id).slice(0, 6)
      : [];
  } catch { return []; }
}

let _failedBatchStates = readFailedBatchStates();

function persistFailedBatchStates() {
  try {
    globalThis.localStorage?.setItem(
      _FAILED_BATCH_STORAGE_KEY, JSON.stringify(_failedBatchStates.slice(0, 6)),
    );
  } catch {}
}

function rememberFailedBatch(failure) {
  if (!failure?.batch_id) return null;
  const state = {
    ...failure,
    updated_at: new Date().toISOString(),
    undoing: false,
  };
  _failedBatchStates = [state, ..._failedBatchStates.filter(
    item => item.batch_id !== state.batch_id,
  )].slice(0, 6);
  persistFailedBatchStates();
  if (typeof renderStagingPanel === 'function') renderStagingPanel();
  return state;
}

function forgetFailedBatch(batchId) {
  _failedBatchStates = _failedBatchStates.filter(item => item.batch_id !== batchId);
  persistFailedBatchStates();
  if (typeof renderStagingPanel === 'function') renderStagingPanel();
}

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
  renderTargetOptions(ts, '（先选案件可列出完整文件夹）', 'pf-target', 'pf-target-hint');
  renderTargetOptions(ts, '可选', 'pf-batch-target', 'pf-batch-target-hint');
}

// 把目标文件夹列表渲染成"按顶层分组"的下拉
function renderTargetOptions(folders, placeholder, selectId = 'pf-target', hintId = 'pf-target-hint') {
  const sel = $(selectId);
  const cur = sel.value;
  const groups = {};
  (folders || []).forEach(f => {
    const top = f.split('/')[0];
    (groups[top] = groups[top] || []).push(f);
  });
  const empty = selectId === 'pf-batch-target'
    ? '（逐份填写 / 自动识别）' : '（继承批次 / 自动识别）';
  let html = `<option value="">${empty}</option>`;
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
  $(hintId).textContent = (folders || []).length
    ? `共 ${folders.length} 个文件夹` : (placeholder || '');
}

async function loadCaseFolders(key) {
  if (!key) { populatePfTargets(); return; }
  try {
    const r = await api(`/api/cases/${encodeURIComponent(key)}/folders`);
    if ($('pf-case').value !== key) return;
    if (r && r.folders && r.folders.length) {
      renderTargetOptions(r.folders, '', 'pf-target', 'pf-target-hint');
      renderTargetOptions(r.folders, '', 'pf-batch-target', 'pf-batch-target-hint');
    }
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
       ${i === _pfIdx ? `<span class="pf-current">${icon('chevron')}</span>` : '<span class="pf-current"></span>'}
       ${icon('file', 'pf-file-icon')} <span class="pf-file-path" title="${esc(relativePathOf(f))}">${esc(relativePathOf(f))}</span>${pfFilled(i) ? ` <span class="pf-done">${icon('check')}</span>` : ''}
     </div>`).join('');
}

function renderFolderContext(files) {
  const parents = [...new Set(files.map(f => relativePathOf(f).split('/').slice(0, -1).join('/')).filter(Boolean))];
  const el = $('pf-folder-context');
  if (!parents.length) {
    el.innerHTML = '<span>单层投放：没有可继承的文件夹线索。</span>';
    return;
  }
  const sample = parents.slice(0, 3).map(esc).join(' · ');
  el.innerHTML = `<strong>原始目录线索</strong><span>${parents.length} 个目录${sample ? ` · ${sample}` : ''}${parents.length > 3 ? ' …' : ''}</span><em>仅记录供复核，不会据此自动认定文件来源</em>`;
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

async function openPrefill(files, stagedItems = []) {
  _pendingFiles = files;
  _pfPer = files.map(() => ({}));
  _pfDecisions = files.map(() => null);
  _pendingStaged = stagedItems || [];
  _pfIdx = 0;
  if (!(_meta.cases || []).length && !(_meta.targets || []).length) await loadMeta();

  $('pf-case').innerHTML = '<option value="">（自动识别）</option>' +
    (_meta.cases || []).map(c => `<option value="${esc(c.key)}">${esc(c.key)} · ${esc(c.name)}</option>`).join('');
  $('pf-case').value = '';
  $('pf-country').value = '';
  $('pf-proc').value = '';
  $('pf-batch-source').value = '';
  $('pf-stage').value = '';
  const _pfDefaultDate = files[0]?.lastModified
    ? new Date(files[0].lastModified).toISOString().slice(0, 10)
    : new Date().toISOString().slice(0, 10);
  $('pf-date').value = _pfDefaultDate;
  populatePfTargets();
  $('pf-batch-target').value = '';
  populatePfParties('');
  populatePfAlso('');
  loadPfPer(0);                  // 清空逐项字段
  $('pf-multi').style.display = files.length > 1 ? '' : 'none';
  _lastDecision = null;
  $('pf-decision').innerHTML = '<div class="pf-decision-empty">文件仍处于暂存状态。点击“预览本批次”后，系统才会读取内容并生成案件、文书性质、来源、命名和路径建议。</div>';
  renderPfFiles();
  renderFolderContext(files);

  $('prefill-overlay').classList.add('open');
}

function batchPrefillMeta() {
  const m = {};
  if ($('pf-case').value)         m.case_key = $('pf-case').value;
  if ($('pf-batch-source').value) m.source = $('pf-batch-source').value;
  if ($('pf-stage').value)        m.stage = $('pf-stage').value;
  if ($('pf-date').value)         m.date = $('pf-date').value;
  if ($('pf-batch-target').value) m.target = $('pf-batch-target').value;
  if ($('pf-country').value)      m.country = $('pf-country').value;
  if ($('pf-proc').value)         m.procedure = $('pf-proc').value;
  return m;
}

function currentPrefillManual() {
  savePfPer(_pfIdx);
  const p = _pfPer[_pfIdx] || {};
  const manual = { ...batchPrefillMeta() };
  Object.assign(manual, p);
  return manual;
}

function renderPrefillDecision(d) {
  const pathUnavailable = d.destination?.health && !['ok', 'unknown'].includes(d.destination.health);
  const risk = pathUnavailable ? 'red' : (d.risk || 'red');
  const riskText = { green: '可提交', yellow: '建议复核', red: '需要确认' }[risk] || '需要确认';
  const matter = d.matter || {};
  const doc = d.document_type || {};
  const source = d.source || {};
  const selectedMatter = matter.selected ? `${matter.selected.key} · ${matter.selected.name}` : '未确定';
  const selectedDoc = doc.selected ? (doc.selected.target || doc.selected.name || '未确定') : '未确定';
  const sourceText = source.kind && source.kind !== 'unknown'
    ? ({ ours:'我方', opposing:'对方', court:'法院', tribunal:'仲裁机构',
         third_party:'第三方', expert:'专家 / 鉴定机构', police:'公安机关',
         prosecutor:'检察机关', regulator:'监管机构', internal:'内部材料' }[source.kind] || source.kind)
    : '未确定';
  const reasons = [];
  (matter.candidates?.[0]?.evidence || []).slice(0, 3).forEach(x => reasons.push(`案件：${x}`));
  (doc.selected?.evidence || []).slice(0, 3).forEach(x => reasons.push(`文书：${x}`));
  (source.evidence || []).slice(0, 3).forEach(x => reasons.push(`来源：${x}`));
  const candidateText = (matter.candidates || []).slice(0, 3)
    .map(x => `${x.key} · ${x.name}（${x.score ?? 0}）`).join('；');
  const docCandidateText = (doc.candidates || []).slice(0, 3)
    .map(x => `${x.name || x.target}（${x.score ?? 0}）`).join('；');
  const conflicts = [matter.conflict, doc.conflict, source.conflict]
    .filter(Boolean)
    .map(x => x.kind === 'matter_conflict' ? '案件候选冲突'
      : x.kind === 'document_type_conflict' ? '文书性质候选冲突' : '来源候选冲突');
  if (d.destination?.source_required) conflicts.push('来源未确认，不能进入业务文件夹');
  if (d.destination?.health && !['ok', 'unknown'].includes(d.destination.health)) conflicts.push('目标路径不可用');
  const manual = new Set(d.manual_fields || []);
  const manualMark = key => manual.has(key) ? '<span class="pf-manual-mark">人工确认</span>' : '';
  const pathRoot = d.destination?.case_root || '';
  const caseName = matter.selected?.name || '';
  const fullPath = d.destination?.relative && pathRoot && caseName
    ? [pathRoot.replace(/\/$/, ''), caseName, d.destination.relative].join('/') : '待确定';
  const previewText = d.file?.text_preview || '未提取到可预览的文本。PDF 可能是扫描件，或文档需要手动识别。';
  const stage = $('pf-stage')?.value || '使用案件当前阶段';
  const proc = $('pf-proc')?.value || '使用案件程序';
  const fileName = d.file?.original_name || _pendingFiles[_pfIdx]?.name || '';
  let warning = '';
  if (risk === 'red' || (d.destination?.health && !['ok', 'unknown'].includes(d.destination.health))) {
    const bits = [];
    if (matter.status !== 'selected' && matter.status !== 'manual') bits.push('案件');
    if (doc.status !== 'selected' && doc.status !== 'manual') bits.push('文书性质');
    if (d.destination?.pending_target || source.status === 'unknown' || source.status === 'ambiguous') bits.push('来源');
    if (d.destination?.health && d.destination.health !== 'ok' && d.destination.health !== 'unknown') bits.push('路径');
    if (d.destination?.health && !['ok', 'unknown'].includes(d.destination.health)) bits.push('案件归档路径');
    warning = `${bits.join('、') || '部分信息'}未达到自动归档阈值；不能直接提交归档。`;
  }
  $('pf-decision').innerHTML = `<div class="pf-decision-head">
    <div><span class="pf-decision-title">Filing Decision · 本地分析结果</span><span class="pf-decision-file" title="${esc(fileName)}">${esc(fileName)}</span></div>
    <div class="pf-decision-head-actions"><span class="pf-decision-risk pf-risk-${esc(risk)}">${esc(riskText)}</span><button class="icon-btn" onclick="analyzePrefill()">再次分析</button></div>
  </div>
  <div class="pf-decision-layout">
    <div class="pf-evidence-pane">
      <div class="pf-card-label">材料摘录 / TEXT PREVIEW</div>
      <pre class="pf-text-preview">${esc(previewText)}</pre>
      <div class="pf-evidence-block"><span class="pf-card-label">命中依据</span>${reasons.length ? reasons.map(x => `<div class="pf-evidence-line">${esc(x)}</div>`).join('') : '<div class="pf-muted">暂无明确依据</div>'}</div>
    </div>
    <div class="pf-facts-pane">
      <div class="pf-decision-grid">
        <div class="pf-decision-cell"><span class="pf-decision-label">案件</span><span class="pf-decision-value">${esc(selectedMatter)} ${manualMark('case_key')}</span></div>
        <div class="pf-decision-cell"><span class="pf-decision-label">程序 / 阶段</span><span class="pf-decision-value">${esc(proc)} · ${esc(stage)}</span></div>
        <div class="pf-decision-cell"><span class="pf-decision-label">文书性质</span><span class="pf-decision-value">${esc(selectedDoc)} ${manualMark('target')}</span></div>
        <div class="pf-decision-cell"><span class="pf-decision-label">来源</span><span class="pf-decision-value">${esc(sourceText)} ${source.party ? `· ${esc(source.party)}` : ''} ${manualMark('source')}</span></div>
        <div class="pf-decision-cell"><span class="pf-decision-label">目标文件夹</span><span class="pf-decision-value">${esc(d.destination?.relative || '未确定')}</span></div>
        <div class="pf-decision-cell"><span class="pf-decision-label">路径健康</span><span class="pf-decision-value">${esc({ok:'可写',missing:'不存在',not_directory:'不是文件夹',not_writable:'不可写',unknown:'待确定'}[d.destination?.health] || '待确定')}</span></div>
        <div class="pf-decision-cell pf-decision-wide"><span class="pf-decision-label">标准文件名</span><span class="pf-decision-value">${esc(d.naming?.preview || '待确认')}</span></div>
        <div class="pf-decision-cell pf-decision-wide"><span class="pf-decision-label">完整目标路径</span><span class="pf-decision-value pf-path-value">${esc(fullPath)}</span></div>
      </div>
      ${candidateText ? `<div class="pf-candidate-block"><span class="pf-card-label">案件候选</span><div>${esc(candidateText)}</div></div>` : ''}
      ${docCandidateText ? `<div class="pf-candidate-block"><span class="pf-card-label">文书候选</span><div>${esc(docCandidateText)}</div></div>` : ''}
    </div>
  </div>
  ${conflicts.length ? `<div class="pf-decision-conflicts"><span class="pf-card-label">待确认 / 冲突</span>${conflicts.map(x => `<span class="pf-conflict-chip">${esc(x)}</span>`).join('')}</div>` : ''}
  ${warning ? `<div class="pf-decision-warning">${esc(warning)}</div>` : ''}`;
}

async function analyzePrefill(options = {}) {
  const file = _pendingFiles[_pfIdx];
  if (!file) { toast('没有可分析的文件', true); return; }
  savePfPer(_pfIdx);
  const form = new FormData();
  form.append('file', file);
  form.append('meta', JSON.stringify(currentPrefillManual()));
  const btn = $('btn-analyze-prefill');
  btn.disabled = true; btn.textContent = '分析中…';
  $('pf-decision').innerHTML = '<div class="pf-decision-empty">正在本地读取文本 / OCR 并生成决策预览…</div>';
  try {
    const res = await fetch('/api/decision/analyze-file', { method: 'POST', body: form });
    const data = await res.json();
    if (!data.ok) { $('pf-decision').innerHTML = `<div class="pf-decision-warning">${esc(data.error || '分析失败')}</div>`; return; }
    _lastDecision = data.decision;
    _pfDecisions[_pfIdx] = _lastDecision;
    // Suggestions remain suggestions.  They are shown in the card but are
    // never silently copied into manual fields, otherwise a later re-analysis
    // would mistake the system's own guess for a human fact.
    renderPrefillDecision(_lastDecision);
    renderPfFiles();
  } catch (e) {
    $('pf-decision').innerHTML = `<div class="pf-decision-warning">分析失败：${esc(e.message)}</div>`;
  } finally {
    if (!options.batch) {
      btn.disabled = false; btn.textContent = '预览本批次';
    }
  }
}

async function previewPrefillBatch() {
  if (!_pendingFiles.length) { toast('没有可预览的文件', true); return; }
  if (previewPrefillBatch.running) return;
  previewPrefillBatch.running = true;
  const original = _pfIdx;
  const button = $('btn-analyze-prefill');
  button.disabled = true; button.textContent = '批次分析中…';
  try {
    for (let i = 0; i < _pendingFiles.length; i++) {
      _pfIdx = i;
      loadPfPer(i);
      await analyzePrefill({ batch: true });
    }
    _pfIdx = original;
    loadPfPer(original);
    _lastDecision = _pfDecisions[original] || null;
    if (_lastDecision) renderPrefillDecision(_lastDecision);
    toast(`已完成 ${_pfDecisions.filter(Boolean).length}/${_pendingFiles.length} 份决策预览`);
  } finally {
    previewPrefillBatch.running = false;
    button.disabled = false; button.textContent = '预览本批次';
    _pfIdx = original;
    loadPfPer(original);
    renderPfFiles();
  }
}

function decisionIsStale(d, i) {
  if (!d) return true;
  const now = { ...batchPrefillMeta(), ...(_pfPer[i] || {}) };
  const frozen = d.manual_values || {};
  return ['case_key', 'target', 'source', 'doc_type', 'party', 'date', 'batch', 'version', 'translation', 'stage'].some(key =>
    String(now[key] || '') !== String(frozen[key] || ''));
}

async function commitFrozenDecision(decision, saved, index, batchId) {
  const path = saved?.path;
  if (!path || !decision?.decision_id) return { ok: false, error: '缺少暂存文件或冻结决策' };
  const p = _pfPer[index] || {};
  const body = {
    path,
    new_name: p.doc_type ? (decision.naming?.preview || '') : '',
    batch_id: batchId,
  };
  const res = await api(`/api/decisions/${encodeURIComponent(decision.decision_id)}/commit`, {
    method: 'POST', body: JSON.stringify(body),
  });
  return res;
}

async function undoFailedBatch(batchId) {
  const state = _failedBatchStates.find(item => item.batch_id === batchId);
  if (state?.undoing) return;
  if (state) {
    state.undoing = true;
    persistFailedBatchStates();
    if (typeof renderStagingPanel === 'function') renderStagingPanel();
  }
  const res = await api(`/api/transactions/batches/${encodeURIComponent(batchId)}/undo`, {
    method: 'POST', body: JSON.stringify({}),
  });
  if (!res.ok) {
    if (state) {
      state.undoing = false;
      state.error = res.error || '批次撤销失败';
      persistFailedBatchStates();
      if (typeof renderStagingPanel === 'function') renderStagingPanel();
    }
    toast(res.error || '批次撤销失败，请保留当前文件并转入待复核', true);
    loadStaging();
    return false;
  }
  forgetFailedBatch(batchId);
  toast('本批次已撤销，文件和人工填写信息已恢复');
  await Promise.allSettled([loadStaging(), loadInbox(), loadReview(), loadCases()]);
  return true;
}

function keepFailedBatch(batchId) {
  const state = _failedBatchStates.find(item => item.batch_id === batchId);
  if (state) {
    state.kept = true;
    state.updated_at = new Date().toISOString();
  }
  persistFailedBatchStates();
  toast('已保留已提交文件；未提交文件仍在暂存区，可继续恢复处理');
  loadStaging();
}

async function handleBatchCommitFailure(result, batchId, committedCount) {
  if (result?.rolled_back) {
    forgetFailedBatch(batchId);
    toast('批量提交发生异常，已自动回滚；材料仍在暂存区，可重新分析后提交', true);
    await Promise.allSettled([loadStaging(), loadInbox(), loadReview(), loadCases()]);
    return;
  }
  if (!result?.can_undo && committedCount === 0) {
    toast(`批次预检未通过，未移动任何文件：${result?.error || '请修正后重试'}`, true);
    await Promise.allSettled([loadStaging(), loadInbox()]);
    return;
  }
  const failure = rememberFailedBatch({
    ...result, batch_id: batchId, committed_count: committedCount,
  });
  const reason = result?.error || '批量归档失败';
  const undoNow = confirm(
    `批量归档未完成。\n\n已提交：${committedCount} 份\n原因：${reason}\n\n` +
    '点击“确定”撤销本批次已提交文件并恢复人工信息；点击“取消”保留已提交文件。',
  );
  if (undoNow) {
    await undoFailedBatch(failure?.batch_id || batchId);
  } else {
    keepFailedBatch(failure?.batch_id || batchId);
  }
}

async function confirmPrefill() {
  savePfPer(_pfIdx);
  if (!_pendingFiles.length) return;
  const missing = _pendingFiles.findIndex((_, i) => !_pfDecisions[i]);
  if (missing >= 0) {
    _pfIdx = missing; loadPfPer(missing);
    toast(`请先点击“预览本批次”，完成第 ${missing + 1} 份文件的分析`, true);
    return;
  }
  const stale = _pfDecisions.findIndex((d, i) => decisionIsStale(d, i));
  if (stale >= 0) {
    _pfIdx = stale; loadPfPer(stale); renderPrefillDecision(_pfDecisions[stale]);
    toast(`第 ${stale + 1} 份文件的人工选择已变化，请再次分析后提交`, true);
    return;
  }
  const hasRed = _pfDecisions.some(d => d.risk !== 'green' ||
    (d.destination?.health && !['ok', 'unknown'].includes(d.destination.health)));
  const reusingStaged = _pendingStaged.length === _pendingFiles.length &&
    _pendingStaged.every(item => item && item.path);
  if (hasRed && reusingStaged) {
    toast('当前决策仍需补充人工信息；暂存文件保持不变，请先完成确认后再提交', true);
    return;
  }
  if (hasRed && !confirm('本批次存在红色或黄色决策，不能直接归档。是否仅放入收件箱，交由待复核队列处理？')) return;
  const batchMeta = batchPrefillMeta();
  const metas = _pendingFiles.map((f, i) => {
    const p = _pfPer[i] || {}, m = {};
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
  const decisions = [..._pfDecisions];
  const existingStaged = reusingStaged ? [..._pendingStaged] : [];
  closePrefill();
  if (!files.length) return;
  if (hasRed) {
    await uploadToInbox(files, batchMeta, metas);
    return;
  }
  const staged = existingStaged.length
    ? { ok: true, saved: existingStaged, batchId: existingStaged[0].batch_id }
    : await uploadToInbox(files, batchMeta, metas, { stageOnly: true });
  if (!staged.ok || staged.saved.length !== files.length) {
    toast('暂存未完整完成，未执行任何归档提交', true);
    return;
  }
  const batchItems = decisions.map((decision, i) => {
    const p = _pfPer[i] || {};
    return {
      decision_id: decision?.decision_id,
      path: staged.saved[i]?.path,
      target: p.target || '',
      new_name: p.doc_type ? (decision.naming?.preview || '') : '',
    };
  });
  const result = await api('/api/decisions/batch/commit', {
    method: 'POST', body: JSON.stringify({
      batch_id: staged.batchId, items: batchItems,
    }),
  });
  if (!result.ok) {
    await handleBatchCommitFailure(result, staged.batchId,
      Number(result.committed_count || 0));
    return;
  }
  forgetFailedBatch(staged.batchId);
  toast(`已确认归档 ${result.committed_count || decisions.length} 份`);
  await Promise.allSettled([loadStaging(), loadInbox(), loadReview(), loadCases()]);
}

function skipPrefill() {
  closePrefill();
  toast('已暂缓处理；文件尚未写入收件箱');
}

function closePrefill() {
  $('prefill-overlay').classList.remove('open');
  _pendingFiles = [];
  _pfDecisions = [];
  _pendingStaged = [];
  _lastDecision = null;
}

function tagRelativePath(file, path) {
  const clean = String(path || file.name).replace(/^\/+/, '');
  try { Object.defineProperty(file, '_lociRelativePath', { value: clean, configurable: true }); }
  catch { file._lociRelativePath = clean; }
  return file;
}

function readEntryFile(entry) {
  return new Promise((resolve, reject) => entry.file(
    file => resolve(tagRelativePath(file, entry.fullPath || file.name)), reject,
  ));
}

function readDirectoryEntries(reader) {
  return new Promise((resolve, reject) => {
    const all = [];
    const next = () => reader.readEntries(entries => {
      if (!entries.length) { resolve(all); return; }
      all.push(...entries); next();
    }, reject);
    next();
  });
}

async function filesFromEntry(entry) {
  if (entry.isFile) return [await readEntryFile(entry)];
  if (!entry.isDirectory) return [];
  const children = await readDirectoryEntries(entry.createReader());
  const nested = await Promise.all(children.map(filesFromEntry));
  return nested.flat();
}

async function filesFromDrop(dataTransfer) {
  const entries = [...(dataTransfer.items || [])]
    .filter(item => item.kind === 'file')
    .map(item => item.webkitGetAsEntry ? item.webkitGetAsEntry() : null)
    .filter(Boolean);
  if (entries.length) {
    const groups = await Promise.all(entries.map(filesFromEntry));
    return groups.flat().filter(file => file && file.name !== '.DS_Store');
  }
  return [...(dataTransfer.files || [])]
    .filter(file => file.name !== '.DS_Store')
    .map(file => tagRelativePath(file, file.webkitRelativePath || file.name));
}

// ── Inbox ──────────────────────────────────────────────────────
async function loadInbox() {
  loadStaging();
  const data = await api('/api/inbox');
  const list = $('inbox-list');
  if (!data.files.length) {
    list.innerHTML = `<div class="inbox-empty-drop">
      <div class="inbox-drop-icon">${icon('inbox', 'ui-icon-xl')}</div>
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

let _stagingData = [];

function renderStagingPanel() {
  const panel = $('staging-panel');
  const list = $('staging-list');
  if (!panel || !list) return;
  const failed = _failedBatchStates.filter(item => item && item.batch_id);
  const failedHtml = failed.map(item => `
    <div class="staging-row staging-recovery-row">
      <span class="file-ext">批次</span>
      <span class="staging-name" title="${esc(item.error || '')}">归档未完成：${esc(item.batch_id)}</span>
      <span class="staging-meta">已提交 ${Number(item.committed_count || 0)} 份 · ${esc(item.phase || 'commit')}</span>
      <div class="rule-actions">
        <button class="icon-btn" onclick="undoFailedBatch('${esc(item.batch_id)}')" ${item.undoing ? 'disabled' : ''}>${item.undoing ? '撤销中…' : '撤销本批次'}</button>
        <button class="icon-btn" onclick="keepFailedBatch('${esc(item.batch_id)}')">保留已提交</button>
      </div>
    </div>`).join('');
  const fileHtml = _stagingData.map((item, i) => `
    <div class="staging-row">
      <span class="file-ext">${esc((item.name.split('.').pop() || 'file').slice(0, 4))}</span>
      <span class="staging-name" title="${esc(item.path)}">${esc(item.name)}</span>
      <span class="staging-meta">${esc(item.batch_id)} · ${esc(item.mtime)} · ${fmtBytes(item.size)}</span>
      <div class="rule-actions">
        <button class="icon-btn" onclick="restoreStaged(${i})">恢复决策</button>
        <button class="icon-btn danger-text" onclick="discardStaged(${i})">放弃</button>
      </div>
    </div>`).join('');
  list.innerHTML = failedHtml + fileHtml;
  panel.style.display = (failed.length || _stagingData.length) ? '' : 'none';
}

async function loadStaging() {
  const panel = $('staging-panel');
  if (!panel) return;
  try {
    const data = await api('/api/inbox/staging');
    _stagingData = data.items || [];
    $('staging-count').textContent = _stagingData.length ? `${_stagingData.length} 份` : '';
    renderStagingPanel();
  } catch (e) {
    panel.style.display = '';
    $('staging-list').innerHTML = `<div class="empty">暂存列表加载失败：${esc(e.message)}</div>`;
  }
}

async function restoreStaged(i) {
  const item = _stagingData[i];
  if (!item) return;
  try {
    const res = await fetch(`/api/inbox/staging/file?path=${encodeURIComponent(item.path)}`);
    if (!res.ok) throw new Error((await res.json()).error || '暂存文件不存在');
    const blob = await res.blob();
    const file = new File([blob], item.name, { type: blob.type || 'application/octet-stream', lastModified: Date.now() });
    tagRelativePath(file, item.name);
    await openPrefill([file], [item]);
  } catch (e) {
    toast('恢复暂存失败：' + e.message, true);
  }
}

async function discardStaged(i) {
  const item = _stagingData[i];
  if (!item) return;
  const sameBatch = _stagingData.filter(x => x.batch_id === item.batch_id).length;
  if (!confirm(`放弃暂存批次 ${item.batch_id}？将删除其中 ${sameBatch} 份文件，且无法撤销。`)) return;
  const res = await api('/api/inbox/staging/discard', {
    method: 'POST', body: JSON.stringify({ batch_id: item.batch_id }),
  });
  if (!res.ok) { toast(res.error || '放弃暂存失败', true); return; }
  toast('暂存批次已放弃');
  loadStaging();
}

// ── Cases ──────────────────────────────────────────────────────
let _casesData = [];
const CASE_ROLE_LABELS = {
  plaintiff: '原告方', defendant: '被告方', 'third-party': '第三人方',
  claimant: '仲裁申请人', respondent: '仲裁被申请人',
  suspect: '嫌疑人 / 被告人方', complainant: '被害人 / 报案人方',
};

async function loadCases() {
  const data = await api('/api/cases');
  _casesData = data.cases || [];
  const list = $('cases-list');
  if (!_casesData.length) {
    list.innerHTML = '<div class="empty">尚无案件 — 点击「新建案件」开始</div>';
    return;
  }
  list.innerHTML = _casesData.map((c, i) => {
    const tags = (c.keywords || []).slice(0, 5)
      .map(k => `<span class="tag">${esc(k)}</span>`).join('');
    const scenario = c.scenario || '程序类型未填写';
    const stage = c.stage || '阶段未填写';
    const subject = c.subject || c.cause || '案由未填写';
    const role = (c.role || '').split('+').map(x => CASE_ROLE_LABELS[x] || x).join(' + ');
    const ourParties = (c.party_us || []).filter(Boolean).slice(0, 2).join('、');
    const opposingParties = (c.party_op || []).filter(Boolean).slice(0, 2).join('、');
    const archivePath = c.cases_dir || '';
    const archiveLabel = archivePath
      ? archivePath.split('/').filter(Boolean).slice(-2).join(' / ')
      : '默认案件库';
    const closed = c.status === 'closed';
    const status = closed ? '已关闭' : '在办案件';
    return `<article class="case-card${closed ? ' case-closed' : ''}">
      <div class="case-card-head">
        <div class="case-card-title-block">
          <div class="case-card-kicker"><span class="case-status-dot"></span>${esc(status)}<span class="case-card-divider">/</span><span class="case-card-key" title="${esc(c.key)}">${esc(c.key)}</span></div>
          <h3 class="case-card-title" title="${esc(c.name)}">${esc(c.name)}</h3>
        </div>
        ${c.case_number ? `<div class="case-card-number" title="${esc(c.case_number)}">${esc(c.case_number)}</div>` : ''}
      </div>

      <div class="case-card-facts">
        <div class="case-fact"><span class="case-fact-label">程序</span><strong>${esc(scenario)}</strong></div>
        <div class="case-fact"><span class="case-fact-label">当前阶段</span><strong>${esc(stage)}</strong></div>
        <div class="case-fact"><span class="case-fact-label">案由</span><strong title="${esc(subject)}">${esc(subject)}</strong></div>
        ${role ? `<div class="case-fact"><span class="case-fact-label">我方角色</span><strong>${esc(role)}</strong></div>` : ''}
      </div>

      ${(ourParties || opposingParties) ? `<div class="case-card-parties">
        ${ourParties ? `<div class="case-party"><span class="case-fact-label">我方主体</span><span title="${esc(ourParties)}">${esc(ourParties)}</span></div>` : ''}
        ${opposingParties ? `<div class="case-party"><span class="case-fact-label">对方主体</span><span title="${esc(opposingParties)}">${esc(opposingParties)}</span></div>` : ''}
      </div>` : ''}

      <div class="case-card-keywords">${tags || '<span class="case-card-muted">尚无案件关键词</span>'}</div>

      <div class="case-card-foot">
        <div class="case-card-path" title="${esc(archivePath || '使用默认案件路径')}">
          ${icon('folder')}<span>${esc(archiveLabel)}</span>
        </div>
        <div class="case-card-actions">
        <button class="icon-btn" onclick="openCaseFolderDirect(${i})" title="在 Finder 中打开">打开</button>
        <button class="icon-btn" onclick="showFileTree(${i})" title="查看材料清单">材料</button>
        <button class="icon-btn" onclick="desensitizeCase(${i})" title="整案脱敏，生成脱敏版案件空间">脱敏</button>
        <button class="icon-btn" onclick="openCaseEditor(${i})" title="维护案件信息">维护</button>
        <button class="icon-btn" onclick="toggleCaseStatus(${i})" title="${closed ? '重新激活' : '关闭案件（停止自动归档）'}">${closed ? '激活' : '关闭'}</button>
        </div>
      </div>
    </article>`;
  }).join('');
}

// ── 整案脱敏 → 脱敏版案件空间 ──────────────────────────────────
let _desensTimer = null;

async function desensitizeCase(idx) {
  const c = _casesData[idx];
  if (!confirm(`将对案件「${c.name}」整案脱敏，生成「脱敏版」到 cases_脱敏/${c.name}/（归类层次一致、全案化名一致）。\n大案可能需数分钟，确定开始？`)) return;
  const res = await api(`/api/cases/${encodeURIComponent(c.key)}/desensitize`, { method: 'POST' });
  if (!res.ok) { toast(res.error, true); return; }
  toast('脱敏已开始…');
  clearInterval(_desensTimer);
  _desensTimer = setInterval(async () => {
    const s = await api('/api/desensitize/status');
    if (s.running) {
      toast(`脱敏中 ${s.i}/${s.total}… ${s.current.split('/').pop()}`);
    } else if (s.done) {
      clearInterval(_desensTimer);
      if (s.error) { toast('脱敏失败：' + s.error, true); return; }
      const r = s.report || {};
      toast(`完成：处理 ${r.processed} 份 · 命中 ${r.hits} · 实体 ${r.entities} · cases_脱敏/${s.case}`);
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
  if (res.ok) { toast(closing ? '案件已关闭' : '案件已激活'); loadCases(); }
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
          <span class="ft-toggle${startCollapsed ? ' is-collapsed' : ''}">${icon('chevron')}</span>
          <span class="ft-icon">${icon(isEmpty ? 'folderOpen' : 'folder')}</span>
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
  if (['pdf'].includes(ext)) return icon('pdf');
  if (['doc', 'docx'].includes(ext)) return icon('doc');
  if (['xls', 'xlsx'].includes(ext)) return icon('sheet');
  if (['ppt', 'pptx'].includes(ext)) return icon('doc');
  if (['jpg', 'jpeg', 'png', 'gif', 'tiff', 'webp'].includes(ext)) return icon('image');
  if (['mp3', 'wav', 'm4a', 'aac', 'mp4', 'mov', 'avi', 'mkv'].includes(ext)) return icon('media');
  if (['zip', 'rar', '7z', 'tar'].includes(ext)) return icon('archive');
  return icon('file');
}

function toggleDir(labelEl) {
  const node     = labelEl.parentElement;
  const children = node.querySelector('.ft-children');
  const toggle   = labelEl.querySelector('.ft-toggle');
  const collapsed = node.classList.contains('ft-collapsed');
  node.classList.toggle('ft-collapsed', !collapsed);
  children.style.display = collapsed ? '' : 'none';
  toggle.classList.toggle('is-collapsed', !collapsed);
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
  $('ce-number').value      = Array.isArray(c.case_number) ? c.case_number.join('；') : (c.case_number || '');
  $('ce-scenario').value    = c.scenario    || '';
  $('ce-stage').value       = c.stage       || '';
  $('stage-create-status').textContent = '';
  $('ce-cause').value       = c.cause || c.subject || '';
  $('ce-role').value        = c.role        || '';
  $('ce-status').value      = c.status === 'closed' ? 'closed' : 'active';
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
      stage:       $('ce-stage').value,
      cause:       $('ce-cause').value.trim(),
      role:        $('ce-role').value,
      status:      $('ce-status').value,
      cases_dir:   $('ce-path').value.trim(),
      party_us:    splitParties('ce-party-us'),
      party_op:    splitParties('ce-party-op'),
      party_third: splitParties('ce-party-third'),
      party_court: splitParties('ce-party-court'),
    }),
  });
  if (res.ok) {
    toast('案件已更新');
    closeCaseModal();
    loadCases();
  } else {
    toast('保存失败: ' + (res.error || ''), true);
  }
}

async function createSelectedStage() {
  const key = $('ce-key').value;
  const stage = $('ce-stage').value;
  const btn = $('btn-create-stage');
  const status = $('stage-create-status');
  if (!key || !stage) {
    toast('请先选择程序阶段', true);
    return;
  }
  if (!window.confirm(`为案件创建“${stage}”阶段目录？\n\n系统只增加缺失目录，不移动或重命名现有材料。`)) return;
  btn.disabled = true;
  status.textContent = '正在检查并创建目录…';
  try {
    const res = await api(`/api/cases/${encodeURIComponent(key)}/stages`, {
      method: 'POST',
      body: JSON.stringify({ stage }),
    });
    if (!res.ok) {
      status.textContent = res.error || '阶段目录创建失败';
      toast(status.textContent, true);
      return;
    }
    status.textContent = res.count
      ? `已新增 ${res.count} 个目录；既有材料未改动。`
      : '该阶段目录已经存在，无需重复创建。';
    toast(status.textContent);
    await loadCases();
  } catch (error) {
    status.textContent = '阶段目录创建失败：' + error.message;
    toast(status.textContent, true);
  } finally {
    btn.disabled = false;
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

const STAGE_OPTS = {
  civil: [
    { v: '一审', l: '一审 / First instance' },
    { v: '二审', l: '二审 / Appeal' },
    { v: '再审', l: '再审 / Retrial' },
    { v: '执行', l: '执行 / Enforcement' },
  ],
  arbitration: [
    { v: '仲裁', l: '仲裁 / Arbitration' },
    { v: '司法审查', l: '司法审查 / Set-aside review' },
    { v: '执行', l: '执行 / Enforcement' },
  ],
  criminal: [
    { v: '侦查', l: '侦查 / Investigation' },
    { v: '审查起诉', l: '审查起诉 / Prosecution review' },
    { v: '一审', l: '一审 / Trial' },
    { v: '二审', l: '二审 / Appeal' },
    { v: '再审', l: '再审 / Retrial' },
    { v: '执行', l: '执行 / Enforcement' },
  ],
  us_civil: [
    { v: 'Pleadings', l: 'Pleadings / 诉答阶段' },
    { v: 'Discovery', l: 'Discovery / 证据开示' },
    { v: 'Trial', l: 'Trial / 审判' },
    { v: 'Appeal', l: 'Appeal / 上诉' },
  ],
  mixed: [
    { v: '民事一审 + 刑事侦查', l: '民事一审 + 刑事侦查' },
    { v: '民事二审 + 刑事侦查', l: '民事二审 + 刑事侦查' },
    { v: '刑事审查起诉', l: '刑事审查起诉' },
  ],
};

let _intake = {};

function resetIntake() { _intake = {}; renderIntake(); }

function collectFields() {
  const g = id => { const el = $(id); return el ? el.value.trim() : ''; };
  if ($('f-name'))   _intake.case_name   = g('f-name');
  if ($('f-abbr'))   _intake.abbr        = g('f-abbr');
  if ($('f-number')) _intake.case_number = g('f-number');
  if ($('f-cause')) _intake.cause       = g('f-cause');
  if ($('f-stage')) {
    const selected = g('f-stage');
    _intake.stage = selected === '__custom__' ? g('f-stage-custom') : selected;
  }
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

function toggleCustomStage(value) {
  const input = $('f-stage-custom');
  if (!input) return;
  input.style.display = value === '__custom__' ? '' : 'none';
  if (value !== '__custom__') _intake.stage = value;
}

function intakeStageField(pt) {
  const options = STAGE_OPTS[pt] || [];
  const known = options.some(item => item.v === _intake.stage);
  const selected = known ? _intake.stage : (_intake.stage ? '__custom__' : '');
  const optionHtml = options.map(item =>
    `<option value="${esc(item.v)}"${item.v === selected ? ' selected' : ''}>${esc(item.l)}</option>`
  ).join('');
  return `<select id="f-stage" class="select-input" onchange="toggleCustomStage(this.value)">
      <option value="">初始阶段（默认）/ 只创建当前阶段目录</option>
      ${optionHtml}
      <option value="__custom__"${selected === '__custom__' ? ' selected' : ''}>其他阶段…</option>
    </select>
    <input type="text" id="f-stage-custom" class="stage-custom-input"
      style="margin-top:8px;display:${selected === '__custom__' ? '' : 'none'}"
      placeholder="输入自定义阶段，例如：一审庭前 / Pre-trial"
      value="${selected === '__custom__' ? esc(_intake.stage || '') : ''}">`;
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
          <label>当前阶段 <span class="hint-sm">只创建当前阶段；进入后续程序时再增量添加</span></label>
          ${intakeStageField(pt)}
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
          <input type="text" id="f-path" placeholder="~/OneDrive/Cases" value="${esc(_intake.cases_dir||'')}">
        </div>
      </div>
      <div class="field">
        <label>备注（可选）</label>
        <input type="text" id="f-notes" placeholder="案件特殊情况、注意事项…" value="${esc(_intake.notes||'')}">
      </div>
    </div>

    ${pt && rl ? renderPreview() : ''}

    <div class="intake-submit">
      <button class="btn btn-ghost" onclick="previewIntake()">预览建案</button>
      <button class="btn btn-blue" onclick="submitIntake()">创建案件</button>
      <button class="btn btn-ghost" onclick="resetIntake()">重置</button>
    </div>
  `;
}

function validateIntakeClient() {
  const checks = [
    [_intake.case_name, '请填写案件名称'],
    [_intake.abbr,      '请填写文件简称'],
    [_intake.proc_type, '请选择程序类型'],
    [_intake.role,      '请选择我方角色'],
  ];
  for (const [value, message] of checks) {
    if (!value) { toast(message, true); return false; }
  }
  if (/[\\/:*?"<>|：\s]/.test(_intake.abbr)) {
    toast('简称不能含空格或 / \\ : * ? 等字符（它会进每个文件名）', true);
    return false;
  }
  return true;
}

async function previewIntake() {
  collectFields();
  if (!validateIntakeClient()) return;
  toast('正在生成建案预览…');
  const res = await api('/api/intake/preview', {
    method: 'POST', body: JSON.stringify({ ..._intake, parties_other: [] }),
  });
  if (!res.ok) {
    showModal('建案预览失败', res.error || '无法生成预览');
    toast('预览失败', true);
    return;
  }
  const health = res.path_health === 'available' ? '案件根路径已存在'
    : '案件根路径不存在，创建时将按填写路径创建';
  const folders = (res.folders || []).join('\n  ');
  showModal('建案预览', [
    '以下内容仅为预览，尚未写入配置或创建目录。',
    '',
    `案件：${res.case.name}`,
    `简称：${res.case.abbr}`,
    `程序：${res.case.scenario}`,
    `当前阶段：${res.case.stage || '默认初始阶段'}`,
    `路径：${res.path}`,
    `路径状态：${health}`,
    `将创建目录：${res.folder_count} 个`,
    '',
    '目录清单：',
    `  ${folders || '（无）'}`,
    '',
    '确认无误后，点击“创建案件”。',
  ].join('\n'));
}

async function submitIntake() {
  collectFields();
  if (!validateIntakeClient()) return;

  toast('正在创建…');
  const res = await api('/api/intake', {
    method: 'POST',
    body: JSON.stringify({ ..._intake, parties_other: [] }),
  });

  if (res.ok) {
    const kwLine = res.keywords && res.keywords.length
      ? `\n关键词: ${res.keywords.join(' · ')}` : '';
    showModal('案件创建成功', `路径: ${res.path}${kwLine}\n\n案件已写入 config.yaml，现在可以将文件拖入收件箱开始归档。`);
    toast('案件已创建');
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
let _editCkw    = [];
let _rulesValidation = null;
let _rulesBaseDigest = '';
let _ruleScenario = '中国民事';
const RULE_SCENARIOS = [
  { key: '中国民事', label: '中国民事' },
  { key: '仲裁', label: '仲裁' },
  { key: '中国刑事', label: '中国刑事' },
  { key: '美国民事', label: '美国民事' },
  { key: '刑民交叉', label: '刑民交叉' },
  { key: '通用', label: '通用规则' },
];

function ruleScenarioOf(rule) {
  return (rule && rule.scenario) || '通用';
}

function renderRuleScenarioTabs() {
  const tabs = $('rule-scenario-tabs');
  if (!tabs) return;
  const counts = _rules.reduce((acc, rule) => {
    const key = ruleScenarioOf(rule);
    acc[key] = (acc[key] || 0) + 1;
    return acc;
  }, {});
  tabs.innerHTML = RULE_SCENARIOS.map(item => `
    <button type="button" class="rule-scenario-tab${item.key === _ruleScenario ? ' active' : ''}"
            data-scenario="${esc(item.key)}" aria-pressed="${item.key === _ruleScenario}">
      <span>${esc(item.label)}</span><span class="rule-scenario-count">${counts[item.key] || 0}</span>
    </button>`).join('');
  const hint = $('rules-scope-hint');
  if (hint) {
    if (_ruleScenario === '通用') {
      hint.textContent = '通用规则会参与所有案件类型的归档。请使用跨场景都成立的稳定特征，避免加入某一程序独有的宽泛关键词。';
    } else if (_ruleScenario === '刑民交叉') {
      hint.textContent = '当前维护刑民交叉案件的专属规则。实际归档时还会同时调用中国民事、中国刑事和通用规则。';
    } else {
      hint.textContent = `当前仅显示“${_ruleScenario}”专属规则；实际归档时还会自动调用通用规则。排序只在当前案件类型内调整。`;
    }
  }
}

function selectRuleScenario(scenario) {
  if (!RULE_SCENARIOS.some(item => item.key === scenario)) return;
  _ruleScenario = scenario;
  $('rules-search').value = '';
  renderRuleScenarioTabs();
  renderRules('');
}

async function loadRules() {
  const data = await api('/api/rules');
  _rules = data.rules || [];
  _rulesValidation = data.validation || null;
  _rulesBaseDigest = data.digest || _rulesValidation?.digest || '';
  _rulesDirty = false;
  renderRuleValidation(_rulesValidation);
  renderRuleScenarioTabs();
  renderRules();
}

function renderRuleValidation(v) {
  const el = $('rules-validation');
  if (!el || !v) return;
  const errors = v.errors || [], warnings = v.warnings || [];
  const stats = v.stats || {};
  const digest = (v.digest || '').slice(0, 12);
  if (errors.length) {
    el.className = 'rule-validation bad';
    el.innerHTML = `校验失败：${esc(errors.slice(0, 2).join('；'))}${errors.length > 2 ? ` 等 ${errors.length} 项` : ''}`;
  } else if (warnings.length) {
    el.className = 'rule-validation warn';
    el.innerHTML = `${stats.count || 0} 条规则 · ${warnings.length} 项维护提示 · 版本 ${esc(digest)}`;
  } else {
    el.className = 'rule-validation good';
    el.innerHTML = `${stats.count || 0} 条规则通过校验 · 版本 ${esc(digest)}`;
  }
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

  const scoped = _rules.map((r, i) => ({ r, i }))
    .filter(({ r }) => ruleScenarioOf(r) === _ruleScenario);
  const visible = scoped.filter(({ r }) => {
    if (!filter) return true;
    const hay = [r.name, r.target, r.scenario, ...(r.keywords || []),
      ...(r.title_keywords || []), ...(r.content_keywords || [])]
      .join(' ').toLowerCase();
    return hay.includes(filter);
  });

  if (!visible.length) {
    list.innerHTML = filter
      ? `<div class="empty">“${esc(_ruleScenario)}”中没有匹配规则</div>`
      : `<div class="empty">“${esc(_ruleScenario)}”尚无专属规则 — 点击「新建」添加</div>`;
    return;
  }

  const SRC_LABEL = { court: '法院', ours: '我方', opposing: '对方' };
  list.innerHTML = visible.map(({ r, i }) => {
    const scopedPos = scoped.findIndex(item => item.i === i);
    const kwTotal   = (r.keywords || []).length + (r.title_keywords || []).length + (r.content_keywords || []).length;
    const dispName  = r.name || (r.target || '').split('/').pop() || '（未命名）';
    const srcBadge  = r.source
      ? `<span class="rule-source ${r.source}">${SRC_LABEL[r.source] || r.source}</span>` : '';
    const scenarioBadge = r.scenario && r.scenario !== '通用'
      ? `<span class="rule-scenario">${esc(r.scenario)}</span>` : '';
    return `<div class="rule-row">
      <span class="rule-idx">${scopedPos + 1}</span>
      <span class="rule-name" title="${esc(dispName)}">${esc(dispName)}</span>
      <span class="rule-folder" title="${esc(r.target || '')}">${esc(r.target || '')}</span>
      ${scenarioBadge}
      ${srcBadge}
      <span class="rule-kw-count">${kwTotal} 词</span>
      <div class="rule-actions">
        <button class="icon-btn" onclick="moveRule(${i},-1)" ${scopedPos===0?'disabled':''} title="在当前案件类型内上移">↑</button>
        <button class="icon-btn" onclick="moveRule(${i},1)"  ${scopedPos===scoped.length-1?'disabled':''} title="在当前案件类型内下移">↓</button>
        <button class="icon-btn" onclick="openRuleEditor(${i})">编辑</button>
        <button class="icon-btn danger" onclick="deleteRule(${i})">删除</button>
      </div>
    </div>`;
  }).join('');
}

function moveRule(idx, dir) {
  const scenario = ruleScenarioOf(_rules[idx]);
  const scopedIndices = _rules.map((rule, i) => ({ rule, i }))
    .filter(item => ruleScenarioOf(item.rule) === scenario)
    .map(item => item.i);
  const position = scopedIndices.indexOf(idx);
  const to = scopedIndices[position + dir];
  if (position < 0 || to === undefined) return;
  [_rules[idx], _rules[to]] = [_rules[to], _rules[idx]];
  _rulesDirty = true;
  renderRules();
}

function deleteRule(idx) {
  if (!confirm(`删除规则「${_rules[idx].name || idx}」？`)) return;
  _rules.splice(idx, 1);
  _rulesDirty = true;
  renderRuleScenarioTabs();
  renderRules();
}

function addNewRule() { openRuleEditor(-1); }

function openRuleEditor(idx) {
  const r = idx >= 0 ? _rules[idx] : {};
  $('re-idx').value = idx;
  $('rule-modal-title').textContent = idx < 0 ? '新建规则' : '编辑规则';
  $('re-name').value   = r.name   || '';
  $('re-folder').value = r.target || '';
  $('re-scenario').value = r.scenario || _ruleScenario;
  $('re-source').value = r.source || '';
  _editKw  = [...(r.keywords       || [])];
  _editTkw = [...(r.title_keywords || [])];
  _editCkw = [...(r.content_keywords || [])];
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
  $('re-ckw-tags').innerHTML = _editCkw.length
    ? _editCkw.map((k, i) =>
        `<span class="kw-tag">${esc(k)}<button class="kw-tag-x" onclick="removeKw('ckw',${i})">×</button></span>`
      ).join('')
    : empty;
}

function addKw(type) {
  const inputId = type === 'kw' ? 're-kw-input' : type === 'tkw' ? 're-tkw-input' : 're-ckw-input';
  const vals = $(inputId).value.split(/[,，\n]+/).map(s => s.trim()).filter(Boolean);
  vals.forEach(v => {
    if (type === 'kw'  && !_editKw.includes(v))  _editKw.push(v);
    if (type === 'tkw' && !_editTkw.includes(v)) _editTkw.push(v);
    if (type === 'ckw' && !_editCkw.includes(v)) _editCkw.push(v);
  });
  $(inputId).value = '';
  renderKwTags();
}

function removeKw(type, idx) {
  if (type === 'kw')  _editKw.splice(idx, 1);
  else if (type === 'tkw') _editTkw.splice(idx, 1);
  else                _editCkw.splice(idx, 1);
  renderKwTags();
}

function confirmRule() {
  const kwPending  = $('re-kw-input').value.trim();
  const tkwPending = $('re-tkw-input').value.trim();
  const ckwPending = $('re-ckw-input').value.trim();
  if (kwPending)  { $('re-kw-input').value  = kwPending;  addKw('kw');  }
  if (tkwPending) { $('re-tkw-input').value = tkwPending; addKw('tkw'); }
  if (ckwPending) { $('re-ckw-input').value = ckwPending; addKw('ckw'); }

  const name   = $('re-name').value.trim();
  const folder = $('re-folder').value.trim();
  if (!name)   { toast('请填写规则名称', true);   return; }
  if (!folder) { toast('请填写目标文件夹', true); return; }

  const idx  = parseInt($('re-idx').value);
  const rule = {
    ...(idx >= 0 ? _rules[idx] : {}),
    target: folder,
    scenario: $('re-scenario').value || '通用',
    keywords: [..._editKw],
  };
  if (name) rule.name = name;
  else      delete rule.name;
  const src = $('re-source').value;
  if (src) rule.source = src;
  else     delete rule.source;
  if (_editTkw.length) rule.title_keywords = [..._editTkw];
  else                 delete rule.title_keywords;
  if (_editCkw.length) rule.content_keywords = [..._editCkw];
  else                 delete rule.content_keywords;

  if (idx < 0) _rules.push(rule);
  else         _rules[idx] = rule;

  _ruleScenario = rule.scenario;
  _rulesDirty = true;
  closeRuleModal();
  renderRuleScenarioTabs();
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
    body: JSON.stringify({ rules: _rules, base_digest: _rulesBaseDigest }),
  });
  if (res.ok) {
    _rulesDirty = false;
    _rulesBaseDigest = res.digest || _rulesBaseDigest;
    _rulesValidation = res.validation || _rulesValidation;
    renderRuleValidation(_rulesValidation);
    renderRuleScenarioTabs();
    renderRules();
    toast(res.warning ? '规则已保存，但历史记录失败' : '规则已保存');
    if (res.warning) showModal('规则保存提示', res.warning);
  } else {
    const detail = res.validation?.errors?.slice(0, 2).join('；');
    toast('保存失败: ' + (detail || res.error || ''), true);
    if (res.validation) renderRuleValidation(res.validation);
  }
}

// ── Rule sandbox / version history ──────────────────────────
async function openRuleSandbox() {
  if (!(_meta.cases || []).length) await loadMeta();
  $('rs-case').innerHTML = '<option value="">不限定，测试案件识别</option>' +
    (_meta.cases || []).map(c => `<option value="${esc(c.key)}">${esc(c.key)} · ${esc(c.name)}</option>`).join('');
  $('rule-sandbox-result').innerHTML = '<div class="pf-decision-empty">填写内容后点击“运行分析”。</div>';
  $('rule-sandbox-overlay').classList.add('open');
  setTimeout(() => $('rs-filename').focus(), 60);
}

function closeRuleSandbox() { $('rule-sandbox-overlay').classList.remove('open'); }

function renderSandboxDecision(d) {
  const risk = d.risk || 'red';
  const riskText = {green:'建议归档', yellow:'需要注意', red:'需要人工确认'}[risk] || risk;
  const matter = d.matter?.selected;
  const doc = d.document_type?.selected;
  const src = d.source || {};
  const sourceText = {ours:'我方', opposing:'对方', court:'法院 / 仲裁机构', unknown:'未知'}[src.kind] || src.kind || '未知';
  const reasons = [];
  (matter?.evidence || []).slice(0, 2).forEach(x => reasons.push(x));
  (doc?.evidence || []).slice(0, 3).forEach(x => reasons.push(x));
  (src.evidence || []).slice(0, 2).forEach(x => reasons.push(x));
  const candidates = (d.document_type?.candidates || []).slice(0, 4).map(x =>
    `${x.name || x.target} (${x.score})`).join('；');
  const warning = risk === 'red'
    ? `案件：${d.matter?.status || '待确认'} · 文书：${d.document_type?.status || '待确认'} · 来源：${src.status || '待确认'}`
    : (src.status === 'unknown' ? '来源尚未确定；如目标目录按来源分流，请人工选择来源。' : '');
  $('rule-sandbox-result').innerHTML = `<div class="pf-decision-head">
    <span class="pf-decision-title">试判结果</span>
    <span class="pf-decision-risk pf-risk-${esc(risk)}">${esc(riskText)}</span>
  </div>
  <div class="pf-decision-grid">
    <div class="pf-decision-cell"><span class="pf-decision-label">案件</span><span class="pf-decision-value">${esc(matter ? `${matter.key} · ${matter.name}` : '未确定')}</span></div>
    <div class="pf-decision-cell"><span class="pf-decision-label">文书</span><span class="pf-decision-value">${esc(doc?.name || '未确定')}</span></div>
    <div class="pf-decision-cell"><span class="pf-decision-label">来源</span><span class="pf-decision-value">${esc(sourceText)}</span></div>
    <div class="pf-decision-cell"><span class="pf-decision-label">目标</span><span class="pf-decision-value">${esc(d.destination?.relative || '未确定')}</span></div>
    <div class="pf-decision-cell"><span class="pf-decision-label">命名预览</span><span class="pf-decision-value">${esc(d.naming?.preview || '待确认')}</span></div>
    <div class="pf-decision-cell"><span class="pf-decision-label">路径健康</span><span class="pf-decision-value">${esc(d.destination?.health || 'unknown')}</span></div>
  </div>
  ${reasons.length ? `<div class="pf-decision-reason">依据：${esc(reasons.join('；'))}</div>` : ''}
  ${candidates ? `<div class="pf-decision-reason">文书候选：${esc(candidates)}</div>` : ''}
  ${warning ? `<div class="pf-decision-warning">${esc(warning)}</div>` : ''}`;
}

async function runRuleSandbox() {
  const filename = $('rs-filename').value.trim();
  if (!filename) { toast('请填写文件名', true); $('rs-filename').focus(); return; }
  $('rule-sandbox-result').innerHTML = '<div class="pf-decision-empty">分析中…</div>';
  const res = await api('/api/rules/sandbox', {
    method: 'POST',
    body: JSON.stringify({
      filename,
      title: $('rs-title').value.trim(),
      text: $('rs-text').value,
      case_key: $('rs-case').value,
      source: $('rs-source').value,
      rules: _rules,
    }),
  });
  if (res.ok) renderSandboxDecision(res.decision);
  else $('rule-sandbox-result').innerHTML = `<div class="pf-decision-warning">${esc(res.error || '沙盒分析失败')}</div>`;
}

async function openRuleHistory() {
  $('rule-history-overlay').classList.add('open');
  const list = $('rule-history-list');
  list.innerHTML = '<div class="empty">加载中…</div>';
  const data = await api('/api/rules/history');
  const entries = data.entries || [];
  if (!entries.length) {
    list.innerHTML = '<div class="empty">暂无保存历史。第一次保存规则后会在这里生成版本。</div>';
    return;
  }
  list.innerHTML = entries.map((v, i) => {
    const when = v.timestamp ? new Date(v.timestamp).toLocaleString() : '未知时间';
    const digest = (v.digest || '').slice(0, 12);
    return `<div class="history-row">
      <div><span class="history-time">${esc(when)}</span><span class="history-count">${esc(v.count || 0)} 条规则</span></div>
      <div class="history-digest mono">${esc(digest)}</div>
      <button class="icon-btn" onclick="restoreRuleVersion('${esc(v.id)}')">恢复</button>
    </div>`;
  }).join('');
}

function closeRuleHistory() { $('rule-history-overlay').classList.remove('open'); }

async function restoreRuleVersion(id) {
  if (!confirm('恢复这个规则版本？当前规则会先保留在历史记录中。')) return;
  const res = await api('/api/rules/restore', {
    method: 'POST', body: JSON.stringify({ id }),
  });
  if (!res.ok) { toast('恢复失败：' + (res.error || ''), true); return; }
  await loadRules();
  closeRuleHistory();
  toast('规则版本已恢复');
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
    list.innerHTML = '<div class="empty">没有待复核的文件</div>';
    return;
  }
  list.innerHTML = _reviewData.map((it, i) => {
    const actions = it.need_source
      ? `<button class="icon-btn" onclick="openReviewDecision(${i})">查看决策</button>
         <span class="hint-sm" style="margin-right:4px">来源?</span>
         <button class="icon-btn" onclick="confirmSource(${i},'我方证据')">我方</button>
         <button class="icon-btn" onclick="confirmSource(${i},'对方证据')">对方</button>
         <button class="icon-btn" onclick="openReclass(${i})">其它</button>`
      : `<button class="icon-btn" onclick="openReviewDecision(${i})">查看决策</button>
         <button class="icon-btn" onclick="openReclass(${i})">重新归类</button>`;
    const reasonCls = it.need_source ? 'review-reason need-source' : 'review-reason';
    return `<div class="review-row">
      <div class="review-main">
        <span class="${reasonCls}" title="${esc(it.reason)}">${esc(it.reason)}</span>
        <span class="file-name" title="${esc(it.path)}">${esc(it.name)}</span>
      </div>
      <span class="review-location" title="${esc(it.loc || '')}">${esc(it.loc || '')}</span>
      <div class="rule-actions">${actions}</div>
    </div>`;
  }).join('');
}

async function openReviewDecision(i) {
  const item = _reviewData[i];
  if (!item || !item.path) return;
  try {
    const res = await fetch(`/api/review/file?path=${encodeURIComponent(item.path)}`);
    if (!res.ok) {
      let detail = '复核文件不存在';
      try { detail = (await res.json()).error || detail; } catch {}
      throw new Error(detail);
    }
    const blob = await res.blob();
    const file = new File([blob], item.name, {
      type: blob.type || 'application/octet-stream',
      lastModified: Date.now(),
    });
    tagRelativePath(file, item.name);
    // Reuse the same frozen-decision card and commit path.  The original
    // review file stays where it is until the user confirms a green decision.
    await openPrefill([file], [{
      ...item,
      batch_id: item.batch_id || 'review',
      path: item.path,
    }]);
    if (item.case_key) {
      $('pf-case').value = item.case_key;
      const c = (_meta.cases || []).find(x => x.key === item.case_key);
      const ui = _SCEN_UI[c && c.scenario] || {};
      if (ui.country) $('pf-country').value = ui.country;
      if (ui.proc) $('pf-proc').value = ui.proc;
      await loadCaseFolders(item.case_key);
      populatePfParties(item.case_key);
      populatePfAlso(item.case_key);
    }
  } catch (e) {
    toast('打开复核决策失败：' + e.message, true);
  }
}

async function confirmSource(i, side) {
  const it = _reviewData[i];
  const res = await api('/api/review/confirm_source', {
    method: 'POST',
    body: JSON.stringify({ path: it.path, side, pending_target: it.pending_target }),
  });
  if (res.ok) { toast('已确认来源并归位'); loadReview(); }
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
  if (res.ok) { toast('已重新归类'); closeReclass(); loadReview(); }
  else        toast(res.error || '失败', true);
}

function closeReclass() { $('reclass-overlay').classList.remove('open'); }

// ── Init ───────────────────────────────────────────────────────
// ══════════════════════════════════════════════════════════════
// 脱敏工作台：选文件 → 识别 → 人工删补 → 左右预览 → 导出
// ══════════════════════════════════════════════════════════════
const DESK = { route: 'case', case: '', rel: '', selectionId: '', sourceName: '', src: '',
               spans: [], hidden: new Set(), applied: false, ner: true, vis: [],
               policy: 'strict', categories: new Set(), scopeInitialized: false,
               files: [], outputId: '', outputLocation: '', terms: [], excluded: new Set(),
               history: [], revision: 0, busy: false, scopeDirty: false };

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
const DESK_SCOPE_CATS = [
  ['PERSON','人名'],['COMPANY','公司'],['ADDRESS','地址'],['PHONE','电话'],['EMAIL','邮箱'],
  ['ID_CARD','身份证'],['USCC','统一码'],['BANKCARD','银行卡'],['PASSPORT','护照'],
  ['HK_MO_TW','港澳台证件'],['DRIVER_LICENSE','驾驶证'],['SOCIAL_SEC','社保公积金'],
  ['WECHAT','微信'],['QQ','QQ'],['SSN','SSN'],['EIN','EIN'],['PLATE','车牌'],
  ['BIRTHDAY','出生日期'],['IP','IP'],['MAC','MAC'],['IMEI','IMEI'],['GPS','坐标'],
  ['CASE_NO','案号'],['MONEY','金额'],['SECURITIES','证券'],['OTHER','其他'],
];
const DESK_SCOPE_ALL = new Set(DESK_SCOPE_CATS.map(([key]) => key));
const DESK_SCOPE_GROUPS = [
  { key: 'identity', label: '身份与主体', items: [
    'PERSON','COMPANY','ID_CARD','USCC','PASSPORT','HK_MO_TW',
    'DRIVER_LICENSE','SSN','EIN','BIRTHDAY',
  ]},
  { key: 'contact', label: '联系方式与设备', items: [
    'ADDRESS','PHONE','EMAIL','WECHAT','QQ','IP','MAC','IMEI','GPS',
  ]},
  { key: 'finance', label: '财务与账户', items: [
    'BANKCARD','SOCIAL_SEC','MONEY','SECURITIES',
  ]},
  { key: 'case', label: '案件与程序', items: ['CASE_NO','PLATE'] },
  { key: 'other', label: '其他', items: ['OTHER'] },
];
const DESK_SCOPE_BY_KEY = new Map(DESK_SCOPE_CATS);
const deskZh = lbl => DESK_LABEL_ZH[lbl] || lbl;
const deskMode = () => (document.querySelector('input[name="desk-mode"]:checked') || {}).value || 'alias';

function deskSelectedCategories() {
  return DESK.policy === 'custom' ? [...DESK.categories] : undefined;
}

function deskMarkScopeChanged() {
  DESK.scopeDirty = true;
  DESK.revision++;
  deskStale();
  if (DESK.src) $('desk-out').innerHTML = '<div class="empty">脱敏范围已改变，请重新识别</div>';
}

function deskSyncScopeChecks(panel) {
  const selected = DESK.categories;
  const all = panel.querySelector('#desk-scope-all');
  const allKeys = [...DESK_SCOPE_ALL];
  if (all) {
    const count = allKeys.filter(key => selected.has(key)).length;
    all.checked = count === allKeys.length;
    all.indeterminate = count > 0 && count < allKeys.length;
  }
  panel.querySelectorAll('[data-scope-group]').forEach(box => {
    const group = DESK_SCOPE_GROUPS.find(item => item.key === box.dataset.scopeGroup);
    if (!group) return;
    const count = group.items.filter(key => selected.has(key)).length;
    box.checked = count === group.items.length;
    box.indeterminate = count > 0 && count < group.items.length;
    const counter = panel.querySelector(`[data-scope-group-count="${group.key}"]`);
    if (counter) counter.textContent = `${count}/${group.items.length}`;
  });
  panel.querySelectorAll('[data-scope-cat]').forEach(box => {
    box.checked = selected.has(box.dataset.scopeCat);
  });
  const count = allKeys.filter(key => selected.has(key)).length;
  const counter = panel.querySelector('[data-scope-count]');
  if (counter) counter.textContent = `${count}/${allKeys.length}`;
}

function deskRenderScope() {
  const panel = $('desk-scope-options');
  if (!panel) return;
  panel.hidden = DESK.policy !== 'custom';
  if (DESK.policy !== 'custom') {
    panel.innerHTML = '';
    return;
  }
  if (!DESK.scopeInitialized) {
    DESK.categories = new Set(DESK_SCOPE_ALL);
    DESK.scopeInitialized = true;
  }
  panel.innerHTML = '<div class="desk-scope-head">' +
      '<div><div class="desk-scope-title">自定义需要脱敏的类别</div>' +
      '<div class="desk-scope-note">按组或单项选择自动识别范围；已确认的手工补标会保留</div></div>' +
      '<label class="scope-master"><input id="desk-scope-all" type="checkbox">全选 <span data-scope-count></span></label>' +
    '</div>' +
    '<div class="desk-scope-groups">' + DESK_SCOPE_GROUPS.map(group =>
      '<section class="desk-scope-group" data-scope-group-card="' + group.key + '">' +
        '<div class="scope-group-head">' +
          '<label class="scope-group-toggle"><input type="checkbox" data-scope-group="' + group.key + '">' +
          '<span>' + group.label + '</span></label>' +
        '<span class="scope-group-count" data-scope-group-count="' + group.key + '"></span>' +
        '</div>' +
        '<div class="desk-scope-grid">' + group.items.map(key => {
          // DESK_SCOPE_BY_KEY 的 value 是标签字符串，直接渲染完整标签。
          const label = DESK_SCOPE_BY_KEY.get(key) || key;
          return '<label class="desk-scope-check"><input type="checkbox" data-scope-cat="' + key + '">' +
            '<span class="desk-scope-label" data-scope-label="' + key + '" aria-label="' + esc(label) + '">' + esc(label) + '</span></label>';
        }).join('') + '</div>' +
      '</section>'
    ).join('') + '</div>';

  deskSyncScopeChecks(panel);
  panel.querySelectorAll('[data-scope-cat]').forEach(box => {
    box.addEventListener('change', () => {
      if (box.checked) DESK.categories.add(box.dataset.scopeCat);
      else DESK.categories.delete(box.dataset.scopeCat);
      deskSyncScopeChecks(panel);
      deskMarkScopeChanged();
    });
  });
  panel.querySelector('#desk-scope-all')?.addEventListener('change', e => {
    DESK.categories = e.target.checked ? new Set(DESK_SCOPE_ALL) : new Set();
    deskSyncScopeChecks(panel);
    deskMarkScopeChanged();
  });
  panel.querySelectorAll('[data-scope-group]').forEach(box => {
    box.addEventListener('change', () => {
      const group = DESK_SCOPE_GROUPS.find(item => item.key === box.dataset.scopeGroup);
      if (!group) return;
      group.items.forEach(key => {
        if (box.checked) DESK.categories.add(key);
        else DESK.categories.delete(key);
      });
      deskSyncScopeChecks(panel);
      deskMarkScopeChanged();
    });
  });
}

function deskSetFileStatus(text, kind = '') {
  const el = $('desk-file-status');
  if (!el) return;
  el.textContent = text || '';
  el.className = 'desk-file-status' + (kind ? ` ${kind}` : '');
}

function deskUpdateExportButton() {
  $('desk-export').disabled = !(DESK.src && DESK.outputId && DESK.applied) || DESK.busy || DESK.scopeDirty;
  $('desk-apply').disabled = !DESK.src || DESK.busy || DESK.scopeDirty;
  $('desk-match-terms').disabled = !DESK.src || !DESK.terms.length || DESK.busy || DESK.scopeDirty;
  $('desk-undo').disabled = !DESK.history.length || DESK.busy || DESK.scopeDirty;
  $('desk-analyze').disabled = DESK.busy || !(DESK.selectionId || (DESK.case && DESK.rel));
}

function deskSetSettings(open) {
  $('desk-settings').hidden = !open;
  $('desk-settings-toggle').setAttribute('aria-expanded', String(open));
  deskHidePicker();
}

function deskSetBusy(busy) {
  DESK.busy = busy;
  $('desk-settings').inert = busy;
  $('desk-src').inert = busy;
  $('desk-cats').inert = busy;
  $('sec-desk').setAttribute('aria-busy', String(busy));
  deskUpdateExportButton();
}

function deskRemember() {
  DESK.history.push({ spans: DESK.spans.map(s => ({...s})),
    terms: DESK.terms.map(t => ({...t})), excluded: new Set(DESK.excluded), hidden: new Set(DESK.hidden) });
  if (DESK.history.length > 30) DESK.history.shift();
}

function deskRefreshEdits() {
  DESK.revision++;
  deskRenderTerms(); deskRenderCats(); deskRenderSrc(); deskUpdateStat(); deskStale();
}

function deskUndo() {
  const prev = DESK.history.pop();
  if (!prev) return;
  Object.assign(DESK, prev);
  deskRefreshEdits();
  $('desk-review-status').textContent = '已撤销上一步，请生成新预览';
}

function deskAddTerm(text, label) {
  const normalized = DeskTerms.normalize(text).value;
  let term = DESK.terms.find(t => DeskTerms.normalize(t.text).value === normalized);
  if (term) {
    if (term.label !== label) {
      term.label = label;
      DESK.spans.filter(s => s.termId === term.id).forEach(s => { s.label = label; });
    }
    return term;
  }
  term = { id: 'term-' + (++DESK.revision), text: text.trim(), label };
  DESK.terms.push(term);
  return term;
}

function deskRenderTerms() {
  $('desk-term-count').textContent = DESK.terms.length;
  $('desk-term-list').innerHTML = DESK.terms.length ? DESK.terms.map(t =>
    `<div class="desk-term"><span title="${esc(t.text)}">${esc(t.text)}</span><small>${esc(deskZh(t.label))}</small><button class="btn btn-ghost btn-xs" data-remove-term="${esc(t.id)}" aria-label="移除关键词 ${esc(t.text)}">移除</button></div>`
  ).join('') : '<p>暂无补充关键词</p>';
  deskUpdateExportButton();
}

function deskMatchTerms() {
  if (!DESK.src || !DESK.terms.length || DESK.busy || DESK.scopeDirty) return;
  deskRemember();
  const result = DeskTerms.match(DESK.src, DESK.terms, DESK.spans, DESK.excluded);
  DESK.spans = result.spans;
  deskRefreshEdits();
  deskSetSettings(false);
  const message = `全文匹配完成：新增 ${result.added} 处标记，请复核后生成脱敏`;
  $('desk-review-status').textContent = message;
  toast(message);
}

// The API uses Python code-point offsets; the browser Selection uses UTF-16.
function deskSpansFromApi(text, spans) {
  const offsets = [0]; let pos = 0;
  for (const char of text) { pos += char.length; offsets.push(pos); }
  return spans.map(s => ({ ...s, start: offsets[s.start], end: offsets[s.end] }))
    .filter(s => Number.isInteger(s.start) && Number.isInteger(s.end) && s.end > s.start);
}

function deskSpansToApi(text, spans) {
  const offsets = new Map([[0, 0]]); let pos = 0, cp = 0;
  for (const char of text) { pos += char.length; offsets.set(pos, ++cp); }
  return spans.map(s => ({ ...s, start: offsets.get(s.start), end: offsets.get(s.end) }));
}

function deskUpdateSourceActions() {
  const selected = Boolean(DESK.selectionId || (DESK.case && DESK.rel));
  $('desk-open-file').disabled = !selected;
  if (!selected) {
    $('desk-source-selected').textContent = '尚未明确选择源文件';
  }
  $('desk-document-name').textContent = selected ? (DESK.sourceName || DESK.rel || '已选择文件') : '尚未选择文件';
  $('desk-document-name').title = DESK.sourceName || DESK.rel || '';
  deskUpdateExportButton();
}

function deskResetPreview(message = '选择案件与材料后点“识别”') {
  DESK.revision++;
  DESK.terms = []; DESK.excluded = new Set(); DESK.history = [];
  DESK.scopeDirty = false;
  deskHidePicker();
  DESK.src = '';
  DESK.spans = [];
  DESK.hidden = new Set();
  DESK.vis = [];
  DESK.applied = false;
  $('desk-src').innerHTML = `<div class="empty">${esc(message)}</div>`;
  $('desk-out').innerHTML = '<div class="empty">识别后点“生成脱敏”在此预览</div>';
  $('desk-out').dataset.has = '';
  $('desk-review-status').textContent = '识别后请逐项复核';
  $('desk-cats').innerHTML = '';
  $('desk-stat').textContent = '';
  $('desk-pending').style.display = 'none';
  $('desk-pending').innerHTML = '';
  deskRenderTerms();
}

function deskSetRoute(route) {
  const next = route === 'standalone' ? 'standalone' : 'case';
  const changed = next !== DESK.route;
  DESK.route = next;
  if (changed) {
    if (DESK.route === 'case') {
      DESK.selectionId = '';
      DESK.sourceName = '';
      if ($('desk-case')) $('desk-case').value = '';
      if ($('desk-file')) {
        $('desk-file').innerHTML = '<option value="">先选择案件…</option>';
        $('desk-file').disabled = true;
      }
      deskSetFileStatus('先选择案件');
    } else {
      DESK.case = '';
      DESK.rel = '';
      DESK.files = [];
      DESK.selectionId = '';
      DESK.sourceName = '';
      if ($('desk-case')) $('desk-case').value = '';
      if ($('desk-file')) $('desk-file').value = '';
      if ($('desk-source-selected')) $('desk-source-selected').textContent = '尚未明确选择源文件';
    }
    deskResetPreview(DESK.route === 'case'
      ? '选择案件与材料后点「识别」'
      : '从访达选择文件后点「识别」');
  }
  document.querySelectorAll('[data-desk-route]').forEach(button => {
    const active = button.dataset.deskRoute === DESK.route;
    button.classList.toggle('active', active);
    button.setAttribute('aria-selected', active ? 'true' : 'false');
  });
  const casePanel = $('desk-case-source');
  const standalonePanel = $('desk-standalone-source');
  if (casePanel) casePanel.hidden = DESK.route !== 'case';
  if (standalonePanel) standalonePanel.hidden = DESK.route !== 'standalone';
  const hint = $('desk-route-source-hint');
  if (hint) hint.textContent = DESK.route === 'case'
    ? '从案件目录选择材料，保留本案化名一致性'
    : '从访达选择任意支持格式的文件，不依赖案件目录';
  deskUpdateSourceActions();
}

async function loadDesk() {
  const data = await api('/api/cases');
  const sel = $('desk-case');
  if (!Array.isArray(data.cases)) {
    sel.innerHTML = '<option value="">无法加载案件</option>';
    $('desk-file').innerHTML = '<option value="">无法加载材料</option>';
    $('desk-file').disabled = true;
    deskSetFileStatus(data.error || '无法连接 Loci 服务，请从启动入口打开前台', 'error');
    return;
  }
  const cases = data.cases.filter(c => c.status !== 'closed');
  const prev = DESK.case;
  sel.innerHTML = '<option value="">选择案件…</option>' +
    cases.map(c => `<option value="${esc(c.key)}">${esc(c.name)}</option>`).join('');
  if (prev && cases.some(c => c.key === prev)) {
    sel.value = prev;
    if (!DESK.files.length) await deskLoadFiles(prev, DESK.rel);
  } else {
    $('desk-file').innerHTML = '<option value="">先选择案件…</option>';
    $('desk-file').disabled = true;
    deskSetFileStatus(cases.length ? '先选择案件' : '暂无可用案件', cases.length ? '' : 'warning');
  }
}

async function deskLoadFiles(key, preferredRel = '') {
  const fs = $('desk-file');
  DESK.files = [];
  DESK.rel = '';
  DESK.selectionId = '';
  DESK.sourceName = '';
  $('desk-source-selected').textContent = '尚未明确选择源文件';
  deskResetPreview('正在加载案件材料…');
  if (!key) {
    fs.innerHTML = '<option value="">先选择案件…</option>';
    fs.disabled = true;
    deskSetFileStatus('先选择案件');
    deskUpdateSourceActions();
    return;
  }
  fs.disabled = true;
  fs.innerHTML = '<option value="">扫描材料中…</option>';
  deskSetFileStatus('正在扫描…');
  const data = await api('/api/desensitize/files?case=' + encodeURIComponent(key));
  if (!data.ok) {
    fs.innerHTML = '<option value="">无法加载材料</option>';
    fs.disabled = true;
    deskSetFileStatus(data.error || '加载失败', 'error');
    deskUpdateSourceActions();
    return;
  }
  DESK.files = Array.isArray(data.files) ? data.files : [];
  if (!DESK.files.length) {
    fs.innerHTML = '<option value="">（没有可脱敏材料）</option>';
    fs.disabled = true;
    let message = data.exists === false ? '案件文件夹不存在' : '未找到可脱敏格式';
    if (data.unsupported) message += `；另有 ${data.unsupported} 份不支持的文件`;
    if (data.warnings && data.warnings.length) message += '；目录读取有警告';
    deskSetFileStatus(message, data.warnings?.length ? 'warning' : '');
    deskUpdateSourceActions();
    return;
  }

  // 用 optgroup 保留案件文件夹层级，避免同名材料只能靠猜测选择。
  const groups = new Map();
  DESK.files.forEach(f => {
    const dir = f.dir || '案件根目录';
    if (!groups.has(dir)) groups.set(dir, []);
    groups.get(dir).push(f);
  });
  let options = '<option value="">选择案件材料…</option>';
  for (const [dir, files] of groups) {
    options += `<optgroup label="${esc(dir)}">` + files.map(f =>
      `<option value="${esc(f.rel)}">${esc(f.name)} · ${esc(String(f.ext || '').toUpperCase())} · ${fmtBytes(f.size || 0)}</option>`
    ).join('') + '</optgroup>';
  }
  fs.innerHTML = options;
  fs.disabled = false;
  const preferred = preferredRel && DESK.files.some(f => f.rel === preferredRel) ? preferredRel : '';
  fs.value = preferred;
  DESK.rel = preferred;
  let message = `${DESK.files.length} 份材料可选`;
  if (data.unsupported) message += ` · ${data.unsupported} 份格式暂不支持`;
  if (data.warnings && data.warnings.length) message += ' · 目录有读取警告';
  deskSetFileStatus(message, data.warnings?.length ? 'warning' : 'ready');
  deskUpdateSourceActions();
}

async function deskAnalyze() {
  if (DESK.busy) return;
  if (!DESK.selectionId && !(DESK.case && DESK.rel)) {
    toast('请先从访达或案件材料列表选择一个文件', true); return;
  }
  const src = $('desk-src');
  const previousText = DESK.src;
  const previousSpans = DESK.spans;
  const revision = ++DESK.revision;
  deskSetBusy(true);
  src.innerHTML = '<div class="empty">识别中…（规则引擎 / OCR 可能需要一点时间）</div>';
  $('desk-out').innerHTML = '<div class="empty">识别后点「生成脱敏」在此预览</div>';
  $('desk-cats').innerHTML = '';
  DESK.applied = false; deskUpdateExportButton();
  $('desk-out').dataset.has = '';
  const source = DESK.selectionId
    ? { selection_id: DESK.selectionId }
    : { case: DESK.case, rel: DESK.rel };
  const r = await api('/api/desensitize/analyze', {
    method: 'POST',
    body: JSON.stringify({ ...source, case: DESK.case, policy: DESK.policy,
                           categories: deskSelectedCategories() }),
  });
  deskSetBusy(false);
  if (revision !== DESK.revision) return;
  if (!r.ok) {
    if (previousText) deskRenderSrc();
    else src.innerHTML = `<div class="empty">${esc(r.error || '识别失败')}</div>`;
    DESK.scopeDirty = true;
    deskUpdateExportButton();
    $('desk-review-status').textContent = r.error || '识别失败，请重试';
    toast(r.error || '识别失败', true); return;
  }
  DESK.src = r.text || '';
  DESK.spans = deskSpansFromApi(DESK.src, r.spans || []);
  if (previousText === DESK.src) {
    const manual = previousSpans.filter(s => s.source === 'manual' || s.source === 'manual_keyword');
    DESK.spans = DESK.spans.filter(s => !DESK.excluded.has(DeskTerms.key(s)) &&
      !manual.some(m => s.start < m.end && m.start < s.end)).concat(manual);
  } else {
    DESK.excluded = new Set(); DESK.history = []; DESK.hidden = new Set();
  }
  // Explicit manual decisions survive reanalysis and override the automatic scope.
  DESK.spans = DeskTerms.match(DESK.src, DESK.terms, DESK.spans, DESK.excluded).spans;
  DESK.scopeDirty = false;
  DESK.ner = r.ner;
  if (r.source_name) {
    DESK.sourceName = r.source_name;
    $('desk-source-selected').textContent = `已选择：${r.source_name}`;
  }
  if (!DESK.src.trim()) {
    src.innerHTML = '<div class="empty desk-extract-warning">' +
      esc(r.msg || '未提取到文本内容，请检查文件格式和读取权限') + '</div>';
    $('desk-out').innerHTML = '<div class="empty">没有可供复核的文本</div>';
    deskUpdateExportButton();
    return;
  }
  deskSetSettings(false);
  deskUpdateSourceActions(); deskRenderTerms();
  deskRenderCats();
  deskRenderSrc();
  deskUpdateStat();
  $('desk-review-status').textContent = '识别完成；划选补标后可按关键词匹配全文';
}

function deskUpdateStat() {
  const total = DESK.spans.length;
  const low = DESK.spans.filter(s => s.low_conf).length;
  const active = DESK.spans.filter(s => !DESK.hidden.has(deskZh(s.label))).length;
  $('desk-stat').innerHTML = `命中 <b>${total}</b> 处` +
    (low ? ` · <span class="desk-low-n">待确认 ${low}</span>` : '') +
    (active !== total ? ` · 启用 ${active} 处` : '');
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

// 将浏览器 Range 的边界映射回 DESK.src 的纯文本偏移。
// 原文渲染后会被多个 <mark> 分割，不能依赖元素数量或 innerHTML 位置。
function deskDomTextOffset(root, node, offset) {
  if (!root || !node) return -1;
  if (node === root) {
    let total = 0;
    const children = [...root.childNodes];
    children.slice(0, offset).forEach(child => { total += (child.textContent || '').length; });
    return total;
  }
  try {
    const pre = document.createRange();
    pre.selectNodeContents(root);
    pre.setEnd(node, offset);
    return pre.toString().length;
  } catch (_) {
    return -1;
  }
}

function deskCaptureSelection() {
  const box = $('desk-src');
  const sel = window.getSelection();
  if (!box || !sel || !sel.rangeCount || sel.isCollapsed) return null;
  const range = sel.getRangeAt(0);
  if (!box.contains(range.commonAncestorContainer)) return null;
  const selectedText = range.toString();
  if (!selectedText || !selectedText.trim()) return null;
  const start = deskDomTextOffset(box, range.startContainer, range.startOffset);
  const end = deskDomTextOffset(box, range.endContainer, range.endOffset);
  if (start < 0 || end <= start) return null;

  // Range.toString() 可能把 PDF 的换行、非断行空格规范化；偏移已经由
  // deskDomTextOffset 在当前 DOM 内可靠计算，因此以 DESK.src 为提交依据，
  // 不再因视觉文本与原始文本的换行差异而误判“选区无效”。
  const sourceText = DESK.src.slice(start, end);
  if (!sourceText || !sourceText.trim()) return null;
  return { start, end, text: selectedText, sourceText };
}

// 编辑后：预览标记为过期
function deskStale() {
  DESK.applied = false;
  deskUpdateExportButton();
  const out = $('desk-out');
  if (out.dataset.has === '1')
    out.innerHTML = '<div class="empty">内容已修改 — 点击“生成脱敏”刷新预览</div>';
  out.dataset.has = '';
  $('desk-review-status').textContent = '标记已更新，请生成新预览后保存';
}

async function deskApply(forExport) {
  if (DESK.busy) return;
  if (DESK.scopeDirty) { toast('脱敏范围已改变，请重新识别', true); return; }
  if (forExport && !DESK.applied) { toast('请先生成并复核脱敏预览', true); return; }
  if (!DESK.src) { toast('请先识别', true); return; }
  const spans = DESK.spans.filter(s => !DESK.hidden.has(deskZh(s.label)));
  const mode = deskMode();
  const revision = DESK.revision;
  DESK.applied = false;
  deskSetBusy(true);
  const out = $('desk-out');
  out.innerHTML = '<div class="empty">生成中…</div>';
  const source = DESK.selectionId
    ? { selection_id: DESK.selectionId }
    : { case: DESK.case, rel: DESK.rel };
  const r = await api('/api/desensitize/apply', {
    method: 'POST',
    body: JSON.stringify({ ...source, case: DESK.case, rel: DESK.rel,
                           source_name: DESK.sourceName, output_id: DESK.outputId,
                           text: DESK.src, spans: deskSpansToApi(DESK.src, spans), mode, save: !!forExport }),
  });
  deskSetBusy(false);
  if (revision !== DESK.revision) return;
  if (!r.ok) {
    out.dataset.has = '';
    out.innerHTML = `<div class="empty">${esc(r.error || '脱敏失败')}</div>`;
    $('desk-review-status').textContent = r.error || '脱敏失败，请重试';
    return;
  }
  out.textContent = r.masked;
  out.dataset.has = '1';
  DESK.applied = true;
  deskSetSettings(false);
  deskUpdateExportButton();
  deskRenderPending(r.pending || []);
  $('desk-review-status').textContent = forExport ? '已保存脱敏副本' : '预览已生成，请复核后保存';
  if (forExport && r.saved) {
    toast('已保存：' + r.saved.split('/').slice(-2).join('/'));
    $('desk-output-selected').textContent = `已保存：${r.saved.split('/').pop()}`;
  }
}

function deskRenderPending(pending) {
  const bar = $('desk-pending');
  if (!pending.length) { bar.style.display = 'none'; bar.innerHTML = ''; return; }
  bar.style.display = '';
  bar.innerHTML = '<span class="desk-pending-label">待人工确认（疑似同名归并）</span>' +
    pending.map(p => `<span class="pending-item">${esc(p.alias)} ← ${esc(p.canonical)}</span>`).join('');
}

async function deskReset() {
  if (!confirm('清空本案已建立的化名映射并重新编号？（不影响已识别结果）')) return;
  const result = await api('/api/desensitize/reset', {
    method: 'POST',
    body: JSON.stringify({ case: DESK.case, selection_id: DESK.selectionId }),
  });
  if (!result.ok) { toast(result.error || '重置失败', true); return; }
  DESK.revision++;
  DESK.applied = false;
  $('desk-out').innerHTML = '<div class="empty">已重置化名，点「生成脱敏」重新生成</div>';
  $('desk-out').dataset.has = '';
  deskUpdateExportButton();
  toast('化名已重置');
}

async function deskPickFile() {
  deskSetRoute('standalone');
  DESK.case = '';
  DESK.rel = '';
  const r = await api('/api/desensitize/pick-file', {
    method: 'POST', body: JSON.stringify({}),
  });
  if (!r.ok) {
    if (!r.cancelled) toast(r.error || '未能从访达选择文件', true);
    return;
  }
  DESK.selectionId = r.selection_id;
  DESK.rel = '';
  DESK.sourceName = r.name || '';
  $('desk-case').value = '';
  $('desk-file').value = '';
  $('desk-source-selected').textContent = `已从访达选择：${r.name || '文件'}`;
  deskSetFileStatus(r.location ? `访达文件：${r.location}` : '已从访达选择文件', 'ready');
  deskResetPreview('已从访达选择材料，点击“识别”开始脱敏');
  deskUpdateSourceActions();
}

async function deskOpenFile() {
  const source = DESK.selectionId
    ? { selection_id: DESK.selectionId }
    : { case: DESK.case, rel: DESK.rel };
  const r = await api('/api/desensitize/open-file', {
    method: 'POST', body: JSON.stringify(source),
  });
  if (!r.ok) toast(r.error || '无法在访达中显示文件', true);
}

async function deskPickOutput() {
  const r = await api('/api/desensitize/pick-output', {
    method: 'POST', body: JSON.stringify({}),
  });
  if (!r.ok) {
    if (!r.cancelled) toast(r.error || '未能选择输出工作区', true);
    return;
  }
  DESK.outputId = r.output_id;
  DESK.outputLocation = r.location || '';
  $('desk-output-selected').textContent = r.location || r.name || '已选择输出工作区';
  $('desk-output-selected').title = r.location || '';
  deskUpdateExportButton();
  toast('已选择脱敏输出工作区');
}

function deskInit() {
  $('desk-settings-toggle').addEventListener('click', () => deskSetSettings($('desk-settings').hidden));
  $('desk-settings-close').addEventListener('click', () => {
    deskSetSettings(false); $('desk-settings-toggle').focus();
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && _currentSec === 'desk') { deskSetSettings(false); deskHidePicker(); }
  });
  $('desk-match-terms').addEventListener('click', deskMatchTerms);
  $('desk-undo').addEventListener('click', deskUndo);
  $('desk-term-label').innerHTML = DESK_SCOPE_CATS.map(([key, label]) => `<option value="${key}">${label}</option>`).join('');
  $('desk-term-form').addEventListener('submit', e => {
    e.preventDefault();
    const text = $('desk-term-text').value.trim();
    if (!DESK.src) { toast('请先识别文件，再补充关键词', true); return; }
    if (!text) return;
    deskRemember();
    deskAddTerm(text, $('desk-term-label').value);
    $('desk-term-text').value = '';
    deskRenderTerms(); deskStale();
    $('desk-review-status').textContent = '关键词已添加，点击「按补标匹配全文」';
  });
  $('desk-term-list').addEventListener('click', e => {
    const button = e.target.closest('[data-remove-term]');
    if (!button) return;
    deskRemember();
    DESK.terms = DESK.terms.filter(t => t.id !== button.dataset.removeTerm);
    DESK.spans = DESK.spans.filter(s => s.termId !== button.dataset.removeTerm);
    deskRefreshEdits();
  });
  deskRenderTerms();
  document.querySelectorAll('[data-desk-route]').forEach(button => {
    button.addEventListener('click', () => {
      deskSetRoute(button.dataset.deskRoute);
      if (DESK.route === 'standalone') {
        $('desk-case').value = '';
        $('desk-file').value = '';
      }
    });
  });
  deskSetRoute('case');
  deskRenderScope();

  $('desk-case').addEventListener('change', e => {
    deskSetRoute('case');
    DESK.case = e.target.value; DESK.rel = '';
    deskLoadFiles(DESK.case);
  });
  $('desk-file').addEventListener('change', e => {
    DESK.selectionId = '';
    DESK.rel = e.target.value;
    deskResetPreview(DESK.rel ? '已选择材料，点击“识别”开始脱敏' : '选择案件与材料后点“识别”');
    const selected = DESK.files.find(f => f.rel === DESK.rel);
    DESK.sourceName = selected ? selected.name : '';
    $('desk-source-selected').textContent = selected ? `已选择：${selected.name}` : '尚未明确选择源文件';
    deskSetFileStatus(selected ? `已选择：${selected.name}` : '请选择材料', selected ? 'ready' : '');
    deskUpdateSourceActions();
  });
  $('desk-refresh-files').addEventListener('click', () => {
    if (!DESK.case) { toast('请先选择案件', true); return; }
    deskLoadFiles(DESK.case, DESK.rel);
  });
  $('desk-policy').addEventListener('change', e => {
    DESK.policy = e.target.value;
    deskRenderScope();
    deskMarkScopeChanged();
    $('desk-out').innerHTML = '<div class="empty">策略已改变 — 请重新点「识别」</div>';
  });
  $('desk-pick-file').addEventListener('click', deskPickFile);
  $('desk-open-file').addEventListener('click', deskOpenFile);
  $('desk-pick-output').addEventListener('click', deskPickOutput);
  $('desk-analyze').addEventListener('click', deskAnalyze);
  $('desk-apply').addEventListener('click', () => deskApply(false));
  $('desk-export').addEventListener('click', () => {
    if (!DESK.outputId) { toast('请先选择脱敏输出工作区', true); return; }
    deskApply(true);
  });
  $('desk-reset').addEventListener('click', deskReset);

  // 脱敏方式切换：已生成则实时刷新预览
  document.querySelectorAll('input[name="desk-mode"]').forEach(r =>
    r.addEventListener('change', () => {
      $('desk-mode-tag').textContent = { alias:'化名', blackout:'黑框', asterisk:'星号' }[deskMode()];
      const refresh = DESK.applied;
      DESK.revision++;
      deskStale();
      if (refresh) deskApply(false);
    }));

  // 类别筛选
  $('desk-cats').addEventListener('click', e => {
    const chip = e.target.closest('.cat-chip'); if (!chip) return;
    const z = chip.dataset.zh;
    deskRemember();
    if (DESK.hidden.has(z)) DESK.hidden.delete(z); else DESK.hidden.add(z);
    deskRefreshEdits();
  });

  // 点高亮 → 删误标
  $('desk-src').addEventListener('click', e => {
    const m = e.target.closest('mark'); if (!m) return;
    if (!window.getSelection()?.isCollapsed) return;
    const target = DESK.vis[+m.dataset.i];
    if (!target) return;
    deskRemember();
    DESK.excluded.add(DeskTerms.key(target));
    DESK.spans = DESK.spans.filter(s => s !== target);
    deskRefreshEdits();
  });

  // 划选 → 类别浮层 → 补标。延迟一帧，确保浏览器完成选区更新；
  // 偏移由文本节点计算，不受已有 <mark> 节点影响。pointerup/keyup 是
  // pywebview 与键盘扩选的兜底，避免只监听 mouseup 时偶发没有浮层。
  let selectionFrame = 0;
  const scheduleSelectionPicker = () => {
    if (selectionFrame) cancelAnimationFrame(selectionFrame);
    selectionFrame = requestAnimationFrame(() => {
      selectionFrame = 0;
      const captured = deskCaptureSelection();
      if (!captured) { deskHidePicker(); return; }
      const sel = window.getSelection();
      const rect = sel && sel.rangeCount ? sel.getRangeAt(0).getBoundingClientRect() : null;
      if (rect) deskShowPicker(captured.start, captured.end, rect, captured.text);
    });
  };
  $('desk-src').addEventListener('mouseup', scheduleSelectionPicker);
  $('desk-src').addEventListener('pointerup', scheduleSelectionPicker);
  $('desk-src').addEventListener('keyup', scheduleSelectionPicker);

  // 补标类别浮层点击
  $('desk-picker').addEventListener('mousedown', e => {
    // 保留浏览器选区；范围已保存到 picker._range，不再依赖点击后的 Selection。
    if (e.target.closest('button')) e.preventDefault();
  });
  $('desk-picker').addEventListener('click', e => {
    const b = e.target.closest('button'); if (!b) return;
    const { start, end } = $('desk-picker')._range || {};
    if (!(end > start) || !DESK.src.slice(start, end).trim()) {
      deskHidePicker();
      toast('选区已失效，请重新划选文字', true);
      return;
    }
    const markedText = DESK.src.slice(start, end);
    deskRemember();
    const term = deskAddTerm(markedText, b.dataset.label);
    // The selected range replaces overlapping automatic marks so it is visible.
    DESK.spans = DESK.spans.filter(s => !(s.start < end && start < s.end));
    DESK.hidden.delete(deskZh(b.dataset.label));
    DESK.excluded.delete(DeskTerms.key({start, end, label: b.dataset.label}));
    DESK.spans.push({ start, end, text: markedText,
                      label: b.dataset.label, source: 'manual', termId: term.id, score: 1, low_conf: false });
    deskHidePicker(); window.getSelection().removeAllRanges();
    deskRefreshEdits();
    $('desk-review-status').textContent = '补标已加入关键词，点击「按补标匹配全文」处理其余同词';
    toast(`已补标：${markedText}；可按关键词匹配全文`);
  });
}

function deskShowPicker(start, end, rect, selectedText = '') {
  const pk = $('desk-picker');
  pk._range = { start, end, text: selectedText };
  pk.innerHTML = '<span class="picker-hint">标为：</span>' +
    DESK_PICK.map(([l, z]) => `<button data-label="${l}">${z}</button>`).join('');
  const host = $('sec-desk').getBoundingClientRect();
  pk.style.display = 'flex';
  let left = rect.left - host.left;
  let top = rect.bottom - host.top + 4;
  left = Math.max(8, Math.min(left, host.width - pk.offsetWidth - 8));
  // 选区靠近底部时向上收回；同时设置下限，避免浮层被 sec-desk
  // 的 overflow:hidden 裁掉后看起来像“没有弹出”。
  top = Math.max(8, Math.min(top, host.height - pk.offsetHeight - 8));
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
    const daemonBtn = $('btn-daemon');
    const stopping = daemonBtn.dataset.daemonAction === 'stop' ||
      daemonBtn.classList.contains('btn-danger');
    if (!stopping && !window.confirm(
      '启动后，后台守护会持续观察收件箱，并可能在无人值守时执行归档。' +
      '\n\n仅应在完成充分验收并明确承担风险后继续。确定要启动吗？'
    )) return;
    daemonBtn.disabled = true;
    toast(stopping ? '正在停止…' : '正在启动…');
    let accepted = false;
    try {
      const res = await api(stopping ? '/api/daemon/stop' : '/api/daemon/start', { method: 'POST' });
      if (!res.ok) {
        toast(res.error || '守护进程操作被拒绝', true);
        return;
      }
      accepted = true;
      setTimeout(loadDashboard, 1400);
    } catch (e) {
      toast('守护进程操作失败：' + e.message, true);
    } finally {
      if (!accepted) daemonBtn.disabled = false;
    }
  });

  $('btn-once').addEventListener('click',    () => nav('inbox'));
  $('btn-dry').addEventListener('click',     () => runOnce(true));
  $('btn-process-inbox').addEventListener('click', () => nav('inbox'));
  $('btn-refresh-inbox').addEventListener('click', loadInbox);
  $('btn-refresh-staging').addEventListener('click', loadStaging);

  // rules
  $('btn-add-rule').addEventListener('click',   addNewRule);
  $('btn-save-rules').addEventListener('click', saveAllRules);
  $('btn-rule-sandbox').addEventListener('click', openRuleSandbox);
  $('btn-run-rule-sandbox').addEventListener('click', runRuleSandbox);
  $('btn-rule-history').addEventListener('click', openRuleHistory);
  $('rules-search').addEventListener('input', e => renderRules(e.target.value));
  $('rule-scenario-tabs').addEventListener('click', e => {
    const tab = e.target.closest('.rule-scenario-tab');
    if (tab) selectRuleScenario(tab.dataset.scenario);
  });
  $('re-kw-input').addEventListener('keydown',  e => { if (e.key === 'Enter') { e.preventDefault(); addKw('kw');  } });
  $('re-tkw-input').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); addKw('tkw'); } });
  $('re-ckw-input').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); addKw('ckw'); } });

  // modal close
  $('overlay').addEventListener('click', closeModal);
  $('rule-overlay').addEventListener('click', function(e) {
    if (e.target === this) closeRuleModal();
  });
  $('rule-sandbox-overlay').addEventListener('click', function(e) {
    if (e.target === this) closeRuleSandbox();
  });
  $('rule-history-overlay').addEventListener('click', function(e) {
    if (e.target === this) closeRuleHistory();
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
  document.addEventListener('drop', async e => {
    e.preventDefault();
    _dragCount = 0;
    $('drag-overlay').classList.remove('active');
    try {
      const files = await filesFromDrop(e.dataTransfer);
      if (files.length) openPrefill(files);
      else toast('没有发现可上传的文件', true);
    } catch (err) {
      toast('读取拖放文件夹失败：' + err.message, true);
    }
  });

  // click-to-upload
  $('btn-upload-inbox').addEventListener('click', () => $('file-input').click());
  $('btn-upload-folder').addEventListener('click', () => $('folder-input').click());
  $('file-input').addEventListener('change', e => {
    const files = [...e.target.files].map(file => tagRelativePath(file, file.name));
    if (files.length) openPrefill(files);
    e.target.value = '';
  });
  $('folder-input').addEventListener('change', e => {
    const files = [...e.target.files]
      .filter(file => file.name !== '.DS_Store')
      .map(file => tagRelativePath(file, file.webkitRelativePath || file.name));
    if (files.length) openPrefill(files);
    e.target.value = '';
  });
  $('btn-analyze-prefill').addEventListener('click', previewPrefillBatch);

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
      toast(res.msg || '已提交重新归类');
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
