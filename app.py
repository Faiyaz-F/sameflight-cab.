"""SameFlight Cab - backend (Flask + SQLite). Run: python app.py"""
import base64
import functools
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import date

from flask import Flask, g, jsonify, request, send_from_directory

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH") or os.path.join(HERE, "samecab.db")
app = Flask(__name__, static_folder=os.path.join(HERE, "static"), static_url_path="")

# name: (x km, y km, solo cab fare in INR). Rough numbers - edit freely.
AREAS = {
    "Bandra": (0, 0, 1200), "Khar": (-1, -2, 1100), "Santacruz": (-2, -5, 900),
    "Juhu": (-5, -3, 1100), "Andheri": (-1, -7, 800), "Powai": (6, -8, 1400),
    "Worli": (3, 8, 1500), "Colaba": (5, 20, 2000),
}
MAX_DETOUR_MIN = 15      # extra minutes allowed for the second drop
MAX_LANDING_GAP = 30     # minutes between two landings

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, phone TEXT UNIQUE NOT NULL,
  woman INTEGER DEFAULT 0, created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS trips(
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, flight TEXT NOT NULL,
  date TEXT NOT NULL, landing INTEGER NOT NULL, dest TEXT NOT NULL,
  bags TEXT DEFAULT 'cabin', women_only INTEGER DEFAULT 0, status TEXT DEFAULT 'open', planned INTEGER, checked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS requests(
  id INTEGER PRIMARY KEY, from_trip INTEGER NOT NULL, to_trip INTEGER NOT NULL,
  status TEXT DEFAULT 'pending');
CREATE TABLE IF NOT EXISTS payments(
  request_id INTEGER PRIMARY KEY, status TEXT DEFAULT 'unpaid', method TEXT DEFAULT 'upi');
CREATE TABLE IF NOT EXISTS otps(
  phone TEXT PRIMARY KEY, code_hash TEXT NOT NULL, expires INTEGER NOT NULL,
  tries INTEGER DEFAULT 0, sent INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(
  token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created INTEGER NOT NULL);
"""


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_):
    conn = g.pop("db", None)
    if conn:
        conn.close()


def init_db():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    if not conn.execute("SELECT 1 FROM users").fetchone():  # sample riders for first run
        today = date.today().isoformat()
        people = [("Aarav", "9000000001", 0, "6E 204", 1120, "Khar"),
                  ("Meera", "9000000002", 1, "6E 204", 1120, "Bandra"),
                  ("Rohan", "9000000003", 0, "AI 617", 1135, "Bandra"),
                  ("Sana", "9000000004", 1, "6E 204", 1120, "Powai"),
                  ("Isha", "9000000005", 1, "6E 204", 1120, "Santacruz")]
        for name, phone, woman, flight, landing, dest in people:
            uid = conn.execute("INSERT INTO users(name,phone,woman) VALUES(?,?,?)",
                               (name, phone, woman)).lastrowid
            conn.execute("INSERT INTO trips(user_id,flight,date,landing,dest) VALUES(?,?,?,?,?)",
                         (uid, flight, today, landing, dest))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(trips)")}  # upgrade older databases
    for name, ddl in (("planned", "INTEGER"), ("checked", "INTEGER DEFAULT 0")):
        if name not in cols:
            conn.execute(f"ALTER TABLE trips ADD COLUMN {name} {ddl}")
    if "upi_id" not in {r[1] for r in conn.execute("PRAGMA table_info(users)")}:
        conn.execute("ALTER TABLE users ADD COLUMN upi_id TEXT")
    if "method" not in {r[1] for r in conn.execute("PRAGMA table_info(payments)")}:
        conn.execute("ALTER TABLE payments ADD COLUMN method TEXT DEFAULT 'upi'")
    conn.execute("UPDATE trips SET planned=landing WHERE planned IS NULL")
    conn.commit()
    conn.close()


def norm(flight):
    return "".join(flight.split()).upper()


def detour(a, b):
    ax, ay, _ = AREAS[a]
    bx, by, _ = AREAS[b]
    return round(math.hypot(ax - bx, ay - by) * 2.5)


def shared_fare(a, b):
    hi, lo = sorted([AREAS[a][2], AREAS[b][2]], reverse=True)
    return round((hi + lo * 0.25) / 2 / 10) * 10


def err(msg, code=400):
    return jsonify(error=msg), code


@app.get("/api/areas")
def areas():
    return jsonify({k: v[2] for k, v in AREAS.items()})


def clean_phone(raw):
    return "".join(c for c in (raw or "") if c.isdigit())[-10:]


def hash_code(phone, code):
    return hashlib.sha256((phone + ":" + code).encode()).hexdigest()


def send_sms(phone, text):
    """Sends via Twilio if TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM are set. Returns True if sent."""
    sid, tok, frm = (os.environ.get(k) for k in ("TWILIO_SID", "TWILIO_TOKEN", "TWILIO_FROM"))
    if not (sid and tok and frm):
        return None  # not configured -> test mode
    body = urllib.parse.urlencode({"To": "+91" + phone, "From": frm, "Body": text}).encode()
    req = urllib.request.Request(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json", data=body)
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{sid}:{tok}".encode()).decode())
    try:
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception:
        return False


def current_user():
    header = request.headers.get("Authorization", "")
    token = header[7:] if header.startswith("Bearer ") else ""
    if not token:
        return None
    return db().execute("SELECT u.id, u.name, u.woman FROM sessions s JOIN users u ON u.id=s.user_id "
                        "WHERE s.token=?", (token,)).fetchone()


def login_required(fn):
    @functools.wraps(fn)
    def wrapper(*a, **k):
        g.user = current_user()
        if not g.user:
            return err("Please log in again.", 401)
        return fn(*a, **k)
    return wrapper


@app.post("/api/otp/send")
def otp_send():
    phone = clean_phone((request.get_json(force=True) or {}).get("phone"))
    if len(phone) != 10:
        return err("Enter a 10-digit phone number.")
    if os.environ.get("REQUIRE_SMS") == "1" and not os.environ.get("TWILIO_SID"):
        return err("SMS login is not set up yet.", 503)  # blocks on-screen test codes on a public site
    conn = db()
    old = conn.execute("SELECT sent FROM otps WHERE phone=?", (phone,)).fetchone()
    if old and time.time() - old["sent"] < 30:
        return err("Please wait 30 seconds before asking for another code.", 429)
    code = f"{secrets.randbelow(10 ** 6):06d}"
    conn.execute("INSERT OR REPLACE INTO otps(phone,code_hash,expires,tries,sent) VALUES(?,?,?,0,?)",
                 (phone, hash_code(phone, code), int(time.time()) + 300, int(time.time())))
    conn.commit()
    sent = send_sms(phone, f"Your SameFlight Cab code is {code}. It is valid for 5 minutes.")
    if sent is None:  # test mode: no SMS provider configured
        print(f"[TEST MODE] OTP for {phone}: {code}")
        return jsonify(ok=True, dev_otp=code)
    if not sent:
        return err("Could not send the SMS. Try again.", 502)
    return jsonify(ok=True)


@app.post("/api/otp/verify")
def otp_verify():
    d = request.get_json(force=True) or {}
    phone, code = clean_phone(d.get("phone")), "".join(c for c in (d.get("code") or "") if c.isdigit())
    conn = db()
    row = conn.execute("SELECT * FROM otps WHERE phone=?", (phone,)).fetchone()
    if not row or row["expires"] < time.time():
        return err("Code expired. Ask for a new one.")
    if row["tries"] >= 5:
        return err("Too many wrong tries. Ask for a new code.", 429)
    if not hmac.compare_digest(row["code_hash"], hash_code(phone, code)):
        conn.execute("UPDATE otps SET tries=tries+1 WHERE phone=?", (phone,))
        conn.commit()
        return err("Wrong code. Check it and try again.")
    conn.execute("DELETE FROM otps WHERE phone=?", (phone,))
    name, woman = (d.get("name") or "").strip()[:60], int(bool(d.get("woman")))
    u = conn.execute("SELECT * FROM users WHERE phone=?", (phone,)).fetchone()
    if u:
        conn.execute("UPDATE users SET name=?, woman=? WHERE id=?", (name or u["name"], woman, u["id"]))
        uid = u["id"]
    else:
        if not name:
            return err("Enter your name.")
        uid = conn.execute("INSERT INTO users(name,phone,woman) VALUES(?,?,?)", (name, phone, woman)).lastrowid
    token = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO sessions(token,user_id,created) VALUES(?,?,?)", (token, uid, int(time.time())))
    conn.commit()
    user = conn.execute("SELECT id,name,woman FROM users WHERE id=?", (uid,)).fetchone()
    return jsonify(token=token, user=dict(user))


# Demo flights work without any API key, so you can test the feature.
DEMO_FLIGHTS = {"6E204": ("18:40", 0), "AI617": ("19:05", 30)}


def clock(value):
    """'2026-11-03 18:40+05:30' -> minutes since midnight (or None)."""
    m = re.search(r"(\d{2}):(\d{2})", (value or "").split(" ")[-1])
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def lookup_flight(number, day):
    """Returns a dict: landing ('HH:MM' or None), delay, status, source, note."""
    key = os.environ.get("AERODATABOX_KEY")
    if not key:
        if number in DEMO_FLIGHTS:
            t, delay = DEMO_FLIGHTS[number]
            return {"landing": t, "delay": delay, "source": "demo"}
        return {"landing": None, "note": "Live lookup is off (no API key). Type the landing time yourself."}
    url = f"https://aerodatabox.p.rapidapi.com/flights/number/{urllib.parse.quote(number)}/{day}"
    req = urllib.request.Request(url, headers={"X-RapidAPI-Key": key,
                                               "X-RapidAPI-Host": "aerodatabox.p.rapidapi.com"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.load(r)
    except Exception:
        return {"landing": None, "note": "Could not reach the flight service. Type the landing time yourself."}
    flights = data if isinstance(data, list) else data.get("flights", [])
    if not flights:
        return {"landing": None, "note": "Flight not found for that date. Type the landing time yourself."}
    f = next((x for x in flights if ((x.get("arrival") or {}).get("airport") or {}).get("iata") == "BOM"),
             flights[0])
    arr = f.get("arrival") or {}
    iata = (arr.get("airport") or {}).get("iata")
    planned = clock((arr.get("scheduledTime") or {}).get("local"))
    live = clock((arr.get("revisedTime") or {}).get("local"))
    best = live if live is not None else planned
    if best is None:
        return {"landing": None, "note": "No landing time yet. Type it yourself."}
    delay = (live - planned) if (live is not None and planned is not None) else 0
    note = f"This flight lands at {iata}, not Mumbai." if iata and iata != "BOM" else ""
    return {"landing": f"{best // 60 % 24:02d}:{best % 60:02d}", "delay": delay,
            "status": f.get("status"), "source": "live", "note": note}


@app.get("/api/flight")
@login_required
def flight_lookup():
    number = "".join((request.args.get("number") or "").split()).upper()
    day = request.args.get("date") or date.today().isoformat()
    if not number:
        return err("Enter a flight number.")
    return jsonify(lookup_flight(number, day))


REFRESH_SECONDS = 300  # re-check live flight status at most every 5 minutes per trip


def refresh_landing(conn, t):
    """If a live flight service is configured, move the trip's landing time when the flight is delayed."""
    if t["status"] not in ("open", "matched") or time.time() - (t["checked"] or 0) < REFRESH_SECONDS:
        return t
    conn.execute("UPDATE trips SET checked=? WHERE id=?", (int(time.time()), t["id"]))
    info = lookup_flight("".join(t["flight"].split()).upper(), t["date"])
    if info.get("source") == "live" and info.get("landing"):
        h, m = map(int, info["landing"].split(":"))
        if h * 60 + m != t["landing"]:
            conn.execute("UPDATE trips SET landing=? WHERE id=?", (h * 60 + m, t["id"]))
    conn.commit()
    return conn.execute("SELECT t.*, u.name, u.woman FROM trips t JOIN users u ON u.id=t.user_id WHERE t.id=?",
                        (t["id"],)).fetchone()


@app.post("/api/trips/<int:tid>/landing")
@login_required
def update_landing(tid):
    try:
        h, m = map(int, (request.get_json(force=True) or {})["landing"].split(":"))
    except (KeyError, ValueError, AttributeError):
        return err("Enter a valid time.")
    conn = db()
    cur = conn.execute("UPDATE trips SET landing=? WHERE id=? AND user_id=? AND status IN ('open','matched')",
                       (h * 60 + m, tid, g.user["id"]))
    conn.commit()
    if not cur.rowcount:
        return err("Trip not found.", 404)
    return jsonify(ok=True)


def host_of(ta, tb):
    """The rider with the farther drop books and pays the cab; the other sends their share by UPI."""
    fa, fb = AREAS[ta["dest"]][2], AREAS[tb["dest"]][2]
    return ta if (fa > fb or (fa == fb and ta["id"] < tb["id"])) else tb


def ride_role(conn, rid, uid):
    r = conn.execute("SELECT * FROM requests WHERE id=? AND status='accepted'", (rid,)).fetchone()
    if not r:
        return None
    ta = conn.execute("SELECT * FROM trips WHERE id=?", (r["from_trip"],)).fetchone()
    tb = conn.execute("SELECT * FROM trips WHERE id=?", (r["to_trip"],)).fetchone()
    if uid not in (ta["user_id"], tb["user_id"]):
        return None
    return "host" if host_of(ta, tb)["user_id"] == uid else "guest"


@app.post("/api/profile")
@login_required
def save_profile():
    upi = ((request.get_json(force=True) or {}).get("upi_id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._\-]{2,}@[A-Za-z]{2,}", upi):
        return err("Enter a valid UPI ID, like name@bank.")
    conn = db()
    conn.execute("UPDATE users SET upi_id=? WHERE id=?", (upi, g.user["id"]))
    conn.commit()
    return jsonify(ok=True)


def set_payment(rid, need_role, allowed, new_status):
    conn = db()
    if ride_role(conn, rid, g.user["id"]) != need_role:
        return err("Not allowed.", 403)
    pay = conn.execute("SELECT status FROM payments WHERE request_id=?", (rid,)).fetchone()
    if not pay or pay["status"] not in allowed:
        return err("Nothing to update.", 409)
    conn.execute("UPDATE payments SET status=? WHERE request_id=?", (new_status, rid))
    conn.commit()
    return jsonify(ok=True)


@app.post("/api/payments/<int:rid>/method")
@login_required
def payment_method(rid):
    method = (request.get_json(force=True) or {}).get("method")
    if method not in ("upi", "cash"):
        return err("Choose UPI or cash.")
    conn = db()
    if ride_role(conn, rid, g.user["id"]) != "guest":
        return err("Not allowed.", 403)
    cur = conn.execute("UPDATE payments SET method=? WHERE request_id=? AND status='unpaid'", (method, rid))
    conn.commit()
    if not cur.rowcount:
        return err("Nothing to update.", 409)
    return jsonify(ok=True)


@app.post("/api/payments/<int:rid>/paid")
@login_required
def payment_paid(rid):
    return set_payment(rid, "guest", ("unpaid",), "marked_paid")


@app.post("/api/payments/<int:rid>/confirm")
@login_required
def payment_confirm(rid):
    return set_payment(rid, "host", ("unpaid", "marked_paid"), "confirmed")


@app.post("/api/trips")
@login_required
def create_trip():
    d = request.get_json(force=True)
    try:
        h, m = map(int, d["landing"].split(":"))
        dest, flight = d["dest"], (d["flight"] or "").strip()
        uid = g.user["id"]
    except (KeyError, ValueError, AttributeError):
        return err("Fill in all trip details.")
    if dest not in AREAS or not flight:
        return err("Pick a valid destination and flight number.")
    conn = db()
    conn.execute("UPDATE trips SET status='cancelled' WHERE user_id=? AND status='open'", (uid,))
    cur = conn.execute(
        "INSERT INTO trips(user_id,flight,date,landing,dest,bags,women_only,planned) VALUES(?,?,?,?,?,?,?,?)",
        (uid, flight, d.get("date") or date.today().isoformat(), h * 60 + m, dest,
         d.get("bags", "cabin"), int(bool(d.get("women_only"))), h * 60 + m))
    conn.commit()
    return jsonify(id=cur.lastrowid)


@app.get("/api/trips/<int:tid>")
@login_required
def trip_state(tid):
    conn = db()
    t = conn.execute("SELECT t.*, u.name, u.woman FROM trips t JOIN users u ON u.id=t.user_id WHERE t.id=?",
                     (tid,)).fetchone()
    if not t:
        return err("Trip not found.", 404)
    if t["user_id"] != g.user["id"]:
        return err("This is not your trip.", 403)
    t = refresh_landing(conn, t)
    out = {"trip": dict(t), "solo_fare": AREAS[t["dest"]][2], "matches": [], "incoming": [],
           "outgoing": [], "ride": None}
    ride = conn.execute(
        "SELECT * FROM requests WHERE status='accepted' AND (from_trip=? OR to_trip=?)", (tid, tid)).fetchone()
    if ride:
        pid = ride["to_trip"] if ride["from_trip"] == tid else ride["from_trip"]
        p = conn.execute("SELECT t.*, u.name, u.phone, u.upi_id FROM trips t JOIN users u ON u.id=t.user_id WHERE t.id=?",
                         (pid,)).fetchone()
        wait = 25 if "check" in (t["bags"], p["bags"]) else 10
        out["ride"] = {"partner": p["name"], "phone": p["phone"], "dest": p["dest"],
                       "fare_each": shared_fare(t["dest"], p["dest"]),
                       "pickup": max(t["landing"], p["landing"]) + wait,
                       "partner_late": p["landing"] - (p["planned"] or p["landing"])}
        hi, lo = sorted([AREAS[t["dest"]][2], AREAS[p["dest"]][2]], reverse=True)
        is_host = host_of(t, p)["id"] == t["id"]
        conn.execute("INSERT OR IGNORE INTO payments(request_id) VALUES(?)", (ride["id"],))
        conn.commit()
        pay = conn.execute("SELECT status, method FROM payments WHERE request_id=?", (ride["id"],)).fetchone()
        mine = conn.execute("SELECT upi_id FROM users WHERE id=?", (g.user["id"],)).fetchone()["upi_id"]
        out["ride"].update(request_id=ride["id"], role="host" if is_host else "guest", total=round(hi + lo * 0.25),
                           pay_status=pay["status"], pay_method=pay["method"], my_upi=mine,
                           host_name=g.user["name"] if is_host else p["name"],
                           host_upi=mine if is_host else p["upi_id"])
        return jsonify(out)
    if t["status"] != "open":
        return jsonify(out)
    rows = conn.execute(
        "SELECT t.*, u.name, u.woman FROM trips t JOIN users u ON u.id=t.user_id "
        "WHERE t.status='open' AND t.date=? AND t.user_id!=?", (t["date"], t["user_id"])).fetchall()
    sent = {r["to_trip"]: r["status"] for r in conn.execute("SELECT * FROM requests WHERE from_trip=?", (tid,))}
    for o in rows:
        if t["women_only"] and not o["woman"]:
            continue
        if o["women_only"] and not t["woman"]:
            continue
        same = norm(o["flight"]) == norm(t["flight"])
        if not same and abs(o["landing"] - t["landing"]) > MAX_LANDING_GAP:
            continue
        det = detour(t["dest"], o["dest"])
        if det > MAX_DETOUR_MIN:
            continue
        out["matches"].append({"trip_id": o["id"], "name": o["name"], "flight": o["flight"], "dest": o["dest"],
                               "landing": o["landing"], "bags": o["bags"], "same_flight": same, "detour": det,
                               "fare_each": shared_fare(t["dest"], o["dest"]), "woman": bool(o["woman"]),
                               "requested": sent.get(o["id"]) == "pending"})
    out["matches"].sort(key=lambda m: (not m["same_flight"], m["detour"]))
    for r in conn.execute(
            "SELECT r.id, t.dest, t.flight, u.name FROM requests r JOIN trips t ON t.id=r.from_trip "
            "JOIN users u ON u.id=t.user_id WHERE r.to_trip=? AND r.status='pending'", (tid,)):
        out["incoming"].append(dict(r))
    return jsonify(out)


@app.post("/api/requests")
@login_required
def send_request():
    d = request.get_json(force=True)
    conn = db()
    mine = conn.execute("SELECT 1 FROM trips WHERE id=? AND user_id=? AND status='open'",
                        (d.get("from_trip"), g.user["id"])).fetchone()
    other = conn.execute("SELECT 1 FROM trips WHERE id=? AND status='open'", (d.get("to_trip"),)).fetchone()
    if not mine or not other:
        return err("That trip is not available.", 404)
    if conn.execute("SELECT 1 FROM requests WHERE from_trip=? AND to_trip=? AND status='pending'",
                    (d["from_trip"], d["to_trip"])).fetchone():
        return jsonify(ok=True)
    conn.execute("INSERT INTO requests(from_trip,to_trip) VALUES(?,?)", (d["from_trip"], d["to_trip"]))
    conn.commit()
    return jsonify(ok=True)


@app.post("/api/requests/<int:rid>/accept")
@login_required
def accept(rid):
    conn = db()
    r = conn.execute("SELECT * FROM requests WHERE id=? AND status='pending'", (rid,)).fetchone()
    if not r:
        return err("Request is no longer available.", 404)
    a, b = r["from_trip"], r["to_trip"]
    if not conn.execute("SELECT 1 FROM trips WHERE id=? AND user_id=?", (b, g.user["id"])).fetchone():
        return err("This request is not for you.", 403)
    if conn.execute("SELECT COUNT(*) FROM trips WHERE id IN (?,?) AND status='open'", (a, b)).fetchone()[0] != 2:
        return err("This rider is no longer available.", 409)
    conn.execute("UPDATE requests SET status='accepted' WHERE id=?", (rid,))
    conn.execute("UPDATE trips SET status='matched' WHERE id IN (?,?)", (a, b))
    conn.execute("UPDATE requests SET status='declined' WHERE status='pending' AND "
                 "(from_trip IN (?,?) OR to_trip IN (?,?))", (a, b, a, b))
    conn.commit()
    return jsonify(ok=True)


@app.get("/healthz")
def healthz():
    return "ok"


@app.get("/")
def index():
    for folder in (app.static_folder, HERE):
        if os.path.exists(os.path.join(folder, "index.html")):
            return send_from_directory(folder, "index.html")
    return "index.html not found. Put it in a folder named static next to app.py.", 404


init_db()  # runs on import too, so gunicorn creates the database

if __name__ == "__main__":
    print("Open http://localhost:5000  (same Wi-Fi: http://<your-computer-ip>:5000)")
    app.run(host="0.0.0.0", port=5000, debug=False)
