"""
STEP 5 — Statistics + visual browser for the SortPin database
=============================================================
Reads the relational data built by step 4 (sortpin_data.json / sortpin.db),
prints a statistics summary in the terminal, then builds a self-contained
HTML viewer and opens it in your browser so you can navigate:

        PINNER  →  BOARDS  →  PINS

The viewer is ONE offline HTML file (data embedded) — it works on any
computer with no server and no internet.

Run:
  python 5_view_data.py            # stats + open the viewer
  python 5_view_data.py --no-open  # just build the HTML, don't open it
"""

import os, sys, json, sqlite3, webbrowser
from datetime import datetime

BASE      = os.path.dirname(os.path.abspath(__file__))
JSON_PATH = os.path.join(BASE, "sortpin_data.json")
DB_PATH   = os.path.join(BASE, "sortpin.db")
HTML_PATH = os.path.join(BASE, "sortpin_viewer.html")

def load():
    if not os.path.exists(JSON_PATH):
        print(f"\n  ⚠  {os.path.basename(JSON_PATH)} not found.\n"
              f"     Run  python 4_build_database.py  first.\n")
        sys.exit(1)
    with open(JSON_PATH, encoding="utf-8") as f:
        return json.load(f)

# ── terminal statistics ───────────────────────────────────────────────────────
def print_stats(data):
    pinners = data["pinners"]
    s = data["stats"]
    n_with_email   = sum(1 for p in pinners if p.get("contact_email"))
    n_with_site    = sum(1 for p in pinners if p.get("website_url"))
    n_with_boards  = sum(1 for p in pinners if p.get("_n_boards"))
    n_with_pins    = sum(1 for p in pinners if p.get("_n_pins"))
    total_reach    = sum(p.get("profile_reach", 0) for p in pinners)
    total_foll     = sum(p.get("follower_count", 0) for p in pinners)

    def bar(label, value, total):
        pct = (value / total * 100) if total else 0
        fill = int(pct / 4)
        return f"  {label:<22}{value:>6}  [{'█'*fill}{'░'*(25-fill)}] {pct:4.0f}%"

    print(f"\n{'='*62}")
    print(f"  SortPin data — statistics   ({data.get('generated_at','')})")
    print(f"{'='*62}")
    print(f"  Pinners : {s['pinners']:>6}")
    print(f"  Boards  : {s['boards']:>6}")
    print(f"  Pins    : {s['pins']:>6}")
    print(f"{'-'*62}")
    print(bar("with email",   n_with_email,  s["pinners"]))
    print(bar("with website", n_with_site,   s["pinners"]))
    print(bar("with boards",  n_with_boards, s["pinners"]))
    print(bar("with pins",    n_with_pins,   s["pinners"]))
    print(f"{'-'*62}")
    print(f"  Total followers (all pinners): {total_foll:,}")
    print(f"  Total profile reach          : {total_reach:,}")

    print(f"\n  ── Top 10 pinners by followers ──")
    top = sorted(pinners, key=lambda p: p.get("follower_count", 0), reverse=True)[:10]
    for i, p in enumerate(top, 1):
        name = (p.get("full_name") or p.get("username") or "")[:28]
        print(f"   {i:>2}. {name:<28} {p.get('follower_count',0):>9,}  "
              f"@{p.get('username','')[:20]:<20} "
              f"boards:{p.get('_n_boards',0):>3} pins:{p.get('_n_pins',0):>3}")

    # top boards across everyone
    boards = []
    for p in pinners:
        for b in p.get("boards", []):
            boards.append((b.get("pin_count", 0), b.get("name", ""), p.get("username", "")))
    boards.sort(reverse=True)
    print(f"\n  ── Top 10 boards by pin count ──")
    for i, (pc, name, owner) in enumerate(boards[:10], 1):
        print(f"   {i:>2}. {name[:38]:<38} {pc:>6} pins   @{owner[:18]}")
    print(f"{'='*62}\n")

