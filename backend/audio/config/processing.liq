# Liquidsoap input stage. It reads the Spotify PCM from the FIFO, fills gaps with
# silence (mksafe), applies a fixed Spotify input trim, mixes in the optional VBAN
# receiver input (with its own trim), runs the slow gated input AGC, and writes 44.1 kHz
# stereo s16le PCM to stdout. The rest of the radio processing is in
# config/processing.ffmpeg, run by FFmpeg after this.

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

# Fixed Spotify input trim before processing (-12 dB).
spotify_trim_db = -12.0
raw = amplify(lin_of_dB(spotify_trim_db), raw)

# Optional VBAN receiver input (vban_receiver in audio.yml). vban_receptor writes the
# sender's 16-bit stereo PCM to this FIFO, at 44.1 kHz like the rest of the chain, so it
# is not resampled. It is trimmed, summed with Spotify, and is silent while no sender is live.
vban_fifo = getenv("VBAN_INPUT_FIFO")
# Jitter buffer from vban_receiver.quality in audio.yml (medium = 0.4 s).
vban_buffer = float_of_string(getenv(default="0.4", "VBAN_INPUT_BUFFER"))
radio = if vban_fifo == "" then
  raw
else
  vban_in = input.external.rawaudio(
    id="jmm_vban_in",
    buffer=vban_buffer,
    max=2.0 * vban_buffer,
    channels=2,
    samplerate=44100,
    restart=true,
    "cat #{string.quote(vban_fifo)}"
  )
  # The input's level (RMS dBFS, before its trim), read by the control API's /api/source.
  # The API uses it to tell when the stream is the VBAN input alone.
  vban_in = rms(duration=1.0, mksafe(vban_in))
  server.register(
    description="VBAN input RMS level in dBFS.",
    "vban_level",
    fun (_) -> begin
      level = vban_in.rms()
      if level > 0. then string_of(dB_of_lin(level)) else "-200." end
    end
  )
  # Fixed VBAN input trim, for headroom when speech and music play together.
  vban_trim_db = -6.0
  add(normalize=false, [raw, amplify(lin_of_dB(vban_trim_db), vban_in)])
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

# Input AGC: a gated leveler. Every 20 ms it moves its gain toward the gain that would
# put the input RMS (agc_window) at agc_target_db, at fixed rates: agc_attack_db_s when
# turning down and agc_release_db_s when turning up. Fixed rates move small and large
# level differences at the same pace, so the gain rides the programme level rather than
# following phrases. The ratio is infinite (full levelling) within agc_range_db.
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
agc_step = 0.02
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
  def agc_update() =
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
  thread.run(every=agc_step, agc_update)
  # The AGC gain in dB, read by the admin monitor's AGC meter.
  server.register(
    description="Input AGC gain in dB.",
    "agc_gain",
    fun (_) -> string_of(!agc_gain_db)
  )
  add(normalize=false, [
    amplify({if agc_bypass() then 0. else lin_of_dB(!agc_gain_db) end}, measured),
    amplify({if agc_bypass() then 1. else 0. end}, radio)
  ])
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
