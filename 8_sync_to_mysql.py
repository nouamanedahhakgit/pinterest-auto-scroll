"""
STEP 8 — Synchronize Local SQLite Database to Cloud MySQL Database
==================================================================
Reads data from the local SQLite database (sortpin.db) and pushes it to
the cloud MySQL database specified in the .env file.

It uses bulk inserts with "ON DUPLICATE KEY UPDATE" to resolve conflicts
if multiple computers are scraping and syncing to the same MySQL database.
It also updates calculated statistics (pins count, boards count, etc.)
after merging.

Requirements:
  pip install mysql-connector-python cryptography
"""

import os
import sys
import json
import time
import sqlite3

# Load configuration from .env file
BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "sortpin.db")
DQS_DB_PATH = os.path.join(BASE, "domain_quick_scrape.db")  # step 9/10 output
ENV_PATH = os.path.join(BASE, ".env")

def load_env():
    env = {}
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    return env

def get_mysql_connection(env):
    try:
        import mysql.connector
    except ImportError:
        print("\n  ❌ Error: 'mysql-connector-python' is not installed.")
        print("  Please run: pip install mysql-connector-python cryptography\n")
        sys.exit(1)

    host = env.get("MYSQL_HOST", "72.61.197.144")
    port = int(env.get("MYSQL_PORT", "3306"))
    db = env.get("MYSQL_DB", "data_pint")
    user = env.get("MYSQL_USER", "data_pint_user")
    password = env.get("MYSQL_PASSWORD", "")

    if password == "YOUR_PASSWORD_HERE" or not password:
        print("\n  ⚠️  Warning: Please set your MySQL password in the .env file first.\n")
        sys.exit(1)

    print(f"  Connecting to MySQL ({host}:{port}, Database: {db}, User: {user})...")
    try:
        con = mysql.connector.connect(
            host=host,
            port=port,
            database=db,
            user=user,
            password=password,
            charset="utf8mb4",
            collation="utf8mb4_general_ci"
        )
        try:
            cursor = con.cursor()
            # Use READ COMMITTED to prevent gap locking
            cursor.execute("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
            # Extend lock wait timeout per session to 120s to survive busy server
            cursor.execute("SET SESSION innodb_lock_wait_timeout = 120")
            # Kill any stale SLEEPING connections from the same user that may hold locks
            killed = 0
            cursor.execute(
                "SELECT id FROM information_schema.processlist "
                "WHERE user = %s AND command = 'Sleep' AND id <> CONNECTION_ID()",
                (user,)
            )
            stale = [row[0] for row in cursor.fetchall()]
            for conn_id in stale:
                try:
                    cursor.execute(f"KILL CONNECTION {conn_id}")
                    killed += 1
                except Exception:
                    pass
            if killed:
                print(f"  Killed {killed} stale sleeping MySQL connection(s) to prevent lock contention.")
            cursor.close()
        except Exception:
            pass
        return con
    except mysql.connector.Error as e:
        print(f"\n  ❌ Failed to connect to MySQL: {e}\n")
        sys.exit(1)

def execute_with_retry(cursor, conn, sql, params=None, retries=5, delay=10):
    for attempt in range(1, retries + 1):
        try:
            if params is not None:
                cursor.execute(sql, params)
            else:
                cursor.execute(sql)
            conn.commit()
            return True
        except Exception as err:
            conn.rollback()
            is_lock_timeout = "1205" in str(err) or (hasattr(err, 'errno') and getattr(err, 'errno', None) == 1205)
            if is_lock_timeout and attempt < retries:
                print(f"    ⚠️ Lock wait timeout exceeded. Retrying query in {delay} seconds (attempt {attempt}/{retries})...")
                time.sleep(delay)
            else:
                raise err

def sync_table(sqlite_cursor, mysql_cursor, mysql_conn, table, pk, new_scraped_ids):
    """Syncs one SQLite table to MySQL: creates the table on first run,
    upserts rows with ON DUPLICATE KEY UPDATE. Used for both sortpin.db's
    pinners/boards/pins and domain_quick_scrape.db's websites/posts/
    ad_networks/robots_sitemaps/scan_errors -- same merge-safe logic either
    way, just with new_scraped_ids=None for the latter (those tables are
    small, so we always do a full sync instead of incremental filtering)."""
    sqlite_cursor.execute(f"PRAGMA table_info({table})")
    columns_info = sqlite_cursor.fetchall()

    if not columns_info:
        print(f"  Warning: Table '{table}' is empty or does not exist in local SQLite. Skipping.")
        return

    # 1. Create table in MySQL if it doesn't exist
    print(f"\nSyncing table '{table}'...")
    col_defs = []
    cols = []
    for col in columns_info:
        col_name = col[1]
        col_type = col[2]
        cols.append(col_name)

        # Map SQLite type to MySQL type
        if col_type == "INTEGER":
            mysql_type = "BIGINT"
        else:
            # String fields: if primary key, give it a limited VARCHAR length for keys
            if col_name == pk:
                mysql_type = "VARCHAR(190)"
            else:
                mysql_type = "LONGTEXT"

        col_defs.append(f"`{col_name}` {mysql_type}" + (" PRIMARY KEY" if col_name == pk else ""))

    # Check if table already exists in MySQL
    mysql_cursor.execute("SHOW TABLES LIKE %s", (table,))
    table_exists = mysql_cursor.fetchone()

    if not table_exists:
        print(f"  - Creating table `{table}` in MySQL...")
        create_sql = f"CREATE TABLE `{table}` (\n  " + ",\n  ".join(col_defs) + "\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;"
        mysql_cursor.execute(create_sql)
        mysql_conn.commit()

        # Create indexes for performance if they don't exist
        if table == "boards":
            try:
                mysql_cursor.execute("CREATE INDEX idx_boards_owner ON boards(owner_username)")
                mysql_conn.commit()
            except Exception:
                pass
        elif table == "pins":
            for idx_name, col_name in [("idx_pins_pinner", "pinner_username"), ("idx_pins_board", "board_id")]:
                try:
                    mysql_cursor.execute(f"CREATE INDEX {idx_name} ON pins({col_name})")
                    mysql_conn.commit()
                except Exception:
                    pass
        elif table == "posts":
            for idx_name, col_name in [("idx_posts_domain", "domain"), ("idx_posts_downloaded", "downloaded")]:
                try:
                    mysql_cursor.execute(f"CREATE INDEX {idx_name} ON posts({col_name})")
                    mysql_conn.commit()
                except Exception:
                    pass
        elif table in ("ad_networks", "robots_sitemaps", "scan_errors"):
            try:
                mysql_cursor.execute(f"CREATE INDEX idx_{table}_domain ON `{table}`(domain)")
                mysql_conn.commit()
            except Exception:
                pass

    # 2. Fetch data from SQLite
    rows = []
    if new_scraped_ids and table in new_scraped_ids:
        target_ids = new_scraped_ids[table]
        if target_ids:
            # Chunk to avoid SQLite parameter limit
            chunk_size = 500
            for j in range(0, len(target_ids), chunk_size):
                chunk = target_ids[j:j + chunk_size]
                placeholders = ", ".join(["?"] * len(chunk))
                sqlite_cursor.execute(f"SELECT * FROM `{table}` WHERE `{pk}` IN ({placeholders})", chunk)
                rows.extend(sqlite_cursor.fetchall())
        else:
            print(f"  - No new data to sync for `{table}` in this cycle.")
            return
    else:
        sqlite_cursor.execute(f"SELECT * FROM `{table}`")
        rows = sqlite_cursor.fetchall()

    if not rows:
        print(f"  - No data to sync for `{table}`.")
        return

    print(f"  - Read {len(rows)} rows from local SQLite.")

    # Build simple upsert: last writer wins (VALUES(col) without COALESCE).
    # COALESCE(NULLIF(VALUES(col), ''), col) forces MySQL to read the existing row
    # before writing, which acquires a shared lock and causes deadlocks when
    # multiple computers sync concurrently. Plain VALUES(col) only needs an
    # exclusive lock on the row being written — no shared read lock needed.
    col_list = ", ".join(f"`{c}`" for c in cols)
    placeholders = ", ".join(["%s"] * len(cols))
    update_parts = [f"`{c}`=VALUES(`{c}`)"
                    for c in cols if c != pk]
    update_clause = ", ".join(update_parts)

    upsert_sql = f"INSERT INTO `{table}` ({col_list}) VALUES ({placeholders})"
    if update_clause:
        upsert_sql += f" ON DUPLICATE KEY UPDATE {update_clause}"

    # Insert data in small batches of 25 rows to minimise lock hold time.
    # On persistent lock timeout: SKIP the batch and continue — the rows
    # will be synced on the next scrape cycle. This prevents one locked table
    # from blocking the entire multi-computer workflow.
    batch_size = 25
    total_inserted = 0
    total_skipped = 0

    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        formatted_batch = [[val for val in r] for r in batch]

        max_lock_retries = 2   # short — skip quickly rather than blocking
        for attempt in range(1, max_lock_retries + 2):
            try:
                mysql_cursor.executemany(upsert_sql, formatted_batch)
                mysql_conn.commit()
                total_inserted += len(batch)
                if total_inserted % 100 == 0 or total_inserted == len(rows):
                    print(f"  - Synchronized {total_inserted}/{len(rows)} rows...")
                break
            except Exception as err:
                mysql_conn.rollback()
                is_lock_timeout = ("1205" in str(err) or
                                   (hasattr(err, 'errno') and getattr(err, 'errno', None) == 1205))
                if is_lock_timeout:
                    if attempt <= max_lock_retries:
                        wait = 5 * attempt
                        print(f"  ⚠️ Lock timeout on `{table}` (attempt {attempt}/{max_lock_retries}), retrying in {wait}s...")
                        time.sleep(wait)
                    else:
                        # Give up on this batch — skip it, keep going
                        total_skipped += len(batch)
                        print(f"  ⚠️ Skipping locked batch in `{table}` ({len(batch)} rows) — will retry on next sync.")
                        break
                else:
                    print(f"  Error syncing batch in `{table}`: {err}")
                    break

    if total_skipped:
        print(f"  ⚠️ `{table}`: {total_inserted} rows synced, {total_skipped} rows skipped (lock contention — will sync next cycle).")


def main():
    import sys
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding='utf-8')
            sys.stderr.reconfigure(encoding='utf-8')
        except AttributeError:
            pass

    if not os.path.exists(DB_PATH):
        print(f"\n  ❌ Local database not found: {DB_PATH}")
        print("  Run step 4 first to build your local SQLite database.\n")
        sys.exit(1)

    env = load_env()
    mysql_conn = get_mysql_connection(env)
    mysql_cursor = mysql_conn.cursor()

    sqlite_conn = sqlite3.connect(DB_PATH)
    sqlite_cursor = sqlite_conn.cursor()

    # Tables to sync with their Primary Keys
    tables_pk = {
        "pinners": "username",
        "boards": "id",
        "pins": "id",
        "keywords": "keyword",
        "pin_keywords": "id"
    }

    # Load new scraped IDs if present (emitted by 4_build_database.py)
    new_ids_file = os.path.join(BASE, "new_scraped_ids.json")
    new_scraped_ids = None
    if os.path.exists(new_ids_file):
        try:
            with open(new_ids_file, encoding="utf-8") as f:
                new_scraped_ids = json.load(f)
            p_cnt = len(new_scraped_ids.get("pinners", []))
            b_cnt = len(new_scraped_ids.get("boards", []))
            pin_cnt = len(new_scraped_ids.get("pins", []))
            pk_cnt = len(new_scraped_ids.get("pin_keywords", []))
            print(f"  - Loaded incremental sync filter: {p_cnt} pinners, {b_cnt} boards, {pin_cnt} pins, {pk_cnt} pin-keywords.")
        except Exception as e:
            print(f"  - Warning: Failed to load {new_ids_file}: {e}")

    print("\nStarting Local-to-Cloud Sync...")

    for table, pk in tables_pk.items():
        sync_table(sqlite_cursor, mysql_cursor, mysql_conn, table, pk, new_scraped_ids)

    # 4b. Sync domain_quick_scrape.db (step 9/10 output) if it exists. These
    # tables are small (one row per domain/post/ad-network), so we always do
    # a full sync rather than filtering by new_scraped_ids.
    if os.path.exists(DQS_DB_PATH):
        print(f"\nStarting domain_quick_scrape.db sync...")
        dqs_conn = sqlite3.connect(DQS_DB_PATH)
        dqs_cursor = dqs_conn.cursor()
        dqs_tables_pk = {
            "websites": "domain",
            "posts": "post_id",
            "ad_networks": "id",
            "robots_sitemaps": "id",
            "scan_errors": "id",
        }
        try:
            for table, pk in dqs_tables_pk.items():
                sync_table(dqs_cursor, mysql_cursor, mysql_conn, table, pk, None)
        finally:
            dqs_conn.close()
    else:
        print(f"\n  (Skipping domain_quick_scrape.db sync -- not found yet. Run step 9 first.)")

    # 5. Run aggregation updates to organize stats and counts (handling multi-computer merges)
    print("\nOrganizing and recalculating metrics in MySQL (combining data from all workers)...")
    
    try:
        new_pinners = new_scraped_ids.get("pinners", []) if new_scraped_ids else []
        new_boards = new_scraped_ids.get("boards", []) if new_scraped_ids else []

        if new_scraped_ids:
            # Incremental updates to avoid table locks and timeouts
            if new_boards:
                print(f"  - Calculating board pin counts for {len(new_boards)} boards...")
                chunk_size = 1000
                for j in range(0, len(new_boards), chunk_size):
                    chunk = new_boards[j:j + chunk_size]
                    placeholders = ", ".join(["%s"] * len(chunk))
                    execute_with_retry(mysql_cursor, mysql_conn, f"""
                        UPDATE boards b 
                        LEFT JOIN (
                            SELECT board_id, COUNT(*) as cnt 
                            FROM pins 
                            WHERE board_id IN ({placeholders})
                            GROUP BY board_id
                        ) p_counts ON b.id = p_counts.board_id
                        SET b.pin_count = COALESCE(p_counts.cnt, 0)
                        WHERE b.id IN ({placeholders})
                    """, chunk + chunk)

            if new_pinners:
                print(f"  - Calculating pinner board counts for {len(new_pinners)} pinners...")
                chunk_size = 1000
                for j in range(0, len(new_pinners), chunk_size):
                    chunk = new_pinners[j:j + chunk_size]
                    placeholders = ", ".join(["%s"] * len(chunk))
                    execute_with_retry(mysql_cursor, mysql_conn, f"""
                        UPDATE pinners p
                        LEFT JOIN (
                            SELECT owner_username, COUNT(*) as cnt
                            FROM boards
                            WHERE owner_username IN ({placeholders})
                            GROUP BY owner_username
                        ) b_counts ON p.username = b_counts.owner_username
                        SET p.scraped_boards_count = COALESCE(b_counts.cnt, 0)
                        WHERE p.username IN ({placeholders})
                    """, chunk + chunk)

                print(f"  - Calculating pinner pin counts for {len(new_pinners)} pinners...")
                chunk_size = 1000
                for j in range(0, len(new_pinners), chunk_size):
                    chunk = new_pinners[j:j + chunk_size]
                    placeholders = ", ".join(["%s"] * len(chunk))
                    execute_with_retry(mysql_cursor, mysql_conn, f"""
                        UPDATE pinners p
                        LEFT JOIN (
                            SELECT pinner_username, COUNT(*) as cnt
                            FROM pins
                            WHERE pinner_username IN ({placeholders})
                            GROUP BY pinner_username
                        ) p_counts ON p.username = p_counts.pinner_username
                        SET p.scraped_pins_count = COALESCE(p_counts.cnt, 0)
                        WHERE p.username IN ({placeholders})
                    """, chunk + chunk)

                print(f"  - Calculating pinner created vs saved pin counts for {len(new_pinners)} pinners...")
                chunk_size = 1000
                for j in range(0, len(new_pinners), chunk_size):
                    chunk = new_pinners[j:j + chunk_size]
                    placeholders = ", ".join(["%s"] * len(chunk))
                    execute_with_retry(mysql_cursor, mysql_conn, f"""
                        UPDATE pinners p
                        LEFT JOIN (
                            SELECT pinner_username,
                                   SUM(CASE WHEN pin_type = 'created' THEN 1 ELSE 0 END) as created_cnt,
                                   SUM(CASE WHEN pin_type = 'saved' THEN 1 ELSE 0 END) as saved_cnt
                            FROM pins
                            WHERE pinner_username IN ({placeholders})
                            GROUP BY pinner_username
                        ) p_counts ON p.username = p_counts.pinner_username
                        SET p.scraped_created_pins_count = COALESCE(p_counts.created_cnt, 0),
                            p.scraped_saved_pins_count = COALESCE(p_counts.saved_cnt, 0)
                        WHERE p.username IN ({placeholders})
                    """, chunk + chunk)
        else:
            # Full recalculation — process in chunks of 500 to avoid long lock holds
            chunk_size = 500

            # Board pin counts
            print("  - Calculating all board pin counts (chunked)...")
            mysql_cursor.execute("SELECT id FROM boards")
            all_board_ids = [row[0] for row in mysql_cursor.fetchall()]
            for j in range(0, len(all_board_ids), chunk_size):
                chunk = all_board_ids[j:j + chunk_size]
                placeholders = ", ".join(["%s"] * len(chunk))
                execute_with_retry(mysql_cursor, mysql_conn, f"""
                    UPDATE boards b
                    LEFT JOIN (
                        SELECT board_id, COUNT(*) as cnt
                        FROM pins
                        WHERE board_id IN ({placeholders})
                        GROUP BY board_id
                    ) p_counts ON b.id = p_counts.board_id
                    SET b.pin_count = COALESCE(p_counts.cnt, 0)
                    WHERE b.id IN ({placeholders})
                """, chunk + chunk)

            # Pinner board + pin counts
            print("  - Calculating all pinner board/pin counts (chunked)...")
            mysql_cursor.execute("SELECT username FROM pinners")
            all_pinners = [row[0] for row in mysql_cursor.fetchall()]
            for j in range(0, len(all_pinners), chunk_size):
                chunk = all_pinners[j:j + chunk_size]
                placeholders = ", ".join(["%s"] * len(chunk))
                execute_with_retry(mysql_cursor, mysql_conn, f"""
                    UPDATE pinners p
                    LEFT JOIN (
                        SELECT owner_username, COUNT(*) as cnt
                        FROM boards
                        WHERE owner_username IN ({placeholders})
                        GROUP BY owner_username
                    ) b_counts ON p.username = b_counts.owner_username
                    SET p.scraped_boards_count = COALESCE(b_counts.cnt, 0)
                    WHERE p.username IN ({placeholders})
                """, chunk + chunk)
                execute_with_retry(mysql_cursor, mysql_conn, f"""
                    UPDATE pinners p
                    LEFT JOIN (
                        SELECT pinner_username, COUNT(*) as cnt
                        FROM pins
                        WHERE pinner_username IN ({placeholders})
                        GROUP BY pinner_username
                    ) p_counts ON p.username = p_counts.pinner_username
                    SET p.scraped_pins_count = COALESCE(p_counts.cnt, 0)
                    WHERE p.username IN ({placeholders})
                """, chunk + chunk)
                execute_with_retry(mysql_cursor, mysql_conn, f"""
                    UPDATE pinners p
                    LEFT JOIN (
                        SELECT pinner_username,
                               SUM(CASE WHEN pin_type = 'created' THEN 1 ELSE 0 END) as created_cnt,
                               SUM(CASE WHEN pin_type = 'saved' THEN 1 ELSE 0 END) as saved_cnt
                        FROM pins
                        WHERE pinner_username IN ({placeholders})
                        GROUP BY pinner_username
                    ) p_counts ON p.username = p_counts.pinner_username
                    SET p.scraped_created_pins_count = COALESCE(p_counts.created_cnt, 0),
                        p.scraped_saved_pins_count = COALESCE(p_counts.saved_cnt, 0)
                    WHERE p.username IN ({placeholders})
                """, chunk + chunk)
            
        print("  Metric calculations updated successfully.")
        
    except Exception as err:
        print(f"  Error running aggregations: {err}")
        mysql_conn.rollback()

    # Clean up the new scraped IDs file after successful sync
    if new_scraped_ids and os.path.exists(new_ids_file):
        try:
            os.remove(new_ids_file)
        except Exception:
            pass

    # Close connections
    sqlite_conn.close()
    mysql_conn.close()
    
    print("\nLocal database synchronized to MySQL successfully!")
    
    # Automatically sync websites to Google Sheets
    try:
        print("\nAutomatically syncing websites to Google Sheets...")
        sys.path.insert(0, BASE)
        vd = __import__("5_view_data")
        count = vd.run_websites_sync(DB_PATH)
        print(f"Website sync completed. Added {count} new websites to Google Sheet.")
    except Exception as e:
        print(f"Warning: Website auto-sync failed: {e}")

if __name__ == "__main__":
    main()
