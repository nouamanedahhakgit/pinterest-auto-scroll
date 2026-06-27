"""
AI-powered scraper using Groq API (llama-3.3-70b-versatile).
Makes scraping work on ANY website — not just WordPress.

Phase 1 (quick):  AI analyzes listing pages → discovers all posts, categories, pagination
Phase 2 (deep):   AI extracts full structured content from each post → blocks JSON + HTML
"""
from __future__ import annotations
import json
import re
import time
import xml.etree.ElementTree as ET
import requests
try:
    from curl_cffi import requests as curl_cffi_requests
except ImportError:
    curl_cffi_requests = None  # optional: browser TLS fingerprint for strict CDNs (e.g. sitemap shards)
from bs4 import BeautifulSoup, Comment
try:
    import bs4
    _orig_bs4_init = bs4.BeautifulSoup.__init__
    def _patched_bs4_init(self, markup="", features=None, *args, **kwargs):
        try:
            _orig_bs4_init(self, markup, features, *args, **kwargs)
        except bs4.FeatureNotFound:
            fallback = "html.parser"
            if features == "lxml-xml":
                fallback = "xml" if "xml" in bs4.builder.builders else "html.parser"
            _orig_bs4_init(self, markup, fallback, *args, **kwargs)
    bs4.BeautifulSoup.__init__ = _patched_bs4_init
except Exception:
    pass
from urllib.parse import urlparse, urljoin

