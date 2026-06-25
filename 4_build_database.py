"""
STEP 4 — Build a local relational database from SortPin data
============================================================
Turns the SortPin extension's data (pinners / boards / pins) into a clean,
RELATIONAL local database you can query and browse:

        PINNER  ──1:N──►  BOARD  ──1:N──►  PIN
          │                                 ▲
          └──────────── 1:N ────────────────┘   (a pinner's pins)

OUTPUTS (written next to this script):
  • sortpin.db          → SQLite database (tables: pinners, boards, pins)
  • sortpin_data.json   → same data, nested Pinner → Boards → Pins (for the viewer)

DATA SOURCES (tried in this order):
  1. LIVE from the SortPin extension in Brave (best-effort, needs Brave + CDP).
       python 4_build_database.py --live
  2. The extension's CSV exports placed in this folder (the reliable path):
       SortPin.com_all_leads_*.csv
       SortPin.com_all_boards_*.csv
       SortPin.com_all_pins_*.csv
     (In the extension popup: Pins / Boards / Pinners → Export — drop the 3
      CSVs into this folder, then run `python 4_build_database.py`.)

Run:
  python 4_build_database.py            # build from the newest CSV exports here
  python 4_build_database.py --live     # try the live extension first, else CSV
"""

import os, sys, csv, json, re, glob, sqlite3, socket, subprocess, time
from datetime import datetime

csv.field_size_limit(10_000_000)              # SortPin rows have huge fields

BASE      = os.path.dirname(os.path.abspath(__file__))
# All scraped data is saved here (SQLite + importable MySQL .sql). Keep this folder!
DB_DIR    = os.path.join(BASE, "IMPORTANT_DATABASE")
DB_PATH   = os.path.join(BASE, "sortpin.db")            # SQLite (also copied to DB_DIR)
JSON_PATH = os.path.join(BASE, "sortpin_data.json")
MYSQL_PATH= os.path.join(DB_DIR, "sortpin_mysql.sql")   # import into MySQL/phpMyAdmin
CDP_PORT  = 9222
EXT_ID    = "djcledakkebdgjncnemijiabiaimbaic"   # SortPin extension id
BRAVE_PATH = r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"

# ── small helpers ─────────────────────────────────────────────────────────────
def _int(v, default=0):
    try:
        if v is None or v == "":
            return default
        return int(float(v))
    except (ValueError, TypeError):
        return default

def _clean(v):
    if v is None:
        return ""
    return str(v).strip()

_PIN_ID_RE = re.compile(r"/pin/(\d+)")
def pin_id_from_url(pin_url, fallback=""):
    """The CSV 'id' column is mangled by Excel into 1.00001E+18 — recover the
    real id from the pin_url (.../pin/<id>)."""
    m = _PIN_ID_RE.search(pin_url or "")
    if m:
        return m.group(1)
    fb = _clean(fallback)
    return fb if (fb and "E+" not in fb.upper()) else ""

def read_csv(path):
    if not path or not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))

def read_all(pattern):
    """Read & concatenate EVERY CSV matching pattern, oldest→newest by mtime.
    Merging all exports (from any computer) lets the DB accumulate across
    machines; duplicates are collapsed later by primary key (newest wins)."""
    files = sorted(glob.glob(os.path.join(BASE, pattern)),
                   key=lambda p: os.path.getmtime(p))
    rows = []
    for fp in files:
        rows += read_csv(fp)
    return rows, files

