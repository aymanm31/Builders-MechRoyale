"""Rooftop sites: stand on a building, shoot down, drop off the far side when hurt."""
import json
import math
import os

from world import _pip


def _seg_closest(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    t = 0.0 if l2 < 1e-9 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2))
    return ax + dx * t, ay + dy * t


class Site:
    __slots__ = ("ring", "top", "cx", "cy", "area", "rise")

    def inside(self, x, y, margin=0.0):
        if not _pip(self.ring, x, y):
            return False
        if margin <= 0:
            return True
        r = self.ring
        n = len(r) // 2
        for i in range(n):
            j = (i + 1) % n
            qx, qy = _seg_closest(x, y, r[2 * i], r[2 * i + 1], r[2 * j], r[2 * j + 1])
            if math.hypot(qx - x, qy - y) < margin:
                return False
        return True

    def boundary_points(self, spacing=6.0):
        r = self.ring
        n = len(r) // 2
        out = []
        for i in range(n):
            j = (i + 1) % n
            ax, ay, bx, by = r[2 * i], r[2 * i + 1], r[2 * j], r[2 * j + 1]
            L = math.hypot(bx - ax, by - ay)
            k = max(1, int(L / spacing))
            for s in range(k + 1):
                f = s / k
                out.append((ax + (bx - ax) * f, ay + (by - ay) * f))
        return out


class RoofPlanner:
    def __init__(self, world, wjson_dirs):
        self.world = world
        self.sites = []
        path = None
        for d in wjson_dirs:
            p = os.path.join(d, "v2", "world.json")
            if os.path.exists(p):
                path = p
                break
        if path is None:
            return
        for p in json.load(open(path))["prisms"]:
            if p["kind"] != "building" or p.get("holes"):
                continue
            ring = p["ring"]
            n = len(ring) // 2
            if n < 3:
                continue
            a = cx = cy = 0.0
            for i in range(n):
                j = (i + 1) % n
                cr = ring[2 * i] * ring[2 * j + 1] - ring[2 * j] * ring[2 * i + 1]
                a += cr
                cx += (ring[2 * i] + ring[2 * j]) * cr
                cy += (ring[2 * i + 1] + ring[2 * j + 1]) * cr
            if abs(a) < 1e-6:
                continue
            area = abs(a) / 2
            cx, cy = cx / (3 * a), cy / (3 * a)
            s = Site()
            s.ring, s.top, s.area = ring, p["top"], area
            if area < 120 or not _pip(ring, cx, cy):
                continue
            g = world.ground(cx, cy)
            s.rise = s.top - g
            if not (6.0 <= s.rise <= 45.0):
                continue
            i = int((cx - world.minx) / 2.0)
            j = int((cy - world.miny) / 2.0)
            if 0 <= i < world.W and 0 <= j < world.H and abs(world.top[j * world.W + i] - s.top) > 0.6:
                continue                      # something taller sits here
            s.cx, s.cy = cx, cy
            if not s.inside(cx, cy, 3.0):
                continue
            self.sites.append(s)

    def entry(self, site, pos):
        """(wall point B, standing point E outside it) nearest to pos that is walkable ground."""
        best = None
        for bx, by in site.boundary_points():
            ux, uy = bx - site.cx, by - site.cy
            L = math.hypot(ux, uy) or 1.0
            ex, ey = bx + ux / L * 3.5, by + uy / L * 3.5
            if self.world.walk_blocked(ex, ey):
                continue
            if abs(self.world.ground(ex, ey) - pos[2]) > 5.0:
                continue
            d = math.hypot(ex - pos[0], ey - pos[1])
            if best is None or d < best[0]:
                best = (d, (bx, by), (ex, ey))
        return best

    def choose(self, pos, threat, max_walk=45.0, energy=100.0, safe=None):
        """Best site to climb: close to us, a decent distance from the threat."""
        best = None
        for s in self.sites:
            dd = math.hypot(s.cx - pos[0], s.cy - pos[1])
            if dd > max_walk + 30:
                continue
            if energy < 100.0 * (s.top - pos[2]) / 60.0 + 25.0:
                continue
            td = math.hypot(s.cx - threat[0], s.cy - threat[1])
            if td < 40 or td > 230:
                continue
            if safe is not None and not safe((s.cx, s.cy)):
                continue
            sc = dd - 0.1 * min(td, 150)
            if best is None or sc < best[0]:
                best = (sc, s)
        if best is None:
            return None
        e = self.entry(best[1], pos)
        if e is None or e[0] > max_walk:
            return None
        return best[1], e[1], e[2]
