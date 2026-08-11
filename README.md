# Sistema Agéntico de Inversión – IBEX35 & S&P500

Implementación en Python del sistema multi-agente de inversión intradía descrito en
`sistema_agentico_inversion_v2_spec` (v2.0). Detecta, evalúa, decide y ejecuta
oportunidades sobre acciones de **IBEX35** y **S&P500** con gobierno y trazabilidad
por diseño.

> **Estado actual**: sistema completo end-to-end operando en **modo PAPER contra IB Gateway real**.
> Los **6 buscadores** razonan con **DeepSeek** (v4-flash), el **nodo HITL LangGraph** está
> cableado con `TelegramHITLClient` real y el **Monitor** emite 11 métricas a
> Prometheus/Grafana. El `IBKRMarketDataClient` gestiona el Error 162 con
> *fail-fast* + diagnóstico único. El `Aggregator` aplica bonus de convergencia
> multi-fuente con memoria rodante de 1 hora entre ejecuciones.

---

## Principios de diseño

- **No-operar por defecto**: solo se opera con *expectancy* positiva y todos los controles en verde.
- **Determinismo en la ejecución**: los LLM razonan; las órdenes las ejecuta código determinístico con validación previa.
- **Human-in-the-loop (HITL)** vía Telegram (fase inicial).
- **Gobierno y trazabilidad por diseño**: cada decisión es auditable extremo a extremo.

---

## Arquitectura del flujo

```
searchers ─► aggregate ─► risk_filter ─► evaluate ─► decide ─► hitl ─► execute ─► monitor
              (§3.2)        (§3.3)        (§3.4)     (§3.5)   (§3.6)   (§3.7)     (§3.8)
```

El recorrido está implementado de dos formas equivalentes:

- **`src/graph/pipeline.py`** — orquestador en Python puro, sin dependencias externas (usado en tests y shadow mode).
- **`src/graph/workflow.py`** — grafo **LangGraph** (requiere el extra `agents`).

---

## Estructura del proyecto

```
agentic-investing/
├── config/                     # config.yaml, risk_limits.yaml, decisor_weights.yaml
├── src/
│   ├── agents/
│   │   ├── searchers/          # 6 buscadores LLM (news, social, technical, premarket,
│   │   │                       #   fundamental, cross_market) + indicators/context
│   │   ├── evaluators/         # 4 evaluadores con perfiles de riesgo + contexto multi-fuente
│   │   ├── aggregator.py       # dedup + bonus convergencia multi-fuente (memoria rodante 1h)
│   │   ├── risk_filter.py      # reglas duras de riesgo (determinístico)
│   │   ├── decisor.py          # consenso ponderado + expectancy
│   │   ├── executor.py         # construcción + envío de bracket order
│   │   └── monitor.py          # P&L, stop-loss, kill switch + métricas Prometheus
│   ├── connectors/
│   │   ├── ibkr_read.py        # MarketDataClient: OHLCV, pre-market, scanner movers;
│   │   │                       #   fail-fast ante Error 162 + filtro de ruido ib_async.wrapper
│   │   ├── ibkr_write.py       # BrokerClient (Mock + ib_async real)
│   │   ├── news_apis.py        # NewsClient (NewsAPI/Marketaux/Finnhub/AlphaVantage)
│   │   ├── social_apis.py      # SocialClient (Reddit/StockTwits)
│   │   ├── fundamentals.py     # FundamentalsClient (Finnhub)
│   │   ├── telegram_bot.py     # HITLClient real + MockHITLClient (TTL, PAUSE_1H, botones)
│   │   └── factory.py          # selección real/mock por env (TRADING_MODE, claves)
│   ├── governance/             # pre_trade_validator, kill_switch, audit_logger
│   ├── graph/                  # pipeline.py + workflow.py (LangGraph con nodo HITL real)
│   ├── messaging/              # EventPublisher + RedisStreamBus + InMemoryStreamBus
│   ├── memory/                 # episodic, track_record, vector_store
│   ├── observability/          # metrics.py — 11 métricas Prometheus (monitor + pipeline)
│   ├── schemas/                # contratos de datos Pydantic (§5)
│   └── utils/                  # carga de configuración
├── tests/                      # unit (19 ficheros) + integration + backtest
├── scripts/                    # run_tests.ps1 / run_tests.bat / test_telegram_bot.py
└── pyproject.toml
```

