#!/usr/bin/env python3
"""Good Feed builder.

GitHub runs this every hour. At each batch time (see config.json) it:
  1. checks every source in sources.json and adds new posts to a 2-week pool,
  2. picks a mixed batch from the pool, following the caps in config.json,
  3. mixes in memory cards (memory.txt) and the day's True Vine meditation,
  4. writes feed.xml, which Inoreader reads.

Day to day you only touch sources.json, config.json and memory.txt
(or just ask Claude to).
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
CONFIG, SOURCES, MEMORY = ROOT / "config.json", ROOT / "sources.json", ROOT / "memory.txt"
STATE, STATUS, FEED = ROOT / "state.json", ROOT / "status.json", ROOT / "feed.xml"
CACHE = ROOT / "truevine"
UTC = timezone.utc

CATEGORY_LABEL = {
    "leaders": ("\U0001F3A5", "Leaders"), "news": ("☀️", "Good News"),
    "missions": ("\U0001F30D", "Missions"), "business": ("\U0001F4BC", "Business"),
    "politics": ("\U0001F3DB️", "Politics"), "entertainment": ("\U0001F3AC", "Entertainment"),
}

DEFAULTS = {
    "feed_title": "Good Feed", "timezone": "America/Chicago", "posts_per_day": 40,
    "batch_hours": [6, 9, 12, 15, 18, 21], "pool_days": 14,
    "person_cap": 5, "category_caps": {}, "default_category_cap": 5, "podcast_cap": 2,
    "memory_cards_per_day": 4, "memory_styles": {"verse": 1, "blanks": 1, "where": 1},
    "scripture_notice": "", "true_vine": True, "true_vine_repeat": True, "warn_after_days": 3,
}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def base_url() -> str:
    if os.environ.get("FEED_BASE_URL"):
        return os.environ["FEED_BASE_URL"].rstrip("/") + "/"
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in repo:
        owner, name = repo.split("/", 1)
        if name.lower() == f"{owner.lower()}.github.io":
            return f"https://{owner.lower()}.github.io/"
        return f"https://{owner.lower()}.github.io/{name}/"
    return "https://example.github.io/good-feed/"


def uid(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------
# Fetching and parsing
# --------------------------------------------------------------------------
UA = ("Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0 Mobile Safari/537.36 GoodFeed/1.0")


def fetch(url: str, timeout=25, tries=2) -> str:
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
                "Cookie": "CONSENT=YES+1; SOCS=CAI"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read(4_000_000)
                cs = r.headers.get_content_charset()
            for enc in filter(None, [cs, "utf-8", "cp1252", "latin-1"]):
                try:
                    return data.decode(enc)
                except (UnicodeDecodeError, LookupError):
                    continue
            return data.decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001 - any network trouble
            last = e
            if i + 1 < tries:
                time.sleep(2)
    raise last


def plain(s: str | None) -> str:
    if not s:
        return ""
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def clip(s: str, n=260) -> str:
    s = plain(s)
    if len(s) <= n:
        return s
    cut = s[:n].rsplit(" ", 1)[0].rstrip(",;:-—")
    return cut + "…"


def parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError):
        d = None
    if d is None:
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def kids(el, name):
    return [c for c in el if local(c.tag) == name]


def kid_text(el, *names) -> str:
    for n in names:
        for c in kids(el, n):
            if c.text and c.text.strip():
                return c.text.strip()
    return ""


IMG_IN_HTML = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.I)


def first_img(s: str) -> str:
    m = IMG_IN_HTML.search(s or "")
    return html.unescape(m.group(1)) if m else ""


def parse_feed(text: str) -> list[dict]:
    """RSS or Atom -> [{id,title,link,published,summary,image,audio,audio_type,video_id}]"""
    text = text.lstrip("﻿ \r\n\t")
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        fixed = re.sub(r"&(?!(?:[a-zA-Z]+|#\d+|#x[0-9a-fA-F]+);)", "&amp;", text)
        fixed = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", fixed)
        root = ET.fromstring(fixed)
    out = []
    if local(root.tag) == "feed":  # Atom (YouTube uses this)
        for e in kids(root, "entry"):
            link = ""
            for l in kids(e, "link"):
                if l.get("rel", "alternate") == "alternate":
                    link = l.get("href", "")
                    break
            img, desc = "", kid_text(e, "summary", "content")
            for g in kids(e, "group"):
                for t in kids(g, "thumbnail"):
                    img = img or t.get("url", "")
                desc = desc or kid_text(g, "description")
            out.append({"id": kid_text(e, "id") or link, "title": plain(kid_text(e, "title")),
                        "link": link, "published": kid_text(e, "published", "updated"),
                        "summary": desc, "image": img or first_img(desc),
                        "video_id": kid_text(e, "videoId")})
        return out
    channel = root.find("channel") if local(root.tag) == "rss" else root
    if channel is None:
        channel = next((c for c in root if local(c.tag) == "channel"), root)
    ch_img = ""
    for i in kids(channel, "image"):
        ch_img = ch_img or i.get("href", "") or kid_text(i, "url")
    items = kids(channel, "item") or kids(root, "item")
    for it in items:
        body = kid_text(it, "encoded") or kid_text(it, "description", "summary")
        img = audio = atype = ""
        for c in it:
            n = local(c.tag)
            typ = (c.get("type") or "").lower()
            url = c.get("url") or c.get("href") or ""
            if n == "enclosure" and url:
                if typ.startswith("audio") or url.lower().split("?")[0].endswith((".mp3", ".m4a")):
                    audio, atype = audio or url, atype or (typ or "audio/mpeg")
                elif typ.startswith("image") or not typ:
                    img = img or url
            elif n in ("thumbnail", "image") and url:
                img = img or url
            elif n == "content" and url and (c.get("medium") == "image" or typ.startswith("image")
                                            or re.search(r"\.(jpe?g|png|webp|gif)(\?|$)", url, re.I)):
                img = img or url
            elif n == "group":
                for g in c:
                    gu = g.get("url", "")
                    if local(g.tag) in ("content", "thumbnail") and gu:
                        img = img or gu
        link = kid_text(it, "link")
        if not link:
            for l in kids(it, "link"):
                link = link or l.get("href", "")
        guid = kid_text(it, "guid") or link or kid_text(it, "title")
        out.append({"id": guid, "title": plain(kid_text(it, "title")), "link": link,
                    "published": kid_text(it, "pubDate", "date", "published", "updated"),
                    "summary": body, "image": img or first_img(body) or (ch_img if audio else ""),
                    "audio": audio, "audio_type": atype,
                    "duration": kid_text(it, "duration")})
    return out


def looks_like_feed(text: str) -> bool:
    head = text.lstrip("﻿ \r\n\t")[:600].lower()
    return head.startswith("<?xml") and ("<rss" in head or "<feed" in head or "<rdf" in head) \
        or head.startswith("<rss") or head.startswith("<feed")


class LinkScan(HTMLParser):
    """Collects <link rel=alternate> feeds, <a href> links with their text, and meta tags."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.feeds, self.anchors, self.meta = [], [], {}
        self._a, self._txt = None, []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "link" and "alternate" in (a.get("rel") or "").lower() and \
                re.search(r"(rss|atom)\+xml", a.get("type") or ""):
            self.feeds.append(a.get("href") or "")
        elif tag == "meta":
            k = (a.get("property") or a.get("name") or "").lower()
            if k and a.get("content") and k not in self.meta:
                self.meta[k] = a["content"]
        elif tag == "a" and a.get("href"):
            self._a, self._txt = a["href"], []
        elif tag == "img" and self._a is not None and a.get("alt"):
            self._txt.append(" ")

    def handle_endtag(self, tag):
        if tag == "a" and self._a is not None:
            self.anchors.append((self._a, re.sub(r"\s+", " ", "".join(self._txt)).strip()))
            self._a = None

    def handle_data(self, data):
        if self._a is not None:
            self._txt.append(data)


