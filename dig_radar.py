#!/usr/bin/env python3
"""
dig_radar.py  —  hunt records that your DJs are playing, that are cheap, available,
                 and under-collected on Discogs.

Pipeline
  init      write radar_config.json (fill in your keys + flagged DJs)
  harvest   pull candidate track IDs from flagged DJs:
              - YouTube: video descriptions (tracklists) + comment threads (ID answers)
              - SoundCloud: timed comments on a user's uploads  (official API, optional)
  import    paste a tracklist (1001Tracklists / MixesDB / NTS / Trackid.net / brizm) from a text file
  resolve   match candidates to Discogs, fetch price / copies-for-sale / want-have / linked YouTube videos,
            and snapshot the numbers so repeat runs show what's heating up
  report    rank everything and write radar_report.html + radar_targets.json
  wantlist  push "Watch" tier releases to your Discogs wantlist (Discogs then emails you when one is listed)
  notify    push new dig targets to your phone via ntfy (free Android app), with Discogs/YouTube buttons
  export-md write Obsidian notes (per-track, wikilinked to artist/label/style/DJ) + a daily log into your vault
  all       harvest -> resolve -> report -> notify
  selftest  run the ID extractor on sample comments (no keys needed)
  stats     quick counts from the local database

Only dependency:  pip install requests
"""

import argparse
import datetime as dt
import difflib
import html
import json
import math
import os
import re
import sqlite3
import sys
import time
from urllib.parse import quote_plus

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("Missing dependency. Run:  pip install requests")

VERSION = "0.2"
CONFIG_PATH = "radar_config.json"
SECRETS_PATH = "radar_secrets.json"
DB_PATH = "radar.db"
REPORT_HTML = "radar_report.html"
REPORT_JSON = "radar_targets.json"

DEFAULT_CONFIG = {
    "keys": {
        "youtube_api_key": "",
        "discogs_token": "",
        "discogs_username": "",
        "soundcloud_client_id": "",
        "soundcloud_client_secret": "",
        "anthropic_api_key": ""
    },
    "user_agent": "DigRadar/0.2 (personal record digging tool)",
    "notify": {
        "_help": "Install the ntfy app on Android, subscribe to a private-looking topic name, put it here.",
        "ntfy_server": "https://ntfy.sh",
        "ntfy_topic": "",
        "tiers": ["buy"],
        "max_individual": 6,
        "app_url": ""
    },
    "llm": {
        "_help": "Optional. Uses Claude to read the messy comments the regex can't, e.g. 'pretty sure thats the sultan remix of closer to me'.",
        "enabled": False,
        "model": "claude-haiku-4-5-20251001",
        "max_comments_per_video": 150,
        "batch_size": 60
    },
    "currency": "GBP",
    "sources": {
        "_help": "Paste channel URLs, @handles or UC... ids. Playlists/videos/tracks take full URLs.",
        "youtube_channels": [
            "https://www.youtube.com/@BoilerRoom"
        ],
        "youtube_playlists": [],
        "youtube_videos": [],
        "soundcloud_users": [],
        "soundcloud_tracks": []
    },
    "harvest": {
        "videos_per_channel": 8,
        "comments_per_video": 400,
        "tracks_per_soundcloud_user": 10,
        "reharvest_after_days": 7
    },
    "scoring": {
        "max_price": 15.0,
        "prefer_years": [1993, 2012],
        "style_boost": ["Progressive House", "Progressive Trance", "Trance", "Breaks", "Tech House", "Deep House", "Techno"],
        "style_boost_weight": 1.5,
        "prefer_format": "Vinyl",
        "min_confidence": 0.5
    }
}


# ----------------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------------

def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    try:
        return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except Exception:
        return None


def load_config():
    if not os.path.exists(CONFIG_PATH):
        sys.exit(f"No {CONFIG_PATH} found. Run:  python dig_radar.py init")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    # fill any missing sections with defaults so old configs keep working
    for section, vals in DEFAULT_CONFIG.items():
        if isinstance(vals, dict):
            cfg.setdefault(section, {})
            for k, v in vals.items():
                cfg[section].setdefault(k, v)
        else:
            cfg.setdefault(section, vals)
    # secrets can live in radar_secrets.json (git-ignored) so the committed config stays key-free
    if os.path.exists(SECRETS_PATH):
        with open(SECRETS_PATH, "r", encoding="utf-8") as f:
            sec = json.load(f)
        for section in ("keys", "notify"):
            for k, v in (sec.get(section) or {}).items():
                if v:
                    cfg[section][k] = v
    # ...or from the environment, e.g. DIGRADAR_DISCOGS_TOKEN (GitHub Actions secrets)
    for section in ("keys", "notify"):
        for k in list(cfg[section].keys()):
            env = os.environ.get("DIGRADAR_" + k.upper())
            if env:
                cfg[section][k] = env
    if os.environ.get("DIGRADAR_LLM_ENABLED", "").lower() in ("1", "true", "yes"):
        cfg["llm"]["enabled"] = True
    return cfg


