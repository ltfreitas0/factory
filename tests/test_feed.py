from factory import feed


def test_publish_and_history():
    item = feed.publish("runner", "hello", ticket_id="tkt_x", state="planning")
    assert item["text"] == "hello"
    hist = feed.history()
    assert hist[-1]["kind"] == "runner"
