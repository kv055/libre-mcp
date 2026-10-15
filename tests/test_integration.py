"""End-to-end test against a real LibreOffice install.

Skipped automatically when soffice or its bundled python cannot be found.
"""

import os

import pytest

from src.config import Config
from src.office import discovery
from src.office.session import OfficeSession

pytestmark = pytest.mark.asyncio


def _have_libreoffice() -> bool:
    try:
        soffice = discovery.find_soffice(None)
        discovery.find_bundled_python(None, soffice)
        return True
    except discovery.DiscoveryError:
        return False


requires_lo = pytest.mark.skipif(
    not _have_libreoffice(), reason="LibreOffice not installed"
)


@pytest.fixture
async def session(tmp_path):
    config = Config()
    config.profile_dir = str(tmp_path / "profiles")
    config.keep_profile = False
    s = OfficeSession(config)
    try:
        yield s
    finally:
        await s.shutdown()


@requires_lo
async def test_writer_roundtrip(session, tmp_path):
    doc = await session.call("create_document", kind="writer")
    doc_id = doc["doc_id"]
    await session.call("insert_text", doc_id=doc_id, text="Hello libre-mcp")
    out = await session.call("get_text", doc_id=doc_id)
    assert "Hello libre-mcp" in out["text"]

    pdf = str(tmp_path / "out.pdf")
    res = await session.call("export_document", doc_id=doc_id, path=pdf, format="pdf")
    assert os.path.exists(res["path"])
    with open(res["path"], "rb") as fh:
        assert fh.read(5) == b"%PDF-"


@requires_lo
async def test_calc_formula(session):
    doc = await session.call("create_document", kind="calc")
    doc_id = doc["doc_id"]
    await session.call(
        "set_cells",
        doc_id=doc_id,
        cells=[
            {"cell": "A1", "value": 21},
            {"cell": "A2", "value": 2},
            {"cell": "A3", "formula": "=A1*A2"},
        ],
    )
    out = await session.call("read_cells", doc_id=doc_id, range="A3:A3")
    assert out["values"][0][0] == 42.0


@requires_lo
async def test_draw_text_edit(session):
    doc = await session.call("create_document", kind="draw")
    doc_id = doc["doc_id"]
    added = await session.call(
        "draw_add_textbox",
        doc_id=doc_id,
        page=0,
        text="Hello draw",
        x_cm=2.0,
        y_cm=2.0,
        w_cm=8.0,
        h_cm=1.0,
    )
    idx = added["index"]
    shapes = await session.call("draw_list_shapes", doc_id=doc_id, page=0)
    assert shapes["count"] >= 1
    assert any(
        s["index"] == idx and s["text"] == "Hello draw" for s in shapes["shapes"]
    )

    await session.call(
        "draw_set_text", doc_id=doc_id, page=0, shape=idx, text="Hello edited"
    )
    out = await session.call("draw_get_text", doc_id=doc_id, page=0, shape=idx)
    assert out["text"] == "Hello edited"

    moved = await session.call(
        "draw_move_shape", doc_id=doc_id, page=0, shape=idx, dx_cm=1.0, dy_cm=0.5
    )
    assert moved["x_cm"] == pytest.approx(3.0)
    assert moved["y_cm"] == pytest.approx(2.5)

    before = (await session.call("draw_list_shapes", doc_id=doc_id, page=0))["count"]
    await session.call("draw_delete_shape", doc_id=doc_id, page=0, shape=idx)
    after = (await session.call("draw_list_shapes", doc_id=doc_id, page=0))["count"]
    assert after == before - 1


@requires_lo
async def test_document_fonts(session):
    doc = await session.call("create_document", kind="writer")
    doc_id = doc["doc_id"]
    await session.call("insert_text", doc_id=doc_id, text="Hello fonts")
    out = await session.call("document_fonts", doc_id=doc_id)
    assert out["declared"], "fresh writer doc should declare its default font"
    assert "fontconfig" in out
    if out["fontconfig"]:
        assert set(out["resolved"]) == set(out["declared"])

    draw = await session.call("create_document", kind="draw")
    draw_id = draw["doc_id"]
    await session.call(
        "draw_add_textbox",
        doc_id=draw_id,
        page=0,
        text="Bogus family",
        x_cm=2.0,
        y_cm=2.0,
        w_cm=8.0,
        h_cm=1.0,
        font="NoSuchFontXYZ",
    )
    out = await session.call("document_fonts", doc_id=draw_id)
    assert "NoSuchFontXYZ" in out["declared"]
    if out["fontconfig"]:
        assert "NoSuchFontXYZ" in out["substituted"]


@requires_lo
async def test_draw_audit_overlaps(session):
    doc = await session.call("create_document", kind="draw")
    doc_id = doc["doc_id"]
    a = await session.call(
        "draw_add_textbox",
        doc_id=doc_id,
        page=0,
        text="Alpha box",
        x_cm=2.0,
        y_cm=2.0,
        w_cm=6.0,
        h_cm=2.0,
    )
    b = await session.call(
        "draw_add_textbox",
        doc_id=doc_id,
        page=0,
        text="Beta box",
        x_cm=4.0,
        y_cm=3.0,
        w_cm=6.0,
        h_cm=2.0,
    )
    c = await session.call(
        "draw_add_textbox",
        doc_id=doc_id,
        page=0,
        text="Far away",
        x_cm=20.0,
        y_cm=20.0,
        w_cm=4.0,
        h_cm=2.0,
    )
    out = await session.call("draw_audit_overlaps", doc_id=doc_id, page=0)
    pairs = {(o["a"]["index"], o["b"]["index"]) for o in out["overlaps"]}
    assert (a["index"], b["index"]) in pairs
    assert all(c["index"] not in pair for pair in pairs)
    ov = next(
        o
        for o in out["overlaps"]
        if (o["a"]["index"], o["b"]["index"]) == (a["index"], b["index"])
    )
    assert ov["w_cm"] == pytest.approx(4.0)
    assert ov["h_cm"] == pytest.approx(1.0)
