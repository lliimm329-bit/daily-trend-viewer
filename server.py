#!/usr/bin/env python3
"""트렌드 뷰어 로컬 서버 — 유튜브/쇼츠/릴스 인기 영상과 AI 영상 소식을 제공합니다.
외부 패키지 없이 파이썬 표준 라이브러리만 사용합니다.
실행: python3 server.py  →  http://localhost:8778
"""
import base64
import datetime
import email.utils
import html as htmllib
import json
import math
import os
import re
import threading
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

# macOS의 Python은 인증서 경로를 못 찾는 경우가 있어 시스템 인증서 파일을 지정합니다 (이미 지정돼 있으면 그대로 둠)
if not os.environ.get("SSL_CERT_FILE") and os.path.exists("/etc/ssl/cert.pem"):
    os.environ["SSL_CERT_FILE"] = "/etc/ssl/cert.pem"

PORT = 8778
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_TTL = 3600  # 1시간 캐시 (새로고침 버튼으로 강제 갱신 가능)
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

_cache = {}
_cache_lock = threading.Lock()
# 썸네일 프록시 메모리 캐시 (url -> (content_type, bytes))
_img_cache = {}
_img_lock = threading.Lock()
IMG_CACHE_MAX = 600

# ---------------------------------------------------------------- 유튜브
# 카테고리 → 유튜브 검색어 목록 (카테고리당 3~5개). 여러 검색어 결과를 합쳐 조회수순으로 정렬
# 기본값. 화면에서 추가/삭제한 내용은 categories.json에 저장되어 이 기본값 위에 덮어씁니다.
DEFAULT_CATEGORIES = {
    "AI 툴": ["AI 툴 추천", "AI 툴 리뷰", "AI 자동화 툴", "ai tools review", "chatgpt 활용법"],
    "AI 콘텐츠 제작": ["AI 콘텐츠 제작", "AI 블로그 자동화", "AI 대본 작성", "AI 유튜브 수익화"],
    "AI 디자인": ["AI 디자인 툴", "AI 로고 만들기", "피그마 AI 디자인", "figma ai design"],
    "AI 영상 제작": ["AI 영상 제작", "AI 영상 생성", "sora ai video", "runway ai video"],
    "AI 이미지 제작": ["AI 이미지 제작", "미드저니 AI 이미지", "AI 그림 그리기", "stable diffusion 튜토리얼"],
    "AI": ["AI 최신 소식", "AI 신기술 소개", "인공지능 최신 뉴스", "ChatGPT Gemini Claude 비교"],
    "AI 이슈": ["AI 이슈", "AI 논란", "AI 업계 소식", "AI issue this week"],
}
CATEGORIES_FILE = os.path.join(BASE_DIR, "categories.json")


def parse_terms(value):
    """쉼표로 구분된 문자열 또는 목록을 공백을 뗀 단어 목록으로 바꿉니다."""
    items = value.split(",") if isinstance(value, str) else (value or [])
    return [str(t).strip() for t in items if str(t).strip()]


def load_categories():
    """{이름: {"keywords": [...], "exclude": [...]}} 형태로 읽습니다. 예전 형식(목록만 있음)도 받아들입니다."""
    try:
        with open(CATEGORIES_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = DEFAULT_CATEGORIES
    out = {}
    for name, v in data.items():
        if isinstance(v, list):
            v = {"keywords": v, "exclude": []}
        if v.get("keywords"):
            out[name] = {"keywords": list(v["keywords"]), "exclude": list(v.get("exclude", []))}
    return out


CATEGORIES = load_categories()

# 검색 필터 protobuf: 업로드 날짜 (2=오늘, 3=이번 주, 4=이번 달)
PERIOD_CODE = {"day": 2, "week": 3, "month": 4}

# 검색 결과에 섞여 오는 추천 섹션 영상이 기간 필터를 우회하는 경우를 걸러내기 위한
# 기간별 제외 문구 ("N일 전" 형태의 게시일 텍스트 기준)
PERIOD_EXCLUDE = {
    "day": ("일 전", "주 전", "개월 전", "년 전"),
    "week": ("주 전", "개월 전", "년 전"),
    "month": ("개월 전", "년 전"),
}

# ---------------------------------------------------------------- 인스타그램 릴스
IG_APP_ID = "936619743392459"  # instagram.com 웹이 쓰는 공개 앱 ID
ACCOUNTS_FILE = os.path.join(BASE_DIR, "reels_accounts.json")
DEFAULT_IG_ACCOUNTS = [
    "openai", "runwayapp", "pika_labs", "lumalabsai", "midjourney",
    "klingai_official", "heygen_official", "higgsfield.ai", "googledeepmind",
]

# ---------------------------------------------------------------- X (트위터)
X_ACCOUNTS_FILE = os.path.join(BASE_DIR, "x_accounts.json")
DEFAULT_X_ACCOUNTS = [
    "OpenAI", "runwayml", "Kling_ai", "GoogleDeepMind", "midjourney",
    "LumaLabsAI", "pika_labs", "heygen_com", "elevenlabsio", "AIatMeta",
]

# ---------------------------------------------------------------- 스레드(Threads)
THREADS_ACCOUNTS_FILE = os.path.join(BASE_DIR, "threads_accounts.json")
DEFAULT_THREADS_ACCOUNTS = [
    "openai", "runway", "google", "meta.ai", "zuck",
]
IG_APP_ID_THREADS = "238260118697367"  # threads.com 웹이 쓰는 공개 앱 ID

# ---------------------------------------------------------------- 틱톡(TikTok)
# tikwm 무료 공개 API가 서명(X-Bogus/msToken)을 대신 처리해 조회수·좋아요·댓글까지 반환합니다.
TIKTOK_ACCOUNTS_FILE = os.path.join(BASE_DIR, "tiktok_accounts.json")
DEFAULT_TIKTOK_ACCOUNTS = [
    "openai", "runwayapp", "krea.ai", "elevenlabs", "sora",
    "zachking", "khaby.lame", "google",
]
TIKWM_BASE = "https://www.tikwm.com/api"
TIKTOK_REGION = "KR"
TIKTOK_TREND_SEEDS = ["일상", "먹방", "챌린지", "브이로그", "여행", "뷰티"]  # 한국 게시물이 많이 나오는 해시태그

# ---------------------------------------------------------------- AI 영상 탭
NEWS_FEEDS = [
    ("국내", "https://news.google.com/rss/search?q=" +
     quote('AI 영상 생성 OR "AI 비디오" OR 영상생성모델') + "&hl=ko&gl=KR&ceid=KR:ko"),
    ("해외", "https://news.google.com/rss/search?q=" +
     quote('"AI video" model OR Sora OR Runway OR Kling OR Veo') + "&hl=en-US&gl=US&ceid=US:en"),
]
HF_PIPELINES = ["text-to-video", "image-to-video"]
# 이미지 프록시로 가져올 수 있는 호스트 (핫링크/차단 우회용)
IMG_PROXY_ALLOW = (".cdninstagram.com", ".fbcdn.net", ".ytimg.com",
                   ".googleusercontent.com", ".twimg.com", ".imginn.com",
                   ".tiktokcdn.com", ".tiktokcdn-eu.com", ".tiktokcdn-us.com")

# 인스타그램이 무인증 API를 차단(429)해 미러 사이트를 사용합니다
IMGINN_BASE = "https://imginn.com"
# X(트위터) syndication 타임라인 API 폐쇄 대응: nitter RSS로 트윗 목록을 얻고
# 트윗별 통계는 임베드용 공개 CDN(tweet-result)에서 가져옵니다
NITTER_INSTANCES = ("nitter.net", "xcancel.com", "nitter.privacyredirect.com")
RSS_UA = "Mozilla/5.0 (compatible; RSSReader/1.0)"
# 스레드 실시간 GraphQL이 막혔을 때 사용할 RSSHub 공개 인스턴스들
RSSHUB_INSTANCES = ("rsshub.rssforever.com", "hub.slarker.me", "rsshub.pseudoyu.com")


def within_period(published: str, period: str) -> bool:
    if not published:
        return True  # 게시일 정보가 없으면(라이브 등) 통과
    return not any(word in published for word in PERIOD_EXCLUDE.get(period, ()))


def build_search_params(period: str, shorts: bool = False) -> str:
    """정렬=조회수(3) + 필터(업로드날짜, 동영상 타입, 길이) protobuf를 base64로 만듭니다."""
    filters = bytes([0x08, PERIOD_CODE.get(period, 3), 0x10, 0x01])
    if shorts:
        filters += bytes([0x18, 0x01])  # 길이: 4분 미만
    raw = bytes([0x08, 0x03, 0x12, len(filters)]) + filters
    return base64.urlsafe_b64encode(raw).decode()


def http_get(url: str, payload=None, headers=None, timeout=15):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data)
    req.add_header("User-Agent", UA)
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.headers.get("Content-Type", ""), resp.read()


