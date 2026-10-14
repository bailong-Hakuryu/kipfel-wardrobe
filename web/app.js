import { Viewer } from './viewer.js';

const TOKEN = document.querySelector('meta[name="token"]').content;
const $ = (s) => document.querySelector(s);

async function api(path, body) {
  const opt = { headers: { 'X-Token': TOKEN } };
  if (body !== undefined) {
    opt.method = 'POST';
    opt.headers['Content-Type'] = 'application/json';
    opt.body = JSON.stringify(body);
  }
  const r = await fetch(path, opt);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).error || msg; } catch { /* 非 JSON */ }
    throw new Error(msg);
  }
  return r;
}
const getJSON = async (p) => (await api(p)).json();
const postJSON = async (p, b = {}) => (await api(p, b)).json();

// ------------------------------------------------------------------ 状态
let catalog = null;
let config = null;     // 当前编辑中的搭配
let state = null;      // 服务端状态
let viewer = null;
let busy = false;
let comparing = false;
let checks = null;

const clone = (o) => JSON.parse(JSON.stringify(o));
const HEX = /^#[0-9a-f]{6}$/;

// 与 wardrobe.py normalize_config 相同的规则。glow: null = 原版；0~100 = 夜光遮罩百分比
function normalizeGlow(c, ver) {
  const clamp = (x) => Math.round(Math.min(Math.max(x, 0), catalog.glow_max));
  if (ver >= 2) return typeof c.glow === 'number' && Number.isFinite(c.glow) ? clamp(c.glow) : null;
  // 旧版（v1）：glow 是 $selfillumtint（0~5，原版 0.2）
  const g = typeof c.glow === 'number' && Number.isFinite(c.glow) ? c.glow : 0.2;
  return Math.abs(g - 0.2) < 1e-6 ? null : clamp(g * 5);
}
function normalize(c) {
  const out = { v: catalog.default.v, visible: { ...catalog.default.visible }, tint: {}, glow: null };
  if (!c || typeof c !== 'object') return out;
  const ver = Number.isInteger(c.v) ? c.v : 1;
  const src = { ...(c.visible || {}) };
  if (ver < 3) {  // 旧版的粗粒度部件拆成现在的细分部件
    for (const [old, ids] of Object.entries(catalog.legacy_splits)) if (src[old] === false) ids.forEach((k) => { src[k] = false; });
    delete src.metal;
  }
  for (const k of Object.keys(out.visible)) if (k in src) out.visible[k] = !!src[k];
  for (const [k, v] of Object.entries(c.tint || {})) {
    const p = catalog.parts.find((x) => x.id === k);
    const col = String(v).toLowerCase();
    if (p?.tint && HEX.test(col) && col !== '#ffffff') out.tint[k] = col;
  }
  out.glow = normalizeGlow(c, ver);
  return out;
}
function key(c) {
  c = normalize(c);
  const vis = Object.keys(c.visible).sort().map((k) => k + (c.visible[k] ? '1' : '0')).join(',');
  const tint = Object.keys(c.tint).sort().map((k) => k + c.tint[k]).join(',');
  return `${vis}|${tint}|${c.glow ?? 'orig'}`;
}
const same = (a, b) => !!a && !!b && key(a) === key(b);

/** 游戏里当前生效的搭配（原版 = 默认） */
function inGameConfig() {
  if (!state) return null;
  if (state.mode === 'custom' && state.applied) return state.applied;
  if (state.mode === 'original') return catalog.default;
  return null;
}
const pendingApply = () => (state?.pending?.action === 'apply' ? state.pending.config : null);

// 与 wardrobe.py hidden_parts 相同：父级隐藏，子级跟着隐藏
function hiddenParts(c) {
  const hid = new Set(catalog.parts.filter((p) => p.hide && c.visible[p.id] === false).map((p) => p.id));
  let more = true;
  while (more) {
    more = false;
    for (const p of catalog.parts) if (p.parent && hid.has(p.parent) && !hid.has(p.id)) { hid.add(p.id); more = true; }
  }
  return hid;
}

