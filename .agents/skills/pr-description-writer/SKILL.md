---
name: pr-description-writer
description: Write a pull request description as exactly three concise bullets covering the problem, cause, and fix. Trigger on "write a PR description", "PR description", "describe this change", or similar requests.
---

# PR Description Writer

Write a pull request description as exactly three bullets, in this order:

1. **problem** — what went wrong or what needed improvement
2. **cause** — why the problem happened or what caused the limitation
3. **fix** — what changed to resolve it

## Rules

- Exactly three bullets. No more, no fewer.
- One bullet per section: problem, then cause, then fix.
- Do not prefix bullets with "Problem:", "Cause:", "Fix:", or any other label.
- Start each bullet directly with the content.
- Keep each bullet short, clear, and natural, ideally one sentence.
- Use simple developer-friendly language.
- Inspect the available changes or diff to understand the actual problem, cause, and fix.
- Do not invent details or assume an unverified root cause.
- No title, no intro line, no trailing explanation — only the three bullets.

## Example

- The collector memory was high and didn't go down
- The collector retained all time series without expiring them
- Updated the collector version to support per-series expiry and configured it

