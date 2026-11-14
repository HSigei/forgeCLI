---
description: Builds production-minded, full-featured web applications
  with high-end, concept-led visual design. Use when creating or
  upgrading SaaS products, dashboards, admin systems, marketplaces,
  collaboration platforms, portals, data tools, interactive learning
  products, or any browser-based software where users must complete real
  workflows. Combines product discovery, application architecture,
  frontend craft, backend integration, security, accessibility, testing,
  and premium visual direction. Do not treat an application as a
  marketing website or stop at attractive static screens.
name: awwwards-tier-web-application
---

# Awwwards-Tier Web Application Engineering

A product-building skill for applications that feel exceptionally
designed and work reliably. It preserves the visual ambition of high-end
creative websites while changing the unit of design from a page to a
complete product: user goals, workflows, data, permissions, states, and
dependable behavior.

## 0. North star

**Build software people can use, not screens people can admire.**

Every implementation must satisfy two independent standards:

1.  **Product integrity:** the core workflows function end-to-end; data,
    permissions, errors, and edge cases are handled honestly.
2.  **Design integrity:** the interface has a clear concept, a
    distinctive visual system, excellent hierarchy, considered motion,
    and production-level finish.

A beautiful interface with dead buttons fails. A functional interface
that looks like a generic admin template also fails. Aim for both, in
that order: establish correctness and product coherence, then refine the
visual expression without compromising usability.

## 1. Execution protocol

Follow these phases in order. For an existing project, inspect before
changing anything.

### Phase 1 --- Understand the product

Establish: - **Audience:** who uses the product, their experience level,
devices, and context. - **Jobs to be done:** what each user comes to
accomplish. - **Roles and permissions:** what each role can view,
create, edit, delete, approve, export, or administer. - **Primary
workflows:** the start, meaningful steps, success state, and
failure/recovery path for each important task. - **Domain objects:** the
real entities the product manages and how they relate. -
**Constraints:** existing stack, codebase conventions, integrations,
performance, compliance, budget, and deployment environment.

Do not invent a large product from a vague brief. Make reasonable,
reversible assumptions; state only the assumptions that materially
affect implementation. Ask a focused question only when a missing
decision blocks a safe or coherent build.

### Phase 2 --- Product concept gate

Before designing components, define: - **Product promise:** one sentence
describing the useful outcome. - **Primary user journey:** the most
important task the application must make easy. - **Signature
interaction:** one distinctive interaction that improves understanding
or use; two maximum. - **Visual idea:** a clear metaphor, material,
spatial system, or editorial principle that suits the product. -
**Design archetype:** choose one primary direction from Section 4. -
**Quality bar:** define what must work before the app can be called
complete.

The visual concept must support the product, not compete with it. An
event operations dashboard should prioritize speed and legibility; a
creative collaboration product can support richer expression. Do not
force cinematic motion, 3D, or dramatic loaders onto utilitarian
workflows.

### Phase 3 --- Audit the codebase

For existing repositories: 1. Inspect project structure, package
scripts, framework versions, routing, styling, data access,
authentication, and tests. 2. Read the relevant files before editing
them. 3. Identify working behavior that must be preserved. 4. Check for
existing components, tokens, utilities, and established conventions
before adding replacements. 5. Run the current app or tests when
practical to establish a baseline. 6. Record actual blockers and
distinguish them from assumptions.

Do not replace a functioning architecture merely to match a preferred
stack. Avoid broad rewrites when a targeted change is safer.

### Phase 4 --- Plan the system

Create a concise implementation plan covering: - Routes and
navigation. - Screens and reusable components. - Data entities and
relationships. - Authentication and authorization. - Server actions/API
endpoints and validation. - Loading, empty, success, error, offline, and
permission-denied states. - Integrations and environment variables. -
Tests for the core workflows.

For substantial products, define a small domain model and a workflow map
before building the interface. Keep planning proportional to project
size; do not bury a small app in paperwork.

### Phase 5 --- Build a vertical slice

