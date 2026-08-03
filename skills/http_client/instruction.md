# HTTP Client: Structured API Request Skill

A structured HTTP client for testing APIs, debugging endpoints, and running
multi-step request chains with variable capture and assertions. Powered by
httpx via PEP 723 (uv run), with environment presets for zero-repetition
auth and base URLs.

---

## Trigger Guard

| Condition | Action |
|---|---|
| User says "hit this endpoint", "test this API", "send a request to" | Fire this skill |
| User shares an API URL and wants to see the response | Fire this skill |
| User needs to chain requests (login then use token then fetch data) | Fire with --chain |
| User says "set up an env for [service]" | Fire with --env-set |
| Quick one-off GET to check if a URL is alive | Use xh directly instead (simpler) |
| User needs to download a file/video | NOT this skill - Video Downloader |
| User needs to scrape/extract structured data from a website | NOT this skill - use native web_extractor |

Rule of thumb: If it's a raw API call (REST, JSON, auth headers, webhooks) then this skill. If it's scraping a rendered website then use native web_extractor.

---

## Script Path

    PROJECT_ROOT/skills/http_client/scripts/api_request.py

All commands use uv run for zero-install dependency resolution (httpx).

---

## Commands

### Single Request

    uv run PROJECT_ROOT/skills/http_client/scripts/api_request.py METHOD URL [flags]

### Chained Requests

    uv run PROJECT_ROOT/skills/http_client/scripts/api_request.py --chain /path/to/chain.json --env NAME

### Environment Management

    uv run PROJECT_ROOT/skills/http_client/scripts/api_request.py --env-set NAME --base-url URL [--auth TYPE] [--token TOKEN]

---

## Parameters

| Flag | Values | Default | Notes |
|---|---|---|---|
| METHOD | GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS | - | Positional arg 1 |
| URL | Full URL or relative path (with --env) | - | Positional arg 2 |
| --env | Preset name | None | Loads base_url, auth, token from preset |
| --auth | bearer, basic, apikey | None | Sets Authorization header |
| --token | Token string | None | Used with --auth or stored in env |
| -H / --header | Key:Value (repeatable) | None | Custom headers |
| -p / --param | key=value (repeatable) | None | Query parameters |
| -b / --body | JSON string or @file.json | None | Request body |
| --timeout | Seconds (float) | 30.0 | Request timeout |
| --no-redirect | Flag | Off | Don't follow redirects |
| --chain | Path to chain JSON | None | Multi-step execution |
| --var | key=value (repeatable) | None | Inject variables into chain |
| --body-only | Flag | Off | Print only response body |
| --quiet | Flag | Off | Print only status + ok + elapsed |

### Environment Management Flags

| Flag | Notes |
|---|---|
| --env-set NAME | Create/update preset (combine with --base-url, --auth, --token) |
| --env-list | List all saved presets |
| --env-del NAME | Delete a preset |
| --base-url URL | Base URL for the preset |

---

## Chain File Format

Chain files are JSON with this structure:

    {
      "variables": {"user_id": "123"},
      "requests": [
        {
          "method": "POST",
          "url": "/auth/login",
          "body": {"username": "user", "password": "secret"},
          "capture": {"access_token": "data.token"},
          "assert": [{"type": "status", "expect": 200}],
          "stop_on_fail": true
        },
        {
          "method": "GET",
          "url": "/users/{{user_id}}/profile",
          "headers": {"Authorization": "Bearer {{access_token}}"},
          "capture": {"name": "data.name"},
          "assert": [
            {"type": "status", "expect": 200},
            {"type": "json_path", "path": "data.id", "expect": 123}
          ]
        }
      ]
    }

### Capture Syntax

capture: {"var_name": "json.path.to.value"} - extracts from response body using dot-notation.
Supports array indices: data.items[0].id

### Assertion Types

