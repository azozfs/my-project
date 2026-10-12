"""Convert the rental department's Excel sheet into data/units.json for the agent.

Usage:
    python import_excel.py "عقارات للإيجار جديد.xlsx"            # writes data/units.json
    python import_excel.py file.xlsx --out other.json --skip "عقارات خارجية محدث"

Every sheet is read; the header row is found automatically (the row with "م" plus
known column names). Re-run it whenever the sheet changes — the agent reloads the
file on its own, no restart needed.
"""

import argparse
import json
import re
import sys
from pathlib import Path

import openpyxl

STATUSES = {"شاغر", "مؤجر", "غير متاح"}
AC_WORDS = {"اسبليت", "سبليت", "مركزي", "شباك", "دولابي", "مخفي", "كونسيلد"}

# Header text (normalized) -> unit field
ALIASES = {
    "رقمالوحده": "unit_no",
    "رقمالمحل": "unit_no",
    "رقمالمكتب": "unit_no",
    "مركزالتكلفه": "building",
    "اسمالعقار": "building",
    "العقار": "building",
    "المبني": "building",
    "الحي": "district",
    "الدور": "floor",
    "الحاله": "status",
    "غرفه/غرف": "rooms",
    "الغرف": "rooms",
    "عددالغرف": "rooms",
    "صاله": "halls",
    "دوراتمياه": "bathrooms",
    "النوع": "type_raw",
    "نوعالاستديو": "studio_type",
    "حالهالمطبخ": "kitchen",
    "المساحه": "area_m2",
    "مساحهالوحده": "area_m2",
    "الايجارالسنوي": "annual_rent",
    "الايجار": "annual_rent",
    "الايجارالشهري": "monthly_rent",
    "التامين": "insurance",
    "الخدمات": "services_fee",
    "الدلالهورسومالتاجير": "commission",
    "الدلاله(شاملضريبه)": "commission",
    "الدلاله": "commission",
    "عددالدفعات": "payments",
    "الرابط": "ad_link",
    "حالهالوحده": "unit_condition",
    "ضريبهالايجار": "rent_vat",
    "الاجماليالسنوي": "annual_total",
    "الكهرباء": "electricity",
}
NUMERIC = {"rooms", "halls", "bathrooms", "area_m2", "annual_rent", "monthly_rent", "insurance",
           "services_fee", "commission", "payments", "rent_vat", "annual_total"}
INT_FIELDS = {"rooms", "halls", "bathrooms", "payments"}


def norm(text) -> str:
    s = str(text).strip()
    for a, b in (("أ", "ا"), ("إ", "ا"), ("آ", "ا"), ("ة", "ه"), ("ى", "ي")):
        s = s.replace(a, b)
    return re.sub(r"\s+", "", s)


def to_number(value):
    if value is None or value == "" or value == "-":
        return None
    if isinstance(value, (int, float)):
        return value
    m = re.search(r"-?\d[\d,]*\.?\d*", str(value))
    return float(m.group().replace(",", "")) if m else None


def clean_text(value):
    if value is None:
        return None
    s = str(value).strip()
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s or None


def district_from_building(building: str | None) -> str | None:
    if not building:
        return None
    first = re.split(r"[_\-–]", building)[0].strip()
    first = re.sub(r"^(عماره|عمارة|مجمع|مبنى|مبني)\s+", "", first)
    return first or None


def category_for_sheet(name: str) -> str:
    for cat in ("تجاري", "إداري", "اداري", "سكني"):
        if cat in name:
            return "إداري" if cat == "اداري" else cat
    return "سكني"


def find_header(rows: list[tuple]) -> tuple[int, dict[int, str]] | None:
    """Return (last header row index, {column: field}).

    Headers may span up to three rows (a merged group title like "بيانات الوحدة"
    with the real column names under it), so the rows below the "م" row are
    merged in until the first numbered data row.
    """
    for i, row in enumerate(rows[:25]):
        cells = {j: norm(v) for j, v in enumerate(row) if v is not None and str(v).strip()}
        idx_col = next((j for j, c in cells.items() if c == "م"), None)
        if idx_col is None:
            continue
        mapping: dict[int, str] = {}
        last = i
        for k in range(i, min(i + 3, len(rows))):
            if k > i and to_number(rows[k][idx_col] if idx_col < len(rows[k]) else None) is not None:
                break
            for j, v in enumerate(rows[k]):
                if v is not None and norm(v) in ALIASES and j not in mapping:
                    mapping[j] = ALIASES[norm(v)]
            last = k
        if len(mapping) >= 3 and "status" in mapping.values():
            mapping[-1] = idx_col  # type: ignore[assignment]
            return last, mapping
    return None


