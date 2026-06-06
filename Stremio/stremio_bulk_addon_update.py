#!/usr/bin/env python3
"""
Stremio Bulk Addon Updater
--------------------------
Logs into your Stremio account, pulls every installed addon, automatically
finds the old Real-Debrid (or other) API key embedded in those URLs, and
replaces it with your new key — all in one shot.

What you need to run this:
  1. Your STREMIO email and password (same as the Stremio app login)
  2. Your NEW Real-Debrid API key (get it from https://real-debrid.com/apitoken)
  That's it. The script finds the old key for you.

Quickstart (interactive — easiest):
    python3 stremio_bulk_addon_update.py

With flags:
    python3 stremio_bulk_addon_update.py \
        --email you@example.com \
        --new-key YOUR_NEW_RD_API_KEY \
        --backup before_update.json \
        --dry-run

NOTE: This uses your STREMIO login, NOT your Real-Debrid login.
      Real-Debrid username/password are NOT needed here.
"""

import argparse
import json
import sys
import urllib.request
import urllib.error
import getpass
import re
from pathlib import Path
from collections import Counter

API_BASE = "https://api.strem.io"

# Patterns that signal a Real-Debrid API key is nearby in a URL.
# RD keys are typically 52-character alphanumeric strings.
RD_PARAM_PATTERNS = [
    re.compile(r'(?:realdebrid|RealDebrid)[%3D=]+([A-Za-z0-9_\-]{20,})', re.IGNORECASE),
    re.compile(r'\bRD[%3D=]+([A-Za-z0-9_\-]{20,})', re.IGNORECASE),
    re.compile(r'debrid[^&/]*[%3D=]+(?:realdebrid)[^&/]*apikey[%3D=]+([A-Za-z0-9_\-]{20,})', re.IGNORECASE),
    re.compile(r'apikey[%3D=]+([A-Za-z0-9_\-]{20,})', re.IGNORECASE),
    re.compile(r'token[%3D=]+([A-Za-z0-9_\-]{20,})', re.IGNORECASE),
]


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def api_post(path: str, payload: dict) -> dict:
    url = f"{API_BASE}{path}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "stremio-bulk-updater/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"\n[ERROR] HTTP {e.code} calling {url}:\n  {body}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"\n[ERROR] Network error reaching {url}: {e.reason}", file=sys.stderr)
        sys.exit(1)


def stremio_login(email: str, password: str) -> str:
    print(f"\n[*] Logging in to Stremio as {email} ...")
    resp = api_post("/api/login", {"type": "Login", "email": email, "password": password})
    if resp.get("error"):
        print(f"\n[ERROR] Login failed: {resp['error']}", file=sys.stderr)
        print("  → Double-check your STREMIO email and password (not Real-Debrid).", file=sys.stderr)
        sys.exit(1)
    # Auth key may live at different depths depending on API version
    auth_key = (
        resp.get("authKey")
        or resp.get("result", {}).get("authKey")
        or resp.get("result", {}).get("user", {}).get("authKey")
    )
    if not auth_key:
        print(f"\n[ERROR] Login succeeded but could not find authKey in response.", file=sys.stderr)
        print(f"  Full response: {json.dumps(resp, indent=2)}", file=sys.stderr)
        sys.exit(1)
    print(f"[+] Stremio login OK.")
    return auth_key


def get_addon_collection(auth_key: str) -> list:
    print("[*] Downloading your addon collection ...")
    resp = api_post("/api/addonCollectionGet", {
        "type": "AddonCollectionGet",
        "authKey": auth_key,
        "update": True,
    })
    if resp.get("error"):
        print(f"\n[ERROR] Could not fetch addons: {resp['error']}", file=sys.stderr)
        sys.exit(1)
    addons = None
    result = resp.get("result")
    if isinstance(result, list):
        addons = result
    elif isinstance(result, dict):
        addons = result.get("addons")
    if addons is None:
        addons = resp.get("addons")
    if not isinstance(addons, list):
        print(f"\n[ERROR] Unexpected response format:\n{json.dumps(resp, indent=2)}", file=sys.stderr)
        sys.exit(1)
    print(f"[+] Found {len(addons)} installed addon(s).")
    return addons


def push_addon_collection(auth_key: str, addons: list) -> None:
    print(f"[*] Pushing updated collection ({len(addons)} addons) to Stremio ...")
    resp = api_post("/api/addonCollectionSet", {
        "type": "AddonCollectionSet",
        "authKey": auth_key,
        "addons": addons,
    })
    if resp.get("error"):
        print(f"\n[ERROR] Push failed: {resp['error']}", file=sys.stderr)
        sys.exit(1)
    print("[+] Collection saved successfully.")


# ---------------------------------------------------------------------------
# Key auto-detection
# ---------------------------------------------------------------------------

def extract_all_strings(obj) -> list[str]:
    """Recursively collect every string value from a nested dict/list."""
    out = []
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            out.extend(extract_all_strings(v))
    elif isinstance(obj, list):
        for item in obj:
            out.extend(extract_all_strings(item))
    return out


def find_rd_keys_in_collection(addons: list) -> list[str]:
    """
    Scan every string in the addon collection for Real-Debrid API key patterns.
    Returns a deduplicated list of candidate keys, most-common first.
    """
    found: Counter = Counter()
    all_strings = extract_all_strings(addons)
    for s in all_strings:
        for pattern in RD_PARAM_PATTERNS:
            for match in pattern.finditer(s):
                key = match.group(1)
                # Skip very short hits and obviously non-key values
                if len(key) >= 20:
                    found[key] += 1
    return [k for k, _ in found.most_common()]


