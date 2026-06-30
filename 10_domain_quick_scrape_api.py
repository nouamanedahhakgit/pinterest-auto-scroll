"""
STEP 10 - Local API & CLI for Domain Quick/Bulk Scrape
======================================================

Flask API and CLI scanner that runs standalone in this project,
importing the scraper engine directly from C:\\Users\\leno\\Documents\\GitHub\\scrap_any_blog
without duplicating any files. It connects to Google Sheets via your Apps Script
Web App to fetch websites and update rows in-place.

Run as CLI Bulk Scraper (default — no flags needed):
  python 10_domain_quick_scrape_api.py                   # Same as --run: scrape only pending websites
  python 10_domain_quick_scrape_api.py --run              # Explicit, identical to no-flags default
  python 10_domain_quick_scrape_api.py --runjobforall    # Scrape all websites

Run as Flask API (must be requested explicitly):
  python 10_domain_quick_scrape_api.py --serve
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue as _queue
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

# ── Shared MySQL connection pool ───────────────────────────────────────────────
# Created lazily on first use (thread-safe double-checked locking).
# All worker threads share one pool instead of opening a new physical connection
# per get_db_connection() call, which previously caused "pool exhausted" errors
# when MAX_BULK_WORKERS concurrent threads all tried to connect simultaneously.
_mysql_pool      = None
_mysql_pool_lock = threading.Lock()

def _get_mysql_pool():
    """Return the shared MySQLConnectionPool, creating it on first call."""
    global _mysql_pool
    if _mysql_pool is not None:
        return _mysql_pool
    with _mysql_pool_lock:
        if _mysql_pool is not None:
            return _mysql_pool
        env = load_env()
        pw = env.get("MYSQL_PASSWORD", "")
        if not pw or pw == "YOUR_PASSWORD_HERE":
            return None
        try:
            import mysql.connector.pooling
            pool_size = int(env.get("MYSQL_POOL_SIZE", 15))
            _mysql_pool = mysql.connector.pooling.MySQLConnectionPool(
                pool_name="step10",
                pool_size=pool_size,
                pool_reset_session=False,          # keep SET SESSION vars across checkouts
                host=env.get("MYSQL_HOST", "72.61.197.144"),
                port=int(env.get("MYSQL_PORT", "3306")),
                database=env.get("MYSQL_DB", "data_pint"),
                user=env.get("MYSQL_USER", "data_pint_user"),
                password=pw,
                charset="utf8mb4",
                collation="utf8mb4_general_ci",
                autocommit=True,
                connection_timeout=30,
            )
            return _mysql_pool
        except Exception as e:
            print(f"  Warning: MySQL pool init failed: {e}. Will fall back to SQLite.")
            return None

def get_db_connection():
    """Get a DB connection from the shared MySQL pool, or fall back to SQLite."""
    pool = _get_mysql_pool()
    if pool is not None:
        try:
            conn = pool.get_connection()
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
            print(f"  Warning: MySQL pool get_connection failed: {e}. Falling back to SQLite.")

    import sqlite3
    sqlite_path = os.path.join(BASE, "sortpin.db")
    conn = sqlite3.connect(sqlite_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-65536")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA busy_timeout=15000")
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
    # Creator shopping / affiliate platforms (NOT blogs)
    "shopmy.us", "shopltk.com", "ltk.com", "liketoknowit.com", "shopstyle.com",
    "rewardstyle.com", "rstyle.me", "collage.com", "collshp.com",
    "benable.com", "direct.me",
    # Photo / portfolio / visual platforms
    "vsco.co", "behance.net", "dribbble.com", "flickr.com", "500px.com",
    "unsplash.com", "pexels.com", "pixabay.com",
    # URL shorteners (never blogs)
    "bit.ly", "cutt.ly", "tinyurl.com", "ow.ly", "buff.ly", "rb.gy",
    "walmrt.us", "amzn.to", "amzn.eu",
    # Web-app / firestore / hosting (never content sites)
    "web.app", "firebaseapp.com", "pages.dev", "netlify.app", "vercel.app",
    # ── Major global retail / fashion / sports brands ──────────────────────
    # Sports & footwear
    "adidas.com", "nike.com", "puma.com", "reebok.com", "newbalance.com",
    "underarmour.com", "asics.com", "skechers.com", "vans.com", "converse.com",
    "timberland.com", "merrell.com", "salomon.com", "columbia.com", "thenorthface.com",
    "ugg.com", "birkenstock.com", "crocs.com", "clarks.com", "ecco.com",
    # Fast fashion / apparel
    "zara.com", "hm.com", "uniqlo.com", "gap.com", "oldnavy.com",
    "bananarepublic.com", "forever21.com", "fashionnova.com", "shein.com", "temu.com",
    "asos.com", "boohoo.com", "prettylittlething.com", "misguided.com",
    "topshop.com", "cos.com", "mango.com", "massimozara.com",
    "anthropologie.com", "freepeople.com", "urbanoutfitters.com", "bhldn.com",
    "ae.com", "aerie.com", "aeropostale.com", "hollister.com", "abercrombie.com",
    # Luxury / designer
    "gucci.com", "louisvuitton.com", "lv.com", "chanel.com", "hermes.com",
    "prada.com", "fendi.com", "dior.com", "versace.com", "armani.com",
    "balenciaga.com", "givenchy.com", "valentino.com", "burberry.com",
    "alexandermcqueen.com", "bottegaveneta.com", "loewe.com", "celine.com",
    "tiffany.com", "cartier.com", "swarovski.com",
    # Department stores / marketplaces
    "nordstrom.com", "nordstromrack.com", "macys.com", "bloomingdales.com",
    "saksfifthavenue.com", "saks.com", "neimanmarcus.com", "bergdorfgoodman.com",
    "target.com", "walmart.com", "costco.com", "kohls.com", "jcpenney.com",
    "belk.com", "dsw.com", "aldoshoes.com", "aldo.com", "aldi.co.uk", "aldi.com",
    "marshalls.com", "tjmaxx.com", "homegoods.com", "ross.com",
    # Beauty / skincare
    "sephora.com", "ulta.com", "glossier.com", "fentybeauty.com",
    "charlotte tilbury.com", "charlottetilbury.com", "nars.com", "maccosmetics.com",
    "kiehls.com", "lancome.com", "esteelauder.com", "clinique.com",
    "lush.com", "thebodyshop.com", "bathandbodyworks.com",
    # Activewear / lifestyle
    "lululemon.com", "alo.com", "aloyoga.com", "vuori.com", "athleta.com",
    "gymshark.com", "fabletics.com", "outdoor voices.com", "outdoorvoices.com",
    # Home / furniture
    "ikea.com", "wayfair.com", "overstock.com", "crateandbarrel.com", "cb2.com",
    "westelm.com", "potterybarn.com", "restorationhardware.com", "rh.com",
    "williams-sonoma.com", "surlatable.com", "anthropologie.com",
    "zgallerie.com", "worldmarket.com", "pier1.com",
    # Accessories / jewelry
    "pandora.net", "signetjewelers.com", "zales.com", "kay.com", "jared.com",
    "mejuri.com", "missoma.com", "gorjana.com", "kendra-scott.com",
    # Other major retail
    "apple.com", "samsung.com", "sony.com", "dell.com", "hp.com", "lenovo.com",
    "bestbuy.com", "newegg.com", "bhphotovideo.com",
    "petco.com", "petsmart.com", "chewy.com",
    "vitaminshoppe.com", "gnc.com", "iherb.com",
    "autozone.com", "advance auto.com", "advanceauto.com",
    "wikipedia.org", "ar.wikipedia.org",
]

SOCIAL_MEDIA_DOMAINS = [
    "pinterest.com", "pinterest.fr", "pinterest.de", "pinterest.co.uk",
    "instagram.com", "facebook.com", "youtube.com", "youtu.be",
    "twitter.com", "x.com", "tiktok.com", "linktr.ee", "t.co", "github.com", "google.com",
    # Blog / community platforms — subdomains ARE user blogs, not stores
    "tumblr.com", "medium.com", "substack.com", "blogspot.com", "blogger.com",
    "wordpress.com", "weebly.com", "wixsite.com", "squarespace.com",
    "reddit.com", "quora.com",
]

def classify_site_type(domain: str, is_wordpress: bool, post_count: int, tech_stack: str = "") -> str:
    domain = domain.lower()

    # 1. Social Media / blog-platform check
    for social in SOCIAL_MEDIA_DOMAINS:
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
    # Check social-media / blog-platform domains (weebly, wordpress.com, wixsite,
    # squarespace, blogspot, blogger, tumblr, medium, substack, reddit, quora, etc.)
    # — same list classify_site_type() uses, so a subdomain on one of these never
    # falls through to the full slow scrape pipeline before being recognized.
    for p in SOCIAL_MEDIA_DOMAINS:
        if domain == p or domain.endswith("." + p):
            return True
    return False

def check_for_blocks(url: str, log_cb: Any) -> tuple[bool, str, str, str]:
    """
    Check if the URL is blocked by Cloudflare, Captcha, or access restriction.
    Returns (is_blocked, reason, homepage_html, final_url)
    homepage_html is the raw response text on success, empty string on block/error.
    final_url is the URL actually reached after following redirects (requests'
    response.url) — falls back to the original `url` if no request ever
    completed. Callers use this to catch domains that redirect straight to a
    known mega-platform (e.g. a typo'd domain like pintrest.com 301-ing to the
    real www.pinterest.com) so they can fast-track that instead of running the
    full sitemap/post-discovery crawl against the platform's real site.
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
            return True, "Connection Timeout", "", url
        except requests.exceptions.RequestException as e:
            return True, f"Connection Failed: {str(e)}", "", url

    final_url = getattr(r, "url", None) or url
    try:
        # Check HTTP status codes commonly used for blocking
        if r.status_code in (403, 429, 503):
            text_lower = r.text.lower()
            if "cloudflare" in text_lower:
                return True, "Cloudflare Block", "", final_url
            elif "captcha" in text_lower or "recaptcha" in text_lower or "hcaptcha" in text_lower:
                return True, "Captcha Block", "", final_url
            elif "access denied" in text_lower or "permission denied" in text_lower:
                return True, "Access Denied", "", final_url
            else:
                return True, f"HTTP Blocked ({r.status_code})", "", final_url

        # Even on 200, it could show a Cloudflare challenge page or Captcha page
        if r.status_code == 200:
            text_lower = r.text.lower()
            content_len = len(r.text)
            # Real Cloudflare challenge pages are short and have specific markers
            if content_len < 30000 and "cloudflare" in text_lower and ("challenge" in text_lower or "enable javascript" in text_lower or "checking your browser" in text_lower):
                return True, "Cloudflare Challenge", "", final_url
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
                    return True, "Captcha Challenge Page", "", final_url

        return False, "", r.text, final_url
    except Exception as e:
        return True, f"Parsing Block Page Failed: {str(e)}", "", final_url


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

