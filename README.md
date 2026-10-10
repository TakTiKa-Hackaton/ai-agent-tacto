# Taktika Buyer Agent

Taktika is a standalone procurement agent for the Tacto Negotiation Arena. It uses the arena API and
published total-cost model, then adds its own case research, offer planning, and conservative email
writer. Its decisions stay reproducible: Python chooses the supplier, benchmarks, complete term
sheets, acceptance, and walk-away. An optional language model can only draft the email prose.

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
is in `data/`; set `ARENA_DATA` if you keep it elsewhere. A separately downloaded Sunday release can
be supplied with `ARENA_RELEASE`.

## Run

```bash
uv run taktika me
uv run taktika cases --set practice
uv run taktika play case:P01 --verbose
uv run taktika run --set practice --parallel 8
```

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

For optional model drafting, set `LLM_MODEL`, `LLM_API_KEY`, and optionally `LLM_BASE_URL` in `.env`.
The model does not choose terms or accept offers.

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
Arena negotiation emails, not a personal Gmail or Outlook inbox.

## Data and attribution

The research files are provided by Tacto Technology GmbH under CC BY 4.0. Their attribution and
source licences are retained in [LICENSE-data](LICENSE-data) and [data/ATTRIBUTION.md](data/ATTRIBUTION.md).
The agent code is MIT licensed; see [LICENSE](LICENSE).
The research snapshot is synthetic company data with public market series, and should be refreshed
from the arena before a new event when updated data is available.