# ── normalisation: raw rows → pinners / boards / pins ─────────────────────────
def normalize(leads, boards, pins):
    """Return dict(pinners=..., boards=..., pins=...) keyed for relational use."""
    pinners = {}   # username -> pinner dict

    def ensure_pinner(username, name=""):
        username = _clean(username)
        if not username:
            return None
        p = pinners.get(username)
        if p is None:
            p = {
                "username": username, "full_name": _clean(name), "website_url": "",
                "domain_url": "", "contact_email": "", "contact_phone": "",
                "image_url": "", "board_count": 0, "follower_count": 0,
                "following_count": 0, "pin_count": 0, "profile_reach": 0,
                "profile_views": 0, "last_pin_at": "",
            }
            pinners[username] = p
        elif name and not p["full_name"]:
            p["full_name"] = _clean(name)
        return p

    # 1) Leads are the master pinner records (richest: contact info + stats)
    for l in leads:
        p = ensure_pinner(l.get("username"), l.get("full_name"))
        if not p:
            continue
        for k, v in l.items():                  # KEEP every lead field
            if k == "username":
                continue
            if v not in (None, ""):
                p[k] = v
        # canonical fields the viewer/sort rely on
        p["full_name"]      = _clean(l.get("full_name")) or p.get("full_name", "")
        p["contact_email"]  = _clean(l.get("contact_email")) or p.get("contact_email", "")
        p["last_pin_at"]    = _clean(l.get("lastPinAt") or l.get("last_pin_at") or p.get("last_pin_at", ""))
        for f in ("board_count", "follower_count", "following_count", "pin_count",
                  "profile_reach", "profile_views"):
            p[f] = _int(l.get(f) if l.get(f) not in (None, "") else p.get(f))
        if not p.get("image_url"):
            p["image_url"] = _clean(l.get("image_medium_url")) or _clean(l.get("image_small_url"))

    # 2) Boards (link to pinner via owner_username) — dedup by board id
    board_by_url = {}
    boards_by_id = {}                 # id -> board (newest occurrence wins)
    for b in boards:
        owner = _clean(b.get("owner_username"))
        p = ensure_pinner(owner, b.get("owner_full_name"))
        if p and not p["image_url"]:
            p["image_url"] = _clean(b.get("owner_image_medium_url")) or \
                             _clean(b.get("owner_image_small_url"))
        rec = dict(b)                           # KEEP every field from the source
        rec["id"]             = _clean(b.get("id"))
        rec["owner_username"] = owner or None
        rec["modified_at"]    = _clean(b.get("modifiedAt") or b.get("modified_at"))
        for f in ("follower_count", "section_count", "pin_count", "collaborator_count"):
            if f in rec:
                rec[f] = _int(rec.get(f))
        if not rec["id"]:
            continue
        boards_by_id[rec["id"]] = rec          # dedup: newest export wins
        if rec["url"]:
            board_by_url[rec["url"]] = rec["id"]
    out_boards = list(boards_by_id.values())

    # 3) Pins (link to pinner via pinner_username, to board via board_url)
    pins_by_id = {}                            # pin id -> pin (newest wins)
    for pn in pins:
        pinner = _clean(pn.get("pinner_username"))
        ensure_pinner(pinner, pn.get("pinner_name"))
        pid = pin_id_from_url(pn.get("pin_url"), pn.get("id"))
        if not pid:
            continue
        burl = _clean(pn.get("board_url"))
        rec = dict(pn)                          # KEEP every field from the source
        rec["id"]              = pid
        rec["board_url"]       = burl
        rec["board_id"]        = board_by_url.get(burl) or None
        rec["pinner_username"] = pinner or None
        for f in ("saves", "repin_count", "comment_count", "like_count", "share_count"):
            if f in rec:
                rec[f] = _int(rec.get(f))
        pins_by_id[pid] = rec
    out_pins = list(pins_by_id.values())

    return {"pinners": list(pinners.values()),
            "boards":  out_boards,
            "pins":    out_pins}

# ── dynamic schema: keep EVERY field; build columns from the data itself ──────
_TABLE_PK = {"pinners": "username", "boards": "id", "pins": "id"}

def _force_text(col):
    """Columns that must stay TEXT even if they look numeric (ids, keys, urls…)."""
    low = col.lower()
    return (col in ("id", "username", "board_id", "owner_username", "pinner_username")
            or "username" in low or low.endswith("_url") or low.endswith("_at")
            or low in ("pin_url", "link", "image", "images", "video", "videos",
                       "dominant_color", "reaction_counts", "node_id", "phone", "contact_phone"))

def _columns_for(pk, rows):
    cols = [pk]
    for r in rows:
        for k in r.keys():
            if k not in cols:
                cols.append(k)
    return cols

def _numeric_cols(cols, pk, rows):
    num = set()
    for c in cols:
        if c == pk or _force_text(c):
            continue
        seen = False
        ok = True
        for r in rows:
            v = r.get(c)
            if v in (None, ""):
                continue
            try:
                int(float(v)); seen = True
            except (ValueError, TypeError):
                ok = False; break
        if ok and seen:
            num.add(c)
    return num

def write_sqlite(data):
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    con = sqlite3.connect(DB_PATH)
    for table, pk in _TABLE_PK.items():
        rows = data[table]
        cols = _columns_for(pk, rows)
        num = _numeric_cols(cols, pk, rows)
        defs = ", ".join(
            f'"{c}" {"INTEGER" if c in num else "TEXT"}' + (" PRIMARY KEY" if c == pk else "")
            for c in cols)
        con.execute(f'CREATE TABLE "{table}" ({defs})')
        ph = ", ".join("?" * len(cols))
        def cellval(r, c):
            v = r.get(c)
            if c in num:
                return _int(v)
            return None if v is None else str(v)
        con.executemany(f'INSERT OR REPLACE INTO "{table}" VALUES ({ph})',
                        [[cellval(r, c) for c in cols] for r in rows])
    for sql in ("CREATE INDEX IF NOT EXISTS idx_boards_owner ON boards(owner_username)",
                "CREATE INDEX IF NOT EXISTS idx_pins_pinner  ON pins(pinner_username)",
                "CREATE INDEX IF NOT EXISTS idx_pins_board   ON pins(board_id)"):
        try: con.execute(sql)
        except Exception: pass            # column may be absent if a table is empty
    con.commit()
    con.close()

# ── write a MySQL-importable .sql dump (dynamic columns, no live connection) ──
def _sql_val(v, is_int):
    if v is None or v == "":
        return "0" if is_int else "''"
    if is_int:
        try: return str(int(float(v)))
        except (ValueError, TypeError): return "0"
    s = str(v).replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n").replace("\r", "")
    return "'" + s + "'"

