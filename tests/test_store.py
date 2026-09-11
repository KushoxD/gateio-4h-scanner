import time

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
        assert store.prune(retention_days=30) == 1
        assert store.count() == 1
        assert store.was_alerted("NEW_USDT", 2) is True


def test_unwritable_data_dir_falls_back(tmp_path):
    store = AlertStore("/proc/definitely-not-writable")
    try:
        assert store.data_dir.startswith("/tmp")
        # The shared fallback DB may already hold rows, so use a unique key.
        unique_bar = int(time.time() * 1000)
        assert store.claim("FALLBACK_USDT", unique_bar) is True
    finally:
        store.close()
