# Drop Unknown Subs (Tdarr plugin)

Some WEB-DL releases carry a subtitle track that ffmpeg can't identify (`codec unknown`). Tdarr stacks that copy every
stream (`-map 0 -c:s copy`, e.g. Migz FFMPEG) then fail with:

```
Subtitle codec 0 is not supported.
Could not write header (incorrect codec parameters ?)
Conversion failed!
```

Library Doctor's Tdarr card shows this as "a subtitle track ffmpeg can't copy". This plugin removes just those tracks
(stream copy, no re-encode) and re-queues the file, so the rest of your stack can convert it.

**Install:** copy `Tdarr_Plugin_Local_Drop_Unknown_Subs.js` into Tdarr's `server/Tdarr/Plugins/Local/` folder, then in each
library's Transcode Options add it (Local plugins) **before** your transcode plugin. Files without such a track are untouched.