Implement one complete, representative user journey from UI to
persistence before duplicating patterns across the application. For
example: sign in → create a record → validate → save → show confirmation
→ retrieve the saved record.

This proves the architecture early and exposes missing data or
permission decisions before they spread through the project.

### Phase 6 --- Expand by workflow

Build related capabilities in coherent increments. Each increment must
include: - Interface and interaction. - Real business logic. -
Server-side validation where appropriate. - Persistence or a clearly
declared temporary/mock boundary. - Access control. - User feedback and
recoverable errors. - Tests for the behavior introduced.

Do not mark a feature complete because its page exists.

### Phase 7 --- Design refinement

Once the main workflows are sound, apply the design system, typography,
spatial rhythm, interaction details, and purposeful motion in Sections
4--8. Refine the real interface and realistic data rather than a
disconnected showcase mockup.

### Phase 8 --- Verify and deliver

Run the app, lint/type-check/test commands available in the repository,
and test important flows in a browser when browser tooling is available.
Fix failures rather than hiding them. Report what was implemented, what
was tested, and any remaining limitations accurately.

------------------------------------------------------------------------

## 2. Product architecture and workflow integrity

### 2.1 Think in workflows, not isolated pages

For every major feature, answer:

1.  What triggers the workflow?
2.  What information does the user need?
3.  What can they do next?
4.  What validation applies?
5.  What changes in the system?
6.  What confirms success?
7.  What happens if the request fails, is repeated, or the user lacks
    permission?

Represent meaningful states explicitly. Avoid interfaces that only look
complete in their initial state.

### 2.2 Data and persistence

-   Model real entities and relationships rather than storing unrelated
    fields in oversized objects.
-   Use the existing database and ORM conventions unless there is a
    strong reason to change.
-   Validate data at trust boundaries, especially on the server.
-   Handle duplicate submissions and race conditions where they matter.
-   Use transactions for multi-step writes that must succeed or fail
    together.
-   Include timestamps, ownership, status, and audit history when the
    domain requires them.
-   Add pagination or sensible limits to potentially large collections.
-   Use migrations or repeatable schema setup; do not rely on
    undocumented manual database edits.
-   Never imply that data is saved when it exists only in component
    state or local mock data.
-   If a backend or external service is unavailable, isolate the mock
    behind a clear adapter and label it as a development limitation.

### 2.3 Authentication and authorization

-   Authentication answers **who is signed in**; authorization answers
    **what that person may do**.
-   Enforce permissions on the server or trusted data layer, not only by
    hiding buttons in the UI.
-   Protect private routes and resources, verify ownership, and scope
    queries by the authenticated user or organization.
-   Use established authentication libraries and secure session
    practices appropriate to the stack.
-   Never store passwords in plaintext, hardcode secrets, or expose
    server credentials in client bundles.
-   Include meaningful unauthorized, expired-session, and forbidden
    states.
-   For multi-tenant apps, test that one organization cannot access
    another organization's data.

### 2.4 API and integration quality

-   Use clear request/response contracts and consistent error formats.
-   Validate inputs and normalize data at boundaries.
-   Keep secrets server-side and use environment variables with
    documented examples.
-   Handle timeouts, rate limits, retries, and provider failures where
    relevant.
-   Avoid duplicate side effects on retries; use idempotency for
    sensitive operations such as payments or order creation.
-   Keep integrations behind small service modules so they can be tested
    and replaced.
-   Do not fabricate successful responses from payments, emails,
    uploads, or external APIs.

### 2.5 Maintainable code

-   Use components for meaningful reusable behavior, not for every
    trivial line.
-   Separate UI, domain rules, data access, and integration logic at a
    scale appropriate to the project.
-   Give variables, components, routes, and database fields
    domain-specific names.
-   Keep state as close as practical to where it is used; avoid
    unnecessary global state.
-   Avoid giant components, duplicated validation, hidden side effects,
    and speculative abstractions.
-   Follow the repository's conventions and framework version; verify
    APIs rather than guessing.
