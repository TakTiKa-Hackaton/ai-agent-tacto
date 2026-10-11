# Run the Taktichek dashboard with the agent

The `taktichek.html` interface can connect to the repository's `TaktikaAgent` through a small local Python web server. The server serves the page, lists available Arena cases, starts the existing autonomous `play()` loop, and streams supplier messages and safe agent-status updates into **Live view**. It records deal outcomes in a local SQLite database under `~/.taktichek/`; deal records and credentials are not written to the repository.

## Start it

From the repository root:

1. Copy `.env.example` to `.env` and set `ARENA_API_KEY` to your Arena key. Keep `.env` private; it is ignored by Git.
2. Install the project environment if needed: `uv sync --all-extras`.
3. Run `uv run python -m tactika_agent.web`.
4. Open [http://127.0.0.1:8765](http://127.0.0.1:8765).
5. Choose **New deal**, select a Sandbox case, and start it. Taktichek then researches the case, sends supplier messages, and closes or walks away according to the existing agent policy. Practice cases use the Arena practice quota.

The dashboard binds to `127.0.0.1` by default. `TAKTICHEK_HOST`, `TAKTICHEK_PORT`, and `TAKTICHEK_DB` can override the bind address, port, and local database path.

## What appears in Live view

The exchange panel shows the latest supplier email and Taktichek's sent response. The agent panel reflects the actual runtime: the coordinator, procurement researcher, deterministic offer planner, email composer, and the bounded commercial specialists when `LLM_COUNCIL_ENABLED=true` and its supported model credentials are configured. Specialist status cards display activity summaries and outcomes, not private model reasoning. If the optional council is disabled or a specialist result cannot be used, the deterministic planner remains the decision path.

The connected runner uses the Arena supplier simulator through `ArenaClient`; it does not connect to a company mailbox or send real supplier email. To use a real mail system, add an authenticated mail adapter to the Python service and retain the same event interface for the dashboard.

Opening `taktichek.html` directly or from GitHub keeps the standalone demo data. The live integration requires the local server because browser code never receives the Arena or model API keys.
