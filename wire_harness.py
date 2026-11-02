# -*- coding: utf-8 -*-
"""Wire harness for FreeCAD: smooth spline wires, driven by spreadsheets.

HarnessTable (the wires table; headers in row 4, data from row 5, blank rows are skipped):
      Wire | Start X | Start Y | Start Z | Outer diameter | Min. bend radius | Total length | Color | Points sheet | Leaves toward
Every wire has its own points sheet (named in the last column, default Points_<wire>):
      row 4 headers, data from row 5, one row per point; dX, dY, dZ (mm) are offsets from the PREVIOUS point
      (the first one from the wire's start point):
      Point # | Kind (via / end) | dX | dY | dZ | Dir X | Dir Y | Dir Z

A wire leaves its start point in the direction of the last column (+X -X +Y -Y +Z -Z, or three numbers; blank = straight
up, +Z) and passes through its points, in row order, as a smooth chain of cubic curves.
  - No "end" row (free end): after the last point the wire continues straight for whatever
    length is left, so the total length is exact.
  - An "end" row (last row) fixes the end position. The total length is made exact by
    tightening the curve (shorter) or letting the slack hang as a downward sag (longer).
  - Dir X/Y/Z (optional, any row) fixes the heading of the wire at that point. (The Harness Designer window also
    fills these in for the points next to one you move or turn, so that only the stretches touching that point change.)
  - A free-end wire never needs a longer Length because it was bent: the Harness Designer window pulls the last point
    in toward the one before it, just far enough to keep the wire exactly as long as its Length.
The minimum bend radius is only used for warnings in WireReport.
"""
import math
import re

import FreeCAD
import Part
from FreeCAD import Vector, Rotation

HARNESS_FIRST_ROW = 5    # first data row of the wires table
POINTS_FIRST_ROW = 5     # first data row of a points sheet
SCAN_ROWS = 300          # rows scanned in every table; blank rows are skipped

COLORS = {
    "red": (0.85, 0.10, 0.10), "blue": (0.10, 0.35, 0.85), "green": (0.10, 0.65, 0.25),
    "yellow": (0.95, 0.80, 0.10), "orange": (0.95, 0.50, 0.10), "white": (0.95, 0.95, 0.95),
    "black": (0.10, 0.10, 0.10), "brown": (0.45, 0.28, 0.12), "purple": (0.55, 0.25, 0.75),
    "grey": (0.55, 0.55, 0.55), "gray": (0.55, 0.55, 0.55), "pink": (0.95, 0.55, 0.70),
    "cyan": (0.10, 0.75, 0.85),
}
PALETTE = ["red", "blue", "green", "yellow", "orange", "purple", "cyan", "brown"]


def parse_color(text, index):
    """Color name or #RRGGBB; blank/unknown falls back to a palette color by wire order."""
    t = (text or "").strip().lstrip("'").strip().lower()
    if t in COLORS:
        return COLORS[t]
    if t.startswith("#") and len(t) == 7:
        try:
            return tuple(int(t[i:i + 2], 16) / 255.0 for i in (1, 3, 5))
        except ValueError:
            pass
    return COLORS[PALETTE[index % len(PALETTE)]]


def _num(v):
    return float(v.Value) if hasattr(v, "Value") else float(v)


def _contents(sheet, addr):
    try:
        return sheet.getContents(addr).strip()
    except Exception:
        return ""


def _ident(sheet, addr):
    """Wire ID as text; 1, 1.0 and "1" all give "1"."""
    if _contents(sheet, addr) == "":
        return ""
    try:
        v = sheet.get(addr)
    except Exception:
        return ""
    if hasattr(v, "Value"):
        v = v.Value
    if isinstance(v, float) and v == int(v):
        return str(int(v))
    return str(v).strip()


def _opt(sheet, addr, default):
    return _num(sheet.get(addr)) if _contents(sheet, addr) else default


def _fmt(v):
    s = ("%.4f" % v).rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _fmt_digits(v, digits):
    s = ("%.*f" % (digits, v)).rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _stored(v, digits=4):
    """A number as it reads back after it was written to a cell with `digits` decimals."""
    return float(_fmt_digits(v, digits))


# ---------------------------------------------------------------------------
# Reading the sheets
# ---------------------------------------------------------------------------

def points_sheet_name(wid):
    return "Points_" + re.sub(r"[^0-9A-Za-z_]", "_", str(wid))


def find_sheet(doc, name):
    """A spreadsheet of the document by name or label, or None."""
    obj = doc.getObject(name)
    if obj is None:
        found = doc.getObjectsByLabel(name)
        obj = found[0] if found else None
    return obj if obj is not None and obj.TypeId == "Spreadsheet::Sheet" else None


def read_points_sheet(psheet, start=(0.0, 0.0, 0.0)):
    """Return (points, problems) from one wire's points sheet; point = {kind, p, offset, dir, row}.

    The dX, dY, dZ cells are offsets from the previous point (the first point from `start`);
    p is the resulting position in model coordinates."""
    points, problems = [], []
    pos = Vector(*start)
    for row in range(POINTS_FIRST_ROW, POINTS_FIRST_ROW + SCAN_ROWS):
        filled = [_contents(psheet, "%s%d" % (c, row)) != "" for c in "CDE"]
        if not any(filled):
            continue
        if not all(filled):
            problems.append("row %d: dX, dY and dZ must all be filled in" % row)
            continue
        off = Vector(_opt(psheet, "C%d" % row, 0.0), _opt(psheet, "D%d" % row, 0.0), _opt(psheet, "E%d" % row, 0.0))
        if off.Length < 1e-6:
            problems.append("row %d: the offset is (0, 0, 0), so this point would be in the same place as the previous one" % row)
            continue
        pos = pos + off
        kind = _contents(psheet, "B%d" % row).lstrip("'").strip().lower()
        kind = "end" if kind.startswith("end") else "via"
        d = None
        dir_filled = [_contents(psheet, "%s%d" % (c, row)) != "" for c in "FGH"]
        if any(dir_filled) and not all(dir_filled):
            problems.append("row %d: Dir X, Dir Y and Dir Z must all be filled in, or all left blank" % row)
        elif all(dir_filled):
            d = (_opt(psheet, "F%d" % row, 0.0), _opt(psheet, "G%d" % row, 0.0), _opt(psheet, "H%d" % row, 0.0))
            if d[0] ** 2 + d[1] ** 2 + d[2] ** 2 < 1e-12:
                problems.append("row %d: the direction cannot be (0, 0, 0)" % row)
                d = None
        points.append({"kind": kind, "p": (pos.x, pos.y, pos.z), "offset": (off.x, off.y, off.z), "dir": d, "row": row})
    ends = [i for i, q in enumerate(points) if q["kind"] == "end"]
    if len(ends) > 1 or (ends and ends[0] != len(points) - 1):
        problems.append("only one 'end' point is allowed, and it must be the last row")
    return points, problems


def read_harness(sheet, linked=None):
    """Return (wires, problems) from the wires table.

    Each wire is a dict with id, x, y, z, od, radius, total, color, sheet, dir0 (the direction it leaves its start in),
    points and, if its points sheet cannot be used, error. linked = names of the sheets linked to the harness (None = don't check)."""
    doc = sheet.Document
    problems, wires, seen = [], [], set()
    for row in range(HARNESS_FIRST_ROW, HARNESS_FIRST_ROW + SCAN_ROWS):
        wid = _ident(sheet, "A%d" % row)
        if wid == "":
            continue
        if wid in seen:
            problems.append("Wire %s appears more than once in the Wires table" % wid)
            continue
        seen.add(wid)
        name = _contents(sheet, "I%d" % row).lstrip("'").strip() or points_sheet_name(wid)
        w = {
            "id": wid, "row": row, "sheet": name, "points": [],
            "x": _opt(sheet, "B%d" % row, 0.0),
            "y": _opt(sheet, "C%d" % row, 0.0),
            "z": _opt(sheet, "D%d" % row, 0.0),
            "od": _opt(sheet, "E%d" % row, 1.4),
            "radius": _opt(sheet, "F%d" % row, 6.0),
            "total": _opt(sheet, "G%d" % row, 100.0),
            "color": _contents(sheet, "H%d" % row),
        }
        psheet = find_sheet(doc, name)
        if psheet is None:
            w["error"] = "the points sheet '%s' does not exist" % name
            w["error_kind"] = "missing"
        elif linked is not None and psheet.Name not in linked:
            w["error"] = ("the points sheet '%s' is not linked to the harness - run "
                          "wire_harness.link_point_sheets(App.ActiveDocument)" % name)
            w["error_kind"] = "unlinked"
        else:
            w["points"], probs = read_points_sheet(psheet, (w["x"], w["y"], w["z"]))
            problems.extend("Wire %s (%s): %s" % (wid, name, p) for p in probs)
        try:
            w["dir0"] = parse_direction(_contents(sheet, "J%d" % row))
        except ValueError as e:
            w["dir0"] = Vector(0, 0, 1)
            w.setdefault("error", "the 'Leaves toward' cell: %s" % e)
        wires.append(w)
    return wires, problems


# ---------------------------------------------------------------------------
# Spline geometry
# ---------------------------------------------------------------------------

def _unit(v):
    n = v.Length
    return Vector(v.x / n, v.y / n, v.z / n)


DIRECTIONS = [("+Z (up)", (0, 0, 1)), ("-Z (down)", (0, 0, -1)), ("+X", (1, 0, 0)), ("-X", (-1, 0, 0)), ("+Y", (0, 1, 0)),
              ("-Y", (0, -1, 0))]
_DIR_NAMES = {"+x": (1, 0, 0), "-x": (-1, 0, 0), "+y": (0, 1, 0), "-y": (0, -1, 0), "+z": (0, 0, 1), "-z": (0, 0, -1),
              "x": (1, 0, 0), "y": (0, 1, 0), "z": (0, 0, 1), "up": (0, 0, 1), "down": (0, 0, -1)}
_DIR_HEAD = "Leaves toward (+X -X +Y -Y +Z -Z)"


def parse_direction(text):
    """The direction typed in the 'Leaves toward' cell: +X -X +Y -Y +Z -Z (X = +X, up = +Z, down = -Z) or three numbers
    "x, y, z". Returns a unit Vector; blank = up (+Z). Raises ValueError when it cannot be read."""
    t = (text or "").lstrip("'").strip().lower()
    if t == "":
        return Vector(0, 0, 1)
    if t.replace(" ", "") in _DIR_NAMES:
        return Vector(*_DIR_NAMES[t.replace(" ", "")])
    parts = [p for p in re.split(r"[,;\s]+", t) if p]
    try:
        v = Vector(*[float(p) for p in parts]) if len(parts) == 3 else None
    except (ValueError, TypeError):
        v = None
    if v is None:
        raise ValueError("cannot read the direction '%s': use +X, -X, +Y, -Y, +Z, -Z or three numbers x, y, z" % t)
    if v.Length < 1e-9:
        raise ValueError("the direction cannot be (0, 0, 0)")
    return _unit(v)


def format_direction(v):
    """A direction as it is written in the table: +X ... -Z for the six axes, otherwise 'x, y, z'."""
    v = _unit(v if isinstance(v, Vector) else Vector(*v))
    for name, a in (("+X", (1, 0, 0)), ("-X", (-1, 0, 0)), ("+Y", (0, 1, 0)), ("-Y", (0, -1, 0)), ("+Z", (0, 0, 1)), ("-Z", (0, 0, -1))):
        if (v - Vector(*a)).Length < 1e-9:
            return name
    return ", ".join(("%.6f" % c).rstrip("0").rstrip(".") or "0" for c in (v.x, v.y, v.z))


def _start_dir(w):
    """The direction a wire dict leaves its start point in (a unit Vector; up when the wire does not say)."""
    d = w.get("dir0")
    return _unit(d) if d is not None and d.Length > 1e-9 else Vector(0, 0, 1)


def _ensure_direction_header(sheet):
    """Tables made before the 'Leaves toward' column existed get its header the first time it is used."""
    try:
        if _contents(sheet, "J4") == "":
            sheet.set("J4", _DIR_HEAD)
            sheet.setColumnWidth("J", 200)
    except Exception:
        pass


def _chain_tangents(pts, t0, dirs):
    """Tangent direction at every point; dirs[i] (optional) overrides the automatic choice."""
    n = len(pts)
    T = [None] * n
    T[0] = _unit(t0)
    for i in range(1, n - 1):
        if dirs[i] is not None:
            T[i] = _unit(dirs[i])
        else:
            a, b = pts[i] - pts[i - 1], pts[i + 1] - pts[i]
            s = _unit(a) * b.Length + _unit(b) * a.Length      # weight each chord by the OTHER one's length: exact on circular bends
            T[i] = _unit(s) if s.Length > 1e-9 else _unit(b)
    if dirs[-1] is not None:
        T[-1] = _unit(dirs[-1])
    else:
        c = _unit(pts[-1] - pts[-2])
        tp = T[-2]
        T[-1] = _unit(c * (2.0 * tp.dot(c)) - tp)    # continue like a circular arc
    return T


def _bezier_chain(pts, t0, dirs, k):
    """C1 chain of cubic Bezier segments through pts.

    Handle length = k * chord / 3, enlarged by 1 / cos^2(turn / 4), where turn is the change of heading
    along the segment. That makes a bend between two points a close copy of a circular arc."""
    T = _chain_tangents(pts, t0, dirs)
    edges = []
    for i in range(len(pts) - 1):
        turn = math.acos(max(-1.0, min(1.0, T[i].dot(T[i + 1]))))
        h = k * (pts[i + 1] - pts[i]).Length / 3.0 / (math.cos(turn / 4.0) ** 2)
        bz = Part.BezierCurve()
        bz.setPoles([pts[i], pts[i] + T[i] * h, pts[i + 1] - T[i + 1] * h, pts[i + 1]])
        edges.append(bz.toShape())
    return edges, T


def _tightest_radius(edges, samples=60):
    kmax = 0.0
    for e in edges:
        if e.Curve.TypeId == "Part::GeomLine":
            continue
        f, l = e.FirstParameter, e.LastParameter
        for i in range(samples + 1):
            try:
                kmax = max(kmax, abs(e.curvatureAt(f + (l - f) * i / float(samples))))
            except Exception:
                pass
    return (1.0 / kmax) if kmax > 1e-9 else float("inf")


def _spacing_warnings(pts, T, labels, min_radius):
    """Pairs of consecutive points too close together for the turn between them at the minimum radius."""
    out = []
    if min_radius <= 0:
        return out
    for i in range(len(pts) - 1):
        chord = (pts[i + 1] - pts[i]).Length
        theta = math.acos(max(-1.0, min(1.0, T[i].dot(T[i + 1]))))     # change of heading between the two points
        need = 2.0 * min_radius * math.sin(theta / 2.0)                # chord of a turn of that angle at the minimum radius
        if theta > 0.05 and chord < 0.9 * need:
            out.append("%s and %s are %.1f mm apart, but the %.0f deg turn between them needs about %.1f mm "
                       "at the %.1f mm minimum radius" % (labels[i], labels[i + 1], chord, math.degrees(theta), need, min_radius))
    return out


_MIN_TAIL = 0.001       # mm: a straight end shorter than this is left out (a wire cannot be swept along a path that ends in a tiny edge)


def spline_path(w):
    """Centreline of a wire in local coordinates (start at the origin, leaving along its start direction, up by default).

    Returns (edges, note, tightest_radius)."""
    S = Vector(w["x"], w["y"], w["z"])
    pts = [Vector(0, 0, 0)]
    dirs = [None]
    labels = ["the start point"]
    end_pt = None
    for q in w["points"]:
        v = Vector(*q["p"]) - S
        d = Vector(*q["dir"]) if q["dir"] is not None else None
        if q["kind"] == "end":
            end_pt = v
        pts.append(v)
        dirs.append(d)
        labels.append("row %s" % q.get("row", "?"))
    if len(pts) < 2:
        raise ValueError("the wire needs at least one point (via or end) in its points sheet")
    for i, (a, b) in enumerate(zip(pts[:-1], pts[1:])):
        if (b - a).Length < 1e-6:
            raise ValueError("%s and %s are at the same position - move one of them" % (labels[i], labels[i + 1]))
    t0 = _start_dir(w)
    total = w["total"]
    close = _spacing_warnings(pts, _chain_tangents(pts, t0, dirs), labels, w["radius"])
    warn = (" - WARNING: " + "; ".join(close[:3]) + ("; ..." if len(close) > 3 else "")) if close else ""

    if end_pt is None:                                   # free end: straight tail takes the remaining length
        edges, T = _bezier_chain(pts, t0, dirs, 1.0)
        curve = sum(e.Length for e in edges)
        tail = total - curve
        if tail < -1e-6:
            raise ValueError("the curve through the points is %.2f mm long, more than the total length "
                             "of %.2f mm" % (curve, total))
        tail = max(tail, 0.0)
        if tail > _MIN_TAIL:
            p_end = edges[-1].valueAt(edges[-1].LastParameter)
            edges.append(Part.LineSegment(p_end, p_end + T[-1] * tail).toShape())
        note = "free end: %.1f mm curve + %.1f mm straight end = %.1f mm" % (curve, tail, total)
        return edges, note + warn, _tightest_radius(edges)

    def build(k, sag):                                    # fixed end: tune handles (k) or slack (sag)
        p = list(pts)
        dd = list(dirs)
        if sag > 1e-12:
            i = max(range(len(p) - 1), key=lambda j: (p[j + 1] - p[j]).Length)
            c = _unit(p[i + 1] - p[i])
            n = Vector(0, 0, -1) - c * Vector(0, 0, -1).dot(c)      # hang downward
            if n.Length < 1e-6:                                      # chord is vertical: sag sideways
                n = Vector(1, 0, 0) - c * c.x
                if n.Length < 1e-6:
                    n = Vector(0, 1, 0)
            p.insert(i + 1, (p[i] + p[i + 1]) * 0.5 + _unit(n) * sag)
            dd.insert(i + 1, None)
        edges, T = _bezier_chain(p, t0, dd, k)
        return edges, sum(e.Length for e in edges)

    k, sag = 1.0, 0.0
    edges, length = build(k, sag)
    if abs(length - total) > 1e-6:
        if total < length:                                # shorter than the natural curve: tighten handles
            _, lmin = build(0.02, 0.0)
            if total < lmin - 1e-6:
                raise ValueError("too short: through these points the wire needs at least %.1f mm "
                                 "(total length is %.1f mm)" % (lmin, total))
            lo, hi = 0.02, 1.0
            for _ in range(80):
                mid = 0.5 * (lo + hi)
                lo, hi = (mid, hi) if build(mid, 0.0)[1] < total else (lo, mid)
            k = 0.5 * (lo + hi)
        else:                                             # longer: let the slack sag
            lo, hi = 0.0, total
            for _ in range(80):
                mid = 0.5 * (lo + hi)
                lo, hi = (mid, hi) if build(1.0, mid)[1] < total else (lo, mid)
            sag = 0.5 * (lo + hi)
        edges, length = build(k, sag)
    if abs(length - total) > 1e-3:
        raise ValueError("could not reach the total length of %.1f mm (got %.3f mm)" % (total, length))
    if sag > 1e-9:
        how = "%.1f mm of slack hangs as a sag" % sag
    elif k < 0.9999:
        how = "tight fit, handle scale %.2f" % k
    else:
        how = "natural curve"
    return edges, "fixed end: %.1f mm (%s)" % (total, how) + warn, _tightest_radius(edges)


# How the centre line of a twisted wire is fitted by one smooth curve, in the order they are tried: mm between the points that are
# fitted, lowest and highest degree, tolerance (mm). A sweep along one curve does not always come out right, so it is checked.
_TWIST_FIT_MAX = 100.0          # mm: a wire longer than this is not fitted by one curve, it goes straight to the chain of pieces
_TWIST_FITS = ((1.0, (3, 5), 0.005), (1.5, (3, 5), 0.005), (1.0, (3, 5), 0.02), (2.0, (3, 5), 0.02), (2.0, (3, 8), 0.005),
               (0.75, (3, 5), 0.02), (1.0, (3, 8), 0.02))


def _resample_path(P, d):
    """The points P (a polyline) again, every d mm along it."""
    out, acc, nxt = [P[0]], 0.0, d
    for a, b in zip(P[:-1], P[1:]):
        seg = (b - a).Length
        while seg > 0 and acc + seg >= nxt - 1e-12:
            out.append(a + (b - a) * ((nxt - acc) / seg))
            nxt += d
        acc += seg
    if (out[-1] - P[-1]).Length > 1e-6:
        out.append(P[-1])
    return out


def _twist_bspline(pts, r):
    """The wire of radius r swept along one smooth curve through pts, as (shape, the curve), or None if no sweep came out right
    (it must be valid and have the volume of a tube of that radius and length)."""
    for d, deg, tol in _TWIST_FITS:
        try:
            bs = Part.BSplineCurve()
            bs.approximate(Points=_resample_path(pts, d), DegMin=deg[0], DegMax=deg[1], Tolerance=tol, Continuity="C2")
            edge = bs.toShape()
            prof = Part.Wire(Part.makeCircle(r, Vector(0, 0, 0), Vector(*bs.tangent(bs.FirstParameter)[0])))
            shape = Part.Wire([edge]).makePipeShell([prof], True, False)
            if shape.isValid() and abs(abs(shape.Volume) / (math.pi * r * r * edge.Length) - 1.0) < 0.006:
                return shape, edge
        except Exception:
            pass
    return None


def _twisted_shape(w, tw):
    """The swept wire for a wire that is twisted (tw: its twisted centre line, see _twist_group). Returns (shape, report_text)."""
    od = w["od"]
    fit = _twist_bspline(tw["pts"], od / 2.0) if _path_length(tw["pts"]) <= _TWIST_FIT_MAX else None     # (long wires: not worth the tries)
    if fit is not None:                                     # one smooth surface along the whole wire
        shape, tight = fit[0], _tightest_radius([fit[1]], 200)
        if tight <= od / 2.0:
            raise ValueError("the twist bends the wire tighter (%.2f mm radius) than a %.2f mm wire can follow - use a longer lay "
                             "length" % (tight, od))
    else:                                                   # a chain of short pieces: reliable, but a seam shows between the pieces
        edges, t0 = _axis_edges(tw["pts"], max(1.0, min(3.0, tw["lay"] / 8.0)), True)
        tight = _tightest_radius(edges, 8)
        if tight <= od / 2.0:
            raise ValueError("the twist bends the wire tighter (%.2f mm radius) than a %.2f mm wire can follow - use a longer lay "
                             "length" % (tight, od))
        prof = Part.Wire(Part.makeCircle(od / 2.0, Vector(0, 0, 0), t0))
        shape = Part.Wire(edges).makePipeShell([prof], True, False)
        if abs(abs(shape.Volume) / (math.pi * (od / 2.0) ** 2 * sum(e.Length for e in edges)) - 1.0) > 0.02:
            raise ValueError("the twisted wire could not be built along this path - try another lay length, or start the twist elsewhere")
    if shape.Volume < 0:
        shape.reverse()
    if not shape.isValid():
        raise ValueError("the twisted wire shape is not valid - use a longer lay length")
    note = spline_path(w)[1] + ", twisted with wire %s (%+.2f mm of wire)" % (", ".join(tw["mates"]), tw["extra"])
    if w["radius"] > 0 and tight < 0.98 * w["radius"]:
        note += " - WARNING: tightest bend %.1f mm is below the %.1f mm minimum" % (tight, w["radius"])
    else:
        note += ", tightest bend %.1f mm" % tight
    return shape, note


_TWIST_SHAPES = {}             # (document, wire) -> (what it was made from, shape, text)


def _twisted_cached(doc, w, tw):
    where, key = (doc.Name, w["id"]), (repr(w), tw["key"])
    kept = _TWIST_SHAPES.get(where)
    if kept is not None and kept[0] == key:
        return kept[1].copy(), kept[2]
    shape, text = _twisted_shape(w, tw)
    if len(_TWIST_SHAPES) > 100:
        _TWIST_SHAPES.clear()
    _TWIST_SHAPES[where] = (key, shape.copy(), text)
    return shape, text


def build_spline_shape(w, twist=None):
    """Swept wire for a wire dict. Returns (shape, report_text). `twist`: the twisted centre line of the wire, if it is twisted."""
    if twist is not None:
        return _twisted_shape(w, twist)
    edges, note, tight = spline_path(w)
    od = w["od"]
    if tight <= od / 2.0:
        raise ValueError("the path bends tighter (%.2f mm radius) than a %.2f mm wire can follow - "
                         "move the points or change the length" % (tight, od))
    prof = Part.Wire(Part.makeCircle(od / 2.0, Vector(0, 0, 0), _start_dir(w)))
    shape = Part.Wire(edges).makePipeShell([prof], True, False)
    if shape.Volume < 0:
        shape.reverse()
    if not shape.isValid():
        raise ValueError("wire shape is not valid - the path bends too tightly for this wire diameter")
    if tight == float("inf"):
        note += ", straight"
    elif w["radius"] > 0 and tight < 0.98 * w["radius"]:         # 2 % tolerance for numerical rounding
        note += " - WARNING: tightest bend %.1f mm is below the %.1f mm minimum" % (tight, w["radius"])
    else:
        note += ", tightest bend %.1f mm" % tight
    return shape, note


# ---------------------------------------------------------------------------
# The harness object
# ---------------------------------------------------------------------------

def _execute_harness(obj):
    linked = [o.Name for o in obj.PointSheets] if "PointSheets" in obj.PropertiesList else None
    wires, errors = read_harness(obj.Params, linked)
    if any(w.get("error_kind") in ("missing", "unlinked") for w in wires):
        _schedule_sheet_fix(obj)
    if not wires and not errors:
        obj.WireReport = ["No wires yet - add one with the Harness Designer window (New wire...)."]
        obj.Shape = Part.Shape()
        return
    solids, face_colors, report = [], [], []
    paths, tinfo, tproblems = twist_paths(obj.Document, obj, wires)
    tinfo = dict(tinfo)
    for index, w in enumerate(wires):
        try:
            if "error" in w:
                raise ValueError(w["error"])
            tp = paths.get(w["id"])
            if tp is None:
                shape, text = build_spline_shape(w)
            else:
                try:
                    shape, text = _twisted_cached(obj.Document, w, tp)
                except Exception as e:                        # the wire stays straight, the twist is reported
                    tinfo[tp["group"]] = str(e)
                    shape, text = build_spline_shape(w)
                    text += " - TWIST LEFT OUT: %s" % e
        except Exception as e:
            errors.append("Wire %s: %s" % (w["id"], e))
            report.append("Wire %s: ERROR - %s" % (w["id"], e))
            continue
        shape.Placement = FreeCAD.Placement(Vector(w["x"], w["y"], w["z"]), FreeCAD.Rotation())
        solids.append(shape)
        face_colors.extend([parse_color(w["color"], index)] * len(shape.Faces))
        report.append("Wire %s: %s" % (w["id"], text))
    for tid, v in tinfo.items():
        report.append("Twist %s: %s" % (tid, ("ERROR - " + v) if isinstance(v, str) else twist_text(v)))
    report += ["Twist table: %s" % p for p in tproblems]
    obj.WireReport = report
    if not solids:
        raise ValueError("; ".join(errors) if errors else "The Wires table is empty")
    comp = Part.makeCompound(solids)
    comp.Placement = obj.Placement
    obj.Shape = comp
    if FreeCAD.GuiUp and obj.ViewObject is not None:
        try:
            obj.ViewObject.DiffuseColor = face_colors
        except Exception:
            pass
    if errors:
        raise ValueError("; ".join(errors))


class WireHarness:
    """One compound object holding every wire described by the wires table and the points sheets."""

    def __init__(self, obj, sheet):
        obj.Proxy = self
        obj.addProperty("App::PropertyLink", "Params", "Harness", "The wires table")
        obj.addProperty("App::PropertyLinkList", "PointSheets", "Harness", "The points sheets of the wires")
        obj.addProperty("App::PropertyStringList", "WireReport", "Result", "One line per wire (calculated)")
        obj.Params = sheet
        obj.setEditorMode("WireReport", 1)

    def execute(self, obj):
        _execute_harness(obj)

    def onDocumentRestored(self, obj):
        obj.Proxy = self
        if "PointSheets" not in obj.PropertiesList:
            obj.addProperty("App::PropertyLinkList", "PointSheets", "Harness", "The points sheets of the wires")
        obj.setEditorMode("WireReport", 1)

    def dumps(self):
        return None

    def loads(self, state):
        return None

    __getstate__ = dumps
    __setstate__ = loads


# ---------------------------------------------------------------------------
# Creating sheets, wires and the harness
# ---------------------------------------------------------------------------

def write_headers(sh):
    """Title, note, headers and column widths of the wires table."""
    sh.set("A1", "Wire harness - wires table")
    sh.set("A2", "One row per wire. Each wire's points are in its own sheet, named in the last column "
                 "(default Points_<wire>); open that sheet to add or move points. A new row gets its sheet created and linked "
                 "on the next recompute, as a copy of the previous wire's points. Leaves toward: the direction the wire leaves its "
                 "start point in (+X -X +Y -Y +Z -Z or three numbers x, y, z; blank = +Z, up). Blank rows are skipped.")
    for col, head in zip("ABCDEFGHIJ", ["Wire", "Start X (mm)", "Start Y (mm)", "Start Z (mm)", "Outer diameter (mm)",
                                        "Min. bend radius (mm)", "Total length (mm)", "Color", "Points sheet", _DIR_HEAD]):
        sh.set("%s4" % col, head)
    try:
        for col, width in zip("ABCDEFGHIJ", [70, 90, 90, 90, 130, 140, 120, 80, 120, 200]):
            sh.setColumnWidth(col, width)
    except Exception:
        pass


def write_points_headers(sh, wid):
    sh.set("A1", "Wire %s - points" % wid)
    sh.set("A2", "One row per point, in the order the wire passes through them. dX, dY, dZ (mm) are measured from the PREVIOUS point "
                 "(the first point from the wire's start point), so a row can never repeat the position of the one before it. "
                 "Kind via = passes through; kind end (last row) = fixed end; no end row = free end. "
                 "Dir (optional) = the heading of the wire at that point; give all three values or none. "
                 "A bend with straight runs on both sides is two points: one where it starts (Dir = the heading before the bend) "
                 "and one where it ends (Dir = the heading after it). Blank rows are skipped.")
    for col, head in zip("ABCDEFGH", ["Point #", "Kind (via / end)", "dX from previous (mm)", "dY (mm)", "dZ (mm)",
                                      "Dir X (all three or none)", "Dir Y", "Dir Z"]):
        sh.set("%s4" % col, head)
    try:
        for col, width in zip("ABCDEFGH", [70, 110, 150, 80, 80, 170, 80, 80]):
            sh.setColumnWidth(col, width)
    except Exception:
        pass


def fill_points(sh, points):
    """points: list of (kind, (dx, dy, dz), direction_or_None), written from row 5; offsets are from the previous point."""
    for i, (kind, p, d) in enumerate(points):
        r = POINTS_FIRST_ROW + i
        sh.set("A%d" % r, str(i + 1))
        sh.set("B%d" % r, kind)
        for col, val in zip("CDE", p):
            sh.set("%s%d" % (col, r), _fmt(val))
        if d is not None:
            for col, val in zip("FGH", d):
                sh.set("%s%d" % (col, r), _fmt_digits(val, 6))


def make_points_sheet(doc, wid, points, name=None, start=None):
    """Create the points sheet of one wire (start = the wire's start point, used by the example)."""
    name = name or points_sheet_name(wid)
    sh = doc.addObject("Spreadsheet::Sheet", name)
    sh.Label = "Wire %s points" % wid
    write_points_headers(sh, wid)
    fill_points(sh, points)
    return sh


def _harness_wires_sheet(harness):
    return harness.Params


def link_point_sheets(doc, harness=None):
    """Link every points sheet named in the wires table to the harness, so edits rebuild the wires."""
    harnesses = [harness] if harness is not None else \
        [o for o in doc.Objects if type(getattr(o, "Proxy", None)).__name__ == "WireHarness"]
    for h in harnesses:
        sheets = []
        for w in read_harness(h.Params)[0]:
            s = find_sheet(doc, w["sheet"])
            if s is not None and s not in sheets:
                sheets.append(s)
        h.PointSheets = sheets
    doc.recompute()


def make_harness_sheet(doc, name, wires):
    """Create the wires table. wires: list of dicts (id, x, y, z, od, radius, total, color)."""
    sh = doc.addObject("Spreadsheet::Sheet", name)
    write_headers(sh)
    for i, w in enumerate(wires):
        _write_wire_row(sh, HARNESS_FIRST_ROW + i, w)
    return sh


def _write_wire_row(sh, r, w):
    sh.set("A%d" % r, str(w["id"]))
    for col, key, default in (("B", "x", 0.0), ("C", "y", 0.0), ("D", "z", 0.0), ("E", "od", 1.4),
                              ("F", "radius", 6.0), ("G", "total", 100.0)):
        sh.set("%s%d" % (col, r), _fmt(w.get(key, default)))
    if w.get("color"):
        sh.set("H%d" % r, w["color"])
    sh.set("I%d" % r, w.get("sheet") or points_sheet_name(w["id"]))
    d = w.get("dir0")
    if d is not None:
        d = d if isinstance(d, Vector) else Vector(*d)
        if (d - Vector(0, 0, 1)).Length > 1e-9 or _contents(sh, "J4") != "":      # an old table is left alone while every wire goes up
            _ensure_direction_header(sh)
            sh.set("J%d" % r, "'" + format_direction(d))


def make_harness(doc, name, sheet):
    """Create the harness object that builds every wire."""
    obj = doc.addObject("Part::FeaturePython", name)
    WireHarness(obj, sheet)
    if FreeCAD.GuiUp:
        obj.ViewObject.Proxy = 0
    return obj


HARNESS_SHEET = "HarnessTable"
HARNESS_OBJECT = "Wire_Harness"


def _has_harness(doc):
    return any(type(getattr(o, "Proxy", None)).__name__ == "WireHarness" for o in doc.Objects)


def ensure_harness(doc):
    """The wire harness of the document. A document that has none gets an empty one: the wires table and the harness object."""
    for o in doc.Objects:
        if type(getattr(o, "Proxy", None)).__name__ == "WireHarness":
            return o
    sheet = make_harness_sheet(doc, HARNESS_SHEET, [])
    obj = make_harness(doc, HARNESS_OBJECT, sheet)
    doc.recompute()
    return obj


def _open_harness(doc):
    """The harness of the document; one that has none gets an empty harness, as one undo step."""
    if _has_harness(doc):
        return _find_harness(doc)
    doc.openTransaction("New wire harness")
    try:
        obj = ensure_harness(doc)
    except Exception:
        doc.abortTransaction()
        raise
    doc.commitTransaction()
    return obj


def add_wire(doc, harness, wid, x, y, z, points, od=1.4, radius=6.0, total=100.0, color="", direction=None):
    """Add a wire: a row in the wires table plus its own points sheet. points: [(kind, (dx, dy, dz), dir_or_None), ...], each offset measured from the previous point (the first from the wire start). direction: the way the wire leaves its start point, (x, y, z) or a Vector (None = up)."""
    sheet = harness.Params
    wid = str(wid)
    if any(w["id"] == wid for w in read_harness(sheet)[0]):
        raise ValueError("wire %s already exists" % wid)
    row = HARNESS_FIRST_ROW
    while any(_contents(sheet, "%s%d" % (c, row)) != "" for c in "ABCDEFGHIJ"):
        row += 1
    psheet = make_points_sheet(doc, wid, points, start=(x, y, z))
    _write_wire_row(sheet, row, {"id": wid, "x": x, "y": y, "z": z, "od": od, "radius": radius,
                                 "total": total, "color": color, "sheet": psheet.Name, "dir0": direction})
    doc.recompute()
    link_point_sheets(doc, harness)
    return psheet


def remove_wire(doc, harness, wid):
    """Remove a wire: its row in the wires table and its points sheet."""
    sheet = harness.Params
    wid = str(wid)
    found = [w for w in read_harness(sheet)[0] if w["id"] == wid]
    if not found:
        raise ValueError("wire %s does not exist" % wid)
    w = found[0]
    for c in "ABCDEFGHIJ":
        sheet.clear("%s%d" % (c, w["row"]))
    _drop_wire_from_coverings(doc, wid)
    _drop_wire_from_twists(harness, wid)
    harness.PointSheets = [s for s in harness.PointSheets if s.Name != w["sheet"]]
    psheet = find_sheet(doc, w["sheet"])
    doc.recompute()
    if psheet is not None:
        doc.removeObject(psheet.Name)
    doc.recompute()


