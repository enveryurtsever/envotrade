# Contributing to Envotrade

Contributions are welcome!

1. Open an issue first for major strategy, broker, or architecture changes.
2. Fork the repository and create a feature branch from `main`.
3. Test all trading logic in **paper trading mode** (`ALPACA_MODE=paper`) and run `tools/backtest.py` before opening a PR.
4. Never commit `.env`, API keys, broker credentials, or database dumps.
