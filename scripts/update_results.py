#!/usr/bin/env python3
"""
Strict 2026 updater for Specialized Racing Dashboard.

This version is designed for the current milestone:
- Keep the clean Claude seed up to 2026-05-28.
- Add only valid 2026 records after the latest date in data/results.json.
- Every published record must have a real YYYY-MM-DD date.
- Never publish PCS rider-profile / all-time / classification rows.
- Merge verified post-May-28 seed records while source parsers mature.
- Parse only explicitly targeted PCS race/stage/result pages with known dates.

Required repo files:
  data/roster.json
  data/results.json
Optional repo files:
  data/verified_additions_after_2026_05_28.json
  data/pcs_road_targets.json
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
from urllib.parse import urljoin

try:
    import requests
    from bs4 import BeautifulSoup
except Exception:  # allows validation-only execution if dependencies are unavailable
    requests = None
    BeautifulSoup = None

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
ROSTER_FILE = DATA / "roster.json"
RESULTS_FILE = DATA / "results.json"
VERIFIED_ADDITIONS_FILE = DATA / "verified_additions_after_2026_05_28.json"
PCS_TARGETS_FILE = DATA / "pcs_road_targets.json"
REVIEW_FILE = DATA / "discovered_results_review.json"

RESULT_YEAR = os.getenv("RESULT_YEAR", "2026")
MAX_POSITION = int(os.getenv("MAX_POSITION", "30"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "25"))
REQUEST_PAUSE_SECONDS = float(os.getenv("REQUEST_PAUSE_SECONDS", "0.5"))

HEADERS = {
    "User-Agent": "SpecializedRacingDashboard/6.0 (+GitHub Actions)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

BAD_RACE_TERMS = [
    "general classification", "points classification", "mountains classification",
    "youth classification", "teams classification", "kom classification",
    "statistics", "ranking", "rankings", "pcs ranking", "uci ranking",
    "startlist", "profile", "history", "overview", "team ranking", "rider", "palmares"
]
YEAR_ONLY_RE = re.compile(r"^20\d{2}$")
DATE_FULL_RE = re.compile(r"^2026-\d{2}-\d{2}$")
POS_RE = re.compile(r"^(\d{1,2})(?:st|nd|rd|th)?$", re.I)


def norm(value: str) -> str:
    value = unicodedata.normalize("NFD", str(value or ""))
    value = "".join(ch for ch in value if unicodedata.category(ch) != "Mn")
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


def is_2026_date(value: str) -> bool:
    return bool(DATE_FULL_RE.match(str(value or "")))


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
    if YEAR_ONLY_RE.match(race):
        return True
    return any(term in race_norm for term in BAD_RACE_TERMS)


def roster_index(roster_payload: dict) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for athlete in roster_payload.get("athletes", []):
        for name in [athlete.get("rider", "")] + (athlete.get("aliases") or []):
            key = norm(name)
            if key:
                out[key] = athlete
    return out


def find_roster_match(text_cells: List[str], idx: Dict[str, dict]) -> Optional[dict]:
    joined = norm(" ".join(text_cells))
    # Longest first to reduce false positive partial matches.
    for key in sorted(idx.keys(), key=len, reverse=True):
        if len(key) > 3 and key in joined:
            return idx[key]
    return None


def canonical_athlete_name(name: str, idx: Dict[str, dict]) -> str:
    match = idx.get(norm(name))
    return match.get("rider", name) if match else name


def result_key(result: dict) -> str:
    return "|".join([
        str(result.get("date", "")),
        norm(result.get("race", "")),
        norm(result.get("athlete", "")),
        str(result.get("pos", ""))
    ])


def valid_result(result: dict, idx: Dict[str, dict]) -> bool:
    try:
        position = int(result.get("pos"))
    except Exception:
        return False
    if not (1 <= position <= MAX_POSITION):
        return False
    if not is_2026_date(result.get("date", "")):
        return False
    if bad_race_name(result.get("race", "")):
        return False
    if norm(result.get("athlete", "")) not in idx:
        return False
    return True


def clean_existing_results(existing: List[dict], idx: Dict[str, dict]) -> Tuple[List[dict], List[dict]]:
    clean: List[dict] = []
    rejected: List[dict] = []
    seen = set()
    for row in existing:
        if valid_result(row, idx):
            row = dict(row)
            row["athlete"] = canonical_athlete_name(row.get("athlete", ""), idx)
            row.setdefault("sourceUrl", "")
            row.setdefault("sourceId", row.get("src", ""))
            key = result_key(row)
            if key not in seen:
                seen.add(key)
                clean.append(row)
        else:
            rejected.append(row)
    return clean, rejected


def latest_2026_date(clean_results: List[dict]) -> str:
    dates = [r.get("date", "") for r in clean_results if is_2026_date(r.get("date", ""))]
    return max(dates) if dates else "2026-01-01"


def fetch(url: str) -> Optional[str]:
    if requests is None:
        return None
    try:
        response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if response.status_code != 200:
            return None
        return response.text
    except Exception:
        return None
    finally:
        if REQUEST_PAUSE_SECONDS:
            time.sleep(REQUEST_PAUSE_SECONDS)


def clean_race_name(name: str) -> str:
    name = re.sub(r"\s+", " ", str(name or "")).strip()
    name = re.sub(r"\s*-\s*ProCyclingStats\.com\s*$", "", name, flags=re.I)
    name = re.sub(r"\bOne day race results\b", "", name, flags=re.I).strip(" -–|")
    return name


def discover_pcs_links(url: str, html: str) -> List[str]:
    if BeautifulSoup is None:
        return []
    soup = BeautifulSoup(html, "lxml")
    links = []
    for anchor in soup.find_all("a", href=True):
        full = urljoin(url, anchor["href"])
        if "procyclingstats.com/race/" not in full:
            continue
        lower = full.lower()
        if any(token in lower for token in ["/result", "/stage-", "/results"]):
            links.append(full)
    if "/race/" in url and not any(token in url.lower() for token in ["/result", "/stage-", "/results"]):
        links.append(url.rstrip("/") + "/result/result/result")
        links.append(url.rstrip("/") + "/results")
    seen = set()
    out = []
    for link in links:
        if link not in seen:
            seen.add(link)
            out.append(link)
    return out[:100]


def parse_pcs_result_page(url: str, html: str, target: dict, idx: Dict[str, dict]) -> Tuple[List[dict], List[dict]]:
    if BeautifulSoup is None:
        return [], []
    date = target.get("date", "")
    race = clean_race_name(target.get("name", ""))
    if not is_2026_date(date) or bad_race_name(race):
        return [], [{"sourceUrl": url, "target": target, "reason": "target_missing_valid_2026_date_or_race"}]
    soup = BeautifulSoup(html, "lxml")
    records: List[dict] = []
    review: List[dict] = []
    for tr in soup.find_all("tr"):
        cells = [cell.get_text(" ", strip=True) for cell in tr.find_all(["td", "th"])]
        if len(cells) < 4:
            continue
        pos = None
        for cell in cells[:4]:
            pos = parse_pos(cell)
            if pos:
                break
        if not pos:
            continue
        athlete = find_roster_match(cells, idx)
        if not athlete:
            continue
        records.append({
            "date": date,
            "race": race,
            "athlete": athlete.get("rider", ""),
            "pos": pos,
            "src": "ProCyclingStats",
            "sourceUrl": url,
            "sourceId": "pcs_race_page",
            "program": athlete.get("program", ""),
            "discipline": athlete.get("primaryDiscipline", "Road"),
            "team": athlete.get("team", "")
        })
    if not records:
        review.append({"sourceUrl": url, "target": target, "reason": "no_roster_matches_found_on_pcs_page"})
    return records, review


def flatten_pcs_targets(payload: dict) -> List[dict]:
    targets: List[dict] = []
    for target in payload.get("targets", []):
        if isinstance(target, str):
            targets.append({"url": target})
        elif isinstance(target, dict):
            targets.append(target)
            for stage in target.get("stages", []) or []:
                if isinstance(stage, dict):
                    targets.append(stage)
    return [t for t in targets if t.get("url")]


def collect_pcs_targets(idx: Dict[str, dict], last_date: str) -> Tuple[List[dict], List[dict]]:
    payload = load_json(PCS_TARGETS_FILE, {"targets": []})
    records: List[dict] = []
    review: List[dict] = []
    queue: List[dict] = []
    seen_urls = set()
    for target in flatten_pcs_targets(payload):
        if not is_2026_date(target.get("date", "")):
            review.append({"target": target, "reason": "target_without_2026_date"})
            continue
        if target.get("date", "") <= last_date:
            # Existing seed already covers this date or later; skip repeat scraping.
            continue
        url = target.get("url")
        if url and url not in seen_urls:
            seen_urls.add(url)
            queue.append(target)
    while queue:
        target = queue.pop(0)
        url = target.get("url")
        html = fetch(url)
        if not html:
            review.append({"target": target, "reason": "fetch_failed"})
            continue
        # Parse the target itself when it looks like an explicit result/stage page.
        if any(token in url.lower() for token in ["/result", "/stage-", "/results"]):
            parsed, rv = parse_pcs_result_page(url, html, target, idx)
            records.extend(parsed)
            review.extend(rv)
        # From overview pages, discover stage/result pages, inheriting date only when target has it.
        for link in discover_pcs_links(url, html):
            if link not in seen_urls:
                seen_urls.add(link)
                queue.append({"name": target.get("name", ""), "date": target.get("date", ""), "url": link})
    return records, review


def load_verified_additions(idx: Dict[str, dict], last_date: str) -> Tuple[List[dict], List[dict]]:
    payload = load_json(VERIFIED_ADDITIONS_FILE, {"results": []})
    rows = payload.get("results", [])
    accepted: List[dict] = []
    rejected: List[dict] = []
    for row in rows:
        candidate = dict(row)
        candidate.setdefault("sourceUrl", "")
        candidate.setdefault("sourceId", "verified_post_may_28_seed")
        if candidate.get("date", "") <= last_date:
            continue
        if valid_result(candidate, idx):
            candidate["athlete"] = canonical_athlete_name(candidate.get("athlete", ""), idx)
            accepted.append(candidate)
        else:
            rejected.append({"row": row, "reason": "verified_addition_failed_validation"})
    return accepted, rejected


def main():
    roster_payload = load_json(ROSTER_FILE, {"athletes": []})
    results_payload = load_json(RESULTS_FILE, {"results": []})
    idx = roster_index(roster_payload)
    existing = results_payload.get("results", [])
    clean, rejected_existing = clean_existing_results(existing, idx)
    last_date = latest_2026_date(clean)

    verified_additions, verified_review = load_verified_additions(idx, last_date)
    pcs_additions, pcs_review = collect_pcs_targets(idx, last_date)

    combined = list(clean)
    seen = {result_key(r) for r in combined}
    added_this_run = []
    for candidate in verified_additions + pcs_additions:
        if valid_result(candidate, idx):
            candidate["athlete"] = canonical_athlete_name(candidate.get("athlete", ""), idx)
            key = result_key(candidate)
            if key not in seen:
                seen.add(key)
                combined.append(candidate)
                added_this_run.append(candidate)

    combined.sort(key=lambda row: (row.get("date", ""), row.get("race", ""), row.get("athlete", "")))
    output = {
        "lastUpdated": datetime.now(timezone.utc).date().isoformat(),
        "generatedBy": "scripts/update_results.py",
        "strategy": "Strict 2026 only; latest-date incremental update; verified additions + targeted PCS event pages; no rider-profile scraping",
        "latestInputDate": last_date,
        "resultCount": len(combined),
        "addedThisRun": len(added_this_run),
        "results": combined,
        "schema": {
            "date": "YYYY-MM-DD; required; 2026 only",
            "race": "valid race/stage name",
            "athlete": "roster athlete or alias canonicalized to rider",
            "pos": "number 1-30",
            "src": "source label",
            "sourceUrl": "evidence URL when available",
            "sourceId": "source identifier"
        }
    }
    save_json(RESULTS_FILE, output)
    save_json(REVIEW_FILE, {
        "lastUpdated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": "Rejected or skipped candidates from strict post-May-28 update.",
        "latestInputDate": last_date,
        "candidateCount": len(rejected_existing) + len(verified_review) + len(pcs_review),
        "rejectedExistingCount": len(rejected_existing),
        "verifiedReviewCount": len(verified_review),
        "pcsReviewCount": len(pcs_review),
        "candidates": (verified_review + pcs_review + rejected_existing[:100])[:500]
    })
    print(f"Clean existing strict-2026 results: {len(clean)}")
    print(f"Latest input date: {last_date}")
    print(f"Verified additions accepted: {len(verified_additions)}")
    print(f"PCS additions accepted before dedupe: {len(pcs_additions)}")
    print(f"Added this run after dedupe: {len(added_this_run)}")
    print(f"Total strict-2026 results: {len(combined)}")

if __name__ == "__main__":
    main()
