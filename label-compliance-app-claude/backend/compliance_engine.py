"""
compliance_engine.py

Rule-based compliance checker that runs on top of the fields already
pulled out by ocr_engine.structure_label_fields(). It doesn't do any
new OCR/text work - it just judges what was found (or not found)
against the mandatory declarations that actually apply, and explains
*why* under which rule.

Legal basis encoded here:

  - Legal Metrology (Packaged Commodities) Rules, 2011 ("LMPC Rules")
    Rule 6 - applies to EVERY pre-packaged commodity sold in India:
      6(1)(a) name & address of manufacturer/packer/importer
      6(1)(b) common/generic name + net quantity
      6(1)(c) lot/batch/code number
      6(1)(d) month & year of manufacture/pack/import
      6(1)(e) retail sale price, inclusive of all taxes
      6(1)(f) name/address/telephone/email for consumer complaints
    Rule 27 - additional declarations required specifically on
      imported packages (country of origin, importer details).

  - Category add-ons (only checked when relevant, via product_type):
      food        -> FSS Act 2006 + FSS (Packaging & Labelling)
                     Regulations, 2020 (FSSAI license, ingredients,
                     allergen declaration, veg/non-veg mark)
      cosmetics   -> Drugs & Cosmetics Rules 1945, Rule 148
      drugs       -> Drugs & Cosmetics Rules 1945, Rule 96
      electronics -> LMPC Rules only, + an informational BIS note

IMPORTANT: this is decision support built on heuristic OCR + regex
extraction, not a legal determination. A "fail" here means "this
wasn't detected in the scanned text" - it does NOT necessarily mean
the label is actually non-compliant (the OCR may have missed it, or
it may be printed as a logo/symbol this pipeline can't read). Always
treat this as a checklist to verify by hand, not a certificate.
"""

import re
from datetime import datetime

import label_parser

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_WARN = "warn"
STATUS_INFO = "info"

PRODUCT_CATEGORIES = {
    "food": "Food & Beverage",
    "cosmetics": "Cosmetics & Personal Care",
    "drugs": "Drugs & Pharmaceuticals",
    "electronics": "Electronics & Appliances",
    "other": "General / Other Packaged Commodity",
}


def _parse_date_safe(date_str):
    """Parses a label date into a datetime via the shared label_parser
    (full dates, month-year, month names, ISO) or None if unparseable.
    Kept as a tiny wrapper so all call sites stay unchanged."""
    return label_parser.parse_date(date_str)


def _check(check_id, label, citation, status, message, required=True):
    """
    required=True means this is a declaration the law actually mandates
    for the applicable product/package - it counts toward the
    "X/N required checks passed" summary regardless of whether it
    passed. required=False is for genuinely conditional/informational
    items (e.g. "country of origin" when the product isn't imported,
    or anything OCR fundamentally can't verify like a logo/symbol) -
    still shown to the inspector, just not held against the pass count.
    """
    return {
        "id": check_id, "label": label, "citation": citation,
        "status": status, "message": message, "required": required,
    }


def _summarize(checks, product_type, is_imported):
    """
    Computes the verdict/summary from a finished checks list.
    """
    # Overall verdict is driven by REQUIRED checks only - a conditional/
    # informational item (e.g. an unverifiable BIS logo, or country of
    # origin noted on a non-imported product) shouldn't tank the verdict
    # on its own. Non-required items are still shown to the inspector,
    # just not held against the pass/fail determination.
    required_checks = [c for c in checks if c["required"]]
    required_total = len(required_checks)
    required_passed = sum(1 for c in required_checks if c["status"] == STATUS_PASS)
    required_fail_n = sum(1 for c in required_checks if c["status"] == STATUS_FAIL)
    required_warn_n = sum(1 for c in required_checks if c["status"] == STATUS_WARN)

    pass_n = sum(1 for c in checks if c["status"] == STATUS_PASS)
    fail_n = sum(1 for c in checks if c["status"] == STATUS_FAIL)
    warn_n = sum(1 for c in checks if c["status"] == STATUS_WARN)
    info_n = sum(1 for c in checks if c["status"] == STATUS_INFO)

    if required_fail_n > 0:
        overall_status = "non_compliant"
    elif required_warn_n > 0:
        overall_status = "needs_review"
    else:
        overall_status = "compliant"

    return {
        "product_type": product_type,
        "product_type_label": PRODUCT_CATEGORIES.get(product_type, product_type),
        "is_imported": bool(is_imported),
        "overall_status": overall_status,
        "required_total": required_total,
        "required_passed": required_passed,
        "summary": {"pass": pass_n, "fail": fail_n, "warn": warn_n, "info": info_n, "total": len(checks)},
        "checks": checks,
    }