---

## Instalación

Requiere **Python 3.11+** (probado con 3.13).

```powershell
# Nucleo + tests (minimo)
python -m pip install -e .[dev]

# Extras opcionales segun lo que vayas a usar:
python -m pip install -e .[agents]        # LangGraph
python -m pip install -e .[infra]         # Redis, Postgres, Qdrant, APScheduler
python -m pip install -e .[connectors]    # ib_async, telegram, httpx
python -m pip install -e .[llm]           # openai, anthropic
python -m pip install -e .[indicators]    # pandas, pandas-ta (opcional; hay fallback)
```

> **Red corporativa**: si PyPI público está bloqueado (error SSL handshake), usa el
> índice interno con `--index-url <url-artifactory>` o configura `PIP_INDEX_URL`.

---

## Ejecutar las pruebas

```powershell
# Si las dependencias ya estan instaladas
./scripts/run_tests.ps1

# Instalando deps en un venv desde el indice interno
./scripts/run_tests.ps1 -Install -UseVenv -IndexUrl https://<artifactory>/api/pypi/pypi/simple

# Con cobertura
./scripts/run_tests.ps1 -Coverage
```

O directamente con pytest:

```powershell
$env:PYTHONPATH = "."
python -m pytest -v
```

### En otra máquina (Windows 11 + WSL + Docker Desktop)

**Opción A — Docker (recomendada, no instala nada en el host).** Ejecuta los tests
dentro de un contenedor `python:3.11-slim`:

```powershell
# PowerShell (Windows 11 con Docker Desktop)
./scripts/run_tests_docker.ps1
./scripts/run_tests_docker.ps1 -Coverage
```

```bash
# Dentro de WSL / Linux / macOS
./scripts/run_tests_docker.sh
./scripts/run_tests_docker.sh --coverage
```

**Opción B — WSL con venv** (si prefieres no usar Docker):

```bash
./scripts/run_tests.sh                 # crea .venv, instala y ejecuta
make install && make test              # equivalente con make
```

**Opción C — Windows nativo con PyPI**:

```powershell
./scripts/run_tests.ps1 -Install -UseVenv
```

```bash
# Instalacion manual de dependencias de test
python -m pip install -r requirements-dev.txt
PYTHONPATH=. python -m pytest -v
```

> Todas las dependencias de test estan en `requirements-dev.txt`. Las dependencias de
> servicios externos (LLM, IBKR, infra) son **opcionales** y no hacen falta para pasar
> la bateria de pruebas, gracias a los imports perezosos y los mocks.
>
> Si la máquina destino también tuviera un proxy corporativo, pasa el índice interno:
> `./scripts/run_tests_docker.ps1 -IndexUrl https://<artifactory>/api/pypi/pypi/simple`.

---

## Configuración

Copia `.env.example` a `.env` y rellena las credenciales. Los límites de negocio se
ajustan en:

- `config/config.yaml` — mercados, frecuencias de buscadores, HITL, LLM (DeepSeek),
  `window_seconds`, `cross_source_window_seconds` (ventana del bonus multi-fuente).
- `config/risk_limits.yaml` — exposición, correlación, stop-loss, kill switch.
- `config/decisor_weights.yaml` — pesos del consenso y umbrales de decisión.

---

## Ejemplo de uso (orquestador)

```python
from src.graph.pipeline import DecisionPipeline
from src.schemas.portfolio import Portfolio

pipeline = DecisionPipeline()
portfolio = Portfolio(total_equity=100_000, cash=100_000)

states = pipeline.run(
    opportunities=mis_oportunidades,   # list[Opportunity]
    portfolio=portfolio,
    approval_fn=lambda d, o: True,     # HITL (Telegram en produccion)
    execute_fn=mi_funcion_ejecucion,   # ejecucion (Executor + broker)
)
for s in states:
    print(s.decision.decision, s.decision.reason)
```

