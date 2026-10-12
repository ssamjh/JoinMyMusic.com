# Liquidsoap input stage. It reads the Spotify PCM from the FIFO, fills gaps with
# silence (mksafe), applies a fixed Spotify input trim, mixes in the optional live inputs
# (the VBAN receiver and the microphone, each with its own trim), runs the slow gated input
# AGC, and writes 44.1 kHz stereo s16le PCM to stdout. The rest of the radio processing is
# in config/processing.ffmpeg, run by FFmpeg after this.

settings.log.stdout.set(false)
settings.log.file.set(false)

input_fifo = getenv(default="/run/audio/liquidsoap-input.pcm", "RADIO_INPUT_FIFO")
raw = input.external.rawaudio(
  id="jmm_pcm",
  buffer=0.10,
  max=0.20,
  channels=2,
  samplerate=44100,
  restart=true,
  "cat #{string.quote(input_fifo)}"
)
raw = mksafe(raw)

# Spotify input trim before the mix and the AGC: spotify.trim_db in audio.yml, passed
# in by the supervisor (default -12 dB).
spotify_trim_db = float_of_string(getenv(default="-12.0", "SPOTIFY_TRIM_DB"))
raw = amplify(lin_of_dB(spotify_trim_db), raw)

# Live input bus: the optional VBAN receiver and the optional microphone, each trimmed and
# summed with Spotify. A source that is off is blank (silent), so the bus works with either
# input alone or both. Each input is mksafe, so the mix never waits on a sender.
#  - VBAN receiver: vban_receptor writes the sender's 16-bit stereo PCM to a FIFO at 44.1 kHz,
#    so it is not resampled. Its jitter buffer is vban_receiver.quality (medium = 0.4 s).
#  - Microphone: the control API (POST /api/mic) writes the browser microphone, duplicated to
#    stereo, to a FIFO at 44.1 kHz. Nothing is written between sessions, so the input is silent
#    then. Its buffer is 0.15 s.
# The inputs are measured before the trims, for the level command and the ducking below.
vban_fifo = getenv("VBAN_INPUT_FIFO")
mic_fifo = getenv("MIC_INPUT_FIFO")
radio = if vban_fifo == "" and mic_fifo == "" then
  raw
else
  # Jitter buffer from vban_receiver.quality in audio.yml (medium = 0.4 s).
  vban_buffer = float_of_string(getenv(default="0.4", "VBAN_INPUT_BUFFER"))
  vban_in = if vban_fifo == "" then
    blank()
  else
    mksafe(input.external.rawaudio(
      id="jmm_vban_in",
      buffer=vban_buffer,
      max=2.0 * vban_buffer,
      channels=2,
      samplerate=44100,
      restart=true,
      "cat #{string.quote(vban_fifo)}"
    ))
  end
  mic_in = if mic_fifo == "" then
    blank()
  else
    mksafe(input.external.rawaudio(
      id="jmm_mic_in",
      buffer=0.15,
      max=0.30,
      channels=2,
      samplerate=44100,
      restart=true,
      "cat #{string.quote(mic_fifo)}"
    ))
  end
  # Each input is measured in its own path to the mix, so the measurements run on every
  # frame (a source that reaches no output is never computed). The bus level is the power
  # sum of the two: each input's level over 50 ms for the ducking below, and over 1 s for
  # the level command.
  vban_fast = rms(duration=0.05, vban_in)
  vban_in = rms(duration=1.0, vban_fast)
  mic_fast = rms(duration=0.05, mic_in)
  mic_in = rms(duration=1.0, mic_fast)
  def bus_level(a, b) =
    sqrt(a * a + b * b)
  end
  # The bus's level (RMS dBFS, before the trims), read by the control API's /api/source.
  # The API uses it to tell when the stream is live input alone.
  server.register(
    description="Live input bus RMS level in dBFS, before the trims.",
    "live_level",
    fun (_) -> begin
      level = bus_level(vban_in.rms(), mic_in.rms())
      if level > 0. then string_of(dB_of_lin(level)) else "-200." end
    end
  )
  # Input trims: vban_receiver.trim_db (default -6 dB) and mic.trim_db (default -6 dB) in audio.yml.
  vban_trim_db = float_of_string(getenv(default="-6.0", "VBAN_INPUT_TRIM_DB"))
  mic_trim_db = float_of_string(getenv(default="-6.0", "MIC_INPUT_TRIM_DB"))
  # Ducking: while the live bus is above duck_threshold_db (RMS dBFS, before the trims), Spotify
  # is turned down by duck_db (live_input.duck_db in audio.yml; 0 is off) at duck_attack_db_s.
  # duck_hold seconds after the bus falls below the threshold, so Spotify does not come up
  # between words; it returns at duck_release_db_s.
  duck_db = float_of_string(getenv(default="0.0", "LIVE_DUCK_DB"))
  duck_threshold_db = -40.0
  duck_hold = 0.6
  duck_attack_db_s = 30.0
  duck_release_db_s = 8.0
  duck_gain_db = ref(0.)
  duck_held = ref(0.)
  # Called once per frame. The step is the frame duration, so the rates and the hold stay
  # in real time whatever the frame size.
  def duck_update() =
    duck_step = frame.duration()
    level = bus_level(vban_fast.rms(), mic_fast.rms())
    if level > 0. and dB_of_lin(level) > duck_threshold_db then
      duck_held := duck_hold
    else
      duck_held := max(0., !duck_held - duck_step)
    end
    wanted = if !duck_held > 0. then duck_db else 0. end
    change = wanted - !duck_gain_db
    if change < 0. then
      duck_gain_db := !duck_gain_db + max(change, 0. - duck_attack_db_s * duck_step)
    else
      duck_gain_db := !duck_gain_db + min(change, duck_release_db_s * duck_step)
    end
  end
  mixed = add(normalize=false, [
    amplify({lin_of_dB(!duck_gain_db)}, raw),
    amplify(lin_of_dB(vban_trim_db), vban_in),
    amplify(lin_of_dB(mic_trim_db), mic_in)
  ])
  if duck_db < 0. then source.on_frame(mixed, duck_update) else mixed end
