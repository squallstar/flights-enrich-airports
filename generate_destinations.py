#!/usr/bin/env python3
"""
Generates a dataset of popular travel destination airports per country.
Uses the enriched airports data + OpenAI to pick the best recommendations.

For each country (by ISO 2-letter code), produces:
  - domestic: top popular airports within the same country
  - international: top popular international destination airports

Designed for IP-based personalization: resolve user's country from IP,
then show 10 destination suggestions (mix of domestic + international).

Self-resumable: skips countries already in the output file.

Usage:
    python generate_destinations.py [--dry-run] [--concurrency 5]
"""

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from openai import AsyncOpenAI, RateLimitError

ENV_FILE = Path(__file__).parent / ".env"
ENRICHED_FILE = Path(__file__).parent / "airports_enriched.json"
OUTPUT_FILE = Path(__file__).parent / "destinations.json"
MODEL = "gpt-4.1-mini"

SYSTEM_PROMPT = """You are a travel industry expert. I will give you a country code and a list of airports in that country (and globally). Your job is to select the most popular travel destination airports for users located in that country.

Return a JSON object with this structure:
{
  "<COUNTRY_CODE>": {
    "domestic": [
      {"iata": "XXX", "reason": "short reason this is a top domestic destination"}
    ],
    "international": [
      {"iata": "XXX", "reason": "short reason this is a top international destination"}
    ]
  }
}

Rules:
1. "domestic": Pick the top popular DESTINATION airports within the same country that travelers from this country would fly to (leisure, business, tourism). Pick up to 10, but fewer if the country has few meaningful destinations. For very small countries/islands with only 1-2 airports, include just those.
2. "international": Pick the top 10-15 most popular international destination airports that travelers FROM this country typically fly to. Consider:
   - Geographic proximity (neighboring countries, regional hubs)
   - Tourism popularity (beach destinations, cultural capitals, shopping hubs)
   - Business travel corridors
   - Diaspora/cultural connections
   - Budget airline route popularity
3. Sort both lists by popularity (most popular first).
4. Use ONLY IATA codes from the provided airport list — do not invent codes.
5. "reason" should be 3-8 words explaining why (e.g., "top beach destination", "major business hub", "cultural capital of Europe").
6. For the domestic list, DO NOT include airports that are primarily origin/hub airports with no tourist appeal (e.g., don't recommend ORD for US travelers just because it's a hub — but DO include it if Chicago is genuinely a popular destination).

Think about what a travel agent or OTA like Skyscanner would recommend as "Popular destinations" when a user from this country opens their homepage."""


def load_json(path: Path) -> dict:
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_json(data: dict, path: Path):
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(path)


def build_airport_summary(airports: dict) -> dict:
    """Slim version of airport data for the prompt."""
    summary = {}
    for icao, v in airports.items():
        iata = v.get("iata", "")
        if not iata:
            continue
        summary[iata] = {
            "name": v.get("display_name") or v.get("name", ""),
            "city": v.get("city", ""),
            "country": v.get("country", ""),
        }
        if v.get("weight"):
            summary[iata]["weight"] = v["weight"]
    return summary


def get_top_global_airports(airports: dict, n: int = 300) -> dict:
    """Get the top N airports globally by weight, plus all with weight >= 4."""
    weighted = []
    for icao, v in airports.items():
        if v.get("weight") and v.get("iata"):
            weighted.append((v["weight"], v["iata"], v))

    weighted.sort(key=lambda x: -x[0])

    result = {}
    for w, iata, v in weighted[:n]:
        result[iata] = {
            "name": v.get("display_name") or v.get("name", ""),
            "city": v.get("city", ""),
            "country": v.get("country", ""),
            "weight": w,
        }
    return result


