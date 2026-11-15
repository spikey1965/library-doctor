#!/usr/bin/env python3
"""Library Doctor: checks a Plex library against the disk and Radarr/Sonarr.

Checks:
  * missing files   - Plex lists a file that isn't on disk; Radarr thinks it has a file that isn't on disk
  * duplicates      - one Plex item with several versions, or the same film/episode as separate items in one library
  * metadata gaps   - (stage 2) "Episode N" titles Sonarr has real names for, Plex vs Radarr/Sonarr wrong match,
                      not in Radarr/Sonarr, year differs from Radarr, no poster, unmatched. Fixes only touch Plex's info.
  * file health     - (stage 3) broken files (no length / under 5 MB) and files far shorter than the runtime Radarr/Sonarr
                      expect. "Replace" moves the file (+ its .edl/.ffmeta) to the trash and asks the arr to search again.
  * subtitles       - (stage 3) advert-only subtitle files (Bazarr thinks they're real), subtitles whose name no longer
                      matches the video (Plex can't link them), stray subtitles with no video, + a Bazarr "missing" summary.
  * coming soon     - (v17) Agregarr Coming Soon placeholders: generic clip instead of the real trailer ("Pick trailer"
                      downloads the YouTube trailer you choose over it), wrong library, stale Plex entries, leftovers after
                      the real film arrives, and the age of the yt-dlp Agregarr uses ("Update yt-dlp").
  * quality & space - (v18) space hogs (GB an hour far above the usual for that library + resolution), .ts/.avi etc.
                      ("Convert to MKV" = lossless ffmpeg remux, old file to the trash), episodes far below their season.
  * unwatched       - (v18) Tautulli: not played in a year (or since history began), biggest first; never deletes.
  * Sportarr        - (v18) duplicates prefer the file Sportarr tracks; Sportarr files gone from disk; sports files it doesn't know.
  * virus scan      - (stage 3) programs/scripts/archives in the libraries, "videos" that are really programs or archives,
                      and ClamAV (clamd on 127.0.0.1:3310, /media -> /scandir) on every file changed since the last scan.
                      "Full scan now" runs clamd MULTISCAN over the whole library in the background.
Clean-up (v2, 2026-11-13):
  * every duplicate gets a recommendation: which copy to keep and why
  * "Clean up" MOVES the other copies into TRASH_DIR (same dataset, so instant); nothing is ever deleted straight away
  * Restore puts a file back; the trash empties itself after TRASH_DAYS, or when you press "Empty trash"
  * after a move it asks Plex to rescan that folder and Radarr/Sonarr to rescan the film/series
Safety: the page only sends an issue id. The server takes the file list from its own last scan, checks every path
is inside MEDIA_ROOT, never moves the kept file, and never leaves an item with no copy at all.
Standard library only.
"""
import errno, hashlib, hmac, json, os, re, secrets, shutil, sqlite3, subprocess, threading, time, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
import http.cookiejar as _cookiejar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie        # imported by name: this file has its own http() function

APP_VERSION = "0.1.4"
CONFIG_DIR = os.environ.get("LD_CONFIG", "/config")
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(CONFIG_DIR, "ldoctor.db")
LISTEN = os.environ.get("LD_LISTEN", "0.0.0.0")
PORT = int(os.environ.get("LD_PORT", "8790"))
SCAN_HOUR = int(os.environ.get("LD_SCAN_HOUR", "3"))   # default; the Settings page can set time + days (v0.1.3)
MEDIA_ROOT = os.path.realpath(os.environ.get("LD_MEDIA_ROOT", "/media"))
TRASH_DIR = os.path.join(MEDIA_ROOT, ".library-doctor-trash")
TRASH_DAYS = int(os.environ.get("LD_TRASH_DAYS", "7"))
PAGE = 1000
TINY = 50 * 1024 * 1024
GOOD_CONTAINERS = (".mkv", ".mp4", ".m4v")                    # preferred when quality and length match
VIDEO_EXT = (".mkv", ".mp4", ".m4v", ".ts", ".m2ts", ".avi", ".mov", ".wmv", ".mpg", ".mpeg")
SIDE_EXT = (".edl", ".ffmeta", ".txt")                        # leftover side files we may tidy (never .srt/.nfo/art)

CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")        # written by the Settings page
LEGACY_FILE = os.path.join(CONFIG_DIR, "secrets.json")       # hand-written config from before the Settings page

def load_secrets():
    for f in (CONFIG_FILE, LEGACY_FILE):
        if os.path.isfile(f):
            with open(f) as fh: return json.load(fh)
    return {}                                                 # first run: setup mode until Plex + one arr are saved

def normalize(cfg):
    sp = cfg.get("sportarr")
    if isinstance(sp, dict) and sp.get("container_root") and not sp.get("paths"):
        sp["paths"] = [{"from": sp.pop("container_root"), "to": sp.pop("media_root", None) or MEDIA_ROOT}]
    # optional fields the rest of the code relies on: the browser address falls back to the app's own address
    if isinstance(cfg.get("plex"), dict): cfg["plex"]["public"] = cfg["plex"].get("public") or "https://app.plex.tv"
    for k, name in (("tautulli", "Tautulli"), ("tdarr", "Tdarr"), ("sportarr", "Sportarr"), ("gotify", "Gotify")):
        e = cfg.get(k)
        if isinstance(e, dict): e["public"] = e.get("public") or e.get("url", ""); e.setdefault("name", name); e.setdefault("key", e.get("key", ""))
    for k in ("arrs", "bazarr"):
        for e in cfg.get(k) or []:
            if isinstance(e, dict): e["public"] = e.get("public") or e.get("url", "")
    return cfg

S = normalize(load_secrets())   # {"plex": {"url","token","public","paths"}, "arrs": [{"name","kind","url","key","public","paths"}], ...}

def sched():
    """v0.1.3: (hour, minute, days) of the automatic scan. Days are 0=Mon..6=Sun. From Settings, else LD_SCAN_HOUR every day."""
    sc = S.get("schedule") or {}
    try: h = int(sc.get("hour", SCAN_HOUR))
    except (TypeError, ValueError): h = SCAN_HOUR
    if not 0 <= h <= 23: h = -1                               # e.g. LD_SCAN_HOUR=99: no automatic scan
    try: mi = min(59, max(0, int(sc.get("minute", 0))))
    except (TypeError, ValueError): mi = 0
    raw = sc.get("days")
    days = sorted({int(d) for d in raw if str(d).isdigit() and 0 <= int(d) <= 6}) if isinstance(raw, list) else []
    return h, mi, (days or list(range(7)))

def configured():
    """Enough set up to scan: Plex plus at least one Radarr/Sonarr."""
    return bool((S.get("plex") or {}).get("url") and (S.get("plex") or {}).get("token") and any(a.get("url") and a.get("key") for a in S.get("arrs") or []))

# ---------------- folder mapping ----------------
# Each app may see the media under a different folder than Library Doctor does (Plex: /data/movies, Radarr: /movies,
# Library Doctor: /media/movies). Every app config can carry "paths": [{"from": "<app's folder>", "to": "<Library Doctor's folder>"}].
# Replies are translated as they arrive (map_in) and paths we send back are translated the other way (map_out),
# so the rest of the code only ever sees Library Doctor's own paths. No "paths" = no change.
PATH_KEYS_IN = {"file", "path", "folderPath", "rootFolderPath", "filePath", "folder", "recycleBin", "outputPath"}
PATH_KEYS_OUT = {"path", "file", "folder", "arrayOrPath", "docID", "folderPath"}

def _pairs(cfg, out=False):
    ps = [((m.get("from") or "").rstrip("/") or "/", (m.get("to") or "").rstrip("/") or "/") for m in (cfg or {}).get("paths") or [] if m.get("from") and m.get("to")]
    ps = [(b, a) for a, b in ps] if out else ps
    return sorted(ps, key=lambda x: -len(x[0]))               # longest prefix first

def _swap(v, pairs):
    for a, b in pairs:
        if v == a: return b
        if v.startswith(a + "/") or a == "/": return (b.rstrip("/") + "/" + v[len(a):].lstrip("/")) if a == "/" else b + v[len(a):]
    return v

def to_local(v, cfg): return _swap(v, _pairs(cfg)) if isinstance(v, str) else v
def to_app(v, cfg): return _swap(v, _pairs(cfg, out=True)) if isinstance(v, str) else v

def _walk(o, pairs, keys):
    if isinstance(o, dict):
        return {k: (_swap(v, pairs) if k in keys and isinstance(v, str) else [_swap(x, pairs) if isinstance(x, str) else _walk(x, pairs, keys) for x in v] if k in keys and isinstance(v, list) else _walk(v, pairs, keys)) for k, v in o.items()}
    if isinstance(o, list): return [_walk(x, pairs, keys) for x in o]
    return o

def map_in(obj, cfg, extra=()):
    pairs = _pairs(cfg)
    return _walk(obj, pairs, PATH_KEYS_IN | set(extra)) if pairs else obj

def map_out(obj, cfg):
    pairs = _pairs(cfg, out=True)
    return _walk(obj, pairs, PATH_KEYS_OUT) if pairs else obj

def move_file(src, dst):
    """os.rename with a plain-English error when src and dst are on different disks/datasets (clean-up only ever renames)."""
    try:
        os.rename(src, dst)
    except OSError as e:
        if e.errno == errno.EXDEV:
            raise OSError(e.errno, "%s is on a different disk or dataset from %s, so it can't be moved instantly. Library Doctor needs your media in one mount (see Known limitations in the README). Nothing was moved."
                          % (src, os.path.dirname(dst) or dst)) from None
        raise

_SPLIT = {"t": 0, "v": []}
def split_mounts():
    """Folders under MEDIA_ROOT (two levels deep) that sit on another disk or dataset: clean-up can't move files from them."""
    if time.time() - _SPLIT["t"] < 600: return _SPLIT["v"]
    out = []
    try:
        d0 = os.stat(MEDIA_ROOT).st_dev
        for a in os.scandir(MEDIA_ROOT):
            if not a.is_dir(follow_symlinks=False) or a.name.startswith("."): continue
            if os.stat(a.path).st_dev != d0: out.append(a.path); continue
            try:
                out += [b.path for b in os.scandir(a.path) if b.is_dir(follow_symlinks=False) and not b.name.startswith(".") and os.stat(b.path).st_dev != d0]
            except OSError: pass
    except OSError: pass
    _SPLIT.update(t=time.time(), v=sorted(out)[:20]); return _SPLIT["v"]

def log(msg):
    print(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)

def now_iso():
    return datetime.now().isoformat(timespec="seconds")

def http(url, headers=None, data=None, method=None, timeout=60):
    h = dict({"Accept": "application/json"}, **(headers or {}))
    body = None
    if data is not None:
        body = json.dumps(data).encode(); h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, headers=h, data=body, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8")
        return json.loads(raw) if raw.strip() else {}

# ---------------- database ----------------
def db():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS scans (id INTEGER PRIMARY KEY, started TEXT, finished TEXT, ok INTEGER,
            items INTEGER, files INTEGER, issues INTEGER, note TEXT);
        CREATE TABLE IF NOT EXISTS issues (id TEXT PRIMARY KEY, scan_id INTEGER, chk TEXT, sev TEXT, title TEXT,
            library TEXT, detail TEXT, links TEXT, first_seen TEXT, last_seen TEXT);
        CREATE TABLE IF NOT EXISTS ignored (id TEXT PRIMARY KEY, title TEXT, at TEXT);
        CREATE TABLE IF NOT EXISTS trash (id INTEGER PRIMARY KEY, issue_id TEXT, title TEXT, src TEXT, dst TEXT, size INTEGER,
            reason TEXT, moved_at TEXT, restored_at TEXT, purged_at TEXT);
        CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, at TEXT, kind TEXT, text TEXT);
        CREATE TABLE IF NOT EXISTS done (id INTEGER PRIMARY KEY, issue_id TEXT, title TEXT, library TEXT, kept TEXT, moved TEXT,
            trash_ids TEXT, links TEXT, at TEXT, status TEXT, note TEXT, checked_at TEXT, cleared INTEGER DEFAULT 0);
        """)
        c.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS sessions (h TEXT PRIMARY KEY, created TEXT, expires TEXT, last_seen TEXT, ip TEXT)")
        # v31: "Searched, nothing better yet" - hidden until `until`, then the nightly run searches again if it's still there
        c.execute("CREATE TABLE IF NOT EXISTS snoozed (id TEXT PRIMARY KEY, title TEXT, until TEXT, searched_at TEXT, searches INTEGER DEFAULT 1)")
        cols = [r[1] for r in c.execute("PRAGMA table_info(done)")]
        if "chk" not in cols:
            c.execute("ALTER TABLE done ADD COLUMN chk TEXT DEFAULT 'dupes'")
        if "ref" not in cols:
            c.execute("ALTER TABLE done ADD COLUMN ref TEXT")
        if "until" not in [r[1] for r in c.execute("PRAGMA table_info(ignored)")]:
            c.execute("ALTER TABLE ignored ADD COLUMN until TEXT")      # Unwatched "Keep": back on the list after this date; NULL = for good

SNOOZE_DAYS = int(os.environ.get("LD_SNOOZE_DAYS", "30"))

def hidden_ids(c):
    """Ignored issues plus ones snoozed after a search that found nothing better yet (still inside their 30 days)."""
    now = now_iso()
    return {r["id"] for r in c.execute("SELECT id FROM ignored")} | {r["id"] for r in c.execute("SELECT id FROM snoozed WHERE until > ?", (now,))}

def snooze(c, issue_id, title, days=None, count=True):
    old = c.execute("SELECT searches FROM snoozed WHERE id=?", (issue_id,)).fetchone()
    until = (datetime.now() + timedelta(days=days or SNOOZE_DAYS)).isoformat(timespec="seconds")
    n = ((old["searches"] + 1) if count else old["searches"]) if old else 1
    c.execute("INSERT OR REPLACE INTO snoozed (id, title, until, searched_at, searches) VALUES (?,?,?,?,?)",
              (issue_id, title, until, now_iso(), n))

def snooze_due():
    """Nightly, after the scan: snoozes that ran out. Still found by the scan -> search again (and snooze again); gone -> forget it."""
    now = now_iso()
    with db() as c:
        due = [dict(r) for r in c.execute("SELECT * FROM snoozed WHERE until <= ?", (now,))]
        live = {r["id"] for r in c.execute("SELECT id FROM issues")}
        ign = {r["id"] for r in c.execute("SELECT id FROM ignored")}
    for s_ in due:
        if s_["id"] not in live or s_["id"] in ign:              # fixed, or you ignored it: no more searching
            with db() as c: c.execute("DELETE FROM snoozed WHERE id=?", (s_["id"],))
            continue
        r = quality_fix(s_["id"], "search")
        if not r.get("ok"): log("snooze re-search %s failed: %s" % (s_["title"], r.get("error")))

def event(kind, text):
    with db() as c:
        c.execute("INSERT INTO events (at, kind, text) VALUES (?,?,?)", (now_iso(), kind, text))
    _STATS["v"] = None                     # something changed: the Space saved / Jobs left rings recount
    log("%s: %s" % (kind, text))

# ---------------- Plex ----------------
def plex(path, **params):
    p = S["plex"]
    q = urllib.parse.urlencode(params)
    return map_in(http(p["url"].rstrip("/") + path + ("?" + q if q else ""), {"X-Plex-Token": p["token"]})["MediaContainer"], p)

PLACEHOLDER_ROOT = os.path.join(MEDIA_ROOT, "placeholder")

def is_placeholder(f):
    """Agregarr 'Coming Soon' trailer placeholders (30 s, ~6 MB): not real media, never report them."""
    return bool(f) and (f.startswith(PLACEHOLDER_ROOT + "/") or "{edition-Trailer}" in f or f.endswith("/S00E00.Trailer.mp4"))

def strip_placeholders(row):
    """Drop placeholder copies from a Plex item; None if nothing real is left."""
    meds = row.get("Media")
    if not meds: return row
    keep = [md for md in meds if not all(is_placeholder(pt.get("file")) for pt in md.get("Part", []) if pt.get("file"))
            or not any(pt.get("file") for pt in md.get("Part", []))]
    if not keep: return None
    if len(keep) != len(meds): row = dict(row, Media=keep)
    return row

def plex_paged(path, **params):
    raw = params.pop("_raw", False)              # True: keep Agregarr placeholders (only the Coming Soon check wants them)
    start = 0
    while True:
        p = S["plex"]
        q = urllib.parse.urlencode(params)
        mc = http(p["url"].rstrip("/") + path + "?" + q, {"X-Plex-Token": p["token"],
                  "X-Plex-Container-Start": str(start), "X-Plex-Container-Size": str(PAGE)}, timeout=120)["MediaContainer"]
        mc = map_in(mc, p)
        rows = mc.get("Metadata", [])
        for r in rows:
            r = r if raw else strip_placeholders(r)
            if r is not None: yield r
        start += len(rows)
        if not rows or start >= int(mc.get("totalSize", start)):
            break

def plex_refresh_folder(section, folder):
    """Ask Plex to rescan one folder (partial scan) so it drops moved copies / picks restored ones."""
    try:
        p = S["plex"]
        q = urllib.parse.urlencode({"path": to_app(folder, p)})
        req = urllib.request.Request("%s/library/sections/%s/refresh?%s" % (p["url"].rstrip("/"), section, q), headers={"X-Plex-Token": p["token"]})
        urllib.request.urlopen(req, timeout=30).read()
        return True
    except Exception as e:
        log("plex refresh failed: %r" % e); return False

_EDITION_RE = re.compile(r"\{edition-([^}]+)\}", re.I)

def edition_of(m=None, md=None):
    """v0.1.3: Plex edition of an item or one of its copies ("Director's Cut"), from Plex's editionTitle or an
    {edition-...} tag in the file/folder name. Different editions of a film are not duplicates."""
    if md is not None:
        f = ((md.get("Part") or [{}])[0].get("file") or "")
        x = _EDITION_RE.search(f)
        if x: return x.group(1).strip().lower()
    if m is not None and m.get("editionTitle"): return str(m["editionTitle"]).strip().lower()
    if m is not None:
        for md2 in m.get("Media", []):
            x = _EDITION_RE.search((md2.get("Part") or [{}])[0].get("file") or "")
            if x: return x.group(1).strip().lower()
    return ""

def guids(m):
    out = {}
    for g in m.get("Guid", []):
        k, _, v = g.get("id", "").partition("://")
        if k and v: out[k] = v
    return out

def plex_link(rating_key):
    p = S["plex"]
    return {"label": "Open in Plex", "url": "%s/desktop/#!/server/%s/details?key=%s" % (
        p.get("public", "https://app.plex.tv").rstrip("/"), p["machine"], urllib.parse.quote("/library/metadata/%s" % rating_key, safe=""))}

def human(n):
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB": return ("%.1f %s" % (n, u)) if u != "B" else "%d B" % n
        n /= 1024

def mins(ms):
    m = int(round((ms or 0) / 60000))
    return "%d h %02d min" % (m // 60, m % 60) if m >= 60 else "%d min" % m

def media_desc(md):
    res = md.get("videoResolution", "")
    bits = [res.upper().replace("4K", "2160p") + ("p" if res.isdigit() else ""), md.get("videoCodec", ""),
            human(sum(p.get("size", 0) or 0 for p in md.get("Part", []))), mins(md.get("duration")) if md.get("duration") else ""]
    return " · ".join(b for b in bits if b)

def version(md):
    p = (md.get("Part") or [{}])[0]
    return {"desc": media_desc(md), "path": p.get("file", ""), "size": sum(x.get("size", 0) or 0 for x in md.get("Part", [])),
            "duration": md.get("duration") or 0, "height": int(md.get("height") or 0), "bitrate": md.get("bitrate") or 0,
            "parts": [x.get("file", "") for x in md.get("Part", []) if x.get("file")]}

# ---------------- recommendation ----------------
def recommend(vers, tracked=()):
    """Mark each copy keep / remove / plex, with a plain reason. Returns the list (mutated in place)."""
    longest = max([v["duration"] for v in vers] or [0])
    for v in vers:
        f, base = v["path"], os.path.basename(v["path"]).lower()
        v["tracked"] = f in tracked
        if "/Plex Versions/" in f:
            v["action"], v["reason"] = "plex", "Plex's own converted copy: remove it under Plex → Settings → Manage → Optimized Versions"
        elif "TdarrCacheFile" in f:
            v["action"], v["reason"], v["sure"] = "remove", "Leftover Tdarr temp file", True
        elif v["size"] and v["size"] < TINY:
            v["action"], v["reason"], v["sure"] = "remove", "Tiny file (%s): broken or a sample" % human(v["size"]), True
        elif longest and v["duration"] and v["duration"] < 0.6 * longest:
            v["action"], v["reason"], v["sure"] = "remove", "Much shorter than the others (%s): probably broken" % mins(v["duration"]), True
        elif "sample" in base:
            v["action"], v["reason"], v["sure"] = "remove", "Looks like a sample", True
        else:
            v["action"], v["reason"] = "candidate", ""
    cands = [v for v in vers if v["action"] == "candidate"]
    fallback = not cands
    if not cands:   # nothing looked good: keep the biggest normal file, never recommend removing everything
        cands = [max([v for v in vers if v["action"] != "plex"] or vers, key=lambda v: v["size"])]
        cands[0]["action"] = "candidate"
    def good_box(v): return 1 if os.path.splitext(v["path"])[1].lower() in GOOD_CONTAINERS else 0
    def key(v):
        dur = round(v["duration"] / max(1, longest) * 50)   # 2% buckets: near-equal lengths count as equal
        return (v["height"], dur, good_box(v), 1 if v["tracked"] else 0, v["bitrate"], v["size"])
    best = max(cands, key=key)
    for v in cands:
        if v is best:
            why = []
            if len(cands) > 1:
                other = [o for o in cands if o is not best]
                if any(best["height"] > o["height"] for o in other): why.append("higher resolution")
                if any(best["duration"] - o["duration"] > 60000 for o in other): why.append("longest (%s)" % mins(best["duration"]))
                if good_box(best) and any(not good_box(o) for o in other): why.append("better file type (%s)" % os.path.splitext(best["path"])[1][1:].upper())
                if best["tracked"]: why.append("the file Radarr/Sonarr/Sportarr tracks")
                if not why: why.append("best quality of the copies")
            elif fallback:
                why.append("the biggest copy, but every copy looks broken: check it plays")
            else:
                why.append("the only good copy")
            v["action"], v["reason"] = "keep", "Keep: " + ", ".join(why)
        else:
            v["sure"] = best["height"] > v["height"]   # a clearly lower resolution is a safe call; same-quality picks are "check"
            if best["height"] > v["height"]: r = "Lower resolution than the one kept"
            elif best["duration"] - v["duration"] > 60000: r = "%s shorter than the one kept" % mins(best["duration"] - v["duration"])
            elif good_box(best) and not good_box(v): r = "Same quality, but a .%s file (the %s is kept%s)" % (
                os.path.splitext(v["path"])[1][1:].lower(), os.path.splitext(best["path"])[1][1:].upper(),
                "; Sonarr/Radarr will switch to it on the rescan" if v["tracked"] else "")
            elif best["tracked"] and not v["tracked"]: r = "Same quality; not the file Radarr/Sonarr/Sportarr tracks"
            else: r = "Same quality as the one kept"
            v["action"], v["reason"] = "remove", r
    return vers

def confidence(vers):
    rm = [v for v in vers if v.get("action") == "remove"]
    return "sure" if rm and all(v.get("sure") for v in rm) else "check"

def side_stem(name):
    n = name
    if ".part-" in n: n = n.split(".part-")[0]
    return os.path.splitext(n)[0]

def leftovers(vers):
    """Known junk side files next to the copies being removed: same name as a removed copy, or belonging to no video."""
    rm = [v["path"] for v in vers if v.get("action") == "remove"]
    if not rm: return []
    out = []
    for folder in sorted({os.path.dirname(p) for p in rm}):
        try: names = os.listdir(folder)
        except OSError: continue
        rm_stems = {os.path.splitext(os.path.basename(p))[0] for p in rm if os.path.dirname(p) == folder}
        keep_videos = {os.path.splitext(n)[0] for n in names if n.lower().endswith(VIDEO_EXT) and os.path.join(folder, n) not in rm}
        for n in sorted(names):
            p = os.path.join(folder, n); ext = os.path.splitext(n)[1].lower()
            if ext not in SIDE_EXT or not os.path.isfile(p): continue
            size = os.path.getsize(p)
            if ext == ".txt" and size > 0: continue          # only empty .txt files
            st = side_stem(n)
            if st in rm_stems or st not in keep_videos:
                out.append({"path": p, "size": size})
    return out

# ---------------- Radarr / Sonarr ----------------
def arr(a, path, data=None, method=None):
    return map_in(http(a["url"].rstrip("/") + "/api/v3" + path, {"X-Api-Key": a["key"]}, data=map_out(data, a), method=method, timeout=120), a)

def arr_by_name(name):
    return next((a for a in S.get("arrs", []) if a["name"] == name), None)

def load_arrs():
    movies, series, arr_files = {}, {}, []
    for a in S.get("arrs", []):
        try:
            a["roots"] = [r["path"].rstrip("/") for r in arr(a, "/rootfolder")]
            if a["kind"] == "radarr":
                for m in arr(a, "/movie"):
                    movies.setdefault(str(m.get("tmdbId")), []).append((a, m))
                    mf = m.get("movieFile")
                    if m.get("hasFile") and mf and mf.get("path"):
                        arr_files.append((a, m, mf["path"], mf.get("size", 0)))
            else:
                for sr in arr(a, "/series"):
                    series.setdefault(str(sr.get("tvdbId")), []).append((a, sr))
        except Exception as e:
            log("arr %s failed: %s" % (a["name"], e))
    return movies, series, arr_files

def arr_link(a, item, kind):
    return {"label": "Open in " + a["name"], "url": "%s/%s/%s" % (a["public"].rstrip("/"), "movie" if kind == "movie" else "series", item.get("titleSlug", ""))}

def arr_rescan(refs):
    """refs: [{"arr": name, "kind": "movie"|"series", "id": n}] -> ask each arr to rescan (only reads the disk)."""
    done = []
    for r in refs or []:
        a = arr_by_name(r.get("arr"))
        if not a: continue
        try:
            if r["kind"] == "movie": arr(a, "/command", {"name": "RescanMovie", "movieId": int(r["id"])}, "POST")
            else: arr(a, "/command", {"name": "RescanSeries", "seriesId": int(r["id"])}, "POST")
            done.append(a["name"])
        except Exception as e:
            log("rescan %s failed: %r" % (a["name"], e))
    return done

# ---------------- scan ----------------
SCAN_LOCK = threading.Lock()
MOVE_LOCK = threading.Lock()
STATE = {"running": False, "step": "", "progress": 0.0}

def iid(*parts):
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:16]

def in_trash():
    with db() as c:
        return {r["src"] for r in c.execute("SELECT src FROM trash WHERE restored_at IS NULL")}

def scan():
    if not SCAN_LOCK.acquire(blocking=False):
        return False
    STATE.update(running=True, step="Starting", progress=0.0)
    started = now_iso()
    issues, n_items, n_files = [], 0, 0
    try:
        S["plex"]["machine"] = plex("/identity")["machineIdentifier"]
        STATE["step"] = "Reading Radarr and Sonarr"
        movies, series, arr_files = load_arrs()
        trashed = in_trash()
        try: sport, sport_roots = sportarr_files()
        except Exception as e:
            log("sportarr failed: %r" % e); sport, sport_roots = {}, []
        ep_files = {}   # (arr name, series id) -> set of episode file paths Sonarr tracks (fetched only for duplicates)
        def sonarr_tracked(a, sid):
            k = (a["name"], sid)
            if k not in ep_files:
                try: ep_files[k] = {f.get("path") for f in arr(a, "/episodefile?seriesId=%d" % sid)}
                except Exception: ep_files[k] = set()
            return ep_files[k]
        sections = [d for d in plex("/library/sections").get("Directory", []) if d.get("type") in ("movie", "show")]
        for si, sec in enumerate(sections):
            lib, kind = sec["title"], sec["type"]
            STATE.update(step="Checking " + lib, progress=si / max(1, len(sections)))
            by_guid = {}
            locs = [l["path"].rstrip("/") for l in sec.get("Location", [])]
            mine = lambda a: any(r in locs for r in a.get("roots", []))
            lib_movies = {k: [(a, it) for a, it in v if mine(a)] for k, v in movies.items()} if kind == "movie" else {}
            show_tvdb = {}
            if kind == "show":
                for sh in plex_paged("/library/sections/%s/all" % sec["key"], type=2, includeGuids=1):
                    show_tvdb[sh["ratingKey"]] = guids(sh).get("tvdb")
            t = 1 if kind == "movie" else 4
            for m in plex_paged("/library/sections/%s/all" % sec["key"], type=t, includeGuids=1):
                n_items += 1
                title = "%s (%s)" % (m.get("title"), m.get("year", "?")) if kind == "movie" else \
                        "%s S%02dE%02d" % (m.get("grandparentTitle"), int(m.get("parentIndex", 0) or 0), int(m.get("index", 0) or 0))
                g = guids(m)
                medias = m.get("Media", [])
                links, refs, tracked = [plex_link(m["ratingKey"])], [], set(sport)
                if kind == "movie":
                    for a, it in lib_movies.get(g.get("tmdb"), []):
                        links.append(arr_link(a, it, "movie")); refs.append({"arr": a["name"], "kind": "movie", "id": it["id"]})
                        if (it.get("movieFile") or {}).get("path"): tracked.add(it["movieFile"]["path"])
                    arr_series = []
                else:
                    tv = show_tvdb.get(m.get("grandparentRatingKey"))
                    arr_series = [(a, it) for a, it in series.get(str(tv), []) if mine(a)]
                    for a, it in arr_series:
                        links.append(arr_link(a, it, "series")); refs.append({"arr": a["name"], "kind": "series", "id": it["id"]})
                # --- missing files ---
                for md in medias:
                    for p in md.get("Part", []):
                        f = p.get("file")
                        if not f: continue
                        n_files += 1
                        if not os.path.exists(f) and f not in trashed:
                            note = ""
                            if kind == "movie" and lib_movies.get(g.get("tmdb")):
                                parts = []
                                for a, it in lib_movies[g["tmdb"]]:
                                    mf = it.get("movieFile") or {}
                                    if it.get("hasFile") and mf.get("path") and os.path.exists(mf["path"]):
                                        parts.append("%s has it at %s, so Plex just needs a library scan." % (a["name"], mf["path"]))
                                    else:
                                        parts.append("%s has no file for it (%s)." % (a["name"], "monitored" if it.get("monitored") else "not monitored"))
                                note = " ".join(parts)
                            elif kind == "movie":
                                note = "Not in Radarr."
                            issues.append(dict(id=iid("missing", f), chk="missing", sev="bad", title=title, library=lib,
                                               detail={"text": "Plex lists a file that isn't on the disk.", "path": f, "note": note, "size": human(p.get("size"))},
                                               links=links))
                # copies that are really on disk and not in our trash
                live = [md for md in medias if md.get("Part") and all(os.path.exists(x.get("file", "")) and x.get("file") not in trashed
                        for x in md.get("Part", []))]
                # v0.1.3: copies that are different editions (Theatrical + Director's Cut) aren't duplicates of each other:
                # only copies of the SAME edition count (largest group), and none if every copy is a different edition
                live_d = live
                if len(live) > 1:
                    eds = {}
                    for md in live: eds.setdefault(edition_of(m, md) if _EDITION_RE.search((md.get("Part") or [{}])[0].get("file") or "") else "", []).append(md)
                    if len(eds) > 1:
                        live_d = max(eds.values(), key=len) if max(len(v) for v in eds.values()) > 1 else live[:1]
                # --- duplicates: several versions in one item ---
                if len(live_d) > 1 and len({(md.get("Part") or [{}])[0].get("file") for md in live_d}) == 1:
                    f0 = live[0]["Part"][0]["file"]
                    issues.append(dict(id=iid("double", lib, m["ratingKey"]), chk="dupes", sev="amb", title=title, library=lib,
                                       detail={"text": "Plex lists the same file %d times. It's one file on disk, so there's nothing to delete." % len(live),
                                               "kind": "double", "path": f0, "rk": m["ratingKey"], "ep_title": m.get("title", ""),
                                               "section": sec["key"], "refs": refs, "confidence": "fix",
                                               "versions": [dict(version(live[0]), action="keep", reason="The one real file")]},
                                       links=links))
                elif len(live_d) > 1:
                    vers = [version(md) for md in live_d]
                    if kind == "show":
                        for a, it in arr_series: tracked |= sonarr_tracked(a, it["id"])
                    issues.append(dict(id=iid("dupe-item", lib, m["ratingKey"]), chk="dupes", sev="amb", title=title, library=lib,
                                       detail={"text": "%d copies in one Plex item." % len(vers), "versions": recommend(vers, tracked),
                                               "leftovers": leftovers(vers), "confidence": confidence(vers), "section": sec["key"], "refs": refs},
                                       links=links))
                key = g.get("tmdb") or g.get("imdb") or g.get("tvdb")
                if key and live:
                    gk = key if kind == "movie" else (key, m.get("parentIndex"), m.get("index"))
                    ed = edition_of(m) if kind == "movie" else ""
                    if ed: gk = (gk, "edition", ed)            # v0.1.3: each edition is its own group (ids unchanged when there's no edition)
                    by_guid.setdefault(gk, []).append((m, title, links, refs, tracked, live, arr_series))
            # --- duplicates: the same film/episode as separate items in one library ---
            for k, lst in by_guid.items():
                if len(lst) > 1:
                    m0, title, links, refs, tracked, _, arr_series = lst[0]
                    vers, tr = [], set(tracked)
                    for m, _, _, _, t2, live, aser in lst:
                        tr |= t2
                        if kind == "show":
                            for a, it in aser: tr |= sonarr_tracked(a, it["id"])
                        vers += [version(md) for md in live]
                    recommend(vers, tr)
                    issues.append(dict(id=iid("dupe-guid", lib, k), chk="dupes", sev="amb", title=title, library=lib,
                                       detail={"text": "%d separate Plex items for the same %s." % (len(lst), "film" if kind == "movie" else "episode"),
                                               "versions": vers, "leftovers": leftovers(vers), "confidence": confidence(vers), "section": sec["key"], "refs": refs},
                                       links=[plex_link(m["ratingKey"]) for m, *_ in lst] + [l for l in links if l["label"] != "Open in Plex"]))
        try:
            issues += health_scan(sections, movies, series, arr_files, trashed)
        except Exception as e:
            log("file health check failed: %r" % e)
        try:
            issues += virus_scan(sections, movies, series, trashed, started)
        except Exception as e:
            log("virus check failed: %r" % e)
        try:
            issues += subs_scan(movies, series, trashed)
        except Exception as e:
            log("subtitle check failed: %r" % e)
        try:
            issues += meta_scan(sections, movies, series, arr_files)
        except Exception as e:
            log("metadata check failed: %r" % e)
        try:
            issues += coming_scan(sections, movies, series)
        except Exception as e:
            log("coming soon check failed: %r" % e)
        sizes = {}
        try:
            q, sizes = quality_scan(sections, movies, series, trashed); issues += q
            try: monitor_state(q)                             # v32: Monitor button where Sonarr/Radarr won't upgrade by itself
            except Exception as e: log("monitor check failed: %r" % e)
            try: issues += queue_cards()                      # v33: Downloads (Radarr/Sonarr queues)
            except Exception as e: log("queue check failed: %r" % e)
        except Exception as e:
            log("quality check failed: %r" % e)
        try:
            skip = {p for it in issues if it["chk"] == "quality" and it["detail"].get("kind") == "hog" for p in it["detail"].get("tdarr_paths", [])}
            issues += tdarr_missed_scan(trashed, skip)
        except Exception as e:
            log("tdarr missed check failed: %r" % e)
        try:
            issues += unwatched_scan(sections, sizes)
        except Exception as e:
            log("unwatched check failed: %r" % e)
        try:
            issues += sports_scan(sections, sport, sport_roots, trashed)
        except Exception as e:
            log("sportarr check failed: %r" % e)
        STATE.update(step="Checking Radarr files", progress=0.95)
        for a, m, path, size in arr_files:
            if not os.path.exists(path) and path not in trashed:
                issues.append(dict(id=iid("arrmissing", a["name"], path), chk="missing", sev="bad",
                                   title="%s (%s)" % (m.get("title"), m.get("year", "?")), library=a["name"],
                                   detail={"text": "%s thinks it has this file, but it isn't on the disk." % a["name"], "path": path,
                                           "note": "A rescan in %s will notice it's gone and search again if the film is monitored." % a["name"],
                                           "size": human(size)},
                                   links=[arr_link(a, m, "movie")]))
        finished = now_iso()
        with db() as c:
            sid = c.execute("INSERT INTO scans (started, finished, ok, items, files, issues, note) VALUES (?,?,?,?,?,?,?)",
                            (started, finished, 1, n_items, n_files, len(issues), "")).lastrowid
            seen = {r["id"]: r["first_seen"] for r in c.execute("SELECT id, first_seen FROM issues")}
            c.execute("DELETE FROM issues")
            for it in issues:
                c.execute("INSERT OR REPLACE INTO issues VALUES (?,?,?,?,?,?,?,?,?,?)",
                          (it["id"], sid, it["chk"], it["sev"], it["title"], it["library"], json.dumps(it["detail"]),
                           json.dumps(it["links"]), seen.get(it["id"], finished), finished))
        log("scan done: %d items, %d files, %d issues" % (n_items, n_files, len(issues)))
    except Exception as e:
        log("scan failed: %r" % e)
        with db() as c:
            c.execute("INSERT INTO scans (started, finished, ok, items, files, issues, note) VALUES (?,?,?,?,?,?,?)",
                      (started, now_iso(), 0, n_items, n_files, 0, str(e)[:300]))
    finally:
        STATE.update(running=False, step="", progress=1.0); _STATS["v"] = None
        SCAN_LOCK.release()
    return True

# ---------------- trash ----------------
def safe_media_path(p):
    rp = os.path.realpath(p)
    return rp.startswith(MEDIA_ROOT + os.sep) and not rp.startswith(TRASH_DIR + os.sep)

def plex_parts(rk):
    """Files Plex lists for one item, or None if the item is gone."""
    try:
        mc = plex("/library/metadata/%s" % rk)
    except urllib.error.HTTPError as e:
        return None if e.code == 404 else []
    out = []
    for m in mc.get("Metadata", []):
        for md in m.get("Media", []):
            out += [p.get("file") for p in md.get("Part", []) if p.get("file")]
    return out

def verify(done_id, rks, removed, kept):
    """After a clean-up: wait for Plex's folder rescan and check it now lists only the kept copy."""
    status, note = "warn", "Couldn't reach Plex to check."
    for attempt in range(20):                      # up to ~60 s
        time.sleep(3)
        try:
            parts = []
            gone = 0
            for rk in rks:
                p = plex_parts(rk)
                if p is None: gone += 1
                else: parts += p
            still = [x for x in removed if x in parts]
            if not still and (kept in parts or gone):
                n = len([x for x in parts if x])
                status = "ok"
                note = "Plex checked: %d version%s left (%s)." % (n, "" if n == 1 else "s", os.path.basename(kept)) if kept in parts else \
                       "Plex checked: the extra item is gone; %s stays." % os.path.basename(kept)
                break
            status, note = "warn", "Plex still lists %s. It should drop it at its next library scan." % ", ".join(os.path.basename(x) for x in still)
            if attempt == 8:                         # nudge Plex once more half way
                for d in sorted({os.path.dirname(x) for x in removed}):
                    for sec in plex("/library/sections").get("Directory", []):
                        if any(d.startswith(l["path"].rstrip("/")) for l in sec.get("Location", [])): plex_refresh_folder(sec["key"], d)
        except Exception as e:
            status, note = "warn", "Couldn't check Plex: %s" % e
    with db() as c:
        c.execute("UPDATE done SET status=?, note=?, checked_at=? WHERE id=?", (status, note, now_iso(), done_id))
    log("verify %s: %s" % (done_id, note))

