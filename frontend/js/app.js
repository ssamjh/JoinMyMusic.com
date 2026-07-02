const audioStream = document.getElementById("audio-stream");
const buttonToggle = document.getElementById("button-toggle");
const playingImage = document.getElementById("playing-image");
let isPlaying = false;
let pollInterval = null;
let lastVotedSongId = null;
let mutedSongId = null;
let songId = null;
let selectedSongData = null;
let hlsFailed = false;
let hlsInstance = null;

// Display-only mode: index.html?display — strip controls/footer, force dark
// theme + effects on, and autoplay. Used for unattended screens/kiosks.
const DISPLAY_MODE = /[?&]display\b/i.test(location.search);
if (DISPLAY_MODE) document.body.classList.add("display-mode");

// --- Web Audio graph (built lazily, only when the visualizer needs it) ---
let audioContext = null;
let audioStartTimer = null; // delays fx playback until the record/arm are in place
let analyser = null;
let audioSource = null;
let gainNode = null;        // master volume (final node before destination)
let currentGain = 1.0;
// Scratch graph: the live stream and a scrubbable buffer-player are crossfaded
// into a shared mix bus, which the analyser taps and the volume node feeds out.
//   source ─┬─→ liveGain ──────────────┐
//           └─→ scratchNode → scratchGain ┤→ mixBus ─┬─→ analyser (tap)
//                                                     └─→ gainNode → destination
let liveGain = null, scratchGain = null, mixBus = null;
let scratchNode = null, scratchReady = false, scratchModulePromise = null;

// --- Visualizer canvas + state ---
// Colours are { r, g, b }. "primary" = the dominant/bass colour, "presence" =
// the secondary/vocal colour. The visualizer paints with these instantly.
let vizCanvas, vizCtx, vizRafId;
let vizPrimary = { r: 80, g: 80, b: 80 };
let vizPresence = { r: 80, g: 80, b: 80 };
let smoothedBass = 0;      // 0..1 low-frequency energy, eased per frame
let smoothedPresence = 0;  // 0..1 vocal-presence energy, eased per frame
let vizDataArray = null;   // reused frequency buffer (allocated once, see drawViz)

// --- Background glow canvas + state ---
// Same primary/presence colours, but each frame the live value eases toward its
// *Target, so the backdrop drifts to a new palette rather than snapping.
let bgCanvas, bgCtx, bgRafId, bgLastTime = 0;
let bgPrimary = { r: 20, g: 20, b: 20 };
let bgPrimaryTarget = { r: 20, g: 20, b: 20 };
let bgPresence = { r: 20, g: 20, b: 20 };
let bgPresenceTarget = { r: 20, g: 20, b: 20 };
// Idle-skip state for drawBgLoop: last-painted energy + a force-redraw flag for
// invalidations that aren't captured by the colour/energy values themselves.
let bgLastBass = -1, bgLastPresence = -1, bgForceDraw = true;

// Palette extracted from the next cover, staged until the record actually swaps
// to it — so the backdrop / side glow never change colour ahead of the artwork.
let pendingColors = null;

// Tonearm progress globals — drive the arm from the rim to the centre
const TONEARM_SWEEP_DEG = 29.32; // rim → just before centre (runout, ~15% radius)
const TONEARM_PARK_DEG = -10;    // swung off the side of the record (rest)
// Anchored against performance.now() (monotonic) rather than Date.now(), so the
// arm position is immune to the listener's wall clock being wrong/skewed. Set
// from the server's elapsed_ms — the position currently being heard — on every
// metadata event (song change, in-track seek, or reconnect snapshot).
let songStartedAtPerf = null;
let songDurationMs = null;
let tonearmSongId = null; // song the arm timing is locked to
let tonearmChoreography = false; // true while the scripted swing-off/on is running
// Pending timeouts of the running swap sequence, so a second song change (e.g.
// a skip landing right at a track boundary) or a stop can abort it cleanly
// instead of two choreographies fighting over the arm and the dim state.
let choreoTimers = [];
function choreoTimeout(fn, ms) {
  choreoTimers.push(setTimeout(fn, ms));
}
function cancelChoreography() {
  choreoTimers.forEach(clearTimeout);
  choreoTimers = [];
  const cardEl = document.getElementById('art-card');
  const titleEl = document.querySelector('.div-playing-title');
  const artistEl = document.querySelector('.div-playing-artist');
  if (cardEl) cardEl.classList.remove('flip-swap', 'fade-swap');
  if (titleEl) titleEl.classList.remove('fade-swap-text');
  if (artistEl) artistEl.classList.remove('fade-swap-text');
  if (tonearmChoreography) {
    const armEl = document.getElementById('tonearm');
    if (armEl) armEl.style.transition = ''; // restore play-time (linear) transition
    tonearmChoreography = false;
  }
  document.body.classList.remove('fx-dimmed');
}
function updateTonearm() {
  const el = document.getElementById("tonearm");
  if (!el) return;
  if (songStartedAtPerf == null || !songDurationMs) {
    el.classList.remove("ready");
    return;
  }
  el.classList.add("ready");
  if (tonearmChoreography) return; // scripted swing in progress — don't fight it
  if (!isPlaying) {
    // Paused — park the arm off to the side; it swings into place on play.
    el.style.transform = `rotate(${TONEARM_PARK_DEG}deg)`;
    return;
  }
  let p = (performance.now() - songStartedAtPerf) / songDurationMs;
  p = p < 0 ? 0 : p > 1 ? 1 : p;
  el.style.transform = `rotate(${(p * TONEARM_SWEEP_DEG).toFixed(2)}deg)`;
}
setInterval(updateTonearm, 500);
// Background tabs throttle the interval above to ~1/min, so snap the arm to the
// right spot the instant we're refocused rather than waiting for the next tick.
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) updateTonearm();
});

// ---- Record spin engine (JS-driven so speed can ramp up/down and stop) ----
const SPIN_FULL = 360 / 8000; // deg per ms → one revolution every 8s
const SPIN_UP_MS = 700;       // spin-up ramp on play (also the audio start delay w/ fx)
const EASE_OUT = (x) => 1 - (1 - x) * (1 - x); // decelerate into a stop
const EASE_IN = (x) => x * x; // accelerate from a stop
let spinEls = null;
let spinAngle = 0, spinSpeed = 0, spinRafId = null, spinPrev = 0;
let spinRampFrom = 0, spinRampTo = 0, spinRampStart = 0, spinRampDur = 0, spinRampEase = EASE_OUT;
// Scratch gesture state (see initRecordScratch). recordGrabbed pauses the spin
// engine so the pointer can drive the record's angle (and the audio) directly.
let recordGrabbed = false, scratchRect = null;
let scratchPrevAngle = 0, scratchPrevTime = 0, scratchVel = 0;
let scratchMoved = false;
function fxOn() {
  return !document.body.classList.contains("no-bg-animation");
}
function startSpinRamp(to, dur, ease) {
  spinRampFrom = spinSpeed;
  spinRampTo = to;
  spinRampStart = performance.now();
  spinRampDur = dur;
  spinRampEase = ease;
  // Run the loop whenever effects are on, so the record keeps turning as it
  // decelerates to a stop even after playback has been paused.
  if (!spinRafId && fxOn()) { spinPrev = 0; spinRafId = requestAnimationFrame(spinFrame); }
}
function spinFrame(t) {
  if (recordGrabbed) { spinRafId = null; return; } // the hand is driving the record
  if (!fxOn()) {
    // Effects off — square-mode art must sit upright; clear rotation and stop.
    spinRafId = null; spinSpeed = 0; spinRampDur = 0;
    if (spinEls) spinEls.forEach((el) => (el.style.transform = ""));
    return;
  }
  const dt = spinPrev ? Math.min(t - spinPrev, 50) : 16;
  spinPrev = t;
  if (spinRampDur > 0) {
    const e = Math.min(1, (t - spinRampStart) / spinRampDur);
    spinSpeed = spinRampFrom + (spinRampTo - spinRampFrom) * spinRampEase(e);
    if (e >= 1) spinRampDur = 0; // hold final speed
  }
  spinAngle = (spinAngle + spinSpeed * dt) % 360;
  if (spinEls) spinEls.forEach((el) => (el.style.transform = `rotate(${spinAngle}deg)`));
  // Keep animating while spinning or still ramping. Once fully at rest, stop
  // the loop and leave the record at its current angle — no snap to upright.
  if (spinSpeed > 0 || spinRampDur > 0) {
    spinRafId = requestAnimationFrame(spinFrame);
  } else {
    spinRafId = null;
  }
}
function ensureSpin() {
  if (!spinEls) {
    spinEls = [
      document.getElementById("playing-image"),
      document.querySelector(".art-placeholder"),
      document.getElementById("record-label"),
    ].filter(Boolean);
  }
  if (recordGrabbed) return;       // a scratch is driving the record directly
  if (tonearmChoreography) return; // song-change sequence is driving the spin
  if (!fxOn()) {
    // Effects off — clear to upright (now if idle, else on the next frame).
    if (spinRafId) spinPrev = 0;
    else if (spinEls) spinEls.forEach((el) => (el.style.transform = ""));
  } else if (document.body.classList.contains("is-playing")) {
    // Playing — spin up to full speed from wherever the record is resting.
    if (spinSpeed < SPIN_FULL) startSpinRamp(SPIN_FULL, SPIN_UP_MS, EASE_IN);
  } else if (spinSpeed > 0 || spinRafId) {
    // Paused — ease the record to a stop, leaving it where it lands.
    startSpinRamp(0, 1500, EASE_OUT);
  }
}

// Background / song-change globals
let bgHasBeenSet = false;
let pendingMetadata = null;       // { data, imgSrc } waiting to be applied
let lastDisplayedSongId = null;   // currently shown song, for change detection

function generateUUID() {
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(
    /[xy]/g,
    function (c) {
      const r = (Math.random() * 16) | 0;
      const v = c === "x" ? r : (r & 0x3) | 0x8;
      return v.toString(16);
    }
  );
}

function getOrCreateUUID() {
  let uuid = storageGet("userUUID");
  if (!uuid) {
    uuid = generateUUID();
    storageSet("userUUID", uuid);
  }
  return uuid;
}

function storageSet(key, value) {
  localStorage.setItem(key, value);
}

function storageGet(key) {
  return localStorage.getItem(key);
}

