"""UNO worker — runs under LibreOffice's BUNDLED Python interpreter.

STDLIB ONLY. This file must not import any third-party package: the bundled
interpreter is a reduced 3.10 build (no sqlite3/lzma) and we deliberately keep
it dependency-free so there is nothing to install into the app bundle.

It connects to a headless soffice over URP, then serves newline-delimited JSON
requests on stdin and writes JSON responses on stdout. All diagnostics go to
stderr. Document handles are kept in-process and referenced by doc_id.

Run:  <bundled-python> uno_worker.py --url "uno:socket,host=127.0.0.1,port=2002;urp;StarOffice.ComponentContext"
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

import uno  # provided by the bundled interpreter
import unohelper  # first-party UNO helper (ships with LibreOffice)
from com.sun.star.beans import PropertyValue
from com.sun.star.util import XModifyListener


def eprint(*a) -> None:
    print(*a, file=sys.stderr, flush=True)


def _prop(name, value):
    p = PropertyValue()
    p.Name = name
    p.Value = value
    return p


# Export filter names keyed by (doc-kind, format). Format is a short token.
_FILTERS = {
    "writer": {
        "odt": "writer8",
        "pdf": "writer_pdf_Export",
        "docx": "MS Word 2007 XML",
        "doc": "MS Word 97",
        "rtf": "Rich Text Format",
        "txt": "Text",
        "html": "HTML (StarWriter)",
    },
    "calc": {
        "ods": "calc8",
        "pdf": "calc_pdf_Export",
        "xlsx": "Calc MS Excel 2007 XML",
        "xls": "MS Excel 97",
        "csv": "Text - txt - csv (StarCalc)",
        "html": "HTML (StarCalc)",
    },
    "impress": {
        "odp": "impress8",
        "pdf": "impress_pdf_Export",
        "pptx": "Impress MS PowerPoint 2007 XML",
    },
    "draw": {"odg": "draw8", "pdf": "draw_pdf_Export"},
}

_FACTORY = {
    "writer": "private:factory/swriter",
    "calc": "private:factory/scalc",
    "impress": "private:factory/simpress",
    "draw": "private:factory/sdraw",
}

# Slide presets, (width, height) in 1/100 mm.
_PRESET_SIZES = {"16:9": (33867, 19050), "4:3": (25400, 19050)}

# Drawing shape services keyed by a short name.
_SHAPES = {
    "rect": "com.sun.star.drawing.RectangleShape",
    "round": "com.sun.star.drawing.RectangleShape",
    "ellipse": "com.sun.star.drawing.EllipseShape",
    "line": "com.sun.star.drawing.LineShape",
    "text": "com.sun.star.drawing.TextShape",
}

# Service-name suffix -> short shape type reported by draw_list_shapes.
_DRAW_TYPES = {
    "TextShape": "text",
    "TitleTextShape": "text",
    "OutlinerShape": "text",
    "RectangleShape": "rect",
    "EllipseShape": "ellipse",
    "LineShape": "line",
    "GraphicObjectShape": "image",
    "PolygonShape": "path",
    "PolyPolygonShape": "path",
    "PolyLineShape": "path",
    "PathShape": "path",
    "CustomShape": "custom",
    "GroupShape": "group",
    "MeasureShape": "measure",
    "OLE2Shape": "ole",
    "FrameShape": "frame",
    "MediaShape": "media",
}


def _color(value):
    """Accept '#RRGGBB' (or 3-digit '#RGB', or an int) and return an int RGB."""
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        if not 0 <= value <= 0xFFFFFF:
            raise ValueError(f"bad color int: {value!r} (want 0..0xFFFFFF)")
        return value
    text = str(value).strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        raise ValueError(f"bad color: {value!r} (want '#RRGGBB', '#RGB', or int)")
    try:
        return int(text, 16)
    except ValueError:
        raise ValueError(
            f"bad color: {value!r} (want '#RRGGBB', '#RGB', or int)"
        ) from None


def _cm(value):
    """Centimetres -> 1/100 mm (UNO's metric unit)."""
    return int(round(float(value) * 1000))


def _col_to_index(spec) -> int:
    """Spreadsheet column letters -> 0-based index (A=0, Z=25, AA=26, ...)."""
    text = str(spec).strip().upper()
    if not text or not text.isalpha() or not text.isascii():
        raise ValueError(f"invalid column: {spec!r}")
    index = 0
    for ch in text:
        index = index * 26 + (ord(ch) - 64)
    index -= 1
    if index > 16383:
        raise ValueError(f"invalid column: {spec!r} (beyond XFD)")
    return index


# Largest cell range a single read_cells call will materialize. Whole-sheet
# ranges would hang or OOM building the JSON payload; narrow the range instead.
_MAX_READ_CELLS = 100000


# Worker-internal default colors. Tool-facing defaults live in
# src/tools/_defaults.py; these cover the few cases a tool doesn't expose.
_TABLE_HEADER_TEXT = "#ffffff"
_LINE_FALLBACK = "#000000"


class _RevListener(unohelper.Base, XModifyListener):
    """Bumps a per-document revision counter on every modification — whether the
    change comes from one of our tool calls or from a human editing the live
    window. Works for any document kind (Writer/Calc/Impress/Draw all support
    XModifiable), which lets the worker detect out-of-band edits and refuse to
    clobber them. See the concurrency guard in serve()."""

    def __init__(self, bump):
        self._bump = bump

    def modified(self, event):  # com.sun.star.lang.EventObject
        self._bump()

    def disposing(self, event):  # XEventListener
        pass


# _show() retry budget: a frame fresh off the loader is sometimes not
# realizable the very instant loading returns.
_SHOW_ATTEMPTS = 10
_SHOW_RETRY_DELAY = 0.2


class Worker:
    def __init__(self, url: str) -> None:
        self.url = url
        self.ctx = None
        self.desktop = None
        self.docs: dict[str, object] = {}
        self._counter = 0
        self.visible = False  # live mode: load docs in a visible window
        # Optimistic-concurrency tracking. `_rev` is bumped on every change to a
        # document (by us or by a human in the live window); `_seen` records the
        # revision as of our last operation on that doc. When `_rev > _seen` a
        # mutating call is refused so we don't clobber an out-of-band edit.
        self._rev: dict[str, int] = {}
        self._seen: dict[str, int] = {}
        self._listeners: dict[str, object] = {}

    def _hidden(self, args):
        """Whether to load a document off-screen. A headless office can only ever
        be hidden; a visible (live) office honors a per-call `show`, defaulting to
        shown."""
        if not self.visible:
            return True
        show = args.get("show")
        return False if show is None else not show

    def _show(self, doc):
        """Map the document window, retrying briefly: a frame fresh off the
        loader is sometimes not realizable the very instant loading
        returns, and one swallowed failure leaves live mode windowless
        forever. Never raises — a visibility failure must not fail the
        open — but it is logged loudly instead of being swallowed.

        Deliberately no toFront(): on Wayland it disposes the URP bridge
        (every later op on the connection then fails with
        DisposedException) while the compositor owns focus anyway;
        setVisible alone maps the window.
        """
        try:
            win = doc.getCurrentController().getFrame().getContainerWindow()
        except Exception as e:
            eprint(f"_show: no container window: {e}")
            return
        last = None
        for _ in range(_SHOW_ATTEMPTS):
            try:
                win.setVisible(True)
                return
            except Exception as e:  # window not realizable yet; retry
                last = e
                time.sleep(_SHOW_RETRY_DELAY)
        eprint(f"_show: setVisible failed after retries: {last}")

    # -- connection -------------------------------------------------------
    def connect(self, timeout: float = 30.0, delay: float = 0.5) -> None:
        retries = max(1, int(timeout / delay))
        local = uno.getComponentContext()
        resolver = local.ServiceManager.createInstanceWithContext(
            "com.sun.star.bridge.UnoUrlResolver", local
        )
        last = None
        for _ in range(retries):
            try:
                self.ctx = resolver.resolve(self.url)
                break
            except Exception as e:  # NoConnectException until soffice is ready
                last = e
                time.sleep(delay)
        if self.ctx is None:
            raise RuntimeError(f"could not connect to soffice: {last}")
        smgr = self.ctx.ServiceManager
        self.desktop = smgr.createInstanceWithContext(
            "com.sun.star.frame.Desktop", self.ctx
        )
        eprint(f"connected to {self.url}")

    # -- helpers ----------------------------------------------------------
    def _new_id(self) -> str:
        self._counter += 1
        return f"doc-{self._counter}"

    def _doc(self, doc_id: str):
        doc = self.docs.get(doc_id)
        if doc is None:
            raise KeyError(f"unknown doc_id: {doc_id}")
        return doc

    def _track(self, doc, doc_id: str) -> None:
        """Register a document for revision tracking and attach a modify
        listener so human edits in the live window bump its revision too."""
        self.docs[doc_id] = doc
        self._rev[doc_id] = 0
        self._seen[doc_id] = 0

        def bump():
            self._rev[doc_id] = self._rev.get(doc_id, 0) + 1

        try:
            listener = _RevListener(bump)
            doc.addModifyListener(listener)
            self._listeners[doc_id] = listener
        except Exception as e:  # XModifiable should be universal; stay defensive
            eprint(f"modify-listener attach failed for {doc_id}: {e}")

    def _untrack(self, doc, doc_id: str) -> None:
        listener = self._listeners.pop(doc_id, None)
        if listener is not None:
            try:
                doc.removeModifyListener(listener)
            except Exception:
                pass
        self._rev.pop(doc_id, None)
        self._seen.pop(doc_id, None)

    @staticmethod
    def _kind_of(doc) -> str:
        checks = (
            ("com.sun.star.text.TextDocument", "writer"),
            ("com.sun.star.sheet.SpreadsheetDocument", "calc"),
            ("com.sun.star.presentation.PresentationDocument", "impress"),
            ("com.sun.star.drawing.DrawingDocument", "draw"),
        )
        for service, kind in checks:
            try:
                if doc.supportsService(service):
                    return kind
            except Exception:
                pass
        return "unknown"

    def _sheet(self, doc, sheet):
        sheets = doc.getSheets()
        if sheet is None:
            return sheets.getByIndex(0)
        if isinstance(sheet, int):
            return sheets.getByIndex(sheet)
        return sheets.getByName(sheet)

    @staticmethod
    def _collect_text_fonts(xtext, out) -> None:
        """Add every CharFontName used by an XText's portions to `out`.

        Defensive by design: an audit reports what it can and never fails on
        one odd shape or paragraph.
        """
        try:
            paras = xtext.createEnumeration()
        except Exception:
            return
        while True:
            try:
                if not paras.hasMoreElements():
                    return
                para = paras.nextElement()
            except Exception:
                return
            try:
                portions = para.createEnumeration()
            except Exception:
                continue
            while True:
                try:
                    if not portions.hasMoreElements():
                        break
                    portion = portions.nextElement()
                except Exception:
                    break
                try:
                    name = portion.getPropertyValue("CharFontName")
                except Exception:
                    continue
                if name:
                    out.add(name)

    def _draw_fonts(self, doc, out) -> None:
        try:
            pages = doc.getDrawPages()
        except Exception:
            return
        for p in range(pages.getCount()):
            try:
                pg = pages.getByIndex(p)
            except Exception:
                continue
            for i in range(pg.getCount()):
                try:
                    shape = pg.getByIndex(i)
                except Exception:
                    continue
                self._collect_text_fonts(shape, out)

    def _calc_fonts(self, doc, out) -> None:
        try:
            sheets = doc.getSheets()
        except Exception:
            return
        for s in range(sheets.getCount()):
            try:
                sheet = sheets.getByIndex(s)
                cursor = sheet.createCursor()
                cursor.gotoStartOfUsedArea(False)
                cursor.gotoEndOfUsedArea(True)
                addr = cursor.getRangeAddress()
            except Exception:
                continue
            for col in range(addr.StartColumn, addr.EndColumn + 1):
                for row in range(addr.StartRow, addr.EndRow + 1):
                    try:
                        cell = sheet.getCellByPosition(col, row)
                        name = cell.getPropertyValue("CharFontName")
                    except Exception:
                        continue
                    if name:
                        out.add(name)

    @staticmethod
    def _resolve_fontconfig(names):
        """Map declared families to fontconfig-matched files.

        Returns (resolved, substituted, available). `resolved` maps every name
        to its matched file (None when unverifiable); `substituted` lists names
        whose match belongs to a different family — the silent-replacement bug.
        Where fc-match is unavailable, available is False and both stay empty.
        """
        resolved, substituted = {}, []
        if shutil.which("fc-match") is None:
            return resolved, substituted, False
        for name in sorted(names):
            try:
                proc = subprocess.run(
                    ["fc-match", "-f", "%{file}\t%{family}\n", name],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                fields = proc.stdout.split("\t")
                path = fields[0].strip() or None
                families = [f.strip().lower() for f in fields[1].split(",")]
            except Exception:
                path, families = None, []
            resolved[name] = path
            if path is not None and name.lower() not in families:
                substituted.append(name)
        return resolved, substituted, True

    # -- operations -------------------------------------------------------
    def op_ping(self, args):
        # Touch the desktop, not just the stdio pipe: ping doubles as the
        # bridge-health probe the session uses to tell a dead bridge (tear
        # the session down) from a merely closed document (leave it up).
        enum = self.desktop.getComponents().createEnumeration()
        count = 0
        while enum.hasMoreElements():
            enum.nextElement()
            count += 1
        return {"pong": True, "components": count}

    def op_create_document(self, args):
        kind = args.get("kind", "writer")
        if kind not in _FACTORY:
            raise ValueError(f"unknown kind: {kind}")
        hidden = self._hidden(args)
        doc = self.desktop.loadComponentFromURL(
            _FACTORY[kind], "_blank", 0, (_prop("Hidden", hidden),)
        )
        if not hidden:
            self._show(doc)
        doc_id = self._new_id()
        self._track(doc, doc_id)
        return {"doc_id": doc_id, "kind": kind}

    def op_open_document(self, args):
        path = args["path"]
        url = uno.systemPathToFileUrl(path)
        hidden = self._hidden(args)
        doc = self.desktop.loadComponentFromURL(
            url, "_blank", 0, (_prop("Hidden", hidden),)
        )
        if not hidden:
            self._show(doc)
        doc_id = self._new_id()
        self._track(doc, doc_id)
        return {"doc_id": doc_id, "kind": self._kind_of(doc), "path": path}

    def op_list_documents(self, args):
        out = []
        for doc_id, doc in self.docs.items():
            try:
                url = doc.getURL()
            except Exception:
                url = ""
            out.append({"doc_id": doc_id, "kind": self._kind_of(doc), "url": url})
        return {"documents": out}

    def op_document_info(self, args):
        doc_id = args["doc_id"]
        doc = self._doc(doc_id)
        return {
            "doc_id": doc_id,
            "kind": self._kind_of(doc),
            "url": doc.getURL() or "",
            "modified": bool(doc.isModified()),
            "rev": self._rev.get(doc_id, 0),
            "seen": self._seen.get(doc_id, 0),
        }

    def op_document_fonts(self, args):
        doc = self._doc(args["doc_id"])
        kind = self._kind_of(doc)
        found = set()
        if kind == "writer":
            try:
                self._collect_text_fonts(doc.getText(), found)
            except Exception:
                pass
        elif kind in ("draw", "impress"):
            self._draw_fonts(doc, found)
        elif kind == "calc":
            self._calc_fonts(doc, found)
        resolved, substituted, available = self._resolve_fontconfig(found)
        return {
            "declared": sorted(found),
            "resolved": resolved,
            "substituted": substituted,
            "fontconfig": available,
        }

    def op_close_document(self, args):
        from com.sun.star.util import CloseVetoException

        doc_id = args["doc_id"]
        doc = self._doc(doc_id)
        # Closing discards unsaved changes. Refuse if the document has been
        # modified (by us or a human) unless the caller explicitly forces it,
        # so we never silently throw away in-flight work.
        if doc.isModified() and not args.get("force"):
            return {
                "closed": None,
                "warning": (
                    "document has unsaved changes — save_document first, or pass "
                    "force=true to discard them and close."
                ),
                "modified": True,
            }
        try:
            doc.close(False)
        except CloseVetoException:
            if not args.get("force"):
                raise
            # Vetoed (e.g. a stuck confirmation dialog on the frame): force
            # bypasses the controller and disposes the model directly.
            doc.dispose()
        self._untrack(doc, doc_id)
        del self.docs[doc_id]
        return {"closed": doc_id}

    # writer
    def op_get_text(self, args):
        doc = self._doc(args["doc_id"])
        return {"text": doc.getText().getString()}

    def op_insert_text(self, args):
        doc = self._doc(args["doc_id"])
        text = doc.getText()
        cursor = text.createTextCursor()
        cursor.gotoEnd(False)
        if args.get("paragraph_break"):
            text.insertControlCharacter(cursor, PARAGRAPH_BREAK, False)
        text.insertString(cursor, args["text"], False)
        return {"ok": True}

    def op_replace_all(self, args):
        doc = self._doc(args["doc_id"])
        rd = doc.createReplaceDescriptor()
        rd.setSearchString(args["search"])
        rd.setReplaceString(args["replace"])
        rd.SearchRegularExpression = bool(args.get("regex", False))
        count = doc.replaceAll(rd)
        return {"count": int(count)}

    # calc
    def op_set_cells(self, args):
        doc = self._doc(args["doc_id"])
        sheet = self._sheet(doc, args.get("sheet"))
        written = 0
        for entry in args["cells"]:
            cell = sheet.getCellRangeByName(entry["cell"]).getCellByPosition(0, 0)
            if "formula" in entry and entry["formula"] is not None:
                cell.setFormula(entry["formula"])
            elif isinstance(entry.get("value"), (int, float)):
                cell.setValue(float(entry["value"]))
            else:
                cell.setString(str(entry.get("value", "")))
            written += 1
        return {"written": written}

    def op_read_cells(self, args):
        doc = self._doc(args["doc_id"])
        sheet = self._sheet(doc, args.get("sheet"))
        rng = sheet.getCellRangeByName(args["range"])
        addr = rng.getRangeAddress()
        n = (addr.EndColumn - addr.StartColumn + 1) * (addr.EndRow - addr.StartRow + 1)
        if n > _MAX_READ_CELLS:
            raise ValueError(f"range too large: {n} cells (max {_MAX_READ_CELLS})")
        if args.get("formula"):
            data = [
                [
                    sheet.getCellByPosition(c, r).getFormula()
                    for c in range(addr.StartColumn, addr.EndColumn + 1)
                ]
                for r in range(addr.StartRow, addr.EndRow + 1)
            ]
            return {"values": data}
        data = [list(row) for row in rng.getDataArray()]
        return {"values": data}

    def op_add_sheet(self, args):
        doc = self._doc(args["doc_id"])
        sheets = doc.getSheets()
        name = args["name"]
        index = args.get("index")
        sheets.insertNewByName(name, sheets.getCount() if index is None else int(index))
        return {"name": name, "count": sheets.getCount()}

    def _number_format(self, doc, fmt):
        from com.sun.star.lang import Locale

        formats = doc.getNumberFormats()
        loc = Locale()
        key = formats.queryKey(fmt, loc, False)
        if key == -1:
            key = formats.addNew(fmt, loc)
        return key

    def op_style_cells(self, args):
        doc = self._doc(args["doc_id"])
        sheet = self._sheet(doc, args.get("sheet"))
        rng = sheet.getCellRangeByName(args["range"])
        if args.get("bold") is not None:
            from com.sun.star.awt.FontWeight import BOLD, NORMAL

            rng.CharWeight = BOLD if args["bold"] else NORMAL
        if args.get("italic") is not None:
            from com.sun.star.awt.FontSlant import ITALIC
            from com.sun.star.awt.FontSlant import NONE as SLANT_NONE

            rng.CharPosture = ITALIC if args["italic"] else SLANT_NONE
        if args.get("color") is not None:
            rng.CharColor = _color(args["color"])
        if args.get("size"):
            rng.CharHeight = float(args["size"])
        if args.get("font"):
            rng.CharFontName = args["font"]
        if args.get("fill") is not None:
            rng.CellBackColor = _color(args["fill"])
        if args.get("align"):
            from com.sun.star.table.CellHoriJustify import CENTER, LEFT, RIGHT

            rng.HoriJustify = {"left": LEFT, "center": CENTER, "right": RIGHT}.get(
                args["align"], LEFT
            )
        if args.get("number_format"):
            rng.NumberFormat = self._number_format(doc, args["number_format"])
        return {"ok": True}

    def op_set_column_width(self, args):
        doc = self._doc(args["doc_id"])
        cols = self._sheet(doc, args.get("sheet")).getColumns()
        width = _cm(args["width_cm"])
        spec = str(args["columns"])
        parts = spec.split(":")
        try:
            if len(parts) == 1:
                start = end = _col_to_index(parts[0])
            elif len(parts) == 2:
                start, end = _col_to_index(parts[0]), _col_to_index(parts[1])
            else:
                raise ValueError(spec)
        except ValueError:
            raise ValueError(f"invalid columns: {spec!r} (use A, AB, or A:E)") from None
        if start > end:
            raise ValueError(f"invalid columns: {spec!r} (reversed span)")
        for i in range(start, end + 1):
            cols.getByIndex(i).Width = width
        return {"ok": True}

    # impress
    def op_add_slide(self, args):
        doc = self._doc(args["doc_id"])
        slides = doc.getDrawPages()
        idx = slides.getCount()
        slides.insertNewByIndex(idx)
        page = slides.getByIndex(idx)
        layout = args.get("layout")
        page.Layout = 1 if layout is None else int(layout)
        return {"index": idx, "count": slides.getCount()}

    def op_set_slide_content(self, args):
        doc = self._doc(args["doc_id"])
        index = int(args["index"])
        page = doc.getDrawPages().getByIndex(index)
        title = args.get("title")
        bullets = args.get("bullets")
        if title is not None or bullets is not None:
            page.Layout = 1  # title + content placeholders
        if title is not None:
            page.getByIndex(0).setString(title)
        if bullets is not None:
            body = page.getByIndex(1).getText()
            body.setString("")
            cursor = body.createTextCursor()
            for i, line in enumerate(bullets):
                if i:
                    body.insertControlCharacter(cursor, PARAGRAPH_BREAK, False)
                body.insertString(cursor, str(line), False)
        return {"index": index}

    def op_list_slides(self, args):
        slides = self._doc(args["doc_id"]).getDrawPages()
        out = []
        for i in range(slides.getCount()):
            page = slides.getByIndex(i)
            title = ""
            for j in range(page.getCount()):
                shape = page.getByIndex(j)
                try:
                    if shape.supportsService(
                        "com.sun.star.presentation.TitleTextShape"
                    ):
                        title = shape.getString()
                        break
                except Exception:
                    pass
            out.append({"index": i, "title": title})
        return {"slides": out, "count": slides.getCount()}

    def op_read_slide(self, args):
        index = int(args["index"])
        page = self._doc(args["doc_id"]).getDrawPages().getByIndex(index)
        title = ""
        bullets = []
        texts = []
        for j in range(page.getCount()):
            shape = page.getByIndex(j)
            try:
                s = shape.getString()
            except Exception:
                s = ""
            try:
                services = list(shape.SupportedServiceNames)
            except Exception:
                services = []
            if "com.sun.star.presentation.TitleTextShape" in services:
                title = s
            elif (
                s
                and not bullets
                and any(
                    n.startswith("com.sun.star.presentation.")
                    and n.endswith("TextShape")
                    for n in services
                )
            ):
                bullets = s.split("\n")
            if s:
                texts.append(s)
        return {"index": index, "title": title, "bullets": bullets, "text": texts}

    def op_delete_slide(self, args):
        slides = self._doc(args["doc_id"]).getDrawPages()
        page = slides.getByIndex(int(args["index"]))
        slides.remove(page)
        return {"count": slides.getCount()}

    # impress graphics --------------------------------------------------
    # Positions/sizes are percentages (0-100) of the slide; colors are hex.
    def _page(self, doc, index):
        return doc.getDrawPages().getByIndex(int(index))

    def _place(self, shape, page, x, y, w, h):
        from com.sun.star.awt import Point, Size

        shape.Position = Point(int(page.Width * x / 100), int(page.Height * y / 100))
        shape.Size = Size(int(page.Width * w / 100), int(page.Height * h / 100))

    def _apply_fill(self, shape, fill, fill2=None, angle=0):
        from com.sun.star.drawing.FillStyle import GRADIENT, NONE, SOLID

        if fill is None:
            shape.FillStyle = NONE
            return
        if fill2 is not None:
            from com.sun.star.awt import Gradient
            from com.sun.star.awt.GradientStyle import LINEAR

            g = Gradient()
            g.Style = LINEAR
            g.StartColor = _color(fill)
            g.EndColor = _color(fill2)
            g.Angle = int((angle or 0) * 10)
            g.StartIntensity = 100
            g.EndIntensity = 100
            g.Border = 0
            g.XOffset = 50
            g.YOffset = 50
            g.StepCount = 0
            shape.FillStyle = GRADIENT
            shape.FillGradient = g
        else:
            shape.FillStyle = SOLID
            shape.FillColor = _color(fill)

    def _style_text(self, shape, text, color, size, bold, italic, align, valign, font):
        from com.sun.star.awt.FontSlant import ITALIC
        from com.sun.star.awt.FontSlant import NONE as SLANT_NONE
        from com.sun.star.awt.FontWeight import BOLD, NORMAL
        from com.sun.star.drawing.TextVerticalAdjust import BOTTOM
        from com.sun.star.drawing.TextVerticalAdjust import CENTER as VCENTER
        from com.sun.star.drawing.TextVerticalAdjust import TOP
        from com.sun.star.style.ParagraphAdjust import CENTER, LEFT, RIGHT

        adjust = {"left": LEFT, "center": CENTER, "right": RIGHT}.get(align, LEFT)
        vadj = {"top": TOP, "center": VCENTER, "bottom": BOTTOM}.get(valign, TOP)
        try:
            shape.TextVerticalAdjust = vadj
        except Exception:
            pass
        t = shape.getText()
        t.setString(str(text))
        cur = t.createTextCursor()
        cur.gotoStart(False)
        cur.gotoEnd(True)
        if size:
            cur.CharHeight = float(size)
        if color is not None:
            cur.CharColor = _color(color)
        cur.CharWeight = BOLD if bold else NORMAL
        cur.CharPosture = ITALIC if italic else SLANT_NONE
        if font:
            cur.CharFontName = font
        cur.ParaAdjust = adjust

    def op_set_presentation_size(self, args):
        doc = self._doc(args["doc_id"])
        w, h = _PRESET_SIZES.get(args.get("preset", "16:9"), _PRESET_SIZES["16:9"])
        slides = doc.getDrawPages()
        for i in range(slides.getCount()):
            page = slides.getByIndex(i)
            page.Width = w
            page.Height = h
        return {"width": w, "height": h}

    def op_set_slide_background(self, args):
        doc = self._doc(args["doc_id"])
        page = self._page(doc, args["slide"])
        from com.sun.star.awt import Point, Size
        from com.sun.star.drawing.LineStyle import NONE as LINE_NONE

        rect = doc.createInstance("com.sun.star.drawing.RectangleShape")
        page.add(rect)
        rect.Position = Point(0, 0)
        rect.Size = Size(page.Width, page.Height)
        rect.LineStyle = LINE_NONE
        self._apply_fill(
            rect, args.get("color"), args.get("color2"), args.get("angle", 0)
        )
        return {"ok": True}

    def op_add_textbox(self, args):
        doc = self._doc(args["doc_id"])
        page = self._page(doc, args["slide"])
        from com.sun.star.drawing.FillStyle import NONE as FILL_NONE
        from com.sun.star.drawing.LineStyle import NONE as LINE_NONE

        shape = doc.createInstance("com.sun.star.drawing.TextShape")
        page.add(shape)
        self._place(shape, page, args["x"], args["y"], args["w"], args["h"])
        shape.FillStyle = FILL_NONE
        shape.LineStyle = LINE_NONE
        try:
            shape.TextAutoGrowHeight = False
            shape.TextAutoGrowWidth = False
            shape.TextWordWrap = True
        except Exception:
            pass
        self._style_text(
            shape,
            args["text"],
            args.get("color"),
            args.get("size", 18),
            args.get("bold", False),
            args.get("italic", False),
            args.get("align", "left"),
            args.get("valign", "top"),
            args.get("font"),
        )
        return {"ok": True}

    def op_add_shape(self, args):
        doc = self._doc(args["doc_id"])
        page = self._page(doc, args["slide"])
        from com.sun.star.drawing.FillStyle import NONE as FILL_NONE
        from com.sun.star.drawing.LineStyle import NONE as LINE_NONE
        from com.sun.star.drawing.LineStyle import SOLID as LINE_SOLID

        kind = args.get("shape", "rect")
        shape = doc.createInstance(_SHAPES.get(kind, _SHAPES["rect"]))
        page.add(shape)
        self._place(shape, page, args["x"], args["y"], args["w"], args["h"])
        if kind == "round":
            try:
                shape.CornerRadius = int(args.get("corner", 300))
            except Exception:
                pass
        if kind == "line":
            shape.FillStyle = FILL_NONE
        else:
            self._apply_fill(
                shape,
                args.get("fill"),
                args.get("fill2"),
                args.get("angle", 0),
            )
        line = args.get("line")
        if line is not None or kind == "line":
            shape.LineStyle = LINE_SOLID
            shape.LineColor = _color(
                line if line is not None else (args.get("fill") or _LINE_FALLBACK)
            )
            shape.LineWidth = int(args.get("line_width", 40))
        else:
            shape.LineStyle = LINE_NONE
        if args.get("text"):
            self._style_text(
                shape,
                args["text"],
                args.get("text_color"),
                args.get("text_size", 16),
                args.get("text_bold", False),
                False,
                args.get("text_align", "center"),
                "center",
                args.get("font"),
            )
        return {"ok": True}

    def op_add_image(self, args):
        doc = self._doc(args["doc_id"])
        page = self._page(doc, args["slide"])
        shape = doc.createInstance("com.sun.star.drawing.GraphicObjectShape")
        page.add(shape)
        self._place(shape, page, args["x"], args["y"], args["w"], args["h"])
        provider = self.ctx.ServiceManager.createInstanceWithContext(
            "com.sun.star.graphic.GraphicProvider", self.ctx
        )
        prop = PropertyValue()
        prop.Name = "URL"
        prop.Value = uno.systemPathToFileUrl(args["path"])
        shape.Graphic = provider.queryGraphic((prop,))
        return {"ok": True}

    # draw ----------------------------------------------------------------
    # Text-first editing for Draw pages (CVs, diagrams, posters). Positions and
    # sizes are centimetres; colors are hex; font sizes are points. Shapes are
    # addressed by 0-based index in z-order; deleting shifts later indices, so
    # remove several in descending index order.
    def _draw_page(self, doc, page):
        pages = doc.getDrawPages()
        page = int(page)
        if page < 0 or page >= pages.getCount():
            raise IndexError(f"no page {page} (has {pages.getCount()})")
        return pages.getByIndex(page)

    def _draw_shape(self, doc, page, index):
        pg = self._draw_page(doc, page)
        index = int(index)
        if index < 0 or index >= pg.getCount():
            raise IndexError(f"no shape {index} on page {page} (has {pg.getCount()})")
        return pg.getByIndex(index)

    @staticmethod
    def _shape_type(shape):
        try:
            names = list(shape.SupportedServiceNames)
        except Exception:
            return "shape"
        for name in names:
            short = _DRAW_TYPES.get(name.rsplit(".", 1)[-1])
            if short is not None:
                return short
        return "shape"

    def op_draw_list_shapes(self, args):
        page = int(args["page"])
        pg = self._draw_page(self._doc(args["doc_id"]), page)
        out = []
        for i in range(pg.getCount()):
            shape = pg.getByIndex(i)
            try:
                text = shape.getString()
            except Exception:
                text = ""
            pos, size = shape.Position, shape.Size
            out.append(
                {
                    "index": i,
                    "type": self._shape_type(shape),
                    "x_cm": round(pos.X / 1000, 3),
                    "y_cm": round(pos.Y / 1000, 3),
                    "w_cm": round(size.Width / 1000, 3),
                    "h_cm": round(size.Height / 1000, 3),
                    "text": text,
                }
            )
        return {"page": page, "count": pg.getCount(), "shapes": out}

    def op_draw_get_text(self, args):
        doc = self._doc(args["doc_id"])
        index = int(args["shape"])
        shape = self._draw_shape(doc, args["page"], index)
        try:
            text = shape.getString()
        except Exception:
            text = ""
        return {"shape": index, "text": text}

    def op_draw_set_text(self, args):
        doc = self._doc(args["doc_id"])
        index = int(args["shape"])
        shape = self._draw_shape(doc, args["page"], index)
        try:
            t = shape.getText()
        except Exception:
            raise ValueError(
                f"shape {index} is {self._shape_type(shape)}, has no editable text"
            )
        # Snapshot the current formatting so style options left out are kept
        # rather than reset when the string (and its runs) is replaced.
        snap = {}
        try:
            probe = t.createTextCursor()
            probe.gotoStart(False)
            probe.gotoEnd(True)
            for key in (
                "CharHeight",
                "CharColor",
                "CharWeight",
                "CharPosture",
                "CharFontName",
                "ParaAdjust",
            ):
                try:
                    snap[key] = probe.getPropertyValue(key)
                except Exception:
                    pass
        except Exception:
            pass
        t.setString(str(args["text"]))
        cur = t.createTextCursor()
        cur.gotoStart(False)
        cur.gotoEnd(True)
        style = dict(snap)
        if args.get("size"):
            style["CharHeight"] = float(args["size"])
        if args.get("color") is not None:
            style["CharColor"] = _color(args["color"])
        if args.get("bold") is not None:
            from com.sun.star.awt.FontWeight import BOLD, NORMAL

            style["CharWeight"] = BOLD if args["bold"] else NORMAL
        if args.get("italic") is not None:
            from com.sun.star.awt.FontSlant import ITALIC
            from com.sun.star.awt.FontSlant import NONE as SLANT_NONE

            style["CharPosture"] = ITALIC if args["italic"] else SLANT_NONE
        if args.get("font"):
            style["CharFontName"] = args["font"]
        if args.get("align"):
            from com.sun.star.style.ParagraphAdjust import (
                BLOCK,
                CENTER,
                LEFT,
                RIGHT,
            )

            style["ParaAdjust"] = {
                "left": LEFT,
                "center": CENTER,
                "right": RIGHT,
                "justify": BLOCK,
            }.get(args["align"], LEFT)
        for key, value in style.items():
            try:
                cur.setPropertyValue(key, value)
            except Exception:
                pass
        if args.get("valign"):
            from com.sun.star.drawing.TextVerticalAdjust import BOTTOM
            from com.sun.star.drawing.TextVerticalAdjust import CENTER as VCENTER
            from com.sun.star.drawing.TextVerticalAdjust import TOP

            try:
                shape.TextVerticalAdjust = {
                    "top": TOP,
                    "center": VCENTER,
                    "bottom": BOTTOM,
                }.get(args["valign"], TOP)
            except Exception:
                pass
        return {"shape": index}

    def op_draw_delete_shape(self, args):
        doc = self._doc(args["doc_id"])
        page = int(args["page"])
        shape = self._draw_shape(doc, page, args["shape"])
        pg = doc.getDrawPages().getByIndex(page)
        pg.remove(shape)
        return {"count": pg.getCount()}

    def op_draw_move_shape(self, args):
        from com.sun.star.awt import Point

        doc = self._doc(args["doc_id"])
        index = int(args["shape"])
        shape = self._draw_shape(doc, args["page"], index)
        pos = shape.Position
        shape.Position = Point(
            pos.X + _cm(args.get("dx_cm", 0)), pos.Y + _cm(args.get("dy_cm", 0))
        )
        pos = shape.Position
        return {
            "shape": index,
            "x_cm": round(pos.X / 1000, 3),
            "y_cm": round(pos.Y / 1000, 3),
        }

    def op_draw_add_textbox(self, args):
        from com.sun.star.awt import Point, Size
        from com.sun.star.drawing.FillStyle import NONE as FILL_NONE
        from com.sun.star.drawing.LineStyle import NONE as LINE_NONE

        doc = self._doc(args["doc_id"])
        pg = self._draw_page(doc, args["page"])
        shape = doc.createInstance("com.sun.star.drawing.TextShape")
        pg.add(shape)
        shape.Position = Point(_cm(args["x_cm"]), _cm(args["y_cm"]))
        shape.Size = Size(_cm(args["w_cm"]), _cm(args["h_cm"]))
        shape.FillStyle = FILL_NONE
        shape.LineStyle = LINE_NONE
        try:
            shape.TextAutoGrowHeight = False
            shape.TextAutoGrowWidth = False
            shape.TextWordWrap = True
        except Exception:
            pass
        self._style_text(
            shape,
            args["text"],
            args.get("color"),
            args.get("size", 11),
            args.get("bold", False),
            args.get("italic", False),
            args.get("align", "left"),
            args.get("valign", "top"),
            args.get("font"),
        )
        return {"index": pg.getCount() - 1}

    # writer: rich documents -------------------------------------------
    def _page_style(self, doc):
        return doc.getStyleFamilies().getByName("PageStyles").getByName("Standard")

    def _graphic(self, path):
        if not path or not os.path.isfile(path):
            raise ValueError(f"image not found: {path}")
        provider = self.ctx.ServiceManager.createInstanceWithContext(
            "com.sun.star.graphic.GraphicProvider", self.ctx
        )
        prop = PropertyValue()
        prop.Name = "URL"
        prop.Value = uno.systemPathToFileUrl(path)
        return provider.queryGraphic((prop,))

    def _char(self, cur, args):
        from com.sun.star.awt.FontSlant import ITALIC
        from com.sun.star.awt.FontSlant import NONE as SLANT_NONE
        from com.sun.star.awt.FontWeight import BOLD, NORMAL

        if args.get("size"):
            cur.CharHeight = float(args["size"])
        if args.get("color") is not None:
            cur.CharColor = _color(args["color"])
        cur.CharWeight = BOLD if args.get("bold") else NORMAL
        cur.CharPosture = ITALIC if args.get("italic") else SLANT_NONE
        if args.get("font"):
            cur.CharFontName = args["font"]

    def op_draw_audit_overlaps(self, args):
        page = int(args["page"])
        pg = self._draw_page(self._doc(args["doc_id"]), page)
        boxes = []
        for i in range(pg.getCount()):
            shape = pg.getByIndex(i)
            try:
                text = shape.getString()
            except Exception:
                text = ""
            if not text or not text.strip():
                continue
            pos, size = shape.Position, shape.Size
            boxes.append(
                (
                    i,
                    " ".join(text.split())[:60],
                    pos.X,
                    pos.Y,
                    pos.X + size.Width,
                    pos.Y + size.Height,
                )
            )
        overlaps = []
        for a in range(len(boxes)):
            for b in range(a + 1, len(boxes)):
                ia, ta, ax0, ay0, ax1, ay1 = boxes[a]
                ib, tb, bx0, by0, bx1, by1 = boxes[b]
                x0, y0 = max(ax0, bx0), max(ay0, by0)
                x1, y1 = min(ax1, bx1), min(ay1, by1)
                if x1 > x0 and y1 > y0:
                    overlaps.append(
                        {
                            "a": {"index": ia, "text": ta},
                            "b": {"index": ib, "text": tb},
                            "x_cm": round(x0 / 1000, 3),
                            "y_cm": round(y0 / 1000, 3),
                            "w_cm": round((x1 - x0) / 1000, 3),
                            "h_cm": round((y1 - y0) / 1000, 3),
                        }
                    )
        return {
            "page": page,
            "count": pg.getCount(),
            "checked": len(boxes),
            "overlaps": overlaps,
        }

    def op_page_setup(self, args):
        ps = self._page_style(self._doc(args["doc_id"]))
        margin = args.get("margin")
        if margin is not None:
            ps.LeftMargin = ps.RightMargin = ps.BottomMargin = _cm(margin)
            ps.TopMargin = _cm(args.get("top", margin))
        elif args.get("top") is not None:
            ps.TopMargin = _cm(args["top"])
        if args.get("color") is not None:
            ps.BackColor = _color(args["color"])
            ps.BackTransparent = False
        return {"ok": True}

    def op_add_paragraph(self, args):
        from com.sun.star.style.ParagraphAdjust import BLOCK, CENTER, LEFT, RIGHT

        doc = self._doc(args["doc_id"])
        text = doc.getText()
        cur = text.createTextCursor()
        cur.gotoEnd(False)
        style = args.get("style")
        if style:
            cur.ParaStyleName = style
        cur.ParaAdjust = {
            "left": LEFT,
            "center": CENTER,
            "right": RIGHT,
            "justify": BLOCK,
        }.get(args.get("align", "left"), LEFT)
        cur.ParaLeftMargin = 0
        cur.ParaFirstLineIndent = 0
        cur.ParaTopMargin = _cm(args.get("space_before", 0))
        cur.ParaBottomMargin = _cm(args.get("space_after", 0.2))
        self._char(cur, args)
        text.insertString(cur, args["text"], False)
        text.insertControlCharacter(cur, PARAGRAPH_BREAK, False)
        return {"ok": True}

    def op_add_list(self, args):
        doc = self._doc(args["doc_id"])
        text = doc.getText()
        cur = text.createTextCursor()
        ordered = args.get("ordered", False)
        for i, item in enumerate(args["items"]):
            cur.gotoEnd(False)
            cur.ParaLeftMargin = _cm(0.7)
            cur.ParaFirstLineIndent = -_cm(0.7)
            cur.ParaBottomMargin = _cm(args.get("space_after", 0.12))
            self._char(cur, args)
            bullet = f"{i + 1}.   " if ordered else "•   "
            text.insertString(cur, bullet + str(item), False)
            text.insertControlCharacter(cur, PARAGRAPH_BREAK, False)
        cur.gotoEnd(False)
        cur.ParaLeftMargin = 0
        cur.ParaFirstLineIndent = 0
        return {"ok": True}

    def op_insert_table(self, args):
        doc = self._doc(args["doc_id"])
        text = doc.getText()
        rows = args["rows"]
        if not rows:
            raise ValueError("rows must not be empty")
        nrows, ncols = len(rows), max(len(r) for r in rows)
        table = doc.createInstance("com.sun.star.text.TextTable")
        table.initialize(nrows, ncols)
        cur = text.createTextCursor()
        cur.gotoEnd(False)
        text.insertTextContent(cur, table, False)
        header = args.get("header", True)
        accent = _color(args.get("accent"))
        header_text = _color(args.get("header_color") or _TABLE_HEADER_TEXT)
        body_text = _color(args.get("color"))
        from com.sun.star.awt.FontWeight import BOLD, NORMAL

        for r, row in enumerate(rows):
            for c in range(ncols):
                # NOTE: getCellByName only understands single-letter columns
                # ("AA1" returns None); positions work for any width.
                cell = table.getCellByPosition(c, r)
                val = row[c] if c < len(row) else ""
                ct = cell.getText()
                ccur = ct.createTextCursor()
                ccur.CharHeight = float(args.get("size", 11))
                if header and r == 0:
                    if accent is not None:
                        cell.BackColor = accent
                    ccur.CharColor = header_text
                    ccur.CharWeight = BOLD
                else:
                    if body_text is not None:
                        ccur.CharColor = body_text
                    ccur.CharWeight = NORMAL
                ct.insertString(ccur, str(val), False)
        return {"ok": True}

    def op_insert_image(self, args):
        doc = self._doc(args["doc_id"])
        text = doc.getText()
        from com.sun.star.text.TextContentAnchorType import AS_CHARACTER

        img = doc.createInstance("com.sun.star.text.TextGraphicObject")
        graphic = self._graphic(args["path"])
        img.Graphic = graphic
        img.AnchorType = AS_CHARACTER
        native = graphic.Size100thMM
        width = _cm(args.get("width_cm", 8))
        ratio = (native.Height / native.Width) if native.Width else 0.6
        img.Width = width
        img.Height = int(width * ratio)
        cur = text.createTextCursor()
        cur.gotoEnd(False)
        text.insertTextContent(cur, img, False)
        return {"ok": True}

    def op_add_page_box(self, args):
        from com.sun.star.table import BorderLine2
        from com.sun.star.text.HoriOrientation import NONE as H_NONE
        from com.sun.star.text.RelOrientation import PAGE_FRAME
        from com.sun.star.text.TextContentAnchorType import AT_PAGE
        from com.sun.star.text.VertOrientation import NONE as V_NONE

        doc = self._doc(args["doc_id"])
        text = doc.getText()
        ps = self._page_style(doc)
        frame = doc.createInstance("com.sun.star.text.TextFrame")
        frame.Width = int(ps.Width * args["w"] / 100)
        frame.Height = int(ps.Height * args["h"] / 100)
        frame.AnchorType = AT_PAGE
        frame.HoriOrient = H_NONE
        frame.HoriOrientRelation = PAGE_FRAME
        frame.HoriOrientPosition = int(ps.Width * args["x"] / 100)
        frame.VertOrient = V_NONE
        frame.VertOrientRelation = PAGE_FRAME
        frame.VertOrientPosition = int(ps.Height * args["y"] / 100)
        no_border = BorderLine2()
        no_border.LineWidth = 0
        for side in ("TopBorder", "BottomBorder", "LeftBorder", "RightBorder"):
            setattr(frame, side, no_border)
        for dist in (
            "LeftBorderDistance",
            "RightBorderDistance",
            "TopBorderDistance",
            "BottomBorderDistance",
        ):
            try:
                setattr(frame, dist, _cm(args.get("pad", 0.3)))
            except Exception:
                pass
        if args.get("fill") is not None:
            frame.BackColor = _color(args["fill"])
            frame.BackTransparent = False
        else:
            frame.BackTransparent = True
        cur = text.createTextCursor()
        cur.gotoEnd(False)
        text.insertTextContent(cur, frame, False)
        if args.get("text"):
            from com.sun.star.style.ParagraphAdjust import CENTER, LEFT, RIGHT

            ft = frame.getText()
            fcur = ft.createTextCursor()
            fcur.ParaAdjust = {"left": LEFT, "center": CENTER, "right": RIGHT}.get(
                args.get("align", "left"), LEFT
            )
            self._char(fcur, args)
            ft.insertString(fcur, args["text"], False)
        return {"ok": True}

    # persistence
    def op_save_document(self, args):
        doc = self._doc(args["doc_id"])
        path = args.get("path")
        if path is None:
            if not doc.getURL():
                raise ValueError("document has no location; provide path")
            doc.store()
            return {"path": uno.fileUrlToSystemPath(doc.getURL())}
        fmt = args.get("format") or path.rsplit(".", 1)[-1].lower()
        kind = self._kind_of(doc)
        filt = _FILTERS.get(kind, {}).get(fmt)
        if not filt:
            raise ValueError(f"no filter for kind={kind} format={fmt}")
        url = uno.systemPathToFileUrl(path)
        doc.storeToURL(url, (_prop("FilterName", filt),))
        return {"path": path, "filter": filt}

    def op_export_document(self, args):
        # same machinery as save with an explicit format/path
        return self.op_save_document(args)


# Resolved lazily after `import uno` so the constant is real at call time.
PARAGRAPH_BREAK = None


def _load_constants():
    global PARAGRAPH_BREAK
    from com.sun.star.text.ControlCharacter import PARAGRAPH_BREAK as PB

    PARAGRAPH_BREAK = PB


_OPS = {name[3:]: name for name in dir(Worker) if name.startswith("op_")}

# Ops that never mutate the document model: reads, plus lifecycle ops that handle
# their own safety (close guards on unsaved changes; save/export only read the
# model). Every *other* op that carries a `doc_id` is treated as a mutating edit
# and is gated by the optimistic-concurrency guard in serve(), so new edit tools
# are protected automatically, for any document kind.
_SAFE_OPS = {
    "ping",
    "create_document",
    "open_document",
    "list_documents",
    "document_info",
    "document_fonts",
    "get_text",
    "read_cells",
    "list_slides",
    "read_slide",
    "draw_list_shapes",
    "draw_get_text",
    "draw_audit_overlaps",
    "close_document",
    "save_document",
    "export_document",
}


def serve(worker: Worker) -> None:
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except Exception as e:
            sys.stdout.write(
                json.dumps(
                    {
                        "id": 0,
                        "ok": False,
                        "error": {"type": "bad_json", "message": str(e)},
                    }
                )
                + "\n"
            )
            sys.stdout.flush()
            continue

        req_id = req.get("id", 0)
        op = req.get("op", "")
        args = req.get("args", {}) or {}

        if op == "shutdown":
            sys.stdout.write(
                json.dumps({"id": req_id, "ok": True, "result": {}}) + "\n"
            )
            sys.stdout.flush()
            return

        method = _OPS.get(op)
        if method is None:
            sys.stdout.write(
                json.dumps(
                    {
                        "id": req_id,
                        "ok": False,
                        "error": {"type": "unknown_op", "message": op},
                    }
                )
                + "\n"
            )
            sys.stdout.flush()
            continue

        # Optimistic-concurrency guard: refuse to mutate a document that changed
        # since we last touched it (a human edited the live window), unless forced.
        doc_id = args.get("doc_id")
        if (
            doc_id is not None
            and op not in _SAFE_OPS
            and not args.get("force")
            and worker._rev.get(doc_id, 0) > worker._seen.get(doc_id, 0)
        ):
            sys.stdout.write(
                json.dumps(
                    {
                        "id": req_id,
                        "ok": True,
                        "result": {
                            "conflict": True,
                            "warning": (
                                "document changed since your last edit (an "
                                "external/human edit). Re-read it (e.g. get_text) "
                                "to pick up the change, then retry."
                            ),
                            "rev": worker._rev.get(doc_id, 0),
                            "seen": worker._seen.get(doc_id, 0),
                        },
                    }
                )
                + "\n"
            )
            sys.stdout.flush()
            continue

        try:
            result = getattr(worker, method)(args)
            # Resync: after any successful op on a tracked doc, mark its current
            # revision as seen (our own edits' modify events fire synchronously
            # within the call above, so they're already counted).
            if doc_id is not None and doc_id in worker._rev:
                worker._seen[doc_id] = worker._rev.get(doc_id, 0)
            resp = {"id": req_id, "ok": True, "result": result}
        except Exception as e:
            fatal = "DisposedException" in type(
                e
            ).__name__ or "DisposedException" in str(e)
            resp = {
                "id": req_id,
                "ok": False,
                "error": {"type": type(e).__name__, "message": str(e), "fatal": fatal},
            }
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--connect-timeout", type=float, default=30.0)
    parser.add_argument("--visible", action="store_true")
    ns = parser.parse_args()

    worker = Worker(ns.url)
    worker.visible = ns.visible
    worker.connect(timeout=ns.connect_timeout)
    _load_constants()
    serve(worker)
    return 0


if __name__ == "__main__":
    sys.exit(main())