### LangGraph (con HITL real)

```python
from src.graph.workflow import build_workflow
from src.connectors.factory import build_hitl_client

app = build_workflow(
    broker=mi_broker,
    hitl=build_hitl_client(),   # TelegramHITLClient o Mock
    kill_switch=mi_kill_switch,
)
result = await app.ainvoke({"raw_opportunities": opps, "portfolio": portfolio})
```

### Validar el flujo TTL (sin red)

```powershell
# Prueba mock: TIMEOUT → no-operar (instantáneo, sin Telegram)
python scripts/test_telegram_bot.py --timeout-test

# Todos los flujos HITL de una vez
python scripts/test_telegram_bot.py --all-mock

# Prueba interactiva real (requiere .env con TELEGRAM_BOT_TOKEN)
python scripts/test_telegram_bot.py AAPL 30
```

---

## Roadmap

Implementado y validado en runtime (modo PAPER):

- [x] Contratos de datos Pydantic (§5)
- [x] Aggregator stateful: dedup intra-lote + **bonus de convergencia multi-fuente** con memoria rodante 1 hora cross-run
- [x] Risk Filter, Decisor (determinísticos)
- [x] Evaluadores LLM (4 perfiles) con contexto `{multi_source_context}` inyectado en prompt
- [x] Buscadores LLM: los 6 razonan sobre datos reales con **DeepSeek v4-flash** (confirmado 200 OK en runtime)
  - `technical` / `fundamental`: rotación por lotes (50 / 35 tickers) para respetar pacing IBKR
  - `cross_market`: scanner IBKR (`TOP_PERC_GAIN/LOSE`) → filtra por watchlist; BME usa `STOCK.EU` / `STK.EU.BM`
  - `premarket`: gating por ventana de preapertura con `zoneinfo`
  - `social`: Apify Twitter + Reddit/StockTwits; lotes rotativos de 20 tickers/cashtag
- [x] Pre-Trade Validator, Kill Switch, Audit Logger
- [x] Executor + Monitor con `IBKRBrokerClient` real (paper 4002)
- [x] Orquestador (`pipeline.py` run/arun) + grafo LangGraph (`workflow.py`)
- [x] Tests unit + integración (19 ficheros)
- [x] **IBKR `ib_async` en producción paper**: `IBKRMarketDataClient` conectado a IB Gateway 4002;
  - *fail-fast* ante Error 162 «different IP address» (cortacircuitos 90 s)
  - Filtro `_IBWrapperNoiseFilter` en `ib_async.wrapper` colapsa el spam de ERRORs por contrato
  - Scanner BME con suscripción «Bolsa de Madrid Plus (Nivel 1)» activa
- [x] Conectores HTTP: noticias (NewsAPI/Marketaux/Finnhub/AlphaVantage), redes (Apify Social), fundamentales (Finnhub + yfinance fallback)
- [x] **HITL Telegram end-to-end**: botones Aprobar/Rechazar/Pausar, flujo TTL → no-operar validado
- [x] **Observabilidad (F6)**: 11 métricas Prometheus (pipeline + monitor); endpoint `0.0.0.0:8000/metrics`
- [x] **EventPublisher**: publica en Redis Streams tras cada fase del pipeline
- [x] Persistencia: PostgreSQL/TimescaleDB + Redis Streams + Qdrant (in-memory como fallback)
- [x] Scheduler (`APScheduler`) y mensajería (`RedisStreamBus`)
- [x] Infraestructura: Docker Compose + manifiestos K8s; Backtesting (KPIs §11)
- [x] Dashboard `agent_monitor.py`: timeline y tablas de oportunidades/decisiones en hora local

Pendiente:

