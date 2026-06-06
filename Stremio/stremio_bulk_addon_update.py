#!/usr/bin/env python3
"""
Stremio Bulk Addon Updater
--------------------------
Pulls your full Stremio addon collection via the Stremio API,
replaces an old API key (e.g. Real-Debrid) with a new one across
every addon manifest URL, then pushes the updated collection back.

Usage (interactive):
    python3 stremio_bulk_addon_update.py

Usage (flags):
    python3 stremio_bulk_addon_update.py \
        --email you@example.com \
        --old-key OLD_RD_API_KEY \
        --new-key NEW_RD_API_KEY \
        [--auth-key YOUR_STREMIO_AUTH_KEY]  # skip login if you already have it
        [--dry-run]                          # preview changes, don't push
        [--backup backup.json]               # save original collection to file

Getting your Stremio auth key without your password:
    1. Open https://app.strem.io in a browser and log in.
    2. Open DevTools → Console and run:
           JSON.parse(localStorage.getItem("profile")).auth.key
    3. Copy the key and pass it with --auth-key.
"""

import argparse
import json
import sys
import urllib.request
import urllib.error
import getpass
import re
from pathlib import Path

API_BASE = "https://api.strem.io"


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
        print(f"[ERROR] HTTP {e.code} from {url}: {body}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"[ERROR] Network error reaching {url}: {e.reason}", file=sys.stderr)
        sys.exit(1)


def login(email: str, password: str) -> str:
    print(f"[*] Logging in as {email} ...")
    resp = api_post("/api/login", {"type": "Login", "email": email, "password": password})
    if resp.get("error"):
        print(f"[ERROR] Login failed: {resp['error']}", file=sys.stderr)
        sys.exit(1)
    auth_key = resp.get("authKey") or (resp.get("result", {}) or {}).get("authKey")
    if not auth_key:
        # Some API versions nest differently
        auth_key = resp.get("result", {}).get("user", {}).get("authKey")
    if not auth_key:
        print(f"[ERROR] Could not extract authKey from response:\n{json.dumps(resp, indent=2)}", file=sys.stderr)
        sys.exit(1)
    print(f"[+] Authenticated. Auth key starts with: {auth_key[:8]}...")
    return auth_key


def get_addon_collection(auth_key: str) -> list:
    print("[*] Fetching addon collection ...")
    resp = api_post("/api/addonCollectionGet", {
        "type": "AddonCollectionGet",
        "authKey": auth_key,
        "update": True,
    })
    if resp.get("error"):
        print(f"[ERROR] addonCollectionGet failed: {resp['error']}", file=sys.stderr)
        sys.exit(1)
    addons = (
        resp.get("result")
        or resp.get("addons")
        or resp.get("result", {}).get("addons")
    )
    if addons is None:
        # Try nested result structure
        result = resp.get("result", {})
        if isinstance(result, dict):
            addons = result.get("addons", [])
        else:
            addons = result
    if not isinstance(addons, list):
        print(f"[ERROR] Unexpected addon collection format:\n{json.dumps(resp, indent=2)}", file=sys.stderr)
        sys.exit(1)
    print(f"[+] Found {len(addons)} installed addon(s).")
    return addons


def set_addon_collection(auth_key: str, addons: list) -> None:
    print(f"[*] Pushing updated collection ({len(addons)} addons) ...")
    resp = api_post("/api/addonCollectionSet", {
        "type": "AddonCollectionSet",
        "authKey": auth_key,
        "addons": addons,
    })
    if resp.get("error"):
        print(f"[ERROR] addonCollectionSet failed: {resp['error']}", file=sys.stderr)
        sys.exit(1)
    print("[+] Addon collection updated successfully.")


def replace_key_in_url(url: str, old_key: str, new_key: str) -> str:
    """Replace old_key with new_key in a URL string, case-insensitively for the key portion."""
    # Direct substring replacement (handles most addon URL formats)
    if old_key in url:
        return url.replace(old_key, new_key)
    # Case-insensitive fallback
    pattern = re.compile(re.escape(old_key), re.IGNORECASE)
    return pattern.sub(new_key, url)


def patch_addons(addons: list, old_key: str, new_key: str) -> tuple[list, int]:
    """
    Walk every addon entry and replace old_key with new_key in:
      - transportUrl  (the manifest URL, where RD keys live)
      - manifest.behaviorHints.configurationRequired (sometimes stores config)
      Any other string field that contains the key.
    Returns (patched_addons, change_count).
    """
    changed = 0

    def patch_value(v):
        nonlocal changed
        if isinstance(v, str) and old_key in v:
            changed += 1
            return replace_key_in_url(v, old_key, new_key)
        return v

    def patch_obj(obj):
        if isinstance(obj, dict):
            return {k: patch_obj(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [patch_obj(item) for item in obj]
        return patch_value(obj)

    patched = patch_obj(addons)
    return patched, changed


def build_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Bulk-update an API key across all Stremio addon URLs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--email", help="Stremio account email")
    p.add_argument("--auth-key", dest="auth_key", help="Stremio auth key (skips login)")
    p.add_argument("--old-key", dest="old_key", help="API key to replace (e.g. old Real-Debrid key)")
    p.add_argument("--new-key", dest="new_key", help="New API key to substitute in")
    p.add_argument("--dry-run", action="store_true", help="Show what would change without pushing")
    p.add_argument("--backup", metavar="FILE", help="Save original collection JSON to this file before patching")
    p.add_argument("--show-addons", action="store_true", help="Print all addon transport URLs after fetching")
    return p.parse_args()


def prompt_if_missing(label: str, current, secret: bool = False) -> str:
    if current:
        return current
    if secret:
        return getpass.getpass(f"{label}: ")
    return input(f"{label}: ").strip()


def main():
    args = build_args()

    # ------------------------------------------------------------------
    # 1. Collect credentials
    # ------------------------------------------------------------------
    auth_key = args.auth_key
    if not auth_key:
        email = prompt_if_missing("Stremio email", args.email)
        password = getpass.getpass("Stremio password: ")
        auth_key = login(email, password)

    # ------------------------------------------------------------------
    # 2. Pull addon collection
    # ------------------------------------------------------------------
    addons = get_addon_collection(auth_key)

    if args.show_addons:
        print("\n--- Current addon transport URLs ---")
        for i, addon in enumerate(addons, 1):
            url = addon.get("transportUrl", "(no transportUrl)")
            name = addon.get("manifest", {}).get("name", "")
            print(f"  {i:3}. [{name}] {url}")
        print()

    # ------------------------------------------------------------------
    # 3. Collect keys to replace
    # ------------------------------------------------------------------
    old_key = prompt_if_missing("Old API key to replace", args.old_key, secret=False)
    new_key = prompt_if_missing("New API key to insert", args.new_key, secret=False)

    if old_key == new_key:
        print("[!] Old and new keys are identical — nothing to do.")
        sys.exit(0)

    # ------------------------------------------------------------------
    # 4. Optional backup
    # ------------------------------------------------------------------
    if args.backup:
        backup_path = Path(args.backup)
        backup_path.write_text(json.dumps(addons, indent=2))
        print(f"[+] Original collection backed up to: {backup_path}")

    # ------------------------------------------------------------------
    # 5. Patch
    # ------------------------------------------------------------------
    patched_addons, change_count = patch_addons(addons, old_key, new_key)

    if change_count == 0:
        print(f"[!] The key '{old_key[:8]}...' was not found in any addon URL.")
        print("    Tip: use --show-addons to inspect your addon URLs.")
        sys.exit(0)

    # Show affected addons
    print(f"\n[+] Found {change_count} occurrence(s) of the old key across addon entries:")
    for orig, patched in zip(addons, patched_addons):
        orig_url = orig.get("transportUrl", "")
        new_url = patched.get("transportUrl", "")
        if orig_url != new_url:
            name = orig.get("manifest", {}).get("name", "unknown")
            print(f"    [{name}]")
            print(f"      OLD: {orig_url}")
            print(f"      NEW: {new_url}")

    # ------------------------------------------------------------------
    # 6. Push (unless dry run)
    # ------------------------------------------------------------------
    if args.dry_run:
        print("\n[DRY RUN] No changes pushed. Remove --dry-run to apply.")
        sys.exit(0)

    confirm = input(f"\nPush {change_count} change(s) to your Stremio account? [y/N] ").strip().lower()
    if confirm != "y":
        print("[!] Aborted. No changes pushed.")
        sys.exit(0)

    set_addon_collection(auth_key, patched_addons)
    print(f"\n[✓] Done. {change_count} addon URL(s) updated with the new API key.")
    print("    Open Stremio and your addons will reflect the new key after the next sync.")


if __name__ == "__main__":
    main()
