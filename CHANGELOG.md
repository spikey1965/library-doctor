# Changelog

## 0.3.2 (beta)

- **Quick test (new).** A light version of the deep check: ffprobe opens the file, then ffmpeg plays 6 seconds at the start, at 25, 50 and 75 % and near the end, seeking straight to each one. A few seconds a file (a 44-minute 1080p episode took 3 s, a 2-hour 4K HEVC film 14 s). It finds downloads that were cut off ("it stops playing somewhere between 1 h 5 min and 1 h 38 min"), files that won't open or won't start, and parts with lots of errors. It can't see damage between the samples, so the card offers Deep check to be sure. Damaged files get the same File Health card with Replace.
  - **Quick test** button on every File Health card (next to Deep check), and **Test a folder…** now offers Quick test or Deep check for a whole folder.
  - **Settings → Quick test & deep check → Quick-test new downloads when they're imported** (on by default; if the deep check for new downloads is on, that runs instead).
  - **Quick-test the whole library in the night window** (off by default): each file once, newest first, then only new and changed files, so after the first night or two it takes seconds. It uses the same window as the nightly deep check and goes first. Files a deep check already passed are skipped.
  - A deep check result for the same file always wins over a quick test.
- **Deep check fix:** 4K Blu-ray files with PGS subtitles print ffmpeg notes like "not enough frames to estimate rate; consider increasing probesize" when they open. They aren't damage, but the deep check counted them, so a good 4K file could get a "Some errors" card. They're ignored now.

## 0.3.1 (beta)

