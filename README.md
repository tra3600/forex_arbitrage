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
python forex_arbitrage.py --triangular --pairs EURUSD,GBPUSD,USDJPY   # + arbitrage triangulaire
python forex_arbitrage.py --once --json                # état complet en JSON
python forex_arbitrage.py --log-file opportunites.jsonl
```

Options aussi disponibles en variables d'environnement : `FX_PAIRS`, `FX_PROVIDERS`, `FX_INTERVAL`, `FX_FEE_BPS`,
`FX_MIN_NET_BPS`, `FX_MAX_AGE`. Le tableau de bord expose `/` (HTML), `/api/state` (JSON) et `/health`.

## Arbitrage triangulaire

Avec `--triangular`, les paires croisées entre les devises citées sont ajoutées automatiquement (EURUSD, GBPUSD,
USDJPY → + EURGBP, EURJPY, GBPJPY). Pour chaque triplet de devises, le programme teste les deux sens
A → B → C → A avec, pour chaque jambe, le meilleur prix exécutable parmi toutes les sources (bid le plus haut pour
vendre, ask le plus bas pour acheter). Le coût est de 3 × `--tri-leg-fee-bps` (0,5 bps par défaut).
Les opportunités apparaissent dans les logs, le JSONL (`"type": "triangular"`), `/api/state` et le dashboard.
Avec des cours milieu issus d'une même table, le produit des taux vaut 1 : les opportunités viennent surtout de
l'écart entre sources ou d'un vrai bid/ask (alphavantage, `generic`) et sont souvent des écarts de fraîcheur.

## Alertes Instagram

```bash
export INSTAGRAM_ACCESS_TOKEN=...   # jeton de l'API Instagram (jamais dans le code ni dans git)
export INSTAGRAM_RECIPIENT_ID=...   # IGSID du destinataire
python forex_arbitrage.py --triangular --alert-instagram --alert-min-bps 3 --alert-cooldown 300
```

Chaque opportunité (directe ou triangulaire) est envoyée en message direct via l'API officielle Instagram Messaging
(`POST graph.instagram.com/{version}/me/messages`), en tâche de fond pour ne pas ralentir les cycles ; une erreur
d'envoi est loguée sans arrêter le programme. Anti-spam : la même opportunité n'est renvoyée qu'après
`--alert-cooldown` s, sauf si l'écart net grossit de 50 %. Contraintes imposées par Meta : compte Instagram
**professionnel** lié à une app Meta (permission `instagram_business_manage_messages`), et le destinataire doit avoir
écrit au compte dans les dernières 24 h, sinon l'API refuse l'envoi. `INSTAGRAM_API_VERSION` (défaut `v21.0`) est réglable.

## Alertes Telegram

Plus simple qu'Instagram : pas de compte pro ni de fenêtre de 24 h. Créez un bot avec @BotFather, écrivez-lui un
message, puis récupérez votre `chat_id` (`https://api.telegram.org/bot<TOKEN>/getUpdates`).

```bash
export TELEGRAM_BOT_TOKEN=...   # jeton donné par @BotFather
export TELEGRAM_CHAT_ID=...
python forex_arbitrage.py --triangular --alert-telegram              # combinable avec --alert-instagram
```

Mêmes options anti-spam que pour Instagram (`--alert-cooldown`, `--alert-min-bps`).

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
