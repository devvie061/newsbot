#!/usr/bin/env python3
"""NewsBot : surveille Google News et t'alerte sur Telegram quand plusieurs
médias différents couvrent la même histoire.

Commandes :
    python3 newsbot.py setup          configuration interactive (Telegram, mots-clés, Claude)
    python3 newsbot.py run            service en continu (utilisé par launchd)
    python3 newsbot.py once           un seul scan, puis quitte
    python3 newsbot.py history        liste des histoires détectées
    python3 newsbot.py story ID       articles d'une histoire
    python3 newsbot.py test-notify    envoie un message de test sur Telegram
"""
from __future__ import annotations

import argparse
import fcntl
import getpass
import html
import json
import logging
import math
import os
import re
import sqlite3
import ssl
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from logging.handlers import RotatingFileHandler

HOME = os.environ.get("NEWSBOT_HOME") or os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("NEWSBOT_CONFIG") or os.path.join(HOME, "config.json")
HISTORY_URL = os.environ.get("NEWSBOT_HISTORY_URL", "")
DB_PATH = os.path.join(HOME, "newsbot.db")
HISTORY_PATH = os.path.join(HOME, "history.html")
LOG_PATH = os.path.join(HOME, "newsbot.log")
LOCK_PATH = os.path.join(HOME, ".newsbot.lock")

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) NewsBot/1.0 (personal feed reader)"
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
TELEGRAM_API = os.environ.get("NEWSBOT_TELEGRAM_API", "https://api.telegram.org")

DEFAULT_CONFIG = {
    "keywords": ["dog", "cat", "elephant"],
    "min_sources": 3,
    "interval_minutes": 5,
    "lookback_hours": 24,
    "story_window_hours": 48,
    "similarity_threshold": 0.3,
    "min_shared_terms": 2,
    "renotify_new_sources": 3,
    "max_alerts_per_scan": 10,
    "request_delay_seconds": 2,
    "retention_days": 30,
    "blocked_domains": ["facebook.com", "instagram.com", "x.com", "twitter.com",
                        "youtube.com", "tiktok.com", "reddit.com", "pinterest.com"],
    "google_news": {"hl": "en-US", "gl": "US", "ceid": "US:en"},
    "telegram": {"token": "", "chat_id": ""},
    "claude": {"enabled": False, "api_key": "", "model": "claude-opus-5-5"},
}

log = logging.getLogger("newsbot")


# --------------------------------------------------------------------------- utils

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def local_str(s: str | None) -> str:
    if not s:
        return "—"
    return parse_iso(s).astimezone().strftime("%d/%m %H:%M")


def _ssl_context() -> ssl.SSLContext:
    # Le Python de python.org n'utilise pas le trousseau macOS : certifi évite
    # les erreurs CERTIFICATE_VERIFY_FAILED.
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


SSL_CTX = _ssl_context()


def http_request(url: str, data: dict | None = None, timeout: float = 20) -> bytes:
    headers = {"User-Agent": USER_AGENT}
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
        return resp.read()


# -------------------------------------------------------------------------- config