async def generate_for_country(
    client: AsyncOpenAI,
    country_code: str,
    domestic_airports: dict,
    global_airports: dict,
    retries: int = 3,
) -> dict:
    domestic_slim = {}
    for iata, info in domestic_airports.items():
        domestic_slim[iata] = {
            "name": info.get("name", ""),
            "city": info.get("city", ""),
        }
        if info.get("weight"):
            domestic_slim[iata]["weight"] = info["weight"]

    prompt = json.dumps({
        "country": country_code,
        "domestic_airports": domestic_slim,
        "global_top_airports": global_airports,
    }, ensure_ascii=False)

    for attempt in range(retries):
        try:
            resp = await client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0.3,
            )
            result = json.loads(resp.choices[0].message.content)
            if country_code in result:
                return result[country_code]
            first_key = next(iter(result), None)
            if first_key and isinstance(result[first_key], dict):
                return result[first_key]
            return result
        except RateLimitError:
            wait = 2 ** (attempt + 1)
            print(f"    Rate limited on {country_code}, waiting {wait}s...")
            await asyncio.sleep(wait)
        except Exception as e:
            print(f"    Error on {country_code}: {e}, retrying ({attempt + 1}/{retries})...")
            await asyncio.sleep(2 ** attempt)

    return {}


async def main():
    parser = argparse.ArgumentParser(description="Generate destination recommendations per country")
    parser.add_argument("--concurrency", type=int, default=5, help="Max concurrent API calls")
    parser.add_argument("--dry-run", action="store_true", help="Process only 3 countries")
    args = parser.parse_args()

    if not os.environ.get("OPENAI_API_KEY") and ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                os.environ[key.strip()] = val.strip().strip("'\"")

    if not os.environ.get("OPENAI_API_KEY"):
        print("Error: set OPENAI_API_KEY via env var or .env file")
        sys.exit(1)

    client = AsyncOpenAI()

    enriched = load_json(ENRICHED_FILE)
    if not enriched:
        print(f"Error: run enrich_airports.py first to create {ENRICHED_FILE}")
        sys.exit(1)

    destinations = load_json(OUTPUT_FILE)

    by_country: dict[str, dict[str, dict]] = {}
    for icao, v in enriched.items():
        cc = v.get("country", "")
        if not cc or not v.get("iata"):
            continue
        by_country.setdefault(cc, {})[v["iata"]] = {
            "name": v.get("display_name") or v.get("name", ""),
            "city": v.get("city", ""),
            "country": cc,
            "weight": v.get("weight"),
        }

    global_airports = get_top_global_airports(enriched)

    to_process = [cc for cc in sorted(by_country.keys()) if cc not in destinations]

    total = len(by_country)
    done = len(destinations)
    remaining = len(to_process)

    if remaining == 0:
        print(f"All {total} countries already processed in {OUTPUT_FILE}")
        return

    print(f"Countries: {total} total, {done} done, {remaining} remaining")
    print(f"Global airport pool: {len(global_airports)} top airports")
    print(f"Config: concurrency={args.concurrency}, model={MODEL}")

    if args.dry_run:
        to_process = to_process[:3]
        print(f"Dry run: processing only {len(to_process)} countries: {to_process}")

    semaphore = asyncio.Semaphore(args.concurrency)
    processed = 0
    failed = 0
    start_time = time.time()

    async def process_country(cc: str):
        nonlocal processed, failed
        async with semaphore:
            result = await generate_for_country(
                client, cc, by_country[cc], global_airports
            )
            if result and ("domestic" in result or "international" in result):
                destinations[cc] = result
                processed += 1
            else:
                failed += 1

            elapsed = time.time() - start_time
            rate = (processed + failed) / elapsed if elapsed > 0 else 0
            print(f"  [{processed + failed}/{remaining}] {cc} — "
                  f"domestic: {len(result.get('domestic', []))}, "
                  f"intl: {len(result.get('international', []))}"
                  f" [{rate:.1f}/s]")

            save_json(destinations, OUTPUT_FILE)

    tasks = [process_country(cc) for cc in to_process]
    await asyncio.gather(*tasks)

    save_json(destinations, OUTPUT_FILE)

    elapsed = time.time() - start_time
    print(f"\nDone in {elapsed:.1f}s")
    print(f"  Processed: {processed}")
    print(f"  Failed: {failed}")
    print(f"  Total in output: {len(destinations)}")
    print(f"  Output: {OUTPUT_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
