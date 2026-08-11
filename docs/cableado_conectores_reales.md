# Guía paso a paso: cablear las implementaciones reales

Esta guía explica cómo sustituir los **mocks/stubs** por las integraciones reales del
sistema. Cada sección es independiente: puedes cablear un conector sin tocar los demás,
porque todos comparten una interfaz (`Protocol`) y se inyectan por dependencia.

> **Principio general**: cada conector real ya tiene un *stub* con la firma correcta.
> "Cablear" significa **rellenar el cuerpo** de ese stub (o su implementación) y
> **seleccionarlo** en el arranque en lugar del mock. Mantén siempre los mocks: son los
> que permiten ejecutar los tests offline.

## Índice

1. [Preparación común](#0-preparación-común)
2. [IBKR read/write (ib_async / MCP)](#1-ibkr-readwrite-ib_async--mcp)
3. [APIs de noticias y redes](#2-apis-de-noticias-y-redes)
4. [Bot de Telegram (HITL)](#3-bot-de-telegram-hitl)
5. [LLM real (OpenAI / Anthropic / Azure)](#4-llm-real-openai--anthropic--azure)
6. [Persistencia: Postgres/TimescaleDB, Qdrant, Redis Streams](#5-persistencia-postgrestimescaledb-qdrant-redis-streams)
7. [Buscadores (LLM + datos reales)](#6-buscadores-llm--datos-reales)
8. [Checklist final](#7-checklist-final)

---

## 0. Preparación común

### 0.1 Instalar los extras

```bash
# En la máquina con acceso a PyPI (o índice interno con --index-url)
pip install -e ".[connectors,llm,infra,indicators]"
```

Extras definidos en `pyproject.toml`:

- `connectors` → `ib-async`, `python-telegram-bot`, `httpx`
- `llm` → `openai`, `anthropic`
- `infra` → `redis`, `psycopg[binary]`, `qdrant-client`, `apscheduler`, `prometheus-client`
- `indicators` → `pandas`, `pandas-ta` (indicadores del buscador técnico; **opcional**: si
  falta, `src/agents/searchers/indicators.py` degrada a una implementación pura en Python)

### 0.2 Configurar variables de entorno

```bash
cp .env.example .env
```

Rellena `.env` con las credenciales (ver cada sección). **Nunca** subas `.env` al repo.
En producción usa Vault / AWS Secrets Manager (ver `infra/k8s/secret.example.yaml`).

### 0.3 Levantar la infraestructura local

```bash
docker compose up -d postgres redis qdrant
```

---

## 1. IBKR read/write (ib_async) ✅ (validado en runtime paper)

Ficheros: `src/connectors/ibkr_read.py` y `src/connectors/ibkr_write.py`.

> **Ambos conectores validados en runtime** contra IB Gateway paper (puerto 4002).
> `IBKRMarketDataClient` expone: `connect`, `get_portfolio`, `get_quote`,
> `is_market_open`, `is_tradable_today`, `get_ohlcv`, `get_premarket` y
> `get_top_movers` (scanner `TOP_PERC_GAIN/LOSE` sin consumir pacing de históricos).
> Incluye manejo robusto del **Error 162** — ver sección 1.6.

### 1.1 Requisitos

- **IB Gateway** o **TWS** corriendo y logueado (cuenta paper recomendada al inicio).
  - Puerto `4002` = paper, `4001` = live.
  - En *Global Configuration → API → Settings*: activar *Enable ActiveX and Socket
    Clients* y añadir `127.0.0.1` (o la IP del contenedor) a *Trusted IPs*.
- Variables en `.env`:

```bash
IBKR_HOST=127.0.0.1          # o ib-gateway si usas el contenedor
IBKR_PORT=4002
IBKR_CLIENT_ID_READ=10
IBKR_CLIENT_ID_WRITE=11
IBKR_ACCOUNT=DU1234567
```

### 1.2 Scanner de movers (`get_top_movers`) ✅

Usa `reqScannerDataAsync` (no consume pacing de históricos). Configuración por mercado:

| Mercado | `instrument` | `locationCode` | Suscripción requerida |
|---------|-------------|----------------|----------------------|
| NYSE/NASDAQ | `STK` | `STK.US.MAJOR` | Market data US (incluida) |
| BME (IBEX35) | `STOCK.EU` | `STK.EU.BM` | **Bolsa de Madrid Plus (Nivel 1)** |

> El scanner BME **exige** `instrument="STOCK.EU"` (no `"STK"`). Sin la suscripción
> europea activa en la cuenta IBKR aparece Warning 492 + Error 162/365.

```python
# config/config.yaml — cross_market searcher
searchers:
  cross_market:
    movers_count: 25          # por mercado (NYSE + BME)
    min_move_pct: 0.5
    sector_ttl_hours: 24
```

### 1.3 Lectura histórica (`get_ohlcv`, `get_premarket`) ✅

Ya implementado. Patrón usado como referencia:

```python
# Referencia del patrón de get_ohlcv
raw = await asyncio.wait_for(
    self.ib.reqHistoricalDataAsync(
        contract, endDateTime="",
        durationStr=_duration_for(bars, interval),
        barSizeSetting=interval,
        whatToShow="TRADES", useRTH=True,
    ), timeout=30.0
)
```

> `get_premarket` hace 2 llamadas por ticker (cierre del día anterior + OHLCV extendido).
> `get_ohlcv` hace 1 llamada. El pacing de IBKR (60 req históricos / 10 min) se respeta
> con lotes rotativos en los searchers (`technical` batch≤ 50, `fundamental` batch ≤ 35).

### 1.4 Verificar la escritura (`IBKRBrokerClient`) ✅

Ya implementado y conectado. `Stock(symbol, "SMART", "EUR")` para BME y
`Stock(symbol, "SMART", "USD")` para US. Prueba siempre en **paper** antes de `LIVE`.

### 1.5 Probar la conexión

```python
import asyncio
from src.connectors.ibkr_read import IBKRMarketDataClient

async def main():
    c = IBKRMarketDataClient(host="127.0.0.1", port=4002, client_id=10)
    await c.connect()
    print(await c.get_portfolio())
    print(await c.get_quote("AAPL"))

asyncio.run(main())
```

### 1.6 Error 162 «different IP address» — manejo robusto ✅

El Error 162 `Trading TWS session is connected from a different IP address` indica que
otra sesión IBKR (app móvil, Client Portal, otro TWS) está consumiendo el feed de
datos históricos. El sistema lo maneja en tres capas:

1. **Cortacircuitos (`_on_ib_error`)**: al detectar el 162 de IP, activa una ventana de
   90 s durante la cual `_ensure_feed_available()` falla instantáneamente en
   `get_ohlcv`, `get_quote`, `get_premarket` y `get_index_ohlcv`.
   Evita ~40 s perdidos por ciclo reintentando ticker a ticker.
2. **Diagnóstico único**: emite un solo `WARNING agentic.connectors.ibkr` con la causa
   y la acción a tomar; no se repite hasta que pasa la ventana.
3. **Filtro de ruido (`_IBWrapperNoiseFilter`)**: instalado en el logger `ib_async.wrapper`,
   colapsa los ERRORs por contrato a 1 mensaje / 90 s. El 162 de cierre normal del
   scanner (`API scanner subscription cancelled`) **no se filtra**.

**Solución de raíz**: cierra la app móvil IBKR, el Client Portal y cualquier otro
TWS/Gateway usando las mismas credenciales. Para operación permanente usa un
**usuario dedicado** en el Gateway.

### 1.7 Inyectarlo en el sistema ✅ (implementado)

La selección es automática vía `src/connectors/factory.py`:

- `resolve_trading_mode(config)`: prioridad a la env `TRADING_MODE`; si no,
  `config.system.trading_mode` (por defecto `PAPER`).
- `build_market_data_client(config)`: `IBKRMarketDataClient` en `PAPER`/`LIVE`;
  `MockMarketDataClient` en otro modo o si `ib_async` no está instalado (fallback con aviso).
- `build_broker_client(config)`: `IBKRBrokerClient` en `PAPER`/`LIVE`; `MockBrokerClient` en
  otro caso.
- Parámetros de conexión leídos de `.env`: `IBKR_HOST`, `IBKR_PORT` (default 4002 paper /
  4001 live), `IBKR_CLIENT_ID_READ` (10), `IBKR_CLIENT_ID_WRITE` (11), `IBKR_ACCOUNT`.

En `src/app/main.py` la `Application`:

- Construye `self.market_data` y `self.broker` con la fábrica e inyecta el broker en
  `Executor`; crea también el `Monitor`.
- `_connect_clients()` conecta ambos conectores en `start()` (degrada con aviso si el
  Gateway no responde, sin abortar).
- `_execute()` arma el `MarketContext` con el market data y llama al ejecutor; se pasa como
  `execute_fn` a `pipeline.arun`, pero **solo se dispara con aprobación HITL**
  (`state.approved`), respetando el principio "no-operar por defecto".

| `TRADING_MODE` | Market data | Broker |
|---|---|---|
| `PAPER` / `LIVE` | `IBKRMarketDataClient` | `IBKRBrokerClient` |
| otro valor o sin `ib_async` | `MockMarketDataClient` | `MockBrokerClient` |

> Para desarrollo offline sin Gateway, define `TRADING_MODE` a un valor distinto de
> `PAPER`/`LIVE` para forzar los mocks.

---

## 2. APIs de noticias y redes ✅ (implementado)

Ficheros: `src/connectors/news_apis.py` (`HttpNewsClient`) y
`src/connectors/social_apis.py` (`HttpSocialClient`).

**Implementación:**

- `HttpNewsClient.fetch` consulta **NewsAPI**, **Marketaux**, **Finnhub** y **Alpha Vantage**
  con `httpx.AsyncClient` (import perezoso), normaliza cada fuente a `NewsItem` y **aísla el
  fallo por fuente** (una API caída no tumba al resto). Solo se consultan las fuentes con
  clave presente.
- `HttpSocialClient.fetch` consulta **Reddit** (OAuth client-credentials sobre los
  subreddits `wallstreetbets`, `investing`, `stocks`) y **StockTwits** (API pública por
  símbolo), normalizando a `SocialPost` con extracción de cashtags (`$AAPL`) y sentimiento
  básico de StockTwits.
- **Alternativa RapidAPI** (`RapidApiSocialClient`): una sola clave `RAPIDAPI_KEY` activa
  dos backends sin necesidad de OAuth propio:
  - **Socialgrep** (`socialgrep.p.rapidapi.com`): búsqueda de posts en Reddit por cashtag.
  - **Finance Social Sentiment** (`finance-social-sentiment-for-twitter-and-stocktwits.p.rapidapi.com`):
    sentimiento de StockTwits y Twitter por símbolo.
- Selección real/mock en `src/connectors/factory.py`: `build_social_client()` prioriza
  `RapidApiSocialClient` si hay `RAPIDAPI_KEY`; si no, `HttpSocialClient` (Reddit OAuth +
  StockTwits); si no hay ninguna clave, `HttpSocialClient` con StockTwits público (limitado).
- Inyección en los searchers: `NewsSearcher`/`SocialSearcher` reciben el cliente en el
  constructor y consumen el feed en `search()` (guardado en `last_items`/`last_posts`). La
  conversión de feed a `Opportunity` vía LLM queda pendiente de la fase LLM, así que por
  ahora los buscadores obtienen el feed pero **no fabrican oportunidades** (no-operar por
  defecto). El cableado se hace en `src/app/main.py` (`build_searchers`).
- **Robustez (`src/connectors/_http.py`)**: todas las llamadas HTTP pasan por
  `request_json`, que aplica **reintentos con backoff exponencial + jitter** ante errores
  transitorios (429 rate-limit, 5xx, timeouts). Ademas hay **cache opcional en Redis**
  (`FeedCache` + `cached_json`) con TTL corto para deduplicar llamadas y respetar los
  limites de las APIs. Ambos **degradan a no-op** si falta la dependencia o el servicio:
  sin `REDIS_URL`/paquete `redis` la cache se desactiva; los mocks no hacen red. Ajustable
  por entorno: `FEEDS_HTTP_MAX_ATTEMPTS`, `FEEDS_HTTP_BASE_DELAY`, `FEEDS_HTTP_MAX_DELAY`,
  `FEEDS_CACHE_TTL`, `REDIS_URL`.

### 2.1 Claves en `.env`

```bash
NEWSAPI_KEY=...
MARKETAUX_KEY=...
FINNHUB_KEY=...
ALPHAVANTAGE_KEY=...

# Opción A — RapidAPI (recomendada; reemplaza Reddit OAuth + StockTwits):
RAPIDAPI_KEY=...          # una sola clave activa Socialgrep (Reddit) + Finance Sentiment (StockTwits/Twitter)

# Opción B — APIs directas (solo si no usas RapidAPI):
REDDIT_CLIENT_ID=...
REDDIT_SECRET=...
STOCKTWITS_TOKEN=...      # opcional; sin token usa la API publica (limitada por IP)
```

### 2.2 Implementar `HttpNewsClient.fetch`

Usa `httpx.AsyncClient` y normaliza cada fuente a `NewsItem` (`src/schemas/feeds.py`).
Ejemplo con NewsAPI + Finnhub:

```python
# src/connectors/news_apis.py
import httpx
from datetime import datetime, timedelta, timezone

class HttpNewsClient:
    def __init__(self, api_keys: dict[str, str], timeout: float = 10.0) -> None:
        self.api_keys = api_keys
        self._timeout = timeout

    async def fetch(self, tickers=None, since_minutes=30) -> list[NewsItem]:
        items: list[NewsItem] = []
        async with httpx.AsyncClient(timeout=self._timeout) as http:
            # --- NewsAPI ---
            if key := self.api_keys.get("newsapi"):
                q = " OR ".join(tickers or ["stock market"])
                r = await http.get("https://newsapi.org/v2/everything",
                                   params={"q": q, "apiKey": key, "pageSize": 50,
                                           "from": (datetime.now(timezone.utc)
                                                    - timedelta(minutes=since_minutes)).isoformat()})
                r.raise_for_status()
                for a in r.json().get("articles", []):
                    items.append(NewsItem(source="newsapi", headline=a["title"],
                                          url=a.get("url"), tickers=tickers or []))
            # --- Finnhub company-news (por ticker) ---
            if (key := self.api_keys.get("finnhub")) and tickers:
                for tk in tickers:
                    r = await http.get("https://finnhub.io/api/v1/company-news",
                                       params={"symbol": tk.split('.')[0], "token": key,
                                               "from": "...", "to": "..."})
                    for a in r.json():
                        items.append(NewsItem(source="finnhub", headline=a["headline"],
                                              url=a.get("url"), tickers=[tk]))
        return items
```

> **Buenas prácticas**: respeta los *rate limits* (añade backoff con `tenacity`), cachea
> respuestas en Redis con TTL corto, y captura excepciones por fuente para que el fallo de
> una API no tumbe el resto.

### 2.3 Implementar `HttpSocialClient.fetch`

Mismo patrón para Reddit (OAuth con `REDDIT_CLIENT_ID`/`REDDIT_SECRET`, endpoint
`https://oauth.reddit.com/r/<sub>/new`) y StockTwits. Normaliza a `SocialPost`.

### 2.4 Inyectar en los searchers ✅ (implementado)

Ya cableado en `src/app/main.py`: `build_news_client`/`build_social_client` construyen el
cliente real (o mock) y `build_searchers` los inyecta en `NewsSearcher`/`SocialSearcher`.
Estos buscadores ya no devuelven `[]`: formatean el feed y razonan oportunidades con el
LLM (ver **sección 6. Buscadores**).

---

## 3. Bot de Telegram (HITL) ✅ (implementado)

Fichero: `src/connectors/telegram_bot.py` (`TelegramHITLClient`).

**Implementación:**

- `TelegramHITLClient` (python-telegram-bot v21, asíncrono): `start()`/`stop()` gestionan
  el ciclo de vida (polling); `request_approval()` envía a cada usuario autorizado un
  mensaje con teclado inline (Aprobar / Rechazar / Pausar 1h) y **espera la respuesta con
  TTL**. Si expira → `TIMEOUT` (no operar).
- `_on_callback` valida que `update.effective_user.id` esté en `authorized_users` antes de
  resolver el `Future`; ignora pulsaciones de usuarios no autorizados.
- Selección real/mock en `src/connectors/factory.py`: `build_hitl_client()` usa
  `TelegramHITLClient` si hay `TELEGRAM_BOT_TOKEN` **y** `TELEGRAM_AUTHORIZED_USERS`; si no
  (o si falta el paquete), `MockHITLClient` (que devuelve `TIMEOUT` = no operar).
- Cableado en `src/app/main.py`: el bot se arranca en `start()` (`_start_hitl`) y se cierra
  en `_shutdown`; `_approve` se pasa como `approval_fn` a `pipeline.arun` y la ejecución
  solo se dispara con `APPROVE`. `PAUSE_1H` activa el kill switch 1h (auto-reanudación).

### 3.1 Crear el bot y obtener credenciales

1. Habla con **@BotFather** en Telegram → `/newbot` → copia el **token**.
2. Obtén tu **chat/user id** (p. ej. con **@userinfobot**).
3. Configura `.env`:

```bash
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
TELEGRAM_AUTHORIZED_USERS=12345,67890
```

### 3.2 Implementar `request_approval`

Envía un mensaje con teclado inline (Aprobar / Rechazar / Pausar 1h) y **espera la
respuesta con TTL**. Si expira → `TIMEOUT` (no operar). Esqueleto:

```python
# src/connectors/telegram_bot.py
import asyncio
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application as TgApp, CallbackQueryHandler

class TelegramHITLClient:
    def __init__(self, bot_token: str, authorized_users: list[int]) -> None:
        self.bot = Bot(bot_token)
        self.authorized_users = set(authorized_users)
        self._pending: dict[str, asyncio.Future] = {}

    def _keyboard(self, request_id: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Aprobar",  callback_data=f"approve:{request_id}"),
            InlineKeyboardButton("❌ Rechazar", callback_data=f"reject:{request_id}"),
            InlineKeyboardButton("⏸ Pausar 1h", callback_data=f"pause:{request_id}"),
        ]])

    async def request_approval(self, request: ApprovalRequest) -> ApprovalResponse:
        text = (f"*{request.ticker}* — score {request.final_score:.2f}\n"
                f"Expectancy {request.expectancy_pct:.2f}% | R/R {request.risk_reward_ratio:.2f}\n"
                f"SL {request.stop_loss} | TP {request.take_profit}\n{request.summary}")
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[str(request.request_id)] = fut
        for uid in self.authorized_users:
            await self.bot.send_message(uid, text, parse_mode="Markdown",
                                        reply_markup=self._keyboard(str(request.request_id)))
        try:
            decision = await asyncio.wait_for(fut, timeout=request.ttl_seconds)
        except asyncio.TimeoutError:
            decision = ApprovalDecision.TIMEOUT
        return ApprovalResponse(request_id=request.request_id,
                                opportunity_id=request.opportunity_id,
                                decision=decision, responder="telegram")
```

> Necesitas además un *handler* (`CallbackQueryHandler`) que resuelva el `Future`
> correspondiente cuando un usuario **autorizado** pulsa un botón, validando
> `update.effective_user.id in self.authorized_users`. Arranca el `TgApp` en `start()`.

### 3.3 Inyectar en el pipeline

Usa el `request_approval` como `approval_fn` de `pipeline.arun(...)`:

```python
hitl = TelegramHITLClient(token, users)
async def approval_fn(decision, opp):
    resp = await hitl.request_approval(build_request(decision, opp))
    return resp.approved
```

---

## 4. LLM real (OpenAI / Anthropic / Azure) ✅ (implementado)

Ficheros: `src/llm/providers.py` y `src/graph/factory.py` (selección).

> El factory **ya** construye el cliente real si hay API key, y cae a `MockLLMClient`
> si no. Solo necesitas claves y activar el motor LLM.

**Implementación (esta fase):**

- **Carga de `.env`**: nuevo `src/utils/env.py` (`load_env()`), invocado en
  `src/app/main.py` (`_amain`) **antes** de construir los factories. Sin esto, los
  `os.getenv` de LLM/conectores/IBKR/Telegram no veían las claves del `.env`.
- **Azure OpenAI**: `OpenAIClient` acepta `azure_endpoint`/`api_version` y usa
  `AsyncAzureOpenAI`; en Azure el "modelo" es el **deployment**. `build_llm_client`
  (en `src/graph/factory.py`) lee `AZURE_OPENAI_API_KEY`/`AZURE_OPENAI_ENDPOINT`/
  `AZURE_OPENAI_DEPLOYMENT`/`OPENAI_API_VERSION`. Para OpenAI estándar admite
  `OPENAI_BASE_URL` (gateway compatible).
- **Scripts de prueba**: `scripts/test_llm.py` (llamada de prueba en JSON) y
  `scripts/test_telegram_bot.py` (bot HITL).

### 4.1 Claves y modelo en `.env` / `config.yaml`

```bash
OPENAI_API_KEY=sk-...
# o ANTHROPIC_API_KEY=...
```

```yaml
# config/config.yaml  (configuración real en producción paper)
llm:
  provider: deepseek              # deepseek | openai | azure_openai | anthropic
  reasoning_model: deepseek-v4-flash
  tool_model: deepseek-v4-flash
  temperature: 0.2
  evaluation_engine: llm          # usa LLMEvaluator con arun()
```

> `deepseek-v4-flash` es el modelo confirmado en runtime (responde 200 OK, latencia ~1-3 s).

### 4.2 Azure OpenAI

Ya soportado. En `config/config.yaml` pon `llm.provider: azure_openai` y en `.env`:

```bash
AZURE_OPENAI_API_KEY=...
AZURE_OPENAI_ENDPOINT=https://<recurso>.openai.azure.com
AZURE_OPENAI_DEPLOYMENT=<nombre-del-deployment>   # se usa como "modelo"
OPENAI_API_VERSION=2024-06-01
```

`build_llm_client` detecta el proveedor Azure, usa `AsyncAzureOpenAI` y toma el deployment
como modelo. Si falta el endpoint, cae a `MockLLMClient` con aviso.

### 4.3 Probar

Forma rápida (script incluido):

```powershell
python scripts/test_llm.py
```

O programáticamente:

```python
import asyncio
from src.graph.factory import build_llm_client
from src.utils.config import get_config

async def main():
    c = build_llm_client(get_config())
    print((await c.complete("Eres un evaluador.", "Di OK en JSON {\"ok\":true}",
                            json_mode=True)).text)

asyncio.run(main())
```

Con `evaluation_engine: llm`, `src/app/main.py` usará automáticamente `pipeline.arun()`
con los `LLMEvaluator`. Las trazas deben guardarse en `llm_traces` (ver sección 5).

---

## 5. Persistencia: Postgres/TimescaleDB, Qdrant, Redis Streams ✅ (implementado)

**Implementación (esta fase):**

- **PostgreSQL** (`src/persistence/postgres.py`): `PostgresRepository` con `insert`/`all`/
  `count` sobre psycopg 3 (JSONB via `Jsonb`), hereda los helpers tipados de
  `RepositoryHelpers` (extraídos en `repository.py`), por lo que es reemplazo directo de
  `InMemoryRepository`. `init_schema()` crea las 8 tablas append-only.
- **Redis Streams** (`src/messaging/streams.py`): `RedisStreamBus` con `publish` (serializa
  a campo `json`), `read` con grupos de consumidor (XREADGROUP + creación con MKSTREAM) y
  `ack`. `ping()` al construir para degradar si Redis no responde.
- **Qdrant** (`src/memory/vector_store.py`): `QdrantVectorStore` crea la colección (coseno),
  `upsert`/`search` con `query_points`; ids arbitrarios → UUID determinista (`original_id`
  en el payload).
- **Cableado** (`src/app/main.py`): `build_repository()` y `build_message_bus()` eligen la
  implementación real según `POSTGRES_DSN`/`REDIS_URL`, con degradación a memoria si el SDK
  o el servicio no están disponibles.
- **Script de prueba**: `scripts/test_persistence.py` (Postgres + Redis + Qdrant).

> Detalle de referencia (DDL, snippets) más abajo.


### 5.1 PostgreSQL / TimescaleDB

Fichero: `src/persistence/postgres.py` (`PostgresRepository`).

1. **Levanta** la BD: `docker compose up -d postgres`.
2. **Crea el esquema** (el DDL ya está en `SCHEMA_DDL`):

```python
from src.persistence.postgres import PostgresRepository
repo = PostgresRepository(dsn="postgresql://postgres:postgres@localhost:5432/agentic")
repo.init_schema()   # crea las 8 tablas append-only
```

3. **Implementa `insert` y `all`** con `psycopg` 3. Como las tablas guardan `payload
   JSONB`, mapea el dict del modelo:

```python
import json, psycopg

# Columna JSONB principal por tabla.
_PAYLOAD_COL = {
    "opportunities": "payload", "evaluations": "payload", "decisions": "payload",
    "orders": "payload", "executions": "payload", "monitoring_events": "payload",
}

def insert(self, table: str, record: dict) -> None:
    if table not in TABLES:
        raise KeyError(table)
    with psycopg.connect(self.dsn) as conn:
        if table == "prompts_history":
            conn.execute(
                "INSERT INTO prompts_history (name, version, hash, model) VALUES (%s,%s,%s,%s)",
                (record["name"], record["version"], record["hash"], record.get("model")))
        elif table == "llm_traces":
            conn.execute(
                "INSERT INTO llm_traces (request, response) VALUES (%s,%s)",
                (json.dumps(record["request"]), json.dumps(record["response"])))
        else:
            # tablas con id propio + payload JSONB
            id_col = {"opportunities": "opportunity_id", "evaluations": "evaluation_id",
                      "decisions": "decision_id", "orders": "order_id_internal",
                      "monitoring_events": "event_id"}.get(table)
            if id_col:
                conn.execute(
                    f"INSERT INTO {table} ({id_col}, payload) VALUES (%s, %s)",
                    (record.get(id_col), json.dumps(record)))
            else:  # executions (BIGSERIAL)
                conn.execute(
                    "INSERT INTO executions (order_id_internal, payload) VALUES (%s,%s)",
                    (record.get("order_id_internal"), json.dumps(record)))
        conn.commit()

def all(self, table: str) -> list[dict]:
    col = _PAYLOAD_COL.get(table, "*")
    with psycopg.connect(self.dsn) as conn:
        rows = conn.execute(f"SELECT {col} FROM {table} ORDER BY created_at").fetchall()
    return [r[0] for r in rows]
```

4. **Inyecta** `PostgresRepository` en lugar de `InMemoryRepository` en
   `src/app/main.py` (`self.repository = PostgresRepository(os.getenv("POSTGRES_DSN"))`).
   El `DecisionPipeline` ya llama a `save_opportunity/evaluation/decision`.
5. *(Opcional TimescaleDB)*: convierte `executions`/`monitoring_events` en *hypertables*
   por `created_at` para series temporales.

### 5.2 Qdrant (memoria semántica)

Fichero: `src/memory/vector_store.py` (`QdrantVectorStore`).

1. **Levanta** Qdrant: `docker compose up -d qdrant`.
2. **Crea la colección** e implementa `upsert`/`search`:

```python
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct

class QdrantVectorStore:
    def __init__(self, url, collection, embedder=None, dim=1536):
        self.client = QdrantClient(url=url)
        self.collection = collection
        self.embedder = embedder
        if not self.client.collection_exists(collection):
            self.client.create_collection(
                collection, VectorParams(size=dim, distance=Distance.COSINE))

    def upsert(self, record):
        self.client.upsert(self.collection, [PointStruct(
            id=record.id, vector=record.vector, payload=record.payload)])

    def search(self, vector, top_k=5):
        hits = self.client.search(self.collection, vector, limit=top_k)
        return [SearchHit(id=str(h.id), score=h.score, payload=h.payload or {}) for h in hits]
```

3. **Embeddings**: inyecta un `embedder` real (p. ej. `text-embedding-3-small` de OpenAI)
   que convierta texto → vector. Ajusta `dim` al modelo.
4. **Conecta** el `QdrantVectorStore` a la memoria episódica (`src/memory/`) para el
   recall semántico del Decisor.

### 5.3 Redis Streams (mensajería)

Fichero: `src/messaging/streams.py` (`RedisStreamBus`).

1. **Levanta** Redis: `docker compose up -d redis`.
2. **Crea el grupo de consumidores** e implementa `read` con `XREADGROUP`:

```python
import redis

class RedisStreamBus:
    def __init__(self, url):
        self.client = redis.Redis.from_url(url, decode_responses=True)

    def ensure_group(self, stream, group):
        try:
            self.client.xgroup_create(stream, group, id="0", mkstream=True)
        except redis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    def publish(self, stream, data):
        return self.client.xadd(stream, {"json": json.dumps(data)})

    def read(self, stream, group, count=10):
        self.ensure_group(stream, group)
        resp = self.client.xreadgroup(group, "consumer-1", {stream: ">"}, count=count)
        msgs = []
        for _stream, entries in resp or []:
            for msg_id, fields in entries:
                msgs.append(StreamMessage(id=msg_id, data=json.loads(fields["json"])))
        return msgs

    def ack(self, stream, group, message_id):
        self.client.xack(stream, group, message_id)
```

3. **Patrón productor/consumidor**: los searchers `publish("opportunities", ...)` y un
   consumidor lee con `read("opportunities", "aggregator")`, procesa por el pipeline y
   hace `ack`. Sustituye `InMemoryStreamBus` por `RedisStreamBus` en `src/app/main.py`.

---

## 6. Buscadores (LLM + datos reales) ✅ (implementado)

Ficheros: `src/agents/searchers/` (base LLM, buscadores concretos, contexto e
indicadores), `src/connectors/fundamentals.py` (Finnhub) y prompts en
`config/prompts/searchers/`.

Los 6 buscadores ya **generan oportunidades** razonando con el LLM en lugar de devolver
`[]`. El patrón común vive en `LLMSearcher` (`src/agents/searchers/llm_base.py`):

1. Cada buscador construye un **contexto** de texto a partir de su fuente.
2. `reason(context)` renderiza el prompt versionado (`config/prompts/searchers/<nombre>.md`
   con el placeholder `{market_context}`), llama al LLM en **modo JSON** y parsea una lista
   de candidatos con `extract_json_array` (`src/llm/parsing.py`).
3. Cada candidato se convierte en una `Opportunity` **validada** (`build_opportunity`
   recomputa el R/R, normaliza `position_size_pct` y descarta geometrías inválidas).

Ante cualquier fallo (sin LLM, respuesta ilegible, datos insuficientes) devuelven `[]`
(principio "no-operar por defecto"). Sin `DEEPSEEK_API_KEY` / `OPENAI_API_KEY` /
`ANTHROPIC_API_KEY`, `build_llm_client` devuelve `MockLLMClient` y no se generan
oportunidades.

### 6.1 Fuentes de datos por buscador

| Buscador | Fuente de contexto | Cliente | Patrón de pacing |
|---|---|---|---|
| `news` | Feed de noticias (titular, resumen, sentimiento) | `HttpNewsClient` (NewsAPI/Finnhub/Marketaux/AlphaVantage) | Sin límite IBKR |
| `social` | Posts de Reddit/StockTwits/Twitter | `ApifySocialClient` | Lote rotatorio 20 tickers/cashtag |
| `technical` | OHLCV intradía → indicadores (RSI, MACD, SMA/EMA, ATR) | `MarketDataClient.get_ohlcv` | Lote ≤ 50 tickers / 10 min |
| `premarket` | Snapshot pre-apertura (gap vs cierre previo) | `MarketDataClient.get_premarket` | Gating por ventana horaria |
| `fundamental` | Ratios/crecimiento/márgenes | `FallbackFundamentalsClient` (Finnhub + yfinance) | Lote ≤ 35 tickers / 30 min |
| `cross_market` | Top movers scanner IBKR filtrados por watchlist | `MarketDataClient.get_top_movers` + `get_ohlcv` | Scanner sin pacing; OHLCV sólo de candidatos |

### 6.2 Convergencia multi-fuente (Aggregator stateful)

El `Aggregator` mantiene una memoria rodante de **1 hora** entre ejecuciones del
scheduler. Si en esa ventana otra fuente independiente detectó el mismo ticker, se
aplicará un **bonus de confianza** (`multi_source_confidence_bonus` en `config.yaml`) y
los evaluadores reciben el campo `{multi_source_context}` en su prompt con el detalle
de las fuentes convergentes.

```yaml
# config/config.yaml
aggregator:
  window_seconds: 300               # dedup intra-ciclo
  cross_source_window_seconds: 3600 # ventana de memoria rodante entre runs
  multi_source_confidence_bonus: 0.08
```

### 6.3 Configuración

- **`config/config.yaml`**: `watchlist` (≈ 508 tickers IBEX35 + S&P500),
  `searchers.technical.ohlcv_bars`/`ohlcv_interval`, `aggregator.*`.
- **`.env`**: `FINNHUB_KEY` para fundamentales reales (sin clave → yfinance fallback).
  Los buscadores de mercado requieren `TRADING_MODE=PAPER/LIVE` con IB Gateway.
- **Indicadores**: instala el extra `indicators` para usar `pandas-ta`; si no, se aplica el
  fallback puro Python.

### 6.3 Cableado

En `src/app/main.py`, `build_searchers(config, ...)` inyecta a cada buscador el LLM
compartido (`build_llm_client`), el `market_data`, el `fundamentals_client`
(`build_fundamentals_client`) y la `watchlist`. Las oportunidades resultantes pasan por el
pipeline (`_process`) igual que antes.

> **Nota**: la calidad de señal de premarket/fundamental/cross_market depende de la
> riqueza de datos. Los `TODO(F2)` en cada fichero indican las siguientes mejoras
> (OHLCV pre-market real, más ratios, series de correlación entre índices).

---

## 7. Checklist final

### Validado en runtime (modo PAPER) ✅

- [x] `pip install -e ".[connectors,llm,infra,indicators]"` sin errores.
- [x] `.env` completo con `TRADING_MODE=PAPER`, `DEEPSEEK_API_KEY`, claves de noticias/redes.
- [x] `docker compose up -d postgres redis qdrant` — PostgreSQL, Redis y Qdrant saludables.
- [x] **IBKR**: `connect()` OK contra IB Gateway paper (4002); dos clientes (client_id 10 y 11).
- [x] **IBKR OHLCV/pre-market**: `get_ohlcv` y `get_premarket` funcionales en lotes rotativos.
- [x] **IBKR scanner**: `get_top_movers` (NYSE + BME) con suscripción «Bolsa de Madrid Plus (Nivel 1)».
- [x] **Error 162**: cortacircuitos 90 s + filtro de ruido `_IBWrapperNoiseFilter` activos.
- [x] **Noticias**: NewsAPI/Marketaux/Finnhub/AlphaVantage — normalización a `NewsItem`.
- [x] **Redes sociales**: Apify Social (Twitter + Reddit/StockTwits) con lotes rotativos.
- [x] **Fundamentales**: `FallbackFundamentalsClient` (Finnhub + yfinance) funcional.
- [x] **Telegram HITL**: mensaje con botones; Aprobar/Rechazar/Pausar resuelven correctamente; TTL → TIMEOUT.
- [x] **LLM DeepSeek**: `evaluation_engine: llm`, `provider: deepseek`, responde 200 OK; 6 buscadores generan oportunidades.
- [x] **Aggregator stateful**: bonus multi-fuente aplicado; memoria rodante 1h entre runs.
- [x] **Postgres**: 8 tablas creadas; filas insertadas en `decisions` y `opportunities`.
- [x] **Redis**: grupos de consumidor creados; mensajes procesados con `ack`.
- [x] **Tests siguen verdes** con los mocks: `python -m pytest -q`.

### Aún pendiente ⚠️

- [ ] Qdrant con embeddings reales (en producción usa in-memory).
- [ ] Gobierno IA (NeuralTrust + watsonx.governance).
- [ ] Cambio a modo `LIVE` (requiere cuenta real y límites de riesgo auditados).

> **Orden recomendado para activar LIVE**: (1) revisar `risk_limits.yaml` con límites
> conservadores → (2) verificar kill switch en paper → (3) test de aprobación Telegram
> con una operación real pequeña → (4) cambiar `TRADING_MODE=LIVE` y puerto 4001.
