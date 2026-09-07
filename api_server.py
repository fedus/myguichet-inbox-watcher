"""Read-only REST API and minimal dashboard for the document watcher."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import ConfigProvider, EnvConfigProvider
from outputs import available_output_names
from runtime_state import RuntimeState, runtime_state as default_runtime_state
from sources import available_source_names
from sources.base import SourceAccountConfig
from watcher_core import load_state

try:
    from fastapi import FastAPI, HTTPException, Query
    from fastapi.responses import HTMLResponse
except ImportError as error:  # pragma: no cover - exercised by real startup
    raise RuntimeError(
        "FastAPI is not installed. Run pip install -r requirements.txt."
    ) from error


SENSITIVE_SETTING_PARTS = (
    "access",
    "credential",
    "key",
    "password",
    "refresh",
    "secret",
    "token",
    "username",
)
DEFAULT_API_HOST = "0.0.0.0"
DEFAULT_API_PORT = 8000
OPENAPI_TAGS = [
    {
        "name": "runtime",
        "description": "Health, active polls, pending inputs, and events.",
    },
    {
        "name": "configuration",
        "description": "Configured accounts and loaded plugin names.",
    },
    {
        "name": "documents",
        "description": "Downloaded files visible through folder outputs.",
    },
]


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.environ.get(name, str(default)).strip().lower()
    return value in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    value = os.environ.get(name, str(default)).strip()
    try:
        result = int(value)
    except ValueError as error:
        raise RuntimeError(f"{name} must be an integer.") from error
    if result <= 0:
        raise RuntimeError(f"{name} must be positive.")
    return result


def is_sensitive_setting(name: str) -> bool:
    normalized = name.lower()
    return any(part in normalized for part in SENSITIVE_SETTING_PARTS)


def sanitize_settings(settings: dict[str, Any] | Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in dict(settings).items():
        result[str(key)] = "***" if is_sensitive_setting(str(key)) else value
    return result


def account_user(account: SourceAccountConfig) -> str:
    suffix = f"_{account.source}"
    if account.name.endswith(suffix):
        return account.name[: -len(suffix)]
    return account.name


def state_summary(account: SourceAccountConfig) -> dict[str, Any]:
    if not account.state_file.exists():
        return {
            "exists": False,
            "seen_count": 0,
            "last_run": None,
            "error": None,
        }
    try:
        state = load_state(account)
    except Exception as error:
        return {
            "exists": True,
            "seen_count": None,
            "last_run": None,
            "error": str(error),
        }
    return {
        "exists": True,
        "seen_count": len(state.get("seen_ids", [])),
        "last_run": state.get("last_run"),
        "error": None,
    }


def account_payload(account: SourceAccountConfig) -> dict[str, Any]:
    return {
        "name": account.name,
        "user": account_user(account),
        "source": account.source,
        "runtime_dir": str(account.runtime_dir),
        "state_file": str(account.state_file),
        "maximum_document_mb": account.maximum_document_mb,
        "state": state_summary(account),
        "source_settings": sanitize_settings(dict(account.source_settings)),
        "outputs": [
            {
                "name": output.name,
                "type": output.type,
                "settings": sanitize_settings(dict(output.settings)),
            }
            for output in account.output_configs
        ],
    }


def folder_output_directories(account: SourceAccountConfig) -> list[Path]:
    directories: list[Path] = []
    for output in account.output_configs:
        if output.type != "folder":
            continue
        raw_directory = output.settings.get("directory")
        if raw_directory is None or not str(raw_directory).strip():
            path = Path("downloads") / account.name
        else:
            path = Path(str(raw_directory)).expanduser()
        if not path.is_absolute():
            path = Path(__file__).resolve().parent / path
        directories.append(path)
    return directories


def document_payload(path: Path, account: SourceAccountConfig) -> dict[str, Any]:
    stat = path.stat()
    return {
        "account": account.name,
        "source": account.source,
        "filename": path.name,
        "path": str(path),
        "size_bytes": stat.st_size,
        "modified_at": datetime_from_ns(stat.st_mtime_ns),
        "modified_ns": stat.st_mtime_ns,
    }


def datetime_from_ns(value: int) -> str:
    seconds = value / 1_000_000_000
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def list_downloaded_documents(
    accounts: list[SourceAccountConfig],
    account_name: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    selected = [
        account
        for account in accounts
        if account_name is None or account.name == account_name
    ]
    for account in selected:
        for directory in folder_output_directories(account):
            if not directory.exists():
                continue
            for path in directory.iterdir():
                if path.is_file() and not path.name.startswith("."):
                    documents.append(document_payload(path, account))
    documents.sort(key=lambda item: int(item["modified_ns"]), reverse=True)
    return documents[:limit]


def dashboard_html() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Document Watcher</title>
  <style>
    :root { color-scheme: light; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f6f7f9; color: #172033; }
    body { margin: 0; }
    header { background: #1d4f8f; color: #fff; padding: 20px 28px; }
    h1 { font-size: 22px; margin: 0 0 4px; font-weight: 700; letter-spacing: 0; }
    header p { margin: 0; opacity: .85; }
    main { max-width: 1180px; margin: 0 auto; padding: 24px; }
    .toolbar { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 18px; }
    .toolbar a, button { border: 1px solid #c9d3e1; background: #fff; color: #172033; border-radius: 6px; padding: 8px 10px; text-decoration: none; cursor: pointer; }
    .grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; margin-bottom: 18px; }
    .panel { background: #fff; border: 1px solid #dde3eb; border-radius: 8px; padding: 16px; }
    .metric { font-size: 26px; font-weight: 700; }
    .label { color: #5c6a7d; font-size: 13px; }
    .columns { display: grid; grid-template-columns: 1.2fr .8fr; gap: 18px; }
    table { width: 100%; border-collapse: collapse; font-size: 14px; }
    th, td { text-align: left; padding: 10px 8px; border-bottom: 1px solid #edf0f4; vertical-align: top; }
    th { color: #5c6a7d; font-weight: 600; }
    code { background: #eef2f7; border-radius: 4px; padding: 2px 5px; }
    .status { display: inline-block; border-radius: 999px; padding: 3px 8px; font-size: 12px; font-weight: 600; background: #edf0f4; }
    .ok { background: #dff5e8; color: #146b39; }
    .error { background: #fde2e2; color: #9b1c1c; }
    .running, .waiting, .queued { background: #e3efff; color: #1d4f8f; }
    .list { display: grid; gap: 10px; }
    .event { border-bottom: 1px solid #edf0f4; padding-bottom: 10px; }
    .event:last-child { border-bottom: 0; padding-bottom: 0; }
    .muted { color: #5c6a7d; }
    @media (max-width: 900px) { .grid, .columns { grid-template-columns: 1fr; } main { padding: 16px; } }
  </style>
</head>
<body>
  <header>
    <h1>Document Watcher</h1>
    <p>Read-only runtime dashboard</p>
  </header>
  <main>
    <div class="toolbar">
      <div class="muted" id="updated">Loading...</div>
      <div><a href="/docs">Swagger</a> <button onclick="loadData()">Refresh</button></div>
    </div>
    <section class="grid">
      <div class="panel"><div class="metric" id="accountCount">0</div><div class="label">Accounts</div></div>
      <div class="panel"><div class="metric" id="activeCount">0</div><div class="label">Active Polls</div></div>
      <div class="panel"><div class="metric" id="inputCount">0</div><div class="label">Pending Inputs</div></div>
      <div class="panel"><div class="metric" id="documentCount">0</div><div class="label">Recent Documents</div></div>
    </section>
    <section class="columns">
      <div class="panel"><h2>Accounts</h2><table><thead><tr><th>Account</th><th>Source</th><th>Outputs</th><th>Last Run</th><th>Seen</th></tr></thead><tbody id="accounts"></tbody></table></div>
      <div class="panel"><h2>Runtime</h2><div id="runtime" class="list"></div></div>
    </section>
    <section class="panel" style="margin-top:18px"><h2>Recent Documents</h2><table><thead><tr><th>Account</th><th>File</th><th>Size</th></tr></thead><tbody id="documents"></tbody></table></section>
    <section class="panel" style="margin-top:18px"><h2>Recent Events</h2><div id="events" class="list"></div></section>
  </main>
  <script>
    const $ = id => document.getElementById(id);
    const cls = s => "status " + (s || "").replace(/[^a-z0-9_-]/gi, "");
    const text = v => v === null || v === undefined || v === "" ? "n/a" : v;
    const escapeMap = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
    const html = v => String(text(v)).replace(/[&<>"']/g, c => escapeMap[c]);
    const size = n => n < 1024 ? `${n} B` : n < 1048576 ? `${(n/1024).toFixed(1)} KB` : `${(n/1048576).toFixed(1)} MB`;
    async function fetchJson(url) {
      const response = await fetch(url);
      if (!response.ok) {
        let detail = `${response.status} ${response.statusText}`;
        try {
          const body = await response.json();
          detail = body.detail || detail;
        } catch (_) {}
        throw new Error(`${url}: ${detail}`);
      }
      return response.json();
    }
    async function loadData() {
      try {
        const [accounts, status, documents] = await Promise.all([
          fetchJson("/api/accounts"),
          fetchJson("/api/status"),
          fetchJson("/api/documents?limit=20"),
        ]);
        $("accountCount").textContent = accounts.length;
        $("activeCount").textContent = status.active_polls.length;
        $("inputCount").textContent = status.pending_inputs.length;
        $("documentCount").textContent = documents.length;
        $("updated").textContent = `Updated ${new Date().toLocaleTimeString()}`;
        $("accounts").innerHTML = accounts.length ? accounts.map(a => `<tr><td><code>${html(a.name)}</code></td><td>${html(a.source)}</td><td>${a.outputs.map(o => `${html(o.name)}:${html(o.type)}`).join("<br>")}</td><td>${html(a.state.last_run)}</td><td>${html(a.state.seen_count)}</td></tr>`).join("") : '<tr><td colspan="5" class="muted">No configured accounts returned by the API.</td></tr>';
        const runtimeItems = [
          ...status.active_polls.map(p => `<div><span class="${cls("running")}">running</span> <code>${html(p.account)}</code><div class="muted">since ${html(p.started_at)}</div></div>`),
          ...status.pending_inputs.map(i => `<div><span class="${cls("waiting")}">waiting</span> <code>${html(i.account)}</code><div class="muted">${html(i.kind)}: ${i.fields.map(html).join(", ")}</div></div>`),
        ];
        $("runtime").innerHTML = runtimeItems.length ? runtimeItems.join("") : '<div class="muted">No active polls or pending inputs.</div>';
        $("documents").innerHTML = documents.length ? documents.map(d => `<tr><td><code>${html(d.account)}</code></td><td>${html(d.filename)}</td><td>${html(size(d.size_bytes))}</td></tr>`).join("") : '<tr><td colspan="3" class="muted">No folder-output documents found.</td></tr>';
        $("events").innerHTML = status.recent_events.length ? status.recent_events.slice().reverse().slice(0, 20).map(e => `<div class="event"><span class="${cls(e.status)}">${html(e.status)}</span> <strong>${html(e.event)}</strong> ${e.account ? `<code>${html(e.account)}</code>` : ""}<div class="muted">${html(e.timestamp)} ${html(e.message || "")}</div></div>`).join("") : '<div class="muted">No runtime events yet.</div>';
      } catch (error) {
        $("updated").textContent = `API error: ${error.message}`;
        $("runtime").innerHTML = `<div class="error">${html(error.message)}</div>`;
      }
    }
    loadData();
    setInterval(loadData, 10000);
  </script>
</body>
</html>"""