-   Keep dependencies purposeful and compatible. Do not install a
    library for a behavior that a small native implementation can safely
    handle.

------------------------------------------------------------------------

## 3. Application interface states

Every meaningful interactive feature must consider the states that real
users encounter.

### Required state checklist

-   **Initial:** clear default view and useful next action.
-   **Loading:** indicate progress without shifting the whole layout
    unnecessarily.
-   **Empty:** explain why nothing appears and how to create or discover
    content.
-   **Success:** confirm the result in context.
-   **Validation:** show the issue next to the relevant field and
    explain how to correct it.
-   **Failure:** use plain language, preserve user input where possible,
    and offer a safe recovery path.
-   **Disabled:** explain why an action is unavailable when the reason
    is not obvious.
-   **Permission denied:** do not reveal protected information; explain
    access limits appropriately.
-   **Long-running:** show progress or status and avoid duplicate
    submissions.
-   **Partial:** communicate what succeeded and what did not when a
    multi-part operation partially completes.
-   **Destructive:** clarify consequences and request confirmation when
    the action is consequential.
-   **Offline or stale:** when relevant, communicate connection or data
    freshness and protect unsaved work.

Use skeletons when the eventual layout is known and loading lasts long
enough to benefit. Use spinners for localized actions. Avoid decorative
loading screens that delay access to a working application.

### Forms

-   Use labels that remain visible; placeholders are examples, not
    labels.
-   Mark required fields and explain constraints before or at the point
    of entry.
-   Validate on the server even when client-side validation improves
    feedback.
-   Preserve values after recoverable errors.
-   Prevent accidental double submits and communicate pending state.
-   Use correct input types, autocomplete attributes, and
    mobile-friendly controls.
-   For complex forms, group fields by task and provide a clear
    progression.
-   Show actionable error messages rather than raw stack traces or vague
    "Something went wrong" messages.
-   Confirm destructive or irreversible actions; do not add confirmation
    dialogs to every harmless action.

### Tables and data-heavy interfaces

-   Optimize for scanning, comparison, and task completion.
-   Use clear column labels, sensible alignment, stable row actions, and
    meaningful empty states.
-   Provide search, filtering, sorting, and pagination when the amount
    of data warrants them.
-   Keep selected filters and pagination consistent with the visible
    result set.
-   Support keyboard access and small screens; use responsive layouts or
    intentional horizontal scrolling rather than shrinking text until it
    becomes unreadable.
-   Use bulk actions only when selection behavior and consequences are
    clear.
-   Distinguish statuses with text as well as color.
-   Never invent analytics, counts, ratings, or activity to make a
    dashboard feel populated.

### Navigation and information architecture

-   Organize navigation around user tasks and domain concepts.
-   Make the current location and active section obvious.
-   Use consistent naming for the same destination across menus,
    buttons, breadcrumbs, and page headings.
-   Support deep links and browser back/forward behavior.
-   Preserve context when opening details, editing records, or returning
    from a workflow.
-   Keep global navigation distinct from page-level actions.
-   For role-specific navigation, show relevant destinations without
    treating hidden navigation as security.

------------------------------------------------------------------------

## 4. High-end visual direction for software

Choose one primary archetype based on the product. The goal is a
distinctive, coherent interface, not the same dark glass dashboard for
every project.

  -----------------------------------------------------------------------
  Archetype               Best suited to          Visual language
  ----------------------- ----------------------- -----------------------
  **Quiet Precision**     Operations tools,       Restrained palette,
                          finance, analytics,     excellent typography,
                          admin systems           dense but calm layouts,
                                                  fine separators,
                                                  precise alignment

  **Editorial Workspace** Learning products,      Strong typographic
                          knowledge tools,        hierarchy, generous
                          research, publishing    reading width,
                                                  annotated metadata,
                                                  purposeful page rhythm

  **Warm Utility**        Community, education,   Approachable color,
                          wellness, collaboration human copy, clear
                                                  affordances, softer
                                                  surfaces without
                                                  childish styling

  **Technical Console**   Developer tools,        Monospaced metadata,
                          infrastructure,         dark or neutral
                          security, observability surfaces, clear status
                                                  signals, data-rich
                                                  layouts

  **Creative Studio**     Design tools,           Expressive type,
                          portfolios inside apps, flexible canvases,
                          media production        visual previews,
                                                  restrained motion,
                                                  strong content
                                                  presentation

  **Spatial Product**     Mapping, property, 3D   Spatial navigation,
                          editors, visual         canvas-based
                          exploration             interaction, clear DOM
                                                  controls, progressive
                                                  disclosure
  -----------------------------------------------------------------------

