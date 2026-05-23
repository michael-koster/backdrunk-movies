from flask import (
    Flask, request, render_template, send_from_directory,
    abort, redirect, url_for, flash, Response,
)
from flask_caching import Cache
from flask_paginate import Pagination, get_page_args
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user,
)
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv

import os
import re
import json
import time
import shutil
import secrets
import sqlite3
import functools
import subprocess
from urllib.parse import urlparse, quote

import requests


# ---------------------------------------------------------------------------
# Config & app setup
# ---------------------------------------------------------------------------

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

TMDB_API_KEY = (os.environ.get('TMDB_API_KEY') or '').strip()
SECRET_KEY = (os.environ.get('SECRET_KEY') or '').strip()
ADMIN_USERNAME = (os.environ.get('ADMIN_USERNAME') or '').strip()
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD') or ''

DBFILE = os.path.join(os.path.dirname(__file__), 'movies-v3.db')

app = Flask(__name__)
app.config['CACHE_TYPE'] = 'SimpleCache'
app.config['CACHE_DEFAULT_TIMEOUT'] = 86400
app.config['SECRET_KEY'] = SECRET_KEY or 'dev-only-insecure-key-change-me'
cache = Cache(app)

login_manager = LoginManager(app)
login_manager.login_view = 'login'


# ---------------------------------------------------------------------------
# DB helpers + migration
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DBFILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _ensure_column(conn, table, column, decl):
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_db():
    """Idempotent, additive migration. Never drops or modifies existing data."""
    conn = get_db()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT,
                email TEXT,
                is_admin INTEGER NOT NULL DEFAULT 0,
                invite_token TEXT UNIQUE,
                invited_at TEXT,
                invited_by INTEGER,
                accepted_at TEXT,
                last_login_at TEXT,
                xtream_url TEXT,
                xtream_username TEXT,
                xtream_password TEXT,
                created_at TEXT NOT NULL
            )
        """)
        for col, decl in [
            ('password_hash', 'TEXT'),
            ('email', 'TEXT'),
            ('is_admin', 'INTEGER NOT NULL DEFAULT 0'),
            ('invite_token', 'TEXT'),
            ('invited_at', 'TEXT'),
            ('invited_by', 'INTEGER'),
            ('accepted_at', 'TEXT'),
            ('last_login_at', 'TEXT'),
            ('xtream_url', 'TEXT'),
            ('xtream_username', 'TEXT'),
            ('xtream_password', 'TEXT'),
        ]:
            _ensure_column(conn, 'users', col, decl)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_movies (
                user_id INTEGER NOT NULL,
                movie_id INTEGER NOT NULL,
                in_watchlist INTEGER NOT NULL DEFAULT 0,
                watchlisted_at TEXT,
                seen INTEGER NOT NULL DEFAULT 0,
                seen_at TEXT,
                rating INTEGER,
                rated_at TEXT,
                PRIMARY KEY (user_id, movie_id)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_user_movies_user_watchlist
            ON user_movies(user_id, in_watchlist)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_user_movies_user_seen
            ON user_movies(user_id, seen)
        """)
        conn.commit()
    finally:
        conn.close()


def bootstrap_admin():
    """Create the initial admin from env vars when users table is empty."""
    if not ADMIN_USERNAME or not ADMIN_PASSWORD:
        return
    conn = get_db()
    try:
        existing = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if existing > 0:
            return
        now = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
        conn.execute(
            """INSERT INTO users
               (username, password_hash, is_admin, accepted_at, created_at)
               VALUES (?, ?, 1, ?, ?)""",
            (ADMIN_USERNAME, generate_password_hash(ADMIN_PASSWORD), now, now),
        )
        conn.commit()
        app.logger.warning(
            "Bootstrapped admin user %r from ADMIN_USERNAME/ADMIN_PASSWORD env.",
            ADMIN_USERNAME,
        )
    finally:
        conn.close()


init_db()
bootstrap_admin()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class User(UserMixin):
    def __init__(self, row):
        self.row = row
        self.id = row['id']
        self.username = row['username']
        self.email = row['email']
        self.is_admin = bool(row['is_admin'])
        self.xtream_url = row['xtream_url']
        self.xtream_username = row['xtream_username']
        self.xtream_password = row['xtream_password']

    @property
    def has_xtream(self):
        return bool(self.xtream_url and self.xtream_username and self.xtream_password)


