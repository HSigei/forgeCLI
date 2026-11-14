# architecture-optimization phase methods

The full fallback method for each phase — read the relevant section before running a phase from its Brief (constituent skill not installed). Each section distills the constituent skill plus the named external sources into a runnable procedure. When the constituent skill *is* installed, use it; this file then serves only as the cross-phase glue (formulas, orderings, heuristics).

## §1 Pin it and measure it (Working Effectively with Legacy Code — Feathers; Systems Performance — Gregg)

**Behavior net:**

1. **Identify change points.** For each hot flow, list the methods that will change in Phases 2-6. Sketch effects outward from each: what state and outputs does it influence?
2. **Break dependencies at seams.** Least-invasive first: Parameterize Constructor (inject the dependency, keep a production-default constructor), Extract Interface for the collaborator, Extract and Override for one buried call. The goal is getting the class into a test harness with minimal edits.
3. **Write characterization tests.** Photograph actual behavior: call with representative inputs, assert something deliberately wrong, read the failure, pin the real value. Cover the money behaviors first (billing, auth, the core action). A bug discovered here gets pinned as-is and ledgered — callers may depend on the quirk.
4. **When full coverage isn't feasible in time,** Sprout (new code in new tested methods/classes called from the legacy host) or Wrap (new behavior around the old call), and track the untested host in the Debt Ledger.

**Performance net:**

5. **Profile before touching anything.** CPU profiler or APM trace on the real workload — the bottleneck is rarely where intuition points, and the profile is the only arbiter. Capture: p50/p95/p99 latency per hot endpoint, throughput, error rate; query time share; external-call time share; allocation/GC pressure where relevant.
6. **Measure under realistic data.** Dev databases with 100 rows lie about queries that scan millions. Use a production-like snapshot or synthetic volume; record the volume with the baseline.
7. **Run the USE method (Gregg) per resource** — for CPU, memory, disk I/O, network, and every bounded pool (DB connections, thread pools, worker queues): Utilization (how busy), Saturation (queue depth, wait time), Errors. Saturated pools produce latency cliffs that look like application slowness.
8. **Set budgets.** Per hot flow: the number it must meet (e.g. p95 < 500ms). State the budget at the load you can actually observe — an APM only sees current traffic, so a "at 2× peak" budget requires a load-test tool; without one, budget at current peak and note the headroom question as unanswered. Budgets make "done" objective, gate Phase 8's CI gates, and stop optimization past the point of payoff.
9. **Fix the tool of record.** Same profiler/APM/load-test configuration for every before/after in the ledger — mixed tools make deltas meaningless.

## §2 Re-draw the boundaries (Clean Architecture — Martin)

1. **Map the real dependency graph.** Imports, not intentions: which modules name the framework, the ORM, vendor SDKs? Tooling helps (dependency-cruiser, import-linter, jdeps, go list), but a grep for framework imports inside business logic finds most of it.
2. **List violations against the Dependency Rule.** Inward only: Frameworks → Interface Adapters → Use Cases → Entities. A violation = an inner circle naming an outer one (a use case importing the ORM entity, a domain rule reading the HTTP request).
3. **Extract by Dependency Inversion, hot paths first.** The use case owns a repository/gateway interface; the Postgres/Stripe/S3 implementation moves to an outer adapter. Do it seam by seam under the Phase 1 tests — never a big-bang re-layering.
4. **Boundary only at volatility.** Full interfaces at the DB, external services, and delivery mechanism; collapse ceremony layers elsewhere. A boundary is justified when you can name the second implementation you'd plausibly swap in (test double counts).
5. **Why this phase precedes optimization:** a boundary is an insertion point. Cache-aside goes inside the repository adapter; a queue goes behind the gateway interface; neither requires touching business rules — but only if the interface exists.
6. **Record every boundary decision** in the Decision Log with alternatives rejected — boundary placement is the expensive-to-reverse class of decision.

## §3 Deepen the modules (A Philosophy of Software Design — Ousterhout)

1. **Score depth.** For each module on the hot paths: functionality ÷ interface complexity. Deep = small interface hiding real machinery. Shallow = interface as large as the implementation (a class whose methods each wrap one call; a "manager" that forwards).
2. **Find the three red flags:** shallow classes/pass-throughs (same abstraction on both sides of a call); information leakage (one design decision — a format, a protocol, a naming rule — reflected in multiple modules, so one change touches many files); temporal decomposition (module structure mirrors execution order — `Step1Reader`, `Step2Validator` — instead of knowledge).
3. **Consolidate.** Merge shallow classes that always travel together and share state; pull the leaked decision into one owner module; convert pass-through layers into direct calls. Guard: don't merge genuinely unrelated concerns just to cut file count — depth, not size, is the goal.
4. **Somewhat-general-purpose interfaces:** design module interfaces for the class of use, not today's single caller — that is what keeps them small while functionality grows.
5. **Strategic over tactical:** budget 10-20% of the pass for design investment. The payoff inside this journey is immediate — Phases 4-6 touch fewer files.