// 与 wardrobe.py material_state 对应：预览里按「部件」隐藏小件（'part:id'），再加上原版就隐藏 / 联动显示的材质
function materialState(c) {
  const hidden = new Set(catalog.base_hidden);
  for (const id of hiddenParts(c)) {
    const p = catalog.parts.find((x) => x.id === id);
    hidden.add('part:' + id);
    const wh = p.when_hidden || {};
    (wh.show || []).forEach((m) => hidden.delete(m));
    (wh.hide || []).forEach((m) => hidden.add(m));
  }
  const tints = {};
  for (const [pid, col] of Object.entries(c.tint)) {
    const p = catalog.parts.find((x) => x.id === pid);
    const rgb = [1, 3, 5].map((i) => parseInt(col.slice(i, i + 2), 16) / 255);
    (p?.tint || []).forEach((m) => { tints[m] = rgb; });
  }
  return { hidden, tints };
}

// ------------------------------------------------------------------ 撤销 / 重做
const hist = { stack: [], index: -1 };
function record() {
  if (hist.index >= 0 && same(hist.stack[hist.index], config)) return;
  hist.stack = hist.stack.slice(0, hist.index + 1);
  hist.stack.push(clone(config));
  if (hist.stack.length > 100) hist.stack.shift();
  hist.index = hist.stack.length - 1;
}
function undo() { if (hist.index > 0) { config = clone(hist.stack[--hist.index]); changed({ record: false }); } }
function redo() { if (hist.index < hist.stack.length - 1) { config = clone(hist.stack[++hist.index]); changed({ record: false }); } }
function renderHistory() {
  $('#btnUndo').disabled = hist.index <= 0;
  $('#btnRedo').disabled = hist.index >= hist.stack.length - 1;
}

