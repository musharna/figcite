from figcite import cli


def test_ui_verb_passes_the_port_through(monkeypatch):
    seen = {}
    def fake_serve(port=8765, open_browser=False):
        seen.update(port=port, open_browser=open_browser)
        # serve() returns an exit code now, and cmd_ui propagates it -- a
        # fake returning None made a healthy start look like a failure.
        return 0

    monkeypatch.setattr("figcite.web.serve", fake_serve)
    assert cli.main(["ui", "--port", "9001", "--open"]) == 0
    assert seen == {"port": 9001, "open_browser": True}
