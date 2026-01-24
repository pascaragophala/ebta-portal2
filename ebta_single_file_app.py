import os
import json
import sqlite3
import datetime
import calendar
import random
import base64
import secrets
from urllib.parse import urlencode
from zoneinfo import ZoneInfo
from pathlib import Path

from flask import Flask, request, redirect, url_for, render_template_string, send_from_directory, session, flash, make_response

app = Flask(__name__)
app.secret_key = os.environ.get('EBTA_SECRET_KEY', 'ebta-dev-secret')



# =============================================================
# RENDER PERSISTENT STORAGE (SAFE + ORDERED)
# =============================================================
BASE_DATA_DIR = os.environ.get("RENDER_DATA_DIR", "/var/data")
os.makedirs(BASE_DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(BASE_DATA_DIR, "ebta.db")
UPLOADS_DIR = Path(BASE_DATA_DIR) / "uploads"
UPLOAD_DIR = UPLOADS_DIR
MATERIALS_DIR = Path(BASE_DATA_DIR) / "materials"
SUBMISSIONS_DIR = Path(BASE_DATA_DIR) / "submissions"
QR_DIR = Path(BASE_DATA_DIR) / "qr"

for d in (UPLOADS_DIR, MATERIALS_DIR, SUBMISSIONS_DIR, QR_DIR):
    d.mkdir(parents=True, exist_ok=True)

LOGO_URL = os.environ.get("EBTA_LOGO_URL", "https://i.imgur.com/SqocnYt.png")
# =============================================================



# ===================== DB ==============
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
    except Exception:
        pass
    return conn

def now_utc_iso():
    """Return ISO timestamp in Africa/Johannesburg timezone (UTC+02:00)."""
    try:
        if ZoneInfo is not None:
            tz = ZoneInfo('Africa/Johannesburg')
            return datetime.datetime.now(tz).isoformat()
    except Exception:
        pass
    # Fallback: fixed UTC+02 offset if zoneinfo unavailable
    tz = datetime.timezone(datetime.timedelta(hours=2))
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=2)).replace(tzinfo=tz).isoformat()


def ensure_column(conn, table, column, ddl_tail):
    cur = conn.cursor()
    # -------- SAFE SUBJECTS GUARD --------
    try:
        cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='subjects'")
        if not cur.fetchone():
            init_db()
    except Exception:
        init_db()
    # -----------------------------------

    cur.execute(f"PRAGMA table_info({table})")
    cols = [r[1] for r in cur.fetchall()]
    if column not in cols:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_tail}")

def init_db():
    conn = get_db()
    cur = conn.cursor()
  

    cur.execute("""
    CREATE TABLE IF NOT EXISTS settings(
        key TEXT PRIMARY KEY,
        value TEXT
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS students(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        full_name TEXT NOT NULL,
        phone_whatsapp TEXT NOT NULL UNIQUE,
        guardian_phone TEXT,
        email TEXT,
        grade TEXT NOT NULL,
        pin TEXT,
        created_at TEXT NOT NULL
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS subjects(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        grade TEXT NOT NULL,
        UNIQUE(name,grade)
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS groups(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        subject_id INTEGER NOT NULL,
        month TEXT NOT NULL,
        invite_link TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(subject_id,month),
        FOREIGN KEY(subject_id) REFERENCES subjects(id)
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS enrollments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER NOT NULL,
        subject_id INTEGER NOT NULL,
        month TEXT NOT NULL,
        status TEXT NOT NULL,
        payment_method TEXT,
        payment_ref TEXT,
        pop_url TEXT,                 -- legacy single PoP (kept for compatibility)
        status_token TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(student_id) REFERENCES students(id),
        FOREIGN KEY(subject_id) REFERENCES subjects(id)
    );
    """)

    # Multiple PoP files per enrollment
    cur.execute("""
    CREATE TABLE IF NOT EXISTS enrollment_files(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        enrollment_id INTEGER NOT NULL,
        file_path TEXT NOT NULL,
        FOREIGN KEY(enrollment_id) REFERENCES enrollments(id) ON DELETE CASCADE
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS payments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        enrollment_id INTEGER NOT NULL,
        amount INTEGER NOT NULL,
        gateway TEXT NOT NULL,
        reference TEXT NOT NULL,
        result TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        FOREIGN KEY(enrollment_id) REFERENCES enrollments(id)
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS tutors(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        full_name TEXT NOT NULL,
        phone TEXT NOT NULL UNIQUE,
        pin TEXT,
        created_at TEXT NOT NULL
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS tutor_subjects(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tutor_id INTEGER NOT NULL,
        subject_id INTEGER NOT NULL,
        UNIQUE(tutor_id,subject_id),
        FOREIGN KEY(tutor_id) REFERENCES tutors(id) ON DELETE CASCADE,
        FOREIGN KEY(subject_id) REFERENCES subjects(id) ON DELETE CASCADE
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS sessions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        subject_id INTEGER NOT NULL,
        tutor_id INTEGER NOT NULL,
        day_of_week INTEGER NOT NULL,
        start_time TEXT NOT NULL,
        end_time TEXT NOT NULL,
        meet_link TEXT,
        active INTEGER NOT NULL DEFAULT 1,
        FOREIGN KEY(subject_id) REFERENCES subjects(id),
        FOREIGN KEY(tutor_id) REFERENCES tutors(id)
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS attendance(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL,
        student_id INTEGER NOT NULL,
        date TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(session_id) REFERENCES sessions(id),
        FOREIGN KEY(student_id) REFERENCES students(id)
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS materials(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        subject_id INTEGER NOT NULL,
        tutor_id INTEGER NOT NULL,
        month TEXT NOT NULL,
        title TEXT NOT NULL,
        kind TEXT NOT NULL,          -- 'file'|'youtube'|'assignment'
        file_path TEXT,
        youtube_url TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(subject_id) REFERENCES subjects(id),
        FOREIGN KEY(tutor_id) REFERENCES tutors(id)
    );
    """)
    
    ensure_column(conn, "students", "guardian_name", "TEXT")
    ensure_column(conn, "materials", "is_assignment", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "materials", "due_date", "TEXT")
    ensure_column(conn, "materials", "max_points", "INTEGER NOT NULL DEFAULT 100")
    ensure_column(conn, "students", "province", "TEXT")
    ensure_column(conn, "students", "school", "TEXT")
    ensure_column(conn, "enrollments", "amount_paid", "INTEGER")
    ensure_column(conn, "groups", "is_visible", "INTEGER NOT NULL DEFAULT 1")
    ensure_column(conn, "sessions", "is_visible", "INTEGER NOT NULL DEFAULT 1")



    cur.execute("""
    CREATE TABLE IF NOT EXISTS submissions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        material_id INTEGER NOT NULL,
        student_id INTEGER NOT NULL,
        file_path TEXT NOT NULL,
        submitted_at TEXT NOT NULL,
        mark INTEGER,
        feedback TEXT,
        evaluated_at TEXT,
        UNIQUE(material_id,student_id),
        FOREIGN KEY(material_id) REFERENCES materials(id) ON DELETE CASCADE,
        FOREIGN KEY(student_id) REFERENCES students(id) ON DELETE CASCADE
    );
    """)

    # Simple direct messages between roles
    cur.execute("""
    CREATE TABLE IF NOT EXISTS direct_messages(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        from_role TEXT NOT NULL,     -- 'student'|'tutor'|'admin'
        from_id INTEGER,             -- null/0 for admin
        to_role TEXT NOT NULL,
        to_id INTEGER,
        subject_id INTEGER,          -- optional context
        body TEXT NOT NULL,
        created_at TEXT NOT NULL,
        is_read INTEGER NOT NULL DEFAULT 0
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS messages(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        payload TEXT NOT NULL,
        created_at TEXT NOT NULL,
        resolved INTEGER NOT NULL DEFAULT 0
    );
    """)

    # Students rate their classes monthly (24th to end-of-month)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS lesson_ratings(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER NOT NULL,
        subject_id INTEGER NOT NULL,
        month TEXT NOT NULL,         -- 'YYYY-MM'
        rating INTEGER NOT NULL,     -- 1..5
        comment TEXT,
        created_at TEXT NOT NULL,
        UNIQUE(student_id, subject_id, month),
        FOREIGN KEY(student_id) REFERENCES students(id) ON DELETE CASCADE,
        FOREIGN KEY(subject_id) REFERENCES subjects(id) ON DELETE CASCADE
    );
    """)
    
    
    

    # Defaults & seed
    cur.execute("SELECT value FROM settings WHERE key='current_month'")
    if not cur.fetchone():
        cur.execute("INSERT INTO settings(key,value) VALUES(?,?)",
                    ('current_month', datetime.date.today().strftime('%Y-%m')))

    cur.execute("SELECT COUNT(*) AS c FROM subjects")
    if cur.fetchone()["c"] == 0:
        seed = [
            # Mathematics
            ("Mathematics","G8"), ("Mathematics","G9"),
            ("Mathematics","G10"), ("Mathematics","G11"), ("Mathematics","G12"),("Mathematics","G13"),

            # Mathematical Literacy
            ("Mathematical Literacy","G10"),
            ("Mathematical Literacy","G11"),
            ("Mathematical Literacy","G12"),("Mathematical Literacy","G13"),

            # Physical Sciences
            ("Physical Sciences","G10"),
            ("Physical Sciences","G11"),
            ("Physical Sciences","G12"),("Physical Sciences","G13"),

            # Life Sciences
            ("Life Sciences","G10"),
            ("Life Sciences","G11"),
            ("Life Sciences","G12"),("Life Sciences","G13"),

            # Accounting
            ("Accounting","G10"),
            ("Accounting","G11"),
            ("Accounting","G12"),("Accounting","G13"),

            # Geography
            ("Geography","G11"),
            ("Geography","G12"),

            # Economics
            ("Economics","G12"),

            # Business Studies
            ("Business Studies","G10"),
            ("Business Studies","G11"),
            ("Business Studies","G12"),

            # Grades 8–9
            ("EMS","G8"), ("EMS","G9"),
            ("Natural Sciences","G8"), ("Natural Sciences","G9"),
            
            #English
            ("English","G8"), ("English","G9"),("English","G10"),("English","G11"),("English","G12"),
        ]

        cur.executemany("INSERT OR IGNORE INTO subjects(name,grade) VALUES(?,?)", seed)
        # Ensure required subjects exist even if DB was previously seeded
        required_subjects = [
            # Mathematics
            ("Mathematics","G8"), ("Mathematics","G9"),
            ("Mathematics","G10"), ("Mathematics","G11"), ("Mathematics","G12"),("Mathematics","G13"),

            # Mathematical Literacy
            ("Mathematical Literacy","G10"),
            ("Mathematical Literacy","G11"),
            ("Mathematical Literacy","G12"),("Mathematical Literacy","G13"),

            # Physical Sciences
            ("Physical Sciences","G10"),
            ("Physical Sciences","G11"),
            ("Physical Sciences","G12"),("Physical Sciences","G13"),

            # Life Sciences
            ("Life Sciences","G10"),
            ("Life Sciences","G11"),
            ("Life Sciences","G12"),("Life Sciences","G13"),

            # Accounting
            ("Accounting","G10"),
            ("Accounting","G11"),
            ("Accounting","G12"),("Accounting","G13"),

            # Geography
            ("Geography","G11"),
            ("Geography","G12"),

            # Economics
            ("Economics","G12"),

            # Business Studies
            ("Business Studies","G10"),
            ("Business Studies","G11"),
            ("Business Studies","G12"),

            # Grades 8–9
            ("EMS","G8"), ("EMS","G9"),
            ("Natural Sciences","G8"), ("Natural Sciences","G9"),
            
            #English
            ("English","G8"), ("English","G9"),("English","G10"),("English","G11"),("English","G12"),
        ]

        cur.executemany("INSERT OR IGNORE INTO subjects(name,grade) VALUES(?,?)", required_subjects)
    

    
    # --- Quizzes ---
    cur.execute("""
    CREATE TABLE IF NOT EXISTS quizzes(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        subject_id INTEGER NOT NULL,
        tutor_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        description TEXT,
        duration_minutes INTEGER NOT NULL DEFAULT 10,
        opens_at TEXT,
        closes_at TEXT,
        is_published INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        FOREIGN KEY(subject_id) REFERENCES subjects(id) ON DELETE CASCADE,
        FOREIGN KEY(tutor_id) REFERENCES tutors(id) ON DELETE CASCADE
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS quiz_questions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        quiz_id INTEGER NOT NULL,
        question_text TEXT NOT NULL,
        options_json TEXT NOT NULL,
        correct_index INTEGER NOT NULL,
        points INTEGER NOT NULL DEFAULT 1,
        FOREIGN KEY(quiz_id) REFERENCES quizzes(id) ON DELETE CASCADE
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS quiz_attempts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        quiz_id INTEGER NOT NULL,
        student_id INTEGER NOT NULL,
        started_at TEXT NOT NULL,
        submitted_at TEXT,
        score INTEGER,
        detail_json TEXT,
        UNIQUE(quiz_id,student_id),
        FOREIGN KEY(quiz_id) REFERENCES quizzes(id) ON DELETE CASCADE,
        FOREIGN KEY(student_id) REFERENCES students(id) ON DELETE CASCADE
    );
    """)
    
    # Enrollment control defaults
    cur.execute("SELECT value FROM settings WHERE key='enrollment_open'")
    if not cur.fetchone():
        cur.execute(
            "INSERT INTO settings(key,value) VALUES(?,?)",
            ('enrollment_open', '1')  # 1 = open, 0 = closed
        )

    cur.execute("SELECT value FROM settings WHERE key='enrollment_message'")
    if not cur.fetchone():
        cur.execute(
            "INSERT INTO settings(key,value) VALUES(?,?)",
            (
                'enrollment_message',
                'Enrollments are currently closed. February enrollments open on 20 January 2026.'
            )
        )

    
    # --- REMOVE UNWANTED SUBJECTS (SAFE CLEANUP) ---
    # (Currently disabled – kept for future use)

    # subjects_to_remove = [
    #     ("Geography", "G10"),
    #     ("Geography", "G13"),
    #     ("Economics", "G10"),
    #     ("Economics", "G11"),
    #     ("Economics", "G13"),
    #     ("Business Studies", "G13"),
    # ]

    # for name, grade in subjects_to_remove:
    #     # Remove related enrollments
    #     cur.execute("""
    #         DELETE FROM enrollments
    #         WHERE subject_id IN (
    #             SELECT id FROM subjects WHERE name=? AND grade=?
    #         )
    #     """, (name, grade))

    #     # Remove tutor-subject mappings
    #     cur.execute("""
    #         DELETE FROM tutor_subjects
    #         WHERE subject_id IN (
    #             SELECT id FROM subjects WHERE name=? AND grade=?
    #         )
    #     """, (name, grade))

    #     # Remove groups
    #     cur.execute("""
    #         DELETE FROM groups
    #         WHERE subject_id IN (
    #             SELECT id FROM subjects WHERE name=? AND grade=?
    #         )
    #     """, (name, grade))

    #     # Remove sessions
    #     cur.execute("""
    #         DELETE FROM sessions
    #         WHERE subject_id IN (
    #             SELECT id FROM subjects WHERE name=? AND grade=?
    #         )
    #     """, (name, grade))

    #     # Finally remove the subject itself
    #     cur.execute("""
    #         DELETE FROM subjects
    #         WHERE name=? AND grade=?
    #     """, (name, grade))


    
    conn.commit()
    conn.close()





