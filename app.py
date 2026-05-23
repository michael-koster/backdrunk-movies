from flask import Flask, request, render_template, send_from_directory, abort
from flask_caching import Cache
from flask_paginate import Pagination, get_page_parameter, get_page_args
from dotenv import load_dotenv

import os
import requests
import sqlite3

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))
TMDB_API_KEY = (os.environ.get('TMDB_API_KEY') or '').strip()

app = Flask(__name__)
app.config['CACHE_TYPE'] = 'SimpleCache'
app.config['CACHE_DEFAULT_TIMEOUT'] = 86400
cache = Cache(app)


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


genres = [
    'Action',
    'Adventure',
    'Animation',
    'Comedy',
    'Crime',
    'Documentary',
    'Drama',
    'Family',
    'Fantasy',
    'History',
    'Horror',
    'Music',
    'Mystery',
    'Romance',
    'Science Fiction',
    'TV Movie',
    'Thriller',
    'War',
    'Western',
]

decades = ['1930', '1940', '1950', '1960', '1970', '1980', '1990', '2000', '2010', '2020']

secret_salt = 'dkd9039jsdlfkj3249562347easwdf534'

# function that creates a url checksum, the cecksum contains timestamp so that they are only valid for a certain amount of time


# TheMovieDB API response fields
# 0, id
# 1 movie_id
# 2 adult
# 3 backdrop_url
# 4 genres
# 5 original_language
# 6 original_title
# 7 overview
# 8 popularity
# 9 poster_url
# 10 release_date
# 11 title
# 12 video
# 13 vote_average
# 14 vote_count

def make_key():
   """A function which is called to derive the key for a computed value.
      The key in this case is the concat value of all the json request
      parameters. Other strategy could to use any hashing function.
   :returns: unique string for which the value should be cached.
   """
   user_data = request.args.to_dict(flat=True)
   return ",".join([f"{key}={value}" for key, value in user_data.items()])


ALLOWED_SORT_COLUMNS = {'id', 'movie_id', 'name', 'year', 'popularity', 'vote_average', 'vote_count', 'genres', 'created'}
ALLOWED_SORT_DIRS = {'ASC', 'DESC'}


# Function to get movies from the database with optional filters
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

    conn = sqlite3.connect('movies-v3.db')
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


# Route for the home page
@app.route('/')
#@cache.cached(timeout=60, make_cache_key=make_key)
def index():
    genre = request.args.get('genre') or ''
    year = request.args.get('year') or ''
    decade = request.args.get('decade') or ''
    sort_by = request.args.get('sort_by') 
    original_language = request.args.get('original_language') or ''
    if not sort_by:
        sort_by = 'name'

    sort_dir = request.args.get('sort_dir')
    if not sort_dir:
        sort_dir = 'DESC'

    page, per_page, offset = get_page_args(page_parameter='page', per_page_parameter='per_page', per_page=100)
    
    search_text = request.args.get('search_text') or ''
    movies, pagination = get_movies(genre, year, sort_by, search_text, sort_dir, page, per_page, offset, decade, original_language)
    return render_template('index.html', pagination=pagination, movies=movies, genre=genre, year=year, sort_by=sort_by, genres=genres, search_text=search_text, sort_dir=sort_dir, per_page=per_page, decade=decade, decades=decades, original_language=original_language)


@app.route('/video')
def movie():
    return render_template('video.html')

@app.route('/movie/<int:id>')
def movie_detail(id):
    conn = sqlite3.connect('movies-v3.db')
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM movies WHERE id = ?", (id,))
    row = cursor.fetchone()
    conn.close()
    if row is None:
        abort(404)
    tmdb = fetch_tmdb_details(row['movie_id'])
    return render_template('movie.html', movie=row, tmdb=tmdb)

@app.route('/files/<path:path>')
def send_report(path):
    return send_from_directory('public', path)

# Route for streaming a movie
@app.route('/stream/<path:url>')
def stream(url):
    return f'<a href="{url}">Stream this movie</a>'

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5001)

