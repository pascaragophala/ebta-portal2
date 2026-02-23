import os
import json
import sqlite3
import datetime
import calendar
import random
import base64
import secrets
import threading
import time
from urllib.parse import urlencode
from zoneinfo import ZoneInfo
from pathlib import Path
from html import escape


from flask import Flask, request, redirect, url_for, render_template_string, send_from_directory, session, flash, make_response

app = Flask(__name__)
app.secret_key = os.environ.get('EBTA_SECRET_KEY', 'ebta-dev-secret')

# =============================================================
# RENDER PERSISTENT STORAGE
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
    
    cur.execute("""
    CREATE TABLE IF NOT EXISTS sms_queue(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        phone TEXT NOT NULL,
        body TEXT NOT NULL,
        recipient_type TEXT,
        status TEXT DEFAULT 'PENDING',  -- PENDING | SENT | FAILED
        retry_count INTEGER DEFAULT 0,
        created_at TEXT NOT NULL,
        sent_at TEXT
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
    ensure_column(conn, "subjects", "uploads_locked", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "materials", "admin_unlocked", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "students", "phone_type", "TEXT DEFAULT 'SA'")
    ensure_column(conn, "students", "guardian_phone_type", "TEXT DEFAULT 'SA'")



    # --- Performance indexes ---
    cur.execute("CREATE INDEX IF NOT EXISTS idx_enrollments_month ON enrollments(month)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_enrollments_student ON enrollments(student_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_enrollments_created ON enrollments(created_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_enrollment_files_enr ON enrollment_files(enrollment_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_students_created ON students(created_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_enrollments_month_created ON enrollments(month, created_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_students_full_name ON students(full_name)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_students_phone ON students(phone_whatsapp)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_students_guardian ON students(guardian_phone)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_students_email ON students(email)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_students_school ON students(school)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_tutor_subjects_tutor ON tutor_subjects(tutor_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_tutor_subjects_tutor ON tutor_subjects(tutor_id)")




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
            ("Geography","G12"),

            # Economics

            # Business Studies
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
            ("Geography","G12"),

            # Economics

            # Business Studies
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
            ('enrollment_open', '1')
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

    subjects_to_remove = [
        ("Geography", "G11"),
        ("Economics", "G12"),
        ("Business Studies", "G10"),
    ]

    for name, grade in subjects_to_remove:

        # Remove related enrollments
        cur.execute("""
            DELETE FROM enrollments
            WHERE subject_id IN (
                SELECT id FROM subjects WHERE name=? AND grade=?
            )
        """, (name, grade))

        # Remove tutor-subject mappings
        cur.execute("""
            DELETE FROM tutor_subjects
            WHERE subject_id IN (
                SELECT id FROM subjects WHERE name=? AND grade=?
            )
        """, (name, grade))

        # Remove groups
        cur.execute("""
            DELETE FROM groups
            WHERE subject_id IN (
                SELECT id FROM subjects WHERE name=? AND grade=?
            )
        """, (name, grade))

        # Remove sessions
        cur.execute("""
            DELETE FROM sessions
            WHERE subject_id IN (
                SELECT id FROM subjects WHERE name=? AND grade=?
            )
        """, (name, grade))

        # Finally remove the subject itself
        cur.execute("""
            DELETE FROM subjects
            WHERE name=? AND grade=?
        """, (name, grade))


    conn.commit()
    conn.close()


# Initialize database AFTER function exists
with app.app_context():
    init_db()


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


def normalize_phone(phone: str, phone_type: str = "SA") -> str:

    if not phone:
        return ""

    phone = str(phone).strip()

    # remove spaces, brackets, dashes
    phone = phone.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")

    if phone_type == "INT":
        # International numbers must start with +
        if not phone.startswith("+"):
            phone = "+" + ''.join(ch for ch in phone if ch.isdigit())
        return phone

    # Default: South Africa normalization
    digits = ''.join(ch for ch in phone if ch.isdigit())

    if digits.startswith("0") and len(digits) == 10:
        return "+27" + digits[1:]

    if digits.startswith("27") and len(digits) == 11:
        return "+" + digits

    if digits.startswith("+27"):
        return digits

    return digits
    
def phone_variants(phone: str):
    """
    Generate all possible phone variants so login works
    regardless of format entered.
    Supports:
    0821234567
    +27821234567
    27821234567
    international numbers
    """

    if not phone:
        return []

    raw = str(phone).strip()

    # remove spaces, brackets, dashes
    clean = raw.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")

    # digits only
    digits = ''.join(ch for ch in clean if ch.isdigit())

    variants = set()

    # always include original clean input
    variants.add(clean)

    # include digits-only version
    if digits:
        variants.add(digits)
        variants.add("+" + digits)

    # South Africa conversions

    # 0821234567 → 27821234567 and +27821234567
    if digits.startswith("0") and len(digits) == 10:
        variants.add("27" + digits[1:])
        variants.add("+27" + digits[1:])

    # 27821234567 → 0821234567 and +27821234567
    elif digits.startswith("27") and len(digits) == 11:
        variants.add("0" + digits[2:])
        variants.add("+27" + digits[2:])

    # +27821234567 → 27821234567 and 0821234567
    if clean.startswith("+") and digits.startswith("27") and len(digits) == 11:
        variants.add(digits)
        variants.add("0" + digits[2:])

    return list(variants)


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
    # Best-effort SMS sender with automatic SA phone normalization

    if not to_phone:
        return

    # --- Normalize South African phone number ---
    phone = str(to_phone).strip()

    # remove spaces, dashes, brackets
    phone = phone.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")

    # convert 0821234567 → +27821234567
    if phone.startswith("0") and len(phone) == 10:
        phone = "+27" + phone[1:]

    # convert 27821234567 → +27821234567
    elif phone.startswith("27") and len(phone) == 11:
        phone = "+" + phone

    # already correct format
    elif phone.startswith("+"):
        pass

    else:
        # invalid format → log error
        try:
            conn = get_db()
            cur = conn.cursor()
            payload = f"INVALID_PHONE:{to_phone}"
            cur.execute(
                "INSERT INTO messages(kind,payload,created_at,resolved) VALUES(?,?,?,0)",
                ("sms_error", payload, now_utc_iso()),
            )
            conn.commit()
            conn.close()
        except:
            pass
        return  

    to_phone = phone

    account_sid = os.environ.get("EBTA_TWILIO_SID")
    auth_token = os.environ.get("EBTA_TWILIO_TOKEN")
    from_number = os.environ.get("EBTA_TWILIO_FROM")

    # If Twilio not configured, log SMS
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
        except:
            pass
        return

    # Send via Twilio
    try:
        from twilio.rest import Client

        client = Client(account_sid, auth_token)

        client.messages.create(
            from_=from_number,
            to=to_phone,
            body=body
        )

        # optional success log
        conn = get_db()
        cur = conn.cursor()
        payload = f"SENT TO:{to_phone}"
        cur.execute(
            "INSERT INTO messages(kind,payload,created_at,resolved) VALUES(?,?,?,0)",
            ("sms_sent", payload, now_utc_iso()),
        )
        conn.commit()
        conn.close()

    except Exception as e:
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
        except:
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
@media (max-width: 700px){
  .scroll-x table{
    min-width: 100%;
  }
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

@media (max-width: 700px) {
  table thead { display: none; }
  table tr { display: block; margin-bottom: 12px; }
  table td {
    display: flex;
    justify-content: space-between;
    padding: 8px;
    border-bottom: 1px solid #eee;
  }
  table td::before {
    content: attr(data-label);
    font-weight: 600;
    color: #555;
  }
}

/* ================= CHAT UI ================= */

.chat-layout{
display:grid;
grid-template-columns:280px 1fr;
gap:12px;
height:600px;
border:1px solid var(--border);
border-radius:14px;
overflow:hidden;
background:#fff;
}

/* left conversation list */

.chat-list{
border-right:1px solid var(--border);
overflow-y:auto;
background:#f8fafc;
}

.chat-user{
padding:12px;
border-bottom:1px solid var(--border-light);
cursor:pointer;
transition:.15s;
}

.chat-user:hover{
background:#eef6ee;
}

.chat-user.active{
background:#e8f5e9;
border-left:4px solid var(--primary);
}

/* right chat window */

.chat-window{
display:flex;
flex-direction:column;
height:100%;
}

.chat-messages{
flex:1;
overflow-y:auto;
padding:14px;
display:flex;
flex-direction:column;
gap:8px;
background:#f1f5f9;
}

/* message bubbles */

.bubble{
max-width:70%;
padding:10px 14px;
border-radius:14px;
font-size:14px;
box-shadow:var(--shadow-sm);
}

.bubble.them{
align-self:flex-start;
background:#ffffff;
border:1px solid var(--border);
}

.bubble.me{
align-self:flex-end;
background:linear-gradient(135deg,var(--primary),var(--primary-dark));
color:#fff;
}

.bubble .time{
font-size:11px;
opacity:.7;
margin-top:4px;
}

/* input area */

.chat-input{
border-top:1px solid var(--border);
padding:10px;
background:#fff;
}

.chat-layout {
    display: grid;
    grid-template-columns: 280px 1fr;
    gap: 0;
    height: 500px;
    border: 1px solid #e5e7eb;
    border-radius: 10px;
    overflow: hidden;
}

/* LEFT SIDE LIST */
.chat-list {
    border-right: 1px solid #e5e7eb;
    overflow-y: auto;
    background: #f9fafb;
}

/* RIGHT SIDE WINDOW */
.chat-window {
    display: flex;
    flex-direction: column;
    height: 100%;
    background: white;
}

/* THIS IS THE IMPORTANT FIX */
.chat-messages {
    flex: 1;
    overflow-y: auto;
    padding: 12px;
    min-height: 0;
}

/* INPUT STAYS FIXED */
.chat-input {
    border-top: 1px solid #e5e7eb;
    padding: 10px;
    background: white;
}

/* MOBILE FIX */
@media (max-width: 768px) {

    .chat-layout {
        grid-template-columns: 1fr;
        height: 70vh;
    }

    .chat-list {
        max-height: 150px;
    }

}

/* MAIN CHAT CONTAINER */
.chat-layout {
    display: grid;
    grid-template-columns: 280px 1fr;
    height: 520px;              /* IMPORTANT: fixed height */
    max-height: 520px;
    border-radius: 10px;
    overflow: hidden;
    border: 1px solid #e5e7eb;
    background: white;
}

/* LEFT LIST */
.chat-list {
    overflow-y: auto;
    border-right: 1px solid #e5e7eb;
    background: #f9fafb;
}

/* RIGHT SIDE */
.chat-window {
    display: flex;
    flex-direction: column;
    height: 100%;
    min-height: 0;
}

/* THIS FIXES YOUR ISSUE */
.chat-messages {
    flex: 1;
    overflow-y: auto;
    padding: 12px;
}

/* SEND AREA ALWAYS VISIBLE */
.chat-input {
    flex-shrink: 0;
    border-top: 1px solid #e5e7eb;
    padding: 10px;
    background: white;
}

/* MOBILE FIX */
@media (max-width: 768px) {

    .chat-layout {
        grid-template-columns: 1fr;
        height: 75vh;
        max-height: 75vh;
    }

    .chat-list {
        max-height: 140px;
    }

}

.chat-list {
    width: 320px;
    max-height: 600px;
    overflow-y: auto;
    border-right: 1px solid #ddd;
    padding: 8px;
}

.chat-section {
    font-size: 12px;
    font-weight: 700;
    color: #64748b;
    padding: 8px 6px;
    margin-top: 10px;
    border-bottom: 1px solid #eee;
}

.chat-user {
    display: block;
    padding: 10px;
    border-radius: 8px;
    text-decoration: none;
    margin-bottom: 4px;
    transition: background 0.15s;
}

.chat-user:hover {
    background: #f1f5f9;
}

.chat-user.active {
    background: #e0f2fe;
}

.chat-name {
    font-weight: 600;
    font-size: 14px;
    color: #0f172a;
}

.chat-role {
    font-size: 12px;
    color: #64748b;
}

.chat-user.tutor .chat-name {
    color: #2563eb;
}

.chat-user.student .chat-name {
    color: #059669;
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
    {id:'materials', keys:['unlock uploads','uploads','materials']},
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

'#dashboard': ['dashboard','overview','analytics'],

'#enrollments': ['manage enrollments','enrollments','manage enrollment','enrollment'],

'#students': ['students','student list'],

'#tutors': ['tutors','tutor list'],

'#groups': ['group links','whatsapp links','links','groups'],

'#sessions': ['sessions & qr','sessions','qr'],

'#materials': ['unlock uploads','uploads','materials','assignments'],

'#inbox': ['inbox'],

'#messages': ['direct messages','messages'],

'#analytics': ['analytics','dashboard','reports'],

'#settings': ['settings','configuration']

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

document.addEventListener("DOMContentLoaded", function(){
    const box = document.querySelector(".chat-messages");
    if(box){
        box.scrollTop = box.scrollHeight;
    }
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
            
            month = get_active_month('student')
            
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
                ("Status", "#status"),
                ("Assignments", "#assignments"),
                ("Materials", "#materials"),
                ("Messages", "#messages"),
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
            month = get_active_month('tutor')
            
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
                ("Messages", "#messages"),
                ("Students", "#students"),
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
        <div style="opacity:0.95;">⚡ Powered by <a href="https://pascalmindtech.co.za/" target="_blank" style="color:#000;text-decoration:underline;font-weight:600;">PascalMindTech</a></div>
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
        ("Geography","G12"),

        # Economics

        # Business Studies
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
        <div class="card soft" style="margin-top:18px;">

    <h2>Portal Help Videos</h2>
    <div class="mini muted" style="margin-bottom:10px;">
    Watch these videos if you need help using the EBTA Portal.
    </div>

    <div style="
    display:grid;
    grid-template-columns:repeat(auto-fit,minmax(280px,1fr));
    gap:16px;
    ">

    <div>
    <div class="mini" style="font-weight:600;margin-bottom:6px;">
    How to Enroll on EBTA Portal
    </div>
    <iframe
    src="https://drive.google.com/file/d/1z3kRXeO0iTvcofjQKWH0TJx7MKs7IIdg/preview"
    width="100%"
    height="180"
    allow="autoplay"
    loading="lazy"
    style="border-radius:10px;border:1px solid #e2e8f0;"
    ></iframe>
    </div>

    <div>
    <div class="mini" style="font-weight:600;margin-bottom:6px;">
    How to Join WhatsApp Group
    </div>
    <iframe
    src="https://drive.google.com/file/d/1shB3HSinhvBinC7_RUeB0OhxwLyN6ZV8/preview"
    width="100%"
    height="180"
    allow="autoplay"
    loading="lazy"
    style="border-radius:10px;border:1px solid #e2e8f0;"
    ></iframe>
    </div>

    <div>
    <div class="mini" style="font-weight:600;margin-bottom:6px;">
    How to Log into Portal
    </div>
    <iframe
    src="https://drive.google.com/file/d/18J666KbFvxMY2K9PhbgDmZQOWLH-8uz4/preview"
    width="100%"
    height="180"
    allow="autoplay"
    loading="lazy"
    style="border-radius:10px;border:1px solid #e2e8f0;"
    ></iframe>
    </div>

    <div>
    <div class="mini" style="font-weight:600;margin-bottom:6px;">
    How to Upload Assignment
    </div>
    <iframe
    src="https://drive.google.com/file/d/1cPluM63tebmvAuwt157sFhZ7LtOHNkti/preview"
    width="100%"
    height="180"
    allow="autoplay"
    loading="lazy"
    style="border-radius:10px;border:1px solid #e2e8f0;"
    ></iframe>
    </div>

    </div>
    </div>
    
    
    <div class='card soft'>
        <h1>Enroll for {month_label}</h1>
        <p class='muted'>All required fields are marked. Upload 1–2 Proof of Payment files.</p>

        <form id='reg_form' method='post' action='{url_for('register')}' enctype='multipart/form-data' class='grid'>

        <!-- Student & guardian details -->
        <div class="grid two-col">
            <div>
            <label>Student Name & Surname</label>
            <input name='full_name' required/>
            </div>
            <div>
            <label>Student WhatsApp Number</label>

            <select name="phone_type" id="phone_type">
                <option value="SA">South African</option>
                <option value="INT">International</option>
            </select>

            <input name="phone"
                   id="phone_input"
                   required
                   placeholder="0821234567 or +447911123456">
            </div>
            <div>
            <label>Guardian WhatsApp Number</label>

            <select name="guardian_phone_type" id="guardian_phone_type">
                <option value="SA">South African</option>
                <option value="INT">International</option>
            </select>

            <input name="guardian"
                   id="guardian_input"
                   required>
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
                <li>Capitec number: 0649619653</li>
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
                <label>Amount You Paid</label>
                <input
                    type="number"
                    name="amount_paid"
                    id="amount_paid"
                    inputmode="numeric"
                    min="0"
                    step="1"
                    placeholder="Enter the amount you paid for classes"
                    required
                />
                <div class="mini muted" id="amount_paid_hint" style="margin-top:4px;"></div>
            </div>

        </div>

               

        <div class='toolbar'>

            <button type="button" class="btn secondary" onclick="openTermsModal()">
                View Terms & Conditions
            </button>

            <label style="display:flex;align-items:center;gap:6px;">
                <input type="checkbox" id="terms_check" required>
                <span class="mini">I agree to the Terms & Conditions</span>
            </label>

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
        const termsCheck = document.getElementById('terms_check');

        if(!termsCheck || !termsCheck.checked){
            e.preventDefault();
            showPopup("You must agree to the Terms & Conditions before enrolling.", "error");
            return;
        }

        // ✅ allow exit without warning when submitting
        ebtaAllowExit = true;
        const grade = gradeSelect.value;

        // Validate phone numbers: ensure 10 digits for student and guardian WhatsApp numbers
        const studentType =
            document.querySelector("select[name='phone_type']")?.value || "SA";

        const guardianType =
            document.querySelector("select[name='guardian_phone_type']")?.value || "SA";

        function digitsOnly(str){
            return (str||'').replace(/\D/g,'');
        }

        // Student validation
        if(studentPhoneInput){

            if(studentType === "SA"){

                const digits = digitsOnly(studentPhoneInput.value);

                if(digits.length !== 10){
                    e.preventDefault();
                    showPopup(
                        "South African number must be 10 digits.",
                        "error"
                    );
                    return;
                }

            }else{

                if(!studentPhoneInput.value.startsWith("+")){
                    e.preventDefault();
                    showPopup(
                        "International number must start with +",
                        "error"
                    );
                    return;
                }

            }
        }

        // Guardian validation
        if(guardianPhoneInput){

            if(guardianType === "SA"){

                const digits = digitsOnly(guardianPhoneInput.value);

                if(digits.length !== 10){
                    e.preventDefault();
                    showPopup(
                        "Guardian SA number must be 10 digits.",
                        "error"
                    );
                    return;
                }

            }else{

                if(!guardianPhoneInput.value.startsWith("+")){
                    e.preventDefault();
                    showPopup(
                        "Guardian international number must start with +",
                        "error"
                    );
                    return;
                }

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
    
    extra_js += """
    <script>

    function openTermsModal(){
        if(document.getElementById('terms-modal')) return;

        const overlay = document.createElement('div');
        overlay.id = 'terms-modal';
        overlay.style.position = 'fixed';
        overlay.style.inset = '0';
        overlay.style.background = 'rgba(0,0,0,0.6)';
        overlay.style.display = 'flex';
        overlay.style.alignItems = 'center';
        overlay.style.justifyContent = 'center';
        overlay.style.zIndex = '99999';

        const box = document.createElement('div');
        box.style.width = '95%';
        box.style.maxWidth = '600px';
        box.style.maxHeight = '80vh';
        box.style.overflow = 'auto';
        box.style.background = '#fff';
        box.style.padding = '20px';
        box.style.borderRadius = '12px';

        box.innerHTML = `
            <h2>EBTA Terms & Conditions</h2>

            <div style="font-size:14px;line-height:1.6;margin-top:10px;">

            <p><strong>1. Enrollment Agreement</strong><br>
            By enrolling, you agree to participate in EBTA classes and follow all academy rules.</p>

            <p><strong>2. Payment Policy</strong><br>
            • Monthly fees must be paid before attending classes.<br>
            • Fees are non-refundable once classes have started.<br>
            • Proof of payment must be uploaded during enrollment.</p>

            <p><strong>3. PIN Responsibility</strong><br>
            Your PIN is confidential. Do not share it with anyone.</p>

            <p><strong>4. Attendance</strong><br>
            Students must attend sessions regularly and on time.</p>

            <p><strong>5. Conduct</strong><br>
            Respect tutors, students, and academy policies at all times.</p>

            <p><strong>6. Communication</strong><br>
            Important information will be shared via WhatsApp or the EBTA Portal.</p>

            <p><strong>7. Privacy</strong><br>
            Your personal information is stored securely and used only for academic purposes.</p>

            <p><strong>8. Agreement</strong><br>
            By proceeding, you confirm that you understand and accept these Terms & Conditions.</p>

            </div>

            <div style="margin-top:15px;text-align:right;">
                <button class="btn success" onclick="closeTermsModal()">Close</button>
            </div>
        `;

        overlay.appendChild(box);
        document.body.appendChild(overlay);
    }

    function closeTermsModal(){
        const modal = document.getElementById('terms-modal');
        if(modal) modal.remove();
    }

    </script>
    """

    
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
    phone_type = request.form.get("phone_type", "SA")
    guardian_phone_type = request.form.get("guardian_phone_type", "SA")

    phone = normalize_phone(
        request.form.get("phone",""),
        phone_type
    )

    guardian = normalize_phone(
        request.form.get("guardian",""),
        guardian_phone_type
    )
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
            phone_type,
            guardian_phone_type,
            created_at
        )VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
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
            phone_type,
            guardian_phone_type,
            now_utc_iso()
        ))

        sid = cur.lastrowid

    # Save PoP files
    saved_paths = []
    ts = int(datetime.datetime.now().timestamp())
    for idx, pop in enumerate(pops, start=1):
        safe = f"{ts}_{secrets.token_hex(8)}_{idx}_{secure_name(pop.filename)}"
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
            "<strong>Next steps:</strong> Once your enrollment is approved (usually within 14 days), "
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
                <select name="phone_type">
                    <option value="SA">South Africa</option>
                    <option value="INT">International</option>
                </select>

                <input name='phone' required/>
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
                    <select name="phone_type">
                        <option value="SA">South Africa</option>
                        <option value="INT">International</option>
                    </select>

                    <input name='phone' required/>
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

    raw_phone = request.form.get('phone','').strip()
    pin = request.form.get('pin','').strip()

    normalized = normalize_phone(raw_phone)
    variants = phone_variants(normalized)

    if not variants:
        return page("Login failed", card_msg("Phone number required."))

    conn = get_db()
    cur = conn.cursor()

    placeholders = ",".join("?" * len(variants))

    cur.execute(f"""
        SELECT id, pin, full_name
        FROM students
        WHERE phone_whatsapp IN ({placeholders})
        LIMIT 1
    """, variants)

    row = cur.fetchone()
    conn.close()

    if not row or not row['pin'] or row['pin'] != pin:
        return page("Login failed", card_msg("Wrong phone or PIN."))

    session['student_id'] = row['id']
    session['student_name'] = row['full_name']

    return redirect(url_for('student_home'))

@app.post('/student/forgot-pin')
def student_forgot_pin():

    raw_phone = request.form.get('phone','').strip()

    variants = phone_variants(raw_phone)

    if not variants:
        return page(
            "Error",
            card_msg("Please enter your WhatsApp number.")
        )

    conn = get_db()
    cur = conn.cursor()

    placeholders = ",".join("?" * len(variants))

    cur.execute(f"""
        SELECT id, full_name, phone_whatsapp, pin
        FROM students
        WHERE phone_whatsapp IN ({placeholders})
        LIMIT 1
    """, variants)

    student = cur.fetchone()

    # Use the actual stored phone
    phone = student["phone_whatsapp"] if student else raw_phone

    # Student not found
    if not student:
        conn.close()
        return page(
            "Not found",
            card_msg("""
                This number is not registered.
                Please enroll first before requesting your PIN.
            """)
        )

    student_id = student["id"]
    student_name = student["full_name"]
    student_pin = student["pin"]

    # Check if enrolled at least once
    cur.execute("""
        SELECT 1
        FROM enrollments
        WHERE student_id=?
        LIMIT 1
    """, (student_id,))

    enrolled = cur.fetchone()

    if not enrolled:
        conn.close()
        return page(
            "Not enrolled",
            card_msg("""
                You are not enrolled yet.
                Please enroll first before requesting your PIN.
            """)
        )

    # Generate PIN if missing
    if not student_pin:

        pins = set()

        cur.execute("SELECT pin FROM students WHERE pin IS NOT NULL")
        pins |= {r["pin"] for r in cur.fetchall()}

        cur.execute("SELECT pin FROM tutors WHERE pin IS NOT NULL")
        pins |= {r["pin"] for r in cur.fetchall()}

        student_pin = gen_pin(pins)

        cur.execute("""
            UPDATE students
            SET pin=?
            WHERE id=?
        """, (student_pin, student_id))

        conn.commit()

    # Log admin notification
    cur.execute("""
        INSERT INTO messages(kind,payload,created_at,resolved)
        VALUES(?,?,?,0)
    """, (
        "forgot_student_pin",
        f"{student_name} requested PIN reset ({phone})",
        now_utc_iso()
    ))

    conn.commit()
    conn.close()

    # Send SMS automatically
    try:

        base_url = (request.url_root or '').rstrip('/')

        sms = (
            f"EBTA Portal: Hi {student_name.split()[0]}, "
            f"your login PIN is {student_pin}. "
            f"Login at {base_url}/student/login"
        )

        send_sms_notification(phone, sms)

    except Exception:
        pass

    return page(
        "PIN sent",
        card_msg("""
            Your PIN has been sent to your WhatsApp number.
            Please check your messages.
        """)
    )


def card_msg(text):
    return f"<div class='card'><div>{text}</div></div>"


@app.get('/student/logout')
def student_logout():
    session.pop('student_id', None)
    session.pop('student_name', None)
    session.pop('student_month', None)  # 🔑 clear month override
    return redirect(url_for('student_login'))


def get_active_month(role):
    """
    Returns the effective month for the current session.

    Priority:
    1. User-selected month (session)
    2. Real current calendar month
    """

    # Real calendar month in SA timezone
    now = datetime.datetime.now(ZoneInfo("Africa/Johannesburg"))
    real_month = now.strftime("%Y-%m")

    if role == 'student':
        return session.get('student_month') or real_month

    if role == 'tutor':
        return session.get('tutor_month') or real_month

    return real_month

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
    <div class="card soft" style="margin-bottom:14px;border-left:5px solid #25D366">

        <div style="font-weight:600;font-size:16px;margin-bottom:6px">
            Switch Month
        </div>

        <div class="mini muted" style="margin-bottom:10px">
            Select a month to view your subjects, assignments, and sessions.
            <br>
            Green = enrolled • Grey = not enrolled
        </div>

        <form method="post" action="{url_for('student_set_month')}">

            <select name="month"
                    onchange="this.form.submit()"
                    style="
                        width:100%;
                        padding:12px;
                        font-size:16px;
                        border-radius:10px;
                        border:2px solid #25D366;
                        background:#fff;
                        cursor:pointer;
                    ">

                {''.join(
                    f"<option value='{m}' "
                    f"{'selected' if m == month else ''}>"
                    f"{'✓ ' if m in active_months else ''}"
                    f"{pretty_month_label(m)}"
                    f"{'' if m in active_months else ' (not enrolled)'}"
                    f"</option>"
                    for m in all_months
                )}

            </select>

        </form>

    </div>
    """
    
    # Add quick button to jump to current enrolled month
    if system_month in active_months and month != system_month:
        month_selector += f"""
        <form method="post" action="{url_for('student_set_month')}" style="margin-top:8px">
            <input type="hidden" name="month" value="{system_month}">
            <button class="btn success mini">
                Go to your enrolled month ({pretty_month_label(system_month)})
            </button>
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
        <div class='card soft' style="display:block !important; width:100%; margin-top:12px;">
            <h3>Not enrolled for {pretty_month_label(month)}</h3>
            <p class='muted'>
                Enrollments status pending. You can add subjects now.
            </p>
            <a class='btn' href='{url_for("home")}' style="display:inline-block;">
                Enroll now
            </a>
        </div>
        """



    # WhatsApp links for enrolled subjects
    group_html = "<div class='empty'>No group links yet.</div>"

    if has_active_enrollment and active_sub_ids:

        q = f"""
        SELECT g.subject_id, g.invite_link, s.name, s.grade
        FROM groups g
        JOIN subjects s ON s.id = g.subject_id
        WHERE g.month = 'ALL'
          AND g.is_visible = 1
          AND g.subject_id IN ({','.join('?' * len(active_sub_ids))})
        ORDER BY CAST(REPLACE(s.grade,'G','') AS INTEGER), s.name
        """

        cur.execute(q, (*active_sub_ids,))
        gs = cur.fetchall()

        if gs:

            cards = []

            for r in gs:

                cards.append(f"""
                <div class="card soft" style="border-left:5px solid #25D366">

                    <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap">

                        <div>
                            <div style="font-weight:600">
                                {grade_label(r['grade'])} — {r['name']}
                            </div>

                            <div class="mini muted">
                                Official subject WhatsApp group
                            </div>
                        </div>

                        <a class="btn success mini"
                           target="_blank"
                           href="{r['invite_link']}">

                           Join Group

                        </a>

                    </div>

                </div>
                """)

            group_html = f"""
            <div class="grid" style="gap:10px">
                {''.join(cards)}
            </div>
            """


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

            cards = []

            for r in sess:

                meet_btn = ""

                if r['meet_link']:
                    meet_btn = f"""
                    <a class="btn success mini"
                       target="_blank"
                       href="{r['meet_link']}">
                       Join Class Session
                    </a>
                    """

                cards.append(f"""
                <div class="card soft" style="border-left:5px solid #3b82f6">

                    <div style="
                        display:flex;
                        justify-content:space-between;
                        align-items:center;
                        flex-wrap:wrap;
                        gap:10px;
                    ">

                        <div>

                            <div style="font-weight:600">
                                {grade_label(r['grade'])} — {r['subject_name']}
                            </div>

                            <div class="mini muted">
                                {DOW[r['day_of_week']]} • {r['start_time']} - {r['end_time']}
                            </div>

                        </div>

                        <div>
                            {meet_btn}
                        </div>

                    </div>

                </div>
                """)

            sessions_html = f"""
            <div class="grid" style="gap:10px">
                {''.join(cards)}
            </div>
            """



    # Materials & Assignments grouped by subject
    materials_html = "<div class='empty'>No materials yet.</div>"

    assignments = []
    
    if has_active_enrollment and active_sub_ids:

        cur.execute(f"""
            SELECT m.*, sub.name AS subject_name, sub.grade, t.full_name AS tutor_name
            FROM materials m
            JOIN subjects sub ON sub.id=m.subject_id
            JOIN tutors t ON t.id=m.tutor_id
            WHERE m.subject_id IN ({','.join('?'*len(active_sub_ids))})
              AND m.month IN (?, ?)
            ORDER BY sub.grade, sub.name, m.created_at DESC
        """, (*active_sub_ids, month, system_month))

        mats = cur.fetchall()
        
        if mats:
            grouped = {}

            for m in mats:

                subject_key = f"{grade_label(m['grade'])} — {m['subject_name']}"

                if subject_key not in grouped:
                    grouped[subject_key] = {
                        "assignments": [],
                        "recordings": [],
                        "documents": []
                    }

                is_assignment = (m['is_assignment']==1 or m['kind']=='assignment')

                # DEFINE FIRST
                is_recording = bool(m['youtube_url'])

                when = m['created_at'][:16].replace('T',' ')

                link = (
                    f"<a class='links' target='_blank' href='{m['file_path']}'>Download</a>"
                    if m['kind'] in ('file','assignment') and m['file_path']
                    else
                    f"<a class='links' target='_blank' href='{m['youtube_url']}'>Open</a>"
                )

                # USE AFTER DEFINITION
                icon = "🎥 " if is_recording else "📄 "

                row_html = f"""
                <tr>
                    <td>{icon}{m['title']} {"<span class='badge'>assignment</span>" if is_assignment else ""}</td>
                    <td>{m['tutor_name']}</td>
                    <td>{when}</td>
                    <td>{link}</td>
                </tr>
                """

                # classification
                if is_assignment:

                    grouped[subject_key]["assignments"].append(row_html)
                    assignments.append(m)

                elif is_recording:

                    grouped[subject_key]["recordings"].append(row_html)

                else:

                    grouped[subject_key]["documents"].append(row_html)


            blocks = []

            for subject, content in grouped.items():

                subject_block = f"""
                <div class='card soft' style="margin-bottom:16px">

                    <h3 style="margin-bottom:10px">{subject}</h3>
                """

                if content["assignments"]:

                    subject_block += f"""
                    <h4 class="mini muted">Assignments</h4>

                    <div class="scroll-x">
                    <table>
                    <thead>
                        <tr>
                            <th>Title</th>
                            <th>Tutor</th>
                            <th>Uploaded</th>
                            <th>Link</th>
                        </tr>
                    </thead>
                    <tbody>
                        {''.join(content["assignments"])}
                    </tbody>
                    </table>
                    </div>
                    """

                if content["recordings"]:

                    subject_block += f"""
                    <h4 class="mini muted" style="margin-top:12px;color:#2563eb">
                        Session Recordings
                    </h4>

                    <div class="scroll-x">
                    <table>
                    <thead>
                        <tr>
                            <th>Recording</th>
                            <th>Tutor</th>
                            <th>Uploaded</th>
                            <th>Watch</th>
                        </tr>
                    </thead>
                    <tbody>
                        {''.join(content["recordings"])}
                    </tbody>
                    </table>
                    </div>
                    """

                if content["documents"]:

                    subject_block += f"""
                    <h4 class="mini muted" style="margin-top:12px;color:#16a34a">
                        Documents & Notes
                    </h4>

                    <div class="scroll-x">
                    <table>
                    <thead>
                        <tr>
                            <th>Document</th>
                            <th>Tutor</th>
                            <th>Uploaded</th>
                            <th>Open</th>
                        </tr>
                    </thead>
                    <tbody>
                        {''.join(content["documents"])}
                    </tbody>
                    </table>
                    </div>
                    """

                subject_block += "</div>"

                blocks.append(subject_block)

            materials_html = "".join(blocks)

    # Assignment submission blocks (top priority)
    submit_blocks = []

    if assignments:

        for m in assignments:

            due = m['due_date'] or ''

            cur.execute(
                "SELECT id,file_path,mark,feedback,submitted_at "
                "FROM submissions WHERE material_id=? AND student_id=?",
                (m['id'], sid)
            )

            sub = cur.fetchone()

            maxp = m['max_points'] if m['max_points'] else 100

            if sub:

                mark = f" • Mark: {sub['mark']} / {maxp}" if sub['mark'] is not None else ""

                fb = (
                    f"<div class='muted mini'>Feedback: {sub['feedback']}</div>"
                    if sub['feedback'] else ""
                )

                submit_blocks.append(f"""
                    <div class='card'>
                        <b>{m['title']}</b> —
                        {grade_label(m['grade'])} {m['subject_name']}
                        • Due: {due or '—'}
                        <br>
                        Submitted:
                        {sub['submitted_at'][:16].replace('T',' ')}
                        {mark}
                        {fb}
                        <a class='links' href='{sub['file_path']}' target='_blank'>
                            Download your file
                        </a>
                    </div>
                """)

            else:

                allow = True

                if due:
                    try:
                        end = datetime.datetime.fromisoformat(
                            due+"T23:59:59+00:00"
                        )
                        allow = datetime.datetime.now(
                            datetime.timezone.utc
                        ) <= end
                    except Exception:
                        pass

                if allow:

                    submit_blocks.append(f"""
                        <div class='card'>
                            <b>{m['title']}</b> —
                            {grade_label(m['grade'])} {m['subject_name']}
                            • Due: {due or '—'}
                            • Total: {maxp}

                            <form method='post'
                                  action='{url_for('student_submit_assignment', mid=m['id'])}'
                                  enctype='multipart/form-data'
                                  class='grid'
                                  style='grid-template-columns:1fr auto;gap:10px;margin-top:8px'>

                                <input type='file'
                                       name='file'
                                       required
                                       accept='.pdf,.doc,.docx,.png,.jpg,.jpeg,.zip,.txt'/>

                                <button class='btn'>Submit</button>

                            </form>
                        </div>
                    """)

                else:

                    submit_blocks.append(f"""
                        <div class='card'>
                            <b>{m['title']}</b>
                            — Due: {due}
                            <span class='chip'>Closed</span>
                        </div>
                    """)


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
    # ================= STUDENT CHAT SYSTEM =================
    
    # build tutors_for_subject lookup (REQUIRED for chat system)
    tutors_for_subject = {}

    if has_active_enrollment and active_sub_ids:

        cur.execute(f"""
            SELECT ts.subject_id, t.id AS tutor_id, t.full_name
            FROM tutor_subjects ts
            JOIN tutors t ON t.id = ts.tutor_id
            WHERE ts.subject_id IN ({','.join('?'*len(active_sub_ids))})
        """, (*active_sub_ids,))

        for row in cur.fetchall():

            subject_id = row["subject_id"]

            if subject_id not in tutors_for_subject:
                tutors_for_subject[subject_id] = []

            tutors_for_subject[subject_id].append(
                (row["tutor_id"], row["full_name"])
            )


    # Build conversation list (one per tutor + subject)
    cur.execute(f"""
        SELECT DISTINCT
            dm.subject_id,
            t.id AS tutor_id,
            t.full_name AS tutor_name,
            s.name AS subject_name,
            s.grade
        FROM direct_messages dm
        JOIN tutors t ON t.id = CASE
            WHEN dm.from_role='tutor' THEN dm.from_id
            ELSE dm.to_id END
        JOIN subjects s ON s.id = dm.subject_id
        WHERE
            (dm.from_role='student' AND dm.from_id=?)
            OR
            (dm.to_role='student' AND dm.to_id=?)
        ORDER BY s.grade, s.name
    """, (sid, sid))

    conversations = [dict(row) for row in cur.fetchall()]

    # include tutors even if no conversation exists yet
    for subid in active_sub_ids:
        sid_int = int(subid)

        for (tid, tname) in tutors_for_subject.get(sid_int, []):

            exists = any(
                c["tutor_id"] == tid and c["subject_id"] == sid_int
                for c in conversations
            )

            if not exists:

                subj = next(
                    (f"{grade_label(e['grade'])} — {e['subject_name']}"
                     for e in enrolls if e['subject_id'] == sid_int),
                    "Subject"
                )

                conversations.append({
                    "tutor_id": tid,
                    "subject_id": sid_int,
                    "tutor_name": tname,
                    "subject_name": subj,
                    "grade": ""
                })

    # conversation selector
    conv_list = ""

    for c in conversations:

        grade_val = ""

        # works for BOTH sqlite3.Row and dict
        if isinstance(c, dict):
            grade_val = c.get("grade") or ""
        else:
            grade_val = c["grade"] if "grade" in c.keys() else ""

        grade_text = grade_label(grade_val) if grade_val else ""

        conv_list += f"""
        <div class="chat-user"
             onclick="loadChat({c['tutor_id']},{c['subject_id']})">

            <div style="font-weight:600">
                {c['tutor_name']}
            </div>

            <div class="mini muted">
                {grade_text} {c['subject_name']}
            </div>

        </div>
        """


    if not conv_list:
        conv_list = "<div class='empty'>No conversations yet.</div>"

    # messages window (default empty)
    chat_window = """
    <div class="chat-window">
        <div class="chat-messages" id="chatMessages">
            <div class="empty">Select a conversation</div>
        </div>

        <form method="post"
              action="/student/message"
              class="chat-input"
              id="chatForm">

            <input type="hidden" name="combo" id="chatCombo">

            <div style="display:flex;gap:8px">
                <input type="text"
                       name="body"
                       placeholder="Type message..."
                       required
                       style="flex:1">

                <button class="btn success">
                    Send
                </button>
            </div>

        </form>
    </div>
    """

    compose_block = f"""
    <div class="card">
        <h2>Messages</h2>

        <div class="chat-layout">

            <div class="chat-list">
                {conv_list}
            </div>

            {chat_window}

        </div>

    </div>

    <script>

    function loadChat(tutor_id, subject_id)
    {{
        document.getElementById("chatCombo").value =
            tutor_id + "|" + subject_id;

        fetch(`/student/messages/thread?tutor_id=${{tutor_id}}&subject_id=${{subject_id}}`)
        .then(r => r.text())
        .then(html =>
        {{
            document.getElementById("chatMessages").innerHTML = html;

            var box = document.getElementById("chatMessages");
            box.scrollTop = box.scrollHeight;
        }});
    }}

    </script>
    """

    conn.close()

    # Enrollment list UI
    if enrolls:
        e_rows="".join([
          f"""
          <tr>
            <td data-label="Subject">
                {grade_label(r['grade'])} — {r['subject_name']}
            </td>
            <td data-label="Status">
                <span class='chip {r['status'].lower()}'>{r['status']}</span>
            </td>
          </tr>
          """
          for r in enrolls
        ])

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


    body=fr"""
    <section class='grid'>
    <div class='card'>
        <h1>Welcome, {session.get('student_name','Student')}</h1>
            <p class='muted' style="margin-bottom:6px">
                Currently viewing: <b>{pretty_month_label(month)}</b>
            </p>

            {month_selector}

        <h2>Your Enrollments</h2>
        {enr_html}
        {enroll_cta}

        <p class='mini muted'>To add more subjects, submit the Home form again with your phone number and the new subjects + PoP.</p>
    </div>
    <div class='card soft' style="border-left:5px solid #25D366;">
        <h2>EBTA Notifications Groups</h2>

        <p class="mini muted" style="margin-bottom:12px;">
            Join these official EBTA WhatsApp groups to receive important announcements and updates.
        </p>

        <div class="grid" style="gap:10px">

            <a class="btn success"
               target="_blank"
               href="https://chat.whatsapp.com/HfmZyzcU9bMDB3N1DAuFrJ"
               style="display:block;text-align:center">

                EBTA Learners Notifications

            </a>

            <a class="btn secondary"
               target="_blank"
               href="https://chat.whatsapp.com/DZYMvnEl9jpEyvzxqbgwV6"
               style="display:block;text-align:center">

                EBTA Parents Notifications

            </a>

        </div>

    </div>



    <div class='card' style="border-left:5px solid #25D366">

        <h2 style="display:flex;align-items:center;gap:8px">
            Subject WhatsApp Groups
        </h2>

        <div class="mini muted" style="margin-bottom:12px">
            Join your subject-specific WhatsApp groups for class communication.
        </div>

        {group_html}

    </div>

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

    r = require_student()
    if r:
        return r

    sid = is_student()
    file = request.files.get('file')

    if not file or not file.filename:
        return page("Error", card_msg("Please select a file."))

    conn = get_db()
    cur = conn.cursor()

    # Get assignment
    cur.execute("""
        SELECT id, subject_id, month, due_date,
               is_assignment, kind
        FROM materials
        WHERE id=?
    """, (mid,))
    m = cur.fetchone()

    # Validate assignment exists and is assignment
    if not m or (m['kind'] != 'assignment' and m['is_assignment'] != 1):
        conn.close()
        return page("Error", card_msg("Invalid assignment."))

    assignment_month = m['month']

    # Check student was ACTIVE in that assignment month
    cur.execute("""
        SELECT 1
        FROM enrollments
        WHERE student_id=?
        AND subject_id=?
        AND month=?
        AND status='ACTIVE'
        LIMIT 1
    """, (sid, m['subject_id'], assignment_month))

    enrolled = cur.fetchone()

    if not enrolled:
        conn.close()
        return page("Error", card_msg(
            f"You were not enrolled for this subject in {pretty_month_label(assignment_month)}."
        ))

    # Check due date ONLY (NOT system month)
    if m['due_date']:
        try:
            end = datetime.datetime.fromisoformat(
                m['due_date'] + "T23:59:59+00:00"
            )
            now = datetime.datetime.now(datetime.timezone.utc)

            if now > end:
                conn.close()
                return page("Closed", card_msg("Submission window has closed."))

        except Exception:
            pass

    # Save submission
    safe = f"{int(datetime.datetime.now().timestamp())}_{sid}_{secure_name(file.filename)}"

    dest = SUBMISSIONS_DIR / safe

    file.save(dest)

    path = f"/submission-files/{safe}"

    now = now_utc_iso()

    cur.execute("""
        INSERT OR REPLACE INTO submissions
        (material_id, student_id, file_path, submitted_at)
        VALUES (?, ?, ?, ?)
    """, (mid, sid, path, now))

    conn.commit()
    conn.close()

    return page("Submitted", card_msg("Assignment submitted successfully."))

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
    
    
@app.get("/student/messages/thread")
def student_message_thread():

    r = require_student()
    if r: return r

    sid = is_student()

    tutor_id = request.args.get("tutor_id")
    subject_id = request.args.get("subject_id")

    if not tutor_id or not subject_id:
        return ""

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT *
        FROM direct_messages
        WHERE subject_id=?
        AND (
            (from_role='student' AND from_id=? AND to_role='tutor' AND to_id=?)
            OR
            (from_role='tutor' AND from_id=? AND to_role='student' AND to_id=?)
        )
        ORDER BY created_at ASC
    """, (subject_id, sid, tutor_id, tutor_id, sid))

    msgs = cur.fetchall()

    conn.close()

    html = ""

    for m in msgs:

        cls = "me" if m["from_role"] == "student" else "them"

        time = m["created_at"][:16].replace("T"," ")

        html += f"""
        <div class="bubble {cls}">
            {m['body']}
            <div class="time">{time}</div>
        </div>
        """

    return html


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
                <select name="phone_type">
                    <option value="SA">South Africa</option>
                    <option value="INT">International</option>
                </select>

                <input name='phone' required/>
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
                    <select name="phone_type">
                        <option value="SA">South Africa</option>
                        <option value="INT">International</option>
                    </select>

                    <input name='phone' required/>
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

    # Get raw input
    raw_phone = request.form.get('phone', '').strip()
    pin = request.form.get('pin', '').strip()

    # Normalize first (remove spaces, symbols)
    normalized = normalize_phone(raw_phone)

    # Generate all possible variants
    variants = phone_variants(normalized)

    if not variants:
        return page(
            "Login failed",
            card_msg("Phone number required.")
        )

    conn = get_db()
    cur = conn.cursor()

    # Build safe SQL placeholders (?, ?, ?, ...)
    placeholders = ",".join("?" * len(variants))

    cur.execute(f"""
        SELECT id, pin, full_name
        FROM tutors
        WHERE phone IN ({placeholders})
        LIMIT 1
    """, variants)

    tutor = cur.fetchone()

    conn.close()

    # Validate credentials
    if not tutor or not tutor['pin'] or tutor['pin'] != pin:
        return page(
            "Login failed",
            card_msg("Wrong phone or PIN.")
        )

    # Create session
    session['tutor_id'] = tutor['id']
    session['tutor_name'] = tutor['full_name']

    return redirect(url_for('tutor_home'))

