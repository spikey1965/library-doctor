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

APP_VERSION = "0.2.1"
CONFIG_DIR = os.environ.get("LD_CONFIG", "/config")
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(CONFIG_DIR, "ldoctor.db")
LISTEN = os.environ.get("LD_LISTEN", "0.0.0.0")
PORT = int(os.environ.get("LD_PORT", "8790"))
SCAN_HOUR = int(os.environ.get("LD_SCAN_HOUR", "3"))   # default; the Settings page can set time + days (v0.1.3)
MEDIA_ROOT = os.path.realpath(os.environ.get("LD_MEDIA_ROOT", "/media"))
TRASH_DIR = os.path.join(MEDIA_ROOT, ".library-doctor-trash")
TRASH_DAYS = int(os.environ.get("LD_TRASH_DAYS", "7"))
TRASH_DAYS_ENV = TRASH_DAYS
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
    for k, name in (("tautulli", "Tautulli"), ("tdarr", "Tdarr"), ("sportarr", "Sportarr"), ("prowlarr", "Prowlarr"), ("gotify", "Gotify")):
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

def acl_protected(path):
    """v0.2.1: is this file (or a folder above it, up to the media root) governed by a NAS permission list (ACL)?
    Synology marks folders with system.syno_acl_self, TrueNAS with system.nfs4_acl, plain Linux with system.posix_acl_*.
    Changing a mode bit (chmod) on such a file replaces its ACL, so Library Doctor leaves the permissions alone there."""
    p = path
    for _ in range(40):
        try:
            if any("acl" in x.lower() for x in os.listxattr(p)): return True
        except OSError:
            pass
        if p in ("/", "") or os.path.normpath(p) == os.path.dirname(MEDIA_ROOT.rstrip("/")): break
        p = os.path.dirname(p)
    return False

def config_write_hint(err):
    """A plain-English fix when /config can't be written (v0.2.1: Synology and other NAS permission lists)."""
    uid = os.getuid()
    try: st = os.stat(CONFIG_DIR); owner = st.st_uid
    except OSError: owner = None
    if acl_protected(CONFIG_DIR) or acl_protected(MEDIA_ROOT):
        return ("Can't write to %s (%s). The folder has a NAS permission list (ACL) that doesn't let user %d write, even if the folder belongs to it. "
                "On Synology: Control Panel > Shared Folder > docker (the share that holds this folder) > Edit > Permissions, tick Read/Write for your own user, "
                "and do the same for your media share. On TrueNAS: give user %d Modify in the dataset's ACL. Then start Library Doctor again." % (CONFIG_DIR, err, uid, uid))
    if owner == uid:
        return ("Can't write to %s (%s). The folder already belongs to user %d, so something else blocks writing, usually a NAS permission list (ACL): "
                "give user %d write access to the folder (on Synology: Control Panel > Shared Folder > Edit > Permissions > Read/Write for your user), then start it again." % (CONFIG_DIR, err, uid, uid))
    return ("Can't write to %s (%s). Library Doctor keeps its settings and database there. Mount a folder at %s that user %d can write to "
            "(set PUID/PGID in compose to the folder's owner), then start it again." % (CONFIG_DIR, err, CONFIG_DIR, uid))

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

# v0.1.9: never write a key to the log (SABnzbd/Tautulli/Plex take theirs in the URL, so an error message could carry one)
_SECRET_RE = re.compile(r"""((?:apikey|api_key|apiKey|X-Plex-Token|token|password|pass|key)=)[^&\s'"]+|((?:X-Api-Key|Authorization|X-Plex-Token)['"]?\s*[:=]\s*['"]?(?:Bearer |Basic )?)[^'"\s,}]+""", re.I)
_URL_SECRET_RE = re.compile(r"(discord(?:app)?\.com/api/webhooks/)[^\s'\"]+|(api\.telegram\.org/bot)[^/\s'\"]+", re.I)
def redact(s):
    s = _SECRET_RE.sub(lambda m: (m.group(1) or m.group(2)) + "***", str(s))
    return _URL_SECRET_RE.sub(lambda m: (m.group(1) or m.group(2)) + "***", s)

def log(msg):
    print(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), redact(msg), flush=True)

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
        CREATE TABLE IF NOT EXISTS ms_ids (rk INTEGER PRIMARY KEY AUTOINCREMENT, sid TEXT UNIQUE);
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
        c.execute("CREATE TABLE IF NOT EXISTS scores (at TEXT PRIMARY KEY, overall REAL, areas TEXT)")                       # v0.1.9 score history
        c.execute("CREATE TABLE IF NOT EXISTS appearances (id INTEGER PRIMARY KEY, issue_id TEXT, chk TEXT, kind TEXT, title TEXT, library TEXT, at TEXT)")
        c.execute("CREATE INDEX IF NOT EXISTS appearances_issue ON appearances (issue_id)")
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

# ---------------- media server (v0.2: one door for every request) ----------------
# Everything Library Doctor asks the media server goes through ms_request(). The rest of the code speaks Plex's API
# (paths like /library/sections, replies with MediaContainer/Metadata/Media/Part); for Plex this is a straight pass-through.
def ms_request(method, path, params=None, headers=None, timeout=60, raw=False, server=None):
    """raw=False: JSON reply as a dict (like http()). raw=True: (status, body bytes), no Accept header (refreshes, images).
    server: settings being tested (Settings page) instead of the saved ones."""
    p = server or S["plex"]
    if ms_kind(p) in ("jellyfin", "emby"):
        return jf_request(p, method, path, params, headers, timeout, raw)
    q = urllib.parse.urlencode(params or {})
    url = p["url"].rstrip("/") + path + ("?" + q if q else "")
    h = dict({"X-Plex-Token": p.get("token", "")}, **(headers or {}))
    if not raw:
        return http(url, h, method=method, timeout=timeout)
    with urllib.request.urlopen(urllib.request.Request(url, headers=h, method=method), timeout=timeout) as r:
        return r.status, r.read()

# ---------------- Jellyfin / Emby (v0.2) ----------------
# The rest of Library Doctor speaks Plex's API. For Jellyfin and Emby, ms_request() answers the same handful of Plex
# requests from their API and returns Plex-shaped replies (MediaContainer / Directory / Metadata / Media / Part).
# Their item ids are long hex strings; Library Doctor uses whole numbers as ids (ratingKey), so each one gets a stable
# number from the ms_ids table. Things only Plex has (split, poster picking, title locks, Plex's own housekeeping)
# answer with JFUnsupported, and the page hides those buttons.
class JFUnsupported(Exception):
    pass

MS_NAMES = {"plex": "Plex", "jellyfin": "Jellyfin", "emby": "Emby"}
def ms_kind(p=None):
    return ((p if p is not None else S.get("plex")) or {}).get("kind") or "plex"
def ms_name(p=None):
    return MS_NAMES.get(ms_kind(p), "Plex")

# Messages are written for Plex; with Jellyfin/Emby the page gets them with the right name (only message fields, never
# titles, paths or links). Plex's "Fix Match" is "Identify" in Jellyfin and Emby.
_MS_TEXT_KEYS = {"text", "note", "msg", "error", "errors", "warn", "notes", "reason", "problems"}
def ms_words(v):
    if ms_kind() == "plex": return v
    if isinstance(v, str):
        return v.replace("Fix Match", "Identify").replace("X-Plex", "X-PLEXHDR").replace("Plex", ms_name()).replace("X-PLEXHDR", "X-Plex")
    if isinstance(v, list): return [ms_words(x) for x in v]
    return v

def ms_words_in(obj):
    """Apply ms_words to the message fields of a reply, at any depth."""
    if ms_kind() == "plex": return obj
    if isinstance(obj, dict):
        return {k: (ms_words(v) if k in _MS_TEXT_KEYS and isinstance(v, (str, list)) and all(isinstance(x, str) for x in (v if isinstance(v, list) else [v]))
                    else ms_words_in(v)) for k, v in obj.items()}
    if isinstance(obj, list): return [ms_words_in(x) for x in obj]
    return obj

_MSID, _MSRK = {}, {}
def ms_rk(sid):
    """Stable whole-number id for a Jellyfin/Emby item id."""
    if not sid: return None
    if sid in _MSID: return _MSID[sid]
    with db() as c:
        r = c.execute("SELECT rk FROM ms_ids WHERE sid=?", (sid,)).fetchone()
        rk = r["rk"] if r else c.execute("INSERT INTO ms_ids (sid) VALUES (?)", (sid,)).lastrowid
    _MSID[sid] = rk; _MSRK[rk] = sid
    return rk

def ms_sid(rk):
    rk = int(rk)
    if rk in _MSRK: return _MSRK[rk]
    with db() as c:
        r = c.execute("SELECT sid FROM ms_ids WHERE rk=?", (rk,)).fetchone()
    if not r: raise urllib.error.HTTPError("", 404, "Not Found", {}, None)
    _MSRK[rk] = r["sid"]; _MSID[r["sid"]] = rk
    return r["sid"]

_JF_FIELDS = "Path,MediaSources,ProviderIds,ProductionYear,ParentId,DateCreated"
_JF_TYPES = {"1": "Movie", "2": "Series", "3": "Season", "4": "Episode"}

def _jf_auth(p):
    tok = p.get("token", "")
    return {"Authorization": 'MediaBrowser Token="%s"' % tok} if ms_kind(p) == "jellyfin" else {"X-Emby-Token": tok}

def _jf_get(p, path, params=None, timeout=60):
    q = urllib.parse.urlencode(params or {})
    return http(p["url"].rstrip("/") + path + ("?" + q if q else ""), _jf_auth(p), timeout=timeout)

def _jf_send(p, method, path, params=None, body=None, timeout=60):
    q = urllib.parse.urlencode(params or {})
    h = dict(_jf_auth(p), **({"Content-Type": "application/json"} if body is not None else {}))
    req = urllib.request.Request(p["url"].rstrip("/") + path + ("?" + q if q else ""), headers=h, method=method,
                                 data=json.dumps(body).encode() if body is not None else b"")
    with urllib.request.urlopen(req, timeout=timeout) as r: return r.status, r.read()

def _jf_res(h):
    h = int(h or 0)
    return "4k" if h >= 1600 else "1080" if h >= 900 else "720" if h >= 650 else "576" if h >= 540 else "480" if h >= 400 else "sd" if h else ""

def _jf_media(src):
    vs = next((s for s in src.get("MediaStreams") or [] if s.get("Type") == "Video"), {})
    au = next((s for s in src.get("MediaStreams") or [] if s.get("Type") == "Audio"), {})
    dur = int((src.get("RunTimeTicks") or 0) / 10000)
    return {"id": src.get("Id"), "duration": dur, "bitrate": int((src.get("Bitrate") or 0) / 1000),
            "width": vs.get("Width") or 0, "height": vs.get("Height") or 0, "videoResolution": _jf_res(vs.get("Height")),
            "videoCodec": (vs.get("Codec") or "").lower(), "audioCodec": (au.get("Codec") or "").lower(), "container": src.get("Container") or "",
            "Part": [{"file": src.get("Path") or "", "size": src.get("Size") or 0, "duration": dur, "container": src.get("Container") or ""}]}

def _jf_item(it):
    ids = {k.lower(): v for k, v in (it.get("ProviderIds") or {}).items() if v}
    out = {"ratingKey": ms_rk(it["Id"]), "title": it.get("Name") or "", "type": (it.get("Type") or "").lower(),
           "Guid": [{"id": "%s://%s" % (k, ids[k])} for k in ("tmdb", "tvdb", "imdb") if k in ids],
           # Plex marks a matched item with a plex:// guid; here "matched" = the server found online ids for it
           "guid": ("plex://%s/%s" % (ms_kind(), it["Id"])) if ids else "local://%s" % it["Id"],
           "thumb": "/Items/%s/Images/Primary" % it["Id"] if (it.get("ImageTags") or {}).get("Primary") else None}
    if it.get("ProductionYear"): out["year"] = it["ProductionYear"]
    t = it.get("Type")
    if t == "Episode":
        out.update(grandparentTitle=it.get("SeriesName") or "", grandparentRatingKey=ms_rk(it.get("SeriesId")),
                   parentRatingKey=ms_rk(it.get("SeasonId")), parentIndex=it.get("ParentIndexNumber"), index=it.get("IndexNumber"))
    elif t == "Season":
        out.update(index=it.get("IndexNumber"), parentRatingKey=ms_rk(it.get("SeriesId") or it.get("ParentId")))
    if it.get("MediaSources") is not None or it.get("Path"):
        srcs = it.get("MediaSources") or ([{"Path": it["Path"], "Size": 0}] if it.get("Path") and t in ("Movie", "Episode") else [])
        if srcs: out["Media"] = [_jf_media(s) for s in srcs]
    return out

def _jf_items(p, params, start=None, size=None):
    q = dict({"Recursive": "true", "Fields": _JF_FIELDS, "EnableImageTypes": "Primary", "SortBy": "SortName"}, **params)
    if start is not None: q["StartIndex"] = int(start)
    if size is not None: q["Limit"] = int(size)
    r = _jf_get(p, "/Items", q, timeout=120)
    rows = [_jf_item(it) for it in r.get("Items") or []]
    return {"MediaContainer": {"size": len(rows), "totalSize": r.get("TotalRecordCount", len(rows)), "Metadata": rows}}

def _jf_folders(p):
    r = _jf_get(p, "/Library/VirtualFolders")
    return r if isinstance(r, list) else (r.get("Items") or [])

def jf_request(p, method, path, params, headers, timeout, raw):
    params = dict(params or {}); headers = headers or {}
    start = headers.get("X-Plex-Container-Start", params.pop("X-Plex-Container-Start", None))
    size = headers.get("X-Plex-Container-Size", params.pop("X-Plex-Container-Size", None))
    name = ms_name(p)
    if path == "/identity":
        i = _jf_get(p, "/System/Info", timeout=timeout)
        return {"MediaContainer": {"machineIdentifier": i.get("Id", ""), "version": i.get("Version", ""), "friendlyName": i.get("ServerName", "")}}
    if path == "/library/sections" and method == "GET":
        dirs = []
        for f in _jf_folders(p):
            typ = {"movies": "movie", "tvshows": "show"}.get((f.get("CollectionType") or "").lower())
            if not typ: continue
            dirs.append({"key": str(ms_rk(f.get("ItemId") or f.get("Id"))), "type": typ, "title": f.get("Name") or "",
                         "agent": "tv.plex.agents.movie" if typ == "movie" else "tv.plex.agents.series",
                         "Location": [{"path": x} for x in (f.get("Locations") or [])]})
        return {"MediaContainer": {"Directory": dirs}}
    m = re.match(r"^/library/sections/(\d+)/(all|search|refresh|emptyTrash)$", path)
    if m:
        lib, what = ms_sid(m.group(1)), m.group(2)
        if what == "all" and method == "GET":
            return _jf_items(p, {"ParentId": lib, "IncludeItemTypes": _JF_TYPES.get(str(params.get("type", "1")), "Movie")}, start, size)
        if what == "search":
            return _jf_items(p, {"ParentId": lib, "IncludeItemTypes": "Episode", "SearchTerm": params.get("query", "")})
        if what == "refresh":            # rescan one folder: tell the server that path changed
            body = {"Updates": [{"Path": params.get("path", ""), "UpdateType": "Modified"}]}
            return _jf_send(p, "POST", "/Library/Media/Updated", body=body, timeout=timeout)
        if what == "emptyTrash":         # Jellyfin/Emby drop missing files on their own scan: nothing to empty
            return (200, b"") if raw else {}
        raise JFUnsupported("Locking titles isn't available on %s." % name)
    m = re.match(r"^/library/metadata/(\d+)(/[a-zA-Z]+)?$", path)
    if m:
        sid, sub = ms_sid(m.group(1)), m.group(2) or ""
        if sub == "" and method == "GET":
            r = _jf_items(p, {"Ids": sid})
            if not r["MediaContainer"]["Metadata"]: raise urllib.error.HTTPError("", 404, "Not Found", {}, None)
            return r
        if sub == "/allLeaves":
            return _jf_items(p, {"ParentId": sid, "IncludeItemTypes": "Episode"})
        if sub == "/refresh":
            return _jf_send(p, "POST", "/Items/%s/Refresh" % sid, {"Recursive": "true", "MetadataRefreshMode": "Default", "ImageRefreshMode": "Default"}, timeout=timeout)
        raise JFUnsupported({"/split": "Splitting merged items", "/posters": "Picking posters", "/poster": "Picking posters"}.get(sub, "That") + " isn't available on %s." % name)
    if path == "/photo/:/transcode":     # the cheap poster check: fetch a tiny copy of the image
        img = params.get("url") or ""
        if not img.startswith("/Items/"): return (404, b"")
        q = urllib.parse.urlencode({"maxWidth": params.get("width", 40)})
        with urllib.request.urlopen(urllib.request.Request(p["url"].rstrip("/") + img + "?" + q, headers=_jf_auth(p)), timeout=timeout) as r:
            return r.status, r.read()
    raise JFUnsupported("That isn't available on %s." % name)

# ---------------- Plex ----------------
def plex(path, **params):
    return map_in(ms_request("GET", path, params)["MediaContainer"], S["plex"])

def plex_backup():
    """v0.1.5: ask Plex to run its own "Back up database" task now (Plex keeps the copy in its Databases folder)."""
    p = S.get("plex") or {}
    if not p.get("url"): return {"ok": False, "error": "Plex isn't set up."}
    try:
        ms_request("POST", "/butler/BackupDatabase", timeout=20)
        kv_set("plex_backup_at", now_iso()); event("backup", "Asked Plex to back up its database.")
        return {"ok": True, "at": now_iso()}
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": "Plex said %d %s." % (e.code, e.reason)}
    except Exception as e:
        return {"ok": False, "error": "Couldn't reach Plex: %s" % getattr(e, "reason", e)}

def plex_tidy():
    """v0.1.5 Plex tidy-up: run Plex's own housekeeping now - Clean Bundles (artwork/metadata no item uses any more, e.g.
    old uploaded posters) and Optimize Database. Plex's own "Empty trash" is deliberately NOT run (a briefly offline disk
    would make it drop real items)."""
    p = S.get("plex") or {}
    if not p.get("url"): return {"ok": False, "error": "Plex isn't set up."}
    done, errs = [], []
    for label, path in (("Clean Bundles", "/library/clean/bundles"), ("Optimize Database", "/library/optimize?async=1")):
        try:
            ms_request("PUT", path, timeout=30); done.append(label)
        except urllib.error.HTTPError as e: errs.append("%s: Plex said %d %s" % (label, e.code, e.reason))
        except Exception as e: errs.append("%s: %s" % (label, getattr(e, "reason", e)))
    if done:
        kv_set("plex_tidy_at", now_iso()); event("plex", "Asked Plex to run %s." % " and ".join(done))
    return {"ok": bool(done), "done": done, "error": "; ".join(errs), "at": now_iso()}

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
    raw = params.pop("_raw", False)              # True: keep Agregarr placeholders
    start = 0
    while True:
        p = S["plex"]
        mc = ms_request("GET", path, params, {"X-Plex-Container-Start": str(start), "X-Plex-Container-Size": str(PAGE)}, timeout=120)["MediaContainer"]
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
        ms_request("GET", "/library/sections/%s/refresh" % section, {"path": to_app(folder, S["plex"])}, timeout=30, raw=True)
        return True
    except Exception as e:
        log("plex refresh failed: %r" % e); return False

_EDITION_RE = re.compile(r"\{edition-([^}]+)\}", re.I)

# v0.1.7: the same cut is written many ways ("Directors Cut", "Director's Cut", "DC"…) and many libraries don't use the
# {edition-...} tag at all. Editions are normalised to one name, and plain file names are read too, but only the part
# after the "(year)", so a film called "Extended Family" isn't taken for an Extended edition. "Remastered" isn't an
# edition (same cut, better transfer), so remastered copies still count as duplicates of the original.
_EDITION_WORDS = [
    (re.compile(r"director'?s[ ._-]?cut|\bdc\b", re.I), "Director's Cut"), (re.compile(r"\bfinal[ ._-]?cut\b", re.I), "Final Cut"),
    (re.compile(r"\bextended(?:[ ._-]?(?:cut|edition|version))?\b", re.I), "Extended"), (re.compile(r"\btheatrical(?:[ ._-]?(?:cut|edition|version))?\b", re.I), "Theatrical"),
    (re.compile(r"\bimax\b", re.I), "IMAX"), (re.compile(r"\b(?:unrated|uncut)\b", re.I), "Unrated"), (re.compile(r"\bspecial[ ._-]?edition\b", re.I), "Special Edition"),
    (re.compile(r"\bopen[ ._-]?matte\b", re.I), "Open Matte"), (re.compile(r"\bassembly[ ._-]?cut\b", re.I), "Assembly Cut"),
    (re.compile(r"\bultimate(?:[ ._-]?(?:cut|edition))?\b", re.I), "Ultimate"), (re.compile(r"\bredux\b", re.I), "Redux"),
    (re.compile(r"\bcriterion\b", re.I), "Criterion"), (re.compile(r"\b(?:anniversary)(?:[ ._-]?edition)?\b", re.I), "Anniversary Edition")]
_YEAR_SPLIT = re.compile(r"\((?:19|20)\d\d\)|[ ._](?:19|20)\d\d[ ._]")

def edition_name(raw):
    """'directors cut' / 'Director's Cut' / 'DC' -> "Director's Cut"; unknown names are kept, tidied."""
    raw = (raw or "").strip()
    if not raw: return ""
    for rx, name in _EDITION_WORDS:
        if rx.search(raw): return name
    return raw[:1].upper() + raw[1:]

def edition_from_name(path):
    """Edition from a file or folder name: the {edition-...} tag, else known words after the (year)."""
    x = _EDITION_RE.search(path or "")
    if x: return edition_name(x.group(1))
    base = os.path.basename(path or "")
    parts = _YEAR_SPLIT.split(base, maxsplit=1)
    if len(parts) < 2: return ""
    tail = re.sub(r"\{[^}]*\}|\[[^\]]*\]", " ", parts[1])
    for rx, name in _EDITION_WORDS:
        if rx.search(tail): return name
    return ""

def edition_of(m=None, md=None):
    """v0.1.3: Plex edition of an item or one of its copies ("Director's Cut"), from Plex's editionTitle or an
    {edition-...} tag in the file/folder name. Different editions of a film are not duplicates. v0.1.7: normalised
    names, and editions in plain file names too (see edition_from_name). Returns a lowercase key."""
    if md is not None:
        e = edition_from_name((md.get("Part") or [{}])[0].get("file") or "")
        if e: return e.lower()
    if m is not None and m.get("editionTitle"): return edition_name(str(m["editionTitle"])).lower()
    if m is not None:
        for md2 in m.get("Media", []):
            e = edition_from_name((md2.get("Part") or [{}])[0].get("file") or "")
            if e: return e.lower()
    return ""

def guids(m):
    out = {}
    for g in m.get("Guid", []):
        k, _, v = g.get("id", "").partition("://")
        if k and v: out[k] = v
    return out

