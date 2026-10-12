"""Rental units inventory: loaded from data/units.json (built by import_excel.py)."""

import json
from pathlib import Path

VACANT = "شاغر"


def _norm(text: str) -> str:
    """Loose Arabic matching: ignore spaces, hamza forms, taa marbuta and the leading 'ال'."""
    text = text.strip()
    for a, b in (("أ", "ا"), ("إ", "ا"), ("آ", "ا"), ("ة", "ه"), ("ى", "ي")):
        text = text.replace(a, b)
    text = text.replace(" ", "")
    return text[2:] if text.startswith("ال") else text


class Inventory:
    def __init__(self, path: Path):
        self.path = path
        self._units: list[dict] = []
        self._mtime: float | None = None

    def _load(self) -> list[dict]:
        # Reload whenever the file changes, so staff can re-import without a restart.
        if not self.path.exists():
            return []
        mtime = self.path.stat().st_mtime
        if mtime != self._mtime:
            self._units = json.loads(self.path.read_text(encoding="utf-8"))
            self._mtime = mtime
        return self._units

    def search(
        self,
        category: str | None = None,
        district: str | None = None,
        max_annual_rent: float | None = None,
        min_rooms: int | None = None,
        limit: int = 8,
    ) -> list[dict]:
        results = []
        for u in self._load():
            if u.get("status") != VACANT:
                continue
            if category and u.get("category") != category:
                continue
            if district:
                where = _norm(f"{u.get('district') or ''}{u.get('building') or ''}")
                if _norm(district) not in where:
                    continue
            if max_annual_rent is not None and (u.get("annual_rent") or 0) > max_annual_rent:
                continue
            if min_rooms is not None and (u.get("rooms") or 0) < min_rooms:
                continue
            results.append(u)
        results.sort(key=lambda u: u.get("annual_rent") or 0)
        return results[:limit]

    def districts(self) -> list[str]:
        return sorted({u["district"] for u in self._load() if u.get("district") and u.get("status") == VACANT})