def write_mysql_dump(data):
    os.makedirs(DB_DIR, exist_ok=True)
    with open(MYSQL_PATH, "w", encoding="utf-8") as f:
        f.write("-- SortPin scraped data — import into MySQL:\n")
        f.write("--   mysql -u USER -p DBNAME < sortpin_mysql.sql   (or phpMyAdmin → Import)\n")
        f.write(f"-- generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("SET NAMES utf8mb4;\nSET FOREIGN_KEY_CHECKS=0;\n\n")
        for table in ("pins", "boards", "pinners"):
            f.write(f"DROP TABLE IF EXISTS `{table}`;\n")
        meta = {}
        for table, pk in _TABLE_PK.items():
            rows = data[table]
            cols = _columns_for(pk, rows)
            num = _numeric_cols(cols, pk, rows)
            meta[table] = (cols, num, rows)
            defs = ",\n  ".join(
                f"`{c}` " + ("BIGINT" if c in num else
                             ("VARCHAR(190)" if c == pk else "LONGTEXT"))
                for c in cols)
            f.write(f"\nCREATE TABLE `{table}` (\n  {defs},\n  PRIMARY KEY (`{pk}`)\n"
                    f") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;\n")
        f.write("\nSET FOREIGN_KEY_CHECKS=1;\n")
        for table in ("pinners", "boards", "pins"):
            cols, num, rows = meta[table]
            if not rows:
                continue
            collist = ", ".join(f"`{c}`" for c in cols)
            f.write(f"\n-- {len(rows)} {table}\n")
            for i in range(0, len(rows), 400):
                f.write(f"INSERT INTO `{table}` ({collist}) VALUES\n")
                vals = ["(" + ", ".join(_sql_val(r.get(c), c in num) for c in cols) + ")"
                        for r in rows[i:i+400]]
                f.write(",\n".join(vals) + ";\n")
    # also keep a copy of the SQLite db next to it
    try:
        import shutil
        shutil.copy2(DB_PATH, os.path.join(DB_DIR, os.path.basename(DB_PATH)))
    except Exception:
        pass

# ── write FLAT JSON (pinners / boards / pins arrays) for the viewer ───────────
def write_json(data):
    # per-pinner counts so the Pinners tab can show/sort them
    n_boards = {}; n_pins = {}
    for b in data["boards"]:
        if b.get("owner_username"):
            n_boards[b["owner_username"]] = n_boards.get(b["owner_username"], 0) + 1
    for p in data["pins"]:
        if p.get("pinner_username"):
            n_pins[p["pinner_username"]] = n_pins.get(p["pinner_username"], 0) + 1

    pinners = []
    for p in data["pinners"]:
        u = p["username"]
        pinners.append({
            "username": u, "full_name": p.get("full_name", ""),
            "image_url": p.get("image_url", ""), "website_url": p.get("website_url", ""),
            "domain_url": p.get("domain_url", ""), "contact_email": p.get("contact_email", ""),
            "follower_count": p.get("follower_count", 0), "pin_count": p.get("pin_count", 0),
            "board_count": p.get("board_count", 0), "profile_reach": p.get("profile_reach", 0),
            "nb": n_boards.get(u, 0), "np": n_pins.get(u, 0),
        })
    pinners.sort(key=lambda x: (x["np"], x["follower_count"]), reverse=True)

    boards = sorted(data["boards"], key=lambda b: b.get("pin_count", 0), reverse=True)
    pins   = sorted(data["pins"],   key=lambda p: p.get("repin_count", 0), reverse=True)

    out = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "stats": {"pinners": len(data["pinners"]), "boards": len(data["boards"]),
                  "pins": len(data["pins"])},
        "pinners": pinners,
        "boards":  boards,
        "pins":    pins,
    }
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)

# ── data source 1: LIVE from the SortPin extension in Brave ───────────────────
def _cdp_up():
    try:
        s = socket.create_connection(("127.0.0.1", CDP_PORT), timeout=1); s.close()
        return True
    except OSError:
        return False

def _ensure_brave_cdp():
    """Attach to a Brave already on the debug port, or launch one (default
    profile, so the SortPin extension + its stored data are present)."""
    if _cdp_up():
        print(f"  (live) Brave already on CDP :{CDP_PORT}")
        return True
    if not os.path.exists(BRAVE_PATH):
        print(f"  (live) Brave not found at {BRAVE_PATH}")
        return False
    print(f"  (live) launching Brave with debug port {CDP_PORT}...")
    subprocess.run(["taskkill", "/F", "/IM", "brave.exe"], capture_output=True)
    time.sleep(2)
    subprocess.Popen([BRAVE_PATH, f"--remote-debugging-port={CDP_PORT}",
                      "--no-first-run", "--no-default-browser-check"])
    for _ in range(15):
        if _cdp_up():
            time.sleep(2); return True
        time.sleep(1)
    return False

