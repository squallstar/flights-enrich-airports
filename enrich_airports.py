#!/usr/bin/env python3
"""
Enriches airports.json using OpenAI to generate:
  - display_name: clean UI name like "Milan Bergamo (BGY)"
  - search_keywords: terms users would search for, sorted by relevancy
  - nearest_major_city: closest well-known city with distance in km

Self-resumable: reads existing output file and skips already-processed airports.

Usage:
    # Option 1: environment variable
    export OPENAI_API_KEY="sk-..."
    python enrich_airports.py [--batch-size 20] [--concurrency 5] [--dry-run]

    # Option 2: .env file in same directory
    echo 'OPENAI_API_KEY=sk-...' > .env
    python enrich_airports.py
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
INPUT_FILE = Path(__file__).parent / "airports.json"
OUTPUT_FILE = Path(__file__).parent / "airports_enriched.json"
MODEL = "gpt-5.4-mini-2026-03-17"

SYSTEM_PROMPT = """You are an aviation and geography expert. For each airport I provide, return a JSON object keyed by ICAO code with these fields:

1. "display_name" (string): A user-friendly name for an OTA website like Skyscanner or Google Flights.
   Rules:
   - Format: "<Primary City> [<Distinguishing Name>] (<IATA>)"
   - If the city name IS the airport's common identity, just use city + IATA: "Bologna (BLQ)"
   - If the airport serves a major city but is named differently, lead with the major city: "Milan Bergamo (BGY)", "London Gatwick (LGW)", "Tokyo Narita (NRT)"
   - If the airport has a well-known short name, use it: "New York JFK (JFK)", "Los Angeles LAX (LAX)", "Paris CDG (CDG)"
   - For small/regional airports, use: "<City> (<IATA>)"
   - Never include "Airport" or "International" in the display name

2. "search_keywords" (array of strings): Additional terms a traveler might type when searching for this airport, sorted from most likely to least likely.

   CRITICAL — You MUST include the city/airport name in the LOCAL LANGUAGE(S) of the country, plus major world languages. This is essential for international travelers searching in their own language.

   Required categories (in priority order):
   a) LOCAL LANGUAGE names: the city and airport name in the country's official/local language(s) using native script
      - Examples: "北京" for Beijing, "東京" for Tokyo, "Москва" for Moscow, "مومباي" for Mumbai, "서울" for Seoul, "Αθήνα" for Athens, "القاهرة" for Cairo
   b) MAJOR WORLD LANGUAGE translations of the city name — include whichever are commonly used and DIFFERENT from the English name:
      - Chinese (Simplified): 巴黎, 伦敦, 纽约, 柏林, 罗马
      - Japanese: パリ, ロンドン, ニューヨーク, ベルリン
      - Korean: 파리, 런던, 뉴욕, 베를린
      - Arabic: باريس, لندن, نيويورك, برلين
      - Russian: Париж, Лондон, Нью-Йорк, Берлин
      - Spanish/Portuguese/French/German/Italian — only when the name differs from English (e.g., "Moscú", "Londres", "Pékin", "Mailand", "Londra")
      - Hindi/Thai/other regional scripts when relevant to the airport's region
   c) The major city/metro area the airport serves (if different from the primary city)
   d) Alternative English spellings, romanizations, and transliterations (e.g., "Peking" for Beijing, "Bombay" for Mumbai, "Canton" for Guangzhou)
   e) Well-known neighborhoods, districts, or regions
   f) Common nicknames or abbreviations travelers use
   g) Nearby well-known cities or tourist destinations this airport serves
   h) Historical or former airport names

   Do NOT include: the IATA code, the ICAO code, the exact city name from the input data, or generic words like "airport"/"international".
   For small/obscure airports where translations don't meaningfully exist, include at minimum the country name in 2-3 major languages.

3. "nearest_major_city" (object): The nearest well-known/major city travelers would recognize.
   - "name" (string): city name in English
   - "distance_km" (integer): approximate straight-line distance from the airport to the city center
   - If the airport is IN a major city, distance can be 0 or the distance to city center