# ---------------------------------------------------------------------------
# Window layout (only in the FreeCAD GUI)
# ---------------------------------------------------------------------------

def _mdi_area():
    import FreeCADGui
    from PySide import QtWidgets
    return FreeCADGui.getMainWindow().findChild(QtWidgets.QMdiArea)


def show_side_by_side(left_fraction=0.55):
    """Show the 3D view on the left and the open spreadsheets stacked on the right.

    FreeCAD normally shows its windows as tabs; this switches to separate windows and arranges them.
    Undo it with show_tabs()."""
    import FreeCADGui
    from PySide import QtWidgets, QtCore
    mdi = _mdi_area()
    title = lambda w: w.windowTitle().replace("[*]", "").strip()
    wins = mdi.subWindowList()
    views = [w for w in wins if " : " in title(w)]                  # 3D views are named "<document> : <number>"
    sheets = [w for w in wins if w not in views and title(w) != "Start"]
    mdi.setViewMode(QtWidgets.QMdiArea.SubWindowView)
    W, H = mdi.width(), mdi.height()
    left = int(W * left_fraction)
    if views:
        views[0].showNormal()
        views[0].setGeometry(QtCore.QRect(0, 0, left, H))
    weights = [1.0 if title(w) == "HarnessTable" else 2.0 for w in sheets]       # the wires table needs the least room
    total, y = sum(weights), 0
    for i, (w, wt) in enumerate(zip(sheets, weights)):
        h = H - y if i == len(sheets) - 1 else int(H * wt / total)
        w.showNormal()
        w.setGeometry(QtCore.QRect(left, y, W - left, h))
        y += h
    for w in wins:
        if title(w) == "Start":
            w.showMinimized()
    if views:
        mdi.setActiveSubWindow(views[0])
        FreeCADGui.updateGui()
        FreeCADGui.SendMsgToActiveView("ViewFit")


def show_tabs():
    """Back to FreeCAD's normal tabbed windows."""
    from PySide import QtWidgets
    _mdi_area().setViewMode(QtWidgets.QMdiArea.TabbedView)


# ---------------------------------------------------------------------------
# Adding wires quickly
# ---------------------------------------------------------------------------

def _find_harness(doc):
    for o in doc.Objects:
        if type(getattr(o, "Proxy", None)).__name__ == "WireHarness":
            return o
    raise ValueError("this document has no wire harness")


def next_wire_id(sheet):
    """The next free wire number (highest number in the wires table + 1)."""
    nums = [int(w["id"]) for w in read_harness(sheet)[0] if w["id"].isdigit()]
    return str(max(nums) + 1) if nums else "1"


def _auto_color(sheet):
    used = [(w["color"] or "").lstrip("'").strip().lower() for w in read_harness(sheet)[0]]
    for c in PALETTE:
        if c not in used:
            return c
    return PALETTE[len(used) % len(PALETTE)]


def bend_points(lead=20.0, angle=90.0, radius=6.0, toward=(-1, 0, 0), start=(0, 0, 1)):
    """The points of a wire that leaves its start in the direction `start` (up by default), goes straight on `lead` mm
    and then makes one bend: two points (where the bend starts and where it ends), or just the second one when `lead` is 0.

    angle in degrees (0 to 180), radius in mm, toward = the side the bend turns to (only the part of it that is square to
    `start` counts; with the default start that is a horizontal direction, for example (-1, 0, 0) for -X).
    Returns [("via", offset, heading), ("via", offset, heading)] for add_wire."""
    a = math.radians(angle)
    T = _unit(Vector(*start))
    N = Vector(*toward)
    N = N - T * N.dot(T)
    if N.Length < 1e-9:
        raise ValueError("the bend must turn to a side of the direction the wire leaves its start in, not along it")
    N = _unit(N)
    off = T * (radius * math.sin(a)) + N * (radius * (1.0 - math.cos(a)))
    head = T * math.cos(a) + N * math.sin(a)
    bend_end = ("via", (off.x, off.y, off.z), (head.x, head.y, head.z))
    if float(lead) < 1e-6:                      # the bend starts at the start point: there is nothing to pass through before it
        return [bend_end]
    along = T * float(lead)
    return [("via", (along.x, along.y, along.z), (T.x, T.y, T.z)), bend_end]


def duplicate_wire(doc, harness, source, new_id=None, start=None, color=None):
    """Copy a wire (table row + its own points sheet) to a new wire number.

    start = (x, y, z) of the copy; default: the source start moved 2.54 mm along X. Returns the new wire number."""
    found = [w for w in read_harness(harness.Params, [s.Name for s in harness.PointSheets])[0] if w["id"] == str(source)]
    if not found:
        raise ValueError("wire %s does not exist" % source)
    src_w = found[0]
    if "error" in src_w:
        raise ValueError(src_w["error"])
    new_id = str(new_id) if new_id else next_wire_id(harness.Params)
    x, y, z = start if start is not None else (src_w["x"] + 2.54, src_w["y"], src_w["z"])
    points = [(q["kind"], q["offset"], q["dir"]) for q in src_w["points"]]
    add_wire(doc, harness, new_id, x, y, z, points, od=src_w["od"], radius=src_w["radius"], total=src_w["total"],
             color=color or _auto_color(harness.Params), direction=_start_dir(src_w))
    return new_id


_BEND_SIDES = [("-X", (-1, 0, 0)), ("+X", (1, 0, 0)), ("-Y", (0, -1, 0)), ("+Y", (0, 1, 0)), ("-Z", (0, 0, -1)), ("+Z", (0, 0, 1))]


def selected_point():
    """The position of the vertex (or point object) selected in the 3D view, as a Vector, or None."""
    try:
        import FreeCADGui
        for sel in FreeCADGui.Selection.getSelectionEx("", 0):          # 0: positions in the document, not inside a container
            for shp in sel.SubObjects:
                if shp.ShapeType == "Vertex":
                    return Vector(shp.Point)
            if not sel.SubElementNames or all(n == "" for n in sel.SubElementNames):
                shp = getattr(sel.Object, "Shape", None)
                if shp is not None and not shp.isNull() and shp.ShapeType == "Vertex":
                    return Vector(shp.Point)
    except Exception:
        pass
    return None


def build_add_wire_dialog(doc, harness, parent=None):
    """The 'New wire' form. dlg.apply() adds the wire from the current field values; dlg.exec() shows it.

    The wire can start anywhere (type the point, or select a vertex in the 3D view before opening the form), leave its start
    in any of the six axis directions, and be a wire with one bend, a straight wire, or a copy of the points of another wire.
    dlg.result_id is the number of the wire that was added."""
    import FreeCADGui
    from PySide import QtWidgets
    sheet = harness.Params
    wires = read_harness(sheet, [s.Name for s in harness.PointSheets])[0]
    last = wires[-1] if wires else None
    dlg = QtWidgets.QDialog(parent or FreeCADGui.getMainWindow())
    dlg.setWindowTitle("New wire")
    dlg.result_id = None
    form = QtWidgets.QFormLayout(dlg)

    def spin(value, lo=-1000.0, hi=1000.0, dec=2):
        s = QtWidgets.QDoubleSpinBox()
        s.setDecimals(dec)
        s.setRange(lo, hi)
        s.setValue(value)
        return s

    picked = selected_point()
    dlg.f_id = QtWidgets.QLineEdit(next_wire_id(sheet))
    if picked is not None:
        sx, sy, sz = picked.x, picked.y, picked.z
    elif last:
        sx, sy, sz = last["x"] + 2.54, last["y"], last["z"]
    else:
        sx, sy, sz = 0.0, 0.0, 0.0
    dlg.f_x, dlg.f_y, dlg.f_z = spin(sx, dec=3), spin(sy, dec=3), spin(sz, dec=3)
    dlg.b_sel = QtWidgets.QPushButton("Use the selected point")
    dlg.b_sel.setToolTip("Takes the start point from the vertex (or point) that is selected in the 3D view.\n"
                         "Select it before you open this form: the 3D view cannot be used while the form is open.")
    dlg.sel_info = QtWidgets.QLabel("The start is the vertex selected in the 3D view." if picked is not None else "")
    dlg.f_dir = QtWidgets.QComboBox()
    for label_, vec_ in DIRECTIONS:
        dlg.f_dir.addItem(label_, vec_)
    dlg.f_dir.setToolTip("The direction the wire leaves its start point in. Its first stretch follows it.")
    d_last = _start_dir(last) if last else Vector(0, 0, 1)
    for k, (_l, v_) in enumerate(DIRECTIONS):
        if (d_last - Vector(*v_)).Length < 1e-6:
            dlg.f_dir.setCurrentIndex(k)
    dlg.f_color = QtWidgets.QComboBox()
    dlg.f_color.setEditable(True)
    dlg.f_color.addItems(PALETTE)
    dlg.f_color.setCurrentText(_auto_color(sheet))
    dlg.f_od = spin(last["od"] if last else 1.4, 0.1, 50.0)
    dlg.f_radius = spin(last["radius"] if last else 6.0, 0.1, 500.0)
    dlg.f_total = spin(last["total"] if last else 100.0, 1.0, 5000.0, 1)
    dlg.f_shape = QtWidgets.QComboBox()
    dlg.f_shape.addItem("A wire with one bend", "new:bend")
    dlg.f_shape.addItem("A straight wire", "new:straight")
    for w in wires:
        dlg.f_shape.addItem("Copy the points of wire %s" % w["id"], "copy:%s" % w["id"])
    if not wires:                                               # the first wire of a new harness: start with the plain wire (its start and one point)
        dlg.f_shape.setCurrentIndex(dlg.f_shape.findData("new:straight"))
    dlg.f_lead, dlg.f_angle, dlg.f_bend_r = spin(20.0, 0.0, 1000.0, 1), spin(90.0, 1.0, 175.0, 1), spin(6.0, 0.5, 500.0)
    dlg.f_toward = QtWidgets.QComboBox()
    start_row = QtWidgets.QHBoxLayout()
    start_row.addWidget(dlg.b_sel)
    start_row.addWidget(dlg.sel_info, 1)
    for label, widget in (("Wire number", dlg.f_id), ("Start X (mm)", dlg.f_x), ("Start Y (mm)", dlg.f_y), ("Start Z (mm)", dlg.f_z),
                          ("", start_row), ("Leaves its start toward", dlg.f_dir),
                          ("Color", dlg.f_color), ("Outer diameter (mm)", dlg.f_od), ("Min. bend radius (mm)", dlg.f_radius),
                          ("Total length (mm)", dlg.f_total), ("Shape", dlg.f_shape),
                          ("Straight run before the bend (mm)", dlg.f_lead), ("Bend angle (deg)", dlg.f_angle),
                          ("Bend radius (mm)", dlg.f_bend_r), ("Bend toward", dlg.f_toward)):
        form.addRow(label, widget)
    bend_fields = (dlg.f_lead, dlg.f_angle, dlg.f_bend_r, dlg.f_toward)

    def dir_vec():
        return Vector(*dlg.f_dir.currentData())

    def refresh_toward():
        T = dir_vec()
        keep = dlg.f_toward.currentText()
        dlg.f_toward.blockSignals(True)
        dlg.f_toward.clear()
        for name, vec in _BEND_SIDES:
            if abs(Vector(*vec).dot(T)) < 0.5:                  # a side of the direction, not along it
                dlg.f_toward.addItem(name, vec)
        i = dlg.f_toward.findText(keep)
        dlg.f_toward.setCurrentIndex(i if i >= 0 else 0)
        dlg.f_toward.blockSignals(False)

    def refresh():
        kind = dlg.f_shape.currentData()
        for f in bend_fields:
            f.setEnabled(kind == "new:bend")
        if kind.startswith("copy:"):                           # a copy leaves its start the same way as the wire it copies
            src = [w for w in wires if w["id"] == kind.split(":", 1)[1]]
            if src:
                d = _start_dir(src[0])
                for k, (_l, v_) in enumerate(DIRECTIONS):
                    if (d - Vector(*v_)).Length < 1e-6:
                        dlg.f_dir.setCurrentIndex(k)

    def use_selected():
        p = selected_point()
        if p is None:
            dlg.sel_info.setText("Nothing to use: select a vertex or a point in the 3D view, then open this form.")
            return
        dlg.f_x.setValue(p.x)
        dlg.f_y.setValue(p.y)
        dlg.f_z.setValue(p.z)
        dlg.sel_info.setText("The start is the vertex selected in the 3D view.")
    dlg.b_sel.clicked.connect(use_selected)
    dlg.f_shape.currentIndexChanged.connect(lambda _i: refresh())
    dlg.f_dir.currentIndexChanged.connect(lambda _i: refresh_toward())
    refresh_toward()
    refresh()

    def apply():
        wid = dlg.f_id.text().strip()
        if not wid:
            raise ValueError("enter a wire number")
        d0 = dir_vec()
        kind = dlg.f_shape.currentData()
        total = dlg.f_total.value()
        if kind == "new:bend":
            points = bend_points(dlg.f_lead.value(), dlg.f_angle.value(), dlg.f_bend_r.value(), dlg.f_toward.currentData(), d0)
        elif kind == "new:straight":
            end = d0 * total                    # the start and one point at the end: no other point is needed
            points = [("via", (end.x, end.y, end.z), None)]
        else:
            src_w = [w for w in read_harness(sheet, [s.Name for s in harness.PointSheets])[0] if w["id"] == kind.split(":", 1)[1]][0]
            points = [(q["kind"], q["offset"], q["dir"]) for q in src_w["points"]]
        doc.openTransaction("Add wire")
        try:
            add_wire(doc, harness, wid, dlg.f_x.value(), dlg.f_y.value(), dlg.f_z.value(), points, od=dlg.f_od.value(),
                     radius=dlg.f_radius.value(), total=total, color=dlg.f_color.currentText().strip(), direction=d0)
            line = [l for l in harness.WireReport if l.startswith("Wire %s:" % wid)]
            if line and "ERROR" in line[0]:
                raise ValueError("Wire %s cannot be built like this: %s" % (wid, line[0].split("ERROR - ", 1)[-1]))
        except Exception:
            doc.abortTransaction()
            doc.recompute()
            raise
        doc.commitTransaction()
        dlg.result_id = wid
        return wid
    dlg.apply = apply

    buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
    buttons.button(QtWidgets.QDialogButtonBox.Ok).setText("Add wire")
    form.addRow(buttons)

    def on_ok():
        try:
            wid = apply()
        except Exception as e:
            QtWidgets.QMessageBox.warning(dlg, "New wire", str(e))
            return
        FreeCAD.Console.PrintMessage("wire_harness: added wire %s\n" % wid)
        dlg.accept()
    buttons.accepted.connect(on_ok)
    buttons.rejected.connect(dlg.reject)
    return dlg


def add_wire_dialog(doc=None):
    """Open the 'New wire' form. Works in any document: one with no harness gets an empty harness first, and with no
    document open a new one is made."""
    doc = doc or FreeCAD.ActiveDocument or FreeCAD.newDocument("WireHarness")
    return build_add_wire_dialog(doc, _open_harness(doc)).exec()

# ---------------------------------------------------------------------------
# A wire declared in the wires table gets its points sheet by itself
# ---------------------------------------------------------------------------

_fix_pending = set()
_fix_failed = set()


def _schedule_sheet_fix(obj):
    """Ask for the missing / unlinked points sheets to be put right just after the current recompute."""
    key = (obj.Document.Name, obj.Name)
    if key in _fix_pending:
        return
    try:
        from PySide import QtCore
    except Exception:
        return
    _fix_pending.add(key)
    QtCore.QTimer.singleShot(150, lambda: _fix_sheets(key[0], key[1]))


def _create_sheet_for(doc, harness, w, previous):
    """Create the points sheet a declared wire is missing: a copy of the previous wire's points, or one 90 degree bend."""
    name = w["sheet"]
    obj_name = re.sub(r"[^0-9A-Za-z_]", "_", name)
    if not obj_name or obj_name[0].isdigit():
        obj_name = "P_" + obj_name
    if doc.getObject(obj_name) is not None:
        raise ValueError("an object named '%s' already exists" % obj_name)
    if previous is not None and previous["points"]:
        points = [(q["kind"], q["offset"], q["dir"]) for q in previous["points"]]
    else:
        points = bend_points()
    ps = make_points_sheet(doc, w["id"], points, name=obj_name)
    if obj_name != name:
        ps.Label = name                                     # the table finds a sheet by its name OR its label
    sheet = harness.Params
    if _contents(sheet, "I%d" % w["row"]) == "":
        sheet.set("I%d" % w["row"], ps.Name)                # show which sheet was made
    return ps


def _fix_sheets(doc_name, obj_name, attempt=0):
    try:
        doc = FreeCAD.getDocument(doc_name)
        if doc.Recomputing and attempt < 40:                  # FreeCAD is in the middle of a recompute: try again shortly
            from PySide import QtCore
            QtCore.QTimer.singleShot(200, lambda: _fix_sheets(doc_name, obj_name, attempt + 1))
            return
    except Exception:
        _fix_pending.discard((doc_name, obj_name))
        return
    _fix_pending.discard((doc_name, obj_name))
    try:
        harness = doc.getObject(obj_name)
        wires = read_harness(harness.Params, [s.Name for s in harness.PointSheets])[0]
        previous, changed = None, False
        for w in wires:
            kind = w.get("error_kind")
            if kind == "missing":
                key = (doc_name, w["id"], w["sheet"])
                if key not in _fix_failed:
                    try:
                        ps = _create_sheet_for(doc, harness, w, previous)
                        FreeCAD.Console.PrintMessage("wire_harness: created the points sheet %s for wire %s (a copy of the previous "
                                                     "wire's points - edit it to change the route)\n" % (ps.Name, w["id"]))
                        changed = True
                    except Exception as e:
                        _fix_failed.add(key)
                        FreeCAD.Console.PrintWarning("wire_harness: could not create the points sheet '%s' for wire %s: %s\n"
                                                     % (w["sheet"], w["id"], e))
            elif kind == "unlinked":
                changed = True
            if "error" not in w:
                previous = w
        if changed:
            doc.recompute()                                   # so the new sheet is calculated before it is linked
            link_point_sheets(doc, harness)
    except Exception as e:
        FreeCAD.Console.PrintWarning("wire_harness: could not put the points sheets right: %s\n" % e)


# ---------------------------------------------------------------------------
# Moving and turning points
# ---------------------------------------------------------------------------

ROTATE_MODES = ["tilt toward -X", "tilt toward +X", "tilt toward -Y", "tilt toward +Y", "tilt up", "tilt down",
                "turn counter-clockwise", "turn clockwise"]
_TILT_TARGETS = {"tilt toward -X": (-1, 0, 0), "tilt toward +X": (1, 0, 0), "tilt toward -Y": (0, -1, 0),
                 "tilt toward +Y": (0, 1, 0), "tilt up": (0, 0, 1), "tilt down": (0, 0, -1)}


def _wire_dict(doc, harness, wid):
    for w in read_harness(harness.Params, [s.Name for s in harness.PointSheets])[0]:
        if w["id"] == str(wid):
            if "error" in w:
                raise ValueError(w["error"])
            return w
    raise ValueError("wire %s does not exist" % wid)


def point_headings(w):
    """The heading of the wire at each of its table points (the one you typed, or the one FreeCAD chooses)."""
    S = Vector(w["x"], w["y"], w["z"])
    pts = [Vector(0, 0, 0)] + [Vector(*q["p"]) - S for q in w["points"]]
    dirs = [None] + [Vector(*q["dir"]) if q["dir"] is not None else None for q in w["points"]]
    return _chain_tangents(pts, _start_dir(w), dirs)[1:]


def heading_angles(v):
    """(tilt from vertical in degrees, direction around Z in degrees with 0 = +X; None when the heading is vertical)."""
    tilt = math.degrees(math.acos(max(-1.0, min(1.0, v.z / v.Length))))
    direction = None if (tilt < 0.5 or tilt > 179.5) else math.degrees(math.atan2(v.y, v.x))
    return tilt, direction


def _set_offset(ps, row, vec):
    for col, val in zip("CDE", (vec.x, vec.y, vec.z)):
        ps.set("%s%d" % (col, row), _fmt(val))


def _set_offset_fine(ps, row, vec):
    """Like _set_offset, with 7 decimals: a wire pulled in to be exactly as long as its Length needs more than the usual 4."""
    for col, val in zip("CDE", (vec.x, vec.y, vec.z)):
        ps.set("%s%d" % (col, row), _fmt_digits(val, 7))


def _set_heading(ps, row, vec):
    if vec is None:
        for col in "FGH":
            ps.clear("%s%d" % (col, row))
    else:
        u = _unit(vec)
        for col, val in zip("FGH", (u.x, u.y, u.z)):       # 6 decimals: a turn of 0.01 degree is still stored
            txt = ("%.6f" % val).rstrip("0").rstrip(".")
            ps.set("%s%d" % (col, row), "0" if txt in ("-0", "") else txt)


def move_point(doc, harness, wid, row, delta, later_follow=True):
    """Move a point by delta = (dx, dy, dz) mm in model coordinates.

    row = "start" (the wire's start point in the wires table) or the row number of a point in the wire's points sheet.
    later_follow=True: the points after it move too (the rest of the wire keeps its shape);
    False: only this point moves and the later points stay where they are.

    The point before the moved one and the point after it keep the heading they have now (see _kept_headings), so only the
    two stretches next to the moved point change. A free-end wire never gets longer than its Length: when the move would make the
    curve through the points longer, the last point is pulled in toward the one before it (see _pull_end_in).
    Returns a sentence saying so when that happened, otherwise ""."""
    w = _wire_dict(doc, harness, wid)
    d = Vector(*delta)
    ps = find_sheet(doc, w["sheet"])
    if row == "start":
        idx = None
    else:
        rows = [q["row"] for q in w["points"]]
        if int(row) not in rows:
            raise ValueError("wire %s has no point in row %s" % (wid, row))
        idx = rows.index(int(row))
    _w_new, keep, last_off, pulled = _plan_edit(w, _moved_wire(w, idx, d, later_follow), idx)
    if idx is None:
        for col, val in zip("BCD", (w["x"] + d.x, w["y"] + d.y, w["z"] + d.z)):
            harness.Params.set("%s%d" % (col, w["row"]), _fmt(val))
        if not later_follow and w["points"]:
            first = w["points"][0]
            _set_offset(ps, first["row"], Vector(*first["offset"]) - d)
    else:
        q = w["points"][idx]
        _set_offset(ps, q["row"], Vector(*q["offset"]) + d)
        if not later_follow and idx + 1 < len(w["points"]):
            nxt = w["points"][idx + 1]
            _set_offset(ps, nxt["row"], Vector(*nxt["offset"]) - d)
    for j, heading in keep.items():
        _set_heading(ps, w["points"][j]["row"], heading)
    if last_off is not None:
        _set_offset_fine(ps, w["points"][-1]["row"], last_off)
    doc.recompute()
    return _pull_note(w, pulled)


def rotate_point(doc, harness, wid, row, angle, mode, later_follow=True):
    """Turn the heading of a point by `angle` degrees. mode is one of ROTATE_MODES.

    "tilt toward X" turns the heading toward that direction; "turn ... (about Z)" turns it around the vertical.
    later_follow=True: the later points swing around this point by the same rotation (the rest of the wire
    keeps its shape); False: only the heading of this point changes."""
    w = _wire_dict(doc, harness, wid)
    rows = [q["row"] for q in w["points"]]
    if int(row) not in rows:
        raise ValueError("wire %s has no point in row %s" % (wid, row))
    idx = rows.index(int(row))
    h = point_headings(w)[idx]
    if mode == ROTATE_MODES[6]:
        axis = Vector(0, 0, 1)
    elif mode == ROTATE_MODES[7]:
        axis = Vector(0, 0, -1)
    elif mode in _TILT_TARGETS:
        axis = h.cross(Vector(*_TILT_TARGETS[mode]))
        if axis.Length < 1e-6:
            raise ValueError("the heading already points along that direction, so it cannot tilt toward it - choose another")
        axis = _unit(axis)
    else:
        raise ValueError("unknown rotation '%s'" % mode)
    return rotate_point_about(doc, harness, wid, row, axis, angle, later_follow)


def rotate_point_about(doc, harness, wid, row, axis, angle, later_follow=True):
    """Turn the heading of a point by `angle` degrees around `axis` (a direction, through the point).

    later_follow=True: the later points swing around this point by the same rotation; False: only this heading changes.
    The point before it and the point after it keep the heading they have now (see _kept_headings), so only the two
    stretches next to the turned point change. A free-end wire never gets longer than its Length: when the turn would make the
    curve through the points longer, the last point is pulled in toward the one before it (see _pull_end_in).
    Returns a sentence saying so when that happened, otherwise ""."""
    w = _wire_dict(doc, harness, wid)
    rows = [q["row"] for q in w["points"]]
    if int(row) not in rows:
        raise ValueError("wire %s has no point in row %s" % (wid, row))
    idx = rows.index(int(row))
    new_heading, later = _turn_plan(w, idx, axis, angle, later_follow)
    _w_new, keep, last_off, pulled = _plan_edit(w, _turned_wire(w, idx, new_heading, later), idx,
                                                Rotation(Vector(*axis), angle) if later_follow else None)
    ps = find_sheet(doc, w["sheet"])
    _set_heading(ps, w["points"][idx]["row"], new_heading)
    for j, off, d in later:
        _set_offset(ps, w["points"][j]["row"], off)
        if d is not None:
            _set_heading(ps, w["points"][j]["row"], d)
    for j, heading in keep.items():
        _set_heading(ps, w["points"][j]["row"], heading)
    if last_off is not None:
        _set_offset_fine(ps, w["points"][-1]["row"], last_off)
    doc.recompute()
    return _pull_note(w, pulled)


def _turn_plan(w, idx, axis, angle, later_follow=True, heading=None):
    """What turning the heading of point `idx` of wire dict `w` by `angle` degrees around `axis` changes.

    Returns (new heading, [(index, new offset, new typed direction or None), ...]); the list is empty unless later_follow."""
    h = heading if heading is not None else point_headings(w)[idx]
    rot = Rotation(Vector(*axis), angle)
    later = []
    if later_follow:
        for j in range(idx + 1, len(w["points"])):
            q = w["points"][j]
            d = rot.multVec(Vector(*q["dir"])) if q["dir"] is not None else None
            later.append((j, rot.multVec(Vector(*q["offset"])), d))
    return rot.multVec(h), later


_HOLD_TOL = 1e-4        # radians (0.006 deg): a neighbour that would turn by less than this is left alone
_FIT_LOW, _FIT_HIGH = 2e-7, 8e-7     # mm: how much longer than its Length a pulled-in wire ends up (spline_path accepts up to 1e-6)


def _stored_vec(v):
    """A Vector as it reads back after _set_offset wrote it (4 decimals)."""
    return Vector(_stored(v.x), _stored(v.y), _stored(v.z))


def _stored_dir(v):
    """A heading as it reads back after _set_heading wrote it (unit length, 6 decimals)."""
    u = _unit(v)
    return Vector(*(_stored(c, 6) for c in (u.x, u.y, u.z)))


def _moved_wire(w, idx, d, later_follow):
    """The wire dict `w` as it would be after point `idx` (None: the start point) moves by the Vector `d`, with the numbers rounded as
    they are when they are written to the sheet. Nothing is written."""
    pts = [dict(q) for q in w["points"]]
    S = Vector(w["x"], w["y"], w["z"])
    if idx is None:
        S = Vector(_stored(S.x + d.x), _stored(S.y + d.y), _stored(S.z + d.z))
        if not later_follow and pts:
            pts[0]["offset"] = tuple(_stored_vec(Vector(*pts[0]["offset"]) - d))
    else:
        pts[idx]["offset"] = tuple(_stored_vec(Vector(*pts[idx]["offset"]) + d))
        if not later_follow and idx + 1 < len(pts):
            pts[idx + 1]["offset"] = tuple(_stored_vec(Vector(*pts[idx + 1]["offset"]) - d))
    pos = Vector(S)
    for q in pts:
        pos = pos + Vector(*q["offset"])
        q["p"] = (pos.x, pos.y, pos.z)
    return dict(w, x=S.x, y=S.y, z=S.z, points=pts)


def _turned_wire(w, idx, new_heading, later):
    """The wire dict `w` as it would be after the heading of point `idx` becomes `new_heading` and the points in `later`
    (from _turn_plan) swing with it, with the numbers rounded as they are when they are written to the sheet. Nothing is written."""
    pts = [dict(q) for q in w["points"]]
    pts[idx]["dir"] = tuple(_stored_dir(new_heading))
    for j, off, d in later:
        pts[j]["offset"] = tuple(_stored_vec(off))
        if d is not None:
            pts[j]["dir"] = tuple(_stored_dir(d))
    pos = Vector(w["x"], w["y"], w["z"])
    for q in pts:
        pos = pos + Vector(*q["offset"])
        q["p"] = (pos.x, pos.y, pos.z)
    return dict(w, points=pts)


def _with_dirs(w2, keep):
    """The wire dict `w2` with the headings in `keep` ({index: Vector}) typed in."""
    if not keep:
        return w2
    pts = [dict(q) for q in w2["points"]]
    for j, heading in keep.items():
        pts[j]["dir"] = (heading.x, heading.y, heading.z)
    return dict(w2, points=pts)


def _wire_from_items(w, items):
    """The wire dict `w` with the points `items` ([kind, offset Vector, heading Vector or None], as _point_items gives them) as they
    read back after _rewrite_points wrote them."""
    pos, pts = Vector(w["x"], w["y"], w["z"]), []
    for i, (k, o, d) in enumerate(items):
        o = _stored_vec(o)
        pos = pos + o
        pts.append({"kind": k, "p": (pos.x, pos.y, pos.z), "offset": (o.x, o.y, o.z), "row": POINTS_FIRST_ROW + i,
                    "dir": None if d is None else tuple(_stored(c, 6) for c in d)})
    return dict(w, points=pts)


def _kept_headings(w, w2, idx, rot=None):
    """Headings to type so that editing one point leaves the points next to it as they are.

    A point without a typed heading gets its heading from the points on both sides of it, so moving or turning point `idx`
    would also turn the point before it and the point after it, and bend the stretches beyond them. `w` is the wire before the
    edit, `w2` the wire after it (idx = None for the start point). `rot` is the Rotation the later points swing by when they
    follow a turn: their headings are meant to turn with it, so they are compared with the turned ones.

    Returns {index: heading} for the neighbours that have no typed heading and would turn; typing these keeps them as they were.
    No other point can change: a heading depends on the points on both sides of it (the last point also on the heading before it)."""
    first = -1 if idx is None else idx
    try:
        before, after = point_headings(w), point_headings(w2)
    except Exception:
        return {}                                             # a wire that cannot be built anyway: nothing to keep
    keep = {}
    for j in (first - 1, first + 1):
        if not 0 <= j < len(w2["points"]) or w2["points"][j]["dir"] is not None:
            continue
        want = _unit(before[j] if (rot is None or j < first) else rot.multVec(before[j]))
        if math.acos(max(-1.0, min(1.0, _unit(after[j]).dot(want)))) > _HOLD_TOL:
            keep[j] = _stored_dir(want)
    return keep


def _pull_end_in(w2, idx):
    """Pull the last point of the free-end wire dict `w2` in toward the point before it, if the curve through the points is longer
    than the wire's Length.

    A free-end wire has to be at least as long as the curve through its points, so bending it can make it too long. Rather than
    raising the Length, the last point slides back along the line from the point before it, just far enough for the curve to be as
    long as the Length (up to a millionth of a mm more: the straight end of a wire cannot be shorter than nothing). The point before
    the last one keeps the heading it has, unless it is the edited point `idx` (None: the start point), so that the stretch before it
    stays as it is.

    Returns (wire dict, {index: heading to type in}, new offset of the last point or None, mm it was pulled in), or None when the wire
    is too long and cannot be fitted even with the last point right next to the one before it. A wire that fits, or has a fixed end,
    comes back as it is."""
    pts = w2["points"]
    n = len(pts)
    same = (w2, {}, None, 0.0)
    if n == 0 or any(q["kind"] == "end" for q in pts):
        return same
    total = w2["total"]
    try:
        if curve_length(w2) <= total + 1e-6:
            return same
    except Exception:
        return same                                           # a wire that cannot be built anyway
    try:
        keep = {}
        if n >= 2 and pts[n - 2]["dir"] is None and idx != n - 2:
            keep[n - 2] = _stored_dir(point_headings(w2)[n - 2])
        base = _with_dirs(w2, keep)
        prev = Vector(*pts[n - 2]["p"]) if n >= 2 else Vector(w2["x"], w2["y"], w2["z"])
        off = Vector(*pts[-1]["offset"])
        L0 = off.Length
        lo = _MIN_POINT_GAP / L0 if L0 > 1e-9 else 1.0
        if lo >= 1.0:
            return None

        def fitted(s):
            o = Vector(*(_stored(c * s, 7) for c in (off.x, off.y, off.z)))
            p = prev + o
            return o, dict(base, points=base["points"][:-1] + [dict(pts[-1], offset=(o.x, o.y, o.z), p=(p.x, p.y, p.z))])

        over = lambda s: curve_length(fitted(s)[1]) - total        # mm longer than the Length with the last offset scaled by s
        fb = curve_length(base) - total
        if fb <= 1e-6:
            return base, keep, None, 0.0                           # keeping that heading was enough
        a, fa = lo, over(lo)
        if fa > _FIT_HIGH:
            return None
        s = a
        if fa < _FIT_LOW:                                          # regula falsi (Illinois) on [lo, 1]
            t = 0.5 * (_FIT_LOW + _FIT_HIGH)
            ga, b, gb, side, s = fa - t, 1.0, fb - t, 0, None
            for _ in range(60):
                c = (a * gb - b * ga) / (gb - ga)
                gc = over(c) - t
                if abs(gc) <= 0.5 * (_FIT_HIGH - _FIT_LOW):
                    s = c
                    break
                if gc > 0:
                    b, gb = c, gc
                    if side == 1:
                        ga *= 0.5
                    side = 1
                else:
                    a, ga = c, gc
                    if side == -1:
                        gb *= 0.5
                    side = -1
            if s is None:
                return None
        o, w_fit = fitted(s)
        return w_fit, keep, o, L0 - o.Length
    except Exception:
        return None


def _plan_edit(w, w2, idx, rot=None):
    """What an edit of point `idx` (None: the start point) has to write besides the edit itself, and the wire that comes out.

    `w` is the wire before the edit, `w2` after it (_moved_wire / _turned_wire). The points next to the edited one keep their
    headings (_kept_headings), and a free-end wire that would get longer than its Length has its last point pulled in (_pull_end_in).
    Returns (wire dict after the edit, {index: heading to type in}, new offset of the last point or None, mm the last point was pulled in).
    A wire that is too long even so is returned as it is: the caller's check refuses it."""
    keep = _kept_headings(w, w2, idx, rot)
    held = _with_dirs(w2, keep)
    fit = _pull_end_in(held, idx)
    if fit is None:
        return held, keep, None, 0.0
    w3, more, last_off, pulled = fit
    keep.update(more)
    return w3, keep, last_off, pulled


def _pull_note(w, pulled):
    """What to tell the person when the end of a wire was pulled in to keep its Length ("" when it was not)."""
    if pulled < 0.005:
        return ""
    return "End of wire %s pulled in by %.2f mm to keep its Length (%s mm)." % (w["id"], pulled, _fmt(w["total"]))


def _status_message(text, ms=7000):
    """A short message in FreeCAD's status bar."""
    try:
        import FreeCADGui
        FreeCADGui.getMainWindow().statusBar().showMessage(text, ms)
    except Exception:
        pass


def _preview_path(w2, S, samples=12):
    """Centreline of wire dict w2 as points in model coordinates. Returns (path or None, problem text or None)."""
    try:
        edges, _note, tight = spline_path(w2)
    except Exception as e:
        return None, str(e)
    problem = None
    if tight <= w2["od"] / 2.0:
        problem = "the wire would bend tighter (%.2f mm radius) than its diameter allows" % tight
    path = []
    for e in edges:
        path.extend(p + S for p in e.discretize(samples))
    return path, problem