def _batch_update_sheets_raw(updates: list):
    """Send a list of {website, fields} updates to Apps Script in one call."""
    cfg = load_webapp_config()
    if not cfg or not updates:
        return
    try:
        r = requests.post(cfg["url"], json={"action": "batch_update_websites",
                                            "secret": cfg.get("secret", "pinterest-scan-2026"),
                                            "updates": updates}, timeout=60)
        r.raise_for_status()
    except Exception:
        pass

def _batch_mark_yes_in_sheets(urls: list):
    """Mark multiple URLs as scrapped='Yes' in one Apps Script call (avoids rate limiting)."""
    cfg = load_webapp_config()
    if not cfg or not urls:
        return
    updates = [{"website": u, "fields": {"scrapped": "Yes"}} for u in urls]
    try:
        r = requests.post(cfg["url"], json={"action": "batch_update_websites",
                                            "secret": cfg.get("secret", "pinterest-scan-2026"),
                                            "updates": updates}, timeout=60)
        r.raise_for_status()
    except Exception:
        pass  # non-critical — DB is already authoritative

# ── Batched async Sheets writer ────────────────────────────────────────────────
# The old implementation spawned a new thread per update, so 30 workers finishing
# simultaneously fired 30+ individual HTTP requests to Apps Script at once →
# all timed out (60 s each) and/or triggered rate-limit 404s.
# This version accumulates updates in a queue and flushes them as a single
# batch_update_websites call every BATCH_WINDOW seconds (or when BATCH_MAX is hit),
# reducing N individual calls to 1 call per time window regardless of concurrency.
_sheets_write_queue          = _queue.Queue()
_sheets_batch_started        = False
_sheets_batch_started_lock   = threading.Lock()

