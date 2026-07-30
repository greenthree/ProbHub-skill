# ProbHub test workspaces

These fixtures are intentionally small, deterministic Workspace Schema v1
projects. Tests must copy a workspace to a temporary directory before running
Core commands because Judge, stress, and status checks create local artifacts.

`workspaces/` contains independent examples for standard, custom, float,
interactive, and stress workflows. `faults/` contains source files that the
test helper overlays onto a copied workspace to exercise infrastructure and
resource failures.

Do not commit generated binaries, caches, stress counterexamples, PDFs, ZIPs,
metadata, or Build Manifests under this directory.
