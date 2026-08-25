# dsh-probhub

The first DeepSeek Harness adapter for ProbHub. This P0 package exposes only
two model-facing tools:

- `probhub_lint`
- `probhub_status`

Both tools run the installed `probhub` npm package from the current Harness
session workspace. They do not accept a workspace path, do not invoke a shell,
and do not implement ProbHub business logic.

## Install in a Harness profile

```sh
dsh plugin --profile web add --save-exact dsh-probhub@0.1.0
```

Restart the profile after installation. The profile must already provide the
standard Harness `tools`, `sandboxPolicy`, `sandbox`, and `subprocess` seams.

## Scope and limitations

- The current P0 adapter is read-only and does not expose judge, stress,
  mutation, seal, generation, build, package, or WebUI operations.
- The adapter applies the current Harness file policy before spawning the
  ProbHub CLI. Harness file sandboxing is a resource boundary, not a promise
  of safe execution for arbitrary hostile code.
- ProbHub's final JSON is preserved under `result`. Missing, truncated, or
  malformed JSON is reported as `adapter-failed` instead of being guessed.
- The adapter is pinned to ProbHub `0.7.0` and Harness `0.1.1-rc.2` tool seams
  for this proof of concept.
