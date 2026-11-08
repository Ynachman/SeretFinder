# 🍿 SeretFinder — Israel Movie Showtime Aggregator

A backend scaffold that scrapes movie showtimes from **seret.co.il**, enriches
them with **TMDB** metadata, and stores everything in **PostgreSQL + pgvector**
for radius-based search (the vector column is kept for future semantic search).

## 📁 Project Structure

```text
SeretFinder/
├── backend/
│   ├── __init__.py     # Marks the folder as a Python package
│   ├── config.py       # Typed settings (.env): DB URL, TMDB & ScrapingAnt keys
│   ├── database.py     # SQLAlchemy engine, session factory, pgvector bootstrap
│   ├── models.py       # Movie, Cinema, and Showtime ORM models
│   └── main.py         # FastAPI app entrypoint with CORS setup
├── scraper/
│   ├── __init__.py     # Marks the folder as a Python package
│   ├── indexer.py      # Base Playwright scraper targeting seret.co.il
│   └── utils.py        # Hebrew-text normalization + TMDB API helpers
├── requirements.txt    # Python dependencies
└── README.md
```

## ✅ Prerequisites

- **Python 3.11+** — verify with `python --version` (the `py` launcher is
  preferred on Windows)
- A **[Neon](https://neon.tech)** account (free tier) — SeretFinder uses
  cloud-hosted Postgres rather than a local install. Neon's Postgres images
  ship the `vector` extension, so no separate pgvector setup is needed beyond
  running `CREATE EXTENSION` (the app does this automatically on startup).

## 🪟 Setup on Windows (PowerShell)

Open a terminal in the project root and run:

```powershell
# 1. Create the virtual environment
py -m venv .venv

# 2. Activate it (PowerShell)
.venv\Scripts\Activate.ps1

# 3. Upgrade pip
python -m pip install --upgrade pip

# 4. Install dependencies
pip install -r requirements.txt

# 5. Install the Playwright Chromium browser (required for scraping)
playwright install chromium
```

> **Note:** if the activation step is blocked, run PowerShell as administrator
> and execute `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser`
> first, then retry.

### Creating the `.env` file

Copy the template below into a file named `.env` in the project root (or edit
`init.env`, which is read the same way):

```dotenv
# PostgreSQL (psycopg2 dialect) — Neon connection string, with sslmode=require.
# Get this from the Neon console: Project -> Connect -> "Connection string".
# Neon gives you a *pooled* connection string (host contains "-pooler") for
# app traffic, and a direct one for migrations/admin work — the pooled one is
# fine here.
DATABASE_URL=postgresql+psycopg2://<user>:<password>@<project>-pooler.<region>.aws.neon.tech/<dbname>?sslmode=require

# TMDB (https://www.themoviedb.org/settings/api)
TMDB_API_KEY=your_tmdb_api_key

# ScrapingAnt (optional residential proxy)
SCRAPINGANT_API_KEY=your_scrapingant_key
SCRAPINGANT_PROXY_HOST=proxy.scrapingant.com
SCRAPINGANT_PROXY_PORT=8000
SCRAPINGANT_PROXY_USERNAME=
SCRAPINGANT_PROXY_PASSWORD=

# CORS origins (comma-separated)
CORS_ORIGINS=http://localhost:3000,http://127.0.0.1:3000
```

## 🗄️ Preparing the database

1. Sign up at [neon.tech](https://neon.tech) (free tier is enough for this
   project) and create a new project — this provisions a default database
   and gives you a connection string immediately.
2. Copy the connection string into `DATABASE_URL` in `.env` / `init.env` as
   shown above. Make sure it uses the `postgresql+psycopg2://` scheme (Neon's
   console shows a plain `postgresql://` URL — just prepend `+psycopg2`) and
   keeps `?sslmode=require`.
3. That's it — the `vector` extension and all tables (`movies`, `cinemas`,
   `showtimes`) are created automatically the first time the API starts, via
   `backend/database.py`'s `enable_vector_extension()` and
   `Base.metadata.create_all()`.

No local Postgres install, Docker container, or `psql` session is required.

## 🚀 Running the application

Start the FastAPI server (from the project root, with the venv active):

```powershell
uvicorn backend.main:app --reload
```

- **Web app (Hebrew, RTL):** http://127.0.0.1:8000/ — browse what's showing,
  search by title, filter
  by city or "near me", and open a movie for showtimes grouped by cinema.
- Interactive API docs: http://127.0.0.1:8000/docs
- Health check: http://127.0.0.1:8000/health

## 📱 Publishing for free (GitHub Pages)

