'use strict';

const $ = (id) => document.getElementById(id);
const SVG_NS = 'http://www.w3.org/2000/svg';

const state = {
  boot: null,
  sites: [],
  session: null,
  activeSiteId: '',
  view: 'welcome',
  path: '/',
  entries: [],
  meta: {},
  loading: false,
  loadToken: 0,
  filter: '',
  sort: { key: 'name', dir: 1 },
  selected: new Map(),
  aria2: {},
  yunx: {},
  preferences: {},
  context: null,
  engine: 'browser',
  scan: null,
  scanTimer: null,
  queueOpen: false,
  queueTimer: null,
  tasks: [],
  queueLoading: false,
  loginSite: null,
  loginTab: 'password',
  connecting: false,
  lastConnectAuth: false,
  settingsInitial: null,
  busy: false,
};

const VIDEO_EXT = new Set(['mp4', 'mkv', 'avi', 'mov', 'wmv', 'flv', 'webm', 'm4v', 'ts', 'rmvb', 'mpg', 'mpeg', '3gp']);
const AUDIO_EXT = new Set(['mp3', 'flac', 'wav', 'aac', 'm4a', 'ogg', 'wma', 'ape', 'opus', 'dsf']);
const IMAGE_EXT = new Set(['jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp', 'svg', 'ico', 'tif', 'tiff', 'avif', 'heic']);
const DOC_EXT = new Set(['pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx', 'txt', 'md', 'epub', 'mobi', 'csv', 'rtf']);
const ARCHIVE_EXT = new Set(['zip', 'rar', '7z', 'tar', 'gz', 'bz2', 'xz', 'iso', 'apk', 'exe', 'msi', 'dmg']);
const CODE_EXT = new Set(['js', 'ts', 'py', 'go', 'rs', 'java', 'c', 'cpp', 'h', 'json', 'xml', 'html', 'css', 'sh', 'ps1', 'yml', 'yaml', 'toml', 'sql']);

function icon(name, cls = '') {
  const svg = document.createElementNS(SVG_NS, 'svg');
  if (cls) svg.setAttribute('class', cls);
  const use = document.createElementNS(SVG_NS, 'use');
  use.setAttribute('href', '#i-' + name);
  svg.appendChild(use);
  return svg;
}

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function debounce(fn, wait) {
  let timer = 0;
  return (...args) => {
    window.clearTimeout(timer);
    timer = window.setTimeout(() => fn(...args), wait);
  };
}

/* ---------------------------------------------------------------- API */

async function api(path, options = {}) {
  const opts = { method: options.method || 'GET', credentials: 'same-origin', headers: {} };
  if (options.body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(options.body);
  }
  let response;
  try {
    response = await fetch(path, opts);
  } catch (error) {
    const err = new Error('无法连接本地服务，请确认程序仍在运行');
    err.network = true;
    throw err;
  }
  let payload = null;
  const text = await response.text();
  try {
    payload = text ? JSON.parse(text) : null;
  } catch (error) {
    const err = new Error('本地服务返回了无法识别的数据');
    err.status = response.status;
    throw err;
  }
  if (!response.ok || !payload || payload.ok === false) {
    const info = (payload && payload.error) || {};
    const err = new Error(info.message || `请求失败（HTTP ${response.status}）`);
    err.status = response.status;
    err.auth = Boolean(info.auth);
    err.code = info.code;
    throw err;
  }
  return payload.data;
}

function setDirInput(id, value) {
  const input = $(id);
  if (!input) return;
  input.value = value || '';
  try {
    input.setSelectionRange(0, 0);
  } catch (error) {
    /* some input types do not support selection */
  }
}

function showError(node, message) {
  if (!node) return;
  node.textContent = message;
  node.hidden = !message;
}

function toast(message, type = 'info', duration = 3600) {
  const stack = $('toasts');
  if (!stack) return;
  const item = el('div', 'toast ' + type);
  item.appendChild(icon(type === 'error' ? 'alert' : type === 'success' ? 'check' : 'cloud'));
  item.appendChild(el('span', '', message));
  stack.appendChild(item);
  while (stack.children.length > 4) stack.firstElementChild.remove();
  window.setTimeout(() => {
    item.classList.add('leaving');
    window.setTimeout(() => item.remove(), 220);
  }, duration);
}

/* ------------------------------------------------------------- format */