# STEP 1 (runs in the extension page): cache every data array in page memory
# (window.__src) and return ONLY metadata (name, length, field keys). This keeps
# the round-trip tiny even when the database is huge — we slice the data out in
# chunks afterwards instead of returning it all at once (that caused the timeout).
_META_JS = r"""
const done = arguments[arguments.length - 1];
(async () => {
  window.__src = {};
  const errors = [];
  try {
    if (typeof chrome!=='undefined' && chrome.storage && chrome.storage.local) {
      const all = await new Promise(r => chrome.storage.local.get(null, d => r(d||{})));
      for (const k in all) {
        const v = all[k];
        if (Array.isArray(v) && v.length && typeof v[0]==='object') window.__src['storage:'+k]=v;
        else if (v && typeof v==='object') {
          for (const k2 in v) {
            const v2 = v[k2];
            if (Array.isArray(v2) && v2.length && typeof v2[0]==='object') window.__src['storage:'+k+'.'+k2]=v2;
          }
        }
      }
    }
  } catch(e){ errors.push('storage:'+e); }
  const meta = {};
  // storage arrays are small/config — keep them cached for slicing
  for (const n in window.__src){ const a = window.__src[n]; meta[n] = {len:a.length, keys:Object.keys(a[0]||{}), src:'storage'}; }
  // IndexedDB: only COUNT + sample first row's keys here (no getAll → no hang).
  // The actual rows are streamed later with a cursor, chunk by chunk.
  try {
    const dbs = (indexedDB.databases ? await indexedDB.databases() : []);
    for (const dbmeta of dbs) {
      const db = await new Promise((res,rej)=>{const r=indexedDB.open(dbmeta.name);
        r.onsuccess=()=>res(r.result); r.onerror=()=>rej(r.error);});
      for (const s of Array.from(db.objectStoreNames)) {
        const tx = db.transaction(s,'readonly'); const osx = tx.objectStore(s);
        const cnt = await new Promise(res=>{const rq=osx.count(); rq.onsuccess=()=>res(rq.result||0); rq.onerror=()=>res(0);});
        const sampleKeys = await new Promise(res=>{const rq=osx.openCursor();
          rq.onsuccess=e=>{const c=e.target.result; res(c?Object.keys(c.value||{}):[]);}; rq.onerror=()=>res([]);});
        if (cnt > 0) meta['idb:'+dbmeta.name+'/'+s] = {len:cnt, keys:sampleKeys, src:'idb',
                                                       db:dbmeta.name, store:s};
      }
      db.close();
    }
  } catch(e){ errors.push('idb:'+e); }
  done({meta:meta, errors:errors});
})();
"""

# STEP 2b (async): stream ONE chunk of an IndexedDB store via a cursor, resuming
# after the last primary key — O(n) total, never loads the whole store at once.
_READ_IDB_CHUNK_JS = r"""
const dbName = arguments[0], store = arguments[1], afterKey = arguments[2],
      count = arguments[3], done = arguments[4];
(async () => {
  // keep scalars, small nested, AND the relationship objects we need to link
  const KEEP = {pinner:1, board:1, owner:1, native_creator:1, origin_pinner:1, pinned_to_board:1};
  function trim(o){
    if (o===null || typeof o!=='object') return o;
    const r = {};
    for (const k in o){ const v=o[k], t=typeof v;
      if (v===null||t==='string'||t==='number'||t==='boolean') r[k]=v;
      else if (KEEP[k] && v && typeof v==='object') {
        // keep just the linking sub-fields of the related object
        r[k] = {username:v.username, full_name:v.full_name, url:v.url, id:v.id,
                name:v.name, image_medium_url:v.image_medium_url};
      }
      else { try { const s=JSON.stringify(v); if (s && s.length<=800) r[k]=v; } catch(e){} } }
    return r;
  }
  try {
    const db = await new Promise((res,rej)=>{const r=indexedDB.open(dbName);
      r.onsuccess=()=>res(r.result); r.onerror=()=>rej(r.error);});
    const osx = db.transaction(store,'readonly').objectStore(store);
    const range = (afterKey===null||afterKey===undefined) ? null
                  : IDBKeyRange.lowerBound(afterKey, true);
    const out = []; let lastKey = null;
    await new Promise(res=>{
      const rq = osx.openCursor(range);
      rq.onsuccess = e => { const cur = e.target.result;
        if (!cur || out.length>=count){ res(); return; }
        out.push(cur.value); lastKey = cur.primaryKey; cur.continue(); };
      rq.onerror = () => res();
    });
    db.close();
    done({rows: out.map(trim), lastKey: lastKey});
  } catch(e){ done({__error:String(e)}); }
})();
"""

# STEP 2 (sync): return one trimmed slice of a cached array. Big nested fields
# (image-size maps, videos, etc.) are dropped so each chunk stays small; scalar
# fields we actually need (pin_url, title, image, owner_username, …) are kept.
_SLICE_JS = r"""
const name = arguments[0], start = arguments[1], count = arguments[2];
const a = (window.__src && window.__src[name]) || [];
function trim(o){
  if (o===null || typeof o!=='object') return o;
  const r = {};
  for (const k in o){
    const v = o[k], t = typeof v;
    if (v===null || t==='string' || t==='number' || t==='boolean') r[k]=v;
    else { try { const s = JSON.stringify(v); if (s && s.length<=800) r[k]=v; } catch(e){} }
  }
  return r;
}
return a.slice(start, start+count).map(trim);
"""

