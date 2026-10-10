CREATE TABLE IF NOT EXISTS metadata(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL,
  code TEXT NOT NULL, level TEXT NOT NULL, payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);

CREATE TABLE IF NOT EXISTS cycles(
  cycle_id TEXT PRIMARY KEY, started_at REAL NOT NULL, finished_at REAL,
  status TEXT NOT NULL, config_hash TEXT NOT NULL, reason TEXT
);

CREATE TABLE IF NOT EXISTS instruments(
  id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, venue TEXT NOT NULL,
  product_type TEXT NOT NULL, instrument_id TEXT NOT NULL, altname TEXT NOT NULL,
  base TEXT NOT NULL, quote TEXT NOT NULL, status TEXT NOT NULL,
  margin_available INTEGER NOT NULL, long_available INTEGER NOT NULL, short_available INTEGER NOT NULL,
  leverage_levels TEXT NOT NULL, min_order_qty TEXT NOT NULL, min_cost TEXT NOT NULL,
  lot_decimals INTEGER NOT NULL, price_decimals INTEGER NOT NULL, tick_size TEXT NOT NULL,
  margin_class TEXT NOT NULL, position_limits_json TEXT NOT NULL, collateral_json TEXT NOT NULL,
  funding_json TEXT NOT NULL, fee_model_json TEXT NOT NULL, metadata_json TEXT NOT NULL,
  updated_at REAL NOT NULL, UNIQUE(symbol,venue)
);