def http_json(url: str, payload=None, headers=None, timeout=15):
    _, body = http_get(url, payload, headers, timeout)
    return json.loads(body.decode())


def parse_view_count(text: str) -> int:
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else 0


def cached(key, force, fetch_fn):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and not force and now - hit[0] < CACHE_TTL:
            return hit[1], hit[0]
    result = fetch_fn()
    fetched_at = time.time()
    with _cache_lock:
        _cache[key] = (fetched_at, result)
    return result, fetched_at


# ================================================================ 유튜브
def extract_videos(node, out):
    """응답 트리를 순회하며 videoRenderer를 수집합니다."""
    if isinstance(node, dict):
        if "videoRenderer" in node:
            v = node["videoRenderer"]
            title = "".join(r.get("text", "") for r in v.get("title", {}).get("runs", []))
            views_text = v.get("viewCountText", {}).get("simpleText", "")
            thumbs = v.get("thumbnail", {}).get("thumbnails", [])
            out.append({
                "id": v.get("videoId", ""),
                "title": title,
                "channel": "".join(r.get("text", "") for r in v.get("ownerText", {}).get("runs", [])),
                "views": parse_view_count(views_text),
                "viewsText": views_text,
                "length": v.get("lengthText", {}).get("simpleText", ""),
                "published": v.get("publishedTimeText", {}).get("simpleText", ""),
                "thumbnail": thumbs[-1]["url"] if thumbs else "",
            })
        for value in node.values():
            extract_videos(value, out)
    elif isinstance(node, list):
        for item in node:
            extract_videos(item, out)


def yt_search(query: str, period: str, shorts: bool):
    payload = {
        "context": {"client": {
            "clientName": "WEB",
            "clientVersion": "2.20250624.01.00",
            "hl": "ko", "gl": "KR",
        }},
        "query": query,
        "params": build_search_params(period, shorts),
    }
    try:
        data = http_json("https://www.youtube.com/youtubei/v1/search", payload)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return []
    videos = []
    extract_videos(data, videos)
    seen, unique = set(), []
    for v in videos:
        if v["id"] and v["id"] not in seen and within_period(v["published"], period):
            seen.add(v["id"])
            unique.append(v)
    return unique


def yt_like_count(video_id: str):
    """youtubei/v1/next로 영상 1개의 좋아요 수를 가져옵니다(검색 API엔 없음)."""
    payload = {"context": {"client": {
        "clientName": "WEB", "clientVersion": "2.20250624.01.00", "hl": "ko", "gl": "KR"}},
        "videoId": video_id}
    try:
        _, body = http_get("https://www.youtube.com/youtubei/v1/next", payload=payload, timeout=10)
        s = body.decode("utf-8", "ignore")
        m = re.search(r"다른 사용자 ([0-9,]+)명", s) or re.search(r"along with ([0-9,]+) other", s)
        return int(m.group(1).replace(",", "")) + 1 if m else 0
    except Exception:
        return 0


def enrich_likes(videos, limit=45):
    """영상 리스트에 좋아요 수(likes)를 병렬로 채웁니다. 이미 채워진 항목은 건너뜁니다."""
    todo = [v for v in videos[:limit] if not v.get("likes")]
    if not todo:
        return videos
    with ThreadPoolExecutor(max_workers=12) as pool:
        counts = pool.map(lambda v: yt_like_count(v["id"]), todo)
    for v, c in zip(todo, counts):
        v["likes"] = c
    return videos


