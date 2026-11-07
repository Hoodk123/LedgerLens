# LedgerLens: Project Spec

Turn paper invoices into structured digital records, flag anomalies, store them, and generate reports.

## Stack
- Backend: Python, FastAPI, SQLAlchemy, SQLite (swap to Neon/Supabase Postgres at deploy)
- AI: Fireworks API (OpenAI-compatible), model GLM 5.3 Flash (backup: DeepSeek V4.1 Flash)
- Frontend: simple HTML + vanilla JS or small React/Astro app, served by FastAPI or Netlify
- Hosting: Hugging Face Space (Docker, port 7860)
- Secrets: FIREWORKS_API_KEY in environment only. Never commit. `.env` in .gitignore.

## Folder structure
```
ledgerlens/
  app/
    main.py          # FastAPI app, routes
    config.py        # reads env vars
    db.py            # engine, session
    models.py        # Invoice, LineItem, Flag
    extractor.py     # calls Fireworks, returns validated dict
    anomalies.py     # rule engine
    reports.py       # aggregation queries, CSV export
    static/          # frontend
  tests/
    test_anomalies.py
  Dockerfile
  requirements.txt
  README.md
  SPEC.md
```

## Data model
- Invoice: id, vendor, invoice_number, invoice_date, subtotal, tax_amount, total, currency, file_path, status (pending_review | approved | rejected), created_at
- LineItem: id, invoice_id, description, quantity, unit_price, line_total
- Flag: id, invoice_id, rule, severity (low|medium|high), reason (plain language), resolved (bool), reviewer_note

## Endpoints
- POST /invoices : upload image/PDF, extract, run anomaly rules, store, return result
- GET /invoices : list, filter by status/vendor/date
- GET /invoices/{id} : detail with line items and flags
- POST /invoices/{id}/review : body {decision: approve|reject, note}
- GET /reports/summary : spend by vendor and month, flagged count
- GET /reports/export.csv : CSV export

## Extraction
- Send image as base64 data URL to Fireworks chat completions.
- Ask for JSON only, with fixed keys: vendor, invoice_number, date (ISO YYYY-MM-DD), line_items[{description, quantity, unit_price, total}], subtotal, tax_amount, total, currency.
- Parse JSON defensively (strip code fences), validate with Pydantic.
- If parsing fails, retry once; if it still fails, store the invoice with status pending_review and a high-severity flag "extraction failed".
- Limit upload size (e.g. 5 MB) and allow only jpg/png/pdf.

## Anomaly rules (each produces a plain-language reason)
1. Duplicate: same invoice_number + vendor, or same vendor + total + date.
2. Math mismatch: sum of line totals + tax differs from total by more than 0.01.
3. Line mismatch: quantity * unit_price differs from line total.
4. Amount outlier: total above mean + 3 std dev of that vendor's history (needs 5+ past invoices).
5. New vendor: no previous invoices from this vendor.
6. Date oddity: future date, or older than 365 days.
7. Missing fields: no invoice number, no total, or no vendor.

## Build order
1. Day 1: project skeleton, upload endpoint, extractor working end to end, DB models.
2. Day 2: anomaly rules with tests, review queue endpoints, basic UI (upload, list, review).
3. Day 3: reports + CSV export, Dockerfile, deploy, README, demo.

## Testing with public data
Load a Kaggle transactions/invoice CSV directly into the DB via a script, inject a few known-bad rows (duplicate, wrong total), and confirm the rules catch them. Use only sample or synthetic invoice images, not real private ones.

## Copilot prompts (paste one at a time)
1. "Read SPEC.md. Create the folder structure, requirements.txt, config.py, db.py and models.py exactly as specified."
2. "Implement extractor.py using the OpenAI client with base_url https://api.fireworks.ai/inference/v1, reading FIREWORKS_API_KEY from env. Return a validated Pydantic model."
3. "Implement anomalies.py with the 7 rules in SPEC.md, one function per rule, each returning a Flag or None. Add pytest tests for each."
4. "Implement the endpoints in SPEC.md in main.py with proper error handling and file validation."
5. "Create a simple static frontend: upload form, invoice table with flag badges, a review page with approve/reject buttons."
