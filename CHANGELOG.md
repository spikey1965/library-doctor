# Changelog

## 0.1.2 (beta)

- **Downloads check (new tile):** reads every Radarr/Sonarr queue. Finished downloads they won't import by themselves get an **Import** button (only files Radarr/Sonarr has no objection to); torrents still at 0% after a day (or your night window) get **Remove & try another** (blocklist + a fresh search, even for unmonitored episodes). Checked hourly; new problems go to Gotify once a day.
- **Search follow-up:** after "Search for better copies", the Done list shows what happened: downloading, imported (with quality), waiting to import, or nothing found. Only "nothing found" hides the card, for 30 days, then it searches again by itself.
- **Monitor button** on Quality & Space cards when Radarr/Sonarr isn't monitoring the flagged film or episodes (it never upgrades those on its own). Monitors just those episodes, never whole seasons. Card buttons now read Open in Plex, Open in Sonarr, Search, Monitor, Ignore.
- **Tdarr failures card:** files Tdarr couldn't convert, with the reason read from Tdarr's job report and a Retry button. A Tdarr plugin for the common "subtitle codec not supported" failure is in `extras/tdarr`.
- **All recycle bins in the Trash screen:** Radarr's and Sonarr's recycle bins (and qbit_manage's, if it's under your downloads folder) are listed with Put Back, plus one summary chip per bin with size and when it next empties.
- Clearing the Done list keeps jobs that are still running (their follow-up used to stop).
- The nightly Gotify summary no longer calls a problem "new" when a fix was already tried; it lists those as "Still there after a fix".

## 0.1.1 (beta)

- The Space saved and Jobs left rings update straight after a scan or a fix (they could lag by up to 30 seconds).
- If your media is spread over several disks or datasets, the main screen now says so, and clean-up explains why a file can't be moved instead of showing a raw "cross-device link" error.
- README: a Known limitations section.

## 0.1.0 (beta)

First public version.

- Nine checks: missing files, duplicates, metadata gaps, file health, quality & space, subtitles, unwatched, virus scan, Coming Soon.
- Settings page: add Plex, Radarr, Sonarr and the optional apps with a Test button for each.
- Folder mapping with Detect folders, for apps that see your media at a different path.
- Built-in login (create a password on first run), or `LD_AUTH=off` behind another login.
- Space saved and Jobs left rings, 7-day trash, 6-month Keep for unwatched items.
