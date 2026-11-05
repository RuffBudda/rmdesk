import io

import rmscene

from rmdesk.notebook.render import page_svg
from rmdesk.ui.pdf_export import write_pdf


def test_write_pdf_two_pages(qtbot, tmp_path):
    buf = io.BytesIO()
    rmscene.write_blocks(buf, rmscene.simple_text_document("hello"))
    svg = page_svg(buf.getvalue())
    dest = tmp_path / "out.pdf"
    write_pdf([svg, svg], dest)
    data = dest.read_bytes()
    assert data.startswith(b"%PDF")
    assert data.count(b"/Type /Page\n") + data.count(b"/Type /Page ") + \
        data.count(b"/Type /Page\r") == 2
