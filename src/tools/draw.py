"""Draw tools — text-first editing for Draw pages (CVs, diagrams, posters).

Positions and sizes are CENTIMETRES from the page's top-left; colors are hex
"#RRGGBB"; font sizes are points. Shapes are addressed by 0-based `shape`
index in z-order on a 0-based `page`. Deleting a shape shifts later indices —
when removing several, delete in descending index order.
"""

from mcp.server.fastmcp import FastMCP

from src.office.session import get_session
from src.tools._defaults import INK


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def draw_list_shapes(doc_id: str, page: int) -> dict:
        """List a Draw page's shapes in z-order.

        Returns {page, count, shapes: [{index, type, x_cm, y_cm, w_cm, h_cm,
        text}]}. `type` is text|rect|ellipse|line|image|path|custom|group|...;
        `text` is the shape's full string ("" when it holds none).
        """
        return await get_session().call("draw_list_shapes", doc_id=doc_id, page=page)

    @mcp.tool()
    async def draw_get_text(doc_id: str, page: int, shape: int) -> dict:
        """Read one shape's text by 0-based page and shape index.

        Returns {shape, text} ("" when the shape holds no text).
        """
        return await get_session().call(
            "draw_get_text", doc_id=doc_id, page=page, shape=shape
        )

    @mcp.tool()
    async def draw_set_text(
        doc_id: str,
        page: int,
        shape: int,
        text: str,
        size: float | None = None,
        color: str | None = None,
        bold: bool | None = None,
        italic: bool | None = None,
        align: str | None = None,
        valign: str | None = None,
        font: str | None = None,
    ) -> dict:
        """Replace a text shape's string. Returns {shape}.

        The new string replaces the shape's text runs; pass style options to
        restyle it (size in points, hex color, bold/italic, align
        left|center|right|justify, valign top|center|bottom, font), or omit
        them to keep the shape's current formatting. Position and size are
        untouched. Refuses shapes that hold no editable text.
        """
        return await get_session().call(
            "draw_set_text",
            doc_id=doc_id,
            page=page,
            shape=shape,
            text=text,
            size=size,
            color=color,
            bold=bold,
            italic=italic,
            align=align,
            valign=valign,
            font=font,
        )

    @mcp.tool()
    async def draw_delete_shape(doc_id: str, page: int, shape: int) -> dict:
        """Delete a shape by 0-based page and shape index. Returns {count}.

        Later shape indices shift down — when deleting several, go in
        descending index order.
        """
        return await get_session().call(
            "draw_delete_shape", doc_id=doc_id, page=page, shape=shape
        )

    @mcp.tool()
    async def draw_move_shape(
        doc_id: str, page: int, shape: int, dx_cm: float = 0, dy_cm: float = 0
    ) -> dict:
        """Shift a shape by dx_cm/dy_cm centimetres. Returns {shape, x_cm, y_cm}."""
        return await get_session().call(
            "draw_move_shape",
            doc_id=doc_id,
            page=page,
            shape=shape,
            dx_cm=dx_cm,
            dy_cm=dy_cm,
        )

    @mcp.tool()
    async def draw_add_textbox(
        doc_id: str,
        page: int,
        text: str,
        x_cm: float,
        y_cm: float,
        w_cm: float,
        h_cm: float,
        size: float = 11,
        color: str = INK,
        bold: bool = False,
        italic: bool = False,
        align: str = "left",
        valign: str = "top",
        font: str | None = None,
    ) -> dict:
        """Place a styled text box; geometry in centimetres. Returns {index}.

        size is in points; color is hex; align is left|center|right|justify;
        valign is top|center|bottom. Use newlines in `text` for multiple lines.
        """
        return await get_session().call(
            "draw_add_textbox",
            doc_id=doc_id,
            page=page,
            text=text,
            x_cm=x_cm,
            y_cm=y_cm,
            w_cm=w_cm,
            h_cm=h_cm,
            size=size,
            color=color,
            bold=bold,
            italic=italic,
            align=align,
            valign=valign,
            font=font,
        )