def mask(key: str) -> str:
    """Show first 6 and last 4 chars, mask the middle."""
    if len(key) <= 10:
        return key[:3] + "***"
    return key[:6] + "***" + key[-4:]


# ---------------------------------------------------------------------------
# Patching
# ---------------------------------------------------------------------------

def patch_collection(addons: list, old_key: str, new_key: str) -> tuple[list, int]:
    """
    Replace old_key with new_key everywhere in the addon collection.
    Returns (patched_collection, number_of_replacements).
    """
    changed = 0

    def patch_val(v):
        nonlocal changed
        if isinstance(v, str) and old_key in v:
            changed += 1
            return v.replace(old_key, new_key)
        return v

    def patch_obj(obj):
        if isinstance(obj, dict):
            return {k: patch_obj(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [patch_obj(item) for item in obj]
        return patch_val(obj)

    return patch_obj(addons), changed


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Bulk-replace a Real-Debrid (or other) API key across all Stremio addons.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--email",    help="Your Stremio account email")
    p.add_argument("--new-key",  dest="new_key", help="Your NEW Real-Debrid API key")
    p.add_argument("--old-key",  dest="old_key", help="Old key to replace (auto-detected if omitted)")
    p.add_argument("--dry-run",  action="store_true", help="Preview changes without pushing anything")
    p.add_argument("--backup",   metavar="FILE", help="Save original collection to FILE before changing")
    p.add_argument("--list-addons", action="store_true", help="Print all addon URLs and exit")
    return p.parse_args()


def main():
    args = build_args()

    print("=" * 60)
    print("  Stremio Bulk Addon Updater")
    print("=" * 60)
    print()
    print("You need: your STREMIO email + password, and your NEW Real-Debrid API key.")
    print("(Not your Real-Debrid password — just the API key from real-debrid.com/apitoken)")
    print()

    # 1. Stremio login
    email = args.email or input("Stremio email: ").strip()
    password = getpass.getpass("Stremio password: ")
    auth_key = stremio_login(email, password)

    # 2. Pull collection
    addons = get_addon_collection(auth_key)

    # 3. Optional: just list everything and exit
    if args.list_addons:
        print("\n--- All addon transport URLs ---")
        for i, addon in enumerate(addons, 1):
            name = addon.get("manifest", {}).get("name", "?")
            url  = addon.get("transportUrl", "(none)")
            print(f"  {i:3}. [{name}]\n       {url}")
        sys.exit(0)

    # 4. Determine old key
    old_key = args.old_key
    if not old_key:
        print("\n[*] Scanning addon URLs for existing Real-Debrid API keys ...")
        candidates = find_rd_keys_in_collection(addons)

        if not candidates:
            print("\n[!] No Real-Debrid API key pattern found in your addon URLs.")
            print("    Your addons may not have RD keys embedded, or they use an unusual format.")
            print("    Run with --list-addons to inspect all URLs manually.")
            print("    You can also specify --old-key YOURKEY to force a search.")
            sys.exit(0)

        if len(candidates) == 1:
            old_key = candidates[0]
            print(f"[+] Found 1 key in use: {mask(old_key)}")
        else:
            print(f"[+] Found {len(candidates)} distinct key(s) in your addon URLs:")
            for i, k in enumerate(candidates, 1):
                count_addons = sum(
                    1 for a in addons
                    if k in str(a.get("transportUrl", ""))
                )
                print(f"  [{i}] {mask(k)}  (in {count_addons} addon URL(s))")
            choice = input("\nWhich key should be replaced? Enter number: ").strip()
            try:
                old_key = candidates[int(choice) - 1]
            except (ValueError, IndexError):
                print("[ERROR] Invalid choice.", file=sys.stderr)
                sys.exit(1)

    # 5. New key
    new_key = args.new_key
    if not new_key:
        print("\nPaste your NEW Real-Debrid API key below.")
        print("(Find it at: https://real-debrid.com/apitoken)")
        new_key = input("New Real-Debrid API key: ").strip()

    if not new_key:
        print("[ERROR] New key cannot be empty.", file=sys.stderr)
        sys.exit(1)

    if old_key == new_key:
        print("\n[!] Old and new keys are the same — nothing to update.")
        sys.exit(0)

    # 6. Backup
    if args.backup:
        Path(args.backup).write_text(json.dumps(addons, indent=2))
        print(f"\n[+] Original collection backed up to: {args.backup}")

    # 7. Patch
    patched, change_count = patch_collection(addons, old_key, new_key)

    if change_count == 0:
        print(f"\n[!] Key {mask(old_key)} was not found anywhere in your addon collection.")
        print("    Run with --list-addons to see what's there.")
        sys.exit(0)

    # Show diff
    affected_names = []
    print(f"\n[+] {change_count} change(s) across these addons:")
    for orig, new in zip(addons, patched):
        orig_url = orig.get("transportUrl", "")
        new_url  = new.get("transportUrl", "")
        if orig_url != new_url:
            name = orig.get("manifest", {}).get("name", "unknown")
            affected_names.append(name)
            print(f"    • {name}")

    # 8. Dry run check
    if args.dry_run:
        print("\n[DRY RUN] No changes were pushed. Remove --dry-run to apply for real.")
        sys.exit(0)

    # 9. Confirm and push
    print()
    confirm = input(f"Update {len(affected_names)} addon(s) on your Stremio account? [y/N] ").strip().lower()
    if confirm != "y":
        print("[!] Cancelled. Nothing was changed.")
        sys.exit(0)

    push_addon_collection(auth_key, patched)

    print()
    print("=" * 60)
    print(f"  Done! {len(affected_names)} addon(s) updated.")
    print("  Restart Stremio or wait for it to sync.")
    print("=" * 60)


if __name__ == "__main__":
    main()
