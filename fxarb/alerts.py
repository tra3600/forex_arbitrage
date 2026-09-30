"""Alertes : envoi des opportunités par message direct Instagram (API officielle Instagram Messaging).

Prérequis (côté Meta) : compte Instagram professionnel lié à une app Meta avec la permission
`instagram_business_manage_messages`, un jeton d'accès, et l'IGSID du destinataire. Meta n'autorise
l'envoi qu'aux comptes ayant écrit au compte pro dans les dernières 24 h (envoyez-lui un DM d'abord).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from .models import Opportunity, TriangularOpportunity

log = logging.getLogger("fxarb.alerts")

MAX_TEXT_BYTES = 1000  # limite de l'API pour un message texte


class AlertError(Exception):
    pass


class InstagramNotifier:
    name = "instagram"

    def __init__(self, token: str | None = None, recipient_id: str | None = None,
                 api_version: str | None = None, timeout: float = 10.0,
                 base_url: str = "https://graph.instagram.com", session: requests.Session | None = None):
        self.token = token or os.environ.get("INSTAGRAM_ACCESS_TOKEN")
        self.recipient_id = recipient_id or os.environ.get("INSTAGRAM_RECIPIENT_ID")
        if not self.token or not self.recipient_id:
            raise AlertError("Instagram: définir INSTAGRAM_ACCESS_TOKEN et INSTAGRAM_RECIPIENT_ID")
        self.api_version = api_version or os.environ.get("INSTAGRAM_API_VERSION", "v21.0")
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()

    @staticmethod
    def _truncate(text: str) -> str:
        raw = text.encode("utf-8")
        if len(raw) <= MAX_TEXT_BYTES:
            return text
        return raw[:MAX_TEXT_BYTES - 3].decode("utf-8", "ignore") + "..."

    def send(self, text: str) -> None:
        url = f"{self.base_url}/{self.api_version}/me/messages"
        try:
            r = self.session.post(
                url, timeout=self.timeout,
                headers={"Authorization": f"Bearer {self.token}"},  # jamais loggé
                json={"recipient": {"id": self.recipient_id},
                      "message": {"text": self._truncate(text)}})
        except requests.RequestException as e:
            raise AlertError(f"Instagram: erreur réseau ({e.__class__.__name__})") from e
        if r.status_code != 200:
            try:
                msg = r.json().get("error", {}).get("message", "")
            except ValueError:
                msg = ""
            raise AlertError(f"Instagram: HTTP {r.status_code} {msg}".strip())


def format_opportunity(o) -> str:
    kind = "Arbitrage triangulaire" if isinstance(o, TriangularOpportunity) else "Arbitrage Forex"
    return f"{kind} - {o.describe()}"


def _key(o) -> str:
    if isinstance(o, TriangularOpportunity):
        return "tri|" + ">".join(o.path) + "|" + ",".join(l["provider"] for l in o.legs)
    return f"{o.pair}|{o.buy_provider}|{o.sell_provider}"


class AlertManager:
    """Filtre (écart net minimal), anti-spam (cooldown par opportunité) et envoi asynchrone."""

    def __init__(self, notifiers: list, cooldown: float = 300.0, min_net_bps: float = 0.0):
        self.notifiers = notifiers
        self.cooldown = cooldown
        self.min_net_bps = min_net_bps
        self._last: dict[str, tuple[float, float]] = {}  # clé -> (heure, net_bps)
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="fxarb-alert")

    def should_send(self, o, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        if o.net_bps < self.min_net_bps:
            return False
        prev = self._last.get(_key(o))
        # on renvoie si le cooldown est écoulé ou si l'écart a nettement grossi (x1,5)
        if prev and now - prev[0] < self.cooldown and o.net_bps < prev[1] * 1.5:
            return False
        self._last[_key(o)] = (now, o.net_bps)
        return True

    def notify(self, opportunities: list) -> int:
        sent = 0
        for o in opportunities:
            if self.notifiers and self.should_send(o):
                text = format_opportunity(o)
                for n in self.notifiers:
                    self._pool.submit(self._send, n, text)
                sent += 1
        return sent

    @staticmethod
    def _send(notifier, text):
        try:
            notifier.send(text)
            log.info("Alerte %s envoyée", notifier.name)
        except AlertError as e:
            log.warning("Alerte %s échouée : %s", notifier.name, e)
        except Exception:
            log.exception("Alerte %s : erreur inattendue", notifier.name)

    def close(self):
        self._pool.shutdown(wait=True)
