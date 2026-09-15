# SETUP

Workspace layout (Python 3.12+, per CLAUDE.md):

```
packages/shared           chalybcrypto-shared        pydantic models, enums, fees, config
services/engine           chalybcrypto-engine        strategy + risk + backtest + paper
services/api              chalybcrypto-api           FastAPI app (/api/health)
services/telegram_ingest  chalybcrypto-telegram-ingest  Telegram signal parser
connectors                chalybcrypto-connectors    Bitunix + Binance-data + LBank
```

## First-time setup

```powershell
# 1. venv (use Python 3.12+; the repo also runs on 3.14)
py -3.12 -m venv .venv   # or py -3.14 -m venv .venv
.venv/Scripts/python.exe -m pip install --upgrade pip setuptools wheel

# 2. editable installs of every workspace package
.venv/Scripts/python.exe -m pip install `
  -e packages/shared `
  -e services/engine `
  -e services/api `
  -e services/telegram_ingest `
  -e connectors

# 3. test deps
.venv/Scripts/python.exe -m pip install `
  pytest pytest-asyncio pytest-postgresql `
  "pydantic>=2.7,<3" "pydantic-settings>=2.3,<3" `
  "httpx" "fastapi>=0.111,<1" "uvicorn[standard]>=0.30,<1" `
  "websockets>=12,<14" "psycopg[binary]"
```

After step 2 you can run any of the workspace packages as `-m`:

```powershell
.venv/Scripts/python.exe -m chalybcrypto_connectors.bitunix.capture BTCUSDT depth_books
.venv/Scripts/python.exe examples/run_backtest.py
```

## Running the test suite

```powershell
.venv/Scripts/python.exe -m pytest          # all
.venv/Scripts/python.exe -m pytest tests/risk -v
```

Postgres is required for the RLS suite (3 tests in `tests/test_rls.py`); other 167
pass without it. To install Postgres locally without Docker:

```powershell
scoop install postgresql
pg_ctl -D $env:USERPROFILE\scoop\apps\postgresql\current\data `
       -l $env:USERPROFILE\scoop\apps\postgresql\current\pg.log start
```

## docker-compose

```powershell
docker compose up -d redis postgres   # infrastructure only
docker compose up -d api              # FastAPI app on :8000
docker compose ps
```

## Try things

| What | How |
|---|---|
| Backtest BTCUSDT on Binance public klines | `python examples/run_backtest.py` |
| Capture Bitunix WS depth_books shape | `python -m chalybcrypto_connectors.bitunix.capture BTCUSDT depth_books --count 3` |
| Health check | `curl http://localhost:8000/api/health` |
| Parse a Telegram message | see [tests/test_telegram_parser.py](tests/test_telegram_parser.py) |

## File paths cheat-sheet

| Area | Path |
|---|---|
| Strategy ABC + indicators | [services/engine/src/chalybcrypto_engine/strategy/](services/engine/src/chalybcrypto_engine/strategy/) |
| Risk engine | [services/engine/src/chalybcrypto_engine/risk/](services/engine/src/chalybcrypto_engine/risk/) |
| Backtester | [services/engine/src/chalybcrypto_engine/backtest/](services/engine/src/chalybcrypto_engine/backtest/) |
| Paper engine | [services/engine/src/chalybcrypto_engine/paper/](services/engine/src/chalybcrypto_engine/paper/) |
| Bitunix connector | [connectors/src/chalybcrypto_connectors/bitunix/](connectors/src/chalybcrypto_connectors/bitunix/) |
| Supabase migrations | [supabase/migrations/](supabase/migrations/) |
| Telegram parser | [services/telegram_ingest/src/chalybcrypto_telegram_ingest/](services/telegram_ingest/src/chalybcrypto_telegram_ingest/) |
