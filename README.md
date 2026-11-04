# Radio Recorder

A Home Assistant integration that records internet radio shows. Schedule shows as
events on a calendar, or start a recording with an action. Each recording is saved as a
tagged MP3 in a folder per station:

```
<output folder>/KALX/Madame X - 2026_10_14_18_15_00.mp3
    artist = KALX    album = Madame X    title = 2026_10_14_18_15_00    genre = Radio
```

It needs no external tools such as streamripper or ffmpeg. The integration fetches the
stream over HTTP and tags the file with [mutagen](https://mutagen.readthedocs.io/).

## Install

Through [HACS](https://hacs.xyz):

1. HACS → ⋮ → **Custom repositories** → add `https://github.com/odoublewen/ha-radio-recorder`
   with type **Integration**.
2. Install **Radio Recorder**, then restart Home Assistant.
3. **Settings → Devices & services → Add integration → Radio Recorder**.

Or by hand: copy `custom_components/radio_recorder` into your Home Assistant
`config/custom_components/` and restart.

Requires Home Assistant 2025.10 or newer.

## Configure

| Setting | |
| --- | --- |
| Output folder | Where recordings go, as seen from inside Home Assistant (default `/media/music`). It must exist and be writable. In a container install, mount your music share into the container. |
| Schedule calendar | Optional. A calendar whose events are recordings; see below. A **Local Calendar** named "Radio" works well. |
| Start early by / keep recording for | Padding around each calendar event, in minutes (default 1 and 2). |
| File owner | Optional `uid:gid` for saved files and station folders. Home Assistant's container usually runs as root. |

Then add stations. On the integration's entry, open **⋮ → Add station** and give a
name (`KALX`) and the direct stream URL
(`http://stream.kalx.berkeley.edu:8000/kalx-256.mp3`). The name becomes the artist tag
and the folder name.

## Scheduling with a calendar

Each timed event on the schedule calendar is one recording:

| Event field | Meaning |
| --- | --- |
| Summary | The show: the album tag and the start of the file name. |
| Location | A station's name (case doesn't matter), or a stream URL. |
| Start / end | When to record, widened by the padding. |

Recurring events record every time, so a weekly show is one repeating event. All-day
events are ignored. An event whose location matches no station fires
`radio_recorder_failed`.

The calendar is read every minute, so you can add, move or delete an event right up to
its start. An event already under way when it's added, or when Home Assistant starts,
begins recording straight away.

Anything that can add calendar events can schedule a recording, for example the
`calendar.create_event` action:

```yaml
action: calendar.create_event
target:
  entity_id: calendar.radio
data:
  summary: Madame X
  location: KALX
  start_date_time: "2026-10-14 18:15:00"
  end_date_time: "2026-10-14 20:00:00"
```

## Actions

`radio_recorder.record` starts recording now. Pass a `duration` or an `end` time. Pass `url` to record a stream that
isn't configured as a station (in that case `station` can be any name).

```yaml
action: radio_recorder.record
data:
  station: KALX
  show: Madame X
  duration: "01:45:00"
```

The response holds the `recording_id`.

`radio_recorder.stop` ends a recording early, given a `recording_id` or a `station`.
What has been recorded so far is still tagged and saved, and the response includes the
saved paths.

Both actions are available over the REST API too, for example
`POST /api/services/radio_recorder/record?return_response`.

## Entities and events

Each station's device has:

* **Recording**: on while a recording is in progress. Its attributes give the show, start and end
  times, bytes received and reconnects.
* **Last recording**: when the last recording finished. The file path and show are in
  its attributes.

The **Radio Recorder** device has **Next recording**, the start of the next scheduled
recording, when a calendar is configured.

When a recording is saved, `radio_recorder_finished` fires with the `path`. When nothing
could be saved, `radio_recorder_failed` fires with an `error`.

## How it records

* The stream is appended to `<output folder>/.incoming/<id>.part` until the end time.
  At the end it's tagged and moved into the station's folder. Existing files are never
  overwritten; a second recording with the same name gets ` (2)`.
* If the connection drops or stalls for 30 s, the integration reconnects with backoff
  (up to 30 s) and keeps appending. MP3 frames concatenate cleanly, so a dropout leaves
  a gap rather than a broken file.
* Recordings survive restarts. If Home Assistant restarts mid-show, or the integration
  reloads after a settings change, the recording picks up where it left off in the
  same file once Home Assistant is back.
* Streams that aren't MP3 (AAC, Ogg) are saved with the right extension but not tagged.
  HLS playlists (`.m3u8`) and web pages are refused.

## Development

```
uv sync
uv run pytest
uv run ruff check && uv run ruff format --check
```