// --- Theme ---
function applyTheme(theme) {
  document.body.classList.toggle("theme-dark", theme === "dark");
  document.body.classList.toggle("theme-light", theme === "light");
  bgForceDraw = true; // light/dark changes how the glow colours are rendered
  document.querySelector(".theme-icon-dark").style.display =
    theme === "dark" ? "" : "none";
  document.querySelector(".theme-icon-light").style.display =
    theme === "light" ? "" : "none";
}

function toggleTheme() {
  const next = document.body.classList.contains("theme-dark")
    ? "light"
    : "dark";
  applyTheme(next);
  storageSet("ms_theme", next);
}

(function initTheme() {
  if (DISPLAY_MODE) { applyTheme("dark"); return; }
  let theme = storageGet("ms_theme");
  if (!theme) {
    theme =
      window.matchMedia &&
      window.matchMedia("(prefers-color-scheme: light)").matches
        ? "light"
        : "dark";
  }
  applyTheme(theme);
})();

function updateButtons() {
  const voteSkipBtn = document.getElementById("voteSkipBtn");
  const muteSongBtn = document.getElementById("muteSongBtn");

  if (isPlaying) {
    voteSkipBtn.disabled = songId === lastVotedSongId || !songId;
    muteSongBtn.disabled = false;
  } else {
    voteSkipBtn.disabled = true;
    muteSongBtn.disabled = true;
  }
  // Mute icon + active highlight
  const muted = audioStream.muted;
  muteSongBtn.classList.toggle("active", muted && isPlaying);
  muteSongBtn.querySelector(".mute-icon-on").style.display = muted ? "none" : "";
  muteSongBtn.querySelector(".mute-icon-off").style.display = muted ? "" : "none";
}

function updatePlayUI() {
  buttonToggle.querySelector(".play-icon").style.display = isPlaying ? "none" : "";
  buttonToggle.querySelector(".stop-icon").style.display = isPlaying ? "" : "none";
  buttonToggle.title = isPlaying ? "Stop" : "Play";
  document.body.classList.toggle("is-playing", isPlaying);
  ensureSpin();
  updateTonearm(); // swing the arm in on play / park it on pause, immediately
}

function onPlayClick() {
  if (isPlaying) buttonStop();
  else togglePlay();
}

function enableConfirmButton(token) {
  document.getElementById("confirmRequestBtn").disabled = false;
  document.getElementById("confirmRequestBtn").dataset.turnstileToken = token;
}

function enableVoteSkipButton(token) {
  const button = document.getElementById("confirmVoteSkipBtn");
  if (button) {
    button.disabled = false;
    button.dataset.turnstileToken = token;
  }
}

// --- Turnstile (rendered explicitly so each modal owns its widget) ---
// Tokens are single-use, so after every submission the widget that produced the
// token is reset by id. A no-arg turnstile.reset() only targets the first
// widget on the page, which left the vote widget holding a spent token.
const TURNSTILE_SITEKEY = "0x4AAAAAAAeaz0KAvdHl7LvY";
let turnstileRequestId = null;
let turnstileVoteId = null;

// Called by the Turnstile API script (?onload=onTurnstileLoad), which is loaded
// after app.js so this is guaranteed to exist by then.
function onTurnstileLoad() {
  turnstileRequestId = turnstile.render("#turnstile-request", {
    sitekey: TURNSTILE_SITEKEY,
    callback: enableConfirmButton,
    "expired-callback": disarmRequestChallenge,
  });
  turnstileVoteId = turnstile.render("#turnstile-vote", {
    sitekey: TURNSTILE_SITEKEY,
    callback: enableVoteSkipButton,
    "expired-callback": disarmVoteChallenge,
  });
}

// Drop the stored token and disable the button until a fresh token arrives.
function disarmRequestChallenge() {
  const btn = document.getElementById("confirmRequestBtn");
  btn.disabled = true;
  delete btn.dataset.turnstileToken;
}

function disarmVoteChallenge() {
  const btn = document.getElementById("confirmVoteSkipBtn");
  btn.disabled = true;
  delete btn.dataset.turnstileToken;
}

function resetRequestChallenge() {
  disarmRequestChallenge();
  if (window.turnstile && turnstileRequestId !== null) turnstile.reset(turnstileRequestId);
}

function resetVoteChallenge() {
  disarmVoteChallenge();
  if (window.turnstile && turnstileVoteId !== null) turnstile.reset(turnstileVoteId);
}

function pollServer() {
  if (!isPlaying) return;

  const listenerName = storageGet("requesterName") || "";
  const userUUID = getOrCreateUUID();

  const volume = parseInt(storageGet("volume") ?? "50", 10);
  fetch("/api/listener", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ uuid: userUUID, name: listenerName, volume }),
  }).catch((error) => console.error("Error polling server:", error));
}

function startPolling() {
  if (!pollInterval) {
    pollInterval = setInterval(pollServer, 30000); // Poll every 30 seconds
    pollServer(); // Poll immediately
  }
}

function stopPolling() {
  if (pollInterval) {
    clearInterval(pollInterval);
    pollInterval = null;
  }
}

function fallbackToIcecast() {
  audioStream.pause();
  audioStream.removeAttribute('src');
  audioStream.load();
  audioStream.src = "https://icecast.sjh.at/spotifynet";
  audioStream.play().catch(function(e) { console.warn("Icecast play failed:", e); });
}

function icecastFallbackHandler() {
  console.warn("Native HLS error, falling back to Icecast");
  hlsFailed = true;
  fallbackToIcecast();
}

// Deliberate ceiling: the slider maps 0–100 onto 0–30% gain (50 → 10%).
// This is not a bug — don't "fix" it to reach 1.0.
function scaleVolume(inputValue) {
  return inputValue <= 50
    ? inputValue * 0.2
    : 10 + (inputValue - 50) * 0.4;
}

const HLS_STREAM_URL = "https://hls.sjh.at/spotifynet/stream.m3u8";

// hls.js is only needed on the effects-on / no-native-HLS path, so it's loaded
// on demand instead of on every page view (~85 KB gzipped, unused until play).
const HLS_JS_SRC = "https://cdn.jsdelivr.net/npm/hls.js@1.6.16/dist/hls.min.js";
const HLS_JS_INTEGRITY = "sha384-5E8B0pTlZZJMabWpC0fyYf6OUpe15jJij34BqBAh4NXoHAlLNOjCPRrwtOXOQFAn";
let hlsJsPromise = null;
function loadHlsJs() {
  if (window.Hls) return Promise.resolve();
  if (!hlsJsPromise) {
    hlsJsPromise = new Promise(function (resolve, reject) {
      const s = document.createElement("script");
      s.src = HLS_JS_SRC;
      s.integrity = HLS_JS_INTEGRITY;
      s.crossOrigin = "anonymous";
      s.onload = () => resolve();
      s.onerror = () => {
        hlsJsPromise = null; // allow a retry on the next play attempt
        reject(new Error("hls.js failed to load"));
      };
      document.head.appendChild(s);
    });
  }
  return hlsJsPromise;
}

function playNativeHls() {
  // Re-arm the fallback handler without stacking one per call.
  audioStream.removeEventListener("error", icecastFallbackHandler);
  audioStream.src = HLS_STREAM_URL;
  audioStream.addEventListener("error", icecastFallbackHandler, { once: true });
  audioStream.play().catch(function (e) {});
}

function startHlsJs() {
  // Two play attempts can be queued behind the same script download — never
  // let a second instance orphan (and leak) a still-streaming first one.
  if (hlsInstance) {
    hlsInstance.destroy();
    hlsInstance = null;
  }
  // Effects on: use hls.js so the analyser gets a clean, decodable signal.
  hlsInstance = new Hls();
  hlsInstance.loadSource(HLS_STREAM_URL);
  hlsInstance.attachMedia(audioStream);

  hlsInstance.once(Hls.Events.MANIFEST_PARSED, function () {
    audioStream.play().catch(function (e) { console.warn("HLS play failed:", e); });
  });

  hlsInstance.on(Hls.Events.ERROR, function (event, data) {
    if (data.fatal) {
      console.warn("HLS fatal error, falling back to Icecast", data);
      hlsFailed = true;
      hlsInstance.destroy();
      hlsInstance = null;
      fallbackToIcecast();
    }
  });
}

function qualityUpdate() {
  // Clean up previous HLS instance
  if (hlsInstance) {
    hlsInstance.destroy();
    hlsInstance = null;
  }

  const nativeHls = audioStream.canPlayType("application/vnd.apple.mpegurl");

  if (hlsFailed) {
    fallbackToIcecast();
    return;
  }

  if (nativeHls && !fxOn()) {
    // With effects off we don't need the Web Audio analyser, so prefer the
    // browser's native HLS — it's hardware-accelerated and far lighter on the
    // CPU than hls.js's software MSE pipeline (which makes phones stutter).
    playNativeHls();
    return;
  }

  loadHlsJs()
    .then(function () {
      if (!isPlaying) return; // stopped while the script was downloading
      if (Hls.isSupported()) startHlsJs();
      else if (nativeHls) playNativeHls();
      else fallbackToIcecast();
    })
    .catch(function () {
      if (!isPlaying) return;
      if (nativeHls) playNativeHls();
      else fallbackToIcecast();
    });
}

// --- Enhanced Background Effects ---

function initAudioContext() {
  if (audioContext) return;
  audioContext = new (window.AudioContext || window.webkitAudioContext)();
  analyser = audioContext.createAnalyser();
  analyser.fftSize = 2048;
  analyser.smoothingTimeConstant = 0.4;
  analyser.minDecibels = -60;  // raise floor — cuts dead silence, spreads compressed signal
  analyser.maxDecibels = -5;   // near 0dBFS peak for compressed/loudness-war tracks
  gainNode = audioContext.createGain();
  gainNode.gain.value = currentGain;
  // Scratch crossfade + summing bus (see graph diagram above).
  mixBus = audioContext.createGain();
  liveGain = audioContext.createGain();
  scratchGain = audioContext.createGain();
  liveGain.gain.value = 1;     // live stream audible by default
  scratchGain.gain.value = 0;  // scrub layer silent until the record is grabbed
}

