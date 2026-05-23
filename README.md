# Backdrunk Movies

A small Flask web app that turns an M3U/Xtream Codes VOD feed into a browsable,
TMDB-enriched movie library. Visitors browse the catalogue and stream titles
through their own Xtream credentials; the catalogue is private and invite-only.

## Features

- **Movie library** — browse, filter, search and sort ~12k movies. Posters,
  backdrops, genres, language, popularity and TMDB rating are stored locally in
  SQLite; cast/crew/runtime/tagline are pulled live from TMDB and cached.
- **Invite-only auth** — admin users invite people by username; the app
  generates an invite URL to send manually (mail server hookup planned later).
- **Per-user Xtream credentials** — each user fills in their own Xtream server,
  username and password on their profile. The Stream button is hidden until
  they do, and the stream URL is rewritten with their credentials at render
  time.
- **In-browser playback with live transcoding** — the Stream button opens an
  inline HTML5 player that pulls from `/stream/<id>`. Flask pipes the source
  through `ffmpeg`, copying browser-compatible streams (H.264 + AAC) and
  re-encoding everything else (HEVC, AC3/EAC3/DTS, …) on the fly to fragmented
  MP4. Supports `Range` requests for seeking.
- **Watchlist** — heart icon on the movie page; lists in the profile.
- **Mark as seen** — eye icon on the movie page; lists in the profile.
- **1‑10 rating** — for a future recommendation engine.
- **Admin page** — `/admin/users` lists users with status, copies invite links,
  shows last-login.

## Tech stack

- Python 3 + Flask
- Flask-Login (session auth) + Werkzeug (password hashing)
- Flask-Caching (in-memory TMDB cache) + flask-paginate
- SQLite (`movies-v3.db`)
- Bootstrap 5 + Font Awesome (CDN)
- `ffmpeg` / `ffprobe` (system binaries on `PATH`, for live transcoding)

## Project layout

```
app.py               Flask app — routes, auth, DB migration, TMDB enrichment
db_update.py         One-off script: parses an M3U file and populates movies-v3.db from TMDB
movies-v3.db         SQLite database (movies, users, user_movies)
templates/           Jinja templates
public/flags/        Country flag PNGs served at /files/flags/<lang>.png
m3u_source/          Source M3U files for db_update.py (gitignored)
requirements.txt
.env                 Local secrets (gitignored)
```

## Development setup

### 1. Install system dependencies

`ffmpeg` and `ffprobe` must be on `PATH` for `/stream/<id>` (in-browser playback
with live transcoding). On Debian/Ubuntu:

```bash
sudo apt install ffmpeg
```

### 2. Clone and create a virtualenv

```bash
git clone <repo-url> backdrunk-movies
cd backdrunk-movies
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure `.env`

Create a `.env` file in the project root:

```ini
TMDB_API_KEY = 'your-tmdb-v3-api-key'
SECRET_KEY = 'generate-with-python -c "import secrets; print(secrets.token_hex(32))"'
ADMIN_USERNAME = 'admin'
ADMIN_PASSWORD = 'pick-a-strong-password'
```

- `TMDB_API_KEY` — v3 key from https://www.themoviedb.org/settings/api. Required
  for cast/crew/tagline lookups; the catalogue still works without it.
- `SECRET_KEY` — signs Flask session cookies. Generate once and keep stable.
- `ADMIN_USERNAME` / `ADMIN_PASSWORD` — used **only on first run** when the
  `users` table is empty, to bootstrap an admin account. Ignored on subsequent
  runs.

### 4. Run the dev server

```bash
python3 app.py
```

The app starts on `http://0.0.0.0:5001` in debug mode (auto-reload on file
change). On first run it migrates the database (additively — no existing rows
are touched) and creates the bootstrap admin.

### 5. Log in

Open http://localhost:5001/, log in as the admin user from `.env`, then go to
**Admin → Invite a user** to generate invite URLs for other users. Each invited
user clicks their link, sets a password, and lands on their profile to fill in
Xtream credentials.

## Populating the movie catalogue

The Flask app does not ingest the M3U feed — that's a separate offline job
handled by `db_update.py`. Drop your `source.m3u` into `m3u_source/` and run:

```bash
# List the group-title categories available in the file
python3 db_update.py list ./m3u_source/source.m3u

# Parse a category (edit db_update.py's parse_m3u() to set the regex) and
# enrich each title with TMDB metadata. Runs incrementally; safe to re-run.
python3 db_update.py parse ./m3u_source/source.m3u
```

Movies that already exist (by title or TMDB id) are skipped. Titles that TMDB
can't match are written to `not_found.txt`.

## Database

SQLite, three tables:

- `movies` — one row per movie (TMDB-enriched). Populated by `db_update.py`.
- `users` — accounts, invites, Xtream credentials.
- `user_movies` — per-(user, movie) state: `in_watchlist`, `seen`, `rating`.

The migration in `app.py` is purely additive (`CREATE TABLE IF NOT EXISTS` +
`ALTER TABLE ADD COLUMN` only). Bring-up never drops or rewrites rows, so it's
safe to deploy over an existing database.

Quick poke at the DB:

```bash
sqlite3 movies-v3.db ".tables"
sqlite3 movies-v3.db "SELECT COUNT(*) FROM movies;"
sqlite3 movies-v3.db "SELECT username, accepted_at, last_login_at FROM users;"
```

## Notes

- Xtream passwords are stored in plaintext (they're the user's own credentials
  to a third-party service, not site credentials). If that's not acceptable for
  your deployment, encrypt them at rest.
- The default port is 5001 to avoid clashing with macOS AirPlay (5000).
- The dev server (`app.run`) is fine for local use; behind a reverse proxy use
  a real WSGI server such as gunicorn. For streaming, prefer threaded workers
  (one long-lived ffmpeg pipe per active viewer):
  `gunicorn -w 2 --threads 8 -b 127.0.0.1:5001 app:app`.
- `/stream/<id>` maps byte ranges to timestamps via the source's average
  bitrate. Seeking is accurate to a few seconds for CBR sources; VBR sources
  may drift slightly. Suitable for a home server; for many concurrent viewers
  consider switching to HLS.