# ── HTML viewer (self-contained, vanilla JS) ──────────────────────────────────
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SortPin Explorer</title>
<style>
  :root{ --bg:#0f1115; --panel:#171a21; --panel2:#1f232c; --line:#2a2f3a;
         --txt:#e7e9ee; --muted:#9aa3b2; --accent:#e60023; --accent2:#3b82f6; }
  *{box-sizing:border-box}
  body{margin:0;font:14px/1.45 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
       background:var(--bg);color:var(--txt)}
  header{padding:14px 20px;background:var(--panel);border-bottom:1px solid var(--line);
         display:flex;align-items:center;gap:18px;flex-wrap:wrap;position:sticky;top:0;z-index:5}
  header h1{font-size:16px;margin:0;letter-spacing:.5px}
  header h1 b{color:var(--accent)}
  .stat{font-size:12px;color:var(--muted)}
  .stat b{color:var(--txt);font-size:14px}
  .wrap{display:flex;height:calc(100vh - 53px)}
  .side{width:360px;min-width:300px;border-right:1px solid var(--line);
        background:var(--panel);display:flex;flex-direction:column}
  .controls{padding:10px;border-bottom:1px solid var(--line);display:flex;flex-direction:column;gap:8px}
  .controls input,.controls select{width:100%;padding:8px 10px;background:var(--panel2);
        border:1px solid var(--line);border-radius:8px;color:var(--txt);font-size:13px}
  .chips{display:flex;gap:6px;flex-wrap:wrap}
  .chip{font-size:11px;padding:4px 9px;border:1px solid var(--line);border-radius:20px;
        cursor:pointer;color:var(--muted);user-select:none}
  .chip.on{background:var(--accent2);border-color:var(--accent2);color:#fff}
  .list{overflow:auto;flex:1}
  .pinner{display:flex;gap:10px;padding:9px 12px;border-bottom:1px solid var(--line);cursor:pointer}
  .pinner:hover{background:var(--panel2)}
  .pinner.sel{background:#222a39;border-left:3px solid var(--accent)}
  .av{width:40px;height:40px;border-radius:50%;background:var(--panel2);object-fit:cover;flex:none}
  .pinner .nm{font-weight:600;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .pinner .sub{color:var(--muted);font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .pinner .mini{color:var(--muted);font-size:11px;margin-top:2px}
  .pinner .mini b{color:var(--txt)}
  .main{flex:1;overflow:auto;padding:20px}
  .crumb{color:var(--muted);font-size:12px;margin-bottom:14px}
  .crumb a{color:var(--accent2);cursor:pointer;text-decoration:none}
  .phead{display:flex;gap:16px;align-items:center;margin-bottom:8px}
  .phead img{width:64px;height:64px;border-radius:50%;background:var(--panel2)}
  .phead h2{margin:0;font-size:20px}
  .phead .muted{color:var(--muted);font-size:13px}
  .kv{display:flex;gap:18px;flex-wrap:wrap;margin:10px 0 22px;font-size:12px;color:var(--muted)}
  .kv b{color:var(--txt)}
  .kv a{color:var(--accent2);text-decoration:none}
  .sectit{font-size:13px;text-transform:uppercase;letter-spacing:.6px;color:var(--muted);
          margin:18px 0 10px;border-bottom:1px solid var(--line);padding-bottom:6px}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden;cursor:pointer}
  .card:hover{border-color:var(--accent2)}
  .card .cv{width:100%;height:120px;object-fit:cover;background:var(--panel2);display:block}
  .card .bd{padding:9px 11px}
  .card .bd .t{font-weight:600;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .card .bd .m{color:var(--muted);font-size:11px;margin-top:3px}
  .pin{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden}
  .pin img{width:100%;height:150px;object-fit:cover;background:var(--panel2);display:block}
  .pin .bd{padding:9px 11px}
  .pin .t{font-size:12px;line-height:1.35;max-height:50px;overflow:hidden}
  .pin .m{color:var(--muted);font-size:11px;margin-top:6px;display:flex;justify-content:space-between}
  .pin a{color:var(--accent2);text-decoration:none}
  .empty{color:var(--muted);padding:40px;text-align:center}
  .tag{display:inline-block;background:var(--panel2);border:1px solid var(--line);
       border-radius:6px;padding:1px 7px;font-size:11px;color:var(--muted)}
</style>
</head>
<body>
<header>
  <h1><b>SortPin</b> Explorer</h1>
  <span class="stat"><b id="s_pinners">0</b> pinners</span>
  <span class="stat"><b id="s_boards">0</b> boards</span>
  <span class="stat"><b id="s_pins">0</b> pins</span>
  <span class="stat" id="s_gen"></span>
</header>
<div class="wrap">
  <div class="side">
    <div class="controls">
      <input id="q" placeholder="Search name / @username / email / website…">
      <select id="sort">
        <option value="follower_count">Sort: followers</option>
        <option value="_n_pins">Sort: pins scraped</option>
        <option value="_n_boards">Sort: boards scraped</option>
        <option value="profile_reach">Sort: profile reach</option>
        <option value="pin_count">Sort: total pins (profile)</option>
      </select>
      <div class="chips">
        <span class="chip" data-f="pins">has pins</span>
        <span class="chip" data-f="boards">has boards</span>
        <span class="chip" data-f="email">has email</span>
      </div>
    </div>
    <div class="list" id="list"></div>
  </div>
  <div class="main" id="main"><div class="empty">Select a pinner on the left to explore their boards and pins.</div></div>
</div>

<script id="data" type="application/json">__DATA__</script>
<script>
const DB = JSON.parse(document.getElementById('data').textContent);
const P  = DB.pinners;
const filters = {pins:false, boards:false, email:false};
let selected = null;

document.getElementById('s_pinners').textContent = DB.stats.pinners.toLocaleString();
document.getElementById('s_boards').textContent  = DB.stats.boards.toLocaleString();
document.getElementById('s_pins').textContent    = DB.stats.pins.toLocaleString();
document.getElementById('s_gen').textContent     = 'built ' + (DB.generated_at||'');

const esc = s => (s==null?'':String(s)).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const num = n => (n||0).toLocaleString();
const img = (u,cls) => u ? `<img class="${cls}" loading="lazy" src="${esc(u)}" onerror="this.style.visibility='hidden'">`
                         : `<div class="${cls}"></div>`;

function passes(p){
  if(filters.pins   && !p._n_pins)   return false;
  if(filters.boards && !p._n_boards) return false;
  if(filters.email  && !p.contact_email) return false;
  const q = document.getElementById('q').value.trim().toLowerCase();
  if(!q) return true;
  return [p.full_name,p.username,p.contact_email,p.website_url,p.domain_url]
         .some(v => (v||'').toLowerCase().includes(q));
}

function renderList(){
  const key = document.getElementById('sort').value;
  const rows = P.filter(passes).sort((a,b)=>(b[key]||0)-(a[key]||0)).slice(0, 600);
  const host = document.getElementById('list');
  host.innerHTML = rows.map((p,i)=>`
    <div class="pinner ${selected===p?'sel':''}" data-u="${esc(p.username)}">
      ${img(p.image_url,'av')}
      <div style="min-width:0;flex:1">
        <div class="nm">${esc(p.full_name||p.username)}</div>
        <div class="sub">@${esc(p.username)}${p.contact_email?' · '+esc(p.contact_email):''}</div>
        <div class="mini"><b>${num(p.follower_count)}</b> foll · <b>${p._n_boards}</b> boards · <b>${p._n_pins}</b> pins</div>
      </div>
    </div>`).join('') ||
    '<div class="empty">No pinners match.</div>';
  if(P.filter(passes).length > 600)
    host.insertAdjacentHTML('beforeend','<div class="empty">Showing first 600 — refine your search.</div>');
  [...host.querySelectorAll('.pinner')].forEach(el=>{
    el.onclick = () => { selected = P.find(x=>x.username===el.dataset.u); renderList(); showPinner(selected); };
  });
}

function showPinner(p){
  const boards = p.boards.map(b=>`
    <div class="card" data-b="${esc(b.id)}">
      ${img(b.image_cover_url,'cv')}
      <div class="bd"><div class="t">${esc(b.name||'(untitled board)')}</div>
        <div class="m">${num(b.pin_count)} pins · ${num(b.follower_count)} followers · ${b.pins.length} scraped</div></div>
    </div>`).join('');
  const loose = p.loose_pins.length ? `<div class="sectit">Other pins (${p.loose_pins.length})</div>
       <div class="grid">${p.loose_pins.map(pinCard).join('')}</div>` : '';
  document.getElementById('main').innerHTML = `
    <div class="crumb"><a onclick="back()">Pinners</a> › ${esc(p.full_name||p.username)}</div>
    <div class="phead">${img(p.image_url,'av').replace('class="av"','class="av" style="width:64px;height:64px"')}
      <div><h2>${esc(p.full_name||p.username)}</h2>
        <div class="muted">@${esc(p.username)}</div></div></div>
    <div class="kv">
      <span><b>${num(p.follower_count)}</b> followers</span>
      <span><b>${num(p.pin_count)}</b> pins (profile)</span>
      <span><b>${num(p.board_count)}</b> boards (profile)</span>
      <span><b>${num(p.profile_reach)}</b> reach</span>
      ${p.website_url?`<span>🌐 <a href="${esc(p.website_url)}" target="_blank">${esc(p.domain_url||p.website_url)}</a></span>`:''}
      ${p.contact_email?`<span>✉ <a href="mailto:${esc(p.contact_email)}">${esc(p.contact_email)}</a></span>`:''}
      ${p.contact_phone?`<span>☎ ${esc(p.contact_phone)}</span>`:''}
    </div>
    <div class="sectit">Boards (${p.boards.length})</div>
    ${boards?`<div class="grid">${boards}</div>`:'<div class="empty">No boards scraped for this pinner.</div>'}
    ${loose}`;
  [...document.querySelectorAll('.card[data-b]')].forEach(el=>{
    el.onclick = () => { const b=p.boards.find(x=>x.id===el.dataset.b); showBoard(p,b); };
  });
}

function pinCard(pin){
  return `<div class="pin">
    ${img(pin.image,'')}
    <div class="bd"><div class="t">${esc(pin.title||pin.description||'(no title)')}</div>
      <div class="m"><span>♥ ${num(pin.repin_count||pin.saves)}</span>
        ${pin.pin_url?`<a href="${esc(pin.pin_url)}" target="_blank">open ↗</a>`:''}</div></div></div>`;
}

function showBoard(p,b){
  document.getElementById('main').innerHTML = `
    <div class="crumb"><a onclick="back()">Pinners</a> › <a onclick="reopen('${esc(p.username)}')">${esc(p.full_name||p.username)}</a> › ${esc(b.name)}</div>
    <div class="phead">${img(b.image_cover_url,'av').replace('class="av"','class="av" style="width:64px;height:64px;border-radius:10px"')}
      <div><h2>${esc(b.name)}</h2>
        <div class="muted">${num(b.pin_count)} pins on Pinterest · ${num(b.follower_count)} followers · ${b.pins.length} pins scraped</div>
        ${b.url?`<div><a href="${esc(b.url)}" target="_blank" style="color:var(--accent2)">open board ↗</a></div>`:''}</div></div>
    ${b.description?`<div class="kv"><span>${esc(b.description)}</span></div>`:''}
    <div class="sectit">Pins scraped (${b.pins.length})</div>
    ${b.pins.length?`<div class="grid">${b.pins.map(pinCard).join('')}</div>`
                   :'<div class="empty">No individual pins scraped for this board yet.</div>'}`;
}

function back(){ selected=null; renderList();
  document.getElementById('main').innerHTML='<div class="empty">Select a pinner on the left.</div>'; }
function reopen(u){ const p=P.find(x=>x.username===u); selected=p; renderList(); showPinner(p); }

document.getElementById('q').oninput = renderList;
document.getElementById('sort').onchange = renderList;
[...document.querySelectorAll('.chip')].forEach(c=>{
  c.onclick = () => { filters[c.dataset.f]=!filters[c.dataset.f]; c.classList.toggle('on'); renderList(); };
});
renderList();
</script>
</body>
</html>"""

def build_html(data):
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = HTML_TEMPLATE.replace("__DATA__", payload)
    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    return HTML_PATH

def main():
    data = load()
    print_stats(data)
    path = build_html(data)
    print(f"  🖥  Viewer built: {os.path.basename(path)}")
    if "--no-open" not in sys.argv[1:]:
        webbrowser.open("file://" + path.replace("\\", "/"))
        print(f"     Opening in your browser…  (Pinner → Boards → Pins)\n")
    else:
        print(f"     Open it manually: {path}\n")

if __name__ == "__main__":
    main()
