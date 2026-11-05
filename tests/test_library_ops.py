import io
import json
from pathlib import Path

import pytest
import requests
import rmscene

from rmdesk.device import CommandResult, DeviceError
from rmdesk.library import ops
from rmdesk.library.model import XOCHITL, Item
from rmdesk.notebook import render
from tests.fakes import FakeDevice


class Resp:
    def __init__(self, status=200, content=b"", chunks=None, exc=None):
        self.status_code = status
        self.content = content
        self._chunks = chunks
        self._exc = exc
        self.closed = False

    def close(self):
        self.closed = True

    def iter_content(self, n):
        if self._chunks is not None:
            yield from self._chunks()
        else:
            yield self.content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class StubSession:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    def get(self, url, **kw):
        self.calls.append(("GET", url))
        r = self.responses.get(url, Resp())
        if isinstance(r, Exception):
            raise r
        return r

    def post(self, url, **kw):
        self.calls.append(("POST", url, sorted(kw.get("files", {}))))
        return Resp()


def df_out(avail_kb: int) -> str:
    """BusyBox `df -Pk /home` output (as on the tablet)."""
    return ("Filesystem 1024-blocks Used Available Capacity Mounted on\n"
            f"/dev/mmcblk2p4 7158616 902288 {avail_kb} 13% /home\n")


def dev(avail_kb=100_000_000):
    def h(cmd):
        if cmd.startswith("df "):
            assert cmd == "df -Pk /home"
            return CommandResult(0, df_out(avail_kb), "")
        return None  # built-in emulation (systemctl is-active -> active)
    return FakeDevice(run_handler=h)


def restarts(d):
    return [a for m, a in d.log if m == "run" and "systemctl restart xochitl" in a]


def pdf(tmp_path, name="a.pdf", data=b"%PDF-1.4"):
    p = tmp_path / name
    p.write_bytes(data)
    return p


def test_upload_sftp_writes_three_files_and_restarts(tmp_path):
    d = dev()
    res = ops.upload(d, "wifi", [pdf(tmp_path, "Book.pdf")], "folder1").results
    assert [r.ok for r in res] == [True]
    names = sorted(p[len(XOCHITL) + 1:] for p in d.files)
    uid = names[0].split(".")[0]
    assert names == [f"{uid}.content", f"{uid}.metadata", f"{uid}.pdf"]
    meta = json.loads(d.files[f"{XOCHITL}/{uid}.metadata"])
    assert meta["visibleName"] == "Book" and meta["type"] == "DocumentType"
    assert meta["parent"] == "folder1" and meta["version"] == 0
    assert meta["deleted"] is False and meta["pinned"] is False
    assert meta["lastOpened"] == "0" and meta["createdTime"] == meta["lastModified"]
    assert isinstance(meta["lastModified"], str)
    assert json.loads(d.files[f"{XOCHITL}/{uid}.content"]) == {"fileType": "pdf"}
    assert len(restarts(d)) == 1
    assert not any(p.endswith(".rmdesk-tmp") for p in d.files)


def test_upload_usb_uses_web(tmp_path):
    s = StubSession()
    web = ops.UsbWeb(session=s)
    d = dev()
    res = ops.upload(d, "usb", [pdf(tmp_path)], "f1", web=web).results
    assert res[0].ok
    assert s.calls[0] == ("GET", "http://10.11.99.1/documents/")
    assert s.calls[1] == ("GET", "http://10.11.99.1/documents/f1")
    assert s.calls[2][:2] == ("POST", "http://10.11.99.1/upload")
    assert s.calls[2][2] == ["file"]
    assert d.files == {} and restarts(d) == []


def test_upload_rejects_non_pdf(tmp_path):
    d = dev()
    res = ops.upload(d, "wifi", [pdf(tmp_path, "x.docx")], "").results
    assert not res[0].ok and res[0].error == "not a PDF/EPUB"
    assert d.files == {} and restarts(d) == []


def test_upload_partial_failure_continues(tmp_path):
    d = dev()
    res = ops.upload(d, "wifi", [pdf(tmp_path, "a.pdf"), pdf(tmp_path, "b.docx"),
                                 pdf(tmp_path, "c.epub")], "").results
    assert [r.ok for r in res] == [True, False, True]
    assert res[1].error == "not a PDF/EPUB"
    assert len(restarts(d)) == 1
    assert any(p.endswith(".epub") for p in d.files)


def test_upload_odd_names(tmp_path):
    d = dev()
    ops.upload(d, "wifi", [pdf(tmp_path, "My Notes ü.PDF")], "")
    metas = [v for p, v in d.files.items() if p.endswith(".metadata")]
    assert json.loads(metas[0])["visibleName"] == "My Notes ü"
    assert any(p.endswith(".pdf") for p in d.files)


def test_upload_storage_full(tmp_path):
    d = dev(avail_kb=1)
    res = ops.upload(d, "wifi", [pdf(tmp_path)], "").results
    assert not res[0].ok and res[0].error == "tablet storage full"
    assert d.files == {} and restarts(d) == []


