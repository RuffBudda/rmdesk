import json
from pathlib import Path

from rmdesk.library import model
from tests.fakes import FakeDevice

FIX = Path(__file__).parent / "fixtures" / "xochitl"
ROOT = "/x"


def device(extra=None):
    files = {f"{ROOT}/{p.name}": p.read_bytes() for p in FIX.iterdir() if p.is_file()}
    files[f"{ROOT}/doc-nb.thumbnails/x"] = b""
    files.update(extra or {})
    return FakeDevice(files)


def lib(extra=None):
    return model.load(device(extra), ROOT)


def test_tree_children_sorted():
    names = [i.name for i in lib().children()]
    assert names == ["Alpha", "beta", "Meeting notes", "Novel", "Paper"]
    assert [i.kind for i in lib().children()][:2] == ["folder", "folder"]


def test_nested_folder_path():
    library = lib()
    assert [i.id for i in library.path("doc-nested")] == ["folder-a", "folder-sub"]
    assert [i.id for i in library.children("folder-sub")] == ["doc-nested"]


def test_trash_hidden_from_root_and_search():
    library = lib()
    assert "doc-trash" not in [i.id for i in library.children()]
    assert [i.id for i in library.children("trash")] == ["doc-trash"]
    assert library.search("old") == []
    assert [i.id for i in library.search("NOTES")] == ["doc-nb"]
    assert "doc-deleted" not in library.items


def test_page_ids_from_cpages():
    library = lib()
    assert library.items["doc-nb"].page_ids == ["pg-1", "pg-2"]
    assert library.items["doc-pdf"].page_ids == ["pp-1", "pp-2"]
    assert library.items["folder-a"].page_ids == []


def test_kinds_and_modified():
    items = lib().items
    assert items["doc-pdf"].kind == "pdf"
    assert items["doc-epub"].kind == "epub"
    assert items["doc-nested"].kind == "notebook"
    assert items["doc-pdf"].modified == 1700000500000


def test_corrupt_metadata_skipped():
    library = lib()
    assert "doc-bad" not in library.items
    assert "doc-nb" in library.items


def test_missing_keys_bad_content_and_missing_content_skipped():
    meta = json.dumps({"visibleName": "X", "type": "DocumentType", "parent": "",
                       "lastModified": 5}).encode()
    extra = {
        f"{ROOT}/nokey.metadata": b'{"type": "DocumentType"}',
        f"{ROOT}/nocontent.metadata": meta,
        f"{ROOT}/badcontent.metadata": meta,
        f"{ROOT}/badcontent.content": b"nope",
        f"{ROOT}/intmod.metadata": meta,
        f"{ROOT}/intmod.content": b'{"fileType": "notebook"}',
    }
    library = lib(extra)
    assert "nokey" not in library.items
    assert "nocontent" not in library.items
    assert "badcontent" not in library.items
    assert library.items["intmod"].modified == 5


def test_device_error_on_item_skips_but_listdir_error_propagates():
    import pytest
    from rmdesk.device import DeviceError
    d = device()
    del d.files[f"{ROOT}/doc-pdf.content"]
    assert "doc-pdf" not in model.load(d, ROOT).items
    with pytest.raises(DeviceError):
        model.load(FakeDevice({}), ROOT)
