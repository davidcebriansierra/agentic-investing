# Plan de encaje y migración: TradingAgents v0.3.0

**Contexto:** la especificación v2.0 (§4) propone usar *LangGraph 0.2+ (fork de
TradingAgents v0.3.0)* para la orquestación de agentes. La implementación actual usa
un orquestador propio determinístico (`src/graph/pipeline.py`) más un esqueleto
LangGraph compatible (`src/graph/workflow.py`), **sin** derivar de TradingAgents.

Este documento analiza el encaje de TradingAgents con la arquitectura de la spec y
propone un plan de migración. **No implica cambios de código todavía.**

---

## 1. Cómo funciona TradingAgents

Framework multi-agente sobre **LangGraph** (`StateGraph` + `AgentState` compartido)
que analiza **un ticker en una fecha** y emite `BUY/HOLD/SELL`. Orquesta ~12 agentes
LLM en 4 fases secuenciales enlazadas por un *Situation Summariser*:

```
Analyst Team (market, social, news, fundamentals)
   └─► Situation Summariser
        └─► Research Team: Bull ⇄ Bear (debate N rondas) ─► Research Manager (investment_plan)
             └─► Trader (trader_investment_plan + "FINAL TRANSACTION PROPOSAL: BUY/HOLD/SELL")
                  └─► Risk Team: Aggressive → Conservative → Neutral (debate) ─► Risk Judge
                       └─► Portfolio Manager (final_trade_decision)
```

Piezas clave:

- **`TradingAgentsGraph`** — orquestador; inicializa LLMs (quick/deep thinking),
  memorias y `ToolNode`s, y compila el grafo.
- **`FinancialSituationMemory`** — memoria por agente con BM25 (sin embeddings
  externos) + `reflect_and_remember(returns)`.
- **`SignalProcessor`** — parseo **determinístico** de la decisión final.
- **Tools** vía `@tool` de LangChain hacia yfinance / Alpha Vantage.

---

## 2. Diferencias estructurales con la spec v2.0

| Aspecto | TradingAgents | Spec v2.0 (este proyecto) |
|---|---|---|
| **Disparo** | On-demand, 1 ticker + 1 fecha | Continuo, event-driven, escanea todo IBEX35/S&P500 |
| **Detección** | No escanea; analiza un ticker dado | 6 buscadores generan oportunidades + aggregator |
| **Evaluación** | Debate adversarial Bull/Bear + Risk team (3 estilos) | 4 evaluadores ponderados (conservador/moderado/alto riesgo/sensacionalista) |
| **"Risk"** | Debate LLM sobre apetito de riesgo | Risk Filter determinístico (límites duros) — concepto distinto |
| **Decisión** | Portfolio Manager (LLM) + SignalProcessor | Decisor determinístico: consenso ponderado + expectancy |
| **Ejecución** | Solo emite señal; no ejecuta | Pre-Trade Validator + bracket order real (ib_async) |
| **Gobierno** | No tiene | Kill switch, HITL Telegram, audit trail, NeuralTrust/watsonx |
| **Memoria** | BM25 + reflexión por outcome | Episódica + track_record (recalibra pesos cada 50 ops) |

**Conclusión:** TradingAgents cubre bien la *evaluación profunda de un ticker*, pero
**no** cubre el escaneo continuo, el risk filter determinístico, la ejecución real ni
el gobierno. No es un reemplazo del sistema; es un **componente de evaluación**.

---

## 3. Mapeo de componentes