SKIP_PATH = re.compile(r"/(tag|tags|category|categories|author|authors|page|feed|search|login|"
                       r"subscribe|donate|give|about|contact|privacy|terms|cart|account|events?)(/|$)",
                       re.I)


def scrape_links(page_url: str, page_html: str, limit=12) -> list[dict]:
    """For sites without a feed: treat article-looking links on the page as posts."""
    p = LinkScan()
    try:
        p.feed(page_html)
    except Exception:  # noqa: BLE001
        pass
    base = urllib.parse.urlparse(page_url)
    host = base.netloc.lower().removeprefix("www.")
    base_path = base.path.rstrip("/")
    seen, out = set(), []
    for href, text in p.anchors:
        u = urllib.parse.urlparse(urllib.parse.urljoin(page_url, href))
        if u.scheme not in ("http", "https") or u.netloc.lower().removeprefix("www.") != host:
            continue
        path = u.path.rstrip("/")
        if not path or path == base_path or SKIP_PATH.search(path) or path in seen:
            continue
        slug = path.rsplit("/", 1)[-1]
        words = len(text.split())
        if slug.count("-") < 2 or words < 4 or words > 30:
            continue
        seen.add(path)
        url = urllib.parse.urlunparse((u.scheme, u.netloc, u.path, "", u.query, ""))
        out.append({"id": url, "title": text, "link": url, "published": "", "summary": "", "image": ""})
        if len(out) >= limit:
            break
    return out


