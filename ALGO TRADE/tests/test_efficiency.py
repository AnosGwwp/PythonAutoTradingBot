"""Connection reuse, background Telegram, lock-free downloads, skipped writes (offline)."""
import gzip
import io
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from signalbot import net, news, remote, scanner
from signalbot.data import Candle
from signalbot.notify import Notifier
from signalbot.scanner import State, scan_once
from signalbot.strategy import Signal


class FakeResponse:
    def __init__(self, status=200, body=b"{}", headers=None, will_close=False):
        self.status, self._body, self._h, self.will_close = status, body, headers or {}, will_close

    def read(self):
        return self._body

    def getheader(self, k, default=None):
        return self._h.get(k, default)


class FakeConn:
    made = []

    def __init__(self, host, timeout=None):
        self.host, self.timeout, self.sock, self.fail_next, self.closed = host, timeout, None, None, False
        self.requests = 0
        FakeConn.made.append(self)

    def request(self, method, path, body=None, headers=None):
        self.requests += 1
        if self.fail_next:
            e, self.fail_next = self.fail_next, None
            raise e

    def getresponse(self):
        return FakeResponse(body=gzip.compress(b'{"ok": 1}'), headers={"Content-Encoding": "gzip"})

    def close(self):
        self.closed = True


class NetTests(unittest.TestCase):
    def setUp(self):
        FakeConn.made = []
        net._idle.clear()
        p = mock.patch.object(net.http.client, "HTTPSConnection", FakeConn)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(net._idle.clear)

    def test_connection_is_reused_and_gzip_decoded(self):
        self.assertEqual(net.request("GET", "https://x.test/a"), b'{"ok": 1}')
        net.request("GET", "https://x.test/b")
        self.assertEqual(len(FakeConn.made), 1)                 # one handshake for both calls
        self.assertEqual(FakeConn.made[0].requests, 2)

    def test_stale_reused_connection_retried_once_on_fresh_one(self):
        net.request("GET", "https://x.test/a")
        FakeConn.made[0].fail_next = ConnectionResetError("server closed idle socket")
        self.assertEqual(net.request("POST", "https://x.test/order", b"{}"), b'{"ok": 1}')
        self.assertEqual(len(FakeConn.made), 2)

    def test_fresh_connection_failure_and_timeouts_are_not_retried(self):
        with mock.patch.object(FakeConn, "request", side_effect=ConnectionRefusedError("down")):
            with self.assertRaises(OSError):
                net.request("GET", "https://x.test/a")
        self.assertEqual(len(FakeConn.made), 1)
        net._idle.clear()
        net.request("GET", "https://x.test/a")
        FakeConn.made[-1].fail_next = TimeoutError("slow")
        with self.assertRaises(TimeoutError):
            net.request("POST", "https://x.test/order", b"{}")   # may still be executing: never resend
        self.assertEqual(len(FakeConn.made), 2)

    def test_old_idle_connection_is_not_reused(self):
        net.request("GET", "https://x.test/a")
        conn, _ = net._idle["x.test"][0]
        net._idle["x.test"][0] = (conn, time.monotonic() - net.MAX_IDLE_SEC - 1)
        net.request("GET", "https://x.test/a")
        self.assertTrue(conn.closed)
        self.assertEqual(len(FakeConn.made), 2)


class TelegramQueueTests(unittest.TestCase):
    def test_messages_sent_in_order_in_background_and_flushed(self):
        with tempfile.TemporaryDirectory() as t, \
                mock.patch("signalbot.notify.env", side_effect=lambda k: "x"):
            n = Notifier({"alerts": {"log_dir": t, "telegram": True}})
            sent, gate = [], threading.Event()

            def slow_send(text, silent=False, keyboard=None):
                gate.wait(5)
                sent.append(text)
            n._send_now = slow_send
            t0 = time.time()
            for i in range(3):
                n.reply(f"m{i}")
            self.assertLess(time.time() - t0, 0.5)               # callers don't wait for Telegram
            gate.set()
            self.assertTrue(n.flush(5))
            self.assertEqual(sent, ["m0", "m1", "m2"])


class StateTests(unittest.TestCase):
    def test_unchanged_state_is_not_rewritten(self):
        with tempfile.TemporaryDirectory() as t:
            st = State(Path(t) / "s.json")
            st.save()
            with mock.patch.object(Path, "write_text") as w, mock.patch.object(Path, "replace"):
                st.save()
                w.assert_not_called()
                st.meta["x"] = 1
                st.save()
                w.assert_called_once()


