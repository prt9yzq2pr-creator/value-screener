#!/bin/zsh
# Actualizacion semanal del value screener: refresca datos de EDGAR,
# regenera la web y publica en GitHub Pages.
# La SEC bloquea las IPs de la nube (GitHub Actions), asi que este ciclo
# tiene que ejecutarse desde una IP residencial (este Mac).

set -e
export PATH="/Library/Frameworks/Python.framework/Versions/3.14/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
cd "$(dirname "$0")"

echo "=== $(date) Actualizando value screener ==="

/usr/bin/caffeinate -i python3 value_screener.py update \
    --refresh 144 --rate 120 --csv ranking_completo.csv

python3 build_site.py

git add universe_metrics.json docs/data.json ranking_completo.csv
git commit -m "Actualizacion semanal del ranking" || { echo "Sin cambios"; exit 0; }
git push

echo "=== $(date) Publicado ==="