@app.post('/tutor/forgot-pin')
def tutor_forgot_pin():
    raw_phone = request.form.get('phone','').strip()

    variants = phone_variants(raw_phone)

    phone = variants[0] if variants else ""
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
    
    # ADD THIS BLOCK HERE time slots
    real_month = datetime.datetime.now(ZoneInfo("Africa/Johannesburg")).strftime("%Y-%m")

    if month != real_month:
        month_selector += f"""
        <form method="post" action="{url_for('tutor_set_month')}" style="margin-top:8px">
            <input type="hidden" name="month" value="{real_month}">
            <button class="btn success mini">
                Go to current month ({pretty_month_label(real_month)})
            </button>
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

            cards = []

            for r in groups:

                cards.append(f"""
                <div class="card soft"
                     style="border-left:5px solid #25D366">

                    <div style="
                        display:flex;
                        justify-content:space-between;
                        align-items:center;
                        flex-wrap:wrap;
                        gap:10px;
                    ">

                        <div>

                            <div style="font-weight:600">
                                {grade_label(r['grade'])} — {r['name']}
                            </div>

                            <div class="mini muted">
                                WhatsApp class group
                            </div>

                        </div>

                        <a class="btn success mini"
                           target="_blank"
                           href="{r['invite_link']}">
                           Open Group
                        </a>

                    </div>

                </div>
                """)

            groups_html = f"""
            <div class="grid" style="gap:10px">
                {''.join(cards)}
            </div>
            """



    # Sessions for this tutor
    cur.execute("""SELECT se.id, se.subject_id, s.name AS subject_name, s.grade, se.day_of_week, se.start_time, se.end_time, se.meet_link
                FROM sessions se JOIN subjects s ON s.id=se.subject_id
                WHERE se.tutor_id=? AND se.active=1 ORDER BY se.day_of_week,se.start_time""",(tid,))
    sess=cur.fetchall()
    session_cards = []

    for r in sess:

        meet_btn = ""

        if r['meet_link']:
            meet_btn = f"""
            <a class="btn success mini"
               target="_blank"
               href="{r['meet_link']}">
               Join
            </a>
            """

        tools = f"""
        <a class="btn mini"
           href="{url_for('tutor_session_attendance', sid=r['id'])}">
           Attendance
        </a>
        """

        session_cards.append(f"""
        <div class="card soft"
             style="border-left:5px solid #3b82f6">

            <div style="
                display:flex;
                justify-content:space-between;
                align-items:center;
                flex-wrap:wrap;
                gap:10px;
            ">

                <div>

                    <div style="font-weight:600">
                        {grade_label(r['grade'])} — {r['subject_name']}
                    </div>

                    <div class="mini muted">
                        {DOW[r['day_of_week']]} • {r['start_time']} - {r['end_time']}
                    </div>

                </div>

                <div style="display:flex;gap:6px">

                    {meet_btn}
                    {tools}

                </div>

            </div>

        </div>
        """)

    sessions_html = (
        "<div class='empty'>No sessions yet.</div>"
        if not session_cards else
        f"<div class='grid' style='gap:10px'>{''.join(session_cards)}</div>"
    )


    # Upload form (assignments + due date + max points)
    subjects_options="".join([f"<option value='{r['subject_id']}'>{grade_label(r['grade'])} — {r['subject_name']}</option>" for r in subs]) or "<option value=''>No assigned subjects</option>"

    upload_block=f"""
    <div class='card' style="border-left:5px solid #22c55e">

        <h2 style="margin-bottom:6px">
            Upload Teaching Material
        </h2>

        <div class="mini muted" style="margin-bottom:16px">
            Choose what you are uploading. Recordings, documents, and assignments are organised automatically.
        </div>

        <form method='post'
              action='{url_for('tutor_upload')}'
              enctype='multipart/form-data'>

            <!-- SUBJECT -->
            <div style="margin-bottom:14px">
                <label><b>Subject</b></label>
                <select name='subject_id' required style="width:100%">
                    {subjects_options}
                </select>
            </div>


            <!-- TITLE -->
            <div style="margin-bottom:18px">
                <label><b>Title</b></label>
                <input name='title'
                       placeholder="Example: Photosynthesis Lesson 1"
                       required
                       style="width:100%">
            </div>


            <!-- RECORDING SECTION -->
            <div class="card soft"
                 style="border-left:5px solid #2563eb;margin-bottom:16px">

                <div style="font-weight:600">
                    🎥 Session Recording
                </div>

                <div class="mini muted" style="margin-bottom:8px">
                    Paste the Google drive link, YouTube recording link
                </div>

                <input name='youtube'
                       placeholder="https://youtube.com/..."
                       style="width:100%">
            </div>


            <!-- DOCUMENT SECTION -->
            <div class="card soft"
                 style="border-left:5px solid #16a34a;margin-bottom:16px">

                <div style="font-weight:600">
                    📄 Document / Notes
                </div>

                <div class="mini muted" style="margin-bottom:8px">
                    Upload slides, notes, worksheets, or resources
                </div>

                <input type='file'
                       name='file'
                       accept='.pdf,.doc,.docx,.png,.jpg,.jpeg,.zip,.ppt,.pptx'
                       style="width:100%">
            </div>


            <!-- ASSIGNMENT SECTION -->
            <div class="card soft"
                 style="border-left:5px solid #f59e0b;margin-bottom:16px">

                <div style="font-weight:600;margin-bottom:8px">
                    📝 Assignment (optional)
                </div>

                <label style="display:flex;gap:8px;margin-bottom:10px">
                    <input type='checkbox' name='is_assignment'>
                    Mark this upload as an assignment
                </label>

                <div class="grid"
                     style="grid-template-columns:1fr 1fr;gap:10px">

                    <div>
                        <label class="mini muted">Due date</label>
                        <input name='due'
                               type="date"
                               style="width:100%">
                    </div>

                    <div>
                        <label class="mini muted">Total marks</label>
                        <input name='max_points'
                               type='number'
                               min='1'
                               max='1000'
                               placeholder='100'
                               style="width:100%">
                    </div>

                </div>

            </div>


            <!-- SUBMIT -->
            <button class='btn success'
                    style="width:100%;padding:14px;font-size:16px">
                Upload Material
            </button>

        </form>

    </div>
    """

    # Your uploads (delete within 24h)
    cur.execute("""SELECT m.*, s.name AS subject_name, s.grade
                FROM materials m JOIN subjects s ON s.id=m.subject_id
                WHERE m.tutor_id=? ORDER BY m.created_at DESC LIMIT 200""",(tid,))
    mymats=cur.fetchall()
    def can_delete(ts, admin_unlocked):
        if admin_unlocked == 1:
            return True

        try:
            created = datetime.datetime.fromisoformat(ts)
            return (
                datetime.datetime.now(datetime.timezone.utc) - created
            ) <= datetime.timedelta(hours=24)
        except Exception:
            return False

        
    assign_rows = []
    record_rows = []
    doc_rows = []

    for m in mymats:

        when = m['created_at'][:16].replace('T', ' ')

        is_assignment = (m['is_assignment'] == 1 or m['kind'] == 'assignment')
        is_recording = bool(m['youtube_url'])

        # link
        link = "—"
        if m['file_path']:
            link = f"<a class='links' target='_blank' href='{m['file_path']}'>Download</a>"
        elif m['youtube_url']:
            link = f"<a class='links' target='_blank' href='{m['youtube_url']}'>Watch</a>"

        # icon
        if is_assignment:
            icon = "📝 "
        elif is_recording:
            icon = "🎥 "
        else:
            icon = "📄 "

        # delete button
        if can_delete(m['created_at'], m['admin_unlocked']):
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

        row = f"""
        <tr>
            <td>{grade_label(m['grade'])} — {m['subject_name']}</td>
            <td>{icon}{m['title']}</td>
            <td>{link}</td>
            <td>{when}</td>
            <td>{action}</td>
        </tr>
        """

        if is_assignment:
            assign_rows.append(row)
        elif is_recording:
            record_rows.append(row)
        else:
            doc_rows.append(row)

    uploads_html = ""

    if assign_rows:

        uploads_html += f"""
        <h3 style="margin-top:10px">📝 Assignments</h3>
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
            {''.join(assign_rows)}
        </tbody>
        </table>
        </div>
        """

    if record_rows:

        uploads_html += f"""
        <h3 style="margin-top:20px;color:#2563eb">🎥 Recordings</h3>
        <div class="scroll-x">
        <table>
        <thead>
            <tr>
                <th>Subject</th>
                <th>Recording</th>
                <th>Watch</th>
                <th>Uploaded</th>
                <th>Action</th>
            </tr>
        </thead>
        <tbody>
            {''.join(record_rows)}
        </tbody>
        </table>
        </div>
        """

    if doc_rows:

        uploads_html += f"""
        <h3 style="margin-top:20px;color:#16a34a">📄 Documents</h3>
        <div class="scroll-x">
        <table>
        <thead>
            <tr>
                <th>Subject</th>
                <th>Document</th>
                <th>File</th>
                <th>Uploaded</th>
                <th>Action</th>
            </tr>
        </thead>
        <tbody>
            {''.join(doc_rows)}
        </tbody>
        </table>
        </div>
        """

    if not uploads_html:
        uploads_html = "<div class='empty'>No uploads yet.</div>"

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
        cur.execute("""SELECT st.id, st.full_name, st.phone_whatsapp
            FROM enrollments e 
            JOIN students st ON st.id=e.student_id
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
            rows.append(f"""
            <tr>
                <td>
                    {st['full_name']}
                    <div class='mini muted'>{st['phone_whatsapp'] or '—'}</div>
                </td>

                <td>
                    {st['phone_whatsapp'] or '—'}
                </td>

                <td>
                    {c}
                </td>

                <td>
                    {rate}
                </td>

                <td>
                    {'-' if avgm is None else int(round(avgm))}
                </td>

            </tr>
            """)

            message_student_options.append((st['id'], s['subject_id'], f"{st['full_name']} — {grade_label(s['grade'])} {s['subject_name']}"))
        table = (
            "<div class='empty'>No active students.</div>"
            if not rows
            else f"<div class='scroll-x'><table><thead><tr>"
                 f"<th>Student</th><th>Phone</th><th>Attendance</th><th>Rate</th><th>Avg mark</th>"
                 f"</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
        )

        stu_sections.append(f"""
        <div class='card' id='students'>
            <h3>{grade_label(s['grade'])} — {s['subject_name']}</h3>
            {table}
        </div>
        """)


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
    
    # =========================
    # Tutor ↔ Admin chat
    # =========================

    cur.execute("""
        SELECT dm.*, 
               CASE dm.from_role
                    WHEN 'admin' THEN 'Admin'
                    ELSE (SELECT full_name FROM tutors WHERE id=dm.from_id)
               END AS from_name
        FROM direct_messages dm
        WHERE
            (dm.from_role='admin' AND dm.to_role='tutor' AND dm.to_id=?)
            OR
            (dm.from_role='tutor' AND dm.from_id=? AND dm.to_role='admin')
        ORDER BY dm.created_at ASC
        LIMIT 100
    """, (tid, tid))

    admin_msgs = cur.fetchall()

    admin_chat_html = ""

    for m in admin_msgs:

        side = "me" if m["from_role"] == "tutor" else "them"

        time = m["created_at"][11:16]

        admin_chat_html += f"""
        <div class="bubble {side}">
            {m['body']}
            <div class="time">{time}</div>
        </div>
        """

    if not admin_chat_html:
        admin_chat_html = "<div class='empty'>No admin messages yet.</div>"


    # =========================
    # Compose forms (UPDATED)
    # =========================

    # Broadcast to ALL students across tutor subjects
    broadcast_all = "<option value='ALL'>All My Students</option>"

    # Broadcast per subject
    subject_broadcast_opts = "".join([
        f"<option value='SUBJECT_ALL|{s['subject_id']}'>All students — {grade_label(s['grade'])} {s['subject_name']}</option>"
        for s in subs
    ])
    
    # Broadcast per grade
    grades = sorted({s['grade'] for s in subs})

    grade_broadcast_opts = "".join([
        f"<option value='GRADE_ALL|{g}'>All students — {grade_label(g)}</option>"
        for g in grades
    ])


    # Individual students
    individual_opts = "".join([
        f"<option value='{sid}|{subid}'>{label}</option>"
        for sid, subid, label in message_student_options
    ]) or "<option value=''>No students</option>"

    # Final dropdown with categories
    stud_opts = f"""
    <optgroup label="Broadcast All">
        {broadcast_all}
    </optgroup>

    <optgroup label="Broadcast by Grade">
        {grade_broadcast_opts}
    </optgroup>

    <optgroup label="Broadcast by Subject">
        {subject_broadcast_opts}
    </optgroup>

    <optgroup label="Individual Students">
        {individual_opts}
    </optgroup>
    """



    # =========================
    # Inbox Card UI
    # =========================
    # get all conversations

    cur.execute("""
    SELECT DISTINCT
        CASE
            WHEN from_role='student' THEN from_id
            ELSE to_id
        END AS student_id,
        st.full_name
    FROM direct_messages dm
    JOIN students st
    ON st.id =
        CASE
            WHEN dm.from_role='student' THEN dm.from_id
            ELSE dm.to_id
        END
    WHERE
        (dm.to_role='tutor' AND dm.to_id=?)
        OR
        (dm.from_role='tutor' AND dm.from_id=?)
    ORDER BY st.full_name
    """,(tid,tid))

    conversations = cur.fetchall()

    selected = request.args.get("chat")

    # build conversation list

    chat_list = ""

    for c in conversations:

        cur.execute("""
            SELECT body, created_at
            FROM direct_messages
            WHERE
            (from_role='student' AND from_id=? AND to_role='tutor' AND to_id=?)
            OR
            (from_role='tutor' AND from_id=? AND to_role='student' AND to_id=?)
            ORDER BY created_at DESC LIMIT 1
        """,(c["student_id"],tid,tid,c["student_id"]))

        last = cur.fetchone()

        preview = (last["body"][:30] + "...") if last else ""
        time = last["created_at"][11:16] if last else ""

        active = "active" if str(c["student_id"]) == str(selected) else ""

        chat_list += f"""
        <a href="?chat={c['student_id']}" class="chat-user {active}">
            <div style="font-weight:600">{c['full_name']}</div>
            <div class="mini muted">{preview}</div>
            <div class="mini muted">{time}</div>
        </a>
        """

    chat_messages = ""
    subject_id_for_chat = 0

    if selected:

        cur.execute("""
        SELECT dm.*, st.full_name, dm.subject_id
        FROM direct_messages dm
        LEFT JOIN students st ON st.id =
            CASE
                WHEN dm.from_role='student' THEN dm.from_id
                ELSE dm.to_id
            END
        WHERE
        (
            dm.from_role='student'
            AND dm.from_id=?
            AND dm.to_role='tutor'
            AND dm.to_id=?
        )
        OR
        (
            dm.from_role='tutor'
            AND dm.from_id=?
            AND dm.to_role='student'
            AND dm.to_id=?
        )
        ORDER BY dm.created_at ASC
        """,(selected,tid,tid,selected))

        msgs = cur.fetchall()

        subject_id_for_chat = msgs[0]["subject_id"] if msgs and msgs[0]["subject_id"] else 0

        for m in msgs:

            side = "me" if m["from_role"]=="tutor" else "them"

            time = m["created_at"][11:16]

            chat_messages += f"""
            <div class="bubble {side}">
                {m['body']}
                <div class="time">{time}</div>
            </div>
            """

     
    chat_header = ""

    if selected:
        chat_header = f"""
        <div style="padding:12px;border-bottom:1px solid var(--border);font-weight:600">
            {next((c['full_name'] for c in conversations if str(c['student_id'])==str(selected)), '')}
        </div>
        """
        
    message_form = f"""
    <div class="card">
    <h2>Send Message</h2>

    <form method="post" action="{url_for('tutor_message_student')}" class="grid">

    <label>Send to</label>

    <select name="combo" required>
    {stud_opts}
    </select>

    <label>Message</label>

    <textarea name="body" required></textarea>

    <button class="btn success">
    Send Message
    </button>

    </form>

    </div>
    """

    
    inbox_card = f"""
    <div class="card">

    <h2>Messages</h2>

    <div class="chat-layout">

    <div class="chat-list">
    {chat_list or "<div class='empty'>No conversations</div>"}
    </div>

    <div class="chat-window">

    {chat_header}

    <div class="chat-messages">
    {chat_messages or "<div class='empty'>Select conversation</div>"}
    </div>

    <div class="chat-input">

    <form method="post" action="{url_for('tutor_message_student')}">

    {"<input type='hidden' name='combo' value='" + str(selected) + "|" + str(subject_id_for_chat) + "'>" if selected else ""}

    <textarea name="body" placeholder="Type message..." required {"disabled" if not selected else ""}></textarea>


    <button class="btn success mini" {"disabled" if not selected else ""}>Send</button>


    </form>

    </div>

    </div>

    </div>

    </div>
    """
    
    admin_chat_card = f"""
    <div class="card">

        <h2>Message Admin</h2>

        <div class="chat-window">

            <div class="chat-messages">
                {admin_chat_html}
            </div>

            <div class="chat-input">

                <form method="post"
                      action="{url_for('tutor_message_admin')}">

                    <textarea name="body"
                              placeholder="Message admin..."
                              required></textarea>

                    <button class="btn success mini">
                        Send
                    </button>

                </form>

            </div>

        </div>

    </div>
    """



    conn.close()


    body=fr"""
    <section class='grid'>

    <div class='card'>

        <h1>Welcome, {session.get('tutor_name','Tutor')}</h1>

        <div class="card soft"
             style="margin-top:12px;border-left:5px solid #3b82f6">

            <div style="font-weight:600;font-size:16px;margin-bottom:4px">
                Viewing Month
            </div>

            <div style="font-size:20px;font-weight:700;margin-bottom:8px">
                {pretty_month_label(month)}
            </div>

            <div class="mini muted" style="margin-bottom:10px">
                Switch month to new view
            </div>

            <form method="post"
                  action="{url_for('tutor_set_month')}">

                <select name="month"
                        onchange="this.form.submit()"
                        style="
                            width:100%;
                            padding:12px;
                            font-size:16px;
                            border-radius:10px;
                            border:2px solid #3b82f6;
                            background:#fff;
                            cursor:pointer;
                        ">

                    {''.join(
                        f"<option value='{m}' "
                        f"{'selected' if m == month else ''}>"
                        f"{'✓ ' if m in active_months else ''}"
                        f"{pretty_month_label(m)}"
                        f"{'' if m in active_months else ' (no students)'}"
                        f"</option>"
                        for m in all_months
                    )}

                </select>

            </form>

        </div>

        <div style="margin-top:12px">
            {assigned_list}
        </div>

    </div>


    <div class='card'><h2>WhatsApp Group Links</h2>{groups_html}</div>

    <div class='card'><h2>Your sessions</h2>
        {sessions_html}
    </div>


    {upload_block}

    <div class='card'><h2>Your uploads</h2>{uploads_html}</div>

    <div class='card'><h2>Your assignments</h2>
        <div class="scroll-x"><table><thead><tr><th>Subject</th><th>Title</th><th>Due</th><th>Total</th><th>Manage</th></tr></thead><tbody>{asg_rows}</tbody></table></div>
    </div>
    {message_form}
    {inbox_card}
    {admin_chat_card}

    <div id="students">
        {''.join(stu_sections)}
    </div>

    </section>
    """
    return page("Tutor Portal", body)



