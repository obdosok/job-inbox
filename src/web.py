from __future__ import annotations

import argparse
import json
import mimetypes
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import advise, candidate, providers
from .db import JobDatabase
from .service import assess_job, ingest_jobs


ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = ROOT / "web"
MAX_BODY = 2 * 1024 * 1024


class JobInboxHandler(BaseHTTPRequestHandler):
    server_version = "JobInbox/0.1"

    @property
    def db(self) -> JobDatabase:
        return self.server.database  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}")

    def _json(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _payload(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_BODY:
            raise ValueError("Request body must be between 1 byte and 2 MB")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _static(self, request_path: str) -> None:
        relative = "index.html" if request_path in {"", "/"} else request_path.lstrip("/")
        target = (WEB_ROOT / relative).resolve()
        if WEB_ROOT.resolve() not in target.parents and target != WEB_ROOT.resolve():
            self._error(403, "Forbidden")
            return
        if not target.is_file():
            target = WEB_ROOT / "index.html"
        body = target.read_bytes()
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8" if content_type.startswith("text/") or content_type == "application/javascript" else content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/jobs":
                inbox_filter = parse_qs(parsed.query).get("filter", ["today"])[0]
                self._json({"jobs": self.db.list_jobs(inbox_filter), "counts": self.db.counts()})
            elif parsed.path == "/api/summary":
                self._json(self.db.summary())
            elif parsed.path == "/api/settings":
                self._json({
                    "llm_enabled": providers.has_key(),
                    "provider": providers.configured_provider() or "none",
                    "llm_model": os.environ.get("JOB_INBOX_LLM_MODEL") or providers.default_model(),
                    "advisor_mode": advise.mode() if advise.is_active() else "off",
                    "profile_ready": candidate.is_available(),
                })
            elif parsed.path.startswith("/api/jobs/"):
                self._json(self.db.get_job(parsed.path.rsplit("/", 1)[-1]))
            elif parsed.path.startswith("/api/"):
                self._error(404, "API route not found")
            else:
                self._static(parsed.path)
        except KeyError:
            self._error(404, "Job not found")
        except (ValueError, json.JSONDecodeError) as error:
            self._error(400, str(error))
        except Exception as error:
            self._error(500, f"Request failed: {error}")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            payload = self._payload()
            if parsed.path.startswith("/api/ingest/"):
                adapter = parsed.path.rsplit("/", 1)[-1]
                jobs, report = ingest_jobs(self.db, adapter, payload, str(payload.get("track", "B")))
                self._json({"jobs": jobs, "count": len(jobs), "funnel": report.summary()}, HTTPStatus.CREATED)
            elif parsed.path.startswith("/api/jobs/") and parsed.path.endswith("/assess"):
                self._json(assess_job(self.db, parsed.path.split("/")[3]))
            elif parsed.path.startswith("/api/jobs/") and parsed.path.endswith("/prepare"):
                job_id = parsed.path.split("/")[3]
                self._json(self.db.prepare_application(job_id))
            elif parsed.path == "/api/outcomes":
                self._json(self.db.add_outcome(payload), HTTPStatus.CREATED)
            else:
                self._error(404, "API route not found")
        except KeyError:
            self._error(404, "Job not found")
        except (ValueError, json.JSONDecodeError) as error:
            self._error(400, str(error))
        except Exception as error:
            self._error(500, f"Request failed: {error}")


def serve(host: str = "127.0.0.1", port: int = 8765, db_path: str | None = None) -> None:
    server = ThreadingHTTPServer((host, port), JobInboxHandler)
    server.database = JobDatabase(db_path)  # type: ignore[attr-defined]
    server.database.bootstrap_existing()  # type: ignore[attr-defined]
    print(f"Job Inbox running at http://{host}:{port}")
    print("Local only. Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local Job Inbox web app")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db")
    args = parser.parse_args()
    serve(args.host, args.port, args.db)


if __name__ == "__main__":
    main()
