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
DB_PATH   = os.path.join(BASE, "sortpin.db")
JSON_PATH = os.path.join(BASE, "sortpin_data.json")
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
        p["full_name"]      = _clean(l.get("full_name")) or p["full_name"]
        p["website_url"]    = _clean(l.get("website_url"))
        p["domain_url"]     = _clean(l.get("domain_url"))
        p["contact_email"]  = _clean(l.get("contact_email"))
        p["contact_phone"]  = _clean(l.get("contact_phone"))
        p["board_count"]    = _int(l.get("board_count"))
        p["follower_count"] = _int(l.get("follower_count"))
        p["following_count"]= _int(l.get("following_count"))
        p["pin_count"]      = _int(l.get("pin_count"))
        p["profile_reach"]  = _int(l.get("profile_reach"))
        p["profile_views"]  = _int(l.get("profile_views"))
        p["last_pin_at"]    = _clean(l.get("lastPinAt"))

    # 2) Boards (link to pinner via owner_username) — dedup by board id
    board_by_url = {}
    boards_by_id = {}                 # id -> board (newest occurrence wins)
    for b in boards:
        owner = _clean(b.get("owner_username"))
        p = ensure_pinner(owner, b.get("owner_full_name"))
        if p and not p["image_url"]:
            p["image_url"] = _clean(b.get("owner_image_medium_url")) or \
                             _clean(b.get("owner_image_small_url"))
        rec = {
            "id":              _clean(b.get("id")),
            "name":            _clean(b.get("name")),
            "description":     _clean(b.get("description")),
            "url":             _clean(b.get("url")),
            "image_cover_url": _clean(b.get("image_cover_url")),
            "follower_count":  _int(b.get("follower_count")),
            "section_count":   _int(b.get("section_count")),
            "pin_count":       _int(b.get("pin_count")),
            "category":        _clean(b.get("category")),
            "privacy":         _clean(b.get("privacy")),
            "modified_at":     _clean(b.get("modifiedAt")),
            "owner_username":  owner or None,   # None (not "") so FK isn't enforced
        }
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
        pins_by_id[pid] = {
            "id":             pid,
            "title":          _clean(pn.get("title")),
            "description":    _clean(pn.get("description")),
            "link":           _clean(pn.get("link")),
            "pin_url":        _clean(pn.get("pin_url")),
            "image":          _clean(pn.get("image")),
            "saves":          _int(pn.get("saves")),
            "repin_count":    _int(pn.get("repin_count")),
            "comment_count":  _int(pn.get("comment_count")),
            "like_count":     _int(pn.get("like_count")),
            "created_at":     _clean(pn.get("created_at")),
            "board_url":      burl,
            "board_id":       board_by_url.get(burl) or None,   # None → FK not enforced
            "board_name":     _clean(pn.get("board_name")),
            "pinner_username":pinner or None,
        }
    out_pins = list(pins_by_id.values())

    return {"pinners": list(pinners.values()),
            "boards":  out_boards,
            "pins":    out_pins}

