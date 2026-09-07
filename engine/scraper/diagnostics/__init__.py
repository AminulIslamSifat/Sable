"""engine.scraper.diagnostics — browser session health monitoring.

The beacon has been replaced by a compiled Go binary (bin/sable-beacon-*).
It is launched as a sidecar process by server/api/application.py.

Python source files (beacon.py, monitor.py, replay.py) are no longer
shipped in the public repo. Only the compiled binaries in bin/ are tracked.
"""
