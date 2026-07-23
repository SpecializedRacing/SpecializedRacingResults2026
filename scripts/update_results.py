#!/usr/bin/env python3
"""
Auto-discovery updater for Specialized Racing Dashboard.

Instead of a manually maintained race list, this script scrapes each
roster athlete's ProCyclingStats results page directly. No targets file
needed — new races appear automatically as athletes compete.

Strategy:
  - For every athlete in roster.json, fetch their PCS /rider/<slug>/2026 page
  - Parse all 2026 race results where pos <= MAX_POSITION
  - Merge with any manually verified additions (verified_additions.json)
  - Deduplicate and write back to data/results.json

Required repo files:
  data/roster.json
  data/results.json
Optional repo files:
  data/verified_additions.json   (manual overrides / seed data)

Roster fields used:
  rider             canonical name
  aliases           list of alternate spellings
  pcsSlug           optional PCS URL slug (e.g. "tadej-pogacar")
                    if omitted, derived automatically from rider name
  program           e.g. "S-Works Road"
  primaryDiscipline "Road" | "MTB" | etc.
  team              team name string
"""
from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import requests
    from bs4 import BeautifulSoup
except Exception:
    requests = None
    BeautifulSoup = None

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
ROSTER_FILE        = DATA / "roster.json"
RESULTS_FILE       = DATA / "results.json"
VERIFIED_FILE      = DATA / "verified_additions.json"   # optional
REVIEW_FILE        = DATA / "discovered_results_review.json"

RESULT_YEAR    = os.getenv("RESULT_YEAR", "2026")
MAX_POSITION   = int(os.getenv("MAX_POSITION", "30"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "25"))
REQUEST_PAUSE  = float(os.getenv("REQUEST_PAUSE_SECONDS", "0.8"))
# Set FORCE_RESCRAPE=1 to ignore last_date and re-pull the full season
FORCE_RESCRAPE = os.getenv("FORCE_RESCRAPE", "0") == "1"

PCS_BASE = "https://www.procyclingstats.com"

