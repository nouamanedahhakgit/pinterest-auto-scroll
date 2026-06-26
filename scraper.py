"""
Hybrid WordPress + AI scraper.

Quick scrape:
  1. Try WordPress REST API  →  fast, reliable, all posts
  2. If not WordPress or API blocked  →  AI crawl (any site)

Deep scrape per post:
  1. If AI key set        →  Grok extracts full structured blocks (best quality)
  2. If WP REST API       →  get raw content HTML, parse with ContentParser
  3. HTML fallback        →  fetch URL, parse with ContentParser
"""
from __future__ import annotations
import json
import re
import time
import requests
from collections.abc import Callable
from bs4 import BeautifulSoup, NavigableString
from html import unescape as html_unescape
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode, parse_qs, unquote
from datetime import datetime

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}
# Prefer JSON so hosts that serve SPA HTML for unknown routes return real WP JSON when possible.
WP_HEADERS = {
    **HEADERS,
    "Accept": "application/json, */*;q=0.1",
}
TIMEOUT = 20


def _http_response_looks_json(r: requests.Response) -> bool:
    """Reject HTML pages mistaken for the WordPress REST API (common on SPA / catch-all hosts)."""
    raw = (r.text or "").lstrip()
    if raw.startswith("<") or raw.startswith("<!"):
        return False
    if raw.startswith("{") or raw.startswith("["):
        return True
    ct = (r.headers.get("Content-Type") or "").lower()
    if "text/html" in ct:
        return False
    if "application/json" in ct or "application/problem+json" in ct:
        return True
    return False

# ─── Utilities ────────────────────────────────────────────────────────────────

def normalize_url(url: str) -> str:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url.rstrip("/")


def normalize_post_url(url: str) -> str:
    """
    Canonical article URL for deduplication: stable scheme/host, strip trailing slash on path,
    drop tracking query params. Same post from sitemap, RSS, and AI often differs only here.
    """
    u = (url or "").strip()
    if not u:
        return ""
    try:
        p = urlparse(u)
        scheme = (p.scheme or "https").lower()
        if scheme not in ("http", "https"):
            scheme = "https"
        netloc = (p.netloc or "").lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        path = p.path or "/"
        if len(path) > 1 and path.endswith("/"):
            path = path.rstrip("/")
        track = frozenset({
            "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
            "fbclid", "gclid", "mc_cid", "mc_eid", "_ga", "ref",
        })
        q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if k.lower() not in track]
        query = urlencode(sorted(q)) if q else ""
        return urlunparse((scheme, netloc, path, "", query, ""))
    except Exception:
        return u.split("#")[0].lower().rstrip("/")


def dedupe_posts_by_link(posts: list) -> list:
    """Remove duplicate posts that share the same normalized link (e.g. REST quirks)."""
    seen = set()
    out = []
    for p in posts:
        if not isinstance(p, dict):
            continue
        link = p.get("link") or ""
        key = normalize_post_url(link)
        if not key:
            out.append(p)
            continue
        if key in seen:
            continue
        seen.add(key)
        merged = dict(p)
        merged["link"] = key
        out.append(merged)
    return out


def safe_filename(post_id, slug: str) -> str:
    slug = re.sub(r"[^\w-]", "", slug or "")[:60] or str(post_id)
    return f"{post_id}-{slug}"


def fmt_date(iso: str) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).strftime("%B %d, %Y")
    except Exception:
        return iso


def abs_url(src: str, base: str) -> str:
    if not src:
        return ""
    if src.startswith(("data:", "blob:")):
        return ""
    if src.startswith("//"):
        return "https:" + src
    if src.startswith(("http://", "https://")):
        return src
    return urljoin(base, src)


def extract_hero_image_from_html(html: str, page_url: str) -> str:
    """
    Best-effort hero/thumbnail URL from full page HTML (og:image, Twitter card, JSON-LD).
    Sitemaps often omit images (e.g. Drupal XML sitemap); this fills featured_image after fetch.
    """
    try:
        soup = BeautifulSoup(html, "lxml")
        for meta in soup.find_all("meta"):
            prop = (meta.get("property") or "").lower()
            name = (meta.get("name") or "").lower()
            if prop in ("og:image", "og:image:secure_url", "og:image:url"):
                c = (meta.get("content") or "").strip()
                if c:
                    return abs_url(c, page_url)
            if name in ("twitter:image", "twitter:image:src"):
                c = (meta.get("content") or "").strip()
                if c:
                    return abs_url(c, page_url)
        for link in soup.find_all("link", href=True):
            rel = link.get("rel")
            rel_s = " ".join(rel) if isinstance(rel, list) else (rel or "")
            if "image_src" in rel_s.lower():
                return abs_url(link["href"], page_url)
        for script in soup.find_all("script", type="application/ld+json"):
            raw = (script.string or "").strip()
            if not raw:
                continue
            try:
                blob = json.loads(raw)
            except Exception:
                continue
            candidates = blob if isinstance(blob, list) else [blob]
            for item in candidates:
                if not isinstance(item, dict):
                    continue
                typ = item.get("@type", "")
                if isinstance(typ, list):
                    typ = ",".join(str(t) for t in typ)
                if not any(x in str(typ) for x in ("Recipe", "Article", "WebPage", "BlogPosting")):
                    continue
                img = item.get("image")
                if isinstance(img, str) and img.startswith("http"):
                    return abs_url(img, page_url)
                if isinstance(img, dict) and img.get("url"):
                    return abs_url(img["url"], page_url)
                if isinstance(img, list) and img:
                    first = img[0]
                    if isinstance(first, str) and first.startswith("http"):
                        return abs_url(first, page_url)
                    if isinstance(first, dict) and first.get("url"):
                        return abs_url(first["url"], page_url)
    except Exception:
        pass
    return ""


def infer_site_stack(html: str, base_url: str, result: dict,
                     headers: dict | None = None) -> dict:
    """
    Best-effort fingerprint: CMS (WordPress, Drupal, …), JS stack (React, Next, …),
    serving style (static, SPA, CMS), optional X-Powered-By / meta generator.
    """
    low = html.lower()
    hdr = {k.lower(): v for k, v in (headers or {}).items()}
    powered = (hdr.get("x-powered-by") or "").lower()

    generator = ""
    try:
        soup = BeautifulSoup(html, "lxml")
        g = soup.find("meta", attrs={"name": "generator"})
        if g:
            generator = (g.get("content") or "").strip()
    except Exception:
        pass

    cms = "none"
    gl = generator.lower()
    if "drupal" in gl:
        cms = "Drupal"
    elif "wordpress" in gl:
        cms = "WordPress"
    elif "shopify" in gl:
        cms = "Shopify"
    elif "ghost" in gl:
        cms = "Ghost"
    elif result.get("api_available") or result.get("is_wordpress"):
        cms = "WordPress"
    elif "wp-content" in low or "wp-includes" in low:
        cms = "WordPress"
    elif "drupal" in low or "/sites/default/files/" in low or "data-drupal" in low or "drupal.js" in low:
        cms = "Drupal"
    elif "cdn.shopify" in low or "shopifycdn" in low:
        cms = "Shopify"
    elif "ghost-" in low and "ghost.org" in low:
        cms = "Ghost"
    elif "squarespace" in low:
        cms = "Squarespace"
    elif "wixstatic" in low or "wix.com" in low:
        cms = "Wix"

    frontend: list = []
    if "__next_data__" in low or "/_next/" in low:
        frontend.append("Next.js")
    elif "/_nuxt/" in low or "window.__nuxt" in low or "__nuxt" in low:
        frontend.append("Nuxt")
    elif "ng-version" in low or "angular.js" in low or "/angular/" in low:
        frontend.append("Angular")
    elif "react-dom" in low or "react.production" in low or "react.development" in low:
        frontend.append("React")
    elif "vue" in low and ("vue.js" in low or "__vue" in low or "vue.min.js" in low):
        frontend.append("Vue")
    elif "svelte" in low:
        frontend.append("Svelte")
    elif "gatsby" in low:
        frontend.append("Gatsby")

    backend: list = []
    if "flask" in powered:
        backend.append("Flask")
    if "django" in powered:
        backend.append("Django")
    if "express" in powered:
        backend.append("Express")
    if "php" in powered:
        backend.append("PHP")

    if cms == "none" and not frontend:
        serving = "static or minimal JS"
    elif cms != "none" and not frontend:
        serving = "server-rendered (CMS)"
    elif frontend and cms == "none":
        serving = "client-side (SPA)"
    else:
        serving = "hybrid (CMS + framework)"

    return {
        "cms": cms,
        "frontend": frontend if frontend else ["—"],
        "serving": serving,
        "backend_hint": backend[0] if backend else None,
        "generator_meta": generator or None,
        "x_powered_by": hdr.get("x-powered-by"),
    }


def format_site_stack_line(stack: dict, site_type_ai: str | None) -> str:
    """One-line summary for UI and sites.json."""
    parts = []
    if stack.get("cms") and stack["cms"] != "none":
        parts.append(stack["cms"])
    fe = [x for x in (stack.get("frontend") or []) if x and x != "—"]
    if fe:
        parts.append("/".join(fe))
    if stack.get("serving"):
        parts.append(stack["serving"])
    if site_type_ai:
        parts.append(site_type_ai.replace("_", " "))
    if stack.get("backend_hint"):
        parts.append(stack["backend_hint"])
    return " · ".join(parts)


def best_src(img_tag) -> str:
    srcset = img_tag.get("srcset", "") or img_tag.get("data-srcset", "")
    if srcset:
        entries = [s.strip().split() for s in srcset.split(",") if s.strip()]
        valid = [e for e in entries if e]
        if valid:
            best = max(valid, key=lambda e: int(re.sub(r"\D", "", e[1])) if len(e) > 1 else 0)
            if best:
                return best[0]
    return (img_tag.get("src", "")
            or img_tag.get("data-src", "")
            or img_tag.get("data-lazy-src", ""))


# ─── WordPress detection ──────────────────────────────────────────────────────

def detect_wordpress(base_url: str) -> bool:
    try:
        r = requests.get(f"{base_url}/wp-json/", headers=WP_HEADERS, timeout=TIMEOUT)
        if r.status_code != 200 or not _http_response_looks_json(r):
            return False
        data = r.json()
        if isinstance(data, dict) and data.get("namespaces"):
            return True
    except Exception:
        pass
    try:
        r = requests.get(base_url, headers=HEADERS, timeout=TIMEOUT)
        text = r.text.lower()
        if "wp-content" in text or "wp-json" in text or "wordpress" in text:
            return True
        soup = BeautifulSoup(r.text, "lxml")
        gen = soup.find("meta", attrs={"name": "generator"})
        if gen and "wordpress" in gen.get("content", "").lower():
            return True
    except Exception:
        pass
    return False


# ─── REST API helpers ─────────────────────────────────────────────────────────

def _fetch_rest_page(base_url: str, endpoint: str, params: dict) -> tuple:
    """
    Try standard /wp-json/… then ?rest_route=… (plain permalinks / some proxies).
    Returns (data_list_or_none, headers_or_none).
    """
    base = base_url.rstrip("/")
    urls = [
        (f"{base}/wp-json/wp/v2/{endpoint}", params),
        (f"{base}/index.php", {**params, "rest_route": f"/wp/v2/{endpoint}"}),
        (f"{base}/", {**params, "rest_route": f"/wp/v2/{endpoint}"}),
    ]
    for url, qp in urls:
        try:
            r = requests.get(url, headers=WP_HEADERS, params=qp, timeout=TIMEOUT)
            if r.status_code != 200 or not _http_response_looks_json(r):
                continue
            data = r.json()
            if isinstance(data, list):
                return data, r.headers
        except Exception:
            continue
    return None, None


def _rest_batch_detail_line(raw_items: list) -> str:
    """Append to progress logs: last item link (+ short title) from a REST page."""
    if not raw_items:
        return ""
    last = raw_items[-1]
    if not isinstance(last, dict):
        return ""
    link = (last.get("link") or "").strip()
    if not link:
        return ""
    title = ""
    t = last.get("title")
    if isinstance(t, dict):
        title = BeautifulSoup(t.get("rendered") or "", "lxml").get_text(strip=True)[:45]
    elif isinstance(t, str):
        title = t[:45]
    if len(link) > 92:
        link = link[:90] + "…"
    if title:
        return f' — last on page: "{title}" → {link}'
    return f" — last on page: {link}"


def fetch_rest(base_url: str, endpoint: str, params: dict = None,
               max_pages: int = 200, progress_cb=None,
               progress_label: str | None = None,
               cancel_check: Callable[[], bool] | None = None) -> tuple[list, bool]:
    """
    Paginate WordPress REST collections. Optional progress_cb is invoked after each page
    so long-running fetches (many posts) show activity in the UI.
    If cancel_check returns True, returns (partial_results, True) without fetching more pages.
    """
    label = progress_label or endpoint
    results = []
    page = 1
    while True:
        if cancel_check and cancel_check():
            return results, True
        p = {"per_page": 100, "page": page, **(params or {})}
        data, headers = _fetch_rest_page(base_url, endpoint, p)
        if data is None:
            break
        if not data:
            break
        results.extend(data)
        hdr = headers or {}
        try:
            total_pages = int(hdr.get("X-WP-TotalPages") or 1)
        except (TypeError, ValueError):
            total_pages = 1
        total_hdr = hdr.get("X-WP-Total")
        detail = _rest_batch_detail_line(data)
        if progress_cb:
            if total_hdr is not None:
                try:
                    total_n = int(total_hdr)
                    progress_cb(
                        f"REST {label}: page {page}/{total_pages} — "
                        f"{len(results)} of {total_n} items{detail}"
                    )
                except (TypeError, ValueError):
                    progress_cb(
                        f"REST {label}: page {page}/{total_pages} — "
                        f"{len(results)} items{detail}"
                    )
            else:
                progress_cb(
                    f"REST {label}: page {page}/{total_pages} — "
                    f"{len(results)} items so far{detail}"
                )
        if cancel_check and cancel_check():
            return results, True
        if page >= total_pages or page >= max_pages:
            break
        page += 1
        if cancel_check and cancel_check():
            return results, True
        time.sleep(0.25)
    return results, False


def _path_is_listing_hub(link: str) -> bool:
    """Single-segment paths that are usually archive / blog index pages, not single articles."""
    from urllib.parse import urlparse

    parts = [x for x in urlparse(link or "").path.strip("/").split("/") if x]
    if len(parts) != 1:
        return False
    return parts[0].lower() in (
        "blog",
        "news",
        "archives",
        "archive",
        "recipes",
        "articles",
    )


def _discovery_content_kind_from_post(link: str, title: str, wp_type: str) -> str:
    """
    Lightweight classification for exports / filters (no AI). WordPress still stores
    travel guides, maps, etc. as posts — we only label likely non-recipe content.
    """
    low = ((title or "") + " " + (link or "")).lower()
    if wp_type and wp_type != "post":
        return wp_type
    if _path_is_listing_hub(link):
        return "listing_hub"
    if any(
        x in low
        for x in (
            "food map",
            "travel map",
            "city map",
            "restaurant map",
            "downloadable map",
        )
    ):
        return "travel_or_map"
    return "article"


def parse_embedded_post(p: dict) -> dict:
    embedded  = p.get("_embedded", {})
    categories, tags = [], []
    for term_group in embedded.get("wp:term", []):
        for term in term_group:
            entry = {"id": term.get("id"), "name": term.get("name", ""),
                     "slug": term.get("slug", ""), "link": term.get("link", "")}
            if term.get("taxonomy") == "category":
                categories.append(entry)
            elif term.get("taxonomy") == "post_tag":
                tags.append(entry)

    featured_image = None
    fm_list = embedded.get("wp:featuredmedia", [])
    if fm_list:
        fm = fm_list[0]
        featured_image = (
            fm.get("source_url")
            or (fm.get("media_details", {}) or {})
               .get("sizes", {}).get("full", {}).get("source_url")
        )

    excerpt = BeautifulSoup(
        p.get("excerpt", {}).get("rendered", ""), "lxml"
    ).get_text(strip=True)
    slug = p.get("slug", "")
    post_id = p.get("id")
    title = p.get("title", {}).get("rendered", "")
    link = p.get("link", "")
    wp_type = (p.get("type") or "post").strip() or "post"
    kind = _discovery_content_kind_from_post(link, title, wp_type)
    out = {
        "id": post_id, "slug": slug,
        "title": title,
        "excerpt": excerpt,
        "link": link,
        "date": p.get("date", ""),
        "status": p.get("status", ""),
        "categories": categories, "tags": tags,
        "featured_image": featured_image,
        "filename": safe_filename(post_id, slug),
        "source": "api",
        "discovery": {
            "wp_type": wp_type,
            "content_kind": kind,
        },
    }
    return out