The app your friends use is **fully static**: `site/index.html` plus one
`site/data.json` (~35 KB gzipped) that `python -m scraper.export` writes from
the database. The page filters by city/radius/day and sorts on the phone
itself, so it opens instantly from a CDN with no server to wake up, and a
service worker makes it launch instantly (and work offline) from the home
screen.

`.github/workflows/publish.yml` runs every day at 03:00 UTC on GitHub's free
runners: scrape → update Neon → ratings → export → deploy to GitHub Pages.
One-time setup:

1. Push this repo to GitHub. GitHub Pages is free for **public** repositories
   (`init.env` is gitignored, so no secrets are published).
2. Repo **Settings → Secrets and variables → Actions**: add `DATABASE_URL`,
   `TMDB_API_KEY` and `OMDB_API_KEY`.
3. Repo **Settings → Pages → Source: GitHub Actions**.
4. **Actions → Refresh & publish → Run workflow** for the first run. The app is
   then at `https://<user>.github.io/<repo>/`.

Friends open the link and use **Share → Add to Home Screen** (iPhone) or
**⋮ → Install app** (Android). If the scrape ever fails (e.g. the site blocks
GitHub's servers), the last data is still published and the app's footer
warns once the data is more than two days old.

## 🔄 Daily refresh

(Locally, or as a fallback to the GitHub workflow above.) One command runs
the whole pipeline — scrape → ingest → prune old showtimes → geocode new
cinemas → ratings → export `site/data.json`:

```powershell
python -m scraper.refresh
```

To run it automatically every day (default 04:00, current user, no admin
needed; the PC must be on — a missed run happens at next login):

```powershell
.\scripts\schedule_daily_refresh.ps1             # or -At 06:30, or -Remove
Start-ScheduledTask -TaskName "SeretFinder daily refresh"   # test it now
```

Output is appended to `logs\refresh.log`.

## ⭐ Ratings

Each movie shows its **IMDb** rating and **Rotten Tomatoes** score (TMDB's
score only as a fallback). Sources:

- **IMDb rating + votes:** IMDb's official daily dataset
  (`title.ratings.tsv.gz`, free for personal/non-commercial use, cached in
  `data/`). Much fresher than OMDb, which lags weeks behind on new releases.
- **IMDb id + English title:** TMDB.
- **Rotten Tomatoes:** [OMDb](https://www.omdbapi.com/apikey.aspx) (free key,
  1,000 requests/day; add `OMDB_API_KEY=...` to `init.env`). RT only scores
  movies its critics reviewed, so most Israeli and very new releases have none.

Run `python -m scraper.ratings --all` once; the daily refresh keeps them
current. Google user ratings are not included — Google offers no public API
for movie ratings.

Movies are sorted by **popularity** by default: the number of upcoming
screenings (within the selected area and day), which is what Israeli cinemas
are actually betting on this week.

## 🔎 API endpoints

| Endpoint | Description |
| --- | --- |
| `GET /movies?q=&now_showing=true&limit=&offset=` | Search/list movies (default: only those with upcoming showtimes) |
| `GET /cities` | Cities with geocoded cinemas and their centre point |
| `GET /movies?lat=&lng=&radius_km=` | Same filters on `/movies` and `/movies/{id}/showtimes`: only what shows within the radius |
| `GET /movies/{id}` | Movie detail |
| `GET /movies/{id}/showtimes?city=&date_from=&date_to=` | Upcoming screenings with cinema info |
| `GET /cinemas?city=&chain=` | List cinemas |
| `GET /cinemas/nearby?lat=&lng=&radius_km=` | Cinemas within a radius, nearest first (haversine) |
| `GET /cinemas/{id}` | Cinema detail |
| `GET /cinemas/{id}/showtimes?date_from=&date_to=` | Upcoming screenings at a cinema |

## 🕷️ Running the scraper

```powershell
python -m scraper.indexer
```

This scrapes the default Seret.co.il listing page, enriches each record with
TMDB metadata, and writes the results to `data.json` in the project root.

With `--to-db`, new cinemas are geocoded automatically (OpenStreetMap
Nominatim, ~1 request/second, no API key): the venue is looked up by name and
city, falling back to the city centre. To (re)run it by hand:

```powershell
python -m scraper.geocode          # fill in missing coordinates
python -m scraper.geocode --redo   # re-geocode every cinema
```

> The CSS selectors in `scraper/indexer.py` are best-effort defaults — inspect
> the live DOM of seret.co.il and update `MOVIE_LINK_SELECTOR`,
> `CINEMA_ROW_SELECTOR`, and `SHOWTIME_SLOT_SELECTOR` as needed.
