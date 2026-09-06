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
  agent     ONE COMMAND. Say what you want in plain English; it plans the hunt, runs the tools,
            judges every record against what you asked for, and pushes the verdicts to your phone.
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

VERSION = "0.4"
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
        "min_confidence": 0.5,
        "require_genres": ["Electronic"],
        "penalise_compilations": True
    },
    "cosine": {
        "_help": "cosine.club finds records that SOUND like the ones your DJs play, by audio not tags. Free API key at cosine.club/account/api",
        "api_key": "",
        "seeds": 15,
        "similar_per_seed": 8,
        "max_want": 120,
        "max_price": 25,
        "start_year": 0,
        "end_year": 0
    },
    "agent": {
        "_help": "The agent turns a plain-English request into a hunt, runs it, and judges the results. Needs anthropic_api_key.",
        "model": "claude-sonnet-5",
        "judge_batch": 25,
        "max_judge": 120,
        "save_config": True,
        "standing_brief": "Progressive house and progressive trance 1996-2006, plus the breaks and tech house next to it. Vinyl preferred. Long builds, real breakdowns, space to mix. No vocal trance, no big-room, no compilations where the original 12 inch exists."
    },
    "festivals": {
        "_help": "Names searched on YouTube for set recordings. Each search costs 100 YouTube quota units (you get 10,000/day).",
        "names": ["Houghton Festival", "Dimensions Festival", "Waking Life", "Draaimolen", "Dekmantel", "ADE Amsterdam Dance Event"],
        "videos_per_festival": 4,
        "extra_terms": "dj set"
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
        for section in ("keys", "notify", "cosine"):
            for k, v in (sec.get(section) or {}).items():
                if v:
                    cfg[section][k] = v
    # ...or from the environment, e.g. DIGRADAR_DISCOGS_TOKEN (GitHub Actions secrets)
    for section in ("keys", "notify", "cosine"):
        for k in list(cfg[section].keys()):
            env = os.environ.get("DIGRADAR_" + ("COSINE_API_KEY" if (section == "cosine" and k == "api_key") else k.upper()))
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
            release_id INTEGER, master_id INTEGER, match_note TEXT,
            discogs_hint INTEGER, similar_to TEXT, similarity REAL,
            verdict TEXT, verdict_reason TEXT, verdict_at TEXT
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
    have = {r["name"] for r in db.execute("PRAGMA table_info(candidates)")}
    for col, decl in (("discogs_hint", "INTEGER"), ("similar_to", "TEXT"), ("similarity", "REAL"),
                      ("verdict", "TEXT"), ("verdict_reason", "TEXT"), ("verdict_at", "TEXT")):
        if col not in have:
            db.execute(f"ALTER TABLE candidates ADD COLUMN {col} {decl}")
    db.commit()
    return db


def log(msg):
    print(msg, flush=True)


# ----------------------------------------------------------------------------
# ID extraction  (the heart of the harvester)
# ----------------------------------------------------------------------------

TIMESTAMP_RE = re.compile(r"\(?\[?(?<![\d:])(?:\d{1,2}:)?\d{1,2}:\d{2}(?!\d)\]?\)?:?")
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
CREDIT_ROLE_RE = re.compile(
    r"^(?:"
    r"(?:\w+\s+)?(?:by|credits?)|"
    r"(?:lead |backing |additional |guest )?(?:vocals?|voice|trumpet|saxophone|sax|guitar|bass|drums|percussion|"
    r"keys|keyboards|piano|synth(?:esizer)?|strings|violin|cello|flute|horns?|congas|organ)|"
    r"film(?:ed|ing)?|camera|video|photo(?:graphy|s)?|directed|direction|edit(?:ed|or)?|art(?:work)?|design|"
    r"mix(?:ed|ing)?|master(?:ed|ing)?|record(?:ed|ing)|engineer(?:ed|ing)?|produc(?:ed|er|tion)|written|"
    r"compos(?:ed|er)|lyrics|arrang(?:ed|ement)|remix(?:ed)?|label|booking|management|manager|"
    r"lighting|sound|stage|host(?:ed)?|presented|supported|filmed|shot|colou?r|graphics?|"
    r"follow|subscribe|listen|watch|buy|stream|out\s+now|release[ds]?|available"
    r")\b",
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
    if CREDIT_ROLE_RE.match(artist):
        return False          # "Trumpet - Ben Edwards" is a credits line, not a track
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
            pos = m.group(0).strip("[]() :")
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
                        h["position"] = parent_ts.group(0).strip("[]() :")  # "ID at 34:12?" -> answer inherits 34:12
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


def pick_release(results, artist, prefer_format, require_genres=None, penalise_comps=True):
    """Choose the best Discogs hit. require_genres keeps a jazz LP from matching a techno ID."""
    scored = []
    a_n = _norm_text(artist)
    want_genres = {g.lower() for g in (require_genres or [])}
    for r in results:
        rtitle = r.get("title", "")
        r_artist = rtitle.split(" - ", 1)[0]
        sim = difflib.SequenceMatcher(None, _norm_text(r_artist), a_n).ratio()
        various = r_artist.strip().lower().startswith("various")
        if sim < 0.45 and not various:
            continue
        genres = {g.lower() for g in (r.get("genre") or [])}
        if want_genres and genres and not (genres & want_genres):
            continue          # wrong universe entirely — skip it
        fmt = " ".join(r.get("format") or [])
        comp = 1 if (penalise_comps and re.search(r"compilation|mixed", fmt, re.I)) else 0
        pref = 0 if prefer_format.lower() in fmt.lower() else 1
        y = str(r.get("year") or "")
        year = int(y) if y.isdigit() else 9999
        scored.append(((1 if various else 0), comp, pref, year, -sim, r))
    if not scored:
        return None
    scored.sort(key=lambda x: x[:5])
    return scored[0][5]


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
        hint = c["discogs_hint"] if "discogs_hint" in c.keys() else None
        if hint:
            # cosine.club already told us the exact release — skip searching entirely
            full = dc.release(hint) or {}
            if full:
                store_release(db, hint, {}, full)
                snapshot(db, hint, full, {}, cfg.get("currency", "GBP"), None)
                db.execute("UPDATE candidates SET resolved=1, release_id=?, master_id=?, match_note=? WHERE key=?",
                           (hint, full.get("master_id"), "exact release from cosine.club", c["key"]))
                db.commit()
                matched += 1
                log(f"  ✓ {artist} - {title}  →  {full.get('title')} ({full.get('year')}) "
                    f"{full.get('lowest_price')} · {full.get('num_for_sale', 0)} for sale")
                continue
        note = "track match"
        results = dc.search(artist=artist, track=title)
        if not results and base_title(title) != title:
            results = dc.search(artist=artist, track=base_title(title))
            note = "matched base title (remix/version may differ)"
        if not results:
            results = dc.search(q=f"{artist} {base_title(title)}")
            note = "loose text match — check it"
        rel = pick_release(results, artist, sc.get("prefer_format", "Vinyl"),
                           sc.get("require_genres"), sc.get("penalise_compilations", True)) if results else None
        if not rel:
            db.execute("UPDATE candidates SET resolved=-1, match_note='no Discogs match in your genres' WHERE key=?",
                       (c["key"],))
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
# cosine.club — "what else sounds like this?"  (free API key at cosine.club/account/api)
#
# It runs the discogs-effnet audio model over 2M+ electronic tracks, so it matches on how a
# record actually SOUNDS, not on tags or on what other people streamed. That matters here:
# it can filter by Discogs want-count, so we can ask it directly for records that sound like
# the ones your DJs play AND that almost nobody has in their wantlist.
# ----------------------------------------------------------------------------

class Cosine:
    BASE = "https://cosine.club/api/v1"

    def __init__(self, api_key, ua):
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {api_key}", "User-Agent": ua,
                               "Content-Type": "application/json"})

    def _req(self, method, path, **kw):
        for attempt in range(3):
            r = self.s.request(method, self.BASE + path, timeout=60, **kw)
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After", 10)) + 1)
                continue
            if r.status_code == 401:
                raise RuntimeError("cosine.club rejected the API key (get one at cosine.club/account/api)")
            if r.status_code == 404:
                return None
            r.raise_for_status()
            time.sleep(0.6)          # well inside the 120/min limit
            return r.json()
        return None

    def bulk_similar(self, tracks, limit, filters):
        body = {"tracks": tracks[:50], "similar_limit": limit}
        body.update({k: v for k, v in filters.items() if v})
        return self._req("POST", "/search/bulk", json=body)


