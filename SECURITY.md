# Security Policy

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.
Report security issues privately via **GitHub Private Vulnerability Reporting** under the **Security** tab.

## Trading & Credential Safety

- Always start with `ALPACA_MODE=paper` until you have thoroughly validated your strategy and risk caps (`MAX_TRADING_CAPITAL`, `NOTIONAL_PER_TRADE`).
- Keep all brokerage (`ALPACA_KEY`, `ALPACA_SECRET`), AI (`ANTHROPIC_API_KEY`), and database credentials strictly inside `.env` (gitignored).
- Never expose your PostgreSQL database publicly without firewall rules and SSL.
