r"""
STEP 6 — Clear the SortPin extension's stored data (by deleting its files)
=========================================================================
The SortPin extension keeps every pin/board/pinner in Brave's IndexedDB, so it
grows huge. This wipes that data the simple way: it closes Brave and DELETES the
extension's IndexedDB folder(s) on disk. No CDP, no Selenium, no popup.

IMPORTANT — run step 4 FIRST so your data is saved to CSV/DB:
    python 4_build_database.py     # pulls live + saves a snapshot
    python 6_clear_sortpin.py      # then clear the extension

What it deletes (SortPin extension only), under every Brave profile:
    User Data\<Profile>\IndexedDB\chrome-extension_<id>_0.indexeddb.leveldb   (+ .blob)
With --all it ALSO removes the extension's chrome.storage data:
    User Data\<Profile>\Local Extension Settings\<id>

Your saved CSV snapshots and sortpin.db / sortpin_data.json are NOT touched.
Brave must be closed (the files are locked while it runs) — this script closes
it for you. Brave re-creates the (empty) folders next time it starts.

Run:
  python 6_clear_sortpin.py            # delete IndexedDB data (asks to confirm)
  python 6_clear_sortpin.py --yes      # no confirmation prompt
  python 6_clear_sortpin.py --all      # also clear chrome.storage (settings/login)
"""

import os, sys, glob, shutil, subprocess, time

EXT_ID = "djcledakkebdgjncnemijiabiaimbaic"   # SortPin extension id
USER_DATA = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                         "BraveSoftware", "Brave-Browser", "User Data")
BASE = os.path.dirname(os.path.abspath(__file__))
# deleted extension data is backed up here before removal (never auto-deleted)
ARCHIVE_DIR = os.path.join(BASE, "_SORTPIN_ARCHIVE")

def _dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for fn in files:
            try: total += os.path.getsize(os.path.join(root, fn))
            except OSError: pass
    return total

def _human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024: return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"

def find_targets(include_settings):
    """Every SortPin data folder under every Brave profile."""
    targets = []
    if not os.path.isdir(USER_DATA):
        return targets
    for prof in sorted(glob.glob(os.path.join(USER_DATA, "*"))):
        if not os.path.isdir(prof):
            continue
        idb = os.path.join(prof, "IndexedDB")
        for suffix in ("leveldb", "blob"):
            p = os.path.join(idb, f"chrome-extension_{EXT_ID}_0.indexeddb.{suffix}")
            if os.path.exists(p):
                targets.append(p)
        if include_settings:
            les = os.path.join(prof, "Local Extension Settings", EXT_ID)
            if os.path.isdir(les):
                targets.append(les)
    return targets

def brave_running():
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq brave.exe"],
                             capture_output=True, text=True)
        return "brave.exe" in (out.stdout or "")
    except Exception:
        return False

def close_brave():
    subprocess.run(["taskkill", "/F", "/IM", "brave.exe"], capture_output=True)
    time.sleep(2)

def main():
    print(f"\n{'='*60}\n  STEP 6 — Clear SortPin data (delete files on disk)\n{'='*60}")
    skip_confirm   = "--yes" in sys.argv[1:]
    include_setts  = "--all" in sys.argv[1:]

    if not os.path.isdir(USER_DATA):
        print(f"  Could not find Brave profile at:\n    {USER_DATA}\n"
              f"  Set the right path at the top of this script.\n")
        sys.exit(1)

    targets = find_targets(include_setts)
    if not targets:
        print("  Nothing to delete — SortPin has no stored folders here.\n"
              "  (Already cleared, or Brave uses a different profile path.)\n")
        return

    total = sum(_dir_size(t) for t in targets)
    print("  Will delete these SortPin folders:")
    for t in targets:
        print(f"     [{_human(_dir_size(t)):>7}]  {t}")
    print(f"  Total: {_human(total)}")
    if not include_setts:
        print("  (extension settings/login kept — add --all to clear those too)")

    if not skip_confirm:
        print(f"\n  ⚠  This permanently deletes the folders above. Run step 4 first to save data.")
        ans = input(f"     Type 'yes' to clear, anything else to cancel: ").strip().lower()
        if ans not in ("y", "yes"):
            print("  Cancelled — nothing was deleted.\n")
            return

    if brave_running():
        print("\n  Closing Brave (its files are locked while it runs)...")
        close_brave()

    # 1) ARCHIVE (backup) every folder into the project before deleting it
    import time as _t
    stamp = _t.strftime("%Y-%m-%d_%H-%M-%S")
    archive_root = os.path.join(ARCHIVE_DIR, stamp)
    print(f"\n  Archiving a backup into _SORTPIN_ARCHIVE/{stamp}/ ...")
    archived = 0
    for t in targets:
        try:
            # keep the profile name + folder name so backups are identifiable
            prof = os.path.basename(os.path.dirname(os.path.dirname(t)))
            dest = os.path.join(archive_root, prof, os.path.basename(t))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copytree(t, dest)
            archived += 1
        except Exception as e:
            print(f"     ⚠ could not archive {t}: {e}")
    if archived:
        print(f"     ✅ backed up {archived} folder(s) → _SORTPIN_ARCHIVE/{stamp}/")

    # 2) DELETE from the extension
    deleted, failed = [], []
    for t in targets:
        try:
            shutil.rmtree(t)
            deleted.append(t)
        except Exception as e:
            failed.append((t, str(e)))

    print(f"\n  ✅ Deleted {len(deleted)} folder(s) from the extension, freed ~{_human(total)}.")
    for t, e in failed:
        print(f"     ⚠ could not delete {t}\n        {e}")
    if failed:
        print("     (Make sure Brave is fully closed, then re-run.)")
    print(f"\n  Done. SortPin will start empty next time you open Brave.")
    print(f"  A backup of the deleted data is in:  _SORTPIN_ARCHIVE/{stamp}/")
    print(f"  Your CSV snapshots and sortpin.db are untouched.\n")

if __name__ == "__main__":
    main()
