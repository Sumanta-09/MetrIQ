"""
Web server for the Legal Metrology Label Scanner ("MetrIQ").

Serves the multi-page frontend (Home / Scan / About / Admin) and the
API endpoints:

  GET  /api/product-types            categories for the scan dropdown
  POST /api/process                  run OCR + compliance on uploaded images
  GET  /api/admin/sessions           list every past check (session archive)
  GET  /api/admin/sessions/{id}      full detail of one past check
  GET  /api/admin/sessions/{id}/images/{name}   original label photo

The /api/admin/* endpoints are protected by a shared passcode so the
check archive isn't open to anyone who can reach the server. Set the
passcode with the ADMIN_PASSCODE environment variable (defaults to
"labelcheck"). The frontend asks for it once and remembers it in the
browser tab.

Run with:
    uvicorn server:app --reload --port 8000

Then open http://localhost:8000 in a browser.
"""

import os
import re
import json
from datetime import datetime

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from typing import List

import ocr_engine
from compliance_engine import PRODUCT_CATEGORIES

app = FastAPI(title="MetrIQ - Legal Metrology Label Scanner")

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")
SESSIONS_DIR = ocr_engine.SESSIONS_DIR

ALLOWED_EXTENSIONS = (".jpg", ".jpeg", ".png", ".bmp", ".webp")
MAX_FILES_PER_REQUEST = 10
MAX_FILE_SIZE_MB = 15

ADMIN_PASSCODE = os.environ.get("ADMIN_PASSCODE", "labelcheck")
SESSION_ID_RE = re.compile(r"^session_\d{8}_\d{6}_\d{6}$")


@app.on_event("startup")
def preload_ocr_model():
    # Load the PaddleOCR model once at startup instead of on the first
    # request, so the first user doesn't hit a ~1 minute delay.
    ocr_engine.get_ocr_engine()


# ----------------------------------------------------------------------
# Shared helpers
# ----------------------------------------------------------------------
def _require_admin(request: Request):
    """Every /api/admin/* endpoint checks the passcode header."""
    key = request.headers.get("x-admin-key", "")
    if key != ADMIN_PASSCODE:
        raise HTTPException(
            status_code=401,
            detail="This area is restricted. Enter the admin passcode to view the check archive.",
        )


def _format_timestamp(ts):
    """session_timestamp 'YYYYMMDD_HHMMSS_ffffff' -> (iso, human display)."""
    try:
        dt = datetime.strptime(ts, "%Y%m%d_%H%M%S_%f")
    except (TypeError, ValueError):
        return ts, ts
    return dt.isoformat(), dt.strftime("%d %b %Y, %I:%M %p")


def _list_session_images(session_dir):
    images_dir = os.path.join(session_dir, "images")
    if not os.path.isdir(images_dir):
        return []
    return sorted(
        n for n in os.listdir(images_dir)
        if os.path.splitext(n)[1].lower() in ALLOWED_EXTENSIONS
    )