- **Plex, Jellyfin or Emby on Windows** ([#2](https://github.com/spikey1965/library-doctor/issues/2)). Folder mappings now work for Windows paths: `Q:\Plex` → `/media/Q` matches `Q:\Plex\Movies\…` whether you type the mapping with backslashes or forward slashes, and the drive letter and folder names ignore upper/lower case (as Windows does). Paths sent back to a Windows server are turned back into Windows paths. **Detect** in Settings → Folder check also finds the mapping for Windows paths now (it only understood `/` before, so it found 0 files).
- **Clean-up works with separate mounts** ([#1](https://github.com/spikey1965/library-doctor/issues/1)). The trash used to be one folder, `/media/.library-doctor-trash`, so with `/mnt/movies:/media/movies` and `/mnt/tv:/media/tv` (or several network shares) every clean-up failed with "Nothing was moved". Now each mount gets its own `.library-doctor-trash` at its top, worked out from the container's mount table, so files are still only ever renamed (never copied, so nothing can be half-moved). Put back, Delete, Empty Trash and the automatic clear-out after N days work across all of them. Each trash folder gets a `.plexignore` (`*`) and an `.ignore` so Plex, Jellyfin and Emby skip it. Setups with everything in one mount keep using `/media/.library-doctor-trash` exactly as before. The "Your media is on more than one disk" warning is now just a note.

## 0.3.0 (beta)

- **Lidarr (music), step 1.** Settings → Radarr, Sonarr, Lidarr & Sportarr → + Lidarr (address, API key, folder mapping; the Folder check includes it).
- **Missing Files: albums Lidarr wants but never got**, one card per artist, saying which albums are missing and which are incomplete ("8 of 10 tracks"), with **Search** (refused with a clear message when Lidarr has no download client or indexer).
- **Missing Files: tracks Lidarr lists that aren't on the disk**, with **Rescan** (Lidarr looks at the artist's folder again and marks them as wanted).
- **App Health and Downloads for Lidarr:** its own warnings and update notices, and downloads it won't import by itself or that failed (open its queue to deal with them).
- **Music step 2 (works with any media server, from Lidarr and the disk):**
  - **Broken or cut-short tracks** in File Health. Only tracks that look too small for their real length (from MusicBrainz) are played through to measure them, because a cut-off MP3's header often still claims the full length. Replace moves the track to the trash and asks Lidarr to search for the album again.
  - **Mixed quality** in Quality & Space: an album with a few lossy tracks among lossless ones, or a few low-bitrate MP3s among good ones, with Search for a better copy.
  - **The same album in two folders** in Duplicates (year and format tags like [FLAC] or (320) are ignored, editions like Deluxe stay apart). Clean up moves the extra folder to the trash, but only when Lidarr doesn't use any of its files; when it uses files from both, the card says to sort it out in Lidarr first.
- **Music libraries in your media server:** tracks it lists that aren't on the disk, albums Plex couldn't match, albums without cover art (Refresh). If most of a music library can't be found, one card says so instead of listing every track (usually a missing folder mapping).
- **Jellyfin and Emby music libraries too:** the same three checks (missing tracks, unmatched albums with Refresh, albums without cover art) on all three media servers.
- The Folder check now names your media server (it said "Plex" for Jellyfin and Emby too).
- **Downloads: "download not needed".** A finished download that Radarr/Sonarr won't import because you already have a copy that's as good or better (for example a second release grabbed as an upgrade, then a better one imported first) used to show as "waiting to import" with an Import button. Now it gets its own card that says what you have against what was downloaded (with custom format scores) and offers **Delete download**: the files go to the trash (Put back works), it leaves the download client and the queue, and it is **not** blocklisted, so no new search starts. Private or protected torrents are only taken out of the queue and keep seeding. Library Doctor checks again that your copy is still there before it deletes anything.

- **Deep check (new).** The nightly scan only sees a file's size and length; a deep check plays the whole file through with ffmpeg (nothing is written or changed) and finds damage in the middle. Files that stop early, won't play at all, or throw lots of decode errors get a **File Health card ("Damaged") with Replace**, which works like any other Replace (trash first, then Radarr/Sonarr search, and it counts towards "release groups to avoid"). A few errors give a softer "Some errors" card to check by eye. Three ways to use it:
  - **Deep check** button on every File Health card, and **Check a folder…** at the top of File Health (pick any folder inside your media folder).
  - **Settings → Deep check → Check new downloads when they're imported** (off by default).
  - **Settings → Deep check → Check the whole library, a bit each night**, inside a time window you pick (off by default). It carries on where it stopped the next night, newest files first, and shows how many are done and roughly how many nights are left. Files are checked again after 1, 3, 6 or 12 months (or never); changed files always are.
  - One file at a time, at the lowest priority, half the CPU cores at most (and never more than the container's `cpus:` limit). **Waits while someone is watching** on Plex, Jellyfin or Emby. A file that needs more memory than the container has free is skipped with a note instead of failing, and a check the system stops is never counted as damage.
- **Tdarr health-check errors in File Health.** If Tdarr is set up and its own health check (Library settings → Health check) finds an error, the file gets the same Damaged card with Replace. When Library Doctor's own deep check later finds the file fine, Tdarr's older error is dropped.

## 0.2.3 (beta)

- **Jackett support.** Settings → Indexers → Jackett: address, API key and (only if you set one in Jackett) its admin password. Test says how many indexers are set up.
- **App Health: Jackett indexers that failed on their last search.** Cloudflare blocks are grouped into one card that says whether Jackett has FlareSolverr set up and what to do; other errors (site down, timeouts) are listed in a second card with each indexer's own error. This needs the admin password if Jackett has one; with only the API key, Library Doctor can see which indexers are set up but not which ones fail.
- **App Health: Radarr/Sonarr indexers that point at a Jackett indexer you removed.** They fail on every search; the card has a Turn off button (the indexer stays set up in Radarr/Sonarr). Works with just the API key.
- **Seerr support (also Overseerr and Jellyseerr: same API).** Settings → Optional apps → Seerr (address + API key); Test says how many reports are open.
- **Problem reports from Seerr in File Health.** When someone reports a problem in Seerr (won't play, wrong audio, subtitles), it shows on Library Doctor's own card for that file ("Also reported in Seerr by…") when Library Doctor found the problem too, or on a card of its own when it didn't (Replace, or Mark resolved in Seerr if the file is fine). Replace comments on the report and resolves it in Seerr.
- **"Requested by" on Duplicates and Unwatched**, from the newest Seerr request, so you don't clean up something someone just asked for.
- **Replacing a requested film marks it "processing" in Seerr** until the new copy arrives (Seerr marks it available again by itself).
- The Replace confirmation no longer says the new copy "downloads in the night window" (that was the author's own download schedule).

## 0.2.2 (beta)

- **Works without Radarr or Sonarr (report-only mode).** Only a media server (Plex, Jellyfin or Emby) is needed to scan now. Without Radarr/Sonarr, Library Doctor still finds missing, duplicate, broken and virus files, metadata problems and space hogs, and a note on the main screen says what isn't checked (audio language, cut-short files, subtitles, missing episodes, stuck downloads) and that Replace/Search need Radarr or Sonarr. Broken-file cards say to get a new copy yourself, and the Downloads and Subtitles tiles say "Needs Radarr or Sonarr" instead of "All clear", and File Health counts "to look at" instead of "to replace".
- Folder check says "Your app lines up" when there's only one app (was "All 1 apps line up").

## 0.2.1 (beta)

- **Synology tested** (DSM 7.2.2, Container Manager): the README has step-by-step Synology instructions, including the two things that stop most installs (create the `config` folder first, and give your own user Read/Write on the shares).
- **Clearer message when Library Doctor can't write its settings folder:** if the folder already belongs to the right user, or a NAS permission list (ACL) is in the way, it now says exactly what to change on Synology or TrueNAS, instead of telling you to change PUID/PGID.
- **Put back restores a quarantined program file's permissions**, and on folders with a NAS permission list (Synology, TrueNAS) the Virus Scan no longer changes a file's permissions at all (that would wipe the permission list).
- **Settings has two more named sections:** **Indexers** (Prowlarr, with Jackett shown as coming soon) and **Download clients** (SABnzbd, NZBGet, qBittorrent, Transmission). **Optional apps** now holds Bazarr, Tautulli and Tdarr.

## 0.2.0 (beta)

- **Settings has clearer sections:** **Media server** (Plex, Jellyfin and Emby side by side: pick yours; each keeps its own address while you switch), **Radarr, Sonarr & Sportarr** (with Lidarr shown as coming soon: music isn't checked yet), then **Optional apps**.
- **Jellyfin and Emby support.** Settings → Media server: pick Plex, Jellyfin or Emby, paste the server's API key, press Test. Every check runs on all three. On a test library scanned side by side, Plex, Jellyfin and Emby gave the same findings (duplicates across folders, broken, too short and wrong-language files, app warnings), and a duplicate clean-up through Jellyfin was confirmed by Jellyfin afterwards. Open in Jellyfin/Emby buttons go to the item's page.
- Fixes that only exist in Plex are hidden on Jellyfin/Emby: Split, Pick posters, Use Sonarr's titles, Fix Plex's listing, Plex tidy-up and the backup reminder. Season-poster and episode-title cards offer Refresh instead.
- Messages name your server ("Jellyfin checked: 1 version left", "Open in Emby"); "Fix Match" is "Identify" there.
- Jellyfin and Emby take a minute or two to notice moved files, so the check after a clean-up waits up to about 4 minutes on them (1 minute on Plex).
- Under the hood: every request to the media server now goes through one place. Plex behaves exactly as before (checked request by request against 0.1.15).

## 0.1.15 (beta)

- **Fixed:** if Library Doctor restarted while a clean-up or replace was still being checked, that item stayed "Checking…" for ever and its card was hidden (the tile still counted it). Unfinished checks are now marked "Interrupted" at start-up and the card comes back.
- **Warning when you keep a copy Radarr/Sonarr can't see.** If you press "Keep this one instead" on a copy in a different folder from the file Radarr/Sonarr track, the card now warns that they'll think it's missing and may download it again, and the clean-up message says so too.
- **Restore now tells Radarr/Sonarr the file is back.** Before, a restored file stayed "missing" in Radarr/Sonarr, so they kept searching for (and could download) a replacement for a file you chose to keep, and checks that read their file info (like the audio language) skipped it. A Replace that was still waiting for a new copy now says "You put the file back" instead of waiting forever.
- The last two "overnight" / "tonight" messages now only mention a download window when `LD_DOWNLOAD_WINDOW_END` is set.

## 0.1.14 (beta)

- **Replace checks Radarr/Sonarr can download first.** If the app has no download client, or no indexer with searches on, Library Doctor now says so and moves nothing, instead of putting the file in the trash and waiting for a copy that can never come. Search for missing episodes and Find a smaller copy check the same.
- **No more "night window" unless you have one:** messages only mention a download window when `LD_DOWNLOAD_WINDOW_END` is set; otherwise they say a copy arrives as soon as your download client gets it.
- **Open in Plex keeps working after Plex renumbers an item.** Plex gives a film or episode a new ID when it merges two entries or adds a file again (after a restore, a clean-up or a replace), which broke the button until the next scan. It now finds the item by its file and opens the right one.
- **"Plex couldn't match this" cards follow the item too:** Refresh no longer fails with "HTTP Error 404" when Plex has merged the item in the meantime, and if Plex has already matched it, the card closes as fixed.
- **Show button on password boxes:** when you create or change the Library Doctor password (and on the login page, Backup passphrase and app keys), press **Show** to see what you typed. On the create/change screens it shows both boxes at once, so you can check they match.

## 0.1.13 (beta)

- **App logos in Settings:** each app row, card, "+ Add" button and the Folder check table now shows the app's logo next to its name.
- **Settings tests your apps when it opens,** so every status dot means something straight away. Notifications are never tested automatically, so nobody gets test messages.
- **Unsaved changes show on the Settings tab:** an orange dot while there's something not saved yet, so you can see it from the other tabs.
- A row whose test passed now shows the app's own reply (for example "Connected to Tautulli v2.19.1").
- **Fixed:** an app that connects but whose folders don't line up stayed green after Test all. It now stays orange ("Connected, folders don't line up").

## 0.1.12 (beta)

- **A new Settings page that uses the whole screen.** A menu on the left (a row of tabs on a phone) jumps to each part: Folder check, Plex & arrs, Optional apps, Notifications, General, Backup and Login. The menu shows how many apps each part has, and an orange count when something needs a look.
- **Apps that work fold down to one line** with a coloured dot (green = connected, orange = folders don't line up, red = test failed, grey = not tested yet), the address, and Test / Edit buttons. A new app, one that isn't set up yet or one whose test failed opens by itself; **Done** folds it back. App cards sit two to a row on wide screens.
- **Test all apps** button tests Plex, every Radarr/Sonarr and the optional apps in one go (not the notifications, so nobody gets test messages).
- **General** is one card: automatic scan time and days, when to send messages, how long the trash keeps files, and your audio language.
- **Folder check** shows one line when every app lines up (details behind "Show details") and the full table only when something is wrong.
- The Save bar says how many unsaved changes there are, and the browser asks before you close the tab with changes not saved.

## 0.1.11 (beta)

- **A cleaner main screen**: the check tiles are now rows, with the icon on the left and the count coloured by how serious it is, and tiles that are all clear are dimmed so the ones with work stand out. The Scans list is a compact timeline (date, items checked, how many to look at). Panels have clearer edges, the tabs are pills with hover and keyboard-focus outlines, and the score is shown once (in the ring). Movement is turned off if your device asks for reduced motion.
- **Fixed:** the count on the Trash tab was grey and squashed; "1 warnings" on App Health now says "1 warning".
- **New logo:** a shelf of books with a pulse running through it, the orange one being the book that needs fixing. In the app, the login page, the browser tab and the Unraid icon.
- README: new screenshots (light and dark) and the tagline "A nightly health check for Plex, Radarr and Sonarr".
- **Removed: Coming Soon check.** The Agregarr trailer picker (which downloaded trailers from YouTube with yt-dlp), the yt-dlp updater and the rest of the Coming Soon check are gone, along with the `/agregarr-bin` mount and the `deno` package in the image. Agregarr's placeholder files are still recognised and never reported as problems.
- **Removed: Sportarr support.** The Sportarr card in Settings and its checks are gone. An old Sportarr entry in your settings is ignored.

## 0.1.10 (beta)

- **The library score in your messages:** the nightly summary (when it has news) and the weekly digest now say the score, how it changed since yesterday or this week, the lowest areas and how many problems keep coming back.
- **Dismiss** button on the "refused a request" warning. It hides the warning until something else is refused; the request stays in the log and the History list.
- **Unraid template:** the 0.1.9 settings (Trusted proxies, Allowed host names, Incomplete folder days) are now in the template's advanced view, and the description mentions the Analysis tab.

## 0.1.9 (beta)

**New**
- **Incomplete folder clean-up (Downloads):** when a download fails or is removed, SABnzbd, qBittorrent, Transmission or NZBGet can leave its half-finished files in the "incomplete" folder, and nothing ever empties it. Library Doctor asks each download client where that folder is, lists what's in it that no client is working on and that nothing has touched for 3 days (`LD_INCOMPLETE_DAYS`), with the size really used on the disk, and offers **Move all to trash** (asks once more first) or **Trash** per item. Everything is checked again just before it's moved, and if any download client can't be reached nothing is offered, because Library Doctor can't tell what's still downloading. qBittorrent's per-category folders are looked inside. `LD_INCOMPLETE_DIRS` adds folders by hand.
- **"Don't have these yet?" in Settings:** under the optional apps, one line each on what Bazarr, Tautulli, Tdarr, Sportarr, Prowlarr, qBittorrent, Transmission, SABnzbd, NZBGet and Gotify add, with a Docker Compose snippet filled in with your user, group, time zone and the media folder Radarr/Sonarr use (Copy button), where to find it on Unraid and TrueNAS, and a step-by-step guide. Library Doctor still never installs anything itself.
- **Analysis tab:** a new page with:
  - **Library overview:** titles, files, total size, movies, episodes, free space.
  - **Library score** out of 100, split into File integrity, Quality, App sync, Metadata and Duplicates, each with what weighs on it most. Small problems are weighted up so they show (1% broken files costs about 25 points). The main ring now shows this score.
  - **Trends:** score over time (a point after every scan, at least daily) with the change over 7 and 30 days, problems found per scan, and storage recovered (freed for good, waiting in the trash, last 30 days).
  - **Problems that keep coming back:** back after a fix, seen again and again between scans, or the same title fixed more than once (ignored cards and running totals left out).
  - **Storage by library** and **Quality by library** (share in HD, resolution and video codec mix), plus **Download CSV**.
- **Best fix / Risk / Space saved** on every card: the recommended fix, how risky it is (No risk / Low risk / Check first, with the reason) and the space it frees (hardlinked copies excluded; Tdarr conversions as an estimate).

**Security**
- **Reverse proxies:** `X-Forwarded-For` and `X-Forwarded-Proto` are now only believed from addresses in the new `LD_TRUSTED_PROXIES`. Before, anyone could send a made-up `X-Forwarded-For` to get round the per-address login lock-out (the overall limit still applied).
- **Login switched off (`LD_AUTH=off`):** Library Doctor now only answers your proxy (`LD_TRUSTED_PROXIES`) or, if that isn't set, local network addresses, so a port opened to the internet by mistake isn't an open door. New `LD_ALLOWED_HOSTS` refuses requests for any other host name (stops DNS rebinding). Refused requests show a red warning on the main screen, and Settings → Login shows what's set.
- **Settings backup:** now either without API keys (restoring keeps the keys already saved) or with the keys encrypted by a passphrase you choose (AES-256, `openssl enc` format, so it can also be opened by hand). Older backup files still restore. The image now includes `openssl`.
- **Logs never show keys:** API keys, tokens, passwords, Discord webhook and Telegram bot URLs are masked in everything Library Doctor logs.

## 0.1.8 (beta)

**New**
- **A closer look before anything leaves the trash for good.** Before deleting a file, Library Doctor checks again and keeps it back (with the reason shown in the Trash screen, the warning banner and the nightly summary) when: the copy a duplicate clean-up kept is no longer on the disk (an upgraded copy in the same folder counts as fine), a replaced file's new copy hasn't arrived or has gone, an old .ts file's new .mkv is missing, or a Plex listing fix didn't finish (so the file in the trash is the only copy). Empty Trash leaves held files too; delete them one by one if you're sure.

**Security**
- The page can no longer be shown inside another website (`frame-ancestors 'none'`, `X-Frame-Options: DENY`), which blocks clickjacking, mainly relevant when the built-in login is switched off behind a proxy. Also `Referrer-Policy: same-origin`, `base-uri` and `form-action` limited to the app itself.
- **Review all fixes → Download CSV:** a file or title starting with `=`, `+`, `-` or `@` is written with a leading apostrophe, so a spreadsheet can't run it as a formula.
- README: create the password straight away after installing, before the port is reachable from elsewhere.
- README: Library Doctor is now in Unraid's Community Applications (search Library Doctor in Apps).

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
