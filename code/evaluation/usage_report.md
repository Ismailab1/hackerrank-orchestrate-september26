# Usage Report

Token usage and cost for the Anthropic API calls behind the submitted `output.csv`.

All extraction (Stage 2: `code/pipeline/extraction.py`) is cached in
`cache/extracted_facts.json` by source id. Producing `output.csv` itself
(Stages 1, 3, 4, 5, 6) makes **zero new LLM calls** -- every extracted fact
it consumes is served from that cache -- so the numbers below, from the
extraction run that populated it, are the real, final usage for this
submission, not an estimate.

| Model | Purpose | Calls | Input tokens | Output tokens | Cost |
| --- | --- | --- | --- | --- | --- |
| claude-haiku-4-5 | message extraction | 215 | 275,551 | 13,871 | $0.3449 |
| claude-sonnet-5 | image extraction | 16 | 47,987 | 1,468 | $0.1107 |
| **Total** | | **231** | **323,538** | **15,339** | **$0.4556** |

- Total tokens: 338,877
- Total estimated cost: $0.4556
- Requests in `output.csv`: 250
- Average tokens per request: 1355.5
- Average cost per request: $0.00182

Pricing: Claude Haiku 4.5 $1.00 / $5.00 per MTok (input/output); Claude
Sonnet 5 $2.00 / $10.00 per MTok. Provider: Anthropic, first-party API.
