import os
import sqlite3
import requests
import re
import time
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

# Constants
TMDB_API_KEY = os.environ.get('TMDB_API_KEY', '8a587de91cf5a3605c413116322e7f96')
COMMIT_INTERVAL = 10  # Commit to the database every 10 movies
DEFAULT_WORKERS = 8   # Parallel TMDB requests
DBFILE = 'movies-v3.db'

# Dictionary to map genre IDs to names
genres = {
    28: 'Action',
    12: 'Adventure',
    16: 'Animation',
    35: 'Comedy',
    80: 'Crime',
    99: 'Documentary',
    18: 'Drama',
    10751: 'Family',
    14: 'Fantasy',
    36: 'History',
    27: 'Horror',
    10402: 'Music',
    9648: 'Mystery',
    10749: 'Romance',
    878: 'Science Fiction',
    10770: 'TV Movie',
    53: 'Thriller',
    10752: 'War',
    37: 'Western',
}


# TheMovieDB API response fields
# id
# adult
# backdrop_path
# genres
# original_language
# original_title
# overview
# popularity
# poster_path
# release_date
# title
# video
# vote_average
# vote_count

# Database setup with all the fields from TheMovieDB
def create_database():
    conn = sqlite3.connect(DBFILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS movies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            movie_id INTEGER,
            name TEXT NOT NULL,
            year INTEGER NOT NULL,
            poster_url TEXT NOT NULL,
            backdrop_url TEXT NOT NULL,
            genres TEXT NOT NULL,
            streaming_url TEXT NOT NULL,
            popularity REAL,
            release_date TEXT,
            overview TEXT,
            vote_average REAL,
            vote_count INTEGER,
            adult BOOLEAN,
            original_language TEXT,
            original_title TEXT,
            video BOOLEAN,
            created TEXT,
            updated TEXT
        )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_movies_movie_id ON movies(movie_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_movies_name_lower ON movies(lower(trim(name)))')
    conn.commit()
    conn.close()



# Parse M3U file. `groups` is a list of group-title prefixes to include.
def parse_m3u(file_path, groups):
    with open(file_path, 'r', encoding='utf-8') as file:
        content = file.read()

    alternation = '|'.join(re.escape(g) for g in groups)
    pattern = re.compile(
        rf'#EXTINF:-1.*group-title="(?:{alternation})[^"]*",(.+?)\s+(http[^\s]+)'
    )
    matches = pattern.findall(content)
    cleaned_titles_urls = []

    print(f'Found {len(matches)} movies in the M3U file (groups: {", ".join(groups)})')

    for title, url in matches:
        # Remove all [content] except for the year
        cleaned_title = re.sub(r'\[(?!\d{4}\])[^]]*\]', '', title).strip()
        cleaned_titles_urls.append((cleaned_title, url))

    return cleaned_titles_urls


# Get all distinct group titles from m3u file
def get_group_titles(file_path):
    with open(file_path, 'r', encoding='utf-8') as file:
        content = file.read()
    pattern = re.compile(r'#EXTINF:-1.*group-title="(.+?)".*')
    return sorted(set(pattern.findall(content)))
    


# Search for movie information on TheMovieDB
def search_movie_info(movie_title, api_key, year=None, session=None):
    params = {'api_key': api_key, 'include_adult': 'true', 'query': movie_title}
    if year:
        params['year'] = year
    getter = (session or requests).get
    response = getter(
        'https://api.themoviedb.org/3/search/movie',
        params=params,
        timeout=10,
    )
    if response.status_code == 200:
        results = response.json().get('results') or []
        if results:
            return results[0]
    return None

# Store movie information in the database
def store_movie_info(movie, cursor):
    cursor.execute('''
        INSERT INTO movies (name, movie_id, year, poster_url, backdrop_url, genres, streaming_url, popularity, release_date, overview, vote_average, vote_count, adult, original_language, original_title, video, created, updated)
        VALUES (?, ?, ?, ?,  ?,  ?,  ?,  ?,  ?,  ?,  ?,  ?,  ?,  ?,  ?,  ?, ?, ?)
    ''', (
        movie['name'],
        movie['id'],
        movie['year'],
        movie['poster_url'],
        movie['backdrop_url'],
        movie['genres'],
        movie['streaming_url'],
        movie['popularity'],
        movie['release_date'],
        movie['overview'],
        movie['vote_average'],
        movie['vote_count'],
        movie['adult'],
        movie['original_language'],
        movie['original_title'],
        movie['video'],
        movie['created'],
        movie['updated']
    ))


def normalize_name(name):
    return name.strip().lower()


def write_to_logfile(message):
    with open('not_found.txt', 'a', encoding='utf-8') as file:
        file.write(message + '\n')


def build_movie_record(movie_info, url):
    current_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
    release_date = movie_info.get('release_date') or ''
    poster_path = movie_info.get('poster_path') or ''
    backdrop_path = movie_info.get('backdrop_path') or ''
    return {
        'name': movie_info['title'],
        'id': movie_info['id'],
        'year': release_date[:4],
        'poster_url': f'https://image.tmdb.org/t/p/w500{poster_path}' if poster_path else '',
        'backdrop_url': f'https://image.tmdb.org/t/p/original{backdrop_path}' if backdrop_path else '',
        'genres': ', '.join(
            name for name in (genres.get(g) for g in movie_info.get('genre_ids', [])) if name
        ),
        'streaming_url': url,
        'popularity': movie_info.get('popularity'),
        'release_date': release_date,
        'overview': movie_info.get('overview'),
        'vote_average': movie_info.get('vote_average'),
        'vote_count': movie_info.get('vote_count'),
        'adult': movie_info.get('adult'),
        'original_language': movie_info.get('original_language'),
        'original_title': movie_info.get('original_title'),
        'video': movie_info.get('video'),
        'created': current_time,
        'updated': current_time,
    }


def main(m3u_file, groups, workers=DEFAULT_WORKERS):
    create_database()
    start = time.monotonic()

    with sqlite3.connect(DBFILE) as conn:
        cursor = conn.cursor()

        # Pre-load existing movies — replaces a full-table scan per item with O(1) set lookups.
        existing_names = {
            row[0] for row in cursor.execute('SELECT lower(trim(name)) FROM movies')
        }
        existing_ids = {
            row[0] for row in cursor.execute(
                'SELECT movie_id FROM movies WHERE movie_id IS NOT NULL'
            )
        }

        movie_titles_urls = parse_m3u(m3u_file, groups)

        # Parse titles and skip names already in the DB before spending any HTTP.
        to_fetch = []
        for title, url in movie_titles_urls:
            match = re.match(r'(.+?)(?:\s+\[(\d{4})\])?$', title)
            if not match:
                print(f'🔍 {title} No movies found in the M3U file')
                continue
            movie_name = match.group(1).strip()
            movie_year = match.group(2) or None
            if normalize_name(movie_name) in existing_names:
                continue
            to_fetch.append((movie_name, movie_year, url))

        if not to_fetch:
            print('No new movies to fetch')
            return

        print(f'Fetching {len(to_fetch)} new movies from TMDB ({workers} workers)…')

        session = requests.Session()

        def fetch(item):
            movie_name, movie_year, _url = item
            try:
                info = search_movie_info(movie_name, TMDB_API_KEY, year=movie_year, session=session)
            except requests.RequestException:
                info = None
            return item, info

        movie_count = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(fetch, item) for item in to_fetch]
            for fut in as_completed(futures):
                (movie_name, movie_year, url), movie_info = fut.result()

                if movie_info is None:
                    label = f'{movie_name} ({movie_year})' if movie_year else movie_name
                    print(f'⛔ Not found at TMDB: {label}')
                    write_to_logfile(movie_name)
                    continue

                # Guard against two M3U entries resolving to the same TMDB id
                # (the set is also seeded with rows already in the DB).
                if movie_info['id'] in existing_ids:
                    print(f'🟧 {movie_info["id"]} {movie_name} already exists in the database')
                    continue
                existing_ids.add(movie_info['id'])

                movie = build_movie_record(movie_info, url)
                store_movie_info(movie, cursor)
                print(f'🟢 {movie["name"]}')
                movie_count += 1

                if movie_count % COMMIT_INTERVAL == 0:
                    conn.commit()
                    print(f'✅ Committed {movie_count} movies to the database')

        conn.commit()
        elapsed = time.monotonic() - start
        print(f'Committed {movie_count} movies in {elapsed:.1f}s')
        print('Done!')

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Process M3U file')
    parser.add_argument('action', choices=['list', 'parse'], help='Action to perform')
    parser.add_argument(
        'file',
        type=str,
        nargs='?',
        default='./m3u_source/source.m3u',
        help='M3U file to process (default: ./m3u_source/source.m3u)',
    )
    parser.add_argument(
        '--groups',
        nargs='+',
        default=['IMDB Top Movie'],
        help='Group-title prefixes to parse (parse action). May be repeated. '
             'Default: "IMDB Top Movie"',
    )
    parser.add_argument(
        '--workers',
        type=int,
        default=DEFAULT_WORKERS,
        help=f'Number of parallel TMDB requests (default: {DEFAULT_WORKERS}). Use 1 to disable parallelism.',
    )
    args = parser.parse_args()

    if args.action == 'list':
        print('Group Titles:')
        for group_title in get_group_titles(args.file):
            print(group_title)
    elif args.action == 'parse':
        print('Parsing M3U file...')
        main(args.file, args.groups, workers=args.workers)
