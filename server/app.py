"""FastAPI application setup."""

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Generator

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse

from engine.service import ChatService
from engine.scraper import scraper as scraper_service
from engine.config import (
    BROWSER_DATA_DIR, MEMORY_PATH as _MEMORY_PATH,
    MEMORY_SEARCH_SETTINGS_PATH as _MEMORY_SEARCH_SETTINGS,
    HOST, PORT
)
from engine.memory_search import get_searcher
from connectors.deepseek.client import get_client as get_deepseek_client

from server.database import init_db
from server.middleware import setup_middleware
from server.auth import get_auth_token

# Import route modules
from server.routes import (
    auth_routes, chats, memory, scraper, settings, skills,
    deepseek, browser, upload, file, health
)

logger = logging.getLogger("sable")

BASE_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = BASE_DIR / "web"
INDEX_FILE = WEB_DIR / "index.html"
UPLOAD_DIR = BASE_DIR / "uploads"


@asynccontextmanager
async def lifespan(app: FastAPI) -> Generator[None, None, None]:
    # Initialize database
    init_db()
    
    # Restore persisted memory-search settings
    if _MEMORY_SEARCH_SETTINGS.exists():
        try:
            _ms = json.loads(_MEMORY_SEARCH_SETTINGS.read_text(encoding="utf-8"))
            _s = get_searcher()
            if _ms.get("model"):
                _s.set_model(str(_ms["model"]))
            if isinstance(_ms.get("model_thresholds"), dict):
                _s.set_thresholds(_ms["model_thresholds"])
        except Exception:
            pass
    
    # Create and warm up service
    service = ChatService(user_data_dir=str(BROWSER_DATA_DIR))
    await service.warmup()
    
    # Set service in route modules
    chats.set_service(service)
    memory.set_service(service)
    settings.set_service(service)
    deepseek.set_service(service)
    browser.set_service(service)
    upload.set_service(service)
    
    # Refresh DeepSeek token
    try:
        ds_token = await service.refresh_deepseek_token()
        get_deepseek_client().set_token(ds_token)
    except Exception as exc:
        logger.warning("DeepSeek startup token refresh failed: %s: %s", type(exc).__name__, exc)
    
    yield
    
    # Cleanup
    await service.close()
    await scraper_service.stop(kill_browser=True)


def create_app() -> FastAPI:
    app = FastAPI(title="Sable API", version="0.4.0", lifespan=lifespan)
    
    # Setup middleware
    setup_middleware(app)
    
    # Mount static files
    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    if UPLOAD_DIR.exists():
        app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
    
    # Include routers
    app.include_router(auth_routes.router)
    app.include_router(health.router)
    app.include_router(chats.router)
    app.include_router(memory.router)
    app.include_router(scraper.router)
    app.include_router(settings.router)
    app.include_router(skills.router)
    app.include_router(deepseek.router)
    app.include_router(browser.router)
    app.include_router(upload.router)
    app.include_router(file.router)
    
    # Root endpoint
    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        if INDEX_FILE.exists():
            return INDEX_FILE.read_text(encoding="utf-8")
        return "<h1>Sable API is running</h1><p>POST /api/chat</p>"
    
    return app