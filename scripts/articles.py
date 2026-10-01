"""
기사 후보 수집 · 가중치 선택 모듈

소스별로 여러 방법을 차례로 시도한다 (사이트 구조가 바뀌어도 하나는 살아남도록).
  sedaily : RSS 후보 → 홈/섹션 페이지 링크 (URL에 날짜 포함: /finance/2026/08/07/slug)
  ted     : RSS 후보 → 홈/최신 강연 페이지 링크 → 강연 페이지에서 날짜 추출
  engoo   : ENGOO_LIST_URL(직접 지정) → sitemap → 목록 페이지 링크
`python scripts/send_cheat_sheet.py --mode sources` 로 각 소스에서 뭐가 잡히는지 점검할 수 있다.
"""
from __future__ import annotations

import base64
import email.utils
import html
import json
import os
import random
import re
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse, urlunparse

import requests

KST = timezone(timedelta(hours=9))
UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}
TIMEOUT = 20
RECENT_DAYS = 92          # "최근 3개월"
MAX_ENRICH_PER_SOURCE = 15

SOURCE_LABELS = {"engoo": "Engoo Daily News", "sedaily": "Seoul Economic Daily", "ted": "TED"}
SOURCE_COLORS = {"engoo": 0x00A4E4, "sedaily": 0x1F3A93, "ted": 0xE62B1E}


@dataclass
class Candidate:
    source: str
    url: str
    title: str = ""
    published: date | None = None
    description: str = ""
    text: str = ""
    enriched: bool = False
    level: int | None = None


# ───────────────────────── 공통 유틸 ─────────────────────────
def http_get(url: str) -> requests.Response | None:
    try:
        r = requests.get(url, headers=UA, timeout=TIMEOUT)
    except requests.RequestException as e:
        print(f"[fetch] {url} → {e.__class__.__name__}")
        return None
    if not r.ok:
        print(f"[fetch] {url} → HTTP {r.status_code}")
        return None
    return r


def norm_url(url: str) -> str:
    p = urlparse(url.strip())
    path = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme or "https", p.netloc.lower().removeprefix("www."), path, "", "", ""))


def parse_date(s: str | None) -> date | None:
    if not s:
        return None
    s = s.strip()
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None
    try:
        return email.utils.parsedate_to_datetime(s).date()
    except (TypeError, ValueError, IndexError):
        return None