def plex_put(path):
    p = S["plex"]
    req = urllib.request.Request(p["url"].rstrip("/") + path, headers={"X-Plex-Token": p["token"]}, method="PUT")
    urllib.request.urlopen(req, timeout=30).read()

def find_listing(section, ep_title, path):
    """Plex items in a section that list this file: [(ratingKey, number of entries for the file)]."""
    out = []
    try:
        mc = plex("/library/sections/%s/search" % section, type=4, query=ep_title) if ep_title else {}
        for m in mc.get("Metadata", []):
            n = sum(1 for md in m.get("Media", []) for p in md.get("Part", []) if p.get("file") == path)
            if n: out.append((m["ratingKey"], n))
    except Exception as e:
        log("find_listing failed: %r" % e)
    return out

def fix_double(issue_id):
    """The 'Plex Dance' for a file Plex lists twice: move it out, let Plex drop the entries, move it back, let Plex add it once."""
    with db() as c:
        row = c.execute("SELECT * FROM issues WHERE id=? AND chk='dupes'", (issue_id,)).fetchone()
    if not row: return {"ok": False, "error": "That item isn't in the last scan any more. Scan again first."}
    d = json.loads(row["detail"])
    if d.get("kind") != "double": return {"ok": False, "error": "This isn't a double listing."}
    src, sec, title = d["path"], d.get("section"), d.get("ep_title", "")
    if not safe_media_path(src) or not os.path.isfile(src): return {"ok": False, "error": "The file isn't where Plex says it is, so nothing was changed."}
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    tmp = os.path.join(TRASH_DIR, ".plex-fix", stamp + "-" + issue_id, os.path.relpath(os.path.realpath(src), MEDIA_ROOT))
    with db() as c:
        done_id = c.execute("INSERT INTO done (issue_id, title, library, kept, moved, trash_ids, links, at, status, note) VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (issue_id, row["title"], row["library"], src, "[]", "[]", row["links"], now_iso(), "checking", "Fixing Plex's listing…")).lastrowid
        c.execute("DELETE FROM issues WHERE id=?", (issue_id,))
    threading.Thread(target=_dance, args=(done_id, issue_id, row["title"], src, tmp, sec, title), daemon=True).start()
    return {"ok": True, "started": True}

def _dance(done_id, issue_id, item_title, src, tmp, sec, ep_title):
    try:
        _dance_steps(done_id, issue_id, item_title, src, tmp, sec, ep_title)
    except Exception as e:
        back = os.path.exists(src)
        with db() as c:
            c.execute("UPDATE done SET status='warn', note=?, checked_at=? WHERE id=?",
                      ("Something went wrong (%s). %s" % (e, "The file is back in place." if back else "The file is in the Trash: use Put Back."), now_iso(), done_id))
        log("plex fix failed: %r" % e)
    finally:
        for root, dirs, files in os.walk(os.path.join(TRASH_DIR, ".plex-fix"), topdown=False):
            if not os.listdir(root):
                try: os.rmdir(root)
                except OSError: pass

def _dance_steps(done_id, issue_id, item_title, src, tmp, sec, ep_title):
    folder = os.path.dirname(src)
    status, note, tid = "warn", "", None
    with MOVE_LOCK:
        try:
            os.makedirs(os.path.dirname(tmp), exist_ok=True)
            size = os.path.getsize(src)
            move_file(src, tmp)
            with db() as c:   # recorded like a trash item, so it can always be put back by hand if anything breaks
                tid = c.execute("INSERT INTO trash (issue_id, title, src, dst, size, reason, moved_at) VALUES (?,?,?,?,?,?,?)",
                                (issue_id, item_title, src, tmp, size, "Plex fix in progress (moved back automatically)", now_iso())).lastrowid
            plex_refresh_folder(sec, folder)
            gone = False
            for attempt in range(30):                    # up to ~90 s for Plex to drop the entries
                time.sleep(3)
                if not find_listing(sec, ep_title, src): gone = True; break
                if attempt == 12:
                    try: plex_put("/library/sections/%s/emptyTrash" % sec)
                    except Exception as e: log("emptyTrash failed: %r" % e)
        finally:
            if os.path.exists(tmp) and not os.path.exists(src):
                move_file(tmp, src)
                with db() as c:
                    if tid: c.execute("UPDATE trash SET restored_at=? WHERE id=?", (now_iso(), tid))
    if not gone:
        note = "Plex didn't let go of the old entries, so the file was put straight back. Nothing changed; try again later."
    else:
        plex_refresh_folder(sec, folder)
        for attempt in range(30):
            time.sleep(3)
            hits = find_listing(sec, ep_title, src)
            if hits and all(n == 1 for _, n in hits) and len(hits) == 1:
                status, note = "ok", "Plex checked: listed once now (%s)." % os.path.basename(src)
                with db() as c:   # Plex re-added the episode under a new ID: point Open in Plex at it
                    old = json.loads(c.execute("SELECT links FROM done WHERE id=?", (done_id,)).fetchone()["links"] or "[]")
                    new = [plex_link(hits[0][0])] + [l for l in old if l.get("label") != "Open in Plex"]
                    c.execute("UPDATE done SET links=? WHERE id=?", (json.dumps(new), done_id))
                break
            note = "File is back; waiting for Plex to add it again." if not hits else "Plex still lists it %d times." % max(n for _, n in hits)
    with db() as c:
        c.execute("UPDATE done SET status=?, note=?, checked_at=? WHERE id=?", (status, note, now_iso(), done_id))
    event("plexfix", "%s: %s" % (item_title, note))

def cleanup(issue_id, keep_idx=None):
    """Move the 'remove' copies of one duplicate issue into the trash. keep_idx = your choice of copy to keep (optional)."""
    with MOVE_LOCK:
        with db() as c:
            row = c.execute("SELECT * FROM issues WHERE id=? AND chk='dupes'", (issue_id,)).fetchone()
        if not row: return {"ok": False, "error": "That duplicate isn't in the last scan any more. Scan again first."}
        d = json.loads(row["detail"]); vers = d.get("versions", [])
        if keep_idx is not None:
            if not (isinstance(keep_idx, int) and 0 <= keep_idx < len(vers)): return {"ok": False, "error": "Unknown copy."}
            if vers[keep_idx].get("action") == "plex": return {"ok": False, "error": "A Plex-converted copy can't be the one you keep."}
            for j, v in enumerate(vers):
                if v.get("action") == "plex": continue
                v["action"] = "keep" if j == keep_idx else "remove"
                if j != keep_idx: v["reason"] = "You chose to keep another copy"
            d["leftovers"] = leftovers(vers)      # re-work side files for the copies now being removed
        keep = [v for v in vers if v.get("action") == "keep"]
        go = [v for v in vers if v.get("action") == "remove"]
        if len(keep) != 1 or not go: return {"ok": False, "error": "Nothing to clean up for this one."}
        if not os.path.exists(keep[0]["path"]): return {"ok": False, "error": "The copy to keep isn't on the disk any more, so nothing was moved. Scan again."}
        moved, problems = [], []
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        for v in go:
            for src in v.get("parts") or [v["path"]]:
                if src == keep[0]["path"] or src in keep[0].get("parts", []): continue
                if not safe_media_path(src) or not os.path.isfile(src):
                    problems.append("%s: not found or outside the media folder" % os.path.basename(src)); continue
                rel = os.path.relpath(os.path.realpath(src), MEDIA_ROOT)
                dst = os.path.join(TRASH_DIR, stamp + "-" + issue_id, rel)
                try:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    size = os.path.getsize(src)
                    move_file(src, dst)        # same dataset: instant, nothing copied
                    with db() as c:
                        c.execute("INSERT INTO trash (issue_id, title, src, dst, size, reason, moved_at) VALUES (?,?,?,?,?,?,?)",
                                  (issue_id, row["title"], src, dst, size, v.get("reason", ""), now_iso()))
                    moved.append((src, size))
                except Exception as e:
                    problems.append("%s: %s" % (os.path.basename(src), e))
        rm_dirs = {os.path.dirname(v["path"]) for v in go}
        side_n = 0
        for lf in d.get("leftovers", []) if moved else []:
            src = lf["path"]
            if os.path.dirname(src) not in rm_dirs or os.path.splitext(src)[1].lower() not in SIDE_EXT: continue
            if not safe_media_path(src) or not os.path.isfile(src): continue
            if src.lower().endswith(".txt") and os.path.getsize(src) > 0: continue
            rel = os.path.relpath(os.path.realpath(src), MEDIA_ROOT)
            dst = os.path.join(TRASH_DIR, stamp + "-" + issue_id, rel)
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True); size = os.path.getsize(src); move_file(src, dst)
                with db() as c:
                    c.execute("INSERT INTO trash (issue_id, title, src, dst, size, reason, moved_at) VALUES (?,?,?,?,?,?,?)",
                              (issue_id, row["title"], src, dst, size, "Leftover side file", now_iso()))
                side_n += 1
            except Exception as e:
                problems.append("%s: %s" % (os.path.basename(src), e))
        if moved:
            folders = sorted({os.path.dirname(s) for s, _ in moved})
            for f in folders: plex_refresh_folder(d.get("section"), f)
            rescanned = arr_rescan(d.get("refs"))
            links = json.loads(row["links"] or "[]")
            rks = [m.group(1) for l in links for m in [re.search(r"metadata%2F(\d+)", l.get("url", ""))] if m]
            with db() as c:
                tids = [r["id"] for r in c.execute("SELECT id FROM trash WHERE issue_id=? AND restored_at IS NULL AND purged_at IS NULL AND dst LIKE ?",
                                                    (issue_id, "%" + stamp + "%"))]
                done_id = c.execute("INSERT INTO done (issue_id, title, library, kept, moved, trash_ids, links, at, status, note) VALUES (?,?,?,?,?,?,?,?,?,?)",
                                    (issue_id, row["title"], row["library"], keep[0]["path"], json.dumps([s for s, _ in moved]), json.dumps(tids),
                                     row["links"], now_iso(), "checking", "Checking Plex…")).lastrowid
                c.execute("DELETE FROM issues WHERE id=?", (issue_id,))
            if rks: threading.Thread(target=verify, args=(done_id, rks, [s for s, _ in moved], keep[0]["path"]), daemon=True).start()
            else:
                with db() as c: c.execute("UPDATE done SET status='warn', note=? WHERE id=?", ("No Plex item to check.", done_id))
            event("cleanup", "%s: moved %d cop%s to the trash (%s)%s. Kept %s." % (row["title"], len(moved), "y" if len(moved) == 1 else "ies",
                  human(sum(s for _, s in moved)), (" plus %d leftover side file%s" % (side_n, "" if side_n == 1 else "s")) if side_n else "",
                  os.path.basename(keep[0]["path"])))
            return {"ok": True, "moved": len(moved), "side_files": side_n, "freed_later": human(sum(s for _, s in moved)), "rescanned": rescanned, "problems": problems}
        return {"ok": False, "error": "Nothing was moved. " + "; ".join(problems)}

def restore(trash_id):
    with MOVE_LOCK:
        with db() as c:
            t = c.execute("SELECT * FROM trash WHERE id=? AND restored_at IS NULL AND purged_at IS NULL", (trash_id,)).fetchone()
        if not t: return {"ok": False, "error": "That file isn't in the trash."}
        if os.path.exists(t["src"]): return {"ok": False, "error": "A file already exists at the original place, so nothing was changed."}
        if not os.path.isfile(t["dst"]): return {"ok": False, "error": "The file is no longer in the trash."}
        os.makedirs(os.path.dirname(t["src"]), exist_ok=True)
        move_file(t["dst"], t["src"])
        with db() as c:
            c.execute("UPDATE trash SET restored_at=? WHERE id=?", (now_iso(), trash_id))
        for sec in plex("/library/sections").get("Directory", []):
            if any(t["src"].startswith(l["path"].rstrip("/") + "/") for l in sec.get("Location", [])):
                plex_refresh_folder(sec["key"], os.path.dirname(t["src"]))
        event("restore", "Put back %s" % os.path.basename(t["src"]))
        return {"ok": True}

def purge(older_than_days=None):
    """Delete trashed files for good. older_than_days=None empties everything."""
    with MOVE_LOCK:
        with db() as c:
            rows = c.execute("SELECT * FROM trash WHERE restored_at IS NULL AND purged_at IS NULL").fetchall()
        cutoff = datetime.now() - timedelta(days=older_than_days) if older_than_days is not None else None
        n, freed = 0, 0
        for t in rows:
            if cutoff and datetime.fromisoformat(t["moved_at"]) > cutoff: continue
            if os.path.realpath(t["dst"]).startswith(TRASH_DIR + os.sep) and os.path.isfile(t["dst"]):
                os.remove(t["dst"]); n += 1; freed += t["size"] or 0
            with db() as c:
                c.execute("UPDATE trash SET purged_at=? WHERE id=?", (now_iso(), t["id"]))
        # tidy empty folders inside the trash
        for root, dirs, files in os.walk(TRASH_DIR, topdown=False):
            if root != TRASH_DIR and not os.listdir(root):
                try: os.rmdir(root)
                except OSError: pass
        if n: event("purge", "Emptied %d file%s from the trash (%s freed)." % (n, "" if n == 1 else "s", human(freed)))
        return {"ok": True, "deleted": n, "freed": human(freed)}

def purge_ids(ids):
    """Delete chosen trash entries for good."""
    ids = [int(i) for i in ids if str(i).isdigit()][:500]
    with MOVE_LOCK:
        n, freed = 0, 0
        for tid in ids:
            with db() as c:
                t = c.execute("SELECT * FROM trash WHERE id=? AND restored_at IS NULL AND purged_at IS NULL", (tid,)).fetchone()
            if not t: continue
            if os.path.realpath(t["dst"]).startswith(TRASH_DIR + os.sep) and os.path.isfile(t["dst"]):
                os.remove(t["dst"]); n += 1; freed += t["size"] or 0
            with db() as c:
                c.execute("UPDATE trash SET purged_at=? WHERE id=?", (now_iso(), tid))
        for root, dirs, files in os.walk(TRASH_DIR, topdown=False):
            if root != TRASH_DIR and not os.listdir(root):
                try: os.rmdir(root)
                except OSError: pass
        if n: event("purge", "Deleted %d file%s from the trash for good (%s freed)." % (n, "" if n == 1 else "s", human(freed)))
        return {"ok": True, "deleted": n, "freed": human(freed)}

# ---------------- Radarr/Sonarr recycle bins in the Trash screen (v29) ----------------
# "Find a smaller copy" and Radarr upgrades put the old file in the arr's own recycle bin, not our trash. They are listed
# in the Trash screen too (ids "rb<hash>") so Put Back / Delete Immediately / Empty Trash work on them. The arr empties
# its bin itself after recycleBinCleanupDays; our 03:00 auto-purge never touches these files.
_RB = {"at": 0, "bins": []}
QBM_BIN_DAYS = int(os.environ.get("LD_QBM_BIN_DAYS", "14"))   # qbit_manage recyclebin.empty_after_x_days
_RB_NOUTIME = set()

def recycle_bins():
    if time.time() - _RB["at"] < 600: return _RB["bins"]
    bins = []
    for a in S.get("arrs", []):
        try:
            mm = arr(a, "/config/mediamanagement")
            raw = (mm.get("recycleBin") or "").rstrip("/")
            root = os.path.realpath(raw) if raw else ""
            if root and root.startswith(MEDIA_ROOT + os.sep) and not root.startswith(TRASH_DIR) and os.path.isdir(root):
                bins.append({"arr": a, "root": root, "days": int(mm.get("recycleBinCleanupDays") or 7)})
        except Exception as e:
            log("recycle bin of %s: %r" % (a["name"], e))
    # v34: qbit_manage's recycle bin (share-limit clean-up of public torrents) - files keep their path under downloads/
    qroot = os.path.join(MEDIA_ROOT, "downloads", ".RecycleBin")
    if os.path.isdir(qroot):
        bins.append({"arr": {"name": "qbit_manage", "kind": "qbm"}, "root": os.path.realpath(qroot), "days": QBM_BIN_DAYS})
    _RB.update(at=time.time(), bins=bins)
    return bins

def _rb_owners(a):
    """folder name -> (id, path, title) for every film/series of this arr (cached 60 s)."""
    ck = a.setdefault("_rbown", {"at": 0, "map": {}})
    if time.time() - ck["at"] > 60:
        items = arr(a, "/movie" if a["kind"] == "radarr" else "/series")
        ck.update(at=time.time(), map={os.path.basename(x["path"].rstrip("/")): (x["id"], x["path"].rstrip("/"), x.get("title", ""))
                                       for x in items if x.get("path")})
    return ck["map"]

def recycle_items():
    out = []
    for b in recycle_bins():
        qbm = b["arr"]["kind"] == "qbm"
        try: own = {} if qbm else _rb_owners(b["arr"])
        except Exception as e:
            log("recycle owners %s: %r" % (b["arr"]["name"], e)); own = {}
        for dp, dirs, files in os.walk(b["root"]):
            for f in files:
                p = os.path.join(dp, f)
                try: st = os.stat(p)
                except OSError: continue
                rel = os.path.relpath(p, b["root"]).split(os.sep)
                o = ("qbm", os.path.join(MEDIA_ROOT, "downloads"), rel[0]) if qbm else (own.get(rel[0]) if len(rel) > 1 else None)
                if not qbm and st.st_ctime - st.st_mtime > 86400 and p not in _RB_NOUTIME:
                    # binned by a manual import, which keeps the old file date: the arr would purge it at its next clean-up.
                    # Set the date to when it was binned so it really gets recycleBinCleanupDays.
                    try:
                        os.utime(p, (st.st_atime, st.st_ctime)); st = os.stat(p)
                    except OSError as e:
                        _RB_NOUTIME.add(p); log("recycle utime %s: %r (root-owned? the arr may purge it early)" % (p, e))
                moved = datetime.fromtimestamp(max(st.st_ctime, st.st_mtime))      # ctime = when the arr moved it in
                gone = datetime.fromtimestamp(st.st_mtime) + timedelta(days=b["days"])   # the arr's own clean-up goes by mtime
                out.append({"id": "rb" + iid(p), "title": o[2] if o else rel[0], "path": p, "bin": b,
                            "src": (os.path.join(o[1], *rel) if qbm else os.path.join(o[1], *rel[1:])) if o else os.path.join("/media", "(not in %s any more)" % b["arr"]["name"], f),
                            "owner": o, "size": st.st_size, "reason": ("Finished torrent qbit_manage cleaned up (seeding goal met)" if qbm else "Old copy in %s's recycle bin" % b["arr"]["name"]),
                            "moved_at": moved.isoformat(timespec="seconds"),
                            "empties": gone.isoformat(timespec="seconds"), "rb": b["arr"]["name"]})
    return out

def _rb_tidy(root, d):
    while d.startswith(root + os.sep) and os.path.isdir(d) and not os.listdir(d):
        try: os.rmdir(d)
        except OSError: break
        d = os.path.dirname(d)

def restore_recycle(rid, items=None):
    with MOVE_LOCK:
        it = next((x for x in (items or recycle_items()) if x["id"] == rid), None)
        if not it: return {"ok": False, "error": "That file isn't in the recycle bin any more."}
        if not it["owner"]:
            return {"ok": False, "error": "%s: the film or show isn't in %s any more, so there's nowhere to put it back." % (os.path.basename(it["path"]), it["rb"])}
        src, dst, a = it["path"], it["src"], it["bin"]["arr"]
        if a["kind"] == "qbm" and not os.path.abspath(dst).startswith(os.path.join(MEDIA_ROOT, "downloads") + os.sep):
            return {"ok": False, "error": "Refused: qbit_manage files only go back under downloads."}
        if not os.path.realpath(src).startswith(it["bin"]["root"] + os.sep): return {"ok": False, "error": "Refused: not inside the recycle bin."}
        if not os.path.abspath(dst).startswith(MEDIA_ROOT + os.sep) or os.path.abspath(dst).startswith(TRASH_DIR):
            return {"ok": False, "error": "Refused: the original place is outside the media folder."}
        if os.path.exists(dst): return {"ok": False, "error": "%s: a file already exists at the original place, so nothing was changed." % os.path.basename(dst)}
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        move_file(src, dst)
        _rb_tidy(it["bin"]["root"], os.path.dirname(src))
        note = ""
        if a["kind"] == "qbm":
            note = "The files are back in downloads, but qBittorrent no longer has the torrent; add it again there if you want it seeding."
            event("restore", "Put back %s from qbit_manage's recycle bin. %s" % (os.path.basename(dst), note))
            return {"ok": True, "note": note}
        try:
            if a["kind"] == "radarr":
                cur = arr(a, "/movie/%d" % it["owner"][0])
                if cur.get("hasFile") and dst.lower().endswith(VIDEO_EXT):
                    note = "%s already had another copy, so both are in the folder now: the Duplicates check will offer to tidy one." % a["name"]
                arr(a, "/command", {"name": "RescanMovie", "movieId": it["owner"][0]})
            else:
                arr(a, "/command", {"name": "RescanSeries", "seriesId": it["owner"][0]})
        except Exception as e:
            log("rescan after recycle put back: %r" % e)
        for sec in plex("/library/sections").get("Directory", []):
            if any(dst.startswith(l["path"].rstrip("/") + "/") for l in sec.get("Location", [])):
                plex_refresh_folder(sec["key"], os.path.dirname(dst))
        event("restore", "Put back %s from %s's recycle bin. %s" % (os.path.basename(dst), a["name"], note))
        return {"ok": True, "note": note}

def purge_recycle(ids=None):
    """Delete recycle-bin files for good: chosen ids, or every one when ids is None (Empty Trash)."""
    n, freed = 0, 0
    with MOVE_LOCK:
        for it in recycle_items():
            if ids is not None and it["id"] not in ids: continue
            p = os.path.realpath(it["path"])
            if p.startswith(it["bin"]["root"] + os.sep) and os.path.isfile(p):
                os.remove(p); n += 1; freed += it["size"]
                _rb_tidy(it["bin"]["root"], os.path.dirname(p))
    if n: event("purge", "Deleted %d file%s from the Radarr/Sonarr/qbit_manage recycle bins for good (%s freed)." % (n, "" if n == 1 else "s", human(freed)))
    return n, freed

# ---------------- metadata gaps (stage 2) ----------------
GENERIC = re.compile(r"(?:episode|episodio|folge|\u00e9pisode)\s*#?\d+(?:\.\d+)?|tba|tbd|", re.I)
STD_AGENTS = ("tv.plex.agents.movie", "tv.plex.agents.series")   # custom agents (Sportarr) use their own ids

def is_generic(t): return bool(GENERIC.fullmatch((t or "").strip()))

def arr_add_link(a, kind, ext_id):
    term = "%s:%s" % ("tmdb" if kind == "movie" else "tvdb", ext_id)
    return {"label": "Add in " + a["name"], "url": "%s/add/new?term=%s" % (a["public"].rstrip("/"), urllib.parse.quote(term))}

def first_file(items):
    for it in items:
        for md in it.get("Media", []):
            for p in md.get("Part", []):
                if p.get("file"): return p["file"]
    return None

def meta_scan(sections, movies, series, arr_files):
    out = []
    by_path = {p: (a, m) for a, m, p, _ in arr_files}
    sonarr_paths = {s["path"].rstrip("/"): (a, s) for lst in series.values() for a, s in lst if s.get("path")}
    cache = {}
    def sonarr_titles(a, sid):
        k = (a["name"], sid)
        if k not in cache:
            try: cache[k] = {(x["seasonNumber"], x["episodeNumber"]): x.get("title") or "" for x in arr(a, "/episode?seriesId=%d" % sid)}
            except Exception: cache[k] = {}
        return cache[k]
    for sec in sections:
        lib, kind, key = sec["title"], sec["type"], sec["key"]
        std = sec.get("agent") in STD_AGENTS
        locs = [l["path"].rstrip("/") for l in sec.get("Location", [])]
        owners = [a for a in S.get("arrs", []) if a["kind"] == ("radarr" if kind == "movie" else "sonarr")
                  and any(r in locs for r in a.get("roots", []))]
        STATE["step"] = "Checking metadata in " + lib
        if kind == "movie":
            for m in plex_paged("/library/sections/%s/all" % key, type=1, includeGuids=1):
                out += movie_meta(m, lib, key, std, owners, movies, by_path)
        else:
            eps = {}
            for e in plex_paged("/library/sections/%s/all" % key, type=4):
                eps.setdefault(e.get("grandparentRatingKey"), []).append(e)
            seasons = {}
            for se in plex_paged("/library/sections/%s/all" % key, type=3):
                seasons.setdefault(se.get("parentRatingKey"), []).append(se)
            for sh in plex_paged("/library/sections/%s/all" % key, type=2, includeGuids=1):
                if not eps.get(sh["ratingKey"]) and int(sh.get("leafCount") or 0) > 0:
                    continue                                       # only placeholder episodes (Agregarr Coming Soon)
                out += show_meta(sh, eps.get(sh["ratingKey"], []), lib, key, std, owners, series, sonarr_paths, sonarr_titles)
                out += season_posters(sh, seasons.get(sh["ratingKey"], []), lib, key)
    return out

def plex_img_ok(thumb):
    """Does Plex actually serve this image? (tiny transcode, so it's cheap)"""
    if not thumb: return False
    p = S["plex"]
    u = p["url"].rstrip("/") + "/photo/:/transcode?" + urllib.parse.urlencode({"width": 40, "height": 60, "minSize": 1, "url": thumb})
    try:
        with urllib.request.urlopen(urllib.request.Request(u, headers={"X-Plex-Token": p["token"]}), timeout=30) as r: return r.status == 200 and len(r.read()) > 200
    except Exception: return False

def season_posters(sh, seasons, lib, sec):
    bad = [{"rk": str(x["ratingKey"]), "t": x.get("title") or "Season %s" % x.get("index")} for x in seasons if not plex_img_ok(x.get("thumb"))]
    if not bad: return []
    bad.sort(key=lambda x: (len(x["t"]), x["t"]))
    return [_meta("seasonposters", lib, sh["ratingKey"], sh.get("title") or "?", "att",
                  "%d season%s show a blank poster in Plex: no poster is picked for %s." % (len(bad), "" if len(bad) == 1 else "s", "it" if len(bad) == 1 else "them"),
                  "Pick posters chooses the top poster Plex already has for each season (from TMDB). You can still change it in Plex afterwards.",
                  ["posters", "refresh"], [plex_link(sh["ratingKey"])], section=sec, seasons=bad)]

def _meta(kind, lib, rk, title, sev, text, note, fixes, links, **extra):
    return dict(id=iid("meta", kind, lib, rk), chk="meta", sev=sev, title=title, library=lib,
                detail=dict(kind=kind, rk=str(rk), text=text, note=note, fixes=fixes, **extra), links=links)

