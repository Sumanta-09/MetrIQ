"""
OCR engine for the Legal Metrology Label Scanner web app.

This is the same pipeline as the standalone desktop script, adapted to
work on in-memory image bytes coming from HTTP uploads instead of files
on disk / a webcam. For every request it still writes a full session
folder to disk (images + raw OCR + structured fields) so nothing is
lost and results can be inspected or re-used later.
"""

import cv2
import numpy as np
import os
import re
import json
import difflib
from datetime import datetime
from paddleocr import PaddleOCR

import compliance_engine
import label_parser
import visual_engine

# ----------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------
SESSIONS_DIR = os.path.join(os.path.dirname(__file__), "sessions")
os.makedirs(SESSIONS_DIR, exist_ok=True)

_ocr_engine = None


def get_ocr_engine(lang="en"):
    global _ocr_engine
    if _ocr_engine is None:
        print("Loading PaddleOCR models (first run may take a minute)...")
        _ocr_engine = PaddleOCR(
            lang=lang,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=True,
            enable_mkldnn=False,  # workaround for a PaddlePaddle 3.3.x oneDNN crash
        )
    return _ocr_engine


# ----------------------------------------------------------------------
# IMAGE PRE-PROCESSING
# ----------------------------------------------------------------------
def preprocess_image_bytes(image_bytes):
    """
    Decodes raw image bytes (as received over HTTP) and applies the same
    light pre-processing used in the desktop version: upscale small
    images, mild denoise, keep color (PaddleOCR handles lighting/noise
    better than a hand-binarized image).
    """
    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("Could not decode image data - unsupported or corrupt file.")

    height, width = img.shape[:2]
    if width < 1000:
        scale = 1000 / width
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)

    denoised = cv2.fastNlMeansDenoisingColored(img, None, 5, 5, 7, 21)
    return denoised


# ----------------------------------------------------------------------
# OCR TEXT CLEANUP HELPERS
# ----------------------------------------------------------------------
def normalize_ocr_text(text):
    """
    Fixes two common PaddleOCR artifacts on stylized/bold fonts:
    a word merged with an adjacent number (no space), and O/I/S/B
    misread as 0/1/5/8 inside that merged numeric part.
    """
    pattern = re.compile(r'^([A-Za-z]+)([0-9][0-9OoIlSB]*)$')
    fixed_tokens = []
    for tok in text.split(' '):
        m = pattern.match(tok)
        if m:
            word, numpart = m.groups()
            numpart = (
                numpart.replace('O', '0').replace('o', '0')
                       .replace('I', '1').replace('l', '1')
                       .replace('S', '5').replace('B', '8')
            )
            fixed_tokens.append(word)
            fixed_tokens.append(numpart)
        else:
            fixed_tokens.append(tok)
    return ' '.join(fixed_tokens)


LEGAL_METROLOGY_KEYWORDS = [
    "MRP", "MAXIMUM", "RETAIL", "PRICE", "INCLUSIVE", "TAXES",
    "NET", "QUANTITY", "WEIGHT", "WT", "VOLUME", "GRAMS", "KILOGRAMS",
    "MANUFACTURED", "MANUFACTURER", "PACKED", "PACKER", "IMPORTED", "IMPORTER",
    "MFG", "MFD", "EXPIRY", "BEST", "BEFORE", "USE", "CONTENTS",
    "CONSUMER", "CARE", "CUSTOMER", "ADDRESS", "PHONE", "EMAIL",
    "COUNTRY", "ORIGIN", "MADE", "INDIA", "INGREDIENTS", "CONTAINS",
    "ALLERGEN", "LOT", "BATCH",
]


def correct_domain_keywords(text, min_word_len=4, max_distance_ratio=0.34):
    """Fuzzy-corrects words toward the closest Legal Metrology keyword, only when very close."""
    words = text.split(' ')
    corrected = []
    for word in words:
        core = word.strip(':,.-')
        suffix = word[len(core):] if core else ''
        if len(core) >= min_word_len and core.isalpha():
            match = difflib.get_close_matches(
                core.upper(), LEGAL_METROLOGY_KEYWORDS, n=1, cutoff=1 - max_distance_ratio
            )
            if match and match[0] != core.upper():
                corrected.append(match[0] + suffix)
                continue
        corrected.append(word)
    return ' '.join(corrected)