DISCOGS_RELEASE_RE = re.compile(r"discogs\.com/(?:.*?/)?release/(\d+)")


def expand_cosine(db, cfg, seeds_override=None, dry_run=False):
    """Take the tracks your DJs actually played and ask cosine.club what else sounds like them."""
    cc = cfg.get("cosine", {})
    key = cc.get("api_key")
    if not key:
        log("cosine: no api_key set — skipping. Free key at https://cosine.club/account/api")
        return
    # seeds: highest-confidence DJ-played tracks, not ones cosine itself suggested
    rows = db.execute(
        "SELECT key, artist, title FROM candidates WHERE resolved=1 AND similar_to IS NULL"
        " ORDER BY confidence DESC, mentions DESC LIMIT ?",
        (seeds_override or cc.get("seeds", 15),)).fetchall()
    if not rows:
        log("cosine: no resolved DJ-played tracks to expand from yet — run harvest + resolve first.")
        return
    queries = [f"{r['artist']} - {r['title']}" for r in rows]
    log(f"cosine: expanding from {len(queries)} seed tracks...")
    filters = {
        "max_want": cc.get("max_want") or None,
        "max_price": cc.get("max_price") or None,
        "start_year": cc.get("start_year") or None,
        "end_year": cc.get("end_year") or None,
    }
    try:
        data = Cosine(key, cfg["user_agent"]).bulk_similar(queries, cc.get("similar_per_seed", 8), filters)
    except Exception as e:
        log(f"cosine: {e}")
        return
    if not data or not data.get("success"):
        log("cosine: no usable response.")
        return
    results = (data.get("data") or {}).get("results") or []
    unmatched = (data.get("data") or {}).get("unmatched") or []
    new_total = 0
    for res in results:
        seed = res.get("track") or {}
        seed_name = seed.get("name") or res.get("query")
        for sim in res.get("similar_tracks") or []:
            artist, title = clean_side(sim.get("artist") or ""), clean_side(sim.get("track") or "")
            if not plausible(artist, title):
                continue
            score = float(sim.get("score") or 0)
            hit = {"artist": artist, "title": title, "label": None, "position": None,
                   "raw": f"sounds like {seed_name} ({score:.2f})", "has_ts": False}
            if dry_run:
                log(f"  {artist} - {title}   {score:.2f}  ← {seed_name}")
                continue
            # cosine confidence is deliberately lower than a DJ actually playing it
            conf = min(0.75, 0.35 + 0.4 * score)
            is_new = add_sighting(db, hit, conf, "cosine", f"sounds like {seed_name}",
                                  sim.get("external_link") or f"cosine:{sim.get('id')}")
            k = norm_key(artist, title)
            m = DISCOGS_RELEASE_RE.search(sim.get("external_link") or "")
            db.execute("UPDATE candidates SET similar_to=COALESCE(similar_to,?), similarity=MAX(COALESCE(similarity,0),?),"
                       " discogs_hint=COALESCE(discogs_hint,?) WHERE key=?",
                       (seed_name, score, int(m.group(1)) if m else None, k))
            new_total += is_new
    db.commit()
    log(f"cosine: {new_total} new candidates that sound like your DJs' records"
        + (f" ({len(unmatched)} seeds not in the catalogue)" if unmatched else ""))
    if not dry_run:
        log("  run `resolve` next — cosine hands over exact Discogs release ids, so those are cheap and accurate.")