function connectAudioSource() {
  if (!audioContext || audioSource) return;
  try {
    audioSource = audioContext.createMediaElementSource(audioStream);
    // Live path → mix bus. The scratch layer joins the same bus (scratchGain
    // starts silent). The analyser taps the bus so the visualizer reacts to
    // whatever is audible — live or scratch — at full amplitude, pre-volume.
    audioSource.connect(liveGain);
    liveGain.connect(mixBus);
    scratchGain.connect(mixBus);
    mixBus.connect(analyser);              // tap (not routed onward to destination)
    mixBus.connect(gainNode);              // gainNode = master volume
    gainNode.connect(audioContext.destination);
    audioStream.volume = 1.0;
    initScratchWorklet();                  // async; scratch becomes available once loaded
  } catch (e) {
    console.warn('Audio source connection failed:', e);
  }
}

// --- Record scratch engine ---

// AudioWorklet processor (runs on the audio thread). It continuously records the
// live signal into a ~6s stereo ring buffer, and while "scratching" plays that
// buffer back at a position/rate driven from the main thread — negative rate =
// reverse. Loaded from a Blob URL so it stays self-contained in this file (no
// extra asset to copy into static/).
const SCRATCH_WORKLET_CODE = `
class ScratchProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.L = Math.floor(sampleRate * 6);
    this.ring = [new Float32Array(this.L), new Float32Array(this.L)];
    this.w = 0;            // absolute write head (samples written)
    this.readPos = 0;      // absolute fractional read position
    this.scratching = false;
    this.rate = 0;         // current playback rate (1 = live speed)
    this.targetRate = 0;
    this.port.onmessage = (e) => {
      const d = e.data;
      if (d.type === 'grab') { this.scratching = true; this.readPos = this.w - 64; this.rate = 0; this.targetRate = 0; }
      else if (d.type === 'release') { this.scratching = false; }
      else if (d.type === 'rate') { this.targetRate = d.value; }
    };
  }
  process(inputs, outputs) {
    const input = inputs[0];
    const output = outputs[0];
    const L = this.L;
    // Record live input into the ring buffer (always, so scrubbing has material).
    if (input && input.length && input[0]) {
      const chN = Math.min(2, input.length);
      for (let c = 0; c < chN; c++) {
        const inCh = input[c], ring = this.ring[c];
        for (let i = 0; i < inCh.length; i++) ring[(this.w + i) % L] = inCh[i];
      }
      if (input.length === 1) { // mono source — mirror into the right channel
        const r0 = this.ring[0], r1 = this.ring[1], n = input[0].length;
        for (let i = 0; i < n; i++) r1[(this.w + i) % L] = r0[(this.w + i) % L];
      }
      this.w += input[0].length;
    }
    const outLen = output[0] ? output[0].length : 128;
    if (!this.scratching) {
      for (let c = 0; c < output.length; c++) output[c].fill(0);
      return true;
    }
    this.rate += (this.targetRate - this.rate) * 0.25; // smooth — kill zipper noise
    const stopped = Math.abs(this.rate) < 0.02;        // held still = silence
    // Readable window: the last ~6s of recorded audio, ending just behind the
    // live write head. Instead of clamping at the edges (which freezes the
    // sound), we LOOP — scrub past either end and the buffer wraps around and
    // keeps playing. Live is never lost: w keeps advancing and release
    // crossfades back to the always-running live stream.
    const hi = this.w - 64;            // newest readable (just behind live)
    const lo = this.w - (L - 512);     // oldest readable
    const span = hi - lo;              // loop length (≈ 6s of buffer)
    for (let i = 0; i < outLen; i++) {
      let s0 = 0, s1 = 0;
      if (!stopped) {
        while (this.readPos > hi) this.readPos -= span; // scrubbed forward past live → loop
        while (this.readPos < lo) this.readPos += span; // scrubbed back past buffer → loop
        const i0 = Math.floor(this.readPos);
        const frac = this.readPos - i0;
        const a = ((i0 % L) + L) % L, b = (((i0 + 1) % L) + L) % L;
        s0 = this.ring[0][a] + (this.ring[0][b] - this.ring[0][a]) * frac;
        s1 = this.ring[1][a] + (this.ring[1][b] - this.ring[1][a]) * frac;
        this.readPos += this.rate;
      }
      if (output[0]) output[0][i] = s0;
      if (output[1]) output[1][i] = s1;
    }
    return true;
  }
}
registerProcessor('scratch-processor', ScratchProcessor);
`;

function initScratchWorklet() {
  if (!audioContext || !audioContext.audioWorklet || scratchNode || scratchModulePromise) return;
  const url = URL.createObjectURL(new Blob([SCRATCH_WORKLET_CODE], { type: 'application/javascript' }));
  scratchModulePromise = audioContext.audioWorklet.addModule(url)
    .then(() => {
      scratchNode = new AudioWorkletNode(audioContext, 'scratch-processor', {
        numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [2],
      });
      if (audioSource) audioSource.connect(scratchNode); // feed the ring buffer
      scratchNode.connect(scratchGain);
      scratchReady = true;
    })
    .catch((e) => { console.warn('Scratch worklet unavailable:', e); })
    .finally(() => URL.revokeObjectURL(url));
}

// Crossfade the audible signal between the live stream and the scrub layer. The
// live <audio> element never stops, so on release we're already at the live edge.
function startScratchAudio() {
  if (!scratchNode) return;
  const t = audioContext.currentTime;
  liveGain.gain.cancelScheduledValues(t);
  scratchGain.gain.cancelScheduledValues(t);
  liveGain.gain.setValueAtTime(liveGain.gain.value, t);
  scratchGain.gain.setValueAtTime(scratchGain.gain.value, t);
  liveGain.gain.linearRampToValueAtTime(0, t + 0.04);
  scratchGain.gain.linearRampToValueAtTime(1, t + 0.04);
  scratchNode.port.postMessage({ type: 'grab' });
}

function setScratchRate(rate) {
  if (scratchNode) scratchNode.port.postMessage({ type: 'rate', value: rate });
}

function stopScratchAudio() {
  if (!scratchNode) return;
  const t = audioContext.currentTime;
  liveGain.gain.cancelScheduledValues(t);
  scratchGain.gain.cancelScheduledValues(t);
  liveGain.gain.setValueAtTime(liveGain.gain.value, t);
  scratchGain.gain.setValueAtTime(scratchGain.gain.value, t);
  liveGain.gain.linearRampToValueAtTime(1, t + 0.06);
  scratchGain.gain.linearRampToValueAtTime(0, t + 0.06);
  scratchNode.port.postMessage({ type: 'release' });
}

// --- Scratch gesture (pointer drag on the record) ---

// Normal spin is SPIN_FULL deg/ms and corresponds to 1× playback, so the audio
// rate is simply the hand's angular speed measured in those same units.
const SCRATCH_MAX_RATE = 8;   // clamp wild flicks
const SCRATCH_RELEASE_MS = 600; // how fast the platter recovers to full speed

function scratchEnabled() {
  return fxOn() && isPlaying && scratchReady && !recordGrabbed &&
    !tonearmChoreography && !document.body.classList.contains('no-bg-animation');
}

function pointerAngleDeg(e) {
  const cx = scratchRect.left + scratchRect.width / 2;
  const cy = scratchRect.top + scratchRect.height / 2;
  return Math.atan2(e.clientY - cy, e.clientX - cx) * 180 / Math.PI;
}

function onRecordPointerDown(e) {
  if (!scratchEnabled()) return;
  if (e.pointerType === 'mouse' && e.button !== 0) return;
  const card = document.getElementById('art-card');
  if (!card) return;
  scratchRect = card.getBoundingClientRect();
  recordGrabbed = true;
  scratchMoved = false;
  scratchVel = 0;
  scratchPrevAngle = pointerAngleDeg(e);
  scratchPrevTime = e.timeStamp || performance.now();
  // Take over the spin engine: freeze any ramp and stop its rAF loop.
  spinRampDur = 0;
  if (spinRafId) { cancelAnimationFrame(spinRafId); spinRafId = null; }
  if (card.setPointerCapture) { try { card.setPointerCapture(e.pointerId); } catch (_) {} }
  document.body.classList.add('record-grabbed');
  // Audio doesn't cross over to the scrub layer until the finger actually moves
  // (see onRecordPointerMove) — so a plain tap to open the album link is silent-safe.
  e.preventDefault();
  window.addEventListener('pointermove', onRecordPointerMove);
  window.addEventListener('pointerup', onRecordPointerUp);
  window.addEventListener('pointercancel', onRecordPointerUp);
}

function onRecordPointerMove(e) {
  if (!recordGrabbed) return;
  const ang = pointerAngleDeg(e);
  let d = ang - scratchPrevAngle;
  if (d > 180) d -= 360; else if (d < -180) d += 360; // shortest way round
  const now = e.timeStamp || performance.now();
  let dt = now - scratchPrevTime;
  if (dt < 1) dt = 1;
  scratchPrevAngle = ang;
  scratchPrevTime = now;
  if (!scratchMoved && Math.abs(d) > 0.6) {
    scratchMoved = true;
    startScratchAudio(); // first real movement — cross over to the scrub layer
  }
  // Rotate the record under the finger.
  spinAngle = (spinAngle + d) % 360;
  if (spinEls) spinEls.forEach((el) => (el.style.transform = `rotate(${spinAngle}deg)`));
  // Angular speed (deg/ms) → playback rate; 1× when moving at normal spin speed.
  scratchVel = d / dt;
  let rate = scratchVel / SPIN_FULL;
  rate = Math.max(-SCRATCH_MAX_RATE, Math.min(SCRATCH_MAX_RATE, rate));
  setScratchRate(rate);
}

function onRecordPointerUp() {
  if (!recordGrabbed) return;
  recordGrabbed = false;
  window.removeEventListener('pointermove', onRecordPointerMove);
  window.removeEventListener('pointerup', onRecordPointerUp);
  window.removeEventListener('pointercancel', onRecordPointerUp);
  document.body.classList.remove('record-grabbed');
  if (scratchMoved) {
    stopScratchAudio();          // cross back to the (still-live) stream
  }
  // Carry the hand's forward momentum into the spin, then ease back to full.
  spinSpeed = Math.max(0, Math.min(scratchVel, SPIN_FULL * 3));
  spinPrev = 0;
  startSpinRamp(isPlaying ? SPIN_FULL : 0, SCRATCH_RELEASE_MS, EASE_OUT);
}

