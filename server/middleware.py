"""Middleware setup for the FastAPI app."""

from fastapi.middleware.cors import CORSMiddleware

from server.auth import auth_middleware


def setup_middleware(app):
    """Configure all middleware for the FastAPI app."""
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    
    # Auth middleware must be added last (or first, depending on order)
    app.middleware("http")(auth_middleware)