"""
STEP 10 - Local API & CLI for Domain Quick/Bulk Scrape
======================================================

Flask API and CLI scanner that runs standalone in this project,
importing the scraper engine directly from C:\\Users\\leno\\Documents\\GitHub\\scrap_any_blog
without duplicating any files. It connects to Google Sheets via your Apps Script
Web App to fetch websites and update rows in-place.

Run as Flask API:
  python 10_domain_quick_scrape_api.py

Run as CLI Bulk Scraper:
  python 10_domain_quick_scrape_api.py --run             # Scrape only pending websites
  python 10_domain_quick_scrape_api.py --runjobforall    # Scrape all websites
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import requests
from flask import Flask, jsonify, request, send_file

# Paths
BASE = os.path.dirname(os.path.abspath(__file__))
SITES_FILE = os.path.join(BASE, "domain_quick_scrape_sites.json")
RESULTS_DIR = os.path.join(BASE, "_DOMAIN_QUICK_SCRAPE_API")
WEBAPP_FILE = os.path.join(BASE, "google_sheets_webapp.json")
ENV_PATH = os.path.join(BASE, ".env")
SETTINGS_FILE = os.path.join(BASE, "settings.json")  # Keep for fallback if needed

DEFAULT_SETTINGS = {
    "quick_scrape_ai_provider": "local",
    "quick_scrape_groq_model": "llama-3.3-70b-versatile",
    "quick_scrape_openrouter_model": "openai/gpt-4.1-mini",
    "groq_api_key": "",
    "openrouter_api_key": "",
}

app = Flask(__name__)

# Jobs & Bulk Sync Statuses
jobs: Dict[str, dict] = {}
jobs_lock = threading.Lock()

bulk_job = {
    "status": "idle",
    "total_sites": 0,
    "scanned_sites": 0,
    "current_site": "",
    "log": [],
    "created_at": 0.0,
    "cancel": False
}
bulk_lock = threading.Lock()
db_write_lock = threading.Lock()

# Inject scrap_any_blog into sys.path to import its scraper
BLOG_DIR = r"C:\Users\leno\Documents\GitHub\scrap_any_blog"
if BLOG_DIR not in sys.path:
    sys.path.insert(0, BLOG_DIR)

from scraper import scrape_site
from ai_scraper import GroqClient, OpenRouterClient

# ─── Database Connection Wrapper ──────────────────────────────────────────────

class RowWrapper(dict):
    """Support dictionary keys and list-style integer index access."""
    def __init__(self, data_dict, data_tuple):
        super().__init__(data_dict)
        self.tuple = data_tuple

    def __getitem__(self, key):
        if isinstance(key, int):
            return self.tuple[key]
        return super().__getitem__(key)

class CursorWrapper:
    """Wraps cursors to return RowWrapper objects for MySQL, or pass-through sqlite3.Row rows."""
    def __init__(self, cursor, is_mysql):
        self.cursor = cursor
        self.is_mysql = is_mysql
        self.cols = [desc[0] for desc in cursor.description] if cursor.description else []

    def __iter__(self):
        return self

    def __next__(self):
        row = next(self.cursor)
        if self.is_mysql:
            return RowWrapper(dict(zip(self.cols, row)), row)
        return row

    def fetchone(self):
        row = self.cursor.fetchone()
        if not row:
            return None
        if self.is_mysql:
            return RowWrapper(dict(zip(self.cols, row)), row)
        return row

    def fetchall(self):
        rows = self.cursor.fetchall()
        if self.is_mysql:
            return [RowWrapper(dict(zip(self.cols, r)), r) for r in rows]
        return rows

class DBWrapper:
    """Unified wrapper around SQLite and MySQL connections."""
    def __init__(self, is_mysql, conn):
        self.is_mysql = is_mysql
        self.conn = conn

    def _reconnect(self):
        """Reconnect MySQL if the connection was lost."""
        if self.is_mysql:
            try:
                self.conn.reconnect(attempts=3, delay=2)
                try:
                    cursor = self.conn.cursor()
                    cursor.execute("SET SESSION net_read_timeout = 600")
                    cursor.execute("SET SESSION net_write_timeout = 600")
                    cursor.close()
                except Exception:
                    pass
            except Exception:
                pass

    def execute(self, sql, params=None):
        if params is None:
            params = []
        if not isinstance(params, (list, tuple)):
            params = [params]
            
        if self.is_mysql:
            sql_translated = sql.replace('?', '%s')
            try:
                cursor = self.conn.cursor(buffered=True)
                cursor.execute(sql_translated, params)
                return CursorWrapper(cursor, is_mysql=True)
            except Exception as e:
                err_str = str(e).lower()
                if "lost connection" in err_str or "gone away" in err_str or "2013" in err_str or "2006" in err_str:
                    self._reconnect()
                    cursor = self.conn.cursor(buffered=True)
                    cursor.execute(sql_translated, params)
                    return CursorWrapper(cursor, is_mysql=True)
                raise
        else:
            cursor = self.conn.cursor()
            cursor.execute(sql, params)
            return CursorWrapper(cursor, is_mysql=False)

    def executemany(self, sql, params_list):
        if not params_list:
            return None
        if self.is_mysql:
            sql_translated = sql.replace('?', '%s')
            try:
                cursor = self.conn.cursor()
                cursor.executemany(sql_translated, params_list)
                return CursorWrapper(cursor, is_mysql=True)
            except Exception as e:
                err_str = str(e).lower()
                if "lost connection" in err_str or "gone away" in err_str or "2013" in err_str or "2006" in err_str:
                    self._reconnect()
                    cursor = self.conn.cursor()
                    cursor.executemany(sql_translated, params_list)
                    return CursorWrapper(cursor, is_mysql=True)
                raise
        else:
            cursor = self.conn.cursor()
            cursor.executemany(sql, params_list)
            return CursorWrapper(cursor, is_mysql=False)

    def commit(self):
        try:
            self.conn.commit()
        except Exception:
            pass

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass

def load_env() -> dict:
    env = {}
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    return env

def get_db_connection():
    """Loads MySQL configuration from env and attempts to connect.
    Falls back to SQLite (sortpin.db) if not configured or if connection fails."""
    env = load_env()
    mysql_password = env.get("MYSQL_PASSWORD", "")
    if mysql_password and mysql_password != "YOUR_PASSWORD_HERE":
        try:
            import mysql.connector
            host = env.get("MYSQL_HOST", "72.61.197.144")
            port = int(env.get("MYSQL_PORT", "3306"))
            db = env.get("MYSQL_DB", "data_pint")
            user = env.get("MYSQL_USER", "data_pint_user")

            conn = mysql.connector.connect(
                host=host,
                port=port,
                database=db,
                user=user,
                password=mysql_password,
                charset="utf8mb4",
                collation="utf8mb4_general_ci",
                autocommit=True,
                connection_timeout=30,
                pool_reset_session=False
            )
            try:
                cursor = conn.cursor()
                cursor.execute("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
                cursor.execute("SET SESSION net_read_timeout = 600")
                cursor.execute("SET SESSION net_write_timeout = 600")
                cursor.execute("SET SESSION wait_timeout = 28800")
                cursor.close()
            except Exception:
                pass
            return DBWrapper(is_mysql=True, conn=conn)
        except Exception as e:
            print(f"  Warning: MySQL connection failed: {e}. Falling back to SQLite.")

    import sqlite3
    sqlite_path = os.path.join(BASE, "sortpin.db")
    conn = sqlite3.connect(sqlite_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")       # concurrent readers + writer
    conn.execute("PRAGMA synchronous=NORMAL")     # safe but faster than FULL
    conn.execute("PRAGMA cache_size=-65536")      # 64 MB page cache
    conn.execute("PRAGMA temp_store=MEMORY")
    return DBWrapper(is_mysql=False, conn=conn)

# ─── Table Auto-Initialization ────────────────────────────────────────────────

def init_bulk_tables(db: DBWrapper):
    if db.is_mysql:
        queries = [
            """
            CREATE TABLE IF NOT EXISTS scraped_categories (
                id INT AUTO_INCREMENT PRIMARY KEY,
                name VARCHAR(255) UNIQUE NOT NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """,
            """
            CREATE TABLE IF NOT EXISTS scraped_websites (
                domain VARCHAR(255) PRIMARY KEY,
                url VARCHAR(255),
                title TEXT,
                description TEXT,
                cms VARCHAR(100),
                tech_stack TEXT,
                category_id INT,
                status VARCHAR(50),
                post_count INT DEFAULT 0,
                last_scraped_at DATETIME NULL,
                site_type VARCHAR(100) DEFAULT 'Blog'
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """,
            """
            CREATE TABLE IF NOT EXISTS scraped_websites_history (
                id INT AUTO_INCREMENT PRIMARY KEY,
                domain VARCHAR(255) NOT NULL,
                run_date DATETIME NOT NULL,
                status VARCHAR(50) NOT NULL,
                posts_count INT DEFAULT 0,
                posts_added INT DEFAULT 0,
                posts_removed INT DEFAULT 0,
                log LONGTEXT
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """,
            """
            CREATE TABLE IF NOT EXISTS scraped_posts (
                id VARCHAR(255) PRIMARY KEY,
                domain VARCHAR(255) NOT NULL,
                url TEXT NOT NULL,
                title TEXT,
                excerpt TEXT,
                author VARCHAR(255),
                date_published VARCHAR(100),
                image_url TEXT,
                source VARCHAR(100),
                status VARCHAR(50) DEFAULT 'active',
                first_seen_at DATETIME NULL,
                last_seen_at DATETIME NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """
        ]
    else:
        queries = [
            """
            CREATE TABLE IF NOT EXISTS scraped_categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS scraped_websites (
                domain TEXT PRIMARY KEY,
                url TEXT,
                title TEXT,
                description TEXT,
                cms TEXT,
                tech_stack TEXT,
                category_id INTEGER,
                status TEXT,
                post_count INTEGER DEFAULT 0,
                last_scraped_at TEXT,
                site_type TEXT DEFAULT 'Blog'
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS scraped_websites_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                domain TEXT NOT NULL,
                run_date TEXT NOT NULL,
                status TEXT NOT NULL,
                posts_count INTEGER DEFAULT 0,
                posts_added INTEGER DEFAULT 0,
                posts_removed INTEGER DEFAULT 0,
                log TEXT
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS scraped_posts (
                id TEXT PRIMARY KEY,
                domain TEXT NOT NULL,
                url TEXT NOT NULL,
                title TEXT,
                excerpt TEXT,
                author TEXT,
                date_published TEXT,
                image_url TEXT,
                source TEXT,
                status TEXT DEFAULT 'active',
                first_seen_at TEXT,
                last_seen_at TEXT
            );
            """
        ]
    for q in queries:
        db.execute(q)
    db.commit()

    # Self-healing database migration for site_type column
    try:
        db.execute("SELECT site_type FROM scraped_websites LIMIT 1")
    except Exception:
        alter_query = (
            "ALTER TABLE scraped_websites ADD COLUMN site_type VARCHAR(100) DEFAULT 'Blog'"
            if db.is_mysql else
            "ALTER TABLE scraped_websites ADD COLUMN site_type TEXT DEFAULT 'Blog'"
        )
        try:
            db.execute(alter_query)
            db.commit()
            print("Successfully added site_type column to scraped_websites table.")
        except Exception as e:
            print(f"Error migrating database (adding site_type): {e}")

    # Self-healing database migration for url column
    try:
        db.execute("SELECT url FROM scraped_websites LIMIT 1")
    except Exception:
        alter_query = (
            "ALTER TABLE scraped_websites ADD COLUMN url VARCHAR(255)"
            if db.is_mysql else
            "ALTER TABLE scraped_websites ADD COLUMN url TEXT"
        )
        try:
            db.execute(alter_query)
            db.commit()
            print("Successfully added url column to scraped_websites table.")
        except Exception as e:
            print(f"Error migrating database (adding url): {e}")

# ─── Settings & Config Helpers ────────────────────────────────────────────────

def utc_now() -> str:
    return datetime.utcnow().isoformat() + "Z"

def db_now() -> str:
    return datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')

def read_json(path: str, default: Any = None) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def write_json(path: str, data: Any) -> None:
    dir_name = os.path.dirname(path)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def load_settings() -> dict:
    # 1. Load from local settings.json if exists
    data = read_json(SETTINGS_FILE, {})
    if not isinstance(data, dict):
        data = {}
    
    # 2. Enrich with .env values
    env = load_env()
    env_mappings = {
        "OPENROUTER_API_KEY": "openrouter_api_key",
        "GROQ_API_KEY": "groq_api_key",
        "QUICK_SCRAPE_AI_PROVIDER": "quick_scrape_ai_provider",
        "QUICK_SCRAPE_GROQ_MODEL": "quick_scrape_groq_model",
        "QUICK_SCRAPE_OPENROUTER_MODEL": "quick_scrape_openrouter_model"
    }
    for env_key, settings_key in env_mappings.items():
        if env_key in env and env[env_key]:
            data[settings_key] = env[env_key]
            
    for k, v in env.items():
        k_lower = k.lower()
        if k_lower in DEFAULT_SETTINGS and v:
            data[k_lower] = v

    # 3. Auto-detect provider fallback if default 'local' is used but API keys are provided
    merged = {**DEFAULT_SETTINGS, **data}
    if merged.get("quick_scrape_ai_provider") == "local":
        if merged.get("openrouter_api_key"):
            merged["quick_scrape_ai_provider"] = "openrouter"
        elif merged.get("groq_api_key"):
            merged["quick_scrape_ai_provider"] = "groq"
            
    return merged

def get_ai_client(provider: str, model: str):
    """Return the right AI client for provider ('groq' | 'openrouter')."""
    cfg = load_settings()
    if provider == "openrouter":
        key = cfg.get("openrouter_api_key", "").strip()
        if not key:
            return None, "OpenRouter API key not set — add it in Settings"
        return OpenRouterClient(key, model), ""
    else:  # groq
        key = cfg.get("groq_api_key", "").strip()
        if not key:
            return None, "Groq API key not set — add it in Settings"
        return GroqClient(key, model), ""

# ─── URL Utilities ────────────────────────────────────────────────────────────

STORE_DOMAINS = [
    "etsy.com", "etsy.me",
    "shopify.com", "myshopify.com",
    "amazon.com", "amazon.co.uk", "amazon.ca", "amazon.de", "amazon.fr", "amazon.it", "amazon.es", "amazon.co.jp", "amazon.in",
    "ebay.com", "ebay.co.uk", "ebay.ca", "ebay.com.au",
    "aliexpress.com", "alibaba.com",
    "gumroad.com", "patreon.com", "subscribestar.com",
    "redbubble.com", "zazzle.com", "teespring.com", "spreadshirt.com", "society6.com", "cafepress.com",
    "poshmark.com", "depop.com", "mercari.com", "vinted.com",
    "pinterest.com", "pinterest.fr", "pinterest.de", "pinterest.co.uk",
    "instagram.com",
    "facebook.com",
    "youtube.com", "youtu.be",
    "twitter.com", "x.com",
    "tiktok.com",
    "linktr.ee",
    "t.co",
    "github.com",
    "google.com",
    # Streaming / music / media platforms
    "spotify.com", "open.spotify.com", "soundcloud.com", "deezer.com", "tidal.com",
    "apple.com", "music.apple.com", "podcasts.apple.com",
    "netflix.com", "hulu.com", "disneyplus.com", "hbomax.com", "peacocktv.com",
    "twitch.tv", "vimeo.com", "dailymotion.com",
    # Other social / community platforms
    "reddit.com", "tumblr.com", "quora.com", "medium.com", "substack.com",
    "snapchat.com", "threads.net", "mastodon.social", "bsky.app",
    "discord.com", "discord.gg", "slack.com", "telegram.org", "t.me",
    # Arts / education / schools
    "arts.ac.uk",
]

def classify_site_type(domain: str, is_wordpress: bool, post_count: int, tech_stack: str = "") -> str:
    domain = domain.lower()

    # 1. Social Media check
    social_domains = [
        "pinterest.com", "pinterest.fr", "pinterest.de", "pinterest.co.uk",
        "instagram.com", "facebook.com", "youtube.com", "youtu.be",
        "twitter.com", "x.com", "tiktok.com", "linktr.ee", "t.co", "github.com", "google.com"
    ]
    for social in social_domains:
        if domain == social or domain.endswith("." + social):
            return "Social Media"

    # 2. Link-in-bio services
    for lib in LINK_IN_BIO_DOMAINS:
        if domain == lib or domain.endswith("." + lib):
            return "Link-in-Bio"

    # 3. Known store domains
    for store in STORE_DOMAINS:
        if domain == store or domain.endswith("." + store):
            return "Store"

    # 4. Check tech stack
    tech_lower = (tech_stack or "").lower()
    if any(k in tech_lower for k in ["woocommerce", "shopify", "magento", "prestashop", "bigcommerce", "opencart", "squarespace store", "wix store"]):
        return "Store"

    # 5. Check wordpress / post_count
    if is_wordpress or post_count > 0:
        return "Blog"

    return "General Website"

LINK_IN_BIO_DOMAINS = [
    "msha.ke", "beacons.ai", "bio.link", "campsite.bio", "koji.to",
    "later.com", "linkinbio.com", "snipfeed.co", "tap.bio", "lnk.bio",
    "milkshake.app", "mysites.io", "palm.me", "shor.by", "stan.store",
    "contact.me", "solo.to", "carrd.co", "about.me", "bento.me",
    "liinks.co", "hoo.be", "linkpop.com", "flo.ink", "shorby.com",
    "jemi.so", "withkoji.com", "bit.ly", "rebrand.ly", "short.io",
]

def is_marketplace_or_social(domain: str) -> bool:
    domain = domain.lower()
    # Check STORE_DOMAINS (covers myshopify.com subdomains, etsy, amazon, etc.)
    for p in STORE_DOMAINS:
        if domain == p or domain.endswith("." + p):
            return True
    # Check link-in-bio services
    for p in LINK_IN_BIO_DOMAINS:
        if domain == p or domain.endswith("." + p):
            return True
    return False

def check_for_blocks(url: str, log_cb: Any) -> tuple[bool, str, str]:
    """
    Check if the URL is blocked by Cloudflare, Captcha, or access restriction.
    Returns (is_blocked, reason, homepage_html)
    homepage_html is the raw response text on success, empty string on block/error.
    """
    import requests

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "max-age=0",
        "Upgrade-Insecure-Requests": "1"
    }

    r = None
    # Try curl_cffi first to bypass Cloudflare/Captcha blocks if installed
    try:
        from curl_cffi import requests as curl_cffi_requests
        if curl_cffi_requests is not None:
            r = curl_cffi_requests.get(url, impersonate="chrome120", headers=headers, timeout=15, allow_redirects=True)
    except Exception:
        pass

    if r is None:
        try:
            r = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
        except requests.exceptions.Timeout:
            return True, "Connection Timeout", ""
        except requests.exceptions.RequestException as e:
            return True, f"Connection Failed: {str(e)}", ""

    try:
        # Check HTTP status codes commonly used for blocking
        if r.status_code in (403, 429, 503):
            text_lower = r.text.lower()
            if "cloudflare" in text_lower:
                return True, "Cloudflare Block", ""
            elif "captcha" in text_lower or "recaptcha" in text_lower or "hcaptcha" in text_lower:
                return True, "Captcha Block", ""
            elif "access denied" in text_lower or "permission denied" in text_lower:
                return True, "Access Denied", ""
            else:
                return True, f"HTTP Blocked ({r.status_code})", ""

        # Even on 200, it could show a Cloudflare challenge page or Captcha page
        if r.status_code == 200:
            text_lower = r.text.lower()
            content_len = len(r.text)
            # Real Cloudflare challenge pages are short and have specific markers
            if content_len < 30000 and "cloudflare" in text_lower and ("challenge" in text_lower or "enable javascript" in text_lower or "checking your browser" in text_lower):
                return True, "Cloudflare Challenge", ""
            # Real captcha CHALLENGE pages are short (<15KB) and have specific
            # blocking phrases. Normal sites often include reCAPTCHA scripts for
            # contact forms or have "captcha" in JS bundle names — those are NOT blocks.
            if content_len < 15000 and ("captcha" in text_lower or "recaptcha" in text_lower or "hcaptcha" in text_lower):
                challenge_phrases = [
                    "verify you are human",
                    "please verify",
                    "challenge-platform",
                    "just a moment",
                    "checking your browser",
                    "are you a robot",
                    "bot verification",
                    "security check",
                ]
                if any(phrase in text_lower for phrase in challenge_phrases):
                    return True, "Captcha Challenge Page", ""

        return False, "", r.text
    except Exception as e:
        return True, f"Parsing Block Page Failed: {str(e)}", ""


# Definitive HTML fingerprints for store platforms — zero false-positives on blogs
_STORE_HTML_PATTERNS = [
    ("cdn.shopify.com/s/files",  "Shopify Store"),
    ("cdn.shopify.com/shopifycloud", "Shopify Store"),
    ("/cdn/shop/",               "Shopify Store"),
    ("shopify-section",          "Shopify Store"),
    ("Shopify.theme",            "Shopify Store"),
    ("x-shopify-stage",          "Shopify Store"),
    ("cdn11.bigcommerce.com",    "BigCommerce Store"),
    ("bigcommerce.com/s/",       "BigCommerce Store"),
    ("cdn.bigcommerce.com",      "BigCommerce Store"),
    ("wcsstore/",                "IBM WebSphere Store"),
    ("magento/theme",            "Magento Store"),
    ("mage/",                    "Magento Store"),
]

def _quick_detect_store(domain: str, html: str) -> str | None:
    """
    Scan the homepage HTML for definitive store fingerprints.
    Returns a site_type string like 'Shopify Store' if matched, else None.
    Only returns non-None when we are 100% certain it is NOT a blog.
    """
    if not html:
        return None
    for pattern, label in _STORE_HTML_PATTERNS:
        if pattern in html:
            return label
    return None

def extract_domain(url: str) -> str:
    parsed = urlparse(url)
    domain = parsed.netloc or parsed.path
    if domain.startswith("www."):
        domain = domain[4:]
    return domain.split(":")[0].lower()

def safe_folder(domain: str) -> str:
    return re.sub(r"[^a-zA-Z0-9.-]+", "_", domain)

def normalize_url(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if not value.startswith(("http://", "https://")):
        value = "https://" + value
    parsed = urlparse(value)
    if not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}/"

def sha256_hash(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()

# ─── Google Sheets Web App Connection ─────────────────────────────────────────

def load_webapp_config():
    if not os.path.exists(WEBAPP_FILE):
        return None
    try:
        with open(WEBAPP_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if cfg.get("url") else None
    except Exception:
        return None

def get_websites_from_sheets() -> list:
    cfg = load_webapp_config()
    if not cfg:
        print("  Error: google_sheets_webapp.json not configured.")
        return []
    payload = {
        "action": "get_websites",
        "secret": cfg.get("secret", "pinterest-scan-2026")
    }
    try:
        r = requests.post(cfg["url"], json=payload, timeout=60)
        r.raise_for_status()
        data = r.json()
        if data.get("ok"):
            return data.get("websites") or []
    except Exception as e:
        print(f"  Error loading websites from Google Sheets: {e}")
    return []

def claim_websites_from_sheets(count: int) -> list:
    cfg = load_webapp_config()
    if not cfg:
        print("  Error: google_sheets_webapp.json not configured.")
        return []
    payload = {
        "action": "claim_websites",
        "count": count,
        "secret": cfg.get("secret", "pinterest-scan-2026")
    }
    try:
        r = requests.post(cfg["url"], json=payload, timeout=60)
        r.raise_for_status()
        data = r.json()
        if data.get("ok"):
            return data.get("claimed") or []
        else:
            print(f"  Failed to claim websites: {data.get('error')}")
    except Exception as e:
        print(f"  Error claiming websites from Google Sheets: {e}")
    return []

def update_website_in_sheets(website_url: str, updates: dict) -> bool:
    cfg = load_webapp_config()
    if not cfg:
        return False
    payload = {
        "action": "update_website",
        "website": website_url,
        "updates": updates,
        "secret": cfg.get("secret", "pinterest-scan-2026")
    }
    try:
        r = requests.post(cfg["url"], json=payload, timeout=60)
        r.raise_for_status()
        data = r.json()
        return bool(data.get("ok"))
    except Exception as e:
        print(f"  Error updating website {website_url} in Google Sheets: {e}")
    return False

import threading as _threading

def _sheets_update_async(url: str, updates: dict):
    """Fire-and-forget Google Sheets update — never blocks the scan worker."""
    def _do():
        try:
            update_website_in_sheets(url, updates)
        except Exception:
            pass
    _threading.Thread(target=_do, daemon=True).start()

# ─── Scraper Core & Post Comparison ───────────────────────────────────────────

def run_single_scrape(site: dict, provider: str, model: str, log_cb: Any) -> dict:
    domain = extract_domain(site.get("url", ""))
    
    if is_marketplace_or_social(domain):
        site_type = classify_site_type(domain, False, 0)
        log_cb(f"Fast-tracking marketplace/social domain: {domain} ({site_type})")
        
        result = {
            "url": site["url"],
            "is_wordpress": False,
            "posts": [],
            "site_info": {
                "name": f"{site_type} Profile",
                "description": f"Fast-tracked {site_type.lower()} domain.",
                "stack": {"cms": site_type},
                "stack_summary": site_type
            }
        }
        # Save JSON snapshot locally
        write_json(os.path.join(RESULTS_DIR, f"{safe_folder(domain)}.json"), result)
        
        db = get_db_connection()
        use_lock = not db.is_mysql
        if use_lock:
            db_write_lock.acquire()
        try:
            init_bulk_tables(db)
            
            cat_name = "General & Other"
            db.execute(
                "INSERT INTO scraped_categories (name) VALUES (?) ON DUPLICATE KEY UPDATE name=name" if db.is_mysql else
                "INSERT OR IGNORE INTO scraped_categories (name) VALUES (?)",
                [cat_name]
            )
            db.commit()
            
            cur_cat = db.execute("SELECT id FROM scraped_categories WHERE name = ?", [cat_name])
            cat_row = cur_cat.fetchone()
            category_id = cat_row["id"] if cat_row else None
            
            now_ts = db_now()
            db.execute(
                """
                INSERT INTO scraped_websites
                (domain, url, title, description, cms, tech_stack, category_id, status, post_count, last_scraped_at, site_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'done', 0, ?, ?)
                ON DUPLICATE KEY UPDATE
                    url = VALUES(url),
                    title = VALUES(title),
                    description = VALUES(description),
                    cms = VALUES(cms),
                    tech_stack = VALUES(tech_stack),
                    status = 'done',
                    post_count = 0,
                    last_scraped_at = VALUES(last_scraped_at),
                    site_type = VALUES(site_type)
                """ if db.is_mysql else
                """
                INSERT OR REPLACE INTO scraped_websites
                (domain, url, title, description, cms, tech_stack, category_id, status, post_count, last_scraped_at, site_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'done', 0, ?, ?)
                """,
                [domain, site["url"], f"{site_type} Profile", f"Fast-tracked {site_type.lower()} domain.", site_type, site_type, category_id, now_ts, site_type]
            )
            db.execute(
                """
                INSERT INTO scraped_websites_history
                (domain, run_date, status, posts_count, posts_added, posts_removed, log)
                VALUES (?, ?, 'done', 0, 0, 0, ?)
                """,
                [domain, now_ts, f"Fast-tracked {site_type.lower()} domain"]
            )
            db.commit()
            
            log_cb(f"Syncing fast-track results to Google Sheets Row...")
            sheet_updates = {
                "scrapped": "Yes",
                "name": f"{site_type} Profile",
                "categories": "General & Other",
                "scraped_pins": 0,
                "site_type": site_type
            }
            _sheets_update_async(site["url"], sheet_updates)
            log_cb(f"Database and Google Sheets updated successfully.")
        finally:
            db.close()
            if use_lock:
                db_write_lock.release()
        return result

    # Immediately set status to 'Running' in Google Sheets and Database for crash recovery
    log_cb(f"Setting site status to 'Running'...")
    update_website_in_sheets(site["url"], {"scrapped": "Running"})
    
    db = get_db_connection()
    use_lock = not db.is_mysql
    if use_lock:
        db_write_lock.acquire()
    try:
        init_bulk_tables(db)
        cur = db.execute("SELECT domain FROM scraped_websites WHERE domain = ?", [domain])
        if cur.fetchone():
            db.execute(
                "UPDATE scraped_websites SET url = ?, status = 'running', last_scraped_at = ? WHERE domain = ?",
                [site["url"], db_now(), domain]
            )
        else:
            db.execute(
                "INSERT INTO scraped_websites (domain, url, status, last_scraped_at) VALUES (?, ?, 'running', ?)",
                [domain, site["url"], db_now()]
            )
        db.commit()
    except Exception as e:
        log_cb(f"Warning: Failed to set database status to running: {e}")
    finally:
        db.close()
        if use_lock:
            db_write_lock.release()

    # Check for Captcha/Cloudflare blocks (also returns homepage HTML for reuse)
    is_blocked, block_reason, homepage_html = check_for_blocks(site["url"], log_cb)
    if is_blocked:
        log_cb(f"Blocked by security system: {block_reason}")
        result = {
            "url": site["url"],
            "is_wordpress": False,
            "posts": [],
            "site_info": {
                "name": "Blocked Site",
                "description": f"Access blocked: {block_reason}",
                "stack": {"cms": "Unknown"},
                "stack_summary": "Blocked"
            }
        }
        # Save JSON snapshot locally
        write_json(os.path.join(RESULTS_DIR, f"{safe_folder(domain)}.json"), result)
        
        db = get_db_connection()
        use_lock = not db.is_mysql
        if use_lock:
            db_write_lock.acquire()
        try:
            init_bulk_tables(db)
            cat_name = "General & Other"
            db.execute(
                "INSERT INTO scraped_categories (name) VALUES (?) ON DUPLICATE KEY UPDATE name=name" if db.is_mysql else
                "INSERT OR IGNORE INTO scraped_categories (name) VALUES (?)",
                [cat_name]
            )
            db.commit()
            
            cur_cat = db.execute("SELECT id FROM scraped_categories WHERE name = ?", [cat_name])
            cat_row = cur_cat.fetchone()
            category_id = cat_row["id"] if cat_row else None
            
            blocked_site_type = classify_site_type(domain, False, 0)
            # Check if domain exists to preserve other columns
            cur = db.execute("SELECT domain FROM scraped_websites WHERE domain = ?", [domain])
            if cur.fetchone():
                db.execute(
                    """
                    UPDATE scraped_websites
                    SET url = ?, title = ?, description = ?, cms = ?, tech_stack = ?, category_id = ?, status = 'blocked', post_count = 0, last_scraped_at = ?, site_type = ?
                    WHERE domain = ?
                    """,
                    [site["url"], "Blocked Site", f"Access blocked: {block_reason}", "Unknown", "Blocked", category_id, db_now(), blocked_site_type, domain]
                )
            else:
                db.execute(
                    """
                    INSERT INTO scraped_websites
                    (domain, url, title, description, cms, tech_stack, category_id, status, post_count, last_scraped_at, site_type)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'blocked', 0, ?, ?)
                    """,
                    [domain, site["url"], "Blocked Site", f"Access blocked: {block_reason}", "Unknown", "Blocked", category_id, db_now(), blocked_site_type]
                )
            
            db.execute(
                """
                INSERT INTO scraped_websites_history
                (domain, run_date, status, posts_count, posts_added, posts_removed, log)
                VALUES (?, ?, 'blocked', 0, 0, 0, ?)
                """,
                [domain, db_now(), f"Blocked by security: {block_reason}"]
            )
            db.commit()
            
            blocked_site_type = classify_site_type(domain, False, 0)
            log_cb(f"Syncing blocked status to Google Sheets Row...")
            sheet_updates = {
                "scrapped": f"Blocked ({block_reason})",
                "name": "Blocked Site",
                "categories": "General & Other",
                "scraped_pins": 0,
                "site_type": blocked_site_type
            }
            _sheets_update_async(site["url"], sheet_updates)
            log_cb(f"Database and Google Sheets updated with blocked status.")
        finally:
            db.close()
            if use_lock:
                db_write_lock.release()
            
        return result

    # ── Quick non-blog detection from homepage HTML ────────────────────────────
    # We already fetched the homepage in check_for_blocks — reuse it.
    # If the HTML reveals a definitive store platform, skip the full scrape now.
    quick_store_type = _quick_detect_store(domain, homepage_html)
    if quick_store_type:
        log_cb(f"Homepage HTML identified as {quick_store_type} — skipping full scrape")
        result = {
            "url": site["url"], "is_wordpress": False, "posts": [],
            "site_info": {
                "name": f"{quick_store_type}",
                "description": f"Detected as {quick_store_type.lower()} from homepage.",
                "stack": {"cms": quick_store_type}, "stack_summary": quick_store_type
            }
        }
        write_json(os.path.join(RESULTS_DIR, f"{safe_folder(domain)}.json"), result)
        db = get_db_connection()
        use_lock = not db.is_mysql
        if use_lock:
            db_write_lock.acquire()
        try:
            init_bulk_tables(db)
            cat_name = "General & Other"
            db.execute(
                "INSERT INTO scraped_categories (name) VALUES (?) ON DUPLICATE KEY UPDATE name=name" if db.is_mysql else
                "INSERT OR IGNORE INTO scraped_categories (name) VALUES (?)", [cat_name]
            )
            db.commit()
            cur_cat = db.execute("SELECT id FROM scraped_categories WHERE name = ?", [cat_name])
            cat_row = cur_cat.fetchone()
            category_id = cat_row["id"] if cat_row else None
            now_ts = db_now()
            db.execute(
                """
                INSERT INTO scraped_websites
                (domain, url, title, description, cms, tech_stack, category_id, status, post_count, last_scraped_at, site_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'done', 0, ?, ?)
                ON DUPLICATE KEY UPDATE url=VALUES(url), title=VALUES(title), description=VALUES(description),
                    cms=VALUES(cms), tech_stack=VALUES(tech_stack), status='done',
                    post_count=0, last_scraped_at=VALUES(last_scraped_at), site_type=VALUES(site_type)
                """ if db.is_mysql else
                """
                INSERT OR REPLACE INTO scraped_websites
                (domain, url, title, description, cms, tech_stack, category_id, status, post_count, last_scraped_at, site_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'done', 0, ?, ?)
                """,
                [domain, site["url"], quick_store_type, f"Detected as {quick_store_type.lower()} from homepage.",
                 quick_store_type, quick_store_type, category_id, now_ts, "Store"]
            )
            db.commit()
            log_cb(f"Syncing quick-detect results to Google Sheets Row...")
            _sheets_update_async(site["url"], {
                "scrapped": "Yes", "name": quick_store_type,
                "categories": "General & Other", "scraped_pins": 0, "site_type": "Store"
            })
            log_cb(f"Database and Google Sheets updated — {quick_store_type}, skipped.")
        finally:
            db.close()
            if use_lock:
                db_write_lock.release()
        return result
    # ──────────────────────────────────────────────────────────────────────────

    ai_client = None
    if provider in ("groq", "openrouter"):
        ai_client, ai_err = get_ai_client(provider, model)
        if ai_err:
            log_cb(f"Warning: {ai_err}")
        else:
            log_cb(f"Using {provider} with model {model}...")
    else:
        log_cb("Running local sitemap/Wordpress scrape (no AI provider).")
        
    result = scrape_site(
        site["url"],
        grok_client=ai_client,
        progress_cb=log_cb
    )
    
    # Save JSON snapshot locally
    write_json(os.path.join(RESULTS_DIR, f"{safe_folder(domain)}.json"), result)
    
    # Diff posts and save to DB
    db = get_db_connection()
    use_lock = not db.is_mysql
    if use_lock:
        db_write_lock.acquire()
    try:
        # Create tables if not exist
        init_bulk_tables(db)
        
        # Load existing posts from DB for comparison
        cur = db.execute("SELECT url, id, status FROM scraped_posts WHERE domain = ?", [domain])
        db_posts = {row['url']: row for row in cur.fetchall()}
        
        posts_scraped = result.get("posts") or []
        added_count = 0
        removed_count = 0
        now_ts = db_now()
        
        scraped_urls = set()
        insert_data = []
        update_data = []
        
        for post in posts_scraped:
            p_url = post.get("link") or post.get("url") or ""
            if not p_url:
                continue
            if p_url in scraped_urls:
                continue
            scraped_urls.add(p_url)
            
            p_id = sha256_hash(domain + p_url)
            title = post.get("title")
            if isinstance(title, dict):
                title = title.get("rendered") or ""
            title = str(title or "")
            
            excerpt = post.get("excerpt")
            if isinstance(excerpt, dict):
                excerpt = excerpt.get("rendered") or ""
            excerpt = str(excerpt or "")
            
            author = str(post.get("author_name") or post.get("author") or "")
            date_pub = str(post.get("date") or post.get("date_published") or "")
            image_url = str(post.get("featured_image") or post.get("image_url") or "")
            source = str(post.get("source") or "crawler")
            
            if p_url in db_posts:
                db_row = db_posts[p_url]
                status = "active"
                if db_row["status"] == "removed":
                    added_count += 1
                
                update_data.append([
                    title, excerpt, author, date_pub, image_url, status, now_ts, db_row["id"]
                ])
            else:
                insert_data.append([
                    p_id, domain, p_url, title, excerpt, author, date_pub, image_url, source, now_ts, now_ts
                ])
                added_count += 1
                
        # Batch execute updates
        if update_data:
            chunk_size = 500
            for idx in range(0, len(update_data), chunk_size):
                db.executemany(
                    """
                    UPDATE scraped_posts
                    SET title = ?, excerpt = ?, author = ?, date_published = ?, image_url = ?, status = ?, last_seen_at = ?
                    WHERE id = ?
                    """,
                    update_data[idx : idx + chunk_size]
                )
                db.commit()
                
        # Batch execute inserts
        if insert_data:
            chunk_size = 500
            for idx in range(0, len(insert_data), chunk_size):
                db.executemany(
                    """
                    INSERT INTO scraped_posts
                    (id, domain, url, title, excerpt, author, date_published, image_url, source, status, first_seen_at, last_seen_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                    """,
                    insert_data[idx : idx + chunk_size]
                )
                db.commit()
                
        # Identify removed posts
        removed_data = []
        for p_url, db_row in db_posts.items():
            if db_row["status"] == "active" and p_url not in scraped_urls:
                removed_data.append([now_ts, db_row["id"]])
                removed_count += 1
                
        if removed_data:
            chunk_size = 500
            for idx in range(0, len(removed_data), chunk_size):
                db.executemany(
                    "UPDATE scraped_posts SET status = 'removed', last_seen_at = ? WHERE id = ?",
                    removed_data[idx : idx + chunk_size]
                )
                db.commit()
                
        # Update scraped_websites
        site_info = result.get("site_info") or {}
        stack = site_info.get("stack") or {}
        cms = "WordPress" if result.get("is_wordpress") else (stack.get("cms") or "Unknown")
        tech_stack = site_info.get("stack_summary") or ""
        
        # Keep existing category_id if present
        cur = db.execute("SELECT category_id FROM scraped_websites WHERE domain = ?", [domain])
        existing_site = cur.fetchone()
        category_id = existing_site["category_id"] if existing_site else None
        
        # Programmatic site classification
        site_type = classify_site_type(
            domain=domain,
            is_wordpress=result.get("is_wordpress", False),
            post_count=len(posts_scraped),
            tech_stack=tech_stack
        )
        log_cb(f"Classified site type: {site_type}")
        
        db.execute(
            """
            INSERT INTO scraped_websites
            (domain, url, title, description, cms, tech_stack, category_id, status, post_count, last_scraped_at, site_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'done', ?, ?, ?)
            ON DUPLICATE KEY UPDATE
                url = VALUES(url),
                title = VALUES(title),
                description = VALUES(description),
                cms = VALUES(cms),
                tech_stack = VALUES(tech_stack),
                status = 'done',
                post_count = VALUES(post_count),
                last_scraped_at = VALUES(last_scraped_at),
                site_type = VALUES(site_type)
            """ if db.is_mysql else
            """
            INSERT OR REPLACE INTO scraped_websites
            (domain, url, title, description, cms, tech_stack, category_id, status, post_count, last_scraped_at, site_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'done', ?, ?, ?)
            """,
            [domain, site["url"], site_info.get("name") or "", site_info.get("description") or "", cms, tech_stack, category_id, len(posts_scraped), now_ts, site_type]
        )
        
        # Log execution history
        db.execute(
            """
            INSERT INTO scraped_websites_history
            (domain, run_date, status, posts_count, posts_added, posts_removed, log)
            VALUES (?, ?, 'done', ?, ?, ?, ?)
            """,
            [domain, now_ts, len(posts_scraped), added_count, removed_count, "Single Scan Done"]
        )
        db.commit()
        
        log_cb(f"Syncing results to Google Sheets Row...")
        sheet_updates = {
            "scrapped": "Yes",
            "name": site_info.get("name") or "",
            "scraped_pins": len(posts_scraped),
            "site_type": site_type
        }
        _sheets_update_async(site["url"], sheet_updates)
        log_cb(f"Database and Google Sheets updated successfully. Added {added_count} posts, removed {removed_count} posts.")
        
    finally:
        db.close()
        if use_lock:
            db_write_lock.release()
        
    return result

# ─── Token-Efficient AI Categorizer ───────────────────────────────────────────

def run_ai_categorization(log_cb: Any) -> None:
    db = get_db_connection()
    try:
        settings = load_settings()
        provider = settings.get("quick_scrape_ai_provider", "local")
        if provider == "openrouter":
            model = settings.get("quick_scrape_openrouter_model", "openai/gpt-4.1-mini")
        else:
            model = settings.get("quick_scrape_groq_model", "llama-3.3-70b-versatile")
            
        ai_client, ai_err = get_ai_client(provider, model)
        if ai_err or not ai_client:
            log_cb(f"AI Client not configured: {ai_err}. Skipping AI categorization.")
            return

        cur = db.execute(
            """
            SELECT domain, title, description FROM scraped_websites
            WHERE category_id IS NULL AND title IS NOT NULL AND title != ""
            """
        )
        pending = cur.fetchall()
        if not pending:
            log_cb("No uncategorized websites found in database.")
            return
            
        log_cb(f"Found {len(pending)} uncategorized website(s). Running AI classification...")
        
        categories_list = [
            "Food & Recipes", "Travel & Outdoors", "Fashion & Beauty",
            "Home Decor & DIY", "Health & Wellness", "Tech & Gadgets",
            "Lifestyle & Parenting", "Craft & Hobbies", "General & Other"
        ]
        
        batch_size = 20
        for idx in range(0, len(pending), batch_size):
            chunk = pending[idx : idx + batch_size]
            sites_data = []
            for row in chunk:
                sites_data.append({
                    "domain": row["domain"],
                    "title": row["title"],
                    "description": row["description"][:200] if row["description"] else ""
                })
                
            system_prompt = (
                "You are an expert website classifier. You classify websites into one of these exact categories: "
                f"{', '.join(categories_list)}. Respond ONLY with a clean JSON object where keys are domains and values are the classified category strings."
            )
            user_prompt = f"Please classify the following websites:\n{json.dumps(sites_data, indent=2)}"
            
            log_cb(f"Sending batch {idx // batch_size + 1} to {provider}...")
            try:
                response_text = ai_client._chat(system_prompt, user_prompt)
                
                json_match = re.search(r"\{.*\}", response_text, re.DOTALL)
                if json_match:
                    classified = json.loads(json_match.group(0))
                else:
                    classified = json.loads(response_text)
                    
                for domain, cat_name in classified.items():
                    cat_name = str(cat_name).strip()
                    if cat_name not in categories_list:
                        cat_name = "General & Other"
                        
                    db.execute(
                        "INSERT INTO scraped_categories (name) VALUES (?) ON DUPLICATE KEY UPDATE name=name" if db.is_mysql else
                        "INSERT OR IGNORE INTO scraped_categories (name) VALUES (?)",
                        [cat_name]
                    )
                    db.commit()
                    
                    cur_cat = db.execute("SELECT id FROM scraped_categories WHERE name = ?", [cat_name])
                    cat_row = cur_cat.fetchone()
                    if cat_row:
                        cat_id = cat_row["id"]
                        db.execute(
                            "UPDATE scraped_websites SET category_id = ? WHERE domain = ?",
                            [cat_id, domain]
                        )
                        db.commit()
                        log_cb(f"  Domain '{domain}' -> classified as '{cat_name}'")
                        
                        full_url = f"https://{domain}"
                        _sheets_update_async(full_url, {"categories": cat_name})
                        
            except Exception as e:
                log_cb(f"  Error processing batch: {e}")
                
        log_cb("AI Categorization complete.")
    finally:
        db.close()

# ─── Bulk Scraper Background Job ──────────────────────────────────────────────

def run_bulk_scrape_job(run_all: bool) -> None:
    global bulk_job
    with bulk_lock:
        bulk_job.update({
            "status": "running",
            "total_sites": 0,
            "scanned_sites": 0,
            "current_site": "",
            "log": [],
            "created_at": time.time(),
            "cancel": False
        })
        
    def log(msg: str) -> None:
        print(f"[Bulk Log] {msg}")
        with bulk_lock:
            bulk_job["log"].append(str(msg))

    settings = load_settings()
    provider = settings.get("quick_scrape_ai_provider", "local")
    if provider == "openrouter":
        model = settings.get("quick_scrape_openrouter_model", "openai/gpt-4.1-mini")
    else:
        model = settings.get("quick_scrape_groq_model", "llama-3.3-70b-versatile")
        
    env_vars = load_env()
    try:
        max_workers = int(env_vars.get("MAX_BULK_WORKERS", 5))
    except Exception:
        max_workers = 5

    try:
        batch_size = int(env_vars.get("BATCH_CLAIM_SIZE", 5))
    except Exception:
        batch_size = 5
        
    from concurrent.futures import ThreadPoolExecutor
    
    def scan_worker(site_info: dict):
        with bulk_lock:
            if bulk_job["cancel"]:
                return
                
        domain = extract_domain(site_info["url"])
        
        def site_log(msg: str):
            log(f"[{domain}] {msg}")
            
        site_log("Starting parallel scan...")
        with bulk_lock:
            bulk_job["current_site"] = domain
            
        try:
            run_single_scrape(site_info, provider, model, site_log)
            site_log("Parallel scan completed successfully.")
        except Exception as e:
            site_log(f"Parallel scan failed: {e}")
            # Automatically update DB status and sheet to failed to avoid infinite loop
            db = get_db_connection()
            use_lock = not db.is_mysql
            if use_lock:
                db_write_lock.acquire()
            try:
                db.execute(
                    "UPDATE scraped_websites SET status = 'failed', last_scraped_at = ? WHERE domain = ?",
                    [db_now(), domain]
                )
                db.execute(
                    """
                    INSERT INTO scraped_websites_history
                    (domain, run_date, status, posts_count, posts_added, posts_removed, log)
                    VALUES (?, ?, 'failed', 0, 0, 0, ?)
                    """,
                    [domain, db_now(), f"Scan failed: {e}"]
                )
                db.commit()
            except Exception as db_err:
                site_log(f"Warning: Failed to set failed status in database: {db_err}")
            finally:
                db.close()
                if use_lock:
                    db_write_lock.release()
            try:
                _sheets_update_async(site_info["url"], {"scrapped": f"Failed ({str(e)[:50]})"})
            except Exception as sheet_err:
                site_log(f"Warning: Failed to set failed status in sheets: {sheet_err}")
            
        with bulk_lock:
            bulk_job["scanned_sites"] += 1

    first_iteration = True
    while True:
        with bulk_lock:
            if bulk_job["cancel"]:
                log("Bulk job cancelled by user.")
                break

        if run_all:
            if not first_iteration:
                break
            log("Loading websites from Google Sheets...")
            sites_list = get_websites_from_sheets()
            if not sites_list:
                log("No websites found in Google Sheet or failed to connect.")
                with bulk_lock:
                    bulk_job["status"] = "error"
                    bulk_job["log"].append("Failed to load websites from Google Sheets.")
                return
            
            valid_sites = []
            for s in sites_list:
                url = s.get("website") or s.get("url") or ""
                if url:
                    valid_sites.append({
                        "url": normalize_url(url),
                        "scrapped": str(s.get("scrapped") or "").strip()
                    })
            target_sites = valid_sites
            log(f"Bulk Run: Scanning all {len(target_sites)} websites.")
        else:
            # ── Stale-running cleanup: sites stuck >3 h are crashes, not active scans ──
            db = get_db_connection()
            try:
                init_bulk_tables(db)
                stale_q = (
                    "SELECT domain, url FROM scraped_websites WHERE status = 'running' AND last_scraped_at < DATE_SUB(NOW(), INTERVAL 3 HOUR)"
                    if db.is_mysql else
                    "SELECT domain, url FROM scraped_websites WHERE status = 'running' AND datetime(last_scraped_at) < datetime('now', '-3 hours')"
                )
                stale_rows = db.execute(stale_q).fetchall()
                if stale_rows:
                    stale_domains = [r['domain'] for r in stale_rows]
                    log(f"Resetting {len(stale_rows)} stale 'Running' site(s) that crashed >3 h ago: {stale_domains}")
                    for r in stale_rows:
                        try:
                            db.execute("UPDATE scraped_websites SET status='failed' WHERE domain=?", [r['domain']])
                            # Reset Google Sheets row so it can be reclaimed
                            _sheets_update_async(r.get('url') or f"https://{r['domain']}", {"scrapped": "Not Yet"})
                        except Exception:
                            pass
                    db.commit()
            except Exception as e:
                log(f"Warning: Stale-running cleanup failed: {e}")
            finally:
                db.close()

            # Check local/remote DB for recently-crashed/running crawls (< 3 h old = genuine resume)
            resumed_sites = []
            db = get_db_connection()
            try:
                cur = db.execute("SELECT domain, url FROM scraped_websites WHERE status = 'running'")
                rows = cur.fetchall()
                for row in rows:
                    domain_val = row['domain']
                    url_val = row.get('url') or f"https://{domain_val}"
                    resumed_sites.append({
                        "url": normalize_url(url_val),
                        "scrapped": "Running (Resumed)"
                    })
            except Exception as e:
                log(f"Warning: Failed to fetch resumed sites from database: {e}")
            finally:
                db.close()

            if resumed_sites:
                log(f"Found {len(resumed_sites)} interrupted website scan(s) to resume: {[s['url'] for s in resumed_sites]}")

            to_claim = batch_size - len(resumed_sites)
            claimed_sites = []
            if to_claim > 0:
                log(f"Attempting to claim {to_claim} new website(s) from Google Sheets...")
                claimed_list = claim_websites_from_sheets(to_claim)
                for s in claimed_list:
                    url = s.get("website") or s.get("url") or ""
                    if url:
                        claimed_sites.append({
                            "url": normalize_url(url),
                            "scrapped": str(s.get("scrapped") or "Running").strip()
                        })
                if claimed_sites:
                    log(f"Atomically claimed {len(claimed_sites)} new website(s) from Google Sheets: {[s['url'] for s in claimed_sites]}")
                else:
                    log("No new websites could be claimed from Google Sheets.")

            target_sites = resumed_sites + claimed_sites

            # Deduplicate: drop domains already finished in DB (prevents re-scanning on duplicate sheet rows)
            try:
                db = get_db_connection()
                done_domains = set(
                    r['domain'] for r in db.execute(
                        "SELECT domain FROM scraped_websites WHERE status IN ('done','blocked')"
                    ).fetchall()
                )
                db.close()
                before = len(target_sites)
                target_sites = [s for s in target_sites if extract_domain(s['url']) not in done_domains]
                skipped_dupes = before - len(target_sites)
                if skipped_dupes:
                    log(f"Skipped {skipped_dupes} already-done domain(s) (duplicate sheet rows).")
            except Exception as _ded_err:
                log(f"Warning: deduplication check failed: {_ded_err}")

            if not target_sites:
                log("No pending or resumed websites to scan. Bulk job finished.")
                break

            log(f"Bulk Batch: Scanning {len(target_sites)} website(s) ({len(resumed_sites)} resumed, {len(claimed_sites)} newly claimed).")

        with bulk_lock:
            bulk_job["total_sites"] += len(target_sites)

        log(f"Starting parallel batch scan using ThreadPoolExecutor (max_workers={max_workers})...")
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            list(executor.map(scan_worker, target_sites))

        log("\n--- Running AI Website Categorization for this batch ---")
        try:
            run_ai_categorization(log)
        except Exception as e:
            log(f"AI Categorization error: {e}")

        first_iteration = False
        if run_all:
            break

    log("\nBulk Scraper job completed successfully!")
    with bulk_lock:
        bulk_job["status"] = "done"

# ─── Flask API Server Routes ──────────────────────────────────────────────────

@app.route("/")
def index():
    return jsonify({
        "name": "Pinterest Scan Standalone API & Bulk Engine",
        "port": 5050,
        "endpoints": [
            "/api/sites", "/api/sites/<site_id>/scrape", "/api/sites/<site_id>/scrape-status", "/api/sites/<site_id>/results",
            "/api/bulk/scrape", "/api/bulk/scrape-status", "/api/bulk/history"
        ]
    })

@app.route("/api/sites", methods=["GET"])
def api_sites_get():
    return jsonify(get_websites_from_sheets())

@app.route("/api/sites/<path:site_id>/scrape", methods=["POST", "GET"])
def api_site_scrape(site_id):
    job_id = str(uuid.uuid4())
    url = normalize_url(site_id)
    if not url:
        return jsonify({"error": "invalid URL"}), 400
        
    settings = load_settings()
    provider = settings.get("quick_scrape_ai_provider", "local")
    if provider == "openrouter":
        model = settings.get("quick_scrape_openrouter_model", "openai/gpt-4.1-mini")
    else:
        model = settings.get("quick_scrape_groq_model", "llama-3.3-70b-versatile")
        
    with jobs_lock:
        jobs[job_id] = {
            "status": "running",
            "site_id": site_id,
            "domain": extract_domain(url),
            "log": [],
            "created_at": time.time()
        }
        
    def run_single_background():
        def log(msg):
            with jobs_lock:
                jobs[job_id]["log"].append(str(msg))
        try:
            result = run_single_scrape({"url": url}, provider, model, log)
            try:
                run_ai_categorization(log)
            except Exception:
                pass
                
            with jobs_lock:
                jobs[job_id].update({
                    "status": "done",
                    "post_count": len(result.get("posts") or [])
                })
        except Exception as e:
            with jobs_lock:
                jobs[job_id].update({
                    "status": "error",
                    "error": str(e)
                })
                jobs[job_id]["log"].append(f"Error: {e}")
                
    threading.Thread(target=run_single_background, daemon=True).start()
    
    return jsonify({
        "job_id": job_id,
        "site_id": site_id,
        "status_url": f"/api/sites/{site_id}/scrape-status"
    })

@app.route("/api/sites/<path:site_id>/scrape-status", methods=["GET"])
def api_site_scrape_status(site_id):
    domain = extract_domain(site_id)
    with jobs_lock:
        matches = [
            (jid, job) for jid, job in jobs.items()
            if job.get("domain") == domain
        ]
    if not matches:
        return jsonify({"status": "idle"})
        
    jid, job = max(matches, key=lambda item: item[1].get("created_at", 0))
    return jsonify({
        "job_id": jid,
        **job
    })

@app.route("/api/sites/<path:site_id>/results", methods=["GET"])
def api_site_results(site_id):
    domain = extract_domain(site_id)
    res = read_json(os.path.join(RESULTS_DIR, f"{safe_folder(domain)}.json"), {})
    return jsonify(res)

@app.route("/api/bulk/scrape", methods=["POST"])
def api_bulk_scrape():
    data = request.get_json(silent=True) or {}
    run_all = bool(data.get("run_all", False))
    
    with bulk_lock:
        if bulk_job["status"] == "running":
            return jsonify({"error": "Bulk job is already running."}), 400
            
    threading.Thread(target=run_bulk_scrape_job, args=(run_all,), daemon=True).start()
    return jsonify({"ok": True, "message": "Bulk scrape started in background."})

@app.route("/api/bulk/scrape-status", methods=["GET"])
def api_bulk_scrape_status():
    with bulk_lock:
        return jsonify(bulk_job)

@app.route("/api/bulk/scrape/cancel", methods=["POST"])
def api_bulk_scrape_cancel():
    with bulk_lock:
        bulk_job["cancel"] = True
    return jsonify({"ok": True})

@app.route("/api/bulk/history", methods=["GET"])
def api_bulk_history():
    db = get_db_connection()
    try:
        init_bulk_tables(db)
        cur = db.execute("SELECT * FROM scraped_websites_history ORDER BY run_date DESC LIMIT 100")
        rows = [dict(r) for r in cur.fetchall()]
        return jsonify(rows)
    finally:
        db.close()

# ─── Main CLI / Server Entry ──────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Standalone Local API for Domain Quick Scrape.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5050)
    parser.add_argument("--run", action="store_true", help="Bulk scan pending domains listed in Google Sheets and exit")
    parser.add_argument("--runjobforall", action="store_true", help="Bulk scan all domains listed in Google Sheets and exit")
    args = parser.parse_args()
    
    os.makedirs(RESULTS_DIR, exist_ok=True)
    
    # Run in CLI Mode
    if args.run or args.runjobforall:
        print(f"Starting Bulk Scraper CLI Mode (run_all={args.runjobforall})...")
        run_bulk_scrape_job(run_all=args.runjobforall)
        return 0
        
    # Start Flask API server
    print(f"Domain Quick Scrape API: http://{args.host}:{args.port}")
    print("Running independently using local scraper engine.")
    
    # Initialize DB tables once at start
    db = get_db_connection()
    try:
        init_bulk_tables(db)
    finally:
        db.close()
        
    app.run(host=args.host, port=args.port, debug=False, threaded=True)
    return 0

if __name__ == "__main__":
    sys.exit(main())
