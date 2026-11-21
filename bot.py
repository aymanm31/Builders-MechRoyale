"""mechfight/1 bot: survive first, fight only when ahead, dodge, heal behind cover, respect the zone."""
import math
import os
import random
import sys
import time

import mechfight as mf
from world import World

TAU = math.tau
UPGRADE_RANK = [
    "nanites", "armor", "plating", "laser_damage", "lightning_efficiency", "gyros", "sensors",
    "gun_damage", "laser_charge", "servos", "gun_rate", "reactor", "capacitor", "jets",
    "lightning_chain", "gun_velocity", "gun_mag", "lightning_range", "laser_pierce",
    "mortar_reload", "mortar_splash", "mortar_velocity",
]


def adiff(a, b):
    return (a - b + math.pi) % TAU - math.pi


def hyp(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def norm(x, y):
    d = math.hypot(x, y)
    return (x / d, y / d) if d > 1e-9 else (0.0, 0.0)


def rot(x, y, a):
    c, s = math.cos(a), math.sin(a)
    return x * c - y * s, x * s + y * c


class Bot:
    name = "survivor-v0"

    def __init__(self, hello):
        self.cfg = hello["config"]
        self.slot = hello["slot"]
        random.seed(hello.get("seed", 0))
        dirs = []
        m = hello.get("map") or {}
        if m.get("dir"):
            dirs.append(m["dir"])
        here = os.path.dirname(os.path.abspath(__file__))
        dirs += [os.path.join(here, "arena"), "/arena", "arena"]
        t = time.time()
        self.world = World(dirs)
        mf.log("map loaded in %.2fs" % (time.time() - t))
        c = self.cfg
        self.wcfg = c["weapons"]
        self.tick_budget = c["bot"]["tick_budget_ms"] / 1000.0
        # state
        self.known_pickups = {}
        self.enemy_seen = {}
        self.last_dmg_t = -99.0
        self.last_dmg_bearing = None
        self.alert = None  # (bearing, until)
        self.flee_until = 0.0
        self.strafe = 1
        self.strafe_until = 0.0
        self.path = []
        self.path_goal = None
        self.path_t = -99.0
        self.hide = None
        self.hide_t = -99.0
        self.pos_hist = []
        self.stuck_until = 0.0
        self.stuck_dir = (1.0, 0.0)
        self.spin = 0.0
        self.last_move = (0.0, 0.0)
        self.hp_prev = None
        self.bad_pickups = {}
        self.laser_hold = 0
        self.t = 0.0

    # ------------------------------------------------------------------ utils
    def time_left(self):
        return self.tick_budget * 0.7 - (time.perf_counter() - self.t0)

    def scale(self):
        return 1.0 + self.cfg["cubes"]["size_per_cube"] * self.me.get("cubes", 0)

    def eye(self):
        s = self.me["stats"]
        return self.pos[2] + s.get("eye_height", 8)

    def muzzle(self):
        s = self.me["stats"]
        return (self.pos[0], self.pos[1], self.pos[2] + s.get("muzzle_height", 7))

    def en_center(self, e):
        sc = 1.0 + 0.05 * e.get("cubes", 0)
        return (e["pos"][0], e["pos"][1], e["pos"][2] + 4.5 * sc)

    # ------------------------------------------------------------------ zone
    def circle_at(self, tt):
        z = self.obs["zone"]
        ss, se = z["shrink_start"], z["shrink_end"]
        if tt <= ss or se <= ss:
            f = 0.0
        elif tt >= se:
            f = 1.0
        else:
            f = (tt - ss) / (se - ss)
        c0, c1 = z["center"], z["next_center"]
        return (c0[0] + (c1[0] - c0[0]) * f, c0[1] + (c1[1] - c0[1]) * f,
                z["radius"] + (z["next_radius"] - z["radius"]) * f)

    def zone_goal(self):
        """Return (goal_xy or None, urgent). goal None means position is fine."""
        z = self.obs["zone"]
        t = self.t
        nc, nr = z["next_center"], z["next_radius"]
        d_next = hyp(self.pos, nc)
        need = max(0.0, d_next - 0.6 * nr) / 8.5
        if t >= z["shrink_start"] - need - 12:
            cx, cy, r = nc[0], nc[1], nr
            if t < z["shrink_end"]:
                cx, cy, r = self.circle_at(min(z["shrink_end"], t + 10))
                if r < nr:
                    cx, cy, r = nc[0], nc[1], nr
        else:
            cx, cy, r = self.circle_at(t + 5)
        d = math.hypot(self.pos[0] - cx, self.pos[1] - cy)
        urgent = not self.me.get("in_zone", True)
        lim = max(6.0, 0.6 * r) if r > 40 else max(3.0, 0.4 * r)
        if d <= lim and not urgent:
            return None, False
        if r <= 40:
            return (cx, cy), urgent
        ux, uy = norm(self.pos[0] - cx, self.pos[1] - cy)
        return (cx + ux * 0.5 * r, cy + uy * 0.5 * r), urgent

    def in_safe_zone(self, p, margin=10.0):
        cx, cy, r = self.circle_at(self.t + 6)
        return math.hypot(p[0] - cx, p[1] - cy) < r - margin

    # ------------------------------------------------------------------ ingest
    def ingest(self, obs):
        self.obs = obs
        self.t = obs["t"]
        me = self.me = obs["self"]
        self.pos = me["pos"]
        self.vel = me["vel"]
        self.aim = me["aim"]
        st = me["stats"]
        self.hp, self.maxhp = me["hp"], me["max_hp"]
        self.hpf = self.hp / max(1.0, self.maxhp)
        for e in obs["events"]:
            ty = e["type"]
            if ty == "damaged":
                self.last_dmg_t = self.t
                if e.get("bearing") is not None:
                    self.last_dmg_bearing = e["bearing"]
                    self.alert = (e["bearing"], self.t + 2.0)
                if e.get("weapon") == "laser":
                    self.strafe_until = 0
            elif ty == "hit":
                self.last_dmg_t = self.t
            elif ty == "took":
                self.known_pickups.pop(e["id"], None)
            elif ty == "take_failed":
                if e["reason"] in ("gone",):
                    self.known_pickups.pop(e["id"], None)
                else:
                    self.bad_pickups[e["id"]] = self.t + 20
        for p in obs["pickups"]:
            self.known_pickups[p["id"]] = (p["kind"], p["pos"])
        vis_ids = {p["id"] for p in obs["pickups"]}
        for pid in list(self.known_pickups):
            kind, pp = self.known_pickups[pid]
            if pid not in vis_ids and hyp(pp, self.pos) < 25 and abs(pp[2] - self.pos[2]) < 12:
                self.known_pickups.pop(pid, None)
        # sounds -> alert
        for s in obs["sounds"]:
            if s["kind"] in ("gun", "laser", "laser_charge", "lightning", "mortar") and s["dist"] in ("near", "mid"):
                if self.alert is None or self.alert[1] < self.t + 0.5:
                    self.alert = (s["bearing"], self.t + 1.0)
        # enemies
        near_r = self.cfg["vision"]["near_range"] + st.get("near_range_bonus", 0)
        rng = self.cfg["vision"]["range"] + st.get("vision_bonus", 0)
        half_fov = math.radians(self.cfg["vision"]["fov_deg"]) / 2
        eye = (self.pos[0], self.pos[1], self.eye())
        self.enemies = []
        self.npcs = []
        for m in obs["mechs"]:
            d = hyp(m["pos"], self.pos)
            c = self.en_center(m)
            vis = False
            if d <= rng + 2:
                yaw = math.atan2(m["pos"][1] - self.pos[1], m["pos"][0] - self.pos[0])
                if d <= near_r or abs(adiff(yaw, self.aim[0])) <= half_fov:
                    vis = self.world.los(eye, c)
            m["_d"] = d
            m["_vis"] = vis
            if m["kind"] == "mech":
                self.enemies.append(m)
                if vis:
                    self.enemy_seen[m["id"]] = self.t
            else:
                self.npcs.append(m)
        self.enemies.sort(key=lambda m: m["_d"])
        # stuck detection
        self.pos_hist.append((self.t, self.pos[0], self.pos[1]))
        self.pos_hist = [h for h in self.pos_hist if self.t - h[0] <= 1.2]

    # ------------------------------------------------------------------ power estimate
    def dps_of(self, weapons, d, dist_hit=0.5):
        s = 0.0
        for w in weapons:
            if w == "gun":
                s += 187.0 * (0.6 if d < 80 else 0.4 if d < 160 else 0.25) if d < 235 else 0.0
            elif w == "laser":
                s += 130.0 * 0.7 if d < 280 else 0.0
            elif w == "lightning":
                s += 220.0 if d < 45 else 0.0
            elif w == "mortar":
                s += 138.0 * 0.25 if d < 230 else 0.0
        return s

    def my_weapons(self):
        return [w["kind"] for w in self.me["weapons"] if w]

    def has(self, kind):
        return kind in self.my_weapons()

    # ------------------------------------------------------------------ movement helpers
    def free_dir(self, dx, dy, look=(3.0, 6.0, 9.0)):
        """Rotate (dx,dy) until the ground ahead is free of walls."""
        w = self.world
        if dx == 0 and dy == 0:
            return 0.0, 0.0
        for a in (0, 0.4, -0.4, 0.8, -0.8, 1.2, -1.2, 1.6, -1.6, 2.0, -2.0):
            rx, ry = rot(dx, dy, a)
            ok = True
            for L in look:
                if w.walk_blocked(self.pos[0] + rx * L, self.pos[1] + ry * L):
                    ok = False
                    break
            if ok:
                return rx, ry
        return dx, dy

    def path_dir(self, goal, replan_s=2.5):
        """Direction toward goal following A* waypoints."""
        w = self.world
        d = hyp(goal, self.pos)
        if d < 6:
            self.path = []
            return norm(goal[0] - self.pos[0], goal[1] - self.pos[1])
        if w.line_walkable(self.pos, goal, 2.0) and d < 60:
            self.path = []
            return norm(goal[0] - self.pos[0], goal[1] - self.pos[1])
        need = (not self.path or self.path_goal is None or hyp(self.path_goal, goal) > 10
                or self.t - self.path_t > replan_s)
        if need and self.time_left() > 0.02:
            p = w.astar(self.pos, goal)
            self.path_goal = goal
            self.path_t = self.t
            self.path = p or []
        # drop reached waypoints
        while len(self.path) > 1 and hyp(self.path[0], self.pos) < 5:
            self.path.pop(0)
        if not self.path:
            return norm(goal[0] - self.pos[0], goal[1] - self.pos[1])
        tgt = self.path[0]
        for k in range(min(len(self.path), 8) - 1, -1, -1):
            if w.line_walkable(self.pos, self.path[k], 2.0):
                tgt = self.path[k]
                break
        return norm(tgt[0] - self.pos[0], tgt[1] - self.pos[1])

    def find_hide(self, threats):
        """Nearest point where no threat has line of sight to us; None if not found in time."""
        w = self.world
        if not threats:
            return None
        cands = []
        px, py = self.pos[0], self.pos[1]
        for r in (6, 12, 20, 30, 42, 56):
            n = 12 if r < 20 else 16
            off = random.random() * TAU
            for k in range(n):
                a = off + TAU * k / n
                x, y = px + r * math.cos(a), py + r * math.sin(a)
                if w.walk_blocked(x, y) or not self.in_safe_zone((x, y), 6):
                    continue
                away = 0.0
                for th in threats:
                    away += hyp((x, y), th) - hyp(self.pos, th)
                cands.append((r - 0.3 * away / len(threats), x, y))
        cands.sort()
        t_end = time.perf_counter() + min(0.012, max(0.0, self.time_left() - 0.01))
        g_here = self.pos[2]
        for _, x, y in cands:
            if time.perf_counter() > t_end:
                break
            if not w.line_walkable(self.pos, (x, y), 2.0):
                continue
            g = w.ground(x, y)
            ok = True
            for th in threats:
                eye = (th[0], th[1], th[2] + 8.0)
                if w.los(eye, (x, y, g + 4.5), 3.0) or w.los(eye, (x, y, g + 8.5), 3.0):
                    ok = False
                    break
            if ok:
                return (x, y)
        return None

    # ------------------------------------------------------------------ dodging
    def dodge(self, mx, my, hold_fast=True):
        obs = self.obs
        projs = obs["projectiles"]
        if not projs:
            return mx, my
        me = self.me
        st = me["stats"]
        rad = st.get("radius", 2.5) + 0.7
        speed = st["walk_speed"]
        acc = st["ground_accel"] if me.get("grounded", True) else st["air_accel"]
        px, py, pz = self.pos
        shells = []
        bullets = []
        for p in projs:
            pp, vv = p["pos"], p["vel"]
            d = hyp(pp, self.pos)
            if p["kind"] == "mortar":
                # integrate to ground
                g = self.cfg["weapons"]["mortar"]["gravity"]
                x, y, z = pp
                vx, vy, vz = vv
                tt = 0.0
                for _ in range(140):
                    tt += 0.05
                    x += vx * 0.05
                    y += vy * 0.05
                    vz -= g * 0.05
                    z += vz * 0.05
                    if z <= self.world.ground(x, y) + 0.5 or self.world.solid(x, y, z):
                        break
                shells.append((x, y, tt))
            elif d < 160:
                bullets.append((pp, vv, d))
        if not shells and not bullets:
            return mx, my
        bullets.sort(key=lambda b: b[2])
        bullets = bullets[:10]
        cands = [(mx, my)]
        for k in range(16):
            a = TAU * k / 16
            cands.append((math.cos(a), math.sin(a)))
        cands.append((0.0, 0.0))
        dt = 0.05
        steps = 14
        best, bestc = (mx, my), 1e18
        dev0 = math.atan2(my, mx) if (mx or my) else 0.0
        for ci, (cx, cy) in enumerate(cands):
            tvx, tvy = cx * speed, cy * speed
            x, y = px, py
            vx, vy = self.vel[0], self.vel[1]
            cost = 0.0
            traj = []
            for s in range(steps):
                dvx, dvy = tvx - vx, tvy - vy
                dn = math.hypot(dvx, dvy)
                mxv = acc * dt
                if dn > mxv:
                    dvx, dvy = dvx / dn * mxv, dvy / dn * mxv
                vx += dvx
                vy += dvy
                x0, y0 = x, y
                x += vx * dt
                y += vy * dt
                traj.append((x, y))
                t0 = s * dt
                for pp, vv, d in bullets:
                    # relative linear motion over this step
                    bx0, by0 = pp[0] + vv[0] * t0 - x0, pp[1] + vv[1] * t0 - y0
                    bx1, by1 = pp[0] + vv[0] * (t0 + dt) - x, pp[1] + vv[1] * (t0 + dt) - y
                    ex, ey = bx1 - bx0, by1 - by0
                    L2 = ex * ex + ey * ey
                    f = 0.0 if L2 < 1e-9 else max(0.0, min(1.0, -(bx0 * ex + by0 * ey) / L2))
                    cxx, cyy = bx0 + ex * f, by0 + ey * f
                    dd = math.hypot(cxx, cyy)
                    bz = pp[2] + vv[2] * (t0 + f * dt)
                    if dd < rad + 3.0 and -1.0 < bz - pz < 10.5:
                        if dd < rad:
                            cost += 100.0 + (steps - s)
                        else:
                            cost += 4.0 * (rad + 3.0 - dd)
            for sx, sy, tt in shells:
                k = min(steps - 1, int(tt / dt))
                if tt > steps * dt:
                    qx, qy = traj[-1][0] + vx * (tt - steps * dt), traj[-1][1] + vy * (tt - steps * dt)
                else:
                    qx, qy = traj[k]
                dd = math.hypot(qx - sx, qy - sy)
                if dd < 9.5:
                    cost += 60.0 + (9.5 - dd) * 4
            if cost == 0.0 and ci == 0:
                return mx, my
            ang = abs(adiff(math.atan2(cy, cx), dev0)) if (cx or cy) and (mx or my) else 0.0
            cost += 2.0 * ang
            if (cx or cy) and self.world.walk_blocked(px + cx * 5, py + cy * 5):
                cost += 40.0
            if not (cx or cy):
                cost += 3.0
            if cost < bestc:
                bestc, best = cost, (cx, cy)
        return best

    # ------------------------------------------------------------------ aiming
    def lead(self, tp, tv, speed, src):
        d = math.dist(src, tp)
        t = d / speed
        p = tp
        for _ in range(4):
            p = (tp[0] + tv[0] * t, tp[1] + tv[1] * t, tp[2] + tv[2] * t * 0.3)
            t = min(1.5, math.dist(src, p) / speed)
        return p, t

    def aim_at(self, p, src):
        dx, dy, dz = p[0] - src[0], p[1] - src[1], p[2] - src[2]
        h = math.hypot(dx, dy)
        pitch = max(self.me["stats"].get("min_pitch", -0.7), min(self.me["stats"].get("max_pitch", 1.3), math.atan2(dz, max(h, 0.01))))
        return math.atan2(dy, dx), pitch

    # ------------------------------------------------------------------ main
    def act(self, obs):
        self.t0 = time.perf_counter()
        self.ingest(obs)
        me = self.me
        act = {"tick": obs["tick"]}
        # upgrades
        if obs.get("offer"):
            best, bi = 99, 0
            for i, ch in enumerate(obs["offer"]["choices"]):
                r = UPGRADE_RANK.index(ch["id"]) if ch["id"] in UPGRADE_RANK else 50
                if r < best:
                    best, bi = r, i
            act["pick"] = bi
        t = self.t
        weapons = me["weapons"]
        wk = self.my_weapons()
        recent_dmg = t - self.last_dmg_t < 1.5
        in_zone = me.get("in_zone", True)

        vis_en = [e for e in self.enemies if e["_vis"] and e["_d"] < 235]
        mem_en = [e for e in self.enemies if (not e["_vis"]) and e["_d"] < 220 and t - self.enemy_seen.get(e["id"], -99) < 5.0]

        target = None
        mode = "idle"
        move = (0.0, 0.0)
        boost = False
        fire = [False, False]
        aim_yaw, aim_pitch = self.aim[0], self.aim[1]
        reload = False
        speed_frac = 1.0

        # ---- decide fight / flee
        flee = False
        if vis_en or mem_en:
            tot_en_dps = 0.0
            for e in vis_en + mem_en:
                tot_en_dps += self.dps_of(e.get("weapons", ["gun"]), e["_d"])
            if vis_en:
                target = min(vis_en, key=lambda e: e.get("hp", 1400) * (0.6 + e["_d"] / 250.0))
                d = target["_d"]
                my_dps = max(1.0, self.dps_of(wk, d))
                ttk_me = target.get("hp", 1400) / my_dps
                ttk_en = self.hp / max(1.0, tot_en_dps)
                ratio = ttk_me / max(0.1, ttk_en)
                thr = 1.0 if self.hpf > 0.7 else 0.6
                if len(vis_en) + len(mem_en) >= 3:
                    thr *= 0.6
                if ratio > thr or self.hpf < 0.3:
                    flee = True
            elif recent_dmg or self.hpf < 0.9:
                flee = True
        elif recent_dmg and self.alert is not None:
            flee = True
        if flee:
            self.flee_until = t + 1.5
        fleeing = t < self.flee_until

        threats = [e["pos"] for e in (vis_en + mem_en)]
        if not threats and self.alert and self.alert[1] > t and recent_dmg and self.last_dmg_bearing is not None:
            b = self.last_dmg_bearing
            threats = [(self.pos[0] + 120 * math.cos(b), self.pos[1] + 120 * math.sin(b), self.pos[2])]

        zgoal, zurgent = self.zone_goal()

        # ---- compute movement intent
        cornered = False
        if fleeing and threats:
            mode = "flee"
            if self.hide is None or t - self.hide_t > 1.0 or hyp(self.hide, self.pos) < 3:
                h = self.find_hide(threats)
                if h:
                    self.hide, self.hide_t = h, t
                elif self.hide is None or hyp(self.hide, self.pos) < 3:
                    self.hide = None
            if self.hide is not None and hyp(self.hide, self.pos) > 2.5:
                move = self.path_dir(self.hide)
                boost = self.hpf < 0.5 and me["energy"] > 35 and not self.has("lightning")
            elif self.hide is not None:
                move = (0.0, 0.0)  # hidden: hold still and quiet
            else:
                # no cover: run away from threats toward the zone
                ax = sum(self.pos[0] - th[0] for th in threats)
                ay = sum(self.pos[1] - th[1] for th in threats)
                move = norm(ax, ay)
                if target is not None:
                    cornered = True
        else:
            self.hide = None

        if (mode in ("idle",) or cornered) and target is not None:
            mode = "fight"
            d = target["_d"]
            ux, uy = norm(target["pos"][0] - self.pos[0], target["pos"][1] - self.pos[1])
            if self.has("laser"):
                pref = 130.0
            elif self.has("lightning") and "laser" in target.get("weapons", []):
                pref = 25.0
            elif self.has("lightning"):
                pref = 35.0
            else:
                pref = 100.0
            rad = 0.0
            if d > pref + 15:
                rad = 1.0
            elif d < pref - 15:
                rad = -1.0
            if t > self.strafe_until:
                self.strafe = -self.strafe if random.random() < 0.6 else self.strafe
                self.strafe_until = t + (0.25 + random.random() * 0.5 if target.get("charging") else 0.4 + random.random() * 0.8)
            tx, ty = -uy * self.strafe, ux * self.strafe
            move = norm(ux * rad * 0.7 + tx, uy * rad * 0.7 + ty)
            if zgoal is not None and zurgent:
                zx, zy = norm(zgoal[0] - self.pos[0], zgoal[1] - self.pos[1])
                move = norm(move[0] + zx, move[1] + zy)
        elif mode == "idle":
            move, mode_s, boost = self.idle_plan(zgoal, zurgent, vis_en, wk)
            mode = mode_s

        # ---- weapons / aim
        eye_m = self.muzzle()
        crate_target = getattr(self, "_aux_target", None)
        if target is not None:
            tc = self.en_center(target)
            tv = target["vel"]
            tdist = target["_d"]
            lead_gun, _ = self.lead(tc, tv, self.me["weapons"][0]["stats"]["speed"] if weapons[0] and weapons[0]["kind"] == "gun" else 252.0, eye_m)
            gun_yaw, gun_pitch = self.aim_at(lead_gun, eye_m)
            aim_yaw, aim_pitch = gun_yaw, gun_pitch
            los_ok = self.world.los(eye_m, tc)
            for i, w in enumerate(weapons):
                if not w:
                    continue
                k = w["kind"]
                st = w.get("stats", {})
                if k == "laser":
                    lp = (tc[0] + tv[0] * 0.2, tc[1] + tv[1] * 0.2, tc[2])
                    ly, lpit = self.aim_at(lp, eye_m)
                    err = abs(adiff(ly, self.aim[0]))
                    charge = w.get("charge", 0.0)
                    full = st.get("charge", 0.75)
                    ready = w["cooldown"] <= 0.12
                    can_start = (ready and los_ok and 20 < tdist < st.get("range", 280) - 15 and err < 0.35
                                 and "lightning" not in (target.get("weapons") or []))
                    if charge > 0 or can_start:
                        aim_yaw, aim_pitch = ly, lpit
                    if charge > 0.0:
                        if not los_ok or tdist > st.get("range", 280):
                            fire[i] = False
                        elif charge >= full - 0.04 and err < 0.04 + 2.5 / max(10.0, tdist):
                            fire[i] = False  # release
                        elif charge >= full + 0.5:
                            fire[i] = False
                        else:
                            fire[i] = True
                    elif can_start:
                        fire[i] = True
                elif k == "gun":
                    err = abs(adiff(gun_yaw, self.aim[0]))
                    tol = math.atan2(2.4, max(5.0, tdist)) + 0.01
                    laser_charging = any(x and x["kind"] == "laser" and x.get("charge", 0) > 0 for x in weapons)
                    if los_ok and tdist < st.get("range", 230) - 8 and err < tol and w.get("ammo", 1) > 0 and not (laser_charging and abs(adiff(aim_yaw, gun_yaw)) > tol):
                        fire[i] = True
                    if w.get("ammo", 1) == 0 and w.get("reload", 0) == 0:
                        reload = True
                elif k == "lightning":
                    ly, lpit = self.aim_at(tc, eye_m)
                    if tdist < st.get("range", 45) - 2 and los_ok and me["energy"] > 6 and abs(adiff(ly, self.aim[0])) < 0.18:
                        fire[i] = True
                        if tdist < 40 and not any(x and x["kind"] == "laser" and x.get("charge", 0) > 0 for x in weapons):
                            aim_yaw, aim_pitch = ly, lpit
                elif k == "mortar":
                    pass
            if weapons[0] and weapons[0]["kind"] == "gun" and weapons[0].get("ammo", 1) <= 0:
                reload = True
        else:
            # no target: aux target (crate/npc), else look around
            if crate_target is not None:
                c, kind = crate_target
                cc = (c["pos"][0], c["pos"][1], c["pos"][2] + (1.2 if kind == "crate" else 1.7))
                ay_, ap_ = self.aim_at(cc, eye_m)
                aim_yaw, aim_pitch = ay_, ap_
                if weapons[0] and weapons[0]["kind"] == "gun" and self.world.los(eye_m, cc):
                    err = abs(adiff(ay_, self.aim[0]))
                    if err < 0.06 and math.dist(cc, eye_m) < 150 and weapons[0].get("ammo", 1) > 0:
                        fire[0] = True
            elif self.alert is not None and self.alert[1] > t:
                aim_yaw, aim_pitch = self.alert[0], 0.0
            elif move != (0.0, 0.0) and mode != "hold":
                aim_yaw, aim_pitch = math.atan2(move[1], move[0]), 0.0
            else:
                self.spin = (self.spin + 0.33) % TAU
                aim_yaw, aim_pitch = adiff(self.aim[0] + 0.33, 0.0), 0.0
            if weapons[0] and weapons[0]["kind"] == "gun" and weapons[0].get("ammo", 12) < 5 and not recent_dmg:
                reload = True
        self._aux_target = None

        # ---- zone override while idle-ish handled in idle_plan; dodge projectiles
        if (move[0] or move[1]) or obs["projectiles"]:
            charging_slow = any(w and w["kind"] == "laser" and w.get("charge", 0) > 0 for w in weapons)
            if self.time_left() > 0.01:
                move = self.dodge(move[0], move[1])
        if target is None and mode not in ("flee",):
            pass
        # stuck handling
        if t < self.stuck_until:
            move = self.stuck_dir
            act["jump"] = True
            boost = me["energy"] > 20
        elif (move[0] or move[1]) and len(self.pos_hist) >= 8 and self.t - self.pos_hist[0][0] > 0.9:
            h = self.pos_hist[0]
            if math.hypot(self.pos[0] - h[1], self.pos[1] - h[2]) < 1.5 and not any(fire):
                a = random.random() * TAU
                self.stuck_dir = (math.cos(a), math.sin(a))
                self.stuck_until = t + 0.8
                self.path = []

        # take pickups
        tk = self.pickup_action(wk)
        if tk:
            act.update(tk)

        if not in_zone:
            boost = me["energy"] > 10
        if any(fire[i] and weapons[i] and weapons[i]["kind"] == "lightning" for i in range(2)):
            boost = False
        if mode == "idle" or mode == "hold":
            pass
        act["move"] = [round(move[0] * speed_frac, 3), round(move[1] * speed_frac, 3)]
        act["boost"] = bool(boost)
        act["aim"] = [round(aim_yaw, 4), round(aim_pitch, 4)]
        act["fire"] = fire
        if reload:
            act["reload"] = True
        mf.draw.label("%s hp%d %s" % (mode, self.hp, ",".join(wk)))
        return act

    # ------------------------------------------------------------------ idle planning
    def idle_plan(self, zgoal, zurgent, vis_en, wk):
        t = self.t
        me = self.me
        boost = False
        # supply drop
        for d in self.obs.get("drops", []):
            if d["lands_in"] <= 4 and self.hpf > 0.5:
                dd = hyp(d["pos"], self.pos)
                busy = any(hyp(e["pos"], d["pos"]) < d["radius"] + 30 for e in self.enemies if e["_vis"])
                if dd < 160 and not busy and (d.get("holder") in (None, self.slot)):
                    if dd > d["radius"] - 3:
                        return self.path_dir((d["pos"][0], d["pos"][1])), "drop", self.hpf > 0 and me["energy"] > 50 and dd > 40
                    return (0.0, 0.0), "hold", False
        if zgoal is not None and (zurgent or self.t - self.last_dmg_t > 0.3):
            mv = self.path_dir(zgoal)
            return mv, "zone", (zurgent and me["energy"] > 10) or (hyp(zgoal, self.pos) > 150 and me["energy"] > 85 and False)
        # heal in place if hurt and recently hit
        if self.hpf < 0.9 and t - self.last_dmg_t < 25:
            return (0.0, 0.0), "heal", False
        # weapons
        want = None
        bestv = 0.0
        have = set(wk)
        for pid, (kind, pp) in self.known_pickups.items():
            if self.bad_pickups.get(pid, 0) > t:
                continue
            v = 0.0
            if me["weapons"][1] is None:
                if kind == "laser" and "laser" not in have:
                    v = 3.0
                elif kind == "lightning" and "lightning" not in have:
                    v = 2.6
            if v <= 0:
                continue
            d = hyp(pp, self.pos)
            if d > 260 or not self.in_safe_zone(pp, 15) or abs(pp[2] - self.pos[2]) > 14:
                continue
            sc = v / (d + 40.0)
            if sc > bestv:
                bestv, want = sc, pp
        if want is not None:
            return self.path_dir((want[0], want[1])), "loot", False
        # crates / cubes / npcs when healthy
        if self.hpf > 0.7:
            for c in self.obs["cubes"]:
                if hyp(c["pos"], self.pos) < 70 and self.in_safe_zone(c["pos"], 10) and abs(c["pos"][2] - self.pos[2]) < 8:
                    return self.path_dir((c["pos"][0], c["pos"][1])), "cube", False
            best = None
            for c in self.obs["crates"]:
                d = hyp(c["pos"], self.pos)
                if d < 90 and self.in_safe_zone(c["pos"], 10) and abs(c["pos"][2] - self.pos[2]) < 8:
                    if best is None or d < best[0]:
                        best = (d, c, "crate")
            if best is None and self.hpf > 0.9:
                for n in self.npcs:
                    d = n["_d"]
                    if d < 55 and n["_vis"] and self.in_safe_zone(n["pos"], 10):
                        if best is None or d < best[0]:
                            best = (d, n, "npc")
            if best is not None:
                d, c, kind = best
                self._aux_target = (c, kind)
                if d > 38:
                    return self.path_dir((c["pos"][0], c["pos"][1])), "farm", False
                if d < 22:
                    ux, uy = norm(self.pos[0] - c["pos"][0], self.pos[1] - c["pos"][1])
                    return (ux * 0.5, uy * 0.5), "farm", False
                return (0.0, 0.0), "farm", False
        return (0.0, 0.0), "hold", False

    # ------------------------------------------------------------------ pickups
    def pickup_action(self, wk):
        me = self.me
        if me["weapons"][1] is not None:
            return None
        have = set(wk)
        best = None
        for pid, (kind, pp) in self.known_pickups.items():
            if self.bad_pickups.get(pid, 0) > self.t:
                continue
            if kind in have or kind == "mortar" or kind == "gun":
                continue
            if hyp(pp, self.pos) < 3.6 and abs(pp[2] - self.pos[2]) < 3.5:
                pr = 2 if kind == "laser" else 1
                if best is None or pr > best[0]:
                    best = (pr, pid)
        if best:
            return {"take": best[1]}
        return None


if __name__ == "__main__":
    mf.run(Bot)
