"""
STEP 5 — Statistics + visual browser for the SortPin database
=============================================================
Reads the flat data built by step 4 (sortpin_data.json), prints a statistics
summary, and starts a live database server.

The viewer has five tabs you can browse independently:
    • PINNERS  — search/sort people; click one to see their boards + pins
    • BOARDS   — search/sort boards;  click one to see its pins + owner
    • PINS     — search/sort every pin; click to open it on Pinterest
    • JOBS     — see scraper run times, keyword stats, and details
    • CONTENT LAB — see created vs saved analysis, keywords, tags, domains, and top pins

Run:
  python 5_view_data.py            # default: start live database server + open browser
  python 5_view_data.py --no-open  # start server without opening browser
"""

import os, sys, json, sqlite3, webbrowser, urllib.parse, http.server, socketserver
from datetime import datetime

BASE      = os.path.dirname(os.path.abspath(__file__))
DB_PATH   = os.path.join(BASE, "sortpin.db")
JSON_PATH = os.path.join(BASE, "sortpin_data.json")
LOG_PATH  = os.path.join(BASE, "magic_log.jsonl")   # written by magic_scroll.py

def read_logs(limit=500):
    """Return the most recent job-log events (newest first)."""
    if not os.path.exists(LOG_PATH):
        return []
    out = []
    try:
        with open(LOG_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try: out.append(json.loads(line))
                    except Exception: pass
    except Exception:
        return []
    return out[-limit:][::-1]

def run_websites_sync(db_path):
    """Sync all pinners with non-empty website_url to Google Sheets websites tab."""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    pinners = [dict(r) for r in con.execute(
        "SELECT username, website_url FROM pinners "
        "WHERE website_url IS NOT NULL AND website_url <> ''"
    )]
    con.close()

    if not pinners:
        return 0

    # Add parent directory to path so we can import google_sheets_client
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import google_sheets_client as gsc

    webapp = gsc.resolve_webapp()
    sa_exists = os.path.exists(os.path.join(gsc.BASE, gsc.SA_FILE))
    oauth_exists = os.path.exists(os.path.join(gsc.BASE, gsc.OAUTH_FILE))

    if not sa_exists and not oauth_exists and not webapp:
        raise RuntimeError("No Google Sheets auth found. Configure google_sheets_webapp.json or service account.")

    rows_to_sync = [[p["username"], p["website_url"], "not yet"] for p in pinners]

    # --- Mode 1: gspread API ---
    if sa_exists or oauth_exists:
        gc, auth_mode = gsc.get_gspread_client()
        if gc is None:
            raise RuntimeError("Failed to authorize Google Sheets API client.")
        sh = gc.open_by_key(gsc.SPREADSHEET_ID)
        
        # Check / create 'websites' sheet
        try:
            ws = sh.worksheet("websites")
        except Exception:
            ws = sh.add_worksheet(title="websites", rows="100", cols="3")
            ws.update("A1:C1", [["id", "website", "scrapped"]], value_input_option="RAW")
            
        # Get existing IDs from column A
        existing_ids = set()
        col_a = ws.col_values(1)
        if col_a:
            for val in col_a[1:]: # skip header
                if val:
                    existing_ids.add(val.strip().lower())
                    
        # Filter new ones
        new_rows = []
        for r in rows_to_sync:
            pinner_id = r[0]
            if pinner_id and pinner_id.strip().lower() not in existing_ids:
                new_rows.append(r)
                
        if new_rows:
            ws.append_rows(new_rows, value_input_option="RAW")
        return len(new_rows)

    # --- Mode 2: Web App ---
    else:
        payload = {
            "action": "sync_websites",
            "rows": rows_to_sync
        }
        res = gsc.post_webapp(webapp, payload)
        return res.get("count", 0)

def load_from_db(db_path):
    """Read pinners/boards/pins straight from the SQLite DB and build the flat
    structure the viewer uses (so the viewer is database-driven)."""
    con = sqlite3.connect(db_path); con.row_factory = sqlite3.Row
    pinners = [dict(r) for r in con.execute("SELECT * FROM pinners")]
    boards  = [dict(r) for r in con.execute("SELECT * FROM boards")]
    pins    = [dict(r) for r in con.execute("SELECT * FROM pins")]
    con.close()

    nb, np_ = {}, {}
    for b in boards:
        if b.get("owner_username"):  nb[b["owner_username"]]  = nb.get(b["owner_username"], 0) + 1
    for p in pins:
        if p.get("pinner_username"): np_[p["pinner_username"]] = np_.get(p["pinner_username"], 0) + 1

    pout = []
    for p in pinners:
        u = p["username"]
        pout.append({
            "username": u, "full_name": p.get("full_name", ""), "image_url": p.get("image_url", ""),
            "website_url": p.get("website_url", ""), "domain_url": p.get("domain_url", ""),
            "contact_email": p.get("contact_email", ""), "follower_count": p.get("follower_count", 0),
            "pin_count": p.get("pin_count", 0), "board_count": p.get("board_count", 0),
            "profile_reach": p.get("profile_reach", 0), 
            "nb": p.get("scraped_boards_count") if p.get("scraped_boards_count") is not None else nb.get(u, 0), 
            "np": p.get("scraped_pins_count") if p.get("scraped_pins_count") is not None else np_.get(u, 0),
            "np_created": p.get("scraped_created_pins_count") if p.get("scraped_created_pins_count") is not None else sum(1 for x in pins if x.get("pinner_username") == u and x.get("pin_type") == "created"),
            "np_saved": p.get("scraped_saved_pins_count") if p.get("scraped_saved_pins_count") is not None else sum(1 for x in pins if x.get("pinner_username") == u and x.get("pin_type") == "saved")
        })
    pout.sort(key=lambda x: (x["np"], x["follower_count"]), reverse=True)
    boards.sort(key=lambda b: b.get("pin_count", 0), reverse=True)
    pins.sort(key=lambda p: p.get("repin_count", 0), reverse=True)
    return {"generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "stats": {"pinners": len(pinners), "boards": len(boards), "pins": len(pins)},
            "pinners": pout, "boards": boards, "pins": pins}

def load():
    # database first (organised, single source of truth); JSON only as fallback
    if os.path.exists(DB_PATH):
        print(f"  Reading from {os.path.basename(DB_PATH)} (SQLite database)...")
        data = load_from_db(DB_PATH)
    elif os.path.exists(JSON_PATH):
        with open(JSON_PATH, encoding="utf-8") as f:
            data = json.load(f)
    else:
        print(f"\n  ⚠  No database found — run step 4 first.\n")
        sys.exit(1)
    
    data["jobs"] = read_logs()
    return data

# ── terminal statistics ───────────────────────────────────────────────────────
def print_stats(d):
    pinners, boards, pins = d["pinners"], d["boards"], d["pins"]
    s = d["stats"]
    n_email  = sum(1 for p in pinners if p.get("contact_email"))
    n_site   = sum(1 for p in pinners if p.get("website_url"))
    n_wb     = sum(1 for p in pinners if p.get("nb"))
    n_wp     = sum(1 for p in pinners if p.get("np"))
    foll     = sum(p.get("follower_count", 0) for p in pinners)

    def bar(label, v, t):
        pct = (v / t * 100) if t else 0
        return f"  {label:<14}{v:>7}  [{'█'*int(pct/4)}{'░'*(25-int(pct/4))}] {pct:4.0f}%"

    n_created = sum(1 for p in pins if p.get("pin_type") == "created")
    n_saved = sum(1 for p in pins if p.get("pin_type") == "saved")

    print(f"\n{'='*62}\n  SortPin data — statistics   ({d.get('generated_at','')})\n{'='*62}")
    print(f"  Pinners : {s['pinners']:>7}")
    print(f"  Boards  : {s['boards']:>7}")
    print(f"  Pins    : {s['pins']:>7} (created: {n_created}, saved: {n_saved})")
    print(f"{'-'*62}")
    print(bar("with email",   n_email, s["pinners"]))
    print(bar("with website", n_site,  s["pinners"]))
    print(bar("with boards",  n_wb,    s["pinners"]))
    print(bar("with pins",    n_wp,    s["pinners"]))
    print(f"{'-'*62}")
    print(f"  Total followers (all pinners): {foll:,}")

    print(f"\n  ── Top 10 pinners by followers ──")
    for i, p in enumerate(sorted(pinners, key=lambda x: x.get('follower_count',0), reverse=True)[:10], 1):
        print(f"   {i:>2}. {(p.get('full_name') or p.get('username'))[:26]:<26} "
              f"{p.get('follower_count',0):>9,}  @{p.get('username','')[:18]:<18} "
              f"bd:{p.get('nb',0):>3} pin:{p.get('np',0):>3}")

    print(f"\n  ── Top 10 boards by pin count ──")
    for i, b in enumerate(boards[:10], 1):
        print(f"   {i:>2}. {(b.get('name') or '')[:38]:<38} {b.get('pin_count',0):>7} pins  "
              f"@{(b.get('owner_username') or '')[:16]}")
    print(f"{'='*62}\n")

# ── Live Server ───────────────────────────────────────────────────────────────

_SEARCH = {
    "pinners": ["username", "full_name", "contact_email", "website_url", "domain_url"],
    "boards":  ["name", "description", "owner_username", "category"],
    "pins":    ["title", "description", "board_name", "pinner_username"],
}
_PK = {"pinners": "username", "boards": "id", "pins": "id"}
_DEFAULT_SORT = {"pinners": "follower_count", "boards": "pin_count", "pins": "repin_count"}

def _cols(con, table):
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]