function initRecordScratch() {
  const card = document.getElementById('art-card');
  const link = document.getElementById('playing-link');
  if (!card) return;
  card.addEventListener('pointerdown', onRecordPointerDown);
  // The record is not a link — it's only a scratch surface. Block any click
  // navigation outright (a stray href, a scratch that ends as a tap, etc.).
  if (link) link.addEventListener('click', (e) => { e.preventDefault(); }, true);
}


// --- Background Canvas ---

function initBgCanvas() {
  bgCanvas = document.getElementById('bg-canvas');
  bgCtx = bgCanvas.getContext('2d');
  resizeBgCanvas();
  window.addEventListener('resize', resizeBgCanvas);
}

function resizeBgCanvas() {
  if (!bgCanvas) return;
  bgCanvas.width = Math.max(1, Math.floor(window.innerWidth / 8));
  bgCanvas.height = Math.max(1, Math.floor(window.innerHeight / 8));
  bgForceDraw = true; // setting width clears the buffer — repaint next frame
}

function startBg() {
  if (bgRafId || document.body.classList.contains('no-bg-animation')) return;
  bgForceDraw = true; // canvas was cleared by stopBg — repaint on restart
  drawBgLoop();
}

function stopBg() {
  if (bgRafId) {
    cancelAnimationFrame(bgRafId);
    bgRafId = null;
  }
  if (bgCtx) bgCtx.clearRect(0, 0, bgCanvas.width, bgCanvas.height);
}

function drawBgLoop(timestamp) {
  bgRafId = requestAnimationFrame(drawBgLoop);
  if (timestamp - bgLastTime < BG_INTERVAL) return;
  bgLastTime = timestamp;

  // Lerp toward target colors
  bgPrimary.r += (bgPrimaryTarget.r - bgPrimary.r) * 0.06;
  bgPrimary.g += (bgPrimaryTarget.g - bgPrimary.g) * 0.06;
  bgPrimary.b += (bgPrimaryTarget.b - bgPrimary.b) * 0.06;
  bgPresence.r += (bgPresenceTarget.r - bgPresence.r) * 0.06;
  bgPresence.g += (bgPresenceTarget.g - bgPresence.g) * 0.06;
  bgPresence.b += (bgPresenceTarget.b - bgPresence.b) * 0.06;

  // Skip the paint entirely when nothing is moving: colours have settled onto
  // their targets and the bass/presence energy is steady. The loop keeps
  // ticking cheaply and re-engages the moment audio or the palette changes.
  // bgForceDraw covers invalidations that don't show up in these values
  // (resize clears the canvas; theme toggle changes how colours are rendered).
  const settled =
    Math.abs(bgPrimaryTarget.r - bgPrimary.r) < 0.5 &&
    Math.abs(bgPrimaryTarget.g - bgPrimary.g) < 0.5 &&
    Math.abs(bgPrimaryTarget.b - bgPrimary.b) < 0.5 &&
    Math.abs(bgPresenceTarget.r - bgPresence.r) < 0.5 &&
    Math.abs(bgPresenceTarget.g - bgPresence.g) < 0.5 &&
    Math.abs(bgPresenceTarget.b - bgPresence.b) < 0.5;
  const motion =
    Math.abs(smoothedBass - bgLastBass) > 0.002 ||
    Math.abs(smoothedPresence - bgLastPresence) > 0.002;
  if (!bgForceDraw && settled && !motion) return;
  bgForceDraw = false;
  bgLastBass = smoothedBass;
  bgLastPresence = smoothedPresence;

  const W = bgCanvas.width, H = bgCanvas.height;
  const { cx, cy } = bgGlowCenter();
  const scale = 1 + smoothedBass * 0.65;

  // Light mode: deepen the colours and push opacity so glows read against the bright bg
  const light = document.body.classList.contains('theme-light');
  const aMul = light ? 2.0 : 1;
  const deepen = (v) => light ? Math.round(v * 0.55) : v;
  const r = deepen(bgPrimary.r), g = deepen(bgPrimary.g), b = deepen(bgPrimary.b);
  const pr = deepen(bgPresence.r), pg = deepen(bgPresence.g), pb = deepen(bgPresence.b);

  bgCtx.clearRect(0, 0, W, H);

  // Main centered glow
  const maxR = Math.max(W, H) * 0.3 * scale;
  const grd = bgCtx.createRadialGradient(cx, cy, 0, cx, cy, maxR);
  grd.addColorStop(0, `rgba(${r}, ${g}, ${b}, ${(0.1 + smoothedBass * 0.7) * aMul})`);
  grd.addColorStop(0.5, `rgba(${r}, ${g}, ${b}, ${(0.03 + smoothedBass * 0.2) * aMul})`);
  grd.addColorStop(1, 'rgba(0,0,0,0)');
  bgCtx.fillStyle = grd;
  bgCtx.fillRect(0, 0, W, H);

  // Offset secondary glows — presence colour for visual separation
  const grd2 = bgCtx.createRadialGradient(W * 0.25, H * 0.25, 0, W * 0.25, H * 0.25, maxR * 0.5);
  grd2.addColorStop(0, `rgba(${pr}, ${pg}, ${pb}, ${(0.04 + smoothedPresence * 0.25) * aMul})`);
  grd2.addColorStop(1, 'rgba(0,0,0,0)');
  bgCtx.fillStyle = grd2;
  bgCtx.fillRect(0, 0, W, H);

  const grd3 = bgCtx.createRadialGradient(W * 0.75, H * 0.75, 0, W * 0.75, H * 0.75, maxR * 0.5);
  grd3.addColorStop(0, `rgba(${pr}, ${pg}, ${pb}, ${(0.03 + smoothedPresence * 0.2) * aMul})`);
  grd3.addColorStop(1, 'rgba(0,0,0,0)');
  bgCtx.fillStyle = grd3;
  bgCtx.fillRect(0, 0, W, H);
}

// Centre point for the background glow, aligned vertically with the album art
function bgGlowCenter() {
  const W = bgCanvas.width, H = bgCanvas.height;
  let cy = H / 2;
  const card = document.getElementById('art-card');
  if (card) {
    const rect = card.getBoundingClientRect();
    if (rect.height) cy = ((rect.top + rect.height / 2) / window.innerHeight) * H;
  }
  return { cx: W / 2, cy };
}

function applyPendingMetadata() {
  if (!pendingMetadata) return;
  const { data, imgSrc } = pendingMetadata;
  pendingMetadata = null;

  const createLink = (url, id, name) =>
    `<a href="${url}${encodeURIComponent(id)}" target="_blank" style="color: inherit; text-decoration: none;">${escapeHtml(name)}</a>`;

  const cardEl   = document.getElementById('art-card');
  const titleEl  = document.querySelector('.div-playing-title');
  const artistEl = document.querySelector('.div-playing-artist');

  const isChange = lastDisplayedSongId !== null && lastDisplayedSongId !== data.songid;
  lastDisplayedSongId = data.songid;

  const applyContent = () => {
    // Swap the cover and the colour palette together — at this point the
    // background + side glow are dimmed, so the colour change isn't visible
    // until they fade back in as the record spins up.
    applyPendingColors();
    playingImage.src = imgSrc;
    titleEl.innerHTML  = createLink('https://open.spotify.com/track/', data.songid, data.song);
    artistEl.innerHTML = data.artist
      .map((a) => createLink('https://open.spotify.com/artist/', a.id, a.name))
      .join(', ');
    updateRecordLabel(data);
    // Re-anchor the arm on every metadata — song change, in-track seek, and the
    // reconnect snapshot all carry a fresh elapsed_ms (the position currently
    // being heard, already adjusted for the stream delay server-side). The
    // server only re-sends mid-song when there's a real seek, so this is
    // idempotent for steady playback. elapsed_ms may be negative while a new
    // song is still in the stream's delay buffer — updateTonearm clamps to 0.
    tonearmSongId = data.songid;
    songDurationMs = data.duration_ms || null;
    songStartedAtPerf =
      typeof data.elapsed_ms === "number"
        ? performance.now() - data.elapsed_ms
        : null;
    updateTonearm();
    if ('mediaSession' in navigator && data.song) {
      navigator.mediaSession.metadata = new MediaMetadata({
        title: data.song,
        artist: data.artist.map((a) => a.name).join(', '),
        album: data.albumid,
        artwork: [{ src: imgSrc, sizes: '250x250', type: 'image/jpeg' }],
      });
    }
  };

  if (!isChange) {
    applyContent();
    return;
  }

  // A previous swap may still be mid-flight (rapid skip / back-to-back
  // changes) — abort it before starting this one.
  cancelChoreography();

  const armEl = document.getElementById('tonearm');

  // Lightweight mode: no record/arm — just cross-fade the art + text.
  if (document.body.classList.contains('no-bg-animation')) {
    cardEl.classList.add('fade-swap');
    titleEl.classList.add('fade-swap-text');
    artistEl.classList.add('fade-swap-text');
    choreoTimeout(applyContent, 600);
    choreoTimeout(() => {
      cardEl.classList.remove('fade-swap');
      titleEl.classList.remove('fade-swap-text');
      artistEl.classList.remove('fade-swap-text');
    }, 1200);
    return;
  }

  // Full turntable sequence:
  //   1. arm swings back off the side of the record
  //   2. the record flips, swapping to the new cover while edge-on
  //   3. arm drops back onto the rim to start the new song
  const LEAD_MS = 700;  // let the record spin down a bit before the arm moves
  const SWING_MS = 1000;
  tonearmChoreography = true;
  // Song's about to end — fade out the background + side glow as the record spins down.
  document.body.classList.add('fx-dimmed');
  // Record eases to a stop first, finishing right as the arm parks / flip begins.
  startSpinRamp(0, LEAD_MS + SWING_MS, EASE_OUT);

  choreoTimeout(() => {
    // Arm swings off the side of the record.
    if (armEl) {
      armEl.style.transition = 'transform 1s ease, opacity 1s ease';
      armEl.style.transform = `rotate(${TONEARM_PARK_DEG}deg)`;
    }
    choreoTimeout(() => {
      cardEl.classList.add('flip-swap');
      titleEl.classList.add('fade-swap-text');
      artistEl.classList.add('fade-swap-text');
      choreoTimeout(applyContent, 600); // swap at the edge-on (invisible) point
      choreoTimeout(() => {
        cardEl.classList.remove('flip-swap');
        titleEl.classList.remove('fade-swap-text');
        artistEl.classList.remove('fade-swap-text');
        // Flip done — drop the arm back onto the rim and spin back up to speed
        // (only if actually playing; a paused record stays at rest).
        if (armEl) armEl.style.transform = 'rotate(0deg)';
        startSpinRamp(isPlaying ? SPIN_FULL : 0, 1400, EASE_IN);
        // Record's spinning again — bring the background + side glow back.
        document.body.classList.remove('fx-dimmed');
        choreoTimeout(() => {
          if (armEl) armEl.style.transition = ''; // restore play-time (linear) transition
          tonearmChoreography = false;
        }, SWING_MS);
      }, 1200);
    }, SWING_MS);
  }, LEAD_MS);
}