def fetch_wordpress_rest_bundle(
    base_url: str,
    progress_cb=None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict:
    """
    Load posts, pages, categories, tags, media from WordPress REST endpoints.
    Used when the REST index is valid, or when the site is WordPress but the index URL returned HTML.
    """
    if progress_cb:
        progress_cb("WordPress REST: downloading posts (API pagination, not HTML crawl)…")

    # Do NOT use _embed on the listing call — it makes each page response ~5× larger
    # and causes timeouts on big sites (1000+ posts). We fetch featured media separately.
    raw, cancelled = fetch_rest(
        base_url,
        "posts",
        {
            "_fields": "id,date,modified,title,excerpt,link,slug,status,categories,tags,featured_media",
        },
        progress_cb=progress_cb,
        progress_label="posts",
        cancel_check=cancel_check,
    )

    # ── Build a featured-media id → URL map (one quick call, first page only) ──
    _fm_map: dict[int, str] = {}
    try:
        _fm_raw, _ = fetch_rest(
            base_url, "media",
            {"_fields": "id,source_url", "per_page": 100},
            max_pages=3, progress_cb=None,
        )
        for _m in _fm_raw:
            if _m.get("id") and _m.get("source_url"):
                _fm_map[int(_m["id"])] = _m["source_url"]
    except Exception:
        pass

    # ── Also build a category-id → name map ───────────────────────────────────
    _cat_map: dict[int, str] = {}

    def _parse_lite_post(p: dict) -> dict:
        """Convert a lightweight REST post object (no _embed) to our internal format."""
        title   = (p.get("title") or {}).get("rendered", p.get("slug", ""))
        excerpt = BeautifulSoup((p.get("excerpt") or {}).get("rendered", ""), "lxml").get_text(" ", strip=True)[:300]
        link    = p.get("link", "")
        slug    = p.get("slug", "")
        date    = (p.get("date") or "")[:10]
        fm_id   = p.get("featured_media") or 0
        fi      = _fm_map.get(int(fm_id), "") if fm_id else ""
        cats    = [{"id": cid, "name": _cat_map.get(cid, "")} for cid in (p.get("categories") or [])]
        fn      = safe_filename(p.get("id"), slug)
        return {
            "id": p.get("id"), "title": title, "date": date, "link": link,
            "slug": slug, "excerpt": excerpt, "featured_image": fi,
            "categories": cats, "filename": fn, "source": "rest",
        }

    posts = dedupe_posts_by_link([_parse_lite_post(p) for p in raw])
    if cancelled:
        if progress_cb:
            progress_cb(
                f"Stopped — saved {len(posts)} posts (partial). Pages, tags, and media were skipped."
            )
        return {
            "posts": posts,
            "pages": [],
            "categories": [],
            "tags": [],
            "media": [],
            "cancelled": True,
        }

    if progress_cb:
        progress_cb("WordPress REST: downloading pages…")
    raw_pages, cancelled = fetch_rest(
        base_url,
        "pages",
        {"_fields": "id,date,title,excerpt,link,slug,status"},
        progress_cb=progress_cb,
        progress_label="pages",
        cancel_check=cancel_check,
    )
    pages = [_parse_lite_post(p) for p in raw_pages]
    if cancelled:
        if progress_cb:
            progress_cb(
                f"Stopped — saved {len(posts)} posts and {len(pages)} pages (partial). "
                "Categories, tags, and media were skipped."
            )
        return {
            "posts": posts,
            "pages": pages,
            "categories": [],
            "tags": [],
            "media": [],
            "cancelled": True,
        }

    if progress_cb:
        progress_cb("WordPress REST: downloading categories…")
    raw_cats, cancelled = fetch_rest(
        base_url,
        "categories",
        {"_fields": "id,name,slug,count,link"},
        progress_cb=progress_cb,
        progress_label="categories",
        cancel_check=cancel_check,
    )
    categories = [
        {"id": c.get("id"), "name": c.get("name", ""), "slug": c.get("slug", ""),
         "count": c.get("count", 0), "link": c.get("link", "")}
        for c in raw_cats
    ]
    # Populate category name map now that we have all categories
    for c in categories:
        if c.get("id") and c.get("name"):
            _cat_map[int(c["id"])] = c["name"]
    # Backfill category names into posts (were empty during listing phase)
    for p in posts:
        p["categories"] = [
            {"id": cat["id"], "name": _cat_map.get(int(cat["id"]) if cat["id"] else 0, cat.get("name", ""))}
            for cat in p.get("categories", [])
        ]
    if cancelled:
        if progress_cb:
            progress_cb("Stopped — partial bundle (categories/tags/media incomplete).")
        return {
            "posts": posts,
            "pages": pages,
            "categories": categories,
            "tags": [],
            "media": [],
            "cancelled": True,
        }

    if progress_cb:
        progress_cb("WordPress REST: downloading tags…")
    raw_tags, cancelled = fetch_rest(
        base_url,
        "tags",
        {"_fields": "id,name,slug,count,link"},
        progress_cb=progress_cb,
        progress_label="tags",
        cancel_check=cancel_check,
    )
    tags = [
        {"id": t.get("id"), "name": t.get("name", ""), "slug": t.get("slug", ""),
         "count": t.get("count", 0), "link": t.get("link", "")}
        for t in raw_tags
    ]
    if cancelled:
        if progress_cb:
            progress_cb("Stopped — partial bundle (tags/media incomplete).")
        return {
            "posts": posts,
            "pages": pages,
            "categories": categories,
            "tags": tags,
            "media": [],
            "cancelled": True,
        }

    if progress_cb:
        progress_cb("WordPress REST: sampling media (first page)…")
    raw_media, cancelled = fetch_rest(
        base_url,
        "media",
        {
            "_fields": "id,title,source_url,mime_type,date,alt_text",
            "per_page": 100,
        },
        max_pages=1,
        progress_cb=progress_cb,
        progress_label="media",
        cancel_check=cancel_check,
    )
    media = [
        {"id": m.get("id"),
         "title": m.get("title", {}).get("rendered", ""),
         "url": m.get("source_url", ""),
         "mime_type": m.get("mime_type", ""),
         "alt_text": m.get("alt_text", ""),
         "date": m.get("date", "")}
        for m in raw_media
    ]

    if cancelled:
        if progress_cb:
            progress_cb("Stopped — media list may be incomplete.")
        return {
            "posts": posts,
            "pages": pages,
            "categories": categories,
            "tags": tags,
            "media": media,
            "cancelled": True,
        }

    # ── Sitemap merge: find posts in sitemap that REST didn't return ─────────────
    # This catches posts REST truncated due to timeouts, API limits, or embed overhead.
    try:
        if progress_cb:
            progress_cb("Checking sitemap for additional posts not in REST results…")
        from ai_scraper import discover_posts_sitemap_rss
        _sm = discover_posts_sitemap_rss(base_url, progress_cb=None)
        _sm_posts = _sm.get("posts", [])
        if _sm_posts:
            # Build a set of known URLs (normalised, no trailing slash)
            _known = {p["link"].rstrip("/") for p in posts if p.get("link")}
            _added = 0
            for _sp in _sm_posts:
                _url = (_sp.get("link") or "").rstrip("/")
                if _url and _url not in _known:
                    _known.add(_url)
                    posts.append(_sp)
                    _added += 1
            if _added and progress_cb:
                progress_cb(f"Sitemap added {_added} more posts — total: {len(posts)}")
    except Exception as _sm_err:
        if progress_cb:
            progress_cb(f"Sitemap check skipped: {_sm_err}")

    return {
        "posts": posts, "pages": pages, "categories": categories,
        "tags": tags, "media": media,
    }


# ─── Phase 1: Quick scrape ────────────────────────────────────────────────────

def scrape_site(
    url: str,
    grok_client=None,
    progress_cb=None,
    cancel_check: Callable[[], bool] | None = None,
    checkpoint_cb: Callable[[dict], None] | None = None,
) -> dict:
    """
    Quick scrape: discover all posts, categories, site info.
    WordPress: REST API + sitemap/RSS only (no AI). Other sites: AI crawl when an API key is set.
    """
    base_url = normalize_url(url)
    result = {
        "url": base_url,
        "is_wordpress": False,
        "api_available": False,
        "ai_assisted": False,
        "cancelled": False,
        "posts": [], "pages": [], "categories": [], "tags": [], "media": [],
        "site_info": {}, "errors": [],
    }

    if progress_cb:
        progress_cb("Detecting site type…")

    result["is_wordpress"] = detect_wordpress(base_url)

    # Try WP REST API (must be real JSON — many SPAs return 200 HTML for /wp-json/)
    try:
        r = requests.get(f"{base_url}/wp-json/", headers=WP_HEADERS, timeout=TIMEOUT)
        if r.status_code == 200 and _http_response_looks_json(r):
            info = r.json()
            if isinstance(info, dict) and info.get("namespaces"):
                result["api_available"] = True
                result["site_info"] = {
                    "name":        info.get("name", ""),
                    "description": info.get("description", ""),
                    "url":         info.get("url", base_url),
                }
    except Exception as e:
        result["errors"].append(f"REST root: {e}")

    if result["api_available"]:
        # ── WordPress REST API path ────────────────────────────────────────────
        if progress_cb:
            progress_cb("WordPress REST API found — fetching posts…")

        bundle = fetch_wordpress_rest_bundle(
            base_url, progress_cb=progress_cb, cancel_check=cancel_check
        )
        result["posts"] = bundle["posts"]
        result["pages"] = bundle["pages"]
        result["categories"] = bundle["categories"]
        result["tags"] = bundle["tags"]
        result["media"] = bundle["media"]
        if bundle.get("cancelled"):
            result["cancelled"] = True
            result["errors"].append("Discovery stopped by user — partial list.")
        elif progress_cb:
            progress_cb(f"Found {len(result['posts'])} posts (REST + sitemap merged)")

        # Fallback: if REST returned nothing at all, try sitemap standalone
        if not result.get("cancelled") and not result["posts"]:
            if progress_cb:
                progress_cb("REST returned 0 posts — falling back to sitemap/RSS…")
            from ai_scraper import discover_posts_sitemap_rss

            fb = discover_posts_sitemap_rss(base_url, progress_cb=progress_cb)
            result["posts"] = fb.get("posts", [])
            if fb.get("pages"):
                result["pages"] = dedupe_posts_by_link(
                    (result.get("pages") or []) + fb["pages"]
                )
            if fb.get("categories"):
                result["categories"] = fb["categories"]
            if fb.get("site_info"):
                si = result.get("site_info") or {}
                result["site_info"] = {**si, **fb["site_info"]}
            result["errors"].extend(fb.get("errors", []))
            if not result["posts"]:
                result["errors"].append(
                    "No posts via WordPress REST, sitemap, or RSS. "
                    "Check that posts are public and REST is not blocked."
                )

    elif result["is_wordpress"]:
        # ── WordPress but /wp-json/ index was HTML (SPA, etc.) — still no AI ───
        if progress_cb:
            progress_cb(
                "WordPress detected — fetching /wp-json/wp/v2/… + sitemap if needed (no AI)…"
            )
        bundle = fetch_wordpress_rest_bundle(
            base_url, progress_cb=progress_cb, cancel_check=cancel_check
        )
        result["posts"] = bundle["posts"]
        result["pages"] = bundle["pages"]
        result["categories"] = bundle["categories"]
        result["tags"] = bundle["tags"]
        result["media"] = bundle["media"]
        if bundle.get("cancelled"):
            result["cancelled"] = True
            result["errors"].append("Discovery stopped by user — partial list.")
        if result["posts"] or result["pages"]:
            result["api_available"] = True
        try:
            r = requests.get(f"{base_url}/wp-json/", headers=WP_HEADERS, timeout=TIMEOUT)
            if r.status_code == 200 and _http_response_looks_json(r):
                info = r.json()
                if isinstance(info, dict) and info.get("namespaces"):
                    result["site_info"] = {
                        "name":        info.get("name", ""),
                        "description": info.get("description", ""),
                        "url":         info.get("url", base_url),
                    }
        except Exception:
            pass

        if not result.get("cancelled") and progress_cb:
            progress_cb(f"Found {len(result['posts'])} posts via REST (WordPress, no AI)")

        if not result.get("cancelled") and not result["posts"]:
            if progress_cb:
                progress_cb("Trying sitemap & RSS for WordPress…")
            from ai_scraper import discover_posts_sitemap_rss

            fb = discover_posts_sitemap_rss(base_url, progress_cb=progress_cb)
            result["posts"] = fb.get("posts", [])
            if fb.get("pages"):
                result["pages"] = dedupe_posts_by_link(
                    (result.get("pages") or []) + fb["pages"]
                )
            if fb.get("categories") and not result["categories"]:
                result["categories"] = fb["categories"]
            if fb.get("site_info"):
                si = result.get("site_info") or {}
                result["site_info"] = {**si, **fb["site_info"]}
            result["errors"].extend(fb.get("errors", []))
            if not result["posts"]:
                result["errors"].append(
                    "No posts via WordPress REST, sitemap, or RSS. "
                    "Check that posts are public and endpoints are reachable."
                )

    elif grok_client:
        # ── AI crawl path (non-WordPress sites only) ──────────────────────────
        if progress_cb:
            progress_cb("🤖 Not WordPress — launching AI crawler…")

        from ai_scraper import crawl_site_with_ai

        ai_data = crawl_site_with_ai(
            base_url,
            grok_client,
            progress_cb=progress_cb,
            cancel_check=cancel_check,
            checkpoint_cb=checkpoint_cb,
        )
        result.update(ai_data)
        result["is_wordpress"] = result.get("is_wordpress", False)
        result["ai_assisted"] = True
        if ai_data.get("cancelled"):
            result["cancelled"] = True

        if progress_cb:
            progress_cb(f"🤖 AI crawler finished: {len(result['posts'])} posts found")

    else:
        # ── Basic HTML fallback (no WP, no AI) ────────────────────────────────
        if progress_cb:
            progress_cb("Basic HTML fallback (no REST API, no AI key)…")
        result["posts"] = _scrape_posts_html(base_url)

    # Platform fingerprint (CMS, React/Angular/…, static vs dynamic) — always last
    try:
        hr = requests.get(base_url, headers=HEADERS, timeout=TIMEOUT)
        if hr.status_code == 200:
            stack = infer_site_stack(hr.text, base_url, result, dict(hr.headers))
            si = result.setdefault("site_info", {})
            si["stack"] = stack
            si["stack_summary"] = format_site_stack_line(stack, si.get("site_type"))
    except Exception:
        pass

    return result


def _scrape_posts_html(base_url: str) -> list:
    posts = []
    try:
        r = requests.get(base_url, headers=HEADERS, timeout=TIMEOUT)
        soup = BeautifulSoup(r.text, "lxml")
        for art in soup.find_all("article"):
            title_el = art.find(["h1", "h2", "h3"])
            link_el  = art.find("a", href=True)
            excerpt_el = art.find(class_=lambda c: c and "excerpt" in c)
            date_el  = art.find("time")
            img_el   = art.find("img")
            link = link_el["href"] if link_el else ""
            slug = link.rstrip("/").split("/")[-1] if link else ""
            pid  = len(posts)
            posts.append({
                "id": None, "slug": slug,
                "title": title_el.get_text(strip=True) if title_el else "",
                "excerpt": excerpt_el.get_text(strip=True) if excerpt_el else "",
                "link": link,
                "date": date_el.get("datetime", "") if date_el else "",
                "status": "publish",
                "categories": [], "tags": [],
                "featured_image": abs_url(best_src(img_el), base_url) if img_el else None,
                "filename": safe_filename(pid, slug),
                "source": "html",
            })
    except Exception:
        pass
    return posts


# ─── Phase 2: Content parser (fallback when no AI) ───────────────────────────

_NOISE_SELECTORS = [
    "script", "style", "noscript",
    ".wp-block-rank-math-toc-block", ".wprm-jump-to-recipe-shortcode",
    ".sharedaddy", ".jp-relatedposts",
    "[class*='share-']", "[class*='social-share']",
    "[class*='newsletter']", "[class*='subscribe']",
    "[class*='related-post']", "[class*='related_post']",
    "[id*='newsletter']", "[id*='comments']",
    ".comment-respond", "#comments",
    "[class*='advertisement']", "[class*='ads-']",
]


class ContentParser:
    """Parse WordPress post HTML → structured blocks (used when AI is not available)."""

    def __init__(self, html: str, base_url: str):
        self.base = base_url
        self.soup = BeautifulSoup(html, "lxml")
        for sel in _NOISE_SELECTORS:
            try:
                for el in self.soup.select(sel):
                    el.decompose()
            except Exception:
                pass

    def parse(self) -> list:
        body = self.soup.find("body") or self.soup
        blocks = []
        for el in body.children:
            result = self._parse_el(el)
            if result is None:
                continue
            if isinstance(result, list):
                blocks.extend(b for b in result if b)
            else:
                blocks.append(result)
        return blocks

    def _parse_el(self, el):
        if isinstance(el, NavigableString):
            text = str(el).strip()
            return {"type": "paragraph", "text": text, "html": f"<p>{text}</p>"} if len(text) > 3 else None

        name = el.name
        if not name:
            return None
        classes = " ".join(el.get("class", []))

        if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            text = el.get_text(strip=True)
            return {"type": "heading", "level": int(name[1]), "text": text} if text else None

        if name == "p":
            img = el.find("img")
            if img and not el.get_text(strip=True):
                return self._parse_img(img)
            text = el.get_text(strip=True)
            if not text:
                return None
            return {"type": "paragraph", "text": text, "html": f"<p>{_clean_inline(el)}</p>"}

        if name in ("ul", "ol"):
            return self._parse_list(el)

        if name == "figure":
            img = el.find("img")
            if img:
                cap_el = el.find("figcaption")
                return {
                    "type": "image",
                    "src": abs_url(best_src(img), self.base),
                    "alt": img.get("alt", ""),
                    "caption": cap_el.get_text(strip=True) if cap_el else "",
                }
            return None

        if name == "img":
            return self._parse_img(el)

        if name == "blockquote":
            return {"type": "blockquote", "text": el.get_text(strip=True)}

        if name == "table":
            headers = [th.get_text(strip=True) for th in el.find_all("th")]
            rows = [[td.get_text(strip=True) for td in tr.find_all("td")]
                    for tr in el.find_all("tr") if tr.find_all("td")]
            return {"type": "table", "headers": headers, "rows": rows} if (headers or rows) else None

        if name == "iframe":
            return self._parse_iframe(el)

        if name == "div":
            return self._parse_div(el, classes)

        # Treat article, section, main, aside like div (recurse into children)
        if name in ("article", "section", "main", "aside", "span"):
            return self._parse_div(el, classes)

        return None

    def _parse_div(self, el, classes):
        if any(k in classes for k in ["wprm-recipe-container", "tasty-recipes",
                                       "mv-recipe-card", "recipe-card", "cooked-recipe"]):
            return self._parse_recipe(el)

        if "wp-block-image" in classes:
            img = el.find("img")
            if img:
                cap_el = el.find("figcaption")
                return {
                    "type": "image",
                    "src": abs_url(best_src(img), self.base),
                    "alt": img.get("alt", ""),
                    "caption": cap_el.get_text(strip=True) if cap_el else "",
                }

        if any(k in classes for k in ["wp-block-gallery", "gallery", "tiled-gallery"]):
            images = [{"src": abs_url(best_src(img), self.base), "alt": img.get("alt", "")}
                      for img in el.find_all("img")
                      if abs_url(best_src(img), self.base)]
            if images:
                return {"type": "gallery", "images": images}

        iframe = el.find("iframe")
        if iframe:
            return self._parse_iframe(iframe)

        blocks = []
        for child in el.children:
            result = self._parse_el(child)
            if result is None:
                continue
            if isinstance(result, list):
                blocks.extend(b for b in result if b)
            else:
                blocks.append(result)
        return blocks if blocks else None

    def _parse_img(self, img) -> dict | None:
        src = abs_url(best_src(img), self.base)
        if not src:
            return None
        return {"type": "image", "src": src, "alt": img.get("alt", ""), "caption": ""}

    def _parse_iframe(self, iframe) -> dict | None:
        src = iframe.get("src", "")
        if not src:
            return None
        provider = "youtube" if "youtube" in src else "vimeo" if "vimeo" in src else "embed"
        return {"type": "embed", "src": src, "provider": provider}

    def _parse_list(self, el) -> dict | None:
        items = [li.get_text(strip=True) for li in el.find_all("li", recursive=False)
                 if li.get_text(strip=True)]
        return {"type": "list", "ordered": el.name == "ol", "items": items} if items else None

    def _parse_recipe(self, el) -> dict:
        recipe = {"type": "recipe_card"}

        def txt(sel):
            node = el.select_one(sel)
            return node.get_text(strip=True) if node else ""

        recipe["name"]        = txt(".wprm-recipe-name") or txt(".tasty-recipes-title") or txt("[class*='recipe-name']")
        recipe["description"] = txt(".wprm-recipe-summary") or txt(".tasty-recipes-description")
        recipe["prep_time"]   = self._wprm_time(el, "prep")
        recipe["cook_time"]   = self._wprm_time(el, "cook")
        recipe["total_time"]  = self._wprm_time(el, "total")
        recipe["servings"]    = txt(".wprm-recipe-servings")
        img_el = el.select_one(".wprm-recipe-image img")
        recipe["image"] = abs_url(best_src(img_el), self.base) if img_el else None

        ingredients = []
        for li in el.select(".wprm-recipe-ingredient"):
            def part(sel):
                n = li.select_one(sel)
                return n.get_text(strip=True) if n else ""
            iname = part(".wprm-recipe-ingredient-name")
            if iname:
                ingredients.append({
                    "amount": part(".wprm-recipe-ingredient-amount"),
                    "unit":   part(".wprm-recipe-ingredient-unit"),
                    "name":   iname,
                    "notes":  part(".wprm-recipe-ingredient-notes"),
                })
            else:
                t = li.get_text(strip=True)
                if t:
                    ingredients.append({"amount": "", "unit": "", "name": t, "notes": ""})
        if not ingredients:
            for li in el.select(".tasty-recipes-ingredients li"):
                t = li.get_text(strip=True)
                if t:
                    ingredients.append({"amount": "", "unit": "", "name": t, "notes": ""})
        recipe["ingredients"] = ingredients

        instructions = []
        for i, li in enumerate(el.select(".wprm-recipe-instruction"), 1):
            text_el = li.select_one(".wprm-recipe-instruction-text")
            text    = text_el.get_text(strip=True) if text_el else li.get_text(strip=True)
            step: dict = {"step": i, "text": text}
            img_tag = li.find("img")
            if img_tag:
                step["image"] = abs_url(best_src(img_tag), self.base)
            if text:
                instructions.append(step)
        if not instructions:
            for i, li in enumerate(el.select(".tasty-recipes-instructions li"), 1):
                t = li.get_text(strip=True)
                if t:
                    instructions.append({"step": i, "text": t})
        recipe["instructions"] = instructions

        notes_el = el.select_one(".wprm-recipe-notes-container, [class*='recipe-notes']")
        recipe["notes"] = notes_el.get_text(strip=True) if notes_el else ""

        nutri = {}
        nutri_block = el.select_one(".wprm-recipe-nutrition")
        if nutri_block:
            nutri["summary"] = nutri_block.get_text(strip=True)
        recipe["nutrition"] = nutri

        return recipe

    def _wprm_time(self, el, kind: str) -> dict | None:
        h_el = el.select_one(f".wprm-recipe-{kind}_time-hours")
        m_el = el.select_one(f".wprm-recipe-{kind}_time-minutes")
        h = re.sub(r"\D", "", h_el.get_text()) if h_el else ""
        m = re.sub(r"\D", "", m_el.get_text()) if m_el else ""
        if not h and not m:
            return None
        parts = ([f"{h}h"] if h else []) + ([f"{m}m"] if m else [])
        return {"hours": h or "0", "minutes": m or "0", "display": " ".join(parts)}


def _clean_inline(el) -> str:
    allowed = {"a", "strong", "b", "em", "i", "span", "code", "br"}
    parts = []
    for child in el.children:
        if isinstance(child, NavigableString):
            parts.append(str(child))
        elif child.name in allowed:
            parts.append(str(child))
        else:
            parts.append(child.get_text())
    return "".join(parts)


# ─── HTML renderer (blocks → HTML) ───────────────────────────────────────────

_BLOCK_NOISE_RE = re.compile(
    r"(DISABLED\s*:|Ezoic|Ad Placement|ad placement|adsbygoogle|"
    r"Jump to Recipe|jump to recipe|wprm-jump|mediavine|"
    r"This post may contain affiliate|^\[[\w_-]+\]|"  # shortcodes like [wprm-recipe]
    r"^\s*<!--.*?-->\s*$)",  # bare HTML comments
    re.I,
)


def _is_noise_block(b: dict) -> bool:
    """Return True if a block contains only ad/plugin/shortcode garbage."""
    text = (b.get("text") or "").strip()
    html = (b.get("html") or "").strip()
    # Check plain text for noise patterns
    if text and _BLOCK_NOISE_RE.search(text):
        return True
    # Paragraph with only a shortcode in the html (no real content)
    if html and not text:
        import re as _r
        stripped = _r.sub(r"<[^>]+>", "", html).strip()
        if not stripped or _BLOCK_NOISE_RE.search(stripped):
            return True
    return False


def render_blocks_html(blocks: list) -> str:
    parts = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        t = b.get("type", "")
        if t == "paragraph":
            if _is_noise_block(b):
                continue
            parts.append(b.get("html") or f"<p>{b.get('text','')}</p>")
        elif t == "heading":
            lvl = min(max(b.get("level", 2), 2), 4)
            parts.append(f"<h{lvl}>{b.get('text','')}</h{lvl}>")
        elif t == "image":
            src, alt, cap = b.get("src",""), b.get("alt",""), b.get("caption","")
            if src:
                cap_html = f"<figcaption>{cap}</figcaption>" if cap else ""
                parts.append(f'<figure><img src="{src}" alt="{alt}" loading="lazy"/>{cap_html}</figure>')
        elif t == "gallery":
            imgs = "".join(
                f'<figure><img src="{i["src"]}" alt="{i.get("alt","")}" loading="lazy"/></figure>'
                for i in b.get("images", []) if i.get("src")
            )
            if imgs:
                parts.append(f'<div class="gallery">{imgs}</div>')
        elif t == "list":
            tag   = "ol" if b.get("ordered") else "ul"
            items = "".join(f"<li>{item}</li>" for item in b.get("items", []))
            if items:
                parts.append(f"<{tag}>{items}</{tag}>")
        elif t == "blockquote":
            parts.append(f"<blockquote><p>{b.get('text','')}</p></blockquote>")
        elif t == "table":
            headers, rows = b.get("headers", []), b.get("rows", [])
            thead = ("<thead><tr>"
                     + "".join(f"<th>{h}</th>" for h in headers)
                     + "</tr></thead>") if headers else ""
            tbody = ("<tbody>"
                     + "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>"
                                for row in rows)
                     + "</tbody>")
            parts.append(f"<table>{thead}{tbody}</table>")
        elif t == "embed":
            src = b.get("src", "")
            if src:
                parts.append(
                    f'<div class="embed-wrap">'
                    f'<iframe src="{src}" frameborder="0" allowfullscreen loading="lazy">'
                    f'</iframe></div>'
                )
        elif t == "recipe_card":
            parts.append(_render_recipe_html(b))
    return "\n".join(parts)


