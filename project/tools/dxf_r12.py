"""A minimal DXF R12 (AC1009) writer: layers, 3D polylines, points, lines, text.

R12 is the oldest DXF every CAD program still reads (Rhino, AutoCAD, QGIS,
Illustrator, LibreCAD ...), and it is plain text, so no library is needed.
Coordinates are written as given (the package's site coordinates);
$INSUNITS says metres or unitless.
"""
from __future__ import annotations

from pathlib import Path


class Dxf:
    def __init__(self, metres: bool):
        self.metres = metres
        self.layers: dict[str, int] = {}
        self.ents: list[str] = []

    def layer(self, name: str, colour: int = 7) -> str:
        """Declare a layer (AutoCAD colour index: 1 red, 2 yellow, 3 green,
        4 cyan, 5 blue, 6 magenta, 7 black/white, 8 dark grey, 30 orange)."""
        self.layers.setdefault(name, colour)
        return name

    def polyline(self, layer: str, pts, closed: bool = False) -> None:
        """3D polyline through pts (N x 3, or N x 2 at z = 0)."""
        self.ents += ["0", "POLYLINE", "8", layer, "66", "1", "10", "0", "20", "0", "30", "0",
                      "70", "9" if closed else "8"]
        for p in pts:
            x, y = float(p[0]), float(p[1])
            z = float(p[2]) if len(p) > 2 else 0.0
            self.ents += ["0", "VERTEX", "8", layer, "10", f"{x:.6f}", "20", f"{y:.6f}",
                          "30", f"{z:.6f}", "70", "32"]
        self.ents += ["0", "SEQEND", "8", layer]

    def point(self, layer: str, p) -> None:
        self.ents += ["0", "POINT", "8", layer, "10", f"{float(p[0]):.6f}",
                      "20", f"{float(p[1]):.6f}", "30", f"{float(p[2]) if len(p) > 2 else 0.0:.6f}"]

    def line(self, layer: str, a, b) -> None:
        self.ents += ["0", "LINE", "8", layer,
                      "10", f"{float(a[0]):.6f}", "20", f"{float(a[1]):.6f}", "30", f"{float(a[2]):.6f}",
                      "11", f"{float(b[0]):.6f}", "21", f"{float(b[1]):.6f}", "31", f"{float(b[2]):.6f}"]

    def text(self, layer: str, p, s: str, height: float) -> None:
        s = s.encode("ascii", "replace").decode("ascii")
        self.ents += ["0", "TEXT", "8", layer, "10", f"{float(p[0]):.6f}", "20", f"{float(p[1]):.6f}",
                      "30", f"{float(p[2]) if len(p) > 2 else 0.0:.6f}", "40", f"{height:.6f}", "1", s]

    def write(self, path: Path) -> None:
        out = ["0", "SECTION", "2", "HEADER", "9", "$ACADVER", "1", "AC1009",
               "9", "$INSUNITS", "70", "6" if self.metres else "0", "0", "ENDSEC",
               "0", "SECTION", "2", "TABLES", "0", "TABLE", "2", "LAYER", "70", str(len(self.layers))]
        for name, col in self.layers.items():
            out += ["0", "LAYER", "2", name, "70", "0", "62", str(col), "6", "CONTINUOUS"]
        out += ["0", "ENDTAB", "0", "ENDSEC", "0", "SECTION", "2", "ENTITIES"]
        out += self.ents
        out += ["0", "ENDSEC", "0", "EOF"]
        Path(path).write_text("\n".join(out) + "\n", encoding="ascii")