// --- End Background Canvas ---

// --- Audio Visualizer ---

function initVizCanvas() {
  vizCanvas = document.getElementById('viz-canvas');
  vizCtx = vizCanvas.getContext('2d');
  resizeVizCanvas();
  window.addEventListener('resize', resizeVizCanvas);
}

function resizeVizCanvas() {
  if (!vizCanvas) return;
  // Render at 1/5 resolution — soft glows are indistinguishable, massive GPU savings
  vizCanvas.width = Math.floor(window.innerWidth / 5);
  vizCanvas.height = Math.floor(window.innerHeight / 5);
}

function startViz() {
  if (vizRafId || document.body.classList.contains('no-bg-animation')) return;
  vizCanvas.style.opacity = '1';
  drawViz();
}

function stopViz() {
  if (vizRafId) {
    cancelAnimationFrame(vizRafId);
    vizRafId = null;
  }
  if (vizCanvas) {
    vizCanvas.style.opacity = '0';
    setTimeout(() => {
      if (vizCtx && !vizRafId) vizCtx.clearRect(0, 0, vizCanvas.width, vizCanvas.height);
    }, 1500);
  }
  smoothedBass = 0;
  smoothedPresence = 0;
  vizLastTime = 0;
}

const VIZ_FPS = 60;
const VIZ_INTERVAL = 1000 / VIZ_FPS;
let vizLastTime = 0;

// The background is just slowly-drifting radial glows — 30fps is imperceptible
// and halves its per-frame gradient work vs. the audio-reactive visualizer.
const BG_FPS = 30;
const BG_INTERVAL = 1000 / BG_FPS;

function drawViz(timestamp) {
  vizRafId = requestAnimationFrame(drawViz);
  if (!analyser) return;
  if (timestamp - vizLastTime < VIZ_INTERVAL) return;
  vizLastTime = timestamp;

  const bufferLength = analyser.frequencyBinCount;
  // Reuse a single buffer across frames — reallocating 60×/sec churns the GC.
  if (!vizDataArray || vizDataArray.length !== bufferLength) {
    vizDataArray = new Uint8Array(bufferLength);
  }
  const dataArray = vizDataArray;
  // A paused stream is silence. Don't trust the analyser here: after a
  // play→pause it still holds the last frequencies from before the pause, which
  // would briefly flash the glow during the play() pre-roll. Feed zeros instead
  // so the glow only rises once audio is actually flowing.
  if (audioStream.paused) dataArray.fill(0);
  else analyser.getByteFrequencyData(dataArray);

  const W = vizCanvas.width;
  const H = vizCanvas.height;
  const cx = W / 2;
  const cy = H / 2;

  // Hz-accurate bin ranges based on actual sample rate
  const nyquist = audioContext.sampleRate / 2;
  const hzPerBin = nyquist / bufferLength;

  // Bass 30–400 Hz
  const bassStart = Math.max(1, Math.round(30 / hzPerBin));
  const bassEnd = Math.max(bassStart + 1, Math.round(400 / hzPerBin));
  let bass = 0;
  for (let i = bassStart; i <= bassEnd; i++) bass += dataArray[i];
  bass = Math.pow(bass / (bassEnd - bassStart + 1) / 255, 0.55);
  smoothedBass += (bass - smoothedBass) * 0.2;

  // Vocal presence 1500–3500 Hz
  const presStart = Math.round(1500 / hzPerBin);
  const presEnd = Math.min(bufferLength - 1, Math.round(3500 / hzPerBin));
  let mid = 0;
  for (let i = presStart; i <= presEnd; i++) mid += dataArray[i];
  mid = Math.pow(mid / (presEnd - presStart + 1) / 255, 0.55);
  smoothedPresence += (mid - smoothedPresence) * 0.16;

  vizCtx.clearRect(0, 0, W, H);

  // Light mode: deepen colours and push opacity so glows read against the bright bg
  const light = document.body.classList.contains('theme-light');
  const aMul = light ? 2.0 : 1;
  const deepen = (v) => light ? Math.round(v * 0.55) : v;
  const r = deepen(vizPrimary.r), g = deepen(vizPrimary.g), b = deepen(vizPrimary.b);
  const minDim = Math.min(W, H);

  // Center bass glow — large and punchy
  if (smoothedBass > 0.02) {
    const glowR = minDim * 0.06 + smoothedBass * minDim * 0.22;
    const grd = vizCtx.createRadialGradient(cx, cy, 0, cx, cy, glowR);
    grd.addColorStop(0, `rgba(${r}, ${g}, ${b}, ${smoothedBass * 0.75 * aMul})`);
    grd.addColorStop(0.4, `rgba(${r}, ${g}, ${b}, ${smoothedBass * 0.25 * aMul})`);
    grd.addColorStop(1, 'rgba(0,0,0,0)');
    vizCtx.beginPath();
    vizCtx.arc(cx, cy, glowR, 0, Math.PI * 2);
    vizCtx.fillStyle = grd;
    vizCtx.fill();
  }

  // Edge bands driven by presence (1500–3500 Hz) — 1/8th inset on all sides
  if (smoothedPresence > 0.01) {
    const pr = deepen(vizPresence.r), pg = deepen(vizPresence.g), pb = deepen(vizPresence.b);
    const alpha = smoothedPresence * 0.65 * aMul;
    const color = `rgba(${pr}, ${pg}, ${pb}, ${alpha})`;
    const clear = 'rgba(0,0,0,0)';

    vizCtx.save();
    vizCtx.beginPath();
    vizCtx.rect(0, 0, W, H);
    vizCtx.roundRect(W * 0.125, H * 0.125, W * 0.75, H * 0.75, Math.min(W, H) * 0.08);
    vizCtx.clip('evenodd');

    // Left
    const gradL = vizCtx.createLinearGradient(0, 0, W * 0.125, 0);
    gradL.addColorStop(0, color); gradL.addColorStop(1, clear);
    vizCtx.fillStyle = gradL;
    vizCtx.fillRect(0, 0, W * 0.125, H);

    // Right
    const gradR = vizCtx.createLinearGradient(W * 0.875, 0, W, 0);
    gradR.addColorStop(0, clear); gradR.addColorStop(1, color);
    vizCtx.fillStyle = gradR;
    vizCtx.fillRect(W * 0.875, 0, W * 0.125, H);

    vizCtx.restore();
  }
}

// --- End Audio Visualizer ---

initBgCanvas();
initVizCanvas();
initRecordScratch();

// --- End Enhanced Background Effects ---

function togglePlay() {
  isPlaying = true;
  startPolling();
  updatePlayUI();   // kicks off the spin-up ramp + tonearm swing
  updateButtons();

  // Only build the Web Audio graph when the visualizer actually needs it. With
  // effects off (most phones) the <audio> element plays natively — far lighter
  // on the CPU. Volume then runs through audioStream.volume (see volumeSet).
  if (fxOn()) {
    enableAudioGraph();
    // Hold the audio until the record has spun up and the arm has dropped into
    // place — like lowering the needle on a platter that's already at speed.
    if (audioStartTimer) clearTimeout(audioStartTimer);
    audioStartTimer = setTimeout(function () {
      audioStartTimer = null;
      if (isPlaying) qualityUpdate();
    }, SPIN_UP_MS);
  } else {
    qualityUpdate();
  }
}

// Lazily route the <audio> element through Web Audio (analyser + gain) for the
// visualizer. Safe to call repeatedly — the underlying nodes are created once.
function enableAudioGraph() {
  initAudioContext();
  connectAudioSource();
  if (audioContext.state === 'suspended') audioContext.resume();
  startViz();
}

function buttonStop() {
  if (audioStartTimer) { clearTimeout(audioStartTimer); audioStartTimer = null; }
  audioStream.pause();
  audioStream.currentTime = 0;
  isPlaying = false;
  stopPolling();
  updatePlayUI();
  updateButtons();
  stopViz();
  // Abort any in-flight song-change sequence — don't leave the background
  // dimmed or the arm mid-swing.
  cancelChoreography();
}

function volumeSet(val) {
  const scaledVolume = scaleVolume(val);
  currentGain = scaledVolume / 100;
  if (gainNode) {
    gainNode.gain.value = currentGain;
  } else {
    audioStream.volume = currentGain;
  }
  storageSet("volume", val);
  setVolumeUI(val);
}

function setVolumeUI(val) {
  const fill = document.getElementById("vol-fill");
  const thumb = document.getElementById("vol-thumb");
  const track = document.getElementById("vol-track");
  const pct = Math.max(0, Math.min(100, val));
  if (fill) fill.style.width = pct + "%";
  if (thumb) thumb.style.left = pct + "%";
  if (track) track.setAttribute("aria-valuenow", String(pct));
}

// Custom volume slider drag
(function initVolumeSlider() {
  const track = document.getElementById("vol-track");
  let dragging = false;

  function updateFromX(clientX) {
    const rect = track.getBoundingClientRect();
    let v = Math.round(((clientX - rect.left) / rect.width) * 100);
    v = Math.max(0, Math.min(100, v));
    volumeSet(v);
  }
  track.addEventListener("pointerdown", (e) => {
    if (e.pointerType === "mouse" && e.button !== 0) return;
    // Without this the browser can start a text-selection/native drag mid-
    // slide, which cancels the pointer stream and leaves the slider stuck.
    e.preventDefault();
    dragging = true;
    try { track.setPointerCapture(e.pointerId); } catch (_) {}
    updateFromX(e.clientX);
  });
  // Listen on window, not the track: if pointer capture failed (or was lost)
  // the drag keeps tracking even once the cursor leaves the 6px-tall track,
  // and a pointerup anywhere always ends it.
  window.addEventListener("pointermove", (e) => {
    if (dragging) updateFromX(e.clientX);
  });
  const end = () => { dragging = false; };
  window.addEventListener("pointerup", end);
  window.addEventListener("pointercancel", end);

  // Keyboard support (role="slider"): arrows nudge, Home/End jump.
  track.addEventListener("keydown", (e) => {
    const current = parseInt(storageGet("volume") ?? "50", 10);
    let next = null;
    if (e.key === "ArrowLeft" || e.key === "ArrowDown") next = current - 5;
    else if (e.key === "ArrowRight" || e.key === "ArrowUp") next = current + 5;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = 100;
    if (next !== null) {
      e.preventDefault();
      volumeSet(Math.max(0, Math.min(100, next)));
    }
  });
})();

