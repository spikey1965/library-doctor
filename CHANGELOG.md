# Changelog

## 0.1.7 (beta)

- **Folder check (Settings):** every app's view of your media side by side with Library Doctor's, ✓/✗ per app with an example file, and **Use suggested mapping** (or **Remove the mapping**) when they don't line up. Runs by itself when you open Settings and after each save.
- **Scan guard:** before every scan the folders are checked again; if Plex's or Radarr/Sonarr's files can't be found at all, the scan stops with a banner pointing to Folder check, instead of filling Missing Files with your whole library. As a second safety net, a scan where most files look missing doesn't show them and says why.
- **Hardlink aware:** files with another link (usually a torrent still seeding) get a **Hardlinked** badge and a note that moving them frees nothing until the torrent goes; two "copies" that are links to the same file are never offered as duplicates; the space totals and the trash only count space that's really freed.
- **Your custom formats decide:** when Radarr/Sonarr have custom formats (TRaSH guides etc.), each duplicate copy is scored by them and the highest score is kept, ahead of resolution; the score is shown on every copy, and a pick against the higher resolution is marked Check, never Sure.
- **Keep both:** duplicate cards say Keep both (or Keep them all) instead of Ignore, for versions you want, like a 4K and a 1080p for phones.
- **Impact line:** Clean up all and Replace all now say what they'll do first: files to the trash, GB freed (hardlinked ones excluded), downloads started, and free space now and after the trash empties.
- **Weekly digest + Pushover:** Settings → Notifications can send messages after scans with news (as before), once a week on Sunday (library health, items to review, fixed this week, disk space, plus anything urgent at once), or both. Pushover added.
- **Trash days in Settings:** keep files in the trash for 3, 7, 14 or 30 days.
- **Download clients:** qBittorrent, Transmission, SABnzbd and NZBGet can be added in Settings. A stuck download now says why (nobody sharing it, the tracker no longer has it, every tracker failing, still fetching details, paused, or downloading slowly with time left; for usenet the client's own status or failure reason), with **Re-announce** for torrents before **Remove & try another**.
- **Checks on import:** every 10 minutes Library Doctor looks at what Radarr/Sonarr just imported; a file that's broken, far too short, the wrong audio language or a program pretending to be a video gets a message straight away and a scan, so the card is there within minutes instead of after the night's scan.
- **Release groups to avoid:** when one group keeps supplying files that had to be replaced (3+ files across 2+ titles in 180 days, and at least a quarter of that group's files in your library), Quality & Space offers **Block it in Radarr and Sonarr** (a "Library Doctor: groups to avoid" release profile).
- **Edition labels:** each copy on a duplicate card shows its edition (Director's Cut, Extended, IMAX…). Editions are normalised (Directors Cut = Director's Cut, Extended Cut = Extended Edition) and also read from plain file names after the year; Remastered isn't an edition.
- **Settings backup:** Settings → Backup downloads everything as a file (API keys included) and restores it, for moving to another server.
- **Unraid template** in `extras/unraid`.

**Fixed**
- Saving Settings also wrote two of Library Doctor's internal caches (the recycle-bin index and the root-folder list) into `config.json`, which could grow it to hundreds of KB. Only real settings are saved now, and the next save shrinks the file.
- qBittorrent 5's login answer (an empty reply instead of "Ok.") is accepted.

## 0.1.6 (beta)

- **App Health update cards:** an "update available" card now shows the installed and new version and:
  - **Update now** when the app can update itself (not in Docker): it runs the app's own update.
  - **How to update** for Docker installs: the exact `docker compose pull` / `up -d` commands with a Copy button, plus notes for pinned image tags, Unraid and Synology. Library Doctor never gets access to Docker itself.
  - **Skip this version:** hides the card until a newer version comes out.
  - Test versions are called out: an app on a develop/nightly branch says so, and how to switch to the stable branch.
- **Audio language (File Health):** flags a film or show made in your language whose only audio is another one (usually a dubbed copy, e.g. an English film with Russian audio only). Untagged audio and foreign-language originals are never flagged; TV is grouped per season. **Replace** moves the files to the trash, blocks that release in Radarr/Sonarr (when it's still in their history) and searches for another copy. Pick your language, or Off, under Settings → Audio language (default English).
- **Replacements are checked too:** when a file replaced for its audio language arrives, its audio is checked; another dubbed copy gives a warning ("its audio is Russian again — try Interactive Search") instead of a ✓. A season replaced at once only shows ✓ when every episode has arrived ("12 of 25 so far" until then).
- **Season gaps (Missing Files):** episodes Sonarr has on its wanted list but never got, one card per show with the seasons as ranges ("Season 2: episodes 6–10 · Season 3: 1, 7, 11, 12"). **Search for these** asks Sonarr to look again (whole seasons as season packs). Aired, monitored episodes only, no specials.
- **Indexer advice (App Health):** an indexer Radarr/Sonarr names as failing in 3+ hourly checks a week that hasn't found anything you downloaded in 90 days gets a card with **Turn it off in Sonarr/Radarr** (it stays set up there).
- **Disk space panel with a forecast:** free space now, and after a week of daily readings how fast the disk fills and roughly when it's full, plus what the open suggestions could free. The nightly summary warns when it's under 60 days. Several disks under the media folder are added up.
- **Same title, different film:** "The Fly (1958)" and "The Fly (1986)" were never grouped (different TMDB ids), but if Plex mismatched one so both sat in one item they'd have looked like two copies. Library Doctor now checks the {tmdb-…}/{imdb-…} tags or the (year) in the names first; different films get a "Plex has put different films together" card with **Split in Plex** instead of a clean-up.

**Fixed**
- A Replace whose film or show was then deleted from Radarr/Sonarr kept being checked every hour; it now says "Removed from Radarr, so there's no new copy to wait for".

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
