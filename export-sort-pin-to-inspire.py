import os, re, csv
from pathlib import Path

EXT_ID = "djcledakkebdgjncnemijiabiaimbaic"

SCRIPT_DIR = Path(__file__).resolve().parent
OUT = SCRIPT_DIR / "sortpin_output"
OUT.mkdir(exist_ok=True)

def find_sortpin_db():
    local = Path(os.environ.get("LOCALAPPDATA", ""))

    bases = [
        local / "BraveSoftware/Brave-Browser/User Data",
        local / "Google/Chrome/User Data",
        local / "Microsoft/Edge/User Data",
    ]

    for base in bases:
        if base.exists():
            for db in base.glob(f"**/IndexedDB/chrome-extension_{EXT_ID}_0.indexeddb.leveldb"):
                return db

    current = SCRIPT_DIR
    if any(current.glob("*.ldb")) or any(current.glob("*.log")):
        return current

    return None

DB_PATH = find_sortpin_db()
if not DB_PATH:
    print("SortPin DB not found.")
    input("Press Enter to exit...")
    raise SystemExit

print("DB found:", DB_PATH)
print("Output:", OUT)

def read_all_text():
    txt = ""
    for fn in DB_PATH.iterdir():
        if fn.suffix.lower() in [".ldb", ".log"]:
            try:
                txt += fn.read_bytes().decode("utf-8", errors="ignore").replace("\x00", "") + "\n"
            except:
                pass
    return txt

def clean(v):
    return re.sub(r"[\x00-\x1f\x7f]", "", v or "").strip()

def get(block, key):
    m = re.search(re.escape(key) + r'".{1}([^"{\x00-\x1f]{0,800})', block, re.S)
    return clean(m.group(1)) if m else ""

def get_int(block, key):
    m = re.search(re.escape(key) + r"I([0-9]+)", block)
    return m.group(1) if m else ""

def all_urls(block):
    return "\n".join(sorted(set(re.findall(r"https://[^\s\"{}<>]+", block))))

def first_url(block, contains=""):
    for u in re.findall(r"https://[^\s\"{}<>]+", block):
        if contains in u:
            return u
    return ""

def save_csv(filename, rows):
    rows = list(rows)
    path = OUT / filename

    if not rows:
        path.write_text("", encoding="utf-8-sig")
        return

    headers = []
    for r in rows:
        for k in r.keys():
            if k not in headers:
                headers.append(k)

    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

text = read_all_text()

print("Loaded chars:", len(text))
print("Signals:",
      "users", text.count("VXNlcjo"),
      "boards", text.count("Qm9hcmQ6"),
      "pin_id", text.count("pin_id"))

# =========================
# LEADS / PINNERS
# =========================

leads_raw = []
leads_unique = {}

for m in re.finditer("VXNlcjo", text):
    block = text[max(0, m.start() - 5000): m.start() + 12000]
    username = get(block, "username")

    if not username or len(username) > 80:
        continue

    row = {
        "full_name": get(block, "full_name"),
        "domain_url": get(block, "domain_url"),
        "username": username,
        "profile_url": f"https://www.pinterest.com/{username}",
        "website_url": get(block, "website_url") or get(block, "listed_website_url"),
        "contact_email": get(block, "contact_email"),
        "contact_phone": get(block, "contact_phone"),
        "about": get(block, "about"),
        "board_count": get_int(block, "board_count"),
        "follower_count": get_int(block, "follower_count"),
        "following_count": get_int(block, "following_count"),
        "pin_count": get_int(block, "pin_count"),
        "profile_reach": get_int(block, "profile_reach"),
        "profile_views": get_int(block, "profile_views"),
        "story_pin_count": get_int(block, "story_pin_count"),
        "video_pin_count": get_int(block, "video_pin_count"),
        "lastPinAt": get(block, "lastPinAt") or get(block, "last_pin_save_time"),
        "image_small_url": get(block, "image_small_url"),
        "image_medium_url": get(block, "image_medium_url"),
        "image_xlarge_url": get(block, "image_xlarge_url"),
        "all_urls": all_urls(block),
    }

    leads_raw.append(row)

    old = leads_unique.get(username)
    if not old:
        leads_unique[username] = row
    else:
        # keep richer row
        if sum(bool(v) for v in row.values()) > sum(bool(v) for v in old.values()):
            leads_unique[username] = row

# =========================
# BOARDS
# =========================

boards_raw = []
boards_unique = {}

