# Pilot router difficulty rubric — v1

Version: `v1`
Spec: Pilot router Rev 4 §6.2. This file is the binding, labeler-facing
instruction set for `label_corpus.py`. Its `sha256` is recorded in
`labels_meta.json` and flows into the T7 training manifest — **do not edit
in place after a labeling run; bump the version and re-hash instead.**

---

## Task

You assign one **reasoning-difficulty tier** to a single user chat message.
The tier estimates how much reasoning capability a model needs to produce a
**good** answer to that message.

Output tiers, cheapest capability first:

| Class | Tier | One-line meaning |
|---|---|---|
| `R0` | c0 | Trivial / social. No reasoning. A single-fact lookup or a pleasantry. |
| `R1` | c1 | Routine single-step task. Short writing, a simple code edit, a direct factual question. |
| `R2` | c2 | Multi-step reasoning. Non-trivial code, structured analysis, a several-step explanation. |
| `R3` | c3 | Deep / long-horizon reasoning. Complex architecture, formal proofs, large design work. |

---

## Rules (read every one — they override intuition)

1. **Judge the current message ALONE.** You are shown one message with no
   conversation history. Do not imagine prior turns. If the message *reads*
   as standalone, grade the standalone request. (The corpus is pre-filtered
   to self-contained turns, so referential fragments should be rare.)

2. **Grade the difficulty of a GOOD answer, not the length of the message.**
   A one-line message can be `R3` ("prove that every finite integral domain is
   a field"). A long message can be `R0` ("hey!! how are you doing today, hope
   you're well :) "). Length of the *prompt* is not evidence. Length/effort of
   a *correct answer* is.

3. **Grade the hardest thing the message actually asks for.** If a message
   asks for a simple thing and a hard thing, use the hard thing's tier.

4. **Do not reward or penalize topic, politeness, or domain.** A casual tone
   around a hard problem is still hard; formal phrasing around a trivial
   question is still trivial.

5. **When genuinely between two tiers, pick the LOWER tier.** The router pays
   for over-provisioning capability, so ties resolve down. (This does not
   apply when you are confident — only for real coin-flips.)

6. **Ignore whether YOU find it easy.** Grade the intrinsic difficulty for a
   competent model, not your own capability.

---

## Class definitions with anchors

### R0 — c0 — trivial / social

No reasoning required. Pleasantries, acknowledgements, or a single atomic
fact that is either common knowledge or a direct lookup with no synthesis.

**Positive anchors (label R0):**
- "thanks!"
- "what's 2+2"
- "hi, how are you?"
- "what is the capital of France"
- "lol that's funny"

**Negative anchors (NOT R0):**
- "what is the capital of France and why was it chosen over Lyon" → the *why*
  needs explanation → **R1**.
- "explain how binary search works" → a multi-step explanation → **R1/R2**,
  not a single fact.

### R1 — c1 — routine single-step task

One clear step. A direct factual question needing a short explanation; a short
piece of writing; a small, well-scoped code edit; a simple regex or formula.
A competent model produces a good answer in one pass with no branching plan.

**Positive anchors (label R1):**
- "write a regex for emails"
- "write a two-line thank-you note to my landlord"
- "what does the `git stash` command do"
- "convert this Celsius value 37 to Fahrenheit"
- "fix the off-by-one in this loop: `for i in range(len(a)+1):`"

**Negative anchors (NOT R1):**
- "write a regex for emails and explain each part, then handle
  internationalized domains" → multiple coupled steps → **R2**.
- "write a short story" with no other constraints → open-ended creative work
  that a good answer plans → treat as **R2** (see writing note below).

### R2 — c2 — multi-step reasoning

Several coupled steps, a non-trivial chain of reasoning, or code/analysis that
requires holding multiple constraints at once. A good answer benefits from an
internal plan but stays bounded (one function, one focused analysis, one bug).

**Positive anchors (label R2):**
- "debug this stack trace" (with a pasted trace)
- "write a Python function that merges overlapping intervals"
- "compare REST and GraphQL for a mobile app and recommend one"
- "explain the tradeoffs of optimistic vs pessimistic locking"
- "solve this system of three linear equations and show the steps"

**Negative anchors (NOT R2):**
- "what is optimistic locking" (definition only) → **R1**.
- "design the data model, sharding, and 3-year migration plan for our
  billing system" → long-horizon, many interacting subsystems → **R3**.

### R3 — c3 — deep / long-horizon reasoning

Complex architecture, formal proofs, research-level synthesis, or a task whose
good answer requires a long plan over many interacting parts and sustained
rigor. The failure mode of a weak model here is a plausible-but-wrong answer.

**Positive anchors (label R3):**
- "design a migration plan to move our monolith to microservices with zero
  downtime"
- "prove that the sum of the first n odd numbers is n²"
- "architect a multi-region, strongly-consistent key-value store and justify
  the consistency protocol"
- "derive the backpropagation equations for a two-layer MLP"

**Negative anchors (NOT R3):**
- "write a function to reverse a linked list" → bounded, single-step
  algorithm → **R2**.
- "what is a microservice" → single definition → **R1**.

---

## Boundary guidance

### R1 vs R2 — "one step" vs "several coupled steps"

Ask: *does a good answer need a plan, or can a competent model just write it
straight through?* If straight-through → R1. If it needs to reason across a
few coupled pieces (branching logic, multiple constraints, a non-obvious
algorithm) → R2.

Contrastive pairs:

| R1 | R2 |
|---|---|
| "write a function to check if a string is a palindrome" | "write a function to find the longest palindromic substring" |
| "what is a hash map" | "when would a hash map beat a balanced tree, and why" |
| "translate this sentence to French" | "translate this paragraph preserving the poem's meter and rhyme" |

### R2 vs R3 — "bounded" vs "long-horizon"

Ask: *is the answer one focused artifact, or a plan spanning many interacting
subsystems / a chain of rigorous deductions?* One bounded artifact → R2. Many
interacting parts, or sustained formal rigor where any misstep breaks the
result → R3.

Contrastive pairs:

| R2 | R3 |
|---|---|
| "write a rate limiter for one service" | "design org-wide rate limiting across 40 services with fairness guarantees" |
| "solve this quadratic equation" | "prove the quadratic formula from first principles" |
| "refactor this function to be pure" | "propose an architecture to make this monolith incrementally testable over 6 months" |

### Note on open-ended writing

Bare creative prompts ("write a story", "write a poem about the sea") have no
single correct answer; a *good* answer is planned and coherent. Grade these at
**R2** unless the message is trivially short/social (R0) or tightly scoped to a
tiny fixed form (e.g. "write a haiku about rain" → R1).

---

## Output contract

Reply with **only** a JSON object, no prose around it:

```json
{"label": "R0", "why": "<one short sentence justifying the tier>"}
```

`label` must be exactly one of `R0`, `R1`, `R2`, `R3`. `why` is a single
sentence (≤ 25 words) naming the deciding factor.
