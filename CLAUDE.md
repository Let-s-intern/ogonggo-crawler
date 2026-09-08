@.claude/rules/core.md

# Repository Guide

A crawling automation backend. LLM generates the CSS selectors, a test run proves them, a workflow
runs them on a schedule, and the normalized result is served to a separate job board over REST.

Not a job board. `.claude/rules/core.md` states what follows from that.

Everything Claude Code needs lives under `.claude/`. The rules that constrain behaviour are imported
above; this file is the map that says where to look for the rest.

## The pipeline

```
URL 입력 → 셀렉터 생성(LLM) → 테스트 실행 → 워크플로우 등록 → 주기 실행
                                                                  ↓
                                        raw_jobs → 정규화 → normalized_jobs → 제공 API
```

Each stage fails independently and says which one failed. `.claude/docs/architecture.md` has the
detail.

## Layout

```
.claude/
├── rules/                    Constraints. One owner per rule; agents reference by path
│   ├── core.md               Main constraints, imported by this file
│   ├── crawling.md           One fetch client, politeness, failure classes, scheduling
│   ├── llm.md                Selector generation: bounded input, validated output
│   ├── data-safety.md        Raw is append-only, migrations, delivery state
│   ├── git-safety.md         Branch check before commit, commit convention, secrets
│   └── writing.md            Document writing
│
├── agents/                   Subagent definitions: role, rules, skills, boundary
│   ├── selector-worker.md    HTML cleaning, generation, selector fixes
│   ├── crawler-worker.md     Fetch client, parser, retry, scheduler, run logging
│   ├── api-worker.md         Routes, schema, migrations, normalization, delivery API
│   ├── ui-worker.md          Jinja2 templates and HTMX fragments
│   ├── test-runner.md        Runs and analyses tests
│   ├── push-lead.md          Splits one push and runs the workers. Never implements
│   └── task-executor.md      Single autonomous executor for a small task file
│
├── skills/                   Reusable procedures
│   ├── selector-generate/    URL -> selector JSON, validated against the same HTML
│   ├── crawl-test/           One live run, preview, failure classification
│   ├── workflow-ops/         Workflow status, pause, resume, interval
│   ├── db-inspect/           Read-only look at what the pipeline actually stored
│   ├── site-recipe/          Per-site findings, written down once
│   ├── quality-check/        ruff, mypy, pytest on changed files
│   ├── local-env/            Start, inspect and stop the local server
│   ├── task-maker/           PRD -> push files with per-type verification
│   ├── task-runner/          Reads a task file and delegates. Picks mode A or B
│   └── task-cleaner/         Archive a finished feature into done/<name>/
│
├── commands/                 Slash-command entry points
│   ├── new-site.md           /new-site <list-url> <detail-url>
│   └── fix-workflow.md       /fix-workflow <id>
│
├── hooks/                    Scripts registered in settings.json
│   ├── post-edit-format.sh   ruff format and fix on write
│   ├── guard-direct-fetch.sh Warns when a module bypasses the shared fetch client
│   ├── check-tasks.sh        Keeps a task-runner session going
│   └── inject-task-context.sh Restores task progress after compaction
│
├── docs/                     Project documentation (Korean)
│   ├── README.md             Index
│   ├── architecture.md       Structure, pipeline stages, folder layout
│   ├── data-model.md         Tables, content hash, state transitions
│   ├── api-contract.md       The delivery API the job board consumes
│   ├── spring-delivery.md    Sending to the ogonggo backend. Design only, not built
│   ├── tech-stack.md         What is used, and what is deliberately not
│   └── ocr-benchmark.md      Measured cost of the four collection paths
│
├── site-recipes/             One file per site: rendering, pagination, past failures
├── troubleshooting/          Open or unexplained issues
├── dev-records/              Handover records
└── tasks/                    PRD, todo/, done/, memos/
```

## Where to look

| Situation | Read first |
|---|---|
| Registering a new site | `commands/new-site.md`, `skills/selector-generate/SKILL.md` |
| A selector stopped matching | `site-recipes/<domain>.md`, `skills/crawl-test/SKILL.md` |
| A workflow is failing | `commands/fix-workflow.md`, `skills/workflow-ops/SKILL.md` |
| Changing the fetch client, retry or scheduler | `rules/crawling.md`, `docs/architecture.md` |
| Writing or changing a model API call | `rules/llm.md`, then that provider's current docs |
| Changing a table or writing a migration | `rules/data-safety.md`, `docs/data-model.md` |
| Changing what the job board receives | `docs/api-contract.md` |
| Sending postings to the ogonggo backend | `docs/spring-delivery.md` |
| A data question ("did it actually store it") | `skills/db-inspect/SKILL.md` |
| Normalization rules producing wrong values | `docs/data-model.md`, `agents/api-worker.md` |
| Building a screen | `agents/ui-worker.md` |
| Running anything locally | `skills/local-env/SKILL.md` |
| Format, lint, typecheck, test before commit | `skills/quality-check/SKILL.md` |
| Committing or pushing | `rules/git-safety.md` |
| Why a technology was or was not chosen | `docs/tech-stack.md` |
| Choosing how to collect a site (static/api/render/OCR) | `docs/ocr-benchmark.md` |
| PRD -> tasks | `skills/task-maker/SKILL.md` |
| Executing tasks | `skills/task-runner/SKILL.md`, then `agents/push-lead.md` for mode A |
| Archiving a finished feature | `skills/task-cleaner/SKILL.md` |
| Writing any document | `rules/writing.md` |
| Something learned about one site | `skills/site-recipe/SKILL.md` |
| Unexplained or open issue | `troubleshooting/README.md` |

All paths above are relative to `.claude/`.

## Two things that are easy to get wrong

A run that extracted nothing is a failure, not an empty success. If it reports as a clean run with
zero new postings, a site that changed its markup looks identical to a site with no new jobs.

Dirty extracted text is a normalization problem, not a selector problem. Fixing it in the selector
is how a working selector gets replaced with a fragile one.