// ------------------------------------------------------------------ 分享码
// KIPFEL3：细分部件；KIPFEL2：粗粒度部件，夜光 0~100；KIPFEL1：夜光为 0~5 的旧数值。旧码导入时自动换算
const SHARE_PREFIX = 'KIPFEL3:';
function encodeShare(c) {
  c = normalize(c);
  const o = { h: Object.keys(c.visible).filter((k) => !c.visible[k]), t: c.tint, g: c.glow };
  const bytes = new TextEncoder().encode(JSON.stringify(o));
  return SHARE_PREFIX + btoa(String.fromCharCode(...bytes)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}
function decodeShare(s) {
  const m = /KIPFEL([123]):/.exec(s);
  if (!m) throw new Error('这不是 Kipfel 装扮分享码');
  const b64 = s.slice(m.index + m[0].length).trim().split(/\s/)[0].replace(/-/g, '+').replace(/_/g, '/');
  let o;
  try {
    o = JSON.parse(new TextDecoder().decode(Uint8Array.from(atob(b64), (ch) => ch.charCodeAt(0))));
  } catch { throw new Error('分享码不完整或已损坏'); }
  if (!o || typeof o !== 'object') throw new Error('分享码不完整或已损坏');
  const visible = {};   // 只记录被隐藏的 id（旧码里的 plate / rig / metal 交给 normalize 换算）
  for (const k of Array.isArray(o.h) ? o.h : []) if (typeof k === 'string') visible[k] = false;
  return normalize({ v: +m[1], visible, tint: o.t || {}, glow: o.g });
}

// ------------------------------------------------------------------ 面板
function renderPresets() {
  const box = $('#presets');
  box.replaceChildren();
  const all = [...catalog.builtin_presets.map((p) => ({ ...p, builtin: true })), ...catalog.presets];
  for (const p of all) {
    const b = document.createElement('button');
    b.className = 'chip' + (p.builtin ? '' : ' user');
    b.setAttribute('aria-pressed', same(p.config, config) ? 'true' : 'false');
    b.textContent = p.name;
    b.addEventListener('click', () => { config = normalize(clone(p.config)); changed(); });
    if (!p.builtin) {
      const x = document.createElement('span');
      x.className = 'x'; x.textContent = '×'; x.title = '删除这个预设';
      x.addEventListener('click', async (e) => {
        e.stopPropagation();
        const r = await postJSON('/api/presets', { action: 'delete', name: p.name });
        catalog.presets = r.presets;
        renderPresets();
      });
      b.append(x);
    }
    box.append(b);
  }
}

function renderPresetTools(mode) {
  const box = $('#presetSave');
  box.replaceChildren();
  const link = (text, fn) => {
    const b = document.createElement('button');
    b.className = 'link'; b.textContent = text;
    b.addEventListener('click', fn);
    return b;
  };
  const sep = () => Object.assign(document.createElement('span'), { className: 'sep', textContent: '·' });
  if (!mode) {
    box.append(link('＋ 存为预设', () => renderPresetTools('save')), sep(),
      link('复制分享码', copyShare), sep(), link('导入分享码', () => renderPresetTools('import')));
    return;
  }
  const inp = document.createElement('input');
  inp.className = 'input';
  inp.maxLength = mode === 'save' ? 24 : 2000;
  inp.placeholder = mode === 'save' ? '给这套搭配起个名字' : '粘贴 KIPFEL1: 开头的分享码';
  const ok = document.createElement('button'); ok.className = 'mini'; ok.textContent = mode === 'save' ? '保存' : '导入';
  const cancel = document.createElement('button'); cancel.className = 'mini'; cancel.textContent = mode === 'show' ? '完成' : '取消';
  const done = () => renderPresetTools();
  if (mode === 'show') {
    cancel.addEventListener('click', done);
    inp.addEventListener('keydown', (e) => { if (e.key === 'Escape' || e.key === 'Enter') done(); });
    box.append(inp, cancel);
    return;
  }
  const submit = async () => {
    const v = inp.value.trim();
    if (!v) { inp.focus(); return; }
    try {
      if (mode === 'save') {
        if (catalog.builtin_presets.some((p) => p.name === v)) throw new Error('不能和内置预设重名');
        const r = await postJSON('/api/presets', { action: 'save', name: v, config });
        catalog.presets = r.presets;
        renderPresets();
        toast(`已保存预设「${v}」`);
      } else {
        config = decodeShare(v);
        changed();
        toast('已导入分享码（还没应用到游戏）');
      }
      done();
    } catch (e) { toast(e.message, true); }
  };
  ok.addEventListener('click', submit);
  cancel.addEventListener('click', done);
  inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') submit(); if (e.key === 'Escape') done(); });
  box.append(inp, ok, cancel);
  inp.focus();
}

async function copyShare() {
  const code = encodeShare(config);
  let ok = false;
  try {
    await navigator.clipboard.writeText(code);
    ok = true;
  } catch {
    const ta = Object.assign(document.createElement('textarea'), { value: code });
    ta.style.cssText = 'position:fixed;opacity:0';
    document.body.append(ta);
    ta.select();
    try { ok = document.execCommand('copy'); } catch { ok = false; }
    ta.remove();
  }
  if (ok) {
    toast('分享码已复制，发给朋友后在「导入分享码」里粘贴即可');
    return;
  }
  // 剪贴板不可用：把分享码放进输入框，让用户自己 Ctrl+C
  renderPresetTools('show');
  const inp = $('#presetSave .input');
  inp.value = code;
  inp.readOnly = true;
  inp.select();
  toast('无法自动复制，请按 Ctrl+C 复制输入框里的分享码');
}

function renderGroups() {
  const root = $('#groups');
  root.replaceChildren();
  for (const g of catalog.groups) {
    const sec = document.createElement('section');
    sec.className = 'block';
    sec.innerHTML = '<div class="block-head"><h2></h2><span class="block-note"></span></div><div class="rows"></div>';
    sec.querySelector('h2').textContent = g.name;
    sec.querySelector('.block-note').textContent = g.note || '';
    const rows = sec.querySelector('.rows');
    for (const p of catalog.parts.filter((x) => x.group === g.id)) rows.append(partRow(p));
    root.append(sec);
  }
}

function partRow(p) {
  const row = document.createElement('div');
  row.className = 'row' + (p.parent ? ' child' : '');
  row.dataset.part = p.id;
  row.innerHTML = '<div class="row-text"><div class="row-name"></div><div class="row-desc"></div></div><div class="row-ctrl"></div>';
  row.querySelector('.row-name').textContent = p.name;
  row.querySelector('.row-desc').textContent = p.desc || '';
  row.querySelector('.row-desc').title = p.desc || '';
  const ctrl = row.querySelector('.row-ctrl');

  if (p.tint) {
    const reset = document.createElement('button');
    reset.className = 'swatch-reset'; reset.textContent = '原色'; reset.title = '去掉染色';
    reset.addEventListener('click', () => { delete config.tint[p.id]; changed(); });
    const sw = document.createElement('label');
    sw.className = 'swatch'; sw.title = '染色（与原贴图颜色相乘，深色部位变化较小）';
    const inp = document.createElement('input');
    inp.type = 'color';
    inp.setAttribute('aria-label', p.name + ' 染色');
    inp.addEventListener('input', () => { config.tint[p.id] = inp.value; changed({ light: true }); });
    inp.addEventListener('change', () => changed());
    sw.append(inp);
    ctrl.append(reset, sw);
    row._tint = { sw, inp, reset };
  }
  if (p.hide) {
    const lab = document.createElement('label');
    lab.className = 'switch';
    lab.innerHTML = '<input type="checkbox"><span></span>';
    const cb = lab.querySelector('input');
    cb.setAttribute('aria-label', '显示' + p.name);
    cb.addEventListener('change', () => { config.visible[p.id] = cb.checked; changed(); });
    ctrl.append(lab);
    row._cb = cb;
  }
  // 开关行高亮这件装饰自己的小件；只能染色的行高亮整个材质（染色作用于整个材质）
  const hl = (on) => viewer?.update({ highlight: new Set(on ? (p.hide ? ['part:' + p.id] : p.tint || []) : []) });
  row.addEventListener('mouseenter', () => hl(true));
  row.addEventListener('mouseleave', () => hl(false));
  return row;
}

function syncControls() {
  const hid = hiddenParts(config);
  for (const row of document.querySelectorAll('.row')) {
    const id = row.dataset.part;
    if (row._cb) {
      // 父级（例如帽子）被隐藏时，子级（帽子上的蝴蝶结）跟着隐藏，开关锁定
      const p = catalog.parts.find((x) => x.id === id);
      const byParent = !!(p?.parent && hid.has(p.parent));
      row._cb.checked = config.visible[id] !== false && !byParent;
      row._cb.disabled = byParent;
      row.classList.toggle('off', !row._cb.checked);
      row.classList.toggle('locked', byParent);
      row.title = byParent ? `跟随「${catalog.parts.find((x) => x.id === p.parent).name}」一起隐藏` : '';
    }
    if (row._tint) {
      const c = config.tint[id];
      row._tint.sw.classList.toggle('set', !!c);
      row._tint.sw.style.setProperty('--c', c || 'transparent');
      row._tint.inp.value = c || '#ffffff';
      row._tint.reset.style.visibility = c ? 'visible' : 'hidden';
    }
  }
  renderGlow();
  renderPresets();
}

function renderGlow() {
  $('#glow').value = config.glow ?? 1;   // 原版实际约等于 1%
  $('#glowVal').textContent = config.glow == null ? '原版' : config.glow + '%';
  $('#glowReset').disabled = config.glow == null;
}

function syncViewer() {
  if (!viewer) return;
  const c = comparing ? (inGameConfig() || catalog.default) : config;
  const { hidden, tints } = materialState(c);
  viewer.update({ hidden, tints, glow: c.glow });
}

let draftTimer = 0;
function changed(opts = {}) {
  if (!opts.light && opts.record !== false) record();
  syncViewer();
  if (!opts.light) syncControls();
  renderHistory();
  renderStatus();
  clearTimeout(draftTimer);
  draftTimer = setTimeout(() => postJSON('/api/draft', { config }).catch(() => {}), 400);
}

// ------------------------------------------------------------------ 状态显示
function fmtTime(ts) {
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getMonth() + 1}月${d.getDate()}日 ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function renderStatus() {
  if (!state || !catalog) return;
  const live = inGameConfig();
  const queued = pendingApply();
  const dirty = !live || !same(config, live);
  const pill = $('#pill');
  let tone, text;
  if (!state.workshop_exists && !state.local_exists) { tone = 'danger'; text = '找不到 mod 文件'; }
  else if (state.game_running) { tone = 'warn'; text = state.pending ? '游戏运行中 · 已排队' : '游戏运行中'; }
  else if (state.mode === 'custom') { tone = 'accent'; text = '自定义版生效中'; }
  else if (state.mode === 'original') { tone = 'ok'; text = '原版生效中'; }
  else if (state.mode === 'both') { tone = 'warn'; text = '原版与自定义同时启用'; }
  else { tone = 'muted'; text = 'mod 未启用'; }
  pill.dataset.tone = tone;
  pill.querySelector('span').textContent = text;

  // 底部状态行
  const st = $('#actionStatus');
  st.replaceChildren();
  const span = (cls, t) => Object.assign(document.createElement('span'), { className: cls, textContent: t });
  $('#autoSync').checked = state.auto_sync !== false;
  if (state.pending) {
    const sync = state.pending.reason === 'sync';
    st.append(span('dirty', state.pending.action !== 'apply' ? '● 已排队：退出游戏后自动恢复原版'
      : sync ? '● 作者更新了 mod：退出游戏后自动同步你的搭配'
        : '● 已排队：退出游戏后自动应用' + (same(queued, config) ? '' : '（排队的是之前的搭配）')), ' ');
    const c = document.createElement('button');
    c.className = 'link'; c.textContent = '取消排队';
    c.addEventListener('click', cancelPending);
    st.append(c);
  } else if (state.game_running) {
    st.append(span('warn', dirty ? '游戏运行中 —— 点「退出游戏后应用」先排队' : '游戏运行中'));
  } else if (dirty) {
    st.append(span('dirty', '● 有未应用的更改'));
  } else {
    st.textContent = state.mode === 'custom' && state.applied_at
      ? `游戏内已是这套搭配 · ${fmtTime(state.applied_at)} 应用` : '游戏内已是这套搭配';
  }

  const apply = $('#btnApply'), restore = $('#btnRestore');
  apply.textContent = state.game_running ? '退出游戏后应用' : '应用到游戏';
  restore.textContent = state.game_running ? '退出游戏后恢复' : '恢复原版';
  const needApply = dirty || state.mode === 'both' || state.outdated;
  apply.disabled = busy || !state.workshop_exists
    || (state.game_running ? same(config, queued || live) : !needApply);
  restore.disabled = busy || (state.mode === 'original' && !state.local_exists && !queued)
    || state.pending?.action === 'restore';
  $('#btnLaunch').disabled = state.game_running;
  $('#btnCompare').disabled = !live || same(config, live);

  // 顶部提示：只放需要处理的
  renderAlerts();
}

// 顶部提示：只放需要处理的。内容没变就不重绘（避免把用户正在输入的路径冲掉）
let alertsKey = '';
function renderAlerts() {
  const alerts = [];
  if (!state.game_found) {
    alerts.push({ tone: 'danger', title: '没找到 Left 4 Dead 2', body: '自动查找 Steam 库文件夹没有找到游戏，请手动选择游戏的安装文件夹（里面有 left4dead2 文件夹）。',
      actions: [{ label: '选择文件夹', fn: pickGameDir }], input: true });
  } else if (!state.workshop_exists) {
    alerts.push({ tone: 'danger', title: '还没有订阅 Kipfel mod', body: '本工具只修改你电脑上已订阅的原 mod。请先在创意工坊订阅，等 Steam 下载完成后回到这里。',
      actions: [{ label: '打开创意工坊', fn: () => postJSON('/api/open_workshop').catch((e) => toast(e.message, true)) }] });
  }
  if (state.outdated) {
    alerts.push({ tone: 'info', title: '创意工坊版本已更新', body: '你的自定义版还是基于旧文件生成的，点「应用到游戏」即可同步。' });
  }
  const bad = (checks?.checks || []).filter((c) => c.level === 'warn' || c.level === 'error');
  if (bad.length) {
    alerts.push({ tone: 'warn', title: `安全检查有 ${bad.length} 项需要留意`, body: bad.map((c) => c.title).join('；'),
      actions: [{ label: '查看', fn: () => $('#safety').scrollIntoView({ behavior: 'smooth' }) }] });
  }
  const k = JSON.stringify(alerts.map((a) => [a.tone, a.title, a.body]));
  if (k === alertsKey) return;
  alertsKey = k;
  $('#alerts').replaceChildren(...alerts.map((a) => {
    const d = document.createElement('div');
    d.className = 'alert ' + a.tone;
    const t = document.createElement('div');
    const b = document.createElement('b'); b.textContent = a.title;
    const s = document.createElement('small'); s.textContent = a.body;
    t.append(b, s);
    if (a.input) {  // 也可以直接粘贴路径
      const row = document.createElement('div');
      row.className = 'alert-input';
      const inp = Object.assign(document.createElement('input'), { className: 'input', placeholder: '或粘贴路径，例如 D:\\Steam\\steamapps\\common\\Left 4 Dead 2' });
      const ok = Object.assign(document.createElement('button'), { className: 'mini', textContent: '使用这个路径' });
      const go = () => inp.value.trim() && setGameDir(inp.value.trim());
      ok.addEventListener('click', go);
      inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') go(); });
      row.append(inp, ok);
      t.append(row);
    }
    d.append(t);
    for (const act of a.actions || []) {
      const g = Object.assign(document.createElement('button'), { className: 'mini go', textContent: act.label });
      g.addEventListener('click', act.fn);
      d.append(g);
    }
    return d;
  }));
}

async function pickGameDir() {
  try {
    const r = await postJSON('/api/pick_game_dir');
    if (r.path) await setGameDir(r.path);
  } catch (e) { toast(e.message, true); }
}

async function setGameDir(path) {
  try {
    await postJSON('/api/game_dir', { path });
    toast('已找到游戏，正在重新加载…');
    setTimeout(() => location.reload(), 600);
  } catch (e) { toast(e.message, true); }
}

function renderChecks() {
  const ul = $('#checks');
  ul.replaceChildren(...(checks?.checks || []).map((c) => {
    const li = document.createElement('li');
    li.className = 'check ' + c.level;
    const t = document.createElement('span'); t.textContent = c.title;
    li.append(t);
    if (c.detail) { const s = document.createElement('small'); s.textContent = c.detail; li.append(s); }
    return li;
  }));
}

async function loadChecks() {
  try {
    checks = await getJSON('/api/check');
    renderChecks();
    renderStatus();
  } catch { /* 下次再试 */ }
}

let failures = 0;
let checkKey = '';
let refreshing = null;
async function refreshState() {
  if (refreshing) return refreshing;
  refreshing = (async () => {
    try {
      state = await getJSON('/api/state');
      failures = 0;
      if (state.pending_result) {
        const r = state.pending_result;
        const what = r.action === 'apply' ? '应用' : '恢复原版';
        if (r.reason === 'sync') {
          toast(r.ok ? '作者更新了 mod，已按你上次应用的搭配自动同步' : `自动同步失败：${r.error}（可以手动点「应用到游戏」）`, !r.ok);
        } else {
          toast(r.ok ? `游戏已退出，排队的${what}已自动完成` : `排队的${what}失败：${r.error}`, !r.ok);
        }
        postJSON('/api/pending/ack').catch(() => {});
      }
      // 关键状态变化时重新跑安全检查
      const k = JSON.stringify([state.mode, state.applied_at, state.local_exists, state.outdated, state.scanning,
        state.overlaps.map((o) => o.file)]);
      if (k !== checkKey) { checkKey = k; loadChecks(); }
      renderStatus();
      if (comparing) syncViewer();
    } catch (e) {
      if (e.message === 'bad token') { location.reload(); return; }  // 工具重启过，令牌已更换
      if (++failures >= 2) {
        const pill = $('#pill');
        pill.dataset.tone = 'danger';
        pill.querySelector('span').textContent = '工具服务已断开，请重新打开';
        $('#btnApply').disabled = $('#btnRestore').disabled = true;
      }
    }
  })().finally(() => { refreshing = null; });
  return refreshing;
}

let pollTimer = 0;
function schedulePoll() {
  clearTimeout(pollTimer);
  // 窗口在后台时降低轮询频率
  pollTimer = setTimeout(async () => { await refreshState(); schedulePoll(); }, document.hidden ? 15000 : 3000);
}

// ------------------------------------------------------------------ 提示 / 对话框
let toastTimer = 0;
function toast(msg, error = false) {
  const t = $('#toast');
  t.textContent = msg;
  t.classList.toggle('error', error);
  t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), error ? 6000 : 3600);
}