def _merge(defaults: dict, value: dict) -> dict:
    out = dict(defaults)
    for k, v in value.items():
        if isinstance(v, dict) and isinstance(defaults.get(k), dict):
            out[k] = _merge(defaults[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        return json.loads(json.dumps(DEFAULT_CONFIG))
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return _merge(DEFAULT_CONFIG, json.load(f))


def save_config(cfg: dict) -> None:
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG_PATH)


def setup_logging(to_console: bool = True) -> None:
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if to_console:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        log.addHandler(sh)


# ------------------------------------------------------------------------ database

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    clean_title TEXT NOT NULL,
    url TEXT NOT NULL,
    source TEXT NOT NULL,
    domain TEXT NOT NULL,
    published TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    tokens TEXT NOT NULL,
    keywords TEXT NOT NULL,
    story_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_articles_story ON articles(story_id);
CREATE INDEX IF NOT EXISTS idx_articles_seen ON articles(first_seen);
CREATE TABLE IF NOT EXISTS stories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    last_article_at TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT,
    keywords TEXT NOT NULL DEFAULT '',
    n_articles INTEGER NOT NULL DEFAULT 0,
    n_sources INTEGER NOT NULL DEFAULT 0,
    notified_at TEXT,
    notified_sources INTEGER NOT NULL DEFAULT 0,
    tg_message_id INTEGER,
    llm_checked_sources INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_stories_last ON stories(last_article_at);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def open_db() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def meta_get(db: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def meta_set(db: sqlite3.Connection, key: str, value) -> None:
    db.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
    db.commit()


# ------------------------------------------------------------------ text analysis

STOPWORDS = set("""
a about above after again against all also am an and any are as at be because been before
being below between both but by can could did do does doing down during each few for from
further had has have having he her here hers herself him himself his how i if in into is it
its itself just me more most my myself no nor not now of off on once only or other our ours
out over own same she should so some such than that the their theirs them themselves then
there these they this those through to too under until up very was we were what when where
which while who whom why will with would you your yours yourself
says said say new news latest live update updates video videos watch watching photo photos
picture pictures report reports reported breaking today week year years day days amid via
get gets got make makes made see seen look looks know tap read full story stories here's
""".split())

NUMBER_WORDS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
                "seven": "7", "eight": "8", "nine": "9", "ten": "10", "eleven": "11",
                "twelve": "12", "dozen": "12"}
SYNONYMS = {"hrs": "hour", "hr": "hour", "mins": "minute", "min": "minute", "yr": "year",
            "yrs": "year", "govt": "government", "kg": "kilogram", "kgs": "kilogram"}


def normalize_text(text: str) -> str:
    # NFKC ramène les lettres « 𝗯𝗼𝗹𝗱 » Unicode à l'ASCII, NFKD retire les accents.
    text = unicodedata.normalize("NFKC", text)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().replace("’", "'").replace("‘", "'")
    return re.sub(r"'s\b", "", text)


def stem(w: str) -> str:
    """Racinisation volontairement grossière : rapproche reunite/reunited/reunion."""
    if w.isdigit():
        return w
    if len(w) > 4 and w.endswith("ies"):
        w = w[:-3] + "y"
    elif len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        w = w[:-1]
    if len(w) > 5 and w.endswith("ing"):
        w = w[:-3]
    elif len(w) > 4 and w.endswith("ed"):
        w = w[:-2]
    if len(w) > 3 and w.endswith("e"):
        w = w[:-1]
    if len(w) > 3 and w[-1] == w[-2] and w[-1] not in "aeiouls":
        w = w[:-1]
    return w[:5]


def tokenize(text: str) -> list[str]:
    out = []
    for w in re.findall(r"[a-z0-9]+", normalize_text(text)):
        w = NUMBER_WORDS.get(w, SYNONYMS.get(w, w))
        if w in STOPWORDS or (len(w) < 3 and not w.isdigit()):
            continue
        out.append(stem(w))
    return out


def keyword_stems(keywords: list[str]) -> set[str]:
    stems = set()
    for kw in keywords:
        for w in re.findall(r"[a-z0-9]+", normalize_text(kw)):
            stems.add(stem(w))
    return stems


def clean_title(title: str, source: str) -> str:
    t = title.strip()
    if source and t.endswith(" - " + source):
        t = t[: -len(source) - 3].strip()
    parts = [p.strip() for p in t.split(" | ") if p.strip()]
    if len(parts) > 1:
        t = max(parts, key=len)
    return t


def domain_of(url: str, fallback: str) -> str:
    netloc = urllib.parse.urlparse(url or "").netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc or fallback.strip().lower()


def is_blocked(domain: str, blocked: list[str]) -> bool:
    return any(domain == b or domain.endswith("." + b) for b in blocked)


def normalize_vec(v: dict[str, float]) -> dict[str, float]:
    n = math.sqrt(sum(x * x for x in v.values()))
    return {k: x / n for k, x in v.items()} if n else {}


def cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(x * b.get(k, 0.0) for k, x in a.items())


# --------------------------------------------------------------------- Google News

def fetch_google_news(keyword: str, hours: int, cfg: dict) -> list[dict]:
    term = f'"{keyword}"' if " " in keyword.strip() else keyword.strip()
    params = {"q": f"{term} when:{hours}h", **cfg["google_news"]}
    url = GOOGLE_NEWS_RSS + "?" + urllib.parse.urlencode(params)
    root = ET.fromstring(http_request(url))
    items = []
    for it in root.iter("item"):
        src = it.find("source")
        source = (src.text or "").strip() if src is not None else ""
        source_url = src.get("url", "") if src is not None else ""
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        guid = (it.findtext("guid") or link).strip()
        try:
            published = parsedate_to_datetime(it.findtext("pubDate") or "")
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            published = utcnow()
        if title and link:
            items.append({"id": guid, "title": title, "url": link, "source": source or "?",
                          "domain": domain_of(source_url, source or "?"),
                          "published": published})
    return items


# ---------------------------------------------------------------------- clustering

class Clusterer:
    """Rattache chaque nouvel article à l'histoire la plus proche (TF-IDF sur les
    titres, sans les mots-clés surveillés, qui sont communs à tous les articles)."""

    def __init__(self, db: sqlite3.Connection, cfg: dict):
        self.db = db
        self.cfg = cfg
        self.exclude = keyword_stems(cfg["keywords"])
        since = iso(utcnow() - timedelta(days=7))
        df: Counter = Counter()
        n = 0
        for row in db.execute("SELECT tokens FROM articles WHERE first_seen >= ?", (since,)):
            df.update(set(row["tokens"].split()))
            n += 1
        self.idf = {t: math.log((n + 1) / (c + 1)) + 1 for t, c in df.items()}
        self.default_idf = math.log(n + 1) + 1

    def vector(self, tokens: str) -> dict[str, float]:
        terms = {t for t in tokens.split() if t not in self.exclude}
        return normalize_vec({t: self.idf.get(t, self.default_idf) for t in terms})

    def run(self, new_ids: list[str]) -> set[int]:
        if not new_ids:
            return set()
        threshold = float(self.cfg["similarity_threshold"])
        min_shared = int(self.cfg["min_shared_terms"])
        window = iso(utcnow() - timedelta(hours=self.cfg["story_window_hours"]))

        stories: dict[int, dict] = {}
        for row in self.db.execute(
                "SELECT a.story_id, a.tokens FROM articles a JOIN stories s ON s.id = a.story_id "
                "WHERE s.last_article_at >= ?", (window,)):
            st = stories.setdefault(row["story_id"], {"members": [], "sum": Counter()})
            v = self.vector(row["tokens"])
            st["members"].append(v)
            st["sum"].update(v)

        marks = ",".join("?" * len(new_ids))
        new_rows = self.db.execute(
            f"SELECT id, tokens, published FROM articles WHERE id IN ({marks}) "
            f"ORDER BY published", new_ids).fetchall()

        touched: set[int] = set()
        for art in new_rows:
            v = self.vector(art["tokens"])
            best_sid, best_score = None, 0.0
            if v:
                for sid, st in stories.items():
                    best_pair, shared = 0.0, 0
                    for m in st["members"]:
                        s = cosine(v, m)
                        if s > best_pair:
                            best_pair, shared = s, len(v.keys() & m.keys())
                    if shared < min_shared:
                        continue
                    score = 0.5 * cosine(v, normalize_vec(dict(st["sum"]))) + 0.5 * best_pair
                    if score > best_score:
                        best_sid, best_score = sid, score
            if best_sid is None or best_score < threshold:
                cur = self.db.execute(
                    "INSERT INTO stories(created_at, last_article_at, title) VALUES(?, ?, '')",
                    (iso(utcnow()), art["published"]))
                best_sid = cur.lastrowid
                stories[best_sid] = {"members": [], "sum": Counter()}
            self.db.execute("UPDATE articles SET story_id=? WHERE id=?", (best_sid, art["id"]))
            stories[best_sid]["members"].append(v)
            stories[best_sid]["sum"].update(v)
            touched.add(best_sid)

        for sid in touched:
            refresh_story(self.db, sid, self)
        self.db.commit()
        return touched


def story_articles(db: sqlite3.Connection, sid: int) -> list[sqlite3.Row]:
    return db.execute("SELECT * FROM articles WHERE story_id=? ORDER BY published DESC",
                      (sid,)).fetchall()


def refresh_story(db: sqlite3.Connection, sid: int, clusterer: Clusterer | None = None) -> None:
    """Recalcule compteurs, date et titre représentatif d'une histoire."""
    arts = story_articles(db, sid)
    if not arts:
        db.execute("DELETE FROM stories WHERE id=? AND notified_at IS NULL", (sid,))
        return
    keywords = sorted({k for a in arts for k in a["keywords"].split(",") if k})
    row = db.execute("SELECT title, summary FROM stories WHERE id=?", (sid,)).fetchone()
    title = arts[-1]["clean_title"]
    if clusterer and len(arts) > 2:
        vecs = [clusterer.vector(a["tokens"]) for a in arts]
        centroid = Counter()
        for v in vecs:
            centroid.update(v)
        centroid = normalize_vec(dict(centroid))
        title = max(zip(arts, vecs), key=lambda p: cosine(p[1], centroid))[0]["clean_title"]
    # Un titre rédigé par Claude (stocké avec le résumé) a la priorité.
    if row and row["summary"]:
        title = row["title"] or title
    db.execute(
        "UPDATE stories SET n_articles=?, n_sources=?, last_article_at=?, title=?, keywords=? "
        "WHERE id=?",
        (len(arts), len({a["domain"] for a in arts}), max(a["published"] for a in arts),
         title, ",".join(keywords), sid))


# -------------------------------------------------------------------------- Claude

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "same_story": {"type": "array", "items": {"type": "integer"}},
        "title": {"type": "string"},
        "summary": {"type": "string"},
    },
    "required": ["same_story", "title", "summary"],
    "additionalProperties": False,
}

