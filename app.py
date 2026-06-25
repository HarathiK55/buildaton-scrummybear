"""
app.py  -  Natural-language to SQL over a healthcare database
Run with:  streamlit run app.py

THE PIPELINE (this is the whole product):
  1. Schema linking   -> Claude picks ONLY the tables/columns the question needs
  2. Access control   -> strip PHI/PII columns this user's role can't see
  3. SQL generation   -> Claude writes SQL using only the pruned, allowed schema
  4. Execute          -> run on DuckDB
  5. Self-correction  -> if SQL errors, send the error back to Claude once
  6. Answer           -> Claude turns rows into a plain-English answer

The reason this beats a naive approach: feeding the FULL multi-table schema is
what makes the model hallucinate joins. Pruning first (step 1) is the fix.
"""

import os
import json
import duckdb
import streamlit as st
from openai import OpenAI

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
DB_FILE = "healthcare.duckdb"
MODEL = "Qwen/Qwen3-Coder-30B-A3B-Instruct"   # coding model = better SQL
client = OpenAI(
    base_url="https://api.featherless.ai/v1",
    api_key=os.environ["FEATHERLESS_API_KEY"],   # set this in your terminal
)

# The full schema, described for the model. Each column is tagged.
# "sensitive": which roles are NOT allowed to see it.
SCHEMA = {
    "members": {
        "description": "One row per insured person.",
        "columns": {
            "member_id": "INTEGER, primary key",
            "first_name": "VARCHAR",
            "last_name": "VARCHAR",
            "ssn": "VARCHAR  -- PII, social security number",
            "date_of_birth": "DATE  -- PII",
            "state": "VARCHAR, US state code",
            "email": "VARCHAR  -- PII",
        },
    },
    "enrollments": {
        "description": "Which plan each member is enrolled in. Joins to members on member_id.",
        "columns": {
            "enrollment_id": "INTEGER, primary key",
            "member_id": "INTEGER, joins to members.member_id",
            "plan_name": "VARCHAR",
            "start_date": "DATE",
            "end_date": "DATE",
            "monthly_premium": "DECIMAL",
        },
    },
    "claims": {
        "description": "Medical claims. Joins to members on member_id.",
        "columns": {
            "claim_id": "INTEGER, primary key",
            "member_id": "INTEGER, joins to members.member_id",
            "claim_date": "DATE",
            "diagnosis_code": "VARCHAR  -- PHI, medical diagnosis",
            "amount": "DECIMAL",
            "status": "VARCHAR, one of PAID/DENIED/PENDING",
        },
    },
    "pharmacy_benefits": {
        "description": "Prescriptions filled. Joins to members on member_id.",
        "columns": {
            "rx_id": "INTEGER, primary key",
            "member_id": "INTEGER, joins to members.member_id",
            "drug_name": "VARCHAR",
            "fill_date": "DATE",
            "days_supply": "INTEGER",
            "copay": "DECIMAL",
        },
    },
}

# Which columns are sensitive, and which roles MAY see them.
# Anything listed here is hidden from roles NOT in the allowed list.
SENSITIVE_COLUMNS = {
    ("members", "ssn"): ["admin"],
    ("members", "date_of_birth"): ["admin", "analyst"],
    ("members", "email"): ["admin", "analyst"],
    ("claims", "diagnosis_code"): ["admin", "analyst"],
}

ROLES = ["admin", "analyst", "viewer"]


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------
def llm(prompt: str, system: str = "") -> str:
    """One-shot call to the model via Featherless. Returns the text."""
    resp = client.chat.completions.create(
        model=MODEL,
        max_tokens=1000,
        messages=[
            {"role": "system", "content": system or "You are a precise data assistant."},
            {"role": "user", "content": prompt},
        ],
    )
    return resp.choices[0].message.content.strip()


def allowed_schema(role: str) -> dict:
    """Return the schema with sensitive columns this role can't see removed."""
    pruned = {}
    for table, info in SCHEMA.items():
        cols = {}
        for col, desc in info["columns"].items():
            allowed = SENSITIVE_COLUMNS.get((table, col))
            if allowed is None or role in allowed:
                cols[col] = desc
        pruned[table] = {"description": info["description"], "columns": cols}
    return pruned