def plex_link(rating_key):
    p = S["plex"]
    k = ms_kind(p)
    if k in ("jellyfin", "emby"):       # v0.2: ldrk= carries Library Doctor's own id so the link can be followed later
        base = (p.get("public") or p["url"]).rstrip("/")
        try: sid = ms_sid(rating_key)
        except Exception: sid = ""
        page = "/web/#/details?id=%s&serverId=%s" if k == "jellyfin" else "/web/index.html#!/item?id=%s&serverId=%s"
        return {"label": "Open in Plex", "url": base + page % (sid, p.get("machine", "")) + "&ldrk=%s" % rating_key}
    return {"label": "Open in Plex", "url": "%s/desktop/#!/server/%s/details?key=%s" % (
        p.get("public", "https://app.plex.tv").rstrip("/"), p["machine"], urllib.parse.quote("/library/metadata/%s" % rating_key, safe=""))}

_RK_RE = re.compile(r"(?:key=%2Flibrary%2Fmetadata%2F|[?&]ldrk=)(\d+)")
def live_plex_links(links, kind, row_id):
    """v0.1.13: Plex gives an item a new ID when it merges or re-adds it (after a restore, a clean-up, a replace).
    'Open in Plex' goes through /api/plex-open, which checks the saved ID and finds the item by its file if it moved."""
    out, jf = [], ms_kind() != "plex"
    for l in links or []:
        m = _RK_RE.search(l.get("url", "")) if l.get("label") == "Open in Plex" else None
        if m and (("ldrk=" in l["url"]) != jf): m = None   # a link from before you switched media server: leave it as it was
        out.append(dict(l, url="/api/plex-open?" + urllib.parse.urlencode({"rk": m.group(1), kind: row_id})) if m else l)
    return out

def _video_paths(v, out):
    if isinstance(v, str):
        if v.lower().endswith(VIDEO_EXT) and v.startswith(MEDIA_ROOT): out.append(v)
    elif isinstance(v, dict):
        for x in v.values(): _video_paths(x, out)
    elif isinstance(v, list):
        for x in v: _video_paths(x, out)
    return out

def plex_open(rk, issue_id=None, done_id=None):
    """URL to open: the saved Plex item if it still exists, else the item that now holds one of the files, else None."""
    try:
        plex("/library/metadata/%s" % int(rk)); return plex_link(rk)["url"]
    except urllib.error.HTTPError as e:
        if e.code != 404: return plex_link(rk)["url"]
    except Exception:
        return plex_link(rk)["url"]
    paths = []
    with db() as c:
        if issue_id:
            r = c.execute("SELECT detail FROM issues WHERE id=?", (issue_id,)).fetchone()
            if r: _video_paths(json.loads(r["detail"] or "{}"), paths)
        if done_id:
            r = c.execute("SELECT kept, moved FROM done WHERE id=?", (done_id,)).fetchone()
            if r: paths += [r["kept"]] if r["kept"] else []; _video_paths(json.loads(r["moved"] or "[]"), paths)
    hit = plex_find_by_files(paths)
    if not hit: return None
    log("plex-open: item %s is now %s" % (rk, hit["ratingKey"]))
    return plex_link(hit["ratingKey"])["url"]

def plex_find_by_files(paths):
    """The Plex film or episode that now holds one of these files (after Plex merged or re-added it), or None."""
    paths = [x for x in dict.fromkeys(paths or []) if isinstance(x, str) and os.path.exists(x)]
    if not paths: return None
    for sec in plex("/library/sections").get("Directory", []):
        locs = [l["path"].rstrip("/") + "/" for l in sec.get("Location", [])]
        if not any(x.startswith(tuple(locs)) for x in paths): continue
        for m in plex("/library/sections/%s/all" % sec["key"], type=1 if sec.get("type") == "movie" else 4).get("Metadata", []):
            for md in m.get("Media", []):
                if any(pt.get("file") in paths for pt in md.get("Part", [])): return m
    return None

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
            "parts": [x.get("file", "") for x in md.get("Part", []) if x.get("file")], "edition": edition_from_name(p.get("file", "")), **_link_info(p.get("file", ""))}

def _link_info(path):
    """v0.1.7: hardlinks. A file with more than one link (e.g. the same data also in a torrent's folder, still seeding)
    frees no space when Library Doctor moves this copy, and two "copies" with the same inode are one file."""
    try:
        st = os.stat(path)
        return {"links": st.st_nlink, "ino": "%d:%d" % (st.st_dev, st.st_ino)}
    except OSError:
        return {"links": 1, "ino": ""}

# v0.1.6: same title, different film. "The Fly (1958)" and "The Fly (1986)" have different TMDB ids, so Library Doctor never
# groups them. But when Plex MISMATCHES a file (puts the 1958 file under the 1986 entry) the two look like "2 copies" of one
# film. Before calling anything a duplicate, compare what the files themselves say: {tmdb-…}/{imdb-…} tags first, else the
# (year) in the folder or file name. Different answers = different films: no clean-up, a "Plex mixed these up" card instead.
_TMDB_TAG = re.compile(r"\{tmdb-(\d+)\}", re.I)
_IMDB_TAG = re.compile(r"\{imdb-(tt\d+)\}", re.I)
_YEAR_IN = re.compile(r"\((19\d\d|20\d\d)\)")

def _film_id_year(path):
    fid = None
    for rx, k in ((_TMDB_TAG, "tmdb"), (_IMDB_TAG, "imdb")):
        m = rx.search(path or "")
        if m: fid = (k, m.group(1).lower()); break
    yr = None
    for part in (os.path.basename(os.path.dirname(path or "")), os.path.basename(path or "")):
        m = _YEAR_IN.findall(part)
        if m: yr = m[-1]; break
    return fid, yr

def different_films(vers):
    """True when the copies' names say they're different films. For each pair: if both carry the same kind of id
    ({tmdb-…}/{imdb-…}) the ids decide (same id = same film, whatever the years say); otherwise the (year) decides when
    both have one. A name with neither can't prove anything, so it never splits a group."""
    keys = [_film_id_year(v.get("path")) for v in vers]
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            (ia, ya), (ib, yb) = keys[i], keys[j]
            if ia and ib and ia[0] == ib[0]:
                if ia != ib: return True
                continue
            if ya and yb and ya != yb: return True
    return False

def _film_label(path):
    folder = os.path.basename(os.path.dirname(path or "")) or os.path.basename(path or "")
    return re.sub(r"\s*[\[{].*$", "", folder).strip() or folder

def mismatch_card(lib, key_parts, title, vers, links, section, rk=None):
    names = []
    for v in vers:
        n = _film_label(v["path"])
        if n not in names: names.append(n)
    for v in vers: v.update(action="keep", reason="A different film, not a copy")
    return dict(id=iid("mismatch", lib, *key_parts), chk="dupes", sev="amb", title=title, library=lib,
                detail={"kind": "mismatch", "text": "Plex has put different films together: %s." % " and ".join(names),
                        "note": ("Nothing to delete: they're separate films and Plex matched one of them wrong. Split in Plex separates them "
                                 "and Plex matches each one again by itself." if rk else
                                 "Nothing to delete: they're separate films and Plex matched one of them wrong. Open it in Plex and use Fix Match on the wrong one."),
                        "versions": vers, "section": section, "rk": rk, "confidence": "check", "fixes": ["split"] if rk else []},
                links=links)

def plex_split(issue_id):
    with db() as c:
        row = c.execute("SELECT * FROM issues WHERE id=? AND chk='dupes'", (issue_id,)).fetchone()
    if not row: return {"ok": False, "error": "That card has gone; scan again."}
    d = json.loads(row["detail"])
    if d.get("kind") != "mismatch" or not d.get("rk"): return {"ok": False, "error": "Nothing to split here."}
    try:
        ms_request("PUT", "/library/metadata/%s/split" % d["rk"], timeout=30)
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": "Plex said %d %s." % (e.code, e.reason)}
    except Exception as e:
        return {"ok": False, "error": "Couldn't reach Plex (%s)." % getattr(e, "reason", e)}
    with db() as c: c.execute("DELETE FROM issues WHERE id=?", (issue_id,))
    event("dupes", "%s: asked Plex to split the films it had put together." % row["title"])
    return {"ok": True, "text": "Plex is splitting them and matching each film again. They show as separate items in a minute or two."}

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
    # v0.1.7: when Radarr/Sonarr have custom formats (TRaSH guides etc.), their score for each copy decides first, so a
    # bloated 4K file never beats the 1080p remux your own rules prefer
    use_cf = len({v.get("cf") for v in cands if v.get("cf") is not None}) > 1
    def key(v):
        dur = round(v["duration"] / max(1, longest) * 50)   # 2% buckets: near-equal lengths count as equal
        return ((v.get("cf") or 0) if use_cf else 0, v["height"], dur, good_box(v), 1 if v["tracked"] else 0, v["bitrate"], v["size"])
    best = max(cands, key=key)
    for v in cands:
        if v is best:
            why = []
            if len(cands) > 1:
                other = [o for o in cands if o is not best]
                if use_cf and any((best.get("cf") or 0) > (o.get("cf") or 0) for o in other):
                    why.append("highest %s custom format score (%d vs %d)" % (best.get("cf_app") or "Radarr/Sonarr", best.get("cf") or 0, max(o.get("cf") or 0 for o in other)))
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
            if best.get("ino") and v.get("ino") == best["ino"]:
                v["action"], v["reason"], v["sure"] = "keep", "The same file as the one kept (a hardlink), so it takes no extra space", False
                continue
            if use_cf and (v.get("cf") or 0) < (best.get("cf") or 0):
                r = "Lower %s custom format score (%d vs %d)" % (best.get("cf_app") or "Radarr/Sonarr", v.get("cf") or 0, best.get("cf") or 0)
                if v["height"] > best["height"]: v["sure"] = False; r += ", although it's a higher resolution"
            elif best["height"] > v["height"]: r = "Lower resolution than the one kept"
            elif best["duration"] - v["duration"] > 60000: r = "%s shorter than the one kept" % mins(best["duration"] - v["duration"])
            elif good_box(best) and not good_box(v): r = "Same quality, but a .%s file (the %s is kept%s)" % (
                os.path.splitext(v["path"])[1][1:].lower(), os.path.splitext(best["path"])[1][1:].upper(),
                "; Sonarr/Radarr will switch to it on the rescan" if v["tracked"] else "")
            elif best["tracked"] and not v["tracked"]: r = "Same quality; not the file Radarr/Sonarr/Sportarr tracks"
            else: r = "Same quality as the one kept"
            if (v.get("links") or 1) > 1: r += ". It's hardlinked (probably still seeding), so removing it frees no space until the torrent is removed"
            v["action"], v["reason"] = "remove", r
    for v in vers:                                     # a link to the very same data as the kept copy is never a copy to remove
        if v is not best and v.get("action") == "remove" and best.get("ino") and v.get("ino") == best["ino"]:
            v["action"], v["reason"], v["sure"] = "keep", "The same file as the one kept (a hardlink), so it takes no extra space", False
    return vers

_CF_HAS = {}
def cf_scores(vers, refs):
    """v0.1.7: ask Radarr/Sonarr to score each copy's file name with its own custom formats (/parse). Only when that app
    has custom formats at all; otherwise nothing changes."""
    ref = next((r for r in refs or [] if arr_by_name(r.get("arr"))), None)
    if not ref: return
    a = arr_by_name(ref["arr"])
    c = _CF_HAS.get(a["name"])
    if not c or time.time() - c[0] > 3600:
        try: c = (time.time(), len(arr(a, "/customformat")) > 0)
        except Exception: c = (time.time(), False)
        _CF_HAS[a["name"]] = c
    if not c[1]: return
    for v in vers:
        try:
            r = arr(a, "/parse?" + urllib.parse.urlencode({"title": os.path.splitext(os.path.basename(v["path"]))[0]}))
            if r.get("customFormatScore") is not None:
                v["cf"], v["cf_app"] = int(r["customFormatScore"]), a["name"]
                v["cf_names"] = [x.get("name") for x in r.get("customFormats") or []][:5]
                v["desc"] = (v.get("desc") or "") + " · %s score %d" % (a["name"], v["cf"])
        except Exception as e:
            log("custom format score failed: %r" % e); return

def dupes_useful(vers):
    """False when nothing is left to remove (e.g. every other copy is a hardlink of the kept one)."""
    return any(v.get("action") == "remove" for v in vers)

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

_GRAB_CACHE = {}
def arr_cannot_grab(a):
    """Why this Radarr/Sonarr can't fetch anything (no enabled download client / no indexer for searches), or None.
    Cached 5 minutes. If the check itself fails we say nothing, so a slow arr never blocks a fix."""
    if not a: return None
    hit = _GRAB_CACHE.get(a["name"])
    if hit and time.time() - hit[0] < 300: return hit[1]
    why = None
    try:
        dc = [x for x in arr(a, "/downloadclient") if x.get("enable")]
        ix = [x for x in arr(a, "/indexer") if x.get("enableAutomaticSearch")]
        missing = ([] if dc else ["download client"]) + ([] if ix else ["indexer with searches turned on"])
        if missing: why = "it has no %s" % " and no ".join(missing)
    except Exception as e:
        log("grab check %s failed: %r" % (a.get("name"), e)); return None
    _GRAB_CACHE[a["name"]] = (time.time(), why)
    return why

def cant_grab_msg(a, why, moved=False):
    where = "%s \u2192 Settings \u2192 %s" % (a["name"], "Download Clients" if "download client" in why and "indexer" not in why else "Download Clients / Indexers")
    return ("%s can't fetch a new copy: %s. %sAdd %s in %s, then try again." %
            (a["name"], why, "" if moved else "Nothing was moved. ", "them" if " and no " in why else "one", where))