def load_user_by_id(user_id):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    finally:
        conn.close()
    return User(row) if row else None


@login_manager.user_loader
def _user_loader(user_id):
    try:
        return load_user_by_id(int(user_id))
    except (TypeError, ValueError):
        return None


def admin_required(view):
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for('login', next=request.path))
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)
    return wrapper


def now_str():
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())


# ---------------------------------------------------------------------------
# Stream URL rewrite
# ---------------------------------------------------------------------------

def build_stream_url(user, stored_url):
    """Return a personalised stream URL or None if it can't be built."""
    if not user or not user.has_xtream or not stored_url:
        return None
    parsed = urlparse(stored_url)
    filename = os.path.basename(parsed.path or '')
    if not filename:
        return None
    base = user.xtream_url.rstrip('/')
    return (
        f"{base}/movie/"
        f"{quote(user.xtream_username, safe='')}/"
        f"{quote(user.xtream_password, safe='')}/"
        f"{filename}"
    )


# ---------------------------------------------------------------------------
# Live ffmpeg transcoding
# ---------------------------------------------------------------------------

FFMPEG_BIN = shutil.which('ffmpeg')
FFPROBE_BIN = shutil.which('ffprobe')
if not FFMPEG_BIN or not FFPROBE_BIN:
    # Logged at import time; /stream/<id> returns 503 if these are missing.
    print('WARNING: ffmpeg/ffprobe not found on PATH — /stream/<id> will 503.')

# Codecs the browser <video> tag can play directly inside MP4 across Chrome,
# Firefox, Edge, and Safari. Anything else gets re-encoded.
BROWSER_OK_VIDEO = {'h264'}
BROWSER_OK_AUDIO = {'aac'}

_PROBE_CACHE = {}
_PROBE_CACHE_MAX = 256