# ----------------------------------------------------------------------------
# Festival mode — find the set recordings, then read their tracklists and ID threads
# ----------------------------------------------------------------------------

def harvest_festivals(db, cfg, names=None, year=None):
    key = cfg["keys"].get("youtube_api_key")
    if not key:
        log("festivals: no youtube_api_key — skipping.")
        return
    fc = cfg.get("festivals", {})
    names = names or fc.get("names", [])
    if not names:
        log("festivals: none listed in radar_config.json.")
        return
    yt = YouTube(key, cfg["user_agent"])
    per = fc.get("videos_per_festival", 4)
    extra = fc.get("extra_terms", "dj set")
    year = year or dt.datetime.now().year
    found = []
    for name in names:
        q = f"{name} {year} {extra}".strip()
        try:
            data = yt.get("search", part="snippet", q=q, type="video", maxResults=min(per * 2, 25),
                          order="relevance", videoDuration="long")
        except QuotaExceeded:
            log("festivals: YouTube quota exceeded — try tomorrow.")
            break
        items = (data or {}).get("items", [])
        if not items:
            data = yt.get("search", part="snippet", q=f"{name} {extra}", type="video",
                          maxResults=min(per * 2, 25), order="viewCount", videoDuration="long")
            items = (data or {}).get("items", [])
        ids = [it["id"]["videoId"] for it in items][:per]
        log(f"festivals: {name} — {len(ids)} long-form sets found")
        found += ids
    if not found:
        return
    seen = set()
    found = [v for v in found if not (v in seen or seen.add(v))]
    # reuse the normal harvester by pointing it at these videos
    tmp = json.loads(json.dumps(cfg))
    tmp["sources"] = {"youtube_channels": [], "youtube_playlists": [],
                      "youtube_videos": [f"https://www.youtube.com/watch?v={v}" for v in found],
                      "soundcloud_users": [], "soundcloud_tracks": []}
    harvest_youtube(db, tmp)