def _classify_keys(keys):
    keys = set(keys)
    if ("pin_url" in keys) or ("pinner_username" in keys) or ("repin_count" in keys):
        return "pins"
    if ("owner_username" in keys) or ("image_cover_url" in keys) or ("section_count" in keys):
        return "boards"
    if ("contact_email" in keys) or ("profile_reach" in keys) or \
       ("website_url" in keys and "username" in keys):
        return "leads"
    return None

def try_live_extension():
    """Read SortPin's data live from the extension in Brave, in chunks so it
    works even for a huge database. Returns (leads, boards, pins) or None.
    Self-diagnoses by printing every data container it finds."""
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
    except Exception:
        print("  (live) selenium not installed — run: pip install selenium")
        return None
    if not _ensure_brave_cdp():
        print("  (live) could not get Brave on the debug port — falling back to CSV")
        return None
    try:
        opts = Options()
        opts.add_experimental_option("debuggerAddress", f"127.0.0.1:{CDP_PORT}")
        driver = webdriver.Chrome(options=opts)
    except Exception as e:
        print(f"  (live) could not attach Selenium to Brave — {e}")
        return None

    try:
        driver.set_page_load_timeout(60)
        driver.set_script_timeout(600)          # huge DBs need a long timeout
        driver.switch_to.new_window("tab")
        # one lightweight load into the extension origin (enough to reach its
        # chrome.storage + IndexedDB); we don't need the heavy data UI to render
        try:
            driver.get(f"chrome-extension://{EXT_ID}/popup.html")
        except Exception as e:
            print(f"  (live) could not open extension page — {e}")
        time.sleep(3)
        print("  (live) scanning extension storage (counts only)...")
        res = driver.execute_async_script(_META_JS)   # tiny: metadata only
    except Exception as e:
        print(f"  (live) extraction failed — {e}")
        try: driver.quit()
        except Exception: pass
        return None

    meta = (res or {}).get("meta") or {}
    errs = (res or {}).get("errors") or []
    if not meta:
        print(f"  (live) no data arrays found in the extension. {errs}")
        return None

    print("  (live) data containers found in the extension:")
    for name, info in sorted(meta.items(), key=lambda kv: -kv[1]["len"]):
        print(f"        {name:<42} {info['len']:>7} rows  → {(_classify_keys(info['keys']) or '?')}")

    # pull each relevant container in CHUNKS (bounded transfers)
    picked = {"leads": None, "boards": None, "pins": None}
    CH = 1000
    for name, info in sorted(meta.items(), key=lambda kv: -kv[1]["len"]):
        kind = _classify_keys(info["keys"])
        if not kind or picked[kind] is not None:
            continue
        n = info["len"]; rows = []
        if info.get("src") == "idb":
            # stream the IndexedDB store with a cursor (no full load → no hang)
            after = None
            while True:
                try:
                    r = driver.execute_async_script(
                        _READ_IDB_CHUNK_JS, info["db"], info["store"], after, CH)
                except Exception as e:
                    print(f"\n  (live) idb chunk failed — {e}")
                    break
                if isinstance(r, dict) and r.get("__error"):
                    print(f"\n  (live) idb read error — {r['__error']}")
                    break
                chunk = (r or {}).get("rows") or []
                if not chunk:
                    break
                rows += chunk
                after = r.get("lastKey")
                print(f"\r  (live) reading {name} as {kind} ({len(rows)}/{n})   ", end="", flush=True)
                if len(chunk) < CH:
                    break
        else:
            # storage array cached in window.__src — slice it out
            start = 0
            while start < n:
                try:
                    chunk = driver.execute_script(_SLICE_JS, name, start, CH)
                except Exception as e:
                    print(f"\n  (live) chunk failed at {start} — {e}")
                    break
                if not chunk:
                    break
                rows += chunk
                start += len(chunk)
                print(f"\r  (live) reading {name} as {kind} ({len(rows)}/{n})   ", end="", flush=True)
        print()
        # flatten raw Pinterest objects → flat schema the linker expects
        picked[kind] = [_flatten_raw(kind, r) for r in rows]

    try: driver.close()
    except Exception: pass

    if any(picked.values()):
        return (picked["leads"] or [], picked["boards"] or [], picked["pins"] or [])
    print("  (live) found data but couldn't classify it — falling back to CSV")
    return None

