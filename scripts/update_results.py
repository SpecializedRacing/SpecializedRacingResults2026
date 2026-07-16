#!/usr/bin/env python3
"""
Strict 2026 results updater for Specialized Racing Dashboard.

Fixes in this version:
1. Publishes only 2026 records.
2. Requires a real YYYY-MM-DD date for every published record.
3. Removes prior polluted PCS rider-profile rows.
4. Uses PCS event/result/stage pages only for Road.
5. Sends records with missing dates to review instead of the dashboard.
"""
from __future__ import annotations

import json, os, re, time, unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
ROSTER_FILE = DATA / "roster.json"
RESULTS_FILE = DATA / "results.json"
SOURCES_FILE = DATA / "sources.json"
PCS_TARGETS_FILE = DATA / "pcs_road_targets.json"
REVIEW_FILE = DATA / "discovered_results_review.json"

RESULT_YEAR = os.getenv("RESULT_YEAR", "2026")
MAX_POSITION = int(os.getenv("MAX_POSITION", "30"))
REQUEST_TIMEOUT = 25
REQUEST_PAUSE_SECONDS = float(os.getenv("REQUEST_PAUSE_SECONDS", "0.5"))

HEADERS = {
    "User-Agent": "SpecializedRacingDashboard/5.0 (+GitHub Actions)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

DATE_RE = re.compile(r"\b(20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]20\d{2}|\d{1,2}\s+[A-Za-z]{3,9}\s+20\d{2}|\d{2}/\d{2}|\d{2}-\d{2})\b")
YEAR_ONLY_RE = re.compile(r"^20\d{2}$")
BAD_RACE_TERMS = [
    "general classification", "points classification", "mountains classification",
    "youth classification", "teams classification", "kom classification",
    "statistics", "ranking", "rankings", "pcs ranking", "uci ranking",
    "startlist", "profile", "history", "overview", "rider", "team ranking"
]


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


def fetch(url: str) -> Optional[str]:
    try:
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            return None
        return r.text
    except Exception:
        return None
    finally:
        if REQUEST_PAUSE_SECONDS:
            time.sleep(REQUEST_PAUSE_SECONDS)


def normalize_date(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    # mm/dd or mm-dd from PCS pages => 2026-mm-dd
    m = re.match(r"^(\d{2})[/-](\d{2})$", value)
    if m:
        mm, dd = int(m.group(1)), int(m.group(2))
        if 1 <= mm <= 12 and 1 <= dd <= 31:
            return f"{RESULT_YEAR}-{mm:02d}-{dd:02d}"
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y"):
        try:
            parsed = datetime.strptime(value, fmt).strftime("%Y-%m-%d")
            return parsed if parsed.startswith(f"{RESULT_YEAR}-") else ""
        except Exception:
            pass
    return ""


def is_2026_date(date: str) -> bool:
    return bool(re.match(rf"^{RESULT_YEAR}-\d{{2}}-\d{{2}}$", str(date or "")))


def parse_pos(value: str) -> Optional[int]:
    value = str(value or "").strip()
    m = re.match(r"^(\d{1,2})(?:st|nd|rd|th)?$", value, re.I)
    if not m:
        return None
    p = int(m.group(1))
    return p if 1 <= p <= MAX_POSITION else None


def result_key(r: dict) -> str:
    return "|".join([str(r.get("date", "")), norm(r.get("race", "")), norm(r.get("athlete", "")), str(r.get("pos", ""))])


def bad_race_name(race: str) -> bool:
    race = str(race or "").strip()
    r = norm(race)
    if not r or len(race) < 5:
        return True
    if YEAR_ONLY_RE.match(race):
        return True
    return any(term in r for term in BAD_RACE_TERMS)


def clean_race_name(text: str) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    text = re.sub(r"\s*-\s*ProCyclingStats\.com\s*$", "", text, flags=re.I)
    text = re.sub(r"\bOne day race results\b", "", text, flags=re.I).strip(" -–|")
    return text


def get_page_title(soup: BeautifulSoup) -> str:
    for selector in ["h1", "h2", "title"]:
        node = soup.find(selector) if selector != "title" else soup.title
        if node and node.get_text(strip=True):
            return clean_race_name(node.get_text(" ", strip=True))
    return ""


def roster_lookup(roster: List[dict]) -> Dict[str, dict]:
    out = {}
    for a in roster:
        for name in [a.get("rider", "")] + (a.get("aliases") or []):
            k = norm(name)
            if k:
                out[k] = a
    return out


def find_roster_match_from_cells(cells: List[str], lookup: Dict[str, dict]) -> Optional[dict]:
    joined = norm(" ".join(cells))
    # prefer longer keys to avoid substring collisions
    for key in sorted(lookup.keys(), key=len, reverse=True):
        if len(key) > 3 and key in joined:
            return lookup[key]
    return None


def parse_pcs_result_page(url: str, html: str, roster_index: Dict[str, dict], default_race: str = "", default_date: str = "") -> Tuple[List[dict], List[dict]]:
    soup = BeautifulSoup(html, "lxml")
    page_title = get_page_title(soup)
    race_name = clean_race_name(default_race or page_title)

    page_text = soup.get_text("\n", strip=True)
    date = normalize_date(default_date)
    if not date:
        m = DATE_RE.search(page_text)
        if m:
            date = normalize_date(m.group(1))

    records, review = [], []
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if len(cells) < 4:
            continue
        pos = None
        for c in cells[:4]:
            pos = parse_pos(c)
            if pos:
                break
        if not pos:
            continue
        athlete = find_roster_match_from_cells(cells, roster_index)
        if not athlete:
            continue
        if bad_race_name(race_name):
            review.append({"sourceUrl": url, "rawRow": " | ".join(cells), "reason": "bad_or_missing_race_name"})
            continue
        if not is_2026_date(date):
            review.append({"sourceUrl": url, "race": race_name, "athlete": athlete.get("rider", ""), "pos": pos, "reason": "missing_or_non_2026_date"})
            continue
        records.append({
            "date": date,
            "race": race_name,
            "athlete": athlete.get("rider", ""),
            "pos": pos,
            "src": "pcs_race_page",
            "sourceUrl": url,
            "sourceId": "pcs_race_page",
            "program": athlete.get("program", ""),
            "discipline": athlete.get("primaryDiscipline", "Road"),
            "team": athlete.get("team", "")
        })
    return records, review


def discover_pcs_links(url: str, html: str) -> List[str]:
    soup = BeautifulSoup(html, "lxml")
    links = []
    for a in soup.find_all("a", href=True):
        full = urljoin(url, a["href"])
        if "procyclingstats.com/race/" not in full:
            continue
        lower = full.lower()
        if any(token in lower for token in ["/result", "/stage-", "/gc", "/results"]):
            links.append(full)
    if "/race/" in url and not any(x in url for x in ["/result", "/stage-"]):
        links.append(url.rstrip("/") + "/result/result/result")
        links.append(url.rstrip("/") + "/results")
    seen, out = set(), []
    for l in links:
        if l not in seen:
            seen.add(l)
            out.append(l)
    return out[:80]


def target_entries(payload: dict) -> List[dict]:
    out = []
    for t in payload.get("targets", []):
        if isinstance(t, str):
            out.append({"url": t})
        elif isinstance(t, dict):
            out.append(t)
            for stage in t.get("stages", []) or []:
                if isinstance(stage, dict) and stage.get("url"):
                    stage.setdefault("name", t.get("name", ""))
                    out.append(stage)
    return out


def collect_pcs_event_results(roster: List[dict]) -> Tuple[List[dict], List[dict]]:
    payload = load_json(PCS_TARGETS_FILE, {"targets": []})
    road_lookup = roster_lookup([a for a in roster if (a.get("primaryDiscipline") or "").lower() == "road"])
    records, review = [], []
    queue, seen, meta = [], set(), {}
    for t in target_entries(payload):
        u = t.get("url")
        if not u:
            continue
        if u not in seen:
            seen.add(u)
            queue.append(u)
        meta[u] = {"name": t.get("name", ""), "date": t.get("date", "")}

    i = 0
    while i < len(queue):
        url = queue[i]
        i += 1
        html = fetch(url)
        if not html:
            continue
        for l in discover_pcs_links(url, html):
            if l not in seen:
                seen.add(l)
                queue.append(l)
                # inherit metadata from parent when discovering stage/result links
                meta[l] = meta.get(url, {})
        if any(token in url.lower() for token in ["/result", "/stage-", "/results"]):
            m = meta.get(url, {})
            recs, rev = parse_pcs_result_page(url, html, road_lookup, m.get("name", ""), m.get("date", ""))
            records.extend(recs)
            review.extend(rev)
    return records, review


def collect_mtbdata_results(roster: List[dict]) -> Tuple[List[dict], List[dict]]:
    # Keep MTBData conservative: publish only if MTBData row has a parsed 2026 date.
    records, review = [], []
    for a in roster:
        disc = (a.get("primaryDiscipline") or "").lower()
        if disc not in {"mtb", "dh"}:
            continue
        profile = a.get("profileUrl") or ""
        if "mtbdata.com/riders/" not in profile:
            rider_slug = norm(a.get("rider", "")).replace(" ", "-")
            profile = f"https://mtbdata.com/riders/{rider_slug}" if rider_slug else ""
        if not profile:
            continue
        html = fetch(profile)
        if not html:
            continue
        soup = BeautifulSoup(html, "lxml")
        for tr in soup.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if len(cells) < 2:
                continue
            pos = None
            for c in cells[:4]:
                pos = parse_pos(c)
                if pos:
                    break
            if not pos:
                continue
            date = ""
            for c in cells:
                m = DATE_RE.search(c)
                if m:
                    date = normalize_date(m.group(1))
                    break
            candidates = [c for c in cells if len(c) > 8 and parse_pos(c) is None and not DATE_RE.search(c)]
            race = max(candidates, key=len) if candidates else ""
            if bad_race_name(race):
                continue
            if not is_2026_date(date):
                review.append({"sourceUrl": profile, "race": race, "athlete": a.get("rider", ""), "pos": pos, "reason": "missing_or_non_2026_date"})
                continue
            records.append({
                "date": date,
                "race": race,
                "athlete": a.get("rider", ""),
                "pos": pos,
                "src": "mtbdata_rider_page",
                "sourceUrl": profile,
                "sourceId": "mtbdata_rider_page",
                "program": a.get("program", ""),
                "discipline": a.get("primaryDiscipline", ""),
                "team": a.get("team", "")
            })
    return records, review


def valid_record(r: dict) -> bool:
    try:
        pos = int(r.get("pos"))
    except Exception:
        return False
    return bool(is_2026_date(r.get("date", "")) and r.get("race") and r.get("athlete") and 1 <= pos <= MAX_POSITION and not bad_race_name(r.get("race", "")))


def clean_existing(existing: List[dict]) -> Tuple[List[dict], int]:
    cleaned, removed = [], 0
    for r in existing:
        if not valid_record(r):
            removed += 1
            continue
        if r.get("src") in {"pcs_slug_guess", "profileUrl"} or r.get("sourceId") in {"pcs_slug_guess", "profileUrl"}:
            removed += 1
            continue
        cleaned.append(r)
    return cleaned, removed


def main():
    roster_payload = load_json(ROSTER_FILE, {"athletes": []})
    results_payload = load_json(RESULTS_FILE, {"results": []})
    roster = roster_payload.get("athletes", [])
    existing = results_payload.get("results", [])
    cleaned_existing, removed = clean_existing(existing)
    keys = {result_key(r) for r in cleaned_existing}

    pcs_records, pcs_review = collect_pcs_event_results(roster)
    mtb_records, mtb_review = collect_mtbdata_results(roster)
    candidates = pcs_records + mtb_records
    additions = []
    for r in candidates:
        if not valid_record(r):
            continue
        k = result_key(r)
        if k not in keys:
            keys.add(k)
            additions.append(r)
    combined = cleaned_existing + additions
    combined.sort(key=lambda r: (r.get("date", ""), r.get("race", ""), r.get("athlete", "")))

    output = {
        "lastUpdated": datetime.now(timezone.utc).date().isoformat(),
        "generatedBy": "scripts/update_results.py",
        "strategy": "Strict 2026 only; requires real date; PCS road from race/stage/result pages only",
        "resultCount": len(combined),
        "addedThisRun": len(additions),
        "removedInvalidOrNon2026Rows": removed,
        "results": combined,
        "schema": {
            "date": "YYYY-MM-DD; required; must be in 2026",
            "race": "explicit race/stage page title",
            "athlete": "name matching roster rider or alias",
            "pos": "number 1-30",
            "src": "source label",
            "sourceUrl": "evidence URL",
            "sourceId": "source registry id"
        }
    }
    save_json(RESULTS_FILE, output)
    save_json(REVIEW_FILE, {
        "lastUpdated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": "Rows not published because they were missing a 2026 date, had bad race names, or failed matching.",
        "candidateCount": len(pcs_review) + len(mtb_review),
        "candidates": pcs_review + mtb_review
    })
    print(f"Existing results: {len(existing)}")
    print(f"Removed invalid/non-2026 rows: {removed}")
    print(f"PCS 2026 event-page candidates: {len(pcs_records)}")
    print(f"MTBData 2026 dated candidates: {len(mtb_records)}")
    print(f"Added this run: {len(additions)}")
    print(f"Total published 2026 results: {len(combined)}")

if __name__ == "__main__":
    main()