def _render_recipe_html(r: dict) -> str:
    name         = r.get("name", "")
    description  = r.get("description", "")
    servings     = r.get("servings", "")
    image        = r.get("image", "")
    notes        = r.get("notes", "")
    ingredients  = r.get("ingredients", [])
    instructions = r.get("instructions", [])
    nutrition    = r.get("nutrition", {})

    def time_block(kind, label):
        t = r.get(f"{kind}_time")
        if not t:
            return ""
        return (f'<div class="rc-time-item">'
                f'<span class="rc-time-label">{label}</span>'
                f'<span class="rc-time-value">{t.get("display","")}</span>'
                f'</div>')

    times_html = (time_block("prep", "Prep")
                  + time_block("cook", "Cook")
                  + time_block("total", "Total"))
    if servings:
        times_html += (f'<div class="rc-time-item">'
                       f'<span class="rc-time-label">Servings</span>'
                       f'<span class="rc-time-value">{servings}</span></div>')

    def ing_line(ing: dict) -> str:
        parts = []
        if ing.get("amount"): parts.append(f'<span class="rc-ing-amount">{ing["amount"]}</span>')
        if ing.get("unit"):   parts.append(f'<span class="rc-ing-unit">{ing["unit"]}</span>')
        parts.append(f'<span class="rc-ing-name">{ing.get("name","")}</span>')
        if ing.get("notes"):  parts.append(f'<span class="rc-ing-notes">{ing["notes"]}</span>')
        return "<li>" + " ".join(parts) + "</li>"

    def instr_line(ins: dict) -> str:
        img_html = (f'<img class="rc-step-img" src="{ins["image"]}" loading="lazy"/>'
                    if ins.get("image") else "")
        return (f'<li class="rc-step">'
                f'<span class="rc-step-num">{ins["step"]}</span>'
                f'<div class="rc-step-body">{img_html}<p>{ins["text"]}</p></div>'
                f'</li>')

    ings_html  = "".join(ing_line(i) for i in ingredients)
    instr_html = "".join(instr_line(i) for i in instructions)
    img_html   = f'<img class="rc-hero-img" src="{image}" alt="{name}" loading="lazy"/>' if image else ""
    notes_html = f'<div class="rc-notes"><h4>Notes</h4><p>{notes}</p></div>' if notes else ""
    nutri_text = nutrition.get("summary", "")
    nutri_html = f'<div class="rc-nutrition"><h4>Nutrition per serving</h4><p>{nutri_text}</p></div>' if nutri_text else ""

    return f"""<div class="recipe-card">
  {img_html}
  <div class="rc-head">
    <h2 class="rc-title">{name}</h2>
    {"<p class='rc-desc'>" + description + "</p>" if description else ""}
  </div>
  {"<div class='rc-times'>" + times_html + "</div>" if times_html else ""}
  {"<div class='rc-section'><h3>Ingredients</h3><ul class='rc-ingredients'>" + ings_html + "</ul></div>" if ings_html else ""}
  {"<div class='rc-section'><h3>Instructions</h3><ol class='rc-steps'>" + instr_html + "</ol></div>" if instr_html else ""}
  {notes_html}
  {nutri_html}
</div>"""


# ── "Chrome" CSS — always applied (our wrapper elements + recipe card) ─────────
_CHROME_CSS = """
    *,*::before,*::after{box-sizing:border-box}
    body{max-width:860px;margin:0 auto;padding:40px 20px}
    .site-name{font-size:.78rem;color:#aaa;margin-bottom:28px;text-transform:uppercase;letter-spacing:.6px}
    .site-name a{color:#aaa;text-decoration:none}
    .meta{color:#999;font-size:.88rem;margin-bottom:14px}
    .tags{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:24px}
    .tag{padding:3px 10px;border-radius:20px;font-size:.76rem;text-decoration:none}
    .tag.cat{background:#e8f0fe;color:#1a56db}
    .tag.tax{background:#f0f0f0;color:#555}
    .featured-img{width:100%;max-height:500px;object-fit:cover;border-radius:8px;margin-bottom:32px}
    .content{margin-top:8px}
    .content img{max-width:100%;height:auto}
    .gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:8px;margin:1em 0}
    .gallery figure{margin:0}
    .embed-wrap{position:relative;padding-bottom:56.25%;height:0;overflow:hidden;margin:1.5em 0;border-radius:8px}
    .embed-wrap iframe{position:absolute;top:0;left:0;width:100%;height:100%;border:none}
    .recipe-card{background:#fafafa;border:1px solid #e5e5e5;border-radius:12px;overflow:hidden;margin:2em 0;font-family:-apple-system,sans-serif}
    .rc-hero-img{width:100%;max-height:400px;object-fit:cover;display:block}
    .rc-head{padding:20px 24px 0}
    .rc-title{font-size:1.4rem;font-weight:800;color:#111;margin-bottom:6px}
    .rc-desc{font-size:.9rem;color:#555;line-height:1.6}
    .rc-times{display:flex;gap:0;padding:14px 24px;border-bottom:1px solid #e5e5e5;margin-top:14px;flex-wrap:wrap}
    .rc-time-item{flex:1;min-width:100px;padding:6px 12px;border-right:1px solid #e5e5e5}
    .rc-time-item:last-child{border-right:none}
    .rc-time-label{display:block;font-size:.72rem;color:#999;text-transform:uppercase;letter-spacing:.4px;font-weight:700;margin-bottom:2px}
    .rc-time-value{font-size:1rem;font-weight:700;color:#111}
    .rc-section{padding:18px 24px;border-bottom:1px solid #e5e5e5}
    .rc-section:last-child{border-bottom:none}
    .rc-section h3{font-size:1.05rem;font-weight:800;color:#111;margin-bottom:12px;text-transform:uppercase;letter-spacing:.4px}
    .rc-ingredients{list-style:none;padding:0;display:flex;flex-direction:column;gap:8px}
    .rc-ingredients li{display:flex;align-items:baseline;gap:5px;padding:8px 0;border-bottom:1px solid #f0f0f0;font-size:.93rem}
    .rc-ingredients li:last-child{border-bottom:none}
    .rc-ing-amount{font-weight:700;color:#111;min-width:28px}
    .rc-ing-unit{color:#555;min-width:36px}
    .rc-ing-name{color:#111}
    .rc-ing-notes{color:#888;font-size:.83rem;font-style:italic}
    .rc-steps{list-style:none;padding:0;display:flex;flex-direction:column;gap:16px}
    .rc-step{display:flex;gap:14px;align-items:flex-start}
    .rc-step-num{background:#111;color:#fff;border-radius:50%;width:26px;height:26px;display:flex;align-items:center;justify-content:center;font-size:.8rem;font-weight:800;flex-shrink:0;margin-top:2px}
    .rc-step-body p{margin:0;font-size:.93rem;color:#333;line-height:1.65}
    .rc-step-img{width:100%;max-width:300px;border-radius:6px;margin-top:8px}
    .rc-notes{padding:16px 24px;background:#fffbeb;border-top:1px solid #fde68a}
    .rc-notes h4{font-size:.85rem;font-weight:800;color:#92400e;margin-bottom:6px;text-transform:uppercase;letter-spacing:.3px}
    .rc-notes p{font-size:.88rem;color:#78350f;line-height:1.6}
    .rc-nutrition{padding:14px 24px;background:#f0fdf4;border-top:1px solid #bbf7d0}
    .rc-nutrition h4{font-size:.8rem;font-weight:800;color:#065f46;margin-bottom:4px;text-transform:uppercase;letter-spacing:.3px}
    .rc-nutrition p{font-size:.85rem;color:#047857}
    .footer{margin-top:56px;padding-top:16px;border-top:1px solid #eee;font-size:.8rem;color:#bbb}
    .footer a{color:#bbb}
    .ai-badge{display:inline-block;background:#f0fdf4;border:1px solid #bbf7d0;color:#065f46;
              padding:2px 8px;border-radius:10px;font-size:.72rem;font-weight:700;margin-left:8px;
              font-family:-apple-system,sans-serif}
"""

# ── Fallback typography — used only when no real site CSS is available ──────────
_FALLBACK_TYPOGRAPHY_CSS = """
    body{font-family:Georgia,serif;color:#222;background:#fff;line-height:1.75}
    h1{font-size:2rem;line-height:1.25;margin-bottom:12px;color:#111}
    .content p{margin-bottom:1.1em}
    .content h2{font-size:1.45rem;margin:1.8em 0 .6em;color:#111}
    .content h3{font-size:1.15rem;margin:1.5em 0 .5em;color:#222}
    .content h4{font-size:1rem;margin:1.2em 0 .4em;color:#333}
    .content a{color:#1a56db}
    .content ul,.content ol{margin:0 0 1em 1.5em}
    .content li{margin-bottom:.4em}
    .content figure{margin:1.5em 0}
    .content figure img{width:100%;border-radius:6px}
    .content figcaption{font-size:.82rem;color:#888;margin-top:6px;text-align:center}
    .content blockquote{border-left:3px solid #ddd;padding-left:18px;color:#666;margin:1em 0}
    .content table{width:100%;border-collapse:collapse;margin-bottom:1em;font-size:.9rem}
    .content th,.content td{padding:8px 12px;border:1px solid #eee;text-align:left}
    .content th{background:#f8f8f8;font-weight:700}
"""

# ── Legacy alias so any external code that references _POST_CSS still works ─────
_POST_CSS = _FALLBACK_TYPOGRAPHY_CSS + _CHROME_CSS


# ─── Site CSS extraction ──────────────────────────────────────────────────────

_CSS_NOISE_SEL_RE = re.compile(
    r'\b(?:nav(?:igation|-bar|-menu|-link|-item|-toggle|-collapse)?'
    r'|header(?:-wrap|-area|-inner|-top|-container)?'
    r'|footer(?:-wrap|-area|-inner|-bottom|-container)?'
    r'|sidebar|widget(?:-title|-area|-content|-wrap)?'
    r'|site-navigation|primary-menu|main-menu|mobile-menu|off-canvas|offcanvas'
    r'|breadcrumb(?:s|-nav|-wrap)?|page-numbers'
    r'|comment(?:s|-section|-list|-form|-meta|-author|-respond)?'
    r'|pagination(?:-wrap|-nav)?'
    r'|modal(?:-dialog|-content|-backdrop|-overlay)?'
    r'|popup|overlay'
    r'|toast|notification(?:-bar)?'
    r'|advertisement|ad-wrapper|ads-container|\.ad\b|#ad\b'
    r'|social(?:-share|-icons|-links|-media)?|sharing'
    r'|newsletter(?:-form|-signup)?|subscribe(?:-form|-box)?'
    r'|cookie(?:-notice|-banner|-bar|-popup)?'
    r'|ribbon|toolbar|topbar|admin-bar|wpadminbar'
    r'|related-posts?|author-bio|author-box|tag-cloud'
    r'|search-form|search-modal|search-overlay'
    r')\b',
    re.I,
)

_CSS_CONTENT_SEL_RE = re.compile(
    r'(?:\b(?:body|html|article|entry|post|content|main|prose|story|text|'
    r'read|recipe|ingredient|instruction|single|page-content)\b'
    r'|^:root|^\*$|^html$|^body$'
    r'|\b(?:p|ul|ol|li|dl|dt|dd|h[1-6]|img|picture|source|'
    r'figure|figcaption|blockquote|table|thead|tbody|tfoot|'
    r'tr|th|td|strong|em|b|i|s|u|mark|code|pre|kbd|samp|var|'
    r'a|span|time|cite|q|abbr)\b'
    r')',
    re.I,
)


def _iter_css_blocks(css: str):
    """Yield (preamble, body) pairs from CSS text. body=None for @statements."""
    i, n = 0, len(css)
    while i < n:
        while i < n and css[i] in ' \t\n\r':
            i += 1
        if i >= n:
            break
        first_brace = first_semi = None
        j = i
        while j < n:
            if css[j] == '{':
                first_brace = j
                break
            if css[j] == ';':
                first_semi = j
                break
            j += 1
        if first_brace is None and first_semi is None:
            break
        if first_semi is not None and (first_brace is None or first_semi < first_brace):
            yield (css[i:first_semi].strip(), None)
            i = first_semi + 1
            continue
        preamble = css[i:first_brace].strip()
        depth, j = 0, first_brace
        while j < n:
            if css[j] == '{':
                depth += 1
            elif css[j] == '}':
                depth -= 1
                if depth == 0:
                    break
            j += 1
        yield (preamble, css[first_brace:j + 1])
        i = j + 1


def _filter_css(css: str) -> str:
    """Keep only CSS rules relevant to article content rendering."""
    css = re.sub(r'/\*.*?\*/', ' ', css, flags=re.DOTALL)
    kept = []
    for preamble, body in _iter_css_blocks(css):
        if body is None:
            continue  # skip @import / @charset statements
        sel = preamble.lower().strip()
        # Always keep @font-face and @keyframes
        if sel.startswith('@font-face') or sel.startswith('@keyframes'):
            kept.append(preamble + ' ' + body)
            continue
        # Always keep :root, *, html, body
        if re.match(r'^:root\b|^\*|^html\b|^body\b', sel):
            kept.append(preamble + ' ' + body)
            continue
        # @media / @supports — recursively filter their contents
        if sel.startswith('@media') or sel.startswith('@supports'):
            inner = _filter_css(body[1:-1])
            if inner.strip():
                kept.append(preamble + ' {\n' + inner + '\n}')
            continue
        # Regular selector — check for noise
        if _CSS_NOISE_SEL_RE.search(preamble):
            continue
        if _CSS_CONTENT_SEL_RE.search(preamble):
            kept.append(preamble + ' ' + body)
            continue
        # Keep simple element-only selectors (e.g. "p, h2, strong")
        if re.match(r'^[a-z][a-z0-9-]*(?:\s*[,>+~]\s*[a-z][a-z0-9-]*)*$',
                    preamble.strip(), re.I):
            kept.append(preamble + ' ' + body)
    return '\n'.join(kept)