def strip_tags(s: str) -> str:
    s = re.sub(r"<(script|style|noscript)\b.*?</\1>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def hrefs(page_html: str, base: str) -> list[str]:
    out = []
    for m in re.finditer(r'href\s*=\s*["\']([^"\'#]+)', page_html, re.I):
        out.append(urljoin(base, html.unescape(m.group(1))))
    return out


_META_RE = re.compile(r"<meta\b[^>]*>", re.I)
_ATTR_RE = re.compile(r'([\w:.-]+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\')')


def metas(page_html: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for tag in _META_RE.findall(page_html):
        attrs = {k.lower(): (dq if dq is not None and dq != "" else sq) for k, dq, sq in _ATTR_RE.findall(tag)}
        key = attrs.get("property") or attrs.get("name") or attrs.get("itemprop")
        if key and attrs.get("content"):
            found.setdefault(key.lower(), html.unescape(attrs["content"]).strip())
    return found


def find_page_date(page_html: str, m: dict[str, str]) -> date | None:
    for key in ("article:published_time", "og:published_time", "datepublished", "uploaddate",
                "publishdate", "pubdate", "date", "article:modified_time"):
        d = parse_date(m.get(key))
        if d:
            return d
    mm = re.search(r'"(?:datePublished|uploadDate|publishedAt|published_at|publishDate|recordedOn)"'
                   r'\s*:\s*"([^"]+)"', page_html)
    return parse_date(mm.group(1)) if mm else None


def parse_feed(xml_text: str) -> list[dict]:
    """RSS/Atom → [{link, title, date, desc, raw}]"""
    try:
        root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    except ET.ParseError:
        return []
    items = []
    for el in root.iter():
        if el.tag.split("}")[-1] not in ("item", "entry"):
            continue
        kids = {c.tag.split("}")[-1]: c for c in el}
        link_el = kids.get("link")
        link = ""
        if link_el is not None:
            link = (link_el.text or link_el.get("href") or "").strip()
        title = strip_tags(kids["title"].text or "") if "title" in kids and kids["title"].text else ""
        d = None
        for n in ("pubDate", "published", "updated", "date"):
            if n in kids and kids[n].text:
                d = parse_date(kids[n].text)
                if d:
                    break
        desc = ""
        for n in ("description", "summary", "content"):
            if n in kids and kids[n].text:
                desc = strip_tags(kids[n].text)[:600]
                break
        items.append({"link": link, "title": title, "date": d, "desc": desc,
                      "raw": ET.tostring(el, encoding="unicode")})
    return items


def parse_sitemap(xml_text: str) -> tuple[list[tuple[str, date | None]], list[str]]:
    """→ (urls with lastmod, child sitemap urls)"""
    try:
        root = ET.fromstring(xml_text.encode("utf-8"))
    except ET.ParseError:
        return [], []
    urls, subs = [], []
    kind = root.tag.split("}")[-1]
    for el in root:
        kids = {c.tag.split("}")[-1]: (c.text or "").strip() for c in el}
        if not kids.get("loc"):
            continue
        if kind == "sitemapindex":
            subs.append(kids["loc"])
        else:
            urls.append((kids["loc"], parse_date(kids.get("lastmod"))))
    return urls, subs


def enrich(c: Candidate) -> Candidate:
    """기사 페이지에서 제목·날짜·설명·본문 일부를 채운다."""
    if c.enriched:
        return c
    c.enriched = True
    r = http_get(c.url)
    if not r:
        return c
    page = r.text
    m = metas(page)
    if not c.title:
        t = m.get("og:title") or m.get("twitter:title")
        if not t:
            tm = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
            t = strip_tags(tm.group(1)) if tm else ""
        c.title = t
    if not c.description:
        c.description = m.get("og:description") or m.get("description") or ""
    if not c.published:
        c.published = find_page_date(page, m)
    paras = [strip_tags(p) for p in re.findall(r"<p\b[^>]*>(.*?)</p>", page, re.S | re.I)]
    body = " ".join(p for p in paras if len(p) > 40)
    c.text = body[:4000]
    time.sleep(0.5)  # 사이트에 부담 주지 않도록
    return c


def title_from_slug(url: str) -> str:
    parts = [p for p in urlparse(url).path.split("/") if p]
    # UUID(16진수+하이픈)는 건너뛰고 영단어가 들어간 slug를 찾는다
    slug = next((p for p in reversed(parts) if ("-" in p or "_" in p) and re.search(r"[g-z]", p, re.I)),
                parts[-1] if parts else "")
    return re.sub(r"[-_]+", " ", slug).strip().capitalize()


# ───────────────────────── 소스별 수집 ─────────────────────────
SEDAILY_ART = re.compile(r"^https?://en\.sedaily\.com/[a-z-]+/(\d{4})/(\d{2})/(\d{2})/[a-z0-9-]+/?$", re.I)


def collect_sedaily() -> list[Candidate]:
    found: dict[str, Candidate] = {}

    def add(url, title="", desc=""):
        m = SEDAILY_ART.match(url.split("?")[0])
        if not m:
            return
        key = norm_url(url)
        if key not in found:
            try:
                d = date(int(m[1]), int(m[2]), int(m[3]))
            except ValueError:
                d = None
            found[key] = Candidate("sedaily", url.split("?")[0], title, d, desc)

    for feed in ("https://en.sedaily.com/rss", "https://en.sedaily.com/feed",
                 "https://en.sedaily.com/rss.xml", "https://en.sedaily.com/feed.xml"):
        r = http_get(feed)
        if r and r.text.lstrip().startswith("<"):
            for it in parse_feed(r.text):
                add(it["link"], it["title"], it["desc"])
        if found:
            break
    if len(found) < 15:
        for page in ("https://en.sedaily.com/", "https://en.sedaily.com/technology",
                     "https://en.sedaily.com/society", "https://en.sedaily.com/culture",
                     "https://en.sedaily.com/finance"):
            r = http_get(page)
            if r:
                for u in hrefs(r.text, page):
                    add(u)
    return list(found.values())


TED_TALK = re.compile(r"https?://(?:www\.)?ted\.com/talks/([a-z0-9_]+)", re.I)


def collect_ted() -> list[Candidate]:
    found: dict[str, Candidate] = {}

    def add(url, title="", d=None, desc=""):
        m = TED_TALK.search(url)
        if not m:
            return
        clean = f"https://www.ted.com/talks/{m.group(1)}"
        key = norm_url(clean)
        if key not in found:
            found[key] = Candidate("ted", clean, title, d, desc)

    for feed in ("https://feeds.feedburner.com/TEDTalks_video", "https://www.ted.com/talks/rss",
                 "https://pa.tedcdn.com/feeds/talks.rss"):
        r = http_get(feed)
        if r and r.text.lstrip().startswith("<"):
            for it in parse_feed(r.text):
                m = TED_TALK.search(it["link"]) or TED_TALK.search(it["raw"])
                if m:
                    add(m.group(0), it["title"], it["date"], it["desc"])
        if found:
            break
    # 피드가 업데이트를 멈춘 경우가 있어서(2025년 5월에 멈춘 피드 확인됨) 최신 강연 페이지는 항상 확인한다
    page_found: list[str] = []
    for page in ("https://www.ted.com/talks?sort=newest", "https://www.ted.com/"):
        r = http_get(page)
        if not r:
            continue
        before = set(found)
        for u in hrefs(r.text, page):
            add(u)
        for m in TED_TALK.finditer(r.text.replace("\\/", "/")):
            add(m.group(0))
        page_found += [k for k in found if k not in before]
    print(f"[ted] 최신 강연 페이지에서 {len(page_found)}개 발견")
    # 날짜를 모르는 후보(=페이지에서 찾은 강연)는 강연 페이지를 열어 날짜 확인. 목록 앞쪽이 최신.
    undated = [found[k] for k in page_found if not found[k].published]
    for c in undated[:MAX_ENRICH_PER_SOURCE]:
        enrich(c)
    return list(found.values())


# Engoo Daily News 공개 API (브라우저 개발자 도구에서 확인한 주소)
#   - 5개 카테고리 × count 개의 최신 기사 헤더를 돌려준다
#   - 기사 링크 = /app/daily-news/article/{제목 slug}/{master_id를 base64url로 줄인 값}
ENGOO_API = ("https://api.engoo.com/api/lesson_headers/by_course"
             "?category=0225ae09-5d63-41c2-bd75-693985d07d78&count={count}"
             "&for_brand=5a4657f2-e151-4c48-9cce-000000000002"
             "&max_level=9&min_level=4&published_latest=true&type=Published")
ENGOO_HEADERS = {**UA, "Accept": "application/json", "Origin": "https://engoo.com",
                 "Referer": "https://engoo.com/app/daily-news"}


def engoo_clean_title(title: str) -> str:
    return re.sub(r"_([^_]+)_", r"\1", title).strip()          # _Tsukimi:_ → Tsukimi:


def engoo_slug(title: str) -> str:
    t = engoo_clean_title(title).lower()
    t = re.sub(r"['’`]", "", t)                                 # don't → dont
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")


def engoo_short_id(master_id: str) -> str:
    return base64.urlsafe_b64encode(uuid.UUID(master_id).bytes).decode().rstrip("=")


def parse_engoo_api(payload: dict) -> list[Candidate]:
    out = []
    for v in (payload.get("references") or {}).values():
        if v.get("_type") != "LessonHeader" or v.get("hidden") or v.get("requires_plan"):
            continue
        title = engoo_clean_title(((v.get("title_text") or {}).get("text") or ""))
        mid = v.get("master_id") or v.get("id")
        if not title or not mid:
            continue
        try:
            url = f"https://engoo.com/app/daily-news/article/{engoo_slug(title)}/{engoo_short_id(mid)}"
        except ValueError:
            continue
        intro = ((v.get("introduction_text") or {}).get("text") or "").strip()
        c = Candidate("engoo", url, title, parse_date(v.get("first_published_at")), intro)
        c.text = intro
        c.level = v.get("content_level")
        c.enriched = True          # 기사 페이지는 JS 앱이라 따로 열어도 얻을 게 없음
        out.append(c)
    return out


def collect_engoo() -> list[Candidate]:
    urls = []
    custom = (os.getenv("ENGOO_LIST_URL") or "").strip()
    if custom:
        urls.append(custom)
    urls += [ENGOO_API.format(count=25), ENGOO_API.format(count=9)]
    for u in urls:
        try:
            r = requests.get(u, headers=ENGOO_HEADERS, timeout=TIMEOUT)
        except requests.RequestException as e:
            print(f"[fetch] engoo api → {e.__class__.__name__}")
            continue
        if not r.ok:
            print(f"[fetch] engoo api → HTTP {r.status_code}")
            continue
        try:
            cands = parse_engoo_api(r.json())
        except ValueError:
            print("[fetch] engoo api → JSON 아님")
            continue
        if cands:
            uniq = {norm_url(c.url): c for c in cands}
            return list(uniq.values())
    return []


COLLECTORS = {"engoo": collect_engoo, "sedaily": collect_sedaily, "ted": collect_ted}


def parse_weights(spec: str) -> dict[str, float]:
    weights = {}
    for part in spec.split(","):
        if ":" in part:
            k, v = part.split(":", 1)
            try:
                weights[k.strip().lower()] = float(v)
            except ValueError:
                pass
    return {k: v for k, v in weights.items() if k in COLLECTORS and v > 0}


def collect_all(sources) -> dict[str, list[Candidate]]:
    out = {}
    for s in sources:
        try:
            out[s] = COLLECTORS[s]()
        except Exception as e:  # 한 소스가 망가져도 나머지는 진행
            print(f"[{s}] 수집 중 오류: {e}")
            out[s] = []
        print(f"[{s}] 후보 {len(out[s])}개")
    return out


def tier(c: Candidate, today: date) -> int:
    """0 = 최근 3개월, 1 = 날짜 미상, 2 = 오래됨"""
    if c.published is None:
        return 1
    return 0 if (today - c.published).days <= RECENT_DAYS else 2


def choose(pools: dict[str, list[Candidate]], weights: dict[str, float], used: set[str],
           k: int = 3, rng: random.Random | None = None, today: date | None = None) -> list[Candidate]:
    """소스를 가중치로 뽑고, 그 소스에서 '최근 → 날짜 미상 → 오래됨' 순으로 미사용 기사를 고른다."""
    rng = rng or random.Random()
    today = today or datetime.now(KST).date()
    avail: dict[str, list[Candidate]] = {}
    for s, lst in pools.items():
        fresh = [c for c in lst if norm_url(c.url) not in used]
        rng.shuffle(fresh)
        fresh.sort(key=lambda c: tier(c, today))
        if fresh and weights.get(s, 0) > 0:
            avail[s] = fresh
    picks: list[Candidate] = []
    while len(picks) < k and avail:
        names = list(avail)
        s = rng.choices(names, weights=[weights[n] for n in names])[0]
        picks.append(avail[s].pop(0))
        if not avail[s]:
            del avail[s]
    return picks