function formatBytes(value) {
  const size = Number(value) || 0;
  if (size <= 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
  let index = 0;
  let number = size;
  while (number >= 1024 && index < units.length - 1) {
    number /= 1024;
    index += 1;
  }
  const digits = number >= 100 || index === 0 ? 0 : number >= 10 ? 1 : 2;
  return number.toFixed(digits) + ' ' + units[index];
}

function formatSpeed(value) {
  const size = Number(value) || 0;
  if (size <= 0) return '0 B/s';
  return formatBytes(size) + '/s';
}

function formatEta(seconds) {
  const total = Number(seconds) || 0;
  if (total <= 0) return '';
  if (total < 60) return total + ' 秒';
  if (total < 3600) return Math.floor(total / 60) + ' 分 ' + (total % 60) + ' 秒';
  return Math.floor(total / 3600) + ' 小时 ' + Math.floor((total % 3600) / 60) + ' 分';
}

function formatDate(value) {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value).slice(0, 16).replace('T', ' ');
  try {
    return new Intl.DateTimeFormat('zh-CN', {
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', hour12: false,
    }).format(date).replace(/\//g, '-');
  } catch (error) {
    return date.toISOString().slice(0, 16).replace('T', ' ');
  }
}

function fileMeta(name, isDir) {
  if (isDir) return { cls: 'folder', label: '' };
  const base = String(name || '');
  const dot = base.lastIndexOf('.');
  const ext = dot > 0 ? base.slice(dot + 1).toLowerCase() : '';
  let cls = '';
  if (VIDEO_EXT.has(ext)) cls = 'video';
  else if (AUDIO_EXT.has(ext)) cls = 'audio';
  else if (IMAGE_EXT.has(ext)) cls = 'image';
  else if (DOC_EXT.has(ext)) cls = 'doc';
  else if (ARCHIVE_EXT.has(ext)) cls = 'archive';
  else if (CODE_EXT.has(ext)) cls = 'code';
  const label = ext ? ext.slice(0, 4).toUpperCase() : 'FILE';
  return { cls, label };
}

/* --------------------------------------------------------------- theme */

function applyTheme(theme) {
  const value = theme === 'light' ? 'light' : 'dark';
  document.documentElement.dataset.theme = value;
  try { localStorage.setItem('old-theme', value); } catch (error) { /* ignore */ }
  document.querySelectorAll('[data-theme-choice]').forEach((button) => {
    button.classList.toggle('active', button.dataset.themeChoice === value);
  });
}

function preferredTheme() {
  try {
    return localStorage.getItem('old-theme') || 'dark';
  } catch (error) {
    return 'dark';
  }
}

/* -------------------------------------------------------------- header */

function updateHeader() {
  const name = state.session ? state.session.siteName : '尚未选择';
  $('currentSiteName').textContent = name;
  $('siteDot').classList.toggle('online', Boolean(state.session));
  $('appVersion').textContent = 'v' + (state.boot?.app?.version || '1.0.0');
}

function updateAria2(status) {
  if (status) state.aria2 = status;
  const info = state.aria2 || {};
  const badge = $('aria2Badge');
  const label = $('aria2Label');
  let text = '浏览器模式';
  let mode = 'off';
  if (info.available) {
    text = 'aria2 就绪';
    mode = 'ready';
  } else if (info.state === 'starting') {
    text = 'aria2 启动中';
    mode = 'busy';
  } else if (info.state === 'need_secret') {
    text = 'aria2 需要密钥';
    mode = 'busy';
  } else if (info.state === 'missing') {
    text = '浏览器模式';
    mode = 'off';
  } else if (info.state === 'error') {
    text = 'aria2 异常';
    mode = 'busy';
  }
  badge.classList.remove('ready', 'off', 'busy');
  badge.classList.add(mode);
  label.textContent = text;
  badge.title = info.available
    ? `aria2 ${info.version || ''} · ${info.endpoint || ''}`
    : (info.message || '点击启动内置 aria2');

  const tag = $('aria2EngineTag');
  if (tag) {
    tag.textContent = info.available ? (info.managed ? '内置' : '已连接') : '未连接';
  }
  const hint = $('aria2EngineHint');
  if (hint) {
    hint.textContent = info.available
      ? (info.version ? 'aria2 ' + info.version + ' · 多连接加速' : '多连接加速')
      : '点击下载时自动启动';
  }
  const statusText = $('settingsAria2Status');
  if (statusText) {
    statusText.textContent = info.available
      ? '已连接 ' + (info.version || '')
      : (info.message || '未连接');
    statusText.classList.toggle('ok', Boolean(info.available));
    statusText.classList.toggle('bad', !info.available && info.state === 'error');
  }
  const settingsHint = $('settingsAria2Hint');
  if (settingsHint) {
    settingsHint.textContent = info.available
      ? (info.managed ? '正在使用内置 aria2，通常无需修改。' : '正在使用外部 aria2。')
      : '内置 aria2 会在下载时自动启动，通常不需要修改这里。';
  }
  if (info.state === 'starting' || info.state === 'idle' || info.state === 'queued') {
    window.setTimeout(() => refreshAria2(true), 1800);
  }
}

async function refreshAria2(silent = false) {
  try {
    const data = await api('/api/aria2/status');
    updateAria2(data.aria2);
  } catch (error) {
    if (!silent) toast(error.message, 'error');
  }
}

async function ensureAria2() {
  if (state.aria2.available) return state.aria2;
  toast('正在启动内置 aria2…', 'info', 2200);
  const data = await api('/api/aria2/ensure', { method: 'POST' });
  updateAria2(data.aria2);
  if (!data.aria2.available) {
    throw new Error(data.aria2.message || 'aria2 未就绪，可改用浏览器下载');
  }
  toast('aria2 已就绪', 'success');
  return data.aria2;
}

/* --------------------------------------------------------------- sites */

function siteHost(url) {
  try {
    return new URL(url).host;
  } catch (error) {
    return url;
  }
}

function siteInitial(name) {
  const clean = String(name || '?').trim();
  if (!clean) return '?';
  const first = clean[0];
  return /[a-z0-9]/i.test(first) ? first.toUpperCase() : first;
}

function renderSites() {
  const list = $('siteList');
  list.replaceChildren();
  if (!state.sites.length) {
    list.appendChild(el('div', 'site-empty', '还没有资源站。点右上角 + 添加一个 OpenList / AList 站点。'));
    return;
  }
  const activeId = state.session?.siteId || state.activeSiteId;
  for (const site of state.sites) {
    const item = el('div', 'site-item' + (site.id === activeId ? ' active' : ''));
    item.dataset.siteId = site.id;
    const main = el('button', 'site-select');
    main.type = 'button';
    main.title = site.url;
    const avatar = el('span', 'site-avatar', siteInitial(site.name));
    const copy = el('span', 'site-item-copy');
    copy.appendChild(el('span', 'site-item-name', site.name || site.url));
    copy.appendChild(el('span', 'site-item-url', siteHost(site.url)));
    main.append(avatar, copy);
    main.addEventListener('click', () => {
      closeSidebar();
      if (state.session && state.session.siteId === site.id) {
        loadPath(state.path || '/');
      } else {
        connectSite(site.id, {}, { silent: false });
      }
    });
    const remove = el('button', 'site-remove');
    remove.type = 'button';
    remove.title = '移除资源站';
    remove.appendChild(icon('trash'));
    remove.addEventListener('click', (event) => {
      event.stopPropagation();
      deleteSite(site.id);
    });
    item.append(main, remove);
    list.appendChild(item);
  }
}

function renderSiteCards() {
  const box = $('siteCards');
  box.replaceChildren();
  if (!state.sites.length) {
    box.appendChild(el('div', 'site-cards-empty', '还没有保存的资源站，添加一个开始使用。'));
    return;
  }
  for (const site of state.sites) {
    const card = el('div', 'site-card' + (state.session?.siteId === site.id ? ' current' : ''));
    const main = el('button', 'site-main');
    main.type = 'button';
    main.appendChild(el('span', 'site-avatar', siteInitial(site.name)));
    const copy = el('span', 'site-card-copy');
    const title = el('b', '', site.name || site.url);
    copy.append(title, el('span', '', site.url));
    main.appendChild(copy);
    if (state.session?.siteId === site.id) {
      main.appendChild(el('span', 'badge', '当前'));
    }
    main.addEventListener('click', () => {
      closeModal('siteModal');
      connectSite(site.id, {}, { silent: false });
    });
    const remove = el('button', 'site-remove');
    remove.type = 'button';
    remove.title = '移除';
    remove.appendChild(icon('trash'));
    remove.addEventListener('click', (event) => {
      event.stopPropagation();
      deleteSite(site.id);
    });
    card.append(main, remove);
    box.appendChild(card);
  }
}

function renderDetected(urls) {
  const lists = [
    { wrap: $('detectedRow'), box: $('detectedList') },
    { wrap: $('modalDetected'), box: $('modalDetectedList') },
  ];
  for (const { wrap, box } of lists) {
    if (!box || !wrap) continue;
    box.replaceChildren();
    const items = Array.isArray(urls) ? urls : [];
    wrap.hidden = items.length === 0;
    for (const item of items) {
      const chip = el('button', 'detected-chip');
      chip.type = 'button';
      chip.title = '点击连接 ' + item.url;
      const dot = el('span', 'site-dot online');
      chip.append(dot, el('span', '', item.name || item.url));
      chip.addEventListener('click', () => addAndConnect(item.url, item.name || '本机 OpenList'));
      box.appendChild(chip);
    }
  }
}

async function deleteSite(siteId) {
  const site = state.sites.find((item) => item.id === siteId);
  if (!site) return;
  if (!window.confirm(`移除资源站「${site.name || site.url}」？已下载的文件不会删除。`)) return;
  try {
    await api('/api/sites/' + encodeURIComponent(siteId), { method: 'DELETE' });
    state.sites = state.sites.filter((item) => item.id !== siteId);
    if (state.session?.siteId === siteId) {
      state.session = null;
      state.activeSiteId = '';
      state.selected.clear();
      updateHeader();
      showSessionOrWelcome();
    }
    renderSites();
    renderSiteCards();
    toast('资源站已移除', 'success');
  } catch (error) {
    toast(error.message, 'error');
  }
}

const NETDISK_PATTERNS = [
  { host: /(?:^|\.)pan\.baidu\.com$|(?:^|\.)yun\.baidu\.com$/i, name: '百度网盘' },
  { host: /(?:^|\.)pan\.quark\.cn$/i, name: '夸克网盘' },
  { host: /(?:^|\.)aliyundrive\.com$/i, name: '阿里云盘' },
  { host: /(?:^|\.)pan\.xunlei\.com$/i, name: '迅雷网盘' },
  { host: /(?:^|\.)115\.com$/i, name: '115 网盘' },
  { host: /(?:^|\.)cloud\.189\.cn$/i, name: '天翼云盘' },
  { host: /(?:^|\.)uc\.cn$/i, name: 'UC 网盘' },
];

function normalizeInputUrl(text) {
  const value = String(text || '').trim();
  if (!value) return '';
  const match = value.match(/https?:\/\/[^\s]+/i);
  if (match) return match[0];
  if (/^[^\s/]+\.[^\s/]+/.test(value)) return 'https://' + value.split(/\s+/)[0];
  return value;
}

function netdiskName(text) {
  const url = normalizeInputUrl(text);
  try {
    const host = new URL(url).hostname.toLowerCase();
    const found = NETDISK_PATTERNS.find((item) => item.host.test(host));
    return found ? found.name : '';
  } catch (error) {
    return '';
  }
}

function isLikelyDirect(text) {
  const url = normalizeInputUrl(text);
  try {
    const parsed = new URL(url);
    const path = parsed.pathname;
    if (/\.(zip|rar|7z|tar|gz|mkv|mp4|avi|mov|mp3|flac|wav|pdf|epub|apk|exe|iso|txt|docx?|xlsx?|pptx?)$/i.test(path)) return true;
    if (path.startsWith('/d/') || path.startsWith('/download/')) return true;
    if (parsed.search && /filename|file=|download/i.test(parsed.search)) return true;
    return false;
  } catch (error) {
    return false;
  }
}

async function smartInputToYunx(text) {
  const name = netdiskName(text) || '网盘';
  const url = normalizeInputUrl(text);
  if (!state.yunx.installed) {
    openYunxModal();
    $('yunxUrl').value = text;
    showError($('yunxError'), `需要先安装云析，才能解析${name}分享链接。`);
    return;
  }
  try {
    await api('/api/yunx/open', { method: 'POST', body: { url: text } });
    $('welcomeUrl').value = '';
    toast(`已打开云析（${name}），登录后即可解析下载`, 'success', 5200);
  } catch (error) {
    openYunxModal();
    $('yunxUrl').value = text;
    showError($('yunxError'), error.message);
  }
}

async function handleSmartInput() {
  const raw = $('welcomeUrl').value.trim();
  if (!raw) {
    toast('请粘贴链接', 'error');
    return;
  }
  if (netdiskName(raw)) {
    await smartInputToYunx(raw);
    return;
  }
  const url = normalizeInputUrl(raw);
  if (!/^https?:\/\//i.test(url)) {
    toast('链接格式不对，请粘贴完整地址', 'error');
    return;
  }
  if (isLikelyDirect(raw)) {
    openDirectModal(raw);
    return;
  }
  const connected = await addAndConnect(url);
  if (connected === false && !state.lastConnectAuth) {
    toast('没有识别为 OpenList 站点，已按直链处理', 'info', 4200);
    openDirectModal(raw);
  }
}

async function addAndConnect(url, name) {
  const clean = String(url || '').trim();
  if (!clean) {
    toast('请输入资源站地址', 'error');
    return;
  }
  const button = $('welcomeAddBtn');
  const oldText = button.textContent;
  state.lastConnectAuth = false;
  button.disabled = true;
  button.textContent = '检测中…';
  try {
    const existing = state.sites.find((site) => site.url.replace(/\/+$/, '').toLowerCase() === clean.replace(/\/+$/, '').toLowerCase());
    let site;
    if (existing) {
      site = existing;
    } else {
      const data = await api('/api/sites', { method: 'POST', body: { url: clean, name: name || '' } });
      site = data.site;
      state.sites.push(site);
      state.sites.sort((a, b) => String(a.name).localeCompare(String(b.name), 'zh-CN'));
      renderSites();
      renderSiteCards();
    }
    $('welcomeUrl').value = '';
    $('newSiteUrl').value = '';
    $('newSiteName').value = '';
    closeModal('siteModal');
    return await connectSite(site.id, {}, { silent: false });
  } catch (error) {
    toast(error.message, 'error', 5200);
    return false;
  } finally {
    button.disabled = false;
    button.textContent = oldText;
  }
}

/* ------------------------------------------------------------ connecting */

function showSessionView(site, message) {
  setView('session');
  const name = site?.name || '资源站';
  $('sessionTitle').textContent = site ? `连接「${name}」` : '连接资源站';
  $('sessionText').textContent = message || '这个资源站需要登录后才能浏览文件。';
  $('sessionLoginBtn').dataset.siteId = site?.id || '';
  state.activeSiteId = site?.id || state.activeSiteId;
}

function showSessionOrWelcome() {
  if (state.sites.length) {
    const site = state.sites.find((item) => item.id === state.activeSiteId) || state.sites[0];
    showSessionView(site, '点击“登录资源站”继续，或换一个资源站。');
  } else {
    showWelcome();
  }
}

function showWelcome() {
  setView('welcome');
}

function showBrowser() {
  setView('browser');
}

function setView(view) {
  state.view = view;
  $('welcomeView').hidden = view !== 'welcome';
  $('browserView').hidden = view !== 'browser';
  $('sessionView').hidden = view !== 'session';
  $('navFiles').classList.toggle('active', view === 'browser');
  if (view === 'welcome' || view === 'session') {
    $('navFiles').classList.remove('active');
  }
}

function isNetdiskHost(url) {
  try {
    const host = new URL(url).hostname.toLowerCase();
    return [
      'pan.baidu.com', 'yun.baidu.com', 'aliyundrive.com', 'pan.quark.cn',
      '115.com', 'cloud.189.cn', 'drive.google.com', 'onedrive.live.com', 'mega.nz',
    ].some((item) => host === item || host.endsWith('.' + item));
  } catch (error) {
    return false;
  }
}

async function connectSite(siteId, credentials = {}, options = {}) {
  const site = state.sites.find((item) => item.id === siteId);
  if (!site) {
    toast('资源站不存在', 'error');
    return false;
  }
  if (isNetdiskHost(site.url)) {
    showSessionView(site, '这是网盘网页，不是 OpenList/AList 资源站。点“换一个资源站”移除它，填写你的 OpenList 地址；没有 OpenList 就用“链接下载”。');
    toast('pan.baidu.com 不能直接登录，请填写 OpenList 地址', 'error', 6000);
    return false;
  }
  if (state.connecting) return false;
  state.connecting = true;
  state.activeSiteId = siteId;
  try {
    const data = await api('/api/connect', {
      method: 'POST',
      body: {
        site_id: siteId,
        username: credentials.username || '',
        password: credentials.password || '',
        token: credentials.token || '',
        otp_code: credentials.otp_code || '',
      },
    });
    state.session = data.session;
    state.selected.clear();
    state.scan = null;
    stopScanPoll();
    updateHeader();
    renderSites();
    renderSiteCards();
    closeModal('loginModal');
    closeModal('siteModal');
    showError($('loginError'), '');
    toast(`已连接 ${data.session.siteName}`, 'success');
    const lastPath = localStorage.getItem('old-path:' + siteId) || '/';
    await loadPath(lastPath || '/');
    closeSidebar();
    return true;
  } catch (error) {
    state.lastConnectAuth = Boolean(error.auth);
    if (error.auth) {
      if (options.fromLogin) {
        showSessionView(site, '登录后即可浏览和下载。');
      } else if (options.silent) {
        showSessionView(site, '这个资源站需要登录后才能浏览文件。');
      } else {
        showSessionView(site, '登录后即可浏览和下载。');
        openLogin(site, error.message);
      }
    } else {
      if (options.silent) {
        showSessionView(site, error.message);
      } else {
        toast(error.message, 'error', 5200);
        if (state.view !== 'browser') showSessionView(site, error.message);
      }
    }
    return false;
  } finally {
    state.connecting = false;
  }
}

async function autoConnect() {
  if (state.session || !state.sites.length) {
    showSessionOrWelcome();
    return;
  }
  const preferred = state.sites.find((site) => site.id === state.activeSiteId) || state.sites[0];
  state.activeSiteId = preferred.id;
  showSessionView(preferred, '正在尝试连接…');
  await connectSite(preferred.id, {}, { silent: true });
}

/* ------------------------------------------------------------ login modal */

function openLogin(site, message = '') {
  if (!site) {
    openSiteModal();
    return;
  }
  if (isNetdiskHost(site.url)) {
    showSessionView(site, '这是网盘网页，不是 OpenList/AList 资源站。点“换一个资源站”移除它，填写你的 OpenList 地址；没有 OpenList 就用“链接下载”。');
    toast('这里不能登录百度账号', 'error', 5000);
    return;
  }
  state.loginSite = site;
  $('loginSiteName').textContent = site.name || site.url;
  $('loginSiteUrl').textContent = site.url;
  $('loginUsername').value = '';
  $('loginPassword').value = '';
  $('loginOtp').value = '';
  $('loginToken').value = '';
  showError($('loginError'), message);
  setLoginTab('password');
  openModal('loginModal');
  window.setTimeout(() => $('loginUsername').focus(), 80);
}

function setLoginTab(tab) {
  state.loginTab = tab;
  document.querySelectorAll('[data-login-tab]').forEach((button) => {
    button.classList.toggle('active', button.dataset.loginTab === tab);
  });
  $('passwordFields').hidden = tab !== 'password';
  $('tokenFields').hidden = tab !== 'token';
}

async function submitLogin(event) {
  event.preventDefault();
  if (!state.loginSite) return;
  const button = $('loginSubmit');
  button.disabled = true;
  showError($('loginError'), '');
  try {
    let credentials;
    if (state.loginTab === 'token') {
      const token = $('loginToken').value.trim();
      if (!token) throw new Error('请粘贴访问令牌');
      credentials = { token };
    } else {
      const username = $('loginUsername').value.trim();
      const password = $('loginPassword').value;
      if (!username || !password) throw new Error('请输入用户名和密码');
      credentials = { username, password, otp_code: $('loginOtp').value.trim() };
    }
    await connectSite(state.loginSite.id, credentials, { silent: false, fromLogin: true });
  } catch (error) {
    showError($('loginError'), error.message);
  } finally {
    button.disabled = false;
  }
}

async function guestLogin() {
  if (!state.loginSite) return;
  const button = $('guestLoginBtn');
  button.disabled = true;
  showError($('loginError'), '');
  try {
    await connectSite(state.loginSite.id, {}, { silent: false, fromLogin: true });
  } catch (error) {
    showError($('loginError'), error.message);
  } finally {
    button.disabled = false;
  }
}

/* --------------------------------------------------------------- browsing */

function joinPath(parent, name) {
  return (parent === '/' ? '/' : parent.replace(/\/+$/, '') + '/') + name;
}

function parentPath(path) {
  if (!path || path === '/') return '/';
  const trimmed = path.replace(/\/+$/, '');
  const index = trimmed.lastIndexOf('/');
  if (index <= 0) return '/';
  return trimmed.slice(0, index);
}

function ancestorSelected(path) {
  let current = path;
  while (current && current !== '/') {
    current = parentPath(current);
    const item = state.selected.get(current);
    if (item && item.is_dir) return current;
    if (current === '/') break;
  }
  return '';
}

async function loadPath(path, options = {}) {
  if (!state.session) {
    showSessionOrWelcome();
    return;
  }
  const target = path && path.startsWith('/') ? path : '/';
  const token = ++state.loadToken;
  showBrowser();
  state.path = target;
  state.filter = '';
  $('filterInput').value = '';
  $('clearFilter').hidden = true;
  state.loading = true;
  $('loadingState').hidden = false;
  $('emptyState').hidden = true;
  $('fileList').replaceChildren();
  renderBreadcrumb();
  $('itemSummary').textContent = '正在读取…';
  $('providerLabel').textContent = '';
  try {
    const data = await api('/api/fs/list', { method: 'POST', body: { path: target, refresh: Boolean(options.refresh) } });
    if (token !== state.loadToken) return;
    state.entries = Array.isArray(data.entries) ? data.entries : [];
    state.meta = data.meta || {};
    state.loading = false;
    renderEntries();
    try { localStorage.setItem('old-path:' + state.session.siteId, target); } catch (error) { /* ignore */ }
    if (window.innerWidth <= 860) closeSidebar();
  } catch (error) {
    if (token !== state.loadToken) return;
    state.loading = false;
    $('loadingState').hidden = true;
    $('itemSummary').textContent = '读取失败';
    if (error.auth) {
      handleAuthError(error);
    } else {
      toast(error.message, 'error', 5200);
      showEmpty('读取失败', error.message);
    }
  }
}

function showEmpty(title, text) {
  const empty = $('emptyState');
  empty.hidden = false;
  const h = empty.querySelector('h3');
  const p = empty.querySelector('p');
  if (h) h.textContent = title;
  if (p) p.textContent = text;
}

function renderBreadcrumb() {
  const box = $('breadcrumb');
  box.replaceChildren();
  const root = el('button', 'crumb', '全部文件');
  root.type = 'button';
  root.addEventListener('click', () => loadPath('/'));
  box.appendChild(root);
  const parts = state.path.split('/').filter(Boolean);
  let accum = '';
  parts.forEach((part, index) => {
    accum += '/' + part;
    const sep = el('span', 'crumb-sep');
    sep.appendChild(icon('chevron'));
    box.appendChild(sep);
    const crumb = el('button', 'crumb' + (index === parts.length - 1 ? ' current' : ''), part);
    crumb.type = 'button';
    crumb.title = accum;
    crumb.addEventListener('click', () => loadPath(accum));
    box.appendChild(crumb);
  });
  window.requestAnimationFrame(() => {
    box.scrollLeft = box.scrollWidth;
  });
}

function sortedEntries() {
  const keyword = state.filter.trim().toLowerCase();
  const list = state.entries.filter((entry) => !keyword || String(entry.name).toLowerCase().includes(keyword));
  const { key, dir } = state.sort;
  list.sort((a, b) => {
    if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1;
    let result = 0;
    if (key === 'size') result = (Number(a.size) || 0) - (Number(b.size) || 0);
    else if (key === 'modified') result = String(a.modified || '').localeCompare(String(b.modified || ''));
    else result = String(a.name || '').localeCompare(String(b.name || ''), 'zh-CN', { numeric: true, sensitivity: 'base' });
    if (result === 0) result = String(a.name || '').localeCompare(String(b.name || ''), 'zh-CN');
    return result * dir;
  });
  return list;
}

function renderEntries() {
  const list = sortedEntries();
  const fragment = document.createDocumentFragment();
  for (const entry of list) fragment.appendChild(buildRow(entry));
  $('fileList').replaceChildren(fragment);
  $('loadingState').hidden = true;
  const empty = $('emptyState');
  if (!list.length) {
    if (state.filter) showEmpty('没有匹配的项目', '换个关键词试试，或清除筛选。');
    else showEmpty('这个文件夹是空的', '换一个目录看看，或者刷新重试。');
  } else {
    empty.hidden = true;
  }
  const total = Number(state.meta.total) || state.entries.length;
  $('itemSummary').textContent = state.filter
    ? `${list.length} / ${total} 个项目`
    : `${total} 个项目`;
  $('pathSummary').textContent = state.path;
  $('providerLabel').textContent = state.meta.provider || '';
  updateSortIndicators();
  syncSelectionUI();
}

function buildRow(entry) {
  const row = el('div', 'file-row');
  row.dataset.path = entry.path;
  row.setAttribute('role', 'listitem');
  const check = el('label', 'check-wrap');
  const input = document.createElement('input');
  input.type = 'checkbox';
  input.dataset.path = entry.path;
  input.setAttribute('aria-label', '选择 ' + entry.name);
  check.append(input, el('span'));
  check.addEventListener('click', (event) => event.stopPropagation());

  const nameCell = el('div', 'file-name-cell');
  const meta = fileMeta(entry.name, entry.is_dir);
  const iconBox = el('span', 'file-icon ' + meta.cls);
  if (entry.is_dir) iconBox.appendChild(icon('folder'));
  else iconBox.textContent = meta.label;
  const nameButton = el('button', 'file-name-btn', entry.name);
  nameButton.type = 'button';
  nameButton.title = entry.name;
  nameButton.dataset.rowAction = 'open';
  nameButton.dataset.path = entry.path;
  nameCell.append(iconBox, nameButton);

  const size = el('span', 'file-size', entry.is_dir ? '—' : formatBytes(entry.size));
  const date = el('span', 'file-date', entry.is_dir ? '文件夹' : formatDate(entry.modified));

  const action = el('div', 'file-action');
  const download = el('button', 'row-icon-btn');
  download.type = 'button';
  download.title = entry.is_dir ? '下载整个文件夹' : '下载这个文件';
  download.dataset.rowAction = 'download';
  download.dataset.path = entry.path;
  download.appendChild(icon('download'));
  action.appendChild(download);

  row.append(check, nameCell, size, date, action);
  return row;
}

function findEntry(path) {
  return state.entries.find((entry) => entry.path === path);
}

function onFileListClick(event) {
  const target = event.target.closest('[data-row-action]');
  if (!target) return;
  const entry = findEntry(target.dataset.path);
  if (!entry) return;
  if (target.dataset.rowAction === 'open') {
    if (entry.is_dir) loadPath(entry.path);
    else openDownloadModal({ items: [entry], label: entry.name });
  } else if (target.dataset.rowAction === 'download') {
    openDownloadModal({
      items: [entry],
      label: entry.name,
      forcedZip: entry.is_dir,
    });
  }
}

function onFileListChange(event) {
  const input = event.target.closest('input[type="checkbox"][data-path]');
  if (!input) return;
  const entry = findEntry(input.dataset.path);
  if (!entry) return;
  setSelected(entry, input.checked);
}

function setSelected(entry, selected) {
  if (selected) {
    const ancestor = ancestorSelected(entry.path);
    if (ancestor) {
      toast('上级文件夹已经被选中', 'info', 1800);
      syncSelectionUI();
      return;
    }
    if (entry.is_dir) {
      for (const key of [...state.selected.keys()]) {
        if (key.startsWith(entry.path.replace(/\/+$/, '') + '/')) state.selected.delete(key);
      }
    }
    state.selected.set(entry.path, entry);
  } else {
    state.selected.delete(entry.path);
    const ancestor = ancestorSelected(entry.path);
    if (ancestor) state.selected.delete(ancestor);
  }
  syncSelectionUI();
}

function hasSelectedDescendant(path) {
  const prefix = path.replace(/\/+$/, '') + '/';
  for (const key of state.selected.keys()) {
    if (key.startsWith(prefix)) return true;
  }
  return false;
}

function syncSelectionUI() {
  const rows = $('fileList').querySelectorAll('.file-row');
  for (const row of rows) {
    const path = row.dataset.path;
    const entry = findEntry(path);
    if (!entry) continue;
    const exact = state.selected.has(path);
    const inherited = Boolean(ancestorSelected(path));
    const checked = exact || inherited;
    const indeterminate = !checked && entry.is_dir && hasSelectedDescendant(path);
    row.classList.toggle('selected', checked || indeterminate);
    const input = row.querySelector('input[type="checkbox"]');
    if (input) {
      input.checked = checked;
      input.indeterminate = indeterminate;
    }
  }
  updateSelectionBar();
  updateSelectAllState();
}

function updateSelectionBar() {
  const count = state.selected.size;
  $('selectionBar').hidden = count === 0;
  $('selectionCount').textContent = String(count);
  let size = 0;
  let folders = 0;
  for (const entry of state.selected.values()) {
    if (entry.is_dir) folders += 1;
    else size += Number(entry.size) || 0;
  }
  let text = formatBytes(size);
  if (folders) text += folders === count ? ' · 文件夹' : ` + ${folders} 个文件夹`;
  $('selectionSize').textContent = text;
}

function visibleEntryState(entry) {
  const exact = state.selected.has(entry.path);
  const inherited = Boolean(ancestorSelected(entry.path));
  return { checked: exact || inherited, indeterminate: !exact && !inherited && entry.is_dir && hasSelectedDescendant(entry.path) };
}

function updateSelectAllState() {
  const check = $('selectPageCheckbox');
  const rows = [...$('fileList').querySelectorAll('.file-row')].map((row) => findEntry(row.dataset.path)).filter(Boolean);
  if (!rows.length) {
    check.checked = false;
    check.indeterminate = false;
    check.disabled = true;
    return;
  }
  check.disabled = false;
  const states = rows.map(visibleEntryState);
  const all = states.every((item) => item.checked);
  const some = states.some((item) => item.checked || item.indeterminate);
  check.checked = all;
  check.indeterminate = !all && some;
}

function toggleSelectAllVisible() {
  const rows = [...$('fileList').querySelectorAll('.file-row')].map((row) => findEntry(row.dataset.path)).filter(Boolean);
  if (!rows.length) return;
  const all = rows.every((entry) => visibleEntryState(entry).checked);
  if (all) {
    for (const entry of rows) {
      state.selected.delete(entry.path);
      const ancestor = ancestorSelected(entry.path);
      if (ancestor) state.selected.delete(ancestor);
    }
  } else {
    for (const entry of rows) {
      if (visibleEntryState(entry).checked) continue;
      const ancestor = ancestorSelected(entry.path);
      if (ancestor) continue;
      if (entry.is_dir) {
        for (const key of [...state.selected.keys()]) {
          if (key.startsWith(entry.path.replace(/\/+$/, '') + '/')) state.selected.delete(key);
        }
      }
      state.selected.set(entry.path, entry);
    }
  }
  syncSelectionUI();
}

function clearSelection() {
  state.selected.clear();
  syncSelectionUI();
}

function updateSortIndicators() {
  document.querySelectorAll('.sortable').forEach((button) => {
    const indicator = button.querySelector('.sort-ind');
    if (!indicator) return;
    if (button.dataset.sort === state.sort.key) indicator.textContent = state.sort.dir > 0 ? '↑' : '↓';
    else indicator.textContent = '';
  });
}

function setSort(key) {
  if (state.sort.key === key) state.sort.dir *= -1;
  else {
    state.sort.key = key;
    state.sort.dir = key === 'modified' ? -1 : 1;
  }
  renderEntries();
}

function handleAuthError(error) {
  state.session = null;
  updateHeader();
  renderSites();
  const site = state.sites.find((item) => item.id === state.activeSiteId);
  showSessionView(site, '登录状态已失效，请重新登录。');
  toast(error.message || '请重新登录资源站', 'error');
}

/* ---------------------------------------------------------- download flow */

function selectionItems() {
  return [...state.selected.values()];
}

function openDownloadModal(context) {
  if (!state.session) {
    showSessionOrWelcome();
    return;
  }
  state.context = context;
  showError($('downloadError'), '');
  $('downloadProgress').hidden = true;
  $('downloadProgress').querySelector('.progress-bar i').style.width = '0%';
  $('downloadProgress').querySelector('span').textContent = '';

  const summary = $('downloadSummary');
  summary.replaceChildren();
  const iconBox = el('span', 'summary-icon');
  iconBox.appendChild(icon(context.scanId ? 'scan' : (context.items && context.items.length > 1 ? 'folder' : 'download')));
  const copy = el('div', 'summary-copy');
  const title = el('b', '', context.label || '下载所选');
  let details = '';
  if (context.scanId) details = `${context.count || 0} 个文件 · ${formatBytes(context.size || 0)}`;
  else if (context.items) {
    let size = 0;
    let folders = 0;
    for (const item of context.items) {
      if (item.is_dir) folders += 1;
      else size += Number(item.size) || 0;
    }
    details = `${context.items.length} 个项目` + (size ? ` · ${formatBytes(size)}` : '') + (folders ? ' · 含文件夹' : '');
  }
  copy.append(title, el('span', '', details));
  summary.append(iconBox, copy);
  $('downloadTitle').textContent = context.items && context.items.length === 1 && !context.items[0].is_dir ? '下载文件' : '下载所选';

  const defaultEngine = context.forcedZip ? 'browser' : (state.preferences.engine === 'aria2' ? 'aria2' : 'browser');
  setEngine(defaultEngine);
  setDirInput('downloadDir', state.aria2.dir || state.boot?.directories?.default || '');
  $('splitValue').textContent = String(state.aria2.split || 16);
  openModal('downloadModal');
}

function setEngine(engine) {
  if (engine === 'aria2' && state.context?.forcedZip) engine = 'browser';
  state.engine = engine;
  document.querySelectorAll('[data-engine]').forEach((button) => {
    button.classList.toggle('selected', button.dataset.engine === engine);
    if (button.dataset.engine === 'aria2') {
      button.classList.toggle('disabled', !state.aria2.available);
      button.title = state.aria2.available ? '使用 aria2 下载' : '点击后将自动启动内置 aria2';
    }
  });
  $('aria2Options').hidden = engine !== 'aria2';
  $('browserOptions').hidden = engine === 'aria2';
}

function adjustSplit(delta) {
  const current = Number($('splitValue').textContent) || 16;
  const next = Math.max(1, Math.min(32, current + delta));
  $('splitValue').textContent = String(next);
}

function setDownloadProgress(text, percent) {
  const strip = $('downloadProgress');
  strip.hidden = false;
  strip.querySelector('span').textContent = text;
  strip.querySelector('.progress-bar i').style.width = Math.max(0, Math.min(100, percent)) + '%';
}

function triggerBrowserDownload(url, filename) {
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename || '';
  anchor.rel = 'noopener';
  anchor.style.display = 'none';
  document.body.appendChild(anchor);
  anchor.click();
  window.setTimeout(() => anchor.remove(), 1000);
}

async function fetchScanFiles(scanId) {
  const files = [];
  const limit = 400;
  let offset = 0;
  let total = 0;
  do {
    const data = await api(`/api/scan/${encodeURIComponent(scanId)}/files?offset=${offset}&limit=${limit}`);
    total = data.total || 0;
    files.push(...(data.files || []));
    offset += (data.files || []).length;
    const percent = total ? Math.round(offset * 100 / total) : 10;
    setDownloadProgress(`读取文件列表 ${offset} / ${total}`, Math.min(90, percent));
    if (!data.files || !data.files.length) break;
  } while (offset < total);
  return files;
}

async function resolveContextFiles(context) {
  if (context.scanId) return fetchScanFiles(context.scanId);
  if (context.files) return context.files;
  if (context.items) {
    setDownloadProgress('正在整理文件夹…', 5);
    const data = await api('/api/expand', { method: 'POST', body: { items: context.items } });
    return data.files || [];
  }
  return [];
}

async function startDownload() {
  if (!state.context || state.busy) return;
  const button = $('startDownloadBtn');
  state.busy = true;
  button.disabled = true;
  showError($('downloadError'), '');
  try {
    if (state.engine === 'browser') {
      setDownloadProgress('正在准备下载…', 8);
      const body = { zip: Boolean(state.context.forcedZip) };
      if (state.context.scanId) body.scan_id = state.context.scanId;
      else if (state.context.items) body.items = state.context.items;
      else if (state.context.files) body.files = state.context.files;
      const data = await api('/api/download/prepare', { method: 'POST', body });
      setDownloadProgress('已准备 ' + data.count + ' 个文件，正在交给浏览器…', 100);
      triggerBrowserDownload(data.url, data.filename);
      toast(data.kind === 'zip' ? '正在打包下载 ZIP' : '浏览器下载已开始', 'success');
      window.setTimeout(() => closeModal('downloadModal'), 550);
    } else {
      await ensureAria2();
      setDownloadProgress('正在读取文件信息…', 4);
      const files = await resolveContextFiles(state.context);
      if (!files.length) throw new Error('没有找到可下载的文件');
      const chunkSize = 12;
      let added = 0;
      let failed = 0;
      const errors = [];
      const baseDir = $('downloadDir').value.trim() || state.aria2.dir || '';
      const split = Number($('splitValue').textContent) || 8;
      for (let index = 0; index < files.length; index += chunkSize) {
        const chunk = files.slice(index, index + chunkSize);
        const data = await api('/api/aria2/add', {
          method: 'POST',
          body: { files: chunk, base_dir: baseDir, split },
        });
        added += data.added || 0;
        failed += data.failed || 0;
        if (Array.isArray(data.errors)) errors.push(...data.errors);
        const done = Math.min(index + chunk.length, files.length);
        setDownloadProgress(`已加入 aria2：${added} / ${files.length}`, Math.round(done * 100 / files.length));
      }
      if (!added) {
        const first = errors[0]?.message || 'aria2 没有接受任何任务';
        throw new Error(first);
      }
      if (failed) {
        toast(`已加入 ${added} 个任务，${failed} 个失败${errors[0] ? '：' + errors[0].message : ''}`, 'error', 6000);
      } else {
        toast(`已加入 ${added} 个 aria2 任务`, 'success');
      }
      state.aria2.split = split;
      closeModal('downloadModal');
      openQueue();
    }
  } catch (error) {
    showError($('downloadError'), error.message);
    if (error.auth) handleAuthError(error);
  } finally {
    state.busy = false;
    button.disabled = false;
  }
}

/* ---------------------------------------------------------------- scanner */

async function openScan() {
  if (!state.session) {
    showSessionOrWelcome();
    return;
  }
  openModal('scanModal');
  showError($('scanError'), '');
  if (state.scan && ['queued', 'running'].includes(state.scan.state)) {
    renderScan(state.scan);
    scheduleScanPoll();
    return;
  }
  if (state.scan && ['done', 'limited', 'cancelled'].includes(state.scan.state) && state.scan.files > 0) {
    renderScan(state.scan);
    return;
  }
  await startScan();
}

async function startScan() {
  if (!state.session) return;
  stopScanPoll();
  state.scan = null;
  $('scanFiles').textContent = '0';
  $('scanSize').textContent = '0 B';
  $('scanDirs').textContent = '0';
  $('scanDownloadBtn').disabled = true;
  $('scanCancelBtn').textContent = '停止扫描';
  $('scanVisual').querySelector('.scan-radar').classList.remove('done');
  $('scanStateTitle').textContent = '正在遍历资源站';
  $('scanStateText').textContent = '首次扫描会读取每个文件夹，站点越大耗时越长。';
  $('scanCurrent').textContent = '准备中…';
  showError($('scanError'), '');
  try {
    const data = await api('/api/scan/start', { method: 'POST', body: {} });
    state.scan = data.scan;
    renderScan(data.scan);
    scheduleScanPoll();
  } catch (error) {
    showError($('scanError'), error.message);
    $('scanStateTitle').textContent = '扫描失败';
    $('scanStateText').textContent = error.message;
    $('scanCancelBtn').textContent = '重新扫描';
    if (error.auth) handleAuthError(error);
  }
}

function scheduleScanPoll() {
  stopScanPoll();
  state.scanTimer = window.setTimeout(pollScan, 700);
}

function stopScanPoll() {
  if (state.scanTimer) {
    window.clearTimeout(state.scanTimer);
    state.scanTimer = null;
  }
}

async function pollScan() {
  if (!state.scan) return;
  try {
    const data = await api('/api/scan/' + encodeURIComponent(state.scan.id));
    state.scan = data.scan;
    renderScan(data.scan);
    if (['queued', 'running'].includes(data.scan.state)) {
      scheduleScanPoll();
    }
  } catch (error) {
    stopScanPoll();
    showError($('scanError'), error.message);
    $('scanStateTitle').textContent = '扫描中断';
    $('scanStateText').textContent = error.message;
  }
}

function renderScan(scan) {
  state.scan = scan;
  $('scanFiles').textContent = (scan.files || 0).toLocaleString('zh-CN');
  $('scanSize').textContent = formatBytes(scan.size);
  $('scanDirs').textContent = (scan.dirsDone || 0).toLocaleString('zh-CN');
  $('scanCurrent').textContent = scan.state === 'running'
    ? `已读取 ${scan.dirsDone || 0} / ${scan.dirsQueued || 0} 个目录`
    : (scan.message || '');
  const radar = $('scanVisual').querySelector('.scan-radar');
  const title = $('scanStateTitle');
  const text = $('scanStateText');
  const downloadButton = $('scanDownloadBtn');
  const cancelButton = $('scanCancelBtn');
  radar.classList.toggle('done', ['done', 'limited', 'cancelled', 'error'].includes(scan.state));
  if (scan.state === 'running' || scan.state === 'queued') {
    title.textContent = '正在遍历资源站';
    text.textContent = scan.dirsDone > 0 ? '正在读取目录，文件数会持续增加…' : '正在读取根目录…';
    downloadButton.disabled = true;
    cancelButton.textContent = '停止扫描';
  } else if (scan.state === 'done') {
    title.textContent = '扫描完成';
    text.textContent = `发现 ${Number(scan.files || 0).toLocaleString('zh-CN')} 个文件，共 ${formatBytes(scan.size)}。`;
    downloadButton.disabled = !scan.files;
    cancelButton.textContent = '重新扫描';
    if (scan.errors) showError($('scanError'), `有 ${scan.errors} 个目录读取失败，已跳过。`);
  } else if (scan.state === 'limited') {
    title.textContent = '达到扫描上限';
    text.textContent = scan.message || '文件太多，已停止扫描，可先下载已发现的部分。';
    downloadButton.disabled = !scan.files;
    cancelButton.textContent = '重新扫描';
  } else if (scan.state === 'cancelled') {
    title.textContent = '扫描已停止';
    text.textContent = `已发现 ${Number(scan.files || 0).toLocaleString('zh-CN')} 个文件，可下载已发现的部分。`;
    downloadButton.disabled = !scan.files;
    cancelButton.textContent = '重新扫描';
  } else if (scan.state === 'error') {
    title.textContent = '扫描失败';
    text.textContent = scan.message || '请稍后重试。';
    downloadButton.disabled = !scan.files;
    cancelButton.textContent = '重新扫描';
  }
  $('scanNavBadge').hidden = !(scan.files > 0);
  $('scanNavBadge').textContent = scan.files > 0 ? String(scan.files) : '';
}

async function cancelOrRestartScan() {
  if (!state.scan) return;
  if (['queued', 'running'].includes(state.scan.state)) {
    $('scanCancelBtn').disabled = true;
    try {
      const data = await api('/api/scan/' + encodeURIComponent(state.scan.id) + '/cancel', { method: 'POST' });
      state.scan = data.scan;
      renderScan(data.scan);
    } catch (error) {
      showError($('scanError'), error.message);
    } finally {
      $('scanCancelBtn').disabled = false;
    }
  } else {
    await startScan();
  }
}

/* ------------------------------------------------------------------ queue */

function openQueue() {
  $('queueLayer').hidden = false;
  state.queueOpen = true;
  $('navQueue').classList.add('active');
  loadQueue();
  if (state.queueTimer) window.clearTimeout(state.queueTimer);
  const poll = async () => {
    if (!state.queueOpen) return;
    await loadQueue(true);
    state.queueTimer = window.setTimeout(poll, 1500);
  };
  state.queueTimer = window.setTimeout(poll, 1500);
}

function closeQueue() {
  $('queueLayer').hidden = true;
  state.queueOpen = false;
  $('navQueue').classList.remove('active');
  if (state.queueTimer) {
    window.clearTimeout(state.queueTimer);
    state.queueTimer = null;
  }
}

async function loadQueue(silent = false) {
  if (state.queueLoading) return;
  state.queueLoading = true;
  try {
    const data = await api('/api/aria2/tasks');
    state.tasks = data.tasks || [];
    if (data.available === false && state.aria2.available) {
      updateAria2({ ...state.aria2, available: false, state: 'offline', message: 'aria2 连接已断开' });
    }
    renderQueue(data);
  } catch (error) {
    if (!silent) toast(error.message, 'error');
  } finally {
    state.queueLoading = false;
  }
}

function renderQueue(data) {
  const tasks = data.tasks || [];
  const list = $('queueList');
  list.replaceChildren();
  const empty = $('queueEmpty');
  const available = data.available !== false;
  empty.hidden = tasks.length > 0;
  if (!tasks.length) {
    const h = empty.querySelector('h3');
    const p = empty.querySelector('p');
    if (!available) {
      h.textContent = 'aria2 未连接';
      p.textContent = state.aria2.message || '启动内置 aria2 后，任务会显示在这里。';
    } else {
      h.textContent = '还没有 aria2 任务';
      p.textContent = '选择文件后使用 aria2 下载，任务会出现在这里。';
    }
  }
  for (const task of tasks) list.appendChild(buildQueueTask(task));
  const stats = data.global || {};
  $('queueSpeed').textContent = formatSpeed(stats.downloadSpeed);
  $('queueActive').textContent = String(stats.numActive || tasks.filter((task) => task.status === 'active').length);
  $('queueWaiting').textContent = String(stats.numWaiting || tasks.filter((task) => task.status === 'waiting').length);
  $('queueDone').textContent = String(tasks.filter((task) => task.status === 'complete').length);
  $('queueFailed').textContent = String(tasks.filter((task) => task.status === 'error').length);
  const pending = tasks.filter((task) => ['active', 'waiting', 'paused'].includes(task.status)).length;
  $('queueNavBadge').hidden = pending === 0;
  $('queueNavBadge').textContent = String(pending);
}

function buildQueueTask(task) {
  const wrap = el('div', 'queue-task');
  const top = el('div', 'task-top');
  const name = el('div', 'task-name', task.arcname || task.name || '未命名');
  name.title = task.arcname || task.name || '';
  const status = el('span', 'task-status ' + task.status, task.statusText || task.status);
  top.append(name, status);
  const meta = el('div', 'task-meta');
  const percent = Number(task.percent) || 0;
  const pieces = [
    `${formatBytes(task.completed)} / ${formatBytes(task.total)}`,
    `${percent.toFixed(percent >= 100 ? 0 : 1)}%`,
  ];
  if (task.status === 'active') {
    pieces.push(formatSpeed(task.speed));
    if (task.eta) pieces.push('剩余 ' + formatEta(task.eta));
  }
  if (task.connections) pieces.push(task.connections + ' 连接');
  pieces.forEach((text, index) => {
    if (index > 0) meta.appendChild(el('span', '', '·'));
    meta.appendChild(el('span', index === 2 && task.status === 'active' ? 'speed' : '', text));
  });
  const progress = el('div', 'task-progress');
  const fill = el('i');
  fill.style.width = Math.max(0, Math.min(100, percent)) + '%';
  progress.appendChild(fill);
  wrap.append(top, meta, progress);
  if (task.error) wrap.appendChild(el('div', 'task-error', task.error));
  const actions = el('div', 'task-actions');
  if (['active', 'waiting'].includes(task.status)) {
    actions.appendChild(taskButton('pause', '暂停', task.gid));
  } else if (task.status === 'paused') {
    actions.appendChild(taskButton('resume', '继续', task.gid));
  }
  actions.appendChild(taskButton('remove', '移除', task.gid));
  wrap.appendChild(actions);
  return wrap;
}

function taskButton(action, title, gid) {
  const button = el('button', 'row-icon-btn');
  button.type = 'button';
  button.title = title;
  button.dataset.taskAction = action;
  button.dataset.gid = gid;
  button.appendChild(icon(action === 'pause' ? 'pause' : action === 'resume' ? 'play' : 'trash'));
  return button;
}

async function controlTask(gid, action) {
  try {
    await api('/api/aria2/control', { method: 'POST', body: { gid, action } });
    await loadQueue(true);
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function clearQueue() {
  const gids = state.tasks.filter((task) => ['complete', 'error', 'removed'].includes(task.status)).map((task) => task.gid);
  if (!gids.length) {
    toast('没有已完成的任务需要清理', 'info');
    return;
  }
  try {
    await api('/api/aria2/clear', { method: 'POST', body: { gids } });
    await loadQueue(true);
    toast('已清理完成的任务', 'success');
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function clearAllQueue() {
  if (!state.tasks.length) {
    toast('下载列表已经是空的', 'info');
    return;
  }
  if (!window.confirm('一键清空全部下载记录？正在下载的任务会停止，已经下载好的文件会保留。')) return;
  const button = $('clearAllQueueBtn');
  button.disabled = true;
  try {
    const data = await api('/api/aria2/clear-all', {
      method: 'POST',
      body: { stop_active: true },
    });
    state.tasks = [];
    renderQueue({ available: state.aria2.available, tasks: [], global: {} });
    toast(`已清空 ${data.cleared || 0} 条记录`, 'success');
  } catch (error) {
    toast(error.message, 'error');
  } finally {
    button.disabled = false;
  }
}

function renderYunxStatus() {
  const status = state.yunx || {};
  const box = $('yunxStatus');
  if (!box) return;
  box.classList.remove('ready', 'missing');
  if (status.installed) {
    box.classList.add('ready');
    box.textContent = `已找到云析${status.version ? ' ' + status.version : ''}：${status.path}`;
  } else {
    box.classList.add('missing');
    box.textContent = '未找到云析。请把 YunX 文件夹放到程序目录，或点“获取云析”下载便携版。';
  }
}

async function openYunxModal() {
  showError($('yunxError'), '');
  renderYunxStatus();
  openModal('yunxModal');
  try {
    state.yunx = await api('/api/yunx/status');
    renderYunxStatus();
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function launchYunx(withUrl) {
  const url = withUrl ? $('yunxUrl').value.trim() : '';
  if (withUrl && !url) {
    showError($('yunxError'), '请先粘贴网盘分享链接');
    return;
  }
  const button = withUrl ? $('startYunxBtn') : $('openYunxOnlyBtn');
  button.disabled = true;
  showError($('yunxError'), '');
  try {
    const data = await api('/api/yunx/open', {
      method: 'POST',
      body: { url },
    });
    if (data.copied) {
      toast('链接已复制并打开云析。首次使用请在云析中登录网盘。', 'success', 5200);
    } else {
      toast('云析已启动', 'success');
    }
    closeModal('yunxModal');
  } catch (error) {
    showError($('yunxError'), error.message);
    if (error.status === 404) {
      state.yunx = { ...(state.yunx || {}), installed: false };
      renderYunxStatus();
    }
  } finally {
    button.disabled = false;
  }
}

function openYunxRelease() {
  const url = state.yunx?.releaseUrl || 'https://github.com/tidain/YunX-Desktop/releases/latest';
  window.open(url, '_blank', 'noopener');
}

function openDirectModal(prefill = '') {
  showError($('directError'), '');
  $('directProgress').hidden = true;
  $('directProgress').querySelector('.progress-bar i').style.width = '0%';
  setDirInput('directDir', state.aria2.dir || state.boot?.directories?.default || '');
  $('directEngine').value = state.aria2.available ? 'aria2' : 'browser';
  if (prefill) {
    $('directUrls').value = prefill;
  }
  openModal('directModal');
  window.setTimeout(() => $('directUrls').focus(), 80);
}

async function startDirectDownload() {
  const button = $('startDirectBtn');
  const urls = $('directUrls').value
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
  if (!urls.length) {
    showError($('directError'), '请至少粘贴一个下载链接');
    return;
  }
  if (urls.length > 200) {
    showError($('directError'), '一次最多添加 200 个链接');
    return;
  }
  const engine = $('directEngine').value;
  if (engine === 'browser' && urls.length !== 1) {
    showError($('directError'), '浏览器模式一次只能下载一个链接，多个链接请用 aria2');
    return;
  }
  button.disabled = true;
  showError($('directError'), '');
  const progress = $('directProgress');
  progress.hidden = false;
  progress.querySelector('.progress-bar i').style.width = '15%';
  progress.querySelector('span').textContent = engine === 'aria2' ? '正在创建 aria2 任务…' : '正在准备下载…';
  try {
    if (engine === 'aria2' && !state.aria2.available) {
      await ensureAria2();
    }
    const data = await api('/api/direct/add', {
      method: 'POST',
      body: {
        urls,
        headers: $('directHeaders').value,
        engine,
        base_dir: $('directDir').value.trim(),
        split: state.aria2.split || 16,
        direct: state.aria2.direct !== false,
      },
    });
    progress.querySelector('.progress-bar i').style.width = '100%';
    if (engine === 'browser') {
      progress.querySelector('span').textContent = '正在交给浏览器…';
      triggerBrowserDownload(data.url, data.filename);
      toast('浏览器直链下载已开始', 'success');
      window.setTimeout(() => closeModal('directModal'), 550);
    } else {
      progress.querySelector('span').textContent = `已加入 ${data.added || 0} 个任务`;
      if (data.failed) {
        toast(`已加入 ${data.added} 个，失败 ${data.failed} 个`, 'error', 5000);
      } else {
        toast(`已加入 ${data.added} 个 aria2 直链任务`, 'success');
      }
      closeModal('directModal');
      openQueue();
    }
  } catch (error) {
    showError($('directError'), error.message);
    if (error.auth) handleAuthError(error);
  } finally {
    button.disabled = false;
  }
}

/* --------------------------------------------------------------- settings */

async function openSettings() {
  try {
    const data = await api('/api/settings');
    state.settingsInitial = data;
    setDirInput('settingsDir', data.downloadDir || '');
    $('settingsEngine').value = data.preferences?.engine || 'browser';
    $('settingsSplit').value = String(data.split || 16);
    $('settingsConcurrency').value = String(data.concurrency || 8);
    $('settingsDirect').value = data.direct === false ? 'proxy' : 'direct';
    $('settingsEndpoint').value = data.endpoint || '';
    $('settingsSecret').value = '';
    applyTheme(data.preferences?.theme || preferredTheme());
    showError($('settingsError'), '');
    updateAria2(state.aria2);
    openModal('settingsModal');
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function testAria2() {
  const button = $('testAria2Btn');
  button.disabled = true;
  button.textContent = '连接中…';
  showError($('settingsError'), '');
  try {
    const endpoint = $('settingsEndpoint').value.trim();
    const secret = $('settingsSecret').value;
    const dir = $('settingsDir').value.trim();
    if (!endpoint) throw new Error('请填写 aria2 RPC 地址');
    const data = await api('/api/aria2/config', {
      method: 'POST',
      body: { endpoint, secret, dir },
    });
    updateAria2(data.aria2);
    if (data.aria2.available) toast('aria2 连接成功', 'success');
    else throw new Error(data.aria2.message || '连接失败');
  } catch (error) {
    showError($('settingsError'), error.message);
  } finally {
    button.disabled = false;
    button.textContent = '测试 / 连接';
  }
}

async function startBuiltInAria2() {
  const button = $('startAria2Btn');
  button.disabled = true;
  button.textContent = '启动中…';
  showError($('settingsError'), '');
  try {
    const data = await api('/api/aria2/ensure', { method: 'POST' });
    updateAria2(data.aria2);
    if (data.aria2.available) {
      toast('aria2 已启动', 'success');
      $('settingsEndpoint').value = data.aria2.endpoint || '';
      $('settingsSecret').value = '';
    } else {
      throw new Error(data.aria2.message || '启动失败');
    }
  } catch (error) {
    showError($('settingsError'), error.message);
  } finally {
    button.disabled = false;
    button.textContent = '启动内置 aria2';
  }
}

async function applyTurboPreset() {
  $('settingsSplit').value = '16';
  $('settingsConcurrency').value = '10';
  $('settingsDirect').value = 'direct';
  toast('已填入极速参数，正在保存并应用…', 'info', 1800);
  await saveSettings();
}

async function saveSettings() {
  const button = $('saveSettingsBtn');
  button.disabled = true;
  showError($('settingsError'), '');
  try {
    const dir = $('settingsDir').value.trim();
    if (!dir) throw new Error('请填写默认保存目录');
    const engine = $('settingsEngine').value;
    const split = Number($('settingsSplit').value) || 16;
    const concurrency = Number($('settingsConcurrency').value) || 8;
    const direct = $('settingsDirect').value !== 'proxy';
    const theme = document.documentElement.dataset.theme || 'dark';
    await api('/api/settings', {
      method: 'POST',
      body: { downloadDir: dir, engine, split, concurrency, direct, theme },
    });
    state.preferences = { ...state.preferences, engine, theme };
    state.aria2 = { ...state.aria2, dir, split, concurrency, direct };
    updateAria2(state.aria2);
    if (state.boot) state.boot.directories.default = dir;
    const endpoint = $('settingsEndpoint').value.trim();
    const secret = $('settingsSecret').value;
    const changedEndpoint = endpoint && endpoint !== (state.settingsInitial?.endpoint || '');
    if (changedEndpoint || secret) {
      const data = await api('/api/aria2/config', {
        method: 'POST',
        body: { endpoint, secret, dir },
      });
      updateAria2(data.aria2);
    }
    applyTheme(theme);
    closeModal('settingsModal');
    toast('设置已保存', 'success');
  } catch (error) {
    showError($('settingsError'), error.message);
  } finally {
    button.disabled = false;
  }
}

async function openFolder(path) {
  const target = String(path || '').trim();
  if (!target) {
    toast('还没有设置下载目录', 'error');
    return;
  }
  try {
    await api('/api/open-folder', { method: 'POST', body: { path: target } });
  } catch (error) {
    toast(error.message, 'error');
  }
}

/* ---------------------------------------------------------------- modals */

function openModal(id) {
  const node = $(id);
  if (!node) return;
  node.hidden = false;
  window.setTimeout(() => {
    const focusable = node.querySelector('input:not([type="hidden"]), select, button.primary-btn');
    if (focusable) focusable.focus();
  }, 60);
}

function closeModal(id) {
  const node = $(id);
  if (!node) return;
  node.hidden = true;
  if (id === 'queueLayer') closeQueue();
}

function closeTopLayer() {
  const layers = [...document.querySelectorAll('.modal-layer:not([hidden]), .drawer-layer:not([hidden])')];
  if (!layers.length) return;
  const top = layers[layers.length - 1];
  if (top.id === 'queueLayer') closeQueue();
  else closeModal(top.id);
}

function openSiteModal() {
  renderSiteCards();
  renderDetected(state.boot?.detected || []);
  showError($('loginError'), '');
  openModal('siteModal');
}

function closeSidebar() {
  $('sidebar').classList.remove('open');
  $('sidebarScrim').hidden = true;
}

function openSidebar() {
  $('sidebar').classList.add('open');
  $('sidebarScrim').hidden = false;
}

/* ------------------------------------------------------------------- init */

function bindEvents() {
  $('siteSwitch').addEventListener('click', openSiteModal);
  $('addSiteBtn').addEventListener('click', openSiteModal);
  $('helpBtn').addEventListener('click', () => openModal('helpModal'));
  $('settingsBtn').addEventListener('click', openSettings);
  $('aria2Badge').addEventListener('click', async () => {
    if (state.aria2.available) openQueue();
    else {
      try { await ensureAria2(); } catch (error) { toast(error.message, 'error'); }
    }
  });
  $('navFiles').addEventListener('click', () => {
    if (state.session) {
      showBrowser();
      closeSidebar();
    } else {
      showSessionOrWelcome();
      closeSidebar();
    }
  });
  $('navScan').addEventListener('click', () => { closeSidebar(); openScan(); });
  $('navDirect').addEventListener('click', () => { closeSidebar(); openDirectModal(); });
  $('navYunx').addEventListener('click', () => { closeSidebar(); openYunxModal(); });
  $('navQueue').addEventListener('click', () => { closeSidebar(); openQueue(); });
  $('sidebarToggle').addEventListener('click', openSidebar);
  $('sidebarScrim').addEventListener('click', closeSidebar);

  $('welcomeAddBtn').addEventListener('click', handleSmartInput);
  $('welcomeUrl').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      handleSmartInput();
    }
  });
  $('addSiteForm').addEventListener('submit', (event) => {
    event.preventDefault();
    addAndConnect($('newSiteUrl').value, $('newSiteName').value);
  });

  document.querySelectorAll('[data-close]').forEach((node) => {
    node.addEventListener('click', () => closeModal(node.dataset.close));
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') closeTopLayer();
  });

  $('sessionLoginBtn').addEventListener('click', () => {
    const site = state.sites.find((item) => item.id === ($('sessionLoginBtn').dataset.siteId || state.activeSiteId));
    if (site) openLogin(site);
    else openSiteModal();
  });
  $('sessionSwitchBtn').addEventListener('click', openSiteModal);

  document.querySelectorAll('[data-login-tab]').forEach((button) => {
    button.addEventListener('click', () => setLoginTab(button.dataset.loginTab));
  });
  $('loginForm').addEventListener('submit', submitLogin);
  $('guestLoginBtn').addEventListener('click', guestLogin);

  $('upBtn').addEventListener('click', () => loadPath(parentPath(state.path)));
  $('refreshBtn').addEventListener('click', () => loadPath(state.path, { refresh: true }));
  $('selectAllBtn').addEventListener('click', toggleSelectAllVisible);
  $('selectPageCheckbox').addEventListener('change', toggleSelectAllVisible);
  $('clearSelection').addEventListener('click', clearSelection);
  $('downloadSelected').addEventListener('click', () => openDownloadModal({ items: selectionItems(), label: '下载所选' }));
  $('downloadSelectedZip').addEventListener('click', () => openDownloadModal({ items: selectionItems(), label: '打包下载', forcedZip: true }));
  $('filterInput').addEventListener('input', debounce((event) => {
    state.filter = event.target.value.trim();
    $('clearFilter').hidden = !state.filter;
    renderEntries();
  }, 120));
  $('clearFilter').addEventListener('click', () => {
    $('filterInput').value = '';
    state.filter = '';
    $('clearFilter').hidden = true;
    renderEntries();
    $('filterInput').focus();
  });
  document.querySelectorAll('.sortable').forEach((button) => {
    button.addEventListener('click', () => setSort(button.dataset.sort));
  });
  $('fileList').addEventListener('click', onFileListClick);
  $('fileList').addEventListener('change', onFileListChange);

  $('scanBtn').addEventListener('click', openScan);
  $('scanCancelBtn').addEventListener('click', cancelOrRestartScan);
  $('scanDownloadBtn').addEventListener('click', () => {
    if (!state.scan) return;
    closeModal('scanModal');
    openDownloadModal({
      scanId: state.scan.id,
      label: '整站全部文件',
      count: state.scan.files,
      size: state.scan.size,
    });
  });

  document.querySelectorAll('[data-engine]').forEach((button) => {
    button.addEventListener('click', () => {
      if (button.dataset.engine === 'aria2' && state.context?.forcedZip) {
        toast('“打包下载”固定使用浏览器 ZIP，如需 aria2 请用“下载所选”', 'info', 4200);
        return;
      }
      setEngine(button.dataset.engine);
      if (button.dataset.engine === 'aria2' && !state.aria2.available) ensureAria2().catch((error) => toast(error.message, 'error'));
    });
  });
  document.querySelectorAll('[data-split]').forEach((button) => {
    button.addEventListener('click', () => adjustSplit(Number(button.dataset.split)));
  });
  $('startDownloadBtn').addEventListener('click', startDownload);
  $('pickDirHint').addEventListener('click', () => openFolder($('downloadDir').value));

  $('queueList').addEventListener('click', (event) => {
    const button = event.target.closest('[data-task-action]');
    if (!button) return;
    controlTask(button.dataset.gid, button.dataset.taskAction);
  });
  $('clearQueueBtn').addEventListener('click', clearQueue);
  $('clearAllQueueBtn').addEventListener('click', clearAllQueue);
  $('queueOpenDir').addEventListener('click', () => openFolder(state.aria2.dir || state.boot?.directories?.default));
  $('queueEnsureBtn').addEventListener('click', async () => {
    try { await ensureAria2(); await loadQueue(true); } catch (error) { toast(error.message, 'error'); }
  });

  $('startDirectBtn').addEventListener('click', startDirectDownload);
  $('startYunxBtn').addEventListener('click', () => launchYunx(true));
  $('openYunxOnlyBtn').addEventListener('click', () => launchYunx(false));
  $('getYunxBtn').addEventListener('click', openYunxRelease);
  $('saveSettingsBtn').addEventListener('click', saveSettings);
  $('testAria2Btn').addEventListener('click', testAria2);
  $('startAria2Btn').addEventListener('click', startBuiltInAria2);
  $('turboPresetBtn').addEventListener('click', applyTurboPreset);
  $('settingsOpenDir').addEventListener('click', () => openFolder($('settingsDir').value));
  document.querySelectorAll('[data-theme-choice]').forEach((button) => {
    button.addEventListener('click', () => applyTheme(button.dataset.themeChoice));
  });
  $('openDownloadDir').addEventListener('click', () => openFolder(state.aria2.dir || state.boot?.directories?.default));
}

async function loadBootstrap() {
  try {
    const data = await api('/api/bootstrap');
    state.boot = data;
    state.sites = data.sites || [];
    state.session = data.session || null;
    state.activeSiteId = data.activeSite || data.session?.siteId || (data.sites?.[0]?.id || '');
    state.aria2 = data.aria2 || {};
    state.yunx = data.yunx || {};
    state.preferences = data.preferences || {};
    applyTheme(state.preferences.theme || preferredTheme());
    updateHeader();
    updateAria2(state.aria2);
    renderSites();
    renderSiteCards();
    renderDetected(data.detected || []);
    if (state.session) {
      showBrowser();
      const lastPath = localStorage.getItem('old-path:' + state.session.siteId) || '/';
      await loadPath(lastPath || '/');
    } else if (state.sites.length) {
      await autoConnect();
    } else {
      showWelcome();
    }
  } catch (error) {
    showWelcome();
    toast(error.message, 'error', 5200);
  }
}

function init() {
  applyTheme(preferredTheme());
  bindEvents();
  loadBootstrap();
  window.setInterval(() => refreshAria2(true), 6000);
  window.setInterval(() => {
    if (!state.queueOpen && state.aria2.available) loadQueue(true);
  }, 5000);
}

document.addEventListener('DOMContentLoaded', init);