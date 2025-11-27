#!/usr/bin/env python3
"""
Dorking Quack - SAFE multi-threaded SerpAPI dork runner (updated)

Changes:
 - DEFAULT_QUOTA set to 250
 - --realtime flag: after each completed dork the script fetches SerpAPI account usage and prints up-to-date quota/used info
 - --poll-interval option (seconds) to limit how often realtime polling hits account endpoint; default = 0 (poll every completed dork)
 - Auto-quota behavior unchanged: if --auto-quota and initial fetch fails, the script ABORTS (Option A).

Dependencies:
  pip install requests
"""
import argparse
import os
import re
import requests
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import cycle
from typing import Dict, List, Set, Tuple

# ---------- CONFIG ----------
NUM_PER_PAGE = 100         # SerpAPI 'num' param (max per call)
DEFAULT_THREADS = 8
DEFAULT_PAGES = 1
DEFAULT_DELAY = 0.8
DEFAULT_QUOTA = 250       # <--- changed default quota to 250
RETRIES = 3
BACKOFF = 2.0             # seconds multiplier

# ---------- ASCII spinner ----------
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
        self._thread = None

# ---------- helpers ----------
def load_categorized_dorks(path: str) -> Dict[str, List[str]]:
    """Parse a dorks file with [Category] headers."""
    categories: Dict[str, List[str]] = {}
    current = None
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r"^\[(.+?)\]\s*$", line)
            if m:
                current = m.group(1).strip()
                categories.setdefault(current, [])
                continue
            if current is None:
                current = "Uncategorized"
                categories.setdefault(current, [])
            categories[current].append(line)
    return categories

def ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)

