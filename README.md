# Vendor Price Sheet → Jobber Import ETL

Turns manufacturer price sheets (Excel or CSV, any layout) into the 5-column CSV that
Jobber's Products & Services import expects: `Name, Description, Cost, Taxable, Category`.

## Run it

```
pip install -r requirements.txt
python jobber_etl.py samples --out jobber_out
```

Point it at a folder or at individual files. For each input it writes:

| File | What's in it |
|---|---|
| `<file>_Jobber_Import.csv` | Rows ready to upload to Jobber |
| `<file>_rejects.csv` | Rows that were dropped, each with a reason |
| `run_report.json` | One entry per file: vendor matched, columns used, row counts |

Files with "Jobber" in the name are skipped, so the output folder can be re-scanned safely.

## Adding a vendor

Add an entry to `vendors.json`. No code changes:

```jsonc
{
  "name": "New Vendor",
  "match": ["newvendor"],          // substring of the file name
  "prefix": "NV",                  // Jobber Name becomes "NV <SKU>"
  "sku_columns": ["Part #"],       // candidates, first one found wins
  "description_columns": ["Description"],
  "price_columns": ["MSRP", "List"],
  "price_multiplier": 1.0          // e.g. 1.35 to mark up dealer cost
}
```

## What it cleans up

- Title rows above the header, multiple sheets, CSVs in UTF-8 or Windows-1252
- Prices written as text (`"6,022.00"`)
- Stray control characters from Windows exports, curly quotes, `’’` used as an inch mark, escaped `\#` and `\&`
- SKUs that are already prefixed (re-runs don't produce `HHT HHT 0000191`)

Rows with no SKU, no price, or a $0 price, and duplicate SKUs, go to the rejects file instead of being silently dropped.

## Samples

`samples/` holds a handful of real catalog rows with known problems in them. The dealer
net price column has been removed from the AFD sample.