One primary archetype is the foundation. A second may provide a limited
accent. Do not mix unrelated visual languages without a reason rooted in
the product.

### 4.1 Concept before components

Define: - One-sentence visual concept. - One signature interaction at
most, unless the product genuinely benefits from two. - One reusable
visual motif, applied consistently across selected controls,
transitions, and feedback. - One hierarchy rule that makes important
information immediately apparent.

The product's content and task flow remain the main event. Effects are
there to clarify, reward, or orient the user.

### 4.2 Design tokens

Establish reusable tokens before styling many screens:

-   Semantic colors: background, surface, elevated surface, text, muted
    text, border, primary, accent, success, warning, danger, focus.
-   Light and dark themes if both are required.
-   Type families, sizes, weights, line heights, and tracking.
-   Spacing scale and layout widths.
-   Radii, border treatment, elevation, and overlays.
-   Motion durations and easing curves.
-   Layering and z-index conventions.
-   Focus-ring and interactive-state styles.

Use semantic tokens instead of scattered one-off values. Color must
carry meaning consistently. Maintain sufficient contrast and never
communicate state through color alone.

### 4.3 Typography and hierarchy

-   Choose typefaces that fit the product's personality and remain
    legible at working sizes.
-   Use expressive display typography selectively; dense operational
    interfaces need efficient scanning more than oversized headlines.
-   Establish a clear hierarchy for page title, section title, field
    label, body text, helper text, and metadata.
-   Use tabular numerals for changing metrics when useful.
-   Keep line lengths comfortable for reading and avoid low-contrast
    tiny labels.
-   Load fonts efficiently, provide fallbacks, and avoid layout shifts.

Do not automatically use Inter, gradients, glass, rounded cards, or
oversized headings. These are choices, not a design system.

### 4.4 Surface, spacing, and composition

-   Use whitespace to separate priorities, not merely to make the page
    sparse.
-   Align elements to a consistent grid and baseline.
-   Create hierarchy with spacing, type, and surface contrast before
    adding borders and shadows.
-   Reserve strong accent colors for important actions and meaningful
    states.
-   Use hairline borders, restrained elevation, and surface variation
    only where they help grouping.
-   Let the density match the job: an analytics console may be dense; an
    onboarding flow should be calm.
-   Use realistic domain content and believable record lengths to test
    the layout.
-   Design responsive behavior intentionally, not as a final shrinking
    pass.

### 4.5 Ban the generic AI-default look

Avoid: - The same sidebar + topbar + four gradient metric cards for
every product. - A landing-page hero pasted inside an authenticated
dashboard. - Glassmorphism, glow, blur, gradients, and oversized rounded
cards without a product-specific reason. - Decorative charts that
communicate no real information. - Fake user names, random percentages,
fabricated activity, and lorem ipsum in final interfaces. - Emoji in
place of purposeful interface icons. - Tiny low-contrast text, ambiguous
icon-only controls, and unlabeled form fields. - Motion on every
element, parallax throughout the app, and gratuitous 3D. - A different
radius, shadow, spacing, or button style on every screen. - An imitation
of a known product that copies its identity rather than learning from
its interaction principles.

The interface should feel authored because its decisions are coherent,
not because every element has an effect.

------------------------------------------------------------------------

## 5. Interaction and motion

Motion is part of usability. It should explain cause and effect,
preserve context, and provide feedback.

### 5.1 Motion budget

