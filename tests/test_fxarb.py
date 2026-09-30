"""Tests hors-ligne : un faux serveur HTTP local imite les API réelles."""
import json
import os
import sys
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fxarb import providers as P
from fxarb.alerts import AlertError, AlertManager, InstagramNotifier, format_opportunity
from fxarb.models import Opportunity
from fxarb.engine import Engine, expand_triangular_pairs, find_opportunity, find_triangular
from fxarb.models import Quote, normalize_pair
from fxarb.web import make_server

ROUTES = {}


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ROUTES:
            code, body = ROUTES[path]
            data = json.dumps(body).encode()
        else:
            code, data = 404, b"{}"
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        H.last_post = {"path": self.path, "auth": self.headers.get("Authorization"),
                       "body": json.loads(self.rfile.read(n) or b"{}")}
        code, body = ROUTES.get(self.path, (404, {}))
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        ROUTES.clear()
        self.session = P.make_session(retries=0)


class TestModels(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_pair("eur/usd"), "EURUSD")
        self.assertEqual(normalize_pair("EURUSD=X"), "EURUSD")
        with self.assertRaises(ValueError):
            normalize_pair("EUR")


class TestEngineLogic(unittest.TestCase):
    def q(self, name, bid, ask):
        return Quote(name, "EURUSD", bid, ask)

    def test_opportunity(self):
        o = find_opportunity("EURUSD", [self.q("a", 1.0995, 1.1000), self.q("b", 1.1010, 1.1015)], fee_bps=1)
        self.assertEqual((o.buy_provider, o.sell_provider), ("a", "b"))
        self.assertAlmostEqual(o.gross_bps, (1.1010 - 1.1000) / 1.1000 * 1e4)
        self.assertAlmostEqual(o.net_bps, o.gross_bps - 1)

    def test_fees_kill_opportunity(self):
        self.assertIsNone(find_opportunity("EURUSD", [self.q("a", 1.1, 1.1), self.q("b", 1.1005, 1.1005)], fee_bps=10))

    def test_single_provider(self):
        self.assertIsNone(find_opportunity("EURUSD", [self.q("a", 1.1, 1.1)]))


class TestTriangular(unittest.TestCase):
    def qs(self, **rates):
        return {p: [Quote("x", p, r, r)] for p, r in rates.items()}

    def test_consistent_rates_no_opportunity(self):
        self.assertEqual(find_triangular(self.qs(EURUSD=1.10, GBPUSD=1.25, EURGBP=1.10 / 1.25)), [])

    def test_detects_cycle_and_direction(self):
        # EURGBP trop cher : EUR->USD->GBP->EUR (vendre EURUSD, acheter GBPUSD, vendre EURGBP)
        res = find_triangular(self.qs(EURUSD=1.10, GBPUSD=1.25, EURGBP=0.90))
        self.assertEqual(len(res), 1)
        o = res[0]
        prod = 1.0
        for l in o.legs:
            prod *= l["rate"]
        self.assertAlmostEqual(o.gross_bps, (prod - 1) * 1e4)
        self.assertGreater(o.net_bps, 0)
        self.assertEqual(o.path[0], o.path[-1])
        self.assertEqual(sorted(o.path[:3]), ["EUR", "GBP", "USD"])

    def test_fees_and_bid_ask(self):
        q = self.qs(EURUSD=1.10, GBPUSD=1.25, EURGBP=0.8850)
        gross = find_triangular(q, 0)[0].gross_bps
        self.assertIsNone(next(iter(find_triangular(q, gross)), None))  # 3*fee > gross
        # spread bid/ask détruit l'opportunité
        wide = {p: [Quote("x", p, r * 0.997, r * 1.003)] for p, r in dict(EURUSD=1.10, GBPUSD=1.25, EURGBP=0.8850).items()}
        self.assertEqual(find_triangular(wide), [])

    def test_best_leg_across_providers(self):
        q = self.qs(EURUSD=1.10, GBPUSD=1.25, EURGBP=1.10 / 1.25)
        q["EURGBP"].append(Quote("y", "EURGBP", 0.885, 0.886))  # meilleur bid chez y
        res = find_triangular(q)
        self.assertEqual(len(res), 1)
        self.assertIn("y", [l["provider"] for l in res[0].legs])

    def test_missing_leg(self):
        self.assertEqual(find_triangular(self.qs(EURUSD=1.1, GBPUSD=1.25)), [])

    def test_expand_pairs(self):
        self.assertEqual(set(expand_triangular_pairs(["EURUSD", "USDJPY"])),
                         {"EURUSD", "USDJPY", "EURJPY"})


POSTS = []


