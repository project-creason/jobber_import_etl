"""Browser glue for the demo: runs jobber_etl on one file and returns JSON for the page.

Uses the same extract/transform functions as the command-line tool, so the demo
and the repo code can't drift apart.
"""
import json
import re
from pathlib import Path

import pandas as pd

import jobber_etl as J

VENDORS = J.load_vendors(Path("/app/vendors.json"))


def vendor_names():
    return json.dumps([v.name for v in VENDORS])


def _fmt_price(p):
    return f"{p:g}" if p == int(p) else f"{p:.2f}"


def run(path, vendor_choice="auto"):
    p = Path(path)
    if "jobber" in p.name.lower():
        return json.dumps({"status": "skipped", "message": "This file name contains \"Jobber\", so it's treated as an import that's already done. Rename it to process it."})
    if vendor_choice == "auto":
        v = J.pick_vendor(p, VENDORS)
        if v is None:
            return json.dumps({"status": "needs_vendor", "message": "No vendor rule matches this file name. Pick the vendor below and run it again."})
    else:
        v = next(x for x in VENDORS if x.name == vendor_choice)
    try:
        df = J.extract(p, v)
        res = J.transform(df, v)
    except Exception as e:  # show the reason instead of a stack trace
        return json.dumps({"status": "error", "message": str(e)})

    sku_c = J._first_present(df, v.sku_columns)
    desc_c = J._first_present(df, v.description_columns)
    price_c = J._first_present(df, v.price_columns)
    changes = []
    for i, r in df.iterrows():
        sku = J.clean_text(r.get(sku_c))
        if not sku:
            continue
        raw_d = r.get(desc_c)
        if isinstance(raw_d, str) and raw_d.strip() and J.clean_text(raw_d) != raw_d.strip():
            changes.append({"row": i + 2, "sku": sku, "field": "Description", "before": raw_d.strip(), "after": J.clean_text(raw_d)})
        raw_p = r.get(price_c)
        num = J.parse_price(raw_p)
        if isinstance(raw_p, str) and num is not None and re.search(r"[^\d.\s]", raw_p):
            changes.append({"row": i + 2, "sku": sku, "field": "Price", "before": raw_p.strip(), "after": _fmt_price(round(num * v.price_multiplier, v.round_to))})

    rejects = res.rejects.fillna("").astype(str).to_dict("records") if len(res.rejects) else []
    return json.dumps({
        "status": "ok",
        "vendor": v.name,
        "columns": {"sku": sku_c, "description": desc_c, "price": price_c},
        "rows_in": int(df.dropna(how="all").shape[0]),
        "good": res.good.to_dict("records"),
        "rejects": rejects,
        "changes": changes[:300],
        "csv": res.good.to_csv(index=False, lineterminator="\r\n"),
    })