# Modèles qui acceptent le repli automatique côté serveur en cas de refus.
FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}


def claude_review(cfg: dict, arts: list[sqlite3.Row]) -> dict | None:
    """Demande à Claude quels titres décrivent vraiment le même événement, plus un
    titre et un résumé en français. Renvoie None en cas d'échec (on continue sans)."""
    try:
        import anthropic
    except ImportError:
        log.warning("Le paquet 'anthropic' n'est pas installé : vérification Claude ignorée.")
        return None
    lines = "\n".join(
        f"[{i}] {a['clean_title']} ({a['source']}, {a['published'][:10]})"
        for i, a in enumerate(arts))
    prompt = (
        "These news headlines were grouped automatically because they look similar.\n\n"
        f"{lines}\n\n"
        "Return in same_story the indexes of the largest set of headlines that report on the "
        "same specific real-world event or story (the same incident, announcement or case, "
        "not merely the same topic). Then, in French, write a short title (at most 12 words) "
        "and a factual 2-3 sentence summary of that story. Base the summary only on the "
        "headlines and do not invent details they do not contain.")
    model = cfg["claude"].get("model") or "claude-opus-5-5"
    params = {
        "model": model,
        "max_tokens": 4000,
        "output_config": {"effort": "low",
                          "format": {"type": "json_schema", "schema": REVIEW_SCHEMA}},
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        client = anthropic.Anthropic(api_key=cfg["claude"].get("api_key") or None)
        if model in FALLBACK_MODELS:
            resp = client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **params)
        else:
            resp = client.messages.create(**params)
    except anthropic.AuthenticationError:
        log.error("Clé API Anthropic invalide : vérification Claude ignorée.")
        return None
    except anthropic.RateLimitError:
        log.warning("Limite de requêtes Anthropic atteinte : vérification Claude ignorée.")
        return None
    except anthropic.APIStatusError as e:
        log.warning("Erreur API Anthropic %s : %s", e.status_code, e.message)
        return None
    except anthropic.APIConnectionError:
        log.warning("Connexion à l'API Anthropic impossible.")
        return None
    except Exception as e:  # SDK trop ancien, etc. : on continue sans Claude
        log.warning("Vérification Claude impossible : %s", e)
        return None
    if resp.stop_reason != "end_turn":
        log.warning("Réponse Claude inutilisable (stop_reason=%s).", resp.stop_reason)
        return None
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log.warning("Réponse Claude illisible.")
        return None
    data["same_story"] = sorted({i for i in data["same_story"] if 0 <= i < len(arts)})
    return data


# ------------------------------------------------------------------------ Telegram

class Telegram:
    def __init__(self, token: str, chat_id: str = ""):
        self.base = f"{TELEGRAM_API}/bot{token}/"
        self.chat_id = str(chat_id)

    def call(self, method: str, http_timeout: float = 20, **params):
        try:
            raw = http_request(self.base + method, params, timeout=http_timeout)
        except urllib.error.HTTPError as e:
            raw = e.read()
        data = json.loads(raw)
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method} : {data.get('description')}")
        return data["result"]

    def send(self, text: str, reply_to: int | None = None) -> int:
        params = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
                  "link_preview_options": {"is_disabled": True}}
        if reply_to:
            params["reply_parameters"] = {"message_id": reply_to,
                                          "allow_sending_without_reply": True}
        return self.call("sendMessage", **params)["message_id"]