def _abs_urls_in_css(css: str, base_url: str) -> str:
    """Convert relative url(...) values in CSS to absolute URLs."""
    def _fix(m):
        raw = m.group(1).strip().strip('"\'')
        if not raw or raw.startswith(('http', 'data:', '//', '#')):
            return m.group(0)
        return "url('" + urljoin(base_url, raw) + "')"
    return re.sub(r'url\(\s*([^)]+)\s*\)', _fix, css)


def fetch_and_filter_site_css(html: str, page_url: str,
                               max_raw: int = 500_000,
                               max_out: int = 120_000) -> str:
    """
    Collect all CSS from a page (linked stylesheets + inline <style> tags),
    filter to article-relevant rules, and return a CSS string ready to inline.
    """
    soup = BeautifulSoup(html, "lxml")
    parts: list[str] = []
    total_raw = 0

    # 1. Linked stylesheets
    for link in soup.find_all("link"):
        rels = link.get("rel") or []
        if isinstance(rels, str):
            rels = [rels]
        if "stylesheet" not in [r.lower() for r in rels]:
            continue
        href = (link.get("href") or "").strip()
        if not href or href.startswith("data:"):
            continue
        abs_href = urljoin(page_url, href)
        if total_raw >= max_raw:
            break
        try:
            r = requests.get(abs_href, headers=HEADERS, timeout=10)
            if r.status_code == 200:
                text = _abs_urls_in_css(r.text, abs_href)
                parts.append(text)
                total_raw += len(text)
        except Exception:
            pass

    # 2. Inline <style> blocks
    for tag in soup.find_all("style"):
        text = tag.get_text()
        parts.append(_abs_urls_in_css(text, page_url))
        total_raw += len(text)

    raw = "\n\n".join(parts)[:max_raw]
    return _filter_css(raw)[:max_out]


# ─── Phase 2: Deep scrape ─────────────────────────────────────────────────────

