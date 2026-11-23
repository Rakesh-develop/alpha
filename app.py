"""SIGMA attendance server.  Run:  python app.py   (then open http://localhost:5000)"""
import base64, os, re, secrets, time
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request, send_from_directory, redirect, make_response, abort

import db

IST = ZoneInfo("Asia/Kolkata")
PHOTO_DIR = os.path.join(db.BASE, "data", "photos")
MAX_ACCURACY_M = 150          # reject GPS fixes worse than this
MAX_PHOTO_BYTES = 3_000_000

app = Flask(__name__, static_folder="static", template_folder="templates")
db.init_db()

_fails = {}  # ip -> [count, first_ts]  (basic brute-force throttle)


def now_ist():
    return datetime.now(IST).replace(microsecond=0)


def current_user():
    token = request.cookies.get("sigma_session")
    if not token:
        return None
    c = db.conn()
    row = c.execute(
        """SELECT u.id AS user_pk, u.user_id, u.role, e.id AS emp_id, e.name, e.designation, e.mobile
           FROM sessions s JOIN users u ON u.id = s.user_pk
           JOIN employees e ON e.mobile = u.mobile WHERE s.token = ?""",
        (token,),
    ).fetchone()
    c.close()
    return dict(row) if row else None


def need_login(admin=False):
    u = current_user()
    if not u:
        abort(401)
    if admin and u["role"] != "admin":
        abort(403)
    return u


@app.errorhandler(401)
def _401(e):
    return jsonify(error="Please log in."), 401


@app.errorhandler(403)
def _403(e):
    return jsonify(error="Admin only."), 403


def get_geofence(c):
    s = {r["key"]: r["value"] for r in c.execute("SELECT * FROM settings")}
    if s.get("geo_lat") in (None, "") or s.get("geo_lng") in (None, ""):
        return None
    return {"lat": float(s["geo_lat"]), "lng": float(s["geo_lng"]), "radius_m": float(s["geo_radius_m"])}


# ---------- pages ----------
@app.route("/")
def login_page():
    if current_user():
        return redirect("/home")
    return send_from_directory("templates", "login.html")


@app.route("/home")
def home():
    u = current_user()
    if not u:
        return redirect("/")
    return send_from_directory("templates", "admin.html" if u["role"] == "admin" else "punch.html")


@app.route("/punch")
def punch_page():
    if not current_user():
        return redirect("/")
    return send_from_directory("templates", "punch.html")


# ---------- auth ----------
@app.post("/api/login")
def login():
    ip = request.remote_addr
    cnt, t0 = _fails.get(ip, [0, time.time()])
    if time.time() - t0 > 600:
        cnt, t0 = 0, time.time()
    if cnt >= 8:
        return jsonify(error="Too many attempts. Try again in a few minutes."), 429
    d = request.get_json(silent=True) or {}
    user_id, password = (d.get("user_id") or "").strip(), d.get("password") or ""
    c = db.conn()
    # USER ID is not unique in the source DB (two "worker" rows) - the password picks the row.
    rows = c.execute("SELECT * FROM users WHERE lower(user_id)=lower(?)", (user_id,)).fetchall()
    match = next((r for r in rows if db.check_pw(password, r["password_hash"])), None)
    if not match:
        _fails[ip] = [cnt + 1, t0]
        c.close()
        return jsonify(error="Wrong user ID or password."), 401
    _fails.pop(ip, None)
    token = secrets.token_urlsafe(32)
    c.execute("INSERT INTO sessions(token,user_pk) VALUES (?,?)", (token, match["id"]))
    c.commit()
    c.close()
    resp = make_response(jsonify(ok=True, role=match["role"]))
    resp.set_cookie("sigma_session", token, httponly=True, samesite="Lax", max_age=12 * 3600)
    return resp


@app.post("/api/logout")
def logout():
    token = request.cookies.get("sigma_session")
    if token:
        c = db.conn()
        c.execute("DELETE FROM sessions WHERE token=?", (token,))
        c.commit()
        c.close()
    resp = make_response(jsonify(ok=True))
    resp.delete_cookie("sigma_session")
    return resp


@app.get("/api/me")
def me():
    u = need_login()
    return jsonify(name=u["name"], role=u["role"], designation=u["designation"])


# ---------- employee: status + punch ----------
def today_rows(c, emp_id):
    day = now_ist().strftime("%Y-%m-%d")
    return c.execute(
        "SELECT id, kind, punched_at, distance_m FROM attendance WHERE employee_id=? AND punched_at LIKE ? ORDER BY punched_at",
        (emp_id, day + "%"),
    ).fetchall()


@app.get("/api/status")
def status():
    u = need_login()
    c = db.conn()
    rows = today_rows(c, u["emp_id"])
    geo = get_geofence(c)
    c.close()
    last = rows[-1]["kind"] if rows else None
    return jsonify(
        name=u["name"], role=u["role"], designation=u["designation"],
        geofence=geo, next_action="OUT" if last == "IN" else "IN",
        today=[dict(r) for r in rows], server_time=now_ist().isoformat(),
    )