@app.post('/tutor/upload')
def tutor_upload():
    r=require_tutor()
    if r: return r
    tid=is_tutor(); month = get_active_month('tutor')
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
    cur.execute("""
        SELECT s.uploads_locked
        FROM tutor_subjects ts
        JOIN subjects s ON s.id = ts.subject_id
        WHERE ts.tutor_id=? AND ts.subject_id=?
    """, (tid, subject_id))

    row = cur.fetchone()

    if not row:
        conn.close()
        return page("Error", card_msg("This subject is not assigned to you."))

    if row["uploads_locked"] == 1:
        conn.close()
        return page(
            "Uploads Locked",
            card_msg("Uploads and assignments are currently locked for this subject. Contact Admin.")
        )

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
    return redirect(url_for('tutor_home'))

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
    return redirect(url_for('tutor_home'))

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
    month = get_active_month('tutor')
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
        <p class='muted'>
        {grade_label(m['grade'])} — {m['subject_name']}
        • Due: {m['due_date'] or '—'}
        • Total: {total}
        </p>

        <div class="card soft" style="margin-top:10px;border-left:5px solid #f59e0b">

            <form method="post"
                  action="{url_for('tutor_extend_due_date', mid=mid)}"
                  class="inlineform"
                  style="display:flex;gap:10px;align-items:end;flex-wrap:wrap">

                <div>
                    <label class="mini muted">Extend due date</label>
                    <input type="date"
                           name="due_date"
                           value="{m['due_date'] or ''}"
                           required>
                </div>

                <button class="btn success mini">
                    Update Due Date
                </button>

            </form>

        </div>

        {table}
        </div>
    </section>
    """
    return page("Manage Assignment", body, extra_js=js_alert)
    
    
@app.post('/tutor/assignment/<int:mid>/extend')
def tutor_extend_due_date(mid:int):

    r = require_tutor()
    if r:
        return r

    tid = is_tutor()

    new_due = request.form.get("due_date", "").strip()

    if not new_due:
        return page("Error", card_msg("Due date required."))

    conn = get_db()
    cur = conn.cursor()

    # security check — tutor owns assignment
    cur.execute("""
        SELECT id
        FROM materials
        WHERE id=? AND tutor_id=?
    """, (mid, tid))

    if not cur.fetchone():
        conn.close()
        return page("Error", card_msg("Assignment not found."))

    # update due date
    cur.execute("""
        UPDATE materials
        SET due_date=?
        WHERE id=?
    """, (new_due, mid))

    conn.commit()
    conn.close()

    return redirect(url_for(
        'tutor_assignment_manage',
        mid=mid,
        saved=1
    ))


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

# Tutor → Student message (individual OR broadcast)
@app.post('/tutor/message-student')
def tutor_message_student():

    r = require_tutor()
    if r:
        return r

    tid = is_tutor()

    combo = request.form.get('combo', '')
    body = request.form.get('body', '').strip()

    if not body:
        return page("Error", card_msg("Message cannot be empty."))

    conn = get_db()
    cur = conn.cursor()

    month = get_active_month('tutor')
    now = now_utc_iso()

    # ========================
    # SEND TO ALL STUDENTS (ALL SUBJECTS)
    # ========================
    if combo == "ALL":

        cur.execute("""
            SELECT DISTINCT e.student_id, e.subject_id
            FROM enrollments e
            JOIN tutor_subjects ts ON ts.subject_id = e.subject_id
            WHERE ts.tutor_id = ?
            AND e.month = ?
            AND e.status = 'ACTIVE'
        """, (tid, month))

        rows = cur.fetchall()

        cur.executemany("""
            INSERT INTO direct_messages
            (from_role, from_id, to_role, to_id, subject_id, body, created_at)
            VALUES ('tutor', ?, 'student', ?, ?, ?, ?)
        """, [(tid, r['student_id'], r['subject_id'], body, now) for r in rows])

        conn.commit()
        conn.close()

        return redirect(url_for('tutor_home'))

    # ========================
    # SEND TO ALL STUDENTS IN ONE GRADE
    # ========================
    if combo.startswith("GRADE_ALL|"):

        try:
            grade = combo.split('|')[1]
        except:
            conn.close()
            return page("Error", card_msg("Invalid grade."))

        # verify tutor teaches this grade
        cur.execute("""
            SELECT DISTINCT s.id
            FROM tutor_subjects ts
            JOIN subjects s ON s.id = ts.subject_id
            WHERE ts.tutor_id=? AND s.grade=?
        """, (tid, grade))

        subjects = cur.fetchall()

        if not subjects:
            conn.close()
            return page("Error", card_msg("You are not assigned to that grade."))

        subject_ids = [s["id"] for s in subjects]

        q = f"""
            SELECT DISTINCT student_id, subject_id
            FROM enrollments
            WHERE subject_id IN ({','.join(['?']*len(subject_ids))})
            AND month=?
            AND status='ACTIVE'
        """

        cur.execute(q, subject_ids + [month])

        students = cur.fetchall()

        cur.executemany("""
            INSERT INTO direct_messages
            (from_role, from_id, to_role, to_id, subject_id, body, created_at)
            VALUES ('tutor', ?, 'student', ?, ?, ?, ?)
        """, [(tid, s['student_id'], s['subject_id'], body, now) for s in students])

        conn.commit()
        conn.close()

        return redirect(url_for('tutor_home'))


    # ========================
    # SEND TO INDIVIDUAL STUDENT
    # ========================
    try:
        student_id_str, subject_id_str = combo.split('|', 1)
        student_id = int(student_id_str)
        subject_id = int(subject_id_str)
    except:
        conn.close()
        return page("Error", card_msg("Invalid selection."))

    # verify tutor teaches subject
    cur.execute("""
        SELECT 1
        FROM tutor_subjects
        WHERE tutor_id=? AND subject_id=?
    """, (tid, subject_id))

    if not cur.fetchone():
        conn.close()
        return page("Error", card_msg("Not allowed."))

    # verify student active
    cur.execute("""
        SELECT 1
        FROM enrollments
        WHERE student_id=?
        AND subject_id=?
        AND month=?
        AND status='ACTIVE'
    """, (student_id, subject_id, month))

    if not cur.fetchone():
        conn.close()
        return page("Error", card_msg("Student not active."))

    cur.execute("""
        INSERT INTO direct_messages
        (from_role, from_id, to_role, to_id, subject_id, body, created_at)
        VALUES ('tutor', ?, 'student', ?, ?, ?, ?)
    """, (tid, student_id, subject_id, body, now))

    conn.commit()
    conn.close()

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
    month = get_active_month('tutor')

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
        <a class="btn secondary" href="{url_for('admin_settings')}">Settings</a>
        <a class="btn secondary" href="{url_for('admin_uploads_control')}">Uploads Control</a>
        <a class="btn secondary" href="{url_for('admin_materials')}">Unlock Uploads</a>
        <a class="btn secondary" href="{url_for('admin_direct_messages')}">Direct Msgs</a>
        <a class="btn secondary" href="{url_for('admin_sms_dashboard')}">SMS Dashboard</a>
        <a class="btn secondary" href="{url_for('admin_process_sms')}">Processed SMS</a>

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
    <a class='btn secondary' href='{url_for('admin_settings')}'>Settings</a>
    <a class="btn secondary" href="{url_for('admin_uploads_control')}">Uploads Control</a>
    <a class="btn secondary" href="{url_for('admin_materials')}">Unlock Uploads</a>

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
    year = month.split('-')[0]

    page_num = int(request.args.get("page", 1))
    limit = 30
    offset = (page_num - 1) * limit

    conn = get_db()
    cur = conn.cursor()

    # Total count
    cur.execute("SELECT COUNT(*) AS c FROM enrollments WHERE month=?", (month,))
    total = cur.fetchone()['c']
    total_pages = (total + limit - 1) // limit

    # Main query
    cur.execute("""
        SELECT 
            e.id, e.student_id, e.status, e.amount_paid,
            e.pop_url, e.status_token,
            strftime('%Y-%m-%d %H:%M', datetime(e.created_at, '+2 hours')) AS created_at,
            st.full_name, st.phone_whatsapp, st.grade,
            sub.name AS subject_name
        FROM enrollments e
        JOIN students st ON st.id = e.student_id
        JOIN subjects sub ON sub.id = e.subject_id
        WHERE e.month = ?
        ORDER BY e.created_at DESC
        LIMIT ? OFFSET ?
    """, (month, limit, offset))
    rows = cur.fetchall()

    # Returning students
    cur.execute("""
        SELECT DISTINCT student_id
        FROM enrollments
        WHERE status='ACTIVE'
          AND substr(month,1,4)=?
          AND month < ?
    """, (year, month))
    returning_ids = {r['student_id'] for r in cur.fetchall()}

    # PoP files
    cur.execute("SELECT enrollment_id, file_path FROM enrollment_files")
    pop_map = {}
    for r in cur.fetchall():
        pop_map.setdefault(r['enrollment_id'], []).append(r['file_path'])

    conn.close()

    trs = []
    for r in rows:
        history = "Returning student" if r['student_id'] in returning_ids else "First month"

        files = pop_map.get(r['id'], []) or ([r['pop_url']] if r['pop_url'] else [])
        pop_html = " ".join(
            f"<a class='links' target='_blank' href='{p}'>PoP</a>" for p in files
        ) or "—"

        trs.append(f"""
        <tr>
            <td>{r['full_name']}<div class='muted'>{r['phone_whatsapp']}</div></td>
            <td>{grade_label(r['grade'])}</td>
            <td>{r['subject_name']}</td>
            <td><span class='chip {r['status'].lower()}'>{r['status']}</span></td>
            <td><span class='mini muted'>{history}</span></td>
            <td><span class='mini'>{r['created_at']}</span></td>
            <td>{pop_html}</td>
            <td><strong>R{r['amount_paid']}</strong></td>
            <td style="white-space:nowrap">
                <form method='post' action='{url_for('enrollment_action', id=r['id'], action='approve')}' style='display:inline'>
                    <input type="hidden" name="page" value="{page_num}">
                    <button class='btn success'>Approve</button>
                </form>
                <form method='post' action='{url_for('enrollment_action', id=r['id'], action='lapse')}' style='display:inline'>
                    <input type="hidden" name="page" value="{page_num}">
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
        """)

    # Build smart page range
    start = max(1, page_num - 3)
    end = min(total_pages, page_num + 3)

    page_links = []

    # First + Prev
    if page_num > 1:
        page_links.append(f"<a class='links' href='?page=1'>« First</a>")
        page_links.append(f"<a class='links' href='?page={page_num-1}'>‹ Prev</a>")

    # Numbered pages
    for p in range(start, end + 1):
        if p == page_num:
            page_links.append(f"<span class='current' style='padding:4px 8px;background:#0f172a;color:white;border-radius:6px'>{p}</span>")
        else:
            page_links.append(f"<a class='links' href='?page={p}'>{p}</a>")

    # Next + Last
    if page_num < total_pages:
        page_links.append(f"<a class='links' href='?page={page_num+1}'>Next ›</a>")
        page_links.append(f"<a class='links' href='?page={total_pages}'>Last »</a>")


    nav = f"""
    <div class='pager' style="
        display:flex;
        align-items:center;
        gap:8px;
        flex-wrap:wrap;
        margin:10px 0
    ">

        <span class="mini muted">
            Page {page_num} of {total_pages}
        </span>

        {"".join(page_links)}

        <form method="get"
              style="display:inline-flex;align-items:center;gap:6px;margin-left:10px">

            <span class="mini muted">Go to page</span>

            <input type="number"
                   name="page"
                   min="1"
                   max="{total_pages}"
                   value="{page_num}"
                   style="
                       width:70px;
                       padding:4px;
                       border-radius:6px;
                       border:1px solid #ccc
                   ">

            <button class="btn mini">Go</button>

        </form>

    </div>
    """


    body = f"""
    {admin_nav()}

    <section class='card'>
        <h1>Enrollments — {month}</h1>

        <div class='toolbar'>
            <input id='enr_q' class='pill'
                   placeholder='Search by name, phone, grade, subject'
                   oninput="filterTable('enr_q','enr_tbl')"/>
        </div>

        {nav}

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
                    {''.join(trs) or "<tr><td colspan='10'>No enrollments.</td></tr>"}
                </tbody>
            </table>
        </div>

        {nav}
    </section>
    """

    return page("Enrollments", body)

    

@app.post('/admin/enrollments/<int:id>/<action>')
def enrollment_action(id: int, action: str):
    r = require_admin()
    if r:
        return r
        
    page_num = int(request.form.get("page", 1))
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

    return redirect(url_for('admin_enrollments', page=page_num))

# --- Admin: Students (show Guardian & Email) ---

@app.get('/admin/students')
def admin_students():
    q = request.args.get("q", "").strip()
    q_safe = escape(q)
    if q:
        page_num = 1

    r = require_admin()
    if r:
        return r
    
    page_num = int(request.args.get("page", 1))
    limit = 20
    offset = (page_num - 1) * limit

    conn = get_db()
    cur = conn.cursor()

    if q:
        cur.execute("""
            SELECT COUNT(*) AS c
            FROM students
            WHERE
                full_name LIKE ?
                OR phone_whatsapp LIKE ?
                OR guardian_phone LIKE ?
                OR email LIKE ?
                OR school LIKE ?
        """, (f"%{q}%",)*5)
    else:
        cur.execute("SELECT COUNT(*) AS c FROM students")

    total = cur.fetchone()['c']
    total_pages = (total + limit - 1) // limit

    if q:
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
            WHERE
                full_name LIKE ?
                OR phone_whatsapp LIKE ?
                OR guardian_phone LIKE ?
                OR email LIKE ?
                OR school LIKE ?
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
        """, (f"%{q}%",)*5 + (limit, offset))
    else:
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
            LIMIT ? OFFSET ?
        """, (limit, offset))

    students = cur.fetchall()

    ids = [s['id'] for s in students]
    subject_map = {}

    if ids:
        qmarks = ",".join("?" * len(ids))
        cur.execute(f"""
            SELECT e.student_id, sub.name
            FROM enrollments e
            JOIN subjects sub ON sub.id = e.subject_id
            WHERE e.student_id IN ({qmarks})
        """, ids)

        for r in cur.fetchall():
            subject_map.setdefault(r['student_id'], []).append(r['name'])

    conn.close()

    def nz(v):
        return v if (v and str(v).strip()) else "N/A"

    trs = []
    for s in students:
        subjects = ", ".join(subject_map.get(s['id'], [])) or "N/A"
        pin = s['pin'] if s['pin'] else "<span class='muted'>not set</span>"

        trs.append(f"""
        <tr>
            <td>{s['full_name']}<div class='muted'>{s['phone_whatsapp']}</div></td>
            <td>{grade_label(s['grade'])}</td>
            <td>{subjects}</td>
            <td>{nz(s['guardian_phone'])}</td>
            <td>{nz(s['province'])}</td>
            <td>{nz(s['school'])}</td>
            <td>{nz(s['email'])}</td>
            <td>{pin}</td>
            <td style="white-space:nowrap">

                <a class='btn mini'
                   href='{url_for("admin_student_edit", sid=s["id"])}'>
                   Edit
                </a>

                <form method='post'
                      action='{url_for('admin_student_reset_pin', sid=s['id'])}'
                      style='display:inline'>
                    <button class='btn success mini'>
                        Reset
                    </button>
                </form>

                <form method='post'
                      action='{url_for('admin_student_delete', sid=s['id'])}'
                      style='display:inline'
                      onsubmit='return confirm("Delete this student?")'>
                    <button class='btn danger mini'>
                        Delete
                    </button>
                </form>

            </td>
        </tr>
        """)

    # Build smart page number range
    start = max(1, page_num - 3)
    end = min(total_pages, page_num + 3)

    page_links = []

    # First
    if page_num > 1:
        page_links.append(f"<a class='links' href='?page=1&q={q}'>« First</a>")
        page_links.append(f"<a class='links' href='?page={page_num-1}&q={q}'>‹ Prev</a>")

    # Numbered pages
    for p in range(start, end + 1):
        if p == page_num:
            page_links.append(f"<span class='current'>{p}</span>")
        else:
            page_links.append(f"<a class='links' href='?page={p}&q={q}'>{p}</a>")

    # Next
    if page_num < total_pages:
        page_links.append(f"<a class='links' href='?page={page_num+1}&q={q}'>Next ›</a>")
        page_links.append(f"<a class='links' href='?page={total_pages}&q={q}'>Last »</a>")


    nav = f"""
    <div class='pager' style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:10px 0">

        <span class="mini muted">
            Page {page_num} of {total_pages}
        </span>

        {"".join(page_links)}

        <form method="get" style="display:inline-flex;align-items:center;gap:6px;margin-left:10px">
            <span class="mini muted">Go to page</span>
            <input type="number"
                   name="page"
                   min="1"
                   max="{total_pages}"
                   value="{page_num}"
                   style="width:70px;padding:4px;border-radius:6px;border:1px solid #ccc">
            <button class="btn mini">Go</button>
        </form>

    </div>
    """


    body = f"""
    {admin_nav()}
    <section class='card'>
        <h1>Students</h1>

        <div class='toolbar'>
            <form method="get" style="display:flex;gap:8px">
                <input name="q"
                       value="{q_safe}"
                       placeholder="Search students (name, phone, email, school)"
                       style="padding:6px;border-radius:8px;border:1px solid #ccc">
                <button class="btn mini">Search</button>
            </form>

        </div>

        {nav}

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
                    {''.join(trs) or "<tr><td colspan='9'>No students.</td></tr>"}
                </tbody>
            </table>
        </div>

        {nav}
    </section>
    """

    return page("Students", body)


@app.get('/admin/students/<int:sid>/edit')
def admin_student_edit(sid):

    r = require_admin()
    if r:
        return r

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT *
        FROM students
        WHERE id=?
    """, (sid,))

    s = cur.fetchone()
    conn.close()

    if not s:
        return page("Not found", card_msg("Student not found."))

    def val(x):
        return escape(x) if x else ""

    body = f"""
    {admin_nav()}

    <section class='card' style='max-width:600px'>

        <h1>Edit Student</h1>

        <form method="post"
              action="{url_for('admin_student_update', sid=sid)}"
              class="grid">

            <div>
                <label>Full Name</label>
                <input name="full_name"
                       value="{val(s['full_name'])}"
                       required>
            </div>

            <div>
                <label>WhatsApp Phone</label>
                <input name="phone_whatsapp"
                       value="{val(s['phone_whatsapp'])}"
                       required>
            </div>

            <div>
                <label>Guardian Phone</label>
                <input name="guardian_phone"
                       value="{val(s['guardian_phone'])}">
            </div>

            <div>
                <label>Email</label>
                <input name="email"
                       value="{val(s['email'])}">
            </div>

            <div>
                <label>Grade</label>
                <select name="grade" required>

                    {''.join(
                        f"<option value='G{i}' {'selected' if s['grade']==f'G{i}' else ''}>Grade {i}</option>"
                        for i in range(8, 14)
                    )}

                </select>
            </div>

            <div>
                <label>Province</label>
                <input name="province"
                       value="{val(s['province'])}">
            </div>

            <div>
                <label>School</label>
                <input name="school"
                       value="{val(s['school'])}">
            </div>

            <div style="display:flex;gap:10px;margin-top:10px">

                <button class="btn success">
                    Save Changes
                </button>

                <a class="btn"
                   href="{url_for('admin_students')}">
                   Cancel
                </a>

            </div>

        </form>

    </section>
    """

    return page("Edit Student", body)
    
    