def _sheets_batch_writer():
    BATCH_WINDOW = 3.0   # flush at most every 3 s
    BATCH_MAX    = 50    # or when 50 items accumulate, whichever comes first
    pending  = []
    deadline = time.time() + BATCH_WINDOW
    while True:
        timeout = max(0.05, deadline - time.time())
        try:
            item = _sheets_write_queue.get(timeout=timeout)
            if item is None:
                break
            pending.append(item)
        except _queue.Empty:
            pass
        if pending and (time.time() >= deadline or len(pending) >= BATCH_MAX):
            try:
                batch = [{"website": it["url"], "fields": it["updates"]} for it in pending]
                _batch_update_sheets_raw(batch)
            except Exception:
                pass
            pending.clear()
            deadline = time.time() + BATCH_WINDOW

def _sheets_update_async(url: str, updates: dict):
    """Queue a Sheets update — batched by background thread, never blocks workers."""
    global _sheets_batch_started
    if not _sheets_batch_started:
        with _sheets_batch_started_lock:
            if not _sheets_batch_started:
                threading.Thread(target=_sheets_batch_writer, daemon=True).start()
                _sheets_batch_started = True
    _sheets_write_queue.put({"url": url, "updates": updates})

# ─── Scraper Core & Post Comparison ───────────────────────────────────────────

