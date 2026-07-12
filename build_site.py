"""Genera docs/data.json (el ranking para la web) desde el checkpoint.

Tambien mantiene la cartera modelo (cartera.json): los N_CARTERA primeros
valores aptos del ranking general a peso igual, cada posicion 12 meses y
rotacion automatica al vencer — la mecanica del backtest de Greenblatt.

Se ejecuta despues de cada barrido/update:
  python build_site.py
"""
import json
import os
import time
from datetime import date, timedelta

from value_screener import (Metrics, _load_checkpoint, financial_rank,
                            magic_formula_rank, passes_filters)

CHECKPOINT = "universe_metrics.json"
OUT = os.path.join("docs", "data.json")
CARTERA = "cartera.json"
N_CARTERA = 25
DIAS_ROTACION = 365


def _r(v, nd=2):
    return None if v is None else round(v, nd)


def _norm(tk):
    return tk.upper().replace(".", "-")


def _grupos():
    """top500 = las 500 primeras del mapa SEC (ordenado ~por capitalizacion);
    russell2000 = cartera del ETF VTWO cacheada."""
    top500, russell = set(), set()
    if os.path.exists("edgar_company_tickers.json"):
        with open("edgar_company_tickers.json", encoding="utf-8") as f:
            data = json.load(f)
        vistos = []
        for r in data.values():
            tk = _norm(r["ticker"])
            if tk not in top500:
                top500.add(tk)
                vistos.append(tk)
            if len(vistos) >= 500:
                break
    ruta = os.path.join(".cache", "russell2000_tickers.json")
    if os.path.exists(ruta):
        with open(ruta, encoding="utf-8") as f:
            russell = {_norm(t) for t in json.load(f)}
    return top500, russell


def _precio_cache(ticker):
    """Ultimo precio conocido, desde la cache de empresas (refresco semanal)."""
    safe = ticker.replace("/", "_").replace(".", "_")
    p = os.path.join(".cache", f"edgar_{safe}.json")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)["company"]["price"] or None
        except Exception:
            return None
    return None


def actualizar_cartera(general):
    """Mantiene cartera.json: cierra posiciones con 12 meses cumplidos y
    rellena huecos con los mejores valores aptos del ranking no poseidos."""
    hoy = date.today()
    estado = {"creada": hoy.isoformat(), "posiciones": [], "historial": []}
    if os.path.exists(CARTERA):
        with open(CARTERA, encoding="utf-8") as f:
            estado = json.load(f)
    by_ticker = {r["ticker"]: r for r in general}

    vivas = []
    for p in estado["posiciones"]:
        if (hoy - date.fromisoformat(p["entrada"])).days >= DIAS_ROTACION:
            pa = _precio_cache(p["ticker"])
            rent = (pa / p["precio_entrada"] - 1) if pa and p.get("precio_entrada") else None
            estado.setdefault("historial", []).append(
                {**p, "salida": hoy.isoformat(), "precio_salida": _r(pa),
                 "rent": _r(rent, 4)})
        else:
            vivas.append(p)

    held = {p["ticker"] for p in vivas}
    for r in general:
        if len(vivas) >= N_CARTERA:
            break
        if r["apta"] and r["ticker"] not in held:
            vivas.append({"ticker": r["ticker"], "nombre": r["nombre"],
                          "entrada": hoy.isoformat(), "rank_entrada": r["rank"],
                          "precio_entrada": _r(_precio_cache(r["ticker"]))})
            held.add(r["ticker"])

    estado["posiciones"] = vivas
    with open(CARTERA, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=1)

    out = []
    for p in vivas:
        pa = _precio_cache(p["ticker"])
        rent = (pa / p["precio_entrada"] - 1) if pa and p.get("precio_entrada") else None
        r = by_ticker.get(p["ticker"], {})
        out.append({"ticker": p["ticker"], "nombre": p["nombre"],
                    "entrada": p["entrada"],
                    "rotacion": (date.fromisoformat(p["entrada"])
                                 + timedelta(days=DIAS_ROTACION)).isoformat(),
                    "peso": round(1.0 / N_CARTERA, 4),
                    "precio_entrada": _r(p.get("precio_entrada")),
                    "precio_actual": _r(pa), "rent": _r(rent, 4),
                    "rank_actual": r.get("rank"),
                    "apta": r.get("apta", False),
                    "grupo": r.get("grupo", "otro")})
    return {"creada": estado.get("creada", hoy.isoformat()), "n": N_CARTERA,
            "posiciones": out,
            "historial": estado.get("historial", [])}


def main():
    ck = _load_checkpoint(CHECKPOINT)
    if not ck:
        raise SystemExit("No hay checkpoint; ejecuta primero 'universe'.")
    top500, russell = _grupos()

    def grupo(tk):
        n = _norm(tk)
        if n in top500:
            return "top500"
        if n in russell:
            return "russell"
        return "otro"

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
            "grupo": grupo(m.ticker), "cap": _r(m.market_cap, 0),
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
            "grupo": grupo(m.ticker), "cap": _r(m.market_cap, 0),
            "per": _r(m.per), "pb": _r(m.pb),
            "roe": _r(m.roe, 4), "roa": _r(m.roa, 4),
            "capital_activos": _r(m.equity_assets, 4),
            "score": m.fin_score, "apta": apta, "razones": razones,
        })

    os.makedirs("docs", exist_ok=True)
    cartera = actualizar_cartera(general)
    data = {
        "generado": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
        "n_metricas": len(metrics), "n_errores": errores,
        "general": general, "financieras": financieras, "cartera": cartera,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    print(f"{OUT}: {len(general)} generales + {len(financieras)} financieras "
          f"+ cartera de {len(cartera['posiciones'])} "
          f"({os.path.getsize(OUT) // 1024} KB)")


if __name__ == "__main__":
    main()
