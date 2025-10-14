# app.py — EchoBoard Full MVP
# Flask + Socket.IO (eventlet) + SQLite, Pterodactyl-ready

import os
import io
import csv
import sqlite3
import secrets

import eventlet
eventlet.monkey_patch()

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, send_from_directory, jsonify, abort, Response
)
from flask_socketio import SocketIO, join_room, emit
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from functools import wraps

# -------------------- Config --------------------
APP_PORT = int(os.getenv("SERVER_PORT") or os.getenv("PORT") or "8080")
DB_PATH = os.getenv("DB_PATH", "echoboard.db")

UPLOAD_DIR = os.getenv("UPLOAD_DIR", "uploads")
AVATAR_DIR = os.path.join(UPLOAD_DIR, "avatars")
os.makedirs(AVATAR_DIR, exist_ok=True)

app = Flask(__name__, template_folder="templates")
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret")  # prod: env setzen
app.config["MAX_CONTENT_LENGTH"] = 3 * 1024 * 1024  # 3 MB

# Socket.IO
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

# -------------------- DB Helpers --------------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    c = db(); cur = c.cursor()
    cur.executescript("""
    PRAGMA journal_mode=WAL;

    CREATE TABLE IF NOT EXISTS boards(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      code TEXT UNIQUE,
      title TEXT DEFAULT 'Untitled',
      theme TEXT DEFAULT 'ocean',
      created_at TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS cards(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      board_id INTEGER NOT NULL,
      author TEXT DEFAULT '',
      text TEXT NOT NULL,
      tag TEXT DEFAULT '',
      votes INTEGER DEFAULT 0,
      created_at TEXT DEFAULT (datetime('now')),
      FOREIGN KEY(board_id) REFERENCES boards(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS messages(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      board_id INTEGER NOT NULL,
      author TEXT DEFAULT '',
      text TEXT NOT NULL,
      created_at TEXT DEFAULT (datetime('now')),
      FOREIGN KEY(board_id) REFERENCES boards(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      username TEXT UNIQUE NOT NULL,
      email TEXT UNIQUE,
      pass_hash TEXT NOT NULL,
      avatar TEXT,
      created_at TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS contacts(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER NOT NULL,
      contact_id INTEGER NOT NULL,
      status TEXT DEFAULT 'accepted',
      created_at TEXT DEFAULT (datetime('now')),
      UNIQUE(user_id, contact_id),
      FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
      FOREIGN KEY(contact_id) REFERENCES users(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS dm_messages(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      sender_id INTEGER NOT NULL,
      receiver_id INTEGER NOT NULL,
      text TEXT NOT NULL,
      created_at TEXT DEFAULT (datetime('now')),
      FOREIGN KEY(sender_id) REFERENCES users(id) ON DELETE CASCADE,
      FOREIGN KEY(receiver_id) REFERENCES users(id) ON DELETE CASCADE
    );
    """)
    # Falls alte DB ohne theme-Spalte existiert:
    try:
        cur.execute("ALTER TABLE boards ADD COLUMN theme TEXT DEFAULT 'ocean'")
        c.commit()
    except sqlite3.OperationalError:
        pass
    c.close()

def get_board_by_code(code):
    c = db(); cur = c.cursor()
    cur.execute("SELECT * FROM boards WHERE code=?", (code,))
    row = cur.fetchone()
    c.close()
    return row

def create_board():
    code = secrets.token_hex(3)  # ~6 Zeichen
    c = db(); cur = c.cursor()
    cur.execute("INSERT INTO boards(code) VALUES(?)", (code,))
    c.commit(); c.close()
    return code

# -------------------- Auth Helpers --------------------
def current_user():
    uid = session.get("uid")
    if not uid: return None
    c = db(); cur = c.cursor()
    cur.execute("SELECT id, username, email, avatar FROM users WHERE id=?", (uid,))
    row = cur.fetchone(); c.close()
    return dict(row) if row else None

def login_required(fn):
    @wraps(fn)
    def wrap(*args, **kwargs):
        if not session.get("uid"):
            return redirect(url_for("login_page", next=request.path))
        return fn(*args, **kwargs)
    return wrap

def get_user_by_username(name):
    c = db(); cur = c.cursor()
    cur.execute("SELECT * FROM users WHERE username=?", (name,))
    row = cur.fetchone(); c.close()
    return row

def add_contact(user_id, contact_id):
    if user_id == contact_id: return
    c = db(); cur = c.cursor()
    try:
        cur.execute("INSERT OR IGNORE INTO contacts(user_id, contact_id, status) VALUES(?,?, 'accepted')",
                    (user_id, contact_id))
        cur.execute("INSERT OR IGNORE INTO contacts(user_id, contact_id, status) VALUES(?,?, 'accepted')",
                    (contact_id, user_id))
        c.commit()
    finally:
        c.close()