function confirmDialog(title, text, okText) {
  const dlg = $('#dlg');
  $('#dlgTitle').textContent = title;
  $('#dlgText').textContent = text;
  $('#dlgOk').textContent = okText;
  dlg.returnValue = '';
  dlg.showModal();
  return new Promise((res) => dlg.addEventListener('close', () => res(dlg.returnValue === 'ok'), { once: true }));
}

// ------------------------------------------------------------------ 动作
async function doApply() {
  if ($('#btnApply').disabled) return;
  busy = true;
  const btn = $('#btnApply');
  btn.classList.add('busy');
  renderStatus();
  try {
    state = await postJSON('/api/apply', { config });
    toast(state.pending
      ? '已排队 —— 退出游戏后自动应用（关掉本窗口也会照常执行）'
      : '已应用 —— 下次进入游戏就是这套搭配');
    loadChecks();
  } catch (e) {
    toast(e.message, true);
    await refreshState();
  } finally {
    busy = false;
    btn.classList.remove('busy');
    renderStatus();
  }
}

async function doRestore() {
  const running = state?.game_running;
  const ok = await confirmDialog(running ? '退出游戏后恢复原版？' : '恢复原版？',
    '会删除本工具生成的 kipfel_custom.vpk，并重新启用创意工坊原版。你的搭配和预设都会保留，之后随时可以再应用。'
    + (running ? '游戏开着，所以会在你退出游戏后自动执行。' : ''), '恢复原版');
  if (!ok) return;
  busy = true;
  renderStatus();
  try {
    state = await postJSON('/api/restore');
    toast(state.pending ? '已排队 —— 退出游戏后自动恢复原版' : '已恢复原版');
    loadChecks();
  } catch (e) {
    toast(e.message, true);
    await refreshState();
  } finally {
    busy = false;
    renderStatus();
  }
}

