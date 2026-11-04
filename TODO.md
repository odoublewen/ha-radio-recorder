# TODO

## First-class FLAC (and other non-MP3) recordings

KALX also streams lossless FLAC at `https://stream.kalx.berkeley.edu:8443/kalx.flac`
(Icecast 2.4.4). This is what a 10 s test recording with the 0.1.0 code showed:

- The server sends **FLAC inside Ogg** with `Content-Type: audio/ogg`, so the file is
  saved as `<show> - <timestamp>.ogg`. The audio is fine. mutagen detects it as
  `OggFLAC`, 44.1 kHz.
- **It isn't tagged.** `tagging.finalize` only tags `.mp3`. mutagen can tag the file;
  this worked on the test file:
  `m = mutagen.File(path, easy=True); m.add_tags() if m.tags is None; m["artist"] = ...; m.save()`.
- **The duration is wrong** (mutagen reported about 559,000 s for a 10 s file). The Ogg
  timestamps (granule positions) carry on from where the live broadcast was when the
  recording joined, so players may show the wrong length or seek badly.
- **Each reconnect adds a new Ogg stream to the file**, with its own FLAC headers
  ("chained Ogg"). Many players cope, but some FLAC tools and mutagen only see the first
  one. MP3 has no such issue, because its frames join cleanly.

### 1. Tag every format, not just MP3

- In `tagging.py`, replace the `.mp3`-only `tag_mp3` with a general `tag_file` built on
  `mutagen.File(path, easy=True)`. The easy keys (`artist`, `album`, `title`, `date`,
  `genre`) map to ID3 frames for MP3 and to Vorbis comments for Ogg/FLAC/Opus.
- Keep the current MP3 behaviour. A raw MP3 stream has no ID3 header, and
  `mutagen.File` may return `None` for it, so keep the `EasyID3()` fallback.
- Formats mutagen can't read or tag (raw ADTS `.aac`) are still saved untagged, with a
  debug log line, as now.
- Tests in `tests/test_tagging.py`: a small real Ogg FLAC file is needed as a fixture.
  Ten seconds of the KALX stream is about 600 KB, which is too big; generate a tiny one,
  or keep the first few KB of a recording, which is just the Ogg FLAC headers plus a
  few frames.

### 2. Convert Ogg FLAC to plain `.flac` with ffmpeg

- After recording, and before tagging, run
  `ffmpeg -nostdin -v error -i <part> -map 0:a -c:a copy -f flac <tmp>.flac` (copy the
  audio, no re-encode). That gives a normal FLAC file with a correct
  STREAMINFO duration, and it handles the chained streams from reconnects. **Check**
  that ffmpeg reads past the first chained stream. If it stops at the first one,
  either convert each connection's segment separately and join them, or write each
  connection to its own part file (see below).
- ffmpeg is in the official HA container image (`/usr/bin/ffmpeg`). The HA way to find
  it is `homeassistant.components.ffmpeg.get_ffmpeg_manager(hass).binary`, which needs
  `"ffmpeg"` in the manifest's `dependencies` or `after_dependencies`. Run it as an
  asyncio subprocess, not in the executor.
- If ffmpeg is missing or fails, keep the `.ogg` file and tag that. A recording must
  never be lost because the conversion failed.
- Consider the same conversion for `audio/aac` (ADTS → `.m4a` with
  `-c:a copy -bsf:a aac_adtstoasc`), so AAC streams can be tagged too. It's optional.

### Design notes

- An alternative to ffmpeg's chain handling is to give `StreamRecorder` one part file
  per connection (`<id>.0.part`, `<id>.1.part`, ...). Then `finalize` joins MP3 parts
  by appending them, and converts or joins Ogg parts with ffmpeg's concat demuxer.
  Restart resumption would then need to know the next part number; it's in `Recording`
  already as `reconnects`. Only do this if ffmpeg can't handle the chained single file.
- `extension_for()` maps `audio/ogg` → `.ogg`. After conversion, the final extension
  comes from the format actually written (`.flac`), not from the content type.
- Update the README's "How it records" section: what happens to non-MP3 streams.

### Verification

- `uv run pytest`, plus `ruff check` / `ruff format --check`.
- Live: the scratchpad script approach used for 0.1.0 still works. Build a `Recording`
  pointing at the FLAC URL, run `StreamRecorder` for about 15 s, then `finalize`, and
  check with mutagen that the file is `FLAC`, its duration is about 15 s, and its tags
  are set. Also test with a forced reconnect (e.g. cancel and resume, or a short
  `stall_timeout`) to confirm the joined file plays through both segments.
- In HA, after installing the new version through HACS, use `radio_recorder.record`
  with `url: https://stream.kalx.berkeley.edu:8443/kalx.flac` for 2 minutes and check
  the file under `/media/music/KALX/`.

## Smaller follow-ups

- HACS validation warns that the manifest has no integration `categories`. It's
  harmless; add one if HA's category list has a good fit.
- After the FLAC work, bump `version` in `manifest.json` and `pyproject.toml` (0.2.0)
  and make a GitHub release, so HACS offers the update.