Use motion for: - State transitions and feedback. - Opening or closing
menus, dialogs, drawers, and command palettes. - Showing relationships
between list items and detail views. - Meaningful progress and
completion. - A single signature interaction where it improves the
product experience.

Keep routine actions fast. Avoid slowing repeated tasks for spectacle.
Do not force a cinematic loader between ordinary app routes.

### 5.2 Timing tokens

Example starting point; tune to the product:

``` css
:root {
  --ease-out-expo: cubic-bezier(0.16, 1, 0.3, 1);
  --ease-in-out-expo: cubic-bezier(0.87, 0, 0.13, 1);
  --ease-ui: cubic-bezier(0.32, 0.72, 0, 1);
  --duration-micro: 120ms;
  --duration-ui: 200ms;
  --duration-panel: 280ms;
  --duration-scene: 500ms;
}
```

Use consistent easing tokens instead of arbitrary timings. Most feedback
should be subtle and brief. Longer transitions belong to meaningful
context changes, not every hover.

### 5.3 Input and motion rules

-   Every interactive element needs hover, focus-visible, pressed,
    disabled, and pending states where applicable.
-   Never make essential information available only on hover.
-   Support keyboard, touch, and coarse-pointer devices.
-   Respect `prefers-reduced-motion`; reduce or remove nonessential
    movement.
-   Avoid animating layout-heavy properties when transform and opacity
    will work.
-   Avoid scroll hijacking in task-oriented software.
-   Use a single source of truth for state; do not let animation state
    drift from application state.
-   Avoid optimistic UI for sensitive operations unless rollback and
    error recovery are designed.
-   Keep focus predictable when dialogs, menus, and route transitions
    open or close.
-   Use transitions to clarify state, not conceal latency or errors.

### 5.4 High-end interaction details

Small, consistent details often make software feel premium: - A button
responds immediately and shows a clear pending state. - A row selection,
saved filter, or active tab is unmistakable. - A command palette
supports keyboard navigation and sensible empty results. - A drawer
retains context and closes predictably. - A successful save confirms the
change without interrupting the next task. - An undo action is offered
when it is safer than a confirmation dialog. - Motion uses the same
visual grammar across related components. - Empty states explain the
next useful action in the user's language.

Do not add custom cursors, sounds, or magnetic buttons to ordinary
business software unless they serve a clear purpose.

------------------------------------------------------------------------

## 6. Accessibility and responsive behavior

Accessibility is part of correctness, not a finishing touch.

-   Use semantic HTML and native controls when possible.
-   Make every workflow operable by keyboard.
-   Provide visible focus indicators and logical focus order.
-   Associate form labels, descriptions, and errors programmatically.
-   Give meaningful accessible names to icon-only buttons.
-   Use appropriate headings, landmarks, and dialog semantics.
-   Announce asynchronous success and error feedback when needed.
-   Ensure color contrast and non-color status indicators.
-   Avoid focus traps except inside modal contexts, and restore focus
    when a dialog closes.
-   Respect reduced motion and avoid flashing content.
-   Ensure touch targets are comfortably usable.
-   Test at narrow widths and zoomed text sizes.
-   Provide alternatives for canvas/WebGL interactions; essential
    actions and content must remain accessible in the DOM.
-   Do not assume that an accessibility overlay makes an inaccessible
    implementation compliant.

Responsive behavior must reflect the actual task. Tables can become
stacked records, compact views, or controlled horizontal scroll. Complex
navigation should become an accessible menu or drawer. Preserve
important actions rather than simply hiding them.

------------------------------------------------------------------------

## 7. Technical implementation tiers

Choose the simplest architecture that satisfies real product
requirements. Use the existing stack when it is sound.

### Tier 1 --- Interactive frontend prototype

Suitable for concept validation or a small local tool: - Componentized
UI. - Real local interaction and validation. - Clearly labeled sample
data. - A defined boundary between prototype state and persistence.

Do not describe this as production-ready if it has no real
authentication, persistence, server validation, or deployment hardening.

### Tier 2 --- Full-stack application