def probe_source(url):
    """Return {duration, bitrate, video_codec, audio_codec} or None.

    Cached per source URL because ffprobe over HTTP costs 1-3s.
    """
    if not FFPROBE_BIN:
        return None
    cached = _PROBE_CACHE.get(url)
    if cached is not None:
        return cached
    try:
        result = subprocess.run(
            [FFPROBE_BIN, '-v', 'error', '-print_format', 'json',
             '-show_format', '-show_streams', url],
            capture_output=True, text=True, timeout=20,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return None

    fmt = data.get('format') or {}
    try:
        duration = float(fmt.get('duration') or 0)
        bitrate = int(fmt.get('bit_rate') or 0)
    except (TypeError, ValueError):
        return None
    video_codec = None
    audio_codec = None
    for s in data.get('streams') or []:
        kind = s.get('codec_type')
        name = (s.get('codec_name') or '').lower()
        if kind == 'video' and not video_codec:
            video_codec = name
        elif kind == 'audio' and not audio_codec:
            audio_codec = name
    if duration <= 0 or bitrate <= 0:
        return None

    probe = {
        'duration': duration,
        'bitrate': bitrate,
        'video_codec': video_codec,
        'audio_codec': audio_codec,
    }
    if len(_PROBE_CACHE) >= _PROBE_CACHE_MAX:
        _PROBE_CACHE.pop(next(iter(_PROBE_CACHE)))
    _PROBE_CACHE[url] = probe
    return probe


def decide_ffmpeg_args(probe):
    """Pick per-stream copy vs re-encode flags based on source codecs."""
    args = []
    if probe.get('video_codec') in BROWSER_OK_VIDEO:
        args += ['-c:v', 'copy']
    else:
        args += ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23']
    if probe.get('audio_codec') in BROWSER_OK_AUDIO:
        args += ['-c:a', 'copy']
    else:
        args += ['-c:a', 'aac', '-b:a', '192k']
    args += [
        '-movflags', 'frag_keyframe+empty_moov+default_base_moof+faststart',
        '-f', 'mp4',
    ]
    return args


_RANGE_RE = re.compile(r'^bytes=(\d+)-(\d*)$')


def parse_range_header(header, total_size):
    """Return (start, end, bounded). end is inclusive; bounded=True iff the
    client asked for a specific end byte (e.g. bytes=0-1)."""
    if not header:
        return 0, total_size - 1, False
    m = _RANGE_RE.match(header.strip())
    if not m:
        return 0, total_size - 1, False
    start = int(m.group(1))
    if m.group(2):
        end = int(m.group(2))
        bounded = True
    else:
        end = total_size - 1
        bounded = False
    start = max(0, min(start, total_size - 1))
    end = max(start, min(end, total_size - 1))
    return start, end, bounded


# ---------------------------------------------------------------------------
# TMDB enrichment (unchanged)
# ---------------------------------------------------------------------------

@cache.memoize(timeout=86400)
def fetch_tmdb_details(movie_id):
    """Fetch cast + key crew from TMDB. Returns dict or None on failure."""
    if not TMDB_API_KEY or not movie_id:
        return None
    try:
        resp = requests.get(
            f"https://api.themoviedb.org/3/movie/{movie_id}",
            params={'api_key': TMDB_API_KEY, 'append_to_response': 'credits'},
            timeout=5,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
    except (requests.RequestException, ValueError):
        return None

    credits = data.get('credits') or {}
    cast_raw = credits.get('cast') or []
    crew_raw = credits.get('crew') or []

    cast = [
        {
            'name': c.get('name'),
            'character': c.get('character'),
            'profile_url': (
                f"https://image.tmdb.org/t/p/w185{c['profile_path']}"
                if c.get('profile_path') else None
            ),
        }
        for c in cast_raw[:8]
    ]

    directors = []
    writers = []
    for c in crew_raw:
        name = c.get('name')
        if not name:
            continue
        if c.get('job') == 'Director' and name not in directors:
            directors.append(name)
        if c.get('department') == 'Writing' and name not in writers:
            writers.append(name)

    return {
        'cast': cast,
        'directors': directors,
        'writers': writers,
        'runtime': data.get('runtime'),
        'tagline': data.get('tagline'),
    }


# ---------------------------------------------------------------------------
# Movie listing
# ---------------------------------------------------------------------------

genres = [
    'Action', 'Adventure', 'Animation', 'Comedy', 'Crime', 'Documentary',
    'Drama', 'Family', 'Fantasy', 'History', 'Horror', 'Music', 'Mystery',
    'Romance', 'Science Fiction', 'TV Movie', 'Thriller', 'War', 'Western',
]

decades = ['1930', '1940', '1950', '1960', '1970', '1980', '1990', '2000', '2010', '2020']


ALLOWED_SORT_COLUMNS = {
    'id', 'movie_id', 'name', 'year', 'popularity',
    'vote_average', 'vote_count', 'genres', 'created',
}
ALLOWED_SORT_DIRS = {'ASC', 'DESC'}


def get_movies(genre, year, sort_by, search_text, sort_dir, page, per_page, offset, decade, original_language):
    conditions = []
    params = []

    if genre and genre in genres:
        conditions.append("genres LIKE ?")
        params.append(f'%{genre}%')
    if year and year.isdigit():
        conditions.append("year = ?")
        params.append(year)
    if original_language:
        conditions.append("original_language = ?")
        params.append(original_language.lower())
    if sort_by == 'vote_average':
        conditions.append("vote_count > 500")
    if len(search_text) > 1:
        conditions.append("name LIKE ?")
        params.append(f'%{search_text}%')
    if decade and decade.isdigit():
        conditions.append("year >= ? AND year < ?")
        params.extend([int(decade), int(decade) + 10])

    where_clause = ""
    if conditions:
        where_clause = " WHERE " + " AND ".join(conditions)

    if sort_by not in ALLOWED_SORT_COLUMNS:
        sort_by = 'name'
    if sort_dir not in ALLOWED_SORT_DIRS:
        sort_dir = 'DESC'
    order_clause = f" ORDER BY {sort_by} {sort_dir}"

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(f"SELECT COUNT(*) FROM movies{where_clause}", params)
    total = cursor.fetchone()[0]
    cursor.execute(
        f"SELECT DISTINCT movie_id, * FROM movies{where_clause}{order_clause} LIMIT ? OFFSET ?",
        params + [per_page, offset],
    )
    movies = cursor.fetchall()
    conn.close()

    pagination = Pagination(page=page, per_page=per_page, total=total, css_framework='foundation')
    return movies, pagination


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    error = None
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT * FROM users WHERE username = ? COLLATE NOCASE",
                (username,),
            ).fetchone()
            if (row and row['password_hash']
                    and check_password_hash(row['password_hash'], password)):
                conn.execute(
                    "UPDATE users SET last_login_at = ? WHERE id = ?",
                    (now_str(), row['id']),
                )
                conn.commit()
                login_user(User(row))
                next_url = request.args.get('next') or url_for('index')
                return redirect(next_url)
            error = 'Invalid username or password.'
        finally:
            conn.close()
    return render_template('login.html', error=error)


@app.route('/logout')
def logout():
    logout_user()
    return redirect(url_for('login'))


@app.route('/invite/<token>', methods=['GET', 'POST'])
def accept_invite(token):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE invite_token = ?", (token,)
        ).fetchone()
        if not row or row['accepted_at']:
            return render_template('invite_invalid.html'), 404

        error = None
        if request.method == 'POST':
            password = request.form.get('password') or ''
            confirm = request.form.get('confirm') or ''
            if len(password) < 6:
                error = 'Password must be at least 6 characters.'
            elif password != confirm:
                error = 'Passwords do not match.'
            else:
                conn.execute(
                    """UPDATE users
                       SET password_hash = ?, accepted_at = ?,
                           invite_token = NULL, last_login_at = ?
                       WHERE id = ?""",
                    (generate_password_hash(password), now_str(), now_str(), row['id']),
                )
                conn.commit()
                fresh = conn.execute(
                    "SELECT * FROM users WHERE id = ?", (row['id'],)
                ).fetchone()
                login_user(User(fresh))
                return redirect(url_for('profile'))
        return render_template('invite_accept.html', username=row['username'], error=error)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------------