@app.post("/api/punch")
def punch():
    u = need_login()
    d = request.get_json(silent=True) or {}
    try:
        lat, lng = float(d["lat"]), float(d["lng"])
        acc = float(d.get("accuracy") or 0)
    except (KeyError, TypeError, ValueError):
        return jsonify(error="Location missing. Allow location access and try again."), 400
    photo = d.get("photo") or ""
    m = re.match(r"^data:image/jpeg;base64,([A-Za-z0-9+/=]+)$", photo)
    if not m:
        return jsonify(error="Live photo missing. Capture a photo with the camera."), 400
    raw = base64.b64decode(m.group(1))
    if len(raw) > MAX_PHOTO_BYTES or raw[:2] != b"\xff\xd8":
        return jsonify(error="Photo invalid or too large."), 400

    c = db.conn()
    geo = get_geofence(c)
    if not geo:
        c.close()
        return jsonify(error="Office location is not set yet. Ask the admin to set the geofence centre."), 409
    if acc > MAX_ACCURACY_M:
        c.close()
        return jsonify(error=f"GPS accuracy too low (±{acc:.0f} m). Move to open sky / turn on precise location."), 400
    dist = db.haversine_m(lat, lng, geo["lat"], geo["lng"])
    if dist > geo["radius_m"]:
        c.close()
        return jsonify(error=f"You are {dist:.0f} m from the office. Allowed radius is {geo['radius_m']:.0f} m.", distance_m=round(dist)), 403

    rows = today_rows(c, u["emp_id"])
    kind = "OUT" if rows and rows[-1]["kind"] == "IN" else "IN"
    if d.get("kind") and d["kind"] != kind:
        c.close()
        return jsonify(error=f"You must punch {kind} now (last punch today was {rows[-1]['kind'] if rows else 'none'})."), 409

    ts = now_ist()
    fname = f"{u['emp_id']}_{ts.strftime('%Y%m%d_%H%M%S')}_{kind}_{secrets.token_hex(3)}.jpg"
    with open(os.path.join(PHOTO_DIR, fname), "wb") as f:
        f.write(raw)
    c.execute(
        "INSERT INTO attendance(employee_id,kind,punched_at,lat,lng,accuracy,distance_m,photo) VALUES (?,?,?,?,?,?,?,?)",
        (u["emp_id"], kind, ts.strftime("%Y-%m-%d %H:%M:%S"), lat, lng, acc, round(dist, 1), fname),
    )
    c.commit()
    c.close()
    return jsonify(ok=True, kind=kind, time=ts.strftime("%I:%M:%S %p"), distance_m=round(dist))


# ---------- admin ----------
@app.get("/api/admin/employees")
def admin_employees():
    need_login(admin=True)
    c = db.conn()
    rows = [dict(r) for r in c.execute("SELECT * FROM employees ORDER BY id")]
    c.close()
    return jsonify(rows)


@app.get("/api/admin/attendance")
def admin_attendance():
    need_login(admin=True)
    day = request.args.get("date") or now_ist().strftime("%Y-%m-%d")
    c = db.conn()
    rows = c.execute(
        """SELECT a.id, e.name, e.designation, a.kind, a.punched_at, a.lat, a.lng, a.accuracy, a.distance_m, a.photo
           FROM attendance a JOIN employees e ON e.id=a.employee_id
           WHERE a.punched_at LIKE ? ORDER BY a.punched_at DESC""",
        (day + "%",),
    ).fetchall()
    c.close()
    return jsonify(date=day, rows=[dict(r) for r in rows])


@app.get("/api/admin/geofence")
def admin_geofence_get():
    need_login(admin=True)
    c = db.conn()
    g = get_geofence(c)
    c.close()
    return jsonify(g)


@app.post("/api/admin/geofence")
def admin_geofence_set():
    need_login(admin=True)
    d = request.get_json(silent=True) or {}
    try:
        lat, lng, radius = float(d["lat"]), float(d["lng"]), float(d["radius_m"])
        assert -90 <= lat <= 90 and -180 <= lng <= 180 and 20 <= radius <= 5000
    except Exception:
        return jsonify(error="Invalid location or radius (20-5000 m)."), 400
    c = db.conn()
    for k, v in (("geo_lat", lat), ("geo_lng", lng), ("geo_radius_m", radius)):
        c.execute("INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, str(v)))
    c.commit()
    c.close()
    return jsonify(ok=True)


@app.get("/photos/<name>")
def photo(name):
    u = need_login()
    if not re.match(r"^\d+_[\w]+\.jpg$", name):
        abort(404)
    if u["role"] != "admin" and not name.startswith(f"{u['emp_id']}_"):
        abort(403)
    return send_from_directory(PHOTO_DIR, name)


if __name__ == "__main__":
    # Camera + GPS in browsers need HTTPS (or localhost). For phones on the network, put this behind HTTPS.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