# ── write SQLite (true relational, with foreign keys + indexes) ───────────────
def write_sqlite(data):
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA foreign_keys = ON;")
    con.executescript("""
        CREATE TABLE pinners (
            username TEXT PRIMARY KEY, full_name TEXT, website_url TEXT,
            domain_url TEXT, contact_email TEXT, contact_phone TEXT, image_url TEXT,
            board_count INTEGER, follower_count INTEGER, following_count INTEGER,
            pin_count INTEGER, profile_reach INTEGER, profile_views INTEGER,
            last_pin_at TEXT
        );
        CREATE TABLE boards (
            id TEXT PRIMARY KEY, name TEXT, description TEXT, url TEXT,
            image_cover_url TEXT, follower_count INTEGER, section_count INTEGER,
            pin_count INTEGER, category TEXT, privacy TEXT, modified_at TEXT,
            owner_username TEXT,
            FOREIGN KEY (owner_username) REFERENCES pinners(username)
        );
        CREATE TABLE pins (
            id TEXT PRIMARY KEY, title TEXT, description TEXT, link TEXT,
            pin_url TEXT, image TEXT, saves INTEGER, repin_count INTEGER,
            comment_count INTEGER, like_count INTEGER, created_at TEXT,
            board_url TEXT, board_id TEXT, board_name TEXT, pinner_username TEXT,
            FOREIGN KEY (board_id) REFERENCES boards(id),
            FOREIGN KEY (pinner_username) REFERENCES pinners(username)
        );
    """)
    con.executemany(
        "INSERT OR REPLACE INTO pinners VALUES "
        "(:username,:full_name,:website_url,:domain_url,:contact_email,"
        ":contact_phone,:image_url,:board_count,:follower_count,:following_count,"
        ":pin_count,:profile_reach,:profile_views,:last_pin_at)", data["pinners"])
    con.executemany(
        "INSERT OR REPLACE INTO boards VALUES "
        "(:id,:name,:description,:url,:image_cover_url,:follower_count,"
        ":section_count,:pin_count,:category,:privacy,:modified_at,:owner_username)",
        data["boards"])
    con.executemany(
        "INSERT OR REPLACE INTO pins VALUES "
        "(:id,:title,:description,:link,:pin_url,:image,:saves,:repin_count,"
        ":comment_count,:like_count,:created_at,:board_url,:board_id,:board_name,"
        ":pinner_username)", data["pins"])
    con.executescript("""
        CREATE INDEX idx_boards_owner ON boards(owner_username);
        CREATE INDEX idx_pins_pinner  ON pins(pinner_username);
        CREATE INDEX idx_pins_board   ON pins(board_id);
    """)
    con.commit()
    con.close()

# ── write nested JSON (Pinner → Boards → Pins) for the viewer ─────────────────
def write_json(data):
    boards_by_owner = {}
    for b in data["boards"]:
        boards_by_owner.setdefault(b["owner_username"], []).append(b)
    pins_by_pinner = {}
    for p in data["pins"]:
        pins_by_pinner.setdefault(p["pinner_username"], []).append(p)

    pinners_out = []
    for p in data["pinners"]:
        u = p["username"]
        my_boards = boards_by_owner.get(u, [])
        my_pins   = pins_by_pinner.get(u, [])
        pins_by_board = {}
        loose = []
        board_ids = {b["id"] for b in my_boards}
        for pin in my_pins:
            if pin["board_id"] and pin["board_id"] in board_ids:
                pins_by_board.setdefault(pin["board_id"], []).append(pin)
            else:
                loose.append(pin)
        boards_nested = []
        for b in sorted(my_boards, key=lambda x: x["pin_count"], reverse=True):
            bb = dict(b)
            bb["pins"] = pins_by_board.get(b["id"], [])
            boards_nested.append(bb)
        rec = dict(p)
        rec["boards"]      = boards_nested
        rec["loose_pins"]  = loose
        rec["_n_boards"]   = len(my_boards)
        rec["_n_pins"]     = len(my_pins)
        pinners_out.append(rec)

    pinners_out.sort(key=lambda x: (x["follower_count"], x["_n_pins"]), reverse=True)

    out = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "stats": {
            "pinners": len(data["pinners"]),
            "boards":  len(data["boards"]),
            "pins":    len(data["pins"]),
        },
        "pinners": pinners_out,
    }
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

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
  function trim(o){
    if (o===null || typeof o!=='object') return o;
    const r = {};
    for (const k in o){ const v=o[k], t=typeof v;
      if (v===null||t==='string'||t==='number'||t==='boolean') r[k]=v;
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
        picked[kind] = rows

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

    # 1) LIVE pull → save a timestamped CSV snapshot (preserves data on disk so it
    #    survives clearing the extension in step 6, and git-syncs to other PCs).
    if not csv_only:
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

    print(f"\n  ✅ Built local database:")
    print(f"     • pinners : {len(data['pinners']):>6}")
    print(f"     • boards  : {len(data['boards']):>6}")
    print(f"     • pins    : {len(data['pins']):>6}")
    print(f"\n     → {os.path.basename(DB_PATH)}   (SQLite, relational)")
    print(f"     → {os.path.basename(JSON_PATH)}   (nested, for the viewer)")
    print(f"\n  Next:  python 5_view_data.py   (stats + browse the data)\n")

if __name__ == "__main__":
    main()