@app.route('/admin/users')
@admin_required
def admin_users():
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT id, username, email, is_admin, invite_token,
                   invited_at, accepted_at, last_login_at, created_at
              FROM users
             ORDER BY created_at DESC, id DESC
        """).fetchall()
    finally:
        conn.close()

    users = []
    for r in rows:
        invite_url = None
        if r['invite_token'] and not r['accepted_at']:
            invite_url = request.host_url.rstrip('/') + url_for(
                'accept_invite', token=r['invite_token']
            )
        users.append({
            'id': r['id'],
            'username': r['username'],
            'email': r['email'],
            'is_admin': bool(r['is_admin']),
            'invited_at': r['invited_at'],
            'accepted_at': r['accepted_at'],
            'last_login_at': r['last_login_at'],
            'created_at': r['created_at'],
            'invite_url': invite_url,
        })
    return render_template('admin_users.html', users=users)


@app.route('/admin/users/invite', methods=['POST'])
@admin_required
def admin_users_invite():
    username = (request.form.get('username') or '').strip()
    email = (request.form.get('email') or '').strip() or None
    is_admin = 1 if request.form.get('is_admin') else 0

    if not username:
        flash('Username is required.', 'error')
        return redirect(url_for('admin_users'))

    conn = get_db()
    try:
        existing = conn.execute(
            "SELECT id FROM users WHERE username = ? COLLATE NOCASE", (username,)
        ).fetchone()
        if existing:
            flash(f"User {username!r} already exists.", 'error')
            return redirect(url_for('admin_users'))

        token = secrets.token_urlsafe(32)
        conn.execute(
            """INSERT INTO users
               (username, email, is_admin, invite_token, invited_at,
                invited_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (username, email, is_admin, token, now_str(),
             current_user.id, now_str()),
        )
        conn.commit()
    finally:
        conn.close()

    flash(f"Invited {username}. Copy the invite link below to send it.", 'success')
    return redirect(url_for('admin_users'))


# ---------------------------------------------------------------------------
# Profile + per-user movie state
# ---------------------------------------------------------------------------