def length_seconds(text: str) -> int:
    """'1:02' · '1:02:03' 형태의 길이를 초로 바꿉니다. 없으면 아주 큰 값(=제외)."""
    try:
        total = 0
        for part in text.split(":"):
            total = total * 60 + int(part)
        return total
    except ValueError:
        return 10**9


def search_one(pair, period, shorts):
    """검색 1회 후, 그 카테고리의 제외 키워드가 제목에 들어간 영상은 뺍니다."""
    query, exclude = pair
    low_ex = [w.lower() for w in exclude]
    return [v for v in yt_search(query, period, shorts)
            if not any(w in v["title"].lower() for w in low_ex)]


def merge_yt_searches(pairs, period, shorts):
    """pairs: [(검색어, 제외 키워드 목록), ...]"""
    if shorts:
        # 유튜브엔 '60초 이하' 필터가 없어서, 검색어에 #shorts를 붙여 쇼츠가 더 많이 나오게 한 뒤 길이로 거릅니다
        pairs = [(x, ex) for q, ex in pairs for x in (q, q + " #shorts", q + " shorts")]
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = pool.map(lambda pair: search_one(pair, period, shorts), pairs)
    merged, seen = [], set()
    for chunk in results:
        for v in chunk:
            if v["id"] not in seen:
                seen.add(v["id"])
                merged.append(v)
    if shorts:
        merged = [v for v in merged if length_seconds(v["length"]) <= 60]
    merged.sort(key=lambda v: v["views"], reverse=True)
    return merged


def get_videos(category: str, period: str, shorts: bool, force: bool, enrich: bool = False, query: str = ""):
    def fetch():
        if query:
            pairs = [(query, [])]
        elif category == "전체":
            pairs = [(q, c["exclude"]) for c in CATEGORIES.values() for q in c["keywords"]]
        else:
            pairs = [(q, CATEGORIES[category]["exclude"]) for q in CATEGORIES[category]["keywords"]]
        vids = merge_yt_searches(pairs, period, shorts)
        if enrich:
            enrich_likes(vids)
        return vids
    return cached(("yt", query or category, period, shorts, enrich), force, fetch)


# ================================================================ 유튜브 구독 채널
YT_ACCOUNTS_FILE = os.path.join(BASE_DIR, "yt_accounts.json")  # [{"handle", "channelId"}]
YT_SUB_DAYS = {"day": 1, "week": 7, "month": 30}
YT_ATOM = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
           "media": "http://search.yahoo.com/mrss/"}


def resolve_yt_channel(handle: str) -> str:
    _, body = http_get("https://www.youtube.com/@%s" % quote(handle), timeout=15)
    m = re.search(r'"(?:channelId|externalId)":"(UC[\w-]{22})"', body.decode("utf-8", "ignore"))
    return m.group(1) if m else ""