## §4 Refactor the hot paths (Refactoring — Fowler; Clean Code — Martin)

1. **Name the smell, cite the transformation.** Long Method → Extract Method (if a block needs a comment, extract it and name it after the comment). Nested conditionals → Replace Nested Conditional with Guard Clauses. Type switches → Replace Conditional with Polymorphism. Data clumps → Introduce Parameter Object. Magic numbers → Symbolic Constant. Repeated computation of an expression → Replace Temp with Query.
2. **The mechanical loop:** tests green → one named transformation in small steps → tests green → commit. A red test mid-refactoring means revert and retry smaller, never debug forward. Structure-only commits carry no behavior change.
3. **Clean-code fold-in while passing:** names reveal intent (`elapsedDays`, not `d`); functions do one thing at one level of abstraction, 0-2 arguments (a boolean flag argument is two functions); Command-Query Separation; no null returns (empty collection, Optional, Null Object); every thrown error carries operation + state context; catch specific exception types.
4. **Preparatory Refactoring:** for each planned Phase 5-6 optimization, first reshape the insertion point so the optimization becomes a small diff ("make the change easy, then make the easy change") — the reshape commit and the optimization commit stay separate.
5. **Rule of Three:** tolerate duplication twice; abstract on the third occurrence — premature abstraction on hot paths is how frameworks-within-frameworks start.

## §5 Attack the bottleneck (System Design — Xu; Amdahl; Little's law)

