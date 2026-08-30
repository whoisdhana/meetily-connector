from meetily_connector.outbox import StateStore


def _store(cfg):
    cfg.retry_base_seconds = 30
    cfg.retry_max_seconds = 3600
    return StateStore(cfg)


def test_enqueue_due_success_flow(cfg):
    store = _store(cfg)
    try:
        store.enqueue("m1", {"event": "meeting.completed"})
        due = store.due()
        assert [(m, a) for m, _, a in due] == [("m1", 0)]
        store.record_success("m1", 202, "abc123")
        assert store.due() == []
        assert store.depth() == 0
        assert "m1" in store.seen_meeting_ids()
    finally:
        store.close()


def test_transient_failure_backs_off_and_retries_forever(cfg):
    store = _store(cfg)
    try:
        store.enqueue("m1", {})
        for attempt in range(1, 12):
            store.record_failure("m1", attempt - 1, "transport_ConnectionRefusedError", None)
            row = store.conn.execute(
                "SELECT attempts FROM outbox WHERE meeting_id='m1'").fetchone()
            assert row is not None, "transient failures must never give up"
            assert row[0] == attempt
        # backoff is capped: next_attempt_at within max_seconds * 1.2 jitter
    finally:
        store.close()


def test_permanent_4xx_gives_up_after_cap(cfg):
    store = _store(cfg)
    try:
        store.enqueue("m1", {})
        attempts = 0
        for _ in range(10):
            row = store.conn.execute(
                "SELECT attempts FROM outbox WHERE meeting_id='m1'").fetchone()
            if row is None:
                break
            store.record_failure("m1", row[0], "http_401", 401)
            attempts += 1
        disposition = store.conn.execute(
            "SELECT disposition FROM sent_log WHERE meeting_id='m1'").fetchone()
        assert disposition == ("gave_up",)
        assert attempts == 5
    finally:
        store.close()


def test_429_is_retryable(cfg):
    store = _store(cfg)
    try:
        store.enqueue("m1", {})
        for attempt in range(8):
            store.record_failure("m1", attempt, "http_429", 429)
        assert store.conn.execute(
            "SELECT COUNT(*) FROM outbox WHERE meeting_id='m1'").fetchone() == (1,)
    finally:
        store.close()


def test_replay_forgets(cfg):
    store = _store(cfg)
    try:
        store.enqueue("m1", {})
        store.record_success("m1", 202, "sha")
        assert store.forget("m1") is True
        assert store.seen_meeting_ids() == set()
        assert store.forget("m1") is False
    finally:
        store.close()
