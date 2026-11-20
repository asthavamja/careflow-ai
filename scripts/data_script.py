from pathlib import Path
import pandas as pd
import random
import re
from datetime import date
import os
import psycopg
from dotenv import load_dotenv
load_dotenv()

CSV_PATH = Path(__file__).resolve().parents[1] / "data" / "synthea" / "patients.csv"

# 1. Read only the columns needed for this report.
df = pd.read_csv(
    CSV_PATH,
    usecols=["Id", "BIRTHDATE", "DEATHDATE", "FIRST", "LAST", "GENDER", "CITY"],
)
total_rows = len(df)

# 2. Keep living patients only (DEATHDATE is empty for them)
df = df[df["DEATHDATE"].isna()].copy()
living_rows = len(df)

# 3. Count names with digits, then remove the digits
names_with_digits = (
    df["FIRST"].str.contains(r"\d", na=False).sum()
    + df["LAST"].str.contains(r"\d", na=False).sum()
)
for col in ["FIRST", "LAST"]:
    df[col] = df[col].str.replace(r"\d+", "", regex=True).str.strip()

# 4. Rename columns to match our database table, and drop DEATHDATE
df = df.rename(columns={
    "Id": "synthea_id",
    "BIRTHDATE": "date_of_birth",
    "FIRST": "first_name",
    "LAST": "last_name",
    "GENDER": "gender",
    "CITY": "city",
})
df = df[["synthea_id", "first_name", "last_name",
         "date_of_birth", "gender", "city"]]

# --- 4b. Fake phone numbers and emails ---
rng = random.Random(42)   # fixed seed: same fake data on every run
used_phones = set()

def make_phone_pair():
    """Return (messy phone text, cleaned 10 digits). Every number is unique."""
    while True:
        digits = str(rng.choice([6, 7, 8, 9])) + "".join(rng.choices("0123456789", k=9))
        if digits not in used_phones:
            used_phones.add(digits)
            break
    messy_formats = [
        f"+91 {digits[:5]}-{digits[5:]}",
        f"{digits[:5]} {digits[5:]}",
        f"0{digits}",
        digits,
    ]
    return rng.choice(messy_formats), digits

pairs = [make_phone_pair() for _ in range(len(df))]
df["phone"] = [p[0] for p in pairs]
df["phone_normalized"] = [p[1] for p in pairs]

# Email: first.last@example.com, with a number added if the name repeats
base = (df["first_name"] + "." + df["last_name"]).str.lower()
base = base.str.replace(r"[^a-z.]", "", regex=True)
repeat_no = base.groupby(base).cumcount()
df["email"] = [
    f"{b}@example.com" if n == 0 else f"{b}{n + 1}@example.com"
    for b, n in zip(base, repeat_no)
]

df = df[["synthea_id", "first_name", "last_name", "date_of_birth",
         "gender", "phone", "phone_normalized", "email", "city"]]

import re
from datetime import date

# --- 4c. Hand-made demo patients ---
TEST_INBOX = "testmail@gmail.com"   # <-- put YOUR real test inbox here

def normalize_phone(text):
    """Keep digits only, then the last 10 (drops +91 and a leading 0)."""
    return re.sub(r"\D", "", text)[-10:]

def test_email(tag):
    name, domain = TEST_INBOX.split("@")
    return f"{name}+{tag}@{domain}"        # Gmail delivers these to you

demo_rows = [
    # first, last, date of birth, gender, phone, email, city
    ("Amit",   "Patel", "1985-09-09", "M", "+91 90010 10001", test_email, "Surat"),        # clean booking
    ("Rahul",  "Shah",  "1990-05-14", "M", "98765 43210",     "rahul.shah@example.com", "Ahmedabad"), # duplicate target
    ("Priya",  "Mehta", "1988-02-11", "F", "+91 98200-11122", "priya.mehta@example.com", "Vadodara"), # conflict pair (adult)
    ("Priya",  "Mehta", "2009-07-03", "F", "98200 11122",     "priya.mehta2@example.com", "Vadodara"), # conflict pair (minor)
    ("Kiran",  "Joshi", "2013-03-18", "M", "+91 97120 55533", "kiran.joshi@example.com", "Surat"),   # minor rule
    ("Sneha",  "Iyer",  "1995-11-23", "F", "99250 77788",     test_email, "Surat"),        # cancel scenario
    ("Deepak", "Rao",   "1979-01-30", "M", "+91 94260-22244", test_email, "Surat"),       # waitlist
]

demo_df = pd.DataFrame([{
    "synthea_id": None,                       # not from Synthea
    "first_name": f, "last_name": l, "date_of_birth": dob, "gender": g,
    "phone": ph, "phone_normalized": normalize_phone(ph),
    "email": em, "city": c,
} for f, l, dob, g, ph, em, c in demo_rows])

df = pd.concat([df, demo_df], ignore_index=True)

# --- 5. Report ---
pd.set_option("display.width", 200)
pd.set_option("display.max_columns", None)

dob = pd.to_datetime(df["date_of_birth"])
age = (pd.Timestamp(date.today()) - dob).dt.days // 365
shared_phone = df["phone_normalized"].duplicated(keep=False).sum()

print(f"Patients total:        {len(df)}  (Synthea {len(df) - len(demo_df)} + demo {len(demo_df)})")
print(f"Under 18:              {(age < 18).sum()}")
print(f"Rows sharing a phone:  {shared_phone}   (expected: 2, the two Priya Mehtas)")
print(f"Unique emails:         {df['email'].nunique()} of {len(df)}")
print("\nDemo patients:")
print(df.tail(len(demo_df))[["first_name", "last_name", "date_of_birth",
                             "phone_normalized", "email"]])

# --- 6. Save to the database ---

rows = df.astype(object).where(df.notna(), None)   # empty values become NULL
records = [
    (r.synthea_id, r.first_name, r.last_name, r.date_of_birth, r.gender,
     r.phone, r.phone_normalized, r.email, r.city)
    for r in rows.itertuples(index=False)
]
print("Records prepared:", len(records))
with psycopg.connect(
    host=os.getenv("DB_HOST"), port=os.getenv("DB_PORT"),
    dbname=os.getenv("DB_NAME"), user=os.getenv("DB_USER"),
    password=os.getenv("DB_PASSWORD"),
) as conn:
    with conn.cursor() as cur:
        cur.execute("select count(*) from careflow.patients")
        existing = cur.fetchone()[0]
        if existing > 0:
            raise SystemExit(
                f"patients already has {existing} rows. Stopping so nothing is added twice."
            )
        cur.executemany(
            """insert into careflow.patients
               (synthea_id, first_name, last_name, date_of_birth, gender,
                phone, phone_normalized, email, city)
               values (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            records,
        )
        cur.execute("select count(*) from careflow.patients")
        print(f"Saved. patients table now has {cur.fetchone()[0]} rows.")
    