@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    conn = get_db()
    try:
        if request.method == 'POST':
            xtream_url = (request.form.get('xtream_url') or '').strip() or None
            xtream_username = (request.form.get('xtream_username') or '').strip() or None
            xtream_password = (request.form.get('xtream_password') or '').strip() or None
            conn.execute(
                """UPDATE users
                   SET xtream_url = ?, xtream_username = ?, xtream_password = ?
                   WHERE id = ?""",
                (xtream_url, xtream_username, xtream_password, current_user.id),
            )
            conn.commit()
            flash('Saved.', 'success')
            return redirect(url_for('profile'))

        row = conn.execute(
            "SELECT * FROM users WHERE id = ?", (current_user.id,)
        ).fetchone()

        watchlist = conn.execute("""
            SELECT m.* FROM movies m
              JOIN user_movies um ON um.movie_id = m.id
             WHERE um.user_id = ? AND um.in_watchlist = 1
             ORDER BY um.watchlisted_at DESC
        """, (current_user.id,)).fetchall()

        seen = conn.execute("""
            SELECT m.*, um.rating FROM movies m
              JOIN user_movies um ON um.movie_id = m.id
             WHERE um.user_id = ? AND um.seen = 1
             ORDER BY um.seen_at DESC
        """, (current_user.id,)).fetchall()
    finally:
        conn.close()

    return render_template(
        'profile.html', user_row=row, watchlist=watchlist, seen=seen,
    )


def _upsert_user_movie(conn, user_id, movie_id, **fields):
    """Insert or update a row in user_movies with the given fields."""
    cols = ['user_id', 'movie_id'] + list(fields.keys())
    placeholders = ', '.join('?' for _ in cols)
    set_clause = ', '.join(f"{k} = excluded.{k}" for k in fields.keys())
    conn.execute(
        f"""INSERT INTO user_movies ({', '.join(cols)})
            VALUES ({placeholders})
            ON CONFLICT(user_id, movie_id) DO UPDATE SET {set_clause}""",
        [user_id, movie_id] + list(fields.values()),
    )


@app.route('/movie/<int:id>/watchlist/toggle', methods=['POST'])
@login_required
def toggle_watchlist(id):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT in_watchlist FROM user_movies WHERE user_id = ? AND movie_id = ?",
            (current_user.id, id),
        ).fetchone()
        new_state = 0 if (row and row['in_watchlist']) else 1
        _upsert_user_movie(
            conn, current_user.id, id,
            in_watchlist=new_state,
            watchlisted_at=now_str() if new_state else None,
        )
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for('movie_detail', id=id))


@app.route('/movie/<int:id>/seen/toggle', methods=['POST'])
@login_required
def toggle_seen(id):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT seen FROM user_movies WHERE user_id = ? AND movie_id = ?",
            (current_user.id, id),
        ).fetchone()
        new_state = 0 if (row and row['seen']) else 1
        _upsert_user_movie(
            conn, current_user.id, id,
            seen=new_state,
            seen_at=now_str() if new_state else None,
        )
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for('movie_detail', id=id))


@app.route('/movie/<int:id>/rate', methods=['POST'])
@login_required
def rate_movie(id):
    raw = (request.form.get('rating') or '').strip()
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 0 or value > 10:
        value = 0

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT rating, seen FROM user_movies WHERE user_id = ? AND movie_id = ?",
            (current_user.id, id),
        ).fetchone()
        current = row['rating'] if row else None
        # Toggle off when the user clicks their existing rating.
        if value == 0 or current == value:
            _upsert_user_movie(
                conn, current_user.id, id,
                rating=None, rated_at=None,
            )
        else:
            fields = {'rating': value, 'rated_at': now_str()}
            # Rating a movie implies you've seen it.
            if not (row and row['seen']):
                fields['seen'] = 1
                fields['seen_at'] = now_str()
            _upsert_user_movie(conn, current_user.id, id, **fields)
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for('movie_detail', id=id))


# ---------------------------------------------------------------------------
# Main routes
# ---------------------------------------------------------------------------