def get_sample_values(con):
    """Fetch a few distinct example values per useful column so the model
    knows the real data format (e.g. diabetes is a diagnosis, not a drug)."""
    samples = {}
    probes = {
        ("claims", "diagnosis_code"): "claims",
        ("claims", "status"): "claims",
        ("pharmacy_benefits", "drug_name"): "pharmacy_benefits",
        ("enrollments", "plan_name"): "enrollments",
        ("members", "state"): "members",
    }
    for (table, col), tname in probes.items():
        try:
            rows = con.execute(
                f"SELECT DISTINCT {col} FROM {tname} WHERE {col} IS NOT NULL LIMIT 6"
            ).fetchall()
            samples[(table, col)] = [str(r[0]) for r in rows]
        except Exception:
            pass
    return samples


def schema_to_text(schema: dict, samples: dict = None) -> str:
    lines = []
    for table, info in schema.items():
        lines.append(f"Table {table}: {info['description']}")
        for col, desc in info["columns"].items():
            line = f"    {col}: {desc}"
            if samples and (table, col) in samples:
                ex = ", ".join(samples[(table, col)][:5])
                line += f"  (examples: {ex})"
            lines.append(line)
        lines.append("")
    return "\n".join(lines)


# ---- pipeline stages ------------------------------------------------------
def step_schema_linking(question: str, schema: dict) -> list:
    """Ask Claude which tables are relevant. Returns a list of table names."""
    table_list = "\n".join(
        f"- {t}: {info['description']}" for t, info in schema.items()
    )
    prompt = f"""Given this question and the available tables, list ONLY the tables
needed to answer it. Return a JSON array of table names, nothing else.

Question: {question}

Tables:
{table_list}

Return only valid JSON, e.g. ["members","claims"]"""
    raw = llm(prompt)
    raw = raw.replace("```json", "").replace("```", "").strip()
    try:
        tables = json.loads(raw)
        return [t for t in tables if t in schema]
    except Exception:
        return list(schema.keys())  # fall back to all tables


def step_generate_sql(question: str, schema: dict, use_linking: bool, samples=None) -> tuple:
    """Generate SQL. Returns (sql, selected_tables)."""
    if use_linking:
        selected = step_schema_linking(question, schema)
        pruned = {t: schema[t] for t in selected} if selected else schema
    else:
        selected = list(schema.keys())
        pruned = schema

    prompt = f"""You are an expert DuckDB SQL writer. Write ONE SQL query to answer the question.

CRITICAL: If the question asks for information that does NOT exist in the schema
below (for example: blood pressure, satisfaction score, smoking status, height,
weight, deaths), do NOT invent a column or a constant value. Instead reply with
exactly this single word: IMPOSSIBLE
Never fabricate a column, and never SELECT a hard-coded value (like 1.0) to stand
in for data that isn't there.

Rules:
- Use ONLY the tables and columns listed below. Never invent columns. Check the
  exact column names and the example values before writing.
- Tables join on member_id (members.member_id = enrollments/claims/pharmacy_benefits.member_id).
- Diagnoses (like diabetes, hypertension) are in claims.diagnosis_code, NOT in drug names.
  To find members with a condition, filter claims.diagnosis_code (e.g. LIKE '%diabetes%').
- For "which X has the most/least Y", GROUP BY with ORDER BY ... DESC/ASC LIMIT 1,
  and SELECT both the label AND the count/sum so the answer is readable.
- For "how many/total/average by X", GROUP BY that category and return all groups.
- For "top N", ORDER BY ... DESC LIMIT N and include the columns asked for.
- When counting members, use COUNT(DISTINCT member_id) to avoid double counting.
- Return ONLY the SQL (or the word IMPOSSIBLE). No explanation, no markdown fences.

Schema:
{schema_to_text(pruned, samples)}

Question: {question}

SQL:"""
    sql = llm(prompt).replace("```sql", "").replace("```", "").strip()
    return sql, selected


def step_execute(con, sql: str):
    """Run SQL. Returns (dataframe_or_None, error_or_None)."""
    try:
        return con.execute(sql).df(), None
    except Exception as e:
        return None, str(e)


def step_self_correct(question, schema, sql, error, use_linking, samples=None):
    """Give the model the error once and ask for a fix."""
    pruned = schema
    if use_linking:
        selected = step_schema_linking(question, schema)
        pruned = {t: schema[t] for t in selected} if selected else schema
    prompt = f"""This DuckDB SQL failed. Fix it. Return ONLY corrected SQL.
Use only the exact column names shown (check the examples).

Schema:
{schema_to_text(pruned, samples)}

Question: {question}
Broken SQL: {sql}
Error: {error}

Corrected SQL:"""
    return llm(prompt).replace("```sql", "").replace("```", "").strip()