def movie_meta(m, lib, sec, std, owners, movies, by_path):
    rk, g = m["ratingKey"], guids(m)
    title, links = "%s (%s)" % (m.get("title"), m.get("year", "?")), [plex_link(m["ratingKey"])]
    if std and not m.get("guid", "").startswith("plex://"):
        return [_meta("unmatched", lib, rk, title, "att", "Plex couldn't match this to a film, so it has no proper info or artwork.",
                      "Refresh tries the match again. If that doesn't work, use Fix Match on the film's page in Plex.", ["refresh"], links, section=sec)]
    out = []
    if not m.get("thumb"):
        out.append(_meta("noposter", lib, rk, title, "att", "No poster in Plex.", "A refresh usually fetches one.", ["refresh"], links, section=sec))
    if not owners: return out
    files = [p.get("file") for md in m.get("Media", []) for p in md.get("Part", []) if p.get("file")]
    for f in files:
        a, it = by_path.get(f, (None, None))
        if a in owners and g.get("tmdb") and str(it.get("tmdbId")) != g["tmdb"]:
            return out + [_meta("wrongmatch", lib, rk, title, "bad",
                "Plex and %s disagree about this file. Plex thinks it's %s (%s); %s says it's %s (%s)." % (a["name"], m.get("title"), m.get("year", "?"), a["name"], it.get("title"), it.get("year", "?")),
                "Open it in Plex, choose \u22ef \u203a Fix Match, and search for \u201c%s %s\u201d." % (it.get("title"), it.get("year", "")), [], links + [arr_link(a, it, "movie")], section=sec)]
    mine = [(a, it) for a, it in movies.get(str(g.get("tmdb")), []) if a in owners]
    if not mine and not any(by_path.get(f, (None,))[0] in owners for f in files):
        return out + [_meta("notarr", lib, rk, title, "att", "Not in %s, so it won't be upgraded, and nothing will fetch it again if the file is lost." % " or ".join(a["name"] for a in owners),
                            "Add it (the file is already there, so choose the same folder and it just gets picked up), or Ignore if you keep it by hand.", [],
                            links + ([arr_add_link(owners[0], "movie", g["tmdb"])] if g.get("tmdb") else []), section=sec)]
    if mine:
        a, it = mine[0]
        if m.get("year") and it.get("year") and abs(int(m["year"]) - int(it["year"])) > 1:
            out.append(_meta("year", lib, rk, title, "att", "Plex says %s, %s says %s." % (m["year"], a["name"], it["year"]),
                             "Often just a re-release date. Refresh if the Plex page looks wrong, otherwise Ignore.", ["refresh"],
                             links + [arr_link(a, it, "movie")], section=sec, arr_year=int(it["year"])))
    return out

def show_meta(sh, eps, lib, sec, std, owners, series, sonarr_paths, sonarr_titles):
    rk, tv = sh["ratingKey"], guids(sh).get("tvdb")
    title, links = sh.get("title") or "?", [plex_link(sh["ratingKey"])]
    if std and not sh.get("guid", "").startswith("plex://"):
        return [_meta("unmatched", lib, rk, title, "att", "Plex couldn't match this to a show, so it has no proper info or artwork.",
                      "Refresh tries the match again. If that doesn't work, use Fix Match on the show's page in Plex.", ["refresh"], links, section=sec)]
    out = []
    if not sh.get("thumb"):
        out.append(_meta("noposter", lib, rk, title, "att", "No poster in Plex.", "A refresh usually fetches one.", ["refresh"], links, section=sec))
    if not owners: return out
    owner, f = None, first_file(eps)
    d = os.path.dirname(f) if f else ""
    for _ in range(3):
        if d in sonarr_paths: owner = sonarr_paths[d]; break
        d = os.path.dirname(d)
    if owner and owner[0] not in owners: owner = None
    if owner and tv and str(owner[1].get("tvdbId")) != str(tv):
        a, s = owner
        return out + [_meta("wrongmatch", lib, rk, title, "bad",
            "Plex and %s disagree about this show. Plex matched it as %s; %s has these files as %s (%s)." % (a["name"], title, a["name"], s.get("title"), s.get("year", "?")),
            "Open it in Plex, choose \u22ef \u203a Fix Match, and search for \u201c%s\u201d." % s.get("title"), [], links + [arr_link(a, s, "series")], section=sec)]
    mine = [(a, s) for a, s in series.get(str(tv), []) if a in owners] if tv else []
    if not mine and not owner:
        return out + [_meta("notarr", lib, rk, title, "att", "Not in %s, so new episodes and upgrades won't come in, and nothing will fetch it again if files are lost." % " or ".join(a["name"] for a in owners),
                            "Add it (pick the folder it's already in and the episodes get picked up), or Ignore if you keep it by hand.", [],
                            links + ([arr_add_link(owners[0], "series", tv)] if tv else []), section=sec)]
    a, s = mine[0] if mine else owner
    gen = [e for e in eps if is_generic(e.get("title"))]
    if gen:
        st, fix = sonarr_titles(a, s["id"]), []
        for e in gen:
            real = st.get((int(e.get("parentIndex") or 0), int(e.get("index") or 0)))
            if real and not is_generic(real):
                fix.append({"rk": str(e["ratingKey"]), "se": "S%02dE%02d" % (int(e.get("parentIndex") or 0), int(e.get("index") or 0)), "plex": e.get("title", ""), "real": real})
        if fix:
            fix.sort(key=lambda x: x["se"])
            out.append(_meta("titles", lib, rk, title, "att",
                "%d episode%s still called \u201cEpisode N\u201d in Plex. %s has the real titles." % (len(fix), "" if len(fix) == 1 else "s", a["name"]),
                "Use %s's titles copies them into Plex and locks them so Plex can't change them back. Refresh asks Plex to look again with its own info, which sometimes still lacks them." % a["name"],
                ["titles", "refresh"], links + [arr_link(a, s, "series")], section=sec, eps=fix[:400]))
    return out

def _plex_item(rk):
    try: return (plex("/library/metadata/%s" % rk).get("Metadata") or [None])[0]
    except Exception: return None

def _meta_left(d):
    """What's still wrong for a metadata issue: (fixed?, note, remaining eps)."""
    k = d["kind"]
    if k == "titles":
        now = {str(e["ratingKey"]): e.get("title", "") for e in plex("/library/metadata/%s/allLeaves" % d["rk"]).get("Metadata", [])}
        left = [e for e in d["eps"] if e["rk"] in now and is_generic(now[e["rk"]])]
        n = len(d["eps"]) - len(left)
        return (not left, ("Plex checked: the episode title is right now." if len(d["eps"]) == 1 else "Plex checked: all %d episode titles are right now." % len(d["eps"])) if not left else
                "Plex now has %d of %d titles. The rest are still \u201cEpisode N\u201d: use Sonarr's titles for those." % (n, len(d["eps"])), left)
    m = _plex_item(d["rk"])
    if not m: return (False, "Couldn't read the item from Plex.", None)
    if k == "unmatched":
        ok = m.get("guid", "").startswith("plex://")
        return (ok, "Plex checked: matched as %s (%s)." % (m.get("title"), m.get("year", "?")) if ok else "Plex still can't match it. Use Fix Match in Plex.", None)
    if k == "seasonposters":
        left = [x for x in d["seasons"] if not plex_img_ok((_plex_item(x["rk"]) or {}).get("thumb"))]
        n = len(d["seasons"]) - len(left)
        return (not left, "Plex checked: all %d season posters show now." % len(d["seasons"]) if not left else
                "%d of %d seasons have a poster now. The rest have no poster to pick from in Plex; try Refresh." % (n, len(d["seasons"])), left)
    if k == "noposter":
        ok = bool(m.get("thumb"))
        return (ok, "Plex checked: it has a poster now." if ok else "Still no poster. Plex's info for it may not have one.", None)
    if k == "year":
        ok = m.get("year") and abs(int(m["year"]) - int(d["arr_year"])) <= 1
        return (ok, "Plex checked: the year is %s now." % m.get("year") if ok else "Plex still says %s. That's what Plex's own info has; Ignore it if the film is right." % m.get("year"), None)
    return (False, "Nothing to check.", None)

def meta_fix(issue_id, how):
    with db() as c:
        row = c.execute("SELECT * FROM issues WHERE id=? AND chk='meta'", (issue_id,)).fetchone()
    if not row: return {"ok": False, "error": "That item isn't in the last scan any more. Scan again first."}
    d = json.loads(row["detail"])
    if how not in d.get("fixes", []): return {"ok": False, "error": "That fix doesn't apply here."}
    if not str(d.get("rk", "")).isdigit() or not all(e["rk"].isdigit() for e in d.get("eps", []) + d.get("seasons", [])):
        return {"ok": False, "error": "Bad item id."}
    note = "Copying %d titles from Sonarr\u2026" % len(d["eps"]) if how == "titles" else "Picking %d season posters\u2026" % len(d["seasons"]) if how == "posters" else "Asking Plex to refresh\u2026"
    with db() as c:
        c.execute("DELETE FROM done WHERE issue_id=? AND status='checking'", (issue_id,))
        done_id = c.execute("INSERT INTO done (issue_id, title, library, kept, moved, trash_ids, links, at, status, note, chk) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (issue_id, row["title"], row["library"], "", "[]", "[]", row["links"], now_iso(), "checking", note, "meta")).lastrowid
    threading.Thread(target=_meta_fix, args=(done_id, issue_id, row["title"], d, how), daemon=True).start()
    return {"ok": True, "started": True}

def _meta_fix(done_id, issue_id, title, d, how):
    status, note, left = "warn", "", None
    try:
        if how == "titles":
            n = 0
            for e in d["eps"]:
                q = urllib.parse.urlencode({"type": 4, "id": e["rk"], "title.value": e["real"], "title.locked": 1})
                plex_put("/library/sections/%s/all?%s" % (d["section"], q)); n += 1
            event("metadata", "%s: copied %d episode title%s from Sonarr into Plex (locked)." % (title, n, "" if n == 1 else "s"))
            tries = 4
        elif how == "posters":
            n = 0
            for x in d["seasons"]:
                ps = plex("/library/metadata/%s/posters" % x["rk"]).get("Metadata", [])
                if ps:
                    plex_put("/library/metadata/%s/poster?%s" % (x["rk"], urllib.parse.urlencode({"url": ps[0]["ratingKey"]}))); n += 1
            event("metadata", "%s: picked posters for %d season%s." % (title, n, "" if n == 1 else "s"))
            tries = 3
        else:
            plex_put("/library/metadata/%s/refresh" % d["rk"])
            event("metadata", "%s: asked Plex to refresh its info." % title)
            tries = 24                                # Plex refreshes in the background: give it up to ~2 min
        for _ in range(tries):
            time.sleep(5)
            ok, note, left = _meta_left(d)
            if ok: status = "ok"; break
    except Exception as e:
        note = "Couldn't finish: %s" % e
    with db() as c:
        c.execute("UPDATE done SET status=?, note=?, checked_at=? WHERE id=?", (status, note, now_iso(), done_id))
        if status == "ok":
            c.execute("DELETE FROM issues WHERE id=?", (issue_id,))
        elif left and d["kind"] == "seasonposters":
            d2 = dict(d, seasons=left, text="%d season%s still show a blank poster in Plex." % (len(left), "" if len(left) == 1 else "s"))
            c.execute("UPDATE issues SET detail=? WHERE id=?", (json.dumps(d2), issue_id))
        elif left:                                    # keep the card, but only for the titles still wrong
            d2 = dict(d, eps=left, text="%d episode%s still called \u201cEpisode N\u201d in Plex. Sonarr has the real titles." % (len(left), "" if len(left) == 1 else "s"))
            c.execute("UPDATE issues SET detail=? WHERE id=?", (json.dumps(d2), issue_id))
    log("metafix %s %s: %s" % (issue_id, how, note))

# ---------------- file health (stage 3) ----------------
BROKEN_BYTES = 5 * 1024 * 1024

def health_scan(sections, movies, series, arr_files, trashed):
    out = []
    by_path = {p: (a, m) for a, m, p, _ in arr_files}
    eps_cache = {}
    def sonarr_eps(a, sid):
        k = (a["name"], sid)
        if k not in eps_cache:
            try: eps_cache[k] = {(e["seasonNumber"], e["episodeNumber"]): e for e in arr(a, "/episode?seriesId=%d" % sid)}
            except Exception: eps_cache[k] = {}
        return eps_cache[k]
    for sec in sections:
        lib, kind, key = sec["title"], sec["type"], sec["key"]
        locs = [l["path"].rstrip("/") for l in sec.get("Location", [])]
        mine = lambda a: any(r in locs for r in a.get("roots", []))
        STATE["step"] = "Checking file health in " + lib
        shows = {}
        if kind == "show":
            for sh in plex_paged("/library/sections/%s/all" % key, type=2, includeGuids=1):
                hit = [(a, x) for a, x in series.get(str(guids(sh).get("tvdb")), []) if mine(a)]
                shows[sh["ratingKey"]] = hit[0] if hit else None
        for m in plex_paged("/library/sections/%s/all" % key, type=1 if kind == "movie" else 4, includeGuids=1):
            if kind == "movie":
                title = "%s (%s)" % (m.get("title"), m.get("year", "?"))
            else:
                sn, en = int(m.get("parentIndex") or 0), int(m.get("index") or 0)
                title = "%s S%02dE%02d" % (m.get("grandparentTitle"), sn, en)
            for md in m.get("Media", []):
                parts = [p for p in md.get("Part", []) if p.get("file")]
                if len(parts) != 1: continue                       # multi-part films: skip, lengths are per part
                f = parts[0]["file"]
                if f in trashed or not os.path.isfile(f): continue
                size, dur = os.path.getsize(f), (md.get("duration") or 0) / 60000.0
                broken = size < BROKEN_BYTES or dur < 1
                # what should it be?
                exp, ref, link = 0, None, None
                if kind == "movie":
                    a, it = by_path.get(f, (None, None))
                    if not a:
                        cand = [(a2, x) for a2, x in movies.get(str(guids(m).get("tmdb")), []) if mine(a2)]
                        a, it = cand[0] if cand else (None, None)
                    if a:
                        exp = it.get("runtime") or 0
                        ref, link = {"arr": a["name"], "kind": "movie", "id": it["id"]}, arr_link(a, it, "movie")
                else:
                    h = shows.get(m.get("grandparentRatingKey"))
                    if h:
                        a, sr = h
                        avg = sr.get("runtime") or 0
                        if broken or (dur and dur < max(avg, 15) * 0.5):   # only fetch episode runtimes when it might matter
                            e = sonarr_eps(a, sr["id"]).get((sn, en))
                            if e:
                                exp = e.get("runtime") or 0
                                ref = {"arr": a["name"], "kind": "episode", "id": e["id"], "series": sr["id"]}
                        link = arr_link(a, sr, "series")
                short = not broken and exp >= 15 and dur < exp * 0.5
                if not (broken or short): continue
                if broken:
                    text = "This file is broken: %s and %s long, so it won't play." % (human(size), ("%d second%s" % (round(dur * 60), "" if round(dur * 60) == 1 else "s")) if dur < 1 else "%d minutes" % dur)
                else:
                    text = "Only %d minutes long, but it should be about %d." % (round(dur), exp)
                if broken or dur < exp * 0.25:
                    note, sev, conf = "Most likely a failed download or a processing step that went wrong.", "bad", "sure"
                else:
                    note, sev, conf = "Could be a cut-down or partial copy, or a listing that counts two parts as one. Play a bit of it if you're not sure.", "amb", "check"
                if ref: note += " Replace moves it to the trash and asks %s to download a good copy." % ref["arr"]
                else: note += " Neither Radarr nor Sonarr manages it, so there's nothing to fetch a new copy."
                out.append(dict(id=iid("health", f), chk="health", sev=sev, title=title, library=lib,
                                detail={"kind": "broken" if broken else "short", "text": text, "note": note, "path": f, "size": size,
                                        "dur": round(dur, 1), "exp": exp, "ref": ref, "section": key, "confidence": conf,
                                        "fixes": ["replace"] if ref else []},
                                links=[plex_link(m["ratingKey"])] + ([link] if link else [])))
    return out

def health_side_files(path):
    """.edl/.ffmeta (and empty .txt) left next to a broken video with the same name."""
    folder, stem = os.path.dirname(path), os.path.splitext(os.path.basename(path))[0]
    out = []
    try: names = os.listdir(folder)
    except OSError: return out
    for n in names:
        p = os.path.join(folder, n); ext = os.path.splitext(n)[1].lower()
        if ext in SIDE_EXT and side_stem(n) == stem and os.path.isfile(p) and not (ext == ".txt" and os.path.getsize(p) > 0):
            out.append(p)
    return out

def health_fix(issue_id):
    with MOVE_LOCK:
        with db() as c:
            row = c.execute("SELECT * FROM issues WHERE id=? AND chk='health'", (issue_id,)).fetchone()
        if not row: return {"ok": False, "error": "That file isn't in the last scan any more. Scan again first."}
        d = json.loads(row["detail"]); src, ref = d["path"], d.get("ref")
        if not ref: return {"ok": False, "error": "Radarr/Sonarr doesn't manage this one, so there's nothing to fetch a new copy."}
        if not safe_media_path(src) or not os.path.isfile(src): return {"ok": False, "error": "The file isn't where it was, so nothing was moved. Scan again."}
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        moved, tids = [], []
        for p, reason in [(src, "Broken file (replaced)" if d["kind"] == "broken" else "Too short (replaced)")] + [(x, "Leftover side file") for x in health_side_files(src)]:
            dst = os.path.join(TRASH_DIR, stamp + "-" + issue_id, os.path.relpath(os.path.realpath(p), MEDIA_ROOT))
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True); size = os.path.getsize(p); move_file(p, dst)
                with db() as c:
                    tids.append(c.execute("INSERT INTO trash (issue_id, title, src, dst, size, reason, moved_at) VALUES (?,?,?,?,?,?,?)",
                                          (issue_id, row["title"], p, dst, size, reason, now_iso())).lastrowid)
                moved.append(p)
            except Exception as e:
                if p == src: return {"ok": False, "error": "Couldn't move it: %s" % e}
        plex_refresh_folder(d.get("section"), os.path.dirname(src))
        with db() as c:
            done_id = c.execute("INSERT INTO done (issue_id, title, library, kept, moved, trash_ids, links, at, status, note, chk, ref) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                (issue_id, row["title"], row["library"], "", json.dumps(moved), json.dumps(tids), row["links"], now_iso(),
                                 "checking", "Asking %s for a new copy\u2026" % ref["arr"], "health", json.dumps(ref))).lastrowid
            c.execute("DELETE FROM issues WHERE id=?", (issue_id,))
    event("health", "%s: moved the %s file to the trash and asked %s for a new copy." % (row["title"], d["kind"], ref["arr"]))
    threading.Thread(target=_health_search, args=(done_id, ref), daemon=True).start()
    return {"ok": True}

def _health_search(done_id, ref):
    status, note = "warn", ""
    try:
        a = arr_by_name(ref["arr"])
        if ref["kind"] == "movie":
            arr(a, "/command", {"name": "RescanMovie", "movieId": int(ref["id"])}, "POST")
        else:
            arr(a, "/command", {"name": "RescanSeries", "seriesId": int(ref["series"])}, "POST")
        has = True
        for _ in range(12):                         # wait for the rescan to notice the file is gone
            time.sleep(5)
            item = arr(a, "/movie/%d" % int(ref["id"])) if ref["kind"] == "movie" else arr(a, "/episode/%d" % int(ref["id"]))
            has = item.get("hasFile")
            if not has: break
        if ref["kind"] == "movie":
            arr(a, "/command", {"name": "MoviesSearch", "movieIds": [int(ref["id"])]}, "POST")
        else:
            arr(a, "/command", {"name": "EpisodeSearch", "episodeIds": [int(ref["id"])]}, "POST")
        status = "waiting"
        note = "%s is searching for a new copy. It downloads in the night window and shows up in Plex by itself." % a["name"] if not has else \
               "Asked %s to search, but it still thinks it has the old file. Its next disk scan will catch up." % a["name"]
    except Exception as e:
        note = "Moved to the trash, but couldn't ask %s to search: %s" % (ref.get("arr"), e)
    with db() as c:
        c.execute("UPDATE done SET status=?, note=?, checked_at=? WHERE id=?", (status, note, now_iso(), done_id))
    log("health search %s: %s" % (done_id, note))

# ---------------- subtitles (stage 3) ----------------
SUB_EXTS = (".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx", ".sup")
SUB_FLAGS = {"hi", "sdh", "cc", "forced", "default", "english", "eng", "en", "und", "full"}
AD_RE = re.compile(r"https?://|www\.|\.(?:com|net|org|mx|lt|ag|eu|cc|io|tv|me|to|se)\b|\byts\b|yify|opensubtitles|subscene|addic7ed|"
                   r"provided by|downloaded from|download more|your ad here|advertise|support us and become", re.I)
EP_RE = re.compile(r"[Ss](\d{1,3})[Ee](\d{1,4})")
EXTRA_RE = re.compile(r"-(?:trailer|sample|featurette|behindthescenes|deleted|interview|scene|short|other)$", re.I)

def bazarr_for(arr_name):
    return next((b for b in S.get("bazarr", []) if arr_name in (b.get("radarr"), b.get("sonarr"))), None)

def baz(b, path, method="GET", timeout=60, **q):
    return map_in(http("%s/api%s?%s" % (b["url"].rstrip("/"), path, urllib.parse.urlencode(q)), {"X-API-KEY": b["key"]}, method=method, timeout=timeout), b)

def sub_split(stem):
    """'Film (2001) [..]-GRP.en.hi' -> ('Film (2001) [..]-GRP', '.en.hi')"""
    toks, suf = stem.split("."), []
    while len(toks) > 1:
        t = toks[-1]
        if t.lower() in SUB_FLAGS or (t.isalpha() and t.islower() and 2 <= len(t) <= 3) or (t.isdigit() and len(t) <= 2):
            suf.insert(0, toks.pop())
        else: break
    return ".".join(toks), ("." + ".".join(suf)) if suf else ""

def read_text(p, limit=400000):
    with open(p, "rb") as h: b = h.read(limit)
    if b[:2] in (b"\xff\xfe", b"\xfe\xff"): return b.decode("utf-16", "replace")
    try: return b.decode("utf-8-sig")
    except UnicodeDecodeError: return b.decode("latin-1")

def advert_only(p):
    """An .srt whose every line is a website / 'provided by' advert (at most 12 cues)."""
    if not p.lower().endswith(".srt") or os.path.getsize(p) > 20000: return False
    cues = [c for c in re.split(r"\r?\n\s*\r?\n", read_text(p).strip()) if c.strip()]
    if not cues or len(cues) > 12: return False
    for c in cues:
        lines = [l for l in c.splitlines() if l.strip() and "-->" not in l and not l.strip().isdigit()]
        if lines and not all(AD_RE.search(l) for l in lines): return False
    return True

def ad_excerpt(p):
    t = re.sub(r"\s+", " ", " ".join(l.strip() for l in read_text(p, 800).splitlines() if l.strip() and "-->" not in l and not l.strip().isdigit()))
    return t if len(t) <= 60 else t[:t.rfind(" ", 0, 60)].rstrip() + "\u2026"

def subs_scan(movies, series, trashed):
    out = []
    items = []   # (arr, kind, item, folders)
    for lst in movies.values():
        for a, m in lst:
            if m.get("path") and m.get("hasFile"): items.append((a, "movie", m, [m["path"]]))
    for lst in series.values():
        for a, sr in lst:
            p = sr.get("path")
            if not p or not os.path.isdir(p): continue
            folders = [p] + [os.path.join(p, d) for d in sorted(os.listdir(p)) if os.path.isdir(os.path.join(p, d)) and not d.startswith(".")]
            items.append((a, "series", sr, folders))
    STATE["step"] = "Checking subtitles"
    for a, kind, it, folders in items:
        b = bazarr_for(a["name"])
        links = [arr_link(a, it, kind)]
        if b: links.append({"label": "Open in " + b["name"], "url": "%s/%s/%d" % (b["public"].rstrip("/"), "movies" if kind == "movie" else "series", it["id"])})
        for folder in folders:
            try: names = os.listdir(folder)
            except OSError: continue
            vids = [os.path.splitext(n)[0] for n in names if n.lower().endswith(VIDEO_EXT) and not EXTRA_RE.search(os.path.splitext(n)[0])]
            for n in sorted(names):
                stem, ext = os.path.splitext(n)
                if ext.lower() not in SUB_EXTS: continue
                p = os.path.join(folder, n)
                if p in trashed or not os.path.isfile(p): continue
                base, suf = sub_split(stem)
                ep = EP_RE.search(n)
                title = "%s (%s)" % (it.get("title"), it.get("year", "?")) if kind == "movie" else \
                        ("%s S%02dE%02d" % (it.get("title"), int(ep.group(1)), int(ep.group(2))) if ep else it.get("title"))
                ref = {"arr": a["name"], "kind": kind, "id": it["id"], "bazarr": b["name"] if b else None}
                common = dict(path=p, size=os.path.getsize(p), ref=ref)
                linked = base in vids or stem in vids
                if linked and advert_only(p):
                    out.append(dict(id=iid("subs", p), chk="subs", sev="bad", title=title, library=a["name"], links=links,
                        detail=dict(common, kind="advert", text="This subtitle file is only adverts (\u201c%s\u201d), not real dialogue. Because it exists, Bazarr thinks this one is covered and never looks for a real subtitle." % ad_excerpt(p),
                                    note="Replace moves it to the trash and asks %s for a real English subtitle." % b["name"] if b else "Remove moves it to the trash.",
                                    fixes=["replace"] if b else ["trash"], confidence="sure")))
                    continue
                if linked: continue
                if kind == "movie": cand = vids
                else: cand = [v for v in vids if ep and (EP_RE.search(v) or [None, None, None]) and EP_RE.search(v) and
                              (int(EP_RE.search(v).group(1)), int(EP_RE.search(v).group(2))) == (int(ep.group(1)), int(ep.group(2)))]
                if len(cand) == 1:
                    new = cand[0] + suf + ext
                    if os.path.exists(os.path.join(folder, new)):
                        out.append(dict(id=iid("subs", p), chk="subs", sev="att", title=title, library=a["name"], links=links,
                            detail=dict(common, kind="spare", text="A leftover subtitle for an older copy of the video. The video already has its own subtitle with the right name (%s)." % new,
                                        note="Remove moves the old one to the trash.", fixes=["trash"], confidence="sure")))
                    else:
                        out.append(dict(id=iid("subs", p), chk="subs", sev="att", title=title, library=a["name"], links=links,
                            detail=dict(common, kind="unlinked", new=new, text="Plex can't use this subtitle because its name doesn't match the video any more (the video was renamed, probably by Tdarr).",
                                        note="Rename to match renames it to %s so Plex links it." % new, fixes=["rename"], confidence="sure")))
                elif not cand:
                    out.append(dict(id=iid("subs", p), chk="subs", sev="att", title=title, library=a["name"], links=links,
                        detail=dict(common, kind="orphan", text="A subtitle with no video to go with it.", note="Remove moves it to the trash.", fixes=["trash"], confidence="sure")))
    # Bazarr's own "missing" lists: one summary card per Bazarr
    for b in S.get("bazarr", []):
        try:
            eps = baz(b, "/episodes/wanted", start=0, length=-1).get("data", [])
            mvs = baz(b, "/movies/wanted", start=0, length=-1).get("data", [])
            provs = [p["name"] for p in baz(b, "/providers").get("data", [])]
        except Exception as e:
            log("bazarr %s failed: %r" % (b["name"], e)); continue
        if not eps and not mvs: continue
        shows = {}
        for e in eps: shows[e["seriesTitle"]] = shows.get(e["seriesTitle"], 0) + 1
        top = sorted(shows.items(), key=lambda x: -x[1])[:8]
        parts = []
        if eps: parts.append("%s episode%s in %d show%s" % ("{:,}".format(len(eps)), "" if len(eps) == 1 else "s", len(shows), "" if len(shows) == 1 else "s"))
        if mvs: parts.append("%d film%s" % (len(mvs), "" if len(mvs) == 1 else "s"))
        hint = "" if "opensubtitlescom" in provs else " A free OpenSubtitles.com account (Bazarr \u203a Settings \u203a Providers) usually fills most of these gaps."
        out.append(dict(id=iid("subs-wanted", b["name"]), chk="subs", sev="att", title="Missing English subtitles", library=b["name"],
            links=[{"label": "Open " + b["name"] + " Wanted", "url": b["public"].rstrip("/") + "/wanted/series"}],
            detail=dict(kind="wanted", text="%s have no English subtitles." % " and ".join(parts),
                        note="%s already searches for these every 6 hours with %d free source%s (%s), which don't have them.%s" % (b["name"], len(provs), "" if len(provs) == 1 else "s", ", ".join(provs) or "none", hint),
                        top=[{"t": t, "n": n} for t, n in top], films=[m["title"] for m in mvs[:8]], fixes=["searchall"], ref={"bazarr": b["name"]})))
    return out

def plex_refresh_path(path):
    for sec in plex("/library/sections").get("Directory", []):
        if any(path.startswith(l["path"].rstrip("/") + "/") for l in sec.get("Location", [])):
            plex_refresh_folder(sec["key"], os.path.dirname(path))

def subs_fix(issue_id, how):
    with MOVE_LOCK:
        with db() as c:
            row = c.execute("SELECT * FROM issues WHERE id=? AND chk='subs'", (issue_id,)).fetchone()
        if not row: return {"ok": False, "error": "That one isn't in the last scan any more. Scan again first."}
        d = json.loads(row["detail"])
        if how not in d.get("fixes", []): return {"ok": False, "error": "That fix doesn't apply here."}
        ref = d.get("ref") or {}
        b = next((x for x in S.get("bazarr", []) if x["name"] == ref.get("bazarr")), None)
        moved, tids, note, status = [], [], "", "checking"
        if how == "searchall":
            if not b: return {"ok": False, "error": "Unknown Bazarr."}
            for t in ("wanted_search_missing_subtitles_series", "wanted_search_missing_subtitles_movies"):
                baz(b, "/system/tasks", "POST", taskid=t)
            note, status = "%s is searching for every missing subtitle now. It takes a while; new ones appear in Plex by themselves." % b["name"], "ok"
            event("subtitles", "Asked %s to search for all missing subtitles." % b["name"])
        else:
            src = d["path"]
            if not safe_media_path(src) or not os.path.isfile(src): return {"ok": False, "error": "The subtitle isn't where it was. Scan again."}
            if how == "rename":
                dst = os.path.join(os.path.dirname(src), d["new"])
                if os.path.dirname(os.path.realpath(dst)) != os.path.dirname(os.path.realpath(src)) or os.path.exists(dst):
                    return {"ok": False, "error": "A subtitle with that name already exists, so nothing was changed."}
                move_file(src, dst)
                note, status = "Renamed to match the video. Plex links it after its folder scan.", "ok"
                event("subtitles", "%s: renamed the subtitle to match its video." % row["title"])
            else:
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                dst = os.path.join(TRASH_DIR, stamp + "-" + issue_id, os.path.relpath(os.path.realpath(src), MEDIA_ROOT))
                os.makedirs(os.path.dirname(dst), exist_ok=True); size = os.path.getsize(src); move_file(src, dst)
                with db() as c:
                    tids.append(c.execute("INSERT INTO trash (issue_id, title, src, dst, size, reason, moved_at) VALUES (?,?,?,?,?,?,?)",
                                          (issue_id, row["title"], src, dst, size, {"advert": "Advert-only subtitle"}.get(d["kind"], "Stray subtitle"), now_iso())).lastrowid)
                moved.append(src)
                if how == "replace": note = "Moved to the trash. Asking %s for a real one\u2026" % b["name"]
                else: note, status = "Moved to the trash.", "ok"
                event("subtitles", "%s: moved the %s subtitle to the trash." % (row["title"], "advert-only" if d["kind"] == "advert" else "stray"))
            try: plex_refresh_path(src)
            except Exception as e: log("plex refresh failed: %r" % e)
        with db() as c:
            done_id = c.execute("INSERT INTO done (issue_id, title, library, kept, moved, trash_ids, links, at, status, note, chk) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                (issue_id, row["title"], row["library"], "", json.dumps(moved), json.dumps(tids), row["links"], now_iso(), status, note, "subs")).lastrowid
            c.execute("DELETE FROM issues WHERE id=?", (issue_id,))
    if how in ("rename", "trash", "replace") and b:
        threading.Thread(target=_subs_bazarr, args=(done_id, b, ref, d.get("path"), how), daemon=True).start()
    return {"ok": True}

def _subs_bazarr(done_id, b, ref, path, how):
    """Tell Bazarr the disk changed; for Replace also search for a real subtitle and report whether one arrived."""
    status, note = "ok", None
    which = ("/movies", {"radarrid": ref["id"]}) if ref["kind"] == "movie" else ("/series", {"seriesid": ref["id"]})
    try:
        baz(b, which[0], "PATCH", timeout=300, action="scan-disk", **which[1])
        if how == "replace":
            folder = os.path.dirname(path); vid_base = sub_split(os.path.splitext(os.path.basename(path))[0])[0]
            before = set(os.listdir(folder))
            baz(b, which[0], "PATCH", timeout=900, action="search-missing", **which[1])
            new = [n for n in os.listdir(folder) if n not in before and n.startswith(vid_base) and n.lower().endswith(SUB_EXTS)]
            item = None
            try:
                rows = baz(b, "/movies", **{"radarrid[]": ref["id"]}).get("data", []) if ref["kind"] == "movie" else \
                       baz(b, "/episodes", **{"seriesid[]": ref["id"]}).get("data", [])
                item = next((r for r in rows if os.path.splitext(os.path.basename(r.get("path") or ""))[0] == vid_base), rows[0] if ref["kind"] == "movie" and rows else None)
            except Exception as e:
                log("bazarr item check failed: %r" % e)
            emb = [x for x in (item or {}).get("subtitles", []) if x.get("code2") == "en" and x.get("embedded_track_id") is not None]
            if new:
                status, note = "ok", "%s found a real subtitle: %s" % (b["name"], new[0])
            elif emb:
                status, note = "ok", "Done: the video already has English subtitles built in, so nothing else is needed."
            elif item is not None and not item.get("missing_subtitles"):
                status, note = "ok", "Done: %s says English subtitles are covered." % b["name"]
            else:
                status, note = "warn", "%s couldn't find a real one yet. It keeps trying every 6 hours." % b["name"]
    except Exception as e:
        status, note = ("warn", "Moved to the trash, but %s didn't answer: %s" % (b["name"], e)) if how == "replace" else ("ok", None)
    if note:
        with db() as c: c.execute("UPDATE done SET status=?, note=?, checked_at=? WHERE id=?", (status, note, now_iso(), done_id))

# ---------------- virus scan (stage 3) ----------------
CLAMD = (os.environ.get("LD_CLAMD_HOST", "127.0.0.1"), int(os.environ.get("LD_CLAMD_PORT", "3310")))
CLAM_ROOT = os.environ.get("LD_CLAM_ROOT", "/scandir")          # where clamd sees MEDIA_ROOT
CLAM_MAX = 100 * 1024 * 1024                                     # clamd MaxFileSize: bigger files aren't scanned anyway
RISKY_EXT = (".exe", ".scr", ".bat", ".cmd", ".com", ".pif", ".msi", ".lnk", ".vbs", ".vbe", ".js", ".jse", ".wsf",
             ".ps1", ".jar", ".apk", ".dll", ".hta", ".reg", ".url", ".desktop")
ARCHIVE_EXT = (".zip", ".rar", ".7z", ".r00", ".r01", ".tar", ".gz", ".iso")
SKIP_TOP = ("downloads", "usenet", "torrents", "incomplete")

def kv_get(k, default=None):
    with db() as c:
        r = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return json.loads(r["v"]) if r else default

def kv_set(k, v):
    with db() as c: c.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (k, json.dumps(v)))