def telegram_token(cfg: dict) -> str:
    # Dans le cloud, les secrets arrivent par variables d'environnement et ne sont
    # jamais écrits dans config.json.
    return os.environ.get("TELEGRAM_TOKEN") or cfg["telegram"].get("token") or ""


def telegram_from(cfg: dict, db: sqlite3.Connection | None = None) -> Telegram | None:
    chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or cfg["telegram"].get("chat_id")
               or (meta_get(db, "tg_chat_id") if db else None))
    token = telegram_token(cfg)
    if token and chat_id:
        return Telegram(token, chat_id)
    return None


def claim_chat(db: sqlite3.Connection, token: str) -> Telegram | None:
    """Sans conversation configurée, la première personne qui envoie /start au bot
    en devient la propriétaire (utile quand le bot tourne dans le cloud)."""
    tg = Telegram(token)
    offset = int(meta_get(db, "tg_offset", "0"))
    for u in tg.call("getUpdates", offset=offset, timeout=0, allowed_updates=["message"]):
        offset = u["update_id"] + 1
        msg = u.get("message") or {}
        if (msg.get("text") or "").startswith("/start") and msg.get("chat"):
            tg.chat_id = str(msg["chat"]["id"])
            meta_set(db, "tg_chat_id", tg.chat_id)
            break
    meta_set(db, "tg_offset", offset)
    if not tg.chat_id:
        return None
    log.info("Conversation Telegram liée (%s).", tg.chat_id)
    return tg


def ensure_commands(db: sqlite3.Connection, tg: Telegram) -> None:
    if meta_get(db, "tg_commands_set") == tg.chat_id:
        return
    tg.call("setMyCommands",
            commands=[{"command": c, "description": d} for c, d in BOT_COMMANDS])
    meta_set(db, "tg_commands_set", tg.chat_id)


def claude_enabled(cfg: dict) -> bool:
    return bool(cfg["claude"].get("enabled") or os.environ.get("ANTHROPIC_API_KEY"))


EMOJI = {"dog": "🐶", "cat": "🐱", "elephant": "🐘", "lion": "🦁", "tiger": "🐯",
         "bear": "🐻", "horse": "🐴", "shark": "🦈", "whale": "🐋", "dolphin": "🐬",
         "monkey": "🐒", "bird": "🐦", "snake": "🐍", "wolf": "🐺", "fox": "🦊",
         "panda": "🐼", "penguin": "🐧", "cow": "🐄", "pig": "🐷", "rabbit": "🐰",
         "giraffe": "🦒", "zebra": "🦓", "crocodile": "🐊", "kangaroo": "🦘", "koala": "🐨"}


TELEGRAM_LIMIT = 3800  # Telegram limite à 4096 caractères visibles (hors balises et liens)


def esc(s: str) -> str:
    return html.escape(s or "", quote=False)


def visible_len(s: str) -> int:
    return len(html.unescape(re.sub(r"<[^>]+>", "", s)))


def format_alert(story: sqlite3.Row, arts: list[sqlite3.Row], kind: str) -> str:
    keywords = [k for k in story["keywords"].split(",") if k]
    icon = next((EMOJI[k.lower()] for k in keywords if k.lower() in EMOJI), "📰")
    head = "Nouvelle histoire" if kind == "new" else "Mise à jour : l'histoire prend de l'ampleur"
    lines = [f"{icon} <b>{head}</b> · {esc(', '.join(keywords))}",
             "", f"<b>{esc(story['title'])}</b>"]
    if story["summary"]:
        lines += ["", esc(story["summary"])]
    lines += ["", f"📰 {story['n_articles']} articles · {story['n_sources']} sources distinctes"
                  f" · histoire #{story['id']}", ""]
    seen_domains = set()
    shown = 0
    text = "\n".join(lines)
    for a in arts:
        if a["domain"] in seen_domains:
            continue
        line = (f"• <a href=\"{html.escape(a['url'], quote=True)}\">{esc(a['clean_title'])}</a>"
                f" — <i>{esc(a['source'])}</i>")
        if visible_len(text) + visible_len(line) > TELEGRAM_LIMIT:
            break
        seen_domains.add(a["domain"])
        text += line + "\n"
        shown += 1
    rest = story["n_sources"] - shown
    if rest > 0:
        text += f"… et {rest} autre(s) source(s). /histoire {story['id']}\n"
    return text.rstrip()


# --------------------------------------------------------------------------- scan

def scan(db: sqlite3.Connection, cfg: dict, tg: Telegram | None) -> dict:
    now = utcnow()
    last = meta_get(db, "last_scan")
    lookback = int(cfg["lookback_hours"])
    if last:
        hours = math.ceil((now - parse_iso(last)).total_seconds() / 3600) + 1
        hours = max(1, min(lookback, hours))
    else:
        hours = lookback
    oldest = now - timedelta(hours=cfg["story_window_hours"])
    blocked = [d.lower() for d in cfg["blocked_domains"]]

    new_ids: list[str] = []
    ok = 0
    for i, kw in enumerate(cfg["keywords"]):
        if i:
            time.sleep(float(cfg["request_delay_seconds"]))
        try:
            items = fetch_google_news(kw, hours, cfg)
            ok += 1
        except Exception as e:
            log.warning("Recherche « %s » impossible : %s", kw, e)
            continue
        for it in items:
            if is_blocked(it["domain"], blocked) or it["published"] < oldest:
                continue
            row = db.execute("SELECT keywords FROM articles WHERE id=?", (it["id"],)).fetchone()
            if row:
                kws = [k for k in row["keywords"].split(",") if k]
                if kw not in kws:
                    db.execute("UPDATE articles SET keywords=? WHERE id=?",
                               (",".join(kws + [kw]), it["id"]))
                continue
            ct = clean_title(it["title"], it["source"])
            if db.execute("SELECT 1 FROM articles WHERE domain=? AND clean_title=? AND first_seen>=?",
                          (it["domain"], ct, iso(oldest))).fetchone():
                continue  # même article republié par le même média
            db.execute(
                "INSERT INTO articles(id, title, clean_title, url, source, domain, published, "
                "first_seen, tokens, keywords) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (it["id"], it["title"], ct, it["url"], it["source"], it["domain"],
                 iso(it["published"]), iso(now), " ".join(sorted(set(tokenize(ct)))), kw))
            new_ids.append(it["id"])
    db.commit()
    if ok:
        meta_set(db, "last_scan", iso(now))
        meta_set(db, "fail_streak", 0)
    elif cfg["keywords"]:
        streak = int(meta_get(db, "fail_streak", "0")) + 1
        meta_set(db, "fail_streak", streak)
        if streak == 3 and tg:
            try:
                tg.send("⚠️ Google News ne répond plus depuis 3 scans. Je continue d'essayer ; "
                        "si ça dure, regarde le journal.")
            except Exception as e:
                log.warning("Avertissement Telegram impossible : %s", e)

    clusterer = Clusterer(db, cfg)
    clusterer.run(new_ids)
    sent = evaluate_alerts(db, cfg, tg, clusterer)
    purge_old(db, cfg)
    try:
        write_history(db, cfg)
    except OSError as e:
        log.warning("Historique HTML non écrit : %s", e)
    log.info("Scan terminé : %d/%d recherches OK, %d nouveaux articles, %d alerte(s), fenêtre %dh",
             ok, len(cfg["keywords"]), len(new_ids), sent, hours)
    return {"new_articles": len(new_ids), "alerts": sent, "ok": ok}


