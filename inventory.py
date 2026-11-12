"""Read the supplied cleaned listings and search their stated facts in SQLite."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from openpyxl import load_workbook


DEFAULT_WORKBOOK = Path(__file__).parent / "data" / "Copy_of_sample_cars_dataset.xlsx"
SOURCE_COLUMNS = ("Listing_ID", "year", "make", "model", "trim", "title", "description", "photo_url")
FIELDS = (
    "listing_id", "year", "make", "model", "trim", "title", "description", "photo_url",
    "price_aed", "price_evidence", "mileage_km", "mileage_evidence",
    "color", "color_evidence", "body_style", "body_style_evidence",
)

# Accept grouped thousands (including "23 900 kms") and ungrouped numbers.
NUMBER = r"(?:\d{1,3}(?:[, ]\d{3})+|\d+)"
AMOUNT = rf"(?P<n>{NUMBER}(?:\.\d{{2}})?)"
AED = r"(?:AED|DHS|dirhams?)"
MONEY = rf"(?:{AED}\s*{AMOUNT})"
PRICE_LABEL = r"(?:asking\s+price|cash\s+price|selling\s+price|price(?:\s+reduced)?)\s*[:#-]?\s*"
PRICE_PATTERNS = tuple(re.compile(pattern, re.I) for pattern in (
    rf"\b{MONEY}\s+(?:in\s+)?cash\b",
    rf"\b{PRICE_LABEL}{MONEY}\b",
    rf"\b{PRICE_LABEL}{AMOUNT}\s*{AED}\b",
    rf"\bpayment\s*:\s*[^\w\s]*\s*{MONEY}(?=\s*-{{3,}})",
    rf"(?:^|-{{3,}}\s*){MONEY}\s*/-(?=\s*(?:-{{3,}}|$))",
    rf"\b{MONEY}\s*/-(?=\s*-{{3,}})",
    rf"^\s*{AMOUNT}\s*{AED}\s*/(?=\s*[\d,.]+\s*{AED}\s+per\s+month)",
    rf"\bkm[s]?\s+{AMOUNT}\s*{AED}\s*$",
))
FINANCE_SUFFIX = re.compile(r"\s*(?:/\s*(?:mo|month)|(?:per\s+)?month|p\.?m\b|installment|down[ -]payment)", re.I)

KM = r"\s*\(?\s*k(?:m[s]?|ilomet(?:er|re)s?)\b(?!\s*(?:/\s*h|per\s+hour|ph))\)?"
DISTANCE = re.compile(rf"(?<![\d,.])(?P<n>{NUMBER}){KM}", re.I)
ODOMETER = re.compile(
    rf"\b(?:odometer|mileage)\s*[:=-]?\s*(?:just\s+|only\s+)?(?P<n>{NUMBER}){KM}", re.I
)
NON_ODOMETER_BEFORE = re.compile(
    r"(?:service(?:\s+contract)?|servicing|warranty|coverage|up\s+to|range|speed)"
    r"[\s:(\-]*(?:up\s+to\s+)?$|\b(?:last|next)\s+service[^•|]{0,35}$|"
    r"\b\d+\s*(?:years?|months?)\s+(?:or|/)\s*$", re.I
)
NON_ODOMETER_AFTER = re.compile(r"\s*(?:or\s+\d+\s+years?|service\s+interval|of\s+(?:range|coverage))", re.I)
POSSIBLE_WARRANTY_LIMIT = re.compile(r"\s+warranty\b", re.I)

COLORS = r"(?P<value>white|black|silver|gr[ae]y|red|blue|green|gold|yellow|orange|brown|beige)"
COLOR_PATTERNS = tuple(re.compile(pattern, re.I) for pattern in (
    rf"\bexterior(?:\s+colou?r)?\s*:\s*(?:diamond\s+|pearl\s+|metallic\s+)?{COLORS}\b",
    rf"\b{COLORS}\s+exterior\b",
    rf"\b{COLORS}\s+colou?r\b",
))
BODY = r"(?P<value>suv|sedan|coupe|hatchback|wagon|convertible|crossover|roadster|minivan|pickup)"
BODY_WORD = re.compile(rf"\b{BODY}\b", re.I)
BODY_CONTEXT = re.compile(rf"\b(?:this\s+(?:stylish\s+)?|luxury\s+|\d{{4}}\s+\w+\s+){BODY}\b", re.I)


def _unambiguous(matches):
    """Keep the first source excerpt only when all candidates agree."""
    matches = list(matches)
    if len({value for value, _ in matches}) == 1:
        return matches[0]
    return None, None


def _value(match):
    return float(match["n"].replace(",", "").replace(" ", ""))


def _asking_price(texts):
    candidates = []
    for text in texts:
        for pattern in PRICE_PATTERNS:
            for match in pattern.finditer(text):
                if not FINANCE_SUFFIX.match(text[match.end():]):
                    candidates.append((_value(match), match[0].strip(" -")))
    return _unambiguous(candidates)


def _mileage(texts):
    # An explicit odometer label takes precedence over nearby service intervals.
    for pattern in (ODOMETER, DISTANCE):
        candidates = []
        for text in texts:
            for match in pattern.finditer(text):
                before = text[max(0, match.start() - 65):match.start()]
                after = text[match.end():match.end() + 40]
                if NON_ODOMETER_BEFORE.search(before) or NON_ODOMETER_AFTER.match(after):
                    continue
                if pattern is DISTANCE and POSSIBLE_WARRANTY_LIMIT.match(after):
                    continue
                candidates.append((int(_value(match)), match[0]))
        if candidates:
            return _unambiguous(candidates)
    return None, None


def _color(texts):
    return _unambiguous(
        (m["value"].lower().replace("gray", "grey"), m[0])
        for text in texts for pattern in COLOR_PATTERNS for m in pattern.finditer(text)
    )


def _body_style(title, description):
    # A body type elsewhere in a dealer's prose may describe a comparison vehicle.
    matches = list(BODY_WORD.finditer(title)) or list(BODY_CONTEXT.finditer(description))
    return _unambiguous((m["value"].lower(), m[0]) for m in matches)


def _words(text):
    return re.findall(r"[^\W_]+", text.casefold(), re.UNICODE)


def _index_text(*parts):
    return " " + " ".join(_words(" ".join(parts))) + " "


def _label(value):
    # Excel stores numeric model names such as Mazda 3 as a number.
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _load_records(path):
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        rows = iter(workbook["cleaned dataset"].values)
        headers = next(rows)
        indices = [headers.index(label) for label in SOURCE_COLUMNS]
        records = []
        for row in rows:
            if all(v is None for v in row):
                continue
            listing_id, year, make, model, trim, title, description, photo = (row[i] for i in indices)
            make, model, trim = map(_label, (make, model, trim))
            texts = (title, description)
            records.append((
                int(listing_id), int(year), make, model, trim, title, description, photo,
                *_asking_price(texts), *_mileage(texts), *_color(texts), *_body_style(*texts),
                _index_text(make, model, trim, str(int(year)), title), _index_text(description),
            ))
        return records
    finally:
        workbook.close()


def init_inventory(conn: sqlite3.Connection, workbook_path=DEFAULT_WORKBOOK) -> None:
    records = _load_records(workbook_path)
    conn.execute("SAVEPOINT load_inventory")
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS listings (
                listing_id INTEGER PRIMARY KEY, year INTEGER NOT NULL, make TEXT NOT NULL,
                model TEXT NOT NULL, trim TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL,
                photo_url TEXT NOT NULL, price_aed REAL, price_evidence TEXT, mileage_km INTEGER,
                mileage_evidence TEXT, color TEXT, color_evidence TEXT, body_style TEXT,
                body_style_evidence TEXT, title_terms TEXT NOT NULL, description_terms TEXT NOT NULL
            )
        """)
        columns = (*FIELDS, "title_terms", "description_terms")
        fields = ", ".join(columns)
        placeholders = ", ".join("?" for _ in columns)
        updates = ", ".join(f"{c}=excluded.{c}" for c in columns[1:])
        conn.executemany(
            f"INSERT INTO listings ({fields}) VALUES ({placeholders}) "
            f"ON CONFLICT(listing_id) DO UPDATE SET {updates}", records,
        )
        current_ids = [r[0] for r in records]
        placeholders = ", ".join("?" for _ in current_ids)
        conn.execute(f"DELETE FROM listings WHERE listing_id NOT IN ({placeholders})", current_ids)
    except BaseException:
        conn.execute("ROLLBACK TO load_inventory")
        raise
    finally:
        conn.execute("RELEASE load_inventory")