@app.post('/admin/students/<int:sid>/edit')
def admin_student_update(sid):

    r = require_admin()
    if r:
        return r

    full_name = request.form.get("full_name","").strip()
    phone = request.form.get("phone_whatsapp","").strip()
    guardian = request.form.get("guardian_phone","").strip()
    email = request.form.get("email","").strip()
    grade = request.form.get("grade","").strip()
    province = request.form.get("province","").strip()
    school = request.form.get("school","").strip()

    if not full_name or not phone:
        return page(
            "Error",
            card_msg("Full name and phone are required.")
        )

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        UPDATE students
        SET
            full_name=?,
            phone_whatsapp=?,
            guardian_phone=?,
            email=?,
            grade=?,
            province=?,
            school=?
        WHERE id=?
    """, (
        full_name,
        phone,
        guardian,
        email,
        grade,
        province,
        school,
        sid
    ))

    conn.commit()
    conn.close()

    return redirect(url_for("admin_students"))


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
    
    
@app.get('/admin/uploads-control')
def admin_uploads_control():

    r = require_admin()
    if r:
        return r

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT id, name, grade, uploads_locked
        FROM subjects
        ORDER BY grade, name
    """)

    subs = cur.fetchall()
    conn.close()

    rows = []

    for s in subs:

        locked = s["uploads_locked"] == 1

        status = (
            "<span class='chip danger'>Locked</span>"
            if locked else
            "<span class='chip success'>Unlocked</span>"
        )

        if locked:
            action = f"""
            <a class='btn success mini'
               href='/admin/uploads-unlock/{s["id"]}'>
               Unlock
            </a>
            """
        else:
            action = f"""
            <a class='btn danger mini'
               href='/admin/uploads-lock/{s["id"]}'>
               Lock
            </a>
            """

        rows.append(f"""
        <tr>
            <td>{grade_label(s['grade'])}</td>
            <td>{s['name']}</td>
            <td>{status}</td>
            <td>{action}</td>
        </tr>
        """)

    body = f"""
    {admin_nav()}

    <section class='card' id="uploads-control">

        <h1>Uploads & Assignments Control</h1>

        <div class='muted mini'>
            Locking prevents tutors from uploading materials or assignments for that subject.
        </div>

        <div class="scroll-x" style="margin-top:12px">

        <table>

            <thead>
                <tr>
                    <th>Grade</th>
                    <th>Subject</th>
                    <th>Status</th>
                    <th>Action</th>
                </tr>
            </thead>

            <tbody>
                {''.join(rows) or "<tr><td colspan='4'>No subjects found.</td></tr>"}
            </tbody>

        </table>

        </div>

    </section>
    """

    return page("Uploads Control", body)

    