def evaluate_alerts(db: sqlite3.Connection, cfg: dict, tg: Telegram | None,
                    clusterer: Clusterer) -> int:
    min_sources = int(cfg["min_sources"])
    renotify = int(cfg["renotify_new_sources"])
    use_claude = claude_enabled(cfg)
    window = iso(utcnow() - timedelta(hours=cfg["story_window_hours"]))
    candidates = db.execute(
        "SELECT * FROM stories WHERE last_article_at >= ? AND ("
        " (notified_at IS NULL AND n_sources >= ?) OR"
        " (notified_at IS NOT NULL AND n_sources >= notified_sources + ?))"
        " ORDER BY n_sources DESC", (window, min_sources, renotify)).fetchall()
    sent = 0
    for story in candidates:
        if sent >= int(cfg["max_alerts_per_scan"]):
            break
        sid = story["id"]
        kind = "new" if story["notified_at"] is None else "update"
        if use_claude and story["n_sources"] <= story["llm_checked_sources"]:
            continue  # déjà vérifiée, aucune nouvelle source depuis
        arts = story_articles(db, sid)
        if use_claude:
            review = claude_review(cfg, arts)
            if review is not None:
                keep = set(review["same_story"])
                for i, a in enumerate(arts):
                    if i not in keep:
                        db.execute("UPDATE articles SET story_id=NULL WHERE id=?", (a["id"],))
                db.execute("UPDATE stories SET title=?, summary=? WHERE id=?",
                           (review["title"].strip(), review["summary"].strip(), sid))
                refresh_story(db, sid, clusterer)
                n = db.execute("SELECT n_sources FROM stories WHERE id=?", (sid,)).fetchone()
                n_sources = n["n_sources"] if n else 0
                db.execute("UPDATE stories SET llm_checked_sources=? WHERE id=?", (n_sources, sid))
                db.commit()
                needed = min_sources if kind == "new" else story["notified_sources"] + renotify
                if n_sources < needed:
                    log.info("Histoire #%d écartée par Claude (%d sources confirmées).",
                             sid, n_sources)
                    continue
        story = db.execute("SELECT * FROM stories WHERE id=?", (sid,)).fetchone()
        arts = story_articles(db, sid)
        text = format_alert(story, arts, kind)
        msg_id = None
        if tg:
            try:
                msg_id = tg.send(text, reply_to=story["tg_message_id"])
            except Exception as e:
                log.error("Envoi Telegram impossible pour l'histoire #%d : %s", sid, e)
                continue  # on réessaiera au prochain scan
        else:
            print("\n" + re.sub(r"<[^>]+>", "", html.unescape(text)) + "\n")
        db.execute(
            "UPDATE stories SET notified_at=?, notified_sources=?, "
            "tg_message_id=COALESCE(tg_message_id, ?) WHERE id=?",
            (iso(utcnow()), story["n_sources"], msg_id, sid))
        db.commit()
        sent += 1
        log.info("Alerte %s : #%d « %s » (%d sources)", kind, sid, story["title"],
                 story["n_sources"])
    return sent


def purge_old(db: sqlite3.Connection, cfg: dict) -> None:
    cutoff = iso(utcnow() - timedelta(days=int(cfg["retention_days"])))
    db.execute(
        "DELETE FROM articles WHERE first_seen < ? AND (story_id IS NULL OR story_id IN "
        "(SELECT id FROM stories WHERE notified_at IS NULL))", (cutoff,))
    db.execute("DELETE FROM stories WHERE notified_at IS NULL AND id NOT IN "
               "(SELECT DISTINCT story_id FROM articles WHERE story_id IS NOT NULL)")
    db.commit()


# ------------------------------------------------------------------------ history

