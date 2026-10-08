/* Minimal canvas charts: line/area, bars, candlesticks with trade markers. No dependencies. */
(function () {
  'use strict';
  const FONT = '11.5px ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';
  const tip = document.getElementById('tip');
  const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

  function niceStep(range, n) {
    const raw = range / n, mag = Math.pow(10, Math.floor(Math.log10(raw))), f = raw / mag;
    return (f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10) * mag;
  }
  function ticks(min, max, n) {
    const step = niceStep(max - min || 1, n), out = [];
    for (let v = Math.ceil(min / step) * step; v <= max + step * 1e-6; v += step) out.push(+v.toFixed(10));
    return out;
  }
  function timeLabel(ms, spanMs) {
    const d = new Date(ms), p = (x) => String(x).padStart(2, '0');
    return spanMs > 2 * 86400e3 ? `${p(d.getMonth() + 1)}-${p(d.getDate())}` : `${p(d.getHours())}:${p(d.getMinutes())}`;
  }
  function fullTime(ms) {
    const d = new Date(ms), p = (x) => String(x).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  /* Wraps a canvas: handles DPR, resize, pointer hover and tooltip. makeModel(data,w,h) -> {draw,hit,tip} */
  function chart(canvas, makeModel) {
    let data = null, model = null, hover = -1;
    function render() {
      const r = canvas.getBoundingClientRect();
      if (!r.width || !r.height) return;
      const dpr = window.devicePixelRatio || 1;
      const W = Math.round(r.width * dpr), H = Math.round(r.height * dpr);
      if (canvas.width !== W || canvas.height !== H) { canvas.width = W; canvas.height = H; }
      const g = canvas.getContext('2d');
      g.setTransform(dpr, 0, 0, dpr, 0, 0);
      g.clearRect(0, 0, r.width, r.height);
      g.font = FONT;
      model = data ? makeModel(data, r.width, r.height) : null;
      if (model) model.draw(g, hover);
    }
    canvas.addEventListener('pointermove', (e) => {
      if (!model) return;
      const r = canvas.getBoundingClientRect();
      const i = model.hit(e.clientX - r.left, e.clientY - r.top);
      if (i !== hover) { hover = i; render(); }
      if (i >= 0) {
        tip.hidden = false;
        tip.textContent = model.tip(i);
        const tw = tip.offsetWidth, th = tip.offsetHeight;
        let x = e.clientX + 14, y = e.clientY - th - 10;
        if (x + tw > window.innerWidth - 8) x = e.clientX - tw - 14;
        if (y < 8) y = e.clientY + 16;
        tip.style.left = x + 'px';
        tip.style.top = y + 'px';
      } else tip.hidden = true;
    });
    canvas.addEventListener('pointerleave', () => { hover = -1; tip.hidden = true; render(); });
    if (window.ResizeObserver) new ResizeObserver(render).observe(canvas);
    return { set(d) { data = d; hover = -1; render(); }, render };
  }

  function axes(g, w, h, pad, yMin, yMax, fmtY, xTicks) {
    g.strokeStyle = css('--line'); g.fillStyle = css('--faint'); g.lineWidth = 1;
    g.textAlign = 'right'; g.textBaseline = 'middle';
    const yOf = (v) => pad.t + (1 - (v - yMin) / (yMax - yMin)) * (h - pad.t - pad.b);
    for (const v of ticks(yMin, yMax, 4)) {
      const y = Math.round(yOf(v)) + .5;
      g.beginPath(); g.moveTo(pad.l, y); g.lineTo(w - pad.r, y); g.stroke();
      g.fillText(fmtY(v), pad.l - 8, y);
    }
    g.textAlign = 'center'; g.textBaseline = 'top';
    for (const t of xTicks) g.fillText(t.text, t.x, h - pad.b + 7);
    return yOf;
  }

  /* ---------- line / area ---------- */
  function line(canvas, opts) {
    opts = opts || {};
    const fmtY = opts.fmt || ((v) => String(v));
    return chart(canvas, (pts, w, h) => {
      const pad = { l: opts.padLeft || 62, r: 14, t: 12, b: 26 };
      const t0 = pts[0].t, t1 = pts[pts.length - 1].t, span = Math.max(t1 - t0, 1);
      let lo = Math.min(...pts.map((p) => p.v)), hi = Math.max(...pts.map((p) => p.v));
      if (opts.base != null) { lo = Math.min(lo, opts.base); hi = Math.max(hi, opts.base); }
      if (hi - lo < 1e-9) { hi += 1; lo -= 1; }
      const m = (hi - lo) * .08; lo -= m; hi += m;
      const xOf = (t) => pad.l + (pts.length === 1 ? .5 : (t - t0) / span) * (w - pad.l - pad.r);
      const xt = [0, 1, 2, 3].map((i) => ({ x: pad.l + (i / 3) * (w - pad.l - pad.r), text: timeLabel(t0 + (i / 3) * span, span) }));
      const xs = pts.map((p) => xOf(p.t));
      return {
        draw(g, hover) {
          const yOf = axes(g, w, h, pad, lo, hi, fmtY, xt);
          if (opts.base != null) {
            g.save(); g.setLineDash([4, 4]); g.strokeStyle = css('--line-strong');
            const y = Math.round(yOf(opts.base)) + .5; g.beginPath(); g.moveTo(pad.l, y); g.lineTo(w - pad.r, y); g.stroke(); g.restore();
          }
          const accent = css('--accent');
          g.beginPath();
          pts.forEach((p, i) => (i ? g.lineTo(xs[i], yOf(p.v)) : g.moveTo(xs[i], yOf(p.v))));
          g.lineWidth = 2; g.lineJoin = 'round'; g.strokeStyle = accent; g.stroke();
          if (pts.length > 1) {
            g.lineTo(xs[xs.length - 1], h - pad.b); g.lineTo(xs[0], h - pad.b); g.closePath();
            const gr = g.createLinearGradient(0, pad.t, 0, h - pad.b);
            gr.addColorStop(0, 'rgba(123,163,255,.20)'); gr.addColorStop(1, 'rgba(123,163,255,0)');
            g.fillStyle = gr; g.fill();
          }
          const last = pts.length - 1, k = hover >= 0 ? hover : last;
          if (hover >= 0) {
            g.strokeStyle = css('--line-strong'); g.lineWidth = 1;
            g.beginPath(); g.moveTo(Math.round(xs[k]) + .5, pad.t); g.lineTo(Math.round(xs[k]) + .5, h - pad.b); g.stroke();
          }
          g.beginPath(); g.arc(xs[k], yOf(pts[k].v), 4.5, 0, 7);
          g.fillStyle = accent; g.fill(); g.lineWidth = 2; g.strokeStyle = css('--surface'); g.stroke();
        },
        hit(x) { let b = -1, bd = 1e9; xs.forEach((px, i) => { const d = Math.abs(px - x); if (d < bd) { bd = d; b = i; } }); return bd < 40 ? b : -1; },
        tip(i) { return `${fullTime(pts[i].t)}\n${fmtY(pts[i].v, true)}`; },
      };
    });
  }

  /* ---------- bars (positive / negative around zero) ---------- */
  function bars(canvas, opts) {
    opts = opts || {};
    const fmtY = opts.fmt || ((v) => String(v));
    return chart(canvas, (items, w, h) => {
      const pad = { l: opts.padLeft || 62, r: 10, t: 12, b: 26 };
      let lo = Math.min(0, ...items.map((d) => d.v)), hi = Math.max(0, ...items.map((d) => d.v));
      if (hi - lo < 1e-9) hi = lo + 1;
      const m = (hi - lo) * .08; if (lo < 0) lo -= m; hi += m;
      const slot = (w - pad.l - pad.r) / items.length, bw = Math.max(2, Math.min(34, slot - 2));
      const cx = (i) => pad.l + slot * (i + .5);
      const n = items.length, every = Math.max(1, Math.ceil(n / 6));
      const xt = items.map((d, i) => ({ x: cx(i), text: d.label.slice(5), i })).filter((t) => t.i % every === 0);
      return {
        draw(g, hover) {
          const yOf = axes(g, w, h, pad, lo, hi, fmtY, xt);
          const y0 = yOf(0);
          items.forEach((d, i) => {
            const y = yOf(d.v), top = Math.min(y, y0), hgt = Math.max(1, Math.abs(y - y0));
            g.fillStyle = d.v >= 0 ? css('--gain') : css('--loss');
            g.globalAlpha = hover >= 0 && hover !== i ? .55 : 1;
            g.fillRect(Math.round(cx(i) - bw / 2), top, bw, hgt);
          });
          g.globalAlpha = 1;
          g.strokeStyle = css('--line-strong'); g.beginPath(); g.moveTo(pad.l, Math.round(y0) + .5); g.lineTo(w - pad.r, Math.round(y0) + .5); g.stroke();
        },
        hit(x) { const i = Math.floor((x - pad.l) / slot); return i >= 0 && i < items.length ? i : -1; },
        tip(i) { return `${items[i].label}\n${fmtY(items[i].v, true)}`; },
      };
    });
  }

  /* ---------- candlesticks with trade markers ---------- */
  function candles(canvas, opts) {
    opts = opts || {};
    const fmtP = opts.fmt || ((v) => String(v));
    return chart(canvas, (d, w, h) => {
      const cs = d.candles;
      if (!cs.length) return null;
      const pad = { l: opts.padLeft || 70, r: 12, t: 12, b: 26 };
      let lo = Math.min(...cs.map((c) => c[3])), hi = Math.max(...cs.map((c) => c[2]));
      const near = (p) => p > lo - (hi - lo) * .6 && p < hi + (hi - lo) * .6;
      for (const o of d.open) { if (near(o.sl)) { lo = Math.min(lo, o.sl); hi = Math.max(hi, o.sl); } if (near(o.tp)) { lo = Math.min(lo, o.tp); hi = Math.max(hi, o.tp); } }
      const m = (hi - lo) * .06 || 1; lo -= m; hi += m;
      const n = cs.length, slot = (w - pad.l - pad.r) / n, bw = Math.max(1, Math.min(14, slot * .66));
      const cx = (i) => pad.l + slot * (i + .5);
      const idxOf = (t) => (t < cs[0][0] ? -1 : Math.min(n - 1, Math.floor((t - cs[0][0]) / d.tf_ms)));
      const span = cs[n - 1][0] - cs[0][0];
      const xt = [0, 1, 2, 3].map((i) => { const k = Math.round((i / 3) * (n - 1)); return { x: cx(k), text: timeLabel(cs[k][0], span) }; });
      return {
        draw(g, hover) {
          const yOf = axes(g, w, h, pad, lo, hi, fmtP, xt);
          const up = css('--gain'), dn = css('--loss'), bg = css('--surface');
          cs.forEach((c, i) => {
            const col = c[4] >= c[1] ? up : dn, x = cx(i);
            g.strokeStyle = col; g.lineWidth = 1;
            g.beginPath(); g.moveTo(Math.round(x) + .5, yOf(c[2])); g.lineTo(Math.round(x) + .5, yOf(c[3])); g.stroke();
            const yo = yOf(c[1]), yc = yOf(c[4]), top = Math.min(yo, yc), bh = Math.max(1, Math.abs(yo - yc));
            if (c[4] >= c[1]) { g.fillStyle = bg; g.fillRect(x - bw / 2, top, bw, bh); g.strokeRect(Math.round(x - bw / 2) + .5, Math.round(top) + .5, Math.max(1, Math.round(bw) - 1), Math.max(1, Math.round(bh) - 1)); }
            else { g.fillStyle = col; g.fillRect(x - bw / 2, top, bw, bh); }
          });
          for (const t of d.trades) {
            const a = idxOf(t.opened_at), b = idxOf(t.closed_at);
            if (a < 0 || b < 0) continue;
            const win = t.pnl > 0;
            g.save(); g.setLineDash([3, 3]); g.strokeStyle = win ? up : dn; g.lineWidth = 1.2;
            g.beginPath(); g.moveTo(cx(a), yOf(t.entry)); g.lineTo(cx(b), yOf(t.exit)); g.stroke(); g.restore();
            marker(g, cx(a), yOf(t.entry), t.side, css('--text'));
            g.beginPath(); g.arc(cx(b), yOf(t.exit), 4, 0, 7); g.lineWidth = 2; g.strokeStyle = win ? up : dn; g.fillStyle = bg; g.fill(); g.stroke();
          }
          for (const o of d.open) {
            const a = Math.max(0, idxOf(o.opened_at));
            for (const [p, label, col] of [[o.sl, 'Stop', dn], [o.tp, 'Target', up]]) {
              const y = yOf(p); g.save(); g.setLineDash([5, 4]); g.strokeStyle = col; g.lineWidth = 1;
              g.beginPath(); g.moveTo(cx(a), y); g.lineTo(w - pad.r, y); g.stroke(); g.restore();
              g.fillStyle = col; g.textAlign = 'right'; g.textBaseline = 'bottom'; g.fillText(label, w - pad.r - 4, y - 2);
            }
            marker(g, cx(a), yOf(o.entry), o.side, css('--accent'));
          }
          if (hover >= 0) {
            g.strokeStyle = css('--line-strong'); g.lineWidth = 1; g.beginPath();
            g.moveTo(Math.round(cx(hover)) + .5, pad.t); g.lineTo(Math.round(cx(hover)) + .5, h - pad.b); g.stroke();
          }
        },
        hit(x) { const i = Math.floor((x - pad.l) / slot); return i >= 0 && i < n ? i : -1; },
        tip(i) { const c = cs[i]; return `${fullTime(c[0])}\nOpen ${fmtP(c[1])}\nHigh ${fmtP(c[2])}\nLow ${fmtP(c[3])}\nClose ${fmtP(c[4])}`; },
      };
    });
  }
  function marker(g, x, y, side, color) {
    g.fillStyle = color; g.beginPath();
    if (side === 'long') { g.moveTo(x, y - 7); g.lineTo(x - 5.5, y + 3); g.lineTo(x + 5.5, y + 3); }
    else { g.moveTo(x, y + 7); g.lineTo(x - 5.5, y - 3); g.lineTo(x + 5.5, y - 3); }
    g.closePath(); g.fill();
  }

  window.Charts = { line, bars, candles };
})();
