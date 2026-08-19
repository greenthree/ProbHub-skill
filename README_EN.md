<div align="center">

<a href="https://github.com/greenthree/ProbHub-skill">
  <img src="logo.svg" alt="ProbHub" width="230">
</a>

<p><strong>Take a competitive-programming problem from idea to verified delivery.</strong></p>
<p><a href="README.md">中文</a> / <a href="README_EN.md">English</a></p>
<p>
  <a href="https://github.com/greenthree/ProbHub-skill">GitHub</a> ·
  <a href="https://github.com/greenthree/ProbHub-skill/releases">Releases</a> ·
  <a href="https://www.npmjs.com/package/probhub">npm</a> ·
  <a href="https://github.com/greenthree/ProbHub-skill/issues">Issues</a> ·
  <a href="https://github.com/greenthree/ProbHub-skill/blob/main/CHANGELOG.md">Changelog</a>
</p>

[![Release](https://img.shields.io/github/v/release/greenthree/ProbHub-skill?style=flat-square&label=release)](https://github.com/greenthree/ProbHub-skill/releases)
[![CI](https://img.shields.io/github/actions/workflow/status/greenthree/ProbHub-skill/ci.yml?branch=main&style=flat-square&label=CI)](https://github.com/greenthree/ProbHub-skill/actions/workflows/ci.yml)
[![npm](https://img.shields.io/npm/v/probhub?style=flat-square&label=npm)](https://www.npmjs.com/package/probhub)
[![Node.js](https://img.shields.io/badge/Node.js-18%2B-339933?style=flat-square&logo=node.js&logoColor=white)](https://nodejs.org/)
[![License](https://img.shields.io/badge/license-MIT-2f80ed?style=flat-square)](LICENSE)

</div>

## What is ProbHub?

ProbHub is a local workflow for ACM/ICPC problem setters. It connects statements, source code, test data, judging, typesetting, and DOMjudge packages. You can use it directly or let Codex, Claude Code, and other Agents collaborate under the same rules.

It is useful for authors creating a contest from scratch, teams maintaining many statements and data sets, and anyone who wants Agent assistance without giving up reviewable and reproducible delivery artifacts.

ProbHub is designed for a trusted, single-user local environment. It limits time, memory, output, and process trees, but it is not a security container for running hostile or untrusted code.

## At a glance

| Your goal | What ProbHub provides |
| --- | --- |
| Organize a problem | Workspace Schema v1, statement templates, configuration, and source layout |
| Check that judging is real | Validator, accepted, brute, wrong solutions, samples, and no-cache Judge |
| Find hidden counterexamples | Stress differential testing, replay, data-group roles, and std mutation testing |
| Test a special Judge | Checker/Interactor fixtures, robustness probes, and isolated Judge QA |
| Build and publish a contest | Typst PDFs, DOMjudge ZIPs, deep package verification, and Manifests |
| Work with an Agent | A global Skill, three verification modes, and explicit hand-off rules |

~~~mermaid
flowchart LR
    Idea["Problem idea"] --> Source["Statement + config + code + data"]
    Source --> Verify["lint / Judge / stress / Judge QA"]
    Verify --> Freeze["seal: freeze a reproducible revision"]
    Freeze --> Preview["Complete contest preview"]
    Freeze --> Build["Formal build"]
    Build --> Delivery["PDF + DOMjudge ZIP + Manifest"]
~~~

## Quick start

### 1. Install the Skill

Install Node.js 18+ (including npm) and Python 3.10+. On Ubuntu, the system Python also needs python3-pip. ProbHub does not require or recommend creating a virtual environment, and it does not recommend writing dependencies into the global system Python directory.

Windows PowerShell:

~~~powershell
npm install -g probhub
$env:PROBHUB_ALLOW_SYSTEM_PYTHON = "1"
probhub-skill
probhub doctor
~~~

Ubuntu/Linux:

~~~bash
npm install -g probhub
PROBHUB_ALLOW_SYSTEM_PYTHON=1 probhub-skill
probhub doctor
~~~

PROBHUB_ALLOW_SYSTEM_PYTHON=1 authorizes this installation to write pinned Python dependencies to the selected interpreter's user site. It does not overwrite Ubuntu's package-managed global packages or disable resource limits. The PowerShell setting only applies to the current terminal session.

The Skill is installed into:

~~~text
~/.claude/skills/probhub
~/.agents/skills/probhub
~~~

### 2. Call an Agent

Open Codex, Claude Code, or another compatible Agent in the directory where you keep contest files, then describe the goal:

~~~text
Use the probhub skill to create an algorithm contest and start with the first problem.
The problem idea is: ...
Complete the statement, programs, data, verification, typesetting, and DOMjudge package.
~~~

For an existing problem:

~~~text
Use the probhub skill to continue L01.
Check the statement, accepted solution, Validator, brute, typical wrong solutions,
and test data. Complete Judge, stress, seal, and the final build without changing other problems.
~~~

You can provide an idea, Markdown, PDF, web page, code, or data. The Agent reads the Schema v1 source of truth and calls the same ProbHub Core; you mainly review the problem meaning, algorithm, data strength, and final PDF.

## Choose a verification mode

The default is Normal. Every mode runs baseline lint, sample checks, no-cache Judge, and delivery gates. High-risk signals can only upgrade the mode.

| Mode | Use it when | What it does |
| --- | --- | --- |
| **Quick** | The problem is simple and deterministic, the proof is closed, and Judge risk is low | Runs 100 fixed-seed stress rounds; no independent Agent |
| **Normal (default)** | Most problems | Runs formal stress and asks one blind reviewer, seeing only the frozen statement, for an independent proof and std |
| **Full** | Hard, randomized, heuristic, floating-point, special-Judge, resource-tight, or disputed problems | Adds independent proof/reference review and adversarial review; suitable standard+C++ problems also receive a bounded mutation recommendation |

Verification modes describe Agent behavior, not CLI flags. They do not replace an algorithmic proof or turn local measurements into Linux/DOMjudge performance guarantees. See the [verification mode guide](references/verification-modes.md).

## From idea to delivery

1. **Set up the workspace:** use Workspace Schema v1 and fix the contest metadata and stable problem order.
2. **Maintain source files:** put the statement in problem.md, limits and Judge settings in probhub.yaml, and code/data inside the problem directory.
3. **Verify:** run lint, sample checks, and Judge; add stress, Judge QA, and mutation where configured or appropriate.
4. **Freeze:** seal the current revision and create an isolated complete contest preview; parallel problem work does not need to wait for other problems.
5. **Publish:** once all problems are sealed, run one multi-problem build for the formal PDFs, ZIPs, and Manifests.

In parallel work, each task edits only its own problem directory and publishes immutable checkpoints. Previews never read another task's live files. See [Checkpoints, seals, and generations](references/generations.md).

## WebUI

Run the WebUI from a contest directory containing .probhub/workspace.yaml:

~~~bash
probhub ui
~~~

To keep the browser closed:

~~~bash
probhub ui --no-browser
~~~

The default address is <http://127.0.0.1:33933/>. To check an installation without starting a server:

~~~bash
probhub --json ui --check
~~~

The WebUI edits statements, samples, limits, covers, and problem order. It also provides live preview, isolated compilation, temporary code judging, and task cancellation. Compile is for isolated preview; Distribute is the operation that formally writes PDFs, ZIPs, and build records. Request and task queues have explicit limits and return retryable feedback when busy; the service listens only on the local loopback address.

## CLI for manual control

Agents and the WebUI use the same Core. Most users do not need every command; these are the usual troubleshooting and orchestration commands:

| Command | Purpose |
| --- | --- |
| <code>probhub doctor</code> | Check Python, Node.js, npm, g++, Typst, fonts, and dependencies |
| <code>probhub init</code> | Initialize a Schema v1 workspace |
| <code>probhub new L01</code> | Create a compilable, judgeable problem skeleton |
| <code>probhub lint L01</code> | Check layout, config, statement, data, and constraints |
| <code>probhub judge L01 --no-cache</code> | Compile and run the Validator, accepted, brute, and wrong solutions |
| <code>probhub stress L01 --rounds 1000 --seed 12345</code> | Differential-test random small cases |
| <code>probhub judge-qa L01 --no-cache</code> | Actively test Checker/Interactor fixtures |
| <code>probhub seal L01 --no-cache</code> | Verify and freeze the current revision |
| <code>probhub build L01 --no-cache</code> | Formally create PDFs, a ZIP, and a Manifest |
| <code>probhub status L01</code> | Check source and formal artifacts for consistency |

Commands use stable IDs from workspace.yaml (for example L01), not display letters derived from the order. See the [CLI reference](references/cli.md) for all options.

## Workspace layout

~~~text
L01/
├── probhub.yaml
├── problem.md
├── code/
│   ├── std.cpp
│   ├── validator.cpp
│   ├── brute.cpp
│   └── wrong.cpp
└── data/
    ├── sample/
    └── secret/
~~~

| Path | Contents |
| --- | --- |
| .probhub/workspace.yaml | Contest metadata, problem order, and typesetting settings |
| L01/problem.md | Statement, input, output, and notes |
| L01/probhub.yaml | Limits, Judge type, code, and data configuration |
| L01/code/ | Accepted, brute, wrong, Validator, and Checker/Interactor sources |
| L01/data/sample/ | Samples shown in the statement |
| L01/data/secret/ | Official hidden test data |

Do not edit Core-generated files by hand: meta.json, Typst problems.json, problem.yaml, domjudge-problem.ini, problem.pdf, the full contest PDF, <ID>.zip, or .probhub/build-manifest.json. Re-run seal/build when artifacts are stale.

## What to verify

- validator.cpp must strictly check format, ranges, and structure. For multi-case input, use a sufficiently wide accumulator and actually reject inputs over the statement's aggregate limit.
- Register accepted, brute, and wrong solutions with data-group roles. killed means that a known wrong solution was killed; it does not prove that no unknown wrong solution passes.
- Register judge.qa fixtures for custom or interactive problems, and require judge-qa to return passed with current evidence before delivery.
- mutation supplements standard C++ testing. survived, manual exclusions, and a mutation score are not correctness proofs.
- A fixed seed is for replay. Local resource measurements do not replace Linux/DOMjudge calibration.

Read the [data-group guide](references/data-groups-expectations.md), [mistake taxonomy](references/mistake-taxonomy.md), [Checker and Interactor guide](references/checker-interactor.md), and [std mutation guide](references/mutation-testing.md) for details.

## Delivery checklist

A delivered problem should have: passing lint, Judge, and required stress/Judge QA; a final Judge result of all_expectations_met; passed/current evidence for configured special Judges; a successful seal; current status after the formal build; a deep ZIP verification with no errors; and a manual review of both the problem PDF and the complete contest PDF.

The normal delivery includes main.pdf, each problem.pdf, and <ID>.zip at the workspace root. Re-verify and rebuild after changes to statements, data, order, templates, or the toolchain.

## Installation and platforms

| Tool | Requirement | Use |
| --- | --- | --- |
| [Python](https://www.python.org/downloads/) | 3.10+ | Run the ProbHub Core |
| [Node.js](https://nodejs.org/) | 18+ | Install npm packages and the Agent Skill |
| g++ | C++17 | Compile accepted, Validators, and Checkers |
| [Typst](https://github.com/typst/typst/releases/tag/v0.14.2) | 0.14.2 | Generate PDFs |
| Noto Sans CJK SC | Bundled with the main package | Stable Chinese statement rendering |

On Windows, install g++ with [MSYS2](https://www.msys2.org/) and use the pinned Typst 0.14.2. On Ubuntu:

~~~bash
sudo apt update
sudo apt install -y g++ python3-pip
~~~

The fixed font ships with the probhub package and is byte-checked during formal compilation. macOS usually works but is not a required release-CI platform.

Without a global npm install:

~~~powershell
$env:PROBHUB_ALLOW_SYSTEM_PYTHON = "1"
npx probhub-skill
~~~

~~~bash
PROBHUB_ALLOW_SYSTEM_PYTHON=1 npx probhub-skill
~~~

Add --local to install the Skill only in the current project.

## Troubleshooting

### probhub command not found

~~~bash
node --version
npm --version
npm install -g probhub
~~~

Check that npm's global executable directory is on PATH, or use npx probhub --version temporarily.

### doctor reports an error

Follow the diagnostics. Common causes are the wrong Python interpreter, missing dependencies, g++ or Typst missing from PATH, a Typst version other than 0.14.2, or a missing bundled font. Re-run the installation command with PROBHUB_ALLOW_SYSTEM_PYTHON=1 to install the pinned dependencies.

### WebUI does not open

Confirm that .probhub/workspace.yaml exists in the current directory or a parent, then run:

~~~bash
probhub --json ui --check
probhub ui --no-browser
~~~

Open <http://127.0.0.1:33933/> manually.

### build asks for a sealed revision

~~~bash
probhub seal L01 --no-cache --seed 12345
~~~

Seal every problem, then run one multi-problem build.

### seal reports seal_judge_qa_failed

Run probhub judge-qa L01 --no-cache. Fix the Checker, Interactor, simulated contestant, or fixture according to the structured result, then seal again. FAIL means a problem-infrastructure failure, not a successful wrong-answer kill.

### status reports stale

Read stale_fields, then seal and build again. Do not edit the Manifest by hand.

### recovery_required

Re-run the original build, gen --apply, or stress --fixate command so ProbHub can recover its transaction record. Do not delete recovery material under .probhub manually.

## Security boundary

ProbHub is for a local, single-user, trusted setting. It limits time, memory, output, and process trees, but is not a strong sandbox; another process on the same machine may access the loopback service, and a CSRF token is not multi-user host authentication. Do not run unknown or intentionally hostile code with ProbHub; use a dedicated VM or container instead.

## Further reading

- [CLI reference](references/cli.md)
- [Installation and release guide](references/installation.md)
- [Workspace Schema v1](references/workspace-schema-v1.md)
- [Data groups and solution expectations](references/data-groups-expectations.md)
- [Mistake taxonomy and data strength](references/mistake-taxonomy.md)
- [Stress differential testing](references/stress.md)
- [Std mutation testing](references/mutation-testing.md)
- [Agent verification modes](references/verification-modes.md)
- [Checker and Interactor](references/checker-interactor.md)
- [Process and resource control](references/process-control.md)
- [Checkpoints, seals, and generations](references/generations.md)

## Contributing

~~~bash
npm ci
npm run check
npm run pack:check
~~~

check validates Python, Node.js, WebUI assets, and the test suite. pack:check validates both npm package inventories. Reusable standard, custom, float, interactive, and stress workspaces are under tests/fixtures/.

Please use [GitHub Issues](https://github.com/greenthree/ProbHub-skill/issues) for bugs and suggestions. See [CHANGELOG.md](CHANGELOG.md) for release history.

## Acknowledgements

- [CYaRon](https://github.com/luogu-dev/cyaron), a test-data generator.
- [olymp-in-typst](https://github.com/lihaoze123/olymp-in-typst), an algorithm-contest Typst template.
- [testlib](https://github.com/MikeMirzayanov/testlib), a contest judging library.

## License

[MIT](LICENSE)
