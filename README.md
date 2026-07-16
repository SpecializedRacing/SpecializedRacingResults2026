# 2026 Racing Overview — Sports Marketing


## Automated source registry

Source definitions live in:

```text
data/sources.json
```

Registered source families now include:

- Road: ProCyclingStats, UCI
- MTB / DH: MTBData, WHOOP UCI Mountain Bike World Series, MTB ProCyclingStats, UCI
- Gravel: Gravel Earth Series, Life Time Grand Prix
- Triathlon: World Triathlon, PTO / T100, IRONMAN

MTBData has been added as a first-priority MTB/DH source.

## Automated update workflow

The repository now includes:

```text
.github/workflows/update-results.yml
scripts/update_results.py
requirements.txt
```

The workflow runs on Mondays and can also be triggered manually from the GitHub Actions tab.

Important: the workflow and scraper framework are wired in. Each source adapter is isolated in `scripts/update_results.py` so individual source parsers can be maintained safely as site structures change.
