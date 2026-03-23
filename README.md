# Airport Data Enrichment & Destination Recommendations

Two scripts that use OpenAI to enrich airport data for OTA search UIs and generate personalized destination suggestions.

## Setup

```bash
pip install -r requirements.txt
```

Set your OpenAI API key (either way works):

```bash
# Option A: environment variable
export OPENAI_API_KEY="sk-..."

# Option B: .env file
echo 'OPENAI_API_KEY=sk-...' > .env
```

---

## 1. Airport Enrichment (`enrich_airports.py`)

Enriches all 7,911 airports in `airports.json` with display names, multilingual search keywords, and nearest city info.

### What it generates

| Field | Description | Example (BGY) |
|---|---|---|
| `display_name` | Clean UI label | `"Milan Bergamo (BGY)"` |
| `search_keywords` | Multilingual search terms, sorted by relevancy | `["Milano", "ミラノ", "米兰", "Orio al Serio", ...]` |
| `nearest_major_city` | Closest well-known city + distance | `{"name": "Milan", "distance_km": 45}` |

Keywords include the city name in local language(s), Chinese, Japanese, Korean, Arabic, Russian, and European languages where the name differs from English.

### Sample output (`airports_enriched.json`)

```json
{
  "LIME": {
    "icao": "LIME",
    "iata": "BGY",
    "name": "Il Caravaggio International Airport",
    "city": "Orio al Serio",
    "state": "Lombardy",
    "country": "IT",
    "elevation": 782,
    "lat": 45.6739006042,
    "lon": 9.7041702271,
    "tz": "Europe/Rome",
    "keywords": ["Bergamo", "Orio al Serio International Airport", ...],
    "weight": 6,
    "display_name": "Milan Bergamo (BGY)",
    "search_keywords": ["Milano", "ミラノ", "米兰", "밀라노", "Orio al Serio", ...],
    "nearest_major_city": {
      "name": "Milan",
      "distance_km": 45
    }
  }
}
```

### Usage

```bash
# Dry run — processes 2 batches (40 airports) to verify everything works
python enrich_airports.py --dry-run

# Full run with defaults (batch_size=20, concurrency=5)
python enrich_airports.py

# Tune for speed (higher concurrency) or cost (larger batches)
python enrich_airports.py --batch-size 25 --concurrency 8
```

| Flag | Default | Description |
|---|---|---|
| `--batch-size` | 20 | Airports sent per API call |
| `--concurrency` | 5 | Max parallel API calls |
| `--dry-run` | off | Only process 2 batches |

### How to use the data for search

The enriched data supports Skyscanner-style typeahead search:

1. **Direct match** — user types "Bolog" → match against `city`, `name`, `iata`, `display_name`, `keywords`, and `search_keywords` → show **Bologna (BLQ)**
2. **Nearby match** — for airports where "Bologna" appears in their `search_keywords`, or compute distance between Bologna's coordinates and other airports using `lat`/`lon` → show **Florence (FLR) — 77km from Bologna**
3. **Ranking** — use the `weight` field (when present, values 2–10) to boost major airports in results

---

## 2. Destination Recommendations (`generate_destinations.py`)

Generates a dataset of popular travel destinations per country (236 countries). Designed for IP-based personalization: resolve a user's country from their IP, then show a curated list of suggested destinations.

### What it generates

For each country code, a list of popular **domestic** and **international** destination airports:

| Field | Description |
|---|---|
| `domestic` | Up to 10 popular airports within the same country |
| `international` | 10–15 popular international destinations from that country |

Each entry includes an IATA code and a short reason (e.g., "top beach destination", "major business hub").

### Sample output (`destinations.json`)

```json
{
  "AE": {
    "domestic": [
      {"iata": "DXB", "reason": "major tourist and business hub"},
      {"iata": "AUH", "reason": "capital city with cultural sites"},
      {"iata": "SHJ", "reason": "popular for shopping and culture"}
    ],
    "international": [
      {"iata": "LHR", "reason": "major UK gateway, business and leisure"},
      {"iata": "CDG", "reason": "Paris cultural and shopping hub"},
      {"iata": "IST", "reason": "Istanbul cultural and business hub"},
      {"iata": "DEL", "reason": "strong business and diaspora links"}
    ]
  }
}
```

### Usage

```bash
# Dry run — processes 3 countries
python generate_destinations.py --dry-run

# Full run (236 countries, ~236 API calls)
python generate_destinations.py

# Tune concurrency
python generate_destinations.py --concurrency 8
```

| Flag | Default | Description |
|---|---|---|
| `--concurrency` | 5 | Max parallel API calls |
| `--dry-run` | off | Only process 3 countries |

### How to use the data for IP-based suggestions

1. Resolve the user's country from their IP address (e.g., using MaxMind GeoIP)
2. Look up their country code in `destinations.json`
3. Pick ~5 domestic + ~5 international destinations (or adjust the ratio)
4. Cross-reference each IATA code with `airports_enriched.json` for display names, coordinates, etc.

---

## 3. Test UI (`index.html`)

A single-file React app to test the enriched data with a Skyscanner-style autocomplete and destination recommendations.

### Running locally

```bash
npx serve .
```

Then open [http://localhost:3000](http://localhost:3000).

### Features

- **Fuzzy search** across city, airport name, IATA code, and all keywords (including multilingual)
- **Tier-based ranking** — exact matches always beat prefix matches, which always beat fuzzy matches; weight breaks ties within the same tier
- **Popular destinations** — shown below the search bar when the input is empty, personalized to the user's country (detected from browser timezone)
- Clicking a destination card populates the search box

### How country detection works

The UI detects the user's country from `Intl.DateTimeFormat().resolvedOptions().timeZone` (e.g., `America/New_York` → US, `Europe/Rome` → IT). This works without any API calls or IP lookups. In production, you'd replace this with IP-based geolocation for better accuracy.

---

## Resumability

Both scripts are **self-resuming**. They read their output file on startup and skip entries already present. Progress is saved after every batch via atomic writes (write to `.tmp`, then rename).

If a script crashes or you stop it, just run it again — it picks up where it left off.

## Files

| File | Description |
|---|---|
| `airports.json` | Source data (7,911 airports, read-only) |
| `airports_enriched.json` | Enriched airport data (generated) |
| `destinations.json` | Popular destinations per country (generated) |
| `enrich_airports.py` | Airport enrichment script |
| `generate_destinations.py` | Destination recommendations script |
| `index.html` | Test UI with search + destinations |
| `vercel.json` | Vercel deployment config |
| `.env` | OpenAI API key (not committed) |

## Cost estimates

| Script | Model | API calls | Estimated cost |
|---|---|---|---|
| `enrich_airports.py` | gpt-4o-mini | ~396 | $3–5 |
| `generate_destinations.py` | gpt-4.1-mini | ~236 | $0.50–1 |
