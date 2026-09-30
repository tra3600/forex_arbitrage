# forex_arbitrage

Détecteur d'arbitrage Forex : il interroge en parallèle plusieurs sources de cotations sur le web, compare les prix
(acheter à l'ask le plus bas, vendre au bid le plus haut), retranche les frais et signale les opportunités.
**Lecture seule : aucun ordre n'est passé.** Un tableau de bord web optionnel est inclus.

## Installation

```bash
pip install -r requirements.txt   # seule dépendance : requests
python -m unittest discover -s tests   # tests hors-ligne
```

## Utilisation

```bash
python forex_arbitrage.py --once                       # un cycle, sortie console
python forex_arbitrage.py --pairs EURUSD,GBPUSD --interval 10 --fee-bps 1.5
python forex_arbitrage.py --web --port 8000            # dashboard http://127.0.0.1:8000
python forex_arbitrage.py --once --json                # état complet en JSON
python forex_arbitrage.py --log-file opportunites.jsonl
```

Options aussi disponibles en variables d'environnement : `FX_PAIRS`, `FX_PROVIDERS`, `FX_INTERVAL`, `FX_FEE_BPS`,
`FX_MIN_NET_BPS`, `FX_MAX_AGE`. Le tableau de bord expose `/` (HTML), `/api/state` (JSON) et `/health`.

## Fournisseurs (API web)

| Nom | Clé | Remarque |
|---|---|---|
| `erapi` | non | open.er-api.com, mid |
| `fawaz` | non | currency-api sur CDN jsDelivr + miroir de secours, mid |
| `yahoo` | non | endpoint Yahoo Finance non officiel, quasi temps réel, mid |
| `frankfurter` | non | taux BCE, 1 fois par jour : référence, pas du temps réel |
| `exchangerateapi` | `EXCHANGERATE_API_KEY` | v6.exchangerate-api.com |
| `twelvedata` | `TWELVEDATA_API_KEY` | mid |
| `alphavantage` | `ALPHAVANTAGE_API_KEY` | fournit bid et ask (25 req/jour en gratuit) |
| `generic` | `FX_GENERIC_URL`, `FX_GENERIC_KEY` | ancien format : `GET {url}/EURUSD`, Bearer, JSON `{"rate": x}` ou `{"bid","ask"}` |

Les fournisseurs sans clé sont ignorés avec un avertissement ; une panne réseau, un HTTP non 200 ou un JSON invalide
sur une source n'arrête pas les autres (timeouts, retries avec backoff sur 429/5xx).
Ajouter une source : sous-classer `Provider` (ou `TableProvider`) dans `fxarb/providers.py` et l'enregistrer dans `PROVIDERS`.

## À savoir avant de trader

- La plupart des API gratuites ne donnent qu'un cours milieu et sont agrégées/retardées : un écart entre deux d'entre
  elles est souvent un écart de fraîcheur, pas une vraie opportunité exécutable. Réglez `--fee-bps` (spread, commission,
  slippage) de façon réaliste.
- Les clés API se passent par variables d'environnement, jamais dans le code.
- Respectez les conditions d'utilisation des API (quotas) et la réglementation locale du trading algorithmique.