GROQ_BASE = "https://api.groq.com/openai/v1"
OR_BASE   = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "llama-3.3-70b-versatile"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}
# Sitemaps are often fetched stricter than HTML; some hosts block generic clients.
SITEMAP_HEADERS = {
    **HEADERS,
    "Accept": "application/xml, text/xml, application/rss+xml, */*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
TIMEOUT = 30
SITEMAP_FETCH_TIMEOUT = 45


class ScrapeCancelled(Exception):
    """User stopped discovery mid-flight; carries merged sitemap rows so far."""

    def __init__(self, entries: list, cat_names: dict):
        self.entries = list(entries or [])
        self.cat_names = dict(cat_names or {})
        super().__init__("cancelled")


# ─── HTML cleaner ─────────────────────────────────────────────────────────────

_NOISE_TAGS = ["script", "style", "noscript", "svg", "path", "symbol",
               "iframe", "canvas", "video", "audio", "source", "track"]

_NOISE_CLASS_RE = re.compile(
    r"\b(navigation|header|footer|sidebar|widget|comment|advertisement|"
    r"ads?[-_]|sharing|social[-_]share|newsletter|subscribe|popup|modal|cookie|"
    r"related[-_]posts?|recommended|suggested|site[-_]menu|breadcrumb|pagination[-_]wrap|"
    r"author-bio|tag-cloud)\b", re.I
)


def clean_html_for_ai(html: str, max_chars: int = 60000, *, preserve_pin_attrs: bool = False) -> str:
    """
    Strip all noise from raw HTML so the AI only sees meaningful content.
    Returns a compact string safe to embed in a prompt.
    If preserve_pin_attrs is True, keep data-pin* attributes (Pinterest widgets) while stripping other data-* noise.
    """
    soup = BeautifulSoup(html, "lxml")

    # Remove noise tags
    for tag in soup.find_all(_NOISE_TAGS):
        tag.decompose()

    # Remove HTML comments
    for c in soup.find_all(string=lambda t: isinstance(t, Comment)):
        c.extract()

    # Remove noisy elements by class / id
    for el in soup.find_all(True):
        if not el.attrs:
            continue
        combined = " ".join(el.get("class", [])) + " " + (el.get("id") or "")
        if _NOISE_CLASS_RE.search(combined):
            el.decompose()

    # Keep only body
    body = soup.find("body") or soup

    # Stringify and strip inline styles / event handlers
    text = str(body)
    text = re.sub(r'\s+style="[^"]*"', "", text)
    text = re.sub(r'\s+on\w+="[^"]*"', "", text)
    if preserve_pin_attrs:
        # Strip data-* except data-pin… (Pinterest save widgets)
        text = re.sub(r'\s+data-(?!pin)[a-z0-9\-]+="[^"]*"', "", text)
    else:
        text = re.sub(r'\s+data-[a-z\-]+="[^"]*"', "", text)

    # Collapse whitespace
    text = re.sub(r"\n\s*\n+", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)

    return text[:max_chars]


def _head_snippet_for_pinterest(html: str, max_chars: int = 4500) -> str:
    """OG/Twitter/Pinterest meta from <head> for pin-style context (title/image/description)."""
    try:
        soup = BeautifulSoup(html, "lxml")
        head = soup.find("head")
        if not head:
            return ""
        bits: list[str] = []
        for tag in head.find_all(["meta", "title", "link"]):
            if tag.name == "meta":
                prop = (tag.get("property") or "").lower()
                name = (tag.get("name") or "").lower()
                if prop.startswith("og:") or "pinterest" in name or name in (
                    "description", "twitter:image", "twitter:title", "twitter:description",
                ):
                    bits.append(str(tag))
            elif tag.name == "title":
                bits.append(str(tag))
        out = "\n".join(bits)
        return out[:max_chars]
    except Exception:
        return ""


# ─── JSON extractor ───────────────────────────────────────────────────────────

def extract_json(text: str):
    """Extract JSON from AI response that may have markdown fences or extra prose."""
    # Direct parse
    try:
        return json.loads(text.strip())
    except Exception:
        pass

    # Markdown code block
    m = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass

    # First JSON object / array in the string
    for pat in [r"\{[\s\S]+\}", r"\[[\s\S]+\]"]:
        m = re.search(pat, text)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return {}


# Default instructions for Reskin / multi-model benchmark (food & recipe sites).
DEFAULT_RESKIN_TEMPLATE = """You are a professional food blogger, copywriter, and SEO expert.

Your task is to rewrite and reskin the following recipe article to make it better than the original in every way.

🎯 Goals:
- Make the content feel 100% human-written (not AI-like)
- Improve clarity, flow, and readability
- Keep it simple, engaging, and appetizing
- Make it more structured and easier to follow
- Enhance it for SEO (Google + Pinterest friendly)
- Avoid repetition and remove unnecessary filler
- Keep all important cooking information accurate

✨ Style requirements:
- Natural, friendly, food-blog tone (like a real chef or food blogger)
- Use short, clear sentences
- Add emotional appeal (make the reader crave it)
- Use headings that are catchy and clean
- Make instructions super easy to follow step-by-step
- Add small "pro tips" where useful (but don't overdo it)

🔥 Optimization rules:
- Improve the recipe flow (fix any confusing steps)
- Reorder sections only when the JSON block order allows — respect the same keys/positions
- Make ingredients clean and scannable
- Make tips more practical and useful
- Improve titles and headings to be more clickable and compelling
- Remove redundancy and repeated ideas

📌 Content shape (aim for this across the blocks you rewrite):
- Strong title and intro where those blocks exist
- "Why you'll love this" energy in the opening where it fits
- Ingredients (clean format)
- Step-by-step instructions (crystal clear)
- Tips & tricks, variations, storage, FAQ — enrich those blocks when present

⚠️ Important:
- Do NOT shorten the content too much vs each original segment.
- Keep it detailed but clearer, smoother, and more professional than the original.
- The reskin theme and voice should feel clearly better than the original: warmer, clearer, more trustworthy.

Technical note: You rewrite one JSON value per key. Apply this voice consistently so the full article feels like one cohesive food-blog piece (SEO-friendly, Pinterest-friendly phrasing where natural)."""


# ─── Grok client ─────────────────────────────────────────────────────────────

class GroqClient:
    def __init__(self, api_key: str, model: str = DEFAULT_MODEL):
        self.api_key = api_key
        self.model = model

    # ── Low-level call ────────────────────────────────────────────────────────

    def _chat(self, system: str, user: str, max_tokens: int = 4000, temperature: float = 0.1, timeout: int | None = None) -> str:
        _to = 90 if timeout is None else timeout
        r = requests.post(
            f"{GROQ_BASE}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user",   "content": user},
                ],
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
            timeout=_to,
        )
        data = r.json()

        # Groq returns errors as JSON with status 200 sometimes
        if r.status_code != 200 or "error" in data:
            err = data.get("error", {})
            msg = err.get("message", r.text[:300]) if isinstance(err, dict) else str(err)
            raise RuntimeError(f"Groq API error {r.status_code}: {msg}")

        choices = data.get("choices")
        if not choices:
            raise RuntimeError(f"Groq returned no choices: {str(data)[:200]}")

        return choices[0]["message"]["content"]

    # ── Test connection ───────────────────────────────────────────────────────

    def test(self) -> tuple[bool, str]:
        try:
            r = requests.post(
                f"{GROQ_BASE}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": "Say OK"}],
                    "max_tokens": 5,
                },
                timeout=15,
            )
            if r.status_code == 200:
                return True, "Connected ✓"
            if r.status_code == 401:
                return False, "Invalid API key"
            return False, f"HTTP {r.status_code}: {r.text[:200]}"
        except Exception as e:
            return False, str(e)


    # ─── CSS / theme generation models ──────────────────────────────────────
    CSS_MODEL = "qwen/qwen3-32b"  # overridden by OpenRouterClient

    # ── Phase 1: analyze a listing / archive page ─────────────────────────────

    def analyze_listing(self, html: str, url: str) -> dict:
        """
        Given the HTML of any listing/archive/homepage, extract every visible
        post card and the overall site structure.

        Returns:
        {
          site_name, site_description, site_type,
          posts: [{title, url, date, excerpt, featured_image, categories[]}],
          categories: [{name, url}],
          pagination_next_url
        }
        """
        clean = clean_html_for_ai(html, max_chars=40000)

        system = (
            "You are an expert web scraping AI. You analyze any website HTML and extract "
            "structured data. Always reply with ONLY valid JSON — no markdown, no explanation."
        )
        user = f"""Analyze this webpage and extract ALL visible content items (posts, articles, recipes, products, etc.).

Page URL: {url}

HTML:
{clean}

Return ONLY this JSON (no markdown, no text before or after):
{{
  "site_name": "name from logo/title tag",
  "site_description": "tagline or meta description",
  "site_type": "recipe_blog|news|ecommerce|portfolio|general_blog|magazine|forum|other",
  "posts": [
    {{
      "title": "item title",
      "url": "ABSOLUTE URL to the full item page",
      "date": "YYYY-MM-DD or empty string",
      "excerpt": "description or empty string",
      "featured_image": "ABSOLUTE image URL or null",
      "categories": ["Category Name 1", "Category Name 2"]
    }}
  ],
  "categories": [
    {{"name": "Category Name", "url": "ABSOLUTE URL or empty string"}}
  ],
  "pagination_next_url": "ABSOLUTE URL of the next page or null"
}}

Rules:
- Extract EVERY visible card/item on the page — be exhaustive
- Convert all relative URLs to absolute using base: {url}
- If pagination exists (next page button/link), include it in pagination_next_url
- Categories should be from the nav menu or tag/category labels on cards"""

        try:
            raw = self._chat(system, user, max_tokens=3000)
            result = extract_json(raw)
            return result if isinstance(result, dict) else {}
        except Exception as e:
            return {"error": str(e)}

    def analyze_pinterest_share_context(self, html: str, page_url: str, request_timeout: int | None = None) -> dict:
        """
        Small-token pass: locate Pinterest save/pin UI and infer pin-style metadata
        (image, title, description, pin URL if visible). Intended for a few cached pages only.
        """
        head_part = _head_snippet_for_pinterest(html)
        body_part = clean_html_for_ai(html, max_chars=10000, preserve_pin_attrs=True)
        combined = f"=== HEAD (meta) ===\n{head_part}\n\n=== BODY (truncated) ===\n{body_part}"

        system = (
            "You read HTML fragments from a single article page. "
            "Find Pinterest sharing: Save/Pin buttons, widgets, or links to pinterest.com/pin/. "
            "Reply with ONLY valid JSON — no markdown."
        )
        user = f"""Article URL: {page_url}

HTML fragments:
{combined}

Return ONLY this JSON:
{{
  "has_pinterest_ui": true or false,
  "pin_url": "full https URL to a /pin/ page if present in the HTML, else empty string",
  "pin_image_url": "ONLY the image URL the Pinterest Save button / pin widget would use (plugin JSON pinterest_image_url, data-pin-media, or pin/create?media=). Leave EMPTY if you only see a normal article/og:image with no Pinterest-specific image — do not guess from generic hero photos.",
  "pin_title": "short title suitable for a Pinterest pin or empty",
  "pin_description": "1-2 sentences for pin description or empty",
  "notes": "brief: where you saw the pin UI (e.g. share bar, widget) or empty"
}}

Rules:
- If there is only a generic share button with no pin URL, set has_pinterest_ui true only if you see an explicit Pinterest icon/link.
- Use empty strings for unknown fields, not null.
- pin_image_url must be http(s) if set; never use plain og:image as pin_image_url unless the same URL is clearly tied to a Pinterest save control in the HTML."""

        try:
            raw = self._chat(system, user, max_tokens=700, temperature=0.05, timeout=request_timeout)
            result = extract_json(raw)
            if not isinstance(result, dict):
                return {"error": "invalid_json"}
            for k in ("pin_url", "pin_image_url", "pin_title", "pin_description", "notes"):
                if k in result and result[k] is None:
                    result[k] = ""
            if "has_pinterest_ui" not in result:
                result["has_pinterest_ui"] = False
            return result
        except Exception as e:
            return {"error": str(e)}

    # ── Build extraction schema from sample pages ─────────────────────────────

    def build_schema(self, samples: list, base_url: str) -> dict:
        """
        Analyze N sample HTML pages and return a reusable extraction schema.
        samples = [{"url": str, "html": str}, ...]
        Returns schema dict with CSS selectors, JSON-LD hints, and generation template.
        """
        cleaned_samples = []
        for s in samples[:5]:  # max 5 samples
            c = clean_html_for_ai(s["html"], max_chars=8000)
            cleaned_samples.append(f"=== PAGE: {s['url']} ===\n{c}")
        combined = "\n\n".join(cleaned_samples)

        system = (
            "You are an expert web scraping engineer. You analyze HTML pages and produce "
            "precise extraction schemas. Reply with ONLY valid JSON."
        )
        user = f"""Analyze these {len(samples)} sample pages from {base_url} and produce a reusable extraction schema.

SAMPLE PAGES:
{combined}

Return ONLY this JSON schema:
{{
  "site_type": "recipe_blog|news|ecommerce|blog|other",
  "primary_method": "jsonld|css|hybrid",
  "notes": "brief explanation of site structure",
  "css_selectors": {{
    "title": "CSS selector for article title",
    "date": "CSS selector for publish date",
    "author": "CSS selector for author name",
    "featured_image": "CSS selector for hero/featured image",
    "content": "CSS selector for main content area",
    "excerpt": "CSS selector for description/excerpt"
  }},
  "date_attr": "text|datetime|content — which attribute holds the date value",
  "image_attr": "src|data-src|data-lazy-src — which attribute holds image URL",
  "recipe": {{
    "container": "CSS selector for recipe card container (empty if no recipe)",
    "name": "CSS selector for recipe name",
    "description": "CSS selector for recipe description",
    "prep_time": "CSS selector",
    "cook_time": "CSS selector",
    "total_time": "CSS selector",
    "servings": "CSS selector",
    "ingredients": "CSS selector (each ingredient item)",
    "instructions": "CSS selector (each instruction step)",
    "nutrition": "CSS selector for nutrition info"
  }},
  "has_jsonld": true,
  "jsonld_types": ["Recipe", "Article"],
  "theme_css": "A snippet of pure CSS (no markdown) to style the extracted content to roughly match the visual vibe of the original site (colors, fonts, sizes).",
  "generation": {{
    "article_type": "recipe|article|news|product",
    "tone": "friendly|professional|casual|authoritative",
    "typical_length": "short|medium|long",
    "structure": ["introduction", "ingredients", "instructions", "tips"],
    "prompt_template": "Write a [tone] [article_type] titled '{{title}}'. Structure: [structure]. Include: [key elements the user must fill]. Style matches {base_url}."
  }}
}}"""

        try:
            raw = self._chat(system, user, max_tokens=2000)
            result = extract_json(raw)
            return result if isinstance(result, dict) else {"error": "No schema returned"}
        except Exception as e:
            return {"error": str(e)}

    # ── Phase 2: extract full structured content from a single post ───────────

    def extract_content(self, html: str, url: str) -> dict:
        """
        Given the full HTML of any individual post / article / recipe page,
        extract ALL content as structured blocks.

        Returns:
        {
          title, date, author, categories[], tags[],
          featured_image, excerpt,
          blocks: [ ...typed blocks... ]
        }
        """
        clean = clean_html_for_ai(html, max_chars=32000)

        system = (
            "You are an expert content extraction AI. You extract every piece of content "
            "from any webpage into a precise JSON structure. "
            "Be THOROUGH — capture every paragraph, heading, image, list, recipe, table, embed. "
            "Reply with ONLY valid JSON."
        )
        user = f"""Extract ALL content from this webpage into structured blocks.

URL: {url}

HTML:
{clean}

Return ONLY this JSON (no markdown, no text before or after):
{{
  "title": "page/post title",
  "date": "YYYY-MM-DD or empty",
  "author": "author name or empty",
  "categories": [{{"name": "name", "url": "absolute url or empty"}}],
  "tags": [{{"name": "name", "url": "absolute url or empty"}}],
  "featured_image": "best hero image absolute URL or null",
  "excerpt": "summary / first paragraph / meta description",
  "blocks": [

    // ── Paragraph ──
    {{"type": "paragraph", "text": "plain text content", "html": "<p>html with links/bold/italic</p>"}},

    // ── Heading ──
    {{"type": "heading", "level": 2, "text": "heading text"}},

    // ── Image ──
    {{"type": "image", "src": "absolute URL", "alt": "alt text", "caption": "caption or empty"}},

    // ── Gallery (multiple images grouped together) ──
    {{"type": "gallery", "images": [{{"src": "absolute URL", "alt": "text"}}]}},

    // ── List ──
    {{"type": "list", "ordered": false, "items": ["item 1", "item 2"]}},

    // ── Blockquote ──
    {{"type": "blockquote", "text": "quoted content"}},

    // ── Table ──
    {{"type": "table", "headers": ["Col 1", "Col 2"], "rows": [["val", "val"]]}},

    // ── Video / embed ──
    {{"type": "embed", "src": "embed iframe src URL", "provider": "youtube|vimeo|other"}},

    // ── Recipe card (if ANY recipe exists — extract it COMPLETELY) ──
    {{
      "type": "recipe_card",
      "name": "recipe name",
      "description": "recipe description",
      "prep_time":  {{"display": "20m", "minutes": "20"}},
      "cook_time":  {{"display": "30m", "minutes": "30"}},
      "total_time": {{"display": "50m", "minutes": "50"}},
      "servings": "4 servings",
      "image": "recipe image absolute URL or null",
      "ingredients": [
        {{"amount": "2", "unit": "cups", "name": "flour", "notes": "sifted or extra note"}}
      ],
      "instructions": [
        {{"step": 1, "text": "full step description", "image": "step image URL or null"}}
      ],
      "notes": "recipe notes/tips or empty",
      "nutrition": {{"summary": "Calories: 450 | Protein: 32g | Carbs: 28g | Fat: 14g"}}
    }}

  ]
}}

CRITICAL RULES:
- Include ALL blocks in reading order — do NOT skip any content
- For recipe pages, extract the COMPLETE recipe card with ALL ingredients and ALL steps
- Make every image/link URL absolute using base: {url}
- Skip navigation menus, ads, comment sections, related-posts widgets, social share buttons
- For ingredients: always split into amount + unit + name + notes separately
- For instructions: number each step, include step images if visible"""

        try:
            raw = self._chat(system, user, max_tokens=4000)
            result = extract_json(raw)
            return result if isinstance(result, dict) else {}
        except Exception as e:
            return {"error": str(e), "blocks": []}

    # ── Phase 3: AI Generation & Reskinning ───────────────────────────────────

    def generate_new_article(self, title: str, template: str) -> dict:
        """Generate a completely new structured article based purely on a title & template."""
        system = (
            "You are an expert AI content writer. You will write a brand new, highly engaging "
            "article, recipe, or post based on the requested title. "
            "You MUST return your output in the EXACT same JSON blocks format used by the extraction engine. "
            "Return ONLY valid JSON. No markdown."
        )
        
        user = f"""Please generate a NEW article for the following title:
Title: "{title}"

Follow this template/style guideline closely:
{template}

Return ONLY this JSON format (no text before or after):
{{
  "title": "Exact same title",
  "excerpt": "A catchy summary",
  "blocks": [
    {{"type": "paragraph", "text": "...", "html": "<p>...</p>"}},
    {{"type": "heading", "level": 2, "text": "..."}},
    // For recipes, include the full recipe_card block format!
    // Include whatever blocks fit the template best.
  ]
}}"""
        try:
            raw = self._chat(system, user, max_tokens=4000)
            return extract_json(raw) or {}
        except Exception as e:
            return {"error": str(e)}

    # ── Noise-block filter ────────────────────────────────────────────────────

    _NOISE_BLOCK_RE = re.compile(
        r"ezoic|ad placement|ad placeholder|disabled:|modern breadcrumb|"
        r"affiliate link|jump to recipe|"
        r"^(sidebar|recipe card|main content|featured image|article content|"
        r"recipe meta info|header section|content section|compact info row|"
        r"highlighted action buttons|small circular image|title and summary|"
        r"recipe notes|recipe card products|dessert|american|by[a-z]+|"
        # standalone UI labels from recipe-meta sections:
        r"servings?|minutes?|hours?|prep|cook|total|calories?|rating|stars?)$|"
        r"^header section\b|^recipe card\b|^compact info\b|^content section\b|"
        r"^published[a-z\s,0-9]+$|^by[a-z\s]+$",
        re.I,
    )

    def _filter_content_blocks(self, blocks: list) -> list:
        """Return only meaningful content blocks — strip UI labels, ads, breadcrumbs."""
        out = []
        for b in blocks:
            btype = b.get("type", "")
            text  = (b.get("text") or b.get("name") or "").strip()

            # Always keep rich block types
            if btype in ("recipe_card", "table", "blockquote", "gallery", "embed"):
                out.append(b)
                continue

            # Keep images with a real src
            if btype == "image" and b.get("src"):
                out.append(b)
                continue

            # Keep lists that have items
            if btype == "list" and b.get("items"):
                out.append(b)
                continue

            # For paragraphs / headings: skip noise
            if btype in ("paragraph", "heading"):
                if self._NOISE_BLOCK_RE.search(text):
                    continue
                # For paragraphs only: skip very short UI fragments ("minutes", "servings", etc.)
                # extract_article_from_cached_html already strips those via _TEXT_NOISE_RE,
                # but keep the threshold low (8) so short ingredient lines like "- Salt" survive.
                # Headings like "Ingredients" (11 chars) are valid section titles — keep them all.
                if btype == "paragraph" and len(text) < 8:
                    continue
                if btype == "heading" and len(text) < 3:
                    continue
                out.append(b)
                continue

        return out

    def reskin_article(self, original_blocks: list, template: str) -> dict:
        """
        Rewrite article text while preserving exact block structure.

        Instead of asking Groq to reproduce the full JSON block array (which it
        consistently gets wrong — wrong count, wrong types), we send a flat
        {index: text} map and receive back a flat {index: rewritten_text} map.
        We then reconstruct the full blocks ourselves, so type/count/structure
        are always guaranteed.
        """

        # ── Strip junk before sending to the AI ──────────────────────────────
        content_blocks = self._filter_content_blocks(original_blocks)
        if not content_blocks:
            return {"error": "No content blocks found to reskin — run AI extraction first"}

        # ── Build flat text map: index → text string that needs rewriting ─────
        text_map: dict[str, str] = {}
        for i, b in enumerate(content_blocks):
            btype = b.get("type", "")
            if btype in ("paragraph", "heading"):
                t = (b.get("text") or "").strip()
                if t:
                    text_map[str(i)] = t
            elif btype == "list":
                for j, item in enumerate(b.get("items") or []):
                    text_map[f"{i}.{j}"] = str(item)
            elif btype == "recipe_card":
                if b.get("description"):
                    text_map[f"{i}.desc"] = b["description"]
                for j, step in enumerate(b.get("instructions") or []):
                    t = step.get("text", "").strip()
                    if t:
                        text_map[f"{i}.s{j}"] = t

        if not text_map:
            return {"error": "No text to rewrite — run AI extraction first"}

        system = (
            "You are a professional content rewriter. "
            "You receive a JSON object where every key is a positional index and every value is a text string. "
            "You return a JSON object with the EXACT SAME KEYS and each value rewritten in fresh, natural language. "
            "Return ONLY valid JSON. No markdown, no explanation."
        )

        user = f"""Rewrite each text value completely using different vocabulary and sentence structure.

Use the Tone/Style section below as your main editorial brief (food blog, SEO, human voice, clarity). If a rule here conflicts with generic rewriting, follow Tone/Style.

RULES:
1. Return a JSON object with EXACTLY the same keys as the input — do NOT add, remove, or rename keys.
2. Rewrite each value so it reads naturally and passes plagiarism checks.
3. Preserve exact quantities (e.g. "1 cup", "2 tbsp") and exact ingredient names (e.g. "rolled oats").
   Example: "- 1 cup rolled oats" → "- Rolled oats, 1 cup" or "- 1 generous cup of rolled oats".
4. Match the approximate word count of each original value (±30%). Do NOT pad or inflate — and do not over-shorten vs the original (keep detail; make it clearer, not thinner).
5. Do NOT invent new facts, steps, or ingredients.
6. Heading values: rewrite in a fresh, engaging, clickable way (same meaning, different words).

Tone/Style: {template}

INPUT ({len(text_map)} entries):
{json.dumps(text_map, indent=2, ensure_ascii=False)}

Return ONLY the JSON object:
{{
  "0": "rewritten text...",
  "1.0": "rewritten item...",
  ...
}}"""

        try:
            raw = self._chat(system, user, max_tokens=12000)
            rewritten_map = extract_json(raw)
            if not isinstance(rewritten_map, dict) or not rewritten_map:
                return {"error": "AI returned empty or unparseable response — reskin failed. Try again."}

            # ── Rebuild full blocks from original structure + rewritten texts ──
            out_blocks = []
            for i, b in enumerate(content_blocks):
                blk = dict(b)  # shallow copy preserves type, level, src, etc.
                btype = b.get("type", "")
                key = str(i)

                if btype in ("paragraph", "heading"):
                    new_text = (rewritten_map.get(key) or "").strip()
                    if new_text:
                        blk["text"] = new_text
                        if btype == "paragraph":
                            blk["html"] = f"<p>{new_text}</p>"

                elif btype == "list":
                    new_items = []
                    for j, item in enumerate(b.get("items") or []):
                        new_items.append(rewritten_map.get(f"{i}.{j}", str(item)))
                    blk["items"] = new_items

                elif btype == "recipe_card":
                    if f"{i}.desc" in rewritten_map:
                        blk["description"] = rewritten_map[f"{i}.desc"]
                    new_instructions = []
                    for j, step in enumerate(b.get("instructions") or []):
                        new_step = dict(step)
                        step_key = f"{i}.s{j}"
                        if step_key in rewritten_map:
                            new_step["text"] = rewritten_map[step_key]
                        new_instructions.append(new_step)
                    blk["instructions"] = new_instructions

                out_blocks.append(blk)

            return {"blocks": out_blocks}

        except Exception as e:
            return {"error": str(e)}

    def judge_reskin_variants(
        self, original_title: str, original_excerpt: str, variants: list
    ) -> dict:
        """
        Compare several reskinned text excerpts to the original. variants:
        [{"model_id": str, "text": str}, ...]
        Returns JSON with rankings (1–10 scores), humanization notes, best pick.
        """
        if not variants:
            return {"error": "No variants to judge"}
        ve = []
        for v in variants:
            mid = (v.get("model_id") or "").strip()
            txt = (v.get("text") or "").strip()
            if not mid or not txt:
                continue
            ve.append({"model_id": mid, "excerpt": txt[:5000]})
        if not ve:
            return {"error": "No valid variant excerpts"}

        system = (
            "You are a senior editor and AI-writing evaluator. "
            "You compare rewritten article text to an original. "
            "Return ONLY valid JSON — no markdown fences, no commentary outside JSON."
        )
        user = f"""Article title (reference): {original_title}

ORIGINAL EXCERPT (ground truth for meaning and tone):
---
{original_excerpt[:6500]}
---

VARIANTS (each label is a different model's reskin of the same source):
{json.dumps(ve, indent=2, ensure_ascii=False)}

Evaluate each variant on:
- overall (1–10): clarity, flow, engagement, fit for a blog/recipe site
- humanization (1–10): sounds written by a person — varied rhythm, not robotic or template-y
- fidelity (1–10): preserves the original meaning, quantities, steps — no invented facts

Rules:
- Use integers 1–10 only for scores.
- In "notes", one or two sentences: strengths vs original and any AI-ish telltales.
- In "comparison_to_original", one short sentence on how it reads vs the original excerpt.
- Rank ALL variants from best to worst in "rankings" (same model_id as input).
- "best_model_id" must be exactly one of the input model_id strings.

Return JSON ONLY in this exact shape:
{{
  "rankings": [
    {{
      "model_id": "provider/model",
      "overall": 8,
      "humanization": 7,
      "fidelity": 9,
      "notes": "…",
      "comparison_to_original": "…"
    }}
  ],
  "best_model_id": "provider/model",
  "verdict": "One sentence: which model won and why for this piece."
}}
"""
        try:
            raw = self._chat(system, user, max_tokens=8000)
            data = extract_json(raw)
            if not isinstance(data, dict):
                return {"error": "Judge returned non-JSON"}
            if "rankings" not in data:
                return {"error": "Judge JSON missing rankings", "raw": raw[:500]}
            return data
        except Exception as e:
            return {"error": str(e)}

    def judge_reskin_matrix(
        self, original_title: str, original_excerpt: str, variants: list
    ) -> dict:
        """
        Matrix benchmark: each variant has variant_id, prompt_name, provider, model_id, text.
        Returns rankings with notes on original excerpt + each reskin, best_variant_id.
        """
        if not variants:
            return {"error": "No variants to judge"}
        ve = []
        for v in variants:
            vid = (v.get("variant_id") or "").strip()
            txt = (v.get("text") or "").strip()
            if not vid or not txt:
                continue
            ve.append(
                {
                    "variant_id": vid,
                    "prompt_name": (v.get("prompt_name") or "").strip(),
                    "provider": (v.get("provider") or "").strip(),
                    "model_id": (v.get("model_id") or "").strip(),
                    "excerpt": txt[:4500],
                }
            )
        if not ve:
            return {"error": "No valid variant excerpts"}

        system = (
            "You are a senior editor evaluating recipe/article rewrites. "
            "Each variant used a different PROMPT BRIEF and/or AI PROVIDER. "
            "Return ONLY valid JSON — no markdown fences."
        )
        user = f"""Article title (reference): {original_title}

ORIGINAL EXCERPT (what we're improving on):
---
{original_excerpt[:6000]}
---

VARIANTS (prompt × provider × model — each is one full reskin):
{json.dumps(ve, indent=2, ensure_ascii=False)}

First, briefly critique the ORIGINAL excerpt in "original_excerpt_critique" (tone, clarity, SEO feel, weaknesses).

Then rank EVERY variant. For each row use the exact "variant_id" string from the input.
Scores 1–10 integers: overall, humanization, fidelity.
- "note_on_original": how this reskin addresses (or not) weaknesses you saw in the source (one sentence).
- "note_on_reskin": quality of THIS rewrite in isolation — voice, clarity, appetite appeal (one sentence).
- "comparison_to_original": reader experience vs the original (one sentence).

"best_variant_id" must match one input variant_id exactly.

Return JSON ONLY:
{{
  "original_excerpt_critique": "…",
  "rankings": [
    {{
      "variant_id": "exact|from|input",
      "overall": 8,
      "humanization": 7,
      "fidelity": 9,
      "note_on_original": "…",
      "note_on_reskin": "…",
      "comparison_to_original": "…"
    }}
  ],
  "best_variant_id": "exact|from|input",
  "verdict": "Which prompt×provider combo won and why (one sentence)."
}}
"""
        try:
            raw = self._chat(system, user, max_tokens=12000)
            data = extract_json(raw)
            if not isinstance(data, dict):
                return {"error": "Judge returned non-JSON"}
            if "rankings" not in data:
                return {"error": "Judge JSON missing rankings", "raw": raw[:400]}
            return data
        except Exception as e:
            return {"error": str(e)}

    # Model used specifically for CSS/code generation
    # qwen/qwen3-32b is Groq's best coding model; vastly better CSS than the general model
    CSS_MODEL = "qwen/qwen3-32b"

    def generate_article_css(self, style_hint: str = "", sample_html: str = "") -> dict:
        """
        Ask Groq (qwen/qwen3-32b) to generate CSS that fits the ACTUAL HTML structure.
        sample_html: a real cleaned article page — the AI reads its tags/classes and
                     writes CSS that targets them directly.
        Returns {"css": "..."} or {"error": "..."}.
        """
        style_desc = style_hint.strip() or "modern clean food/recipe blog, warm tones, elegant typography"

        system = (
            "You are a senior frontend CSS engineer specialising in beautiful editorial design. "
            "You receive real HTML and write CSS that makes it look polished and professional. "
            "Output ONLY raw CSS — zero markdown, zero prose, zero code fences."
        )

        # If we have real HTML, extract visible class names and element structure to help the AI
        html_context = ""
        if sample_html.strip():
            # Pull out a compact summary: unique tags + class names present in the HTML
            try:
                from bs4 import BeautifulSoup as _BS
                _soup = _BS(sample_html[:6000], "lxml")
                seen_tags: set = set()
                seen_classes: list = []
                seen_ids: list = []
                for el in _soup.find_all(True):
                    seen_tags.add(el.name)
                    for c in (el.get("class") or []):
                        if c not in seen_classes:
                            seen_classes.append(c)
                    eid = el.get("id", "")
                    if eid and eid not in seen_ids:
                        seen_ids.append(eid)
                # Trim to reasonable size
                tags_str = ", ".join(sorted(seen_tags))
                classes_str = " ".join(f".{c}" for c in seen_classes[:60])
                ids_str = " ".join(f"#{i}" for i in seen_ids[:30])
                html_context = (
                    f"\nHTML ELEMENTS PRESENT: {tags_str}"
                    f"\nHTML CLASSES PRESENT: {classes_str}"
                    f"\nHTML IDs PRESENT: {ids_str}"
                    f"\n\nACTUAL HTML SAMPLE (first 3000 chars):\n{sample_html[:3000]}"
                )
            except Exception:
                html_context = f"\n\nACTUAL HTML SAMPLE:\n{sample_html[:3000]}"

        user = f"""You are redesigning a real blog article page. Study the HTML below and write CSS that:
1. Targets the ACTUAL class names and element types present in this HTML
2. Makes the page look stunning with the style: {style_desc}
3. Covers every visible element — body, headings, paragraphs, images, lists, links, recipe cards, etc.
{html_context}

REQUIRED CSS SECTIONS (write all of them):

@import — Pick 2 Google Fonts that match "{style_desc}" (one serif/display for headings, one sans-serif for body)

:root — Define these tokens with real values:
  --bg (page background), --surface (card/box background), --surface2 (subtle highlight)
  --text (body text), --muted (secondary text), --heading (heading colour)
  --accent (brand colour), --accent-h (accent hover, 10% darker)
  --border (divider colour), --radius: 10px, --max-w: 800px
  --font-body, --font-heading

* reset — box-sizing, margin, padding zero

body — bg, font, 17px, line-height 1.8, max-width centered with auto margins, padding 0 24px 80px

h1 — font-heading, clamp(1.9rem,5vw,3rem), line-height 1.15, heading colour, margin-bottom 16px
h2 — font-heading, clamp(1.25rem,3vw,1.7rem), heading colour, margin 44px 0 14px, padding-bottom 10px, border-bottom 3px solid accent
h3 — font-heading, 1.2rem, heading colour, margin 28px 0 10px
h4,h5,h6 — bold, heading colour, margin 20px 0 8px
p — margin-bottom 1.5em
strong — font-weight 800, heading colour
a — accent colour, no underline, border-bottom 1px transparent, transition; hover: border-bottom accent

img — max-width 100%, height auto, border-radius radius, display block, margin 28px auto, box-shadow 0 4px 20px rgba(0,0,0,.1)
img:first-of-type — width 100%, max-height 500px, object-fit cover (hero image)

ul, ol — padding-left 1.5em, margin-bottom 1.4em
li — margin-bottom 0.5em, line-height 1.7
ul li::marker — accent colour

blockquote — border-left 4px solid accent, margin 32px 0, padding 16px 24px, bg surface, border-radius 0 radius radius 0, italic, muted colour

/* Recipe / content containers — use ACTUAL class names from the HTML above */
/* Look for any class containing "recipe", "wprm", "entry", "post", "content" and style those */
[class*="recipe"] — bg surface, border 2px solid border, border-radius calc(radius+4px), padding 28px 32px, margin 40px 0, box-shadow subtle
[class*="recipe"] h2, [class*="recipe"] h3 — border-bottom none, margin-top 0
[class*="ingredients"], [class*="instructions"] — margin-top 24px
[class*="entry-content"], [class*="post-content"], .content — line-height 1.9

/* Times / meta boxes */
[class*="time"], [class*="servings"] — inline-block, bg surface2, padding 8px 14px, border-radius radius, font-size .85rem, text-align center
[class*="time"] strong, [class*="servings"] strong — display block, font-size 1.1rem, colour accent

table — width 100%, border-collapse collapse, margin 24px 0
th — bg surface, heading colour, font-weight 700, padding 10px 14px, text-align left, border-bottom 2px solid border
td — padding 10px 14px, border-bottom 1px solid border
tr:hover td — bg surface

@media (max-width: 640px) — font-size 16px, padding 0 16px 60px, h1 clamp(1.5rem,8vw,2rem), [class*="recipe"] padding 18px 16px

Write complete, real CSS values. Choose a BEAUTIFUL real colour palette — not generic grey. Be opinionated.
Output ONLY the CSS. Nothing else. /no_think"""

        saved_model = self.model
        self.model = self.CSS_MODEL
        try:
            raw = self._chat(system, user, max_tokens=6000)
        finally:
            self.model = saved_model

        # Strip Qwen3 thinking blocks (<think>…</think>) if present
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()

        # Strip any accidental markdown fences
        css = raw.strip()
        css = re.sub(r"^```[a-zA-Z]*\n?", "", css, flags=re.MULTILINE)
        css = re.sub(r"\n?```\s*$", "", css, flags=re.MULTILINE).strip()

        if not css or len(css) < 200:
            return {"error": "Model returned empty CSS — try again"}
        return {"css": css}

    # ── HTML structure reference (plain string — no f-string interpolation) ──────
    # This tells the AI the EXACT class names in the HTML so CSS targets correctly.
    # We list class names only — NO pre-filled CSS values so the AI invents everything.
    _CSS_CLASS_SKELETON = """\
━━━ EXACT HTML STRUCTURE — use these class names in your CSS ━━━

HEADER (rendered in every page):
  <header class="site-header">
    <div class="header-inner">
      <a class="site-logo" href="index.html">M</a>          <!-- logo circle -->
      <span class="site-name">Blog Name</span>
      <nav class="site-nav">
        <a href="index.html">Home</a>
        <a href="...">About</a>
        <a href="category-recipes.html">Recipes</a>
      </nav>
    </div>
  </header>

ARTICLE PAGE:
  <div class="article-layout">                <!-- 2-column grid: content | sidebar -->
    <div class="article-primary">
      <article class="article">
        <div class="article-header">
          <div class="article-cats">
            <span class="cat-badge">Desserts</span>
          </div>
          <h1 class="article-title">Recipe Title Here</h1>
          <div class="article-meta">
            <span class="article-author">By Emma</span>
            <span class="article-date">2024-03-15</span>
          </div>
        </div>
        <img class="featured-img" src="..." />
        <div class="article-content">
          <p>...</p>
          <h2>Section heading</h2>   <!-- YOUR SIGNATURE ELEMENT goes here -->
          <h3>Sub-heading</h3>
          <blockquote><p>Quote text</p></blockquote>
          <ul><li>...</li></ul>
          <ol><li>...</li></ol>
          <img src="..." />
          <table><thead><tr><th>...</th></tr></thead><tbody>...</tbody></table>
          <div class="recipe-card">
            <img class="rc-hero-img" />
            <div class="rc-head">
              <h2 class="rc-title">Recipe Name</h2>
              <p class="rc-desc">Description</p>
            </div>
            <div class="rc-times">
              <div class="rc-time-item">
                <span class="rc-time-label">Prep</span>
                <span class="rc-time-value">15 min</span>
              </div>
            </div>
            <div class="rc-section">
              <h3>Ingredients</h3>
              <ul class="rc-ingredients">
                <li>
                  <span class="rc-ing-amount">2</span>
                  <span class="rc-ing-unit">cups</span>
                  <span class="rc-ing-name">flour</span>
                </li>
              </ul>
            </div>
            <div class="rc-section">
              <h3>Instructions</h3>
              <ol class="rc-steps">
                <li class="rc-step">
                  <span class="rc-step-num">1</span>
                  <div class="rc-step-body"><p>Step text</p></div>
                </li>
              </ol>
            </div>
            <div class="rc-notes"><h4>Notes</h4><p>...</p></div>
            <div class="rc-nutrition"><h4>Nutrition</h4><p>...</p></div>
          </div>
        </div>
      </article>
      <div class="author-card">
        <div class="author-avatar">E</div>
        <div class="author-info">
          <div class="author-name">Emma</div>
          <span class="author-specialty">Food & Recipe Specialist</span>
          <p class="author-bio">Bio text...</p>
        </div>
      </div>
    </div>
    <aside class="article-sidebar">
      <div class="sidebar-widget">
        <div class="sidebar-widget-title">Recent Articles</div>
        <div class="sb-posts">
          <a class="sb-post" href="...">
            <img class="sb-post-img" />
            <div class="sb-post-info">
              <div class="sb-post-title">Title</div>
              <div class="sb-post-date">2024-03-15</div>
            </div>
          </a>
        </div>
      </div>
    </aside>
  </div>

HOMEPAGE:
  <div class="index-wrap">
    <div class="index-hero">
      <img class="hero-bg" />
      <div class="hero-overlay"></div>
      <div class="hero-content">
        <span class="hero-cat-badge">Category</span>
        <div class="hero-title">Featured Post Title</div>
        <div class="hero-meta">By Emma · Mar 15</div>
        <a class="hero-link" href="...">Read More</a>
      </div>
    </div>
    <div class="index-section-title">Latest Recipes</div>
    <div class="cat-filter">
      <button class="cat-filter-btn active">All</button>
      <button class="cat-filter-btn">Desserts</button>
    </div>
    <div class="posts-grid">
      <a class="post-card" href="...">
        <img class="post-card-img" />
        <div class="post-card-body">
          <div class="post-card-cats"><span class="cat-badge">Desserts</span></div>
          <div class="post-card-title">Post Title</div>
          <div class="post-card-excerpt">Short excerpt...</div>
          <div class="post-card-meta">By Emma · Mar 15, 2024</div>
        </div>
      </a>
    </div>
  </div>
  <div class="cat-page-title">Desserts</div>
  <div class="cat-page-desc">All dessert recipes</div>

FOOTER:
  <footer class="site-footer">
    <div class="footer-inner">
      <span class="footer-site-name">Blog Name</span>
      <nav class="footer-nav"><a href="...">About</a></nav>
      <span class="footer-copy">© 2024 Blog Name</span>
    </div>
  </footer>

━━━ NOW WRITE THE COMPLETE CSS FOR ALL THESE ELEMENTS ━━━
Write stunning, professional CSS. Be opinionated. Use your archetype's personality fully.
Make design decisions that a senior designer at a top food publication would make.
Every element should feel intentional — spacing, typography, shadows, transitions, color usage.
The result must look like a REAL website (Bon Appétit, NYT Cooking, Serious Eats level quality).
DO NOT leave any class empty. Style everything."""

    # ── Design archetypes ──────────────────────────────────────────────────────
    # Each archetype = completely distinct visual personality, not just a color swap.
    # Each archetype = a complete VISUAL PERSONALITY, not just a color swap.
    # Keys: name, personality, bg/surface/surface2/text/muted/heading/accent/accent_h/border,
    #       h_font, b_font, logo_grad, signature (a unique design detail the AI MUST implement)
    _DESIGN_ARCHETYPES = [
        dict(name="Rustic Editorial",
             personality="Like Bon Appétit meets a farmhouse diary. Warm, earthy, deeply inviting. Big bold serif headlines, cream backgrounds, terracotta accents, generous whitespace.",
             bg="#fdf6ee", surface="#fffaf4", surface2="#f5ede0",
             text="#2c1810", muted="#9a7a65", heading="#1a0e08",
             accent="#c05621", accent_h="#9c4318", border="#e8d5bf",
             h_font="Playfair Display", b_font="Source Sans 3",
             logo_grad="linear-gradient(135deg,#c05621,#e8994a)",
             signature="h2 in article gets: border-left:4px solid var(--accent); padding-left:16px; background:rgba(192,86,33,.04); border-radius:0 8px 8px 0. Blockquote: position:relative; padding-left:40px; font-style:italic; font-size:1.15em; and a huge decorative apostrophe via ::before (content:'\\\"'; font-size:5rem; color:var(--accent); opacity:.15; position:absolute; top:-20px; left:0; font-family:Georgia,serif; line-height:1). Recipe cards: solid left stripe border-left:5px solid var(--accent); background:linear-gradient(135deg,var(--surface) 0%,var(--surface2) 100%)."),
        dict(name="Midnight Luxe",
             personality="Dark mode done right. Michelin-star restaurant energy. Deep charcoal, gold accents, premium serif fonts. Sophisticated, exclusive, cinematic.",
             bg="#121212", surface="#1c1c1c", surface2="#242424",
             text="#e0e0e0", muted="#888888", heading="#ffffff",
             accent="#d4a853", accent_h="#b8902a", border="#2d2d2d",
             h_font="Cormorant Garamond", b_font="DM Sans",
             logo_grad="linear-gradient(135deg,#d4a853,#a07830)",
             signature="Header: background:rgba(18,18,18,.88); backdrop-filter:blur(20px); border-bottom:1px solid rgba(212,168,83,.2). Featured image: no border-radius (full bleed edge to edge). Post cards: background:#1c1c1c; border:1px solid #2d2d2d; on hover: border-color:var(--accent); box-shadow:0 0 0 1px var(--accent),0 20px 40px rgba(0,0,0,.5). h2 gets color:var(--accent) and a thin gold underline via ::after (display:block; content:''; width:40px; height:2px; background:var(--accent); margin-top:8px)."),
        dict(name="Modern Minimalist",
             personality="NYT Cooking meets an architect's sketchbook. Ultra-clean, high contrast, typography-forward. Headlines are massive and dramatic. Maximum whitespace. ONE accent color used sparingly.",
             bg="#ffffff", surface="#f8f8f8", surface2="#f0f0f0",
             text="#111111", muted="#777777", heading="#000000",
             accent="#e53e3e", accent_h="#c53030", border="#e0e0e0",
             h_font="Space Grotesk", b_font="Inter",
             logo_grad="linear-gradient(135deg,#111111,#444444)",
             signature="Header: background:#fff; border-bottom:3px solid #000; no blur. Article title: font-size:clamp(2.8rem,7vw,4.5rem); letter-spacing:-0.04em; line-height:1.05. Category badges: background:#111; color:#fff; border-radius:0; padding:3px 10px (square corners). Post cards on hover: border-top:3px solid var(--accent); transform:none; just the color stripe. h2: font-size:1.5rem; border-bottom:2px solid #000; padding-bottom:8px; no left border."),
        dict(name="Vibrant Wellness",
             personality="Healthline meets a yoga retreat. Fresh greens, clean whites, rounded everywhere. Optimistic, healthy, approachable. Great for health/nutrition blogs.",
             bg="#f0f9f4", surface="#ffffff", surface2="#e6f4ed",
             text="#1a3a2a", muted="#5a8a6a", heading="#0f2a1a",
             accent="#16a34a", accent_h="#15803d", border="#c8e8d5",
             h_font="DM Serif Display", b_font="Nunito",
             logo_grad="linear-gradient(135deg,#16a34a,#4ade80)",
             signature="Everything uses border-radius:20px or higher. Post cards on hover: box-shadow:0 8px 30px rgba(22,163,74,.15). Author card gets a wave-shaped decoration: border-top:4px solid var(--accent); background:linear-gradient(to bottom,rgba(22,163,74,.06),transparent). Recipe cards: background:linear-gradient(135deg,#f0fff4,#fff); border:2px solid var(--border); border-top:4px solid var(--accent); border-radius:20px; box-shadow:0 4px 20px rgba(22,163,74,.08). h2: color:var(--accent); font-size:1.6rem."),
        dict(name="Bold Purple Creative",
             personality="A graphic designer's food blog. Deep purple/violet, unexpected layout, artistic touches. Feels designed, not templated. Maximum visual impact.",
             bg="#faf5ff", surface="#ffffff", surface2="#f3e8ff",
             text="#1a0a2e", muted="#7c5aa0", heading="#0d0520",
             accent="#7c3aed", accent_h="#6d28d9", border="#ddd0ff",
             h_font="Raleway", b_font="Lato",
             logo_grad="linear-gradient(135deg,#7c3aed,#ec4899)",
             signature="Hero overlay: background:linear-gradient(135deg,rgba(124,58,237,.7) 0%,rgba(0,0,0,.6) 100%). Category filter buttons: background:linear-gradient(135deg,var(--accent),#ec4899); color:#fff; border:none on active. Post card title on hover: text-decoration:underline; text-decoration-color:var(--accent); text-underline-offset:4px. Footer: background:var(--accent); color:#fff; .footer-inner a:color:rgba(255,255,255,.75). h2: background:linear-gradient(135deg,var(--accent),#ec4899); -webkit-background-clip:text; -webkit-text-fill-color:transparent; background-clip:text (gradient text)."),
        dict(name="Coastal Fresh",
             personality="Beach house recipe blog. Ocean blues, clean whites, breezy and light. Summer vibes, fresh produce energy. Navigation feels like a sunny pier boardwalk.",
             bg="#f0f8ff", surface="#ffffff", surface2="#e0f0ff",
             text="#1a2a3a", muted="#5a7a90", heading="#0d1a2a",
             accent="#0ea5e9", accent_h="#0284c7", border="#bde0f5",
             h_font="Josefin Sans", b_font="Open Sans",
             logo_grad="linear-gradient(135deg,#0ea5e9,#06b6d4)",
             signature="Header: background:linear-gradient(to right,rgba(14,165,233,.05),rgba(6,182,212,.08)); border-bottom:2px solid rgba(14,165,233,.15). Post cards: border-left:3px solid transparent; on hover: border-left-color:var(--accent); transform:translateX(3px). Featured image: border-radius:24px; box-shadow:0 20px 60px rgba(14,165,233,.15). h2: color:var(--accent); display:flex; align-items:center; gap:10px; ::before{content:''; display:block; width:30px; height:3px; background:var(--accent); flex-shrink:0}."),
        dict(name="Warm Neo-Serif",
             personality="Sophisticated Brooklyn apartment blog. Warm neutrals, deep amber accents, bold serif/sans pairing. Editorial but personal. Award-winning typography.",
             bg="#faf8f5", surface="#ffffff", surface2="#f5f0e8",
             text="#2a2520", muted="#8a7a6a", heading="#1a1510",
             accent="#d97706", accent_h="#b45309", border="#e8ddd0",
             h_font="Libre Baskerville", b_font="Work Sans",
             logo_grad="linear-gradient(135deg,#d97706,#f59e0b)",
             signature="Article title: letter-spacing:-0.03em; font-size:clamp(2.2rem,5vw,3.5rem). Article meta gets a decorative separator: display:flex; align-items:center; gap:12px; ::before{content:''; display:block; flex:1; height:1px; background:var(--border)}. Post cards: box-shadow:0 2px 8px rgba(0,0,0,.06); border:none; border-bottom:3px solid transparent; on hover: border-bottom-color:var(--accent); box-shadow:0 12px 30px rgba(0,0,0,.1). Blockquote: font-size:1.25em; font-style:italic; color:var(--heading); padding:24px 32px; background:var(--surface2); border-radius:12px; position:relative; ::before{content:'\\\"'; font-family:'Libre Baskerville'; font-size:6rem; color:var(--accent); opacity:.2; position:absolute; top:-10px; left:16px; line-height:1}."),
        dict(name="Retro Pop",
             personality="1970s cookbook meets Instagram. Bold retro colors, chunky shapes, coral + cream + near-black. Fun, energetic, memorable. Every element has personality.",
             bg="#fffbf0", surface="#fff5e0", surface2="#ffedc5",
             text="#1a1208", muted="#7a6050", heading="#0d0905",
             accent="#ff6b35", accent_h="#e5552a", border="#f0d5b0",
             h_font="Fraunces", b_font="Nunito Sans",
             logo_grad="linear-gradient(135deg,#ff6b35,#ffd23f)",
             signature="Post cards on hover: transform:rotate(0.8deg) translateY(-4px); box-shadow:6px 6px 0 var(--accent). Recipe cards: border:2px dashed var(--accent); border-radius:16px; background:var(--surface). Category badges: border-radius:0; background:var(--accent); color:#fff; font-weight:800. h2: font-size:1.8rem; position:relative; display:inline-block; ::after{content:''; display:block; height:4px; background:var(--accent); margin-top:4px; width:60%}. Header site-name: position:relative; ::after{content:''; position:absolute; bottom:-2px; left:0; right:0; height:3px; background:var(--accent)}."),
        dict(name="Sage Botanical",
             personality="Herbalist's kitchen blog. Muted sage greens, warm whites, almost hand-made feel. Earthy and calming. Long elegant serif type with generous leading.",
             bg="#f8faf6", surface="#ffffff", surface2="#eef2ec",
             text="#252820", muted="#708070", heading="#141810",
             accent="#5a7a52", accent_h="#4a6a42", border="#cdd9c8",
             h_font="Cormorant", b_font="Jost",
             logo_grad="linear-gradient(135deg,#5a7a52,#8aac82)",
             signature="Header: background:transparent; border-bottom:1px solid var(--border); (minimal, airy). Post card images get a subtle green tint overlay via ::after (position:absolute; inset:0; background:rgba(90,122,82,.06); pointer-events:none). Author card: outline:1px solid var(--border); outline-offset:4px; border:1px solid var(--border). Recipe cards: border-left:5px solid var(--accent); border-radius:0 12px 12px 0; background:linear-gradient(to right,rgba(90,122,82,.06),transparent). Article body: font-size:19px; line-height:2; (very generous leading — key signature of this style)."),
        dict(name="Neo Brutalist",
             personality="Bold, raw, intentionally graphic. Black borders everywhere, offset drop shadows, stark contrast. Like a high-end art zine. Maximum visual impact with minimum decoration.",
             bg="#f5f5dc", surface="#fffff0", surface2="#eeeed8",
             text="#0a0a0a", muted="#4a4a3a", heading="#000000",
             accent="#ff0055", accent_h="#cc0044", border="#0a0a0a",
             h_font="Space Grotesk", b_font="IBM Plex Mono",
             logo_grad="linear-gradient(135deg,#ff0055,#0a0a0a)",
             signature="EVERY card/container gets: border:2px solid #0a0a0a; box-shadow:4px 4px 0 #0a0a0a. On hover: transform:translate(-2px,-2px); box-shadow:6px 6px 0 #0a0a0a. Header: border-bottom:4px solid #0a0a0a; background:var(--bg). Category badges: border:2px solid #0a0a0a; border-radius:0; background:#0a0a0a; color:var(--bg). h2: border:2px solid #0a0a0a; padding:8px 16px; display:inline-block; background:var(--accent); color:#fff. Featured image: border:3px solid #0a0a0a; border-radius:0; box-shadow:8px 8px 0 #0a0a0a."),
    ]

    def generate_blog_theme(self, site_name: str, categories: list,
                             writers: list, style_hint: str = "") -> dict:
        """
        Generate a complete blog theme: CSS + HTML partials with {{PLACEHOLDER}} slots.
        Each call picks a DIFFERENT design archetype (truly random) so themes are always unique.
        Returns {"css": str, "header": str, "footer": str, "author_card": str,
                 "archetype": str} or {"error": str}.
        """
        import random
        cats_str = ", ".join(categories[:12]) if categories else "Recipes, Health, Lifestyle"

        # True random — different archetype every generate call
        arc = random.choice(self._DESIGN_ARCHETYPES)

        writers_desc = ""
        for i, w in enumerate(writers[:2]):
            writers_desc += f"  Writer {i+1}: {w.get('name','')}, specialty: {w.get('specialty','')}\n"
        if not writers_desc:
            writers_desc = "  Writer 1: Emma, food & recipe specialist\n  Writer 2: Alex, health & nutrition expert\n"

        style_override = style_hint.strip()

        system = (
            "You are the lead CSS engineer at a top-tier digital food magazine (think Bon Appétit, NYT Cooking, Serious Eats). "
            "You create stunning, award-winning blog themes that look like real professional publications — not Bootstrap templates. "
            "Your CSS is opinionated, complete, and beautiful. Every spacing choice, font size, shadow, and transition is intentional. "
            "You make BOLD design decisions: dramatic typography, rich colors, unique hover effects, magazine-quality layouts. "
            "Output ONLY valid JSON. No markdown, no prose outside JSON, no truncation."
        )

        gf_url = (
            f"https://fonts.googleapis.com/css2?family={arc['h_font'].replace(' ', '+')}:"
            f"ital,wght@0,400;0,600;0,700;0,800;1,400;1,700"
            f"&family={arc['b_font'].replace(' ', '+')}:wght@400;500;600&display=swap"
        )

        user = f"""Design a world-class blog theme for "{site_name}" — a {cats_str} blog.

━━━ CREATIVE DIRECTION ━━━
ARCHETYPE: {arc['name']}
FEEL: {arc['personality']}
{f'EXTRA REQUEST: {style_override}' if style_override else ''}

━━━ COLOR SYSTEM (use exactly these values) ━━━
Background:      {arc['bg']}
Surface cards:   {arc['surface']}
Surface alt:     {arc['surface2']}
Body text:       {arc['text']}
Muted text:      {arc['muted']}
Headings:        {arc['heading']}
Accent:          {arc['accent']}
Accent hover:    {arc['accent_h']}
Borders:         {arc['border']}

━━━ TYPOGRAPHY ━━━
Heading font: {arc['h_font']} (Google Fonts)
Body font:    {arc['b_font']} (Google Fonts)
Import:       @import url('{gf_url}');

━━━ LOGO ━━━
.site-logo background: {arc['logo_grad']}

━━━ SIGNATURE DESIGN ELEMENT (implement this exactly — it's what makes this theme unique) ━━━
{arc['signature']}

{self._CSS_CLASS_SKELETON}

━━━ OUTPUT RULES ━━━
- Write the CSS as if a senior designer is reviewing it — every class must be beautifully styled
- .article-title: make it dramatic (large font, tight line-height, distinctive weight)
- .article-content h2: MUST use the signature element above
- .post-card and .post-card:hover: must feel premium (shadow, transform, border changes)
- .site-header: sticky, visually distinct from content, not just white + border
- .recipe-card: make it look like a magazine pull-out (distinct background, nice type hierarchy)
- .cat-filter-btn and .cat-filter-btn.active: pill shape, clear active state
- ALL :hover states must have smooth transitions
- Fonts: use var(--font-heading) for all headlines, var(--font-body) for body text
- Use var() CSS variables everywhere, never hardcode colors
- Include @media(max-width:900px) and @media(max-width:640px) responsive rules

━━━ RETURN THIS EXACT JSON (no other text) ━━━
{{
  "archetype": "{arc['name']}",
  "css": "PASTE_COMPLETE_CSS_HERE",
  "header": "<header class=\\"site-header\\"><div class=\\"header-inner\\"><a class=\\"site-logo\\" href=\\"index.html\\">{{{{SITE_INITIAL}}}}</a><span class=\\"site-name\\">{{{{SITE_NAME}}}}</span><nav class=\\"site-nav\\">{{{{NAV_LINKS}}}}</nav></div></header>",
  "footer": "<footer class=\\"site-footer\\"><div class=\\"footer-inner\\"><span class=\\"footer-site-name\\">{{{{SITE_NAME}}}}</span><nav class=\\"footer-nav\\">{{{{NAV_LINKS}}}}</nav><span class=\\"footer-copy\\">© {{{{YEAR}}}} {{{{SITE_NAME}}}}</span></div></footer>",
  "author_card": "<div class=\\"author-card\\"><div class=\\"author-avatar\\">{{{{AUTHOR_INITIAL}}}}</div><div class=\\"author-info\\"><div class=\\"author-name\\">{{{{AUTHOR_NAME}}}}</div><span class=\\"author-specialty\\">{{{{AUTHOR_SPECIALTY}}}}</span><p class=\\"author-bio\\">{{{{AUTHOR_BIO}}}}</p></div></div>"
}}"""

        saved_model = self.model
        self.model = self.CSS_MODEL
        try:
            raw = self._chat(system, user, max_tokens=12000, temperature=0.9)
        except Exception as e:
            return {"error": f"AI request failed: {e}"}
        finally:
            self.model = saved_model

        # Strip Qwen3 / thinking-model blocks (<think>…</think>) if present
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw, flags=re.MULTILINE)
        raw = re.sub(r"\n?```\s*$", "", raw, flags=re.MULTILINE).strip()

        result = extract_json(raw)
        if not isinstance(result, dict):
            # Last-ditch: maybe the model returned only CSS, not JSON
            if raw.strip().startswith(("{", "{")):
                return {"error": f"AI returned invalid JSON for blog theme (raw: {raw[:200]})"}
            return {"error": f"AI returned invalid JSON for blog theme (raw: {raw[:200]})"}
        if "css" not in result:
            return {"error": f"AI response missing 'css' key — keys returned: {list(result.keys())}"}
        return result