# -------------------- HTTP: Core --------------------
@app.get("/")
def home():
    return render_template("index.html")

@app.post("/new")
def new_board():
    code = create_board()
    return redirect(url_for("board_page", code=code))

@app.get("/b/<code>")
def board_page(code):
    b = get_board_by_code(code)
    if not b:
        return "Board not found", 404
    return render_template("board.html", code=code, theme=b["theme"])

# -------------------- HTTP: Auth --------------------
@app.get("/auth")
def login_page():
    if current_user(): return redirect(url_for("home"))
    return render_template("auth.html")

@app.post("/auth/register")
def register_post():
    username = (request.form.get("username") or "").strip().lower()[:24]
    email = (request.form.get("email") or "").strip()
    password = (request.form.get("password") or "")[:128]
    if not username or not password:
        return "Missing username or password", 400
    pass_hash = generate_password_hash(password)
    c = db(); cur = c.cursor()
    try:
        cur.execute("INSERT INTO users(username, email, pass_hash) VALUES(?,?,?)",
                    (username, email, pass_hash))
        c.commit()
        cur.execute("SELECT id FROM users WHERE username=?", (username,))
        uid = cur.fetchone()["id"]
        session["uid"] = uid
        return redirect(url_for("home"))
    except sqlite3.IntegrityError:
        return "Username oder Email schon vergeben", 400
    finally:
        c.close()

@app.post("/auth/login")
def login_post():
    username = (request.form.get("username") or "").strip().lower()
    password = (request.form.get("password") or "")
    u = get_user_by_username(username)
    if not u or not check_password_hash(u["pass_hash"], password):
        return "Login fehlgeschlagen", 400
    session["uid"] = u["id"]
    return redirect(url_for("home"))

@app.post("/auth/logout")
def logout_post():
    session.pop("uid", None)
    return redirect(url_for("home"))

# -------------------- HTTP: Profile/Avatar --------------------
@app.get("/settings")
@login_required
def settings_page():
    return render_template("settings.html", u=current_user())

@app.post("/settings/avatar")
@login_required
def upload_avatar():
    f = request.files.get("avatar")
    if not f: return "no file", 400
    fn = secure_filename(f.filename or "avatar.png")
    ext = (os.path.splitext(fn)[1] or ".png").lower()
    if ext not in [".png", ".jpg", ".jpeg", ".webp"]:
        return "bad type", 400
    uid = session["uid"]
    stored = f"user_{uid}{ext}"
    path = os.path.join(AVATAR_DIR, stored)
    f.save(path)
    c = db(); cur = c.cursor()
    cur.execute("UPDATE users SET avatar=? WHERE id=?", (stored, uid))
    c.commit(); c.close()
    return redirect(url_for("settings_page"))

@app.get("/u/avatars/<path:fn>")
def serve_avatar(fn):
    return send_from_directory(AVATAR_DIR, fn, as_attachment=False, download_name=fn)

# -------------------- HTTP: Contacts --------------------
@app.get("/contacts")
@login_required
def contacts_page():
    u = current_user()
    c = db(); cur = c.cursor()
    cur.execute("""SELECT users.id, users.username, users.avatar
                   FROM contacts JOIN users ON users.id = contacts.contact_id
                   WHERE contacts.user_id=? ORDER BY users.username""", (u["id"],))
    rows = [dict(x) for x in cur.fetchall()]
    c.close()
    return render_template("contacts.html", u=u, contacts=rows)

@app.post("/contacts/add")
@login_required
def contacts_add():
    u = current_user()
    username = (request.form.get("username") or "").strip().lower()
    other = get_user_by_username(username)
    if not other: return "User nicht gefunden", 404
    add_contact(u["id"], other["id"])
    return redirect(url_for("contacts_page"))

# -------------------- HTTP: Direct Messages --------------------
@app.get("/dm/<username>")
@login_required
def dm_page(username):
    me = current_user()
    other = get_user_by_username(username.lower())
    if not other: return "User not found", 404
    c = db(); cur = c.cursor()
    cur.execute("""SELECT id, sender_id, receiver_id, text, created_at
                   FROM dm_messages
                   WHERE (sender_id=? AND receiver_id=?) OR (sender_id=? AND receiver_id=?)
                   ORDER BY id DESC LIMIT 100""",
                (me["id"], other["id"], other["id"], me["id"]))
    msgs = [dict(x) for x in cur.fetchall()]
    c.close()
    return render_template("dm.html", me=me, other=dict(other), messages=list(reversed(msgs)))