@app.get('/admin/uploads-lock/<int:subject_id>')
def admin_uploads_lock(subject_id):
    r = require_admin()
    if r: return r

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        UPDATE subjects
        SET uploads_locked=1
        WHERE id=?
    """, (subject_id,))

    conn.commit()
    conn.close()

    return redirect(url_for('admin_uploads_control'))
   
@app.get('/admin/uploads-unlock/<int:subject_id>')
def admin_uploads_unlock(subject_id):
    r = require_admin()
    if r: return r

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        UPDATE subjects
        SET uploads_locked=0
        WHERE id=?
    """, (subject_id,))

    conn.commit()
    conn.close()

    return redirect(url_for('admin_uploads_control'))
    
@app.get('/admin/materials')
def admin_materials():

    r = require_admin()
    if r:
        return r

    page_num = int(request.args.get("page", 1))
    limit = 20
    offset = (page_num - 1) * limit

    conn = get_db()
    cur = conn.cursor()

    # total count
    cur.execute("SELECT COUNT(*) AS c FROM materials")
    total = cur.fetchone()["c"]
    total_pages = (total + limit - 1) // limit

    # fetch page
    cur.execute("""
        SELECT
            m.id,
            m.title,
            m.created_at,
            m.admin_unlocked,
            s.name AS subject,
            s.grade,
            t.full_name AS tutor
        FROM materials m
        JOIN subjects s ON s.id = m.subject_id
        JOIN tutors t ON t.id = m.tutor_id
        ORDER BY m.created_at DESC
        LIMIT ? OFFSET ?
    """, (limit, offset))

    rows = cur.fetchall()
    conn.close()

    trs = []

    for row in rows:

        locked = row["admin_unlocked"] == 0

        status = (
            "<span class='chip danger'>Locked</span>"
            if locked else
            "<span class='chip success'>Unlocked</span>"
        )

        if locked:

            action = f"""
            <form method='post'
                  action='{url_for('admin_unlock_material', mid=row["id"])}'
                  style='display:inline'>
                <input type="hidden" name="page" value="{page_num}">
                <button class='btn success mini'>Unlock</button>
            </form>
            """

        else:

            action = f"""
            <form method='post'
                  action='{url_for('admin_relock_material', mid=row["id"])}'
                  style='display:inline'>
                <input type="hidden" name="page" value="{page_num}">
                <button class='btn warning mini'>Relock</button>
            </form>
            """

        # ADD DELETE BUTTON (always visible)

        action += f"""
        <form method='post'
              action='{url_for('admin_delete_material', mid=row["id"])}'
              style='display:inline'
              onsubmit="return confirm('Delete this material permanently?')">

            <input type="hidden" name="page" value="{page_num}">

            <button class='btn danger mini'>
                Delete
            </button>

        </form>
        """


        trs.append(f"""
        <tr>
            <td>{grade_label(row['grade'])} — {row['subject']}</td>
            <td>{row['title']}</td>
            <td>{row['tutor']}</td>
            <td>{row['created_at'][:16].replace('T',' ')}</td>
            <td>{status}</td>
            <td>{action}</td>
        </tr>
        """)

    # pager logic
    start = max(1, page_num - 3)
    end = min(total_pages, page_num + 3)

    page_links = []

    if page_num > 1:
        page_links.append(f"<a class='links' href='?page=1'>« First</a>")
        page_links.append(f"<a class='links' href='?page={page_num-1}'>‹ Prev</a>")

    for p in range(start, end + 1):

        if p == page_num:
            page_links.append(
                f"<span class='current' style='padding:4px 8px;background:#0f172a;color:white;border-radius:6px'>{p}</span>"
            )
        else:
            page_links.append(f"<a class='links' href='?page={p}'>{p}</a>")

    if page_num < total_pages:
        page_links.append(f"<a class='links' href='?page={page_num+1}'>Next ›</a>")
        page_links.append(f"<a class='links' href='?page={total_pages}'>Last »</a>")

    nav = f"""
    <div class='pager' style="
        display:flex;
        align-items:center;
        gap:8px;
        flex-wrap:wrap;
        margin:10px 0">

        <span class="mini muted">
            Page {page_num} of {total_pages}
        </span>

        {"".join(page_links)}

        <form method="get"
              style="display:inline-flex;align-items:center;gap:6px;margin-left:10px">

            <span class="mini muted">Go to page</span>

            <input type="number"
                   name="page"
                   min="1"
                   max="{total_pages}"
                   value="{page_num}"
                   style="width:70px;padding:4px;border-radius:6px;border:1px solid #ccc">

            <button class="btn mini">Go</button>

        </form>

    </div>
    """

    body = f"""
    {admin_nav()}

    <section class='card' id="materials">

        <h1>Unlock Tutor Uploads</h1>

        {nav}

        <div class="scroll-x">

        <table>

        <thead>
        <tr>
            <th>Subject</th>
            <th>Title</th>
            <th>Tutor</th>
            <th>Created</th>
            <th>Status</th>
            <th>Action</th>
        </tr>
        </thead>

        <tbody>
        {''.join(trs) or "<tr><td colspan='6'>No uploads.</td></tr>"}
        </tbody>

        </table>

        </div>

        {nav}

    </section>
    """

    return page("Unlock Uploads", body)

   

    
