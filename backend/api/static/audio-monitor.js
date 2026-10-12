// Audio monitor: waveforms, meters, bypasses and the processing graph.
// The script defines window.JmmMonitor.mount(root), which draws a monitor into root and returns
// its { start, stop } handle. Nothing streams until start() is called.
(() => {
  'use strict';

  function mount(root) {
    if (root.jmmMonitor) return root.jmmMonitor;
    // data-base is the URL prefix of the admin proxy to the audio container's control API; the
    // admin key comes from data-key or the page's ?key=, and is sent with every API call.
    const API_BASE = root.dataset.base || '';
    const API_KEY = root.dataset.key || new URLSearchParams(window.location.search).get('key') || '';
    const apiUrl = (path) => API_BASE + path
      + (API_KEY ? (path.includes('?') ? '&' : '?') + 'key=' + encodeURIComponent(API_KEY) : '');
    root.classList.add('jmm-monitor');
    // data-embedded: inside the admin panel. Use its Bootstrap buttons and colours.
    const EMBEDDED = 'embedded' in root.dataset;
    if (EMBEDDED) root.classList.add('embedded');
    // data-input-only: just the input graph (the studio deck). The rest of the monitor is not built.
    const inputOnly = 'inputOnly' in root.dataset;
    if (inputOnly) root.classList.add('input-only');
    const P = inputOnly ? 'jmi-' : 'jm-';
    const $ = (id) => root.querySelector('#' + P + id);
    // Element ids carry a prefix so two monitors on one page never share an id. The input-only
    // view (the studio deck's input graph) renders just the linear waveform and its meters.
    const channelIn = `
    <section class="channel">
      ${inputOnly ? '' : '<h2>Input <span>After the input trims, before the AGC and processing</span></h2>'}
      <div class="wave-row">
        <div class="wave" id="${P}inWave"><canvas id="${P}inCanvas" aria-label="Input waveform"></canvas></div>
        <div class="meters" id="${P}inMeters"></div>
      </div>
    </section>`;

    const monitorHead = `
  <header>
    <div>
      <h1>JMM Audio</h1>
      <div class="sub">Processing monitor</div>
    </div>
    <div id="${P}conn" class="conn" data-state="reconnecting"><span class="dot"></span><span id="${P}connText">Connecting</span></div>
    <span class="spacer"></span>
    <button id="${P}agc" class="bypass" type="button" aria-pressed="false"><span>Bypass AGC</span><span class="state">AGC ON</span></button>
    <button id="${P}bypass" class="bypass" type="button" aria-pressed="false" disabled><span>Bypass processing</span><span class="state">PROCESSING</span></button>
  </header>`;

    const monitorBody = `
  <main>
    <div id="${P}notice" class="notice" role="status">Live control is unavailable: the processor is not accepting changes, so it cannot be bypassed.</div>

    ${channelIn}

    <div class="agcbar" id="${P}agcBar">
      <span>AGC gain</span>
      <div class="agc-track" aria-hidden="true"><div class="agc-fill" id="${P}agcFill"></div><div class="agc-zero"></div></div>
      <b id="${P}agcVal" class="mono">--</b>
      <div class="agc-scale" aria-hidden="true"></div>
    </div>

    <div class="diffbar">
      <span>Output &minus; Input RMS <small>(3 s average)</small></span>
      <b id="${P}diff" class="mono">--</b>
      <div class="seg" role="group" aria-label="Waveform scale">
        <button type="button" data-scale="lin" aria-pressed="true">Linear</button>
        <button type="button" data-scale="db" aria-pressed="false">dB</button>
      </div>
    </div>

    <section class="channel">
      <h2>Output <span>As broadcast, after processing</span></h2>
      <div class="wave-row">
        <div class="wave" id="${P}outWave"><canvas id="${P}outCanvas" aria-label="Output waveform"></canvas></div>
        <div class="meters" id="${P}outMeters"></div>
      </div>
    </section>

    <details class="panel" id="${P}graphPanel">
      <summary><h2>Processing graph</h2></summary>
      <p class="muted">Edit the FFmpeg source and apply it. The processor is replaced without
        interrupting the stream. Edits last until the audio service restarts; the stream and
        its encoder are not restarted by a change.</p>
      <textarea id="${P}graphText" spellcheck="false" aria-label="Processing graph source"></textarea>
      <div class="graphbar">
        <button id="${P}graphApply" type="button" disabled>Apply</button>
        <button id="${P}graphRevert" type="button" disabled>Revert to file</button>
        <span id="${P}graphOrigin" class="muted"></span>
        <span id="${P}graphSwap" class="st-line muted"></span>
      </div>
    </details>
  </main>`;

    const monitorFoot = `
  <footer>
    <span id="${P}note" class="note"></span>
  </footer>

  <div id="${P}toasts" aria-live="polite"></div>`;

    root.innerHTML = inputOnly ? `<main>${channelIn}</main>` : monitorHead + monitorBody + monitorFoot;

    const N = 1000;                      // history columns: 10 ms each, 10 s total
    const FLOOR_DB = -60;                // waveform dB scale floor
    const METER_DB = -48;                // meter scale floor
    const GRID_DB = [-6, -12, -24];
    const AGC_RANGE_DB = 15;             // agc_range_db in config/processing.liq
    const COLORS = {
      in: ['45,212,191', '56,189,248'],  // L, R
      out: ['163,230,53', '250,204,21'],
    };
    const CONN_TEXT = { live: 'Live', reconnecting: 'Reconnecting', nodata: 'No meter data', offline: 'Offline' };

    const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
    const toDb = (a) => (a > 0 ? 20 * Math.log10(a) : -Infinity);
    const fromDb = (d) => Math.pow(10, d / 20);
    const fmtDb = (d) => (Number.isFinite(d) ? d.toFixed(1) : '-inf');
    const meterPos = (d) => (Number.isFinite(d) ? clamp((d - METER_DB) / -METER_DB, 0, 1) * 100 : 0);

    let control = false;
    let bypass = false;
    let agcBypass = false;
    let scaleMode = 'lin';
    let stateDown = false;
    let es = null;
    let lastMsg = 0;
    let connState = '';
    let dirty = true;
    let agcGain = null;                  // AGC gain in dB from the meter stream; null: unknown

    class Ring {
      constructor() { this.data = new Float32Array(N * 4); this.head = 0; }
      push(col) {
        const o = this.head * 4;
        for (let i = 0; i < 4; i++) this.data[o + i] = Number(col[i]) || 0;
        this.head = (this.head + 1) % N;
      }
      // Over the last n columns: peak per channel (max) and RMS per channel (power mean).
      window(n) {
        const pk = [0, 0], ms = [0, 0], d = this.data;
        for (let k = 0; k < n; k++) {
          const o = ((this.head - 1 - k + 2 * N) % N) * 4;
          for (let c = 0; c < 2; c++) {
            pk[c] = Math.max(pk[c], d[o + c]);
            ms[c] += d[o + 2 + c] ** 2;
          }
        }
        return { pk, rms: ms.map((v) => Math.sqrt(v / n)) };
      }
    }

    const inRing = new Ring();
    const outRing = new Ring();
    const waves = [{ canvas: $('inCanvas'), ring: inRing, colors: COLORS.in, wrap: $('inWave'), w: 0, h: 0, dpr: 0 }];
    if (!inputOnly) {
      waves.push({ canvas: $('outCanvas'), ring: outRing, colors: COLORS.out, wrap: $('outWave'), w: 0, h: 0, dpr: 0 });
    }
    waves.forEach((wv) => { wv.ctx = wv.canvas.getContext('2d'); });

    function buildMeters(root, colors) {
      const rows = ['L', 'R'].map((name, ch) => {
        const row = document.createElement('div');
        row.className = 'm';
        row.style.setProperty('--c', `rgb(${colors[ch]})`);
        row.innerHTML = '<span class="lbl"></span><div class="track"><div class="pk"></div>'
          + '<div class="rms"></div><div class="hold"></div></div>'
          + '<div class="rd"><span class="rv"></span><span class="pv"></span></div>';
        row.querySelector('.lbl').textContent = name;
        root.append(row);
        return {
          ch, pk: row.querySelector('.pk'), rms: row.querySelector('.rms'), hold: row.querySelector('.hold'),
          rv: row.querySelector('.rv'), pv: row.querySelector('.pv'), held: -Infinity, heldAt: 0,
        };
      });
      const scale = document.createElement('div');
      scale.className = 'scale';
      scale.innerHTML = [-48, -36, -24, -12, -6, 0]
        .map((v) => `<span style="left:${meterPos(v)}%">${v}</span>`).join('');
      root.append(scale);
      return rows;
    }
    if (!inputOnly) {
      root.querySelector('.agc-scale').innerHTML = [-15, -10, -5, 0, 5, 10, 15]
        .map((v) => `<span style="left:${50 + (v / AGC_RANGE_DB) * 50}%">${v > 0 ? '+' + v : v}</span>`).join('');
    }

    const meters = [{ ring: inRing, rows: buildMeters($('inMeters'), COLORS.in) }];
    if (!inputOnly) {
      meters.push({ ring: outRing, rows: buildMeters($('outMeters'), COLORS.out) });
    }

    // ---- Waveform drawing -------------------------------------------------

    function fit(wv) {
      const r = wv.canvas.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      const w = Math.max(1, Math.round(r.width));
      const h = Math.max(1, Math.round(r.height));
      if (w !== wv.w || h !== wv.h || dpr !== wv.dpr) {
        Object.assign(wv, { w, h, dpr });
        wv.canvas.width = Math.round(w * dpr);
        wv.canvas.height = Math.round(h * dpr);
      }
    }

    // Distance from the centre line for amplitude a, given the half-height in px.
    function ampToLen(a, half) {
      if (scaleMode === 'lin') return clamp(a, 0, 1) * half;
      const d = toDb(a);
      return d <= FLOOR_DB ? 0 : Math.min(half, ((d - FLOOR_DB) / -FLOOR_DB) * half);
    }

    function drawWave(wv) {
      const { ctx, ring, w, h, dpr } = wv;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);
      const mid = h / 2;
      const half = mid - 2;
      ctx.fillStyle = 'rgba(255,255,255,0.12)';
      ctx.fillRect(0, mid, w, 1);
      ctx.font = '10px ui-monospace, Consolas, monospace';
      ctx.textBaseline = 'bottom';
      for (const g of GRID_DB) {
        const len = ampToLen(fromDb(g), half);
        ctx.fillStyle = 'rgba(127,138,155,0.28)';
        ctx.fillRect(0, mid - len, w, 1);
        ctx.fillRect(0, mid + len, w, 1);
      }
      // One value per pixel (the loudest column landing on it), drawn as a single filled
      // shape per layer: separate bars at fractional positions leave seams between them.
      const bins = Math.max(1, Math.round(w));
      const d = ring.data;
      const series = (off) => {
        const out = new Float32Array(bins);
        for (let k = 0; k < N; k++) {
          const x = Math.min(bins - 1, Math.floor((k * bins) / N));
          const v = d[((ring.head + k) % N) * 4 + off];
          if (v > out[x]) out[x] = v;
        }
        return out;
      };
      const shape = (vals, ch) => {
        ctx.beginPath();
        ctx.moveTo(0, mid);
        for (let i = 0; i < bins; i++) {
          const len = ampToLen(vals[i], half);
          const y = ch ? mid + len : mid - len;
          ctx.lineTo(i, y);
          ctx.lineTo(i + 1, y);
        }
        ctx.lineTo(bins, mid);
        ctx.closePath();
      };
      // Channel 0 (L) up, channel 1 (R) down. Peak is the light layer, RMS the strong one.
      for (let ch = 0; ch < 2; ch++) {
        const peak = series(ch);
        shape(peak, ch);
        ctx.globalCompositeOperation = 'destination-out';  // erase the grid under the waveform
        ctx.fillStyle = '#000';
        ctx.fill();
        ctx.globalCompositeOperation = 'source-over';
        ctx.fillStyle = `rgba(${wv.colors[ch]},0.35)`;
        ctx.fill();
        shape(series(2 + ch), ch);
        ctx.fillStyle = `rgb(${wv.colors[ch]})`;
        ctx.fill();
      }
      // Grid labels last, on a backing box, so the waveform never covers them.
      for (const g of GRID_DB) {
        const label = `${g} dB`;
        const y = mid - ampToLen(fromDb(g), half) - 1;
        ctx.fillStyle = 'rgba(10,12,16,0.75)';
        ctx.fillRect(2, y - 11, ctx.measureText(label).width + 4, 11);
        ctx.fillStyle = 'rgba(127,138,155,0.9)';
        ctx.fillText(label, 4, y);
      }
    }

    // ---- Meters and diff --------------------------------------------------

    function updateMeters(set, now) {
      const short = set.ring.window(30);   // ~300 ms RMS
      const peak = set.ring.window(5);     // ~50 ms peak
      for (const row of set.rows) {
        const pkD = toDb(peak.pk[row.ch]);
        const rmsD = toDb(short.rms[row.ch]);
        if (!Number.isFinite(row.held) || pkD >= row.held || now - row.heldAt > 1500) {
          row.held = pkD;
          row.heldAt = now;
        }
        row.pk.style.width = meterPos(pkD) + '%';
        row.rms.style.width = meterPos(rmsD) + '%';
        row.hold.style.left = meterPos(row.held) + '%';
        row.hold.style.opacity = Number.isFinite(row.held) ? '1' : '0';
        row.rv.textContent = `RMS ${fmtDb(rmsD)}`;
        row.pv.textContent = `pk ${fmtDb(pkD)}`;
      }
    }

    function diffDb() {
      const pw = (r) => (r[0] * r[0] + r[1] * r[1]) / 2;
      const i = inRing.window(300).rms;
      const o = outRing.window(300).rms;
      return toDb(Math.sqrt(pw(o))) - toDb(Math.sqrt(pw(i)));
    }

    // The AGC gain as a bar from the centre: right for boost, left for cut.
    function updateAgc() {
      const known = Number.isFinite(agcGain);
      const g = known ? clamp(agcGain, -AGC_RANGE_DB, AGC_RANGE_DB) : 0;
      const w = (Math.abs(g) / AGC_RANGE_DB) * 50;
      const fill = $('agcFill');
      fill.style.left = (g < 0 ? 50 - w : 50) + '%';
      fill.style.width = w + '%';
      fill.classList.toggle('cut', g < 0);
      $('agcVal').textContent = known ? `${agcGain >= 0 ? '+' : '−'}${Math.abs(agcGain).toFixed(1)} dB` : '--';
      const bar = $('agcBar');
      bar.classList.toggle('off', agcBypass);
      bar.title = agcBypass ? 'AGC bypassed: the gain is still tracked but not applied' : '';
    }

    // Columns arrive in bursts (the output more than the input), so each side plays them
    // out at a steady 100 per second from a small buffer instead of drawing them on arrival.
    const COL_MS = 10;
    const BUFFER_COLS = 20;   // ~200 ms of jitter buffer (events arrive every 100 ms)
    const MAX_QUEUE = 100;    // over ~1 s behind: skip ahead to the buffer target
    class Playout {
      constructor(ring) { this.ring = ring; this.q = []; this.pos = 0; this.playing = false; this.delay = 0; }
      get target() { return BUFFER_COLS + this.delay; }
      // Hold this side back by delta more columns (negative: catch up by dropping columns).
      adjust(delta) {
        this.delay += delta;
        if (delta > 0) this.pos -= delta;
        else this.q.splice(0, Math.min(-delta, this.q.length));
      }
      add(cols) {
        if (!Array.isArray(cols)) return;
        for (const col of cols) if (Array.isArray(col) && col.length >= 4) this.q.push(col);
        if (this.q.length > MAX_QUEUE + this.delay) this.q.splice(0, this.q.length - this.target);
      }
      // Advance by dt ms. Returns true when columns were added to the ring.
      tick(dt) {
        if (!this.playing) {
          if (this.q.length < this.target) return false;
          this.playing = true;
          this.pos = 0;
        }
        // Play slightly faster or slower to hold the queue near the buffer target.
        const rate = clamp(1 + (this.q.length - this.target) * 0.01, 0.9, 1.25);
        this.pos += (dt / COL_MS) * rate;
        let n = Math.floor(this.pos);
        this.pos -= n;
        let pushed = false;
        while (n-- > 0) {
          const col = this.q.shift();
          if (!col) { this.playing = false; break; }  // ran dry: buffer again
          this.ring.push(col);
          pushed = true;
        }
        return pushed;
      }
    }
    const playouts = [new Playout(inRing), new Playout(outRing)];
    let lastFrame = 0;
    const DRAW_MS = 30;  // redraw about 30 times a second
    let lastDraw = 0;

    // The input is measured before the processing and the output after it, so the output
    // trails. Find that lag by cross-correlating the two RMS envelopes as displayed, and
    // hold the input back by it. After a change, wait until the shown history reflects it.
    const SYNC_WINDOW = 300;   // columns compared (3 s)
    const SYNC_MAX_LAG = 150;  // up to 1.5 s either way of the current delay
    let nextSync = 0;
    function envelopeDb(ring, k) {
      const o = ((ring.head - 1 - k + 2 * N) % N) * 4;
      return Math.max(-60, toDb(Math.max(ring.data[o + 2], ring.data[o + 3])));
    }
    function measureLag() {
      const x = [], y = [];
      for (let k = 0; k < SYNC_WINDOW + 2 * SYNC_MAX_LAG; k++) { x.push(envelopeDb(inRing, k)); y.push(envelopeDb(outRing, k)); }
      let best = 0, bestCorr = -1;
      for (let lag = -SYNC_MAX_LAG; lag <= SYNC_MAX_LAG; lag++) {
        let sx = 0, sy = 0, sxx = 0, syy = 0, sxy = 0, n = 0;
        for (let k = SYNC_MAX_LAG; k < SYNC_MAX_LAG + SYNC_WINDOW; k++) {
          const a = x[k + lag], b = y[k];
          sx += a; sy += b; sxx += a * a; syy += b * b; sxy += a * b; n++;
        }
        const cov = sxy - (sx * sy) / n;
        const den = Math.sqrt((sxx - (sx * sx) / n) * (syy - (sy * sy) / n));
        const corr = den > 1e-6 ? cov / den : -1;
        if (corr > bestCorr) { bestCorr = corr; best = lag; }
      }
      return { lag: best, corr: bestCorr };
    }
    function syncSides(now) {
      if (now < nextSync) return;
      nextSync = now + 1000;
      const { lag, corr } = measureLag();
      if (corr < 0.6 || Math.abs(lag) < 2) return;
      // Positive lag: the input showed this content lag columns earlier than the output.
      const delta = Math.max(-playouts[0].delay, lag);
      if (!delta) return;
      playouts[0].adjust(delta);
      nextSync = now + SYNC_WINDOW * COL_MS + 2 * SYNC_MAX_LAG * COL_MS + 1000;
    }

    function frame(now) {
      requestAnimationFrame(frame);
      const dt = lastFrame ? Math.min(now - lastFrame, 100) : 0;
      lastFrame = now;
      for (const p of playouts) if (p.tick(dt)) dirty = true;
      syncSides(now);
      if (!dirty || now - lastDraw < DRAW_MS) return;
      dirty = false;
      lastDraw = now;
      for (const wv of waves) { fit(wv); drawWave(wv); }
      for (const set of meters) updateMeters(set, now);
      if (inputOnly) return;
      updateAgc();
      const df = diffDb();
      $('diff').textContent = Number.isFinite(df)
        ? `${df >= 0 ? '+' : '−'}${Math.abs(df).toFixed(1)} dB` : '--';
    }

    // ---- Meter stream -----------------------------------------------------

    function setConn(state) {
      if (state === connState) return;
      connState = state;
      if (!$('conn')) return;  // the input-only view has no connection indicator
      $('conn').dataset.state = state;
      $('connText').textContent = CONN_TEXT[state];
    }

    function connectMeters() {
      const src = new EventSource(apiUrl('api/meters'));
      es = src;
      src.onopen = () => { lastMsg = performance.now(); setConn('live'); };
      src.onmessage = (ev) => {
        let m;
        try { m = JSON.parse(ev.data); } catch (_) { return; }
        lastMsg = performance.now();
        playouts[0].add(m.in);
        playouts[1].add(m.out);
        if ('agc' in m) { agcGain = typeof m.agc === 'number' ? m.agc : null; dirty = true; }
        setConn('live');
      };
      src.onerror = () => {
        if (!running || es !== src) return;
        if (src.readyState === EventSource.CLOSED) {
          src.close();
          setConn('offline');
          setTimeout(() => { if (running && es === src) connectMeters(); }, 3000);
        } else {
          setConn('reconnecting');
        }
      };
    }

    // ---- API helpers ------------------------------------------------------

    async function api(method, url, body) {
      const opt = { method, cache: 'no-store', headers: {} };
      if (body !== undefined) {
        opt.headers['Content-Type'] = 'application/json';
        opt.body = JSON.stringify(body);
      }
      const r = await fetch(apiUrl(url), opt);
      const text = await r.text();
      let j = null;
      try { j = text ? JSON.parse(text) : null; } catch (_) { j = null; }
      if (!r.ok) {
        const err = new Error((j && j.error) || `HTTP ${r.status}`);
        err.status = r.status;
        throw err;
      }
      return j;
    }

    function toast(msg, kind) {
      const t = document.createElement('div');
      t.className = 'toast' + (kind === 'err' ? ' err' : '');
      t.textContent = msg;
      const host = $('toasts');
      host.append(t);
      while (host.children.length > 4) host.firstChild.remove();
      setTimeout(() => t.remove(), 4500);
    }

    function setNote(msg) { $('note').textContent = msg; }

    function renderBypass() {
      const b = $('bypass');
      b.setAttribute('aria-pressed', String(bypass));
      b.querySelector('.state').textContent = bypass ? 'BYPASSED' : 'PROCESSING';
      if (EMBEDDED) {
        b.classList.toggle('btn-danger', bypass);
        b.classList.toggle('btn-outline-danger', !bypass);
      }
      b.disabled = !control;
      b.title = control ? '' : 'Live control unavailable';
      waves[1].wrap.classList.toggle('bypassed', bypass);
      updateAgc();
      const a = $('agc');
      a.setAttribute('aria-pressed', String(agcBypass));
      a.querySelector('.state').textContent = agcBypass ? 'BYPASSED' : 'AGC ON';
      if (EMBEDDED) {
        a.classList.toggle('btn-danger', agcBypass);
        a.classList.toggle('btn-outline-danger', !agcBypass);
      }
    }

    function applyState(s) {
      control = !!s.control;
      bypass = !!s.bypass;
      agcBypass = !!s.agc_bypass;
      $('notice').classList.toggle('show', !control);
      renderBypass();
    }

    async function loadState() {
      try {
        const s = await api('GET', 'api/state');
        if (stateDown) toast('Connected to processor');
        stateDown = false;
        setNote('');
        applyState(s);
      } catch (e) {
        if (!stateDown) toast(`Processor state unavailable: ${e.message}`, 'err');
        stateDown = true;
        setNote('Processor unreachable, retrying');
      }
    }

    // ---- Footer and header actions ---------------------------------------

    if (!inputOnly) {
      $('bypass').addEventListener('click', async () => {
        const b = $('bypass');
        b.disabled = true;
        try {
          applyState(await api('POST', 'api/bypass', { bypass: !bypass }));
        } catch (e) {
          toast(`Bypass: ${e.message}`, 'err');
        } finally {
          b.disabled = !control;
        }
      });

      $('agc').addEventListener('click', async () => {
        const a = $('agc');
        a.disabled = true;
        try {
          applyState(await api('POST', 'api/agc', { bypass: !agcBypass }));
        } catch (e) {
          toast(`AGC bypass: ${e.message}`, 'err');
        } finally {
          a.disabled = false;
        }
      });
    }

    root.querySelectorAll('[data-scale]').forEach((btn) => {
      btn.addEventListener('click', () => {
        scaleMode = btn.dataset.scale;
        root.querySelectorAll('[data-scale]').forEach((b) => {
          b.setAttribute('aria-pressed', String(b === btn));
          if (EMBEDDED) b.classList.toggle('active', b === btn);
        });
        dirty = true;
      });
    });

    // ---- Processing graph ---------------------------------------------------

    let graphServerText = null;  // the source the processor runs, as the server reports it
    let graphEdited = false;     // the textarea holds text the user typed and has not applied

    function showGraph(g, force) {
      graphServerText = g.text;
      if (force || !graphEdited) {
        $('graphText').value = g.text;
        graphEdited = false;
      }
      const origin = g.origin === 'file' ? 'Matches processing.ffmpeg' : 'Differs from processing.ffmpeg (temporary)';
      $('graphOrigin').textContent = origin;
      const sw = g.swap || {};
      const line = $('graphSwap');
      line.className = 'st-line muted';
      if (sw.state === 'warming') {
        line.textContent = 'Starting the new graph...';
      } else if (sw.state === 'error') {
        line.className = 'st-line err';
        line.textContent = `Last change not applied: ${sw.error || 'unknown error'}`;
      } else {
        line.textContent = '';
      }
      $('graphApply').disabled = false;
      $('graphRevert').disabled = false;
    }

    async function loadGraph() {
      try {
        showGraph(await api('GET', 'api/graph'));
      } catch (_) {
        $('graphApply').disabled = true;
        $('graphRevert').disabled = true;
      }
    }

    async function applyGraph() {
      const btn = $('graphApply');
      btn.disabled = true;
      try {
        const g = await api('POST', 'api/graph', { text: $('graphText').value });
        toast('Processing graph applied');
        showGraph(g, true);
        await loadState();
      } catch (e) {
        toast(`Graph not applied: ${e.message}`, 'err');
        await loadGraph();
      } finally {
        btn.disabled = false;
      }
    }

    async function revertGraph() {
      if (graphEdited && !window.confirm('Discard the edited graph and run processing.ffmpeg?')) return;
      try {
        const g = await api('POST', 'api/graph/reload', {});
        toast('Reverted to processing.ffmpeg');
        showGraph(g, true);
        await loadState();
      } catch (e) {
        toast(`Revert failed: ${e.message}`, 'err');
      }
    }

    if (!inputOnly) {
      $('graphText').addEventListener('input', () => {
        graphEdited = $('graphText').value !== graphServerText;
      });
      $('graphApply').addEventListener('click', applyGraph);
      $('graphRevert').addEventListener('click', revertGraph);
    }

    // ---- Start -------------------------------------------------------------

    if (EMBEDDED) {
      root.querySelectorAll('button').forEach((b) => b.classList.add('btn', 'btn-sm', 'btn-outline-secondary'));
      root.querySelectorAll('.bypass').forEach((b) => {
        b.classList.remove('btn-sm', 'btn-outline-secondary');
        b.classList.add('btn-outline-danger');
      });
      if (!inputOnly) {
        root.querySelector('.seg').classList.add('btn-group', 'btn-group-sm');
        $('graphApply').classList.replace('btn-outline-secondary', 'btn-outline-primary');
        $('graphText').classList.add('form-control');
        renderBypass();
      }
      root.querySelectorAll('[data-scale]').forEach((b) => b.classList.toggle('active', b.getAttribute('aria-pressed') === 'true'));
    }

    // start() and stop() let a host page (the admin panel) pause the meter stream and the
    // state polling while the monitor is hidden, so it uses no bandwidth then. The input-only
    // view polls nothing but the meters.
    let running = false;
    let timers = [];
    function start() {
      if (running) return;
      running = true;
      lastMsg = performance.now();
      timers = [
        setInterval(() => {
          if (es && es.readyState === EventSource.OPEN && performance.now() - lastMsg > 2500) setConn('nodata');
        }, 1000),
      ];
      if (!inputOnly) timers.push(setInterval(loadState, 10000), setInterval(loadGraph, 5000));
      connectMeters();
      if (!inputOnly) {
        loadState();
        loadGraph();
      }
    }
    function stop() {
      running = false;
      timers.forEach(clearInterval);
      timers = [];
      if (es) { es.close(); es = null; }
      setConn('offline');
    }
    root.jmmMonitor = { start, stop };

    for (const wv of waves) new ResizeObserver(() => { dirty = true; }).observe(wv.canvas);
    requestAnimationFrame(frame);
    return root.jmmMonitor;
  }

  window.JmmMonitor = { mount };
})();