# ----------------------------------------------------------------------
# OCR EXTRACTION
# ----------------------------------------------------------------------
def _get_field(res, key):
    try:
        return res[key]
    except (KeyError, TypeError):
        return res["res"][key]


def extract_text_with_confidence(processed_image, lang="en", normalize=True):
    engine = get_ocr_engine(lang=lang)
    result = engine.predict(processed_image)

    words = []
    for res in result:
        texts = _get_field(res, "rec_texts")
        scores = _get_field(res, "rec_scores")
        try:
            boxes = _get_field(res, "rec_boxes")
        except (KeyError, TypeError):
            boxes = [None] * len(texts)

        for text, score, box in zip(texts, scores, boxes):
            if normalize:
                text = correct_domain_keywords(normalize_ocr_text(text))
            words.append({
                "text": text,
                "confidence": round(float(score) * 100, 2),
                "bbox": box.tolist() if hasattr(box, "tolist") else box
            })
    return words


# ----------------------------------------------------------------------
# MULTI-IMAGE MERGE / DE-DUPLICATION
# ----------------------------------------------------------------------
def merge_and_dedupe_lines(all_images_lines, similarity_threshold=0.85):
    """
    all_images_lines: list of (image_name, list_of_line_dicts).
    Collapses near-duplicate lines (same text photographed twice) into
    the single highest-confidence version.
    """
    merged = []
    for image_name, lines in all_images_lines:
        for line in lines:
            norm = re.sub(r'[^A-Z0-9]', '', line["text"].upper())
            if not norm:
                continue

            duplicate_idx = None
            for idx, existing in enumerate(merged):
                existing_norm = re.sub(r'[^A-Z0-9]', '', existing["text"].upper())
                if not existing_norm:
                    continue
                ratio = difflib.SequenceMatcher(None, norm, existing_norm).ratio()
                if ratio >= similarity_threshold:
                    duplicate_idx = idx
                    break

            entry = dict(line)
            entry["source_image"] = image_name

            if duplicate_idx is None:
                merged.append(entry)
            elif entry["confidence"] > merged[duplicate_idx]["confidence"]:
                merged[duplicate_idx] = entry
    return merged


# ----------------------------------------------------------------------
# FIELD STRUCTURING (Legal Metrology-relevant declarations)
# ----------------------------------------------------------------------
def order_lines_reading_order(lines):
    """
    Reconstructs left-to-right, top-to-bottom reading order from each
    line's bounding box, instead of trusting PaddleOCR's own detection
    order. That raw order is NOT guaranteed to be spatial - on real
    packaging it's very common for a label like "NET VOL." and its
    value "250ml" to be detected as two separate boxes on the same
    physical line, with the value's box returned BEFORE the label's box
    in PaddleOCR's output. Joining lines in that raw order then puts the
    value earlier in the text than its own keyword, which silently
    breaks every "keyword ... value" regex looking forward from the
    keyword (the value it needs already went by).

    Lines without a usable bbox are left in their original relative
    order, appended at the end.

    Rows are found by simple greedy vertical-overlap clustering: sort
    all lines by vertical center, then walk through them, joining a
    line to the current row if its vertical extent meaningfully
    overlaps the row's, else starting a new row. Each row is then
    sorted left-to-right by its left edge.
    """
    with_bbox = [l for l in lines if l.get("bbox") and len(l["bbox"]) >= 4]
    without_bbox = [l for l in lines if not (l.get("bbox") and len(l["bbox"]) >= 4)]

    with_bbox = sorted(with_bbox, key=lambda l: (l["bbox"][1] + l["bbox"][3]) / 2)

    # A running average per row (rather than a min/max union of every
    # member's bbox) is important here: a union keeps growing every time
    # a taller box joins, which can eventually let an unrelated line two
    # or three rows down drift within its bounds and get pulled in. An
    # average recentres on where the row actually is.
    rows = []
    for line in with_bbox:
        y1, y2 = line["bbox"][1], line["bbox"][3]
        center = (y1 + y2) / 2
        height = max(y2 - y1, 1)
        placed = False
        for row in rows:
            if abs(center - row["avg_center"]) <= 0.6 * min(height, row["avg_height"]):
                row["lines"].append(line)
                n = len(row["lines"])
                row["avg_center"] += (center - row["avg_center"]) / n
                row["avg_height"] += (height - row["avg_height"]) / n
                placed = True
                break
        if not placed:
            rows.append({"avg_center": center, "avg_height": height, "lines": [line]})

    ordered = []
    for row in rows:
        row["lines"].sort(key=lambda l: l["bbox"][0])
        ordered.extend(row["lines"])
    ordered.extend(without_bbox)
    return ordered


