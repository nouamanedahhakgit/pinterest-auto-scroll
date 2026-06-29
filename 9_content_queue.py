"""
STEP 9 — Content Queue: browse pins and queue them for rewriting
================================================================
Run:
  python 9_content_queue.py
  python 9_content_queue.py --port 8090
"""

import os, sys, json, sqlite3, http.server, socketserver, urllib.parse, webbrowser

BASE       = os.path.dirname(os.path.abspath(__file__))
DB_PATH    = os.path.join(BASE, "sortpin.db")
ENV_PATH   = os.path.join(BASE, ".env")
QUEUE_PATH = os.path.join(BASE, "content_queue.json")
PORT       = int(next((a for a in sys.argv[1:] if a.isdigit()), "8091"))

# ── queue ─────────────────────────────────────────────────────────────────────

def load_queue():
    if os.path.exists(QUEUE_PATH):
        try:
            with open(QUEUE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_queue(q):
    with open(QUEUE_PATH, "w", encoding="utf-8") as f:
        json.dump(q, f, ensure_ascii=False, indent=2)

# ── DB ────────────────────────────────────────────────────────────────────────

def get_db():
    env = {}
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    pw = env.get("MYSQL_PASSWORD", "")
    if pw and pw != "YOUR_PASSWORD_HERE":
        try:
            import mysql.connector
            conn = mysql.connector.connect(
                host=env.get("MYSQL_HOST", "72.61.197.144"),
                port=int(env.get("MYSQL_PORT", "3306")),
                database=env.get("MYSQL_DB", "data_pint"),
                user=env.get("MYSQL_USER", "data_pint_user"),
                password=pw, charset="utf8mb4", autocommit=True
            )
            print("  Connected to MySQL.")
            return conn
        except Exception as e:
            print(f"  MySQL failed ({e}), trying SQLite.")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    print("  Using local sortpin.db.")
    return conn

def get_pins():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, pin_url, title, description, image, link,
                   COALESCE(repin_count,0)+COALESCE(saves,0) AS repins,
                   COALESCE(saves,0)         AS saves,
                   COALESCE(repin_count,0)   AS repin_count,
                   COALESCE(comment_count,0) AS comments,
                   COALESCE(like_count,0)    AS likes,
                   pin_type, pinner_username, pinner_follower_count,
                   board_name, board_category, board_follower_count,
                   created_at
            FROM pins
            ORDER BY repins DESC
        """)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        result = []
        for r in rows:
            row = dict(zip(cols, r))
            # convert datetime to string if needed
            if row.get("created_at") and not isinstance(row["created_at"], str):
                row["created_at"] = str(row["created_at"])[:10]
            result.append(row)
        return result
    except Exception as e:
        print(f"  Query error: {e}")
        return []
    finally:
        try: conn.close()
        except Exception: pass

# ── HTTP ──────────────────────────────────────────────────────────────────────

class Handler(http.server.BaseHTTPRequestHandler):
    pins  = []
    queue = {}
    def log_message(self, *a): pass

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._html()
        elif path == "/api/pins":
            self._json({"pins": Handler.pins, "queue": Handler.queue})
        else:
            self.send_error(404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        n    = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n)) if n else {}
        if path == "/api/mark":
            pid    = str(body.get("id", ""))
            status = body.get("status", "")
            q = load_queue()
            if status == "clear": q.pop(pid, None)
            elif status in ("queue","skip","done"): q[pid] = status
            save_queue(q)
            Handler.queue = q
            self._json({"ok": True})
        else:
            self.send_error(404)

    def _json(self, obj):
        data = json.dumps(obj, ensure_ascii=False, default=str).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _html(self):
        h = HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(h)))
        self.end_headers()
        self.wfile.write(h)

# ── HTML ──────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Content Queue</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f5f5f5;color:#222;display:flex;flex-direction:column;height:100vh;overflow:hidden}
header{background:#e60023;color:#fff;padding:10px 16px;display:flex;align-items:center;gap:12px;flex-shrink:0}
header h1{font-size:16px;font-weight:700}
.stats{margin-left:auto;display:flex;gap:8px;font-size:12px}
.stat{background:rgba(255,255,255,.2);border-radius:20px;padding:3px 10px}
.stat.hi{background:rgba(255,255,255,.4);font-weight:700}
.toolbar{background:#fff;border-bottom:1px solid #e0e0e0;padding:8px 12px;display:flex;gap:8px;flex-wrap:wrap;align-items:center;flex-shrink:0}
.toolbar input,.toolbar select{border:1px solid #ddd;border-radius:6px;padding:6px 10px;font-size:12px;outline:none}
.toolbar input{flex:1;min-width:180px}.toolbar input:focus,.toolbar select:focus{border-color:#e60023}
.tabs{display:flex;gap:2px;background:#f0f0f0;padding:0 12px;flex-shrink:0;border-bottom:1px solid #ddd}
.tab{padding:8px 16px;font-size:12px;font-weight:600;cursor:pointer;border-bottom:2px solid transparent;color:#666;white-space:nowrap}
.tab.on{color:#e60023;border-color:#e60023;background:#fff}
.body{display:flex;flex:1;overflow:hidden}
.table-wrap{flex:1;overflow:auto}
table{width:100%;border-collapse:collapse;font-size:12px}
thead{background:#fff;position:sticky;top:0;z-index:10}
th{padding:8px 10px;text-align:left;border-bottom:2px solid #e0e0e0;white-space:nowrap;cursor:pointer;user-select:none;color:#555}
th:hover{color:#e60023}
th.asc::after{content:' ▲'}th.desc::after{content:' ▼'}
td{padding:6px 10px;border-bottom:1px solid #f0f0f0;vertical-align:middle}
tr:hover td{background:#fff9f9;cursor:pointer}
tr.sel td{background:#fff0f0}
.thumb{width:48px;height:48px;object-fit:cover;border-radius:4px;background:#eee;display:block}
.no-img{width:48px;height:48px;border-radius:4px;background:#eee;display:flex;align-items:center;justify-content:center;font-size:18px;color:#ccc}
.badge{font-size:10px;font-weight:700;padding:2px 6px;border-radius:10px;text-transform:uppercase;white-space:nowrap}
.bc{background:#fce4ec;color:#c62828}.bs{background:#e8eaf6;color:#283593}
.sq{background:#e8f5e9;color:#2e7d32}.sd{background:#e3f2fd;color:#1565c0}.sk{background:#fff3e0;color:#e65100}
.desc-cell{max-width:260px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.num{text-align:right;color:#444;font-variant-numeric:tabular-nums}
/* detail panel */
.panel{width:360px;background:#fff;border-left:1px solid #e0e0e0;overflow-y:auto;flex-shrink:0;display:none}
.panel.open{display:flex;flex-direction:column}
.panel header{background:#333;color:#fff;padding:10px 14px;display:flex;align-items:center;justify-content:space-between;flex-shrink:0}
.panel header h2{font-size:13px;font-weight:600}
.panel .close{cursor:pointer;font-size:18px;line-height:1}
.panel .pimg{width:100%;max-height:220px;object-fit:cover;background:#eee}
.panel .info{padding:12px;display:flex;flex-direction:column;gap:8px;flex:1}
.panel .row{display:flex;gap:8px;align-items:flex-start}
.panel .lbl{font-size:10px;font-weight:700;color:#aaa;text-transform:uppercase;min-width:90px;padding-top:1px}
.panel .val{font-size:12px;color:#333;word-break:break-word}
.panel .val a{color:#e60023;text-decoration:none}.panel .val a:hover{text-decoration:underline}
.actions{display:flex;gap:8px;padding:12px;border-top:1px solid #f0f0f0;flex-shrink:0}
.abtn{flex:1;border:none;border-radius:6px;padding:9px 4px;font-size:12px;font-weight:700;cursor:pointer;transition:.15s}
.abtn-q{background:#e8f5e9;color:#2e7d32}.abtn-q:hover,.abtn-q.on{background:#2e7d32;color:#fff}
.abtn-s{background:#fff3e0;color:#e65100}.abtn-s:hover,.abtn-s.on{background:#e65100;color:#fff}
.abtn-d{background:#e3f2fd;color:#1565c0}.abtn-d:hover,.abtn-d.on{background:#1565c0;color:#fff}
.pager{display:flex;align-items:center;gap:6px;padding:6px 12px;background:#fff;border-top:1px solid #e0e0e0;font-size:12px;flex-shrink:0}
.pager button{border:1px solid #ddd;background:#fff;border-radius:4px;padding:4px 10px;cursor:pointer;font-size:12px}
.pager button:hover{background:#f5f5f5}.pager button:disabled{opacity:.4;cursor:default}
#empty{text-align:center;padding:60px;color:#aaa;font-size:14px;display:none}
</style>
</head>
<body>

<header>
  <h1>📌 Content Queue</h1>
  <div class="stats">
    <span class="stat" id="s-shown">—</span>
    <span class="stat hi" id="s-queue">Queue 0</span>
    <span class="stat" id="s-done">Done 0</span>
    <span class="stat" id="s-skip">Skip 0</span>
  </div>
</header>

<div class="toolbar">
  <input id="q" type="text" placeholder="Search description, pinner, board, category…" oninput="reset()">
  <select id="ftype" onchange="reset()">
    <option value="">All types</option>
    <option value="created">Created</option>
    <option value="saved">Saved</option>
  </select>
  <select id="frep" onchange="reset()">
    <option value="0">Any repins</option>
    <option value="1">1+</option><option value="5">5+</option>
    <option value="10">10+</option><option value="50">50+</option><option value="100">100+</option>
  </select>
  <select id="fsort" onchange="reset()">
    <option value="repins">↓ Repins</option>
    <option value="saves">↓ Saves</option>
    <option value="comments">↓ Comments</option>
    <option value="date">↓ Date</option>
  </select>
</div>

<div class="tabs">
  <div class="tab on"  onclick="tab('all',this)">All</div>
  <div class="tab" onclick="tab('none',this)">Not reviewed</div>
  <div class="tab" onclick="tab('queue',this)">Queued</div>
  <div class="tab" onclick="tab('done',this)">Done</div>
  <div class="tab" onclick="tab('skip',this)">Skipped</div>
</div>

<div class="body">
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th style="width:60px"></th>
          <th>Description</th>
          <th onclick="sortBy('repins')">Repins</th>
          <th onclick="sortBy('saves')">Saves</th>
          <th onclick="sortBy('comments')">Comments</th>
          <th>Type</th>
          <th>Status</th>
          <th>Board</th>
          <th>Pinner</th>
          <th onclick="sortBy('date')">Date</th>
        </tr>
      </thead>
      <tbody id="tbody"></tbody>
    </table>
    <div id="empty">No pins match your filters.</div>
  </div>

  <div class="panel" id="panel">
    <header>
      <h2 id="p-title">Pin Detail</h2>
      <span class="close" onclick="closePanel()">✕</span>
    </header>
    <img class="pimg" id="p-img" src="" alt="" onerror="this.style.display='none'">
    <div class="info" id="p-info"></div>
    <div class="actions">
      <button class="abtn abtn-q" id="b-q" onclick="mark('queue')">✅ Queue</button>
      <button class="abtn abtn-s" id="b-s" onclick="mark('skip')">⏭ Skip</button>
      <button class="abtn abtn-d" id="b-d" onclick="mark('done')">✔ Done</button>
    </div>
  </div>
</div>

<div class="pager">
  <button id="p-prev" onclick="page(-1)">◀ Prev</button>
  <span id="p-info2">—</span>
  <button id="p-next" onclick="page(1)">Next ▶</button>
  <span style="margin-left:auto;color:#aaa" id="p-total"></span>
</div>

<script>
const PER = 100;
let ALL=[], Q={}, filtered=[], cur=0, sortKey='repins', sortDir=-1, activeTab='all', selPin=null;

function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function num(n){return Number(n||0).toLocaleString()}
function status(id){return Q[id]||'none'}
function fmtDate(d){
  if(!d) return '—';
  try{
    const dt=new Date(d);
    if(isNaN(dt)) return String(d).slice(0,10);
    return dt.toISOString().slice(0,10);
  }catch(e){return String(d).slice(0,10);}
}

async function load(){
  const r=await fetch('/api/pins'); const d=await r.json();
  ALL=d.pins; Q=d.queue;
  reset(); updateStats();
}

function tab(t,el){
  activeTab=t;
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('on'));
  el.classList.add('on');
  reset();
}

function reset(){cur=0;filter();render();}

function filter(){
  const q=(document.getElementById('q').value||'').toLowerCase();
  const ft=document.getElementById('ftype').value;
  const fr=parseInt(document.getElementById('frep').value)||0;

  filtered=ALL.filter(p=>{
    if(ft && p.pin_type!==ft) return false;
    if(fr && (p.repins||0)<fr) return false;
    const st=status(p.id);
    if(activeTab!=='all' && st!==activeTab) return false;
    if(q){
      const t=[p.description,p.title,p.pinner_username,p.board_name,p.board_category].join(' ').toLowerCase();
      if(!t.includes(q)) return false;
    }
    return true;
  });

  // sort
  filtered.sort((a,b)=>{
    if(sortKey==='repins') return sortDir*((b.repins||0)-(a.repins||0));
    if(sortKey==='saves') return sortDir*((b.saves||0)-(a.saves||0));
    if(sortKey==='comments') return sortDir*((b.comments||0)-(a.comments||0));
    if(sortKey==='date'){const ta=new Date(a.created_at||0).getTime()||0,tb=new Date(b.created_at||0).getTime()||0;return sortDir*(tb-ta);}
    return 0;
  });
}

function sortBy(k){
  if(sortKey===k) sortDir*=-1; else{sortKey=k;sortDir=-1;}
  document.querySelectorAll('th').forEach(th=>{th.classList.remove('asc','desc');});
  const idx={repins:2,saves:3,comments:4,date:9}[k];
  if(idx!==undefined){
    const th=document.querySelectorAll('th')[idx];
    th.classList.add(sortDir===-1?'desc':'asc');
  }
  filter(); render();
}

function page(d){
  const pages=Math.ceil(filtered.length/PER);
  cur=Math.max(0,Math.min(pages-1,cur+d));
  render();
}

function render(){
  const tbody=document.getElementById('tbody');
  const empty=document.getElementById('empty');
  const slice=filtered.slice(cur*PER,(cur+1)*PER);

  if(!filtered.length){tbody.innerHTML='';empty.style.display='block';}
  else{
    empty.style.display='none';
    tbody.innerHTML=slice.map(p=>{
      const st=status(p.id);
      const img=p.image?`<img class="thumb" src="${esc(p.image)}" loading="lazy" onerror="this.outerHTML='<div class=no-img>🖼</div>'">`:`<div class="no-img">🖼</div>`;
      const badge=p.pin_type==='created'?'<span class="badge bc">Created</span>':'<span class="badge bs">Saved</span>';
      const stbadge=st==='queue'?'<span class="badge sq">Queue</span>':st==='done'?'<span class="badge sd">Done</span>':st==='skip'?'<span class="badge sk">Skip</span>':'';
      return `<tr onclick="select('${esc(p.id)}')" ${selPin&&selPin.id===p.id?'class="sel"':''}>
        <td>${img}</td>
        <td class="desc-cell">${esc(p.description||p.title||'—')}</td>
        <td class="num">${num(p.repins)}</td>
        <td class="num">${num(p.saves)}</td>
        <td class="num">${num(p.comments)}</td>
        <td>${badge}</td>
        <td>${stbadge}</td>
        <td class="desc-cell">${esc(p.board_name||'—')}</td>
        <td>${esc(p.pinner_username||'—')}</td>
        <td style="color:#888">${fmtDate(p.created_at)}</td>
      </tr>`;
    }).join('');
  }

  const pages=Math.ceil(filtered.length/PER)||1;
  document.getElementById('p-prev').disabled=cur===0;
  document.getElementById('p-next').disabled=cur>=pages-1;
  document.getElementById('p-info2').textContent=`Page ${cur+1} / ${pages}`;
  document.getElementById('p-total').textContent=`${num(filtered.length)} pins`;
  document.getElementById('s-shown').textContent=`Showing ${num(filtered.length)} / ${num(ALL.length)}`;
}

function select(id){
  selPin=ALL.find(p=>p.id===id)||null;
  if(!selPin) return;
  const panel=document.getElementById('panel');
  panel.classList.add('open');
  document.getElementById('p-title').textContent=selPin.title||selPin.description||'Pin';
  const img=document.getElementById('p-img');
  if(selPin.image){img.src=selPin.image;img.style.display='';}else{img.style.display='none';}

  const rows=[
    ['Description', selPin.description||selPin.title||'—'],
    ['Repins', num(selPin.repins)],
    ['Saves', num(selPin.saves)],
    ['Repin count', num(selPin.repin_count)],
    ['Comments', num(selPin.comments)],
    ['Likes', num(selPin.likes)],
    ['Type', selPin.pin_type||'—'],
    ['Board', selPin.board_name||'—'],
    ['Board cat.', selPin.board_category||'—'],
    ['Board followers', num(selPin.board_follower_count)],
    ['Pinner', selPin.pinner_username||'—'],
    ['Pinner followers', num(selPin.pinner_follower_count)],
    ['Date', fmtDate(selPin.created_at)],
    ['Link', selPin.link?`<a href="${esc(selPin.link)}" target="_blank">${esc(selPin.link)}</a>`:'—'],
    ['Pinterest URL', selPin.pin_url?`<a href="${esc(selPin.pin_url)}" target="_blank">Open ↗</a>`:'—'],
  ];
  document.getElementById('p-info').innerHTML=rows.map(([l,v])=>
    `<div class="row"><span class="lbl">${esc(l)}</span><span class="val">${v}</span></div>`
  ).join('');

  updatePanelButtons();
  render();
}

function closePanel(){
  document.getElementById('panel').classList.remove('open');
  selPin=null; render();
}

function updatePanelButtons(){
  if(!selPin) return;
  const st=status(selPin.id);
  document.getElementById('b-q').classList.toggle('on',st==='queue');
  document.getElementById('b-s').classList.toggle('on',st==='skip');
  document.getElementById('b-d').classList.toggle('on',st==='done');
}

async function mark(st){
  if(!selPin) return;
  const cur=status(selPin.id);
  const send=cur===st?'clear':st;
  await fetch('/api/mark',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:selPin.id,status:send})});
  if(send==='clear') delete Q[selPin.id]; else Q[selPin.id]=send;
  updatePanelButtons(); updateStats(); render();
}

function updateStats(){
  const vals=Object.values(Q);
  document.getElementById('s-queue').textContent='Queue '+vals.filter(v=>v==='queue').length;
  document.getElementById('s-done').textContent='Done '+vals.filter(v=>v==='done').length;
  document.getElementById('s-skip').textContent='Skip '+vals.filter(v=>v==='skip').length;
}

load();
</script>
</body>
</html>"""

# ── main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{'='*52}\n  CONTENT QUEUE — Pin Browser\n{'='*52}")
    print("  Loading pins …")
    Handler.pins  = get_pins()
    Handler.queue = load_queue()
    q = Handler.queue
    print(f"  Loaded {len(Handler.pins)} pins  |  Queue: {sum(1 for v in q.values() if v=='queue')}  Done: {sum(1 for v in q.values() if v=='done')}")
    url = f"http://localhost:{PORT}"
    print(f"\n  Open: {url}\n")
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", PORT), Handler) as httpd:
        webbrowser.open(url)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n  Stopped.")

if __name__ == "__main__":
    main()
