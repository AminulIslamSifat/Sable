"""engine.scraper.diagnostics — HTTP client for Go beacon sidecar.

The beacon runs as a compiled Go binary with a local HTTP diagnostics
server on port 18923. This module provides Python wrappers that call
those endpoints, replacing the old in-process monitor/replay modules.
"""

from .client import (
    register_session,
    heartbeat,
    mark_inactive,
    unregister_session,
    get_alive_sessions,
    get_all_sessions,
    get_recent_events,
    probe_engine_pid,
    clear_monitor,
    start_replay,
    get_replay_result,
    stop_replay,
    list_replays,
    clear_replays,
)