def create_app(
    config_provider: ConfigProvider | None = None,
    runtime_state: RuntimeState | None = None,
) -> FastAPI:
    provider = config_provider or EnvConfigProvider()
    state = runtime_state or default_runtime_state
    provider.load()
    app = FastAPI(
        title="Document Watcher API",
        version="0.1.0",
        description="Read-only account, plugin, runtime, and downloaded-document views.",
        openapi_tags=OPENAPI_TAGS,
    )

    @app.middleware("http")
    async def no_store_observability_responses(request: Any, call_next: Any) -> Any:
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def accounts() -> list[SourceAccountConfig]:
        try:
            return provider.get_source_accounts()
        except Exception as error:
            raise HTTPException(status_code=500, detail=str(error)) from error

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def dashboard() -> str:
        return dashboard_html()

    @app.get("/api/health", tags=["runtime"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/plugins", tags=["configuration"])
    def plugins() -> dict[str, list[str]]:
        return {
            "sources": sorted(available_source_names()),
            "outputs": sorted(available_output_names()),
        }

    @app.get("/api/accounts", tags=["configuration"])
    def configured_accounts() -> list[dict[str, Any]]:
        return [account_payload(account) for account in accounts()]

    @app.get("/api/accounts/{account_name}", tags=["configuration"])
    def configured_account(account_name: str) -> dict[str, Any]:
        for account in accounts():
            if account.name == account_name:
                return account_payload(account)
        raise HTTPException(status_code=404, detail="Unknown account.")

    @app.get("/api/status", tags=["runtime"])
    def status() -> dict[str, Any]:
        return state.snapshot()

    @app.get("/api/documents", tags=["documents"])
    def documents(
        account: str | None = None,
        limit: int = Query(default=50, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        return list_downloaded_documents(accounts(), account, limit)

    return app


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the read-only watcher API.")
    parser.add_argument(
        "--host",
        default=os.environ.get("DOCUMENT_API_HOST", DEFAULT_API_HOST),
        help="Bind host. Defaults to DOCUMENT_API_HOST or 0.0.0.0.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=_int_env("DOCUMENT_API_PORT", DEFAULT_API_PORT),
        help="Bind port. Defaults to DOCUMENT_API_PORT or 8000.",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        default=_bool_env("DOCUMENT_API_RELOAD", False),
        help="Enable Uvicorn reload for local development.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        import uvicorn
    except ImportError as error:  # pragma: no cover - exercised by real startup
        raise RuntimeError(
            "Uvicorn is not installed. Run pip install -r requirements.txt."
        ) from error
    uvicorn.run(
        "api_server:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
