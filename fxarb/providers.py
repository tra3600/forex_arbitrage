"""Fournisseurs de taux de change accessibles via le web.

Sans clé API : frankfurter (BCE), erapi (open.er-api.com), fawaz (currency-api
sur CDN), yahoo (endpoint non officiel de Yahoo Finance).
Avec clé (variable d'environnement) : twelvedata, alphavantage, exchangerateapi.
`generic` reproduit l'ancien comportement (URL + Bearer + JSON {"rate": ...}).
"""
from __future__ import annotations

import logging
import os
import time
from typing import Callable

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .models import Quote, split_pair, normalize_pair

log = logging.getLogger("fxarb.providers")

DEFAULT_TIMEOUT = 8.0
USER_AGENT = "fxarb/1.0 (+https://github.com/tra3600/forex_arbitrage)"


class ProviderError(Exception):
    pass


def make_session(retries: int = 2) -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=retries,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        respect_retry_after_header=True,
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    s.headers["User-Agent"] = USER_AGENT
    return s


class Provider:
    name = "base"
    requires_key: str | None = None  # nom de la variable d'environnement

    def __init__(self, session: requests.Session | None = None, timeout: float = DEFAULT_TIMEOUT,
                 api_key: str | None = None):
        self.session = session or make_session()
        self.timeout = timeout
        self.api_key = api_key or (os.environ.get(self.requires_key) if self.requires_key else None)
        if self.requires_key and not self.api_key:
            raise ProviderError(f"{self.name}: clé API manquante (variable {self.requires_key})")
        self._table_cache: dict[str, tuple[float, dict]] = {}

    # -- HTTP --------------------------------------------------------------
    def _get_json(self, url: str, **kwargs):
        try:
            r = self.session.get(url, timeout=self.timeout, **kwargs)
        except requests.RequestException as e:
            raise ProviderError(f"{self.name}: erreur réseau ({e.__class__.__name__}: {e})") from e
        if r.status_code != 200:
            raise ProviderError(f"{self.name}: HTTP {r.status_code} pour {url}")
        try:
            return r.json()
        except ValueError as e:
            raise ProviderError(f"{self.name}: réponse JSON invalide") from e

    # -- API publique ------------------------------------------------------
    def get_quote(self, pair: str) -> Quote:
        raise NotImplementedError

    def get_quotes(self, pairs: list[str]) -> dict[str, Quote]:
        """Une cotation par paire ; les paires en échec sont ignorées (loguées)."""
        out: dict[str, Quote] = {}
        for pair in pairs:
            try:
                out[pair] = self.get_quote(pair)
            except ProviderError as e:
                log.warning("%s", e)
        return out

    @staticmethod
    def _mid_quote(name: str, pair: str, rate) -> Quote:
        try:
            rate = float(rate)
        except (TypeError, ValueError) as e:
            raise ProviderError(f"{name}: taux invalide {rate!r} pour {pair}") from e
        if not rate > 0:
            raise ProviderError(f"{name}: taux non positif {rate!r} pour {pair}")
        return Quote(name, pair, rate, rate, has_spread=False)


class TableProvider(Provider):
    """Fournisseur qui renvoie tous les taux d'une devise de base en un appel."""

    table_ttl = 5.0

    def _fetch_table(self, base: str) -> dict[str, float]:
        raise NotImplementedError

    def _table(self, base: str) -> dict[str, float]:
        now = time.time()
        cached = self._table_cache.get(base)
        if cached and now - cached[0] < self.table_ttl:
            return cached[1]
        table = self._fetch_table(base)
        self._table_cache[base] = (now, table)
        return table

    def get_quote(self, pair: str) -> Quote:
        base, quote = split_pair(pair)
        table = self._table(base)
        if quote not in table:
            raise ProviderError(f"{self.name}: devise {quote} absente de la réponse")
        return self._mid_quote(self.name, normalize_pair(pair), table[quote])


class Frankfurter(TableProvider):
    """Taux de référence BCE (mis à jour 1x/jour ouvré) - utile comme référence."""
    name = "frankfurter"
    url = "https://api.frankfurter.dev/v1/latest"

    def _fetch_table(self, base):
        data = self._get_json(self.url, params={"base": base})
        rates = data.get("rates")
        if not isinstance(rates, dict):
            raise ProviderError("frankfurter: champ 'rates' absent")
        return {k.upper(): v for k, v in rates.items()}


class ErApi(TableProvider):
    name = "erapi"
    url = "https://open.er-api.com/v6/latest/{base}"

    def _fetch_table(self, base):
        data = self._get_json(self.url.format(base=base))
        if data.get("result") != "success" or not isinstance(data.get("rates"), dict):
            raise ProviderError(f"erapi: réponse en erreur ({data.get('error-type', 'inconnue')})")
        return {k.upper(): v for k, v in data["rates"].items()}


class FawazCurrency(TableProvider):
    """fawazahmed0/currency-api, servi par CDN (avec miroir de secours)."""
    name = "fawaz"
    urls = (
        "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/{base}.json",
        "https://latest.currency-api.pages.dev/v1/currencies/{base}.json",
    )

    def _fetch_table(self, base):
        last: Exception | None = None
        for url in self.urls:
            try:
                data = self._get_json(url.format(base=base.lower()))
                rates = data.get(base.lower())
                if isinstance(rates, dict):
                    return {k.upper(): v for k, v in rates.items()}
                last = ProviderError("fawaz: table absente")
            except ProviderError as e:
                last = e
        raise ProviderError(str(last))


