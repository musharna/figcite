import json
import socket
import threading
import urllib.request
import pytest

from figcite import web


@pytest.mark.live
def test_the_server_serves_pending_over_a_real_socket():
    srv = web.make_server(0)  # ephemeral port
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/pending") as r:
            assert r.status == 200
            assert isinstance(json.loads(r.read())["items"], list)
    finally:
        srv.shutdown()


@pytest.mark.live
def test_it_binds_loopback_only():
    srv = web.make_server(0)
    try:
        assert srv.server_address[0] == "127.0.0.1"
    finally:
        srv.server_close()


@pytest.mark.live
def test_it_refuses_a_port_already_in_use_rather_than_moving():
    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    held.listen(1)
    port = held.getsockname()[1]
    try:
        with pytest.raises(OSError):
            web.make_server(port)
    finally:
        held.close()
