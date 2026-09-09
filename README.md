# MetrIQ — Legal Metrology Label Scanner

Point a camera or upload a photo of a packaged product's label, and MetrIQ
reads it, checks it against the declarations Indian legal metrology law
requires, and tells you exactly what's right, what's missing, and what looks
off — each flag tied to the specific rule it comes from.

Built by **NEXORA**, a team of six students from Jalpaiguri Government
Engineering College (JGEC).

> Decision-support tool, not a legal certificate. A "fail" may mean the OCR
> missed a declaration that's genuinely printed on the pack — always
> spot-check flagged items against the physical package.

## What it checks

Every product is judged against the baseline declarations of **Rule 6, LMPC
Rules 2011**, plus category-specific rules:

| Category | Legal basis |
|---|---|
| Food & Beverage | FSS (Packaging & Labelling) Regulations, 2020 |
| Cosmetics & Personal Care | Cosmetics Rules, 2020 |
| Drugs & Pharmaceuticals | Drugs & Cosmetics Rules, 1945 |
| Electronics & Appliances | BIS Compulsory Registration |
| General / Other Packaged Commodity | Rule 6, LMPC Rules 2011 (baseline only) |

Imported products are also checked against **Rule 27** (country of origin &
importer details).

Typical checks include MRP (with "inclusive of all taxes" wording), net
quantity in standard units, manufacturer/packer/importer details, month &
year of manufacture, and best-before/expiry declarations — each result comes
back as pass / fail / warn / info, never a silent guess.

## Features

- **Multi-image scans** — add several photos of one label (front, back,
  folded flap) so text hidden behind a fold still gets read.
- **Camera or upload** — capture directly from a device camera or drop in
  image files.
- **Rule-cited verdicts** — every check names the exact rule it comes from,
  so a result can be verified against the source regulation.
- **Annotated photos** — flagged issues are marked directly on the label
  image, not just listed as text.
- **Scan archive** — every check is saved to disk and browsable from a
  passcode-protected admin view (`/admin`), with filtering by verdict.
- **Open source, runs locally** — no paid APIs, no data leaving your
  machine. OCR runs on-device via PaddleOCR.

## Tech stack

- **Backend:** Python, FastAPI, PaddleOCR, OpenCV
- **Frontend:** Vanilla HTML/CSS/JS (no framework, no build step)
- **Storage:** Flat JSON session files on disk (`backend/sessions/`)

## Project structure

```
.
├── backend/
│   ├── server.py              FastAPI app, routes, admin auth
│   ├── ocr_engine.py          Image → text extraction (PaddleOCR)
│   ├── compliance_engine.py   Rule checks per product category
│   ├── visual_engine.py       Annotates flagged fields on the photo
│   └── label_parser.py        Shared date / price parsing helpers
├── frontend/
│   ├── index.html             Landing page
│   ├── scan.html              Scan station (camera/upload + results log)
│   ├── about.html             Team & rules reference
│   ├── admin.html             Scan archive (passcode-protected)
│   ├── app.js / admin.js
│   └── style.css
├── Dockerfile
└── requirements.txt
```

## Running locally

Requires Python 3.10+.

```bash
pip install -r requirements.txt
cd backend
uvicorn server:app --reload --port 8000
```

Open `http://localhost:8000`. The scanner is at `/scan`, the archive at
`/admin` (passcode set via the `ADMIN_PASSCODE` environment variable,
defaults to `labelcheck` — change this before deploying anywhere public).

## Deployment

A `Dockerfile` is included, set up for [Google Cloud Run](https://cloud.google.com/run)'s free tier:

```bash
gcloud run deploy metriq \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --memory 4Gi \
  --set-env-vars ADMIN_PASSCODE=choose-a-real-passcode
```

Note: Cloud Run's free tier is stateless — the scan archive under
`backend/sessions/` does not persist across container restarts. For a
persistent archive, back it with an external store (e.g. a database or
object storage) instead.

## Team

| Name | Role |
|---|---|
| Rishik Bhattacharya | Team Leader — Mechanical Engineering, JGEC |
| Sumanta Kumar Adhek | Information Technology, JGEC |
| Akash Paria | Information Technology, JGEC |
| Prapti Howlader | Information Technology, JGEC |
| Swapnanil Dey | Mechanical Engineering, JGEC |
| Anurag Sarkar | Mechanical Engineering, JGEC |