def _fast_track_marketplace_result(site: dict, domain: str, site_type: str, log_cb: Any) -> dict:
    """Write a fast-tracked Store/Social Media/Link-in-Bio classification without
    running the full scrape/sitemap-discovery pipeline. Shared by the upfront
    domain check and the post-redirect check in run_single_scrape (e.g. a
    typo'd/defensively-registered domain that 301s straight to the real
    platform — pintrest.com -> www.pinterest.com)."""
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


def run_single_scrape(site: dict, provider: str, model: str, log_cb: Any) -> dict:
    domain = extract_domain(site.get("url", ""))

    if is_marketplace_or_social(domain):
        site_type = classify_site_type(domain, False, 0)
        log_cb(f"Fast-tracking marketplace/social domain: {domain} ({site_type})")
        return _fast_track_marketplace_result(site, domain, site_type, log_cb)

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
    is_blocked, block_reason, homepage_html, final_url = check_for_blocks(site["url"], log_cb)

    # ── Redirect escape check ───────────────────────────────────────────────
    # The is_marketplace_or_social() check above only matches the ORIGINAL input
    # domain string. A typo'd or defensively-registered domain (e.g. pintrest.com,
    # missing the 'e') can still 301/302 straight to a known mega-platform's real
    # domain (www.pinterest.com) without ever matching that string check. Left
    # unguarded, this falls through into the full sitemap/post-discovery crawl
    # run against the PLATFORM's real site — e.g. Pinterest's own multi-GB pin
    # sitemaps — which is both pointless (a Social Media platform is never a
    # "blog") and extremely slow. Re-check whatever domain we actually landed on.
    final_domain = extract_domain(final_url) if final_url else domain
    if final_domain != domain and is_marketplace_or_social(final_domain):
        site_type = classify_site_type(final_domain, False, 0)
        log_cb(f"{domain} redirects to {final_domain} — fast-tracking as {site_type} instead of crawling it")
        return _fast_track_marketplace_result(site, domain, site_type, log_cb)

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
        max_workers = int(env_vars.get("MAX_BULK_WORKERS", 20))
    except Exception:
        max_workers = 20

    try:
        batch_size = int(env_vars.get("BATCH_CLAIM_SIZE", 20))
    except Exception:
        batch_size = 20
        
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
            # Automatically update DB status and sheet to failed to avoid infinite loop.
            # This recovery block must never itself raise — get_db_connection() can throw
            # too (e.g. a corrupted local sortpin.db: "database disk image is malformed"),
            # and an uncaught exception here would escape scan_worker and propagate through
            # executor.map(), aborting the ENTIRE bulk batch for every other site still
            # queued behind this one. So the whole recovery attempt is wrapped defensively.
            try:
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
            except Exception as conn_err:
                site_log(f"Warning: Could not record failure in database (unreachable or corrupted?): {conn_err}")
            try:
                _sheets_update_async(site_info["url"], {"scrapped": f"Failed ({str(e)[:50]})"})
            except Exception as sheet_err:
                site_log(f"Warning: Failed to set failed status in sheets: {sheet_err}")
            
        with bulk_lock:
            bulk_job["scanned_sites"] += 1

    first_iteration = True
    _empty_batches = 0   # consecutive batches where all claimed sites were already done
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

            # Filter: only scan blank site_type or Blog — skip already-classified non-blog sites
            # Also deduplicates domains already finished in DB
            try:
                db = get_db_connection()
                db_rows_all = db.execute(
                    "SELECT domain, site_type, status FROM scraped_websites"
                ).fetchall()
                db.close()

                # Map: domain → {site_type, status}
                db_domain_map = {r['domain']: {'site_type': r['site_type'] or '', 'status': r['status'] or ''}
                                 for r in db_rows_all if r['domain']}

                skip_types = {"Store", "Social Media", "Link-in-Bio", "General Website"}
                skip_sites   = []   # already classified non-blog → mark Yes, don't scan
                already_done = []   # status=done/blocked in DB → mark Yes, don't scan
                scan_sites   = []   # blank site_type or Blog → scan

                for s in target_sites:
                    dom = extract_domain(s['url'])
                    info = db_domain_map.get(dom, {})
                    st  = info.get('site_type', '')
                    status = info.get('status', '')

                    if status in ('done', 'blocked'):
                        already_done.append((s, st))
                    elif st in skip_types:
                        # Classified but not yet marked done — skip scanning, mark done
                        skip_sites.append((s, st))
                    else:
                        scan_sites.append(s)   # blank or Blog → scan

                # Batch-mark all non-blog / already-done sites as Yes in Sheets
                mark_yes_updates = []
                for s, st in (already_done + skip_sites):
                    _fields = {"scrapped": "Yes"}
                    if st:
                        _fields["site_type"] = st
                    mark_yes_updates.append({"website": s['url'], "fields": _fields})

                if mark_yes_updates:
                    log(f"Skipped {len(mark_yes_updates)} site(s) (already classified/done) — marking 'Yes' in Sheets.")
                    _threading.Thread(
                        target=lambda u=mark_yes_updates: _batch_update_sheets_raw(u),
                        daemon=True
                    ).start()

                if skip_sites:
                    log(f"  → {len(skip_sites)} non-blog site(s) skipped (Store/Social/General Website).")
                if already_done:
                    log(f"  → {len(already_done)} already-done domain(s) skipped.")

                target_sites = scan_sites

            except Exception as _ded_err:
                log(f"Warning: deduplication/filter check failed: {_ded_err}")

            if not target_sites:
                if not claimed_sites and not resumed_sites:
                    # Sheet returned nothing at all — truly finished
                    log("No pending or resumed websites to scan. Bulk job finished.")
                    break
                # We claimed sites but all were already done in DB
                _empty_batches += 1
                if _empty_batches >= 20:
                    log("20 consecutive batches with no new sites — sheet appears exhausted. Stopping.")
                    break
                log(f"All claimed sites already done (#{_empty_batches}/20) — retrying next batch...")
                continue
            _empty_batches = 0  # reset when we actually have work to do

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