def api_list(con, t, q, sort, dir_, limit, offset):
    cols = _cols(con, t)
    where, params = "", []
    if q:
        likes = [f"`{c}` LIKE ?" for c in _SEARCH.get(t, []) if c in cols]
        if likes:
            where = "WHERE (" + " OR ".join(likes) + ")"
            params = [f"%{q}%"] * len(likes)
    if sort not in cols:
        sort = _DEFAULT_SORT[t]
    dir_ = "ASC" if str(dir_).lower() == "asc" else "DESC"
    total = con.execute(f"SELECT COUNT(*) FROM `{t}` {where}", params).fetchone()[0]
    rows = [dict(r) for r in con.execute(
        f"SELECT * FROM `{t}` {where} ORDER BY `{sort}` {dir_} LIMIT ? OFFSET ?",
        params + [limit, offset])]
    return {"columns": cols, "rows": rows, "total": total, "sort": sort, "dir": dir_.lower()}

def api_get(con, t, id_):
    if t not in _PK:
        return None
    r = con.execute(f"SELECT * FROM `{t}` WHERE `{_PK[t]}`=?", [id_]).fetchone()
    return dict(r) if r else None

def api_children(con, t, id_):
    if t == "pinner":
        return {
            "boards": [dict(r) for r in con.execute(
                "SELECT * FROM boards WHERE owner_username=? ORDER BY pin_count DESC", [id_])],
            "pins": [dict(r) for r in con.execute(
                "SELECT * FROM pins WHERE pinner_username=? ORDER BY repin_count DESC LIMIT 500", [id_])],
        }
    if t == "board":
        return {"pins": [dict(r) for r in con.execute(
            "SELECT * FROM pins WHERE board_id=? ORDER BY repin_count DESC LIMIT 500", [id_])]}
    return {}

