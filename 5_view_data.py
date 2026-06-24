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

import os, sys, json, webbrowser

BASE      = os.path.dirname(os.path.abspath(__file__))
JSON_PATH = os.path.join(BASE, "sortpin_data.json")
HTML_PATH = os.path.join(BASE, "sortpin_viewer.html")

def load():
    if not os.path.exists(JSON_PATH):
        print(f"\n  ⚠  {os.path.basename(JSON_PATH)} not found — run step 4 first.\n")
        sys.exit(1)
    with open(JSON_PATH, encoding="utf-8") as f:
        return json.load(f)

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

def main():
    d = load()
    print_stats(d)
    path = build_html(d)
    print(f"  🖥  Viewer built: {os.path.basename(path)}  (tabs: Pinners | Boards | Pins)")
    if "--no-open" not in sys.argv[1:]:
        webbrowser.open("file://" + path.replace("\\", "/"))
        print(f"     Opening in your browser…\n")
    else:
        print(f"     Open it manually: {path}\n")

if __name__ == "__main__":
    main()
