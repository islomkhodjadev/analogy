# CLAUDE.md — auto_screen_api

## Project Overview

AI-powered website screenshot automation API. Crawls websites using browser automation (Playwright/Selenium), uses OpenAI GPT models to intelligently decide which pages to visit, captures full-page screenshots organized by theme, and optionally exports to Miro boards.

**Domain:** `analogy.postbaby.uz`
**Frontend:** `webskaut.netlify.app`

## Tech Stack

| Layer              | Technology                                    |
|--------------------|-----------------------------------------------|
| Language           | Python 3.12                                   |
| Framework          | FastAPI (sync routes, no async/await)          |
| ORM                | SQLAlchemy 2.0                                |
| Database           | PostgreSQL 16                                 |
| Migrations         | Alembic                                       |
| Cache/Broker       | Redis 7                                       |
| Task Queue         | Celery 5.3 (Redis broker + backend)           |
| Auth               | JWT (python-jose) + bcrypt (passlib)          |
| Browser (default)  | Playwright (Chromium, sync API)               |
| Browser (alt)      | Selenium + undetected-chromedriver            |
| AI/LLM             | OpenAI SDK (GPT-4-turbo / gpt-4.1)           |
| External API       | Miro REST API v2 (httpx)                      |
| Containerization   | Docker + Docker Compose                       |
| Reverse Proxy      | Nginx + Let's Encrypt (certbot)               |
| CI/CD              | GitHub Actions (SSH deploy to main)           |

## Project Structure

```
auto_screen_api/
├── app/                        # FastAPI application (HTTP layer)
│   ├── main.py                 # App entry, routers, CORS, static mount
│   ├── config.py               # Pydantic Settings (loads .env)
│   ├── database.py             # SQLAlchemy engine, SessionLocal, Base
│   ├── dependencies.py         # get_db(), get_current_user() (JWT)
│   ├── core/security.py        # bcrypt hashing, JWT encode/decode
│   ├── models/                 # SQLAlchemy ORM models
│   │   ├── user.py             # User (UUID PK)
│   │   ├── job.py              # Job + JobStatus enum
│   │   ├── screenshot.py       # Screenshot
│   │   └── profile.py          # BrowserProfile
│   ├── routers/                # FastAPI routers
│   │   ├── auth.py             # /auth — register, login, me
│   │   ├── jobs.py             # /jobs — CRUD + Miro export trigger
│   │   ├── screenshots.py      # /screenshots
│   │   ├── profiles.py         # /profiles — browser profile CRUD
│   │   └── health.py           # GET /health
│   ├── schemas/                # Pydantic v2 request/response schemas
│   ├── services/               # Business logic services
│   │   ├── miro.py             # MiroExporter (Miro API v2 calls)
│   │   └── board_planner.py    # AI board layout planner (GPT-4.1)
│   └── worker/                 # Celery worker
│       ├── celery_app.py       # Celery app config
│       ├── tasks.py            # run_screenshot_job, run_miro_export
│       └── engine.py           # Bridge: AppConfig → SiteAgent → DB
├── core/                       # Reusable browser automation engine
│   ├── config.py               # AppConfig dataclass + constants
│   ├── agent.py                # SiteAgent — main AI crawl loop
│   ├── playwright_controller.py # Playwright browser wrapper
│   ├── browser_controller.py   # Selenium/undetected-chromedriver wrapper
│   ├── ai_analyzer.py          # AIAnalyzer — GPT prompts for planning
│   ├── screenshot_manager.py   # Screenshot file management
│   └── site_builder.py         # HTML gallery generator
├── alembic/                    # DB migrations (5 versions)
├── nginx/conf.d/               # Nginx configs
├── docker-compose.yml          # Production stack
├── docker-compose.local.yml    # Local dev stack
├── Dockerfile                  # API image
├── Dockerfile.worker           # Worker image (Chrome + Playwright)
├── Dockerfile.worker.local     # Worker image (local/arm64)
├── requirements.txt            # Python dependencies
└── .env.example                # Environment variable template
```

## Architecture Patterns

### Two-Layer Architecture
- **`app/`** — HTTP/API layer (FastAPI, SQLAlchemy, Celery tasks). Handles auth, routing, persistence.
- **`core/`** — Browser automation engine. Framework-agnostic (no FastAPI/SQLAlchemy imports). Connected via `AppConfig` dataclass.

### Key Design Decisions
- **All FastAPI routes are synchronous** — no async/await. Database calls use `SessionLocal()` directly.
- **Celery tasks are synchronous** — browser automation is blocking I/O.
- **Worker concurrency = 1** — one browser per worker to avoid memory issues. Max 5 tasks per child process.
- **UUID primary keys** on all tables (`postgresql.UUID`).
- **Ownership filtering** — every resource query filters by `user_id`.

### Crawl Session Lifecycle (SiteAgent)
1. **INIT** — Load browser profile (cookies + localStorage) if exists
2. **LOGIN** — Attempt login if credentials provided
3. **EXPLORE** — AI decision loop: observe → decide → act → repeat (max 200 steps, 14-min budget, max 50 screenshots)
4. **SAVE** — Persist updated cookies/localStorage to BrowserProfile
5. **QUIT** — Clean up browser resources