def reset_running_sites() -> int:
    """
    1. Calls the Apps Script reset_running_websites action — single HTTP call
       that scans the sheet once and resets all 'Running' rows to 'Not Yet'.
    2. Resets DB rows with status IN ('running','failed') to 'not_yet'.
    """
    # ── Step 1: batch-reset Google Sheets via single Apps Script call ──────────
    print("  Calling Apps Script to batch-reset all 'Running' rows in Google Sheets…")
    sheets_reset = 0
    sheet_domains: set = set()
    cfg = load_webapp_config()
    if not cfg:
        print("  Warning: google_sheets_webapp.json not configured — Sheets step skipped.")
    else:
        try:
            r = requests.post(
                cfg["url"],
                json={"action": "reset_running_websites",
                      "secret": cfg.get("secret", "pinterest-scan-2026")},
                timeout=120
            )
            data = r.json()
            if data.get("ok"):
                sheets_reset = data.get("reset", 0)
                for raw_url in (data.get("domains") or []):
                    dom = extract_domain(normalize_url(raw_url))
                    if dom:
                        sheet_domains.add(dom)
                print(f"  Google Sheets: reset {sheets_reset} 'Running' row(s) to 'Not Yet'.")
            else:
                print(f"  Sheets error: {data.get('error')}")
        except Exception as e:
            print(f"  Sheets call failed: {e}")

    # ── Step 2: reset DB ────────────────────────────────────────────────────────
    print("  Scanning database for 'running'/'failed' rows…")
    db = get_db_connection()
    db_reset = 0
    try:
        init_bulk_tables(db)
        db_rows = db.execute(
            "SELECT domain, url FROM scraped_websites WHERE status IN ('running','failed')"
        ).fetchall()
        for r in db_rows:
            dom = r['domain']
            try:
                db.execute("UPDATE scraped_websites SET status='not_yet' WHERE domain=?", [dom])
                db_reset += 1
                print(f"    ✓ DB: {dom}")
            except Exception as e:
                print(f"    ✗ DB error for {dom}: {e}")
        db.commit()
        print(f"  Database: reset {db_reset} row(s).")
    except Exception as e:
        print(f"  DB error: {e}")
    finally:
        db.close()

    total = max(sheets_reset, db_reset)
    print(f"\n  Done — {sheets_reset} Sheets row(s) + {db_reset} DB row(s) reset to 'Not Yet'.")
    return total


