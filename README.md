# Taktika Buyer Agent

Taktika is a standalone procurement agent for the Tacto Negotiation Arena. It uses the arena API and
published total-cost model, then adds its own case research, offer planning, and conservative email
writer. By default, Python chooses the supplier, terms, acceptance, and walk-away. An optional
specialist council can propose bounded term changes and assess rejection risk; Python still validates
every proposed term sheet against the case rules and the best alternative.

All arena-facing Python code lives in this repository: the HTTP client, attachment reader, cost
calculator, session runner, research, and offer planner. Runtime dependencies are general-purpose
HTTP, PDF, spreadsheet, and environment-variable libraries; no negotiation starter package is
installed or imported.

## Install

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env
```

Set `ARENA_URL` and `ARENA_API_KEY` in `.env`. Keep that file private. The bundled research package
is in `data/`. Relative `ARENA_DATA` and `ARENA_RELEASE` paths resolve from this project directory,
even when you launch the CLI from elsewhere; if `ARENA_DATA` is missing or invalid, the agent falls
back to the bundled package.

## Run

```bash
uv run taktika me
uv run taktika cases --set practice
uv run taktika --verbose play case:P01 --record ./runs
uv run taktika run --set practice --parallel 8
uv run taktika run --set practice --limit 6 --parallel 1
uv run taktika analyze ./runs --replay
```

`play` and `run` automatically save a private JSON trace for each session and a timestamped result
summary under `./runs`. The console prints one consolidated score line. Pass `--record DIR` to use a
different directory. Traces include case data, supplier messages, planner research inputs, decisions,
and Arena results; keep them local. `analyze` reads these files without connecting to the Arena.
It reports recorded rejection forecasts only when one selected package maps to the response, shows
ambiguous bundles separately, breaks out score and deal rate by category goal, and summarizes council
failure causes. `--replay` compares decisions with the current planner; it does not simulate supplier
responses or estimate a new score.

For the live final, start the runner before the first wave and leave it running:

```bash
uv run taktika run --set final --parallel 10
```

The runner uses the arena's replay and session machinery, including attachment extraction, API
retry handling, final wave deadlines, and safe case resumption. A practice run consumes the team's
practice quota.

## How the agent works

1. **Research:** joins same-part, same-unit purchase orders, RFQ quotes, should-cost estimates, and
   supplier scorecards. It also calculates cost-weighted market index changes and keeps only data
   IDs the case or local package can verify.
2. **Plan:** sets a target from the current purchase price and market movement, adjusts it for the
   category goal and same-part competitor quotes, and prices complete term sheets with the arena's
   cost function. Each supplier is compared with the best alternative excluding that supplier.
3. **Negotiate:** sends a staged concession curve and up to three concrete packages. Each package
   must satisfy every must-have and stay within the alternative's total cost. Valid standing offers
   are accepted once they meet target or reach their final round; otherwise the agent walks away
   after every supplier conversation is exhausted.
4. **Write:** produces a short, supplier-aware email. With no model configured, the built-in writer
   works without a paid API. If configured, an OpenAI-compatible model drafts prose only; messages
   containing numbers or commercial thresholds are rejected in favor of the built-in wording.

### Optional specialist council

Set `LLM_API_KEY`, `LLM_COUNCIL_ENABLED=true`, and `LLM_COUNCIL_MODEL=gpt-6-luna` to let independent
money, payment, delivery, quality, contract, market/index, relationship, and evidence/alternatives
specialists advise one rejection-risk specialist and one decision boss. Set the council model to
`gpt-4o` for that model instead. Only terms owned by the matching specialist are considered. The
boss can select at most three packages; Python rejects unsupported options, broken must-haves, and
offers beyond the current earned cost limit. Existing agreed offers and walk-away decisions stay
deterministic.

GPT-6 Luna is the default because this workflow makes several narrow model calls per decision and
OpenAI Docs positions Luna for focused, high-volume tasks. Current standard token rates are `$0.10`
input / `$0.50` output per million tokens for Luna versus `$2.50` / `$10` for GPT-4o. This is a cost
choice, not a measured quality win; compare both on the same practice cases before switching models
for real negotiations ([Luna](https://developers.openai.com/api/docs/models/gpt-6-luna),
[GPT-4o](https://developers.openai.com/api/docs/models/gpt-4o),
[pricing](https://developers.openai.com/api/docs/pricing)).

The rejection specialist separately estimates acceptance, counteroffer, and supplier walk-away
probabilities. It marks confidence as low, medium, or high; low-confidence, malformed, timed-out, or
over-budget advice falls back to the deterministic planner. Probabilities are not calibrated against
the small available negotiation history. The trace stores specialist findings, risk estimates,
the boss's reason, and council API cost. The council uses the existing safe email wording.

`LLM_API_BUDGET_USD` caps council API calls for one running process (default `$2`, hard maximum `$5`,
using the bundled published-rate estimates).
The process-wide ledger reserves the maximum request cost before each call and charges the full
reservation if usage is not returned. Use `LLM_API_BUDGET_USD=unlimited` only for explicit model
evaluation runs. Normal runs retain the finite default. Per-request usage, latency, finish reason and
failure causes are saved to private traces.
Only GPT-6 Luna and GPT-4o have built-in budget rates. The council falls back if another model is
configured.

Relationship review runs for every category goal. It can flag evidenced concerns, previous commitments,
and mitigations; it does not invent a reputation score or override the case's commercial constraints.
The Arena may evaluate behavior on undisclosed cases and criteria. The offline 60-case test matrix checks
planner behavior across category goals, market moves, and comparable quote profiles; it is synthetic
coverage, not a supplier simulation or proof of hidden-score performance.

For optional model drafting, set `LLM_PROVIDER=openai-compatible`, `LLM_MODEL=gpt-6-luna`, and
`LLM_API_KEY` in `.env`; optionally set `LLM_BASE_URL` for another OpenAI-compatible endpoint.
Without a model API key, the built-in writer is used. The model does not choose terms or accept offers.

## MCP email tools

Taktika also exposes the Arena's supplier-email workflow as an MCP server over stdio. Start it from
the project directory after configuring `.env`:

```bash
uv run taktika-mcp
```

Register it with an MCP client using its project folder as the working directory. For clients that
use a JSON server configuration, the entry is:

```json
{
  "mcpServers": {
    "taktika-arena": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/ai-agent-tacto", "run", "taktika-mcp"]
    }
  }
}
```

The server provides tools to list cases, start a negotiation, read supplier emails, ask Taktika to
draft its next email, send an email with up to three term sheets, and accept or walk away. Drafting
does not send anything; `send_negotiation_email` and `close_negotiation` submit actions to the Arena
when called. Starting a practice negotiation uses one case from the team's practice quota. These are
Arena negotiation emails, not a personal Gmail or Outlook inbox. For a retry after a timed-out MCP
call, pass the same `operation_id` to `start_negotiation`, `send_negotiation_email`, or
`close_negotiation`; use a new ID when intentionally repeating an operation. The operation journal
is stored under `TAKTIKA_STATE_DIR` or `~/.taktika-buyer-agent` and contains local request/result data.

## Data and attribution

The research files are provided by Tacto Technology GmbH under CC BY 4.0. Their attribution and
source licences are retained in [LICENSE-data](LICENSE-data) and [data/ATTRIBUTION.md](data/ATTRIBUTION.md).
The agent code is MIT licensed; see [LICENSE](LICENSE).
The research snapshot is synthetic company data with public market series, and should be refreshed
from the arena before a new event when updated data is available.