class ExchangeRateApi(Provider):
    """v6.exchangerate-api.com (clé gratuite requise)."""
    name = "exchangerateapi"
    requires_key = "EXCHANGERATE_API_KEY"
    url = "https://v6.exchangerate-api.com/v6/{key}/pair/{base}/{quote}"

    def get_quote(self, pair):
        base, quote = split_pair(pair)
        data = self._get_json(self.url.format(key=self.api_key, base=base, quote=quote))
        if data.get("result") != "success":
            raise ProviderError(f"exchangerateapi: {data.get('error-type', 'erreur')}")
        return self._mid_quote(self.name, normalize_pair(pair), data.get("conversion_rate"))


class Yahoo(Provider):
    """Endpoint chart de Yahoo Finance (non officiel, quasi temps réel, mid)."""
    name = "yahoo"
    url = "https://query1.finance.yahoo.com/v8/finance/chart/{pair}=X"

    def get_quote(self, pair):
        pair = normalize_pair(pair)
        data = self._get_json(self.url.format(pair=pair), params={"interval": "1m", "range": "1d"})
        try:
            meta = data["chart"]["result"][0]["meta"]
            price = meta["regularMarketPrice"]
        except (KeyError, IndexError, TypeError) as e:
            raise ProviderError(f"yahoo: format inattendu pour {pair}") from e
        return self._mid_quote(self.name, pair, price)


class TwelveData(Provider):
    name = "twelvedata"
    requires_key = "TWELVEDATA_API_KEY"
    url = "https://api.twelvedata.com/exchange_rate"

    def get_quote(self, pair):
        base, quote = split_pair(pair)
        data = self._get_json(self.url, params={"symbol": f"{base}/{quote}", "apikey": self.api_key})
        if data.get("status") == "error" or "rate" not in data:
            raise ProviderError(f"twelvedata: {data.get('message', 'réponse sans taux')}")
        return self._mid_quote(self.name, normalize_pair(pair), data["rate"])


class AlphaVantage(Provider):
    """CURRENCY_EXCHANGE_RATE : fournit bid et ask (limite 25 req/jour en gratuit)."""
    name = "alphavantage"
    requires_key = "ALPHAVANTAGE_API_KEY"
    url = "https://www.alphavantage.co/query"

    def get_quote(self, pair):
        base, quote = split_pair(pair)
        data = self._get_json(self.url, params={
            "function": "CURRENCY_EXCHANGE_RATE", "from_currency": base,
            "to_currency": quote, "apikey": self.api_key})
        node = data.get("Realtime Currency Exchange Rate")
        if not node:
            raise ProviderError(f"alphavantage: {data.get('Note') or data.get('Information') or 'réponse vide'}")
        try:
            bid, ask = float(node["8. Bid Price"]), float(node["9. Ask Price"])
            if bid > 0 and ask > 0 and bid <= ask:
                return Quote(self.name, normalize_pair(pair), bid, ask, has_spread=True)
        except (KeyError, ValueError):
            pass
        return self._mid_quote(self.name, normalize_pair(pair), node.get("5. Exchange Rate"))


class Generic(Provider):
    """Compatibilité avec l'ancien script : GET {url}/{PAIR}, en-tête Bearer, JSON {"rate": x}
    (ou {"bid": x, "ask": y}). Configuré par FX_GENERIC_URL / FX_GENERIC_KEY."""
    name = "generic"

    def __init__(self, *a, url: str | None = None, **kw):
        super().__init__(*a, **kw)
        self.base_url = url or os.environ.get("FX_GENERIC_URL")
        if not self.base_url:
            raise ProviderError("generic: définir FX_GENERIC_URL")
        self.api_key = self.api_key or os.environ.get("FX_GENERIC_KEY")

    def get_quote(self, pair):
        pair = normalize_pair(pair)
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        data = self._get_json(f"{self.base_url.rstrip('/')}/{pair}", headers=headers)
        if "bid" in data and "ask" in data:
            try:
                return Quote(self.name, pair, float(data["bid"]), float(data["ask"]), has_spread=True)
            except (TypeError, ValueError) as e:
                raise ProviderError("generic: bid/ask invalides") from e
        return self._mid_quote(self.name, pair, data.get("rate"))


PROVIDERS: dict[str, Callable[..., Provider]] = {
    "frankfurter": Frankfurter,
    "erapi": ErApi,
    "fawaz": FawazCurrency,
    "yahoo": Yahoo,
    "exchangerateapi": ExchangeRateApi,
    "twelvedata": TwelveData,
    "alphavantage": AlphaVantage,
    "generic": Generic,
}
DEFAULT_PROVIDERS = ["erapi", "fawaz", "yahoo", "frankfurter"]


def build_providers(names: list[str], **kw) -> list[Provider]:
    out = []
    for n in names:
        n = n.strip().lower()
        if n not in PROVIDERS:
            raise ValueError(f"Fournisseur inconnu : {n} (disponibles : {', '.join(PROVIDERS)})")
        try:
            out.append(PROVIDERS[n](**kw))
        except ProviderError as e:
            log.warning("Fournisseur ignoré : %s", e)
    return out