def test_upload_failure_not_counted_for_restart(tmp_path):
    d = dev()
    d.fail_on["write_bytes"] = DeviceError("boom")
    res = ops.upload(d, "wifi", [pdf(tmp_path)], "").results
    assert not res[0].ok and "boom" in res[0].error
    assert restarts(d) == []


def make_item(kind="notebook"):
    return Item("n1", "N", kind, "", 0, ["p1"])


def test_trash_sets_parent_trash():
    meta = {"visibleName": "N", "parent": "x", "type": "DocumentType",
            "lastModified": "1", "extra": [1]}
    d = dev()
    d.files[f"{XOCHITL}/n1.metadata"] = json.dumps(meta).encode()
    d.files[f"{XOCHITL}/n1.content"] = b"{}"
    d.files[f"{XOCHITL}/n1/p1.rm"] = b"x"
    ops.trash(d, make_item())
    new = json.loads(d.files[f"{XOCHITL}/n1.metadata"])
    assert new["parent"] == "trash" and new["extra"] == [1]
    assert new["lastModified"] != "1" and isinstance(new["lastModified"], str)
    assert new["visibleName"] == "N"
    assert f"{XOCHITL}/n1.content" in d.files and f"{XOCHITL}/n1/p1.rm" in d.files
    assert len(restarts(d)) == 1


def _put(d, uid, parent="x"):
    meta = {"visibleName": uid, "parent": parent, "type": "DocumentType",
            "lastModified": "1", "extra": uid}
    d.files[f"{XOCHITL}/{uid}.metadata"] = json.dumps(meta).encode()


def test_trash_restart_false_skips_systemctl():
    d = dev()
    _put(d, "n1")
    ops.trash(d, make_item(), restart=False)
    assert json.loads(d.files[f"{XOCHITL}/n1.metadata"])["parent"] == "trash"
    assert restarts(d) == []


def test_trash_many_edits_all_without_restart():
    d = dev()
    items = [Item(u, u, "pdf", "", 0) for u in ("a", "b", "c")]
    for u in ("a", "b", "c"):
        _put(d, u)
    res = ops.trash_many(d, items)
    assert [r.ok for r in res] == [True, True, True]
    for u in ("a", "b", "c"):
        new = json.loads(d.files[f"{XOCHITL}/{u}.metadata"])
        assert new["parent"] == "trash" and new["extra"] == u and new["lastModified"] != "1"
        assert not any(n.endswith(".rmdesk-tmp") for n in d.files)
    assert not any("systemctl" in a for m, a in d.log)


def test_trash_many_failure_continues():
    d = dev()
    items = [Item(u, u, "pdf", "", 0) for u in ("a", "missing", "c")]
    _put(d, "a")
    _put(d, "c")
    res = ops.trash_many(d, items)
    assert [r.ok for r in res] == [True, False, True]
    assert res[1].error and res[1].path.name == "missing"
    assert json.loads(d.files[f"{XOCHITL}/c.metadata"])["parent"] == "trash"
    assert not any("systemctl" in a for m, a in d.log)


def test_download_original(tmp_path):
    d = dev()
    d.files[f"{XOCHITL}/d1.pdf"] = b"PDFDATA"
    dest = tmp_path / "out.pdf"
    ops.download_original(d, Item("d1", "D", "pdf", "", 0), dest)
    assert dest.read_bytes() == b"PDFDATA"
    assert not Path(str(dest) + ".part").exists()


def test_download_usb_atomic_failure(tmp_path):
    def chunks():
        yield b"%PDFabc"
        raise requests.ConnectionError("cut")
    s = StubSession({"http://10.11.99.1/download/n1/pdf": Resp(chunks=chunks)})
    web = ops.UsbWeb(session=s)
    dest = tmp_path / "o.pdf"
    with pytest.raises(Exception):
        ops.download_pdf(dev(), "usb", make_item(), dest, web=web)
    assert not dest.exists() and not Path(str(dest) + ".part").exists()


def test_download_usb_success_and_placeholder_fallback(tmp_path):
    s = StubSession({"http://10.11.99.1/download/n1/pdf": Resp(404),
                     "http://10.11.99.1/download/n1/placeholder": Resp(content=b"%PDFPH")})
    dest = tmp_path / "o.pdf"
    ops.download_pdf(dev(), "usb", make_item(), dest, web=ops.UsbWeb(session=s))
    assert dest.read_bytes() == b"%PDFPH"
    assert ("GET", "http://10.11.99.1/download/n1/placeholder") in s.calls


def test_download_wifi_notebook_uses_writer(tmp_path):
    d = dev()
    d.files[f"{XOCHITL}/n1/p1.rm"] = b"not a real page"

    def fake_svgs(device, item, root=XOCHITL):
        return [b"<svg/>"]
    orig = ops.notebook_svgs
    ops.notebook_svgs = fake_svgs
    try:
        got = {}

        def writer(svgs, path):
            got["svgs"], got["path"] = svgs, path
            path.write_bytes(b"PDF")
        dest = tmp_path / "o.pdf"
        ops.download_pdf(d, "wifi", make_item(), dest, writer=writer)
    finally:
        ops.notebook_svgs = orig
    assert dest.read_bytes() == b"PDF" and got["svgs"] == [b"<svg/>"]
    assert got["path"].name == "o.pdf.part" and not got["path"].exists()


