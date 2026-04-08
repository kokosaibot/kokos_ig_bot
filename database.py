import sqlite3

conn = sqlite3.connect("users.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    platform TEXT,
    media_type TEXT
)
""")

conn.commit()


def add_user(user_id, username=None, first_name=None):
    cursor.execute("""
    INSERT OR IGNORE INTO users (user_id, username, first_name)
    VALUES (?, ?, ?)
    """, (user_id, username, first_name))
    conn.commit()


def get_users_count():
    cursor.execute("SELECT COUNT(*) FROM users")
    return cursor.fetchone()[0]


def get_all_user_ids():
    cursor.execute("SELECT user_id FROM users")
    return [row[0] for row in cursor.fetchall()]


def add_stat(user_id, platform, media_type):
    cursor.execute("""
    INSERT INTO stats (user_id, platform, media_type)
    VALUES (?, ?, ?)
    """, (user_id, platform, media_type))
    conn.commit()


def get_stats_summary():
    cursor.execute("""
    SELECT platform, media_type, COUNT(*)
    FROM stats
    GROUP BY platform, media_type
    """)
    return cursor.fetchall()
