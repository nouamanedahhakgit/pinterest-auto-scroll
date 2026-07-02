import os
import sys
import collections.abc
import typing

# Configure Python 3.9 compatibility for typing hooks
if sys.version_info < (3, 10):
    collections.abc.Callable = typing.Callable
    collections.abc.Iterable = typing.Iterable
    collections.abc.Sequence = typing.Sequence

try:
    from ccl_chromium_reader import ccl_chromium_indexeddb as _idb
except ImportError:
    print("\n❌ Error: 'ccl_chromium_reader' is not installed.")
    print("Please run: pip install ccl_chromium_reader\n")
    sys.exit(1)

BASE = os.path.dirname(os.path.abspath(__file__))
ARCHIVE = os.path.join(BASE, "_SORTPIN_ARCHIVE")

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

def main():
    if not os.path.isdir(ARCHIVE):
        print(f"❌ Archive directory not found: {ARCHIVE}")
        return
        
    print(f"Scanning archive backups in: {ARCHIVE}...")
    
    archive_dirs = []
    for root, dirs, _files in os.walk(ARCHIVE):
        for d in dirs:
            if d == "chrome-extension_djcledakkebdgjncnemijiabiaimbaic_0.indexeddb.leveldb":
                archive_dirs.append(os.path.join(root, d))
                
    print(f"Found {len(archive_dirs)} LevelDB folders in backups.")
    
    total_leads = 0
    leads_with_websites = 0
    sample_websites = []
    
    for d in archive_dirs:
        try:
            wrapper = _idb.WrappedIndexDB(d)
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
                            kind = _classify_keys(v.keys())
                            if kind == "leads":
                                total_leads += 1
                                web_url = v.get("website_url") or v.get("listed_website_url")
                                if web_url:
                                    leads_with_websites += 1
                                    if len(sample_websites) < 15:
                                        username = v.get("username", "unknown")
                                        web_clean = str(web_url).encode("ascii", errors="ignore").decode("ascii")
                                        sample_websites.append((username, web_clean))
        except Exception as e:
            pass
            
    print(f"\n📊 Archive Scan Results:")
    print(f"  • Total pinner records found in archive: {total_leads}")
    print(f"  • Pinner records with websites: {leads_with_websites}")
    
    if sample_websites:
        print("\n🔎 Sample pinners with websites found in archive:")
        for username, web in sample_websites:
            print(f"    @{username} -> {web}")
    else:
        print("\n❌ No pinner records with website URLs found in your archive backups.")

if __name__ == "__main__":
    main()