def get_listing(conn: sqlite3.Connection, listing_id: int) -> dict | None:
    fields = ", ".join(FIELDS)
    row = conn.execute(f"SELECT {fields} FROM listings WHERE listing_id = ?", (listing_id,)).fetchone()
    return dict(zip(FIELDS, row)) if row is not None else None


STOPWORDS = set("a an the show find me cars car listings listing with of for and please".split())
MAKES = {"mercedes": "mercedes benz", "benz": "mercedes benz", "merc": "mercedes benz",
         "vw": "volkswagen", "rolls": "rolls royce"}


def search_inventory(
    conn: sqlite3.Connection, *, make=None, model=None, trim=None, min_year=None, max_year=None,
    min_price=None, max_price=None, min_mileage=None, max_mileage=None, query=None, limit=10,
    color=None, body_style=None,
) -> list[dict]:
    """AND all supplied filters. Literal keyword search favors matches in titles.

    Budget and odometer boundaries are inclusive. NULL facts never satisfy a bound.
    Results have a stable order for conversational references such as "the first one".
    """
    conditions, parameters = [], []
    for column, value in (("make", make), ("model", model), ("trim", trim),
                          ("color", color), ("body_style", body_style)):
        if not value:
            continue
        normalized = " ".join(_words(value))
        if column == "make":
            normalized = MAKES.get(normalized, normalized)
        if column == "color":
            normalized = normalized.replace("gray", "grey")
        conditions.append(f"instr(' ' || lower(replace({column}, '-', ' ')) || ' ', ?) > 0")
        parameters.append(" " + normalized + " ")
    for column, operator, value in (
        ("year", ">=", min_year), ("year", "<=", max_year), ("price_aed", ">=", min_price),
        ("price_aed", "<=", max_price), ("mileage_km", ">=", min_mileage), ("mileage_km", "<=", max_mileage),
    ):
        if value is not None:
            conditions.append(f"{column} {operator} ?")
            parameters.append(value)
    tokens = list(dict.fromkeys(t for t in _words(query or "") if t not in STOPWORDS))
    if len(tokens) > 32:
        raise ValueError("Use up to 32 search terms.")
    terms = [f" {word} " for word in tokens]
    for term in terms:
        conditions.append("(instr(title_terms, ?) > 0 OR instr(description_terms, ?) > 0)")
        parameters.extend((term, term))
    order = "listing_id"
    if terms:
        title_hits = " + ".join("(instr(title_terms, ?) > 0)" for _ in terms)
        order = f"({title_hits}) DESC, listing_id"
        parameters.extend(terms)
    where = " AND ".join(conditions) or "1"
    parameters.append(max(0, min(int(limit), 100)))
    rows = conn.execute(
        f"SELECT {', '.join(FIELDS)} FROM listings WHERE {where} ORDER BY {order} LIMIT ?", parameters
    )
    return [dict(zip(FIELDS, row)) for row in rows]