def open_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS candidates (
            key TEXT PRIMARY KEY,
            artist TEXT, title TEXT, label_hint TEXT,
            mentions INTEGER DEFAULT 0,
            confidence REAL DEFAULT 0,
            first_seen TEXT, last_seen TEXT,
            resolved INTEGER DEFAULT 0,      -- 0 pending, 1 matched, -1 no match
            release_id INTEGER, master_id INTEGER, match_note TEXT
        );
        CREATE TABLE IF NOT EXISTS sightings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT, source_type TEXT, source_name TEXT, source_url TEXT,
            position TEXT, raw TEXT, confidence REAL, seen_at TEXT,
            UNIQUE(key, source_url, raw)
        );
        CREATE TABLE IF NOT EXISTS releases (
            release_id INTEGER PRIMARY KEY,
            master_id INTEGER, title TEXT, artists TEXT, year INTEGER, country TEXT,
            labels TEXT, formats TEXT, styles TEXT, genres TEXT, thumb TEXT,
            videos TEXT, fetched_at TEXT
        );
        CREATE TABLE IF NOT EXISTS snapshots (
            release_id INTEGER, taken_at TEXT,
            want INTEGER, have INTEGER, num_for_sale INTEGER, lowest_price REAL, currency TEXT,
            yt_views INTEGER,
            PRIMARY KEY (release_id, taken_at)
        );
        CREATE TABLE IF NOT EXISTS seen_sources (
            source_url TEXT PRIMARY KEY, source_name TEXT, harvested_at TEXT
        );
        CREATE TABLE IF NOT EXISTS notified (
            key TEXT, tier TEXT, notified_at TEXT, PRIMARY KEY (key, tier)
        );
        """
    )
    return db


def log(msg):
    print(msg, flush=True)


# ----------------------------------------------------------------------------
# ID extraction  (the heart of the harvester)
# ----------------------------------------------------------------------------

TIMESTAMP_RE = re.compile(r"\(?\[?(?<![\d:])(?:\d{1,2}:)?\d{1,2}:\d{2}(?![\d:])\]?\)?")
LEADIN_RE = re.compile(
    r"^(?:"
    r"(?:the\s+)?(?:best|favou?rite|biggest)?\s*(?:track|tune|song|id|one|record|banger|tune\s+id|track\s+id)\s*"
    r"(?:of\s+the\s+(?:set|mix|night)|here)?\s*(?:at|@|around|from|on|is|=|:|->)?\s*(?:is|=|:|->)?\s*"
    r"|(?:it'?s|it\s+is|this\s+is|that'?s|thats|its|i\s+think\s+it'?s|pretty\s+sure\s+it'?s|i\s+believe\s+it'?s)\s+"
    r"|(?:\d{1,3}[.)]\s+)"
    r"|(?:[-–—•*]\s+)"
    r"|(?:@\S+\s+)"
    r")+",
    re.I,
)
SPLIT_RE = re.compile(r"\s+[-‒−]\s+|\s*[–—]\s*")
MASHUP_RE = re.compile(r"\s+w/\s+", re.I)
EMOJI_RE = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B50\u2B06\u2B07\uFE0F\u200D]+")
CHATTER_RE = re.compile(
    r"\s+(?:is|was|are|were)\s+((?:such|so|a|an|the|my|one\s+of|still|absolutely|just|what|from|out|on|pure|unreal|massive|huge)\b[^A-Z]*)$"
)
JUNK_ARTIST_RE = re.compile(
    r"\b(http|www\.|subscribe|thanks?|thank you|love|best|amazing|please|pls|anyone|does anyone|"
    r"what is|what's|whats|who is|whos|goosebumps|fire|insane|banger|sick|mix|set|tracklist|timestamps?)\b",
    re.I,
)
UNKNOWN_RE = re.compile(r"^\s*(id|ids|unknown|unreleased|untitled|\?+|tba|n/?a)\s*$", re.I)
ASKS_ID_RE = re.compile(
    r"\b(id\??|track\s*id|tune\s*id|what('?s| is) (this|the|that) (track|tune|song|one)|"
    r"anyone know|does anyone know|name of (this|the) (track|tune|song)|"
    r"track at|tune at|song at)\b",
    re.I,
)
LABEL_RE = re.compile(r"\s*\[([^\]]{2,40})\]\s*$")


def clean_side(s):
    s = EMOJI_RE.sub("", s)
    s = s.strip().strip("\"'“”‘’`*_ ")
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"^(?:by|from)\s+", "", s, flags=re.I)
    m = CHATTER_RE.search(s)          # "Xpander is such a tune" -> "Xpander"; leaves "We Are The Mammoths" alone
    if m and len(m.group(1).split()) >= 3 and m.start() >= 2:
        s = s[: m.start()]
    s = s.strip(" .,:;!-–—")
    return s


def plausible(artist, title):
    if not (2 <= len(artist) <= 80 and 2 <= len(title) <= 120):
        return False
    if len(artist.split()) > 8 or len(title.split()) > 14:
        return False
    if not re.search(r"[A-Za-z]", artist) or not re.search(r"[A-Za-z0-9]", title):
        return False
    if UNKNOWN_RE.match(artist) or UNKNOWN_RE.match(title):
        return False
    if JUNK_ARTIST_RE.search(artist):
        return False
    if artist.endswith("?"):
        return False
    return True


def extract_ids(text):
    """Return a list of dicts {artist, title, label, position, raw, has_ts} found in a block of text."""
    found = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or len(line) > 300:
            continue
        low = line.lower()
        if "http" in low or "www." in low:
            continue
        pos = None
        m = TIMESTAMP_RE.search(line)
        if m:
            pos = m.group(0).strip("[]() ")
            line = (line[: m.start()] + " " + line[m.end():]).strip()
        line = LEADIN_RE.sub("", line).strip(" :-–—=>.,")
        for part in MASHUP_RE.split(line):
            part = part.strip()
            if not part:
                continue
            if "\t" in part:
                a, t = part.split("\t", 1)
            else:
                pieces = SPLIT_RE.split(part, maxsplit=1)
                if len(pieces) != 2:
                    continue
                a, t = pieces
            label = None
            lm = LABEL_RE.search(t)
            if lm:
                label = lm.group(1).strip()
                t = t[: lm.start()]
            a, t = clean_side(a), clean_side(t)
            if not plausible(a, t):
                continue
            found.append({"artist": a, "title": t, "label": label, "position": pos,
                          "raw": raw_line.strip()[:200], "has_ts": pos is not None})
    return found


def norm_key(artist, title):
    def n(s):
        s = s.lower()
        s = re.sub(r"\((original mix|original|extended mix|extended|extended version)\)", "", s)
        s = re.sub(r"\b(feat\.?|ft\.?|featuring)\b", "feat", s)
        s = re.sub(r"[^a-z0-9]+", " ", s).strip()
        return s
    return f"{n(artist)}|{n(title)}"


def add_sighting(db, hit, conf, source_type, source_name, source_url):
    key = norm_key(hit["artist"], hit["title"])
    ts = now_iso()
    row = db.execute("SELECT mentions FROM candidates WHERE key=?", (key,)).fetchone()
    try:
        db.execute(
            "INSERT INTO sightings(key, source_type, source_name, source_url, position, raw, confidence, seen_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (key, source_type, source_name, source_url, hit.get("position"), hit["raw"], conf, ts),
        )
        new = True
    except sqlite3.IntegrityError:
        new = False
    if row is None:
        db.execute(
            "INSERT INTO candidates(key, artist, title, label_hint, mentions, confidence, first_seen, last_seen)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (key, hit["artist"], hit["title"], hit.get("label"), 1, conf, ts, ts),
        )
    elif new:
        db.execute(
            "UPDATE candidates SET mentions=mentions+1, confidence=MAX(confidence,?), last_seen=?,"
            " label_hint=COALESCE(label_hint,?) WHERE key=?",
            (conf, ts, hit.get("label"), key),
        )
    return new


def source_is_fresh(db, url, days):
    row = db.execute("SELECT harvested_at FROM seen_sources WHERE source_url=?", (url,)).fetchone()
    if not row:
        return False
    t = parse_iso(row["harvested_at"])
    return bool(t and (dt.datetime.now(dt.timezone.utc) - t).days < days)


def mark_source(db, url, name):
    db.execute("INSERT OR REPLACE INTO seen_sources(source_url, source_name, harvested_at) VALUES (?,?,?)",
               (url, name, now_iso()))



# ----------------------------------------------------------------------------
# Optional LLM extractor (Claude) for the comments the regex can't read
# ----------------------------------------------------------------------------

LLM_PROMPT = """You extract DJ track IDs from comments posted under a DJ set or in a music forum thread.
Context: {context}

Return ONLY a JSON array, no prose, no code fences. Each item:
{{"i": <comment index>, "artist": "<artist>", "title": "<title incl. remix/version if stated>", "position": "<h:mm:ss or mm:ss from the comment or the question it answers, else null>", "confidence": <0.0-1.0>}}

Rules:
- Only include tracks where an actual artist AND title are named or clearly implied (e.g. "the Sultan remix of Closer To Me" -> artist "Chab", title "Closer To Me (Sultan & The Greek Remix)" only if you are confident of the artist; otherwise skip).
- Skip bare requests ("ID?", "what's the tune at 12:00") that have no answer.
- Skip jokes, unrelated chatter, and mentions of the DJ's own name as a track.
- If a comment answers an earlier "ID at 34:12?" question, use that timestamp.
- Confidence 0.9 for explicit "Artist - Title", lower for hedged or partial answers.
- Return [] if nothing qualifies.

