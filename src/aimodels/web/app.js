/* Model Console - control surface + analysis views. Vanilla JS, no dependencies.
   Model names come from public APIs, so every string that reaches innerHTML goes through esc(). */
(() => {
  'use strict';
  const { esc, isNum, fmt } = Charts;
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const TOKEN = $('meta[name="dashboard-token"]').content;
  const clamp = (v, a = 0, b = 1) => Math.min(b, Math.max(a, v));
  const store = {
    get(k) { try { return JSON.parse(localStorage.getItem(k)); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* private window: fine */ } },
  };

  async function api(path, body) {
    const opt = body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Dashboard-Token': TOKEN }, body: JSON.stringify(body) };
    const r = await fetch(path, opt);
    let data = null;
    try { data = await r.json(); } catch { /* not json */ }
    if (!r.ok) throw Object.assign(new Error((data && data.error) || r.statusText), { status: r.status });
    return data;
  }

  /* ------------------------------------------------------------------ state */
  const SCORE_OPTS = [['intelligence_index', 'Intelligence index'], ['coding_index', 'Coding index'], ['math_index', 'Math index'], ['mmlu_pro', 'MMLU-Pro'], ['gpqa', 'GPQA'], ['hle', 'HLE (Humanity\'s Last Exam)'], ['livecodebench', 'LiveCodeBench'], ['scicode', 'SciCode'], ['math_500', 'MATH-500'], ['aime', 'AIME']];
  const SCORE_LABEL = Object.fromEntries(SCORE_OPTS);
  const SHORT = { intelligence_index: 'Intelligence', coding_index: 'Coding idx', math_index: 'Math idx', mmlu_pro: 'MMLU-Pro', gpqa: 'GPQA', hle: 'HLE', livecodebench: 'LiveCodeBench', scicode: 'SciCode', math_500: 'MATH-500', aime: 'AIME' };
  const STEPS = [['extract', 'Extract'], ['transform', 'Transform'], ['validate', 'Validate'], ['load', 'Load'], ['eda', 'Report']];
  const PIN_COLORS = ['#2a78d6', '#eb6834', '#1baf7a', '#6250d6', '#e87ba4'];
  const MAX_PINS = 5;

  const DEFAULTS = () => ({
    fuzzy: 0.65, tol: 0.5, useCache: false, skipHf: false, withEda: true,
    wq: 0.8, wc: 0.6, ws: 0.3, wz: 0.2, mode: 'weighted', rankerOn: true, scoreKey: 'intelligence_index', missing: 'neutral', topN: 10,
    sizeMin: 0, sizeMax: 1, sizeLink: false, minScore: 0, maxPrice: 1, since: 0, filtersOn: true,
    openness: 'all', creators: [], search: '', complete: false, noReasoning: false, keepUnknownSize: false,
    adv: { left: false, mid: false, right: false },
  });
  let C = Object.assign(DEFAULTS(), store.get('mc.ctl') || {});
  C.adv = Object.assign({ left: false, mid: false, right: false }, C.adv);

  const S = {
    dataset: store.get('mc.dataset') || 'sample', models: [], run: null, status: null, quality: null, report: null,
    tab: store.get('mc.tab') || 'explore', pins: store.get('mc.pins') || [],
    explore: { x: 'size', y: 'score', color: 'group', xLog: true, yLog: false, frontier: true, top: true },
    lb: { sort: null, dir: -1, shown: 50 }, job: null, log: [], logNext: 0, timer: null, sql: { query: '', result: null, error: '' },
  };
  const EXT = { size: [1, 1], price: [1, 1], score: [0, 100], t: [0, 1] };
  let D = { filtered: [], ranked: [] };
  let PCT = {};

  /* ---------------------------------------------------------------- helpers */
  const logMap = (v, [lo, hi]) => (hi > lo ? lo * Math.pow(hi / lo, v) : lo);
  const linMap = (v, [lo, hi]) => lo + (hi - lo) * v;
  const ago = (iso) => {
    if (!iso) return '';
    const s = (Date.now() - Date.parse(iso)) / 1000;
    return s < 90 ? 'just now' : s < 5400 ? Math.round(s / 60) + ' min ago' : s < 129600 ? Math.round(s / 3600) + ' h ago' : Math.round(s / 86400) + ' days ago';
  };
  function toast(msg, err) {
    const el = document.createElement('div');
    el.className = 'toast' + (err ? ' err' : '');
    el.textContent = msg;
    $('#toast').appendChild(el);
    setTimeout(() => el.remove(), err ? 6500 : 3200);
  }
  function csvText(rows) {
    return rows.map((r) => r.map((v) => { if (v == null) return ''; const s = String(v); return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s; }).join(',')).join('\n');
  }
  function download(name, text) {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([text], { type: 'text/csv' }));
    a.download = name; document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1500);
  }
  let saveT = 0;
  const saveCtl = () => { clearTimeout(saveT); saveT = setTimeout(() => store.set('mc.ctl', C), 250); };
  let raf = 0;
  function schedule() { if (raf) return; raf = requestAnimationFrame(() => { raf = 0; recompute(); saveCtl(); }); }
  const paints = [];
  const syncControls = () => paints.forEach((f) => f());

  /* ------------------------------------------------------------- knob widget */
  const KDEF = {
    fuzzy: { label: 'Match', aria: 'Name-match strictness', big: true, def: 0.65, get: () => C.fuzzy, set: (v) => { C.fuzzy = v; }, text: (v) => `strictness ${(0.8 + 0.2 * v).toFixed(2)}`, tip: 'How alike two model names must be to be joined across sources. Higher = fewer, safer joins.' },
    tol: { label: 'Tolerance', aria: 'Reject tolerance', small: true, def: 0.5, get: () => C.tol, set: (v) => { C.tol = v; }, text: (v) => `abort if > ${Math.round(v * 50)}% rejected`, tip: 'The run aborts if more than this share of rows fails validation.' },
    sizeMin: { label: 'Min size', aria: 'Minimum parameters', def: 0, get: () => C.sizeMin, text: (v) => (v < 0.005 ? 'any' : '≥ ' + fmt.params(logMap(v, EXT.size))), set: (v) => linkSize('sizeMin', v) },
    sizeMax: { label: 'Max size', aria: 'Maximum parameters', def: 1, get: () => C.sizeMax, text: (v) => (v > 0.995 ? 'any' : '≤ ' + fmt.params(logMap(v, EXT.size))), set: (v) => linkSize('sizeMax', v) },
    minScore: { label: 'Min quality', aria: 'Minimum quality score', def: 0, get: () => C.minScore, text: (v) => (v < 0.005 ? 'any' : '≥ ' + fmt.num(linMap(v, EXT.score), 0)), set: (v) => { C.minScore = v; } },
    maxPrice: { label: 'Max price', aria: 'Maximum price per million tokens', def: 1, get: () => C.maxPrice, text: (v) => (v > 0.995 ? 'no limit' : '≤ ' + fmt.price(logMap(v, EXT.price))), set: (v) => { C.maxPrice = v; } },
    since: { label: 'Released since', aria: 'Earliest release date', big: false, def: 0, get: () => C.since, text: (v) => (v < 0.005 ? 'any date' : fmt.date(linMap(v, EXT.t)).slice(0, 7)), set: (v) => { C.since = v; } },
  };
  const knobs = {};
  function linkSize(which, v) {
    const other = which === 'sizeMin' ? 'sizeMax' : 'sizeMin', delta = v - C[which];
    C[which] = v;
    if (C.sizeLink) { C[other] = clamp(C[other] + delta); knobs[other].paint(); }
  }
  function ticksSvg() {
    let s = '';
    for (let i = 0; i < 55; i++) {
      const a = ((-135 + (270 * i) / 54) * Math.PI) / 180, c = Math.sin(a), d = -Math.cos(a), long = i % 9 === 0;
      s += `<line x1="${(c * (long ? 56 : 58)).toFixed(2)}" y1="${(d * (long ? 56 : 58)).toFixed(2)}" x2="${(c * 62).toFixed(2)}" y2="${(d * 62).toFixed(2)}"/>`;
    }
    return s;
  }
  function makeKnob(host, id) {
    const d = KDEF[id];
    host.classList.add('knob-wrap');
    if (d.big) host.classList.add('big');
    if (d.small) host.classList.add('small');
    host.innerHTML = `<div class="knob" role="slider" tabindex="0" aria-label="${esc(d.aria)}" aria-valuemin="0" aria-valuemax="100" ${d.tip ? `title="${esc(d.tip)}"` : ''}><svg class="ticks" viewBox="-66 -66 132 132" aria-hidden="true">${ticksSvg()}</svg><div class="cap"><i></i></div></div><div class="klabel">${esc(d.label)}</div><div class="kval"></div>`;
    const el = $('.knob', host), val = $('.kval', host), tk = $$('.ticks line', host);
    const api_ = {
      paint() {
        const v = clamp(d.get());
        el.style.setProperty('--rot', (-135 + 270 * v) + 'deg');
        tk.forEach((t, i) => t.classList.toggle('on', i / (tk.length - 1) <= v + 1e-6));
        const txt = d.text(v);
        val.textContent = txt;
        el.setAttribute('aria-valuenow', Math.round(v * 100));
        el.setAttribute('aria-valuetext', txt);
      },
    };
    const user = (nv) => { nv = clamp(nv); if (nv === d.get()) return; d.set(nv); api_.paint(); schedule(); };
    let drag = null;
    el.addEventListener('pointerdown', (e) => { el.setPointerCapture(e.pointerId); drag = { x: e.clientX, y: e.clientY, v: d.get() }; el.classList.add('drag'); el.focus(); e.preventDefault(); });
    el.addEventListener('pointermove', (e) => { if (drag) user(drag.v + ((drag.y - e.clientY) + (e.clientX - drag.x)) / (e.shiftKey ? 700 : 220)); });
    const end = () => { drag = null; el.classList.remove('drag'); };
    el.addEventListener('pointerup', end); el.addEventListener('pointercancel', end);
    el.addEventListener('wheel', (e) => { e.preventDefault(); user(d.get() - Math.sign(e.deltaY) * (e.shiftKey ? 0.004 : 0.02)); }, { passive: false });
    el.addEventListener('dblclick', () => user(d.def));
    el.addEventListener('keydown', (e) => {
      const st = e.shiftKey ? 0.1 : 0.01, v = d.get(), map = { ArrowUp: v + st, ArrowRight: v + st, ArrowDown: v - st, ArrowLeft: v - st, PageUp: v + 0.1, PageDown: v - 0.1, Home: 0, End: 1 };
      if (e.key in map) { e.preventDefault(); user(map[e.key]); }
    });
    knobs[id] = api_; paints.push(api_.paint); api_.paint();
  }

  /* ------------------------------------------------------------ fader widget */
  const FDEF = {
    wq: { label: 'Quality', key: 'wq', tip: 'Weight of the benchmark score' },
    wc: { label: 'Cost', key: 'wc', tip: 'Weight of being cheap' },
    ws: { label: 'Speed', key: 'ws', tip: 'Weight of output tokens per second' },
    wz: { label: 'Size', key: 'wz', tip: 'Weight of being small (fewer parameters)' },
  };
  function makeFader(host, id) {
    const d = FDEF[id];
    host.innerHTML = `<div class="fader" role="slider" tabindex="0" aria-orientation="vertical" aria-label="${esc(d.label)} weight" aria-valuemin="0" aria-valuemax="100" title="${esc(d.tip)}"><span class="fill"></span><span class="handle"></span></div><div class="klabel">${esc(d.label)}</div><div class="kval"></div>`;
    const el = $('.fader', host), val = $('.kval', host);
    const paint = () => { const v = C[d.key]; el.style.setProperty('--v', v); val.textContent = Math.round(v * 100); el.setAttribute('aria-valuenow', Math.round(v * 100)); };
    const set = (v) => { v = clamp(v); if (v === C[d.key]) return; C[d.key] = v; paint(); schedule(); };
    const fromPointer = (e) => { const r = el.getBoundingClientRect(); set(1 - (e.clientY - r.top - 8) / (r.height - 16)); };
    let on = false;
    el.addEventListener('pointerdown', (e) => { el.setPointerCapture(e.pointerId); on = true; el.focus(); fromPointer(e); e.preventDefault(); });
    el.addEventListener('pointermove', (e) => { if (on) fromPointer(e); });
    el.addEventListener('pointerup', () => { on = false; }); el.addEventListener('pointercancel', () => { on = false; });
    el.addEventListener('wheel', (e) => { e.preventDefault(); set(C[d.key] - Math.sign(e.deltaY) * 0.04); }, { passive: false });
    el.addEventListener('dblclick', () => set(DEFAULTS()[d.key]));
    el.addEventListener('keydown', (e) => {
      const st = e.shiftKey ? 0.1 : 0.02, v = C[d.key], map = { ArrowUp: v + st, ArrowRight: v + st, ArrowDown: v - st, ArrowLeft: v - st, PageUp: v + 0.1, PageDown: v - 0.1, Home: 0, End: 1 };
      if (e.key in map) { e.preventDefault(); set(map[e.key]); }
    });
    paints.push(paint); paint();
  }

  /* ------------------------------------------------- switches / segs / power */
  function bindSwitch(el, get, set, after) {
    const paint = () => el.setAttribute('aria-checked', String(!!get()));
    el.addEventListener('click', () => { if (el.disabled) return; set(!get()); paint(); if (after) after(); schedule(); });
    paints.push(paint); paint();
  }
  function bindSeg(el, get, set, after) {
    const paint = () => $$('button', el).forEach((b) => b.setAttribute('aria-checked', String(b.dataset.v === get())));
    el.addEventListener('click', (e) => { const b = e.target.closest('button[data-v]'); if (!b) return; set(b.dataset.v); paint(); if (after) after(); schedule(); });
    paints.push(paint); paint();
  }
  function bindPower(el, get, set, panel) {
    const paint = () => { el.setAttribute('aria-pressed', String(!!get())); panel.classList.toggle('bypassed', !get()); };
    el.addEventListener('click', () => { set(!get()); paint(); schedule(); });
    paints.push(paint); paint();
  }
  function bindAdv(sw, drawer, key) {
    const paint = () => { sw.setAttribute('aria-checked', String(!!C.adv[key])); drawer.hidden = !C.adv[key]; };
    sw.addEventListener('click', () => { C.adv[key] = !C.adv[key]; paint(); saveCtl(); });
    paints.push(paint); paint();
    return (open) => { C.adv[key] = open; paint(); saveCtl(); };
  }

  /* ---------------------------------------------------- data, rank, filter */
  function prep(list) {
    for (const m of list) {
      m._open = !!m.hf_id;
      m._t = m.release_date ? Date.parse(m.release_date) : null;
      m._reason = /reasoning/i.test(m.name || '') || /reasoning/i.test(m.variant || '');
    }
    return list;
  }
  function computeExt() {
    const rng = (a, fb) => (a.length ? [Math.min(...a), Math.max(...a)] : fb);
    const pos = (k) => S.models.map((m) => m[k]).filter((v) => isNum(v) && v > 0);
    EXT.size = rng(pos('params_b'), [1, 1]); EXT.price = rng(pos('price_blended_usd_per_m'), [1, 1]);
    EXT.score = rng(S.models.map((m) => m[C.scoreKey]).filter(isNum), [0, 100]);
    EXT.t = rng(S.models.map((m) => m._t).filter(isNum), [0, 1]);
  }
  function buildPct() {
    const mk = (get, low) => {
      const v = S.models.map(get).filter(isNum).sort((a, b) => a - b);
      return (x) => {
        if (!isNum(x) || !v.length) return null;
        let lo = 0, hi = v.length; while (lo < hi) { const mid = (lo + hi) >> 1; if (v[mid] < x) lo = mid + 1; else hi = mid; }
        let up = lo; hi = v.length; while (up < hi) { const mid = (up + hi) >> 1; if (v[mid] <= x) up = mid + 1; else hi = mid; }
        const p = (lo + 0.5 * (up - lo)) / v.length;
        return low ? 1 - p : p;
      };
    };
    PCT = { q: mk((m) => m[C.scoreKey], false), c: mk((m) => m.price_blended_usd_per_m, true), s: mk((m) => m.tokens_per_sec, false), z: mk((m) => m.params_b, true), x: mk((m) => m.context_length, false), r: mk((m) => m._t, false) };
    for (const m of S.models) m._c = { q: PCT.q(m[C.scoreKey]), c: PCT.c(m.price_blended_usd_per_m), s: PCT.s(m.tokens_per_sec), z: PCT.z(m.params_b), x: PCT.x(m.context_length), r: PCT.r(m._t) };
  }
  function filterModels() {
    const c = C;
    if (!c.filtersOn) return S.models.slice();
    const a = Math.min(c.sizeMin, c.sizeMax), b = Math.max(c.sizeMin, c.sizeMax), sizeOn = a > 0.005 || b < 0.995;
    const sLo = sizeOn ? logMap(a, EXT.size) : 0, sHi = sizeOn ? logMap(b, EXT.size) : Infinity;
    const minScore = c.minScore > 0.005 ? linMap(c.minScore, EXT.score) : -Infinity;
    const maxPrice = c.maxPrice < 0.995 ? logMap(c.maxPrice, EXT.price) : Infinity;
    const since = c.since > 0.005 ? linMap(c.since, EXT.t) : -Infinity;
    const q = c.search.trim().toLowerCase(), cre = new Set(c.creators);
    return S.models.filter((m) => {
      if (sizeOn) {
        if (!isNum(m.params_b)) { if (!c.keepUnknownSize) return false; } else if (m.params_b < sLo * 0.995 || m.params_b > sHi * 1.005) return false;
      }
      const sc = m[c.scoreKey], pr = m.price_blended_usd_per_m;
      if (minScore > -Infinity && !(isNum(sc) && sc >= minScore)) return false;
      if (maxPrice < Infinity && !(isNum(pr) && pr <= maxPrice * 1.005)) return false;
      if (since > -Infinity && !(isNum(m._t) && m._t >= since)) return false;
      if (c.openness === 'open' && !m._open) return false;
      if (c.openness === 'closed' && m._open) return false;
      if (cre.size && !cre.has(m.creator)) return false;
      if (q && !((m.name || '').toLowerCase().includes(q) || (m.creator || '').toLowerCase().includes(q))) return false;
      if (c.complete && !(isNum(sc) && isNum(pr) && isNum(m.tokens_per_sec))) return false;
      if (c.noReasoning && m._reason) return false;
      return true;
    });
  }
  function weights() {
    const on = C.rankerOn;
    let W = on ? { q: C.wq, c: C.wc, s: C.ws, z: C.wz } : { q: 1, c: 0, s: 0, z: 0 };
    if (!(W.q + W.c + W.s + W.z > 0)) W = { q: 1, c: 0, s: 0, z: 0 };
    return W;
  }
  function rankModels(list) {
    for (const m of S.models) { m._rank = null; m._val = null; }
    const W = weights(), mode = C.rankerOn ? C.mode : 'weighted', dims = ['q', 'c', 's', 'z'].filter((k) => W[k] > 0);
    const items = [];
    for (const m of list) {
      let num = 0, den = 0, skip = false; const vec = [];
      for (const k of dims) {
        let v = m._c[k];
        if (v == null) { if (C.missing === 'exclude') { skip = true; break; } if (C.missing === 'skip') { vec.push(0.5); continue; } v = 0.5; }
        num += W[k] * v; den += W[k]; vec.push(v);
      }
      if (!skip) items.push({ m, val: den ? num / den : 0, vec });
    }
    if (mode === 'pareto') {
      let rest = items.slice(), layer = 1;
      while (rest.length) {
        const front = [], next = [];
        for (const a of rest) {
          let dom = false;
          if (layer <= 12) for (const b of rest) {
            if (a === b) continue;
            let ge = true, gt = false;
            for (let k = 0; k < dims.length; k++) { if (b.vec[k] < a.vec[k]) { ge = false; break; } if (b.vec[k] > a.vec[k]) gt = true; }
            if (ge && gt) { dom = true; break; }
          }
          (dom ? next : front).push(a);
        }
        front.forEach((a) => { a.layer = layer; }); rest = next; layer++;
      }
      items.sort((a, b) => a.layer - b.layer || b.val - a.val);
      items.forEach((a) => { a.text = 'L' + a.layer; a.num = a.val * 100; });
    } else if (mode === 'perdollar') {
      const sc = S.models.map((m) => m[C.scoreKey]).filter(isNum).sort((a, b) => a - b), med = sc.length ? sc[sc.length >> 1] : 0;
      for (const a of items) {
        const s = a.m[C.scoreKey], p = a.m.price_blended_usd_per_m;
        a.ratio = isNum(s) && isNum(p) && p > 0 ? s / p : -1; a.above = isNum(s) && s >= med ? 1 : 0;
        a.text = a.ratio < 0 ? '-' : a.ratio >= 100 ? Math.round(a.ratio) + '' : a.ratio.toFixed(1); a.num = Math.max(0, a.ratio);
      }
      items.sort((a, b) => b.above - a.above || b.ratio - a.ratio);
    } else {
      items.sort((a, b) => b.val - a.val);
      items.forEach((a) => { a.text = String(Math.round(a.val * 100)); a.num = a.val * 100; });
    }
    items.forEach((a, i) => { a.rank = i + 1; a.m._rank = i + 1; a.m._val = a.num; a.m._valText = a.text; });
    return items;
  }
  function recompute() {
    if (!S.models.length) D = { filtered: [], ranked: [] };
    else { D.filtered = filterModels(); D.ranked = rankModels(D.filtered); }
    paintCount(); paintVerdict(); paintMini();
    if (view && view.update) view.update();
  }

  /* ---------------------------------------------------- panels & read-outs */
  function paintCount() {
    const n = S.models.length;
    $('#count').textContent = n ? `Showing ${D.filtered.length} of ${n} models` + (D.ranked.length < D.filtered.length ? ` · ${D.ranked.length} ranked` : '') : '';
  }
  function paintBadge() {
    const b = $('#dataBadge');
    if (!S.models.length) { b.className = 'badge'; b.innerHTML = S.dataset === 'sample' ? 'Demo dataset: empty' : 'Live mode: no data fetched yet. Add your key, then press the power button'; return; }
    const demo = (S.run && S.run.source === 'sample') || S.dataset === 'sample';
    b.className = 'badge ' + (demo ? 'demo' : 'live');
    b.innerHTML = demo ? '<svg><use href="#i-warn"/></svg>DEMO DATA: synthetic, not real models' : `<svg><use href="#i-ok"/></svg>LIVE DATA · ${esc(ago(S.run && S.run.finished_at))}`;
  }
  function paintStepper() {
    const j = S.job, el = $('#stepper');
    el.innerHTML = STEPS.map(([k, t]) => {
      let cls = '';
      if (j) {
        if (j.status === 'error' && j.stage === k) cls = 'error';
        else if ((j.done_stages || []).includes(k) || (j.status === 'done' && (j.kind !== 'etl' || k !== 'eda') && (j.kind !== 'eda' || k === 'eda'))) cls = 'done';
        else if (j.stage === k && j.status === 'running') cls = 'active';
      }
      return `<span class="step ${cls}">${t}</span>`;
    }).join('');
    const running = j && (j.status === 'running' || j.status === 'queued');
    const btn = $('#runBtn');
    btn.classList.toggle('running', !!running);
    btn.setAttribute('aria-pressed', String(!!running));
  }
  function paintRunStatus() {
    const st = S.status, el = $('#runStatus'), j = S.job;
    if (j && (j.status === 'running' || j.status === 'queued')) { el.innerHTML = `<b>Running…</b> ${esc(j.stage || 'starting')}`; return; }
    if (j && j.status === 'error') { el.innerHTML = `<b style="color:var(--crit)">Failed:</b> ${esc(j.error || 'see the Log tab')}`; return; }
    const d = st && st.datasets[S.dataset];
    if (d && d.exists && d.last_run) {
      const r = d.last_run;
      el.innerHTML = `<b>${d.models}</b> models · run #${r.run_id} · ${esc(ago(r.finished_at))}<br>${r.n_rejected} rejected by validation`;
    } else if (S.dataset === 'sample') el.innerHTML = 'No demo data yet.<br>Press <b>⏻</b> to generate it.';
    else el.innerHTML = st && st.keys.aa ? 'No live data yet.<br>Press <b>⏻</b> to fetch it.' : 'No live data yet.<br>Open <b>ADV</b>, add your key, then press <b>⏻</b>.';
  }
  function paintKeys() {
    const k = (S.status && S.status.keys) || {};
    for (const [id, ok] of [['keyAAState', k.aa], ['keyHFState', k.hf]]) { const e = $('#' + id); e.textContent = ok ? '✓ set' : 'not set'; e.className = 'tag' + (ok ? '' : ' warm'); }
    const cached = (S.status && S.status.cached_raw) || [];
    $('#cacheNote').textContent = cached.length ? `Last download on disk: ${cached.slice(-1)[0].replace(/\.json$/, '')}` : 'No download on disk yet: "reuse" needs one live run first.';
    $('#swCache').disabled = !cached.length;
    $('#attr').textContent = (S.status && S.status.attribution) || '';
  }
  function paintMini() {
    const host = $('#miniScreen');
    if (!S.models.length) { host.innerHTML = '<div class="cap-note" style="padding:46px 0">no data</div>'; return; }
    const N = 24, [lo, hi] = EXT.price, la = Math.log10(lo), lr = (Math.log10(hi) - la) || 1;
    const cnt = Array(N).fill(0), sum = Array(N).fill(0), n = Array(N).fill(0);
    for (const m of D.filtered) {
      const p = m.price_blended_usd_per_m; if (!(p > 0)) continue;
      const b = clamp(Math.floor(((Math.log10(p) - la) / lr) * N), 0, N - 1); cnt[b]++;
      const s = m[C.scoreKey]; if (isNum(s)) { sum[b] += s; n[b]++; }
    }
    const sm = cnt.map((_, i) => ((cnt[i - 1] ?? cnt[i]) + 2 * cnt[i] + (cnt[i + 1] ?? cnt[i])) / 4), mx = Math.max(1, ...sm), [s0, s1] = EXT.score;
    const curve = sum.map((v, i) => (n[i] ? clamp((v / n[i] - s0) / ((s1 - s0) || 1)) : null));
    host.innerHTML = Charts.miniCurve({
      label: 'Models by price (grey) and their average quality (orange)', density: sm.map((v) => v / mx), curve,
      yLabels: [0, 0.5, 1].map((v) => ({ v, label: fmt.num(linMap(v, EXT.score), 0) })),
      xLabels: Charts.logTicks(lo, hi).filter((_, i, a) => a.length < 6 || i % 2 === 0).map((p) => ({ v: (Math.log10(p) - la) / lr, label: fmt.price(p) })),
    }) + `<div class="cap-note">grey: models by price · orange: average ${esc(SHORT[C.scoreKey].toLowerCase())}</div>`;
  }
  function paintVerdict() {
    const el = $('#verdict');
    if (!S.models.length) { el.innerHTML = ''; return; }
    const W = weights(), top = D.ranked.slice(0, 3), mode = C.rankerOn ? C.mode : 'weighted';
    const how = mode === 'pareto' ? 'Pareto layers' : mode === 'perdollar' ? 'quality per dollar' : 'weighted blend';
    const wtxt = ['q', 'c', 's', 'z'].filter((k) => W[k] > 0).map((k) => `${{ q: 'quality', c: 'cost', s: 'speed', z: 'size' }[k]} ${Math.round(W[k] * 100)}`).join(' · ');
    let h = `<div class="headline"><small>Best for your settings</small><strong>${top.length ? esc(top[0].m.name) : 'Nothing matches'}</strong><span>${top.length ? `${how} of ${esc(wtxt)}, among ${D.ranked.length} models` : 'Loosen a filter knob or reset the filters.'}</span></div>`;
    h += top.map((r, i) => {
      const m = r.m, why = [['q', 'quality'], ['c', 'cost'], ['s', 'speed'], ['z', 'size']].filter(([k]) => W[k] > 0 && m._c[k] != null && m._c[k] >= 0.7).map(([k, t]) => `<span class="tag">${t} top ${Math.max(1, Math.round((1 - m._c[k]) * 100))}%</span>`);
      return `<article class="pick ${i === 0 ? 'first' : ''}"><div class="top"><span class="rank">#${r.rank} · ${esc(r.text)}${C.rankerOn && C.mode === 'perdollar' ? ' pts/$' : ''}</span><span class="oc ${m._open ? 'open' : 'closed'}">${m._open ? 'open weights' : 'closed'}</span></div>
        <div><div class="name">${esc(m.name)}</div><div class="by">${esc(m.creator || 'unknown creator')}</div></div>
        <div class="stats"><div><small>Quality</small>${fmt.num(m[C.scoreKey], C.scoreKey === 'intelligence_index' ? 0 : 2)}</div><div><small>$ / 1M</small>${fmt.price(m.price_blended_usd_per_m)}</div><div><small>Tokens/s</small>${fmt.num(m.tokens_per_sec, 0)}</div><div><small>Size</small>${fmt.params(m.params_b)}</div></div>
        <div class="why">${why.join('') || '<span class="tag warm">balanced profile</span>'}</div></article>`;
    }).join('');
    el.innerHTML = h;
  }
  function paintCreators() {
    const counts = {};
    S.models.forEach((m) => { if (m.creator) counts[m.creator] = (counts[m.creator] || 0) + 1; });
    const list = Object.entries(counts).sort((a, b) => b[1] - a[1]);
    $('#creatorChips').innerHTML = list.map(([n, k]) => `<button class="chip" data-c="${esc(n)}" aria-pressed="${C.creators.includes(n)}">${esc(n)} <small>${k}</small></button>`).join('') || '<span class="hint">no data</span>';
  }

  /* ----------------------------------------------------------- tooltips etc */
  const tip = $('#tip');
  function showTip(m, e) {
    tip.innerHTML = `<b>${esc(m.name)}</b><br><span class="dim">${esc(m.creator || '')} · ${m._open ? 'open weights' : 'closed'}${m._rank ? ' · rank #' + m._rank : ''}</span><br>Quality ${fmt.num(m[C.scoreKey], 1)} · ${fmt.price(m.price_blended_usd_per_m)}/1M · ${fmt.num(m.tokens_per_sec, 0)} tok/s<br>Size ${fmt.params(m.params_b)} · context ${fmt.ctx(m.context_length)} · ${fmt.date(m._t)}`;
    tip.hidden = false;
    const w = tip.offsetWidth, h = tip.offsetHeight;
    tip.style.left = Math.min(innerWidth - w - 8, e.clientX + 14) + 'px';
    tip.style.top = (e.clientY + 18 + h > innerHeight ? e.clientY - h - 12 : e.clientY + 18) + 'px';
  }
  const hideTip = () => { tip.hidden = true; };
  function togglePin(key) {
    const i = S.pins.indexOf(key);
    if (i >= 0) S.pins.splice(i, 1); else { S.pins.push(key); if (S.pins.length > MAX_PINS) S.pins.shift(); }
    store.set('mc.pins', S.pins);
    if (view && view.update) view.update();
  }

  /* ------------------------------------------------------------------ views */
  const AX = {
    score: { label: () => SCORE_LABEL[C.scoreKey], get: (m) => m[C.scoreKey], hi: true, f: (v) => fmt.num(v, C.scoreKey.endsWith('index') ? 0 : 2) },
    size: { label: () => 'Parameters (billions, total)', get: (m) => m.params_b, log: true, hi: false, f: fmt.params },
    price: { label: () => 'Price, USD per 1M tokens (3:1 blend)', get: (m) => m.price_blended_usd_per_m, log: true, hi: false, f: fmt.price },
    speed: { label: () => 'Output speed (tokens per second)', get: (m) => m.tokens_per_sec, hi: true, f: (v) => fmt.num(v, 0) },
    ttft: { label: () => 'Time to first token (s)', get: (m) => m.ttft_s, hi: false, f: (v) => fmt.num(v, 2) },
    ctx: { label: () => 'Context length (tokens)', get: (m) => m.context_length, log: true, hi: true, f: fmt.ctx },
    date: { label: () => 'Release date', get: (m) => m._t, date: true, hi: true, f: fmt.date },
    rank: { label: () => 'Rank value', get: (m) => m._val, hi: true, f: (v) => fmt.num(v, 0) },
  };
  const PRESETS = { size: { x: 'size', y: 'score', xLog: true, yLog: false }, price: { x: 'price', y: 'score', xLog: true, yLog: false }, speed: { x: 'price', y: 'speed', xLog: true, yLog: false }, time: { x: 'date', y: 'score', xLog: false, yLog: false } };
  const hostOf = () => $('#view');
  const VIEWS = {};

  /* --- explore --- */
  const EX = { idx: [] };
  VIEWS.explore = {
    mount(h) {
      const opts = Object.entries(AX).map(([k, a]) => `<option value="${k}">${esc(a.label())}</option>`).join('');
      h.innerHTML = `<div class="toolbar"><div class="chips" id="exPresets">${[['size', 'Score vs size'], ['price', 'Score vs price'], ['speed', 'Speed vs price'], ['time', 'Score over time']].map(([k, t]) => `<button class="chip" data-p="${k}">${t}</button>`).join('')}</div></div>
      <div class="toolbar"><label>X <select id="exX">${opts}</select></label><label>Y <select id="exY">${opts}</select></label>
        <label>Colour <select id="exC"><option value="group">Open vs closed</option><option value="score">Quality</option><option value="rank">Rank value</option></select></label>
        <label><input type="checkbox" id="exXl"> log X</label><label><input type="checkbox" id="exYl"> log Y</label><label><input type="checkbox" id="exFr"> frontier</label><label><input type="checkbox" id="exTop"> ring the top ranked</label></div>
      <div class="legend" id="exLegend"></div><div class="chartbox" id="exHost"></div><p class="note" id="exNote"></p>`;
      const ex = S.explore, sync = () => { $('#exX').value = ex.x; $('#exY').value = ex.y; $('#exC').value = ex.color; $('#exXl').checked = ex.xLog; $('#exYl').checked = ex.yLog; $('#exFr').checked = ex.frontier; $('#exTop').checked = ex.top; };
      sync();
      $('#exPresets').onclick = (e) => { const b = e.target.closest('[data-p]'); if (b) { Object.assign(ex, PRESETS[b.dataset.p]); sync(); this.update(); } };
      $('#exX').onchange = (e) => { ex.x = e.target.value; ex.xLog = !!AX[ex.x].log; sync(); this.update(); };
      $('#exY').onchange = (e) => { ex.y = e.target.value; ex.yLog = !!AX[ex.y].log; sync(); this.update(); };
      $('#exC').onchange = (e) => { ex.color = e.target.value; this.update(); };
      for (const [id, k] of [['exXl', 'xLog'], ['exYl', 'yLog'], ['exFr', 'frontier'], ['exTop', 'top']]) $('#' + id).onchange = (e) => { ex[k] = e.target.checked; this.update(); };
      const box = $('#exHost');
      box.onmousemove = (e) => { const c = e.target.closest('circle.pt'); if (c) showTip(EX.idx[+c.dataset.i], e); else hideTip(); };
      box.onmouseleave = hideTip;
      box.onclick = (e) => { const c = e.target.closest('circle.pt'); if (c) { togglePin(EX.idx[+c.dataset.i].model_key); toast(S.pins.includes(EX.idx[+c.dataset.i].model_key) ? 'Pinned to Compare' : 'Unpinned'); } };
    },
    update() {
      if (!$('#exHost')) return;
      const ex = S.explore, X = AX[ex.x], Y = AX[ex.y], list = D.filtered, topSet = new Set(D.ranked.slice(0, C.topN).map((r) => r.m.model_key));
      const pts = [], idx = [];
      let cLo = Infinity, cHi = -Infinity;
      const cval = (m) => (ex.color === 'score' ? m[C.scoreKey] : ex.color === 'rank' ? m._val : null);
      if (ex.color !== 'group') list.forEach((m) => { const v = cval(m); if (isNum(v)) { cLo = Math.min(cLo, v); cHi = Math.max(cHi, v); } });
      for (const m of list) {
        const x = X.get(m), y = Y.get(m);
        if (!isNum(x) || !isNum(y)) continue;
        idx.push(m);
        const v = cval(m), p = { x, y, i: idx.length - 1, r: topSet.has(m.model_key) ? 6.5 : 4.8 };
        if (ex.color === 'group') p.cls = m._open ? 'c-open' : 'c-closed'; else p.fill = isNum(v) ? Charts.ramp((v - cLo) / ((cHi - cLo) || 1)) : 'var(--muted)';
        pts.push(p);
      }
      EX.idx = idx;
      const front = ex.frontier ? Charts.pareto2(pts, X.hi, Y.hi) : [];
      const rings = [], labels = [];
      idx.forEach((m, i) => {
        const p = pts[i];
        if (S.pins.includes(m.model_key)) rings.push({ x: p.x, y: p.y, r: p.r, cls: 'pin' }); else if (ex.top && topSet.has(m.model_key)) rings.push({ x: p.x, y: p.y, r: p.r });
      });
      D.ranked.slice(0, 5).forEach((r) => { const i = idx.indexOf(r.m); if (i >= 0) labels.push({ x: pts[i].x, y: pts[i].y, text: r.m.name.length > 28 ? r.m.name.slice(0, 27) + '…' : r.m.name }); });
      $('#exHost').innerHTML = Charts.scatter({ pts, xLog: ex.xLog, yLog: ex.yLog, xDate: !!X.date, xLabel: X.label(), yLabel: Y.label(), xFmt: X.f, yFmt: Y.f, front, rings, labels, title: `${Y.label()} versus ${X.label()}` });
      $('#exLegend').innerHTML = (ex.color === 'group' ? `<span><i style="background:var(--s1)"></i>Open weights</span><span><i style="background:var(--s2)"></i>Closed / not on Hugging Face</span>` : `<span>${ex.color === 'score' ? 'Quality' : 'Rank value'}: low<span class="ramp"></span>high</span>`) + (ex.frontier ? '<span><span class="dash"></span>best-available frontier</span>' : '') + (S.pins.length ? '<span>◯ pinned (blue ring)</span>' : '');
      const lacking = list.length - idx.length;
      $('#exNote').textContent = `${idx.length} of ${list.length} filtered models have both values` + (lacking ? ` (${lacking} are missing one: see Data quality).` : '.') + ' Hover for details, click a point to pin it for comparison.';
    },
  };

  /* --- leaderboard --- */
  const LB = [
    { k: 'rank', t: 'Rank', num: 1, get: (r) => r.rank }, { k: 'name', t: 'Model', get: (r) => r.m.name.toLowerCase() }, { k: 'open', t: 'Type', get: (r) => (r.m._open ? 1 : 0) },
    { k: 'val', t: 'Rank value', get: (r) => r.num }, { k: 'q', t: 'Quality', num: 1, get: (r) => r.m[C.scoreKey] }, { k: 'p', t: '$ / 1M', num: 1, get: (r) => r.m.price_blended_usd_per_m },
    { k: 's', t: 'Tok/s', num: 1, get: (r) => r.m.tokens_per_sec }, { k: 'z', t: 'Params', num: 1, get: (r) => r.m.params_b }, { k: 'c', t: 'Context', num: 1, get: (r) => r.m.context_length }, { k: 'd', t: 'Released', get: (r) => r.m._t },
  ];
  VIEWS.board = {
    mount(h) {
      h.innerHTML = `<div class="toolbar"><button class="pill" id="lbPin"><svg><use href="#i-link"/></svg>Pin the top 3</button><button class="pill" id="lbCsv"><svg><use href="#i-csv"/></svg>Export this table (CSV)</button><span class="count" id="lbCnt"></span></div><div class="table-wrap" id="lbHost"></div><p class="note">Click a column to sort; click the star to pin a model to Compare.</p>`;
      $('#lbPin').onclick = () => { S.pins = D.ranked.slice(0, 3).map((r) => r.m.model_key); store.set('mc.pins', S.pins); this.update(); toast('Pinned the top 3. Open Compare.'); };
      $('#lbCsv').onclick = () => { const rows = this.rows(); download('model-leaderboard.csv', csvText([['rank', 'model', 'creator', 'open_weights', 'rank_value', 'quality', 'price_usd_per_m', 'tokens_per_sec', 'params_b', 'context_length', 'released'], ...rows.map((r) => [r.rank, r.m.name, r.m.creator, r.m._open, r.text, r.m[C.scoreKey], r.m.price_blended_usd_per_m, r.m.tokens_per_sec, r.m.params_b, r.m.context_length, fmt.date(r.m._t)])])); };
      const host = $('#lbHost');
      host.onclick = (e) => {
        const th = e.target.closest('th[data-k]'), pin = e.target.closest('[data-pin]'), more = e.target.closest('[data-more]');
        if (th) { const k = th.dataset.k; if (S.lb.sort === k) S.lb.dir = -S.lb.dir; else { S.lb.sort = k; S.lb.dir = k === 'name' || k === 'p' || k === 'z' || k === 'rank' ? 1 : -1; } this.update(); }
        if (pin) togglePin(pin.dataset.pin);
        if (more) { S.lb.shown += 100; this.update(); }
      };
      host.onmousemove = (e) => { const tr = e.target.closest('tr[data-key]'); if (tr) { const m = S.models.find((x) => x.model_key === tr.dataset.key); if (m) showTip(m, e); } else hideTip(); };
      host.onmouseleave = hideTip;
    },
    rows() {
      const rows = D.ranked.slice();
      if (S.lb.sort) { const col = LB.find((c) => c.k === S.lb.sort), dir = S.lb.dir; rows.sort((a, b) => { const x = col.get(a), y = col.get(b); if (x == null && y == null) return 0; if (x == null) return 1; if (y == null) return -1; return (x < y ? -1 : x > y ? 1 : 0) * dir; }); }
      return rows;
    },
    update() {
      if (!$('#lbHost')) return;
      const rows = this.rows(), shown = rows.slice(0, S.lb.shown), maxv = Math.max(1, ...rows.map((r) => r.num || 0));
      const head = LB.map((c) => `<th data-k="${c.k}" class="${c.num ? 'num' : ''}" ${S.lb.sort === c.k ? `aria-sort="${S.lb.dir > 0 ? 'ascending' : 'descending'}"` : ''}>${c.t}${S.lb.sort === c.k ? (S.lb.dir > 0 ? ' ▲' : ' ▼') : ''}</th>`).join('');
      const miss = '<span class="miss">-</span>';
      const body = shown.map((r) => { const m = r.m, pinned = S.pins.includes(m.model_key);
        return `<tr data-key="${esc(m.model_key)}" class="${pinned ? 'sel' : ''}"><td class="num"><button class="pinbtn" data-pin="${esc(m.model_key)}" aria-pressed="${pinned}" aria-label="Pin ${esc(m.name)}">${pinned ? '★' : '☆'}</button>${r.rank}</td>
        <td class="name">${esc(m.name)}<small>${esc(m.creator || '')}</small></td><td><span class="oc ${m._open ? 'open' : 'closed'}">${m._open ? 'Open' : 'Closed'}</span></td>
        <td><span class="bar-track"><span class="bar" style="width:${Math.max(2, (100 * (r.num || 0)) / maxv).toFixed(0)}%"></span></span>${esc(r.text)}</td>
        <td class="num">${m[C.scoreKey] == null ? miss : fmt.num(m[C.scoreKey], C.scoreKey.endsWith('index') ? 1 : 3)}</td><td class="num">${m.price_blended_usd_per_m == null ? miss : fmt.price(m.price_blended_usd_per_m)}</td>
        <td class="num">${m.tokens_per_sec == null ? miss : fmt.num(m.tokens_per_sec, 0)}</td><td class="num">${m.params_b == null ? miss : fmt.params(m.params_b)}</td><td class="num">${m.context_length == null ? miss : fmt.ctx(m.context_length)}</td><td>${m._t == null ? miss : fmt.date(m._t)}</td></tr>`; }).join('');
      $('#lbHost').innerHTML = `<table><thead><tr>${head}</tr></thead><tbody>${body || '<tr><td colspan="10">No models match the current filters.</td></tr>'}</tbody></table>` + (rows.length > shown.length ? `<div style="padding:10px"><button class="pill" data-more="1">Show ${Math.min(100, rows.length - shown.length)} more of ${rows.length - shown.length}</button></div>` : '');
      $('#lbCnt').textContent = `${rows.length} models ranked`;
    },
  };

  /* --- compare --- */
  VIEWS.compare = {
    mount(h) {
      h.innerHTML = `<div class="toolbar"><div class="chips" id="cmpChips"></div><button class="pill" id="cmpTop">Pin the top 3</button><button class="pill" id="cmpClear">Clear</button></div><div class="grid2"><div class="chartbox" id="cmpRadar"></div><div class="table-wrap" id="cmpTable"></div></div><p class="note">Radar axes are percentiles among all models (further out = better; cost and size count lower as better), so shapes are comparable. Pin up to ${MAX_PINS} models from the chart or the leaderboard.</p>`;
      $('#cmpTop').onclick = () => { S.pins = D.ranked.slice(0, 3).map((r) => r.m.model_key); store.set('mc.pins', S.pins); this.update(); };
      $('#cmpClear').onclick = () => { S.pins = []; store.set('mc.pins', S.pins); this.update(); };
      $('#cmpChips').onclick = (e) => { const b = e.target.closest('[data-pin]'); if (b) togglePin(b.dataset.pin); };
    },
    update() {
      if (!$('#cmpRadar')) return;
      const ms = S.pins.map((k) => S.models.find((m) => m.model_key === k)).filter(Boolean);
      $('#cmpChips').innerHTML = ms.map((m, i) => `<button class="chip on" data-pin="${esc(m.model_key)}" title="Remove"><span style="color:${PIN_COLORS[i]}">●</span> ${esc(m.name)} ✕</button>`).join('') || '<span class="count">Nothing pinned yet.</span>';
      if (!ms.length) { $('#cmpRadar').innerHTML = Charts.empty('Pin models from the Explore chart (click a point) or the Leaderboard (star) to compare them here.'); $('#cmpTable').innerHTML = ''; return; }
      const axes = ['Quality', 'Cheap', 'Speed', 'Small', 'Context', 'Recent'], keys = ['q', 'c', 's', 'z', 'x', 'r'];
      $('#cmpRadar').innerHTML = Charts.radar({ title: 'Percentile profile of pinned models', axes, series: ms.map((m, i) => ({ name: m.name, color: PIN_COLORS[i], values: keys.map((k) => m._c[k]) })) });
      const rows = [
        ['Quality', (m) => m[C.scoreKey], (v) => fmt.num(v, 2), true], ['Price / 1M tokens', (m) => m.price_blended_usd_per_m, fmt.price, false], ['Tokens per second', (m) => m.tokens_per_sec, (v) => fmt.num(v, 0), true],
        ['Time to first token (s)', (m) => m.ttft_s, (v) => fmt.num(v, 2), false], ['Parameters', (m) => m.params_b, fmt.params, false], ['Context', (m) => m.context_length, fmt.ctx, true], ['Released', (m) => m._t, fmt.date, true], ['Rank here', (m) => m._rank, (v) => '#' + v, false],
      ];
      let t = `<table><thead><tr><th>Metric</th>${ms.map((m, i) => `<th style="color:${PIN_COLORS[i]}">${esc(m.name)}</th>`).join('')}</tr></thead><tbody>`;
      for (const [label, get, f, hi] of rows) {
        const vals = ms.map(get), nums = vals.filter(isNum), best = nums.length > 1 ? (hi ? Math.max(...nums) : Math.min(...nums)) : null;
        t += `<tr><td>${label}</td>${vals.map((v) => `<td class="${best !== null && v === best ? '' : 'miss'}" ${best !== null && v === best ? 'style="font-weight:700"' : ''}>${isNum(v) ? esc(f(v)) + (best !== null && v === best ? ' ★' : '') : '-'}</td>`).join('')}</tr>`;
      }
      $('#cmpTable').innerHTML = t + '</tbody></table>';
    },
  };

  /* --- correlations --- */
  function ranksOf(vals) {
    const idx = vals.map((v, i) => [v, i]).sort((a, b) => a[0] - b[0]), r = Array(vals.length);
    for (let i = 0; i < idx.length;) { let j = i; while (j + 1 < idx.length && idx[j + 1][0] === idx[i][0]) j++; const avg = (i + j) / 2 + 1; for (let k = i; k <= j; k++) r[idx[k][1]] = avg; i = j + 1; }
    return r;
  }
  function spearman(a, b) {
    const xs = [], ys = [];
    for (let i = 0; i < a.length; i++) if (isNum(a[i]) && isNum(b[i])) { xs.push(a[i]); ys.push(b[i]); }
    if (xs.length < 8) return { r: null, n: xs.length };
    const rx = ranksOf(xs), ry = ranksOf(ys), n = xs.length, mx = rx.reduce((s, v) => s + v, 0) / n, my = ry.reduce((s, v) => s + v, 0) / n;
    let sxy = 0, sxx = 0, syy = 0;
    for (let i = 0; i < n; i++) { sxy += (rx[i] - mx) * (ry[i] - my); sxx += (rx[i] - mx) ** 2; syy += (ry[i] - my) ** 2; }
    return { r: sxx && syy ? sxy / Math.sqrt(sxx * syy) : null, n };
  }
  VIEWS.corr = {
    mount(h) { h.innerHTML = '<div class="grid2"><div class="chartbox" id="coHost"></div><div class="stack" id="coSide"></div></div>'; },
    update() {
      if (!$('#coHost')) return;
      const cols = SCORE_OPTS.map(([k]) => k).filter((k) => D.filtered.filter((m) => isNum(m[k])).length >= 15);
      if (cols.length < 3) { $('#coHost').innerHTML = Charts.empty('Need at least three benchmarks with 15+ filtered models each. Loosen the filters.'); $('#coSide').innerHTML = ''; return; }
      const data = cols.map((k) => D.filtered.map((m) => m[k])), matrix = [], counts = [], pairs = [];
      cols.forEach((_, i) => { matrix.push([]); counts.push([]); cols.forEach((__, j) => { const s = i === j ? { r: 1, n: data[i].filter(isNum).length } : spearman(data[i], data[j]); matrix[i].push(s.r); counts[i].push(s.n); if (j > i && s.r != null) pairs.push([cols[i], cols[j], s.r, s.n]); }); });
      const off = pairs.map((p) => p[2]), lo = Math.min(...off), vmin = lo >= 0 ? Math.max(0, Math.floor((lo - 0.05) * 10) / 10) : -1;
      $('#coHost').innerHTML = Charts.heatmap({ title: 'Spearman correlation between benchmarks', labels: cols.map((k) => SHORT[k]), matrix, counts, vmin });
      pairs.sort((a, b) => a[2] - b[2]);
      const row = (p) => `<tr><td>${esc(SHORT[p[0]])} × ${esc(SHORT[p[1]])}</td><td class="num">${p[2].toFixed(2)}</td><td class="num">${p[3]}</td></tr>`;
      const mean = off.reduce((s, v) => s + v, 0) / off.length;
      $('#coSide').innerHTML = `<div class="card"><h3>Summary</h3><p>Mean pairwise correlation <b>${mean.toFixed(2)}</b> across ${pairs.length} pairs (range ${lo.toFixed(2)} to ${Math.max(...off).toFixed(2)}), using ${D.filtered.length} filtered models. Colours run from ${vmin.toFixed(1)} to 1.0.</p></div>
        <div class="card"><h3>Most distinct pairs <small>least redundant</small></h3><div class="table-wrap"><table><tbody>${pairs.slice(0, 5).map(row).join('')}</tbody></table></div></div>
        <div class="card"><h3>Most redundant pairs</h3><div class="table-wrap"><table><tbody>${pairs.slice(-4).reverse().map(row).join('')}</tbody></table></div></div>`;
    },
  };

  /* --- trends --- */
  VIEWS.trends = {
    mount(h) { h.innerHTML = '<div class="stack"><div><div class="legend"><span><i style="background:var(--s1)"></i>open weights</span><span><i style="background:var(--s2)"></i>closed</span><span><span class="dash" style="border-top-style:solid"></span>best so far (all)</span><span><span class="dash" style="border-top-color:var(--s1);border-top-style:solid"></span>best open-weights so far</span></div><div class="chartbox" id="trA"></div></div><div><div class="legend" id="trLegB"></div><div class="chartbox" id="trB"></div></div></div><p class="note" id="trNote"></p>'; },
    update() {
      if (!$('#trA')) return;
      const sk = C.scoreKey, d = D.filtered.filter((m) => isNum(m._t) && isNum(m[sk])).sort((a, b) => a._t - b._t);
      if (d.length < 5) { $('#trA').innerHTML = Charts.empty('Not enough dated models under the current filters.'); $('#trB').innerHTML = ''; return; }
      const all = [], open = []; let best = -Infinity, bestO = -Infinity;
      for (const m of d) { if (m[sk] > best) { best = m[sk]; all.push({ x: m._t, y: best }); } if (m._open && m[sk] > bestO) { bestO = m[sk]; open.push({ x: m._t, y: bestO }); } }
      const last = d[d.length - 1]._t; all.push({ x: last, y: best }); if (open.length) open.push({ x: last, y: bestO });
      const pts = d.map((m, i) => ({ x: m._t, y: m[sk], i, cls: m._open ? 'c-open' : 'c-closed' }));
      const A = (id, o) => { $(id).innerHTML = Charts.scatter(o); };
      A('#trA', { h: 420, pts, xDate: true, xLabel: 'Release date', yLabel: SCORE_LABEL[sk], yFmt: AX.score.f, lines: [{ pts: all, cls: 'ln-all', step: true }, { pts: open, cls: 'ln-open', step: true }], title: 'Quality over release date' });
      const sc = d.map((m) => m[sk]).sort((a, b) => a - b), med = sc[sc.length >> 1], cheap = d.filter((m) => isNum(m.price_blended_usd_per_m) && m.price_blended_usd_per_m > 0 && m[sk] >= med);
      let low = Infinity; const line = [];
      for (const m of cheap) if (m.price_blended_usd_per_m < low) { low = m.price_blended_usd_per_m; line.push({ x: m._t, y: low }); }
      if (line.length) line.push({ x: last, y: low });
      $('#trLegB').innerHTML = `<span>Only models scoring at least ${fmt.num(med, 1)} (the median): price per 1M tokens, with the cheapest so far as a line</span>`;
      A('#trB', { h: 340, pts: cheap.map((m, i) => ({ x: m._t, y: m.price_blended_usd_per_m, i, fill: 'var(--muted)' })), xDate: true, yLog: true, xLabel: 'Release date', yLabel: 'USD per 1M tokens', yFmt: fmt.price, lines: [{ pts: line, cls: 'ln-cheap', step: true }], title: 'Cheapest model above the median quality, over time' });
      const first = line[0], now = line[line.length - 1];
      $('#trNote').textContent = line.length > 1 ? `Cheapest model scoring ≥ ${fmt.num(med, 1)}: ${fmt.price(first.y)} when it first appeared, ${fmt.price(now.y)} now (${(first.y / now.y).toFixed(1)}× cheaper). Best overall: ${fmt.num(best, 1)}; best open-weights: ${open.length ? fmt.num(bestO, 1) : 'none'}.` : '';
    },
  };

  /* --- data quality --- */
  const MISS_COLS = ['intelligence_index', 'coding_index', 'math_index', 'mmlu_pro', 'gpqa', 'hle', 'livecodebench', 'scicode', 'math_500', 'aime', 'price_blended_usd_per_m', 'tokens_per_sec', 'ttft_s', 'context_length', 'params_b', 'release_date'];
  const FIELD_NAME = { price_blended_usd_per_m: 'Price', tokens_per_sec: 'Speed', context_length: 'Context length', params_b: 'Parameter count', hle: 'HLE score' };
  const hbars = (rows, warmAt) => `<div class="hbars">${rows.map(([l, v, max, txt]) => `<div class="hbar"><span>${esc(l)}</span><span class="track"><span class="fillb ${warmAt != null && v / max > warmAt ? 'warm' : ''}" style="display:block;width:${(100 * v / (max || 1)).toFixed(1)}%"></span></span><span class="v">${esc(txt)}</span></div>`).join('')}</div>`;
  VIEWS.quality = {
    async mount(h) {
      h.innerHTML = '<div class="empty"><p>Loading…</p></div>';
      try { S.quality = await api(`/api/quality?db=${S.dataset}`); } catch (e) { h.innerHTML = `<div class="empty"><p class="err">${esc(e.message)}</p></div>`; return; }
      if (S.tab !== 'quality') return;
      this.draw(h);
    },
    draw(h) {
      const q = S.quality, n = S.models.length, st = S.status && S.status.datasets[S.dataset], stats = (st && st.last_run && st.last_run.stats) || {};
      const missing = MISS_COLS.map((k) => [k === 'price_blended_usd_per_m' ? 'Price' : k === 'tokens_per_sec' ? 'Speed' : k === 'ttft_s' ? 'Time to 1st token' : k === 'context_length' ? 'Context' : k === 'params_b' ? 'Parameters' : k === 'release_date' ? 'Release date' : (SHORT[k] || k), S.models.filter((m) => m[k] == null).length]).sort((a, b) => b[1] - a[1]);
      const kv = (o) => Object.entries(o || {}).map(([k, v]) => [k.replace(/_/g, ' '), v, Math.max(1, ...Object.values(o)), String(v)]);
      const grp = (k) => { const o = S.models.filter((m) => m._open), c = S.models.filter((m) => !m._open), pct = (a) => (a.length ? Math.round((100 * a.filter((m) => m[k] == null).length) / a.length) + '%' : '-'); return `<tr><td>${esc(FIELD_NAME[k] || k)}</td><td class="num">${pct(o)}</td><td class="num">${pct(c)}</td></tr>`; };
      const mh = (q.manual_hf || []).map((r) => `<tr><td>${esc(r.match_key)}</td><td>${esc(r.hf_repo_id)}</td><td><button class="chip" data-rm="${esc(r.match_key)}">remove</button></td></tr>`).join('');
      h.innerHTML = `<div class="grid2">
        <div class="card"><h3>Missing values <small>${n} models</small></h3>${hbars(missing.map(([l, v]) => [l, v, n, `${Math.round((100 * v) / n)}%`]), 0.4)}</div>
        <div class="stack">
          <div class="card"><h3>Missing by model type <small>% of rows empty</small></h3><div class="table-wrap"><table><thead><tr><th>Field</th><th class="num">Open</th><th class="num">Closed</th></tr></thead><tbody>${['price_blended_usd_per_m', 'tokens_per_sec', 'context_length', 'params_b', 'hle'].map(grp).join('')}</tbody></table></div><p class="note">Gaps are rarely random: closed models have no parameter count, and models absent from OpenRouter have no context length.</p></div>
          <div class="card"><h3>How models were matched <small>name matching across sources${stats.fuzzy_threshold ? ', strictness ' + stats.fuzzy_threshold : ''}</small></h3>${hbars(kv(stats.match_methods))}<h3 style="margin-top:14px">Where sizes came from</h3>${hbars(kv(stats.params_sources))}<h3 style="margin-top:14px">Where prices came from</h3>${hbars(kv(stats.price_sources))}</div>
          <div class="card"><h3>Hugging Face lookups</h3>${(q.hf_status || []).length ? hbars(q.hf_status.map((r) => [r.status.replace(/_/g, ' '), r.n, Math.max(...q.hf_status.map((x) => x.n)), String(r.n)])) : '<p class="note">None yet.</p>'}</div>
        </div></div>
        <div class="card" style="margin-top:18px"><h3>Rows rejected by validation <small>last run</small></h3>${q.rejects.length ? `<div class="table-wrap"><table><thead><tr><th>Key</th><th>Model</th><th>Why</th></tr></thead><tbody>${q.rejects.map((r) => `<tr><td>${esc(r.model_key)}</td><td>${esc(r.name)}</td><td style="white-space:normal">${esc(r.errors)}</td></tr>`).join('')}</tbody></table></div>` : '<p class="note">Every row passed validation.</p>'}</div>
        <div class="card" style="margin-top:18px"><h3>Add a Hugging Face repo by hand <small>for open models OpenRouter does not list</small></h3>
          <div class="toolbar"><input type="text" id="mhKey" list="mhKeys" placeholder="match key, e.g. llama-3-1-8b" style="min-width:230px"><input type="text" id="mhRepo" placeholder="owner/repo, e.g. meta-llama/Llama-3.1-8B-Instruct" style="min-width:320px"><button class="pill" id="mhAdd">Add</button></div>
          <datalist id="mhKeys">${(q.unmatched || []).map((u) => `<option value="${esc(u.name_key)}">${esc(u.name)}</option>`).join('')}</datalist>
          ${mh ? `<div class="table-wrap"><table><tbody>${mh}</tbody></table></div>` : '<p class="note">No manual mappings yet.</p>'}
          <p class="note">${(q.unmatched || []).length} models have no OpenRouter match and no repo; start typing to see their keys. Changes apply on the next pipeline run.</p></div>
        <div class="card" style="margin-top:18px"><h3>Run history</h3><div class="table-wrap"><table><thead><tr><th>Run</th><th>Finished</th><th>Source</th><th class="num">AA</th><th class="num">OpenRouter</th><th class="num">HF</th><th class="num">Valid</th><th class="num">Rejected</th></tr></thead><tbody>${q.runs.map((r) => `<tr><td>#${r.run_id}</td><td>${esc(r.finished_at)}</td><td>${esc(r.source)}</td><td class="num">${r.n_aa}</td><td class="num">${r.n_openrouter}</td><td class="num">${r.n_hf}</td><td class="num">${r.n_valid}</td><td class="num">${r.n_rejected}</td></tr>`).join('')}</tbody></table></div>${q.snapshots.length > 1 ? `<p class="note">${q.snapshots.length} daily snapshots stored: re-running the pipeline builds a true time series per model.</p>` : ''}</div>`;
      const edit = async (body) => { try { const r = await api('/api/manual_hf', { op: body.op, match_key: body.match_key, hf_repo_id: body.hf_repo_id }); S.quality.manual_hf = r.manual_hf; toast('Saved. Run the pipeline to apply it.'); this.draw(hostOf()); } catch (e) { toast(e.message, true); } };
      $('#mhAdd').onclick = () => edit({ op: 'add', match_key: $('#mhKey').value, hf_repo_id: $('#mhRepo').value });
      $$('[data-rm]', h).forEach((b) => { b.onclick = () => edit({ op: 'remove', match_key: b.dataset.rm }); });
    },
    update() { /* static until remounted */ },
  };

  /* --- SQL --- */
  const SQL_PRESETS = [
    ['Best value', 'SELECT name, creator, intelligence_index AS quality, price_blended_usd_per_m AS price,\n       ROUND(index_points_per_usd, 1) AS pts_per_usd\nFROM v_value\nWHERE intelligence_index >= (SELECT AVG(intelligence_index) FROM models)\nORDER BY index_points_per_usd DESC\nLIMIT 15'],
    ['Smallest per score', 'SELECT name, params_b, intelligence_index\nFROM models\nWHERE params_b IS NOT NULL AND intelligence_index >= 60\nORDER BY params_b\nLIMIT 15'],
    ['By creator', 'SELECT creator, COUNT(*) AS models, ROUND(AVG(intelligence_index), 1) AS avg_quality,\n       ROUND(AVG(price_blended_usd_per_m), 2) AS avg_price\nFROM models\nGROUP BY creator\nORDER BY avg_quality DESC'],
    ['Rejected rows', 'SELECT model_key, errors FROM rejects ORDER BY rowid DESC LIMIT 50'],
    ['Name matching', 'SELECT match_method, COUNT(*) AS models FROM models GROUP BY match_method'],
    ['Tables', "SELECT name, type FROM sqlite_master WHERE type IN ('table','view') ORDER BY type, name"],
  ];
  VIEWS.sql = {
    mount(h) {
      if (!S.sql.query) S.sql.query = SQL_PRESETS[0][1];
      h.innerHTML = `<div class="toolbar"><div class="chips" id="sqlPre">${SQL_PRESETS.map(([t], i) => `<button class="chip" data-i="${i}">${esc(t)}</button>`).join('')}</div></div>
        <textarea class="sql" id="sqlText" spellcheck="false" aria-label="SQL query"></textarea>
        <div class="toolbar" style="margin-top:10px"><button class="pill primary" id="sqlRun">Run (Ctrl+Enter)</button><button class="pill" id="sqlCsv"><svg><use href="#i-csv"/></svg>Download CSV</button><span class="count" id="sqlInfo">Read-only: only SELECT works. Tables: models, model_snapshots, rejects, pipeline_runs, raw_*, view v_value.</span></div>
        <div id="sqlOut"></div>`;
      $('#sqlText').value = S.sql.query;
      $('#sqlText').oninput = (e) => { S.sql.query = e.target.value; };
      $('#sqlText').onkeydown = (e) => { if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); this.run(); } };
      $('#sqlPre').onclick = (e) => { const b = e.target.closest('[data-i]'); if (b) { S.sql.query = SQL_PRESETS[+b.dataset.i][1]; $('#sqlText').value = S.sql.query; this.run(); } };
      $('#sqlRun').onclick = () => this.run();
      $('#sqlCsv').onclick = () => { const r = S.sql.result; if (r) download('query.csv', csvText([r.columns, ...r.rows])); };
      this.draw();
    },
    async run() {
      try { S.sql.result = await api('/api/sql', { dataset: S.dataset, query: S.sql.query }); S.sql.error = ''; } catch (e) { S.sql.error = e.message; S.sql.result = null; }
      this.draw();
    },
    draw() {
      const out = $('#sqlOut'); if (!out) return;
      const r = S.sql.result;
      if (S.sql.error) out.innerHTML = `<p class="err" role="alert">${esc(S.sql.error)}</p>`;
      else if (r) {
        $('#sqlInfo').textContent = `${r.rows.length}${r.truncated ? '+' : ''} rows · ${r.ms} ms${r.truncated ? ' (truncated at 1000)' : ''}`;
        out.innerHTML = `<div class="table-wrap"><table><thead><tr>${r.columns.map((c) => `<th>${esc(c)}</th>`).join('')}</tr></thead><tbody>${r.rows.map((row) => `<tr>${row.map((v) => `<td class="${typeof v === 'number' ? 'num' : ''}">${v == null ? '<span class="miss">null</span>' : esc(typeof v === 'number' ? +v.toFixed(4) : v)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
      } else out.innerHTML = '';
    },
    update() {},
  };

  /* --- report --- */
  function md(src) {
    const inline = (s) => esc(s).replace(/`([^`]+)`/g, '<code>$1</code>').replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>').replace(/\*([^*\s][^*]*)\*/g, '<i>$1</i>');
    const lines = src.split('\n'); let out = '', i = 0;
    const cells = (l) => l.replace(/^\||\|$/g, '').split(/(?<!\\)\|/).map((c) => c.trim().replace(/\\\|/g, '|'));
    while (i < lines.length) {
      const L = lines[i];
      if (/^\|/.test(L)) { const rows = []; while (i < lines.length && /^\|/.test(lines[i])) rows.push(lines[i++]); const head = cells(rows[0]), body = rows.slice(2).map(cells);
        out += `<div class="table-wrap"><table><thead><tr>${head.map((c) => `<th>${inline(c)}</th>`).join('')}</tr></thead><tbody>${body.map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`; continue; }
      if (/^#{1,3} /.test(L)) { const n = L.match(/^#+/)[0].length; out += `<h${n}>${inline(L.replace(/^#+ /, ''))}</h${n}>`; i++; continue; }
      if (/^> /.test(L)) { out += `<blockquote>${inline(L.slice(2))}</blockquote>`; i++; continue; }
      if (/^- /.test(L)) { out += '<ul>'; while (i < lines.length && /^- /.test(lines[i])) out += `<li>${inline(lines[i++].slice(2))}</li>`; out += '</ul>'; continue; }
      if (!L.trim()) { i++; continue; }
      out += `<p>${inline(L)}</p>`; i++;
    }
    return out;
  }
  VIEWS.report = {
    async mount(h) {
      h.innerHTML = '<div class="empty"><p>Loading…</p></div>';
      try { S.report = await api(`/api/report?db=${S.dataset}`); } catch (e) { h.innerHTML = `<div class="empty"><p class="err">${esc(e.message)}</p></div>`; return; }
      if (S.tab !== 'report') return;
      const r = S.report;
      if (!r.exists) { h.innerHTML = `<div class="empty"><h2>No report yet</h2><p>The report is the set of charts and the written summary the pipeline builds after loading the data.</p><button class="pill primary" data-act="eda">Build the report</button></div>`; return; }
      h.innerHTML = `<div class="toolbar"><button class="pill" data-act="eda">Rebuild report</button><span class="count">Generated ${esc(ago(new Date(r.mtime * 1000).toISOString()))}. Click a chart to enlarge.</span></div>
        <div class="report-fig">${r.figures.map((f) => `<button data-img="${esc(f.url)}" aria-label="Enlarge ${esc(f.name)}"><img src="${esc(f.url)}" alt="${esc(f.name.replace(/^\d+_|\.png$/g, '').replace(/_/g, ' '))}" loading="lazy"></button>`).join('')}</div>
        <div class="md" style="margin-top:22px">${md(r.markdown)}</div>`;
    },
    update() {},
  };

  /* --- log --- */
  VIEWS.log = {
    mount(h) {
      h.innerHTML = `<div class="toolbar"><span class="count" id="logInfo"></span></div><div class="console" id="console" role="log" aria-live="off"></div><p class="note">Everything the pipeline reports while it runs appears here. Warnings (amber) are things worth a look; they do not stop the run.</p>`;
      const c = $('#console'); c.innerHTML = S.log.length ? S.log.map(logLine).join('') : '<span class="t">Nothing has run in this session yet. Press the power button on the Pipeline panel.</span>';
      c.scrollTop = c.scrollHeight; this.update();
    },
    update() { const j = S.job, el = $('#logInfo'); if (el) el.textContent = j ? `${j.kind.toUpperCase()} on ${j.params && j.params.dataset === 'live' ? 'live' : 'demo'} data: ${j.status}${j.error ? ' - ' + j.error : ''}` : ''; },
  };
  const logLine = (l) => `<div class="${esc(l.lvl)}"><span class="t">${esc(l.t)}</span>${esc(l.msg)}</div>`;

  /* ----------------------------------------------------------- tabs & mount */
  const TABS = [['explore', 'Explore'], ['board', 'Leaderboard'], ['compare', 'Compare'], ['corr', 'Correlations'], ['trends', 'Trends'], ['quality', 'Data quality'], ['sql', 'SQL'], ['report', 'Report'], ['log', 'Log']];
  let view = null;
  function paintTabs() {
    $('#tabs').innerHTML = TABS.map(([k, t]) => `<button class="tab" role="tab" id="tab-${k}" data-tab="${k}" aria-selected="${S.tab === k}" tabindex="${S.tab === k ? 0 : -1}">${t}</button>`).join('');
  }
  function emptyCard() {
    return S.dataset === 'sample'
      ? `<div class="empty"><h2>No demo data yet</h2><p>The demo uses a synthetic dataset (invented models) so you can try every control without any account or internet. It takes a few seconds.</p><button class="pill primary" data-act="demo">Load demo data</button></div>`
      : `<div class="empty"><h2>No live data yet</h2><p>Live mode pulls real benchmarks from Artificial Analysis, prices from OpenRouter and sizes from Hugging Face. You need a free Artificial Analysis key: open <b>ADV</b> on the Pipeline panel, paste it, then press the power button.</p><div class="btns" style="display:flex;gap:10px"><button class="pill primary" data-act="adv">Open ADV and add my key</button><button class="pill" data-act="to-demo">Use the demo data instead</button></div></div>`;
  }
  function mountView() {
    hideTip();
    const host = hostOf(), needs = !['log', 'sql', 'report'].includes(S.tab);
    if (needs && !S.models.length) { host.innerHTML = emptyCard(); view = null; return; }
    view = VIEWS[S.tab]; view.mount(host); if (view.update) view.update();
  }
  function setTab(k, keep) { S.tab = k; store.set('mc.tab', k); paintTabs(); if (!keep) mountView(); }

  /* -------------------------------------------------------------- pipeline */
  async function refreshStatus() { S.status = await api('/api/status'); paintKeys(); paintRunStatus(); paintStepper(); }
  async function loadData() {
    try { const d = await api(`/api/models?db=${S.dataset}`); S.models = prep(d.models); S.run = d.run; } catch (e) { if (e.status === 404) { S.models = []; S.run = null; } else throw e; }
    S.quality = null; S.report = null;
    computeExt(); buildPct(); paintCreators(); paintBadge(); paintRunStatus(); syncControls(); recompute(); mountView();
  }
  async function submitKeys(quiet) {
    const body = {}; if ($('#keyAA').value.trim()) body.aa_api_key = $('#keyAA').value.trim(); if ($('#keyHF').value.trim()) body.hf_token = $('#keyHF').value.trim();
    if (!Object.keys(body).length) return false;
    await api('/api/keys', body); $('#keyAA').value = ''; $('#keyHF').value = ''; await refreshStatus();
    if (!quiet) toast('Key stored for this session.');
    return true;
  }
  async function startRun(kind) {
    if (S.job && (S.job.status === 'running' || S.job.status === 'queued')) { toast('A run is already in progress.'); return; }
    try { await submitKeys(true); } catch (e) { toast('Could not store the key: ' + e.message, true); return; }
    const body = { kind: kind === 'all' && !C.withEda ? 'etl' : kind, dataset: S.dataset, use_cache: C.useCache, skip_hf: C.skipHf, fuzzy_threshold: +(0.8 + 0.2 * C.fuzzy).toFixed(3), max_reject_fraction: +(C.tol * 0.5).toFixed(3) };
    try { S.job = await api('/api/run', body); } catch (e) { toast(e.message, true); return; }
    if (S.dataset === 'live' && kind !== 'eda' && !(S.status && S.status.keys.aa) && !C.useCache) toast('No Artificial Analysis key is set, so this live run will stop at the first step.', true);
    S.log = []; S.logNext = 0; setTab('log'); paintStepper(); paintRunStatus(); poll();
  }
  function poll() {
    clearTimeout(S.timer);
    const tick = async () => {
      let j;
      try { j = await api(`/api/jobs/${S.job.id}?since=${S.logNext}`); } catch (e) { toast('Lost contact with the console. The launcher window may have been closed. Restart it and reload this page.', true); return; }
      S.logNext = j.next;
      if (j.lines.length) { S.log.push(...j.lines); const c = $('#console'); if (c) { const stick = c.scrollTop + c.clientHeight > c.scrollHeight - 30; c.insertAdjacentHTML('beforeend', j.lines.map(logLine).join('')); if (stick) c.scrollTop = c.scrollHeight; } }
      S.job = j; paintStepper(); paintRunStatus(); if (view && view.update && S.tab === 'log') view.update();
      if (j.status === 'running' || j.status === 'queued') S.timer = setTimeout(tick, 550); else finishRun(j);
    };
    tick();
  }
  async function finishRun(j) {
    await refreshStatus();
    if (j.status === 'done') {
      S.dataset = j.params.dataset; store.set('mc.dataset', S.dataset); paintDataset();
      await loadData(); toast(j.result.n_valid != null ? `Done: ${j.result.n_valid} models loaded, ${j.result.n_rejected} rejected.` : 'Report rebuilt.');
      if (j.kind !== 'eda') setTab('explore'); else if (S.tab === 'log') setTab('report');
    } else {
      toast((j.error || 'The run failed.').slice(0, 400), true); setTab('log');
      if (/AA_API_KEY|key|HTTP 40|connect|proxy|certificate/i.test(j.error || '')) { setAdvLeft(true); setTimeout(() => $('#keyAA').focus(), 100); }
    }
  }
  const paintDataset = () => { $$('#dsSeg button').forEach((b) => b.setAttribute('aria-checked', String(b.dataset.v === S.dataset))); };

  /* ------------------------------------------------------------------ scenes */
  const SCENES = {
    balanced: { name: 'Balanced', ctl: { wq: 0.8, wc: 0.6, ws: 0.3, wz: 0.2, mode: 'weighted', rankerOn: true, missing: 'neutral' }, reset: true },
    value: { name: 'Best value', ctl: { wq: 0.65, wc: 1, ws: 0.2, wz: 0.1, mode: 'weighted' }, reset: true },
    quality: { name: 'Quality first', ctl: { wq: 1, wc: 0.1, ws: 0.1, wz: 0, mode: 'weighted' }, reset: true },
    small: { name: 'Small and open', ctl: { wq: 0.7, wc: 0.3, ws: 0.2, wz: 1, mode: 'weighted', openness: 'open', sizeMax: 0.75 }, reset: true },
    fast: { name: 'Fast and cheap', ctl: { wq: 0.4, wc: 0.8, ws: 1, wz: 0.1, mode: 'weighted' }, reset: true },
  };
  const FILTER_KEYS = ['sizeMin', 'sizeMax', 'sizeLink', 'minScore', 'maxPrice', 'since', 'openness', 'creators', 'search', 'complete', 'noReasoning', 'keepUnknownSize'];
  function applyScene(sc) {
    if (sc.reset) { const d = DEFAULTS(); FILTER_KEYS.forEach((k) => { C[k] = d[k]; }); }
    Object.assign(C, JSON.parse(JSON.stringify(sc.ctl)));
    $('#searchBox').value = C.search; buildPct(); computeExt(); paintCreators(); syncControls(); schedule();
  }
  function paintScenes() {
    const user = store.get('mc.scenes') || [];
    $('#scene').innerHTML = '<option value="">Scene…</option>' + Object.entries(SCENES).map(([k, s]) => `<option value="b:${k}">${esc(s.name)}</option>`).join('') + user.map((s, i) => `<option value="u:${i}">★ ${esc(s.name)}</option>`).join('');
  }

  /* -------------------------------------------------------------------- init */
  let setAdvLeft = () => {};
  function init() {
    $$('[data-knob]').forEach((h) => makeKnob(h, h.dataset.knob));
    $$('[data-fader]').forEach((h) => makeFader(h, h.dataset.fader));
    bindPower($('#rankPower'), () => C.rankerOn, (v) => { C.rankerOn = v; }, $('#pMid'));
    bindPower($('#filtPower'), () => C.filtersOn, (v) => { C.filtersOn = v; }, $('#pRight'));
    bindSeg($('#modeSeg'), () => C.mode, (v) => { C.mode = v; });
    bindSeg($('#missSeg'), () => C.missing, (v) => { C.missing = v; });
    bindSeg($('#openSeg'), () => C.openness, (v) => { C.openness = v; });
    bindSwitch($('#swCache'), () => C.useCache, (v) => { C.useCache = v; });
    bindSwitch($('#swSkipHf'), () => C.skipHf, (v) => { C.skipHf = v; });
    bindSwitch($('#swEda'), () => C.withEda, (v) => { C.withEda = v; });
    bindSwitch($('#swComplete'), () => C.complete, (v) => { C.complete = v; });
    bindSwitch($('#swNoReason'), () => C.noReasoning, (v) => { C.noReasoning = v; });
    bindSwitch($('#swKeepSize'), () => C.keepUnknownSize, (v) => { C.keepUnknownSize = v; });
    setAdvLeft = bindAdv($('#advLeftSw'), $('#advLeft'), 'left'); bindAdv($('#advMidSw'), $('#advMid'), 'mid'); bindAdv($('#advRightSw'), $('#advRight'), 'right');
    const link = $('#linkBtn'); const paintLink = () => link.setAttribute('aria-pressed', String(!!C.sizeLink));
    link.onclick = () => { C.sizeLink = !C.sizeLink; paintLink(); saveCtl(); }; paints.push(paintLink); paintLink();

    $('#scoreSel').onchange = (e) => { C.scoreKey = e.target.value; computeExt(); buildPct(); paintCreators(); syncControls(); schedule(); };
    $('#topNSel').onchange = (e) => { C.topN = +e.target.value; schedule(); };
    paints.push(() => { $('#topNSel').value = String(C.topN); if ($('#scoreSel').value !== C.scoreKey) $('#scoreSel').value = C.scoreKey; });
    $('#searchBox').value = C.search;
    $('#searchBox').oninput = (e) => { C.search = e.target.value; schedule(); };
    $('#creatorChips').onclick = (e) => { const b = e.target.closest('[data-c]'); if (!b) return; const n = b.dataset.c; C.creators = C.creators.includes(n) ? C.creators.filter((x) => x !== n) : [...C.creators, n]; b.setAttribute('aria-pressed', String(C.creators.includes(n))); schedule(); };
    $('#resetFilters').onclick = () => { const d = DEFAULTS(); FILTER_KEYS.forEach((k) => { C[k] = d[k]; }); C.filtersOn = true; $('#searchBox').value = ''; paintCreators(); syncControls(); schedule(); };

    $('#runBtn').onclick = () => startRun('all');
    $('#runEtlOnly').onclick = () => startRun('etl');
    $('#runEdaOnly').onclick = () => startRun('eda');
    bindSeg($('#dsSeg'), () => S.dataset, (v) => { S.dataset = v; store.set('mc.dataset', v); S.job = null; loadData().then(() => { paintStepper(); }); }, null);
    $('#saveKeys').onclick = async () => {
      try { if (!(await submitKeys(false))) toast('Paste a key first.', true); } catch (e) { toast(e.message, true); }
    };
    $('#testConn').onclick = async () => {
      const out = $('#connResult'), btn = $('#testConn');
      btn.disabled = true; out.innerHTML = '<span class="hint">Testing the three sources…</span>';
      try {
        await submitKeys(true);
        const r = await api('/api/diagnose', {});
        out.innerHTML = r.results.map((x) => `<div class="conn-row ${x.ok ? 'ok' : 'bad'}"><b>${x.ok ? '✓' : '✗'} ${esc(x.source)}</b><span>${esc(x.message)}</span></div>`).join('');
      } catch (e) { out.innerHTML = `<div class="conn-row bad"><b>✗ Console server</b><span>${esc('Could not reach the local console: ' + e.message + '. Is the black launcher window still open?')}</span></div>`; }
      btn.disabled = false;
    };
    $('#forgetKeys').onclick = async () => { try { await api('/api/keys', { aa_api_key: '', hf_token: '' }); await refreshStatus(); toast('Keys forgotten.'); } catch (e) { toast(e.message, true); } };

    $('#tabs').onclick = (e) => { const b = e.target.closest('[data-tab]'); if (b) setTab(b.dataset.tab); };
    $('#tabs').onkeydown = (e) => { const i = TABS.findIndex((t) => t[0] === S.tab); if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') { setTab(TABS[(i + (e.key === 'ArrowRight' ? 1 : TABS.length - 1)) % TABS.length][0]); $('#tab-' + S.tab).focus(); } };
    $('#view').addEventListener('click', (e) => {
      const a = e.target.closest('[data-act]'), img = e.target.closest('[data-img]');
      if (img) { $('#lightbox img').src = img.dataset.img; $('#lightbox').hidden = false; }
      if (!a) return;
      if (a.dataset.act === 'demo') { S.dataset = 'sample'; store.set('mc.dataset', 'sample'); paintDataset(); startRun('all'); }
      if (a.dataset.act === 'eda') startRun('eda');
      if (a.dataset.act === 'to-demo') { S.dataset = 'sample'; store.set('mc.dataset', 'sample'); paintDataset(); loadData(); }
      if (a.dataset.act === 'adv') { setAdvLeft(true); $('#keyAA').focus(); }
    });
    $('#lightbox').onclick = () => { $('#lightbox').hidden = true; };
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') $('#lightbox').hidden = true; });

    paintScenes();
    $('#scene').onchange = (e) => { const v = e.target.value; if (!v) return; const [t, k] = v.split(':'); applyScene(t === 'b' ? SCENES[k] : (store.get('mc.scenes') || [])[+k]); e.target.value = ''; toast('Scene applied.'); };
    $('#saveScene').onclick = () => {
      const name = (window.prompt('Name this scene (it stores the position of every control):') || '').trim().slice(0, 40); if (!name) return;
      const keep = Object.assign({}, C); delete keep.adv; const list = store.get('mc.scenes') || []; list.push({ name, ctl: JSON.parse(JSON.stringify(keep)) }); store.set('mc.scenes', list); paintScenes(); toast(`Saved scene "${name}".`);
    };
    const themes = ['auto', 'light', 'dark']; let theme = store.get('mc.theme') || 'auto';
    const applyTheme = () => { if (theme === 'auto') delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = theme; $('#themeLbl').textContent = theme[0].toUpperCase() + theme.slice(1); };
    $('#themeBtn').onclick = () => { theme = themes[(themes.indexOf(theme) + 1) % 3]; store.set('mc.theme', theme); applyTheme(); };
    applyTheme();

    paintTabs(); paintDataset(); paintStepper();
    boot();
  }
  async function boot() {
    try { await refreshStatus(); } catch (e) { toast('Cannot reach the console server. Make sure the launcher window is still open, then reload this page.', true); return; }
    const ds = S.status.datasets;
    if (!ds[S.dataset].exists && !ds[S.dataset === 'sample' ? 'live' : 'sample'].exists) S.dataset = S.dataset === 'live' ? 'live' : 'sample';
    else if (!ds[S.dataset].exists && S.dataset === 'sample') S.dataset = 'live';
    paintDataset();
    const sel = $('#scoreSel');
    await loadData();
    const has = SCORE_OPTS.filter(([k]) => S.models.filter((m) => isNum(m[k])).length >= 5);
    sel.innerHTML = (has.length ? has : SCORE_OPTS).map(([k, t]) => `<option value="${k}">${esc(t)}</option>`).join(''); sel.value = C.scoreKey;
    if (sel.value !== C.scoreKey) { C.scoreKey = sel.value || 'intelligence_index'; computeExt(); buildPct(); syncControls(); recompute(); }
    if (S.status.job && (S.status.job.status === 'running' || S.status.job.status === 'queued')) { S.job = S.status.job; S.logNext = 0; poll(); }
    $('#runInfo').textContent = 'Local console · nothing leaves your machine except the pipeline\'s own API calls';
  }
  init();
})();