async function runSelftest() {
  const btn = $('#btnSelftest');
  btn.classList.add('busy');
  btn.textContent = '自检中…';
  try {
    const r = await postJSON('/api/selftest');
    const passed = r.results.filter((x) => x.ok).length;
    $('#testTitle').textContent = r.ok ? `完整自检通过（${passed}/${r.results.length}）` : `自检有 ${r.results.length - passed} 项失败`;
    $('#testSub').textContent = `用时 ${r.seconds} 秒。全部在临时文件夹里进行，真实的游戏文件只读、不改。`;
    $('#testList').replaceChildren(...r.results.map((x) => {
      const li = document.createElement('li');
      li.className = 'check ' + (x.ok ? 'ok' : 'error');
      const t = document.createElement('span'); t.textContent = x.name;
      const s = document.createElement('small'); s.textContent = x.detail;
      li.append(t, s);
      return li;
    }));
    $('#dlgTest').showModal();
  } catch (e) {
    toast('自检没能运行：' + e.message, true);
  } finally {
    btn.classList.remove('busy');
    btn.textContent = '完整自检';
  }
}

async function cancelPending() {
  try {
    state = await postJSON('/api/pending/cancel');
    toast('已取消排队');
    renderStatus();
  } catch (e) { toast(e.message, true); }
}

