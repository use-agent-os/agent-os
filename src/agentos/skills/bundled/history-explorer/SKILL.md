---
name: history-explorer
description: "Query the per-turn DecisionEntry log for skill co-occurrence patterns and the router fixture corpus. Returns a JSON summary suitable for downstream LLM consumption. Useful for 'which skills did I use most this week?'"
provenance:
  origin: agentos-original
  license: MIT
metadata:
  requires:
    anyBins: ["python", "python3"]
entrypoint:
  command: python {baseDir}/scripts/explore.py
  args:
    - --query
    - "{{ with.query | truncate(512) }}"
    - --window-days
    - "{{ with.window_days | default('30') }}"
    - --include
    - "{{ with.include | join(',') if with.include is sequence and with.include is not string else with.include | default('co_occurrences,router_fixtures') }}"
    - --top-k
    - "10"
  parse: json
  timeout: 30
---

# History Explorer

Lightweight read-only view over `~/.agentos/logs/decisions-*.jsonl`. Aggregates `DecisionEntry.skills_invoked` (SCHEMA_VERSION 10) into co-occurrence frequencies and surfaces the router fixture corpus (when present).

## Usage

```
uv run python {baseDir}/scripts/explore.py \
  --log-dir ~/.agentos/logs \
  --query "Co-occurring chains for PDF workflows" \
  --window-days 30 \
  --include co_occurrences,router_fixtures \
  --top-k 10
```

## Output

JSON to stdout with keys `co_occurrences`, `router_fixtures`, and a `placeholder` string when the log is empty.

## Fallback

If no decision-log exists, return an empty result with a placeholder string explaining "no history; downstream should rely on user intent only".