def move_previewer(doc, harness, wid, row, later_follow=True, whole=False, samples=12):
    """Returns preview(delta) -> {"path": [Vector, ...] or None, "problem": text or None}.

    preview() changes nothing: it shows how wire `wid` would run after move_point(doc, harness, wid, row, delta, later_follow)
    (whole=True: after moving the whole wire, as move_point(..., "start", delta, True) does)."""
    w = _wire_dict(doc, harness, wid)
    rows = [q["row"] for q in w["points"]]
    if whole:
        row, later_follow = "start", True
    idx = None
    if row != "start":
        if int(row) not in rows:
            raise ValueError("wire %s has no point in row %s" % (wid, row))
        idx = rows.index(int(row))

    def preview(delta):
        w2, _keep, _last, pulled = _plan_edit(w, _moved_wire(w, idx, Vector(*delta), later_follow), idx)     # as move_point does
        path, problem = _preview_path(w2, Vector(w2["x"], w2["y"], w2["z"]), samples)
        return {"path": path, "problem": problem, "note": _pull_note(w, pulled)}
    return preview


def turn_previewer(doc, harness, wid, row, later_follow=True, samples=12):
    """Returns preview(axis, angle) -> {"heading": Vector, "path": [Vector, ...], "problem": text or None}.

    preview() changes nothing: it shows how wire `wid` would run after turning the heading of point `row` by `angle`
    degrees around `axis` (model coordinates). The wire is read once, so calling preview() is cheap."""
    w = _wire_dict(doc, harness, wid)
    rows = [q["row"] for q in w["points"]]
    if int(row) not in rows:
        raise ValueError("wire %s has no point in row %s" % (wid, row))
    idx = rows.index(int(row))
    h0 = point_headings(w)[idx]
    S = Vector(w["x"], w["y"], w["z"])

    def preview(axis, angle):
        new_heading, later = _turn_plan(w, idx, axis, angle, later_follow, h0)
        rot = Rotation(Vector(*axis), angle) if later_follow else None
        w2, _keep, _last, pulled = _plan_edit(w, _turned_wire(w, idx, new_heading, later), idx, rot)          # as rotate_point_about does
        out = {"heading": new_heading, "path": [], "problem": None, "note": _pull_note(w, pulled)}
        try:
            edges, _note, tight = spline_path(w2)
        except Exception as e:
            out["problem"] = str(e)
            return out
        if tight <= w2["od"] / 2.0:
            out["problem"] = "the wire would bend tighter (%.2f mm radius) than its diameter allows" % tight
        path = []
        for e in edges:
            path.extend(p + S for p in e.discretize(samples))
        out["path"] = path
        return out
    return preview


def clear_heading(doc, harness, wid, row):
    """Remove the typed heading of a point, so FreeCAD chooses a smooth one again.

    A free-end wire never gets longer than its Length: when the smooth heading makes the curve longer, the last point is pulled in
    toward the one before it (see _pull_end_in). Returns a sentence saying so when that happened, otherwise ""."""
    w = _wire_dict(doc, harness, wid)
    rows = [q["row"] for q in w["points"]]
    if int(row) not in rows:
        raise ValueError("wire %s has no point in row %s" % (wid, row))
    idx = rows.index(int(row))
    ps = find_sheet(doc, w["sheet"])
    pts = [dict(q) for q in w["points"]]
    pts[idx]["dir"] = None
    fit = _pull_end_in(dict(w, points=pts), idx)
    _set_heading(ps, int(row), None)
    pulled = 0.0
    if fit is not None:
        _w_new, keep, last_off, pulled = fit
        for j, heading in keep.items():
            _set_heading(ps, w["points"][j]["row"], heading)
        if last_off is not None:
            _set_offset_fine(ps, w["points"][-1]["row"], last_off)
    doc.recompute()
    return _pull_note(w, pulled)


MARKER_NAME = "Wire_Point_Marker"


def show_point_marker(doc, position, heading):
    """A yellow sphere with an arrow along the heading, so the selected point is visible in the 3D view."""
    p = Vector(*position)
    h = _unit(Vector(*heading))
    shape = Part.makeCompound([Part.makeSphere(1.3, p), Part.makeCylinder(0.4, 6.0, p, h),
                               Part.makeCone(1.0, 0.0, 3.0, p + h * 6.0, h)])
    obj = doc.getObject(MARKER_NAME) or doc.addObject("Part::Feature", MARKER_NAME)
    obj.Shape = shape
    if FreeCAD.GuiUp and obj.ViewObject is not None:
        obj.ViewObject.ShapeColor = (1.0, 0.9, 0.1)
    return obj


def remove_point_marker(doc):
    if doc.getObject(MARKER_NAME) is not None:
        doc.removeObject(MARKER_NAME)


# ---------------------------------------------------------------------------
# Collisions between wires
# ---------------------------------------------------------------------------

COLLISION_NAME = "WireCollisions"
_COLLISION_STEP = 0.3     # mm between the points a wire is sampled at when it is searched for collisions
_NEAR_MM = 25.0           # wires further apart than this are not compared (they cannot matter)
_TOUCH = 1e-6             # mm: a gap this small counts as touching
_RING_RADIUS = 20.0                    # logical pixels: the ring that marks a collision on the screen
_RING_POINTS = 40
_COLLIDE_COLOR = (1.0, 0.15, 0.15)     # overlapping
_TIGHT_COLOR = (1.0, 0.65, 0.1)        # closer than the minimum gap, but not overlapping


def _setting(name, default):
    p = FreeCAD.ParamGet(_GIZMO_PARAM)
    try:
        if isinstance(default, bool):
            return bool(p.GetBool(name, default))
        return float(p.GetFloat(name, default))
    except Exception:
        return default


def _save_setting(name, value):
    p = FreeCAD.ParamGet(_GIZMO_PARAM)
    try:
        if isinstance(value, bool):
            p.SetBool(name, value)
        else:
            p.SetFloat(name, float(value))
    except Exception:
        pass


def _nearest_samples(P, Q):
    """For every point of P the nearest point of Q: (list of distances, list of indices into Q)."""
    try:
        import numpy as np
    except ImportError:
        dist, idx = [], []
        for p in P:
            best, at = None, 0
            for j, q in enumerate(Q):
                d = (p - q).Length
                if best is None or d < best:
                    best, at = d, j
            dist.append(best)
            idx.append(at)
        return dist, idx
    a = np.array([(p.x, p.y, p.z) for p in P])
    b = np.array([(q.x, q.y, q.z) for q in Q])
    d2 = ((a[:, None, :] - b[None, :, :]) ** 2).sum(axis=2)
    j = d2.argmin(axis=1)
    return [float(x) for x in np.sqrt(d2[np.arange(len(a)), j])], [int(x) for x in j]


def _runs(flags, hole=2):
    """(first, last) of every stretch of True values; up to `hole` False values inside a stretch do not break it."""
    out, i, n = [], 0, len(flags)
    while i < n:
        if not flags[i]:
            i += 1
            continue
        last, k = i, i + 1
        while k < n and k - last <= hole + 1:
            if flags[k]:
                last = k
            k += 1
        out.append((i, last))
        i = last + 1
    return out


def _refine(P, i, Q, j, span=3):
    """The exact nearest points of the two short stretches of the sampled lines around P[i] and Q[j]: (distance, point on P, point on Q)."""
    a = P[max(0, i - span):i + span + 1]
    b = Q[max(0, j - span):j + span + 1]
    d, pairs, _info = Part.makePolygon(a).distToShape(Part.makePolygon(b))
    return d, pairs[0][0], pairs[0][1]


def _common(a, b):
    """The volume two solids share, or None. A little fuzziness makes OpenCascade cope with wires whose axes cross exactly."""
    for tol in (1e-3, 0.0):
        try:
            c = a.common(b, tol) if tol else a.common(b)
            if abs(c.Volume) > 1e-6:
                return c
        except Exception:
            pass
    return None


def _attach_overlaps(found, common):
    """Give every overlap in `found` the piece of the common volume that lies at it."""
    if common is None:
        return
    pieces = [s for s in common.Solids if abs(s.Volume) > 1e-6]
    if not pieces:
        return
    for c in found:
        if c["state"] == "overlap":
            piece = min(pieces, key=lambda s: (s.CenterOfMass - c["point"]).Length)
            c["shape"], c["volume"] = piece, abs(piece.Volume)


def _other_parts(doc, harness):
    """The solids in the document other than the wires: the connector, for instance."""
    out = []
    for o in doc.Objects:
        if o is harness or o.Name == MARKER_NAME or not o.isDerivedFrom("Part::Feature"):
            continue
        if type(getattr(o, "Proxy", None)).__name__ in ("WireHarness", "HarnessCoverings"):
            continue
        sh = getattr(o, "Shape", None)
        if sh is None or sh.isNull() or not sh.Solids:
            continue
        if hasattr(o, "Visibility") and not o.Visibility:
            continue
        out.append(o)
    return out


def _wire_items(harness, step=_COLLISION_STEP, twisted=False):
    """Every wire of the harness that can be built, with its centre line (placed in the document) sampled every `step` mm.
    With `twisted` the wires that are twisted have their twisted centre line (otherwise the straight path they twist around).

    Returns (items, ids of the wires that could not be built). An item: w (the wire dict), id, r (radius), line (the
    centre line as a Part wire), len, pts (the samples, equally spaced along the wire) and solid (None until needed)."""
    wires, _errors = read_harness(harness.Params, [s.Name for s in harness.PointSheets])
    base = harness.Placement
    paths = twist_paths(harness.Document, harness, wires)[0] if twisted else {}
    items, skipped = [], []
    for w in wires:
        try:
            if "error" in w:
                raise ValueError(w["error"])
            tp = paths.get(w["id"])
            line = Part.makePolygon(tp["pts"]) if tp is not None else Part.Wire(spline_path(w)[0])
            line.Placement = base * FreeCAD.Placement(Vector(w["x"], w["y"], w["z"]), FreeCAD.Rotation())
            n = max(2, int(math.ceil(line.Length / step)) + 1)
            items.append({"w": w, "id": w["id"], "r": w["od"] / 2.0, "line": line, "len": line.Length,
                          "pts": line.discretize(Number=n), "solid": None, "twist": tp})
        except Exception:
            skipped.append(w["id"])
    return items, skipped


# ---------------------------------------------------------------------------
# Twisted wires
# ---------------------------------------------------------------------------

TWIST_SHEET = "TwistTable"
TWIST_FIRST_ROW = 5
_TWIST_LAY = 25.0            # mm per full turn when nothing else is typed
_TWIST_STEP = 0.25           # mm between the points the wires are sampled at
_TWIST_GAP = 0.05            # mm of room left around each twisted wire, so two wires never touch (0.1 mm between them)
_TWIST_SPACING = 1.0         # mm between the knots of the swept centre line (closer when the lay is short)
_TWIST_APART = 15.0          # wires further apart than this cannot be twisted together
_TWIST_NOTE = ("One row per twist (2 or more wires that wind around each other). Wires: the wires that are twisted, separated by "
               "commas; the first one is the reference for From and Length. From: where the twist starts, in mm along the reference "
               "wire from its start. Length from start: how long the twist is, in mm along the reference wire counted from From "
               "(blank = until the end of the wire). Lay length: how far along the wires one full turn is, in mm. Hand: Right or Left. "
               "The wires keep their ends and lengths; a twisted wire needs a little more wire than the straight path, shown in the "
               "Harness Designer window and in the wire report. A wire can be in one twist only. Blank rows are skipped.")


def write_twist_headers(sh):
    sh.set("A1", "Wire harness - twisted wires table")
    sh.set("A2", _TWIST_NOTE)
    for col, head in zip("ABCDEF", ["Twist", "Wires (comma separated)", "From (mm)", "Length from start (mm)",
                                    "Lay length (mm per turn)", "Hand (Right / Left)"]):
        sh.set("%s4" % col, head)
    try:
        for col, width in zip("ABCDEF", [80, 170, 90, 150, 160, 130]):
            sh.setColumnWidth(col, width)
    except Exception:
        pass


def read_twists(sheet):
    """Return (twists, problems) from the twisted wires table.

    A twist is a dict: id, row, wires (list of ids), from, to (None = the end of the wire), lay (mm per turn), hand (1 = right,
    -1 = left). "to" is the position along the first wire where the twist ends: the table holds its length from "from"."""
    out, problems, seen = [], [], set()
    for row in range(TWIST_FIRST_ROW, TWIST_FIRST_ROW + SCAN_ROWS):
        tid = _ident(sheet, "A%d" % row)
        if tid == "":
            continue
        if tid in seen:
            problems.append("Twist %s appears more than once" % tid)
            continue
        seen.add(tid)
        text = _contents(sheet, "B%d" % row).lstrip("'")
        start = _opt(sheet, "C%d" % row, 0.0)
        span = _opt(sheet, "D%d" % row, None)
        out.append({"id": tid, "row": row,
                    "wires": [x for x in re.split(r"[,; ]+", text) if x],
                    "from": start, "to": start + span if span else None,
                    "lay": _opt(sheet, "E%d" % row, _TWIST_LAY),
                    "hand": -1 if _contents(sheet, "F%d" % row).lstrip("'").strip().lower().startswith("l") else 1})
    return out, problems


def _write_twist_row(sh, row, t):
    """Write a twist; t["to"] is the position where it ends, the table gets its length from t["from"]."""
    sh.set("A%d" % row, str(t["id"]))
    sh.set("B%d" % row, "'" + ", ".join(str(w) for w in t["wires"]))
    sh.set("C%d" % row, _fmt(t["from"]))
    if t.get("to"):
        sh.set("D%d" % row, _fmt(max(t["to"] - t["from"], 0.1)))
    else:
        sh.clear("D%d" % row)
    sh.set("E%d" % row, _fmt(t["lay"]))
    sh.set("F%d" % row, "'Left" if t["hand"] < 0 else "'Right")


def twist_sheet_of(harness):
    """The twisted wires table of a harness, or None if it has none yet."""
    try:
        return harness.Twists if "Twists" in harness.PropertiesList else None
    except Exception:
        return None


def ensure_twist_sheet(doc, harness):
    sheet = twist_sheet_of(harness)
    if sheet is not None:
        return sheet
    sheet = doc.getObject(TWIST_SHEET)
    if sheet is None:
        sheet = doc.addObject("Spreadsheet::Sheet", TWIST_SHEET)
        write_twist_headers(sheet)
    if "Twists" not in harness.PropertiesList:
        harness.addProperty("App::PropertyLink", "Twists", "Harness", "The twisted wires table")
    harness.Twists = sheet
    return sheet


def harness_twists(harness):
    sheet = twist_sheet_of(harness)
    return read_twists(sheet) if sheet is not None else ([], [])


_TWIST_BASE = {}             # (document, wire) -> (what it was made from, the wire sampled every _TWIST_STEP mm)
_TWIST_CACHE = {}            # (document, twist) -> (what it was made from, result)


def _twist_item(doc, w):
    """The wire sampled for twisting: its centre line (without any twist) as points in the harness's own coordinates."""
    key, where = repr(w), (doc.Name, w["id"])
    kept = _TWIST_BASE.get(where)
    if kept is not None and kept[0] == key:
        return kept[1]
    line = Part.Wire(spline_path(w)[0])
    n = max(2, int(math.ceil(line.Length / _TWIST_STEP)) + 1)
    S = Vector(w["x"], w["y"], w["z"])
    item = {"id": w["id"], "w": w, "r": w["od"] / 2.0, "len": line.Length, "S": S, "pts": [p + S for p in line.discretize(Number=n)]}
    if len(_TWIST_BASE) > 200:
        _TWIST_BASE.clear()
    _TWIST_BASE[where] = (key, item)
    return item


def _smoothstep(x):
    x = max(0.0, min(1.0, x))
    return x * x * (3.0 - 2.0 * x)


def _twist_group(items, t):
    """Twist the wires of twist `t` (items: wire id -> _twist_item). Returns (paths, info).

    The wires wind around the line midway between them, from t["from"] to t["to"] mm along the first wire. Before and after, and
    while they move in and out of the twist, they keep their own path. paths: wire id -> {"pts": the new centre line (from the wire's
    start), "extra": mm of wire the twist needs on top of the straight path, "zone": (arc where the twist begins, where it ends),
    "group", "key"}. Raises ValueError if the wires cannot be twisted there."""
    sel = [items[i] for i in t["wires"]]
    ref = sel[0]
    count = len(sel)
    s0 = max(0.0, t["from"])
    s1 = ref["len"] if t["to"] is None else min(t["to"], ref["len"])
    if s1 - s0 < 1.0:
        raise ValueError("it must be at least 1 mm long (wire %s is %.1f mm long)" % (ref["id"], ref["len"]))
    lay = t["lay"]
    if lay < 1.0:
        raise ValueError("the lay length must be at least 1 mm per turn")
    R, arcs, columns, beyond = _covering_columns(sel, s0, s1)
    P = ref["pts"]
    step = ref["len"] / (len(P) - 1)
    ks = [k for k in range(len(P)) if s0 + 1e-9 < k * step < s1 - 1e-9]
    if not ks:
        raise ValueError("it must be at least 1 mm long")
    idx = [[ks[0]] + ks + [ks[-1]]] + [m["_idx"] for m in sel[1:]]
    good = [k for k in range(len(R)) if all(b[k] <= 0.0 for b in beyond)]
    if len(good) < 4:
        raise ValueError("the wires do not run side by side there")
    k0, k1 = good[0], good[-1]
    columns = [c[k0:k1 + 1] for c in columns]
    idx = [x[k0:k1 + 1] for x in idx]
    arcs = arcs[k0:k1 + 1]
    n = len(columns[0])
    A = [sum((c[k] for c in columns), Vector()) * (1.0 / count) for k in range(n)]
    apart = max((c[k] - A[k]).Length for c in columns for k in range(n))
    if apart > _TWIST_APART:
        raise ValueError("the wires are up to %.0f mm apart there - wires must run side by side to be twisted" % (2 * apart))
    U = [0.0]
    for k in range(1, n):
        U.append(U[-1] + (A[k] - A[k - 1]).Length)
    total = U[-1]
    if total < 1.0:
        raise ValueError("the twist is shorter than 1 mm")
    m = max(1, int(round(1.0 / _TWIST_STEP)))
    T = []
    for k in range(n):
        d = A[min(n - 1, k + m)] - A[max(0, k - m)]
        T.append(_unit(d) if d.Length > 1e-9 else (T[-1] if T else Vector(0, 0, 1)))
    o = columns[0][0] - A[0]
    o = o - T[0] * o.dot(T[0])
    if o.Length > 1e-6:
        n0 = _unit(o)
    else:
        a = Vector(1, 0, 0) if abs(T[0].x) < 0.9 else Vector(0, 1, 0)
        n0 = _unit(a - T[0] * a.dot(T[0]))
    N, B = [n0], [T[0].cross(n0)]
    for k in range(1, n):
        v = N[-1] - T[k] * N[-1].dot(T[k])
        v = _unit(v) if v.Length > 1e-9 else N[-1]
        N.append(v)
        B.append(T[k].cross(v))
    rel = {}
    for i in range(1, count):                             # where each wire sits around the middle at the start of the twist
        oi = columns[i][0] - A[0]
        rel[i] = math.atan2(oi.dot(B[0]), oi.dot(N[0])) % (2 * math.pi) if oi.Length > 1e-6 else 2 * math.pi * i / count
    phi = {0: 0.0}
    for rank, i in enumerate(sorted(rel, key=rel.get), 1):
        phi[i] = 2 * math.pi * rank / count
    dmax = max(it["w"]["od"] for it in sel)
    rho = (dmax / 2.0 + _TWIST_GAP) / math.sin(math.pi / count)
    ramp = min(max(0.5 * lay, 3.0), 0.5 * total)
    weight = [_smoothstep(u / ramp) * _smoothstep((total - u) / ramp) for u in U]
    theta = [t["hand"] * 2 * math.pi * u / lay for u in U]
    # Every wire goes round the middle line at some distance and angle. Moving into the twist the distance shrinks to rho and the
    # wires settle on the ring, turning with the twist; moving out they go back to their own distance and angle. The angles of the
    # wires are blended together with that of the first one, so two wires never cut across each other on the way in or out.
    two_pi = 2 * math.pi
    polar = []
    for i in range(count):
        row = []
        for k in range(n):
            o = columns[i][k] - A[k]
            x, y = o.dot(N[k]), o.dot(B[k])
            row.append((math.hypot(x, y), math.atan2(y, x), o.dot(T[k])))
        polar.append(row)
    a0, prev = [], 0.0
    for k in range(n):
        r, a, _ax = polar[0][k]
        if r > 1e-6:
            if k > 0:
                a += two_pi * round((prev - a) / two_pi)               # keep it continuous
            prev = a
        a0.append(prev)
    rel = [[0.0] * n]
    for i in range(1, count):
        slot = phi[i] - phi[0]
        row = []
        for k in range(n):
            if polar[i][k][0] > 1e-6 and polar[0][k][0] > 1e-6:
                dif = (polar[i][k][1] - polar[0][k][1]) - slot
                row.append(slot + dif - two_pi * round(dif / two_pi))
            else:
                row.append(slot)
        rel.append(row)
    k_mid = max(range(n), key=weight.__getitem__)                      # where the twist is fully on: the turns are counted afresh
    shift_in = two_pi * round((a0[0] - theta[0]) / two_pi)
    shift_out = two_pi * round((a0[-1] - theta[-1]) / two_pi)
    def lay_out(scale):
        news = [[] for _i in range(count)]
        for k in range(n):
            w_ = weight[k]
            ang0 = (1.0 - w_) * a0[k] + w_ * (theta[k] + (shift_in if k <= k_mid else shift_out))
            for i in range(count):
                angle = ang0 + (1.0 - w_) * rel[i][k] + w_ * (phi[i] - phi[0])
                radius = (1.0 - w_) * polar[i][k][0] + w_ * rho * scale
                news[i].append(A[k] + N[k] * (radius * math.cos(angle)) + B[k] * (radius * math.sin(angle)) +
                               T[k] * ((1.0 - w_) * polar[i][k][2]))
        return news

    def too_close(news):
        """The first two wires that come closer to each other than they are now, or touch (None if there are none)."""
        for a in range(count):
            for b in range(a + 1, count):
                need = sel[a]["r"] + sel[b]["r"]
                for k in range(n):
                    lo, hi = max(0, k - 8), min(n, k + 9)
                    if (min((news[a][k] - news[b][j]).Length for j in range(lo, hi)) <
                            min(need, min((columns[a][k] - columns[b][j]).Length for j in range(lo, hi))) - 1e-3):
                        return sel[a]["id"], sel[b]["id"]
        return None

    for scale in (1.0, 1.1, 1.25, 1.5):                    # moving in and out, wires can dip closer than at either end: give them room
        news = lay_out(scale)
        clash = too_close(news)
        if clash is None:
            break
    if clash is not None:
        raise ValueError("wires %s and %s would come too close to each other where they move into the twist - start the twist "
                         "somewhere the wires run straight, or use a longer lay length" % clash)
    rho *= scale
    paths, extra = {}, {}
    for i, it in enumerate(sel):
        new = news[i]
        base = it["pts"]
        before, after = base[:idx[i][0]], base[idx[i][-1] + 1:]
        whole = []
        for p in before + new + after:
            if not whole or (p - whole[-1]).Length > 1e-6:
                whole.append(p)
        z0 = _path_length(before + new[:1])
        extra[it["id"]] = _path_length(new) - _path_length(columns[i])
        paths[it["id"]] = {"pts": [p - it["S"] for p in whole], "extra": extra[it["id"]], "zone": (z0, z0 + _path_length(new)),
                           "group": t["id"], "mates": [x["id"] for x in sel if x is not it]}
    key = hash((tuple(t["wires"]), t["from"], t["to"], lay, t["hand"], tuple(_wire_key(it) for it in sel)))
    for v in paths.values():
        v["key"] = key
        v["lay"] = lay
    q = lay / (2 * math.pi)
    info = {"from": arcs[0], "to": arcs[-1], "asked_to": s1, "length": total, "turns": total / lay, "lay": lay, "rho": rho,
            "need": rho + dmax / 2.0, "tight": (rho * rho + q * q) / rho, "extra": extra, "wires": list(t["wires"]), "key": key,
            "min_radius": max(it["w"]["radius"] for it in sel)}
    return paths, info


def _twist_compute(doc, by_id, t, used, cache=True):
    """One twist: checks it and works it out (kept until something it is made from changes). Returns (paths, info)."""
    if len(t["wires"]) < 2:
        raise ValueError("a twist needs at least 2 wires")
    if len(set(t["wires"])) < len(t["wires"]):
        raise ValueError("a wire is listed twice")
    missing = [i for i in t["wires"] if i not in by_id]
    if missing:
        raise ValueError("wire %s does not exist or cannot be built" % ", ".join(missing))
    clash = [i for i in t["wires"] if i in used]
    if clash:
        raise ValueError("wire %s is already twisted in twist %s - a wire can be in one twist only" % (clash[0], used[clash[0]]))
    items = {i: _twist_item(doc, by_id[i]) for i in t["wires"]}
    key = hash((tuple(t["wires"]), t["from"], t["to"], t["lay"], t["hand"], tuple(_wire_key(items[i]) for i in t["wires"])))
    where = (doc.Name, t["id"])
    kept = _TWIST_CACHE.get(where) if cache else None
    if kept is not None and kept[0] == key:
        return kept[1]
    result = _twist_group(items, t)
    if cache:
        _TWIST_CACHE[where] = (key, result)
    return result


def twist_paths(doc, harness, wires=None):
    """The twists of a harness worked out: (paths, info, problems).

    paths: wire id -> its twisted centre line (see _twist_group). info: twist id -> a dict (it works) or the text of what is wrong.
    problems: lines about the table itself."""
    sheet = twist_sheet_of(harness)
    if sheet is None:
        return {}, {}, []
    if wires is None:
        wires = read_harness(harness.Params, [s.Name for s in harness.PointSheets])[0]
    twists, problems = read_twists(sheet)
    by_id = {w["id"]: w for w in wires if "error" not in w}
    paths, info, used = {}, {}, {}
    for t in twists:
        try:
            got, text = _twist_compute(doc, by_id, t, used)
        except Exception as e:
            info[t["id"]] = str(e)
            continue
        paths.update(got)
        info[t["id"]] = text
        for i in t["wires"]:
            used[i] = t["id"]
    gone = {(doc.Name, t["id"]) for t in twists}
    for k in [k for k in _TWIST_CACHE if k[0] == doc.Name and k not in gone]:
        del _TWIST_CACHE[k]
    return paths, info, problems


def twist_conflicts(doc, harness, t, paths, by_id):
    """The wires outside twist `t` that its twisted wires (`paths`) would touch: [(twisted wire, other wire, mm along the twisted wire,
    gap in mm)]. Only what the twist makes new is listed: wires that were that close before are left out."""
    now = twist_paths(doc, harness, list(by_id.values()))[0]
    members = set(t["wires"])
    gap = max(0.0, _setting("CollisionGap", 0.0))
    found = []
    for wid, tp in paths.items():
        m = by_id[wid]
        mine = _twist_item(doc, m)
        P = [p + mine["S"] for p in tp["pts"]]
        for oid, ow in by_id.items():
            if oid in members:
                continue
            other = _twist_item(doc, ow)
            otp = now.get(oid)
            Q = [p + other["S"] for p in otp["pts"]] if (otp is not None and otp["group"] != t["id"]) else other["pts"]
            need = mine["r"] + other["r"] + gap
            dist = _nearest_samples(P, Q)[0]
            k = min(range(len(dist)), key=dist.__getitem__)
            if dist[k] < need - 1e-3 and min(_nearest_samples(mine["pts"], other["pts"])[0]) >= need - 1e-3:
                found.append((wid, oid, _path_length(P[:k + 1]), dist[k] - need))
    return found


def twist_check(doc, harness, t):
    """Work out twist `t` (a dict as read_twists makes them) without changing anything. Returns its info (see _twist_group), or raises
    ValueError saying why it cannot be made - also when its wires would touch other wires."""
    wires = read_harness(harness.Params, [s.Name for s in harness.PointSheets])[0]
    by_id = {w["id"]: w for w in wires if "error" not in w}
    used = {i: o["id"] for o in harness_twists(harness)[0] if o["id"] != t["id"] for i in o["wires"]}
    paths, info = _twist_compute(doc, by_id, t, used, cache=False)
    bad = twist_conflicts(doc, harness, t, paths, by_id)
    if bad:
        raise ValueError("twisting would make wire %s touch wire %s (%.1f mm along wire %s) - twist the wires that lie next to them "
                         "too, or move the wires apart%s" % (bad[0][0], bad[0][1], bad[0][2], bad[0][0],
                                                              "" if len(bad) == 1 else " (and %d more places)" % (len(bad) - 1)))
    return info


def twist_title(t):
    where = "%s - %s mm" % (_fmt(t["from"]), _fmt(t["to"])) if t["to"] else "%s mm - end" % _fmt(t["from"])
    return "%s: wires %s (%s), %s mm per turn, %s" % (t["id"], ", ".join(t["wires"]), where, _fmt(t["lay"]),
                                                      "left-hand" if t["hand"] < 0 else "right-hand")


def twist_text(info):
    """A short description of a twist that works (a dict from twist_paths)."""
    text = "Twisted over %.1f mm (%.1f turns of %s mm). Extra wire needed: %s." % (
        info["to"] - info["from"], info["turns"], _fmt(info["lay"]),
        ", ".join("wire %s %+.2f mm" % (i, info["extra"][i]) for i in info["wires"]))
    if info["asked_to"] - info["to"] > 0.5:
        text += " The twist stops at %.1f mm because the wires stop running side by side there." % info["to"]
    if info["min_radius"] > 0 and info["tight"] < 0.98 * info["min_radius"]:
        text += (" WARNING: the wires turn at a radius of %.1f mm there, below their %.1f mm minimum bend radius - use a longer "
                 "lay length." % (info["tight"], info["min_radius"]))
    return text


def next_twist_id(harness):
    used = {t["id"] for t in harness_twists(harness)[0]}
    k = 1
    while str(k) in used:
        k += 1
    return str(k)


def _twist_error(harness, tid):
    """The error text of a twist that could not be made, or None."""
    prefix = "Twist %s: ERROR - " % tid
    for line in harness.WireReport:
        if line.startswith(prefix):
            return line[len(prefix):]
    return None


def _check_twist(harness, tid, undo):
    error = _twist_error(harness, tid)
    if error is None and not any(l.startswith("Twist %s: " % tid) for l in harness.WireReport):
        error = "it was not made: the wires of the harness cannot be built right now (see the wire report), so fix them first"
    if error is not None:
        undo()
        raise ValueError("Twist %s cannot be made: %s. Nothing was changed." % (tid, error))


def add_twist(doc, harness, wires, start=0.0, end=None, lay=_TWIST_LAY, hand=1, tid=None):
    """Twist `wires` from `start` to `end` mm along the first of them (end None = to the end of the wire)."""
    wires = [str(w) for w in wires]
    if len(wires) < 2:
        raise ValueError("choose at least 2 wires to twist")
    known = {w["id"] for w in read_harness(harness.Params)[0]}
    if [w for w in wires if w not in known]:
        raise ValueError("wire %s does not exist" % ", ".join(w for w in wires if w not in known))
    tid = str(tid) if tid is not None else next_twist_id(harness)
    if any(t["id"] == tid for t in harness_twists(harness)[0]):
        raise ValueError("twist %s already exists" % tid)
    twist_check(doc, harness, {"id": tid, "wires": wires, "from": start, "to": end, "lay": lay, "hand": hand})
    sheet = ensure_twist_sheet(doc, harness)
    row = TWIST_FIRST_ROW
    while any(_contents(sheet, "%s%d" % (c, row)) != "" for c in "ABCDEF"):
        row += 1
    _write_twist_row(sheet, row, {"id": tid, "wires": wires, "from": start, "to": end, "lay": lay, "hand": hand})
    doc.recompute()

    def undo():
        for col in "ABCDEF":
            sheet.clear("%s%d" % (col, row))
        doc.recompute()
    _check_twist(harness, tid, undo)
    return tid


def update_twist(doc, harness, tid, wires, start, end, lay, hand):
    """Change a twist; if it cannot be made with the new values, the old ones are put back."""
    found = [t for t in harness_twists(harness)[0] if t["id"] == str(tid)]
    if not found:
        raise ValueError("twist %s does not exist" % tid)
    old, sheet = found[0], twist_sheet_of(harness)
    twist_check(doc, harness, {"id": old["id"], "wires": [str(w) for w in wires], "from": start, "to": end, "lay": lay, "hand": hand})
    _write_twist_row(sheet, old["row"], {"id": old["id"], "wires": [str(w) for w in wires], "from": start, "to": end, "lay": lay,
                                         "hand": hand})
    doc.recompute()
    _check_twist(harness, old["id"], lambda: (_write_twist_row(sheet, old["row"], old), doc.recompute()))


def remove_twist(doc, harness, tid):
    found = [t for t in harness_twists(harness)[0] if t["id"] == str(tid)]
    if not found:
        raise ValueError("twist %s does not exist" % tid)
    for col in "ABCDEF":
        twist_sheet_of(harness).clear("%s%d" % (col, found[0]["row"]))
    doc.recompute()


def _drop_wire_from_twists(harness, wid):
    """A wire was deleted: take it out of the twists; a twist left with fewer than 2 wires is removed."""
    sheet = twist_sheet_of(harness)
    if sheet is None:
        return
    for t in read_twists(sheet)[0]:
        if str(wid) in t["wires"]:
            rest = [w for w in t["wires"] if w != str(wid)]
            if len(rest) >= 2:
                sheet.set("B%d" % t["row"], "'" + ", ".join(rest))
            else:
                for col in "ABCDEF":
                    sheet.clear("%s%d" % (col, t["row"]))


class _CoverPart:
    """One covering as a solid to test the wires against (it looks like a document object: Label and Shape)."""

    def __init__(self, cid, shape, members=()):
        self.cid, self.Label, self.Shape = cid, "Covering %s" % cid, shape
        self.members = set(members)                 # the wires the covering is built around: they are not tested against it

def _covering_parts(doc):
    """The coverings that are built now, one _CoverPart each. Wires are always checked against them."""
    obj = find_coverings(doc)
    if obj is None or obj.Shape.isNull() or not obj.Shape.Solids:
        return []
    ids = []
    for line in obj.CoveringReport:
        m = re.match(r"Covering (.+?): (ERROR - )?", line)
        if m and not m.group(2):
            ids.append(m.group(1))
    solids = obj.Shape.Solids
    if len(ids) != len(solids):
        ids = ["" for _s in solids]
    wires = {c["id"]: c["wires"] for c in read_coverings(obj.Params)[0]}
    return [_CoverPart(cid, s, wires.get(cid, ())) for cid, s in zip(ids, solids)]

_PART_CACHE = {}               # (document, wire, part) -> what was found there, and what it was found for
_PAIR_CACHE = {}               # (document, wire, wire) -> what was found between them, and what it was found for