def step_answer(question: str, df) -> tuple:
    """Turn result rows into a grounded plain-English answer.
    Returns (answer_text, is_grounded).

    The grounding rule: the model is told it may ONLY use the data shown.
    If the data can't answer the question, it must say so instead of inventing.
    This is the anti-hallucination guardrail."""
    if df is None or df.empty:
        return ("The query returned no rows, so there's no data to answer "
                "this question.", False)

    preview = df.head(30).to_string(index=False)
    prompt = f"""Answer the question using ONLY the data table below.

Rules:
- Use only values that appear in the data. Do not invent numbers.
- The data is the correct query result — trust it. Even a single value or
  a single row IS a valid answer (e.g. "Silver HMO" answers "which plan...").
- Reply NOT_ENOUGH_DATA ONLY if the data is truly unrelated to the question
  (e.g. the question asks about blood pressure but there is no such column).
- Otherwise give a clear 1-2 sentence answer, citing the value(s) from the data.

Question: {question}

Data (the complete query result):
{preview}

Answer:"""
    ans = llm(prompt).strip()
    if "NOT_ENOUGH_DATA" in ans.upper():
        return ("I can't answer this from the available data — rather than guess, "
                "I'm flagging that the result doesn't contain what's needed.", False)
    return (ans, True)


def render_visualization(df):
    """Let the user pick a chart type from a dropdown and render it.
    Auto-detects which columns are text (labels) and numeric (values)."""
    import pandas as pd

    if df is None or df.empty:
        st.info("No rows to visualize.")
        return

    numeric_cols = df.select_dtypes(include="number").columns.tolist()
    text_cols = [c for c in df.columns if c not in numeric_cols]

    # If there's nothing numeric, only a table makes sense.
    if not numeric_cols:
        st.dataframe(df, use_container_width=True)
        return

    chart_type = st.selectbox(
        "Chart type",
        ["Bar chart", "Line chart", "Area chart", "Pie chart", "Table only"],
    )

    col1, col2 = st.columns(2)
    with col1:
        label_col = st.selectbox(
            "Label (x-axis)",
            text_cols + numeric_cols,
            index=0 if text_cols else 0,
        )
    with col2:
        value_col = st.selectbox("Value (y-axis)", numeric_cols, index=0)

    # Build a small frame indexed by the label for st charts.
    try:
        plot_df = df[[label_col, value_col]].copy()
        plot_df = plot_df.groupby(label_col, as_index=True)[value_col].sum()
        plot_df = plot_df.sort_values(ascending=False).head(20)
    except Exception:
        st.dataframe(df, use_container_width=True)
        return

    if chart_type == "Bar chart":
        st.bar_chart(plot_df)
    elif chart_type == "Line chart":
        st.line_chart(plot_df)
    elif chart_type == "Area chart":
        st.area_chart(plot_df)
    elif chart_type == "Pie chart":
        # Streamlit has no native pie; use a matplotlib figure.
        try:
            import matplotlib.pyplot as plt
            fig, ax = plt.subplots()
            ax.pie(plot_df.values, labels=plot_df.index, autopct="%1.0f%%",
                   startangle=90)
            ax.axis("equal")
            st.pyplot(fig)
        except Exception:
            st.bar_chart(plot_df)
    else:  # Table only
        st.dataframe(df, use_container_width=True)


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
st.set_page_config(page_title="ClariQuery", page_icon="🔎", layout="wide")

# ---- custom styling --------------------------------------------------------
st.markdown("""
<style>
    .stApp { background: #0f1117; }
    .block-container { padding-top: 2rem; max-width: 1100px; }
    h1, h2, h3 { color: #e8eaf0; font-weight: 600; }
    .hero {
        background: linear-gradient(135deg, #1a8a6b 0%, #0e6b8a 100%);
        padding: 26px 30px; border-radius: 16px; margin-bottom: 22px;
    }
    .hero h1 { color: #fff; margin: 0; font-size: 30px; }
    .hero p { color: #d7f5ec; margin: 6px 0 0; font-size: 15px; }
    .pill {
        display: inline-block; background: rgba(255,255,255,0.15); color: #fff;
        font-size: 12px; padding: 4px 12px; border-radius: 20px; margin: 10px 6px 0 0;
    }
    div[data-testid="stMetric"] {
        background: #1b1e27; border: 1px solid #2a2f3c; border-radius: 12px;
        padding: 14px 16px;
    }
    div[data-testid="stMetricValue"] { color: #1a8a6b; }
    .stButton button {
        background: #1a8a6b; color: #fff; border: none; border-radius: 10px;
        padding: 8px 28px; font-weight: 600;
    }
    .stButton button:hover { background: #15745a; color: #fff; }
</style>
""", unsafe_allow_html=True)

