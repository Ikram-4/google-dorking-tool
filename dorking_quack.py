#!/usr/bin/env python3
"""
Dorking Quack - SAFE multi-threaded SerpAPI dork runner
MULTI-DOMAIN VERSION
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

NUM_PER_PAGE = 100
DEFAULT_THREADS = 8
DEFAULT_PAGES = 1
DEFAULT_DELAY = 0.8
MONTHLY_QUOTA = 250
USAGE_FILE = "quota_usage.json"
RETRIES = 3
BACKOFF = 2.0

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


def load_usage() -> int:
    if not os.path.exists(USAGE_FILE):
        return 0
    try:
        with open(USAGE_FILE, "r") as f:
            data = json.load(f)
    except:
        return 0

    if data.get("month") != datetime.now().strftime("%Y-%m"):
        return 0
    return data.get("used", 0)


def save_usage(used: int):
    data = {"month": datetime.now().strftime("%Y-%m"), "used": used}
    with open(USAGE_FILE, "w") as f:
        json.dump(data, f)


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


def process_dork(domain, category, dork, api_key, pages, delay):
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
            except:
                attempt += 1
                if attempt > RETRIES:
                    pass
                else:
                    time.sleep(BACKOFF ** attempt)
        time.sleep(delay)
    return domain, category, dork, found


def main():
    print("Dorking Quack 🦆 (Multi-domain)\n")

    parser = argparse.ArgumentParser(
        description="Multi-domain Google dork scanner using SerpAPI.",
        formatter_class=argparse.RawTextHelpFormatter
    )

    parser.add_argument("--domains", required=True,
                        help="Comma-separated list of target domains")
    parser.add_argument("--dorks", required=True, help="Path to categorized dorks file")
    parser.add_argument("--apikey", required=True)
    parser.add_argument("--pages", type=int, default=2)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--delay", type=float, default=0.8)
    parser.add_argument("--csv", action="store_true")

    args = parser.parse_args()

    domains = [x.strip() for x in args.domains.split(",") if x.strip()]

    categories = load_categorized_dorks(args.dorks)

    tasks = []
    for domain in domains:
        for c in categories:
            for d in categories[c]:
                tasks.append((domain, c, d))

    total_dorks = len(tasks)

    y = input(f"Run scan for {len(domains)} domains? (Y/n): ").lower()
    if y not in ("", "y", "yes"):
        print("Aborted.")
        return

    spinner = Spinner("Scanning")
    spinner.start()

    ensure_dir("output")

    lock = threading.Lock()
    domain_urls = {d: set() for d in domains}

    futures = []
    with ThreadPoolExecutor(max_workers=args.threads) as ex:
        for (domain, c, dork_text) in tasks:
            futures.append(ex.submit(
                process_dork, domain, c, dork_text,
                args.apikey, args.pages, args.delay
            ))

        done = 0
        for f in as_completed(futures):
            done += 1
            domain, category, dork_text, urls = f.result()

            outdir = f"output/{domain}/{category}"
            ensure_dir(outdir)

            out_file = f"{outdir}/urls.txt"
            old = set()
            if os.path.exists(out_file):
                old = {x.strip() for x in open(out_file).read().splitlines()}

            new = sorted(set(urls) - old)
            with open(out_file, "a") as fw:
                fw.writelines([u + "\n" for u in new])

            if args.csv:
                with open(f"{outdir}/results.csv", "a") as fw:
                    for u in new:
                        safe = dork_text.replace('"', "'")
                        fw.write(f'"{category}","{safe}","{u}"\n')

            with lock:
                domain_urls[domain].update(urls)

            print(f"\rProcessed {done}/{total_dorks}", end="")

    spinner.stop()

    print("\n\n[✓] Finished.")

    for domain in domains:
        print(f"[{domain}] URLs found: {len(domain_urls[domain])}")


if __name__ == "__main__":
    main()