## Code Conventions

### Style
- Use `.format()` for string formatting (NOT f-strings) — this is the project convention
- Pydantic v2 schemas with `model_config` via inner `Config` class using `from_attributes = True`
- SQLAlchemy models use declarative base with `Column()` syntax
- Import order: stdlib → third-party → local (no isort/black enforced)

### Naming
- Snake_case for everything (files, functions, variables)
- Router files match resource names: `auth.py`, `jobs.py`, `profiles.py`
- Schema files mirror model files
- Alembic migrations prefixed with sequential numbers: `001_`, `002_`, etc.

### Patterns
- Database sessions via FastAPI `Depends(get_db)` — yields `SessionLocal()`
- Auth via `Depends(get_current_user)` — decodes JWT, returns User model
- Celery tasks accept primitive args (job_id, user_id as strings) — construct own DB sessions
- Browser controllers have identical public API — interchangeable via `browser_engine` param
- Miro export has two modes: simple grid layout OR AI-driven layout (when `prompt` is provided)

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check |
| POST | `/auth/register` | Create account, returns JWT |
| POST | `/auth/login` | Login, returns JWT |
| GET | `/auth/me` | Get current user |
| PATCH | `/auth/me` | Update API keys |
| POST | `/jobs/` | Create screenshot job (dispatches Celery task) |
| GET | `/jobs/` | List jobs (paginated, filterable) |
| GET | `/jobs/{id}` | Get job details |
| DELETE | `/jobs/{id}` | Delete job + screenshots + files |
| POST | `/jobs/{id}/export/miro` | Trigger async Miro export |
| GET | `/jobs/{id}/screenshots` | List screenshots for job |
| GET | `/screenshots/{id}` | Get single screenshot |
| CRUD | `/profiles/` | Browser profile management |

## Database

### Tables
- `users` — email, hashed_password, openai_api_key, miro_access_token
- `jobs` — url, depth, model, browser_engine, status (pending/running/completed/failed), celery tracking, miro export fields
- `screenshots` — url, title, description, theme, file_path, parent_url, order_index
- `browser_profiles` — domain, cookies_json, local_storage_json, session_storage_json, login credentials

### Migrations
```bash
# Create new migration
alembic revision --autogenerate -m "description"
# Apply migrations
alembic upgrade head
# Rollback one step
alembic downgrade -1
```

## Development

### Local Setup
```bash
# Start all services
docker compose -f docker-compose.local.yml up --build

# Services exposed:
#   API:      http://localhost:8000
#   Postgres: localhost:5432
#   Redis:    localhost:6379
```

### Production Deployment
Push to `main` branch triggers GitHub Actions → SSH deploy to server → rebuild & restart containers.

### Docker Services
| Service  | Image                | Notes |
|----------|----------------------|-------|
| postgres | postgres:16-alpine   | Internal only |
| redis    | redis:7-alpine       | Internal only |
| api      | Custom (python:3.12) | Runs alembic + uvicorn (2 workers) |
| worker   | Custom (Chrome+PW)   | Celery worker, 2GB memory limit |
| nginx    | nginx:alpine         | Ports 80/443, serves static files |

### Environment Variables
Copy `.env.example` to `.env` and fill in values. Key vars:
- `DATABASE_URL` — PostgreSQL connection string
- `REDIS_URL` — Redis connection URL
- `JWT_SECRET_KEY` — secret for JWT signing
- `DEFAULT_OPENAI_API_KEY` — fallback OpenAI API key
- `DEBUG` — enable debug mode

## Common Tasks

### Adding a New Endpoint
1. Create/update schema in `app/schemas/`
2. Create/update model in `app/models/` (if new table)
3. Add route in appropriate `app/routers/` file
4. Register router in `app/main.py` (if new router)
5. Create Alembic migration if schema changed

### Adding a New Celery Task
1. Define task in `app/worker/tasks.py`
2. Use `@celery_app.task(bind=True)` decorator
3. Create own DB session inside task — don't pass ORM objects
4. Trigger from router via `.delay()` or `.apply_async()`

### Modifying Browser Automation
- Playwright logic: `core/playwright_controller.py`
- Selenium logic: `core/browser_controller.py`
- AI decisions/prompts: `core/ai_analyzer.py`
- Crawl orchestration: `core/agent.py`
- Both controllers must maintain the same public API

## Testing

No automated tests exist yet. Manual testing via Postman collection (`postman_collection.json`).

## Important Notes

- **Never commit `.env`** — contains secrets. Use `.env.example` as template.
- **core/ is framework-agnostic** — do not import FastAPI/SQLAlchemy/Celery in `core/` files.
- **Worker memory** — each browser session uses significant memory. Worker is limited to 2GB with `--max-tasks-per-child=5`.
- **Celery task timeouts** — `run_screenshot_job` has 15-min soft limit / 16-min hard kill. Handles `SoftTimeLimitExceeded` gracefully.
- **Static files** — screenshots stored at `SCREENSHOTS_ROOT`, served by nginx in prod or FastAPI mount in dev.
