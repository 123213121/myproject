import sqlite3

DB_NAME = "vpn_bot.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            vpn_key TEXT,
            expire_date TEXT
        )
    """)
    conn.commit()
    conn.close()

def get_user(user_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, username, vpn_key, expire_date FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row

def save_user_key(user_id: int, username: str, vpn_key: str):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO users (user_id, username, vpn_key)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET vpn_key=excluded.vpn_key
    """, (user_id, username, vpn_key))
    conn.commit()
    conn.close()