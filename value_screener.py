"""
value_screener.py
==================
Herramienta de value investing con ingesta automatica de universos.

Modos:
  analyze  -> ficha profunda de una o varias empresas concretas.
  screen   -> rankea una lista de tickers que pasas a mano.
  universe -> se trae un universo entero (sp500, ibex35, nasdaq100, all, o un
              archivo de tickers), lo procesa por tandas con checkpoint
              reanudable y lo rankea. Es el modo para "coger el mayor numero
              de valores automaticamente".
  update   -> refresca los datos obsoletos del ultimo universo procesado y
              vuelve a rankear (para "buscar actualizaciones en los datos").

Arquitectura por capas (datos / metricas / decision) desacopladas.

Financieras (bancos, aseguradoras): se detectan automaticamente (por sector o
porque no reportan EBIT/EBITDA/circulante) y se evaluan con metricas propias
(P/B, ROE, ROA, apalancamiento) en un ranking separado del Magic Formula, que
por diseño las excluye.

Uso:
  python value_screener.py analyze SAN.MC
  python value_screener.py screen AAPL MSFT SAN.MC
  python value_screener.py universe --universe ibex35 --mock
  python value_screener.py universe --universe sp500 --fmp --rate 250 --top 40
  python value_screener.py universe --universe mis_tickers.txt --csv ranking.csv
  python value_screener.py update --fmp --refresh 24

Dos caches independientes:
  - CachedProvider: ./.cache/{provider}_{ticker}.json (empresa cruda, TTL horas).
  - Checkpoint de universo: universe_metrics.json (metricas ya calculadas +
    timestamp). Permite reanudar un barrido cortado y refrescar solo lo viejo.

Fuentes de datos:
  - SEC EDGAR (por defecto): fuente oficial y gratuita del regulador de EE.UU.
    Sin API key, ~10 llamadas/segundo permitidas, datos XBRL estructurados de
    las cuentas presentadas (10-K). Solo cubre cotizadas en EE.UU. El precio
    de cotizacion (que EDGAR no tiene) se toma de stooq.com y, si falla, de
    yfinance. Universo "usa": todas las cotizadas SEC (~10.000, ordenadas
    aprox. por capitalizacion, asi que --limit 500 = las 500 mayores).
  - yfinance (--yahoo): para tickers no americanos (ej. IBEX). No oficial,
    bloquea si se abusa; con refresco trimestral no suele ser problema.
  - FMP (--fmp): requiere FMP_API_KEY; gratuito ~250 llamadas/dia.

Uso EDGAR:
  python value_screener.py analyze AAPL JPM
  python value_screener.py universe --universe usa --limit 500 --csv ranking.csv
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import date as _date
from typing import Optional


def _ssl_context():
    """El Python de python.org en macOS no trae certificados raiz para
    urllib; si certifi esta instalado, se usan los suyos."""
    try:
        import ssl
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return None


_SSL_CTX = _ssl_context()


def _urlopen(req, timeout: float = 30):
    return urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX)


# ============================================================================
# 1. MODELO DE DATOS
# ============================================================================

@dataclass
class YearData:
    year: int
    revenue: float
    gross_profit: float
    ebit: float
    ebitda: float
    net_income: float
    interest_expense: float
    tax_rate: float
    operating_cash_flow: float
    capex: float
    total_assets: float
    total_debt: float
    long_term_debt: float
    cash: float
    current_assets: float
    current_liabilities: float
    net_fixed_assets: float
    equity: float
    shares_outstanding: float


@dataclass
class Company:
    ticker: str
    name: str
    price: float
    currency: str
    years: list[YearData]
    sector: str = ""

    @property
    def latest(self) -> YearData:
        return self.years[-1]

    @property
    def prior(self) -> Optional[YearData]:
        return self.years[-2] if len(self.years) >= 2 else None

    @property
    def market_cap(self) -> float:
        return self.price * self.latest.shares_outstanding


def company_to_dict(c: Company) -> dict:
    return asdict(c)


def company_from_dict(d: dict) -> Company:
    return Company(ticker=d["ticker"], name=d["name"], price=d["price"],
                   currency=d["currency"], years=[YearData(**y) for y in d["years"]],
                   sector=d.get("sector", ""))


def is_financial(c: Company) -> bool:
    """Bancos y aseguradoras: por sector declarado o, si no hay sector (cache
    antigua, FMP sin profile), porque no reportan EBIT/EBITDA/circulante."""
    s = c.sector.lower()
    if any(k in s for k in ("financ", "bank", "insur", "banc", "segur")):
        return True
    y = c.latest
    return (y.ebit == 0 and y.ebitda == 0 and y.current_assets == 0
            and y.total_assets > 0)


# ============================================================================
# 2. METRICAS
# ============================================================================

def _safe_div(a: float, b: float) -> Optional[float]:
    return a / b if b not in (0, None) else None


@dataclass
class Metrics:
    ticker: str
    name: str
    per: Optional[float] = None
    pb: Optional[float] = None
    ev_ebit: Optional[float] = None
    ev_ebitda: Optional[float] = None
    fcf_yield: Optional[float] = None
    earnings_yield: Optional[float] = None
    roic: Optional[float] = None
    roc_greenblatt: Optional[float] = None
    roe: Optional[float] = None
    gross_margin: Optional[float] = None
    operating_margin: Optional[float] = None
    net_margin: Optional[float] = None
    net_debt_ebitda: Optional[float] = None
    interest_coverage: Optional[float] = None
    current_ratio: Optional[float] = None
    fcf_conversion: Optional[float] = None
    piotroski: Optional[int] = None
    magic_rank: Optional[int] = None
    market_cap: Optional[float] = None
    # --- solo financieras ---
    is_financial: bool = False
    roa: Optional[float] = None
    equity_assets: Optional[float] = None
    fin_score: Optional[int] = None
    fin_rank: Optional[int] = None


def compute_metrics(c: Company) -> Metrics:
    if is_financial(c):
        return compute_financial_metrics(c)
    y = c.latest
    ev = c.market_cap + y.total_debt - y.cash
    fcf = y.operating_cash_flow - y.capex
    invested_capital = y.total_debt + y.equity - y.cash
    greenblatt_base = max(y.current_assets - y.current_liabilities, 0) + y.net_fixed_assets

    m = Metrics(ticker=c.ticker, name=c.name)
    m.market_cap = c.market_cap
    m.per = _safe_div(c.market_cap, y.net_income)
    m.pb = _safe_div(c.market_cap, y.equity)
    m.ev_ebit = _safe_div(ev, y.ebit)
    m.ev_ebitda = _safe_div(ev, y.ebitda)
    m.fcf_yield = _safe_div(fcf, c.market_cap)
    m.earnings_yield = _safe_div(y.ebit, ev)
    m.roic = _safe_div(y.ebit * (1 - y.tax_rate), invested_capital)
    m.roc_greenblatt = _safe_div(y.ebit, greenblatt_base)
    m.roe = _safe_div(y.net_income, y.equity)
    m.gross_margin = _safe_div(y.gross_profit, y.revenue)
    m.operating_margin = _safe_div(y.ebit, y.revenue)
    m.net_margin = _safe_div(y.net_income, y.revenue)
    m.net_debt_ebitda = _safe_div(y.total_debt - y.cash, y.ebitda)
    m.interest_coverage = _safe_div(y.ebit, y.interest_expense)
    m.current_ratio = _safe_div(y.current_assets, y.current_liabilities)
    m.fcf_conversion = _safe_div(fcf, y.net_income)
    m.piotroski = piotroski_fscore(c)
    return m


def compute_financial_metrics(c: Company) -> Metrics:
    """Bancos/aseguradoras: EV, EBIT, deuda neta y circulante no son
    significativos; se evaluan por rentabilidad sobre activos y capital."""
    y = c.latest
    m = Metrics(ticker=c.ticker, name=c.name, is_financial=True)
    m.market_cap = c.market_cap
    m.per = _safe_div(c.market_cap, y.net_income)
    m.pb = _safe_div(c.market_cap, y.equity)
    m.roe = _safe_div(y.net_income, y.equity)
    m.roa = _safe_div(y.net_income, y.total_assets)
    m.equity_assets = _safe_div(y.equity, y.total_assets)
    m.net_margin = _safe_div(y.net_income, y.revenue)
    m.fin_score = financial_score(c)
    return m


# ============================================================================
# 3. DECISION
# ============================================================================

def piotroski_fscore(c: Company) -> Optional[int]:
    y, p = c.latest, c.prior
    if p is None:
        return None
    score = 0
    roa = _safe_div(y.net_income, y.total_assets) or 0
    roa_prev = _safe_div(p.net_income, p.total_assets) or 0
    score += y.net_income > 0
    score += y.operating_cash_flow > 0
    score += roa > roa_prev
    score += y.operating_cash_flow > y.net_income
    ltd = _safe_div(y.long_term_debt, y.total_assets) or 0
    ltd_prev = _safe_div(p.long_term_debt, p.total_assets) or 0
    score += ltd < ltd_prev
    cr = _safe_div(y.current_assets, y.current_liabilities) or 0
    cr_prev = _safe_div(p.current_assets, p.current_liabilities) or 0
    score += cr > cr_prev
    score += y.shares_outstanding <= p.shares_outstanding
    gm = _safe_div(y.gross_profit, y.revenue) or 0
    gm_prev = _safe_div(p.gross_profit, p.revenue) or 0
    score += gm > gm_prev
    at = _safe_div(y.revenue, y.total_assets) or 0
    at_prev = _safe_div(p.revenue, p.total_assets) or 0
    score += at > at_prev
    return int(score)


def financial_score(c: Company) -> Optional[int]:
    """Score 0-6 para financieras (el Piotroski clasico no aplica: el flujo de
    caja operativo de un banco es ruido de depositos y no hay circulante)."""
    y, p = c.latest, c.prior
    if p is None:
        return None
    roe = _safe_div(y.net_income, y.equity) or 0
    roa = _safe_div(y.net_income, y.total_assets) or 0
    roa_prev = _safe_div(p.net_income, p.total_assets) or 0
    ea = _safe_div(y.equity, y.total_assets) or 0
    score = 0
    score += y.net_income > 0
    score += roe > 0.10
    score += roa > 0.007
    score += ea > 0.05
    score += roa > roa_prev
    score += y.shares_outstanding <= p.shares_outstanding
    return int(score)


def financial_rank(metrics: list[Metrics]) -> None:
    """Ranking propio de financieras: barato (1/PER) + rentable (ROE),
    misma mecanica de doble ranking que la Magic Formula."""
    valid = [m for m in metrics if m.is_financial
             and m.per is not None and m.per > 0 and m.roe is not None]
    by_ey = sorted(valid, key=lambda m: 1 / m.per, reverse=True)
    by_roe = sorted(valid, key=lambda m: m.roe, reverse=True)
    rank_ey = {m.ticker: i for i, m in enumerate(by_ey)}
    rank_roe = {m.ticker: i for i, m in enumerate(by_roe)}
    combined = sorted(valid, key=lambda m: rank_ey[m.ticker] + rank_roe[m.ticker])
    for m in metrics:
        m.fin_rank = None
    for i, m in enumerate(combined, start=1):
        m.fin_rank = i


def magic_formula_rank(metrics: list[Metrics]) -> None:
    valid = [m for m in metrics
             if m.earnings_yield is not None and m.roc_greenblatt is not None
             and m.earnings_yield > 0]
    by_ey = sorted(valid, key=lambda m: m.earnings_yield, reverse=True)
    by_roc = sorted(valid, key=lambda m: m.roc_greenblatt, reverse=True)
    rank_ey = {m.ticker: i for i, m in enumerate(by_ey)}
    rank_roc = {m.ticker: i for i, m in enumerate(by_roc)}
    combined = sorted(valid, key=lambda m: rank_ey[m.ticker] + rank_roc[m.ticker])
    for m in metrics:
        m.magic_rank = None
    for i, m in enumerate(combined, start=1):
        m.magic_rank = i


def passes_filters(m: Metrics) -> tuple[bool, list[str]]:
    if m.is_financial:
        return passes_filters_financial(m)
    reasons = []
    if m.net_debt_ebitda is not None and m.net_debt_ebitda > 4:
        reasons.append(f"deuda neta/EBITDA alta ({m.net_debt_ebitda:.1f})")
    if m.interest_coverage is not None and m.interest_coverage < 3:
        reasons.append(f"cobertura de intereses baja ({m.interest_coverage:.1f})")
    if m.roic is not None and m.roic < 0.08:
        reasons.append(f"ROIC bajo ({m.roic:.1%})")
    if m.fcf_yield is not None and m.fcf_yield < 0:
        reasons.append("FCF negativo")
    if m.piotroski is not None and m.piotroski < 5:
        reasons.append(f"Piotroski debil ({m.piotroski}/9)")
    return (len(reasons) == 0, reasons)


def passes_filters_financial(m: Metrics) -> tuple[bool, list[str]]:
    reasons = []
    if m.per is not None and m.per < 0:
        reasons.append("perdidas")
    if m.roe is not None and m.roe < 0.08:
        reasons.append(f"ROE bajo ({m.roe:.1%})")
    if m.equity_assets is not None and m.equity_assets < 0.04:
        reasons.append(f"capital debil ({m.equity_assets:.1%} sobre activos)")
    if m.fin_score is not None and m.fin_score < 4:
        reasons.append(f"score financiero debil ({m.fin_score}/6)")
    return (len(reasons) == 0, reasons)


# ============================================================================
# 4. CAPA DE DATOS
# ============================================================================

class DataProvider:
    name = "base"

    def get_company(self, ticker: str) -> Company:
        raise NotImplementedError


class CachedProvider(DataProvider):
    """Envuelve a otro proveedor y cachea cada empresa en disco con TTL."""

    def __init__(self, inner: DataProvider, cache_dir: str = ".cache",
                 ttl_hours: float = 24.0):
        self.inner = inner
        self.name = inner.name
        self.cache_dir = cache_dir
        self.ttl = ttl_hours * 3600
        os.makedirs(cache_dir, exist_ok=True)

    def _path(self, ticker: str) -> str:
        safe = ticker.replace("/", "_").replace(".", "_")
        return os.path.join(self.cache_dir, f"{self.inner.name}_{safe}.json")

    def get_company(self, ticker: str) -> Company:
        path = self._path(ticker)
        if os.path.exists(path) and (time.time() - os.path.getmtime(path)) < self.ttl:
            with open(path, encoding="utf-8") as f:
                return company_from_dict(json.load(f)["company"])
        company = self.inner.get_company(ticker)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"fetched": time.time(), "company": company_to_dict(company)},
                      f, ensure_ascii=False, indent=2)
        return company


class YFinanceProvider(DataProvider):
    name = "yfinance"

    def get_company(self, ticker: str) -> Company:
        import yfinance as yf
        t = yf.Ticker(ticker)
        info = t.info
        inc, bal, cf = t.income_stmt, t.balance_sheet, t.cashflow

        def pick(df, *labels):
            for lbl in labels:
                if lbl in df.index:
                    return df.loc[lbl]
            return None

        cols = list(inc.columns)[:2][::-1]
        years = []
        for col in cols:
            def v(df, *labels, default=0.0):
                row = pick(df, *labels)
                if row is None or col not in row.index:
                    return default
                val = row[col]
                return float(val) if val == val else default

            revenue = v(inc, "Total Revenue", "Operating Revenue")
            pretax = v(inc, "Pretax Income")
            tax = v(inc, "Tax Provision")
            tax_rate = (tax / pretax) if pretax else 0.21
            years.append(YearData(
                year=col.year,
                revenue=revenue,
                gross_profit=v(inc, "Gross Profit") or (revenue - v(inc, "Cost Of Revenue")),
                ebit=v(inc, "EBIT", "Operating Income"),
                ebitda=v(inc, "EBITDA", "Normalized EBITDA"),
                net_income=v(inc, "Net Income", "Net Income Common Stockholders"),
                interest_expense=abs(v(inc, "Interest Expense")),
                tax_rate=max(0.0, min(tax_rate, 0.5)),
                operating_cash_flow=v(cf, "Operating Cash Flow",
                                      "Cash Flow From Continuing Operating Activities"),
                capex=abs(v(cf, "Capital Expenditure")),
                total_assets=v(bal, "Total Assets"),
                total_debt=v(bal, "Total Debt"),
                long_term_debt=v(bal, "Long Term Debt"),
                cash=v(bal, "Cash And Cash Equivalents",
                       "Cash Cash Equivalents And Short Term Investments"),
                current_assets=v(bal, "Current Assets", "Total Current Assets"),
                current_liabilities=v(bal, "Current Liabilities", "Total Current Liabilities"),
                net_fixed_assets=v(bal, "Net PPE", "Gross PPE"),
                equity=v(bal, "Stockholders Equity", "Total Equity Gross Minority Interest"),
                shares_outstanding=info.get("sharesOutstanding") or v(bal, "Ordinary Shares Number"),
            ))
        return Company(
            ticker=ticker,
            name=info.get("shortName", ticker),
            price=info.get("currentPrice") or info.get("regularMarketPrice") or 0.0,
            currency=info.get("currency", "?"),
            years=years,
            sector=info.get("sector", ""),
        )


class FMPProvider(DataProvider):
    """Financial Modeling Prep. Requiere FMP_API_KEY. Endpoints v3 clasicos.
    NO probado en vivo aqui: valida nombres de campo con tu primera llamada."""
    name = "fmp"
    BASE = "https://financialmodelingprep.com/api/v3"

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ.get("FMP_API_KEY")
        if not self.api_key:
            raise RuntimeError("Falta FMP_API_KEY (export FMP_API_KEY=...)")

    def _get(self, path: str):
        sep = "&" if "?" in path else "?"
        url = f"{self.BASE}/{path}{sep}apikey={self.api_key}"
        try:
            with _urlopen(url, timeout=25) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"FMP HTTP {e.code} en {path}") from e

    # --- Listados de universo ---
    def list_index(self, index: str) -> list[str]:
        ep = {"sp500": "sp500_constituent", "nasdaq": "nasdaq_constituent",
              "dow": "dowjones_constituent"}[index]
        return [r["symbol"] for r in self._get(ep) if r.get("symbol")]

    def list_all(self) -> list[str]:
        # Universo amplio de valores negociables (miles). Requiere plan adecuado.
        return [r["symbol"] for r in self._get("available-traded/list")
                if r.get("symbol") and r.get("type", "stock") == "stock"]

    # --- Fundamentales ---
    def get_company(self, ticker: str) -> Company:
        inc = self._get(f"income-statement/{ticker}?limit=2&period=annual")
        bal = self._get(f"balance-sheet-statement/{ticker}?limit=2&period=annual")
        cf = self._get(f"cash-flow-statement/{ticker}?limit=2&period=annual")
        quote = self._get(f"quote/{ticker}")
        if not inc or not bal or not cf or not quote:
            raise RuntimeError(f"FMP sin datos completos para {ticker}")
        q = quote[0]

        def g(row, *keys, default=0.0):
            for k in keys:
                if k in row and row[k] is not None:
                    return float(row[k])
            return default

        years = []
        for i in range(min(len(inc), len(bal), len(cf)) - 1, -1, -1):
            I, B, C = inc[i], bal[i], cf[i]
            pretax = g(I, "incomeBeforeTax")
            tax = g(I, "incomeTaxExpense")
            tax_rate = (tax / pretax) if pretax else 0.21
            years.append(YearData(
                year=int(str(I.get("calendarYear", str(I.get("date", "0"))[:4]))),
                revenue=g(I, "revenue"),
                gross_profit=g(I, "grossProfit"),
                ebit=g(I, "operatingIncome", "ebit"),
                ebitda=g(I, "ebitda"),
                net_income=g(I, "netIncome"),
                interest_expense=abs(g(I, "interestExpense")),
                tax_rate=max(0.0, min(tax_rate, 0.5)),
                operating_cash_flow=g(C, "operatingCashFlow",
                                      "netCashProvidedByOperatingActivities"),
                capex=abs(g(C, "capitalExpenditure")),
                total_assets=g(B, "totalAssets"),
                total_debt=g(B, "totalDebt"),
                long_term_debt=g(B, "longTermDebt"),
                cash=g(B, "cashAndCashEquivalents", "cashAndShortTermInvestments"),
                current_assets=g(B, "totalCurrentAssets"),
                current_liabilities=g(B, "totalCurrentLiabilities"),
                net_fixed_assets=g(B, "propertyPlantEquipmentNet"),
                equity=g(B, "totalStockholdersEquity"),
                shares_outstanding=g(q, "sharesOutstanding") or g(I, "weightedAverageShsOut"),
            ))
        try:
            prof = self._get(f"profile/{ticker}")
            sector = prof[0].get("sector", "") if prof else ""
        except Exception:
            sector = ""            # sin profile, is_financial usa el heuristico
        return Company(ticker=ticker, name=q.get("name", ticker),
                       price=g(q, "price"), currency="USD", years=years,
                       sector=sector)


class EdgarProvider(DataProvider):
    """SEC EDGAR (XBRL companyfacts): fuente oficial y gratuita para cotizadas
    en EE.UU. Sin API key; la SEC exige identificarse en el User-Agent (se
    puede personalizar con la variable de entorno EDGAR_UA) y tolera ~10
    llamadas/segundo. El precio no esta en EDGAR: se toma de stooq.com y, si
    falla, de yfinance."""
    name = "edgar"
    TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
    FORMS_ANUALES = ("10-K", "20-F", "40-F")

    def __init__(self, cache_dir: str = ".cache"):
        self.ua = os.environ.get(
            "EDGAR_UA",
            "value_screener/1.0 (+https://github.com/prt9yzq2pr-creator/value-screener)")
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)
        self._map: Optional[dict] = None
        self._order: list[str] = []
        self._price_cache: dict[str, float] = {}

    def prefetch_prices(self, tickers: list[str], chunk: int = 200) -> None:
        """Precarga precios en lote via yfinance (una peticion por ~200
        valores). Evita miles de llamadas sueltas a stooq/Yahoo, que acaban
        en limite diario o bloqueo."""
        try:
            import yfinance as yf
        except ImportError:
            return
        tks = [t.upper().replace(".", "-") for t in tickers
               if t.upper().replace(".", "-") not in self._price_cache]
        for i in range(0, len(tks), chunk):
            batch = tks[i:i + chunk]
            try:
                data = yf.download(batch, period="1d", progress=False,
                                   auto_adjust=True)["Close"]
            except Exception as e:
                print(f"[!] precarga de precios (lote {i//chunk + 1}): {e}",
                      file=sys.stderr)
                continue
            for tk in batch:
                try:
                    col = data[tk] if tk in getattr(data, "columns", []) else data
                    v = float(col.dropna().iloc[-1])
                    if v > 0:
                        self._price_cache[tk] = v
                except Exception:
                    continue
        print(f"  precios precargados: {len(self._price_cache)}/{len(tickers)}",
              file=sys.stderr)

    def _get_json(self, url: str):
        req = urllib.request.Request(url, headers={"User-Agent": self.ua})
        try:
            with _urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"EDGAR HTTP {e.code} en {url}") from e

    # --- mapa ticker -> CIK (viene ordenado aprox. por capitalizacion) ---
    def _ticker_map(self) -> dict:
        if self._map is not None:
            return self._map
        path = os.path.join(self.cache_dir, "edgar_company_tickers.json")
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < 7 * 86400:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        else:
            try:
                data = self._get_json(self.TICKERS_URL)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f)
            except RuntimeError as e:
                # www.sec.gov bloquea IPs de centros de datos (GitHub Actions);
                # un mapa viejo es mejor que ninguno: los CIK apenas cambian.
                if os.path.exists(path):
                    print(f"[!] mapa de tickers no actualizable ({e}); "
                          "usando copia local", file=sys.stderr)
                    with open(path, encoding="utf-8") as f:
                        data = json.load(f)
                else:
                    raise
        self._map = {}
        self._order = []
        for r in data.values():
            tk = r["ticker"].upper()
            if tk not in self._map:
                self._map[tk] = (int(r["cik_str"]), r["title"])
                self._order.append(tk)
        return self._map

    def list_us(self, limit: Optional[int] = None) -> list[str]:
        self._ticker_map()
        return self._order[:limit] if limit else list(self._order)

    # --- series anuales de un concepto XBRL: {fecha_cierre: valor} ---
    def _series(self, facts: dict, taxo: str, tag: str,
                annual_only: bool = True) -> dict:
        node = facts.get(taxo, {}).get(tag)
        if not node:
            return {}
        units = node.get("units", {})
        # Solo USD (o acciones): mezclar JPY/BRL/EUR con precio en dolares
        # produce ratios absurdos (ej. Mizuho PER 0.02).
        entries = units.get("USD") or units.get("shares") or []
        best: dict = {}
        for e in entries:
            end, val = e.get("end"), e.get("val")
            if end is None or val is None:
                continue
            if annual_only:
                if not str(e.get("form", "")).startswith(self.FORMS_ANUALES):
                    continue
                start = e.get("start")
                if start:                      # concepto de flujo: exigir ~1 año
                    try:
                        days = (_date.fromisoformat(end)
                                - _date.fromisoformat(start)).days
                    except ValueError:
                        continue
                    if not 320 <= days <= 380:
                        continue
            filed = e.get("filed", "")
            if end not in best or filed >= best[end][1]:
                best[end] = (float(val), filed)
        return {k: v[0] for k, v in best.items()}

    def _merged(self, facts: dict, *tags: str) -> dict:
        out: dict = {}
        for tag in reversed(tags):             # el primer tag tiene prioridad
            out.update(self._series(facts, "us-gaap", tag))
        return out

    def get_company(self, ticker: str) -> Company:
        tk = ticker.upper().replace(".", "-")
        m = self._ticker_map()
        if tk not in m:
            raise RuntimeError(
                f"{ticker} no esta en SEC EDGAR (solo cotizadas en EE.UU.; "
                "para otros mercados usa --yahoo)")
        cik, title = m[tk]
        facts = self._get_json(
            f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
        ).get("facts", {})

        probe = (facts.get("us-gaap", {}).get("Assets")
                 or facts.get("us-gaap", {}).get("NetIncomeLoss"))
        if probe and "USD" not in probe.get("units", {}):
            cur = next(iter(probe.get("units", {})), "?")
            raise RuntimeError(
                f"{ticker} presenta cuentas en {cur}, no en USD (ADR "
                "extranjero); se omite para no mezclar divisas")

        rev = self._merged(facts, "RevenueFromContractWithCustomerExcludingAssessedTax",
                           "Revenues", "SalesRevenueNet", "RevenuesNetOfInterestExpense")
        cost = self._merged(facts, "CostOfRevenue", "CostOfGoodsAndServicesSold",
                            "CostOfGoodsSold")
        gross = self._merged(facts, "GrossProfit")
        ebit = self._merged(facts, "OperatingIncomeLoss")
        dep = self._merged(facts, "DepreciationDepletionAndAmortization",
                           "DepreciationAndAmortization",
                           "DepreciationAmortizationAndAccretionNet")
        ni = self._merged(facts, "NetIncomeLoss", "ProfitLoss")
        interest = self._merged(facts, "InterestExpense", "InterestExpenseDebt",
                                "InterestExpenseNonoperating")
        pretax = self._merged(facts, "IncomeLossFromContinuingOperationsBeforeIncomeTax"
                              "esExtraordinaryItemsNoncontrollingInterest",
                              "IncomeLossFromContinuingOperationsBeforeIncomeTaxes"
                              "MinorityInterestAndIncomeLossFromEquityMethodInvestments")
        tax = self._merged(facts, "IncomeTaxExpenseBenefit")
        ocf = self._merged(facts, "NetCashProvidedByUsedInOperatingActivities",
                           "NetCashProvidedByUsedInOperatingActivitiesContinuing"
                           "Operations")
        capex = self._merged(facts, "PaymentsToAcquirePropertyPlantAndEquipment",
                             "PaymentsToAcquireProductiveAssets")
        assets = self._merged(facts, "Assets")
        ltd_nc = self._merged(facts, "LongTermDebtNoncurrent")
        ltd_c = self._merged(facts, "LongTermDebtCurrent")
        ltd_total = self._merged(facts, "LongTermDebt")
        short_debt = self._merged(facts, "ShortTermBorrowings", "CommercialPaper")
        cash = self._merged(facts, "CashAndCashEquivalentsAtCarryingValue",
                            "CashCashEquivalentsRestrictedCashAndRestrictedCash"
                            "Equivalents")
        ca = self._merged(facts, "AssetsCurrent")
        cl = self._merged(facts, "LiabilitiesCurrent")
        ppe = self._merged(facts, "PropertyPlantAndEquipmentNet")
        equity = self._merged(facts, "StockholdersEquity",
                              "StockholdersEquityIncludingPortionAttributableTo"
                              "NoncontrollingInterest")
        wavg = self._merged(facts, "WeightedAverageNumberOfDilutedSharesOutstanding",
                            "WeightedAverageNumberOfSharesOutstandingBasic")
        dei_shares = {k: v for k, v in self._series(
            facts, "dei", "EntityCommonStockSharesOutstanding",
            annual_only=False).items() if v > 0}

        ends = sorted(ni) or sorted(rev) or sorted(assets)
        ends = [e for e in ends if e in assets or e in equity or e in rev][-2:]
        if not ends:
            raise RuntimeError(f"EDGAR sin datos anuales XBRL para {ticker}")

        def shares_for(end: str, is_last: bool) -> float:
            # El dato de portada (dei) solo vale si es fresco respecto al
            # cierre: emisores multiclase (ej. BRK) dejan de publicarlo
            # agregado y el ultimo valor puede tener muchos años.
            if is_last and dei_shares:
                latest = max(dei_shares)
                age = (_date.fromisoformat(end) - _date.fromisoformat(latest)).days
                if age < 200:
                    return dei_shares[latest]
            return wavg.get(end, 0.0)

        years = []
        for idx, end in enumerate(ends):
            p, t = pretax.get(end, 0.0), tax.get(end, 0.0)
            tax_rate = (t / p) if p else 0.21
            ltd_val = ltd_nc.get(end) or ltd_total.get(end, 0.0)
            total_debt = ((ltd_nc.get(end, 0.0) + ltd_c.get(end, 0.0))
                          or ltd_total.get(end, 0.0)) + short_debt.get(end, 0.0)
            e_ebit = ebit.get(end, 0.0)
            is_last = idx == len(ends) - 1
            years.append(YearData(
                year=int(end[:4]),
                revenue=rev.get(end, 0.0),
                gross_profit=gross.get(end) or (rev.get(end, 0.0) - cost.get(end, 0.0)),
                ebit=e_ebit,
                ebitda=e_ebit + dep.get(end, 0.0) if e_ebit else 0.0,
                net_income=ni.get(end, 0.0),
                interest_expense=abs(interest.get(end, 0.0)),
                tax_rate=max(0.0, min(tax_rate, 0.5)),
                operating_cash_flow=ocf.get(end, 0.0),
                capex=abs(capex.get(end, 0.0)),
                total_assets=assets.get(end, 0.0),
                total_debt=total_debt,
                long_term_debt=ltd_val,
                cash=cash.get(end, 0.0),
                current_assets=ca.get(end, 0.0),
                current_liabilities=cl.get(end, 0.0),
                net_fixed_assets=ppe.get(end, 0.0),
                equity=equity.get(end, 0.0),
                shares_outstanding=shares_for(end, is_last),
            ))

        price = self._price(tk)
        if years[-1].shares_outstanding <= 0:
            years[-1].shares_outstanding = self._shares_fallback(tk, price)
        if years[-1].shares_outstanding <= 0:
            raise RuntimeError(
                f"sin numero de acciones fiable para {ticker} (emisor "
                "multiclase sin dato agregado); mejor descartar que rankear mal")

        sub = self._get_json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
        sector = sub.get("sicDescription") or ""
        if str(sub.get("sic") or "")[:1] == "6":   # SIC 6xxx = finanzas/seguros
            sector = f"Financial - {sector}" if sector else "Financial"

        return Company(ticker=tk, name=title, price=price,
                       currency="USD", years=years, sector=sector)

    def _shares_fallback(self, tk: str, price: float) -> float:
        """Ultimo recurso: capitalizacion de Yahoo / precio. Cubre emisores
        multiclase (BRK, GOOGL...) donde EDGAR no da el agregado."""
        try:
            import yfinance as yf
            fi = yf.Ticker(tk.replace("-", "-")).fast_info
            for k in ("marketCap", "market_cap"):
                try:
                    if fi[k] and price:
                        return float(fi[k]) / price
                except Exception:
                    continue
            for k in ("shares", "sharesOutstanding"):
                try:
                    if fi[k]:
                        return float(fi[k])
                except Exception:
                    continue
        except Exception:
            pass
        return 0.0

    def _price(self, tk: str) -> float:
        if tk in self._price_cache:
            return self._price_cache[tk]
        url = (f"https://stooq.com/q/l/?s={tk.lower()}.us&f=sd2t2ohlcv&h&e=csv")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": self.ua})
            with _urlopen(req, timeout=20) as r:
                lines = r.read().decode().strip().splitlines()
            close = lines[1].split(",")[6]
            if close not in ("N/D", ""):
                return float(close)
        except Exception:
            pass
        try:
            import yfinance as yf
            fi = yf.Ticker(tk).fast_info
            for k in ("lastPrice", "last_price"):
                try:
                    if fi[k]:
                        return float(fi[k])
                except Exception:
                    continue
        except Exception:
            pass
        raise RuntimeError(f"sin precio de cotizacion para {tk} (stooq y yfinance)")


class MockProvider(DataProvider):
    """Empresa sintetica, variada por ticker, para validar la logica sin red."""
    name = "mock"

    def get_company(self, ticker: str) -> Company:
        h = int(hashlib.md5(ticker.encode()).hexdigest(), 16)
        qf = 0.7 + (h % 70) / 100.0          # factor de calidad 0.70..1.39
        pf = 0.6 + (h % 90) / 100.0          # factor de precio 0.60..1.49
        if "BANK" in ticker.upper():
            return self._mock_bank(ticker, qf, pf)
        prior = YearData(
            year=2023, revenue=1000, gross_profit=400, ebit=200, ebitda=260,
            net_income=140, interest_expense=20, tax_rate=0.25,
            operating_cash_flow=180, capex=50, total_assets=1200, total_debt=300,
            long_term_debt=250, cash=100, current_assets=400, current_liabilities=200,
            net_fixed_assets=500, equity=600, shares_outstanding=100,
        )
        latest = YearData(
            year=2024, revenue=1100, gross_profit=460, ebit=230 * qf, ebitda=300 * qf,
            net_income=165 * qf, interest_expense=18, tax_rate=0.25,
            operating_cash_flow=210 * qf, capex=55, total_assets=1250, total_debt=280,
            long_term_debt=230, cash=140, current_assets=440, current_liabilities=200,
            net_fixed_assets=520, equity=680, shares_outstanding=100,
        )
        return Company(ticker=ticker, name=f"Mock {ticker}", price=25.0 * pf,
                       currency="EUR", years=[prior, latest])

    def _mock_bank(self, ticker: str, qf: float, pf: float) -> Company:
        def bank_year(year, ni):
            return YearData(
                year=year, revenue=5000, gross_profit=5000, ebit=0, ebitda=0,
                net_income=ni, interest_expense=3000, tax_rate=0.25,
                operating_cash_flow=-500, capex=100, total_assets=100000,
                total_debt=20000, long_term_debt=18000, cash=8000,
                current_assets=0, current_liabilities=0, net_fixed_assets=1500,
                equity=6000, shares_outstanding=1000,
            )
        return Company(ticker=ticker, name=f"Mock {ticker}", price=8.0 * pf,
                       currency="EUR", years=[bank_year(2023, 700),
                                              bank_year(2024, 800 * qf)],
                       sector="Financial Services")


# ============================================================================
# 5. UNIVERSOS
# ============================================================================

# IBEX 35 (composicion aproximada; ajustar a la vigente o usar FMP como fuente).
IBEX35 = [
    "SAN.MC", "BBVA.MC", "ITX.MC", "IBE.MC", "TEF.MC", "REP.MC", "AMS.MC", "FER.MC",
    "ELE.MC", "AENA.MC", "CLNX.MC", "CABK.MC", "IAG.MC", "ACS.MC", "NTGY.MC", "SAB.MC",
    "MAP.MC", "GRF.MC", "RED.MC", "ENG.MC", "BKT.MC", "MTS.MC", "ANA.MC", "COL.MC",
    "MEL.MC", "LOG.MC", "ROVI.MC", "IDR.MC", "ACX.MC", "CIE.MC", "SLR.MC", "UNI.MC",
    "PUIG.MC", "BKY.MC", "FDR.MC",
]


def load_russell2000(cache_dir: str = ".cache") -> list[str]:
    """Composicion aproximada del Russell 2000 via la cartera del ETF
    Vanguard VTWO, que lo replica (la lista oficial de FTSE Russell es de
    pago). ~1.900 tickers, cacheados 7 dias."""
    path = os.path.join(cache_dir, "russell2000_tickers.json")
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < 7 * 86400:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    os.makedirs(cache_dir, exist_ok=True)
    base = ("https://investor.vanguard.com/investment-products/etfs/profile/"
            "api/VTWO/portfolio-holding/stock")
    tickers: list[str] = []
    start = 1
    while start <= 4001:
        req = urllib.request.Request(
            f"{base}?start={start}&count=500",
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        with _urlopen(req) as r:
            d = json.loads(r.read().decode())
        ents = d.get("fund", {}).get("entity", [])
        for e in ents:
            tk = (e.get("ticker") or "").strip().upper()
            if tk and tk != "-":
                tickers.append(tk)
        if len(ents) < 500:
            break
        start += 500
    if len(tickers) < 1000:
        raise RuntimeError(f"Vanguard VTWO devolvio solo {len(tickers)} "
                           "tickers; API posiblemente cambiada")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(tickers, f)
    return tickers


def _as_fmp(provider: DataProvider) -> FMPProvider:
    p = provider.inner if isinstance(provider, CachedProvider) else provider
    if not isinstance(p, FMPProvider):
        raise RuntimeError("Los universos sp500/nasdaq100/dow/all requieren --fmp")
    return p


def _as_edgar(provider: DataProvider) -> EdgarProvider:
    p = provider.inner if isinstance(provider, CachedProvider) else provider
    if not isinstance(p, EdgarProvider):
        raise RuntimeError("El universo 'usa' requiere el proveedor EDGAR "
                           "(quita --yahoo/--fmp/--mock)")
    return p


def load_universe(source: str, provider: Optional[DataProvider] = None,
                  limit: Optional[int] = None) -> list[str]:
    s = source.strip()
    if os.path.exists(s):
        with open(s, encoding="utf-8") as f:
            tickers = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    else:
        key = s.lower()
        if key == "ibex35":
            tickers = list(IBEX35)
        elif key in ("usa", "us", "edgar"):
            # ~10.000 cotizadas SEC, ordenadas aprox. por capitalizacion:
            # --limit 500 equivale a "las 500 mayores de EE.UU."
            tickers = _as_edgar(provider).list_us()
        elif key in ("russell2000", "russell", "r2000"):
            tickers = load_russell2000()
        elif key in ("sp500", "s&p500", "sp-500"):
            tickers = _as_fmp(provider).list_index("sp500")
        elif key in ("nasdaq100", "nasdaq"):
            tickers = _as_fmp(provider).list_index("nasdaq")
        elif key in ("dow", "dow30", "dowjones"):
            tickers = _as_fmp(provider).list_index("dow")
        elif key in ("all", "market"):
            tickers = _as_fmp(provider).list_all()
        else:
            raise ValueError(
                f"Universo desconocido: '{source}'. Usa un archivo .txt, "
                "usa, ibex35, sp500, nasdaq100, dow o all.")
    seen, out = set(), []
    for x in tickers:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out[:limit] if limit else out


# ============================================================================
# 6. BARRIDO DE UNIVERSO (checkpoint reanudable)
# ============================================================================

def _load_checkpoint(path: str) -> dict:
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_checkpoint(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def screen_universe(provider: DataProvider, tickers: list[str], checkpoint_path: str,
                    rate_per_min: float, refresh_hours: float,
                    csv_path: Optional[str], top: int) -> list[Metrics]:
    ck = _load_checkpoint(checkpoint_path)
    interval = 60.0 / rate_per_min if rate_per_min else 0.0
    pending = [tk for tk in tickers
               if not (ck.get(tk) and "metrics" in ck[tk]
                       and (time.time() - ck[tk].get("ts", 0)) < refresh_hours * 3600)]
    inner = provider.inner if isinstance(provider, CachedProvider) else provider
    if pending and hasattr(inner, "prefetch_prices"):
        print(f"Precargando precios de {len(pending)} valores...", file=sys.stderr)
        inner.prefetch_prices(pending)
    total, fetched = len(tickers), 0
    for i, tk in enumerate(tickers, 1):
        e = ck.get(tk)
        if e and (time.time() - e.get("ts", 0)) < refresh_hours * 3600 and "metrics" in e:
            continue                                   # fresco: no re-descargar
        try:
            m = compute_metrics(provider.get_company(tk))
            ck[tk] = {"ts": time.time(), "metrics": asdict(m)}
        except Exception as ex:
            prev = ck.get(tk)
            if prev and "metrics" in prev:
                # refresco fallido: conservar metricas antiguas y reintentar
                # en el siguiente barrido (no se actualiza el ts)
                prev["error"] = str(ex)
            else:
                ck[tk] = {"ts": time.time(), "error": str(ex)}
            print(f"[!] {tk}: {ex}", file=sys.stderr)
        fetched += 1
        if interval:
            time.sleep(interval)
        if i % 25 == 0:
            _save_checkpoint(checkpoint_path, ck)
            print(f"  {i}/{total} procesados ({fetched} descargados)...", file=sys.stderr)
    _save_checkpoint(checkpoint_path, ck)
    return _rank_and_report(ck, csv_path, top)


def _rank_and_report(ck: dict, csv_path: Optional[str], top: int) -> list[Metrics]:
    metrics = [Metrics(**e["metrics"]) for e in ck.values() if "metrics" in e]
    errors = sum(1 for e in ck.values() if "error" in e)
    magic_formula_rank(metrics)
    financial_rank(metrics)
    ranked = sorted([m for m in metrics if m.magic_rank is not None],
                    key=lambda m: m.magic_rank)
    fin_ranked = sorted([m for m in metrics if m.fin_rank is not None],
                        key=lambda m: m.fin_rank)
    aptas = [m for m in ranked + fin_ranked if passes_filters(m)[0]]

    print(f"\nUniverso procesado: {len(metrics)} con metricas, {errors} con error.")
    print(f"No financieras rankeadas: {len(ranked)}. Financieras: {len(fin_ranked)}. "
          f"Pasan filtros: {len(aptas)}.")
    print(f"\n--- TOP {top} por Magic Formula ( * = pasa filtros de calidad ) ---")
    print(f"{'#':>4} {'Ticker':<11}{'EV/EBIT':>9}{'ROIC':>8}{'FCFyld':>8}{'Piotr':>7}")
    for m in ranked[:top]:
        star = "*" if passes_filters(m)[0] else " "
        print(f"{m.magic_rank:>4}{star}{m.ticker:<10}{_fmt(m.ev_ebit):>9}"
              f"{_fmt(m.roic, pct=True):>8}{_fmt(m.fcf_yield, pct=True):>8}"
              f"{str(m.piotroski):>7}")

    if fin_ranked:
        print(f"\n--- FINANCIERAS: TOP {top} por barato+rentable (PER/ROE) ---")
        print(f"{'#':>4} {'Ticker':<11}{'PER':>7}{'P/B':>7}{'ROE':>8}{'ROA':>7}"
              f"{'Cap/Act':>9}{'Score':>7}")
        for m in fin_ranked[:top]:
            star = "*" if passes_filters(m)[0] else " "
            print(f"{m.fin_rank:>4}{star}{m.ticker:<10}{_fmt(m.per):>7}{_fmt(m.pb):>7}"
                  f"{_fmt(m.roe, pct=True):>8}{_fmt(m.roa, pct=True):>7}"
                  f"{_fmt(m.equity_assets, pct=True):>9}"
                  f"{(str(m.fin_score) + '/6') if m.fin_score is not None else 'n/d':>7}")

    if csv_path:
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["magic_rank", "ticker", "name", "pasa_filtros", "per", "pb",
                        "ev_ebit", "ev_ebitda", "fcf_yield", "earnings_yield", "roic",
                        "roc_greenblatt", "roe", "net_debt_ebitda", "interest_coverage",
                        "current_ratio", "piotroski",
                        "es_financiera", "fin_rank", "roa", "capital_activos", "fin_score"])
            for m in ranked + fin_ranked:
                w.writerow([m.magic_rank, m.ticker, m.name, passes_filters(m)[0], m.per,
                            m.pb, m.ev_ebit, m.ev_ebitda, m.fcf_yield, m.earnings_yield,
                            m.roic, m.roc_greenblatt, m.roe, m.net_debt_ebitda,
                            m.interest_coverage, m.current_ratio, m.piotroski,
                            m.is_financial, m.fin_rank, m.roa, m.equity_assets,
                            m.fin_score])
        print(f"\nRanking completo guardado en {csv_path}")
    return ranked + fin_ranked


# ============================================================================
# 7. PRESENTACION (ficha individual)
# ============================================================================

def _fmt(v, pct=False):
    if v is None:
        return "n/d"
    return f"{v:.1%}" if pct else f"{v:.2f}"


def print_financial_analysis(m: Metrics, c: Company) -> None:
    apt, reasons = passes_filters(m)
    print(f"\n{'='*60}\n{m.name} ({m.ticker})  ·  {c.price:.2f} {c.currency}"
          f"  ·  FINANCIERA\n{'='*60}")
    print("VALORACION")
    print(f"  PER {_fmt(m.per)} | P/B {_fmt(m.pb)}")
    print("RENTABILIDAD")
    print(f"  ROE {_fmt(m.roe, pct=True)} | ROA {_fmt(m.roa, pct=True)} | "
          f"Margen neto {_fmt(m.net_margin, pct=True)}")
    print("SOLVENCIA")
    print(f"  Capital/Activos {_fmt(m.equity_assets, pct=True)}")
    print(f"SCORE FINANCIERO: {m.fin_score}/6" if m.fin_score is not None
          else "SCORE FINANCIERO: n/d (falta año previo)")
    print("  (EV/EBIT, ROIC, deuda neta y Magic Formula no aplican a financieras)")
    print(f"VEREDICTO: {'APTA' if apt else 'DESCARTE -> ' + '; '.join(reasons)}")


def print_analysis(m: Metrics, c: Company) -> None:
    if m.is_financial:
        return print_financial_analysis(m, c)
    apt, reasons = passes_filters(m)
    print(f"\n{'='*60}\n{m.name} ({m.ticker})  ·  {c.price:.2f} {c.currency}\n{'='*60}")
    print("VALORACION")
    print(f"  PER {_fmt(m.per)} | P/B {_fmt(m.pb)} | EV/EBIT {_fmt(m.ev_ebit)} "
          f"| EV/EBITDA {_fmt(m.ev_ebitda)}")
    print(f"  FCF yield {_fmt(m.fcf_yield, pct=True)} | Earnings yield "
          f"{_fmt(m.earnings_yield, pct=True)}")
    print("CALIDAD")
    print(f"  ROIC {_fmt(m.roic, pct=True)} | ROC(Greenblatt) {_fmt(m.roc_greenblatt, pct=True)} "
          f"| ROE {_fmt(m.roe, pct=True)}")
    print(f"  Margenes  bruto {_fmt(m.gross_margin, pct=True)} / operativo "
          f"{_fmt(m.operating_margin, pct=True)} / neto {_fmt(m.net_margin, pct=True)}")
    print("SOLIDEZ")
    print(f"  Deuda neta/EBITDA {_fmt(m.net_debt_ebitda)} | Cobertura intereses "
          f"{_fmt(m.interest_coverage)} | Current ratio {_fmt(m.current_ratio)}")
    print("CAJA / CALIDAD")
    print(f"  Conversion FCF/Bº neto {_fmt(m.fcf_conversion, pct=True)} | "
          f"Piotroski {m.piotroski}/9")
    print(f"VEREDICTO: {'APTA' if apt else 'DESCARTE -> ' + '; '.join(reasons)}")


def screen(provider: DataProvider, tickers: list[str], csv_path: Optional[str]) -> None:
    metrics = []
    for tk in tickers:
        try:
            metrics.append(compute_metrics(provider.get_company(tk)))
        except Exception as e:
            print(f"[!] {tk}: {e}", file=sys.stderr)
    _rank_and_report({m.ticker: {"metrics": asdict(m)} for m in metrics}, csv_path, len(metrics))


# ============================================================================
# 8. CLI
# ============================================================================

def build_provider(args) -> DataProvider:
    if args.mock:
        return MockProvider()
    if args.fmp:
        base: DataProvider = FMPProvider()
    elif args.yahoo:
        base = YFinanceProvider()
    else:
        base = EdgarProvider()                 # fuente principal: SEC EDGAR
    return base if args.no_cache else CachedProvider(base, ttl_hours=args.cache_ttl)


def main():
    ap = argparse.ArgumentParser(description="Screener + analizador value investing")
    ap.add_argument("mode", choices=["analyze", "screen", "universe", "update"])
    ap.add_argument("tickers", nargs="*")
    ap.add_argument("--universe", help="usa | russell2000 | ibex35 | sp500 | nasdaq100 | dow | all | archivo.txt")
    ap.add_argument("--limit", type=int, help="tope de tickers del universo")
    ap.add_argument("--top", type=int, default=25, help="cuantos mostrar en el ranking")
    ap.add_argument("--rate", type=float, default=60.0, help="llamadas por minuto")
    ap.add_argument("--refresh", type=float, default=24.0, help="horas antes de re-descargar en universo/update")
    ap.add_argument("--checkpoint", default="universe_metrics.json")
    ap.add_argument("--csv", default=None)
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--fmp", action="store_true")
    ap.add_argument("--yahoo", action="store_true",
                    help="usar yfinance en vez de SEC EDGAR (mercados no USA)")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--cache-ttl", type=float, default=24.0)
    args = ap.parse_args()

    try:
        provider = build_provider(args)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.mode == "analyze":
        for tk in args.tickers:
            try:
                c = provider.get_company(tk)
                print_analysis(compute_metrics(c), c)
            except Exception as e:
                print(f"[!] {tk}: {e}", file=sys.stderr)

    elif args.mode == "screen":
        if not args.tickers:
            print("Pasa tickers, o usa 'universe --universe ...'", file=sys.stderr)
            sys.exit(1)
        screen(provider, args.tickers, args.csv)

    elif args.mode == "universe":
        if not args.universe:
            print("Indica --universe (ibex35 | sp500 | archivo.txt | ...)", file=sys.stderr)
            sys.exit(1)
        try:
            tickers = load_universe(args.universe, provider, args.limit)
        except (ValueError, RuntimeError) as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        print(f"Universo '{args.universe}': {len(tickers)} valores.", file=sys.stderr)
        screen_universe(provider, tickers, args.checkpoint, args.rate,
                        args.refresh, args.csv, args.top)

    elif args.mode == "update":
        ck = _load_checkpoint(args.checkpoint)
        if not ck:
            print("No hay checkpoint. Ejecuta primero 'universe'.", file=sys.stderr)
            sys.exit(1)
        tickers = list(ck.keys())
        print(f"Refrescando {len(tickers)} valores (obsoletos > {args.refresh}h)...",
              file=sys.stderr)
        screen_universe(provider, tickers, args.checkpoint, args.rate,
                        args.refresh, args.csv, args.top)


if __name__ == "__main__":
    main()
