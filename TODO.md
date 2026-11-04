# TODO

- Verify FLAC in HA: after installing 0.2.0 through HACS, use `radio_recorder.record`
  with `url: https://stream.kalx.berkeley.edu:8443/kalx.flac` for 2 minutes and check
  that `/media/music/KALX/` gets a tagged `.flac` about 2 minutes long.
- Make a GitHub release for 0.2.0 (the version is already bumped), so HACS offers the
  update.
- Ogg Vorbis and Opus streams are tagged but not converted, so they likely have the same
  wrong length and chained-stream problems that Ogg FLAC had. Re-encoding them would be
  lossy; a stream-copy remux (`-c:a copy -f ogg`) might be enough. Check with a real
  stream before doing anything.
- HACS validation warns that the manifest has no integration `categories`. It's
  harmless; add one if HA's category list has a good fit.
