<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/openplan-labs/branding/main/assets/logo/mark-dark.svg">
  <img src="https://raw.githubusercontent.com/openplan-labs/branding/main/assets/logo/mark-accent.svg" width="44" alt="OpenPlan Labs">
</picture>

# pddl-examples

A collection of PDDL (Planning Domain Definition Language) domains and
problems: eight small classroom examples, plus a growing mirror of the
[planning.domains](https://api.planning.domains) classical collection that a
daily job extends automatically. Domain files and problem files only —
planners not included.

## Layout

```
blocksworld/ dinner/ flip/ grid/     the eight hand-written examples,
pallet/ switch/ tsp/ vehicle/        one domain.pddl + one problem.pddl each
domains/<collection>/<domain>/       synced from planning.domains
  domain.pddl                        the domain file
  *.pddl                             up to 20 problem files
  metadata.json                      provenance: source URLs, tags, fetch date
catalog/state.json                   which domain IDs are already imported
CATALOG.md                           generated index of the synced domains
scripts/sync_domains.py              the sync script
```

The eight original examples stay at the repository root so existing paths —
including submodule references from
[PythonPDDL](https://github.com/openplan-labs/PythonPDDL) — keep working.

## How the daily sync works

A [scheduled workflow](.github/workflows/sync.yml) runs
`scripts/sync_domains.py` once a day (04:17 UTC). Each run:

1. reads `catalog/state.json` for the set of already-imported domain IDs,
2. asks the planning.domains JSON API for the classical collections and picks
   up to 5 domains not yet imported,
3. downloads each domain file and up to 20 of its problem files,
4. writes them under `domains/<collection>/<upstream-name>/` with a
   `metadata.json` recording where every file came from,
5. regenerates [`CATALOG.md`](CATALOG.md).

The workflow, not the script, then commits the batch — `git add -A` and a push
straight to `main`, with no pull request and therefore no review. The batch cap
keeps each diff small enough to read after the fact, which is not the same
thing. When the collection is fully mirrored the job exits with "nothing new to
import". Run it yourself with:

```sh
pip install requests
python3 scripts/sync_domains.py            # next batch of 5
python3 scripts/sync_domains.py --dry-run  # show what it would fetch
```

## Contributing your own domains

Contributions are welcome — a domain you wrote for a course, a paper, or a
robot is exactly what this collection is for. Open a pull request that:

- adds a new top-level directory named after the domain, containing
  `domain.pddl` and at least one problem file;
- states in the PR description what the domain models and, if it comes from a
  paper, which one;
- passes the [validate workflow](.github/workflows/validate.yml), which checks
  that every `.pddl` file has balanced parentheses and a `(define ...)` form.

Do not edit anything under `domains/` or `CATALOG.md` by hand — the sync job
regenerates them and will overwrite manual changes.

## Provenance and licensing

Files under `domains/` are indexed by the
[planning.domains](http://planning.domains) classical API and served from
`www.haz.ca`, which is where the sync actually downloads them. They aggregate
domains from the International Planning Competitions and community
submissions. Each synced domain's `metadata.json` records the exact source URLs
and the fetch date.

**Neither this repository nor the upstream host states a licence for those
files.** They are competition and community material of long standing,
redistributed here unmodified and with their provenance attached. The
[LICENSE](LICENSE) in this repository covers the repository — the sync script,
the catalogue, the layout — and cannot grant rights over upstream material that
nobody has licensed to us. If you are a rights holder and want something
removed or attributed differently, open an issue.

The examples at the root are small, standard teaching instances (blocksworld,
tsp, grid and similar) of the kind that appears in every textbook. They arrived
in 2021 as a bulk upload with no attribution recorded, so calling them original
work would overstate what is known about them.

## See also

[awesome-pddl](https://github.com/openplan-labs/awesome-pddl) — a curated
list of PDDL resources: planners, parsers, editors, and learning material.
