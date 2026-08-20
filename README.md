# 🍿 SeretFinder — Israel Movie Showtime Aggregator

A backend scaffold that scrapes movie showtimes from **seret.co.il**, enriches
them with **TMDB** metadata, and stores everything in **PostgreSQL + pgvector**
for future radius-based and semantic (vector) search.

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
- **PostgreSQL 14+** with the `vector` extension available (installed by
  default in the official Docker image `pgvector/pgvector`)

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

Copy the template below into a file named `.env` in the project root:

```dotenv
# PostgreSQL (psycopg2 dialect)
DATABASE_URL=postgresql+psycopg2://postgres:postgres@localhost:5432/seretfinder

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

```sql
CREATE DATABASE seretfinder;
```

The `vector` extension and all tables (`movies`, `cinemas`, `showtimes`) are
created automatically when the API starts.

## 🚀 Running the application

Start the FastAPI server (from the project root, with the venv active):

```powershell
uvicorn backend.main:app --reload
```

- Interactive API docs: http://127.0.0.1:8000/docs
- Health check: http://127.0.0.1:8000/health

## 🕷️ Running the scraper

```powershell
python -m scraper.indexer
```

This scrapes the default Seret.co.il listing page, enriches each record with
TMDB metadata, and writes the results to `data.json` in the project root.

> The CSS selectors in `scraper/indexer.py` are best-effort defaults — inspect
> the live DOM of seret.co.il and update `MOVIE_LINK_SELECTOR`,
> `CINEMA_ROW_SELECTOR`, and `SHOWTIME_SLOT_SELECTOR` as needed.
