# app.py — EchoBoard Full MVP
# Flask + Socket.IO (eventlet) + SQLite, Pterodactyl-ready

import os
import io
import csv
import json
import re
import sqlite3
import secrets
from datetime import datetime, timedelta
from html import escape

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
BOARD_FILES_DIR = os.path.join(UPLOAD_DIR, "board_files")
VOICE_DIR = os.path.join(UPLOAD_DIR, "voices")
os.makedirs(AVATAR_DIR, exist_ok=True)
os.makedirs(BOARD_FILES_DIR, exist_ok=True)
os.makedirs(VOICE_DIR, exist_ok=True)

ALLOWED_FILE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".pdf": "application/pdf",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".ogg": "audio/ogg",
}

AUDIO_EXTENSIONS = {ext for ext, mime in ALLOWED_FILE_TYPES.items() if mime.startswith("audio/")}

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
      channel TEXT DEFAULT 'general',
      reply_to INTEGER,
      pinned INTEGER DEFAULT 0,
      attachments TEXT,
      voice_path TEXT,
      edited_at TEXT,
      deleted_at TEXT,
      created_at TEXT DEFAULT (datetime('now')),
      FOREIGN KEY(board_id) REFERENCES boards(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      username TEXT UNIQUE NOT NULL,
      email TEXT UNIQUE,
      pass_hash TEXT NOT NULL,
      avatar TEXT,
      status TEXT DEFAULT 'offline',
      badge TEXT DEFAULT 'Member',
      profile_theme TEXT DEFAULT 'ocean',
      accent_color TEXT DEFAULT '#38bdf8',
      created_at TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS contacts(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER NOT NULL,
      contact_id INTEGER NOT NULL,
      status TEXT DEFAULT 'pending',
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
      reply_to INTEGER,
      voice_path TEXT,
      edited_at TEXT,
      deleted_at TEXT,
      read_at TEXT,
      created_at TEXT DEFAULT (datetime('now')),
      FOREIGN KEY(sender_id) REFERENCES users(id) ON DELETE CASCADE,
      FOREIGN KEY(receiver_id) REFERENCES users(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS board_members(
      board_id INTEGER NOT NULL,
      user_id INTEGER NOT NULL,
      role TEXT DEFAULT 'member',
      joined_at TEXT DEFAULT (datetime('now')),
      PRIMARY KEY(board_id, user_id),
      FOREIGN KEY(board_id) REFERENCES boards(id) ON DELETE CASCADE,
      FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS board_activity(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      board_id INTEGER NOT NULL,
      user_id INTEGER,
      kind TEXT NOT NULL,
      payload TEXT,
      created_at TEXT DEFAULT (datetime('now')),
      FOREIGN KEY(board_id) REFERENCES boards(id) ON DELETE CASCADE,
      FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL
    );

    CREATE TABLE IF NOT EXISTS board_invites(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      board_id INTEGER NOT NULL,
      token TEXT UNIQUE NOT NULL,
      expires_at TEXT,
      created_by INTEGER,
      created_at TEXT DEFAULT (datetime('now')),
      FOREIGN KEY(board_id) REFERENCES boards(id) ON DELETE CASCADE,
      FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE SET NULL
    );

    CREATE TABLE IF NOT EXISTS message_reactions(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      message_id INTEGER NOT NULL,
      user_id INTEGER NOT NULL,
      emoji TEXT NOT NULL,
      created_at TEXT DEFAULT (datetime('now')),
      UNIQUE(message_id, user_id, emoji),
      FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE CASCADE,
      FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS dm_reactions(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      message_id INTEGER NOT NULL,
      user_id INTEGER NOT NULL,
      emoji TEXT NOT NULL,
      created_at TEXT DEFAULT (datetime('now')),
      UNIQUE(message_id, user_id, emoji),
      FOREIGN KEY(message_id) REFERENCES dm_messages(id) ON DELETE CASCADE,
      FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS message_reads(
      message_id INTEGER NOT NULL,
      user_id INTEGER NOT NULL,
      read_at TEXT DEFAULT (datetime('now')),
      PRIMARY KEY(message_id, user_id),
      FOREIGN KEY(message_id) REFERENCES dm_messages(id) ON DELETE CASCADE,
      FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS presence_history(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      board_id INTEGER NOT NULL,
      user_id INTEGER,
      action TEXT NOT NULL,
      details TEXT,
      created_at TEXT DEFAULT (datetime('now')),
      FOREIGN KEY(board_id) REFERENCES boards(id) ON DELETE CASCADE,
      FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL
    );

    CREATE TABLE IF NOT EXISTS notifications(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER NOT NULL,
      content TEXT NOT NULL,
      link TEXT,
      read_at TEXT,
      created_at TEXT DEFAULT (datetime('now')),
      FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS group_rooms(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      slug TEXT UNIQUE NOT NULL,
      title TEXT NOT NULL,
      created_by INTEGER,
      created_at TEXT DEFAULT (datetime('now')),
      FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE SET NULL
    );

    CREATE TABLE IF NOT EXISTS group_messages(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      room_id INTEGER NOT NULL,
      sender_id INTEGER,
      sender_name TEXT,
      text TEXT NOT NULL,
      edited_at TEXT,
      deleted_at TEXT,
      created_at TEXT DEFAULT (datetime('now')),
      FOREIGN KEY(room_id) REFERENCES group_rooms(id) ON DELETE CASCADE,
      FOREIGN KEY(sender_id) REFERENCES users(id) ON DELETE SET NULL
    );
    """)
    # Falls alte DB ohne theme-Spalte existiert:
    migrations = [
        "ALTER TABLE boards ADD COLUMN theme TEXT DEFAULT 'ocean'",
        "ALTER TABLE boards ADD COLUMN owner_id INTEGER",
        "ALTER TABLE boards ADD COLUMN template TEXT",
        "ALTER TABLE boards ADD COLUMN accent_color TEXT DEFAULT '#38bdf8'",
        "ALTER TABLE boards ADD COLUMN background_anim TEXT DEFAULT 'aurora'",
        "ALTER TABLE cards ADD COLUMN attachment_path TEXT",
        "ALTER TABLE cards ADD COLUMN order_index INTEGER",
        "ALTER TABLE messages ADD COLUMN channel TEXT DEFAULT 'general'",
        "ALTER TABLE messages ADD COLUMN reply_to INTEGER",
        "ALTER TABLE messages ADD COLUMN pinned INTEGER DEFAULT 0",
        "ALTER TABLE messages ADD COLUMN attachments TEXT",
        "ALTER TABLE messages ADD COLUMN voice_path TEXT",
        "ALTER TABLE messages ADD COLUMN edited_at TEXT",
        "ALTER TABLE messages ADD COLUMN deleted_at TEXT",
        "ALTER TABLE dm_messages ADD COLUMN reply_to INTEGER",
        "ALTER TABLE dm_messages ADD COLUMN voice_path TEXT",
        "ALTER TABLE dm_messages ADD COLUMN edited_at TEXT",
        "ALTER TABLE dm_messages ADD COLUMN deleted_at TEXT",
        "ALTER TABLE dm_messages ADD COLUMN read_at TEXT",
        "ALTER TABLE users ADD COLUMN status TEXT DEFAULT 'offline'",
        "ALTER TABLE users ADD COLUMN badge TEXT DEFAULT 'Member'",
        "ALTER TABLE users ADD COLUMN profile_theme TEXT DEFAULT 'ocean'",
        "ALTER TABLE users ADD COLUMN accent_color TEXT DEFAULT '#38bdf8'",
    ]
    for sql in migrations:
        try:
            cur.execute(sql)
            c.commit()
        except sqlite3.OperationalError:
            pass
    try:
        cur.execute("SELECT COUNT(*) FROM group_rooms")
        if cur.fetchone()[0] == 0:
            cur.execute("INSERT INTO group_rooms(slug, title) VALUES(?,?)", ("lobby", "Community Lounge"))
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

def create_board(owner_id=None, template_key=None):
    code = secrets.token_hex(3)  # ~6 Zeichen
    c = db(); cur = c.cursor()
    cur.execute("INSERT INTO boards(code, owner_id, template) VALUES(?,?,?)",
                (code, owner_id, template_key))
    board_id = cur.lastrowid
    if owner_id:
        cur.execute("INSERT OR REPLACE INTO board_members(board_id, user_id, role) VALUES(?,?,?)",
                    (board_id, owner_id, "owner"))
    c.commit()
    if template_key:
        apply_board_template(board_id, template_key)
    c.close()
    return code

# -------------------- Feature Helpers --------------------
def apply_board_template(board_id, template_key):
    tpl = BOARD_TEMPLATES.get(template_key)
    if not tpl:
        return
    c = db(); cur = c.cursor()
    title = tpl.get("title")
    theme = tpl.get("theme")
    if title or theme:
        cur.execute(
            "UPDATE boards SET title = COALESCE(?, title), theme = COALESCE(?, theme) WHERE id=?",
            (title, theme, board_id),
        )
    order_index = 0
    for author, text, tag in tpl.get("cards", []):
        cur.execute(
            "INSERT INTO cards(board_id, author, text, tag, order_index) VALUES(?,?,?,?,?)",
            (board_id, author, text, tag or "", order_index),
        )
        order_index += 1
    c.commit(); c.close()


def ensure_board_member(board_id, user_id, role="member"):
    if not user_id:
        return
    c = db(); cur = c.cursor()
    cur.execute("INSERT OR IGNORE INTO board_members(board_id, user_id, role) VALUES(?,?,COALESCE((SELECT role FROM board_members WHERE board_id=? AND user_id=?), ?))",
                (board_id, user_id, board_id, user_id, role))
    c.commit(); c.close()


def set_board_member_role(board_id, user_id, role):
    c = db(); cur = c.cursor()
    cur.execute("UPDATE board_members SET role=? WHERE board_id=? AND user_id=?", (role, board_id, user_id))
    if cur.rowcount == 0:
        cur.execute("INSERT INTO board_members(board_id, user_id, role) VALUES(?,?,?)", (board_id, user_id, role))
    c.commit(); c.close()


def get_board_members(board_id):
    c = db(); cur = c.cursor()
    cur.execute("""
        SELECT users.id, users.username, users.avatar, users.badge, users.status, board_members.role
        FROM board_members
        LEFT JOIN users ON users.id = board_members.user_id
        WHERE board_members.board_id=?
        ORDER BY board_members.role DESC, users.username
    """, (board_id,))
    rows = [dict(r) for r in cur.fetchall()]
    c.close()
    return rows


def get_member_role(board_id, user_id):
    if not user_id:
        return None
    c = db(); cur = c.cursor()
    cur.execute("SELECT role FROM board_members WHERE board_id=? AND user_id=?", (board_id, user_id))
    row = cur.fetchone()
    c.close()
    return row["role"] if row else None


def log_activity(board_id, kind, user_id=None, payload=None):
    c = db(); cur = c.cursor()
    cur.execute(
        "INSERT INTO board_activity(board_id, user_id, kind, payload) VALUES(?,?,?,?)",
        (board_id, user_id, kind, json.dumps(payload or {})),
    )
    c.commit(); c.close()


def record_presence(board_id, user_id, action, details=None):
    c = db(); cur = c.cursor()
    cur.execute(
        "INSERT INTO presence_history(board_id, user_id, action, details) VALUES(?,?,?,?)",
        (board_id, user_id, action, details),
    )
    c.commit(); c.close()


def add_notification(user_id, content, link=None):
    if not user_id:
        return
    c = db(); cur = c.cursor()
    cur.execute(
        "INSERT INTO notifications(user_id, content, link) VALUES(?,?,?)",
        (user_id, content, link),
    )
    c.commit(); c.close()


def get_user_boards(user_id):
    if not user_id:
        return []
    c = db(); cur = c.cursor()
    cur.execute(
        """
        SELECT boards.*, board_members.role
        FROM board_members
        JOIN boards ON boards.id = board_members.board_id
        WHERE board_members.user_id=?
        ORDER BY boards.created_at DESC
        """,
        (user_id,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    c.close()
    return rows


def get_notifications(user_id, limit=10, unread_only=False):
    if not user_id:
        return []
    c = db(); cur = c.cursor()
    query = "SELECT * FROM notifications WHERE user_id=?"
    if unread_only:
        query += " AND read_at IS NULL"
    query += " ORDER BY created_at DESC LIMIT ?"
    cur.execute(query, (user_id, limit))
    rows = [dict(r) for r in cur.fetchall()]
    c.close()
    return rows


def mark_notifications_read(user_id):
    c = db(); cur = c.cursor()
    cur.execute("UPDATE notifications SET read_at=datetime('now') WHERE user_id=? AND read_at IS NULL", (user_id,))
    c.commit(); c.close()


def get_board_activity(board_id, limit=25):
    c = db(); cur = c.cursor()
    cur.execute(
        """
        SELECT board_activity.*, users.username
        FROM board_activity
        LEFT JOIN users ON users.id = board_activity.user_id
        WHERE board_activity.board_id=?
        ORDER BY board_activity.id DESC
        LIMIT ?
        """,
        (board_id, limit),
    )
    rows = [dict(r) for r in cur.fetchall()]
    c.close()
    return rows


def list_group_rooms():
    c = db(); cur = c.cursor()
    cur.execute("SELECT * FROM group_rooms ORDER BY created_at DESC")
    rooms = [dict(r) for r in cur.fetchall()]
    c.close()
    return rooms


def get_group_by_slug(slug):
    c = db(); cur = c.cursor()
    cur.execute("SELECT * FROM group_rooms WHERE slug=?", (slug,))
    row = cur.fetchone()
    c.close()
    return dict(row) if row else None


def get_presence_history(board_id, limit=20):
    c = db(); cur = c.cursor()
    cur.execute(
        """
        SELECT presence_history.*, users.username
        FROM presence_history
        LEFT JOIN users ON users.id = presence_history.user_id
        WHERE board_id=?
        ORDER BY id DESC
        LIMIT ?
        """,
        (board_id, limit),
    )
    rows = [dict(r) for r in cur.fetchall()]
    c.close()
    return rows


def markdown_to_html(text):
    text = escape(text or "")
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\\1</strong>", text)
    text = re.sub(r"\*(.+?)\*", r"<em>\\1</em>", text)
    text = re.sub(r"`([^`]+)`", r"<code>\\1</code>", text)
    text = text.replace("\n", "<br>")
    return text


def format_message_row(row):
    data = dict(row)
    data["html"] = markdown_to_html(data.get("text"))
    if data.get("attachments"):
        try:
            files = json.loads(data["attachments"])
        except json.JSONDecodeError:
            files = []
        data["attachments"] = [
            {
                "name": f.get("name"),
                "url": url_for("serve_board_file", fn=f.get("stored")),
                "mime": f.get("mime"),
            }
            for f in files
        ]
    else:
        data["attachments"] = []
    if data.get("voice_path"):
        data["voice_url"] = url_for("serve_board_file", fn=data["voice_path"])
    return data


def ai_summarize(messages):
    if not messages:
        return "Keine Nachrichten zum Zusammenfassen."
    last = messages[-5:]
    authors = sorted({m.get("author") for m in last if m.get("author")})
    highlights = []
    for m in last:
        txt = (m.get("text") or "").strip()
        if not txt:
            continue
        highlights.append(f"• {m.get('author') or 'Anon'}: {txt[:120]}")
    summary = "Aktuelle Stimmen\n" + "\n".join(highlights)
    if authors:
        summary += f"\nTeilnehmer: {', '.join(authors)}"
    return summary
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
        cur.execute("INSERT OR REPLACE INTO contacts(user_id, contact_id, status) VALUES(?,?,?)",
                    (user_id, contact_id, 'pending'))
        cur.execute("INSERT OR REPLACE INTO contacts(user_id, contact_id, status) VALUES(?,?,?)",
                    (contact_id, user_id, 'incoming'))
        c.commit()
    finally:
        c.close()

# -------------------- HTTP: Core --------------------
HOME_FEATURES = [
    {
        "category": "Board Collaboration",
        "items": [
            "Realtime sticky-note sync",
            "Threaded feedback bubbles",
            "Card grouping with drag & drop",
            "Vote counters with live tally",
            "Focus timer overlay",
            "Facilitator spotlight mode",
            "Merge duplicate ideas",
            "Comment pinning",
            "Board activity heatmap",
            "Export to PDF & CSV"
        ],
    },
    {
        "category": "Messaging & Presence",
        "items": [
            "DM inbox with filters",
            "Read receipts",
            "Typing indicators",
            "Emoji reactions",
            "AI summary highlights",
            "Scheduled announcements",
            "Channel mentions",
            "Audio room hand-raise",
            "Presence radar",
            "Status presets"
        ],
    },
    {
        "category": "Board Governance",
        "items": [
            "Public / private toggles",
            "Invite-only access codes",
            "Moderator approvals",
            "Session lock timer",
            "Automated clean-up",
            "Audit log export",
            "Role badges",
            "Granular permissions",
            "Board version history",
            "Policy reminders"
        ],
    },
    {
        "category": "Customization",
        "items": [
            "Theme presets",
            "Custom gradients",
            "Accent color picker",
            "Compact layout toggle",
            "Typography packs",
            "Adaptive spacing",
            "Rounded corner controls",
            "Widget ordering",
            "Animated backgrounds",
            "Minimal focus mode"
        ],
    },
    {
        "category": "Productivity",
        "items": [
            "Agenda timeline",
            "Checklist automation",
            "Meeting notes export",
            "Sprint retro templates",
            "Follow-up reminders",
            "Recurring standups",
            "Keyboard shortcut map",
            "Calendar sync",
            "Task owner handoff",
            "Priority labels"
        ],
    },
    {
        "category": "Analytics",
        "items": [
            "Sentiment tracker",
            "Participation scores",
            "Time-in-stage chart",
            "Top contributor list",
            "Idea velocity",
            "Engagement timeline",
            "Export to BI",
            "Heatmap playback",
            "Goal completion",
            "Automated insights"
        ],
    },
    {
        "category": "Integrations",
        "items": [
            "Slack sync",
            "Teams notifications",
            "Jira issue bridge",
            "Linear task sync",
            "Miro import",
            "Figma preview",
            "Zapier automation",
            "Google Drive embed",
            "Outlook calendar hook",
            "Webhooks playground"
        ],
    },
    {
        "category": "Security",
        "items": [
            "Two-factor login",
            "Session device list",
            "Download watermarking",
            "Link expiry",
            "IP allowlist",
            "Compliance center",
            "Encrypted attachments",
            "SOC2 reporting",
            "Moderator escalation",
            "Custom password rules"
        ],
    },
    {
        "category": "Support & Guidance",
        "items": [
            "Interactive onboarding",
            "Template walkthroughs",
            "Live facilitation tips",
            "Contextual tooltips",
            "Best practice library",
            "Community gallery",
            "Video tutorials",
            "Release notes feed",
            "In-app surveys",
            "Status page link"
        ],
    },
    {
        "category": "Mobile & Accessibility",
        "items": [
            "Responsive touch UI",
            "Offline note taking",
            "Voice dictation",
            "High contrast theme",
            "Screen reader labels",
            "Captioned media",
            "Gesture shortcuts",
            "Dynamic font sizing",
            "Haptic feedback cues",
            "Low-bandwidth mode"
        ],
    },
]

HOME_THEMES = [
    {"id": "ocean", "label": "Ocean"},
    {"id": "sunrise", "label": "Sunrise"},
    {"id": "midnight", "label": "Midnight"},
    {"id": "forest", "label": "Forest"},
    {"id": "aurora", "label": "Aurora"},
    {"id": "sand", "label": "Sahara"},
]

BOARD_TEMPLATES = {
    "retro": {
        "title": "Sprint Retrospective",
        "theme": "mint",
        "cards": [
            ("Alice", "What went well this sprint?", "🟢"),
            ("Bob", "What slowed us down?", "❓"),
            ("Carol", "Action items for next sprint", "⚡️"),
        ],
    },
    "kanban": {
        "title": "Kanban Standup",
        "theme": "violet",
        "cards": [
            ("Team", "Todo", "🟡"),
            ("Team", "In Progress", "⚡️"),
            ("Team", "Blocked", "🔴"),
        ],
    },
    "brainstorm": {
        "title": "Brainstorm Board",
        "theme": "sunset",
        "cards": [
            ("Dana", "Wild ideas", "✨"),
            ("Eli", "Opportunities", "🟢"),
            ("Fran", "Risks", "🔴"),
        ],
    },
}

PROFILE_COLORS = [
    "#3b82f6", "#a855f7", "#ec4899", "#14b8a6", "#f59e0b", "#ef4444",
    "#22c55e", "#6366f1", "#0ea5e9", "#94a3b8"
]

USER_STATUS_OPTIONS = ["online", "away", "busy", "focus", "offline"]
USER_BADGE_OPTIONS = ["Member", "Admin", "VIP", "Developer"]

ROLE_POWER = {"viewer": 0, "member": 1, "moderator": 2, "owner": 3}


def has_role(role, minimum):
    return ROLE_POWER.get(role or "member", 0) >= ROLE_POWER.get(minimum, 0)


@app.get("/")
def home():
    user = current_user()
    boards = get_user_boards(user["id"]) if user else []
    notifications = get_notifications(user["id"], limit=10) if user else []
    return render_template(
        "index.html",
        user=user,
        features=HOME_FEATURES,
        board_themes=HOME_THEMES,
        profile_colors=PROFILE_COLORS,
        templates=BOARD_TEMPLATES,
        boards=boards,
        notifications=notifications,
        group_rooms=list_group_rooms(),
    )

@app.post("/new")
def new_board():
    tpl = request.form.get("template") or None
    tpl = tpl if tpl in BOARD_TEMPLATES else None
    owner = current_user()
    code = create_board(owner_id=owner["id"] if owner else None, template_key=tpl)
    title = (request.form.get("title") or "").strip()
    accent = (request.form.get("accent") or "").strip()
    c = db(); cur = c.cursor()
    updates = []
    params = []
    if title:
        updates.append("title=?"); params.append(title[:80])
    if accent and re.match(r"^#?[0-9a-fA-F]{6}$", accent):
        color = accent if accent.startswith("#") else f"#{accent}"
        updates.append("accent_color=?"); params.append(color)
    if owner:
        updates.append("owner_id=?"); params.append(owner["id"])
    if updates:
        params.append(code)
        cur.execute(f"UPDATE boards SET {', '.join(updates)} WHERE code=?", params)
    c.commit(); c.close()
    return redirect(url_for("board_page", code=code))

@app.get("/b/<code>")
def board_page(code):
    b = get_board_by_code(code)
    if not b:
        return "Board not found", 404
    members = get_board_members(b["id"])
    activity = get_board_activity(b["id"], limit=40)
    return render_template(
        "board.html",
        code=code,
        theme=b["theme"],
        title=b["title"],
        board=b,
        members=members,
        activity=activity,
    )

# -------------------- HTTP: Auth --------------------
@app.get("/auth")
def login_page():
    if current_user():
        return redirect(url_for("home"))
    return render_template(
        "auth.html",
        login_error=None,
        register_error=None,
        login_username="",
        register_username="",
        register_email="",
    )

@app.post("/auth/register")
def register_post():
    username = (request.form.get("username") or "").strip().lower()[:24]
    email = (request.form.get("email") or "").strip()
    password = (request.form.get("password") or "")[:128]
    if not username or not password:
        return "Missing username or password", 400
    email_db = email or None
    pass_hash = generate_password_hash(password)
    c = db(); cur = c.cursor()
    try:
        cur.execute("INSERT INTO users(username, email, pass_hash) VALUES(?,?,?)",
                    (username, email_db, pass_hash))
        c.commit()
        cur.execute("SELECT id FROM users WHERE username=?", (username,))
        uid = cur.fetchone()["id"]
        session["uid"] = uid
        return redirect(url_for("home"))
    except sqlite3.IntegrityError:
        c.rollback()
        msg = "Benutzername ist bereits vergeben."
        cur.execute("SELECT 1 FROM users WHERE email=?", (email_db,))
        if email_db and cur.fetchone():
            msg = "E-Mail wird bereits verwendet."
        return render_template(
            "auth.html",
            login_error=None,
            register_error=msg,
            login_username="",
            register_username=username,
            register_email=email,
        ), 400
    finally:
        c.close()

@app.post("/auth/login")
def login_post():
    username = (request.form.get("username") or "").strip().lower()
    password = (request.form.get("password") or "")
    u = get_user_by_username(username)
    if not u or not check_password_hash(u["pass_hash"], password):
        return render_template(
            "auth.html",
            login_error="Ungültige Kombination aus Username und Passwort.",
            register_error=None,
            login_username=username,
            register_username="",
            register_email="",
        ), 400
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
    return render_template(
        "settings.html",
        u=current_user(),
        status_options=USER_STATUS_OPTIONS,
        badge_options=USER_BADGE_OPTIONS,
        board_themes=HOME_THEMES,
        profile_colors=PROFILE_COLORS,
    )

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


@app.post("/settings/profile")
@login_required
def update_profile_settings():
    status = (request.form.get("status") or "offline").lower()
    badge = request.form.get("badge") or "Member"
    theme = request.form.get("theme") or "ocean"
    accent = (request.form.get("accent") or "#38bdf8").strip()
    if status not in USER_STATUS_OPTIONS:
        status = "offline"
    if badge not in USER_BADGE_OPTIONS:
        badge = "Member"
    if theme not in {t["id"] for t in HOME_THEMES}:
        theme = "ocean"
    if not re.match(r"^#?[0-9a-fA-F]{6}$", accent):
        accent = "#38bdf8"
    if not accent.startswith("#"):
        accent = f"#{accent}"
    uid = session["uid"]
    c = db(); cur = c.cursor()
    cur.execute(
        "UPDATE users SET status=?, badge=?, profile_theme=?, accent_color=? WHERE id=?",
        (status, badge, theme, accent, uid),
    )
    c.commit(); c.close()
    return redirect(url_for("settings_page"))


@app.get("/u/avatars/<path:fn>")
def serve_avatar(fn):
    return send_from_directory(AVATAR_DIR, fn, as_attachment=False, download_name=fn)


@app.get("/u/files/<path:fn>")
def serve_board_file(fn):
    return send_from_directory(BOARD_FILES_DIR, fn, as_attachment=False, download_name=fn)


@app.get("/u/voices/<path:fn>")
def serve_voice_file(fn):
    return send_from_directory(VOICE_DIR, fn, as_attachment=False, download_name=fn)

# -------------------- HTTP: Contacts --------------------
@app.get("/contacts")
@login_required
def contacts_page():
    u = current_user()
    c = db(); cur = c.cursor()
    cur.execute("""SELECT users.id, users.username, users.avatar, contacts.status, contacts.created_at
                   FROM contacts JOIN users ON users.id = contacts.contact_id
                   WHERE contacts.user_id=? ORDER BY contacts.created_at DESC""", (u["id"],))
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
    add_notification(other["id"], f"{u['username']} hat dir eine Kontaktanfrage gesendet.", url_for("contacts_page"))
    return redirect(url_for("contacts_page"))


@app.post("/contacts/respond")
@login_required
def contacts_respond():
    u = current_user()
    other_id = int(request.form.get("user_id") or 0)
    action = request.form.get("action")
    if not other_id or action not in {"accept", "decline"}:
        return redirect(url_for("contacts_page"))
    c = db(); cur = c.cursor()
    if action == "accept":
        cur.execute("UPDATE contacts SET status='accepted' WHERE user_id=? AND contact_id=?", (u["id"], other_id))
        cur.execute("UPDATE contacts SET status='accepted' WHERE user_id=? AND contact_id=?", (other_id, u["id"]))
        add_notification(other_id, f"{u['username']} hat deine Kontaktanfrage akzeptiert.", url_for("contacts_page"))
    else:
        cur.execute("DELETE FROM contacts WHERE (user_id=? AND contact_id=?) OR (user_id=? AND contact_id=?)",
                    (u["id"], other_id, other_id, u["id"]))
    c.commit(); c.close()
    return redirect(url_for("contacts_page"))

# -------------------- HTTP: Direct Messages --------------------
@app.get("/dm/<username>")
@login_required
def dm_page(username):
    me = current_user()
    other = get_user_by_username(username.lower())
    if not other: return "User not found", 404
    c = db(); cur = c.cursor()
    cur.execute("""SELECT id, sender_id, receiver_id, text, reply_to, voice_path, edited_at, deleted_at, read_at, created_at
                   FROM dm_messages
                   WHERE (sender_id=? AND receiver_id=?) OR (sender_id=? AND receiver_id=?)
                   ORDER BY id DESC LIMIT 100""",
                (me["id"], other["id"], other["id"], me["id"]))
    msgs = [dict(x) for x in cur.fetchall()]
    cur.execute("UPDATE dm_messages SET read_at=datetime('now') WHERE receiver_id=? AND sender_id=? AND read_at IS NULL",
                (me["id"], other["id"]))
    ids = [m["id"] for m in msgs if m["receiver_id"] == me["id"]]
    if ids:
        cur.executemany("INSERT OR IGNORE INTO message_reads(message_id, user_id) VALUES(?,?)",
                        [(mid, me["id"]) for mid in ids])
    reactions = {}
    if msgs:
        placeholders = ",".join(["?"] * len(msgs))
        cur.execute(
            f"SELECT message_id, emoji, COUNT(*) as cnt FROM dm_reactions WHERE message_id IN ({placeholders}) GROUP BY message_id, emoji",
            [m["id"] for m in msgs],
        )
        for row in cur.fetchall():
            reactions.setdefault(row["message_id"], []).append({"emoji": row["emoji"], "count": row["cnt"]})
    c.commit(); c.close()
    for m in msgs:
        m["reactions"] = reactions.get(m["id"], [])
        if m.get("voice_path"):
            m["voice_url"] = url_for("serve_voice_file", fn=m["voice_path"])
    return render_template("dm.html", me=me, other=dict(other), messages=list(reversed(msgs)))


@app.get("/g/<slug>")
@login_required
def group_page(slug):
    me = current_user()
    room = get_group_by_slug(slug)
    if not room:
        return "Group not found", 404
    c = db(); cur = c.cursor()
    cur.execute("SELECT * FROM group_messages WHERE room_id=? ORDER BY id DESC LIMIT 120", (room["id"],))
    msgs = [dict(x) for x in cur.fetchall()]
    c.close()
    return render_template("group.html", room=room, me=me, messages=list(reversed(msgs)))

# -------------------- HTTP: Mini APIs --------------------
@app.get("/api/me")
def api_me():
    u = current_user()
    return jsonify(u or {})

@app.get("/discover")
def discover_page():
    c = db(); cur = c.cursor()
    cur.execute(
        """
        SELECT boards.id, boards.code, boards.title, boards.theme, boards.created_at,
               COUNT(DISTINCT cards.id) AS card_count,
               COUNT(DISTINCT messages.id) AS message_count
        FROM boards
        LEFT JOIN cards ON cards.board_id = boards.id
        LEFT JOIN messages ON messages.board_id = boards.id
        GROUP BY boards.id
        ORDER BY boards.created_at DESC
        LIMIT 12
        """
    )
    rows = [dict(x) for x in cur.fetchall()]
    c.close()
    return render_template("discover.html", boards=rows)

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


@app.post("/api/board/<code>/files")
@login_required
def upload_board_asset(code):
    board = get_board_by_code(code)
    if not board:
        abort(404)
    f = request.files.get("file")
    if not f:
        return "missing file", 400
    ext = (os.path.splitext(f.filename or "")[1] or "").lower()
    if ext not in ALLOWED_FILE_TYPES:
        return "unsupported", 400
    stored = f"{code}_{int(datetime.utcnow().timestamp())}_{secrets.token_hex(4)}{ext}"
    path = os.path.join(BOARD_FILES_DIR, stored)
    f.save(path)
    url = url_for("serve_board_file", fn=stored, _external=False)
    return jsonify({
        "name": f.filename,
        "url": url,
        "stored": stored,
        "mime": ALLOWED_FILE_TYPES[ext],
    })


@app.post("/api/dm/upload")
@login_required
def upload_dm_voice():
    f = request.files.get("file")
    if not f:
        return "missing file", 400
    original = secure_filename(f.filename or "voice.ogg")
    ext = (os.path.splitext(original)[1] or "").lower()
    if ext not in AUDIO_EXTENSIONS:
        return "unsupported", 400
    stored = f"dm_{session['uid']}_{int(datetime.utcnow().timestamp())}_{secrets.token_hex(4)}{ext}"
    path = os.path.join(VOICE_DIR, stored)
    f.save(path)
    mime = ALLOWED_FILE_TYPES.get(ext, "audio/ogg")
    return jsonify({
        "name": f.filename,
        "url": url_for("serve_voice_file", fn=stored, _external=False),
        "stored": stored,
        "mime": mime,
    })


@app.get("/api/board/<code>/activity")
def api_board_activity(code):
    board = get_board_by_code(code)
    if not board:
        abort(404)
    return jsonify(get_board_activity(board["id"], limit=50))


@app.get("/api/board/<code>/search")
def api_board_search(code):
    board = get_board_by_code(code)
    if not board:
        abort(404)
    term = (request.args.get("q") or "").strip()
    channel = (request.args.get("channel") or "").strip()
    c = db(); cur = c.cursor()
    cards = []
    messages = []
    if term:
        like = f"%{term}%"
        cur.execute(
            "SELECT id, author, text, tag, votes, created_at FROM cards WHERE board_id=? AND text LIKE ? ORDER BY created_at DESC LIMIT 20",
            (board["id"], like),
        )
        cards = [dict(r) for r in cur.fetchall()]
        params = [board["id"], like]
        sql = "SELECT id, author, text, channel, reply_to, pinned, created_at FROM messages WHERE board_id=? AND text LIKE ?"
        if channel:
            sql += " AND channel=?"; params.append(channel)
        sql += " ORDER BY created_at DESC LIMIT 20"
        cur.execute(sql, params)
        messages = [dict(r) for r in cur.fetchall()]
    c.close()
    return jsonify({"cards": cards, "messages": messages})


@app.post("/api/board/<code>/summary")
def api_board_summary(code):
    board = get_board_by_code(code)
    if not board:
        abort(404)
    c = db(); cur = c.cursor()
    cur.execute(
        "SELECT author, text FROM messages WHERE board_id=? AND deleted_at IS NULL ORDER BY id ASC",
        (board["id"],),
    )
    msgs = [dict(r) for r in cur.fetchall()]
    c.close()
    return jsonify({"summary": ai_summarize(msgs)})


@app.post("/api/board/<code>/cards/reorder")
def api_board_reorder(code):
    board = get_board_by_code(code)
    if not board:
        abort(404)
    order = (request.json or {}).get("order") or []
    c = db(); cur = c.cursor()
    for idx, card_id in enumerate(order):
        cur.execute("UPDATE cards SET order_index=? WHERE id=? AND board_id=?", (idx, card_id, board["id"]))
    c.commit(); c.close()
    return jsonify({"ok": True})


@app.post("/api/board/<code>/invite")
@login_required
def create_board_invite(code):
    board = get_board_by_code(code)
    if not board:
        abort(404)
    me = current_user()
    role = get_member_role(board["id"], me["id"])
    if not has_role(role, "moderator"):
        abort(403)
    token = secrets.token_urlsafe(8)
    expires = datetime.utcnow() + timedelta(days=7)
    c = db(); cur = c.cursor()
    cur.execute(
        "INSERT INTO board_invites(board_id, token, expires_at, created_by) VALUES(?,?,?,?)",
        (board["id"], token, expires.isoformat(), me["id"]),
    )
    c.commit(); c.close()
    link = url_for("redeem_invite", token=token, _external=True)
    return jsonify({"token": token, "link": link, "expires": expires.isoformat()})


@app.get("/i/<token>")
@login_required
def redeem_invite(token):
    me = current_user()
    c = db(); cur = c.cursor()
    cur.execute(
        "SELECT board_invites.*, boards.code FROM board_invites JOIN boards ON boards.id = board_invites.board_id WHERE token=?",
        (token,),
    )
    invite = cur.fetchone()
    if not invite:
        c.close(); return "Invite ungültig", 404
    expires = invite["expires_at"]
    if expires and datetime.utcnow() > datetime.fromisoformat(expires):
        c.close(); return "Einladung abgelaufen", 410
    ensure_board_member(invite["board_id"], me["id"], role="member")
    if invite["created_by"]:
        cur.execute("UPDATE contacts SET status='accepted' WHERE user_id=? AND contact_id=?", (me["id"], invite["created_by"]))
        cur.execute("UPDATE contacts SET status='accepted' WHERE user_id=? AND contact_id=?", (invite["created_by"], me["id"]))
    c.commit(); c.close()
    return redirect(url_for("board_page", code=invite["code"]))

# -------------------- Realtime: Presence/Boards --------------------
presence = {}        # code -> set(socket_ids)
presence_names = {}  # code -> {sid: name}
ALLOWED_THEMES = {"ocean", "mint", "sunset", "violet", "slate"}
typing_state = {}
cursor_positions = {}
group_typing_state = {}

def board_room(code) -> str:
    return f"board_{code}"

# DM online mapping
user_sids = {}  # user_id -> set(sids)
dm_typing_state = {}

@socketio.on("connect")
def on_connect():
    # Map Flask-Session-User zu dieser SID
    u = current_user()
    if u:
        user_sids.setdefault(u["id"], set()).add(request.sid)

@socketio.on("disconnect")
def ws_disconnect():
    u = current_user()
    # Boards: aus allen Präsenzlisten entfernen
    for code, sids in list(presence.items()):
        if request.sid in sids:
            sids.remove(request.sid)
            presence_names.get(code, {}).pop(request.sid, None)
            typing_state.get(code, {}).pop(request.sid, None)
            cursor_positions.get(code, {}).pop(request.sid, None)
            b = get_board_by_code(code)
            if b and u:
                record_presence(b["id"], u["id"], "leave")
            socketio.emit("presence", {
                "count": len(sids),
                "names": sorted([n for n in presence_names.get(code, {}).values() if n])
            }, room=board_room(code))
            socketio.emit("typing", {
                "code": code,
                "authors": [n for n in typing_state.get(code, {}).values() if n]
            }, room=board_room(code))
            socketio.emit("cursors", list(cursor_positions.get(code, {}).values()), room=board_room(code))
    # DMs: online mapping abbauen
    for uid, sids in list(user_sids.items()):
        if request.sid in sids:
            sids.remove(request.sid)
            if not sids:
                user_sids.pop(uid, None)
    if u:
        for room in list(dm_typing_state.keys()):
            dm_typing_state[room].discard(u["username"])
            if not dm_typing_state[room]:
                dm_typing_state.pop(room, None)
        for slug in list(group_typing_state.keys()):
            group_typing_state[slug].discard(u["username"])
            if not group_typing_state[slug]:
                group_typing_state.pop(slug, None)

@socketio.on("join_board")
def join_board(data):
    code = (data or {}).get("code")
    client_name = ((data or {}).get("clientName") or "Anon")[:24]

    b = get_board_by_code(code)
    if not code or not b:
        emit("error", {"message": "Invalid board code"})
        return

    me = current_user()
    if me:
        ensure_board_member(b["id"], me["id"])
        record_presence(b["id"], me["id"], "join", client_name)

    room = board_room(code)
    join_room(room)

    presence.setdefault(code, set()).add(request.sid)
    presence_names.setdefault(code, {})[request.sid] = client_name

    c = db(); cur = c.cursor()
    cur.execute("""
        SELECT cards.* FROM cards
        JOIN boards ON boards.id = cards.board_id
        WHERE boards.code = ?
        ORDER BY COALESCE(cards.order_index, cards.id) ASC
    """, (code,))
    cards = [dict(x) for x in cur.fetchall()]

    cur.execute("""
        SELECT messages.*
        FROM messages
        JOIN boards ON boards.id = messages.board_id
        WHERE boards.code = ?
        ORDER BY messages.id DESC
        LIMIT 200
    """, (code,))
    messages = [dict(x) for x in cur.fetchall()]
    message_ids = [m["id"] for m in messages]
    reactions = {}
    if message_ids:
        placeholders = ",".join(["?"] * len(message_ids))
        cur.execute(
            f"SELECT message_id, emoji, COUNT(*) as cnt FROM message_reactions WHERE message_id IN ({placeholders}) GROUP BY message_id, emoji",
            message_ids,
        )
        for row in cur.fetchall():
            reactions.setdefault(row["message_id"], []).append({"emoji": row["emoji"], "count": row["cnt"]})
    c.close()

    for card in cards:
        if card.get("attachment_path"):
            card["attachment_url"] = url_for("serve_board_file", fn=card["attachment_path"])

    formatted_messages = []
    channels = set()
    for m in reversed(messages):
        if m.get("deleted_at"):
            continue
        m["reactions"] = reactions.get(m["id"], [])
        formatted = format_message_row(m)
        formatted["reactions"] = m["reactions"]
        channels.add(formatted.get("channel") or "general")
        formatted_messages.append(formatted)

    emit("board_state", {
        "cards": cards,
        "messages": formatted_messages,
        "theme": b["theme"],
        "title": b["title"],
        "board": {
            "accent": b.get("accent_color"),
            "background": b.get("background_anim"),
            "code": code,
        },
        "members": get_board_members(b["id"]),
        "activity": get_board_activity(b["id"], limit=25),
        "presenceHistory": get_presence_history(b["id"], limit=25),
        "channels": sorted(channels) or ["general"],
        "role": get_member_role(b["id"], me["id"]) if me else "viewer",
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
    attachment = (data or {}).get("attachment")
    if not code or not text:
        emit("error", {"message": "Missing code or text"})
        return
    b = get_board_by_code(code)
    if not b:
        emit("error", {"message": "Board not found"})
        return

    c = db(); cur = c.cursor()
    cur.execute("SELECT COALESCE(MAX(order_index), -1) + 1 FROM cards WHERE board_id=?", (b["id"],))
    next_order = cur.fetchone()[0] or 0
    cur.execute("INSERT INTO cards(board_id,author,text,tag,order_index,attachment_path) VALUES(?,?,?,?,?,?)",
                (b["id"], author, text, tag, next_order, attachment))
    cid = cur.lastrowid
    cur.execute("SELECT * FROM cards WHERE id=?", (cid,))
    card = dict(cur.fetchone())
    c.commit(); c.close()

    if card.get("attachment_path"):
        card["attachment_url"] = url_for("serve_board_file", fn=card["attachment_path"])
    log_activity(b["id"], "card_created", session.get("uid"), {"card_id": cid, "text": text[:120]})

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
    log_activity(b["id"], "card_voted", session.get("uid"), {"card_id": cid})
    socketio.emit("card_updated", card, room=board_room(code))

@socketio.on("send_chat")
def send_chat_ev(data):
    code = (data or {}).get("code")
    author = ((data or {}).get("author") or "").strip()[:32]
    text = ((data or {}).get("text") or "").strip()[:500]
    channel = ((data or {}).get("channel") or "general").strip()[:32]
    reply_to = (data or {}).get("replyTo")
    attachments = (data or {}).get("attachments") or []
    voice_path = (data or {}).get("voice")
    if not code or not text:
        emit("error", {"message": "Missing code or text"})
        return
    b = get_board_by_code(code)
    if not b:
        emit("error", {"message": "Board not found"})
        return

    c = db(); cur = c.cursor()
    attachments_json = json.dumps(attachments) if attachments else None
    cur.execute("INSERT INTO messages(board_id,author,text,channel,reply_to,attachments,voice_path) VALUES(?,?,?,?,?,?,?)",
                (b["id"], author, text, channel or "general", reply_to, attachments_json, voice_path))
    mid = cur.lastrowid
    cur.execute("SELECT * FROM messages WHERE id=?", (mid,))
    msg = dict(cur.fetchone())
    c.commit(); c.close()

    msg = format_message_row(msg)
    msg["reactions"] = []
    socketio.emit("chat_added", msg, room=board_room(code))
    log_activity(b["id"], "message_posted", session.get("uid"), {"message_id": mid, "channel": channel})


@socketio.on("typing")
def typing_event(data):
    code = (data or {}).get("code")
    author = ((data or {}).get("author") or "Anon")[:32]
    if not code:
        return
    typing_state.setdefault(code, {})[request.sid] = author
    socketio.emit(
        "typing",
        {"code": code, "authors": [n for n in typing_state.get(code, {}).values() if n]},
        room=board_room(code),
        include_self=False,
    )


@socketio.on("stop_typing")
def typing_stop(data):
    code = (data or {}).get("code")
    if not code:
        return
    typing_state.get(code, {}).pop(request.sid, None)
    socketio.emit(
        "typing",
        {"code": code, "authors": [n for n in typing_state.get(code, {}).values() if n]},
        room=board_room(code),
        include_self=False,
    )


@socketio.on("cursor_move")
def cursor_move(data):
    code = (data or {}).get("code")
    author = ((data or {}).get("author") or "Anon")[:32]
    pos = (data or {}).get("pos") or {}
    if not code:
        return
    cursor_positions.setdefault(code, {})[request.sid] = {
        "author": author,
        "x": pos.get("x"),
        "y": pos.get("y"),
        "color": (data or {}).get("color"),
    }
    socketio.emit("cursors", list(cursor_positions.get(code, {}).values()), room=board_room(code), include_self=False)


@socketio.on("chat_react")
def chat_react(data):
    me = current_user()
    if not me:
        emit("error", {"message": "auth required"}); return
    code = (data or {}).get("code")
    mid = (data or {}).get("messageId")
    emoji = ((data or {}).get("emoji") or "").strip()[:16]
    if not mid or not emoji:
        emit("error", {"message": "missing reaction data"}); return
    c = db(); cur = c.cursor()
    cur.execute("SELECT board_id FROM messages WHERE id=?", (mid,))
    row = cur.fetchone()
    if not row:
        c.close(); emit("error", {"message": "message missing"}); return
    board_id = row["board_id"]
    if not code:
        cur.execute("SELECT code FROM boards WHERE id=?", (board_id,))
        code_row = cur.fetchone()
        code = code_row["code"] if code_row else None
    cur.execute("SELECT 1 FROM message_reactions WHERE message_id=? AND user_id=? AND emoji=?", (mid, me["id"], emoji))
    if cur.fetchone():
        cur.execute("DELETE FROM message_reactions WHERE message_id=? AND user_id=? AND emoji=?", (mid, me["id"], emoji))
    else:
        cur.execute("INSERT INTO message_reactions(message_id, user_id, emoji) VALUES(?,?,?)", (mid, me["id"], emoji))
    cur.execute("SELECT emoji, COUNT(*) as cnt FROM message_reactions WHERE message_id=? GROUP BY emoji", (mid,))
    reactions = [{"emoji": r["emoji"], "count": r["cnt"]} for r in cur.fetchall()]
    c.commit(); c.close()
    if code:
        socketio.emit("chat_reactions", {"messageId": mid, "reactions": reactions}, room=board_room(code))
    log_activity(board_id, "message_reaction", me["id"], {"message_id": mid, "emoji": emoji})


@socketio.on("chat_pin")
def chat_pin(data):
    me = current_user()
    if not me:
        emit("error", {"message": "auth required"}); return
    code = (data or {}).get("code")
    mid = (data or {}).get("messageId")
    if not code or not mid:
        emit("error", {"message": "missing data"}); return
    board = get_board_by_code(code)
    if not board:
        emit("error", {"message": "board missing"}); return
    role = get_member_role(board["id"], me["id"])
    if not has_role(role, "moderator"):
        emit("error", {"message": "no permission"}); return
    c = db(); cur = c.cursor()
    cur.execute("UPDATE messages SET pinned=CASE WHEN pinned=1 THEN 0 ELSE 1 END WHERE id=? AND board_id=?", (mid, board["id"]))
    cur.execute("SELECT * FROM messages WHERE id=?", (mid,))
    row = cur.fetchone()
    c.commit(); c.close()
    if not row:
        return
    formatted = format_message_row(row)
    formatted["reactions"] = []
    socketio.emit("chat_pinned", {"message": formatted}, room=board_room(code))
    log_activity(board["id"], "message_pin", me["id"], {"message_id": mid, "pinned": formatted.get("pinned")})


@socketio.on("chat_edit")
def chat_edit(data):
    me = current_user()
    if not me:
        emit("error", {"message": "auth required"}); return
    code = (data or {}).get("code")
    mid = (data or {}).get("messageId")
    new_text = ((data or {}).get("text") or "").strip()[:500]
    if not code or not mid or not new_text:
        emit("error", {"message": "missing data"}); return
    board = get_board_by_code(code)
    if not board:
        emit("error", {"message": "board missing"}); return
    c = db(); cur = c.cursor()
    cur.execute("SELECT author FROM messages WHERE id=? AND board_id=?", (mid, board["id"]))
    row = cur.fetchone()
    if not row:
        c.close(); emit("error", {"message": "message missing"}); return
    role = get_member_role(board["id"], me["id"])
    if row["author"] != me["username"] and not has_role(role, "moderator"):
        c.close(); emit("error", {"message": "no permission"}); return
    cur.execute("UPDATE messages SET text=?, edited_at=datetime('now') WHERE id=?", (new_text, mid))
    cur.execute("SELECT * FROM messages WHERE id=?", (mid,))
    updated = cur.fetchone()
    c.commit(); c.close()
    formatted = format_message_row(updated)
    formatted["reactions"] = []
    socketio.emit("chat_updated", formatted, room=board_room(code))
    log_activity(board["id"], "message_edit", me["id"], {"message_id": mid})


@socketio.on("chat_delete")
def chat_delete(data):
    me = current_user()
    if not me:
        emit("error", {"message": "auth required"}); return
    code = (data or {}).get("code")
    mid = (data or {}).get("messageId")
    if not code or not mid:
        emit("error", {"message": "missing data"}); return
    board = get_board_by_code(code)
    if not board:
        emit("error", {"message": "board missing"}); return
    c = db(); cur = c.cursor()
    cur.execute("SELECT author FROM messages WHERE id=? AND board_id=?", (mid, board["id"]))
    row = cur.fetchone()
    if not row:
        c.close(); emit("error", {"message": "message missing"}); return
    role = get_member_role(board["id"], me["id"])
    if row["author"] != me["username"] and not has_role(role, "moderator"):
        c.close(); emit("error", {"message": "no permission"}); return
    cur.execute("UPDATE messages SET deleted_at=datetime('now') WHERE id=?", (mid,))
    c.commit(); c.close()
    socketio.emit("chat_deleted", {"id": mid}, room=board_room(code))
    log_activity(board["id"], "message_delete", me["id"], {"message_id": mid})

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

@socketio.on("set_title")
def set_title_ev(data):
    code = (data or {}).get("code")
    title = ((data or {}).get("title") or "").strip()
    if not code:
        emit("error", {"message": "Missing board code"}); return
    b = get_board_by_code(code)
    if not b:
        emit("error", {"message": "Board not found"}); return
    title = title[:80] or "Team Board"
    c = db(); cur = c.cursor()
    cur.execute("UPDATE boards SET title=? WHERE id=?", (title, b["id"]))
    c.commit(); c.close()
    socketio.emit("title_changed", {"title": title}, room=board_room(code))

# -------------------- Realtime: Direct Messages --------------------
def dm_room(a_id, b_id):
    lo, hi = sorted([int(a_id), int(b_id)])
    return f"dm_{lo}_{hi}"


def group_socket_room(slug):
    return f"group_{slug}"

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
    reply_to = (data or {}).get("replyTo")
    voice_path = (data or {}).get("voice")
    if not to_name or (not text and not voice_path):
        emit("error", {"message": "missing content"}); return
    other = get_user_by_username(to_name.lower())
    if not other:
        emit("error", {"message": "other not found"}); return

    c = db(); cur = c.cursor()
    cur.execute("INSERT INTO dm_messages(sender_id,receiver_id,text,reply_to,voice_path) VALUES(?,?,?,?,?)",
                (me["id"], other["id"], text, reply_to, voice_path))
    mid = cur.lastrowid
    cur.execute("SELECT * FROM dm_messages WHERE id=?", (mid,))
    msg = dict(cur.fetchone())
    c.commit(); c.close()

    payload = {
        "id": msg["id"],
        "sender_id": me["id"],
        "receiver_id": other["id"],
        "sender": current_user()["username"],
        "text": msg["text"],
        "created_at": msg["created_at"],
        "reply_to": msg.get("reply_to"),
        "voice_url": url_for("serve_voice_file", fn=voice_path) if voice_path else None,
        "edited_at": msg.get("edited_at"),
    }
    socketio.emit("dm_new", payload, room=dm_room(me["id"], other["id"]))


@socketio.on("dm_typing")
def dm_typing(data):
    me = current_user()
    if not me:
        return
    other_name = (data or {}).get("to")
    other = get_user_by_username((other_name or "").lower())
    if not other:
        return
    room = dm_room(me["id"], other["id"])
    dm_typing_state.setdefault(room, set()).add(me["username"])
    socketio.emit("dm_typing", {"authors": list(dm_typing_state[room])}, room=room, include_self=False)


@socketio.on("dm_stop_typing")
def dm_stop_typing(data):
    me = current_user()
    if not me:
        return
    other_name = (data or {}).get("to")
    other = get_user_by_username((other_name or "").lower())
    if not other:
        return
    room = dm_room(me["id"], other["id"])
    dm_typing_state.get(room, set()).discard(me["username"])
    socketio.emit("dm_typing", {"authors": list(dm_typing_state.get(room, set()))}, room=room, include_self=False)


@socketio.on("dm_react")
def dm_react(data):
    me = current_user()
    if not me:
        emit("error", {"message": "auth required"}); return
    mid = (data or {}).get("messageId")
    emoji = ((data or {}).get("emoji") or "").strip()[:16]
    if not mid or not emoji:
        emit("error", {"message": "missing data"}); return
    c = db(); cur = c.cursor()
    cur.execute("SELECT sender_id, receiver_id FROM dm_messages WHERE id=?", (mid,))
    row = cur.fetchone()
    if not row:
        c.close(); emit("error", {"message": "missing message"}); return
    room = dm_room(row["sender_id"], row["receiver_id"])
    cur.execute("SELECT 1 FROM dm_reactions WHERE message_id=? AND user_id=? AND emoji=?", (mid, me["id"], emoji))
    if cur.fetchone():
        cur.execute("DELETE FROM dm_reactions WHERE message_id=? AND user_id=? AND emoji=?", (mid, me["id"], emoji))
    else:
        cur.execute("INSERT INTO dm_reactions(message_id, user_id, emoji) VALUES(?,?,?)", (mid, me["id"], emoji))
    cur.execute("SELECT emoji, COUNT(*) as cnt FROM dm_reactions WHERE message_id=? GROUP BY emoji", (mid,))
    reactions = [{"emoji": r["emoji"], "count": r["cnt"]} for r in cur.fetchall()]
    c.commit(); c.close()
    socketio.emit("dm_reactions", {"messageId": mid, "reactions": reactions}, room=room)


@socketio.on("dm_edit")
def dm_edit(data):
    me = current_user()
    if not me:
        emit("error", {"message": "auth required"}); return
    mid = (data or {}).get("messageId")
    new_text = ((data or {}).get("text") or "").strip()[:1000]
    if not mid or not new_text:
        emit("error", {"message": "missing data"}); return
    c = db(); cur = c.cursor()
    cur.execute("SELECT sender_id, receiver_id FROM dm_messages WHERE id=?", (mid,))
    row = cur.fetchone()
    if not row:
        c.close(); emit("error", {"message": "missing message"}); return
    if row["sender_id"] != me["id"]:
        c.close(); emit("error", {"message": "no permission"}); return
    cur.execute("UPDATE dm_messages SET text=?, edited_at=datetime('now') WHERE id=?", (new_text, mid))
    cur.execute("SELECT * FROM dm_messages WHERE id=?", (mid,))
    updated = dict(cur.fetchone())
    if updated.get("voice_path"):
        updated["voice_url"] = url_for("serve_voice_file", fn=updated["voice_path"])
    c.commit(); c.close()
    room = dm_room(updated["sender_id"], updated["receiver_id"])
    socketio.emit("dm_updated", updated, room=room)


@socketio.on("dm_delete")
def dm_delete(data):
    me = current_user()
    if not me:
        emit("error", {"message": "auth required"}); return
    mid = (data or {}).get("messageId")
    if not mid:
        emit("error", {"message": "missing id"}); return
    c = db(); cur = c.cursor()
    cur.execute("SELECT sender_id, receiver_id FROM dm_messages WHERE id=?", (mid,))
    row = cur.fetchone()
    if not row:
        c.close(); emit("error", {"message": "missing message"}); return
    if row["sender_id"] != me["id"]:
        c.close(); emit("error", {"message": "no permission"}); return
    cur.execute("UPDATE dm_messages SET deleted_at=datetime('now') WHERE id=?", (mid,))
    c.commit(); c.close()
    socketio.emit("dm_deleted", {"id": mid}, room=dm_room(row["sender_id"], row["receiver_id"]))


@socketio.on("dm_read")
def dm_read(data):
    me = current_user()
    if not me:
        return
    mid = (data or {}).get("messageId")
    if not mid:
        return
    c = db(); cur = c.cursor()
    cur.execute("SELECT sender_id, receiver_id FROM dm_messages WHERE id=?", (mid,))
    row = cur.fetchone()
    if not row:
        c.close(); return
    if row["receiver_id"] != me["id"]:
        c.close(); return
    cur.execute("UPDATE dm_messages SET read_at=datetime('now') WHERE id=?", (mid,))
    cur.execute("INSERT OR REPLACE INTO message_reads(message_id, user_id) VALUES(?,?)", (mid, me["id"]))
    c.commit(); c.close()
    socketio.emit("dm_read", {"id": mid}, room=dm_room(row["sender_id"], row["receiver_id"]))


@socketio.on("group_join")
def group_join(data):
    me = current_user()
    if not me:
        emit("error", {"message": "auth required"}); return
    slug = (data or {}).get("slug")
    room = get_group_by_slug((slug or "").strip())
    if not room:
        emit("error", {"message": "group missing"}); return
    join_room(group_socket_room(room["slug"]))
    c = db(); cur = c.cursor()
    cur.execute("SELECT * FROM group_messages WHERE room_id=? ORDER BY id DESC LIMIT 120", (room["id"],))
    msgs = [dict(x) for x in cur.fetchall()]
    c.close()
    socketio.emit("group_history", list(reversed(msgs)), room=request.sid)


@socketio.on("group_send")
def group_send(data):
    me = current_user()
    if not me:
        emit("error", {"message": "auth required"}); return
    slug = (data or {}).get("slug")
    text = ((data or {}).get("text") or "").strip()[:800]
    if not slug or not text:
        emit("error", {"message": "missing data"}); return
    room = get_group_by_slug(slug)
    if not room:
        emit("error", {"message": "group missing"}); return
    c = db(); cur = c.cursor()
    cur.execute("INSERT INTO group_messages(room_id, sender_id, sender_name, text) VALUES(?,?,?,?)",
                (room["id"], me["id"], me["username"], text))
    mid = cur.lastrowid
    cur.execute("SELECT * FROM group_messages WHERE id=?", (mid,))
    msg = dict(cur.fetchone())
    c.commit(); c.close()
    socketio.emit("group_new", msg, room=group_socket_room(room["slug"]))


@socketio.on("group_typing")
def group_typing(data):
    me = current_user()
    if not me:
        return
    slug = (data or {}).get("slug")
    room = get_group_by_slug(slug)
    if not room:
        return
    group_typing_state.setdefault(room["slug"], set()).add(me["username"])
    socketio.emit("group_typing", list(group_typing_state[room["slug"]]), room=group_socket_room(room["slug"]), include_self=False)


@socketio.on("group_stop_typing")
def group_stop_typing(data):
    me = current_user()
    if not me:
        return
    slug = (data or {}).get("slug")
    if not slug:
        return
    names = group_typing_state.get(slug)
    if names:
        names.discard(me["username"])
        socketio.emit("group_typing", list(names), room=group_socket_room(slug), include_self=False)

# -------------------- Main --------------------
if __name__ == "__main__":
    init_db()
    print(f"[Boot] EchoBoard startet auf 0.0.0.0:{APP_PORT}")
    socketio.run(app, host="0.0.0.0", port=APP_PORT)