// ------------------------------------------------------------------ 3D 预览加载
async function loadViewer() {
  const bar = $('#loadingBar'), txt = $('#loadingText'), box = $('#loading');
  const v = encodeURIComponent(catalog.source_version || '');
  try {
    viewer = new Viewer($('#view'));
    if (new URLSearchParams(location.search).has('debug')) window.kipfelViewer = viewer;  // 核对小件用
    const buf = await (await api('/api/model?v=' + v)).arrayBuffer();
    const texNames = viewer.loadModel(buf);
    syncViewer();
    let done = 0, direct = 0;
    bar.style.width = '10%';
    await Promise.all(texNames.map(async (name) => {
      const r = await api(`/api/tex?name=${encodeURIComponent(name)}&v=${v}`);
      const raw = new Uint8Array(await r.arrayBuffer());
      const sizes = (r.headers.get('X-Mips') || String(raw.length)).split(',').map(Number);
      const levels = [];
      let off = 0;
      for (const s of sizes) { levels.push(raw.subarray(off, off + s)); off += s; }
      if (viewer.addTexture(name, r.headers.get('X-Fmt'), +r.headers.get('X-Width'), +r.headers.get('X-Height'), levels)) direct++;
      done++;
      bar.style.width = `${10 + (90 * done) / texNames.length}%`;
      txt.textContent = `正在读取贴图 ${done}/${texNames.length}`;
    }));
    console.info(`[kipfel] 贴图 ${direct}/${texNames.length} 张由显卡直接解压`);
    box.classList.add('done');
  } catch (e) {
    box.classList.add('error');
    txt.textContent = '3D 预览加载失败：' + e.message + '（不影响换装功能）';
  }
}