def clamd(cmd, timeout=120):
    import socket
    with socket.create_connection(CLAMD, timeout=10) as s:
        s.settimeout(timeout)
        s.sendall(b"z" + cmd.encode() + b"\0")
        buf = b""
        while True:
            chunk = s.recv(65536)
            if not chunk: break
            buf += chunk
            if cmd.startswith(("SCAN", "VERSION")) and buf.endswith(b"\0"): break
    return [x.decode("utf-8", "replace") for x in buf.split(b"\0") if x]

def to_clam(p): return CLAM_ROOT + p[len(MEDIA_ROOT):] if p.startswith(MEDIA_ROOT + "/") or p == MEDIA_ROOT else p
def from_clam(p): return MEDIA_ROOT + p[len(CLAM_ROOT):] if p.startswith(CLAM_ROOT) else p

_CLAM_CACHE = {"t": 0, "v": None}
def clam_status(cached=False):
    if cached and _CLAM_CACHE["v"] and time.time() - _CLAM_CACHE["t"] < 300: return _CLAM_CACHE["v"]
    _CLAM_CACHE.update(t=time.time(), v=_clam_status())
    return _CLAM_CACHE["v"]

def _clam_status():
    try:
        v = clamd("VERSION", 5)[0]                 # ClamAV 1.5.4/28182/Fri Nov 13 06:24:28 2026
        parts = v.split("/")
        return {"ok": True, "version": parts[0], "db": parts[1] if len(parts) > 1 else "", "db_date": parts[2] if len(parts) > 2 else ""}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def magic_kind(p):
    try:
        with open(p, "rb") as h: b = h.read(8)
    except OSError: return None
    if b[:2] == b"MZ": return "a Windows program"
    if b[:4] == b"\x7fELF": return "a Linux program"
    if b[:4] == b"PK\x03\x04": return "a zip archive"
    if b[:4] == b"Rar!": return "a RAR archive"
    if b[:6] == b"7z\xbc\xaf\x27\x1c": return "a 7-Zip archive"
    return None

def owner_refs(p, movies, series):
    for lst in movies.values():
        for a, m in lst:
            if m.get("path") and p.startswith(m["path"].rstrip("/") + "/"): return [{"arr": a["name"], "kind": "movie", "id": m["id"]}], m.get("title")
    for lst in series.values():
        for a, sr in lst:
            if sr.get("path") and p.startswith(sr["path"].rstrip("/") + "/"): return [{"arr": a["name"], "kind": "series", "id": sr["id"]}], sr.get("title")
    return [], None

def virus_scan(sections, movies, series, trashed, started):
    out, roots = [], set()
    for sec in sections:
        for l in sec.get("Location", []): roots.add(l["path"].rstrip("/"))
    last = kv_get("virus_last_incremental")
    cutoff = (datetime.fromisoformat(last["at"]) - timedelta(hours=1)).timestamp() if last else time.time() - 8 * 86400
    changed, n_files = [], 0
    def add(kind, p, sev, text, note):
        refs, owner = owner_refs(p, movies, series)
        out.append(dict(id=iid("virus", kind, p), chk="virus", sev=sev, title=owner or os.path.basename(p), library=os.path.relpath(p, MEDIA_ROOT).split("/")[0],
                        detail={"kind": kind, "text": text, "note": note, "path": p, "size": os.path.getsize(p), "refs": refs, "fixes": ["remove"]},
                        links=[]))
    STATE["step"] = "Checking for viruses and fake files"
    for root in sorted(roots):
        if os.path.relpath(root, MEDIA_ROOT).split("/")[0] in SKIP_TOP: continue
        for dp, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for n in files:
                p = os.path.join(dp, n)
                if p in trashed: continue
                n_files += 1
                ext = os.path.splitext(n)[1].lower()
                try: st = os.stat(p)
                except OSError: continue
                if ext in RISKY_EXT:
                    add("program", p, "bad", "A program or script file (%s) in your media library. Films and shows never need one; fake releases use them to get a program onto your computer." % ext,
                        "Remove moves it to the trash. Don't open it.")
                elif ext in ARCHIVE_EXT:
                    add("archive", p, "att", "A leftover archive (%s, %s) in your media library. Plex can't play it, and archives can hide programs." % (ext, human(st.st_size)),
                        "Remove moves it to the trash. If it was meant to be unpacked, the download didn't finish properly.")
                elif ext in VIDEO_EXT:
                    mk = magic_kind(p)
                    if mk:
                        add("fakevideo", p, "bad", "Named like a video, but it's really %s. A fake release." % mk,
                            "Remove moves it to the trash and tells Radarr/Sonarr so they can fetch a real copy. Don't open it.")
                if st.st_mtime > cutoff and st.st_size < CLAM_MAX: changed.append(p)
    # ClamAV on everything new or changed since the last check
    checked, errors, found = 0, 0, []
    status = clam_status()
    if status["ok"]:
        STATE["step"] = "ClamAV: checking %d new files" % len(changed)
        for p in changed:
            try:
                r = clamd("SCAN " + to_clam(p))[0]
                checked += 1
                if r.endswith("FOUND"): found.append((p, r.rsplit(": ", 1)[-1][:-6].strip()))
                elif r.endswith("ERROR"): errors += 1
            except Exception as e:
                errors += 1; log("clamd scan failed for %s: %r" % (p, e))
        # anything a full scan found earlier stays listed while the file is still there
        for p, sig in kv_get("virus_full_found", []):
            if os.path.isfile(p) and p not in trashed and p not in [x for x, _ in found]: found.append((p, sig))
        kv_set("virus_last_incremental", {"at": started, "checked": checked, "errors": errors, "files": n_files, "found": len(found)})
    for p, sig in found:
        add("infected", p, "bad", "ClamAV found %s in this file." % sig, "Remove moves it to the trash (it can't run from there). Don't open it.")
    return out

