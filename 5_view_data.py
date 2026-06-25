"""
STEP 5 — Statistics + visual browser for the SortPin database
=============================================================
Reads the flat data built by step 4 (sortpin_data.json), prints a statistics
summary, then builds a self-contained HTML viewer and opens it.

The viewer has three tabs you can browse independently:
    • PINNERS  — search/sort people; click one to see their boards + pins
    • BOARDS   — search/sort boards;  click one to see its pins + owner
    • PINS     — search/sort every pin; click to open it on Pinterest

It is ONE offline HTML file (data embedded) — works on any computer, no server.

Run:
  python 5_view_data.py            # stats + open the viewer
  python 5_view_data.py --no-open  # just build the HTML
"""

import os, sys, json, sqlite3, webbrowser
from datetime import datetime

BASE      = os.path.dirname(os.path.abspath(__file__))
DB_PATH   = os.path.join(BASE, "sortpin.db")
JSON_PATH = os.path.join(BASE, "sortpin_data.json")
HTML_PATH = os.path.join(BASE, "sortpin_viewer.html")
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
            "profile_reach": p.get("profile_reach", 0), "nb": nb.get(u, 0), "np": np_.get(u, 0),
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
        return load_from_db(DB_PATH)
    if os.path.exists(JSON_PATH):
        with open(JSON_PATH, encoding="utf-8") as f:
            return json.load(f)
    print(f"\n  ⚠  No database found — run step 4 first.\n")
    sys.exit(1)

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

    print(f"\n{'='*62}\n  SortPin data — statistics   ({d.get('generated_at','')})\n{'='*62}")
    print(f"  Pinners : {s['pinners']:>7}")
    print(f"  Boards  : {s['boards']:>7}")
    print(f"  Pins    : {s['pins']:>7}")
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

