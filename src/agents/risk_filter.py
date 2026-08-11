"""Risk Filter deterministico (spec v2.0, seccion 3.3).

Valida reglas duras antes de los evaluadores LLM. Cualquier violacion produce
REJECT y la oportunidad no pasa a evaluacion.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.schemas.enums import RiskDecision
from src.schemas.opportunity import Opportunity
from src.schemas.portfolio import Portfolio
from src.utils.config import get_risk_limits


@dataclass
class RiskFilterResult:
    decision: RiskDecision
    violations: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.decision == RiskDecision.PASS


class RiskFilter:
    def __init__(self, limits: dict | None = None) -> None:
        self.limits = limits or get_risk_limits()

    def check(self, opp: Opportunity, portfolio: Portfolio) -> RiskFilterResult:
        violations: list[str] = []

        exposure = self.limits.get("exposure", {})
        correlation = self.limits.get("correlation", {})
        liquidity = self.limits.get("liquidity", {})
        var_cfg = self.limits.get("var", {})

        # 1. Exposicion maxima por activo.
        max_asset = exposure.get("max_per_asset_pct", 10) / 100
        projected_asset = portfolio.exposure_pct(opp.ticker) + opp.position_size_pct
        if projected_asset > max_asset:
            violations.append(
                f"exposicion_activo {projected_asset:.2%} > {max_asset:.0%}"
            )

        # 2. Exposicion maxima por sector.
        sector = opp.supporting_data.get("sector")
        max_sector = exposure.get("max_per_sector_pct", 25) / 100
        if sector is not None:
            projected_sector = (
                portfolio.sector_exposure_pct(sector) + opp.position_size_pct
            )
            if projected_sector > max_sector:
                violations.append(
                    f"exposicion_sector {projected_sector:.2%} > {max_sector:.0%}"
                )

        # 3. Correlacion con posiciones abiertas.
        max_corr = correlation.get("max_with_open_positions", 0.7)
        for pos in portfolio.positions:
            rho = self._correlation(portfolio, opp.ticker, pos.ticker)
            if rho is not None and rho > max_corr:
                violations.append(
                    f"correlacion {opp.ticker}/{pos.ticker} {rho:.2f} > {max_corr}"
                )

        # 4. Liquidez minima del activo.
        min_vol = liquidity.get("min_avg_volume_20d", 0)
        vol = portfolio.avg_volume_20d.get(opp.ticker)
        if min_vol and vol is not None and vol < min_vol:
            violations.append(f"liquidez {vol:.0f} < {min_vol:.0f}")

        # 5. Capital total en riesgo simultaneo.
        max_total = exposure.get("max_total_at_risk_pct", 30) / 100
        projected_total = portfolio.total_at_risk_pct() + (
            opp.stop_loss_pct * opp.position_size_pct
        )
        if projected_total > max_total:
            violations.append(
                f"capital_en_riesgo {projected_total:.2%} > {max_total:.0%}"
            )

        # 6. VaR(95%) del portfolio sobre umbral.
        max_var = var_cfg.get("max_portfolio_var_pct")
        portfolio_var = opp.supporting_data.get("portfolio_var_pct")
        if max_var is not None and portfolio_var is not None:
            if portfolio_var > max_var:
                violations.append(
                    f"VaR {portfolio_var:.2f}% > {max_var:.2f}%"
                )

        decision = RiskDecision.REJECT if violations else RiskDecision.PASS
        return RiskFilterResult(decision=decision, violations=violations)

    @staticmethod
    def _correlation(portfolio: Portfolio, a: str, b: str) -> float | None:
        if a == b:
            return 1.0
        return portfolio.correlations.get(f"{a}|{b}") or portfolio.correlations.get(
            f"{b}|{a}"
        )