function preloadImage(url, callback) {
  const img = new Image();
  img.crossOrigin = 'Anonymous';
  // Wait for a full decode (not just download) so the bitmap is warm in cache —
  // setting playingImage.src to the same URL then paints instantly without a flicker.
  const done = () => callback(img);
  img.onload = () => (img.decode ? img.decode().then(done, done) : done());
  img.onerror = done; // don't stall the swap on a broken cover
  img.src = url;
}

function handleMetadata(currentData) {
  if (!currentData || !currentData.song) {
    document
      .querySelectorAll(".div-playing-title, .div-playing-artist")
      .forEach((el) => (el.textContent = ""));
    updateRecordLabel(null);
    if (
      playingImage.src !==
      "data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw=="
    ) {
      playingImage.src =
        "data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==";
    }
    updateSongId(null);
    songStartedAtPerf = null;
    songDurationMs = null;
    tonearmSongId = null;
    updateTonearm();
    return;
  }

  updateSongId(currentData.songid);

  if (playingImage.src !== currentData.cover) {
    // Preload new cover, extract colours, then kick off the wipe transition.
    // The DOM update (image + text) is deferred until the wipe reaches the centre.
    preloadImage(currentData.cover, function (img) {
      extractColors(img).then((colors) => {
        pendingMetadata = { data: currentData, imgSrc: currentData.cover };
        updateBackgroundColors(colors);
      });
    });
  } else {
    // Same cover — just queue the text/metadata update
    pendingMetadata = { data: currentData, imgSrc: currentData.cover };
    applyPendingMetadata();
  }
}

// ---- Record centre label (classic pressed-vinyl label text) ----
// Title arcs over the top of the label, artist arcs under the bottom, release
// year sits above the spindle hole. Text is sized to fit its arc; if it can't
// fit even at the minimum size it's trimmed with an ellipsis, like a tight
// pressing plant typesetter would.
const LABEL_ARCS = {
  title: { len: 104, max: 8.0 },  // usable path length + max font size, in viewBox units
  artist: { len: 118, max: 6.0 },
};
function setLabelArcText(textEl, str, arc) {
  if (!textEl) return;
  const MIN_FS = 2.9;
  const CHAR_W = 0.75; // avg uppercase glyph width (incl. tracking) as a fraction of font-size
  let text = (str || "").toUpperCase();
  let fs = Math.min(arc.max, arc.len / (Math.max(text.length, 1) * CHAR_W));
  if (fs < MIN_FS) {
    fs = MIN_FS;
    text = text.slice(0, Math.floor(arc.len / (fs * CHAR_W)) - 1) + "…";
  }
  textEl.setAttribute("font-size", fs.toFixed(2));
  textEl.querySelector("textPath").textContent = text;
}
function updateRecordLabel(data) {
  const svg = document.getElementById("record-label");
  if (!svg) return;
  setLabelArcText(svg.querySelector("#label-title"), data && data.song, LABEL_ARCS.title);
  setLabelArcText(
    svg.querySelector("#label-artist"),
    data && data.artist ? data.artist.map((a) => a.name).join(" · ") : "",
    LABEL_ARCS.artist
  );
  svg.querySelector("#label-year").textContent = (data && data.year) || "";
}

function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

let lastHistoryTopId = null;
function renderHistory(history) {
  const panel = document.getElementById("history-panel");
  const list = document.getElementById("history-list");
  if (!panel || !list) return;

  const items = (history || []).slice(0, 4);
  panel.style.display = items.length ? "" : "none";

  const newTopId = items.length ? items[0].songid : null;
  const isNew = newTopId && newTopId !== lastHistoryTopId && lastHistoryTopId !== null;
  lastHistoryTopId = newTopId;

  list.innerHTML = items
    .map((it) => {
      const artists = (it.artist || []).map((a) => escapeHtml(a.name)).join(", ");
      return `<a class="history-item" href="https://open.spotify.com/track/${encodeURIComponent(it.songid)}" target="_blank">
        <img class="history-art" src="${escapeHtml(it.cover || "")}" alt="" />
        <div class="history-text">
          <div class="history-title">${escapeHtml(it.song)}</div>
          <div class="history-artist">${artists}</div>
        </div>
      </a>`;
    })
    .join("");

  // Animate the freshly added song shifting into the top of the list
  if (isNew && list.firstElementChild) {
    list.firstElementChild.classList.add("history-enter");
  }
}

function connectSSE() {
  const evtSource = new EventSource("/api/events");

  evtSource.addEventListener("metadata", (e) => {
    handleMetadata(JSON.parse(e.data));
  });

  evtSource.addEventListener("history", (e) => {
    renderHistory(JSON.parse(e.data).history);
  });

  evtSource.addEventListener("listeners", (e) => {
    const data = JSON.parse(e.data);
    document.getElementById("listener-count").textContent = data.count;
  });

  evtSource.addEventListener("skipvotes", (e) => {
    const data = JSON.parse(e.data);
    document.getElementById("vote-count").textContent = data.count;
    document.getElementById("votes-needed").textContent = data.needed;
    const fill = document.getElementById("vote-bar-fill");
    if (fill) {
      const pct = data.needed
        ? Math.min(100, Math.round((data.count / data.needed) * 100))
        : 0;
      fill.style.width = pct + "%";
    }
  });

  evtSource.onerror = () => {
    evtSource.close();
    setTimeout(connectSSE, 5000);
  };
}

function updateSongId(newSongId) {
  if (newSongId !== songId) {
    if (mutedSongId !== null) {
      audioStream.muted = false;
      mutedSongId = null;
    }
    songId = newSongId;
  }
  updateButtons();
}

function hue2rgb(p, q, t) {
  if (t < 0) t += 1;
  if (t > 1) t -= 1;
  if (t < 1/6) return p + (q - p) * 6 * t;
  if (t < 1/2) return q;
  if (t < 2/3) return p + (q - p) * (2/3 - t) * 6;
  return p;
}

// Clamp a colour away from near-black and near-white, ensure minimum saturation
function sanitizeColor(r, g, b) {
  const rn = r/255, gn = g/255, bn = b/255;
  const max = Math.max(rn, gn, bn), min = Math.min(rn, gn, bn);
  let h, s, l = (max + min) / 2;
  if (max === min) { h = 0; s = 0; }
  else {
    const d = max - min;
    s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
    if (max === rn) h = ((gn - bn) / d + (gn < bn ? 6 : 0)) / 6;
    else if (max === gn) h = ((bn - rn) / d + 2) / 6;
    else h = ((rn - gn) / d + 4) / 6;
  }
  l = Math.max(0.22, Math.min(0.70, l));  // not too dark, not too white
  s = Math.max(0.30, s);                  // minimum saturation
  const q2 = l < 0.5 ? l * (1 + s) : l + s - l * s;
  const p2 = 2 * l - q2;
  return {
    r: Math.round(hue2rgb(p2, q2, h + 1/3) * 255),
    g: Math.round(hue2rgb(p2, q2, h) * 255),
    b: Math.round(hue2rgb(p2, q2, h - 1/3) * 255),
  };
}

// Rotate hue by degrees, keeping sanitized lightness/saturation
function shiftHue(rgb, degrees) {
  const rn = rgb.r/255, gn = rgb.g/255, bn = rgb.b/255;
  const max = Math.max(rn, gn, bn), min = Math.min(rn, gn, bn);
  let h, s, l = (max + min) / 2;
  if (max === min) { h = 0; s = 0; }
  else {
    const d = max - min;
    s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
    if (max === rn) h = ((gn - bn) / d + (gn < bn ? 6 : 0)) / 6;
    else if (max === gn) h = ((bn - rn) / d + 2) / 6;
    else h = ((rn - gn) / d + 4) / 6;
  }
  h = (h + degrees / 360 + 1) % 1;
  const q2 = l < 0.5 ? l * (1 + s) : l + s - l * s;
  const p2 = 2 * l - q2;
  return {
    r: Math.round(hue2rgb(p2, q2, h + 1/3) * 255),
    g: Math.round(hue2rgb(p2, q2, h) * 255),
    b: Math.round(hue2rgb(p2, q2, h - 1/3) * 255),
  };
}