def page_meta(url: str) -> dict:
    p = LinkScan()
    p.feed(fetch(url, timeout=20, tries=1))
    m = p.meta
    return {"image": m.get("og:image") or m.get("twitter:image") or "",
            "summary": m.get("og:description") or m.get("description") or "",
            "published": m.get("article:published_time") or ""}


# --------------------------------------------------------------------------
# Source types
# --------------------------------------------------------------------------
def resolve_youtube(spec: str, resolved: dict) -> tuple[str, str]:
    spec = spec.strip()
    key = f"yt:{spec}"
    if key in resolved:
        return resolved[key]["id"], resolved[key]["name"]
    if re.fullmatch(r"UC[\w-]{22}", spec):
        cid, name = spec, spec
    elif spec.startswith("@"):
        page = fetch(f"https://www.youtube.com/{spec}")
        m = (re.search(r'"externalId":"(UC[\w-]{22})"', page)
             or re.search(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"', page)
             or re.search(r'"channelId":"(UC[\w-]{22})"', page))
        if not m:
            raise RuntimeError(f"couldn't find a channel for {spec}")
        cid = m.group(1)
        t = re.search(r'<meta property="og:title" content="([^"]+)"', page)
        name = html.unescape(t.group(1)) if t else spec
    elif spec.startswith("search:"):
        q = urllib.parse.quote_plus(spec[7:])
        page = fetch(f"https://www.youtube.com/results?search_query={q}&sp=EgIQAg%253D%253D")
        m = re.search(r'"channelRenderer":\{"channelId":"(UC[\w-]{22})".{0,400}?"title":\{"simpleText":"([^"]+)"',
                      page, re.S)
        if not m:
            raise RuntimeError(f"no YouTube channel found for '{spec[7:]}'")
        cid, name = m.group(1), json.loads(f'"{m.group(2)}"')
    else:
        raise RuntimeError(f"don't understand youtube '{spec}'")
    resolved[key] = {"id": cid, "name": name}
    return cid, name


def resolve_podcast(spec: str, resolved: dict) -> tuple[str, str]:
    spec = str(spec).strip()
    key = f"pod:{spec}"
    r = resolved.get(key)
    if r and r.get("at", "") > (datetime.now(UTC) - timedelta(days=30)).isoformat():
        return r["url"], r["name"]
    if spec.startswith("http"):
        feed, name = spec, spec
    else:
        api = (f"https://itunes.apple.com/search?media=podcast&limit=3&term={urllib.parse.quote_plus(spec[7:])}"
               if spec.startswith("search:") else f"https://itunes.apple.com/lookup?id={spec}")
        res = [x for x in json.loads(fetch(api)).get("results", []) if x.get("feedUrl")]
        if not res:
            raise RuntimeError(f"no podcast found for '{spec}'")
        feed, name = res[0]["feedUrl"], res[0].get("collectionName", spec)
    resolved[key] = {"url": feed, "name": name, "at": datetime.now(UTC).isoformat()}
    return feed, name


def resolve_page(url: str, resolved: dict) -> tuple[str, str]:
    """-> ('feed', feed_url) or ('page', url). Rechecked weekly."""
    key = f"web:{url}"
    r = resolved.get(key)
    if r and r.get("at", "") > (datetime.now(UTC) - timedelta(days=7)).isoformat():
        return r["mode"], r["url"]
    text = fetch(url)
    mode, target = "page", url
    if looks_like_feed(text):
        mode = "feed"
    else:
        p = LinkScan()
        try:
            p.feed(text)
        except Exception:  # noqa: BLE001
            pass
        path = urllib.parse.urlparse(url).path.strip("/")
        last = path.rsplit("/", 1)[-1].lower()
        feeds = [urllib.parse.urljoin(url, f) for f in p.feeds if f]
        if path:
            feeds = [f for f in feeds if last and last in f.lower()]
            guesses = [url.rstrip("/") + "/feed/"]
        else:
            guesses = [urllib.parse.urljoin(url, g) for g in ("/feed/", "/rss", "/feed.xml", "/rss.xml")]
        for cand in feeds + guesses:
            try:
                t = fetch(cand, timeout=15, tries=1)
            except Exception:  # noqa: BLE001
                continue
            if looks_like_feed(t) and parse_feed(t):
                mode, target = "feed", cand
                break
    resolved[key] = {"mode": mode, "url": target, "at": datetime.now(UTC).isoformat()}
    return mode, target


def collect_source(src: dict, resolved: dict) -> tuple[list[dict], str]:
    """-> (posts, description of where they came from)"""
    name, cat = src["name"], src.get("category", "news")
    person = src.get("person") or name
    posts = []
    if src.get("youtube"):
        cid, ch = resolve_youtube(src["youtube"], resolved)
        feed = f"https://www.youtube.com/feeds/videos.xml?playlist_id=UUSH{cid[2:]}"
        for e in parse_feed(fetch(feed, tries=3)):
            vid = e.get("video_id") or e["link"].rsplit("=", 1)[-1]
            posts.append({**e, "id": f"yt:{vid}", "kind": "video", "video_id": vid,
                          "link": f"https://www.youtube.com/shorts/{vid}",
                          "image": e["image"] or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"})
        where = f"YouTube Shorts: {ch} ({cid})"
    elif src.get("podcast"):
        feed, show = resolve_podcast(src["podcast"], resolved)
        for e in parse_feed(fetch(feed))[:10]:
            if e.get("audio"):
                posts.append({**e, "id": f"pod:{e['id']}", "kind": "listen", "show": show})
        where = f"Podcast: {show} ({feed})"
    else:
        mode, target = resolve_page(src["url"], resolved)
        if mode == "feed":
            posts = [{**e, "kind": "story"} for e in parse_feed(fetch(target))[:20]]
            where = f"Feed: {target}"
        else:
            posts = [{**e, "kind": "story"} for e in scrape_links(target, fetch(target))]
            where = f"Page links: {target}"
    for p in posts:
        p.update(source=name, person=person, category=cat)
        p["id"] = uid(name, p["id"])
    return posts, where


# --------------------------------------------------------------------------
# The True Vine (Andrew Murray, 1898, public domain; text from CCEL)
# --------------------------------------------------------------------------
TV_BASE = "https://media.sabda.org/alkitab-12/ccel/m/murray/true_vin/"
TV_TITLES = [
    "The Vine", "The Husbandman", "The Branch", "The Fruit", "More Fruit",
    "The Cleansing", "The Pruning Knife", "Abide", "Except Ye Abide", "The Vine",
    "Ye the Branches", "Much Fruit", "You Can Do Nothing", "Withered Branches",
    "Whatsoever Ye Will", "If Ye Abide", "The Father Glorified", "True Disciples",
    "The Wonderful Love", "Abide in My Love", "Obey and Abide", "Ye, Even as I",
    "Joy", "Love One Another", "Even as I Have Loved You",
    "Christ's Friendship: Its Origin", "Christ's Friendship: Its Evidence",
    "Christ's Friendship: Its Intimacy", "Election", "Abiding Fruit",
    "Prevailing Prayer",
]
KEEP = {"p": "p", "br": "br", "i": "em", "em": "em", "b": "strong", "strong": "strong",
        "blockquote": "blockquote", "hr": "hr"}
BLOCK = {"p", "div", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "center", "table",
         "tr", "td", "hr", "ul", "ol", "li", "pre"}


class Simplifier(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks, self.cur, self.skip = [], [], 0

    def _flush(self):
        text = re.sub(r"\s+", " ", "".join(self.cur)).strip()
        text = re.sub(r"<(em|strong)>\s*</\1>", "", text)
        text = re.sub(r"(<br>\s*)+$", "", text).strip()
        if plain(text):
            self.blocks.append(text)
        self.cur = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head", "title"):
            self.skip += 1
        elif tag in BLOCK:
            self._flush()
        elif tag == "br":
            if self.cur and self.cur[-1] == "<br>":
                self.cur.pop()
                self._flush()
            else:
                self.cur.append("<br>")
        elif tag in KEEP:
            self.cur.append(f"<{KEEP[tag]}>")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head", "title"):
            self.skip = max(0, self.skip - 1)
        elif tag in BLOCK:
            self._flush()
        elif tag in KEEP and tag != "br":
            self.cur.append(f"</{KEEP[tag]}>")

    def handle_data(self, data):
        if not self.skip:
            self.cur.append(html.escape(data, quote=False))

    def close(self):
        super().close()
        self._flush()


NAV_ONLY = re.compile(r"^(?:<<|>>|up|title|contents|prev|next|\s|\|)+$", re.I)
TV_FOOTER = re.compile(r"(last modified|christian classics ethereal library|ccel\b|wheaton college|"
                       r"this document|typed by|public domain)", re.I)


def extract_meditation(raw_html: str, title: str) -> dict | None:
    p = Simplifier()
    p.feed(raw_html)
    p.close()
    norm = lambda s: re.sub(r"[^a-z]", "", plain(s).lower())
    want = norm(title)
    start = next((i for i, b in enumerate(p.blocks) if norm(b) == want), None)
    if start is None:
        start = next((i for i, b in enumerate(p.blocks) if want in norm(b)[: len(want) + 6]), None)
    if start is None:
        return None
    body = []
    for b in p.blocks[start + 1:]:
        t = plain(b)
        if NAV_ONLY.match(t):
            if body:
                break
            continue
        if TV_FOOTER.search(t) and len(t) < 300:
            break
        body.append(b)
    if not body:
        return None
    verse = ""
    if re.search(r"John\s*\.?\s*1?5", plain(body[0]), re.I) and len(plain(body[0])) < 400:
        verse = body.pop(0)
    words = sum(len(plain(b).split()) for b in body)
    if words < 150 or words > 4000:
        return None
    return {"verse": verse, "paras": body}


def true_vine_post(day: int, now: datetime, base: str) -> dict:
    title = TV_TITLES[day - 1]
    url = f"{TV_BASE}vine{day + 3:02d}.htm"
    CACHE.mkdir(exist_ok=True)
    cached = CACHE / f"day{day:02d}.json"
    med = load_json(cached, None)
    if med is None:
        try:
            med = extract_meditation(fetch(url), title)
            if med:
                save_json(cached, med)
        except Exception as e:  # noqa: BLE001
            log(f"True Vine day {day}: {e}")
    img = f"{base}cards/vine-{day:02d}.png"
    parts = [f'<p><img src="{img}" alt=""></p>']
    if med:
        if med["verse"]:
            parts.append(f"<blockquote>{med['verse']}</blockquote>")
        parts += [f"<p>{x}</p>" for x in med["paras"]]
        parts.append(f"<p><small>Andrew Murray, <em>The True Vine</em> (1898), day {day} of 31. "
                     f'Public domain. <a href="{url}">Source</a></small></p>')
    else:
        parts.append(f'<p>Today\'s meditation is on John 15. <a href="{url}">Read Day {day}: '
                     f"{html.escape(title)}</a></p>")
    return {"id": f"vine-{now.date().isoformat()}", "kind": "devotional", "category": "devotional",
            "person": "The True Vine", "source": "The True Vine",
            "title": f"\U0001F33F The True Vine · Day {day}: {title}", "link": url,
            "image": img, "html": "\n".join(parts)}


# --------------------------------------------------------------------------
# Memory cards (whole passage every time)
# --------------------------------------------------------------------------
def load_passages() -> list[dict]:
    passages, cur = [], None
    text = MEMORY.read_text(encoding="utf-8") if MEMORY.exists() else ""
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("=="):
            cur = {"ref": s.lstrip("=").strip(), "verses": []}
            passages.append(cur)
        elif cur is not None:
            m = re.match(r"^(\d{1,3})[\s.:)]+(.+)$", s)
            if m:
                cur["verses"].append([int(m.group(1)), m.group(2).strip()])
            elif cur["verses"]:
                cur["verses"][-1][1] += " " + s
            else:
                cur["verses"].append([None, s])
    return [p for p in passages if p["ref"] and p["verses"]]


WORD = re.compile(r"([A-Za-z’']+)")


def verses_html(verses, transform=lambda t: html.escape(t)) -> str:
    return "<p>" + " ".join((f"<sup>{n}</sup> " if n is not None else "") + transform(t)
                            for n, t in verses) + "</p>"


def memory_post(p: dict, style: str, now: datetime, base: str, notice: str, rng) -> dict:
    gap = "<p> </p>" * 6
    check = "<hr><p><strong>↓ Check yourself</strong></p>"
    vs, ref = p["verses"], p["ref"]
    if style == "blanks":
        def blank(t):
            return html.escape(WORD.sub(lambda m: "_" * max(5, len(m.group(1)))
                                        if len(m.group(1)) >= 4 and rng.random() < 0.33 else m.group(1), t))
        title = f"✏️ Fill in the blanks · {ref}"
        body = verses_html(vs, blank) + gap + check + verses_html(vs)
    elif style == "where":
        title = "❓ Where is this from?"
        body = (verses_html(vs) + "<p>Name the reference before you scroll.</p>"
                + gap + check + f"<p><strong>{html.escape(ref)}</strong></p>")
    else:
        style = "verse"
        title = f"\U0001F4D6 {ref}"
        body = verses_html(vs) + f"<p><em>— {html.escape(ref)}</em></p>"
    img = f"{base}cards/memory-{style}.png"
    note = f"<p><small>{html.escape(notice)}</small></p>" if notice else ""
    pid = f"mem-{now.strftime('%Y%m%d%H%M')}-{uid(ref, style)}"
    return {"id": pid, "kind": "memory", "category": "memory", "person": "Memory",
            "source": "Memory work", "ref": ref, "title": title, "link": f"{base}#{pid}",
            "image": img, "html": f'<p><img src="{img}" alt=""></p>' + body + note}


# --------------------------------------------------------------------------
# Turning pool posts into feed entries
# --------------------------------------------------------------------------
def enrich(post: dict, budget: list) -> None:
    """Fill in a missing picture/summary from the article's own preview tags."""
    if post["kind"] not in ("story",) or (post.get("image") and plain(post.get("summary"))):
        return
    if budget[0] <= 0:
        return
    budget[0] -= 1
    try:
        m = page_meta(post["link"])
        post["image"] = post.get("image") or m["image"]
        if not plain(post.get("summary")):
            post["summary"] = m["summary"]
    except Exception as e:  # noqa: BLE001
        log(f"preview for {post['link']}: {e}")


def render(post: dict, base: str) -> dict:
    """Pool post -> feed entry with html/title/image."""
    if "html" in post:
        return post
    e = dict(post)
    src, kind = post["source"], post["kind"]
    emoji, label = CATEGORY_LABEL.get(post["category"], ("•", post["category"].title()))
    img = post.get("image") or f"{base}cards/cat-{post['category'] if kind != 'listen' else 'listen'}.png"
    e["image"] = img
    summary = html.escape(clip(post.get("summary", ""), 300))
    link = html.escape(post["link"], quote=True)
    if kind == "video":
        e["title"] = f"\U0001F3A5 {src} · {post['title']}"
        e["html"] = (f'<p><iframe src="https://www.youtube.com/embed/{post["video_id"]}" width="360" '
                     f'height="640" frameborder="0" allowfullscreen></iframe></p>'
                     f'<p><a href="{link}"><img src="{html.escape(img, quote=True)}" alt=""></a></p>'
                     + (f"<p>{summary}</p>" if summary else ""))
    elif kind == "listen":
        audio = post["audio"]
        t_start = "#t=180" if "#" not in audio else ""
        e["title"] = f"\U0001F3A7 {src} · {post['title']}"
        e["html"] = (f'<p><img src="{html.escape(img, quote=True)}" alt=""></p>'
                     f"<p><em>{html.escape(post.get('show') or src)}</em></p>"
                     + (f"<p>{summary}</p>" if summary else "")
                     + f'<p><audio controls preload="none" src="{html.escape(audio + t_start, quote=True)}">'
                       f"</audio></p>"
                     f'<p><a href="{html.escape(audio, quote=True)}">▶ Play the full episode</a></p>')
    else:
        e["title"] = f"{emoji} {src} · {post['title']}"
        e["html"] = (f'<p><img src="{html.escape(img, quote=True)}" alt=""></p>'
                     + (f"<p>{summary}</p>" if summary else "")
                     + f'<p><a href="{link}">Read the full story →</a></p>')
    return e


# --------------------------------------------------------------------------
# Picking a batch
# --------------------------------------------------------------------------
def pick_batch(pool: list[dict], recent: list[dict], size: int, cfg: dict, now: datetime, rng,
               extra: list[dict]) -> list[dict]:
    """Choose up to `size` posts so that, counting the last day of posts, no person,
    category or podcast count goes over its cap."""
    caps = cfg["category_caps"]

    def counts(seq):
        c = {"person": {}, "cat": {}, "listen": 0}
        for x in seq:
            c["person"][x["person"]] = c["person"].get(x["person"], 0) + 1
            c["cat"][x["category"]] = c["cat"].get(x["category"], 0) + 1
            c["listen"] += x.get("kind") == "listen"
        return c

    def fits(x, seq):
        c = counts(seq)
        if x["kind"] not in ("memory", "devotional", "notice") and \
                c["person"].get(x["person"], 0) >= cfg["person_cap"]:
            return False
        if x["kind"] != "notice" and \
                c["cat"].get(x["category"], 0) >= caps.get(x["category"], cfg["default_category_cap"]):
            return False
        if x["kind"] == "listen" and c["listen"] >= cfg["podcast_cap"]:
            return False
        return True

    def score(x):
        pub = parse_date(x.get("published")) or parse_date(x.get("seen")) or now
        age = max(0.0, (now - pub).total_seconds() / 86400)
        return 0.5 ** (age / 4) * rng.uniform(0.4, 1.6)

    history = list(recent)
    chosen = []
    for x in extra:  # devotional / memory / notices go in if they fit
        if fits(x, history + chosen):
            chosen.append(x)
    for x in sorted(pool, key=score, reverse=True):
        if len([c for c in chosen if c["kind"] not in ("devotional", "notice")]) >= size:
            break
        if fits(x, history + chosen):
            chosen.append(x)

    # spread out: avoid the same category twice in a row where possible
    rest = [c for c in chosen if c["kind"] != "devotional"]
    rng.shuffle(rest)
    ordered = [c for c in chosen if c["kind"] == "devotional"]
    while rest:
        prev = ordered[-1]["category"] if ordered else None
        i = next((k for k, c in enumerate(rest) if c["category"] != prev), 0)
        ordered.append(rest.pop(i))
    return ordered


# --------------------------------------------------------------------------
# Feed output
# --------------------------------------------------------------------------
def write_feed(entries: list[dict], cfg: dict, base: str):
    esc = lambda s: html.escape(s or "", quote=True)
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/" '
           'xmlns:media="http://search.yahoo.com/mrss/" xmlns:atom="http://www.w3.org/2005/Atom">',
           "<channel>", f"<title>{esc(cfg['feed_title'])}</title>", f"<link>{esc(base)}</link>",
           f'<atom:link href="{esc(base)}feed.xml" rel="self" type="application/rss+xml"/>',
           "<description>A hand-picked feed</description>", "<language>en-us</language>", "<ttl>30</ttl>",
           f"<image><url>{esc(base)}cards/icon.png</url><title>{esc(cfg['feed_title'])}</title>"
           f"<link>{esc(base)}</link></image>"]
    for e in entries:
        when = datetime.fromisoformat(e["posted"])
        img = e.get("image", "")
        itype = "image/png" if img.lower().split("?")[0].endswith(".png") else "image/jpeg"
        out += ["<item>", f"<title>{esc(e['title'])}</title>", f"<link>{esc(e['link'])}</link>",
                f'<guid isPermaLink="false">{esc(e["id"])}</guid>',
                f"<pubDate>{format_datetime(when)}</pubDate>",
                f"<category>{esc(e.get('category', ''))}</category>",
                f"<description>{esc(e['html'])}</description>",
                f"<content:encoded><![CDATA[{e['html'].replace(']]>', ']]&gt;')}]]></content:encoded>"]
        if e.get("kind") == "listen" and e.get("audio"):
            out.append(f'<enclosure url="{esc(e["audio"])}" length="0" type="{esc(e.get("audio_type") or "audio/mpeg")}"/>')
        elif img:
            out.append(f'<enclosure url="{esc(img)}" length="0" type="{itype}"/>')
        if img:
            out.append(f'<media:content url="{esc(img)}" medium="image"/>')
            out.append(f'<media:thumbnail url="{esc(img)}"/>')
        out.append("</item>")
    out += ["</channel>", "</rss>", ""]
    FEED.write_text("\n".join(out), encoding="utf-8")


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--now", help="pretend it's this local time, YYYY-MM-DDTHH:MM (testing)")
    ap.add_argument("--batch-now", action="store_true", help="release a batch even if it's not time")
    ap.add_argument("--seed", type=int)
    args = ap.parse_args()

    cfg = {**DEFAULTS, **load_json(CONFIG, {})}
    tz = ZoneInfo(cfg["timezone"])
    now = (datetime.fromisoformat(args.now).replace(tzinfo=tz) if args.now
           else datetime.now(tz)).replace(second=0, microsecond=0)
    rng = random.Random(args.seed)
    base = base_url()
    state = load_json(STATE, {})
    status = load_json(STATUS, {})
    resolved = state.setdefault("resolved", {})
    today = now.date().isoformat()

    due = [h for h in cfg["batch_hours"] if h <= now.hour]
    slot = f"{today}@{due[-1]}" if due else None
    done = state.setdefault("batches_done", [])
    first_run = not state.get("feed")
    if not (args.batch_now or first_run or (slot and slot not in done)):
        print("Not batch time.")
        return

    # 1) refresh the pool
    pool = state.setdefault("pool", {})
    released = state.setdefault("released_ids", {})
    cutoff = now - timedelta(days=cfg["pool_days"])
    sources = load_json(SOURCES, {}).get("sources", [])
    for src in sources:
        key = src["name"] + (" (YouTube)" if src.get("youtube") else " (podcast)" if src.get("podcast") else "")
        st = status.setdefault(key, {"first_tried": now.isoformat()})
        try:
            posts, where = collect_source(src, resolved)
            added = 0
            for p in posts:
                if p["id"] in released or p["id"] in pool or not p.get("title"):
                    continue
                pub = parse_date(p.get("published"))
                if pub and pub < cutoff:
                    continue
                p["seen"] = now.isoformat()
                for k in ("duration",):
                    p.pop(k, None)
                pool[p["id"]] = p
                added += 1
            st.update(ok=True, last_ok=now.isoformat(), found=len(posts), added=added, using=where, error="")
            log(f"OK   {key}: {len(posts)} found, {added} new  <- {where}")
        except Exception as e:  # noqa: BLE001
            st.update(ok=False, error=f"{type(e).__name__}: {e}"[:300], last_error=now.isoformat())
            log(f"FAIL {key}: {st['error']}")
    known = {s["name"] + (" (YouTube)" if s.get("youtube") else " (podcast)" if s.get("podcast") else "")
             for s in sources}
    for k in list(status):
        if k not in known:
            del status[k]
    for pid, p in list(pool.items()):
        if (parse_date(p.get("published")) or parse_date(p["seen"])) < cutoff:
            del pool[pid]

    # 2) extras: True Vine (first batch of the day), memory card, broken-source notices
    extra = []
    if cfg["true_vine"] and state.get("vine_last") != today:
        start = date.fromisoformat(state.setdefault("vine_start", today))
        n = (now.date() - start).days
        day = n % 31 + 1 if cfg["true_vine_repeat"] else n + 1
        if 1 <= day <= 31:
            extra.append(true_vine_post(day, now, base))
            state["vine_last"] = today
    passages = load_passages()
    per_batch = cfg["memory_cards_per_day"] / max(1, len(cfg["batch_hours"]))
    n_mem = int(per_batch) + (rng.random() < per_batch - int(per_batch))
    for _ in range(n_mem if passages else 0):
        last = state.get("last_memory_ref")
        choices = [p for p in passages if p["ref"] != last] or passages
        p = rng.choice(choices)
        styles = {k: v for k, v in cfg["memory_styles"].items() if v > 0} or {"verse": 1}
        style = rng.choices(list(styles), weights=list(styles.values()))[0]
        extra.append(memory_post(p, style, now, base, cfg["scripture_notice"], rng))
        state["last_memory_ref"] = p["ref"]
    warned = state.setdefault("warned", {})
    for k, st in status.items():
        last_ok = parse_date(st.get("last_ok")) or parse_date(st.get("first_tried"))
        broken_for = (now - last_ok).days if last_ok else 0
        limit = cfg["warn_after_days"] if st.get("last_ok") else 1
        if not st.get("ok") and broken_for >= limit and \
                (parse_date(warned.get(k)) or cutoff - timedelta(days=30)) < now - timedelta(days=7):
            img = f"{base}cards/cat-notice.png"
            extra.append({"id": f"notice-{k}-{today}", "kind": "notice", "category": "notice",
                          "person": "notice", "source": "Feed check", "image": img,
                          "title": f"⚠️ Feed check: {k} isn't coming through",
                          "link": base,
                          "html": f'<p><img src="{img}" alt=""></p><p><strong>{html.escape(k)}</strong> '
                                  f"hasn't worked for {broken_for} day(s). Everything else is fine.</p>"
                                  f"<p>Ask Claude to fix or replace it. The error was: "
                                  f"<code>{html.escape(st.get('error', ''))}</code></p>"})
            warned[k] = now.isoformat()

    # 3) pick the batch
    size = math.ceil(cfg["posts_per_day"] / max(1, len(cfg["batch_hours"])))
    feed = state.setdefault("feed", [])
    # caps count the last 24 hours of posts (about 40 of them), so a quiet week can
    # never freeze the feed. 25h leaves a little slack around batch times.
    day_ago = (now - timedelta(hours=25)).isoformat()
    recent = [{"person": e["person"], "category": e["category"], "kind": e["kind"]}
              for e in feed if e["posted"] > day_ago]
    batch = pick_batch(list(pool.values()), recent, size, cfg, now, rng, extra)
    budget = [15]
    new_entries = []
    for i, post in enumerate(batch):
        enrich(post, budget)
        e = render(post, base)
        e["posted"] = (now - timedelta(minutes=i)).isoformat()
        for k in ("summary", "seen", "published"):
            e.pop(k, None)
        new_entries.append(e)
        if post["id"] in pool:
            del pool[post["id"]]
            released[post["id"]] = today
    for k, v in list(released.items()):
        if v < (now.date() - timedelta(days=45)).isoformat():
            del released[k]

    state["feed"] = (new_entries + feed)[:150]
    if slot:
        done.append(slot)
    state["batches_done"] = done[-30:]
    save_json(STATE, state)
    save_json(STATUS, dict(sorted(status.items())))
    write_feed(state["feed"], cfg, base)
    kinds = {}
    for e in new_entries:
        kinds[e["category"]] = kinds.get(e["category"], 0) + 1
    print(f"Released {len(new_entries)} posts {kinds}; pool now {len(pool)}")


if __name__ == "__main__":
    main()