- [ ] Gobierno IA (NeuralTrust + watsonx.governance)
- [ ] Colecciones Qdrant y embeddings reales (en producción usa in-memory)

---

## Despliegue (Docker / Kubernetes)

**Docker Compose** levanta toda la pila (Postgres+TimescaleDB, Redis, Qdrant,
Prometheus, Grafana y la app):

```powershell
# Red corporativa: pasar el indice pip interno al build de la imagen
$env:PIP_INDEX_URL = "https://<artifactory>/api/pypi/pypi/simple"
docker compose up --build
```

- App / métricas: `http://localhost:8000/metrics`
- Prometheus: `http://localhost:9090`
- Grafana: `http://localhost:3000` (admin/admin) → dashboard *Agentic Investing - Overview*

**Kubernetes** (`infra/k8s/`):

```powershell
kubectl apply -f infra/k8s/namespace.yaml
kubectl apply -f infra/k8s/configmap.yaml
kubectl apply -f infra/k8s/secret.example.yaml   # reemplazar por Vault/External Secrets
kubectl apply -f infra/k8s/deployment.yaml
kubectl apply -f infra/k8s/service.yaml
```

> Los secretos del ejemplo deben sustituirse por **HashiCorp Vault / AWS Secrets
> Manager** (con External Secrets Operator) en entorno corporativo.

---

## Observabilidad — métricas disponibles

| Métrica | Tipo | Descripción |
|---|---|---|
| `agentic_opportunities_total{agent_source}` | Counter | Oportunidades detectadas por fuente |
| `agentic_decisions_total{decision}` | Counter | Decisiones emitidas (OPERATE / NO_OPERATE) |
| `agentic_orders_total{status}` | Counter | Órdenes enviadas al broker |
| `agentic_risk_rejections_total` | Counter | Rechazadas por Risk Filter |
| `agentic_kill_switch_active` | Gauge | 1=activo, 0=inactivo |
| `agentic_signal_to_order_seconds` | Histogram | Latencia señal → orden |
| `agentic_pnl_unrealized_pct` | Gauge | P&L no realizado / equity |
| `agentic_open_positions` | Gauge | Posiciones abiertas |
| `agentic_consecutive_losses` | Gauge | Pérdidas consecutivas actuales |
| `agentic_missing_stop_loss_total` | Counter | Posiciones sin stop-loss detectadas |
| `agentic_monitoring_events_total{event_type,severity}` | Counter | Eventos de monitorización |

Endpoint: `http://localhost:8000/metrics` · Dashboard Grafana: `http://localhost:3000`

---

## Documentación

- [`docs/arquitectura.html`](docs/arquitectura.html) —
  arquitectura lógica interactiva, infraestructura, animación del flujo y árbol del proyecto.
- [`docs/diagrama_implementado.html`](docs/diagrama_implementado.html) —
  estado de implementación por componente (verde/amarillo/gris) con KPIs.
- [`docs/cableado_conectores_reales.md`](docs/cableado_conectores_reales.md) —
  guía paso a paso para cablear las integraciones reales (IBKR, noticias/redes, Telegram
  HITL, LLM, Postgres/Qdrant/Redis) sustituyendo los mocks.
- [`docs/tradingagents_migration_plan.md`](docs/tradingagents_migration_plan.md) —
  análisis de encaje y plan de migración hacia **TradingAgents v0.3.0** (la spec §4
  propone usarlo como base de orquestación). Incluye mapeo de componentes, estrategias
  de integración y riesgos (latencia/SLA, versión, red).

> **Nota sobre la orquestación**: la implementación actual usa un orquestador propio
> determinístico (`src/graph/pipeline.py`) y un grafo LangGraph completo
> (`src/graph/workflow.py`) con nodo HITL real, **no** un fork de TradingAgents.

---

## Cumplimiento

Diseñado considerando MiFID II art. 17 (kill switch, controles pre-trade), EU AI Act,
DORA y SR 11-7 / SS1/23 (cada agente LLM es un "modelo" sujeto a validación
independiente). Ver §7 de la especificación.
