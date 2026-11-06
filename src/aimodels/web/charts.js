/* Dependency-free SVG charts. Colours come from CSS classes/tokens in app.css,
   so light/dark switching never needs a redraw. All text is escaped. */
const Charts = (() => {
  'use strict';
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
  const lin = (d0, d1, r0, r1) => { const k = (r1 - r0) / ((d1 - d0) || 1); return (v) => r0 + (v - d0) * k; };
  const logs = (d0, d1, r0, r1) => { const a = Math.log10(d0), k = (r1 - r0) / ((Math.log10(d1) - a) || 1); return (v) => r0 + (Math.log10(v) - a) * k; };

  function niceStep(span, n) {
    const raw = span / n, p = Math.pow(10, Math.floor(Math.log10(raw))), f = raw / p;
    return (f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10) * p;
  }
  function linTicks(lo, hi, n = 6) {
    const s = niceStep(hi - lo, n), out = [];
    for (let v = Math.ceil(lo / s) * s; v <= hi + s * 1e-6; v += s) out.push(+v.toPrecision(12));
    return out;
  }
  function logTicks(lo, hi) {
    const a = Math.floor(Math.log10(lo)), b = Math.ceil(Math.log10(hi)), dec = b - a;
    const mults = dec > 4 ? [1] : dec > 1 ? [1, 3] : [1, 2, 5], out = [];
    for (let e = a; e <= b; e++) for (const m of mults) { const v = +(m * 10 ** e).toPrecision(12); if (v >= lo * 0.999 && v <= hi * 1.001) out.push(v); }
    return out.length >= 2 ? out : [lo, hi];
  }
  function dateTicks(lo, hi) {
    const y0 = new Date(lo).getUTCFullYear(), y1 = new Date(hi).getUTCFullYear(), out = [], quarterly = (hi - lo) < 2.2 * 365 * 864e5;
    for (let y = y0; y <= y1 + 1; y++) for (const m of quarterly ? [0, 3, 6, 9] : [0]) {
      const t = Date.UTC(y, m, 1);
      if (t >= lo && t <= hi) out.push({ v: t, label: quarterly ? `${y}-${String(m + 1).padStart(2, '0')}` : String(y) });
    }
    return out;
  }
  const ext = (a) => [Math.min(...a), Math.max(...a)];

  const fmt = {
    price: (v) => (v == null ? '-' : v === 0 ? '$0' : v < 0.01 ? '$' + v.toPrecision(2) : v < 1 ? '$' + v.toFixed(2) : v < 100 ? '$' + (+v.toFixed(1)) : '$' + Math.round(v)),
    params: (b) => (b == null ? '-' : b < 1 ? Math.round(b * 1000) + 'M' : (b < 10 ? +b.toFixed(1) : Math.round(b)) + 'B'),
    num: (v, d = 1) => (v == null ? '-' : +v.toFixed(d) + ''),
    ctx: (n) => (n == null ? '-' : n >= 1e6 ? +(n / 1e6).toFixed(1) + 'M' : n >= 1000 ? Math.round(n / 1000) + 'K' : String(n)),
    date: (t) => (t == null ? '-' : new Date(t).toISOString().slice(0, 10)),
    pct: (v) => (v == null ? '-' : Math.round(v * 100) + '%'),
  };

  const ramp = (() => {
    const stops = ['#9ec5f4', '#6da7ec', '#3987e5', '#256abf', '#184f95', '#0d366b'].map((h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16)));
    return (t) => {
      t = Math.min(1, Math.max(0, t)); const x = t * (stops.length - 1), i = Math.min(stops.length - 2, Math.floor(x)), f = x - i;
      return 'rgb(' + stops[i].map((c, k) => Math.round(c + (stops[i + 1][k] - c) * f)).join(',') + ')';
    };
  })();

  const empty = (msg) => `<div class="empty"><p>${esc(msg)}</p></div>`;

  /* Non-dominated points for two metrics; `hiX`/`hiY` say whether larger is better. */
  function pareto2(pts, hiX, hiY) {
    const sx = hiX ? -1 : 1, sy = hiY ? 1 : -1;  // sort best-x first, keep points that improve y
    const order = pts.slice().sort((a, b) => sx * (a.x - b.x) || sy * (b.y - a.y));
    const out = []; let best = -Infinity;
    for (const p of order) { const y = hiY ? p.y : -p.y; if (y > best) { out.push(p); best = y; } }
    return out;
  }

  /* o: {w,h,pts:[{x,y,i,cls,fill,r}], xLog,yLog,xDate, xLabel,yLabel, xFmt,yFmt, lines:[{pts,cls,step}],
         front:[{x,y}], rings:[{x,y,cls}], labels:[{x,y,text}], title} */
  function scatter(o) {
    const W = o.w || 900, H = o.h || 520, m = { l: 66, r: 20, t: 14, b: 50 };
    const pts = (o.pts || []).filter((p) => isNum(p.x) && isNum(p.y) && (!o.xLog || p.x > 0) && (!o.yLog || p.y > 0));
    if (!pts.length) return empty('No models have both values under the current filters. Loosen a knob or pick other axes.');
    let [x0, x1] = ext(pts.map((p) => p.x)), [y0, y1] = ext(pts.map((p) => p.y));
    const pad = (lo, hi, log) => (log ? [lo / 1.18, hi * 1.18] : [lo - (hi - lo || 1) * 0.05, hi + (hi - lo || 1) * 0.05]);
    [x0, x1] = pad(x0, x1, o.xLog); [y0, y1] = pad(y0, y1, o.yLog);
    const sx = (o.xLog ? logs : lin)(x0, x1, m.l, W - m.r), sy = (o.yLog ? logs : lin)(y0, y1, H - m.b, m.t);
    const xt = o.xDate ? dateTicks(x0, x1) : (o.xLog ? logTicks(x0, x1) : linTicks(x0, x1, 7)).map((v) => ({ v, label: (o.xFmt || String)(v) }));
    const yt = (o.yLog ? logTicks(y0, y1) : linTicks(y0, y1, 6)).map((v) => ({ v, label: (o.yFmt || String)(v) }));
    let s = `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(o.title || 'Scatter chart')}">`;
    for (const t of yt) s += `<line class="gl" x1="${m.l}" x2="${W - m.r}" y1="${sy(t.v).toFixed(1)}" y2="${sy(t.v).toFixed(1)}"/><text class="ax" x="${m.l - 8}" y="${(sy(t.v) + 4).toFixed(1)}" text-anchor="end">${esc(t.label)}</text>`;
    for (const t of xt) s += `<line class="gl" y1="${m.t}" y2="${H - m.b}" x1="${sx(t.v).toFixed(1)}" x2="${sx(t.v).toFixed(1)}"/><text class="ax" y="${H - m.b + 18}" x="${sx(t.v).toFixed(1)}" text-anchor="middle">${esc(t.label)}</text>`;
    s += `<line class="axis" x1="${m.l}" x2="${W - m.r}" y1="${H - m.b}" y2="${H - m.b}"/><line class="axis" x1="${m.l}" x2="${m.l}" y1="${m.t}" y2="${H - m.b}"/>`;
    s += `<text class="axt" x="${(m.l + W - m.r) / 2}" y="${H - 8}" text-anchor="middle">${esc(o.xLabel || '')}</text>`;
    s += `<text class="axt" transform="translate(16 ${(m.t + H - m.b) / 2}) rotate(-90)" text-anchor="middle">${esc(o.yLabel || '')}</text>`;
    const path = (arr, step) => {
      const a = arr.filter((p) => isNum(p.x) && isNum(p.y) && (!o.xLog || p.x > 0) && (!o.yLog || p.y > 0)).sort((p, q) => p.x - q.x);
      let d = '';
      a.forEach((p, k) => { const X = sx(p.x).toFixed(1), Y = sy(p.y).toFixed(1); d += k === 0 ? `M${X} ${Y}` : step ? `H${X}V${Y}` : `L${X} ${Y}`; });
      return d;
    };
    if (o.front && o.front.length > 1) s += `<path class="front" d="${path(o.front, true)}"/>`;
    for (const l of o.lines || []) if (l.pts.length > 1) s += `<path class="${l.cls}" d="${path(l.pts, l.step)}"/>`;
    for (const p of pts) {
      const style = p.fill ? ` style="fill:${p.fill}"` : '';
      s += `<circle class="pt ${p.cls || ''}" data-i="${p.i}" cx="${sx(p.x).toFixed(1)}" cy="${sy(p.y).toFixed(1)}" r="${p.r || 5}"${style}/>`;
    }
    for (const r of o.rings || []) if (isNum(r.x) && isNum(r.y) && (!o.xLog || r.x > 0) && (!o.yLog || r.y > 0))
      s += `<circle class="ring ${r.cls || ''}" cx="${sx(r.x).toFixed(1)}" cy="${sy(r.y).toFixed(1)}" r="${(r.r || 5) + 4}"/>`;
    const placed = [];
    for (const l of o.labels || []) {
      if (!isNum(l.x) || !isNum(l.y) || (o.xLog && l.x <= 0) || (o.yLog && l.y <= 0)) continue;
      const px = sx(l.x), py = sy(l.y), w = l.text.length * 6.4, flip = px + w + 14 > W - m.r;
      const box = { x0: flip ? px - w - 10 : px + 9, x1: flip ? px - 9 : px + w + 10, y0: py - 15, y1: py + 3 };
      if (placed.some((b) => box.x0 < b.x1 && box.x1 > b.x0 && box.y0 < b.y1 && box.y1 > b.y0)) continue;
      placed.push(box);
      s += `<text class="lab" x="${flip ? px - 10 : px + 10}" y="${py - 5}" text-anchor="${flip ? 'end' : 'start'}">${esc(l.text)}</text>`;
    }
    return s + '</svg>';
  }

  /* The little display in the middle panel: grey density area + orange response curve. */
  function miniCurve(o) {
    const W = 300, H = 150, m = { l: 26, r: 8, t: 8, b: 20 }, n = o.density.length;
    const X = (k) => m.l + (k / (n - 1)) * (W - m.l - m.r), Y = (v) => H - m.b - v * (H - m.t - m.b);
    let s = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(o.label)}">`;
    (o.yLabels || []).forEach((t) => { s += `<line class="sgl dash" x1="${m.l}" x2="${W - m.r}" y1="${Y(t.v)}" y2="${Y(t.v)}"/><text x="${m.l - 4}" y="${Y(t.v) + 3}" text-anchor="end">${esc(t.label)}</text>`; });
    (o.xLabels || []).forEach((t) => { const x = m.l + t.v * (W - m.l - m.r); s += `<line class="sgl" x1="${x}" x2="${x}" y1="${m.t}" y2="${H - m.b}"/><text x="${x}" y="${H - 6}" text-anchor="middle">${esc(t.label)}</text>`; });
    let d = `M${X(0)} ${Y(0)}`; o.density.forEach((v, k) => { d += `L${X(k).toFixed(1)} ${Y(v).toFixed(1)}`; }); d += `L${X(n - 1)} ${Y(0)}Z`;
    s += `<path class="area" d="${d}"/>`;
    let c = '', pen = false;
    o.curve.forEach((v, k) => { if (v == null) { pen = false; return; } c += `${pen ? 'L' : 'M'}${X(k).toFixed(1)} ${Y(v).toFixed(1)}`; pen = true; });
    return s + `<path class="curve" d="${c}"/></svg>`;
  }

  function heatmap(o) {
    const k = o.labels.length, cell = Math.min(58, 560 / k), L = 120, T = 10, B = 96, W = L + cell * k + 14, H = T + cell * k + B;
    let s = `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(o.title)}">`;
    for (let i = 0; i < k; i++) {
      s += `<text class="ax" x="${L - 8}" y="${T + i * cell + cell / 2 + 4}" text-anchor="end">${esc(o.labels[i])}</text>`;
      s += `<text class="ax" transform="translate(${L + i * cell + cell / 2} ${T + k * cell + 12}) rotate(-40)" text-anchor="end">${esc(o.labels[i])}</text>`;
      for (let j = 0; j < k; j++) {
        const v = o.matrix[i][j];
        if (!isNum(v)) { s += `<rect x="${L + j * cell}" y="${T + i * cell}" width="${cell - 2}" height="${cell - 2}" rx="5" fill="none" stroke="var(--line)"/>`; continue; }
        const t = (v - o.vmin) / (1 - o.vmin);
        s += `<rect x="${L + j * cell}" y="${T + i * cell}" width="${cell - 2}" height="${cell - 2}" rx="5" fill="${ramp(t)}"><title>${esc(o.labels[i])} vs ${esc(o.labels[j])}: ${v.toFixed(2)} (n=${o.counts[i][j]})</title></rect>`;
        s += `<text class="cell-t" x="${L + j * cell + (cell - 2) / 2}" y="${T + i * cell + cell / 2 + 4}" text-anchor="middle" fill="${t > 0.5 ? '#fff' : '#0b0b0b'}">${v.toFixed(2)}</text>`;
      }
    }
    return s + '</svg>';
  }

  function radar(o) {
    const S = 360, c = S / 2, R = 118, n = o.axes.length;
    const pt = (i, v) => { const a = -Math.PI / 2 + (i * 2 * Math.PI) / n; return [c + Math.cos(a) * R * v, c + Math.sin(a) * R * v]; };
    let s = `<svg class="chart" viewBox="0 0 ${S} ${S}" role="img" aria-label="${esc(o.title)}">`;
    for (const r of [0.25, 0.5, 0.75, 1]) s += `<polygon fill="none" stroke="var(--grid)" points="${o.axes.map((_, i) => pt(i, r).join(',')).join(' ')}"/>`;
    o.axes.forEach((a, i) => { const [x, y] = pt(i, 1), [lx, ly] = pt(i, 1.2); s += `<line class="gl" x1="${c}" y1="${c}" x2="${x}" y2="${y}"/><text class="ax" x="${lx}" y="${ly + 4}" text-anchor="${Math.abs(lx - c) < 8 ? 'middle' : lx > c ? 'start' : 'end'}">${esc(a)}</text>`; });
    for (const se of o.series) {
      const p = se.values.map((v, i) => pt(i, v == null ? 0 : v));
      s += `<polygon class="poly" fill="${se.color}" stroke="${se.color}" points="${p.map((q) => q.join(',')).join(' ')}"/>`;
      p.forEach((q, i) => { s += `<circle cx="${q[0]}" cy="${q[1]}" r="3.4" fill="${se.color}"><title>${esc(se.name)} - ${esc(o.axes[i])}: ${se.values[i] == null ? 'n/a' : Math.round(se.values[i] * 100) + 'th percentile'}</title></circle>`; });
    }
    return s + '</svg>';
  }

  return { esc, isNum, fmt, ramp, scatter, miniCurve, heatmap, radar, pareto2, empty, logTicks, linTicks };
})();