# ─── OpenRouter client (OpenAI-compatible, many models) ──────────────────────

class OpenRouterClient(GroqClient):
    """
    Thin wrapper over the OpenRouter API (OpenAI-compatible endpoint).
    Inherits ALL generation methods from GroqClient; only overrides _chat
    to hit openrouter.ai and send required headers.
    CSS_MODEL is overridden to use claude-sonnet on OR by default.
    """

    CSS_MODEL = "anthropic/claude-sonnet-4-5"   # overridden per request anyway

    def __init__(self, api_key: str, model: str = "openrouter/free"):
        super().__init__(api_key, model)
        self._or_api_key = api_key

    def _chat(self, system: str, user: str, max_tokens: int = 4000, temperature: float = 0.1, timeout: int | None = None) -> str:
        # Free models on OpenRouter can queue for several minutes — use a long timeout
        if timeout is not None:
            _timeout = timeout
        else:
            _timeout = 360 if ":free" in self.model else 180
        r = requests.post(
            f"{OR_BASE}/chat/completions",
            headers={
                "Authorization":  f"Bearer {self._or_api_key}",
                "Content-Type":   "application/json",
                "HTTP-Referer":   "https://wp-scraper.local",
                "X-Title":        "WP Scraper",
            },
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user",   "content": user},
                ],
                "temperature": temperature,
                "max_tokens":  max_tokens,
            },
            timeout=_timeout,
        )
        data = r.json()
        if r.status_code != 200 or "error" in data:
            err = data.get("error", {})
            msg = err.get("message", r.text[:300]) if isinstance(err, dict) else str(err)
            raise RuntimeError(f"OpenRouter API error {r.status_code}: {msg}")
        choices = data.get("choices")
        if not choices:
            raise RuntimeError(f"OpenRouter returned no choices: {str(data)[:200]}")
        return choices[0]["message"]["content"]

    def test(self) -> tuple[bool, str]:
        try:
            self._chat("You are a test.", "Say OK", max_tokens=5)
            return True, "OpenRouter connected"
        except Exception as e:
            return False, str(e)


