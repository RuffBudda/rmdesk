import json

import pytest

from rmdesk.device import DeviceError
from tests.fakes import FailRule, FakeDevice
from rmdesk.library.model import XOCHITL
from rmdesk.notebook import opened
from rmdesk.notebook.opened import (BLANK_PAGE_MSG, ZOOM_MSG, NotEditable,
                                    find_opened_page)


def nb(files, uid, pages, *, opened_at="1000", page=0, modified="1", name="N",
       parent="", deleted=False, write_pages=True, **content_extra):
    meta = {"visibleName": name, "type": "DocumentType", "parent": parent,
            "lastOpened": opened_at, "lastOpenedPage": page, "lastModified": modified,
            "deleted": deleted}
    content = {"fileType": "notebook", "orientation": "portrait", "pageCount": len(pages),
               "cPages": {"pages": [{"id": p, "idx": {"value": "a"}} if p[0] != "!" else
                                    {"id": p[1:], "deleted": {"value": 1}} for p in pages]},
               **content_extra}
    files[f"{XOCHITL}/{uid}.metadata"] = json.dumps(meta).encode()
    files[f"{XOCHITL}/{uid}.content"] = json.dumps(content).encode()
    if write_pages:
        for p in pages:
            p = p.lstrip("!")
            files[f"{XOCHITL}/{uid}/{p}.rm"] = f"page-{p}".encode()


def find(files):
    return find_opened_page(FakeDevice(files))


def test_most_recent_notebook_and_page():
    f = {}
    nb(f, "old", ["o1"], opened_at="100")
    nb(f, "new", ["p1", "p2"], opened_at="200", page=1, name="Fresh")
    r = find(f)
    assert (r.notebook_id, r.name, r.page_id, r.page_index, r.page_count) == ("new", "Fresh", "p2", 1, 2)
    assert r.data == b"page-p2" and r.orientation == "portrait"


def test_deleted_pages_skipped_in_index():
    f = {}
    nb(f, "a", ["p1", "!gone", "p3"], page=1)
    r = find(f)
    assert (r.page_id, r.page_index, r.page_count) == ("p3", 1, 2)


def test_orientation_landscape():
    f = {}
    nb(f, "a", ["p1"], orientation="landscape")
    assert find(f).orientation == "landscape"


def refused(files):
    with pytest.raises(NotEditable) as e:
        find(files)
    return e.value.reason


def test_tie_breaks_on_modified_then_refuses_full_tie():
    f = {}
    nb(f, "a", ["pa"], modified="5")
    nb(f, "b", ["pb"], modified="9")
    assert find(f).notebook_id == "b"
    g = {}
    nb(g, "a", ["pa"], modified="5")
    nb(g, "b", ["pb"], modified="5")
    assert refused(g) == opened.TIE_MSG


def test_pdf_and_epub_refused():
    for t in ("pdf", "epub"):
        f = {}
        nb(f, "a", ["p1"], fileType=t)
        assert refused(f) == opened.PDF_MSG


def test_index_out_of_range_refused():
    f = {}
    nb(f, "a", ["p1"], page=3)
    assert refused(f) == opened.BAD_PAGE_MSG


@pytest.mark.parametrize("bad", [None, "x", -1, True, 1.5])
def test_garbage_page_index_refused_unless_single_page(bad):
    f = {}
    nb(f, "a", ["p1", "p2"], page=bad)
    if True:
        assert refused(f) == opened.NO_PAGE_MSG
    g = {}
    nb(g, "a", ["p1", "p2"])
    meta = json.loads(g[f"{XOCHITL}/a.metadata"])
    del meta["lastOpenedPage"]
    g[f"{XOCHITL}/a.metadata"] = json.dumps(meta).encode()
    assert refused(g) == opened.NO_PAGE_MSG
    h = {}
    nb(h, "a", ["only"])
    meta = json.loads(h[f"{XOCHITL}/a.metadata"])
    del meta["lastOpenedPage"]
    h[f"{XOCHITL}/a.metadata"] = json.dumps(meta).encode()
    assert find(h).page_index == 0


def test_garbage_page_index_single_page_uses_page_zero():
    f = {}
    nb(f, "a", ["only"], page=-1)
    assert find(f).page_index == 0


@pytest.mark.parametrize("junk", [b"{nope", b"[1]", b"\xff"])
def test_damaged_metadata_refused(junk):
    f = {}
    nb(f, "old", ["o"], opened_at="1")
    f[f"{XOCHITL}/new.metadata"] = junk
    assert "damaged file (new.metadata)" in refused(f)