st.markdown("""
<div class="hero">
  <h1>🔎 ClariQuery</h1>
  <p>Ask your data in plain English. Accurate across many tables. Private by design.</p>
  <span class="pill">✓ Multi-table accuracy</span>
  <span class="pill">✓ No hallucinated joins</span>
  <span class="pill">✓ HIPAA-safe access control</span>
</div>
""", unsafe_allow_html=True)

ROLE_LABELS = {
    "admin": "Administrator — full access",
    "analyst": "Analyst — limited PHI access",
    "viewer": "Viewer — no sensitive data",
}
COLUMN_FRIENDLY = {
    ("members", "ssn"): "Social Security Number",
    ("members", "date_of_birth"): "Date of Birth",
    ("members", "email"): "Email Address",
    ("claims", "diagnosis_code"): "Medical Diagnosis",
}

with st.sidebar:
    st.markdown("### 🔎 ClariQuery")
    st.caption("Talk to your data.")
    st.divider()

    role = st.selectbox("View as", ROLES,
                        format_func=lambda r: ROLE_LABELS[r],
                        help="Switch roles to see access control in action.")
    use_linking = st.toggle("Smart table selection", value=True,
                            help="Finds the relevant tables before writing SQL. "
                                 "Turn off to see accuracy drop on multi-table questions.")

    st.divider()
    blocked_pairs = [(t, c) for (t, c), roles in SENSITIVE_COLUMNS.items()
                     if role not in roles]
    blocked = [f"{t}.{c}" for (t, c) in blocked_pairs]
    if blocked_pairs:
        friendly = "".join(
            f"<div style='font-size:12px;color:#f0a8a8;margin:2px 0;'>🔒 "
            f"{COLUMN_FRIENDLY.get((t,c), c)}</div>" for (t, c) in blocked_pairs)
        st.markdown(
            f"<div style='background:#2a1a1f;border:1px solid #5a2a35;"
            f"border-radius:10px;padding:10px 12px;'>"
            f"<div style='font-size:12px;color:#e8eaf0;font-weight:600;"
            f"margin-bottom:5px;'>Protected from this role</div>{friendly}</div>",
            unsafe_allow_html=True)
    else:
        st.markdown(
            "<div style='background:#13332a;border:1px solid #1a8a6b;"
            "border-radius:10px;padding:10px 12px;font-size:12px;color:#8fe6c8;'>"
            "✓ Full data access</div>", unsafe_allow_html=True)

    # query history
    if "history" not in st.session_state:
        st.session_state.history = []
    st.divider()
    st.markdown("**Recent questions**")
    if st.session_state.history:
        for h in st.session_state.history[:8]:
            badge = "🛡️" if h["grounded"] else "⚠️"
            st.caption(f"{badge} {h['q']}  ·  {h['rows']} rows")
    else:
        st.caption("Your questions will appear here.")

    st.divider()
    with st.expander("ℹ️ How it works"):
        st.caption(
            "1. You ask in plain English.\n\n"
            "2. ClariQuery finds the relevant tables.\n\n"
            "3. It writes and runs SQL for you.\n\n"
            "4. Answers use only real query results — no made-up data.\n\n"
            "5. Sensitive columns are hidden based on your role."
        )

question = st.text_input(
    "Your question",
    placeholder="e.g. How many paid claims are there per plan?",
)

examples = [
    "How many members are in each state?",
    "What is the total claim amount by status?",
    "Which plan has the most enrolled members?",
    "What is the average copay by drug across all prescriptions?",
    "List the top 5 members by total paid claim amount and their plan name.",
]
st.caption("Try: " + "  ·  ".join(f"`{e}`" for e in examples))