for m in re.finditer("Qm9hcmQ6", text):
    block = text[max(0, m.start() - 5000): m.start() + 12000]

    board_id = get(block, "id")
    name = get(block, "name")
    url = get(block, "url")
    username = get(block, "username")

    if not board_id and not name:
        continue

    board_url = "https://www.pinterest.com" + url if url.startswith("/") else url

    row = {
        "id": board_id,
        "name": name,
        "description": get(block, "description"),
        "url": board_url,
        "image_cover_url": get(block, "image_cover_url"),
        "follower_count": get_int(block, "follower_count"),
        "section_count": get_int(block, "section_count"),
        "pin_count": get_int(block, "pin_count"),
        "privacy": get(block, "privacy"),
        "category": get(block, "category"),
        "modifiedAt": get(block, "modifiedAt"),
        "collaborator_count": get_int(block, "collaborator_count"),
        "sectionless_pin_count": get_int(block, "sectionless_pin_count"),
        "owner_username": username,
        "owner_url": f"https://www.pinterest.com/{username}" if username else "",
        "owner_full_name": get(block, "full_name"),
        "owner_image_xlarge_url": get(block, "image_xlarge_url"),
        "owner_image_medium_url": get(block, "image_medium_url"),
        "owner_image_small_url": get(block, "image_small_url"),
        "images": all_urls(block),
    }

    boards_raw.append(row)

    key = board_id or board_url or name
    old = boards_unique.get(key)
    if not old:
        boards_unique[key] = row
    else:
        if sum(bool(v) for v in row.values()) > sum(bool(v) for v in old.values()):
            boards_unique[key] = row

# =========================
# PINS
# =========================

pins_raw = []
pins_unique = {}

pin_patterns = [
    r'pin_id".{1,8}([0-9]{8,25})',
    r'/pin/([0-9]{8,25})',
    r'pinterest\.com/pin/([0-9]{8,25})',
]

seen_positions = set()

for pat in pin_patterns:
    for m in re.finditer(pat, text, re.S):
        pin_id = m.group(1)
        pos_key = (pin_id, m.start() // 1000)

        if pos_key in seen_positions:
            continue

        seen_positions.add(pos_key)

        block = text[max(0, m.start() - 7000): m.start() + 15000]
        username = get(block, "username")

        row = {
            "id": pin_id,
            "title": get(block, "title"),
            "description": get(block, "description"),
            "link": get(block, "link"),
            "pin_url": f"https://www.pinterest.com/pin/{pin_id}",
            "image": get(block, "image_cover_url") or first_url(block, "i.pinimg.com"),
            "images": all_urls(block),
            "video": first_url(block, "v.pinimg.com"),
            "saves": get_int(block, "saves"),
            "done": get_int(block, "done"),
            "reaction_counts": get_int(block, "reaction_counts"),
            "repin_count": get_int(block, "repin_count"),
            "comment_count": get_int(block, "comment_count"),
            "like_count": get_int(block, "like_count"),
            "share_count": get_int(block, "share_count"),
            "board_name": get(block, "name"),
            "board_url": "",
            "board_pin_count": get_int(block, "pin_count"),
            "board_follower_count": get_int(block, "follower_count"),
            "board_collaborator_count": get_int(block, "collaborator_count"),
            "board_privacy": get(block, "privacy"),
            "board_category": get(block, "category"),
            "pinner_name": get(block, "full_name"),
            "pinner_username": username,
            "pinner_url": f"https://www.pinterest.com/{username}" if username else "",
            "pinner_pin_count": get_int(block, "pin_count"),
            "pinner_board_count": get_int(block, "board_count"),
            "pinner_follower_count": get_int(block, "follower_count"),
            "pinner_following_count": get_int(block, "following_count"),
            "pinner_profile_reach": get_int(block, "profile_reach"),
            "pinner_profile_views": get_int(block, "profile_views"),
            "created_at": get(block, "createdAt"),
            "updated_at": get(block, "updatedAt"),
        }

        pins_raw.append(row)

        old = pins_unique.get(pin_id)
        if not old:
            pins_unique[pin_id] = row
        else:
            if sum(bool(v) for v in row.values()) > sum(bool(v) for v in old.values()):
                pins_unique[pin_id] = row

# =========================
# SAVE FILES
# =========================

save_csv("SortPin.com_all_pins_python_UNIQUE.csv", pins_unique.values())
save_csv("SortPin.com_all_boards_python_UNIQUE.csv", boards_unique.values())
save_csv("SortPin.com_all_leads_python_UNIQUE.csv", leads_unique.values())

save_csv("SortPin.com_all_pins_python_RAW.csv", pins_raw)
save_csv("SortPin.com_all_boards_python_RAW.csv", boards_raw)
save_csv("SortPin.com_all_leads_python_RAW.csv", leads_raw)

print("DONE")
print("Pins unique:", len(pins_unique), "| raw:", len(pins_raw))
print("Boards unique:", len(boards_unique), "| raw:", len(boards_raw))
print("Leads unique:", len(leads_unique), "| raw:", len(leads_raw))
print("Saved in:", OUT)

input("Press Enter to exit...")