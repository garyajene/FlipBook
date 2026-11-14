"""FlipBook MVP: private admin, PDF uploads and stable public flipbook URLs."""
import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import sqlite3
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("DATA_DIR", "/data" if Path("/data").is_dir() else str(ROOT / "data")))
DATA.mkdir(parents=True, exist_ok=True)
PDFS = DATA / "pdfs"
PDFS.mkdir(exist_ok=True)
DB = DATA / "flipbooks.sqlite3"
ELEMENTS = DATA / "elements"
ELEMENTS.mkdir(exist_ok=True)
PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
MAX_BYTES = 40 * 1024 * 1024
with sqlite3.connect(DB) as db:
    db.execute("CREATE TABLE IF NOT EXISTS books (id TEXT PRIMARY KEY, title TEXT NOT NULL, filename TEXT NOT NULL, created TEXT DEFAULT CURRENT_TIMESTAMP)")

class Handler(BaseHTTPRequestHandler):
    def send(self, status, content, mime="text/html; charset=utf-8", headers=None):
        content = content.encode("utf-8") if isinstance(content, str) else content
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(content)

    def authorized(self):
        if not PASSWORD:
            self.send(503, "Set ADMIN_PASSWORD in Railway Variables before using the admin dashboard.", "text/plain; charset=utf-8")
            return False
        raw = self.headers.get("Authorization", "")
        try:
            kind, encoded = raw.split(" ", 1)
            userpass = base64.b64decode(encoded, validate=True).decode("utf-8")
        except (ValueError, UnicodeError):
            kind, userpass = "", ""
        good = kind.lower() == "basic" and hmac.compare_digest(userpass, "admin:" + PASSWORD)
        if not good:
            self.send(401, "Administrator login required", "text/plain; charset=utf-8",
                      {"WWW-Authenticate": 'Basic realm="FlipBook Admin"'})
        return good

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/admin"):
            if not self.authorized():
                return
            with sqlite3.connect(DB) as db:
                books = db.execute("SELECT id,title,created FROM books ORDER BY created DESC").fetchall()
            rows = "".join(
                '<tr><td>' + html.escape(title) + '</td><td><a target="_blank" href="/b/' + bid +
                '">Open flipbook</a> · <a href="/edit/' + bid + '">Edit</a></td><td><button type="button" onclick="navigator.clipboard.writeText(location.origin+\'/b/' + bid + '\')">Copy link</button></td><td>' +
                html.escape(created) + '</td></tr>' for bid, title, created in books)
            page = """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>FlipBook Admin</title>
<style>body{font:16px system-ui;background:#11131b;color:#f7f7fb;margin:0;padding:32px}main{max-width:1000px;margin:auto}h1{font-size:28px}section{background:#1c202b;padding:24px;border-radius:14px;margin:24px 0}input,button{padding:12px;border-radius:8px;border:1px solid #515668;font:inherit}input{background:#fff;color:#111;max-width:100%}button{background:#c9d6ff;color:#111;cursor:pointer}table{width:100%;border-collapse:collapse}td,th{padding:12px;border-bottom:1px solid #3b3f4b;text-align:left}a{color:#b7caff}.note{color:#bfc3d0}</style>
<main><h1>FlipBook Dashboard</h1><p class="note">Private administrator area · Public books have permanent links.</p>
<section><h2>Upload a PDF</h2><form method="post" action="/api/upload" enctype="multipart/form-data">
<p><label>Title <input name="title" maxlength="120" required placeholder="My flipbook"></label></p>
<p><label>PDF file <input name="pdf" type="file" accept="application/pdf,.pdf" required></label></p>
<button type="submit">Create flipbook</button></form><p class="note">Maximum PDF size: 40 MB.</p></section>
<section><h2>Your flipbooks</h2><div style="overflow:auto"><table><thead><tr><th>Title</th><th>Public link</th><th>Share</th><th>Created</th></tr></thead><tbody>""" + (rows or '<tr><td colspan="4">No flipbooks yet. Upload your first PDF above.</td></tr>') + """</tbody></table></div></section>
<p class="note">For permanent files on Railway, attach a volume at /data before uploading.</p></main></html>"""
            return self.send(200, page)
        if path.startswith(("/edit/", "/api/elements/", "/public/elements/")):
            is_edit = path.startswith("/edit/")
            is_private = is_edit or path.startswith("/api/elements/")
            if is_private and not self.authorized():
                return
            bid = path.rsplit("/", 1)[-1]
            if not re.fullmatch(r"[a-f0-9]{24}", bid):
                return self.send(404, "Not found", "text/plain")
            with sqlite3.connect(DB) as db:
                exists = db.execute("SELECT 1 FROM books WHERE id=?", (bid,)).fetchone()
            if not exists:
                return self.send(404, "Not found", "text/plain")
            if is_edit:
                return self.send(200, (ROOT / "editor.html").read_bytes())
            file = ELEMENTS / (bid + ".json")
            return self.send(200, file.read_bytes() if file.exists() else b"[]", "application/json")
        if path.startswith("/b/"):
            bid = path[3:]
            if not re.fullmatch(r"[a-f0-9]{24}", bid):
                return self.send(404, "Not found", "text/plain")
            with sqlite3.connect(DB) as db:
                row = db.execute("SELECT title FROM books WHERE id=?", (bid,)).fetchone()
            if not row:
                return self.send(404, "Not found", "text/plain")
            page = (ROOT / "viewer.html").read_text(encoding="utf-8")
            # Inject same-origin PDF URL into viewer without allowing external URL input.
            page = page.replace('new URLSearchParams(location.search).get("pdf") || "https://mozilla.github.io/pdf.js/web/compressed.tracemonkey-pldi-09.pdf"', json.dumps("/pdf/" + bid))
            page = page.replace("<title>FlipBook — GrabFold Viewer</title>", "<title>" + html.escape(row[0]) + " — FlipBook</title>")
            return self.send(200, page)
        if path.startswith("/pdf/"):
            bid = path[5:]
            if not re.fullmatch(r"[a-f0-9]{24}", bid):
                return self.send(404, "Not found", "text/plain")
            with sqlite3.connect(DB) as db:
                row = db.execute("SELECT filename FROM books WHERE id=?", (bid,)).fetchone()
            if not row:
                return self.send(404, "Not found", "text/plain")
            file = PDFS / row[0]
            if not file.is_file():
                return self.send(404, "PDF missing", "text/plain")
            return self.send(200, file.read_bytes(), "application/pdf", {"Content-Disposition": "inline"})
        if path == "/viewer.html":
            return self.send(200, (ROOT / "viewer.html").read_bytes())
        return self.send(404, "Not found", "text/plain")

    def do_POST(self):
        path = urlparse(self.path).path
        if path.startswith("/api/elements/"):
            if not self.authorized():
                return
            bid = path.rsplit("/", 1)[-1]
            if not re.fullmatch(r"[a-f0-9]{24}", bid):
                return self.send(404, "Not found", "text/plain")
            with sqlite3.connect(DB) as db:
                exists = db.execute("SELECT 1 FROM books WHERE id=?", (bid,)).fetchone()
            if not exists:
                return self.send(404, "Not found", "text/plain")
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 250000:
                    raise ValueError("Invalid size")
                payload = json.loads(self.rfile.read(size))
                if not isinstance(payload, list) or len(payload) > 300:
                    raise ValueError("Invalid elements")
                for e in payload:
                    if not isinstance(e, dict) or e.get("type") not in ("text", "link", "video") or type(e.get("page")) is not int or e["page"] < 1:
                        raise ValueError("Invalid element")
                    for key in ("x", "y", "w", "h", "size"):
                        if type(e.get(key)) not in (int, float) or not 0 <= e[key] <= 120:
                            raise ValueError("Invalid dimensions")
                    for key in ("label", "url", "font", "color", "id"):
                        if not isinstance(e.get(key), str) or len(e[key]) > 2000:
                            raise ValueError("Invalid field")
                    if e["type"] in ("link", "video") and e["url"] and not e["url"].startswith(("https://", "http://")):
                        raise ValueError("Only HTTP(S) URLs allowed")
                target = ELEMENTS / (bid + ".json")
                temp = ELEMENTS / (bid + ".tmp")
                temp.write_text(json.dumps(payload), encoding="utf-8")
                temp.replace(target)
            except (ValueError, KeyError, TypeError) as exc:
                return self.send(400, str(exc), "text/plain")
            return self.send(200, '{"ok":true}', "application/json")
        if path != "/api/upload":
            return self.send(404, "Not found", "text/plain")
        if not self.authorized():
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self.send(400, "Invalid length", "text/plain")
        if size <= 0 or size > MAX_BYTES + 100000:
            return self.send(413, "Upload must be under 40 MB", "text/plain")
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            return self.send(400, "Expected PDF form upload", "text/plain")
        raw = self.rfile.read(size)
        msg = BytesParser(policy=default).parsebytes(("MIME-Version: 1.0\r\nContent-Type: " + content_type + "\r\n\r\n").encode() + raw)
        if not msg.is_multipart():
            return self.send(400, "Invalid upload", "text/plain")
        fields = {}
        for part in msg.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if name in ("title", "pdf"):
                fields[name] = part.get_payload(decode=True)
        title = fields.get("title", b"").decode("utf-8", errors="replace").strip()[:120]
        pdf = fields.get("pdf", b"")
        if not title or len(pdf) < 8 or len(pdf) > MAX_BYTES or not pdf.startswith(b"%PDF-"):
            return self.send(400, "Provide a title and a valid PDF under 40 MB", "text/plain")
        bid = secrets.token_hex(12)
        filename = bid + ".pdf"
        (PDFS / filename).write_bytes(pdf)
        with sqlite3.connect(DB) as db:
            db.execute("INSERT INTO books(id,title,filename) VALUES (?,?,?)", (bid,title,filename))
        self.send_response(303)
        self.send_header("Location", "/admin")
        self.send_header("Content-Length", "0")
        self.end_headers()

port = int(os.environ.get("PORT", "8080"))
ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