| Type | Fields | Example |
|---|---|---|
| status | expect | {"type": "status", "expect": 200} |
| json_path | path + expect/contains/exists | {"type": "json_path", "path": "data.id", "expect": 42} |
| elapsed_ms | max | {"type": "elapsed_ms", "max": 500} |
| body_contains | value | {"type": "body_contains", "value": "success"} |

---

## Execution Protocol

### Step 1 - Identify intent
- Determine: single request vs chain vs env management.
- For single requests: extract method, URL, headers, body, auth from the user's message.
- If the user references a service they've used before, check if an env preset exists (--env-list).

### Step 2 - Resolve environment
- If URL is relative (no http), an --env with base_url is required.
- If the user hasn't set up an env for this service, ask: "Want me to save an env preset for this?"
- Never hardcode tokens in commands if an env preset can store them.

### Step 3 - Execute
- Single request: fire the command with resolved flags.
- Chain: write the chain JSON to /tmp/sable_chain_<timestamp>.json first, then execute with --chain.
- Always use uv run - never python3 directly (httpx won't be available).

### Step 4 - Report
Parse the JSON output and report:
- Status code + ok/fail
- Elapsed time (ms)
- Key body fields (don't dump entire response unless the user asks)
- Captured variables (if chain mode)
- Assertion results (if present) - highlight failures prominently

---

## Output Modes

| User's intent | Flag | What they see |
|---|---|---|
| "Just show me the response" | --body-only | Clean JSON body |
| "Is it up?" /health check | --quiet | {"status": 200, "ok": true, "elapsed_ms": 45.2} |
| "Full details" /debugging | (default) | Everything: status, headers, body, timing |

---

## Failure Handling

| Failure | Symptom | Action |
|---|---|---|
| httpx not available | vv run fails with import error | Report: Run uv run --with httpx or check uv installation |
| Timeout | "error": "timeout" in output | Report timeout + suggest increasing --timeout |
| Connection refused | "error": "connection_failed" | Report: server may be down or URL is wrong |
| Env not found | "error": "Environment 'x' not found" | List available envs, ask which to use |
| Relative URL, no env | "error": "Relative URL requires --env" | Ask for full URL or set up env preset |
| Chain file missing | "error": "Chain file not found" | Write the chain file first, then execute |
| Assertion failure | "passed": false in assertions | Report which assertion failed + actual vs expected |

---

## Examples

### Quick GET

User: "Hit https://httpbin.org/get and show me what comes back"

    uv run PROJECT_ROOT/skills/http_client/scripts/api_request.py GET https://httpbin.org/get --body-only

### POST with JSON body

User: "Send a POST to https://api.example.com/users with name Alice and email alice@example.com"

    uv run PROJECT_ROOT/skills/http_client/scripts/api_request.py POST https://api.example.com/users -b '{"name": "Alice", "email": "alice@example.com"}'

### With environment preset

Setup (one-time):

    uv run PROJECT_ROOT/skills/http_client/scripts/api_request.py --env-set github --base-url https://api.github.com --auth bearer --token ghp_xxxxx

Usage:

    uv run PROJECT_ROOT/skills/http_client/scripts/api_request.py GET /user --env github

### Auth login then protected request (chain)

User: "Log into my API at localhost:8000, grab the token, then fetch /me/profile"

1. Write chain file to /tmp/sable_chain.json
2. Execute:

    uv run PROJECT_ROOT/skills/http_client/scripts/api_request.py --chain /tmp/sable_chain.json

---

## Global Rules

1. Always use uv run - never bare python3. The script depends on httpx via PEP 723.
2. Prefer env presets for repeated services. Don't make the user repeat base URLs and tokens.
3. Never dump full response headers unless the user explicitly asks for debugging. Status + body is the default report.
4. Chain files go in /tmp/ - they're ephemeral. Never clutter project dirs.
5. One logical operation per turn. Don't batch unrelated requests.
6. Report assertion failures prominently. If a test chain fails, that's the headline.
7. For simple "is this URL alive?" checks, prefer xh GET url over this script. Save the heavy tool for structured work.