def _session_summary(session_id, session_dir):
    """Thin summary of one archived session for the admin list view."""
    sf_path = os.path.join(session_dir, "structured_fields.json")
    if not os.path.isfile(sf_path):
        return None
    try:
        with open(sf_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    compliance = data.get("compliance") or {}
    has_verdict = bool(data.get("compliance"))
    ts = data.get("session_timestamp") or session_id[len("session_"):]
    iso, display = _format_timestamp(ts)

    return {
        "id": session_id,
        "display": display,
        "iso": iso,
        "product_type": (compliance.get("product_type") or data.get("product_type") or "other") if has_verdict else "other",
        "product_type_label": (
            compliance.get("product_type_label") or data.get("product_type_label")
            or "General / Other Packaged Commodity"
        ) if has_verdict else "Archived scan (pre-compliance)",
        "verdict": compliance.get("overall_status", "needs_review") if has_verdict else "no_verdict",
        "required_passed": compliance.get("required_passed", 0) if has_verdict else 0,
        "required_total": compliance.get("required_total", 0) if has_verdict else 0,
        "summary": compliance.get("summary") or {},
        "fields_detected": sum(
            1 for k, v in (data.get("structured_fields") or {}).items()
            if not k.startswith("_") and isinstance(v, dict)
        ),
        "image_count": len(_list_session_images(session_dir)),
    }


# ----------------------------------------------------------------------
# Public API (used by the scan page)
# ----------------------------------------------------------------------
@app.get("/api/product-types")
def get_product_types():
    """Product categories the compliance engine knows rule sets for, for the frontend dropdown."""
    return [{"value": key, "label": label} for key, label in PRODUCT_CATEGORIES.items()]


@app.post("/api/process")
async def process_images(
    images: List[UploadFile] = File(...),
    product_type: str = Form("other"),
    is_imported: bool = Form(False),
):
    if not images:
        raise HTTPException(status_code=400, detail="No images were uploaded.")
    if len(images) > MAX_FILES_PER_REQUEST:
        raise HTTPException(
            status_code=400,
            detail=f"Too many images in one request (max {MAX_FILES_PER_REQUEST})."
        )
    if product_type not in PRODUCT_CATEGORIES:
        product_type = "other"

    files = []
    for upload in images:
        ext = os.path.splitext(upload.filename or "")[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported file type '{ext}' for '{upload.filename}'. "
                       f"Allowed: {', '.join(ALLOWED_EXTENSIONS)}"
            )
        content = await upload.read()
        if len(content) > MAX_FILE_SIZE_MB * 1024 * 1024:
            raise HTTPException(
                status_code=400,
                detail=f"'{upload.filename}' exceeds the {MAX_FILE_SIZE_MB}MB limit."
            )
        files.append((upload.filename, content))

    try:
        result = ocr_engine.process_uploaded_images(
            files, product_type=product_type, is_imported=is_imported,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return result


# ----------------------------------------------------------------------
# Admin API (past checks / session archive)
# ----------------------------------------------------------------------
@app.get("/api/admin/sessions")
def list_sessions(request: Request):
    _require_admin(request)

    sessions = []
    if os.path.isdir(SESSIONS_DIR):
        for name in sorted(os.listdir(SESSIONS_DIR), reverse=True):
            session_dir = os.path.join(SESSIONS_DIR, name)
            if not os.path.isdir(session_dir) or not name.startswith("session_"):
                continue
            summary = _session_summary(name, session_dir)
            if summary:
                sessions.append(summary)

    stats = {
        "total": len(sessions), "compliant": 0,
        "needs_review": 0, "non_compliant": 0, "no_verdict": 0,
    }
    for s in sessions:
        key = s["verdict"]
        if key in stats:
            stats[key] += 1

    return {"sessions": sessions, "stats": stats}


@app.get("/api/admin/sessions/{session_id}")
def get_session_detail(session_id: str, request: Request):
    _require_admin(request)
    if not SESSION_ID_RE.match(session_id):
        raise HTTPException(status_code=404, detail="Unknown session id.")

    session_dir = os.path.join(SESSIONS_DIR, session_id)
    sf_path = os.path.join(session_dir, "structured_fields.json")
    if not os.path.isdir(session_dir) or not os.path.isfile(sf_path):
        raise HTTPException(status_code=404, detail="That session no longer exists on disk.")

    try:
        with open(sf_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        raise HTTPException(status_code=500, detail=f"Could not read session file: {e}")

    ts = data.get("session_timestamp") or session_id[len("session_"):]
    iso, display = _format_timestamp(ts)
    data["session_id"] = session_id
    data["display"] = display
    data["iso"] = iso
    data["images"] = _list_session_images(session_dir)
    data["has_verdict"] = bool(data.get("compliance"))
    if not data.get("has_verdict"):
        # Archived before the compliance engine existed - surface honestly
        # instead of inventing a verdict for scans that never had one.
        data["compliance"] = {
            "product_type": "other",
            "product_type_label": "Archived scan (pre-compliance)",
            "is_imported": False,
            "overall_status": "no_verdict",
            "required_total": 0,
            "required_passed": 0,
            "summary": {},
            "checks": [],
        }
    return data


@app.get("/api/admin/sessions/{session_id}/images/{image_name}")
def get_session_image(session_id: str, image_name: str, request: Request):
    _require_admin(request)
    if not SESSION_ID_RE.match(session_id):
        raise HTTPException(status_code=404, detail="Unknown session id.")
    # Never allow path traversal: image must be a plain filename inside the session's images/ dir.
    if os.path.basename(image_name) != image_name or os.path.splitext(image_name)[1].lower() not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=404, detail="Unknown image.")
    path = os.path.join(SESSIONS_DIR, session_id, "images", image_name)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="That image no longer exists on disk.")
    return FileResponse(path)


# ----------------------------------------------------------------------
# Pages
# ----------------------------------------------------------------------
def _page(filename: str):
    return FileResponse(os.path.join(FRONTEND_DIR, filename))


@app.get("/")
def serve_index():
    return _page("index.html")


@app.get("/scan")
def serve_scan():
    return _page("scan.html")


@app.get("/admin")
def serve_admin():
    return _page("admin.html")


# Serve everything else in /frontend (style.css, app.js, admin.js, etc.) as static files
app.mount("/", StaticFiles(directory=FRONTEND_DIR), name="frontend")