# ===================== Registration helper/table ==============
def ensure_registration_table(conn=None):
    """Ensure registrations table exists. If conn provided, use it; otherwise open a new connection."""
    own_conn = False
    if conn is None:
        conn = get_db()
        own_conn = True
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS registrations(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER NOT NULL,
        year TEXT NOT NULL,
        amount INTEGER NOT NULL DEFAULT 50,
        payment_ref TEXT,
        created_at TEXT NOT NULL,
        UNIQUE(student_id, year),
        FOREIGN KEY(student_id) REFERENCES students(id) ON DELETE CASCADE
    );
    """)
    conn.commit()
    if own_conn:
        conn.close()

def student_registered_for_year(conn, student_id, year):
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM registrations WHERE student_id=? AND year=? LIMIT 1", (student_id, year))
    return cur.fetchone() is not None

# ===================== Helpers ==============
def safe_url(endpoint, fallback):
    """Return url_for(endpoint) if route exists, else fallback string."""
    try:
        return url_for(endpoint)
    except Exception:
        return fallback


DOW = ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"]

def get_admin_active_month():
    """
    Admin-only working month.
    Falls back to global system month if not overridden.
    """
    return session.get('admin_month') or get_setting('current_month')


def get_setting(key, default=""):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = cur.fetchone()
    conn.close()
    return row["value"] if row else default

def set_setting(key, value):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value)
    )
    conn.commit()
    conn.close()

def grade_label(g): return g.replace("G","Grade ")

def is_admin(): return bool(session.get("admin"))
def is_student(): return session.get("student_id")
def is_tutor(): return session.get("tutor_id")

def require_admin():
    if not is_admin(): return redirect(url_for('admin_login'))
def require_student():
    if not is_student(): return redirect(url_for('student_login'))
def require_tutor():
    if not is_tutor(): return redirect(url_for('tutor_login'))

def secure_name(name):
    keep="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
    return ''.join(ch if ch in keep else '_' for ch in name)


def normalize_phone(phone: str) -> str:
    if not phone:
        return ""
    return ''.join(ch for ch in phone if ch.isdigit())


def gen_pin(existing):
    while True:
        p = f"{random.randint(0,99999):05d}"
        if p not in existing: return p

def is_valid_pin(pin): return len(pin)==5 and pin.isdigit()

def pin_in_use(conn, pin):
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM students WHERE pin=? LIMIT 1",(pin,))
    if cur.fetchone(): return True
    cur.execute("SELECT 1 FROM tutors WHERE pin=? LIMIT 1",(pin,))
    return cur.fetchone() is not None

def b64url_encode(b): return base64.urlsafe_b64encode(b).decode('ascii').rstrip('=')
def b64url_decode(s):
    pad = '=' * (-len(s)%4)
    return base64.urlsafe_b64decode(s+pad)

def month_last_day(year:int, month:int) -> int:
    return calendar.monthrange(year, month)[1]

def rating_window_open(current_month: str) -> bool:
    """Open from the 24th to the last day of current_month (server date)."""
    today = datetime.date.today()
    try:
        y, m = map(int, current_month.split('-'))
    except Exception:
        return False
    last = month_last_day(y, m)
    if today.year == y and today.month == m and 24 <= today.day <= last:
        return True
    return False
        

def enrollment_exists(conn, student_id, subject_id, month):
    cur = conn.cursor()
    cur.execute(
        """
        SELECT 1 FROM enrollments
        WHERE student_id=? AND subject_id=? AND month=?
        LIMIT 1
        """,
        (student_id, subject_id, month)
    )
    return cur.fetchone() is not None


def pretty_month_label(month_str: str) -> str:
    """Convert 'YYYY-MM' to 'Month YYYY' (e.g., '2025-10' -> 'October 2025')."""
    try:
        y, m = map(int, month_str.split('-')[:2])
        return datetime.date(y, m, 1).strftime('%B %Y')
    except Exception:
        return month_str





# ===================== Notifications (Email & SMS) ==============
def send_email_notification(to_email: str, subject: str, body: str):
    # Best-effort email sender.
    # Uses SMTP settings from environment if configured, otherwise logs into the messages table.
    # Env vars for real sending:
    #   EBTA_SMTP_HOST, EBTA_SMTP_PORT, EBTA_SMTP_USER, EBTA_SMTP_PASS, EBTA_SMTP_FROM (optional, falls back to user).
    if not to_email:
        return
    host = os.environ.get("EBTA_SMTP_HOST")
    port = int(os.environ.get("EBTA_SMTP_PORT", "587"))
    user = os.environ.get("EBTA_SMTP_USER")
    pwd = os.environ.get("EBTA_SMTP_PASS")
    sender = os.environ.get("EBTA_SMTP_FROM", user)

    # If SMTP is not configured, just log the outgoing email in the admin Messages page
    if not (host and user and pwd and sender):
        try:
            conn = get_db()
            cur = conn.cursor()
            payload = f"TO:{to_email} | SUBJECT:{subject} | BODY:{body}"
            cur.execute(
                "INSERT INTO messages(kind,payload,created_at,resolved) VALUES(?,?,?,0)",
                ("email_log", payload, now_utc_iso()),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass
        return

    try:
        import smtplib
        from email.message import EmailMessage



        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = to_email
        msg.set_content(body)

        with smtplib.SMTP(host, port, timeout=30) as s:
            s.ehlo()
            s.starttls()
            s.ehlo()
            s.login(user, pwd)
            s.send_message(msg)
    except Exception as e:
        # Log error so admin can see what went wrong
        try:
            conn = get_db()
            cur = conn.cursor()
            payload = f"TO:{to_email} | SUBJECT:{subject} | ERROR:{e}"
            cur.execute(
                "INSERT INTO messages(kind,payload,created_at,resolved) VALUES(?,?,?,0)",
                ("email_error", payload, now_utc_iso()),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass


def send_sms_notification(to_phone: str, body: str):
    # Best-effort SMS sender.
    # Uses Twilio-style environment variables if available, otherwise logs into the messages table.
    # Env vars for real sending:
    #   EBTA_TWILIO_SID, EBTA_TWILIO_TOKEN, EBTA_TWILIO_FROM
    if not to_phone:
        return

    account_sid = os.environ.get("EBTA_TWILIO_SID")
    auth_token = os.environ.get("EBTA_TWILIO_TOKEN")
    from_number = os.environ.get("EBTA_TWILIO_FROM")

    # If Twilio not configured, log the SMS so it appears in Admin → Messages
    if not (account_sid and auth_token and from_number):
        try:
            conn = get_db()
            cur = conn.cursor()
            payload = f"TO:{to_phone} | BODY:{body}"
            cur.execute(
                "INSERT INTO messages(kind,payload,created_at,resolved) VALUES(?,?,?,0)",
                ("sms_log", payload, now_utc_iso()),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass
        return

    try:
        from twilio.rest import Client  # type: ignore

        client = Client(account_sid, auth_token)
        client.messages.create(from_=from_number, to=to_phone, body=body)
    except Exception as e:
        # Log error so admin can see what went wrong
        try:
            conn = get_db()
            cur = conn.cursor()
            payload = f"TO:{to_phone} | ERROR:{e}"
            cur.execute(
                "INSERT INTO messages(kind,payload,created_at,resolved) VALUES(?,?,?,0)",
                ("sms_error", payload, now_utc_iso()),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass


# ===================== Templating ==============
GOOGLE_FONTS = "<link href='https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap' rel='stylesheet'>"

BASE_CSS = """
<style>
:root{
--primary:#1b5e20;            /* Pasco green */
--primary-dark:#0f3d14;
--primary-light:#43a047;
--primary-bg:#f0fdf4;
--accent:#ffd54f;             /* Pasco gold */
--accent-light:#ffec99;
--bg:#f8fafc;
--card:#ffffff;
--text:#0f172a;
--muted:#64748b;
--border:#e2e8f0;
--border-light:#f1f5f9;
--table-stripe:#f8fafc;
--radius-sm:6px; --radius:10px; --radius-lg:14px; --radius-xl:18px; --radius-full:9999px;
--shadow-sm:0 1px 2px rgb(0 0 0 / 0.05);
--shadow:0 1px 3px rgb(0 0 0 / 0.1), 0 1px 2px rgb(0 0 0 / 0.06);
--shadow-md:0 4px 6px rgb(0 0 0 / 0.1), 0 2px 4px rgb(0 0 0 / 0.06);
--shadow-lg:0 10px 15px rgb(0 0 0 / 0.1), 0 4px 6px rgb(0 0 0 / 0.05);
--transition:all .2s ease;
}
*{box-sizing:border-box}
html,body{height:100%}
body{
margin:0;
font-family: Inter, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
background:linear-gradient(135deg, var(--primary-bg) 0%, var(--bg) 100%);
color:var(--text);
line-height:1.55;
}
/* Header */
.header{
position:sticky; top:0; z-index:20;
background:rgba(255,255,255,.95); backdrop-filter: blur(14px);
border-bottom:1px solid var(--border-light);
box-shadow:0 1px 3px rgba(0,0,0,.05);
}
.nav{max-width:1200px;margin:0 auto; padding:14px 18px; display:flex; align-items:center; justify-content:space-between}
.brand{display:flex;align-items:center;gap:12px}
.brand-logo{width:42px;height:42px;border-radius:14px;object-fit:cover;border:2px solid var(--primary-bg);box-shadow:var(--shadow-md);transition:var(--transition)}
.brand:hover .brand-logo{transform:scale(1.05)}
.brand .title{font-weight:800; letter-spacing:-.3px; background:linear-gradient(135deg,var(--primary-dark),var(--primary)); -webkit-background-clip:text; -webkit-text-fill-color:transparent}
.links a{color:#0f172a;text-decoration:none;font-weight:600; font-size:14px; margin-left:12px; padding:8px 12px; border-radius:10px; transition:var(--transition)}
.links a:hover{background:var(--border-light); color:var(--primary)}
/* Layout */
.wrap{max-width:1200px;margin:22px auto;padding:0 18px}
.grid{display:grid;grid-template-columns:1fr;gap:14px}
/* Cards */
.card{
background:var(--card);
border:1px solid var(--border);
border-radius:var(--radius-xl);
padding:16px;
box-shadow:var(--shadow);
position:relative; overflow:hidden; transition:var(--transition);
}
.card.soft{background:linear-gradient(180deg,rgba(255,255,255,.92),rgba(255,255,255,.75))}
.card:hover{box-shadow:var(--shadow-lg); transform:translateY(-2px)}
.card::before{
content:""; position:absolute; inset:0 0 auto 0; height:3px;
background:linear-gradient(90deg,var(--primary),var(--accent)); opacity:.0; transition:var(--transition)
}
.card:hover::before{opacity:1}
/* Headings */
h1{font-family:"Plus Jakarta Sans", Inter, sans-serif; font-size:22px; margin:0 0 8px}
h2{font-size:18px;margin:0 0 10px}
h3{font-size:16px;margin:0 0 8px}
.muted{color:var(--muted)} .mini{font-size:12px}
.auth-card{max-width:420px;margin:0 auto}
/* Forms */
label{font-size:13px;color:var(--muted); display:block; margin-bottom:6px; font-weight:600}
input,select,textarea{
width:100%; padding:11px 12px; border:2px solid var(--border);
border-radius:12px; background:#fff; color:var(--text); transition:var(--transition)
}
input:focus,select:focus,textarea:focus{outline:none; border-color:var(--primary); box-shadow:0 0 0 3px rgba(27,94,32,.12)}
textarea{min-height:96px; resize:vertical}
input::file-selector-button{padding:8px 10px;border:0;background:linear-gradient(135deg,var(--primary),var(--primary-dark));color:#fff;border-radius:10px;margin-right:10px}
/* Buttons */
.btn{
display:inline-flex; align-items:center; gap:8px; padding:11px 16px;
border-radius:12px; border:0; background:linear-gradient(135deg,var(--primary),var(--primary-dark));
color:#fff; text-decoration:none; cursor:pointer; box-shadow:var(--shadow-md); font-weight:700; transition:var(--transition); position:relative; overflow:hidden
}
.btn:hover{transform:translateY(-2px); box-shadow:var(--shadow-lg)}
.btn.secondary{background:#fff; color:var(--primary); border:2px solid var(--primary)}
.btn.success{background:linear-gradient(135deg,var(--primary-light),#2e7d32)}
.btn.warn{background:linear-gradient(135deg,#f59e0b,#d97706)}
.btn.danger{background:linear-gradient(135deg,#ef4444,#dc2626)}
.btn.mini{padding:6px 10px; font-weight:600}
/* Toolbar */
.toolbar{display:flex;flex-wrap:wrap;gap:10px;margin:10px 0}
/* Chips/Badges */
.chip{
display:inline-block; padding:6px 10px; border-radius:999px; font-size:12px;
background:#eef6ee; color:#14532d; border:1px solid rgba(20,83,45,.15); font-weight:700
}
.chip.pending{background:linear-gradient(135deg,#fef3c7,#fde68a); color:#92400e; border-color:#fbbf24}
.chip.active{background:linear-gradient(135deg,#d1fae5,#a7f3d0); color:#065f46; border-color:#34d399}
.chip.lapsed{background:linear-gradient(135deg,#fee2e2,#fecaca); color:#991b1b; border-color:#f87171}
.badge{display:inline-block;font-size:11px;background:#e6f5e7;color:#185c1c;border:1px solid #cbe8cd;padding:2px 8px;border-radius:999px}
/* Tables */
table{width:100%;border-collapse:separate;border-spacing:0;overflow:hidden;border-radius:14px}
thead th{
background:var(--bg); text-align:left; padding:12px; font-size:12px; color:#1b441c;
text-transform:uppercase; letter-spacing:.4px; border-bottom:1px solid var(--border)
}
tbody td{padding:12px; border-bottom:1px dashed rgba(0,0,0,.06)}
tbody tr:nth-child(even){background:var(--table-stripe)}
tbody tr:hover{background:#f0f7f1}
/* Messages */
.msg{border:1px solid var(--border);border-radius:14px;padding:12px;margin:8px 0;background:#fff}
.msg.me{border-left:4px solid var(--primary)} .msg.them{border-left:4px solid var(--accent)}
.msg .meta{font-size:12px;color:var(--muted);margin-bottom:4px}
/* Empty state */
.empty{padding:16px;border:1px dashed var(--border);border-radius:14px;color:var(--muted);text-align:center}
/* Footer */
.footer{
padding:28px 0; margin-top:36px; color:var(--muted); font-size:12px; text-align:center;
border-top:1px solid var(--border-light);
background:linear-gradient(180deg,transparent,var(--bg))
}
/* Utilities */
.small{max-width:760px;margin:0 auto}
.stats{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px}
.stat{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:14px;box-shadow:var(--shadow)}
.stat .k{font-size:20px;font-weight:800;color:var(--primary)}
.inlineform{display:inline-grid;grid-template-columns:1fr auto;gap:8px;align-items:center}
.feedback-list{display:grid;gap:10px}
.feedback-item{background:#fff;border:1px solid var(--border);border-left:4px solid var(--primary);padding:12px;border-radius:14px}
/* Responsive */
@media (max-width: 768px){
body{font-size:15px;}
.nav{
    padding:10px 14px;
    flex-direction:column;
    align-items:flex-start;
    gap:6px;
}
.brand .title{font-size:18px;}
.links{
    width:100%;
    display:flex;
    flex-wrap:wrap;
    gap:6px;
    justify-content:flex-start;
}
.links a{
    margin-left:0;
    padding:6px 10px;
    font-size:13px;
}
.stats{grid-template-columns:repeat(2,minmax(0,1fr))}
.wrap{padding:0 12px}
.layout{grid-template-columns:1fr}
.sidebar{
    position:relative;
    top:auto;
    max-height:none;
}
.footer{padding:22px 0}
}

.toolbar{
    flex-wrap: wrap;
}

.toolbar .btn{
    white-space: nowrap;
}


/* === Modern LMS Layout Additions === */
.layout{display:grid;grid-template-columns:280px 1fr;gap:16px;align-items:start}
.sidebar{
position:sticky; top:76px;
background:var(--card);
border:1px solid var(--border);
border-radius:var(--radius-xl);
box-shadow:var(--shadow);
padding:14px;
max-height: calc(100vh - 100px);
overflow:auto;
}
.sidebar .role{font-weight:800; font-size:14px; margin-bottom:6px}
.sidebar .user{font-size:13px;color:var(--muted); margin-bottom:12px}
.side-links{display:grid;gap:8px;margin:8px 0 14px}
.side-links a{
display:block; text-decoration:none; padding:10px 12px;
border:1px solid var(--border); border-radius:12px; font-weight:600;
color:var(--text); background:#fff; transition:var(--transition)
}
.side-links a:hover{transform:translateY(-1px); box-shadow:var(--shadow-sm); border-color:var(--primary)}
.stats-mini{display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px}
.stats-mini .s{
background:#fff; border:1px solid var(--border);
border-radius:12px; padding:10px; box-shadow:var(--shadow-sm)
}
.stats-mini .s .k{font-size:18px; font-weight:800; color:var(--primary)}
.stats-mini .s .t{font-size:11px; color:var(--muted)}
.announce{background:#fffaf0; border:1px solid #fde68a; padding:12px; border-radius:12px; margin-bottom:12px}
.announce h3{margin:0 0 6px; font-size:14px}

/* Hash navigation highlight */
.flash-highlight{animation: flashBorder 2.8s ease-in-out; box-shadow: 0 0 0 4px rgba(255,215,0,.25); position: relative;}
@keyframes flashBorder{
0%{box-shadow: 0 0 0 0 rgba(255,215,0,.0)}
10%{box-shadow: 0 0 0 4px rgba(255,215,0,.35)}
55%{box-shadow: 0 0 0 4px rgba(46,125,50,.35)}
100%{box-shadow: 0 0 0 0 rgba(46,125,50,.0)}
}
.flash-highlight::before{
content:""; position:absolute; inset:-1px; border-radius:inherit; padding:1px;
background: linear-gradient(135deg,#ffd54f,#2e7d32);
-webkit-mask: linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0);
-webkit-mask-composite: xor; mask-composite: exclude;
}
.side-links a.active{ outline:2px solid #2e7d32; background:#f0fff4; }

/* === EBTA wide-mode & sidebar collapse enhancements (kept INSIDE <style>) === */
:root { --page-max: 1280px; }
.wrap, .container, .shell, .page, .content-wrap, main.page { max-width: var(--page-max); }
.admin-shell, .two-col, .layout-admin, .admin-grid { display: grid; grid-template-columns: 280px 1fr; gap: 18px; }
body.sidebar-collapsed .admin-shell,
body.sidebar-collapsed .two-col,
body.sidebar-collapsed .layout-admin,
body.sidebar-collapsed .admin-grid { grid-template-columns: 72px 1fr; }
body.sidebar-collapsed .side-links .label { display: none; }
body.sidebar-collapsed .side-links .item { justify-content: center; }
body.sidebar-collapsed .side-links .icon { margin-right: 0; }
body.wide-mode :root, body.wide-mode .wrap, body.wide-mode .container, body.wide-mode .shell, body.wide-mode .page, body.wide-mode .content-wrap { --page-max: 1440px; }
.card, .panel, .stats .tile { transition: transform .12s ease, box-shadow .12s ease; }
.card:hover, .panel:hover, .stats .tile:hover { transform: translateY(-2px); box-shadow: 0 8px 24px rgba(0,0,0,.08); }
.btn { transition: transform .08s ease; } .btn:active { transform: scale(.98); }
.ui-controls { position: sticky; top: 8px; display: flex; gap: 8px; justify-content: flex-end; align-items: center; margin-bottom: 8px; }
.ui-controls .chip { cursor: pointer; padding: 6px 10px; border: 1px solid #cfd8d3; border-radius: 999px; background: #ffffffcc; backdrop-filter: blur(6px); font-size: 12px; }
.sidebar-head { display:flex; align-items:center; justify-content:space-between; margin-bottom:10px; }
.sidebar-head .collapse { font-size:12px; border:1px solid #cfd8d3; border-radius:8px; padding:6px 8px; cursor:pointer; background:#fff; }

.scroll-x{
overflow-x:auto;
-webkit-overflow-scrolling:touch;
}
.scroll-x table{
min-width:720px;
}

.two-col{
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}

.subject-grid{
  display: grid;
  gap: 10px;
  grid-template-columns: repeat(3, 1fr); /* desktop default */
}

.subject-item{
  display: flex;
  align-items: center;          /* vertical lock */
  justify-content: flex-start;
  gap: 12px;

  min-height: 56px;             /* VERY important for iOS */
  width: 100%;

  border: 1px solid var(--border);
  padding: 12px 14px;
  border-radius: 12px;
  background: #fff;
}

.subject-item.hidden{
  display: none;
}
/* Checkbox hard lock */
.subject-item input[type="checkbox"]{
  flex: 0 0 20px;
  width: 20px;
  height: 20px;
  margin: 0;
  padding: 0;

  appearance: auto;
  -webkit-appearance: checkbox;
}

/* Text lock */
.subject-item span{
  flex: 1;
  line-height: 1.3;
  white-space: normal;
}

.payment-confirm{
  margin-top:10px;
  display:flex;
  align-items:flex-start;
  gap:12px;
}

.payment-confirm input{
  flex-shrink:0;
  width:20px;
  height:20px;
  margin-top:2px;
}

/* =========================
   TABLETS & iPad (<=1024px)
   ========================= */
@media (max-width: 1024px){

  /* Force all two-column layouts to stack */
  .two-col{
    grid-template-columns: 1fr;
    gap: 14px;
  }

  /* Generic grid safety */
  .grid[style*="grid-template-columns"]{
    grid-template-columns: 1fr !important;
  }

  /* Layout + sidebar */
  .layout{
    grid-template-columns: 1fr;
    gap: 12px;
  }

  .sidebar{
    position: relative;
    top: auto;
    max-height: none;
    margin-bottom: 14px;
  }

  /* Subject grid: 2 columns on tablet */
  .subject-grid{
    grid-template-columns: repeat(2, 1fr);
  }

  /* Inputs (prevent iOS zoom + spacing issues) */
  input,
  select,
  textarea{
    padding: 14px;
    font-size: 16px;
  }

  /* Stats */
  .stats,
  .stats-mini{
    grid-template-columns: repeat(2, minmax(0,1fr));
  }

  h1{font-size:20px;}
  h2{font-size:17px;}
}

/* =========================
   PHONES (<=600px)
   ========================= */
@media (max-width: 600px){

  /* Subject grid: single column */
  .subject-grid{
    grid-template-columns: 1fr;
  }

  #fee_summary{
    position: relative;
  }
}

@supports (-webkit-touch-callout: none) {

  /* iOS grid stability */
  .subject-grid{
    align-content: start;
  }

  /* Prevent Safari from reserving phantom space */
  .subject-item{
    max-width: 100%;
  }

  /* iOS checkbox vertical bug */
  .subject-item input[type="checkbox"]{
    align-self: center;
  }

  /* Prevent iOS font zoom shifting layout */
  input,
  select,
  textarea{
    font-size: 16px !important;
  }
}

.admin-nav {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 16px;
}



</style>
"""


BASE_JS = """
<script>
function filterTable(inputId, tableId){
const q=(document.getElementById(inputId)?.value||"").toLowerCase();
const rows=document.querySelectorAll('#'+tableId+' tbody tr');
rows.forEach(r=>{ r.style.display = r.innerText.toLowerCase().includes(q) ? '' : 'none'; });
}
document.addEventListener('DOMContentLoaded',()=> {
const appear = new IntersectionObserver((entries)=>{
    entries.forEach(e=>{
    if(e.isIntersecting){ e.target.style.transition='transform .4s, opacity .4s'; e.target.style.transform='translateY(0)'; e.target.style.opacity='1'; appear.unobserve(e.target); }
    });
}, {threshold:.12});
document.querySelectorAll('.card').forEach(el=>{ el.style.transform='translateY(8px)'; el.style.opacity='.0'; appear.observe(el); });
});

function smoothScrollIntoView(el){
if(!el) return;
const y = el.getBoundingClientRect().top + window.scrollY - 90;
window.scrollTo({top:y, behavior:'smooth'});
el.classList.add('flash-highlight');
setTimeout(()=>el.classList.remove('flash-highlight'), 3000);
}
function findCardByHeadingText(keywords){
const cards=[...document.querySelectorAll('.card')];
for(const card of cards){
    const h = card.querySelector('h1,h2,h3');
    if(!h) continue;
    const t = (h.textContent||'').toLowerCase();
    for(const k of keywords){
    if(t.includes(k.toLowerCase())) return card;
    }
}
return null;
}
function highlightSectionByHash(hash){
if(!hash) return;
let el=null;
switch(hash){
    case '#dashboard':
    // Try to find "Your Enrollments"
    el = findCardByHeadingText(['your enrollments','welcome']);
    if(el){
        // also lightly highlight next siblings
        const next1 = el.nextElementSibling, next2 = next1 && next1.nextElementSibling;
        [el,next1,next2].forEach(x=>{ if(x && x.classList.contains('card')) { x.classList.add('flash-highlight'); setTimeout(()=>x.classList.remove('flash-highlight'),3000);} });
        smoothScrollIntoView(el);
        return;
    }
    break;
    case '#upload':
    el = findCardByHeadingText(['upload materials','upload'])
    break;
    case '#assignments':
    el = findCardByHeadingText(['assignments','materials & assignments','materials']);
    break;
    case '#materials':
    el = findCardByHeadingText(['materials']);
    break;
    case '#messages':
    el = findCardByHeadingText(['messages','inbox']);
    break;
    case '#status':
    el = document.getElementById('status-banner') || findCardByHeadingText(['status']);
    break;
    case '#students':
    case '#tutors':
    case '#subjects':
    case '#enrollments':
    case '#groups':
    el = findCardByHeadingText(['group links','whatsapp links']); break;

    case '#sessions':
    case '#inbox':
    case '#analytics':
    case '#settings':
    case '#export':
    el = findCardByHeadingText([hash.replace('#','')]);
    break;
    default:
    // Fall back: try id
    el = document.querySelector(hash);
}
if(el){ smoothScrollIntoView(el); }
}
window.addEventListener('hashchange', ()=>highlightSectionByHash(location.hash));
document.addEventListener('DOMContentLoaded', ()=>{
// intercept sidebar hash clicks for immediate action
document.body.addEventListener('click', (e)=>{
    const a = e.target.closest('a[href^="#"]');
    if(a){ e.preventDefault(); const h=a.getAttribute('href'); history.pushState(null,"",h); highlightSectionByHash(h); }
});
// if page loaded with a hash
if(location.hash){ setTimeout(()=>highlightSectionByHash(location.hash), 50); }
});

function mapAdminAnchors(){
// Add stable IDs to common admin sections by heading text
const pairs = [
    {id:'enrollments', keys:['manage enrollments','enrollments']},
    {id:'students', keys:['students']},
    {id:'tutors', keys:['tutors']},
    {id:'groups', keys:['group links','whatsapp links']},
    {id:'sessions', keys:['sessions & qr','sessions','qr']},
    {id:'inbox', keys:['inbox']},
    {id:'messages', keys:['direct messages','messages']},
    {id:'analytics', keys:['analytics','dashboard']},
    {id:'settings', keys:['settings']},
    {id:'export', keys:['export remove list','export','remove list']},
];
const cards=[...document.querySelectorAll('.card')];
for(const {id,keys} of pairs){
    for(const card of cards){
    const h=card.querySelector('h1,h2,h3'); if(!h) continue;
    const t=(h.textContent||'').toLowerCase();
    if(keys.some(k=>t.includes(k))){
        card.setAttribute('id', id);
        break;
    }
    }
}
}

function smoothScrollIntoView(el){
if(!el) return;
const y = el.getBoundingClientRect().top + window.scrollY - 90;
window.scrollTo({top:y, behavior:'smooth'});
el.classList.add('flash-highlight');
setTimeout(()=>el.classList.remove('flash-highlight'), 3000);
}
function findCardByHeadingText(keywords){
const cards=[...document.querySelectorAll('.card')];
for(const card of cards){
    const h = card.querySelector('h1,h2,h3');
    if(!h) continue;
    const t = (h.textContent||'').toLowerCase();
    for(const k of keywords){
    if(t.includes(k.toLowerCase())) return card;
    }
}
return null;
}
function highlightSectionByHash(hash){
if(!hash) return;
let el=null;
switch(hash){
    case '#dashboard':
    el = findCardByHeadingText(['your enrollments','welcome','overview','analytics']); 
    if(el){
        const next1 = el.nextElementSibling, next2 = next1 && next1.nextElementSibling;
        [el,next1,next2].forEach(x=>{ if(x && x.classList.contains('card')) { x.classList.add('flash-highlight'); setTimeout(()=>x.classList.remove('flash-highlight'),3000);} });
        smoothScrollIntoView(el); return;
    }
    break;
    case '#upload':
    el = findCardByHeadingText(['upload materials','upload']); break;
    case '#assignments':
    el = findCardByHeadingText(['assignments','materials & assignments','materials']); break;
    case '#materials':
    el = findCardByHeadingText(['materials']); break;
    case '#messages':
    el = findCardByHeadingText(['messages','inbox','direct messages']); break;
    case '#status':
    el = document.getElementById('status-banner') || findCardByHeadingText(['status']); break;
    case '#enrollments':
    el = document.getElementById('enrollments') || findCardByHeadingText(['manage enrollments','enrollments']); break;
    case '#students':
    el = document.getElementById('students') || findCardByHeadingText(['students']); break;
    case '#tutors':
    el = document.getElementById('tutors') || findCardByHeadingText(['tutors']); break;
    case '#groups':
    el = document.getElementById('groups') || findCardByHeadingText(['group links','whatsapp links']); break;
    case '#sessions':
    el = document.getElementById('sessions') || findCardByHeadingText(['sessions & qr','sessions','qr']); break;
    case '#inbox':
    el = document.getElementById('inbox') || findCardByHeadingText(['inbox']); break;
    case '#analytics':
    el = document.getElementById('analytics') || findCardByHeadingText(['analytics','dashboard']); break;
    case '#settings':
    el = document.getElementById('settings') || findCardByHeadingText(['settings']); break;
    case '#export':
    el = document.getElementById('export') || findCardByHeadingText(['export remove list','export','remove list']); break;
    default:
    el = document.querySelector(hash);
}
if(el){ smoothScrollIntoView(el); }
}
window.addEventListener('hashchange', ()=>highlightSectionByHash(location.hash));
document.addEventListener('DOMContentLoaded', ()=>{
mapAdminAnchors();
document.body.addEventListener('click', (e)=>{
    const a = e.target.closest('a[href^=\"#\"]');
    if(a){ e.preventDefault(); const h=a.getAttribute('href'); history.pushState(null,\"\",h); highlightSectionByHash(h); }
});
if(location.hash){ setTimeout(()=>{ mapAdminAnchors(); highlightSectionByHash(location.hash); }, 50); }
});

// --- Admin sidebar -> real content navigation (robust) ---
const ADMIN_MAP = {
'#enrollments': ['manage enrollments','enrollments','manage enrollment','enrollment'],
'#students': ['students','student list'],
'#tutors': ['tutors','tutor list'],
'#groups': ['group links','whatsapp links','links','groups'],
'#sessions': ['sessions & qr','sessions','qr'],
'#inbox': ['inbox'],
'#messages': ['direct messages','messages'],
'#analytics': ['analytics','dashboard','reports'],
'#settings': ['settings','configuration'],
'#export': ['export remove list','export','remove list']
};

function normalizeText(t){ return (t||'').replace(/\\s+/g,' ').trim().toLowerCase(); }

function findToolbarElementByLabels(labels){
const scope = document.querySelector('.dashboard-main') || document;
const candidates = [...scope.querySelectorAll('a,button')]
    .filter(el => !el.closest('.side-links')); // exclude sidebar itself
for(const el of candidates){
    const txt = normalizeText(el.textContent);
    for(const lbl of labels){
    const l = normalizeText(lbl);
    if(txt === l || txt.includes(l)) return el;
    }
}
return null;
}

function gotoAdminSection(hash){
const labels = ADMIN_MAP[hash];
if(!labels) return false;
const el = findToolbarElementByLabels(labels);
if(!el) return false;

// Prefer native navigation if it's a link
const href = el.getAttribute('href');
if(href){
    if(href.startsWith('#')){
    // intra-page: emulate natural click so existing handlers fire
    el.dispatchEvent(new MouseEvent('click', {bubbles:true}));
    }else{
    window.location.href = href;
    }
}else{
    el.dispatchEvent(new MouseEvent('click', {bubbles:true}));
}
return true;
}

function setActiveSidebar(hash){
document.querySelectorAll('.side-links a').forEach(a=>a.classList.remove('active'));
const link = document.querySelector(`.side-links a[href="${hash}"]`);
if(link){ link.classList.add('active'); }
}

document.addEventListener('DOMContentLoaded', ()=>{
document.body.addEventListener('click', (e)=>{
    const a = e.target.closest('.side-links a[href^="#"]');
    if(!a) return;
    const hash = a.getAttribute('href');
    setActiveSidebar(hash);
    if(ADMIN_MAP[hash]){
    e.preventDefault();
    const ok = gotoAdminSection(hash);
    if(!ok){
        // fallback to hash highlight
        history.pushState(null,"",hash);
        highlightSectionByHash(hash);
    }
    }
});

if(location.hash && ADMIN_MAP[location.hash]){
    setActiveSidebar(location.hash);
    // try to open the section on load too
    gotoAdminSection(location.hash);
}
});
// --- end robust admin nav ---

/* EBTA UI toggles */
(function(){
const LS = window.localStorage;
const apply = () => {
    if (LS.getItem('ebta-wide') === '1') document.body.classList.add('wide-mode'); else document.body.classList.remove('wide-mode');
    if (LS.getItem('ebta-sidebar-collapsed') === '1') document.body.classList.add('sidebar-collapsed'); else document.body.classList.remove('sidebar-collapsed');
};
apply();
document.addEventListener('DOMContentLoaded', ()=>{
    apply();
    const tWide = document.getElementById('toggleWide');
    const tSide = document.getElementById('toggleSidebar');
    const collapseBtn = document.getElementById('collapseSidebar');
    const toggleSide = ()=>{
    const v = LS.getItem('ebta-sidebar-collapsed') === '1' ? '0':'1';
    LS.setItem('ebta-sidebar-collapsed', v); apply();
    };
    if (tWide) tWide.addEventListener('click', ()=>{
    const v = LS.getItem('ebta-wide') === '1' ? '0':'1';
    LS.setItem('ebta-wide', v); apply();
    });
    if (tSide) tSide.addEventListener('click', toggleSide);
    if (collapseBtn) collapseBtn.addEventListener('click', toggleSide);
});
})();


document.addEventListener('DOMContentLoaded', function () {
const LS = window.localStorage;
const body = document.body;
const collapseBtn = document.getElementById('collapseSidebar');
const toggleBtn = document.getElementById('toggleSidebar');
const wideBtn = document.getElementById('toggleWide');

function applyState() {
    body.classList.toggle('sidebar-collapsed', LS.getItem('sidebarCollapsed') === '1');
    body.classList.toggle('wide-mode', LS.getItem('wideMode') === '1');
}

function toggleSidebar() {
    const newState = LS.getItem('sidebarCollapsed') === '1' ? '0' : '1';
    LS.setItem('sidebarCollapsed', newState);
    applyState();
}

function toggleWide() {
    const newState = LS.getItem('wideMode') === '1' ? '0' : '1';
    LS.setItem('wideMode', newState);
    applyState();
}

if (collapseBtn) collapseBtn.addEventListener('click', toggleSidebar);
if (toggleBtn) toggleBtn.addEventListener('click', toggleSidebar);
if (wideBtn) wideBtn.addEventListener('click', toggleWide);

applyState();
});


</script>
"""


def page(title, body_html, extra_head="", extra_js=""):
    auth = []
    if not (is_student() or is_tutor() or is_admin()):
        auth += [f"<a href='{url_for('student_login')}'>Student</a>",
                f"<a href='{url_for('tutor_login')}'>Tutor</a>"]
    else:
        if is_student():
            auth += [f"<a href='{url_for('student_home')}'>My Portal</a>", f"<a href='{url_for('student_logout')}'>Logout</a>"]
        if is_tutor():
            auth += [f"<a href='{url_for('tutor_home')}'>Tutor</a>", f"<a href='{url_for('tutor_logout')}'>Logout</a>"]
        if is_admin():
            auth += [f"<a href='{safe_url('admin_home','/admin')}'>Admin</a>", f"<a href='{url_for('admin_logout')}'>Logout</a>"]
    right = " ".join(auth)

    # Build role-aware sidebar with compact stats
    sidebar_html = ""
    ann_html = ""
    try:
        conn = get_db(); cur = conn.cursor()
        month = get_setting('current_month')

        if is_student():
            sid = is_student()
            cur.execute("SELECT COUNT(*) FROM enrollments WHERE student_id=? AND month=? AND status='ACTIVE'", (sid, month))
            active_subjects = cur.fetchone()[0] or 0
            cur.execute("""
                SELECT COUNT(*)
                FROM materials m
                WHERE (m.is_assignment=1 OR m.kind='assignment') AND m.month=?
                AND m.subject_id IN (SELECT subject_id FROM enrollments WHERE student_id=? AND month=? AND status='ACTIVE')
                AND NOT EXISTS (SELECT 1 FROM submissions s WHERE s.material_id=m.id AND s.student_id=?)
            """, (month, sid, month, sid))
            pending = cur.fetchone()[0] or 0
            cur.execute("SELECT COUNT(*) FROM submissions WHERE student_id=? AND mark IS NOT NULL", (sid,))
            graded = cur.fetchone()[0] or 0
            cur.execute("SELECT COUNT(*) FROM direct_messages WHERE to_role='student' AND to_id=? AND is_read=0", (sid,))
            unread = cur.fetchone()[0] or 0
            role_title, user_name = "Student", session.get('student_name','Student')
            links = [
                ("Dashboard", "#dashboard"),
                ("Assignments", "#assignments"),
                ("Materials", "#materials"),
                ("Messages", "#messages"),
                ("Status", "#status"),
                ("Logout", url_for('student_logout'))
            ]
            stats_grid = f"""
            <div class='stats-mini'>
            <div class='s'><div class='k'>{active_subjects}</div><div class='t'>Active subjects</div></div>
            <div class='s'><div class='k'>{pending}</div><div class='t'>Pending tasks</div></div>
            <div class='s'><div class='k'>{graded}</div><div class='t'>Marks released</div></div>
            <div class='s'><div class='k'>{unread}</div><div class='t'>Unread messages</div></div>
            </div>"""
        elif is_tutor():
            tid = is_tutor()
            cur.execute("SELECT COUNT(*) FROM tutor_subjects WHERE tutor_id=?", (tid,))
            subs = cur.fetchone()[0] or 0
            cur.execute("""
                SELECT COUNT(*)
                FROM submissions s
                JOIN materials m ON m.id=s.material_id
                WHERE m.tutor_id=? AND (s.mark IS NULL OR s.mark='')
            """, (tid,))
            to_mark = cur.fetchone()[0] or 0
            cur.execute("""
                SELECT COUNT(DISTINCT e.student_id)
                FROM enrollments e
                WHERE e.month=? AND e.status='ACTIVE'
                AND e.subject_id IN (SELECT subject_id FROM tutor_subjects WHERE tutor_id=?)
            """, (month, tid))
            active_students = cur.fetchone()[0] or 0
            cur.execute("SELECT COUNT(*) FROM direct_messages WHERE to_role='tutor' AND to_id=? AND is_read=0", (tid,))
            unread = cur.fetchone()[0] or 0
            role_title, user_name = "Tutor", session.get('tutor_name','Tutor')
            links = [
                ("Dashboard", "#dashboard"),
                ("Upload Material", "#upload"),
                ("Assignments", "#assignments"),
                ("Attendance", "#attendance"),
                ("Messages", "#messages"),
                ("Logout", url_for('tutor_logout'))
            ]
            stats_grid = f"""
            <div class='stats-mini'>
            <div class='s'><div class='k'>{subs}</div><div class='t'>Subjects</div></div>
            <div class='s'><div class='k'>{to_mark}</div><div class='t'>To mark</div></div>
            <div class='s'><div class='k'>{active_students}</div><div class='t'>Active students</div></div>
            <div class='s'><div class='k'>{unread}</div><div class='t'>Unread messages</div></div>
            </div>"""
        elif is_admin():
            cur.execute("SELECT COUNT(*) FROM enrollments WHERE status='PENDING'")
            pend = cur.fetchone()[0] or 0
            cur.execute("SELECT COUNT(*) FROM messages WHERE kind IN ('forgot_student_pin','forgot_tutor_pin') AND resolved=0")
            resets = cur.fetchone()[0] or 0
            cur.execute("SELECT COUNT(*) FROM students")
            students = cur.fetchone()[0] or 0
            cur.execute("SELECT COUNT(*) FROM tutors")
            tutors = cur.fetchone()[0] or 0
            role_title, user_name = "Admin", "Administrator"
            links = [
                ("Manage enrollments", "#enrollments"),
                ("Students", "#students"),
                ("Tutors", "#tutors"),
                ("Group links", "#groups"),
                ("Sessions & QR", "#sessions"),
                ("Inbox", "#inbox"),
                ("Direct messages", "#messages"),
                ("Analytics", "#analytics"),
                ("Settings", "#settings"),
                ("Export remove list", "#export"),
                ("Logout", url_for('admin_logout'))
            ]
            stats_grid = f"""
            <div class='stats-mini'>
            <div class='s'><div class='k'>{pend}</div><div class='t'>PoPs pending</div></div>
            <div class='s'><div class='k'>{resets}</div><div class='t'>PIN resets</div></div>
            <div class='s'><div class='k'>{students}</div><div class='t'>Students</div></div>
            <div class='s'><div class='k'>{tutors}</div><div class='t'>Tutors</div></div>
            </div>"""
        else:
            role_title = ""
            user_name = ""
            links = []
            stats_grid = ""

        # Build announcements (optional)
        cur = get_db().cursor()
        cur.execute("SELECT payload, created_at FROM messages WHERE kind='announcement' ORDER BY id ASC LIMIT 3")
        ann = cur.fetchall()
        if ann:
            items = "".join([f"<div><div class='mini muted'>{r['created_at'][:16].replace('T',' ')}</div><div>{r['payload']}</div></div>" for r in ann])
            ann_html = f"<div class='announce'><h3>Announcements</h3>{items}</div>"
    except Exception:
        role_title = ""
        links = []
        stats_grid = ""
        ann_html = ""

    if role_title:
        links_html = "".join([
            f"<a href='{href}'>{label}</a>"
            for (label, href) in links
        ])
        sidebar_html = f"""
        <aside class='sidebar'>
        <div class='role'>{role_title}</div>
        <div class='user'>{user_name}</div>
        

<div class='side-links'>{links_html}</div>
        {stats_grid}
        </aside>
        """

    # Build optional student status banner
    status_banner = ""
    try:
        if role_title == 'Student':
            # active_subjects and month already computed above
            if active_subjects and int(active_subjects) > 0:
                status_text = f"Enrolled for {month} (subjects: {active_subjects})"
                status_extra = ""
            else:
                status_text = f"Not enrolled for {month}"
                status_extra = f" <a class='links' href='/'>(Enroll now)</a>"
            status_banner = f"<div id='status-banner' class='card'><h2>Status</h2><div>{status_text}{status_extra}</div></div>"
    except Exception:
        status_banner = ""

    content_wrapped = f"<div class='layout'>{sidebar_html}<section class='dashboard-main'>{ann_html}{status_banner}{body_html}</section></div>" if sidebar_html else body_html

    return f"""
    <html><head>
    <meta name='viewport' content='width=device-width, initial-scale=1'/>
    <title>{title}</title>
    <link rel="icon" type="image/jpeg" href="https://i.imgur.com/SqocnYt.png">

    <!-- PWA -->
    <link rel="manifest" href="/static/manifest.json">
    <meta name="theme-color" content="#0f172a">
    <script>
      if ("serviceWorker" in navigator) {{
        navigator.serviceWorker.register("/static/sw.js");
      }}

      window.addEventListener("beforeinstallprompt", e => {{
        e.preventDefault();
        window.deferredPrompt = e;
      }});

      function installApp() {{
        if (window.deferredPrompt) {{
          window.deferredPrompt.prompt();
        }} else {{
          alert("Install option not available yet. Use your browser menu.");
        }}
      }}
    </script>


    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>

    {GOOGLE_FONTS}{BASE_CSS}{BASE_JS}{extra_head}
    </head><body>
    <header class='header'>
        <div class='nav'>
        <div class='brand'>
            <img class='brand-logo'
                src="https://i.imgur.com/SqocnYt.png"
                alt="EBTA logo"/>
            <div class='title'>EBTA Portal</div>
        </div>
        <div class='links'>
            <a href='/'>Home</a>
            <button onclick="installApp()" class="btn success mini" style="margin-left:8px">
                Install App
            </button>
            {right}
        </div>

        </div>
    </header>


    <main class='wrap'>{content_wrapped}</main>
    <footer class='footer'>
        <div class="copyright">
            © <span id="year"></span> Early Bird Testimony Academy · All rights reserved.
        </div>
        <div style="opacity:0.95;">⚡ Powered by <a href="https://pascalmindtech.netlify.app/" target="_blank" style="color:#000;text-decoration:underline;font-weight:600;">Pasca Ragophala</a></div>
    </footer>{extra_js}
    </body></html>
    """


# ===================== File routes ==============
@app.route('/uploads/<path:filename>')
def uploads(filename): return send_from_directory(UPLOAD_DIR, filename)

@app.route('/materials-files/<path:filename>')
def materials_files(filename): return send_from_directory(MATERIALS_DIR, filename)

@app.route('/submission-files/<path:filename>')
def submission_files(filename): return send_from_directory(SUBMISSIONS_DIR, filename)

@app.get('/logo')
def logo():
    for ext in ['png','jpg','jpeg','webp','gif']:
        f=UPLOAD_DIR/f'logo.{ext}'
        if f.exists(): return send_from_directory(UPLOAD_DIR, f.name)
    try:
        if LOGO_URL:
            with urlreq.urlopen(LOGO_URL, timeout=7) as r:
                data=r.read()
                ct=(r.headers.get('Content-Type') or 'image/jpeg').split(';')[0]
                ext='png' if 'png' in ct else ('webp' if 'webp' in ct else ('gif' if 'gif' in ct else 'jpg'))
                p=UPLOAD_DIR/f'logo.{ext}'
                p.write_bytes(data)
                resp=make_response(data); resp.headers['Content-Type']=ct; return resp
    except Exception: pass
    svg=("<svg xmlns='http://www.w3.org/2000/svg' width='48' height='48' viewBox='0 0 48 48'>"
        "<rect width='48' height='48' rx='8' fill='#2e7d32'/>"
        "<text x='24' y='28' text-anchor='middle' font-family='Poppins, Arial' font-size='20' font-weight='700' fill='#ffeb3b'>PA</text>"
        "</svg>")
    resp=make_response(svg); resp.headers['Content-Type']='image/svg+xml'; return resp


# ===================== Home & Registration (multi-subject + PIN + PoP required) ==============


@app.get('/')
def home():
    conn = get_db()
    cur = conn.cursor()
    
    enrollment_open = get_setting('enrollment_open', '1') == '1'
    enrollment_message = get_setting(
        'enrollment_message',
        'Enrollments are currently closed.'
    )

    
    # Ensure key subjects exist for all offered grades (idempotent)
    required_subjects = [
        # Mathematics
        ("Mathematics","G8"), ("Mathematics","G9"),
        ("Mathematics","G10"), ("Mathematics","G11"), ("Mathematics","G12"),("Mathematics","G13"),

        # Mathematical Literacy
        ("Mathematical Literacy","G10"),
        ("Mathematical Literacy","G11"),
        ("Mathematical Literacy","G12"),("Mathematical Literacy","G13"),

        # Physical Sciences
        ("Physical Sciences","G10"),
        ("Physical Sciences","G11"),
        ("Physical Sciences","G12"),("Physical Sciences","G13"),

        # Life Sciences
        ("Life Sciences","G10"),
        ("Life Sciences","G11"),
        ("Life Sciences","G12"),("Life Sciences","G13"),

        # Accounting
        ("Accounting","G10"),
        ("Accounting","G11"),
        ("Accounting","G12"),("Accounting","G13"),

        # Geography
        ("Geography","G11"),
        ("Geography","G12"),

        # Economics
        ("Economics","G12"),

        # Business Studies
        ("Business Studies","G10"),
        ("Business Studies","G11"),
        ("Business Studies","G12"),

        # Grades 8–9
        ("EMS","G8"), ("EMS","G9"),
        ("Natural Sciences","G8"), ("Natural Sciences","G9"),
        
        #English
        ("English","G8"), ("English","G9"),("English","G10"),("English","G11"),("English","G12"),
    ]

    try:
        cur.executemany("INSERT OR IGNORE INTO subjects(name,grade) VALUES(?,?)", required_subjects)
        conn.commit()
    except Exception:
        pass

    cur.execute("SELECT id,name,grade FROM subjects ORDER BY grade,name")
    subjects = cur.fetchall()
    conn.close()

    order = ['G8', 'G9', 'G10', 'G11', 'G12','G13']
    grade_names = {
        'G8': 'Grade 8',
        'G9': 'Grade 9',
        'G10': 'Grade 10',
        'G11': 'Grade 11',
        'G12': 'Grade 12',
        'G13': 'Upgrading'  
    }

    # Build grade dropdown options (only grades that have subjects)
    available_grades = sorted({row['grade'] for row in subjects if row['grade'] in order},
                            key=lambda g: order.index(g))
    grade_options = "<option value=''>Select grade…</option>" + "".join(
        f"<option value='{g}'>{grade_names.get(g, g)}</option>"
        for g in available_grades
    )

    # Build a flat list of subject checkboxes, each tagged with data-grade
    subject_items = "".join(
        f"<label data-grade='{s['grade']}' class='subject-item hidden'>"
        f"<input type='checkbox' name='subject_ids' value='{s['id']}'/>"
        f"<span>{grade_names.get(s['grade'], s['grade'])} — {s['name']}</span>"
        f"</label>"
        for s in subjects
    )



    month_raw = get_setting('current_month')
    month_label = pretty_month_label(month_raw)
    
    if not enrollment_open:
        conn.close()
        body = f"""
        <section style="
            min-height:70vh;
            display:flex;
            align-items:center;
            justify-content:center;
            padding:20px;
        ">
            <div class='card soft' style="
                max-width:520px;
                width:100%;
                text-align:center;
                padding:28px 24px;
            ">
                <h1 style="margin-bottom:12px;">
                    Enrollments Closed
                </h1>

                <p class='mini muted' style="
                    font-size:16px;
                    line-height:1.6;
                ">
                    {enrollment_message}
                </p>

                <div style="margin-top:18px;">
                    <span class="mini" style="color:#475569;">
                        Please check back soon.
                    </span>
                </div>
            </div>
        </section>
        """
        return page("EBTA Enrollment", body)



    body = fr"""
    <section class='grid' style='margin-top:10px'>
    <div class='card soft'>
        <h1>Enroll for {month_label}</h1>
        <p class='muted'>All required fields are marked. Upload 1–2 Proof of Payment files.</p>

        <form id='reg_form' method='post' action='{url_for('register')}' enctype='multipart/form-data' class='grid'>

        <!-- Student & guardian details -->
        <div class="grid two-col">
            <div>
            <label>Student Name</label>
            <input name='full_name' required/>
            </div>
            <div>
            <label>Student WhatsApp Number</label>
            <input name='phone' required/>
            </div>
            <div>
            <label>Guardian Name</label>
            <input name='guardian_name' required/>
            </div>
            <div>
            <label>Guardian WhatsApp Number</label>
            <input name='guardian' required/>
            </div>
            <div>
            <label>Student Email (optional)</label>
            <input name='email'/>
            </div>
            <div>
              <label>Province</label>
              <select name="province" required>
                <option value="">Select province…</option>
                <option>Eastern Cape</option>
                <option>Free State</option>
                <option>Gauteng</option>
                <option>KwaZulu-Natal</option>
                <option>Limpopo</option>
                <option>Mpumalanga</option>
                <option>North West</option>
                <option>Northern Cape</option>
                <option>Western Cape</option>
              </select>
            </div>

            <div>
              <label>School</label>
              <input name="school" placeholder="School name" required />
            </div>

        </div>

        <!-- Grade & subjects -->
        <div class="grid two-col">
            <div>
            <label>Choose grade</label>
            <select id='grade_select' name='grade'>
                {grade_options}
            </select>
            </div>
            <div class='mini muted' style='align-self:end'>
            Select a grade first, then choose subject(s) for that grade.
            </div>
        </div>

        <div class='grid'>
            <label>Choose subject(s) for selected grade</label>
            <div id="subject_list" class="subject-grid">
            {subject_items}
            </div>
        </div>

        <!-- PIN + Payment + PoP -->
        <div class="grid two-col">
          <div>
            <label>Create a 5-digit PIN</label>
            <input
              name="pin"
              required
              maxlength="5"
              inputmode="numeric"
              placeholder="e.g. 12345"
              autocomplete="off"
            />
          </div>
        </div>

        <div class="card soft" id="payment-anchor">

            <label>Payment details</label>

            <div class="mini">
                Please pay your monthly EBTA fees via EFT using the details below, then tick the box to confirm payment and upload your Proof of Payment.
            </div>

            <ul class="mini" style="margin:6px 0 4px 14px;padding:0;">
                <li>Account holder: Ms MCB MOHALE</li>
                <li>Contact: 0649619653</li>
                <li>Account number: 2062604285</li>
                <li>Bank name: Capitec</li>
            </ul>

            <label class="payment-confirm">
                <input type="checkbox" id="paid_check" name="paid_check" />
                <span class="mini" id="payment_text">
                    Payment has been made and I will upload the Proof of Payment now.
                </span>
            </label>


            <div id="pop_section" style="margin-top:8px;display:none;">
                <label>Proof of Payment (1–2 files)</label>
                <input type="file"
                       name="pop"
                       accept=".pdf,.png,.jpg,.jpeg,.gif,.webp"
                       multiple />
            </div>
            
            <div style="margin-top:14px;">
                <label>Amount paid</label>
                <input
                    type="number"
                    name="amount_paid"
                    id="amount_paid"
                    inputmode="numeric"
                    min="0"
                    step="1"
                    placeholder="Enter amount paid for this month"
                    required
                />
                <div class="mini muted" id="amount_paid_hint" style="margin-top:4px;"></div>
            </div>

        </div>

               

        <div class='toolbar'>
            <button class='btn'>Submit Enrollment</button>
            <a class='btn secondary' href='{url_for('student_login')}'>Student login</a>
            <a class='btn secondary' href='{url_for('tutor_login')}'>Tutor login</a>
        </div>
        </form>
    </div>
    </section>
    """

    extra_js = '''
    
<script>
let ebtaAllowExit = false;

window.addEventListener('beforeunload', function (e) {
    if (ebtaAllowExit) return;
    const message = 'Are you sure you want to leave this page?';
    e.preventDefault();
    e.returnValue = message;
    return message;
});
</script>
  
    
<script>
// Simple on-page popup function (toast/modal) used instead of alert()
function showPopup(message, type='info', timeout=4000){
    // type can be 'info','error','success'
    let container = document.getElementById('ebta-popup-container');
    if(!container){
        container = document.createElement('div');
        container.id = 'ebta-popup-container';
        container.style.position = 'fixed';
        container.style.right = '20px';
        container.style.top = '20px';
        container.style.zIndex = 99999;
        container.style.maxWidth = '320px';
        document.body.appendChild(container);
    }
    const el = document.createElement('div');
    el.className = 'ebta-popup ebta-popup-' + type;
    el.style.marginBottom = '10px';
    el.style.padding = '12px 14px';
    el.style.borderRadius = '8px';
    el.style.boxShadow = '0 2px 10px rgba(0,0,0,0.12)';
    el.style.background = type==='error' ? '#fdecea' : (type==='success' ? '#edf7ed' : '#eef3ff');
    el.style.color = '#111';
    el.textContent = message;
    container.appendChild(el);
    setTimeout(()=>{
        el.style.transition = 'opacity 0.3s ease';
        el.style.opacity = '0';
        setTimeout(()=> container.removeChild(el), 400);
    }, timeout);
}
</script>
<script>
    document.addEventListener('DOMContentLoaded', function(){
    const form = document.getElementById('reg_form');
    if (!form) return;

    const gradeSelect = document.getElementById('grade_select');
    const boxes = Array.from(
        form.querySelectorAll("input[type='checkbox'][name='subject_ids']")
    );

    const paidCheck = document.getElementById('paid_check');
    const popSection = document.getElementById('pop_section');
    const popInput = form.querySelector("input[type='file'][name='pop']");

    function updateSubjects() {
      const grade = gradeSelect.value;

      boxes.forEach(box => {
        const label = box.closest('.subject-item');
        if (!label) return;

        const g = label.getAttribute('data-grade');

        if (!grade) {
          label.classList.add('hidden');
          box.checked = false;
          return;
        }

        if (g === grade) {
          label.classList.remove('hidden');
        } else {
          label.classList.add('hidden');
          box.checked = false;
        }
      });
    }


    gradeSelect.addEventListener('change', updateSubjects);
    updateSubjects(); // initial

    if (paidCheck && popSection) {
        paidCheck.addEventListener('change', function(){
        if (this.checked) {
            popSection.style.display = 'block';
        } else {
            popSection.style.display = 'none';
            if (popInput) {
            popInput.value = '';
            }
        }
        });
    }

    form.addEventListener('submit', function(e){
    
        // ✅ allow exit without warning when submitting
        ebtaAllowExit = true;
        const grade = gradeSelect.value;

        // Validate phone numbers: ensure 10 digits for student and guardian WhatsApp numbers
        const studentPhoneInput = form.querySelector("input[name='phone']") || form.querySelector("input[name='phone_whatsap']") || form.querySelector("input[name='phone_whatsapp']");
        const guardianPhoneInput = form.querySelector("input[name='guardian_phone']") || form.querySelector("input[name='guardian']");
        function digitsOnly(str){ return (str||'').replace(/\\D/g,''); }
        if(studentPhoneInput){
            const sdigits = digitsOnly(studentPhoneInput.value);
            if(sdigits.length !== 10){
                e.preventDefault();
                showPopup('Student WhatsApp number must be exactly 10 digits.', 'error');
                studentPhoneInput.focus();
                return;
            }
        }
        if(guardianPhoneInput){
            const gdigits = digitsOnly(guardianPhoneInput.value);
            if(gdigits.length !== 10){
                e.preventDefault();
                showPopup('Guardian WhatsApp number must be exactly 10 digits.', 'error');
                guardianPhoneInput.focus();
                return;
            }
        }
        // Validate optional student email ends with @gmail.com if provided
        const emailInput = form.querySelector("input[name='email']") || form.querySelector("input[name='student_email']");
        if(emailInput && emailInput.value.trim() !== ''){
            if(!emailInput.value.trim().toLowerCase().endsWith('@gmail.com')){
                e.preventDefault();
                showPopup('Student Email (optional) must end with @gmail.com', 'error');
                emailInput.focus();
                return;
            }
        }
                if (!grade) {
        e.preventDefault();
        showPopup('Please choose a grade first.', 'error');;
        return;
        }
        const anyChecked = boxes.some(b => b.checked);
        if (!anyChecked) {
        e.preventDefault();
        showPopup('Please select at least one subject for the chosen grade.', 'error');;
        return;
        }

        if (!paidCheck || !paidCheck.checked) {
        e.preventDefault();
        showPopup('Please confirm that you have made payment before submitting, and then upload your Proof of Payment.', 'error');;
        return;
        }
        
        const amountInput = form.querySelector("#amount_paid");
        const amountHint = document.getElementById("amount_paid_hint");

        if (!amountInput || amountInput.value.trim() === "") {
            e.preventDefault();
            showPopup("Please enter the amount you paid for this month.", "error");
            amountInput.focus();
            return;
        }

        const paid = parseInt(amountInput.value, 10);
        const due = window.ebtaTotalDue || 0;

        if (paid !== due) {
            e.preventDefault();
            amountHint.textContent = `You need to pay R${due} to enroll for this month.`;
            showPopup(`Payment mismatch. Required amount is R${due}.`, "error");
            amountInput.focus();
            return;
        } else {
            amountHint.textContent = "";
        }

        
        if (!popInput || !popInput.files || popInput.files.length < 1 || popInput.files.length > 2) {
        e.preventDefault();
        showPopup('Please upload 1 or 2 Proof of Payment files.', 'error');;
        }
    });
    });

    // --- Fee calculation: display per-subject fee and total dynamically ---
    (function(){
        function feeForGrade(g){
            if(!g) return 0;
            if(g==='G12') return 250;
            if(g==='G13') return 350;
            if(g==='G10' || g==='G11') return 200;
            if(g==='G8' || g==='G9') return 200;
            return 200;
        }

        function updateFees(){
            const grade = document.getElementById('grade_select')?.value || '';
            const boxes = Array.from(
                document.querySelectorAll("input[type='checkbox'][name='subject_ids']")
            );

            const selected = boxes.filter(b =>
                b.checked &&
                b.closest('label') &&
                b.closest('label').getAttribute('data-grade') === grade
            );

            const count = selected.length;
            const per = feeForGrade(grade);
            const subtotal = per * count;

            let discount = 0;
            let discountLabel = '';

            if (count >= 3) {
                if (grade === 'G13') {
                    discount = Math.round(subtotal * 0.10);
                    discountLabel = `
                        <div style="color:#065f46; margin-top:4px;">
                            Multi-subject discount (10%): <strong>-R${discount}</strong>
                        </div>
                    `;
                } else {
                    discount = Math.round(subtotal * 0.05);
                    discountLabel = `
                        <div style="color:#065f46; margin-top:4px;">
                            Multi-subject discount (5%): <strong>-R${discount}</strong>
                        </div>
                    `;
                }
            }

            const total = subtotal - discount;
            window.ebtaTotalDue = total;

            let feeBox = document.getElementById('fee_summary');
            if (!feeBox) {
                feeBox = document.createElement('div');
                feeBox.id = 'fee_summary';
                feeBox.style.marginTop = '10px';

                const anchor = document.getElementById('payment-anchor');
                if (anchor) {
                    feeBox.style.marginBottom = '12px';
                    anchor.appendChild(feeBox);
                }
            }

            feeBox.innerHTML = `
                <div class='mini' style="
                    font-size:15px;
                    font-weight:600;
                    color:#0f172a;
                    padding:12px;
                    border:2px solid #1b5e20;
                    border-radius:12px;
                    background:#f0fdf4;
                ">
                    Per-subject fee: <strong>R${per}</strong><br>
                    Subjects selected: <strong>${count}</strong><br>
                    Subtotal: <strong>R${subtotal}</strong>
                    ${discountLabel}
                    <div style="margin-top:6px;">
                        Total due for this month:
                        <span style="font-size:18px; font-weight:800; color:#1b5e20;">
                            R${total}
                        </span>
                    </div>
                </div>
            `;
        }

        document.addEventListener('change', function(e){
            if(e.target && (e.target.name==='subject_ids' || e.target.id==='grade_select')){
                updateFees();
            }
        });

        document.addEventListener('DOMContentLoaded', updateFees);
    })();

    
    </script>'''

    

    # small helper to show a modal message instead of alert()
    extra_js += '''
    <script>
    function showProceedModal(message){
      // create simple centered modal
      if(document.getElementById('ebta-proceed-modal')) return;
      const ov = document.createElement('div');
      ov.id = 'ebta-proceed-modal';
        ov.style.position='fixed';
        ov.style.inset='0';
        ov.style.display='flex';
        ov.style.alignItems='center';
        ov.style.justifyContent='center';
        ov.style.background='rgba(0,0,0,0.35)';
        ov.style.zIndex='10000';
        const box = document.createElement('div');
        box.style.maxWidth='480px';
        box.style.padding='16px';
        box.style.borderRadius='10px';
        box.style.background='#fff';
        box.style.boxShadow='0 10px 30px rgba(0,0,0,0.2)';
        const h = document.createElement('div');
        h.style.marginBottom='12px';
        h.style.fontSize='16px';
        h.textContent = message || '';
        const btn = document.createElement('button');
        btn.textContent='OK';
        btn.className='btn';
        btn.onclick = function(){ document.getElementById('ebta-proceed-modal')?.remove(); };
        box.appendChild(h);
        box.appendChild(btn);
        ov.appendChild(box);
        document.body.appendChild(ov);
    }
    </script>
    '''

# --- Auto-inserted: 2026 registration popup (Yes / No) ---
    extra_js += '''<script>
    document.addEventListener('DOMContentLoaded', function(){
      // Create modal
      if (document.getElementById('ebta-reg-2026-modal')) return;
      const overlay = document.createElement('div');
      overlay.id = 'ebta-reg-2026-modal';
      overlay.style.position = 'fixed';
      overlay.style.inset = '0';
      overlay.style.background = 'rgba(0,0,0,0.45)';
      overlay.style.display = 'flex';
      overlay.style.alignItems = 'center';
      overlay.style.justifyContent = 'center';
      overlay.style.zIndex = '9999';

      const box = document.createElement('div');
      box.style.maxWidth = '520px';
      box.style.width = '92%';
      box.style.padding = '18px';
      box.style.borderRadius = '12px';
      box.style.boxShadow = '0 8px 32px rgba(0,0,0,0.25)';
      box.style.background = '#fff';
      box.style.fontFamily = 'Inter, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif';
      box.style.color = '#0f172a';

      const h = document.createElement('h2');
      h.textContent = 'Annual registration (2026)';
      h.style.margin = '0 0 8px';
      h.style.fontSize = '18px';

      const p = document.createElement('div');
      p.className = 'muted mini';
      p.style.marginBottom = '14px';
      p.style.color = '#334155';   // darker than muted, still soft
      p.style.fontWeight = '500';  // slight emphasis, not bold
      p.textContent = 'Have you filled in the Google Form after paying the R50 non-refundable registration fee?';


      const btnRow = document.createElement('div');
      btnRow.style.display = 'flex';
      btnRow.style.gap = '10px';
      btnRow.style.justifyContent = 'flex-end';

      const yesBtn = document.createElement('button');
      yesBtn.className = 'btn secondary';   // YES now looks like old NO
      yesBtn.textContent = 'YES';
      yesBtn.onclick = function(){
        if (typeof showProceedModal === 'function') {
          showProceedModal('You may proceed with the monthly enrollment.');
        }
        const modal = document.getElementById('ebta-reg-2026-modal');
        if(modal) modal.remove();
      };

      const noBtn = document.createElement('button');
      noBtn.className = 'btn success';      // NO now takes green emphasis
      noBtn.textContent = 'NO';
      noBtn.onclick = function(){
        ebtaAllowExit = true;
        window.location.href =          'https://docs.google.com/forms/d/e/1FAIpQLScCF4rLX81GxKDhuq2xk0rxYMEognlcytvqKqdLgvzpJ36I3A/viewform?usp=header';
      };

      btnRow.appendChild(yesBtn);
      btnRow.appendChild(noBtn);
      box.appendChild(h);
      box.appendChild(p);
      box.appendChild(btnRow);
      overlay.appendChild(box);
      document.body.appendChild(overlay);
    });
    </script>'''
    return page("EBTA Enrollment", body, extra_js=extra_js)



@app.post('/register')
def register():
    if get_setting('enrollment_open', '1') != '1':
        return page(
            "Enrollments Closed",
            card_msg(
                get_setting(
                    'enrollment_message',
                    'Enrollments are currently closed.'
                )
            )
        )
    full_name = request.form.get('full_name','').strip()
    phone = normalize_phone(request.form.get('phone',''))
    guardian = request.form.get('guardian','').strip()
    guardian_name = request.form.get('guardian_name','').strip()
    email = request.form.get('email','').strip() or None
    subject_ids = request.form.getlist('subject_ids')
    pin = request.form.get('pin','').strip()
    pops = request.files.getlist('pop')
    province = request.form.get('province')
    school = request.form.get('school')

    amount_paid = request.form.get('amount_paid', '').strip()

    try:
        amount_paid = int(amount_paid)
    except ValueError:
        return page("Error", card_msg("Invalid amount paid."))


    amount_paid = int(amount_paid)

    
    # Validation
    if not (full_name and phone and guardian and guardian_name and subject_ids and pin):
        return page("Error", card_msg("All fields are required."))

    if not is_valid_pin(pin):
        return page("Error", card_msg("PIN must be exactly 5 digits."))

    pops = [f for f in pops if f and f.filename]
    if len(pops) < 1 or len(pops) > 2:
        return page("Error", card_msg("Upload 1 or 2 Proof of Payment files."))

    conn = get_db()
    ensure_registration_table(conn)
    cur = conn.cursor()

    # Check existing student
    cur.execute("SELECT id, pin FROM students WHERE phone_whatsapp=?", (phone,))
    srow = cur.fetchone()

    if srow:
        if srow['pin'] != pin:
            conn.close()
            return page("Error", card_msg("Incorrect PIN for this phone number."))
        sid = srow['id']
    else:
        if pin_in_use(conn, pin):
            conn.close()
            return page("Error", card_msg("PIN already in use. Pick another."))

        # Derive grade from first subject
        cur.execute("SELECT grade FROM subjects WHERE id=?", (subject_ids[0],))
        r0 = cur.fetchone()
        if not r0:
            conn.close()
            return page("Error", card_msg("Invalid subject selection."))

        derived_grade = r0['grade']

        cur.execute("""
        INSERT INTO students (
            full_name,
            phone_whatsapp,
            guardian_phone,
            guardian_name,
            email,
            grade,
            pin,
            province,
            school,
            created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            full_name,
            phone,
            guardian,
            guardian_name,
            email,
            derived_grade,
            pin,
            province,
            school,
            now_utc_iso()
        ))

        sid = cur.lastrowid

    # Save PoP files
    saved_paths = []
    ts = int(datetime.datetime.now().timestamp())
    for idx, pop in enumerate(pops, start=1):
        safe = f"{ts}_{idx}_{secure_name(pop.filename)}"
        dest = UPLOAD_DIR / safe
        pop.save(dest)
        saved_paths.append(f"/uploads/{safe}")

    # Annual registration (optional)
    try:
        year = datetime.date.today().strftime('%Y')
        if not student_registered_for_year(conn, sid, year):
            if request.form.get('paid_check'):
                cur.execute(
                    "INSERT INTO registrations(student_id,year,amount,created_at) VALUES(?,?,?,?)",
                    (sid, year, 50, now_utc_iso())
                )
    except Exception:
        pass

    month = get_setting('current_month', datetime.date.today().strftime('%Y-%m'))

    cur.execute("SELECT subject_id FROM enrollments WHERE student_id=? AND month=?", (sid, month))
    existing = {str(x['subject_id']) for x in cur.fetchall()}
    
    # Recalculate total server-side
    cur.execute(
        "SELECT grade FROM subjects WHERE id=?",
        (subject_ids[0],)
    )
    row = cur.fetchone()
    if not row:
        conn.close()
        return page("Error", card_msg("Invalid subject selection."))

    grade = row['grade']

    def fee_for_grade(g):
        if g == 'G13':
            return 350
        if g == 'G12':
            return 250
        return 200


    per = fee_for_grade(grade)
    count = len(subject_ids)
    subtotal = per * count

    # Discount rules
    if count >= 3:
        if grade == 'G13':
            discount = int(round(subtotal * 0.10))  # 10% for G13 (3+ subjects)
        else:
            discount = int(round(subtotal * 0.05))  # 5% for others (3+ subjects)
    else:
        discount = 0

    total_due = subtotal - discount


    if amount_paid != total_due:
        conn.close()
        return page(
            "Payment error",
            card_msg(f"You need to pay R{total_due} to enroll for this month.")
        )


    created = []

    for subid in subject_ids:
        if subid in existing:
            continue

        token = secrets.token_urlsafe(16)

        # 1️⃣ Insert enrollment FIRST
        cur.execute("""
        INSERT INTO enrollments(
            student_id,
            subject_id,
            month,
            status,
            payment_method,
            payment_ref,
            pop_url,
            amount_paid,
            status_token,
            created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            sid,
            subid,
            month,
            'PENDING',
            'EFT',
            None,              # temporary, updated below
            saved_paths[0],
            amount_paid,
            token,
            now_utc_iso()
        ))

        # 2️⃣ Now eid is valid
        eid = cur.lastrowid

        # 3️⃣ Generate reference AFTER eid exists
        payment_ref = f"EFT-{eid}-{int(datetime.datetime.now().timestamp())}"

        # 4️⃣ Update enrollment with reference
        cur.execute(
            "UPDATE enrollments SET payment_ref=? WHERE id=?",
            (payment_ref, eid)
        )

        # 5️⃣ Insert payment record
        cur.execute("""
        INSERT INTO payments(
            enrollment_id,
            amount,
            gateway,
            reference,
            result,
            timestamp
        ) VALUES (?,?,?,?,?,?)
        """, (
            eid,
            amount_paid,
            'EFT',
            payment_ref,
            'PENDING',
            now_utc_iso()
        ))

        # 6️⃣ Save PoP files
        for pth in saved_paths:
            cur.execute(
                "INSERT INTO enrollment_files(enrollment_id,file_path) VALUES(?,?)",
                (eid, pth)
            )

        created.append((eid, token))

    conn.commit()
    conn.close()

    if not created:
        return page("No change", card_msg("Already enrolled for selected subjects this month."))

    if len(created) == 1:
        eid, tok = created[0]
        return redirect(url_for('status', id=eid) + '?' + urlencode({'token': tok}))

    links = "".join(
        f"<li><a class='links' target='_blank' href='{url_for('status', id=e)}?{urlencode({'token': t})}'>Enrollment #{e}</a></li>"
        for e, t in created
    )

    return page("Submitted", f"""
    <section class='wrap small'>
        <div class='card'>
            <h1>Registration submitted</h1>
            <ul>{links}</ul>
        </div>
    </section>
    """)



@app.get('/admin/registered')
def admin_registered():
    r = require_admin()
    if r: return r
    conn = get_db(); cur = conn.cursor()
    ensure_registration_table(conn)
    # get distinct years from registrations, fallback to current year
    cur.execute("SELECT DISTINCT year FROM registrations ORDER BY year DESC")
    years = [r['year'] for r in cur.fetchall()] or [datetime.date.today().strftime('%Y')]
    selected_year = request.args.get('year') or (years[0] if years else datetime.date.today().strftime('%Y'))
    # counts and listing
    cur.execute("SELECT COUNT(*) AS c FROM registrations WHERE year=?", (selected_year,))
    total = cur.fetchone()['c']
    cur.execute("SELECT r.id, r.student_id, r.amount, r.created_at, s.full_name, s.phone_whatsapp, s.grade FROM registrations r JOIN students s ON s.id=r.student_id WHERE r.year=? ORDER BY s.full_name", (selected_year,))
    rows = cur.fetchall()
    conn.close()
    year_options = ''.join([f"<option value='{y}' {'selected' if y==selected_year else ''}>{y}</option>" for y in years])
    rows_html = ''
    if not rows:
        rows_html = "<div class='empty'>No registrations for this year.</div>"
    else:
        rrows = []
        for rr in rows:
            when = rr['created_at'][:16].replace('T',' ')
            rrows.append(f"<tr><td>{rr['full_name']}</td><td>{rr['phone_whatsapp']}</td><td>{grade_label(rr['grade'])}</td><td>R{rr['amount']}</td><td>{when}</td></tr>")
        rows_html = (
            "<div class='scroll-x'>"
            "<table>"
            "<thead><tr>"
            "<th>Student</th><th>Phone</th><th>Grade</th><th>Amount</th><th>Registered at</th>"
            "</tr></thead>"
            f"<tbody>{''.join(rrows)}</tbody>"
            "</table></div>"
        )

    body = f"""
    <section class='grid'>
    <div class='card'>
        <h1>Registered students — {selected_year}</h1>
        <div class='toolbar'>
        <form method='get' action='{url_for('admin_registered')}' style='display:inline-block;margin-right:12px'>
            <label class='mini muted'>Filter by year</label>
            <select name='year' onchange='this.form.submit()'>
            {year_options}
            </select>
        </form>
        <div class='chip'>Total: {total}</div>
        </div>
        <div style='margin-top:12px'>{rows_html}</div>
    </div>
    </section>
    """
    return page('Registered students', body)

# ===================== Status page ==============
@app.get('/status/<int:id>')
def status(id: int):
    token = request.args.get('token')
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT e.*, s.full_name, s.phone_whatsapp, sub.name AS subject_name, sub.id AS subject_id
        FROM enrollments e
        JOIN students s ON s.id = e.student_id
        JOIN subjects sub ON sub.id = e.subject_id
        WHERE e.id = ?
    """, (id,))
    e = cur.fetchone()

    if not e:
        conn.close()
        return page("Not found", card_msg("Enrollment not found."))

    if token and token != e['status_token']:
        conn.close()
        return page("Forbidden", card_msg("Invalid token."))

    cur.execute("""
        SELECT invite_link 
        FROM groups 
        WHERE subject_id = ? AND month = ? 
        ORDER BY id DESC 
        LIMIT 1
    """, (e['subject_id'], e['month']))
    g = cur.fetchone()

    cur.execute("""
        SELECT file_path 
        FROM enrollment_files 
        WHERE enrollment_id = ?
    """, (id,))
    pops = [r['file_path'] for r in cur.fetchall()]

    conn.close()

    gl = g['invite_link'] if g else None

    join = (
        f"<a class='btn success' target='_blank' href='{gl}'>Join WhatsApp Group</a>"
        if (e['status'] == 'ACTIVE' and gl)
        else (
            "<div class='muted mini'>"
            "<strong>Next steps:</strong> Once your enrollment is approved (usually within 7 days), "
            "you’ll be able to log in on the Student Portal using the phone number you used to enroll "
            "and your 5-digit PIN to access all classes and learning materials."
            "</div>"
        )
    )

    pop_list = (
        " • ".join([f"<a class='links' href='{p}' target='_blank'>PoP</a>" for p in pops])
        if pops else "—"
    )

    body = fr"""
    <a class='links' href='/'>← Back</a>
    <section class='grid'>
        <div class='card'>
            <h1>Hello {e['full_name']}</h1>
            <p class='muted'>Subject: {e['subject_name']} • Month: {pretty_month_label(e['month'])}</p>
            <p>Status: <span class='chip {e['status'].lower()}'>{e['status']}</span></p>
            <p class='mini muted'>Proof of Payment: {pop_list}</p>
            {join}
        </div>
    </section>
    """

    return page("Status", body)


# ===================== Student Portal (includes messaging & monthly ratings) ==============
@app.get('/student/login')
def student_login():
    if is_student():
        return redirect(url_for('student_home'))

    body = f"""
    <section class='wrap small'>
      <div class='card auth-card'>
        <h1>Student login</h1>

        <form method='post' action='{url_for('student_login_post')}' class='grid'>
            <div>
                <label>WhatsApp number</label>
                <input name='phone' required />
            </div>

            <div>
                <label>5-digit PIN</label>
                <input name='pin' required maxlength='5' minlength='5' />
            </div>

            <button class='btn success'>Login</button>
        </form>

        <div style="margin-top:14px; text-align:center">
            <a href="#" class="mini muted" id="helpToggle">
                Need help?
            </a>
        </div>

        <div id="forgotSection" style="display:none; margin-top:16px;">
            <hr/>
            <form method='post' action='{url_for('student_forgot_pin')}' class='grid'>
                <div class='mini muted'>
                    Forgot your PIN? Enter your WhatsApp number and we’ll notify the admin.
                </div>
                <div>
                    <label>WhatsApp number</label>
                    <input name='phone' required />
                </div>
                <button class='btn secondary'>Notify Admin</button>
            </form>
        </div>
      </div>
    </section>
    """

    extra_js = """
    <script>
      document.addEventListener('DOMContentLoaded', function(){
        const help = document.getElementById('helpToggle');
        const section = document.getElementById('forgotSection');

        if(help && section){
            help.addEventListener('click', function(e){
                e.preventDefault();
                section.style.display =
                    section.style.display === 'none' ? 'block' : 'none';
            });
        }
      });
    </script>
    """

    return page("Student Login", body, extra_js=extra_js)


@app.post('/student/login')
def student_login_post():
    phone = normalize_phone(request.form.get('phone',''))
    pin=request.form.get('pin','').strip()
    conn=get_db(); cur=conn.cursor()
    cur.execute("SELECT id,pin,full_name FROM students WHERE phone_whatsapp=?", (phone,))
    row=cur.fetchone(); conn.close()
    if not row or not row['pin'] or row['pin']!=pin:
        return page("Login failed", card_msg("Wrong phone or PIN."))
    session['student_id']=row['id']; session['student_name']=row['full_name']
    return redirect(url_for('student_home'))

@app.post('/student/forgot-pin')
def student_forgot_pin():
    phone = normalize_phone(request.form.get('phone',''))
    if not phone: return page("Error", card_msg("Phone required."))
    conn=get_db(); cur=conn.cursor()
    cur.execute("INSERT INTO messages(kind,payload,created_at) VALUES(?,?,?)",
                ('forgot_student_pin', f"phone={phone}", now_utc_iso()))
    conn.commit(); conn.close()
    return page("Submitted", card_msg("Request sent to Admin."))

@app.get('/student/logout')
def student_logout():
    session.pop('student_id', None)
    session.pop('student_name', None)
    session.pop('student_month', None)  # 🔑 clear month override
    return redirect(url_for('student_login'))


def get_active_month(role):
    """
    Returns the effective month for the current session.
    Falls back to admin global month if no override is set.
    """
    if role == 'student':
        return session.get('student_month') or get_setting('current_month')
    if role == 'tutor':
        return session.get('tutor_month') or get_setting('current_month')
    return get_setting('current_month')

@app.get('/student')
def student_home():

    r=require_student()
    if r: return r
    sid = is_student()
    month = get_active_month('student')

    conn=get_db(); cur=conn.cursor()
    
    # Determine year to show (use current system month year)
    system_month = get_setting('current_month')
    year = int(system_month.split('-')[0])

    all_months = all_months_for_year(year)

    # Months where student had at least one ACTIVE enrollment
    cur.execute("""
        SELECT DISTINCT month
        FROM enrollments
        WHERE student_id=? AND status='ACTIVE'
    """, (sid,))
    active_months = {r['month'] for r in cur.fetchall()}
    
    month_selector = f"""
    <form method="post" action="{url_for('student_set_month')}" class="inlineform">
        <select name="month" onchange="this.form.submit()">
            {''.join(
                f"<option value='{m}' "
                f"{'selected' if m == month else ''}>"
                f"{pretty_month_label(m)}"
                f"{'' if m in active_months else ' (not enrolled)'}"
                f"</option>"
                for m in all_months
            )}
        </select>
    </form>
    """

    # Enrollments this month
    cur.execute("""
    SELECT e.subject_id, e.status, s.name AS subject_name, s.grade
    FROM enrollments e JOIN subjects s ON s.id=e.subject_id
    WHERE e.student_id=? AND e.month=? ORDER BY s.grade,s.name
    """,(sid,month))
    enrolls=cur.fetchall()
    active_sub_ids=[str(x['subject_id']) for x in enrolls if x['status']=='ACTIVE']
    has_active_enrollment = month in active_months
    
    enroll_cta = ""

    if month == system_month and not has_active_enrollment:
        enroll_cta = f"""
        <div class='card soft'>
            <h3>Not enrolled for {pretty_month_label(month)}</h3>
            <p class='muted'>
                Enrollments for this month are open. You can add subjects now.
            </p>
            <a class='btn' href='{url_for("home")}'>
                Enroll now
            </a>
        </div>
        """


    # WhatsApp links for enrolled subjects
    group_html="<div class='empty'>No group links yet.</div>"
    if has_active_enrollment and active_sub_ids:
        q = f"""
        SELECT g.subject_id, g.invite_link, s.name, s.grade
        FROM groups g
        JOIN subjects s ON s.id = g.subject_id
        WHERE g.month = 'ALL' AND g.is_visible=1
          AND g.subject_id IN ({','.join('?' * len(active_sub_ids))})
        ORDER BY s.grade, s.name
        """
        cur.execute(q, (*active_sub_ids,))

        
        gs=cur.fetchall()
        if gs:
            rows="".join([f"<tr><td>{grade_label(r['grade'])} — {r['name']}</td><td><a class='links' target='_blank' href='{r['invite_link']}'>Open WhatsApp</a></td></tr>" for r in gs])
            group_html=f'<div class="scroll-x"><table><thead><tr><th>Subject</th><th>Link</th></tr></thead><tbody>{rows}</tbody></table></div>'

    # Sessions + Meet link for enrolled subjects
    sessions_html="<div class='empty'>No sessions yet.</div>"
    if has_active_enrollment and active_sub_ids:
        q=f"""SELECT s.subject_id, sub.name AS subject_name, sub.grade, s.day_of_week, s.start_time, s.end_time, s.meet_link
            FROM sessions s JOIN subjects sub ON sub.id=s.subject_id
            WHERE s.active=1 AND s.subject_id IN ({','.join('?'*len(active_sub_ids))})
            ORDER BY s.day_of_week, s.start_time"""
        cur.execute(q, (*active_sub_ids,))
        sess=cur.fetchall()
        if sess:
            rows=[]
            for r in sess:
                meet = f"<a class='links' target='_blank' href='{r['meet_link']}'>Join</a>" if r['meet_link'] else "—"
                rows.append(f"<tr><td>{grade_label(r['grade'])} — {r['subject_name']}</td><td>{DOW[r['day_of_week']]} {r['start_time']}-{r['end_time']}</td><td>{meet}</td></tr>")
            rows_html = "".join(rows)

            sessions_html = f"""
            <div class="scroll-x">
                <table>
                    <thead>
                        <tr>
                            <th>Subject</th>
                            <th>When</th>
                            <th>Meet</th>
                        </tr>
                    </thead>
                    <tbody>
                        {rows_html}
                    </tbody>
                </table>
            </div>
            """


    # Materials & Assignments list (with upload timestamp)
    materials_html="<div class='empty'>No materials yet.</div>"
    assignments=[]; normal=[]
    tutors_for_subject={}
    if has_active_enrollment and active_sub_ids:
        # get tutors for each active subject (for messaging)
        cur.execute(f"""SELECT ts.subject_id, t.id AS tutor_id, t.full_name
                        FROM tutor_subjects ts JOIN tutors t ON t.id=ts.tutor_id
                        WHERE ts.subject_id IN ({','.join('?'*len(active_sub_ids))})""", (*active_sub_ids,))
        for row in cur.fetchall():
            tutors_for_subject.setdefault(row['subject_id'], []).append((row['tutor_id'], row['full_name']))

        q=f"""SELECT m.*, sub.name AS subject_name, sub.grade, t.full_name AS tutor_name
            FROM materials m
            JOIN subjects sub ON sub.id=m.subject_id
            JOIN tutors t ON t.id=m.tutor_id
            WHERE m.month=? AND m.subject_id IN ({','.join('?'*len(active_sub_ids))})
            ORDER BY m.created_at DESC"""
        cur.execute(q,(month,*active_sub_ids)); mats=cur.fetchall()
        if mats:
            for m in mats:
                is_ass = (m['is_assignment']==1 or m['kind']=='assignment')
                when = m['created_at'][:16].replace('T',' ')
                link = f"<a class='links' target='_blank' href='{m['file_path']}'>Download</a>" if m['kind'] in ('file','assignment') and m['file_path'] else f"<a class='links' target='_blank' href='{m['youtube_url']}'>Open</a>"
                row = (m, f"<tr><td>{grade_label(m['grade'])} — {m['subject_name']}</td><td>{m['title']} {'<span class=\"badge\">assignment</span>' if is_ass else ''}</td><td>{m['tutor_name']}</td><td>{when}</td><td>{link}</td></tr>")
                (assignments if is_ass else normal).append(row)
            def pack(rows):
                rows_html = "".join(r[1] for r in rows)

                return f"""
                <div class="scroll-x">
                  <table>
                    <thead>
                      <tr>
                        <th>Subject</th>
                        <th>Title</th>
                        <th>Tutor</th>
                        <th>Uploaded</th>
                        <th>Link</th>
                      </tr>
                    </thead>
                    <tbody>
                      {rows_html}
                    </tbody>
                  </table>
                </div>
                """
            materials_html = (("<h3>Assignments</h3>"+pack(assignments)) if assignments else "") + (("<h3>Materials</h3>"+pack(normal)) if normal else "")

    # Assignment submission blocks (top priority)
    submit_blocks=[]
    if assignments:
        for m,_ in assignments:
            due = m['due_date'] or ''
            # submission status
            cur.execute("SELECT id,file_path,mark,feedback,submitted_at FROM submissions WHERE material_id=? AND student_id=?", (m['id'], sid))
            sub = cur.fetchone()
            maxp = m['max_points'] if m['max_points'] else 100
            if sub:
                mark = f" • Mark: {sub['mark']} / {maxp}" if sub['mark'] is not None else ""
                fb = f"<div class='muted mini'>Feedback: {sub['feedback']}</div>" if sub['feedback'] else ""
                submit_blocks.append(f"<div class='card'><b>{m['title']}</b> — {grade_label(m['grade'])} {m['subject_name']} • Due: {due or '—'}<br/>Submitted: {sub['submitted_at'][:16].replace('T',' ')}{mark}{fb} <a class='links' href='{sub['file_path']}' target='_blank'>Download your file</a></div>")
            else:
                allow=True
                if due:
                    try:
                        end=datetime.datetime.fromisoformat(due+"T23:59:59+00:00")
                        allow = datetime.datetime.now(datetime.timezone.utc) <= end
                    except Exception: pass
                if allow:
                    submit_blocks.append(f"""
                    <div class='card'>
                        <b>{m['title']}</b> — {grade_label(m['grade'])} {m['subject_name']} • Due: {due or '—'} • Total: {maxp}
                        <form method='post' action='{url_for('student_submit_assignment', mid=m['id'])}' enctype='multipart/form-data' class='grid' style='grid-template-columns:1fr auto;gap:10px;margin-top:8px'>
                        <input type='file' name='file' required accept='.pdf,.doc,.docx,.png,.jpg,.jpeg,.zip,.txt'/>
                        <button class='btn'>Submit</button>
                        </form>
                    </div>""")
                else:
                    submit_blocks.append(f"<div class='card'><b>{m['title']}</b> — Due: {due} <span class='chip'>Closed</span></div>")

    # Feedback & Results (graded items)
    feedback_card = ""
    cur.execute("""SELECT m.title, m.max_points, s2.name AS subject_name, s2.grade,
                        sub.mark, sub.feedback, sub.evaluated_at
                FROM submissions sub
                JOIN materials m ON m.id=sub.material_id
                JOIN subjects s2 ON s2.id=m.subject_id
                WHERE sub.student_id=? AND sub.mark IS NOT NULL
                ORDER BY sub.evaluated_at DESC LIMIT 50""", (sid,))
    graded = cur.fetchall()
    if graded:
        items = []
        for g in graded:
            when = (g['evaluated_at'] or '')[:16].replace('T',' ')
            maxp = g['max_points'] if g['max_points'] else 100
            fb = f"<div class='muted mini' style='margin-top:4px'>{g['feedback']}</div>" if g['feedback'] else ""
            items.append(
                f"<div class='feedback-item'><div class='feedback-title'>{g['title']} — "
                f"{grade_label(g['grade'])} {g['subject_name']}</div>"
                f"<div>Mark: <span class='badge'>{g['mark']} / {maxp}</span> <span class='muted mini'>• {when}</span></div>"
                f"{fb}</div>"
            )
        feedback_card = f"<div class='card'><h2>Feedback & Results</h2><div class='feedback-list'>{''.join(items)}</div></div>"

    # Messages (compose to tutor + inbox)
    # Compose: pick "Tutor (Subject)"
    options=[]
    for subid in active_sub_ids:
        sid_int = int(subid)
        for (tid, tname) in tutors_for_subject.get(sid_int, []):
            # label: Tutor Name — Subject
            subj = next((f"{grade_label(e['grade'])} {e['subject_name']}" for e in enrolls if e['subject_id']==sid_int), "Subject")
            options.append((tid, sid_int, f"{tname} — {subj}"))
    msg_opts = "".join([f"<option value='{tid}|{sid_int}'>{label}</option>" for tid,sid_int,label in options]) or "<option value=''>No tutors available</option>"

    cur.execute("""SELECT dm.*, 
                        CASE dm.from_role 
                            WHEN 'tutor' THEN (SELECT full_name FROM tutors WHERE id=dm.from_id)
                            WHEN 'student' THEN (SELECT full_name FROM students WHERE id=dm.from_id)
                            ELSE 'Admin' END AS from_name,
                        CASE dm.to_role 
                            WHEN 'tutor' THEN (SELECT full_name FROM tutors WHERE id=dm.to_id)
                            WHEN 'student' THEN (SELECT full_name FROM students WHERE id=dm.to_id)
                            ELSE 'Admin' END AS to_name
                FROM direct_messages dm
                WHERE (to_role='student' AND to_id=?) OR (from_role='student' AND from_id=?)
                ORDER BY created_at ASC LIMIT 30""",(sid,sid))
    msgs = cur.fetchall()
    msg_list = "".join([f"<div class='msg {'me' if m['from_role']=='student' else 'them'}'><div class='meta'>{m['from_name']} → {m['to_name']} • {m['created_at'][:16].replace('T',' ')}</div><div>{m['body']}</div></div>" for m in msgs]) or "<div class='empty'>No messages yet.</div>"

    conn.close()

    # Enrollment list UI
    if enrolls:
        e_rows="".join([f"<tr><td>{grade_label(r['grade'])} — {r['subject_name']}</td><td><span class='chip {r['status'].lower()}'>{r['status']}</span></td></tr>" for r in enrolls])
        enr_html=f'<div class="scroll-x"><table><thead><tr><th>Subject</th><th>Status</th></tr></thead><tbody>{e_rows}</tbody></table></div>'
    else:
        enr_html = f"""
        <div class='empty'>
            You were not enrolled for {pretty_month_label(month)}.
        </div>
        """

    # ===== Ratings block (24th to month-end) =====
    rate_card = ""
    if rating_window_open(month) and active_sub_ids:
        # Fetch existing ratings this month for prefill
        cur2 = get_db().cursor()
        cur2.execute("""SELECT subject_id, rating, comment FROM lesson_ratings
                    WHERE student_id=? AND month=?""", (sid, month))
        previous = {r["subject_id"]:(r["rating"], r["comment"]) for r in cur2.fetchall()}
        cur2.connection.close()

        rows=[]
        for e in enrolls:
            if e['status'] != 'ACTIVE':
                continue
            sid_int = int(e['subject_id'])
            r0, c0 = previous.get(sid_int, (None, "")) if sid_int in previous else (None, "")
            rows.append(f"""
            <tr>
                <td>{grade_label(e['grade'])} — {e['subject_name']}</td>
                <td>
                <select name='rating_{sid_int}' required>
                    <option value='' {'selected' if not r0 else ''}>Select</option>
                    {''.join([f"<option value='{k}' {'selected' if r0==k else ''}>{k} ★</option>" for k in range(1,6)])}
                </select>
                </td>
                <td><input name='comment_{sid_int}' placeholder='Optional comment' value="{(c0 or '').replace('"','&quot;')}"/></td>
            </tr>
            """)

        rate_card = f"""
        <div class='card'>
            <h2>Rate your classes for {month}</h2>
            <p class='muted mini'>This is open from the 24th to the end of the month. 1 ★ (poor) → 5 ★ (excellent).</p>
            <form method='post' action='{url_for('student_submit_ratings')}'>
            <div class="scroll-x">
                <table>
                    <thead><tr><th>Subject</th><th>Rating</th><th>Comment</th></tr></thead>
                    <tbody>{''.join(rows)}</tbody>
                </table>
            </div>
            <div class='toolbar'><button class='btn'>Save ratings</button></div>
            </form>
        </div>
        """

    compose_block = f"""
    <div class='card'><h2>Messages</h2>
        <form method='post' action='{url_for('student_send_message')}' class='grid'>
        <div><label>To Tutor</label><select name='combo' required>{msg_opts}</select></div>
        <div><label>Your message</label><textarea name='body' required placeholder='Type your message...'></textarea></div>
        <button class='btn'>Send</button>
        </form>
        <div style='margin-top:10px'>{msg_list}</div>
    </div>
    """


    body=fr"""
    <section class='grid'>
    <div class='card'>
        <h1>Welcome, {session.get('student_name','Student')}</h1>
            <p class='muted'>
                Viewing: {pretty_month_label(month)} {month_selector}
            </p>

        <h2>Your Enrollments</h2>
        {enr_html}
        {enroll_cta}

        <p class='mini muted'>To add more subjects, submit the Home form again with your phone number and the new subjects + PoP.</p>
    </div>

    <div class='card'><h2>WhatsApp Group Links</h2>{group_html}</div>
    <div class='card'><h2>Sessions</h2>{sessions_html}</div>
    <div class='card'><h2>Materials & Assignments</h2><div class='scroll-x'>{materials_html}</div></div>
{(''.join(submit_blocks)) if submit_blocks else ''}

    {feedback_card}
    {rate_card}
    {compose_block}
    </section>"""
    return page("Student Portal", body)


@app.post('/student/set-month')
def student_set_month():
    r = require_student()
    if r:
        return r

    month = request.form.get('month')
    if not month:
        return redirect(url_for('student_home'))

    # Always allow switching month
    session['student_month'] = month
    return redirect(url_for('student_home'))
    
def all_months_for_year(year: int):
    """
    Returns ['YYYY-01', 'YYYY-02', ..., 'YYYY-12']
    """
    return [f"{year}-{m:02d}" for m in range(1, 13)]
    

@app.post('/tutor/set-month')
def tutor_set_month():
    r = require_tutor()
    if r: return r

    month = request.form.get('month')
    session['tutor_month'] = month

    return redirect(url_for('tutor_home'))


@app.post('/student/assignment/<int:mid>/submit')
def student_submit_assignment(mid:int):
    r=require_student()
    if r: return r
    sid=is_student(); file=request.files.get('file')
    if not file or not file.filename: return page("Error", card_msg("File required."))
    conn=get_db(); cur=conn.cursor()
    month=get_setting('current_month')
    cur.execute("SELECT subject_id,is_assignment,kind,month,due_date FROM materials WHERE id=?", (mid,))
    m=cur.fetchone()
    if not m or (m['kind']!='assignment' and m['is_assignment']!=1) or m['month']!=month:
        conn.close(); return page("Error", card_msg("Assignment not available."))
    cur.execute("SELECT 1 FROM enrollments WHERE student_id=? AND subject_id=? AND month=? AND status='ACTIVE'", (sid, m['subject_id'], month))
    if not cur.fetchone():
        conn.close(); return page("Error", card_msg("You are not ACTIVE in this subject."))
    if m['due_date']:
        try:
            end=datetime.datetime.fromisoformat(m['due_date']+"T23:59:59+00:00")
            if datetime.datetime.now(datetime.timezone.utc) > end:
                conn.close(); return page("Closed", card_msg("Submission window has closed."))
        except Exception: pass
    safe=f"{int(datetime.datetime.now().timestamp())}_{sid}_{secure_name(file.filename)}"
    dest=SUBMISSIONS_DIR/safe; file.save(dest)
    path=f"/submission-files/{safe}"
    now=now_utc_iso()
    cur.execute("INSERT OR REPLACE INTO submissions(material_id,student_id,file_path,submitted_at) VALUES(?,?,?,?)",
                (mid, sid, path, now))
    conn.commit(); conn.close()
    return page("Submitted", card_msg("Your assignment was submitted."))

# Student → Tutor message
@app.post('/student/message')
def student_send_message():
    r=require_student()
    if r: return r
    sid=is_student()
    combo=request.form.get('combo','')
    body=request.form.get('body','').strip()
    if not (combo and body): return page("Error", card_msg("Choose a tutor and write a message."))
    try:
        tutor_id_str, subject_id_str = combo.split('|',1)
        tutor_id=int(tutor_id_str); subject_id=int(subject_id_str)
    except Exception:
        return page("Error", card_msg("Bad selection."))
    conn=get_db(); cur=conn.cursor()
    # verify student is ACTIVE in subject and tutor teaches that subject
    month=get_setting('current_month')
    cur.execute("SELECT 1 FROM enrollments WHERE student_id=? AND subject_id=? AND month=? AND status='ACTIVE'", (sid,subject_id,month))
    if not cur.fetchone():
        conn.close(); return page("Error", card_msg("You are not ACTIVE in that subject."))
    cur.execute("SELECT 1 FROM tutor_subjects WHERE tutor_id=? AND subject_id=?", (tutor_id,subject_id))
    if not cur.fetchone():
        conn.close(); return page("Error", card_msg("Tutor not assigned to that subject."))
    cur.execute("INSERT INTO direct_messages(from_role,from_id,to_role,to_id,subject_id,body,created_at) VALUES('student',?,?,?,?,?,?)",
                (sid,'tutor',tutor_id,subject_id,body,now_utc_iso()))
    conn.commit(); conn.close()
    return redirect(url_for('student_home'))

# Student: submit monthly ratings
@app.post('/student/ratings')
def student_submit_ratings():
    r = require_student()
    if r: return r
    sid = is_student()
    month = get_setting('current_month')
    if not rating_window_open(month):
        return page("Closed", card_msg("The rating window is not open."))
    conn = get_db(); cur = conn.cursor()
    cur.execute("""SELECT subject_id FROM enrollments
                WHERE student_id=? AND month=? AND status='ACTIVE'""", (sid, month))
    subids = [row['subject_id'] for row in cur.fetchall()]
    now = now_utc_iso()
    for subid in subids:
        rkey = f"rating_{subid}"
        ckey = f"comment_{subid}"
        raw = request.form.get(rkey, "").strip()
        if not raw:
            continue
        try:
            rating = int(raw)
        except Exception:
            continue
        if rating < 1 or rating > 5:
            continue
        comment = request.form.get(ckey, "").strip() or None
        cur.execute("""INSERT INTO lesson_ratings(student_id,subject_id,month,rating,comment,created_at)
                    VALUES(?,?,?,?,?,?)
                    ON CONFLICT(student_id,subject_id,month)
                    DO UPDATE SET rating=excluded.rating, comment=excluded.comment, created_at=excluded.created_at""",
                    (sid, subid, month, rating, comment, now))
    conn.commit(); conn.close()
    return page("Thanks!", card_msg("Your ratings were saved."))


# ===================== Tutor Portal (includes messaging to student/admin) ==============
@app.get('/tutor/login')
def tutor_login():
    if is_tutor():
        return redirect(url_for('tutor_home'))

    body = f"""
    <section class='wrap small'>
      <div class='card auth-card'>
        <h1>Tutor login</h1>

        <form method='post' action='{url_for('tutor_login_post')}' class='grid'>
            <div>
                <label>Phone number</label>
                <input name='phone' required />
            </div>

            <div>
                <label>5-digit PIN</label>
                <input name='pin' required maxlength='5' minlength='5' />
            </div>

            <button class='btn success'>Login</button>
        </form>

        <div style="margin-top:14px; text-align:center">
            <a href="#" class="mini muted" id="tutorHelpToggle">
                Need help?
            </a>
        </div>

        <div id="tutorForgotSection" style="display:none; margin-top:16px;">
            <hr/>
            <form method='post' action='{url_for('tutor_forgot_pin')}' class='grid'>
                <div class='mini muted'>
                    Forgot your PIN? Enter your phone number and we’ll notify the admin.
                </div>
                <div>
                    <label>Phone number</label>
                    <input name='phone' required />
                </div>
                <button class='btn secondary'>Notify Admin</button>
            </form>
        </div>
      </div>
    </section>
    """

    extra_js = """
    <script>
      document.addEventListener('DOMContentLoaded', function(){
        const help = document.getElementById('tutorHelpToggle');
        const section = document.getElementById('tutorForgotSection');

        if(help && section){
            help.addEventListener('click', function(e){
                e.preventDefault();
                section.style.display =
                    section.style.display === 'none' ? 'block' : 'none';
            });
        }
      });
    </script>
    """

    return page("Tutor Login", body, extra_js=extra_js)

@app.post('/tutor/login')
def tutor_login_post():
    phone = normalize_phone(request.form.get('phone','')); pin=request.form.get('pin','').strip()
    conn=get_db(); cur=conn.cursor()
    cur.execute("SELECT id,pin,full_name FROM tutors WHERE phone=?", (phone,))
    row=cur.fetchone(); conn.close()
    if not row or not row['pin'] or row['pin']!=pin:
        return page("Login failed", card_msg("Wrong phone or PIN."))
    session['tutor_id']=row['id']; session['tutor_name']=row['full_name']
    return redirect(url_for('tutor_home'))

@app.post('/tutor/forgot-pin')
def tutor_forgot_pin():
    phone = normalize_phone(request.form.get('phone',''))
    if not phone: return page("Error", card_msg("Phone required."))
    conn=get_db(); cur=conn.cursor()
    cur.execute("INSERT INTO messages(kind,payload,created_at) VALUES(?,?,?)",
                ('forgot_tutor_pin', f"phone={phone}", now_utc_iso()))
    conn.commit(); conn.close()
    return page("Submitted", card_msg("Request sent to Admin."))

@app.get('/tutor/logout')
def tutor_logout():
    session.pop('tutor_id', None)
    session.pop('tutor_name', None)
    session.pop('tutor_month', None)  # 🔑 clear month override
    return redirect(url_for('tutor_login'))


@app.get('/tutor')
def tutor_home():
  
    r=require_tutor()
    if r: return r
    tid = is_tutor()
    month = get_active_month('tutor')

    conn=get_db(); cur=conn.cursor()
    
    system_month = get_setting('current_month')
    year = int(system_month.split('-')[0])
    all_months = all_months_for_year(year)

    
    cur.execute("""
        SELECT DISTINCT e.month
        FROM enrollments e
        JOIN tutor_subjects ts ON ts.subject_id = e.subject_id
        WHERE ts.tutor_id = ?
          AND e.status = 'ACTIVE'
    """, (tid,))
    active_months = {r['month'] for r in cur.fetchall()}


    month_selector = f"""
    <form method="post" action="{url_for('tutor_set_month')}" class="inlineform">
        <select name="month" onchange="this.form.submit()">
            {''.join(
                f"<option value='{m}' {'selected' if m == month else ''}>"
                f"{pretty_month_label(m)}"
                f"{'' if m in active_months else ' (no activity)'}"
                f"</option>"
                for m in all_months
            )}
        </select>
    </form>
    """

    # Assigned subjects
    cur.execute("""SELECT s.id AS subject_id, s.name AS subject_name, s.grade
                FROM tutor_subjects ts JOIN subjects s ON s.id=ts.subject_id
                WHERE ts.tutor_id=? ORDER BY s.grade,s.name""",(tid,))
    subs=cur.fetchall()
    assigned_list=", ".join([f"{grade_label(r['grade'])} — {r['subject_name']}" for r in subs]) or "<span class='muted'>No subjects assigned yet.</span>"

    # WhatsApp links for current month
    # WhatsApp group links (persistent, not month-based)
    sub_ids = [str(x['subject_id']) for x in subs]
    groups_html = "<div class='empty'>No group links yet.</div>"

    if sub_ids:
        q = f"""
            SELECT g.subject_id, g.invite_link, s.name, s.grade
            FROM groups g
            JOIN subjects s ON s.id = g.subject_id
            WHERE g.month = 'ALL' AND g.is_visible=1
              AND g.subject_id IN ({','.join('?' * len(sub_ids))})
            ORDER BY s.grade, s.name
        """
        cur.execute(q, sub_ids)
        groups = cur.fetchall()

        if groups:
            rows = "".join([
                f"""
                <tr>
                    <td>{grade_label(r['grade'])} — {r['name']}</td>
                    <td>
                        <a class='links' target='_blank' href='{r['invite_link']}'>
                            Open WhatsApp
                        </a>
                    </td>
                </tr>
                """
                for r in groups
            ])

            groups_html = f"""
            <div class="scroll-x">
                <table>
                    <thead>
                        <tr><th>Subject</th><th>Link</th></tr>
                    </thead>
                    <tbody>{rows}</tbody>
                </table>
            </div>
            """


    # Sessions for this tutor
    cur.execute("""SELECT se.id, se.subject_id, s.name AS subject_name, s.grade, se.day_of_week, se.start_time, se.end_time, se.meet_link
                FROM sessions se JOIN subjects s ON s.id=se.subject_id
                WHERE se.tutor_id=? AND se.active=1 ORDER BY se.day_of_week,se.start_time""",(tid,))
    sess=cur.fetchall()
    s_rows="".join([
        f"<tr><td>{grade_label(r['grade'])} — {r['subject_name']}</td>"
        f"<td>{DOW[r['day_of_week']]} {r['start_time']}-{r['end_time']}</td>"
        f"<td>{('<a class=\"links\" target=\"_blank\" href=\"'+r['meet_link']+'\">Meet</a>') if r['meet_link'] else '—'}</td>"
        f"<td><a class='links' href='{url_for('session_qr', id=r['id'])}'></a> · "
        f"<a class='links' href='{url_for('tutor_session_attendance', sid=r['id'])}'>Mark attendance</a></td></tr>"
        for r in sess
    ]) or "<tr><td colspan='4'><div class='empty'>No sessions yet.</div></td></tr>"

    # Upload form (assignments + due date + max points)
    subjects_options="".join([f"<option value='{r['subject_id']}'>{grade_label(r['grade'])} — {r['subject_name']}</option>" for r in subs]) or "<option value=''>No assigned subjects</option>"

    upload_block=f"""
    <div class='card'>
        <h2>Upload materials (Month: {month})</h2>
        <form method='post' action='{url_for('tutor_upload')}' enctype='multipart/form-data' class='grid'>
        <div><label>Subject</label><select name='subject_id' required>{subjects_options}</select></div>
        <div><label>Title</label><input name='title' required/></div>
        <div><label>Upload File</label><input type='file' name='file' accept='.pdf,.png,.jpg,.jpeg,.gif,.webp,.doc,.docx,.zip'/></div>
        <div><label>YouTube URL</label><input name='youtube' placeholder='https://youtube.com/...'/></div>
        <div class='grid' style='grid-template-columns:1fr 1fr 1fr;gap:10px'>
            <label style='display:flex;align-items:center;gap:8px'><input type='checkbox' name='is_assignment'/> Mark as assignment</label>
            <div><label>Due date (YYYY-MM-DD)</label><input name='due' placeholder='e.g. 2025-10-01'/></div>
            <div><label>Out of (default 100)</label><input name='max_points' type='number' min='1' max='1000' placeholder='100'/></div>
        </div>
        <button class='btn'>Save</button>
        <p class='muted mini'>Attach a file and/or paste a YouTube link. Assignments show first to students.</p>
        </form>
    </div>
    """

    # Your uploads (delete within 24h)
    cur.execute("""SELECT m.*, s.name AS subject_name, s.grade
                FROM materials m JOIN subjects s ON s.id=m.subject_id
                WHERE m.tutor_id=? ORDER BY m.created_at DESC LIMIT 200""",(tid,))
    mymats=cur.fetchall()
    def can_delete(ts):
        try:
            created=datetime.datetime.fromisoformat(ts)
            return (datetime.datetime.now(datetime.timezone.utc) - created) <= datetime.timedelta(hours=24)
        except Exception:
            return False
        
    rows = []
    for m in mymats:
        when = m['created_at'][:16].replace('T', ' ')

        # file or video link
        link = "—"
        if m['file_path']:
            link = f"<a class='links' target='_blank' href='{m['file_path']}'>Download</a>"
        elif m['youtube_url']:
            link = f"<a class='links' target='_blank' href='{m['youtube_url']}'>Open video</a>"

        # delete button (only within 24h)
        if can_delete(m['created_at']):
            action = f"""
            <form method="post"
                  action="{url_for('tutor_delete_material', mid=m['id'])}"
                  style="display:inline"
                  onsubmit="return confirm('Delete this upload?')">
                <button class="btn danger mini">Delete</button>
            </form>
            """
        else:
            action = "<span class='muted mini'>Locked</span>"

        rows.append(f"""
            <tr>
                <td>{grade_label(m['grade'])} — {m['subject_name']}</td>
                <td>{m['title']}</td>
                <td>{link}</td>
                <td>{when}</td>
                <td>{action}</td>
            </tr>
        """)

    uploads_html = (
        "<div class='empty'>No uploads yet.</div>"
        if not rows else
        f"""
        <div class="scroll-x">
            <table>
                <thead>
                    <tr>
                        <th>Subject</th>
                        <th>Title</th>
                        <th>File</th>
                        <th>Uploaded</th>
                        <th>Action</th>
                    </tr>
                </thead>
                <tbody>
                    {''.join(rows)}
                </tbody>
            </table>
        </div>    
        """
    )


    # Assignments you posted (manage submissions)
    cur.execute("""SELECT m.id, m.title, m.due_date, m.max_points, s.name AS subject_name, s.grade
                FROM materials m JOIN subjects s ON s.id=m.subject_id
                WHERE m.tutor_id=? AND (m.is_assignment=1 OR m.kind='assignment') ORDER BY m.created_at DESC""",(tid,))
    asg=cur.fetchall()
    asg_rows="".join([f"<tr><td>{grade_label(a['grade'])} — {a['subject_name']}</td><td>{a['title']}</td><td>Due: {a['due_date'] or '—'}</td><td>Total: {a['max_points'] or 100}</td><td><a class='links' href='{url_for('tutor_assignment_manage', mid=a['id'])}'>Manage</a></td></tr>" for a in asg]) or "<tr><td colspan='5'><div class='empty'>No assignments yet.</div></td></tr>"

    # Students overview per subject (attendance + avg mark) + simple "message a student" picker
    stu_sections=[]
    message_student_options=[]
    for s in subs:
        cur.execute("""SELECT st.id, st.full_name
                    FROM enrollments e JOIN students st ON st.id=e.student_id
                    WHERE e.subject_id=? AND e.month=? AND e.status='ACTIVE'
                    ORDER BY st.full_name""",(s['subject_id'], month))
        studs=cur.fetchall()
        cur.execute("SELECT COUNT(DISTINCT date) AS c FROM attendance a JOIN sessions se ON se.id=a.session_id WHERE se.subject_id=? AND strftime('%Y-%m', a.date)=?", (s['subject_id'], month))
        total_days = cur.fetchone()['c'] or 0
        rows=[]
        for st in studs:
            cur.execute("""SELECT COUNT(*) AS c FROM attendance a JOIN sessions se ON se.id=a.session_id
                        WHERE a.student_id=? AND se.subject_id=? AND strftime('%Y-%m', a.date)=?""",(st['id'], s['subject_id'], month))
            c=cur.fetchone()['c'] or 0
            rate = f"{int(round((c/total_days)*100))}%" if total_days>0 else "—"
            cur.execute("""SELECT AVG(mark) AS avgm FROM submissions sub
                        JOIN materials m ON m.id=sub.material_id
                        WHERE sub.student_id=? AND m.subject_id=? AND m.month=? AND sub.mark IS NOT NULL""",(st['id'], s['subject_id'], month))
            avgm = cur.fetchone()['avgm']
            rows.append(f"<tr><td>{st['full_name']}</td><td>{c}</td><td>{rate}</td><td>{'-' if avgm is None else int(round(avgm))}</td></tr>")
            message_student_options.append((st['id'], s['subject_id'], f"{st['full_name']} — {grade_label(s['grade'])} {s['subject_name']}"))
        table = (
            "<div class='empty'>No active students.</div>"
            if not rows
            else f"<div class='scroll-x'><table><thead><tr>"
                 f"<th>Student</th><th>Attendance</th><th>Rate</th><th>Avg mark</th>"
                 f"</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
        )

        stu_sections.append(f"<div class='card'><h3>{grade_label(s['grade'])} — {s['subject_name']}</h3>{table}</div>")

    # Tutor inbox
    cur.execute("""SELECT dm.*,
                        CASE dm.from_role 
                            WHEN 'tutor' THEN (SELECT full_name FROM tutors WHERE id=dm.from_id)
                            WHEN 'student' THEN (SELECT full_name FROM students WHERE id=dm.from_id)
                            ELSE 'Admin' END AS from_name,
                        CASE dm.to_role 
                            WHEN 'tutor' THEN (SELECT full_name FROM tutors WHERE id=dm.to_id)
                            WHEN 'student' THEN (SELECT full_name FROM students WHERE id=dm.to_id)
                            ELSE 'Admin' END AS to_name
                FROM direct_messages dm
                WHERE (to_role='tutor' AND to_id=?) OR (from_role='tutor' AND from_id=?)
                ORDER BY created_at ASC LIMIT 40""",(tid,tid))
    inbox = cur.fetchall()
    inbox_list = "".join([f"<div class='msg {'me' if m['from_role']=='tutor' else 'them'}'><div class='meta'>{m['from_name']} → {m['to_name']} • {m['created_at'][:16].replace('T',' ')}</div><div>{m['body']}</div></div>" for m in inbox]) or "<div class='empty'>No messages yet.</div>"

    # Compose forms
    stud_opts = "".join([f"<option value='{sid}|{subid}'>{label}</option>" for sid,subid,label in message_student_options]) or "<option value=''>No students</option>"

    inbox_card = f"""
    <div class='card'><h2>Inbox & Messages</h2>
        <div class='grid' style='grid-template-columns:1fr;gap:8px'>
        <form method='post' action='{url_for('tutor_message_student')}' class='grid'>
            <div><label>Message a student</label><select name='combo' required>{stud_opts}</select></div>
            <div><label>Your message</label><textarea name='body' required placeholder='Type your message...'></textarea></div>
            <button class='btn'>Send</button>
        </form>
        <form method='post' action='{url_for('tutor_message_admin')}' class='grid'>
            <div><label>Message Admin</label><textarea name='body' required placeholder='Type your message for Admin...'></textarea></div>
            <button class='btn secondary'>Send to Admin</button>
        </form>
        </div>
        <div style='margin-top:10px'>{inbox_list}</div>
    </div>
    """

    conn.close()


    body=fr"""
    <section class='grid'>
    <div class='card'>
        <h1>Welcome, {session.get('tutor_name','Tutor')}</h1>
        <p class='muted'>
            Viewing: {pretty_month_label(month)} {month_selector}
        </p>
        <div>{assigned_list}</div>
    </div>


    <div class='card'><h2>WhatsApp Group Links</h2>{groups_html}</div>

    <div class='card'><h2>Your sessions</h2>
        <div class="scroll-x"><table><thead><tr><th>Subject</th><th>When</th><th>Meet</th><th>Tools</th></tr></thead><tbody>{s_rows}</tbody></table></div>
    </div>

    {upload_block}

    <div class='card'><h2>Your uploads</h2>{uploads_html}</div>

    <div class='card'><h2>Your assignments</h2>
        <div class="scroll-x"><table><thead><tr><th>Subject</th><th>Title</th><th>Due</th><th>Total</th><th>Manage</th></tr></thead><tbody>{asg_rows}</tbody></table></div>
    </div>

    {inbox_card}

    {''.join(stu_sections)}
    </section>
    """
    return page("Tutor Portal", body)

@app.post('/tutor/upload')
def tutor_upload():
    r=require_tutor()
    if r: return r
    tid=is_tutor(); month=get_setting('current_month')
    subject_id=request.form.get('subject_id','').strip()
    title=request.form.get('title','').strip()
    youtube=request.form.get('youtube','').strip()
    file=request.files.get('file')
    is_assignment=1 if request.form.get('is_assignment')=='on' else 0
    due=request.form.get('due','').strip() or None
    max_points = request.form.get('max_points','').strip()
    try:
        max_points = int(max_points) if max_points else 100
    except Exception:
        max_points = 100
    if max_points < 1: max_points = 1
    if max_points > 1000: max_points = 1000
    if not (subject_id and title): return page("Error", card_msg("Subject and title required."))

    conn=get_db(); cur=conn.cursor()
    cur.execute("SELECT 1 FROM tutor_subjects WHERE tutor_id=? AND subject_id=?", (tid,subject_id))
    if not cur.fetchone():
        conn.close(); return page("Error", card_msg("This subject is not assigned to you."))
    file_path=None
    if file and file.filename:
        safe=f"{int(datetime.datetime.now().timestamp())}_{secure_name(file.filename)}"
        dest=MATERIALS_DIR/safe; file.save(dest); file_path=f"/materials-files/{safe}"
    if not (file_path or youtube):
        conn.close(); return page("Error", card_msg("Attach a file or provide a YouTube link."))

    now=now_utc_iso()
    kind = 'assignment' if is_assignment else ('file' if file_path else 'youtube')
    cur.execute("""INSERT INTO materials(subject_id,tutor_id,month,title,kind,file_path,youtube_url,created_at,is_assignment,due_date,max_points)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (subject_id, tid, month, title, kind, file_path, youtube if youtube else None, now, is_assignment, due, max_points))
    conn.commit(); conn.close()
    return page("Uploaded", card_msg("Saved. Students with ACTIVE enrollments will see it."))

@app.post('/tutor/materials/<int:mid>/delete')
def tutor_delete_material(mid:int):
    r=require_tutor()
    if r: return r
    tid=is_tutor()
    conn=get_db(); cur=conn.cursor()
    cur.execute("SELECT tutor_id,created_at FROM materials WHERE id=?", (mid,))
    m=cur.fetchone()
    if not m or m['tutor_id']!=tid:
        conn.close(); return page("Error", card_msg("Not found."))
    try:
        created=datetime.datetime.fromisoformat(m['created_at'])
        if (datetime.datetime.now(datetime.timezone.utc)-created) > datetime.timedelta(hours=24):
            conn.close(); return page("Locked", card_msg("You can only delete within 24 hours."))
    except Exception:
        pass
    cur.execute("DELETE FROM materials WHERE id=?", (mid,))
    conn.commit(); conn.close()
    return page("Deleted", card_msg("Upload removed."))

# Tutor: manage one assignment (submissions + grading)
@app.get('/tutor/assignment/<int:mid>')
def tutor_assignment_manage(mid:int):
    r=require_tutor()
    if r: return r
    tid=is_tutor()
    saved = request.args.get('saved')
    conn=get_db(); cur=conn.cursor()
    cur.execute("""SELECT m.*, s.name AS subject_name, s.grade
                FROM materials m JOIN subjects s ON s.id=m.subject_id
                WHERE m.id=? AND m.tutor_id=?""",(mid,tid))
    m=cur.fetchone()
    if not m:
        conn.close()
        return page("Not found", card_msg("Assignment not found."))
    total = m['max_points'] if m['max_points'] else 100

    # active students in subject (this month)
    month=get_setting('current_month')
    cur.execute("""SELECT st.id, st.full_name
                FROM enrollments e JOIN students st ON st.id=e.student_id
                WHERE e.subject_id=? AND e.month=? AND e.status='ACTIVE'
                ORDER BY st.full_name""",(m['subject_id'], month))
    studs=cur.fetchall()
    rows=[]
    for st in studs:
        cur.execute("SELECT id,file_path,submitted_at,mark,feedback FROM submissions WHERE material_id=? AND student_id=?", (mid, st['id']))
        sub=cur.fetchone()
        if sub:
            filelink=f"<a class='links' target='_blank' href='{sub['file_path']}'>download</a>"
            mark = '' if sub['mark'] is None else str(sub['mark'])
            rows.append(f"""
            <tr><td>{st['full_name']}</td><td>{filelink} <span class='muted mini'>({sub['submitted_at'][:16].replace('T',' ')})</span></td>
                <td>
                <form method='post' action='{url_for('tutor_assignment_grade', mid=mid, sid=st['id'])}' class='inlineform'>
                    <input type='number' name='mark' min='0' max='{total}' placeholder='0..{total}' value='{mark if mark else ""}' style='width:100px'/>
                    <input name='feedback' placeholder='Feedback' value='{sub['feedback'] or ""}'/>
                    <button class='btn mini'>Save</button>
                </form>
                </td></tr>""")
        else:
            rows.append(f"<tr><td>{st['full_name']}</td><td><span class='muted'>No submission</span></td><td>—</td></tr>")
    table = (
        "<div class='empty'>No students.</div>"
        if not rows
        else f"<div class='scroll-x'><table><thead><tr>"
             f"<th>Student</th><th>Submission</th><th>Grade (0..{total})</th>"
             f"</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
    )

    conn.close()

    js_alert = "<script>showPopup('Grade saved', 'success');;</script>" if saved else ""
    body=fr"""
    <a class='links' href='{url_for('tutor_home')}'>← Back</a>
    <section class='grid'>
        <div class='card'><h1>{m['title']}</h1>
        <p class='muted'>{grade_label(m['grade'])} — {m['subject_name']} • Due: {m['due_date'] or '—'} • Total: {total}</p>
        {table}
        </div>
    </section>
    """
    return page("Manage Assignment", body, extra_js=js_alert)

@app.post('/tutor/assignment/<int:mid>/grade/<int:sid>')
def tutor_assignment_grade(mid:int, sid:int):
    r=require_tutor()
    if r: return r
    raw_mark=request.form.get('mark','').strip()
    feedback=request.form.get('feedback','').strip() or None
    conn=get_db(); cur=conn.cursor()
    # fetch total
    cur.execute("SELECT max_points FROM materials WHERE id=?", (mid,))
    mrow = cur.fetchone()
    total = mrow['max_points'] if (mrow and mrow['max_points']) else 100
    mark=None
    if raw_mark != "":
        try:
            mark=int(raw_mark)
        except Exception:
            conn.close(); return page("Error", card_msg("Mark must be a number or blank."))
        if mark < 0 or mark > total:
            conn.close(); return page("Error", card_msg(f"Mark must be between 0 and {total}."))
    cur.execute("SELECT id FROM submissions WHERE material_id=? AND student_id=?", (mid, sid))
    row=cur.fetchone()
    if not row:
        conn.close(); return page("Error", card_msg("No submission to grade."))
    cur.execute("UPDATE submissions SET mark=?, feedback=?, evaluated_at=? WHERE id=?", (mark, feedback, now_utc_iso(), row['id']))
    conn.commit(); conn.close()
    # redirect with saved alert
    return redirect(url_for('tutor_assignment_manage', mid=mid, saved=1))

# Tutor → Student message
@app.post('/tutor/message-student')
def tutor_message_student():
    r=require_tutor()
    if r: return r
    tid=is_tutor()
    combo=request.form.get('combo','')
    body=request.form.get('body','').strip()
    if not (combo and body): return page("Error", card_msg("Choose a student and write a message."))
    try:
        student_id_str, subject_id_str = combo.split('|',1)
        student_id=int(student_id_str); subject_id=int(subject_id_str)
    except Exception:
        return page("Error", card_msg("Bad selection."))
    conn=get_db(); cur=conn.cursor()
    # verify tutor teaches subject and student is ACTIVE there
    month=get_setting('current_month')
    cur.execute("SELECT 1 FROM tutor_subjects WHERE tutor_id=? AND subject_id=?", (tid,subject_id))
    if not cur.fetchone():
        conn.close(); return page("Error", card_msg("You are not assigned to that subject."))
    cur.execute("""SELECT 1 FROM enrollments WHERE student_id=? AND subject_id=? AND month=? AND status='ACTIVE'""",(student_id,subject_id,month))
    if not cur.fetchone():
        conn.close(); return page("Error", card_msg("Student not ACTIVE in that subject this month."))
    cur.execute("INSERT INTO direct_messages(from_role,from_id,to_role,to_id,subject_id,body,created_at) VALUES('tutor',?,?,?,?,?,?)",
                (tid,'student',student_id,subject_id,body,now_utc_iso()))
    conn.commit(); conn.close()
    return redirect(url_for('tutor_home'))

# Tutor → Admin message
@app.post('/tutor/message-admin')
def tutor_message_admin():
    r=require_tutor()
    if r: return r
    tid=is_tutor()
    body=request.form.get('body','').strip()
    if not body: return page("Error", card_msg("Message is empty."))
    conn=get_db(); cur=conn.cursor()
    cur.execute("INSERT INTO direct_messages(from_role,from_id,to_role,to_id,subject_id,body,created_at) VALUES('tutor',?,'admin',0,NULL,?,?)",
                (tid, body, now_utc_iso()))
    conn.commit(); conn.close()
    return redirect(url_for('tutor_home'))

# Tutor: manual attendance (fixes missing route)
@app.route('/tutor/session/<int:sid>/attendance', methods=['GET','POST'])
def tutor_session_attendance(sid:int):
    r = require_tutor()
    if r:
        return r

    tid = is_tutor()
    conn = get_db()
    cur = conn.cursor()

    # Get current academic month (FIX)
    month = get_setting('current_month')

    # Session + subject
    cur.execute("""
        SELECT se.*, s.name AS subject_name, s.grade
        FROM sessions se
        JOIN subjects s ON s.id = se.subject_id
        WHERE se.id = ? AND se.tutor_id = ?
    """, (sid, tid))
    se = cur.fetchone()

    if not se:
        conn.close()
        return page("Not found", card_msg("Session not found."))

    # Active students for this subject + month
    cur.execute("""
        SELECT st.id, st.full_name
        FROM enrollments e
        JOIN students st ON st.id = e.student_id
        WHERE e.subject_id = ? AND e.month = ? AND e.status = 'ACTIVE'
        ORDER BY st.full_name
    """, (se['subject_id'], month))
    studs = cur.fetchall()

    # Date (default today)
    date_str = (
        request.form.get('date')
        if request.method == 'POST'
        else datetime.date.today().strftime('%Y-%m-%d')
    )

    if request.method == 'POST':
        present_ids = set(map(int, request.form.getlist('present')))

        # Reset attendance for that date/session
        cur.execute(
            "DELETE FROM attendance WHERE session_id = ? AND date = ?",
            (sid, date_str)
        )

        now = now_utc_iso()
        for st in studs:
            if st['id'] in present_ids:
                cur.execute("""
                    INSERT INTO attendance(session_id, student_id, date, created_at)
                    VALUES (?, ?, ?, ?)
                """, (sid, st['id'], date_str, now))

        conn.commit()
        conn.close()
        return page("Saved", card_msg("Attendance saved."))

    # GET: load existing attendance
    cur.execute(
        "SELECT student_id FROM attendance WHERE session_id = ? AND date = ?",
        (sid, date_str)
    )
    already = {row['student_id'] for row in cur.fetchall()}
    conn.close()

    rows = []
    for st in studs:
        chk = "checked" if st['id'] in already else ""
        rows.append(
            f"<tr><td>{st['full_name']}</td>"
            f"<td><input type='checkbox' name='present' value='{st['id']}' {chk}/></td></tr>"
        )

    table = (
        "<div class='empty'>No students.</div>"
        if not rows
        else f'<div class="scroll-x"><table><thead><tr><th>Student</th><th>Present</th></tr></thead>'
             f'<tbody>{"".join(rows)}</tbody></table></div>'
    )

    body = f"""
    <a class='links' href='{url_for('tutor_home')}'>← Back</a>
    <section class='card'>
        <h1>Mark attendance — {grade_label(se['grade'])} {se['subject_name']}</h1>
        <form method='post' class='grid'>
            <div>
                <label>Date (YYYY-MM-DD)</label>
                <input name='date' value='{date_str}' required/>
            </div>
            {table}
            <button class='btn'>Save</button>
        </form>
    </section>
    """
    return page("Attendance", body)


# ===================== Admin Portal (guardian/email in Students, DM, analytics) ==============
def card_msg(msg): return f"<section class='wrap small'><div class='card'><p>{msg}</p></div></section>"
def stat(title,value): return f"<div class='stat'><div class='muted'>{title}</div><div class='k'>{value}</div></div>"

@app.get('/admin/login')
def admin_login():
    if is_admin(): return redirect(url_for('admin_home'))
    body=fr"""<section class='wrap small'><div class='card auth-card'><h1>Admin login</h1>
    <form method='post' action='{url_for('admin_login_post')}' class='grid'>
        <div><label>Password</label><input type='password' name='pwd' required/></div>
        <button class='btn'>Login</button>
    </form></div></section>"""
    return page("Admin Login", body)

@app.post('/admin/login')
def admin_login_post():
    pwd = request.form.get('pwd', '')
    expected = os.environ.get('EBTA_ADMIN_PASSWORD')

    if not expected:
        return page("Error", card_msg("Admin password is not configured."))

    if pwd == expected:
        session['admin'] = True
        return redirect(url_for('admin_home'))

    return page("Error", card_msg("Wrong password."))


@app.get('/admin/logout')
def admin_logout(): session.clear(); return redirect(url_for('admin_login'))


def admin_nav():
    return f"""
    <nav class="admin-nav">
        <a class="btn secondary" href="{url_for('admin_home')}">Dashboard</a>
        <a class="btn secondary" href="{url_for('admin_enrollments')}">Enrollments</a>
        <a class="btn secondary" href="{url_for('admin_students')}">Students</a>
        <a class="btn secondary" href="{url_for('admin_tutors')}">Tutors</a>
        <a class="btn secondary" href="{url_for('admin_groups')}">Groups</a>
        <a class="btn secondary" href="{url_for('admin_sessions')}">Sessions</a>
        <a class="btn secondary" href="{url_for('admin_messages')}">Inbox</a>
        <a class="btn secondary" href="{url_for('admin_direct_messages')}">Direct Msgs</a>
        <a class="btn secondary" href="{url_for('admin_analytics')}">Analytics</a>
        <a class="btn secondary" href="{url_for('admin_settings')}">Settings</a>
    </nav>
    """


@app.get('/admin')
def admin_home():
    r=require_admin()
    if r: return r
    month=get_setting('current_month')
    conn=get_db(); cur=conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM enrollments WHERE month=?", (month,)); total=cur.fetchone()['c']
    counts={}
    for st in ["PENDING","ACTIVE","LAPSED"]:
        cur.execute("SELECT COUNT(*) AS c FROM enrollments WHERE month=? AND status=?", (month,st)); counts[st]=cur.fetchone()['c']
    cur.execute("SELECT COUNT(*) AS c FROM messages WHERE resolved=0"); msg_count=cur.fetchone()['c']
    # direct messages count to admin (unread)
    cur.execute("SELECT COUNT(*) AS c FROM direct_messages WHERE to_role='admin' AND is_read=0"); dm_unread = cur.fetchone()['c']
    conn.close()
    body=fr"""
    <section class='grid'><div class='stats'>
    {stat('Current month', month)}{stat('Total enrollments', str(total))}
    {stat('Pending', str(counts.get('PENDING',0)))}{stat('Active', str(counts.get('ACTIVE',0)))}
    {stat('Admin inbox', str(msg_count))}{stat('Direct msgs (unread)', str(dm_unread))}
    </div>
    <div class='toolbar'>
    <a class='btn secondary' href='{url_for('admin_enrollments')}'>Manage enrollments</a>
    <a class='btn secondary' href='{url_for('admin_students')}'>Students</a>
    <a class='btn secondary' href='{url_for('admin_tutors')}'>Tutors</a>
    <a class='btn secondary' href='{url_for('admin_groups')}'>Group links</a>
    <a class='btn secondary' href='{url_for('admin_sessions')}'>Sessions & QR</a>
    <a class='btn secondary' href='{url_for('admin_messages')}'>Inbox</a>
    <a class='btn secondary' href='{url_for('admin_direct_messages')}'>Direct messages</a>
    <a class='btn secondary' href='{url_for('admin_analytics')}'>Analytics</a>
    <a class='btn secondary' href='{url_for('admin_settings')}'>Settings</a>
    </div></section>"""
    return page("Admin", body)

# --- Admin: Enrollments (show all PoP files) ---

def format_datetime(dt_str):
    if not dt_str:
        return "—"
    try:
        dt = datetime.datetime.fromisoformat(dt_str)
        return dt.strftime("%d %b %Y, %H:%M")
    except Exception:
        return dt_str.replace("T", " ")[:16]


@app.get('/admin/enrollments')
def admin_enrollments():
    r = require_admin()
    if r:
        return r

    month = get_admin_active_month()
    conn = get_db()
    cur = conn.cursor()

    # Fetch enrollments + student + grade + subject
    cur.execute(
        """
        SELECT 
            e.*,
            st.full_name,
            st.phone_whatsapp,
            st.grade,
            sub.name AS subject_name
        FROM enrollments e
        JOIN students st ON st.id = e.student_id
        JOIN subjects sub ON sub.id = e.subject_id
        WHERE e.month = ?
        ORDER BY e.created_at ASC
        """,
        (month,),
    )

    rows = cur.fetchall()
    
    def enrollment_history_label(student_id, current_month):
        year = current_month.split('-')[0]

        cur.execute("""
            SELECT 1
            FROM enrollments
            WHERE student_id = ?
              AND status = 'ACTIVE'
              AND substr(month, 1, 4) = ?
              AND month < ?
            LIMIT 1
        """, (student_id, year, current_month))

        return "Returning student" if cur.fetchone() else "First month"


    # Proof of Payment helper
    def pop_cell(eid, legacy):
        cur.execute(
            "SELECT file_path FROM enrollment_files WHERE enrollment_id=?",
            (eid,),
        )
        files = [r['file_path'] for r in cur.fetchall()]
        if not files and legacy:
            files = [legacy]
        return " ".join(
            [f"<a class='links' target='_blank' href='{p}'>PoP</a>" for p in files]
        ) or "—"

    table_rows = ""
    for r in rows:
        history = enrollment_history_label(r['student_id'], month)

        table_rows += f"""
        <tr>
            <td>{r['full_name']}<div class='muted'>{r['phone_whatsapp']}</div></td>
            <td>{grade_label(r['grade'])}</td>
            <td>{r['subject_name']}</td>
            <td><span class='chip {r['status'].lower()}'>{r['status']}</span></td>
            <td><span class='mini muted'>{history}</span></td>
            <td>
                <span class='mini'>
                    {format_datetime(r['created_at'])}
                </span>
            </td>
            <td>{pop_cell(r['id'], r['pop_url'])}</td>

            <td><strong>R{r['amount_paid']}</strong></td>
            <td>
                <form method='post' action='{url_for('enrollment_action', id=r['id'], action='approve')}' style='display:inline'>
                    <button class='btn success'>Approve</button>
                </form>
                <form method='post' action='{url_for('enrollment_action', id=r['id'], action='lapse')}' style='display:inline'>
                    <button class='btn danger'>Lapse</button>
                </form>
            </td>
            <td>
                <a class='links' target='_blank'
                   href='{url_for('status', id=r['id'])}?{urlencode({'token': r['status_token']})}'>
                   open
                </a>
            </td>
        </tr>
        """


    conn.close()

    body = f"""
    {admin_nav()}

    <section class='card'>
        <h1>Enrollments — {month}</h1>

        <div class='toolbar'>
            <input id='enr_q' class='pill'
                   placeholder='Search by name, phone, grade, subject'
                   oninput="filterTable('enr_q','enr_tbl')"/>
        </div>

        <div class="scroll-x">
            <table id='enr_tbl'>
                <thead>
                    <tr>
                        <th>Student</th>
                        <th>Grade</th>
                        <th>Subject</th>
                        <th>Status</th>
                        <th>History</th>
                        <th>Timestamp</th>
                        <th>PoP</th>
                        <th>Amount paid</th>
                        <th>Actions</th>
                        <th>Status link</th>
                    </tr>
                </thead>
                <tbody>
                    {table_rows if table_rows else "<tr><td colspan='10'><div class='empty'>No enrollments yet.</div></td></tr>"}
                </tbody>
            </table>
        </div>
        
    </section>
    """

    return page("Enrollments", body)


@app.post('/admin/enrollments/<int:id>/<action>')
def enrollment_action(id: int, action: str):
    r = require_admin()
    if r:
        return r
    conn = get_db()
    cur = conn.cursor()

    notify_email = None
    notify_phone = None
    notify_name = None
    notify_pin = None
    notify_subject = None
    notify_grade = None
    notify_month = None

    if action == 'approve':
        # Activate enrollment
        cur.execute("UPDATE enrollments SET status='ACTIVE' WHERE id=?", (id,))

        # Student details + current PIN
        cur.execute("""
            SELECT st.id, st.full_name, st.phone_whatsapp, st.email, st.pin
            FROM students st
            JOIN enrollments e ON e.student_id = st.id
            WHERE e.id = ?
        """, (id,))
        srow = cur.fetchone()

        # Enrollment + subject details for the notification
        cur.execute("""
            SELECT e.month, sub.name AS subject_name, sub.grade
            FROM enrollments e
            JOIN subjects sub ON sub.id = e.subject_id
            WHERE e.id = ?
        """, (id,))
        erow = cur.fetchone()

        if srow:
            notify_name = srow["full_name"]
            notify_phone = srow["phone_whatsapp"]
            notify_email = srow["email"]
            notify_pin = srow["pin"]
            if erow:
                notify_month = erow["month"]
                notify_subject = erow["subject_name"]
                notify_grade = erow["grade"]

            # If the student does not have a PIN yet, generate one now
            if not notify_pin:
                pins = set()
                cur.execute("SELECT pin FROM students WHERE pin IS NOT NULL")
                pins |= {r['pin'] for r in cur.fetchall()}
                cur.execute("SELECT pin FROM tutors WHERE pin IS NOT NULL")
                pins |= {r['pin'] for r in cur.fetchall()}
                new_pin = gen_pin(pins)
                cur.execute("UPDATE students SET pin=? WHERE id=?", (new_pin, srow['id']))
                notify_pin = new_pin

    elif action == 'lapse':
        cur.execute("UPDATE enrollments SET status='LAPSED' WHERE id=?", (id,))

    conn.commit()
    conn.close()

    # --- Notifications: enrollment approved ---
    try:
        if action == 'approve' and notify_phone and notify_pin:
            base_url = (request.url_root or '').rstrip('/')
            portal_link = base_url
            login_link = base_url + url_for('student_login')

            month_label = pretty_month_label(notify_month) if notify_month else ""
            grade_label_txt = grade_label(notify_grade) if notify_grade else ""
            first_name = notify_name.split()[0] if notify_name else ""

            email_subject = "EBTA enrollment approved"
            email_body_lines = [
                f"Hi {notify_name},",
                "",
                "Your EBTA enrollment has been approved.",
            ]
            if grade_label_txt or notify_subject or month_label:
                detail = " ".join(x for x in [grade_label_txt, notify_subject, month_label] if x)
                if detail.strip():
                    email_body_lines.append(f"Subject/month: {detail}")
                    email_body_lines.append("")
            email_body_lines.extend([
                "Login details (keep these safe):",
                f"WhatsApp number: {notify_phone}",
                f"PIN: {notify_pin}",
                f"Portal: {portal_link}",
                f"Student login: {login_link}",
                "",
                "You can now log in to your EBTA portal to access materials, assignments, and WhatsApp links (where available).",
                "",
                "If you did not request this change, please contact EBTA support.",
            ])
            email_body = "\n".join(email_body_lines)

            sms_body_parts = [
                f"EBTA: Hi {first_name}, your enrollment is APPROVED.",
            ]
            if month_label or grade_label_txt or notify_subject:
                detail = " ".join(x for x in [grade_label_txt, notify_subject, month_label] if x)
                sms_body_parts.append(detail + ".")
            sms_body_parts.append(f"Login with WhatsApp {notify_phone} + PIN {notify_pin} at {login_link}.")
            sms_body = " ".join(sms_body_parts)

            if notify_email:
                send_email_notification(notify_email, email_subject, email_body)
            if notify_phone:
                send_sms_notification(notify_phone, sms_body)
    except Exception:
        # Never break the admin flow if notifications fail
        pass

    return redirect(url_for('admin_enrollments'))

# --- Admin: Students (show Guardian & Email) ---

@app.get('/admin/students')
def admin_students():
    r = require_admin()
    if r:
        return r

    conn = get_db()
    cur = conn.cursor()

    # 1. Fetch students
    cur.execute("""
        SELECT
            id,
            full_name,
            phone_whatsapp,
            guardian_phone,
            email,
            grade,
            province,
            school,
            pin
        FROM students
        ORDER BY created_at DESC
    """)
    rows = cur.fetchall()

    # 2. Second cursor for subjects
    cur2 = conn.cursor()

    def nz(v):
        return v if (v and str(v).strip()) else "N/A"

    trs = []

    # 3. Loop students
    for s in rows:
        # Fetch subjects for THIS student
        cur2.execute("""
            SELECT sub.name
            FROM enrollments e
            JOIN subjects sub ON sub.id = e.subject_id
            WHERE e.student_id = ?
        """, (s['id'],))

        subject_rows = cur2.fetchall()
        subjects = ", ".join([r['name'] for r in subject_rows]) if subject_rows else "N/A"

        pin = s['pin'] if s['pin'] else "<span class='muted'>not set</span>"

        trs.append(
            f"<tr>"
            f"<td>{s['full_name']}<div class='muted'>{s['phone_whatsapp']}</div></td>"
            f"<td>{grade_label(s['grade'])}</td>"
            f"<td>{subjects}</td>"
            f"<td>{nz(s['guardian_phone'])}</td>"
            f"<td>{nz(s['province'])}</td>"
            f"<td>{nz(s['school'])}</td>"
            f"<td>{nz(s['email'])}</td>"
            f"<td>{pin}</td>"
            f"<td>"
            f"<form method='post' action='{url_for('admin_student_reset_pin', sid=s['id'])}' style='display:inline'>"
            f"<button class='btn success'>Reset PIN</button></form> "
            f"<form method='post' action='{url_for('admin_student_delete', sid=s['id'])}' "
            f"style='display:inline' onsubmit='return confirm(\"Delete this student?\")'>"
            f"<button class='btn danger'>Delete</button></form>"
            f"</td>"
            f"</tr>"
        )

    conn.close()

    body = f"""
    {admin_nav()}
    <section class='card'>
        <h1>Students</h1>
        <div class='toolbar'>
            <input id='stu_q' class='pill' placeholder='Search students'
                   oninput="filterTable('stu_q','stu_tbl')"/>
        </div>

        <div class="scroll-x">
            <table id='stu_tbl'>
                <thead>
                    <tr>
                        <th>Student</th>
                        <th>Grade</th>
                        <th>Subject</th>
                        <th>Guardian</th>
                        <th>Province</th>
                        <th>School</th>
                        <th>Email</th>
                        <th>PIN</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody>
                    {''.join(trs) if trs else "<tr><td colspan='9'><div class='empty'>No students yet.</div></td></tr>"}
                </tbody>
            </table>
        </div>    
            
    </section>
    """

    return page("Students", body)


@app.post('/admin/students/add')
def admin_student_add():
    r = require_admin()
    if r:
        return r
    full_name = request.form.get('full_name','').strip()
    phone = normalize_phone(request.form.get('phone',''))
    grade = request.form.get('grade','').strip()
    email = request.form.get('email','').strip() or None
    if not (full_name and phone and grade):
        return page("Error", card_msg("Missing fields."))
    now = now_utc_iso()
    conn = get_db()
    cur = conn.cursor()
    pins = set()
    cur.execute("SELECT pin FROM students WHERE pin IS NOT NULL")
    pins |= {r['pin'] for r in cur.fetchall()}
    cur.execute("SELECT pin FROM tutors WHERE pin IS NOT NULL")
    pins |= {r['pin'] for r in cur.fetchall()}
    pin = gen_pin(pins)
    try:
        cur.execute("""
            INSERT INTO students(full_name,phone_whatsapp,guardian_phone,email,grade,pin,created_at)
            VALUES(?,?,?,?,?,?,?)
        """, (full_name, phone, None, email, grade, pin, now))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return page("Error", card_msg("Phone already exists."))
    conn.close()
    return redirect(url_for('admin_students'))

@app.post('/admin/students/<int:sid>/reset-pin')
def admin_student_reset_pin(sid:int):
    r = require_admin()
    if r:
        return r
    conn = get_db()
    cur = conn.cursor()
    pins = set()
    cur.execute("SELECT pin FROM students WHERE pin IS NOT NULL")
    pins |= {r['pin'] for r in cur.fetchall()}
    cur.execute("SELECT pin FROM tutors WHERE pin IS NOT NULL")
    pins |= {r['pin'] for r in cur.fetchall()}
    new_pin = gen_pin(pins)
    cur.execute("UPDATE students SET pin=? WHERE id=?", (new_pin, sid))
    conn.commit()
    conn.close()
    return page("PIN Updated", card_msg(f"Student PIN reset to: {new_pin}"))

@app.post('/admin/students/<int:sid>/delete')
def admin_student_delete(sid: int):
    r = require_admin()
    if r:
        return r

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("BEGIN")

        # 1. Delete attendance
        cur.execute("DELETE FROM attendance WHERE student_id=?", (sid,))

        # 2. Delete submissions
        cur.execute("DELETE FROM submissions WHERE student_id=?", (sid,))

        # 3. Delete lesson ratings
        cur.execute("DELETE FROM lesson_ratings WHERE student_id=?", (sid,))

        # 4. Delete enrollment files (important)
        cur.execute("""
            DELETE FROM enrollment_files
            WHERE enrollment_id IN (
                SELECT id FROM enrollments WHERE student_id=?
            )
        """, (sid,))

        # 5. Delete payments
        cur.execute("""
            DELETE FROM payments
            WHERE enrollment_id IN (
                SELECT id FROM enrollments WHERE student_id=?
            )
        """, (sid,))

        # 6. Delete enrollments
        cur.execute("DELETE FROM enrollments WHERE student_id=?", (sid,))

        # 7. Delete registrations
        cur.execute("DELETE FROM registrations WHERE student_id=?", (sid,))

        # 8. Delete messages
        cur.execute("""
            DELETE FROM direct_messages
            WHERE (from_role='student' AND from_id=?)
               OR (to_role='student' AND to_id=?)
        """, (sid, sid))

        # 9. Finally delete student
        cur.execute("DELETE FROM students WHERE id=?", (sid,))

        conn.commit()
    except Exception as e:
        conn.rollback()
        raise
    finally:
        conn.close()

    return redirect(url_for('admin_students'))


# --- Admin: Tutors ---

@app.get('/admin/tutors')
def admin_tutors():
    r = require_admin()
    if r:
        return r
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, full_name, phone, pin FROM tutors ORDER BY created_at DESC")
    rows = cur.fetchall()
    # subjects list for mapping
    cur.execute("SELECT id,name,grade FROM subjects ORDER BY grade,name")
    subjects = cur.fetchall()
    # existing mappings
    cur.execute("""SELECT ts.tutor_id, s.name||' ('||s.grade||')' AS label
                FROM tutor_subjects ts JOIN subjects s ON s.id=ts.subject_id ORDER BY s.grade,s.name""")
    maps = {}
    for rmap in cur.fetchall():
        maps.setdefault(rmap['tutor_id'], []).append(rmap['label'])
    conn.close()

    options = "".join([f"<option value='{s['id']}'>{s['name']} — {s['grade']}</option>" for s in subjects])

    trs = []
    for t in rows:
        pin = t['pin'] if t['pin'] else "<span class='muted'>not set</span>"
        mapped = ", ".join(maps.get(t['id'], [])) or "<span class='muted'>No subjects</span>"
        trs.append(
            f"<tr><td>{t['full_name']}<div class='muted'>{t['phone']}</div></td>"
            f"<td>{pin}</td>"
            f"<td>{mapped}</td>"
            f"<td>"
            f"<form method='post' action='{url_for('admin_tutor_reset_pin', tid=t['id'])}' style='display:inline'><button class='btn success'>Reset PIN</button></form> "
            f"<form method='post' action='{url_for('admin_tutor_delete', tid=t['id'])}' style='display:inline' onsubmit='return confirm(\"Delete this tutor?\")'><button class='btn danger'>Delete</button></form>"
            f"<form method='post' action='{url_for('admin_tutor_add_subject', tid=t['id'])}' class='inlineform' style='margin-left:8px'>"
            f"<select name='subject_id'>{options}</select><button class='btn mini'>Add subject</button></form>"
            f"</td></tr>"
        )

    body = f"""
    {admin_nav()}
    <section class='card'>
        <h1>Tutors</h1>
        <div class='toolbar'>
        <input id='tut_q' class='pill' placeholder='Search tutors' oninput="filterTable('tut_q','tut_tbl')"/>
        <form method='post' action='{url_for('admin_tutor_add')}' class='grid' style='grid-template-columns:1fr 160px auto;gap:10px;margin-left:auto'>
            <input name='full_name' placeholder='Full name' required />
            <input name='phone' placeholder='Phone' required />
            <button class='btn'>Add</button>
        </form>
        </div>
        <div class="scroll-x">
            <table id='tut_tbl'>
            <thead><tr><th>Tutor</th><th>PIN</th><th>Subjects</th><th>Actions</th></tr></thead>
            <tbody>{''.join(trs) if trs else "<tr><td colspan='4'><div class='empty'>No tutors yet.</div></td></tr>"}</tbody>
            </table>
        </div>
    </section>
    """
    return page("Tutors", body)

@app.post('/admin/tutors/add')
def admin_tutor_add():
    r = require_admin()
    if r:
        return r
    full_name = request.form.get('full_name','').strip()
    phone = normalize_phone(request.form.get('phone',''))
    if not (full_name and phone):
        return page("Error", card_msg("Missing fields."))
    now = now_utc_iso()
    conn = get_db()
    cur = conn.cursor()
    pins = set()
    cur.execute("SELECT pin FROM students WHERE pin IS NOT NULL")
    pins |= {r['pin'] for r in cur.fetchall()}
    cur.execute("SELECT pin FROM tutors WHERE pin IS NOT NULL")
    pins |= {r['pin'] for r in cur.fetchall()}
    pin = gen_pin(pins)
    try:
        cur.execute("INSERT INTO tutors(full_name,phone,pin,created_at) VALUES(?,?,?,?)",
                    (full_name, phone, pin, now))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return page("Error", card_msg("Phone already exists."))
    conn.close()
    return page("Tutor Added", card_msg(f"Tutor added. Share this PIN securely: {pin}"))

@app.post('/admin/tutors/<int:tid>/reset-pin')
def admin_tutor_reset_pin(tid:int):
    r = require_admin()
    if r:
        return r
    conn = get_db()
    cur = conn.cursor()
    pins = set()
    cur.execute("SELECT pin FROM students WHERE pin IS NOT NULL")
    pins |= {r['pin'] for r in cur.fetchall()}
    cur.execute("SELECT pin FROM tutors WHERE pin IS NOT NULL")
    pins |= {r['pin'] for r in cur.fetchall()}
    new_pin = gen_pin(pins)
    cur.execute("UPDATE tutors SET pin=? WHERE id=?", (new_pin, tid))
    conn.commit()
    conn.close()
    return page("PIN Updated", card_msg(f"Tutor PIN reset to: {new_pin}"))

@app.post('/admin/tutors/<int:tid>/delete')
def admin_tutor_delete(tid:int):
    r = require_admin()
    if r:
        return r
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM sessions WHERE tutor_id=?", (tid,))
    cur.execute("DELETE FROM materials WHERE tutor_id=?", (tid,))
    cur.execute("DELETE FROM tutor_subjects WHERE tutor_id=?", (tid,))
    cur.execute("DELETE FROM tutors WHERE id=?", (tid,))
    conn.commit()
    conn.close()
    return redirect(url_for('admin_tutors'))

@app.post('/admin/tutors/<int:tid>/add-subject')
def admin_tutor_add_subject(tid:int):
    r = require_admin()
    if r:
        return r
    subject_id = request.form.get('subject_id','').strip()
    if not subject_id:
        return page("Error", card_msg("Select a subject."))
    conn=get_db(); cur=conn.cursor()
    try:
        cur.execute("INSERT OR IGNORE INTO tutor_subjects(tutor_id,subject_id) VALUES(?,?)",(tid,subject_id))
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for('admin_tutors'))

# --- Admin: Groups ---

@app.post('/admin/groups/toggle/<int:gid>')
def admin_group_toggle(gid):
    r = require_admin()
    if r:
        return r

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        UPDATE groups
        SET is_visible = CASE WHEN is_visible=1 THEN 0 ELSE 1 END
        WHERE id=?
    """, (gid,))
    conn.commit()
    conn.close()

    return redirect(url_for('admin_groups'))


@app.get('/admin/groups')
def admin_groups():
    r = require_admin()
    if r:
        return r

    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT id,name,grade FROM subjects ORDER BY grade,name")
    subjects = cur.fetchall()

    cur.execute("""
        SELECT g.id, g.invite_link, g.is_visible, s.name, s.grade
        FROM groups g
        JOIN subjects s ON s.id = g.subject_id
        WHERE g.month = 'ALL'
        ORDER BY s.grade, s.name
    """)
    groups = cur.fetchall()
    conn.close()

    group_map = {g['name'] + g['grade']: g for g in groups}

    rows = ""
    for s in subjects:
        key = s['name'] + s['grade']
        g = group_map.get(key)

        if g:
            visibility = (
                "<span class='chip active'>Shown</span>"
                if g['is_visible'] == 1
                else "<span class='chip lapsed'>Hidden</span>"
            )

            rows += f"""
            <tr>
                <td>{grade_label(s['grade'])} — {s['name']}</td>
                <td><a class='links' target='_blank' href='{g['invite_link']}'>Open</a></td>
                <td>{visibility}</td>
                <td>
                    <form method='post' action='{url_for('admin_group_toggle', gid=g['id'])}' style='display:inline'>
                        <button class='btn mini secondary'>
                            {'Hide' if g['is_visible'] else 'Show'}
                        </button>
                    </form>
                    <form method='post' action='{url_for('admin_group_delete', gid=g['id'])}'
                          style='display:inline'
                          onsubmit='return confirm("Delete this group link?")'>
                        <button class='btn danger mini'>Delete</button>
                    </form>
                </td>
            </tr>
            """

        else:
            rows += f"""
            <tr>
                <td>{grade_label(s['grade'])} — {s['name']}</td>
                <td class='muted'>Not set</td>
                <td>-</td>
            </tr>
            """

    options = ''.join(
        [f"<option value='{s['id']}'>{s['grade']} — {s['name']}</option>" for s in subjects]
    )

    body = f"""
    {admin_nav()}
    <section class='card'>
        <h1>Group links (persistent)</h1>

        <form class='grid' method='post' action='{url_for('admin_groups_post')}'>
            <div style='display:grid;grid-template-columns:1fr 2fr auto;gap:10px'>
                <select name='subject_id' required>{options}</select>
                <input name='link' placeholder='WhatsApp invite link' required />
                <button class='btn'>Save</button>
            </div>
        </form>

        <div class="scroll-x">
            <table>
                <thead>
                    <tr><th>Subject</th><th>Link</th><th>Visibility</th><th>Actions</th></tr>
                </thead>
                <tbody>{rows}</tbody>
            </table>
        </div>
    </section>
    """
    return page("Groups", body)


@app.post('/admin/groups')
def admin_groups_post():
    r = require_admin()
    if r:
        return r

    subject_id = request.form.get('subject_id')
    link = request.form.get('link')

    if not (subject_id and link):
        return page("Error", card_msg("Subject and link are required."))

    now = now_utc_iso()
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT id FROM groups
        WHERE subject_id=? AND month='ALL'
    """, (subject_id,))
    row = cur.fetchone()

    if row:
        cur.execute("""
            UPDATE groups
            SET invite_link=?, created_at=?
            WHERE id=?
        """, (link, now, row['id']))
    else:
        cur.execute("""
            INSERT INTO groups(subject_id, month, invite_link, created_at)
            VALUES (?, 'ALL', ?, ?)
        """, (subject_id, link, now))

    conn.commit()
    conn.close()
    return redirect(url_for('admin_groups'))

    
@app.post('/admin/groups/delete/<int:gid>')
def admin_group_delete(gid):
    r = require_admin()
    if r:
        return r

    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM groups WHERE id=?", (gid,))
    conn.commit()
    conn.close()

    return redirect(url_for('admin_groups'))

# --- Admin: Settings ---

@app.get('/admin/settings')
def admin_settings():
    r = require_admin()
    if r:
        return r

    system_month = get_setting('current_month')
    admin_month = session.get('admin_month') or system_month
    
    enrollment_open = '1' if get_setting('enrollment_open', '1') == '1' else '0'
    enrollment_message = get_setting('enrollment_message', '')


    body = f"""
    {admin_nav()}

    <section class='card'>
        <h1>Admin working month</h1>
        <p class='muted mini'>
            This only affects what YOU see on admin pages.
            Students and tutors are not affected.
        </p>
        <form class='grid' method='post' action='{url_for('admin_set_month')}'>
            <div>
                <label>Admin month (YYYY-MM)</label>
                <input name='month' value='{admin_month}' />
            </div>
            <button class='btn'>Apply for admin view</button>
        </form>
    </section>

    <section class='card soft'>
        <h2>System month (global)</h2>
        <p class='muted mini'>
            This affects enrollments, students, tutors, uploads and ratings.
            Change only when starting a new month.
        </p>
        <form class='grid' method='post' action='{url_for('admin_set_system_month')}'>
            <div>
                <label>System month (YYYY-MM)</label>
                <input name='month' value='{system_month}' />
            </div>
            <button class='btn warn'>Change system month</button>
        </form>
    </section>
    
    <section class='card soft'>
        <h2>Enrollment control</h2>
        <p class='muted mini'>
            Control whether students can enroll and what message they see when enrollment is closed.
        </p>

        <form class='grid' method='post' action='{url_for('admin_set_enrollment')}'>
            <div>
                <label>Enrollment status</label>
                <select name='open'>
                    <option value='1' {"selected" if enrollment_open=='1' else ""}>Open</option>
                    <option value='0' {"selected" if enrollment_open=='0' else ""}>Closed</option>
                </select>
            </div>

            <div>
                <label>Closed message (shown to students)</label>
                <textarea name='message' rows='3'>{enrollment_message}</textarea>
            </div>

            <button class='btn warn'>Save enrollment settings</button>
        </form>
    </section>

    
    """

    return page("Settings", body)
    
    
@app.post('/admin/set-enrollment')
def admin_set_enrollment():
    r = require_admin()
    if r:
        return r

    open_val = request.form.get('open', '0')
    message = request.form.get('message', '').strip()

    set_setting('enrollment_open', '1' if open_val == '1' else '0')
    set_setting(
        'enrollment_message',
        message or 'Enrollments are currently closed.'
    )

    return redirect(url_for('admin_settings'))

    
@app.post('/admin/set-month')
def admin_set_month():
    r = require_admin()
    if r:
        return r

    month = request.form.get('month', '').strip()
    if not month:
        return redirect(url_for('admin_settings'))

    session['admin_month'] = month
    return redirect(url_for('admin_home'))


@app.post('/admin/set-system-month')
def admin_set_system_month():
    r = require_admin()
    if r:
        return r

    month = request.form.get('month', '').strip()
    if not month:
        return redirect(url_for('admin_settings'))

    set_setting('current_month', month)
    return redirect(url_for('admin_home'))

@app.get('/admin/sessions')
def admin_sessions():
    r = require_admin()
    if r:
        return r
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id,name,grade FROM subjects ORDER BY grade,name")
    subjects = cur.fetchall()
    cur.execute("""
    SELECT se.*, s.name AS subject_name, s.grade, t.full_name AS tutor_name, t.phone AS tutor_phone
    FROM sessions se
    JOIN subjects s ON s.id=se.subject_id
    JOIN tutors t ON t.id=se.tutor_id
    ORDER BY se.day_of_week, se.start_time
    """)
    sessions_rows = cur.fetchall()
    conn.close()

    options = ''.join([f"<option value='{s['id']}'>{s['grade']} — {s['name']}</option>" for s in subjects])
    dow_opts = ''.join([f"<option value='{i}'>{d}</option>" for i, d in enumerate(DOW)])
    rows = ''.join(
        [
            (
                f"<tr>"
                f"<td>{grade_label(r['grade'])} — {r['subject_name']}</td>"
                f"<td>{r['tutor_name']} ({r['tutor_phone']})</td>"
                f"<td>{DOW[r['day_of_week']]} {r['start_time']}-{r['end_time']}</td>"
                f"<td>"
                f"{'<span class=\"chip active\">Shown</span>' if r['active'] == 1 else '<span class=\"chip lapsed\">Hidden</span>'}"
                f"</td>"
                f"<td>"
                f"<a class='links' href='{url_for('session_qr', id=r['id'])}'>QR</a> · "
                f"<form method='post' action='{url_for('admin_session_toggle', sid=r['id'])}' style='display:inline'>"
                f"<button class='btn mini secondary'>{'Hide' if r['active'] == 1 else 'Show'}</button>"
                f"</form> · "
                f"<form method='post' action='{url_for('admin_session_delete', sid=r['id'])}' "
                f"style='display:inline' onsubmit='return confirm(\"Delete this session?\")'>"
                f"<button class='btn danger mini'>Delete</button>"
                f"</form>"
                f"</td>"
                f"</tr>"
            )
            for r in sessions_rows
        ]
    ) or "<tr><td colspan='5'><div class='empty'>No sessions.</div></td></tr>"



    body = f"""
    {admin_nav()}
    <section class='card'>
        <h1>Sessions</h1>
        <form class='grid' method='post' action='{url_for('admin_sessions_post')}'>
        <div style='display:grid;grid-template-columns:1fr 1fr 110px 110px 1fr auto;gap:10px'>
            <select name='subject_id'>{options}</select>
            <input name='tutor_name' placeholder='Tutor name' required />
            <select name='dow'>{dow_opts}</select>
            <input name='start' placeholder='Start HH:MM' required />
            <input name='end' placeholder='End HH:MM' required />
            <input name='tutor_phone' placeholder='Tutor phone' required />
            <input name='meet' placeholder='Meet link (optional)' />
            <button class='btn'>Add</button>
        </div>
        </form>
        <div class="scroll-x"><table><thead><tr><th>Subject</th><th>Tutor</th><th>When</th><th>Visibility</th><th>Actions</th></thead><tbody>{rows}</tbody></table></div>
    </section>
    """
    return page("Sessions", body)
    

@app.post('/admin/sessions/toggle/<int:sid>')
def admin_session_toggle(sid):
    r = require_admin()
    if r:
        return r

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        UPDATE sessions
        SET active = CASE WHEN active=1 THEN 0 ELSE 1 END
        WHERE id=?
    """, (sid,))
    conn.commit()
    conn.close()

    return redirect(url_for('admin_sessions'))


@app.post('/admin/sessions')
def admin_sessions_post():
    r = require_admin()
    if r:
        return r
    subject_id = request.form.get('subject_id')
    tutor_name = request.form.get('tutor_name', '').strip()
    tutor_phone = request.form.get('tutor_phone', '').strip()
    dow = int(request.form.get('dow', '0'))
    start = request.form.get('start', '')
    end = request.form.get('end', '')
    meet = request.form.get('meet', '') or None

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM tutors WHERE full_name=?", (tutor_name,))
    row = cur.fetchone()
    tutor_id = row['id'] if row else None
    if not tutor_id:
        pins = set()
        cur.execute("SELECT pin FROM students WHERE pin IS NOT NULL")
        pins |= {r['pin'] for r in cur.fetchall()}
        cur.execute("SELECT pin FROM tutors WHERE pin IS NOT NULL")
        pins |= {r['pin'] for r in cur.fetchall()}
        pin = gen_pin(pins)
        now = now_utc_iso()
        cur.execute("INSERT INTO tutors(full_name,phone,pin,created_at) VALUES(?,?,?,?)", (tutor_name, tutor_phone, pin, now))
        tutor_id = cur.lastrowid
    cur.execute("""
    INSERT INTO sessions(subject_id,tutor_id,day_of_week,start_time,end_time,meet_link)
    VALUES(?,?,?,?,?,?)
    """, (subject_id, tutor_id, dow, start, end, meet))
    # Ensure tutor-subject mapping exists for uploads and messaging
    cur.execute("INSERT OR IGNORE INTO tutor_subjects(tutor_id,subject_id) VALUES(?,?)",(tutor_id,subject_id))
    conn.commit()
    conn.close()
    return redirect(url_for('admin_sessions'))

# --- Session QR (uses PNG endpoint) ---

@app.get('/session/<int:id>/qr')
def session_qr(id: int):
    r = require_admin()
    if r:
        return r
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
    SELECT se.*, s.name AS subject_name, s.grade, t.full_name AS tutor_name
    FROM sessions se JOIN subjects s ON s.id=se.subject_id
    JOIN tutors t ON t.id=se.tutor_id WHERE se.id=?
    """, (id,))
    se = cur.fetchone()
    conn.close()
    if not se:
        return page("Not found", card_msg("Session not found."))
    today = datetime.date.today().strftime('%Y-%m-%d')
    payload = {'session_id': id, 'date': today}
    code = b64url_encode(str(payload).encode('utf-8'))
    attend_url = url_for('attend_get', _external=True) + '?' + urlencode({'code': code})
    qr_src = url_for('qr_png') + '?' + urlencode({'text': attend_url})

    body = f"""
    {admin_nav()}
    <section class='card' style='text-align:center'>
        <h1>Scan to check in</h1>
        <p class='muted'>{grade_label(se['grade'])} — {se['subject_name']} with {se['tutor_name']} ({today})</p>
        <img alt='QR code' src='{qr_src}' width='256' height='256' style='margin:14px auto;display:block;border-radius:8px;border:1px solid var(--border);background:#fff' />
        <div class='muted'><a class='links' target='_blank' href='{attend_url}'>Open check-in link</a></div>
    </section>
    """
    return page("Session QR", body)
    
@app.post('/admin/sessions/delete/<int:sid>')
def admin_session_delete(sid):
    r = require_admin()
    if r:
        return r

    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM sessions WHERE id=?", (sid,))
    conn.commit()
    conn.close()

    return redirect(url_for('admin_sessions'))


# --- PNG QR endpoint (reliable) ---

@app.get('/qr.png')
def qr_png():
    text = request.args.get('text', '')
    if not text:
        return make_response('Missing text', 400)
    if qrcode is None:
        return make_response('QR library not installed. Run: pip install qrcode[pil]', 500)
    img = qrcode.make(text)
    buf = BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    resp = make_response(buf.read())
    resp.headers['Content-Type'] = 'image/png'
    return resp

# --- Attendance (QR landing for students) ---

@app.get('/attend')
def attend_get():
    code = request.args.get('code', '')
    if not code:
        return page("Error", card_msg("Missing code."))
    body = f"""
    <section class='wrap small'>
        <div class='card'>
        <h1>Pasco Attendance</h1>
        <form method='post' action='{url_for('attend_post')}' class='grid'>
            <input type='hidden' name='code' value='{code}' />
            <div><label>Enter your WhatsApp number (e.g. 2782...)</label><input name='phone' required /></div>
            <button class='btn success'>Check in</button>
        </form>
        </div>
    </section>
    """
    return page("Attendance", body)

@app.post('/attend')
def attend_post():
    code = request.form.get('code', '')
    phone = request.form.get('phone', '').strip()
    if not (code and phone):
        return page("Error", card_msg("Missing code or phone."))
    try:
        payload = literal_eval(b64url_decode(code).decode('utf-8'))
        session_id = int(payload['session_id'])
        date = payload['date']
    except Exception:
        return page("Error", card_msg("Bad code."))

        month = get_setting('current_month')

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM students WHERE phone_whatsapp=?", (phone,))
    srow = cur.fetchone()
    if not srow:
        conn.close()
        return page("Not found", card_msg("We couldn't find your number. Please register first."))
    student_id = srow['id']

    cur.execute("SELECT subject_id FROM sessions WHERE id=?", (session_id,))
    ses = cur.fetchone()
    if not ses:
        conn.close()
        return page("Not found", card_msg("Session not found."))
    subject_id = ses['subject_id']

    cur.execute("""
    SELECT id FROM enrollments
    WHERE student_id=? AND subject_id=? AND month=? AND status='ACTIVE'
    """, (student_id, subject_id, month))
    enr = cur.fetchone()
    if not enr:
        conn.close()
        return page("Not Active", card_msg("No active enrollment for this month. Please renew to check in."))

    now = now_utc_iso()
    cur.execute(
        "INSERT INTO attendance(session_id,student_id,date,created_at) VALUES(?,?,?,?)",
        (session_id, student_id, date, now),
    )
    conn.commit()
    conn.close()
    return page("Checked in", card_msg("Checked in. Enjoy the session!"))

# --- Admin: Messages (forgot PIN etc.) ---

def format_datime(ts):
    try:
        dt = datetime.datetime.fromisoformat(ts.replace('Z', ''))
        return dt.strftime('%d %b %Y, %H:%M')
    except Exception:
        return ts

@app.get('/admin/messages')
def admin_messages():
    r = require_admin()
    if r:
        return r
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id,kind,payload,created_at,resolved FROM messages ORDER BY created_at DESC")
    rows = cur.fetchall()
    conn.close()
    trs = []
    for m in rows:
        status = "<span class='chip active'>Open</span>" if m['resolved'] == 0 else "<span class='chip'>Resolved</span>"
        action = "" if m['resolved'] else f"""
            <form method='post' action='{url_for('admin_message_resolve', mid=m['id'])}' style='display:inline'>
                <button class='btn success'>Mark resolved</button>
            </form>
        """

        when = format_datime(m['created_at'])

        trs.append(f"""
            <tr>
                <td>{m['kind']}</td>
                <td>{m['payload']}</td>
                <td class='mini muted'>{when}</td>
                <td>{status}</td>
                <td>{action}</td>
            </tr>
        """)


    body = f"""
    {admin_nav()}
    <section class='card'>
        <h1>Admin inbox</h1>
        <div class="scroll-x">
            <table>
            <thead><tr><th>Type</th><th>Payload</th><th>When</th><th>Status</th><th>Action</th></tr></thead>
            <tbody>{''.join(trs) if trs else "<tr><td colspan='5'><div class='empty'>No messages.</div></td></tr>"}</tbody>
            </table>
        </div>
    </section>
    """
    return page("Messages", body)

@app.post('/admin/messages/<int:mid>/resolve')
def admin_message_resolve(mid:int):
    r = require_admin()
    if r:
        return r
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE messages SET resolved=1 WHERE id=?", (mid,))
    conn.commit()
    conn.close()
    return redirect(url_for('admin_messages'))

# --- Admin: Direct Messages (student/tutor DMs) ---

@app.get('/admin/direct-messages')
def admin_direct_messages():
    r=require_admin()
    if r: return r
    conn=get_db(); cur=conn.cursor()
    cur.execute("""SELECT dm.*,
                        CASE dm.from_role 
                            WHEN 'tutor' THEN (SELECT full_name FROM tutors WHERE id=dm.from_id)
                            WHEN 'student' THEN (SELECT full_name FROM students WHERE id=dm.from_id)
                            ELSE 'Admin' END AS from_name,
                        CASE dm.to_role 
                            WHEN 'tutor' THEN (SELECT full_name FROM tutors WHERE id=dm.to_id)
                            WHEN 'student' THEN (SELECT full_name FROM students WHERE id=dm.to_id)
                            ELSE 'Admin' END AS to_name
                FROM direct_messages dm
                ORDER BY dm.created_at ASC LIMIT 80""")
    dms=cur.fetchall()
    # mark those to admin as read
    cur.execute("UPDATE direct_messages SET is_read=1 WHERE to_role='admin'")
    # lists for compose
    cur.execute("SELECT id,full_name FROM tutors ORDER BY full_name"); tutors=cur.fetchall()
    cur.execute("SELECT id,full_name FROM students ORDER BY full_name"); students=cur.fetchall()
    conn.commit(); conn.close()

    dm_list = "".join([f"<div class='msg {'me' if m['from_role']=='admin' else 'them'}'><div class='meta'>{m['from_name']} → {m['to_name']} • {m['created_at'][:16].replace('T',' ')}</div><div>{m['body']}</div></div>" for m in dms]) or "<div class='empty'>No messages yet.</div>"

    tut_opts="".join([f"<option value='tutor|{t['id']}'>{t['full_name']}</option>" for t in tutors])
    stu_opts="".join([f"<option value='student|{s['id']}'>{s['full_name']}</option>" for s in students])
    body=fr"""
    {admin_nav()}
    <section class='grid'>
        <div class='card'><h1>Direct messages</h1>
        <form method='post' action='{url_for('admin_send_dm')}' class='grid'>
            <div><label>To (Tutor/Student)</label>
            <select name='target' required>
                <optgroup label='Tutors'>{tut_opts}</optgroup>
                <optgroup label='Students'>{stu_opts}</optgroup>
            </select>
            </div>
            <div><label>Message</label><textarea name='body' required placeholder='Type your message...'></textarea></div>
            <button class='btn'>Send</button>
        </form>
        <div style='margin-top:10px'>{dm_list}</div>
        </div>
    </section>
    """
    return page("Direct Messages", body)

@app.post('/admin/direct-messages/send')
def admin_send_dm():
    r=require_admin()
    if r: return r
    target=request.form.get('target','')
    body=request.form.get('body','').strip()
    if not (target and body): return page("Error", card_msg("Select a recipient and write a message."))
    try:
        role, id_str = target.split('|',1)
        rid = int(id_str)
    except Exception:
        return page("Error", card_msg("Bad recipient."))
    if role not in ('student','tutor'): return page("Error", card_msg("Bad role."))
    conn=get_db(); cur=conn.cursor()
    cur.execute("INSERT INTO direct_messages(from_role,from_id,to_role,to_id,subject_id,body,created_at) VALUES('admin',0,?,?,NULL,?,?)",
                (role, rid, body, now_utc_iso()))
    conn.commit(); conn.close()
    return redirect(url_for('admin_direct_messages'))

# --- Admin: Analytics dashboard ---

@app.get('/admin/analytics')
def admin_analytics():
    r = require_admin()
    if r: return r

    month = get_admin_active_month()
    conn = get_db()
    cur = conn.cursor()

    # --- KPI metrics ---
    cur.execute("SELECT COUNT(*) AS c FROM enrollments WHERE month=?", (month,))
    total = cur.fetchone()['c'] or 0

    def count_status(s):
        cur.execute("SELECT COUNT(*) AS c FROM enrollments WHERE month=? AND status=?", (month,s))
        return cur.fetchone()['c'] or 0

    pending = count_status('PENDING')
    active = count_status('ACTIVE')
    lapsed = count_status('LAPSED')

    cur.execute("SELECT SUM(amount_paid) AS r FROM enrollments WHERE month=? AND status='ACTIVE'", (month,))
    revenue = cur.fetchone()['r'] or 0

    # --- New vs returning ---
    cur.execute("""
        SELECT COUNT(DISTINCT student_id)
        FROM enrollments
        WHERE month=? AND status='ACTIVE'
        AND student_id NOT IN (
            SELECT student_id FROM enrollments WHERE month < ?
        )
    """, (month, month))
    new_students = cur.fetchone()[0] or 0

    cur.execute("""
        SELECT COUNT(DISTINCT student_id)
        FROM enrollments
        WHERE month=? AND status='ACTIVE'
        AND student_id IN (
            SELECT student_id FROM enrollments WHERE month < ?
        )
    """, (month, month))
    returning = cur.fetchone()[0] or 0

    # --- Revenue per day ---
    cur.execute("""
        SELECT substr(created_at,1,10) AS day, SUM(amount_paid) AS r
        FROM enrollments
        WHERE month=? AND status='ACTIVE'
        GROUP BY day ORDER BY day
    """, (month,))
    rev_rows = cur.fetchall()
    rev_labels = [r['day'] for r in rev_rows]
    rev_data = [r['r'] for r in rev_rows]

    # --- Attendance trend ---
    cur.execute("""
        SELECT a.date, COUNT(*) AS c
        FROM attendance a
        WHERE strftime('%Y-%m', a.date)=?
        GROUP BY a.date ORDER BY a.date
    """, (month,))
    att_rows = cur.fetchall()
    att_labels = [r['date'] for r in att_rows]
    att_data = [r['c'] for r in att_rows]

    # --- Subject table ---
    cur.execute("SELECT id,name,grade FROM subjects ORDER BY grade,name")
    subs = cur.fetchall()

    rows=[]
    for s in subs:
        cur.execute("SELECT COUNT(*) AS c FROM enrollments WHERE subject_id=? AND month=? AND status='ACTIVE'", (s['id'], month))
        active_students = cur.fetchone()['c'] or 0

        cur.execute("""SELECT COUNT(*) AS c FROM attendance a
                       JOIN sessions se ON se.id=a.session_id
                       WHERE se.subject_id=? AND strftime('%Y-%m', a.date)=?""", (s['id'], month))
        att = cur.fetchone()['c'] or 0

        rows.append(f"""
        <tr>
            <td>{grade_label(s['grade'])} — {s['name']}</td>
            <td>{active_students}</td>
            <td>{att}</td>
        </tr>
        """)

    conn.close()

    body = f"""
    {admin_nav()}

    <!-- KPI CARDS -->
    <section class='stats big'>
        {stat('Revenue', f'R{revenue}')}
        {stat('Enrollments', total)}
        {stat('Active', active)}
        {stat('New students', new_students)}
        {stat('Returning', returning)}
        {stat('Lapsed', lapsed)}
    </section>

    <!-- GRAPHS GRID -->
    <section class='grid'>
        <div class='card'><h2>Revenue Trend</h2><canvas id="revChart"></canvas></div>
        <div class='card'><h2>Students Mix</h2><canvas id="studentChart"></canvas></div>
        <div class='card'><h2>Enrollment Status</h2><canvas id="statusChart"></canvas></div>
        <div class='card'><h2>Attendance Trend</h2><canvas id="attChart"></canvas></div>
    </section>

    <!-- TABLE -->
    <div class='card'>
        <h2>Subject Performance</h2>
        <div class="scroll-x">
            <table>
                <thead>
                    <tr><th>Subject</th><th>Active students</th><th>Attendance rows</th></tr>
                </thead>
                <tbody>{''.join(rows)}</tbody>
            </table>
        </div>
    </div>

    <script>
    new Chart(revChart, {{
        type:'line',
        data:{{ labels:{json.dumps(rev_labels)},
        datasets:[{{label:'Revenue',data:{json.dumps(rev_data)}}}] }}
    }});

    new Chart(studentChart, {{
        type:'pie',
        data:{{ labels:['New','Returning'],
        datasets:[{{data:[{new_students},{returning}]}}] }}
    }});

    new Chart(statusChart, {{
        type:'bar',
        data:{{ labels:['Pending','Active','Lapsed'],
        datasets:[{{data:[{pending},{active},{lapsed}]}}] }}
    }});

    new Chart(attChart, {{
        type:'line',
        data:{{ labels:{json.dumps(att_labels)},
        datasets:[{{label:'Attendance',data:{json.dumps(att_data)}}}] }}
    }});
    </script>
    """

    return page("Analytics Dashboard", body)




# --- Export remove list ---

@app.get('/api/export/remove-list')
def export_remove_list():
    r = require_admin()
    if r:
        return r

        month = get_admin_active_month()
    y, m = map(int, month.split('-'))
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    next_month = f"{ny:04d}-{nm:02d}"

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT e.id, e.student_id, e.subject_id, st.full_name, st.phone_whatsapp, st.grade, sub.name AS subject_name
        FROM enrollments e
        JOIN students st ON st.id=e.student_id
        JOIN subjects sub ON sub.id=e.subject_id
        WHERE e.month=? AND e.status='ACTIVE'
    """, (month,))
    active_this = cur.fetchall()

    cur.execute(
        "SELECT student_id, subject_id FROM enrollments WHERE month=? AND status='ACTIVE'",
        (next_month,),
    )
    next_active = {(row['student_id'], row['subject_id']) for row in cur.fetchall()}
    conn.close()

    out = ["Student,Phone,Grade,Subject"]
    for r in active_this:
        if (r['student_id'], r['subject_id']) not in next_active:
            full = str(r['full_name']).replace(',', ' ')
            phone = str(r['phone_whatsapp'])
            grade = str(r['grade'])
            subject = str(r['subject_name']).replace(',', ' ')
            out.append(f"{full},{phone},{grade},{subject}")

    csv_data = "\n".join(out)
    resp = make_response(csv_data)
    resp.headers['Content-Type'] = 'text/csv; charset=utf-8'
    resp.headers['Content-Disposition'] = 'attachment; filename=remove-list.csv'
    return resp

# --- Payfast IPN stub ---

@app.post('/payfast/ipn')
def payfast_ipn():
    body = request.get_data(as_text=True)
    print("[Payfast IPN]", body)
    return {"ok": True}


# ===================== MAIN ==============
if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', '5000'))
    app.run(host='127.0.0.1', port=port, debug=True)



# ===================== QUIZ SYSTEM: lightweight integration layer ==============# This section adds a complete quizzes module WITHOUT touching your existing routes.
# - We override init_db() to call your original init and then create quiz tables.
# - We add QUIZ_IMG_DIR and ensure it exists.
# - We add tutor/student/analytics routes under /tutor/quizzes, /student/quizzes, /admin/analytics/quizzes.

# keep a handle to the original init_db
try:
    _EBTA_ORIG_INIT_DB = init_db
except NameError:
    _EBTA_ORIG_INIT_DB = None

# New directory for question images
from pathlib import Path as _Path
QUIZ_IMG_DIR = (_Path(__file__).resolve().parent) / "quiz_images"
QUIZ_IMG_DIR.mkdir(exist_ok=True)

# Re-define init_db so __main__ calls this one
def init_db():
    if _EBTA_ORIG_INIT_DB:
        _EBTA_ORIG_INIT_DB()
    conn = get_db(); c = conn.cursor()
    
     # 🔧 ONE-TIME CLEANUP: remove legacy subject name
    c.execute("DELETE FROM subjects WHERE name = 'Maths Lit'")
    
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS quizzes(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        subject_id INTEGER NOT NULL,
        tutor_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        mode TEXT NOT NULL,             -- 'mcq' | 'mixed' | 'written'
        duration_seconds INTEGER NOT NULL DEFAULT 600,
        month TEXT NOT NULL,
        is_published INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        FOREIGN KEY(subject_id) REFERENCES subjects(id) ON DELETE CASCADE,
        FOREIGN KEY(tutor_id) REFERENCES tutors(id) ON DELETE CASCADE
        );
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS quiz_questions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        quiz_id INTEGER NOT NULL,
        qtext TEXT NOT NULL,
        qtype TEXT NOT NULL,            -- 'mcq' | 'short' | 'long'
        points INTEGER NOT NULL DEFAULT 1,
        image_path TEXT,
        position INTEGER NOT NULL DEFAULT 1,
        FOREIGN KEY(quiz_id) REFERENCES quizzes(id) ON DELETE CASCADE
        );
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS quiz_options(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        question_id INTEGER NOT NULL,
        opt_text TEXT NOT NULL,
        is_correct INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY(question_id) REFERENCES quiz_questions(id) ON DELETE CASCADE
        );
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS quiz_attempts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        quiz_id INTEGER NOT NULL,
        student_id INTEGER NOT NULL,
        started_at TEXT NOT NULL,
        submitted_at TEXT,
        status TEXT NOT NULL DEFAULT 'in_progress', -- in_progress|submitted|graded
        auto_score REAL,
        manual_score REAL,
        total_score REAL,
        FOREIGN KEY(quiz_id) REFERENCES quizzes(id) ON DELETE CASCADE,
        FOREIGN KEY(student_id) REFERENCES students(id) ON DELETE CASCADE,
        UNIQUE(quiz_id, student_id)
        );
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS quiz_answers(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        attempt_id INTEGER NOT NULL,
        question_id INTEGER NOT NULL,
        chosen_option_id INTEGER,       -- for MCQ
        answer_text TEXT,               -- for short/long
        is_correct INTEGER,             -- nullable until auto-grade or manual grade
        awarded_points REAL,            -- null until graded
        FOREIGN KEY(attempt_id) REFERENCES quiz_attempts(id) ON DELETE CASCADE,
        FOREIGN KEY(question_id) REFERENCES quiz_questions(id) ON DELETE CASCADE,
        FOREIGN KEY(chosen_option_id) REFERENCES quiz_options(id) ON DELETE SET NULL
        );
        """
    )
    # Seed month if missing
    c.execute("SELECT value FROM settings WHERE key='current_month'")
    if not c.fetchone():
        import datetime as _dt
        c.execute("INSERT INTO settings(key,value) VALUES(?,?)", ("current_month", _dt.date.today().strftime("%Y-%m")))
    conn.commit(); conn.close()

# Serve quiz images
@app.route('/quiz-images/<path:filename>')
def quiz_images(filename):
    return send_from_directory(QUIZ_IMG_DIR, filename)

# ---------------------- Tutor: Quizzes CRUD ----------------------
# =============================================================
# RENDER DB BOOTSTRAP (DO NOT REMOVE)
# Ensures all tables exist before first request
# =============================================================
try:
    init_db()
except Exception as e:
    print("DB init warning:", e)
# =============================================================
