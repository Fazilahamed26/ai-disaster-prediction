import sqlite3

DB = "users.db"

conn = sqlite3.connect(DB)
c = conn.cursor()

# Add user_id column if it doesn't exist
try:
    c.execute("ALTER TABLE reports ADD COLUMN user_id INTEGER")
    print("✅ Added user_id column to reports (if it didn't exist).")
except Exception as e:
    print("⚠️ Could not add user_id (it might already exist):", e)

# If there are reports with NULL user_id, set to admin (if admin exists)
c.execute("SELECT id FROM users WHERE email='admin@example.com'")
admin = c.fetchone()
if admin:
    admin_id = admin[0]
    c.execute("UPDATE reports SET user_id=? WHERE user_id IS NULL", (admin_id,))
    print("✅ Assigned existing reports without user to admin (id=%s)." % admin_id)
else:
    print("⚠️ Admin user not found. Existing reports will keep user_id NULL.")

conn.commit()
conn.close()
