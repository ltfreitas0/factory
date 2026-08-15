from factory import feed


def test_publish_and_history():
    item = feed.publish("runner", "hello", ticket_id="tkt_x", state="planning")
    assert item["text"] == "hello"
    hist = feed.history()
    assert hist[-1]["kind"] == "runner"


def test_hydrate_replays():
    feed.hydrate([{"at": "01:00:00", "kind": "cycle", "text": "inbox → planning"}])
    assert feed.history()[0]["text"] == "inbox → planning"


def test_emit_reasoning_and_text():
    from factory import trace

    feed.hydrate([])
    trace.emit_record(
        {"type": "reasoning-chunks", "data": {"texts": ["The", " user"]}},
        "tkt_x",
    )
    trace.emit_record(
        {"type": "text-chunks", "data": {"texts": ["#", " Plan"]}},
        "tkt_x",
    )
    kinds = [i["kind"] for i in feed.history()]
    assert "think" in kinds
    assert "token" in kinds