def test_download_wifi_writer_failure_cleans_part(tmp_path):
    def writer(svgs, path):
        path.write_bytes(b"half")
        raise RuntimeError("x")
    dest = tmp_path / "o.pdf"
    d = dev()
    d.files[f"{XOCHITL}/n1/p1.rm"] = b""
    ops.notebook_svgs = lambda *a, **k: [b"<svg/>"]
    try:
        with pytest.raises(RuntimeError):
            ops.download_pdf(d, "wifi", make_item(), dest, writer=writer)
    finally:
        from rmdesk.notebook.render import notebook_svgs
        ops.notebook_svgs = notebook_svgs
    assert not dest.exists() and not Path(str(dest) + ".part").exists()


def test_download_wifi_pdf_needs_usb(tmp_path):
    with pytest.raises(ops.NeedsUsb, match="Connect over USB"):
        ops.download_pdf(dev(), "wifi", make_item("pdf"), tmp_path / "o.pdf",
                         writer=lambda s, p: None)
    assert not (tmp_path / "o.pdf").exists()


def test_usbweb_available_never_raises():
    assert ops.UsbWeb(session=StubSession(
        {"http://10.11.99.1/documents/": requests.ConnectTimeout()})).available() is False
    assert ops.UsbWeb(session=StubSession(
        {"http://10.11.99.1/documents/": RuntimeError("x")})).available() is False
    assert ops.UsbWeb(session=StubSession()).available() is True
    assert ops.UsbWeb(session=StubSession(
        {"http://10.11.99.1/documents/": Resp(500)})).available() is False


def test_usb_download_used_when_web_available_even_for_pdf(tmp_path):
    s = StubSession({"http://10.11.99.1/download/d1/pdf": Resp(content=b"%PDFX")})
    dest = tmp_path / "o.pdf"
    ops.download_pdf(dev(), "usb", Item("d1", "D", "pdf", "", 0), dest,
                     web=ops.UsbWeb(session=s))
    assert dest.read_bytes() == b"%PDFX"


def _page_bytes():
    buf = io.BytesIO()
    rmscene.write_blocks(buf, rmscene.simple_text_document("hello"))
    return buf.getvalue()


def test_page_svg_renders():
    svg = render.page_svg(_page_bytes())
    assert isinstance(svg, bytes) and b"<svg" in svg


def test_notebook_svgs_with_blank_page():
    d = FakeDevice({f"{XOCHITL}/n1/p1.rm": _page_bytes()})
    item = Item("n1", "N", "notebook", "", 0, ["p1", "p2"])
    svgs = render.notebook_svgs(d, item)
    assert len(svgs) == 2
    assert b"<svg" in svgs[0]
    assert b"1404" in svgs[1] and b"1872" in svgs[1]


def test_upload_restart_failure_reports_but_keeps_results(tmp_path):
    def h(cmd):
        if cmd.startswith("df "):
            return CommandResult(0, df_out(100_000_000), "")
        if "systemctl restart" in cmd:
            raise DeviceError("restart failed")
        return None
    d = FakeDevice(run_handler=h)
    rep = ops.upload(d, "wifi", [pdf(tmp_path, "a.pdf"), pdf(tmp_path, "b.pdf")], "")
    assert [r.ok for r in rep.results] == [True, True]
    assert "restart failed" in rep.restart_error


def test_usb_download_rejects_non_pdf(tmp_path):
    resp = Resp(content=b"<html>nope</html>")
    s = StubSession({"http://10.11.99.1/download/n1/pdf": resp})
    dest = tmp_path / "o.pdf"
    with pytest.raises(DeviceError, match="did not return a PDF"):
        ops.download_pdf(dev(), "usb", make_item(), dest, web=ops.UsbWeb(session=s))
    assert not dest.exists() and not Path(str(dest) + ".part").exists()
    assert resp.closed


class MetaFailDevice(FakeDevice):
    def write_bytes(self, path, data):
        if ".metadata" in path:
            raise DeviceError("meta write failed")
        super().write_bytes(path, data)


def test_upload_metadata_failure_cleans_orphans(tmp_path):
    def h(cmd):
        if cmd.startswith("df "):
            return CommandResult(0, df_out(100_000_000), "")
        if cmd.startswith("rm -f "):
            import shlex
            for n in shlex.split(cmd)[2:]:
                d.files.pop(n, None)
        return None
    d = MetaFailDevice(run_handler=h)
    rep = ops.upload(d, "wifi", [pdf(tmp_path)], "")
    assert not rep.results[0].ok
    assert d.files == {}
    assert restarts(d) == [] and rep.restart_error is None


def test_free_bytes_parses_busybox_df():
    d = dev(avail_kb=5871628)
    assert ops._free_bytes(d) == 5871628 * 1024


def test_free_bytes_unparseable_is_none():
    d = FakeDevice(run_handler=lambda cmd: CommandResult(1, "", "df: unrecognized option"))
    assert ops._free_bytes(d) is None
