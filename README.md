# 🚀 AI Database Query Assistant

[![Python](https://img.shields.io/badge/Python-3.13-blue.svg)](https://www.python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-green.svg)](https://fastapi.tiangolo.com)
[![Streamlit](https://img.shields.io/badge/Streamlit-1.28+-FF4B4B.svg)](https://streamlit.io)
[![JSON-RPC 2.0](https://img.shields.io/badge/API-JSON--RPC%202.0-blueviolet)](https://www.jsonrpc.org/specification)
[![Docker](https://img.shields.io/badge/Docker-ready-blue.svg)](https://www.docker.com)
[![CI/CD](https://github.com/randomdeliciousness/AI-Database-Query-Assistant/actions/workflows/ci.yml/badge.svg)](https://github.com/randomdeliciousness/AI-Database-Query-Assistant/actions)

**Natural language → SQL system with visualizations and a vector store of past queries.**

> **Note:** the vector store (Chroma) keeps past question-and-SQL pairs and shows similar ones next to each answer. The prompt sent to the model gets the top tables and example pairs. Example SQL is not executed. A SQLite execution error is sent back once as its own metered call. A guard block is not retried. The embedding model is the pinned ONNX file in the image.

[**Live demo video:** 
](https://github.com/user-attachments/assets/bca6fb38-120b-4673-a3a0-d50051027b97
)

- **JSON-RPC API**: a FastAPI JSON-RPC endpoint (`/mcp`), not a full MCP implementation.
- **End-to-end tool creation** (LLM + vector DB + API + Docker + CI/CD)
- **Structured outputs**: the model returns SQL as `SQLResponse` on xAI. Latency and SQL validity have not been measured yet; no figures are claimed until an evaluation backs them.
- Provider `xai-openai-compat` uses the installed OpenAI client with `base_url="https://api.x.ai/v1"` and the xAI key, and never calls the OpenAI API.

## Quick Start
```bash
git clone https://github.com/randomdeliciousness/AI-Database-Query-Assistant.git
cd AI-Database-Query-Assistant
pip install -r requirements.txt
# Download Chinook DB if missing: curl -L -o chinook.db https://github.com/lerocha/chinook-database/raw/master/ChinookDatabase/DataSources/Chinook_Sqlite.sqlite
cp .streamlit/secrets.example.toml .streamlit/secrets.toml  # then set [xai] api_key
# Set XAI_API_KEY, or [xai] api_key in .streamlit/secrets.toml.
# The FastAPI server (mcp_server.py) uses the same loader.
streamlit run streamlit_app.py
```

Environment variables take precedence over .streamlit/secrets.toml.

**Limitations:** The app and API are built for the bundled Chinook sample database. Sample DB identity is verified by sha256, not file name, and re-checked when the file's size, mtime or inode changes; an in-place edit that preserves size and mtime is not detected. Any other database needs an audit key. An xAI key is required, and every question is a model call. Generated SQL is checked for safety, not for correctness. The read-only SQL guard is defense in depth (41 of 41 red-team cases in `tests/test_sql_guard.py` are blocked; CI enforces this on every commit), not a guarantee, and it also rejects some valid read-only SQL: `EXPLAIN`, `pragma_table_info` and queries that start with `VALUES`. Results stop at a row cap (10,000 rows by default) and the app says when that happens. The prompt gets the top tables and example pairs. Example SQL is not executed. A SQLite execution error is sent back once as its own metered call. A guard block is not retried. The embedding model is the pinned ONNX file in the image.

## Audit log
Every query appends one JSON line to `audit/adq_audit.jsonl` (set `ADQ_AUDIT_PATH` to move it; there is no switch to turn auditing off). A row records the outcome (`answered`, `blocked`, `truncated` or `error`), the guard's block reason, the row count, the truncated flag, the guard's step count, the model, a per-query `request_id` (also returned by the API) and an optional session ID. Result rows and cell values are never logged.

With a per-install key (`ADQ_AUDIT_HMAC_KEY`, or `[audit] hmac_key` in `.streamlit/secrets.toml`; at least 32 bytes; the environment variable wins), each row also holds a keyed hash (HMAC-SHA256) of the question and of the SQL, plus the SQL with every string, number and blob literal replaced by `?`. Without a key, the bundled `chinook.db` (recognized by its sha256, not its file name) logs outcomes only, with no question or SQL fields, and any other database refuses to start. Raw-text logging (`ADQ_AUDIT_RAW_TEXT=1`) works only on the bundled `chinook.db`. Generate a key with `python -c "import secrets; print(secrets.token_hex(32))"`.

## Synthetic vet-clinic database

The clinic database is not a committed file. `clinic_generator` writes it in its own process. The app and the API only read it.

Live demo, from the repo root, after `ADQ_AUDIT_HMAC_KEY` is exported or set in a gitignored `.env` (at least 32 bytes):

    docker compose up

That starts two containers from the image built by the Dockerfile. The generator container has no network. The app publishes only `127.0.0.1:8501`. The clinic file lives on the named volume `clinic-data`, shared by both containers. The first start creates the file. A later `docker compose up` continues that file from `provenance.clock_end`. `docker compose down -v` discards the volume. The generator does not delete an existing file.

One-shot create, on the host:

    python -m clinic_generator --seed 11 --db clinic.db

A second one-shot create of an existing path refuses and leaves the file in place. Continue one step with `--resume`. Keep writing with `--follow`.

Point the app at that file with `ADQ_DB_PATH` (or `[database] path` in `.streamlit/secrets.toml`). Set `ADQ_AUDIT_HMAC_KEY` (at least 32 bytes; the environment variable wins). The clinic file is not the bundled Chinook database, so the app runs in keyed-hash audit mode. Raw-text audit mode stays limited to the bundled Chinook file. The generator is the only writer and it sets `PRAGMA journal_mode=WAL`. The app opens the file with the same read-only guard it uses for Chinook.

The dataset is synthetic. Table `provenance` has one row. `data_class` and `label` are both `synthetic`, and only the generator writes that table. Email addresses are `@example.com`. Phone numbers are in the fictional range `+1-555-0100` through `+1-555-0199`. No real clinic records are in the generator, the schema, or the tests.

Each visit uses stock from a lot. The product and quantity come from `usage` for that visit's appointment type. An appointment with no visit does not use stock. A supplier delivery is priced at the supplier price in effect that day and creates a lot. A lot is not used on or after its expiry date. Lot 9001 expires on 2024-01-07 with one unit still on hand.

Each supplier row has `lead_time_days` and `min_order_qty`. Supplier 1 is 2 days and minimum quantity 1. Supplier 9001 is 5 days and minimum quantity 10. The generator stores those numbers and does not use them to time a delivery or to change a quantity.

`appointment_type` holds `checkup`, `vaccine`, and `overorder`. `usage` lists the product and quantity one visit of that type consumes: checkup uses one `SYN-SUPPLY`, vaccine uses one `SYN-VACCINE`, and overorder uses one `SYN-OVERORDER`. The generator writes `stock_use` for a visit from that table. Filler visits are checkup. Product `SYN-PRICE-GAP` has no usage row.

The same seed reproduces the snapshot content hash of a fresh create: schema first, then each table sorted by primary key. A different seed produces a different hash. Seed 11 is a worked example, not a special case. The planted rows below are in every seed. Continuing a file from `provenance.clock_end` keeps those rows and changes the content hash.

### Supply Cost Checkup

The sample buttons in the app are these three questions. They are the start of the Supply Cost Checkup.

7. A vaccine given but not billed. Visit 9003 uses vaccine product 9001 (`SYN-VACCINE`). Invoice 9003 for that visit has no line for that product. Visit 9002 uses the same vaccine and its invoice line 9003 bills it.

```sql
SELECT v.visit_id, v.patient_id, su.product_id, pr.product_name
FROM visit v
JOIN stock_use su ON su.visit_id = v.visit_id
JOIN product pr ON pr.product_id = su.product_id
LEFT JOIN invoice i ON i.visit_id = v.visit_id
LEFT JOIN invoice_line l
  ON l.invoice_id = i.invoice_id AND l.product_id = su.product_id
WHERE pr.kind = 'vaccine' AND l.line_id IS NULL;
```

Expected: one row, visit_id 9003, patient_id 9005, product_id 9001, product_name SYN-VACCINE.

8. A supplier price rising while the client price stays flat. Product 9002 (`SYN-PRICE-GAP`) has supplier price 500 cents from 2024-01-01 and 800 cents from 2024-06-01. Its only client price is 1200 cents from 2024-01-01. Deliveries on 2024-03-01 and 2024-07-15 store 500 and 800.

```sql
SELECT sp_new.product_id,
       sp_old.unit_cents AS old_supplier_cents,
       sp_new.unit_cents AS new_supplier_cents,
       cp.unit_cents AS client_cents
FROM supplier_price sp_new
JOIN supplier_price sp_old
  ON sp_old.product_id = sp_new.product_id
 AND sp_old.supplier_id = sp_new.supplier_id
 AND sp_old.effective_on < sp_new.effective_on
 AND sp_old.unit_cents < sp_new.unit_cents
JOIN client_price cp ON cp.product_id = sp_new.product_id
WHERE NOT EXISTS (
    SELECT 1 FROM client_price later
    WHERE later.product_id = cp.product_id
      AND later.effective_on > cp.effective_on
)
  AND NOT EXISTS (
    SELECT 1 FROM supplier_price newer
    WHERE newer.product_id = sp_new.product_id
      AND newer.supplier_id = sp_new.supplier_id
      AND newer.effective_on > sp_new.effective_on
);
```

Expected: one row, product_id 9002, old_supplier_cents 500, new_supplier_cents 800, client_cents 1200.

9. Over-ordering. Product 9003 (`SYN-OVERORDER`) is delivered 24 units and used 1 unit.

```sql
SELECT p.product_id, p.product_name,
       (SELECT COALESCE(SUM(d.qty), 0) FROM delivery d
         WHERE d.product_id = p.product_id) AS delivered_qty,
       (SELECT COALESCE(SUM(u.qty), 0) FROM stock_use u
         WHERE u.product_id = p.product_id) AS used_qty
FROM product p
WHERE (SELECT COALESCE(SUM(u.qty), 0) FROM stock_use u
         WHERE u.product_id = p.product_id) > 0
  AND (SELECT COALESCE(SUM(d.qty), 0) FROM delivery d
         WHERE d.product_id = p.product_id)
      > 4 * (SELECT COALESCE(SUM(u.qty), 0) FROM stock_use u
              WHERE u.product_id = p.product_id);
```

Expected: one row, product_id 9003, product_name SYN-OVERORDER, delivered_qty 24, used_qty 1.

### Planted data-integrity cases

1. Deceased patient with a later appointment. Patient 9001 is deceased on 2024-03-01 and has appointment 9001 starting 2024-04-01T15:00:00Z.

```sql
SELECT p.patient_id, p.deceased_on, a.appointment_id, a.starts_at
FROM patient p
JOIN appointment a ON a.patient_id = p.patient_id
WHERE p.status = 'deceased' AND a.starts_at > p.deceased_on;
```

Expected: one row, patient_id 9001, deceased_on 2024-03-01, appointment_id 9001, starts_at 2024-04-01T15:00:00Z.

2. Paid invoice whose header total is not the sum of its lines. Invoice 9001 is paid, total_cents is 10000, and its two lines are 3000 and 4000.

```sql
SELECT i.invoice_id, i.status, i.total_cents, SUM(l.amount_cents) AS line_cents
FROM invoice i
JOIN invoice_line l ON l.invoice_id = i.invoice_id
GROUP BY i.invoice_id, i.status, i.total_cents
HAVING i.total_cents <> SUM(l.amount_cents);
```

Expected: one row, invoice_id 9001, status paid, total_cents 10000, line_cents 7000.

3. Appointment that ends before it starts. Appointment 9002 starts at 2024-05-02T15:00:00Z and ends at 2024-05-02T14:00:00Z.

```sql
SELECT appointment_id, starts_at, ends_at
FROM appointment
WHERE ends_at < starts_at;
```

Expected: one row, appointment_id 9002, starts_at 2024-05-02T15:00:00Z, ends_at 2024-05-02T14:00:00Z.

4. Two living patients share a microchip. Patients 9002 and 9003 both carry `SYN-CHIP-9000`.

```sql
SELECT microchip, COUNT(*) AS n
FROM patient
WHERE microchip IS NOT NULL
GROUP BY microchip
HAVING COUNT(*) > 1;
```

Expected: one row, microchip SYN-CHIP-9000, n 2.

5. Overlapping appointments for one clinician. Clinician 9001 has appointment 9003 from 2024-06-03T15:00:00Z to 2024-06-03T16:00:00Z and appointment 9004 from 2024-06-03T15:30:00Z to 2024-06-03T16:30:00Z.

```sql
SELECT a.appointment_id, b.appointment_id, a.clinician_id
FROM appointment a
JOIN appointment b
  ON a.clinician_id = b.clinician_id
 AND a.appointment_id < b.appointment_id
 AND a.starts_at < b.ends_at
 AND b.starts_at < a.ends_at;
```

Expected: one row, appointment ids 9003 and 9004, clinician_id 9001.

6. Impossible weight for the species. Visit 9001 records patient 9004, a cat, at 250000 grams.

```sql
SELECT v.visit_id, p.patient_id, p.species, v.weight_g
FROM visit v
JOIN patient p ON p.patient_id = v.patient_id
WHERE p.species = 'cat' AND v.weight_g > 20000;
```

Expected: one row, visit_id 9001, patient_id 9004, species cat, weight_g 250000.