end

# A copy of the input mix, before the AGC, for the admin monitor's input meters. It is
# after the input trims, so with the AGC and processing bypassed it matches the output.
# The supervisor always reads it, so this output never holds up the stream.
monitor_fifo = getenv("MONITOR_INPUT_FIFO")
if monitor_fifo != "" then
  ignore(output.external(
    id="jmm_monitor",
    %wav(header=false, samplesize=16, samplerate=44100, channels=2),
    "cat > #{string.quote(monitor_fifo)}",
    radio
  ))
end

# Input AGC: a gated leveler. On every frame (40 ms at the default frame duration) it moves
# its gain toward the gain that would put the input RMS (agc_window) at agc_target_db, at
# fixed rates: agc_attack_db_s when turning down and agc_release_db_s when turning up.
# Fixed rates move small and large level differences at the same pace, so the gain rides
# the programme level rather than following phrases. The ratio is infinite (full levelling) within agc_range_db.
# Two thresholds stop it chasing quiet material:
# - gating: input more than agc_gate_db below the target may turn the gain down but not
#   up, so quiet passages, breaths and room noise are not pumped up;
# - freeze: input below agc_freeze_db (RMS dBFS) holds the gain, so pauses in speech and
#   the ends of fade-outs keep the gain they had.
# The admin monitor's AGC bypass sets agc_bypass through the server socket; the AGC keeps
# running while bypassed, so its gain is current when the bypass ends.
# JMM_AGC_BYPASS=1 is for the tests that check exact trim and mix gains.
agc_target_db = -13.0
agc_range_db = 15.0
agc_attack_db_s = 1.2
agc_release_db_s = 2.0
agc_gate_db = -30.0
agc_freeze_db = -50.0
agc_window = 0.3
control_socket = getenv("LIQUIDSOAP_SOCKET")
if control_socket != "" then
  settings.server.socket.set(true)
  settings.server.socket.path.set(control_socket)
end
agc_bypass = interactive.bool("agc_bypass", false)
radio = if getenv("JMM_AGC_BYPASS") == "1" then
  radio
else
  measured = rms(duration=agc_window, radio)
  agc_gain_db = ref(0.)
  # Called once per frame, with the frame duration as the step (see duck_update).
  def agc_update() =
    agc_step = frame.duration()
    level = dB_of_lin(measured.rms())
    if level > agc_freeze_db then
      wanted = max(0. - agc_range_db, min(agc_range_db, agc_target_db - level))
      change = wanted - !agc_gain_db
      if change < 0. then
        agc_gain_db := !agc_gain_db + max(change, 0. - agc_attack_db_s * agc_step)
      elsif level > agc_target_db + agc_gate_db then
        agc_gain_db := !agc_gain_db + min(change, agc_release_db_s * agc_step)
      end
    end
  end
  # The AGC gain in dB, read by the admin monitor's AGC meter.
  server.register(
    description="Input AGC gain in dB.",
    "agc_gain",
    fun (_) -> string_of(!agc_gain_db)
  )
  leveled = add(normalize=false, [
    amplify({if agc_bypass() then 0. else lin_of_dB(!agc_gain_db) end}, measured),
    amplify({if agc_bypass() then 1. else 0. end}, radio)
  ])
  source.on_frame(leveled, agc_update)
end

# Headroom for the 16-bit link to FFmpeg. When speech starts over music the AGC takes
# a moment to turn down, and peaks over full scale would be hard-clipped in the s16
# output. The first stage of config/processing.ffmpeg adds this back in floating point,
# where its limiters and clippers handle the peaks. Keep the two values matched.
link_headroom_db = -12.0
radio = amplify(lin_of_dB(link_headroom_db), radio)

# header=false makes this an interleaved 44.1 kHz stereo s16le stream on stdout.
output.external(
  %wav(header=false, samplesize=16, samplerate=44100, channels=2),
  "cat",
  radio
)