def find_collisions(doc, harness, min_gap=0.0, parts=(), step=_COLLISION_STEP):
    """Where the wires of the harness touch, overlap or come closer to each other than `min_gap` mm.

    Every wire is a tube, so two wires collide when their centre lines are closer than the sum of their radii (plus the
    gap). The centre lines are sampled every `step` mm, every stretch where two of them are too close is one collision, and
    its nearest point is worked out exactly. `parts` (objects with a solid shape, see _other_parts) are tested for overlap only.

    Returns {"collisions": [...], "closest": {...} or None, "skipped": [wire ids], "wires": count, "min_gap": ...}.
    A collision is a dict: kind ("wires" or "part"), a, b, state ("overlap", "touch" or "close"), gap (mm, negative = how
    deep they overlap), point, pa, pb, a_at / b_at (mm along the wire), length, volume and shape (the overlapping solid)."""
    items, skipped = _wire_items(harness, step, True)
    base = harness.Placement

    def solid(it):
        if it["solid"] is None:
            shape, _note = build_spline_shape(it["w"], it.get("twist"))
            shape.Placement = base * FreeCAD.Placement(Vector(it["w"]["x"], it["w"]["y"], it["w"]["z"]), FreeCAD.Rotation())
            it["solid"] = shape
        return it["solid"]

    def arc(it, k):
        return k * it["len"] / max(1, len(it["pts"]) - 1)

    gap_limit = max(min_gap, 0.0)
    collisions, closest = [], None
    for ia in range(len(items)):
        for ib in range(ia + 1, len(items)):
            A, B = items[ia], items[ib]
            reach = A["r"] + B["r"]
            near = FreeCAD.BoundBox(A["line"].BoundBox)
            near.enlarge(_NEAR_MM + reach)
            if not near.intersect(B["line"].BoundBox):
                continue
            twin = A.get("twist") is not None and B.get("twist") is not None and A["twist"]["group"] == B["twist"]["group"]
            dist, idx = _nearest_samples(A["pts"], B["pts"])
            i_min = min(range(len(dist)), key=dist.__getitem__)
            if not twin and (closest is None or dist[i_min] - reach < closest["gap"]):
                closest = {"a": A["id"], "b": B["id"], "gap": dist[i_min] - reach, "_at": (ia, ib, i_min, idx[i_min])}
            limit = reach + gap_limit + step + _TOUCH
            found = []
            for i0, i1 in _runs([d < limit for d in dist]):
                i = min(range(i0, i1 + 1), key=dist.__getitem__)
                j = idx[i]
                d, pa, pb = _refine(A["pts"], i, B["pts"], j)
                gap = d - reach
                if gap > gap_limit + _TOUCH:
                    continue
                found.append({"kind": "wires", "a": A["id"], "b": B["id"], "gap": gap, "pa": pa, "pb": pb,
                              "point": (pa + pb) * 0.5, "a_at": arc(A, i), "b_at": arc(B, j),
                              "a_from": arc(A, i0), "a_to": arc(A, i1), "length": arc(A, i1) - arc(A, i0),
                              "volume": None, "shape": None,
                              "state": "overlap" if gap < -_TOUCH else ("touch" if gap <= _TOUCH else "close")})
            if twin:                    # wires twisted together lie close by design: that is not reported (an overlap always is)
                za, zb = A["twist"]["zone"], B["twist"]["zone"]
                found = [c for c in found if c["state"] == "overlap" or
                         not (za[0] - 1.0 <= c["a_at"] <= za[1] + 1.0 and zb[0] - 1.0 <= c["b_at"] <= zb[1] + 1.0)]
            if found:
                # What was found for two wires is kept until one of them (or the gap) changes.
                pk = (doc.Name, A["id"], B["id"])
                keys = (_wire_key(A), _wire_key(B), round(gap_limit, 6))
                kept = _PAIR_CACHE.get(pk)
                if kept is not None and kept["keys"] == keys:
                    collisions.extend(dict(c) for c in kept["found"])
                    continue
                try:
                    sa, sb = solid(A), solid(B)
                    if sa.distToShape(sb)[0] > gap_limit + 1e-4:
                        found = []              # the flat ends of the wires fooled the centre-line test
                    elif any(c["state"] == "overlap" for c in found):
                        _attach_overlaps(found, _common(sa, sb))
                except Exception:
                    pass                        # keep what the centre lines say
                if len(_PAIR_CACHE) > 300:
                    _PAIR_CACHE.clear()
                _PAIR_CACHE[pk] = {"keys": keys, "found": found}
                collisions.extend(dict(c) for c in found)
    for part in list(parts) + _covering_parts(doc):
        try:
            pshape = part.Shape
            pbb = pshape.BoundBox
            for it in items:
                if it["id"] in getattr(part, "members", ()):
                    continue                    # a covering is built around its own wires, so they cannot go through it
                reach = FreeCAD.BoundBox(it["line"].BoundBox)
                reach.enlarge(it["r"] + 0.01)
                if not reach.intersect(pbb):
                    continue                    # the wire cannot even reach the part
                # What was found for this wire and this part is kept until one of them changes.
                ck = (doc.Name, it["id"], part.Label)
                wkey = _wire_key(it)
                kept = _PART_CACHE.get(ck)
                if kept is not None and kept["wkey"] == wkey and kept["pshape"].isSame(pshape):
                    collisions.extend(dict(c) for c in kept["found"])
                    continue
                found = []
                sa = solid(it)
                if sa.BoundBox.intersect(pbb) and sa.distToShape(pshape)[0] <= 1e-4:   # else not even touching: no (slow) overlap volume
                    shared = _common(sa, pshape)
                    for piece in (shared.Solids if shared is not None else []):
                        vol = abs(piece.Volume)
                        if vol < 1e-3:
                            continue
                        pt = piece.CenterOfMass
                        _d, at = _nearest_samples([pt], it["pts"])
                        found.append({"kind": "part", "a": it["id"], "b": part.Label, "gap": None, "pa": pt, "pb": pt,
                                      "point": pt, "a_at": arc(it, at[0]), "b_at": None, "a_from": arc(it, at[0]), "a_to": arc(it, at[0]),
                                      "length": 0.0, "volume": vol, "shape": piece, "state": "overlap",
                                      "covering": isinstance(part, _CoverPart)})
                if len(_PART_CACHE) > 300:
                    _PART_CACHE.clear()
                _PART_CACHE[ck] = {"wkey": wkey, "pshape": pshape, "found": found}
                collisions.extend(dict(c) for c in found)
        except Exception:
            pass
    if closest is not None:
        ia, ib, i, j = closest.pop("_at")
        d, pa, pb = _refine(items[ia]["pts"], i, items[ib]["pts"], j)
        closest["gap"] = d - items[ia]["r"] - items[ib]["r"]
        closest["point"] = (pa + pb) * 0.5
    order = {"overlap": 0, "touch": 1, "close": 2}
    collisions.sort(key=lambda c: (order[c["state"]], c["gap"] if c["gap"] is not None else -c["volume"]))
    return {"collisions": collisions, "closest": closest, "skipped": skipped, "wires": len(items), "min_gap": min_gap}


def collision_title(c):
    other = ("Wire %s" % c["b"]) if c["kind"] == "wires" else c["b"]
    return "Wire %s × %s" % (c["a"], other)


def collision_what(c):
    if c["kind"] == "part":
        size = "%.2f mm³" % c["volume"] if c["volume"] >= 0.01 else "less than 0.01 mm³"
        return ("goes through the covering, overlap " if c.get("covering") else "overlap ") + size
    if c["state"] == "overlap":
        return "overlap %.2f mm" % -c["gap"]
    if c["state"] == "touch":
        return "touching"
    return "gap %.2f mm" % c["gap"]


def collision_detail(c):
    where = ("between %.1f and %.1f mm along wire %s" % (c["a_from"], c["a_to"], c["a"])
             if c["length"] >= 1.0 else "at %.1f mm along wire %s" % (c["a_at"], c["a"]))
    return "%s: %s, %s" % (collision_title(c), collision_what(c), where)


def collision_lines(result):
    """The result as plain text lines."""
    found = result["collisions"]
    lines = []
    if found:
        lines.append("%d collision%s:" % (len(found), "" if len(found) == 1 else "s"))
        lines += [collision_detail(c) for c in found]
    else:
        lines.append("No collisions.")
    near = result["closest"]
    if near is not None and not found:
        lines.append("Closest: wire %s × wire %s, gap %.2f mm." % (near["a"], near["b"], near["gap"]))
    if result["skipped"]:
        lines.append("Not checked (cannot be built): wire %s." % ", ".join(result["skipped"]))
    return lines


def check_collisions(doc=None, min_gap=0.0, parts=False):
    """Check the harness of the document for collisions and print the result in the Report view."""
    doc = doc or FreeCAD.ActiveDocument
    harness = _find_harness(doc)
    result = find_collisions(doc, harness, min_gap, _other_parts(doc, harness) if parts else ())
    for line in collision_lines(result):
        FreeCAD.Console.PrintMessage(line + "\n")
    return result


# ---------------------------------------------------------------------------
# Harness coverings: the sleeve around a bundle of wires
# ---------------------------------------------------------------------------

COVERING_SHEET = "CoveringTable"
COVERING_OBJECT = "Harness_Coverings"
COVERING_FIRST_ROW = 5
_COVER_STEP = 0.25          # mm between the points the wires are sampled at
_COVER_CLEARANCE = 0.25     # mm of room between the wires and an automatic covering
_COVER_FAR = 10.0  # a wire further away than this from the first one is not in the same bundle
_COVER_LIMIT_MIN = 3.5  # wires may be this far apart in a bundle, at least; or 1.5 times as far as they are at the start of the covering
_COVER_LIMIT_GROW = 1.5
_COVER_FADE = 10.0  # after a wire has ended, the covering moves over to the wires that go on, within this many mm
_COVER_ALIGN = 0.5  # ... and neither is one that crosses it: the cosine of the angle between them must be at least this
_COVER_SMOOTH_FIRST = 1.0      # mm: how much the centre line is smoothed first, when the wires bend tighter than the covering is wide
_COVER_SMOOTH_MAX = 40.0      # mm: the most it may be smoothed
_COVER_BEND_MARGIN = 1.25     # a bend must be at least this many times the covering's outer radius, or the tube cannot be built reliably
_COVER_SMOOTH_GROW = 3.0      # mm: the most the covering may get wider than the wires need because of smoothing
_COVER_OPACITY = 0.45  # a covering is drawn see-through, so the wires inside it can be seen           # mm: a wire further than this from the reference wire is probably not in the same bundle


_COVER_LENGTH_HEAD = "Centre-line length (mm) - calculated"
_COVER_LENGTH_NOTE = "Centre-line length: how long the covering is along its centre line, worked out by the program - do not type in it."
_COVER_TO_HEAD = "Length from start (mm)"
_COVER_NOTE = ("One row per covering (the sleeve around a bundle of wires). Wires: the wires it goes around, separated by commas; "
               "the first one is the reference for From and Length. From: where the covering starts, in mm along the reference "
               "wire from its start. Length from start: how long the covering is, in mm along the reference wire counted from "
               "From (blank = until the end of the wire). Inner diameter blank = automatic: the bundle plus a little room. "
               "Wall: the thickness of the covering. Color: a name or #RRGGBB. " + _COVER_LENGTH_NOTE + " Blank rows are skipped.")

def _path_length(pts):
    return sum((b - a).Length for a, b in zip(pts[:-1], pts[1:]))

def _legacy_covering_table(sheet):
    """True while the table still has the old 'To (mm)' column (a position along the wire) instead of the length from the start."""
    return _contents(sheet, "D4").lstrip("'").strip().lower().startswith("to")


def _migrate_covering_table(sheet):
    """Old tables had the position where a covering ends in column D; it is now its length counted from From."""
    if not _legacy_covering_table(sheet):
        return
    for row in range(COVERING_FIRST_ROW, COVERING_FIRST_ROW + SCAN_ROWS):
        try:
            if _contents(sheet, "D%d" % row) == "":
                continue
            end = _opt(sheet, "D%d" % row, None)
            if end:
                sheet.set("D%d" % row, _fmt(max(end - _opt(sheet, "C%d" % row, 0.0), 0.1)))
        except Exception:
            pass                        # something that is not a number stays as it is
    sheet.set("D4", _COVER_TO_HEAD)
    try:
        sheet.setColumnWidth("D", 150)
        sheet.setColumnWidth("H", 230)
    except Exception:
        pass


def _sync_covering_table(sheet, lengths):
    """Keep the calculated Length column of the coverings table up to date: {row: length in mm or None}.

    A cell is only written when its value changes, so this does not touch the table again and again."""
    def put(addr, text):
        have = _contents(sheet, addr).lstrip("'")
        if text == "":
            if have != "":
                sheet.clear(addr)
        elif have != text:
            sheet.set(addr, text)
    try:
        _migrate_covering_table(sheet)
        put("H4", _COVER_LENGTH_HEAD)
        if _contents(sheet, "A2") != "":
            put("A2", _COVER_NOTE)
        for row in range(COVERING_FIRST_ROW, COVERING_FIRST_ROW + SCAN_ROWS):
            length = lengths.get(row)
            have = _contents(sheet, "H%d" % row)
            if length is None:
                if have != "":
                    sheet.clear("H%d" % row)
            else:
                try:
                    same = have != "" and abs(float(have) - length) < 0.005
                except ValueError:
                    same = False
                if not same:
                    sheet.set("H%d" % row, "%.2f" % length)
    except Exception:
        pass

def write_covering_headers(sh):
    sh.set("A1", "Wire harness - coverings table")
    sh.set("A2", _COVER_NOTE)
    for col, head in zip("ABCDEFGH", ["Covering", "Wires (comma separated)", "From (mm)", _COVER_TO_HEAD, "Inner diameter (mm)",
                                      "Wall (mm)", "Color", _COVER_LENGTH_HEAD]):
        sh.set("%s4" % col, head)
    try:
        for col, width in zip("ABCDEFGH", [80, 170, 90, 150, 140, 90, 90, 230]):
            sh.setColumnWidth(col, width)
    except Exception:
        pass


def read_coverings(sheet):
    """Return (coverings, problems) from the coverings table.

    A covering is a dict: id, row, wires (list of ids), from, to (None = the end of the wire), inner (None = automatic),
    wall, color. "to" is the position along the first wire where it ends: the table holds its length from "from" (column D)."""
    out, problems, seen = [], [], set()
    legacy = _legacy_covering_table(sheet)          # an old table: column D is still that position
    for row in range(COVERING_FIRST_ROW, COVERING_FIRST_ROW + SCAN_ROWS):
        cid = _ident(sheet, "A%d" % row)
        if cid == "":
            continue
        if cid in seen:
            problems.append("Covering %s appears more than once" % cid)
            continue
        seen.add(cid)
        text = _contents(sheet, "B%d" % row).lstrip("'")
        start = _opt(sheet, "C%d" % row, 0.0)
        span = _opt(sheet, "D%d" % row, None)
        out.append({"id": cid, "row": row,
                    "wires": [t for t in re.split(r"[,; ]+", text) if t],
                    "from": start,
                    "to": span if (legacy or not span) else start + span,
                    "inner": _opt(sheet, "E%d" % row, None),
                    "wall": _opt(sheet, "F%d" % row, 0.5),
                    "color": _contents(sheet, "G%d" % row).lstrip("'"),
                    "length": _opt(sheet, "H%d" % row, None)})
    return out, problems


def _write_covering_row(sh, row, c):
    """Write a covering; c["to"] is the position where it ends, the table gets its length from c["from"]."""
    _migrate_covering_table(sh)
    sh.set("A%d" % row, str(c["id"]))
    sh.set("B%d" % row, "'" + ", ".join(str(w) for w in c["wires"]))
    sh.set("C%d" % row, _fmt(c["from"]))
    if c.get("to"):
        sh.set("D%d" % row, _fmt(max(c["to"] - c["from"], 0.1)))
    else:
        sh.clear("D%d" % row)
    if c.get("inner"):
        sh.set("E%d" % row, _fmt(c["inner"]))
    else:
        sh.clear("E%d" % row)
    sh.set("F%d" % row, _fmt(c["wall"]))
    if c.get("color"):
        sh.set("G%d" % row, "'" + c["color"])
    else:
        sh.clear("G%d" % row)


def _foot_point(P, j, q):
    """The point of the sampled line P nearest to q, looking at the two segments next to P[j]."""
    best, bd = P[j], (P[j] - q).Length
    for a, b in ((j - 1, j), (j, j + 1)):
        if a < 0 or b >= len(P):
            continue
        s = P[b] - P[a]
        n2 = s.dot(s)
        t = 0.0 if n2 < 1e-12 else max(0.0, min(1.0, (q - P[a]).dot(s) / n2))
        pt = P[a] + s * t
        d = (pt - q).Length
        if d < bd:
            best, bd = pt, d
    return best


def _axis_edges(A, spacing=3.0, lines=False):
    """A smooth chain of cubic Bezier curves (like the wires themselves) through the dense axis points A.

    Returns (edges, start direction). Sweeping along these is reliable, along one long interpolated spline it is not.
    With `lines` a stretch that is straight is a line segment (a sweep along a Bezier curve that is a straight line can fail)."""
    n = len(A)
    acc = [0.0]
    for k in range(1, n):
        acc.append(acc[-1] + (A[k] - A[k - 1]).Length)
    total = acc[-1]
    segs = max(1, int(round(total / spacing)))
    knots, k = [0], 0
    for i in range(1, segs + 1):
        goal = total * i / segs
        while k < n - 1 and acc[k] < goal - 1e-9:
            k += 1
        if k > knots[-1]:
            knots.append(k)
    if knots[-1] != n - 1:
        knots.append(n - 1)
    w = max(1, int(round(0.75 / max(total / (n - 1), 1e-6))))
    tangents = []
    for k in knots:
        d = A[min(n - 1, k + w)] - A[max(0, k - w)]
        tangents.append(_unit(d))
    edges, run = [], None                                   # run: where the straight stretch that is being built began
    for (k0, k1), t0, t1 in zip(zip(knots[:-1], knots[1:]), tangents[:-1], tangents[1:]):
        p0, p1 = A[k0], A[k1]
        d = (p1 - p0).Length
        if lines and d > 1e-9:
            chord = _unit(p1 - p0)
            if all((c - p0 - chord * (c - p0).dot(chord)).Length < 0.003 for c in (p0 + t0 * (d / 3.0), p1 - t1 * (d / 3.0))):
                if run is not None:
                    whole = _unit(p1 - run)
                    if (p0 - run - whole * (p0 - run).dot(whole)).Length < 0.005:       # still the same straight line: one piece
                        edges.pop()
                        edges.append(Part.LineSegment(run, p1).toShape())
                        continue
                run = p0
                edges.append(Part.LineSegment(p0, p1).toShape())
                continue
        run = None
        bz = Part.BezierCurve()
        bz.setPoles([p0, p0 + t0 * (d / 3.0), p1 - t1 * (d / 3.0), p1])
        edges.append(bz.toShape())
    return edges, tangents[0]


def _extended(P, j, q, foot):
    """(the point of wire P that goes with q, how far past the end of the wire that is in mm, 0 on the wire itself).

    Where the covering goes past the end of a (shorter) wire, the wire is taken to go on straight for a while, so the
    covering moves over to the other wires smoothly instead of jumping."""
    n = len(P)
    if n < 2:
        return foot, 0.0
    if j >= n - 1:
        d = P[-1] - P[max(0, n - 4)]
        if d.Length > 1e-9:
            d = d * (1.0 / d.Length)
            t = (q - P[-1]).dot(d)
            if t > 0:
                return P[-1] + d * t, t
    elif j <= 0:
        d = P[0] - P[min(n - 1, 3)]
        if d.Length > 1e-9:
            d = d * (1.0 / d.Length)
            t = (q - P[0]).dot(d)
            if t > 0:
                return P[0] + d * t, t
    return foot, 0.0

def _follow_samples(R, Q, ahead=12):
    """For every point of R the index of the matching sample of Q, always going forward along Q.

    The first point of R gets the nearest sample overall, each next one is looked for a little way ahead only. The nearest point
    on its own would jump to another leg of the path where the path turns back on itself."""
    try:
        import numpy as np
    except ImportError:
        return _nearest_samples(R, Q)[1]
    q = np.array([(p.x, p.y, p.z) for p in Q])
    r = np.array([(p.x, p.y, p.z) for p in R])
    j = int(((q - r[0]) ** 2).sum(axis=1).argmin())
    out = [j]
    for k in range(1, len(r)):
        hi = min(len(q), j + ahead + 1)
        j += int(((q[j:hi] - r[k]) ** 2).sum(axis=1).argmin())
        out.append(j)
    return out

def _covering_columns(sel, s0, s1):
    """The samples R of the first wire between s0 and s1 mm, their arc positions, and for every wire the points of it that are
    nearest to R (the first wire is R itself)."""
    ref = sel[0]
    P = ref["pts"]
    n = len(P)
    step = ref["len"] / (n - 1)

    def at(s):
        k = min(n - 2, max(0, int(s / step)))
        return P[k] + (P[k + 1] - P[k]) * ((s - k * step) / step)

    ks = [k for k in range(n) if s0 + 1e-9 < k * step < s1 - 1e-9]
    R = [at(s0)] + [P[k] for k in ks] + [at(s1)]
    arcs = [s0] + [k * step for k in ks] + [s1]
    columns, beyond = [R], [[0.0] * len(R)]
    for it in sel[1:]:
        idx = _follow_samples(R, it["pts"])
        both = [_extended(it["pts"], j, q, _foot_point(it["pts"], j, q)) for q, j in zip(R, idx)]
        columns.append([b[0] for b in both])
        beyond.append([b[1] for b in both])
        it["_idx"] = idx
    return R, arcs, columns, beyond

def _axis_raw(sel, s0, s1):
    """The centre line of a covering around the wires `sel` from s0 to s1 mm along the first wire (dense points), the points of
    every wire next to it, and the largest distance of another wire from the first."""
    R, _arcs, columns, beyond = _covering_columns(sel, s0, s1)
    apart = max([0.0] + [max((f - q).Length for f, q, e in zip(col, R, ex) if e <= 0.0) for col, ex in zip(columns[1:], beyond[1:])
                         if any(e <= 0.0 for e in ex)])
    axis = []
    for k in range(len(R)):
        total, acc = 0.0, Vector()
        for col, ex in zip(columns, beyond):
            w = max(0.0, 1.0 - ex[k] / _COVER_FADE)
            total += w
            acc += col[k] * w
        axis.append(acc * (1.0 / total))
    return axis, columns, beyond, apart

def _axis_need(sel, columns, beyond, axis):
    """The inner radius the wires need around this centre line."""
    return max((col[k] - axis[k]).Length + it["r"] for col, ex, it in zip(columns, beyond, sel) for k in range(len(axis)) if ex[k] <= 0.0)

def covering_axis(sel, s0, s1):
    """The centre line of a covering around the wires `sel` (items of _wire_items) from s0 to s1 mm along the first wire.

    At every point of the first wire the covering is centred between it and the nearest points of the other wires.
    Returns (dense points of the axis, the inner radius the wires need, the largest distance of another wire from the first)."""
    axis, columns, beyond, apart = _axis_raw(sel, s0, s1)
    return axis, _axis_need(sel, columns, beyond, axis), apart

def _smooth_axis(A, sigma):
    """The points A smoothed over about `sigma` mm (three passes of a moving average). The two ends stay where they are and
    straight stretches stay straight."""
    n = len(A)
    if sigma <= 0 or n < 3:
        return list(A)
    step = _path_length(A) / float(n - 1)
    m = max(1, min(n - 1, int(round(sigma / max(step, 1e-6)))))
    cur = list(A)
    for _ in range(3):
        ext = [cur[0] * 2 - cur[i] for i in range(m, 0, -1)] + cur + [cur[-1] * 2 - cur[n - 1 - i] for i in range(1, m + 1)]
        sums = [[0.0], [0.0], [0.0]]
        for p in ext:
            for c_, v in enumerate((p.x, p.y, p.z)):
                sums[c_].append(sums[c_][-1] + v)
        w = 2 * m + 1
        cur = [Vector(*[(sums[c_][j + w] - sums[c_][j]) / w for c_ in range(3)]) for j in range(n)]
    return cur

class _TooSmall(ValueError):
    """The inner diameter given is smaller than the wires need (the diameter they need is in .need, as a radius)."""
    def __init__(self, text, need):
        ValueError.__init__(self, text)
        self.need = need

def _next_sigma(sigma):
    return _COVER_SMOOTH_FIRST if sigma <= 0 else sigma * 1.6