For most real products: - Frontend routes and reusable components. -
Backend endpoints, server actions, or equivalent application services. -
Persistent database with migrations. - Authentication and
server-enforced authorization. - Input validation and consistent error
handling. - Environment configuration. - Automated tests for domain
rules and critical workflows. - Logging appropriate to the deployment
environment.

### Tier 3 --- Complex or real-time product

Use only when requirements justify it: - Queues and background jobs. -
Realtime subscriptions or WebSockets. - File processing and object
storage. - Search indexing. - Audit logs and advanced permissions. -
Payment integrations and webhook verification. - Observability, rate
limiting, and operational controls. - Specialized rendering such as
WebGL for a genuinely spatial or visual product.

Do not introduce microservices, complex state machines, a queue, or
WebGL because they sound advanced. Prefer a modular monolith until real
requirements justify more complexity.

### Framework-specific discipline

For Next.js: - Confirm the installed Next.js version before using
APIs. - Respect the App Router or Pages Router already in use. - Keep
server-only code and secrets out of client components. - Use client
components only where browser interaction or client state requires
them. - Validate mutations on the server and revalidate or update
affected data deliberately. - Handle loading and error boundaries where
appropriate. - Do not mix incompatible routing patterns or guess at
library APIs.

For any stack: - Inspect `package.json`, lockfiles, scripts, and
existing architecture before adding dependencies. - Use the project's
package manager consistently. - Do not silently change frameworks or
major versions. - Keep configuration reproducible and document required
environment variables.

------------------------------------------------------------------------

## 8. Security, privacy, and reliability

Use secure defaults and tailor safeguards to the data and risk level.

-   Validate and authorize every sensitive server-side operation.
-   Use parameterized database queries or safe ORM APIs.
-   Protect against injection, cross-site scripting, cross-site request
    forgery where applicable, insecure direct object references, and
    unsafe file uploads.
-   Enforce file type, size, ownership, and storage rules on the server.
-   Rate-limit sensitive or abuse-prone endpoints where appropriate.
-   Verify payment and integration webhooks using the provider's
    signature mechanism.
-   Never trust client-supplied prices, roles, ownership, or permission
    claims.
-   Do not expose stack traces, secrets, tokens, or personal data in
    errors and logs.
-   Minimize collection and retention of personal data.
-   Avoid logging sensitive payloads.
-   Use safe redirect handling and secure cookie settings.
-   Keep dependencies updated within the project's compatibility
    constraints.
-   Add destructive-action protections and audit trails where the domain
    warrants them.
-   Treat privacy and security as product requirements, not optional
    polish.

Do not claim a security audit has passed without performing one.
Identify unverified assumptions and residual risks honestly.

------------------------------------------------------------------------

## 9. Performance and perceived quality

A premium interface must remain responsive on realistic devices and
networks.

-   Set performance budgets appropriate to the product and audience.
-   Load only the code and data needed for the current view.
-   Paginate or virtualize large collections when justified.
-   Use optimized images and responsive image sizes.
-   Lazy-load noncritical media and defer optional visual effects.
-   Avoid unnecessary client-side JavaScript and excessive state
    updates.
-   Prevent layout shifts from fonts, images, and asynchronous content.
-   Avoid repeated network requests caused by component lifecycle
    mistakes.
-   Cache only when invalidation and user-specific data boundaries are
    understood.
-   Profile actual bottlenecks before optimizing.
-   Provide useful feedback during slow operations instead of freezing
    the interface.
-   Keep motion smooth, but prioritize correctness and responsiveness
    over elaborate animation.

### 3D, canvas, and immersive features

Use them only when they are central to the product concept. - Keep
navigation, forms, text, and critical controls in semantic DOM. -
Provide static or lightweight alternatives for unsupported devices. -
Respect reduced motion and data-saving preferences where practical. -
Pause nonessential rendering when hidden or off-screen. - Compress
assets, warm up expensive rendering only when justified, and measure
startup cost. - Set mobile quality limits early. - Do not let a canvas
obscure keyboard focus or intercept unrelated input.

