import time

from app.config import Config
from app.store import AlertStore


def test_claim_is_idempotent_per_pair_and_bar(tmp_path):
    with AlertStore(str(tmp_path)) as store:
        assert store.claim("BTC_USDT", 1789041600) is True
        assert store.claim("BTC_USDT", 1789041600) is False
        # A different bar for the same pair is a separate claim.
        assert store.claim("BTC_USDT", 1789056000) is True
        # A different pair on the same bar is a separate claim.
        assert store.claim("ETH_USDT", 1789041600) is True


def test_release_allows_a_retry(tmp_path):
    with AlertStore(str(tmp_path)) as store:
        assert store.claim("SOL_USDT", 1789041600) is True
        store.release("SOL_USDT", 1789041600)
        assert store.was_alerted("SOL_USDT", 1789041600) is False
        assert store.claim("SOL_USDT", 1789041600) is True


def test_state_survives_reopening_the_database(tmp_path):
    with AlertStore(str(tmp_path)) as store:
        store.claim("GT_USDT", 1789041600)
    with AlertStore(str(tmp_path)) as reopened:
        assert reopened.claim("GT_USDT", 1789041600) is False


def test_prune_drops_old_rows_only(tmp_path):
    with AlertStore(str(tmp_path)) as store:
        store.claim("OLD_USDT", 1)
        store.claim("NEW_USDT", 2)
        store._conn.execute("UPDATE alerts SET created_at = 0 WHERE pair = 'OLD_USDT'")
        store._conn.commit()
        assert store.prune(retention_days=3) == 1
        assert store.count() == 1
        assert store.was_alerted("NEW_USDT", 2) is True


class _Hit:
    """Minimal stand-in for app.scanner.Hit."""

    def __init__(self, pair, bar_ts):
        self.pair, self.bar_ts = pair, bar_ts
        self.base = pair.split("_")[0]
        self.interval_seconds, self.timeframe, self.ema_len = 14400, "4H", 20
        self.close = self.ema = self.macd = self.signal = 1.0
        self.market_cap = self.quote_volume_24h = 1.0


class _Result:
    def __init__(self, bar_ts):
        self.bar_ts, self.duration = bar_ts, 1.0
        self.universe_total = self.after_quote_filter = 1
        self.after_status_filter = self.after_leveraged_filter = 1
        self.after_volume_filter = self.after_mcap_filter = self.scanned = 1
        self.hits, self.alerted = [], []
        self.skipped_duplicate = self.errors = 0


def test_prune_clears_hits_and_scans_too(tmp_path):
    """Retention governs every table, not just the dedupe ledger."""
    day = 86400
    with AlertStore(str(tmp_path)) as store:
        for pair, bar in (("OLD_USDT", 1), ("NEW_USDT", 2)):
            store.claim(pair, bar)
            store.record_hit(_Hit(pair, bar), alerted=True)
            store.record_scan(_Result(bar))

        old = int(time.time()) - 5 * day  # older than a 3-day window
        store._conn.execute("UPDATE hits SET created_at = ? WHERE pair = 'OLD_USDT'", (old,))
        store._conn.execute("UPDATE scans SET finished_at = ? WHERE bar_ts = 1", (old,))
        store._conn.execute("UPDATE alerts SET created_at = ? WHERE pair = 'OLD_USDT'", (old,))
        store._conn.commit()

        assert store.totals() == {"hits": 2, "alerted": 2, "scans": 2}
        store.prune(retention_days=3)

        assert store.totals() == {"hits": 1, "alerted": 1, "scans": 1}
        assert [h["pair"] for h in store.recent_hits()] == ["NEW_USDT"]
        assert [r["bar_ts"] for r in store.recent_scans()] == [2]
        assert store.was_alerted("OLD_USDT", 1) is False


def test_prune_zero_keeps_everything(tmp_path):
    with AlertStore(str(tmp_path)) as store:
        store.claim("BTC_USDT", 1)
        store.record_hit(_Hit("BTC_USDT", 1), alerted=True)
        store._conn.execute("UPDATE hits SET created_at = 0")
        store._conn.execute("UPDATE alerts SET created_at = 0")
        store._conn.commit()
        assert store.prune(retention_days=0) == 0
        assert store.totals()["hits"] == 1


def test_unwritable_data_dir_falls_back(tmp_path):
    store = AlertStore("/proc/definitely-not-writable")
    try:
        assert store.data_dir.startswith("/tmp")
        # The shared fallback DB may already hold rows, so use a unique key.
        unique_bar = int(time.time() * 1000)
        assert store.claim("FALLBACK_USDT", unique_bar) is True
    finally:
        store.close()


def test_retention_defaults_to_three_days(monkeypatch):
    monkeypatch.delenv("RETENTION_DAYS", raising=False)
    monkeypatch.delenv("DEDUPE_RETENTION_DAYS", raising=False)
    assert Config().retention_days == 3


def test_retention_honours_both_env_names(monkeypatch):
    monkeypatch.delenv("RETENTION_DAYS", raising=False)
    monkeypatch.setenv("DEDUPE_RETENTION_DAYS", "10")   # the old name still works
    assert Config().retention_days == 10
    monkeypatch.setenv("RETENTION_DAYS", "7")           # the new name wins
    assert Config().retention_days == 7