# ─── Sitemap / RSS discovery ─────────────────────────────────────────────────

def _slug_to_title(url: str) -> str:
    """Convert a URL slug to a readable title. e.g. /healthy-chicken-soup → Healthy Chicken Soup"""
    slug = url.rstrip("/").split("/")[-1]
    slug = re.sub(r"[-_]", " ", slug)
    slug = re.sub(r"\.\w+$", "", slug)  # strip extension
    return slug.title()


def _extract_sitemap_image_from_url_tag(url_tag) -> str:
    """
    Google image extension in sitemaps; XML namespaces rename tags (image:image vs {ns}image).
    Drupal URL-only sitemaps often omit images — returns '' then.
    """
    # Explicit WordPress / Google style
    img_tag = url_tag.find("image:image") or url_tag.find("image")
    if img_tag:
        img_loc = img_tag.find("image:loc") or img_tag.find("loc")
        if img_loc:
            t = img_loc.get_text("").strip()
            if t.startswith("http"):
                return t
    # Namespace-agnostic: any <*loc> under an <*image*> parent
    for loc in url_tag.find_all(True):
        n = (loc.name or "")
        if "loc" not in n:
            continue
        parent = loc.parent
        if parent is None or parent is url_tag:
            continue
        pn = (parent.name or "")
        if "image" in pn and "loc" not in pn:
            t = loc.get_text("").strip()
            if t.startswith("http"):
                return t
    return ""


def _xml_local_name(tag) -> str:
    """Strip XML namespace from BeautifulSoup tag name ({ns}url → url)."""
    if not tag or not getattr(tag, "name", None):
        return ""
    return tag.name.split("}")[-1]


def _xml_find_all(soup, local: str):
    """find_all that works with default-namespace sitemaps (AllRecipes, many CDNs)."""
    el = soup.find_all(local)
    if el:
        return el
    return [t for t in soup.find_all(True) if _xml_local_name(t) == local]


def _xml_find_first(parent, local: str):
    if parent is None:
        return None
    hit = parent.find(local)
    if hit:
        return hit
    for c in parent.find_all(True):
        if _xml_local_name(c) == local:
            return c
    return None


# Default-namespace sitemaps (AllRecipes, many CDNs): BeautifulSoup sometimes matches
# zero <url>/<sitemap> nodes on large files; ElementTree uses explicit {ns} tags.
_SM_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
_SM = "{%s}" % _SM_NS


def _etree_sitemap_index_child_locs(xml_text: str) -> list:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    out = []
    for sm in root.findall(f".//{_SM}sitemap"):
        loc = sm.find(f"{_SM}loc")
        if loc is not None and loc.text:
            out.append(loc.text.strip())
    return out