def test_unreadable_metadata_refused():
    f = {}
    nb(f, "a", ["p"])
    dev = FakeDevice(f, fail_on={"read_bytes": FailRule(DeviceError("x"), match=".metadata")})
    with pytest.raises(NotEditable) as e:
        find_opened_page(dev)
    assert "damaged file (a.metadata)" in e.value.reason


def test_non_document_metadata_is_skipped():
    f = {}
    nb(f, "a", ["p"], opened_at="1")
    f[f"{XOCHITL}/x.metadata"] = json.dumps({"type": "Other"}).encode()
    assert find(f).notebook_id == "a"


def test_garbage_last_opened_refused_but_absent_is_zero():
    f = {}
    nb(f, "a", ["p"], opened_at="soon")
    assert "damaged file (a.metadata)" in refused(f)
    g = {}
    nb(g, "a", ["p"], opened_at="5")
    nb(g, "b", ["q"])
    meta = json.loads(g[f"{XOCHITL}/b.metadata"])
    del meta["lastOpened"]
    g[f"{XOCHITL}/b.metadata"] = json.dumps(meta).encode()
    assert find(g).notebook_id == "a"


def test_garbage_in_trashed_notebook_ignored():
    f = {}
    nb(f, "a", ["p"], opened_at="1")
    nb(f, "t", ["q"], opened_at="junk", parent="trash")
    assert find(f).notebook_id == "a"


def test_content_unreadable_refused():
    f = {}
    nb(f, "a", ["p"])
    dev = FakeDevice(f, fail_on={"read_bytes": FailRule(DeviceError("x"), match=".content")})
    with pytest.raises(NotEditable) as e:
        find_opened_page(dev)
    assert e.value.reason == opened.BAD_FILES_MSG


def test_missing_page_file_refused():
    f = {}
    nb(f, "a", ["p1"], write_pages=False)
    with pytest.raises(NotEditable) as e:
        find(f)
    assert e.value.reason == BLANK_PAGE_MSG


def test_bad_json_refused():
    f = {}
    nb(f, "a", ["p1"])
    f[f"{XOCHITL}/a.content"] = b"{not json"
    assert refused(f) == opened.BAD_FILES_MSG


def test_no_documents_refused():
    assert refused({f"{XOCHITL}/x.metadata": b'{"type": "CollectionType"}'}) == opened.NO_DOC_MSG


@pytest.mark.parametrize("extra", [{"zoomMode": "customFit"}, {"zoomMode": "weird"},
                                   {"customZoomScale": 1.5}])
def test_zoomed_refused(extra):
    f = {}
    nb(f, "a", ["p1"], **extra)
    with pytest.raises(NotEditable) as e:
        find(f)
    assert e.value.reason == ZOOM_MSG


REAL_DEFAULT_ZOOM = {"zoomMode": "bestFit", "customZoomScale": 1, "customZoomCenterX": 0,
                     "customZoomCenterY": 936, "customZoomOrientation": "portrait",
                     "customZoomPageWidth": 1404, "customZoomPageHeight": 1872,
                     "formatVersion": 2}  # observed on fw 3.29.1.0


@pytest.mark.parametrize("extra", [{}, {"zoomMode": "bestFit"}, {"customZoomScale": 1},
                                   REAL_DEFAULT_ZOOM])
def test_default_zoom_ok(extra):
    f = {}
    nb(f, "a", ["p1"], **extra)
    assert find(f).page_id == "p1"


def test_trashed_and_deleted_never_chosen():
    f = {}
    nb(f, "keep", ["k"], opened_at="1")
    nb(f, "t", ["t1"], opened_at="9", parent="trash")
    nb(f, "d", ["d1"], opened_at="8", deleted=True)
    f[f"{XOCHITL}/folder.metadata"] = json.dumps(
        {"visibleName": "F", "type": "CollectionType", "parent": "trash"}).encode()
    nb(f, "inf", ["i1"], opened_at="7", parent="folder")
    assert find(f).notebook_id == "keep"


def test_synced_flag_read_from_metadata():
    for value, want in ((True, True), (False, False), (None, False), ("true", False)):
        f = {}
        nb(f, "n", ["p1"])
        meta = json.loads(f[f"{XOCHITL}/n.metadata"])
        if value is not None:
            meta["synced"] = value
        f[f"{XOCHITL}/n.metadata"] = json.dumps(meta).encode()
        assert find(f).synced is want