@app.post('/admin/materials/<int:mid>/unlock')
def admin_unlock_material(mid):

    r = require_admin()
    if r:
        return r

    page_num = request.form.get("page", 1)

    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        "UPDATE materials SET admin_unlocked=1 WHERE id=?",
        (mid,)
    )

    conn.commit()
    conn.close()

    return redirect(url_for('admin_materials', page=page_num))



@app.post('/admin/materials/<int:mid>/relock')
def admin_relock_material(mid):

    r = require_admin()
    if r:
        return r

    page_num = request.form.get("page", 1)

    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        "UPDATE materials SET admin_unlocked=0 WHERE id=?",
        (mid,)
    )

    conn.commit()
    conn.close()

    return redirect(url_for('admin_materials', page=page_num))
    

@app.post('/admin/materials/<int:mid>/delete')
def admin_delete_material(mid):

    r = require_admin()
    if r:
        return r

    page_num = request.form.get("page", 1)

    conn = get_db()
    cur = conn.cursor()

    # delete submissions first
    cur.execute("""
        DELETE FROM submissions
        WHERE material_id=?
    """, (mid,))

    # delete enrollment files linked via submissions if applicable
    cur.execute("""
        DELETE FROM enrollment_files
        WHERE enrollment_id IN (
            SELECT enrollment_id
            FROM submissions
            WHERE material_id=?
        )
    """, (mid,))

    # delete the material itself
    cur.execute("""
        DELETE FROM materials
        WHERE id=?
    """, (mid,))

    conn.commit()
    conn.close()

    return redirect(url_for('admin_materials', page=page_num))




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
    SELECT se.*, 
           s.name AS subject_name, 
           s.grade,
           t.full_name AS tutor_name, 
           t.phone AS tutor_phone
    FROM sessions se
    JOIN subjects s ON s.id = se.subject_id
    JOIN tutors t ON t.id = se.tutor_id

    ORDER BY
        CAST(REPLACE(s.grade, 'G', '') AS INTEGER) ASC,
        s.name ASC,
        se.day_of_week ASC,
        se.start_time ASC
    """)

    sessions_rows = cur.fetchall()

    conn.close()

    options = ''.join([
        f"<option value='{s['id']}'>{s['grade']} — {s['name']}</option>"
        for s in subjects
    ])

    dow_opts = ''.join([
        f"<option value='{i}'>{d}</option>"
        for i, d in enumerate(DOW)
    ])

    rows = ''.join([
        f"""
        <tr>
            <td>{grade_label(r['grade'])} — {r['subject_name']}</td>

            <td>
                {r['tutor_name']}<br>
                <span class='mini muted'>{r['tutor_phone']}</span>
            </td>

            <td>
                {DOW[r['day_of_week']]}<br>
                <span class='mini muted'>
                    {r['start_time']} - {r['end_time']}
                </span>
            </td>

            <!-- NEW TEST LINK COLUMN -->
            <td>
                {
                    f"<a class='btn mini success' target='_blank' href='{r['meet_link']}'>Open</a>"
                    if r['meet_link']
                    else "<span class='muted mini'>No link</span>"
                }
            </td>

            <td>
                {
                    '<span class="chip active">Shown</span>'
                    if r['active'] == 1
                    else '<span class="chip lapsed">Hidden</span>'
                }
            </td>

            <td style="white-space:nowrap">
                <a class='links' href='{url_for('session_qr', id=r['id'])}'>QR</a>

                ·

                <form method='post'
                      action='{url_for('admin_session_toggle', sid=r['id'])}'
                      style='display:inline'>

                    <button class='btn mini secondary'>
                        {'Hide' if r['active'] == 1 else 'Show'}
                    </button>

                </form>

                ·

                <form method='post'
                      action='{url_for('admin_session_delete', sid=r['id'])}'
                      style='display:inline'
                      onsubmit='return confirm("Delete this session?")'>

                    <button class='btn danger mini'>Delete</button>

                </form>

            </td>

        </tr>
        """
        for r in sessions_rows
    ]) or "<tr><td colspan='6'><div class='empty'>No sessions.</div></td></tr>"


    body = f"""
    {admin_nav()}

    <section class='card'>

        <h1>Sessions</h1>

        <form class='grid'
              method='post'
              action='{url_for('admin_sessions_post')}'>

            <div style='display:grid;
                        grid-template-columns:1fr 1fr 110px 110px 1fr auto;
                        gap:10px'>

                <select name='subject_id'>{options}</select>

                <input name='tutor_name'
                       placeholder='Tutor name'
                       required />

                <select name='dow'>{dow_opts}</select>

                <input name='start'
                       placeholder='Start HH:MM'
                       required />

                <input name='end'
                       placeholder='End HH:MM'
                       required />

                <input name='tutor_phone'
                       placeholder='Tutor phone'
                       required />

                <input name='meet'
                       placeholder='Meet link (optional)' />

                <button class='btn'>Add</button>

            </div>

        </form>


        <div class="scroll-x">

            <table>

                <thead>

                    <tr>

                        <th>Subject</th>

                        <th>Tutor</th>

                        <th>When</th>

                        <!-- NEW COLUMN -->
                        <th>Test link</th>

                        <th>Visibility</th>

                        <th>Actions</th>

                    </tr>

                </thead>

                <tbody>

                    {rows}

                </tbody>

            </table>

        </div>

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
    # First try find tutor by PHONE (most reliable unique field)
    cur.execute("SELECT id FROM tutors WHERE phone=?", (tutor_phone,))
    row = cur.fetchone()

    if row:
        tutor_id = row['id']

        cur.execute("""
            UPDATE tutors
            SET full_name=?
            WHERE id=?
        """, (tutor_name, tutor_id))


    else:
        # If not found, create new tutor safely
        pins = set()

        cur.execute("SELECT pin FROM students WHERE pin IS NOT NULL")
        pins |= {r['pin'] for r in cur.fetchall()}

        cur.execute("SELECT pin FROM tutors WHERE pin IS NOT NULL")
        pins |= {r['pin'] for r in cur.fetchall()}

        pin = gen_pin(pins)
        now = now_utc_iso()

        cur.execute("""
            INSERT INTO tutors(full_name, phone, pin, created_at)
            VALUES (?, ?, ?, ?)
        """, (tutor_name, tutor_phone, pin, now))

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

    import json
    import re

    page_num = int(request.args.get("page", 1))
    q = request.args.get("q", "").strip()

    limit = 50
    offset = (page_num - 1) * limit

    conn = get_db()
    cur = conn.cursor()

    # COUNT
    if q:
        cur.execute("""
            SELECT COUNT(*) AS c
            FROM messages
            WHERE kind LIKE ? OR payload LIKE ?
        """, (f"%{q}%", f"%{q}%"))
    else:
        cur.execute("SELECT COUNT(*) AS c FROM messages")

    total = cur.fetchone()['c']
    total_pages = max(1, (total + limit - 1) // limit)

    # FETCH
    if q:
        cur.execute("""
            SELECT id, kind, payload, created_at, resolved
            FROM messages
            WHERE kind LIKE ? OR payload LIKE ?
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
        """, (f"%{q}%", f"%{q}%", limit, offset))
    else:
        cur.execute("""
            SELECT id, kind, payload, created_at, resolved
            FROM messages
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
        """, (limit, offset))

    rows = cur.fetchall()

    trs = []

    for m in rows:

        when = format_datime(m['created_at'])

        status = (
            "<span class='chip active'>Open</span>"
            if m['resolved'] == 0
            else "<span class='chip'>Resolved</span>"
        )

        action = "" if m['resolved'] else f"""
            <form method='post'
                  action='{url_for('admin_message_resolve', mid=m['id'])}'
                  style='display:inline'>
                <input type="hidden" name="page" value="{page_num}">
                <input type="hidden" name="q" value="{q}">
                <button class='btn success mini'>Mark resolved</button>
            </form>
        """

        payload = m['payload']
        phone = None

        # --- TRY JSON ---
        try:
            data = json.loads(payload)
            phone = data.get("phone") or data.get("phone_whatsapp")
        except:
            pass

        # --- TRY TEXT FORMAT phone=082... ---
        if not phone:
            match = re.search(r'phone\s*=\s*(\d+)', payload)
            if match:
                phone = match.group(1)

        # --- LOOKUP STUDENT ---
        if phone:

            cur.execute("""
                SELECT full_name, phone_whatsapp, pin
                FROM students
                WHERE phone_whatsapp=?
            """, (phone,))

            student = cur.fetchone()

            if student:

                pin = student['pin'] if student['pin'] else "Not enrolled"

                payload_display = f"""
                <div><b>Name:</b> {student['full_name']}</div>
                <div><b>Phone:</b> {student['phone_whatsapp']}</div>
                <div><b>PIN:</b> {pin}</div>
                """

            else:

                payload_display = f"""
                <div><b>Phone:</b> {phone}</div>
                <div><b>PIN:</b> Not enrolled</div>
                """

        else:

            payload_display = payload

        trs.append(f"""
        <tr>
            <td>{m['kind']}</td>
            <td>{payload_display}</td>
            <td class='mini muted'>{when}</td>
            <td>{status}</td>
            <td>{action}</td>
        </tr>
        """)

    conn.close()

    # Build smart page range
    start = max(1, page_num - 3)
    end = min(total_pages, page_num + 3)

    page_links = []

    # First + Prev
    if page_num > 1:
        page_links.append(f"<a class='links' href='?page=1&q={q}'>« First</a>")
        page_links.append(f"<a class='links' href='?page={page_num-1}&q={q}'>‹ Prev</a>")

    # Page numbers
    for p in range(start, end + 1):

        if p == page_num:
            page_links.append(
                f"<span class='current' "
                f"style='padding:4px 8px;background:#0f172a;color:white;border-radius:6px'>"
                f"{p}</span>"
            )
        else:
            page_links.append(
                f"<a class='links' href='?page={p}&q={q}'>{p}</a>"
            )

    # Next + Last
    if page_num < total_pages:
        page_links.append(f"<a class='links' href='?page={page_num+1}&q={q}'>Next ›</a>")
        page_links.append(f"<a class='links' href='?page={total_pages}&q={q}'>Last »</a>")


    nav = f"""
    <div class='pager'
         style="display:flex;
                align-items:center;
                gap:8px;
                flex-wrap:wrap;
                margin:10px 0">

        <span class="mini muted">
            Page {page_num} of {total_pages}
        </span>

        {"".join(page_links)}

        <form method="get"
              style="display:inline-flex;
                     align-items:center;
                     gap:6px;
                     margin-left:10px">

            <span class="mini muted">Go to page</span>

            <input type="number"
                   name="page"
                   min="1"
                   max="{total_pages}"
                   value="{page_num}"
                   style="width:70px;
                          padding:4px;
                          border-radius:6px;
                          border:1px solid #ccc">

            <input type="hidden" name="q" value="{q}">

            <button class="btn mini">Go</button>

        </form>

    </div>
    """


    body = f"""
    {admin_nav()}

    <section class='card'>

        <h1>Admin Inbox</h1>
        <div class="toolbar">

            <form method="post"
                  action="{url_for('admin_messages_resolve_all')}"
                  onsubmit="return confirm('Resolve ALL tickets?')">

                <input type="hidden" name="q" value="{q}">

                <button class="btn success mini">
                    Resolve All
                </button>

            </form>

        </div>


        <form method="get" class="toolbar">
            <input name="q" class="pill" placeholder="Search..." value="{q}">
            <button class="btn mini">Search</button>
        </form>

        {nav}

        <div class="scroll-x">
            <table>
                <thead>
                    <tr>
                        <th>Type</th>
                        <th>Student Info</th>
                        <th>When</th>
                        <th>Status</th>
                        <th>Action</th>
                    </tr>
                </thead>
                <tbody>
                    {''.join(trs)}
                </tbody>
            </table>
        </div>

        {nav}

    </section>
    """

    return page("Messages", body)



@app.post('/admin/messages/<int:mid>/resolve')
def admin_message_resolve(mid:int):
    r = require_admin()
    if r:
        return r

    page_num = request.form.get("page", 1)
    q = request.form.get("q", "")

    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE messages SET resolved=1 WHERE id=?", (mid,))
    conn.commit()
    conn.close()

    return redirect(url_for('admin_messages', page=page_num, q=q))
    

@app.post('/admin/messages/resolve-all')
def admin_messages_resolve_all():

    r = require_admin()
    if r:
        return r

    q = request.form.get("q", "").strip()

    conn = get_db()
    cur = conn.cursor()

    if q:
        cur.execute("""
            UPDATE messages
            SET resolved = 1
            WHERE resolved = 0
            AND (kind LIKE ? OR payload LIKE ?)
        """, (f"%{q}%", f"%{q}%"))
    else:
        cur.execute("""
            UPDATE messages
            SET resolved = 1
            WHERE resolved = 0
        """)

    conn.commit()
    conn.close()

    return redirect(url_for("admin_messages", q=q))




# --- Admin: Direct Messages (student/tutor DMs) ---

@app.get('/admin/direct-messages')
def admin_direct_messages():

    r = require_admin()
    if r:
        return r

    selected = request.args.get("chat")
    q = request.args.get("q", "").strip()

    conn = get_db()
    cur = conn.cursor()

    # =========================
    # SEARCH tutors
    # =========================

    if q:
        cur.execute("""
            SELECT DISTINCT
                t.id,
                t.full_name,
                s.grade
            FROM tutors t
            LEFT JOIN tutor_subjects ts ON ts.tutor_id = t.id
            LEFT JOIN subjects s ON s.id = ts.subject_id
            WHERE t.full_name LIKE ?
            ORDER BY t.full_name
        """, (f"%{q}%",))
    else:
        cur.execute("""
            SELECT DISTINCT
                t.id,
                t.full_name,
                s.grade
            FROM tutors t
            LEFT JOIN tutor_subjects ts ON ts.tutor_id = t.id
            LEFT JOIN subjects s ON s.id = ts.subject_id
            ORDER BY t.full_name

        """)

    tutors = cur.fetchall()

    # =========================
    # SEARCH students
    # =========================

    if q:
        cur.execute("""
            SELECT
                s.id,
                s.full_name,
                s.grade,

                (
                    SELECT body
                    FROM direct_messages dm
                    WHERE
                    (
                        (dm.to_role='student' AND dm.to_id=s.id)
                        OR
                        (dm.from_role='student' AND dm.from_id=s.id)
                    )
                    ORDER BY dm.created_at DESC
                    LIMIT 1
                ) AS last_message,

                (
                    SELECT created_at
                    FROM direct_messages dm
                    WHERE
                    (
                        (dm.to_role='student' AND dm.to_id=s.id)
                        OR
                        (dm.from_role='student' AND dm.from_id=s.id)
                    )
                    ORDER BY dm.created_at DESC
                    LIMIT 1
                ) AS last_time,

                (
                    SELECT COUNT(*)
                    FROM direct_messages dm
                    WHERE dm.to_role='admin'
                    AND dm.from_role='student'
                    AND dm.from_id=s.id
                    AND dm.is_read=0
                ) AS unread

            FROM students s

            WHERE s.full_name LIKE ?

            ORDER BY last_time DESC NULLS LAST, s.full_name
        """, (f"%{q}%",))

    else:
        cur.execute("""
            SELECT
                s.id,
                s.full_name,
                s.grade,

                (
                    SELECT body
                    FROM direct_messages dm
                        WHERE
                    (
                        (dm.to_role='student' AND dm.to_id=s.id)
                        OR
                        (dm.from_role='student' AND dm.from_id=s.id)
                    )
                    ORDER BY dm.created_at DESC
                    LIMIT 1
                ) AS last_message,

                (
                    SELECT created_at
                    FROM direct_messages dm
                        WHERE
                    (
                        (dm.to_role='student' AND dm.to_id=s.id)
                        OR
                        (dm.from_role='student' AND dm.from_id=s.id)
                    )
                    ORDER BY dm.created_at DESC
                    LIMIT 1
                ) AS last_time,

                (
                    SELECT COUNT(*)
                    FROM direct_messages dm
                    WHERE dm.to_role='admin'
                    AND dm.from_role='student'
                    AND dm.from_id=s.id
                    AND dm.is_read=0
                ) AS unread

            FROM students s

            ORDER BY last_time DESC NULLS LAST, s.full_name
        """)

    students = cur.fetchall()

    # =========================
    # SIDEBAR LIST
    # =========================

    chat_list = """

    <div class='chat-section'>Broadcast</div>

    <a href='?chat=ALL_TUTORS' class='chat-user'>
        All Tutors
    </a>

    <a href='?chat=ALL_STUDENTS' class='chat-user'>
        All Students
    </a>

    """

    # Tutors grouped by grade
    tutors_by_grade = {}

    for t in tutors:

        grade = t['grade'] or "OTHER"

        if grade not in tutors_by_grade:
            tutors_by_grade[grade] = {}

        tutors_by_grade[grade][t['id']] = t['full_name']

    for grade in sorted(tutors_by_grade.keys()):

        chat_list += f"""
        <div class="chat-section">
            Tutors — {grade_label(grade)}
        </div>
        """

        for tid, name in sorted(tutors_by_grade[grade].items(), key=lambda x: x[1]):

            active = "active" if selected == f"tutor|{tid}" else ""

            chat_list += f"""
            <a href="?chat=tutor|{tid}"
               class="chat-user tutor {active}">

                <div class="chat-name">
                    {name}
                </div>

                <div class="chat-role">
                    Tutor
                </div>

            </a>
            """

    # Students grouped by grade
    
    students_by_grade = {}

    for s in students:
        grade = s['grade'] or "OTHER"

        if grade not in students_by_grade:
            students_by_grade[grade] = {}

        students_by_grade[grade][s['id']] = s


    chat_list += """
    <div class="chat-section">
    Students
    </div>
    """

    for s in students:

        sid = s['id']
        name = s['full_name']
        unread = s['unread']
        last_msg = s['last_message'] or ""
        last_msg = last_msg[:40] + ("..." if len(last_msg) > 40 else "")

        badge = f"<span class='badge'>{unread}</span>" if unread else ""

        active = "active" if selected == f"student|{sid}" else ""

        time = ""
        if s['last_time']:
            time = s['last_time'][11:16]

        chat_list += f"""
        <a href="?chat=student|{sid}"
           class="chat-user student {active}">

            <div style="display:flex;justify-content:space-between">

                <div class="chat-name">
                    {name}
                </div>

                <div style="text-align:right">
                    <div class="mini">{time}</div>
                    {badge}
                </div>

            </div>

            <div class="chat-role">
                {last_msg}
            </div>

        </a>
        """

    # =========================
    # LOAD CHAT
    # =========================

    chat_messages = ""
    message_input = ""

    if selected and "|" in selected:

        role, rid = selected.split("|")
        rid = int(rid)

        cur.execute("""
        SELECT dm.*,

            CASE dm.from_role
                WHEN 'admin' THEN 'You'
                WHEN 'tutor' THEN (SELECT full_name FROM tutors WHERE id=dm.from_id)
                ELSE (SELECT full_name FROM students WHERE id=dm.from_id)
            END AS sender

        FROM direct_messages dm

        WHERE
        (
            dm.from_role='admin'
            AND dm.to_role=?
            AND dm.to_id=?
        )
        OR
        (
            dm.to_role='admin'
            AND dm.from_role=?
            AND dm.from_id=?
        )

        ORDER BY dm.created_at ASC
        """, (role, rid, role, rid))

        msgs = cur.fetchall()

        for m in msgs:

            side = "me" if m['from_role']=="admin" else "them"

            time = m['created_at'][11:16]

            chat_messages += f"""
            <div class="bubble {side}">

                {m['body']}

                <div class="time">{time}</div>

            </div>
            """

        message_input = f"""
        <form method="post"
              action="{url_for('admin_send_dm')}">

            <input type="hidden"
                   name="target"
                   value="{selected}">

            <textarea name="body"
                      required></textarea>

            <button class="btn success">
                Send
            </button>

        </form>
        """
        
        cur.execute("""
        UPDATE direct_messages
        SET is_read=1
        WHERE to_role='admin'
        AND from_role=?
        AND from_id=?
        """, (role, rid))
        conn.commit()

    # Broadcast message form
    broadcast_form = """
    <div class="card">

        <h3>Broadcast Message</h3>

        <form method="post"
              action="/admin/direct-messages/send">

            <select name="target">

                <option value="ALL_TUTORS">All Tutors</option>

                <option value="ALL_STUDENTS">All Students</option>

                <optgroup label="Tutors by grade">
                    <option value="GRADE_TUTORS|G8">Grade 8 tutors</option>
                    <option value="GRADE_TUTORS|G9">Grade 9 tutors</option>
                    <option value="GRADE_TUTORS|G10">Grade 10 tutors</option>
                    <option value="GRADE_TUTORS|G11">Grade 11 tutors</option>
                    <option value="GRADE_TUTORS|G12">Grade 12 tutors</option>
                </optgroup>

                <optgroup label="Students by grade">
                    <option value="GRADE_STUDENTS|G8">Grade 8 students</option>
                    <option value="GRADE_STUDENTS|G9">Grade 9 students</option>
                    <option value="GRADE_STUDENTS|G10">Grade 10 students</option>
                    <option value="GRADE_STUDENTS|G11">Grade 11 students</option>
                    <option value="GRADE_STUDENTS|G12">Grade 12 students</option>
                </optgroup>

            </select>

            <textarea name="body" required></textarea>

            <button class="btn success">
                Send Broadcast
            </button>

        </form>
        
        <form method="post" action="/admin/broadcast-sms">

            <h3>Broadcast SMS</h3>

            <textarea name="body" required></textarea>

            <button class="btn success">
                Send SMS to All Students & Guardians
            </button>

        </form>
        
        <form method="get" action="/admin/process-sms">
            <button class="btn success">Send Pending SMS Now</button>
        </form>

    </div>
    """

    conn.close()

    body = f"""
    {admin_nav()}

    <section class="grid">

        {broadcast_form}

        <div class="card">

            <form>

                <input name="q"
                       placeholder="Search tutors or students"
                       value="{q}">

            </form>
            
            <form method="post" action="/admin/remind-tutors">
            <button class="btn success">Send Tutor Reminders</button>
            </form>

            <div class="chat-layout">

                <div class="chat-list">
                    {chat_list}
                </div>

                <div class="chat-window">

                    <div class="chat-messages">
                        {chat_messages or "Select conversation"}
                    </div>

                    <div class="chat-input">
                        {message_input}
                    </div>

                </div>

            </div>

        </div>

    </section>
    """

    return page("Direct Messages", body)
    


@app.post('/admin/direct-messages/send')
def admin_send_dm():

    r = require_admin()
    if r:
        return r

    target = request.form.get('target')
    body = request.form.get('body').strip()

    conn = get_db()
    cur = conn.cursor()

    now = now_utc_iso()

    # ALL tutors
    if target == "ALL_TUTORS":

        cur.execute("SELECT id FROM tutors")
        rows = cur.fetchall()

        cur.executemany("""
        INSERT INTO direct_messages
        VALUES(NULL,'admin',0,'tutor',?,NULL,?, ?,0)
        """, [(r['id'], body, now) for r in rows])

    # ALL students
    elif target == "ALL_STUDENTS":

        cur.execute("SELECT id FROM students")
        rows = cur.fetchall()

        cur.executemany("""
        INSERT INTO direct_messages
        VALUES(NULL,'admin',0,'student',?,NULL,?, ?,0)
        """, [(r['id'], body, now) for r in rows])

    # tutors by grade
    elif target.startswith("GRADE_TUTORS|"):

        grade = target.split("|")[1]

        cur.execute("""
        SELECT DISTINCT t.id
        FROM tutors t
        JOIN tutor_subjects ts ON ts.tutor_id = t.id
        JOIN subjects s ON s.id = ts.subject_id
        WHERE s.grade=?

        """, (grade,))

        rows = cur.fetchall()

        cur.executemany("""
        INSERT INTO direct_messages
        VALUES(NULL,'admin',0,'tutor',?,NULL,?, ?,0)
        """, [(r['id'], body, now) for r in rows])

    # students by grade
    elif target.startswith("GRADE_STUDENTS|"):

        grade = target.split("|")[1]

        cur.execute("""
        SELECT id FROM students
        WHERE grade=?
        """, (grade,))

        rows = cur.fetchall()

        cur.executemany("""
        INSERT INTO direct_messages
        VALUES(NULL,'admin',0,'student',?,NULL,?, ?,0)
        """, [(r['id'], body, now) for r in rows])

    # individual
    else:

        role, rid = target.split("|")

        cur.execute("""
        INSERT INTO direct_messages
        VALUES(NULL,'admin',0,?, ?,NULL,?, ?,0)
        """, (role, rid, body, now))

    conn.commit()
    conn.close()

    return redirect(url_for('admin_direct_messages'))

    
@app.post('/admin/message-tutor')
def admin_message_tutor():

    r = require_admin()
    if r:
        return r

    tutor_id = int(request.form.get("tutor_id"))
    body = request.form.get("body","").strip()

    if not body:
        return page("Error", card_msg("Empty message."))

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO direct_messages
        (from_role, from_id, to_role, to_id, subject_id, body, created_at)
        VALUES ('admin', 0, 'tutor', ?, NULL, ?, ?)
    """, (tutor_id, body, now_utc_iso()))

    conn.commit()
    conn.close()

    return redirect(url_for("admin_home"))


def queue_sms_bulk(phones, body, recipient_type="student"):

    conn = get_db()
    cur = conn.cursor()

    now = now_utc_iso()

    unique = set(phones)

    for phone in unique:

        if not phone:
            continue

        cur.execute("""
            INSERT INTO sms_queue(phone, body, recipient_type, created_at, status)
            SELECT ?, ?, ?, ?, 'PENDING'
            WHERE NOT EXISTS (
                SELECT 1 FROM sms_queue
                WHERE phone=? AND body=? AND date(created_at)=date(?)
            )
        """, (phone, body, recipient_type, now, phone, body, now))

    conn.commit()
    conn.close()
    
    
def sms_worker():
    while True:
        try:
            process_sms_queue(100)
        except Exception:
            pass
        time.sleep(15)


if not globals().get("_sms_worker_started"):
    threading.Thread(target=sms_worker, daemon=True).start()
    _sms_worker_started = True
    
def process_sms_queue(batch_size=100):

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT id, phone, body
        FROM sms_queue
        WHERE status IN ('PENDING','FAILED')
        ORDER BY id
        LIMIT ?
    """, (batch_size,))

    rows = cur.fetchall()

    for r in rows:

        try:

            send_sms_notification(r["phone"], r["body"])

            cur.execute("""
                UPDATE sms_queue
                SET status='SENT', sent_at=?
                WHERE id=?
            """, (now_utc_iso(), r["id"]))

        except Exception:

            cur.execute("""
                UPDATE sms_queue
                SET status='FAILED'
                WHERE id=?
            """, (r["id"],))

    conn.commit()
    conn.close()

    return len(rows)

