#!/usr/bin/env python3
"""
Vendor price sheet -> Jobber Products & Services import ETL.

Usage:
    python jobber_etl.py <input files or folder> [--out OUTDIR] [--config vendors.json]

For every input file it writes, into OUTDIR:
    <stem>_Jobber_Import.csv   rows ready for Jobber (Name, Description, Cost, Taxable, Category)
    <stem>_rejects.csv         rows dropped, with the reason
and one run_report.json covering the whole run.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

JOBBER_COLUMNS = ["Name", "Description", "Cost", "Taxable", "Category"]
SUPPORTED = {".csv", ".xlsx", ".xlsm", ".xls"}


# --------------------------------------------------------------------------- config
@dataclass
class Vendor:
    name: str
    match: list[str]
    prefix: str
    sku_columns: list[str]
    description_columns: list[str]
    price_columns: list[str]
    taxable: str = "TRUE"
    category: str = "Product"
    price_multiplier: float = 1.0
    round_to: int = 2
    drop_zero_price: bool = True


def load_vendors(path: Path) -> list[Vendor]:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    defaults = cfg.get("defaults", {})
    out = []
    for v in cfg["vendors"]:
        merged = {**defaults, **{k: val for k, val in v.items() if not k.startswith("_")}}
        out.append(Vendor(**merged))
    return out


def pick_vendor(file: Path, vendors: list[Vendor]) -> Vendor | None:
    stem = file.name.lower()
    for v in vendors:
        if any(m.lower() in stem for m in v.match):
            return v
    return None


# --------------------------------------------------------------------------- extract
def _norm_header(h) -> str:
    return re.sub(r"\s+", " ", str(h)).strip().replace("\\#", "#").lower()


def _find_header_row(raw: pd.DataFrame, wanted: set[str], scan: int = 25) -> int:
    """Vendor sheets often start with logo/title rows. Pick the first row that
    contains at least two of the column names we expect."""
    for i in range(min(scan, len(raw))):
        cells = {_norm_header(c) for c in raw.iloc[i].tolist() if pd.notna(c)}
        if len(cells & wanted) >= 2:
            return i
    return 0


def _read_csv_any_encoding(path: Path) -> pd.DataFrame:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return pd.read_csv(path, header=None, dtype=str, encoding=enc, keep_default_na=False)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"Could not decode {path.name}")


def extract(path: Path, vendor: Vendor) -> pd.DataFrame:
    wanted = {_norm_header(c) for c in vendor.sku_columns + vendor.description_columns + vendor.price_columns}
    if path.suffix.lower() == ".csv":
        raw = _read_csv_any_encoding(path)
    else:
        # Read every sheet, keep the one whose header best matches.
        sheets = pd.read_excel(path, sheet_name=None, header=None, dtype=str)
        best, best_score = None, -1
        for df in sheets.values():
            row = _find_header_row(df, wanted)
            score = len({_norm_header(c) for c in df.iloc[row].tolist() if pd.notna(c)} & wanted) if len(df) else 0
            if score > best_score:
                best, best_score = df, score
        raw = best
    hdr = _find_header_row(raw, wanted)
    df = raw.iloc[hdr + 1:].copy()
    df.columns = [_norm_header(c) for c in raw.iloc[hdr].tolist()]
    return df.reset_index(drop=True)


# --------------------------------------------------------------------------- transform
_C1_CONTROLS = re.compile(r"[\u0080-\u009f]")
_QUOTE_MAP = str.maketrans({
    "“": '"', "”": '"', "″": '"',
    "‘": "'", "’": "'", "′": "'",
    "–": "-", "—": "-", " ": " ",
})


def clean_text(s) -> str:
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return ""
    s = str(s)
    s = unicodedata.normalize("NFC", s)
    s = s.replace("’’", '"')          # 9.69’’ -> 9.69"
    s = s.translate(_QUOTE_MAP)
    s = _C1_CONTROLS.sub("", s)                 # stray \x9d etc. from bad Windows exports
    s = s.replace("\\#", "#").replace("\\&", "&")
    s = re.sub(r'"{2,}', '"', s)                # 8"" -> 8"  (doubled quotes left in cell text)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def parse_price(s) -> float | None:
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return None
    t = re.sub(r"[^\d.\-]", "", str(s))
    if t in ("", ".", "-"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _first_present(df: pd.DataFrame, candidates: list[str]) -> str | None:
    for c in candidates:
        if _norm_header(c) in df.columns:
            return _norm_header(c)
    return None


@dataclass
class Result:
    good: pd.DataFrame
    rejects: pd.DataFrame
    notes: list[str] = field(default_factory=list)


def transform(df: pd.DataFrame, vendor: Vendor) -> Result:
    notes = []
    sku_c = _first_present(df, vendor.sku_columns)
    desc_c = _first_present(df, vendor.description_columns)
    price_c = _first_present(df, vendor.price_columns)
    missing = [n for n, c in (("SKU", sku_c), ("Description", desc_c), ("Price", price_c)) if c is None]
    if missing:
        raise ValueError(f"Missing column(s) {missing}; found {list(df.columns)}")
    notes.append(f"columns used: sku='{sku_c}', description='{desc_c}', price='{price_c}'")

    rows, rejects = [], []
    prefix_re = re.compile(rf"^{re.escape(vendor.prefix)}\s+", re.IGNORECASE)
    for i, r in df.iterrows():
        sku = clean_text(r.get(sku_c))
        sku = prefix_re.sub("", sku)            # input may already be prefixed (re-runs, old exports)
        desc = clean_text(r.get(desc_c))
        price = parse_price(r.get(price_c))
        src = i + 2
        if not sku:
            if desc or price is not None:
                rejects.append({"source_row": src, "reason": "missing SKU", "sku": sku, "description": desc, "raw_price": r.get(price_c)})
            continue                            # fully blank rows are skipped silently
        if price is None:
            rejects.append({"source_row": src, "reason": "missing/unparseable price", "sku": sku, "description": desc, "raw_price": r.get(price_c)})
            continue
        if price <= 0 and vendor.drop_zero_price:
            rejects.append({"source_row": src, "reason": "zero or negative price", "sku": sku, "description": desc, "raw_price": r.get(price_c)})
            continue
        cost = round(price * vendor.price_multiplier, vendor.round_to)
        rows.append({
            "Name": f"{vendor.prefix} {sku}",
            "Description": desc or sku,
            "Cost": f"{cost:g}" if cost == int(cost) else f"{cost:.{vendor.round_to}f}",
            "Taxable": vendor.taxable,
            "Category": vendor.category,
        })

    good = pd.DataFrame(rows, columns=JOBBER_COLUMNS)
    dupes = good[good.duplicated("Name", keep="last")]
    if len(dupes):
        for _, d in dupes.iterrows():
            rejects.append({"source_row": "", "reason": "duplicate Name (kept last occurrence)", "sku": d["Name"], "description": d["Description"], "raw_price": d["Cost"]})
        good = good.drop_duplicates("Name", keep="last")
    return Result(good.reset_index(drop=True), pd.DataFrame(rejects), notes)


# --------------------------------------------------------------------------- load
def load(res: Result, stem: str, out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    good_path = out / f"{stem}_Jobber_Import.csv"
    # utf-8-sig matches the existing Jobber files in the Drive folder and keeps Excel happy.
    res.good.to_csv(good_path, index=False, encoding="utf-8-sig", lineterminator="\r\n")
    rej_path = None
    if len(res.rejects):
        rej_path = out / f"{stem}_rejects.csv"
        res.rejects.to_csv(rej_path, index=False, encoding="utf-8-sig")
    return {"output": good_path.name, "rows_out": len(res.good), "rejects": len(res.rejects),
            "rejects_file": rej_path.name if rej_path else None, "notes": res.notes}


# --------------------------------------------------------------------------- driver
def run(inputs: list[Path], out: Path, cfg: Path) -> dict:
    vendors = load_vendors(cfg)
    files = []
    for p in inputs:
        files += sorted(f for f in p.iterdir() if f.suffix.lower() in SUPPORTED) if p.is_dir() else [p]
    report = {"files": []}
    for f in files:
        entry = {"input": f.name}
        if "jobber" in f.name.lower():
            entry["status"] = "skipped: already a Jobber import"
        elif (v := pick_vendor(f, vendors)) is None:
            entry["status"] = "skipped: no vendor rule matches this file name"
        else:
            try:
                res = transform(extract(f, v), v)
                entry.update({"vendor": v.name, "status": "ok", **load(res, f.stem, out)})
            except Exception as e:  # one bad file shouldn't stop the batch
                entry.update({"status": f"error: {e}"})
        report["files"].append(entry)
    (out / "run_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+", type=Path)
    ap.add_argument("--out", type=Path, default=Path("jobber_out"))
    ap.add_argument("--config", type=Path, default=Path(__file__).with_name("vendors.json"))
    a = ap.parse_args(argv)
    rep = run(a.inputs, a.out, a.config)
    for e in rep["files"]:
        print(f"{e['input']}: {e['status']}" + (f" -> {e['rows_out']} rows, {e['rejects']} rejects" if e.get("status") == "ok" else ""))


if __name__ == "__main__":
    main()
