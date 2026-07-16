#!/usr/bin/env python3
"""
Automated results updater for the Specialized Racing Dashboard.

What this does now:
- Loads the roster and source registry.
- Builds a normalized athlete/alias lookup.
- Preserves existing results.
- Provides adapter hooks for each source.
- Writes data/results.json in the dashboard schema.

Important:
Each source website has different page structure and terms. This script is intentionally
organized as adapters so source-specific parsing can be maintained without touching the dashboard.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

try:
    import requests
    from bs4 import BeautifulSoup
except Exception:
    requests = None
    BeautifulSoup = None

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
ROSTER_FILE = DATA / "roster.json"
RESULTS_FILE = DATA / "results.json"
SOURCES_FILE = DATA / "sources.json"

MAX_POSITION = 30


def norm(value: str) -> str:
    value = value or ""
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def load_json(path: Path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")


@dataclass
class Athlete:
    rider: str
    team: str
    discipline: str
    category: str
    country: str
    aliases: List[str]
    profile_url: str = ""


def roster_lookup(roster_payload) -> Dict[str, Athlete]:
    lookup: Dict[str, Athlete] = {}
    for a in roster_payload.get("athletes", []):
        athlete = Athlete(
            rider=a.get("rider", ""),
            team=a.get("team", ""),
            discipline=a.get("primaryDiscipline", ""),
            category=a.get("category", ""),
            country=a.get("country", ""),
            aliases=a.get("aliases", []) or [],
            profile_url=a.get("profileUrl", "") or "",
        )
        for name in [athlete.rider] + athlete.aliases:
            if name:
                lookup[norm(name)] = athlete
    return lookup


def result_key(r: dict) -> str:
    return "|".join([
        r.get("date", ""),
        norm(r.get("race", "")),
        norm(r.get("athlete", "")),
        str(r.get("pos", "")),
    ])


def valid_result(r: dict, athletes: Dict[str, Athlete]) -> bool:
    try:
        pos = int(r.get("pos"))
    except Exception:
        return False
    if pos < 1 or pos > MAX_POSITION:
        return False
    if not r.get("date") or not r.get("race") or not r.get("athlete"):
        return False
    return norm(r.get("athlete")) in athletes


def fetch_html(url: str) -> Optional[str]:
    if requests is None:
        return None
    headers = {"User-Agent": "SpecializedRacingDashboard/1.0 (+GitHub Actions)"}
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code != 200:
            return None
        return resp.text
    except Exception:
        return None


def adapter_mtbdata(source: dict, athletes: Dict[str, Athlete]) -> List[dict]:
    """MTBData adapter stub.

    MTBData is registered for MTB and DH. The durable approach is:
    1. Fetch MTBData calendar/results pages.
    2. Parse race result pages.
    3. Match rider names against roster aliases.
    4. Emit normalized dashboard result records.

    This function currently returns no new rows until the exact stable page patterns/API endpoints
    are selected and validated for the races we want to track.
    """
    return []


def adapter_source_index(source: dict, athletes: Dict[str, Athlete]) -> List[dict]:
    """Generic source adapter placeholder for indexed sources."""
    return []


def adapter_generic_profile_page(source: dict, athletes: Dict[str, Athlete]) -> List[dict]:
    """Generic profile-page adapter placeholder.

    This is primarily intended for sources where roster profiles have a direct results table URL.
    """
    return []


ADAPTERS = {
    "mtbdata": adapter_mtbdata,
    "source_index": adapter_source_index,
    "generic_profile_page": adapter_generic_profile_page,
}


def collect_new_results(roster_payload, sources_payload) -> List[dict]:
    athletes = roster_lookup(roster_payload)
    new_results: List[dict] = []
    for source in sources_payload.get("sources", []):
        if not source.get("enabled", True):
            continue
        adapter_name = source.get("adapter", "source_index")
        adapter = ADAPTERS.get(adapter_name, adapter_source_index)
        rows = adapter(source, athletes)
        for row in rows:
            if valid_result(row, athletes):
                row.setdefault("src", source.get("name", source.get("id", "")))
                row.setdefault("sourceId", source.get("id", ""))
                new_results.append(row)
    return new_results


def main() -> int:
    roster_payload = load_json(ROSTER_FILE, {"athletes": []})
    sources_payload = load_json(SOURCES_FILE, {"sources": []})
    results_payload = load_json(RESULTS_FILE, {"results": []})

    existing = results_payload.get("results", [])
    existing_keys = {result_key(r) for r in existing}

    collected = collect_new_results(roster_payload, sources_payload)
    additions = []
    for r in collected:
        key = result_key(r)
        if key not in existing_keys:
            additions.append(r)
            existing_keys.add(key)

    combined = existing + additions
    combined.sort(key=lambda r: (r.get("date", ""), r.get("race", ""), r.get("athlete", "")))

    output = {
        "lastUpdated": datetime.now(timezone.utc).date().isoformat(),
        "generatedBy": "scripts/update_results.py",
        "resultCount": len(combined),
        "addedThisRun": len(additions),
        "results": combined,
        "schema": {
            "date": "YYYY-MM-DD",
            "race": "string",
            "athlete": "name matching roster rider or alias",
            "pos": "number 1-30",
            "src": "source label",
            "sourceUrl": "optional evidence URL",
            "sourceId": "optional source registry id"
        }
    }
    save_json(RESULTS_FILE, output)
    print(f"Existing results: {len(existing)}")
    print(f"Collected new results: {len(collected)}")
    print(f"Added this run: {len(additions)}")
    print(f"Total results: {len(combined)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