def deep_scrape_post(post: dict, base_url: str, api_available: bool,
                     site_info: dict, grok_client=None,
                     site_css: str = "", cached_html: str = "") -> dict:
    """
    Fetch and extract full content for one post.
    Returns {"html": str, "structured": dict, "site_css": str}

    When cached_html is provided (original full-page HTML with inlined CSS),
    it is cleaned and used as the HTML output directly — preserving the site's
    exact CSS selectors, Tailwind classes, Astro scoped attrs, etc.

    Priority:
      1. AI (Grok)         → best structured output for ANY site
      2. WP REST API       → get raw HTML, then ContentParser
      3. URL fetch         → get raw HTML, then ContentParser
    """
    raw_html = ""

    # ── Try to get raw HTML ────────────────────────────────────────────────────

    if api_available and post.get("id") and post.get("source") != "ai":
        try:
            r = requests.get(
                f"{base_url}/wp-json/wp/v2/posts/{post['id']}",
                headers=HEADERS,
                params={"_fields": "id,content"},
                timeout=TIMEOUT,
            )
            if r.status_code == 200:
                raw_html = r.json().get("content", {}).get("rendered", "")
        except Exception:
            pass

    if not raw_html and post.get("link"):
        try:
            r = requests.get(post["link"], headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            raw_html = r.text
        except Exception:
            pass

    _page_url = post.get("link", base_url)

    # ── Build clean HTML from cached original page (best visual fidelity) ──────
    # When the original full-page HTML is available (downloaded separately),
    # use extract_article_from_cached_html so that original CSS selectors,
    # Tailwind utility classes, Astro scoped attrs, etc. all still apply.
    _clean_html_for_output: str = ""
    _source_html_for_css = cached_html or raw_html
    if cached_html:
        try:
            _clean_html_for_output = extract_article_from_cached_html(
                cached_html, _page_url
            )
        except Exception:
            _clean_html_for_output = ""

    # ── Extract site CSS from the full page HTML (only when not cached yet) ─────
    if _source_html_for_css and not site_css:
        try:
            site_css = fetch_and_filter_site_css(_source_html_for_css, _page_url)
        except Exception:
            site_css = ""

    # ── JSON-LD structured data (works on JS-rendered sites) ──────────────────
    jsonld_result = None
    if raw_html:
        jsonld_result = _extract_jsonld(raw_html, post.get("link", base_url))
        if jsonld_result:
            # Enrich post metadata from JSON-LD
            if jsonld_result.get("title") and not post.get("title"):
                post["title"] = jsonld_result["title"]
            if jsonld_result.get("featured_image") and not post.get("featured_image"):
                post["featured_image"] = jsonld_result["featured_image"]
            if jsonld_result.get("date") and not post.get("date"):
                post["date"] = jsonld_result["date"]

            # Merge JSON-LD recipe data with full article body text
            body_blocks = _extract_article_body(raw_html, post.get("link", base_url))
            recipe_blocks = [b for b in jsonld_result["blocks"] if b.get("type") == "recipe_card"]
            recipe_desc = recipe_blocks[0].get("description", "") if recipe_blocks else ""
            recipe_name = recipe_blocks[0].get("name", "") if recipe_blocks else ""
            text_blocks = []
            for b in body_blocks:
                # Skip recipe_card blocks from body (JSON-LD version is better)
                if b.get("type") == "recipe_card":
                    continue
                # Skip paragraphs that duplicate the recipe description
                if (b.get("type") == "paragraph" and recipe_desc
                        and b.get("text", "").strip() == recipe_desc.strip()):
                    continue
                # Skip headings that are just the recipe name (duplicate of title)
                if (b.get("type") == "heading" and recipe_name
                        and b.get("text", "").strip().lower() == recipe_name.strip().lower()):
                    continue
                text_blocks.append(b)
            blocks = (text_blocks + recipe_blocks) if text_blocks else jsonld_result["blocks"]

            structured = {
                "meta": {
                    "id":             post.get("id"),
                    "title":          jsonld_result.get("title") or post.get("title", ""),
                    "slug":           post.get("slug", ""),
                    "date":           jsonld_result.get("date") or post.get("date", ""),
                    "author":         jsonld_result.get("author", ""),
                    "url":            post.get("link", ""),
                    "categories":     post.get("categories", []),
                    "tags":           post.get("tags", []),
                    "featured_image": post.get("featured_image"),
                    "excerpt":        jsonld_result.get("description") or post.get("excerpt", ""),
                    "ai_extracted":   False,
                    "jsonld_extracted": True,
                    "extracted_by":   "jsonld+html",
                },
                "blocks": blocks,
            }
            html = (_clean_html_for_output or
                    _build_post_html(post, blocks, base_url, site_info,
                                     ai_used=False, site_css=site_css))
            return {"html": html, "structured": structured, "site_css": site_css}

    # ── AI extraction (only if page has real content, not a JS shell) ──────────

    def _has_real_content(html: str) -> bool:
        """Return True if the HTML has enough text to be worth sending to AI."""
        if not html:
            return False
        soup = BeautifulSoup(html, "lxml")
        for tag in soup.find_all(["script", "style", "nav", "header", "footer"]):
            tag.decompose()
        text = soup.get_text(" ", strip=True)
        return len(text) > 500  # less than 500 chars = JS shell

    if grok_client and raw_html and _has_real_content(raw_html):
        try:
            ai_result = grok_client.extract_content(raw_html, post.get("link", base_url))

            if ai_result and ai_result.get("blocks"):
                blocks = ai_result.get("blocks", [])

                # Enrich post metadata with AI findings
                if ai_result.get("featured_image") and not post.get("featured_image"):
                    post["featured_image"] = ai_result["featured_image"]
                if ai_result.get("categories") and not post.get("categories"):
                    post["categories"] = [
                        {"id": c.get("name","").lower(), "name": c.get("name",""),
                         "slug": c.get("name","").lower(), "link": c.get("url","")}
                        for c in ai_result["categories"]
                    ]

                structured = {
                    "meta": {
                        "id":             post.get("id"),
                        "title":          ai_result.get("title") or post.get("title", ""),
                        "slug":           post.get("slug", ""),
                        "date":           ai_result.get("date") or post.get("date", ""),
                        "author":         ai_result.get("author", ""),
                        "url":            post.get("link", ""),
                        "categories":     post.get("categories", []),
                        "tags":           post.get("tags", []),
                        "featured_image": post.get("featured_image"),
                        "excerpt":        ai_result.get("excerpt") or post.get("excerpt", ""),
                        "ai_extracted":   True,
                    },
                    "blocks": blocks,
                }

                html = (_clean_html_for_output or
                        _build_post_html(post, blocks, base_url, site_info,
                                         ai_used=True, site_css=site_css))
                return {"html": html, "structured": structured, "site_css": site_css}
        except Exception:
            pass  # Fall through to ContentParser

    # ── Fallback: ContentParser ────────────────────────────────────────────────

    if raw_html:
        # For URL-fetched HTML, try to isolate the content area first
        if not api_available or post.get("source") == "ai":
            raw_html = _extract_content_area(raw_html, post.get("link", base_url))

        blocks = ContentParser(raw_html, base_url).parse()
    else:
        blocks = []

    structured = {
        "meta": {
            "id":             post.get("id"),
            "title":          post.get("title", ""),
            "slug":           post.get("slug", ""),
            "date":           post.get("date", ""),
            "author":         "",
            "url":            post.get("link", ""),
            "categories":     post.get("categories", []),
            "tags":           post.get("tags", []),
            "featured_image": post.get("featured_image"),
            "excerpt":        post.get("excerpt", ""),
            "ai_extracted":   False,
        },
        "blocks": blocks,
    }
    html = (_clean_html_for_output or
            _build_post_html(post, blocks, base_url, site_info,
                             ai_used=False, site_css=site_css))
    return {"html": html, "structured": structured, "site_css": site_css}


# ─── Clean-article extractor (uses original HTML for pixel-perfect look) ─────

_CONTENT_SELECTORS = [
    "main", "article",
    ".entry-content", ".post-content", ".article-body", ".article-content",
    ".post-body", ".content-area", ".site-content", ".page-content",
    '[class*="entry-content"]', '[class*="post-content"]',
    "#content", "#main", "#primary",
]

_INNER_NOISE_RE = re.compile(
    r"(ezoic|adsbygoogle|ad[-_]slot|ads?[-_]|ad-placeholder|"
    r"social[-_]share|share[-_]btn|share[-_]bar|pinterest-share|"
    r"newsletter|subscribe[-_]|cookie[-_]|"
    r"related[-_]post|related[-_]recipe|related[-_]article|"
    r"author[-_]bio|author[-_]box|author[-_]card|author[-_]info|"
    r"comment[-_]|respond|pingback|trackback|"
    r"pagination|page[-_]links|nav[-_]links|"
    r"\bsidebar\b|side[-_]bar|side[-_]panel|side[-_]col|"
    r"recipe[-_]sidebar|post[-_]sidebar|widget[-_]area|"
    r"\brecent[-_]posts?\b|\bpopular[-_]posts?\b|"
    r"tag[-_]cloud|categories[-_]list)",
    re.I,
)

# Grid column patterns that signal a sidebar (narrower column beside the article)
_SIDEBAR_COL_RE = re.compile(
    r"\b(col[-_]span[-_]1|col[-_]sm[-_][1-4]|col[-_]md[-_][1-4]|"
    r"lg:col[-_]span[-_]1|xl:col[-_]span[-_]1|"
    r"sidebar|side[-_]col|aside[-_]col)\b",
    re.I,
)

# Article / main column patterns (the wide column we want to keep)
_ARTICLE_COL_RE = re.compile(
    r"\b(col[-_]span[-_][2-9]|col[-_]md[-_][5-9]|col[-_]md[-_]1[0-2]|"
    r"lg:col[-_]span[-_][2-9]|xl:col[-_]span[-_][2-9]|"
    r"article|content|entry|post[-_]body|main[-_]col)\b",
    re.I,
)


def _strip_sidebar_columns(container) -> None:
    """
    In grid/flex containers, remove columns that look like sidebars.
    Keeps only the widest/article column.
    """
    # Look for direct grid children where one is a sidebar column
    for grid in container.find_all(True):
        if not hasattr(grid, "attrs") or grid.attrs is None:
            continue
        cls = " ".join(grid.get("class") or [])
        # Only look inside grid/flex containers
        if not re.search(r"\bgrid\b|\bflex\b|\brow\b|\bcolumns\b", cls, re.I):
            continue
        children = [c for c in grid.children
                    if hasattr(c, "name") and c.name and c.name not in ("script", "style")]
        if len(children) < 2:
            continue
        # Find sidebar children and remove them
        for child in list(children):
            child_cls = " ".join(child.get("class") or [])
            if _SIDEBAR_COL_RE.search(child_cls) and not _ARTICLE_COL_RE.search(child_cls):
                child.decompose()


def extract_article_from_cached_html(cached_html: str, page_url: str) -> str:
    """
    Build a clean, standalone article HTML from the original cached page.

    Strategy:
      1. Keep the original <head> intact (CSS is already inlined by download-html).
      2. Find the main content element.
      3. Strip sidebars (grid columns), ads, social, comments, related posts.
      4. Return a new HTML document — CSS selectors still match because the
         original class names / data-* attrs are preserved.
    """
    soup = BeautifulSoup(cached_html, "lxml")

    # ── 1. Remove whole noisy top-level tags ──────────────────────────────────
    for tag in soup.find_all(["nav", "header", "footer", "aside",
                               "script", "noscript", "iframe",
                               "template", "dialog"]):
        tag.decompose()

    # ── 2. Find the main content container ────────────────────────────────────
    content_el = None
    for sel in _CONTENT_SELECTORS:
        try:
            candidate = soup.select_one(sel)
        except Exception:
            continue
        if candidate and len(candidate.get_text(strip=True)) > 200:
            content_el = candidate
            break

    if not content_el:
        content_el = soup.find("body")

    # ── 3. Remove sidebar columns from grid/flex layouts ─────────────────────
    if content_el:
        _strip_sidebar_columns(content_el)

    # ── 4. Remove inner noise by class/id AND by text content ────────────────
    _TEXT_NOISE_RE = re.compile(
        r"^(by\s*\w+|published|affiliate\s+link|jump\s+to\s+recipe|"
        r"modern\s*breadcrumb|ezoic|ad\s+placement|disabled:|minutes?\s*$|"
        r"this\s+post\s+may\s+contain)",
        re.I,
    )
    if content_el:
        for el in list(content_el.find_all(True)):
            if not hasattr(el, "attrs") or el.attrs is None:
                continue
            cls = " ".join(el.get("class") or [])
            eid = el.get("id") or ""
            if _INNER_NOISE_RE.search(cls) or _INNER_NOISE_RE.search(eid):
                el.decompose()
                continue
            # Remove paragraphs that are author/date/ad text (class-less noise)
            if el.name == "p":
                txt = el.get_text(strip=True)
                if len(txt) < 5 or _TEXT_NOISE_RE.search(txt):
                    el.decompose()

    content_html = str(content_el) if content_el else "<p>No content extracted.</p>"

    # ── 4. Build head — keep original CSS; ensure <base> for URLs ─────────────
    head_el = soup.find("head")
    if head_el:
        # Remove the existing <base> if any (we'll add a fresh one)
        for b in head_el.find_all("base"):
            b.decompose()
        # Prepend <base> so all relative URLs resolve correctly
        base_tag = soup.new_tag("base", href=page_url)
        head_el.insert(0, base_tag)
        head_html = str(head_el)
    else:
        head_html = (
            f'<head>\n  <base href="{page_url}"/>\n'
            f'  <meta charset="UTF-8"/>\n'
            f'  <meta name="viewport" content="width=device-width,initial-scale=1.0"/>\n'
            f'</head>'
        )

    # ── 5. Minimal body wrapper so the page isn't completely naked ─────────────
    # Keep original body classes so any utility-CSS (Tailwind etc.) still applies
    body_el = soup.find("body")
    body_classes = " ".join(body_el.get("class") or []) if body_el else ""
    body_class_attr = f' class="{body_classes}"' if body_classes else ""

    return (
        f"<!DOCTYPE html>\n<html lang=\"en\">\n"
        f"{head_html}\n"
        f"<body{body_class_attr}>\n"
        f"{content_html}\n"
        f"</body>\n</html>"
    )


def build_redesigned_html(cached_html: str, page_url: str, design_css: str) -> str:
    """
    Build a standalone article HTML that uses a completely new CSS design.

    1. Extract clean article content (sidebar/noise stripped) via
       extract_article_from_cached_html.
    2. Strip every <link rel="stylesheet"> and <style> from the <head>
       so the original site's CSS is gone.
    3. Inject the new design_css as a single <style> block.
    4. Return the resulting HTML.
    """
    clean = extract_article_from_cached_html(cached_html, page_url)
    soup = BeautifulSoup(clean, "lxml")

    head = soup.find("head")
    if head:
        # Remove original stylesheet links and inline styles
        for tag in head.find_all(["link", "style"]):
            if tag.name == "link":
                rel = " ".join(tag.get("rel") or [])
                if "stylesheet" in rel.lower():
                    tag.decompose()
            else:
                tag.decompose()
        # Inject the new design CSS
        new_style = soup.new_tag("style")
        new_style.string = design_css
        head.append(new_style)
    else:
        # Build a minimal head if none exists
        head_html = (
            f'<head>\n  <base href="{page_url}"/>\n'
            f'  <meta charset="UTF-8"/>\n'
            f'  <meta name="viewport" content="width=device-width,initial-scale=1.0"/>\n'
            f'  <style>{design_css}</style>\n'
            f'</head>'
        )
        clean = clean.replace("<html", f"<html", 1)  # preserve; rebuild below

    # Strip original body classes (Tailwind utilities etc.) — new CSS handles layout
    body = soup.find("body")
    if body:
        body.attrs.pop("class", None)

    return str(soup)


# ─── Reskin HTML (class renaming + text replacement + design variation) ────────

def _rename_classes_in_html(soup, class_map: dict) -> None:
    """Replace class names in every element's class list using class_map."""
    for el in soup.find_all(True):
        if not hasattr(el, "attrs"):
            continue
        classes = el.get("class")
        if classes:
            el["class"] = [class_map.get(c, c) for c in classes]
        eid = el.get("id")
        if eid and eid in class_map:
            el["id"] = class_map[eid]


def _rename_classes_in_css(css: str, class_map: dict) -> str:
    """Replace class selectors (.old-name) and id selectors (#old-id) in CSS."""
    # Replace .classname → .newname
    for old, new in class_map.items():
        css = re.sub(
            r'(?<![a-zA-Z0-9_-])\.' + re.escape(old) + r'(?![a-zA-Z0-9_-])',
            '.' + new,
            css,
        )
        css = re.sub(
            r'(?<![a-zA-Z0-9_-])#' + re.escape(old) + r'(?![a-zA-Z0-9_-])',
            '#' + new,
            css,
        )
    return css


def _collect_custom_classes(soup) -> set:
    """Collect all class names that appear on elements in the page."""
    classes = set()
    for el in soup.find_all(True):
        for c in (el.get("class") or []):
            # Skip Tailwind utilities (contain ':' or are common utility patterns)
            if ':' in c:
                continue
            # Keep multi-word compound names (likely custom, not Tailwind utilities)
            if '-' in c or '_' in c or (len(c) > 3 and not re.match(
                r'^(m|p|w|h|text|font|bg|border|flex|grid|gap|col|row|'
                r'items|justify|self|space|min|max|overflow|z|top|bottom|'
                r'left|right|block|inline|hidden|absolute|relative|fixed|'
                r'sticky|static|rounded|shadow|opacity|cursor|select|'
                r'pointer|transition|duration|ease|delay|transform|'
                r'scale|rotate|translate|skew|origin|sr|not|ring|'
                r'divide|place|content|object|aspect|truncate|whitespace|'
                r'break|underline|italic|uppercase|lowercase|capitalize|'
                r'leading|tracking|list|decoration|accent|fill|stroke|'
                r'outline|appearance|resize|scroll|snap|touch|will|'
                r'container|prose|group|peer)\b', c)):
                classes.add(c)
    return classes


_COLOR_VARIANTS = [
    # (old_color_hex_or_keyword, new_color) — CSS value substitution pairs
    # We pick a variant based on seed and swap accent colors
    {"#ff6b35": "#e05a2b", "#ff8c00": "#d4780a", "accent": "accent2"},  # warm orange → rust
    {"#2563eb": "#1d4ed8", "#3b82f6": "#2563eb", "blue": "indigo"},     # blue → indigo
    {"#16a34a": "#15803d", "#22c55e": "#16a34a", "green": "emerald"},   # green → emerald
    {"#dc2626": "#b91c1c", "#ef4444": "#dc2626", "red": "rose"},        # red → rose
]

_DESIGN_TWEAKS = [
    "body { letter-spacing: 0.01em; }",
    "body { line-height: 1.8; }",
    "h1, h2, h3 { letter-spacing: -0.02em; }",
    "h1, h2, h3 { font-weight: 700; }",
    "p { margin-bottom: 1.3em; }",
    "a { text-decoration-thickness: 2px; }",
]


def reskin_article_html(
    cached_html: str,
    page_url: str,
    reskinned_blocks: list,
    new_title: str,
    variant_seed: int = 0,
) -> str:
    """
    Build a reskinned article HTML from the original cached page:
      1. Extract clean article HTML (sidebar/noise stripped).
      2. Rename custom CSS classes with a seed-based suffix.
      3. Update CSS selectors to match renamed classes.
      4. Replace visible text (headings + paragraphs) with Groq-rewritten blocks.
      5. Add slight design variations.
    """
    # ── Step 1: Get clean base HTML ───────────────────────────────────────────
    clean_html = extract_article_from_cached_html(cached_html, page_url)
    soup = BeautifulSoup(clean_html, "lxml")

    # ── Step 2: Build class rename map ────────────────────────────────────────
    suffix = format(abs(variant_seed) % 0xFFFF, "04x")
    custom_classes = _collect_custom_classes(soup)

    # Also collect IDs
    custom_ids = set()
    for el in soup.find_all(True):
        eid = el.get("id")
        if eid and eid.strip():
            custom_ids.add(eid)

    class_map = {}
    for c in custom_classes:
        class_map[c] = f"{c}-{suffix}"
    for eid in custom_ids:
        class_map[eid] = f"{eid}-{suffix}"

    # ── Step 3: Rename classes in HTML elements ───────────────────────────────
    _rename_classes_in_html(soup, class_map)

    # ── Step 4: Rename classes in <style> blocks ──────────────────────────────
    for style_tag in soup.find_all("style"):
        style_tag.string = _rename_classes_in_css(style_tag.get_text(), class_map)

    # ── Step 5: Replace visible text using two-pointer document-order walk ───────
    # Build flat ordered list of Groq text blocks (skip images/recipe_cards)
    groq_text_blocks = [
        b for b in reskinned_blocks
        if b.get("type") in ("heading", "paragraph", "list")
    ]

    body_el = soup.find("body")
    if body_el:
        # First: replace <h1> with the new title unconditionally
        h1 = body_el.find("h1")
        if h1:
            h1.clear()
            h1.append(NavigableString(new_title))

        # Collect all meaningful text elements in document order.
        # Exclude <h1> (already handled), nested <li> (handled via parent ul/ol),
        # and short/noise paragraphs.
        text_elements = []
        seen_ul_ol = set()
        for el in body_el.find_all(["h2", "h3", "h4", "h5", "h6", "p", "ul", "ol"]):
            if el.name == "p":
                txt = el.get_text(strip=True)
                if len(txt) < 5:
                    continue  # truly empty — skip (noise already removed by extract_article_from_cached_html)
            if el.name in ("ul", "ol"):
                # Avoid double-processing nested lists
                if id(el) in seen_ul_ol:
                    continue
                # Mark all descendant ul/ol so they don't get processed again
                for nested in el.find_all(["ul", "ol"]):
                    seen_ul_ol.add(id(nested))
                seen_ul_ol.add(id(el))
            text_elements.append(el)

        # Independent per-type indices — the Nth heading in the HTML gets the
        # Nth Groq heading, the Mth paragraph gets the Mth Groq paragraph, etc.
        # This prevents cascading drift: one missing heading no longer shifts
        # all subsequent paragraphs.
        # Exclude h1 blocks from the heading pool — h1 is always set to new_title separately
        groq_headings  = [b for b in groq_text_blocks if b.get("type") == "heading" and b.get("level", 2) != 1]
        groq_paragraphs = [b for b in groq_text_blocks if b.get("type") == "paragraph"]
        groq_lists     = [b for b in groq_text_blocks if b.get("type") == "list"]
        h_idx = p_idx = l_idx = 0

        for el in text_elements:
            if el.name in ("h2", "h3", "h4", "h5", "h6"):
                if h_idx < len(groq_headings):
                    txt = (groq_headings[h_idx].get("text") or "").strip()
                    if txt:
                        el.clear()
                        el.append(NavigableString(txt))
                    h_idx += 1

            elif el.name == "p":
                if p_idx < len(groq_paragraphs):
                    txt = (groq_paragraphs[p_idx].get("text") or "").strip()
                    if txt:
                        el.clear()
                        el.append(NavigableString(txt))
                    p_idx += 1

            elif el.name in ("ul", "ol"):
                if l_idx < len(groq_lists):
                    items_el = el.find_all("li", recursive=False) or el.find_all("li")
                    new_items = [str(it) for it in (groq_lists[l_idx].get("items") or [])]
                    for i, li in enumerate(items_el):
                        if i < len(new_items):
                            li.clear()
                            li.append(NavigableString(new_items[i]))
                    l_idx += 1

    # ── Step 6: Inject design variation CSS ───────────────────────────────────
    design_tweak = _DESIGN_TWEAKS[variant_seed % len(_DESIGN_TWEAKS)]
    variation_css = (
        f"\n/* === Reskin design variation #{variant_seed % len(_DESIGN_TWEAKS) + 1} === */\n"
        f"{design_tweak}\n"
        # Constrain h1 to article column width — prevents full-viewport-width titles
        # that break when imported into a theme layout
        "h1 { max-width: 100%; width: auto; box-sizing: border-box; }\n"
    )

    head_el = soup.find("head")
    if head_el:
        var_style = soup.new_tag("style")
        var_style.string = variation_css
        head_el.append(var_style)

    return str(soup)


def _extract_article_body(html: str, url: str) -> list:
    """
    Find the main article/content area in raw HTML and parse it into blocks.
    Tries multiple CSS selectors in priority order.
    """
    try:
        soup = BeautifulSoup(html, "lxml")
        # Remove noise
        for tag in soup.find_all(["script", "style", "nav", "header", "footer",
                                   "aside", "iframe"]):
            tag.decompose()

        # Priority selectors for content area
        # Drupal-specific selectors first (field--name-body), then WordPress/generic
        content_selectors = [
            ".field--name-body .field__item",   # Drupal: body field inner
            ".field--name-body",                 # Drupal: body field wrapper
            "[class*='field-name-body']",        # Drupal: older body field
            "article.node-recipe .node__content",# Drupal: recipe node content
            "article.node .node__content",       # Drupal: generic node content
            "article.node-recipe", "article.node", "article.post",
            "[class*='recipe-content']", "[class*='article-body']",
            "[class*='entry-content']", "[class*='post-content']",
            "article", "main article", ".content", "main",
        ]
        content_el = None
        for sel in content_selectors:
            el = soup.select_one(sel)
            if el and len(el.get_text(strip=True)) > 300:
                content_el = el
                break

        if not content_el:
            return []

        # Remove recipe card containers (handled by JSON-LD)
        _decompose_classes = [
            "recipe-card", "wprm-recipe", "tasty-recipe",
            "mv-recipe", "recipe-summary",
            # Drupal-specific recipe fields
            "field-name-field-ingredients", "field-name-field-directions",
            "field--name-field-ingredients", "field--name-field-directions",
            "field--name-field-recipe", "recipe-card-wrapper",
            # Common noise
            "comments", "comment-form", "social-share", "share-buttons",
            "related-posts", "related-recipes", "newsletter",
            "breadcrumb", "node-stats", "recipe-nutrition-label",
        ]
        for tag in content_el.find_all(class_=lambda c: c and any(
            x in " ".join(c) for x in _decompose_classes
        )):
            tag.decompose()

        # Also remove specific noise selectors
        for sel in [".recipe-card", "section.recipe", "[class*='recipe-nutri']",
                    "[class*='recipe-controls']", "[class*='recipe-single-meta']",
                    "[class*='comment']", ".breadcrumb",
                    "[class*='social']", "[class*='share-']",
                    ".item-carousel", ".feed-item",
                    "[class*='recipe-item']",
                    # Author bio, sidebar, tags, newsletter
                    "[class*='author-bio']", "[class*='about-author']",
                    "[class*='author-info']", "[class*='user-profile']",
                    ".region-sidebar-second", "[class*='sidebar']",
                    "[class*='field-name-field-tags']", "[class*='field--name-field-tags']",
                    "[class*='field-name-field-category']", "[class*='node-links']",
                    "[class*='field-name-field-dietary']",
                    "[class*='meal-plan']", "[class*='newsletter']",
                    "[class*='snap-picture']", "[class*='did-you-make']"]:
            try:
                for el_noise in content_el.select(sel):
                    el_noise.decompose()
            except Exception:
                pass

        # Fix relative image URLs
        for img in content_el.find_all("img"):
            src = img.get("src") or img.get("data-src") or img.get("data-lazy-src") or ""
            if src:
                img["src"] = abs_url(src, url)

        raw_blocks = ContentParser(str(content_el), url).parse()

        # Post-parse: filter out noise paragraphs (nutrient stats, UI text, etc.)
        _noise_patterns = re.compile(
            r"^(\d+g?\s*(CAL|CARBS?|FAT|PROTEIN|FIBER|SUGAR|SODIUM|CHOL)S?|"
            r"\d+\s*Comments?|Featured in:?|Loading Video.*|Leave a comment|"
            r"PREP TIME:.*|COOK TIME:.*|TOTAL TIME:.*|METRICS|"
            r"Print|Download|Send to your inbox|The Recipe|"
            r"Jump to Recipe.*|Pin|Share|Tweet)$",
            re.IGNORECASE
        )
        filtered = []
        for b in raw_blocks:
            if b.get("type") == "paragraph":
                text = b.get("text", "").strip()
                if len(text) < 5:  # too short to be real content
                    continue
                if _noise_patterns.match(text):
                    continue
            if b.get("type") == "list":
                items = b.get("items", [])
                # Remove lists that are just Print/Download/Share
                if len(items) <= 3 and all(
                    re.match(r"^(Print|Download|Share|Pin|Tweet|Send)", it, re.I)
                    for it in items
                ):
                    continue
            filtered.append(b)
        return filtered
    except Exception:
        return []


_PIN_HREF_RE = re.compile(
    r"https?://(?:[\w-]+\.)?pinterest\.[\w.]+/pin/([^/?\s\"\'<>]+)/?",
    re.I,
)


def is_actionable_pinterest_pin_url(url: str) -> bool:
    """
    False for bare https://www.pinterest.com/pin/create/ (no query) — not a real saved pin.
    True for /pin/<id>/ and for /pin/create?... with parameters (save widget).
    """
    u = (url or "").strip()
    if not u:
        return False
    if "pinterest." not in u.lower():
        return True
    try:
        pu = urlparse(u)
        p = (pu.path or "").replace("//", "/")
        while len(p) > 1 and p.endswith("/"):
            p = p[:-1]
        if p.lower() == "/pin/create":
            return bool((pu.query or "").strip())
        return True
    except Exception:
        return True


def _decode_json_string_fragment(raw: str) -> str:
    if not raw or not raw.strip():
        return ""
    raw = raw.strip()
    try:
        return json.loads(f'"{raw}"')
    except Exception:
        s = raw.replace("\\/", "/").replace(r"\/", "/")
        return s if s.startswith("http") else ""


def _pinterest_save_link_media(html: str) -> str:
    """
    Pinterest 'create pin' / save buttons pass the image in the media= query param.
    """
    for m in re.finditer(
        r'https?://(?:[\w-]+\.)?pinterest\.com/pin/create(?:/[\w-]+)?/?[^"\'\s<>\]]*',
        html,
        re.I,
    ):
        u = html_unescape(m.group(0))
        try:
            q = parse_qs(urlparse(u).query)
            med = (q.get("media") or [""])[0]
            if med:
                out = unquote(med)
                if out.startswith("http"):
                    return out
        except Exception:
            continue
    return ""


def _pinterest_plugin_image_url(html: str) -> str:
    """
    Social plugins embed the Pinterest-specific image (often a collage), not og:image.
    Tries several keys and quote styles seen in Social Warfare / Grow / Tasty Pins JSON.
    """
    keys = (
        "pinterest_image_url",
        "pin_image_url",
        "pinterestImageURL",
        "pinterestImageUrl",
        "pinImageUrl",
    )
    patterns = []
    for k in keys:
        ek = re.escape(k)
        patterns.extend(
            (
                rf'"{ek}"\s*:\s*"((?:[^"\\]|\\.)*)"',
                rf"'{ek}'\s*:\s*'((?:[^'\\]|\\.)*)'",
                rf'"{ek}"\s*:\s*"([^"]+)"',
                rf"'{ek}'\s*:\s*'([^']+)'",
            )
        )
    for pat in patterns:
        m = re.search(pat, html)
        if m:
            u = _decode_json_string_fragment(m.group(1))
            if u.startswith("http"):
                return u
    m = re.search(
        r'pinterest_image_url\s*[=:]\s*["\'](https?[^"\'\\]+)["\']',
        html,
        re.I,
    )
    if m:
        u = html_unescape(m.group(1).replace("\\/", "/"))
        if u.startswith("http"):
            return u
    return ""


def extract_pinterest_pin_meta(html: str, page_url: str = "") -> dict:
    """
    Best-effort: read Pinterest Pin id / canonical URL from widgets or links in cached HTML,
    plus the Pinterest-specific image URL when the page exposes it (save-button media=, plugin JSON,
    data-pin-media). We intentionally do NOT fall back to og:image / article hero — that is usually
    not the same asset as the plugin's pin collage / pin-optimized image.
    Returns only keys found: pinterest_pin_id, pinterest_pin_url, pinterest_pin_image_url.
    """
    out: dict = {}
    if not html or not html.strip():
        return out
    base = page_url or ""
    try:
        soup = BeautifulSoup(html, "html.parser")
        pin_id = None

        for tag in soup.find_all(True):
            did = tag.get("data-pin-id") or tag.get("data-pin")
            if did:
                s = str(did).strip()
                if s and re.match(r"^[0-9]+$", s):
                    pin_id = s
                    break
            for attr in ("data-pin-url", "data-pin-href", "data-url"):
                durl = tag.get(attr)
                if durl and "pin/" in durl:
                    m = _PIN_HREF_RE.search(durl)
                    if m:
                        pin_id = m.group(1).strip().rstrip("/")
                        break
            if pin_id:
                break

        if not pin_id:
            for a in soup.find_all("a", href=True):
                h = (a.get("href") or "").strip()
                if "pinterest." in h.lower() and "/pin/" in h.lower():
                    m = _PIN_HREF_RE.search(h)
                    if m:
                        cand = m.group(1).strip().rstrip("/").lower()
                        if cand and cand not in ("create", "create/bookmarklet", "bookmarklet"):
                            pin_id = cand
                            break

        if not pin_id:
            m = _PIN_HREF_RE.search(html)
            if m:
                cand = m.group(1).strip().rstrip("/").lower()
                if cand and cand not in ("create", "create/bookmarklet", "bookmarklet"):
                    pin_id = cand

        if pin_id:
            out["pinterest_pin_id"] = pin_id
            out["pinterest_pin_url"] = f"https://www.pinterest.com/pin/{pin_id}/"

        # Pin image: only Pinterest-tagged sources (not generic article hero)
        img_url = _pinterest_save_link_media(html)
        if not img_url:
            img_url = _pinterest_plugin_image_url(html)
        if not img_url and base:
            for tag in soup.find_all(True):
                dm = (tag.get("data-pin-media") or tag.get("data-pin-media-href")
                      or tag.get("data-pin-img") or "").strip()
                if not dm:
                    continue
                cand = abs_url(dm, base)
                if cand.startswith("http"):
                    img_url = cand
                    break
        if not img_url and base:
            # WP Recipe Maker "Pin Recipe" button often uses data-media.
            for tag in soup.find_all(True):
                dm = (tag.get("data-media") or "").strip()
                if not dm:
                    continue
                cls = " ".join(tag.get("class", [])).lower() if tag.get("class") else ""
                hint = ((tag.get("data-description") or "") + " " + (tag.get("href") or "") + " " + cls).lower()
                if "pin" not in hint and "pinterest" not in hint:
                    continue
                cand = abs_url(dm, base)
                if cand.startswith("http"):
                    img_url = cand
                    break
        if not img_url and base:
            for a in soup.find_all("a", href=True):
                h = (a.get("href") or "").strip()
                if "pinterest.com/pin/create" not in h.lower():
                    continue
                h2 = html_unescape(h)
                try:
                    q = parse_qs(urlparse(h2).query)
                    med = (q.get("media") or [""])[0]
                    if med:
                        cand = unquote(med)
                        if cand.startswith("http"):
                            img_url = cand
                            break
                except Exception:
                    continue
        if not img_url and base:
            for img in soup.find_all("img", src=True):
                par = img.find_parent("a", href=True)
                if not par:
                    continue
                if "pinterest.com/pin/create" not in (par.get("href") or "").lower():
                    continue
                cand = abs_url((img.get("src") or "").strip(), base)
                if cand.startswith("http"):
                    img_url = cand
                    break
        if img_url:
            out["pinterest_pin_image_url"] = img_url
    except Exception:
        pass
    return out


def apply_schema_to_html(html: str, url: str, schema: dict) -> dict:
    """
    Apply an extraction schema to raw HTML and return structured post data.
    Priority: JSON-LD (if site has it) → CSS selectors from schema.
    Returns {"meta": {...}, "blocks": [...]}
    """
    # 1. Try JSON-LD first (works for Next.js/React sites with schema.org)
    jsonld = _extract_jsonld(html, url)
    if jsonld and jsonld.get("blocks"):
        # Also parse article body for text/image blocks to supplement JSON-LD recipe data
        body_blocks = _extract_article_body(html, url)
        # Merge: body text first, then recipe card (deduplicate description)
        recipe_blocks = [b for b in jsonld["blocks"] if b.get("type") == "recipe_card"]
        # Remove pure description paragraphs that duplicate the recipe description
        recipe_desc = recipe_blocks[0].get("description", "") if recipe_blocks else ""
        recipe_name = recipe_blocks[0].get("name", "") if recipe_blocks else ""
        text_blocks = []
        for b in body_blocks:
            if b.get("type") == "recipe_card":
                continue
            if (b.get("type") == "paragraph" and recipe_desc
                    and b.get("text", "").strip() == recipe_desc.strip()):
                continue
            if (b.get("type") == "heading" and recipe_name
                    and b.get("text", "").strip().lower() == recipe_name.strip().lower()):
                continue
            text_blocks.append(b)
        merged_blocks = text_blocks + recipe_blocks if text_blocks else jsonld["blocks"]
        return {
            "meta": {
                "title":          jsonld.get("title", ""),
                "date":           jsonld.get("date", ""),
                "author":         jsonld.get("author", ""),
                "url":            url,
                "categories":     [],
                "tags":           [],
                "featured_image": jsonld.get("featured_image"),
                "excerpt":        jsonld.get("description", ""),
                "extracted_by":   "jsonld+html",
            },
            "blocks": merged_blocks,
        }

    # 2. CSS selector extraction
    try:
        soup = BeautifulSoup(html, "lxml")
        css = schema.get("css_selectors", {})
        date_attr = schema.get("date_attr", "text")
        img_attr  = schema.get("image_attr", "src")

        def sel_text(selector: str, default: str = "") -> str:
            if not selector:
                return default
            for s in [s.strip() for s in selector.split(",")]:
                el = soup.select_one(s)
                if el:
                    return el.get_text(" ", strip=True)
            return default

        def sel_attr(selector: str, attr: str, default: str = "") -> str:
            if not selector:
                return default
            for s in [s.strip() for s in selector.split(",")]:
                el = soup.select_one(s)
                if el:
                    val = el.get(attr, "") or el.get_text(" ", strip=True)
                    if val:
                        return abs_url(val, url) if attr in ("src", "href", "data-src") else val
            return default

        title = sel_text(css.get("title", "h1"))
        date  = sel_attr(css.get("date", "time"), date_attr) or sel_text(css.get("date", "time"))
        author = sel_text(css.get("author", ""))
        excerpt = sel_text(css.get("excerpt", ""))
        img = sel_attr(css.get("featured_image", "article img"), img_attr)
        if not img:
            img = sel_attr(css.get("featured_image", ""), "data-src")

        # Content area → run through ContentParser
        content_sel = css.get("content", "article, main")
        content_html = ""
        for s in [s.strip() for s in content_sel.split(",")]:
            el = soup.select_one(s)
            if el:
                content_html = str(el)
                break
        if not content_html:
            content_html = html

        blocks = ContentParser(content_html, url).parse()

        # Recipe extraction from schema if no recipe found in ContentParser
        recipe_schema = schema.get("recipe", {})
        has_recipe = any(b.get("type") == "recipe_card" for b in blocks)
        if not has_recipe and recipe_schema.get("container"):
            container_el = None
            for s in [s.strip() for s in recipe_schema["container"].split(",")]:
                container_el = soup.select_one(s)
                if container_el:
                    break
            if container_el:
                def many(sel):
                    if not sel:
                        return []
                    results = []
                    for s in [s.strip() for s in sel.split(",")]:
                        els = container_el.select(s)
                        if els:
                            results = [e.get_text(" ", strip=True) for e in els]
                            break
                    return results

                ingredients = [{"amount": "", "unit": "", "name": t, "notes": ""}
                               for t in many(recipe_schema.get("ingredients", ""))]
                instructions = [{"step": i+1, "text": t, "image": None}
                                for i, t in enumerate(many(recipe_schema.get("instructions", "")))]
                if ingredients or instructions:
                    blocks.insert(0, {
                        "type": "recipe_card",
                        "name": sel_text(recipe_schema.get("name", "")) or title,
                        "description": sel_text(recipe_schema.get("description", "")),
                        "prep_time": {"display": sel_text(recipe_schema.get("prep_time", "")), "minutes": ""},
                        "cook_time": {"display": sel_text(recipe_schema.get("cook_time", "")), "minutes": ""},
                        "total_time": {"display": sel_text(recipe_schema.get("total_time", "")), "minutes": ""},
                        "servings": sel_text(recipe_schema.get("servings", "")),
                        "image": img,
                        "ingredients": ingredients,
                        "instructions": instructions,
                        "notes": "",
                        "nutrition": {"summary": sel_text(recipe_schema.get("nutrition", ""))},
                    })

        return {
            "meta": {
                "title":          title,
                "date":           date[:10] if len(date) >= 10 else date,
                "author":         author,
                "url":            url,
                "categories":     [],
                "tags":           [],
                "featured_image": img or None,
                "excerpt":        excerpt,
                "extracted_by":   "schema_css",
            },
            "blocks": blocks,
        }
    except Exception as e:
        return {"meta": {"title": "", "url": url, "error": str(e)}, "blocks": []}


def _extract_jsonld(html: str, base_url: str) -> dict | None:
    """
    Extract structured content from JSON-LD <script> tags.
    Works on Next.js/React sites that embed schema.org data for SEO.
    Returns a dict with blocks[] or None if nothing useful found.
    """
    try:
        soup = BeautifulSoup(html, "lxml")
        schemas = []
        for tag in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(tag.string or "")
                if isinstance(data, list):
                    schemas.extend(data)
                elif isinstance(data, dict):
                    # Handle @graph
                    if data.get("@graph"):
                        schemas.extend(data["@graph"])
                    else:
                        schemas.append(data)
            except Exception:
                pass

        if not schemas:
            return None

        result = {"title": "", "date": "", "author": "", "description": "",
                  "featured_image": None, "blocks": []}

        for schema in schemas:
            stype = schema.get("@type", "")
            if isinstance(stype, list):
                stype = " ".join(stype)

            # ── Article / BlogPosting / NewsArticle ──
            if any(t in stype for t in ["Article", "BlogPosting", "NewsArticle", "WebPage"]):
                result["title"] = result["title"] or schema.get("headline", "") or schema.get("name", "")
                result["date"] = result["date"] or schema.get("datePublished", "")[:10] if schema.get("datePublished") else result["date"]
                result["description"] = result["description"] or schema.get("description", "")
                author = schema.get("author", {})
                if isinstance(author, list) and author:
                    author = author[0]
                if isinstance(author, dict):
                    result["author"] = result["author"] or author.get("name", "")
                elif isinstance(author, str):
                    result["author"] = result["author"] or author
                img = schema.get("image", {})
                if isinstance(img, str):
                    result["featured_image"] = result["featured_image"] or img
                elif isinstance(img, dict):
                    result["featured_image"] = result["featured_image"] or img.get("url", "")
                elif isinstance(img, list) and img:
                    first = img[0]
                    result["featured_image"] = result["featured_image"] or (
                        first if isinstance(first, str) else first.get("url", ""))

            # ── Recipe ──
            if "Recipe" in stype:
                result["title"] = result["title"] or schema.get("name", "")
                result["date"] = result["date"] or (schema.get("datePublished", "")[:10] if schema.get("datePublished") else "")
                result["description"] = result["description"] or schema.get("description", "")

                # Author — can be dict OR list of dicts
                if not result["author"]:
                    _ra = schema.get("author", {})
                    if isinstance(_ra, list) and _ra:
                        _ra = _ra[0]
                    if isinstance(_ra, dict):
                        result["author"] = _ra.get("name", "")
                    elif isinstance(_ra, str):
                        result["author"] = _ra

                img = schema.get("image", {})
                if isinstance(img, str):
                    result["featured_image"] = result["featured_image"] or img
                elif isinstance(img, dict):
                    result["featured_image"] = result["featured_image"] or img.get("url", "")
                elif isinstance(img, list) and img:
                    first = img[0]
                    result["featured_image"] = result["featured_image"] or (
                        first if isinstance(first, str) else first.get("url", ""))

                # Keywords → tags
                kw = schema.get("keywords", "")
                if kw and not result.get("tags"):
                    result["tags"] = [k.strip() for k in re.split(r"[,;]", kw) if k.strip()]

                def parse_time(val):
                    if not val:
                        return {"display": "", "minutes": ""}
                    # Full ISO 8601: P0Y0M0DT0H20M0.000S  or  PT30M  or  PT1H30M
                    m = re.search(r"T(?:(\d+)H)?(?:(\d+)M)?", val)
                    if m and (m.group(1) or m.group(2)):
                        h  = int(m.group(1) or 0)
                        mn = int(m.group(2) or 0)
                        total = h * 60 + mn
                        if total == 0:
                            return {"display": "", "minutes": ""}
                        display = f"{h}h {mn}m" if h else f"{mn}m"
                        return {"display": display, "minutes": str(total)}
                    return {"display": val, "minutes": ""}

                ingredients = []
                for ing in schema.get("recipeIngredient", []):
                    ingredients.append({"amount": "", "unit": "", "name": ing, "notes": ""})

                instructions = []
                for i, step in enumerate(schema.get("recipeInstructions", []), 1):
                    if isinstance(step, str):
                        instructions.append({"step": i, "text": step, "image": None})
                    elif isinstance(step, dict):
                        text = step.get("text", "") or step.get("name", "")
                        img_s = step.get("image", {})
                        step_img = None
                        if isinstance(img_s, str):
                            step_img = img_s
                        elif isinstance(img_s, dict):
                            step_img = img_s.get("url")
                        elif isinstance(img_s, list) and img_s:
                            step_img = img_s[0] if isinstance(img_s[0], str) else img_s[0].get("url")
                        instructions.append({"step": i, "text": text, "image": step_img})

                nutrition = schema.get("nutrition", {}) or {}
                nutr_fields = [
                    ("calories",            "Calories"),
                    ("proteinContent",      "Protein"),
                    ("carbohydrateContent", "Carbs"),
                    ("fatContent",          "Fat"),
                    ("saturatedFatContent", "Saturated Fat"),
                    ("fiberContent",        "Fiber"),
                    ("sugarContent",        "Sugar"),
                    ("cholesterolContent",  "Cholesterol"),
                    ("sodiumContent",       "Sodium"),
                ]
                nutr_parts = []
                nutr_detail = {}
                for k, label in nutr_fields:
                    v = (nutrition.get(k) or "").strip()
                    if v:
                        nutr_parts.append(f"{label}: {v}")
                        nutr_detail[label.lower().replace(" ", "_")] = v
                serving_size = (nutrition.get("servingSize") or "").strip()

                recipe_block = {
                    "type":        "recipe_card",
                    "name":        schema.get("name", ""),
                    "description": schema.get("description", ""),
                    "author":      result["author"],
                    "prep_time":   parse_time(schema.get("prepTime")),
                    "cook_time":   parse_time(schema.get("cookTime")),
                    "total_time":  parse_time(schema.get("totalTime")),
                    "servings":    str(schema.get("recipeYield", "")),
                    "serving_size": serving_size,
                    "image":       result["featured_image"],
                    "ingredients": ingredients,
                    "instructions": instructions,
                    "notes":       schema.get("recipeNotes", ""),
                    "cuisine":     str(schema.get("recipeCuisine", "")),
                    "category":    str(schema.get("recipeCategory", "")),
                    "diet":        str(schema.get("suitableForDiet", "")),
                    "keywords":    result.get("tags", []),
                    "nutrition":   {
                        "summary":      " | ".join(nutr_parts),
                        "serving_size": serving_size,
                        **nutr_detail,
                    },
                }
                result["blocks"].append(recipe_block)

        if result["title"] or result["blocks"]:
            # Add a description paragraph if present
            if result["description"] and not any(b.get("type") == "paragraph" for b in result["blocks"]):
                result["blocks"].insert(0, {"type": "paragraph",
                                            "text": result["description"],
                                            "html": f"<p>{result['description']}</p>"})
            return result
    except Exception:
        pass
    return None


def _extract_content_area(html: str, base_url: str) -> str:
    """Find the main content element in a full page HTML."""
    try:
        soup = BeautifulSoup(html, "lxml")
        for tag in soup.find_all(["script", "style", "nav", "header", "footer", "aside"]):
            tag.decompose()
        for sel in [".field--name-body .field__item", ".field--name-body",
                    "article .entry-content", "article .post-content",
                    ".entry-content", ".post-content", "article",
                    "main .post", "[class*='article-content']"]:
            el = soup.select_one(sel)
            if el:
                for img in el.find_all("img"):
                    img["src"] = abs_url(best_src(img), base_url)
                for a in el.find_all("a", href=True):
                    if a["href"].startswith("/"):
                        a["href"] = abs_url(a["href"], base_url)
                return str(el)
    except Exception:
        pass
    return html


def _build_post_html(post: dict, blocks: list, base_url: str,
                     site_info: dict, ai_used: bool = False,
                     theme_css: str = None, site_css: str = "") -> str:
    title      = post.get("title", "Untitled")
    date_str   = fmt_date(post.get("date", ""))
    url        = post.get("link", "")
    categories = post.get("categories", [])
    tags       = post.get("tags", [])
    fi         = post.get("featured_image", "")
    site_name  = site_info.get("name", "") or base_url
    ai_badge   = '<span class="ai-badge">🤖 AI extracted</span>' if ai_used else ""

    cat_tags = "".join(f'<a class="tag cat" href="{c.get("link","#")}">{c.get("name","")}</a>' for c in categories)
    tax_tags = "".join(f'<a class="tag tax" href="{t.get("link","#")}">{t.get("name","")}</a>' for t in tags)
    fi_html  = f'<img class="featured-img" src="{fi}" alt="{title}" loading="lazy"/>' if fi else ""
    content  = render_blocks_html(blocks) or "<p>No content could be extracted.</p>"

    # Build the <style> block:
    #   • When real site CSS is available: use it (gives original fonts/colours/typography)
    #     then append _CHROME_CSS (our structural wrappers) on top.
    #   • Fallback: use _FALLBACK_TYPOGRAPHY_CSS + _CHROME_CSS (current default look).
    if site_css and site_css.strip():
        style_block = f"/* === Original site CSS (filtered) === */\n{site_css}\n{_CHROME_CSS}"
    else:
        style_block = _FALLBACK_TYPOGRAPHY_CSS + _CHROME_CSS

    if theme_css:
        style_block += f"\n/* AI Generated Theme */\n{theme_css}"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1.0"/>
  <title>{title}</title>
  <style>{style_block}</style>
</head>
<body>
  <div class="site-name"><a href="{base_url}">{site_name}</a>{ai_badge}</div>
  <h1>{title}</h1>
  <div class="meta">{date_str}</div>
  <div class="tags">{cat_tags}{tax_tags}</div>
  {fi_html}
  <div class="content">{content}</div>
  <div class="footer">
    Original: <a href="{url}">{url}</a>&nbsp;·&nbsp;Scraped by WP Scraper
    {"&nbsp;·&nbsp;🤖 AI extracted" if ai_used else ""}
  </div>
</body>
</html>"""


# ─── Blog theme article builder ──────────────────────────────────────────────

def build_themed_article_html(post: dict, post_json: dict,
                               theme: dict, site_name: str,
                               categories: list, writers: list,
                               base_url: str = "",
                               all_posts: list = None,
                               pages: list = None,
                               important_pages: dict = None) -> str:
    """
    Assemble a full blog-themed article page from:
      - theme: {css, header, footer, author_card}  (from generate_blog_theme)
      - post: index-level post dict (title, date, featured_image, categories…)
      - post_json: deep-extracted dict (blocks, excerpt, meta…)
      - writers: list of {name, bio, specialty} dicts
      - all_posts: full post list used to build sidebar (related + recent articles)
    """
    import html as _html
    import time as _time
    import re as _re

    css          = theme.get("css", "")
    header_tmpl  = theme.get("header", "")
    footer_tmpl  = theme.get("footer", "")
    author_tmpl  = theme.get("author_card", "")

    title       = post.get("title", "Untitled")
    date_raw    = post.get("date", "")[:10]
    featured    = post.get("featured_image", "") or ""
    post_cats   = post.get("categories", [])
    post_fn     = post.get("filename", "")
    blocks      = post_json.get("blocks", [])
    excerpt     = post_json.get("excerpt", "") or post.get("excerpt", "")
    author_name = (post_json.get("meta", {}) or {}).get("author", "") or ""

    def _cat_slug(name):
        return _re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")

    # ── Match author to writer profile ────────────────────────────────────────
    writer = {}
    if writers:
        for w in writers:
            if author_name and w.get("name", "").lower() in author_name.lower():
                writer = w
                break
        if not writer:
            idx = abs(hash(post_fn)) % len(writers)
            writer = writers[idx]
    author_display   = writer.get("name", "") or author_name or "Editor"
    author_bio       = writer.get("bio", "")
    author_specialty = writer.get("specialty", "")
    author_initial   = author_display[0].upper() if author_display else "E"

    # ── Build url → local filename map (for internal link rewriting) ────────
    _url_to_fn: dict = {}
    for _p in (all_posts or []):
        _purl = (_p.get("link") or "").rstrip("/")
        _pfn  = _p.get("filename", "")
        if _purl and _pfn:
            _url_to_fn[_purl] = _pfn
    for _pg in (pages or []):
        _pgurl = (_pg.get("link") or "").rstrip("/")
        _pgfn  = _pg.get("filename") or safe_filename(_pg.get("id"), _pg.get("slug", ""))
        if _pgurl and _pgfn:
            _url_to_fn[_pgurl] = _pgfn

    # ── Build nav links ───────────────────────────────────────────────────────
    # Site pages (About, Contact, Privacy Policy…)
    # Fallback: use important_pages from site_scan when pages list is empty
    _pages_list = pages or []
    if not _pages_list and important_pages:
        _pages_list = [{"title": label, "link": url, "filename": ""}
                       for label, url in important_pages.items()]
    _page_nav = ""
    for _pg in _pages_list[:6]:
        _pg_title = (_pg.get("title") or "").strip()
        _pg_fn    = _pg.get("filename") or safe_filename(_pg.get("id"), _pg.get("slug", ""))
        _pg_url   = _pg.get("link", "")
        if not _pg_title:
            continue
        # If we built a local redesigned file for this page, use it; else original URL
        if _pg_fn in _url_to_fn.values():
            _page_nav += f'<a href="{_pg_fn}.html">{_html.escape(_pg_title)}</a>'
        elif _pg_url:
            _page_nav += (f'<a href="{_html.escape(_pg_url)}" target="_blank" rel="noopener">'
                          f'{_html.escape(_pg_title)}</a>')

    nav_links = '<a href="index.html">Home</a>' + _page_nav + "".join(
        f'<a href="category-{_cat_slug(c)}.html">{_html.escape(c)}</a>'
        for c in (categories or [])[:6]
    )

    # ── Category badges ───────────────────────────────────────────────────────
    cat_badges = "".join(
        f'<span class="cat-badge">{_html.escape(c.get("name",""))}</span>'
        for c in post_cats if c.get("name")
    )

    # ── Featured image ────────────────────────────────────────────────────────
    fi_html = (f'<img class="featured-img" src="{_html.escape(featured)}" '
               f'alt="{_html.escape(title)}" loading="eager"/>') if featured else ""

    # ── Article content ───────────────────────────────────────────────────────
    content_html = render_blocks_html(blocks) or f"<p>{_html.escape(excerpt)}</p>"

    # Rewrite internal links → local .html files
    if _url_to_fn:
        def _rewrite_href(m):
            raw = m.group(1)
            stripped = raw.rstrip("/")
            if stripped in _url_to_fn:
                return f'href="{_url_to_fn[stripped]}.html"'
            # Also try without protocol variation
            stripped2 = _re.sub(r'^https?://', '', stripped)
            for k, v in _url_to_fn.items():
                if _re.sub(r'^https?://', '', k) == stripped2:
                    return f'href="{v}.html"'
            return m.group(0)  # unchanged
        content_html = _re.sub(r'href="([^"]+)"', _rewrite_href, content_html)

    # ── Sidebar ───────────────────────────────────────────────────────────────
    sidebar_html = ""
    if all_posts:
        this_cat_names = {c.get("name","") for c in post_cats if c.get("name")}

        # Related: same category, not current post
        related = [
            p for p in all_posts
            if p.get("filename") != post_fn
            and any(c.get("name","") in this_cat_names for c in p.get("categories",[]))
        ][:5]

        # Recent: latest posts excluding current
        recent = [
            p for p in all_posts
            if p.get("filename") != post_fn
        ][:6]

        def _sidebar_item(p):
            fn    = p.get("filename", "")
            t     = _html.escape(p.get("title", "Untitled"))
            img   = p.get("featured_image", "") or ""
            d     = p.get("date", "")[:10]
            img_h = (f'<img class="sb-post-img" src="{_html.escape(img)}" alt="{t}" loading="lazy"/>'
                     if img else '<div class="sb-post-img sb-no-img"></div>')
            return (f'<a class="sb-post" href="{fn}.html">'
                    f'{img_h}'
                    f'<div class="sb-post-info"><div class="sb-post-title">{t}</div>'
                    f'<div class="sb-post-date">{d}</div></div></a>')

        # Related articles widget
        related_widget = ""
        if related:
            items = "".join(_sidebar_item(p) for p in related)
            related_widget = (
                f'<div class="sidebar-widget">'
                f'<div class="sidebar-widget-title">Related Articles</div>'
                f'<div class="sb-posts">{items}</div>'
                f'</div>'
            )

        # Recent articles widget
        recent_items = "".join(_sidebar_item(p) for p in recent)
        recent_widget = (
            f'<div class="sidebar-widget">'
            f'<div class="sidebar-widget-title">Recent Articles</div>'
            f'<div class="sb-posts">{recent_items}</div>'
            f'</div>'
        )

        # Categories widget
        cat_links = "".join(
            f'<a class="sb-cat-link" href="category-{_cat_slug(c)}.html">{_html.escape(c)}</a>'
            for c in (categories or [])[:12]
        )
        cats_widget = (
            f'<div class="sidebar-widget">'
            f'<div class="sidebar-widget-title">Categories</div>'
            f'<div class="sb-cats">{cat_links}</div>'
            f'</div>'
        ) if cat_links else ""

        sidebar_html = (
            f'<aside class="article-sidebar">'
            + related_widget
            + recent_widget
            + cats_widget
            + '</aside>'
        )

    # ── Fill theme partials ───────────────────────────────────────────────────
    site_initial = (site_name[0].upper()) if site_name else "B"
    year = _time.strftime("%Y")

    def _fill(tmpl: str) -> str:
        return (tmpl
                .replace("{{SITE_NAME}}",        _html.escape(site_name))
                .replace("{{SITE_INITIAL}}",      site_initial)
                .replace("{{NAV_LINKS}}",         nav_links)
                .replace("{{YEAR}}",              year)
                .replace("{{AUTHOR_NAME}}",       _html.escape(author_display))
                .replace("{{AUTHOR_BIO}}",        _html.escape(author_bio))
                .replace("{{AUTHOR_INITIAL}}",    author_initial)
                .replace("{{AUTHOR_SPECIALTY}}", _html.escape(author_specialty)))

    header_html      = _fill(header_tmpl) if header_tmpl  else f'<header class="site-header"><div class="header-inner"><div class="site-logo">{site_initial}</div><span class="site-name">{_html.escape(site_name)}</span><nav class="site-nav">{nav_links}</nav></div></header>'
    footer_html      = _fill(footer_tmpl) if footer_tmpl  else f'<footer class="site-footer"><div class="footer-inner">© {year} {_html.escape(site_name)}</div></footer>'
    author_card_html = _fill(author_tmpl) if author_tmpl  else ""

    # ── Extract Google Fonts for fast <link> loading ──────────────────────────
    font_links = ""
    for m in _re.findall(r'@import\s+url\(["\']?(https://fonts\.googleapis\.com[^"\')]+)["\']?\)', css):
        font_links += (
            '<link rel="preconnect" href="https://fonts.googleapis.com"/>\n  '
            '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>\n  '
            f'<link rel="stylesheet" href="{m}"/>\n  '
        )
        css = _re.sub(r'@import\s+url\(["\']?' + _re.escape(m) + r'["\']?\)\s*;?', '', css)

    # ── CSS injection order:
    #   1. pre_css  — baseline defaults (readable even without AI theme)
    #   2. css      — AI theme (full creative control over visuals)
    #   3. lock_css — !important overrides so AI can't break core layout

    pre_css = """
/* ── Baseline defaults (AI theme may override visuals but not structure) ── */
*,*::before,*::after{box-sizing:border-box}
body{max-width:none;margin:0;padding:0;background:#fff;color:#111;font-family:system-ui,sans-serif;line-height:1.7}
img{max-width:100%;height:auto}
a{color:inherit}
/* Article two-column grid */
.article-layout{display:grid;grid-template-columns:1fr 300px;gap:40px;align-items:start;max-width:1160px;margin:0 auto;padding:40px 24px 80px}
.article-primary{min-width:0;overflow:hidden}
/* Sidebar */
.article-sidebar{display:flex;flex-direction:column;gap:20px}
.sidebar-widget{padding:16px;border-radius:10px}
.sidebar-widget-title{font-size:.82rem;font-weight:700;text-transform:uppercase;letter-spacing:.5px;margin-bottom:12px;padding-bottom:8px}
.sb-posts{display:flex;flex-direction:column;gap:10px}
.sb-post{display:flex;gap:10px;align-items:flex-start;text-decoration:none;color:inherit}
.sb-post-img{width:58px;height:58px;object-fit:cover;flex-shrink:0;border-radius:6px}
.sb-no-img{background:#f0f0f0;width:58px;height:58px;flex-shrink:0;border-radius:6px;display:block}
.sb-post-info{flex:1;min-width:0}
.sb-post-title{font-size:12px;font-weight:600;line-height:1.4;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;margin-bottom:2px}
.sb-post-date{font-size:10px;opacity:.6}
.sb-cats{display:flex;flex-direction:column;gap:4px}
.sb-cat-link{display:flex;align-items:center;justify-content:space-between;text-decoration:none;padding:6px 10px;border-radius:6px;font-size:12px;font-weight:600}
/* Article */
.article{padding:0}
.article-header{margin-bottom:24px}
.article-cats{margin-bottom:10px}
.cat-badge{display:inline-block;padding:3px 10px;border-radius:20px;font-size:.72rem;font-weight:700;margin-right:4px}
.article-title{font-size:2.2rem;font-weight:800;line-height:1.2;margin:0 0 14px}
.article-meta{display:flex;align-items:center;gap:12px;font-size:.85rem;margin-bottom:24px;opacity:.7}
.featured-img{display:block;width:100%;height:420px;object-fit:cover;border-radius:10px;margin-bottom:32px}
.article-content{font-size:1rem;line-height:1.8}
.article-content p{margin:0 0 1.3em}
.article-content h2{font-size:1.5rem;font-weight:700;margin:2em 0 .7em}
.article-content h3{font-size:1.2rem;font-weight:700;margin:1.6em 0 .5em}
.article-content a{text-decoration:underline}
.article-content blockquote{margin:1.5em 0;padding:12px 20px;border-left:4px solid #ccc;font-style:italic}
.article-content ul,.article-content ol{padding-left:1.6em;margin-bottom:1.3em}
.article-content li{margin-bottom:.4em}
.article-content img{max-width:100%;border-radius:8px;height:auto;display:block;margin:1em auto}
.article-content table{width:100%;border-collapse:collapse;margin:1.5em 0;font-size:.93rem}
.article-content th{padding:9px 12px;text-align:left;font-weight:700;border-bottom:2px solid #ddd}
.article-content td{padding:8px 12px;border-bottom:1px solid #eee}
/* Author card */
.author-card{display:flex;gap:14px;align-items:flex-start;padding:20px;border-radius:10px;margin-top:36px}
.author-avatar{width:52px;height:52px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:1.4rem;font-weight:800;flex-shrink:0}
.author-info{flex:1}
.author-name{font-weight:700;margin-bottom:2px}
.author-specialty{font-size:.82rem;margin-bottom:6px}
.author-bio{font-size:.87rem;line-height:1.6;margin:0}
/* Recipe card */
.recipe-card{border-radius:12px;overflow:hidden;margin:2em 0}
.rc-head{padding:20px 22px 0}
.rc-title{font-size:1.4rem;font-weight:800;margin:0 0 6px}
.rc-desc{font-size:.9rem;margin:0 0 4px}
.rc-times{display:flex;flex-wrap:wrap;padding:12px 22px;border-top:1px solid rgba(0,0,0,.08);margin-top:14px}
.rc-time-item{flex:1;min-width:80px;padding:6px 10px}
.rc-time-label{display:block;font-size:.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.4px;opacity:.6;margin-bottom:2px}
.rc-time-value{font-size:.95rem;font-weight:700}
.rc-section{padding:16px 22px;border-top:1px solid rgba(0,0,0,.08)}
.rc-section h3{font-size:.9rem;font-weight:800;text-transform:uppercase;letter-spacing:.4px;margin:0 0 12px}
.rc-ingredients{list-style:none;padding:0;margin:0;display:flex;flex-direction:column;gap:6px}
.rc-ingredients li{display:flex;gap:5px;font-size:.92rem;padding:5px 0;border-bottom:1px solid rgba(0,0,0,.06)}
.rc-ing-amount{font-weight:700;min-width:24px}
.rc-ing-unit{opacity:.7}
.rc-steps{list-style:none;padding:0;margin:0;display:flex;flex-direction:column;gap:14px}
.rc-step{display:flex;gap:14px;align-items:flex-start}
.rc-step-num{width:28px;height:28px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:.82rem;flex-shrink:0}
.rc-step-body{flex:1}
.rc-step-body p{margin:0;font-size:.93rem;line-height:1.7}
.rc-hero-img{width:100%;max-height:360px;object-fit:cover;display:block}
.rc-notes,.rc-nutrition{padding:14px 22px;font-size:.88rem}
.rc-notes h4,.rc-nutrition h4{margin:0 0 6px;font-size:.82rem;font-weight:700;text-transform:uppercase;letter-spacing:.4px}
/* Footer */
.site-footer{margin-top:60px;padding:28px 24px}
.footer-inner{max-width:1160px;margin:0 auto;display:flex;align-items:center;gap:16px;flex-wrap:wrap}
.footer-nav{display:flex;gap:12px;flex-wrap:wrap}
.footer-nav a{font-size:.83rem;text-decoration:none;opacity:.7}
.footer-nav a:hover{opacity:1}
.footer-copy{font-size:.8rem;opacity:.6;margin-left:auto}
"""

    # After AI CSS: lock the 4 layout properties that can NEVER change
    lock_css = """
/* ── Layout lock — overrides any conflicting AI CSS ── */
.article-layout{grid-template-columns:1fr 300px!important;max-width:1160px!important}
.article-primary{min-width:0!important;overflow:hidden!important}
.featured-img{width:100%!important;max-height:480px!important;object-fit:cover!important;display:block!important;height:auto!important}
.article-content img{max-width:100%!important;height:auto!important}
@media(max-width:900px){
  .article-layout{grid-template-columns:1fr!important;padding:24px 16px 60px!important}
  .article-sidebar{position:static!important}
}
@media(max-width:640px){
  .article-layout{padding:14px 12px 48px!important}
  .featured-img{height:260px!important}
}"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1.0"/>
  <title>{_html.escape(title)} – {_html.escape(site_name)}</title>
  {font_links}<style>{pre_css}{css}{lock_css}</style>
</head>
<body>
{header_html}
<div class="article-layout">
  <div class="article-primary">
    <article class="article">
      <div class="article-header">
        {f'<div class="article-cats">{cat_badges}</div>' if cat_badges else ""}
        <h1 class="article-title">{_html.escape(title)}</h1>
        <div class="article-meta">
          <span class="article-author">By {_html.escape(author_display)}</span>
          <span class="article-date">{date_raw}</span>
        </div>
      </div>
      {fi_html}
      <div class="article-content">
        {content_html}
      </div>
    </article>
    {author_card_html}
  </div>
  {sidebar_html}
</div>
{footer_html}
</body>
</html>"""


# ─── Index HTML builder ───────────────────────────────────────────────────────

def build_index_html(site_info: dict, base_url: str, posts: list) -> str:
    site_name = site_info.get("name", "") or base_url
    site_desc = site_info.get("description", "")
    site_type = site_info.get("site_type", "")
    total     = len(posts)

    all_cats: dict = {}
    for p in posts:
        for c in p.get("categories", []):
            cid = str(c.get("id") or c.get("name","")).lower()
            if cid and cid not in all_cats:
                all_cats[cid] = c.get("name", cid)

    cat_btns = f'<button class="cat-btn active" data-cat="all">All ({total})</button>'
    for cid, cname in sorted(all_cats.items(), key=lambda x: x[1]):
        count = sum(1 for p in posts
                    if any(str(c.get("id","")).lower() == cid or c.get("name","").lower() == cname.lower()
                           for c in p.get("categories", [])))
        cat_btns += f'<button class="cat-btn" data-cat="{cid}">{cname} ({count})</button>'

    cards = ""
    for p in posts:
        cats     = p.get("categories", [])
        cat_ids  = ",".join(str(c.get("id","")).lower() for c in cats)
        cat_tags = "".join(f'<span class="tag">{c.get("name","")}</span>' for c in cats)
        fi       = p.get("featured_image", "")
        fi_html  = (f'<img class="card-img" src="{fi}" alt="" loading="lazy"/>'
                    if fi else '<div class="card-img-placeholder"></div>')
        date_str  = fmt_date(p.get("date", ""))
        filename  = p.get("filename", "")
        title     = p.get("title", "Untitled")
        excerpt   = p.get("excerpt", "")
        status    = p.get("deep_status", "pending")
        ai_mark   = p.get("structured", {}).get("meta", {}).get("ai_extracted", False)

        title_html = title
        json_link  = ""
        ai_badge   = '<span class="ai-badge">🤖 AI</span>' if ai_mark else ""
        if status == "done" and filename:
            title_html = f'<a href="posts/{filename}.html">{title}</a>'
            json_link  = f'<a class="json-link" href="posts/{filename}.json" title="Structured JSON">JSON ↓</a>'

        st_badge = {
            "done":    '<span class="badge done">Scraped</span>',
            "error":   '<span class="badge err">Error</span>',
            "pending": '',
        }.get(status, "")

        cards += f"""
        <div class="card" data-cats="{cat_ids}" data-title="{title.lower()}">
          {fi_html}
          <div class="card-body">
            <div class="card-tags">{cat_tags}</div>
            <h2 class="card-title">{title_html}</h2>
            <p class="card-excerpt">{excerpt[:155]}{"…" if len(excerpt) > 155 else ""}</p>
            <div class="card-meta">{date_str} {st_badge} {ai_badge} {json_link}</div>
          </div>
        </div>"""

    type_badge = f' · <span style="color:#6366f1;">{site_type}</span>' if site_type else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1.0"/>
  <title>{site_name} — Index</title>
  <style>
    *,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f5f5f5;color:#222}}
    header{{background:#fff;border-bottom:1px solid #e5e5e5;padding:24px 32px}}
    header h1{{font-size:1.5rem;margin-bottom:4px}}
    header p{{color:#888;font-size:.88rem}}
    .toolbar{{padding:14px 32px;background:#fff;border-bottom:1px solid #eee;display:flex;gap:8px;flex-wrap:wrap;align-items:center}}
    .search{{padding:7px 14px;border:1px solid #ddd;border-radius:20px;font-size:.85rem;outline:none;width:200px}}
    .search:focus{{border-color:#1a56db}}
    .cat-btn{{padding:5px 13px;border-radius:20px;border:1px solid #ddd;background:#fff;font-size:.8rem;cursor:pointer;transition:all .15s}}
    .cat-btn:hover{{border-color:#1a56db;color:#1a56db}}
    .cat-btn.active{{background:#1a56db;border-color:#1a56db;color:#fff}}
    .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:18px;padding:24px 32px}}
    .card{{background:#fff;border-radius:10px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.07);transition:box-shadow .2s}}
    .card:hover{{box-shadow:0 4px 16px rgba(0,0,0,.12)}}
    .card-img{{width:100%;height:175px;object-fit:cover;display:block}}
    .card-img-placeholder{{width:100%;height:175px;background:#f0f0f0}}
    .card-body{{padding:14px}}
    .card-tags{{display:flex;gap:5px;flex-wrap:wrap;margin-bottom:7px}}
    .tag{{background:#e8f0fe;color:#1a56db;padding:2px 8px;border-radius:12px;font-size:.72rem;font-weight:600}}
    .card-title{{font-size:.95rem;font-weight:700;margin-bottom:6px;line-height:1.4}}
    .card-title a{{color:#111;text-decoration:none}}
    .card-title a:hover{{color:#1a56db}}
    .card-excerpt{{font-size:.8rem;color:#666;line-height:1.5;margin-bottom:8px}}
    .card-meta{{font-size:.75rem;color:#aaa;display:flex;align-items:center;gap:8px;flex-wrap:wrap}}
    .badge{{padding:2px 7px;border-radius:10px;font-size:.7rem;font-weight:700}}
    .badge.done{{background:#d1fae5;color:#065f46}}
    .badge.err{{background:#fee2e2;color:#991b1b}}
    .ai-badge{{background:#f0fdf4;border:1px solid #bbf7d0;color:#065f46;padding:1px 6px;border-radius:8px;font-size:.7rem;font-weight:700}}
    .json-link{{color:#6366f1;font-weight:700;font-size:.72rem;text-decoration:none;margin-left:auto}}
    .json-link:hover{{text-decoration:underline}}
    .hidden{{display:none!important}}
    .count{{margin-left:auto;font-size:.82rem;color:#888}}
    footer{{text-align:center;padding:28px;font-size:.78rem;color:#bbb}}
  </style>
</head>
<body>
  <header>
    <h1>{site_name}</h1>
    <p>{site_desc}&nbsp;·&nbsp;<a href="{base_url}">{base_url}</a>{type_badge}&nbsp;·&nbsp;{total} posts</p>
  </header>
  <div class="toolbar">
    <input class="search" type="text" placeholder="Search…" oninput="filter()" id="q"/>
    {cat_btns}
    <span class="count" id="cnt">{total} posts</span>
  </div>
  <div class="grid" id="grid">{cards}</div>
  <footer>Generated by WP Scraper · {total} posts from {base_url}</footer>
  <script>
    let activeCat='all';
    document.querySelectorAll('.cat-btn').forEach(b=>{{
      b.addEventListener('click',()=>{{
        document.querySelectorAll('.cat-btn').forEach(x=>x.classList.remove('active'));
        b.classList.add('active'); activeCat=b.dataset.cat; filter();
      }});
    }});
    function filter(){{
      const q=document.getElementById('q').value.toLowerCase(); let n=0;
      document.querySelectorAll('.card').forEach(c=>{{
        const cats=c.dataset.cats.split(',');
        const mc=activeCat==='all'||cats.includes(activeCat);
        const mq=!q||c.dataset.title.includes(q)||c.textContent.toLowerCase().includes(q);
        c.classList.toggle('hidden',!(mc&&mq)); if(mc&&mq)n++;
      }});
      document.getElementById('cnt').textContent=n+' posts';
    }}
  </script>
</body>
</html>"""


# ─── Site intelligence scan ───────────────────────────────────────────────────

_MONETIZATION_SIGS: list[tuple[str, list[str]]] = [
    ("Google AdSense",       ["pagead2.googlesyndication.com", "adsbygoogle", "google_ad_client"]),
    ("Google Ad Manager",    ["googletag.pubads", "doubleclick.net/tag/js", "gpt.js"]),
    ("MediaVine",            ["mediavine.com", "mv-sticky"]),
    ("Raptive / AdThrive",   ["adthrive.com", "raptive.com"]),
    ("Ezoic",                ["ezoic.com", "ezoicads", "ezoic_pub_ad"]),
    ("Amazon Associates",    ["amazon-adsystem.com", "/ref=", "tag=", "amzn.to"]),
    ("Taboola",              ["cdn.taboola.com", "tbl.0.js"]),
    ("Outbrain",             ["widgets.outbrain.com", "outbrain.js"]),
    ("ShareASale",           ["shareasale.com"]),
    ("CJ Affiliate",         ["cj.com/product", "anrdoezrs", "jdoqocy"]),
    ("Impact Radius",        ["impact.com/affiliates", "sjv.io"]),
    ("Sovrn / VigLink",      ["sovrn.com", "viglink.com"]),
    ("Monumetric",           ["monumetric.com"]),
    ("BuySellAds / Carbon",  ["buysellads.com", "carbonads.com"]),
    ("PropellerAds",         ["propellerads.com"]),
    ("InfoLinks",            ["infolinks.com"]),
    ("Skimlinks",            ["skimlinks.com", "skimresources.com"]),
    ("Grow (Mediavine)",     ["grow.me/publisher"]),
    ("Clickio",              ["clickio.com"]),
]

# Ads.txt first-column domain → human-readable ad network name
# Covers the most common SSPs, DSPs, and exchange networks seen in the wild.
_ADS_TXT_DOMAIN_MAP: dict[str, str] = {
    # Google
    "google.com":              "Google (AdX / AdSense)",
    "googlesyndication.com":   "Google AdSense",
    "doubleclick.net":         "Google DoubleClick",
    # Ezoic
    "ezoic.com":               "Ezoic",
    "ezoic.ai":                "Ezoic",
    "ezoic.co.uk":             "Ezoic",
    # Raptive / AdThrive
    "adthrive.com":            "Raptive / AdThrive",
    "raptive.com":             "Raptive / AdThrive",
    # MediaVine
    "mediavine.com":           "MediaVine",
    # Major SSPs / Exchanges
    "appnexus.com":            "AppNexus / Xandr",
    "xandr.com":               "Xandr",
    "openx.com":               "OpenX",
    "rubiconproject.com":      "Rubicon Project",
    "pubmatic.com":            "PubMatic",
    "indexexchange.com":       "Index Exchange",
    "criteo.com":              "Criteo",
    "33across.com":            "33Across",
    "sovrn.com":               "Sovrn",
    "lijit.com":               "Sovrn (Lijit)",
    "sonobi.com":              "Sonobi",
    "sharethrough.com":        "Sharethrough",
    "triplelift.com":          "TripleLift",
    "districtm.io":            "District M",
    "districtm.ca":            "District M",
    "smartadserver.com":       "Smart AdServer",
    "media.net":               "Media.net",
    "teads.tv":                "Teads",
    "teads.com":               "Teads",
    "amazon-adsystem.com":     "Amazon (A9)",
    "emxdgt.com":              "EMX Digital",
    "undertone.com":           "Undertone",
    "yieldmo.com":             "YieldMo",
    "spotxchange.com":         "SpotX",
    "spotx.tv":                "SpotX",
    "loopme.com":              "LoopMe",
    "kargo.com":               "Kargo",
    "gumgum.com":              "GumGum",
    "improve.digital":         "Improve Digital",
    "contextweb.com":          "Pulsepoint",
    "pulsepoint.com":          "Pulsepoint",
    "conversant.com":          "Conversant",
    "freewheel.tv":            "FreeWheel",
    "adform.com":              "Adform",
    "onetag.com":              "OneTag",
    "onetag.net":              "OneTag",
    "outbrain.com":            "Outbrain",
    "taboola.com":             "Taboola",
    "rhythmone.com":           "RhythmOne",
    "unruly.co":               "Unruly",
    "themediagrid.com":        "The Media Grid",
    "admixer.net":             "AdMixer",
    "yahoo.com":               "Yahoo / Verizon",
    "oath.com":                "Verizon Media",
    "verizonmedia.com":        "Verizon Media",
    "advertising.com":         "Advertising.com",
    "nativo.com":              "Nativo",
    "nativo.net":              "Nativo",
    "springserve.com":         "SpringServe",
    "minutemedia.com":         "Minute Media",
    "beachfront.com":          "Beachfront",
    "liveintent.com":          "LiveIntent",
    "mgid.com":                "MGID",
    "smaato.com":              "Smaato",
    "tremorhub.com":           "Tremor Video",
    "tremorvideo.com":         "Tremor Video",
    "risecodes.com":           "Rise Codes",
    "richaudience.com":        "Rich Audience",
    "bidswitch.net":           "BidSwitch (IPONWEB)",
    "concert.io":              "Concert",
    "kiosked.com":             "Kiosked",
    "powerlinks.com":          "PowerLinks",
    "primeaudience.com":       "Prime Audience",
    "broadstreetads.com":      "BroadStreet",
    "yieldbot.com":            "YieldBot",
    "lkqd.net":                "LKQD",
    "4dex.io":                 "4D Exchange",
    "adskeeper.com":           "AdsKeeper",
    "adtelligent.com":         "Adtelligent",
    "goldbach.com":            "Goldbach",
    "mobfox.com":              "MobFox",
    "plista.com":              "Plista",
    "rtbsape.com":             "Sape",
    "targetspot.com":          "TargetSpot",
    "viewlift.com":            "ViewLift",
    "impact.com":              "Impact",
    "shareasale.com":          "ShareASale",
    "skimlinks.com":           "Skimlinks",
    "viglink.com":             "Sovrn / VigLink",
    "monumetric.com":          "Monumetric",
    "mediabistro.com":         "Mediabistro",
    "grow.me":                 "Grow (Mediavine)",
    "infolinks.com":           "InfoLinks",
    "propellerads.com":        "PropellerAds",
    "clickio.com":             "Clickio",
    "cj.com":                  "CJ Affiliate",
    "buysellads.com":          "BuySellAds",
    "carbonads.com":           "Carbon Ads",
    "ssp.caden.io":            "Caden SSP",
    "caden.io":                "Caden",
    "fueldigital.com":         "Fuel Digital",
    "synperion.com":           "Synperion",
    "smartstream.tv":          "SmartStream",
    "setupad.com":             "SetupAd",
}

_ANALYTICS_SIGS: list[tuple[str, list[str]]] = [
    ("Google Analytics 4",   ["gtag('config", 'gtag("config', '"G-']),
    ("Google Analytics UA",  ["ga('create'", 'ga("create"', "'UA-", "google-analytics.com/analytics.js"]),
    ("Google Tag Manager",   ["googletagmanager.com/gtm.js", "GTM-"]),
    ("Facebook / Meta Pixel",["connect.facebook.net/en_US/fbevents", "fbq(", "_fbq"]),
    ("Hotjar",               ["static.hotjar.com", "/hotjar-"]),
    ("Microsoft Clarity",    ["clarity.ms/tag", "clarity.ms/s/"]),
    ("Pinterest Tag",        ["pintrk(", "s.pinimg.com/ct.js"]),
    ("TikTok Pixel",         ["analytics.tiktok.com", "ttq."]),
    ("Mixpanel",             ["cdn.mxpnl.com", "api.mixpanel.com"]),
    ("Segment",              ["cdn.segment.com"]),
    ("Heap",                 ["heapanalytics.com"]),
]

_SOCIAL_DOMAINS: dict[str, str] = {
    "facebook.com":   "Facebook",
    "fb.com":         "Facebook",
    "twitter.com":    "Twitter",
    "x.com":          "X / Twitter",
    "instagram.com":  "Instagram",
    "pinterest.com":  "Pinterest",
    "youtube.com":    "YouTube",
    "tiktok.com":     "TikTok",
    "linkedin.com":   "LinkedIn",
    "reddit.com":     "Reddit",
    "threads.net":    "Threads",
    "flipboard.com":  "Flipboard",
    "bluesky.social": "Bluesky",
    "mastodon.social":"Mastodon",
}

_IMPORTANT_PAGE_KWS: dict[str, str] = {
    "privacy-policy":              "Privacy Policy",
    "privacy_policy":              "Privacy Policy",
    "politique-de-confidentialite":"Privacy Policy",
    "confidentialite":             "Privacy Policy",
    "privacy":                     "Privacy Policy",
    "about-us":                    "About Us",
    "about_us":                    "About Us",
    "about":                       "About",
    "contact-us":                  "Contact Us",
    "contact_us":                  "Contact Us",
    "contact":                     "Contact",
    "terms-of-service":            "Terms of Service",
    "terms-and-conditions":        "Terms & Conditions",
    "terms":                       "Terms",
    "disclaimer":                  "Disclaimer",
    "affiliate-disclosure":        "Affiliate Disclosure",
    "disclosure":                  "Disclosure",
    "advertise":                   "Advertise",
    "advertising":                 "Advertising",
    "cookie-policy":               "Cookie Policy",
    "cookies":                     "Cookie Policy",
    "dmca":                        "DMCA",
    "faq":                         "FAQ",
    "sitemap":                     "Sitemap",
    "media-kit":                   "Media Kit",
    "press":                       "Press",
    "careers":                     "Careers",
}

_WP_PLUGIN_SIGS: dict[str, str] = {
    "woocommerce":        "WooCommerce",
    "elementor":          "Elementor",
    "yoast":              "Yoast SEO",
    "rankmath":           "Rank Math SEO",
    "akismet":            "Akismet",
    "contact-form-7":     "Contact Form 7",
    "jetpack":            "Jetpack",
    "wordfence":          "Wordfence",
    "wpforms":            "WPForms",
    "mailchimp":          "Mailchimp",
    "wprocket":           "WP Rocket",
    "wp-super-cache":     "WP Super Cache",
    "w3-total-cache":     "W3 Total Cache",
    "smush":              "Smush (images)",
    "shortpixel":         "ShortPixel",
    "imagify":            "Imagify",
    "the-events-calendar":"The Events Calendar",
    "buddypress":         "BuddyPress",
    "bbpress":            "bbPress",
    "learndash":          "LearnDash LMS",
    "wpdiscuz":           "wpDiscuz",
    "loco-translate":     "Loco Translate",
    "polylang":           "Polylang",
    "wpml":               "WPML",
    "tablepress":         "TablePress",
    "gutenberg":          "Gutenberg",
    "seedprod":           "SeedProd",
    "astra":              "Astra Theme",
    "generatepress":      "GeneratePress Theme",
    "genesis":            "Genesis Framework",
    "divi":               "Divi Theme",
    "avada":              "Avada Theme",
    "flatsome":           "Flatsome Theme",
    "kadence":            "Kadence Theme",
}


def analyze_site_deep(url: str) -> dict:
    """
    Comprehensive site intelligence scan via local proxy (Flask backend).
    Returns:
      monetization  – list of detected ad/affiliate networks
      analytics     – list of tracking/analytics tools
      important_pages – {label: absolute_url}
      social        – [{name, url}]
      wp_plugins    – list of detected WordPress plugins/themes
      robots        – {exists, disallow_count, sitemap_urls, raw}
      ads_txt       – {exists, line_count, preview}
      tech          – stack dict from infer_site_stack()
      canonical     – canonical URL if found
      favicon       – favicon URL if found
    """
    base_url = url.rstrip("/")
    parsed_base = urlparse(base_url)
    base_host   = parsed_base.netloc.lower().replace("www.", "")

    result: dict = {
        "monetization":    [],
        "analytics":       [],
        "important_pages": {},
        "social":          [],
        "wp_plugins":      [],
        "robots":          {"exists": False},
        "ads_txt":         {"exists": False},
        "tech":            {},
        "canonical":       None,
        "favicon":         None,
        "error":           None,
    }

    # ── 1. Homepage fetch ──────────────────────────────────────────
    try:
        hr   = requests.get(base_url, headers=HEADERS, timeout=25)
        html = hr.text
        low  = html.lower()
        resp_headers = dict(hr.headers)

        # Monetization
        for name, sigs in _MONETIZATION_SIGS:
            if any(s.lower() in low for s in sigs):
                result["monetization"].append(name)

        # Analytics
        for name, sigs in _ANALYTICS_SIGS:
            if any(s.lower() in low for s in sigs):
                result["analytics"].append(name)

        # WordPress plugins (via wp-content/plugins/SLUG path)
        for slug, label in _WP_PLUGIN_SIGS.items():
            if slug in low:
                result["wp_plugins"].append(label)

        # Tech stack
        dummy_result: dict = {}
        result["tech"] = infer_site_stack(html, base_url, dummy_result, resp_headers)

        soup = BeautifulSoup(html, "lxml")

        # Canonical
        can = soup.find("link", rel="canonical")
        if can and can.get("href"):
            result["canonical"] = can["href"].strip()

        # Favicon
        fav = soup.find("link", rel=lambda r: r and "icon" in str(r).lower())
        if fav and fav.get("href"):
            result["favicon"] = urljoin(base_url, fav["href"])

        # Walk all <a> tags → important pages + social
        seen_labels: set  = set()
        seen_social:  set  = set()
        for a in soup.find_all("a", href=True):
            href = (a.get("href") or "").strip()
            if not href or href.startswith("#") or href.startswith("javascript:") or href.startswith("mailto:"):
                continue
            abs_href = urljoin(base_url, href)
            p        = urlparse(abs_href)
            domain   = p.netloc.lower().replace("www.", "")
            path_low = p.path.lower().rstrip("/")

            # Social
            for sdom, sname in _SOCIAL_DOMAINS.items():
                if sdom in domain and abs_href not in seen_social:
                    seen_social.add(abs_href)
                    result["social"].append({"name": sname, "url": abs_href})
                    break

            # Important pages — same-domain only
            if domain == base_host or domain == "":
                for kw, label in _IMPORTANT_PAGE_KWS.items():
                    if kw in path_low and label not in seen_labels:
                        seen_labels.add(label)
                        result["important_pages"][label] = abs_href
                        break

        # Also check footer / nav text links for important pages we might have missed
        for el in soup.find_all(["footer", "nav"]):
            for a in el.find_all("a", href=True):
                href     = (a.get("href") or "").strip()
                link_txt = a.get_text(strip=True).lower()
                if not href or href.startswith("#"):
                    continue
                abs_href = urljoin(base_url, href)
                p        = urlparse(abs_href)
                domain   = p.netloc.lower().replace("www.", "")
                path_low = p.path.lower().rstrip("/")
                if domain not in (base_host, ""):
                    continue
                for kw, label in _IMPORTANT_PAGE_KWS.items():
                    if (kw in path_low or kw.replace("-", " ") in link_txt) and label not in seen_labels:
                        seen_labels.add(label)
                        result["important_pages"][label] = abs_href
                        break

    except Exception as exc:
        result["error"] = str(exc)

    # ── 2. robots.txt ──────────────────────────────────────────────
    try:
        rb = requests.get(f"{base_url}/robots.txt", headers=HEADERS, timeout=12)
        if rb.status_code == 200 and "text" in rb.headers.get("content-type", "text"):
            txt = rb.text
            result["robots"] = {
                "exists":        True,
                "raw":           txt[:4000],
                "disallow_count": txt.lower().count("disallow:"),
                "sitemap_urls":  re.findall(r"(?im)^sitemap:\s*(\S+)", txt),
            }
    except Exception:
        pass

    # ── 3. ads.txt — parse domains & merge into monetization ──────
    try:
        ad = requests.get(f"{base_url}/ads.txt", headers=HEADERS, timeout=12)
        if ad.status_code == 200 and len(ad.text) < 200_000:
            raw_lines = ad.text.splitlines()
            data_lines = [l.strip() for l in raw_lines if l.strip() and not l.startswith("#")]

            # Extract first-column domain from each record (format: domain, pub-id, rel[, cert])
            seen_domains: set  = set()
            seen_networks: set = set(result["monetization"])  # already found from HTML
            networks_from_ads:  list = []
            raw_domains:        list = []

            for line in data_lines:
                parts = [p.strip() for p in line.split(",")]
                if not parts:
                    continue
                domain = parts[0].lower().replace("www.", "")
                if not domain or domain in seen_domains:
                    continue
                seen_domains.add(domain)
                raw_domains.append(domain)

                # Try exact match first, then suffix match
                label = _ADS_TXT_DOMAIN_MAP.get(domain)
                if label is None:
                    for known_dom, known_label in _ADS_TXT_DOMAIN_MAP.items():
                        if domain.endswith("." + known_dom) or domain == known_dom:
                            label = known_label
                            break
                if label is None:
                    # Fallback: use the domain itself (strip TLD) as label
                    label = domain.split(".")[0].capitalize()

                if label not in seen_networks:
                    seen_networks.add(label)
                    networks_from_ads.append(label)

            # Merge: ads.txt networks extend the monetization list
            result["monetization"].extend(networks_from_ads)

            result["ads_txt"] = {
                "exists":            True,
                "line_count":        len(data_lines),
                "preview":           ad.text[:800],
                "networks":          networks_from_ads,
                "unique_domains":    raw_domains,
            }
    except Exception:
        pass

    return result