def fix_missing_site_types() -> int:
    """
    For every row in scraped_websites with a blank/null site_type,
    derive the type from the domain pattern alone (no HTTP request needed)
    and update DB + Google Sheets.
    """
    db = get_db_connection()
    try:
        init_bulk_tables(db)
        rows = db.execute(
            "SELECT domain, url, cms, tech_stack, post_count FROM scraped_websites "
            "WHERE site_type IS NULL OR site_type = '' OR site_type = 'General Website'"
        ).fetchall()
    except Exception as e:
        print(f"  DB error: {e}")
        db.close()
        return 0

    if not rows:
        print("  All rows already have a site_type — nothing to fix.")
        db.close()
        return 0

    print(f"  Found {len(rows)} row(s) with missing/generic site_type. Fixing…\n")
    fixed = 0
    sheets_batch = []   # (url, new_type) — only rows that actually changed

    for r in rows:
        domain    = r['domain']
        cms       = str(r.get('cms') or '')
        tech      = str(r.get('tech_stack') or '')
        posts     = int(r.get('post_count') or 0)
        is_wp     = 'wordpress' in cms.lower() or 'wordpress' in tech.lower()
        site_type = classify_site_type(domain, is_wp, posts, tech)

        # Skip rows that would still be "General Website" — no real change
        if site_type == "General Website":
            continue

        try:
            db.execute(
                "UPDATE scraped_websites SET site_type = ? WHERE domain = ?",
                [site_type, domain]
            )
            url = r.get('url') or f"https://{domain}"
            sheets_batch.append((url, site_type))
            print(f"    {domain}  →  {site_type}")
            fixed += 1
        except Exception as e:
            print(f"    ✗ {domain}: {e}")

    db.commit()
    db.close()

    # Update Sheets in small batches of 5 with a 2-second gap to avoid rate limiting
    if sheets_batch:
        print(f"\n  Updating {len(sheets_batch)} Sheets row(s) in small batches…")
        import time as _time
        chunk = 5
        for i in range(0, len(sheets_batch), chunk):
            group = sheets_batch[i:i + chunk]
            threads = [
                _threading.Thread(
                    target=lambda u=url, t=st: update_website_in_sheets(u, {"site_type": t}),
                    daemon=True
                )
                for url, st in group
            ]
            for t in threads: t.start()
            for t in threads: t.join(timeout=30)
            if i + chunk < len(sheets_batch):
                _time.sleep(2)

    print(f"\n  Done — {fixed} site_type(s) updated (skipped 'General Website' — no change).")
    return fixed


def _quick_blog_check(url: str) -> str:
    """
    Fetch one homepage and classify using HTML byte patterns only — no AI.
    Returns: 'Blog' | 'Store' | 'General Website'
    """
    try:
        try:
            from curl_cffi import requests as _cr
            resp = _cr.get(url, timeout=12, impersonate="chrome110", allow_redirects=True)
            html = resp.content
        except ImportError:
            resp = requests.get(url, timeout=12, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 Chrome/124 Safari/537.36"
            }, allow_redirects=True)
            html = resp.content

        h = html.lower()

        # ── WordPress (strongest blog signal) ──────────────────────────────
        if any(p in h for p in [b'wp-content/', b'wp-includes/', b'wp-json',
                                  b'api.w.org', b'wordpress']):
            return "Blog"

        # ── RSS / Atom feed link ───────────────────────────────────────────
        if b'application/rss+xml' in h or b'application/atom+xml' in h:
            return "Blog"

        # ── Schema.org BlogPosting ─────────────────────────────────────────
        if b'blogposting' in h or b'"@type":"blog"' in h or b'"@type": "blog"' in h:
            return "Blog"

        # ── Ghost CMS ─────────────────────────────────────────────────────
        if b'ghost.io' in h or b'content="ghost"' in h:
            return "Blog"

        # ── Blogger / Blogspot ─────────────────────────────────────────────
        if b'blogger.com/static' in h or b'blogspot.com' in h:
            return "Blog"

        # ── Store signals not caught by earlier layers ──────────────────────
        if any(p in h for p in [b'cdn.shopify.com', b'shopify.theme',
                                  b'bigcommerce.com', b'woocommerce',
                                  b'"add-to-cart"', b'add to cart']):
            return "Store"

        return "General Website"
    except Exception:
        return "General Website"


