import os, re, csv
from pathlib import Path

EXT_ID = "djcledakkebdgjncnemijiabiaimbaic"

SCRIPT_DIR = Path(__file__).resolve().parent
OUT = SCRIPT_DIR / "sortpin_output"
OUT.mkdir(exist_ok=True)

def find_sortpin_db():
    local = Path(os.environ.get("LOCALAPPDATA", ""))

    candidates = [
        local / "BraveSoftware/Brave-Browser/User Data",
        local / "Google/Chrome/User Data",
        local / "Microsoft/Edge/User Data",
    ]

    for base in candidates:
        if not base.exists():
            continue
        for db in base.glob(f"**/IndexedDB/chrome-extension_{EXT_ID}_0.indexeddb.leveldb"):
            return db

    return None

DB_PATH = find_sortpin_db()

if not DB_PATH:
    print("SortPin database not found.")
    print("Put this script inside the SortPin .leveldb folder OR edit DB_PATH manually.")
    input("Press Enter to exit...")
    raise SystemExit

print("DB found:", DB_PATH)
print("Output:", OUT)

def read_all_text():
    txt = ""
    for fn in DB_PATH.iterdir():
        if fn.suffix in [".ldb", ".log"]:
            txt += fn.read_bytes().decode("utf-8", errors="ignore").replace("\x00", "") + "\n"
    return txt

def clean(v):
    return re.sub(r"[\x00-\x1f\x7f]", "", v or "").strip()

def get(block, key):
    m = re.search(re.escape(key) + r'".{1}([^"{\x00-\x1f]{0,500})', block, re.S)
    return clean(m.group(1)) if m else ""

def get_int(block, key):
    m = re.search(re.escape(key) + r"I([0-9]+)", block)
    return m.group(1) if m else ""

def urls(block):
    return "\n".join(sorted(set(re.findall(r"https://[^\s\"{}<>]+", block))))

text = read_all_text()

print("Loaded chars:", len(text))
print("Signals:",
      "users", text.count("VXNlcjo"),
      "boards", text.count("Qm9hcmQ6"),
      "pin_id", text.count("pin_id"))

leads = {}
for m in re.finditer("VXNlcjo", text):
    block = text[max(0, m.start()-4000): m.start()+9000]
    username = get(block, "username")
    if username and len(username) <= 80:
        leads[username] = {
            "full_name": get(block, "full_name"),
            "domain_url": get(block, "domain_url"),
            "username": username,
            "website_url": get(block, "website_url") or get(block, "listed_website_url"),
            "contact_email": get(block, "contact_email"),
            "contact_phone": get(block, "contact_phone"),
            "board_count": get_int(block, "board_count"),
            "follower_count": get_int(block, "follower_count"),
            "following_count": get_int(block, "following_count"),
            "pin_count": get_int(block, "pin_count"),
            "profile_reach": get_int(block, "profile_reach"),
            "profile_views": get_int(block, "profile_views"),
            "lastPinAt": get(block, "lastPinAt") or get(block, "last_pin_save_time"),
        }

boards = {}
for m in re.finditer("Qm9hcmQ6", text):
    block = text[max(0, m.start()-4000): m.start()+9000]
    board_id = get(block, "id")
    name = get(block, "name")
    url = get(block, "url")
    username = get(block, "username")

    if board_id or name:
        key = board_id or name + url
        boards[key] = {
            "id": board_id,
            "name": name,
            "description": get(block, "description"),
            "url": "https://www.pinterest.com" + url if url.startswith("/") else url,
            "image_cover_url": get(block, "image_cover_url"),
            "follower_count": get_int(block, "follower_count"),
            "section_count": get_int(block, "section_count"),
            "pin_count": get_int(block, "pin_count"),
            "images": urls(block),
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
        }

pins = {}
for m in re.finditer(r'pin_id".{1,5}([0-9]{8,25})', text, re.S):
    pin_id = m.group(1)
    block = text[max(0, m.start()-5000): m.start()+10000]
    username = get(block, "username")

    pins[pin_id] = {
        "id": pin_id,
        "title": get(block, "title"),
        "description": get(block, "description"),
        "link": get(block, "link"),
        "pin_url": f"https://www.pinterest.com/pin/{pin_id}",
        "image": get(block, "image_cover_url"),
        "images": urls(block),
        "saves": get_int(block, "saves"),
        "repin_count": get_int(block, "repin_count"),
        "comment_count": get_int(block, "comment_count"),
        "like_count": get_int(block, "like_count"),
        "share_count": get_int(block, "share_count"),
        "board_name": get(block, "name"),
        "board_pin_count": get_int(block, "pin_count"),
        "board_follower_count": get_int(block, "follower_count"),
        "pinner_name": get(block, "full_name"),
        "pinner_username": username,
        "pinner_url": f"https://www.pinterest.com/{username}" if username else "",
        "created_at": get(block, "createdAt"),
        "updated_at": get(block, "updatedAt"),
    }

def save_csv(filename, rows):
    rows = list(rows)
    path = OUT / filename
    if not rows:
        path.write_text("", encoding="utf-8-sig")
        return
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

save_csv("SortPin.com_all_pins_python.csv", pins.values())
save_csv("SortPin.com_all_boards_python.csv", boards.values())
save_csv("SortPin.com_all_leads_python.csv", leads.values())

print("DONE")
print("Pins:", len(pins))
print("Boards:", len(boards))
print("Leads:", len(leads))
print("Saved beside script in:", OUT)
input("Press Enter to exit...")