def sheet_context(rows: list[tuple], header_row: int) -> tuple[str | None, str | None, list[str]]:
    """Building name, district and general notes written above the table (e.g. Haladeel sheet)."""
    building = district = None
    notes = []
    for row in rows[:header_row]:
        vals = [v for v in row if v is not None and str(v).strip()]
        for a, b in zip(vals, vals[1:]):
            if norm(a) == "مركزالتكلفه" and building is None:
                building = clean_text(b)
            if norm(a) == "الموقع" and district is None:
                district = re.sub(r"^حي\s+", "", str(b).strip())
        for v in vals:
            s = str(v).strip()
            # Free-text notes for customers (e.g. "رسوم حارس 50 ريال"); short labels are skipped.
            words = sum(1 for w in s.split() if re.search(r"[^\W\d_]", w))
            if words >= 4 or (words >= 2 and re.search(r"\d", s) and len(s.split()) >= 3):
                notes.append(s)
    return building, district, notes


def import_workbook(path: Path, skip: set[str]) -> list[dict]:
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    units = []
    for ws in wb.worksheets:
        if ws.title in skip or ws.sheet_state != "visible":
            continue
        rows = [tuple(r) for r in ws.iter_rows(values_only=True)]
        found = find_header(rows)
        if not found:
            print(f"  ⚠ تخطيت ورقة «{ws.title}»: ما لقيت صف العناوين", file=sys.stderr)
            continue
        header_row, mapping = found
        idx_col = mapping.pop(-1)
        sheet_building, sheet_district, sheet_notes = sheet_context(rows, header_row)
        category = category_for_sheet(ws.title)
        count = 0
        for row in rows[header_row + 1 :]:
            if idx_col >= len(row) or to_number(row[idx_col]) is None:
                continue
            raw = {f: row[j] for j, f in mapping.items() if j < len(row)}
            status = clean_text(raw.get("status"))
            if status not in STATUSES:
                continue
            unit = {"category": category, "status": status}
            for f, v in raw.items():
                if f == "status":
                    continue
                if f in NUMERIC:
                    n = to_number(v)
                    unit[f] = int(n) if (n is not None and f in INT_FIELDS) else n
                else:
                    unit[f] = clean_text(v)
            notes = list(sheet_notes)
            type_raw = unit.pop("type_raw", None)
            if type_raw:
                if type_raw in AC_WORDS:
                    unit["ac"] = type_raw
                else:
                    notes.append(f"النوع: {type_raw}")
            if studio := unit.pop("studio_type", None):
                notes.append(f"استديو {studio}")
            if cond := unit.pop("unit_condition", None):
                notes.append(f"حالة الوحدة: {cond}")
            if (vat := unit.pop("rent_vat", None)) is not None:
                notes.append(f"ضريبة الإيجار: {vat:g} ريال")
            if (total := unit.pop("annual_total", None)) is not None:
                notes.append(f"الإجمالي السنوي شامل الخدمات والضريبة: {total:g} ريال")
            if elec := unit.pop("electricity", None):
                notes.append(f"الكهرباء: {elec}")
            unit["building"] = unit.get("building") or sheet_building
            unit["district"] = unit.get("district") or sheet_district or district_from_building(unit["building"])
            unit["notes"] = " · ".join(notes) or None
            unit["id"] = f"{ws.title}:{unit.get('building') or ''}:{unit.get('unit_no') or count}"
            units.append(unit)
            count += 1
        print(f"  ✓ «{ws.title}»: {count} وحدة", file=sys.stderr)
    return units


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("xlsx", type=Path)
    parser.add_argument("--out", type=Path, default=Path(__file__).parent / "data" / "units.json")
    parser.add_argument("--skip", action="append", default=[], help="اسم ورقة تبي تتجاهلها (تقدر تكررها)")
    args = parser.parse_args()

    units = import_workbook(args.xlsx, set(args.skip))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_suffix(".tmp")
    tmp.write_text(json.dumps(units, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(args.out)
    vacant = sum(1 for u in units if u["status"] == "شاغر")
    print(f"تم: {len(units)} وحدة ({vacant} شاغرة) → {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
