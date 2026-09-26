---
name: meta-stack-trace-investigator
description: "Investigate a pasted stack trace, traceback, panic, or exception: detect its source language and delegate to the matching stack-trace-*-probe helper for language-specific root-cause checks, a minimal reproducer, and patch targets, falling back to the generic probe when the language can't be told from the trace. Use when the user pastes or references a stack trace/traceback/panic and wants help finding what to check, how to reproduce it, and what to patch."
provenance:
  origin: agentos-original
  license: MIT
---

# Meta Stack Trace Investigator

Entry point for the `stack-trace-*-probe` family. Each probe is an internal
helper (`disable-model-invocation: true`, so it never appears in your own
skill menu) that returns a fixed-shape template — `LANGUAGE_PROBE`, `CHECKS`,
`REPRODUCER`, `PATCH_TARGETS`, `VERIFY` — scoped to one language's failure
modes. This skill is the only caller that reaches them.

## 1. Identify the language

| Signal in the trace                                                          | Probe                        |
| ------------------------------------------------------------------------------ | ------------------------------ |
| `Traceback (most recent call last):`, `.py` frames                           | `stack-trace-python-probe`   |
| `panic:` followed by `goroutine ... [running]:`, `.go` frames                | `stack-trace-go-probe`       |
| `thread '...' panicked at`, `.rs` frames                                     | `stack-trace-rust-probe`     |
| `at ... (<file>.js:<line>:<col>)` (Node/V8 stack), `.js`/`.ts`/`.tsx` frames  | `stack-trace-js-probe`       |
| Anything else, or the language can't be told from the trace alone            | `stack-trace-generic-probe`  |

## 2. Load the matching probe

Call `skill_view(name="<probe-name>")` for the probe selected above. It
returns the probe's template and language-specific guidance directly.

## 3. Fill in the probe's template

Fill every field — `CHECKS`, `REPRODUCER`, `PATCH_TARGETS`, `VERIFY` — from
the actual trace and surrounding context supplied, using the real file
paths, symbol names, and exception/panic message present. Do not invent
files, dependencies, or commands the request gives no basis for; each probe
repeats this as its own last line.

## Fallback

If `skill_view` reports a language-specific probe is not installed, fall
back to `stack-trace-generic-probe`. If that is unavailable too, proceed
with the same five-field template using general failure-contract reasoning
only.
