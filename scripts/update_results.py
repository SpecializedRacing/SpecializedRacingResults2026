#!/usr/bin/env python3
"""
Strict 2026 updater for the GitHub dashboard.

Patch based on the working Claude dashboard logic:
- Keep only 2026 results.
- Require real YYYY-MM-DD dates.
- Require roster match / alias match.
- Require position 1-30.
- Avoid PCS rider-profile scraping entirely.
- Keep seeded verified results and append only strict future results.

This script is intentionally conservative. It prevents polluted rows like:
  race = "2009"
  race = "General classification General classification"
  date = ""
"""
from __future__ import annotations

import json, re, unicodedata
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data'
ROSTER_FILE = DATA / 'roster.json'
RESULTS_FILE = DATA / 'results.json'
REVIEW_FILE = DATA / 'discovered_results_review.json'
RESULT_YEAR = '2026'
MAX_POSITION = 30
BAD_RACE_TERMS = [
    'general classification','points classification','mountains classification','youth classification',
    'teams classification','kom classification','statistics','ranking','rankings','pcs ranking','uci ranking',
    'startlist','profile','history','overview','team ranking'
]

def norm(v: str) -> str:
    v = unicodedata.normalize('NFD', str(v or ''))
    v = ''.join(ch for ch in v if unicodedata.category(ch) != 'Mn')
    v = v.lower()
    v = re.sub(r'[^a-z0-9]+', ' ', v)
    return re.sub(r'\s+', ' ', v).strip()

def load(path, default):
    if not path.exists(): return default
    with path.open('r', encoding='utf-8') as f: return json.load(f)

def save(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write('\n')

def is_2026_date(d):
    return bool(re.match(r'^2026-\d{2}-\d{2}$', str(d or '')))

def bad_race_name(race):
    race = str(race or '').strip()
    r = norm(race)
    if not r or len(race) < 5: return True
    if re.match(r'^20\d{2}$', race): return True
    return any(term in r for term in BAD_RACE_TERMS)

def roster_index(roster_payload):
    out = {}
    for a in roster_payload.get('athletes', []):
        for name in [a.get('rider','')] + (a.get('aliases') or []):
            k = norm(name)
            if k: out[k] = a
    return out

def result_key(r):
    return '|'.join([str(r.get('date','')), norm(r.get('race','')), norm(r.get('athlete','')), str(r.get('pos',''))])

def valid_result(r, roster_names):
    try:
        pos = int(r.get('pos'))
    except Exception:
        return False
    if pos < 1 or pos > MAX_POSITION: return False
    if not is_2026_date(r.get('date')): return False
    if bad_race_name(r.get('race')): return False
    if norm(r.get('athlete')) not in roster_names: return False
    return True

def main():
    roster = load(ROSTER_FILE, {'athletes': []})
    results = load(RESULTS_FILE, {'results': []})
    idx = roster_index(roster)
    roster_names = set(idx.keys())

    existing = results.get('results', [])
    clean = []
    rejected = []
    seen = set()
    for r in existing:
        if valid_result(r, roster_names):
            # normalize source fields while preserving the record
            r.setdefault('sourceUrl','')
            r.setdefault('sourceId', r.get('src',''))
            k = result_key(r)
            if k not in seen:
                seen.add(k); clean.append(r)
        else:
            rejected.append(r)

    # Future place for strict parsing from PCS race pages / MTBData pages.
    # We intentionally do NOT append anything from rider profile tables here.
    output = {
        'lastUpdated': datetime.now(timezone.utc).date().isoformat(),
        'generatedBy': 'scripts/update_results.py',
        'strategy': 'Strict 2026 only; preserves verified seed; no PCS rider-profile scraping',
        'resultCount': len(clean),
        'addedThisRun': 0,
        'rejectedInvalidRows': len(rejected),
        'results': sorted(clean, key=lambda x: (x.get('date',''), x.get('race',''), x.get('athlete',''))),
        'schema': {
            'date':'YYYY-MM-DD; required; 2026 only',
            'race':'valid race name, not classification/year/statistic',
            'athlete':'name matching roster rider or alias',
            'pos':'number 1-30',
            'src':'source label',
            'sourceUrl':'optional evidence URL',
            'sourceId':'source identifier'
        }
    }
    save(RESULTS_FILE, output)
    save(REVIEW_FILE, {
        'lastUpdated': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'note': 'Rows rejected by strict 2026 validation. These should not be shown on the dashboard.',
        'candidateCount': len(rejected),
        'candidates': rejected[:500]
    })
    print(f"Published strict 2026 results: {len(clean)}")
    print(f"Rejected invalid/non-2026/bad rows: {len(rejected)}")

if __name__ == '__main__':
    main()
