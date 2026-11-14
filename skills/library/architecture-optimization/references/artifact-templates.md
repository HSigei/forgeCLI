# architecture-optimization artifact templates

Skeletons for every docs/ file this journey writes. Read the file before writing: if it is missing, create it from the full skeleton below (all section headings), then fill only the sections your phase names; if it exists, extend it — add or update your sections and preserve everyone else's. A journey that starts in a project with no `docs/` folder creates several of these from scratch, which is the normal case for this skill.

## Tracker: docs/ARCHITECTURE-OPTIMIZATION-PLAN.md

Created on the first run (Intake). Never shared with another journey.

```markdown
# Architecture Optimization Plan

## Context
Intake answers, date started, hot paths in scope.
Mode: skills-installed | fallback (Briefs + references/methods.md) — set at the first phase, so a
resumed session knows whether earlier artifacts are skill-grade or Brief-grade.

## Phase Status
| Phase | Skill | Status | Artifact | Date |
|---|---|---|---|---|
| 1 | working-with-legacy-code | pending | TESTING.md, TECH-DEBT.md, PERFORMANCE.md | |
| 2 | clean-architecture | pending | ARCHITECTURE.md | |
| 3 | software-design-philosophy | pending | TECH-DEBT.md | |
| 4 | refactoring-patterns | pending | TECH-DEBT.md, TESTING.md | |
| 5 | system-design | pending | PERFORMANCE.md, ARCHITECTURE.md | |
| 6 | ddia-systems | pending | ARCHITECTURE.md, PERFORMANCE.md | |
| 7 | release-it | pending | RELIABILITY.md | |
| 8 | pragmatic-programmer | pending | PERFORMANCE.md, TECH-DEBT.md, TESTING.md | |
Statuses: pending · in-progress · awaiting-evidence · done · deferred: <reason> · skipped: <reason>
A phase parked at `awaiting-evidence` carries a Next Actions row naming the evidence owed, its owner,
and the date — otherwise the next session cannot tell what it is waiting for.

## Key Decisions
| Date | Phase | Decision | Rationale |
|---|---|---|---|

## Next Actions
- [ ] action (owner, due)
```

## docs/PERFORMANCE.md

Measured performance state — baselines, budgets, findings, and the before/after ledger. Creates: architecture-optimization.

```markdown
# Performance

## Baselines & Budgets
| Metric (p50/p95, throughput, cost) | Baseline (date) | Budget | Gate |
|---|---|---|---|

## Load Reality
Measured QPS average/peak, data volumes, growth rate. This is the single source for load numbers —
ARCHITECTURE.md `## System Context` cites it rather than repeating them.

## Profile Findings
| Hotspot | Evidence (profiler/APM) | Suspected cause | Fix | Status |
|---|---|---|---|---|

## Optimization Ledger
| Change | Before | After | Verdict (keep/revert) | Date |
|---|---|---|---|---|
```

## docs/TESTING.md

The safety net — what behavior is pinned, where the gaps are.

```markdown
# Testing

## Test Strategy
Pyramid, tooling, what "green" gates.

## Safety Net Map
| Module | Pinned behaviors | Test files | Gaps |
|---|---|---|---|

## Characterization Backlog
- [ ] module (risk, priority)

## CI Gates
```

## docs/TECH-DEBT.md

The debt ledger — single queue for all code journeys.

```markdown
# Technical Debt

## Debt Ledger
| Item | Location | Type | Risk | Effort | Priority | Status |
|---|---|---|---|---|---|---|

## Smell Inventory
| Smell | Location | Refactoring | Status |
|---|---|---|---|

## Sprout / Wrap Register
Code added beside legacy (to fold back in later).

## Debt Budget & Broken-Windows Policy
Time per iteration; what gets fixed now vs boarded up with a ticket.

## Adopted Conventions
```

## docs/ARCHITECTURE.md

System structure and decisions — includes domain model and data decisions (no separate DATA.md or DOMAIN.md).

```markdown
# Architecture

## System Context
What the system does, key integrations, load reality (cite PERFORMANCE.md `## Load Reality`).

## Layer Map & Dependency Rule
Layers, what depends on what, current violations.
| Violation | Location | Fix | Status |
|---|---|---|---|

## Bounded Contexts & Context Map
Contexts, relationships, anti-corruption layers.

## Domain Glossary (Ubiquitous Language)
| Term | Meaning | Code name |
|---|---|---|

## Data & Storage Decisions
Data models, storage engines, isolation levels, replication, system-of-record vs derived data.

## Decision Log
| Date | Decision | Why | Alternatives rejected |
|---|---|---|---|
```

## docs/RELIABILITY.md

Production hardening status.

```markdown
# Reliability

## Integration-Point Audit
| Dependency | Timeout | Circuit breaker | Bulkhead | Retry policy | Status |
|---|---|---|---|---|---|

## Query & Resource Findings
Unbounded result sets, missing LIMITs/pagination, blocked threads.

## Health Checks & Metrics
Deep health checks · RED metrics · symptom-based alerts.

## Deploy vs Release
Feature flags, expand-contract migrations, rollback plan.
```