# ----------------------------------------------------------------------------
# THE AGENT
#
# One command. You say what you want; it plans the hunt, picks and runs the tools,
# then judges every record it found against what you actually asked for.
# The judgement that used to live in a chat now lives in here.
# ----------------------------------------------------------------------------

def claude(cfg, prompt, max_tokens=4000, system=None):
    key = cfg["keys"].get("anthropic_api_key")
    if not key:
        return None
    body = {"model": cfg["agent"].get("model", "claude-sonnet-5"),
            "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]}
    if system:
        body["system"] = system
    try:
        r = requests.post("https://api.anthropic.com/v1/messages",
                          headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                   "content-type": "application/json"}, json=body, timeout=180)
        if r.status_code != 200:
            log(f"agent: API error {r.status_code}: {r.text[:200]}")
            return None
        return "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
    except Exception as e:
        log(f"agent: {e}")
        return None


def _json_from(text):
    if not text:
        return None
    t = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        a, b = t.find(opener), t.rfind(closer)
        if a >= 0 and b > a:
            try:
                return json.loads(t[a:b + 1])
            except Exception:
                continue
    return None


PLAN_SYSTEM = """You plan record-digging runs for a DJ's personal tool. You output only JSON.

The tool can:
- harvest YouTube channels (DJ set uploads: reads tracklists in descriptions and "ID at 34:12?" comment threads)
- search YouTube for festival sets by name
- expand via cosine.club (audio-similarity over 2M electronic tracks, filterable by Discogs want count)
- match everything on Discogs for price, copies for sale, want/have

Style names MUST be real Discogs styles (e.g. Progressive House, Progressive Trance, Trance, Breaks,
Tech House, Deep House, Techno, Downtempo, Ambient, Psy-Trance, Progressive Breaks, House, Electro).
Channels must be real YouTube channels that post long-form DJ sets. Festivals must be real events."""


def plan_hunt(cfg, request):
    prompt = f"""Plan a record hunt.

STANDING TASTE: {cfg['agent'].get('standing_brief', '')}

THIS REQUEST: {request}

CURRENT CONFIG:
{json.dumps({'sources': cfg['sources'], 'scoring': cfg['scoring'], 'cosine': cfg['cosine'], 'festivals': cfg['festivals']}, indent=1, default=str)}

Return ONLY this JSON:
{{
 "brief": "<2-3 sentences: the sound, era, and what counts as a hit here>",
 "reasoning": "<1-2 sentences on why these sources and filters, especially why this material may be underpriced>",
 "run": ["harvest","festival","cosine"],
 "config": {{
   "sources": {{"youtube_channels": ["<full URLs>"]}},
   "festivals": {{"names": ["<event names>"]}},
   "scoring": {{"style_boost": ["<Discogs styles>"], "prefer_years": [<from>,<to>], "max_price": <number>}},
   "cosine": {{"max_want": <number>, "seeds": <number>, "similar_per_seed": <number>}}
 }}
}}

Rules: include a step in "run" only if it will help this request. Keep youtube_channels to at most 6 and
festivals to at most 4 (each festival search costs 100 of 10,000 daily YouTube units). Lower cosine.max_want
finds more overlooked records and more junk; 150 is loose, 40 is strict. Preserve existing channels that
still fit the request; drop ones that do not."""
    return _json_from(claude(cfg, prompt, 2000, PLAN_SYSTEM))