def run_compliance_checks(fields, full_text, product_type="other", is_imported=False, visual_checks=None):
    """
    fields: the dict returned by ocr_engine.structure_label_fields()
    full_text: the merged/deduped OCR text (joined), used for checks
               that need context beyond a single extracted field
               (e.g. spotting the "inclusive of all taxes" wording).
    product_type: one of the keys in PRODUCT_CATEGORIES
    is_imported: whether the user flagged this as an imported package
    visual_checks: optional list of check dicts from visual_engine.py
               (photo-based checks: print size, contrast, layout,
               sharpness) - same shape as the text-based checks below,
               so they're merged straight into the same list/summary.
    """
    full_text = full_text or ""
    checks = []

    # ---------------- Universal declarations: LMPC Rules 2011, Rule 6 ----------------
    if fields.get("manufacturer_or_packer"):
        checks.append(_check(
            "manufacturer_or_packer", "Manufacturer / packer / importer name & address",
            "LMPC Rules 2011, Rule 6(1)(a)", STATUS_PASS,
            f"Found: \u201c{fields['manufacturer_or_packer']['value']}\u201d."
        ))
    else:
        checks.append(_check(
            "manufacturer_or_packer", "Manufacturer / packer / importer name & address",
            "LMPC Rules 2011, Rule 6(1)(a)", STATUS_FAIL,
            "Not found. Every pre-packaged commodity must declare the name and "
            "address of the manufacturer, packer, or importer."
        ))

    if fields.get("net_quantity"):
        # Rule 8 demands standard metric units (g, kg, ml, l etc.) - a
        # value in a non-metric or made-up unit is a declaration gap even
        # though a quantity WAS found, so flag it as incomplete rather
        # than quietly passing.
        nq_value = fields["net_quantity"]["value"].lower()
        standard_units = (
            "g", "gm", "gms", "gr", "gram", "grams", "kg", "kgs",
            "ml", "millilitre", "millilitres", "l", "lt", "ltr", "ltrs",
            "litre", "litres", "cl", "mg",
        )
        nq_unit_ok = any(nq_value.endswith(u) for u in standard_units)
        if nq_unit_ok:
            checks.append(_check(
                "net_quantity", "Net quantity", "LMPC Rules 2011, Rule 6(1)(b) & Rule 8",
                STATUS_PASS, f"Found: {fields['net_quantity']['value']}, declared in standard metric units."
            ))
        else:
            checks.append(_check(
                "net_quantity", "Net quantity", "LMPC Rules 2011, Rule 6(1)(b) & Rule 8",
                STATUS_WARN,
                f"Found: \u201c{fields['net_quantity']['value']}\u201d but the unit isn't a "
                "standard metric unit (g, kg, ml, l). Rule 8 requires metric units - verify manually."
            ))
    else:
        checks.append(_check(
            "net_quantity", "Net quantity", "LMPC Rules 2011, Rule 6(1)(b) & Rule 8",
            STATUS_FAIL,
            "Not found. Net quantity (weight/volume/number) must be declared in "
            "standard metric units (g, kg, ml, l)."
        ))

    if fields.get("mrp"):
        # ocr_engine now records the "inclusive of all taxes" flag right
        # next to the price; still fall back to scanning the raw text for
        # older sessions or when the extractor didn't record it.
        tax_inclusive = bool(fields["mrp"].get("tax_inclusive")) or bool(
            re.search(r'incl(?:usive|\.)?\s+(?:of\s+)?(?:all\s+)?tax', full_text, re.IGNORECASE)
        )
        if tax_inclusive:
            checks.append(_check(
                "mrp", "Retail sale price (MRP)", "LMPC Rules 2011, Rule 6(1)(e)", STATUS_PASS,
                f"Found: {fields['mrp']['value']}, with an \u201cinclusive of all taxes\u201d qualifier."
            ))
        else:
            checks.append(_check(
                "mrp", "Retail sale price (MRP)", "LMPC Rules 2011, Rule 6(1)(e)", STATUS_WARN,
                f"Price found ({fields['mrp']['value']}) but the mandatory \u201cinclusive "
                f"of all taxes\u201d wording wasn't detected nearby - confirm it's on the pack."
            ))
    else:
        checks.append(_check(
            "mrp", "Retail sale price (MRP)", "LMPC Rules 2011, Rule 6(1)(e)", STATUS_FAIL,
            "Not found. The MRP, inclusive of all taxes, must be declared."
        ))

    if fields.get("mfg_date"):
        checks.append(_check(
            "mfg_date", "Month & year of manufacture / pack / import",
            "LMPC Rules 2011, Rule 6(1)(d)", STATUS_PASS,
            f"Found: {fields['mfg_date']['value']}."
        ))
    elif fields.get("date_detected_unclassified"):
        # ocr_engine finds this when a date-like string exists on the
        # pack but isn't sitting next to an "MFD/MFG/USE BY" keyword -
        # common on labels that say "MFD. & Batch No.: See coding" and
        # print the real date as a separate laser-coded stamp elsewhere.
        # That's still a declared date, just not confidently classified
        # as mfg vs. expiry - a WARN to verify by hand is more honest
        # here than silently reporting "not found" when something WAS
        # found on the pack.
        checks.append(_check(
            "mfg_date", "Month & year of manufacture / pack / import",
            "LMPC Rules 2011, Rule 6(1)(d)", STATUS_WARN,
            f"A date ({fields['date_detected_unclassified']['value']}) was found on the pack, "
            f"but not next to an \u201cMFD/MFG\u201d keyword - it may be a separate coded/stamped "
            f"date rather than the printed declaration. Verify it's the manufacture/pack date."
        ))
    else:
        checks.append(_check(
            "mfg_date", "Month & year of manufacture / pack / import",
            "LMPC Rules 2011, Rule 6(1)(d)", STATUS_FAIL,
            "Not found. The month and year of manufacture, packing, or import must be declared."
        ))

    contact_parts = [f["value"] for f in (
        fields.get("consumer_care"), fields.get("toll_free_number"), fields.get("email")
    ) if f]
    if contact_parts:
        checks.append(_check(
            "consumer_care", "Consumer care / grievance contact",
            "LMPC Rules 2011, Rule 6(1)(f)", STATUS_PASS,
            "Found: " + "; ".join(contact_parts)
        ))
    else:
        checks.append(_check(
            "consumer_care", "Consumer care / grievance contact",
            "LMPC Rules 2011, Rule 6(1)(f)", STATUS_FAIL,
            "Not found. A name, address, and telephone/email for consumer "
            "complaints must be declared."
        ))

    if fields.get("lot_no"):
        checks.append(_check(
            "lot_no", "Lot / batch number", "LMPC Rules 2011, Rule 6(1)(c)", STATUS_PASS,
            f"Found: {fields['lot_no']['value']}."
        ))
    else:
        checks.append(_check(
            "lot_no", "Lot / batch number", "LMPC Rules 2011, Rule 6(1)(c)", STATUS_WARN,
            "Not detected by OCR. Required for traceability - check the physical "
            "pack before treating this as a confirmed gap."
        ))

    is_shelf_life_category = product_type in ("food", "drugs", "cosmetics")

    if fields.get("use_by_or_expiry_date"):
        checks.append(_check(
            "use_by_or_expiry_date", "Best before / use-by / expiry date",
            "LMPC Rules 2011, Rule 6(1)(d) / FSS Act (for food)", STATUS_PASS,
            f"Found: {fields['use_by_or_expiry_date']['value']}.",
            required=is_shelf_life_category
        ))
    else:
        severity = STATUS_FAIL if is_shelf_life_category else STATUS_WARN
        checks.append(_check(
            "use_by_or_expiry_date", "Best before / use-by / expiry date",
            "LMPC Rules 2011, Rule 6(1)(d) / FSS Act (for food)", severity,
            "Not found. Shelf-life-bound goods must declare a best before, use-by, or expiry date.",
            required=is_shelf_life_category
        ))

    mfg_dt = _parse_date_safe((fields.get("mfg_date") or {}).get("value"))
    exp_dt = _parse_date_safe((fields.get("use_by_or_expiry_date") or {}).get("value"))

    # An expiry that has already passed means the product is past its
    # shelf life - significant for food/drugs/cosmetics, informational
    # for everything else (an item can still legally be "compliant" as
    # a label while being old stock on a shelf).
    if exp_dt and exp_dt < datetime.now():
        checks.append(_check(
            "expiry_in_past", "Expiry / best-before has already passed",
            "Product state check", STATUS_WARN if is_shelf_life_category else STATUS_INFO,
            f"The declared expiry/best-before date ({fields['use_by_or_expiry_date']['value']}) "
            f"falls before today ({datetime.now().strftime('%d/%m/%Y')}) - the product appears "
            "to be past its shelf life. Verify before sale/consumption.",
            required=False
        ))

    # A manufacture date in the future is physically impossible - almost
    # certainly an OCR misread of the date digits, worth surfacing.
    if mfg_dt and mfg_dt > datetime.now():
        checks.append(_check(
            "mfg_in_future", "Manufacture date in the future",
            "Internal consistency check", STATUS_WARN,
            f"The declared manufacture date ({fields['mfg_date']['value']}) is after today - "
            "likely an OCR misread of the date digits. Verify manually.",
            required=False
        ))

    # Relative best-before ("Best before 9 months from packaging"):
    # if the mfg date is known, ocr_engine computed an implied expiry.
    rel_bb = fields.get("best_before_relative")
    if rel_bb:
        implied_dt = label_parser.parse_date(rel_bb.get("implied_expiry"))
        implied = implied_dt.strftime("%d/%m/%Y") if implied_dt else None
        if implied_dt and implied_dt < datetime.now():
            checks.append(_check(
                "relative_best_before", "Shelf life (relative best-before)",
                "Product state check", STATUS_WARN,
                f"\u201c{rel_bb['context']}\u201d applied to the manufacture date implies the "
                f"product is already past its best-before date ({implied}).",
                required=False
            ))
        elif implied_dt:
            checks.append(_check(
                "relative_best_before", "Shelf life (relative best-before)",
                "Internal consistency check", STATUS_PASS,
                f"\u201c{rel_bb['context']}\u201d applied to the manufacture date implies a "
                f"best-before date of {implied} - within shelf life.",
                required=False
            ))
        else:
            checks.append(_check(
                "relative_best_before", "Shelf life (relative best-before)",
                "LMPC Rules 2011, Rule 6(1)(d)", STATUS_INFO,
                f"Declared \u201c{rel_bb['context']}\u201d but no manufacture date was found to "
                "compute a concrete expiry from.",
                required=False
            ))

    if mfg_dt and exp_dt:
        if exp_dt > mfg_dt:
            checks.append(_check(
                "date_order", "Date consistency", "Internal consistency check", STATUS_PASS,
                "Expiry / use-by date falls after the manufacture date.",
                required=False
            ))
        else:
            checks.append(_check(
                "date_order", "Date consistency", "Internal consistency check", STATUS_FAIL,
                "The expiry/use-by date is not after the manufacture date - likely "
                "an OCR misread, or a genuine labelling error. Verify manually.",
                required=False
            ))

    # ---------------- Rule 27: additional declarations for imported goods ----------------
    if is_imported:
        if fields.get("country_of_origin"):
            checks.append(_check(
                "country_of_origin", "Country of origin (imported goods)",
                "LMPC Rules 2011, Rule 27", STATUS_PASS,
                f"Found: {fields['country_of_origin']['value']}."
            ))
        else:
            checks.append(_check(
                "country_of_origin", "Country of origin (imported goods)",
                "LMPC Rules 2011, Rule 27", STATUS_FAIL,
                "Not found. Imported packages must additionally declare the "
                "country of origin/manufacture/assembly, and the importer's own details."
            ))
    elif fields.get("country_of_origin"):
        checks.append(_check(
            "country_of_origin", "Country of origin", "LMPC Rules 2011, Rule 27", STATUS_INFO,
            f"Found: {fields['country_of_origin']['value']}.",
            required=False
        ))

    # ---------------- Category-specific add-ons ----------------
    if product_type == "food":
        if fields.get("fssai_or_license_no"):
            digits = re.sub(r'\D', '', fields["fssai_or_license_no"]["value"])
            if len(digits) == 14:
                checks.append(_check(
                    "fssai_or_license_no", "FSSAI license number",
                    "FSS (Packaging & Labelling) Regulations, 2020", STATUS_PASS,
                    f"Found: {fields['fssai_or_license_no']['value']} (14 digits)."
                ))
            else:
                checks.append(_check(
                    "fssai_or_license_no", "FSSAI license number",
                    "FSS (Packaging & Labelling) Regulations, 2020", STATUS_WARN,
                    f"Found \u201c{fields['fssai_or_license_no']['value']}\u201d but it isn't "
                    f"14 digits - a genuine FSSAI number always is. Likely an OCR misread."
                ))
        else:
            checks.append(_check(
                "fssai_or_license_no", "FSSAI license number",
                "FSS (Packaging & Labelling) Regulations, 2020", STATUS_FAIL,
                "Not found. All food businesses must print a 14-digit FSSAI "
                "license/registration number on the label."
            ))

        if fields.get("ingredients"):
            checks.append(_check(
                "ingredients", "List of ingredients",
                "FSS (Packaging & Labelling) Regulations, 2020, Reg. 5", STATUS_PASS,
                "Found an ingredients declaration."
            ))
        else:
            checks.append(_check(
                "ingredients", "List of ingredients",
                "FSS (Packaging & Labelling) Regulations, 2020, Reg. 5", STATUS_WARN,
                "Not detected. Required for multi-ingredient foods in descending "
                "order of composition; not applicable to genuinely single-ingredient products."
            ))

        checks.append(_check(
            "allergen_advice", "Allergen declaration",
            "FSS (Packaging & Labelling) Regulations, 2020, Reg. 8",
            STATUS_PASS if fields.get("allergen_advice") else STATUS_INFO,
            (f"Found: {fields['allergen_advice']['value']}" if fields.get("allergen_advice")
             else "Not detected. Only mandatory if the product actually contains a "
                  "declared allergen (milk, nuts, gluten, soy, etc.) - cross-check "
                  "against the ingredient list."),
            required=False
        ))

        checks.append(_check(
            "veg_nonveg_mark", "Veg / non-veg mark",
            "FSS (Packaging & Labelling) Regulations, 2020, Reg. 6", STATUS_INFO,
            "Can't be verified from OCR text - it's a green/brown dot symbol, not text. Confirm visually.",
            required=False
        ))

    elif product_type == "cosmetics":
        if fields.get("fssai_or_license_no"):
            checks.append(_check(
                "cosmetics_license", "Manufacturing / import license number",
                "Drugs & Cosmetics Rules 1945, Rule 148", STATUS_PASS,
                f"Found: {fields['fssai_or_license_no']['value']}."
            ))
        else:
            checks.append(_check(
                "cosmetics_license", "Manufacturing / import license number",
                "Drugs & Cosmetics Rules 1945, Rule 148", STATUS_WARN,
                "Not detected. Cosmetics must declare a manufacturing or import license number - confirm on the pack."
            ))

        checks.append(_check(
            "period_after_opening", "\u201cUse before\u201d period / PAO",
            "Drugs & Cosmetics Rules 1945, Rule 148",
            STATUS_PASS if fields.get("use_by_or_expiry_date") else STATUS_WARN,
            (f"Found: {fields['use_by_or_expiry_date']['value']}." if fields.get("use_by_or_expiry_date")
             else "Not found. Cosmetics should declare a use-before period or PAO (period-after-opening) symbol.")
        ))

    elif product_type == "drugs":
        if fields.get("fssai_or_license_no"):
            checks.append(_check(
                "drug_license", "Manufacturing license number",
                "Drugs & Cosmetics Rules 1945, Rule 96", STATUS_PASS,
                f"Found: {fields['fssai_or_license_no']['value']}."
            ))
        else:
            checks.append(_check(
                "drug_license", "Manufacturing license number",
                "Drugs & Cosmetics Rules 1945, Rule 96", STATUS_FAIL,
                "Not found. Every drug label must carry a manufacturing license number."
            ))

        if fields.get("lot_no"):
            checks.append(_check(
                "drug_batch", "Batch number", "Drugs & Cosmetics Rules 1945, Rule 96", STATUS_PASS,
                f"Found: {fields['lot_no']['value']}."
            ))
        else:
            checks.append(_check(
                "drug_batch", "Batch number", "Drugs & Cosmetics Rules 1945, Rule 96", STATUS_FAIL,
                "Not found. Drug labels must carry a batch number for recall traceability."
            ))

        if fields.get("use_by_or_expiry_date"):
            checks.append(_check(
                "drug_expiry", "Expiry date", "Drugs & Cosmetics Rules 1945, Rule 96", STATUS_PASS,
                f"Found: {fields['use_by_or_expiry_date']['value']}."
            ))
        else:
            checks.append(_check(
                "drug_expiry", "Expiry date", "Drugs & Cosmetics Rules 1945, Rule 96", STATUS_FAIL,
                "Not found. An expiry date is mandatory on every drug label."
            ))

    elif product_type == "electronics":
        checks.append(_check(
            "bis_mark", "BIS registration / mark",
            "BIS (Compulsory Registration) Order, as applicable", STATUS_INFO,
            "Can't be verified from OCR text - the BIS/CRS mark is a logo, not "
            "text. Confirm visually if this product falls under a notified category.",
            required=False
        ))

    # ---------------- Visual / physical checks (OpenCV, optional) ----------------
    # Merged in as-is: print-size is required=True (a real Rule 6 /
    # Second Schedule item once it's actually measurable), the rest
    # (contrast, layout, sharpness) are required=False supplementary
    # signals, same as an unverifiable logo elsewhere in this file.
    if visual_checks:
        checks.extend(visual_checks)

    # ---------------- Summary ----------------
    return _summarize(checks, product_type, is_imported)