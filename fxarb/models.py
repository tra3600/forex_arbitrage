from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict


def normalize_pair(pair: str) -> str:
    """'eur/usd', 'EUR-USD', 'EURUSD=X' -> 'EURUSD'."""
    p = pair.upper().replace("/", "").replace("-", "").replace("_", "").replace("=X", "").strip()
    if len(p) != 6 or not p.isalpha():
        raise ValueError(f"Paire de devises invalide : {pair!r} (attendu ex. EURUSD)")
    return p


def split_pair(pair: str) -> tuple[str, str]:
    p = normalize_pair(pair)
    return p[:3], p[3:]


@dataclass
class Quote:
    """Cotation d'une paire chez un fournisseur.

    Beaucoup d'API gratuites ne donnent qu'un cours milieu (mid) : dans ce cas
    bid == ask == mid (voir `has_spread`).
    """

    provider: str
    pair: str
    bid: float
    ask: float
    timestamp: float = field(default_factory=time.time)
    has_spread: bool = False

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    def to_dict(self) -> dict:
        d = asdict(self)
        d["mid"] = self.mid
        return d


@dataclass
class Opportunity:
    pair: str
    buy_provider: str
    buy_price: float   # ask le plus bas
    sell_provider: str
    sell_price: float  # bid le plus haut
    gross_bps: float   # écart brut en points de base
    net_bps: float     # après frais
    timestamp: float = field(default_factory=time.time)

    def describe(self) -> str:
        return (
            f"{self.pair}: acheter sur {self.buy_provider} @ {self.buy_price:.5f}, "
            f"vendre sur {self.sell_provider} @ {self.sell_price:.5f} "
            f"(brut {self.gross_bps:.2f} bps, net {self.net_bps:.2f} bps)"
        )

    def to_dict(self) -> dict:
        return asdict(self)