function extractColors(imgElement) {
  return new Promise((resolve) => {
    // preloadImage hands over an already-loaded, decoded image — draw it
    // directly instead of re-fetching the URL through a second Image element.
    // drawImage/getImageData throw on a broken or CORS-tainted image; fall
    // back to a neutral palette in that case.
    try {
      const canvas = document.getElementById("color-canvas");
      const ctx = canvas.getContext("2d");
      canvas.width = 50;
      canvas.height = 50;
      ctx.drawImage(imgElement, 0, 0, 50, 50);

      const imageData = ctx.getImageData(0, 0, 50, 50);
      const data = imageData.data;

      // Quantize into 12 hue sectors (30° each), skip neutrals/very dark pixels
      const NUM_SECTORS = 12;
      const buckets = Array.from({length: NUM_SECTORS}, () => ({r:0, g:0, b:0, count:0}));
      let tr = 0, tg = 0, tb = 0, tc = 0;

      for (let i = 0; i < data.length; i += 16) {
        const r = data[i], g = data[i+1], b = data[i+2];
        tr += r; tg += g; tb += b; tc++;
        const max = Math.max(r, g, b), min = Math.min(r, g, b);
        const chroma = max - min;
        if (chroma < 25 || max < 35) continue; // skip neutrals and near-black
        let hue;
        if (max === r) hue = ((g - b) / chroma + 6) % 6;
        else if (max === g) hue = (b - r) / chroma + 2;
        else hue = (r - g) / chroma + 4;
        const sector = Math.floor(hue / 6 * NUM_SECTORS) % NUM_SECTORS;
        buckets[sector].r += r;
        buckets[sector].g += g;
        buckets[sector].b += b;
        buckets[sector].count++;
      }

      const chromatic = buckets
        .map((b, i) => ({...b, i}))
        .filter(b => b.count > 0)
        .sort((a, b) => b.count - a.count);

      function bucketAvg(bucket) {
        return sanitizeColor(
          Math.round(bucket.r / bucket.count),
          Math.round(bucket.g / bucket.count),
          Math.round(bucket.b / bucket.count)
        );
      }

      let bass, presence;
      if (chromatic.length === 0) {
        // Fully neutral/monochrome — sanitize average and shift hue for presence
        bass = sanitizeColor(Math.round(tr/tc), Math.round(tg/tc), Math.round(tb/tc));
        presence = shiftHue(bass, 120);
      } else if (chromatic.length === 1) {
        bass = bucketAvg(chromatic[0]);
        presence = shiftHue(bass, 120);
      } else {
        bass = bucketAvg(chromatic[0]);
        // Find second colour at least 2 sectors (60°) away from dominant
        const second = chromatic.find(b => {
          const dist = Math.min(
            Math.abs(b.i - chromatic[0].i),
            NUM_SECTORS - Math.abs(b.i - chromatic[0].i)
          );
          return dist >= 2;
        }) || chromatic[1];
        presence = bucketAvg(second);
      }

      resolve({
        primary:  `rgb(${bass.r}, ${bass.g}, ${bass.b})`,
        presence: `rgb(${presence.r}, ${presence.g}, ${presence.b})`,
      });
    } catch (e) {
      resolve({
        primary:  'rgb(100, 40, 40)',
        presence: 'rgb(40, 40, 100)',
      });
    }
  });
}

function parseRgb(str) {
  const m = str.match(/rgb\((\d+),\s*(\d+),\s*(\d+)\)/);
  return m ? { r: +m[1], g: +m[2], b: +m[3] } : { r: 80, g: 80, b: 80 };
}

function rgbToHex(c) {
  const h = (n) => n.toString(16).padStart(2, "0");
  return "#" + h(c.r) + h(c.g) + h(c.b);
}

// Drive the design's accent / glow CSS variables from album art colours
function applyAccentVars(primary, presence) {
  const root = document.documentElement.style;
  root.setProperty("--accent", rgbToHex(primary));
  root.setProperty("--accent-glow", `rgba(${primary.r}, ${primary.g}, ${primary.b}, 0.55)`);
  root.setProperty("--c1", rgbToHex(primary));
  root.setProperty("--c2", rgbToHex(presence));
}

// Stash the new palette and drive the song-change sequence. The colours are
// NOT applied here — applyContent() snaps them at the exact cover-swap moment
// (while the background + side glow are dimmed), so they never change ahead of
// the swap. The old colour-wipe transition is intentionally gone.
function updateBackgroundColors(colors) {
  pendingColors = {
    primary: parseRgb(colors.primary),
    presence: parseRgb(colors.presence),
  };
  bgHasBeenSet = true;
  applyPendingMetadata();
}

// Snap the stashed palette into the live colour globals. Called from
// applyContent() — i.e. exactly when the cover image is swapped.
function applyPendingColors() {
  if (!pendingColors) return;
  vizPrimary = { ...pendingColors.primary };
  vizPresence = { ...pendingColors.presence };
  bgPrimaryTarget = { ...pendingColors.primary };
  bgPrimary = { ...pendingColors.primary };
  bgPresenceTarget = { ...pendingColors.presence };
  bgPresence = { ...pendingColors.presence };
  applyAccentVars(vizPrimary, vizPresence);
  pendingColors = null;
}

// Cookie helpers
function getCookie(name) {
  const match = document.cookie.match(new RegExp('(^| )' + name + '=([^;]+)'));
  return match ? match[2] : null;
}

function setCookie(name, value, days) {
  const d = new Date();
  d.setTime(d.getTime() + days * 24 * 60 * 60 * 1000);
  document.cookie = name + "=" + value + ";expires=" + d.toUTCString() + ";path=/";
}

// Background animation toggle
// Default: OFF. Only auto-enable on capable desktop devices.
// Cheap upfront gate. Kept deliberately permissive — it only rules out devices
// that clearly can't cope (reduced-motion, phones, a software GPU, very low
// core/RAM). The real decision is made at runtime by the FPS probe below, which
// measures actual performance with the real workload instead of guessing from
// specs (browsers don't expose enough hardware detail to judge "grunt" reliably).
function shouldEnableAnimations() {
  // Hard opt-out: user prefers reduced motion
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return false;

  // Phones/tablets by user-agent (the layout + effects are desktop-oriented).
  // NOTE: we no longer disable on touch capability alone — plenty of capable
  // laptops are touchscreens and were being wrongly excluded.
  if (/Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(navigator.userAgent)) return false;

  // Only rule out genuinely low-end machines.
  if (navigator.hardwareConcurrency && navigator.hardwareConcurrency < 4) return false;
  if (navigator.deviceMemory && navigator.deviceMemory < 4) return false;

  // Software / known-weak GPU via the WebGL renderer string (when the browser
  // still exposes it — many now mask it for privacy, which is fine).
  try {
    const canvas = document.createElement('canvas');
    const gl = canvas.getContext('webgl') || canvas.getContext('experimental-webgl');
    if (!gl) return false; // no WebGL at all — likely very weak
    const debugInfo = gl.getExtension('WEBGL_debug_renderer_info');
    if (debugInfo) {
      const renderer = String(gl.getParameter(debugInfo.UNMASKED_RENDERER_WEBGL)).toLowerCase();
      if (renderer.includes('swiftshader') || renderer.includes('llvmpipe') ||
          renderer.includes('software') || renderer.includes('microsoft basic')) {
        return false;
      }
    }
  } catch (e) {
    // Couldn't probe WebGL — don't hold that against the device; let the FPS
    // probe be the judge rather than disabling outright.
  }

  return true;
}

function updateToggleButton() {
  const btn = document.getElementById('bgToggleBtn');
  if (!btn) return;
  const isOff = document.body.classList.contains('no-bg-animation');
  btn.classList.toggle('active', !isOff);
  btn.title = isOff ? 'Background effects: off' : 'Background effects: on';
}

function toggleBgAnimation() {
  const isOff = document.body.classList.toggle('no-bg-animation');
  setCookie('bgAnimation', isOff ? 'off' : 'on', 365);
  // User made an explicit choice — stop second-guessing their hardware
  perfWatchDone = true;
  updateToggleButton();
  if (isOff) {
    stopBg();
    stopViz();
  } else {
    startBg();
    // Turning effects on mid-playback: build the Web Audio graph now if needed.
    if (isPlaying) enableAudioGraph();
  }
  ensureSpin();
}

// Runtime performance guard: while effects are auto-enabled, sample the real frame
// rate. If the device can't sustain it, turn effects off and remember the choice so
// future page loads start without them.
let perfWatchActive = false;
let perfWatchDone = false;
function startPerfWatch() {
  if (perfWatchDone || perfWatchActive) return;
  // Never override effects the user explicitly switched on
  if (getCookie('bgAnimation') === 'on') { perfWatchDone = true; return; }
  if (document.body.classList.contains('no-bg-animation')) return;

  perfWatchActive = true;
  const WINDOW_MS = 2000;
  const MIN_FPS = 30;        // only bail on genuinely choppy framerates
  const MAX_STRIKES = 2;     // require two bad windows in a row, not one hitch
  const deadline = performance.now() + 14000; // give the user time to press play (viz is heavier)
  let winStart = 0;
  let frames = 0;
  let strikes = 0;

  const tick = (t) => {
    // Effects were turned off elsewhere — stop watching
    if (document.body.classList.contains('no-bg-animation')) { perfWatchActive = false; return; }
    // Background tab throttles rAF — don't count that as poor performance
    if (document.hidden) { winStart = 0; requestAnimationFrame(tick); return; }
    if (!winStart) { winStart = t; frames = 0; requestAnimationFrame(tick); return; }

    frames++;
    const elapsed = t - winStart;
    if (elapsed >= WINDOW_MS) {
      const fps = (frames / elapsed) * 1000;
      if (fps < MIN_FPS) {
        strikes++;
        if (strikes >= MAX_STRIKES) {
          perfWatchActive = false;
          perfWatchDone = true;
          document.body.classList.add('no-bg-animation');
          setCookie('bgAnimation', 'off', 365);
          updateToggleButton();
          stopBg();
          stopViz();
          ensureSpin();
          if (typeof showToast === 'function') showToast('Effects disabled to keep things smooth');
          return;
        }
      } else {
        strikes = 0; // recovered — reset the streak
      }
      if (t >= deadline) { perfWatchActive = false; perfWatchDone = true; return; }
      winStart = t; // start a fresh window
      frames = 0;
    }
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

// Initialize background animation preference
// Cookie always wins. No cookie → default OFF unless device passes all checks.
(function initBgAnimationToggle() {
  if (DISPLAY_MODE) {
    // Always-on effects on a display; never auto-disable for perf.
    document.body.classList.remove('no-bg-animation');
    perfWatchDone = true;
    updateToggleButton();
    startBg();
    return;
  }
  const pref = getCookie('bgAnimation');
  if (pref === 'on') {
    // User explicitly wants effects — respect it
  } else if (pref === 'off' || !shouldEnableAnimations()) {
    document.body.classList.add('no-bg-animation');
  }
  updateToggleButton();
  if (!document.body.classList.contains('no-bg-animation')) {
    startBg();
    // Let the page settle, then verify the device can actually sustain the effects
    setTimeout(startPerfWatch, 1200);
  }
})();


updatePlayUI();

if ("mediaSession" in navigator) {
  navigator.mediaSession.setActionHandler("play", () => togglePlay());
  navigator.mediaSession.setActionHandler("pause", () => buttonStop());
  navigator.mediaSession.setActionHandler("stop", () => buttonStop());
}

connectSSE();

// ---- Toast ----
let toastTimer = null;
function showToast(message) {
  const toast = document.getElementById("toast");
  document.getElementById("toast-text").textContent = message;
  toast.classList.add("show");
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("show"), 2600);
}

// ---- Modals ----
function openModal(id) {
  document.getElementById(id).classList.add("show");
}
function closeModals() {
  document.querySelectorAll(".modal-overlay").forEach((m) =>
    m.classList.remove("show")
  );
  stopSpotifyPreview();
}
function closeModalsFromOverlay(event) {
  if (event.target === event.currentTarget) closeModals();
}
function openRequestModal() {
  openModal("songRequestModal");
  const nameInput = document.getElementById("requesterName");
  const input = document.getElementById("songSearchInput");
  // Require a name first — focus the name field if it's empty
  const target = nameInput && !nameInput.value.trim() ? nameInput : input;
  setTimeout(() => target && target.focus(), 60);
}
function backToRequest() {
  stopSpotifyPreview();
  document.getElementById("songConfirmModal").classList.remove("show");
  openModal("songRequestModal");
}
function openVoteModal() {
  if (!isPlaying || !songId || songId === lastVotedSongId) return;
  openModal("voteSkipModal");
}

function stopSpotifyPreview() {
  const spotifyPreview = document.getElementById("spotify-preview");
  if (spotifyPreview) spotifyPreview.src = "about:blank";
}

// ---- Keyboard shortcuts ----
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    closeModals();
    return;
  }
  // Space = play/stop, but never while typing, on a focused control (buttons
  // and the slider handle Space/keys themselves), or with a modal open.
  if (e.code === "Space") {
    const t = e.target;
    if (
      t &&
      (t.tagName === "INPUT" ||
        t.tagName === "TEXTAREA" ||
        t.tagName === "BUTTON" ||
        t.isContentEditable ||
        (t.getAttribute && t.getAttribute("role") === "slider"))
    )
      return;
    if (document.querySelector(".modal-overlay.show")) return;
    e.preventDefault(); // don't scroll the page
    onPlayClick();
  }
});

