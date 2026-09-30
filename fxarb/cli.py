from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time

from .alerts import AlertError, AlertManager, InstagramNotifier
from .engine import Engine
from .providers import DEFAULT_PROVIDERS, PROVIDERS, build_providers
from .web import serve_in_thread

log = logging.getLogger("fxarb")


def env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Détecteur d'arbitrage Forex multi-sources (lecture seule, aucun ordre passé).")
    p.add_argument("--pairs", default=os.environ.get("FX_PAIRS", "EURUSD,GBPUSD,USDJPY"),
                   help="paires séparées par des virgules (défaut: %(default)s)")
    p.add_argument("--providers", default=os.environ.get("FX_PROVIDERS", ",".join(DEFAULT_PROVIDERS)),
                   help=f"parmi: {', '.join(PROVIDERS)} (défaut: %(default)s)")
    p.add_argument("--interval", type=float, default=env_float("FX_INTERVAL", 10), help="secondes entre deux cycles")
    p.add_argument("--fee-bps", type=float, default=env_float("FX_FEE_BPS", 1.0),
                   help="coûts aller-retour en points de base retranchés de l'écart (défaut: %(default)s)")
    p.add_argument("--min-net-bps", type=float, default=env_float("FX_MIN_NET_BPS", 0.0),
                   help="écart net minimal pour signaler une opportunité")
    p.add_argument("--max-age", type=float, default=env_float("FX_MAX_AGE", 300),
                   help="ignore les cotations plus vieilles que N secondes")
    p.add_argument("--triangular", action="store_true",
                   help="active l'arbitrage triangulaire (ajoute automatiquement les paires croisées)")
    p.add_argument("--tri-leg-fee-bps", type=float, default=env_float("FX_TRI_LEG_FEE_BPS", 0.5),
                   help="coût par jambe (3 jambes par cycle) en bps (défaut: %(default)s)")
    p.add_argument("--alert-instagram", action="store_true",
                   help="envoie les opportunités par DM Instagram (INSTAGRAM_ACCESS_TOKEN, INSTAGRAM_RECIPIENT_ID)")
    p.add_argument("--alert-cooldown", type=float, default=env_float("FX_ALERT_COOLDOWN", 300),
                   help="secondes avant de renvoyer la même alerte (défaut: %(default)s)")
    p.add_argument("--alert-min-bps", type=float, default=env_float("FX_ALERT_MIN_BPS", 0.0),
                   help="écart net minimal pour alerter (défaut: %(default)s)")
    p.add_argument("--timeout", type=float, default=8.0, help="timeout HTTP (s)")
    p.add_argument("--once", action="store_true", help="un seul cycle puis quitte")
    p.add_argument("--json", action="store_true", help="affiche l'état complet en JSON (avec --once)")
    p.add_argument("--log-file", help="ajoute chaque opportunité à ce fichier JSONL")
    p.add_argument("--web", action="store_true", help="lance le tableau de bord web")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv=None) -> int:
    a = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        providers = build_providers([n for n in a.providers.split(",") if n.strip()], timeout=a.timeout)
        engine = Engine(providers, [x for x in a.pairs.split(",") if x.strip()],
                        fee_bps=a.fee_bps, min_net_bps=a.min_net_bps, max_age=a.max_age,
                        triangular=a.triangular, tri_leg_fee_bps=a.tri_leg_fee_bps)
    except ValueError as e:
        print(f"Erreur de configuration : {e}", file=sys.stderr)
        return 2
    alerts = None
    if a.alert_instagram:
        try:
            alerts = AlertManager([InstagramNotifier()], a.alert_cooldown, a.alert_min_bps)
        except AlertError as e:
            print(f"Erreur de configuration : {e}", file=sys.stderr)
            return 2
    if len(providers) < 2:
        log.warning("Moins de 2 fournisseurs utilisables : aucun arbitrage possible.")

    if a.web:
        srv = serve_in_thread(engine, a.host, a.port)
        log.info("Tableau de bord : http://%s:%d", a.host, srv.server_address[1])

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    log.info("Démarrage : %s sur %s", ",".join(engine.pairs), ",".join(p.name for p in providers))
    while not stop.is_set():
        t0 = time.time()
        opps = engine.run_cycle()
        snap = engine.snapshot()
        for pair, qs in snap["quotes"].items():
            log.info("%s %s", pair, " | ".join(f"{q['provider']}={q['mid']:.5f}" for q in qs) or "aucune cotation")
        for name, err in snap["errors"].items():
            log.warning("%s: %s", name, err)
        for o in opps:
            log.info("OPPORTUNITÉ %s", o.describe())
            if a.log_file:
                with open(a.log_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(o.to_dict()) + "\n")
        for t in engine.last_triangular:
            log.info("TRIANGULAIRE %s", t.describe())
            if a.log_file:
                with open(a.log_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"type": "triangular", **t.to_dict()}) + "\n")
        if alerts:
            alerts.notify(list(opps) + list(engine.last_triangular))
        if not opps:
            log.info("Pas d'opportunité au-dessus des frais (%.1f bps).", a.fee_bps)
        if a.once:
            if alerts:
                alerts.close()
            if a.json:
                print(json.dumps(snap, indent=2))
            return 0 if snap["quotes"] and any(snap["quotes"].values()) else 1
        stop.wait(max(0.0, a.interval - (time.time() - t0)))
    if alerts:
        alerts.close()
    log.info("Arrêt.")
    return 0