def dl_when():
    """How a found release arrives: 'in your download window' only when LD_DOWNLOAD_WINDOW_END is set."""
    return "in your download window" if NIGHT_END_HOUR is not None else "as soon as your download client gets it"

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
        STATE["step"] = "Checking folders"                   # v0.1.7: scan guard
        try: blockers = folder_blockers(folder_check(force=True))
        except Exception as e: blockers = []; log("folder check failed: %r" % e)
        if blockers:
            b = blockers[0]
            ex = b.get("example") or {}
            short = lambda x: (x or "?") if len(x or "") <= 60 else x[:57] + "…"
            raise RuntimeError("Library Doctor can't see %s's files (%s), so nothing was checked. Fix it in Settings → Folder check. Example: %s says %s, looked for %s." % (
                b["app"], b["msg"].rstrip(".").lower(), b["app"], short(ex.get("app")), short(ex.get("local"))))
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
        libstat = {}                                           # v0.1.9 Analysis: size, resolution and codec per library
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
                try: _lib_count(libstat, lib, kind, m)
                except Exception: pass
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
                    for md in live: eds.setdefault(edition_of(m, md) if edition_from_name((md.get("Part") or [{}])[0].get("file") or "") else "", []).append(md)
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
                elif len(live_d) > 1 and kind == "movie" and different_films([version(md) for md in live_d]):
                    issues.append(mismatch_card(lib, (m["ratingKey"],), title, [version(md) for md in live_d], links, sec["key"], rk=m["ratingKey"]))
                elif len(live_d) > 1:
                    vers = [version(md) for md in live_d]
                    if kind == "show":
                        for a, it in arr_series: tracked |= sonarr_tracked(a, it["id"])
                    cf_scores(vers, refs); recommend(vers, tracked)
                    if dupes_useful(vers): issues.append(dict(id=iid("dupe-item", lib, m["ratingKey"]), chk="dupes", sev="amb", title=title, library=lib,
                                       detail={"text": "%d copies in one Plex item." % len(vers), "versions": vers,
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
                    if kind == "movie" and different_films(vers):
                        issues.append(mismatch_card(lib, ("guid", str(k)), title, vers,
                                                    [plex_link(m["ratingKey"]) for m, *_ in lst] + [l for l in links if l["label"] != "Open in Plex"], sec["key"]))
                        continue
                    cf_scores(vers, refs); recommend(vers, tr)
                    if not dupes_useful(vers): continue
                    issues.append(dict(id=iid("dupe-guid", lib, k), chk="dupes", sev="amb", title=title, library=lib,
                                       detail={"text": "%d separate Plex items for the same %s." % (len(lst), "film" if kind == "movie" else "episode"),
                                               "versions": vers, "leftovers": leftovers(vers), "confidence": confidence(vers), "section": sec["key"], "refs": refs},
                                       links=[plex_link(m["ratingKey"]) for m, *_ in lst] + [l for l in links if l["label"] != "Open in Plex"]))
        try:
            issues += health_scan(sections, movies, series, arr_files, trashed)
        except Exception as e:
            log("file health check failed: %r" % e)
        try:
            issues += groups_scan(movies, series)                     # v0.1.7: release groups to avoid
        except Exception as e:
            log("release group check failed: %r" % e)
        try:
            issues += gaps_scan(series)                               # v0.1.6: season gaps
        except Exception as e:
            log("season gaps check failed: %r" % e)
        try:
            issues += lang_scan(sections, movies, series, trashed)    # v0.1.6: audio language
        except Exception as e:
            log("language check failed: %r" % e)
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
        sizes = {}
        try:
            q, sizes = quality_scan(sections, movies, series, trashed); issues += q
            try: monitor_state(q)                             # v32: Monitor button where Sonarr/Radarr won't upgrade by itself
            except Exception as e: log("monitor check failed: %r" % e)
            try: issues += queue_cards()                      # v33: Downloads (Radarr/Sonarr queues)
            except Exception as e: log("queue check failed: %r" % e)
            try: issues += apps_cards()                       # v0.1.5: App health
            except Exception as e: log("app health check failed: %r" % e)
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
        # v0.1.7 safety net: most of the library "missing" means a folder problem, not a missing library
        note = ""
        miss = [it for it in issues if it["chk"] == "missing" and it["detail"].get("kind") != "gaps"]
        if len(miss) > 50 and len(miss) > 0.3 * max(1, n_files):
            issues = [it for it in issues if not (it["chk"] == "missing" and it["detail"].get("kind") != "gaps")]
            note = "Suspect: %d of %d files looked missing, which usually means a folder mapping problem, so they aren't shown. Check Settings → Folder check." % (len(miss), n_files)
            log(note)
        finished = now_iso()
        with db() as c:
            sid = c.execute("INSERT INTO scans (started, finished, ok, items, files, issues, note) VALUES (?,?,?,?,?,?,?)",
                            (started, finished, 1, n_items, n_files, len(issues), note)).lastrowid
            seen = {r["id"]: r["first_seen"] for r in c.execute("SELECT id, first_seen FROM issues")}
            for it in issues:                                  # v0.1.9: a problem that wasn't there last scan (new, or back again)
                if it["id"] not in seen and it["chk"] not in ("unwatched", "queue"):
                    c.execute("INSERT INTO appearances (issue_id, chk, kind, title, library, at) VALUES (?,?,?,?,?,?)",
                              (it["id"], it["chk"], (it["detail"] or {}).get("kind") or "", it["title"][:200], it["library"], finished))
            c.execute("DELETE FROM issues")
            for it in issues:
                c.execute("INSERT OR REPLACE INTO issues VALUES (?,?,?,?,?,?,?,?,?,?)",
                          (it["id"], sid, it["chk"], it["sev"], it["title"], it["library"], json.dumps(it["detail"]),
                           json.dumps(it["links"]), seen.get(it["id"], finished), finished))
        log("scan done: %d items, %d files, %d issues" % (n_items, n_files, len(issues)))
        try: kv_set("library_stats", {"at": finished, "libs": libstat})
        except Exception as e: log("library stats failed: %r" % e)
        try: score_snapshot()
        except Exception as e: log("score snapshot failed: %r" % e)
        try: space_record()
        except Exception as e: log("space record failed: %r" % e)
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
    status, note = "warn", "Couldn't reach %s to check." % ms_name()
    # Plex rescans a folder within seconds; Jellyfin/Emby batch change notices (about a minute or two), so wait longer
    for attempt in range(20 if ms_kind() == "plex" else 80):     # up to ~60 s (Plex) / ~4 min (Jellyfin, Emby)
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
    ms_request("PUT", path, timeout=30, raw=True)

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

def outside_arr_warning(row, vers, kept):
    """v0.1.15: you kept a copy that isn't the tracked one and sits in another folder, so Radarr/Sonarr lose their file."""
    trk = next((v for v in vers if v.get("tracked") and v is not kept), None)
    if not trk or kept.get("tracked") or os.path.dirname(kept["path"]) == os.path.dirname(trk["path"]): return None
    app = next((l["label"][8:] for l in json.loads(row["links"] or "[]") if l.get("label", "").startswith("Open in ") and l["label"] != "Open in Plex"), "Radarr/Sonarr")
    return ("%s doesn't see the copy you kept (it's in another folder), so it now thinks this is missing and may download it again. "
            "Move the kept file into %s or point %s at it." % (app, os.path.dirname(trk["path"]), app))

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
            rks = [m.group(1) for l in links for m in [_RK_RE.search(l.get("url", ""))] if m]
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
            warn = outside_arr_warning(row, vers, keep[0])
            if warn: event("cleanup", "%s: %s" % (row["title"], warn))
            return {"ok": True, "moved": len(moved), "side_files": side_n, "freed_later": human(sum(s for _, s in moved)), "rescanned": rescanned, "problems": problems,
                    "warn": warn}
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
        tm = kv_get("trash_modes") or {}
        if str(trash_id) in tm:                      # v0.2.1: a quarantined program file gets its old permissions back
            try: os.chmod(t["src"], int(tm.pop(str(trash_id))))
            except (OSError, ValueError): pass
            kv_set("trash_modes", tm)
        with db() as c:
            c.execute("UPDATE trash SET restored_at=? WHERE id=?", (now_iso(), trash_id))
        for sec in plex("/library/sections").get("Directory", []):
            if any(t["src"].startswith(l["path"].rstrip("/") + "/") for l in sec.get("Location", [])):
                plex_refresh_folder(sec["key"], os.path.dirname(t["src"]))
        event("restore", "Put back %s" % os.path.basename(t["src"]))
        threading.Thread(target=_restore_followup, args=(dict(t),), daemon=True).start()
        return {"ok": True}

def _restore_followup(t):
    """v0.1.14: after a file is put back, Radarr/Sonarr must see it again (else they keep searching for a copy you
    chose to keep), and a Replace that was waiting for a new copy stops waiting."""
    d, ref = None, None
    with db() as c:
        for r in c.execute("SELECT * FROM done WHERE issue_id=? ORDER BY id DESC", (t["issue_id"] or "",)):
            if t["id"] in json.loads(r["trash_ids"] or "[]"): d = dict(r); break
    try: ref = json.loads((d or {}).get("ref") or "null")
    except Exception: ref = None
    told = None
    try:
        a = arr_by_name(ref.get("arr")) if isinstance(ref, dict) and ref.get("arr") else None
        if a and ref.get("kind") == "movie":
            arr(a, "/command", {"name": "RescanMovie", "movieId": int(ref["id"])}, "POST"); told = a["name"]
        elif a and ref.get("series"):
            arr(a, "/command", {"name": "RescanSeries", "seriesId": int(ref["series"])}, "POST"); told = a["name"]
        else:                                          # no record of which item: find it by folder
            for a in S.get("arrs", []):
                items = arr(a, "/movie" if a["kind"] == "radarr" else "/series")
                hit = next((x for x in items if x.get("path") and t["src"].startswith(x["path"].rstrip("/") + "/")), None)
                if hit:
                    arr(a, "/command", {"name": "RescanMovie", "movieId": hit["id"]} if a["kind"] == "radarr" else {"name": "RescanSeries", "seriesId": hit["id"]}, "POST")
                    told = a["name"]; break
    except Exception as e:
        log("restore: couldn't ask Radarr/Sonarr to rescan %s: %r" % (t["src"], e))
    if d and d["status"] in ("waiting", "checking") and "(replaced)" in (t["reason"] or ""):
        note = "You put the file back, so Library Doctor stopped waiting for a new copy." + \
               (" %s was asked to rescan; if it already grabbed a new copy, that still arrives." % told if told else "")
        with db() as c:
            c.execute("UPDATE done SET status='ok', note=?, checked_at=? WHERE id=?", (note, now_iso(), d["id"]))
        event("restore", "%s: %s" % (d["title"], note))
    elif told:
        log("restore: asked %s to rescan for %s" % (told, os.path.basename(t["src"])))

# v0.1.8: a closer look before anything leaves the trash for good. A file is kept back (and you're told why) when:
#   - a duplicate clean-up's KEPT copy is no longer on the disk,
#   - a replaced file (broken / too short / wrong language) has no good new copy yet,
#   - an old .ts copy's new .mkv isn't there (or is tiny),
#   - a "Plex fix in progress" file is still in the trash, i.e. the only copy.
# Side files, subtitles, virus/fake files and old downloads need no check. Deleting chosen files by hand still works.
_HOLD_SKIP = ("Leftover side file", "Advert-only subtitle", "Virus", "Fake video", "Old download", "Leftover Tdarr temp file", "Left in the incomplete folder")

def purge_check(t):
    r = t["reason"] or ""
    if r.startswith(_HOLD_SKIP): return None
    if r.startswith("Plex fix"):
        return "it's the only copy: a Plex listing fix didn't finish, so put it back"
    if r.startswith("Old .ts copy"):
        mkv = os.path.splitext(t["src"])[0] + ".mkv"
        try:
            if os.path.getsize(mkv) >= BROKEN_BYTES: return None
            return "the new .mkv is only %s" % human(os.path.getsize(mkv))
        except OSError: return "the new .mkv isn't there"
    with db() as c:
        d = c.execute("SELECT * FROM done WHERE issue_id=? ORDER BY id DESC LIMIT 1", (t["issue_id"],)).fetchone() if t["issue_id"] else None
    if "(replaced)" in r:
        if not d: return "there's no record of a new copy arriving"
        if d["status"] in ("waiting", "checking"): return "its new copy hasn't arrived yet"
        if d["status"] != "ok": return "its new copy had a problem: %s" % (d["note"] or "")[:120]
        if (d["note"] or "").startswith("Removed from"): return None              # you removed the film/show yourself
        try:
            ref = json.loads(d["ref"] or "{}"); a = arr_by_name(ref.get("arr"))
            if a and ref.get("kind") == "movie":
                m = arr(a, "/movie/%d" % int(ref["id"])); f = (m.get("movieFile") or {}) if m.get("hasFile") else {}
                if not f.get("path") or not os.path.exists(f["path"]): return "%s no longer has a new copy on the disk" % a["name"]
                if (f.get("size") or 0) < BROKEN_BYTES: return "the new copy in %s is only %s" % (a["name"], human(f.get("size") or 0))
            elif a and ref.get("kind") == "episode":
                e = arr(a, "/episode/%d" % int(ref["id"]))
                if not e.get("hasFile"): return "%s no longer has a new copy of the episode" % a["name"]
        except urllib.error.HTTPError as e:
            if e.code != 404: return "couldn't check with %s (%s)" % (ref.get("arr"), e.code)
        except Exception as e:
            return "couldn't check with %s (%s)" % (ref.get("arr") if isinstance(ref, dict) else "Radarr/Sonarr", getattr(e, "reason", e))
        return None
    if d and d["chk"] == "dupes":                                                  # a duplicate clean-up: the kept copy must still be there
        kept = [k for k in re.split(r"\n|\|", d["kept"] or "") if k.strip()]
        missing = [k.strip() for k in kept if not os.path.exists(k.strip()) and not _upgraded_copy(k.strip())]
        if missing: return "the copy that was kept (%s) is no longer on the disk" % os.path.basename(missing[0])
    return None

_VIDEO_EXT = (".mkv", ".mp4", ".avi", ".m4v", ".ts", ".mov", ".wmv")
def _upgraded_copy(kept):
    """Radarr/Sonarr may have upgraded the kept copy since (new name). Fine if the same folder still has a good video for
    the same film, or for the same SxxEyy episode."""
    folder, name = os.path.dirname(kept), os.path.basename(kept)
    ep = EP_RE.search(name)
    try:
        for n in os.listdir(folder):
            if not n.lower().endswith(_VIDEO_EXT): continue
            if ep:
                m = EP_RE.search(n)
                if not m or (int(m.group(1)), int(m.group(2))) != (int(ep.group(1)), int(ep.group(2))): continue
            if os.path.getsize(os.path.join(folder, n)) >= BROKEN_BYTES: return True
    except OSError: pass
    return False

def trash_review(days_ahead=2):
    """Check the files due to leave the trash soon; remember which are held (shown in the trash and the warning banner)."""
    with db() as c:
        rows = c.execute("SELECT * FROM trash WHERE restored_at IS NULL AND purged_at IS NULL").fetchall()
    soon = datetime.now() + timedelta(days=days_ahead)
    held = {}
    for t in rows:
        if datetime.fromisoformat(t["moved_at"]) + timedelta(days=TRASH_DAYS) > soon: continue
        try: why = purge_check(t)
        except Exception as e: why = "couldn't be checked (%s)" % e
        if why: held[str(t["id"])] = why
    old = kv_get("trash_held") or {}
    kv_set("trash_held", held)
    new = {k: v for k, v in held.items() if k not in old}
    if new:
        event("purge", "Kept %d file%s back in the trash after a closer look: %s" % (len(new), "" if len(new) == 1 else "s", "; ".join(list(new.values())[:3])))
    return held

def purge(older_than_days=None):
    """Delete trashed files for good. older_than_days=None empties everything. v0.1.8: files the closer look holds back stay."""
    held = trash_review(0 if older_than_days is not None else 10000)
    with MOVE_LOCK:
        with db() as c:
            rows = c.execute("SELECT * FROM trash WHERE restored_at IS NULL AND purged_at IS NULL").fetchall()
        cutoff = datetime.now() - timedelta(days=older_than_days) if older_than_days is not None else None
        n, freed = 0, 0
        kept_back = 0
        for t in rows:
            if cutoff and datetime.fromisoformat(t["moved_at"]) > cutoff: continue
            if str(t["id"]) in held: kept_back += 1; continue
            hl = False
            if os.path.realpath(t["dst"]).startswith(TRASH_DIR + os.sep) and os.path.isfile(t["dst"]):
                hl = os.stat(t["dst"]).st_nlink > 1        # v0.1.7: another link still holds the data, so nothing is freed
                os.remove(t["dst"]); n += 1; freed += 0 if hl else (t["size"] or 0)
            with db() as c:
                c.execute("UPDATE trash SET purged_at=?, reason=? WHERE id=?", (now_iso(), (t["reason"] or "") + (" (hardlinked)" if hl else ""), t["id"]))
        # tidy empty folders inside the trash
        for root, dirs, files in os.walk(TRASH_DIR, topdown=False):
            if root != TRASH_DIR and not os.listdir(root):
                try: os.rmdir(root)
                except OSError: pass
        if n: event("purge", "Emptied %d file%s from the trash (%s freed)." % (n, "" if n == 1 else "s", human(freed)))
        return {"ok": True, "deleted": n, "freed": human(freed), "kept_back": kept_back}

def purge_ids(ids):
    """Delete chosen trash entries for good."""
    ids = [int(i) for i in ids if str(i).isdigit()][:500]
    with MOVE_LOCK:
        n, freed = 0, 0
        for tid in ids:
            with db() as c:
                t = c.execute("SELECT * FROM trash WHERE id=? AND restored_at IS NULL AND purged_at IS NULL", (tid,)).fetchone()
            if not t: continue
            hl = False
            if os.path.realpath(t["dst"]).startswith(TRASH_DIR + os.sep) and os.path.isfile(t["dst"]):
                hl = os.stat(t["dst"]).st_nlink > 1
                os.remove(t["dst"]); n += 1; freed += 0 if hl else (t["size"] or 0)
            with db() as c:
                c.execute("UPDATE trash SET purged_at=?, reason=? WHERE id=?", (now_iso(), (t["reason"] or "") + (" (hardlinked)" if hl else ""), tid))
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
    try:
        st, body = ms_request("GET", "/photo/:/transcode", {"width": 40, "height": 60, "minSize": 1, "url": thumb}, timeout=30, raw=True)
        return st == 200 and len(body) > 200
    except Exception: return False

def season_posters(sh, seasons, lib, sec):
    bad = [{"rk": str(x["ratingKey"]), "t": x.get("title") or "Season %s" % x.get("index")} for x in seasons if not plex_img_ok(x.get("thumb"))]
    if not bad: return []
    bad.sort(key=lambda x: (len(x["t"]), x["t"]))
    return [_meta("seasonposters", lib, sh["ratingKey"], sh.get("title") or "?", "att",
                  "%d season%s show a blank poster in Plex: no poster is picked for %s." % (len(bad), "" if len(bad) == 1 else "s", "it" if len(bad) == 1 else "them"),
                  "Pick posters chooses the top poster Plex already has for each season (from TMDB). You can still change it in Plex afterwards."
                  if ms_kind() == "plex" else "Refresh asks %s to look for posters again. If none turns up, pick one on the show's page in %s (Edit images)." % (ms_name(), ms_name()),
                  ["posters", "refresh"] if ms_kind() == "plex" else ["refresh"], [plex_link(sh["ratingKey"])], section=sec, seasons=bad)]

def _meta(kind, lib, rk, title, sev, text, note, fixes, links, **extra):
    return dict(id=iid("meta", kind, lib, rk), chk="meta", sev=sev, title=title, library=lib,
                detail=dict(kind=kind, rk=str(rk), text=text, note=note, fixes=fixes, **extra), links=links)

def movie_meta(m, lib, sec, std, owners, movies, by_path):
    rk, g = m["ratingKey"], guids(m)
    title, links = "%s (%s)" % (m.get("title"), m.get("year", "?")), [plex_link(m["ratingKey"])]
    if std and not m.get("guid", "").startswith("plex://"):
        return [_meta("unmatched", lib, rk, title, "att", "Plex couldn't match this to a film, so it has no proper info or artwork.",
                      "Refresh tries the match again. If that doesn't work, use Fix Match on the film's page in Plex.", ["refresh"], links, section=sec,
                      paths=[pt["file"] for md in m.get("Media", []) for pt in md.get("Part", []) if pt.get("file")][:5])]
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
                      "Refresh tries the match again. If that doesn't work, use Fix Match on the show's page in Plex.", ["refresh"], links, section=sec,
                      paths=[pt["file"] for e in (eps or [])[:3] for md in e.get("Media", []) for pt in md.get("Part", []) if pt.get("file")][:5])]
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
                ("Refresh asks %s to look again with its own info." % ms_name()) if ms_kind() != "plex" else "Use %s's titles copies them into Plex and locks them so Plex can't change them back. Refresh asks Plex to look again with its own info, which sometimes still lacks them." % a["name"],
                ["titles", "refresh"] if ms_kind() == "plex" else ["refresh"], links + [arr_link(a, s, "series")], section=sec, eps=fix[:400]))
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
        if not _plex_item(d["rk"]):                   # v0.1.13: Plex merged or re-added the item under a new ID
            hit = plex_find_by_files(d.get("paths"))
            if not hit:
                raise RuntimeError("Plex no longer has this item (it may have merged it with another copy). Scan again to bring the list up to date.")
            new = str(hit.get("grandparentRatingKey") or hit["ratingKey"]) if hit.get("type") == "episode" else str(hit["ratingKey"])
            log("metafix %s: Plex item %s is now %s" % (issue_id, d["rk"], new))
            d = dict(d, rk=new)
            with db() as c:
                r = c.execute("SELECT links FROM issues WHERE id=?", (issue_id,)).fetchone()
                links = [plex_link(new)] + [l for l in json.loads((r["links"] if r else None) or "[]") if l.get("label") != "Open in Plex"]
                c.execute("UPDATE issues SET detail=?, links=? WHERE id=?", (json.dumps(d), json.dumps(links), issue_id))
                c.execute("UPDATE done SET links=? WHERE id=?", (json.dumps(links), done_id))
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
        elif d["kind"] == "unmatched" and _meta_left(d)[0]:
            status, note = "ok", _meta_left(d)[1]     # Plex matched it by itself in the meantime
            tries = 0
        else:
            plex_put("/library/metadata/%s/refresh" % d["rk"])
            event("metadata", "%s: asked Plex to refresh its info." % title)
            tries = 24                                # Plex refreshes in the background: give it up to ~2 min
        for _ in range(tries):
            time.sleep(5)
            ok, note, left = _meta_left(d)
            if ok: status = "ok"; break
    except RuntimeError as e:
        note = str(e)
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
                                detail={"kind": "broken" if broken else "short", "text": text, "note": note, "path": f, "size": size, "links": _link_info(f)["links"],
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
        a_chk = arr_by_name(ref.get("arr")); why = arr_cannot_grab(a_chk)
        if why: return {"ok": False, "error": cant_grab_msg(a_chk, why)}
        if d.get("kind") == "lang": return lang_fix(issue_id, row, d)
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
        note = "%s is searching for a new copy. If it finds one, it downloads %s and shows up in Plex by itself." % (a["name"], dl_when()) if not has else \
               "Asked %s to search, but it still thinks it has the old file. Its next disk scan will catch up." % a["name"]
    except Exception as e:
        note = "Moved to the trash, but couldn't ask %s to search: %s" % (ref.get("arr"), e)
    with db() as c:
        c.execute("UPDATE done SET status=?, note=?, checked_at=? WHERE id=?", (status, note, now_iso(), done_id))
    log("health search %s: %s" % (done_id, note))

# ---------------- release groups to avoid (v0.1.7) ----------------
# When one release group keeps supplying files Library Doctor had to replace (broken, too short, wrong audio language),
# suggest telling Radarr/Sonarr to skip it. Careful by design: a big group with many good files also has a few bad ones,
# so it needs 3+ replaced files across 2+ titles in 180 days AND at least a quarter of that group's files in your library.
GROUP_WINDOW_DAYS, GROUP_MIN_FILES, GROUP_MIN_TITLES, GROUP_MIN_RATE = 180, 3, 2, 0.25
GROUP_PROFILE = "Library Doctor: groups to avoid"
_GRP_RE = re.compile(r"-([A-Za-z0-9]{2,20})$")

def _group_of(path):
    m = _GRP_RE.search(os.path.splitext(os.path.basename(path or ""))[0])
    return m.group(1) if m else ""

def groups_scan(movies, series):
    since = (datetime.now() - timedelta(days=GROUP_WINDOW_DAYS)).isoformat(timespec="seconds")
    with db() as c:
        rows = [dict(r) for r in c.execute("SELECT title, src, reason FROM trash WHERE moved_at > ? AND reason LIKE '%(replaced)%'", (since,))]
    bad = {}
    for r in rows:
        g = _group_of(r["src"])
        if not g: continue
        e = bad.setdefault(g.lower(), {"name": g, "files": 0, "titles": set(), "why": {}})
        e["files"] += 1; e["titles"].add(re.sub(r"\s+S\d\dE\d\d.*$| Season \d+$", "", r["title"] or ""))
        w = (r["reason"] or "").split(" (")[0]; e["why"][w] = e["why"].get(w, 0) + 1
    cand = {k: v for k, v in bad.items() if v["files"] >= GROUP_MIN_FILES and len(v["titles"]) >= GROUP_MIN_TITLES}
    if not cand: return []
    have = {}
    for lst in movies.values():
        for a, it in lst:
            g = ((it.get("movieFile") or {}).get("releaseGroup") or "").lower()
            if g in cand: have[g] = have.get(g, 0) + 1
    for a in [x for x in S.get("arrs", []) if x.get("kind") == "sonarr"]:
        try:
            for sr in arr(a, "/series"):
                if not (sr.get("statistics") or {}).get("episodeFileCount"): continue
                for f in arr(a, "/episodefile?seriesId=%d" % sr["id"]):
                    g = (f.get("releaseGroup") or "").lower()
                    if g in cand: have[g] = have.get(g, 0) + 1
        except Exception as e: log("group totals %s failed: %r" % (a["name"], e))
    blocked = set()
    for a in S.get("arrs", []):
        try:
            for rp in arr(a, "/releaseprofile"):
                if rp.get("name") == GROUP_PROFILE: blocked |= {str(t).lower() for t in rp.get("ignored") or []}
        except Exception: pass
    out = []
    for k, v in cand.items():
        total = v["files"] + have.get(k, 0); rate = v["files"] / max(1, total)
        if rate < GROUP_MIN_RATE or any(k in t for t in blocked): continue
        why = ", ".join("%d %s" % (n, w.lower()) for w, n in sorted(v["why"].items(), key=lambda x: -x[1]))
        out.append(dict(id=iid("group", k), chk="quality", sev="att", title="Release group %s" % v["name"], library="Radarr/Sonarr",
                        detail={"kind": "group", "group": v["name"], "count": v["files"], "rate": round(rate, 2),
                                "text": "%d of the %d files from %s in your library had to be replaced in the last %d days (%s), across %d titles: %s." % (
                                    v["files"], total, v["name"], GROUP_WINDOW_DAYS, why, len(v["titles"]), ", ".join(sorted(v["titles"])[:4]) + (" …" if len(v["titles"]) > 4 else "")),
                                "note": "Block it adds %s to a \"%s\" release profile in each Radarr/Sonarr, so they skip its releases from now on. Files you already have aren't touched; remove the word there to undo." % (v["name"], GROUP_PROFILE),
                                "fixes": ["blockgroup"]}, links=[]))
    return out

def group_block(issue_id, row, d):
    term = "/-%s\\b/i" % re.escape(d["group"])                # matches "...x264-GROUP" at the end of a release name
    done, errs = [], []
    for a in S.get("arrs", []):
        try:
            rps = arr(a, "/releaseprofile"); rp = next((x for x in rps if x.get("name") == GROUP_PROFILE), None)
            if rp:
                if term not in (rp.get("ignored") or []):
                    rp["ignored"] = (rp.get("ignored") or []) + [term]; arr(a, "/releaseprofile/%d" % rp["id"], rp, "PUT")
            else:
                arr(a, "/releaseprofile", {"name": GROUP_PROFILE, "enabled": True, "required": [], "ignored": [term], "indexerId": 0, "tags": []}, "POST")
            done.append(a["name"])
        except Exception as e:
            errs.append("%s: %s" % (a["name"], getattr(e, "reason", e)))
    if not done: return {"ok": False, "error": "Couldn't change any Radarr/Sonarr. " + "; ".join(errs)}
    with db() as c: c.execute("DELETE FROM issues WHERE id=?", (issue_id,))
    event("quality", "Blocked release group %s in %s." % (d["group"], ", ".join(done)))
    return {"ok": True, "error": "; ".join(errs)}

# ---------------- season gaps (v0.1.6) ----------------
# Missing Files covers files Plex lists that aren't on the disk. This covers episodes that never arrived at all: Sonarr has
# them on its wanted list (monitored, aired) but no copy. One card per show, seasons as ranges ("Season 2: 6–10 · Season 3: 1, 7").
def _ranges(nums):
    nums = sorted(set(nums)); out = []; i = 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1: j += 1
        out.append(str(nums[i]) if i == j else "%d, %d" % (nums[i], nums[j]) if j == i + 1 else "%d\u2013%d" % (nums[i], nums[j]))
        i = j + 1
    return ", ".join(out)

def gaps_scan(series):
    out = []; now = datetime.now(timezone.utc)
    searched = kv_get("gaps_searched") or {}
    by_id = {}
    for lst in series.values():
        for a, sr in lst: by_id[(a["name"], sr["id"])] = (a, sr)
    for a in [x for x in S.get("arrs", []) if x.get("kind") == "sonarr"]:
        try: recs = arr(a, "/wanted/missing?" + urllib.parse.urlencode({"pageSize": 5000, "includeSeries": "true", "monitored": "true", "sortKey": "airDateUtc", "sortDirection": "ascending"})).get("records", [])
        except Exception as e:
            log("season gaps %s failed: %r" % (a["name"], e)); continue
        shows = {}
        for e in recs:
            if not e.get("seasonNumber") or not e.get("airDateUtc"): continue          # no specials, nothing unaired
            try:
                if datetime.fromisoformat(e["airDateUtc"].replace("Z", "+00:00")) > now - timedelta(hours=12): continue
            except ValueError: continue
            shows.setdefault(e["seriesId"], {"series": e.get("series") or {}, "seasons": {}})["seasons"].setdefault(e["seasonNumber"], []).append(e)
        for sid, v in shows.items():
            a2, sr = by_id.get((a["name"], sid), (a, v["series"]))
            stats = {x.get("seasonNumber"): (x.get("statistics") or {}) for x in (sr.get("seasons") or [])}
            seasons, parts, total = [], [], 0
            for sn, eps in sorted(v["seasons"].items()):
                nums = [e.get("episodeNumber") or 0 for e in eps]
                full = (stats.get(sn) or {}).get("episodeFileCount", 1) == 0
                seasons.append({"s": sn, "eps": sorted(nums), "ids": [e["id"] for e in eps], "full": full})
                parts.append("Season %d: %s" % (sn, ("all %d episodes" % len(nums)) if full else ("episode " if len(nums) == 1 else "episodes ") + _ranges(nums)))
                total += len(nums)
            cid = iid("gaps", a["name"], sid)
            note = "Sonarr has them on its wanted list but never found a copy. Search for these asks it to look again now (whole seasons as season packs); anything it finds downloads " + dl_when() + "."
            if searched.get(cid): note += " Last searched from here %s." % searched[cid][:10]
            out.append(dict(id=cid, chk="missing", sev="att", title=sr.get("title") or v["series"].get("title") or "Show %d" % sid, library=a["name"],
                            detail={"kind": "gaps", "text": "%d episode%s never downloaded. %s." % (total, "" if total == 1 else "s", " · ".join(parts)),
                                    "note": note, "count": total, "seasons": seasons, "ref": {"arr": a["name"], "series": sid}, "fixes": ["gapsearch"]},
                            links=[arr_link(a2, sr, "series")] if sr.get("titleSlug") else []))
    return out

def gaps_fix(issue_id):
    with db() as c:
        row = c.execute("SELECT * FROM issues WHERE id=? AND chk='missing'", (issue_id,)).fetchone()
    if not row: return {"ok": False, "error": "That card has gone; scan again."}
    d = json.loads(row["detail"])
    if d.get("kind") != "gaps": return {"ok": False, "error": "Nothing to search here."}
    a = arr_by_name(d["ref"]["arr"]); sid = int(d["ref"]["series"]); n = 0
    why = arr_cannot_grab(a)
    if why: return {"ok": False, "error": cant_grab_msg(a, why, moved=True).replace("a new copy", "the missing episodes")}
    try:
        eps = []
        for x in d.get("seasons") or []:
            if x.get("full"): arr(a, "/command", {"name": "SeasonSearch", "seriesId": sid, "seasonNumber": int(x["s"])}, "POST"); n += 1
            else: eps += [int(i) for i in x.get("ids") or []]
        if eps: arr(a, "/command", {"name": "EpisodeSearch", "episodeIds": eps}, "POST"); n += 1
    except Exception as e:
        return {"ok": False, "error": "Couldn't ask %s to search (%s)." % (a["name"], getattr(e, "reason", e))}
    sk = kv_get("gaps_searched") or {}; sk[issue_id] = now_iso(); kv_set("gaps_searched", sk)
    event("missing", "%s: asked %s to search for the %d missing episode%s." % (row["title"], a["name"], d.get("count") or 0, "" if d.get("count") == 1 else "s"))
    return {"ok": True, "text": ("%s is searching for the %d missing episode%s of %s. Anything it finds downloads " + dl_when() + "; the card goes when they've arrived.") % (
        a["name"], d.get("count") or 0, "" if d.get("count") == 1 else "s", row["title"])}

# ---------------- audio language (v0.1.6) ----------------
# A dubbed copy (an English film with only Russian audio) looks fine everywhere until you press play. Radarr and Sonarr
# already read every file's audio languages (mediaInfo.audioLanguages) and know each title's original language, so:
# flag a file only when its audio is clearly another language AND the title was made in your language. Untagged audio
# ("und", which is how many English files are labelled) never counts, and foreign-language originals are never flagged.
LANGS = {   # Settings name -> the codes Radarr/Sonarr/ffprobe use for it
    "English": {"eng", "en", "english"}, "German": {"ger", "deu", "de", "german"}, "French": {"fre", "fra", "fr", "french"},
    "Spanish": {"spa", "es", "spanish"}, "Italian": {"ita", "it", "italian"}, "Dutch": {"dut", "nld", "nl", "dutch"},
    "Portuguese": {"por", "pt", "portuguese"}, "Polish": {"pol", "pl", "polish"}, "Swedish": {"swe", "sv", "swedish"},
    "Danish": {"dan", "da", "danish"}, "Norwegian": {"nor", "nob", "nno", "no", "nb", "norwegian"}, "Finnish": {"fin", "fi", "finnish"},
    "Czech": {"cze", "ces", "cs", "czech"}, "Hungarian": {"hun", "hu", "hungarian"}, "Greek": {"gre", "ell", "el", "greek"},
    "Turkish": {"tur", "tr", "turkish"}, "Russian": {"rus", "ru", "russian"}, "Japanese": {"jpn", "ja", "japanese"},
    "Korean": {"kor", "ko", "korean"}, "Chinese": {"chi", "zho", "zh", "chinese", "cmn", "yue"}, "Hindi": {"hin", "hi", "hindi"},
    "Arabic": {"ara", "ar", "arabic"}, "Hebrew": {"heb", "he", "hebrew"}, "Thai": {"tha", "th", "thai"}, "Bengali": {"ben", "bn", "bengali"},
    "Ukrainian": {"ukr", "uk", "ukrainian"}, "Romanian": {"rum", "ron", "ro", "romanian"}, "Vietnamese": {"vie", "vi", "vietnamese"},
    "Indonesian": {"ind", "id", "indonesian"}, "Tagalog": {"tgl", "tl", "fil", "tagalog", "filipino"}, "Tongan": {"ton", "to", "tongan"},
}
LANG_UNKNOWN = {"", "und", "unk", "unknown", "zxx", "mul", "mis", "qaa", "none"}
_LANG_NAME = {c: n for n, cs in LANGS.items() for c in cs}

def pref_language():
    v = S.get("language")
    if v is None: return "English"                           # default for everyone who never touched the setting
    return v if v in LANGS else ""                           # "" = check switched off

def _audio(mi):
    return [t.strip().lower() for t in re.split(r"[/,]", (mi or {}).get("audioLanguages") or "") if t.strip()]

def _wrong_audio(codes, pref):
    """True when the audio is clearly in other languages only (nothing in pref, at least one real language tag)."""
    want = LANGS.get(pref) or set()
    real = [c for c in codes if c not in LANG_UNKNOWN]
    return bool(real) and not any(c in want for c in codes)

def _lang_names(codes):
    out = []
    for c in codes:
        if c in LANG_UNKNOWN: continue
        n = _LANG_NAME.get(c, c)
        if n not in out: out.append(n)
    return " and ".join(out) or "unknown"

def _section_for(sections, path):
    best = None
    for sec in sections:
        for l in sec.get("Location", []):
            r = l["path"].rstrip("/") + "/"
            if path.startswith(r) and (best is None or len(r) > best[0]): best = (len(r), sec)
    return best[1] if best else None

def lang_scan(sections, movies, series, trashed):
    pref = pref_language()
    if not pref: return []
    out, seen = [], set()
    STATE["step"] = "Checking audio languages"
    for lst in movies.values():
        for a, it in lst:
            if (a["name"], it["id"]) in seen: continue
            seen.add((a["name"], it["id"]))
            f = it.get("movieFile") or {}
            p = f.get("path")
            if not p or p in trashed or not os.path.isfile(p): continue
            if ((it.get("originalLanguage") or {}).get("name") or "") != pref: continue
            codes = _audio(f.get("mediaInfo"))
            if not _wrong_audio(codes, pref): continue
            sec = _section_for(sections, p)
            title = "%s (%s)" % (it.get("title"), it.get("year", "?"))
            out.append(dict(id=iid("lang", p), chk="health", sev="amb", title=title, library=(sec or {}).get("title") or a["name"],
                            detail={"kind": "lang", "text": "The only audio is %s, but it's %s film." % (_lang_names(codes), "an " + pref if pref[0] in "AEIOU" else "a " + pref),
                                    "note": "Usually a dubbed copy. Labels are occasionally wrong, so play a minute if you're not sure. Replace moves it to the trash, blocks that release in %s (when it still has the download in its history) and asks it for another copy." % a["name"],
                                    "path": p, "size": f.get("size") or os.path.getsize(p), "audio": codes, "lang": pref, "links": _link_info(p)["links"],
                                    "files": [{"path": p, "size": f.get("size") or 0}],
                                    "ref": {"arr": a["name"], "kind": "movie", "id": it["id"]}, "section": (sec or {}).get("key"),
                                    "confidence": "check", "fixes": ["replace"]},
                            links=[arr_link(a, it, "movie")]))
    for lst in series.values():
        for a, sr in lst:
            if (a["name"], "s", sr["id"]) in seen: continue
            seen.add((a["name"], "s", sr["id"]))
            if ((sr.get("originalLanguage") or {}).get("name") or "") != pref: continue
            if not (sr.get("statistics") or {}).get("episodeFileCount"): continue
            try: files = arr(a, "/episodefile?seriesId=%d" % sr["id"])
            except Exception: continue
            bad = [f for f in files if f.get("path") and f["path"] not in trashed and _wrong_audio(_audio(f.get("mediaInfo")), pref) and os.path.isfile(f["path"])]
            if not bad: continue
            try: eps = arr(a, "/episode?seriesId=%d" % sr["id"])
            except Exception: eps = []
            by_file = {}
            for e in eps:
                if e.get("episodeFileId"): by_file.setdefault(e["episodeFileId"], []).append(e)
            seasons = {}
            for f in bad: seasons.setdefault(f.get("seasonNumber", 0), []).append(f)
            for sn, fl in sorted(seasons.items()):
                items, ids, langs = [], [], []
                for f in sorted(fl, key=lambda x: x.get("relativePath") or ""):
                    es = sorted(by_file.get(f["id"], []), key=lambda e: e.get("episodeNumber") or 0)
                    ids += [e["id"] for e in es]
                    codes = _audio(f.get("mediaInfo"))
                    for n in _lang_names(codes).split(" and "):
                        if n not in langs: langs.append(n)
                    items.append({"path": f["path"], "size": f.get("size") or 0, "se": "S%02d" % sn + "".join("E%02d" % (e.get("episodeNumber") or 0) for e in es), "audio": "/".join(codes)})
                sec = _section_for(sections, fl[0]["path"])
                n = len(items)
                out.append(dict(id=iid("lang", a["name"], sr["id"], sn), chk="health", sev="amb",
                                title="%s Season %d" % (sr.get("title"), sn) if n > 1 else "%s %s" % (sr.get("title"), items[0]["se"]),
                                library=(sec or {}).get("title") or a["name"],
                                detail={"kind": "lang", "text": ("%d episodes have only %s audio" % (n, " and ".join(langs)) if n > 1 else "The only audio is %s" % " and ".join(langs)) + ", but it's %s show." % ("an " + pref if pref[0] in "AEIOU" else "a " + pref),
                                        "note": "Usually a dubbed release. Labels are occasionally wrong, so play a minute of one if you're not sure. Replace moves %s to the trash, blocks %s in %s (when it still has the download in its history) and asks it for other copies." % ("them" if n > 1 else "it", "those releases" if n > 1 else "that release", a["name"]),
                                        "path": items[0]["path"], "size": sum(x["size"] for x in items), "lang": pref, "files": items, "count": n,
                                        "ref": {"arr": a["name"], "kind": "episodes", "series": sr["id"], "ids": ids}, "section": (sec or {}).get("key"),
                                        "confidence": "check", "fixes": ["replace"] if ids else []},
                                links=[arr_link(a, sr, "series")]))
    return out

def lang_fix(issue_id, row, d):
    ref = d["ref"]; stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    moved, tids, folders = [], [], set()
    for x in d.get("files") or []:
        src = x["path"]
        if not safe_media_path(src) or not os.path.isfile(src): continue
        for p, reason in [(src, "Wrong audio language (replaced)")] + [(y, "Leftover side file") for y in health_side_files(src)]:
            dst = os.path.join(TRASH_DIR, stamp + "-" + issue_id, os.path.relpath(os.path.realpath(p), MEDIA_ROOT))
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True); size = os.path.getsize(p); move_file(p, dst)
                with db() as c:
                    tids.append(c.execute("INSERT INTO trash (issue_id, title, src, dst, size, reason, moved_at) VALUES (?,?,?,?,?,?,?)",
                                          (issue_id, row["title"], p, dst, size, reason, now_iso())).lastrowid)
                if p == src: moved.append(p); folders.add(os.path.dirname(p))
            except Exception as e:
                if p == src: log("lang move failed %s: %r" % (p, e))
    if not moved: return {"ok": False, "error": "The files aren't where they were, so nothing was moved. Scan again."}
    for fo in folders: plex_refresh_folder(d.get("section"), fo)
    with db() as c:
        done_id = c.execute("INSERT INTO done (issue_id, title, library, kept, moved, trash_ids, links, at, status, note, chk, ref) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                            (issue_id, row["title"], row["library"], "", json.dumps(moved), json.dumps(tids), row["links"], now_iso(),
                             "checking", "Asking %s for a copy in %s…" % (ref["arr"], d.get("lang") or "your language"), "health",
                             json.dumps(dict(dict(ref, kind="movie") if ref["kind"] == "movie" else
                                        {"arr": ref["arr"], "kind": "episode", "id": ref["ids"][0], "series": ref["series"]} if len(ref.get("ids") or []) == 1 else
                                        {"arr": ref["arr"], "kind": "episodes", "series": ref["series"], "ids": ref.get("ids") or []}, lang=d.get("lang") or pref_language())))).lastrowid
        c.execute("DELETE FROM issues WHERE id=?", (issue_id,))
    event("health", "%s: moved %d file%s with the wrong audio language to the trash and asked %s for another copy." % (row["title"], len(moved), "" if len(moved) == 1 else "s", ref["arr"]))
    threading.Thread(target=_lang_search, args=(done_id, ref, moved), daemon=True).start()
    return {"ok": True}