def _etree_urlset_entries(xml_text: str) -> list:
    """Return [(url, lastmod_yyyy_mm_dd), ...] for each <url> in a urlset."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    out = []
    for url_el in root.findall(f".//{_SM}url"):
        loc = url_el.find(f"{_SM}loc")
        if loc is None or not loc.text:
            continue
        u = loc.text.strip()
        lastmod = ""
        lm = url_el.find(f"{_SM}lastmod")
        if lm is not None and lm.text:
            lastmod = lm.text.strip()[:10]
        out.append((u, lastmod))
    return out


def _yoast_sitemap_bucket(source_url: str) -> str:
    """
    Yoast SEO splits sitemaps by type (post-sitemap*.xml, page-sitemap.xml, …).
    Used to route URLs into posts vs pages and to skip taxonomy-only files.
    """
    if not source_url:
        return "post"
    base = source_url.split("/")[-1].lower()
    if "page-sitemap" in base:
        return "page"
    if "post-sitemap" in base:
        return "post"
    if any(
        x in base
        for x in (
            "category-sitemap",
            "post_tag-sitemap",
            "tag-sitemap",
            "author-sitemap",
        )
    ):
        return "taxonomy"
    return "post"


def _infer_sitemap_content_kind(title: str, url: str, bucket: str) -> str:
    if bucket == "page":
        return "page"
    low = ((title or "") + " " + (url or "")).lower()
    if any(
        x in low
        for x in (
            "food map",
            "travel map",
            "city map",
            "restaurant map",
            "map (use it on the go",
        )
    ):
        return "travel_or_map"
    # AllRecipes: index shards sitemap_1.xml … hold /recipe/123/slug/ and root slug stories
    # https://www.allrecipes.com/sitemap.xml → child urlsets
    if "allrecipes.com" in low:
        try:
            parts = [p for p in urlparse(url).path.strip("/").split("/") if p]
            if len(parts) >= 2 and parts[0] == "recipe" and parts[1].isdigit():
                return "recipe"
            if len(parts) >= 2 and parts[0] == "recipe":
                return "recipe"
            if len(parts) == 1 and parts[0]:
                return "article"
        except Exception:
            pass
    return "article"


def _fetch_sitemap_urls(
    base_url: str,
    progress_cb=None,
    cancel_check=None,
    checkpoint_cb=None,
    checkpoint_every: int = 10,
) -> list:
    """
    Parse sitemap.xml (including sitemap indexes and image sitemaps).
    Returns list of dicts: [{url, title, featured_image, date, categories}]
    Also extracts categories from /category/ sitemap entries.

    Merges every sitemap source (main index, robots.txt URLs, fallbacks). Previously we
    stopped after the first file with URLs — robots often lists a tiny "recent" sitemap
    first, so large sites (e.g. allrecipes) only got ~48 URLs instead of the full index.
    """
    from scraper import normalize_post_url, _path_is_listing_hub

    bu = base_url.rstrip("/")
    post_urls = []
    seen_page_urls: set = set()  # normalized URL → avoid duplicates across merged sitemaps
    cat_names: dict = {}   # slug → name, collected from category sitemap entries
    # Only mark URLs as fetched after a successful 200 + body so failed/blocked
    # requests can be retried (e.g. outer loop + AllRecipes shard fallback).
    checked_ok = set()
    MAX_URLS = 250_000  # safety cap for huge indexes

    def fetch(url: str) -> str | None:
        if url in checked_ok:
            return None

        ul = url.lower()
        is_ar_sitemap_shard = "allrecipes.com" in ul and bool(
            re.search(r"/sitemap_\d+\.xml$", ul)
        )

        def _fetch_via_curl_cffi() -> str | None:
            """TLS + HTTP/2 fingerprint like Chrome; often fixes HTTP 402/403 from edge CDNs."""
            if curl_cffi_requests is None:
                return None
            hdrs = {**SITEMAP_HEADERS, "Referer": bu + "/", "Accept-Encoding": "gzip, deflate, br"}
            # Impersonate strings depend on curl_cffi version; try common aliases.
            for imp in ("chrome124", "chrome120", "chrome110", "chrome"):
                try:
                    r = curl_cffi_requests.get(
                        url,
                        headers=hdrs,
                        impersonate=imp,
                        timeout=SITEMAP_FETCH_TIMEOUT,
                    )
                    body = (r.text or "").strip()
                    if r.status_code in (200, 206) and body:
                        if progress_cb and is_ar_sitemap_shard:
                            progress_cb(
                                f"📄   ↳ OK via browser TLS ({imp}): {len(body):,} bytes ← {url}"
                            )
                        checked_ok.add(url)
                        return r.text
                    if progress_cb and is_ar_sitemap_shard:
                        progress_cb(
                            f"📄   ↳ browser TLS try {imp}: HTTP {r.status_code} ← {url}"
                        )
                except Exception as e:
                    if progress_cb and is_ar_sitemap_shard:
                        progress_cb(f"📄   ↳ browser TLS {imp}: {e!s} ← {url}")
            return None

        # AllRecipes large sitemap shards often return HTTP 402 to plain `requests` (TLS mismatch).
        # Try curl_cffi first for those URLs.
        if is_ar_sitemap_shard:
            got = _fetch_via_curl_cffi()
            if got:
                return got

        # Some hosts reject one header profile but allow another.
        header_profiles = [
            {**SITEMAP_HEADERS, "Referer": bu + "/"},
            {**SITEMAP_HEADERS},
            {**HEADERS, "Accept": "application/xml,text/xml,*/*;q=0.8", "Referer": bu + "/"},
            {**HEADERS},
        ]
        last_status = None
        last_err = None
        for idx, hdrs in enumerate(header_profiles):
            try:
                r = requests.get(url, headers=hdrs, timeout=SITEMAP_FETCH_TIMEOUT)
                last_status = r.status_code
                body = (r.text or "").strip()
                if r.status_code in (200, 206) and body:
                    checked_ok.add(url)
                    return r.text
            except Exception as e:
                last_err = str(e)
            if progress_cb and "allrecipes.com/sitemap_" in ul:
                progress_cb(
                    f"📄   ↳ fetch attempt {idx + 1}/{len(header_profiles)} for {url}: "
                    f"status={last_status if last_status is not None else 'error'}"
                )

        # ── Universal curl_cffi fallback for ANY blocked sitemap ────────────────
        # Many CDNs (Dotdash Meredith, Cloudflare, etc.) block plain requests
        # but allow real Chrome TLS fingerprint. Try for any sitemap that failed.
        if last_status not in (200, 206) and curl_cffi_requests is not None:
            got = _fetch_via_curl_cffi()
            if got:
                if progress_cb:
                    progress_cb(f"📄   ↳ curl_cffi bypassed block for {url}")
                return got

        if last_status is not None and last_status not in (200, 206):
            if progress_cb:
                progress_cb(f"📄 ↳ fetch failed or blocked (HTTP {last_status}): {url}")
        elif last_err and progress_cb:
            progress_cb(f"📄 ↳ fetch error: {last_err} — {url}")
        return None

    # ── Infer category name(s) from URL path ─────────────────────────────────
    def _cats_from_url(url: str) -> list:
        """
        Best-effort: extract category hints from the URL path.
        e.g. /high-protein-snacks/peanut-butter-cups/ → ['High Protein Snacks']
        """
        from urllib.parse import urlparse
        parts = [p for p in urlparse(url).path.strip("/").split("/") if p]
        # If URL has 2+ path segments, the first is often a category
        if len(parts) >= 2:
            raw = parts[0]
            # Skip year/date segments
            if raw.isdigit() and len(raw) == 4:
                return []
            name = raw.replace("-", " ").replace("_", " ").title()
            slug = raw.lower()
            if slug not in cat_names:
                cat_names[slug] = name
            return [{"id": slug, "name": name, "slug": slug, "link": ""}]
        return []

    # ── Ordered sitemap URLs: main index FIRST, then robots, then fallbacks ──
    ordered_sitemaps: list = []
    seen_cand: set = set()

    def add_cand(u: str):
        if not u or u in seen_cand:
            return
        seen_cand.add(u)
        ordered_sitemaps.append(u)

    add_cand(bu + "/sitemap.xml")
    robots_txt = fetch(bu + "/robots.txt")
    if robots_txt:
        for line in robots_txt.splitlines():
            if line.lower().startswith("sitemap:"):
                sm = line.split(":", 1)[1].strip()
                add_cand(sm)
    for path in ["/sitemap_index.xml", "/sitemap-index.xml",
                 "/post-sitemap.xml", "/recipe-sitemap.xml", "/sitemap1.xml",
                 "/sitemap/", "/sitemap-posts.xml", "/blog-sitemap.xml",
                 "/articles-sitemap.xml", "/news-sitemap.xml"]:
        add_cand(bu + path)

    def _sitemap_fetch_priority(sm: str) -> tuple:
        """Main site index first; Google News / tiny news maps last (avoid ~50 URL cap)."""
        u = sm.lower()
        if u.endswith("/sitemap.xml") and "google" not in u and "news-sitemap" not in u:
            return (0, u)
        if "sitemap_index" in u or u.rstrip("/").endswith("sitemap_index.xml"):
            return (1, u)
        if re.search(r"/sitemap_\d+\.xml$", u) and "google" not in u:
            return (2, u)
        if "post-sitemap" in u or "recipe-sitemap" in u:
            return (3, u)
        if "google-news" in u or "news-sitemap" in u:
            return (40, u)
        return (10, u)

    ordered_sitemaps.sort(key=_sitemap_fetch_priority)

    visited_sitemaps = set()
    _child_merge_count = [0]  # sitemap index shards processed (for checkpoint cadence)

    def parse_sitemap(xml_text, source_url):
        if source_url in visited_sitemaps:
            return
        posts_before_file = len(post_urls)
        raw_lower = (xml_text or "").lower()

        # ── Sitemap index: recurse with ElementTree first — do NOT run BeautifulSoup on the
        # whole document first. Large sites (AllRecipes) have tiny index XML but multi‑MB
        # shards; if BS/lxml fails or is skipped on huge urlsets, we must still recurse from
        # the index. Previously: BS at top → return on exception → 0 children → only
        # google-news (~49 URLs) merged.
        if "sitemapindex" in raw_lower:
            child_urls = _etree_sitemap_index_child_locs(xml_text)
            if not child_urls and "<sitemap" in raw_lower:
                try:
                    soup_ix = BeautifulSoup(xml_text, "lxml-xml")
                    for sm_tag in _xml_find_all(soup_ix, "sitemap"):
                        child_loc = _xml_find_first(sm_tag, "loc")
                        if not child_loc:
                            continue
                        cu = child_loc.get_text("").strip()
                        if cu:
                            child_urls.append(cu)
                except Exception:
                    pass
            if progress_cb:
                if child_urls:
                    show = child_urls[:8]
                    tail = f" … (+{len(child_urls) - len(show)} more)" if len(child_urls) > len(show) else ""
                    progress_cb(
                        f"📄 Sitemap index: {len(child_urls)} child file(s) under {source_url} → "
                        f"{', '.join(show)}{tail}"
                    )
                else:
                    progress_cb(
                        f"⚠️ Sitemap index: no child <loc> under {source_url} "
                        f"(ET/BS found 0 — index may be blocked or wrong namespace)"
                    )
            for child_url in child_urls:
                if cancel_check and cancel_check():
                    if progress_cb:
                        progress_cb(
                            "⏹ Stop requested — keeping partial sitemap "
                            f"({len(post_urls)} URL(s) merged so far)"
                        )
                    raise ScrapeCancelled(list(post_urls), dict(cat_names))
                if not child_url or child_url in visited_sitemaps:
                    if progress_cb and child_url and child_url in visited_sitemaps:
                        progress_cb(f"📄   ↳ skip (already visited): {child_url}")
                    continue
                posts_before_child = len(post_urls)
                child_xml = fetch(child_url)
                if not child_xml:
                    if progress_cb:
                        progress_cb(
                            f"📄   ↳ fetch failed or blocked (no body): {child_url}"
                        )
                    continue
                if progress_cb:
                    progress_cb(
                        f"📄   ↳ entering child sitemap ({len(child_xml):,} bytes): {child_url}"
                    )
                parse_sitemap(child_xml, child_url)
                _child_merge_count[0] += 1
                if (
                    checkpoint_cb
                    and checkpoint_every > 0
                    and _child_merge_count[0] % checkpoint_every == 0
                    and post_urls
                ):
                    try:
                        checkpoint_cb(list(post_urls), dict(cat_names))
                    except Exception:
                        pass
                if progress_cb:
                    gained = len(post_urls) - posts_before_child
                    progress_cb(
                        f"📄   ↳ +{gained} post(s) from shard (running total {len(post_urls)})"
                    )
            if progress_cb:
                progress_cb(
                    f"📄 Sitemap index done: {source_url} → "
                    f"+{len(post_urls) - posts_before_file} post(s) from all children "
                    f"(total {len(post_urls)})"
                )
            visited_sitemaps.add(source_url)
            return

        # URL entries (non-index documents)
        bucket = _yoast_sitemap_bucket(source_url)
        if bucket == "taxonomy":
            visited_sitemaps.add(source_url)
            return
        sitemap_file = urlparse(source_url).path.strip("/").split("/")[-1] or ""

        def emit(url, url_tag, date_override):
            """Append one post from a <url> entry (BeautifulSoup tag or ET fallback)."""
            if not url:
                return
            low = url.lower()

            dedupe_key = normalize_post_url(url) or url
            if dedupe_key in seen_page_urls:
                return

            # AllRecipes: /recipes/... are category hubs (not single recipes)
            if "allrecipes.com" in low:
                try:
                    ap = [p for p in urlparse(url).path.strip("/").split("/") if p]
                    if ap and ap[0] == "recipes":
                        return
                except Exception:
                    pass

            # Collect categories from /category/ URLs (don't add as posts)
            if "/category/" in low or "/tag/" in low:
                cat_slug = url.rstrip("/").split("/")[-1]
                cat_name = cat_slug.replace("-", " ").replace("_", " ").title()
                if cat_slug and cat_slug not in cat_names:
                    cat_names[cat_slug] = cat_name
                return

            # Skip non-post URLs
            if any(x in low for x in ["/author/", "/page/",
                                       ".jpg", ".png", ".gif", ".pdf", ".xml"]):
                return
            # Skip search/feed query URLs but allow others like ?lang=
            if "?" in low and any(x in low for x in ["?s=", "?feed=", "?p=0", "?replytocom="]):
                return

            if url_tag is not None:
                featured_image = _extract_sitemap_image_from_url_tag(url_tag)
                date = ""
                lastmod = _xml_find_first(url_tag, "lastmod")
                if lastmod:
                    date = lastmod.get_text("").strip()[:10]
                title = ""
                for t_tag in ["news:title", "title"]:
                    t = url_tag.find(t_tag)
                    if t:
                        title = t.get_text("").strip()
                        break
                if not title:
                    nt = _xml_find_first(url_tag, "title")
                    if nt:
                        title = nt.get_text("").strip()
                if not title:
                    title = _slug_to_title(url)
            else:
                featured_image = ""
                date = (date_override or "")[:10]
                title = _slug_to_title(url)

            # Yoast post sitemap still lists blog archive URLs — skip as articles
            if bucket == "post" and _path_is_listing_hub(url):
                return

            categories = _cats_from_url(url)

            seen_page_urls.add(dedupe_key)
            post_urls.append({
                "url":            url,
                "title":          title,
                "featured_image": featured_image,
                "date":           date,
                "categories":     categories,
                "sitemap_bucket": bucket,
                "sitemap_file":   sitemap_file,
                "content_kind":   _infer_sitemap_content_kind(title, url, bucket),
            })

        # Urlsets: use ElementTree only (no BeautifulSoup on multi‑MB shard XML).
        # Other XML: BeautifulSoup + optional ET fallback for odd namespaces.
        is_urlset_doc = "<urlset" in raw_lower and "sitemapindex" not in raw_lower
        et_pairs = []
        url_tags = []

        if is_urlset_doc:
            et_pairs = _etree_urlset_entries(xml_text)
            for u, d in et_pairs:
                emit(u, None, d)
        else:
            try:
                soup = BeautifulSoup(xml_text, "lxml-xml")
            except Exception:
                checked_ok.discard(source_url)
                return
            url_tags = _xml_find_all(soup, "url")
            if url_tags:
                for url_tag in url_tags:
                    loc_tag = _xml_find_first(url_tag, "loc")
                    if not loc_tag:
                        continue
                    u = loc_tag.get_text("").strip()
                    emit(u, url_tag, None)
            elif "<url" in raw_lower and "sitemapindex" not in raw_lower:
                et_pairs = _etree_urlset_entries(xml_text)
                for u, d in et_pairs:
                    emit(u, None, d)

        # Mark done only after processing. If raw XML clearly has <url> but we matched none,
        # do not mark visited and drop fetch cache so a later pass can re-fetch (AllRecipes shards).
        if is_urlset_doc:
            found_tags = len(et_pairs)
        else:
            found_tags = len(url_tags) if url_tags else len(et_pairs)
        if (
            found_tags == 0
            and "<url" in raw_lower
            and "sitemapindex" not in raw_lower
        ):
            if progress_cb:
                progress_cb(
                    f"⚠️ Sitemap parse retryable: {source_url} — "
                    f"{found_tags} <url> rows, body has <url> but nothing merged"
                )
            checked_ok.discard(source_url)
            return
        if progress_cb:
            added_here = len(post_urls) - posts_before_file
            kind = "urlset" if is_urlset_doc else "mixed"
            progress_cb(
                f"📄 Sitemap file ({kind}): {source_url} — "
                f"{found_tags} row(s) in XML, +{added_here} post(s) merged "
                f"(total {len(post_urls)})"
            )
        visited_sitemaps.add(source_url)

    for sm_url in ordered_sitemaps:
        if cancel_check and cancel_check():
            if progress_cb:
                progress_cb(
                    "⏹ Stop requested — saving partial sitemap "
                    f"({len(post_urls)} URL(s))"
                )
            raise ScrapeCancelled(list(post_urls), dict(cat_names))
        if len(post_urls) >= MAX_URLS:
            if progress_cb:
                progress_cb(f"📄 Sitemap URL cap reached ({MAX_URLS}) — stopping merge")
            break
        xml = fetch(sm_url)
        xnorm = (xml or "").lower()
        if xml and ("<urlset" in xnorm or "<sitemapindex" in xnorm):
            n_before = len(post_urls)
            if progress_cb:
                progress_cb(
                    f"📄 Parsing sitemap: {sm_url} ({len(post_urls)} posts in index before this file)"
                )
            parse_sitemap(xml, sm_url)
            if progress_cb:
                progress_cb(
                    f"📄 Finished: {sm_url} → +{len(post_urls) - n_before} post(s) "
                    f"(total {len(post_urls)} in index)"
                )
        elif xml and progress_cb and sm_url.rstrip("/").endswith("/sitemap.xml"):
            preview = (xml[:100] or "").replace("\n", " ")
            progress_cb(
                f"⚠️ /sitemap.xml is not a sitemap (blocked or HTML?) — preview: {preview}…"
            )

    # AllRecipes: index is https://www.allrecipes.com/sitemap.xml → sitemap_1…4.xml urlsets.
    # If we still have almost nothing, force-fetch shards (retries if first fetch failed).
    if len(post_urls) < 80 and "allrecipes.com" in bu.lower():
        if progress_cb:
            progress_cb(
                f"📄 Few URLs merged ({len(post_urls)}) — loading AllRecipes shard files "
                f"sitemap_1.xml … sitemap_8.xml"
            )
        for n in range(1, 9):
            if cancel_check and cancel_check():
                if progress_cb:
                    progress_cb(
                        "⏹ Stop requested during AllRecipes shard pass — "
                        f"keeping {len(post_urls)} URL(s)"
                    )
                raise ScrapeCancelled(list(post_urls), dict(cat_names))
            if len(post_urls) >= MAX_URLS:
                break
            shard = f"{bu}/sitemap_{n}.xml"
            xml = fetch(shard)
            xnorm = (xml or "").lower()
            if xml and ("<urlset" in xnorm or "<sitemapindex" in xnorm):
                n_before = len(post_urls)
                if progress_cb:
                    progress_cb(
                        f"📄 Parsing shard: {shard} ({len(post_urls)} posts before shard)"
                    )
                parse_sitemap(xml, shard)
                if progress_cb:
                    progress_cb(
                        f"📄 Shard done: {shard} → +{len(post_urls) - n_before} post(s) "
                        f"(total {len(post_urls)})"
                    )
            elif xml and progress_cb:
                progress_cb(f"⚠️ Shard not a sitemap XML: {shard} (blocked or HTML?)")
            elif not xml and progress_cb:
                progress_cb(f"⚠️ Shard fetch empty/failed: {shard}")

    # ── HTML sitemap: try common human-readable sitemap pages ───────────────────
    # Many big sites (FoodNetwork, etc.) use /site/site-map or /sitemap instead of XML.
    # Always run — not just as a fallback — to catch sites that don't have XML sitemaps.
    if True:
        _HTML_SITEMAP_PATHS = [
            "/site/site-map", "/site-map", "/sitemap", "/html-sitemap",
            "/htmlsitemap", "/pages/sitemap", "/pages/site-map",
            "/all-recipes", "/all-posts", "/all-articles", "/archive",
        ]
        _base_domain = urlparse(bu).netloc.lower().lstrip("www.")

        def _html_fetch(url: str):
            """Fetch HTML page — tries curl_cffi first (TLS fingerprint), then plain requests."""
            hdrs = {**HEADERS, "Referer": bu + "/", "Accept": "text/html,*/*;q=0.8"}
            if curl_cffi_requests is not None:
                for _imp in ("chrome124", "chrome120", "chrome110"):
                    try:
                        _r2 = curl_cffi_requests.get(url, impersonate=_imp, headers=hdrs,
                                                     timeout=20, allow_redirects=True)
                        if _r2.status_code == 200 and _r2.text and "<a " in _r2.text.lower():
                            return _r2
                    except Exception:
                        continue
            return requests.get(url, headers=hdrs, timeout=15, allow_redirects=True)

        # Collection/category suffixes common on media/recipe sites
        _HUB_SUFFIXES = (
            "-recipes", "-recipe", "-foods", "-food", "-dishes", "-dish",
            "-ideas", "-meals", "-meal", "-drinks", "-drink", "-desserts",
            "-salads", "-soups", "-snacks", "-articles", "-posts", "-guides",
            "-tips", "-how-to", "-videos",
        )
        _HUB_SEGMENTS = {
            "ingredients", "cuisine", "cuisines", "technique", "techniques",
            "cooking-method", "diet", "diets", "occasion", "occasions",
            "course", "courses", "season", "holidays", "world-cuisine",
        }

        def _is_hub_url(url: str) -> bool:
            """Broader hub detection: catches /recipes/banana-bread-recipes style pages."""
            if _path_is_listing_hub(url):
                return True
            _segs2 = [s for s in urlparse(url).path.strip("/").split("/") if s]
            if not _segs2:
                return True
            _last = _segs2[-1].lower()
            # Ends with a collection suffix
            if any(_last.endswith(s) for s in _HUB_SUFFIXES):
                return True
            # Known category segment names
            if _last in _HUB_SEGMENTS or (len(_segs2) > 1 and _segs2[-2].lower() in _HUB_SEGMENTS):
                return True
            # Short path (≤2 segs) with no numeric ID — likely a category, not an article
            if len(_segs2) <= 2 and not re.search(r"\d{4,}", _last):
                return True
            return False

        def _extract_links_from_html(html_text: str, page_url: str):
            """Return (content_links, hub_links) from an HTML page."""
            _final_domain = urlparse(page_url).netloc.lower().lstrip("www.")
            _soup2 = BeautifulSoup(html_text, "lxml")
            content_links, hub_links = [], []
            for _a in _soup2.find_all("a", href=True):
                _href = (_a["href"] or "").strip()
                if not _href or _href.startswith("#") or _href.startswith("mailto:"):
                    continue
                _abs = urljoin(page_url, _href)
                _p = urlparse(_abs)
                _ld = _p.netloc.lower().lstrip("www.")
                if _ld and _ld != _base_domain and _ld != _final_domain:
                    continue
                _path_low = _p.path.lower()
                if any(x in _path_low for x in [".jpg", ".png", ".gif", ".pdf", ".xml",
                                                  "/author/", "/tag/", "/page/"]):
                    continue
                _segs = [s for s in _p.path.split("/") if s]
                if len(_segs) < 1:
                    continue
                _norm = normalize_post_url(_abs)
                if _norm in seen_page_urls:
                    continue
                _title = _a.get_text(strip=True) or _slug_to_title(_abs)
                _entry = {
                    "url": _abs, "title": _title[:200], "featured_image": "",
                    "date": "", "categories": _cats_from_url(_abs),
                    "sitemap_bucket": "html_sitemap", "sitemap_file": page_url,
                    "content_kind": _infer_sitemap_content_kind(_title, _abs, "html_sitemap"),
                }
                if _is_hub_url(_abs):
                    hub_links.append((_norm, _entry))
                else:
                    content_links.append((_norm, _entry))
            return content_links, hub_links

        _hub_queue: list = []   # category pages to crawl one level deeper

        # ── Parallel HTML sitemap probing: fetch all paths simultaneously ─────
        # Build list of paths not already attempted via XML sitemap pass
        _hs_to_probe = [bu + p for p in _HTML_SITEMAP_PATHS if (bu + p) not in seen_cand]
        for _u in _hs_to_probe:
            seen_cand.add(_u)

        _hs_responses: dict = {}   # url → response object or None
        if _hs_to_probe:
            from concurrent.futures import ThreadPoolExecutor as _HSTP, as_completed as _hs_asc

            def _probe_one(url):
                try:
                    return url, _html_fetch(url)
                except Exception:
                    return url, None

            with _HSTP(max_workers=min(len(_hs_to_probe), 8)) as _hsex:
                for _fut in _hs_asc({_hsex.submit(_probe_one, u): u for u in _hs_to_probe}):
                    try:
                        _u, _resp = _fut.result()
                        _hs_responses[_u] = _resp
                    except Exception:
                        pass

        # Process responses in original path order (so priority is preserved)
        for _hs_path in _HTML_SITEMAP_PATHS:
            if cancel_check and cancel_check():
                if progress_cb:
                    progress_cb(
                        "⏹ Stop requested during HTML sitemap pass — "
                        f"keeping {len(post_urls)} URL(s)"
                    )
                raise ScrapeCancelled(list(post_urls), dict(cat_names))
            _hs_url = bu + _hs_path
            _r = _hs_responses.get(_hs_url)
            if _r is None:
                continue
            try:
                if _r.status_code != 200 or not _r.text:
                    continue
                _final_url = getattr(_r, "url", None) or _hs_url
                _low = _r.text.lower()
                if "<urlset" in _low or "<sitemapindex" in _low:
                    continue
                if "<a " not in _low:
                    if progress_cb:
                        progress_cb(f"📄 {_hs_url}   → no links (JS-rendered or empty)")
                    continue
                if progress_cb:
                    progress_cb(f"📄 Trying HTML sitemap: {_hs_url}")
                _content, _hubs = _extract_links_from_html(_r.text, _final_url)
                _added = 0
                for _norm, _entry in _content:
                    seen_page_urls.add(_norm)
                    post_urls.append(_entry)
                    _added += 1
                for _norm, _entry in _hubs:
                    if _norm not in seen_page_urls:
                        seen_page_urls.add(_norm)
                        _hub_queue.append(_entry["url"])
                if progress_cb:
                    progress_cb(
                        f"📄   → {_added} articles + {len(_hubs)} category pages queued"
                        f" (total {len(post_urls)} posts)"
                    )
            except Exception as _e:
                if progress_cb:
                    progress_cb(f"📄 HTML sitemap {_hs_url} — error: {_e}")

        # ── Level 2: crawl category/hub pages found in HTML sitemap ──────────────
        if _hub_queue and len(post_urls) < MAX_URLS:
            if progress_cb:
                progress_cb(
                    f"📄 Crawling {len(_hub_queue)} category pages from HTML sitemap…"
                )
            for _hub_url in _hub_queue[:60]:   # cap at 60 category pages
                if len(post_urls) >= MAX_URLS:
                    break
                try:
                    _r2 = _html_fetch(_hub_url)
                    if _r2.status_code != 200 or not _r2.text:
                        continue
                    _final2 = _r2.url or _hub_url
                    _content2, _ = _extract_links_from_html(_r2.text, _final2)
                    _added2 = 0
                    for _norm2, _entry2 in _content2:
                        seen_page_urls.add(_norm2)
                        post_urls.append(_entry2)
                        _added2 += 1
                    if _added2 and progress_cb:
                        progress_cb(
                            f"📄   {_hub_url} → +{_added2} (total {len(post_urls)})"
                        )
                except Exception:
                    continue

    # Backfill categories for posts that have none, using the cat_names we collected
    if cat_names and post_urls:
        for p in post_urls:
            if not p.get("categories"):
                p["categories"] = _cats_from_url(p["url"])

    return post_urls


def _fetch_rss_urls(base_url: str, progress_cb=None) -> list:
    """Try RSS/Atom feed to get recent post URLs and titles."""
    posts = []
    for path in ["/feed", "/rss", "/rss.xml", "/atom.xml", "/feed.xml", "/blog/feed"]:
        url = base_url.rstrip("/") + path
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "lxml-xml")
            items = soup.find_all("item") or soup.find_all("entry")
            if not items:
                continue
            if progress_cb:
                progress_cb(f"📡 Found RSS feed: {url} ({len(items)} items)")
            for item in items:
                link_tag = item.find("link")
                title_tag = item.find("title")
                link = ""
                if link_tag:
                    link = link_tag.get_text("").strip() or link_tag.get("href", "").strip()
                title = title_tag.get_text("").strip() if title_tag else ""
                if link:
                    posts.append({"url": link, "title": title})
            break
        except Exception:
            pass
    return posts


# ─── Full HTML category/subcategory BFS crawler ───────────────────────────────

_HC_CAT_SEGMENTS = frozenset({
    "category", "categories", "tag", "tags", "cuisine", "cuisines",
    "ingredient", "ingredients", "technique", "techniques", "cooking-method",
    "diet", "diets", "occasion", "occasions", "course", "courses",
    "season", "seasons", "holiday", "holidays", "world-cuisine",
    "recipes", "recipe", "blog", "articles", "news", "posts",
    "meal-type", "meal-ideas", "dish-type", "food-type", "topics",
})

_HC_CAT_SUFFIXES = (
    "-recipes", "-recipe", "-foods", "-food", "-dishes", "-dish",
    "-ideas", "-meals", "-meal", "-drinks", "-drink", "-desserts",
    "-salads", "-soups", "-snacks", "-articles", "-posts", "-guides",
    "-tips", "-how-to", "-videos",
)

_HC_SKIP_SEGS = frozenset({
    "author", "authors", "login", "register", "cart", "checkout",
    "account", "profile", "search", "404", "privacy-policy", "privacy",
    "terms", "about", "contact", "sitemap", "feed", "rss",
    "wp-admin", "wp-login", "cdn-cgi", "amp",
})


def _hc_is_cat(url: str) -> bool:
    segs = [s for s in urlparse(url).path.strip("/").split("/") if s]
    if not segs:
        return True
    last = segs[-1].lower()
    for s in segs:
        if s.lower() in _HC_CAT_SEGMENTS:
            return True
    if any(last.endswith(suf) for suf in _HC_CAT_SUFFIXES):
        return True
    if len(segs) <= 2 and not re.search(r"\d{4,}", last):
        return True
    return False


def _hc_is_article(url: str, base_domain: str) -> bool:
    p = urlparse(url)
    if p.netloc.lower().lstrip("www.") != base_domain:
        return False
    segs = [s for s in p.path.strip("/").split("/") if s]
    if not segs:
        return False
    low = url.lower()
    if any(x in low for x in [".jpg", ".png", ".gif", ".pdf", ".xml",
                                "/author/", "?feed=", "?s="]):
        return False
    if any(s.lower() in _HC_SKIP_SEGS for s in segs):
        return False
    if re.search(r"\d{4,}", segs[-1]):
        return True
    if len(segs) >= 3:
        return True
    if len(segs) == 2 and not _hc_is_cat(url):
        return True
    return False


def _hc_next_page(soup, current_url: str):
    """Detect the next-page URL from common pagination patterns."""
    from urllib.parse import parse_qs, urlencode

    tag = soup.find("link", rel="next") or soup.find("a", rel="next")
    if tag and tag.get("href"):
        return urljoin(current_url, tag["href"])

    _NEXT_TEXTS = {"next", "next »", "next page", "»", "→", ">", "older posts", "load more"}
    _NEXT_CLS = {"next", "pagination-next", "next-page", "pager-next", "nav-next"}
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:")):
            continue
        cls = " ".join(a.get("class", [])).lower()
        text = a.get_text(strip=True).lower()
        if text in _NEXT_TEXTS or any(c in cls for c in _NEXT_CLS):
            return urljoin(current_url, href)

    parsed = urlparse(current_url)
    qs = parse_qs(parsed.query)
    for param in ("page", "paged", "pg", "p"):
        if param in qs:
            try:
                n = int(qs[param][0])
                qs[param] = [str(n + 1)]
                new_qs = urlencode({k: v[0] for k, v in qs.items()})
                return parsed._replace(query=new_qs).geturl()
            except (ValueError, IndexError):
                pass

    m = re.search(r"/page/(\d+)/?$", parsed.path)
    if m:
        n = int(m.group(1))
        new_path = re.sub(r"/page/\d+/?$", f"/page/{n+1}/", parsed.path)
        return parsed._replace(path=new_path).geturl()

    return None


def _html_crawl_categories(base_url: str, progress_cb=None,
                            already_seen: set = None,
                            max_categories: int = 30,
                            max_pages_per_cat: int = 3,
                            max_total: int = 1500,
                            cancel_check=None) -> list:
    """
    Full HTML BFS category crawler.
    1. Fetches homepage → extracts all nav/category links
    2. BFS each category with pagination (up to max_pages_per_cat)
    3. Recurses into subcategories (depth ≤ 3)
    Returns list of post entry dicts (same shape as _fetch_sitemap_urls).
    """
    from scraper import normalize_post_url

    bu = base_url.rstrip("/")
    base_domain = urlparse(bu).netloc.lower().lstrip("www.")
    post_urls: list = []
    seen_urls: set = already_seen if already_seen is not None else set()
    seen_cats: set = set()
    cat_queue: list = []   # (url, depth)
    MAX_DEPTH = 3

    def _cffi_get(url: str):
        hdrs = {
            **HEADERS,
            "Referer": bu + "/",
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
        }
        if curl_cffi_requests is not None:
            for imp in ("chrome124", "chrome120", "chrome110"):
                try:
                    r = curl_cffi_requests.get(url, impersonate=imp, headers=hdrs,
                                               timeout=25, allow_redirects=True)
                    if r.status_code == 200 and r.text:
                        return r
                    if r.status_code in (403, 429, 503):
                        break
                except Exception:
                    continue
        try:
            return requests.get(url, headers=hdrs, timeout=20, allow_redirects=True)
        except Exception:
            return None

    def _enqueue(url: str, depth: int):
        clean = url.split("?")[0].split("#")[0]
        if not clean.startswith("http"):
            return
        dom = urlparse(clean).netloc.lower().lstrip("www.")
        if dom and dom != base_domain:
            return
        segs = [s for s in urlparse(clean).path.strip("/").split("/") if s]
        if any(s.lower() in _HC_SKIP_SEGS for s in segs):
            return
        norm = normalize_post_url(clean) or clean
        if norm in seen_cats:
            return
        seen_cats.add(norm)
        cat_queue.append((clean, depth))

    def _add_article(url: str, title: str = "") -> bool:
        clean = url.split("#")[0]
        norm = normalize_post_url(clean) or clean
        if norm in seen_urls:
            return False
        seen_urls.add(norm)
        post_urls.append({
            "url":            clean,
            "title":          (title or _slug_to_title(clean))[:200],
            "featured_image": "",
            "date":           "",
            "categories":     [],
            "sitemap_bucket": "html_crawl",
            "sitemap_file":   "html_nav_crawl",
            "content_kind":   "article",
        })
        return True

    def _scrape_cat_page(page_url: str, depth: int) -> tuple:
        r = _cffi_get(page_url)
        if not r or r.status_code != 200 or not r.text:
            return 0, None
        soup = BeautifulSoup(r.text, "lxml")
        added = 0
        for a in soup.find_all("a", href=True):
            href = (a.get("href") or "").strip()
            if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            full = urljoin(page_url, href).split("#")[0]
            dom = urlparse(full).netloc.lower().lstrip("www.")
            if dom and dom != base_domain:
                continue
            if any(x in full.lower() for x in [".jpg", ".png", ".gif", ".pdf"]):
                continue
            if re.search(r"/page/\d+/?", full.lower()):
                continue  # pagination handled separately
            if _hc_is_cat(full) and depth < MAX_DEPTH:
                _enqueue(full, depth + 1)
            elif _hc_is_article(full, base_domain):
                title = a.get_text(strip=True) or ""
                if _add_article(full, title):
                    added += 1
        return added, _hc_next_page(soup, page_url)

    # ── Step 1: homepage → seed categories ────────────────────────────────────
    if progress_cb:
        progress_cb(f"🕷 HTML crawler starting: {bu}/")
    r0 = _cffi_get(bu + "/") or _cffi_get(bu)
    if r0 and r0.status_code == 200 and r0.text:
        soup0 = BeautifulSoup(r0.text, "lxml")
        # Priority containers: nav, header menus, category widgets
        containers = (
            soup0.find_all("nav") +
            soup0.find_all(True, class_=re.compile(
                r"\b(menu|nav|navigation|categories|header.?nav|main.?menu|primary.?menu)\b", re.I)) +
            ([soup0.find("header")] if soup0.find("header") else [])
        )
        seeded: set = set()
        for container in containers:
            if not container:
                continue
            for a in container.find_all("a", href=True):
                href = (a.get("href") or "").strip()
                if not href or href.startswith(("#", "javascript:", "mailto:")):
                    continue
                full = urljoin(bu, href)
                norm = normalize_post_url(full.split("#")[0]) or full
                if norm in seeded:
                    continue
                seeded.add(norm)
                _enqueue(full, 0)
        # Also collect obvious category links from the whole page
        for a in soup0.find_all("a", href=True):
            href = (a.get("href") or "").strip()
            if not href or href.startswith(("#", "javascript:", "mailto:")):
                continue
            full = urljoin(bu, href)
            if _hc_is_cat(full):
                _enqueue(full, 0)
        if progress_cb:
            progress_cb(f"🕷 Seeded {len(cat_queue)} category pages from homepage nav")
    else:
        for path in ["/recipes", "/blog", "/articles", "/category", "/food"]:
            _enqueue(bu + path, 0)
        if progress_cb:
            progress_cb(f"🕷 Homepage unavailable — seeded {len(cat_queue)} fallback paths")

    # ── Step 2: BFS through categories ────────────────────────────────────────
    cats_done = 0
    while cat_queue and cats_done < max_categories and len(post_urls) < max_total:
        if cancel_check and cancel_check():
            if progress_cb:
                progress_cb(
                    "⏹ HTML category crawl stopped by user — "
                    f"keeping {len(post_urls)} article link(s) found so far"
                )
            return post_urls
        cat_url, depth = cat_queue.pop(0)
        cats_done += 1
        total_added = 0
        page_url = cat_url
        if progress_cb:
            progress_cb(
                f"🕷 [{cats_done}] depth={depth} {cat_url} "
                f"(queue={len(cat_queue)}, found={len(post_urls)})"
            )
        for page_num in range(1, max_pages_per_cat + 1):
            if len(post_urls) >= max_total:
                break
            added, next_url = _scrape_cat_page(page_url, depth)
            total_added += added
            if not next_url or next_url == page_url:
                break
            if not added and page_num > 1:
                break  # empty page — stop pagination
            page_url = next_url
            time.sleep(0.25)
        if total_added and progress_cb:
            progress_cb(f"🕷   → +{total_added} articles (total {len(post_urls)})")

    if progress_cb:
        progress_cb(
            f"🕷 HTML crawl done: {len(post_urls)} articles from {cats_done} category pages"
        )
    return post_urls


# ─── Category hub discovery (nav / taxonomy → listing pages → recipe URLs) ───

def _is_category_hub_url(full: str, base_url: str) -> bool:
    """True for taxonomy / category listing URLs, not single articles."""
    from urllib.parse import urlparse

    low = full.lower()
    if "allrecipes.com" in low:
        p = urlparse(full).path
        if "/recipes/" in p:
            return True
        if any(x in low for x in ("recipes-a-z", "ingredients-a-z", "cuisine-a-z")):
            return True
        return False
    path = urlparse(full).path.lower()
    return "/category/" in path or "/tag/" in path


def extract_category_hub_urls_from_homepage(html: str, base_url: str, max_hubs: int = 120) -> list:
    """
    Collect hub links from nav + taxonomy blocks (e.g. mntl-header-nav, mntl-taxonomy-nodes).
    Complements sitemap: category trees expose many hubs that each list recipes.
    """
    from urllib.parse import urlparse, urljoin
    from scraper import normalize_post_url

    soup = BeautifulSoup(html, "lxml")
    bu = urlparse(base_url).netloc.replace("www.", "")
    seen: set = set()
    out: list = []

    def consider(href: str):
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            return
        full = urljoin(base_url, href)
        if urlparse(full).netloc.replace("www.", "") != bu:
            return
        if not _is_category_hub_url(full, base_url):
            return
        k = normalize_post_url(full.split("#")[0].split("?")[0])
        if not k or k in seen:
            return
        seen.add(k)
        out.append(full.split("#")[0].split("?")[0])

    for sel in (
        "nav a[href]", "header a[href]",
        ".mntl-header-nav a[href]", ".mntl-taxonomy-nodes a[href]",
        '[class*="taxonomy"] a[href]', '[class*="header-nav"] a[href]',
        '[class*="nav__"] a[href]',
    ):
        for a in soup.select(sel):
            consider(a.get("href"))

    if len(out) < 10:
        for a in soup.find_all("a", href=True):
            consider(a.get("href"))

    return out[:max_hubs]


def extract_recipe_urls_from_listing_html(html: str, page_url: str, base_url: str) -> list:
    """Pull individual recipe/article URLs from a category listing page."""
    from urllib.parse import urlparse, urljoin
    from scraper import normalize_post_url

    soup = BeautifulSoup(html, "lxml")
    bu = urlparse(base_url).netloc.replace("www.", "")
    out: set = set()

    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href or href.startswith("#"):
            continue
        full = urljoin(page_url, href)
        if urlparse(full).netloc.replace("www.", "") != bu:
            continue
        low = full.lower()
        full_clean = full.split("#")[0]
        path = urlparse(full_clean).path
        if "allrecipes.com" not in low:
            continue
        if path.startswith("/recipe/") and not path.startswith("/recipes/"):
            k = normalize_post_url(full_clean) or full_clean
            out.add(k)

    return list(out)


# ─── AI-powered site crawl (non-WP or no REST API) ───────────────────────────

def _canonical_seen_url(url: str, base_url: str = "") -> str:
    """Resolve relative URLs and normalize for seen_urls (listing pages, pagination)."""
    from scraper import normalize_post_url
    from urllib.parse import urljoin

    u = (url or "").strip()
    if not u:
        return ""
    if not u.startswith(("http://", "https://")):
        if u.startswith("//"):
            u = "https:" + u
        elif u.startswith("/") and base_url:
            u = urljoin(base_url, u)
        else:
            return ""
    return normalize_post_url(u)


def _make_post_from_url(url: str, title: str, all_posts: list, seen_urls: set,
                        cat_map: dict, item: dict = None, base_url: str = "") -> bool:
    """Build a post dict from a URL and add to all_posts. Returns True if added."""
    from scraper import safe_filename, normalize_post_url
    from urllib.parse import urlparse, urljoin

    url = (url or "").strip()
    if not url:
        return False
    # Resolve relative URLs using base_url
    if base_url and not url.startswith(("http://", "https://")):
        if url.startswith("//"):
            url = "https:" + url
        elif url.startswith("/"):
            url = urljoin(base_url, url)
        else:
            return False  # skip ambiguous relative URLs

    url = normalize_post_url(url)
    if not url:
        return False

    # Skip non-post content (narrow /page/ — only WP archive pagination, not "/page" inside slugs)
    low = url.lower()
    if re.search(r"/page/\d{1,6}(?:/|$|\?)", low):
        return False
    if any(x in low for x in ["/author/", "/tag/", "/wp-admin/",
                                ".jpg", ".png", ".gif", ".pdf", ".xml",
                                "?feed=", "?s=", "?p=0"]):
        return False
    # AllRecipes: /recipes/... hubs are category listings, not single recipes
    if "allrecipes.com" in low:
        try:
            parts = [p for p in urlparse(url).path.strip("/").split("/") if p]
            if parts and parts[0] == "recipes":
                return False
        except Exception:
            pass
    if url in seen_urls:
        return False

    # Must be on the same domain (ignore www / trailing slash mismatches)
    if base_url:
        try:
            bu = urlparse(normalize_post_url(base_url))
            pu = urlparse(url)
            def _h(n):
                n = (n or "").lower()
                return n[4:] if n.startswith("www.") else n
            if bu.netloc and pu.netloc and _h(bu.netloc) != _h(pu.netloc):
                return False
        except Exception:
            pass

    seen_urls.add(url)

    slug = url.rstrip("/").split("/")[-1] or f"post-{len(all_posts)}"
    post_id = abs(hash(url)) % 99999999

    item_cats = []
    if item:
        for cname in item.get("categories", []) or []:
            if not cname or not isinstance(cname, str):
                continue
            cname = cname.strip()
            if not cname:
                continue
            cslug = re.sub(r"[^\w]", "-", cname.lower()).strip("-")
            item_cats.append({"id": cslug, "name": cname, "slug": cslug, "link": ""})
            if cslug not in cat_map:
                cat_map[cslug] = {"id": cslug, "name": cname, "slug": cslug, "count": 0, "link": ""}
            cat_map[cslug]["count"] += 1

    src = "ai"
    if item:
        if item.get("source"):
            src = item["source"]
        elif item.get("discovery"):
            d = item["discovery"]
            if d.get("sitemap_bucket") == "page":
                src = "sitemap-page"
            elif d.get("sitemap_bucket"):
                src = "sitemap"

    row = {
        "id":             post_id,
        "slug":           slug,
        "title":          (item or {}).get("title", "") or title,
        "excerpt":        (item or {}).get("excerpt", ""),
        "link":           url,
        "date":           (item or {}).get("date", ""),
        "status":         "publish",
        "categories":     item_cats,
        "tags":           [],
        "featured_image": (item or {}).get("featured_image"),
        "filename":       safe_filename(post_id, slug),
        "source":         src,
    }
    if item and item.get("discovery"):
        row["discovery"] = item["discovery"]
    all_posts.append(row)
    return True


def _posts_from_sitemap_entries(entries: list, base_url: str) -> tuple[list, list]:
    """Turn raw sitemap merge rows into post dicts + category list (for checkpoints)."""
    posts: list = []
    seen: set = set()
    cat_map: dict = {}
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        cats_raw = entry.get("categories") or []
        cat_names: list = []
        for c in cats_raw:
            if isinstance(c, dict) and c.get("name"):
                cat_names.append(str(c["name"]).strip())
            elif isinstance(c, str) and c.strip():
                cat_names.append(c.strip())
        _make_post_from_url(
            entry.get("url", ""),
            entry.get("title") or "",
            posts,
            seen,
            cat_map,
            item={
                "title":          entry.get("title", ""),
                "featured_image": entry.get("featured_image", ""),
                "date":           entry.get("date", ""),
                "categories":     cat_names,
                "excerpt":        "",
                "discovery": {
                    "sitemap_bucket": entry.get("sitemap_bucket", "post"),
                    "sitemap_file":   entry.get("sitemap_file", ""),
                    "content_kind":   entry.get("content_kind", "article"),
                },
            },
            base_url=base_url,
        )
    return posts, list(cat_map.values())


def discover_posts_sitemap_rss(base_url: str, progress_cb=None) -> dict:
    """
    Collect post URLs via three complementary strategies (all always run):
      1. XML sitemaps (sitemap.xml / index / shards)
      2. HTML category BFS crawler (nav → categories → subcategories → articles)
      3. RSS/Atom feed (last resort)
    The HTML crawler ensures complete discovery even when sitemaps are blocked or absent.
    """
    all_posts: list = []
    all_pages: list = []
    seen_urls: set = set()
    cat_map: dict = {}
    errors: list = []

    # ── 1. XML sitemap ─────────────────────────────────────────────────────────
    if progress_cb:
        progress_cb("📄 Checking sitemap.xml (Yoast-style index + post/page sitemaps)…")
    sitemap_urls = _fetch_sitemap_urls(base_url, progress_cb)
    for entry in sitemap_urls:
        bucket = entry.get("sitemap_bucket") or "post"
        target = all_pages if bucket == "page" else all_posts
        sk = entry.get("content_kind") or "article"
        disc = {
            "sitemap_bucket": bucket,
            "sitemap_file": entry.get("sitemap_file", ""),
            "content_kind": sk,
        }
        _make_post_from_url(
            entry["url"],
            entry["title"],
            target,
            seen_urls,
            cat_map,
            item={
                "title": entry["title"],
                "featured_image": entry.get("featured_image", ""),
                "date": entry.get("date", ""),
                "categories": [c["name"] for c in entry.get("categories", [])],
                "excerpt": "",
                "discovery": disc,
            },
            base_url=base_url,
        )
    if progress_cb:
        progress_cb(f"📄 XML sitemap: {len(all_posts)} posts found so far")

    # ── 2. HTML category BFS crawler (always runs, fills gaps) ────────────────
    if progress_cb:
        progress_cb("🕷 Starting HTML category crawler (nav → categories → articles)…")
    # Tighter limits if we already found posts in sitemap
    max_cats = 5 if len(seen_urls) >= 10 else 30
    max_pages = 2 if len(seen_urls) >= 10 else 3
    html_entries = _html_crawl_categories(
        base_url,
        progress_cb=progress_cb,
        already_seen=set(seen_urls),   # pass a copy so crawler can track its own additions
        max_categories=max_cats,
        max_pages_per_cat=max_pages,
    )
    html_added = 0
    for entry in html_entries:
        if _make_post_from_url(
            entry["url"], entry["title"], all_posts, seen_urls, cat_map,
            item={"title": entry["title"], "featured_image": "", "date": "",
                  "categories": [], "excerpt": "",
                  "discovery": {"sitemap_bucket": "html_crawl",
                                "sitemap_file": "html_nav_crawl",
                                "content_kind": "article"}},
            base_url=base_url,
        ):
            html_added += 1
    if progress_cb:
        progress_cb(f"🕷 HTML crawler added {html_added} new posts (total {len(all_posts)})")

    # ── 3. RSS feed (if still nothing) ────────────────────────────────────────
    if not all_posts:
        if progress_cb:
            progress_cb("📡 Checking RSS feed…")
        for rp in _fetch_rss_urls(base_url, progress_cb):
            _make_post_from_url(
                rp.get("url", ""),
                rp.get("title", ""),
                all_posts,
                seen_urls,
                cat_map,
                base_url=base_url,
            )

    from scraper import dedupe_posts_by_link

    return {
        "site_info": {},
        "posts": dedupe_posts_by_link(all_posts),
        "pages": dedupe_posts_by_link(all_pages),
        "categories": list(cat_map.values()),
        "tags": [],
        "media": [],
        "errors": errors,
    }


def _dns_or_network_failure_hint(errors: list) -> str | None:
    """
    If discovery found 0 posts but errors look like DNS / no route to host,
    return a short user-facing hint (otherwise None).
    """
    text = " ".join(str(e) for e in (errors or []))
    if not text.strip():
        return None
    low = text.lower()
    if "getaddrinfo failed" in text or "name or service not known" in low:
        return (
            "⚠️ DNS/network: this computer could not resolve or reach the hostname "
            "(getaddrinfo). Check internet, DNS settings, VPN, and firewall — not a lack "
            "of categories on the target site."
        )
    if "temporary failure in name resolution" in low:
        return (
            "⚠️ DNS failure (name resolution). Check connectivity and DNS before scraping."
        )
    return None


def crawl_site_with_ai(
    base_url: str,
    grok: GroqClient,
    progress_cb=None,
    max_pages: int = 150,
    cancel_check=None,
    checkpoint_cb=None,
) -> dict:
    """
    Crawl ANY website using AI to understand structure.
    Priority: sitemap.xml (full index + shards) → hub nav → AI for site info / gaps → RSS → AI HTML crawl
    """
    all_posts: list = []
    seen_urls: set = set()
    cat_map: dict = {}
    site_info: dict = {}
    errors: list = []
    sitemap_urls: list = []
    discovery_cancelled = False

    def _emit_discovery_snapshot(cancelled_val: bool):
        """Write partial/full discovery state (used for Stop + periodic saves)."""
        if not checkpoint_cb:
            return
        from scraper import dedupe_posts_by_link

        try:
            checkpoint_cb(
                {
                    "url":            base_url,
                    "is_wordpress":   False,
                    "api_available":  False,
                    "ai_assisted":    True,
                    "cancelled":      cancelled_val,
                    "posts":          dedupe_posts_by_link(all_posts),
                    "pages":          [],
                    "categories":     list(cat_map.values()),
                    "tags":           [],
                    "media":          [],
                    "site_info":      dict(site_info),
                    "errors":         list(errors),
                }
            )
        except Exception:
            pass

    def _sitemap_checkpoint(entries, _cm_unused=None):
        if not checkpoint_cb:
            return
        posts, cats = _posts_from_sitemap_entries(entries, base_url)
        try:
            checkpoint_cb(
                {
                    "url":            base_url,
                    "is_wordpress":   False,
                    "api_available":  False,
                    "ai_assisted":    True,
                    "cancelled":      False,
                    "posts":          posts,
                    "pages":          [],
                    "categories":     cats,
                    "tags":           [],
                    "media":          [],
                    "site_info":      dict(site_info),
                    "errors":         list(errors),
                }
            )
        except Exception:
            pass

    # ── 1. Homepage HTML (needed for hub nav + later AI site info) ────────────
    if progress_cb:
        progress_cb(f"🌐 Fetching homepage: {base_url}")
    try:
        r = requests.get(base_url, headers=HEADERS, timeout=TIMEOUT)
        homepage_html = r.text if r.status_code == 200 else ""
    except Exception as e:
        homepage_html = ""
        errors.append(f"Homepage fetch: {e}")

    # ── 2. Sitemap FIRST (large sites: AllRecipes index → sitemap_1…4.xml) ───
    #    Running this before homepage AI avoids capping discovery at ~48 links from one listing.
    if progress_cb:
        progress_cb("📄 Scanning sitemap.xml (index + all child sitemaps)…")
    try:
        sitemap_urls = _fetch_sitemap_urls(
            base_url,
            progress_cb,
            cancel_check=cancel_check,
            checkpoint_cb=_sitemap_checkpoint if checkpoint_cb else None,
            checkpoint_every=10,
        )
    except ScrapeCancelled as sc:
        sitemap_urls = sc.entries
        discovery_cancelled = True
        errors.append(
            "Discovery stopped during sitemap merge — partial URL list kept."
        )
        for slug, name in (sc.cat_names or {}).items():
            if slug not in cat_map:
                cat_map[slug] = {
                    "id": slug, "name": name, "slug": slug, "count": 0, "link": "",
                }
        if progress_cb:
            progress_cb(
                f"⏹ Partial sitemap: {len(sitemap_urls)} URL row(s) — merging into index…"
            )
        if checkpoint_cb and sitemap_urls:
            _sitemap_checkpoint(sitemap_urls, sc.cat_names)

    if sitemap_urls:
        imgs_found = sum(1 for e in sitemap_urls if e.get("featured_image"))
        if progress_cb:
            progress_cb(
                f"📄 Sitemap merged {len(sitemap_urls)} URLs"
                + (f" · {imgs_found} with images" if imgs_found else "")
                + " — adding posts…"
            )
        for entry in sitemap_urls:
            _make_post_from_url(
                entry["url"], entry["title"], all_posts, seen_urls, cat_map,
                item={
                    "title":          entry["title"],
                    "featured_image": entry.get("featured_image", ""),
                    "date":           entry.get("date", ""),
                    "categories":     [
                        c["name"]
                        for c in (entry.get("categories") or [])
                        if isinstance(c, dict) and c.get("name")
                    ],
                    "excerpt":        "",
                    "discovery": {
                        "sitemap_bucket": entry.get("sitemap_bucket", "post"),
                        "sitemap_file": entry.get("sitemap_file", ""),
                        "content_kind": entry.get("content_kind", "article"),
                    },
                },
                base_url=base_url,
            )
        if progress_cb:
            progress_cb(f"✅ After sitemap: {len(all_posts)} posts in index")

    sitemap_rich = len(sitemap_urls) >= 40

    if discovery_cancelled:
        if progress_cb:
            progress_cb(
                f"✅ Crawl stopped by user after sitemap phase: {len(all_posts)} posts kept"
            )
        _emit_discovery_snapshot(True)
        from scraper import dedupe_posts_by_link

        return {
            "site_info":  site_info,
            "posts":      dedupe_posts_by_link(all_posts),
            "pages":      [],
            "categories": list(cat_map.values()),
            "tags":       [],
            "media":      [],
            "errors":     errors,
            "cancelled":  True,
        }

    # ── 2b. HTML category BFS crawler (always runs — fills gaps left by sitemaps) ─
    if progress_cb:
        progress_cb("🕷 HTML category crawler starting (nav → categories → subcategories → articles)…")
    # Tighter limits if sitemap was rich
    max_cats = 5 if (sitemap_rich or len(seen_urls) >= 10) else 30
    max_pages = 2 if (sitemap_rich or len(seen_urls) >= 10) else 3
    html_entries = _html_crawl_categories(
        base_url,
        progress_cb=progress_cb,
        already_seen=set(seen_urls),
        max_categories=max_cats,
        max_pages_per_cat=max_pages,
        cancel_check=cancel_check,
    )
    html_added = 0
    for entry in html_entries:
        if _make_post_from_url(
            entry["url"], entry["title"], all_posts, seen_urls, cat_map,
            item={"title": entry["title"], "featured_image": "", "date": "",
                  "categories": [], "excerpt": "",
                  "discovery": {"sitemap_bucket": "html_crawl",
                                "sitemap_file": "html_nav_crawl",
                                "content_kind": "article"}},
            base_url=base_url,
        ):
            html_added += 1
    if progress_cb:
        progress_cb(f"🕷 HTML crawler added {html_added} new posts (total {len(all_posts)})")

    if cancel_check and cancel_check():
        discovery_cancelled = True
        errors.append(
            "Discovery stopped during HTML category crawl — partial list kept."
        )
        if progress_cb:
            progress_cb("⏹ Stop requested — skipping remaining discovery steps")
        _emit_discovery_snapshot(True)
        from scraper import dedupe_posts_by_link

        return {
            "site_info":  site_info,
            "posts":      dedupe_posts_by_link(all_posts),
            "pages":      [],
            "categories": list(cat_map.values()),
            "tags":       [],
            "media":      [],
            "errors":     errors,
            "cancelled":  True,
        }

    # ── 3. Homepage AI: branding + listing links only when sitemap is thin ──
    if homepage_html:
        if progress_cb:
            progress_cb("🤖 AI reading site branding & structure…")
        try:
            analysis = grok.analyze_listing(homepage_html, base_url)
            if analysis and isinstance(analysis, dict) and "error" not in analysis:
                site_info = {
                    "name":        analysis.get("site_name", ""),
                    "description": analysis.get("site_description", ""),
                    "url":         base_url,
                    "site_type":   analysis.get("site_type", "unknown"),
                }
                if not sitemap_rich:
                    if progress_cb:
                        progress_cb("🤖 Sitemap has few URLs — AI extracting links from homepage…")
                    for item in analysis.get("posts", []) or []:
                        if not item or not isinstance(item, dict):
                            continue
                        _make_post_from_url(
                            (item.get("url") or "").strip(), "", all_posts, seen_urls, cat_map, item,
                            base_url=base_url,
                        )
                    pnext = analysis.get("pagination_next_url")
                    _pg_count = 0
                    while pnext and _pg_count < 5:
                        if cancel_check and cancel_check():
                            if progress_cb:
                                progress_cb("⏹ Stop requested — ending homepage pagination")
                            break
                        sn = _canonical_seen_url(pnext, base_url)
                        if not sn or sn in seen_urls:
                            break
                        seen_urls.add(sn)
                        try:
                            pr = requests.get(pnext, headers=HEADERS, timeout=TIMEOUT)
                            pa = grok.analyze_listing(pr.text, pnext)
                            for _pi in (pa.get("posts", []) or []):
                                if _pi and isinstance(_pi, dict):
                                    _make_post_from_url(
                                        (_pi.get("url") or "").strip(), "",
                                        all_posts, seen_urls, cat_map, _pi,
                                        base_url=base_url,
                                    )
                            pnext = pa.get("pagination_next_url") if pa.get("posts") else None
                        except Exception:
                            break
                        _pg_count += 1
                        time.sleep(0.5)
                    if progress_cb:
                        progress_cb(f"🤖 Homepage AI listing pass done — {len(all_posts)} posts total so far")
                elif progress_cb:
                    progress_cb(
                        f"🤖 Using sitemap as primary index ({len(sitemap_urls)} URLs) — "
                        "homepage AI for branding only"
                    )
        except Exception as e:
            errors.append(f"Homepage AI: {e}")

    # ── 4. RSS feed (if sitemap gave nothing) ─────────────────────────────────
    if not all_posts:
        if progress_cb:
            progress_cb("📡 Checking RSS feed…")
        rss_posts = _fetch_rss_urls(base_url, progress_cb)
        for rp in rss_posts:
            _make_post_from_url(rp.get("url", ""), rp.get("title", ""),
                                all_posts, seen_urls, cat_map, base_url=base_url)
        if all_posts and progress_cb:
            progress_cb(f"✅ RSS gave {len(all_posts)} posts")

    # ── 5. AI HTML crawl (thin sitemap index) ──────────────────────────────────
    # If merged sitemap URLs are few (<40), the main index may be partial — e.g.
    # recipe sitemap shard blocked (403) while google-news-sitemap still returns
    # a small set.  Old gate `len(all_posts) < 10` skipped this pass for 10–39
    # URLs, so discovery stopped at an incomplete list.  Run when sitemap is not
    # "rich" and the index is still modest (cap avoids extra AI after huge HTML BFS).
    if not sitemap_rich and len(all_posts) < 500:
        if progress_cb:
            progress_cb(
                "🤖 AI crawling listing paths (/recipes, /blog, …) — "
                f"sitemap merged {len(sitemap_urls)} URL(s); "
                "filling gaps when shards are blocked or news-only…"
            )

        crawl_queue = []
        # Add common listing paths (skip ones already in seen_urls from homepage analysis)
        for fp in ["/blog", "/recipes", "/articles", "/posts", "/news",
                   "/category", "/archive", "/all-recipes", "/all-posts"]:
            u = base_url.rstrip("/") + fp
            su = _canonical_seen_url(u, base_url)
            if su and su not in seen_urls:
                crawl_queue.append(u)
        # Also re-try homepage if we found nothing
        home_k = _canonical_seen_url(base_url, base_url)
        if not all_posts and home_k and home_k not in seen_urls:
            crawl_queue.insert(0, base_url)

        page_num = 0
        for crawl_url in crawl_queue:
            if cancel_check and cancel_check():
                if progress_cb:
                    progress_cb("⏹ Stop requested — ending AI listing crawl")
                break
            if page_num >= max_pages:
                break
            ck = _canonical_seen_url(crawl_url, base_url)
            if not ck or ck in seen_urls:
                continue
            seen_urls.add(ck)

            if progress_cb:
                progress_cb(f"Fetching: {crawl_url}")
            try:
                cr = requests.get(crawl_url, headers=HEADERS, timeout=TIMEOUT)
                cr.raise_for_status()
            except Exception as e:
                errors.append(f"Fetch {crawl_url}: {e}")
                continue

            if progress_cb:
                progress_cb(f"🤖 AI analyzing {crawl_url}…")

            try:
                ca = grok.analyze_listing(cr.text, crawl_url)
            except Exception as e:
                errors.append(f"AI {crawl_url}: {e}")
                continue

            if not ca or not isinstance(ca, dict) or "error" in ca:
                continue

            if not site_info:
                site_info = {
                    "name":        ca.get("site_name", ""),
                    "description": ca.get("site_description", ""),
                    "url":         base_url,
                    "site_type":   ca.get("site_type", "unknown"),
                }

            found = 0
            for item in ca.get("posts", []) or []:
                if not item or not isinstance(item, dict):
                    continue
                if _make_post_from_url(
                    (item.get("url") or "").strip(), "", all_posts, seen_urls, cat_map, item,
                    base_url=base_url,
                ):
                    found += 1

            if progress_cb:
                progress_cb(f"🤖 AI found {found} posts on {crawl_url}")

            # Follow pagination if posts found
            if found > 0:
                pnext = ca.get("pagination_next_url")
                while pnext and page_num < max_pages:
                    if cancel_check and cancel_check():
                        if progress_cb:
                            progress_cb("⏹ Stop requested — ending listing pagination")
                        break
                    sn = _canonical_seen_url(pnext, base_url)
                    if not sn or sn in seen_urls:
                        break
                    seen_urls.add(sn)
                    try:
                        pr = requests.get(pnext, headers=HEADERS, timeout=TIMEOUT)
                        pa = grok.analyze_listing(pr.text, pnext)
                        pf = 0
                        for item in (pa.get("posts", []) or []):
                            if item and isinstance(item, dict):
                                if _make_post_from_url(
                                    (item.get("url") or "").strip(), "",
                                    all_posts, seen_urls, cat_map, item,
                                    base_url=base_url,
                                ):
                                    pf += 1
                        if progress_cb:
                            progress_cb(f"🤖 Pagination: {pf} posts from {pnext}")
                        pnext = pa.get("pagination_next_url") if pf > 0 else None
                    except Exception:
                        break
                    page_num += 1
                    time.sleep(0.5)
            page_num += 1
            time.sleep(0.5)

    if progress_cb:
        progress_cb(f"✅ Crawl complete: {len(all_posts)} posts found")
        if len(all_posts) == 0:
            hint = _dns_or_network_failure_hint(errors)
            if hint:
                progress_cb(hint)

    from scraper import dedupe_posts_by_link

    all_posts = dedupe_posts_by_link(all_posts)

    return {
        "site_info":  site_info,
        "posts":      all_posts,
        "pages":      [],
        "categories": list(cat_map.values()),
        "tags":       [],
        "media":      [],
        "errors":     errors,
        "cancelled":  False,
    }