def covering_fit(sel, s0, s1, inner_d=None, wall=0.5, start=0.0):
    """Everything needed to build a covering: its centre line, the radii and the curves to sweep along.

    Where the wires bend tighter than the covering is wide, the centre line is smoothed (cutting the corners a little, the
    covering gets wider if the wires need that) until a covering of that size can follow it. Raises ValueError if that is not
    possible, _TooSmall if the inner diameter given is too small. `start` is the smoothing to begin with (0: none)."""
    axis, columns, beyond, apart = _axis_raw(sel, s0, s1)
    need0 = _axis_need(sel, columns, beyond, axis)
    sigma, tight0 = start, None
    while True:
        pts = axis if sigma == 0 else _smooth_axis(axis, sigma)
        need = need0 if sigma == 0 else _axis_need(sel, columns, beyond, pts)
        if inner_d and inner_d < 2 * need - 0.005:
            raise _TooSmall("the inner diameter of %.2f mm is too small: the wires need at least %.2f mm over this stretch%s. "
                            "Make it at least %.2f mm, or leave it on automatic"
                            % (inner_d, 2 * need, " (they bend sharply, so the covering has to cut the corners)" if sigma else "",
                               -(-200 * need // 1) / 100.0), need)
        inner = inner_d / 2.0 if inner_d else need + _COVER_CLEARANCE
        outer = inner + wall
        edges, t = _axis_edges(pts)
        tight = _tightest_radius(edges)
        if tight0 is None:
            tight0 = tight
        if tight > outer * _COVER_BEND_MARGIN:
            return {"pts": pts, "need": need, "need0": need0, "apart": apart, "edges": edges, "t": t, "tight": tight, "inner": inner,
                    "outer": outer, "window": sigma}
        nxt = _next_sigma(sigma)
        if nxt > _COVER_SMOOTH_MAX or need - need0 > _COVER_SMOOTH_GROW:
            raise ValueError("the wires bend with a radius of %.1f mm there, too tight for a covering that wide (radius %.1f mm, it "
                             "needs bends of at least %.1f mm) - make the covering narrower or shorter, or bend the wires wider"
                             % (tight0, outer, outer * _COVER_BEND_MARGIN))
        sigma = nxt

def covering_bundle(sel, s0, s1):
    """Do the wires `sel` run together between s0 and s1 mm along the first one?

    Returns {"apart": the largest distance of another wire from the first, "wire": the id of the wire that leaves first (or None
    when they all stay together), "split": where along the first wire it gets further away than _COVER_FAR (or None)}."""
    R, arcs, columns, beyond = _covering_columns(sel, s0, s1)
    last = len(R) - 1
    along = [_unit(R[min(last, k + 1)] - R[max(0, k - 1)]) for k in range(len(R))]
    first = max([0.0] + [(col[0] - R[0]).Length for col, ex in zip(columns[1:], beyond[1:]) if ex[0] <= 0.0])
    limit = min(_COVER_FAR, max(_COVER_LIMIT_MIN, _COVER_LIMIT_GROW * first))
    out = {"apart": 0.0, "wire": None, "split": None, "limit": limit}
    for col, ex, it in zip(columns[1:], beyond[1:], sel[1:]):
        dist = [(f - q).Length for f, q in zip(col, R)]
        out["apart"] = max([out["apart"]] + [d for d, e in zip(dist, ex) if e <= 0.0])
        P = it["pts"]
        for k, (a, d) in enumerate(zip(arcs, dist)):
            if ex[k] > 0.0:
                continue                        # this wire has ended: it cannot leave the bundle any more
            j = it["_idx"][k]
            tw = _unit(P[min(len(P) - 1, j + 1)] - P[max(0, j - 1)])
            if d > limit or abs(tw.dot(along[k])) < _COVER_ALIGN:
                if out["split"] is None or a < out["split"]:
                    out["split"], out["wire"] = a, it["id"]
                break
    return out

def covering_end(sel, s0, to):
    """Where a covering ends. `to` is mm along the first wire, or None for "the end of the wire": then it stops earlier when
    a wire leaves the bundle. Returns (end, bundle at that stretch, id of the wire that made it stop early or None)."""
    s1 = min(sel[0]["len"], to if to else sel[0]["len"])
    b = covering_bundle(sel, s0, s1)
    if b["wire"] is not None and not to and b["split"] - 0.5 - s0 >= 1.0:
        s1 = b["split"] - 0.5
        return s1, covering_bundle(sel, s0, s1), b["wire"]
    return s1, b, None

_COVER_NUDGES = (1.0, 0.5, 0.1, 2.0, 5.0, 10.0)     # mm a covering is made longer / shorter by at a time (the first is the normal one)
_COVER_MIN = 1.0                                    # a covering is at least this long (mm)

def cover_nudge(s0, s1, wire_len, end, more, step, to_end=False):
    """Make a covering cover more or expose more of the wire, at one end, by `step` mm.

    s0 .. s1 is where it is now (mm along its first wire), `end` is "start" or "end", `more` True = cover more, False = expose
    more, to_end = it is set to go until the end of the wire. Returns (start, end, until the end of the wire, note); the note is
    a sentence when nothing can be changed (and the other values are the old ones)."""
    if end == "start":
        if more:
            if s0 <= 1e-6:
                return s0, s1, to_end, "It already starts at the start of the wire."
            return max(0.0, s0 - step), s1, to_end, None
        if s1 - (s0 + step) < _COVER_MIN - 1e-9:
            return s0, s1, to_end, "It cannot be shortened that much: a covering must stay at least %g mm long." % _COVER_MIN
        return s0 + step, s1, to_end, None
    if more:
        if to_end or s1 >= wire_len - 1e-6:
            if s1 < wire_len - 0.5:
                return s0, s1, to_end, "It already goes as far as the wires run side by side (%.1f mm)." % s1
            return s0, s1, to_end, "It already goes until the end of the wire."
        if s1 + step >= wire_len - 1e-6:
            return s0, wire_len, True, None
        return s0, s1 + step, False, None
    if s1 - step - s0 < _COVER_MIN - 1e-9:
        return s0, s1, to_end, "It cannot be shortened that much: a covering must stay at least %g mm long." % _COVER_MIN
    return s0, s1 - step, False, None


def covering_range(harness, c):
    """Where a covering is built now: (start, end, length of its first wire), all in mm along the first wire."""
    items, _skipped = _wire_items(harness, _COVER_STEP)
    by_id = {it["id"]: it for it in items}
    missing = [w for w in c["wires"] if w not in by_id]
    if missing or not c["wires"]:
        raise ValueError("Wire %s cannot be built right now, so the covering cannot be measured." % ", ".join(missing or ["?"]))
    sel = [by_id[w] for w in c["wires"]]
    s0 = max(0.0, c["from"])
    s1, _bundle, _left = covering_end(sel, s0, c["to"])
    return s0, s1, sel[0]["len"]


def _step_value(box, default=None):
    """The number in a step box (a combo box that can be typed in), or `default` when it is not a sensible number."""
    try:
        v = float(box.currentText().strip().replace(",", "."))
    except ValueError:
        return default
    return v if 0.01 <= v <= 1000.0 else default


def _nudge_panel(QtWidgets):
    """The buttons that make a covering cover more or expose more of its wires, at its start and at its end, with the step.

    Returns (widget, step box, {("start" or "end", True = cover more): button})."""
    box = QtWidgets.QComboBox()
    box.setEditable(True)
    for v in _COVER_NUDGES:
        box.addItem(_fmt(v))
    box.setCurrentText(_fmt(_setting("CoverStep", _COVER_NUDGES[0])))
    box.setFixedWidth(72)
    box.setToolTip("How far one click moves the end of the covering, in mm.\nPick a step from the list or type your own.")

    def remember(*_a):
        v = _step_value(box)
        if v is not None:
            _save_setting("CoverStep", v)
    box.editTextChanged.connect(remember)
    box.currentIndexChanged.connect(remember)
    w = QtWidgets.QWidget()
    g = QtWidgets.QGridLayout(w)
    g.setContentsMargins(0, 0, 0, 0)
    g.addWidget(QtWidgets.QLabel("Cover / expose by (mm)"), 0, 0)
    g.addWidget(box, 0, 1)
    buttons = {}
    tips = {("start", False): "Moves the start of the covering along the wire, away from the start of the wire:\nmore of the wire is exposed at the start.",
            ("start", True): "Moves the start of the covering back toward the start of the wire:\nmore of the wire is covered at the start.",
            ("end", False): "Pulls the end of the covering back toward the start of the wire:\nmore of the wire is exposed at the end.",
            ("end", True): "Makes the covering longer at its end: more of the wire is covered.\nAt the end of the wire it switches 'until the end of the wire' on."}
    for r, (end, label) in enumerate((("start", "Start of the covering"), ("end", "End of the covering")), 1):
        g.addWidget(QtWidgets.QLabel(label), r, 0)
        for c, (more, text) in enumerate(((False, "Expose"), (True, "Cover")), 1):
            b = QtWidgets.QPushButton(text)
            b.setToolTip(tips[(end, more)])
            g.addWidget(b, r, c)
            buttons[(end, more)] = b
    g.setColumnStretch(3, 1)
    return w, box, buttons


def bundle_text(sel, s0, b):
    """A sentence about a wire that does not run with the others (b = covering_bundle)."""
    ref, other = sel[0]["id"], b["wire"]
    if b["split"] <= s0 + 0.5:
        return ("wire %s does not run alongside wire %s here (up to %.0f mm away from it). One covering cannot go around both - "
                "leave wire %s out." % (other, ref, b["apart"], other))
    return ("wire %s runs alongside wire %s only until about %.0f mm, then goes its own way (up to %.0f mm away). End the "
            "covering there, or leave wire %s out." % (other, ref, b["split"], b["apart"], other))


def _planar_normal(pts, tol=1.0):
    """The normal of the plane when all the points lie within `tol` mm of one plane (a flat path), otherwise None."""
    a = pts[0]
    far = max(pts, key=lambda p: (p - a).Length)
    d1 = far - a
    if d1.Length < 1e-6:
        return None
    off = max(pts, key=lambda p: (p - a).cross(d1).Length)
    n = d1.cross(off - a)
    if n.Length < 1e-9:
        return None
    n.normalize()
    return n if max(abs((p - a).dot(n)) for p in pts) <= tol else None

def _sweep(spine, ring, normal):
    """The ring swept along the spine, as a solid.

    A flat (or nearly flat) path is swept with a fixed binormal first, the usual way breaks on those; any other path the usual
    way first. The other way is the second try. A result with the wrong volume counts as a failure."""
    radius = ring.Edges[0].Curve.Radius
    expected = math.pi * radius * radius * spine.Length
    tries = ([normal, None] if normal is not None else [None])
    error = None
    for n in tries:
        try:
            if n is None:
                shape = spine.makePipeShell([ring], True, False)
            else:
                ps = Part.BRepOffsetAPI.MakePipeShell(spine)
                ps.setBiNormalMode(n)
                ps.add(ring)
                ps.build()
                ps.makeSolid()
                shape = ps.shape()
            if shape.isValid() and 0.85 < abs(shape.Volume) / expected < 1.15:
                return shape
            error = ValueError("the tube came out with the wrong shape")
        except Exception as e:
            error = e
    raise ValueError("the covering cannot be built here (%s) - try a shorter one or a different size" % error)

_LOFT_STEP = 1.5
_LOFT_STEPS = (1.5, 2.0, 1.2)       # mm between the circles of the loft, in the order they are tried (circles packed closer make the loft overshoot)

def _loft_tube(edges, outer, inner, tight=None):
    """The hollow tube along the chain of edges: two lofts through circles that stand across the path (the outer and the inner
    surface) closed with a ring at each end. No boolean operation, so it is quick; no seams along the tube.

    The circles are turned along (parallel transport), so the tube does not twist. A loft through circles that are too close
    together can overshoot, so a loft that leaves the neighbourhood of the path is thrown away and another spacing is tried."""
    lengths = [e.Length for e in edges]
    total = sum(lengths)
    box = FreeCAD.BoundBox()
    for e in edges:
        box.add(e.BoundBox)
    box.enlarge(2.0 * outer + 1.0)             # a good loft stays inside this box, one that overshoots does not
    last = ValueError("the loft came out wrong")
    for step in _LOFT_STEPS:
        try:
            return _loft_once(edges, lengths, total, outer, inner, step, box)
        except Exception as e:
            last = e
    raise last

def _loft_once(edges, lengths, total, outer, inner, step, box):
    n = max(2, int(math.ceil(total / step)))
    marks, k, base = [], 0, 0.0
    for i in range(n + 1):
        s = total * i / n
        while k < len(edges) - 1 and s > base + lengths[k] + 1e-9:
            base += lengths[k]
            k += 1
        e = edges[k]
        u = e.getParameterByLength(max(0.0, min(lengths[k], s - base)))
        marks.append((e.valueAt(u), _unit(e.tangentAt(u))))
    x = marks[0][1].cross(Vector(0, 0, 1))
    if x.Length < 0.3:
        x = marks[0][1].cross(Vector(1, 0, 0))
    x = _unit(x)
    frames = []
    for p, t in marks:
        x = _unit(x - t * x.dot(t))
        frames.append((p, t, x))

    def ring(frame, r):
        p, t, xd = frame
        c = Part.Circle()
        c.Center, c.Axis, c.Radius = p, t, r
        c.XAxis = xd
        return Part.Wire(c.toShape())

    lofts = [Part.makeLoft([ring(f, r) for f in frames], False, False) for r in (outer, inner)]
    if not all(box.isInside(lf.BoundBox) for lf in lofts):
        raise ValueError("the loft overshoots")
    faces = []
    for lf in lofts:
        faces += lf.Faces
    faces += [Part.Face([ring(f, outer), ring(f, inner)]) for f in (frames[0], frames[-1])]
    solid = Part.Solid(Part.Shell(faces))
    solid.fix(1e-6, 1e-6, 1e-6)
    if solid.Volume < 0:
        solid.reverse()
    expected = math.pi * (outer * outer - inner * inner) * total
    if not solid.isValid() or not 0.93 < abs(solid.Volume) / expected < 1.07:
        raise ValueError("the loft came out wrong")
    # The middle of the hollow must be empty and the middle of the wall solid; otherwise the solid is inside out.
    mid = len(frames) // 2
    p, t, xd = frames[mid]
    if solid.isInside(p, 1e-6, True) or not solid.isInside(p + xd * ((outer + inner) / 2.0), 1e-6, True):
        raise ValueError("the loft came out inside out")
    return solid

def build_covering(by_id, c):
    """The hollow tube of one covering (a dict of read_coverings). Returns (solid, report text)."""
    if not c["wires"]:
        raise ValueError("no wires are listed")
    missing = [w for w in c["wires"] if w not in by_id]
    if missing:
        raise ValueError("wire %s does not exist or cannot be built" % ", ".join(missing))
    sel = [by_id[w] for w in c["wires"]]
    ref = sel[0]
    s0 = max(0.0, c["from"])
    s1, bundle, left = covering_end(sel, s0, c["to"])
    if s1 - s0 < 1.0:
        raise ValueError("it is shorter than 1 mm: wire %s is %.1f mm long, the covering goes from %.1f to %.1f mm"
                         % (ref["id"], ref["len"], s0, s1))
    if c["wall"] <= 0:
        raise ValueError("the wall must be thicker than 0")
    if bundle["wire"] is not None:
        raise ValueError(bundle_text(sel, s0, bundle))
    fit, tube, start = None, None, 0.0
    for attempt in range(3):
        try:
            nf = covering_fit(sel, s0, s1, c["inner"] or None, c["wall"], start)
        except ValueError:
            if fit is None:
                raise
            break
        fit = nf
        try:
            tube = _loft_tube(fit["edges"], fit["outer"], fit["inner"], fit["tight"])
            break
        except Exception:
            start = _next_sigma(fit["window"])
    pts, need, apart = fit["pts"], fit["need"], fit["apart"]
    edges, t, tight, inner, outer = fit["edges"], fit["t"], fit["tight"], fit["inner"], fit["outer"]
    c["length"] = _path_length(pts)
    if tube is None:
        spine = Part.Wire(edges)
        rings = [Part.Wire(Part.makeCircle(r, pts[0], t)) for r in (outer, inner)]
        normal = _planar_normal(pts)
        tube = _sweep(spine, rings[0], normal).cut(_sweep(spine, rings[1], normal))
    solids = tube.Solids
    if not solids or not tube.isValid():
        raise ValueError("the covering cannot be built here - try a shorter one or a different size")
    text = "wires %s, from %.1f to %.1f mm along wire %s, length %.1f mm, inner diameter %.2f mm, wall %.2f mm" % (
        ", ".join(c["wires"]), s0, s1, ref["id"], c["length"], 2 * inner, c["wall"])
    if c["inner"]:
        text += " (the wires need at least %.2f mm)" % (2 * need)
    if fit["window"] > 0:
        text += " - the wires bend sharply, so it cuts the corners (the wires alone need %.2f mm)" % (2 * fit["need0"])
    if left is not None:
        text += " - it ends here because wire %s goes its own way" % left
    return solids[0], text


_COVER_CACHE = {}              # (document, covering object) -> {covering id: [(key, shape, text or error, length), ...]}
_COVER_SHOWN = {}              # (document, covering object) -> what the shape of the object was last made from
_COVER_WARNED = {}             # (document, covering object) -> the last warning printed (so it is not repeated)
_COVER_CACHE_KEEP = 4          # per covering: how many earlier results are kept (undo / redo find them again)

def _wire_key(it):
    """A number that is the same for the same wire: its radius, length and sampled centre line (an item of _wire_items)."""
    k = it.get("_key")
    if k is None:
        k = it["_key"] = hash((round(it["r"], 4), round(it["len"], 4),
                               tuple((round(p.x, 4), round(p.y, 4), round(p.z, 4)) for p in it["pts"])))
    return k

def _cover_key(c, by_id):
    """What a covering is built from: its row of the table and the wires it goes around (None if one of them is missing)."""
    parts = [(tuple(c["wires"]), c["from"], c["to"], c["inner"], c["wall"])]
    for wid in c["wires"]:
        it = by_id.get(wid)
        if it is None:
            return None
        parts.append((wid, _wire_key(it)))
    return hash(tuple(parts))

def _execute_coverings(obj):
    covs, problems = read_coverings(obj.Params)
    items, _skipped = _wire_items(obj.Harness, _COVER_STEP) if obj.Harness is not None else ([], [])
    by_id = {it["id"]: it for it in items}
    where = (obj.Document.Name, obj.Name)
    cache = _COVER_CACHE.setdefault(where, {})
    for gone in [k for k in cache if k not in {c["id"] for c in covs}]:
        del cache[gone]
    solids, face_colors, report, errors, lengths, made = [], [], [], [], {}, []
    for index, c in enumerate(covs):
        # A covering is only built again when its row or one of its wires changed (the same result is kept otherwise).
        key = _cover_key(c, by_id)
        hist = cache.setdefault(c["id"], [])
        hit = next((e for e in hist if key is not None and e[0] == key), None)
        if hit is None:
            try:
                shape, text = build_covering(by_id, c)
                hit = (key, shape, text, c["length"])
            except Exception as e:
                hit = (key, None, str(e), None)
            if key is not None:
                hist.append(hit)
                del hist[:-_COVER_CACHE_KEEP]
        else:
            hist[:] = [e for e in hist if e is not hit] + [hit]
        if hit[1] is None:
            errors.append("Covering %s: %s" % (c["id"], hit[2]))
            report.append("Covering %s: ERROR - %s" % (c["id"], hit[2]))
            continue
        c["length"] = hit[3]
        lengths[c["row"]] = c["length"]
        solids.append(hit[1])
        made.append((c["id"], key, c["color"], index))
        face_colors.extend([tuple(parse_color(c["color"] or "black", index)[:3]) + (_COVER_OPACITY,)] * len(hit[1].Faces))
        report.append("Covering %s: %s" % (c["id"], hit[2]))
    report += ["Covering table: %s" % p for p in problems]
    obj.CoveringReport = report
    _sync_covering_table(obj.Params, lengths)
    errors += problems
    sig = (tuple(made), tuple(obj.Placement.toMatrix().A))
    if _COVER_SHOWN.get(where) != sig or bool(solids) == obj.Shape.isNull():
        if solids:
            comp = Part.makeCompound(solids)
            comp.Placement = obj.Placement
            obj.Shape = comp
            if FreeCAD.GuiUp and obj.ViewObject is not None:
                try:
                    obj.ViewObject.DiffuseColor = face_colors
                except Exception:
                    pass
        else:
            obj.Shape = Part.Shape()
        _COVER_SHOWN[where] = sig
    if errors:
        # A covering that cannot be built is simply left out (the others stay); the reason is in the report, in the window and here.
        text = "; ".join(errors)
        if _COVER_WARNED.get(where) != text:
            _COVER_WARNED[where] = text
            FreeCAD.Console.PrintWarning("Harness covering left out - %s\n" % text)
    else:
        _COVER_WARNED.pop(where, None)

class HarnessCoverings:
    """The coverings of a wire harness: hollow tubes around bundles of wires, described by the coverings table."""

    def __init__(self, obj, sheet, harness):
        obj.Proxy = self
        obj.addProperty("App::PropertyLink", "Params", "Coverings", "The coverings table")
        obj.addProperty("App::PropertyLink", "Harness", "Coverings", "The wire harness that is covered")
        obj.addProperty("App::PropertyStringList", "CoveringReport", "Result", "One line per covering (calculated)")
        obj.Params = sheet
        obj.Harness = harness
        obj.setEditorMode("CoveringReport", 1)

    def execute(self, obj):
        _execute_coverings(obj)

    def onDocumentRestored(self, obj):
        obj.Proxy = self
        obj.setEditorMode("CoveringReport", 1)

    def dumps(self):
        return None

    def loads(self, state):
        return None

    __getstate__ = dumps
    __setstate__ = loads


def find_coverings(doc):
    for o in doc.Objects:
        if type(getattr(o, "Proxy", None)).__name__ == "HarnessCoverings":
            return o
    return None


def ensure_coverings(doc, harness):
    """The coverings object of the document; the table and the object are made when there is none yet."""
    obj = find_coverings(doc)
    if obj is not None:
        return obj
    sheet = doc.getObject(COVERING_SHEET)
    if sheet is None:
        sheet = doc.addObject("Spreadsheet::Sheet", COVERING_SHEET)
        write_covering_headers(sheet)
    obj = doc.addObject("Part::FeaturePython", COVERING_OBJECT)
    HarnessCoverings(obj, sheet, harness)
    if FreeCAD.GuiUp:
        obj.ViewObject.Proxy = 0
        try:
            obj.ViewObject.DisplayMode = "Shaded"   # no lines: the tube is made of pieces and "Flat Lines" shows their seams as rings
        except Exception:
            pass
    doc.recompute()
    return obj


def next_covering_id(doc):
    obj = find_coverings(doc)
    used = {c["id"] for c in read_coverings(obj.Params)[0]} if obj is not None else set()
    k = 1
    while str(k) in used:
        k += 1
    return str(k)


def covering_title(c):
    text = "%s: wires %s (%s)" % (c["id"], ", ".join(c["wires"]),
                                  "%s - %s mm" % (_fmt(c["from"]), _fmt(c["to"])) if c["to"] else "%s mm - end" % _fmt(c["from"]))
    return text + (", %.1f mm long" % c["length"] if c.get("length") else "")


def _covering_error(obj, cid):
    """The error text of a covering that could not be built, or None."""
    prefix = "Covering %s: ERROR - " % cid
    for line in obj.CoveringReport:
        if line.startswith(prefix):
            return line[len(prefix):]
    return None


def _check_covering(obj, cid, undo):
    error = _covering_error(obj, cid)
    if error is None and not any(line.startswith("Covering %s: " % cid) for line in obj.CoveringReport):
        error = "it was not built: the wires of the harness cannot be built right now (see the wire report), so fix them first"
    if error is not None:
        undo()
        raise ValueError("Covering %s cannot be built: %s. Nothing was changed." % (cid, error))


def add_covering(doc, harness, wires, start=6.0, end=None, inner=None, wall=0.5, color="black", cid=None):
    """Put a covering around `wires` from `start` to `end` mm along the first of them (end None = to the end of the wire)."""
    wires = [str(w) for w in wires]
    if not wires:
        raise ValueError("choose at least one wire")
    known = {w["id"] for w in read_harness(harness.Params)[0]}
    if [w for w in wires if w not in known]:
        raise ValueError("wire %s does not exist" % ", ".join(w for w in wires if w not in known))
    obj = ensure_coverings(doc, harness)
    sheet = obj.Params
    cid = str(cid) if cid is not None else next_covering_id(doc)
    if any(c["id"] == cid for c in read_coverings(sheet)[0]):
        raise ValueError("covering %s already exists" % cid)
    row = COVERING_FIRST_ROW
    while any(_contents(sheet, "%s%d" % (c, row)) != "" for c in "ABCDEFG"):
        row += 1
    _write_covering_row(sheet, row, {"id": cid, "wires": wires, "from": start, "to": end, "inner": inner, "wall": wall,
                                     "color": color})
    doc.recompute()

    def undo():
        for col in "ABCDEFGH":
            sheet.clear("%s%d" % (col, row))
        doc.recompute()
    _check_covering(obj, cid, undo)
    return cid


def update_covering(doc, cid, wires, start, end, inner, wall, color):
    """Change a covering; if it cannot be built with the new values, the old ones are put back."""
    obj = find_coverings(doc)
    found = [c for c in read_coverings(obj.Params)[0] if c["id"] == str(cid)] if obj is not None else []
    if not found:
        raise ValueError("covering %s does not exist" % cid)
    old = found[0]
    _write_covering_row(obj.Params, old["row"], {"id": old["id"], "wires": [str(w) for w in wires], "from": start, "to": end,
                                                 "inner": inner, "wall": wall, "color": color})
    doc.recompute()
    _check_covering(obj, old["id"], lambda: (_write_covering_row(obj.Params, old["row"], old), doc.recompute()))


def remove_covering(doc, cid):
    obj = find_coverings(doc)
    found = [c for c in read_coverings(obj.Params)[0] if c["id"] == str(cid)] if obj is not None else []
    if not found:
        raise ValueError("covering %s does not exist" % cid)
    for col in "ABCDEFGH":
        obj.Params.clear("%s%d" % (col, found[0]["row"]))
    doc.recompute()


def _drop_wire_from_coverings(doc, wid):
    """A wire was deleted: take it out of the coverings; a covering left with no wires is removed."""
    obj = find_coverings(doc)
    if obj is None:
        return
    for c in read_coverings(obj.Params)[0]:
        if str(wid) in c["wires"]:
            rest = [w for w in c["wires"] if w != str(wid)]
            if rest:
                obj.Params.set("B%d" % c["row"], "'" + ", ".join(rest))
            else:
                for col in "ABCDEFGH":
                    obj.Params.clear("%s%d" % (col, c["row"]))


def default_covering_wires(harness, start=6.0):
    """The wires a new covering goes around by default: the first wire and the ones that run alongside it."""
    items, _skipped = _wire_items(harness, _COVER_STEP)
    if not items:
        return []
    ref, ids = items[0], [items[0]["id"]]
    for it in items[1:]:
        try:
            if ref["len"] - start < 1.0:
                continue
            b = covering_bundle([ref, it], start, ref["len"])
            if b["wire"] is None or b["split"] - start >= 0.5 * (ref["len"] - start):
                ids.append(it["id"])
        except Exception:
            pass
    return ids

def build_covering_dialog(doc, harness, existing=None, parent=None):
    """The 'Add covering' / 'Edit covering' form. dlg.apply() makes the change; dlg.exec() shows it."""
    import FreeCADGui
    from PySide import QtCore, QtWidgets
    wires = read_harness(harness.Params, [s.Name for s in harness.PointSheets])[0]
    dlg = QtWidgets.QDialog(parent or FreeCADGui.getMainWindow())
    dlg.setWindowTitle("Edit covering" if existing else "Add covering")
    form = QtWidgets.QFormLayout(dlg)

    def spin(lo, hi, value, step, dec):
        s = QtWidgets.QDoubleSpinBox()
        s.setDecimals(dec)
        s.setRange(lo, hi)
        s.setSingleStep(step)
        s.setValue(value)
        return s

    try:
        ticked = [w_["id"] for w_ in wires] if existing else default_covering_wires(harness)
    except Exception:
        ticked = [w_["id"] for w_ in wires]
    dlg.f_id = QtWidgets.QLineEdit(existing["id"] if existing else next_covering_id(doc))
    dlg.f_id.setEnabled(existing is None)
    dlg.f_wires = QtWidgets.QListWidget()
    dlg.f_wires.setMaximumHeight(110)
    for w in wires:
        item = QtWidgets.QListWidgetItem("Wire %s" % w["id"])
        item.setData(QtCore.Qt.UserRole, w["id"])
        item.setFlags(item.flags() | QtCore.Qt.ItemIsUserCheckable)
        item.setCheckState(QtCore.Qt.Checked if (w["id"] in (existing["wires"] if existing else ticked)) else QtCore.Qt.Unchecked)
        dlg.f_wires.addItem(item)
    dlg.f_wires.setToolTip("Tick the wires the covering goes around: wires that run side by side. The first one ticked is the\n"
                           "reference for From and Length. A wire that goes its own way cannot be inside the same covering.")
    start0 = existing["from"] if existing else 6.0
    to_end = existing is None or not existing["to"]
    dlg.f_from = spin(0, 5000, start0, 1.0, 2)
    dlg.f_from.setToolTip("Where the covering starts: mm along the first wire, from the start of the wire.")
    dlg.f_len = spin(0.1, 5000, max(0.1, existing["to"] - start0) if not to_end else 50.0, 1.0, 2)
    dlg.f_len.setToolTip("How long the covering is: mm along the first wire, counted from where it starts.")
    dlg.f_len.setEnabled(not to_end)
    dlg.f_end = QtWidgets.QCheckBox("Until the end of the wire")
    dlg.f_end.setChecked(to_end)
    dlg.f_end.setToolTip("On: the covering goes to the end of the first wire (it stops sooner where a wire leaves the bundle).\n"
                         "The length then follows the wire. Off: the covering has the length you type.")
    dlg.f_inner = spin(0, 200, (existing["inner"] or 0.0) if existing else 0.0, 0.5, 2)
    dlg.f_inner.setSpecialValueText("automatic")
    dlg.f_wall = spin(0.05, 50, existing["wall"] if existing else 0.5, 0.1, 2)
    dlg.f_color = QtWidgets.QComboBox()
    dlg.f_color.setEditable(True)
    dlg.f_color.addItems(sorted(k for k in COLORS if k != "gray"))
    dlg.f_color.setCurrentText((existing["color"].lstrip("'") if existing and existing["color"] else "black"))
    dlg.info = QtWidgets.QLabel("")
    dlg.info.setWordWrap(True)
    dlg.info.setMinimumWidth(380)
    dlg.b_split = QtWidgets.QPushButton("End it where the wires split")
    dlg.b_split.setToolTip("Sets the length so the covering ends where the wires stop running side by side.")
    dlg.b_split.setVisible(False)
    dlg.split_at = None
    dlg.eff = None                      # where the covering is now: (start, end, length of the first wire), set by update_info
    nudge_box, dlg.f_step, dlg.f_nudges = _nudge_panel(QtWidgets)
    for label, widget in (("Covering", dlg.f_id), ("Goes around", dlg.f_wires), ("From (mm)", dlg.f_from),
                          ("Length from start (mm)", dlg.f_len), ("", dlg.f_end), ("Adjust", nudge_box),
                          ("Inner diameter (mm)", dlg.f_inner), ("Wall (mm)", dlg.f_wall), ("Color", dlg.f_color)):
        form.addRow(label, widget)
    form.addRow("", dlg.info)
    form.addRow("", dlg.b_split)

    def chosen():
        return [dlg.f_wires.item(i).data(QtCore.Qt.UserRole) for i in range(dlg.f_wires.count())
                if dlg.f_wires.item(i).checkState() == QtCore.Qt.Checked]

    def update_info(*_a):
        import html
        ids = chosen()
        dlg.split_at = None
        dlg.eff = None
        dlg.b_split.setVisible(False)
        if not ids:
            dlg.info.setText("Tick the wires the covering goes around.")
            return
        bad = False
        try:
            items, _skipped = _wire_items(harness, _COVER_STEP)
            by_id = {it["id"]: it for it in items}
            sel = [by_id[i] for i in ids]
            s0 = dlg.f_from.value()
            s1, bundle, left = covering_end(sel, s0, None if dlg.f_end.isChecked() else s0 + dlg.f_len.value())
            dlg.eff = (s0, s1, sel[0]["len"])
            if dlg.f_end.isChecked() and s1 - s0 >= 0.1:
                dlg.f_len.blockSignals(True)
                dlg.f_len.setValue(round(s1 - s0, 2))           # while it goes until the end, the length shows how long that is
                dlg.f_len.blockSignals(False)
            if s1 - s0 < 1.0:
                raise ValueError("it must be at least 1 mm long: wire %s is %.1f mm long" % (sel[0]["id"], sel[0]["len"]))
            if bundle["wire"] is not None:
                bad = True
                text = "Not possible: " + bundle_text(sel, s0, bundle)
                if bundle["split"] > s0 + 1.0:
                    dlg.split_at = max(s0 + 1.0, int(bundle["split"] * 10) / 10.0 - 0.3)
                    dlg.b_split.setText("End it where the wires split (%.1f mm)" % dlg.split_at)
                    dlg.b_split.setVisible(True)
            else:
                try:
                    fit = covering_fit(sel, s0, s1, dlg.f_inner.value() or None, dlg.f_wall.value())
                    text = "The covering is %.1f mm long. Over this stretch the wires need an inner diameter of at least %.2f mm." % (
                        _path_length(fit["pts"]), 2 * fit["need"])
                    if fit["window"] > 0:
                        text += " The wires bend sharply there, so the covering cuts the corners a little."
                    if left is not None:
                        text += " It ends at %.0f mm because wire %s goes its own way there." % (s1, left)
                    if not dlg.f_end.isChecked() and s0 + dlg.f_len.value() > sel[0]["len"] + 0.005:
                        text += " Wire %s is only %.1f mm long, so the covering stops at its end." % (sel[0]["id"], sel[0]["len"])
                except _TooSmall as e:
                    bad = True
                    text = ("Not possible: an inner diameter of %.2f mm is too small, the wires need at least %.2f mm here. Make it "
                            "bigger, or set it to automatic." % (dlg.f_inner.value(), 2 * e.need))
                except ValueError as e:
                    bad = True
                    text = "Not possible: %s" % e
        except Exception as e:
            bad = True
            text = "Cannot work out the size yet: %s" % e
        if bad:
            dlg.info.setText('<span style="color:#ff5555"><b>%s</b></span>' % html.escape(text))
        else:
            dlg.info.setText(text)

    def set_range(start, length, until_end):
        """Put a new start / length / 'until the end' into the form and work out the covering once."""
        for w_ in (dlg.f_from, dlg.f_len, dlg.f_end):
            w_.blockSignals(True)
        dlg.f_from.setValue(start)
        dlg.f_len.setValue(max(0.1, length))
        dlg.f_end.setChecked(until_end)
        dlg.f_len.setEnabled(not until_end)
        for w_ in (dlg.f_from, dlg.f_len, dlg.f_end):
            w_.blockSignals(False)
        update_info()

    def do_split():
        if dlg.split_at is not None:
            set_range(dlg.f_from.value(), dlg.split_at - dlg.f_from.value(), False)

    def on_end_toggled(on):
        dlg.f_len.setEnabled(not on)
        update_info()

    def nudge(end, more):
        if dlg.eff is None:
            return
        s0, s1, wire_len = dlg.eff
        step = _step_value(dlg.f_step, _setting("CoverStep", _COVER_NUDGES[0]))
        a, b, until_end, note = cover_nudge(s0, s1, wire_len, end, more, step, dlg.f_end.isChecked())
        if note:
            dlg.info.setText(note)
            return
        set_range(a, b - a, until_end)

    for (end_, more_), b_ in dlg.f_nudges.items():
        b_.clicked.connect(lambda _c=False, end=end_, more=more_: nudge(end, more))
    dlg.nudge = nudge
    dlg.b_split.clicked.connect(do_split)
    dlg.f_wires.itemChanged.connect(update_info)
    dlg.f_end.toggled.connect(on_end_toggled)
    for w_ in (dlg.f_from, dlg.f_len, dlg.f_inner, dlg.f_wall):
        w_.valueChanged.connect(update_info)
    update_info()

    def apply():
        ids = chosen()
        end = None if dlg.f_end.isChecked() else dlg.f_from.value() + dlg.f_len.value()
        inner = dlg.f_inner.value() or None
        color = dlg.f_color.currentText().strip()
        doc.openTransaction("Edit covering" if existing else "Add covering")
        try:
            if existing:
                update_covering(doc, existing["id"], ids, dlg.f_from.value(), end, inner, dlg.f_wall.value(), color)
                cid = existing["id"]
            else:
                cid = add_covering(doc, harness, ids, dlg.f_from.value(), end, inner, dlg.f_wall.value(), color,
                                   dlg.f_id.text().strip() or None)
        except Exception:
            doc.abortTransaction()
            doc.recompute()
            raise
        doc.commitTransaction()
        dlg.result_id = cid
        return cid
    dlg.apply = apply

    buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
    buttons.button(QtWidgets.QDialogButtonBox.Ok).setText("Save" if existing else "Add covering")
    form.addRow(buttons)

    def on_ok():
        try:
            apply()
        except Exception as e:
            QtWidgets.QMessageBox.warning(dlg, dlg.windowTitle(), str(e))
            return
        dlg.accept()
    buttons.accepted.connect(on_ok)
    buttons.rejected.connect(dlg.reject)
    return dlg


def build_twist_dialog(doc, harness, existing=None, parent=None):
    """The 'Twist wires' / 'Edit twist' form. dlg.apply() makes the change; dlg.exec() shows it."""
    import FreeCADGui
    from PySide import QtCore, QtWidgets
    wires = read_harness(harness.Params, [s.Name for s in harness.PointSheets])[0]
    dlg = QtWidgets.QDialog(parent or FreeCADGui.getMainWindow())
    dlg.setWindowTitle("Edit twist" if existing else "Twist wires")
    form = QtWidgets.QFormLayout(dlg)

    def spin(lo, hi, value, step, dec):
        s = QtWidgets.QDoubleSpinBox()
        s.setDecimals(dec)
        s.setRange(lo, hi)
        s.setSingleStep(step)
        s.setValue(value)
        return s

    taken = {i: t["id"] for t in harness_twists(harness)[0] if not existing or t["id"] != existing["id"] for i in t["wires"]}
    free = [w_["id"] for w_ in wires if w_["id"] not in taken]
    ticked = existing["wires"] if existing else free[:2]
    dlg.f_id = QtWidgets.QLineEdit(existing["id"] if existing else next_twist_id(harness))
    dlg.f_id.setEnabled(existing is None)
    dlg.f_wires = QtWidgets.QListWidget()
    dlg.f_wires.setMaximumHeight(110)
    for w_ in wires:
        item = QtWidgets.QListWidgetItem("Wire %s%s" % (w_["id"], "  (already in twist %s)" % taken[w_["id"]] if w_["id"] in taken else ""))
        item.setData(QtCore.Qt.UserRole, w_["id"])
        flags = item.flags() | QtCore.Qt.ItemIsUserCheckable
        if w_["id"] in taken:
            flags &= ~QtCore.Qt.ItemIsEnabled
        item.setFlags(flags)
        item.setCheckState(QtCore.Qt.Checked if w_["id"] in ticked else QtCore.Qt.Unchecked)
        dlg.f_wires.addItem(item)
    dlg.f_wires.setToolTip("Tick 2 or more wires that run side by side. They wind around the line between them. The first one\\n"
                           "ticked is the reference for From and Length. A wire can be in one twist only.")
    start0 = existing["from"] if existing else 10.0
    to_end = existing is None or not existing["to"]
    dlg.f_from = spin(0, 5000, start0, 1.0, 2)
    dlg.f_from.setToolTip("Where the twist starts: mm along the first wire, from the start of the wire.")
    dlg.f_len = spin(0.1, 5000, max(0.1, existing["to"] - start0) if not to_end else 50.0, 1.0, 2)
    dlg.f_len.setToolTip("How long the twist is: mm along the first wire, counted from where it starts.")
    dlg.f_len.setEnabled(not to_end)
    dlg.f_end = QtWidgets.QCheckBox("Until the end of the wires")
    dlg.f_end.setChecked(to_end)
    dlg.f_end.setToolTip("On: the twist goes on as far as the wires run side by side.\\nOff: the twist has the length you type.")
    dlg.f_lay = spin(1, 1000, existing["lay"] if existing else _TWIST_LAY, 1.0, 1)
    dlg.f_lay.setToolTip("How far along the wires one full turn is, in mm. Smaller = tighter twist.\\n"
                         "Too small and the wires turn tighter than their minimum bend radius (you get a warning).")
    dlg.f_hand = QtWidgets.QComboBox()
    dlg.f_hand.addItems(["Right-hand", "Left-hand"])
    dlg.f_hand.setCurrentIndex(1 if existing and existing["hand"] < 0 else 0)
    dlg.f_hand.setToolTip("Which way the wires wind. Right-hand is the way a normal screw thread goes.")
    dlg.info = QtWidgets.QLabel("")
    dlg.info.setWordWrap(True)
    dlg.info.setMinimumWidth(380)
    for label, widget in (("Twist", dlg.f_id), ("Wires", dlg.f_wires), ("From (mm)", dlg.f_from),
                          ("Length from start (mm)", dlg.f_len), ("", dlg.f_end), ("Lay length (mm per turn)", dlg.f_lay),
                          ("Hand", dlg.f_hand)):
        form.addRow(label, widget)
    form.addRow("", dlg.info)

    def chosen():
        return [dlg.f_wires.item(i).data(QtCore.Qt.UserRole) for i in range(dlg.f_wires.count())
                if dlg.f_wires.item(i).checkState() == QtCore.Qt.Checked]

    def hand():
        return -1 if dlg.f_hand.currentIndex() == 1 else 1

    def definition():
        return {"id": dlg.f_id.text().strip() or "?", "wires": chosen(), "from": dlg.f_from.value(),
                "to": None if dlg.f_end.isChecked() else dlg.f_from.value() + dlg.f_len.value(), "lay": dlg.f_lay.value(),
                "hand": hand()}

    def update_info(*_a):
        import html
        ids = chosen()
        if len(ids) < 2:
            dlg.info.setText("Tick 2 or more wires to twist.")
            return
        bad = False
        try:
            by_id = {w_["id"]: w_ for w_ in wires if "error" not in w_}
            info = twist_check(doc, harness, definition())
            text = twist_text(info)
            if dlg.f_end.isChecked() and info["to"] - info["from"] >= 0.1:
                dlg.f_len.blockSignals(True)
                dlg.f_len.setValue(round(info["to"] - info["from"], 2))      # while it goes until the end, the length shows how long that is
                dlg.f_len.blockSignals(False)
        except Exception as e:
            bad = True
            text = "Not possible: %s" % e
        dlg.info.setText('<span style="color:#ff5555"><b>%s</b></span>' % html.escape(text) if bad else text)

    dlg.f_wires.itemChanged.connect(update_info)
    dlg.f_end.toggled.connect(lambda on: (dlg.f_len.setEnabled(not on), update_info()))
    dlg.f_hand.currentIndexChanged.connect(update_info)
    for w_ in (dlg.f_from, dlg.f_len, dlg.f_lay):
        w_.valueChanged.connect(update_info)
    update_info()

    def apply():
        ids = chosen()
        end = None if dlg.f_end.isChecked() else dlg.f_from.value() + dlg.f_len.value()
        doc.openTransaction("Edit twist" if existing else "Twist wires")
        try:
            if existing:
                update_twist(doc, harness, existing["id"], ids, dlg.f_from.value(), end, dlg.f_lay.value(), hand())
                tid = existing["id"]
            else:
                tid = add_twist(doc, harness, ids, dlg.f_from.value(), end, dlg.f_lay.value(), hand(), dlg.f_id.text().strip() or None)
        except Exception:
            doc.abortTransaction()
            doc.recompute()
            raise
        doc.commitTransaction()
        dlg.result_id = tid
        return tid
    dlg.apply = apply

    buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
    buttons.button(QtWidgets.QDialogButtonBox.Ok).setText("Save" if existing else "Twist wires")
    form.addRow(buttons)

    def on_ok():
        try:
            apply()
        except Exception as e:
            QtWidgets.QMessageBox.warning(dlg, dlg.windowTitle(), str(e))
            return
        dlg.accept()
    buttons.accepted.connect(on_ok)
    buttons.rejected.connect(dlg.reject)
    return dlg


# ---------------------------------------------------------------------------
# The drag gizmo in the 3D view
# ---------------------------------------------------------------------------

GIZMO_NAME = "WireGizmo"
# per axis: the axis, then the two directions that span the ring around it (axis = first x second)
_GIZMO_AXES = {"X": ((1, 0, 0), (0, 1, 0), (0, 0, 1)),
               "Y": ((0, 1, 0), (0, 0, 1), (1, 0, 0)),
               "Z": ((0, 0, 1), (1, 0, 0), (0, 1, 0))}
_GIZMO_COLORS = {"X": (0.9, 0.2, 0.2), "Y": (0.2, 0.75, 0.25), "Z": (0.25, 0.4, 0.95), "center": (0.95, 0.95, 0.95)}
_GIZMO_ACTIVE = (1.0, 0.9, 0.1)
_GIZMO_PIXELS = 100.0    # length of an arrow at 100 %, in logical screen pixels (the same on a normal and on a high-resolution screen)
_GIZMO_SIZES = (50, 300, 100)       # smallest, largest and normal size of the gizmo, in per cent
_GIZMO_PARAM = "User parameter:BaseApp/Preferences/Mod/WireHarness"
_GRAB_PIXELS = 10.0      # the mouse grabs a handle from this far away (logical pixels), so thin lines and small tips are easy to hit
_BALL_RADIUS = 0.15      # radius of the ball in the middle, as a fraction of the arrow length
_HOVER_LIGHTEN = 0.55    # the handle under the mouse is shown this much closer to white
_GIZMO_RING = 0.62       # radius of the rings, as a fraction of the arrow length
_GHOST_COLOR = (1.0, 0.25, 0.85)    # the preview of the wire / heading while a ring is dragged
_GHOST_BAD = (1.0, 0.1, 0.1)        # ... when the turn would not work
_DIAL_RADIUS = 1.45      # the dial that appears while a ring is turned (gizmo units; bigger than the ring, so 5 degrees are easy to tell apart)
_TURN_STEPS = (1.0, 0.5, 0.1, 0.05, 0.01, 5.0, 15.0, 45.0)    # ring steps offered in the window, in degrees (the first is the normal one)
_TURN_STEP_MIN, _TURN_STEP_MAX = 0.001, 90.0                 # the manual step may be anything in between

def _turn_step():
    """The step the rings turn in, in degrees (remembered between sessions)."""
    return max(_TURN_STEP_MIN, min(_TURN_STEP_MAX, _setting("TurnStep", _TURN_STEPS[0])))

def _angle_str(deg, step, signed=False):
    """An angle written with as many decimals as the step needs (1 -> "5", 0.05 -> "0.05")."""
    n = 3
    for k in range(4):
        if abs(round(step, k) - step) < 1e-9:
            n = k
            break
    return ("%+.*f" if signed else "%.*f") % (n, deg)

_FINE_FACTOR = 0.1       # Shift while turning a ring: the ring follows the mouse at a tenth of the speed


def _gizmo_percent():
    """The size of the gizmo the user chose last time (per cent)."""
    try:
        v = int(FreeCAD.ParamGet(_GIZMO_PARAM).GetInt("GizmoSize", _GIZMO_SIZES[2]))
    except Exception:
        v = _GIZMO_SIZES[2]
    return max(_GIZMO_SIZES[0], min(_GIZMO_SIZES[1], v))


def _save_gizmo_percent(value):
    try:
        FreeCAD.ParamGet(_GIZMO_PARAM).SetInt("GizmoSize", int(value))
    except Exception:
        pass


def _seg_dist(p, a, b):
    """Distance from the point p to the segment a-b (2D tuples)."""
    ax, ay = a
    dx, dy = b[0] - ax, b[1] - ay
    n = dx * dx + dy * dy
    t = 0.0 if n < 1e-12 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / n))
    return math.hypot(p[0] - (ax + t * dx), p[1] - (ay + t * dy))


_AXIS_DIRS = (("+X", (1, 0, 0), "X"), ("-X", (-1, 0, 0), "X"), ("+Y", (0, 1, 0), "Y"), ("-Y", (0, -1, 0), "Y"),
              ("+Z", (0, 0, 1), "Z"), ("-Z", (0, 0, -1), "Z"))
_PARALLEL_TOL = 0.05     # degrees: a heading this close to an axis counts as parallel to it
_PARALLEL_NEAR = 3.0     # degrees: closer than this is worth a hint ("0.4 deg from +Y")
_PARALLEL_TEXT = (0.55, 1.0, 1.0)     # the dial text turns pale cyan, which stands out on the yellow marker too


def _axis_alignment(h):
    """The axis direction closest to heading h: (name, degrees apart, "X"/"Y"/"Z", direction)."""
    h = _unit(Vector(*h))
    best = None
    for name, dv, ax in _AXIS_DIRS:
        ang = math.degrees(math.acos(max(-1.0, min(1.0, h.dot(Vector(*dv))))))
        if best is None or ang < best[1]:
            best = (name, ang, ax, dv)
    return best


def parallel_angles(h, n):
    """Where turning heading h around axis n makes it parallel to an axis direction.

    Returns [(name, degrees)] with degrees in (-180, 180], the turn from the present heading (positive = right-hand
    turn about n); degrees is None when the heading is along n and so never changes."""
    h, n = _unit(Vector(*h)), _unit(Vector(*n))
    hn = h.dot(n)
    hp = h - n * hn
    out = []
    for name, dv, _ax in _AXIS_DIRS:
        d = Vector(*dv)
        dn = d.dot(n)
        if abs(hn - dn) > 1e-6:
            continue
        if hp.Length < 1e-6:
            out.append((name, None))
            continue
        dp = d - n * dn
        out.append((name, math.degrees(math.atan2(n.dot(hp.cross(dp)), hp.dot(dp)))))
    return out


class _Label:
    """Text that always faces the screen, with a dark shadow so it can be read over any colour."""

    def __init__(self, coin, justification="LEFT", size=18):
        self.node = coin.SoSeparator()
        self.font = coin.SoFont()
        self.font.size = size
        self.node.addChild(self.font)
        self._parts = []
        for color in ((0.0, 0.0, 0.0), (1.0, 1.0, 1.0)):          # the shadow first, the text on top of it
            s = coin.SoSeparator()
            m = coin.SoMaterial()
            m.diffuseColor = color
            s.addChild(m)
            t = coin.SoTranslation()
            s.addChild(t)
            x = coin.SoText2()
            x.justification = getattr(coin.SoText2, justification)
            s.addChild(x)
            self.node.addChild(s)
            self._parts.append((m, t, x))

    def set(self, lines, anchor, right, up, px, color=(1.0, 1.0, 1.0), shadow=1.5):
        """lines: list of text lines; anchor: where it is attached (a Vector); right, up: screen directions;
        px: size of one screen pixel at the anchor, in the units of the scene it is in."""
        shift = (right - up) * (px * shadow)
        self._parts[1][0].diffuseColor = color
        for (_m, t, x), at in zip(self._parts, (anchor + shift, anchor)):
            t.translation = tuple(at)
            x.string.setNum(len(lines))
            x.string.setValues(0, len(lines), list(lines))


def _make_gizmo_filter(gizmo):
    """A Qt event filter that hands the mouse events of the 3D view to the gizmo."""
    from PySide import QtCore

    class _GizmoFilter(QtCore.QObject):
        def eventFilter(self, _obj, ev):
            try:
                return bool(gizmo._qt_event(ev))
            except Exception as e:
                FreeCAD.Console.PrintError("Wire gizmo: %s\n" % e)
                gizmo._abort()
                return False
    return _GizmoFilter()


class _ViewMath:
    """What a drawing over the 3D view needs to know about the camera: where a point is drawn, how many mm a pixel is, which way
    is up. Needs self.view, self._coin, self._vp and (for units_per_pixel) self.center."""

    def _view_volume(self):
        """The camera's view volume as Coin draws and picks it (in a window taller than wide, Coin shrinks the picture)."""
        cam = self.view.getCameraNode()
        w, h = self.view.getSize()
        aspect = w / float(h)
        vv = cam.getViewVolume(aspect)
        if aspect < 1.0 and cam.viewportMapping.getValue() == getattr(self._coin.SoCamera, "ADJUST_CAMERA", 3):
            vv.scale(1.0 / aspect)
        return vv


    def project(self, point):
        """The pixel (x, y) where a model point is drawn, counted from the bottom left like Coin."""
        w, h = self.view.getSize()
        q = self._view_volume().projectToScreen(self._coin.SbVec3f(point.x, point.y, point.z)).getValue()
        return (q[0] * w, q[1] * h)


    def project_all(self, points):
        """The pixels (x, y) where model points are drawn (see project), for a whole list at once."""
        w, h = self.view.getSize()
        vv = self._view_volume()
        sv = self._coin.SbVec3f
        out = []
        for p in points:
            q = vv.projectToScreen(sv(p.x, p.y, p.z)).getValue()
            out.append((q[0] * w, q[1] * h))
        return out


    def _ray(self, x, y):
        """The mouse ray (origin, unit direction) through pixel (x, y)."""
        w, h = self.view.getSize()
        a, b = self._view_volume().projectPointToLine(self._coin.SbVec2f(x / float(w), y / float(h)))
        origin = Vector(*a.getValue())
        direction = Vector(*b.getValue()) - origin
        direction.normalize()
        return origin, direction


    def units_per_pixel(self):
        """How many mm one screen pixel is at the gizmo (measured with Coin's own picking rays, so it is right for any window shape)."""
        w, h = self.view.getSize()
        o0, d0 = self._ray(w / 2.0, h / 2.0)
        o1, d1 = self._ray(w / 2.0 + 1.0, h / 2.0)
        t = (self.center - o0).dot(d0)
        return ((o1 + d1 * t) - (o0 + d0 * t)).Length


    def _cam_axes(self):
        """The directions of screen-right and screen-up in the scene."""
        q = self.view.getCameraNode().orientation.getValue().getValue()
        rot = Rotation(q[0], q[1], q[2], q[3])
        return rot.multVec(Vector(1, 0, 0)), rot.multVec(Vector(0, 1, 0))


    def _dpr(self):
        return self._vp.devicePixelRatioF()


class DragGizmo(_ViewMath):
    """Arrows, a ball and rings drawn on the selected point in the 3D view; drag them with the left mouse button.

    Arrow: move along that axis.  Ball: move across the view.  Ring: turn around that axis.
    While you drag, only the gizmo follows the mouse. When you let go, on_move(delta Vector) or
    on_turn(axis Vector, degrees) is called once, so the wires are recomputed once per drag."""

    def __init__(self, view, on_move, on_turn, preview_factory=None, move_preview_factory=None):
        from pivy import coin
        self._coin = coin
        self.view = view
        self.on_move, self.on_turn = on_move, on_turn
        self.preview_factory = preview_factory      # () -> preview(axis, degrees), used to draw the turn before it is applied
        self.move_preview_factory = move_preview_factory    # () -> preview(delta), the same for a move
        self.whole = False                          # True: a move moves the whole wire
        self._swallow_release = False
        self._keys_grabbed = False
        self.root = view.getSceneGraph()
        self.center = Vector(0, 0, 0)
        self.visible = False
        self._drag = None
        self._sensors = []
        self.percent = _gizmo_percent()             # size of the gizmo, in per cent of the normal size
        self.angle_step = _turn_step()              # the rings turn in multiples of this many degrees
        self._ring_styles = []                      # the drawing styles whose line width depends on the screen
        self._hover = None                          # the handle under the mouse
        self._cursor_set = False
        self._s = 1.0                               # mm per gizmo unit (= the length of an arrow) at the moment
        self._vp = view.graphicsView().viewport()
        self._build()

    def set_angle_step(self, step):
        self.angle_step = max(_TURN_STEP_MIN, min(_TURN_STEP_MAX, float(step)))

    # ----- the picture
    def _build(self):
        coin = self._coin
        # the drawing; last in the scene and without a depth test, so it is drawn over the wires
        self.sep = coin.SoSeparator()
        self.sep.setName(GIZMO_NAME)
        depth = coin.SoDepthBuffer()
        depth.test = False
        self.sep.addChild(depth)
        light = coin.SoLightModel()
        light.model = coin.SoLightModel.BASE_COLOR
        self.sep.addChild(light)
        self._build_ghost()
        self._build_measure()
        self.pos = coin.SoTranslation()
        self.sep.addChild(self.pos)
        self.scale = coin.SoScale()
        self.sep.addChild(self.scale)
        self.handles, self.mats, self.turns = {}, {}, {}
        for ax in "XYZ":
            node, mat = self._make_arrow(ax)
            self.sep.addChild(node)
            self.handles["move:" + ax], self.mats["move:" + ax] = node, mat
        node, mat = self._make_center()
        self.sep.addChild(node)
        self.handles["center"], self.mats["center"] = node, mat
        self.rings = coin.SoSwitch()
        self.rings.whichChild = getattr(coin, "SO_SWITCH_ALL", -3)
        self.sep.addChild(self.rings)
        for ax in "XYZ":
            node, mat, rot = self._make_ring(ax)
            self.rings.addChild(node)
            self.handles["turn:" + ax], self.mats["turn:" + ax], self.turns[ax] = node, mat, rot
        self._build_protractor()

    def _make_arrow(self, ax):
        coin = self._coin
        s = coin.SoSeparator()
        m = coin.SoMaterial()
        m.diffuseColor = _GIZMO_COLORS[ax]
        s.addChild(m)
        r = coin.SoRotation()
        r.rotation = coin.SbRotation(coin.SbVec3f(1, 0, 0), coin.SbVec3f(*_GIZMO_AXES[ax][0]))
        s.addChild(r)
        r2 = coin.SoRotationXYZ()
        r2.axis = coin.SoRotationXYZ.Z
        r2.angle = -math.pi / 2          # from here on the local Y direction is the arrow direction
        s.addChild(r2)
        t = coin.SoTranslation()
        t.translation = (0, 0.47, 0)       # the shaft starts just outside the ball, so the ball can be grabbed in the middle
        s.addChild(t)
        cyl = coin.SoCylinder()
        cyl.radius, cyl.height = 0.05, 0.62
        s.addChild(cyl)
        t2 = coin.SoTranslation()
        t2.translation = (0, 0.405, 0)
        s.addChild(t2)
        cone = coin.SoCone()
        cone.bottomRadius, cone.height = 0.12, 0.25
        s.addChild(cone)
        return s, m

    def _make_center(self):
        coin = self._coin
        s = coin.SoSeparator()
        m = coin.SoMaterial()
        m.diffuseColor = _GIZMO_COLORS["center"]
        s.addChild(m)
        ball = coin.SoSphere()
        ball.radius = _BALL_RADIUS
        s.addChild(ball)
        return s, m

    def _make_ring(self, ax):
        coin = self._coin
        _, u, v = (Vector(*c) for c in _GIZMO_AXES[ax])
        s = coin.SoSeparator()
        m = coin.SoMaterial()
        m.diffuseColor = _GIZMO_COLORS[ax]
        s.addChild(m)
        style = coin.SoDrawStyle()
        style.lineWidth = 4
        self._ring_styles.append(style)
        s.addChild(style)
        rot = coin.SoRotation()          # turns with the mouse while you drag
        s.addChild(rot)
        n = 72
        pts = [tuple((u * math.cos(2 * math.pi * i / n) + v * math.sin(2 * math.pi * i / n)) * _GIZMO_RING) for i in range(n + 1)]
        co = coin.SoCoordinate3()
        co.point.setValues(0, n + 1, pts)
        s.addChild(co)
        ls = coin.SoLineSet()
        ls.numVertices = n + 1
        s.addChild(ls)
        return s, m, rot

    def _build_ghost(self):
        """A dashed line in the colour of the preview: the wire as it would run after the turn (model coordinates)."""
        coin = self._coin
        self.ghost = coin.SoSwitch()
        self.ghost.whichChild = getattr(coin, "SO_SWITCH_NONE", -1)
        s = coin.SoSeparator()
        self.ghost_mat = coin.SoMaterial()
        self.ghost_mat.diffuseColor = _GHOST_COLOR
        s.addChild(self.ghost_mat)
        style = coin.SoDrawStyle()
        style.lineWidth = 3
        style.linePattern = 0xF0F0
        s.addChild(style)
        self.ghost_co = coin.SoCoordinate3()
        s.addChild(self.ghost_co)
        self.ghost_ls = coin.SoLineSet()
        s.addChild(self.ghost_ls)
        self.ghost.addChild(s)
        self.sep.addChild(self.ghost)

    def _build_measure(self):
        """While an arrow or the ball is dragged: a line from where the point was to where it is, and a label with
        the distance and the new position (model coordinates)."""
        coin = self._coin
        self.measure = coin.SoSwitch()
        self.measure.whichChild = getattr(coin, "SO_SWITCH_NONE", -1)
        s = coin.SoSeparator()
        part = coin.SoSeparator()
        self.m_mat = coin.SoMaterial()
        self.m_mat.diffuseColor = _GIZMO_ACTIVE
        part.addChild(self.m_mat)
        st = coin.SoDrawStyle()
        st.lineWidth = 3
        part.addChild(st)
        self.m_co = coin.SoCoordinate3()
        part.addChild(self.m_co)
        ls = coin.SoLineSet()
        ls.numVertices = 2
        part.addChild(ls)
        s.addChild(part)
        for attr, color in (("m_start", (1.0, 1.0, 1.0)), ("m_end", _GIZMO_ACTIVE)):
            part = coin.SoSeparator()
            m = coin.SoMaterial()
            m.diffuseColor = color
            part.addChild(m)
            st = coin.SoDrawStyle()
            st.pointSize = 9
            part.addChild(st)
            co = coin.SoCoordinate3()
            part.addChild(co)
            ps = coin.SoPointSet()
            ps.numPoints = 1
            part.addChild(ps)
            setattr(self, attr, co)
            setattr(self, attr + "_mat", m)
            s.addChild(part)
        self.measure_label = _Label(coin, "LEFT")
        s.addChild(self.measure_label.node)
        self.measure.addChild(s)
        self.sep.addChild(self.measure)

    def _build_protractor(self):
        """The dial drawn while a ring is dragged: ticks every 5 degrees, the starting line, the swept arc, the current
        line with the angle written next to it, and the new heading of the point. Gizmo units, centred on the point."""
        coin = self._coin

        def part(color, width=1.0, point=0.0):
            s = coin.SoSeparator()
            m = coin.SoMaterial()
            m.diffuseColor = color
            s.addChild(m)
            st = coin.SoDrawStyle()
            st.lineWidth = width
            if point:
                st.pointSize = point
            s.addChild(st)
            co = coin.SoCoordinate3()
            s.addChild(co)
            self._last_style = st
            return s, m, co

        self.turn_vis = coin.SoSwitch()
        self.turn_vis.whichChild = getattr(coin, "SO_SWITCH_NONE", -1)
        root = coin.SoSeparator()
        self.turn_vis.addChild(root)

        s, _, self.circle_co = part((0.6, 0.6, 0.6), 1.0)
        ls = coin.SoLineSet()
        ls.numVertices = 73
        s.addChild(ls)
        root.addChild(s)

        s, _, self.tick_co = part((0.85, 0.85, 0.85), 2.0)
        self.tick_ls = coin.SoLineSet()
        s.addChild(self.tick_ls)
        root.addChild(s)

        s, _, self.zero_co = part((1.0, 1.0, 1.0), 2.0)
        ls = coin.SoLineSet()
        ls.numVertices = 2
        s.addChild(ls)
        root.addChild(s)

        s, _, self.arc_co = part(_GIZMO_ACTIVE, 8.0)
        self.arc_ls = coin.SoLineSet()
        s.addChild(self.arc_ls)
        root.addChild(s)

        s, _, self.cur_co = part(_GIZMO_ACTIVE, 3.0)
        ls = coin.SoLineSet()
        ls.numVertices = 2
        s.addChild(ls)
        root.addChild(s)

        s, _, self.dot_co = part(_GIZMO_ACTIVE, 1.0, point=13.0)
        ps = coin.SoPointSet()
        ps.numPoints = 1
        s.addChild(ps)
        root.addChild(s)

        self.guide_vis = coin.SoSwitch()              # a long line in the colour of the axis the new heading is parallel to
        self.guide_vis.whichChild = getattr(coin, "SO_SWITCH_NONE", -1)
        s, self.guide_mat, self.guide_co = part((1.0, 1.0, 1.0), 2.0)
        ls = coin.SoLineSet()
        ls.numVertices = 2
        s.addChild(ls)
        self.guide_vis.addChild(s)
        root.addChild(self.guide_vis)

        self.head_vis = coin.SoSwitch()               # the new heading of the point
        self.head_vis.whichChild = getattr(coin, "SO_SWITCH_NONE", -1)
        hs = coin.SoSeparator()
        s, self.head_mat, self.head_co = part(_GHOST_COLOR, 5.0)
        self.head_style = self._last_style
        ls = coin.SoLineSet()
        ls.numVertices = 2
        s.addChild(ls)
        hs.addChild(s)
        s, self.tip_mat, self.tip_co = part(_GHOST_COLOR, 1.0, point=12.0)
        self.tip_style = self._last_style
        ps = coin.SoPointSet()
        ps.numPoints = 1
        s.addChild(ps)
        hs.addChild(s)
        self.head_vis.addChild(hs)
        root.addChild(self.head_vis)

        self.marks = {}                               # dots on the dial where the heading would be parallel to an axis
        for name, _dv, ax in _AXIS_DIRS:
            sw = coin.SoSwitch()
            sw.whichChild = getattr(coin, "SO_SWITCH_NONE", -1)
            s, _m, co = part(_GIZMO_COLORS[ax], 1.0, point=9.0)
            style = self._last_style
            ps = coin.SoPointSet()
            ps.numPoints = 1
            s.addChild(ps)
            lsw = coin.SoSwitch()
            lsw.whichChild = getattr(coin, "SO_SWITCH_ALL", -3)
            lab = _Label(coin, "CENTER", 14)
            lsw.addChild(lab.node)
            s.addChild(lsw)
            sw.addChild(s)
            root.addChild(sw)
            self.marks[name] = {"sw": sw, "co": co, "style": style, "label": lab, "lsw": lsw}

        self.dial_label = _Label(coin, "CENTER")      # the angle as text
        root.addChild(self.dial_label.node)
        self.sep.addChild(self.turn_vis)

    def _dial_dir(self, rad):
        """Direction in the ring plane, `rad` radians from where the drag started."""
        u, v, a0 = self._dial
        return u * math.cos(a0 + rad) + v * math.sin(a0 + rad)

    def _turn_begin_visual(self, u, v, a0, heading=None):
        coin = self._coin
        none, every = getattr(coin, "SO_SWITCH_NONE", -1), getattr(coin, "SO_SWITCH_ALL", -3)
        self._dial = (u, v, a0)
        r = _DIAL_RADIUS
        self.circle_co.point.setValues(0, 73, [tuple(self._dial_dir(math.radians(5 * k)) * r) for k in range(73)])
        pts, counts = [], []
        for k in range(72):
            e = self._dial_dir(math.radians(5 * k))
            length = 0.16 if k % 18 == 0 else (0.10 if k % 3 == 0 else 0.055)    # 90, 15 and 5 degrees
            pts += [tuple(e * r), tuple(e * (r + length))]
            counts.append(2)
        self.tick_co.point.setValues(0, len(pts), pts)
        self.tick_ls.numVertices.setValues(0, len(counts), counts)
        self.zero_co.point.setValues(0, 2, [(0, 0, 0), tuple(self._dial_dir(0.0) * (r + 0.2))])
        # coloured dots where turning this ring makes the heading parallel to X, Y or Z
        right, up = self._cam_axes()
        px = self._dpr() / self.size_px()
        found = dict(parallel_angles(heading, u.cross(v))) if heading is not None else {}
        for name, m in self.marks.items():
            theta = found.get(name)
            if theta is None:
                m["sw"].whichChild = none
                continue
            e = self._dial_dir(math.radians(theta))
            m["co"].point.set1Value(0, tuple(e * r))
            m["label"].set([name], e * (r + 0.3), right, up, px)
            m["sw"].whichChild = every
        self.head_vis.whichChild = none
        self.guide_vis.whichChild = none
        self.turn_vis.whichChild = every
        self._turn_update_visual(0, heading, None)

    def _turn_update_visual(self, deg, heading, problem):
        """Redraw the dial for a turn of `deg` degrees. Returns (axis name, degrees apart, ...) of the axis the new
        heading is closest to, or None."""
        coin = self._coin
        none, every = getattr(coin, "SO_SWITCH_NONE", -1), getattr(coin, "SO_SWITCH_ALL", -3)
        r = _DIAL_RADIUS
        rad = math.radians(deg)
        steps = max(2, int(abs(deg) / 3.0) + 2)
        arc = [tuple(self._dial_dir(rad * i / (steps - 1.0)) * r) for i in range(steps)]
        self.arc_co.point.setValues(0, steps, arc)
        self.arc_ls.numVertices = steps
        cur = self._dial_dir(rad)
        self.cur_co.point.setValues(0, 2, [(0, 0, 0), tuple(cur * (r + 0.2))])
        self.dot_co.point.set1Value(0, tuple(cur * r))
        info = _axis_alignment(heading) if heading is not None else None
        exact = info is not None and info[1] < _PARALLEL_TOL and not problem
        near = info is not None and not exact and info[1] < _PARALLEL_NEAR and not problem
        lines = [(_angle_str(deg, self.angle_step, True) + "\u00b0") if deg else "0\u00b0"]
        if exact:
            lines.append("parallel to %s" % info[0])
        elif near:
            lines.append("%.1f\u00b0 from %s" % (info[1], info[0]))
        right, up = self._cam_axes()
        self.dial_label.set(lines, cur * (r + 0.42), right, up, self._dpr() / self.size_px(),
                            color=_PARALLEL_TEXT if exact else (1.0, 1.0, 1.0))
        color = _GHOST_BAD if problem else (_GIZMO_COLORS[info[2]] if exact else _GHOST_COLOR)
        self.head_mat.diffuseColor = color
        self.tip_mat.diffuseColor = color
        self.head_style.lineWidth = 9 if exact else 5
        self.tip_style.pointSize = 17 if exact else 12
        if exact:                                       # a long line in the axis colour, like a CAD alignment guide
            d = Vector(*info[3])
            self.guide_co.point.setValues(0, 2, [tuple(d * -2.4), tuple(d * 2.4)])
            self.guide_mat.diffuseColor = _GIZMO_COLORS[info[2]]
            self.guide_vis.whichChild = every
        else:
            self.guide_vis.whichChild = none
        for name, m in self.marks.items():              # the dot of the axis we are parallel to grows; its label moves to the dial text
            hot = exact and info[0] == name
            m["style"].pointSize = 16 if hot else 9
            m["lsw"].whichChild = none if hot else every
        if heading is None:
            self.head_vis.whichChild = none
        else:
            tip = _unit(heading) * 1.25
            self.head_co.point.setValues(0, 2, [(0, 0, 0), tuple(tip)])
            self.tip_co.point.set1Value(0, tuple(tip))
            self.head_vis.whichChild = every
        return info

    def _ghost_set(self, path, problem=None):
        coin = self._coin
        if path and not problem:
            pts = [tuple(p) for p in path]
            self.ghost_co.point.setValues(0, len(pts), pts)
            self.ghost_ls.numVertices = len(pts)
            self.ghost.whichChild = getattr(coin, "SO_SWITCH_ALL", -3)
        else:
            self.ghost.whichChild = getattr(coin, "SO_SWITCH_NONE", -1)

    def _hide_turn_visuals(self):
        none = getattr(self._coin, "SO_SWITCH_NONE", -1)
        self.ghost.whichChild = none
        self.turn_vis.whichChild = none
        self.measure.whichChild = none

    # ----- showing it
    def show(self):
        if self.visible:
            return
        self.root.addChild(self.sep)
        self.visible = True
        # The mouse is caught at the 3D view widget, before FreeCAD's navigation (rotate / pan / select) sees it.
        self._vp = self.view.graphicsView().viewport()
        size = 18 * self._vp.devicePixelRatioF()                # text is sized in device pixels
        self.dial_label.font.size = size
        self.measure_label.font.size = size
        for m in self.marks.values():
            m["label"].font.size = size * 0.75
        self._apply_style()
        self._qfilter = _make_gizmo_filter(self)
        self._vp.installEventFilter(self._qfilter)
        cam = self.view.getCameraNode()
        fields = [cam.height] if type(cam).__name__ == "SoOrthographicCamera" else [cam.position, cam.heightAngle]
        for field in fields:
            sensor = self._coin.SoFieldSensor(self._camera_changed, None)
            sensor.attach(field)
            self._sensors.append(sensor)

    def hide(self):
        if not self.visible:
            return
        self._end_drag()
        self._set_hover(None)
        self._hide_turn_visuals()
        for sensor in self._sensors:
            sensor.detach()
        self._sensors = []
        try:
            self._vp.removeEventFilter(self._qfilter)
        except RuntimeError:
            pass
        self._qfilter = None
        self.root.removeChild(self.sep)
        self.visible = False

    def destroy(self):
        self.hide()

    def set_target(self, position, rings=True, whole=False):
        """Put the gizmo on a point (mm). rings=False hides the rings (a point whose heading cannot be turned)."""
        self.center = Vector(*position)
        self.whole = whole
        self.rings.whichChild = getattr(self._coin, "SO_SWITCH_ALL", -3) if rings else getattr(self._coin, "SO_SWITCH_NONE", -1)
        self._drag = None
        self._reset_visual()
        self.show()
        self.refresh_scale()

    def _end_drag(self):
        """Forget the drag in progress and give the keyboard back."""
        self._drag = None
        if self._keys_grabbed:
            self._keys_grabbed = False
            try:
                self._vp.releaseKeyboard()
            except RuntimeError:
                pass

    def _abort(self):
        self._end_drag()
        self._reset_visual()

    def _cancel(self):
        """Esc during a drag: nothing is changed."""
        self._end_drag()
        self._swallow_release = True
        self._reset_visual()
        self._status("Cancelled - nothing was changed.")

    def _reset_visual(self):
        coin = self._coin
        self._set_hover(None)
        self._hide_turn_visuals()
        self.pos.translation = tuple(self.center)
        for rot in self.turns.values():
            rot.rotation = coin.SbRotation(coin.SbVec3f(0, 0, 1), 0.0)
        for hid, mat in self.mats.items():
            mat.diffuseColor = _GIZMO_COLORS[hid[-1] if hid != "center" else "center"]

    def size_px(self):
        """Length of an arrow on the screen, in device pixels."""
        return _GIZMO_PIXELS * self.percent / 100.0 * self._dpr()

    def refresh_scale(self):
        s = self.size_px() * self.units_per_pixel()
        self._s = s
        self.scale.scaleFactor = (s, s, s)

    def set_size(self, percent):
        """Make the gizmo `percent` per cent of its normal size."""
        self.percent = max(_GIZMO_SIZES[0], min(_GIZMO_SIZES[1], int(percent)))
        if self.visible and self._drag is None:
            self._apply_style()
            self.refresh_scale()

    def _apply_style(self):
        """What is measured in pixels and so depends on the screen: the thickness of the rings."""
        width = max(4.0, 3.0 * self._dpr())
        for style in self._ring_styles:
            style.lineWidth = width

    def _camera_changed(self, _data, _sensor):
        if self.visible and self._drag is None:
            self.refresh_scale()

    # ----- mouse geometry
    @staticmethod
    def _axis_param(c, a, o, d):
        """Where along the line c + t*a the mouse ray o + s*d comes closest (None if the line points at the camera)."""
        b = a.dot(d)
        if 1.0 - b * b < 1e-4:
            return None
        w = c - o
        s = (d.dot(w) - b * a.dot(w)) / (1.0 - b * b)
        return s * b - a.dot(w)

    @staticmethod
    def _plane_hit(c, n, o, d):
        den = n.dot(d)
        if abs(den) < 1e-6:
            return None
        return o + d * (n.dot(c - o) / den)

    def _ring_angle(self, c, n, u, v, o, d):
        if abs(n.dot(d)) < 0.12:         # the ring is seen edge-on: the mouse cannot follow it
            return None
        hit = self._plane_hit(c, n, o, d)
        if hit is None:
            return None
        r = hit - c
        return math.atan2(r.dot(v), r.dot(u))

    def _pick(self, x, y):
        """The handle under the mouse (x, y in Coin pixels), or None.

        This is done on the screen, not in 3D: every handle is grabbed from a few pixels around where it is drawn
        (_GRAB_PIXELS), so the thin rings and the tip of an arrow are as easy to hit as the ball, and the nearest one wins."""
        coin = self._coin
        size, grab = self.size_px(), _GRAB_PIXELS * self._dpr()
        c, s = self.center, self._s
        here = (x, y)
        (cx, cy), = self.project_all([c])
        best, best_score = None, 0.0
        score = math.hypot(x - cx, y - cy) - (_BALL_RADIUS * size + grab)
        if score < best_score:
            best, best_score = "center", score
        for ax in "XYZ":
            a = Vector(*_GIZMO_AXES[ax][0])
            p0, p1 = self.project_all([c + a * (0.2 * s), c + a * s])
            score = _seg_dist(here, p0, p1) - (0.06 * size + grab)
            if score < best_score:
                best, best_score = "move:" + ax, score
        if self.rings.whichChild.getValue() != getattr(coin, "SO_SWITCH_NONE", -1):
            for ax in "XYZ":
                _, u, v = (Vector(*q) for q in _GIZMO_AXES[ax])
                n = 48
                pts = self.project_all([c + (u * math.cos(2 * math.pi * k / n) + v * math.sin(2 * math.pi * k / n)) * (_GIZMO_RING * s)
                                        for k in range(n + 1)])
                score = min(_seg_dist(here, pts[k], pts[k + 1]) for k in range(n)) - grab
                if score < best_score:
                    best, best_score = "turn:" + ax, score
        return best

    def _hint(self, hid):
        kind, _, name = hid.partition(":")
        what = "the whole wire" if self.whole else "the point"
        keys = " Shift = fine, arrow keys nudge, Esc cancels."
        if kind == "move":
            return "Drag to move %s along %s.%s" % (what, name, keys)
        if kind == "turn":
            return "Drag to turn the heading around %s in steps of %s deg.%s" % (name, _angle_str(self.angle_step, self.angle_step), keys)
        return "Drag to move %s across the view.%s" % (what, keys)

    def _set_hover(self, hid):
        """Show which handle a click would take: it gets lighter, the cursor becomes a hand, the status bar says what it does."""
        if hid == self._hover:
            return
        from PySide import QtCore
        old = self._hover
        self._hover = hid
        if old is not None and old in self.mats and (self._drag is None or self._drag.get("hid") != old):
            self.mats[old].diffuseColor = _GIZMO_COLORS[old[-1] if old != "center" else "center"]
        try:
            if hid is not None:
                base = _GIZMO_COLORS[hid[-1] if hid != "center" else "center"]
                self.mats[hid].diffuseColor = tuple(q + (1.0 - q) * _HOVER_LIGHTEN for q in base)
                self._vp.setCursor(QtCore.Qt.PointingHandCursor)
                self._cursor_set = True
                self._status(self._hint(hid))
            elif self._cursor_set:
                self._vp.unsetCursor()
                self._cursor_set = False
        except RuntimeError:
            pass

    # ----- the mouse (Qt events on the 3D view widget)
    def _device_pos(self, ev):
        """Qt mouse position -> Coin pixels (device pixels, origin bottom left)."""
        p = ev.position() if hasattr(ev, "position") else ev.pos()
        dpr = self._vp.devicePixelRatioF()
        _w, h = self.view.getSize()
        return p.x() * dpr, h - p.y() * dpr

    def _qt_event(self, ev):
        """Returns True when the event belongs to the gizmo (so FreeCAD must not also use it)."""
        from PySide import QtCore
        kind = ev.type()
        if kind == QtCore.QEvent.MouseButtonRelease and self._swallow_release and ev.button() == QtCore.Qt.LeftButton:
            self._swallow_release = False            # the release that ends a drag cancelled with Esc
            return True
        if kind == QtCore.QEvent.MouseButtonPress:
            if ev.button() != QtCore.Qt.LeftButton or ev.modifiers() != QtCore.Qt.NoModifier:
                return False
            self._swallow_release = False
            x, y = self._device_pos(ev)
            hid = self._pick(x, y)
            if hid is None:
                return False
            self._begin(hid, x, y)
            return self._drag is not None
        if self._drag is None:
            if kind == QtCore.QEvent.MouseMove:
                if ev.buttons() == QtCore.Qt.NoButton:
                    self._set_hover(self._pick(*self._device_pos(ev)))
                else:
                    self._set_hover(None)
            elif kind == QtCore.QEvent.Leave:
                self._set_hover(None)
            return False
        if kind in (QtCore.QEvent.KeyPress, QtCore.QEvent.ShortcutOverride):
            key = ev.key()
            keys = (QtCore.Qt.Key_Escape, QtCore.Qt.Key_Left, QtCore.Qt.Key_Right, QtCore.Qt.Key_Up, QtCore.Qt.Key_Down)
            if key not in keys:
                return False
            if kind == QtCore.QEvent.ShortcutOverride:
                ev.accept()                          # FreeCAD must not use these keys as shortcuts now
                return True
            if key == QtCore.Qt.Key_Escape:
                self._cancel()
            elif self._drag["kind"] == "turn":
                sign = 1 if key in (QtCore.Qt.Key_Right, QtCore.Qt.Key_Up) else -1
                self._set_turn(self._drag, math.radians(self._drag["deg"] + sign * self.angle_step))
            elif self._drag["kind"] == "move":
                step = 0.1 if key in (QtCore.Qt.Key_Right, QtCore.Qt.Key_Up) else -0.1
                self._set_move(self._drag, round(self._drag["raw"], 1) + step)
            return True
        if kind == QtCore.QEvent.MouseMove:
            fine = bool(ev.modifiers() & QtCore.Qt.ShiftModifier)
            if not (ev.buttons() & QtCore.Qt.LeftButton):
                self._abort()                        # the button is no longer down (its release got lost): change nothing
                self._status("The mouse button was released elsewhere - nothing was changed.")
                return False
            self._move(*self._device_pos(ev), fine=fine)
            return True
        if kind == QtCore.QEvent.MouseButtonRelease and ev.button() == QtCore.Qt.LeftButton:
            fine = bool(ev.modifiers() & QtCore.Qt.ShiftModifier)
            self._finish(*self._device_pos(ev), fine=fine)
            return True
        return False                                 # paint, hover, timer ... events are none of our business

    def _begin(self, hid, x, y):
        coin = self._coin
        o, r = self._ray(x, y)
        c = Vector(self.center)
        kind, _, name = hid.partition(":")
        if kind == "move":
            a = Vector(*_GIZMO_AXES[name][0])
            t0 = self._axis_param(c, a, o, r)
            if t0 is None:
                self._status("That arrow points straight at you - turn the view first.")
                return
            drag = {"kind": "move", "name": name, "C": c, "axis": a, "t0": t0, "amount": 0.0, "last": t0, "raw": 0.0,
                    "shown": 0.0, "preview": None, "problem": None, "mouse": (x, y)}
        elif kind == "turn":
            n, u, v = (Vector(*q) for q in _GIZMO_AXES[name])
            a0 = self._ring_angle(c, n, u, v, o, r)
            if a0 is None:
                self._status("That ring is seen edge-on - turn the view first.")
                return
            drag = {"kind": "turn", "name": name, "C": c, "axis": n, "u": u, "v": v, "last": a0, "a0": a0,
                    "raw": 0.0, "deg": 0, "amount": 0.0, "preview": None}
        else:
            normal = Vector(*self.view.getViewDirection())
            p0 = self._plane_hit(c, normal, o, r)
            if p0 is None:
                return
            drag = {"kind": "plane", "name": "view", "C": c, "normal": normal, "p0": p0, "amount": Vector(0, 0, 0),
                    "last": p0, "raw": Vector(0, 0, 0), "shown": (0.0, 0.0, 0.0), "preview": None, "problem": None,
                    "mouse": (x, y)}
        self._drag = drag
        drag["hid"] = hid
        self._hover = None
        self.mats[hid].diffuseColor = _GIZMO_ACTIVE
        self._swallow_release = False
        try:
            self._vp.grabKeyboard()                  # Esc and the arrow keys work while the mouse button is down
            self._keys_grabbed = True
        except RuntimeError:
            pass
        if drag["kind"] == "turn":
            if self.preview_factory is not None:
                try:
                    drag["preview"] = self.preview_factory()
                except Exception as e:
                    FreeCAD.Console.PrintWarning("Wire gizmo preview: %s\n" % e)
            h0 = None
            if drag["preview"] is not None:
                try:
                    h0 = drag["preview"](drag["axis"], 0.0)["heading"]
                except Exception as e:
                    FreeCAD.Console.PrintWarning("Wire gizmo preview: %s\n" % e)
            self._turn_begin_visual(drag["u"], drag["v"], a0, h0)
            self._status("Turn around %s: 0 deg   (steps of %s deg, move away from the ring for finer control, Shift = fine, "
                         "arrow keys = one step, Esc = cancel)" % (name, _angle_str(self.angle_step, self.angle_step)))
        else:
            if self.move_preview_factory is not None:
                try:
                    drag["preview"] = self.move_preview_factory()
                except Exception as e:
                    FreeCAD.Console.PrintWarning("Wire gizmo preview: %s\n" % e)
            self.measure.whichChild = getattr(coin, "SO_SWITCH_ALL", -3)
            self._measure_update(drag, Vector(0, 0, 0))
            self._status("Move: 0.00 mm   (Shift = fine%s, Esc = cancel)"
                         % (", arrow keys = 0.1 mm" if drag["kind"] == "move" else ""))

    def _move(self, x, y, fine=False):
        coin = self._coin
        d = self._drag
        o, r = self._ray(x, y)
        c = d["C"]
        if d["kind"] == "move":
            t = self._axis_param(c, d["axis"], o, r)
            if t is None:
                return
            step = t - d["last"]
            d["last"] = t
            d["mouse"] = (x, y)
            self._set_move(d, d["raw"] + step * (_FINE_FACTOR if fine else 1.0))
        elif d["kind"] == "plane":
            hit = self._plane_hit(c, d["normal"], o, r)
            if hit is None:
                return
            step = hit - d["last"]
            d["last"] = hit
            d["mouse"] = (x, y)
            self._set_move(d, d["raw"] + step * (_FINE_FACTOR if fine else 1.0))
        else:
            a = self._ring_angle(c, d["axis"], d["u"], d["v"], o, r)
            if a is None:
                return
            step = (a - d["last"] + math.pi) % (2 * math.pi) - math.pi
            d["last"] = a
            self._set_turn(d, d["raw"] + step * (_FINE_FACTOR if fine else 1.0))

    def _set_move(self, d, raw):
        """The handle is `raw` mm away from where it started; what will be applied is that rounded to 0.01 mm."""
        d["raw"] = raw
        if d["kind"] == "move":
            amount = round(raw, 2) + 0.0
            key = amount
            delta = d["axis"] * amount
        else:
            delta = Vector(*(round(q, 2) + 0.0 for q in raw))
            amount, key = delta, tuple(delta)
        if key != d["shown"]:
            d["shown"] = key
            d["amount"] = amount
            self.pos.translation = tuple(d["C"] + delta)
            path, problem, note = None, None, ""
            if d["preview"] is not None:
                try:
                    res = d["preview"](tuple(delta))
                    path, problem, note = res["path"], res["problem"], res.get("note") or ""
                except Exception as e:
                    problem = str(e)
            d["problem"] = problem
            self._ghost_set(path, problem)
            if d["kind"] == "move":
                text = "Move along %s: %+.2f mm" % (d["name"], amount)
            else:
                text = "Move across the view: X %+.2f  Y %+.2f  Z %+.2f mm" % tuple(delta)
            if problem:
                text += " - NOT POSSIBLE: " + problem
            elif note:
                text += " - " + note
            self._status(text + "   (Shift = fine, Esc = cancel)")
        self._measure_update(d, delta)

    def _measure_update(self, d, delta):
        """The line from the old to the new position and the label next to the mouse."""
        c = d["C"]
        p = c + delta
        problem = d["problem"]
        color = _GHOST_BAD if problem else _GIZMO_ACTIVE
        self.m_co.point.setValues(0, 2, [tuple(c), tuple(p)])
        self.m_start.point.set1Value(0, tuple(c))
        self.m_end.point.set1Value(0, tuple(p))
        self.m_mat.diffuseColor = color
        self.m_end_mat.diffuseColor = color
        pre = "Wire: " if self.whole else ""
        if d["kind"] == "move":
            lines = ["%s%s %+.2f mm" % (pre, d["name"], d["amount"])]
        else:
            lines = ["%s%.2f mm" % (pre, delta.Length), "dX %+.2f   dY %+.2f   dZ %+.2f" % tuple(delta)]
        lines.append("now (%.2f, %.2f, %.2f)" % tuple(p))
        if problem:
            lines.insert(0, "NOT POSSIBLE")
        right, up = self._cam_axes()
        upp = self.units_per_pixel() * self._dpr()                 # mm per screen (logical) pixel
        o, r = self._ray(*d["mouse"])
        hit = self._plane_hit(p, Vector(*self.view.getViewDirection()), o, r) or p
        anchor = hit + right * (upp * 16) + up * (upp * 10)
        self.measure_label.set(lines, anchor, right, up, upp, color=(1.0, 0.45, 0.45) if problem else (1.0, 1.0, 1.0))

    def _set_turn(self, d, raw):
        """The ring is at `raw` radians; the turn that will be applied is that, rounded to a multiple of the ring step."""
        coin = self._coin
        d["raw"] = raw
        step = self.angle_step
        deg = round(round(math.degrees(raw) / step) * step, 6) + 0.0
        if abs(deg - d["deg"]) < 1e-9:
            return
        d["deg"] = deg
        d["amount"] = math.radians(deg)
        self.turns[d["name"]].rotation = coin.SbRotation(coin.SbVec3f(*d["axis"]), d["amount"])
        heading, path, problem, note = None, None, None, ""
        if d["preview"] is not None:
            try:
                res = d["preview"](d["axis"], float(deg))
                heading, path, problem, note = res["heading"], res["path"], res["problem"], res.get("note") or ""
            except Exception as e:
                problem = str(e)
        info = self._turn_update_visual(deg, heading, problem)
        self._ghost_set(path, problem)
        text = "Turn around %s: %s deg" % (d["name"], _angle_str(deg, step, True))
        if info is not None and info[1] < _PARALLEL_TOL and not problem:
            text += " - parallel to %s axis" % info[0]
        if problem:
            text += " - NOT POSSIBLE: " + problem
        elif note:
            text += " - " + note
        self._status(text + "   (Shift = fine, arrow keys = %s deg, Esc = cancel)" % _angle_str(step, step))

    def _finish(self, x, y, fine=False):
        self._move(x, y, fine)
        d = self._drag
        self._end_drag()
        if d["kind"] == "move":
            amount = round(d["amount"], 2)
            delta = d["axis"] * amount
            nothing = abs(amount) < 0.01
        elif d["kind"] == "plane":
            delta = Vector(*(round(q, 2) for q in d["amount"]))
            nothing = delta.Length < 0.01
        else:
            angle = float(d["deg"])                  # a multiple of the ring step
            nothing = abs(math.remainder(angle, 360.0)) < 1e-9
        if nothing:
            self._reset_visual()
            return
        if d["kind"] == "turn":
            self._later(self.on_turn, d["axis"], angle)
        else:
            self._later(self.on_move, delta)

    def _later(self, fn, *args):
        """Run the change after the mouse event is over (the scene must not be changed while it is being walked)."""
        from PySide import QtCore

        def run():
            try:
                fn(*args)
                self._reset_visual()                 # the dial and the dashed preview go away once the change is drawn
            except Exception as e:
                self._status("Gizmo: %s" % e)
                FreeCAD.Console.PrintWarning("Wire gizmo: %s\n" % e)
                self._reset_visual()
                try:
                    from PySide import QtWidgets
                    import FreeCADGui
                    QtWidgets.QMessageBox.warning(FreeCADGui.getMainWindow(), "Harness Designer", str(e))
                except Exception:
                    pass
        QtCore.QTimer.singleShot(0, run)

    def _status(self, text):
        try:
            import FreeCADGui
            FreeCADGui.getMainWindow().statusBar().showMessage(text, 4000)
        except Exception:
            pass


class CollisionMarks(_ViewMath):
    """Drawn over the 3D view where wires collide: the overlapping volume, a line between the nearest points of the two
    wires and a label. Nothing is added to the document."""

    def __init__(self, view):
        from pivy import coin
        self._coin = coin
        self.view = view
        self.root = view.getSceneGraph()
        self._vp = view.graphicsView().viewport()
        self.center = Vector(0, 0, 0)
        self.visible = False
        self._labels = []
        self._sensors = []
        self.sep = coin.SoSeparator()
        self.sep.setName(COLLISION_NAME)
        depth = coin.SoDepthBuffer()
        depth.test = False                      # drawn over the wires, so a collision inside the bundle can be seen
        self.sep.addChild(depth)
        light = coin.SoLightModel()
        light.model = coin.SoLightModel.BASE_COLOR
        self.sep.addChild(light)
        self.body = coin.SoSeparator()
        self.sep.addChild(self.body)

    def show(self, collisions):
        coin = self._coin
        dpr = self._dpr()
        self.body.removeAllChildren()
        self._labels = []
        for c in collisions:
            color = _TIGHT_COLOR if c["state"] == "close" else _COLLIDE_COLOR
            if c.get("shape") is not None:                       # the volume the two wires share
                try:
                    verts, faces = c["shape"].tessellate(0.05)
                    s = coin.SoSeparator()
                    m = coin.SoMaterial()
                    m.diffuseColor = (1.0, 1.0, 1.0)            # white shows on a wire of any colour
                    m.transparency = 0.2
                    s.addChild(m)
                    co = coin.SoCoordinate3()
                    co.point.setValues(0, len(verts), [tuple(v) for v in verts])
                    s.addChild(co)
                    index = []
                    for f in faces:
                        index += [f[0], f[1], f[2], -1]
                    fs = coin.SoIndexedFaceSet()
                    fs.coordIndex.setValues(0, len(index), index)
                    s.addChild(fs)
                    self.body.addChild(s)
                except Exception:
                    pass
            s = coin.SoSeparator()                              # a ring around the spot, as big on the screen whatever the zoom
            ring = coin.SoCoordinate3()
            s.addChild(ring)
            for width, ring_color in ((max(7.0, 4.0 * dpr + 3.0), (0.0, 0.0, 0.0)), (max(3.0, 2.0 * dpr), color)):
                m = coin.SoMaterial()
                m.diffuseColor = ring_color
                s.addChild(m)
                st = coin.SoDrawStyle()
                st.lineWidth = width
                s.addChild(st)
                ls = coin.SoLineSet()
                ls.numVertices = _RING_POINTS + 1
                s.addChild(ls)
            self.body.addChild(s)
            lab = _Label(coin, "LEFT")
            lab.font.size = 17 * dpr
            self.body.addChild(lab.node)
            self._labels.append((lab, Vector(*c["point"]), [collision_title(c), collision_what(c)], ring, color))
        if not self.visible:
            self.root.addChild(self.sep)
            self.visible = True
            cam = self.view.getCameraNode()
            fields = [cam.orientation, cam.height] if type(cam).__name__ == "SoOrthographicCamera" else \
                [cam.orientation, cam.position, cam.heightAngle]
            for field in fields:
                sensor = coin.SoFieldSensor(lambda _d, _s: self._relayout(), None)
                sensor.attach(field)
                self._sensors.append(sensor)
        self._relayout()

    def _relayout(self):
        """Labels sit a few pixels up and to the right of their point, whatever the zoom."""
        right, up = self._cam_axes()
        for lab, point, lines, ring, color in self._labels:
            self.center = point
            px = self.units_per_pixel() * self._dpr()           # mm per (logical) pixel there
            r = px * _RING_RADIUS
            ring.point.setValues(0, _RING_POINTS + 1, [tuple(point + (right * math.cos(2 * math.pi * k / _RING_POINTS) +
                                                                         up * math.sin(2 * math.pi * k / _RING_POINTS)) * r)
                                                       for k in range(_RING_POINTS + 1)])
            lab.set(lines, point + right * (px * (_RING_RADIUS + 6)) + up * (px * 6), right, up, px, (1.0, 1.0, 1.0))

    def clear(self):
        if not self.visible:
            return
        for sensor in self._sensors:
            sensor.detach()
        self._sensors = []
        self.root.removeChild(self.sep)
        self.visible = False
        self.body.removeAllChildren()
        self._labels = []

    destroy = clear


PATH_NAME = "WirePathLine"
_PATH_STEP = 0.5                       # mm between the points of the drawn centre line
_PATH_COLOR = (0.0, 0.95, 1.0)         # the wire that is chosen in the window
_PATH_OTHER = (1.0, 1.0, 1.0)          # the other wires
_PATH_CACHE = {}                       # (document, wire) -> (what the line was made from, its points)


def wire_path_lines(doc, harness, only=None):
    """The centre line of the wires, ready to draw: [(wire id, [(x, y, z), ...])] in the document's coordinates.
    With `only`, just that wire. Wires that cannot be built are left out."""
    wires = read_harness(harness.Params, [s.Name for s in harness.PointSheets])[0]
    base = harness.Placement
    paths = twist_paths(doc, harness, wires)[0]
    out, seen = [], set()
    for w in wires:
        wid = str(w["id"])
        seen.add(wid)
        if (only is not None and wid != str(only)) or "error" in w:
            continue
        tp = paths.get(wid)
        key = (repr(w), tp["key"] if tp is not None else None, tuple(base.Base), tuple(base.Rotation.Q))
        cached = _PATH_CACHE.get((doc.Name, wid))
        if cached is not None and cached[0] == key:
            pts = cached[1]
        else:
            try:
                place = base * FreeCAD.Placement(Vector(w["x"], w["y"], w["z"]), FreeCAD.Rotation())
                if tp is not None:
                    pts = [tuple(place.multVec(p)) for p in tp["pts"]]
                else:
                    line = Part.Wire(spline_path(w)[0])
                    n = max(2, int(math.ceil(line.Length / _PATH_STEP)) + 1)
                    pts = [tuple(place.multVec(p)) for p in line.discretize(Number=n)]
            except Exception:
                continue
            _PATH_CACHE[(doc.Name, wid)] = (key, pts)
        out.append((wid, pts))
    if only is None:
        for k in [k for k in _PATH_CACHE if k[0] == doc.Name and k[1] not in seen]:
            del _PATH_CACHE[k]
    return out


class PathLine:
    """The centre line of the wires, drawn over the 3D view: the real line through the middle of each wire, which the wire
    is swept along. Nothing is added to the document."""

    def __init__(self, view):
        from pivy import coin
        self._coin = coin
        self.view = view
        self.root = view.getSceneGraph()
        self._vp = view.graphicsView().viewport()
        self.visible = False
        self.sep = coin.SoSeparator()
        self.sep.setName(PATH_NAME)
        depth = coin.SoDepthBuffer()
        depth.test = False                      # drawn over the wire, which would hide a line inside it
        self.sep.addChild(depth)
        light = coin.SoLightModel()
        light.model = coin.SoLightModel.BASE_COLOR
        self.sep.addChild(light)
        self.body = coin.SoSeparator()
        self.sep.addChild(self.body)

    def show(self, lines, chosen=None):
        """Draw `lines` (see wire_path_lines). The line of the `chosen` wire is thicker and has its own colour."""
        coin = self._coin
        dpr = self._vp.devicePixelRatioF()
        self.body.removeAllChildren()
        order = [l for l in lines if str(l[0]) != str(chosen)] + [l for l in lines if str(l[0]) == str(chosen)]
        for wid, pts in order:
            main = str(wid) == str(chosen)
            s = coin.SoSeparator()
            co = coin.SoCoordinate3()
            co.point.setValues(0, len(pts), [tuple(p) for p in pts])
            s.addChild(co)
            thin = max(2.0, 1.5 * dpr)
            width = thin + (1.5 * dpr if main else 0.0)
            for w_, color in ((width + 2.0 * dpr, (0.0, 0.0, 0.0)), (width, _PATH_COLOR if main else _PATH_OTHER)):
                m = coin.SoMaterial()
                m.diffuseColor = color
                s.addChild(m)
                st = coin.SoDrawStyle()
                st.lineWidth = w_
                s.addChild(st)
                ls = coin.SoLineSet()
                ls.numVertices = len(pts)
                s.addChild(ls)
            self.body.addChild(s)
        if not self.visible:
            self.root.addChild(self.sep)
            self.visible = True

    def clear(self):
        if not self.visible:
            return
        self.root.removeChild(self.sep)
        self.visible = False
        self.body.removeAllChildren()

    destroy = clear


def focus_view(view, point, height=30.0):
    """Look at `point` from the direction the camera looks from now, with about `height` mm in the picture."""
    cam = view.getCameraNode()
    q = cam.orientation.getValue().getValue()
    forward = Rotation(q[0], q[1], q[2], q[3]).multVec(Vector(0, 0, -1))
    if type(cam).__name__ == "SoOrthographicCamera":
        dist = cam.focalDistance.getValue()
        cam.height = height
    else:
        dist = (height / 2.0) / math.tan(cam.heightAngle.getValue() / 2.0)
        cam.focalDistance = dist
    eye = point - forward * dist
    cam.position.setValue(eye.x, eye.y, eye.z)


# ---------------------------------------------------------------------------
# The "Edit a wire point" window
# ---------------------------------------------------------------------------

def curve_length(w):
    """Length of the curve through a wire's points (a free-end wire needs at least this total length)."""
    S = Vector(w["x"], w["y"], w["z"])
    pts = [Vector(0, 0, 0)] + [Vector(*q["p"]) - S for q in w["points"]]
    dirs = [None] + [Vector(*q["dir"]) if q["dir"] is not None else None for q in w["points"]]
    edges, T = _bezier_chain(pts, _start_dir(w), dirs, 1.0)
    return sum(e.Length for e in edges)


def set_wire_length(doc, harness, wid, length):
    """Set the total length (mm) of a wire. A length the wire cannot be built with is refused and undone."""
    w = _wire_dict(doc, harness, wid)
    if length <= 0:
        raise ValueError("the length must be greater than 0")
    if not any(q["kind"] == "end" for q in w["points"]):
        need = curve_length(w)
        if length < need - 1e-6:
            raise ValueError("this wire needs at least %.1f mm for the curve through its points. Choose a longer length, "
                             "or shorten the route first." % need)
    sheet = harness.Params
    cell = "G%d" % w["row"]
    old = sheet.getContents(cell)
    sheet.set(cell, _fmt(length))
    doc.recompute()
    line = [l for l in harness.WireReport if l.startswith("Wire %s:" % w["id"])]
    if line and "ERROR" in line[0]:
        sheet.set(cell, old)
        doc.recompute()
        raise ValueError(line[0])


def set_wire_direction(doc, harness, wid, direction):
    """Set the direction a wire leaves its start point in: (x, y, z), a Vector or text such as "-X".

    A direction the wire cannot be built with is refused and undone."""
    w = _wire_dict(doc, harness, wid)
    if isinstance(direction, str):
        d = parse_direction(direction)
    else:
        d = direction if isinstance(direction, Vector) else Vector(*direction)
        if d.Length < 1e-9:
            raise ValueError("the direction cannot be (0, 0, 0)")
        d = _unit(d)
    sheet = harness.Params
    _ensure_direction_header(sheet)
    cell = "J%d" % w["row"]
    old = sheet.getContents(cell)
    sheet.set(cell, "'" + format_direction(d))
    doc.recompute()
    line = [l for l in harness.WireReport if l.startswith("Wire %s:" % w["id"])]
    if line and "ERROR" in line[0]:
        if old == "":
            sheet.clear(cell)
        else:
            sheet.set(cell, old)
        doc.recompute()
        raise ValueError(line[0])


def set_wire_color(doc, harness, wid, color):
    """Set the color of a wire: a name such as red, blue, green, a code like #00aa44, or "" for an automatic color."""
    w = _wire_dict(doc, harness, wid)
    c = (color or "").strip().lstrip("'").strip()
    if c and c.lower() not in COLORS and not re.match(r"^#[0-9a-fA-F]{6}$", c):
        raise ValueError("unknown color '%s' - use a name (%s) or a code like #00aa44" % (c, ", ".join(sorted(k for k in COLORS if k != "gray"))))
    cell = "H%d" % w["row"]
    if c:
        harness.Params.set(cell, c.lower() if c.lower() in COLORS else c)
    else:
        harness.Params.clear(cell)
    doc.recompute()


def _point_items(w):
    return [[q["kind"], Vector(*q["offset"]), Vector(*q["dir"]) if q["dir"] is not None else None] for q in w["points"]]


def _rewrite_points(doc, w, items):
    """Write the points of a wire back to its sheet from row 5, in order (blank rows disappear)."""
    ps = find_sheet(doc, w["sheet"])
    last = max([q["row"] for q in w["points"]] + [POINTS_FIRST_ROW])
    for r in range(POINTS_FIRST_ROW, last + 3):
        for c in "ABCDEFGH":
            ps.clear("%s%d" % (c, r))
    fill_points(ps, [(k, (o.x, o.y, o.z), None if d is None else (d.x, d.y, d.z)) for k, o, d in items])


_MIN_POINT_GAP = 0.5            # mm: a new point is not put closer than this to the point next to it


def add_point(doc, harness, wid, row, extend=10.0):
    """Add a point `extend` mm after the point in `row`, measured along the wire (row = "start": after the start point).
    Returns the row of the new point.

    The new point lies on the wire where it runs now, and all the other points keep their positions. When the next point is
    nearer than `extend` (less than 0.5 mm further on), the new point goes halfway to it instead. After the last point the wire is
    extended by `extend` mm along that point's heading, and a free-end wire gets the extra length it needs (its Length grows). On a
    fixed-end ("end") point the new point is placed before it, `extend` mm before the end, because the end must stay last."""
    extend = float(extend)
    if extend <= 0:
        raise ValueError("the distance to the new point must be greater than 0")
    w = _wire_dict(doc, harness, wid)
    items = _point_items(w)
    rows = [q["row"] for q in w["points"]]
    idx = -1 if row == "start" else rows.index(int(row)) if int(row) in rows else None
    if idx is None:
        raise ValueError("wire %s has no point in row %s" % (wid, row))
    before_end = False
    if idx >= 0 and items[idx][0] == "end":
        idx -= 1                                           # the end stays the last point
        before_end = True
    nxt = idx + 1
    extending = nxt >= len(items)                              # after the last point: the wire is made longer on purpose
    if nxt < len(items):
        S = Vector(w["x"], w["y"], w["z"])
        pts = [Vector(0, 0, 0)] + [Vector(*q["p"]) - S for q in w["points"]]
        dirs = [None] + [Vector(*q["dir"]) if q["dir"] is not None else None for q in w["points"]]
        edge = _bezier_chain(pts, _start_dir(w), dirs, 1.0)[0][nxt]          # the stretch from the point before to the point after
        length = edge.Length
        at = length - extend if before_end else extend                          # mm along that stretch, from its first point
        if at < _MIN_POINT_GAP or at > length - _MIN_POINT_GAP:
            at = length / 2.0                                                  # no room for that distance: halfway
        new_pos = edge.valueAt(edge.getParameterByLength(at))
        new_off = new_pos - pts[nxt]
        items[nxt][1] = pts[nxt + 1] - new_pos                                # the next point keeps its own position
    else:
        heading = _start_dir(w) if idx < 0 else point_headings(w)[idx]
        new_off = _unit(heading) * extend
    items.insert(nxt, ["via", new_off, None])
    last_off = None
    if not any(k == "end" for k, _o, _d in items):
        # a free-end wire needs at least as much wire as the curve through its points. A wire that is extended past its last point
        # gets the extra length it needs; when a point in the middle makes the curve a little longer than the Length, the end of the
        # wire is pulled in instead (the Length stays), and only when that is not possible the Length grows
        w_new = _wire_from_items(w, items)
        need = curve_length(w_new)
        if need > w["total"] + 1e-6:
            fit = None if extending else _pull_end_in(w_new, nxt)
            if fit is None:
                harness.Params.set("G%d" % w["row"], _fmt(math.ceil(need * 1000.0) / 1000.0))
            else:
                for j, heading in fit[1].items():
                    items[j][2] = heading
                last_off = fit[2]
    _rewrite_points(doc, w, items)
    if last_off is not None:
        _set_offset_fine(find_sheet(doc, w["sheet"]), POINTS_FIRST_ROW + len(items) - 1, last_off)
    doc.recompute()
    return POINTS_FIRST_ROW + nxt


def delete_point(doc, harness, wid, row):
    """Delete a point; the points after it keep their positions. Returns the row to select next."""
    w = _wire_dict(doc, harness, wid)
    items = _point_items(w)
    rows = [q["row"] for q in w["points"]]
    if row == "start" or int(row) not in rows:
        raise ValueError("choose one of the wire's points (the start cannot be deleted)")
    if len(items) <= 1:
        raise ValueError("a wire needs at least one point")
    idx = rows.index(int(row))
    removed = items.pop(idx)
    if idx < len(items):
        items[idx][1] = items[idx][1] + removed[1]         # the next point keeps its position
    last_off = None
    if not any(k == "end" for k, _o, _d in items):
        try:                                                # a wire that was exactly as long as its Length stays so
            fit = _pull_end_in(_wire_from_items(w, items), min(idx, len(items) - 1))
        except Exception:
            fit = None
        if fit is not None:
            for j, heading in fit[1].items():
                items[j][2] = heading
            last_off = fit[2]
    _rewrite_points(doc, w, items)
    if last_off is not None:
        _set_offset_fine(find_sheet(doc, w["sheet"]), POINTS_FIRST_ROW + len(items) - 1, last_off)
    doc.recompute()
    return POINTS_FIRST_ROW + min(idx, len(items) - 1)


def new_straight_wire(doc, harness, like, start, length=10.0):
    """Add a new straight wire `length` mm long, defined by one point at its end, leaving in the same direction as wire `like`.

    The new wire takes its thickness and minimum bend radius from wire `like`. Returns the new wire number."""
    src_w = _wire_dict(doc, harness, like)
    new_id = next_wire_id(harness.Params)
    d0 = _start_dir(src_w)
    end = d0 * float(length)
    add_wire(doc, harness, new_id, start[0], start[1], start[2],
             [("via", (end.x, end.y, end.z), None)],
             od=src_w["od"], radius=src_w["radius"], total=length, color=_auto_color(harness.Params), direction=d0)
    return new_id


_open_dialogs = []


def build_point_dialog(doc, harness):
    """A window that stays open: pick a wire and a point, then move it, turn it, and set the wire's length and color."""
    import FreeCADGui
    from PySide import QtCore, QtWidgets
    dlg = QtWidgets.QDialog(FreeCADGui.getMainWindow())
    dlg.setWindowTitle("Harness Designer")
    dlg.setModal(False)
    form = QtWidgets.QFormLayout(dlg)

    def spin(lo, hi, value=0.0, step=1.0, dec=2):
        s = QtWidgets.QDoubleSpinBox()
        s.setDecimals(dec)
        s.setRange(lo, hi)
        s.setSingleStep(step)
        s.setValue(value)
        s.setFixedWidth(74)
        return s

    def row(*items):
        box = QtWidgets.QHBoxLayout()
        for it in items:
            box.addWidget(QtWidgets.QLabel(it) if isinstance(it, str) else it)
        box.addStretch(1)
        return box

    dlg.f_wire, dlg.f_point = QtWidgets.QComboBox(), QtWidgets.QComboBox()
    dlg.f_follow = QtWidgets.QCheckBox("Later points follow")
    dlg.f_follow.setChecked(True)
    dlg.f_follow.setToolTip("On: the points after this one move or swing with it, so the rest of the wire keeps its shape.\n"
                            "Off: only this point changes.")
    dlg.m_x, dlg.m_y, dlg.m_z = spin(-1000, 1000), spin(-1000, 1000), spin(-1000, 1000)
    dlg.b_move = QtWidgets.QPushButton("Move point")
    dlg.b_move.setToolTip("Moves the selected point by these amounts. The point before it and the point after it keep their "
                          "headings, so only the stretches next to it change. If the wire would get longer than its Length, its "
                          "last point is pulled in so the Length stays the same.")
    dlg.b_movewire = QtWidgets.QPushButton("Move whole wire")
    dlg.b_movewire.setToolTip("Moves the whole wire (its start and every point) by these amounts.")
    dlg.r_angle = spin(-180, 180, 0.0, 5.0, 3)
    dlg.r_angle.setFixedWidth(84)
    dlg.r_mode = QtWidgets.QComboBox()
    dlg.r_mode.addItems(ROTATE_MODES)
    dlg.r_mode.setToolTip("Tilt turns the heading toward a direction. Turn rotates it around the vertical axis, seen from above.")
    dlg.b_turn = QtWidgets.QPushButton("Turn")
    dlg.b_turn.setToolTip("Turns the heading of the selected point. The point before it and the point after it keep their "
                          "headings, so only the stretches next to it change. If the wire would get longer than its Length, its "
                          "last point is pulled in so the Length stays the same.")
    dlg.b_auto = QtWidgets.QPushButton("Auto heading")
    dlg.b_auto.setToolTip("Let FreeCAD choose a smooth heading for this point again. A heading you set stays fixed, and so does "
                          "the heading of a point next to one you moved or turned, until you press this.")
    dlg.len_spin = spin(1, 5000, 50.0, 5.0, 1)
    dlg.b_len = QtWidgets.QPushButton("Set")
    dlg.len_info = QtWidgets.QLabel()
    dlg.swatch = QtWidgets.QLabel()
    dlg.swatch.setFixedSize(28, 18)
    dlg.color_combo = QtWidgets.QComboBox()
    dlg.color_combo.setEditable(True)
    dlg.color_combo.addItems(sorted(k for k in COLORS if k != "gray"))
    dlg.color_combo.setToolTip("A color name, a code like #00aa44, or empty for an automatic color.")
    dlg.b_pick = QtWidgets.QPushButton("Pick...")
    dlg.b_color = QtWidgets.QPushButton("Set")
    dlg.dir_combo = QtWidgets.QComboBox()
    for label_, vec_ in DIRECTIONS:
        dlg.dir_combo.addItem(label_, vec_)
    dlg.dir_combo.setToolTip("The direction the selected wire leaves its start point in. Its first stretch follows it.")
    dlg.b_dir = QtWidgets.QPushButton("Set")
    dlg.b_dir.setToolTip("Turns the start of the selected wire to the direction on the left.")
    dlg.p_extend = spin(1, 1000, 10.0, 1.0, 1)
    dlg.p_extend.setToolTip("How far after the selected point the new point goes, measured along the wire. If the next point is nearer "
                            "than that, the new point goes halfway to it.\nAfter the last point the wire is extended by this much "
                            "(and its Length grows if it has to).")
    dlg.b_add = QtWidgets.QPushButton("Add point")
    dlg.b_add.setToolTip("Adds a point the distance on the left after the selected point, on the wire where it runs now (after the last "
                         "point the wire is extended). The other points stay put.")
    dlg.b_del = QtWidgets.QPushButton("Delete point")
    dlg.b_del.setToolTip("Deletes the selected point. The points after it keep their positions.")
    dlg.w_dx, dlg.w_dy = spin(-1000, 1000, 0.0, 0.5), spin(-1000, 1000, 0.0, 0.5)
    dlg.w_dx.setToolTip("Where the new wire starts, measured from the start of the selected wire (mm).")
    dlg.w_dy.setToolTip("Where the new wire starts, measured from the start of the selected wire (mm).")
    dlg.b_addwire = QtWidgets.QPushButton("Quick wire")
    dlg.b_addwire.setToolTip("Adds a new straight wire, 10 mm long, defined by one point at its end. It takes the thickness and the direction "
                             "of the selected wire, gets the next number and its own color, and starts at the offset on the left.")
    dlg.b_newwire = QtWidgets.QPushButton("New wire...")
    dlg.b_newwire.setToolTip("Opens a form for a new wire: where it starts, which way it leaves, its color, size and shape.\n"
                             "To start it at a vertex of another part, select that vertex in the 3D view first.")
    dlg.b_delwire = QtWidgets.QPushButton("Delete wire")
    dlg.b_delwire.setToolTip("Deletes the selected wire and its points sheet (it asks first).")
    dlg.confirm = True
    dlg.g_show = QtWidgets.QCheckBox("Show in 3D view")
    dlg.g_show.setToolTip("Arrows move the point, the ball moves it across the view, rings turn its heading.\n"
                          "Arrow / ball: a label shows the distance and the new position, a dashed line the wire afterwards.\n"
                          "Ring: turns in steps of the 'Ring step'; the dial shows the angle and a dashed line shows the wire afterwards.\n"
                          "Hold Shift for fine control; arrow keys nudge (0.1 mm or one ring step); Esc cancels.")
    dlg.g_show.setChecked(True)
    dlg.g_show.setToolTip("Drag the arrows to move the selected point along X, Y or Z, the ball to move it across the view, "
                          "and the rings to turn its heading around X, Y or Z. The wires update when you let go.")
    dlg.g_mode = QtWidgets.QComboBox()
    dlg.g_mode.addItems(["moves the point", "moves the whole wire"])
    dlg.g_mode.setToolTip("What the arrows and the ball move. For the whole wire the rings are hidden.")
    dlg.g_size = QtWidgets.QSlider(QtCore.Qt.Horizontal)
    dlg.g_size.setRange(_GIZMO_SIZES[0], _GIZMO_SIZES[1])
    dlg.g_size.setSingleStep(10)
    dlg.g_size.setPageStep(25)
    dlg.g_size.setValue(_gizmo_percent())
    dlg.g_size.setFixedWidth(170)
    dlg.g_size.setToolTip("How big the gizmo is in the 3D view (50 to 300 per cent). It is remembered for next time.\n"
                          "The part of the view around the arrows and rings that grabs the mouse grows with it.")
    dlg.g_size_text = QtWidgets.QLabel("%d %%" % dlg.g_size.value())
    dlg.g_size_text.setMinimumWidth(40)
    dlg.g_size_reset = QtWidgets.QPushButton("Normal size")
    dlg.g_step = QtWidgets.QComboBox()
    for v_ in _TURN_STEPS:
        dlg.g_step.addItem(("%s\u00b0 (normal)" if v_ == _TURN_STEPS[0] else "%s\u00b0") % _angle_str(v_, v_), v_)
    dlg.g_step.addItem("Manual", None)
    dlg.g_step_manual = spin(_TURN_STEP_MIN, _TURN_STEP_MAX, 0.2, 0.1, 3)
    dlg.g_step_manual.setFixedWidth(84)
    dlg.g_step_manual.setSuffix(" \u00b0")
    tip_ = ("How far the rings in the 3D view turn with every step. A small step helps to line a wire up with an axis: "
            "the ring says 'parallel to X' when the heading is within 0.05 degrees.\n"
            "The arrow keys turn by one step, Shift slows the mouse down to a tenth. It is remembered for next time.")
    dlg.g_step.setToolTip(tip_)
    dlg.g_step_manual.setToolTip("Your own step, from 0.001 to 90 degrees. Choose 'Manual' on the left to use it.\n" + tip_)
    cur_ = _turn_step()
    for i_ in range(dlg.g_step.count()):
        if dlg.g_step.itemData(i_) is not None and abs(dlg.g_step.itemData(i_) - cur_) < 1e-9:
            dlg.g_step.setCurrentIndex(i_)
            break
    else:
        dlg.g_step.setCurrentIndex(dlg.g_step.count() - 1)
        dlg.g_step_manual.setValue(cur_)
    dlg.g_step_manual.setEnabled(dlg.g_step.currentData() is None)
    dlg.p_show = QtWidgets.QCheckBox("Show path (centre line)")
    dlg.p_show.setChecked(_setting("ShowPath", False))
    dlg.p_show.setToolTip("Draws the line through the middle of the wire, the path the wire follows, over the 3D view.\n"
                          "It shows through the wire and the covering. Nothing is added to the document.")
    dlg.p_only = QtWidgets.QCheckBox("Only the selected wire")
    dlg.p_only.setChecked(_setting("ShowPathSelected", False))
    dlg.p_only.setToolTip("Draw the line of the wire chosen in this window only. Otherwise every wire gets one,\n"
                          "the chosen one in cyan and thicker.")
    dlg.c_check = QtWidgets.QPushButton("Check collisions")
    dlg.c_check.setToolTip("Looks for wires that touch or overlap each other. Every wire is treated as a tube of its outer diameter.")
    dlg.c_auto = QtWidgets.QCheckBox("After each change")
    dlg.c_auto.setChecked(_setting("CollisionAuto", True))
    dlg.c_auto.setToolTip("Check again whenever a wire changes: in this window, with the gizmo, or with undo / redo.")
    dlg.c_gap = spin(0, 20, _setting("CollisionGap", 0.0), 0.05, 2)
    dlg.c_gap.setToolTip("Wires that come closer to each other than this count as a collision too (orange).\n"
                         "0 = only wires that touch or overlap (red).")
    dlg.c_parts = QtWidgets.QCheckBox("Also other parts")
    dlg.c_parts.setChecked(_setting("CollisionParts", False))
    dlg.c_parts.setToolTip("Also look for wires that go through the other solids in the document, the connector for example.\n"
                           "A little slower. Only overlaps count here, not the gap.\n"
                           "Wires that go through a covering are always reported, with or without this.")
    dlg.c_list = QtWidgets.QComboBox()
    dlg.c_list.setMinimumWidth(250)
    dlg.c_list.setEnabled(False)
    dlg.c_zoom = QtWidgets.QPushButton("Zoom to")
    dlg.c_zoom.setToolTip("Moves the 3D view to the collision chosen in the list.")
    dlg.c_zoom.setEnabled(False)
    dlg.c_info = QtWidgets.QLabel("")
    dlg.c_info.setWordWrap(True)
    dlg.c_info.setMinimumWidth(430)
    dlg.marks = None
    dlg.pathline = None
    dlg.found = None
    dlg.gizmo = None
    dlg.v_list = QtWidgets.QComboBox()
    dlg.v_list.setMinimumWidth(250)
    dlg.v_list.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToContents)
    dlg.v_list.setToolTip("The coverings of this harness: a tube around a bundle of wires that protects them and keeps them together\n"
                          "(a Harness Covering in Altium Designer).")
    dlg.v_add = QtWidgets.QPushButton("Add...")
    dlg.v_add.setToolTip("Put a new covering around some wires.")
    dlg.v_edit = QtWidgets.QPushButton("Edit...")
    dlg.v_edit.setToolTip("Change the chosen covering: the wires, the stretch, the size, the wall and the color.")
    dlg.v_del = QtWidgets.QPushButton("Delete")
    dlg.v_del.setToolTip("Remove the chosen covering. The wires stay as they are.")
    dlg.v_nudge, dlg.v_step, dlg.v_nudges = _nudge_panel(QtWidgets)
    dlg.v_info = QtWidgets.QLabel("")
    dlg.v_info.setWordWrap(True)
    dlg.v_info.setMinimumWidth(430)
    dlg.t_list = QtWidgets.QComboBox()
    dlg.t_list.setMinimumWidth(250)
    dlg.t_list.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToContents)
    dlg.t_list.setToolTip("The twists of this harness: 2 or more wires that wind around each other along a stretch\n"
                          "(a twisted pair, for example).")
    dlg.t_add = QtWidgets.QPushButton("Add...")
    dlg.t_add.setToolTip("Twist 2 or more wires around each other. Their ends and lengths stay as they are; the window shows\n"
                         "the extra wire the twist needs.")
    dlg.t_edit = QtWidgets.QPushButton("Edit...")
    dlg.t_edit.setToolTip("Change the chosen twist: the wires, the stretch, the lay length and the hand.")
    dlg.t_del = QtWidgets.QPushButton("Delete")
    dlg.t_del.setToolTip("Remove the chosen twist. The wires go back to their own paths.")
    dlg.t_info = QtWidgets.QLabel("")
    dlg.t_info.setWordWrap(True)
    dlg.t_info.setMinimumWidth(430)
    dlg.b_close = QtWidgets.QPushButton("Close")

    def group(title, *rows):
        box = QtWidgets.QGroupBox(title)
        lay = QtWidgets.QVBoxLayout(box)
        for r_ in rows:
            lay.addLayout(r_)
        return box

    def page(*boxes):
        w_ = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w_)
        for b_ in boxes:
            lay.addWidget(b_)
        lay.addStretch(1)
        return w_

    # What you pick is always on top; what you do with it is on the tabs; the collision result and Close stay at the bottom.
    pick_point = QtWidgets.QHBoxLayout()
    pick_point.addWidget(dlg.f_point, 1)
    pick_point.addWidget(dlg.f_follow)
    form.addRow("Wire", dlg.f_wire)
    form.addRow("Point", pick_point)
    dlg.tabs = QtWidgets.QTabWidget()
    dlg.tabs.setUsesScrollButtons(False)
    dlg.tabs.tabBar().setExpanding(True)
    dlg.tabs.addTab(page(
        group("Move the point (mm)", row("X", dlg.m_x, "Y", dlg.m_y, "Z", dlg.m_z), row(dlg.b_move, dlg.b_movewire)),
        group("Turn the heading", row("Angle (deg)", dlg.r_angle, dlg.r_mode, dlg.b_turn), row(dlg.b_auto)),
        group("Points of the wire", row("Distance (mm)", dlg.p_extend, dlg.b_add, dlg.b_del))), "Point")
    dlg.tabs.addTab(page(
        group("Length, color and start", row("Length (mm)", dlg.len_spin, dlg.b_len, dlg.len_info),
              row("Color", dlg.swatch, dlg.color_combo, dlg.b_pick, dlg.b_color),
              row("Leaves its start toward", dlg.dir_combo, dlg.b_dir)),
        group("Covering", row(dlg.v_list, dlg.v_add, dlg.v_edit, dlg.v_del), row(dlg.v_nudge), row(dlg.v_info)),
        group("Twisted wires", row(dlg.t_list, dlg.t_add, dlg.t_edit, dlg.t_del), row(dlg.t_info)),
        group("Wires", row(dlg.b_newwire, dlg.b_delwire), row("Quick wire starts at  X", dlg.w_dx, "Y", dlg.w_dy, dlg.b_addwire))), "Wire")
    dlg.tabs.addTab(page(
        group("Arrows, ball and rings in the 3D view", row(dlg.g_show, dlg.g_mode),
              row("Size", dlg.g_size, dlg.g_size_text, dlg.g_size_reset),
              row("Ring step", dlg.g_step, dlg.g_step_manual)),
        group("Wire path", row(dlg.p_show, dlg.p_only))), "View")
    dlg.tabs.addTab(page(
        group("Look for wires that touch", row(dlg.c_check, dlg.c_auto), row("Min. gap (mm)", dlg.c_gap, dlg.c_parts),
              row(dlg.c_list, dlg.c_zoom))), "Collisions")
    try:
        dlg.tabs.setCurrentIndex(max(0, min(dlg.tabs.count() - 1, int(_setting("EditTab", 0)))))
    except Exception:
        pass
    dlg.tabs.currentChanged.connect(lambda i_: _save_setting("EditTab", i_))
    form.addRow(dlg.tabs)
    form.addRow(dlg.c_info)
    close_row = QtWidgets.QHBoxLayout()
    close_row.addStretch(1)
    close_row.addWidget(dlg.b_close)
    form.addRow(close_row)

    def current_step():
        v = dlg.g_step.currentData()
        return dlg.g_step_manual.value() if v is None else v

    def ensure_gizmo():
        if dlg.gizmo is None:
            views = FreeCADGui.getDocument(doc.Name).mdiViewsOfType("Gui::View3DInventor")
            if not views:
                return None
            dlg.gizmo = DragGizmo(views[0], gizmo_move, gizmo_turn, gizmo_preview, gizmo_move_preview)
            dlg.gizmo.set_size(dlg.g_size.value())
            dlg.gizmo.set_angle_step(current_step())
        return dlg.gizmo

    def hide_gizmo():
        if dlg.gizmo is not None:
            dlg.gizmo.hide()

    def place_gizmo(pos, row_):
        if not dlg.g_show.isChecked():
            hide_gizmo()
            return
        gz = ensure_gizmo()
        if gz is not None:
            gz.set_target(pos, rings=(row_ != "start" and dlg.g_mode.currentIndex() == 0),
                          whole=(dlg.g_mode.currentIndex() == 1))

    def ensure_marks():
        if dlg.marks is None:
            views = FreeCADGui.getDocument(doc.Name).mdiViewsOfType("Gui::View3DInventor")
            if not views:
                return None
            dlg.marks = CollisionMarks(views[0])
        return dlg.marks

    def refresh_path():
        """Draw (or take away) the centre lines. Cheap when nothing changed: the lines are remembered."""
        if not dlg.p_show.isChecked():
            if dlg.pathline is not None:
                dlg.pathline.clear()
            return
        try:
            if dlg.pathline is None:
                views = FreeCADGui.getDocument(doc.Name).mdiViewsOfType("Gui::View3DInventor")
                if not views:
                    return
                dlg.pathline = PathLine(views[0])
            chosen = dlg.f_wire.currentData()
            dlg.pathline.show(wire_path_lines(doc, harness, chosen if dlg.p_only.isChecked() else None), chosen)
        except Exception:
            if dlg.pathline is not None:
                dlg.pathline.clear()

    def show_collisions(res):
        import html
        dlg.found = res
        found = res["collisions"]
        dlg.c_list.blockSignals(True)
        dlg.c_list.clear()
        for k, c in enumerate(found):
            dlg.c_list.addItem("%s - %s" % (collision_title(c), collision_what(c)), k)
        dlg.c_list.blockSignals(False)
        dlg.c_list.setEnabled(bool(found))
        dlg.c_zoom.setEnabled(bool(found))
        lines = collision_lines(res)
        shown = [html.escape(l) for l in lines[:7]]
        if len(lines) > 7:
            shown.append("... and %d more" % (len(lines) - 7))
        color = "#ff5555" if found else "#4caf50"
        dlg.c_info.setText('<span style="color:%s"><b>%s</b></span>%s' % (color, shown[0], "".join("<br>" + l for l in shown[1:])))
        tabs_ = getattr(dlg, "tabs", None)
        if tabs_ is not None:
            tabs_.setTabText(tabs_.count() - 1, "Collisions (%d)" % len(found) if found else "Collisions")
        marks = ensure_marks() if found else dlg.marks
        if marks is not None:
            if found:
                marks.show(found)
            else:
                marks.clear()

    def run_collisions(manual=False):
        """Look for collisions and show them. Without `manual` this only happens when 'After each change' is on."""
        if not manual and not dlg.c_auto.isChecked():
            return
        try:
            count = dlg.f_wire.count()
            if not manual and count > 15:
                dlg.c_info.setText("%d wires: press Check collisions." % count)
                return
            parts = _other_parts(doc, harness) if dlg.c_parts.isChecked() else ()
            show_collisions(find_collisions(doc, harness, dlg.c_gap.value(), parts))
        except Exception as e:
            dlg.c_info.setText("The collision check did not work: %s" % e)

    def do_zoom():
        k = dlg.c_list.currentData()
        if dlg.found is None or k is None:
            return
        views = FreeCADGui.getDocument(doc.Name).mdiViewsOfType("Gui::View3DInventor")
        if views:
            focus_view(views[0], Vector(*dlg.found["collisions"][k]["point"]))

    def cells_of(wid):
        """The cells a drag can change (the wire's start and its points), so they can be put back."""
        w = _wire_dict(doc, harness, wid)
        ps = find_sheet(doc, w["sheet"])
        cells = {}
        for q in w["points"]:
            for col in "CDEFGH":
                cells[(ps, "%s%d" % (col, q["row"]))] = ps.getContents("%s%d" % (col, q["row"]))
        for col in "BCD":
            cells[(harness.Params, "%s%d" % (col, w["row"]))] = harness.Params.getContents("%s%d" % (col, w["row"]))
        return cells

    def put_back(cells):
        for (sheet_, addr), text in cells.items():
            if text == "":
                sheet_.clear(addr)
            else:
                sheet_.set(addr, text)
        doc.recompute()

    def apply_checked(label, wid, change):
        """Make a change to the points, and keep it only if the harness still builds. Otherwise put everything back."""
        was_invalid = "Invalid" in harness.State
        saved = cells_of(wid)
        note = ""
        doc.openTransaction(label)
        try:
            note = change() or ""
            problem = None
            if not was_invalid and "Invalid" in harness.State:
                problem = "FreeCAD cannot build wire %s after this change." % wid
                try:
                    w = _wire_dict(doc, harness, wid)
                    need = curve_length(w)
                    if need > w["total"] + 1e-6:
                        problem = ("Wire %s would need %.2f mm of wire, but its total length is %.2f mm. "
                                   "Raise its Length first, or drag less." % (wid, need, w["total"]))
                except Exception as e:
                    problem = str(e)
        except Exception:
            doc.abortTransaction()
            put_back(saved)
            raise
        if problem is not None:
            doc.abortTransaction()
            put_back(saved)
            raise ValueError(problem + " Nothing was changed.")
        doc.commitTransaction()
        after()
        if note:
            _status_message(note)

    def gizmo_move(delta):
        wid, row_ = dlg.f_wire.currentData(), dlg.f_point.currentData()
        if wid is None or row_ is None:
            return
        if dlg.g_mode.currentIndex() == 1:
            change = lambda: move_point(doc, harness, wid, "start", tuple(delta), True)
        else:
            change = lambda: move_point(doc, harness, wid, row_, tuple(delta), dlg.f_follow.isChecked())
        apply_checked("Drag wire point", wid, change)

    def gizmo_move_preview():
        wid, row_ = dlg.f_wire.currentData(), dlg.f_point.currentData()
        if wid is None or row_ is None:
            return None
        return move_previewer(doc, harness, wid, row_, dlg.f_follow.isChecked(), dlg.g_mode.currentIndex() == 1)

    def gizmo_preview():
        wid, row_ = dlg.f_wire.currentData(), dlg.f_point.currentData()
        if wid is None or row_ is None or row_ == "start":
            return None
        return turn_previewer(doc, harness, wid, row_, dlg.f_follow.isChecked())

    def gizmo_turn(axis, angle):
        wid, row_ = dlg.f_wire.currentData(), dlg.f_point.currentData()
        if wid is None or row_ is None or row_ == "start":
            return
        apply_checked("Turn wire point", wid,
                      lambda: rotate_point_about(doc, harness, wid, row_, tuple(axis), angle, dlg.f_follow.isChecked()))

    def fill_points(keep=None):
        wid = dlg.f_wire.currentData()
        dlg.f_point.blockSignals(True)
        dlg.f_point.clear()
        if wid is not None:
            try:
                w = _wire_dict(doc, harness, wid)
                dlg.f_point.addItem("Start (where the wire begins)", "start")
                for q in w["points"]:
                    dlg.f_point.addItem("Row %d (%s)" % (q["row"], q["kind"]), q["row"])
                i = dlg.f_point.findData(keep) if keep is not None else -1
                dlg.f_point.setCurrentIndex(i if i >= 0 else (1 if dlg.f_point.count() > 1 else 0))
            except Exception as e:
                dlg.f_point.addItem("(%s)" % e, None)
        dlg.f_point.blockSignals(False)
        update_info()

    def fill_coverings(keep=None):
        """List the coverings of the document (also after undo / redo and after a wire was deleted)."""
        keep = keep if keep is not None else dlg.v_list.currentData()
        obj = find_coverings(doc)
        covs = read_coverings(obj.Params)[0] if obj is not None else []
        dlg.v_list.blockSignals(True)
        dlg.v_list.clear()
        for c in covs:
            error = _covering_error(obj, c["id"])
            dlg.v_list.addItem(covering_title(c) + ("  - cannot be built" if error else ""), c["id"])
            if error:
                dlg.v_list.setItemData(dlg.v_list.count() - 1, error, QtCore.Qt.ToolTipRole)
        if not covs:
            dlg.v_list.addItem("(none yet)", None)
        i = dlg.v_list.findData(keep) if keep is not None else -1
        dlg.v_list.setCurrentIndex(i if i >= 0 else 0)
        dlg.v_list.blockSignals(False)
        dlg.v_edit.setEnabled(bool(covs))
        dlg.v_del.setEnabled(bool(covs))
        dlg.v_nudge.setEnabled(bool(covs))
        dlg.v_info.setText("")

    def show_twist_info(*_a):
        tid = dlg.t_list.currentData()
        dlg.t_info.setText("")
        if tid is None:
            return
        import html
        error = _twist_error(harness, tid)
        if error is not None:
            dlg.t_info.setText('<span style="color:#ff5555"><b>%s</b></span>' % html.escape("Twist %s cannot be made: %s" % (tid, error)))
            return
        prefix = "Twist %s: " % tid
        for line in harness.WireReport:
            if line.startswith(prefix):
                dlg.t_info.setText(line[len(prefix):])

    def fill_twists(keep=None):
        """List the twists of the document (also after undo / redo and after a wire was deleted)."""
        keep = keep if keep is not None else dlg.t_list.currentData()
        twists = harness_twists(harness)[0]
        dlg.t_list.blockSignals(True)
        dlg.t_list.clear()
        for t in twists:
            error = _twist_error(harness, t["id"])
            dlg.t_list.addItem(twist_title(t) + ("  - cannot be made" if error else ""), t["id"])
            if error:
                dlg.t_list.setItemData(dlg.t_list.count() - 1, error, QtCore.Qt.ToolTipRole)
        if not twists:
            dlg.t_list.addItem("(none yet)", None)
        i = dlg.t_list.findData(keep) if keep is not None else -1
        dlg.t_list.setCurrentIndex(i if i >= 0 else 0)
        dlg.t_list.blockSignals(False)
        dlg.t_edit.setEnabled(bool(twists))
        dlg.t_del.setEnabled(bool(twists))
        show_twist_info()

    def fill_wires(keep_point=None):
        keep = dlg.f_wire.currentData()
        dlg.f_wire.blockSignals(True)
        dlg.f_wire.clear()
        for w in read_harness(harness.Params, [s.Name for s in harness.PointSheets])[0]:
            dlg.f_wire.addItem("Wire %s" % w["id"], w["id"])
        if dlg.f_wire.count() == 0:
            dlg.f_wire.addItem("(no wires yet - click New wire...)", None)
        i = dlg.f_wire.findData(keep) if keep is not None else -1
        if i >= 0:
            dlg.f_wire.setCurrentIndex(i)
        dlg.f_wire.blockSignals(False)
        fill_points(keep_point)
        fill_coverings()
        fill_twists()
        run_collisions()

    def update_length():
        wid = dlg.f_wire.currentData()
        if wid is None:
            dlg.len_info.setText("")
            return
        try:
            w = _wire_dict(doc, harness, wid)
            dlg.len_spin.setValue(w["total"])
            if any(q["kind"] == "end" for q in w["points"]):
                dlg.len_info.setText("fixed end")
            else:
                dlg.len_info.setText("min %.1f" % curve_length(w))
        except Exception as e:
            dlg.len_info.setText("")
            dlg.len_info.setToolTip(str(e))

    def update_color():
        wid = dlg.f_wire.currentData()
        if wid is None:
            return
        try:
            ws = read_harness(harness.Params, [s.Name for s in harness.PointSheets])[0]
            index = [x["id"] for x in ws].index(wid)
            text = ws[index]["color"].lstrip("'").strip()
            r, g, b = parse_color(text, index)[:3]
            dlg.swatch.setStyleSheet("background-color: rgb(%d, %d, %d); border: 1px solid gray;" % (round(r * 255), round(g * 255), round(b * 255)))
            dlg.color_combo.setCurrentText(text)
        except Exception:
            pass

    def update_direction():
        wid = dlg.f_wire.currentData()
        dlg.dir_combo.blockSignals(True)
        while dlg.dir_combo.count() > len(DIRECTIONS):          # a 'Custom' entry left over from another wire
            dlg.dir_combo.removeItem(dlg.dir_combo.count() - 1)
        if wid is not None:
            try:
                d = _start_dir(_wire_dict(doc, harness, wid))
                for k, (_l, v_) in enumerate(DIRECTIONS):
                    if (d - Vector(*v_)).Length < 1e-6:
                        dlg.dir_combo.setCurrentIndex(k)
                        break
                else:
                    dlg.dir_combo.addItem("Custom: " + format_direction(d), None)
                    dlg.dir_combo.setCurrentIndex(dlg.dir_combo.count() - 1)
            except Exception:
                pass
        dlg.dir_combo.blockSignals(False)

    def update_info():
        update_length()
        update_color()
        update_direction()
        refresh_path()
        wid, row_ = dlg.f_wire.currentData(), dlg.f_point.currentData()
        if wid is None or row_ is None:
            remove_point_marker(doc)
            hide_gizmo()
            return
        try:
            w = _wire_dict(doc, harness, wid)
            if row_ == "start":
                pos, head = (w["x"], w["y"], w["z"]), _start_dir(w)
                tip = ("Start at (%.2f, %.2f, %.2f) mm. The wire leaves its start toward %s (change it on the Wire tab)."
                       % (pos + (format_direction(head),)))
            else:
                idx = [q["row"] for q in w["points"]].index(int(row_))
                q = w["points"][idx]
                head = point_headings(w)[idx]
                tilt, direction = heading_angles(head)
                pos = q["p"]
                tip = ("Position (%.2f, %.2f, %.2f) mm. Heading: tilt %.1f deg from vertical, direction %s (%s)."
                       % (pos[0], pos[1], pos[2], tilt, "-" if direction is None else "%.1f deg around Z, 0 = +X" % direction,
                          "fixed - Auto heading lets FreeCAD choose it again" if q["dir"] is not None else "chosen by FreeCAD"))
            dlg.f_point.setToolTip(tip)
            show_point_marker(doc, pos, (head.x, head.y, head.z))
            place_gizmo(pos, row_)
        except Exception as e:
            dlg.f_point.setToolTip(str(e))
            hide_gizmo()

    def after():
        fill_points(dlg.f_point.currentData())
        fill_coverings()
        fill_twists()
        run_collisions()

    def do_move():
        wid, row_ = dlg.f_wire.currentData(), dlg.f_point.currentData()
        note = move_point(doc, harness, wid, row_, (dlg.m_x.value(), dlg.m_y.value(), dlg.m_z.value()), dlg.f_follow.isChecked())
        after()
        if note:
            _status_message(note)

    def do_rotate():
        wid, row_ = dlg.f_wire.currentData(), dlg.f_point.currentData()
        if row_ == "start":
            raise ValueError("The start direction is set on the Wire tab (Leaves its start toward). Choose a point to turn.")
        if abs(dlg.r_angle.value()) < 1e-9:
            raise ValueError("Enter an angle other than 0 to turn by.")
        note = rotate_point(doc, harness, wid, row_, dlg.r_angle.value(), dlg.r_mode.currentText(), dlg.f_follow.isChecked())
        after()
        if note:
            _status_message(note)

    def do_auto():
        wid, row_ = dlg.f_wire.currentData(), dlg.f_point.currentData()
        if row_ == "start":
            raise ValueError("Choose a point.")
        note = clear_heading(doc, harness, wid, row_)
        after()
        if note:
            _status_message(note)

    def do_length():
        wid = dlg.f_wire.currentData()
        if wid is None:
            raise ValueError("Choose a wire.")
        set_wire_length(doc, harness, wid, dlg.len_spin.value())
        after()

    def do_direction():
        wid = dlg.f_wire.currentData()
        if wid is None:
            raise ValueError("Choose a wire.")
        d = dlg.dir_combo.currentData()
        if d is None:
            raise ValueError("Choose one of the six directions, or type the direction in the wires table (column J).")
        set_wire_direction(doc, harness, wid, d)
        after()

    def do_color():
        wid = dlg.f_wire.currentData()
        if wid is None:
            raise ValueError("Choose a wire.")
        set_wire_color(doc, harness, wid, dlg.color_combo.currentText())
        after()

    def do_pick():
        from PySide import QtGui
        initial = QtGui.QColor(dlg.swatch.palette().window().color())
        chosen = QtWidgets.QColorDialog.getColor(initial, dlg, "Wire color")
        if chosen.isValid():
            dlg.color_combo.setCurrentText(chosen.name())
            do_color()

    def do_add():
        wid, row_ = dlg.f_wire.currentData(), dlg.f_point.currentData()
        if wid is None or row_ is None:
            raise ValueError("Choose a wire and a point.")
        new_row = add_point(doc, harness, wid, row_, dlg.p_extend.value())
        fill_points(new_row)

    def do_delete():
        wid, row_ = dlg.f_wire.currentData(), dlg.f_point.currentData()
        if wid is None or row_ is None:
            raise ValueError("Choose a wire and a point.")
        fill_points(delete_point(doc, harness, wid, row_))

    def do_add_wire():
        wid = dlg.f_wire.currentData()
        if wid is None:
            raise ValueError("Choose the wire to copy.")
        w = _wire_dict(doc, harness, wid)
        new_id = new_straight_wire(doc, harness, wid, (w["x"] + dlg.w_dx.value(), w["y"] + dlg.w_dy.value(), w["z"]))
        fill_wires()
        dlg.f_wire.setCurrentIndex(dlg.f_wire.findData(new_id))

    def do_new_wire():
        d = build_add_wire_dialog(doc, harness, dlg)
        if d.exec():
            fill_wires()
            i = dlg.f_wire.findData(d.result_id)
            if i >= 0:
                dlg.f_wire.setCurrentIndex(i)

    def do_delete_wire():
        wid = dlg.f_wire.currentData()
        if wid is None:
            raise ValueError("Choose a wire.")
        if dlg.confirm and QtWidgets.QMessageBox.question(dlg, "Delete wire", "Delete wire %s and its points sheet?" % wid) != QtWidgets.QMessageBox.Yes:
            return
        remove_wire(doc, harness, wid)
        fill_wires()

    def do_move_wire():
        wid = dlg.f_wire.currentData()
        if wid is None:
            raise ValueError("Choose a wire.")
        move_point(doc, harness, wid, "start", (dlg.m_x.value(), dlg.m_y.value(), dlg.m_z.value()), True)
        after()

    def chosen_covering():
        cid = dlg.v_list.currentData()
        obj = find_coverings(doc)
        found = [c for c in read_coverings(obj.Params)[0] if c["id"] == cid] if (cid is not None and obj is not None) else []
        if not found:
            raise ValueError("There is no covering to choose. Use Add... first.")
        return found[0]

    def covering_clashes(cid):
        """Check the collisions now and return the lines about wires that go through covering `cid`."""
        run_collisions(True)
        res = dlg.found or {"collisions": []}
        return [collision_detail(c) for c in res["collisions"] if c.get("covering") and c["b"] == "Covering %s" % cid]

    def covering_done(cid):
        fill_coverings(cid)
        lines = covering_clashes(cid)
        if lines:
            QtWidgets.QMessageBox.warning(dlg, "Covering %s" % cid, "The covering was made, but wires go through it:\n\n%s\n\n"
                                          "Leave those wires out of the way, or change the covering (wires, stretch, size)." % "\n".join(lines))

    def do_covering_add():
        d = build_covering_dialog(doc, harness, None, dlg)
        if d.exec():
            covering_done(d.result_id)

    def do_covering_edit():
        d = build_covering_dialog(doc, harness, chosen_covering(), dlg)
        if d.exec():
            covering_done(d.result_id)

    def do_covering_delete():
        c = chosen_covering()
        if dlg.confirm and QtWidgets.QMessageBox.question(dlg, "Delete covering", "Delete covering %s? The wires stay." % c["id"]) != QtWidgets.QMessageBox.Yes:
            return
        doc.openTransaction("Delete covering")
        try:
            remove_covering(doc, c["id"])
        except Exception:
            doc.abortTransaction()
            doc.recompute()
            raise
        doc.commitTransaction()
        fill_coverings()
        run_collisions()

    def do_covering_nudge(end, more):
        """Make the chosen covering cover more or expose more of the wire at one end; it is done right away (Ctrl+Z undoes it)."""
        c = chosen_covering()
        s0, s1, wire_len = covering_range(harness, c)
        step = _step_value(dlg.v_step, _setting("CoverStep", _COVER_NUDGES[0]))
        a, b, until_end, note = cover_nudge(s0, s1, wire_len, end, more, step, not c["to"])
        if note:
            dlg.v_info.setText("Covering %s: %s" % (c["id"], note))
            return
        doc.openTransaction("Cover more" if more else "Expose more")
        try:
            update_covering(doc, c["id"], c["wires"], a, None if until_end else b, c["inner"], c["wall"], c["color"])
        except Exception:
            doc.abortTransaction()
            doc.recompute()
            raise
        doc.commitTransaction()
        fill_coverings(c["id"])
        run_collisions()
        dlg.v_info.setText("Covering %s now goes from %.1f to %.1f mm along wire %s%s." % (
            c["id"], a, b, c["wires"][0], " (until the end of the wire)" if until_end else ""))

    def chosen_twist():
        tid = dlg.t_list.currentData()
        found = [t for t in harness_twists(harness)[0] if t["id"] == tid] if tid is not None else []
        if not found:
            raise ValueError("There is no twist to choose. Use Add... first.")
        return found[0]

    def twist_done(tid):
        fill_twists(tid)
        after()

    def do_twist_add():
        d = build_twist_dialog(doc, harness, None, dlg)
        if d.exec():
            twist_done(d.result_id)

    def do_twist_edit():
        d = build_twist_dialog(doc, harness, chosen_twist(), dlg)
        if d.exec():
            twist_done(d.result_id)

    def do_twist_delete():
        t = chosen_twist()
        if dlg.confirm and QtWidgets.QMessageBox.question(dlg, "Delete twist", "Delete twist %s? The wires stay." % t["id"]) != QtWidgets.QMessageBox.Yes:
            return
        doc.openTransaction("Delete twist")
        try:
            remove_twist(doc, harness, t["id"])
        except Exception:
            doc.abortTransaction()
            doc.recompute()
            raise
        doc.commitTransaction()
        fill_twists()
        after()

    def guarded(fn):
        def run():
            try:
                fn()
            except Exception as e:
                QtWidgets.QMessageBox.warning(dlg, "Harness Designer", str(e))
        return run

    dlg.do_move, dlg.do_rotate, dlg.do_auto, dlg.do_length, dlg.do_color = do_move, do_rotate, do_auto, do_length, do_color
    dlg.do_add, dlg.do_delete = do_add, do_delete
    dlg.do_add_wire, dlg.do_delete_wire = do_add_wire, do_delete_wire
    dlg.do_move_wire = do_move_wire
    dlg.b_movewire.clicked.connect(guarded(do_move_wire))
    dlg.b_addwire.clicked.connect(guarded(do_add_wire))
    dlg.b_newwire.clicked.connect(guarded(do_new_wire))
    dlg.ask_new_wire = guarded(do_new_wire)
    dlg.b_delwire.clicked.connect(guarded(do_delete_wire))
    dlg.b_add.clicked.connect(guarded(do_add))
    dlg.b_del.clicked.connect(guarded(do_delete))
    for (end_, more_), b_ in dlg.v_nudges.items():
        b_.clicked.connect(guarded(lambda end=end_, more=more_: do_covering_nudge(end, more)))
    dlg.do_covering_nudge = do_covering_nudge
    dlg.b_move.clicked.connect(guarded(do_move))
    dlg.b_turn.clicked.connect(guarded(do_rotate))
    dlg.b_auto.clicked.connect(guarded(do_auto))
    dlg.b_len.clicked.connect(guarded(do_length))
    dlg.b_color.clicked.connect(guarded(do_color))
    dlg.b_dir.clicked.connect(guarded(do_direction))
    dlg.do_direction = do_direction
    dlg.b_pick.clicked.connect(guarded(do_pick))
    dlg.b_close.clicked.connect(dlg.close)
    dlg.f_wire.currentIndexChanged.connect(lambda _i: fill_points())
    dlg.f_point.currentIndexChanged.connect(lambda _i: update_info())
    def refresh_after_undo():
        try:
            if dlg.isVisible():
                try:
                    # Ctrl+Z / Ctrl+Y put the cells back but leave the sheets and the wires waiting for a recompute,
                    # so the 3D shape and the numbers in this window would still show the old values.
                    if any("Touched" in o.State for o in doc.Objects):
                        doc.recompute()
                except ReferenceError:
                    raise RuntimeError("the document is gone")
                fill_wires(dlg.f_point.currentData())
        except RuntimeError:
            pass                                       # the window is gone

    class _UndoWatcher:
        """After Ctrl+Z / Ctrl+Y the window, the yellow marker and the gizmo must follow the data."""
        def _go(self, d):
            if d.Name == doc.Name:
                from PySide import QtCore
                QtCore.QTimer.singleShot(0, refresh_after_undo)

        def slotUndoDocument(self, d):
            self._go(d)

        def slotRedoDocument(self, d):
            self._go(d)

    watcher = _UndoWatcher()
    FreeCAD.addDocumentObserver(watcher)

    def cleanup(_r=None):
        try:
            FreeCAD.removeDocumentObserver(watcher)
        except Exception:
            pass
        try:
            remove_point_marker(doc)
        except ReferenceError:                  # the document is already closed
            pass
        if dlg.marks is not None:
            try:
                dlg.marks.destroy()
            except ReferenceError:
                pass
            dlg.marks = None
        if dlg.gizmo is not None:
            try:
                dlg.gizmo.destroy()
            except ReferenceError:
                pass
            dlg.gizmo = None
        if dlg.pathline is not None:
            try:
                dlg.pathline.destroy()
            except ReferenceError:
                pass
            dlg.pathline = None
    dlg.cleanup = cleanup
    dlg.gizmo_move, dlg.gizmo_turn = gizmo_move, gizmo_turn
    dlg.finished.connect(cleanup)
    dlg.g_show.toggled.connect(lambda _c: update_info())

    def on_path_toggled(_c=False):
        _save_setting("ShowPath", dlg.p_show.isChecked())
        _save_setting("ShowPathSelected", dlg.p_only.isChecked())
        dlg.p_only.setEnabled(dlg.p_show.isChecked())
        refresh_path()
    dlg.p_show.toggled.connect(on_path_toggled)
    dlg.p_only.toggled.connect(on_path_toggled)
    dlg.p_only.setEnabled(dlg.p_show.isChecked())
    dlg.refresh_path = refresh_path
    dlg.g_mode.currentIndexChanged.connect(lambda _i: update_info())

    def on_size(value):
        dlg.g_size_text.setText("%d %%" % value)
        _save_gizmo_percent(value)
        if dlg.gizmo is not None:
            dlg.gizmo.set_size(value)
    dlg.g_size.valueChanged.connect(on_size)
    dlg.g_size_reset.clicked.connect(lambda _c=False: dlg.g_size.setValue(_GIZMO_SIZES[2]))

    def on_step(*_a):
        dlg.g_step_manual.setEnabled(dlg.g_step.currentData() is None)
        step = current_step()
        _save_setting("TurnStep", step)
        if dlg.gizmo is not None:
            dlg.gizmo.set_angle_step(step)
    dlg.g_step.currentIndexChanged.connect(on_step)
    dlg.g_step_manual.valueChanged.connect(on_step)
    dlg.run_collisions = run_collisions
    dlg.c_check.clicked.connect(guarded(lambda: run_collisions(True)))
    dlg.c_zoom.clicked.connect(guarded(do_zoom))
    dlg.c_gap.valueChanged.connect(lambda v: (_save_setting("CollisionGap", v), run_collisions(True)))
    dlg.c_parts.toggled.connect(lambda v: (_save_setting("CollisionParts", bool(v)), run_collisions(True)))
    dlg.c_auto.toggled.connect(lambda v: (_save_setting("CollisionAuto", bool(v)), run_collisions(True) if v else None))
    dlg.t_add.clicked.connect(guarded(do_twist_add))
    dlg.t_edit.clicked.connect(guarded(do_twist_edit))
    dlg.t_del.clicked.connect(guarded(do_twist_delete))
    dlg.t_list.currentIndexChanged.connect(show_twist_info)
    dlg.do_twist_add, dlg.do_twist_edit, dlg.do_twist_delete = do_twist_add, do_twist_edit, do_twist_delete
    dlg.v_add.clicked.connect(guarded(do_covering_add))
    dlg.v_edit.clicked.connect(guarded(do_covering_edit))
    dlg.v_del.clicked.connect(guarded(do_covering_delete))
    dlg.do_covering_add, dlg.do_covering_edit, dlg.do_covering_delete = do_covering_add, do_covering_edit, do_covering_delete
    dlg.covering_clashes = covering_clashes
    dlg.refresh = fill_wires
    fill_wires()
    return dlg


def edit_point_dialog(doc=None):
    """Open the 'Harness Designer' window (it stays open while you work in FreeCAD).

    It works in any document: one with no harness gets an empty harness first, with no document open a new one is made,
    and a harness with no wires opens the 'New wire' form so you can draw the first wire."""
    doc = doc or FreeCAD.ActiveDocument or FreeCAD.newDocument("WireHarness")
    for old in list(_open_dialogs):
        try:
            old.close()
            if hasattr(old, "cleanup"):
                old.cleanup()
        except RuntimeError:
            pass
        _open_dialogs.remove(old)
    harness = _open_harness(doc)
    dlg = build_point_dialog(doc, harness)
    _open_dialogs.append(dlg)
    dlg.show()
    if not read_harness(harness.Params)[0]:
        try:
            from PySide import QtCore
            QtCore.QTimer.singleShot(250, dlg.ask_new_wire)
        except Exception:
            pass
    return dlg


harness_designer = edit_point_dialog            # the name the Harness_Designer macro calls