def write_history(db: sqlite3.Connection, cfg: dict) -> None:
    stories = db.execute(
        "SELECT * FROM stories WHERE n_sources >= 2 OR notified_at IS NOT NULL "
        "ORDER BY COALESCE(notified_at, last_article_at) DESC LIMIT 300").fetchall()
    cards = []
    for s in stories:
        arts = story_articles(db, s["id"])
        items = "".join(
            f'<li><a href="{html.escape(a["url"])}" target="_blank" rel="noopener">'
            f'{html.escape(a["clean_title"])}</a> <span class="src">{html.escape(a["source"])}'
            f' · {local_str(a["published"])}</span></li>' for a in arts)
        badge = (f'<span class="badge on">alerte {local_str(s["notified_at"])}</span>'
                 if s["notified_at"] else '<span class="badge">sous le seuil</span>')
        summary = f'<p class="sum">{html.escape(s["summary"])}</p>' if s["summary"] else ""
        cards.append(
            f'<details class="card{" hot" if s["notified_at"] else ""}" '
            f'data-q="{html.escape((s["title"] + " " + s["keywords"]).lower())}">'
            f'<summary><span class="t">#{s["id"]} · {html.escape(s["title"])}</span>'
            f'<span class="m">{html.escape(s["keywords"])} · {s["n_sources"]} sources · '
            f'{s["n_articles"]} articles {badge}</span></summary>{summary}<ol>{items}</ol>'
            f'</details>')
    doc = f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NewsBot · Historique</title>