def is_yt_short(video_id: str) -> bool:
    # shorts: 주소가 그대로 /shorts/ 로 남고, 일반 영상이면 /watch 로 넘어감 (길이 정보가 피드에 없어서 이 방법을 씀)
    req = urllib.request.Request("https://www.youtube.com/shorts/%s" % video_id, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return "/shorts/" in resp.geturl()
    except Exception:
        return False


def get_sub_videos(period: str, shorts: bool, force: bool):
    def fetch():
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=YT_SUB_DAYS.get(period, 7))

        def channel_videos(acc):
            cid = acc.get("channelId") or ""
            if not cid:
                return []
            _, body = http_get("https://www.youtube.com/feeds/videos.xml?channel_id=%s" % cid, timeout=15)
            out = []
            for e in ET.fromstring(body).findall("a:entry", YT_ATOM):
                vid = e.findtext("yt:videoId", "", YT_ATOM)
                pub = e.findtext("a:published", "", YT_ATOM)
                try:
                    when = datetime.datetime.fromisoformat(pub.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if not vid or when < cutoff:
                    continue
                stats = e.find("media:group/media:community/media:statistics", YT_ATOM)
                out.append({
                    "id": vid, "title": e.findtext("a:title", "", YT_ATOM) or "(제목 없음)",
                    "channel": e.findtext("a:author/a:name", "", YT_ATOM),
                    "views": int(stats.get("views", 0)) if stats is not None else 0,
                    "likes": 0, "comments": 0, "length": "",
                    "thumbnail": "https://i.ytimg.com/vi/%s/hqdefault.jpg" % vid,
                    "published": pub,
                })
            return out

        vids = []
        with ThreadPoolExecutor(max_workers=4) as pool:
            for got in pool.map(channel_videos, list(YT_ACCOUNTS)):
                vids.extend(got)
        if shorts:
            with ThreadPoolExecutor(max_workers=8) as pool:
                keep = list(pool.map(lambda v: is_yt_short(v["id"]), vids))
            vids = [v for v, k in zip(vids, keep) if k]
        vids.sort(key=lambda v: v["views"], reverse=True)
        return vids

    return cached(("ytsub", period, shorts), force, fetch)


# ================================================================ 인스타그램 릴스
def load_accounts(path, defaults):
    try:
        with open(path) as f:
            accounts = json.load(f)
            if isinstance(accounts, list) and accounts:
                return accounts
    except (OSError, json.JSONDecodeError):
        pass
    return list(defaults)


def save_accounts(path, accounts):
    with open(path, "w") as f:
        json.dump(accounts, f, ensure_ascii=False, indent=2)


YT_ACCOUNTS = load_accounts(YT_ACCOUNTS_FILE, [])  # 위에서 정의한 유튜브 구독 채널 목록


# 계정 목록을 쓰는 소스별 설정 (파일 경로, 기본 계정)
ACCOUNT_SOURCES = {
    "reels": (ACCOUNTS_FILE, DEFAULT_IG_ACCOUNTS),
    "x": (X_ACCOUNTS_FILE, DEFAULT_X_ACCOUNTS),
    "threads": (THREADS_ACCOUNTS_FILE, DEFAULT_THREADS_ACCOUNTS),
    "tiktok": (TIKTOK_ACCOUNTS_FILE, DEFAULT_TIKTOK_ACCOUNTS),
}


def _parse_imginn_time(text: str) -> int:
    """'July 6th 2026, 06:19 pm' → epoch"""
    t = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", text.strip())
    try:
        return int(datetime.datetime.strptime(t, "%B %d %Y, %I:%M %p").timestamp())
    except ValueError:
        return 0


def fetch_ig_reels(username: str):
    """imginn.com(인스타그램 미러)에서 계정의 최근 릴스를 가져옵니다.
    인스타그램이 무인증 API를 차단(429)해 미러를 사용합니다.
    미러에는 조회수가 없어 0으로 두고, 좋아요·댓글은 상세 페이지에서 채웁니다."""
    items = []
    for path in ("/reels/%s/" % quote(username), "/%s/" % quote(username)):
        try:
            _, body = http_get(IMGINN_BASE + path, timeout=15)
        except Exception:
            continue
        page = body.decode("utf-8", "ignore")
        found = re.findall(
            r'<div class="item">.*?href="/p/([^/"]+)/".*?<img src="([^"]+)"', page, re.S)
        if found:
            items = found[:12]
            break
    reels = []
    for code, thumb in items:
        likes = comments = taken = 0
        title = ""
        try:
            _, body = http_get(IMGINN_BASE + "/p/%s/" % quote(code), timeout=15)
            page = body.decode("utf-8", "ignore")
            m = re.search(r'likes-count.*?<span>([\d,\.]+)</span>', page, re.S)
            likes = parse_view_count(m.group(1)) if m else 0
            m = re.search(r'comments-count.*?<span>([\d,\.]+)</span>', page, re.S)
            comments = parse_view_count(m.group(1)) if m else 0
            m = re.search(r'<div class="desc">(.*?)</div>', page, re.S)
            if m:
                title = htmllib.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
            m = re.search(r'Posted On:\s*([^<]+)', page)
            taken = _parse_imginn_time(m.group(1)) if m else 0
        except Exception:
            pass
        reels.append({
            "account": username,
            "title": title.split("\n")[0][:120] or "(설명 없음)",
            "views": 0,
            "likes": likes,
            "comments": comments,
            "thumbnail": htmllib.unescape(thumb),
            "url": "https://www.instagram.com/p/%s/" % code,
            "takenAt": taken,
        })
    return reels


def get_reels(force: bool):
    accounts = load_accounts(ACCOUNTS_FILE, DEFAULT_IG_ACCOUNTS)

    def fetch():
        # 계정당 상세 페이지를 여러 번 조회하므로 동시성을 낮게 유지합니다
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = pool.map(fetch_ig_reels, accounts)
        merged = [r for chunk in results for r in chunk]
        merged.sort(key=lambda r: (r["views"], r["likes"]), reverse=True)
        return merged
    reels, fetched_at = cached(("reels", tuple(accounts)), force, fetch)
    return reels, accounts, fetched_at


# ================================================================ X (트위터)
def _tweet_stats_token(tweet_id: str) -> str:
    """tweet-result CDN의 공개 토큰: ((id/1e15)*pi)를 36진수로 쓰고 '0'과 '.'을 제거.
    (트위터 임베드 위젯(react-tweet)이 쓰는 공개 알고리즘)"""
    n = (int(tweet_id) / 1e15) * math.pi
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    i, frac = int(n), n - int(n)
    s = "" if i else "0"
    while i:
        s = digits[i % 36] + s
        i //= 36
    s += "."
    for _ in range(12):
        frac *= 36
        d = int(frac)
        s += digits[d]
        frac -= d
    return re.sub(r"(0+|\.)", "", s)


def _fetch_tweet_stats(tweet_id: str):
    """임베드용 공개 CDN에서 트윗 1개의 본문·좋아요·댓글·미디어를 가져옵니다."""
    url = ("https://cdn.syndication.twimg.com/tweet-result?id=%s&token=%s"
           % (tweet_id, _tweet_stats_token(tweet_id)))
    try:
        return http_json(url, timeout=10)
    except Exception:
        return None


def fetch_x_posts(username: str):
    """nitter RSS로 최근 트윗 목록을 얻고, 트윗별 통계는 tweet-result CDN에서 채웁니다.
    (기존 syndication 타임라인 API가 2026년 무인증 접근을 차단(429)한 데 대한 대응.
    이 조합에는 리트윗·조회수가 없어 0으로 둡니다 — 좋아요·댓글은 실데이터)"""
    xml_text = None
    for host in NITTER_INSTANCES:
        try:
            _, body = http_get("https://%s/%s/rss" % (host, quote(username)),
                               headers={"User-Agent": RSS_UA,
                                        "Accept": "application/rss+xml, application/xml"},
                               timeout=12)
            if b"<item>" in body:
                xml_text = body.decode("utf-8", "ignore")
                break
        except Exception:
            continue
    if not xml_text:
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    DC = "{http://purl.org/dc/elements/1.1/}"
    base = []
    for item in root.iter("item"):
        link = item.findtext("link") or ""
        m = re.search(r"/status/(\d+)", link)
        if not m:
            continue
        creator = (item.findtext(DC + "creator") or "").lstrip("@")
        if creator and creator.lower() != username.lower():
            continue  # 리트윗 제외
        base.append({"id": m.group(1),
                     "title": item.findtext("title") or "",
                     "createdAt": item.findtext("pubDate") or ""})
    base = base[:12]
    with ThreadPoolExecutor(max_workers=4) as pool:
        stats = list(pool.map(lambda p: _fetch_tweet_stats(p["id"]), base))
    posts = []
    for p, d in zip(base, stats):
        d = d or {}
        media = ""
        for mm in (d.get("mediaDetails") or []):
            if mm.get("media_url_https"):
                media = mm["media_url_https"]
                break
        user = d.get("user") or {}
        posts.append({
            "account": username,
            "name": user.get("name", username),
            "text": (d.get("text") or p["title"]).strip(),
            "likes": d.get("favorite_count") or 0,
            "replies": d.get("conversation_count") or 0,
            "retweets": 0,
            "views": 0,
            "media": media,
            "url": "https://x.com/%s/status/%s" % (username, p["id"]),
            "createdAt": d.get("created_at") or p["createdAt"],
        })
    return posts


def get_x_posts(force: bool):
    accounts = load_accounts(X_ACCOUNTS_FILE, DEFAULT_X_ACCOUNTS)

    def fetch():
        # syndication은 동시 요청이 많으면 빈 응답을 주므로 동시성을 낮춥니다.
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = pool.map(fetch_x_posts, accounts)
        return [p for chunk in results for p in chunk]
    posts, fetched_at = cached(("x", tuple(accounts)), force, fetch)
    return posts, accounts, fetched_at


# ================================================================ 스레드(Threads)
def _threads_lsd_and_userid(username: str):
    """스레드 프로필 페이지에서 LSD 토큰을, 인스타 API에서 user_id를 얻습니다."""
    lsd = None
    try:
        _, body = http_get("https://www.threads.com/@" + quote(username), timeout=12)
        m = re.search(r'"LSD",\[\],\{"token":"([^"]+)"', body.decode("utf-8", "ignore"))
        lsd = m.group(1) if m else None
    except Exception:
        pass
    user_id = None
    try:
        info = http_json(
            "https://www.instagram.com/api/v1/users/web_profile_info/?username=" + quote(username),
            headers={"x-ig-app-id": IG_APP_ID}, timeout=12)
        user_id = (info.get("data") or {}).get("user", {}).get("id")
    except Exception:
        pass
    return lsd, user_id


# 스레드 프로필 탭 쿼리의 doc_id는 수시로 바뀌므로, 알려진 후보를 순서대로 시도합니다.
THREADS_DOC_IDS = [
    "25073444226023094", "7451607104958938", "23996318550159868",
    "9925907010825989", "26286467210919721",
]


def _fetch_threads_rsshub(username: str):
    """RSSHub 공개 인스턴스로 스레드 글을 가져옵니다(폴백).
    RSS에는 좋아요·댓글 수가 없어 통계는 0으로 둡니다 — 본문·링크·이미지는 실데이터."""
    for host in RSSHUB_INSTANCES:
        try:
            _, body = http_get("https://%s/threads/%s" % (host, quote(username)),
                               headers={"User-Agent": RSS_UA}, timeout=20)
        except Exception:
            continue
        if b"<item>" not in body:
            continue
        try:
            root = ET.fromstring(body.decode("utf-8", "ignore"))
        except ET.ParseError:
            continue
        posts = []
        for item in root.iter("item"):
            desc = item.findtext("description") or ""
            text = htmllib.unescape(re.sub(r"<[^>]+>", " ", desc)).strip()
            text = re.sub(r"\s+", " ", text) or (item.findtext("title") or "")
            text = re.sub(r"^@?%s\s*:\s*" % re.escape(username), "", text, flags=re.I)
            m = re.search(r'<img[^>]+src="([^"]+)"', desc)
            pub = item.findtext("pubDate") or ""
            try:
                ts = int(email.utils.parsedate_to_datetime(pub).timestamp())
            except (TypeError, ValueError):
                ts = 0
            posts.append({
                "account": username, "text": text[:280],
                "likes": 0, "replies": 0, "reposts": 0, "views": 0,
                "media": htmllib.unescape(m.group(1)) if m else "",
                "url": item.findtext("link") or "https://www.threads.com/@" + username,
                "createdAt": ts,
            })
        if posts:
            return posts
    return []


def fetch_threads_posts(username: str):
    lsd, user_id = _threads_lsd_and_userid(username)
    if not lsd or not user_id:
        return _fetch_threads_rsshub(username)
    from urllib.parse import urlencode
    headers = {
        "X-FB-LSD": lsd, "X-IG-App-ID": IG_APP_ID_THREADS,
        "Sec-Fetch-Site": "same-origin",
        "X-FB-Friendly-Name": "BarcelonaProfileThreadsTabQuery",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    for doc_id in THREADS_DOC_IDS:
        payload = urlencode({
            "lsd": lsd, "doc_id": doc_id,
            "variables": json.dumps({"userID": str(user_id), "__relay_internal__pv__BarcelonaIsLoggedInrelayprovider": False}),
        }).encode()
        req = urllib.request.Request("https://www.threads.com/api/graphql", data=payload)
        req.add_header("User-Agent", UA)
        for k, v in headers.items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = json.loads(resp.read().decode())
        except Exception:
            continue
        if data.get("errors"):
            continue
        posts = _parse_threads(data, username)
        if posts:
            return posts
    return _fetch_threads_rsshub(username)


def _parse_threads(data, username):
    posts = []

    def walk(o):
        if isinstance(o, dict):
            if "post" in o and isinstance(o["post"], dict) and o["post"].get("caption") is not None:
                p = o["post"]
                caption = (p.get("caption") or {}).get("text", "") if isinstance(p.get("caption"), dict) else ""
                info = p.get("text_post_app_info", {}) or {}
                imgs = (p.get("image_versions2") or {}).get("candidates") or []
                posts.append({
                    "account": username,
                    "text": caption[:280],
                    "likes": p.get("like_count") or 0,
                    "replies": info.get("direct_reply_count") or 0,
                    "reposts": info.get("repost_count") or 0,
                    "views": 0,
                    "media": imgs[0]["url"] if imgs else "",
                    "url": "https://www.threads.com/@%s/post/%s" % (username, p.get("code", "")),
                    "createdAt": p.get("taken_at") or 0,
                })
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(data)
    return posts


def get_threads_posts(force: bool):
    accounts = load_accounts(THREADS_ACCOUNTS_FILE, DEFAULT_THREADS_ACCOUNTS)

    def fetch():
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = pool.map(fetch_threads_posts, accounts)
        return [p for chunk in results for p in chunk]
    posts, fetched_at = cached(("threads", tuple(accounts)), force, fetch)
    return posts, accounts, fetched_at


# ================================================================ 틱톡(TikTok)
def _tiktok_item(v):
    author = v.get("author", {}) if isinstance(v.get("author"), dict) else {}
    handle = author.get("unique_id", "")
    vid = v.get("video_id", "")
    return {
        "account": handle,
        "name": author.get("nickname", handle),
        "title": (v.get("title") or "").strip() or "(설명 없음)",
        "views": v.get("play_count") or 0,
        "likes": v.get("digg_count") or 0,
        "comments": v.get("comment_count") or 0,
        "shares": v.get("share_count") or 0,
        "thumbnail": v.get("cover") or v.get("origin_cover") or "",
        "play": v.get("play") or "",  # 영상 파일 주소(페이지 안 재생용)
        "url": "https://www.tiktok.com/@%s/video/%s" % (handle, vid),
        "id": vid,
        "createdAt": v.get("create_time") or 0,
    }


def fetch_tiktok_user(handle: str):
    url = "%s/user/posts?unique_id=%s&count=12" % (TIKWM_BASE, quote(handle))
    try:
        d = http_json(url, timeout=15)
    except Exception:
        return []
    vids = (d.get("data") or {}).get("videos") or []
    return [_tiktok_item(v) for v in vids]


TIKTOK_HASHTAGS_FILE = os.path.join(BASE_DIR, "tiktok_hashtags.json")


def load_tt_hashtags():
    try:
        with open(TIKTOK_HASHTAGS_FILE, encoding="utf-8") as f:
            return [t for t in json.load(f) if isinstance(t, str) and t]
    except (OSError, json.JSONDecodeError):
        return []


TT_HASHTAGS = load_tt_hashtags()


def fetch_tiktok_hashtag(tag: str):
    """해시태그 하나의 한국 인기 게시물 TOP 30 (조회수순). 이름이 정확히 같은 해시태그를 우선합니다."""
    try:
        d = http_json("%s/challenge/search?keywords=%s&count=5" % (TIKWM_BASE, quote(tag)), timeout=15)
        tags = ((d.get("data") or {}).get("challenge_list")) or []
        exact = [t for t in tags if t.get("cha_name") == tag]
        if not (exact or tags):
            return []
        pick = exact[0] if exact else max(tags, key=lambda t: t.get("view_count") or 0)
        d = http_json("%s/challenge/posts?challenge_id=%s&count=30&cursor=0" % (TIKWM_BASE, pick["id"]), timeout=15)
    except Exception:
        return []
    vids = ((d.get("data") or {}).get("videos")) or []
    items = [_tiktok_item(v) for v in vids if v.get("region") == TIKTOK_REGION]
    items.sort(key=lambda p: p["views"], reverse=True)
    return items[:30]


def _tiktok_hashtag_posts(seed: str):
    """해시태그(챌린지) 하나의 게시물을 가져옵니다. 한국(KR) 게시물만 남깁니다."""
    try:
        d = http_json("%s/challenge/search?keywords=%s&count=5" % (TIKWM_BASE, quote(seed)), timeout=15)
        tags = ((d.get("data") or {}).get("challenge_list")) or []
        if not tags:
            return []
        tag = max(tags, key=lambda t: t.get("view_count") or 0)
        d = http_json("%s/challenge/posts?challenge_id=%s&count=30&cursor=0" % (TIKWM_BASE, tag["id"]), timeout=15)
    except Exception:
        return []
    vids = ((d.get("data") or {}).get("videos")) or []
    return [_tiktok_item(v) for v in vids if v.get("region") == TIKTOK_REGION]


def fetch_tiktok_trending(limit=30):
    """한국 인기 영상 TOP N. 틱톡에는 공식 지역 차트 API가 없어서, 한국 인기 해시태그들의 게시물을 모아 조회수순으로 자릅니다.
    shortcut: 공식 실시간 차트가 아니라 인기 해시태그 게시물 기준 근사치, 공식 차트 API가 생기면 교체"""
    with ThreadPoolExecutor(max_workers=2) as pool:  # tikwm 무료 티어 레이트리밋 때문에 동시성을 낮춤
        chunks = list(pool.map(_tiktok_hashtag_posts, TIKTOK_TREND_SEEDS))
    seen, unique = set(), []
    for p in (x for chunk in chunks for x in chunk):
        if p["id"] and p["id"] not in seen:
            seen.add(p["id"])
            unique.append(p)
    unique.sort(key=lambda p: p["views"], reverse=True)
    return unique[:limit]


def get_tiktok(force: bool):
    accounts = load_accounts(TIKTOK_ACCOUNTS_FILE, DEFAULT_TIKTOK_ACCOUNTS)

    def fetch():
        # 인기 피드와 구독 계정 최신 영상을 따로 모읍니다. tikwm 무료 티어의 레이트리밋을 피하려 동시성을 낮춥니다.
        trending = fetch_tiktok_trending()
        with ThreadPoolExecutor(max_workers=3) as pool:
            subscribed = [p for chunk in pool.map(fetch_tiktok_user, accounts) for p in chunk]
        seen, unique = set(), []
        for p in subscribed:
            if p["id"] and p["id"] not in seen:
                seen.add(p["id"])
                unique.append(p)
        unique.sort(key=lambda p: p["createdAt"], reverse=True)
        return {"trending": trending, "subscribed": unique}
    data, fetched_at = cached(("tiktok", tuple(accounts)), force, fetch)
    return data, accounts, fetched_at


# ================================================================ AI 영상 탭
def fetch_news():
    def one(feed):
        label, url = feed
        try:
            _, body = http_get(url, timeout=12)
            root = ET.fromstring(body)
        except Exception:
            return []
        items = []
        for item in root.iter("item"):
            title = item.findtext("title") or ""
            source = item.findtext("source") or ""
            pub = item.findtext("pubDate") or ""
            try:
                ts = email.utils.parsedate_to_datetime(pub).timestamp()
            except (TypeError, ValueError):
                ts = 0
            items.append({"region": label, "title": title, "source": source,
                          "link": item.findtext("link") or "", "ts": ts})
        return items[:25]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = pool.map(one, NEWS_FEEDS)
    merged = [n for chunk in results for n in chunk]
    merged.sort(key=lambda n: n["ts"], reverse=True)
    return merged[:40]


def fetch_hf_models():
    def one(args):
        pipeline, sort = args
        url = ("https://huggingface.co/api/models?pipeline_tag=%s&sort=%s"
               "&direction=-1&limit=12" % (pipeline, sort))
        try:
            data = http_json(url, timeout=12)
        except Exception:
            return []
        return [{"id": m.get("id", ""), "likes": m.get("likes", 0),
                 "downloads": m.get("downloads", 0), "pipeline": pipeline,
                 "createdAt": m.get("createdAt", "")} for m in data]

    jobs = [(p, s) for p in HF_PIPELINES for s in ("createdAt", "trendingScore")]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(one, jobs))

    def dedupe(lists):
        seen, out = set(), []
        for chunk in lists:
            for m in chunk:
                if m["id"] not in seen:
                    seen.add(m["id"])
                    out.append(m)
        return out
    latest = dedupe(results[0::2])
    latest.sort(key=lambda m: m["createdAt"], reverse=True)
    trending = dedupe(results[1::2])
    return {"latest": latest[:12], "trending": trending[:12]}


def get_ai_data(force: bool):
    # AI 탭은 '글'(모델·뉴스)만 제공합니다. AI 영상은 유튜브 탭의 'AI' 카테고리로 통합됨.
    def fetch():
        with ThreadPoolExecutor(max_workers=2) as pool:
            news_f = pool.submit(fetch_news)
            models_f = pool.submit(fetch_hf_models)
            return {"news": news_f.result(), "models": models_f.result()}
    return cached(("ai",), force, fetch)


# ================================================================ 기타
def fetch_oembed(url: str):
    """틱톡/유튜브 URL의 oEmbed 메타데이터를 가져옵니다 (CORS 우회용 프록시)."""
    host = urlparse(url).netloc.lower()
    if "tiktok.com" in host:
        endpoint = "https://www.tiktok.com/oembed?url=" + quote(url, safe="")
    elif "youtube.com" in host or "youtu.be" in host:
        endpoint = "https://www.youtube.com/oembed?format=json&url=" + quote(url, safe="")
    else:
        return {"ok": False, "reason": "unsupported"}
    try:
        data = http_json(endpoint, timeout=10)
        return {"ok": True, "title": data.get("title", ""),
                "author": data.get("author_name", ""),
                "thumbnail": data.get("thumbnail_url", "")}
    except Exception:
        return {"ok": False, "reason": "fetch_failed"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("[%s] %s" % (time.strftime("%H:%M:%S"), fmt % args))

    def _send(self, code, body, content_type="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        force = qs.get("force", ["0"])[0] == "1"

        if parsed.path in ("/", "/index.html"):
            with open(os.path.join(BASE_DIR, "index.html"), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
            return

        if parsed.path == "/api/yt/accounts":
            self._send(200, {"accounts": [a["handle"] for a in YT_ACCOUNTS]})
            return

        if parsed.path == "/api/videos" and qs.get("mode", ["keyword"])[0] == "sub":
            period = qs.get("period", ["week"])[0]
            shorts = qs.get("shorts", ["0"])[0] == "1"
            videos, fetched_at = get_sub_videos(period, shorts, force)
            self._send(200, {"videos": videos[:60], "fetchedAt": fetched_at})
            return

        if parsed.path == "/api/videos":
            category = qs.get("category", ["전체"])[0]
            period = qs.get("period", ["week"])[0]
            shorts = qs.get("shorts", ["0"])[0] == "1"
            enrich = qs.get("enrich", ["0"])[0] == "1"
            query = qs.get("q", [""])[0].strip()
            if not query and category != "전체" and category not in CATEGORIES:
                self._send(400, {"error": "unknown category"})
                return
            videos, fetched_at = get_videos(category, period, shorts, force, enrich, query)
            self._send(200, {"videos": videos[:60], "fetchedAt": fetched_at})
            return

        if parsed.path == "/api/categories":
            self._send(200, {"categories": ["전체"] + list(CATEGORIES.keys()), "details": CATEGORIES})
            return

        if parsed.path == "/api/reels":
            reels, accounts, fetched_at = get_reels(force)
            self._send(200, {"reels": reels[:80], "accounts": accounts, "fetchedAt": fetched_at})
            return

        if parsed.path == "/api/x":
            posts, accounts, fetched_at = get_x_posts(force)
            self._send(200, {"posts": posts, "accounts": accounts, "fetchedAt": fetched_at})
            return

        if parsed.path == "/api/threads":
            posts, accounts, fetched_at = get_threads_posts(force)
            self._send(200, {"posts": posts, "accounts": accounts, "fetchedAt": fetched_at})
            return

        if parsed.path == "/api/tiktok/hashtags":
            self._send(200, {"hashtags": TT_HASHTAGS})
            return
        if parsed.path == "/api/tiktok/hashtag":
            tag = (parse_qs(parsed.query).get("tag") or [""])[0].strip()
            if not tag:
                self._send(400, {"error": "tag required"})
                return
            force = parse_qs(parsed.query).get("force", ["0"])[0] == "1"
            posts, fetched_at = cached(("tthash", tag), force, lambda: fetch_tiktok_hashtag(tag))
            self._send(200, {"posts": posts, "fetchedAt": fetched_at})
            return
        if parsed.path == "/api/tiktok":
            data, accounts, fetched_at = get_tiktok(force)
            self._send(200, {"trending": data["trending"], "subscribed": data["subscribed"][:100],
                             "accounts": accounts, "fetchedAt": fetched_at})
            return

        if parsed.path == "/api/ai":
            data, fetched_at = get_ai_data(force)
            self._send(200, {**data, "fetchedAt": fetched_at})
            return

        if parsed.path == "/api/oembed":
            self._send(200, fetch_oembed(qs.get("url", [""])[0]))
            return

        if parsed.path == "/api/img":
            # 인스타/틱톡 CDN 등 핫링크가 막힌 썸네일을 서버가 대신 받아 전달(메모리 캐시)
            url = qs.get("u", [""])[0]
            host = urlparse(url).netloc.lower()
            if not url.startswith("https://") or not host.endswith(IMG_PROXY_ALLOW):
                self._send(400, {"error": "host not allowed"})
                return
            hit = _img_cache.get(url)
            if hit:
                self._send(200, hit[1], hit[0])
                return
            try:
                ctype, body = http_get(url, timeout=12)
                ctype = ctype or "image/jpeg"
                with _img_lock:
                    if len(_img_cache) > IMG_CACHE_MAX:
                        _img_cache.clear()
                    _img_cache[url] = (ctype, body)
                self._send(200, body, ctype)
            except Exception:
                self._send(502, {"error": "fetch failed"})
            return

        self._send(404, {"error": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/yt/accounts":
            length = int(self.headers.get("Content-Length", 0))
            try:
                req = json.loads(self.rfile.read(length).decode())
            except json.JSONDecodeError:
                self._send(400, {"error": "invalid json"})
                return
            handle = (req.get("username") or "").strip().lstrip("@")
            if req.get("action") == "add":
                if not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", handle) or any(a["handle"].lower() == handle.lower() for a in YT_ACCOUNTS):
                    self._send(400, {"error": "채널 핸들을 확인해 주세요 (@ 뒤의 영문·숫자 이름, 중복 불가)"})
                    return
                try:
                    cid = resolve_yt_channel(handle)
                except Exception:
                    cid = ""
                if not cid:
                    self._send(400, {"error": "채널을 찾지 못했습니다. 핸들을 다시 확인해 주세요"})
                    return
                YT_ACCOUNTS.append({"handle": handle, "channelId": cid})
            elif req.get("action") == "remove":
                before = len(YT_ACCOUNTS)
                YT_ACCOUNTS[:] = [a for a in YT_ACCOUNTS if a["handle"].lower() != handle.lower()]
                if len(YT_ACCOUNTS) == before:
                    self._send(400, {"error": "없는 채널입니다"})
                    return
            else:
                self._send(400, {"error": "unknown action"})
                return
            save_accounts(YT_ACCOUNTS_FILE, YT_ACCOUNTS)
            with _cache_lock:
                for key in [k for k in _cache if k[0] == "ytsub"]:
                    del _cache[key]
            self._send(200, {"accounts": [a["handle"] for a in YT_ACCOUNTS]})
            return
        if parsed.path == "/api/tiktok/hashtags":
            length = int(self.headers.get("Content-Length", 0))
            try:
                req = json.loads(self.rfile.read(length).decode())
            except json.JSONDecodeError:
                self._send(400, {"error": "invalid json"})
                return
            name = (req.get("name") or "").strip().lstrip("#").strip()
            if req.get("action") == "add":
                if not name or len(name) > 30 or name in TT_HASHTAGS:
                    self._send(400, {"error": "해시태그 이름을 확인해 주세요 (중복 불가, 30자 이내)"})
                    return
                TT_HASHTAGS.append(name)
            elif req.get("action") == "remove":
                if name not in TT_HASHTAGS:
                    self._send(400, {"error": "없는 해시태그입니다"})
                    return
                TT_HASHTAGS.remove(name)
            else:
                self._send(400, {"error": "unknown action"})
                return
            with open(TIKTOK_HASHTAGS_FILE, "w", encoding="utf-8") as f:
                json.dump(TT_HASHTAGS, f, ensure_ascii=False, indent=2)
            self._send(200, {"hashtags": TT_HASHTAGS})
            return
        if parsed.path == "/api/categories":
            length = int(self.headers.get("Content-Length", 0))
            try:
                req = json.loads(self.rfile.read(length).decode())
            except json.JSONDecodeError:
                self._send(400, {"error": "invalid json"})
                return
            name = (req.get("name") or "").strip()
            if req.get("action") in ("add", "update"):
                keywords = parse_terms(req.get("keywords"))[:5]
                exclude = parse_terms(req.get("exclude"))[:10]
                if not name or name == "전체" or not keywords:
                    self._send(400, {"error": "이름과 검색 키워드 1~5개가 필요합니다"})
                    return
                if req["action"] == "update" and name not in CATEGORIES:
                    self._send(400, {"error": "수정할 카테고리가 없습니다"})
                    return
                CATEGORIES[name] = {"keywords": keywords, "exclude": exclude}
            elif req.get("action") == "reorder":
                names = req.get("names") or []
                if sorted(names) != sorted(CATEGORIES.keys()):
                    self._send(400, {"error": "카테고리 목록이 맞지 않습니다"})
                    return
                items = [(n, CATEGORIES.pop(n)) for n in names]
                CATEGORIES.update(items)  # 같은 dict 안에서 순서만 바꿈
            elif req.get("action") == "remove":
                if name == "전체" or name not in CATEGORIES:
                    self._send(400, {"error": "삭제할 수 없는 카테고리입니다"})
                    return
                del CATEGORIES[name]
            else:
                self._send(400, {"error": "unknown action"})
                return
            with open(CATEGORIES_FILE, "w", encoding="utf-8") as f:
                json.dump(CATEGORIES, f, ensure_ascii=False, indent=2)
            with _cache_lock:
                _cache.clear()  # 검색어가 바뀌었으니 캐시된 영상 목록을 버림
            self._send(200, {"categories": ["전체"] + list(CATEGORIES.keys()), "details": CATEGORIES})
            return
        # /api/{reels|x|threads}/accounts — 구독 계정 추가/삭제
        m = re.match(r"^/api/(reels|x|threads)/accounts$", parsed.path)
        if m:
            source = m.group(1)
            path, defaults = ACCOUNT_SOURCES[source]
            length = int(self.headers.get("Content-Length", 0))
            try:
                req = json.loads(self.rfile.read(length).decode())
            except json.JSONDecodeError:
                self._send(400, {"error": "invalid json"})
                return
            action = req.get("action")
            raw = (req.get("username") or "").strip().lstrip("@")
            # X는 대소문자 보존, 인스타/스레드는 소문자
            username = raw if source == "x" else raw.lower()
            accounts = load_accounts(path, defaults)
            if action == "add" and username and username not in accounts:
                accounts.append(username)
            elif action == "remove" and username in accounts:
                accounts.remove(username)
            save_accounts(path, accounts)
            self._send(200, {"accounts": accounts})
            return
        self._send(404, {"error": "not found"})


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"트렌드 뷰어 실행 중: http://localhost:{PORT}")
    server.serve_forever()
