# Value Screener — EE.UU.

Screener de value investing sobre las 500 mayores cotizadas de EE.UU. más el
Russell 2000 (~2.400 empresas), con datos oficiales de SEC EDGAR (XBRL).

**Ranking web**: publicado con GitHub Pages desde `docs/`, actualizado
automáticamente cada lunes por GitHub Actions.

- Ranking Magic Formula (Greenblatt) con filtros de calidad (deuda, cobertura
  de intereses, ROIC, FCF, Piotroski).
- Financieras (bancos/aseguradoras) en ranking separado: PER, ROE, ROA,
  solvencia y score propio 0-6.
- Solo cuentas anuales presentadas a la SEC en USD; los ADR en divisa local se
  excluyen. Precios de mercado de Yahoo Finance.

## Uso local

```bash
pip install yfinance certifi
python value_screener.py analyze AAPL JPM          # ficha individual
python value_screener.py universe --universe usa --limit 500 --csv ranking.csv
python value_screener.py update --refresh 168      # refrescar lo obsoleto
python build_site.py                               # regenerar docs/data.json
```

Esto es un embudo cuantitativo, no asesoramiento de inversión.
