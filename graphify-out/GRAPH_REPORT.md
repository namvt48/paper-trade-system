# Graph Report - .  (2026-05-27)

## Corpus Check
- Corpus is ~25,798 words - fits in a single context window. You may not need a graph.

## Summary
- 542 nodes · 761 edges · 66 communities (32 shown, 34 thin omitted)
- Extraction: 80% EXTRACTED · 20% INFERRED · 0% AMBIGUOUS · INFERRED: 150 edges (avg confidence: 0.75)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Web Dashboard UI|Web Dashboard UI]]
- [[_COMMUNITY_Worker Database Layer|Worker Database Layer]]
- [[_COMMUNITY_MDS Aggregator Internals|MDS Aggregator Internals]]
- [[_COMMUNITY_Trade Execution Engine|Trade Execution Engine]]
- [[_COMMUNITY_Web API Routes|Web API Routes]]
- [[_COMMUNITY_MDS Aggregator & Data Feeds|MDS Aggregator & Data Feeds]]
- [[_COMMUNITY_Alpha Engine Framework|Alpha Engine Framework]]
- [[_COMMUNITY_Alpha Configuration|Alpha Configuration]]
- [[_COMMUNITY_Worker Main Loop|Worker Main Loop]]
- [[_COMMUNITY_MDS Models & Publisher|MDS Models & Publisher]]
- [[_COMMUNITY_MDS Kline Feed|MDS Kline Feed]]
- [[_COMMUNITY_Alpha Signal Processing|Alpha Signal Processing]]
- [[_COMMUNITY_MDS Ticker Feed|MDS Ticker Feed]]
- [[_COMMUNITY_Docker Infrastructure|Docker Infrastructure]]
- [[_COMMUNITY_Design Spec & Architecture|Design Spec & Architecture]]
- [[_COMMUNITY_ADX Alpha Runtime|ADX Alpha Runtime]]
- [[_COMMUNITY_Signal Models|Signal Models]]
- [[_COMMUNITY_Web Layout & Clock|Web Layout & Clock]]
- [[_COMMUNITY_Strategy Functions|Strategy Functions]]
- [[_COMMUNITY_Web App Icon|Web App Icon]]
- [[_COMMUNITY_Signal Parsing Helpers|Signal Parsing Helpers]]
- [[_COMMUNITY_Alpha Config Classes|Alpha Config Classes]]
- [[_COMMUNITY_Community 25|Community 25]]
- [[_COMMUNITY_Community 26|Community 26]]
- [[_COMMUNITY_Community 28|Community 28]]
- [[_COMMUNITY_Community 29|Community 29]]
- [[_COMMUNITY_Community 30|Community 30]]
- [[_COMMUNITY_Community 31|Community 31]]
- [[_COMMUNITY_Community 32|Community 32]]
- [[_COMMUNITY_Community 33|Community 33]]
- [[_COMMUNITY_Community 34|Community 34]]
- [[_COMMUNITY_Community 40|Community 40]]
- [[_COMMUNITY_Community 41|Community 41]]
- [[_COMMUNITY_Community 46|Community 46]]
- [[_COMMUNITY_Community 47|Community 47]]
- [[_COMMUNITY_Community 48|Community 48]]
- [[_COMMUNITY_Community 49|Community 49]]
- [[_COMMUNITY_Community 50|Community 50]]
- [[_COMMUNITY_Community 51|Community 51]]
- [[_COMMUNITY_Community 52|Community 52]]
- [[_COMMUNITY_Community 53|Community 53]]
- [[_COMMUNITY_Community 54|Community 54]]
- [[_COMMUNITY_Community 55|Community 55]]
- [[_COMMUNITY_Community 56|Community 56]]
- [[_COMMUNITY_Community 57|Community 57]]
- [[_COMMUNITY_Community 58|Community 58]]
- [[_COMMUNITY_Community 59|Community 59]]
- [[_COMMUNITY_Community 60|Community 60]]
- [[_COMMUNITY_Community 61|Community 61]]
- [[_COMMUNITY_Community 62|Community 62]]
- [[_COMMUNITY_Community 63|Community 63]]
- [[_COMMUNITY_Community 64|Community 64]]
- [[_COMMUNITY_Community 65|Community 65]]

## God Nodes (most connected - your core abstractions)
1. `Database` - 29 edges
2. `OpenSignal` - 17 edges
3. `BaseEngine` - 17 edges
4. `Reconciler` - 15 edges
5. `KlineCandle` - 15 edges
6. `KlineFeed` - 15 edges
7. `parse_signal()` - 14 edges
8. `process_signal_message()` - 14 edges
9. `TickerFeed` - 14 edges
10. `Executor` - 13 edges