def _lang_block(a, ref, moved):
    """Block the release(s) the wrong file came from: find the import in the arr's history, then mark its grab failed
    (that's how Radarr/Sonarr blocklist a release). Best effort: returns how many were blocked."""
    try:
        if ref["kind"] == "movie": hist = arr(a, "/history/movie?movieId=%d" % int(ref["id"]))
        else: hist = arr(a, "/history/series?seriesId=%d" % int(ref["series"]))
    except Exception as e:
        log("lang history failed: %r" % e); return 0
    hist = hist if isinstance(hist, list) else hist.get("records", [])
    names = {os.path.basename(p) for p in moved}
    dl = {h.get("downloadId") for h in hist if h.get("eventType") in ("downloadFolderImported", "episodeFileImported", "movieFileImported")
          and h.get("downloadId") and os.path.basename(((h.get("data") or {}).get("importedPath") or "").replace("\\", "/")) in names}
    n = 0
    for h in hist:
        if h.get("eventType") == "grabbed" and h.get("downloadId") in dl:
            try: arr(a, "/history/failed/%d" % h["id"], {}, "POST"); n += 1; dl.discard(h["downloadId"])
            except Exception as e: log("lang blocklist failed: %r" % e)
    return n

def _lang_search(done_id, ref, moved):
    status, note = "warn", ""
    try:
        a = arr_by_name(ref["arr"])
        blocked = _lang_block(a, ref, moved)
        if ref["kind"] == "movie":
            arr(a, "/command", {"name": "RescanMovie", "movieId": int(ref["id"])}, "POST")
        else:
            arr(a, "/command", {"name": "RescanSeries", "seriesId": int(ref["series"])}, "POST")
        time.sleep(20)
        if ref["kind"] == "movie": arr(a, "/command", {"name": "MoviesSearch", "movieIds": [int(ref["id"])]}, "POST")
        else: arr(a, "/command", {"name": "EpisodeSearch", "episodeIds": [int(i) for i in ref.get("ids") or []]}, "POST")
        status = "waiting"
        note = ("%s is searching for another copy%s. If it finds one, it downloads " + dl_when() + " and shows up in Plex by itself.") % (
            a["name"], " (the old release is blocked)" if blocked else "")
    except Exception as e:
        note = "Moved to the trash, but couldn't ask %s to search: %s" % (ref.get("arr"), e)
    with db() as c:
        c.execute("UPDATE done SET status=?, note=?, checked_at=? WHERE id=?", (status, note, now_iso(), done_id))
    log("lang search %s: %s" % (done_id, note))

# ---------------- space forecast (v0.1.6) ----------------
# One free-space reading per day (the latest of the day wins). After a week of readings, a straight line through "used"
# gives how fast the disk fills and roughly when it's full. Several disks/datasets under the media folder are added up.
SPACE_MIN_DAYS = 7

def space_now():
    seen, free, total = set(), 0, 0
    for p in [MEDIA_ROOT] + split_mounts():
        try: st = os.statvfs(p)
        except OSError: continue
        k = (st.f_fsid, st.f_blocks)
        if k in seen: continue
        seen.add(k); free += st.f_bavail * st.f_frsize; total += st.f_blocks * st.f_frsize
    return free, total

def space_record():
    free, total = space_now()
    if not total: return
    log_ = [x for x in (kv_get("space_log") or []) if isinstance(x, dict)]
    d = datetime.now().date().isoformat()
    log_ = [x for x in log_ if x.get("d") != d] + [{"d": d, "free": free, "total": total}]
    kv_set("space_log", log_[-400:])

def space_forecast():
    free, total = space_now()
    pts = [x for x in (kv_get("space_log") or []) if isinstance(x, dict) and x.get("total")]
    out = {"free": free, "total": total, "readings": len(pts), "need": SPACE_MIN_DAYS, "rate": None, "days_left": None}
    try: out["possible"] = stats()["possible"]
    except Exception: out["possible"] = 0
    if len(pts) < SPACE_MIN_DAYS: return out
    pts = pts[-90:]
    d0 = datetime.fromisoformat(pts[0]["d"])
    xs = [(datetime.fromisoformat(x["d"]) - d0).days for x in pts]
    ys = [x["total"] - x["free"] for x in pts]
    n = len(xs); mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den <= 0: return out
    rate = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den          # bytes per day (+ = filling up)
    out["rate"] = rate
    out["days"] = xs[-1]
    if rate > 0: out["days_left"] = int(free / rate)
    return out

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
        os.makedirs(os.path.dirname(dst), exist_ok=True); size = os.path.getsize(src); old_mode = os.stat(src).st_mode & 0o7777; move_file(src, dst)
        made_safe = False
        if not acl_protected(dst):                   # never executable in the trash (but a chmod would wipe a NAS permission list)
            try: os.chmod(dst, 0o640); made_safe = True
            except OSError: pass
        reason = {"infected": "Virus (ClamAV)", "program": "Program file", "archive": "Leftover archive", "fakevideo": "Fake video"}.get(d["kind"], "Virus scan")
        with db() as c:
            tid = c.execute("INSERT INTO trash (issue_id, title, src, dst, size, reason, moved_at) VALUES (?,?,?,?,?,?,?)",
                            (issue_id, row["title"], src, dst, size, reason, now_iso())).lastrowid
        if made_safe:                                # Put back gives the file its old permissions again
            tm = kv_get("trash_modes") or {}; tm[str(tid)] = old_mode; kv_set("trash_modes", tm)
        with db() as c:
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
            age = (datetime.now() - datetime.fromisoformat(r["at"])).total_seconds() / 86400
            if ref["kind"] == "episodes":                     # v0.1.6: a whole season replaced (audio language): wait for ALL of them
                want = {int(i) for i in ref.get("ids") or []}
                eps = [e for e in arr(a, "/episode?seriesId=%d" % int(ref["series"])) if e.get("id") in want]
                files = {x["id"]: x for x in arr(a, "/episodefile?seriesId=%d" % int(ref["series"]))}
                got = [files[e["episodeFileId"]] for e in eps if e.get("hasFile") and e.get("episodeFileId") in files
                       and files[e["episodeFileId"]].get("path") not in moved and os.path.exists(files[e["episodeFileId"]].get("path") or "")]
                n, k = len(want), len({e["id"] for e in eps if e.get("hasFile") and e.get("episodeFileId") in files and files[e["episodeFileId"]].get("path") not in moved})
                qs = sorted({((x.get("quality") or {}).get("quality") or {}).get("name", "") for x in got} - {""})
                wrong = [x for x in got if ref.get("lang") and _wrong_audio(_audio(x.get("mediaInfo")), ref["lang"])]
                if n and k >= n and wrong:
                    st, note = "warn", "All %d new copies arrived, but %d still %s %s audio only (%s). Try Interactive Search in %s and pick a release in %s." % (
                        n, len(wrong), "has" if len(wrong) == 1 else "have", _lang_names(_audio(wrong[0].get("mediaInfo"))),
                        ", ".join(os.path.basename(x.get("path") or "")[:60] for x in wrong[:3]) + (" …" if len(wrong) > 3 else ""), a["name"], ref["lang"])
                    event("health", "%s: %s" % (r["title"], note))
                elif n and k >= n:
                    st, note = "ok", "\u2713 All %d new copies arrived (%s%s)." % (n, ", ".join(qs) + ", " if qs else "", human(sum(x.get("size") or 0 for x in got)))
                    event("health", "%s: %s" % (r["title"], note))
                elif age > FOLLOW_DAYS:
                    st, note = "warn", "%d of %d new copies after %d days. %s can't find the rest; try Interactive Search on its page." % (k, n, FOLLOW_DAYS, a["name"])
                    event("health", "%s: %s" % (r["title"], note))
                else:
                    st, note = "waiting", "%d of %d new copies arrived so far; waiting for %s. Checked %s." % (k, n, a["name"], datetime.now().strftime("%a %H:%M"))
                with db() as c: c.execute("UPDATE done SET status=?, note=?, checked_at=? WHERE id=?", (st, note, now_iso(), r["id"]))
                continue
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
                elif ref.get("lang") and _wrong_audio(_audio(f.get("mediaInfo")), ref["lang"]):    # v0.1.6: dubbed again?
                    st, note = "warn", "A new copy arrived, but its audio is %s again: %s. Try Interactive Search in %s and pick a release in %s." % (
                        _lang_names(_audio(f.get("mediaInfo"))), os.path.basename(f["path"]), a["name"], ref["lang"])
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
                                          ("Waiting for %s to download a new copy%s. Checked %s." % (a["name"], " (in your download window)" if NIGHT_END_HOUR is not None else "", datetime.now().strftime("%a %H:%M")), now_iso(), r["id"]))
        except urllib.error.HTTPError as e:
            if e.code == 404:                                 # v0.1.6: the film/show was removed from Radarr/Sonarr: nothing to wait for
                with db() as c: c.execute("UPDATE done SET status='ok', note=?, checked_at=? WHERE id=?",
                                          ("Removed from %s, so there's no new copy to wait for." % (json.loads(r["ref"] or "{}").get("arr") or "Radarr/Sonarr"), now_iso(), r["id"]))
            else: log("followup %s failed: %r" % (r["id"], e))
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
NOTIFY_KINDS = ("discord", "telegram", "ntfy", "webhook", "email", "pushover")
NOTIFY_DEFAULT_URL = {"discord": "https://discord.com", "telegram": "https://api.telegram.org", "pushover": "https://api.pushover.net"}
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
        elif kind == "pushover":                              # v0.1.7
            if not e.get("token") or not e.get("user"): return {"ok": False, "error": "Fill in the app token and your user key."}
            body = urllib.parse.urlencode({"token": e["token"], "user": e["user"], "title": title[:250], "message": _plain(message)[:1000],
                                           "priority": 1 if priority >= 8 else 0}).encode()
            urllib.request.urlopen(urllib.request.Request((e.get("url") or NOTIFY_DEFAULT_URL["pushover"]).rstrip("/") + "/1/messages.json", data=body, method="POST"), timeout=20).read()
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

CHK_NAMES = {"missing": "missing file", "dupes": "duplicate", "meta": "metadata gap", "health": "broken file", "subs": "subtitle problem", "virus": "virus / fake file", "coming": "Coming Soon problem", "quality": "quality/space item", "unwatched": "unwatched list", "queue": "download problem", "apps": "app warning"}

def health_pct():
    """The main screen's Library Health figure, worked out the same way on the server (for the weekly digest)."""
    with db() as c:
        sc = c.execute("SELECT files FROM scans WHERE ok=1 ORDER BY id DESC LIMIT 1").fetchone()
        ign = hidden_ids(c)
        rows = [dict(r) for r in c.execute("SELECT id, chk, detail FROM issues")]
    files = (sc["files"] if sc else 0) or 0
    bad = set()
    for r in rows:
        if r["id"] in ign or r["chk"] in ("meta", "subs", "coming", "quality", "unwatched", "queue", "apps"): continue
        d = json.loads(r["detail"] or "{}")
        if d.get("kind") in ("gaps", "mismatch"): continue
        if d.get("versions"): bad |= {v.get("path") for v in d["versions"] if v.get("action") != "keep"}
        else: bad.add(d.get("path") or r["id"])
    return round(100.0 * (1 - len(bad) / files), 1) if files else None, len(rows) - len([r for r in rows if r["id"] in ign])

def score_line(days=1):
    """v0.1.10: the library score in one line for the messages: 'Score 97.4 (▲ 1.2 since yesterday) · 2 problems keep coming back'."""
    sc = library_score()
    if not sc: return ""
    cut = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    with db() as c: old = c.execute("SELECT overall FROM scores WHERE at <= ? ORDER BY at DESC LIMIT 1", (cut,)).fetchone()
    cur = sc["overall"]
    txt = "**Score:** %s out of 100" % ("100" if cur >= 99.95 else "%.1f" % cur)
    if old is not None:
        d = cur - old["overall"]
        txt += " (%s %s)" % ("no change" if abs(d) < 0.05 else ("▲ +%.1f" % d if d > 0 else "▼ %.1f" % d), "since yesterday" if days == 1 else "this week")
    low = [a for a in sc["areas"] if a["score"] < 95]
    if low: txt += " · lowest: " + ", ".join("%s %.0f" % (a["label"], a["score"]) for a in sorted(low, key=lambda a: a["score"])[:2])
    try:
        rc = recurring()["total"]
        if rc: txt += " · %d problem%s keep%s coming back" % (rc, "" if rc == 1 else "s", "s" if rc == 1 else "")
    except Exception: pass
    return txt