class NewsConditionalTests(unittest.TestCase):
    def test_unchanged_feed_returns_none(self):
        validators = {}
        ok = mock.MagicMock()
        ok.__enter__.return_value = ok
        ok.headers = {"ETag": '"v1"'}
        ok.read.return_value = b"<rss/>"
        with mock.patch("urllib.request.urlopen", return_value=ok):
            self.assertEqual(news._get("https://feed.test/rss", validators=validators), b"<rss/>")
        sent = {}

        def not_modified(req, timeout=None):
            sent.update(req.headers)
            raise urllib.error.HTTPError(req.full_url, 304, "Not Modified", {}, io.BytesIO(b""))
        with mock.patch("urllib.request.urlopen", side_effect=not_modified):
            self.assertIsNone(news._get("https://feed.test/rss", validators=validators))
        self.assertEqual(sent.get("If-none-match"), '"v1"')


def _cfg(tmp):
    return {"pairs": ["BTCUSDT"], "watch_pairs": [], "entry_tf": "1h", "trend_tf": "4h", "data_source": "bybit",
            "rules": {"max_bars_open": 48, "cooldown_bars": 6, "min_score": 60},
            "risk": {"account_size": 3000, "risk_pct": 1, "leverage": 10, "fee_pct": 0.05},
            "alerts": {"log_dir": tmp}}


class LockFreeScanTests(unittest.TestCase):
    def test_trade_closed_from_phone_during_download_is_left_alone(self):
        with tempfile.TemporaryDirectory() as t:
            st = State(Path(t) / "s.json")
            sig = Signal("BTCUSDT", "LONG", "1h", 0, 100, 98, [102, 104, 106], 70, ["pullback"], [], 1, sl_now=98)
            st.open["BTCUSDT"] = sig
            st.last_eval["BTCUSDT"] = int(time.time() * 1000)
            lock = threading.RLock()

            def download(pair, tf, n, src):               # the user sends /close while we download
                self.assertTrue(lock.acquire(blocking=False))   # downloads must not hold the lock
                lock.release()
                if "BTCUSDT" in st.open:
                    st.closed.append(st.open.pop("BTCUSDT"))
                return [Candle(1, 60_001, 100, 90, 90, 90, 1)]   # would hit the stop
            with mock.patch.object(scanner, "candles", side_effect=download):
                scan_once(_cfg(t), st, mock.Mock(), lock=lock)
            self.assertEqual(sig.status, "open")                 # not processed twice
            self.assertEqual(st.open, {})


class LongDowntimeTests(unittest.TestCase):
    def test_catch_up_uses_hourly_candles_for_the_part_1m_history_cannot_reach(self):
        with tempfile.TemporaryDirectory() as t:
            st = State(Path(t) / "s.json")
            H = 3_600_000
            now = int(time.time() * 1000) // 60_000 * 60_000
            opened = now - 90 * H                                # bot was off for ~4 days
            sig = Signal("BTCUSDT", "LONG", "1h", opened, 100, 98, [102, 104, 106], 70, ["pullback"], [], 1,
                         sl_now=98, last_t=opened - 1)
            st.open["BTCUSDT"] = sig
            st.last_eval["BTCUSDT"] = now
            hourly = [Candle(opened + i * H, opened + (i + 1) * H, 100, 100.5, 99.5, 100, 1) for i in range(90)]
            hourly[10] = Candle(opened + 10 * H, opened + 11 * H, 100, 100.5, 97.0, 98, 1)   # stop hit on day 1

            def get(pair, tf, n, src):
                if tf == "1h":
                    return hourly[-n:]
                return [Candle(now - (n - i) * 60_000, now - (n - i - 1) * 60_000, 100, 100, 100, 100, 1)
                        for i in range(n)]
            with mock.patch.object(scanner, "candles", side_effect=get):
                scan_once(_cfg(t), st, mock.Mock())
            self.assertEqual(sig.exit_reason, "stop loss")       # not "expired" days later
            self.assertEqual(sig.closed_at, opened + 11 * H)


class ReviewInBackgroundTests(unittest.TestCase):
    def test_review_answers_immediately_then_sends_report(self):
        with tempfile.TemporaryDirectory() as t:
            replies, done = [], threading.Event()
            n = mock.Mock(tg=False, tg_chat="1")
            n.reply.side_effect = lambda text, keyboard=None: (replies.append(text), done.set())
            r = remote.Remote(_cfg(t), State(Path(t) / "s.json"), n, threading.RLock(), threading.Event())
            with mock.patch.object(remote.learn, "review", side_effect=lambda cfg, get=None: (time.sleep(0.2), "REPORT")[1]):
                first = r.handle("/review")
                self.assertIn("Reviewing", first)
                self.assertIn("already running", r.handle("/review"))
                self.assertTrue(done.wait(5))
            self.assertEqual(replies, ["REPORT"])


if __name__ == "__main__":
    unittest.main()