## Surprising Connections (you probably didn't know these)
- `Publisher Test Suite` --references--> `Redis Service`  [INFERRED]
  market-data-service/tests/test_publisher.py → docker-compose.yml
- `Integration Test Suite` --references--> `Redis Service`  [INFERRED]
  market-data-service/tests/test_integration.py → docker-compose.yml
- `push_signal` --shares_data_with--> `process_signal_message`  [INFERRED]
  alphas/base/engine.py → worker/app/main.py
- `publish_kline` --shares_data_with--> `on_kline_message`  [INFERRED]
  market-data-service/app/publisher.py → alphas/base/engine.py
- `ADX Trend Follow Alpha Container` --shares_data_with--> `Redis Service`  [INFERRED]
  alphas/adx-trend-follow/docker-compose.yml → docker-compose.yml

## Hyperedges (group relationships)
- **Position Lifecycle (Open → Modify → Close)** — db_create_position, db_modify_position, db_close_position, db_get_trade, executor_process_open, executor_process_modify, executor_process_close [EXTRACTED 0.90]
- **SymbolData Shared State** — base_models_SymbolData, adx_engine_ADXTrendFollowEngine, adx_ws_manager_process_message, adx_market_data_load_initial_data [EXTRACTED 1.00]
- **Alpha Detail Data Aggregation Pattern** — page_AlphaDetailPage, db_getAlphaStats, db_getEquityCurve, db_getOpenPositions [EXTRACTED 1.00]
- **Redis Pub/Sub Signal Pipeline** — base_engine_push_signal, main_process_signal_message, publisher_Publisher [INFERRED 0.75]
- **Ticker Price to TPSL Check Pipeline** — main_TickerPriceCache, main_run_price_check_loop, executor_check_tpsl_hits [EXTRACTED 0.90]
- **Kline Aggregation and Publish Pipeline** — kline_feed_KlineFeed, aggregator_Aggregator, publisher_Publisher [EXTRACTED 0.90]
- **Aggregator→Publisher→Redis Data Flow** — test_aggregator_Aggregator, test_publisher_Publisher, test_integration_e2e [EXTRACTED 0.85]
- **Redis as Central Data Bus for All Services** — docker_compose_redis, docker_compose_market_data_service, docker_compose_worker, docker_compose_web [EXTRACTED 0.90]
- **Alphas Subscribe to Specific kline Channels** — docker_compose_adx_alpha, docker_compose_wilder_alpha, design_redis_pubsub_channels [EXTRACTED 0.85]

## Communities (66 total, 34 thin omitted)

### Community 0 - "Web Dashboard UI"
Cohesion: 0.06
Nodes (51): GET, DashboardPage(), ComparePage(), parseSelectedIds(), COLORS, CompareChart(), CompareChartProps, EquityChart() (+43 more)

### Community 1 - "Worker Database Layer"
Cohesion: 0.06
Nodes (17): Database, process_signal_message(), push_signal(), str, db(), integration_setup(), test_active_tpsl_close_on_sl_hit(), test_active_tpsl_close_on_tp_hit() (+9 more)

### Community 2 - "MDS Aggregator Internals"
Cohesion: 0.07
Nodes (18): Aggregator, _append_or_replace(), _trim(), KlineCandle, _differs(), Reconciler, _make_1m_candle(), test_aggregator_15m_rollup() (+10 more)

### Community 3 - "Trade Execution Engine"
Cohesion: 0.09
Nodes (31): Executor, CloseSignal, ModifySignal, OpenSignal, parse_signal(), SignalType, _to_float(), _to_int() (+23 more)

### Community 4 - "Web API Routes"
Cohesion: 0.1
Nodes (34): withRoute Wrapper, CompareChart Component, EquityChart Component, PositionCard Component, StatsPanel Component, TradeTable Component, getAllAlphas DB Query, getAlpha DB Query (+26 more)

### Community 5 - "MDS Aggregator & Data Feeds"
Cohesion: 0.07
Nodes (33): Aggregator, apply_correction, on_1m_close, _rollup, on_kline_message, subscribe_data_feeds, Executor, _apply_slippage (+25 more)

### Community 6 - "Alpha Engine Framework"
Cohesion: 0.12
Nodes (6): ABC, WilderEngine, BaseEngine, get_required_channels(), scan_loop(), SymbolData

### Community 7 - "Alpha Configuration"
Cohesion: 0.09
Nodes (10): ADXTrendFollowConfig, Settings, WilderConfig, BaseConfig, BaseConfig, BaseEngine, BaseSettings, engine() (+2 more)