@app.post('/admin/broadcast-sms')
def admin_broadcast_sms():

    r = require_admin()
    if r:
        return r

    body = request.form.get("body","").strip()

    if not body:
        return page("Error", card_msg("Message required"))

    conn = get_db()
    cur = conn.cursor()

    # students
    cur.execute("SELECT phone_whatsapp FROM students")
    student_phones = [r["phone_whatsapp"] for r in cur.fetchall()]

    # guardians
    cur.execute("SELECT guardian_phone FROM students WHERE guardian_phone IS NOT NULL")
    guardian_phones = [r["guardian_phone"] for r in cur.fetchall()]

    conn.close()

    queue_sms_bulk(student_phones, body, "student")
    queue_sms_bulk(guardian_phones, body, "guardian")

    return page("Success", card_msg("SMS queued successfully"))
    
    
def remind_tutors_about_sessions():

    conn = get_db()
    cur = conn.cursor()

    today = datetime.datetime.now(ZoneInfo("Africa/Johannesburg")).weekday()

    cur.execute("""
        SELECT DISTINCT t.phone, t.full_name, s.start_time
        FROM sessions s
        JOIN tutors t ON t.id = s.tutor_id
        WHERE s.active=1
        AND s.day_of_week=?
    """, (today,))
    rows = cur.fetchall()

    for r in rows:

        message = f"EBTA Reminder: Hi {r['full_name']}, you have a session today at {r['start_time']}. Please be ready."

        queue_sms_bulk([r["phone"]], message, "tutor")

    conn.close()
    
    
