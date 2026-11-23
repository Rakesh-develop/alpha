"""SIGMA database layer: SQLite, seeded from data/PINGME_DB.xlsx."""
import hashlib, hmac, os, sqlite3, math
import openpyxl

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "data", "sigma.db")
XLSX_PATH = os.path.join(BASE, "data", "PINGME_DB.xlsx")

SCHEMA = """
CREATE TABLE IF NOT EXISTS employees (
    id INTEGER PRIMARY KEY,            -- S.NO
    name TEXT NOT NULL,
    mobile TEXT NOT NULL UNIQUE,
    designation TEXT,
    handset TEXT,
    network TEXT,
    ip_v4 TEXT,
    ip_v6 TEXT,
    bluetooth_ip TEXT,
    imei TEXT
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,             -- "USER ID" column (not unique: two workers share "worker")
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin','worker')),
    mobile TEXT NOT NULL REFERENCES employees(mobile)
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_pk INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS attendance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL REFERENCES employees(id),
    kind TEXT NOT NULL CHECK (kind IN ('IN','OUT')),
    punched_at TEXT NOT NULL,          -- ISO local time (IST)
    lat REAL, lng REAL, accuracy REAL,
    distance_m REAL,
    photo TEXT NOT NULL                -- filename in data/photos/
);
CREATE INDEX IF NOT EXISTS idx_att_emp ON attendance(employee_id, punched_at);
"""


def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


def hash_pw(password, salt=None):
    salt = salt or os.urandom(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return salt.hex() + "$" + h.hex()


def check_pw(password, stored):
    salt_hex, h_hex = stored.split("$")
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 200_000)
    return hmac.compare_digest(h.hex(), h_hex)


def haversine_m(lat1, lng1, lat2, lng2):
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _s(v):
    return None if v is None else str(v).strip()


def init_db(force=False):
    os.makedirs(os.path.join(BASE, "data", "photos"), exist_ok=True)
    if force and os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    first = not os.path.exists(DB_PATH)
    c = conn()
    c.executescript(SCHEMA)
    if first:
        seed(c)
    c.commit()
    c.close()


def seed(c):
    ws = openpyxl.load_workbook(XLSX_PATH, data_only=True).active
    rows = list(ws.iter_rows(values_only=True))
    # Employee table: header on row 2, data until first blank row
    for r in rows[2:]:
        if r[0] is None:
            break
        sno, name, mobile, desig, handset, net, ip4, ip6, bt, imei = r[:10]
        c.execute(
            "INSERT INTO employees VALUES (?,?,?,?,?,?,?,?,?,?)",
            (int(sno), _s(name), _s(mobile), _s(desig), _s(handset), _s(net),
             _s(ip4), _s(ip6), _s(bt), _s(imei)),
        )
    # Login table: starts after the "LOG IN'S" marker + header row
    start = next(i for i, r in enumerate(rows) if r[1] and "LOG IN" in str(r[1]).upper())
    for r in rows[start + 2:]:
        if not r[1]:
            continue
        uid, pw, mobile = _s(r[1]), _s(r[2]), _s(r[3])
        role = "admin" if uid.upper() == "ADMIN" else "worker"
        c.execute(
            "INSERT INTO users(user_id,password_hash,role,mobile) VALUES (?,?,?,?)",
            (uid, hash_pw(pw), role, mobile),
        )
    # Geofence: unset until admin stands at the centre and taps "Use my current location"
    c.execute("INSERT INTO settings VALUES ('geo_lat', NULL)")
    c.execute("INSERT INTO settings VALUES ('geo_lng', NULL)")
    c.execute("INSERT INTO settings VALUES ('geo_radius_m', '100')")


if __name__ == "__main__":
    init_db(force=True)
    c = conn()
    print("employees:", c.execute("select count(*) from employees").fetchone()[0])
    print("users:", c.execute("select user_id, role, mobile from users").fetchall() and
          [tuple(r) for r in c.execute("select user_id, role, mobile from users")])
