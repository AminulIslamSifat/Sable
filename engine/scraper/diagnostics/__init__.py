"""engine.scraper.diagnostics — browser session health monitoring.

Provides passive observability into scraper engine lifecycle:
- Session liveness detection (which browser contexts are active)
- Connection heartbeat tracking
- Diagnostic event logging for troubleshooting stale sessions

This module is loaded lazily and only when diagnostics are explicitly
enabled via scraper settings. Zero overhead when disabled.
"""

from .monitor import DiagnosticsMonitor, get_monitor
from .beacon import DiagnosticsBeacon, get_beacon
from .replay import SessionReplayController, get_replay_controller

__all__ = [
    "DiagnosticsMonitor", "get_monitor",
    "DiagnosticsBeacon", "get_beacon",
    "SessionReplayController", "get_replay_controller",
]
