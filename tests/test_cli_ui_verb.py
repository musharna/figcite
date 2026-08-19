from figcite import cli


def test_ui_verb_passes_the_port_through(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        "figcite.web.serve",
        lambda port=8765, open_browser=False: seen.update(
            port=port, open_browser=open_browser
        ),
    )
    assert cli.main(["ui", "--port", "9001", "--open"]) == 0
    assert seen == {"port": 9001, "open_browser": True}
