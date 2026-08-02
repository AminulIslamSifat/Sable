# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "httpx>=0.27",
# ]
# ///
"""
api_request.py — Structured HTTP client for Sable's HTTP skill.

Features:
  - Single or chained requests with variable capture
  - Environment presets (~/.config/sable-http/env.json)
  - Assertions (status, json path, response time)
  - Structured JSON output for agent parsing

Usage:
  # Single request
  uv run api_request.py GET https://api.example.com/users

  # With env preset
  uv run api_request.py GET /users --env github

  # With auth
  uv run api_request.py GET /user --env github --auth bearer --token ghp_xxx

  # Chained requests (JSON file)
  uv run api_request.py --chain requests.json

  # Manage environments
  uv run api_request.py --env-set github --base-url https://api.github.com
  uv run api_request.py --env-list
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

ENV_DIR = Path.home() / ".config" / "sable-http"
ENV_FILE = ENV_DIR / "env.json"
DEFAULT_TIMEOUT = 30.0


# ─── Environment Management ───────────────────────────────────────────────────

def load_envs() -> dict:
    if ENV_FILE.exists():
        return json.loads(ENV_FILE.read_text())
    return {}


def save_envs(envs: dict) -> None:
    ENV_DIR.mkdir(parents=True, exist_ok=True)
    ENV_FILE.write_text(json.dumps(envs, indent=2))


def resolve_env(name: str | None) -> dict:
    if not name:
        return {}
    envs = load_envs()
    if name not in envs:
        print(json.dumps({"error": f"Environment '{name}' not found. Available: {list(envs.keys())}"}))
        sys.exit(1)
    return envs[name]


# ─── JSONPath-lite Resolver ───────────────────────────────────────────────────

def resolve_path(data: Any, path: str) -> Any:
    """
    Resolve a dot-notation path like 'data.items[0].id' against a JSON structure.
    Supports: dict keys, list indices [n], and nested combinations.
    """
    if not path or path == "$":
        return data

    # Strip leading $. or $
    path = path.lstrip("$").lstrip(".")

    parts: list[str] = []
    current = ""
    i = 0
    while i < len(path):
        ch = path[i]
        if ch == ".":
            if current:
                parts.append(current)
                current = ""
        elif ch == "[":
            if current:
                parts.append(current)
                current = ""
            j = path.index("]", i)
            parts.append(path[i + 1 : j])
            i = j
        else:
            current += ch
        i += 1
    if current:
        parts.append(current)

    result = data
    for part in parts:
        if isinstance(result, dict):
            if part not in result:
                return None
            result = result[part]
        elif isinstance(result, list):
            try:
                result = result[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return result


# ─── Variable Interpolation ───────────────────────────────────────────────────

def interpolate(text: str, variables: dict) -> str:
    """Replace {{var_name}} with values from the variables dict."""
    for key, val in variables.items():
        text = text.replace("{{" + key + "}}", str(val))
    return text


# ─── Request Execution ────────────────────────────────────────────────────────

def build_headers(auth_type: str | None, token: str | None, headers: dict | None) -> dict:
    h = dict(headers) if headers else {}
    if auth_type == "bearer" and token:
        h["Authorization"] = f"Bearer {token}"
    elif auth_type == "basic" and token:
        h["Authorization"] = f"Basic {token}"
    elif auth_type == "apikey" and token:
        h["X-API-Key"] = token
    return h


def execute_request(
    method: str,
    url: str,
    headers: dict | None = None,
    params: dict | None = None,
    body: Any = None,
    timeout: float = DEFAULT_TIMEOUT,
    follow_redirects: bool = True,
) -> dict:
    """Execute a single HTTP request and return structured result."""
    start = time.perf_counter()
    try:
        with httpx.Client(timeout=timeout, follow_redirects=follow_redirects) as client:
            kwargs: dict[str, Any] = {"headers": headers, "params": params}
            if body is not None:
                if isinstance(body, (dict, list)):
                    kwargs["json"] = body
                else:
                    kwargs["content"] = str(body)
                    if "Content-Type" not in (headers or {}):
                        kwargs["headers"] = {**(headers or {}), "Content-Type": "text/plain"}

            resp = client.request(method.upper(), url, **kwargs)
            elapsed_ms = (time.perf_counter() - start) * 1000

            # Try to parse JSON body
            try:
                resp_body = resp.json()
            except (json.JSONDecodeError, ValueError):
                resp_body = resp.text

            return {
                "status": resp.status_code,
                "status_text": resp.reason_phrase,
                "elapsed_ms": round(elapsed_ms, 1),
                "headers": dict(resp.headers),
                "body": resp_body,
                "url": str(resp.url),
                "ok": resp.is_success,
            }
    except httpx.TimeoutException:
        elapsed_ms = (time.perf_counter() - start) * 1000
        return {"error": "timeout", "elapsed_ms": round(elapsed_ms, 1), "url": url, "ok": False}
    except httpx.ConnectError as e:
        return {"error": f"connection_failed: {e}", "url": url, "ok": False}
    except Exception as e:
        return {"error": str(e), "url": url, "ok": False}


# ─── Assertions ───────────────────────────────────────────────────────────────

def run_assertions(assertions: list[dict], result: dict) -> list[dict]:
    """
    Run assertions against a response result.
    Assertion format:
      {"type": "status", "expect": 200}
      {"type": "json_path", "path": "data.id", "expect": 42}
      {"type": "json_path", "path": "data.name", "contains": "user"}
      {"type": "elapsed_ms", "max": 500}
      {"type": "body_contains", "value": "success"}
    """
    results = []
    for a in assertions:
        a_type = a.get("type", "")
        passed = False
        detail = ""

        if a_type == "status":
            passed = result.get("status") == a["expect"]
            detail = f"status={result.get('status')} expected={a['expect']}"

        elif a_type == "json_path":
            actual = resolve_path(result.get("body"), a["path"])
            if "expect" in a:
                passed = actual == a["expect"]
                detail = f"{a['path']}={actual} expected={a['expect']}"
            elif "contains" in a:
                passed = a["contains"] in str(actual)
                detail = f"{a['path']}={actual} contains={a['contains']}"
            elif "exists" in a:
                passed = (actual is not None) == a["exists"]
                detail = f"{a['path']} exists={actual is not None}"

        elif a_type == "elapsed_ms":
            elapsed = result.get("elapsed_ms", 99999)
            passed = elapsed <= a["max"]
            detail = f"elapsed={elapsed}ms max={a['max']}ms"

        elif a_type == "body_contains":
            body_str = json.dumps(result.get("body", ""))
            passed = a["value"] in body_str
            detail = f"body contains '{a['value']}': {passed}"

        results.append({"assertion": a, "passed": passed, "detail": detail})
    return results


# ─── Chain Execution ──────────────────────────────────────────────────────────

def execute_chain(chain: list[dict], env: dict, variables: dict) -> list[dict]:
    """
    Execute a chain of requests. Each step:
    {
      "method": "POST",
      "url": "/auth/login",
      "body": {"user": "x", "pass": "y"},
      "capture": {"token": "data.access_token"},
      "assert": [{"type": "status", "expect": 200}]
    }
    """
    results = []
    for i, step in enumerate(chain):
        # Interpolate URL and body with current variables
        url = interpolate(step.get("url", ""), variables)
        if not url.startswith("http"):
            base = env.get("base_url", "")
            url = base.rstrip("/") + "/" + url.lstrip("/")

        body = step.get("body")
        if body and isinstance(body, str):
            body = interpolate(body, variables)
        elif body and isinstance(body, dict):
            body = json.loads(interpolate(json.dumps(body), variables))

        headers = step.get("headers", {})
        headers = {k: interpolate(v, variables) for k, v in headers.items()}

        # Auth from env or step
        auth_type = step.get("auth", env.get("auth"))
        token = step.get("token", env.get("token"))
        if auth_type and token:
            token = interpolate(token, variables)
            headers = build_headers(auth_type, token, headers)

        params = step.get("params")
        if params and isinstance(params, dict):
            params = {k: interpolate(str(v), variables) for k, v in params.items()}

        result = execute_request(
            method=step.get("method", "GET"),
            url=url,
            headers=headers,
            params=params,
            body=body,
            timeout=step.get("timeout", DEFAULT_TIMEOUT),
        )
        result["step"] = i + 1

        # Captures
        captures = step.get("capture", {})
        captured = {}
        for var_name, path in captures.items():
            val = resolve_path(result.get("body"), path)
            if val is not None:
                variables[var_name] = val
                captured[var_name] = val
        result["captured"] = captured

        # Assertions
        if "assert" in step:
            result["assertions"] = run_assertions(step["assert"], result)

        results.append(result)

        # Stop chain on failure if configured
        if not result.get("ok") and step.get("stop_on_fail", True):
            break

    return results


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Structured HTTP client for Sable")
    parser.add_argument("method", nargs="?", help="HTTP method (GET, POST, etc.)")
    parser.add_argument("url", nargs="?", help="URL or path (with --env)")
    parser.add_argument("--env", help="Environment preset name")
    parser.add_argument("--auth", choices=["bearer", "basic", "apikey"], help="Auth type")
    parser.add_argument("--token", help="Auth token/key")
    parser.add_argument("-H", "--header", action="append", help="Header as Key:Value")
    parser.add_argument("-p", "--param", action="append", help="Query param as key=value")
    parser.add_argument("-b", "--body", help="JSON body (string or @file.json)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--no-redirect", action="store_true")
    parser.add_argument("--chain", help="Path to chain JSON file")
    parser.add_argument("--var", action="append", help="Variable as key=value")

    # Env management
    parser.add_argument("--env-set", help="Create/update environment preset")
    parser.add_argument("--base-url", help="Base URL for --env-set")
    parser.add_argument("--env-list", action="store_true", help="List environments")
    parser.add_argument("--env-del", help="Delete environment preset")

    # Output control
    parser.add_argument("--body-only", action="store_true", help="Print only response body")
    parser.add_argument("--quiet", action="store_true", help="Minimal output")

    args = parser.parse_args()

    # ── Env management mode ──
    if args.env_list:
        envs = load_envs()
        print(json.dumps(envs, indent=2))
        return

    if args.env_set:
        envs = load_envs()
        preset = envs.get(args.env_set, {})
        if args.base_url:
            preset["base_url"] = args.base_url
        if args.auth:
            preset["auth"] = args.auth
        if args.token:
            preset["token"] = args.token
        envs[args.env_set] = preset
        save_envs(envs)
        print(json.dumps({"saved": args.env_set, "preset": preset}))
        return

    if args.env_del:
        envs = load_envs()
        if args.env_del in envs:
            del envs[args.env_del]
            save_envs(envs)
            print(json.dumps({"deleted": args.env_del}))
        else:
            print(json.dumps({"error": f"'{args.env_del}' not found"}))
        return

    # ── Chain mode ──
    if args.chain:
        chain_path = Path(args.chain)
        if not chain_path.exists():
            print(json.dumps({"error": f"Chain file not found: {args.chain}"}))
            sys.exit(1)
        chain_data = json.loads(chain_path.read_text())
        env = resolve_env(args.env)
        variables = dict(chain_data.get("variables", {}))
        if args.var:
            for v in args.var:
                k, _, val = v.partition("=")
                variables[k] = val
        results = execute_chain(chain_data["requests"], env, variables)
        print(json.dumps({"chain_results": results, "variables": variables}, indent=2))
        return

    # ── Single request mode ──
    if not args.method or not args.url:
        parser.print_help()
        sys.exit(1)

    env = resolve_env(args.env)
    url = args.url
    if not url.startswith("http"):
        base = env.get("base_url", "")
        if not base:
            print(json.dumps({"error": "Relative URL requires --env with base_url set"}))
            sys.exit(1)
        url = base.rstrip("/") + "/" + url.lstrip("/")

    # Headers
    headers = {}
    if args.header:
        for h in args.header:
            key, _, val = h.partition(":")
            headers[key.strip()] = val.strip()

    # Auth
    auth_type = args.auth or env.get("auth")
    token = args.token or env.get("token")
    if auth_type and token:
        headers = build_headers(auth_type, token, headers)

    # Params
    params = {}
    if args.param:
        for p in args.param:
            k, _, v = p.partition("=")
            params[k] = v

    # Body
    body = None
    if args.body:
        if args.body.startswith("@"):
            body_path = Path(args.body[1:])
            if body_path.exists():
                body = json.loads(body_path.read_text())
            else:
                print(json.dumps({"error": f"Body file not found: {args.body[1:]}"}))
                sys.exit(1)
        else:
            try:
                body = json.loads(args.body)
            except json.JSONDecodeError:
                body = args.body

    result = execute_request(
        method=args.method,
        url=url,
        headers=headers if headers else None,
        params=params if params else None,
        body=body,
        timeout=args.timeout,
        follow_redirects=not args.no_redirect,
    )

    if args.body_only:
        print(json.dumps(result.get("body"), indent=2))
    elif args.quiet:
        print(json.dumps({"status": result.get("status"), "ok": result.get("ok"), "elapsed_ms": result.get("elapsed_ms")}))
    else:
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