# ── persist a live pull to timestamped CSVs (so data survives clearing) ────────
def save_snapshot(leads, boards, pins):
    """Write the live-pulled rows to timestamped CSVs in SortPin's naming so the
    data is preserved on disk, git-syncs across computers, and is re-merged by
    later runs even after the extension is cleared (step 6)."""
    ts = datetime.now().strftime("%Y-%m-%d %H-%M-%S")
    written = []
    for rows, label in ((leads, "leads"), (boards, "boards"), (pins, "pins")):
        if not rows:
            continue
        cols = []
        seen = set()
        for r in rows:                       # union of keys, stable order
            for k in r.keys():
                if k not in seen:
                    seen.add(k); cols.append(k)
        path = os.path.join(BASE, f"SortPin.com_all_{label}_live {ts}.csv")
        with open(path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in cols})
        written.append(os.path.basename(path))
    if written:
        print(f"  (live) saved snapshot: {', '.join(written)}")
    return written

# ── data source 1b: DISK — read the extension's IndexedDB files (no browser) ───
def _idb_leveldb_dirs():
    ud = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                      "BraveSoftware", "Brave-Browser", "User Data")
    out = []
    for prof in glob.glob(os.path.join(ud, "*")):
        p = os.path.join(prof, "IndexedDB",
                         f"chrome-extension_{EXT_ID}_0.indexeddb.leveldb")
        if os.path.isdir(p):
            out.append(p)
    return out

def _archive_leveldb_dirs():
    """Every SortPin IndexedDB folder backed up by step 6 under _SORTPIN_ARCHIVE/
    (all timestamps → all historical data, merged & de-duplicated by the build)."""
    archive = os.path.join(BASE, "_SORTPIN_ARCHIVE")
    out = []
    if os.path.isdir(archive):
        for root, dirs, _files in os.walk(archive):
            for d in dirs:
                if d == f"chrome-extension_{EXT_ID}_0.indexeddb.leveldb":
                    out.append(os.path.join(root, d))
    return out

def _trim_row(o):
    """Keep scalar fields + small nested ones; drop big blobs (images maps…)."""
    if not isinstance(o, dict):
        return o
    r = {}
    for k, v in o.items():
        if v is None or isinstance(v, (str, int, float, bool)):
            r[k] = v
        else:
            try:
                if len(json.dumps(v, default=str)) <= 800:
                    r[k] = v
            except Exception:
                pass
    return r

def _sub(d, *keys):
    """Safe nested getter: _sub(obj, 'username') from a nested object/dict."""
    if not isinstance(d, dict):
        return ""
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return ""

def _best_image(o):
    """Reconstruct a pin image URL from the raw object. Prefer an actual size
    URL from `images`; otherwise build a 736x URL from the signature (the 736x
    size almost always exists, unlike 'originals' which often 404s)."""
    imgs = o.get("images")
    if isinstance(imgs, dict):
        for key in ("736x", "orig", "564x", "474x", "236x"):
            if isinstance(imgs.get(key), dict) and imgs[key].get("url"):
                return imgs[key]["url"]
    sig = o.get("image_signature") or o.get("image_signature_unique")
    if isinstance(sig, str) and len(sig) >= 6:
        return f"https://i.pinimg.com/736x/{sig[0:2]}/{sig[2:4]}/{sig[4:6]}/{sig}.jpg"
    return ""

def _flatten_raw(kind, o):
    """Map a RAW Pinterest IndexedDB object to the flat schema the linker/CSV
    use, pulling owner/pinner/board out of their nested objects."""
    if not isinstance(o, dict):
        return {}
    if kind == "pins":
        pinner = o.get("pinner") or o.get("native_creator") or o.get("origin_pinner") or {}
        board  = o.get("board") or o.get("pinned_to_board") or {}
        pid = str(o.get("id") or "")
        puser = _sub(pinner, "username")
        rc = o.get("reaction_counts")
        return {
            "id": pid,
            "pin_url": f"https://www.pinterest.com/pin/{pid}" if pid else "",
            "title": o.get("title") or o.get("grid_title") or "",
            "description": o.get("description") or "",
            "link": o.get("link") or o.get("mobile_link") or "",
            "image": _best_image(o),
            "images": o.get("image_signature") or "",
            "video": 1 if o.get("is_video") else 0,
            "saves": o.get("saves") or o.get("repin_count") or 0,
            "repin_count": o.get("repin_count") or 0,
            "comment_count": o.get("comment_count") or 0,
            "like_count": 0,
            "share_count": o.get("share_count") or 0,
            "reaction_counts": (json.dumps(rc) if isinstance(rc, (dict, list)) else (rc or "")),
            "created_at": o.get("created_at") or o.get("createdAt") or "",
            "updated_at": o.get("updated_at") or o.get("updatedAt") or "",
            # board (denormalised)
            "board_url": _sub(board, "url"),
            "board_name": _sub(board, "name"),
            "board_pin_count": _sub(board, "pin_count") or 0,
            "board_follower_count": _sub(board, "follower_count") or 0,
            "board_privacy": _sub(board, "privacy"),
            "board_category": _sub(board, "category"),
            # pinner (denormalised)
            "pinner_username": puser,
            "pinner_name": _sub(pinner, "full_name"),
            "pinner_url": f"https://www.pinterest.com/{puser}/" if puser else "",
            "pinner_pin_count": _sub(pinner, "pin_count") or 0,
            "pinner_board_count": _sub(pinner, "board_count") or 0,
            "pinner_follower_count": _sub(pinner, "follower_count") or 0,
            "pinner_following_count": _sub(pinner, "following_count") or 0,
        }
    if kind == "boards":
        owner = o.get("owner") or {}
        return {
            "id": str(o.get("id") or ""),
            "name": o.get("name") or "",
            "description": o.get("description") or "",
            "url": o.get("url") or "",
            "image_cover_url": o.get("image_cover_url") or "",
            "follower_count": o.get("follower_count") or 0,
            "section_count": o.get("section_count") or 0,
            "pin_count": o.get("pin_count") or 0,
            "category": o.get("category") or "",
            "privacy": o.get("privacy") or "",
            "modifiedAt": o.get("modifiedAt") or o.get("createdAt") or "",
            "owner_username": _sub(owner, "username"),
            "owner_full_name": _sub(owner, "full_name"),
            "owner_image_medium_url": _sub(owner, "image_medium_url", "image_small_url"),
        }
    if kind == "leads":
        return {
            "username": o.get("username") or "",
            "full_name": o.get("full_name") or "",
            "website_url": o.get("website_url") or o.get("listed_website_url") or "",
            "domain_url": o.get("domain_url") or "",
            "contact_email": o.get("contact_email") or "",   # not present in raw
            "contact_phone": o.get("contact_phone") or "",
            "board_count": o.get("board_count") or 0,
            "follower_count": o.get("follower_count") or 0,
            "following_count": o.get("following_count") or 0,
            "pin_count": o.get("pin_count") or 0,
            "profile_reach": o.get("profile_reach") or 0,
            "profile_views": o.get("profile_views") or o.get("profile_view") or 0,
            "lastPinAt": o.get("lastPinAt") or "",
            "image_medium_url": o.get("image_medium_url") or o.get("image_small_url") or "",
        }
    return {}

