import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import auth, jobs, screenshots, health, profiles


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(settings.screenshots_root, exist_ok=True)
    yield


app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(health.router, tags=["Health"])
app.include_router(auth.router, prefix="/auth", tags=["Authentication"])
app.include_router(jobs.router, prefix="/jobs", tags=["Jobs"])
app.include_router(profiles.router, prefix="/profiles", tags=["Browser Profiles"])
app.include_router(screenshots.router, tags=["Screenshots"])

# Static files for serving screenshots
os.makedirs(settings.static_root, exist_ok=True)
app.mount("/static", StaticFiles(directory=settings.static_root), name="static")
