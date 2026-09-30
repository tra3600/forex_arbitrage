from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from itertools import combinations

from .models import (Opportunity, Quote, TriangularOpportunity, canonical_pair, normalize_pair,
                     split_pair)
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


def find_triangular(quotes_by_pair: dict[str, list[Quote]], leg_fee_bps: float = 0.0,
                    min_net_bps: float = 0.0) -> list[TriangularOpportunity]:
    """Arbitrage triangulaire A -> B -> C -> A (départ avec 1 unité de A).

    Pour chaque jambe on prend le meilleur prix exécutable parmi tous les fournisseurs :
    vendre la devise de base au bid le plus haut, ou l'acheter à l'ask le plus bas.
    Coût total = 3 * leg_fee_bps. Renvoie les cycles rentables, du meilleur au moins bon
    (un seul sens par triplet de devises).
    """
    edges: dict[tuple[str, str], dict] = {}
    for pair, quotes in quotes_by_pair.items():
        if not quotes:
            continue
        base, quote = split_pair(pair)
        best_bid = max(quotes, key=lambda q: q.bid)
        best_ask = min(quotes, key=lambda q: q.ask)
        # vendre `base` : base -> quote, on reçoit bid quote par base
        edges[(base, quote)] = {"pair": pair, "action": "SELL", "provider": best_bid.provider,
                                "price": best_bid.bid, "rate": best_bid.bid}
        # acheter `base` : quote -> base, on obtient 1/ask base par quote
        edges[(quote, base)] = {"pair": pair, "action": "BUY", "provider": best_ask.provider,
                                "price": best_ask.ask, "rate": 1.0 / best_ask.ask}
    currencies = sorted({c for k in edges for c in k})
    out: list[TriangularOpportunity] = []
    for trio in combinations(currencies, 3):
        best: TriangularOpportunity | None = None
        a, b, c = trio
        for path in ((a, b, c), (a, c, b)):
            cycle = [path[0], path[1], path[2], path[0]]
            legs = [edges.get((cycle[i], cycle[i + 1])) for i in range(3)]
            if not all(legs):
                continue
            prod = legs[0]["rate"] * legs[1]["rate"] * legs[2]["rate"]
            gross = (prod - 1) * 1e4
            net = gross - 3 * leg_fee_bps
            if net > min_net_bps and (best is None or net > best.net_bps):
                best = TriangularOpportunity(cycle, legs, gross, net)
        if best:
            out.append(best)
    return sorted(out, key=lambda o: -o.net_bps)


def expand_triangular_pairs(pairs: list[str]) -> list[str]:
    """Ajoute toutes les paires (orientation canonique) entre les devises citées."""
    currencies = sorted({c for p in pairs for c in split_pair(p)})
    extra = [canonical_pair(x, y) for x, y in combinations(currencies, 2)]
    return list(dict.fromkeys([normalize_pair(p) for p in pairs] + extra))


class Engine:
    """Interroge tous les fournisseurs en parallèle et conserve le dernier état (pour le web)."""

    def __init__(self, providers: list[Provider], pairs: list[str], fee_bps: float = 0.0,
                 min_net_bps: float = 0.0, max_age: float = 120.0, max_history: int = 200,
                 triangular: bool = False, tri_leg_fee_bps: float = 0.5):
        if not providers:
            raise ValueError("Aucun fournisseur utilisable")
        self.providers = providers
        self.triangular = triangular
        self.tri_leg_fee_bps = tri_leg_fee_bps
        self.pairs = expand_triangular_pairs(pairs) if triangular else [normalize_pair(p) for p in pairs]
        self.fee_bps = fee_bps
        self.min_net_bps = min_net_bps
        self.max_age = max_age
        self.max_history = max_history
        self._lock = threading.Lock()
        self.state: dict = {"updated": None, "quotes": {}, "opportunities": [], "errors": {}, "cycles": 0}
        self.history: list[dict] = []
        self.tri_history: list[dict] = []
        self.last_triangular: list[TriangularOpportunity] = []

    def run_cycle(self) -> list[Opportunity]:
        self.last_triangular: list[TriangularOpportunity] = []
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

        tri = (find_triangular(by_pair, self.tri_leg_fee_bps, self.min_net_bps)
               if self.triangular else [])
        self.last_triangular = tri

        with self._lock:
            self.state = {
                "updated": time.time(),
                "quotes": {p: [q.to_dict() for q in qs] for p, qs in by_pair.items()},
                "opportunities": [o.to_dict() for o in opps],
                "triangular": [t.to_dict() for t in tri],
                "errors": errors,
                "cycles": self.state["cycles"] + 1,
                "params": {"fee_bps": self.fee_bps, "min_net_bps": self.min_net_bps,
                           "providers": [p.name for p in self.providers],
                           "triangular": self.triangular, "tri_leg_fee_bps": self.tri_leg_fee_bps},
            }
            self.history = (self.history + [o.to_dict() for o in opps])[-self.max_history:]
            self.tri_history = (self.tri_history + [t.to_dict() for t in tri])[-self.max_history:]
        return opps

    def snapshot(self) -> dict:
        with self._lock:
            return {**self.state, "history": list(self.history), "tri_history": list(self.tri_history)}
