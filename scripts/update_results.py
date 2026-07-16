#!/usr/bin/env python3
"""
PCS event-page results updater for Specialized Racing Dashboard.

Real PCS fix:
- Do NOT scrape PCS rider profile pages for Road results.
- Road results are collected only from explicit PCS race/stage/result pages.
- MTB/DH logic can still use MTBData profile pages because those examples have event-like race titles.

Expected repo files:
  data/roster.json
  data/results.json
  data/sources.json
  data/pcs_road_targets.json
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

MAX_POSITION = int(os.getenv("MAX_POSITION", "30"))
RESULT_YEAR = os.getenv("RESULT_YEAR", "2026")
REQUEST_TIMEOUT = 25
REQUEST_PAUSE_SECONDS = float(os.getenv("REQUEST_PAUSE_SECONDS", "0.5"))

HEADERS = {
    "User-Agent": "SpecializedRacingDashboard/4.0 (+GitHub Actions)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

DATE_RE = re.compile(r"\b(20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]20\d{2}|\d{1,2}\s+[A-Za-z]{3,9}\s+20\d{2}|\d{2}/\d{2}|\d{2}-\d{2})\b")
YEAR_ONLY_RE = re.compile(r"^20\d{2}$")
BAD_RACE_TERMS = [
    "general classification", "points classification", "mountains classification",
    "youth classification", "teams classification", "kom classification",
    "statistics", "ranking", "rankings", "pcs ranking", "uci ranking",
    "startlist", "profile", "history", "overview"
]


def norm(value: str) -> str:
    value = unicodedata.normalize("NFD", str(value or ""))
    value = "".join(ch for ch in value if unicodedata.category(ch) != "Mn")
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def load_json(path: Path, default):
    if not path.exists(): return default
    with path.open("r", encoding="utf-8") as f: return json.load(f)


def save_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")


def fetch(url: str) -> Optional[str]:
    try:
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200: return None
        return r.text
    except Exception:
        return None
    finally:
        if REQUEST_PAUSE_SECONDS: time.sleep(REQUEST_PAUSE_SECONDS)


def normalize_date(value: str) -> str:
    value = str(value or "").strip()
    if not value: return ""
    # PCS often gives mm/dd on winners/results pages. Use RESULT_YEAR.
    m = re.match(r"^(\d{2})/(\d{2})$", value)
    if m:
        mm, dd = int(m.group(1)), int(m.group(2))
        if 1 <= mm <= 12 and 1 <= dd <= 31:
            return f"{RESULT_YEAR}-{mm:02d}-{dd:02d}"
    m = re.match(r"^(\d{2})-(\d{2})$", value)
    if m:
        mm, dd = int(m.group(1)), int(m.group(2))
        if 1 <= mm <= 12 and 1 <= dd <= 31:
            return f"{RESULT_YEAR}-{mm:02d}-{dd:02d}"
    for fmt in ("%Y-%m-%d","%Y/%m/%d","%d/%m/%Y","%d-%m-%Y","%d %b %Y","%d %B %Y"):
        try: return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except Exception: pass
    return value if RESULT_YEAR in value else ""


def parse_pos(value: str) -> Optional[int]:
    value = str(value or "").strip()
    m = re.match(r"^(\d{1,2})(?:st|nd|rd|th)?$", value, re.I)
    if not m: return None
    p = int(m.group(1))
    return p if 1 <= p <= MAX_POSITION else None


def result_key(r: dict) -> str:
    return "|".join([str(r.get("date","")), norm(r.get("race","")), norm(r.get("athlete","")), str(r.get("pos",""))])


def roster_lookup(roster: List[dict]) -> Dict[str, dict]:
    out={}
    for a in roster:
        for name in [a.get("rider","")] + (a.get("aliases") or []):
            if name: out[norm(name)] = a
    return out


def get_page_title(soup: BeautifulSoup) -> str:
    h = soup.find(["h1","h2"])
    if h and h.get_text(strip=True): return h.get_text(" ", strip=True)
    if soup.title and soup.title.get_text(strip=True): return soup.title.get_text(" ", strip=True)
    return ""


def clean_race_name(text: str) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    text = re.sub(r"\s*-\s*ProCyclingStats\.com\s*$", "", text, flags=re.I)
    text = re.sub(r"\bOne day race results\b", "", text, flags=re.I).strip(" -–|")
    text = re.sub(r"\bStage \d+ results\b", lambda m: m.group(0), text, flags=re.I)
    return text


def bad_race_name(race: str) -> bool:
    r = norm(race)
    if not r: return True
    if YEAR_ONLY_RE.match(race.strip()): return True
    if len(race.strip()) < 5: return True
    return any(term in r for term in BAD_RACE_TERMS)


def discover_pcs_links(url: str, html: str) -> List[str]:
    """From a PCS race overview page, discover result/stage pages."""
    soup = BeautifulSoup(html, "lxml")
    links=[]
    for a in soup.find_all("a", href=True):
        href=a["href"]
        full=urljoin(url, href)
        if "procyclingstats.com/race/" not in full: continue
        lower=full.lower()
        if any(token in lower for token in ["/result", "/stage-", "/gc", "/results"]):
            links.append(full)
    # Also try canonical result pattern if overview URL supplied.
    if "/race/" in url and not any(x in url for x in ["/result", "/stage-"]):
        links.append(url.rstrip("/") + "/result/result/result")
        links.append(url.rstrip("/") + "/results")
    seen=set(); out=[]
    for l in links:
        if l not in seen:
            seen.add(l); out.append(l)
    return out[:60]


def rider_name_variants(name: str) -> List[str]:
    n = str(name or "").strip()
    parts = n.split()
    variants = {norm(n)}
    if len(parts) >= 2:
        variants.add(norm(" ".join(parts[::-1])))
        variants.add(norm(parts[-1] + " " + " ".join(parts[:-1])))
    return [v for v in variants if v]


def find_roster_match_from_cells(cells: List[str], lookup: Dict[str, dict]) -> Optional[Tuple[dict,str]]:
    joined = norm(" ".join(cells))
    # direct contains for full roster names and aliases
    for key, athlete in lookup.items():
        if len(key) > 3 and key in joined:
            return athlete, athlete.get("rider", "")
    return None


def parse_pcs_result_page(url: str, html: str, roster_index: Dict[str, dict]) -> Tuple[List[dict], List[dict]]:
    soup=BeautifulSoup(html, "lxml")
    page_title=clean_race_name(get_page_title(soup))
    text=soup.get_text("\n", strip=True)

    # Date from page text if available. Search around title metadata first.
    date=""
    dm=DATE_RE.search(text)
    if dm: date=normalize_date(dm.group(1))

    records=[]; review=[]
    for tr in soup.find_all("tr"):
        cells=[c.get_text(" ", strip=True) for c in tr.find_all(["td","th"])]
        if len(cells) < 4: continue
        # Must look like an actual result table row with rank in first few cells.
        pos=None
        for c in cells[:3]:
            pos=parse_pos(c)
            if pos: break
        if not pos: continue
        match=find_roster_match_from_cells(cells, roster_index)
        if not match: continue
        athlete, rider=match
        race=page_title
        # Try to enrich race with stage marker from breadcrumb/title/page text.
        if bad_race_name(race):
            review.append({"sourceUrl":url,"rawRow":" | ".join(cells),"reason":"bad_or_missing_race_name_from_pcs_result_page"})
            continue
        rec={
            "date": date,
            "race": race,
            "athlete": rider,
            "pos": pos,
            "src": "pcs_race_page",
            "sourceUrl": url,
            "sourceId": "pcs_race_page",
            "program": athlete.get("program", ""),
            "discipline": athlete.get("primaryDiscipline", "Road"),
            "team": athlete.get("team", "")
        }
        if not date: rec["dateStatus"]="missing_from_pcs_page_parse"
        records.append(rec)
    return records, review


def parse_mtbdata_profile_page(url: str, html: str, athlete: dict) -> Tuple[List[dict], List[dict]]:
    # Kept from earlier approach because MTBData profile examples identify real event names.
    soup=BeautifulSoup(html,"lxml")
    records=[]; review=[]
    for tr in soup.find_all("tr"):
        cells=[c.get_text(" ", strip=True) for c in tr.find_all(["td","th"])]
        if len(cells) < 2: continue
        pos=None
        for c in cells[:4]:
            pos=parse_pos(c)
            if pos: break
        if not pos: continue
        candidates=[c for c in cells if len(c)>8 and parse_pos(c) is None and not DATE_RE.search(c)]
        race=max(candidates, key=len) if candidates else ""
        if bad_race_name(race): continue
        date=""
        for c in cells:
            m=DATE_RE.search(c)
            if m:
                date=normalize_date(m.group(1)); break
        rec={"date":date,"race":race,"athlete":athlete.get("rider",""),"pos":pos,"src":"mtbdata_rider_page","sourceUrl":url,"sourceId":"mtbdata_rider_page","program":athlete.get("program",""),"discipline":athlete.get("primaryDiscipline",""),"team":athlete.get("team","")}
        if not date: rec["dateStatus"]="missing_from_source_parse"
        records.append(rec)
    return records, review


def build_mtbdata_url(a: dict) -> Optional[str]:
    disc=(a.get("primaryDiscipline") or "").lower()
    if disc not in {"mtb","dh"}: return None
    profile=a.get("profileUrl") or ""
    if "mtbdata.com/riders/" in profile: return profile
    rider_slug=norm(a.get("rider","")).replace(" ", "-")
    return f"https://mtbdata.com/riders/{rider_slug}" if rider_slug else None


def collect_pcs_event_results(roster: List[dict]) -> Tuple[List[dict], List[dict]]:
    targets=load_json(PCS_TARGETS_FILE,{"targets":[]}).get("targets",[])
    lookup=roster_lookup([a for a in roster if (a.get("primaryDiscipline") or "").lower()=="road"])
    records=[]; review=[]; urls=[]
    for t in targets:
        u = t.get("url") if isinstance(t, dict) else str(t)
        if u: urls.append(u)
    seen=set(); queue=[]
    for u in urls:
        if u not in seen:
            seen.add(u); queue.append(u)
    i=0
    while i < len(queue):
        url=queue[i]; i+=1
        html=fetch(url)
        if not html: continue
        # overview pages discover result pages. result pages parse directly.
        for l in discover_pcs_links(url, html):
            if l not in seen:
                seen.add(l); queue.append(l)
        if any(token in url.lower() for token in ["/result", "/stage-", "/results"]):
            recs, rev=parse_pcs_result_page(url, html, lookup)
            records.extend(recs); review.extend(rev)
    return records, review


def collect_mtbdata_results(roster: List[dict]) -> Tuple[List[dict], List[dict]]:
    records=[]; review=[]
    for a in roster:
        url=build_mtbdata_url(a)
        if not url: continue
        html=fetch(url)
        if not html: continue
        recs, rev=parse_mtbdata_profile_page(url, html, a)
        records.extend(recs); review.extend(rev)
    return records, review


def valid_record(r: dict) -> bool:
    try: pos=int(r.get("pos"))
    except Exception: return False
    return bool(r.get("race") and r.get("athlete") and 1 <= pos <= MAX_POSITION and not bad_race_name(r.get("race","")))


def main():
    roster_payload=load_json(ROSTER_FILE,{"athletes":[]})
    results_payload=load_json(RESULTS_FILE,{"results":[]})
    roster=roster_payload.get("athletes",[])
    existing=results_payload.get("results",[])

    # Drop previously polluted PCS profile records. Keep PCS race-page and non-PCS data.
    cleaned_existing=[]
    removed_polluted=0
    for r in existing:
        if r.get("src") in {"pcs_slug_guess", "profileUrl"} or r.get("sourceId") in {"pcs_slug_guess", "profileUrl"}:
            removed_polluted += 1
            continue
        if bad_race_name(r.get("race", "")):
            removed_polluted += 1
            continue
        cleaned_existing.append(r)

    keys={result_key(r) for r in cleaned_existing}
    pcs_records, pcs_review=collect_pcs_event_results(roster)
    mtb_records, mtb_review=collect_mtbdata_results(roster)
    candidates=pcs_records+mtb_records
    additions=[]
    for r in candidates:
        if not valid_record(r): continue
        k=result_key(r)
        if k not in keys:
            keys.add(k); additions.append(r)
    combined=cleaned_existing+additions
    combined.sort(key=lambda r:(str(r.get("date","")),str(r.get("race","")),str(r.get("athlete",""))))
    output={"lastUpdated":datetime.now(timezone.utc).date().isoformat(),"generatedBy":"scripts/update_results.py","strategy":"PCS race/stage/result pages only; no PCS rider-profile scraping","resultCount":len(combined),"addedThisRun":len(additions),"removedPollutedPcsProfileRows":removed_polluted,"results":combined,"schema":{"date":"YYYY-MM-DD, may be blank when dateStatus indicates a missing parse","race":"explicit race/stage page title","athlete":"name matching roster rider or alias","pos":"number 1-30","src":"source label","sourceUrl":"evidence URL","sourceId":"source registry id"}}
    save_json(RESULTS_FILE, output)
    save_json(REVIEW_FILE,{"lastUpdated":datetime.now(timezone.utc).isoformat(timespec="seconds"),"note":"Rows not published by PCS event-page or MTBData parsers.","candidateCount":len(pcs_review)+len(mtb_review),"candidates":pcs_review+mtb_review})
    print(f"Existing results: {len(existing)}")
    print(f"Removed polluted PCS profile rows: {removed_polluted}")
    print(f"PCS event-page candidates: {len(pcs_records)}")
    print(f"MTBData candidates: {len(mtb_records)}")
    print(f"Added this run: {len(additions)}")
    print(f"Total results: {len(combined)}")

if __name__ == "__main__":
    main()