HEADERS = {
    "User-Agent": "SpecializedRacingDashboard/7.0 (+GitHub Actions)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

BAD_RACE_TERMS = [
    "general classification", "points classification", "mountains classification",
    "youth classification", "teams classification", "kom classification",
    "statistics", "ranking", "rankings", "pcs ranking", "uci ranking",
    "startlist", "history", "overview", "team ranking", "palmares",
    "profile",
]
YEAR_ONLY_RE = re.compile(r"^20\d{2}$")
DATE_FULL_RE  = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PCS_DATE_RE   = re.compile(r"^(\d{2})\.(\d{2})$")   # DD.MM on PCS pages
POS_RE        = re.compile(r"^(\d{1,3})(?:st|nd|rd|th)?$", re.I)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def norm(value: str) -> str:
    value = unicodedata.normalize("NFD", str(value or ""))
    value = "".join(ch for ch in value if unicodedata.category(ch) != "Mn")
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def pcs_slug(name: str) -> str:
    """Derive a PCS URL slug from a rider name."""
    s = unicodedata.normalize("NFD", str(name or ""))
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


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


def is_valid_date(value: str) -> bool:
    return bool(DATE_FULL_RE.match(str(value or ""))) and str(value).startswith(RESULT_YEAR)


def parse_pos(value: str) -> Optional[int]:
    m = POS_RE.match(str(value or "").strip())
    if not m:
        return None
    pos = int(m.group(1))
    return pos if 1 <= pos <= MAX_POSITION else None


def bad_race_name(race: str) -> bool:
    race = str(race or "").strip()
    race_norm = norm(race)
    if not race_norm or len(race) < 5:
        return True
    if YEAR_ONLY_RE.match(race.strip()):
        return True
    return any(term in race_norm for term in BAD_RACE_TERMS)


def clean_race_name(name: str) -> str:
    name = re.sub(r"\s+", " ", str(name or "")).strip()
    name = re.sub(r"\s*[-–]\s*ProCyclingStats\.com\s*$", "", name, flags=re.I)
    name = re.sub(r"\bOne day race results\b", "", name, flags=re.I).strip(" -–|")
    return name


def result_key(r: dict) -> str:
    return "|".join([
        str(r.get("date", "")),
        norm(r.get("race", "")),
        norm(r.get("athlete", "")),
        str(r.get("pos", "")),
    ])


# ---------------------------------------------------------------------------
# Roster
# ---------------------------------------------------------------------------

def build_roster_index(roster_payload: dict) -> Dict[str, dict]:
    idx: Dict[str, dict] = {}
    for athlete in roster_payload.get("athletes", []):
        for name in [athlete.get("rider", "")] + (athlete.get("aliases") or []):
            key = norm(name)
            if key:
                idx[key] = athlete
    return idx


def canonical_name(name: str, idx: Dict[str, dict]) -> str:
    match = idx.get(norm(name))
    return match.get("rider", name) if match else name


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def valid_result(r: dict, idx: Dict[str, dict]) -> bool:
    try:
        pos = int(r.get("pos"))
    except Exception:
        return False
    if not (1 <= pos <= MAX_POSITION):
        return False
    if not is_valid_date(r.get("date", "")):
        return False
    if bad_race_name(r.get("race", "")):
        return False
    if norm(r.get("athlete", "")) not in idx:
        return False
    return True


def clean_existing(existing: List[dict], idx: Dict[str, dict]) -> Tuple[List[dict], List[dict]]:
    clean, rejected = [], []
    seen = set()
    for row in existing:
        if valid_result(row, idx):
            row = dict(row)
            row["athlete"] = canonical_name(row.get("athlete", ""), idx)
            row.setdefault("sourceUrl", "")
            row.setdefault("sourceId", row.get("src", ""))
            key = result_key(row)
            if key not in seen:
                seen.add(key)
                clean.append(row)
        else:
            rejected.append(row)
    return clean, rejected


def latest_date(results: List[dict]) -> str:
    dates = [r.get("date", "") for r in results if is_valid_date(r.get("date", ""))]
    return max(dates) if dates else f"{RESULT_YEAR}-01-01"


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def fetch(url: str) -> Optional[str]:
    if requests is None:
        return None
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        return resp.text if resp.status_code == 200 else None
    except Exception:
        return None
    finally:
        if REQUEST_PAUSE:
            time.sleep(REQUEST_PAUSE)


# ---------------------------------------------------------------------------
# PCS rider-page scraper
# ---------------------------------------------------------------------------

def scrape_pcs_rider(athlete: dict, last_date: str) -> Tuple[List[dict], List[dict]]:
    """
    Fetch /rider/<slug>/YEAR and parse all result rows.

    PCS rider result pages have a table where each row represents one race.
    Typical columns: Date (DD.MM) | Race | Category | km | Pos | UCI pts | PCS pts
    """
    if BeautifulSoup is None:
        return [], []

    name   = athlete.get("rider", "")
    slug   = athlete.get("pcsSlug") or pcs_slug(name)
    url    = f"{PCS_BASE}/rider/{slug}/{RESULT_YEAR}"

    html = fetch(url)
    if not html:
        return [], [{"rider": name, "url": url, "reason": "fetch_failed"}]

    soup    = BeautifulSoup(html, "lxml")
    records: List[dict] = []
    review:  List[dict] = []

    # PCS renders results in <ul class="rdrResults"> or a <table>.
    # We walk every <tr> and also every <li> to cover both layouts.
    rows = soup.find_all("tr") + soup.find_all("li", class_=re.compile(r"rdrRes", re.I))

    for row in rows:
        cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["td", "th", "span", "div"])]
        if len(cells) < 3:
            continue

        date_str  = None
        pos_val   = None
        race_name = None

        # --- date: look for DD.MM pattern ---
        for cell in cells:
            m = PCS_DATE_RE.match(cell.strip())
            if m:
                day, month = m.group(1), m.group(2)
                date_str = f"{RESULT_YEAR}-{month}-{day}"
                break

        if not date_str or not is_valid_date(date_str):
            continue
        if not FORCE_RESCRAPE and date_str <= last_date:
            continue

        # --- position: scan all cells ---
        for cell in cells:
            pos_val = parse_pos(cell.strip())
            if pos_val:
                break

        if not pos_val:
            continue

        # --- race name: prefer text from a /race/ anchor ---
        for anchor in row.find_all("a", href=True):
            href = anchor.get("href", "")
            text = clean_race_name(anchor.get_text(" ", strip=True))
            if "/race/" in href and text and len(text) >= 5:
                race_name = text
                break

        # fallback: any cell that looks like a race name
        if not race_name:
            for cell in cells:
                cell = cell.strip()
                if len(cell) >= 8 and not re.match(r"^[\d\s\.]+$", cell) and not PCS_DATE_RE.match(cell):
                    race_name = clean_race_name(cell)
                    break

        if not race_name or bad_race_name(race_name):
            continue

        records.append({
            "date":       date_str,
            "race":       race_name,
            "athlete":    name,
            "pos":        pos_val,
            "src":        "ProCyclingStats",
            "sourceUrl":  url,
            "sourceId":   "pcs_rider_page",
            "program":    athlete.get("program", ""),
            "discipline": athlete.get("primaryDiscipline", "Road"),
            "team":       athlete.get("team", ""),
        })

    if not records:
        review.append({"rider": name, "url": url, "reason": "no_results_parsed"})

    return records, review