def virus_full():
    """Full ClamAV scan of the whole library (same as the weekly job), in the background."""
    if not VIRUS_LOCK.acquire(blocking=False): return {"ok": False, "error": "A full scan is already running."}
    def run():
        t0 = time.time(); kv_set("virus_full_running", {"at": now_iso()})
        found, errors = [], 0
        try:
            for line in clamd("MULTISCAN " + CLAM_ROOT, timeout=6 * 3600):
                if line.endswith("FOUND"):
                    path, sig = line[:-6].rsplit(": ", 1)
                    p = from_clam(path)
                    if "/.quarantine/" not in p and "/.library-doctor-trash/" not in p: found.append((p, sig.strip()))
                elif line.endswith("ERROR"): errors += 1
            res = {"at": now_iso(), "secs": int(time.time() - t0), "infected": len(found), "errors": errors, "ok": True}
            kv_set("virus_full_found", found)
            event("virus", "Full ClamAV scan finished: %d infected, %d unreadable, %d min." % (len(found), errors, res["secs"] // 60))
        except Exception as e:
            res = {"at": now_iso(), "secs": int(time.time() - t0), "ok": False, "error": str(e)}
            log("full virus scan failed: %r" % e)
        kv_set("virus_full_last", res); kv_set("virus_full_running", None)
        VIRUS_LOCK.release()
        if found: threading.Thread(target=scan, daemon=True).start()     # list them straight away
    threading.Thread(target=run, daemon=True).start()
    return {"ok": True}

def virus_fix(issue_id):
    with MOVE_LOCK:
        with db() as c:
            row = c.execute("SELECT * FROM issues WHERE id=? AND chk='virus'", (issue_id,)).fetchone()
        if not row: return {"ok": False, "error": "That file isn't in the last scan any more. Scan again first."}
        d = json.loads(row["detail"]); src = d["path"]
        if not safe_media_path(src) or not os.path.isfile(src): return {"ok": False, "error": "The file isn't where it was. Scan again."}
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dst = os.path.join(TRASH_DIR, stamp + "-" + issue_id, os.path.relpath(os.path.realpath(src), MEDIA_ROOT))
        os.makedirs(os.path.dirname(dst), exist_ok=True); size = os.path.getsize(src); move_file(src, dst)
        try: os.chmod(dst, 0o640)                    # never executable in the trash
        except OSError: pass
        reason = {"infected": "Virus (ClamAV)", "program": "Program file", "archive": "Leftover archive", "fakevideo": "Fake video"}.get(d["kind"], "Virus scan")
        with db() as c:
            tid = c.execute("INSERT INTO trash (issue_id, title, src, dst, size, reason, moved_at) VALUES (?,?,?,?,?,?,?)",
                            (issue_id, row["title"], src, dst, size, reason, now_iso())).lastrowid
            c.execute("INSERT INTO done (issue_id, title, library, kept, moved, trash_ids, links, at, status, note, chk) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (issue_id, row["title"], row["library"], "", json.dumps([src]), json.dumps([tid]), row["links"], now_iso(), "ok",
                       "Moved to the trash." + (" %s was told to rescan." % ", ".join(arr_rescan(d.get("refs")))) if d.get("refs") else "Moved to the trash.", "virus"))
            c.execute("DELETE FROM issues WHERE id=?", (issue_id,))
        if d["kind"] == "infected":
            kv_set("virus_full_found", [x for x in kv_get("virus_full_found", []) if x[0] != src])
    try: plex_refresh_path(src)
    except Exception: pass
    event("virus", "%s: moved %s to the trash (%s)." % (row["title"], os.path.basename(src), reason))
    return {"ok": True}

VIRUS_LOCK = threading.Lock()

# ---------------- follow-up after Replace: did a good copy arrive? ----------------
FOLLOW_DAYS = int(os.environ.get("LD_FOLLOW_DAYS", "3"))

def _derive_ref(path):
    """Old Replace rows have no ref: find the Sonarr episode / Radarr film from the file path."""
    for a in S.get("arrs", []):
        try:
            if a["kind"] == "radarr":
                for m in arr(a, "/movie"):
                    if m.get("path") and path.startswith(m["path"].rstrip("/") + "/"): return {"arr": a["name"], "kind": "movie", "id": m["id"]}
            else:
                for sr in arr(a, "/series"):
                    if sr.get("path") and path.startswith(sr["path"].rstrip("/") + "/"):
                        ep = EP_RE.search(os.path.basename(path))
                        if not ep: return None
                        for e in arr(a, "/episode?seriesId=%d" % sr["id"]):
                            if (e["seasonNumber"], e["episodeNumber"]) == (int(ep.group(1)), int(ep.group(2))):
                                return {"arr": a["name"], "kind": "episode", "id": e["id"], "series": sr["id"]}
        except Exception as e:
            log("derive ref via %s failed: %r" % (a["name"], e))
    return None

def followup():
    """For each Replace still waiting: has Radarr/Sonarr got a new copy yet? Warn after FOLLOW_DAYS."""
    with db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM done WHERE chk='health' AND status IN ('waiting','checking') AND cleared=0")]
    for r in rows:
        try:
            moved = json.loads(r["moved"] or "[]"); old = moved[0] if moved else ""
            ref = json.loads(r["ref"]) if r.get("ref") else _derive_ref(old)
            if not ref:
                with db() as c: c.execute("UPDATE done SET status='warn', note=?, checked_at=? WHERE id=?", ("Couldn't find it in Radarr or Sonarr to check for a new copy.", now_iso(), r["id"]))
                continue
            if not r.get("ref"):
                with db() as c: c.execute("UPDATE done SET ref=? WHERE id=?", (json.dumps(ref), r["id"]))
            a = arr_by_name(ref["arr"]); f = None
            if ref["kind"] == "movie":
                m = arr(a, "/movie/%d" % int(ref["id"]))
                f = m.get("movieFile") if m.get("hasFile") else None
            else:
                e = arr(a, "/episode/%d" % int(ref["id"]))
                if e.get("hasFile") and e.get("episodeFileId"): f = arr(a, "/episodefile/%d" % int(e["episodeFileId"]))
            age = (datetime.now() - datetime.fromisoformat(r["at"])).total_seconds() / 86400
            if f and f.get("path") and f["path"] != old and os.path.exists(f["path"]):
                size = f.get("size") or 0
                q = ((f.get("quality") or {}).get("quality") or {}).get("name", "")
                if size < BROKEN_BYTES:
                    st, note = "warn", "A new copy arrived, but it's only %s, so it looks broken too: %s" % (human(size), os.path.basename(f["path"]))
                else:
                    st, note = "ok", "\u2713 New copy arrived: %s (%s%s)." % (os.path.basename(f["path"]), q + ", " if q else "", human(size))
                with db() as c: c.execute("UPDATE done SET status=?, note=?, checked_at=? WHERE id=?", (st, note, now_iso(), r["id"]))
                event("health", "%s: %s" % (r["title"], note))
            elif f and f.get("path") == old and not os.path.exists(old):
                # the arr hasn't noticed the old file is gone yet: nudge it once more
                if ref["kind"] == "movie": arr(a, "/command", {"name": "RescanMovie", "movieId": int(ref["id"])}, "POST")
                else: arr(a, "/command", {"name": "RescanSeries", "seriesId": int(ref["series"])}, "POST")
                with db() as c: c.execute("UPDATE done SET status='waiting', note=?, checked_at=? WHERE id=?",
                                          ("%s still listed the old file, so it was asked to rescan. It searches again after that." % a["name"], now_iso(), r["id"]))
            elif age > FOLLOW_DAYS:
                with db() as c: c.execute("UPDATE done SET status='warn', note=?, checked_at=? WHERE id=?",
                                          ("No new copy after %d days. %s can't find one; try Interactive Search on its page." % (FOLLOW_DAYS, a["name"]), now_iso(), r["id"]))
                event("health", "%s: still no new copy after %d days." % (r["title"], FOLLOW_DAYS))
            else:
                with db() as c: c.execute("UPDATE done SET status='waiting', note=?, checked_at=? WHERE id=?",
                                          ("Waiting for %s to download a new copy (usually overnight). Checked %s." % (a["name"], datetime.now().strftime("%a %H:%M")), now_iso(), r["id"]))
        except Exception as e:
            log("followup %s failed: %r" % (r["id"], e))
    smaller_followup()
    tdarr_followup()
    try: search_followup()
    except Exception as e: log("search followup failed: %r" % e)

# ---------------- Gotify: one message after the nightly scan, only when there's news ----------------
def gotify_send(title, message, priority=5):
    g = S.get("gotify") or {}
    if not g.get("token"): return {"ok": False, "error": "Gotify isn't set up (no token in secrets.json)."}
    extras = {"client::display": {"contentType": "text/markdown"}}
    if g.get("click"): extras["client::notification"] = {"click": {"url": g["click"]}}
    try:
        http(g["url"].rstrip("/") + "/message", {"X-Gotify-Key": g["token"]}, data={"title": title, "message": message, "priority": priority, "extras": extras}, method="POST", timeout=20)
        return {"ok": True}
    except Exception as e:
        log("gotify send failed: %r" % e); return {"ok": False, "error": str(e)}

# ---------------- more notification channels (v0.1.3) ----------------
# Besides Gotify: Discord (webhook), Telegram (bot), ntfy, a plain JSON webhook and email. Standard library only.
# notify() sends one message to every channel that's set up; it's "ok" if at least one got it.
NOTIFY_KINDS = ("discord", "telegram", "ntfy", "webhook", "email")
NOTIFY_DEFAULT_URL = {"discord": "https://discord.com", "telegram": "https://api.telegram.org"}
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")

def _plain(t): return _MD_BOLD.sub(r"\1", t or "")

def notify_one(kind, e, title, message, priority=5):
    """Send to one channel config e. Returns {"ok", "error"?}. Raises nothing."""
    try:
        if kind == "gotify":
            g = e
            extras = {"client::display": {"contentType": "text/markdown"}}
            if g.get("click"): extras["client::notification"] = {"click": {"url": g["click"]}}
            http(g["url"].rstrip("/") + "/message", {"X-Gotify-Key": g.get("token", "")}, data={"title": title, "message": message, "priority": priority, "extras": extras}, method="POST", timeout=20)
        elif kind == "discord":
            hook = e.get("token", "")
            if not hook.startswith("https://"): return {"ok": False, "error": "Paste the whole Discord webhook URL (Channel settings → Integrations → Webhooks)."}
            text = ("**%s**\n%s" % (title, message))[:1990]
            http(hook, {"User-Agent": "LibraryDoctor"}, data={"content": text, "username": "Library Doctor"}, method="POST", timeout=20)
        elif kind == "telegram":
            if not e.get("token") or not e.get("chat"): return {"ok": False, "error": "Fill in the bot token and the chat ID."}
            http((e.get("url") or NOTIFY_DEFAULT_URL["telegram"]).rstrip("/") + "/bot%s/sendMessage" % e["token"], {},
                 data={"chat_id": e["chat"], "text": ("%s\n\n%s" % (title, _plain(message)))[:4000], "disable_web_page_preview": True}, method="POST", timeout=20)
        elif kind == "ntfy":
            if not e.get("topic"): return {"ok": False, "error": "Fill in the topic."}
            h = {"Title": title.encode("ascii", "replace").decode(), "Priority": str(min(5, max(1, priority // 2 + 1))), "Markdown": "yes", "Content-Type": "text/plain; charset=utf-8"}
            if e.get("token"): h["Authorization"] = "Bearer " + e["token"]
            req = urllib.request.Request(e["url"].rstrip("/") + "/" + urllib.parse.quote(e["topic"]), data=message.encode("utf-8"), headers=h, method="POST")
            urllib.request.urlopen(req, timeout=20).read()
        elif kind == "webhook":
            h = {"User-Agent": "LibraryDoctor"}
            if e.get("token"): h["Authorization"] = "Bearer " + e["token"]
            http(e["url"], h, data={"title": title, "message": message, "text": _plain(message), "priority": priority, "source": "library-doctor"}, method="POST", timeout=20)
        elif kind == "email":
            import smtplib, ssl as _ssl
            from email.message import EmailMessage
            host, _, port = (e.get("url") or "").replace("smtp://", "").partition(":")
            mode = e.get("tls") or "starttls"
            port = int(port or (465 if mode == "ssl" else 587 if mode == "starttls" else 25))
            to = [x.strip() for x in (e.get("to") or "").replace(";", ",").split(",") if x.strip()]
            if not host or not to: return {"ok": False, "error": "Fill in the mail server and who to send to."}
            msg = EmailMessage(); msg["Subject"] = title; msg["From"] = e.get("from") or e.get("user") or "library-doctor@localhost"; msg["To"] = ", ".join(to)
            msg.set_content(_plain(message))
            ctx = _ssl.create_default_context()
            srv = smtplib.SMTP_SSL(host, port, timeout=20, context=ctx) if mode == "ssl" else smtplib.SMTP(host, port, timeout=20)
            try:
                if mode == "starttls": srv.starttls(context=ctx)
                if e.get("user"): srv.login(e["user"], e.get("token", ""))
                srv.send_message(msg)
            finally:
                try: srv.quit()
                except Exception: pass
        else:
            return {"ok": False, "error": "Unknown channel."}
        return {"ok": True}
    except urllib.error.HTTPError as x:
        return {"ok": False, "error": "%s said %d %s." % (kind.title(), x.code, x.reason)}
    except Exception as x:
        return {"ok": False, "error": "%s: %s" % (kind.title(), getattr(x, "reason", x))}

def notify_channels():
    out = []
    g = S.get("gotify") or {}
    if g.get("token") and g.get("url"): out.append(("gotify", g))
    for k in NOTIFY_KINDS:
        e = S.get(k)
        if isinstance(e, dict) and e.get("url"): out.append((k, e))
    return out

def notify(title, message, priority=5):
    chans = notify_channels()
    if not chans: return {"ok": False, "error": "No notification channel is set up (Settings → Optional)."}
    res = [(k, notify_one(k, e, title, message, priority)) for k, e in chans]
    for k, r in res:
        if not r.get("ok"): log("notify %s failed: %s" % (k, r.get("error")))
    ok = [k for k, r in res if r.get("ok")]
    return {"ok": bool(ok), "sent": ok, "error": "; ".join(r["error"] for k, r in res if not r.get("ok"))}

CHK_NAMES = {"missing": "missing file", "dupes": "duplicate", "meta": "metadata gap", "health": "broken file", "subs": "subtitle problem", "virus": "virus / fake file", "coming": "Coming Soon problem", "quality": "quality/space item", "unwatched": "unwatched list", "queue": "download problem"}

def nightly_summary():
    last = kv_get("gotify_last_summary") or (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")
    with db() as c:
        sc = c.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 1").fetchone()
        ign = hidden_ids(c)
        fresh = [dict(r) for r in c.execute("SELECT id, chk, title, library, detail FROM issues WHERE first_seen > ?", (last,)) if r["id"] not in ign]
        # v31: an issue that already has a Done row (a fix was tried, the scan found it again) isn't "new"
        tried = {r["issue_id"] for r in c.execute("SELECT DISTINCT issue_id FROM done WHERE issue_id IS NOT NULL")}
        new = [i for i in fresh if i["id"] not in tried]
        back = [i for i in fresh if i["id"] in tried]
        allopen = [r["chk"] for r in c.execute("SELECT id, chk FROM issues") if r["id"] not in ign]
        done = [dict(r) for r in c.execute("SELECT title, status, note FROM done WHERE chk='health' AND checked_at > ? AND status IN ('ok','warn') AND (note LIKE '\u2713%' OR note LIKE 'No new copy%' OR note LIKE 'A new copy arrived%')", (last,))]
        trash = [dict(r) for r in c.execute("SELECT size, moved_at FROM trash WHERE restored_at IS NULL AND purged_at IS NULL")]
        smaller = [dict(r) for r in c.execute("SELECT title FROM done WHERE chk='quality' AND checked_at > ? AND (note LIKE '\u2713 Smaller copy%' OR note LIKE '\u2713 Tdarr converted%')", (last,))]
    soon = [t for t in trash if datetime.fromisoformat(t["moved_at"]) + timedelta(days=TRASH_DAYS) < datetime.now() + timedelta(days=1)]
    lines, prio = [], 4
    if sc and not sc["ok"]:
        lines.append("**The nightly scan failed:** %s" % (sc["note"] or "unknown error")); prio = 6
    if new:
        by = {}
        for i in new: by[i["chk"]] = by.get(i["chk"], 0) + 1
        lines.append("**New to look at:** " + ", ".join("%d %s%s" % (n, CHK_NAMES.get(k, k), "" if n == 1 else "s") for k, n in by.items()))
        if by.get("virus"): prio = 8
        elif by.get("missing") or by.get("health"): prio = max(prio, 5)
    for i in new:                                         # v31: say what Tdarr failed on, not just "1 quality/space item"
        try: d = json.loads(i.get("detail") or "{}")
        except Exception: d = {}
        if d.get("kind") == "tdarrfail":
            lines.append("**%s** (%s): %s" % (i["title"], i.get("library") or "Tdarr", d.get("text", "")))
            prio = max(prio, 5)
    if back:
        lines.append("**Still there after a fix:** " + ", ".join(i["title"] for i in back[:6]) + (" and %d more" % (len(back) - 6) if len(back) > 6 else ""))
    arrived = [d for d in done if d["note"].startswith("\u2713")]
    failed = [d for d in done if not d["note"].startswith("\u2713")]
    if arrived: lines.append("**Replaced and re-downloaded:** " + ", ".join(d["title"] for d in arrived[:6]) + (" and %d more" % (len(arrived) - 6) if len(arrived) > 6 else ""))
    if failed: lines.append("**Still no good copy:** " + ", ".join(d["title"] for d in failed[:6]) + (" and %d more" % (len(failed) - 6) if len(failed) > 6 else ""))
    if smaller: lines.append("**Smaller now:** " + ", ".join(x["title"] for x in smaller[:6]))
    if soon: lines.append("**Trash:** %d item%s (%s) delete%s for good in the next 24 hours." % (len(soon), "" if len(soon) == 1 else "s", human(sum(t["size"] or 0 for t in soon)), "s" if len(soon) == 1 else ""))
    if not lines:
        log("nightly summary: nothing new, no message sent"); kv_set("gotify_last_summary", now_iso()); return {"ok": True, "sent": False}
    lines.append("Open now: %d to look at \u00b7 trash %d item%s (%s)" % (len(allopen), len(trash), "" if len(trash) == 1 else "s", human(sum(t["size"] or 0 for t in trash))))
    title = "Library Doctor: " + ("%d new to look at" % len(new) if new else "%d replaced file%s arrived" % (len(arrived), "" if len(arrived) == 1 else "s") if arrived else "nightly check")
    r = notify(title, "\n\n".join(lines), prio)
    if r.get("ok"): kv_set("gotify_last_summary", now_iso()); log("nightly summary sent: %s" % title)
    return dict(r, sent=r.get("ok"))

def nightly():
    last, last_follow, last_tdarr, last_queue = None, None, None, None
    while True:
        now = datetime.now()
        if not configured(): time.sleep(30); continue          # setup mode: nothing to do until Settings are saved
        sh, sm, sdays = sched()
        if now.hour == sh and now.minute >= sm and now.weekday() in sdays and last != now.date():
            last = now.date()
            try: purge(TRASH_DAYS)
            except Exception as e: log("purge failed: %r" % e)
            try: followup()
            except Exception as e: log("followup failed: %r" % e)
            scan()
            try: snooze_due()
            except Exception as e: log("snooze re-search failed: %r" % e)
            try: nightly_summary()
            except Exception as e: log("nightly summary failed: %r" % e)
        elif now.minute == 17 and last_follow != now.hour:     # hourly: has a replaced file arrived?
            last_follow = now.hour
            try: followup()
            except Exception as e: log("followup failed: %r" % e)
        elif now.minute == 37 and last_queue != now.hour:      # v33: Downloads cards hourly; 07:37 (night window over) tells Gotify
            last_queue = now.hour
            try: queue_refresh(notify=(now.hour == QUEUE_NOTIFY_HOUR))
            except Exception as e: log("queue refresh failed: %r" % e)
        elif now.minute % 10 == 7 and last_tdarr != (now.hour, now.minute):   # v23: every 10 min while Tdarr has files of ours
            last_tdarr = (now.hour, now.minute)
            try:
                if tdarr_pending(): tdarr_followup()
            except Exception as e: log("tdarr followup failed: %r" % e)
        time.sleep(60)

# ---------------- Coming Soon placeholders (v17) ----------------
# Agregarr makes a placeholder per upcoming film/show in /media/placeholder/<lib>/ so it shows in Plex. Each should be the
# real YouTube trailer; if the download fails Agregarr drops in a generic 30 s clip. Library Doctor checks:
#   generic   - still the generic clip -> "Pick trailer": top YouTube results, you choose, it downloads over the clip in place
#   wronglib  - placeholder in a library whose Radarr/Sonarr doesn't have it, while another one does (collection set to All Servers / linked)
#   stale     - Plex lists a placeholder whose file is gone (Plex keeps items from an EMPTY library folder: disk-dropped-off safety)
#   arrived   - the real film/show arrived over a day ago but the placeholder is still there -> Remove (to the trash)
#   ytdlp     - the yt-dlp Agregarr uses (its config/bin, mounted here as AG_BIN) is old -> "Update yt-dlp" (GitHub release, sha256 checked)
AG_BIN = os.environ.get("LD_AGREGARR_BIN", "/agregarr-bin")
YTDLP = os.path.join(AG_BIN, "yt-dlp")
GENERIC_SIZE = 6200828                                    # Agregarr's built-in fallback clip
YTDLP_OLD_DAYS = int(os.environ.get("LD_YTDLP_OLD_DAYS", "60"))
TRAILER_TMP = os.path.join(PLACEHOLDER_ROOT, ".library-doctor-tmp")   # dot folder: Plex and the scans skip it; same dataset -> atomic replace
TRAILER_FMT = "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]"   # same as Agregarr
TRAILER_LOCK, YTDLP_LOCK = threading.Lock(), threading.Lock()
VID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
YT_ENV = {"HOME": "/tmp", "DENO_DIR": "/tmp/deno", "XDG_CACHE_HOME": "/tmp/cache"}
_YT = {"mtime": None, "version": None, "latest": None, "latest_t": 0.0}

def ytdlp(args, timeout=120, exe=None):
    return subprocess.run(["python3", exe or YTDLP, "--no-update"] + args, capture_output=True, text=True,
                          timeout=timeout, env=dict(os.environ, **YT_ENV))

def _ver_days(v):
    try: return (datetime.now() - datetime.strptime(".".join(v.split(".")[:3]), "%Y.%m.%d")).days
    except Exception: return None

def ytdlp_status(refresh=False):
    try: mt = os.path.getmtime(YTDLP)
    except OSError: return {"ok": False, "error": "There's no yt-dlp in Agregarr's config/bin folder, so Agregarr uses the old one inside its image.",
                            "writable": os.access(AG_BIN, os.W_OK)}
    if _YT["mtime"] != mt:
        try:
            r = ytdlp(["--version"], timeout=60)
            v = r.stdout.strip().splitlines()[-1] if r.returncode == 0 and r.stdout.strip() else None
        except Exception: v = None
        _YT.update(mtime=mt, version=v)
    v = _YT["version"]
    if not v: return {"ok": False, "error": "yt-dlp is in Agregarr's bin folder but won't run.", "writable": os.access(AG_BIN, os.W_OK)}
    if refresh and time.time() - _YT["latest_t"] > 6 * 3600:
        _YT["latest_t"] = time.time()
        try: _YT["latest"] = http("https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest", {"User-Agent": "library-doctor"}, timeout=20).get("tag_name")
        except Exception as e: log("yt-dlp latest check failed: %r" % e)
    lt = _YT["latest"]
    return {"ok": True, "version": v, "days": _ver_days(v), "latest": lt, "newer": bool(lt and lt > v),
            "writable": os.access(AG_BIN, os.W_OK), "old_days": YTDLP_OLD_DAYS}

def ytdlp_update():
    if not YTDLP_LOCK.acquire(blocking=False): return {"ok": False, "error": "An update is already running."}
    try:
        if not os.access(AG_BIN, os.W_OK): return {"ok": False, "error": "Library Doctor can't write to Agregarr's bin folder."}
        old = ytdlp_status().get("version")
        def get(name):
            req = urllib.request.Request("https://github.com/yt-dlp/yt-dlp/releases/latest/download/" + name, headers={"User-Agent": "library-doctor"})
            with urllib.request.urlopen(req, timeout=180) as r: return r.read()
        want = next((l.split()[0] for l in get("SHA2-256SUMS").decode().splitlines() if l.split()[-1:] == ["yt-dlp"]), None)
        if not want: return {"ok": False, "error": "yt-dlp wasn't in the release's checksum list, so nothing was changed."}
        blob = get("yt-dlp")
        if hashlib.sha256(blob).hexdigest() != want: return {"ok": False, "error": "The download didn't match its checksum, so nothing was changed."}
        tmp = os.path.join(AG_BIN, ".yt-dlp.new")
        with open(tmp, "wb") as f: f.write(blob)
        os.chmod(tmp, 0o755)
        r = ytdlp(["--version"], timeout=60, exe=tmp)
        new = r.stdout.strip().splitlines()[-1] if r.returncode == 0 and r.stdout.strip() else ""
        if not new:
            os.unlink(tmp); return {"ok": False, "error": "The new yt-dlp wouldn't run, so the old one was kept."}
        if os.path.exists(YTDLP): shutil.copy2(YTDLP, os.path.join(AG_BIN, "yt-dlp.prev"))
        os.replace(tmp, YTDLP)
        _YT.update(mtime=None, latest=new, latest_t=time.time())
        event("coming", "Updated yt-dlp %s → %s (Agregarr uses it for Coming Soon trailers)." % (old or "?", new))
        return {"ok": True, "old": old, "new": new}
    except Exception as e:
        return {"ok": False, "error": "Update failed: %s" % e}
    finally:
        YTDLP_LOCK.release()

def _ph_root(f, ph_locs):
    return next((l for l in ph_locs if f.startswith(l + "/")), None)

def _real_age_days(kind, have):
    """Days since the real copy arrived (Radarr movieFile.dateAdded / newest Sonarr episode file), None if none."""
    ages = []
    for a, it in have:
        try:
            if kind == "movie":
                mf = it.get("movieFile") or {}
                if it.get("hasFile") and mf.get("path") and os.path.exists(mf["path"]):
                    ages.append(mf.get("dateAdded"))
            elif ((it.get("statistics") or {}).get("episodeFileCount") or 0) > 0:
                ages += [x.get("dateAdded") for x in arr(a, "/episodefile?seriesId=%d" % it["id"])]
        except Exception as e:
            log("arrived check %s: %r" % (a["name"], e))
    ages = [datetime.fromisoformat(x.replace("Z", "+00:00")).replace(tzinfo=None) for x in ages if x]
    return (datetime.utcnow() - min(ages)).total_seconds() / 86400 if ages else None

def _coming_item(sec, ph_locs, owners, rk, title, name, year, f, ext, real_files, pool):
    lib, kind, key = sec["title"], sec["type"], sec["key"]
    links = [plex_link(rk)] if rk else []
    root = _ph_root(f, ph_locs) or os.path.dirname(f)
    base_d = {"path": f, "section": key, "root": root}
    mk = lambda k, sev, text, note, fixes, **x: dict(id=iid("coming", k, lib, f), chk="coming", sev=sev, title=title, library=lib,
                                                     detail=dict(base_d, kind=k, text=text, note=note, fixes=fixes, **x), links=links)
    if not os.path.exists(f):
        try: empty = not [n for n in os.listdir(root) if not n.startswith(".")]
        except OSError: empty = True
        if empty:
            return [mk("stale", "att", "Plex still lists this Coming Soon placeholder, but its file is gone.",
                       "Plex never removes anything from a library folder that's completely empty (in case a disk has dropped off), so it stays until Agregarr makes another placeholder in %s. It's harmless." % root.replace(MEDIA_ROOT, "/media"), [])]
        return [mk("stale", "att", "Plex still lists this Coming Soon placeholder, but its file is gone.", "Refresh in Plex rescans the placeholder folder so it drops off.", ["refresh"])]
    out, have = [], pool.get(str(ext), []) if ext else []
    own_names = {a["name"] for a in owners}
    mine = [(a, it) for a, it in have if a["name"] in own_names]
    if owners and have and not mine:
        others = sorted({a["name"] for a, _ in have})
        out.append(mk("wronglib", "att", "This placeholder is in %s, but it's for something %s has, not %s." % (lib, " / ".join(others), " / ".join(sorted(own_names))),
                      "In Agregarr, this library's Coming Soon collection is probably linked to another one or set to All Servers. Set its Radarr/Sonarr server to %s and unlink it. Agregarr removes this one at its next sync." % " / ".join(sorted(own_names)), []))
        return out
    age = _real_age_days(kind, mine) if mine else None
    if age is None and real_files:
        try: age = (time.time() - max(os.path.getmtime(x) for x in real_files)) / 86400
        except OSError: pass
    if age is not None and age >= 1:
        when = "%d day%s ago" % (age, "" if int(age) == 1 else "s") if age < 60 else "a while ago"
        out.append(mk("arrived", "att", "The real %s arrived %s, but its Coming Soon placeholder is still there." % ("film" if kind == "movie" else "show", when),
                      "Agregarr normally removes placeholders by itself at its next collection sync. Remove moves the placeholder to the trash.", ["remove"]))
    elif os.path.getsize(f) == GENERIC_SIZE:
        q = "%s %s official trailer" % (name or title, year or "")
        out.append(mk("generic", "att", "It plays Agregarr's generic 30-second clip, not the real trailer.",
                      "The trailer download failed when Agregarr made it: usually an old yt-dlp, or YouTube had no trailer under 3½ minutes. Pick trailer shows the top YouTube results to choose from.",
                      ["trailer"], query=re.sub(r"\s+", " ", q).strip(), media="movie" if kind == "movie" else "tv"))
    return out

def coming_scan(sections, movies, series):
    out = []
    STATE["step"] = "Checking Coming Soon placeholders"
    st = ytdlp_status(refresh=True)
    if os.path.isdir(PLACEHOLDER_ROOT):
        if not os.path.isdir(AG_BIN): pass                  # Agregarr's config/bin isn't mounted: the yt-dlp part is switched off
        elif not st.get("ok"):
            out.append(dict(id=iid("coming", "ytdlp"), chk="coming", sev="att", title="yt-dlp for Coming Soon trailers", library="Agregarr",
                            detail={"kind": "ytdlp", "text": st.get("error", ""), "note": "Without a current yt-dlp, new placeholders get the generic clip.",
                                    "fixes": ["ytdlp"] if st.get("writable") else []}, links=[]))
        elif (st["days"] or 0) > YTDLP_OLD_DAYS or (st["newer"] and (st["days"] or 0) > 30):
            out.append(dict(id=iid("coming", "ytdlp"), chk="coming", sev="att", title="yt-dlp is %d days old" % st["days"], library="Agregarr",
                            detail={"kind": "ytdlp", "text": "Agregarr downloads Coming Soon trailers with yt-dlp %s.%s" % (st["version"], " Version %s is out." % st["latest"] if st["newer"] else ""),
                                    "note": "YouTube regularly breaks older versions, and then every new placeholder gets the generic clip. Update yt-dlp fetches the latest release and checks its checksum. Agregarr doesn't need a restart.",
                                    "fixes": ["ytdlp"] if st.get("writable") else []}, links=[]))
    for sec in sections:
        kind, key = sec["type"], sec["key"]
        locs = [l["path"].rstrip("/") for l in sec.get("Location", [])]
        ph_locs = [l for l in locs if l.startswith(PLACEHOLDER_ROOT + "/")]
        if not ph_locs: continue
        owners = [a for a in S.get("arrs", []) if a["kind"] == ("radarr" if kind == "movie" else "sonarr") and any(r in locs for r in a.get("roots", []))]
        seen = set()
        try:
            if kind == "movie":
                for m in plex_paged("/library/sections/%s/all" % key, type=1, includeGuids=1, _raw=True):
                    parts = [pt.get("file") for md in m.get("Media", []) for pt in md.get("Part", []) if pt.get("file")]
                    ph = [x for x in parts if is_placeholder(x)]
                    if not ph: continue
                    real = [x for x in parts if not is_placeholder(x) and os.path.exists(x)]
                    for f in ph:
                        seen.add(f)
                        out += _coming_item(sec, ph_locs, owners, m["ratingKey"], "%s (%s)" % (m.get("title"), m.get("year", "?")), m.get("title"), m.get("year"),
                                            f, guids(m).get("tmdb"), real, movies)
            else:
                shows = {sh["ratingKey"]: sh for sh in plex_paged("/library/sections/%s/all" % key, type=2, includeGuids=1, _raw=True)}
                for e in plex_paged("/library/sections/%s/all" % key, type=4, _raw=True):
                    for f in [pt.get("file") for md in e.get("Media", []) for pt in md.get("Part", []) if pt.get("file")]:
                        if not is_placeholder(f): continue
                        seen.add(f); sh = shows.get(e.get("grandparentRatingKey"), {})
                        nm = sh.get("title") or e.get("grandparentTitle")
                        out += _coming_item(sec, ph_locs, owners, sh.get("ratingKey") or e["ratingKey"], nm, nm, sh.get("year"), f, guids(sh).get("tvdb"), [], series)
        except Exception as ex:
            log("coming soon check %s failed: %r" % (sec["title"], ex))
        for root in ph_locs:                                  # clips on disk that Plex hasn't picked up yet
            for dp, dn, fn in os.walk(root):
                dn[:] = [d for d in dn if not d.startswith(".")]
                for n in fn:
                    f = os.path.join(dp, n)
                    if f in seen or not n.lower().endswith(".mp4"): continue
                    try:
                        if os.path.getsize(f) != GENERIC_SIZE: continue
                    except OSError: continue
                    folder = os.path.relpath(f, root).split("/")[0]
                    mt = re.match(r"^(.*?)\s*\((\d{4})\)", folder)
                    out += _coming_item(sec, ph_locs, [], None, folder, mt.group(1) if mt else folder, mt.group(2) if mt else "", f, None, [], {})
    return out

def _coming_row(issue_id):
    with db() as c:
        row = c.execute("SELECT * FROM issues WHERE id=? AND chk='coming'", (issue_id,)).fetchone()
    return (row, json.loads(row["detail"])) if row else (None, None)

def _safe_placeholder(p):
    rp = os.path.realpath(p or "")
    return rp.startswith(PLACEHOLDER_ROOT + "/") and not rp.startswith(TRAILER_TMP + "/") and os.path.isfile(rp)

def trailer_search(issue_id, q=None):
    row, d = _coming_row(issue_id)
    if not row or d.get("kind") != "generic": return {"ok": False, "error": "That placeholder isn't in the last scan any more. Scan again first."}
    q = re.sub(r"\s+", " ", str(q or d.get("query") or row["title"]))[:120].strip()
    try:
        r = ytdlp(["--flat-playlist", "--no-warnings", "--print", "%(id)s\t%(duration)s\t%(channel)s\t%(title)s", "ytsearch8:" + q], timeout=90)
    except Exception as e:
        return {"ok": False, "error": "YouTube search failed: %s" % e}
    res = []
    for line in r.stdout.splitlines():
        p = line.split("\t", 3)
        if len(p) == 4 and VID_RE.match(p[0]):
            try: dur = int(float(p[1]))
            except ValueError: dur = 0
            if dur and dur > 600: continue                   # not a trailer
            res.append({"id": p[0], "secs": dur, "channel": "" if p[2] == "NA" else p[2], "title": p[3]})
    if not res: return {"ok": False, "error": "YouTube didn't return anything for “%s”. Try other words." % q, "q": q}
    return {"ok": True, "q": q, "results": res[:6]}

def trailer_use(issue_id, vid):
    vid = str(vid or "")
    if not VID_RE.match(vid): return {"ok": False, "error": "That isn't a YouTube video id."}
    row, d = _coming_row(issue_id)
    if not row or d.get("kind") != "generic": return {"ok": False, "error": "That placeholder isn't in the last scan any more. Scan again first."}
    f = d["path"]
    if not _safe_placeholder(f): return {"ok": False, "error": "The placeholder isn't where it was. Scan again."}
    if os.path.getsize(f) != GENERIC_SIZE: return {"ok": False, "error": "That placeholder isn't the generic clip any more."}
    if not TRAILER_LOCK.acquire(blocking=False): return {"ok": False, "error": "Another trailer is downloading. Try again in a minute."}
    try:                                              # the lock is handed to the download thread, which releases it
        with db() as c:
            c.execute("DELETE FROM done WHERE issue_id=? AND status='checking'", (issue_id,))
            done_id = c.execute("INSERT INTO done (issue_id, title, library, kept, moved, trash_ids, links, at, status, note, chk) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                (issue_id, row["title"], row["library"], "", "[]", "[]", row["links"], now_iso(), "checking", "Downloading the trailer…", "coming")).lastrowid
    except Exception:
        TRAILER_LOCK.release(); raise
    threading.Thread(target=_trailer_dl, args=(done_id, issue_id, row["title"], f, vid, d.get("section")), daemon=True).start()
    return {"ok": True}

def _probe(p):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=height:format=duration", "-of", "json", p],
                           capture_output=True, text=True, timeout=60)
        j = json.loads(r.stdout or "{}")
        return float((j.get("format") or {}).get("duration") or 0), int(((j.get("streams") or [{}])[0]).get("height") or 0)
    except Exception: return 0.0, 0

def _trailer_dl(done_id, issue_id, title, f, vid, section):
    status, note = "warn", ""
    try:
        try:
            os.makedirs(TRAILER_TMP, exist_ok=True)
            tmp = os.path.join(TRAILER_TMP, issue_id + ".mp4")
            for n in os.listdir(TRAILER_TMP):
                if n.startswith(issue_id): os.unlink(os.path.join(TRAILER_TMP, n))
            r = ytdlp(["--no-playlist", "--no-warnings", "-q", "-f", TRAILER_FMT, "--merge-output-format", "mp4", "--match-filter", "duration < 600",
                       "-o", tmp, "https://www.youtube.com/watch?v=" + vid], timeout=600)
            dur, h = _probe(tmp) if os.path.isfile(tmp) else (0.0, 0)
            if r.returncode != 0 or dur < 20:
                err = (r.stderr.strip().splitlines() or ["no video file came back"])[-1]
                note = "The download didn't work, so the generic clip is still there: %s" % err[:200]
            elif not _safe_placeholder(f) or os.path.getsize(f) != GENERIC_SIZE:
                note = "The placeholder changed while downloading (Agregarr may have replaced or removed it), so it was left alone."
            else:
                size = os.path.getsize(tmp)
                os.replace(tmp, f)
                plex_refresh_folder(section, os.path.dirname(f))
                status = "ok"
                note = "✓ Real trailer in place: %d:%02d, %s, %s. Plex picks it up after its folder scan." % (dur // 60, dur % 60, "%dp" % h if h else "video", human(size))
                with db() as c: c.execute("DELETE FROM issues WHERE id=?", (issue_id,))
                event("coming", "%s: swapped the generic Coming Soon clip for the real trailer (youtu.be/%s)." % (title, vid))
            for n in os.listdir(TRAILER_TMP):
                if n.startswith(issue_id): os.unlink(os.path.join(TRAILER_TMP, n))
        except Exception as e:
            note = "The download failed: %s" % e
    finally:
        TRAILER_LOCK.release()
    with db() as c:
        c.execute("UPDATE done SET status=?, note=?, checked_at=? WHERE id=?", (status, note, now_iso(), done_id))
    log("trailer %s: %s" % (done_id, note))

def coming_fix(issue_id, how):
    row, d = _coming_row(issue_id)
    if not row: return {"ok": False, "error": "That one isn't in the last scan any more. Scan again first."}
    if how not in d.get("fixes", []): return {"ok": False, "error": "That fix doesn't apply here."}
    moved, tids, status = [], [], "ok"
    if how == "ytdlp":
        r = ytdlp_update()
        if not r.get("ok"): return r
        note = "✓ yt-dlp updated %s → %s. New Coming Soon placeholders use it straight away." % (r.get("old") or "?", r["new"])
    elif how == "refresh":
        if not plex_refresh_folder(d["section"], d["root"]): return {"ok": False, "error": "Plex didn't answer."}
        note = "Asked Plex to rescan %s. Scan again in a minute to check it's gone." % d["root"].replace(MEDIA_ROOT, "/media")
    elif how == "remove":
        src = d["path"]
        if not _safe_placeholder(src): return {"ok": False, "error": "The placeholder isn't where it was. Scan again."}
        with MOVE_LOCK:
            dst = os.path.join(TRASH_DIR, datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + issue_id, os.path.relpath(os.path.realpath(src), MEDIA_ROOT))
            os.makedirs(os.path.dirname(dst), exist_ok=True); size = os.path.getsize(src); move_file(src, dst)
            with db() as c:
                tids.append(c.execute("INSERT INTO trash (issue_id, title, src, dst, size, reason, moved_at) VALUES (?,?,?,?,?,?,?)",
                                      (issue_id, row["title"], src, dst, size, "Coming Soon placeholder (film arrived)", now_iso())).lastrowid)
            moved.append(src)
        plex_refresh_folder(d["section"], os.path.dirname(src))
        note = "Moved the placeholder to the trash. Plex drops it after its folder scan."
        event("coming", "%s: moved the leftover Coming Soon placeholder to the trash." % row["title"])
    else:
        return {"ok": False, "error": "Unknown fix."}
    with db() as c:
        c.execute("INSERT INTO done (issue_id, title, library, kept, moved, trash_ids, links, at, status, note, chk) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (issue_id, row["title"], row["library"], "", json.dumps(moved), json.dumps(tids), row["links"], now_iso(), status, note, "coming"))
        c.execute("DELETE FROM issues WHERE id=?", (issue_id,))
    return {"ok": True}

# ---------------- quality & space, unwatched (Tautulli), Sportarr (v18) ----------------
TIER = {"sd": 0, "480": 0, "576": 1, "720": 2, "1080": 3, "2k": 3, "4k": 4}
RES_LABEL = {"sd": "SD", "480": "480p", "576": "576p", "720": "720p", "1080": "1080p", "2k": "2K", "4k": "4K"}
TIER_LABEL = {0: "SD", 1: "576p", 2: "720p", 3: "1080p", 4: "4K"}
HOG_MIN = {"4k": 18.0, "1080": 4.5, "2k": 4.5, "720": 2.5}      # GB an hour: never flag below these...
HOG_X = 2.5                                                     # ...and only at this many times the usual for that library + resolution
REMUX_CONTAINERS = ("mpegts", "ts", "m2ts", "avi", "mpeg", "asf", "wmv", "flv")
MODERN_CODECS = ("hevc", "h264", "av1", "vp9")
OLD_CODECS = ("mpeg2video", "vc1", "wmv3", "wmv2", "msmpeg4v3", "msmpeg4", "mpeg4", "divx", "xvid", "h263")
UNWATCHED_DAYS = int(os.environ.get("LD_UNWATCHED_DAYS", "365"))
UNWATCHED_MIN_AGE = int(os.environ.get("LD_UNWATCHED_MIN_AGE", "90"))
KEEP_DAYS = int(os.environ.get("LD_KEEP_DAYS", "182"))   # Unwatched "Keep" hides an item this long (about 6 months)   # don't list things added recently
REMUX_LOCK = threading.Lock()

def res_label(r): return RES_LABEL.get(r, (r or "?").upper())

def _arr_refs_links(kind, m, show, movies, series, mine):
    """(links, ref) for a Plex film or episode: arr links + what to search/rescan."""
    links, ref = [], None
    if kind == "movie":
        for a, it in [(a, it) for a, it in movies.get(str(guids(m).get("tmdb")), []) if mine(a)][:1]:
            links.append(arr_link(a, it, "movie")); ref = {"arr": a["name"], "kind": "movie", "id": it["id"]}
    elif show:
        for a, it in [(a, it) for a, it in series.get(str(guids(show).get("tvdb")), []) if mine(a)][:1]:
            links.append(arr_link(a, it, "series"))
            ref = {"arr": a["name"], "kind": "series", "id": it["id"], "series": it["id"],
                   "eps": [[int(m.get("parentIndex") or 0), int(m.get("index") or 0)]] if m.get("type") == "episode" else []}
    return links, ref

def quality_scan(sections, movies, series, trashed):
    out, rows, sizes = [], [], {}
    QUALITY_CODECS.clear()
    pending = smaller_waiting_paths()                 # a smaller copy is already on its way for these
    for sec in sections:
        lib, kind, key = sec["title"], sec["type"], sec["key"]
        STATE["step"] = "Checking quality and space in " + lib
        locs = [l["path"].rstrip("/") for l in sec.get("Location", [])]
        mine = (lambda locs: lambda a: any(r in locs for r in a.get("roots", [])))(locs)
        shows = {sh["ratingKey"]: sh for sh in plex_paged("/library/sections/%s/all" % key, type=2, includeGuids=1)} if kind == "show" else {}
        for m in plex_paged("/library/sections/%s/all" % key, type=1 if kind == "movie" else 4, includeGuids=1):
            for md in m.get("Media", []):
                ps = [p for p in md.get("Part", []) if p.get("file")]
                if not ps or any(p["file"] in trashed for p in ps): continue
                size = sum(p.get("size") or 0 for p in ps)
                for p in ps: QUALITY_CODECS[p["file"]] = (md.get("videoCodec") or "").lower()
                skey = m["ratingKey"] if kind == "movie" else m.get("grandparentRatingKey")
                sizes[str(skey)] = sizes.get(str(skey), 0) + size
                rows.append({"sec": sec, "kind": kind, "m": m, "md": md, "parts": ps, "size": size, "mine": mine,
                             "hrs": (md.get("duration") or 0) / 3.6e6, "res": str(md.get("videoResolution") or "").lower(),
                             "show": shows.get(m.get("grandparentRatingKey"))})
    usual = {}
    for r in rows:
        if r["hrs"] > 0.2: usual.setdefault((r["sec"]["key"], r["res"]), []).append(r["size"] / 1e9 / r["hrs"])
    usual = {k: sorted(v)[len(v) // 2] for k, v in usual.items()}
    seasons, hog_shows = {}, {}
    for r in rows:
        sec, m, md, kind = r["sec"], r["m"], r["md"], r["kind"]
        lib = sec["title"]
        title = "%s (%s)" % (m.get("title"), m.get("year", "?")) if kind == "movie" else \
                "%s S%02dE%02d" % (m.get("grandparentTitle"), int(m.get("parentIndex") or 0), int(m.get("index") or 0))
        alinks, ref = _arr_refs_links(kind, m, r["show"], movies, series, r["mine"])
        links = [plex_link(m["ratingKey"])] + alinks
        f0 = r["parts"][0]["file"]
        u = usual.get((sec["key"], r["res"]))
        if r["hrs"] > 0.2 and u:
            gbh = r["size"] / 1e9 / r["hrs"]
            if gbh > max(HOG_X * u, HOG_MIN.get(r["res"], 1.5)):
                extra = max(0, r["size"] - u * 1e9 * r["hrs"])
                if kind == "show":
                    hog_shows.setdefault((sec["key"], m.get("grandparentRatingKey")), []).append((r, gbh, u, extra, alinks, ref))
                elif f0 not in pending:
                  needs_tdarr = len(r["parts"]) == 1 and (md.get("videoCodec") or "").lower() not in TDARR_DONE_CODECS and tdarr_lib_for(f0)
                  out.append(dict(id=iid("quality", "hog", lib, f0), chk="quality", sev="att", title=title, library=lib,
                                detail={"kind": "hog", "path": f0, "size": r["size"], "extra": int(extra), "ref": ref, "section": sec["key"],
                                        "vcodec": (md.get("videoCodec") or "").lower(), "tdarr_paths": [f0] if needs_tdarr else [],
                                        "fixes": (["tdarr"] if needs_tdarr else []) + (["smaller"] if ref and ref.get("kind") == "movie" else []),
                                        "text": "%s for %s: about %.1f GB an hour, %.1f× the usual for %s in %s (%.1f GB an hour)." % (
                                            human(r["size"]), mins(md.get("duration")), gbh, gbh / u, res_label(r["res"]), lib, u),
                                        "note": ("It's still %s, so Tdarr never converted it: that's why it's big. Convert with Tdarr shrinks it in place to about the usual size (around %s), or Find a smaller copy downloads one." % (
                                                    (md.get("videoCodec") or "an older format").upper(), human(r["size"] - extra))) if needs_tdarr else
                                                "Usually a remux or a very high-bitrate release. Keep it if you want the best picture, or press Find a smaller copy to save about %s: the old file stays until the smaller one is in." % human(extra)
                                                if ref and ref.get("kind") == "movie" else "Usually a remux or a very high-bitrate release. Keep it if you want the best picture. To save about %s, pick a smaller release with Interactive Search in %s." % (human(extra), ref["arr"] if ref else "Radarr/Sonarr")},
                                links=links))
        cont, vc = (md.get("container") or "").lower(), (md.get("videoCodec") or "").lower()
        if len(r["parts"]) == 1 and (cont in REMUX_CONTAINERS or vc in OLD_CODECS):
            remux = cont in REMUX_CONTAINERS and vc in MODERN_CODECS
            ext = os.path.splitext(f0)[1].lower() or "." + cont
            out.append(dict(id=iid("quality", "old", lib, f0), chk="quality", sev="att", title=title, library=lib,
                            detail={"kind": "remux" if remux else "oldcodec", "path": f0, "size": r["size"], "section": sec["key"], "ref": ref,
                                    "refs": [{"arr": ref["arr"], "kind": "movie" if ref["kind"] == "movie" else "series", "id": ref["id"]}] if ref else [],
                                    "tdarr_paths": [f0] if (not remux and tdarr_lib_for(f0)) else [],
                                    "fixes": ["remux"] if remux else ((["tdarr"] if tdarr_lib_for(f0) else []) + (["search"] if ref else [])),
                                    "text": ("A %s file. The video inside is fine (%s), but some players can't use the %s box, so Plex has to convert it while you watch." % (ext, vc.upper(), ext))
                                            if remux else ("Old %s video in a %s file, so most players need Plex to convert it while you watch." % (vc.upper() or "?", ext)),
                                    "note": "Convert to MKV copies the same video and sound into an .mkv: nothing is re-encoded, it takes seconds, and the old file goes to the trash." if remux
                                            else "Search for a better copy asks %s to look for one. It only downloads it if it's an upgrade for your quality profile." % (ref["arr"] if ref else "Radarr/Sonarr")},
                            links=links))
        if kind == "show":
            seasons.setdefault((sec["key"], m.get("grandparentRatingKey")), {}).setdefault(int(m.get("parentIndex") or 0), []).append(r)
    for (sk, srk), hs in hog_shows.items():                 # TV space hogs: one card per show
        r0, _, u0, _, alinks, ref = hs[0]; sec, sh = r0["sec"], r0["show"] or {}
        extra, size = sum(h[3] for h in hs), sum(h[0]["size"] for h in hs)
        name = sh.get("title") or r0["m"].get("grandparentTitle") or "?"
        if len(hs) == 1: what = "%s S%02dE%02d" % (name, int(r0["m"].get("parentIndex") or 0), int(r0["m"].get("index") or 0))
        else: what = name
        tdp = [h[0]["parts"][0]["file"] for h in hs if len(h[0]["parts"]) == 1 and (h[0]["md"].get("videoCodec") or "").lower() not in TDARR_DONE_CODECS
               and h[0]["parts"][0]["file"] not in pending and tdarr_lib_for(h[0]["parts"][0]["file"])]
        out.append(dict(id=iid("quality", "hogshow", sec["title"], srk), chk="quality", sev="att", title=what, library=sec["title"],
                        detail={"kind": "hog", "size": size, "extra": int(extra), "fixes": ["tdarr"] if tdp else [], "tdarr_paths": tdp, "section": sec["key"],
                                "refs": [{"arr": ref["arr"], "kind": "series", "id": ref["id"]}] if ref else [],
                                "path": r0["parts"][0]["file"] if len(hs) == 1 else "",
                                "eps": [{"se": "S%02dE%02d" % (int(h[0]["m"].get("parentIndex") or 0), int(h[0]["m"].get("index") or 0)),
                                         "res": "%s · %.1f GB an hour" % (human(h[0]["size"]), h[1])} for h in sorted(hs, key=lambda h: (int(h[0]["m"].get("parentIndex") or 0), int(h[0]["m"].get("index") or 0)))] if len(hs) > 1 else None,
                                "text": ("%d episodes take %s, %.1f× the usual size for %s in %s (%.1f GB an hour)." % (len(hs), human(size), sum(h[1] for h in hs) / len(hs) / u0, res_label(r0["res"]), sec["title"], u0))
                                        if len(hs) > 1 else "%s for %s: about %.1f GB an hour, %.1f× the usual for %s in %s (%.1f GB an hour)." % (
                                            human(size), mins(r0["md"].get("duration")), hs[0][1], hs[0][1] / u0, res_label(r0["res"]), sec["title"], u0),
                                "note": "Usually a remux or a very high-bitrate release. Keep it if you want the best picture. To save about %s, pick smaller releases with Interactive Search in %s; the new files replace these." % (
                                    human(extra), ref["arr"] if ref else "Sonarr")},
                        links=[plex_link(srk)] + alinks))
    for (sk, srk), by_season in seasons.items():
        bad, tops, total = [], set(), 0
        for sn, eps in sorted(by_season.items()):
            if len(eps) < 4: continue
            cnt = {}
            for e in eps: cnt[TIER.get(e["res"], 0)] = cnt.get(TIER.get(e["res"], 0), 0) + 1
            top, n = max(cnt.items(), key=lambda x: x[1])
            if top < 2 or n < 0.6 * len(eps): continue
            low = [e for e in eps if TIER.get(e["res"], 0) <= top - 2]
            if low: bad += [(sn, e) for e in low]; tops.add(TIER_LABEL[top]); total += len(eps)
        if not bad: continue
        e0 = bad[0][1]; sec, sh = e0["sec"], e0["show"] or {}
        alinks, ref = _arr_refs_links("show", e0["m"], sh, movies, series, e0["mine"])
        if ref: ref["eps"] = [[sn, int(e["m"].get("index") or 0)] for sn, e in bad]
        nseas = len({sn for sn, _ in bad})
        out.append(dict(id=iid("quality", "odd", sec["title"], srk), chk="quality", sev="att", title=sh.get("title") or e0["m"].get("grandparentTitle") or "?", library=sec["title"],
                        detail={"kind": "odd", "ref": ref, "fixes": ["search"] if ref else [],
                                "eps": [{"se": "S%02dE%02d" % (sn, int(e["m"].get("index") or 0)), "res": res_label(e["res"]), "rk": e["m"]["ratingKey"]} for sn, e in bad],
                                "text": "%d episode%s in %d season%s %s much lower quality than the rest of %s (mostly %s)." % (
                                    len(bad), "" if len(bad) == 1 else "s", nseas, "" if nseas == 1 else "s", "is" if len(bad) == 1 else "are",
                                    "the season" if nseas == 1 else "their seasons", "/".join(sorted(tops))),
                                "note": "Search for better copies asks %s to look for each one. It only downloads one if it's an upgrade for your quality profile." % (ref["arr"] if ref else "Sonarr")},
                        links=[plex_link(srk)] + alinks))
    return out, sizes

# ---------------- Monitor (v32) ----------------
# Sonarr/Radarr only upgrade monitored items. The odd-quality episodes were all unmonitored, so "Search for better copies"
# was a one-off and nothing improved later. Monitor turns monitoring on for just the flagged episodes (and the series if it
# was off) - never whole seasons, so missing episodes aren't pulled in - or for the film in Radarr.
def _unmonitored(ref, cache):
    a = arr_by_name(ref.get("arr"))
    if not a: return None
    if ref.get("kind") == "movie":
        m = arr(a, "/movie/%d" % int(ref["id"]))
        return {"movie": not m.get("monitored")} if not m.get("monitored") else {}
    sid = int(ref["series"])
    if sid not in cache:
        cache[sid] = (arr(a, "/series/%d" % sid), {(e["seasonNumber"], e["episodeNumber"]): e for e in arr(a, "/episode?seriesId=%d" % sid)})
    ser, eps = cache[sid]
    un = [[s_, e_] for s_, e_ in ref.get("eps", []) if (s_, e_) in eps and not eps[(s_, e_)]["monitored"]]
    out = {}
    if un: out["eps"] = un
    if not ser.get("monitored"): out["series"] = True
    return out

def monitor_state(items):
    cache = {}
    for it in items:
        d = it["detail"]
        if "search" not in d.get("fixes", []) or not d.get("ref"): continue
        try: un = _unmonitored(d["ref"], cache)
        except Exception: continue
        if un is None: continue
        if un:
            d["unmon"] = un
            d["fixes"] = [f for f in d["fixes"] if f != "monitor"]
            d["fixes"].insert(d["fixes"].index("search") + 1, "monitor")
        else:
            d["monitored"] = True

def monitor_fix(issue_id, row, d):
    ref = d.get("ref") or {}; a = arr_by_name(ref.get("arr"))
    if not a: return {"ok": False, "error": "Unknown Radarr/Sonarr."}
    try:
        if ref.get("kind") == "movie":
            arr(a, "/movie/editor", {"movieIds": [int(ref["id"])], "monitored": True}, "PUT"); what = "the film"
        else:
            sid = int(ref["series"]); ser = arr(a, "/series/%d" % sid)
            em = {(x["seasonNumber"], x["episodeNumber"]): x for x in arr(a, "/episode?seriesId=%d" % sid)}
            ids = [em[(s_, e_)]["id"] for s_, e_ in ref.get("eps", []) if (s_, e_) in em and not em[(s_, e_)]["monitored"]]
            ser_off = not ser.get("monitored")
            if ser_off:
                ser["monitored"] = True; arr(a, "/series/%d" % sid, ser, "PUT")
            if ids: arr(a, "/episode/monitor", {"episodeIds": ids, "monitored": True}, "PUT")
            what = "%d episode%s" % (len(ids), "" if len(ids) == 1 else "s") + (" and the series" if ser_off else "")
    except Exception as e:
        return {"ok": False, "error": "Couldn't ask %s: %s" % (a["name"], e)}
    d["fixes"] = [f for f in d.get("fixes", []) if f != "monitor"]; d.pop("unmon", None); d["monitored"] = True
    note = "Monitored %s in %s. It now upgrades %s by itself whenever a better copy turns up." % (what, a["name"], "it" if ref.get("kind") == "movie" else "them")
    with db() as c:
        c.execute("UPDATE issues SET detail=? WHERE id=?", (json.dumps(d), issue_id))
        c.execute("INSERT INTO done (issue_id, title, library, kept, moved, trash_ids, links, at, status, note, chk) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (issue_id, row["title"], row["library"], "", "[]", "[]", row["links"], now_iso(), "ok", note, "quality"))
    event("quality", "%s: %s" % (row["title"], note))
    return {"ok": True}

def quality_fix(issue_id, how):
    with db() as c:
        row = c.execute("SELECT * FROM issues WHERE id=? AND chk='quality'", (issue_id,)).fetchone()
    if not row: return {"ok": False, "error": "That one isn't in the last scan any more. Scan again first."}
    d = json.loads(row["detail"])
    if how not in d.get("fixes", []): return {"ok": False, "error": "That fix doesn't apply here."}
    if how == "tdarr": return tdarr_fix(issue_id, row, d)
    if how == "monitor": return monitor_fix(issue_id, row, d)
    if how == "search":
        ref = d.get("ref") or {}; a = arr_by_name(ref.get("arr"))
        if not a: return {"ok": False, "error": "Unknown Radarr/Sonarr."}
        try:
            if ref["kind"] == "movie":
                cmd = arr(a, "/command", {"name": "MoviesSearch", "movieIds": [int(ref["id"])]}, "POST"); n = 1; sids, mode = [int(ref["id"])], "movie"
            else:
                em = {(x["seasonNumber"], x["episodeNumber"]): x["id"] for x in arr(a, "/episode?seriesId=%d" % int(ref["series"]))}
                ids = [em[(s, e)] for s, e in ref.get("eps", []) if (s, e) in em]
                if not ids: return {"ok": False, "error": "%s doesn't list those episodes." % a["name"]}
                cmd = arr(a, "/command", {"name": "EpisodeSearch", "episodeIds": ids}, "POST"); n = len(ids); sids, mode = ids, "episode"
        except Exception as e:
            return {"ok": False, "error": "Couldn't ask %s: %s" % (a["name"], e)}
        note = "Asked %s to search for %d better cop%s. Followed up every hour: grabbed, imported, waiting to import, or nothing found (then hidden for %d days and searched again by itself)." % (a["name"], n, "y" if n == 1 else "ies", SNOOZE_DAYS)
        sref = {"kind": "search", "arr": a["name"], "mode": mode, "ids": sids, "at": now_iso(), "cmd": (cmd or {}).get("id")}
        with db() as c:
            snooze(c, issue_id, row["title"], days=1)
            c.execute("INSERT INTO done (issue_id, title, library, kept, moved, trash_ids, links, at, status, note, chk, ref) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                      (issue_id, row["title"], row["library"], "", "[]", "[]", row["links"], now_iso(), "waiting", note, "quality", json.dumps(sref)))
            c.execute("DELETE FROM issues WHERE id=?", (issue_id,))
        event("quality", "%s: %s" % (row["title"], note))
        return {"ok": True}
    src = d["path"]
    if not safe_media_path(src) or not os.path.isfile(src): return {"ok": False, "error": "The file isn't where it was. Scan again."}
    if os.path.exists(os.path.splitext(src)[0] + ".mkv"): return {"ok": False, "error": "There's already an .mkv with that name next to it."}
    if not REMUX_LOCK.acquire(blocking=False): return {"ok": False, "error": "Another file is being converted. Try again in a minute."}
    try:
        with db() as c:
            done_id = c.execute("INSERT INTO done (issue_id, title, library, kept, moved, trash_ids, links, at, status, note, chk) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                (issue_id, row["title"], row["library"], "", "[]", "[]", row["links"], now_iso(), "checking", "Converting to MKV…", "quality")).lastrowid
    except Exception:
        REMUX_LOCK.release(); raise
    threading.Thread(target=_remux, args=(done_id, issue_id, row["title"], d), daemon=True).start()
    return {"ok": True}

def _remux(done_id, issue_id, title, d):
    src, status, note, moved, tids = d["path"], "warn", "", [], []
    tmp = os.path.join(os.path.dirname(src), ".ld-remux-%s.mkv" % issue_id)   # dot file: Plex and the arrs ignore it
    dst = os.path.splitext(src)[0] + ".mkv"
    try:
        base_cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-analyzeduration", "100M", "-probesize", "50M", "-i", src, "-map", "0:v", "-map", "0:a?"]   # big probe: some .ts files only give the HEVC picture size after a few MB
        r = subprocess.run(base_cmd + ["-map", "0:s?", "-c", "copy", "-ignore_unknown", "-f", "matroska", tmp], capture_output=True, text=True, timeout=3600)
        if r.returncode != 0:                                   # some .ts subtitle streams can't go in an mkv: try without them
            r = subprocess.run(base_cmd + ["-c", "copy", "-ignore_unknown", "-f", "matroska", tmp], capture_output=True, text=True, timeout=3600)
        reenc = False
        if r.returncode != 0:                                   # some .ts AAC tracks can't be copied into an mkv (no usable audio config): copy the video, re-encode only the sound
            r = subprocess.run(base_cmd + ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-f", "matroska", tmp], capture_output=True, text=True, timeout=3600)
            reenc = r.returncode == 0
        d_src, _ = _probe(src); d_new, h = _probe(tmp) if os.path.isfile(tmp) else (0.0, 0)
        if r.returncode != 0 or not d_new or abs(d_src - d_new) > 2 or os.path.getsize(tmp) < 0.70 * os.path.getsize(src):   # .ts packaging alone can be 10-20% of a low-bitrate file; the duration check above is the real test
            note = "Converting didn't give a complete copy, so nothing was changed. %s" % ((r.stderr.strip().splitlines() or [""])[-1][:160])
        elif os.path.exists(dst):
            note = "An .mkv with that name appeared meanwhile, so nothing was changed."
        else:
            with MOVE_LOCK:
                tdst = os.path.join(TRASH_DIR, datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + issue_id, os.path.relpath(os.path.realpath(src), MEDIA_ROOT))
                os.makedirs(os.path.dirname(tdst), exist_ok=True); size = os.path.getsize(src); move_file(src, tdst)
                with db() as c:
                    tids.append(c.execute("INSERT INTO trash (issue_id, title, src, dst, size, reason, moved_at) VALUES (?,?,?,?,?,?,?)",
                                          (issue_id, title, src, tdst, size, "Old %s copy (now an .mkv)" % os.path.splitext(src)[1], now_iso())).lastrowid)
                moved.append(src)
                move_file(tmp, dst)
            plex_refresh_folder(d.get("section"), os.path.dirname(src))
            arr_rescan(d.get("refs"))
            status = "ok"
            note = ("✓ Now an .mkv (%d:%02d, the same video; the sound had to be re-encoded to AAC 192k because it couldn't be copied). The old file is in the trash." if reenc
                    else "✓ Now an .mkv (%d:%02d, the same video and sound, nothing re-encoded). The old file is in the trash.") % (d_new // 60, d_new % 60)
            with db() as c: c.execute("DELETE FROM issues WHERE id=?", (issue_id,))
            event("quality", "%s: converted to MKV; the old %s is in the trash." % (title, os.path.splitext(src)[1]))
    except Exception as e:
        note = "Converting failed: %s" % e
    finally:
        try:
            if os.path.exists(tmp): os.unlink(tmp)
        except OSError: pass
        REMUX_LOCK.release()
    with db() as c:
        c.execute("UPDATE done SET status=?, note=?, checked_at=?, moved=?, trash_ids=? WHERE id=?", (status, note, now_iso(), json.dumps(moved), json.dumps(tids), done_id))
    log("remux %s: %s" % (done_id, note))

def uw_move(item_id, until=None, show=False):
    """Keep / Show again for one Unwatched item, applied to the stored card straight away (the nightly scan does the same from Tautulli)."""
    with db() as c:
        for r in c.execute("SELECT id, title, detail FROM issues WHERE chk='unwatched'").fetchall():
            d = json.loads(r["detail"]); d.setdefault("kept", [])
            if show:
                it = next((x for x in d["kept"] if x["id"] == item_id), None)
                if not it: continue
                d["kept"].remove(it); it.pop("until", None); d["items"].append(it); d["items"].sort(key=lambda x: -x["size"])
                d["count"] += 1; d["total"] += it["size"]
            else:
                it = next((x for x in d["items"] if x["id"] == item_id), None) or next((x for x in d["kept"] if x["id"] == item_id), None)
                if not it: continue
                if it in d["items"]:
                    d["items"].remove(it); d["count"] -= 1; d["total"] -= it["size"]; d["kept"].append(it); d["kept"].sort(key=lambda x: x["t"].lower())
                it["until"] = until
            title = "%d %s%s nobody has watched" % (d["count"], d.get("what", "film"), "" if d["count"] == 1 else "s")
            c.execute("UPDATE issues SET detail=?, title=? WHERE id=?", (json.dumps(d), title, r["id"]))
            return True
    return False

def unwatched_scan(sections, sizes):
    t = S.get("tautulli")
    if not t: return []
    STATE["step"] = "Checking what nobody watches"
    def tt(cmd, **q):
        return http(t["url"].rstrip("/") + "/api/v2?" + urllib.parse.urlencode(dict(q, apikey=t["key"], cmd=cmd)), timeout=180)["response"]["data"]
    try:
        h = tt("get_history", length=1, order_column="date", order_dir="asc").get("data") or []
        hist_start = float(h[0]["date"]) if h else time.time()
    except Exception as e:
        log("tautulli history failed: %r" % e); return []
    since = max(time.time() - UNWATCHED_DAYS * 86400, hist_start)
    added_before = time.time() - UNWATCHED_MIN_AGE * 86400
    since_txt = datetime.fromtimestamp(since).strftime("%-d %b %Y")
    full_year = since > hist_start
    nowi = now_iso()
    with db() as c:
        keep = {r["id"]: r["until"] for r in c.execute("SELECT id, until FROM ignored")}
        expired = [k for k, u in keep.items() if u and u <= nowi]
        for k in expired: c.execute("DELETE FROM ignored WHERE id=?", (k,))   # its 6 months are up: list it again if it's still unwatched
    for k in expired: keep.pop(k, None)
    out = []
    for sec in sections:
        try: rows = tt("get_library_media_info", section_id=sec["key"], length=20000).get("data") or []
        except Exception as e:
            log("tautulli library %s failed: %r" % (sec["title"], e)); continue
        items, kept = [], []
        for r in rows:
            rk = str(r.get("rating_key")); added = float(r.get("added_at") or 0); last = float(r.get("last_played") or 0)
            if not added or added > added_before or (last and last >= since): continue
            k = iid("unwatched", rk); size = sizes.get(rk, 0)
            if not size: continue
            it = {"id": k, "rk": rk, "t": "%s (%s)" % (r.get("title"), r.get("year") or "?"), "size": size,
                  "plays": int(r.get("play_count") or 0), "last": int(last) or None, "url": plex_link(rk)["url"]}
            if k in keep: it["until"] = keep[k]; kept.append(it)
            else: items.append(it)
        if not items and not kept: continue
        kept.sort(key=lambda x: x["t"].lower())
        items.sort(key=lambda x: -x["size"]); total = sum(x["size"] for x in items)
        what = "film" if sec["type"] == "movie" else "show"
        out.append(dict(id=iid("unwatched", "lib", sec["key"]), chk="unwatched", sev="att", library=sec["title"],
                        title="%d %s%s nobody has watched" % (len(items), what, "" if len(items) == 1 else "s"),
                        detail={"kind": "unwatched", "count": len(items), "total": total, "items": items[:60], "kept": kept, "what": what, "fixes": [],
                                "text": "Together %s. Not played %s, and in the library for at least %d days." % (
                                    human(total), "in the last year" if full_year else "since %s (when Tautulli started keeping history)" % since_txt, UNWATCHED_MIN_AGE),
                                "note": "Nothing is ever deleted from here. Keep hides one for %s (or for good, from the Kept list). To free space, delete in Radarr/Sonarr so they don't download it again." % ("about 6 months" if KEEP_DAYS == 182 else "%d days" % KEEP_DAYS)},
                        links=[{"label": "Open Tautulli", "url": t.get("public", "")}] if t.get("public") else []))
    return out

def sportarr_files():
    """{path as Library Doctor sees it: event} for every Sportarr event with a file, + Sportarr's root folders."""
    sp = S.get("sportarr")
    if not sp: return {}, []
    fix = lambda p: to_local(p, sp)
    hdr = {"X-Api-Key": sp["key"]}
    files = {fix(e["filePath"]): e for e in http(sp["url"].rstrip("/") + "/api/events", hdr, timeout=90) if e.get("hasFile") and e.get("filePath")}
    roots = [fix(r["path"].rstrip("/")) for r in http(sp["url"].rstrip("/") + "/api/rootfolder", hdr, timeout=30)]
    return files, roots

def sports_scan(sections, sport, roots, trashed):
    sp = S.get("sportarr") or {}
    out, link = [], ([{"label": "Open Sportarr", "url": sp["public"]}] if sp.get("public") else [])
    for f, e in sport.items():
        if not os.path.exists(f) and f not in trashed:
            out.append(dict(id=iid("sportmissing", f), chk="missing", sev="bad", title="%s: %s" % (e.get("leagueName") or "Sport", e.get("title")), library="Sportarr",
                            detail={"text": "Sportarr thinks it has this file, but it isn't on the disk.", "path": f,
                                    "note": "Sportarr will look for it again if the event is monitored. A Plex scan drops it from Plex.", "size": human(e.get("fileSize"))},
                            links=link))
    for sec in sections:
        locs = [l["path"].rstrip("/") for l in sec.get("Location", [])]
        if not any(l == r or l.startswith(r + "/") or r.startswith(l + "/") for l in locs for r in roots): continue
        for m in plex_paged("/library/sections/%s/all" % sec["key"], type=4 if sec["type"] == "show" else 1):
            for md in m.get("Media", []):
                for p in md.get("Part", []):
                    f = p.get("file")
                    if not f or f in sport or f in trashed or not os.path.exists(f): continue
                    if not any(f.startswith(r + "/") for r in roots): continue
                    title = "%s: %s" % (m.get("grandparentTitle") or sec["title"], m.get("title") or os.path.basename(f))
                    out.append(dict(id=iid("meta", "notsportarr", sec["title"], f), chk="meta", sev="att", title=title, library=sec["title"],
                                    detail={"kind": "notsportarr", "rk": m["ratingKey"], "path": f, "fixes": [], "section": sec["key"],
                                            "text": "Sportarr doesn't know this file, so it shows as missing there and won't be upgraded or replaced.",
                                            "note": "Import it in Sportarr (Wanted → Manual Import, pick this file), or Ignore if you keep it by hand."},
                                    links=[plex_link(m["ratingKey"])] + link))
    return out

# ---------------- smaller copy for space hogs (v19) ----------------
# "Find a smaller copy" on a film space hog: Radarr interactive search (nothing downloads), same resolution, >= 25% smaller,
# within Radarr's size limit. "Download this" grabs that exact release. The old file stays until the new one has imported;
# Radarr then moves it to its recycle bin (/media/.arr-recycle/<radarr>, cleaned after 7 days). If Radarr holds the download
# back as "not an upgrade" (same quality), the hourly follow-up does the manual import.
SMALLER_CACHE = {}                         # issue id -> {"t": time, "rel": {key: release}}
SMALLER_MAX_RATIO = 0.75                   # at least 25% smaller...
SMALLER_MIN_RATIO = 0.12                   # ...but not suspiciously tiny
OK_REJECT = ("cutoff", "not an upgrade", "existing file")

def release_flags(t):
    t, out = t.lower(), []
    if re.search(r"\b(dv|dovi|dolby[ .]?vision)\b", t): out.append("Dolby Vision")
    if "hdr10+" in t or "hdr10plus" in t: out.append("HDR10+")
    elif re.search(r"\bhdr(10)?\b", t): out.append("HDR")
    if "atmos" in t: out.append("Atmos")
    if re.search(r"truehd|dts-?hd|dts-?x|flac|lpcm", t): out.append("lossless audio")
    if re.search(r"remux", t): out.append("remux")
    return out

def _arr_long(a, path, timeout=300):
    return map_in(http(a["url"].rstrip("/") + "/api/v3" + path, {"X-Api-Key": a["key"]}, timeout=timeout), a)

def smaller_search(issue_id):
    with db() as c:
        row = c.execute("SELECT * FROM issues WHERE id=? AND chk='quality'", (issue_id,)).fetchone()
    if not row: return {"ok": False, "error": "That one isn't in the last scan any more. Scan again first."}
    d = json.loads(row["detail"]); ref = d.get("ref") or {}
    if "smaller" not in d.get("fixes", []) or ref.get("kind") != "movie": return {"ok": False, "error": "That fix doesn't apply here."}
    a = arr_by_name(ref["arr"])
    if not a: return {"ok": False, "error": "Unknown Radarr."}
    try:
        m = arr(a, "/movie/%d" % int(ref["id"]))
        mf = m.get("movieFile") or {}
        if not m.get("hasFile") or not mf.get("size"): return {"ok": False, "error": "%s doesn't have a file for it right now." % a["name"]}
        cur, res = mf["size"], ((mf.get("quality") or {}).get("quality") or {}).get("resolution")
        rel = _arr_long(a, "/release?movieId=%d" % int(ref["id"]))
    except Exception as e:
        return {"ok": False, "error": "Couldn't search in %s: %s" % (a["name"], e)}
    keep, out = {}, []
    for r in rel:
        q = (r.get("quality") or {}).get("quality") or {}
        if res and q.get("resolution") != res: continue
        if not (SMALLER_MIN_RATIO * cur <= (r.get("size") or 0) <= SMALLER_MAX_RATIO * cur): continue
        rej = [x for x in (r.get("rejections") or []) if not any(k in x.lower() for k in OK_REJECT)]
        if rej or r.get("downloadAllowed") is False: continue
        if r.get("protocol") == "torrent" and not (r.get("seeders") or 0): continue
        k = hashlib.sha1(("%s|%s" % (r.get("indexerId"), r.get("guid"))).encode()).hexdigest()[:12]
        same = re.sub(r"[^a-z0-9]", "", (r.get("title") or "").lower())   # the same release on several indexers: show it once
        if k in keep or any(re.sub(r"[^a-z0-9]", "", (x.get("title") or "").lower()) == same for x in keep.values()): continue
        keep[k] = r
        out.append({"key": k, "title": r.get("title", ""), "size": r["size"], "saves": cur - r["size"], "quality": q.get("name", ""),
                    "flags": release_flags(r.get("title", "")), "protocol": r.get("protocol", ""), "indexer": r.get("indexer", ""),
                    "age": r.get("age"), "seeders": r.get("seeders")})
        if len(out) >= 6: break
    SMALLER_CACHE[issue_id] = {"t": time.time(), "rel": keep, "arr": a["name"], "movie": int(ref["id"]), "cur": cur, "path": mf.get("path")}
    if not out:
        return {"ok": True, "results": [], "cur": cur, "msg": "%s found no release at the same resolution that's at least 25%% smaller and within your size limit. Try again in a few weeks." % a["name"]}
    return {"ok": True, "results": out, "cur": cur, "arr": a["name"]}

def smaller_grab(issue_id, key):
    cache = SMALLER_CACHE.get(issue_id)
    if not cache or time.time() - cache["t"] > 1800: return {"ok": False, "error": "The search results are too old. Search again."}
    r = cache["rel"].get(str(key))
    if not r: return {"ok": False, "error": "That release isn't in the last search. Search again."}
    with db() as c:
        row = c.execute("SELECT * FROM issues WHERE id=? AND chk='quality'", (issue_id,)).fetchone()
        busy = c.execute("SELECT 1 FROM done WHERE issue_id=? AND status='waiting' AND cleared=0", (issue_id,)).fetchone()
    if not row: return {"ok": False, "error": "That one isn't in the last scan any more. Scan again first."}
    if busy: return {"ok": False, "error": "A smaller copy is already on its way for this one."}
    a = arr_by_name(cache["arr"])
    try:
        arr(a, "/release", {"guid": r["guid"], "indexerId": r["indexerId"], "movieId": cache["movie"]}, "POST")
    except Exception as e:
        return {"ok": False, "error": "%s couldn't grab it: %s" % (a["name"], e)}
    ref = {"kind": "smaller", "arr": a["name"], "id": cache["movie"], "old_path": cache["path"], "old_size": cache["cur"],
           "new_title": r.get("title"), "new_size": r.get("size"), "manual_tried": False}
    note = "%s is downloading %s (%s, saves %s). It downloads in the night window; the old copy stays until the new one is in." % (
        a["name"], r.get("title"), human(r.get("size")), human(cache["cur"] - (r.get("size") or 0)))
    with db() as c:
        c.execute("INSERT INTO done (issue_id, title, library, kept, moved, trash_ids, links, at, status, note, chk, ref) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (issue_id, row["title"], row["library"], "", "[]", "[]", row["links"], now_iso(), "waiting", note, "quality", json.dumps(ref)))
        c.execute("DELETE FROM issues WHERE id=?", (issue_id,))
    SMALLER_CACHE.pop(issue_id, None)
    event("quality", "%s: asked %s for a smaller copy (%s)." % (row["title"], a["name"], r.get("title")))
    return {"ok": True}

def smaller_waiting_paths():
    with db() as c:
        rows = [r["ref"] for r in c.execute("SELECT ref FROM done WHERE chk='quality' AND status='waiting' AND cleared=0 AND ref IS NOT NULL")]
    out = set()
    for x in rows:
        try:
            j = json.loads(x)
            if j.get("kind") == "smaller" and j.get("old_path"): out.add(j["old_path"])
            if j.get("kind") == "tdarr": out.update(x["path"] for x in j.get("items", []))
        except Exception: pass
    return out

def smaller_followup():
    with db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM done WHERE chk='quality' AND status='waiting' AND cleared=0 AND ref IS NOT NULL")]
    for r in rows:
        try:
            ref = json.loads(r["ref"])
            if ref.get("kind") != "smaller": continue
            a = arr_by_name(ref["arr"]); mid = int(ref["id"])
            m = arr(a, "/movie/%d" % mid); mf = m.get("movieFile") or {}
            age = (datetime.now() - datetime.fromisoformat(r["at"])).total_seconds() / 86400
            st, note = "waiting", None
            if m.get("hasFile") and mf.get("path") and mf["path"] != ref["old_path"] and os.path.exists(mf["path"]) and (mf.get("size") or 0) < ref["old_size"]:
                st = "ok"
                note = "✓ Smaller copy in place: %s (%s, saved %s). The old copy is in %s's recycle bin for 7 days." % (
                    os.path.basename(mf["path"]), human(mf.get("size")), human(ref["old_size"] - (mf.get("size") or 0)), a["name"])
                event("quality", "%s: %s" % (r["title"], note))
            else:
                q = arr(a, "/queue/details?movieId=%d" % mid) or []
                q = q if isinstance(q, list) else q.get("records", [])
                item = next((x for x in q if x.get("movieId") == mid), None)
                if item and item.get("downloadId") and (item.get("trackedDownloadState") in ("importBlocked", "importPending") or
                                                         (item.get("status") == "completed" and item.get("trackedDownloadStatus") == "warning")):
                    cands = arr(a, "/manualimport?downloadId=%s&movieId=%d&filterExistingFiles=false" % (urllib.parse.quote(item["downloadId"]), mid)) or []
                    vids = [x for x in cands if os.path.splitext(x.get("path", ""))[1].lower() in VIDEO_EXT and "sample" not in x.get("path", "").lower()]
                    if vids and not ref.get("manual_tried"):
                        v = max(vids, key=lambda x: x.get("size") or 0)
                        arr(a, "/command", {"name": "ManualImport", "importMode": "auto", "files": [{
                            "path": v["path"], "movieId": mid, "quality": v.get("quality"), "languages": v.get("languages") or [],
                            "releaseGroup": v.get("releaseGroup") or "", "downloadId": item["downloadId"]}]}, "POST")
                        ref["manual_tried"] = True
                        with db() as c: c.execute("UPDATE done SET ref=? WHERE id=?", (json.dumps(ref), r["id"]))
                        note = "Downloaded. %s held it back as \"not an upgrade\" (same quality), so Library Doctor imported it. Checking again next hour." % a["name"]
                    elif ref.get("manual_tried"):
                        msgs = "; ".join(m2 for s2 in item.get("statusMessages") or [] for m2 in s2.get("messages") or [])
                        st, note = "warn", "Downloaded, but the import didn't go through: %s. Open %s → Activity to import it by hand." % (msgs or "no reason given", a["name"])
                elif item:
                    pct = 100 - 100 * (item.get("sizeleft") or 0) / max(1, item.get("size") or 1)
                    note = "Downloading in %s: %.0f%% (%s). The old copy stays until it's in. Checked %s." % (a["name"], pct, item.get("status", ""), datetime.now().strftime("%a %H:%M"))
                elif age > FOLLOW_DAYS:
                    st, note = "warn", "No smaller copy arrived after %d days. The release may have failed: try Find a smaller copy again and pick another." % FOLLOW_DAYS
                else:
                    note = "Waiting for %s to start the download (night window). Checked %s." % (a["name"], datetime.now().strftime("%a %H:%M"))
            if note:
                with db() as c: c.execute("UPDATE done SET status=?, note=?, checked_at=? WHERE id=?", (st, note, now_iso(), r["id"]))
        except Exception as e:
            log("smaller followup %s failed: %r" % (r["id"], e))

# ---------------- Convert with Tdarr (v20) ----------------
# For space hogs / old formats that aren't HEVC/AV1 yet: hand the file(s) to Tdarr. If Tdarr has never seen a file (its folder
# watcher missed it), scan-files adds it to the right library; if it has, its record is set back to Queued. Tdarr's own
# plugin stack then converts it in its normal queue. The hourly follow-up checks the codec with ffprobe.
TDARR_DONE_CODECS = ("hevc", "av1")
_TDARR_LIBS = {"t": 0, "libs": []}

def tdarr_call(path, data, timeout=60):
    t = S.get("tdarr") or {}
    req = urllib.request.Request(t["url"].rstrip("/") + path, data=json.dumps({"data": map_out(data, t)}).encode(),
                                 headers={"Content-Type": "application/json", "x-api-key": t.get("key", "")})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        b = r.read().decode()
    return map_in(json.loads(b), t, extra=("_id",)) if b.strip()[:1] in ("{", "[") else b

def tdarr_libs():
    if not S.get("tdarr"): return []
    if time.time() - _TDARR_LIBS["t"] > 600:
        try:
            _TDARR_LIBS["libs"] = [(l["folder"].rstrip("/"), l["_id"], l.get("name", "")) for l in tdarr_call("/api/v2/cruddb", {"collection": "LibrarySettingsJSONDB", "mode": "getAll"}) if l.get("folder")]
        except Exception as e:
            log("tdarr libraries failed: %r" % e)
        _TDARR_LIBS["t"] = time.time()
    return _TDARR_LIBS["libs"]

def tdarr_lib_for(path):
    hit = [l for l in tdarr_libs() if path.startswith(l[0] + "/")]
    return max(hit, key=lambda l: len(l[0])) if hit else None

def _tdarr_has(p):
    try: doc = tdarr_call("/api/v2/cruddb", {"collection": "FileJSONDB", "mode": "getById", "docID": p})
    except Exception: return False
    return isinstance(doc, dict) and bool(doc.get("_id"))

def tdarr_send(by_lib, wait=45):
    """Add files Tdarr has no record of. v22: mode scanFolderWatcher (scanFindNew silently skips listed files on
    Tdarr 3.06). Then poll until Tdarr has a record of each one; returns the paths it actually added."""
    for lib_id, ps in by_lib.items():               # one scan per library, however many files
        tdarr_call("/api/v2/scan-files", {"scanConfig": {"dbID": lib_id, "arrayOrPath": ps, "mode": "scanFolderWatcher"}})
    left = [p for ps in by_lib.values() for p in ps]; got = set(); end = time.time() + wait
    while left and time.time() < end:
        time.sleep(3)
        for p in list(left):
            if _tdarr_has(p): got.add(p); left.remove(p)
    return got

def tdarr_fix(issue_id, row, d):
    paths = [p for p in d.get("tdarr_paths", []) if safe_media_path(p) and os.path.isfile(p)]
    if not paths: return {"ok": False, "error": "The file isn't where it was. Scan again."}
    items, added, requeued, new_by_lib = [], 0, 0, {}
    try:
        for p in paths:
            lib = tdarr_lib_for(p)
            if not lib: return {"ok": False, "error": "No Tdarr library covers %s." % os.path.dirname(p)}
            doc = tdarr_call("/api/v2/cruddb", {"collection": "FileJSONDB", "mode": "getById", "docID": p})
            if isinstance(doc, dict) and doc.get("_id"):
                tdarr_call("/api/v2/cruddb", {"collection": "FileJSONDB", "mode": "update", "docID": p, "obj": {"TranscodeDecisionMaker": "Queued"}}); requeued += 1
            else:
                new_by_lib.setdefault(lib[1], []).append(p); added += 1
            it = {"path": p, "size": os.path.getsize(p), "lib": lib[2]}
            aref = _arr_ref_for_path(p)
            if aref: it["arr"] = aref
            items.append(it)
        got = tdarr_send(new_by_lib) if new_by_lib else set()
        skipped = added - len(got); added = len(got)
    except Exception as e:
        return {"ok": False, "error": "Tdarr didn't answer: %s" % e}
    ref = {"kind": "tdarr", "items": items, "refs": d.get("refs") or ([{"arr": d["ref"]["arr"], "kind": "movie" if d["ref"]["kind"] == "movie" else "series", "id": d["ref"]["id"]}] if d.get("ref") else []),
           "section": d.get("section")}
    n = len(items)
    note = "Sent %s to Tdarr (%s). It converts in its normal queue; checked every hour." % (
        "1 file" if n == 1 else "%d files" % n, ", ".join(x for x in ["%d added to its library" % added if added else "", "%d queued again" % requeued if requeued else "",
                                      "%d skipped by Tdarr, retried on the next check" % skipped if skipped else ""] if x))
    with db() as c:
        c.execute("INSERT INTO done (issue_id, title, library, kept, moved, trash_ids, links, at, status, note, chk, ref) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (issue_id, row["title"], row["library"], "", "[]", "[]", row["links"], now_iso(), "waiting", note, "quality", json.dumps(ref)))
        c.execute("DELETE FROM issues WHERE id=?", (issue_id,))
    event("quality", "%s: %s" % (row["title"], note))
    return {"ok": True}

def _vcodec(p):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name", "-of", "csv=p=0", p], capture_output=True, text=True, timeout=60)
        return r.stdout.strip().splitlines()[0].strip() if r.stdout.strip() else ""
    except Exception: return ""

def _tdarr_current(p):
    """Tdarr may change the container: find the file at its old path or with the same name and another extension."""
    if os.path.exists(p): return p
    stem, folder = os.path.splitext(os.path.basename(p))[0], os.path.dirname(p)
    try:
        for n in os.listdir(folder):
            if os.path.splitext(n)[0] == stem and os.path.splitext(n)[1].lower() in VIDEO_EXT: return os.path.join(folder, n)
    except OSError: pass
    return None

_ARR_LISTS = {"t": 0, "lists": []}

def _arr_ref_for_path(path):
    """v23: the Radarr film / Sonarr series a file belongs to (by folder), for a rescan after Tdarr converts it.
    The film/series lists are cached for 10 minutes so a batch of files costs one call per arr."""
    if time.time() - _ARR_LISTS["t"] > 600:
        lists = []
        for a in S.get("arrs", []):
            try:
                items = arr(a, "/movie" if a["kind"] == "radarr" else "/series")
                lists.append((a, [(x["path"].rstrip("/") + "/", x["id"]) for x in items if x.get("path")]))
            except Exception as e:
                log("arr list %s failed: %r" % (a["name"], e))
        _ARR_LISTS.update(t=time.time(), lists=lists)
    for a, items in _ARR_LISTS["lists"]:
        for folder, i in items:
            if path.startswith(folder): return {"arr": a["name"], "kind": "movie" if a["kind"] == "radarr" else "series", "id": i}
    return None

def _tdarr_workers():
    """path -> percent for files Tdarr's workers are converting right now."""
    out = {}
    try:
        t = S["tdarr"]
        req = urllib.request.Request(t["url"].rstrip("/") + "/api/v2/get-nodes", headers={"x-api-key": t.get("key", "")})
        with urllib.request.urlopen(req, timeout=20) as resp: nodes = json.loads(resp.read().decode() or "{}")
        for n in (nodes or {}).values():
            for w in (n.get("workers") or {}).values():
                if w.get("file"): out[w["file"]] = round(float(w.get("percentage") or 0))
    except Exception: pass
    return out

def tdarr_followup():
    """v23: each file is handled as soon as Tdarr has converted it (Radarr/Sonarr rescan + Plex refresh, once),
    not when the whole batch is done; per-file states are kept in the row's ref for the Done list."""
    if not S.get("tdarr"): return
    with db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM done WHERE chk='quality' AND status='waiting' AND cleared=0 AND ref LIKE '%\"tdarr\"%'")]
    if not rows: return
    busy = _tdarr_workers()
    for r in rows:
        try:
            ref = json.loads(r["ref"])
            if ref.get("kind") != "tdarr": continue
            items, n = ref["items"], len(ref["items"])
            single = (ref.get("refs") or [None])[0] if n == 1 else None      # a one-film send already knows its Radarr film
            missing_by_lib, newly = {}, []
            for it in items:
                if it.get("state") == "done": continue
                cur = _tdarr_current(it["path"])
                doc = {}
                try: doc = tdarr_call("/api/v2/cruddb", {"collection": "FileJSONDB", "mode": "getById", "docID": cur or it["path"]}) or {}
                except Exception: pass
                has = isinstance(doc, dict) and bool(doc.get("_id"))
                state = doc.get("TranscodeDecisionMaker", "") if has else ""
                codec = _vcodec(cur) if cur else ""
                if cur and codec in TDARR_DONE_CODECS:
                    it.update(state="done", after=os.path.getsize(cur), done_at=now_iso(), now=cur); it.pop("pct", None)
                    newly.append(it)
                elif "error" in state.lower():
                    it["state"] = "failed"
                elif cur and not has:
                    lib = tdarr_lib_for(cur)
                    if lib: missing_by_lib.setdefault(lib[1], []).append(cur)
                    it["state"] = "notin"
                else:
                    it["state"] = "converting" if (cur or it["path"]) in busy else "queued"
                    if it["state"] == "converting": it["pct"] = busy[cur or it["path"]]
                    else: it.pop("pct", None)
            if missing_by_lib:
                try: got = tdarr_send(missing_by_lib, wait=30)
                except Exception: got = set()
                for it in items:
                    if it.get("state") == "notin" and (_tdarr_current(it["path"]) or it["path"]) in got: it["state"] = "queued"
            # straight away for each newly converted file: tell Radarr/Sonarr (it picks up the new size/codec and renames) and Plex
            for it in newly:
                aref = it.get("arr") or single or _arr_ref_for_path(it["now"])
                if aref:
                    it["arr"] = aref
                    try: arr_rescan([aref])
                    except Exception as e: log("tdarr rescan failed: %r" % e)
                try: plex_refresh_path(it["now"])
                except Exception as e: log("tdarr plex refresh failed: %r" % e)
                event("quality", "%s: Tdarr converted it to HEVC, %s → %s (saved %s)." % (
                    os.path.basename(os.path.dirname(it["now"])) or os.path.basename(it["now"]), human(it["size"]), human(it["after"]), human(it["size"] - it["after"])))
            done = [it for it in items if it.get("state") == "done"]
            failed = [it for it in items if it.get("state") == "failed"]
            conv = [it for it in items if it.get("state") == "converting"]
            notin = [it for it in items if it.get("state") == "notin"]
            before, after = sum(it["size"] for it in done), sum(it.get("after", 0) for it in done)
            saved = (" (saved %s)" % human(before - after)) if done else ""
            age = (datetime.now() - datetime.fromisoformat(r["at"])).total_seconds() / 86400
            if len(done) == n:
                st, note = "ok", "✓ Tdarr converted %s to HEVC: %s → %s (saved %s)." % ("it" if n == 1 else "all %d" % n, human(before), human(after), human(before - after))
                event("quality", "%s: %s" % (r["title"], note))
            elif failed and len(done) + len(failed) == n:
                st, note = "warn", "Tdarr converted %d of %d%s; it couldn't convert %s. Open Tdarr → the library's Error/Cancelled tab to see why." % (
                    len(done), n, saved, ", ".join(os.path.basename(it["path"]) for it in failed[:3]))
            elif age > FOLLOW_DAYS:
                st, note = "warn", "After %d days Tdarr has converted %d of %d%s. Check Tdarr's queue and schedule." % (FOLLOW_DAYS, len(done), n, saved)
            else:
                st = "waiting"
                bits = ["%d converting" % len(conv) if conv else "", "%d failed" % len(failed) if failed else "",
                        "%d not in Tdarr yet (sent again)" % len(notin) if notin else ""]
                note = "Tdarr: %d of %d converted%s%s. Checked %s." % (len(done), n, saved, "".join(", " + b for b in bits if b), datetime.now().strftime("%a %H:%M"))
            with db() as c: c.execute("UPDATE done SET status=?, note=?, checked_at=?, ref=? WHERE id=?", (st, note, now_iso(), json.dumps(ref), r["id"]))
        except Exception as e:
            log("tdarr followup %s failed: %r" % (r["id"], e))

def tdarr_pending():
    with db() as c:
        return c.execute("SELECT COUNT(*) FROM done WHERE chk='quality' AND status='waiting' AND cleared=0 AND ref LIKE '%\"tdarr\"%'").fetchone()[0] > 0

# ---------------- files Tdarr never saw (v21) ----------------
# Tdarr's folder watcher sometimes misses a file (e.g. added while Tdarr was down), so it's never converted. Compare every video
# in each Tdarr library folder with Tdarr's database and list the ones it has no record of that aren't HEVC/AV1 yet.
# Files Radarr/Sonarr renamed after Tdarr converted them also have no record, but they're already HEVC, so they aren't listed.
QUALITY_CODECS = {}                       # path -> Plex video codec, filled by quality_scan
EXTRA_RE2 = re.compile(r"(-trailer|-sample|-featurette|-behindthescenes|-deleted|-interview|-scene|-short|-other)\.[a-z0-9]+$", re.I)
EXTRA_DIRS = {"trailers", "featurettes", "extras", "behind the scenes", "deleted scenes", "interviews", "scenes", "shorts", "other", "samples"}

# ---------------- files Tdarr failed to convert (v31) ----------------
# Tdarr's "Transcode error" files never reached the phone before. Each card lists the files with the reason read from the
# latest job report (Tdarr API list-footprintId-reports + read-job-file, keyed by the record's footprintId). "Retry" reuses
# tdarr_fix (sets the record back to Queued; the 10-minute follow-up then shows ✓ or "couldn't convert").
TDARR_REASONS = [
    (re.compile(r"Subtitle codec \d+ is not supported|codec not currently supported in container", re.I), "a subtitle track ffmpeg can't copy"),
    (re.compile(r"No space left on device", re.I), "the disk or Tdarr's cache ran out of space"),
    (re.compile(r"Permission denied|Operation not permitted", re.I), "a permission problem"),
    (re.compile(r"Invalid data found when processing input|moov atom not found|Error while decoding|corrupt", re.I), "the file is damaged"),
    (re.compile(r"OpenEncodeSessionEx|CUDA_ERROR|No NVENC capable devices|nvenc.*(error|failed)|Cannot load libcuda", re.I), "the NVIDIA GPU encoder failed"),
    (re.compile(r"Cannot allocate memory|out of memory", re.I), "it ran out of memory"),
    (re.compile(r"Could not write header", re.I), "ffmpeg couldn't write the new file (a track it can't copy)"),
]
_TDARR_ERRLINE = re.compile(r"\] (?!.*Tdarr)(.*(error|failed|invalid|not supported).*)$", re.I)

def tdarr_reason(fp):
    """Best guess at why the last Tdarr transcode of this footprint failed, in plain words ('' if no report)."""
    if not fp: return ""
    try:
        reps = tdarr_call("/api/v2/list-footprintId-reports", {"footprintId": fp}, timeout=20)
        reps = [r for r in (reps if isinstance(reps, list) else []) if "()transcode()" in r]
        if not reps: return ""
        jf = max(reps, key=lambda r: int(re.sub(r"\D", "", r.rsplit("()", 1)[-1]) or 0))
        res = tdarr_call("/api/v2/read-job-file", {"footprintId": fp, "jobId": jf.split("()")[3], "jobFileId": jf}, timeout=30)
        txt = res.get("text", "") if isinstance(res, dict) else ""
    except Exception:
        return ""
    for rx, why in TDARR_REASONS:
        if rx.search(txt): return why
    for line in reversed(txt.splitlines()):
        m = _TDARR_ERRLINE.search(line.strip())
        if m and "Conversion failed" not in m.group(1): return m.group(1).strip()[:160]
    return "no clear reason in the job report"

def tdarr_failed_cards(recs, trashed, pending):
    libs = {lib_id: name for folder, lib_id, name in tdarr_libs()}
    by_lib = {}
    for f in recs:
        if "error" not in str(f.get("TranscodeDecisionMaker", "")).lower(): continue
        p = f["_id"]
        if p in trashed or p in pending or not os.path.isfile(p): continue
        by_lib.setdefault(f.get("DB") or "", []).append(f)
    out, looked = [], 0
    for lib_id, fs in by_lib.items():
        name = libs.get(lib_id, lib_id or "?")
        items = []
        for f in sorted(fs, key=lambda x: x["_id"]):
            why = ""
            if looked < 40:
                looked += 1; why = tdarr_reason(f.get("footprintId"))
            try: size = os.path.getsize(f["_id"])
            except OSError: size = 0
            items.append({"path": f["_id"], "name": os.path.relpath(f["_id"], os.path.commonpath([x["_id"] for x in fs]) if len(fs) > 1 else os.path.dirname(f["_id"])),
                          "size": size, "why": why or "not checked"})
        reasons = {}
        for x in items: reasons[x["why"]] = reasons.get(x["why"], 0) + 1
        top = sorted(reasons.items(), key=lambda kv: -kv[1])
        why_txt = "; ".join("%s (%d)" % (w, n) if len(top) > 1 else w for w, n in top[:3])
        n = len(items)
        out.append(dict(id=iid("quality", "tdarrfail", lib_id), chk="quality", sev="att", library="Tdarr: " + name,
                        title="Tdarr couldn't convert %d file%s" % (n, "" if n == 1 else "s"),
                        detail={"kind": "tdarrfail", "count": n, "items": items[:80], "reason": top[0][0] if top else "",
                                "tdarr_paths": [x["path"] for x in items], "fixes": ["tdarr"],
                                "text": "Reason: %s." % why_txt,
                                "note": "The files still play; they just weren't made smaller. Retry puts them back in Tdarr's queue (for “a subtitle track ffmpeg can't copy”, add the Drop Unknown Subs plugin from the extras/tdarr folder on GitHub to the library's stack first). The result shows under Done."},
                        links=[{"label": "Open Tdarr", "url": S["tdarr"]["public"]}] if S["tdarr"].get("public") else []))
    return out

def tdarr_missed_scan(trashed, skip):
    if not S.get("tdarr"): return []
    STATE["step"] = "Checking what Tdarr has seen"
    try:
        recs = [f for f in tdarr_call("/api/v2/cruddb", {"collection": "FileJSONDB", "mode": "getAll"}, timeout=300) if f.get("_id")]
    except Exception as e:
        log("tdarr missed check failed: %r" % e); return []
    known = {f["_id"] for f in recs}
    pending = smaller_waiting_paths()
    out = []
    try: out += tdarr_failed_cards(recs, trashed, pending)          # v31
    except Exception as e: log("tdarr failed check failed: %r" % e)
    for folder, lib_id, name in tdarr_libs():
        items = []
        for dp, dn, fn in os.walk(folder):
            dn[:] = [d for d in dn if not d.startswith(".") and d != "Plex Versions" and d.lower() not in EXTRA_DIRS]
            for f in fn:
                p = os.path.join(dp, f)
                if f.startswith(".") or not f.lower().endswith(VIDEO_EXT) or EXTRA_RE2.search(f): continue
                if p in known or p in trashed or p in pending or p in skip or is_placeholder(p): continue
                codec = QUALITY_CODECS.get(p)
                if codec is None or codec in TDARR_DONE_CODECS: continue          # not in Plex (extras) / already converted
                try:
                    st = os.stat(p)
                    if time.time() - st.st_mtime < 7200: continue                   # brand new: Tdarr's watcher may still pick it up
                except OSError: continue
                items.append({"path": p, "name": os.path.relpath(p, folder), "size": st.st_size, "codec": codec})
        if not items: continue
        items.sort(key=lambda x: -x["size"]); total = sum(x["size"] for x in items)
        out.append(dict(id=iid("quality", "tdarrmissed", lib_id), chk="quality", sev="att", library="Tdarr: " + name,
                        title="%d file%s Tdarr never saw" % (len(items), "" if len(items) == 1 else "s"),
                        detail={"kind": "tdarrmissed", "count": len(items), "total": total, "items": items[:80],
                                "tdarr_paths": [x["path"] for x in items], "fixes": ["tdarr"],
                                "text": "Together %s, still %s. Tdarr has no record of them (its folder watcher missed them), so they were never converted." % (
                                    human(total), "/".join(sorted({x["codec"].upper() for x in items}))),
                                "note": "Send all to Tdarr adds them to its %s library so its normal queue converts them, usually to about a third of the size. Checked every hour." % name},
                        links=[{"label": "Open Tdarr", "url": S["tdarr"]["public"]}] if S["tdarr"].get("public") else []))
    return out

# ---------------- Downloads: Radarr/Sonarr queues (v33) ----------------
# 11-15: 4 better copies sat "import blocked" for 12 h, a dead torrent never started and a 4-month-old download no arr knew
# about kept 12.6 GB, and nothing said so. queue_cards() reads every arr's queue and makes a card per problem:
#   blocked  - downloaded but the arr won't import it (name mismatch, "matched by ID", …) -> Import (manual import, only files
#              the arr itself has no objection to; otherwise it says why and links to the arr)
#   stalled  - torrent still at 0% after a night download window, or the arr/client says failed -> Remove & try another
#              (remove + blocklist, then an explicit search because unmonitored items are never re-searched)
#   leftover - finished download no Radarr/Sonarr knows about, over 7 days old -> Move to trash (qBittorrent forgets it, the
#              files go to Library Doctor's trash; private torrents and LD_PROTECT_TAGS are left alone)
QUEUE_LEFTOVER_DAYS = 7
# If your downloads only run in a nightly window, set LD_DOWNLOAD_WINDOW_END to the hour it ends (e.g. 7 for 00:00-07:00):
# a torrent then counts as stuck once a whole window has passed. Unset = stuck after 24 hours at 0%.
_DWE = os.environ.get("LD_DOWNLOAD_WINDOW_END", "").strip()
NIGHT_END_HOUR = int(_DWE) if _DWE.isdigit() and int(_DWE) < 24 else None
QUEUE_NOTIFY_HOUR = NIGHT_END_HOUR if NIGHT_END_HOUR is not None else int(os.environ.get("LD_QUEUE_NOTIFY_HOUR", "8"))
# torrents with any of these qBittorrent tags are never offered for removal (private torrents never are either)
PROTECT_TAGS = {t.strip().lower() for t in os.environ.get("LD_PROTECT_TAGS", "").split(",") if t.strip()}

def _protected(qb):
    return bool(qb.get("private")) or bool(PROTECT_TAGS & {t.strip().lower() for t in (qb.get("tags") or "").split(",")})

def _arr_queue(a):
    inc = "includeMovie=true&includeUnknownMovieItems=true" if a["kind"] == "radarr" else "includeSeries=true&includeEpisode=true&includeUnknownSeriesItems=true"
    return arr(a, "/queue?pageSize=500&" + inc).get("records", [])

def _qbit():
    q = S.get("qbit") or {}
    if not q.get("url"): return None
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_cookiejar.CookieJar()))
    op.open(q["url"] + "/api/v2/auth/login", data=urllib.parse.urlencode({"username": q["username"], "password": q["password"]}).encode(), timeout=15).read()
    return op

def _qbit_info(op, h):
    try: return (json.loads(op.open(S["qbit"]["url"] + "/api/v2/torrents/info?hashes=" + h.lower(), timeout=20).read() or b"[]") or [None])[0]
    except Exception: return None

def _night_passed(added):
    """True once a whole download window has passed since the item was added (or 24 h with no window set)."""
    try: t = datetime.fromisoformat(str(added).replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
    except Exception: return False
    if NIGHT_END_HOUR is None: return datetime.now() - t > timedelta(hours=24)
    first_night = (t + timedelta(days=1)).replace(hour=NIGHT_END_HOUR, minute=0, second=0, microsecond=0) if t.hour >= NIGHT_END_HOUR else t.replace(hour=NIGHT_END_HOUR, minute=0, second=0, microsecond=0)
    return datetime.now() > first_night + timedelta(minutes=30)

def _q_msgs(r):
    m = [x for sm in r.get("statusMessages") or [] for x in sm.get("messages") or []]
    if r.get("errorMessage"): m.append(r["errorMessage"])
    return "; ".join(dict.fromkeys(m))[:300]

def _q_label(r):
    ep = r.get("episode")
    return "S%02dE%02d" % (ep["seasonNumber"], ep["episodeNumber"]) if ep else ""

def queue_cards():
    out, qop = [], None
    for a in S.get("arrs", []):
        try: recs = _arr_queue(a)
        except Exception as e:
            log("queue %s failed: %r" % (a["name"], e)); continue
        groups = {}
        for r in recs:
            state, status = r.get("trackedDownloadState") or "", (r.get("status") or "").lower()
            tstat, msgs = r.get("trackedDownloadStatus") or "", _q_msgs(r)
            owner = r.get("movie") or r.get("series") or {}
            known = bool(r.get("movieId") or r.get("seriesId"))
            size, left = r.get("size") or 0, r.get("sizeleft") or 0
            kind = None
            if known and state in ("importBlocked", "importPending") and tstat == "warning" and status == "completed":
                kind = "blocked"
            elif known and (state == "failedPending" or status in ("failed", "warning") and "fail" in msgs.lower()):
                kind = "stalled"
            elif known and r.get("protocol") == "torrent" and state == "downloading" and (size == 0 or left >= size) and _night_passed(r.get("added")):
                kind = "stalled"
            elif not known and status == "completed":
                if r.get("protocol") == "torrent" and r.get("downloadId"):
                    if qop is None:
                        try: qop = _qbit() or False
                        except Exception as e: log("qbit login failed: %r" % e); qop = False
                    info = _qbit_info(qop, r["downloadId"]) if qop else None
                    age = (time.time() - info["completion_on"]) / 86400 if info and info.get("completion_on", 0) > 0 else 0
                    if info and age >= QUEUE_LEFTOVER_DAYS:
                        kind = "leftover"
                        r["_qb"] = {"private": bool(info.get("private")), "tags": info.get("tags", ""), "path": info.get("content_path", ""),
                                    "ratio": round(info.get("ratio", 0), 2), "days": int(age), "state": info.get("state")}
            if not kind: continue
            key = (kind, owner.get("id") or r.get("downloadId"))
            g = groups.setdefault(key, {"kind": kind, "owner": owner, "items": []})
            it = {"qid": r["id"], "downloadId": r.get("downloadId"), "title": r.get("title", ""), "ep": _q_label(r),
                  "episodeId": r.get("episodeId"), "movieId": r.get("movieId"), "seriesId": r.get("seriesId"),
                  "protocol": r.get("protocol"), "client": r.get("downloadClient"), "size": size, "left": left,
                  "added": r.get("added"), "msg": msgs, "path": r.get("outputPath") or ""}
            if r.get("_qb"): it["qb"] = r["_qb"]
            g["items"].append(it)
        for (kind, oid), g in groups.items():
            items, owner = g["items"], g["owner"]
            n = len(items)
            name = (owner.get("title") or items[0]["title"]) if kind != "leftover" else items[0]["title"]
            eps = ", ".join(x["ep"] for x in items if x["ep"])
            links = [arr_link(a, owner, "movie" if a["kind"] == "radarr" else "series")] if owner.get("titleSlug") else []
            links.append({"label": "Open %s queue" % a["name"], "url": a["public"].rstrip("/") + "/activity/queue"})
            if kind == "blocked":
                title = "%s%s: %s waiting to import" % (name, " " + eps if eps and n <= 4 else "", "1 download" if n == 1 else "%d downloads" % n)
                text = "%s downloaded %s but won't import %s by itself: %s" % (a["name"], "it" if n == 1 else "them", "it" if n == 1 else "them", items[0]["msg"] or "import blocked")
                note = "Import asks %s to import %s now, checking first that %s has no objection to the file. The old copy goes to its recycle bin." % (a["name"], "it" if n == 1 else "them", a["name"])
                fixes = ["import"]
            elif kind == "stalled":
                title = "%s%s: %s" % (name, " " + eps if eps and n <= 4 else "", "download stuck" if n == 1 else "%d downloads stuck" % n)
                text = (("Still at 0%% after %s, so probably nobody is sharing it. " % ("a night's download window" if NIGHT_END_HOUR is not None else "a whole day")) if items[0]["left"] >= items[0]["size"] else "") + (items[0]["msg"] or "")
                note = "Remove & try another removes it, blocklists that release and searches for a different one (it downloads in the night window)."
                fixes = ["retry"]
            else:
                qb = items[0].get("qb") or {}
                prot = _protected(qb)
                title = "Leftover download: %s" % name[:90]
                text = "Finished %d days ago (%s, ratio %s) and no Radarr/Sonarr knows about it, so it's just taking %s." % (qb.get("days", 0), human(items[0]["size"]), qb.get("ratio", 0), human(items[0]["size"]))
                note = ("It's on a private tracker (or has a protected tag), so it's left to its seeding rules." if prot else
                        "Move to trash removes it from qBittorrent and puts the files in Library Doctor's trash (deleted after %d days). If you still want it, add the film or show in %s instead." % (TRASH_DAYS, a["name"]))
                fixes = [] if prot else ["remove"]
            out.append(dict(id=iid("queue", kind, a["name"], oid), chk="queue", sev="bad" if kind == "stalled" else "att", title=title, library=a["name"],
                            detail={"kind": kind, "arr": a["name"], "items": items, "count": n, "fixes": fixes, "text": text, "note": note,
                                    "size": sum(x["size"] for x in items)},
                            links=links))
    return out

def queue_refresh(notify=False):
    """Between scans: replace only the Downloads cards (keeps first_seen), optionally tell Gotify about new ones."""
    cards = queue_cards()
    now = now_iso()
    with db() as c:
        seen = {r["id"]: r["first_seen"] for r in c.execute("SELECT id, first_seen FROM issues WHERE chk='queue'")}
        sid = (c.execute("SELECT MAX(id) FROM scans").fetchone()[0] or 0)
        c.execute("DELETE FROM issues WHERE chk='queue'")
        for it in cards:
            c.execute("INSERT OR REPLACE INTO issues VALUES (?,?,?,?,?,?,?,?,?,?)", (it["id"], sid, "queue", it["sev"], it["title"], it["library"],
                      json.dumps(it["detail"]), json.dumps(it["links"]), seen.get(it["id"], now), now))
        hidden = hidden_ids(c)
    new = [it for it in cards if it["id"] not in seen and it["id"] not in hidden]
    if notify and new and notify_channels():
        try: globals()["notify"]("Library Doctor: %d download%s need%s you" % (len(new), "" if len(new) == 1 else "s", "s" if len(new) == 1 else ""),
                         "\n\n".join("**%s** (%s): %s" % (it["title"], it["library"], it["detail"]["text"][:200]) for it in new[:6]), 5)
        except Exception as e: log("queue gotify failed: %r" % e)
    return len(cards)

def _manual_import(a, it):
    """Import one queue item's files the arr has no objection to. Returns (n_imported, problem or '')."""
    q = {"downloadId": it["downloadId"], "filterExistingFiles": "false"}
    if a["kind"] == "radarr": q["movieId"] = it["movieId"]
    else: q["seriesId"] = it["seriesId"]
    cands = arr(a, "/manualimport?" + urllib.parse.urlencode(q))
    vids = [x for x in cands if (x.get("size") or 0) > 50 * 1024 * 1024 and os.path.splitext(x.get("path", ""))[1].lower() in VIDEO_EXT]
    if not vids: return 0, "no video file found in the download"
    bad = [j.get("reason", "") for x in vids for j in x.get("rejections") or []]
    if bad: return 0, "%s refuses: %s" % (a["name"], bad[0][:160])
    files = []
    for x in vids:
        f = {"path": x["path"], "quality": x.get("quality"), "languages": x.get("languages") or [], "releaseGroup": x.get("releaseGroup"),
             "downloadId": it["downloadId"], "indexerFlags": x.get("indexerFlags", 0)}
        if a["kind"] == "radarr":
            f["movieId"] = (x.get("movie") or {}).get("id") or it["movieId"]
        else:
            eids = [e["id"] for e in x.get("episodes") or []] or ([it["episodeId"]] if it.get("episodeId") else [])
            if not eids: return 0, "%s couldn't tell which episode it is" % a["name"]
            f.update(seriesId=it["seriesId"], episodeIds=eids, folderName=x.get("folderName"), releaseType=x.get("releaseType", "singleEpisode"))
        files.append(f)
    cmd = arr(a, "/command", {"name": "ManualImport", "importMode": "auto", "files": files}, "POST")
    for _ in range(40):
        time.sleep(3)
        st = arr(a, "/command/%d" % cmd["id"])
        if st.get("status") in ("completed", "failed", "aborted"): break
    if st.get("status") != "completed": return 0, "%s's import %s: %s" % (a["name"], st.get("status", "timed out"), (st.get("exception") or st.get("message") or "")[:160])
    return len(files), ""

def _retry(a, it):
    arr(a, "/queue/%d?removeFromClient=true&blocklist=true&skipRedownload=false" % it["qid"], None, "DELETE")
    if a["kind"] == "radarr" and it.get("movieId"):
        arr(a, "/command", {"name": "MoviesSearch", "movieIds": [int(it["movieId"])]}, "POST")
    elif it.get("episodeId"):
        arr(a, "/command", {"name": "EpisodeSearch", "episodeIds": [int(it["episodeId"])]}, "POST")

def _remove_leftover(it):
    qb = it.get("qb") or {}
    if _protected(qb): return "private or protected torrent: left to its seeding rules"
    op = _qbit()
    if not op: return "qBittorrent isn't set up in Library Doctor"
    info = _qbit_info(op, it["downloadId"])
    if not info: return "qBittorrent doesn't have it any more"
    src = info.get("content_path") or ""
    if not src.startswith(MEDIA_ROOT + "/downloads/") or not os.path.exists(src): return "its files aren't under /media/downloads"
    op.open(S["qbit"]["url"] + "/api/v2/torrents/delete", data=urllib.parse.urlencode({"hashes": it["downloadId"].lower(), "deleteFiles": "false"}).encode(), timeout=20).read()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S") + "-leftover-" + it["downloadId"][:8].lower()
    files = [src] if os.path.isfile(src) else [os.path.join(d, f) for d, _, fs in os.walk(src) for f in fs]
    for f in files:
        dst = os.path.join(TRASH_DIR, stamp, os.path.relpath(f, MEDIA_ROOT))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        size = os.path.getsize(f); move_file(f, dst)
        with db() as c: c.execute("INSERT INTO trash (issue_id, title, src, dst, size, reason, moved_at) VALUES (?,?,?,?,?,?,?)",
                                  ("queue", it["title"][:200], f, dst, size, "Leftover download (no Radarr/Sonarr knew it)", now_iso()))
    if os.path.isdir(src):
        for d, dn, fn in sorted(os.walk(src, topdown=False), key=lambda x: -len(x[0])):
            try: os.rmdir(d)
            except OSError: pass
    return ""

def queue_fix(issue_id, how):
    with db() as c:
        row = c.execute("SELECT * FROM issues WHERE id=? AND chk='queue'", (issue_id,)).fetchone()
    if not row: return {"ok": False, "error": "That one isn't in the queue list any more. Refresh first."}
    d = json.loads(row["detail"])
    if how not in d.get("fixes", []): return {"ok": False, "error": "That fix doesn't apply here."}
    a = arr_by_name(d.get("arr"))
    if not a: return {"ok": False, "error": "Unknown Radarr/Sonarr."}
    okn, probs = 0, []
    for it in d["items"]:
        try:
            if how == "import":
                n, why = _manual_import(a, it)
                if n: okn += 1
                else: probs.append("%s: %s" % (it["ep"] or it["title"][:50], why))
            elif how == "retry":
                _retry(a, it); okn += 1
            elif how == "remove":
                why = _remove_leftover(it)
                if why: probs.append(why)
                else: okn += 1
        except Exception as e:
            probs.append("%s: %s" % (it["ep"] or it["title"][:50], e))
    n = len(d["items"])
    verb = {"import": "Imported", "retry": "Removed and searching again for", "remove": "Moved to the trash:"}[how]
    note = "%s %d of %d%s." % (verb, okn, n, (". Not done: " + "; ".join(probs)) if probs else "")
    if how == "retry" and okn: note += " Anything it finds downloads in the night window."
    st = "ok" if okn == n else "warn"
    with db() as c:
        c.execute("INSERT INTO done (issue_id, title, library, kept, moved, trash_ids, links, at, status, note, chk) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (issue_id, row["title"], row["library"], "", "[]", "[]", row["links"], now_iso(), st, note, "queue"))
        if okn == n: c.execute("DELETE FROM issues WHERE id=?", (issue_id,))
    event("queue", "%s: %s" % (row["title"], note))
    return {"ok": okn > 0, "error": "; ".join(probs) if not okn else ""}

# ---------------- Search follow-up (v33) ----------------
# "Search for better copies" used to snooze the card straight away. Now the search is followed: history since the search ->
# grabbed / imported (with quality) / waiting to import (see Downloads) / nothing found. Only "nothing found" snoozes for
# SNOOZE_DAYS; while a search is still being followed the card is hidden for a day.
def search_followup():
    with db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM done WHERE chk='quality' AND status='waiting' AND cleared=0 AND ref LIKE '%\"kind\": \"search\"%'")]
    for r in rows:
        try:
            ref = json.loads(r["ref"]); a = arr_by_name(ref["arr"])
            if not a: continue
            since = ref["at"]
            utc = datetime.fromisoformat(since).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            hist = arr(a, "/history/since?" + urllib.parse.urlencode({"date": utc}))
            key = "movieId" if ref["mode"] == "movie" else "episodeId"
            want = set(ref["ids"]); st = {i: "none" for i in want}; qual = {}
            for h in sorted(hist, key=lambda x: x.get("date", "")):
                i = h.get(key)
                if i not in want: continue
                ev = h.get("eventType")
                if ev == "grabbed" and st[i] == "none": st[i] = "grabbed"
                elif ev in ("downloadFolderImported", "movieFileImported", "episodeFileImported"):
                    st[i] = "imported"; qual[i] = ((h.get("quality") or {}).get("quality") or {}).get("name", "")
                elif ev == "downloadFailed" and st[i] != "imported": st[i] = "failed"
            try:
                inq = {x.get(key): x for x in _arr_queue(a)}
            except Exception: inq = {}
            for i in want:
                if st[i] == "grabbed" and i in inq and (inq[i].get("trackedDownloadState") in ("importBlocked", "importPending")): st[i] = "blocked"
            cmd_done = True
            if ref.get("cmd"):
                try: cmd_done = arr(a, "/command/%d" % ref["cmd"]).get("status") in ("completed", "failed", "aborted")
                except Exception: cmd_done = True
            age_h = (datetime.now() - datetime.fromisoformat(since)).total_seconds() / 3600
            cnt = {k: sum(1 for v in st.values() if v == k) for k in ("imported", "grabbed", "blocked", "failed", "none")}
            n = len(want)
            bits = []
            if cnt["imported"]: bits.append("%d imported (%s)" % (cnt["imported"], ", ".join(sorted({q for q in qual.values() if q})) or "new copy"))
            if cnt["grabbed"]: bits.append("%d downloading" % cnt["grabbed"])
            if cnt["blocked"]: bits.append("%d waiting to import (see Downloads)" % cnt["blocked"])
            if cnt["failed"]: bits.append("%d failed to download" % cnt["failed"])
            if cnt["none"] and (cmd_done or age_h > 1): bits.append("%d nothing better found" % cnt["none"])
            if cnt["imported"] == n:
                status, note = "ok", "✓ %s: all %d better cop%s imported (%s)." % (a["name"], n, "y" if n == 1 else "ies", ", ".join(sorted(set(qual.values()))) or "new copy")
                with db() as c: c.execute("DELETE FROM snoozed WHERE id=?", (r["issue_id"],))
            elif cnt["grabbed"] or cnt["blocked"] or not (cmd_done or age_h > 1):
                status, note = "waiting", "%s: %s. Checked %s." % (a["name"], ", ".join(bits) or "searching", datetime.now().strftime("%a %H:%M"))
                with db() as c: snooze(c, r["issue_id"], r["title"], days=1, count=False)
            else:
                status = "ok" if cnt["imported"] else "warn"
                note = "%s: %s." % (a["name"], ", ".join(bits))
                if cnt["none"] or cnt["failed"]:
                    note += " Hidden for %d days, then searched again by itself." % SNOOZE_DAYS
                    with db() as c: snooze(c, r["issue_id"], r["title"])
            with db() as c:
                c.execute("UPDATE done SET status=?, note=?, checked_at=? WHERE id=?", (status, note, now_iso(), r["id"]))
        except Exception as e:
            log("search followup %s failed: %r" % (r["id"], e))

# ---------------- web ----------------
# ---------------- login ----------------
AUTH_ON = os.environ.get("LD_AUTH", "on").lower() not in ("off", "0", "false", "no")
AUTH_FILE = os.path.join(CONFIG_DIR, "auth.json")
SESSION_DAYS = 30
PBKDF2_ROUNDS = 600000
_FAILS = {}                      # key -> [times of recent wrong passwords]
_FAIL_LOCK = threading.Lock()

def auth_get():
    try:
        with open(AUTH_FILE) as f: return json.load(f)
    except (OSError, ValueError): return None

def _hash_pw(pw, salt, rounds=PBKDF2_ROUNDS):
    return hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), rounds).hex()

def auth_set(pw):
    salt = secrets.token_hex(16)
    tmp = AUTH_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f: json.dump({"algo": "pbkdf2_sha256", "rounds": PBKDF2_ROUNDS, "salt": salt, "hash": _hash_pw(pw, salt), "set": now_iso()}, f)
    os.replace(tmp, AUTH_FILE)

def auth_check(pw):
    a = auth_get()
    if not a or not isinstance(pw, str): return False
    return hmac.compare_digest(_hash_pw(pw, a["salt"], int(a.get("rounds", PBKDF2_ROUNDS))), a["hash"])

def pw_problem(pw):
    if not isinstance(pw, str) or len(pw) < 10: return "Use at least 10 characters."
    if len(pw) > 200: return "That's too long."
    return None

def _locked(key):
    now = time.time()
    with _FAIL_LOCK:
        for k in list(_FAILS):
            _FAILS[k] = [t for t in _FAILS[k] if now - t < 900]
            if not _FAILS[k]: del _FAILS[k]
        recent = [t for t in _FAILS.get(key, []) if now - t < 600]
        everyone = sum(len([t for t in v if now - t < 600]) for v in _FAILS.values())
    return len(recent) >= 10 or everyone >= 50          # one address, or a spread-out guessing run

def _fail(key):
    with _FAIL_LOCK: _FAILS.setdefault(key, []).append(time.time())

def session_new(ip):
    tok = secrets.token_urlsafe(32)
    with db() as c:
        c.execute("DELETE FROM sessions WHERE expires < ?", (now_iso(),))
        c.execute("INSERT INTO sessions VALUES (?,?,?,?,?)", (hashlib.sha256(tok.encode()).hexdigest(), now_iso(),
                  (datetime.now() + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds"), now_iso(), ip))
    return tok

def session_ok(tok):
    if not tok: return False
    h = hashlib.sha256(tok.encode()).hexdigest()
    with db() as c:
        r = c.execute("SELECT expires, last_seen FROM sessions WHERE h=?", (h,)).fetchone()
        if not r or r["expires"] < now_iso(): return False
        if (r["last_seen"] or "") < (datetime.now() - timedelta(minutes=10)).isoformat(timespec="seconds"):
            c.execute("UPDATE sessions SET last_seen=?, expires=? WHERE h=?", (now_iso(), (datetime.now() + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds"), h))
    return True

def session_end(tok, everyone=False):
    with db() as c:
        if everyone: c.execute("DELETE FROM sessions")
        elif tok: c.execute("DELETE FROM sessions WHERE h=?", (hashlib.sha256(tok.encode()).hexdigest(),))

# ---------------- settings page ----------------
SECRET_FIELDS = ("token", "key")
SINGLE = ("plex", "tautulli", "tdarr", "sportarr", "gotify") + NOTIFY_KINDS
LISTS = ("arrs", "bazarr")

def settings_public():
    """Settings for the page: secrets replaced by <field>_set: true. List items carry _i (their index) so a save can keep unchanged keys."""
    def red(e, i=None):
        e = dict(e); e.pop("container_root", None); e.pop("media_root", None)
        for f in SECRET_FIELDS:
            if f in e: e[f + "_set"] = bool(e.pop(f))
        if i is not None: e["_i"] = i
        return e
    out = {k: red(S[k]) for k in SINGLE if isinstance(S.get(k), dict)}
    for k in LISTS: out[k] = [red(e, i) for i, e in enumerate(S.get(k) or [])]
    h, mi, days = sched()
    out["schedule"] = {"hour": h, "minute": mi, "days": days}
    return {"settings": out, "configured": configured(), "media_root": MEDIA_ROOT}

def _with_secrets(kind, e):
    """Fill blank secret fields of an edited entry from what's saved (matched by _i for lists)."""
    e = {k: v for k, v in (e or {}).items() if not k.endswith("_set")}
    old = (S.get(kind) or [])[e["_i"]] if kind in LISTS and isinstance(e.get("_i"), int) and e["_i"] < len(S.get(kind) or []) else S.get(kind) if kind in SINGLE else None
    for f in SECRET_FIELDS:
        if not e.get(f) and isinstance(old, dict) and old.get(f): e[f] = old[f]
    e.pop("_i", None)
    e["paths"] = [{"from": str(m.get("from", "")).strip(), "to": str(m.get("to", "")).strip()} for m in e.get("paths") or [] if str(m.get("from", "")).strip() and str(m.get("to", "")).strip()]
    for k in ("url", "public", "click"):
        if isinstance(e.get(k), str): e[k] = e[k].strip().rstrip("/")
    return e

def settings_test(kind, e):
    e = _with_secrets(kind, e)
    if kind in NOTIFY_DEFAULT_URL and not e.get("url"): e["url"] = NOTIFY_DEFAULT_URL[kind]
    if not e.get("url"): return {"ok": False, "msg": "Fill in the address first."}
    u = e["url"]
    try:
        if kind == "plex":
            r = http(u + "/identity", {"X-Plex-Token": e.get("token", "")}, timeout=15)["MediaContainer"]
            http(u + "/library/sections", {"X-Plex-Token": e.get("token", "")}, timeout=15)      # identity works without a token; this doesn't
            return {"ok": True, "msg": "Connected to Plex %s." % r.get("version", ""), "machine": r.get("machineIdentifier")}
        if kind == "arrs":
            r = http(u + "/api/v3/system/status", {"X-Api-Key": e.get("key", "")}, timeout=15)
            app = (r.get("appName") or "").lower()
            if e.get("kind") and app and app != e["kind"]: return {"ok": False, "msg": "That's %s, not %s. Change the type or the address." % (r.get("appName"), e["kind"].title())}
            return {"ok": True, "msg": "Connected to %s %s." % (r.get("appName", "it"), r.get("version", ""))}
        if kind == "bazarr":
            r = http(u + "/api/system/status", {"X-API-KEY": e.get("key", "")}, timeout=15)
            return {"ok": True, "msg": "Connected to Bazarr %s." % ((r.get("data") or {}).get("bazarr_version", ""))}
        if kind == "tautulli":
            r = http(u + "/api/v2?" + urllib.parse.urlencode({"apikey": e.get("key", ""), "cmd": "get_tautulli_info"}), timeout=15)["response"]
            if r.get("result") != "success": return {"ok": False, "msg": r.get("message") or "Tautulli said no. Check the API key."}
            return {"ok": True, "msg": "Connected to Tautulli %s." % ((r.get("data") or {}).get("tautulli_version", ""))}
        if kind == "tdarr":
            r = http(u + "/api/v2/status", {"x-api-key": e.get("key", "")}, timeout=15)
            return {"ok": True, "msg": "Connected to Tdarr %s." % (r.get("version", "") if isinstance(r, dict) else "")}
        if kind == "sportarr":
            http(u + "/api/rootfolder", {"X-Api-Key": e.get("key", "")}, timeout=15)
            return {"ok": True, "msg": "Connected to Sportarr."}
        if kind in NOTIFY_KINDS:
            r = notify_one(kind, e, "Library Doctor", "Connected. Nightly summaries will come here.", 3)
            return {"ok": r.get("ok"), "msg": "Sent a test message." if r.get("ok") else r.get("error")}
        if kind == "gotify":
            http(u + "/message", {"X-Gotify-Key": e.get("token", "")}, data={"title": "Library Doctor", "message": "Connected. Nightly summaries will come here.", "priority": 3}, method="POST", timeout=15)
            return {"ok": True, "msg": "Sent a test message to Gotify."}
    except urllib.error.HTTPError as x:
        return {"ok": False, "msg": "It answered, but said %d %s. %s" % (x.code, x.reason, "Check the API key." if x.code in (401, 403) else "")}
    except Exception as x:
        return {"ok": False, "msg": "Couldn't reach %s: %s" % (u, getattr(x, "reason", x))}
    return {"ok": False, "msg": "Unknown app."}

_BASES = {"t": 0, "v": []}
def _local_bases():
    """MEDIA_ROOT and the folders up to two levels below it: where an app's tail path may land."""
    if time.time() - _BASES["t"] < 300: return _BASES["v"]
    out = [MEDIA_ROOT]
    try:
        for a in sorted(os.scandir(MEDIA_ROOT), key=lambda x: x.name):
            if a.is_dir() and not a.name.startswith("."):
                out.append(a.path)
                try: out += [b.path for b in sorted(os.scandir(a.path), key=lambda x: x.name) if b.is_dir() and not b.name.startswith(".")][:400]
                except OSError: pass
    except OSError: pass
    _BASES.update(t=time.time(), v=out); return out

def _samples(kind, e):
    """A few real paths exactly as the app reports them (no mapping applied)."""
    raw = dict(e, paths=[]); u = e["url"]; out = []
    if kind == "plex":
        h = {"X-Plex-Token": e["token"]}
        for sec in http(u + "/library/sections", h, timeout=20)["MediaContainer"].get("Directory", []):
            typ = {"movie": 1, "show": 4}.get(sec.get("type"))
            if not typ: continue
            q = urllib.parse.urlencode({"type": typ, "X-Plex-Container-Start": 0, "X-Plex-Container-Size": 4})
            for m in http(u + "/library/sections/%s/all?%s" % (sec["key"], q), h, timeout=30)["MediaContainer"].get("Metadata", []):
                out += [pt["file"] for md in m.get("Media", []) for pt in md.get("Part", []) if pt.get("file")][:1]
    elif kind == "arrs":
        if e.get("kind") == "sonarr":
            out = [x["path"] for x in arr(raw, "/series") if x.get("path") and (x.get("statistics") or {}).get("episodeFileCount")][:8]
        else:
            out = [x["movieFile"]["path"] for x in arr(raw, "/movie") if x.get("hasFile") and (x.get("movieFile") or {}).get("path")][:8]
    elif kind == "bazarr":
        for what in ("movies", "series"):
            try: out += [x["path"] for x in (baz(raw, "/" + what, length=5).get("data") or []) if x.get("path")]
            except Exception: pass
    elif kind == "tdarr":
        req = urllib.request.Request(u + "/api/v2/cruddb", data=json.dumps({"data": {"collection": "LibrarySettingsJSONDB", "mode": "getAll"}}).encode(), headers={"Content-Type": "application/json", "x-api-key": e.get("key", "")})
        with urllib.request.urlopen(req, timeout=30) as r: out = [l["folder"] for l in json.loads(r.read().decode()) if l.get("folder")]
    elif kind == "sportarr":
        h = {"X-Api-Key": e["key"]}
        out = [x["path"] for x in http(u + "/api/rootfolder", h, timeout=20) if x.get("path")]
    return out

def settings_detect(kind, e):
    """Work out the folder mapping: for each sample, find which folder under MEDIA_ROOT holds the same tail."""
    e = _with_secrets(kind, e)
    try: smp = _samples(kind, e)
    except Exception as x: return {"ok": False, "msg": "Couldn't read paths from it: %s. Test the connection first." % getattr(x, "reason", x)}
    if not smp: return {"ok": False, "msg": "It didn't report any files or folders to compare yet."}
    votes, miss = {}, []
    bases = _local_bases()
    for p in smp:
        if os.path.exists(p) and (p == MEDIA_ROOT or p.startswith(MEDIA_ROOT + "/")): votes[("", "")] = votes.get(("", ""), 0) + 1; continue
        parts = p.strip("/").split("/"); hit = None
        for k in range(1, len(parts)):
            tail = "/".join(parts[k:])
            if os.path.isfile(p) is False and len(parts) - k < 2 and "." in parts[-1]: break    # a bare file name is too weak a match
            for b in bases:
                if os.path.exists(os.path.join(b, tail)): hit = ("/" + "/".join(parts[:k]), b); break
            if hit: break
        if hit: votes[hit] = votes.get(hit, 0) + 1
        else: miss.append(p)
    if not votes:
        return {"ok": False, "msg": "None of its %d paths could be found under %s. Check that the media is mounted in Library Doctor's container." % (len(smp), MEDIA_ROOT), "examples": smp[:3]}
    pairs = sorted(votes.items(), key=lambda x: -x[1])
    maps = [{"from": a, "to": b} for (a, b), n in pairs if a]
    found = sum(votes.values())
    if not maps:
        return {"ok": True, "paths": [], "msg": "No mapping needed: it sees the files at the same place as Library Doctor (%d of %d checked)." % (found, len(smp))}
    return {"ok": True, "paths": maps, "msg": "Found %d of %d: %s." % (found, len(smp), "; ".join("%s → %s" % (m["from"], m["to"]) for m in maps)), "missed": miss[:3]}

def settings_save(new):
    """Validate and write config.json (owner-only), then apply it without a restart."""
    cfg = {}
    sc = (new or {}).get("schedule")
    if isinstance(sc, dict):
        try:
            days = sorted({int(d) for d in sc.get("days") or [] if 0 <= int(d) <= 6})
            hr = int(sc.get("hour", SCAN_HOUR)); hr = hr if 0 <= hr <= 23 else -1     # -1 = no automatic scan
            if not days and hr >= 0: return {"ok": False, "msg": "Pick at least one day for the automatic scan."}
            cfg["schedule"] = {"hour": hr, "minute": min(59, max(0, int(sc.get("minute", 0)))), "days": days or list(range(7))}
        except (TypeError, ValueError):
            return {"ok": False, "msg": "The scan time isn't valid."}
    elif S.get("schedule"):
        cfg["schedule"] = S["schedule"]                      # a save without the schedule keeps the old one
    for k in SINGLE:
        e = (new or {}).get(k)
        if isinstance(e, dict) and k in NOTIFY_DEFAULT_URL and not e.get("url"): e["url"] = NOTIFY_DEFAULT_URL[k]
        if isinstance(e, dict) and e.get("url"): cfg[k] = _with_secrets(k, e)
    for k in LISTS:
        cfg[k] = [_with_secrets(k, e) for e in (new or {}).get(k) or [] if isinstance(e, dict) and e.get("url")]
    # v0.1.4: keep settings this page doesn't edit (e.g. "qbit" from an older secrets.json) instead of dropping them
    for k, v in S.items():
        if k not in cfg and k not in SINGLE and k not in LISTS and k != "schedule": cfg[k] = v
    names = [a.get("name", "") for a in cfg["arrs"]]
    if any(not n for n in names) or len(set(names)) != len(names): return {"ok": False, "msg": "Each Radarr/Sonarr needs its own name."}
    for a in cfg["arrs"]:
        if a.get("kind") not in ("radarr", "sonarr"): return {"ok": False, "msg": "Pick Radarr or Sonarr for %s." % a["name"]}
    if cfg.get("plex"):
        t = settings_test("plex", dict(cfg["plex"]))
        if t.get("machine"): cfg["plex"]["machine"] = t["machine"]
        cfg["plex"].setdefault("public", "https://app.plex.tv")
    tmp = CONFIG_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f: json.dump(cfg, f, indent=1)
    os.replace(tmp, CONFIG_FILE)
    S.clear(); S.update(normalize(cfg))
    _TDARR_LIBS["t"] = 0; _STATS["v"] = None
    event("settings", "Settings saved.")
    return {"ok": True, "configured": configured()}

_SIZE_RE = re.compile(r"saved ([\d.]+) (B|KB|MB|GB|TB)\b")
_UNITS = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}
_STATS = {"at": 0, "v": None}
TDARR_SAVE_GUESS = 0.30          # Tdarr's HEVC conversions here have saved 28-33%

def history(days=30):
    """v0.1.3 History panel: scans in the window (issues found, how long they took) and space saved per day.
    Space saved is worked out from what's already recorded (files purged from the trash for good + the "saved X" of
    smaller copies / Tdarr / remuxes, same rules as the Space saved ring), so it has history from day one."""
    days = 90 if int(days or 30) > 30 else 30
    start = datetime.now() - timedelta(days=days)
    s0 = start.isoformat(timespec="seconds")
    with db() as c:
        scans = [dict(r) for r in c.execute("SELECT started, finished, issues, items FROM scans WHERE ok=1 AND started>=? ORDER BY id", (s0,))]
        trash = [dict(r) for r in c.execute("SELECT size, reason, purged_at FROM trash WHERE purged_at IS NOT NULL AND restored_at IS NULL")]
        done = [dict(r) for r in c.execute("SELECT note, at, chk FROM done WHERE chk='quality'")]
    out_scans = []
    for r in scans:
        try: secs = max(0, (datetime.fromisoformat(r["finished"]) - datetime.fromisoformat(r["started"])).total_seconds())
        except Exception: secs = 0
        out_scans.append({"t": r["finished"] or r["started"], "issues": r["issues"] or 0, "items": r["items"] or 0, "secs": round(secs)})
    ev = []
    for t in trash:
        rs = t["reason"] or ""
        if "(replaced)" in rs or rs.startswith("Plex fix") or rs.startswith("Virus"): continue
        ev.append((t["purged_at"][:10], t["size"] or 0))
    for d in done:
        m = _SIZE_RE.search(d["note"] or "")
        if m and d["at"]: ev.append((d["at"][:10], float(m.group(1)) * _UNITS[m.group(2)]))
    base = sum(b for dt, b in ev if dt < s0[:10])
    per = {}
    for dt, b in ev:
        if dt >= s0[:10]: per[dt] = per.get(dt, 0) + b
    space, run = [], base
    for i in range(days + 1):
        dt = (start + timedelta(days=i)).date().isoformat()
        run += per.get(dt, 0)
        space.append({"d": dt, "total": round(run), "day": round(per.get(dt, 0))})
    return {"days": days, "scans": out_scans, "space": space}

def stats():
    """Space saved and jobs left for the two extra rings on the main screen. Cached 30 s: state() is polled often."""
    if _STATS["v"] is not None and time.time() - _STATS["at"] < 30: return _STATS["v"]
    freed = pending = 0
    with db() as c:
        trash = [dict(r) for r in c.execute("SELECT src, size, reason, moved_at, restored_at, purged_at FROM trash")]
        done = [dict(r) for r in c.execute("SELECT chk, status, note, at, cleared FROM done")]
        ign = hidden_ids(c)
        issues = [dict(r) for r in c.execute("SELECT id, chk, detail FROM issues")]
    # 1) files Library Doctor moved out for good: duplicates, leftovers, old .ts copies (minus the new .mkv). Not replaced broken files or Plex fixes.
    for t in trash:
        r = t["reason"] or ""
        if t["restored_at"] or "(replaced)" in r or r.startswith("Plex fix") or r.startswith("Virus"): continue
        size = t["size"] or 0
        if r.startswith("Old .ts copy"):
            mkv = os.path.splitext(t["src"])[0] + ".mkv"
            try: size = max(0, size - os.path.getsize(mkv))
            except OSError: size = 0
        if t["purged_at"]: freed += size
        else: pending += size                      # still in Library Doctor's trash: freed when it empties
    # 2) smaller copies and Tdarr conversions ("saved X" in the Done note)
    week = (datetime.now() - timedelta(days=7)).isoformat(timespec="seconds")
    for d in done:
        m = _SIZE_RE.search(d["note"] or "")
        if not m or d["chk"] != "quality": continue
        b = float(m.group(1)) * _UNITS[m.group(2)]
        if "recycle bin" in (d["note"] or "") and (d["at"] or "") > week: pending += b    # old copy waits in Radarr's recycle bin for 7 days
        else: freed += b
    # 3) what the open cards could still save (an estimate)
    possible = 0
    jobs_open = 0
    for i in issues:
        if i["id"] in ign: continue
        d = json.loads(i["detail"])
        if i["chk"] == "dupes":
            possible += sum(v.get("size") or 0 for v in d.get("versions") or [] if v.get("action") == "remove")
        elif i["chk"] == "quality":
            if d.get("kind") == "hog": possible += d.get("extra") or 0
            elif d.get("kind") == "tdarrmissed": possible += TDARR_SAVE_GUESS * sum(x.get("size") or 0 for x in d.get("items") or [])
        if i["chk"] == "unwatched": continue          # information, not a job
        jobs_open += len(d["eps"]) if d.get("kind") in ("titles", "odd") and isinstance(d.get("eps"), list) and d["eps"] else (d.get("count") or 1) if d.get("kind") in ("tdarrmissed", "tdarrfail") else 1
    month = (datetime.now() - timedelta(days=30)).isoformat(timespec="seconds")
    jobs_done = sum(1 for d in done if d["status"] == "ok" and (d["at"] or "") > month)
    jobs_busy = sum(1 for d in done if d["status"] in ("waiting", "checking"))
    jobs_warn = sum(1 for d in done if d["status"] == "warn" and not d["cleared"])
    v = {"saved": freed + pending, "freed": freed, "pending": pending, "possible": possible,
         "jobs": {"open": jobs_open, "busy": jobs_busy, "done": jobs_done, "warn": jobs_warn}}
    _STATS.update(at=time.time(), v=v)
    return v

def state():
    with db() as c:
        ign = hidden_ids(c)
        snz = {r["id"]: r["until"] for r in c.execute("SELECT id, until FROM snoozed WHERE until > ? AND id NOT IN (SELECT id FROM ignored)", (now_iso(),))}
        rows = [dict(r) for r in c.execute("SELECT * FROM issues ORDER BY chk, library, title")]
        scans = [dict(r) for r in c.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 10")]
        trash = [dict(r) for r in c.execute("SELECT id, title, src, size, reason, moved_at FROM trash WHERE restored_at IS NULL AND purged_at IS NULL ORDER BY id DESC")]
        events = [dict(r) for r in c.execute("SELECT * FROM events ORDER BY id DESC LIMIT 15")]
        done = [dict(r) for r in c.execute("SELECT * FROM done WHERE cleared=0 ORDER BY id DESC LIMIT 100")]
        open_trash = {r["id"] for r in c.execute("SELECT id FROM trash WHERE restored_at IS NULL AND purged_at IS NULL")}
    for x in done:
        x["moved"] = json.loads(x["moved"] or "[]"); x["links"] = json.loads(x["links"] or "[]")
        x["trash_ids"] = [t for t in json.loads(x["trash_ids"] or "[]") if t in open_trash]
        try: rf = json.loads(x.get("ref") or "null")
        except Exception: rf = None
        x["tdarr"] = [{"name": os.path.basename(os.path.dirname(it["path"])), "state": it.get("state", "queued"), "pct": it.get("pct"),
                       "size": it["size"], "after": it.get("after")} for it in rf["items"]] if isinstance(rf, dict) and rf.get("kind") == "tdarr" else None
    for r in rows:
        r["detail"] = json.loads(r["detail"]); r["links"] = json.loads(r["links"]); r["ignored"] = r["id"] in ign
        if r["id"] in snz: r["snoozed"] = snz[r["id"]]
    for t in trash:
        t["empties"] = (datetime.fromisoformat(t["moved_at"]) + timedelta(days=TRASH_DAYS)).isoformat(timespec="seconds")
    bins = [{"name": "Library Doctor", "days": TRASH_DAYS, "count": len(trash), "size": sum(t["size"] or 0 for t in trash),
             "next": min((t["empties"] for t in trash), default=None)}]
    try:
        rbi = recycle_items()
        trash += [{k: v for k, v in x.items() if k in ("id", "title", "src", "size", "reason", "moved_at", "empties", "rb")} for x in rbi]
        for b in recycle_bins():                       # v34: one summary line per bin, empty ones too
            mine = [x for x in rbi if x["rb"] == b["arr"]["name"]]
            bins.append({"name": b["arr"]["name"], "days": b["days"], "count": len(mine), "size": sum(x["size"] for x in mine),
                         "next": min((x["empties"] for x in mine), default=None)})
    except Exception as e:
        log("recycle list failed: %r" % e)
    virus = {"clam": clam_status(cached=True), "last": kv_get("virus_last_incremental"), "full": kv_get("virus_full_last"),
             "full_running": VIRUS_LOCK.locked(), "full_started": (kv_get("virus_full_running") or {}).get("at")}
    coming = {"ytdlp": ytdlp_status(), "busy": TRAILER_LOCK.locked(), "updating": YTDLP_LOCK.locked()}
    return {"gotify": bool(notify_channels()), "notify": [k for k, _ in notify_channels()], "follow_days": FOLLOW_DAYS, "virus": virus, "coming": coming, "running": STATE["running"], "step": STATE["step"], "progress": STATE["progress"], "issues": rows, "scans": scans,
            "scan_hour": sched()[0], "scan_minute": sched()[1], "scan_days": sched()[2], "keep_days": KEEP_DAYS, "stats": stats(), "trash": trash, "bins": bins, "trash_days": TRASH_DAYS, "events": events, "done": done,
            "configured": configured(), "version": APP_VERSION, "split_mounts": split_mounts(), "media_root": MEDIA_ROOT,
            "plex_settings": "%s/desktop/#!/settings/server/%s/manage/optimized" % ((S.get("plex") or {}).get("public", "https://app.plex.tv").rstrip("/"), (S.get("plex") or {}).get("machine", ""))}

class H(BaseHTTPRequestHandler):
    server_version = "LibraryDoctor/2"
    def log_message(self, fmt, *a): pass
    def _ip(self):
        return "%s|%s" % (self.client_address[0], (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip())
    def _tok(self):
        try: return SimpleCookie(self.headers.get("Cookie") or "")["ld_session"].value
        except Exception: return None
    def _cookie(self, tok, clear=False):
        secure = "; Secure" if (self.headers.get("X-Forwarded-Proto") or "").lower() == "https" else ""
        return "ld_session=%s; Path=/; HttpOnly; SameSite=Strict%s; Max-Age=%d" % ("" if clear else tok, secure, 0 if clear else SESSION_DAYS * 86400)
    def authed(self):
        return not AUTH_ON or session_ok(self._tok())
    def send(self, code, body, ctype="application/json", cookie=None):
        b = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        if cookie: self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; script-src 'self' 'unsafe-inline'; img-src 'self' data: https://i.ytimg.com")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/healthz": return self.send(200, {"ok": True, "version": APP_VERSION})
        if path == "/login" or (path in ("/", "/index.html") and not self.authed()):
            if path == "/login" and self.authed(): return self._redirect("/")
            with open(os.path.join(APP_DIR, "login.html"), "rb") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8")
        if path == "/api/auth": return self.send(200, {"on": AUTH_ON, "has_password": bool(auth_get()), "in": self.authed()})
        if not self.authed(): return self.send(401, {"error": "Please log in.", "login": True})
        if path in ("/", "/index.html"):
            with open(os.path.join(APP_DIR, "index.html"), "rb") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8")
        if path == "/api/state": return self.send(200, state())
        if path.startswith("/api/history"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self.send(200, history(int((q.get("days") or ["30"])[0]) if (q.get("days") or ["30"])[0].isdigit() else 30))
        if path == "/api/settings": return self.send(200, settings_public())
        self.send(404, {"error": "not found"})
    def _redirect(self, to):
        self.send_response(302); self.send_header("Location", to); self.send_header("Content-Length", "0"); self.end_headers()
    def do_POST(self):
        if self.headers.get("X-LD") != "1":          # only our own page may call these
            return self.send(403, {"error": "forbidden"})
        path = urllib.parse.urlparse(self.path).path
        n = int(self.headers.get("Content-Length") or 0)
        if n > 2_000_000: return self.send(413, {"error": "too big"})
        body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        if path == "/api/auth/setup":                  # first run only: create the password
            if not AUTH_ON or auth_get(): return self.send(409, {"ok": False, "msg": "A password is already set."})
            bad = pw_problem(body.get("password"))
            if bad: return self.send(200, {"ok": False, "msg": bad})
            auth_set(body["password"]); event("login", "Password created.")
            return self.send(200, {"ok": True}, cookie=self._cookie(session_new(self.client_address[0])))
        if path == "/api/login":
            if not AUTH_ON: return self.send(200, {"ok": True})
            key = self._ip()
            if _locked(key): return self.send(429, {"ok": False, "msg": "Too many wrong passwords. Try again in 15 minutes."})
            if auth_check(body.get("password")):
                return self.send(200, {"ok": True}, cookie=self._cookie(session_new(self.client_address[0])))
            _fail(key); time.sleep(1)
            return self.send(200, {"ok": False, "msg": "That password isn't right."})
        if path == "/api/logout":
            session_end(self._tok()); return self.send(200, {"ok": True}, cookie=self._cookie("", clear=True))
        if not self.authed(): return self.send(401, {"error": "Please log in.", "login": True})
        if path == "/api/auth/change":
            if not AUTH_ON: return self.send(200, {"ok": False, "msg": "The login is switched off (LD_AUTH=off)."})
            if not auth_check(body.get("old")): _fail(self._ip()); time.sleep(1); return self.send(200, {"ok": False, "msg": "Your current password isn't right."})
            bad = pw_problem(body.get("new"))
            if bad: return self.send(200, {"ok": False, "msg": bad})
            auth_set(body["new"]); session_end(None, everyone=True); event("login", "Password changed; everyone else logged out.")
            return self.send(200, {"ok": True}, cookie=self._cookie(session_new(self.client_address[0])))
        try:
            if path == "/api/settings/test": return self.send(200, settings_test(str(body.get("kind")), body.get("cfg") or {}))
            if path == "/api/settings/detect": return self.send(200, settings_detect(str(body.get("kind")), body.get("cfg") or {}))
            if path == "/api/settings/save": return self.send(200, settings_save(body.get("settings") or {}))
            if path == "/api/scan":
                if not configured(): return self.send(409, {"error": "Set up Plex and at least one Radarr or Sonarr in Settings first."})
                if STATE["running"]: return self.send(409, {"error": "A scan is already running."})
                threading.Thread(target=scan, daemon=True).start()
                return self.send(202, {"started": True})
            if path in ("/api/ignore", "/api/unignore"):
                i = str(body.get("id", ""))[:32]
                with db() as c:
                    days = body.get("days")
                    until = (datetime.now() + timedelta(days=int(days))).isoformat(timespec="seconds") if isinstance(days, int) and 0 < days < 10000 else None
                    if path == "/api/ignore":
                        c.execute("INSERT OR REPLACE INTO ignored (id, title, at, until) VALUES (?,?,?,?)", (i, str(body.get("title", ""))[:200], now_iso(), until))
                    else:
                        c.execute("DELETE FROM ignored WHERE id=?", (i,)); c.execute("DELETE FROM snoozed WHERE id=?", (i,))
                if body.get("uw"):                       # an Unwatched item: move it on the card now
                    uw_move(i, until=until, show=(path == "/api/unignore"))
                return self.send(200, {"ok": True})
            if path == "/api/cleanup":
                ids = body.get("ids") or [body.get("id")]
                keep = body.get("keep") or {}          # {issue_id: index of the copy to keep}
                res = [dict(cleanup(str(i)[:32], keep.get(i) if isinstance(keep.get(i), int) else None), id=i) for i in ids if i][:200]
                return self.send(200, {"results": res})
            if path == "/api/metafix":
                ids = body.get("ids") or [body.get("id")]
                how = str(body.get("how", ""))
                res = [dict(meta_fix(str(i)[:32], how), id=i) for i in ids if i][:200]
                return self.send(200, {"ok": any(r.get("ok") for r in res), "started": sum(1 for r in res if r.get("ok")),
                                       "errors": [r["error"] for r in res if not r.get("ok")]})
            if path == "/api/replace":
                ids = body.get("ids") or [body.get("id")]
                res = [dict(health_fix(str(i)[:32]), id=i) for i in ids if i][:200]
                return self.send(200, {"ok": any(r.get("ok") for r in res), "started": sum(1 for r in res if r.get("ok")),
                                       "errors": [r["error"] for r in res if not r.get("ok")]})
            if path == "/api/subfix":
                ids = body.get("ids") or [body.get("id")]
                how = str(body.get("how", ""))
                res = [dict(subs_fix(str(i)[:32], how), id=i) for i in ids if i][:300]
                return self.send(200, {"ok": any(r.get("ok") for r in res), "started": sum(1 for r in res if r.get("ok")),
                                       "errors": [r["error"] for r in res if not r.get("ok")]})
            if path == "/api/virusfix":
                ids = body.get("ids") or [body.get("id")]
                res = [dict(virus_fix(str(i)[:32]), id=i) for i in ids if i][:300]
                return self.send(200, {"ok": any(r.get("ok") for r in res), "started": sum(1 for r in res if r.get("ok")),
                                       "errors": [r["error"] for r in res if not r.get("ok")]})
            if path == "/api/smaller/search":
                return self.send(200, smaller_search(str(body.get("id", ""))[:32]))
            if path == "/api/smaller/grab":
                return self.send(200, smaller_grab(str(body.get("id", ""))[:32], str(body.get("key", ""))[:16]))
            if path == "/api/qualityfix":
                ids = body.get("ids") or [body.get("id")]
                how = str(body.get("how", ""))
                res = [dict(quality_fix(str(i)[:32], how), id=i) for i in ids if i][:100]
                return self.send(200, {"ok": any(r.get("ok") for r in res), "started": sum(1 for r in res if r.get("ok")),
                                       "errors": [r["error"] for r in res if not r.get("ok")]})
            if path == "/api/queuefix":
                ids = body.get("ids") or [body.get("id")]
                how = str(body.get("how", ""))
                res = [dict(queue_fix(str(i)[:32], how), id=i) for i in ids if i][:50]
                return self.send(200, {"ok": any(r.get("ok") for r in res), "errors": [r["error"] for r in res if not r.get("ok") and r.get("error")]})
            if path == "/api/queuerefresh":
                return self.send(200, {"ok": True, "cards": queue_refresh()})
            if path == "/api/comingfix":
                ids = body.get("ids") or [body.get("id")]
                how = str(body.get("how", ""))
                res = [dict(coming_fix(str(i)[:32], how), id=i) for i in ids if i][:100]
                return self.send(200, {"ok": any(r.get("ok") for r in res), "started": sum(1 for r in res if r.get("ok")),
                                       "errors": [r["error"] for r in res if not r.get("ok")]})
            if path == "/api/trailer/search":
                return self.send(200, trailer_search(str(body.get("id", ""))[:32], body.get("q")))
            if path == "/api/trailer/use":
                return self.send(200, trailer_use(str(body.get("id", ""))[:32], body.get("vid")))
            if path == "/api/ytdlp/update":
                return self.send(200, ytdlp_update())
            if path == "/api/gotifytest":
                return self.send(200, notify("Library Doctor: test", "This is a test from Library Doctor. After each nightly scan you get one message like this, but only when there's something new.", 4))
            if path == "/api/followup":
                threading.Thread(target=followup, daemon=True).start()
                return self.send(200, {"ok": True})
            if path == "/api/virusfull":
                return self.send(200, virus_full())
            if path == "/api/fixdouble":
                return self.send(200, fix_double(str(body.get("id", ""))[:32]))
            if path == "/api/done/clear":
                # v31: jobs still running (waiting/checking) stay on the list, otherwise their follow-up stops (cleared=0 filters)
                with db() as c: c.execute("UPDATE done SET cleared=1 WHERE cleared=0 AND COALESCE(chk,'dupes')=? AND COALESCE(status,'') NOT IN ('waiting','checking')", (str(body.get("chk") or "dupes")[:16],))
                return self.send(200, {"ok": True})
            if path == "/api/restore":
                ids = [str(i) for i in (body.get("ids") or [body.get("id", 0)])][:500]
                res = [dict(restore(int(i)), id=i) for i in ids if i.isdigit()]
                rb = [i for i in ids if re.fullmatch(r"rb[0-9a-f]{16}", i)]
                if rb:
                    items = recycle_items()
                    res += [dict(restore_recycle(i, items), id=i) for i in rb]
                ok = [r for r in res if r.get("ok")]
                return self.send(200, {"ok": bool(ok), "restored": len(ok), "errors": [r["error"] for r in res if not r.get("ok")],
                                       "notes": [r["note"] for r in ok if r.get("note")]})
            if path == "/api/purge":
                ids = [str(i) for i in (body.get("ids") or [])][:500]
                if ids:
                    num = [i for i in ids if i.isdigit()]
                    r = purge_ids(num) if num else {"ok": True, "deleted": 0, "freed": human(0)}
                    rb = {i for i in ids if re.fullmatch(r"rb[0-9a-f]{16}", i)}
                    rn, rf = purge_recycle(rb) if rb else (0, 0)
                else:
                    r = purge(None); rn, rf = purge_recycle(None)
                if rn:
                    r["freed"] = human(rf) if not r.get("deleted") else "%s + %s" % (r["freed"], human(rf))
                    r["deleted"] = r.get("deleted", 0) + rn
                return self.send(200, r)
        except Exception as e:
            log("POST %s failed: %r" % (path, e))
            return self.send(500, {"ok": False, "error": str(e)})
        self.send(404, {"error": "not found"})

if __name__ == "__main__":
    try:
        init_db()
    except (OSError, sqlite3.OperationalError) as e:
        log("Can't write to %s (%s). Library Doctor keeps its settings and database there. Mount a folder at %s that user %d can write to "
            "(set PUID/PGID in compose to the folder's owner), then start it again." % (CONFIG_DIR, e, CONFIG_DIR, os.getuid()))
        raise SystemExit(1)
    threading.Thread(target=nightly, daemon=True).start()
    log("Library Doctor listening on %s:%d (nightly scan at %02d:00, trash kept %d days)" % (LISTEN, PORT, SCAN_HOUR, TRASH_DAYS))
    ThreadingHTTPServer((LISTEN, PORT), H).serve_forever()