# ── HTML viewer ───────────────────────────────────────────────────────────────
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SortPin Explorer</title>
<style>
  :root{--bg:#0f1115;--panel:#171a21;--panel2:#1f232c;--line:#2a2f3a;
        --txt:#e7e9ee;--muted:#9aa3b2;--accent:#e60023;--accent2:#3b82f6;}
  *{box-sizing:border-box}
  body{margin:0;font:14px/1.45 -apple-system,Segoe UI,Roboto,Arial,sans-serif;background:var(--bg);color:var(--txt)}
  header{padding:12px 18px;background:var(--panel);border-bottom:1px solid var(--line);
         display:flex;align-items:center;gap:14px;flex-wrap:wrap;position:sticky;top:0;z-index:10}
  header h1{font-size:16px;margin:0}header h1 b{color:var(--accent)}
  .tabs{display:flex;gap:6px;margin-left:8px}
  .tab{padding:6px 14px;border:1px solid var(--line);border-radius:20px;cursor:pointer;color:var(--muted);font-size:13px}
  .tab.on{background:var(--accent);border-color:var(--accent);color:#fff}
  .tab b{font-size:11px;opacity:.8}
  .controls{padding:10px 18px;display:flex;gap:10px;align-items:center;flex-wrap:wrap;border-bottom:1px solid var(--line);background:var(--panel)}
  .controls input,.controls select{padding:8px 10px;background:var(--panel2);border:1px solid var(--line);border-radius:8px;color:var(--txt);font-size:13px}
  .controls input{flex:1;min-width:200px}
  .count{color:var(--muted);font-size:12px}
  .wrap{padding:16px 18px}
  .crumb{color:var(--muted);font-size:13px;margin-bottom:14px}
  .crumb a{color:var(--accent2);cursor:pointer}
  /* lists */
  .rows{display:flex;flex-direction:column;gap:1px}
  .row{display:flex;gap:12px;padding:10px 12px;background:var(--panel);border:1px solid var(--line);border-radius:10px;cursor:pointer;align-items:center}
  .row:hover{border-color:var(--accent2)}
  .av{width:44px;height:44px;border-radius:50%;background:var(--panel2);object-fit:cover;flex:none}
  .row .nm{font-weight:600}.row .sub{color:var(--muted);font-size:12px}
  .row .mini{color:var(--muted);font-size:12px;margin-top:2px}.row .mini b{color:var(--txt)}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:12px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden;cursor:pointer}
  .card:hover{border-color:var(--accent2)}
  .card img{width:100%;height:150px;object-fit:cover;background:var(--panel2);display:block}
  .card .bd{padding:9px 11px}.card .t{font-weight:600;font-size:13px;max-height:38px;overflow:hidden}
  .card .m{color:var(--muted);font-size:11px;margin-top:5px;display:flex;justify-content:space-between}
  .card a{color:var(--accent2);text-decoration:none}
  .phead{display:flex;gap:16px;align-items:center;margin-bottom:8px}
  .phead img{width:64px;height:64px;border-radius:50%;background:var(--panel2)}
  .phead h2{margin:0;font-size:20px}.phead .muted{color:var(--muted);font-size:13px}
  .kv{display:flex;gap:18px;flex-wrap:wrap;margin:10px 0 18px;font-size:12px;color:var(--muted)}
  .kv b{color:var(--txt)}.kv a{color:var(--accent2);text-decoration:none}
  .sectit{font-size:13px;text-transform:uppercase;letter-spacing:.6px;color:var(--muted);margin:18px 0 10px;border-bottom:1px solid var(--line);padding-bottom:6px}
  .empty{color:var(--muted);padding:40px;text-align:center}
</style></head>
<body>
<header>
  <h1><b>SortPin</b> Explorer</h1>
  <div class="tabs">
    <div class="tab on" data-tab="pinners">Pinners <b id="c_pinners"></b></div>
    <div class="tab" data-tab="boards">Boards <b id="c_boards"></b></div>
    <div class="tab" data-tab="pins">Pins <b id="c_pins"></b></div>
  </div>
  <span class="count" id="gen"></span>
</header>
<div class="controls">
  <input id="q" placeholder="Search…">
  <select id="sort"></select>
  <span class="count" id="showing"></span>
</div>
<div class="wrap" id="main"></div>

<script id="data" type="application/json">__DATA__</script>
<script>
const DB = JSON.parse(document.getElementById('data').textContent);
const PIN = DB.pinners, BRD = DB.boards, PNS = DB.pins;
const LIMIT = 400;

// ---- indexes for relationships ----
const pinnerBy = {}; PIN.forEach(p => pinnerBy[p.username] = p);
const boardById = {}; BRD.forEach(b => boardById[b.id] = b);
const boardsByOwner = {}; BRD.forEach(b => (boardsByOwner[b.owner_username] ||= []).push(b));
const pinsByPinner = {}; PNS.forEach(p => (pinsByPinner[p.pinner_username] ||= []).push(p));
const pinsByBoard  = {}; PNS.forEach(p => { if(p.board_id) (pinsByBoard[p.board_id] ||= []).push(p); });

document.getElementById('c_pinners').textContent = DB.stats.pinners.toLocaleString();
document.getElementById('c_boards').textContent  = DB.stats.boards.toLocaleString();
document.getElementById('c_pins').textContent    = DB.stats.pins.toLocaleString();
document.getElementById('gen').textContent       = 'built ' + (DB.generated_at||'');

const esc = s => (s==null?'':String(s)).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const num = n => (n||0).toLocaleString();
const img = (u,cls,h) => u ? `<img class="${cls}" loading="lazy" src="${esc(u)}" onerror="this.style.visibility='hidden'">`
                           : `<div class="${cls}" style="${h?('height:'+h):''}"></div>`;

let tab = 'pinners';
const SORTS = {
  pinners: [['np','pins scraped'],['nb','boards scraped'],['follower_count','followers'],['profile_reach','reach'],['pin_count','total pins']],
  boards:  [['pin_count','pins on Pinterest'],['follower_count','followers']],
  pins:    [['repin_count','repins'],['saves','saves']],
};

function setTab(t){
  tab=t;
  [...document.querySelectorAll('.tab')].forEach(e=>e.classList.toggle('on',e.dataset.tab===t));
  const sel=document.getElementById('sort'); sel.innerHTML='';
  SORTS[t].forEach(([k,lab])=>{const o=document.createElement('option');o.value=k;o.textContent='Sort: '+lab;sel.appendChild(o);});
  document.getElementById('q').value='';
  render();
}

function matches(o, q, fields){ return !q || fields.some(f=>(o[f]||'').toString().toLowerCase().includes(q)); }

function render(){
  const q=document.getElementById('q').value.trim().toLowerCase();
  const key=document.getElementById('sort').value;
  const main=document.getElementById('main');
  let src, html, total;
  if(tab==='pinners'){
    let rows=PIN.filter(p=>matches(p,q,['full_name','username','contact_email','website_url','domain_url']));
    total=rows.length; rows=rows.sort((a,b)=>(b[key]||0)-(a[key]||0)).slice(0,LIMIT);
    html='<div class="rows">'+rows.map(p=>`
      <div class="row" onclick="showPinner('${esc(p.username)}')">
        ${img(p.image_url,'av')}
        <div style="flex:1;min-width:0">
          <div class="nm">${esc(p.full_name||p.username)}</div>
          <div class="sub">@${esc(p.username)}${p.contact_email?' · '+esc(p.contact_email):''}${p.website_url?' · '+esc(p.domain_url||p.website_url):''}</div>
          <div class="mini"><b>${num(p.follower_count)}</b> followers · <b>${p.nb}</b> boards · <b>${p.np}</b> pins</div>
        </div></div>`).join('')+'</div>';
  } else if(tab==='boards'){
    let rows=BRD.filter(b=>matches(b,q,['name','description','owner_username','category']));
    total=rows.length; rows=rows.sort((a,b)=>(b[key]||0)-(a[key]||0)).slice(0,LIMIT);
    html='<div class="grid">'+rows.map(b=>`
      <div class="card" onclick="showBoard('${esc(b.id)}')">
        ${img(b.image_cover_url,'',150)}
        <div class="bd"><div class="t">${esc(b.name||'(untitled)')}</div>
          <div class="m"><span>${num(b.pin_count)} pins</span><span>@${esc(b.owner_username||'')}</span></div></div>
      </div>`).join('')+'</div>';
  } else {
    let rows=PNS.filter(p=>matches(p,q,['title','description','board_name','pinner_username']));
    total=rows.length; rows=rows.sort((a,b)=>(b[key]||0)-(a[key]||0)).slice(0,LIMIT);
    html='<div class="grid">'+rows.map(pinCard).join('')+'</div>';
  }
  document.getElementById('showing').textContent =
    `${Math.min(total,LIMIT).toLocaleString()} of ${total.toLocaleString()}` + (total>LIMIT?' (refine search)':'');
  main.innerHTML = total? html : '<div class="empty">Nothing matches.</div>';
}

function pinCard(p){
  return `<div class="card">
    ${p.pin_url?`<a href="${esc(p.pin_url)}" target="_blank">${img(p.image,'',150)}</a>`:img(p.image,'',150)}
    <div class="bd"><div class="t">${esc(p.title||p.description||'(no title)')}</div>
      <div class="m"><span>♥ ${num(p.repin_count||p.saves)}</span>
        ${p.pinner_username?`<a onclick="showPinner('${esc(p.pinner_username)}')">@${esc(p.pinner_username)}</a>`:''}</div></div></div>`;
}

function showPinner(u){
  const p=pinnerBy[u]; if(!p){return;}
  const boards=(boardsByOwner[u]||[]).slice().sort((a,b)=>(b.pin_count||0)-(a.pin_count||0));
  const pins=(pinsByPinner[u]||[]);
  document.getElementById('main').innerHTML=`
    <div class="crumb"><a onclick="setTab('pinners')">Pinners</a> › ${esc(p.full_name||p.username)}</div>
    <div class="phead">${img(p.image_url,'av').replace('class="av"','class="av" style="width:64px;height:64px"')}
      <div><h2>${esc(p.full_name||p.username)}</h2><div class="muted">@${esc(p.username)}</div></div></div>
    <div class="kv">
      <span><b>${num(p.follower_count)}</b> followers</span><span><b>${num(p.pin_count)}</b> pins (profile)</span>
      <span><b>${num(p.board_count)}</b> boards (profile)</span><span><b>${num(p.profile_reach)}</b> reach</span>
      ${p.website_url?`<span>🌐 <a href="${esc(p.website_url)}" target="_blank">${esc(p.domain_url||p.website_url)}</a></span>`:''}
      ${p.contact_email?`<span>✉ <a href="mailto:${esc(p.contact_email)}">${esc(p.contact_email)}</a></span>`:''}</div>
    <div class="sectit">Boards (${boards.length})</div>
    ${boards.length?`<div class="grid">${boards.map(b=>`
       <div class="card" onclick="showBoard('${esc(b.id)}')">${img(b.image_cover_url,'',150)}
         <div class="bd"><div class="t">${esc(b.name||'(untitled)')}</div>
           <div class="m"><span>${num(b.pin_count)} pins</span><span>${(pinsByBoard[b.id]||[]).length} scraped</span></div></div></div>`).join('')}</div>`
      :'<div class="empty">No boards scraped.</div>'}
    <div class="sectit">Pins (${pins.length})</div>
    ${pins.length?`<div class="grid">${pins.slice(0,LIMIT).map(pinCard).join('')}</div>`:'<div class="empty">No pins scraped for this pinner.</div>'}`;
}

function showBoard(id){
  const b=boardById[id]; if(!b){return;}
  const pins=(pinsByBoard[id]||[]);
  const owner=pinnerBy[b.owner_username];
  document.getElementById('main').innerHTML=`
    <div class="crumb"><a onclick="setTab('boards')">Boards</a> ›
      ${owner?`<a onclick="showPinner('${esc(b.owner_username)}')">@${esc(b.owner_username)}</a> › `:''}${esc(b.name)}</div>
    <div class="phead">${img(b.image_cover_url,'av').replace('class="av"','class="av" style="width:64px;height:64px;border-radius:10px"')}
      <div><h2>${esc(b.name)}</h2>
        <div class="muted">${num(b.pin_count)} pins on Pinterest · ${num(b.follower_count)} followers · ${pins.length} scraped</div>
        ${b.url?`<div><a href="${esc(b.url)}" target="_blank" style="color:var(--accent2)">open board ↗</a></div>`:''}</div></div>
    ${b.description?`<div class="kv"><span>${esc(b.description)}</span></div>`:''}
    <div class="sectit">Pins scraped (${pins.length})</div>
    ${pins.length?`<div class="grid">${pins.slice(0,LIMIT).map(pinCard).join('')}</div>`:'<div class="empty">No individual pins scraped for this board.</div>'}`;
}

[...document.querySelectorAll('.tab')].forEach(e=>e.onclick=()=>setTab(e.dataset.tab));
document.getElementById('q').oninput=render;
document.getElementById('sort').onchange=render;
setTab('pinners');
</script>
</body></html>"""

def build_html(d):
    payload = json.dumps(d, ensure_ascii=False).replace("</", "<\\/")
    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write(HTML_TEMPLATE.replace("__DATA__", payload))
    return HTML_PATH

# ══════════════════════════════════════════════════════════════════════════════
#  LIVE SERVER  —  python 5_view_data.py --server
#  Serves directly from sortpin.db (live), with card views, a raw TABLE view of
#  every column, and a pin-detail view showing all fields + image.
# ══════════════════════════════════════════════════════════════════════════════
import urllib.parse, http.server, socketserver

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
</style></head><body>
<header>
 <h1><b>SortPin</b> Live <span class="live">● database</span></h1>
 <div class="tab on" data-t="pinners">Pinners <b id="c_pinners">·</b></div>
 <div class="tab" data-t="boards">Boards <b id="c_boards">·</b></div>
 <div class="tab" data-t="pins">Pins <b id="c_pins">·</b></div>
 <div class="tab" data-t="jobs">Jobs ⏱</div>
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
 if(t==='jobs'){ renderJobs(); } else { load(); }}

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
   const col=e.event==='keyword'?'':' style="color:var(--muted)"';
   return `<tr${col}><td>${esc(e.ts)}</td><td>${esc(e.computer)}</td><td>${esc(e.cycle)}</td><td>${esc(e.event)}</td><td>${esc(kwc)}</td><td>${e.minutes!=null?esc(e.minutes):''}</td><td>${e.pins!=null?esc(e.pins):''}</td><td>${esc(e.why||'')}</td></tr>`;
 }).join('');
 html+='</tbody></table></div>';
 m.innerHTML=html;
}
setInterval(()=>{ if(state.t==='jobs') renderJobs(); }, 5000);

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
 if(state.t==='pins')return `<div class="card" data-type="pins" data-id="${esc(row.id)}">${imgCell(row)}<div class="bd"><div class="t">${esc(row.title||row.description||'(no title)')}</div><div class="m"><span>♥ ${num(row.repin_count||row.saves)}</span><span>@${esc(row.pinner_username||'')}</span></div></div></div>`;
 if(state.t==='boards')return `<div class="card" data-type="boards" data-id="${esc(row.id)}">${row.image_cover_url?`<img loading="lazy" src="${esc(row.image_cover_url)}" onerror="this.style.visibility='hidden'">`:'<div style="height:160px"></div>'}<div class="bd"><div class="t">${esc(row.name||'(untitled)')}</div><div class="m"><span>${num(row.pin_count)} pins</span><span>@${esc(row.owner_username||'')}</span></div></div></div>`;
 return `<div class="card" data-type="pinner" data-id="${esc(row.username)}"><div class="bd"><div class="t">${esc(row.full_name||row.username)}</div><div class="m"><span>${num(row.follower_count)} foll</span><span>@${esc(row.username)}</span></div><div class="muted" style="margin-top:4px">${esc(row.contact_email||row.website_url||'')}</div></div></div>`;
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
   m.innerHTML=head+kvTable(row)+
     `<div class="sectit">Boards (${(ch.boards||[]).length})</div><div class="grid">${(ch.boards||[]).map(b=>boardMini(b)).join('')||'<div class=empty>none</div>'}</div>`+
     `<div class="sectit">Pins (${(ch.pins||[]).length})</div><div class="grid">${(ch.pins||[]).map(p=>pinMini(p)).join('')||'<div class=empty>none</div>'}</div>`;
   bindMini(m);
 }
}
function pkOf(t){return t==='pinners'?'username':'id';}
function pinMini(p){return `<div class="card" data-type="pins" data-id="${esc(p.id)}">${imgCell(p)}<div class="bd"><div class="t">${esc(p.title||p.description||'(no title)')}</div><div class="m"><span>♥ ${num(p.repin_count||p.saves)}</span></div></div></div>`;}
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
    print(f"     Reads {os.path.basename(db_path)} live · tabs: Pinners | Boards | Pins · "
          f"Cards/Table views · click any pin for full detail")
    print(f"     Press Ctrl+C to stop.\n")
    if "--no-open" not in sys.argv[1:]:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.\n")

def main():
    if "--server" in sys.argv[1:]:
        print_stats(load())          # quick summary, then go live
        serve(DB_PATH)
        return
    d = load()
    print_stats(d)
    path = build_html(d)
    print(f"  🖥  Static viewer built: {os.path.basename(path)}  (tabs: Pinners | Boards | Pins)")
    print(f"     TIP: for a LIVE database view with raw tables + full pin detail, run:")
    print(f"          python 5_view_data.py --server")
    if "--no-open" not in sys.argv[1:]:
        webbrowser.open("file://" + path.replace("\\", "/"))
        print(f"     Opening in your browser…\n")
    else:
        print(f"     Open it manually: {path}\n")

if __name__ == "__main__":
    main()
