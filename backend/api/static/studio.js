// Studio: listen to the Spotify input before processing, and talk into the audio input.
// Both run over WebSockets to the admin proxy. Listen receives s16le stereo 44.1 kHz PCM,
// Talk sends the microphone as s16le mono 44.1 kHz PCM. The two are independent.
// The controls live in the admin page's deck (id="deck"), which owns the markup and layout.
(() => {
  'use strict';

  // data-base is the URL prefix of the admin proxy to the audio container; the admin key
  // comes from the page's ?key=, or from the admin_key cookie that the admin page sets.
  const root = document.getElementById('deck');
  if (!root) return;
  const API_BASE = root.dataset.base || '/admin/audio/';
  const API_KEY = new URLSearchParams(window.location.search).get('key') || '';
  const withKey = (path) => path
    + (API_KEY ? (path.includes('?') ? '&' : '?') + 'key=' + encodeURIComponent(API_KEY) : '');
  const css = document.createElement('link');
  css.rel = 'stylesheet';
  css.href = withKey(API_BASE + 'studio.css');
  document.head.append(css);

  const RATE = 44100;
  // Listen jitter buffer, in seconds: start at about 175 ms, and trim back to that when more
  // than 500 ms has queued, so latency cannot creep up.
  const LISTEN_TARGET = 0.175;
  const LISTEN_MAX = 0.5;
  // Talk sends 20 ms frames. A frame is dropped when more than 250 ms is still waiting to
  // leave the socket, so a slow connection costs audio rather than adding latency.
  const MIC_FRAME = RATE / 50;
  const MIC_MAX_BUFFERED = RATE * 2 * 0.25;

  // Both processors live in one module, loaded once per AudioContext from a Blob URL.
  // The listen ring buffer is sized in frames of the context rate.
  const WORKLET_SRC = `
    class JmmListen extends AudioWorkletProcessor {
      constructor(options) {
        super();
        const o = options.processorOptions;
        this.size = o.size;
        this.target = o.target;
        this.max = o.max;
        this.left = new Float32Array(this.size);
        this.right = new Float32Array(this.size);
        this.read = 0;
        this.write = 0;
        this.count = 0;
        this.primed = false;
        this.reported = 0;
        this.port.onmessage = (e) => this.push(e.data.left, e.data.right);
      }
      push(left, right) {
        let n = left.length;
        let skip = 0;
        if (n > this.size) { skip = n - this.size; n = this.size; }
        const over = this.count + n - this.size;
        if (over > 0) this.drop(over);
        for (let i = 0; i < n; i++) {
          this.left[this.write] = left[skip + i];
          this.right[this.write] = right[skip + i];
          this.write = (this.write + 1) % this.size;
        }
        this.count += n;
        if (this.count > this.max) this.drop(this.count - this.target);
      }
      drop(k) {
        this.read = (this.read + k) % this.size;
        this.count -= k;
      }
      process(inputs, outputs) {
        const l = outputs[0][0];
        const r = outputs[0][1];
        if (!this.primed && this.count >= this.target) this.primed = true;
        for (let i = 0; i < l.length; i++) {
          if (this.primed && this.count > 0) {
            l[i] = this.left[this.read];
            r[i] = this.right[this.read];
            this.read = (this.read + 1) % this.size;
            this.count--;
          } else {
            l[i] = 0;
            r[i] = 0;
            this.primed = false;
          }
        }
        this.reported += l.length;
        if (this.reported >= sampleRate / 5) {
          this.reported = 0;
          this.port.postMessage({ buffered: this.count, primed: this.primed });
        }
        return true;
      }
    }
    class JmmCapture extends AudioWorkletProcessor {
      process(inputs) {
        const channel = inputs[0] && inputs[0][0];
        if (channel && channel.length) this.port.postMessage(channel.slice(0));
        return true;
      }
    }
    registerProcessor('jmm-listen', JmmListen);
    registerProcessor('jmm-capture', JmmCapture);
  `;
  let workletUrl = null;

  // Some browsers refuse a sample rate. The resamplers adapt to whatever rate is granted.
  function newContext() {
    try {
      return new AudioContext({ sampleRate: RATE });
    } catch (err) {
      return new AudioContext();
    }
  }

  async function loadWorklet(ctx) {
    if (!workletUrl) {
      workletUrl = URL.createObjectURL(new Blob([WORKLET_SRC], { type: 'text/javascript' }));
    }
    await ctx.audioWorklet.addModule(workletUrl);
  }

  // Linear resampler for one channel. It keeps the last sample and the fractional position
  // between chunks, so block edges stay seamless. It passes through when the rates match.
  class Resampler {
    constructor(from, to) {
      this.step = from / to;
      this.prev = 0;
      this.pos = 0;
    }

    process(input) {
      if (this.step === 1) return input;
      const n = input.length;
      if (n === 0) return new Float32Array(0);
      const out = [];
      let x = this.pos;
      while (x < n) {
        const i = Math.floor(x);
        const f = x - i;
        const a = i === 0 ? this.prev : input[i - 1];
        out.push(a + (input[i] - a) * f);
        x += this.step;
      }
      this.pos = x - n;
      this.prev = input[n - 1];
      return Float32Array.from(out);
    }
  }

  const listenBtn = document.getElementById('st-listen');
  const volume = document.getElementById('st-vol');
  const volumeLabel = document.getElementById('st-volVal');
  const listenStatus = document.getElementById('st-listenStatus');
  const micBtn = document.getElementById('st-mic-toggle');
  const micSelect = document.getElementById('st-mic');
  const micStatus = document.getElementById('st-micStatus');
  const meter = document.getElementById('st-meter');

  function setStatus(el, text, error) {
    el.textContent = text;
    el.className = error ? 'text-danger' : 'text-secondary';
  }

  // Toggle state lives in aria-pressed; the deck's CSS colours the pressed state.
  function setPressed(btn, on, label) {
    btn.setAttribute('aria-pressed', String(on));
    btn.textContent = label;
  }

  function gainValue() {
    return Number(volume.value) / 100;
  }

  volume.addEventListener('input', () => {
    volumeLabel.textContent = volume.value + '%';
    if (listen && listen.gain) listen.gain.gain.setTargetAtTime(gainValue(), listen.ctx.currentTime, 0.02);
  });

  // ─── Listen ────────────────────────────────────────────────────────────────

  // The active listen session, or null. Only one runs at a time.
  let listen = null;

  function listenWsUrl() {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    return proto + '//' + window.location.host + withKey(API_BASE + 'listen');
  }

  function startListen() {
    const session = { ctx: newContext(), ws: null, node: null, gain: null };
    listen = session;
    setPressed(listenBtn, true, 'Listen: On');
    setStatus(listenStatus, 'Connecting');
    syncInputGraph();
    pumpListen(session).catch((err) => {
      if (listen === session) stopListen('Listen failed: ' + err.message, true);
    });
  }

  async function pumpListen(l) {
    const ctx = l.ctx;
    await ctx.resume();
    await loadWorklet(ctx);
    if (listen !== l) return;
    const gain = ctx.createGain();
    gain.gain.value = gainValue();
    const node = new AudioWorkletNode(ctx, 'jmm-listen', {
      numberOfInputs: 0,
      numberOfOutputs: 1,
      outputChannelCount: [2],
      processorOptions: {
        size: Math.round(ctx.sampleRate * 2),
        target: Math.round(ctx.sampleRate * LISTEN_TARGET),
        max: Math.round(ctx.sampleRate * LISTEN_MAX),
      },
    });
    node.port.onmessage = (e) => {
      if (!listen || listen.node !== node) return;
      const ms = Math.round(e.data.buffered / ctx.sampleRate * 1000);
      setStatus(listenStatus, e.data.primed ? 'Live, buffer ' + ms + ' ms' : 'Buffering');
    };
    node.connect(gain).connect(ctx.destination);
    l.node = node;
    l.gain = gain;

    const leftResampler = new Resampler(RATE, ctx.sampleRate);
    const rightResampler = new Resampler(RATE, ctx.sampleRate);
    const ws = new WebSocket(listenWsUrl());
    ws.binaryType = 'arraybuffer';
    l.ws = ws;
    ws.onmessage = (e) => {
      if (listen !== l) return;
      // The server sends whole stereo frames in each message, so no bytes carry over.
      const frames = e.data.byteLength >> 2;
      const view = new DataView(e.data);
      const left = new Float32Array(frames);
      const right = new Float32Array(frames);
      for (let i = 0; i < frames; i++) {
        left[i] = view.getInt16(i * 4, true) / 32768;
        right[i] = view.getInt16(i * 4 + 2, true) / 32768;
      }
      node.port.postMessage({ left: leftResampler.process(left), right: rightResampler.process(right) });
    };
    ws.onclose = (e) => {
      if (listen === l) stopListen(closeMessage(e), e.code !== 1000);
    };
  }

  // Stops listening. Pass error=true to show the message as an error.
  function stopListen(message, error) {
    const l = listen;
    listen = null;
    if (!l) return;
    if (l.ws) l.ws.close();
    if (l.node) l.node.disconnect();
    if (l.gain) l.gain.disconnect();
    l.ctx.close().catch(() => {});
    setPressed(listenBtn, false, 'Listen: Off');
    setStatus(listenStatus, message || 'Off', error);
    syncInputGraph();
  }

  listenBtn.addEventListener('click', () => {
    if (listen) stopListen(); else startListen();
  });

  // ─── Input graph ───────────────────────────────────────────────────────────

  // The input graph under the deck is the audio monitor's input view. It is shown, and its
  // meter stream runs, only while Listen is on and the page is visible. Otherwise the stream
  // is closed. The monitor script is loaded on first use (window.loadAudioMonitor, from the page).
  const inputHost = document.getElementById('deckInput');
  let inputMonitor = null;  // the monitor's { start, stop } handle, once mounted

  function syncInputGraph() {
    inputHost.hidden = !listen;
    if (!listen || document.visibilityState !== 'visible') {
      if (inputMonitor) inputMonitor.stop();
      return;
    }
    if (inputMonitor) {
      inputMonitor.start();
      return;
    }
    if (!window.loadAudioMonitor) return;
    window.loadAudioMonitor(inputHost).then((monitor) => {
      inputMonitor = monitor;
      syncInputGraph();
    }).catch((err) => console.error(err));
  }

  document.addEventListener('visibilitychange', syncInputGraph);

  // ─── Mic ───────────────────────────────────────────────────────────────────

  // The active talk session, or null. A session owns its stream, context, socket and frame.
  let talk = null;

  function micWsUrl() {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    return proto + '//' + window.location.host + withKey(API_BASE + 'mic');
  }

  // Releases whatever a session holds. Safe to call more than once and at any point.
  function releaseTalk(t) {
    t.closed = true;
    if (t.raf) cancelAnimationFrame(t.raf);
    t.raf = 0;
    if (t.ws) {
      const ws = t.ws;
      t.ws = null;
      ws.close();
    }
    if (t.node) {
      t.node.port.onmessage = null;
      t.node.disconnect();
    }
    if (t.source) t.source.disconnect();
    if (t.mute) t.mute.disconnect();
    if (t.stream) t.stream.getTracks().forEach((track) => track.stop());
    if (t.ctx) t.ctx.close().catch(() => {});
    t.node = t.source = t.mute = t.stream = t.ctx = null;
  }

  function stopTalk(t, message, error) {
    if (talk === t) talk = null;
    releaseTalk(t);
    meter.style.width = '0%';
    meter.classList.remove('hot', 'clip');
    setPressed(micBtn, false, 'Mic: Off');
    micSelect.disabled = false;
    setStatus(micStatus, message || 'Off', error);
  }

  function closeMessage(e) {
    if (e.reason) return e.reason;
    if (e.code === 1008) return 'Refused: the admin session or origin was rejected';
    return 'Connection closed (code ' + e.code + ')';
  }

  // Converts one block from the capture worklet and sends whole 20 ms frames.
  function onMicBlock(t, block) {
    let peak = 0;
    for (let i = 0; i < block.length; i++) {
      const a = Math.abs(block[i]);
      if (a > peak) peak = a;
    }
    if (peak > t.peak) t.peak = peak;
    const out = t.resampler.process(block);
    for (let i = 0; i < out.length; i++) {
      const s = Math.max(-1, Math.min(1, out[i]));
      t.frame[t.fill++] = s * 32767;
      if (t.fill === MIC_FRAME) {
        const ws = t.ws;
        if (ws && ws.readyState === WebSocket.OPEN && ws.bufferedAmount <= MIC_MAX_BUFFERED) {
          ws.send(t.frame.buffer);
        }
        t.frame = new Int16Array(MIC_FRAME);
        t.fill = 0;
      }
    }
  }

  function drawMeter(t) {
    if (talk !== t) return;
    t.level = Math.max(t.peak, t.level * 0.92);
    t.peak = 0;
    meter.style.width = Math.min(100, t.level * 100).toFixed(1) + '%';
    meter.classList.toggle('hot', t.level > 0.7);
    meter.classList.toggle('clip', t.level >= 0.99);
    t.raf = requestAnimationFrame(() => drawMeter(t));
  }

  async function startTalk() {
    const t = {
      closed: false, ctx: null, stream: null, source: null, node: null, mute: null, ws: null,
      resampler: null, frame: new Int16Array(MIC_FRAME), fill: 0, peak: 0, level: 0, raf: 0,
    };
    talk = t;
    setPressed(micBtn, true, 'Mic: LIVE');
    micSelect.disabled = true;
    setStatus(micStatus, 'Opening microphone');
    try {
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        throw new Error('the browser has no microphone access (HTTPS is required)');
      }
      const deviceId = micSelect.value;
      t.stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: false,
          noiseSuppression: false,
          autoGainControl: false,
          channelCount: 1,
          ...(deviceId ? { deviceId: { exact: deviceId } } : {}),
        },
      });
      if (t.closed) return releaseTalk(t);
      refreshDevices(); // Device labels become available once permission is granted.

      t.ctx = newContext();
      t.resampler = new Resampler(t.ctx.sampleRate, RATE);
      await t.ctx.resume();
      await loadWorklet(t.ctx);
      if (t.closed) return releaseTalk(t);

      t.source = t.ctx.createMediaStreamSource(t.stream);
      t.node = new AudioWorkletNode(t.ctx, 'jmm-capture', {
        numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1],
      });
      // The capture node must be pulled by the destination to run. The gain is zero, so nothing is heard.
      t.mute = t.ctx.createGain();
      t.mute.gain.value = 0;
      t.source.connect(t.node);
      t.node.connect(t.mute).connect(t.ctx.destination);
      t.node.port.onmessage = (e) => onMicBlock(t, e.data);

      const ws = new WebSocket(micWsUrl());
      ws.binaryType = 'arraybuffer';
      t.ws = ws;
      ws.onopen = () => {
        if (talk === t) setStatus(micStatus, 'Live');
      };
      ws.onclose = (e) => {
        if (talk === t) stopTalk(t, closeMessage(e), e.code !== 1000);
      };
      drawMeter(t);
      setStatus(micStatus, 'Connecting');
    } catch (err) {
      if (talk === t) stopTalk(t, 'Mic failed: ' + err.message, true);
      else releaseTalk(t);
    }
  }

  micBtn.addEventListener('click', () => {
    if (talk) stopTalk(talk, 'Off'); else startTalk();
  });

  // Lists the audio inputs. Labels stay blank until the microphone has been granted once.
  async function refreshDevices() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return;
    const devices = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === 'audioinput');
    const current = micSelect.value;
    micSelect.options.length = 0;
    micSelect.add(new Option('Default microphone', ''));
    devices.forEach((d, i) => micSelect.add(new Option(d.label || 'Microphone ' + (i + 1), d.deviceId)));
    if (devices.some((d) => d.deviceId === current)) micSelect.value = current;
  }

  if (navigator.mediaDevices && navigator.mediaDevices.addEventListener) {
    navigator.mediaDevices.addEventListener('devicechange', refreshDevices);
  }
  refreshDevices();

  // Leaving the page releases the microphone and the speakers.
  window.addEventListener('pagehide', () => {
    if (listen) stopListen();
    if (talk) stopTalk(talk, 'Off');
  });
})();