SERVER_PAGE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>SortPin Live</title>
<style>
 :root{--bg:#0f1115;--panel:#171a21;--panel2:#1f232c;--line:#2a2f3a;--txt:#e7e9ee;--muted:#9aa3b2;--accent:#e60023;--accent2:#3b82f6;}
 *{box-sizing:border-box}body{margin:0;font:14px/1.45 -apple-system,Segoe UI,Roboto,Arial,sans-serif;background:var(--bg);color:var(--txt)}
 header{padding:12px 18px;background:var(--panel);border-bottom:1px solid var(--line);display:flex;gap:14px;align-items:center;flex-wrap:wrap;position:sticky;top:0;z-index:10}
 h1{font-size:16px;margin:0}h1 b{color:var(--accent)}
 .tab{padding:6px 14px;border:1px solid var(--line);border-radius:20px;cursor:pointer;color:var(--muted);font-size:13px}
 .tab.on{background:var(--accent);border-color:var(--accent);color:#fff}
 .live{font-size:11px;color:#2ecc71}
 .controls{padding:10px 18px;display:flex;gap:8px;align-items:center;flex-wrap:wrap;border-bottom:1px solid var(--line);background:var(--panel)}
 input,select,button{padding:7px 10px;background:var(--panel2);border:1px solid var(--line);border-radius:8px;color:var(--txt);font-size:13px}
 input{flex:1;min-width:180px}button{cursor:pointer}.muted{color:var(--muted);font-size:12px}
 .wrap{padding:16px 18px}
 .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:12px}
 .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden;cursor:pointer}
 .card:hover{border-color:var(--accent2)}.card img{width:100%;height:160px;object-fit:cover;background:var(--panel2);display:block}
 .card .bd{padding:9px 11px}.card .t{font-weight:600;font-size:13px;max-height:38px;overflow:hidden}
 .card .m{color:var(--muted);font-size:11px;margin-top:5px;display:flex;justify-content:space-between}
 table{border-collapse:collapse;width:100%;font-size:12px}
 th,td{border:1px solid var(--line);padding:6px 8px;text-align:left;max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 th{background:var(--panel2);position:sticky;top:0;cursor:pointer}tr:hover td{background:var(--panel2)}
 .tblwrap{overflow:auto;max-height:calc(100vh - 160px)}
 .crumb{color:var(--muted);font-size:13px;margin-bottom:12px}.crumb a{color:var(--accent2);cursor:pointer}
 .detail{display:flex;gap:20px;flex-wrap:wrap}.detail img{max-width:340px;border-radius:12px;background:var(--panel2)}
 .kvs{flex:1;min-width:320px}.kvrow{display:flex;border-bottom:1px solid var(--line);padding:5px 0}
 .kvrow .k{width:170px;color:var(--muted);flex:none}.kvrow .v{word-break:break-word}.kvrow .v a{color:var(--accent2)}
 .sectit{font-size:13px;text-transform:uppercase;letter-spacing:.6px;color:var(--muted);margin:18px 0 10px;border-bottom:1px solid var(--line);padding-bottom:6px}
 .empty{color:var(--muted);padding:40px;text-align:center}
 .pg{display:flex;gap:6px;align-items:center}
 .badge{padding:2px 6px;border-radius:4px;font-size:10px;font-weight:bold;margin-right:6px}
 .badge.created{background:#e60023;color:#fff}
 .badge.saved{background:#3b82f6;color:#fff}
 .insight-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px;margin-bottom:24px}
 .insight-card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px}
 .insight-card h3{margin:0 0 12px 0;font-size:12px;text-transform:uppercase;color:var(--muted);letter-spacing:0.5px}
 .stat-val{font-size:24px;font-weight:bold;color:var(--txt)}
 .tag-cloud{display:flex;flex-wrap:wrap;gap:6px}
 .tag-badge{background:var(--panel2);border:1px solid var(--line);padding:4px 8px;border-radius:12px;font-size:11px;color:var(--txt)}
 .tag-badge b{color:var(--accent2)}
</style></head><body>
<header>
 <h1><b>SortPin</b> Live <span class="live">● database</span></h1>
 <div class="tab on" data-t="pinners">Pinners <b id="c_pinners">·</b></div>
 <div class="tab" data-t="boards">Boards <b id="c_boards">·</b></div>
 <div class="tab" data-t="pins">Pins <b id="c_pins">·</b></div>
 <div class="tab" data-t="jobs">Jobs ⏱</div>
 <div class="tab" data-t="insights">Content Lab 💡</div>
 <button id="sync_websites_btn" style="margin-left:auto;background:var(--accent2);border-color:var(--accent2);color:#fff;font-weight:bold;border-radius:20px;padding:6px 16px;">Sync Websites to Sheet 📊</button>
</header>
<div class="controls">
 <input id="q" placeholder="Search…">
 <select id="sort"></select>
 <select id="dir"><option value="desc">▼ desc</option><option value="asc">▲ asc</option></select>
 <button id="view">Table view</button>
 <span class="pg"><button id="prev">‹</button><span class="muted" id="page">0–0 / 0</span><button id="next">›</button></span>
</div>
<div class="wrap" id="main"></div>
<script>
document.getElementById('sync_websites_btn').addEventListener('click', async () => {
  const btn = document.getElementById('sync_websites_btn');
  const origText = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Syncing... ⏳';
  try {
    const res = await fetch('/api/sync_websites', { method: 'POST' }).then(r => r.json());
    if (res.error) {
      alert('Error: ' + res.error);
    } else {
      alert(`Sync completed!\nAdded ${res.count} new websites to the 'websites' sheet.`);
    }
  } catch (e) {
    alert('Sync failed: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = origText;
  }
});
const api=(p,q)=>fetch(p+'?'+new URLSearchParams(q)).then(r=>r.json());
const esc=s=>(s==null?'':String(s)).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const num=n=>(+n||0).toLocaleString();
const cell=v=>{const s=v==null?'':String(v);
 return /^https?:\/\//.test(s)
   ? `<a href="${esc(s)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">${esc(s)}</a>`
   : esc(s);};
let state={t:'pinners',q:'',sort:'',dir:'desc',off:0,limit:60,mode:'cards'};
const SORTS={pinners:[['follower_count','followers'],['np','—'],['profile_reach','reach'],['pin_count','total pins'],['board_count','boards']],
 boards:[['pin_count','pins'],['follower_count','followers']],
 pins:[['repin_count','repins'],['saves','saves'],['comment_count','comments']]};

api('/api/stats',{}).then(s=>{c_pinners.textContent=num(s.pinners);c_boards.textContent=num(s.boards);c_pins.textContent=num(s.pins);});

function setTab(t){state.t=t;state.off=0;state.q='';q.value='';
 [...document.querySelectorAll('.tab')].forEach(e=>e.classList.toggle('on',e.dataset.t===t));
 const sel=document.getElementById('sort');sel.innerHTML='';
 (SORTS[t]||[]).filter(s=>s[1]!=='—').forEach(([k,l])=>{const o=document.createElement('option');o.value=k;o.textContent='Sort: '+l;sel.appendChild(o);});
 state.sort=sel.value||'';
 const ct = document.querySelector('.controls');
 if(t==='jobs' || t==='insights'){
   ct.style.display = 'none';
 } else {
   ct.style.display = 'flex';
 }
 if(t==='jobs'){ renderJobs(); }
 else if(t==='insights'){ renderInsights(); }
 else { load(); }}

async function renderJobs(){
 document.getElementById('page').textContent='';
 const r=await api('/api/logs',{limit:500}); const ev=r.events||[];
 const m=document.getElementById('main');
 if(!ev.length){m.innerHTML='<div class="empty">No job log yet.<br>Run <b>python magic_scroll.py</b> — each keyword it scrapes will appear here (auto-refreshes every 5s).</div>';return;}
 const kw=ev.filter(e=>e.event==='keyword');
 const totMin=kw.reduce((a,e)=>a+(+e.minutes||0),0).toFixed(1);
 const totPins=kw.reduce((a,e)=>a+(+e.pins||0),0);
 const comps=[...new Set(ev.map(e=>e.computer))].join(', ');
 let html=`<div class="muted" style="margin-bottom:10px"><b>${kw.length}</b> keywords scraped · <b>${totMin}</b> min total · ~<b>${totPins.toLocaleString()}</b> pins · computers: ${esc(comps)} · newest first (live)</div>`;
 html+='<div class="tblwrap"><table><thead><tr><th>time</th><th>computer</th><th>cycle</th><th>event</th><th>keyword(s)</th><th>min</th><th>pins</th><th>why</th></tr></thead><tbody>';
 html+=ev.map(e=>{
   const kwc=e.keyword || (e.keywords?e.keywords.join(', '):'');
   let col = '';
   let why = e.why || '';
   if (e.event === 'keyword') {
     col = '';
   } else if (e.event === 'cycle_done') {
     col = ' style="color:#2ecc71;font-weight:bold"';
     why = `Total Pins: ${num(e.total_pins || 0)} (Created: ${num(e.created_pins || 0)}, Saved: ${num(e.saved_pins || 0)}) · +${num(e.new_pins || 0)} new`;
   } else if (e.event === 'db_build') {
     col = ' style="color:#f39c12;font-weight:bold"';
     why = `Build stats — Pinners: ${num(e.pinners || 0)}, Boards: ${num(e.boards || 0)}, Pins: ${num(e.pins || 0)} (Cr: ${num(e.created_pins || 0)}, Sv: ${num(e.saved_pins || 0)})`;
   } else {
     col = ' style="color:var(--muted)"';
   }
   return `<tr${col}><td>${esc(e.ts)}</td><td>${esc(e.computer)}</td><td>${esc(e.cycle || '')}</td><td>${esc(e.event)}</td><td>${esc(kwc)}</td><td>${e.minutes!=null?esc(e.minutes):''}</td><td>${e.pins!=null?esc(e.pins):''}</td><td>${esc(why)}</td></tr>`;
 }).join('');
 html+='</tbody></table></div>';
 m.innerHTML=html;
}
setInterval(()=>{ if(state.t==='jobs') renderJobs(); }, 5000);

async function renderInsights(){
 document.getElementById('page').textContent='';
 const m=document.getElementById('main');
 m.innerHTML='<div class="empty">Analyzing database and compiling content insights...</div>';
 try {
   const d=await api('/api/insights',{});
   const s=d.stats;
   const r=d.content_recipe;
   
   let html=`
   <div class="insight-grid">
     <div class="insight-card">
       <h3>Engagement Breakdown</h3>
       <div class="stat-val">${num(s.total)} <span class="muted" style="font-size:14px">Total Pins</span></div>
       <div style="margin-top:10px;height:6px;background:var(--panel2);border-radius:3px;overflow:hidden;display:flex">
         <div style="width:${s.created_pct}%;background:var(--accent)"></div>
         <div style="width:${100 - s.created_pct}%;background:var(--accent2)"></div>
       </div>
       <div style="display:flex;justify-content:space-between;margin-top:8px;font-size:12px">
         <span><b style="color:var(--accent)">●</b> Created (Original): <b>${num(s.created)}</b> (${s.created_pct}%)</span>
         <span><b style="color:var(--accent2)">●</b> Saved (Curated): <b>${num(s.saved)}</b> (${(100 - s.created_pct).toFixed(1)}%)</span>
       </div>
     </div>
     <div class="insight-card">
       <h3>Viral Blueprint (Successful Created Pins)</h3>
       <div style="display:flex;flex-direction:column;gap:8px;margin-top:4px">
         <div style="display:flex;justify-content:space-between">
           <span class="muted">Avg Title Length:</span>
           <span><b>${r.avg_title_len}</b> chars</span>
         </div>
         <div style="display:flex;justify-content:space-between">
           <span class="muted">Avg Description Length:</span>
           <span><b>${r.avg_desc_len}</b> chars</span>
         </div>
         <div style="display:flex;justify-content:space-between">
           <span class="muted">Pins with External Link:</span>
           <span><b>${r.link_pct}%</b></span>
         </div>
         <div class="muted" style="font-size:11px;margin-top:4px;border-top:1px solid var(--line);padding-top:4px">
           Calculated from a sample of <b>${r.sample_size}</b> viral original pins.
         </div>
       </div>
     </div>
   </div>
   
   <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:24px;flex-wrap:wrap">
     <div class="insight-card">
       <h3>Top Traffic-Driving Websites</h3>
       <table style="width:100%;margin-top:8px">
         <thead>
           <tr><th>Domain</th><th style="text-align:right">Created Pin Count</th></tr>
         </thead>
         <tbody>
           ${d.top_domains.map(([dom, count]) => `
             <tr>
               <td><a href="https://${esc(dom)}" target="_blank">${esc(dom)}</a></td>
               <td style="text-align:right"><b>${num(count)}</b></td>
             </tr>
           `).join('') || '<tr><td colspan="2" class="empty">No domains found</td></tr>'}
         </tbody>
       </table>
     </div>
     <div class="insight-card">
       <h3>Top Trending Words & Hashtags</h3>
       <h4 style="margin:8px 0 6px 0;font-size:12px;color:var(--muted)">KEYWORDS IN VIRAL PINS</h4>
       <div class="tag-cloud" style="margin-bottom:14px">
         ${d.top_words.map(([w, c]) => `<span class="tag-badge">${esc(w)} (<b>${c}</b>)</span>`).join('') || '<span class="muted">none</span>'}
       </div>
       <h4 style="margin:8px 0 6px 0;font-size:12px;color:var(--muted)">HASHTAGS IN VIRAL PINS</h4>
       <div class="tag-cloud">
         ${d.top_hashtags.map(([t, c]) => `<span class="tag-badge" style="border-color:var(--accent)">${esc(t)} (<b>${c}</b>)</span>`).join('') || '<span class="muted">none</span>'}
       </div>
     </div>
   </div>
   
   <div class="sectit">Top Performing Original/Created Pins (Learn from Competitors' Best Work)</div>
   <div class="grid" style="margin-bottom:24px">
     ${d.top_created.map(p => `
       <div class="card" data-type="pins" data-id="${esc(p.id)}">
         ${imgCell(p)}
         <div class="bd">
           <div class="t">${esc(p.title || p.description || '(no title)')}</div>
           <div class="m" style="margin-top:6px;display:flex;align-items:center;justify-content:space-between">
             <span><span class="badge created">Created</span>♥ ${num(p.repin_count || p.saves)}</span>
             <span class="muted">@${esc(p.pinner_username || '')}</span>
           </div>
         </div>
       </div>
     `).join('') || '<div class="empty">No high-performing created pins found</div>'}
   </div>
   
   <div class="sectit">Top Performing Curated/Saved Pins (Top Curations)</div>
   <div class="grid">
     ${d.top_saved.map(p => `
       <div class="card" data-type="pins" data-id="${esc(p.id)}">
         ${imgCell(p)}
         <div class="bd">
           <div class="t">${esc(p.title || p.description || '(no title)')}</div>
           <div class="m" style="margin-top:6px;display:flex;align-items:center;justify-content:space-between">
             <span><span class="badge saved">Saved</span>♥ ${num(p.repin_count || p.saves)}</span>
             <span class="muted">@${esc(p.pinner_username || '')}</span>
           </div>
         </div>
       </div>
     `).join('') || '<div class="empty">No high-performing curated pins found</div>'}
   </div>
   `;
   
   m.innerHTML=html;
   bindMini(m);
 } catch(e){
   m.innerHTML=`<div class="empty" style="color:var(--accent)">Error generating insights: ${esc(e.message||e)}</div>`;
 }
}

function imgCell(p){return p.image?`<img loading="lazy" src="${esc(p.image)}" onerror="this.style.visibility='hidden'">`:`<div style="height:160px"></div>`;}

async function load(){
 const r=await api('/api/list',{type:state.t,q:state.q,sort:state.sort,dir:state.dir,limit:state.limit,offset:state.off});
 const total=r.total||0,from=total?state.off+1:0,to=Math.min(state.off+state.limit,total);
 document.getElementById('page').textContent=`${from}–${to} / ${num(total)}`;
 const m=document.getElementById('main');
 if(!r.rows||!r.rows.length){m.innerHTML='<div class="empty">No rows.</div>';return;}
 if(state.mode==='table'){
   const cols=r.columns;
   m.innerHTML='<div class="tblwrap"><table><thead><tr>'+cols.map(c=>`<th>${esc(c)}</th>`).join('')+'</tr></thead><tbody>'+
     r.rows.map(row=>`<tr style="cursor:pointer" data-id="${esc(row[pk()])}">`+cols.map(c=>`<td title="${esc(row[c])}">${cell(row[c])}</td>`).join('')+'</tr>').join('')+'</tbody></table></div>';
   [...m.querySelectorAll('tr[data-id]')].forEach(tr=>tr.onclick=()=>detail(state.t,tr.dataset.id));
 } else {
   m.innerHTML='<div class="grid">'+r.rows.map(card).join('')+'</div>';
   [...m.querySelectorAll('[data-id]')].forEach(el=>el.onclick=()=>detail(el.dataset.type,el.dataset.id));
 }
}
function pk(){return state.t==='pinners'?'username':'id';}

function card(row){
 if(state.t==='pins') {
   const badge = row.pin_type === 'created' 
     ? '<span style="background:#e60023;color:#fff;padding:2px 6px;border-radius:4px;font-size:10px;font-weight:bold;margin-right:6px">Created</span>' 
     : '<span style="background:#3b82f6;color:#fff;padding:2px 6px;border-radius:4px;font-size:10px;font-weight:bold;margin-right:6px">Saved</span>';
   return `<div class="card" data-type="pins" data-id="${esc(row.id)}">${imgCell(row)}<div class="bd"><div class="t">${esc(row.title||row.description||'(no title)')}</div><div class="m" style="margin-top:6px;display:flex;align-items:center;justify-content:space-between"><span>${badge}♥ ${num(row.repin_count||row.saves)}</span><span class="muted">@${esc(row.pinner_username||'')}</span></div></div></div>`;
 }
 if(state.t==='boards')return `<div class="card" data-type="boards" data-id="${esc(row.id)}">${row.image_cover_url?`<img loading="lazy" src="${esc(row.image_cover_url)}" onerror="this.style.visibility='hidden'">`:'<div style="height:160px"></div>'}<div class="bd"><div class="t">${esc(row.name||'(untitled)')}</div><div class="m"><span>${num(row.pin_count)} pins</span><span>@${esc(row.owner_username||'')}</span></div></div></div>`;
 
 const cr = row.scraped_created_pins_count !== undefined ? row.scraped_created_pins_count : (row.np_created || 0);
 const sv = row.scraped_saved_pins_count !== undefined ? row.scraped_saved_pins_count : (row.np_saved || 0);
 return `<div class="card" data-type="pinner" data-id="${esc(row.username)}"><div class="bd"><div class="t">${esc(row.full_name||row.username)}</div><div class="m"><span>${num(row.follower_count)} foll</span><span>@${esc(row.username)}</span></div><div class="m" style="margin-top:4px;font-size:11px"><span>Created: <b>${num(cr)}</b></span><span>Saved: <b>${num(sv)}</b></span></div><div class="muted" style="margin-top:4px">${esc(row.contact_email||row.website_url||'')}</div></div></div>`;
}
function kvTable(obj){return '<div class="kvs">'+Object.keys(obj).map(k=>{let v=obj[k];let vh=esc(v);
 if(typeof v==='string'&&/^https?:\/\//.test(v))vh=`<a href="${esc(v)}" target="_blank">${esc(v)}</a>`;
 return `<div class="kvrow"><div class="k">${esc(k)}</div><div class="v">${vh}</div></div>`;}).join('')+'</div>';}

async function detail(type,id){
 const t = type==='pinner'?'pinners':type;
 const row=await api('/api/get',{type:t,id});
 const m=document.getElementById('main');
 let head=`<div class="crumb"><a onclick="setTab('${t}')">${t}</a> › ${esc(row[pkOf(t)]||id)}</div>`;
 if(t==='pins'){
   m.innerHTML=head+`<div class="detail">${row.image?`<a href="${esc(row.pin_url||'#')}" target="_blank"><img src="${esc(row.image)}"></a>`:''}${kvTable(row)}</div>`;
 } else if(t==='boards'){
   const ch=await api('/api/children',{type:'board',id});
   m.innerHTML=head+`<div class="detail">${row.image_cover_url?`<img src="${esc(row.image_cover_url)}">`:''}${kvTable(row)}</div>`+
     `<div class="sectit">Pins (${(ch.pins||[]).length})</div><div class="grid">${(ch.pins||[]).map(p=>pinMini(p)).join('')||'<div class=empty>none</div>'}</div>`;
   bindMini(m);
 } else { // pinners
   const ch=await api('/api/children',{type:'pinner',id});
   const pins = ch.pins || [];
   const createdPins = pins.filter(p => p.pin_type === 'created');
   const savedPins = pins.filter(p => p.pin_type === 'saved');
   
   m.innerHTML=head+kvTable(row)+
     `<div class="sectit">Boards (${(ch.boards||[]).length})</div><div class="grid">${(ch.boards||[]).map(b=>boardMini(b)).join('')||'<div class=empty>none</div>'}</div>`+
     `<div class="sectit">Created Pins (${createdPins.length}) — Original Content</div><div class="grid">${createdPins.map(p=>pinMini(p)).join('')||'<div class=empty>none</div>'}</div>`+
     `<div class="sectit">Saved Pins (${savedPins.length}) — Curated Content</div><div class="grid">${savedPins.map(p=>pinMini(p)).join('')||'<div class=empty>none</div>'}</div>`;
   bindMini(m);
 }
}
function pkOf(t){return t==='pinners'?'username':'id';}
function pinMini(p){
  const badge = p.pin_type === 'created' 
    ? '<span style="background:#e60023;color:#fff;padding:2px 6px;border-radius:4px;font-size:10px;font-weight:bold;margin-right:6px">Created</span>' 
    : '<span style="background:#3b82f6;color:#fff;padding:2px 6px;border-radius:4px;font-size:10px;font-weight:bold;margin-right:6px">Saved</span>';
  return `<div class="card" data-type="pins" data-id="${esc(p.id)}">${imgCell(p)}<div class="bd"><div class="t">${esc(p.title||p.description||'(no title)')}</div><div class="m" style="margin-top:6px;display:flex;align-items:center"><span>${badge}♥ ${num(p.repin_count||p.saves)}</span></div></div></div>`;
}
function boardMini(b){return `<div class="card" data-type="boards" data-id="${esc(b.id)}">${b.image_cover_url?`<img loading="lazy" src="${esc(b.image_cover_url)}" onerror="this.style.visibility='hidden'">`:'<div style="height:160px"></div>'}<div class="bd"><div class="t">${esc(b.name||'(untitled)')}</div><div class="m"><span>${num(b.pin_count)} pins</span></div></div></div>`;}
function bindMini(m){[...m.querySelectorAll('[data-id]')].forEach(el=>el.onclick=()=>detail(el.dataset.type,el.dataset.id));}

[...document.querySelectorAll('.tab')].forEach(e=>e.onclick=()=>setTab(e.dataset.t));
document.getElementById('q').oninput=()=>{state.q=q.value.trim();state.off=0;load();};
document.getElementById('sort').onchange=e=>{state.sort=e.target.value;state.off=0;load();};
document.getElementById('dir').onchange=e=>{state.dir=e.target.value;state.off=0;load();};
document.getElementById('view').onclick=e=>{state.mode=state.mode==='cards'?'table':'cards';e.target.textContent=state.mode==='cards'?'Table view':'Card view';load();};
document.getElementById('prev').onclick=()=>{state.off=Math.max(0,state.off-state.limit);load();};
document.getElementById('next').onclick=()=>{state.off+=state.limit;load();};
setTab('pinners');
</script></body></html>"""

def make_handler(db_path):
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def _send(self, body, ctype):
            b = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers(); self.wfile.write(b)
        def do_POST(self):
            u = urllib.parse.urlparse(self.path)
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length > 0:
                self.rfile.read(content_length)
                
            if u.path == "/api/sync_websites":
                try:
                    count = run_websites_sync(db_path)
                    out = {"ok": True, "count": count}
                except Exception as e:
                    out = {"error": str(e)}
                self._send(json.dumps(out, ensure_ascii=False), "application/json; charset=utf-8")
            else:
                self.send_response(404)
                self.end_headers()
        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(u.query)
            g = lambda k, d="": qs.get(k, [d])[0]
            if u.path in ("/", "/index.html"):
                return self._send(SERVER_PAGE, "text/html; charset=utf-8")
            con = sqlite3.connect(db_path); con.row_factory = sqlite3.Row
            try:
                if u.path == "/api/stats":
                    out = {t: con.execute(f"SELECT COUNT(*) FROM `{t}`").fetchone()[0]
                           for t in ("pinners", "boards", "pins")}
                elif u.path == "/api/list":
                    t = g("type", "pins")
                    out = (api_list(con, t, g("q"), g("sort"), g("dir", "desc"),
                                    min(int(g("limit", "60") or 60), 500), int(g("offset", "0") or 0))
                           if t in _SEARCH else {"error": "bad type"})
                elif u.path == "/api/get":
                    out = api_get(con, g("type"), g("id")) or {}
                elif u.path == "/api/children":
                    out = api_children(con, g("type"), g("id"))
                elif u.path == "/api/logs":
                    out = {"events": read_logs(int(g("limit", "500") or 500))}
                elif u.path == "/api/insights":
                    # 1. Total and type counts
                    tot_pins = con.execute("SELECT COUNT(*) FROM pins").fetchone()[0]
                    created_pins = con.execute("SELECT COUNT(*) FROM pins WHERE pin_type='created'").fetchone()[0]
                    saved_pins = con.execute("SELECT COUNT(*) FROM pins WHERE pin_type='saved'").fetchone()[0]
                    
                    # 2. Top performing original/created pins
                    top_created = [dict(r) for r in con.execute(
                        "SELECT id, title, description, pin_url, image, repin_count, saves, comment_count, pinner_username "
                        "FROM pins WHERE pin_type='created' AND (repin_count > 0 OR saves > 0) "
                        "ORDER BY (repin_count + saves) DESC LIMIT 15"
                    )]
                    
                    # 3. Top performing saved/curated pins
                    top_saved = [dict(r) for r in con.execute(
                        "SELECT id, title, description, pin_url, image, repin_count, saves, comment_count, pinner_username "
                        "FROM pins WHERE pin_type='saved' AND (repin_count > 0 OR saves > 0) "
                        "ORDER BY (repin_count + saves) DESC LIMIT 15"
                    )]

                    # 4. Top domains for created pins
                    domain_counts = {}
                    for r in con.execute("SELECT link FROM pins WHERE pin_type='created' AND link IS NOT NULL AND link <> ''"):
                        dom = extract_domain(r[0])
                        if dom:
                            domain_counts[dom] = domain_counts.get(dom, 0) + 1
                    top_domains = sorted(domain_counts.items(), key=lambda x: x[1], reverse=True)[:10]

                    # 5. Extract words and hashtags from top pins (repin_count + saves >= 5)
                    word_counts = {}
                    hashtag_counts = {}
                    
                    stop_words = {
                        "and", "the", "a", "of", "to", "for", "in", "is", "on", "with", "this", "your", "you", 
                        "that", "it", "are", "by", "as", "at", "an", "be", "from", "or", "how", "why", "what", 
                        "who", "which", "where", "when", "about", "our", "their", "my", "me", "we", "us", "i", 
                        "can", "will", "do", "get", "make", "made", "up", "out", "so", "but", "not",
                        "easy", "best", "simple", "recipe", "recipes", "quick", "ideas", "home", "diy"
                    }
                    
                    # Fetch titles and descriptions for top pins
                    top_texts = con.execute(
                        "SELECT title, description FROM pins "
                        "WHERE pin_type='created' AND (repin_count + saves) >= 5"
                    ).fetchall()
                    
                    import re as pyre
                    for title, desc in top_texts:
                        text = f"{(title or '')} {(desc or '')}".lower()
                        # Extract hashtags
                        tags = pyre.findall(r"#\w+", text)
                        for t in tags:
                            hashtag_counts[t] = hashtag_counts.get(t, 0) + 1
                        
                        # Extract words
                        words = pyre.findall(r"\b\w{3,15}\b", text)
                        for w in words:
                            if w not in stop_words and not w.isdigit():
                                word_counts[w] = word_counts.get(w, 0) + 1
                                
                    top_words = sorted(word_counts.items(), key=lambda x: x[1], reverse=True)[:20]
                    top_hashtags = sorted(hashtag_counts.items(), key=lambda x: x[1], reverse=True)[:15]

                    # 6. Formatting stats (lengths) of high-performing pins
                    high_perf = con.execute(
                        "SELECT title, description, link FROM pins "
                        "WHERE pin_type='created' AND (repin_count + saves) >= 5"
                    ).fetchall()
                    
                    avg_title_len = 0
                    avg_desc_len = 0
                    has_link_count = 0
                    
                    if high_perf:
                        total_title_len = sum(len(r[0] or '') for r in high_perf)
                        total_desc_len = sum(len(r[1] or '') for r in high_perf)
                        has_link_count = sum(1 for r in high_perf if r[2])
                        
                        avg_title_len = round(total_title_len / len(high_perf))
                        avg_desc_len = round(total_desc_len / len(high_perf))
                        link_pct = round((has_link_count / len(high_perf)) * 100)
                    else:
                        link_pct = 0

                    out = {
                        "stats": {
                            "total": tot_pins,
                            "created": created_pins,
                            "saved": saved_pins,
                            "created_pct": round((created_pins / tot_pins * 100) if tot_pins else 0, 1)
                        },
                        "top_created": top_created,
                        "top_saved": top_saved,
                        "top_domains": top_domains,
                        "top_words": top_words,
                        "top_hashtags": top_hashtags,
                        "content_recipe": {
                            "avg_title_len": avg_title_len,
                            "avg_desc_len": avg_desc_len,
                            "link_pct": link_pct,
                            "sample_size": len(high_perf)
                        }
                    }
                else:
                    out = {"error": "not found"}
                self._send(json.dumps(out, ensure_ascii=False), "application/json; charset=utf-8")
            except Exception as e:
                self._send(json.dumps({"error": str(e)}), "application/json; charset=utf-8")
            finally:
                con.close()
    return H

def serve(db_path, port=8000):
    if not os.path.exists(db_path):
        print(f"\n  ⚠  {os.path.basename(db_path)} not found — run step 4 first.\n"); sys.exit(1)
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    httpd = None
    for p in range(port, port + 25):
        try:
            httpd = socketserver.ThreadingTCPServer(("127.0.0.1", p), make_handler(db_path)); break
        except OSError:
            continue
    if not httpd:
        print("  Could not bind a local port."); sys.exit(1)
    url = f"http://127.0.0.1:{p}/"
    print(f"\n  🌐 Live database server running at:  {url}")
    print(f"     Reads {os.path.basename(db_path)} live · tabs: Pinners | Boards | Pins | Jobs | Content Lab · "
          f"Cards/Table views · click any pin for full detail")
    print(f"     Press Ctrl+C to stop.\n")
    if "--no-open" not in sys.argv[1:]:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.\n")

def main():
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding='utf-8')
            sys.stderr.reconfigure(encoding='utf-8')
        except AttributeError:
            pass

    print_stats(load())          # quick summary, then go live
    serve(DB_PATH)

if __name__ == "__main__":
    main()
