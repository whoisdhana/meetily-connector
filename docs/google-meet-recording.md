# Recording Google Meet calls with Meetily

Meetily captures **system audio** (the remote participants) with a macOS Core
Audio process tap, plus your **microphone**, and mixes them into one recording.
There is no Meet integration to configure — you just record while the call runs.
But three quirks (verified against Meetily v0.4.0 source) are worth knowing,
because each can silently ruin a recording.

## One-time setup

1. **macOS 14.4 or newer** (Core Audio taps requirement).
2. Grant both permissions:
   - Microphone (normal prompt), and
   - **System Settings → Privacy & Security → Audio Capture** → meetily ON.

   ⚠️ Check the Audio Capture pane yourself. Meetily's in-app permission check
   is a stub — if the permission is denied, recording can *appear to work while
   capturing pure silence*. (Some Meetily error messages also point to
   "Screen Recording" or "BlackHole" — both are outdated; Audio Capture is the
   one that matters.)
3. **Do a 20-second self-test**: start a Meet with yourself, play any sound,
   and confirm **both level meters** in Meetily's device selector are moving.
   Moving meters are the only reliable proof the tap is alive.

## Every meeting

1. Join Meet in the browser as usual.
2. **Wear headphones/AirPods.** Remote voices are still captured (the tap
   intercepts audio *before* it reaches the headphones), and headphones prevent
   echo — Meetily's mixer has no echo cancellation, so on loudspeakers the
   remote voices leak into your mic and get recorded twice.
3. AirPods users: Meetily silently switches recording to the **built-in Mac
   microphone** when your default mic is Bluetooth (to avoid sample-rate
   problems). That's fine — your voice is still recorded. If you specifically
   want the AirPods mic, pick it explicitly in Meetily's device dropdown.
4. Hit record in Meetily when the call starts; stop when it ends.
5. Everything else is automatic: within ~1–2 minutes the transcript is
   generated, the notes file is written, the summary arrives, and action items
   land in the task store.

## Facts worth knowing

- The recording is **one mixed mono track** — speaker separation in your notes
  comes from Gemini's diarization of that track, not from separate channels.
- If a recording *plays back* pitch-shifted on Bluetooth headphones, the file
  itself is fine — it's a macOS Bluetooth playback bug; check on speakers or
  wired output.
- Meetings shorter than `min_duration_seconds` (config) are deliberately
  skipped as accidental recordings.