------------------------------------------------------------------------

## 10. Testing and verification

A product is not complete until its core behaviors have been checked.

### 10.1 Test layers

-   **Static checks:** lint, type-check, formatting checks where
    configured.
-   **Unit tests:** domain rules, validation, transformations,
    permission helpers.
-   **Integration tests:** database operations, service boundaries, API
    contracts.
-   **End-to-end tests:** primary user journeys in a browser.
-   **Manual exploratory checks:** responsive layouts, keyboard use,
    error states, visual consistency, and realistic data volume.

Do not add a test framework blindly. Inspect existing tooling and use
the smallest suitable setup.

### 10.2 Critical workflow test

For each core journey, verify: 1. The user can reach the feature. 2. The
correct inputs and choices are available. 3. Invalid input is rejected
with useful feedback. 4. Valid input produces the intended server-side
change. 5. The resulting data persists after refresh or navigation. 6.
The user sees the correct success or updated state. 7. Unauthorized
users cannot perform the operation or retrieve protected data. 8.
Failure does not silently lose work or falsely report success. 9.
Repeated submission does not create harmful duplicates. 10. The layout
remains usable on mobile and by keyboard.

### 10.3 Browser verification

When browser automation is available: - Start the app using the
documented command. - Inspect the real rendered page, not only the
source code. - Exercise real interactions and capture console/network
failures. - Test success, empty, loading, validation, and error
states. - Verify responsive behavior at representative widths. - Check
focus behavior in dialogs and menus. - Fix discovered issues and rerun
affected tests.

If browser tooling is unavailable, run all available checks and state
that visual end-to-end verification was not performed. Never claim tests
were run when they were not.

### 10.4 Definition of done

A feature is done only when: - The workflow is connected end-to-end. -
Data and permission rules are correct. - Important states and errors are
handled. - The UI matches the established design system. - Accessibility
and responsive behavior have been considered. - Relevant checks pass, or
remaining failures are explicitly documented.

------------------------------------------------------------------------

## 11. Content and product psychology

Use behavioral principles to reduce confusion and help people make
informed decisions, not to manipulate them.

-   Make the next useful action clear.
-   Reduce unnecessary choices and repeated data entry.
-   Keep terminology consistent with the user's mental model.
-   Use progressive disclosure for advanced controls.
-   Preserve context and user input across recoverable errors.
-   Make progress and consequences visible.
-   Prefer clear defaults while keeping meaningful choices reversible.
-   Use confirmation for high-impact decisions, not routine actions.
-   Avoid dark patterns, false urgency, hidden fees, misleading status,
    or coercive notification prompts.
-   Use realistic copy. Every message should help the user understand
    what happened or what to do next.

For educational, community, or decision-support products, distinguish
evidence from opinion and uncertainty. Do not present persuasive writing
or confident visual design as a substitute for factual reliability.

------------------------------------------------------------------------

## 12. Output and collaboration contract

When implementing a project, keep communication concise and useful.

Before work, provide a short plan when the task is substantial. Do not
narrate every internal step.

During implementation: - Make coherent, reviewable changes. - Preserve
existing behavior unless a change is requested or necessary. - Flag
blockers that genuinely need user input. - Do not replace real
functionality with placeholders to make progress appear faster. - Do not
silently omit requested requirements. - Do not invent credentials, API
responses, test results, or deployment success.

At delivery, summarize: 1. What was built or changed. 2. Which core
workflows now function. 3. The checks actually run and their results. 4.
Any setup steps or environment variables required. 5. Known limitations
or work that remains.

Do not claim "production-ready" merely because the app compiles. Use
precise descriptions such as "prototype," "locally tested," "end-to-end
tested for the listed flows," or "deployed," according to evidence.

------------------------------------------------------------------------

## 13. Pre-output checklist

### Product

-   [ ] Audience, roles, and primary jobs are understood.
-   [ ] Core workflows have clear success and failure paths.
-   [ ] The data model and persistence boundary are explicit.
-   [ ] Permissions are enforced beyond the UI.
-   [ ] No essential feature is represented by a dead button or fake
    success.

