"""Genera docs/data.json (el ranking para la web) desde el checkpoint.

Se ejecuta despues de cada barrido/update:
  python build_site.py
"""
import json
import os
import time

from value_screener import (Metrics, _load_checkpoint, financial_rank,
                            magic_formula_rank, passes_filters)

CHECKPOINT = "universe_metrics.json"
OUT = os.path.join("docs", "data.json")


def _r(v, nd=2):
    return None if v is None else round(v, nd)


def main():
    ck = _load_checkpoint(CHECKPOINT)
    if not ck:
        raise SystemExit("No hay checkpoint; ejecuta primero 'universe'.")
    metrics = [Metrics(**e["metrics"]) for e in ck.values() if "metrics" in e]
    errores = sum(1 for e in ck.values() if "error" in e and "metrics" not in e)
    magic_formula_rank(metrics)
    financial_rank(metrics)

    general = []
    for m in sorted([x for x in metrics if x.magic_rank is not None],
                    key=lambda x: x.magic_rank):
        apta, razones = passes_filters(m)
        general.append({
            "rank": m.magic_rank, "ticker": m.ticker, "nombre": m.name,
            "ev_ebit": _r(m.ev_ebit), "ev_ebitda": _r(m.ev_ebitda),
            "roic": _r(m.roic, 4), "fcf_yield": _r(m.fcf_yield, 4),
            "earnings_yield": _r(m.earnings_yield, 4),
            "per": _r(m.per), "pb": _r(m.pb),
            "deuda_ebitda": _r(m.net_debt_ebitda),
            "piotroski": m.piotroski, "apta": apta, "razones": razones,
        })

    financieras = []
    for m in sorted([x for x in metrics if x.fin_rank is not None],
                    key=lambda x: x.fin_rank):
        apta, razones = passes_filters(m)
        financieras.append({
            "rank": m.fin_rank, "ticker": m.ticker, "nombre": m.name,
            "per": _r(m.per), "pb": _r(m.pb),
            "roe": _r(m.roe, 4), "roa": _r(m.roa, 4),
            "capital_activos": _r(m.equity_assets, 4),
            "score": m.fin_score, "apta": apta, "razones": razones,
        })

    os.makedirs("docs", exist_ok=True)
    data = {
        "generado": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
        "n_metricas": len(metrics), "n_errores": errores,
        "general": general, "financieras": financieras,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    print(f"{OUT}: {len(general)} generales + {len(financieras)} financieras "
          f"({os.path.getsize(OUT) // 1024} KB)")


if __name__ == "__main__":
    main()
