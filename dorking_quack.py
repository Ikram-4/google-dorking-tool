#!/usr/bin/env python3
"""
Dorking Quack - SAFE multi-threaded SerpAPI dork runner
FINAL VERSION (as requested by user)

Features:
 - DEFAULT monthly quota = 250
 - Script tracks usage automatically via quota_usage.json
 - Auto-resets usage at start of each month
 - You do NOT need to pass --used or --quota
 - No SerpAPI account API needed
 - Multi-thread safe
 - No duplicated API calls
 - Category-wise outputs + combined outputs
"""

import argparse
import os
import re
import requests
import sys
import threading
import time
import json
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import cycle
from typing import Dict, List, Set, Tuple

# ==================== CONFIG ====================
NUM_PER_PAGE = 100
DEFAULT_THREADS = 8
DEFAULT_PAGES = 1
DEFAULT_DELAY = 0.8
MONTHLY_QUOTA = 250    # <<< FIXED as requested
USAGE_FILE = "quota_usage.json"
RETRIES = 3
BACKOFF = 2.0


# ==================== SPINNER ====================
class Spinner:
    def __init__(self, text="Working"):
        self._spinner = cycle(["|", "/", "-", "\\"])
        self._running = False
        self._thread = None
        self.text = text

    def _run(self):
        while self._running:
            sys.stdout.write(f"\r{self.text} {next(self._spinner)}")
            sys.stdout.flush()
            time.sleep(0.12)
        sys.stdout.write("\r")
        sys.stdout.flush()

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join()


# ==================== LOAD & RESET USAGE ====================
def load_usage() -> int:
    """Load saved usage from file and reset if month changed."""
    if not os.path.exists(USAGE_FILE):
        return 0

    try:
        with open(USAGE_FILE, "r") as f:
            data = json.load(f)
    except:
        return 0

    saved_month = data.get("month")
    current_month = datetime.now().strftime("%Y-%m")

    # RESET usage at month change
    if saved_month != current_month:
        return 0

    return data.get("used", 0)


def save_usage(used: int):
    current_month = datetime.now().strftime("%Y-%m")
    data = {"month": current_month, "used": used}

    with open(USAGE_FILE, "w") as f:
        json.dump(data, f)


# ==================== HELPERS ====================
def load_categorized_dorks(path: str) -> Dict[str, List[str]]:
    categories = {}
    current = None
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            l = line.strip()
            if not l or l.startswith("#"):
                continue
            m = re.match(r"^\[(.+?)\]$", l)
            if m:
                current = m.group(1)
                categories.setdefault(current, [])
                continue
            if current is None:
                current = "Uncategorized"
                categories.setdefault(current, [])
            categories[current].append(l)
    return categories


def ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)


def serpapi_search(query: str, api_key: str, start: int):
    params = {
        "engine": "google",
        "q": query,
        "num": str(NUM_PER_PAGE),
        "start": str(start),
        "api_key": api_key
    }
    r = requests.get("https://serpapi.com/search", params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def extract_urls(data: dict) -> Set[str]:
    urls = set()
    for r in data.get("organic_results", []):
        link = r.get("link")
        if link:
            urls.add(link)
    return urls


def sanitize(dork: str, domain: str) -> str:
    return dork.replace("example.com", domain).replace("example[.]com", domain)


# ==================== WORKER ====================
def process_dork(category, dork, domain, api_key, pages, delay):
    q = sanitize(dork, domain)
    found = set()
    for p in range(pages):
        start = p * NUM_PER_PAGE
        attempt = 0
        while attempt <= RETRIES:
            try:
                data = serpapi_search(q, api_key, start)
                found.update(extract_urls(data))
                break
            except Exception as e:
                attempt += 1
                if attempt > RETRIES:
                    print(f"\n[!] Failed: {q}  (page={p})  {e}")
                else:
                    time.sleep(BACKOFF ** attempt)
        time.sleep(delay)
    return category, dork, found


# ==================== MAIN ====================
def main():
    banner = r"""
  ____            _      _             
 |  _ \ _ __ ___ | | ___| |_ _ __ ___  
 | | | | '__/ _ \| |/ _ \ __| '__/ _ \ 
 | |_| | | | (_) | |  __/ |_| | | (_) |
 |____/|_|  \___/|_|\___|\__|_|  \___/ 
        D O R K I N G   Q U A C K 🦆
"""
    print(banner)

    parser = argparse.ArgumentParser()
    parser.add_argument("--domain", required=True)
    parser.add_argument("--dorks", required=True)
    parser.add_argument("--apikey", required=True)
    parser.add_argument("--pages", type=int, default=2)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--delay", type=float, default=0.8)
    parser.add_argument("--csv", action="store_true")
    args = parser.parse_args()

    # Load and reset usage
    used_before = load_usage()
    quota = MONTHLY_QUOTA

    # Load dorks
    categories = load_categorized_dorks(args.dorks)
    tasks = [(c, d) for c in categories for d in categories[c]]
    total_dorks = len(tasks)

    # Calculate credits needed
    credits_needed = total_dorks * args.pages
    after = used_before + credits_needed
    percent = (after / quota) * 100

    print(f"[i] Monthly Quota : {quota}")
    print(f"[i] Used Before   : {used_before}")
    print(f"[i] This Run Uses : {credits_needed}")
    print(f"[i] After Run     : {after} ({percent:.1f}%)")

    # Confirm
    y = input("Proceed? (Y/n): ").lower().strip()
    if y not in ("", "y", "yes"):
        print("Aborted.")
        return

    # Fix threads if > dorks
    threads = min(args.threads, total_dorks)

    # Output dirs
    ensure_dir("output")
    for c in categories:
        ensure_dir(f"output/{c}")

    lock = threading.Lock()
    all_urls = set()
    spinner = Spinner("Searching")
    spinner.start()

    futures = []
    with ThreadPoolExecutor(max_workers=threads) as ex:
        for (c, d) in tasks:
            futures.append(ex.submit(process_dork, c, d, args.domain, args.apikey, args.pages, args.delay))

        done = 0
        for f in as_completed(futures):
            done += 1
            category, dork_text, urls = f.result()

            # Write category txt
            out = f"output/{category}/urls.txt"
            old = set()
            if os.path.exists(out):
                old = {x.strip() for x in open(out).read().splitlines()}
            new = sorted(set(urls) - old)
            with open(out, "a") as fw:
                for u in new:
                    fw.write(u + "\n")

            # CSV
            if args.csv:
                with open(f"output/{category}/results.csv", "a") as fw:
                    for u in new:
                        safe = dork_text.replace('"', "'")
                        fw.write(f'"{category}","{safe}","{u}"\n')

            # Combined
            with lock:
                all_urls.update(urls)
                with open("output/all_urls.txt", "w") as fw:
                    for u in sorted(all_urls):
                        fw.write(u + "\n")

            print(f"\r[{done}/{total_dorks}] {category} → {len(urls)} URLs", end="")

    spinner.stop()
    print("\n\n[✓] Done!")

    # Save new usage
    save_usage(after)
    print(f"[✓] Updated usage saved: {after}/{quota} ({percent:.1f}%)")


if __name__ == "__main__":
    main()
