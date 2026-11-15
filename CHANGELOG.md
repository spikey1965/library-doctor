# Changelog

## 0.1.5 (beta)

- **App Health (new tile):** every app's own warnings in one place: Radarr, Sonarr and Prowlarr health checks, Bazarr health, Tdarr nodes, and any app that stops answering. "Update available" shows as a quieter card you can ignore. New warnings go to your notifications once a day. Prowlarr can now be added in Settings.
- **Trash expiry warning:** a banner the day before files are deleted for good ("32 GB will be permanently deleted tomorrow — Review now"), counting Library Doctor's trash and the Radarr/Sonarr/qbit_manage recycle bins. The nightly summary includes the recycle bins too.
- **Plex backup reminder:** before a bulk clean-up (clean up all, replace all, Empty Trash, bulk titles/posters) a gentle reminder offers "Back up Plex now", which starts Plex's own database backup. "Don't ask again today" if you're on a roll.
- **Light mode:** follows your device, or pick light/dark with the ☀/☾ button in the top bar.
- **Review all fixes:** one page listing what every suggested fix would do (files to the trash and their size, downloads, conversions, Plex changes), with Download CSV and Print / save as PDF. Nothing runs from it: scans never change anything and every fix waits for your click.
- **Plex tidy-up:** a sidebar button that runs Plex's own housekeeping now: Clean Bundles (artwork and metadata nothing uses any more, such as old replaced posters) and Optimize Database. Plex's "Empty trash" is deliberately left out.

**Fixed**
- **Scan now could stay greyed out until you reloaded the page** when Library Doctor sits behind a login like Authentik and that login had expired. Now any button shows "Your login has expired — reload the page" with a Reload button, Scan now always comes back with a message, and a started scan shows "Scanning…" straight away.

## 0.1.4 (beta)

- Saving Settings keeps any setting the page doesn't show (for example a qBittorrent entry in an older `secrets.json`), instead of dropping it.
- History: the Space saved chart's scale now rounds to whole GB (it could top out at odd values like 465.7 GB).

## 0.1.3 (beta)

- **More notifications:** besides Gotify, the nightly summary (and new Downloads problems) can go to Discord, Telegram, ntfy, any webhook (JSON POST) and email. Add them in Settings → Notifications, each with a Test button; every one you add gets the message.
- **History panel:** space saved over the last 30 or 90 days, issues found and how long scans took, as three small charts with a tooltip per day. It's worked out from what Library Doctor already recorded, so it shows history straight away.
- **Plex editions:** a Theatrical and a Director's Cut (`{edition-...}` or Plex's edition field) are no longer flagged as duplicates. Only copies of the same edition are compared.
- **Automatic scan in Settings:** pick the time and the days of the week, or leave the time empty for no automatic scan. `LD_SCAN_HOUR` still works until you set one.

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
