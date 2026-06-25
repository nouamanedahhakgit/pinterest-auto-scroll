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
import sqlite3

# Load configuration from .env file
BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "sortpin.db")
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
        return con
    except mysql.connector.Error as e:
        print(f"\n  ❌ Failed to connect to MySQL: {e}\n")
        sys.exit(1)

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
        "pins": "id"
    }

    print("\nStarting Local-to-Cloud Sync...")

    for table, pk in tables_pk.items():
        # Get column definitions from SQLite
        sqlite_cursor.execute(f"PRAGMA table_info({table})")
        columns_info = sqlite_cursor.fetchall()
        
        if not columns_info:
            print(f"  Warning: Table '{table}' is empty or does not exist in local SQLite. Skipping.")
            continue

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

        # 2. Fetch data from SQLite
        sqlite_cursor.execute(f"SELECT * FROM `{table}`")
        rows = sqlite_cursor.fetchall()
        if not rows:
            print(f"  - No data in local SQLite for `{table}`.")
            continue

        print(f"  - Read {len(rows)} rows from local SQLite.")

        # 3. Build UPSERT query for MySQL
        # INSERT INTO table (col1, col2) VALUES (%s, %s) ON DUPLICATE KEY UPDATE col1=COALESCE(NULLIF(VALUES(col1), ''), col1)
        col_list = ", ".join(f"`{c}`" for c in cols)
        placeholders = ", ".join(["%s"] * len(cols))
        
        update_parts = []
        for c in cols:
            if c == pk:
                continue
            is_numeric = False
            for col_info in columns_info:
                if col_info[1] == c and col_info[2] == "INTEGER":
                    is_numeric = True
                    break
            if is_numeric:
                update_parts.append(f"`{c}`=COALESCE(NULLIF(VALUES(`{c}`), 0), `{c}`)")
            else:
                update_parts.append(f"`{c}`=COALESCE(NULLIF(VALUES(`{c}`), ''), `{c}`)")
                
        update_clause = ", ".join(update_parts)
        
        upsert_sql = f"INSERT INTO `{table}` ({col_list}) VALUES ({placeholders})"
        if update_clause:
            upsert_sql += f" ON DUPLICATE KEY UPDATE {update_clause}"

        # 4. Insert data in batches of 500 rows to optimize round-trips
        batch_size = 500
        total_inserted = 0
        
        for i in range(0, len(rows), batch_size):
            batch = rows[i:i + batch_size]
            
            # Map SQLite values (None/NULL handling)
            formatted_batch = []
            for r in batch:
                row_vals = []
                for val in r:
                    # SQLite None maps to MySQL NULL
                    row_vals.append(val)
                formatted_batch.append(row_vals)

            try:
                mysql_cursor.executemany(upsert_sql, formatted_batch)
                mysql_conn.commit()
                total_inserted += len(batch)
                print(f"  - Synchronized {total_inserted}/{len(rows)} rows...")
            except mysql.connector.Error as err:
                print(f"  Error syncing batch in `{table}`: {err}")
                mysql_conn.rollback()

    # 5. Run aggregation updates to organize stats and counts (handling multi-computer merges)
    print("\nOrganizing and recalculating metrics in MySQL (combining data from all workers)...")
    
    try:
        # Recalculate board pins count
        print("  - Calculating board pin counts...")
        mysql_cursor.execute("""
            UPDATE boards b 
            SET b.pin_count = (
                SELECT COUNT(*) FROM pins p WHERE p.board_id = b.id
            )
        """)
        mysql_conn.commit()

        # Recalculate pinner total scraped boards
        print("  - Calculating pinner board counts...")
        mysql_cursor.execute("""
            UPDATE pinners p 
            SET p.scraped_boards_count = (
                SELECT COUNT(*) FROM boards b WHERE b.owner_username = p.username
            )
        """)
        mysql_conn.commit()

        # Recalculate pinner total scraped pins
        print("  - Calculating pinner pin counts...")
        mysql_cursor.execute("""
            UPDATE pinners p 
            SET p.scraped_pins_count = (
                SELECT COUNT(*) FROM pins p2 WHERE p2.pinner_username = p.username
            )
        """)
        mysql_conn.commit()

        # Recalculate pinner created vs saved counts
        print("  - Calculating pinner created vs saved pin counts...")
        mysql_cursor.execute("""
            UPDATE pinners p 
            SET p.scraped_created_pins_count = (
                SELECT COUNT(*) FROM pins p2 
                WHERE p2.pinner_username = p.username AND p2.pin_type = 'created'
            )
        """)
        mysql_cursor.execute("""
            UPDATE pinners p 
            SET p.scraped_saved_pins_count = (
                SELECT COUNT(*) FROM pins p2 
                WHERE p2.pinner_username = p.username AND p2.pin_type = 'saved'
            )
        """)
        mysql_conn.commit()
        
        print("  Metric calculations updated successfully.")
        
    except mysql.connector.Error as err:
        print(f"  Error running aggregations: {err}")
        mysql_conn.rollback()

    # Close connections
    sqlite_conn.close()
    mysql_conn.close()
    
    print("\nLocal database synchronized to MySQL successfully!")

if __name__ == "__main__":
    main()