def _disk_rows_ccl(folder):
    """Read all object-store records from a leveldb folder via ccl_chromium_reader
    (pure-Python, no compiler). Yields dict values."""
    from ccl_chromium_reader import ccl_chromium_indexeddb as _idb
    wrapper = _idb.WrappedIndexDB(folder)
    for dbid in wrapper.database_ids:
        db = wrapper[dbid.dbid_no]
        for store_name in list(db.object_store_names):
            try:
                store = db.get_object_store_by_name(store_name)
            except Exception:
                continue
            for rec in store.iterate_records(
                    live_only=True,
                    bad_deserializer_data_handler=lambda k, d: None):
                v = getattr(rec, "value", None)
                if isinstance(v, dict):
                    yield v

def _disk_rows_df(folder):
    """Read records via dfindexeddb. It can't decode some newer Pinterest records
    ('Unsupported header') and floods those errors — we silence its output and
    skip the records it can't parse. (ccl_chromium_reader handles more — prefer it.)"""
    import pathlib, os as _os, contextlib
    from dfindexeddb.indexeddb.chromium import record as _cr
    rows = []
    with open(_os.devnull, "w") as devnull, \
         contextlib.redirect_stdout(devnull), contextlib.redirect_stderr(devnull):
        try:
            it = _cr.FolderReader(pathlib.Path(folder)).GetRecords(use_manifest=True, load_blobs=False)
        except Exception:
            it = _cr.FolderReader(pathlib.Path(folder)).GetRecords(load_blobs=False)
        while True:                               # buffer inside the silence
            try:
                rec = next(it)
            except StopIteration:
                break
            except Exception:
                continue                          # skip a record it can't decode
            v = getattr(rec, "value", None)
            if isinstance(v, dict):
                rows.append(v)
    for v in rows:                                # yield after — caller output not muted
        yield v

