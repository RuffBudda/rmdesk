"""Pen /events client: SSE parsing, coordinate mapping, and a fake HTTPS server."""
import http.server
import json
import ssl
import threading

from rmdesk.stream import events
from rmdesk.stream.events import PenClient, PenTracker, parse_sse
from tests.test_stream import _self_signed, _wait, tcp_dial


def ev(code, value):
    return {"Source": 1, "Type": 3, "Code": code, "Value": value}


def test_parse_sse_skips_noise():
    lines = [b": hi\n", b'data: {"Code": 1, "Value": 5}\n', b"\n", b"data: nope\n",
             b'data: [1]\n', b'data:{"Code": 0, "Value": 2}\n']
    assert list(parse_sse(lines)) == [{"Code": 1, "Value": 5}, {"Code": 0, "Value": 2}]


def test_tracker_maps_like_the_web_client():
    t = PenTracker()
    assert t.feed(ev(1, 15725 // 2)) is None  # needs both axes
    x, y, vis = t.feed(ev(0, 0))
    assert abs(x - 702) < 0.5 and y == 1872 and vis is False
    assert t.feed(ev(25, 10))[2] is True
    assert t.feed(ev(0, 20966))[1] == 0
    assert t.feed(ev(24, 300))[2] is False
    assert t.feed(ev(99, 1)) is None


class FakeEvents:
    def __init__(self, tmp_path, lines):
        outer = self
        self.auth = []

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                body = b'{"token": "tok"}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                outer.auth.append((self.path, self.headers.get("Authorization")))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Connection", "close")
                self.end_headers()
                for line in lines:
                    self.wfile.write(b"%x\r\n" % len(line) + line + b"\r\n")
                    self.wfile.flush()
                threading.Event().wait(10)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(*_self_signed(tmp_path))
        self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def test_pen_client_streams_positions_and_stops(tmp_path):
    lines = [b"data: " + json.dumps(ev(c, v)).encode() + b"\n\n"
             for c, v in [(1, 7862), (0, 10483), (25, 3)]]
    srv = FakeEvents(tmp_path, lines)
    got = []
    client = PenClient("127.0.0.1", "pw", lambda *a: got.append(a), port=srv.port,
                       dial=tcp_dial("127.0.0.1", srv.port))
    try:
        client.start()
        assert _wait(lambda: got)
        x, y, vis = got[-1]
        assert abs(x - 702) < 1 and abs(y - 936) < 1 and vis
        assert srv.auth[0] == ("/events", "Bearer tok")
        client.stop()
        assert not client.is_alive()
    finally:
        client.stop()
        srv.close()


def test_events_module_has_no_qt():
    assert "PySide6" not in open(events.__file__).read()


def test_overlong_event_line_aborts():
    import io
    import pytest
    from rmdesk.stream.client import StreamError
    from rmdesk.stream.events import read_lines
    with pytest.raises(StreamError):
        list(read_lines(io.BytesIO(b"data: " + b"x" * 200), limit=64))
    assert list(read_lines(io.BytesIO(b"data: 1\n"), limit=64)) == [b"data: 1\n"]
