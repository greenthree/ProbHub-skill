# Python dependency audit

`requirements.txt` pins the exact runtime dependency closure shipped with the npm package, including Flask's transitive runtime dependencies. CI installs the separately pinned `pip-audit` version from `requirements-audit.txt` and audits exactly those listed versions with dependency resolution disabled. The audit tool's own CI-only dependencies do not ship in the npm package.

The audit runs on every pull request, push, release tag, and on a weekly schedule on both Windows and Ubuntu. Scheduled runs skip the full quality and clean-install suites.

The audit fails when the advisory service is unavailable, dependency collection is incomplete, output is invalid, or any applicable vulnerability is not reviewed. It does not silently downgrade network or tool failures to warnings.

Temporary exceptions belong in `python-audit-exceptions.json`. Every entry must contain:

- `id`: the primary or alias vulnerability identifier;
- `package`: the affected package name;
- `reason`: why an immediate upgrade is not currently possible;
- `expires`: an ISO `YYYY-MM-DD` review deadline;
- `tracking_url`: an HTTP(S) issue or advisory URL.

Expired, duplicate, package-mismatched, and no-longer-applicable exceptions fail closed. Keep the list empty whenever possible.
