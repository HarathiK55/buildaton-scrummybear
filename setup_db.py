"""
setup_db.py
Creates a synthetic healthcare database in DuckDB.
Run this ONCE before starting the app:  python setup_db.py

It builds 4 tables that join together:
    members           -> one row per person (has PHI/PII: ssn, dob)
    enrollments       -> which plan each member is on, start/end dates
    claims            -> medical claims (has PHI: diagnosis_code)
    pharmacy_benefits -> prescriptions filled

This is the data your tool answers questions about.
"""

import duckdb
import random
from datetime import date, timedelta

DB_FILE = "healthcare.duckdb"

random.seed(42)  # same data every time -> reproducible demo

# ---- helpers ---------------------------------------------------------------
FIRST = ["James", "Mary", "John", "Patricia", "Robert", "Jennifer", "Michael",
         "Linda", "David", "Elizabeth", "Maria", "Wei", "Aisha", "Carlos",
         "Priya", "Chen", "Fatima", "Diego", "Sofia", "Omar"]
LAST = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
        "Davis", "Rodriguez", "Martinez", "Lee", "Patel", "Nguyen", "Kim",
        "Khan", "Singh", "Lopez", "Gonzalez", "Wang", "Ali", "Thompson",
        "Hernandez", "Moore", "Jackson", "Chen", "Yang", "Hill", "Adams",
        "Nelson", "Baker", "Rivera", "Campbell", "Torres", "Reed", "Cook"]
STATES = ["TX", "CA", "NY", "FL", "IL", "WA", "GA", "OH", "NC", "AZ", "PA", "MI"]
PLANS = ["Gold PPO", "Silver HMO", "Bronze HDHP", "Platinum PPO"]
DIAGS = ["E11.9 Type 2 diabetes", "I10 Hypertension", "J45 Asthma",
         "M54.5 Low back pain", "F41.1 Anxiety", "E78.5 Hyperlipidemia",
         "J06.9 Upper respiratory infection", "K21.9 GERD",
         "M17.9 Osteoarthritis of knee", "N39.0 Urinary tract infection",
         "G43.9 Migraine", "F32.9 Major depressive disorder",
         "E66.9 Obesity", "I25.10 Coronary artery disease"]
DRUGS = ["Metformin", "Lisinopril", "Atorvastatin", "Albuterol",
         "Omeprazole", "Sertraline", "Amoxicillin", "Levothyroxine",
         "Amlodipine", "Gabapentin", "Losartan", "Hydrochlorothiazide",
         "Montelukast", "Escitalopram"]


def rand_date(start_year=2023, end_year=2025):
    start = date(start_year, 1, 1)
    end = date(end_year, 12, 31)
    return start + timedelta(days=random.randint(0, (end - start).days))


def main():
    con = duckdb.connect(DB_FILE)

    # start clean
    for t in ["claims", "pharmacy_benefits", "enrollments", "members"]:
        con.execute(f"DROP TABLE IF EXISTS {t}")

    # ---- members (PHI/PII lives here) --------------------------------------
    con.execute("""
        CREATE TABLE members (
            member_id     INTEGER PRIMARY KEY,
            first_name    VARCHAR,
            last_name     VARCHAR,
            ssn           VARCHAR,      -- PII (sensitive)
            date_of_birth DATE,         -- PII (sensitive)
            state         VARCHAR,
            email         VARCHAR       -- PII (sensitive)
        )
    """)
    members = []
    for i in range(1, 2001):  # 2000 members
        fn = random.choice(FIRST)
        ln = random.choice(LAST)
        ssn = f"{random.randint(100,899)}-{random.randint(10,99)}-{random.randint(1000,9999)}"
        dob = rand_date(1950, 2005)
        st = random.choice(STATES)
        email = f"{fn.lower()}.{ln.lower()}{i}@example.com"
        members.append((i, fn, ln, ssn, dob, st, email))
    con.executemany("INSERT INTO members VALUES (?,?,?,?,?,?,?)", members)

    # ---- enrollments -------------------------------------------------------
    con.execute("""
        CREATE TABLE enrollments (
            enrollment_id INTEGER PRIMARY KEY,
            member_id     INTEGER,      -- joins to members
            plan_name     VARCHAR,
            start_date    DATE,
            end_date      DATE,
            monthly_premium DECIMAL(8,2)
        )
    """)
    enrollments = []
    eid = 1
    for m in members:
        # each member has 1-2 enrollments
        for _ in range(random.randint(1, 2)):
            plan = random.choice(PLANS)
            sd = rand_date(2023, 2024)
            ed = sd + timedelta(days=365)
            premium = round(random.uniform(180, 650), 2)
            enrollments.append((eid, m[0], plan, sd, ed, premium))
            eid += 1
    con.executemany("INSERT INTO enrollments VALUES (?,?,?,?,?,?)", enrollments)

    # ---- claims (PHI: diagnosis) -------------------------------------------
    con.execute("""
        CREATE TABLE claims (
            claim_id       INTEGER PRIMARY KEY,
            member_id      INTEGER,     -- joins to members
            claim_date     DATE,
            diagnosis_code VARCHAR,     -- PHI (sensitive)
            amount         DECIMAL(10,2),
            status         VARCHAR      -- PAID / DENIED / PENDING
        )
    """)
    claims = []
    cid = 1
    for m in members:
        for _ in range(random.randint(0, 5)):  # 0-5 claims each
            cd = rand_date(2024, 2025)
            diag = random.choice(DIAGS)
            amt = round(random.uniform(50, 8000), 2)
            status = random.choices(["PAID", "DENIED", "PENDING"],
                                    weights=[70, 15, 15])[0]
            claims.append((cid, m[0], cd, diag, amt, status))
            cid += 1
    con.executemany("INSERT INTO claims VALUES (?,?,?,?,?,?)", claims)

    # ---- pharmacy_benefits -------------------------------------------------
    con.execute("""
        CREATE TABLE pharmacy_benefits (
            rx_id        INTEGER PRIMARY KEY,
            member_id    INTEGER,       -- joins to members
            drug_name    VARCHAR,
            fill_date    DATE,
            days_supply  INTEGER,
            copay        DECIMAL(8,2)
        )
    """)
    rxs = []
    rid = 1
    for m in members:
        for _ in range(random.randint(0, 4)):
            fd = rand_date(2024, 2025)
            drug = random.choice(DRUGS)
            days = random.choice([30, 60, 90])
            copay = round(random.uniform(5, 75), 2)
            rxs.append((rid, m[0], drug, fd, days, copay))
            rid += 1
    con.executemany("INSERT INTO pharmacy_benefits VALUES (?,?,?,?,?,?)", rxs)

    # ---- report ------------------------------------------------------------
    print("Database created:", DB_FILE)
    for t in ["members", "enrollments", "claims", "pharmacy_benefits"]:
        n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  {t:20s} {n:5d} rows")
    con.close()


if __name__ == "__main__":
    main()