def weekly_digest():
    """v0.1.7: one quiet message a week (after Sunday's scan) when Settings → Notifications says Weekly or Both."""
    if (S.get("digest") or "news") not in ("weekly", "both") or not notify_channels(): return
    now = datetime.now()
    if now.weekday() != 6: return
    wk = now.strftime("%G-W%V")
    if kv_get("digest_week") == wk: return
    pct, n_open = health_pct()
    since = (now - timedelta(days=7)).isoformat(timespec="seconds")
    with db() as c:
        ign = hidden_ids(c)
        new = [r["chk"] for r in c.execute("SELECT id, chk FROM issues WHERE first_seen > ?", (since,)) if r["id"] not in ign]
        fixed = c.execute("SELECT COUNT(*) FROM done WHERE status='ok' AND at > ?", (since,)).fetchone()[0]
    lines = [score_line(days=7) or "**Library health:** %s" % ("%.1f%%" % pct if pct is not None else "no scan yet"),
             "**To review:** %d open item%s, %d new this week" % (n_open, "" if n_open == 1 else "s", len(new)), "**Fixed this week:** %d" % fixed]
    try:
        sf = space_forecast()
        if sf.get("total"):
            lines.append("**Disk:** %s free of %s" % (human(sf["free"]), human(sf["total"])) + (", full in about %d days" % sf["days_left"] if sf.get("days_left") is not None else ""))
    except Exception: pass
    if new:
        by = {}
        for k in new: by[k] = by.get(k, 0) + 1
        lines.append("New: " + ", ".join("%d %s%s" % (n, CHK_NAMES.get(k, k), "" if n == 1 else "s") for k, n in by.items()))
    notify("Library Doctor: your week", "\n".join(lines), 3)
    kv_set("digest_week", wk)

def nightly_summary():
    if (S.get("digest") or "news") == "weekly":              # v0.1.7: weekly only, except things that can't wait
        with db() as c: sc = c.execute("SELECT ok FROM scans ORDER BY id DESC LIMIT 1").fetchone()
        urgent = sc is not None and not sc["ok"]
        with db() as c:
            urgent = urgent or c.execute("SELECT COUNT(*) FROM issues WHERE chk='virus' AND first_seen > ?", ((datetime.now() - timedelta(days=1)).isoformat(timespec="seconds"),)).fetchone()[0] > 0
        if not urgent: return
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
    try:                                                     # v0.1.5: Radarr/Sonarr/qbit_manage recycle bins too
        soon += [{"size": x["size"], "moved_at": x["moved_at"]} for x in recycle_items() if datetime.fromisoformat(x["empties"]) < datetime.now() + timedelta(days=1)]
    except Exception as e: log("recycle soon check failed: %r" % e)
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
    try:
        sf = space_forecast()
        if sf.get("days_left") is not None and sf["days_left"] < 60:
            lines.append("**Disk space:** %s free, filling by about %s a day, so it's full in about %d days." % (human(sf["free"]), human(sf["rate"]), sf["days_left"]))
            prio = max(prio, 5 if sf["days_left"] < 14 else prio)
    except Exception as e: log("space summary failed: %r" % e)
    try:
        hb = kv_get("trash_held") or {}
        if hb: lines.append("**Kept back in the trash:** %d file%s after a closer look (%s). Check them in the Trash screen." % (len(hb), "" if len(hb) == 1 else "s", list(hb.values())[0]))
    except Exception: pass
    if soon: lines.append("**Trash:** %d item%s (%s) delete%s for good in the next 24 hours (Library Doctor's trash and the recycle bins). Review them in the Trash screen." % (len(soon), "" if len(soon) == 1 else "s", human(sum(t["size"] or 0 for t in soon)), "s" if len(soon) == 1 else ""))
    if not lines:
        log("nightly summary: nothing new, no message sent"); kv_set("gotify_last_summary", now_iso()); return {"ok": True, "sent": False}
    try:                                                     # v0.1.10: the score rides along (never a reason to send by itself)
        sl = score_line(days=1)
        if sl: lines.append(sl)
    except Exception as e: log("score line failed: %r" % e)
    lines.append("Open now: %d to look at \u00b7 trash %d item%s (%s)" % (len(allopen), len(trash), "" if len(trash) == 1 else "s", human(sum(t["size"] or 0 for t in trash))))
    title = "Library Doctor: " + ("%d new to look at" % len(new) if new else "%d replaced file%s arrived" % (len(arrived), "" if len(arrived) == 1 else "s") if arrived else "nightly check")
    r = notify(title, "\n\n".join(lines), prio)
    if r.get("ok"): kv_set("gotify_last_summary", now_iso()); log("nightly summary sent: %s" % title)
    return dict(r, sent=r.get("ok"))

# ---------------- checks on import (v0.1.7) ----------------
# Every 10 minutes: what did Radarr/Sonarr import since last time? A new file that's clearly broken (tiny, or far shorter
# than its film/episode), has only the wrong audio language, or is a program pretending to be a video gets a message
# straight away and a scan is started, so the card is there in a minute instead of after the 03:00 scan.
IMPORT_EVERY_MIN = 10
_FAKE_EXT = (".exe", ".scr", ".bat", ".cmd", ".com", ".lnk", ".msi", ".vbs", ".js", ".jar", ".ps1", ".zip", ".rar", ".7z", ".iso")

def import_check():
    last = kv_get("import_check_at")
    now_utc = datetime.now(timezone.utc)
    kv_set("import_check_at", now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"))
    if not last: return []                                     # first run: just remember where we are
    pref = pref_language(); found = []
    for a in S.get("arrs", []):
        try: hist = arr(a, "/history/since?" + urllib.parse.urlencode({"date": last, "eventType": "downloadFolderImported"}))
        except Exception as e: log("import check %s failed: %r" % (a["name"], e)); continue
        for h in hist if isinstance(hist, list) else []:
            try:
                if a["kind"] == "radarr":
                    item = arr(a, "/movie/%d" % int(h["movieId"])); f = item.get("movieFile") or {}
                    title = "%s (%s)" % (item.get("title"), item.get("year", "?")); runtime = item.get("runtime") or 0
                    orig = (item.get("originalLanguage") or {}).get("name")
                else:
                    e = arr(a, "/episode/%d" % int(h["episodeId"]))
                    f = arr(a, "/episodefile/%d" % int(e["episodeFileId"])) if e.get("episodeFileId") else {}
                    sr = arr(a, "/series/%d" % int(e["seriesId"]))
                    title = "%s S%02dE%02d" % (sr.get("title"), e.get("seasonNumber") or 0, e.get("episodeNumber") or 0)
                    runtime = e.get("runtime") or sr.get("runtime") or 0; orig = (sr.get("originalLanguage") or {}).get("name")
                p = f.get("path") or ""
                if not p: continue
                mi = f.get("mediaInfo") or {}
                why = None
                if os.path.splitext(p)[1].lower() in _FAKE_EXT: why = "it's a %s file, not a video" % os.path.splitext(p)[1]
                elif (f.get("size") or 0) and f["size"] < BROKEN_BYTES: why = "it's only %s, so it's broken" % human(f["size"])
                else:
                    rt = mi.get("runTime") or ""
                    try:
                        hh, mm, ss = (rt.split(":") + ["0", "0"])[:3]; mins_ = int(hh) * 60 + int(mm) + float(ss) / 60
                    except ValueError: mins_ = 0
                    if runtime >= 15 and mins_ and mins_ < runtime * 0.5: why = "it's only %d minutes long, but should be about %d" % (mins_, runtime)
                    elif pref and orig == pref and _wrong_audio(_audio(mi), pref): why = "its only audio is %s" % _lang_names(_audio(mi))
                if why: found.append((a["name"], title, why))
            except Exception as e:
                log("import check item failed: %r" % e)
    if found:
        notify("Library Doctor: %d just-imported file%s to check" % (len(found), "" if len(found) == 1 else "s"),
               "\n".join("**%s** (%s): %s" % (t, n, w) for n, t, w in found[:8]), 6)
        event("import", "Just imported and looks wrong: " + "; ".join("%s (%s)" % (t, w) for _, t, w in found[:5]))
        if not STATE["running"]: threading.Thread(target=scan, daemon=True).start()
    return found

def nightly():
    last, last_follow, last_tdarr, last_queue = None, None, None, None
    last_import = None
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
            try: weekly_digest()
            except Exception as e: log("weekly digest failed: %r" % e)
            try: nightly_summary()
            except Exception as e: log("nightly summary failed: %r" % e)
        elif now.minute % IMPORT_EVERY_MIN == 3 and last_import != (now.hour, now.minute):   # v0.1.7: checks on import
            last_import = (now.hour, now.minute)
            try: import_check()
            except Exception as e: log("import check failed: %r" % e)
        elif now.minute == 17 and last_follow != now.hour:     # hourly: has a replaced file arrived?
            last_follow = now.hour
            try: followup()
            except Exception as e: log("followup failed: %r" % e)
            try: trash_review()                                # v0.1.8: closer look at what leaves the trash soon
            except Exception as e: log("trash review failed: %r" % e)
        elif now.minute == 37 and last_queue != now.hour:      # v33: Downloads cards hourly; 07:37 (night window over) tells Gotify
            last_queue = now.hour
            try: score_snapshot(min_hours=20)                         # v0.1.9: at least one score point a day, fixes included
            except Exception as e: log("score snapshot failed: %r" % e)
            try: space_record()                                       # v0.1.6: one free-space reading per day (latest wins)
            except Exception as e: log("space record failed: %r" % e)
            try: queue_refresh(notify=(now.hour == QUEUE_NOTIFY_HOUR))
            except Exception as e: log("queue refresh failed: %r" % e)
            try:
                cards, _ = apps_refresh()
                if now.hour == QUEUE_NOTIFY_HOUR and notify_channels():      # once a day: warnings not told about before
                    with db() as c: hid = hidden_ids(c)
                    told = set(json.loads(kv_get("apps_notified") or "[]"))
                    cur = [x for x in cards if x["id"] not in hid and x["detail"]["kind"] != "update"]
                    bad = [x for x in cur if x["id"] not in told]
                    if bad:
                        notify("Library Doctor: %d app warning%s" % (len(bad), "" if len(bad) == 1 else "s"), "\n\n".join("**%s**: %s" % (x["library"], x["detail"]["text"][:200]) for x in bad[:6]), 5)
                    kv_set("apps_notified", json.dumps([x["id"] for x in cur]))
            except Exception as e: log("app health refresh failed: %r" % e)
        elif now.minute % 10 == 7 and last_tdarr != (now.hour, now.minute):   # v23: every 10 min while Tdarr has files of ours
            last_tdarr = (now.hour, now.minute)
            try:
                if tdarr_pending(): tdarr_followup()
            except Exception as e: log("tdarr followup failed: %r" % e)
        time.sleep(60)

def _probe(p):
    """(duration seconds, video height) via ffprobe; (0.0, 0) if it can't be read. Used by Convert to MKV."""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=height:format=duration", "-of", "json", p],
                           capture_output=True, text=True, timeout=60)
        j = json.loads(r.stdout or "{}")
        return float((j.get("format") or {}).get("duration") or 0), int(((j.get("streams") or [{}])[0]).get("height") or 0)
    except Exception: return 0.0, 0

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
                                detail={"kind": "hog", "path": f0, "size": r["size"], "extra": int(extra), "ref": ref, "section": sec["key"], "links": _link_info(f0)["links"],
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
    if how == "blockgroup": return group_block(issue_id, row, d)
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
    why = arr_cannot_grab(a)
    if why: return {"ok": False, "error": cant_grab_msg(a, why, moved=True).replace("a new copy", "a smaller copy")}
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
    note = ("%s is downloading %s (%s, saves %s). It arrives " + dl_when() + "; the old copy stays until the new one is in.") % (
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
                    note = ("Waiting for %s to start the download" + (" (download window)" if NIGHT_END_HOUR is not None else "") + ". Checked %s.") % (a["name"], datetime.now().strftime("%a %H:%M"))
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

# ---------------- App health (v0.1.5) ----------------
# Every app keeps its own list of warnings (Radarr/Sonarr/Prowlarr Health page, Bazarr health, Tdarr nodes) that you only
# see when you open it. One card per app + warning source; "update available" is a low-key card; an app that doesn't
# answer is a problem in itself. Refreshed with every scan and hourly with the Downloads cards.
def _health_card(app, source, typ, msg, wiki, public, page, upd_info=None):
    upd = source == "UpdateCheck"
    sev = "bad" if typ == "error" or source == "unreachable" else "att"
    title = "%s: %s" % (app, "update available" if upd else "not answering" if source == "unreachable" else msg[:90] + ("…" if len(msg) > 90 else ""))
    note = ("Update it when convenient; nothing is broken." if upd else
            "%s can't do its job until this is fixed. Open it to see the details." % app if sev == "bad" else
            "This is %s's own warning, shown here so you don't have to open every app." % app)
    links = [{"label": "Open in %s" % app, "url": public.rstrip("/") + page}] if public else []
    if wiki: links.append({"label": "What does this mean?", "url": wiki})
    detail = {"kind": "update" if upd else "health", "source": source, "text": msg, "note": note, "fixes": []}
    if upd and upd_info:
        detail.update(upd_info)
        detail["fixes"] = (["selfupdate"] if upd_info.get("self") else ["howto"]) + ["skipver"]
        if upd_info.get("test"):
            title = "%s: test version %s available" % (app, upd_info.get("new") or "")
            detail["note"] = ("%s is set to its %s branch, so it offers test (beta) versions. You're on %s. "
                              "To only be offered finished versions, set Settings → General → Updates → Branch to master in %s, "
                              "or press Skip this version." % (app, upd_info.get("branch"), upd_info.get("cur") or "?", app))
        elif not upd_info.get("self"):
            detail["note"] = ("%s runs in Docker, so it can't update itself: pull the new image (How to update shows the commands). "
                              "Nothing is broken meanwhile." % app)
        else:
            detail["note"] = "%s can update itself: Update now downloads it and restarts %s (about a minute)." % (app, app)
    return dict(id=iid("apps", app, source), chk="apps", sev=sev, title=title, library=app, detail=detail, links=links)

# v0.1.6: what an "update available" card needs: current/new version, branch, and whether the app can update itself
# (only when it isn't in Docker and its update mechanism is the built-in one or a script).
_UPD_RE = re.compile(r"v?(\d+(?:\.\d+){1,3})")
def _update_info(kind, name, url, key, ver, msg):
    info = {"ref": {"kind": kind, "name": name}}
    try:
        st = http(url.rstrip("/") + "/api/%s/system/status" % ver, {"X-Api-Key": key}, timeout=20)
        info["cur"] = st.get("version"); info["branch"] = st.get("branch") or ""
        mech = (st.get("packageUpdateMechanism") or "").lower()
        info["docker"] = bool(st.get("isDocker")) or mech == "docker"
        info["self"] = not info["docker"] and mech in ("builtin", "script")
    except Exception:
        info.update(docker=True, self=False)
    try:
        ups = http(url.rstrip("/") + "/api/%s/update" % ver, {"X-Api-Key": key}, timeout=20)
        top = next((u for u in ups if u.get("latest")), ups[0] if ups else None) if isinstance(ups, list) else None
        if top: info["new"] = top.get("version"); info["branch"] = top.get("branch") or info.get("branch", "")
    except Exception:
        pass
    if not info.get("new"):
        m = _UPD_RE.search(msg or ""); info["new"] = m.group(1) if m else ""
    info["test"] = (info.get("branch") or "").lower() not in ("", "master", "main", "stable")
    svc = re.sub(r"[^a-z0-9_-]", "", name.lower()) or "app"
    info["howto"] = ["docker compose pull %s" % svc, "docker compose up -d %s" % svc]
    return info

def _skipped(app, new):
    return bool(new) and (kv_get("apps_skip") or {}).get(app) == new

def apps_cards():
    out = []
    def arr_like(name, url, key, public, ver, page="/system/status", kind="arr"):
        try:
            items = http(url.rstrip("/") + "/api/%s/health" % ver, {"X-Api-Key": key}, timeout=20)
        except Exception as e:
            out.append(_health_card(name, "unreachable", "error", "Library Doctor couldn't reach %s (%s)." % (name, getattr(e, "reason", e)), "", public, page)); return
        if kind == "arr":
            a_cfg = next((x for x in S.get("arrs", []) if x["name"] == name), None)
            if a_cfg:
                try: out.extend(indexer_cards(a_cfg, items))
                except Exception as e: log("indexer advice %s failed: %r" % (name, e))
        for h in items if isinstance(items, list) else []:
            if (h.get("type") or "").lower() in ("ok", "notice"): continue
            src, info = h.get("source") or "health", None
            if src == "UpdateCheck":
                info = _update_info(kind, name, url, key, ver, h.get("message") or "")
                if _skipped(name, info.get("new")): continue
            out.append(_health_card(name, src, (h.get("type") or "warning").lower(), h.get("message") or "", h.get("wikiUrl") or "", public, page, info))
    for a in S.get("arrs", []):
        arr_like(a["name"], a["url"], a.get("key", ""), a.get("public") or a["url"], "v3")
    pr = S.get("prowlarr") or {}
    if pr.get("url"): arr_like(pr.get("name") or "Prowlarr", pr["url"], pr.get("key", ""), pr.get("public") or pr["url"], "v1", kind="prowlarr")
    for b in S.get("bazarr", []) or []:
        try:
            r = http(b["url"].rstrip("/") + "/api/system/health", {"X-API-KEY": b.get("key", "")}, timeout=20)
            for h in (r.get("data") or []) if isinstance(r, dict) else []:
                msg = "%s: %s" % (h.get("object", ""), h.get("issue", "")) if h.get("object") else (h.get("issue") or str(h))
                out.append(_health_card(b.get("name") or "Bazarr", "bazarr:" + str(h.get("object", ""))[:40], "warning", msg, "", b.get("public") or b["url"], "/system/status"))
        except Exception as e:
            out.append(_health_card(b.get("name") or "Bazarr", "unreachable", "error", "Library Doctor couldn't reach %s (%s)." % (b.get("name") or "Bazarr", getattr(e, "reason", e)), "", b.get("public") or b["url"], "/system/status"))
    t = S.get("tdarr") or {}
    if t.get("url"):
        try:
            nodes = tdarr_call_get("/api/v2/get-nodes")
            if not nodes:
                out.append(_health_card("Tdarr", "nodes", "error", "No Tdarr node is connected, so nothing gets converted.", "", t.get("public") or t["url"], "/"))
        except Exception as e:
            out.append(_health_card("Tdarr", "unreachable", "error", "Library Doctor couldn't reach Tdarr (%s)." % getattr(e, "reason", e), "", t.get("public") or t["url"], "/"))
    return out

def tdarr_call_get(path):
    t = S.get("tdarr") or {}
    req = urllib.request.Request(t["url"].rstrip("/") + path, headers={"x-api-key": t.get("key", "")})
    with urllib.request.urlopen(req, timeout=20) as r:
        b = r.read().decode() or "{}"
    return json.loads(b) if b.strip()[:1] in ("{", "[") else {}

def apps_refresh():
    cards = apps_cards(); now = now_iso()
    with db() as c:
        seen = {r["id"]: r["first_seen"] for r in c.execute("SELECT id, first_seen FROM issues WHERE chk='apps'")}
        sid = (c.execute("SELECT MAX(id) FROM scans").fetchone()[0] or 0)
        c.execute("DELETE FROM issues WHERE chk='apps'")
        for it in cards:
            c.execute("INSERT OR REPLACE INTO issues VALUES (?,?,?,?,?,?,?,?,?,?)", (it["id"], sid, "apps", it["sev"], it["title"], it["library"],
                      json.dumps(it["detail"]), json.dumps(it["links"]), seen.get(it["id"], now), now))
    return cards, [it for it in cards if it["id"] not in seen]

# v0.1.6: indexers that keep failing and never find anything. Radarr/Sonarr say "Indexers unavailable due to failures: A, B"
# on their Health page; every hourly check remembers who was named (kv idx_fail), and the arr's own grab history says
# whether an indexer ever found anything you downloaded. Failing again and again + nothing grabbed in 90 days = a card
# offering to turn it off in that app (it stays configured, just switched off; turn it back on there any time).
IDX_DAYS, IDX_MIN_FAILS = 90, 3
_IDX_RE = re.compile(r"due to failures(?: for more than \d+ hours)?:\s*(.+)$", re.I)

def _failing_names(items):
    out = set()
    for h in items if isinstance(items, list) else []:
        if (h.get("source") or "") not in ("IndexerStatusCheck", "IndexerLongTermStatusCheck"): continue
        m = _IDX_RE.search(h.get("message") or "")
        if m: out |= {x.strip() for x in m.group(1).split(", ") if x.strip()}
    return out

def indexer_cards(a, health_items):
    now = datetime.now(); log_ = kv_get("idx_fail") or {}
    hour = now.strftime("%Y-%m-%dT%H")
    week = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H")
    cur = _failing_names(health_items)
    for n in cur:
        k = "%s|%s" % (a["name"], n); hs = [x for x in log_.get(k, []) if x >= week]
        if hour not in hs: hs.append(hour)
        log_[k] = hs
    for k in list(log_):
        log_[k] = [x for x in log_[k] if x >= week]
        if not log_[k]: del log_[k]
    kv_set("idx_fail", log_)
    fails = {k.split("|", 1)[1]: len(v) for k, v in log_.items() if k.split("|", 1)[0] == a["name"]}
    cand = {n: c for n, c in fails.items() if c >= IDX_MIN_FAILS}
    if not cand: return []
    try:
        idx = {i["name"]: i for i in arr(a, "/indexer")}
        since = (datetime.now(timezone.utc) - timedelta(days=IDX_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        grabs = arr(a, "/history/since?" + urllib.parse.urlencode({"date": since, "eventType": "grabbed"}))
    except Exception as e:
        log("indexer check %s failed: %r" % (a["name"], e)); return []
    used = {}
    for g in grabs if isinstance(grabs, list) else []:
        n = (g.get("data") or {}).get("indexer") or ""
        used[n] = used.get(n, 0) + 1
    out = []
    for n, c in sorted(cand.items()):
        i = idx.get(n)
        if not i or not (i.get("enableRss") or i.get("enableAutomaticSearch") or i.get("enableInteractiveSearch")): continue
        if used.get(n): continue
        out.append(dict(id=iid("apps", a["name"], "indexer", n), chk="apps", sev="att", library=a["name"],
                        title="%s: %s keeps failing and never finds anything" % (a["name"], n),
                        detail={"kind": "indexer", "source": "indexer", "indexer": n, "indexer_id": i["id"], "fails": c,
                                "text": "%s named it as failing in %d hourly check%s over the last 7 days, and nothing you downloaded in the last %d days came from it." % (
                                    a["name"], c, "" if c == 1 else "s", IDX_DAYS),
                                "note": "Turning it off stops the warnings and the wasted searches. It stays set up in %s, so you can switch it back on there any time." % a["name"],
                                "ref": {"kind": "arr", "name": a["name"]}, "fixes": ["idxoff"]},
                        links=[{"label": "Open indexers in %s" % a["name"], "url": (a.get("public") or a["url"]).rstrip("/") + "/settings/indexers"}]))
    return out

def apps_fix(issue_id, how):
    with db() as c:
        row = c.execute("SELECT * FROM issues WHERE id=? AND chk='apps'", (issue_id,)).fetchone()
    if not row: return {"ok": False, "error": "That card has gone; press Scan now to refresh."}
    d = json.loads(row["detail"]); ref = d.get("ref") or {}; app = ref.get("name") or row["library"]
    if how == "skipver":
        sk = kv_get("apps_skip") or {}; sk[app] = d.get("new") or ""; kv_set("apps_skip", sk)
        with db() as c: c.execute("DELETE FROM issues WHERE id=?", (issue_id,))
        event("apps", "Skipping %s %s; Library Doctor tells you again when a newer version comes out." % (app, d.get("new") or ""))
        return {"ok": True, "text": "Skipped %s %s. You'll see a card again when a newer version comes out." % (app, d.get("new") or "")}
    if how == "idxoff":
        a = next((x for x in S.get("arrs", []) if x["name"] == app), None)
        if not a: return {"ok": False, "error": "%s isn't in Settings any more." % app}
        try:
            i = arr(a, "/indexer/%d" % int(d["indexer_id"]))
            i.update(enableRss=False, enableAutomaticSearch=False, enableInteractiveSearch=False)
            arr(a, "/indexer/%d?forceSave=true" % int(d["indexer_id"]), i, "PUT")
        except Exception as e:
            return {"ok": False, "error": "Couldn't change it in %s (%s)." % (app, getattr(e, "reason", e))}
        with db() as c: c.execute("DELETE FROM issues WHERE id=?", (issue_id,))
        event("apps", "Turned off %s in %s (it kept failing and never found anything)." % (d.get("indexer"), app))
        return {"ok": True, "text": "%s is switched off in %s. It's still set up there if you ever want it back." % (d.get("indexer"), app)}
    if how == "selfupdate":
        if not d.get("self"): return {"ok": False, "error": "%s runs in Docker, so it can't update itself. Use How to update." % app}
        if ref.get("kind") == "prowlarr":
            cfg, ver = S.get("prowlarr") or {}, "v1"
        else:
            cfg, ver = next((a for a in S.get("arrs", []) if a["name"] == app), {}), "v3"
        if not cfg.get("url"): return {"ok": False, "error": "%s isn't in Settings any more." % app}
        try:
            http(cfg["url"].rstrip("/") + "/api/%s/command" % ver, {"X-Api-Key": cfg.get("key", "")}, data={"name": "ApplicationUpdate"}, method="POST", timeout=30)
        except urllib.error.HTTPError as e:
            return {"ok": False, "error": "%s said %d %s." % (app, e.code, e.reason)}
        except Exception as e:
            return {"ok": False, "error": "Couldn't reach %s (%s)." % (app, getattr(e, "reason", e))}
        event("apps", "Asked %s to update itself to %s." % (app, d.get("new") or "the new version"))
        return {"ok": True, "text": "%s is downloading %s and restarts by itself in a minute or two. The card goes on the next check." % (app, d.get("new") or "the update")}
    return {"ok": False, "error": "Unknown fix."}

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

def _qbit(q=None):
    q = q if q is not None else (S.get("qbit") or {})
    if not q.get("url"): return None
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_cookiejar.CookieJar()))
    r = op.open(q["url"].rstrip("/") + "/api/v2/auth/login", data=urllib.parse.urlencode({"username": q.get("username", ""), "password": q.get("password", "")}).encode(), timeout=15).read()
    if r.strip() not in (b"Ok.", b""): raise RuntimeError("qBittorrent refused the username or password")   # 4.x says "Ok.", 5.x answers 204 with nothing
    return op

# v0.1.7: why is a download stuck? qBittorrent knows (seeds in the swarm, what the tracker said, speed); SABnzbd knows its
# own status and why a download failed. One plain sentence, and whether a re-announce might help.
_UNREG = re.compile(r"unregistered|not registered|not found|torrent not exist|unknown torrent|info ?hash not found|deleted|nuked|trumped", re.I)

def _speed(b): return "%s/s" % human(b)

# v0.1.7: Transmission (RPC with its session-id handshake) and NZBGet (JSON-RPC), next to qBittorrent and SABnzbd
_TR_SID = {"v": ""}
def _tr_rpc(method, args=None, cfg=None):
    t = cfg if cfg is not None else (S.get("transmission") or {})
    url = t["url"].rstrip("/"); url = url if url.endswith("/rpc") else url + "/transmission/rpc"
    h = {"Content-Type": "application/json"}
    if t.get("username") or t.get("password"):
        import base64
        h["Authorization"] = "Basic " + base64.b64encode(("%s:%s" % (t.get("username", ""), t.get("password", ""))).encode()).decode()
    body = json.dumps({"method": method, "arguments": args or {}}).encode()
    for _ in range(2):
        h["X-Transmission-Session-Id"] = _TR_SID["v"]
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=h, method="POST"), timeout=20) as r:
                d = json.loads(r.read() or b"{}")
                if d.get("result") != "success": raise RuntimeError("Transmission said: %s" % d.get("result"))
                return d.get("arguments") or {}
        except urllib.error.HTTPError as e:
            if e.code == 409: _TR_SID["v"] = e.headers.get("X-Transmission-Session-Id", ""); continue
            raise
    raise RuntimeError("Transmission didn't accept the session")