class TestAlerts(Base):
    def setUp(self):
        super().setUp()
        POSTS.clear()

    def opp(self, net=5.0, buy="a"):
        return Opportunity("EURUSD", buy, 1.1, "b", 1.101, net + 1, net)

    def test_missing_config(self):
        os.environ.pop("INSTAGRAM_ACCESS_TOKEN", None)
        with self.assertRaises(AlertError):
            InstagramNotifier()

    def test_send_payload(self):
        n = InstagramNotifier("TOK", "123", base_url=self.url)
        ROUTES["/v21.0/me/messages"] = (200, {"message_id": "m"})
        n.send("salut")
        self.assertEqual(H.last_post["path"], "/v21.0/me/messages")
        self.assertEqual(H.last_post["auth"], "Bearer TOK")
        self.assertEqual(H.last_post["body"], {"recipient": {"id": "123"}, "message": {"text": "salut"}})

    def test_http_error_no_token_leak(self):
        n = InstagramNotifier("SECRET", "123", base_url=self.url)
        ROUTES["/v21.0/me/messages"] = (400, {"error": {"message": "bad recipient"}})
        with self.assertRaises(AlertError) as cm:
            n.send("x")
        self.assertIn("bad recipient", str(cm.exception))
        self.assertNotIn("SECRET", str(cm.exception))

    def test_truncate(self):
        self.assertLessEqual(len(InstagramNotifier._truncate("é" * 2000).encode()), 1000)

    def test_cooldown_and_threshold(self):
        m = AlertManager([], cooldown=100, min_net_bps=2)
        self.assertFalse(m.should_send(self.opp(1.0), now=0))
        self.assertTrue(m.should_send(self.opp(5.0), now=0))
        self.assertFalse(m.should_send(self.opp(5.5), now=10))
        self.assertTrue(m.should_send(self.opp(9.0), now=20))    # écart x1,5
        self.assertTrue(m.should_send(self.opp(9.0), now=500))   # cooldown écoulé
        self.assertTrue(m.should_send(self.opp(5.0, buy="c"), now=10))  # autre opportunité

    def test_manager_sends(self):
        ROUTES["/v21.0/me/messages"] = (200, {})
        m = AlertManager([InstagramNotifier("T", "1", base_url=self.url)])
        self.assertEqual(m.notify([self.opp()]), 1)
        self.assertEqual(m.notify([self.opp()]), 0)
        m.close()
        self.assertIn("EURUSD", H.last_post["body"]["message"]["text"])


class TestProviders(Base):
    def test_erapi(self):
        ROUTES["/v6/latest/EUR"] = (200, {"result": "success", "rates": {"USD": 1.17}})
        p = P.ErApi(session=self.session)
        p.url = self.url + "/v6/latest/{base}"
        self.assertEqual(p.get_quote("EURUSD").mid, 1.17)

    def test_erapi_error(self):
        ROUTES["/v6/latest/EUR"] = (200, {"result": "error", "error-type": "unsupported-code"})
        p = P.ErApi(session=self.session)
        p.url = self.url + "/v6/latest/{base}"
        with self.assertRaises(P.ProviderError):
            p.get_quote("EURUSD")

    def test_frankfurter_and_http_error(self):
        ROUTES["/latest"] = (200, {"rates": {"USD": 1.1}})
        p = P.Frankfurter(session=self.session)
        p.url = self.url + "/latest"
        self.assertEqual(p.get_quote("EURUSD").mid, 1.1)
        ROUTES["/latest"] = (500, {})
        p._table_cache.clear()
        with self.assertRaises(P.ProviderError):
            p.get_quote("EURUSD")

    def test_fawaz_fallback(self):
        ROUTES["/b/eur.json"] = (200, {"eur": {"usd": 1.2}})
        p = P.FawazCurrency(session=self.session)
        p.urls = (self.url + "/dead/{base}.json", self.url + "/b/{base}.json")
        self.assertEqual(p.get_quote("EURUSD").mid, 1.2)

    def test_yahoo(self):
        ROUTES["/chart/EURUSD=X"] = (200, {"chart": {"result": [{"meta": {"regularMarketPrice": 1.13}}]}})
        p = P.Yahoo(session=self.session)
        p.url = self.url + "/chart/{pair}=X"
        self.assertEqual(p.get_quote("EURUSD").mid, 1.13)

    def test_alphavantage_bid_ask(self):
        ROUTES["/query"] = (200, {"Realtime Currency Exchange Rate": {
            "5. Exchange Rate": "1.1", "8. Bid Price": "1.0999", "9. Ask Price": "1.1001"}})
        p = P.AlphaVantage(session=self.session, api_key="k")
        p.url = self.url + "/query"
        q = p.get_quote("EURUSD")
        self.assertTrue(q.has_spread)
        self.assertEqual((q.bid, q.ask), (1.0999, 1.1001))

    def test_missing_key(self):
        os.environ.pop("TWELVEDATA_API_KEY", None)
        with self.assertRaises(P.ProviderError):
            P.TwelveData()
        self.assertEqual(P.build_providers(["twelvedata"]), [])

    def test_generic_legacy_format(self):
        ROUTES["/forex/EURUSD"] = (200, {"rate": 1.1})
        p = P.Generic(session=self.session, url=self.url + "/forex", api_key="x")
        self.assertEqual(p.get_quote("EURUSD").mid, 1.1)

    def test_unknown_provider(self):
        with self.assertRaises(ValueError):
            P.build_providers(["nope"])


class TestEndToEnd(Base):
    def test_cycle_and_web(self):
        ROUTES["/a/EURUSD"] = (200, {"rate": 1.1000})
        ROUTES["/b/EURUSD"] = (200, {"rate": 1.1020})
        a = P.Generic(session=self.session, url=self.url + "/a")
        a.name = "A"
        b = P.Generic(session=self.session, url=self.url + "/b")
        b.name = "B"
        eng = Engine([a, b], ["EURUSD", "GBPUSD"], fee_bps=1)  # GBPUSD -> 404, ignoré
        opps = eng.run_cycle()
        self.assertEqual(len(opps), 1)
        self.assertEqual((opps[0].buy_provider, opps[0].sell_provider), ("A", "B"))

        eng_t = Engine([a, b], ["EURUSD"], triangular=True)
        self.assertEqual(eng_t.pairs, ["EURUSD"])  # 2 devises : pas de croisée
        srv = make_server(eng, port=0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            state = json.load(urllib.request.urlopen(base + "/api/state"))
            self.assertEqual(state["opportunities"][0]["pair"], "EURUSD")
            self.assertIn(b"Forex Arbitrage", urllib.request.urlopen(base + "/").read())
            self.assertEqual(json.load(urllib.request.urlopen(base + "/health"))["status"], "ok")
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
