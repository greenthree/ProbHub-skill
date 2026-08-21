# Python dependency audit

`requirements.txt` is the only manually maintained source for the exact runtime dependency closure, including Flask's transitive runtime dependencies. `requirements.lock` is generated from that source and ships with the npm package. It records the filename, size, and independently verified SHA-256 of every non-yanked wheel in each pinned PyPI release, requires hashes, and forbids source distributions. The offline lock check proves source/lock identity and wheel coverage for Windows and Linux x86_64 on CPython 3.10, 3.11, and 3.12.

After changing a runtime pin, run `npm run python:lock:update`. The command downloads every allowed wheel, recomputes its SHA-256, prints a structured added/removed/changed diff, and atomically replaces the lock only after the complete candidate succeeds. `npm run python:lock:check` is offline and never repairs a stale or malformed lock.

CI installs the separately pinned `pip-audit` tool closure from `requirements-audit.lock` and audits the active, marker-aware dependency set in `requirements.lock` with dependency resolution disabled. `requirements-audit.txt` is the exact-pin source for that CI-only closure and the same `npm run python:lock:update` command refreshes both locks. Audit-only dependencies do not ship in the npm package and remain outside the runtime lock identity. The installer supports only CPython 3.10-3.12 on Windows/Linux x86_64; other interpreters and architectures fail closed until their wheel targets are explicitly added.

The audit runs on every pull request, push, release tag, and on a weekly schedule on both Windows and Ubuntu. Scheduled runs skip the full quality and clean-install suites.

The audit fails when the advisory service is unavailable, the returned package/version set differs from the active runtime lock, output is invalid, or any applicable vulnerability is not reviewed. It does not silently downgrade empty, partial, network, or tool failures to warnings.

Temporary exceptions belong in `python-audit-exceptions.json`. Every entry must contain:

- `id`: the primary or alias vulnerability identifier;
- `package`: the affected package name;
- `reason`: why an immediate upgrade is not currently possible;
- `expires`: an ISO `YYYY-MM-DD` review deadline;
- `tracking_url`: an HTTP(S) issue or advisory URL.

Expired, duplicate, package-mismatched, and no-longer-applicable exceptions fail closed. Keep the list empty whenever possible.
