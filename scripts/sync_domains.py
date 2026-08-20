#!/usr/bin/env python3
"""Sync PDDL domains from the planning.domains classical collection.

Source: https://api.planning.domains/ (JSON API for the classical collection).
Each run imports a bounded batch of not-yet-imported domains so the repo grows
gradually and diffs stay reviewable. State lives in catalog/state.json; every
imported domain records its provenance in a metadata.json next to the files.

The script is idempotent: run it twice and the second run either imports the
next batch or exits 0 with "nothing new to import".

Usage:
    python3 scripts/sync_domains.py [--batch N] [--max-problems N] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

API_ROOT = "https://api.planning.domains/json/classical"
REPO_ROOT = Path(__file__).resolve().parent.parent
DOMAINS_DIR = REPO_ROOT / "domains"
STATE_FILE = REPO_ROOT / "catalog" / "state.json"
CATALOG_FILE = REPO_ROOT / "CATALOG.md"

DEFAULT_BATCH = 5
DEFAULT_MAX_PROBLEMS = 20
TIMEOUT = 30
RETRIES = 3
RETRY_WAIT = 5  # seconds, doubled per attempt

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def log(msg: str) -> None:
    print(msg, flush=True)


def get(url: str) -> requests.Response:
    """GET with timeout and simple exponential-backoff retries."""
    last_exc: Exception | None = None
    for attempt in range(RETRIES):
        try:
            resp = requests.get(url, timeout=TIMEOUT)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:  # includes timeouts
            last_exc = exc
            wait = RETRY_WAIT * (2**attempt)
            log(f"  retry {attempt + 1}/{RETRIES} for {url} in {wait}s ({exc})")
            time.sleep(wait)
    raise RuntimeError(f"failed after {RETRIES} attempts: {url}") from last_exc


def get_json(url: str) -> dict | list:
    """GET a JSON payload. The API embeds raw control characters in some
    description strings, which strict JSON parsers reject; strip them first."""
    text = _CONTROL_CHARS.sub(" ", get(url).text)
    payload = json.loads(text, strict=False)
    if isinstance(payload, dict) and payload.get("error"):
        raise RuntimeError(f"API error for {url}: {payload}")
    return payload["result"] if isinstance(payload, dict) else payload


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "unnamed"


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"source": API_ROOT, "imported": {}}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def fetch_collections() -> dict[int, str]:
    """Map domain_id -> collection name (first collection that lists it)."""
    mapping: dict[int, str] = {}
    for coll in get_json(f"{API_ROOT}/collections"):
        name = coll.get("collection_name") or f"collection-{coll['collection_id']}"
        for domain_id in json.loads(coll.get("domain_set") or "[]"):
            mapping.setdefault(int(domain_id), name)
    return mapping


def import_domain(
    domain: dict, collection: str, max_problems: int, dry_run: bool
) -> dict | None:
    """Download one domain and its problems. Return a state record, or None
    if the domain has no usable files."""
    domain_id = domain["domain_id"]
    name = slugify(domain.get("domain_name") or f"domain-{domain_id}")
    problems = get_json(f"{API_ROOT}/problems/{domain_id}")
    problems = [p for p in problems if p.get("problem_url") and p.get("domain_url")]
    if not problems:
        log(f"  domain {domain_id} ({name}): no downloadable problems, skipping")
        return None

    # Some domains ship one domain file per problem; keep the group sharing
    # the most common domain file so domain.pddl matches every problem kept.
    counts: dict[str, int] = {}
    for p in problems:
        counts[p["domain_url"]] = counts.get(p["domain_url"], 0) + 1
    domain_url = max(counts, key=lambda u: counts[u])
    problems = [p for p in problems if p["domain_url"] == domain_url][:max_problems]

    target = DOMAINS_DIR / slugify(collection) / name
    if target.exists():
        meta_file = target / "metadata.json"
        owner = json.loads(meta_file.read_text()).get("domain_id") if meta_file.exists() else None
        if owner != domain_id:
            # Same name, different domain (several IPCs reused names).
            target = target.with_name(f"{name}-{domain_id}")
    log(f"  {name} (id {domain_id}, {collection}): {len(problems)} problems -> {target.relative_to(REPO_ROOT)}")
    if dry_run:
        return None

    target.mkdir(parents=True, exist_ok=True)
    (target / "domain.pddl").write_text(get(domain_url).text)

    problem_files = []
    for p in problems:
        fname = slugify(Path(p.get("problem", f"p{p['problem_id']}")).stem) + ".pddl"
        (target / fname).write_text(get(p["problem_url"]).text)
        problem_files.append(
            {"file": fname, "problem_id": p["problem_id"], "source_url": p["problem_url"]}
        )

    metadata = {
        "domain_id": domain_id,
        "name": domain.get("domain_name"),
        "collection": collection,
        "description": (domain.get("description") or "").strip(),
        "tags": json.loads(domain.get("tags") or "[]"),
        "requirements": domain.get("requirements"),
        "source_api": f"{API_ROOT}/domain/{domain_id}",
        "domain_source_url": domain_url,
        "problems": problem_files,
        "attribution": (
            "Fetched from the planning.domains classical collection "
            "(https://api.planning.domains). Domains originate from the "
            "International Planning Competitions and community submissions; "
            "see the source URLs for the original files."
        ),
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    (target / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")

    return {
        "name": name,
        "collection": collection,
        "path": str(target.relative_to(REPO_ROOT)),
        "problems": len(problem_files),
        "fetched_at": metadata["fetched_at"],
    }


def regenerate_catalog() -> None:
    rows = []
    for meta_path in sorted(DOMAINS_DIR.glob("*/*/metadata.json")):
        meta = json.loads(meta_path.read_text())
        rel = meta_path.parent.relative_to(REPO_ROOT)
        tags = " ".join(f"`{t}`" for t in meta.get("tags", [])) or "—"
        rows.append(
            f"| [{meta.get('name') or rel.name}]({rel}/) | {meta.get('collection', '—')} "
            f"| {tags} | {len(meta.get('problems', []))} "
            f"| [planning.domains]({meta.get('source_api', '')}) |"
        )
    lines = [
        "# Catalog",
        "",
        "Domains synced from the [planning.domains](https://api.planning.domains)",
        "classical collection. Regenerated by `scripts/sync_domains.py` on every",
        "sync run — do not edit by hand. The eight hand-written examples at the",
        "repository root are not listed here.",
        "",
        f"{len(rows)} synced domains.",
        "",
        "| Domain | Collection | Requirements | Problems | Source |",
        "| :--- | :--- | :--- | ---: | :--- |",
        *rows,
        "",
    ]
    CATALOG_FILE.write_text("\n".join(lines))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH)
    parser.add_argument("--max-problems", type=int, default=DEFAULT_MAX_PROBLEMS)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    state = load_state()
    imported_ids = set(state["imported"])

    log("Fetching collection index...")
    collections = fetch_collections()
    pending = sorted(d for d in collections if str(d) not in imported_ids)
    if not pending:
        log("Nothing new to import — collection fully synced.")
        return 0
    log(f"{len(pending)} domains pending; importing up to {args.batch}.")

    imported = 0
    for domain_id in pending:
        if imported >= args.batch:
            break
        try:
            domain = get_json(f"{API_ROOT}/domain/{domain_id}")
        except RuntimeError as exc:
            log(f"  domain {domain_id}: metadata fetch failed ({exc}), skipping this run")
            continue
        record = import_domain(domain, collections[domain_id], args.max_problems, args.dry_run)
        if args.dry_run:
            imported += 1
            continue
        # Record even empty domains so they are not retried forever.
        state["imported"][str(domain_id)] = record or {"skipped": "no downloadable problems"}
        imported += 1

    if not args.dry_run:
        save_state(state)
        regenerate_catalog()
    log(f"Done: {imported} domain(s) processed, {len(pending) - imported} still pending.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