def merge_config(cfg, patch, save):
    """Apply the agent's config patch. Lists replace, scalars replace, unknown keys ignored."""
    changed = []
    for section in ("sources", "scoring", "cosine", "festivals"):
        for k, v in (patch.get(section) or {}).items():
            if k.startswith("_") or k not in cfg.get(section, {}):
                continue
            if cfg[section][k] != v:
                changed.append(f"{section}.{k}: {cfg[section][k]!r} -> {v!r}")
                cfg[section][k] = v
    if save and changed:
        keep = json.load(open(CONFIG_PATH, encoding="utf-8")) if os.path.exists(CONFIG_PATH) else {}
        for section in ("sources", "scoring", "cosine", "festivals"):
            keep.setdefault(section, {}).update(cfg[section])
        for sect in ("keys", "notify"):
            for k in keep.get(sect, {}):
                keep[sect][k] = ""          # never write secrets back into the committed config
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(keep, f, indent=2)
    return changed


JUDGE_SYSTEM = """You judge records for a DJ deciding what to buy. You output only JSON.

Provenance matters and you must respect it:
- played      = a DJ actually played it (from a tracklist or a timestamped ID). Strongest signal.
- sounds-like = cosine.club audio similarity from a played record. Nobody played this. Always needs a listen.
- unverified  = one unconfirmed comment. Weak.

Verdicts:
- "buy"      strong fit, for sale, sensibly priced, and the provenance holds up
- "listen"   promising but must be heard first (default for sounds-like)
- "wantlist" right record, nothing for sale or priced wrong today
- "skip"     wrong fit, wrong genre, a parser artefact, repressed to death, or only cheap

Be willing to skip most of a list. A short honest list beats a long padded one. Never invent numbers."""