# -------------------- HTTP: Mini APIs --------------------
@app.get("/api/me")
def api_me():
    u = current_user()
    return jsonify(u or {})

@app.get("/api/board/<code>/export/cards.csv")
def export_cards_csv(code):
    b = get_board_by_code(code)
    if not b: abort(404)
    c = db(); cur = c.cursor()
    cur.execute("""SELECT cards.id, cards.author, cards.text, cards.tag, cards.votes, cards.created_at
                   FROM cards JOIN boards ON boards.id = cards.board_id
                   WHERE boards.code=? ORDER BY cards.id ASC""", (code,))
    rows = cur.fetchall(); c.close()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id","author","text","tag","votes","created_at"])
    for r in rows:
        writer.writerow([r["id"], r["author"], r["text"], r["tag"], r["votes"], r["created_at"]])
    buf.seek(0)
    return Response(buf.read(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=board_{code}_cards.csv"})

@app.get("/api/board/<code>/export/messages.csv")
def export_messages_csv(code):
    b = get_board_by_code(code)
    if not b: abort(404)
    c = db(); cur = c.cursor()
    cur.execute("""SELECT messages.id, messages.author, messages.text, messages.created_at
                   FROM messages JOIN boards ON boards.id = messages.board_id
                   WHERE boards.code=? ORDER BY messages.id ASC""", (code,))
    rows = cur.fetchall(); c.close()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id","author","text","created_at"])
    for r in rows:
        writer.writerow([r["id"], r["author"], r["text"], r["created_at"]])
    buf.seek(0)
    return Response(buf.read(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=board_{code}_messages.csv"})

# -------------------- Realtime: Presence/Boards --------------------
presence = {}        # code -> set(socket_ids)
presence_names = {}  # code -> {sid: name}
ALLOWED_THEMES = {"ocean", "mint", "sunset", "violet", "slate"}

def board_room(code) -> str:
    return f"board_{code}"

# DM online mapping
user_sids = {}  # user_id -> set(sids)

@socketio.on("connect")
def on_connect():
    # Map Flask-Session-User zu dieser SID
    u = current_user()
    if u:
        user_sids.setdefault(u["id"], set()).add(request.sid)

@socketio.on("disconnect")
def ws_disconnect():
    # Boards: aus allen Präsenzlisten entfernen
    for code, sids in list(presence.items()):
        if request.sid in sids:
            sids.remove(request.sid)
            presence_names.get(code, {}).pop(request.sid, None)
            socketio.emit("presence", {
                "count": len(sids),
                "names": sorted([n for n in presence_names.get(code, {}).values() if n])
            }, room=board_room(code))
    # DMs: online mapping abbauen
    for uid, sids in list(user_sids.items()):
        if request.sid in sids:
            sids.remove(request.sid)
            if not sids:
                user_sids.pop(uid, None)

@socketio.on("join_board")
def join_board(data):
    code = (data or {}).get("code")
    client_name = ((data or {}).get("clientName") or "Anon")[:24]

    b = get_board_by_code(code)
    if not code or not b:
        emit("error", {"message": "Invalid board code"})
        return

    room = board_room(code)
    join_room(room)

    presence.setdefault(code, set()).add(request.sid)
    presence_names.setdefault(code, {})[request.sid] = client_name

    c = db(); cur = c.cursor()
    cur.execute("""
        SELECT cards.* FROM cards
        JOIN boards ON boards.id = cards.board_id
        WHERE boards.code = ?
        ORDER BY cards.id DESC
    """, (code,))
    cards = [dict(x) for x in cur.fetchall()]

    cur.execute("""
        SELECT messages.id, messages.author, messages.text, messages.created_at
        FROM messages
        JOIN boards ON boards.id = messages.board_id
        WHERE boards.code = ?
        ORDER BY messages.id DESC
        LIMIT 50
    """, (code,))
    messages = [dict(x) for x in cur.fetchall()]
    c.close()

    emit("board_state", {
        "cards": cards,
        "messages": list(reversed(messages)),
        "theme": b["theme"]
    })

    socketio.emit("presence", {
        "count": len(presence[code]),
        "names": sorted([n for n in presence_names.get(code, {}).values() if n])
    }, room=room)

@socketio.on("create_card")
def create_card_ev(data):
    code = (data or {}).get("code")
    text = ((data or {}).get("text") or "").strip()[:280]
    tag = ((data or {}).get("tag") or "").strip()[:16]
    author = ((data or {}).get("author") or "").strip()[:32]
    if not code or not text:
        emit("error", {"message": "Missing code or text"})
        return
    b = get_board_by_code(code)
    if not b:
        emit("error", {"message": "Board not found"})
        return

    c = db(); cur = c.cursor()
    cur.execute("INSERT INTO cards(board_id,author,text,tag) VALUES(?,?,?,?)",
                (b["id"], author, text, tag))
    cid = cur.lastrowid
    cur.execute("SELECT * FROM cards WHERE id=?", (cid,))
    card = dict(cur.fetchone())
    c.commit(); c.close()

    socketio.emit("card_added", card, room=board_room(code))

@socketio.on("vote_card")
def vote_card_ev(data):
    code = (data or {}).get("code")
    cid = (data or {}).get("cardId")
    if not code or not cid:
        emit("error", {"message": "Missing code or cardId"})
        return
    c = db(); cur = c.cursor()
    cur.execute("UPDATE cards SET votes = COALESCE(votes, 0) + 1 WHERE id=?", (int(cid),))
    c.commit()
    cur.execute("SELECT * FROM cards WHERE id=?", (int(cid),))
    card = dict(cur.fetchone())
    c.close()
    socketio.emit("card_updated", card, room=board_room(code))

@socketio.on("send_chat")
def send_chat_ev(data):
    code = (data or {}).get("code")
    author = ((data or {}).get("author") or "").strip()[:32]
    text = ((data or {}).get("text") or "").strip()[:500]
    if not code or not text:
        emit("error", {"message": "Missing code or text"})
        return
    b = get_board_by_code(code)
    if not b:
        emit("error", {"message": "Board not found"})
        return

    c = db(); cur = c.cursor()
    cur.execute("INSERT INTO messages(board_id,author,text) VALUES(?,?,?)",
                (b["id"], author, text))
    mid = cur.lastrowid
    cur.execute("SELECT id, author, text, created_at FROM messages WHERE id=?", (mid,))
    msg = dict(cur.fetchone())
    c.commit(); c.close()

    socketio.emit("chat_added", msg, room=board_room(code))

@socketio.on("set_theme")
def set_theme_ev(data):
    code = (data or {}).get("code")
    theme = ((data or {}).get("theme") or "ocean").strip().lower()
    if theme not in ALLOWED_THEMES:
        emit("error", {"message": "Invalid theme"})
        return
    b = get_board_by_code(code)
    if not b:
        emit("error", {"message": "Board not found"})
        return
    c = db(); cur = c.cursor()
    cur.execute("UPDATE boards SET theme=? WHERE id=?", (theme, b["id"]))
    c.commit(); c.close()
    socketio.emit("theme_changed", {"theme": theme}, room=board_room(code))

# -------------------- Realtime: Direct Messages --------------------
def dm_room(a_id, b_id):
    lo, hi = sorted([int(a_id), int(b_id)])
    return f"dm_{lo}_{hi}"

@socketio.on("dm_join")
def dm_join(data):
    me = current_user()
    if not me:
        emit("error", {"message": "auth required"}); return
    other_name = (data or {}).get("other")
    other = get_user_by_username((other_name or "").lower())
    if not other:
        emit("error", {"message": "other not found"}); return
    join_room(dm_room(me["id"], other["id"]))
    emit("dm_presence", {
        "other_online": other["id"] in user_sids and len(user_sids[other["id"]]) > 0
    }, room=request.sid)

@socketio.on("dm_send")
def dm_send(data):
    me = current_user()
    if not me:
        emit("error", {"message": "auth required"}); return
    to_name = (data or {}).get("to")
    text = ((data or {}).get("text") or "").strip()[:1000]
    if not to_name or not text:
        emit("error", {"message": "missing to/text"}); return
    other = get_user_by_username(to_name.lower())
    if not other:
        emit("error", {"message": "other not found"}); return

    c = db(); cur = c.cursor()
    cur.execute("INSERT INTO dm_messages(sender_id,receiver_id,text) VALUES(?,?,?)",
                (me["id"], other["id"], text))
    mid = cur.lastrowid
    cur.execute("SELECT id, sender_id, receiver_id, text, created_at FROM dm_messages WHERE id=?", (mid,))
    msg = dict(cur.fetchone())
    c.commit(); c.close()

    payload = {
        "id": msg["id"],
        "sender_id": me["id"],
        "receiver_id": other["id"],
        "sender": current_user()["username"],
        "text": msg["text"],
        "created_at": msg["created_at"]
    }
    socketio.emit("dm_new", payload, room=dm_room(me["id"], other["id"]))

# -------------------- Main --------------------
if __name__ == "__main__":
    init_db()
    print(f"[Boot] EchoBoard startet auf 0.0.0.0:{APP_PORT}")
    socketio.run(app, host="0.0.0.0", port=APP_PORT)
