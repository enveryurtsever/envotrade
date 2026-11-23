-- ============================================================
-- Envorai Trading Terminal — PostgreSQL Schema
-- Idempotent: safe to run on every startup.
-- ============================================================

CREATE TABLE IF NOT EXISTS market_data (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL,
    symbol VARCHAR(10) NOT NULL,
    price NUMERIC(18, 8) NOT NULL,
    volume NUMERIC(18, 4),
    asset_class VARCHAR(10) NOT NULL DEFAULT 'crypto'
);
ALTER TABLE market_data ADD COLUMN IF NOT EXISTS asset_class VARCHAR(10) NOT NULL DEFAULT 'crypto';
CREATE INDEX IF NOT EXISTS idx_market_data_symbol_time
    ON market_data(symbol, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_market_data_asset
    ON market_data(asset_class, symbol, timestamp DESC);

CREATE TABLE IF NOT EXISTS market_sentiment (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    source VARCHAR(20) NOT NULL,
    symbol VARCHAR(10) NOT NULL,
    sentiment_score NUMERIC(3, 2),
    summary TEXT
);
CREATE INDEX IF NOT EXISTS idx_sentiment_symbol_time
    ON market_sentiment(symbol, timestamp DESC);

CREATE TABLE IF NOT EXISTS trading_signals (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    symbol VARCHAR(10) NOT NULL,
    rsi NUMERIC(5, 2),
    ai_decision VARCHAR(10),
    ai_confidence NUMERIC(5, 2),
    raw_ai_output TEXT,
    asset_class VARCHAR(10) NOT NULL DEFAULT 'crypto'
);
ALTER TABLE trading_signals ADD COLUMN IF NOT EXISTS asset_class VARCHAR(10) NOT NULL DEFAULT 'crypto';
CREATE INDEX IF NOT EXISTS idx_signals_asset
    ON trading_signals(asset_class, timestamp DESC);

CREATE TABLE IF NOT EXISTS orders (
    id SERIAL PRIMARY KEY,
    alpaca_order_id VARCHAR(100) UNIQUE,
    timestamp TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    symbol VARCHAR(10) NOT NULL,
    side VARCHAR(5) NOT NULL,
    qty NUMERIC(18, 8) NOT NULL,
    price NUMERIC(18, 8) NOT NULL,
    tp_price NUMERIC(18, 8),
    sl_price NUMERIC(18, 8),
    pnl NUMERIC(18, 8) DEFAULT 0.0,
    commission_paid NUMERIC(18, 8) DEFAULT 0,
    status VARCHAR(20),
    asset_class VARCHAR(10) NOT NULL DEFAULT 'crypto'
);
CREATE INDEX IF NOT EXISTS idx_orders_time
    ON orders(timestamp DESC);

-- Idempotent migration for existing installs (adds tp_price/sl_price if missing)
ALTER TABLE orders ADD COLUMN IF NOT EXISTS tp_price NUMERIC(18, 8);
ALTER TABLE orders ADD COLUMN IF NOT EXISTS sl_price NUMERIC(18, 8);
ALTER TABLE orders ADD COLUMN IF NOT EXISTS commission_paid NUMERIC(18, 8) DEFAULT 0;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS asset_class VARCHAR(10) NOT NULL DEFAULT 'crypto';
CREATE INDEX IF NOT EXISTS idx_orders_asset
    ON orders(asset_class, timestamp DESC);

CREATE TABLE IF NOT EXISTS bot_settings (
    key VARCHAR(50) PRIMARY KEY,
    value NUMERIC(10, 4) NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    description TEXT
);

-- Per-trade post-mortem lessons fired the MOMENT a position closes at a loss.
-- Real-time learning (vs the once-a-day learning_journal): each row is an
-- atomic rule extracted from a single losing trade's full context.
CREATE TABLE IF NOT EXISTS loss_lessons (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    symbol VARCHAR(10) NOT NULL,
    side VARCHAR(5),
    entry_ts TIMESTAMPTZ,
    exit_ts TIMESTAMPTZ,
    entry_price NUMERIC(18, 8),
    exit_price NUMERIC(18, 8),
    qty NUMERIC(18, 8),
    pnl NUMERIC(18, 8),
    rsi_at_entry NUMERIC(5, 2),
    avg_sentiment_at_entry NUMERIC(5, 3),
    sentiment_sources_at_entry INT,
    rule_learned TEXT NOT NULL,
    diagnosis TEXT,
    context_summary JSONB,
    asset_class VARCHAR(10) NOT NULL DEFAULT 'crypto'
);
ALTER TABLE loss_lessons ADD COLUMN IF NOT EXISTS asset_class VARCHAR(10) NOT NULL DEFAULT 'crypto';
CREATE INDEX IF NOT EXISTS idx_loss_lessons_recent
    ON loss_lessons(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_loss_lessons_symbol
    ON loss_lessons(symbol, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_loss_lessons_asset
    ON loss_lessons(asset_class, created_at DESC);

-- Daily self-learning journal: what worked, what didn't, what to change.
CREATE TABLE IF NOT EXISTS learning_journal (
    id SERIAL PRIMARY KEY,
    journal_date DATE NOT NULL UNIQUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    total_trades INT,
    wins INT,
    losses INT,
    total_pnl NUMERIC(18, 8),
    win_rate NUMERIC(5, 2),
    period_summary JSONB,            -- raw aggregate stats
    insights TEXT,                   -- AI-written narrative analysis
    rules_learned TEXT[],            -- atomic rules extracted for future trades
    parameter_changes JSONB,         -- which bot_settings were adjusted (key→old/new)
    confidence_calibration NUMERIC(5, 2),  -- did high-confidence trades actually win more?
    asset_class VARCHAR(10) NOT NULL DEFAULT 'crypto'
);
ALTER TABLE learning_journal ADD COLUMN IF NOT EXISTS asset_class VARCHAR(10) NOT NULL DEFAULT 'crypto';
-- Idempotent constraint swap: drop legacy single-date uniqueness, install composite.
-- We may have two coordinators (crypto + stocks) emitting a journal on the same day.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'learning_journal_journal_date_key'
    ) THEN
        ALTER TABLE learning_journal
            DROP CONSTRAINT learning_journal_journal_date_key;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'learning_journal_date_asset_uniq'
    ) THEN
        ALTER TABLE learning_journal
            ADD CONSTRAINT learning_journal_date_asset_uniq
            UNIQUE (journal_date, asset_class);
    END IF;
END$$;
CREATE INDEX IF NOT EXISTS idx_journal_date ON learning_journal(journal_date DESC);

INSERT INTO bot_settings (key, value, description) VALUES
    ('rsi_buy_threshold',       30.00,   'RSI <= this triggers BUY (with 15m uptrend filter)'),
    ('rsi_sell_threshold',      70.00,   'RSI >= this triggers SELL on existing long'),
    ('atr_period',              14.00,   'ATR averaging period (bars)'),
    ('atr_sl_mult',             1.50,    'Stop-loss = entry − this × ATR(14)'),
    ('atr_tp_mult',             3.00,    'Take-profit = entry + this × ATR(14) → 2:1 R:R'),
    ('trend_ema_15m_period',    50.00,   '15m-resampled EMA period for trend filter'),
    ('news_veto_sentiment_threshold', -0.50, 'Bearish sentiment threshold that triggers LLM news veto consultation'),
    ('news_veto_min_count',     3.00,    'Min bearish items before LLM news veto is called'),
    ('stop_loss_percent',       0.0100,  'Fallback SL when ATR unavailable (cold start)'),
    ('take_profit_percent',     0.0200,  'Fallback TP when ATR unavailable'),
    ('quick_profit_pct',        0.00,    'Force-close at this NET profit. 0 = disabled (preserves ATR R:R)'),
    ('min_hold_seconds',        30.00,   'Min seconds before quick-exit can fire (if enabled)'),
    ('max_trading_capital',     1000.00, 'Hard cap on aggregate open-position notional (USD)'),
    ('notional_per_trade',      100.00,  'USD per individual trade'),
    ('ai_confidence_threshold', 60.00,   'Legacy soft selectivity dial (auto-tuner adjusts based on WR)'),
    ('tweet_max_age_min',       30.00,   'Ignore tweets older than this many minutes'),
    ('news_max_age_min',        120.00,  'Ignore news older than this many minutes'),
    ('starting_equity',         100000.00, 'Baseline Alpaca equity for BUDGET P&L computation'),
    ('alpaca_commission_pct',   0.0015,  'Per-side commission Alpaca charges on crypto')
ON CONFLICT (key) DO NOTHING;