<style>
:root {{ --bg:#f7f7f5; --card:#fff; --fg:#1d1d1f; --muted:#6e6e73; --line:#e3e3e0; --accent:#c2410c; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#161616; --card:#212121; --fg:#ececec; --muted:#9a9a9a; --line:#333; --accent:#fb923c; }} }}
body {{ margin:0; background:var(--bg); color:var(--fg); font:15px/1.5 -apple-system, system-ui, sans-serif; }}
main {{ max-width:860px; margin:0 auto; padding:24px 16px 64px; }}
h1 {{ font-size:22px; margin:0 0 4px; }}
.meta {{ color:var(--muted); margin:0 0 16px; }}
input {{ width:100%; box-sizing:border-box; padding:10px 12px; border:1px solid var(--line);
  border-radius:8px; background:var(--card); color:var(--fg); font:inherit; margin-bottom:16px; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:10px; margin:0 0 10px; padding:12px 14px; }}
.card.hot {{ border-left:4px solid var(--accent); }}
summary {{ cursor:pointer; list-style:none; }}
summary::-webkit-details-marker {{ display:none; }}
.t {{ display:block; font-weight:600; }}
.m, .src {{ color:var(--muted); font-size:13px; }}
.badge {{ font-size:12px; border:1px solid var(--line); border-radius:99px; padding:1px 8px; margin-left:6px; }}
.badge.on {{ color:var(--accent); border-color:var(--accent); }}
.sum {{ margin:10px 0 0; }}
ol {{ margin:10px 0 0; padding-left:20px; }}
li {{ margin:4px 0; }}
a {{ color:inherit; }}
</style></head><body><main>
<h1>Historique NewsBot</h1>
<p class="meta">Mis à jour le {datetime.now().strftime("%d/%m/%Y à %H:%M")} · mots-clés :
{html.escape(", ".join(cfg["keywords"]))} · seuil : {cfg["min_sources"]} sources</p>
<input id="q" placeholder="Filtrer les histoires…" autocomplete="off">
{"".join(cards) or "<p class='meta'>Aucune histoire pour l'instant.</p>"}
</main><script>
document.getElementById('q').addEventListener('input', e => {{
  const q = e.target.value.toLowerCase();
  document.querySelectorAll('.card').forEach(c => c.hidden = !c.dataset.q.includes(q));
}});
</script></body></html>"""
    tmp = HISTORY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(doc)
    os.replace(tmp, HISTORY_PATH)


# ---------------------------------------------------------------- bot commands

HELP = """<b>NewsBot</b> surveille Google News toutes les {interval} min.

/liste : mots-clés et réglages
/ajouter dog, red panda : ajouter des mots-clés (séparés par des virgules)
/retirer cat : retirer des mots-clés
/seuil 3 : nombre minimum de sources distinctes
/histoires : dernières histoires signalées
/histoire 12 : articles de l'histoire #12
/scan : lancer une recherche maintenant
/statut : état du service"""

BOT_COMMANDS = [
    ("liste", "Mots-clés et réglages"), ("ajouter", "Ajouter des mots-clés"),
    ("retirer", "Retirer des mots-clés"), ("seuil", "Nombre minimum de sources"),
    ("histoires", "Dernières histoires signalées"), ("histoire", "Articles d'une histoire"),
    ("scan", "Rechercher maintenant"), ("statut", "État du service"), ("aide", "Aide"),
]
ALIASES = {"start": "aide", "help": "aide", "list": "liste", "add": "ajouter",
           "remove": "retirer", "threshold": "seuil", "stories": "histoires",
           "story": "histoire", "status": "statut"}


def split_keywords(arg: str) -> list[str]:
    parts = arg.split(",") if "," in arg else arg.split()
    return [p.strip().lower() for p in parts if p.strip()]


def handle_command(text: str, cfg: dict, db: sqlite3.Connection) -> tuple[str, bool]:
    """Renvoie (réponse HTML, faut-il lancer un scan maintenant)."""
    m = re.match(r"/(\w+)(?:@\w+)?\s*(.*)", text.strip(), re.S)
    if not m:
        return "Commande inconnue. /aide pour la liste.", False
    cmd, arg = m.group(1).lower(), m.group(2).strip()
    cmd = ALIASES.get(cmd, cmd)

    if cmd == "aide":
        return HELP.format(interval=cfg["interval_minutes"]), False
    if cmd == "liste":
        kws = ", ".join(cfg["keywords"]) or "(aucun)"
        claude = "activé" if claude_enabled(cfg) else "désactivé"
        return (f"<b>Mots-clés :</b> {esc(kws)}\n<b>Seuil :</b> {cfg['min_sources']} sources "
                f"distinctes\n<b>Nouvelle alerte si :</b> +{cfg['renotify_new_sources']} sources"
                f"\n<b>Fréquence :</b> {cfg['interval_minutes']} min\n<b>Claude :</b> {claude}"), False
    if cmd in ("ajouter", "retirer"):
        words = split_keywords(arg)
        if not words:
            return f"Exemple : /{cmd} dog, red panda", False
        current = [k.lower() for k in cfg["keywords"]]
        if cmd == "ajouter":
            added = [w for w in words if w not in current]
            cfg["keywords"] = cfg["keywords"] + added
            msg = f"Ajouté : {esc(', '.join(added))}" if added else "Déjà dans la liste."
        else:
            removed = [w for w in words if w in current]
            cfg["keywords"] = [k for k in cfg["keywords"] if k.lower() not in words]
            msg = f"Retiré : {esc(', '.join(removed))}" if removed else "Aucun de ces mots-clés."
        save_config(cfg)
        return f"{msg}\nMots-clés : {esc(', '.join(cfg['keywords']) or '(aucun)')}", cmd == "ajouter"
    if cmd == "seuil":
        if not arg.isdigit() or int(arg) < 2:
            return "Exemple : /seuil 3 (minimum 2)", False
        cfg["min_sources"] = int(arg)
        save_config(cfg)
        return f"Seuil réglé à {arg} sources distinctes.", True
    if cmd == "histoires":
        rows = db.execute("SELECT * FROM stories WHERE notified_at IS NOT NULL "
                          "ORDER BY notified_at DESC LIMIT 10").fetchall()
        if not rows:
            return "Aucune histoire signalée pour l'instant.", False
        text = "\n".join(f"#{r['id']} · {esc(r['title'])} ({r['n_sources']} sources, "
                         f"{local_str(r['notified_at'])})" for r in rows)
        if HISTORY_URL:
            text += f"\n\nHistorique complet : {esc(HISTORY_URL)}"
        return text, False
    if cmd == "histoire":
        if not arg.lstrip("#").isdigit():
            return "Exemple : /histoire 12", False
        s = db.execute("SELECT * FROM stories WHERE id=?", (int(arg.lstrip("#")),)).fetchone()
        if not s:
            return "Histoire introuvable.", False
        arts = story_articles(db, s["id"])
        lines = [f"<b>#{s['id']} · {esc(s['title'])}</b>"]
        if s["summary"]:
            lines.append(esc(s["summary"]))
        lines.append("")
        text = "\n".join(lines)
        for a in arts:
            line = (f"• <a href=\"{html.escape(a['url'], quote=True)}\">"
                    f"{esc(a['clean_title'])}</a> — <i>{esc(a['source'])}</i>")
            if visible_len(text) + visible_len(line) > TELEGRAM_LIMIT:
                text += "…"
                break
            text += "\n" + line
        return text, False
    if cmd == "scan":
        return "Recherche lancée…", True
    if cmd == "statut":
        last = meta_get(db, "last_scan")
        n_st = db.execute("SELECT COUNT(*) c FROM stories WHERE notified_at IS NOT NULL").fetchone()["c"]
        n_art = db.execute("SELECT COUNT(*) c FROM articles").fetchone()["c"]
        return (f"Dernier scan : {local_str(last)}\nArticles en mémoire : {n_art}\n"
                f"Histoires signalées : {n_st}"), False
    return "Commande inconnue. /aide pour la liste.", False


def poll_telegram(db: sqlite3.Connection, cfg: dict, tg: Telegram, timeout: int) -> bool:
    offset = int(meta_get(db, "tg_offset", "0"))
    updates = tg.call("getUpdates", http_timeout=timeout + 15, offset=offset,
                      timeout=timeout, allowed_updates=["message"])
    force = False
    for u in updates:
        offset = u["update_id"] + 1
        msg = u.get("message") or {}
        text = msg.get("text") or ""
        if str(msg.get("chat", {}).get("id")) != tg.chat_id or not text.startswith("/"):
            continue  # on ignore tout ce qui ne vient pas de ta conversation
        try:
            reply, scan_now = handle_command(text, cfg, db)
        except Exception as e:
            log.exception("Commande « %s » en échec", text)
            reply, scan_now = f"Erreur : {esc(str(e))}", False
        force = force or scan_now
        try:
            tg.send(reply)
        except Exception as e:
            log.warning("Réponse Telegram impossible : %s", e)
    meta_set(db, "tg_offset", offset)
    return force


# ---------------------------------------------------------------------- service

def acquire_lock():
    f = open(LOCK_PATH, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("NewsBot tourne déjà (service en arrière-plan). Envoie /scan sur Telegram, "
              "ou arrête le service avant de relancer.", file=sys.stderr)
        sys.exit(1)
    return f


def run_forever() -> None:
    lock = acquire_lock()  # noqa: F841 (gardé ouvert pendant toute l'exécution)
    setup_logging(to_console=sys.stdout.isatty())
    db = open_db()
    log.info("NewsBot démarré (dossier %s)", HOME)
    next_scan = 0.0
    force = False
    while True:
        try:
            cfg = load_config()
        except (OSError, json.JSONDecodeError) as e:
            log.error("config.json illisible : %s (nouvel essai dans 60 s)", e)
            time.sleep(60)
            continue
        tg = telegram_from(cfg, db)
        if tg:
            try:
                ensure_commands(db, tg)
            except Exception as e:
                log.warning("setMyCommands : %s", e)
        if force or time.time() >= next_scan:
            force = False
            try:
                scan(db, cfg, tg)
            except Exception:
                log.exception("Scan en échec")
            next_scan = time.time() + max(1, float(cfg["interval_minutes"])) * 60
        wait = int(max(1, min(25, next_scan - time.time())))
        if tg:
            try:
                force = poll_telegram(db, cfg, tg, wait)
            except Exception as e:
                log.warning("Telegram injoignable : %s", e)
                time.sleep(min(15, wait))
        else:
            time.sleep(wait)


# ------------------------------------------------------------------------- setup

def cmd_setup() -> None:
    cfg = load_config()
    db = open_db()
    print("\n=== Configuration de NewsBot ===\n")
    print("1) Telegram. Dans Telegram, ouvre @BotFather, envoie /newbot, choisis un nom,\n"
          "   puis copie le token qu'il te donne (format 123456789:ABC...).")
    current = cfg["telegram"].get("token")
    while True:
        token = input(f"   Token du bot{' [Entrée = garder l actuel]' if current else ''} : ").strip()
        token = token or current
        if not token:
            continue
        try:
            me = Telegram(token).call("getMe")
            break
        except Exception as e:
            print(f"   Token refusé ({e}). Réessaie.")
            current = ""
    tg = Telegram(token)
    print(f"\n   Bot trouvé : @{me['username']}.")
    print(f"   Ouvre https://t.me/{me['username']} et envoie-lui /start (j'attends 3 minutes)…")
    chat_id = None
    offset = 0
    deadline = time.time() + 180
    while time.time() < deadline and not chat_id:
        for u in tg.call("getUpdates", http_timeout=40, offset=offset, timeout=25):
            offset = u["update_id"] + 1
            chat = (u.get("message") or {}).get("chat")
            if chat:
                chat_id = str(chat["id"])
    if not chat_id:
        print("   Aucun message reçu. Relance « setup » quand tu es prêt.")
        sys.exit(1)
    tg.call("getUpdates", offset=offset, timeout=0)  # vide la file d'attente
    meta_set(db, "tg_offset", offset)
    cfg["telegram"] = {"token": token, "chat_id": chat_id}
    print("   Conversation liée.\n")

    kws = input(f"2) Mots-clés séparés par des virgules [{', '.join(cfg['keywords'])}] : ").strip()
    if kws:
        cfg["keywords"] = [k.strip().lower() for k in kws.split(",") if k.strip()]
    th = input(f"3) Sources distinctes minimum pour une alerte [{cfg['min_sources']}] : ").strip()
    if th.isdigit() and int(th) >= 2:
        cfg["min_sources"] = int(th)

    print("\n4) Optionnel : une clé API Anthropic (console.anthropic.com) permet à Claude de\n"
          "   confirmer que les articles parlent bien du même événement et d'écrire un résumé\n"
          "   en français. Coût : environ 1 centime par alerte. Entrée pour ignorer.")
    key = getpass.getpass("   Clé API (masquée) : ").strip()
    if key:
        cfg["claude"]["api_key"] = key
        cfg["claude"]["enabled"] = True
    elif cfg["claude"].get("api_key"):
        keep = input("   Garder la clé déjà enregistrée ? [O/n] : ").strip().lower()
        cfg["claude"]["enabled"] = keep != "n"
    save_config(cfg)
    tg.chat_id = chat_id
    tg.send("✅ <b>NewsBot est connecté.</b>\n" + HELP.format(interval=cfg["interval_minutes"]))
    print(f"\nConfiguration enregistrée dans {CONFIG_PATH}. Message de test envoyé sur Telegram.\n")


def cmd_once() -> None:
    """Un passage complet : commandes Telegram en attente, puis scan. C'est le mode
    utilisé par GitHub Actions, qui relance le script toutes les 5 minutes."""
    lock = acquire_lock()  # noqa: F841
    setup_logging()
    cfg = load_config()
    db = open_db()
    tg = telegram_from(cfg, db)
    try:
        if not tg and telegram_token(cfg):
            tg = claim_chat(db, telegram_token(cfg))
            if tg:
                tg.send("✅ <b>NewsBot est connecté.</b>\n"
                        + HELP.format(interval=cfg["interval_minutes"]))
        if tg:
            ensure_commands(db, tg)
            poll_telegram(db, cfg, tg, 0)
    except Exception as e:
        log.warning("Telegram injoignable : %s", e)
    result = scan(db, cfg, tg)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"alerts={result['alerts']}\n")
    print(f"Historique : {HISTORY_PATH}")


def cmd_history(limit: int) -> None:
    db = open_db()
    rows = db.execute("SELECT * FROM stories WHERE notified_at IS NOT NULL "
                      "ORDER BY notified_at DESC LIMIT ?", (limit,)).fetchall()
    if not rows:
        print("Aucune histoire signalée pour l'instant.")
    for r in rows:
        print(f"#{r['id']:<5} {local_str(r['notified_at'])}  {r['n_sources']:>2} sources  "
              f"[{r['keywords']}]  {r['title']}")
    print(f"\nHistorique complet : open \"{HISTORY_PATH}\"")


def cmd_story(sid: int) -> None:
    db = open_db()
    s = db.execute("SELECT * FROM stories WHERE id=?", (sid,)).fetchone()
    if not s:
        print("Histoire introuvable.")
        return
    print(f"#{s['id']} {s['title']}\n{s['summary'] or ''}\n")
    for a in story_articles(db, sid):
        print(f"- {local_str(a['published'])}  {a['source']}: {a['clean_title']}\n  {a['url']}")


def main() -> None:
    p = argparse.ArgumentParser(description="NewsBot : alertes Google News multi-sources")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup")
    sub.add_parser("run")
    sub.add_parser("once")
    h = sub.add_parser("history")
    h.add_argument("--limit", type=int, default=30)
    s = sub.add_parser("story")
    s.add_argument("id", type=int)
    sub.add_parser("test-notify")
    args = p.parse_args()

    if args.cmd == "setup":
        cmd_setup()
    elif args.cmd == "run":
        run_forever()
    elif args.cmd == "once":
        cmd_once()
    elif args.cmd == "history":
        cmd_history(args.limit)
    elif args.cmd == "story":
        cmd_story(args.id)
    elif args.cmd == "test-notify":
        tg = telegram_from(load_config(), open_db())
        if not tg:
            sys.exit("Telegram n'est pas configuré : lance d'abord « setup ».")
        tg.send("🔔 Test NewsBot : les notifications fonctionnent.")
        print("Message envoyé.")


if __name__ == "__main__":
    main()