def recheck_blogs() -> int:
    """
    Fast re-classification of 'General Website' + blank site_type rows.
    Uses homepage HTML pattern matching (no AI). 15 parallel workers.
    """
    import concurrent.futures as _cf

    print("\n=== Re-check Blogs (fast HTML scan — no AI) ===")

    db = get_db_connection()
    try:
        init_bulk_tables(db)
        rows = db.execute(
            "SELECT domain, url, site_type FROM scraped_websites "
            "WHERE site_type IS NULL OR site_type = '' OR site_type = 'General Website'"
        ).fetchall()
    except Exception as e:
        print(f"  DB error: {e}")
        db.close()
        return 0
    db.close()

    if not rows:
        print("  No 'General Website' / blank rows found — all sites already classified.")
        return 0

    print(f"  {len(rows)} site(s) to re-check. 15 parallel workers, ~5-12s each.\n")

    changed = []
    lock = _threading.Lock()
    done_count = [0]

    def check_one(row):
        domain = (row['domain'] or '').strip()
        url    = str(row['url'] or f"https://{domain}").strip()
        if not url or not domain:
            return
        new_type = _quick_blog_check(url)
        with lock:
            done_count[0] += 1
            n = done_count[0]
            total = len(rows)
            if new_type != "General Website":
                print(f"  [{n}/{total}] {domain}  →  {new_type}")
                changed.append((domain, url, new_type))
            elif n % 25 == 0:
                print(f"  [{n}/{total}] still scanning…")

    with _cf.ThreadPoolExecutor(max_workers=15) as ex:
        list(ex.map(check_one, rows))

    print(f"\n  Scan complete — {len(changed)} site(s) reclassified out of {len(rows)} checked.")

    if not changed:
        print("  Nothing new found — sites are genuinely inconclusive without AI.")
        return 0

    # ── Update DB ─────────────────────────────────────────────────────────────
    db = get_db_connection()
    try:
        for domain, url, new_type in changed:
            db.execute("UPDATE scraped_websites SET site_type=? WHERE domain=?",
                       [new_type, domain])
        db.commit()
        print(f"  DB: {len(changed)} row(s) updated.")
    except Exception as e:
        print(f"  DB update error: {e}")
    finally:
        db.close()

    # ── Batch update Sheets ───────────────────────────────────────────────────
    updates = [{"website": url, "fields": {"site_type": new_type}}
               for _, url, new_type in changed]
    _batch_update_sheets_raw(updates)
    print(f"  Sheets: batch update sent ({len(updates)} row(s)).")

    # Print summary
    from collections import Counter
    counts = Counter(t for _, _, t in changed)
    print("\n  Summary of reclassified sites:")
    for t, n in counts.most_common():
        print(f"    {t}: {n}")

    return len(changed)