### Interface

-   [ ] One coherent product concept and visual direction are present.
-   [ ] Typography, color, spacing, surfaces, and states use shared
    tokens.
-   [ ] The interface is tailored to its actual workflow, not a generic
    dashboard template.
-   [ ] Loading, empty, validation, success, and error states are
    designed.
-   [ ] Copy and sample data are specific, realistic, and honest.
-   [ ] Mobile layouts and keyboard use have been considered.

### Craft

-   [ ] Motion has a clear purpose and respects reduced-motion settings.
-   [ ] Signature interactions do not obstruct everyday work.
-   [ ] Performance and asset cost are reasonable for target devices.
-   [ ] Canvas or 3D experiences have accessible fallbacks where needed.
-   [ ] Focus, contrast, labels, and touch targets are checked.

### Engineering

-   [ ] Existing code and framework version were inspected.
-   [ ] Server-side validation and authorization are implemented where
    needed.
-   [ ] Secrets and sensitive data are protected.
-   [ ] Relevant lint, type-check, tests, and browser checks were
    actually run.
-   [ ] Remaining failures and limitations are disclosed accurately.

**Final standard:** the application should feel deliberately designed,
behave consistently, protect its data, and help users complete real
work. Visual polish is mandatory; functional integrity is
non-negotiable.

------------------------------------------------------------------------

## 14. Working with the agent-skills engineering plugin

This skill owns **what** to build and **how it should look and feel**.
The `agent-skills` plugin (addyosmani/agent-skills, installed globally)
owns **how the engineering is done**. Use them together, phase by phase:

  Phase (this skill)            Pair with agent-skills
  ----------------------------- -----------------------------------------------
  1 Understand the product      `interview-me` only if the brief is truly vague
  2 Product concept gate        `idea-refine`, then `spec-driven-development`
                                (write the spec: concept + workflows + quality bar)
  Quality bar                   `constraint-driven-development` (definition of
                                done, performance and accessibility budgets)
  3 Audit the codebase          `context-engineering`, `doubt-driven-development`
                                for unfamiliar or high-stakes code
  4 Plan the system             `planning-and-task-breakdown`,
                                `api-and-interface-design`, `documentation-and-adrs`
                                (record stack and boundary decisions as ADRs)
  5 Vertical slice              `incremental-implementation` +
                                `test-driven-development`
  6 Expand by workflow          same pair, one workflow per increment;
                                `source-driven-development` for framework APIs
  7 Design refinement           `frontend-ui-engineering` (tokens, states,
                                accessibility) under Sections 4-6 of this skill
  8 Verify and deliver          `browser-testing-with-devtools` when a browser
                                is available, `code-review-and-quality`,
                                `security-and-hardening`, `performance-optimization`,
                                `code-simplification`, then `shipping-and-launch`
  Anything breaks               `debugging-and-error-recovery`
  Logging, metrics              `observability-and-instrumentation`
  Commits                       `git-workflow-and-versioning` (only when asked)

Rules when both are active:

-   agent-skills' operating behaviours (surface assumptions, push back,
    simplicity, scope discipline, verify-don't-assume) apply throughout.
-   When the two disagree, product integrity and security win over
    visual ambition; the simpler engineering option wins over the more
    impressive one unless the product concept needs it.
-   The spec written under `spec-driven-development` must contain this
    skill's Phase 2 outputs (promise, journey, signature interaction,
    visual idea, archetype, quality bar).
-   "Done" means both this skill's Section 10.4 and agent-skills'
    Definition of Done: tests pass, behaviour verified at runtime, docs
    updated, limitations reported honestly.
-   The slash commands map to phases: `/agent-skills:spec` (Phase 2),
    `/agent-skills:plan` (4), `/agent-skills:build` (5-6),
    `/agent-skills:test` (5-8), `/agent-skills:review` and
    `/agent-skills:ship` (8).
