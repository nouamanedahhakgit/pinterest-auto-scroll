import os
import sys
import mysql.connector

# Load configuration from .env file
BASE = os.path.dirname(os.path.abspath(__file__))
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

def main():
    env = load_env()
    host = env.get("MYSQL_HOST", "72.61.197.144")
    port = int(env.get("MYSQL_PORT", "3306"))
    db = env.get("MYSQL_DB", "data_pint")
    user = env.get("MYSQL_USER", "data_pint_user")
    password = env.get("MYSQL_PASSWORD", "")

    if not password:
        print("Error: MYSQL_PASSWORD not set in .env")
        sys.exit(1)

    print(f"Connecting to MySQL ({host}:{port}) to inspect locks...")
    try:
        con = mysql.connector.connect(
            host=host,
            port=port,
            database=db,
            user=user,
            password=password,
            charset="utf8mb4"
        )
        cursor = con.cursor(dictionary=True)
    except Exception as e:
        print(f"Failed to connect: {e}")
        sys.exit(1)

    # 1. Inspect Processlist using SHOW PROCESSLIST (works without PROCESS privilege for own connections)
    print("\n--- Process List (connections from same MySQL user) ---")
    try:
        cursor.execute("SHOW PROCESSLIST")
        processes = cursor.fetchall()
    except Exception as e:
        print(f"Failed to query process list: {e}")
        sys.exit(1)
        
    zombies = []
    
    # Get current connection thread ID so we don't kill ourselves
    cursor.execute("SELECT CONNECTION_ID() as id")
    my_conn_id = cursor.fetchone()["id"]

    for p in processes:
        info_str = p.get('Info') or ''
        info_str = info_str.replace('\n', ' ')[:100]
        # Dict keys can be lowercase or uppercase depending on connector version
        p_id = p.get('Id') or p.get('id')
        p_user = p.get('User') or p.get('user')
        p_host = p.get('Host') or p.get('host')
        p_db = p.get('db') or p.get('db')
        p_command = p.get('Command') or p.get('command')
        p_time = p.get('Time') or p.get('time')
        p_state = p.get('State') or p.get('state')
        
        print(f"ID: {p_id} | User: {p_user} | Host: {p_host} | db: {p_db} | Command: {p_command} | Time: {p_time} | State: {p_state}")
        if info_str:
            print(f"  SQL: {info_str}")
        print("-" * 50)

        # Identify zombie/long running connections from same user
        if p_user == user and p_id != my_conn_id:
            # If command is Sleep, or running query for > 60 seconds
            if p_command == 'Sleep' or (p_command == 'Query' and p_time > 60):
                zombies.append(p_id)

    # 3. Kill Zombie Threads
    if zombies:
        print(f"\nFound {len(zombies)} zombie connection thread(s) from user '{user}'.")
        auto_yes = "--yes" in sys.argv or "-y" in sys.argv
        if auto_yes:
            choice = 'y'
        else:
            choice = input("Do you want to KILL these threads to release database locks? (y/n): ").strip().lower()
        if choice == 'y':
            for zid in zombies:
                print(f"Killing thread ID {zid}...")
                try:
                    cursor.execute(f"KILL {zid}")
                    print(f"  Successfully killed thread {zid}.")
                except Exception as ex:
                    print(f"  Failed to kill thread {zid}: {ex}")
            con.commit()
        else:
            print("No threads were killed.")
    else:
        print("\nNo zombie threads detected.")

    cursor.close()
    con.close()

if __name__ == "__main__":
    main()