def _nzbget(method, params=None, cfg=None):
    n = cfg if cfg is not None else (S.get("nzbget") or {})
    import base64
    h = {"Content-Type": "application/json", "Authorization": "Basic " + base64.b64encode(("%s:%s" % (n.get("username", ""), n.get("password", ""))).encode()).decode()}
    r = http(n["url"].rstrip("/") + "/jsonrpc", h, data={"method": method, "params": params or [], "id": 1}, method="POST", timeout=20)
    if r.get("error"): raise RuntimeError("NZBGet said: %s" % (r["error"].get("message") if isinstance(r["error"], dict) else r["error"]))
    return r.get("result")

_NZBGET_WHY = {"HEALTH": "too many parts of it are missing on your usenet server", "PAR": "repairing it failed", "UNPACK": "unpacking it failed",
               "MOVE": "moving the finished files failed", "SCAN": "the post-processing scan failed", "BAD": "the download was bad",
               "PASSWORD": "it's password-protected", "SPACE": "the disk was full", "DISKSPACE": "the disk was full", "FETCH": "the NZB couldn't be fetched"}

def _tr_reason(h):
    tr = _tr_rpc("torrent-get", {"ids": [h.lower()], "fields": ["status", "error", "errorString", "percentDone", "rateDownload", "eta", "peersSendingToUs", "trackerStats", "metadataPercentComplete"]}).get("torrents") or []
    if not tr: return None
    t = tr[0]; stats = t.get("trackerStats") or []
    msgs = [str(x.get("lastAnnounceResult") or "") for x in stats if x.get("lastAnnounceResult") and not x.get("lastAnnounceSucceeded")]
    if t.get("errorString") and _UNREG.search(t["errorString"]) or any(_UNREG.search(m) for m in msgs):
        return ("The tracker says it no longer has this torrent (%s), so it will never finish." % ((t.get("errorString") or next(m for m in msgs if _UNREG.search(m)))[:80]), False)
    if t.get("status") == 0: return ("It's paused in Transmission.", False)
    if t.get("error") in (2, 3) and t.get("errorString"): return ("Transmission reports an error: %s." % t["errorString"][:100], True)
    if (t.get("metadataPercentComplete") or 1) < 1: return ("Transmission is still waiting for the torrent's details: nobody sharing it has been found yet.", True)
    seeds = max([x.get("seederCount") or 0 for x in stats] + [0])
    if seeds <= 0 and not t.get("peersSendingToUs") and (t.get("percentDone") or 0) < 1:
        return ("Nobody is sharing it: 0 seeds in the swarm%s." % (" (%d%% done so far)" % round(100 * t["percentDone"]) if t.get("percentDone") else ""), True)
    sp, eta = t.get("rateDownload") or 0, t.get("eta") or -1
    if sp and eta > 0: return ("It's downloading slowly: %s, about %s left (%d seeds)." % (_speed(sp), mins(eta * 1000) if eta < 86400 else "%d days" % round(eta / 86400), seeds), False)
    return ("%d seed%s, but nothing is coming in right now." % (seeds, "" if seeds == 1 else "s"), True)

def _nzbget_reason(nid):
    try: nid = int(nid)
    except (TypeError, ValueError): return None
    for g in _nzbget("listgroups", [0]) or []:
        if g.get("NZBID") == nid:
            left = g.get("RemainingSizeMB") or 0
            return ("NZBGet says: %s%s." % (str(g.get("Status", "?")).replace("_", " ").lower(), ", %s left" % human(left * 1048576) if left else ""), False)
    for x in _nzbget("history", [False]) or []:
        if x.get("NZBID") == nid:
            st = str(x.get("Status") or "")
            if st.startswith("FAILURE") or st.startswith("DELETED"):
                why = _NZBGET_WHY.get(st.split("/")[-1], st.split("/")[-1].lower())
                return ("NZBGet couldn't finish it: %s." % why, False)
            return ("NZBGet says: %s." % st.lower(), False)
    return None

def dl_reason(it, qop=None):
    """(sentence, can_reannounce) for one stuck download, or ("", False) when the client can't tell more."""
    try:
        if it.get("protocol") == "torrent" and it.get("downloadId") and not qop and (S.get("transmission") or {}).get("url"):
            r = _tr_reason(it["downloadId"])
            return r if r else ("Transmission doesn't have this torrent any more.", False)
        if it.get("protocol") == "usenet" and it.get("downloadId") and not (S.get("sabnzbd") or {}).get("url") and (S.get("nzbget") or {}).get("url"):
            return _nzbget_reason(it["downloadId"]) or ("", False)
        if it.get("protocol") == "torrent" and it.get("downloadId") and qop:
            h = it["downloadId"].lower(); base = S["qbit"]["url"].rstrip("/")
            info = (json.loads(qop.open(base + "/api/v2/torrents/info?hashes=" + h, timeout=20).read() or b"[]") or [None])[0]
            if not info:
                if (S.get("transmission") or {}).get("url"):           # maybe it's in the other torrent client
                    r = _tr_reason(it["downloadId"])
                    if r: return r
                return ("qBittorrent doesn't have this torrent any more.", False)
            trk = json.loads(qop.open(base + "/api/v2/torrents/trackers?hash=" + h, timeout=20).read() or b"[]")
            real = [t for t in trk if not str(t.get("url", "")).startswith("** [")]
            msgs = [str(t.get("msg") or "") for t in real if t.get("msg")]
            st = info.get("state") or ""
            if any(_UNREG.search(m) for m in msgs): return ("The tracker says it no longer has this torrent (%s), so it will never finish." % next(m for m in msgs if _UNREG.search(m))[:80], False)
            if st in ("pausedDL", "stoppedDL"): return ("It's paused in qBittorrent.", False)
            if real and all(t.get("status") == 4 for t in real):
                return ("Every tracker is failing%s, so qBittorrent can't find anyone to download from." % (": " + msgs[0][:80] if msgs else ""), True)
            if st == "metaDL": return ("qBittorrent is still waiting for the torrent's details: nobody sharing it has been found yet.", True)
            seeds = max(info.get("num_complete") or 0, info.get("num_seeds") or 0)
            if seeds == 0 and (info.get("progress") or 0) < 1:
                return ("Nobody is sharing it: 0 seeds in the swarm%s." % (" (%d%% done so far)" % round(100 * info["progress"]) if info.get("progress") else ""), True)
            sp, eta = info.get("dlspeed") or 0, info.get("eta") or 0
            if sp and eta and eta < 8640000: return ("It's downloading slowly: %s, about %s left (%d seeds)." % (_speed(sp), mins(eta * 1000) if eta < 86400 else "%d days" % round(eta / 86400), seeds), False)
            return ("%d seed%s, but nothing is coming in right now (qBittorrent state: %s)." % (seeds, "" if seeds == 1 else "s", st or "?"), True)
        if it.get("protocol") == "usenet" and it.get("downloadId") and (S.get("sabnzbd") or {}).get("url"):
            z = S["sabnzbd"]; base = z["url"].rstrip("/") + "/api?output=json&apikey=" + urllib.parse.quote(z.get("key", ""))
            q = http(base + "&mode=queue&nzo_ids=" + urllib.parse.quote(it["downloadId"]), timeout=20).get("queue") or {}
            sl = (q.get("slots") or [None])[0]
            if sl: return ("SABnzbd says: %s%s." % (sl.get("status", "?"), (", %s left" % sl["timeleft"]) if sl.get("timeleft") and sl.get("timeleft") != "0:00:00" else ""), False)
            hi = http(base + "&mode=history&nzo_ids=" + urllib.parse.quote(it["downloadId"]), timeout=20).get("history") or {}
            sl = (hi.get("slots") or [None])[0]
            if sl and sl.get("fail_message"): return ("SABnzbd couldn't finish it: %s." % sl["fail_message"].rstrip(".")[:120], False)
            if sl: return ("SABnzbd says: %s." % sl.get("status", "?"), False)
            if (S.get("nzbget") or {}).get("url"): return _nzbget_reason(it["downloadId"]) or ("", False)
    except Exception as e:
        log("download reason failed: %r" % e)
    return ("", False)

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
                if qop is None and items[0].get("protocol") == "torrent":
                    try: qop = _qbit() or False
                    except Exception as e: log("qbit login failed: %r" % e); qop = False
                why, rean = dl_reason(items[0], qop or None)
                if why: items[0]["why"] = why
                text = why + (" " if why else "") + ((("Still at 0%% after %s, so probably nobody is sharing it. " % ("a night's download window" if NIGHT_END_HOUR is not None else "a whole day")) if items[0]["left"] >= items[0]["size"] and not why else "") + (items[0]["msg"] or ""))
                note = "Remove & try another removes it, blocklists that release and searches for a different one (it downloads " + dl_when() + ")."
                fixes = ["retry"]
                if rean and all(x.get("protocol") == "torrent" for x in items) and ((S.get("qbit") or {}).get("url") or (S.get("transmission") or {}).get("url")):
                    fixes = ["reannounce", "retry"]
                    note = "Re-announce asks every tracker for people sharing it again (worth a try first). " + note
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
    try: out.extend(incomplete_cards())
    except Exception as e: log("incomplete folder check failed: %r" % e)
    return out

# ---------------- Downloads: the incomplete folder (v0.1.9) ----------------
# 11-16: when a download fails or is removed, SABnzbd/qBittorrent can leave its half-finished files in the "incomplete"
# folder, and nothing ever cleans that folder (qbit_manage skips it). ~900 MB had sat there for a month. incomplete_scan()
# lists what no download client knows about and nothing has touched for LD_INCOMPLETE_DAYS -> Move to trash.
# Safety: if any download client that's set up can't be asked, nothing is offered (we can't tell what's still downloading).
INCOMPLETE_DAYS = int(os.environ.get("LD_INCOMPLETE_DAYS", "3"))
INCOMPLETE_EXTRA = [x.strip() for x in os.environ.get("LD_INCOMPLETE_DIRS", "").split(",") if x.strip()]
_PART_EXT = re.compile(r"\.(!qb|part)$", re.I)

def _inc_norm(n):
    return re.sub(r"[^a-z0-9]+", "", _PART_EXT.sub("", os.path.basename(str(n or "").rstrip("/"))).lower())

def _inc_clients():
    """({folder: client}, names in use, category folder names, {name: why}); raises if a client that's set up can't be asked."""
    dirs, used, cats, why = {}, set(), set(), {}
    def add_dir(d, cfg, who):
        if isinstance(d, str) and d.startswith("/"):
            k = os.path.realpath(to_local(d.rstrip("/"), cfg))
            dirs[k] = (dirs[k] + " + " + who) if dirs.get(k) and who not in dirs[k] else who
    z = S.get("sabnzbd") or {}
    if z.get("url"):
        base = z["url"].rstrip("/") + "/api?output=json&apikey=" + urllib.parse.quote(z.get("key", ""))
        misc = (http(base + "&mode=get_config&section=misc", timeout=20).get("config") or {}).get("misc") or {}
        add_dir(misc.get("download_dir"), z, "SABnzbd")
        cats.update(x for x in http(base + "&mode=get_cats", timeout=20).get("categories") or [] if x != "*")
        q = http(base + "&mode=queue&limit=1000", timeout=30).get("queue")
        if q is None: raise RuntimeError("SABnzbd didn't send its queue")
        for sl in q.get("slots") or []: used.add(_inc_norm(sl.get("filename")))
        for sl in (http(base + "&mode=history&limit=500", timeout=30).get("history") or {}).get("slots") or []:
            n = _inc_norm(sl.get("name"))
            if sl.get("status") not in ("Completed", "Failed"): used.add(n)
            elif sl.get("status") == "Failed": why[n] = "SABnzbd gave up on it" + (": " + sl["fail_message"].rstrip(".")[:100] if sl.get("fail_message") else "")
    q = S.get("qbit") or {}
    if q.get("url"):
        op = _qbit(); b = q["url"].rstrip("/")
        pr = json.loads(op.open(b + "/api/v2/app/preferences", timeout=20).read() or b"{}")
        if pr.get("temp_path_enabled"): add_dir(pr.get("temp_path"), q, "qBittorrent")
        for t in json.loads(op.open(b + "/api/v2/torrents/info", timeout=60).read() or b"[]"):
            used.add(_inc_norm(t.get("name"))); used.add(_inc_norm(t.get("content_path")))
        cats.update(json.loads(op.open(b + "/api/v2/torrents/categories", timeout=20).read() or b"{}").keys())
    t = S.get("transmission") or {}
    if t.get("url"):
        ss = _tr_rpc("session-get", {"fields": ["incomplete-dir", "incomplete-dir-enabled"]})
        if ss.get("incomplete-dir-enabled"): add_dir(ss.get("incomplete-dir"), t, "Transmission")
        for x in _tr_rpc("torrent-get", {"fields": ["name"]}).get("torrents") or []: used.add(_inc_norm(x.get("name")))
    n = S.get("nzbget") or {}
    if n.get("url"):
        conf = {o.get("Name"): o.get("Value") for o in _nzbget("config") or []}
        inter = str(conf.get("InterDir") or "")
        for k in ("MainDir", "DestDir"): inter = inter.replace("${%s}" % k, str(conf.get(k) or ""))
        if "$" not in inter: add_dir(inter, n, "NZBGet")
        cats.update(v for k, v in conf.items() if re.fullmatch(r"Category\d+\.Name", k or "") and v)
        for g in _nzbget("listgroups", [0]) or []:
            used.add(_inc_norm(g.get("NZBName"))); used.add(_inc_norm(g.get("DestDir")))
    for d in INCOMPLETE_EXTRA: dirs.setdefault(os.path.realpath(d), "")
    dirs = {d: w for d, w in dirs.items() if d.startswith(MEDIA_ROOT + "/") and os.path.isdir(d)}
    used.discard("")
    return dirs, used, cats, why

def _inc_tree(p):
    """(bytes really used on the disk, newest file change, files) for a file or a folder. SABnzbd writes sparse files, so a
    "3 GB" file can use a few MB: count the blocks, that's what moving it frees. Age = the newest file's modified time
    (downloaders write data, which updates it; a Put back, chown or chmod doesn't). A folder with no files uses its own."""
    st = os.lstat(p)
    if not os.path.isdir(p): return st.st_blocks * 512, st.st_mtime, 1
    size, newest, n = 0, 0, 0
    for dp, dn, fn in os.walk(p):
        for f in fn:
            try: s2 = os.lstat(os.path.join(dp, f))
            except OSError: continue
            size += s2.st_blocks * 512; newest = max(newest, s2.st_mtime); n += 1
    return size, (newest if n else st.st_mtime), n

def incomplete_scan():
    """{path: item} for everything left behind in the download clients' incomplete folders."""
    dirs, used, cats, why = _inc_clients()
    lcats, out, now = {c.lower() for c in cats}, {}, time.time()
    for d, who in dirs.items():
        ents = []
        for e in os.scandir(d):
            if e.name.startswith("."): continue
            if e.is_dir(follow_symlinks=False) and e.name.lower() in lcats:     # qBittorrent keeps a folder per category
                ents += [(e2, e.name) for e2 in os.scandir(e.path) if not e2.name.startswith(".")]
            else: ents.append((e, ""))
        for e, cat in ents:
            if e.is_symlink() or _inc_norm(e.name) in used: continue
            try: size, newest, files = _inc_tree(e.path)
            except OSError: continue
            days = (now - newest) / 86400
            if days < INCOMPLETE_DAYS: continue
            out[e.path] = {"title": e.name, "path": e.path, "size": size, "files": files, "days": int(days), "cat": cat,
                           "client": who, "why": why.get(_inc_norm(e.name), ""), "root": d}
    return out

def incomplete_cards():
    if not any((S.get(k) or {}).get("url") for k in ("sabnzbd", "qbit", "transmission", "nzbget")) and not INCOMPLETE_EXTRA: return []
    found = sorted(incomplete_scan().values(), key=lambda x: -x["size"])
    if not found: return []
    n, size = len(found), sum(x["size"] for x in found)
    roots = sorted({x["root"] for x in found})
    title = "Incomplete folder: %d leftover%s (%s)" % (n, "" if n == 1 else "s", human(size))
    text = ("%s in %s that no download client is working on and nothing has touched for at least %d days, taking %s. "
            "They're usually downloads that failed or were removed; nothing cleans this folder by itself." %
            ("1 item" if n == 1 else "%d items" % n, " and ".join(roots), INCOMPLETE_DAYS, human(size)))
    note = ("Move to trash puts them in Library Doctor's trash (deleted after %d days, Put back works until then). "
            "Each one is checked again first: anything a download client has picked up again is left alone." % TRASH_DAYS)
    items = [{"title": x["title"], "path": x["path"], "size": x["size"], "files": x["files"], "days": x["days"],
              "why": x["why"], "client": x["client"], "protocol": "untouched %d days" % x["days"], "ep": ""} for x in found]
    return [dict(id=iid("queue", "incomplete", *sorted(x["path"] for x in found)), chk="queue", sev="att", title=title, library="Downloads",
                 detail={"kind": "incomplete", "arr": "", "items": items, "count": n, "fixes": ["clearinc"], "text": text, "note": note, "size": size},
                 links=[])]

def _trash_tree(src, reason, title):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S") + "-incomplete-" + hashlib.sha1(src.encode()).hexdigest()[:8]
    files = [src] if not os.path.isdir(src) else [os.path.join(d, f) for d, _, fs in os.walk(src) for f in fs]
    for f in files:
        if os.path.islink(f): continue
        dst = os.path.join(TRASH_DIR, stamp, os.path.relpath(f, MEDIA_ROOT))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        size = os.path.getsize(f); move_file(f, dst)
        with db() as c: c.execute("INSERT INTO trash (issue_id, title, src, dst, size, reason, moved_at) VALUES (?,?,?,?,?,?,?)",
                                  ("queue", title[:200], f, dst, size, reason, now_iso()))
    if os.path.isdir(src):
        for d, dn, fn in sorted(os.walk(src, topdown=False), key=lambda x: -len(x[0])):
            try: os.rmdir(d)
            except OSError: pass