// ------------------------------------------------------------------ 启动
async function main() {
  [catalog, state] = await Promise.all([getJSON('/api/catalog'), getJSON('/api/state')]);
  config = normalize(catalog.draft || inGameConfig() || catalog.default);
  record();
  $('#appVersion').textContent = catalog.app_version ? '· v' + catalog.app_version : '';
  renderGroups();
  renderPresetTools();
  syncControls();
  renderHistory();
  renderStatus();
  if (state.workshop_exists) loadViewer().then(syncViewer);
  else $('#loadingText').textContent = '找到游戏并订阅 Kipfel mod 后，这里会显示 3D 预览';
  loadChecks();
  checkKey = '';

  $('#glow').max = catalog.glow_max;
  $('#glow').addEventListener('input', (e) => {
    config.glow = Math.round(+e.target.value);
    renderGlow();
    changed({ light: true });
  });
  $('#glow').addEventListener('change', () => changed());
  $('#glowReset').addEventListener('click', () => { config.glow = null; changed(); });
  $('#autoSync').addEventListener('change', async (e) => {
    try {
      state = await postJSON('/api/settings', { auto_sync: e.target.checked });
      toast(e.target.checked ? '已开启：作者更新后自动同步你的搭配' : '已关闭自动同步，作者更新后只会提示');
      loadChecks();
      renderStatus();
    } catch (err) { toast(err.message, true); }
  });
  $('#btnSelftest').addEventListener('click', runSelftest);
  $('#testClose').addEventListener('click', () => $('#dlgTest').close());
  $('#btnUndo').addEventListener('click', undo);
  $('#btnRedo').addEventListener('click', redo);
  $('#btnApply').addEventListener('click', doApply);
  $('#btnRestore').addEventListener('click', doRestore);
  $('#btnRecheck').addEventListener('click', async () => {
    $('#checks').innerHTML = '<li class="check info"><span>正在检查…</span></li>';
    await postJSON('/api/rescan').catch(() => {});
    setTimeout(loadChecks, 400);
  });
  $('#btnShortcut').addEventListener('click', async () => {
    try {
      const r = await postJSON('/api/shortcut');
      toast('已在桌面创建快捷方式：' + r.path.split('\\').pop());
    } catch (e) { toast('创建失败：' + e.message, true); }
  });
  $('#btnLaunch').addEventListener('click', async () => {
    const live = inGameConfig();
    try {
      await postJSON('/api/launch');
      toast(live && !same(config, live) ? '正在启动游戏 —— 注意：还有未应用的更改' : '正在启动游戏…');
    } catch (e) { toast(e.message, true); }
  });

  // 按住对比：临时显示游戏里现在的样子
  const cmp = $('#btnCompare');
  const setCompare = (on) => {
    if (comparing === on) return;
    comparing = on;
    cmp.setAttribute('aria-pressed', String(on));
    syncViewer();
  };
  cmp.addEventListener('pointerdown', (e) => {
    if (cmp.disabled) return;
    try { cmp.setPointerCapture(e.pointerId); } catch { /* 部分输入设备不支持捕获，松开时仍会触发 pointerup */ }
    setCompare(true);
  });
  for (const ev of ['pointerup', 'pointercancel', 'lostpointercapture']) cmp.addEventListener(ev, () => setCompare(false));

  for (const b of document.querySelectorAll('.seg button')) {
    b.addEventListener('click', () => {
      for (const o of document.querySelectorAll('.seg button')) o.setAttribute('aria-selected', o === b ? 'true' : 'false');
      viewer?.setView(b.dataset.view);
    });
  }
  $('#btnFront').addEventListener('click', () => viewer?.resetCamera(0));
  $('#btnBack').addEventListener('click', () => viewer?.resetCamera(Math.PI));
  const toggle = (btn, prop) => btn.addEventListener('click', () => {
    if (!viewer) return;
    viewer.update({ [prop]: !viewer[prop] });
    btn.setAttribute('aria-pressed', String(viewer[prop]));
  });
  toggle($('#btnSpin'), 'spin');
  toggle($('#btnNight'), 'night');

  document.addEventListener('keydown', (e) => {
    if (e.target.classList?.contains('input') || $('#dlg').open || $('#dlgTest').open) return;
    const k = e.key.toLowerCase(), mod = e.ctrlKey || e.metaKey;
    if (!mod) return;
    if (k === 'z' && !e.shiftKey) { e.preventDefault(); undo(); }
    else if (k === 'y' || (k === 'z' && e.shiftKey)) { e.preventDefault(); redo(); }
    else if (k === 's') { e.preventDefault(); doApply(); }
  });

  schedulePoll();
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshState(); schedulePoll(); });
  const ping = () => api('/api/ping', {}).catch(() => {});
  ping();
  setInterval(ping, 5000);
  window.addEventListener('pagehide', () => navigator.sendBeacon('/api/bye?token=' + TOKEN));
}

main().catch((e) => {
  document.body.innerHTML = `<div style="padding:40px;color:#e27b69;font:14px 'Microsoft YaHei UI'">启动失败：${String(e.message || e).replace(/</g, '&lt;')}</div>`;
});
