# SIGMA – Employee attendance (photo + geofence)

Run:  pip install -r requirements.txt && python app.py   -> http://localhost:5000
The database (data/sigma.db) is created automatically from data/PINGME_DB.xlsx on first start.
To rebuild it from the Excel file:  python db.py

Logins (from the DB): ADMIN / Admin_1 (Rakesh), worker / worker_1 (Shankeerth), worker / worker_2 (Sharath)

1. Log in as ADMIN on the phone/laptop AT THE OFFICE -> Geofence tab -> "Use my current location as centre" (set radius).
2. Employees log in, allow camera + location, tap Punch IN / OUT. A live photo is captured and checked against the geofence on the server.
3. Admin sees all punches with photos, distance and map link.

IMPORTANT: browsers allow camera + GPS only on HTTPS (or localhost). For phones, deploy behind HTTPS
(e.g. Render/Railway/a VPS with nginx + Let's Encrypt, or a Cloudflare tunnel).
Change the sample passwords in the Excel before going live (passwords are stored hashed in the DB).