1. **Amdahl's law picks the target.** Speedup_total = 1 / ((1 − p) + p/s), where p is the fraction of time in the optimized part and s its local speedup. Optimizing 5% of the request 10× yields 1.047× overall. Only profile-dominant parts are worth effort — rank Phase 1 hotspots by p.
2. **Confirm the gap with numbers.** Measured load always supersedes the estimate — if intake or APM gave you real QPS, use it and keep the formula only for growth projection. Estimating from scratch: QPS = DAU × actions/day ÷ 86,400; peak 2-5× average; storage = records/day × size × retention. Compare measured load and budget: sometimes the honest finding is "no gap — stop here."
3. **The cheapest-first order:**
   1. **Algorithm & I/O shape:** O(n²) loops on growing data, per-item external calls in a loop (batch them), serial awaits that could be parallel, oversized payloads. Free — no new infrastructure.
   2. **Concurrency and pool sizing:** Little's law (L = λW) gives the in-flight work the fleet must hold — 60 QPS × 4.2s = 252 concurrent requests, which a 32-thread server cannot hold, so requests queue and p95 inflates far beyond service time. Size worker threads, DB connections, and queue depth to L, or cut W; a saturated pool is often the single largest p95 contributor and it is free to diagnose. Beware: once a pool saturates, trace percentages include queue wait, so re-profile after fixing it before trusting the Amdahl split (§1.7).
   3. **Vertical headroom:** one instance-size bump often buys a year; note the ceiling and cost.
   4. **Cache-aside** on read-heavy paths: check cache → miss → load → populate, TTL plus explicit invalidation on the write path (name what event invalidates which key — an unnameable invalidation means don't cache it). Measure hit rate; below ~80% on a hot path, re-examine the key design. A cold or thrashing cache is pure overhead. Guard the stampede: on a hot key, expiry lets every concurrent request recompute at once (at 60 QPS and a 4s recompute, ~250 duplicate computations) — use single-flight/request coalescing or a short lock, stagger TTLs with jitter, and cache negative results so misses don't hammer the origin.
   5. **Queue slow work off the request path:** email, exports, webhooks, image processing. Little's law (L = λW): in-flight requests = arrival rate × latency — halving in-request latency also halves the concurrency the fleet must hold, so queueing is a capacity fix as much as a latency fix. Design for at-least-once delivery + idempotent consumers.
   6. **Read replicas** when reads dominate and the primary saturates; brings replication lag → §6's read-your-writes decisions.
   7. **Shard last,** only with evidence (write-side saturation that vertical + replicas can't absorb), by a key that spreads load and matches the dominant query.
4. **Re-measure after every move** with the tool of record; ledger before/after; keep only what beats its baseline. Also write down what you're deliberately NOT building (multi-region, CDN, shards) with the number that would trigger revisiting.

## §6 Fix the data layer (Designing Data-Intensive Applications — Kleppmann)

1. **Get a query-level source, then read plans.** If none exists, enable one first: `pg_stat_statements` (ranks by total time — the right ranking), `auto_explain`, or `log_min_duration_statement`. Then `EXPLAIN (ANALYZE, BUFFERS)` the top offenders — plain `EXPLAIN` returns planner estimates, not real timings, so it cannot produce the measured before/after the ledger requires. The plan, not the ORM code, tells the truth. Look for: sequential scans on large tables where an index on predicate + sort columns fits; N+1 patterns (one query per row of a parent result — batch with IN, JOIN, or the ORM's eager loading); SELECT * pulling wide rows into memory; deep OFFSET pagination (switch to keyset: WHERE id > last_seen ORDER BY id LIMIT n); unbounded result sets (every list endpoint gets LIMIT + pagination).
2. **Weigh every index:** it speeds the read paths that match it and taxes every write; drop unused indexes found in the stats views. Composite column order decides whether an index is used at all: equality predicates first, then the range or sort column — an index on `(status, created_at)` serves `WHERE status = ? ORDER BY created_at`, while `(created_at, status)` does not.
3. **Know your engine:** LSM-trees (Cassandra, RocksDB, LevelDB) favor write throughput; B-trees (Postgres, MySQL/InnoDB) favor read latency and transactional semantics. Match the model to the access pattern — relational for many-to-many and ad-hoc queries, document for self-contained aggregates read whole, graph for recursive traversals — before buying hardware.
4. **Audit isolation honestly.** Find the actual default (Postgres: read committed; MySQL/InnoDB: repeatable read snapshot; most cloud SQL: not serializable). Then list read-then-write paths — check availability then decrement, read balance then update — and fix write skew explicitly: `SELECT ... FOR UPDATE`, a unique/check constraint, or a serializable transaction. Write skew passes every single-user test; only concurrency exposes it.
5. **Replication-lag consequences (with §5 replicas):** read-your-writes (a user must see their own write — route their reads to the primary briefly), monotonic reads (pin a session to one replica). Decide per flow, deliberately.
6. **Derived data over dual writes.** A second read pattern (search index, analytics store, feed) is fed by CDC or an event log from the system of record — never by the application writing to two stores, which drifts on the first failure. Derived stores must be rebuildable from the source.

## §7 Keep it fast under failure (Release It! — Nygard)

1. **Audit every integration point** — DB, HTTP APIs, queues, caches, DNS, SMTP. For each: connect timeout, read timeout, breaker, bulkhead, retry policy, degradation path. An outbound call with no read timeout can hang a thread forever; enough of them hang the fleet — with nothing in the error logs.
2. **Derive timeouts from the flow budget:** if checkout owes 500ms and the payment call is one of two sequential dependencies, its read timeout is ~200-250ms, not the library default of 30s. A timeout is a latency budget enforced.
3. **Circuit breaker on critical dependencies:** closed → open after a failure threshold (count or rate) → fail fast while open → half-open probe → close on success. Failing fast returns in microseconds what a dying dependency would hold for seconds.
4. **Bulkheads:** dedicated connection/thread pools per critical dependency, so a slow vendor drains its own pool, not the shared one.
5. **Retry with exponential backoff + jitter,** capped attempts, only on idempotent operations — synchronized naive retries are a self-inflicted DDoS timed exactly to the dependency's worst moment.
6. **Steady state:** anything that accumulates (logs, temp files, sessions, cache entries, audit rows) gets a purge policy sized to survive between deploys.
7. **Make budgets operational:** RED metrics (Rate, Errors, Duration) per endpoint; deep health checks that touch real dependencies; alerts on symptoms the user feels (p95 over budget, error rate) rather than causes (CPU%). This is what turns Phase 1's snapshot into a permanent guardrail.

## §8 Lock in budgets and habits (The Pragmatic Programmer — Hunt & Thomas)

1. **CI perf gates.** Per hot path, the cheapest reliable check: a query-count assertion (fails when an N+1 regresses — cheap and deterministic), a micro-benchmark with a threshold, or a small load test in a nightly job with the p95 budget as the pass line. A budget without a gate decays into a wish.
2. **DRY for knowledge, not text.** The same business rule in two places (client and server validation, two services computing the same price) will drift; the same query issued from two layers doubles load and forks the fix. De-duplicate knowledge; leave coincidental look-alikes alone.
3. **Orthogonality check:** a dramatic change to one requirement (swap payment provider, change cache) should touch one module. Where it wouldn't, the Phase 2 interface is leaking — ledger it.
4. **Broken windows:** the first tolerated hack invites the second. Policy: fix now, or board it up with a tracked ticket — an untracked `// TODO: temporary` is a defect. Agree the debt budget per iteration (a fixed share of capacity) so paydown is routine, not an event.
5. **Reversibility:** every vendor and infrastructure choice behind an owned interface (the forking-road test: could you swap the DB, cache, or LLM provider in a week?). This is what keeps the *next* optimization cycle cheap.
6. **Write the conventions down** in TECH-DEBT.md Adopted Conventions — naming, where behavior lives, query rules (no queries outside repositories), the gate list — so the standard survives the people who set it.