def try_disk_extension():
    """Read SortPin's data straight from Brave's IndexedDB files on disk — no
    browser, no CDP. Uses ccl_chromium_reader (pure-Python, recommended) or
    dfindexeddb if present. Reads the folder in place; if files are locked
    (Brave open) it copies them to a temp folder. Returns (leads,boards,pins)."""
    import shutil, tempfile
    # pick an available backend
    reader = None
    try:
        import ccl_chromium_reader  # noqa: F401
        reader = _disk_rows_ccl
        print("  (disk) using ccl_chromium_reader (pure-Python)")
    except Exception:
        try:
            import dfindexeddb  # noqa: F401
            reader = _disk_rows_df
            print("  (disk) using dfindexeddb")
        except Exception:
            print("  (disk) no disk parser installed. Recommended (no compiler):")
            print("        python -m pip install ccl_chromium_reader")
            return None

    from_archive = ("--archive" in sys.argv[1:]) or ("archive" in sys.argv[1:])
    if from_archive:
        dirs = _archive_leveldb_dirs()
        print(f"  (disk) reading from _SORTPIN_ARCHIVE backups — {len(dirs)} folder(s)")
        if not dirs:
            print("  (disk) no archived IndexedDB folders found in _SORTPIN_ARCHIVE/")
            return None
    else:
        dirs = _idb_leveldb_dirs()
        if not dirs:
            print("  (disk) no SortPin IndexedDB folder found under Brave profiles")
            return None

    buckets = {"leads": [], "boards": [], "pins": []}

    def _parse(folder):
        added = 0
        for v in reader(folder):
            kind = _classify_keys(v.keys())
            if not kind:
                continue
            flat = _flatten_raw(kind, v)
            if flat:
                buckets[kind].append(flat); added += 1
        return added

    for d in dirs:
        print(f"  (disk) reading {d}")
        n = 0
        try:                                   # 1) read the folder DIRECTLY (in place)
            n = _parse(d)
        except Exception as e:
            print(f"  (disk) direct read failed ({e}) — copying files (Brave open?)...")
        if n == 0:                             # 2) locked/empty → copy to temp, retry
            tmp = tempfile.mkdtemp(prefix="sortpin_idb_")
            copydir = os.path.join(tmp, "leveldb"); os.makedirs(copydir, exist_ok=True)
            try:
                for fn in os.listdir(d):
                    if fn == "LOCK":
                        continue
                    try: shutil.copy2(os.path.join(d, fn), os.path.join(copydir, fn))
                    except Exception: pass
                _parse(copydir)
            except Exception as e:
                print(f"  (disk) copy read failed — {e}")
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

    if any(buckets.values()):
        print(f"  (disk) read {len(buckets['leads'])} pinners, "
              f"{len(buckets['boards'])} boards, {len(buckets['pins'])} pins from disk")
        return (buckets["leads"], buckets["boards"], buckets["pins"])
    print("  (disk) found the files but no usable records — falling back")
    return None

# ── data source 2: CSV exports in this folder ─────────────────────────────────
def load_from_csv():
    leads,  lf = read_all("*all_leads*.csv")
    boards, bf = read_all("*all_boards*.csv")
    pins,   pf = read_all("*all_pins*.csv")
    print(f"  leads  : {len(lf)} file(s) → {len(leads):>5} rows")
    print(f"  boards : {len(bf)} file(s) → {len(boards):>5} rows")
    print(f"  pins   : {len(pf)} file(s) → {len(pins):>5} rows")
    print(f"  (all CSV exports in this folder are merged & de-duplicated)")
    return leads, boards, pins

# ── main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"\n{'='*60}\n  STEP 4 — Build local relational SortPin database\n{'='*60}")
    # LIVE-from-extension is the default. Use --csv to skip it and read the
    # CSV exports only.  (--live also works and is the same as the default.)
    csv_only = "--csv" in sys.argv[1:]

    # 1) Pull the current extension data → save a timestamped CSV snapshot
    #    (preserves it on disk so it survives clearing in step 6 + git-syncs).
    #    --disk = read IndexedDB files directly (no browser), CDP as fallback.
    #    default = live read via the running Brave (CDP).
    args = sys.argv[1:]
    disk_mode = ("--disk" in args) or ("--archive" in args) or ("archive" in args)
    if not csv_only:
        live = None
        if disk_mode:
            src = "_SORTPIN_ARCHIVE backups" if (("--archive" in args) or ("archive" in args)) \
                  else "IndexedDB files"
            print(f"  Reading SortPin data from disk ({src}, no browser)...")
            live = try_disk_extension()
            if not live:
                # disk-only mode: do NOT open the browser (huge data can't load
                # on the page). Just build from the CSVs already in the folder.
                print("  (disk) disk read unavailable — NOT opening the browser.")
                print("        Install the parser:  python -m pip install ccl_chromium_reader")
                print("        Building from existing CSV snapshots instead.")
        else:
            print("  Pulling LIVE from the SortPin extension in Brave...")
            live = try_live_extension()
        if live:
            save_snapshot(*live)

    # 2) Always build the DB from ALL CSVs in the folder (the new snapshot +
    #    every past snapshot/export), merged & de-duplicated. This way the
    #    database keeps growing across runs, clears, and computers.
    print("  Reading SortPin CSV data from this folder...")
    leads, boards, pins = load_from_csv()

    if not (leads or boards or pins):
        print("\n  ⚠  No data found.\n"
              "     Export Pins / Boards / Pinners from the SortPin popup and put\n"
              "     the 3 CSV files in this folder, then re-run.\n")
        sys.exit(1)

    print("\n  Normalizing into Pinner → Boards → Pins ...")
    data = normalize(leads, boards, pins)
    write_sqlite(data)
    write_json(data)
    write_mysql_dump(data)

    print(f"\n  ✅ Built local database:")
    print(f"     • pinners : {len(data['pinners']):>6}")
    print(f"     • boards  : {len(data['boards']):>6}")
    print(f"     • pins    : {len(data['pins']):>6}")
    print(f"\n     → {os.path.basename(DB_PATH)}   (SQLite, relational)")
    print(f"     → {os.path.basename(JSON_PATH)}   (pinners/boards/pins, for the viewer)")
    print(f"     → IMPORTANT_DATABASE/sortpin_mysql.sql   (import into MySQL)")
    print(f"\n  Next:  python 5_view_data.py   (stats + browse the data)\n")

if __name__ == "__main__":
    main()
