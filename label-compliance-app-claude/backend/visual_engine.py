"""
visual_engine.py

OpenCV-based checks that look at the PHOTO itself rather than just the
OCR'd text - legibility, layout crowding, and photo sharpness. These
sit alongside compliance_engine.py's text-based checks and use the
same check-row schema, so the frontend renders both with no
special-casing.

What's checked:

  - Contrast between each key declaration's text and its background,
    per Rule 9(1)(b) ("numerals ... printed ... in a colour that
    contrasts conspicuously with the background of the label").

  - Free space around the net quantity declaration, per Rule 8(1)
    (clear of other print by at least one numeral-height above/below,
    two numeral-heights either side). Pure pixel geometry - just
    comparing the net quantity's own bounding box against everything
    else detected on the same photo.

  - Overlapping/crowded print in general - OCR bounding boxes that
    collide, which usually means text is printed over itself or
    crammed together on the pack (Rule 9(1)(a), legible & prominent).

  - Photo sharpness (blur detection) - not a legal requirement, but
    it tells you how much to trust every other check on this scan.

Anything flagged above a clean pass gets drawn directly onto the
photo it came from (annotate_image()) - a coloured box with a short
label - so a reviewer can see exactly where the problem is without
cross-referencing coordinates against the checklist.

IMPORTANT: same caveat as compliance_engine.py - this is a heuristic
first pass, not a certified measurement. Always confirm anything
flagged here against the real package.

NOTE: this module used to also check numeral/letter height against
Rule 7(2) & Table-I, which needs a real-world size reference. That
required a two-step "measure the highlighted edge on your photo"
flow, which has been removed - the print-size check is no longer
performed at all rather than asking the person for a measurement.
"""

import cv2
import numpy as np
import base64

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_WARN = "warn"
STATUS_INFO = "info"

# Which extracted fields get the per-declaration visual checks
# (contrast). Kept short and deliberate - these are the declarations
# Legal Metrology actually cares about the visual prominence of, not
# every line of OCR text on the pack.
KEY_DECLARATION_FIELDS = [
    "net_quantity", "mrp", "mfg_date", "use_by_or_expiry_date",
    "manufacturer_or_packer", "consumer_care", "fssai_or_license_no", "lot_no",
]

def _vcheck(check_id, label, citation, status, message, required=False):
    """Same shape as compliance_engine._check() so the frontend needs
    no special-casing between text-based and visual-based checks."""
    return {
        "id": check_id, "label": label, "citation": citation,
        "status": status, "message": message, "required": required,
    }