### Community 8 - "Worker Main Loop"
Cohesion: 0.19
Nodes (14): configure_logging(), connect_redis(), get_symbol_universe(), _health_loop(), register_configured_alphas(), run_consumer(), run_health_loop(), run_price_check_loop() (+6 more)

### Community 9 - "MDS Models & Publisher"
Cohesion: 0.13
Nodes (6): TickerUpdate, Publisher, test_end_to_end_1m_candle(), test_end_to_end_ticker(), test_publish_kline(), test_publish_ticker()

### Community 11 - "Alpha Signal Processing"
Cohesion: 0.18
Nodes (8): ADXTrendFollowEngine, compute_adx(), compute_wilder_indicators(), determine_regime(), get_candle_seconds(), get_storage_size_for_tf(), strategy_filter_signal(), wilder_filter_signal()

### Community 12 - "MDS Ticker Feed"
Cohesion: 0.21
Nodes (6): TickerFeed, test_batch_symbols(), test_build_ticker_streams(), test_parse_binance_ticker(), test_parse_binance_ticker_non_ticker(), test_parse_binance_ticker_wrapped()

### Community 13 - "Docker Infrastructure"
Cohesion: 0.23
Nodes (14): ADX Trend Follow Alpha Container, Market Data Service Container, paper-trade Network, Redis Service, Web Container, Wilder Alpha Container, Worker Container, Market Data Service Dependencies (+6 more)

### Community 15 - "Design Spec & Architecture"
Cohesion: 0.17
Nodes (12): Aggregator 1m-to-Higher-TF Rollup Logic, Alpha Refactoring Strategy, Centralized Market Data Architecture, Correction Message Pattern, Impact Summary Before vs After, Multi-Exchange Extensibility, Reconciler Corrects WS Message Loss, Redis Pub/Sub Channel Schema (+4 more)

### Community 16 - "ADX Alpha Runtime"
Cohesion: 0.24
Nodes (11): ADXTrendFollowEngine, _manage_positions (ADX), scan_loop (ADX), _scan_new_signals (ADX), adx-trend-follow main, BaseEngine, push_signal, WilderEngine (+3 more)

### Community 17 - "Signal Models"
Cohesion: 0.4
Nodes (6): sample_close_signal fixture, sample_modify_signal fixture, sample_open_signal fixture, CloseSignal, ModifySignal, OpenSignal

### Community 19 - "Strategy Functions"
Cohesion: 0.4
Nodes (5): compute_adx, strategy_filter_signal, compute_wilder_indicators, determine_regime, wilder_filter_signal

### Community 22 - "Signal Parsing Helpers"
Cohesion: 0.67
Nodes (3): _to_float, _to_int, parse_signal

### Community 23 - "Alpha Config Classes"
Cohesion: 0.67
Nodes (3): ADXTrendFollowConfig, BaseConfig, WilderConfig

## Knowledge Gaps
- **98 isolated node(s):** `Return Redis Pub/Sub channels needed by this alpha.`, `Main signal scanning loop; call push_signal() when signals are found.`, `config`, `nextConfig`, `AlphaConfigValue` (+93 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **34 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `Publisher` connect `MDS Models & Publisher` to `Worker Main Loop`, `MDS Aggregator Internals`?**
  _High betweenness centrality (0.088) - this node is a cross-community bridge._
- **Why does `run_service()` connect `Worker Main Loop` to `MDS Models & Publisher`, `MDS Kline Feed`, `MDS Aggregator Internals`, `MDS Ticker Feed`?**
  _High betweenness centrality (0.066) - this node is a cross-community bridge._
- **Why does `KlineCandle` connect `MDS Aggregator Internals` to `MDS Models & Publisher`, `MDS Kline Feed`?**
  _High betweenness centrality (0.061) - this node is a cross-community bridge._
- **Are the 7 inferred relationships involving `Database` (e.g. with `TickerPriceCache` and `setup()`) actually correct?**
  _`Database` has 7 INFERRED edges - model-reasoned connections that need verification._
- **Are the 15 inferred relationships involving `OpenSignal` (e.g. with `Executor` and `test_process_open_signal()`) actually correct?**
  _`OpenSignal` has 15 INFERRED edges - model-reasoned connections that need verification._
- **Are the 5 inferred relationships involving `BaseEngine` (e.g. with `ADXTrendFollowEngine` and `BaseConfig`) actually correct?**
  _`BaseEngine` has 5 INFERRED edges - model-reasoned connections that need verification._
- **Are the 6 inferred relationships involving `Reconciler` (e.g. with `Aggregator` and `KlineCandle`) actually correct?**
  _`Reconciler` has 6 INFERRED edges - model-reasoned connections that need verification._