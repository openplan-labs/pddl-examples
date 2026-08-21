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
from urllib.parse import urlparse

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


def upstream_slug(domain_url: str) -> str | None:
    """The directory name upstream uses, which carries the IPC track.

    `domain_name` from the API is the bare family -- three different domains
    all answer "transport". The track that distinguishes them (`opt11` vs
    `sat11` vs `opt14`) survives only in the file URL, as
    `.../classical/transport-sat11-strips/domain.pddl`. Mixing an
    optimal-track and a satisficing-track instance set under one label is how
    a benchmark comparison gets silently corrupted, so the URL wins.
    """
    parts = [seg for seg in urlparse(domain_url).path.split("/") if seg]
    if len(parts) < 2:
        return None
    return slugify(parts[-2]) or None


def track_of(meta: dict) -> str:
    """The competition track, if the upstream path or description names one."""
    slug = upstream_slug(meta.get("domain_source_url") or "") or ""
    match = re.search(r"-((?:opt|sat|net|mco|adl)\d{2})\b", slug)
    if match:
        return match.group(1)
    # The description sometimes opens with a marker. Accept it only when it
    # looks like a track -- a bare "(2008)" is a year, not a track, and
    # printing it in this column would invent a distinction.
    match = re.match(r"\((\s*(?:opt|sat|net|mco|adl)\d{2}\s*)\)",
                     (meta.get("description") or "").strip())
    return match.group(1).strip() if match else "—"


def per_problem_domain_file(meta: dict) -> bool:
    """True when upstream ships a domain file per problem.

    Those domains cannot be imported whole -- only the largest group sharing
    one domain file is kept -- so their problem count collapses, often to one.
    Detectable after the fact from the file name.
    """
    name = (meta.get("domain_source_url") or "").rsplit("/", 1)[-1]
    return bool(re.search(r"(^|[-_])p\d+[-_]?domain|domain[-_]p\d+", name))


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
    n_available = len(problems)
    problems = [p for p in problems if p["domain_url"] == domain_url]
    n_matching = len(problems)
    problems = problems[:max_problems]
    if n_matching < n_available:
        log(
            f"  domain {domain_id}: {n_available - n_matching} of {n_available} "
            "problems ship their own domain file and are not imported"
        )

    # Prefer upstream's own directory name: it disambiguates the IPC track,
    # which `domain_name` does not.
    name = upstream_slug(domain_url) or name
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
        # `problems_available` is what upstream lists; `problems` is what was
        # kept. They differ when a domain ships one domain file per problem,
        # in which case only the largest matching group is importable and the
        # count can collapse to one. Recording both keeps a "Problems: 1" row
        # from reading as "upstream has one problem".
        "problems_available": n_available,
        "problems_matching_domain_file": n_matching,
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
        # The API's `requirements` string is the domain's own declaration.
        # `tags` is a curated label set and the two disagree, so prefer the
        # declaration and mark the rows that only have tags to fall back on.
        declared = (meta.get("requirements") or "").split()
        if declared:
            reqs = " ".join(f"`{r}`" for r in declared)
        elif meta.get("tags"):
            reqs = " ".join(f"`{r}`" for r in meta["tags"]) + "[^tags]"
        else:
            reqs = "—"
        kept = len(meta.get("problems", []))
        available = meta.get("problems_available")
        matching = meta.get("problems_matching_domain_file")
        if available is not None and matching is not None and matching < available:
            problems = f"{kept} of {available}[^split]"
        elif per_problem_domain_file(meta):
            problems = f"{kept}[^split]"
        else:
            problems = str(kept)
        rows.append(
            f"| [{rel.name}]({rel}/) | {meta.get('collection', '—')} "
            f"| {track_of(meta)} | {reqs} | {problems} "
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
        "| Domain | Collection | Track | Requirements | Problems | Source |",
        "| :--- | :--- | :--- | :--- | ---: | :--- |",
        *rows,
        "",
        "[^split]: Upstream ships a separate domain file per problem for this",
        "    domain. Only the largest group sharing one domain file is imported,",
        "    so the rest are not here -- a count of 1 means one *importable*",
        "    problem, not one upstream. See `metadata.json` for the full count.",
        "",
        "[^tags]: This domain publishes no `requirements` string, so the row",
        "    falls back to the API's curated `tags`, which are not the same",
        "    thing and can disagree with the domain file.",
        "",
        "The **Domain** column is upstream's own directory name, which carries",
        "the competition track. `domain_name` alone does not: three different",
        "IPC domains all answer \"transport\".",
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
