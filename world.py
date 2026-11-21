"""Static map model: building/bridge raster, ground height, walkability, LOS, A* on a coarse grid."""
import array
import heapq
import json
import math
import os
import time

CELL = 2.0      # raster resolution (m)
PC = 4.0        # path grid resolution (m)
NEG = -1e9
POS = 1e9


def _pip(ring, x, y):
    c = False
    n = len(ring) // 2
    j = n - 1
    for i in range(n):
        xi, yi = ring[2 * i], ring[2 * i + 1]
        xj, yj = ring[2 * j], ring[2 * j + 1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            c = not c
        j = i
    return c


class World:
    def __init__(self, dirs):
        wpath = tpath = None
        for d in dirs:
            w = os.path.join(d, "v2", "world.json")
            t = os.path.join(d, "v2", "terrain.i16")
            if os.path.exists(w) and os.path.exists(t):
                wpath, tpath = w, t
                break
        if wpath is None:
            raise FileNotFoundError("map not found in %r" % (dirs,))
        world = json.load(open(wpath))
        ter = world["terrain"]
        self.minx, self.miny = ter["minX"], ter["minY"]
        self.tnx, self.tny, self.tstep = ter["nx"], ter["ny"], ter["step"]
        self.ground_arr = array.array("h")
        with open(tpath, "rb") as f:
            self.ground_arr.frombytes(f.read())
        self.maxx = self.minx + (self.tnx - 1) * self.tstep
        self.maxy = self.miny + (self.tny - 1) * self.tstep
        self.W = int((self.maxx - self.minx) / CELL) + 1
        self.H = int((self.maxy - self.miny) / CELL) + 1
        n = self.W * self.H
        self.top = array.array("f", [NEG]) * n
        self.base = array.array("f", [POS]) * n
        self.btop = array.array("f", [NEG]) * n
        self.bbase = array.array("f", [POS]) * n
        for p in world["prisms"]:
            ring = p["ring"]
            xs, ys = ring[0::2], ring[1::2]
            i0 = max(0, int((min(xs) - self.minx) / CELL) - 1)
            i1 = min(self.W - 1, int((max(xs) - self.minx) / CELL) + 1)
            j0 = max(0, int((min(ys) - self.miny) / CELL) - 1)
            j1 = min(self.H - 1, int((max(ys) - self.miny) / CELL) + 1)
            bridge = p["kind"] == "bridge"
            holes = p.get("holes") or []
            tp, bs = p["top"], p["base"]
            for j in range(j0, j1 + 1):
                y = self.miny + (j + 0.5) * CELL
                for i in range(i0, i1 + 1):
                    x = self.minx + (i + 0.5) * CELL
                    if not _pip(ring, x, y):
                        continue
                    if holes and any(_pip(h, x, y) for h in holes):
                        continue
                    c = j * self.W + i
                    if bridge:
                        if tp > self.btop[c]:
                            self.btop[c] = tp
                        if bs < self.bbase[c]:
                            self.bbase[c] = bs
                    else:
                        if tp > self.top[c]:
                            self.top[c] = tp
                        if bs < self.base[c]:
                            self.base[c] = bs
        # walk-blocked raster
        self.wb = bytearray(n)
        for j in range(self.H):
            for i in range(self.W):
                c = j * self.W + i
                if self.top[c] > NEG / 2 or self.btop[c] > NEG / 2:
                    x = self.minx + (i + 0.5) * CELL
                    y = self.miny + (j + 0.5) * CELL
                    g = self.ground(x, y)
                    if (self.top[c] - g > 1.2 and self.base[c] < g + 9.0) or (
                        self.btop[c] - g > 1.2 and self.bbase[c] < g + 9.6
                    ):
                        self.wb[c] = 1
        # coarse path grid
        self.pnx = int(math.ceil((self.maxx - self.minx) / PC))
        self.pny = int(math.ceil((self.maxy - self.miny) / PC))
        pb = bytearray(self.pnx * self.pny)
        k = int(PC / CELL)
        for pj in range(self.pny):
            for pi in range(self.pnx):
                cnt = 0
                for dj in range(k):
                    for di in range(k):
                        i, j = pi * k + di, pj * k + dj
                        if i >= self.W or j >= self.H or self.wb[j * self.W + i]:
                            cnt += 1
                if cnt >= 2:
                    pb[pj * self.pnx + pi] = 1
        self.pb = pb
        self.pnear = bytearray(self.pnx * self.pny)
        for pj in range(1, self.pny - 1):
            for pi in range(1, self.pnx - 1):
                c = pj * self.pnx + pi
                if pb[c]:
                    continue
                if pb[c - 1] or pb[c + 1] or pb[c - self.pnx] or pb[c + self.pnx]:
                    self.pnear[c] = 1

    # ---- terrain / solids ----
    def ground(self, x, y):
        i = int((x - self.minx) / self.tstep + 0.5)
        j = int((y - self.miny) / self.tstep + 0.5)
        if i < 0:
            i = 0
        elif i >= self.tnx:
            i = self.tnx - 1
        if j < 0:
            j = 0
        elif j >= self.tny:
            j = self.tny - 1
        return self.ground_arr[j * self.tnx + i] * 0.01

    def inside_map(self, x, y):
        return self.minx + 2 < x < self.maxx - 2 and self.miny + 2 < y < self.maxy - 2

    def walk_blocked(self, x, y):
        if not self.inside_map(x, y):
            return True
        return self.wb[int((y - self.miny) / CELL) * self.W + int((x - self.minx) / CELL)] == 1

    def solid(self, x, y, z):
        i = int((x - self.minx) / CELL)
        j = int((y - self.miny) / CELL)
        if i < 0 or j < 0 or i >= self.W or j >= self.H:
            return False
        c = j * self.W + i
        if self.base[c] < z < self.top[c]:
            return True
        if self.bbase[c] < z < self.btop[c]:
            return True
        return False

    def los(self, a, b, step=2.5):
        """True if the straight segment a->b is free of buildings/bridges/terrain."""
        dx, dy, dz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        d = math.sqrt(dx * dx + dy * dy + dz * dz)
        n = int(d / step)
        if n < 1:
            return True
        sx, sy, sz = dx / n, dy / n, dz / n
        x, y, z = a
        for _ in range(n - 1):
            x += sx
            y += sy
            z += sz
            if self.solid(x, y, z):
                return False
            if z < self.ground(x, y) - 0.3:
                return False
        return True

    def line_walkable(self, a, b, step=2.0):
        dx, dy = b[0] - a[0], b[1] - a[1]
        d = math.hypot(dx, dy)
        n = int(d / step) + 1
        for k in range(1, n + 1):
            f = k / n
            if self.walk_blocked(a[0] + dx * f, a[1] + dy * f):
                return False
        return True

    # ---- path grid ----
    def pcell(self, x, y):
        pi = int((x - self.minx) / PC)
        pj = int((y - self.miny) / PC)
        return max(0, min(self.pnx - 1, pi)), max(0, min(self.pny - 1, pj))

    def pcenter(self, pi, pj):
        return self.minx + (pi + 0.5) * PC, self.miny + (pj + 0.5) * PC

    def _free_cell_near(self, pi, pj):
        if not self.pb[pj * self.pnx + pi]:
            return pi, pj
        for r in range(1, 12):
            for dj in range(-r, r + 1):
                for di in range(-r, r + 1):
                    if max(abs(di), abs(dj)) != r:
                        continue
                    a, b = pi + di, pj + dj
                    if 0 <= a < self.pnx and 0 <= b < self.pny and not self.pb[b * self.pnx + a]:
                        return a, b
        return pi, pj

    def astar(self, start, goal, budget_s=0.012, max_exp=6000):
        """Return list of (x, y) waypoints from start to goal (coarse), or None."""
        t_end = time.perf_counter() + budget_s
        si, sj = self._free_cell_near(*self.pcell(start[0], start[1]))
        gi, gj = self._free_cell_near(*self.pcell(goal[0], goal[1]))
        nx = self.pnx
        s = sj * nx + si
        g = gj * nx + gi
        if s == g:
            return [goal]
        pb, pn = self.pb, self.pnear
        gs = {s: 0.0}
        came = {}
        h0 = lambda i, j: (max(abs(i - gi), abs(j - gj)) + 0.414 * min(abs(i - gi), abs(j - gj))) * 1.25
        openh = [(h0(si, sj), s)]
        best, besth = s, h0(si, sj)
        exp = 0
        closed = set()
        while openh:
            _, c = heapq.heappop(openh)
            if c in closed:
                continue
            if c == g:
                best = c
                break
            closed.add(c)
            exp += 1
            if exp > max_exp or (exp & 63) == 0 and time.perf_counter() > t_end:
                break
            ci, cj = c % nx, c // nx
            gc = gs[c]
            for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
                ni, nj = ci + di, cj + dj
                if ni < 0 or nj < 0 or ni >= nx or nj >= self.pny:
                    continue
                n = nj * nx + ni
                if pb[n] or n in closed:
                    continue
                if di and dj and (pb[cj * nx + ni] or pb[nj * nx + ci]):
                    continue
                ng = gc + (1.414 if di and dj else 1.0) + (0.6 if pn[n] else 0.0)
                if ng < gs.get(n, 1e18):
                    gs[n] = ng
                    came[n] = c
                    hh = h0(ni, nj)
                    if hh < besth:
                        besth, best = hh, n
                    heapq.heappush(openh, (ng + hh, n))
        if best != g and best == s:
            return None
        path = []
        c = best
        while c != s:
            path.append(self.pcenter(c % nx, c // nx))
            c = came[c]
        path.reverse()
        if best == g:
            path.append(goal)
        return path
