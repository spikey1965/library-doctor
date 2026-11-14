<p align="center"><img src="assets/logo.svg" width="96" height="96" alt="Library Doctor logo"></p>

# Library Doctor

**A nightly health check for your Plex library.** Library Doctor compares what Plex shows with what's really on the disk and what Radarr and Sonarr think they have. Every morning you get one page with everything that needs fixing, and a button to fix most of it.

> **Beta.** Built on the author's own home server and now being tested on others. Please [open an issue](../../issues) if something looks wrong.

[Try the live demo](https://mymediabuild.uk/library-doctor-plex-library-health-check/#try-the-demo) (made-up library, nothing real is changed) · [How it works](https://mymediabuild.uk/library-doctor-plex-library-health-check/)

## What it checks

| Check | What it finds | Needs |
|---|---|---|
| Missing files | Plex, Radarr or Sonarr list a file that isn't on the disk | Plex + Radarr/Sonarr |
| Duplicates | Two copies of the same film or episode, with which one to keep and why | Plex |
| Metadata gaps | "Episode 5" titles Sonarr knows the name of, wrong years, things not in Radarr/Sonarr | Plex + Radarr/Sonarr |
| File health | Broken files and files much shorter than they should be; Replace asks Radarr/Sonarr for a new copy | Plex + Radarr/Sonarr |
| Quality & space | Files far bigger than usual, old .ts/.avi files (Convert to MKV, no re-encoding), episodes much worse than their season, Find a smaller copy | Plex + Radarr/Sonarr |
| Subtitles | Subtitle files that are only adverts, names that no longer match the video, what Bazarr is still looking for | Bazarr |
| Unwatched | What nobody has played in a year, biggest first (never deletes anything) | Tautulli |
| Virus scan | Programs pretending to be films (film.mkv.exe), and ClamAV on every new file | ClamAV (optional) |
| Coming Soon | Agregarr placeholders playing a generic clip instead of the real trailer | Agregarr (optional) |

Tdarr (Convert with Tdarr), Sportarr (sports libraries) and Gotify (a phone message after the nightly scan) are optional too.

## Nothing is deleted without asking

- Library Doctor **only reads** from your apps until you press a fix button.
- Every clean-up **moves** files into its own trash (`.library-doctor-trash` inside your media folder, same disk, so it's instant). You can put anything back for 7 days, then the trash empties itself.
- Suggestions are marked **Sure** (a tiny broken copy, a leftover temp file) or **Check** (two good copies, have a look first).

## Install (Docker Compose)

1. Make a folder, save [`compose.yaml`](compose.yaml) in it and change the three lines marked `CHANGE`:
   - `PUID` and `PGID`: the user and group that own your media (run `ls -ln /path/to/media`; the two numbers after the permissions)
   - `TZ` your time zone
   - `/path/to/media` the folder that holds your films and shows
2. Start it:
   ```sh
   docker compose up -d
   ```
3. Open `http://<your-server>:8790`, create a password, then add Plex and at least one Radarr or Sonarr under **Settings**. Press **Test** on each one, then **Save** and **Scan now**.

After that it scans by itself every night at 3am.

### Mount your media as one folder

Library Doctor needs to see your films and shows inside **one** mount (for example `/mnt/media` with `movies/` and `tv/` inside it), mounted read-write. Clean-up moves files with a rename, which only works within one disk or dataset. If your films and shows are on different disks, mount their common parent folder.

### Folder mapping

Your apps may see the same files at different paths (Plex might say `/data/movies`, Radarr `/movies`, Library Doctor `/media/movies`). On each app's card in Settings, press **Detect folders**: Library Doctor looks at a few real files from that app, finds them under its own `/media`, and fills in the mapping. You can also add one by hand.

## Behind a reverse proxy or SSO

Library Doctor has its own login. If Authentik, Authelia or your proxy already protects it, you can switch the built-in login off with `LD_AUTH=off`. Only do that when something else is in front: Library Doctor can move files.

## Settings (environment variables)

| Variable | Default | What it does |
|---|---|---|
| `TZ` | UTC | Time zone for the nightly scan |
| `LD_SCAN_HOUR` | `3` | Hour of the nightly scan |
| `LD_TRASH_DAYS` | `7` | Days before the trash empties itself |
| `LD_KEEP_DAYS` | `182` | How long "Keep" hides an unwatched item |
| `LD_AUTH` | `on` | `off` = no built-in login (only behind another login) |
| `LD_PORT` | `8790` | Port inside the container |
| `LD_CLAMD_HOST` / `LD_CLAMD_PORT` | `127.0.0.1` / `3310` | Your ClamAV (clamd), for the Virus Scan check |
| `LD_CLAM_ROOT` | `/scandir` | Where ClamAV sees the same media folder |

Everything else (apps, API keys, folder mapping) is set on the Settings page and saved in `/config/config.json`. API keys stay on your server and are never shown again after saving.

## Updating

```sh
docker compose pull && docker compose up -d
```

## Thank you

Library Doctor talks to Plex, Radarr, Sonarr, Bazarr, Tautulli, Tdarr, Sportarr, Agregarr, ClamAV and Gotify, and uses ffmpeg and yt-dlp. It isn't connected to any of those projects.

Made by [My Media Build](https://mymediabuild.uk). MIT licence.