def serpapi_search(query: str, api_key: str, start: int = 0, timeout: int = 30) -> dict:
    params = {
        "engine": "google",
        "q": query,
        "num": str(NUM_PER_PAGE),
        "start": str(start),
        "api_key": api_key
    }
    resp = requests.get("https://serpapi.com/search", params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()

def extract_urls_from_serp(json_data: dict) -> Set[str]:
    urls = set()
    for r in json_data.get("organic_results", []):
        link = r.get("link")
        if link:
            urls.add(link)
    # additional blocks that sometimes contain links
    for block in ("inline_links", "top_stories", "related_questions"):
        for item in json_data.get(block, []):
            link = item.get("link")
            if link:
                urls.add(link)
    return urls

def sanitize_and_replace(dork: str, domain: str) -> str:
    return dork.replace("example[.]com", domain).replace("example.com", domain)

def fetch_serpapi_account(api_key: str, timeout: int = 15) -> Tuple[int, int]:
    """
    Call SerpAPI account endpoint and return (quota_monthly, used_searches)
    Raises exception on failure (per Option A for initial auto-quota).
    """
    url = "https://serpapi.com/account"
    resp = requests.get(url, params={"api_key": api_key}, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    quota = data.get("plan_searches_monthly")
    used = data.get("searches_performed")
    if quota is None and "plan" in data and isinstance(data["plan"], dict):
        quota = data["plan"].get("searches_monthly") or data["plan"].get("searches")
    if used is None:
        used = data.get("searches_used") or data.get("searches_performed_this_month")
    if quota is None or used is None:
        raise ValueError("Unexpected account response format; missing quota/used fields.")
    return int(quota), int(used)

# ---------- worker ----------
def process_dork(category: str, dork: str, domain: str, api_key: str, pages: int, delay: float) -> Tuple[str, str, Set[str]]:
    """
    Fetch 'pages' pages for a single dork sequentially.
    Returns (category, dork, set(urls))
    """
    clean = sanitize_and_replace(dork, domain)
    found: Set[str] = set()
    for p in range(pages):
        start = p * NUM_PER_PAGE
        attempt = 0
        while attempt <= RETRIES:
            try:
                data = serpapi_search(clean, api_key, start=start)
                urls = extract_urls_from_serp(data)
                found.update(urls)
                break
            except Exception as e:
                attempt += 1
                if attempt > RETRIES:
                    print(f"\n[!] Failed search for dork (category={category}): {clean} (start={start}) -> {e}")
                else:
                    backoff = BACKOFF ** attempt
                    print(f"\n[!] Error, retrying in {backoff:.1f}s (attempt {attempt}/{RETRIES}) for: {clean} start={start}")
                    time.sleep(backoff)
        time.sleep(delay)
    return category, dork, found

# ---------- main ----------
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
    parser = argparse.ArgumentParser(description="Dorking Quack - SAFE multi-threaded SerpAPI dork runner")
    parser.add_argument("--domain", required=True, help="Target domain (e.g. target.com)")
    parser.add_argument("--dorks", required=True, help="Path to categorized dorks file (use [Category] headers)")
    parser.add_argument("--apikey", required=True, help="SerpAPI API key")
    parser.add_argument("--output", default="output", help="Output base folder (default ./output)")
    parser.add_argument("--threads", type=int, default=DEFAULT_THREADS, help=f"Parallel workers (default {DEFAULT_THREADS})")
    parser.add_argument("--pages", type=int, default=DEFAULT_PAGES, help=f"Pages per dork (default {DEFAULT_PAGES})")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY, help=f"Delay (s) between requests in a worker (default {DEFAULT_DELAY})")
    parser.add_argument("--quota", type=int, default=DEFAULT_QUOTA, help=f"Total monthly quota (default {DEFAULT_QUOTA}) -- ignored when --auto-quota used")
    parser.add_argument("--used", type=int, default=0, help="Already-used credits this month (default 0) -- ignored when --auto-quota used")
    parser.add_argument("--max-credits", type=int, default=0, help="Optional: hard cap for this run (0 = no cap).")
    parser.add_argument("--csv", action="store_true", help="Also write per-category CSV with columns category,dork,url")
    parser.add_argument("--auto-quota", action="store_true", help="Automatically detect SerpAPI account quota and used credits. If detection fails, script WILL ABORT.")
    parser.add_argument("--realtime", action="store_true", help="Enable realtime account updates: fetch account usage after each completed dork.")
    parser.add_argument("--poll-interval", type=float, default=0.0, help="If >0, realtime polling will wait this many seconds between account polls (default 0 = poll after every dork).")
    args = parser.parse_args()

    domain = args.domain.strip()
    dorks_path = args.dorks
    api_key = args.apikey.strip()
    out_base = args.output
    pages = max(1, args.pages)
    delay = max(0.0, float(args.delay))
    max_credits = args.max_credits

    # Load dorks
    try:
        categories = load_categorized_dorks(dorks_path)
    except Exception as e:
        print(f"[!] Failed to load dorks file: {e}")
        return

    # Flatten to list of (category, dork)
    tasks: List[Tuple[str, str]] = []
    for cat, dlist in categories.items():
        for d in dlist:
            tasks.append((cat, d))

    if not tasks:
        print("[!] No dorks to run (file empty or parsing failed).")
        return

    total_dorks = len(tasks)

    # Auto-quota: fetch actual quota & used from SerpAPI
    if args.auto_quota:
        print("[*] Auto-detecting SerpAPI account quota & usage...")
        try:
            quota, used = fetch_serpapi_account(api_key)
            print(f"[i] Detected quota: {quota} searches/month, already used: {used}")
        except Exception as e:
            print(f"[!] Auto-quota failed and per your choice the run will abort. Reason: {e}")
            return
    else:
        quota = max(1, args.quota)
        used = max(0, args.used)

    # estimate credits: dorks * pages
    credits_needed = total_dorks * pages
    projected_used = used + credits_needed
    percent_after = (projected_used / quota) * 100.0

    print(f"[+] Dorks loaded: {total_dorks} (across {len(categories)} categories)")
    print(f"[+] Pages per dork: {pages}")
    print(f"[+] Threads requested: {args.threads}")
    print(f"[+] Credits needed this run: {credits_needed}")
    print(f"[+] Quota: {quota}  Already used: {used}  Projected after run: {projected_used} ({percent_after:.1f}%)")
    if max_credits:
        print(f"[+] Hard cap for this run (--max-credits): {max_credits}")
        if credits_needed > max_credits:
            print("[!] Credits needed exceed the --max-credits value. Aborting.")
            return

    # Confirmation prompt (Option D - always ask + show percentage)
    answer = input("Proceed? (Y/n): ").strip().lower()
    if answer not in ("", "y", "yes"):
        print("Aborted by user.")
        return

    # Adjust threads: don't run more threads than dorks
    threads = max(1, min(args.threads, total_dorks))
    if threads != args.threads:
        print(f"[i] Adjusted worker count to {threads} (cannot exceed number of dorks)")

    # Prepare output folders
    ensure_dir(out_base)
    for cat in categories:
        ensure_dir(os.path.join(out_base, cat))

    # Combined sets
    all_urls_global: Set[str] = set()
    lock = threading.Lock()

    spinner = Spinner("Searching")
    spinner.start()

    futures = []
    with ThreadPoolExecutor(max_workers=threads) as executor:
        for cat, dork in tasks:
            futures.append(executor.submit(process_dork, cat, dork, domain, api_key, pages, delay))

        completed = 0
        total = len(futures)
        last_poll_time = 0.0
        for fut in as_completed(futures):
            completed += 1
            try:
                cat, dork_text, urls = fut.result()
            except Exception as e:
                print(f"\n[!] Worker failed: {e}")
                continue

            # Save per-category urls (append new, dedupe existing)
            cat_folder = os.path.join(out_base, cat)
            ensure_dir(cat_folder)
            cat_file = os.path.join(cat_folder, "urls.txt")
            existing = set()
            if os.path.exists(cat_file):
                try:
                    with open(cat_file, "r", encoding="utf-8") as fh:
                        for line in fh:
                            existing.add(line.strip())
                except Exception:
                    existing = set()
            new_urls = sorted(u for u in urls if u not in existing)

            if new_urls:
                with open(cat_file, "a", encoding="utf-8") as fh:
                    for u in new_urls:
                        fh.write(u + "\n")

            # Optional CSV
            if args.csv:
                csv_file = os.path.join(cat_folder, "results.csv")
                with open(csv_file, "a", encoding="utf-8") as fh:
                    for u in new_urls:
                        safe_dork = dork_text.replace('"', "'")
                        fh.write(f'"{cat}","{safe_dork}","{u}"\n')

            # Update global combined list (thread-safe)
            with lock:
                all_urls_global.update(urls)
                combined_file = os.path.join(out_base, "all_urls.txt")
                with open(combined_file, "w", encoding="utf-8") as cf:
                    for u in sorted(all_urls_global):
                        cf.write(u + "\n")

            # Realtime polling of account (if requested)
            if args.realtime:
                now = time.time()
                # enforce poll interval if provided
                if args.poll_interval > 0:
                    if now - last_poll_time >= args.poll_interval:
                        try:
                            q, u = fetch_serpapi_account(api_key)
                            last_poll_time = now
                            print(f"\n[i] Realtime account: used {u}/{q} ({(u/q)*100:.1f}%)")
                        except Exception as e:
                            print(f"\n[!] Realtime poll failed (non-fatal): {e}")
                    else:
                        # skip poll due to poll interval
                        pass
                else:
                    # poll after every dork
                    try:
                        q, u = fetch_serpapi_account(api_key)
                        last_poll_time = now
                        print(f"\n[i] Realtime account: used {u}/{q} ({(u/q)*100:.1f}%)")
                    except Exception as e:
                        print(f"\n[!] Realtime poll failed (non-fatal): {e}")

            # Progress line
            print(f"\r[ {completed}/{total} ] Category:{cat} Dork:`{dork_text[:70]}` → {len(urls)} urls", end="", flush=True)

    spinner.stop()
    print("\n\n[✓] Run complete.")
    print(f"[✓] Total unique URLs found: {len(all_urls_global)}")
    print(f"[✓] Output folder: {os.path.abspath(out_base)}")
    after_used = used + credits_needed
    print(f"[i] Credits used this run: {credits_needed}. Projected monthly used: {after_used}/{quota} ({(after_used/quota)*100:.1f}%)")
    print("🦆 Quack! Happy hunting.")
    return

if __name__ == "__main__":
    main()