def collect_all_results(roster_payload: dict, last_date: str) -> Tuple[List[dict], List[dict]]:
    """Scrape PCS for every athlete in the roster."""
    all_records: List[dict] = []
    all_review:  List[dict] = []

    athletes = roster_payload.get("athletes", [])
    print(f"Scraping PCS for {len(athletes)} athletes …")

    for i, athlete in enumerate(athletes, 1):
        name = athlete.get("rider", "")
        print(f"  [{i}/{len(athletes)}] {name}", end=" … ", flush=True)
        records, review = scrape_pcs_rider(athlete, last_date)
        print(f"{len(records)} new result(s)")
        all_records.extend(records)
        all_review.extend(review)

    return all_records, all_review


# ---------------------------------------------------------------------------
# Verified manual additions (optional fallback / override file)
# ---------------------------------------------------------------------------

def load_verified(idx: Dict[str, dict], last_date: str) -> Tuple[List[dict], List[dict]]:
    payload = load_json(VERIFIED_FILE, {"results": []})
    accepted, rejected = [], []
    for row in payload.get("results", []):
        candidate = dict(row)
        candidate.setdefault("sourceUrl", "")
        candidate.setdefault("sourceId", "verified_seed")
        if not FORCE_RESCRAPE and candidate.get("date", "") <= last_date:
            continue
        if valid_result(candidate, idx):
            candidate["athlete"] = canonical_name(candidate.get("athlete", ""), idx)
            accepted.append(candidate)
        else:
            rejected.append({"row": row, "reason": "failed_validation"})
    return accepted, rejected


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    roster_payload  = load_json(ROSTER_FILE, {"athletes": []})
    results_payload = load_json(RESULTS_FILE, {"results": []})

    idx      = build_roster_index(roster_payload)
    existing = results_payload.get("results", [])

    clean, rejected_existing = clean_existing(existing, idx)
    last = "2026-01-01" if FORCE_RESCRAPE else latest_date(clean)

    print(f"Clean existing results : {len(clean)}")
    print(f"Scraping from          : {last} onwards (FORCE_RESCRAPE={FORCE_RESCRAPE})")

    # --- gather new results ---
    pcs_records,      pcs_review      = collect_all_results(roster_payload, last)
    verified_records, verified_review = load_verified(idx, last)

    # --- merge & deduplicate ---
    combined = list(clean)
    seen     = {result_key(r) for r in combined}
    added    = []

    for candidate in verified_records + pcs_records:
        if valid_result(candidate, idx):
            candidate["athlete"] = canonical_name(candidate.get("athlete", ""), idx)
            key = result_key(candidate)
            if key not in seen:
                seen.add(key)
                combined.append(candidate)
                added.append(candidate)

    combined.sort(key=lambda r: (r.get("date", ""), r.get("race", ""), r.get("athlete", "")))

    # --- write results ---
    save_json(RESULTS_FILE, {
        "lastUpdated":  datetime.now(timezone.utc).date().isoformat(),
        "generatedBy":  "scripts/update_results.py",
        "strategy":     "Auto per-athlete PCS rider-page scraping; no manual targets file required",
        "latestInputDate": last,
        "resultCount":  len(combined),
        "addedThisRun": len(added),
        "results":      combined,
        "schema": {
            "date":      "YYYY-MM-DD; required; current season only",
            "race":      "race or stage name",
            "athlete":   "canonical roster name",
            "pos":       f"integer 1–{MAX_POSITION}",
            "src":       "source label",
            "sourceUrl": "URL of the page parsed",
            "sourceId":  "source identifier",
        },
    })

    # --- write review log ---
    all_review = pcs_review + verified_review + rejected_existing[:100]
    save_json(REVIEW_FILE, {
        "lastUpdated":      datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note":             "Riders/rows that were skipped or failed validation this run.",
        "latestInputDate":  last,
        "addedThisRun":     len(added),
        "skippedCount":     len(all_review),
        "skipped":          all_review[:500],
    })

    print(f"\nPCS scraped (before dedupe) : {len(pcs_records)}")
    print(f"Verified additions          : {len(verified_records)}")
    print(f"Added this run (after dedupe): {len(added)}")
    print(f"Total results               : {len(combined)}")


if __name__ == "__main__":
    main()