| Componente (este proyecto) | Equivalente en TradingAgents | Acción de migración |
|---|---|---|
| `searchers/` | Analyst Team (parcial) | Reusar prompts/tools de analistas; adaptar de "informe de 1 ticker" a "escaneo de oportunidades" |
| `aggregator.py` | — (no existe) | Mantener el nuestro |
| `risk_filter.py` | — (su "risk" es LLM, no determinístico) | Mantener el nuestro (no confundir conceptos) |
| `evaluators/` (4) | Risk Team (3) + Bull/Bear | Sustituible por el debate de TradingAgents, o mantener evaluadores |
| `decisor.py` | Portfolio Manager + SignalProcessor | Mantener el determinístico; adoptar el patrón `SignalProcessor` |
| `memory/` | FinancialSituationMemory + Reflector | Adoptar su `reflect_and_remember` para track_record |
| `executor` / `monitor` / `governance` | — (no existen) | Mantener los nuestros (valor diferencial) |

---

## 4. Estrategias de integración

### Estrategia A — Fork & extender (literal a la spec)

Partir de `TradingAgentsGraph` como orquestador y añadir nuestros nodos (searchers
como scanners, risk filter, HITL, executor, monitor, kill switch).

- **Pro:** fiel a la spec.
- **Contra:** su grafo es per-ticker; obliga a reescribir el *entry point* y el estado.

### Estrategia B — Embeber como subgrafo evaluador (recomendada)

Mantener nuestro `pipeline` / `workflow` y usar TradingAgents como *deep-dive
evaluator*: tras `aggregator → risk_filter`, para cada oportunidad pre-seleccionada se
invoca el subgrafo de TradingAgents (analistas + debate) y su salida alimenta nuestro
`Decisor`.

- **Pro:** mínima reescritura, conserva gobierno/ejecución, aísla coste y latencia.
- **Contra:** dos capas de orquestación.

---

## 5. Riesgos

- **Latencia / coste:** los debates multironda son lentos y caros. La spec exige
  ≤ 30 s señal→orden (§11). Un debate completo por oportunidad probablemente rompe ese
  SLA → limitarlo a oportunidades pre-filtradas y/o reducir rondas.
- **Versión:** la spec cita `TauricResearch/TradingAgents v0.3.0`. El fork `Mai0313`
  está refactorizando para eliminar LangGraph/LangChain → diverge. **Fijar (pin) la
  v0.3.0 de TauricResearch.**
- **Acceso de red:** instalar TradingAgents requiere GitHub/PyPI, hoy bloqueados en el
  entorno corporativo. Necesario repo vendorizado o índice interno (Artifactory).
- **Conceptual:** no mapear su "Risk team" (LLM) a nuestro "Risk Filter"
  (determinístico) — son cosas distintas y ambas deben coexistir.

---

## 6. Plan de migración propuesto (Estrategia B)

1. **Vendorizar** `TauricResearch/TradingAgents@v0.3.0` en `third_party/` (o pin en el
   extra `agents` de `pyproject.toml`).
2. Crear `src/agents/deep_evaluator/` que envuelva `TradingAgentsGraph` y exponga
   `evaluate(opportunity) -> Evaluation`, mapeando su salida
   (`final_trade_decision` / `TradeRecommendation`) al schema `Evaluation` (§5.2).
3. Insertar un nodo condicional en `workflow.py`:
   `risk_filter → deep_evaluate (TradingAgents) → decide`, activable por config
   (`evaluation.engine: tradingagents | heuristic`).
4. Adaptar la memoria: puentear su `reflect_and_remember(returns)` con nuestro
   `track_record` / `episodic`.
5. Limitar coste/latencia: `max_debate_rounds` bajo, caché por ticker/fecha y solo para
   las top-N oportunidades.
6. Tests de contrato: validar que su salida siempre encaja en `Evaluation` / `Decision`.

---

## 7. Referencias

- Repo original: `TauricResearch/TradingAgents` (v0.3.0) — orquestación LangGraph.
- Fork refactor: `Mai0313/TradingAgents` — documenta el workflow; en proceso de
  eliminar LangGraph/LangChain (no usar como base para alinearse con la spec).
- Análisis profundo del pipeline: `lucemia/trading-agents-plugin`
  (`TradingAgents_Deep_Analysis.md`).
