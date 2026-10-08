/* Trading bot admin panel. Plain JS, no dependencies. All dynamic text goes through esc(). */
(function () {
  'use strict';
  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  /* ---------- formatting ---------- */
  const nfs = {};
  const nf = (d) => nfs[d] || (nfs[d] = new Intl.NumberFormat('en-US', { minimumFractionDigits: d, maximumFractionDigits: d }));
  const num = (v) => v != null && !isNaN(v);
  const money = (v, d = 2) => (num(v) ? nf(d).format(v) : '–');
  const signed = (v, d = 2) => (num(v) ? (v > 0.0000001 ? '+' : v < -0.0000001 ? '−' : '') + nf(d).format(Math.abs(v)) : '–');
  const pct = (v, d = 2) => (num(v) ? signed(v, d) + '%' : '–');
  const price = (v) => { if (!num(v)) return '–'; const a = Math.abs(v); return nf(a >= 1000 ? 2 : a >= 10 ? 3 : a >= 1 ? 4 : 6).format(v); };
  const cls = (v) => (v > 0.0000001 ? 'pos' : v < -0.0000001 ? 'neg' : '');
  const pad2 = (x) => String(x).padStart(2, '0');
  const when = (ms) => { if (!ms) return '–'; const d = new Date(ms); return `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`; };
  const dur = (s) => {
    if (!num(s)) return '–'; s = Math.round(s);
    if (s < 60) return s + 's'; if (s < 3600) return Math.floor(s / 60) + 'm ' + (s % 60) + 's';
    if (s < 86400) return Math.floor(s / 3600) + 'h ' + Math.floor((s % 3600) / 60) + 'm';
    return Math.floor(s / 86400) + 'd ' + Math.floor((s % 86400) / 3600) + 'h';
  };
  const bytes = (n) => (n < 1024 ? n + ' B' : n < 1048576 ? (n / 1024).toFixed(1) + ' KB' : (n / 1048576).toFixed(1) + ' MB');
  const EXCHANGES = { lbank: 'LBank', binance: 'Binance', bybit: 'Bybit', okx: 'OKX' };
  const exName = (id) => EXCHANGES[id] || id;

  /* ---------- api ---------- */
  async function api(path, opts) {
    opts = opts || {};
    const init = { method: opts.method || 'GET', headers: { 'X-Requested-With': 'bot-panel' }, credentials: 'same-origin' };
    if (opts.json !== undefined) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(opts.json); }
    if (opts.body) { init.body = opts.body; Object.assign(init.headers, opts.headers || {}); }
    let r;
    try { r = await fetch(path, init); } catch (e) { throw new Error('Cannot reach the bot. Check your connection.'); }
    let data = null;
    try { data = await r.json(); } catch (e) { /* not json */ }
    if (r.status === 401 && path !== '/api/login') { showLogin(); throw new Error('Signed out'); }
    if (!r.ok) throw new Error((data && data.error) || `Request failed (${r.status})`);
    return data;
  }

  /* ---------- toast & dialog ---------- */
  function toast(msg, kind) {
    const el = document.createElement('div');
    el.className = 'toast ' + (kind || '');
    el.textContent = msg;
    $('#toasts').appendChild(el);
    setTimeout(() => el.remove(), kind === 'bad' ? 8000 : 4000);
  }
  const fail = (e) => { if (e.message !== 'Signed out') toast(e.message, 'bad'); };

  function ask(o) {
    return new Promise((resolve) => {
      const dlg = $('#dlg');
      $('#dlg-title').textContent = o.title;
      $('#dlg-body').textContent = o.body || '';
      const ok = $('#dlg-ok'), input = $('#dlg-input'), typeBox = $('#dlg-type');
      ok.textContent = o.confirm || 'Confirm';
      ok.className = 'btn ' + (o.danger ? 'solid-danger' : 'primary');
      typeBox.hidden = !o.type;
      input.value = '';
      if (o.type) $('#dlg-input-label').textContent = `Type ${o.type} to continue`;
      const sync = () => { ok.disabled = !!o.type && input.value.trim() !== o.type; };
      sync();
      let done = false;
      const finish = (v) => { if (done) return; done = true; input.removeEventListener('input', sync); dlg.close(); resolve(v); };
      input.oninput = sync;
      $('#dlg-form').onsubmit = (e) => { e.preventDefault(); if (!ok.disabled) finish(true); };
      $('#dlg-cancel').onclick = () => finish(false);
      dlg.oncancel = (e) => { e.preventDefault(); finish(false); };
      dlg.showModal();
      (o.type ? input : $('#dlg-cancel')).focus();
    });
  }

  /* ---------- state ---------- */
  const S = { ov: null, view: 'overview', eqHours: 168, tradePage: 0, tradeLimit: 20, logLevel: '', pair: null, settings: null, timers: [], lastOv: 0, ws: null };
  const KNOWN_VIEWS = ['overview', 'trades', 'system', 'settings', 'logs'];

  /* ---------- auth ---------- */
  function showLogin() {
    $('#app').hidden = true; $('#login').hidden = false;
    closeWs(); clearTimers();
    $('#lg-pass').value = ''; $('#lg-pass').focus();
  }
  async function init() {
    try { const me = await api('/api/me'); $('#who').textContent = me.user; } catch (e) { showLogin(); return; }
    $('#login').hidden = true; $('#app').hidden = false;
    connectWs(); route();
  }
  $('#login-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    $('#lg-err').textContent = '';
    try { await api('/api/login', { method: 'POST', json: { username: $('#lg-user').value, password: $('#lg-pass').value } }); init(); }
    catch (err) { $('#lg-err').textContent = err.message; }
  });
  $('#btn-logout').addEventListener('click', async () => { try { await api('/api/logout', { method: 'POST', json: {} }); } catch (e) { /* ignore */ } showLogin(); });

  /* ---------- websocket (+ polling fallback) ---------- */
  function connectWs() {
    closeWs();
    const ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws');
    S.ws = ws;
    ws.onmessage = (m) => { try { onOverview(JSON.parse(m.data)); } catch (e) { /* ignore */ } };
    ws.onclose = () => { if (S.ws === ws && !$('#app').hidden) setTimeout(() => { if (S.ws === ws) connectWs(); }, 3000); };
  }
  function closeWs() { const w = S.ws; S.ws = null; if (w) { try { w.close(); } catch (e) { /* ignore */ } } }
  setInterval(async () => {
    if ($('#app').hidden || Date.now() - S.lastOv < 7000) return;
    try { onOverview(await api('/api/overview')); } catch (e) { /* offline */ }
  }, 5000);

  /* ---------- routing ---------- */
  function clearTimers() { S.timers.forEach(clearInterval); S.timers = []; }
  function route() {
    const v = (location.hash || '#overview').slice(1);
    S.view = KNOWN_VIEWS.includes(v) ? v : 'overview';
    $$('.view').forEach((el) => { el.hidden = el.id !== 'v-' + S.view; });
    $$('.rail .nav').forEach((b) => { if (b.dataset.view === S.view) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current'); });
    clearTimers();
    const loaders = { overview: [loadEquity, 30000], trades: [loadTrades, 15000], system: [loadSystem, 5000], settings: [loadSettings, 0], logs: [loadLogs, 5000] };
    const [fn, every] = loaders[S.view];
    fn().catch(fail);
    if (every) S.timers.push(setInterval(() => fn().catch(() => {}), every));
    if (S.ov) renderOverview();
    window.scrollTo(0, 0);
  }
  window.addEventListener('hashchange', route);
  $$('.rail .nav').forEach((b) => b.addEventListener('click', () => { location.hash = '#' + b.dataset.view; }));

  /* ---------- header, banners, overview ---------- */
  function onOverview(o) {
    S.ov = o; S.lastOv = Date.now();
    renderTop(o);
    if (S.view === 'overview') renderOverview();
  }

  function renderTop(o) {
    const b = o.bot, st = $('#pill-status');
    const label = { active: 'Trading', paused: 'Paused', halted: 'Halted' }[b.status] || b.status;
    st.className = 'pill ' + (b.status === 'active' ? 'ok' : b.status);
    st.lastElementChild.textContent = label;
    const m = $('#pill-mode');
    m.textContent = b.mode === 'live' ? 'Live: real money' : 'Paper trading';
    m.className = 'pill' + (b.mode === 'live' ? ' live' : '');
    $('#top-note').textContent = `${exName(b.exchange)}, ${b.timeframe} candles`;
    const pb = $('#btn-pause');
    pb.hidden = b.status === 'halted';
    pb.textContent = b.status === 'paused' ? 'Resume trading' : 'Pause new trades';

    const items = [];
    if (b.halted) items.push(`<div class="banner bad"><p><strong>The bot halted itself.</strong> ${esc(b.halted)}. No new trades will open until you review and clear this.</p><button class="btn small" data-act="clear-halt" type="button">Clear halt</button></div>`);
    else if (b.status === 'paused') items.push(`<div class="banner caution"><p>New trades are paused. Open positions are still protected by their stop-loss and take-profit.</p><button class="btn small" data-act="resume" type="button">Resume trading</button></div>`);
    if (b.data_source === 'sim') items.push(`<div class="banner caution"><p><strong>Simulated market data.</strong> Prices are synthetic and for testing only.</p></div>`);
    if (b.feed_error) items.push(`<div class="banner caution"><p><strong>Market data problem.</strong> ${esc(b.feed_error)}. The bot keeps retrying.</p></div>`);
    if (!b.broker_ready) items.push(`<div class="banner bad"><p><strong>No trading connection.</strong> Live mode could not start. Switch to paper mode in Settings or check your API keys.</p></div>`);
    const html = items.join('');
    const box = $('#banners');
    if (box.dataset.sig !== html) { box.innerHTML = html; box.dataset.sig = html; }
  }
  $('#banners').addEventListener('click', async (e) => {
    const act = e.target.dataset && e.target.dataset.act;
    if (!act) return;
    try {
      if (act === 'clear-halt') {
        if (!(await ask({ title: 'Clear the halt?', body: 'Make sure you understand why the bot stopped. The drawdown baseline is reset to the current equity.', confirm: 'Clear halt' }))) return;
      }
      await api('/api/bot/' + act, { method: 'POST', json: {} });
      toast(act === 'resume' ? 'Trading resumed' : 'Halt cleared', 'good');
    } catch (err) { fail(err); }
  });
  $('#btn-pause').addEventListener('click', async () => {
    try { await api('/api/bot/' + (S.ov && S.ov.bot.status === 'paused' ? 'resume' : 'pause'), { method: 'POST', json: {} }); } catch (e) { fail(e); }
  });
  $('#btn-kill').addEventListener('click', async () => {
    const n = S.ov ? S.ov.positions.length : 0;
    if (!(await ask({ title: 'Emergency stop', body: `This blocks all new trades and closes ${n ? n + ' open position' + (n > 1 ? 's' : '') : 'any open positions'} at market price right now.`, confirm: 'Stop and close everything', danger: true }))) return;
    try {
      const r = await api('/api/bot/kill', { method: 'POST', json: {} });
      toast(r.remaining ? `Stopped, but ${r.remaining} position(s) could not be closed. Check the exchange now.` : `Stopped. Closed ${r.closed} position(s).`, r.remaining ? 'bad' : 'good');
    } catch (e) { fail(e); }
  });

  function renderOverview() {
    const o = S.ov, a = o.account;
    $('#o-equity').textContent = money(a.equity) + ' USDT';
    $('#o-equity-sub').textContent = `Balance ${money(a.balance)}, in use ${money(a.margin_used)}`;
    setSigned('#o-today', a.pnl_today, 'USDT'); $('#o-today-sub').textContent = a.equity ? pct(a.pnl_today / (a.equity - a.pnl_today || 1) * 100) : '';
    setSigned('#o-total', a.pnl_total, 'USDT'); $('#o-total-sub').textContent = pct(a.pnl_total_pct);
    $('#o-win').textContent = o.stats.win_rate == null ? '–' : o.stats.win_rate.toFixed(0) + '%';
    $('#o-win-sub').textContent = `${o.stats.trades} closed trade${o.stats.trades === 1 ? '' : 's'}`;
    $('#o-dd').textContent = a.drawdown_pct ? '−' + a.drawdown_pct.toFixed(2) + '%' : '0.00%';
    $('#o-dd-sub').textContent = 'from peak equity';

    const P = o.positions;
    $('#pos-sub').textContent = P.length ? `Margin in use ${money(a.margin_used)} USDT` : '';
    $('#t-pos').innerHTML = P.length
      ? `<thead><tr><th>Pair</th><th>Side</th><th class="n">Size</th><th class="n">Entry</th><th class="n">Price</th><th class="n">Stop</th><th class="n">Target</th><th class="n">P&amp;L (USDT)</th><th class="n">Held</th><th></th></tr></thead><tbody>` +
        P.map((p) => `<tr><td>${esc(p.symbol)}</td><td><span class="tag ${p.side}">${p.side === 'long' ? 'Long' : 'Short'} ${p.leverage}x</span></td><td class="n">${p.qty.toPrecision(4)}</td><td class="n">${price(p.entry)}</td><td class="n">${price(p.price)}</td><td class="n">${price(p.sl)}</td><td class="n">${price(p.tp)}</td><td class="n ${cls(p.upnl)}">${signed(p.upnl)} <span class="muted">(${pct(p.upnl_pct, 1)})</span></td><td class="n">${dur(p.age_s)}</td><td class="n"><button class="btn small" data-close="${p.id}" type="button">Close</button></td></tr>`).join('') + '</tbody>'
      : '<tbody><tr><td class="empty">No open positions. The bot opens a trade only when its model passes validation and the risk checks agree.</td></tr></tbody>';

    const rows = Object.entries(o.pairs).map(([sym, p]) => {
      const m = o.models[sym] || {};
      let model;
      if (p.error) model = `<span class="neg">${esc(p.error)}</span>`;
      else if (!m.trained) model = m.bars < 400 ? `<span class="muted">Collecting data (${m.bars}/400 bars)</span>` : '<span class="muted">Training…</span>';
      else if (m.validated) model = `<span class="pos">Ready to trade</span> <span class="muted">${(m.acc * 100).toFixed(1)}% accuracy</span>`;
      else model = `<span class="warn">Not trading</span> <span class="muted">${(m.acc * 100).toFixed(1)}% vs ${(m.base_rate * 100).toFixed(1)}% baseline</span>`;
      const stale = p.age_s != null && p.age_s > 60;
      return `<tr><td>${esc(sym)}</td><td class="n">${price(p.price)}</td><td class="n ${stale ? 'warn' : 'muted'}">${p.age_s == null ? '–' : dur(p.age_s)}</td><td>${model}</td></tr>`;
    });
    $('#t-pairs').innerHTML = `<thead><tr><th>Pair</th><th class="n">Price</th><th class="n">Updated</th><th>Model</th></tr></thead><tbody>${rows.join('')}</tbody>`;

    const s = o.system || {};
    $('#srv-sub').textContent = s.uptime_s != null ? 'Up ' + dur(s.uptime_s) : '';
    $('#meters-mini').innerHTML = meter('CPU', s.cpu_pct, `${(s.cpu_pct || 0).toFixed(0)}%`) + meter('Memory', s.ram_pct, `${s.ram_used_mb || 0} of ${s.ram_total_mb || 0} MB`) + meter('Disk', s.disk_pct, `${s.disk_used_gb || 0} of ${s.disk_total_gb || 0} GB`);
    const bbox = $('#eq-empty');
    if (bbox) bbox.hidden = !!S.eqPts;
  }
  $('#t-pos').addEventListener('click', async (e) => {
    const id = e.target.dataset && e.target.dataset.close;
    if (!id) return;
    if (!(await ask({ title: 'Close this position?', body: 'It closes at the current market price.', confirm: 'Close position' }))) return;
    try { await api(`/api/positions/${id}/close`, { method: 'POST', json: {} }); toast('Position closed', 'good'); } catch (err) { fail(err); }
  });
  function setSigned(sel, v, unit) { const el = $(sel); el.textContent = signed(v) + (unit ? ' ' + unit : ''); el.className = 'v ' + cls(v); }
  function meter(label, p, text) {
    const v = Math.max(0, Math.min(100, p || 0));
    return `<div class="meter"><div class="row"><span>${esc(label)}</span><span class="muted">${esc(text)}</span></div><div class="bar"><b class="${v > 90 ? 'crit' : v > 75 ? 'hi' : ''}" data-w="${v}"></b></div></div>`;
  }
  function applyMeters(root) { $$('.bar > b', root).forEach((b) => { b.style.width = b.dataset.w + '%'; }); }
  new MutationObserver(() => { applyMeters(document); }).observe(document.body, { childList: true, subtree: true });

  /* ---------- equity chart ---------- */
  const eqChart = Charts.line($('#c-equity'), { fmt: (v, full) => money(v, full ? 2 : v >= 10000 ? 0 : 1) });
  async function loadEquity() {
    const r = await api('/api/equity?hours=' + S.eqHours);
    const pts = r.points.map((p) => ({ t: p.ts, v: p.equity }));
    S.eqPts = pts.length > 1 ? pts : null;
    $('#eq-empty').hidden = !!S.eqPts;
    $('#c-equity').hidden = !S.eqPts;
    if (S.eqPts) eqChart.set(pts);
  }
  $('#eq-range').addEventListener('click', (e) => {
    const h = e.target.dataset && e.target.dataset.h;
    if (!h) return;
    S.eqHours = +h;
    $$('#eq-range button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.h === h)));
    loadEquity().catch(fail);
  });

  /* ---------- trades & analytics ---------- */
  const cumChart = Charts.line($('#c-cum'), { fmt: (v, full) => signed(v, full ? 2 : 1) });
  const dayChart = Charts.bars($('#c-daily'), { fmt: (v, full) => signed(v, full ? 2 : 1) });
  const candleChart = Charts.candles($('#c-candles'), { fmt: price });

  async function loadTrades() {
    const [an, tr] = await Promise.all([api('/api/analytics'), api(`/api/trades?limit=${S.tradeLimit}&offset=${S.tradePage * S.tradeLimit}&side=${encodeURIComponent($('#f-side').value)}`)]);
    renderAnalytics(an);
    renderTradeTable(tr);
    fillPairs();
    loadCandles().catch(() => {});
  }
  function renderAnalytics(a) {
    if (!a.trades) {
      $('#an-sub').textContent = '';
      $('#an-kv').innerHTML = '<p class="empty">No closed trades yet. Results appear here after the first trade closes.</p>';
      cumChart.set(null); dayChart.set(null);
      $('#t-bysym').innerHTML = '<tbody><tr><td class="empty">Nothing to show yet.</td></tr></tbody>';
      $('#t-byreason').innerHTML = $('#t-bysym').innerHTML;
      return;
    }
    $('#an-sub').textContent = `${a.trades} trades, ${a.wins} won, ${a.losses} lost`;
    const kv = [
      ['Net result', `<span class="${cls(a.net_pnl)}">${signed(a.net_pnl)} USDT</span>`],
      ['Win rate', a.win_rate.toFixed(1) + '%'],
      ['Profit factor', a.profit_factor == null ? '–' : a.profit_factor.toFixed(2)],
      ['Average win / loss', `<span class="pos">${signed(a.avg_win)}</span> / <span class="neg">${signed(a.avg_loss)}</span>`],
      ['Reward to risk', a.risk_reward == null ? '–' : a.risk_reward.toFixed(2)],
      ['Expectancy per trade', `<span class="${cls(a.expectancy)}">${signed(a.expectancy)} USDT</span>`],
      ['Average R', `<span class="${cls(a.avg_r)}">${signed(a.avg_r)}R</span>`],
      ['Max drawdown (trades)', money(a.max_drawdown) + ' USDT'],
      ['Longest losing streak', a.max_loss_streak],
      ['Fees paid', money(a.fees) + ' USDT'],
      ['Best / worst trade', `<span class="pos">${signed(a.best)}</span> / <span class="neg">${signed(a.worst)}</span>`],
      ['Average hold', dur(a.avg_hold_min * 60)],
    ];
    $('#an-kv').innerHTML = kv.map(([k, v]) => `<div><span class="k">${k}</span><span class="v">${v}</span></div>`).join('');
    cumChart.set(a.curve.length > 1 ? a.curve.map((c) => ({ t: c.ts, v: c.cum })) : null);
    dayChart.set(a.daily.length ? a.daily.map((d) => ({ label: d.date, v: d.pnl })) : null);
    const grp = (id, obj, label) => {
      $(id).innerHTML = `<thead><tr><th>${label}</th><th class="n">Trades</th><th class="n">Win rate</th><th class="n">Result (USDT)</th></tr></thead><tbody>` +
        Object.entries(obj).sort((x, y) => y[1].pnl - x[1].pnl).map(([k, v]) => `<tr><td>${esc(k.replace(/_/g, ' '))}</td><td class="n">${v.trades}</td><td class="n">${v.win_rate.toFixed(0)}%</td><td class="n ${cls(v.pnl)}">${signed(v.pnl)}</td></tr>`).join('') + '</tbody>';
    };
    grp('#t-bysym', a.by_symbol, 'Pair'); grp('#t-byreason', a.by_reason, 'Exit reason');
  }
  function renderTradeTable(tr) {
    $('#t-trades').innerHTML = tr.trades.length
      ? `<thead><tr><th>Closed</th><th>Pair</th><th>Side</th><th class="n">Entry</th><th class="n">Exit</th><th class="n">Size</th><th class="n">Result (USDT)</th><th class="n">R</th><th>Exit reason</th></tr></thead><tbody>` +
        tr.trades.map((t) => `<tr><td>${when(t.closed_at)}</td><td>${esc(t.symbol)}</td><td><span class="tag ${t.side}">${t.side === 'long' ? 'Long' : 'Short'} ${t.leverage}x</span></td><td class="n">${price(t.entry)}</td><td class="n">${price(t.exit)}</td><td class="n">${t.qty.toPrecision(4)}</td><td class="n ${cls(t.pnl)}">${signed(t.pnl)}</td><td class="n ${cls(t.r_mult)}">${signed(t.r_mult, 2)}</td><td>${esc((t.reason || '').replace(/_/g, ' '))}</td></tr>`).join('') + '</tbody>'
      : '<tbody><tr><td class="empty">No trades yet.</td></tr></tbody>';
    const from = tr.total ? S.tradePage * S.tradeLimit + 1 : 0, to = Math.min(tr.total, (S.tradePage + 1) * S.tradeLimit);
    $('#pg-info').textContent = tr.total ? `${from} to ${to} of ${tr.total}` : '';
    $('#pg-prev').disabled = S.tradePage === 0; $('#pg-next').disabled = to >= tr.total;
  }
  $('#pg-prev').addEventListener('click', () => { S.tradePage = Math.max(0, S.tradePage - 1); loadTrades().catch(fail); });
  $('#pg-next').addEventListener('click', () => { S.tradePage++; loadTrades().catch(fail); });
  $('#f-side').addEventListener('change', () => { S.tradePage = 0; loadTrades().catch(fail); });
  function fillPairs() {
    const sel = $('#sel-pair'), pairs = S.ov ? Object.keys(S.ov.pairs) : [];
    if (sel.options.length !== pairs.length || pairs.some((p, i) => sel.options[i].value !== p)) {
      sel.innerHTML = pairs.map((p) => `<option>${esc(p)}</option>`).join('');
    }
    if (!S.pair || !pairs.includes(S.pair)) S.pair = pairs[0];
    sel.value = S.pair;
  }
  $('#sel-pair').addEventListener('change', (e) => { S.pair = e.target.value; loadCandles().catch(fail); });
  async function loadCandles() {
    if (!S.pair) return;
    candleChart.set(await api('/api/candles?symbol=' + encodeURIComponent(S.pair) + '&limit=200'));
  }

  /* ---------- system & backups ---------- */
  async function loadSystem() {
    const r = await api('/api/system'), m = r.metrics || {};
    $('#sys-sub').textContent = `Python ${r.python}, up ${dur(m.uptime_s)}`;
    $('#meters').innerHTML =
      meter('Server CPU', m.cpu_pct, `${(m.cpu_pct || 0).toFixed(0)}%, load ${m.load1}`) +
      meter('Server memory', m.ram_pct, `${m.ram_used_mb} of ${m.ram_total_mb} MB`) +
      meter('Disk', m.disk_pct, `${m.disk_used_gb} of ${m.disk_total_gb} GB`) +
      `<div class="kv"><div><span class="k">Bot memory</span><span class="v">${m.proc_rss_mb} MB</span></div><div><span class="k">Bot CPU</span><span class="v">${(m.proc_cpu_pct || 0).toFixed(1)}%</span></div><div><span class="k">Database</span><span class="v">${bytes(r.db_bytes)}</span></div><div><span class="k">Threads</span><span class="v">${m.threads}</span></div></div>`;
    const names = { data: 'Market data', strategy: 'Strategy', retrain: 'Model training', risk: 'Risk', 'risk-monitor': 'Risk limits', account: 'Execution', metrics: 'System monitor', equity: 'Equity history', backup: 'Backups', maintenance: 'Cleanup' };
    const hb = r.health.agents;
    $('#t-health').innerHTML = '<thead><tr><th>Component</th><th>State</th><th class="n">Last activity</th></tr></thead><tbody>' +
      Object.entries(r.health.tasks).map(([k, up]) => { const age = (hb[k] || hb[k === 'retrain' ? 'strategy-train' : k === 'risk-monitor' ? 'risk-monitor' : k] || {}).age_s; return `<tr><td>${esc(names[k] || k)}</td><td class="${up ? 'pos' : 'neg'}">${up ? 'Running' : 'Stopped'}</td><td class="n muted">${age == null ? '–' : dur(age) + ' ago'}</td></tr>`; }).join('') + '</tbody>';
    renderBackups(r.backups);
  }
  function renderBackups(list) {
    $('#bk-note').textContent = S.settings ? `Automatic backups run every ${S.settings.values.backup_hours} h; the newest ${S.settings.values.backup_keep} are kept.` : '';
    const kinds = { auto: 'Automatic', manual: 'Manual', 'pre-restore': 'Before restore', 'pre-reset': 'Before reset' };
    $('#t-backups').innerHTML = list.length
      ? '<thead><tr><th>File</th><th>Type</th><th class="n">Size</th><th>Created</th><th></th></tr></thead><tbody>' +
        list.map((b) => `<tr><td class="mono">${esc(b.name)}</td><td>${esc(kinds[b.kind] || b.kind)}</td><td class="n">${bytes(b.size)}</td><td>${when(b.mtime)}</td><td class="n"><a class="btn small" href="/api/backups/${encodeURIComponent(b.name)}" download>Download</a> <button class="btn small" data-restore="${esc(b.name)}" type="button">Restore</button> <button class="btn small danger" data-del="${esc(b.name)}" type="button">Delete</button></td></tr>`).join('') + '</tbody>'
      : '<tbody><tr><td class="empty">No backups yet.</td></tr></tbody>';
  }
  $('#t-backups').addEventListener('click', async (e) => {
    const d = e.target.dataset || {};
    try {
      if (d.restore) {
        if (!(await ask({ title: 'Restore this backup?', body: 'The current database is replaced (a safety backup is made first). Trading is blocked while this runs. Your admin login is kept.', confirm: 'Restore', danger: true, type: 'RESTORE' }))) return;
        const r = await api(`/api/backups/${encodeURIComponent(d.restore)}/restore`, { method: 'POST', json: { confirm: 'RESTORE' } });
        toast(r.message, 'good'); loadSystem().catch(() => {});
      } else if (d.del) {
        if (!(await ask({ title: 'Delete this backup?', body: d.del, confirm: 'Delete', danger: true }))) return;
        await api('/api/backups/' + encodeURIComponent(d.del), { method: 'DELETE' }); loadSystem().catch(() => {});
      }
    } catch (err) { fail(err); }
  });
  $('#btn-backup').addEventListener('click', async () => { try { const r = await api('/api/backups', { method: 'POST', json: {} }); toast('Backup created: ' + r.name, 'good'); loadSystem(); } catch (e) { fail(e); } });
  $('#lbl-restore').addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); $('#file-restore').click(); } });
  $('#file-restore').addEventListener('change', async (e) => {
    const f = e.target.files[0]; e.target.value = '';
    if (!f) return;
    if (!(await ask({ title: 'Restore from this file?', body: `${f.name} (${bytes(f.size)}). It is checked first; if it is not a valid bot backup nothing changes.`, confirm: 'Restore', danger: true, type: 'RESTORE' }))) return;
    try { const r = await api('/api/restore', { method: 'POST', body: f, headers: { 'Content-Type': 'application/octet-stream', 'X-Confirm': 'RESTORE' } }); toast(r.message, 'good'); loadSystem().catch(() => {}); } catch (err) { fail(err); }
  });
  $('#btn-restart').addEventListener('click', async () => {
    if (!(await ask({ title: 'Restart the bot?', body: 'The panel is unavailable for a few seconds. Open trades keep their stops in the database.', confirm: 'Restart' }))) return;
    try { const r = await api('/api/bot/restart', { method: 'POST', json: {} }); toast(r.message); } catch (e) { fail(e); }
  });

  /* ---------- settings ---------- */
  async function loadSettings() {
    const s = await api('/api/settings');
    S.settings = s;
    const groups = {};
    s.schema.forEach((f) => { (groups[f.group] = groups[f.group] || []).push(f); });
    const v = s.values;
    $('#set-groups').innerHTML = Object.entries(groups).map(([g, fields]) =>
      `<div class="group"><h3>${esc(g)}</h3><div class="form-grid">` + fields.map((f) => {
        const id = 's-' + f.key, val = v[f.key];
        let input;
        if (f.kind === 'choice') input = `<select id="${id}" data-key="${f.key}">${f.choices.map((c) => `<option value="${esc(c)}" ${c === val ? 'selected' : ''}>${esc(c === 'paper' ? 'Paper (simulated)' : c === 'live' ? 'Live (real money)' : c)}</option>`).join('')}</select>`;
        else if (f.kind === 'list') input = `<textarea id="${id}" data-key="${f.key}" rows="2">${esc(val.join(', '))}</textarea>`;
        else input = `<input id="${id}" data-key="${f.key}" type="number" inputmode="decimal" step="${f.kind === 'int' ? 1 : 'any'}" ${f.min != null ? `min="${f.min}"` : ''} ${f.max != null ? `max="${f.max}"` : ''} value="${esc(val)}">`;
        const note = f.key === 'mode' && s.exchange === 'lbank' ? 'LBank futures cannot be traded live through this bot; paper mode uses real LBank prices.' : f.help;
        return `<div class="field"><label for="${id}">${esc(f.label)}</label>${input}${note ? `<span class="help">${esc(note)}</span>` : ''}<span class="err" id="e-${f.key}"></span></div>`;
      }).join('') + '</div></div>').join('');
    $('#set-sub').textContent = `Exchange: ${exName(s.exchange)}`;
    const m = s.secrets, have = m.api_key && m.api_secret;
    $('#keys-sub').textContent = have ? `Saved: ${m.api_key}` : 'No keys saved';
    $('#k-key').placeholder = m.api_key ? m.api_key + ' (leave empty to keep)' : '';
    $('#k-secret').placeholder = m.api_secret ? 'Saved (leave empty to keep)' : '';
    $('#k-pass').placeholder = m.api_password ? 'Saved (leave empty to keep)' : '';
    $('#paper-panel').hidden = v.mode !== 'paper';
    applyMeters(document);
  }
  $('#settings-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const s = S.settings; if (!s) return;
    const changes = {};
    $$('#set-groups [data-key]').forEach((el) => {
      const k = el.dataset.key, cur = s.values[k];
      let val = el.value;
      if (s.schema.find((f) => f.key === k).kind === 'list') { if (val.split(/[,\n]/).map((x) => x.trim().toUpperCase()).filter(Boolean).join(',') === cur.join(',')) return; }
      else if (String(cur) === val) return;
      changes[k] = val;
      $('#e-' + k).textContent = '';
    });
    if (!Object.keys(changes).length) { $('#save-note').textContent = 'Nothing changed.'; return; }
    const payload = { settings: changes };
    if (changes.mode === 'live') {
      if (!(await ask({ title: 'Switch to live trading?', body: 'The bot will place real orders with your exchange account and real money. Paper results do not predict live results.', confirm: 'Enable live trading', danger: true, type: 'ENABLE LIVE' }))) return;
      payload.confirm_live = 'ENABLE LIVE';
    }
    try {
      const r = await api('/api/settings', { method: 'POST', json: payload });
      $('#save-note').textContent = 'Saved.';
      toast('Settings saved', 'good');
      if (r.restart_required) toast('The new port applies after a restart (System, Restart bot).');
      await loadSettings();
    } catch (err) { $('#save-note').textContent = ''; fail(err); }
  });
  $('#keys-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const secrets = { api_key: $('#k-key').value, api_secret: $('#k-secret').value, api_password: $('#k-pass').value };
    if (!secrets.api_key && !secrets.api_secret && !secrets.api_password) { toast('Enter a key and secret first.'); return; }
    try { await api('/api/settings', { method: 'POST', json: { settings: {}, secrets } }); ['#k-key', '#k-secret', '#k-pass'].forEach((s) => { $(s).value = ''; }); toast('Keys saved', 'good'); loadSettings(); } catch (err) { fail(err); }
  });
  $('#btn-clear-keys').addEventListener('click', async () => {
    if (!(await ask({ title: 'Remove saved API keys?', body: 'Live trading stops working until you add keys again.', confirm: 'Remove keys', danger: true }))) return;
    try { await api('/api/settings', { method: 'POST', json: { settings: {}, clear_secrets: true } }); toast('Keys removed', 'good'); loadSettings(); } catch (e) { fail(e); }
  });
  $('#pw-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    try { await api('/api/password', { method: 'POST', json: { current: $('#pw-cur').value, new: $('#pw-new').value } }); toast('Password changed. Sign in again.', 'good'); showLogin(); } catch (err) { fail(err); }
  });
  $('#btn-reset-paper').addEventListener('click', async () => {
    if (!(await ask({ title: 'Reset the paper account?', body: 'Paper trades, the equity curve and the balance are cleared. A backup is taken first.', confirm: 'Reset', danger: true, type: 'RESET' }))) return;
    try { await api('/api/paper/reset', { method: 'POST', json: { confirm: 'RESET' } }); toast('Paper account reset', 'good'); } catch (e) { fail(e); }
  });

  /* ---------- logs & signals ---------- */
  async function loadLogs() {
    const [lg, sg] = await Promise.all([api('/api/logs?limit=300&level=' + S.logLevel), api('/api/signals?limit=30')]);
    $('#loglist').innerHTML = lg.logs.length
      ? lg.logs.map((l) => `<div class="logrow"><span class="muted">${new Date(l.ts).toLocaleString([], { hour12: false })}</span><span class="lv-${esc(l.level)}">${esc(l.level.toLowerCase())}</span><span class="muted">${esc(l.agent)}</span><span class="m">${esc(l.msg)}</span></div>`).join('')
      : '<p class="empty">Nothing logged at this level.</p>';
    $('#t-signals').innerHTML = sg.signals.length
      ? '<thead><tr><th>Time</th><th>Pair</th><th>Side</th><th class="n">P(up)</th><th class="n">Price</th><th>Outcome</th></tr></thead><tbody>' +
        sg.signals.map((s) => `<tr><td>${when(s.ts)}</td><td>${esc(s.symbol)}</td><td><span class="tag ${s.side}">${s.side === 'long' ? 'Long' : 'Short'}</span></td><td class="n">${(s.prob * 100).toFixed(1)}%</td><td class="n">${price(s.price)}</td><td>${esc(({ executed: 'Traded', rejected: 'Rejected', filtered: 'Filtered', new: 'Pending', error: 'Error' })[s.status] || s.status)}${s.reason ? ` <span class="muted">${esc(s.reason)}</span>` : ''}</td></tr>`).join('') + '</tbody>'
      : '<tbody><tr><td class="empty">No signals yet. The model only speaks up when it is confident and the market filters agree.</td></tr></tbody>';
  }
  $('#log-level').addEventListener('click', (e) => {
    if (e.target.dataset.l === undefined) return;
    S.logLevel = e.target.dataset.l;
    $$('#log-level button').forEach((b) => b.setAttribute('aria-pressed', String(b === e.target)));
    loadLogs().catch(fail);
  });

  window.addEventListener('resize', () => { /* charts redraw via ResizeObserver */ });
  init();
})();