@app.post('/admin/remind-tutors')
def admin_remind_tutors():

    r = require_admin()
    if r:
        return r

    remind_tutors_about_sessions()

    return page("Success", card_msg("Tutor reminders queued"))
    
@app.get('/admin/process-sms')
def admin_process_sms():

    r = require_admin()
    if r:
        return r

    count = process_sms_queue()

    return page("SMS Processed", card_msg(f"{count} messages sent"))
    
    
    
@app.get('/admin/sms-dashboard')
def admin_sms_dashboard():

    r = require_admin()
    if r:
        return r

    conn = get_db()
    cur = conn.cursor()

    # Summary stats
    cur.execute("""
        SELECT status, COUNT(*) AS count
        FROM sms_queue
        GROUP BY status
    """)

    stats = {r["status"]: r["count"] for r in cur.fetchall()}

    pending = stats.get("PENDING", 0)
    sent = stats.get("SENT", 0)
    failed = stats.get("FAILED", 0)

    # Recent messages
    cur.execute("""
        SELECT phone, body, recipient_type, status, created_at, sent_at
        FROM sms_queue
        ORDER BY created_at DESC
        LIMIT 50
    """)

    rows = cur.fetchall()

    conn.close()

    table_rows = ""

    for r in rows:

        created = r["created_at"][:16].replace("T"," ")
        sent_time = r["sent_at"][:16].replace("T"," ") if r["sent_at"] else "—"

        table_rows += f"""
        <tr>
            <td>{r['phone']}</td>
            <td>{r['recipient_type']}</td>
            <td>{r['status']}</td>
            <td>{created}</td>
            <td>{sent_time}</td>
            <td style="max-width:300px">{r['body']}</td>
        </tr>
        """

    body = f"""
    {admin_nav()}

    <section class="grid">

        <div class="card">
            <h1>SMS Dashboard</h1>

            <div class="stats-mini">

                <div class="s">
                    <div class="k">{pending}</div>
                    <div class="t">Pending</div>
                </div>

                <div class="s">
                    <div class="k">{sent}</div>
                    <div class="t">Sent</div>
                </div>

                <div class="s">
                    <div class="k">{failed}</div>
                    <div class="t">Failed</div>
                </div>

            </div>

        </div>


        <div class="card">

            <h2>Recent SMS</h2>

            <div class="scroll-x">

            <table>

                <thead>
                    <tr>
                        <th>Phone</th>
                        <th>Type</th>
                        <th>Status</th>
                        <th>Created</th>
                        <th>Sent</th>
                        <th>Message</th>
                    </tr>
                </thead>

                <tbody>
                    {table_rows or "<tr><td colspan='6'>No messages</td></tr>"}
                </tbody>

            </table>

            </div>

        </div>

    </section>
    """

    return page("SMS Dashboard", body)


# --- Admin: Analytics dashboard ---

@app.get('/admin/analytics')
def admin_analytics():
    r = require_admin()
    if r: return r

    import json

    month = get_admin_active_month()
    conn = get_db()
    cur = conn.cursor()

    # ===== KPIs =====
    cur.execute("SELECT COUNT(*) AS c FROM enrollments WHERE month=?", (month,))
    total = cur.fetchone()['c'] or 0

    def count_status(s):
        cur.execute("SELECT COUNT(*) AS c FROM enrollments WHERE month=? AND status=?", (month,s))
        return cur.fetchone()['c'] or 0

    pending = count_status('PENDING')
    active = count_status('ACTIVE')
    lapsed = count_status('LAPSED')

    # ===== TRUE REVENUE (distributed) =====
    cur.execute("""
        SELECT ROUND(SUM(
            e.amount_paid * 1.0 / (
                SELECT COUNT(*)
                FROM enrollments e2
                WHERE e2.student_id = e.student_id
                  AND e2.month = e.month
                  AND e2.status = 'ACTIVE'
            )
        ), 2) AS r
        FROM enrollments e
        WHERE e.month=? AND e.status='ACTIVE'
    """, (month,))
    revenue = cur.fetchone()['r'] or 0

    # ===== New vs Returning =====
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

    # ===== Revenue trend (per day, corrected) =====
    cur.execute("""
        SELECT substr(e.created_at,1,10) AS day,
               ROUND(SUM(
                   e.amount_paid * 1.0 / (
                       SELECT COUNT(*)
                       FROM enrollments e2
                       WHERE e2.student_id = e.student_id
                         AND e2.month = e.month
                         AND e2.status = 'ACTIVE'
                   )
               ), 2) AS r
        FROM enrollments e
        WHERE e.month=? AND e.status='ACTIVE'
        GROUP BY day ORDER BY day
    """, (month,))
    rev_rows = cur.fetchall()
    rev_labels = [r['day'] for r in rev_rows]
    rev_data = [r['r'] for r in rev_rows]

    # ===== Attendance trend =====
    cur.execute("""
        SELECT a.date, COUNT(*) AS c
        FROM attendance a
        WHERE strftime('%Y-%m', a.date)=?
        GROUP BY a.date ORDER BY a.date
    """, (month,))
    att_rows = cur.fetchall()
    att_labels = [r['date'] for r in att_rows]
    att_data = [r['c'] for r in att_rows]

    # ===== Revenue per subject (corrected) =====
    cur.execute("""
        SELECT s.name || ' (' || s.grade || ')' AS label,
               ROUND(SUM(
                   e.amount_paid * 1.0 / (
                       SELECT COUNT(*)
                       FROM enrollments e2
                       WHERE e2.student_id = e.student_id
                         AND e2.month = e.month
                         AND e2.status = 'ACTIVE'
                   )
               ), 2) AS r
        FROM enrollments e
        JOIN subjects s ON s.id=e.subject_id
        WHERE e.month=? AND e.status='ACTIVE'
        GROUP BY s.id
        ORDER BY r DESC
    """, (month,))
    rev_sub_rows = cur.fetchall()
    rev_sub_labels = [r['label'] for r in rev_sub_rows]
    rev_sub_data = [r['r'] for r in rev_sub_rows]

    # ===== Top rated tutors =====
    cur.execute("""
        SELECT t.full_name, ROUND(AVG(l.rating),2) AS avg_rating
        FROM lesson_ratings l
        JOIN tutor_subjects ts ON ts.subject_id=l.subject_id
        JOIN tutors t ON t.id=ts.tutor_id
        WHERE l.month=?
        GROUP BY t.id
        ORDER BY avg_rating DESC
    """, (month,))
    tutor_rows = cur.fetchall()
    tutor_labels = [r['full_name'] for r in tutor_rows]
    tutor_data = [r['avg_rating'] for r in tutor_rows]

    # ===== Subject performance table =====
    cur.execute("SELECT id,name,grade FROM subjects ORDER BY grade,name")
    subs = cur.fetchall()

    rows=[]
    for s in subs:
        cur.execute("""
            SELECT COUNT(*) AS c
            FROM enrollments
            WHERE subject_id=? AND month=? AND status='ACTIVE'
        """, (s['id'], month))
        active_students = cur.fetchone()['c'] or 0

        cur.execute("""
            SELECT COUNT(*) AS c
            FROM attendance a
            JOIN sessions se ON se.id=a.session_id
            WHERE se.subject_id=? AND strftime('%Y-%m', a.date)=?
        """, (s['id'], month))
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

    <section class='stats big'>
        {stat('Revenue', f'R{revenue}')}
        {stat('Enrollments', total)}
        {stat('Active', active)}
        {stat('New students', new_students)}
        {stat('Returning', returning)}
        {stat('Lapsed', lapsed)}
        
    </section>

    <section class='grid'>
        <div class='card'>
            <h2>Revenue Trend — {month}</h2>
            <p class="mini muted">Daily revenue for the selected month</p>
            <canvas id="revChart"></canvas>
        </div>

        <div class='card'><h2>Students Mix</h2><canvas id="studentChart"></canvas></div>
        <div class='card'><h2>Enrollment Status</h2><canvas id="statusChart"></canvas></div>
        <div class='card'><h2>Attendance Trend</h2><canvas id="attChart"></canvas></div>
    </section>

    <section class='grid'>
        <div class='card'>
            <h2>Revenue per Subject</h2>
            <select id="revSubFilter" onchange="updateRevSubChart()">
                <option value="3">Top 3</option>
                <option value="10">Top 10</option>
                <option value="all">All</option>
            </select>
            <canvas id="revSubChart"></canvas>
        </div>

        <div class='card'>
            <h2>Top Rated Tutors</h2>
            <select id="tutorFilter" onchange="updateTutorChart()">
                <option value="3">Top 3</option>
                <option value="10">Top 10</option>
                <option value="all">All</option>
            </select>
            <canvas id="tutorChart"></canvas>
        </div>
    </section>

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
    const revLabels = {json.dumps(rev_labels)};
    const revData = {json.dumps(rev_data)};
    const attLabels = {json.dumps(att_labels)};
    const attData = {json.dumps(att_data)};
    const revSubLabels = {json.dumps(rev_sub_labels)};
    const revSubData = {json.dumps(rev_sub_data)};
    const tutorLabels = {json.dumps(tutor_labels)};
    const tutorData = {json.dumps(tutor_data)};

    new Chart(document.getElementById("revChart"), {{
        type:'line',
        data:{{labels:revLabels,datasets:[{{label:'Revenue',data:revData}}]}}
    }});

    new Chart(document.getElementById("studentChart"), {{
        type:'pie',
        data:{{labels:['New','Returning'],datasets:[{{data:[{new_students},{returning}]}}]}}
    }});

    new Chart(document.getElementById("statusChart"), {{
        type:'bar',
        data:{{labels:['Pending','Active','Lapsed'],datasets:[{{data:[{pending},{active},{lapsed}]}}]}}
    }});

    new Chart(document.getElementById("attChart"), {{
        type:'line',
        data:{{labels:attLabels,datasets:[{{label:'Attendance',data:attData}}]}}
    }});

    const revSubCtx = document.getElementById("revSubChart").getContext("2d");
    let revSubChartObj = new Chart(revSubCtx, {{
        type:'bar',
        data:{{labels:revSubLabels,datasets:[{{label:'Revenue',data:revSubData}}]}}
    }});

    function updateRevSubChart(){{
        const mode = document.getElementById("revSubFilter").value;
        let l = revSubLabels, d = revSubData;
        if(mode !== "all"){{ l=l.slice(0,mode); d=d.slice(0,mode); }}
        revSubChartObj.data.labels = l;
        revSubChartObj.data.datasets[0].data = d;
        revSubChartObj.update();
    }}

    const tutorCtx = document.getElementById("tutorChart").getContext("2d");
    let tutorChartObj = new Chart(tutorCtx, {{
        type:'bar',
        data:{{labels:tutorLabels,datasets:[{{label:'Avg ★',data:tutorData}}]}}
    }});

    function updateTutorChart(){{
        const mode = document.getElementById("tutorFilter").value;
        let l = tutorLabels, d = tutorData;
        if(mode !== "all"){{ l=l.slice(0,mode); d=d.slice(0,mode); }}
        tutorChartObj.data.labels = l;
        tutorChartObj.data.datasets[0].data = d;
        tutorChartObj.update();
    }}
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
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