@app.route('/')
@login_required
def index():
    genre = request.args.get('genre') or ''
    year = request.args.get('year') or ''
    decade = request.args.get('decade') or ''
    sort_by = request.args.get('sort_by') or 'name'
    original_language = request.args.get('original_language') or ''
    sort_dir = request.args.get('sort_dir') or 'DESC'

    page, per_page, offset = get_page_args(
        page_parameter='page', per_page_parameter='per_page', per_page=100,
    )
    search_text = request.args.get('search_text') or ''
    movies, pagination = get_movies(
        genre, year, sort_by, search_text, sort_dir,
        page, per_page, offset, decade, original_language,
    )
    return render_template(
        'index.html',
        pagination=pagination, movies=movies, genre=genre, year=year,
        sort_by=sort_by, genres=genres, search_text=search_text,
        sort_dir=sort_dir, per_page=per_page, decade=decade,
        decades=decades, original_language=original_language,
    )


@app.route('/video')
@login_required
def movie():
    return render_template('video.html')


@app.route('/movie/<int:id>')
@login_required
def movie_detail(id):
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM movies WHERE id = ?", (id,)).fetchone()
        if row is None:
            abort(404)
        um = conn.execute(
            """SELECT in_watchlist, seen, rating
                 FROM user_movies WHERE user_id = ? AND movie_id = ?""",
            (current_user.id, id),
        ).fetchone()
    finally:
        conn.close()

    tmdb = fetch_tmdb_details(row['movie_id'])
    stream_url = build_stream_url(current_user, row['streaming_url'])
    state = {
        'in_watchlist': bool(um['in_watchlist']) if um else False,
        'seen': bool(um['seen']) if um else False,
        'rating': um['rating'] if um else None,
    }
    return render_template(
        'movie.html', movie=row, tmdb=tmdb,
        stream_url=stream_url, state=state,
    )


@app.route('/files/<path:path>')
def send_report(path):
    return send_from_directory('public', path)


@app.route('/stream/<int:id>', methods=['GET', 'HEAD'])
@login_required
def stream(id):
    if not FFMPEG_BIN or not FFPROBE_BIN:
        abort(503)

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT streaming_url FROM movies WHERE id = ?", (id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        abort(404)

    source_url = build_stream_url(current_user, row['streaming_url'])
    if not source_url:
        abort(404)

    probe = probe_source(source_url)
    if not probe:
        abort(502)

    duration = probe['duration']
    bitrate = probe['bitrate']
    total_size = max(1, int(bitrate * duration / 8))

    start, end, bounded = parse_range_header(
        request.headers.get('Range'), total_size,
    )
    content_length = end - start + 1
    # Map the requested byte offset back to a timestamp using the avg bitrate.
    # Accurate enough for CBR; off by a few seconds for VBR sources.
    start_time = (start * 8) / bitrate
    if start_time > max(0.0, duration - 1.0):
        start_time = max(0.0, duration - 1.0)

    headers = {
        'Accept-Ranges': 'bytes',
        'Content-Range': f'bytes {start}-{end}/{total_size}',
        'Content-Length': str(content_length),
        'Cache-Control': 'no-store',
        'Content-Type': 'video/mp4',
    }

    if request.method == 'HEAD':
        return Response(status=206, headers=headers)

    ffmpeg_cmd = [
        FFMPEG_BIN, '-hide_banner', '-loglevel', 'error',
        '-reconnect', '1', '-reconnect_streamed', '1',
        '-reconnect_delay_max', '5',
        '-ss', f'{start_time:.3f}',
        '-i', source_url,
    ] + decide_ffmpeg_args(probe) + ['pipe:1']

    proc = subprocess.Popen(
        ffmpeg_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
    )

    def generate():
        remaining = content_length if bounded else None
        try:
            while True:
                read_size = 64 * 1024
                if remaining is not None:
                    if remaining <= 0:
                        break
                    read_size = min(read_size, remaining)
                chunk = proc.stdout.read(read_size)
                if not chunk:
                    break
                yield chunk
                if remaining is not None:
                    remaining -= len(chunk)
        finally:
            try:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass

    return Response(generate(), status=206, headers=headers)


if __name__ == '__main__':
    # threaded=True is required so streaming /stream/<id> doesn't block
    # other requests (the player issues probe + data ranges in parallel).
    app.run(debug=True, host='0.0.0.0', port=5001, threaded=True)
