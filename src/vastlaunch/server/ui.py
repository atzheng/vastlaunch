"""Embedded HTML frontend for vastlaunch."""

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>vastlaunch</title>
<style>
  :root {
    --bg: #0f1117; --surface: #1a1d27; --border: #2a2d3a;
    --text: #e1e4e8; --muted: #6b7280; --accent: #4a9eff;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: ui-monospace, 'Cascadia Code', 'SF Mono', monospace; font-size: 13px; }

  header { padding: 14px 24px; border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
  h1 { font-size: 15px; font-weight: 600; color: var(--accent); letter-spacing: .05em; flex-shrink: 0; }
  .controls { display: flex; gap: 8px; align-items: center; flex: 1; flex-wrap: wrap; }
  input, select { background: var(--surface); border: 1px solid var(--border); color: var(--text); padding: 5px 10px; border-radius: 4px; font-family: inherit; font-size: 12px; outline: none; }
  input:focus, select:focus { border-color: var(--accent); }
  #search { width: 220px; }
  .refresh-info { margin-left: auto; color: var(--muted); font-size: 11px; cursor: pointer; }
  .refresh-info:hover { color: var(--text); }

  table { width: 100%; border-collapse: collapse; }
  th { padding: 9px 16px; text-align: left; color: var(--muted); font-weight: 500; font-size: 11px; text-transform: uppercase; letter-spacing: .06em; border-bottom: 1px solid var(--border); cursor: pointer; user-select: none; white-space: nowrap; }
  th:hover { color: var(--text); }
  th.sorted { color: var(--accent); }
  .sort-arrow { margin-left: 3px; }
  td { padding: 9px 16px; border-bottom: 1px solid var(--border); vertical-align: middle; }
  tr:hover td { background: var(--surface); }

  .badge { display: inline-block; padding: 2px 8px; border-radius: 3px; font-size: 11px; font-weight: 500; }
  .badge-queued     { background: #2a2d3a; color: #9ca3af; }
  .badge-launching  { background: #1e3a5f; color: #60a5fa; }
  .badge-connecting { background: #3d2b00; color: #f59e0b; }
  .badge-running    { background: #14301c; color: #34d399; }
  .badge-success    { background: #0d2b1e; color: #10b981; }
  .badge-failed     { background: #2d1515; color: #f87171; }
  .badge-stopped    { background: #2a2d3a; color: #9ca3af; }

  .job-id { font-weight: 500; color: var(--accent); }
  .muted  { color: var(--muted); }

  .btn { background: none; border: 1px solid var(--border); color: var(--muted); padding: 3px 10px; border-radius: 3px; cursor: pointer; font-family: inherit; font-size: 11px; }
  .btn:hover { color: var(--text); border-color: #6b7280; }
  .btn-danger:hover { color: #f87171; border-color: #f87171; }
  .actions { display: flex; gap: 6px; }

  .overlay { position: fixed; inset: 0; background: rgba(0,0,0,.75); display: flex; align-items: center; justify-content: center; z-index: 100; }
  .modal { background: var(--surface); border: 1px solid var(--border); border-radius: 6px; width: 90%; max-width: 920px; max-height: 80vh; display: flex; flex-direction: column; }
  .modal-header { padding: 13px 18px; border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; align-items: center; gap: 12px; }
  .modal-header h2 { font-size: 13px; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .modal-body { padding: 16px 18px; overflow-y: auto; flex: 1; }
  pre { white-space: pre-wrap; word-break: break-all; font-size: 12px; line-height: 1.6; color: #c9d1d9; }
  .close-btn { background: none; border: none; color: var(--muted); font-size: 20px; cursor: pointer; line-height: 1; flex-shrink: 0; }
  .close-btn:hover { color: var(--text); }

  .auth-overlay { position: fixed; inset: 0; background: var(--bg); display: flex; align-items: center; justify-content: center; z-index: 200; }
  .auth-box { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 32px; width: 340px; }
  .auth-box h2 { font-size: 15px; margin-bottom: 6px; }
  .auth-box p { color: var(--muted); font-size: 12px; margin-bottom: 20px; }
  .auth-box input { width: 100%; margin-bottom: 12px; padding: 8px 12px; }
  .btn-primary { background: var(--accent); border-color: var(--accent); color: #fff; width: 100%; padding: 8px; font-size: 13px; }
  .btn-primary:hover { opacity: .9; color: #fff; }
  .auth-error { color: #f87171; font-size: 11px; margin-top: 8px; }

  .empty { padding: 48px; text-align: center; color: var(--muted); }
  .hidden { display: none !important; }
</style>
</head>
<body>

<div id="auth-overlay" class="auth-overlay hidden">
  <div class="auth-box">
    <h2>vastlaunch</h2>
    <p>Enter your API key to continue.</p>
    <input id="key-input" type="password" placeholder="API key" autocomplete="current-password">
    <button class="btn btn-primary" onclick="submitKey()">Sign in</button>
    <div id="auth-error" class="auth-error hidden">Invalid API key.</div>
  </div>
</div>

<div id="logs-modal" class="overlay hidden">
  <div class="modal">
    <div class="modal-header">
      <h2 id="modal-title">Logs</h2>
      <button class="close-btn" onclick="closeLogs()">&#x2715;</button>
    </div>
    <div class="modal-body"><pre id="logs-content">Loading&#x2026;</pre></div>
  </div>
</div>

<header>
  <h1>vastlaunch</h1>
  <div class="controls">
    <input id="search" type="text" placeholder="Search job ID or name&#x2026;" oninput="renderTable()">
    <select id="status-filter" onchange="renderTable()">
      <option value="">All statuses</option>
      <option>queued</option><option>launching</option><option>connecting</option>
      <option>running</option><option>success</option><option>failed</option><option>stopped</option>
    </select>
    <span class="refresh-info" id="refresh-info" onclick="fetchJobs()" title="Click to refresh now">&#x2014;</span>
  </div>
</header>

<table>
  <thead><tr>
    <th onclick="sortBy('job_id')"     id="th-job_id">Job ID <span class="sort-arrow"></span></th>
    <th onclick="sortBy('name')"       id="th-name">Name <span class="sort-arrow"></span></th>
    <th onclick="sortBy('status')"     id="th-status">Status <span class="sort-arrow"></span></th>
    <th onclick="sortBy('instance_id')"id="th-instance_id">Instance <span class="sort-arrow"></span></th>
    <th onclick="sortBy('started_at')" id="th-started_at">Started <span class="sort-arrow"></span></th>
    <th onclick="sortBy('updated_at')" id="th-updated_at">Updated <span class="sort-arrow"></span></th>
    <th></th>
  </tr></thead>
  <tbody id="jobs-body"></tbody>
</table>

<script>
let allJobs = [], sortCol = 'started_at', sortAsc = false;
let refreshTimer = null, countdownInterval = null;
const REFRESH_SECS = 30;
const TERMINAL = new Set(['success', 'failed', 'stopped']);

const apiKey = () => sessionStorage.getItem('vastlaunch_key') || '';
const authHeaders = () => {
  const k = apiKey();
  return k ? { 'Authorization': 'Bearer ' + k } : {};
};

async function fetchJobs() {
  clearTimeout(refreshTimer); clearInterval(countdownInterval);
  document.getElementById('refresh-info').textContent = 'Refreshing\u2026';
  try {
    const r = await fetch('/jobs', { headers: authHeaders() });
    if (r.status === 401) { showAuth(); return; }
    allJobs = await r.json();
    renderTable();
    scheduleRefresh();
  } catch(e) {
    document.getElementById('refresh-info').textContent = 'Error \u2014 click to retry';
  }
}

function renderTable() {
  const search = document.getElementById('search').value.toLowerCase();
  const sf = document.getElementById('status-filter').value;

  let jobs = allJobs.filter(j =>
    (!sf || j.status === sf) &&
    (!search || j.job_id.toLowerCase().includes(search) || (j.name||'').toLowerCase().includes(search))
  );

  const dir = sortAsc ? 1 : -1;
  jobs.sort((a, b) => {
    const av = a[sortCol] ?? '', bv = b[sortCol] ?? '';
    return typeof av === 'number' ? (av - bv) * dir : String(av).localeCompare(String(bv)) * dir;
  });

  document.querySelectorAll('th[id^="th-"]').forEach(th => {
    const col = th.id.slice(3);
    th.classList.toggle('sorted', col === sortCol);
    th.querySelector('.sort-arrow').textContent = col === sortCol ? (sortAsc ? '\u2191' : '\u2193') : '';
  });

  const tbody = document.getElementById('jobs-body');
  if (!jobs.length) {
    tbody.innerHTML = '<tr><td colspan="7"><div class="empty">No jobs found.</div></td></tr>';
    return;
  }
  tbody.innerHTML = jobs.map(j => {
    const canLog = !!(j.host && j.port);
    const canDestroy = !TERMINAL.has(j.status);
    const id = esc(j.job_id);
    return `<tr>
      <td class="job-id">${id}</td>
      <td>${esc(j.name||'\u2014')}</td>
      <td><span class="badge badge-${j.status}">${esc(j.status)}</span></td>
      <td class="muted">${j.instance_id||'\u2014'}</td>
      <td class="muted">${rel(j.started_at)}</td>
      <td class="muted">${rel(j.updated_at)}</td>
      <td><div class="actions">
        ${canLog    ? `<button class="btn" onclick="showLogs('${id}')">Logs</button>` : ''}
        ${canDestroy? `<button class="btn btn-danger" onclick="destroyJob('${id}')">Destroy</button>` : ''}
      </div></td>
    </tr>`;
  }).join('');
}

function sortBy(col) {
  sortAsc = sortCol === col ? !sortAsc : (col !== 'started_at');
  sortCol = col;
  renderTable();
}

function rel(ts) {
  if (!ts) return '\u2014';
  const d = Math.floor(Date.now() / 1000 - ts);
  if (d < 5)     return 'just now';
  if (d < 60)    return d + 's ago';
  if (d < 3600)  return Math.floor(d/60) + 'm ago';
  if (d < 86400) return Math.floor(d/3600) + 'h ago';
  return Math.floor(d/86400) + 'd ago';
}

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

async function showLogs(jobId) {
  document.getElementById('modal-title').textContent = 'Logs \u2014 ' + jobId;
  document.getElementById('logs-content').textContent = 'Loading\u2026';
  document.getElementById('logs-modal').classList.remove('hidden');
  try {
    const r = await fetch(`/jobs/${jobId}/logs?n=500`, { headers: authHeaders() });
    const data = await r.json();
    const pre = document.getElementById('logs-content');
    pre.textContent = data.logs || '(no output yet)';
    pre.scrollTop = pre.scrollHeight;
  } catch(e) {
    document.getElementById('logs-content').textContent = 'Failed to fetch logs.';
  }
}

function closeLogs() {
  document.getElementById('logs-modal').classList.add('hidden');
}

async function destroyJob(jobId) {
  if (!confirm('Destroy job ' + jobId + '?')) return;
  await fetch(`/jobs/${jobId}`, { method: 'DELETE', headers: authHeaders() });
  fetchJobs();
}

function scheduleRefresh() {
  clearTimeout(refreshTimer); clearInterval(countdownInterval);
  let secs = REFRESH_SECS;
  const el = document.getElementById('refresh-info');
  el.textContent = 'Refresh in ' + secs + 's';
  countdownInterval = setInterval(() => {
    if (--secs <= 0) { clearInterval(countdownInterval); return; }
    el.textContent = 'Refresh in ' + secs + 's';
  }, 1000);
  refreshTimer = setTimeout(fetchJobs, REFRESH_SECS * 1000);
}

function showAuth() {
  document.getElementById('auth-overlay').classList.remove('hidden');
  setTimeout(() => document.getElementById('key-input').focus(), 50);
}

async function submitKey() {
  const key = document.getElementById('key-input').value.trim();
  const r = await fetch('/jobs', { headers: { 'Authorization': 'Bearer ' + key } });
  if (r.status === 401) {
    document.getElementById('auth-error').classList.remove('hidden');
    return;
  }
  sessionStorage.setItem('vastlaunch_key', key);
  document.getElementById('auth-overlay').classList.add('hidden');
  allJobs = await r.json();
  renderTable();
  scheduleRefresh();
}

document.getElementById('key-input').addEventListener('keydown', e => { if (e.key === 'Enter') submitKey(); });
document.getElementById('logs-modal').addEventListener('click', e => { if (e.target === e.currentTarget) closeLogs(); });

fetchJobs();
</script>
</body>
</html>"""