def structure_label_fields(merged_lines):
    """
    Extracts Legal Metrology-relevant fields BY TYPE (currency/date/code
    patterns) across the whole merged text, so it isn't thrown off by a
    label name and its value sitting in misaligned boxes. Heuristic, not
    a legal determination - always spot check against the real package.
    """
    full_text = "\n".join(line["text"] for line in order_lines_reading_order(merged_lines))
    fields = {}

    # Real labels rarely stick to a single clean keyword like "NET WT:" -
    # combined forms like "NET WT./VOL." (weight/volume declared as one
    # line) and "NET CONTENTS" are common, and the separator before the
    # number is just as often a period as a colon or dash. The old
    # version only allowed a single optional ":" or "-", which silently
    # failed to match anything with a period in it (e.g. "NET WT./VOL.
    # 170 ml") even though the declaration was clearly on the pack.
    _NQ_KEYWORD = r'(?:WEIGHT|QUANTITY|QTY|WT|VOL(?:UME)?|CONTENTS?|MASS)'
    _NQ_UNIT = r'(?:g|gm|gms|gr|gram|grams|kg|kgs|ml|millilitre|millilitres|l|lt|ltr|ltrs|litre|litres|cl|mg|oz)'
    m = re.search(
        rf'NET\s*{_NQ_KEYWORD}(?:[\s./\-]*{_NQ_KEYWORD})?[\s./\-:]*'
        rf'([\d]+(?:\.\d+)?\s?(?:{_NQ_UNIT})\b)',
        full_text, re.IGNORECASE
    )
    if m:
        fields["net_quantity"] = {"value": m.group(1).strip(), "context": m.group(0).strip()}
    else:
        # Fallback: a "bonus pack" phrasing like "16 g (12.2 g + 3.8 g
        # EXTRA)" is itself a strong net-quantity signal, independent of
        # whether it sits right next to a "NET QTY" keyword (which is
        # often misaligned into a separate box on real packaging).
        m_extra = re.search(
            r'(\d+(?:\.\d+)?\s?(?:g|kg|ml|l))\s*\(\s*(\d+(?:\.\d+)?\s?(?:g|kg|ml|l))\s*\+\s*'
            r'(\d+(?:\.\d+)?\s?(?:g|kg|ml|l))\s*EXTRA\s*\)',
            full_text, re.IGNORECASE
        )
        if m_extra:
            fields["net_quantity"] = {
                "value": m_extra.group(1).strip(),
                "context": f"{m_extra.group(0).strip()} (includes {m_extra.group(3).strip()} extra)"
            }

    m2 = re.search(
        r'\(\s*(\d+)\s*Units?\s*[x\u00d7]\s*([\d.]+\s?(?:g|kg|ml|l))\s*\)',
        full_text, re.IGNORECASE
    )
    if m2:
        fields["pack_breakdown"] = {"units": m2.group(1), "unit_quantity": m2.group(2)}

    # --- Prices: anchor to explicit MRP / "retail price" keywords first
    # (most reliable), then fall back to any Rs./\u20b9/INR amount on the
    # label. Values are parsed with label_parser.parse_price so the
    # compliance engine gets a typed number, not a string, and the
    # mandatory "inclusive of all taxes" wording is recorded as a flag.
    tax_inclusive = bool(re.search(
        r'incl(?:usive|\.)?\s+(?:of\s+)?(?:all\s+)?tax(?:es)?', full_text, re.IGNORECASE
    ))

    mrp = None
    mrp_context = None
    # Currency-anchored first: "MRP (Incl. of all taxes) Rs. 12.00"
    m_mrp = re.search(
        r'(?:MRP|MAX(?:IMUM)?\.?\s*RETAIL\s*PRICE|M\.?\s*R\.?\s*P\.?)'
        r'[^0-9\n]{0,60}?((?:Rs\.?|\u20b9|INR)\s?\d[\d,]*(?:\.\d{1,2})?)',
        full_text, re.IGNORECASE
    )
    if m_mrp:
        parsed = label_parser.parse_price(m_mrp.group(1))
        if parsed:
            mrp = parsed
            mrp_context = m_mrp.group(0).strip()
    if not mrp:
        # Bare number right after the MRP keyword, e.g. "MRP: 12.00"
        m_mrp_bare = re.search(
            r'(?:MRP|MAX(?:IMUM)?\.?\s*RETAIL\s*PRICE|M\.?\s*R\.?\s*P\.?)'
            r'[^0-9\n]{0,60}?(\d[\d,]*(?:\.\d{1,2})?)',
            full_text, re.IGNORECASE
        )
        if m_mrp_bare:
            parsed = label_parser.parse_price(m_mrp_bare.group(1))
            if parsed:
                mrp = parsed
                mrp_context = m_mrp_bare.group(0).strip()
    if not mrp:
        # Last resort: any currency amount on the label.
        m_any = re.search(r'((?:Rs\.?|\u20b9|INR)\s?\d[\d,]*(?:\.\d{1,2})?)', full_text, re.IGNORECASE)
        if m_any:
            parsed = label_parser.parse_price(m_any.group(1))
            if parsed:
                mrp = parsed
                mrp_context = m_any.group(1).strip()
    if mrp:
        fields["mrp"] = {
            "value": label_parser.format_price(mrp),
            "amount": mrp["amount"],
            "currency": mrp["currency"],
            "tax_inclusive": tax_inclusive,
        }
        if mrp_context:
            fields["mrp"]["context"] = mrp_context

    # Unit price, e.g. "Rs. 40 per kg" / "\u20b9120 per 100 g"
    m_unit = re.search(
        r'((?:Rs\.?|\u20b9|INR)\s?\d[\d,]*(?:\.\d{1,2})?)\s*(?:/-)?\s*per\s*(\d+\s*)?(g|gm|kg|ml|l|litre|litres|ltr)',
        full_text, re.IGNORECASE
    )
    if m_unit:
        parsed = label_parser.parse_price(m_unit.group(1))
        if parsed:
            fields["unit_price"] = {
                "value": f"{label_parser.format_price(parsed)} per {m_unit.group(3)}",
                "amount": parsed["amount"],
            }

    # --- Dates: anchor to explicit keywords first (most reliable),
    # and only fall back to a blind earliest/latest guess across every
    # date-like token on the label when no keyword match is found (for
    # labels where the value sits in a misaligned box far from its
    # keyword, e.g. some snack-brand back labels). ---
    # Rule 6(1)(d) only requires MONTH & YEAR, not a full date - and a lot
    # of real labels print exactly that and nothing more ("MFD: 07/2026",
    # "MFG. 07.2026", "MFD: JUL 2026", "Mfg Date: July 2026"). The old
    # DATE_RE only matched a full dd/mm/yyyy, so any label that (correctly,
    # per the rule) only prints month+year was silently treated as "date
    # not found" even though it was right there in the text.
    DATE_FULL_RE = r'\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}'
    _MONTH_NAMES = (
        r'(?:JAN(?:UARY)?|FEB(?:RUARY)?|MAR(?:CH)?|APR(?:IL)?|MAY|JUNE?|JULY?|'
        r'AUG(?:UST)?|SEP(?:T(?:EMBER)?)?|OCT(?:OBER)?|NOV(?:EMBER)?|DEC(?:EMBER)?)'
    )
    DATE_MONTHYEAR_NAME_RE = rf'{_MONTH_NAMES}\.?\s*\'?\s*\d{{2,4}}'
    DATE_MONTHYEAR_NUM_RE = r'\d{1,2}[/\-.]\d{2,4}\b'
    # Keyword-anchored searches accept any of the three forms (tried in
    # this order, so a full date is preferred over a bare month/year when
    # both would technically match); the un-anchored fallback below stays
    # strict to a full date only, since guessing mfg/use-by from a bare
    # month/year with no keyword nearby is too easy to confuse with some
    # other number on the pack.
    DATE_RE = rf'({DATE_FULL_RE}|{DATE_MONTHYEAR_NAME_RE}|{DATE_MONTHYEAR_NUM_RE})'
    mfg_date, use_by_date = None, None

    # Combined declaration, e.g. "MFD & USE BY: 30/07/26 & 27/11/26"
    _MFG_KEYWORD = r'(?:MFD|MFG|MANUFACTUR\w*|PKD|PACKED\s*(?:ON|DATE)?)'
    _USE_KEYWORD = r'(?:USE\s*BY|EXPIR\w*|BEST\s*BEFORE|BESTBEFORE|EXP\s*DATE)'
    m_combo = re.search(
        rf'{_MFG_KEYWORD}\s*(?:DATE)?\s*(?:&|AND)\s*'
        rf'{_USE_KEYWORD}[:\s]{{0,10}}'
        rf'{DATE_RE}\s*(?:&|AND)\s*{DATE_RE}',
        full_text, re.IGNORECASE
    )
    if m_combo:
        mfg_date, use_by_date = m_combo.group(1), m_combo.group(2)
    else:
        m_mfg = re.search(
            rf'{_MFG_KEYWORD}\.?\s*(?:DATE)?[:\s]{{0,10}}{DATE_RE}',
            full_text, re.IGNORECASE
        )
        m_use = re.search(
            rf'{_USE_KEYWORD}\.?\s*(?:DATE)?[:\s]{{0,10}}{DATE_RE}',
            full_text, re.IGNORECASE
        )
        if m_mfg:
            mfg_date = m_mfg.group(1)
        if m_use:
            use_by_date = m_use.group(1)

    # Some FMCG brands (Unilever's HUL products among them) print the
    # manufacturing timestamp in a "coding area" as a bare date
    # immediately followed by a clock time - e.g. "#19/06/26 16:44" -
    # with the "#" meaning explained once elsewhere on the pack
    # ("#MFD., & BATCH NO. SEE CODING AREA") rather than repeated next
    # to every occurrence, so the keyword-anchored search above never
    # sees "MFD" sitting next to this particular date. A date+time
    # pairing like this is a strong, fairly specific signal on its own
    # ("this is the manufacturing timestamp") - a use-by/expiry date is
    # essentially never printed with a time-of-day - worth trying
    # before falling back to a blind earliest/latest guess across
    # every date-like token.
    mfg_date_note = None
    if not mfg_date:
        m_mfg_ts = re.search(rf'{DATE_FULL_RE}\s+(\d{{1,2}}:\d{{2}})\b', full_text)
        if m_mfg_ts:
            mfg_date = m_mfg_ts.group(0).split()[0]
            mfg_date_note = (
                "Read from a bare date+time stamp in a coding area, not a line "
                "explicitly labelled 'MFD' or 'Manufactured on' - common on some "
                "FMCG packaging where a symbol (e.g. '#') stands in for the label "
                "and its meaning is explained once elsewhere on the pack."
            )

    if not mfg_date and not use_by_date:
        date_candidates = re.findall(rf'\b{DATE_FULL_RE}\b', full_text)
        parsed_dates = [(d_parsed, d) for d in date_candidates for d_parsed in [label_parser.parse_date(d)] if d_parsed]
        parsed_dates.sort(key=lambda x: x[0])
        if len(parsed_dates) >= 2:
            # With two or more dates we can at least infer an order
            # (earliest = mfg, latest = use-by/expiry) even without a
            # keyword anchor.
            mfg_date = parsed_dates[0][1]
            use_by_date = parsed_dates[-1][1]
        elif len(parsed_dates) == 1:
            # A single date with no keyword nearby to say whether it's
            # the MFG or USE BY date - guessing either way risks being
            # confidently wrong, so surface it as unclassified instead.
            fields["date_detected_unclassified"] = {
                "value": parsed_dates[0][1],
                "parsed": parsed_dates[0][0].strftime("%Y-%m-%d"),
                "note": "Found on the label but couldn't confirm if it's the MFG or USE BY / expiry date - check manually."
            }

    def _date_entry(raw_value, note=None):
        """Builds a structured date field, attaching the parsed ISO form
        (via label_parser) so the compliance engine can compare dates
        without re-parsing the raw string."""
        entry = {"value": raw_value}
        parsed = label_parser.parse_date(raw_value)
        if parsed:
            entry["parsed"] = parsed.strftime("%Y-%m-%d")
        if note:
            entry["note"] = note
        return entry

    if mfg_date:
        fields["mfg_date"] = _date_entry(mfg_date, mfg_date_note)
    if use_by_date:
        fields["use_by_or_expiry_date"] = _date_entry(use_by_date)

    # Relative best-before, e.g. "Best before 9 months from packaging" -
    # common on packs that print a shelf life instead of a concrete date.
    # When the manufacture date is known, the implied expiry can be
    # computed from it for the compliance engine.
    rel_months, rel_raw = label_parser.relative_best_before_months(full_text)
    if rel_months is not None:
        rel_entry = {"months": rel_months, "context": rel_raw}
        mfg_parsed = (fields.get("mfg_date") or {}).get("parsed")
        if mfg_parsed:
            mfg_dt = datetime.strptime(mfg_parsed, "%Y-%m-%d")
            # Approximate calendar-month addition (clamped day to 28 so
            # short months never overflow).
            m_total = mfg_dt.year * 12 + (mfg_dt.month - 1) + int(round(rel_months))
            y, m = divmod(m_total, 12)
            implied = datetime(y, m + 1, min(mfg_dt.day, 28))
            rel_entry["implied_expiry"] = implied.strftime("%Y-%m-%d")
            if "use_by_or_expiry_date" not in fields:
                fields["use_by_or_expiry_date"] = {
                    "value": implied.strftime("%d/%m/%y"),
                    "parsed": implied.strftime("%Y-%m-%d"),
                    "note": f"Computed from the declared shelf life ({rel_raw}) applied to the manufacture date.",
                }
        fields["best_before_relative"] = rel_entry

    m3 = re.search(r'\b(\d{2}-\d{3}/[A-Z0-9]+/[A-Z0-9]+)\b', full_text, re.IGNORECASE)
    if m3:
        fields["lot_no"] = {"value": m3.group(1)}
    else:
        m3b = re.search(
            r'(?:Batch|Lot|B\.)\s*(?:No\.?)?[:\s]*([A-Za-z0-9][^\n]{0,40})',
            full_text, re.IGNORECASE
        )
        candidate = m3b.group(1).strip() if m3b else None
        if candidate:
            # Guard against grabbing a misaligned "date & date" value
            # (common on labels where the field name and its real value
            # sit in separate, offset boxes) - that's clearly not a
            # batch code even though it matched the keyword search.
            # Checked loosely (not just a strict date regex) because a
            # misread date, e.g. "3001/26" missing a slash, still isn't
            # a real alphanumeric batch code - genuine codes contain
            # letters, so anything left after stripping digits/slashes/
            # dashes/spaces/& is what tells them apart.
            has_ampersand = '&' in candidate or ' AND ' in candidate.upper()
            leftover = re.sub(r'[\d/\-.\s&]|and', '', candidate, flags=re.IGNORECASE)
            looks_like_date_pair = has_ampersand and leftover == ''
            # A second guard: some brands print a legend like "#MFD., &
            # BATCH NO. SEE CODING AREA" that explains what a symbol
            # means elsewhere on the pack, rather than giving the
            # actual batch value. The "Batch No." keyword search above
            # matches this legend line just as readily as a real
            # declaration, but "SEE CODING AREA" obviously isn't itself
            # a batch code.
            looks_like_legend_reference = bool(re.search(r'SEE\s*CODING|CODING\s*AREA', candidate, re.IGNORECASE))
            if not looks_like_date_pair and not looks_like_legend_reference:
                fields["lot_no"] = {"value": candidate}

        if "lot_no" not in fields:
            # Fallback for the "symbol + short code" coding-area
            # convention some FMCG packaging uses instead of (or in
            # addition to) a literal "Batch No." line - e.g. "Ø AE
            # B1297" on HUL products, where a legend elsewhere on the
            # pack explains that the character(s) after the symbol are
            # the batch/unit code. Deliberately narrow (its own short
            # line: symbol + 1-3 letter unit code + alphanumeric code)
            # to avoid false-matching unrelated short lines.
            m3c = re.search(
                r'(?:^|\n)\s*[\u00d8\u22050O]\s*([A-Z]{1,3})\s+([A-Z0-9]{3,10})\s*(?:\n|$)',
                full_text
            )
            if m3c:
                fields["lot_no"] = {
                    "value": f"{m3c.group(1)} {m3c.group(2)}",
                    "note": "Read from a coding-area symbol reference (e.g. '\u00d8' or similar), not a "
                            "line explicitly labelled 'Batch No.' - common on some FMCG packaging where "
                            "the symbol's meaning is explained once elsewhere on the pack.",
                }

    m4 = re.search(r'((?:Contains|May contain)[^.\n]*)', full_text, re.IGNORECASE)
    if m4:
        fields["allergen_advice"] = {"value": m4.group(1).strip()}

    # The value after "Manufactured by" / "Marketed by" etc. commonly
    # wraps across more than one printed line on real packaging - e.g.
    # "...MKTD. BY: HINDUSTAN\nUNILEVER LTD. (HUL), MUMBAI - 400 099."
    # where the "\n" is just where that physical print line happens to
    # end, not the end of the declaration. The old version's [^\n]+
    # stopped dead at that line break, so a real label like this one
    # only ever yielded the first word ("HINDUSTAN") instead of the
    # full name. Keep consuming a few more lines as long as they don't
    # look like the START of a different declaration.
    NEXT_FIELD_STARTS = (
        r'CONTAINS|FOR\s+MFG|LEVERCARE|LEVER\.?CARE|CONSUMER\s+CARE|CUSTOMER\s+CARE|'
        r'HUL\s+REGN|MINIMUM\s+THICKNESS|\*?MRP|TOLL[\s\-]*FREE|'
        r'NET\s*(?:WT|VOL|QTY|WEIGHT|QUANTITY|CONTENTS)|INGREDIENTS?|BATCH|LOT\s*NO|'
        r'BEST\s+BEFORE|USE\s+BY|EXPIR\w*|MFD|MFG'
    )
    m5 = re.search(
        r'(Manufactured(?:\s*(?:&|and)\s*(?:Marketed|Packed))?\s*by'
        r'|Mktd\.?\s*(?:&|and)?\s*(?:Mfd\.?)?\s*by|Mfd\.?\s*(?:&|and)?\s*(?:Mktd\.?)?\s*by'
        r'|Marketed(?:\s*(?:&|and)\s*Distributed)?\s*by|Packed by|Imported by)'
        r'[:\s]*([^\n]*(?:\n(?!\s*(?:' + NEXT_FIELD_STARTS + r'))[^\n]*){0,2})',
        full_text, re.IGNORECASE
    )
    if m5:
        value = re.sub(r'\s*\n\s*', ' ', m5.group(2)).strip().rstrip('.')
        fields["manufacturer_or_packer"] = {"value": value, "label_found": m5.group(1)}

    m6 = re.search(r'(Country of Origin|Made in)[:\s]*([A-Za-z ]+)', full_text, re.IGNORECASE)
    if m6:
        fields["country_of_origin"] = {"value": m6.group(2).strip()}

    m7 = re.search(
        r'(?:FSSAI(?:\s*Lic(?:ense)?\.?\s*No\.?)?|Mkt\.?\s*Lic(?:ense)?\.?\s*No\.?)'
        r'[^0-9]{0,15}(\d{10,14})',
        full_text, re.IGNORECASE
    )
    if m7:
        fields["fssai_or_license_no"] = {"value": m7.group(1)}

    m7b = re.search(r'\b(1800[\-\s]?\d{2,4}[\-\s]?\d{2,4}[\-\s]?\d{0,4})\b', full_text)
    if m7b:
        fields["toll_free_number"] = {"value": re.sub(r'\s+', '', m7b.group(1))}

    m8 = re.search(r'(Consumer Care|Customer Care|Lever\s*Care)[^\n]{0,120}', full_text, re.IGNORECASE)
    if m8:
        fields["consumer_care"] = {"value": m8.group(0).strip()}

    m9 = re.search(r'[\w.\-]+@[\w.\-]+\.\w+', full_text)
    if m9:
        fields["email"] = {"value": m9.group(0).strip()}

    m10 = re.search(
        r'INGREDIENTS?[:\s]*([^\n]*(?:\n[^\n]*){0,5})',
        full_text, re.IGNORECASE
    )
    if m10:
        ingredients_text = m10.group(1).strip().replace('\n', ' ')
        if ingredients_text:
            fields["ingredients"] = {"value": ingredients_text}

    required_fields = ["net_quantity", "mrp", "mfg_date", "manufacturer_or_packer", "consumer_care"]
    fields["_fields_not_detected"] = [f for f in required_fields if f not in fields]

    return fields