Return ONLY valid JSON. Keys must be the exact ICAO codes provided."""


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


def build_prompt_entry(data: dict) -> dict:
    return {
        "iata": data.get("iata", ""),
        "name": data.get("name", ""),
        "city": data.get("city", ""),
        "state": data.get("state", ""),
        "country": data.get("country", ""),
        "lat": round(data.get("lat", 0), 4),
        "lon": round(data.get("lon", 0), 4),
    }


async def enrich_batch(
    client: AsyncOpenAI,
    batch: dict[str, dict],
    retries: int = 3,
) -> dict:
    prompt_data = {icao: build_prompt_entry(v) for icao, v in batch.items()}

    for attempt in range(retries):
        try:
            resp = await client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(prompt_data, ensure_ascii=False)},
                ],
                response_format={"type": "json_object"},
                temperature=0.2,
            )
            result = json.loads(resp.choices[0].message.content)
            return result
        except RateLimitError:
            wait = 2 ** (attempt + 1)
            print(f"    Rate limited, waiting {wait}s...")
            await asyncio.sleep(wait)
        except json.JSONDecodeError as e:
            print(f"    JSON parse error: {e}, retrying...")
            await asyncio.sleep(1)
        except Exception as e:
            print(f"    API error: {e}, retrying ({attempt + 1}/{retries})...")
            await asyncio.sleep(2 ** attempt)

    return {}


def merge_enrichment(original: dict, enrichment: dict) -> dict:
    merged = dict(original)
    for field in ("display_name", "search_keywords", "nearest_major_city"):
        if field in enrichment:
            merged[field] = enrichment[field]
    return merged


async def main():
    parser = argparse.ArgumentParser(description="Enrich airports with OpenAI")
    parser.add_argument("--batch-size", type=int, default=20, help="Airports per API call")
    parser.add_argument("--concurrency", type=int, default=5, help="Max concurrent API calls")
    parser.add_argument("--dry-run", action="store_true", help="Process only 2 batches")
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

    airports = load_json(INPUT_FILE)
    if not airports:
        print(f"Error: {INPUT_FILE} not found or empty")
        sys.exit(1)

    enriched = load_json(OUTPUT_FILE)

    to_process = {k: v for k, v in airports.items() if k not in enriched}

    total = len(airports)
    done = len(enriched)
    remaining = len(to_process)

    if remaining == 0:
        print(f"All {total} airports already enriched in {OUTPUT_FILE}")
        return

    print(f"Airports: {total} total, {done} done, {remaining} remaining")
    print(f"Config: batch_size={args.batch_size}, concurrency={args.concurrency}, model={MODEL}")

    items = list(to_process.items())
    batches = [dict(items[i : i + args.batch_size]) for i in range(0, len(items), args.batch_size)]

    if args.dry_run:
        batches = batches[:2]
        print(f"Dry run: processing only {len(batches)} batches ({sum(len(b) for b in batches)} airports)")

    semaphore = asyncio.Semaphore(args.concurrency)
    processed = 0
    failed = 0
    start_time = time.time()

    async def process_batch(batch: dict, batch_idx: int):
        nonlocal processed, failed
        async with semaphore:
            result = await enrich_batch(client, batch)

            matched = 0
            for icao in batch:
                if icao in result:
                    enriched[icao] = merge_enrichment(airports[icao], result[icao])
                    matched += 1
                else:
                    icao_lower = {k.lower(): k for k in result}
                    if icao.lower() in icao_lower:
                        enriched[icao] = merge_enrichment(
                            airports[icao], result[icao_lower[icao.lower()]]
                        )
                        matched += 1

            processed += matched
            failed += len(batch) - matched

            elapsed = time.time() - start_time
            rate = processed / elapsed if elapsed > 0 else 0
            eta = (remaining - processed) / rate if rate > 0 else 0

            print(
                f"  [{processed}/{remaining}] Batch {batch_idx + 1}/{len(batches)} "
                f"({matched}/{len(batch)} matched) "
                f"[{rate:.1f} airports/s, ETA {eta / 60:.1f}min]"
            )

            save_json(enriched, OUTPUT_FILE)

    tasks = [process_batch(batch, i) for i, batch in enumerate(batches)]
    await asyncio.gather(*tasks)

    save_json(enriched, OUTPUT_FILE)

    elapsed = time.time() - start_time
    print(f"\nDone in {elapsed:.1f}s")
    print(f"  Enriched: {processed}")
    print(f"  Failed: {failed}")
    print(f"  Total in output: {len(enriched)}")
    print(f"  Output: {OUTPUT_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
