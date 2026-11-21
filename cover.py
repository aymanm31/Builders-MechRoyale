"""Cover planning for the peek-and-hide loop.

Given where a threat is, find a pair of points:
    C  a hidden spot (the threat has no line of sight to a mech standing there)
    P  the closest nearby spot that *does* see the threat (where we step out to shoot)
so the bot can sit at C, step to P, shoot for a fraction of a second, and step back.
"""
import math
import time

TAU = math.tau


class CoverPlanner:
    def __init__(self, world):
        self.w = world

    def hidden(self, th_eye, x, y, margin=False):
        """True if a mech standing at (x, y) cannot be seen from th_eye (centre and head tested).
        With margin=True the left/right edges of the 2.5 m-radius body are tested too, so a spot that
        is only hidden by a hair at the centre line does not count."""
        w = self.w
        g = w.ground(x, y)
        if w.los(th_eye, (x, y, g + 4.5), 3.0) or w.los(th_eye, (x, y, g + 8.0), 3.0):
            return False
        if margin:
            dx, dy = x - th_eye[0], y - th_eye[1]
            d = math.hypot(dx, dy) or 1.0
            px, py = -dy / d * 2.0, dx / d * 2.0
            for s in (1.0, -1.0):
                if w.los(th_eye, (x + px * s, y + py * s, g + 4.5), 3.0):
                    return False
        return True

    def visible(self, th_eye, x, y):
        w = self.w
        g = w.ground(x, y)
        return w.los(th_eye, (x, y, g + 4.5), 3.0)

    def _maybe_blocked(self, x, y, ux, uy):
        """Cheap prefilter: is there a solid between (x, y) and the threat within ~28 m?"""
        w = self.w
        z = w.ground(x, y) + 4.5
        for L in (5.0, 9.0, 14.0, 20.0, 28.0):
            if w.solid(x + ux * L, y + uy * L, z):
                return True
        return False

    def plan(self, me, threat, safe_fn, budget_s=0.014, rng=None, avoid=None, extra=()):
        """me: (x, y, z); threat: (x, y, z) feet position of the threat.

        `extra` is a list of further threat feet-positions the cover must also hide from (the peek
        point only has to see the main threat).
        Returns dict(C=(x, y), P=(x, y) | None, dthr=float) or None if nothing was found in time.
        `avoid` is an optional list of (x, y) recently used cover spots to de-prioritise.
        """
        t_end = time.perf_counter() + budget_s
        w = self.w
        mx, my = me[0], me[1]
        th_eye = (threat[0], threat[1], threat[2] + 8.0)
        extra_eyes = [(e[0], e[1], e[2] + 8.0) for e in list(extra)[:2]]
        tdx, tdy = threat[0] - mx, threat[1] - my
        dth = math.hypot(tdx, tdy)
        rnd = rng.random if rng else (lambda: 0.5)
        found = []
        for r, n in ((4, 8), (8, 10), (13, 12), (19, 14), (27, 14), (37, 16), (50, 16)):
            off = rnd() * TAU
            for k in range(n):
                if time.perf_counter() > t_end:
                    break
                a = off + TAU * k / n
                x, y = mx + r * math.cos(a), my + r * math.sin(a)
                if w.walk_blocked(x, y) or not safe_fn((x, y)):
                    continue
                dx, dy = threat[0] - x, threat[1] - y
                d = math.hypot(dx, dy)
                if d < 14:
                    continue
                ux, uy = dx / d, dy / d
                if not self._maybe_blocked(x, y, ux, uy):
                    continue
                if not self.hidden(th_eye, x, y, margin=True):
                    continue
                if extra_eyes and not all(self.hidden(ee, x, y) for ee in extra_eyes):
                    continue
                cost = r
                if d < 28:
                    cost += 18
                elif d > 170:
                    cost += 18
                if avoid:
                    for ax, ay in avoid:
                        if math.hypot(ax - x, ay - y) < 6:
                            cost += 10
                            break
                found.append((cost + rnd() * 2.0, x, y, d))
            if len(found) >= 8 and r >= 13:
                break
        if not found:
            return None
        found.sort()
        best = None
        for cost, x, y, d in found[:12]:
            if time.perf_counter() > t_end + 0.004 and best is not None:
                break
            if not w.line_walkable(me, (x, y), 2.0):
                cost += 25
            P = self._peek_point(th_eye, x, y, threat, t_end + 0.005)
            if P is None:
                total = cost + 20
            else:
                total = cost + 2.5 * math.hypot(P[0] - x, P[1] - y)
            if best is None or total < best[0]:
                best = (total, x, y, P, d)
        if best is None:
            cost, x, y, d = found[0]
            best = (cost, x, y, None, d)
        return {"C": (best[1], best[2]), "P": best[3], "dthr": best[4]}

    def _peek_point(self, th_eye, cx, cy, threat, deadline=1e18):
        """Nearest walkable spot to (cx, cy) from which the threat is visible."""
        w = self.w
        base = math.atan2(threat[1] - cy, threat[0] - cx)
        # sideways first (classic corner peek), then diagonals, then straight at the threat
        for dist in (2.5, 4.0, 6.0, 8.5, 12.0, 16.0, 21.0):
            if time.perf_counter() > deadline:
                return None
            for da in (math.pi / 2, -math.pi / 2, math.pi / 3, -math.pi / 3, math.pi / 6,
                       -math.pi / 6, 2.4, -2.4, 0.0, 2.0, -2.0, 2.8, -2.8):
                x, y = cx + dist * math.cos(base + da), cy + dist * math.sin(base + da)
                if w.walk_blocked(x, y):
                    continue
                if not self.visible(th_eye, x, y):
                    continue
                if not w.line_walkable((cx, cy), (x, y), 1.5):
                    continue
                return (x, y)
        return None