def _clear_incomplete(issue_id, row, d, only=None):
    items = [x for x in d["items"] if not only or x["path"] == only]
    if not items: return {"ok": False, "error": "That one isn't in the list any more. Refresh first."}
    items = [x for x in items if os.path.lexists(x["path"])]  # already gone (e.g. Move all ran first): nothing to do, not an error
    if not items: return {"ok": True, "error": ""}
    try: fresh = incomplete_scan()
    except Exception as e: return {"ok": False, "error": "Couldn't ask the download clients (%s), so nothing was moved." % e}
    okn, probs, freed = 0, [], 0
    with MOVE_LOCK:
        for it in items:
            p = it["path"]
            if p not in fresh: probs.append("%s: in use again or changed, left alone" % it["title"][:60]); continue
            if not os.path.realpath(p).startswith(fresh[p]["root"] + "/"): probs.append("%s: not inside the incomplete folder" % it["title"][:60]); continue
            try:
                _trash_tree(p, "Left in the incomplete folder", it["title"]); okn += 1; freed += it["size"]
            except Exception as e: probs.append("%s: %s" % (it["title"][:60], e))
    note = "Moved to the trash: %d of %d (%s)%s." % (okn, len(items), human(freed), (". Not done: " + "; ".join(probs)) if probs else "")
    left = [x for x in d["items"] if os.path.lexists(x["path"])]
    with db() as c:
        c.execute("INSERT INTO done (issue_id, title, library, kept, moved, trash_ids, links, at, status, note, chk) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (issue_id, row["title"] if not only else "Incomplete folder: " + items[0]["title"][:150], row["library"], "", "[]", "[]", "[]", now_iso(), "ok" if okn == len(items) else "warn", note, "queue"))
        if not left: c.execute("DELETE FROM issues WHERE id=?", (issue_id,))
        else:
            d = dict(d, items=left, count=len(left), size=sum(x["size"] for x in left))
            c.execute("UPDATE issues SET detail=?, title=? WHERE id=?", (json.dumps(d), "Incomplete folder: %d leftover%s (%s)" % (len(left), "" if len(left) == 1 else "s", human(d["size"])), issue_id))
    event("queue", "Incomplete folder: %s" % note)
    return {"ok": okn > 0, "error": "; ".join(probs) if not okn else ""}

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

def queue_fix(issue_id, how, only=None):
    with db() as c:
        row = c.execute("SELECT * FROM issues WHERE id=? AND chk='queue'", (issue_id,)).fetchone()
    if not row: return {"ok": False, "error": "That one isn't in the queue list any more. Refresh first."}
    d = json.loads(row["detail"])
    if how not in d.get("fixes", []): return {"ok": False, "error": "That fix doesn't apply here."}
    if d.get("kind") == "incomplete": return _clear_incomplete(issue_id, row, d, only)
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
            elif how == "reannounce":
                op = _qbit()
                if op:
                    op.open(S["qbit"]["url"].rstrip("/") + "/api/v2/torrents/reannounce", data=urllib.parse.urlencode({"hashes": it["downloadId"].lower()}).encode(), timeout=20).read()
                if (S.get("transmission") or {}).get("url"):
                    try: _tr_rpc("torrent-reannounce", {"ids": [it["downloadId"].lower()]})
                    except Exception as e:
                        if not op: raise
                if not op and not (S.get("transmission") or {}).get("url"): raise RuntimeError("No torrent client is set up in Settings")
                okn += 1
            elif how == "remove":
                why = _remove_leftover(it)
                if why: probs.append(why)
                else: okn += 1
        except Exception as e:
            probs.append("%s: %s" % (it["ep"] or it["title"][:50], e))
    n = len(d["items"])
    verb = {"import": "Imported", "retry": "Removed and searching again for", "remove": "Moved to the trash:"}[how]
    note = "%s %d of %d%s." % (verb, okn, n, (". Not done: " + "; ".join(probs)) if probs else "")
    if how == "retry" and okn: note += " Anything it finds downloads " + dl_when() + "."
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
# v0.1.9 reverse-proxy safety.
#   LD_TRUSTED_PROXIES  your proxy's address(es) or ranges (e.g. 172.16.0.0/12,127.0.0.1). X-Forwarded-For/-Proto are only
#                       believed when the request comes from one of them (anyone can send those headers).
#   LD_ALLOWED_HOSTS    the name(s) you open Library Doctor by (e.g. ld.example.com). Requests for any other name are refused,
#                       which stops "DNS rebinding" (a web page making your browser talk to Library Doctor). IPs and localhost always work.
#   With LD_AUTH=off only the trusted proxies (or, if none are set, local/private addresses) get an answer.
import ipaddress
def _nets(v):
    out = []
    for x in (v or "").split(","):
        x = x.strip()
        if not x: continue
        try: out.append(ipaddress.ip_network(x, strict=False))
        except ValueError: log("LD_TRUSTED_PROXIES: ignoring %r (not an address or range)" % x)
    return out
TRUSTED_PROXIES = _nets(os.environ.get("LD_TRUSTED_PROXIES", ""))
ALLOWED_HOSTS = {h.strip().lower().rstrip(".") for h in os.environ.get("LD_ALLOWED_HOSTS", "").split(",") if h.strip()}

def _ipobj(s):
    try: a = ipaddress.ip_address(str(s).strip().strip("[]"))
    except ValueError: return None
    return a.ipv4_mapped or a if a.version == 6 else a
def is_trusted_proxy(s):
    a = _ipobj(s); return bool(a and any(a in n for n in TRUSTED_PROXIES))
def is_local(s):
    a = _ipobj(s); return bool(a and (a.is_private or a.is_loopback or a.is_link_local))
def is_loopback(s):
    a = _ipobj(s); return bool(a and a.is_loopback)
def host_name(h):
    h = (h or "").strip().lower()
    return (h[1:].split("]")[0] if h.startswith("[") else h.split(":")[0]).rstrip(".")
def host_ok(h):
    n = host_name(h)
    return not ALLOWED_HOSTS or not n or n in ALLOWED_HOSTS or n == "localhost" or _ipobj(n) is not None
_BLOCK_NOTE = {"t": 0}
def note_blocked(ip, why):
    """Remember (at most once a minute) that a request was refused, for the warning on the page."""
    if time.time() - _BLOCK_NOTE["t"] < 60: return
    _BLOCK_NOTE["t"] = time.time()
    log("refused a request from %s: %s" % (ip, why))
    try: kv_set("security_blocked", {"at": now_iso(), "ip": str(ip)[:60], "why": why})
    except Exception: pass

def security_info():
    b = kv_get("security_blocked")
    if b and b.get("at", "") < (datetime.now() - timedelta(days=7)).isoformat(timespec="seconds"): b = None
    return {"auth": AUTH_ON, "trusted": [str(n) for n in TRUSTED_PROXIES], "hosts": sorted(ALLOWED_HOSTS), "blocked": b}
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
SECRET_FIELDS = ("token", "key", "password")
SINGLE = ("plex", "tautulli", "tdarr", "sportarr", "prowlarr", "qbit", "transmission", "sabnzbd", "nzbget", "gotify") + NOTIFY_KINDS
LISTS = ("arrs", "bazarr")

def settings_public():
    """Settings for the page: secrets replaced by <field>_set: true. List items carry _i (their index) so a save can keep unchanged keys."""
    def red(e, i=None):
        e = _clean_entry(e); e.pop("container_root", None); e.pop("media_root", None)
        for f in SECRET_FIELDS:
            if f in e: e[f + "_set"] = bool(e.pop(f))
        if i is not None: e["_i"] = i
        return e
    out = {k: red(S[k]) for k in SINGLE if isinstance(S.get(k), dict)}
    for k in LISTS: out[k] = [red(e, i) for i, e in enumerate(S.get(k) or [])]
    h, mi, days = sched()
    out["schedule"] = {"hour": h, "minute": mi, "days": days}
    out["language"] = pref_language()
    out["trash_days"] = TRASH_DAYS; out["digest"] = S.get("digest") or "news"
    tz = os.environ.get("TZ", "").strip()
    if not tz:
        try: tz = os.path.relpath(os.path.realpath("/etc/localtime"), "/usr/share/zoneinfo")
        except Exception: tz = ""
    hints = {"puid": os.getuid() or 1000, "pgid": os.getgid() or 1000, "tz": tz if tz and not tz.startswith("..") else "Etc/UTC"}   # v0.1.9 "Don't have it?" snippets
    return {"settings": out, "configured": configured(), "media_root": MEDIA_ROOT, "languages": sorted(LANGS), "hints": hints}

RUNTIME_KEYS = ("roots", "machine")                          # filled in while running; never written to config.json

def _clean_entry(e):
    """v0.1.7: drop runtime caches (keys starting with "_" such as _rbown, plus roots/machine) from a settings entry."""
    return {k: v for k, v in (e or {}).items() if not (k.startswith("_") and k != "_i") and k not in RUNTIME_KEYS}

def _with_secrets(kind, e):
    """Fill blank secret fields of an edited entry from what's saved (matched by _i for lists)."""
    e = {k: v for k, v in _clean_entry(e).items() if not k.endswith("_set")}
    old = (S.get(kind) or [])[e["_i"]] if kind in LISTS and isinstance(e.get("_i"), int) and e["_i"] < len(S.get(kind) or []) else S.get(kind) if kind in SINGLE else None
    if kind == "plex":                       # v0.2: never hand a saved Plex token to Jellyfin/Emby (or the other way round)
        if e.get("kind") not in ("plex", "jellyfin", "emby"): e["kind"] = "plex"
        if isinstance(old, dict) and (old.get("kind") or "plex") != e["kind"]: old = None
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
            r = ms_request("GET", "/identity", timeout=15, server=e)["MediaContainer"]
            ms_request("GET", "/library/sections", timeout=15, server=e)      # identity works without a token; this doesn't
            return {"ok": True, "msg": "Connected to %s %s." % (ms_name(e), r.get("version", "")), "machine": r.get("machineIdentifier")}
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
        if kind == "prowlarr":
            r = http(u + "/api/v1/system/status", {"X-Api-Key": e.get("key", "")}, timeout=15)
            return {"ok": True, "msg": "Connected to Prowlarr %s." % r.get("version", "")}
        if kind == "sportarr":
            http(u + "/api/rootfolder", {"X-Api-Key": e.get("key", "")}, timeout=15)
            return {"ok": True, "msg": "Connected to Sportarr."}
        if kind == "qbit":
            op = _qbit(e)
            v = op.open(u.rstrip("/") + "/api/v2/app/version", timeout=15).read().decode().strip()
            return {"ok": True, "msg": "Connected to qBittorrent %s." % v}
        if kind == "transmission":
            r = _tr_rpc("session-get", {"fields": ["version"]}, cfg=e)
            return {"ok": True, "msg": "Connected to Transmission %s." % r.get("version", "")}
        if kind == "nzbget":
            return {"ok": True, "msg": "Connected to NZBGet %s." % (_nzbget("version", cfg=e) or "")}
        if kind == "sabnzbd":
            r = http(u + "/api?" + urllib.parse.urlencode({"mode": "queue", "output": "json", "limit": 1, "apikey": e.get("key", "")}), timeout=15)
            if r.get("error") or r.get("status") is False: return {"ok": False, "msg": "SABnzbd said: %s. Check the API key." % (r.get("error") or "no")}
            v = http(u + "/api?mode=version&output=json", timeout=15).get("version", "")
            return {"ok": True, "msg": "Connected to SABnzbd %s." % v}
        if kind in NOTIFY_KINDS:
            r = notify_one(kind, e, "Library Doctor", "Connected. Nightly summaries will come here.", 3)
            return {"ok": r.get("ok"), "msg": "Sent a test message." if r.get("ok") else r.get("error")}
        if kind == "gotify":
            http(u + "/message", {"X-Gotify-Key": e.get("token", "")}, data={"title": "Library Doctor", "message": "Connected. Nightly summaries will come here.", "priority": 3}, method="POST", timeout=15)
            return {"ok": True, "msg": "Sent a test message to Gotify."}
    except urllib.error.HTTPError as x:
        return {"ok": False, "msg": "It answered, but said %d %s. %s" % (x.code, x.reason, ("Check the username and password." if kind in ("qbit", "transmission", "nzbget") else "Check the API key.") if x.code in (401, 403) else "")}
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
        for sec in ms_request("GET", "/library/sections", timeout=20, server=e)["MediaContainer"].get("Directory", []):
            typ = {"movie": 1, "show": 4}.get(sec.get("type"))
            if not typ: continue
            for m in ms_request("GET", "/library/sections/%s/all" % sec["key"], {"type": typ, "X-Plex-Container-Start": 0, "X-Plex-Container-Size": 4},
                                timeout=30, server=e)["MediaContainer"].get("Metadata", []):
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

# ---------------- folder check (v0.1.7) ----------------
# The number one reason a tool like this fails on day one: each app sees the media under a different folder (Plex /data/movies,
# Radarr /movies, Library Doctor /media/movies). folder_check() asks every app for a few real paths, applies that app's
# mapping and looks for the files. Shown in Settings (and run by itself after a save and before every scan); a scan
# stops instead of reporting thousands of "missing" files when Plex's or Radarr/Sonarr's files can't be found at all.
_FC = {"t": 0, "v": []}

def apply_trash_days():
    global TRASH_DAYS
    try: TRASH_DAYS = int(S.get("trash_days") or TRASH_DAYS_ENV)
    except (TypeError, ValueError): TRASH_DAYS = TRASH_DAYS_ENV

def _fc_apps():
    out = []
    if (S.get("plex") or {}).get("url"): out.append(("plex", None, S["plex"], "Plex", True))
    for i, a in enumerate(S.get("arrs") or []):
        if a.get("url"): out.append(("arrs", i, a, a.get("name") or a.get("kind", "").title(), True))
    for i, b in enumerate(S.get("bazarr") or []):
        if b.get("url"): out.append(("bazarr", i, b, b.get("name") or "Bazarr", False))
    for k, n in (("tdarr", "Tdarr"), ("sportarr", "Sportarr")):
        if (S.get(k) or {}).get("url"): out.append((k, None, S[k], n, False))
    return out

def folder_check(force=False):
    if not force and time.time() - _FC["t"] < 600: return _FC["v"]
    rows = []
    for kind, idx, e, name, needed in _fc_apps():
        row = {"app": name, "kind": kind, "i": idx, "needed": needed, "mapping": e.get("paths") or [], "total": 0, "found": 0}
        try: smp = _samples(kind, e)
        except Exception as x:
            row.update(status="error", msg="Couldn't ask %s for its folders (%s)." % (name, getattr(x, "reason", x))); rows.append(row); continue
        pairs = _pairs(e)
        loc = [(p, _swap(p, pairs)) for p in smp]
        ok = [(a, b) for a, b in loc if os.path.exists(b)]
        row.update(total=len(loc), found=len(ok))
        ex = (ok or loc or [("", "")])[0]
        row["example"] = {"app": ex[0], "local": ex[1], "exists": bool(ok)}
        if not loc: row.update(status="empty", msg="%s didn't report any files yet." % name)
        elif len(ok) >= max(1, round(len(loc) * 0.8)): row.update(status="ok", msg="%d of %d found." % (len(ok), len(loc)))
        else:
            row.update(status="bad" if len(ok) < len(loc) / 2 else "partial", msg="Only %d of %d found." % (len(ok), len(loc)))
            try:
                d = settings_detect(kind, dict(e, _i=idx) if idx is not None else dict(e))
                if d.get("ok"): row["suggest"] = d.get("paths") or []; row["suggest_msg"] = d.get("msg") if d.get("paths") else "It needs no mapping at all: the files are at the same place as here."
                else: row["suggest_msg"] = d.get("msg")
            except Exception as x: row["suggest_msg"] = str(x)
        rows.append(row)
    _FC.update(t=time.time(), v=rows)
    return rows

def folder_blockers(rows):
    """Apps the scan can't work without (Plex, Radarr/Sonarr) whose files can't be found at all."""
    return [r for r in rows if r.get("needed") and r.get("status") == "bad" and r.get("total", 0) >= 2]

# v0.1.7: settings backup. A JSON file with everything from Settings (API keys included, so keep it safe), for moving
# Library Doctor to another server. The password is not in it (set a new one there).
# v0.1.9: the file either leaves the API keys out, or keeps them encrypted with a passphrase (AES-256 via openssl, the
# same format as `openssl enc -aes-256-cbc -pbkdf2 -iter 600000 -md sha256`, so it can be opened by hand too).
EXPORT_ITER = 600000
def _strip_secrets(e):
    return {k: v for k, v in e.items() if k not in SECRET_FIELDS} if isinstance(e, dict) else e

def settings_export(keys=False):
    out = {}
    for k, v in S.items():
        out[k] = _clean_entry(v) if isinstance(v, dict) else [_clean_entry(x) if isinstance(x, dict) else x for x in v] if isinstance(v, list) else v
        if not keys: out[k] = _strip_secrets(out[k]) if isinstance(out[k], dict) else [_strip_secrets(x) for x in out[k]] if isinstance(out[k], list) else out[k]
    return {"library_doctor_settings": 1, "version": APP_VERSION, "exported": now_iso(), "keys": bool(keys), "settings": out}

def _openssl(args, data, passphrase):
    import base64  # noqa: F401  (kept next to its users)
    r = subprocess.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", str(EXPORT_ITER), "-md", "sha256", "-pass", "env:LD_EXPORT_PASS"] + args,
                       input=data, capture_output=True, timeout=120, env={"LD_EXPORT_PASS": passphrase, "PATH": os.environ.get("PATH", "/usr/bin:/bin")})
    return r

def settings_export_encrypted(passphrase):
    import base64
    if not isinstance(passphrase, str) or len(passphrase) < 10: return {"ok": False, "msg": "Use a passphrase of at least 10 characters."}
    try: r = _openssl(["-salt"], json.dumps(settings_export(keys=True)).encode(), passphrase)
    except FileNotFoundError: return {"ok": False, "msg": "openssl isn't in this image, so the keys can't be encrypted. Download without keys instead."}
    if r.returncode: return {"ok": False, "msg": "Encrypting failed: %s" % r.stderr.decode(errors="replace")[:160]}
    return {"ok": True, "file": {"library_doctor_settings": 1, "version": APP_VERSION, "exported": now_iso(), "keys": True,
            "encrypted": "aes-256-cbc, pbkdf2 sha256 %d (openssl enc)" % EXPORT_ITER,
            "open_by_hand": "put the data value in data.txt, then: base64 -d data.txt | openssl enc -d -aes-256-cbc -pbkdf2 -iter %d -md sha256" % EXPORT_ITER,
            "data": base64.b64encode(r.stdout).decode()}}

def settings_import(data, passphrase=None):
    if isinstance(data, dict) and data.get("library_doctor_settings") and data.get("encrypted"):
        import base64
        if not passphrase: return {"ok": False, "need_pass": True, "msg": "This file is protected: type its passphrase, then choose the file again."}
        try: r = _openssl(["-d"], base64.b64decode(data.get("data") or ""), str(passphrase))
        except FileNotFoundError: return {"ok": False, "msg": "openssl isn't in this image, so the file can't be opened."}
        except Exception: return {"ok": False, "msg": "That file is damaged."}
        if r.returncode: return {"ok": False, "need_pass": True, "msg": "Wrong passphrase (or the file is damaged)."}
        try: data = json.loads(r.stdout)
        except ValueError: return {"ok": False, "need_pass": True, "msg": "Wrong passphrase (or the file is damaged)."}
    cfg = (data or {}).get("settings") if isinstance(data, dict) else None
    if not isinstance(cfg, dict) or not (data or {}).get("library_doctor_settings"):
        return {"ok": False, "msg": "That isn't a Library Doctor settings file."}
    known = set(SINGLE) | set(LISTS) | {"schedule", "language", "trash_days", "digest"}
    for k, v in cfg.items():                                  # settings this page doesn't edit (e.g. qbit) come along too
        if k not in known: S[k] = v
    new = {k: cfg[k] for k in known if k in cfg}
    for k in LISTS: new.setdefault(k, [])
    if not data.get("keys", True):                            # v0.1.9: a file without keys keeps the keys saved here;
        for k in LISTS:                                       # list entries are matched by name, then by address
            olds = S.get(k) or []
            for e in new.get(k) or []:
                if not isinstance(e, dict) or "_i" in e: continue
                for i, o in enumerate(olds):
                    if (e.get("name") and o.get("name") == e.get("name")) or (e.get("url") and str(o.get("url") or "").rstrip("/") == str(e["url"]).rstrip("/")):
                        e["_i"] = i; break
    r = settings_save(new)
    if r.get("ok"): event("settings", "Settings restored from a backup file (made %s)." % str(data.get("exported", "?"))[:16])
    return r

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
    if "trash_days" in (new or {}):                          # v0.1.7: days in the trash, from Settings
        try: td = int(new.get("trash_days"))
        except (TypeError, ValueError): return {"ok": False, "msg": "Pick how many days the trash keeps files."}
        if td not in (3, 7, 14, 30): return {"ok": False, "msg": "Pick 3, 7, 14 or 30 days for the trash."}
        cfg["trash_days"] = td
    elif "trash_days" in S: cfg["trash_days"] = S["trash_days"]
    if "digest" in (new or {}):                              # v0.1.7: news (after scans with news) | weekly | both
        dg = str(new.get("digest") or "news")
        cfg["digest"] = dg if dg in ("news", "weekly", "both") else "news"
    elif "digest" in S: cfg["digest"] = S["digest"]
    if "language" in (new or {}):                            # v0.1.6: audio language check ("" = off)
        lg = str(new.get("language") or "")
        if lg and lg not in LANGS: return {"ok": False, "msg": "Pick a language from the list."}
        cfg["language"] = lg
    elif "language" in S:
        cfg["language"] = S["language"]
    for k in SINGLE:
        e = (new or {}).get(k)
        if isinstance(e, dict) and k in NOTIFY_DEFAULT_URL and not e.get("url"): e["url"] = NOTIFY_DEFAULT_URL[k]
        if isinstance(e, dict) and e.get("url"): cfg[k] = _with_secrets(k, e)
    for k in LISTS:
        cfg[k] = [_with_secrets(k, e) for e in (new or {}).get(k) or [] if isinstance(e, dict) and e.get("url")]
    # v0.1.4: keep settings this page doesn't edit (e.g. "qbit" from an older secrets.json) instead of dropping them
    for k, v in S.items():
        if k not in cfg and k not in SINGLE and k not in LISTS and k not in ("schedule", "language", "trash_days", "digest"): cfg[k] = v
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
    apply_trash_days(); _FC["t"] = 0
    _TDARR_LIBS["t"] = 0; _STATS["v"] = None
    event("settings", "Settings saved.")
    return {"ok": True, "configured": configured()}

_SIZE_RE = re.compile(r"saved ([\d.]+) (B|KB|MB|GB|TB)\b")
_UNITS = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}
_STATS = {"at": 0, "v": None}
TDARR_SAVE_GUESS = 0.30          # Tdarr's HEVC conversions here have saved 28-33%

# ---------------- Library score + trends (v0.1.9) ----------------
# The old ring was "files with a problem / all files", which sits at 99.9% for almost everyone. Now five areas, each 0-100:
#   integrity  broken / too short / wrong language / missing files / viruses       vs files   x25  (1% broken = 75)
#   dupes      extra copies                                                         vs files   x10  (1% extra = 90)
#   quality    oversized, old codecs, odd containers, Tdarr failures               vs files   x4   (5% = 80)
#   metadata   Plex metadata gaps, subtitles, trailers, films Plex merged           vs titles  x6
#   sync       downloads stuck, app warnings, titles Radarr/Sonarr don't know, missing episodes, Folder check: points off
# overall = 30% integrity, 20% quality, 20% sync, 15% metadata, 15% duplicates. Ignored cards don't count.
AREAS = [("integrity", "File integrity", 0.30, 25.0), ("quality", "Quality", 0.20, 4.0), ("sync", "App sync", 0.20, None),
         ("metadata", "Metadata", 0.15, 6.0), ("dupes", "Duplicates", 0.15, 10.0)]