Comments:
{comments}"""


def llm_extract(cfg, texts, context):
    """Send a batch of comment strings to Claude; return hits shaped like extract_ids() output."""
    key = cfg["keys"].get("anthropic_api_key")
    if not key or not texts:
        return []
    numbered = "\n".join(f"[{i}] {t.replace(chr(10), ' ')[:400]}" for i, t in enumerate(texts))
    body = {
        "model": cfg["llm"].get("model", "claude-haiku-4-5-20251001"),
        "max_tokens": 3000,
        "messages": [{"role": "user", "content": LLM_PROMPT.format(context=context, comments=numbered)}],
    }
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json=body, timeout=120,
        )
        if r.status_code != 200:
            log(f"LLM: API error {r.status_code}: {r.text[:200]}")
            return []
        text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
        text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
        start, end = text.find("["), text.rfind("]")
        items = json.loads(text[start:end + 1]) if start >= 0 and end > start else []
    except Exception as e:
        log(f"LLM: could not parse response ({e})")
        return []
    hits = []
    for it in items:
        try:
            i = int(it.get("i"))
            a, t = clean_side(str(it.get("artist", ""))), clean_side(str(it.get("title", "")))
        except Exception:
            continue
        if not plausible(a, t) or not (0 <= i < len(texts)):
            continue
        pos = it.get("position") or None
        hits.append({"artist": a, "title": t, "label": None, "position": pos, "raw": texts[i][:200],
                     "has_ts": bool(pos), "llm_conf": float(it.get("confidence") or 0.6)})
    return hits


def llm_leftovers(cfg, texts, context, source_type, source_name, source_url, db):
    """Run the LLM over comments the regex found nothing in. Returns number of new sightings."""
    if not cfg["llm"].get("enabled") or not cfg["keys"].get("anthropic_api_key"):
        return 0
    cand = [t for t in texts if 12 <= len(t) <= 600 and not ("http" in t.lower())]
    cand = cand[: cfg["llm"].get("max_comments_per_video", 150)]
    new = 0
    bs = max(10, cfg["llm"].get("batch_size", 60))
    for i in range(0, len(cand), bs):
        for h in llm_extract(cfg, cand[i:i + bs], context):
            conf = min(0.85, 0.5 + 0.4 * h.get("llm_conf", 0.6))
            new += add_sighting(db, h, conf, source_type, source_name, source_url)
    return new

# ----------------------------------------------------------------------------
# YouTube (official Data API v3 — free key, 10,000 units/day; a comment page costs 1 unit)
# ----------------------------------------------------------------------------

class QuotaExceeded(Exception):
    pass


class YouTube:
    BASE = "https://www.googleapis.com/youtube/v3/"

    def __init__(self, key, ua):
        self.key = key
        self.s = requests.Session()
        self.s.headers["User-Agent"] = ua

    def get(self, endpoint, **params):
        params["key"] = self.key
        r = self.s.get(self.BASE + endpoint, params=params, timeout=30)
        if r.status_code in (400, 403, 404):
            try:
                reason = r.json()["error"]["errors"][0]["reason"]
            except Exception:
                reason = str(r.status_code)
            if reason == "quotaExceeded":
                raise QuotaExceeded()
            if reason in ("commentsDisabled", "videoNotFound", "playlistNotFound", "forbidden", "notFound"):
                return None
            raise RuntimeError(f"YouTube API error {r.status_code}: {reason} — {r.text[:200]}")
        r.raise_for_status()
        return r.json()

    @staticmethod
    def parse_channel_ref(ref):
        ref = ref.strip()
        m = re.search(r"youtube\.com/(?:channel/)?(UC[\w-]{20,})", ref)
        if m:
            return "id", m.group(1)
        m = re.search(r"youtube\.com/@([\w.\-]+)", ref)
        if m:
            return "handle", m.group(1)
        if ref.startswith("@"):
            return "handle", ref[1:]
        if ref.startswith("UC") and len(ref) >= 22 and " " not in ref:
            return "id", ref
        m = re.search(r"youtube\.com/(?:c|user)/([\w.\-]+)", ref)
        if m:
            return "user", m.group(1)
        return "handle", ref

    @staticmethod
    def video_id(url):
        m = re.search(r"(?:v=|youtu\.be/|/live/|/shorts/|/embed/)([\w-]{11})", url)
        return m.group(1) if m else (url if re.fullmatch(r"[\w-]{11}", url) else None)

    @staticmethod
    def playlist_id(url):
        m = re.search(r"list=([\w-]+)", url)
        return m.group(1) if m else url

    def channel_uploads(self, ref):
        kind, val = self.parse_channel_ref(ref)
        params = {"part": "contentDetails,snippet"}
        if kind == "id":
            params["id"] = val
        elif kind == "user":
            params["forUsername"] = val
        else:
            params["forHandle"] = "@" + val
        data = self.get("channels", **params)
        items = (data or {}).get("items") or []
        if not items:
            return None, None
        ch = items[0]
        return ch["snippet"]["title"], ch["contentDetails"]["relatedPlaylists"]["uploads"]

    def playlist_video_ids(self, playlist_id, limit):
        ids, token = [], None
        while len(ids) < limit:
            data = self.get("playlistItems", part="contentDetails", playlistId=playlist_id,
                            maxResults=min(50, limit - len(ids)), pageToken=token)
            if not data:
                break
            ids += [it["contentDetails"]["videoId"] for it in data.get("items", [])]
            token = data.get("nextPageToken")
            if not token:
                break
        return ids[:limit]

    def videos(self, ids):
        out = []
        for i in range(0, len(ids), 50):
            data = self.get("videos", part="snippet,statistics", id=",".join(ids[i:i + 50]))
            out += (data or {}).get("items", [])
        return out

    def comment_threads(self, video_id, limit):
        """Yield (text, is_reply, parent_text). Replies up to 5 per thread come inline."""
        got, token = 0, None
        while got < limit:
            data = self.get("commentThreads", part="snippet,replies", videoId=video_id,
                            maxResults=100, order="relevance", textFormat="plainText", pageToken=token)
            if not data:
                return
            for th in data.get("items", []):
                top = th["snippet"]["topLevelComment"]["snippet"]["textDisplay"]
                yield top, False, None
                got += 1
                for rep in (th.get("replies") or {}).get("comments", []):
                    yield rep["snippet"]["textDisplay"], True, top
                    got += 1
            token = data.get("nextPageToken")
            if not token:
                return


def harvest_youtube(db, cfg):
    key = cfg["keys"].get("youtube_api_key")
    if not key:
        log("YouTube: no youtube_api_key in config — skipping (see README for the free key).")
        return
    yt = YouTube(key, cfg["user_agent"])
    hv = cfg["harvest"]
    src = cfg["sources"]
    fresh_days = hv.get("reharvest_after_days", 7)

    # 1) collect video ids from channels, playlists and single videos
    video_ids = []
    for ref in src.get("youtube_channels", []):
        try:
            name, uploads = yt.channel_uploads(ref)
        except QuotaExceeded:
            log("YouTube: daily quota exceeded — try again tomorrow.")
            return
        if not uploads:
            log(f"YouTube: could not find channel for '{ref}'")
            continue
        ids = yt.playlist_video_ids(uploads, hv.get("videos_per_channel", 8))
        log(f"YouTube: {name} — {len(ids)} recent uploads")
        video_ids += ids
    for pl in src.get("youtube_playlists", []):
        ids = yt.playlist_video_ids(yt.playlist_id(pl), hv.get("videos_per_channel", 8))
        log(f"YouTube: playlist {pl} — {len(ids)} videos")
        video_ids += ids
    for v in src.get("youtube_videos", []):
        vid = yt.video_id(v)
        if vid:
            video_ids.append(vid)
    # de-dupe, keep order
    seen = set()
    video_ids = [v for v in video_ids if not (v in seen or seen.add(v))]
    todo = [v for v in video_ids if not source_is_fresh(db, f"https://www.youtube.com/watch?v={v}", fresh_days)]
    log(f"YouTube: {len(todo)} videos to harvest ({len(video_ids) - len(todo)} harvested recently, skipped)")
    if not todo:
        return

    # 2) descriptions + stats in batches of 50 (1 unit per batch)
    try:
        metas = yt.videos(todo)
    except QuotaExceeded:
        log("YouTube: daily quota exceeded — try again tomorrow.")
        return
    total_new = 0
    for meta in metas:
        vid = meta["id"]
        url = f"https://www.youtube.com/watch?v={vid}"
        title = meta["snippet"]["title"]
        chan = meta["snippet"].get("channelTitle", "")
        name = f"{chan}: {title}"[:120]
        desc = meta["snippet"].get("description", "")
        new_here = 0
        hits = extract_ids(desc)
        listy = sum(1 for h in hits) >= 3
        for h in hits:
            conf = 0.9 if (h["has_ts"] or listy) else 0.6
            new_here += add_sighting(db, h, conf, "yt_description", name, url)
        # 3) comments
        n_comments, leftovers = 0, []
        try:
            for text, is_reply, parent in yt.comment_threads(vid, hv.get("comments_per_video", 400)):
                n_comments += 1
                answering = bool(is_reply and parent and ASKS_ID_RE.search(parent))
                parent_ts = TIMESTAMP_RE.search(parent) if answering else None
                hits = extract_ids(text)
                if not hits:
                    # keep the reply together with the question it answers so the LLM sees the timestamp
                    leftovers.append((f"(replying to: {parent[:120]}) " if answering else "") + text)
                for h in hits:
                    if answering and not h["position"] and parent_ts:
                        h["position"] = parent_ts.group(0).strip("[]() ")  # "ID at 34:12?" -> answer inherits 34:12
                    conf = 0.5 + (0.2 if h["has_ts"] else 0) + (0.15 if answering else 0)
                    if not h["has_ts"] and not answering and ASKS_ID_RE.search(text):
                        conf += 0.1  # "ID at 34:12 is Artist - Title" style comment
                    new_here += add_sighting(db, h, min(conf, 0.9), "yt_comment", name, url)
            new_here += llm_leftovers(cfg, leftovers, f"YouTube set: {name}", "yt_comment_llm", name, url, db)
        except QuotaExceeded:
            db.commit()
            log("YouTube: daily quota exceeded mid-way — progress saved, run again tomorrow.")
            return
        mark_source(db, url, name)
        db.commit()
        total_new += new_here
        log(f"  {name[:70]}  → {n_comments} comments read, {new_here} new sightings")
    log(f"YouTube: done. {total_new} new sightings.")


# ----------------------------------------------------------------------------
# SoundCloud (official API; needs an app registered at developers.soundcloud.com)
# ----------------------------------------------------------------------------

class SoundCloud:
    BASE = "https://api.soundcloud.com"

    def __init__(self, client_id, client_secret, ua):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = ua
        self.s.headers["Accept"] = "application/json; charset=utf-8"
        r = self.s.post("https://secure.soundcloud.com/oauth/token",
                        data={"grant_type": "client_credentials"},
                        auth=(client_id, client_secret), timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"SoundCloud auth failed ({r.status_code}): {r.text[:200]}")
        self.s.headers["Authorization"] = "OAuth " + r.json()["access_token"]

    def get(self, path, **params):
        url = path if path.startswith("http") else self.BASE + path
        r = self.s.get(url, params=params, timeout=30)
        if r.status_code == 404:
            return None
        if r.status_code == 429:
            time.sleep(20)
            r = self.s.get(url, params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    @staticmethod
    def ident(obj):
        return obj.get("urn") or obj.get("id")

    def resolve(self, url):
        return self.get("/resolve", url=url)

    def user_tracks(self, user, limit):
        data = self.get(f"/users/{self.ident(user)}/tracks", limit=min(limit, 50), linked_partitioning="true")
        coll = data.get("collection", data) if isinstance(data, dict) else data
        return (coll or [])[:limit]

    def comments(self, track, limit=400):
        out, url, params = [], f"/tracks/{self.ident(track)}/comments", {"limit": 200, "linked_partitioning": "true"}
        while url and len(out) < limit:
            data = self.get(url, **params)
            if not data:
                break
            coll = data.get("collection", data) if isinstance(data, dict) else data
            out += coll or []
            url, params = (data.get("next_href") if isinstance(data, dict) else None), {}
        return out[:limit]


def harvest_soundcloud(db, cfg):
    cid, sec = cfg["keys"].get("soundcloud_client_id"), cfg["keys"].get("soundcloud_client_secret")
    src = cfg["sources"]
    if not (src.get("soundcloud_users") or src.get("soundcloud_tracks")):
        return
    if not (cid and sec):
        log("SoundCloud: sources listed but no client id/secret in config — skipping.")
        return
    try:
        sc = SoundCloud(cid, sec, cfg["user_agent"])
    except Exception as e:
        log(f"SoundCloud: {e}")
        return
    hv = cfg["harvest"]
    tracks = []
    for u in src.get("soundcloud_users", []):
        user = sc.resolve(u)
        if not user:
            log(f"SoundCloud: could not resolve {u}")
            continue
        got = sc.user_tracks(user, hv.get("tracks_per_soundcloud_user", 10))
        log(f"SoundCloud: {user.get('username')} — {len(got)} uploads")
        tracks += got
    for t in src.get("soundcloud_tracks", []):
        tr = sc.resolve(t)
        if tr:
            tracks.append(tr)
    total = 0
    for tr in tracks:
        url = tr.get("permalink_url") or f"soundcloud:{sc.ident(tr)}"
        if source_is_fresh(db, url, hv.get("reharvest_after_days", 7)):
            continue
        name = f"{(tr.get('user') or {}).get('username', 'SoundCloud')}: {tr.get('title', '')}"[:120]
        new_here = 0
        for h in extract_ids(tr.get("description") or ""):
            new_here += add_sighting(db, h, 0.85 if h["has_ts"] else 0.6, "sc_description", name, url)
        comments = sc.comments(tr)
        for c in comments:
            body = c.get("body") or ""
            ms = c.get("timestamp")
            for h in extract_ids(body):
                if ms and not h["position"]:
                    h["position"] = f"{int(ms) // 60000}:{(int(ms) // 1000) % 60:02d}"
                    h["has_ts"] = True
                new_here += add_sighting(db, h, 0.7, "sc_comment", name, url)
        mark_source(db, url, name)
        db.commit()
        total += new_here
        log(f"  {name[:70]}  → {len(comments)} comments, {new_here} new sightings")
    log(f"SoundCloud: done. {total} new sightings.")


# ----------------------------------------------------------------------------
# Paste import (1001Tracklists / MixesDB / NTS / Trackid.net / brizm / your own notes)
# ----------------------------------------------------------------------------

def import_file(db, path, source_name, source_url, confidence, use_llm=False, cfg=None):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    hits = extract_ids(text)
    src_url = source_url or f"import:{source_name}"
    new = sum(add_sighting(db, h, confidence, "import", source_name, src_url) for h in hits)
    if use_llm and cfg:
        # feed paragraphs the regex could not read (Reddit threads, forum posts, messy notes) to Claude
        got = {h["raw"] for h in hits}
        paras = [pp.strip() for pp in re.split(r"\n\s*\n|\n", text) if pp.strip() and pp.strip()[:200] not in got]
        llm_cfg = dict(cfg); llm_cfg["llm"] = dict(cfg["llm"], enabled=True, max_comments_per_video=400)
        new += llm_leftovers(llm_cfg, paras, f"pasted text: {source_name}", "import_llm", source_name, src_url, db)
    mark_source(db, src_url, source_name)
    db.commit()
    log(f"Imported {path}: {len(hits)} lines parsed, {new} new sightings under '{source_name}'.")


# ----------------------------------------------------------------------------
# Discogs (official API — token from discogs.com/settings/developers, 60 requests/min)
# ----------------------------------------------------------------------------

class Discogs:
    BASE = "https://api.discogs.com"

    def __init__(self, token, ua, currency):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = ua
        if token:
            self.s.headers["Authorization"] = f"Discogs token={token}"
        self.currency = currency
        self.calls = 0

    def get(self, path, **params):
        for attempt in range(4):
            r = self.s.get(self.BASE + path, params=params, timeout=30)
            self.calls += 1
            time.sleep(1.05)  # stay under 60/min
            if r.status_code == 429:
                time.sleep(15 * (attempt + 1))
                continue
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        return None

    def search(self, **params):
        data = self.get("/database/search", type="release", per_page=8, **params)
        return (data or {}).get("results", [])

    def release(self, rid):
        return self.get(f"/releases/{rid}", curr_abbr=self.currency)

    def add_want(self, username, rid):
        r = self.s.put(f"{self.BASE}/users/{username}/wants/{rid}", timeout=30)
        time.sleep(1.05)
        return r.status_code in (200, 201)


def _norm_text(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def base_title(title):
    return re.sub(r"\s*[\(\[].*?[\)\]]\s*", " ", title).strip() or title


def pick_release(results, artist, prefer_format):
    scored = []
    a_n = _norm_text(artist)
    for r in results:
        rtitle = r.get("title", "")
        r_artist = rtitle.split(" - ", 1)[0]
        sim = difflib.SequenceMatcher(None, _norm_text(r_artist), a_n).ratio()
        various = r_artist.strip().lower().startswith("various")
        if sim < 0.45 and not various:
            continue
        fmt = " ".join(r.get("format") or [])
        pref = 0 if prefer_format.lower() in fmt.lower() else 1
        y = str(r.get("year") or "")
        year = int(y) if y.isdigit() else 9999
        scored.append(((1 if various else 0), pref, year, -sim, r))
    if not scored:
        return None
    scored.sort(key=lambda x: x[:4])
    return scored[0][4]


def resolve_candidates(db, cfg, limit, retry_failed=False):
    token = cfg["keys"].get("discogs_token")
    if not token:
        log("Discogs: no discogs_token in config — resolving without a token is heavily rate-limited; see README.")
    dc = Discogs(token, cfg["user_agent"], cfg.get("currency", "GBP"))
    yt = YouTube(cfg["keys"]["youtube_api_key"], cfg["user_agent"]) if cfg["keys"].get("youtube_api_key") else None
    sc = cfg["scoring"]
    where = "resolved=0" if not retry_failed else "resolved<=0"
    rows = db.execute(
        f"SELECT * FROM candidates WHERE {where} AND confidence>=? ORDER BY mentions DESC, confidence DESC LIMIT ?",
        (sc.get("min_confidence", 0.5), limit),
    ).fetchall()
    log(f"Discogs: resolving {len(rows)} candidates (≈{len(rows) * 2} API calls, ~1s each)...")
    matched = 0
    for c in rows:
        artist, title = c["artist"], c["title"]
        note = "track match"
        results = dc.search(artist=artist, track=title)
        if not results and base_title(title) != title:
            results = dc.search(artist=artist, track=base_title(title))
            note = "matched base title (remix/version may differ)"
        if not results:
            results = dc.search(q=f"{artist} {base_title(title)}")
            note = "loose text match — check it"
        rel = pick_release(results, artist, sc.get("prefer_format", "Vinyl")) if results else None
        if not rel:
            db.execute("UPDATE candidates SET resolved=-1, match_note='no Discogs match' WHERE key=?", (c["key"],))
            db.commit()
            log(f"  ✗ {artist} - {title}")
            continue
        rid = rel["id"]
        full = dc.release(rid) or {}
        store_release(db, rid, rel, full)
        yt_views = None
        vids = [v.get("uri") for v in full.get("videos") or [] if v.get("uri")]
        if yt and vids:
            ids = [YouTube.video_id(u) for u in vids]
            ids = [i for i in ids if i]
            try:
                metas = yt.videos(ids[:50])
                yt_views = sum(int((m.get("statistics") or {}).get("viewCount", 0)) for m in metas)
            except QuotaExceeded:
                yt = None
            except Exception:
                pass
        snapshot(db, rid, full, rel, cfg.get("currency", "GBP"), yt_views)
        db.execute(
            "UPDATE candidates SET resolved=1, release_id=?, master_id=?, match_note=? WHERE key=?",
            (rid, full.get("master_id") or rel.get("master_id"), note, c["key"]),
        )
        db.commit()
        matched += 1
        lp = full.get("lowest_price")
        log(f"  ✓ {artist} - {title}  →  {rel.get('title')} ({rel.get('year')}) "
            f"{'£' if cfg.get('currency') == 'GBP' else ''}{lp if lp is not None else '—'} · {full.get('num_for_sale', 0)} for sale")
    log(f"Discogs: {matched}/{len(rows)} matched, {dc.calls} API calls.")


def store_release(db, rid, rel, full):
    labels = ", ".join(sorted({l.get("name", "") for l in full.get("labels", [])})) or ", ".join(rel.get("label") or [])
    formats = ", ".join(
        sorted({(f.get("name", "") + " " + " ".join(f.get("descriptions") or [])).strip() for f in full.get("formats", [])})
    ) or ", ".join(rel.get("format") or [])
    artists = ", ".join(a.get("name", "") for a in full.get("artists", [])) or rel.get("title", "").split(" - ")[0]
    db.execute(
        "INSERT OR REPLACE INTO releases(release_id, master_id, title, artists, year, country, labels, formats,"
        " styles, genres, thumb, videos, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            rid, full.get("master_id") or rel.get("master_id"), full.get("title") or rel.get("title"), artists,
            full.get("year") or (int(rel["year"]) if str(rel.get("year", "")).isdigit() else None),
            full.get("country") or rel.get("country"), labels, formats,
            ", ".join(full.get("styles") or rel.get("style") or []),
            ", ".join(full.get("genres") or rel.get("genre") or []),
            full.get("thumb") or rel.get("thumb"),
            json.dumps([v.get("uri") for v in full.get("videos") or [] if v.get("uri")]),
            now_iso(),
        ),
    )


def snapshot(db, rid, full, rel, currency, yt_views):
    comm = full.get("community") or rel.get("community") or {}
    db.execute(
        "INSERT OR REPLACE INTO snapshots(release_id, taken_at, want, have, num_for_sale, lowest_price, currency, yt_views)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (rid, now_iso(), comm.get("want"), comm.get("have"), full.get("num_for_sale"),
         full.get("lowest_price"), currency, yt_views),
    )


def refresh_snapshots(db, cfg, limit):
    """Re-fetch market numbers for already-matched releases (this is what makes 'trend' work)."""
    dc = Discogs(cfg["keys"].get("discogs_token"), cfg["user_agent"], cfg.get("currency", "GBP"))
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = db.execute(
        "SELECT r.release_id FROM releases r WHERE NOT EXISTS ("
        " SELECT 1 FROM snapshots s WHERE s.release_id=r.release_id AND s.taken_at>?) LIMIT ?",
        (cutoff, limit),
    ).fetchall()
    if not rows:
        return
    log(f"Discogs: refreshing market numbers for {len(rows)} known releases...")
    for r in rows:
        full = dc.release(r["release_id"])
        if full:
            snapshot(db, r["release_id"], full, {}, cfg.get("currency", "GBP"), None)
    db.commit()


# ----------------------------------------------------------------------------
# Scoring + report
# ----------------------------------------------------------------------------

def gather(db, cfg):
    sc = cfg["scoring"]
    lo, hi = sc.get("prefer_years", [1993, 2012])
    boost = {s.lower() for s in sc.get("style_boost", [])}
    out = []
    cands = db.execute("SELECT * FROM candidates WHERE confidence>=? ORDER BY mentions DESC",
                       (sc.get("min_confidence", 0.5),)).fetchall()
    for c in cands:
        sightings = db.execute(
            "SELECT source_type, source_name, source_url, position, confidence FROM sightings WHERE key=?"
            " ORDER BY confidence DESC, seen_at DESC LIMIT 8", (c["key"],)).fetchall()
        n_sources = db.execute("SELECT COUNT(DISTINCT source_url) AS n FROM sightings WHERE key=?",
                               (c["key"],)).fetchone()["n"]
        item = {
            "artist": c["artist"], "title": c["title"], "label_hint": c["label_hint"],
            "mentions": c["mentions"], "sources": n_sources, "confidence": round(c["confidence"], 2),
            "resolved": c["resolved"], "match_note": c["match_note"],
            "sightings": [dict(s) for s in sightings],
        }
        if c["resolved"] == 1 and c["release_id"]:
            rel = db.execute("SELECT * FROM releases WHERE release_id=?", (c["release_id"],)).fetchone()
            latest = db.execute("SELECT * FROM snapshots WHERE release_id=? ORDER BY taken_at DESC LIMIT 1",
                                (c["release_id"],)).fetchone()
            first = db.execute("SELECT want, taken_at FROM snapshots WHERE release_id=? ORDER BY taken_at ASC LIMIT 1",
                               (c["release_id"],)).fetchone()
            if rel:
                item["release"] = dict(rel)
                item["release"]["videos"] = json.loads(rel["videos"] or "[]")
            if latest:
                item["market"] = dict(latest)
                if first and first["want"] is not None and latest["want"] is not None and first["taken_at"] != latest["taken_at"]:
                    d0, d1 = parse_iso(first["taken_at"]), parse_iso(latest["taken_at"])
                    item["trend"] = {"want_delta": latest["want"] - first["want"],
                                     "days": max(1, (d1 - d0).days) if d0 and d1 else None}
            # score
            want = (latest["want"] if latest and latest["want"] is not None else 0)
            views = (latest["yt_views"] if latest and latest["yt_views"] else 0)
            signal = 1 + math.log1p(c["mentions"]) + 0.6 * math.log1p(n_sources) + 0.4 * math.log1p(views / 1000.0)
            obscurity = 1.0 / (1.0 + math.log1p(want))
            styles = {s.strip().lower() for s in (rel["styles"] or "").split(",")} if rel else set()
            style_mult = sc.get("style_boost_weight", 1.5) if styles & boost else 1.0
            year = rel["year"] if rel and rel["year"] else None
            year_mult = 1.2 if (year and lo <= year <= hi) else 1.0
            trend_mult = 1.0 + min(0.5, (item.get("trend", {}).get("want_delta", 0) or 0) / 20.0)
            item["gem_score"] = round(signal * obscurity * style_mult * year_mult * trend_mult, 3)
            price = latest["lowest_price"] if latest else None
            for_sale = latest["num_for_sale"] if latest else None
            if for_sale and price is not None and price <= sc.get("max_price", 15):
                item["tier"] = "buy"
            elif for_sale:
                item["tier"] = "pricey"
            else:
                item["tier"] = "watch"
        else:
            item["tier"] = "unresolved" if c["resolved"] == -1 else "pending"
            item["gem_score"] = round(1 + math.log1p(c["mentions"]) + 0.6 * math.log1p(n_sources), 3)
        out.append(item)
    order = {"buy": 0, "pricey": 1, "watch": 2, "unresolved": 3, "pending": 4}
    out.sort(key=lambda x: (order.get(x["tier"], 9), -x["gem_score"], -x["mentions"]))
    return out


def fmt_money(v, cur):
    if v is None:
        return "—"
    sym = {"GBP": "£", "USD": "$", "EUR": "€"}.get(cur, cur + " ")
    return f"{sym}{v:,.2f}"


def links_for(item):
    a, t = item["artist"], item["title"]
    q = quote_plus(f"{a} {base_title(t)}")
    rel = item.get("release")
    links = []
    if rel:
        links.append(("Discogs", f"https://www.discogs.com/release/{rel['release_id']}"))
        links.append(("For sale", f"https://www.discogs.com/sell/release/{rel['release_id']}?sort=price%2Casc"))
        if rel.get("videos"):
            links.append(("YouTube", rel["videos"][0]))
    else:
        links.append(("Discogs search", f"https://www.discogs.com/search/?q={q}&type=all"))
        links.append(("YouTube", f"https://www.youtube.com/results?search_query={q}"))
    links.append(("Bandcamp", f"https://bandcamp.com/search?q={q}&item_type=t"))
    links.append(("Juno", f"https://www.juno.co.uk/search/?q%5Ball%5D%5B%5D={q}"))
    links.append(("eBay UK", f"https://www.ebay.co.uk/sch/176985/i.html?_nkw={q}"))
    return links


SECTION_TITLES = {
    "buy": ("Buy now", "for sale on Discogs at or under your price ceiling"),
    "pricey": ("For sale, above your ceiling", "available but priced higher than max_price"),
    "watch": ("Nothing for sale — wantlist these", "matched on Discogs but no copies listed; run `wantlist` and Discogs will email you"),
    "unresolved": ("Not on Discogs (yet)", "could not match — likely digital-only, unreleased, or a typo in the comment; try Bandcamp/YouTube"),
    "pending": ("Not resolved yet", "run `python dig_radar.py resolve` to look these up"),
}

CSS = """
:root{--bg:#F1F0EC;--ink:#1B1F2A;--muted:#5C6270;--rule:#D8D6CF;--link:#0B5FA5;--sticker:#FFD84D;--rise:#C8102E}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.45 -apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
main{max-width:760px;margin:0 auto;padding:20px 14px 60px}
h1{font-size:28px;line-height:1.1;margin:0 0 4px;letter-spacing:-.01em}
.sub{color:var(--muted);margin:0 0 22px}
h2{font-size:20px;margin:34px 0 4px;padding-top:14px;border-top:3px solid var(--ink)}
h2 small{display:block;font-size:14px;font-weight:400;color:var(--muted);margin-top:2px}
.row{display:flex;gap:12px;padding:14px 0;border-bottom:1px solid var(--rule)}
.thumb{flex:0 0 64px;width:64px;height:64px;background:#DDD9D0;border-radius:3px;object-fit:cover}
.body{flex:1;min-width:0}
.name{font-weight:650;font-size:17px;margin:0}
.rel{color:var(--muted);font-size:14px;margin:2px 0 6px;overflow-wrap:anywhere}
.stats{display:flex;flex-wrap:wrap;gap:6px 10px;font-size:14px;margin:0 0 6px}
.price{background:var(--sticker);padding:1px 8px;border-radius:12px;font-weight:650}
.rise{color:var(--rise);font-weight:650}
.seen{font-size:13px;color:var(--muted);margin:0 0 8px}
.seen span{display:inline-block;margin-right:10px}
.links a{display:inline-block;font-size:13px;color:var(--link);text-decoration:none;border:1px solid var(--link);border-radius:14px;padding:2px 9px;margin:0 6px 6px 0}
.links a:focus-visible{outline:2px solid var(--ink);outline-offset:2px}
.note{font-size:12px;color:var(--muted)}
.empty{color:var(--muted);font-style:italic}
"""


def render_html(items, cfg):
    cur = cfg.get("currency", "GBP")
    by = {}
    for it in items:
        by.setdefault(it["tier"], []).append(it)
    n_total = len(items)
    n_buy = len(by.get("buy", []))
    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>Dig Radar — {n_buy} to buy</title><style>{CSS}</style></head><body><main>",
        f"<h1>Dig Radar</h1><p class='sub'>{n_total} tracks spotted across your flagged sources · "
        f"{n_buy} for sale under {fmt_money(cfg['scoring'].get('max_price', 15), cur)} · "
        f"generated {dt.datetime.now().strftime('%d %b %Y %H:%M')}</p>",
    ]
    for tier in ("buy", "pricey", "watch", "unresolved", "pending"):
        rows = by.get(tier, [])
        head, sub = SECTION_TITLES[tier]
        parts.append(f"<h2>{html.escape(head)} ({len(rows)})<small>{html.escape(sub)}</small></h2>")
        if not rows:
            parts.append("<p class='empty'>Nothing here yet.</p>")
            continue
        for it in rows:
            rel, mk = it.get("release"), it.get("market")
            thumb = f"<img class='thumb' src='{html.escape(rel['thumb'])}' alt=''>" if rel and rel.get("thumb") else "<div class='thumb'></div>"
            name = f"{html.escape(it['artist'])} – {html.escape(it['title'])}"
            relline = ""
            if rel:
                bits = [rel.get("title"), rel.get("labels"), str(rel.get("year") or ""), rel.get("formats"), rel.get("styles")]
                relline = " · ".join(html.escape(b) for b in bits if b)
                if it.get("match_note") and "track match" not in it["match_note"]:
                    relline += f" <span class='note'>({html.escape(it['match_note'])})</span>"
            elif it.get("label_hint"):
                relline = f"label hint: {html.escape(it['label_hint'])}"
            stats = []
            if mk:
                if mk.get("num_for_sale"):
                    stats.append(f"<span class='price'>{fmt_money(mk.get('lowest_price'), mk.get('currency') or cur)}</span>"
                                 f"<span>{mk['num_for_sale']} for sale</span>")
                else:
                    stats.append("<span>none for sale</span>")
                if mk.get("want") is not None:
                    stats.append(f"<span>want {mk['want']} · have {mk.get('have')}</span>")
                if mk.get("yt_views"):
                    stats.append(f"<span>{mk['yt_views']:,} YouTube views</span>")
            stats.append(f"<span>seen {it['mentions']}× in {it['sources']} source{'s' if it['sources'] != 1 else ''}</span>")
            tr = it.get("trend")
            if tr and tr.get("want_delta"):
                arrow = "▲" if tr["want_delta"] > 0 else "▼"
                stats.append(f"<span class='rise'>{arrow} {tr['want_delta']:+d} wants in {tr['days']}d</span>")
            seen = "".join(
                f"<span>{html.escape(s['source_name'][:60])}{(' @ ' + s['position']) if s.get('position') else ''}</span>"
                for s in it["sightings"][:4]
            )
            links = "".join(f"<a href='{html.escape(u)}' target='_blank' rel='noopener'>{html.escape(n)}</a>"
                            for n, u in links_for(it))
            parts.append(
                f"<div class='row'>{thumb}<div class='body'><p class='name'>{name}</p>"
                f"<p class='rel'>{relline}</p><p class='stats'>{''.join(stats)}</p>"
                f"<p class='seen'>{seen}</p><p class='links'>{links}</p></div></div>"
            )
    parts.append("</main></body></html>")
    return "".join(parts)


def write_report(db, cfg):
    items = gather(db, cfg)
    with open(REPORT_HTML, "w", encoding="utf-8") as f:
        f.write(render_html(items, cfg))
    with open(REPORT_JSON, "w", encoding="utf-8") as f:
        json.dump({"generated": now_iso(), "currency": cfg.get("currency", "GBP"), "targets": items}, f, indent=1)
    tiers = {}
    for it in items:
        tiers[it["tier"]] = tiers.get(it["tier"], 0) + 1
    log(f"Report written: {REPORT_HTML} and {REPORT_JSON}")
    log("  " + ", ".join(f"{SECTION_TITLES[t][0]}: {n}" for t, n in tiers.items()))


# ----------------------------------------------------------------------------
# wantlist push
# ----------------------------------------------------------------------------

def push_wantlist(db, cfg, tier, dry_run):
    user, token = cfg["keys"].get("discogs_username"), cfg["keys"].get("discogs_token")
    if not (user and token):
        sys.exit("wantlist needs discogs_username and discogs_token in radar_config.json")
    dc = Discogs(token, cfg["user_agent"], cfg.get("currency", "GBP"))
    items = [it for it in gather(db, cfg) if it.get("release") and (tier == "all" or it["tier"] == tier)]
    log(f"{'Would add' if dry_run else 'Adding'} {len(items)} releases to {user}'s wantlist (tier: {tier})")
    for it in items:
        rid = it["release"]["release_id"]
        if dry_run:
            log(f"  {it['artist']} - {it['title']}  (release {rid})")
        else:
            ok = dc.add_want(user, rid)
            log(f"  {'✓' if ok else '✗'} {it['artist']} - {it['title']}")



# ----------------------------------------------------------------------------
# Phone notifications via ntfy (free; install the ntfy app on Android, subscribe to your topic)
# ----------------------------------------------------------------------------

def notify(db, cfg, dry_run=False):
    nc = cfg["notify"]
    topic, server = nc.get("ntfy_topic"), nc.get("ntfy_server", "https://ntfy.sh").rstrip("/")
    if not topic:
        log("notify: no ntfy_topic in config — skipping.")
        return
    tiers = set(nc.get("tiers", ["buy"]))
    cur = cfg.get("currency", "GBP")
    done = {(r["key"], r["tier"]) for r in db.execute("SELECT key, tier FROM notified")}
    fresh = []
    for it in gather(db, cfg):
        if it["tier"] not in tiers:
            continue
        k = norm_key(it["artist"], it["title"])
        if (k, it["tier"]) not in done:
            fresh.append((k, it))
    if not fresh:
        log("notify: nothing new.")
        return
    log(f"notify: {len(fresh)} new target{'s' if len(fresh) != 1 else ''} → {server}/{topic}")

    def post(title, body, click=None, actions=None, tags="headphones"):
        headers = {"Title": title, "Tags": tags, "Priority": "default"}
        if click:
            headers["Click"] = click
        if actions:
            headers["Actions"] = "; ".join(actions)
        if dry_run:
            log(f"  [dry-run] {title}\n    {body.replace(chr(10), ' | ')}")
            return True
        r = requests.post(f"{server}/{topic}", data=body.encode("utf-8"), headers=headers, timeout=30)
        return r.status_code == 200

    max_ind = nc.get("max_individual", 6)
    if len(fresh) > max_ind:
        # one digest rather than a flood
        lines = [f"{it['artist']} – {it['title']}" + (f" · {fmt_money(it['market'].get('lowest_price'), cur)}" if it.get("market") else "")
                 for _, it in fresh[:15]]
        ok = post(f"Dig Radar: {len(fresh)} new targets", "\n".join(lines) + ("\n…" if len(fresh) > 15 else ""),
                  click=nc.get("app_url") or None, tags="headphones,fire")
    else:
        ok = True
        for _, it in fresh:
            rel, mk = it.get("release") or {}, it.get("market") or {}
            body = " · ".join(b for b in [rel.get("title"), rel.get("labels"), str(rel.get("year") or "")] if b)
            if mk:
                body += f"\n{fmt_money(mk.get('lowest_price'), cur)} · {mk.get('num_for_sale') or 0} for sale · want {mk.get('want')}"
            seen = it["sightings"][0]["source_name"] if it["sightings"] else ""
            if seen:
                body += f"\nvia {seen[:60]}"
            links = dict(links_for(it))
            actions = [f"view, Buy, {links.get('For sale') or links.get('Discogs search')}"]
            if links.get("YouTube"):
                actions.append(f"view, Listen, {links['YouTube']}")
            ok = post(f"{it['artist']} – {it['title']}", body, click=links.get("For sale") or links.get("Discogs search"),
                      actions=actions) and ok
    if ok and not dry_run:
        ts = now_iso()
        db.executemany("INSERT OR REPLACE INTO notified(key, tier, notified_at) VALUES (?,?,?)",
                       [(k, it["tier"], ts) for k, it in fresh])
        db.commit()
        log("notify: sent.")
    elif not ok:
        log("notify: ntfy rejected the message — check ntfy_server/ntfy_topic.")


# ----------------------------------------------------------------------------
# Obsidian export — per-track notes wikilinked to artist / label / style / DJ, plus a daily log
# ----------------------------------------------------------------------------

def _safe_name(s, n=80):
    s = re.sub(r'[\\/:*?"<>|#^\[\]]+', "-", s).strip(" .-")
    return (s[:n] or "untitled")


def _wl(*parts):
    return "[[" + " - ".join(p for p in parts if p) + "]]"


def export_md(db, cfg, vault_dir):
    base = os.path.join(vault_dir, "Dig Radar")
    tracks_dir, log_dir = os.path.join(base, "Tracks"), os.path.join(base, "Log")
    os.makedirs(tracks_dir, exist_ok=True)
    os.makedirs(log_dir, exist_ok=True)
    cur = cfg.get("currency", "GBP")
    items = gather(db, cfg)
    today = dt.datetime.now().strftime("%Y-%m-%d")
    index = ["# Dig Radar targets", "", f"Updated {today}. Generated by dig_radar.py — edit the per-track notes, not this table.", "",
             "| Track | Tier | Price | For sale | Want | Score | Seen |", "|---|---|---|---|---|---|---|"]
    for it in items:
        name = _safe_name(f"{it['artist']} - {it['title']}")
        rel, mk = it.get("release") or {}, it.get("market") or {}
        index.append(f"| [[{name}]] | {it['tier']} | {fmt_money(mk.get('lowest_price'), cur) if mk else '—'} | "
                     f"{mk.get('num_for_sale') if mk else '—'} | {mk.get('want') if mk else '—'} | {it['gem_score']} | "
                     f"{it['mentions']}× / {it['sources']} src |")
        path = os.path.join(tracks_dir, name + ".md")
        if os.path.exists(path):
            # refresh only the machine block; keep whatever notes were added above it
            with open(path, "r", encoding="utf-8") as f:
                existing = f.read()
            head = existing.split("%% dig-radar-data %%")[0].rstrip()
        else:
            head = f"# {it['artist']} – {it['title']}\n\n_Your notes go here; everything below the marker is regenerated._\n"
        styles = [x.strip() for x in (rel.get("styles") or "").split(",") if x.strip()]
        labels = [x.strip() for x in (rel.get("labels") or it.get("label_hint") or "").split(",") if x.strip()]
        djs = sorted({s_["source_name"].split(":")[0].strip() for s_ in it["sightings"] if s_.get("source_name")})
        data = ["%% dig-radar-data %%", "",
                f"artist:: {_wl(it['artist'])}",
                "label:: " + ", ".join(_wl(l) for l in labels) if labels else "label:: ",
                "style:: " + ", ".join(_wl(st) for st in styles) if styles else "style:: ",
                "played-by:: " + ", ".join(_wl(d) for d in djs) if djs else "played-by:: ",
                f"tier:: {it['tier']}", f"score:: {it['gem_score']}",
                f"year:: {rel.get('year') or ''}", f"release:: {rel.get('title') or ''}",
                f"discogs:: {('https://www.discogs.com/release/' + str(rel['release_id'])) if rel.get('release_id') else ''}",
                f"price:: {fmt_money(mk.get('lowest_price'), cur) if mk else ''}",
                f"for-sale:: {mk.get('num_for_sale') if mk else ''}", f"want:: {mk.get('want') if mk else ''}",
                f"have:: {mk.get('have') if mk else ''}", f"views:: {mk.get('yt_views') if mk else ''}",
                f"updated:: {today}", "", "## Seen in", ""]
        for s_ in it["sightings"]:
            data.append(f"- {s_['source_name']}" + (f" @ {s_['position']}" if s_.get("position") else "") +
                        (f" — <{s_['source_url']}>" if s_.get("source_url", "").startswith("http") else ""))
        data += ["", "## Links", ""] + [f"- [{n}]({u})" for n, u in links_for(it)]
        with open(path, "w", encoding="utf-8") as f:
            f.write(head + "\n\n" + "\n".join(data) + "\n")
    with open(os.path.join(base, "Targets.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(index) + "\n")
    # daily log of what was seen today
    rows = db.execute("SELECT s.key, s.source_name, s.position, c.artist, c.title FROM sightings s JOIN candidates c ON c.key=s.key"
                      " WHERE substr(s.seen_at,1,10)=? ORDER BY s.seen_at", (today,)).fetchall()
    with open(os.path.join(log_dir, today + ".md"), "w", encoding="utf-8") as f:
        f.write(f"# Dig Radar log {today}\n\n{len(rows)} sightings today.\n\n")
        for r in rows:
            f.write(f"- [[{_safe_name(r['artist'] + ' - ' + r['title'])}]] — {r['source_name']}"
                    + (f" @ {r['position']}" if r["position"] else "") + "\n")
    log(f"Obsidian: wrote {len(items)} track notes, Targets.md and Log/{today}.md under {base}")

# ----------------------------------------------------------------------------
# selftest + stats
# ----------------------------------------------------------------------------

SAMPLE_COMMENTS = [
    ("34:12 Chab - Closer To Me (Sultan & The Greek Remix)", False, None),
    ("ID at 1:02:45?", False, None),
    ("It's Way Out West - Mindcircus (Gabriel & Dresden Remix)", True, "ID at 1:02:45?"),
    ("the track at 12:30 is Sasha – Xpander", False, None),
    ("Tracklist:\n1. Bedrock - Heaven Scent [Bedrock]\n2. Fade - All I Got (Chris Fortier 20yr Dub)\n3. ID - ID\n4. Nalin & Kane - Beachball (Extended Vocal Mix)", False, None),
    ("This set is amazing - best night of my life", False, None),
    ("Does anyone know - what the tune at 45:00 is?", False, None),
    ("Check out https://example.com/tracks - Artist - Title", False, None),
    ("Pretty sure it's Hallucinogen – LSD (Talamasca Remix) w/ Ticon - We Are The Mammoths", True, "what is this track at 55:10"),
    ("Underworld - Rez", False, None),
    ("00:45:10\tAstral Projection\tMahadeva", False, None),
    ("@djfan99 Sasha - Xpander is such a tune 🔥🔥", True, "ID?"),
    ("Best track of the set: Vibrasphere - Ensueno [Digital Structures]", False, None),
    ("[1:12:03] Hybrid vs. Perfecto Allstarz - Reggae Owes Me Money [Distinctive]", False, None),
    ("Man With No Name – Teleport (Original Mix) 1:23:45", False, None),
    ("1:23:45 - Global Communication - 14:31", False, None),
]


def selftest():
    log(f"dig_radar {VERSION} self-test — ID extractor\n")
    total = 0
    for text, is_reply, parent in SAMPLE_COMMENTS:
        hits = extract_ids(text)
        answering = bool(is_reply and parent and ASKS_ID_RE.search(parent))
        shown = text.replace("\n", " | ")[:70]
        if not hits:
            log(f"  –  {shown}")
            continue
        for h in hits:
            conf = 0.5 + (0.2 if h["has_ts"] else 0) + (0.15 if answering else 0)
            total += 1
            log(f"  ✓  {h['artist']}  |  {h['title']}"
                f"{'  [' + h['label'] + ']' if h['label'] else ''}"
                f"{'  @' + h['position'] if h['position'] else ''}   conf {min(conf, 0.9):.2f}   ← {shown}")
    log(f"\n{total} IDs extracted from {len(SAMPLE_COMMENTS)} sample comments. "
        "Expected: no IDs from the 'amazing', 'Does anyone know' or https lines.")


def stats(db):
    c = db.execute("SELECT COUNT(*) n, SUM(resolved=1) m, SUM(resolved=-1) x FROM candidates").fetchone()
    s = db.execute("SELECT COUNT(*) n, COUNT(DISTINCT source_url) u FROM sightings").fetchone()
    snaps = db.execute("SELECT COUNT(*) n, COUNT(DISTINCT release_id) r FROM snapshots").fetchone()
    log(f"candidates: {c['n']} (matched {c['m'] or 0}, unmatched {c['x'] or 0}, pending {c['n'] - (c['m'] or 0) - (c['x'] or 0)})")
    log(f"sightings:  {s['n']} across {s['u']} sources")
    log(f"snapshots:  {snaps['n']} for {snaps['r']} releases")


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="Dig Radar — find DJ-supported records that are cheap, available and overlooked.")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("init", help="write radar_config.json")
    h = sub.add_parser("harvest", help="pull IDs from flagged YouTube/SoundCloud sources")
    h.add_argument("--youtube-only", action="store_true")
    h.add_argument("--soundcloud-only", action="store_true")
    i = sub.add_parser("import", help="import a pasted tracklist text file")
    i.add_argument("file")
    i.add_argument("--source", required=True, help="e.g. 'Digweed Transitions 1080'")
    i.add_argument("--url", default=None)
    i.add_argument("--confidence", type=float, default=0.9)
    i.add_argument("--llm", action="store_true", help="also run Claude over lines the regex could not read (Reddit threads etc.)")
    r = sub.add_parser("resolve", help="match candidates on Discogs + snapshot market numbers")
    r.add_argument("--limit", type=int, default=60, help="max candidates per run (2 API calls each)")
    r.add_argument("--retry-failed", action="store_true")
    r.add_argument("--no-refresh", action="store_true", help="skip re-snapshotting known releases")
    sub.add_parser("report", help="write radar_report.html + radar_targets.json")
    w = sub.add_parser("wantlist", help="add matched releases to your Discogs wantlist")
    w.add_argument("--tier", choices=["buy", "pricey", "watch", "all"], default="watch")
    w.add_argument("--dry-run", action="store_true")
    n = sub.add_parser("notify", help="push new targets to your phone via ntfy")
    n.add_argument("--dry-run", action="store_true")
    e = sub.add_parser("export-md", help="write Obsidian notes into your vault")
    e.add_argument("--vault", required=True, help="path to your Obsidian vault (or any folder)")
    a = sub.add_parser("all", help="harvest, resolve, report, notify")
    a.add_argument("--limit", type=int, default=60)
    a.add_argument("--vault", default=None, help="also export Obsidian notes to this vault")
    sub.add_parser("selftest", help="run the ID extractor on sample comments")
    sub.add_parser("stats", help="counts from the local database")
    args = p.parse_args()

    if args.cmd == "init":
        if os.path.exists(CONFIG_PATH):
            sys.exit(f"{CONFIG_PATH} already exists — edit it rather than re-running init.")
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        if not os.path.exists(SECRETS_PATH):
            with open(SECRETS_PATH, "w", encoding="utf-8") as f:
                json.dump({"keys": {k: "" for k in DEFAULT_CONFIG["keys"]}, "notify": {"ntfy_topic": ""}}, f, indent=2)
        with open(".gitignore", "a", encoding="utf-8") as f:
            f.write("\nradar_secrets.json\n__pycache__/\n")
        open_db().close()
        log(f"Wrote {CONFIG_PATH} (DJs + scoring, safe to commit) and {SECRETS_PATH} (keys, git-ignored).")
        log("Fill in your keys, add your DJs, then run:  python dig_radar.py all")
        return
    if args.cmd == "selftest":
        selftest()
        return
    if not args.cmd:
        p.print_help()
        return

    cfg = load_config()
    db = open_db()
    try:
        if args.cmd == "harvest":
            if not args.soundcloud_only:
                harvest_youtube(db, cfg)
            if not args.youtube_only:
                harvest_soundcloud(db, cfg)
        elif args.cmd == "import":
            import_file(db, args.file, args.source, args.url, args.confidence, args.llm, cfg)
        elif args.cmd == "notify":
            notify(db, cfg, args.dry_run)
        elif args.cmd == "export-md":
            export_md(db, cfg, args.vault)
        elif args.cmd == "resolve":
            resolve_candidates(db, cfg, args.limit, args.retry_failed)
            if not args.no_refresh:
                refresh_snapshots(db, cfg, limit=200)
        elif args.cmd == "report":
            write_report(db, cfg)
        elif args.cmd == "wantlist":
            push_wantlist(db, cfg, args.tier, args.dry_run)
        elif args.cmd == "all":
            harvest_youtube(db, cfg)
            harvest_soundcloud(db, cfg)
            resolve_candidates(db, cfg, args.limit)
            refresh_snapshots(db, cfg, limit=200)
            write_report(db, cfg)
            notify(db, cfg)
            if args.vault:
                export_md(db, cfg, args.vault)
        elif args.cmd == "stats":
            stats(db)
    finally:
        db.commit()
        db.close()


if __name__ == "__main__":
    main()