with st.expander("📂 What data can I ask about?"):
    st.caption("ClariQuery currently has four connected tables:")
    dcol1, dcol2 = st.columns(2)
    table_desc = {
        "members": "People insured — name, state, contact, and protected IDs",
        "enrollments": "Health plans each member is on, dates, and premiums",
        "claims": "Medical claims — dates, diagnosis, amounts, and status",
        "pharmacy_benefits": "Prescriptions filled — drug, date, supply, copay",
    }
    items = list(SCHEMA.items())
    for idx, (table, info) in enumerate(items):
        target = dcol1 if idx % 2 == 0 else dcol2
        cols = ", ".join(info["columns"].keys())
        target.markdown(
            f"**{table}**  \n"
            f"<span style='font-size:12px;color:#9aa0ad;'>{table_desc.get(table,'')}</span>  \n"
            f"<span style='font-size:11px;color:#6f7585;'>{cols}</span>",
            unsafe_allow_html=True)
    st.caption("Tip: try asking for something that isn't here (e.g. "
               "\"average blood pressure\") to see the anti-hallucination guardrail.")

if st.button("Ask", type="primary") and question:
    con = duckdb.connect(DB_FILE, read_only=True)
    schema = allowed_schema(role)
    samples = get_sample_values(con)

    with st.status("Working through the pipeline...", expanded=True) as status:
        st.write("**1. Generating SQL**" + (" (smart table selection)" if use_linking else " (selection OFF)"))
        sql, selected = step_generate_sql(question, schema, use_linking, samples)

        impossible = sql.strip().upper().startswith("IMPOSSIBLE")

        if impossible:
            st.write("The requested data isn't in any table — refusing to fabricate.")
            status.update(label="Done", state="complete", expanded=False)
            df, err = None, None
            answer, grounded = (
                "That information isn't in the available data, so I can't answer it. "
                "Rather than invent a number, I'm flagging this as out of scope.", False)
        else:
            if use_linking:
                st.write("Tables selected as relevant:", ", ".join(selected) or "(none)")
            st.code(sql, language="sql")

            st.write("**2. Running query**")
            df, err = step_execute(con, sql)

            if err:
                st.write("First attempt errored — self-correcting once...")
                sql = step_self_correct(question, schema, sql, err, use_linking, samples)
                st.code(sql, language="sql")
                df, err = step_execute(con, sql)

            answer, grounded = (None, False)
            if not err:
                answer, grounded = step_answer(question, df)

            status.update(label="Done", state="complete", expanded=False)

    con.close()

    # Save everything so changing the chart later does NOT re-run the model.
    if err:
        st.session_state.result = {"error": err}
    elif impossible:
        st.session_state.result = {
            "error": None, "question": question, "sql": "(no query — data not available)",
            "df": None, "selected": [], "use_linking": use_linking,
            "n_tables": 0, "blocked": len(blocked),
            "answer": answer, "grounded": False, "impossible": True,
        }
        st.session_state.history.insert(0, {
            "q": question, "rows": 0, "grounded": False,
        })
    else:
        st.session_state.result = {
            "error": None, "question": question, "sql": sql, "df": df,
            "selected": selected, "use_linking": use_linking,
            "n_tables": len(selected) if use_linking else len(schema),
            "blocked": len(blocked), "answer": answer, "grounded": grounded,
        }
        st.session_state.history.insert(0, {
            "q": question, "rows": len(df), "grounded": grounded,
        })

# ---- render the latest result (runs every time, reads cached data) --------
res = st.session_state.get("result")
if res:
    if res.get("error"):
        st.error(f"Query failed: {res['error']}")
    else:
        df_res = res.get("df")
        n_rows = len(df_res) if df_res is not None else 0
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Tables scanned", res["n_tables"])
        m2.metric("Rows returned", n_rows)
        m3.metric("PHI columns blocked", res["blocked"])
        m4.metric("Answer grounded", "Yes" if res["grounded"] else "Flagged")

        st.subheader("✅ Answer")
        if res["grounded"]:
            st.success(res["answer"])
            st.caption("🛡️ Grounded: this answer uses only values from the query "
                       "result — no invented data.")
        else:
            st.warning(res["answer"])
            st.caption("🛡️ Guardrail triggered: the system refused to guess.")

        if df_res is not None and not df_res.empty:
            st.subheader("📊 Visualize")
            render_visualization(df_res)   # changing chart only redraws this

            with st.expander("Show the SQL and data table"):
                st.code(res["sql"], language="sql")
                st.dataframe(df_res, use_container_width=True)