SEV_W = {"bad": 1.0, "att": 0.6}
SYNC_KINDS = ("notarr", "notsportarr", "gaps")

def area_of(chk, kind):
    if chk == "unwatched": return None
    if chk in ("health", "virus"): return "integrity"
    if chk == "missing": return "sync" if kind == "gaps" else "integrity"
    if chk == "dupes": return "metadata" if kind == "mismatch" else "dupes"
    if chk == "quality": return "quality"
    if chk == "meta": return "sync" if kind in SYNC_KINDS else "metadata"
    if chk in ("subs", "coming"): return "metadata"
    if chk in ("queue", "apps"): return "sync"
    return None

def _units(chk, d):
    """How many files/episodes one card stands for."""
    if chk == "dupes" and d.get("versions"): return max(1, len([v for v in d["versions"] if v.get("action") != "keep"]))
    for k in ("eps", "files"):
        if isinstance(d.get(k), list) and d[k]: return len(d[k])
    return d["count"] if isinstance(d.get("count"), int) and d["count"] > 0 else 1

def library_score():
    with db() as c:
        sc = c.execute("SELECT items, files FROM scans WHERE ok=1 ORDER BY id DESC LIMIT 1").fetchone()
        ign = hidden_ids(c)
        rows = [dict(r) for r in c.execute("SELECT id, chk, sev, title, library, detail FROM issues")]
    if not sc: return None
    files, items = max(1, sc["files"] or 0), max(1, sc["items"] or 0)
    hit = {a[0]: [] for a in AREAS}
    for r in rows:
        if r["id"] in ign: continue
        try: d = json.loads(r["detail"] or "{}")
        except ValueError: d = {}
        a = area_of(r["chk"], d.get("kind") or "")
        if not a: continue
        hit[a].append({"title": r["title"], "library": r["library"], "chk": r["chk"], "w": _units(r["chk"], d) * SEV_W.get(r["sev"], 0.4), "sev": r["sev"]})
    fc_bad = [x for x in (_FC.get("v") or []) if x.get("status") in ("bad", "partial")]
    out, overall = [], 0.0
    for key, label, weight, k in AREAS:
        hs = hit[key]
        if key == "sync":
            pts = sum(12 if h["sev"] == "bad" else 6 for h in hs) + 20 * len(fc_bad)
            score = max(0.0, 100.0 - pts)
            what = "%d open card%s%s" % (len(hs), "" if len(hs) == 1 else "s", (", %d app%s whose folders don't line up" % (len(fc_bad), "" if len(fc_bad) == 1 else "s")) if fc_bad else "")
        else:
            base = items if key == "metadata" else files
            aff = sum(h["w"] for h in hs)
            score = max(0.0, 100.0 - aff / base * 100.0 * k)
            what = "%s card%s, about %s %s of %s" % (len(hs), "" if len(hs) == 1 else "s", "{:,}".format(round(aff)), "title" + ("" if round(aff) == 1 else "s") if key == "metadata" else "file" + ("" if round(aff) == 1 else "s"), "{:,}".format(base))
        top = [{"title": h["title"], "library": h["library"]} for h in sorted(hs, key=lambda h: -h["w"])[:3]]
        out.append({"key": key, "label": label, "weight": weight, "score": round(score, 1), "open": len(hs), "what": what, "top": top})
        overall += weight * score
    return {"overall": round(overall, 1), "areas": out, "files": files, "items": items}

def _res_label(r):
    r = str(r or "").lower()
    return "4K" if r in ("4k", "2160", "2160p") else "1080p" if r in ("1080", "1080p") else "720p" if r in ("720", "720p") else "SD" if r else "Unknown"

def _codec_label(c):
    c = str(c or "").lower()
    return "HEVC" if c in ("hevc", "h265", "x265") else "H.264" if c in ("h264", "avc", "x264") else "AV1" if c == "av1" else "Older" if c in OLD_CODECS else "Other" if c else "Unknown"

def _lib_count(libstat, lib, kind, m):
    ls = libstat.setdefault(lib, {"kind": kind, "items": 0, "files": 0, "size": 0, "res": {}, "codec": {}})
    ls["items"] += 1
    medias = m.get("Media") or []
    if medias:
        md0 = max(medias, key=lambda x: int(x.get("width") or 0))
        r, c = _res_label(md0.get("videoResolution")), _codec_label(md0.get("videoCodec"))
        ls["res"][r] = ls["res"].get(r, 0) + 1; ls["codec"][c] = ls["codec"].get(c, 0) + 1
    for md in medias:
        for pt in md.get("Part") or []:
            ls["files"] += 1; ls["size"] += int(pt.get("size") or 0)

def analysis():
    """Everything the Analysis tab shows: overview, score, trends, storage and quality per library."""
    st = kv_get("library_stats") or {}
    libs = st.get("libs") or {}
    out_libs = []
    for name, l in sorted(libs.items(), key=lambda x: -x[1].get("size", 0)):
        res = l.get("res") or {}; tot = sum(res.values()) or 1
        out_libs.append(dict(l, name=name, hd=round(100.0 * (res.get("4K", 0) + res.get("1080p", 0) + res.get("720p", 0)) / tot, 1)))
    try: sf = space_forecast()
    except Exception: sf = {}
    t = trends()
    return {"at": st.get("at"), "libraries": out_libs, "totals": {"items": sum(l["items"] for l in out_libs), "files": sum(l["files"] for l in out_libs),
            "size": sum(l["size"] for l in out_libs), "movies": sum(l["items"] for l in out_libs if l["kind"] == "movie"),
            "episodes": sum(l["items"] for l in out_libs if l["kind"] == "show")}, "disk": {"free": sf.get("free"), "total": sf.get("total"), "days_left": sf.get("days_left")}, **t}

def score_snapshot(min_hours=0):
    sc = library_score()
    if not sc: return
    with db() as c:
        last = c.execute("SELECT at FROM scores ORDER BY at DESC LIMIT 1").fetchone()
        if min_hours and last and last["at"] > (datetime.now() - timedelta(hours=min_hours)).isoformat(timespec="seconds"): return
        c.execute("INSERT OR REPLACE INTO scores VALUES (?,?,?)", (now_iso(), sc["overall"], json.dumps({a["key"]: a["score"] for a in sc["areas"]})))
        c.execute("DELETE FROM scores WHERE at < ?", ((datetime.now() - timedelta(days=400)).isoformat(timespec="seconds"),))

def recurring(days=90, limit=8):
    """Problems that keep coming back: back after a fix, seen again and again between scans, or the same title fixed twice."""
    since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    with db() as c:
        ign = hidden_ids(c)
        openi = {r["id"]: dict(r) for r in c.execute("SELECT id, chk, title, library, first_seen, detail FROM issues")}
        app = [dict(r) for r in c.execute("SELECT issue_id, chk, kind, title, library, at FROM appearances WHERE at >= ?", (since,))]
        done = [dict(r) for r in c.execute("SELECT issue_id, title, library, chk, at FROM done WHERE at >= ? AND issue_id IS NOT NULL AND issue_id <> '' AND chk NOT IN ('queue', 'apps')", (since,))]
    rec = {}
    def add(issue_id, title, library, chk, why, n, last):
        key = (chk, (title or "").strip().lower(), library or "")          # one row per problem, however it was spotted
        r = rec.setdefault(key, {"title": title, "library": library, "chk": chk, "times": 0, "why": set(), "last": "", "ids": set()})
        r["times"] = max(r["times"], n); r["why"].add(why); r["last"] = max(r["last"], last or ""); r["ids"].add(issue_id)
    SUMMARY = ("wanted", "tdarrmissed", "tdarrfail", "incomplete")     # running totals, not one problem: they come back by design
    def skip(issue_id, kind=""):
        if issue_id in ign or kind in SUMMARY: return True
        o = openi.get(issue_id)
        if o:
            try: return (json.loads(o.get("detail") or "{}").get("kind") or "") in SUMMARY
            except ValueError: return False
        return False
    by_issue = {}
    for a in app:
        if not skip(a["issue_id"], a.get("kind") or ""): by_issue.setdefault(a["issue_id"], []).append(a)
    for iid_, lst in by_issue.items():
        if len(lst) >= 2:
            add(iid_, lst[-1]["title"], lst[-1]["library"], lst[-1]["chk"], "came back %d times" % (len(lst) - 1), len(lst), lst[-1]["at"])
    fixed = {}
    for d in done:
        if not skip(d["issue_id"]): fixed.setdefault(d["issue_id"], []).append(d)
    for iid_, lst in fixed.items():
        o = openi.get(iid_)
        if o and o["first_seen"] > min(x["at"] for x in lst):
            add(iid_, o["title"], o["library"], o["chk"], "back after a fix", len(lst) + 1, o["first_seen"])
    by_title = {}
    for d in done:
        if d["chk"] in ("health", "dupes", "virus", "missing") and not skip(d["issue_id"]):   # real file problems only
            by_title.setdefault((d["chk"], (d["title"] or "").strip().lower()), []).append(d)
    for (chk, t), lst in by_title.items():
        ids = {x["issue_id"] for x in lst}
        if len(lst) >= 2 and t:
            add(lst[-1]["issue_id"], lst[-1]["title"], lst[-1]["library"], chk, "fixed %d times" % len(lst), len(lst), max(x["at"] for x in lst))
    out = [dict({k2: v for k2, v in r.items() if k2 != "ids"}, why=", ".join(sorted(r["why"])), open=any(i in openi and i not in ign for i in r["ids"])) for r in rec.values()]
    out.sort(key=lambda r: (r["times"], r["last"]), reverse=True)
    by_chk = {}
    for a in app: by_chk[a["chk"]] = by_chk.get(a["chk"], 0) + 1
    return {"items": out[:limit], "total": len(out), "new_by_check": by_chk, "days": days}

def trends():
    sc = library_score()
    with db() as c:
        pts = [dict(r) for r in c.execute("SELECT at, overall, areas FROM scores WHERE at >= ? ORDER BY at", ((datetime.now() - timedelta(days=90)).isoformat(timespec="seconds"),))]
        first = c.execute("SELECT MIN(at) FROM scores").fetchone()[0]
        scans = [dict(r) for r in c.execute("SELECT finished, issues FROM scans WHERE ok=1 ORDER BY id DESC LIMIT 60")]
    def ago(days):
        cut = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        old = [p for p in pts if p["at"] <= cut]
        return old[-1]["overall"] if old else None
    cur = sc["overall"] if sc else None
    d7, d30 = ago(7), ago(30)
    h = history(30)
    space30 = (h["space"][-1]["total"] - h["space"][0]["total"]) if h.get("space") else 0
    st = stats()
    iss = [s["issues"] for s in reversed(scans)]
    return {"score": sc, "points": [{"at": p["at"], "overall": p["overall"], "areas": json.loads(p["areas"] or "{}")} for p in pts], "since": first,
            "delta7": None if cur is None or d7 is None else round(cur - d7, 1), "delta30": None if cur is None or d30 is None else round(cur - d30, 1),
            "issues_first": iss[0] if iss else None, "issues_now": iss[-1] if iss else None, "issues_scans": len(iss),
            "recovered": {"freed": st.get("freed", 0) if isinstance(st, dict) else 0, "pending": st.get("pending", 0) if isinstance(st, dict) else 0, "last30": space30},
            "recurring": recurring()}

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
        trash = [dict(r) for r in c.execute("SELECT src, dst, size, reason, moved_at, restored_at, purged_at FROM trash")]
        done = [dict(r) for r in c.execute("SELECT chk, status, note, at, cleared FROM done")]
        ign = hidden_ids(c)
        issues = [dict(r) for r in c.execute("SELECT id, chk, detail FROM issues")]
    # 1) files Library Doctor moved out for good: duplicates, leftovers, old .ts copies (minus the new .mkv). Not replaced broken files or Plex fixes.
    for t in trash:
        r = t["reason"] or ""
        if t["restored_at"] or "(replaced)" in r or r.startswith("Plex fix") or r.startswith("Virus") or "(hardlinked)" in r: continue
        size = t["size"] or 0
        if r.startswith("Old .ts copy"):
            mkv = os.path.splitext(t["src"])[0] + ".mkv"
            try: size = max(0, size - os.path.getsize(mkv))
            except OSError: size = 0
        if t["purged_at"]: freed += size
        else:                                      # still in Library Doctor's trash: freed when it empties (not if it's hardlinked)
            try:
                if os.stat(t["dst"]).st_nlink > 1: continue
            except OSError: pass
            pending += size
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
            possible += sum(v.get("size") or 0 for v in d.get("versions") or [] if v.get("action") == "remove" and (v.get("links") or 1) <= 1)
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
        x["moved"] = json.loads(x["moved"] or "[]"); x["links"] = live_plex_links(json.loads(x["links"] or "[]"), "d", x["id"])
        x["trash_ids"] = [t for t in json.loads(x["trash_ids"] or "[]") if t in open_trash]
        try: rf = json.loads(x.get("ref") or "null")
        except Exception: rf = None
        x["tdarr"] = [{"name": os.path.basename(os.path.dirname(it["path"])), "state": it.get("state", "queued"), "pct": it.get("pct"),
                       "size": it["size"], "after": it.get("after")} for it in rf["items"]] if isinstance(rf, dict) and rf.get("kind") == "tdarr" else None
    for r in rows:
        r["detail"] = json.loads(r["detail"]); r["links"] = live_plex_links(json.loads(r["links"]), "i", r["id"]); r["ignored"] = r["id"] in ign
        if r["id"] in snz: r["snoozed"] = snz[r["id"]]
    held = kv_get("trash_held") or {}
    for t in trash:
        t["empties"] = (datetime.fromisoformat(t["moved_at"]) + timedelta(days=TRASH_DAYS)).isoformat(timespec="seconds")
        if str(t["id"]) in held: t["held"] = held[str(t["id"])]
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
    try: space = space_forecast()
    except Exception as e: space = None; log("space forecast failed: %r" % e)
    return {"ms_kind": ms_kind(), "ms_name": ms_name(), "space": space, "language": pref_language(), "plex_backup_at": kv_get("plex_backup_at"), "plex_tidy_at": kv_get("plex_tidy_at"), "gotify": bool(notify_channels()), "notify": [k for k, _ in notify_channels()], "follow_days": FOLLOW_DAYS, "virus": virus, "running": STATE["running"], "step": STATE["step"], "progress": STATE["progress"], "issues": rows, "scans": scans,
            "scan_hour": sched()[0], "scan_minute": sched()[1], "scan_days": sched()[2], "keep_days": KEEP_DAYS, "stats": stats(), "score": library_score(), "trash": trash, "bins": bins, "trash_days": TRASH_DAYS, "events": events, "done": done,
            "configured": configured(), "version": APP_VERSION, "split_mounts": split_mounts(), "media_root": MEDIA_ROOT, "security_blocked": security_info()["blocked"],
            "plex_settings": "%s/desktop/#!/settings/server/%s/manage/optimized" % ((S.get("plex") or {}).get("public", "https://app.plex.tv").rstrip("/"), (S.get("plex") or {}).get("machine", ""))}

class H(BaseHTTPRequestHandler):
    server_version = "LibraryDoctor/2"
    def log_message(self, fmt, *a): pass
    def _peer(self): return self.client_address[0]
    def _ip(self):
        """The real visitor. X-Forwarded-For is only believed from LD_TRUSTED_PROXIES: anyone can send that header."""
        peer = self._peer()
        if not is_trusted_proxy(peer): return peer
        chain = [x.strip() for x in (self.headers.get("X-Forwarded-For") or "").split(",") if x.strip()]
        for x in reversed(chain):
            if not is_trusted_proxy(x): return x
        return chain[0] if chain else peer
    def _gate(self):
        """None when the request may go on, else the reason it's refused (wrong host name, or login off and not via the proxy)."""
        if not host_ok(self.headers.get("Host")):
            why = "the address %s isn't in LD_ALLOWED_HOSTS" % host_name(self.headers.get("Host"))[:80]
            note_blocked(self._peer(), why); return "Refused: " + why + "."
        if not AUTH_ON:
            peer = self._peer()
            if not (is_loopback(peer) or (is_trusted_proxy(peer) if TRUSTED_PROXIES else is_local(peer))):
                note_blocked(peer, "login is off and the request didn't come through %s" % ("LD_TRUSTED_PROXIES" if TRUSTED_PROXIES else "a local address"))
                return ("Refused: the login is switched off (LD_AUTH=off), so Library Doctor only answers %s. "
                        "Switch the login on, or put your proxy in LD_TRUSTED_PROXIES." % ("your proxy" if TRUSTED_PROXIES else "local network addresses"))
        return None
    def _tok(self):
        try: return SimpleCookie(self.headers.get("Cookie") or "")["ld_session"].value
        except Exception: return None
    def _cookie(self, tok, clear=False):
        secure = "; Secure" if is_trusted_proxy(self._peer()) and (self.headers.get("X-Forwarded-Proto") or "").lower() == "https" else ""
        return "ld_session=%s; Path=/; HttpOnly; SameSite=Strict%s; Max-Age=%d" % ("" if clear else tok, secure, 0 if clear else SESSION_DAYS * 86400)
    def authed(self):
        return not AUTH_ON or session_ok(self._tok())
    def send(self, code, body, ctype="application/json", cookie=None):
        b = body if isinstance(body, bytes) else json.dumps(ms_words_in(body)).encode()
        self.send_response(code)
        if cookie: self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; script-src 'self' 'unsafe-inline'; img-src 'self' data: https://i.ytimg.com; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        self.send_header("X-Frame-Options", "DENY")                 # v0.1.8: never shown inside another site's page (clickjacking)
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/healthz": return self.send(200, {"ok": True, "version": APP_VERSION})
        g = self._gate()
        if g: return self.send(403, g.encode(), "text/plain; charset=utf-8")
        if path == "/login" or (path in ("/", "/index.html") and not self.authed()):
            if path == "/login" and self.authed(): return self._redirect("/")
            with open(os.path.join(APP_DIR, "login.html"), "rb") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8")
        if path == "/api/auth": return self.send(200, dict({"on": AUTH_ON, "has_password": bool(auth_get()), "in": self.authed()}, **({"security": security_info()} if self.authed() else {})))
        if not self.authed(): return self.send(401, {"error": "Please log in.", "login": True})
        if path in ("/", "/index.html"):
            with open(os.path.join(APP_DIR, "index.html"), "rb") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8")
        if path == "/api/state": return self.send(200, state())
        if path == "/api/plex-open":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            rk = (q.get("rk") or [""])[0]
            if not rk.isdigit(): return self.send(400, {"error": "Bad link."})
            try: to = plex_open(rk, (q.get("i") or [None])[0], int((q.get("d") or ["0"])[0]) if (q.get("d") or ["0"])[0].isdigit() else None)
            except Exception as e: log("plex-open failed: %r" % e); to = plex_link(rk)["url"]
            if to: return self._redirect(to)
            return self.send(404, b"<!doctype html><meta charset=utf-8><title>Library Doctor</title><body style='font:16px system-ui;padding:24px'>"
                             b"<p>Plex doesn't have this item any more, and none of its files are in a Plex library now.</p>"
                             b"<p>Run <b>Scan now</b> in Library Doctor to bring the list up to date.</p>", "text/html; charset=utf-8")
        if path.startswith("/api/history"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self.send(200, history(int((q.get("days") or ["30"])[0]) if (q.get("days") or ["30"])[0].isdigit() else 30))
        if path == "/api/settings": return self.send(200, settings_public())
        if path == "/api/settings/export":                     # v0.1.9: without API keys (the passphrase version is a POST)
            body = json.dumps(settings_export(keys=False), indent=1).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Disposition", 'attachment; filename="library-doctor-settings-%s.json"' % datetime.now().strftime("%Y%m%d"))
            self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
        if path == "/api/analysis": return self.send(200, analysis())
        if path.startswith("/api/foldercheck"): return self.send(200, {"rows": folder_check(force="force=1" in self.path)})
        self.send(404, {"error": "not found"})
    def _redirect(self, to):
        self.send_response(302); self.send_header("Location", to); self.send_header("Content-Length", "0"); self.end_headers()
    def do_POST(self):
        g = self._gate()
        if g: return self.send(403, {"error": g, "msg": g})
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
                STATE.update(running=True, step="Starting", progress=0.0)    # v0.1.5: the page sees "running" straight away
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
                only = str(body["path"])[:4096] if body.get("path") else None
                res = [dict(queue_fix(str(i)[:32], how, only), id=i) for i in ids if i][:50]
                return self.send(200, {"ok": any(r.get("ok") for r in res), "errors": [r["error"] for r in res if not r.get("ok") and r.get("error")]})
            if path == "/api/appsfix":
                return self.send(200, apps_fix(str(body.get("id", ""))[:32], str(body.get("how", ""))))
            if path == "/api/plextidy":
                return self.send(200, plex_tidy())
            if path == "/api/plexbackup":
                return self.send(200, plex_backup())
            if path == "/api/queuerefresh":
                return self.send(200, {"ok": True, "cards": queue_refresh()})
            if path == "/api/gotifytest":
                return self.send(200, notify("Library Doctor: test", "This is a test from Library Doctor. After each nightly scan you get one message like this, but only when there's something new.", 4))
            if path == "/api/followup":
                threading.Thread(target=followup, daemon=True).start()
                return self.send(200, {"ok": True})
            if path == "/api/virusfull":
                return self.send(200, virus_full())
            if path == "/api/settings/import":
                return self.send(200, settings_import(body.get("data"), body.get("passphrase")))
            if path == "/api/settings/export":
                return self.send(200, settings_export_encrypted(body.get("passphrase")))
            if path == "/api/security/dismiss":                      # v0.1.10: hide the "refused a request" warning (the log keeps it)
                b = kv_get("security_blocked")
                with db() as c: c.execute("DELETE FROM kv WHERE k='security_blocked'")
                if b: event("security", "Dismissed the warning about a refused request from %s (%s)." % (b.get("ip", "?"), b.get("why", "")))
                return self.send(200, {"ok": True})
            if path == "/api/gapsearch":
                return self.send(200, gaps_fix(str(body.get("id", ""))[:32]))
            if path == "/api/plexsplit":
                return self.send(200, plex_split(str(body.get("id", ""))[:32]))
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
        log(config_write_hint(e))
        raise SystemExit(1)
    try:   # v0.1.15: a check that was running when Library Doctor stopped never finishes; free its card again
        with db() as c:
            n = c.execute("UPDATE done SET status='warn', note=? WHERE status='checking'",
                          ("Interrupted: Library Doctor restarted before this check finished. Scan again to see where things stand.",)).rowcount
        if n: log("startup: %d unfinished check%s marked as interrupted" % (n, "" if n == 1 else "s"))
    except Exception as e:
        log("startup: couldn't tidy unfinished checks: %r" % e)
    threading.Thread(target=nightly, daemon=True).start()
    apply_trash_days()
    log("Library Doctor listening on %s:%d (nightly scan at %02d:00, trash kept %d days)" % (LISTEN, PORT, SCAN_HOUR, TRASH_DAYS))
    ThreadingHTTPServer((LISTEN, PORT), H).serve_forever()
