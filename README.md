# Ask Your Data — Statement 1 starter kit

Natural-language questions over a healthcare database. No SQL required.

## What's here
- `setup_db.py` — builds the synthetic healthcare database (4 tables that join)
- `app.py` — the Streamlit app with the full pipeline
- `requirements.txt` — dependencies

## The pipeline (this IS the product)
1. **Schema linking** — model picks only the tables a question needs
2. **Access control** — strips PHI/PII columns the user's role can't see
3. **SQL generation** — model writes SQL from the pruned, allowed schema
4. **Execute** — runs on DuckDB
5. **Self-correction** — if SQL errors, send the error back once
6. **Grounded answer** — answer uses ONLY the returned rows; if the data
   can't answer, it flags "not grounded" instead of inventing (anti-hallucination)

Plus: trust metrics (tables scanned / rows / PHI blocked / grounded),
chart dropdown (bar/line/area/pie), and a recent-questions history panel.

Why it works: feeding the full multi-table schema is what causes hallucinated
joins. Pruning first (step 1) is the fix. The grounding rule (step 6) stops
the model inventing numbers. That's your differentiator.

## Setup (do this first)
Uses Featherless AI (Buildathon sponsor credits). Model: Qwen3-Coder-30B.
```bash
pip install -r requirements.txt
export FEATHERLESS_API_KEY=your_key_here    # Windows: set FEATHERLESS_API_KEY=...
python setup_db.py                          # builds healthcare.duckdb
streamlit run app.py
```
If SQL quality is weak on hard questions, switch MODEL in app.py to
"Qwen/Qwen3-Coder-Next" (bigger, stronger, slower).

## The database
- `members` (200 rows) — people. Sensitive: ssn, date_of_birth, email
- `enrollments` (299) — plans per member. Joins on member_id
- `claims` (527) — medical claims. Sensitive: diagnosis_code. Joins on member_id
- `pharmacy_benefits` (418) — prescriptions. Joins on member_id

## Roles (the access-control demo)
- **admin** — sees everything
- **analyst** — sees diagnosis & dob, NOT ssn
- **viewer** — sees no sensitive columns at all

## Demo script (3 min, in this order)
1. Ask "How many members are in each state?" → simple, works. *"Baseline."*
2. Ask "List the top 5 members by total paid claim amount and their plan name."
   with **schema linking OFF** → likely wrong/hallucinated join.
   *"This is the problem in the statement — accuracy breaks on multi-table joins."*
3. Same question, schema linking **ON** → correct, and show the tables it picked.
   *"We fix it by choosing relevant tables before writing SQL."*
4. Switch role to **viewer**, ask something about diagnosis → column is gone /
   query can't touch it. *"HIPAA-safe by construction."*

## Where each person works
- **Person A** — tune the prompts in `app.py` (step_schema_linking, step_generate_sql)
- **Person B** — the Streamlit UI (everything under `# UI`)
- **Person C** — access control: `SENSITIVE_COLUMNS` and `allowed_schema()`

## If you fall behind, cut in this order
1. Self-correction (step 5)
2. The 4th table
NEVER cut access control — it's the differentiator.