CREATE TABLE IF NOT EXISTS market_snapshots(
  id INTEGER PRIMARY KEY AUTOINCREMENT, captured_at REAL NOT NULL, symbol TEXT NOT NULL,
  price TEXT NOT NULL, bid TEXT NOT NULL, ask TEXT NOT NULL, volume_24h TEXT NOT NULL,
  age_seconds REAL NOT NULL, spread_bps TEXT NOT NULL, closes_json TEXT NOT NULL,
  depths_bid_json TEXT NOT NULL, depths_ask_json TEXT NOT NULL, funding_rate TEXT,
  open_interest TEXT, basis_bps TEXT, liquidation_pressure TEXT, features_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_market_symbol_time ON market_snapshots(symbol,captured_at);

CREATE TABLE IF NOT EXISTS market_history_cache(
  symbol TEXT PRIMARY KEY, captured_at REAL NOT NULL, closes_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS news_items(
  news_id TEXT PRIMARY KEY, published_at REAL NOT NULL, source TEXT NOT NULL, url TEXT NOT NULL,
  title TEXT NOT NULL, summary TEXT NOT NULL, topics_json TEXT NOT NULL, affected_assets_json TEXT NOT NULL,
  direction TEXT NOT NULL, impact_bps TEXT NOT NULL, novelty TEXT NOT NULL, credibility TEXT NOT NULL,
  horizon TEXT NOT NULL, market_confirmed INTEGER NOT NULL, raw_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS news_analysis(
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL, news_id TEXT NOT NULL,
  engine TEXT NOT NULL, result_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions(
  decision_id TEXT PRIMARY KEY, created_at REAL NOT NULL, symbol TEXT NOT NULL, direction TEXT NOT NULL,
  target_notional_eur TEXT NOT NULL, leverage TEXT NOT NULL, expected_return_bps TEXT NOT NULL,
  expected_cost_bps TEXT NOT NULL, confidence TEXT NOT NULL, regime TEXT NOT NULL,
  news_effect_bps TEXT NOT NULL, gemini_effect_bps TEXT NOT NULL, strategy_version TEXT NOT NULL,
  model_version TEXT NOT NULL, config_hash TEXT NOT NULL, rationale_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decision_checks(
  id INTEGER PRIMARY KEY AUTOINCREMENT, decision_id TEXT NOT NULL, created_at REAL NOT NULL,
  gate TEXT NOT NULL, allowed INTEGER NOT NULL, detail_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS predictions(
  prediction_id TEXT PRIMARY KEY, created_at REAL NOT NULL, decision_id TEXT NOT NULL,
  symbol TEXT NOT NULL, horizon TEXT NOT NULL, probability REAL NOT NULL,
  expected_return_bps TEXT NOT NULL, model_version TEXT NOT NULL, feature_hash TEXT NOT NULL,
  outcome_status TEXT NOT NULL, predicted_direction TEXT NOT NULL DEFAULT 'UNKNOWN',
  regime TEXT NOT NULL DEFAULT '', expected_cost_bps TEXT NOT NULL DEFAULT '0',
  raw_confidence REAL NOT NULL DEFAULT 0.5
);
CREATE TABLE IF NOT EXISTS prediction_outcomes(
  prediction_id TEXT PRIMARY KEY, measured_at REAL NOT NULL, realized_return_bps TEXT,
  success INTEGER, error_bps TEXT, detail_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders(
  intent_id TEXT PRIMARY KEY, client_order_id TEXT NOT NULL UNIQUE, created_at REAL NOT NULL,
  decision_id TEXT NOT NULL, symbol TEXT NOT NULL, direction TEXT NOT NULL, side TEXT NOT NULL,
  order_type TEXT NOT NULL, quantity TEXT NOT NULL, limit_price TEXT, leverage TEXT NOT NULL,
  margin INTEGER NOT NULL, reduce_only INTEGER NOT NULL, post_only INTEGER NOT NULL,
  state TEXT NOT NULL, submitted_at REAL, kraken_order_id TEXT, expected_edge_bps TEXT NOT NULL,
  max_slippage_bps TEXT NOT NULL, expires_seconds INTEGER NOT NULL, last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_symbol_created ON orders(symbol,created_at);

CREATE TABLE IF NOT EXISTS position_profit_state(
  symbol TEXT PRIMARY KEY,
  peak_profit_pct TEXT NOT NULL DEFAULT '0',
  partial_taken INTEGER NOT NULL DEFAULT 0,
  pending_order_id TEXT NOT NULL DEFAULT '',
  pending_start_position_eur TEXT NOT NULL DEFAULT '0',
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS order_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL, client_order_id TEXT NOT NULL,
  state TEXT NOT NULL, detail_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fills(
  order_id TEXT NOT NULL, trade_id TEXT NOT NULL, created_at REAL NOT NULL,
  symbol TEXT NOT NULL, side TEXT NOT NULL, quantity TEXT NOT NULL, price TEXT NOT NULL,
  fee TEXT NOT NULL, fee_currency TEXT NOT NULL, PRIMARY KEY(order_id,trade_id)
);

CREATE TABLE IF NOT EXISTS positions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, captured_at REAL NOT NULL, venue TEXT NOT NULL,
  symbol TEXT NOT NULL, direction TEXT NOT NULL, quantity TEXT NOT NULL, notional_eur TEXT NOT NULL,
  entry_price TEXT, mark_price TEXT, leverage TEXT NOT NULL, margin_used_eur TEXT NOT NULL,
  unrealized_pnl_eur TEXT NOT NULL, liquidation_distance_pct TEXT, detail_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS portfolio_positions(
  snapshot_id INTEGER NOT NULL, venue TEXT NOT NULL, symbol TEXT NOT NULL,
  direction TEXT NOT NULL, quantity TEXT NOT NULL, notional_eur TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS portfolio_snapshots(
  id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id TEXT NOT NULL, captured_at REAL NOT NULL,
  equity_eur TEXT NOT NULL, cash_eur TEXT NOT NULL, gross_eur TEXT NOT NULL, net_eur TEXT NOT NULL,
  margin_used_eur TEXT NOT NULL, unrealized_pnl_eur TEXT NOT NULL, realized_pnl_eur TEXT NOT NULL,
  daily_pnl_eur TEXT NOT NULL, drawdown_pct TEXT NOT NULL, positions_json TEXT NOT NULL, open_orders INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS learning_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL, event_type TEXT NOT NULL,
  entity_id TEXT NOT NULL, payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS model_versions(
  version TEXT PRIMARY KEY, family TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL,
  parent_version TEXT, parameters_json TEXT NOT NULL, metrics_json TEXT NOT NULL, reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS model_evaluations(
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL, version TEXT NOT NULL,
  dataset_hash TEXT NOT NULL, split TEXT NOT NULL, metrics_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS calibrations(
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL, model_version TEXT NOT NULL,
  sample_count INTEGER NOT NULL, brier_score REAL NOT NULL, ece REAL NOT NULL, bins_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL, model_version TEXT NOT NULL,
  dataset_hash TEXT NOT NULL, result_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recovery_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL, issue TEXT NOT NULL,
  state TEXT NOT NULL, detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS risk_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL, symbol TEXT,
  code TEXT NOT NULL, detail_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS health_snapshots(
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL, state TEXT NOT NULL,
  detail_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS error_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL, category TEXT NOT NULL,
  code TEXT NOT NULL, detail_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gemini_analysis(
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL, context_hash TEXT NOT NULL,
  model TEXT NOT NULL, result_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_permissions(
  id INTEGER PRIMARY KEY CHECK(id=1), checked_at REAL NOT NULL,
  permissions_json TEXT NOT NULL, ok INTEGER NOT NULL, detail TEXT NOT NULL
);


CREATE TABLE IF NOT EXISTS tax_events(
  event_id TEXT PRIMARY KEY, created_at REAL NOT NULL, timestamp REAL NOT NULL, tax_year INTEGER NOT NULL,
  venue TEXT NOT NULL, product_type TEXT NOT NULL, asset TEXT NOT NULL, quote_asset TEXT NOT NULL,
  event_type TEXT NOT NULL, quantity TEXT NOT NULL, proceeds_eur TEXT NOT NULL,
  acquisition_cost_eur TEXT NOT NULL, realized_gain_eur TEXT NOT NULL, fee_eur TEXT NOT NULL,
  fee_asset TEXT NOT NULL, tax_class TEXT NOT NULL, asset_regime TEXT NOT NULL, tax_neutral INTEGER NOT NULL,
  kest_withheld_eur TEXT NOT NULL, foreign_tax_eur TEXT NOT NULL, complete INTEGER NOT NULL,
  source TEXT NOT NULL, detail_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tax_events_year_time ON tax_events(tax_year,timestamp);

CREATE TABLE IF NOT EXISTS tax_reports(
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL, tax_year INTEGER NOT NULL,
  report_version TEXT NOT NULL, status TEXT NOT NULL, json_path TEXT NOT NULL,
  csv_path TEXT NOT NULL, markdown_path TEXT NOT NULL, summary_json TEXT NOT NULL
);


CREATE TABLE IF NOT EXISTS tactical_positions(
  symbol TEXT PRIMARY KEY,
  venue TEXT NOT NULL,
  direction TEXT NOT NULL,
  quantity TEXT NOT NULL,
  entry_price TEXT NOT NULL,
  peak_price TEXT NOT NULL,
  trough_price TEXT NOT NULL,
  notional_eur TEXT NOT NULL,
  leverage TEXT NOT NULL,
  opened_at REAL NOT NULL,
  last_update REAL NOT NULL,
  entry_client_order_id TEXT NOT NULL,
  setup_score TEXT NOT NULL,
  state TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tactical_positions_state ON tactical_positions(state);

CREATE TABLE IF NOT EXISTS tactical_trades(
  trade_id TEXT PRIMARY KEY,
  symbol TEXT NOT NULL,
  direction TEXT NOT NULL,
  entry_price TEXT NOT NULL,
  exit_price TEXT NOT NULL,
  quantity TEXT NOT NULL,
  gross_pnl_eur TEXT NOT NULL,
  fees_eur TEXT NOT NULL,
  net_pnl_eur TEXT NOT NULL,
  opened_at REAL NOT NULL,
  closed_at REAL NOT NULL,
  hold_seconds REAL NOT NULL,
  exit_reason TEXT NOT NULL,
  setup_score TEXT NOT NULL,
  detail_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tactical_trades_closed ON tactical_trades(closed_at);
