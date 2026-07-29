#!/usr/bin/env python3
"""Sable FastAPI server with persistence and SSE chat streaming.

Main entry point for the Sable API server.
"""

import uvicorn

from server.app import create_app
from engine.config import HOST, PORT

app = create_app()

if __name__ == "__main__":
    uvicorn.run("server:app", host=HOST, port=PORT, reload=False)