def mark_stores_done() -> int:
    """
    ONE call to Apps Script — it classifies every Sheet row server-side and
    marks Store/Social/Link-in-Bio rows as scrapped=Yes + site_type in one shot.
    Also updates DB status=done for those domains.
    Requires 'mark_non_blog_rows' action deployed in Apps Script.
    """
    NON_BLOG = {"Store", "Social Media", "Link-in-Bio"}

    print("\n=== Mark Stores / Social / Link-in-Bio as Done ===")

    # ── Step 1: single server-side call — classification + Sheets write ───────
    cfg = load_webapp_config()
    if not cfg:
        print("  Error: no Apps Script config (google_sheets_webapp.json missing).")
        return 0

    print(f"  Sending domain lists to Apps Script "
          f"({len(STORE_DOMAINS)} store + {len(LINK_IN_BIO_DOMAINS)} link-in-bio domains)…")
    sheets_updated = 0
    try:
        r = requests.post(cfg["url"], json={
            "action": "mark_non_blog_rows",
            "secret": cfg.get("secret", "pinterest-scan-2026"),
            "store_domains":    list(STORE_DOMAINS),
            "link_in_bio_domains": list(LINK_IN_BIO_DOMAINS),
        }, timeout=300)
        r.raise_for_status()
        data = r.json()
        if data.get("ok") and "total" in data:
            sheets_updated = data.get("updated", 0)
            total = data.get("total", "?")
            print(f"  Sheets: {sheets_updated} row(s) updated out of {total} total.")
        elif data.get("ok") and "total" not in data:
            print("  Apps Script returned ok but no 'total' field — OLD version is still deployed.")
            print("  → Go to Extensions → Apps Script → Deploy → Manage deployments → Edit → New version → Deploy")
            return 0
        else:
            print(f"  Apps Script error: {data.get('error')} — is the latest version deployed?")
    except Exception as e:
        print(f"  Apps Script call failed: {e}")

    # ── Step 2: update DB — mark known non-blog domains as done ───────────────
    db = get_db_connection()
    db_updated = 0
    try:
        init_bulk_tables(db)
        db_rows = db.execute(
            "SELECT domain, url, cms, tech_stack, post_count, site_type, status "
            "FROM scraped_websites WHERE status != 'done'"
        ).fetchall()
        for row in db_rows:
            domain = (row['domain'] or '').lower().strip()
            if not domain:
                continue
            db_type = str(row.get('site_type') or '').strip()
            cms     = str(row.get('cms') or '')
            tech    = str(row.get('tech_stack') or '')
            posts   = int(row.get('post_count') or 0)
            is_wp   = 'wordpress' in cms.lower() or 'wordpress' in tech.lower()

            if is_marketplace_or_social(domain):
                site_type = classify_site_type(domain, is_wp, posts, tech)
                if site_type == "General Website":
                    site_type = "Store"
            elif db_type in NON_BLOG:
                site_type = db_type
            else:
                continue

            db.execute(
                "UPDATE scraped_websites SET status='done', site_type=? WHERE domain=?",
                [site_type, domain]
            )
            db_updated += 1

        if db_updated:
            db.commit()
    except Exception as e:
        print(f"  DB update error: {e}")
    finally:
        db.close()

    print(f"  DB: {db_updated} row(s) → status='done'.")
    print(f"\n  Done — {sheets_updated} Sheets row(s) + {db_updated} DB row(s) updated.")
    return sheets_updated


def main() -> int:
    parser = argparse.ArgumentParser(description="Standalone Local API for Domain Quick Scrape.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5050)
    parser.add_argument("--run", action="store_true", help="Bulk scan pending domains listed in Google Sheets and exit (this is also the default when no flags are given)")
    parser.add_argument("--runjobforall", action="store_true", help="Bulk scan all domains listed in Google Sheets and exit")
    parser.add_argument("--serve", action="store_true", help="Start the Flask API server instead of bulk-scanning")
    parser.add_argument("--reset-running", action="store_true",
                        help="Reset all 'Running' sites to 'Not Yet' in DB and Google Sheets, then exit")
    parser.add_argument("--fix-site-types", action="store_true",
                        help="Backfill missing/generic site_type in DB and Sheets, then exit")
    parser.add_argument("--mark-stores", action="store_true",
                        help="Mark all Store/Social/Link-in-Bio sites as done in DB and Sheets, then exit")
    parser.add_argument("--recheck-blogs", action="store_true",
                        help="Fast HTML pattern scan of all General Website rows to find blogs (no AI)")
    args = parser.parse_args()

    os.makedirs(RESULTS_DIR, exist_ok=True)

    # Reset stuck Running sites and exit
    if args.reset_running:
        print("=== Reset Running Sites ===")
        reset_running_sites()
        return 0

    # Backfill missing site_type
    if args.fix_site_types:
        print("=== Fix Missing site_type ===")
        fix_missing_site_types()
        return 0

    # Mark stores/social/link-in-bio as done
    if args.mark_stores:
        mark_stores_done()
        return 0

    # Fast HTML re-check for General Website rows
    if args.recheck_blogs:
        recheck_blogs()
        return 0

    # Start Flask API server (must be requested explicitly via --serve)
    if args.serve:
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

    # Run in CLI Mode — default when no flags given (same as --run), or --runjobforall for a full sweep
    if not args.run and not args.runjobforall:
        print("No flags given — defaulting to --run (bulk scan pending domains). Use --serve to start the API server instead.")
    print(f"Starting Bulk Scraper CLI Mode (run_all={args.runjobforall})...")
    run_bulk_scrape_job(run_all=args.runjobforall)
    return 0

if __name__ == "__main__":
    sys.exit(main())