// ---- Search / request / vote ----
document.addEventListener("DOMContentLoaded", function () {
  const songSearchInput = document.getElementById("songSearchInput");
  const searchResults = document.getElementById("searchResults");
  const requesterNameInput = document.getElementById("requesterName");

  if (!audioStream.paused) {
    isPlaying = true;
    startPolling();
    updatePlayUI();
  } else if (DISPLAY_MODE) {
    // Autoplay on a display (e.g. OBS browser source). Browsers (incl. OBS's
    // Chromium/CEF) block autoplay WITH SOUND without a user gesture, so start
    // muted — muted autoplay is always allowed — then unmute once playback has
    // actually begun. Also resume the Web Audio context, which can start suspended.
    audioStream.muted = true;
    togglePlay();
    const nudge = setInterval(() => {
      if (audioContext && audioContext.state === "suspended") audioContext.resume();
      if (audioStream.paused) audioStream.play().catch(() => {});
      const running = !audioStream.paused &&
        (!audioContext || audioContext.state === "running");
      if (running) {
        audioStream.muted = false; // we're playing — bring the sound in
        updateButtons();
        clearInterval(nudge);
      }
    }, 800);
    setTimeout(() => clearInterval(nudge), 30000);
    // Last-resort: any interaction (if the source is ever clicked) kicks it off.
    ["pointerdown", "keydown"].forEach((ev) =>
      document.addEventListener(ev, () => {
        if (audioContext && audioContext.state === "suspended") audioContext.resume();
        audioStream.muted = false;
        if (audioStream.paused) togglePlay();
      }, { once: true })
    );
  }

  // Ensure a UUID exists for this device on first load
  getOrCreateUUID();

  // Load the saved requester name
  const savedName = storageGet("requesterName");
  if (savedName) requesterNameInput.value = savedName;

  // Volume slider: desktop shows it, mobile relies on hardware volume
  const isMobileUA = navigator.userAgent.toLowerCase().match(/mobile/i);
  if (isMobileUA) {
    document.getElementById("div-volume").style.display = "none";
    audioStream.volume = 1.0;
  } else {
    const volumeStored = storageGet("volume");
    const v = volumeStored != null ? parseInt(volumeStored, 10) : 50;
    setVolumeUI(v);
    volumeSet(v);
  }

  function debounce(func, wait) {
    let timeout;
    return function executedFunction(...args) {
      const later = () => {
        clearTimeout(timeout);
        func(...args);
      };
      clearTimeout(timeout);
      timeout = setTimeout(later, wait);
    };
  }

  function searchSongs() {
    // Require a name before searching
    if (!requesterNameInput.value.trim()) {
      searchResults.innerHTML =
        '<div class="results-msg">Enter your name first to search.</div>';
      return;
    }

    const query = songSearchInput.value.trim();
    if (query.length < 2) {
      searchResults.innerHTML =
        '<div class="results-msg">Start typing to search for a song.</div>';
      return;
    }

    fetch("/api/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query, uuid: getOrCreateUUID() }),
    })
      .then((response) => response.json())
      .then((data) => {
        if (data.error || data.detail) {
          displayMessage(data.error || data.detail);
        } else {
          displaySearchResults(data);
        }
      })
      .catch((error) => {
        console.error("Error:", error);
        displayMessage("An error occurred while searching.");
      });
  }

  songSearchInput.addEventListener("input", debounce(searchSongs, 300));
  // Re-run the search once a name is entered (clears the "enter your name" prompt)
  requesterNameInput.addEventListener("input", debounce(searchSongs, 300));

  function displaySearchResults(data) {
    searchResults.innerHTML = "";
    const tracks = data.results || [];

    if (tracks.length === 0) {
      displayMessage("No songs found.");
      return;
    }

    tracks.forEach((track) => {
      const item = document.createElement("div");
      item.className = "result-item";
      const cover = track.cover
        ? `<img src="${escapeHtml(track.cover)}" alt="" class="result-art">`
        : `<div class="result-art"></div>`;
      item.innerHTML = `
        ${cover}
        <div class="result-text">
          <div class="result-title">${escapeHtml(track.name)}</div>
          <div class="result-artist">${escapeHtml(track.artist)}</div>
        </div>
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="var(--muted)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"></line><line x1="5" y1="12" x2="19" y2="12"></line></svg>
      `;
      item.addEventListener("click", () => showConfirmation(track));
      searchResults.appendChild(item);
    });
  }

  function showConfirmation(track) {
    selectedSongData = track;

    document.getElementById("confirm-song-image").src = track.cover || "";
    document.getElementById("confirm-song-title").textContent = track.name;
    document.getElementById("confirm-song-artist").textContent = track.artist;

    const spotifyPreview = document.getElementById("spotify-preview");
    spotifyPreview.src = `https://open.spotify.com/embed/track/${track.id}`;

    document.getElementById("songRequestModal").classList.remove("show");
    openModal("songConfirmModal");
  }

  document
    .getElementById("confirmRequestBtn")
    .addEventListener("click", function () {
      if (!selectedSongData) return;

      const requesterName = requesterNameInput.value.trim();
      if (!requesterName) {
        showToast("Please enter your name first");
        backToRequest();
        return;
      }

      // Disable button to prevent multiple submissions
      this.disabled = true;
      this.innerHTML = '<span class="spinner"></span> Sending…';

      // Persist the name for future requests
      storageSet("requesterName", requesterName);

      // Stop the Spotify preview before sending the request
      stopSpotifyPreview();

      const submissionId =
        Date.now() + "-" + Math.random().toString(36).substr(2, 9);
      requestSong(selectedSongData.id, submissionId);
    });

  function requestSong(uri, submissionId) {
    const confirmBtn = document.getElementById("confirmRequestBtn");
    const turnstileToken = confirmBtn.dataset.turnstileToken;
    if (!turnstileToken) {
      showToast("Please complete the challenge");
      confirmBtn.disabled = false;
      confirmBtn.innerHTML = "Request";
      return;
    }
    const requesterName = requesterNameInput.value.trim();

    fetch("/api/request", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        uri,
        name: requesterName,
        submission_id: submissionId,
        turnstile: turnstileToken,
        uuid: getOrCreateUUID(),
      }),
    })
      .then((response) => response.json())
      .then((data) => {
        confirmBtn.innerHTML = "Request";

        if (data.success) {
          closeModals();
          showToast(data.message || "Added to the queue");
        } else {
          showToast(data.error || data.detail || "Failed to send song request.");
        }

        // The token was consumed either way — the button stays disabled until
        // the fresh challenge issues a new one via enableConfirmButton.
        resetRequestChallenge();
      })
      .catch((error) => {
        console.error("Error:", error);
        confirmBtn.innerHTML = "Request";
        showToast("An error occurred while sending the request.");
        resetRequestChallenge();
      });
  }

  function displayMessage(message) {
    searchResults.innerHTML = "";
    const msg = document.createElement("div");
    msg.className = "results-msg";
    msg.textContent = message;
    searchResults.appendChild(msg);
  }

  document
    .getElementById("confirmVoteSkipBtn")
    .addEventListener("click", function () {
      const turnstileToken = this.dataset.turnstileToken;
      if (!turnstileToken) {
        showToast("Please complete the challenge");
        return;
      }

      this.disabled = true;
      this.innerHTML = '<span class="spinner"></span> Submitting…';

      const userUUID = getOrCreateUUID();

      fetch("/api/skip", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ uuid: userUUID, songid: songId, turnstile: turnstileToken }),
      })
        .then((response) => response.json().then((data) => ({ ok: response.ok, data })))
        .then(({ ok, data }) => {
          if (ok && data.success) {
            lastVotedSongId = songId;
            updateButtons();
            closeModals();
            showToast("Skip vote counted");
          } else {
            showToast(data.detail || data.error || "Failed to submit vote");
          }
          this.innerHTML = "Vote to skip";
          try {
            resetVoteChallenge();
          } catch (e) {
            console.error("Error resetting turnstile:", e);
          }
        })
        .catch((error) => {
          console.error("Error:", error);
          showToast("An error occurred while submitting your vote");
          this.innerHTML = "Vote to skip";
          try {
            resetVoteChallenge();
          } catch (e) {
            console.error("Error resetting turnstile:", e);
          }
        });
    });
});

audioStream.addEventListener("ended", function () {
  isPlaying = false;
  stopPolling();
  updatePlayUI();
});

document
  .getElementById("muteSongBtn")
  .addEventListener("click", function () {
    if (!isPlaying || !songId) return;

    if (audioStream.muted) {
      audioStream.muted = false;
      mutedSongId = null;
    } else {
      audioStream.muted = true;
      mutedSongId = songId;
    }
    updateButtons();
  });
