import sqlite3
import requests
import json
import re
import time
import argparse

# Constants
TMDB_API_KEY = '8a587de91cf5a3605c413116322e7f96'  # Replace with your TMDB API key
REQUEST_DELAY = 0.05  # 100 milliseconds delay between requests to respect rate limits
COMMIT_INTERVAL = 10  # Commit to the database every 10 movies
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
    conn.commit()
    conn.close()



    
# VOD: IMDB Top Movies
# VOD: Premiere Cinemas
# VOD: Old Popular Movies
# VOD: Svenska

# Parse M3U file
def parse_m3u(file_path):
    with open(file_path, 'r') as file:
        content = file.read()
    #pattern = re.compile(r'#EXTINF:-1.*group-title="Premiere Cinemas.*",(.+?)\s+(http[^\s]+)')
    pattern = re.compile(r'#EXTINF:-1.*group-title="Old Popular Movies.*",(.+?)\s+(http[^\s]+)')
    matches = pattern.findall(content)
    cleaned_titles_urls = []
    
    print(f'Found {len(matches)} movies in the M3U file')

    for match in matches:
        title, url = match
        # Remove all [content] except for the year
        cleaned_title = re.sub(r'\[(?!\d{4}\])[^]]*\]', '', title).strip()
        cleaned_titles_urls.append((cleaned_title, url))
    
    return cleaned_titles_urls


# get all group titles from m3u file, distict
def get_group_titles(file_path):
    with open(file_path, 'r') as file:
        content = file.read()
    pattern = re.compile(r'#EXTINF:-1.*group-title="(.+?)".*')
    matches = pattern.findall(content)
    return list(set(matches))
    


# Search for movie information on TheMovieDB
def search_movie_info(movie_title, api_key):
    search_url = f'https://api.themoviedb.org/3/search/movie?api_key={api_key}&include_adult=true&query={movie_title}'
    response = requests.get(search_url)
    if response.status_code == 200:
        data = response.json()
        if data['results']:
            return data['results'][0]
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

def find_movie_by_id(movie_id):
    conn = sqlite3.connect(DBFILE)
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM movies WHERE movie_id = ?', (movie_id,))
    movie = cursor.fetchone()
    conn.close()
    return movie


# find movie in db by name
def find_movie_by_name(movie_name):
    conn = sqlite3.connect(DBFILE)
    cursor = conn.cursor()
    # Normalize both sides for comparison
    cursor.execute('SELECT * FROM movies WHERE lower(trim(name)) like ?', (movie_name.strip().lower(),))
    movie = cursor.fetchone()
    conn.close()
    return movie


def write_to_logfile(message):
    filename = 'not_found.txt'
    with open(filename, 'a') as file:
        file.write(message + '\n')


def main(m3u_file):
    create_database()
    
    conn = sqlite3.connect(DBFILE)
    cursor = conn.cursor()

    movie_titles_urls = parse_m3u(M3U_FILE)
    movie_count = 0

    for title, url in movie_titles_urls:
        match = re.match(r'(.+?)(?:\s+\[(\d{4})\])?$', title)
        #match = re.match(r'(.+)\s+\[(\d{4})\]', title)
        #match = re.match(r'(.+)', title)
        if match:
            movie_name = match.group(1).strip()
            movie_year = int(match.group(2)) if match.group(2) else ""
            
            #print(f'🔍 Searching for {movie_name} ({movie_year})...')
            
            search_name = movie_name.replace(' ', '+').replace('.', '_') + '+' + str(movie_year)


            if search_name[-1].isdigit():
                search_name = search_name[:-4].strip()

            search_name = search_name
            # Duplicate check
            if find_movie_by_name(movie_name) != None:
                #print(f'🟠 {movie_name} already exists in the database')
                continue
                
            
            # Respect rate limits
            time.sleep(REQUEST_DELAY)

            movie_info = search_movie_info(search_name, TMDB_API_KEY)
            if (movie_info is None):
                print(f'⛔ Not found at TMDB: {search_name}')
                write_to_logfile(movie_name)
                continue

            if (find_movie_by_id(movie_info['id']) != None):
                print(f'🟧 {movie_info['id']} {movie_name} already exists in the database')
                continue

            current_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())

            if movie_info:
                movie = {
                    'name': movie_info['title'],
                    'id': movie_info['id'],
                    'year': movie_info['release_date'][:4],
                    'poster_url': f'https://image.tmdb.org/t/p/w500{movie_info["poster_path"]}',
                    'backdrop_url': f'https://image.tmdb.org/t/p/w500{movie_info["backdrop_path"]}',
                    'genres': ', '.join([genres[genre_id] for genre_id in movie_info['genre_ids']]),
                    'streaming_url': url,
                    'popularity': movie_info['popularity'],
                    'release_date': movie_info['release_date'],
                    'overview': movie_info['overview'],
                    'vote_average': movie_info['vote_average'],
                    'vote_count': movie_info['vote_count'],
                    'adult': movie_info['adult'],
                    'original_language': movie_info['original_language'],
                    'original_title': movie_info['original_title'],
                    'video': movie_info['video'],
                    'created': current_time,
                    'updated': current_time,

                }
                store_movie_info(movie, cursor)
                print(f'🟢 {movie["name"]}')
                movie_count += 1

                # Commit to the database every COMMIT_INTERVAL movies
                if movie_count % COMMIT_INTERVAL == 0:
                    conn.commit()
                    print(f'✅ Committed {movie_count} movies to the database')

        else:
            print(f'🔍 {title} No movies found in the M3U file')

        
    # Final commit to catch any remaining movies
    conn.commit()
    print(f'Committed {movie_count} movies to the database')
    conn.close()
    print('Done!')

if __name__ == '__main__':

    # argument parser form action (list or parse) and m3u file
    parser = argparse.ArgumentParser(description='Process M3U file')
    parser.add_argument('action', type=str, help='Action to perform: list or parse', default='list')
    parser.add_argument('file', type=str, help='M3U file to process', default='./m3u_source/source.m3u')
    args = parser.parse_args()

    if args.action == 'list':
        print('Group Titles:')
        M3U_FILE = args.file
        group_titles = get_group_titles(M3U_FILE)
        for group_title in group_titles:
            #if 'VOD' in group_title:
            print(group_title)
    elif args.action == 'parse':
        print('Parsing M3U file...')
        M3U_FILE = args.file
        main(M3U_FILE)




    