# ----------------------------------------------------------------------
# Bounding box normalizing - PaddleOCR boxes can come back either as a
# flat [x1, y1, x2, y2] rectangle or as four corner points
# [[x,y],[x,y],[x,y],[x,y]] depending on version/pipeline config. Every
# function below goes through this so a format change upstream doesn't
# silently break height/contrast/overlap math.
# ----------------------------------------------------------------------
def _bbox_to_rect(bbox):
    if not bbox:
        return None
    try:
        if all(not isinstance(v, (list, tuple)) for v in bbox) and len(bbox) == 4:
            x1, y1, x2, y2 = [float(v) for v in bbox]
            return (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
        xs = [float(p[0]) for p in bbox]
        ys = [float(p[1]) for p in bbox]
        return (min(xs), min(ys), max(xs), max(ys))
    except (TypeError, ValueError, IndexError):
        return None




def check_contrast(field_key, label, image, bbox):
    """
    Crude "is this conspicuous" proxy: crop the declaration's bbox,
    Otsu-threshold it into foreground/background, and measure the
    luminance gap. A wide gap means the text clearly stands out; a
    narrow gap means it may blend into the background.
    """
    rect = _bbox_to_rect(bbox)
    if image is None or rect is None:
        return None, None
    x1, y1, x2, y2 = [int(v) for v in rect]
    x1, y1 = max(x1, 0), max(y1, 0)
    crop = image[y1:y2, x1:x2]
    if crop.size == 0 or crop.shape[0] < 4 or crop.shape[1] < 4:
        return None, None

    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    _, mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    fg = gray[mask == 255]
    bg = gray[mask == 0]
    if fg.size == 0 or bg.size == 0:
        return None, None

    contrast = abs(float(fg.mean()) - float(bg.mean()))
    status = STATUS_PASS if contrast >= 60 else (STATUS_WARN if contrast >= 35 else STATUS_FAIL)
    message = f"Text-to-background contrast \u2248{contrast:.0f}/255."
    if status != STATUS_PASS:
        message += " May not be conspicuous enough at a glance - check under normal light."
    check = _vcheck(
        f"{field_key}_contrast", f"{label} \u2014 Legibility",
        "LMPC Rules 2011, Rule 9(1)(b) (numerals must contrast conspicuously with the label)",
        status, message, required=False,
    )
    return check, (rect if status != STATUS_PASS else None)


def check_sharpness(image):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    variance = cv2.Laplacian(gray, cv2.CV_64F).var()
    status = STATUS_PASS if variance >= 80 else (STATUS_WARN if variance >= 30 else STATUS_FAIL)
    message = f"Sharpness score \u2248{variance:.0f}."
    if status != STATUS_PASS:
        message += " Photo looks soft/blurry - every check above is less reliable. Retake in better light or hold steadier."
    return _vcheck(
        "photo_sharpness", "Photo Sharpness",
        "Image quality check (not a legal requirement) — affects reliability of every other check",
        status, message, required=False,
    )


def _iou(box_a, box_b):
    rect_a, rect_b = _bbox_to_rect(box_a), _bbox_to_rect(box_b)
    if rect_a is None or rect_b is None:
        return 0.0
    ax1, ay1, ax2, ay2 = rect_a
    bx1, by1, bx2, by2 = rect_b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    if inter == 0:
        return 0.0
    area_a = (ax2 - ax1) * (ay2 - ay1)
    area_b = (bx2 - bx1) * (by2 - by1)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def check_overlap(merged_lines):
    """Flags OCR lines whose bounding boxes overlap heavily - usually
    means print is crowded or overlapping on the actual label. Returns
    (check, [(image_name, rect), ...]) - the second element is empty
    on a clean pass."""
    boxes = [(l.get("bbox"), l.get("text"), l.get("source_image")) for l in merged_lines if l.get("bbox")]
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            if _iou(boxes[i][0], boxes[j][0]) > 0.25:
                check = _vcheck(
                    "print_crowding", "Print Layout",
                    "LMPC Rules 2011, Rule 9(1)(a) (declarations must be legible & prominent)",
                    STATUS_WARN,
                    f"\u201c{boxes[i][1]}\u201d and \u201c{boxes[j][1]}\u201d overlap in the photo - "
                    f"text may be crowded or printed over itself.",
                    required=False,
                )
                flagged = []
                for bbox, _text, image_name in (boxes[i], boxes[j]):
                    rect = _bbox_to_rect(bbox)
                    if rect and image_name:
                        flagged.append((image_name, rect))
                return check, flagged
    check = _vcheck(
        "print_crowding", "Print Layout",
        "LMPC Rules 2011, Rule 9(1)(a) (declarations must be legible & prominent)",
        STATUS_PASS, "No significantly overlapping text regions detected.", required=False,
    )
    return check, []


def check_net_quantity_free_space(net_qty_line, other_lines):
    """
    LMPC Rules 2011, Rule 8(1): the area around the net quantity
    declaration must be free of other printed matter - at least one
    numeral-height of clear space above/below, and two numeral-heights
    to the left/right. Pure pixel geometry - needs no physical
    measurement.
    """
    target = _bbox_to_rect(net_qty_line.get("bbox"))
    if target is None:
        return None, None
    tx1, ty1, tx2, ty2 = target
    h = ty2 - ty1
    if h <= 0:
        return None, None

    required_zone = (tx1 - 2 * h, ty1 - h, tx2 + 2 * h, ty2 + h)
    intrusions = []
    for line in other_lines:
        if line is net_qty_line:
            continue
        rect = _bbox_to_rect(line.get("bbox"))
        if rect is None or rect == target:
            continue
        rx1, ry1, rx2, ry2 = rect
        zx1, zy1, zx2, zy2 = required_zone
        overlaps_zone = not (rx2 <= zx1 or zx2 <= rx1 or ry2 <= zy1 or zy2 <= ry1)
        overlaps_target = not (rx2 <= tx1 or tx2 <= rx1 or ry2 <= ty1 or ty2 <= ry1)
        if overlaps_zone and not overlaps_target:
            intrusions.append(line.get("text", "").strip())

    if intrusions:
        sample = ", ".join(f"\u201c{t}\u201d" for t in intrusions[:3])
        check = _vcheck(
            "net_quantity_free_space", "Net Quantity \u2014 Free Space Around Declaration",
            "LMPC Rules 2011, Rule 8(1) (clear space: 1x height above/below, 2x height either side)",
            STATUS_WARN,
            f"Other printed text sits inside the required clear zone around the net "
            f"quantity numerals ({sample}).",
            required=True,
        )
        return check, target
    check = _vcheck(
        "net_quantity_free_space", "Net Quantity \u2014 Free Space Around Declaration",
        "LMPC Rules 2011, Rule 8(1) (clear space: 1x height above/below, 2x height either side)",
        STATUS_PASS, "No other detected text intrudes on the required clear space.",
        required=True,
    )
    return check, None


# ----------------------------------------------------------------------
# Highlighting problems directly on the photo
# ----------------------------------------------------------------------
_ANNOTATION_COLOR = {"fail": (43, 57, 192), "warn": (30, 140, 240)}  # BGR: red, amber


def annotate_image(image_bgr, findings):
    """
    findings: [{"rect": (x1,y1,x2,y2), "label": str, "level": "fail"|"warn"}, ...]
    Draws a coloured box + label near each finding. Returns a NEW image
    (the input is never modified) so the original stays untouched on disk.
    Labels that would otherwise collide (e.g. two findings on the same
    small region, like net quantity failing both contrast and margin)
    are stacked instead of overlapping illegibly.
    """
    out = image_bgr.copy()
    placed_label_rects = []  # (lx1, ly1, lx2, ly2) already occupied, for collision checks

    def rects_overlap(a, b):
        ax1, ay1, ax2, ay2 = a
        bx1, by1, bx2, by2 = b
        return not (ax2 <= bx1 or bx2 <= ax1 or ay2 <= by1 or by2 <= ay1)

    # Boxes first, so every rectangle is visible under the labels drawn next.
    for f in findings:
        x1, y1, x2, y2 = [int(v) for v in f["rect"]]
        color = _ANNOTATION_COLOR.get(f.get("level"), _ANNOTATION_COLOR["warn"])
        thickness = max(2, out.shape[1] // 500)
        cv2.rectangle(out, (x1, y1), (x2, y2), color, thickness)

    for f in findings:
        label = f.get("label", "")
        if not label:
            continue
        x1, y1, x2, y2 = [int(v) for v in f["rect"]]
        color = _ANNOTATION_COLOR.get(f.get("level"), _ANNOTATION_COLOR["warn"])
        font_scale = max(0.5, out.shape[1] / 1600)
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, font_scale, 2)

        # Start just above the box; if that spot's already taken by
        # another label, keep stepping up until it's clear (or give up
        # after a few tries and place it anyway rather than loop forever).
        ty = max(th + 6, y1 - 8)
        label_rect = (x1, ty - th - 6, x1 + tw + 8, ty + 4)
        attempts = 0
        while any(rects_overlap(label_rect, placed) for placed in placed_label_rects) and attempts < 6:
            ty -= (th + 10)
            label_rect = (x1, ty - th - 6, x1 + tw + 8, ty + 4)
            attempts += 1

        placed_label_rects.append(label_rect)
        cv2.rectangle(out, (x1, ty - th - 6), (x1 + tw + 8, ty + 4), color, -1)
        cv2.putText(out, label, (x1 + 4, ty), cv2.FONT_HERSHEY_SIMPLEX,
                    font_scale, (255, 255, 255), 2, cv2.LINE_AA)
    return out


def encode_jpeg_data_uri(image_bgr, quality=88):
    ok, buf = cv2.imencode(".jpg", image_bgr, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        return None
    return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode("ascii")


# ----------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------
def find_line_for(value, merged_lines):
    value = str(value or "").strip()
    if len(value) < 3:
        return None
    value_low = value.lower()
    for line in merged_lines:
        if value_low in line["text"].lower():
            return line
    return None


def run_visual_checks(images_by_name, merged_lines, fields):
    """
    images_by_name: {source_image_filename: cv2 BGR ndarray} - the
                     already-preprocessed images ocr_engine produced.
    merged_lines: deduped OCR line dicts (text, bbox, source_image, confidence)
    fields: structured_fields dict from ocr_engine.structure_label_fields()

    Returns (checks, annotations):
      checks: list of check dicts, same shape as compliance_engine._check()
      annotations: {source_image_filename: [{"rect","label","level"}, ...]}
                   - everything that should get drawn on the photo, so a
                   reviewer can see exactly where a flagged issue is
                   without hunting through the checklist.
    """
    checks = []
    annotations = {}

    def flag(image_name, rect, label, level):
        if image_name and rect:
            annotations.setdefault(image_name, []).append(
                {"rect": rect, "label": label, "level": level}
            )

    if not images_by_name:
        return checks, annotations

    # One sharpness check per scan: report the worst of the photos, so
    # a single blurry shot in a multi-photo scan still gets flagged.
    # No bounding box to draw here - blur is a whole-photo property,
    # not a localized region.
    severity_rank = {"fail": 0, "warn": 1, "pass": 2}
    sharpness_results = [check_sharpness(img) for img in images_by_name.values()]
    checks.append(min(sharpness_results, key=lambda c: severity_rank[c["status"]]))

    overlap_check, overlap_flags = check_overlap(merged_lines)
    checks.append(overlap_check)
    for image_name, rect in overlap_flags:
        flag(image_name, rect, "Crowded print", "warn")

    net_qty_line = None
    for field_key, entry in (fields or {}).items():
        if field_key not in KEY_DECLARATION_FIELDS or not isinstance(entry, dict):
            continue
        value = entry.get("value")
        line = find_line_for(value, merged_lines)
        if not line:
            continue
        image_name = line.get("source_image")
        image = images_by_name.get(image_name)
        label = field_key.replace("_", " ").title()

        contrast_check, contrast_rect = check_contrast(field_key, label, image, line.get("bbox"))
        if contrast_check:
            checks.append(contrast_check)
            flag(image_name, contrast_rect, f"{label}: low contrast", contrast_check["status"])

        if field_key == "net_quantity":
            net_qty_line = line

    # Rule 8(1) free-space check - pure pixel geometry, no physical
    # measurement needed, so it runs whenever the net quantity
    # declaration itself was found and located on a photo.
    if net_qty_line:
        source_image = net_qty_line.get("source_image")
        same_image_lines = [l for l in merged_lines if l.get("source_image") == source_image]
        margin_check, margin_rect = check_net_quantity_free_space(net_qty_line, same_image_lines)
        if margin_check:
            checks.append(margin_check)
            flag(source_image, margin_rect, "Crowded margin", "warn")

    return checks, annotations