# ----------------------------------------------------------------------
# FULL PIPELINE FOR ONE REQUEST (one or more uploaded images)
# ----------------------------------------------------------------------
def process_uploaded_images(files, lang="en", product_type="other", is_imported=False):
    """
    files: list of (filename, bytes) tuples, exactly as received from
    the HTTP upload.
    product_type: which compliance rule set to apply (see
    compliance_engine.PRODUCT_CATEGORIES) - falls back to "other".
    is_imported: whether the user flagged this as an imported package
    (enables the Rule 27 country-of-origin/importer checks).

    Returns a dict with per-image results, the merged/de-duplicated
    text, the structured fields, and a compliance verdict - and writes
    a full session folder to disk under backend/sessions/.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    session_dir = os.path.join(SESSIONS_DIR, f"session_{timestamp}")
    images_dir = os.path.join(session_dir, "images")
    raw_dir = os.path.join(session_dir, "raw_ocr")
    os.makedirs(images_dir, exist_ok=True)
    os.makedirs(raw_dir, exist_ok=True)

    all_images_lines = []
    per_image_results = []
    images_by_name = {}

    for i, (filename, content) in enumerate(files, 1):
        ext = os.path.splitext(filename)[1] or ".jpg"
        safe_name = f"img_{i}{ext}"
        image_path = os.path.join(images_dir, safe_name)
        with open(image_path, "wb") as f:
            f.write(content)

        processed = preprocess_image_bytes(content)
        lines = extract_text_with_confidence(processed, lang=lang)
        all_images_lines.append((safe_name, lines))
        images_by_name[safe_name] = processed

        raw_out = os.path.join(raw_dir, f"img_{i}_raw_paddleocr.json")
        with open(raw_out, "w", encoding="utf-8") as f:
            json.dump({"source_image": safe_name, "lines": lines}, f, indent=2, ensure_ascii=False)

        per_image_results.append({
            "filename": filename,
            "saved_as": safe_name,
            "line_count": len(lines),
            "lines": lines,
        })

    merged_lines = merge_and_dedupe_lines(all_images_lines)

    combined_raw = {
        "session_timestamp": timestamp,
        "images": [name for name, _ in all_images_lines],
        "total_lines_before_dedup": sum(len(lines) for _, lines in all_images_lines),
        "total_lines_after_dedup": len(merged_lines),
        "merged_lines": merged_lines,
    }
    with open(os.path.join(session_dir, "combined_raw_ocr.json"), "w", encoding="utf-8") as f:
        json.dump(combined_raw, f, indent=2, ensure_ascii=False)

    structured = structure_label_fields(merged_lines)
    full_text = "\n".join(line["text"] for line in order_lines_reading_order(merged_lines))
    visual_checks, annotations = visual_engine.run_visual_checks(
        images_by_name, merged_lines, structured,
    )
    compliance = compliance_engine.run_compliance_checks(
        structured, full_text, product_type=product_type, is_imported=is_imported,
        visual_checks=visual_checks,
    )

    # Draw whatever was flagged directly onto the photo it came from,
    # so a reviewer can see exactly where the problem is without
    # cross-referencing coordinates against the checklist.
    annotated_images = {}
    for image_name, findings in annotations.items():
        base_image = images_by_name.get(image_name)
        if base_image is None or not findings:
            continue
        annotated = visual_engine.annotate_image(base_image, findings)
        cv2.imwrite(os.path.join(images_dir, f"annotated_{image_name}"), annotated)
        data_uri = visual_engine.encode_jpeg_data_uri(annotated)
        if data_uri:
            annotated_images[image_name] = data_uri

    with open(os.path.join(session_dir, "structured_fields.json"), "w", encoding="utf-8") as f:
        json.dump({
            "session_timestamp": timestamp,
            "product_type": compliance["product_type"],
            "structured_fields": structured,
            "compliance": compliance,
            "annotated_images": list(annotated_images.keys()),
            "visual_findings": annotations,
            "all_deduped_lines": [l["text"] for l in merged_lines],
        }, f, indent=2, ensure_ascii=False)

    return {
        "session_id": f"session_{timestamp}",
        "per_image_results": per_image_results,
        "combined_raw": combined_raw,
        "structured_fields": structured,
        "compliance": compliance,
        "annotated_images": annotated_images,
    }