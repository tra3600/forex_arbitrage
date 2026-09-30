from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .models import Opportunity, Quote, normalize_pair
from .providers import Provider

log = logging.getLogger("fxarb.engine")


def find_opportunity(pair: str, quotes: list[Quote], fee_bps: float = 0.0,
                     min_net_bps: float = 0.0) -> Opportunity | None:
    """Meilleur arbitrage entre fournisseurs : acheter à l'ask le plus bas, vendre au bid le plus haut.

    `fee_bps` = coût aller-retour total (spread, commissions, slippage) retranché de l'écart brut.
    Renvoie None si moins de 2 fournisseurs, ou si l'écart net est <= min_net_bps.
    """
    best: Opportunity | None = None
    for buy in quotes:
        for sell in quotes:
            if buy.provider == sell.provider:
                continue
            gross = (sell.bid - buy.ask) / buy.ask * 1e4
            net = gross - fee_bps
            if net > min_net_bps and (best is None or net > best.net_bps):
                best = Opportunity(pair, buy.provider, buy.ask, sell.provider, sell.bid, gross, net)
    return best


class Engine:
    """Interroge tous les fournisseurs en parallèle et conserve le dernier état (pour le web)."""

    def __init__(self, providers: list[Provider], pairs: list[str], fee_bps: float = 0.0,
                 min_net_bps: float = 0.0, max_age: float = 120.0, max_history: int = 200):
        if not providers:
            raise ValueError("Aucun fournisseur utilisable")
        self.providers = providers
        self.pairs = [normalize_pair(p) for p in pairs]
        self.fee_bps = fee_bps
        self.min_net_bps = min_net_bps
        self.max_age = max_age
        self.max_history = max_history
        self._lock = threading.Lock()
        self.state: dict = {"updated": None, "quotes": {}, "opportunities": [], "errors": {}, "cycles": 0}
        self.history: list[dict] = []

    def run_cycle(self) -> list[Opportunity]:
        started = time.time()
        errors: dict[str, str] = {}

        def fetch(p: Provider):
            try:
                return p.name, p.get_quotes(self.pairs)
            except Exception as e:  # un fournisseur ne doit jamais tuer le cycle
                log.exception("%s a échoué", p.name)
                return p.name, e

        with ThreadPoolExecutor(max_workers=len(self.providers)) as ex:
            results = list(ex.map(fetch, self.providers))

        by_pair: dict[str, list[Quote]] = {p: [] for p in self.pairs}
        for name, res in results:
            if isinstance(res, Exception):
                errors[name] = str(res)
                continue
            if not res:
                errors[name] = "aucune cotation"
            for pair, q in res.items():
                if started - q.timestamp <= self.max_age:
                    by_pair[pair].append(q)

        opps: list[Opportunity] = []
        for pair, quotes in by_pair.items():
            o = find_opportunity(pair, quotes, self.fee_bps, self.min_net_bps)
            if o:
                opps.append(o)

        with self._lock:
            self.state = {
                "updated": time.time(),
                "quotes": {p: [q.to_dict() for q in qs] for p, qs in by_pair.items()},
                "opportunities": [o.to_dict() for o in opps],
                "errors": errors,
                "cycles": self.state["cycles"] + 1,
                "params": {"fee_bps": self.fee_bps, "min_net_bps": self.min_net_bps,
                           "providers": [p.name for p in self.providers]},
            }
            self.history = (self.history + [o.to_dict() for o in opps])[-self.max_history:]
        return opps

    def snapshot(self) -> dict:
        with self._lock:
            return {**self.state, "history": list(self.history)}