def judge_candidates(db, cfg, brief, limit=None):
    items = [it for it in gather(db, cfg) if it["tier"] != "pending"]
    todo = [it for it in items if not it.get("verdict")][: (limit or cfg["agent"].get("max_judge", 120))]
    if not todo:
        log("agent: nothing new to judge.")
        return 0
    bs = cfg["agent"].get("judge_batch", 25)
    done = 0
    for i in range(0, len(todo), bs):
        batch = todo[i:i + bs]
        rows = []
        for n, it in enumerate(batch):
            rel, mk = it.get("release") or {}, it.get("market") or {}
            rows.append({
                "i": n, "artist": it["artist"], "title": it["title"],
                "provenance": "played" if it.get("played") else ("sounds-like" if it.get("similar_to") else "unverified"),
                "sounds_like": it.get("similar_to"), "similarity": it.get("similarity"),
                "release": rel.get("title"), "label": rel.get("labels"), "year": rel.get("year"),
                "format": rel.get("formats"), "styles": rel.get("styles"), "genres": rel.get("genres"),
                "price": mk.get("lowest_price"), "for_sale": mk.get("num_for_sale"),
                "want": mk.get("want"), "have": mk.get("have"), "views": mk.get("yt_views"),
                "seen_in": [sg["source_name"][:60] for sg in it["sightings"][:2]],
                "match_note": it.get("match_note"),
            })
        prompt = f"""THE HUNT: {brief}

STANDING TASTE: {cfg['agent'].get('standing_brief', '')}

CANDIDATES:
{json.dumps(rows, indent=1, default=str)}

Return ONLY a JSON array, one object per candidate:
[{{"i": <index>, "verdict": "buy|listen|wantlist|skip", "reason": "<max 18 words, specific>"}}]

The reason must name the actual thing: the label, the year, the format, the provenance, the numbers.
Not "good progressive house record"."""
        out = _json_from(claude(cfg, prompt, 4000, JUDGE_SYSTEM)) or []
        ts = now_iso()
        for o in out:
            try:
                it = batch[int(o["i"])]
            except Exception:
                continue
            v = str(o.get("verdict", "")).lower()
            if v not in ("buy", "listen", "wantlist", "skip"):
                continue
            db.execute("UPDATE candidates SET verdict=?, verdict_reason=?, verdict_at=? WHERE key=?",
                       (v, str(o.get("reason", ""))[:200], ts, norm_key(it["artist"], it["title"])))
            done += 1
        db.commit()
        log(f"  judged {min(i + bs, len(todo))}/{len(todo)}")
    counts = {}
    for r in db.execute("SELECT verdict, COUNT(*) n FROM candidates WHERE verdict IS NOT NULL GROUP BY verdict"):
        counts[r["verdict"]] = r["n"]
    log(f"agent: {done} judged. Totals: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    return done


def run_agent(db, cfg, request, plan_only=False, judge_only=False):
    if not cfg["keys"].get("anthropic_api_key"):
        sys.exit("The agent needs anthropic_api_key in radar_secrets.json (or DIGRADAR_ANTHROPIC_API_KEY).")
    request = request or "Anything new that fits my standing taste."
    if judge_only:
        judge_candidates(db, cfg, request)
        write_report(db, cfg)
        notify(db, cfg)
        return
    log(f'agent: planning "{request}"...')
    plan = plan_hunt(cfg, request)
    if not plan:
        sys.exit("agent: could not plan the hunt. Check the API key and try again.")
    brief = plan.get("brief", request)
    log(f"\n  Brief: {brief}")
    if plan.get("reasoning"):
        log(f"  Why:   {plan['reasoning']}")
    changed = merge_config(cfg, plan.get("config") or {}, cfg["agent"].get("save_config", True) and not plan_only)
    for c in changed:
        log(f"  set    {c}")
    steps = [s for s in (plan.get("run") or []) if s in ("harvest", "festival", "cosine")]
    log(f"  Run:   {' -> '.join(steps) or 'nothing'} -> resolve -> judge\n")
    if plan_only:
        log("(--plan-only: stopping before anything runs)")
        return
    if "harvest" in steps:
        harvest_youtube(db, cfg)
        harvest_soundcloud(db, cfg)
    if "festival" in steps:
        harvest_festivals(db, cfg)
    resolve_candidates(db, cfg, 60)
    if "cosine" in steps:
        expand_cosine(db, cfg)
        resolve_candidates(db, cfg, 60)
    refresh_snapshots(db, cfg, 200)
    log("agent: judging what came back...")
    judge_candidates(db, cfg, brief)
    write_report(db, cfg)
    notify(db, cfg)

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
            "similar_to": c["similar_to"] if "similar_to" in c.keys() else None,
            "verdict": c["verdict"] if "verdict" in c.keys() else None,
            "verdict_reason": c["verdict_reason"] if "verdict_reason" in c.keys() else None,
            "similarity": c["similarity"] if "similarity" in c.keys() else None,
            "sightings": [dict(s) for s in sightings],
        }
        item["played"] = any(sg["source_type"] != "cosine" for sg in sightings) if sightings else True
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
            played_mult = 1.0 if item["played"] else 0.7   # a DJ playing it beats "sounds like"
            item["gem_score"] = round(signal * obscurity * style_mult * year_mult * trend_mult * played_mult, 3)
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
    vorder = {"buy": 0, "listen": 1, "wantlist": 2, None: 3, "skip": 4}
    out.sort(key=lambda x: (vorder.get(x.get("verdict"), 3), order.get(x["tier"], 9),
                            -x["gem_score"], -x["mentions"]))
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
.verdict{font-size:14px;margin:0 0 6px;padding:6px 9px;border-radius:6px;background:#E7E5DE}
.v-buy{background:#DFF0E4}.v-listen{background:#FFF3CC}.v-wantlist{background:#E3ECF7}.v-skip{background:#EFEDE7;color:var(--muted)}
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
            if it.get("played"):
                stats.append(f"<span>seen {it['mentions']}× in {it['sources']} source{'s' if it['sources'] != 1 else ''}</span>")
            elif it.get("similar_to"):
                stats.append(f"<span>sounds like {html.escape(it['similar_to'])}"
                             + (f" ({it['similarity']:.2f})" if it.get("similarity") else "") + "</span>")
            tr = it.get("trend")
            if tr and tr.get("want_delta"):
                arrow = "▲" if tr["want_delta"] > 0 else "▼"
                stats.append(f"<span class='rise'>{arrow} {tr['want_delta']:+d} wants in {tr['days']}d</span>")
            vd = ""
            if it.get("verdict"):
                vd = (f"<p class='verdict v-{it['verdict']}'><b>{it['verdict'].upper()}</b> "
                      f"{html.escape(it.get('verdict_reason') or '')}</p>")
            seen = "".join(
                f"<span>{html.escape(s['source_name'][:60])}{(' @ ' + s['position']) if s.get('position') else ''}</span>"
                for s in it["sightings"][:4]
            )
            links = "".join(f"<a href='{html.escape(u)}' target='_blank' rel='noopener'>{html.escape(n)}</a>"
                            for n, u in links_for(it))
            parts.append(
                f"<div class='row'>{thumb}<div class='body'><p class='name'>{name}</p>"
                f"<p class='rel'>{relline}</p><p class='stats'>{''.join(stats)}</p>"
                f"{vd}<p class='seen'>{seen}</p><p class='links'>{links}</p></div></div>"
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
    any_verdicts = db.execute("SELECT 1 FROM candidates WHERE verdict IS NOT NULL LIMIT 1").fetchone() is not None
    for it in gather(db, cfg):
        if any_verdicts:
            if it.get("verdict") not in ("buy", "listen"):
                continue        # the agent has an opinion; trust it over the tier heuristic
        elif it["tier"] not in tiers:
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
            if it.get("verdict_reason"):
                body += "\n" + it["verdict_reason"]
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
    ag = sub.add_parser("agent", help="say what you want in plain English; it plans, runs and judges")
    ag.add_argument("request", nargs="?", default=None)
    ag.add_argument("--plan-only", action="store_true", help="show the plan without running anything")
    ag.add_argument("--judge-only", action="store_true", help="judge what is already in the database")
    cs = sub.add_parser("cosine", help="find records that SOUND like the ones your DJs play (cosine.club)")
    cs.add_argument("--seeds", type=int, default=None)
    cs.add_argument("--dry-run", action="store_true")
    fe = sub.add_parser("festival", help="find festival sets on YouTube and harvest their IDs")
    fe.add_argument("--name", action="append", help="override the config list; repeatable")
    fe.add_argument("--year", type=int, default=None)
    n = sub.add_parser("notify", help="push new targets to your phone via ntfy")
    n.add_argument("--dry-run", action="store_true")
    e = sub.add_parser("export-md", help="write Obsidian notes into your vault")
    e.add_argument("--vault", required=True, help="path to your Obsidian vault (or any folder)")
    a = sub.add_parser("all", help="harvest, resolve, report, notify")
    a.add_argument("--limit", type=int, default=60)
    a.add_argument("--vault", default=None, help="also export Obsidian notes to this vault")
    a.add_argument("--festivals", action="store_true", help="also sweep the festivals listed in the config")
    a.add_argument("--cosine", action="store_true", help="also expand via cosine.club")
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
                json.dump({"keys": {k: "" for k in DEFAULT_CONFIG["keys"]},
                           "notify": {"ntfy_topic": ""}, "cosine": {"api_key": ""}}, f, indent=2)
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
        elif args.cmd == "agent":
            run_agent(db, cfg, args.request, args.plan_only, args.judge_only)
        elif args.cmd == "cosine":
            expand_cosine(db, cfg, args.seeds, args.dry_run)
        elif args.cmd == "festival":
            harvest_festivals(db, cfg, args.name, args.year)
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
            if args.festivals:
                harvest_festivals(db, cfg)
            resolve_candidates(db, cfg, args.limit)
            if args.cosine:
                expand_cosine(db, cfg)
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
