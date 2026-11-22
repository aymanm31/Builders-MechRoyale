"""mechfight/1 bot v1 "peeker".

Early game (weak): fight from cover. Sit hidden with the torso pre-aimed at the enemy, step out to a
peek point, shoot for a fraction of a second, step back. Dodge with short thruster bursts.
Late game (strong): stop being polite, hunt whatever it sees/hears and beam it.
Whenever it runs, it runs while facing and shooting.
"""
import math
import os
import random
import time

import mechfight as mf
from cover import CoverPlanner
from roof import RoofPlanner
from world import World

TAU = math.tau

# ----------------------------------------------------------------------------- tunables
AGGRO_SCORE = float(os.environ.get("MECHBOT_AGGRO", "0"))  # 0 = always push; was 4.0 in v1.1        # power score at which the bot stops peeking and just beams (see power())
DUEL = os.environ.get("MECHBOT_DUEL", "1") == "1"   #              # fight a lone, not-clearly-stronger enemy even when weak
AGGRO_HP_MIN = 0.40      # below this hp fraction even a strong bot goes back to cover
PEEK_BUDGET = {"gun": 0.75, "laser": 1.1, "lightning": 1.0}   # max seconds exposed per peek
PEEK_DMG_CAP = 0.11      # leave the peek once we lost this fraction of max hp during it
MAX_ENGAGE = 215.0       # don't start fights beyond this range
KICK_ENERGY = 40.0       # min energy to spend on a thruster kick
UPGRADE_RANK = [
    "armor", "nanites", "plating", "gyros", "servos", "laser_damage", "gun_damage", "jets",
    "capacitor", "laser_charge", "gun_rate", "lightning_efficiency", "reactor", "sensors",
    "lightning_chain", "gun_velocity", "gun_mag", "lightning_range", "laser_pierce",
    "mortar_reload", "mortar_splash", "mortar_velocity",
]
ROOF_ENABLED = os.environ.get("MECHBOT_ROOF", "0") == "1"   # rooftops: off until proven (v1.3 dropped to 2.69)
AIM_LAT = 0.06            # s between the observation and the action taking effect (lead compensation)
LASER_RANK = ["laser_damage", "laser_charge", "armor", "laser_pierce"]
HOSTILE_SOUNDS = ("gun", "laser", "laser_charge", "lightning", "mortar", "explosion")
CLOSE_SOUNDS = ("step", "boost", "jet", "reload")


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
    name = "peeker-v1.4"

    def __init__(self, hello):
        self.cfg = hello["config"]
        self.slot = hello["slot"]
        self.rng = random.Random(hello.get("seed", 0))
        random.seed(hello.get("seed", 0))
        dirs = []
        m = hello.get("map") or {}
        if m.get("dir"):
            dirs.append(m["dir"])
        here = os.path.dirname(os.path.abspath(__file__))
        dirs += [os.path.join(here, "arena"), "/arena", "arena"]
        t = time.time()
        self.world = World(dirs)
        self.planner = CoverPlanner(self.world)
        self.rp = RoofPlanner(self.world, dirs)
        self.roof = None
        self.roof_cd = 10.0
        self._roof_jump = False
        mf.log("map loaded in %.2fs" % (time.time() - t))
        self.wcfg = self.cfg["weapons"]
        self.tick_budget = self.cfg["bot"]["tick_budget_ms"] / 1000.0
        # world knowledge
        self.known_pickups = {}
        self.bad_pickups = {}
        self.enemy_seen = {}
        # damage / alerts
        self.last_dmg_t = -99.0
        self.last_dmg_bearing = None
        self.alert = None            # (bearing, until, dist_kind)
        self.hunt = None             # (x, y, until)
        self.ghosts = []             # unseen shooters: (x, y, z, until)
        self.rear_t = -99.0          # last time we were hit from behind
        self.rear_bearing = 0.0
        self.reloc_t = -99.0
        self.hp_hist = []
        # combat state machine
        self.cs = None               # None | cover | peek | retreat
        self.cs_t = 0.0
        self.cov = None
        self.nocover_until = 0.0
        self.recent_cover = []
        self.next_peek = 0.0
        self.expo = 0.0
        self.peek_hp0 = 0.0
        self.peek_t0 = 0.0
        self.peek_count = 0
        self.aggro_state = False
        self.duel_state = False
        self.focus_id = None
        self.strafe = 1
        self.strafe_until = 0.0
        self.kick_until = 0.0
        self.kick_ready = 0.0
        self.force_release = False
        # navigation
        self.path = []
        self.path_goal = None
        self.path_t = -99.0
        self.pos_hist = []
        self.stuck_until = 0.0
        self.stuck_dir = (1.0, 0.0)
        self.spin = 0.0
        self.sh_key = None
        self.sh_cands = []
        self.sh_best = None
        self.sh_i = 0
        # misc
        self.t = 0.0
        self.prev_t = None
        self.dt = 0.1
        self.mode = "idle"
        self.last_mode = None
        self._aux_target = None
        self.log_lines = 0
        self.log_imp = 0
        self.next_beat = 15.0
        self.stats_hits = 0
        self.stats_dmg_in = 0.0
        self.stats_dmg_out = 0.0

    # ------------------------------------------------------------------ utils
    def log(self, msg, important=False):
        """stderr post-mortem. Chatty lines (mode changes, cover plans) have their own cap so they can
        never crowd out the important ones (hits, kills, aggro flips, heartbeat)."""
        if important:
            if self.log_imp >= 700:
                return
            self.log_imp += 1
        else:
            if self.log_lines >= 450:
                return
            self.log_lines += 1
        mf.log("[t=%5.1f hp=%4d c=%d] %s" % (self.t, self.hp, self.me.get("cubes", 0), msg))

    def time_left(self):
        return self.tick_budget * 0.7 - (time.perf_counter() - self.t0)

    def eye(self):
        return self.pos[2] + self.me["stats"].get("eye_height", 8)

    def muzzle(self):
        return (self.pos[0], self.pos[1], self.pos[2] + self.me["stats"].get("muzzle_height", 7))

    def en_center(self, e):
        sc = 1.0 + 0.05 * e.get("cubes", 0)
        return (e["pos"][0], e["pos"][1], e["pos"][2] + 4.5 * sc)

    def weapon(self, kind):
        for i, w in enumerate(self.me["weapons"]):
            if w and w["kind"] == kind:
                return i, w
        return None, None

    def my_weapons(self):
        return [w["kind"] for w in self.me["weapons"] if w]

    def has(self, kind):
        return kind in self.my_weapons()

    # ------------------------------------------------------------------ zone (unchanged logic from v0)
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
        t = obs["t"]
        self.dt = min(0.5, max(0.05, t - self.prev_t)) if self.prev_t is not None else 0.1
        self.prev_t = t
        self.t = t
        me = self.me = obs["self"]
        self.pos, self.vel, self.aim = me["pos"], me["vel"], me["aim"]
        st = me["stats"]
        self.hp, self.maxhp = me["hp"], me["max_hp"]
        self.hpf = self.hp / max(1.0, self.maxhp)
        self.hp_hist.append((t, self.hp))
        self.hp_hist = [h for h in self.hp_hist if t - h[0] <= 1.5]
        self.new_dmg = []
        self._dmg_log = [(a, b) for a, b in getattr(self, "_dmg_log", []) if t - a < 4.0]
        for e in obs["events"]:
            ty = e["type"]
            if ty == "damaged":
                self.last_dmg_t = t
                self.stats_dmg_in += e.get("amount", 0)
                self.new_dmg.append(e)
                if e.get("weapon") != "zone":
                    self._dmg_log.append((t, e.get("amount", 0)))
                if e.get("bearing") is not None:
                    self.last_dmg_bearing = e["bearing"]
                    self.alert = (e["bearing"], t + 2.0, "near")
                    if e.get("rear"):
                        self.rear_t, self.rear_bearing = t, e["bearing"]
                self.log("HIT -%d %s by=%s%s" % (e.get("amount", 0), e.get("weapon"), e.get("by"),
                                                  " REAR" if e.get("rear") else ""), True)
            elif ty == "hit":
                self.last_dmg_t = t
                self.stats_hits += 1
                self.stats_dmg_out += e.get("amount", 0)
            elif ty == "kill":
                self.log("KILL %s" % e.get("target"), True)
            elif ty == "took":
                self.known_pickups.pop(e["id"], None)
                self.log("took %s" % e.get("kind"))
            elif ty == "take_failed":
                if e["reason"] == "gone":
                    self.known_pickups.pop(e["id"], None)
                else:
                    self.bad_pickups[e["id"]] = t + 20
            elif ty == "upgrade":
                self.log("upgrade %s" % e.get("id"), True)
            elif ty == "effect":
                self.log("EFFECT %s by=%s" % (e.get("effect"), e.get("by")), True)
        for p in obs["pickups"]:
            self.known_pickups[p["id"]] = (p["kind"], p["pos"])
        vis_ids = {p["id"] for p in obs["pickups"]}
        for pid in list(self.known_pickups):
            kind, pp = self.known_pickups[pid]
            if pid not in vis_ids and hyp(pp, self.pos) < 25 and abs(pp[2] - self.pos[2]) < 12:
                self.known_pickups.pop(pid, None)
        # sounds -> alert + hunting candidates
        self.heard = []
        for s in obs["sounds"]:
            k, dk = s["kind"], s["dist"]
            if k in HOSTILE_SOUNDS and dk in ("near", "mid"):
                self.heard.append(s)
                if self.alert is None or self.alert[1] < t + 0.5:
                    self.alert = (s["bearing"], t + 1.2, dk)
            elif k in CLOSE_SOUNDS and dk == "near":
                if self.alert is None or self.alert[1] < t + 0.3:
                    self.alert = (s["bearing"], t + 0.8, dk)
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
            yaw = math.atan2(m["pos"][1] - self.pos[1], m["pos"][0] - self.pos[0])
            los = False
            if d <= 260:
                los = self.world.los(eye, c)
            vis = los and d <= rng + 2 and (d <= near_r or abs(adiff(yaw, self.aim[0])) <= half_fov)
            m["_d"], m["_vis"], m["_los"], m["_yaw"] = d, vis, los, yaw
            # is it pointing at us?  (its torso aim vs the line to us)
            to_me = yaw + math.pi
            m["_aimed"] = abs(adiff(m["aim"][0], to_me)) < math.atan2(3.5, max(d, 8.0)) + 0.12
            if m["kind"] == "mech":
                self.enemies.append(m)
                if vis:
                    self.enemy_seen[m["id"]] = t
            else:
                self.npcs.append(m)
        self.enemies.sort(key=lambda m: m["_d"])
        self.pos_hist.append((t, self.pos[0], self.pos[1]))
        self.pos_hist = [h for h in self.pos_hist if t - h[0] <= 1.2]

    def contested(self, p, factor=0.7):
        """True when a tracked enemy mech is much closer to point p than we are (it will get there first)."""
        myd = hyp(p, self.pos)
        for e in self.enemies:
            if not (e["_vis"] or self.t - self.enemy_seen.get(e["id"], -99) < 5.0):
                continue
            if hyp(e["pos"], p) + 12.0 < myd * factor:
                return True
        return False

    # ------------------------------------------------------------------ power / aggression
    def power(self, target=None):
        """A rough 'how strong am I' score: cubes + a second weapon + time pressure."""
        me = self.me
        p = float(me.get("cubes", 0))
        if self.has("laser") or self.has("lightning"):
            p += 2.0
        p += max(0.0, (self.t - 150.0) / 60.0)
        return p

    def is_aggro(self, target):
        p = self.power(target)
        want = p >= AGGRO_SCORE
        if self.aggro_state:
            want = p >= AGGRO_SCORE - 0.5       # hysteresis
        # duel: a lone tracked enemy that isn't clearly stronger gets fought properly even when weak.
        # (Hunters know where we are, so hiding from a single one just means being shot while "hidden".)
        if (not want and DUEL and target is not None and target.get("kind") == "mech"
                and len(self.enemies) == 1 and self.hpf > (0.35 if self.duel_state else 0.55)
                and target.get("hp", 1400) <= self.hp * 1.35 + 200
                and target.get("cubes", 0) <= self.me.get("cubes", 0) + 3):
            self.duel_state = True
            return True
        self.duel_state = False
        if self.hpf < AGGRO_HP_MIN:
            want = False
        if want and target is not None and target.get("kind") == "mech":
            tc = target.get("cubes", 0)
            if tc > self.me.get("cubes", 0) + 4 and target.get("hp", 1400) > self.hp * 1.15:
                want = False
        n_en = sum(1 for e in self.enemies if e["_d"] < 120 and (e["_vis"] or e["_los"]))
        if want and n_en >= 3 and p < 2 and AGGRO_SCORE > 0:
            want = False
        if want != self.aggro_state:
            self.log("AGGRO %s (power %.1f)" % ("ON" if want else "off", p), True)
        self.aggro_state = want
        return want

    # ------------------------------------------------------------------ navigation
    def path_dir(self, goal, replan_s=2.5):
        w = self.world
        d = hyp(goal, self.pos)
        if d < 6:
            self.path = []
            return norm(goal[0] - self.pos[0], goal[1] - self.pos[1])
        if d < 60 and w.line_walkable(self.pos, goal, 2.0):
            self.path = []
            return norm(goal[0] - self.pos[0], goal[1] - self.pos[1])
        need = (not self.path or self.path_goal is None or hyp(self.path_goal, goal) > 10
                or self.t - self.path_t > replan_s)
        if need and self.time_left() > 0.02:
            p = w.astar(self.pos, goal)
            self.path_goal = goal
            self.path_t = self.t
            self.path = p or []
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

    # ------------------------------------------------------------------ dodging (walk + thruster candidates)
    def dodge(self, mx, my, speed_mult=1.0, allow_boost=True, lasers=()):
        """Pick the move (and thruster flag) that best avoids bullets, shells and laser lines.
        Returns (mx, my, boost)."""
        obs = self.obs
        me = self.me
        st = me["stats"]
        rad = st.get("radius", 2.5) * (1 + 0.05 * me.get("cubes", 0)) + 0.7
        walk = st["walk_speed"] * speed_mult
        bsp = st["boost_speed"]
        acc = st["ground_accel"] if me.get("grounded", True) else st["air_accel"]
        can_boost = allow_boost and me["energy"] > 14
        px, py, pz = self.pos
        bullets, shells = [], []
        for p in obs["projectiles"]:
            pp, vv = p["pos"], p["vel"]
            if p["kind"] == "mortar":
                g = self.cfg["weapons"]["mortar"]["gravity"]
                x, y, z = pp
                vx, vy, vz = vv
                tt = 0.0
                for _ in range(120):
                    tt += 0.05
                    x += vx * 0.05
                    y += vy * 0.05
                    vz -= g * 0.05
                    z += vz * 0.05
                    if z <= self.world.ground(x, y) + 0.5 or self.world.solid(x, y, z):
                        break
                if hyp((x, y), self.pos) < 40:
                    shells.append((x, y, tt))
            else:
                rx, ry, rz = pp[0] - px, pp[1] - py, pp[2] - (pz + 4.5)
                v2 = vv[0] ** 2 + vv[1] ** 2 + vv[2] ** 2
                if v2 < 1:
                    continue
                ts = -(rx * vv[0] + ry * vv[1] + rz * vv[2]) / v2
                if ts < -0.05 or ts > 1.5:
                    continue
                if math.hypot(rx + vv[0] * ts, ry + vv[1] * ts) > 14 or abs(rz + vv[2] * ts) > 10:
                    continue
                bullets.append((pp, vv, ts))
        if not bullets and not shells and not lasers:
            return mx, my, False
        bullets.sort(key=lambda b: b[2])
        bullets = bullets[:8]
        cands = [(mx, my, False)]
        if can_boost and (mx or my):
            cands.append((mx, my, True))
        for k in range(16):
            a = TAU * k / 16
            cands.append((math.cos(a), math.sin(a), False))
            if can_boost and k % 2 == 0:
                cands.append((math.cos(a), math.sin(a), True))
        cands.append((0.0, 0.0, False))
        dt, steps = 0.06, 11
        best, bestc = (mx, my, False), 1e18
        dev0 = math.atan2(my, mx) if (mx or my) else 0.0
        reserve = 40.0 if self.has("lightning") else 22.0
        for ci, (cx, cy, bst) in enumerate(cands):
            sp = bsp if bst else walk
            tvx, tvy = cx * sp, cy * sp
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
                for pp, vv, _ in bullets:
                    bx0, by0 = pp[0] + vv[0] * t0 - x0, pp[1] + vv[1] * t0 - y0
                    bx1, by1 = pp[0] + vv[0] * (t0 + dt) - x, pp[1] + vv[1] * (t0 + dt) - y
                    ex, ey = bx1 - bx0, by1 - by0
                    L2 = ex * ex + ey * ey
                    f = 0.0 if L2 < 1e-9 else max(0.0, min(1.0, -(bx0 * ex + by0 * ey) / L2))
                    dd = math.hypot(bx0 + ex * f, by0 + ey * f)
                    bz = pp[2] + vv[2] * (t0 + f * dt)
                    if dd < rad + 3.0 and -1.0 < bz - pz < 10.5:
                        cost += (100.0 + (steps - s)) if dd < rad else 4.0 * (rad + 3.0 - dd)
            for sx, sy, tt in shells:
                k = min(steps - 1, int(tt / dt))
                qx, qy = traj[k]
                if tt > steps * dt:
                    qx += vx * (tt - steps * dt)
                    qy += vy * (tt - steps * dt)
                dd = math.hypot(qx - sx, qy - sy)
                if dd < 9.5:
                    cost += 60.0 + (9.5 - dd) * 4
            for (ox, oy, ux, uy) in lasers:
                for k in (3, 6, 9):     # ~0.2 / 0.4 / 0.6 s ahead
                    qx, qy = traj[min(k, len(traj) - 1)]
                    lat = abs((qx - ox) * uy - (qy - oy) * ux)
                    if (qx - ox) * ux + (qy - oy) * uy > 0 and lat < rad + 1.5:
                        cost += 14.0 * (rad + 1.5 - lat) / (rad + 1.5)
            if cost == 0.0 and ci == 0:
                return mx, my, False
            if bst:
                cost += 2.5 + (4.0 if me["energy"] < reserve + 20 else 0.0)
            if (cx or cy) and (mx or my):
                cost += 2.0 * abs(adiff(math.atan2(cy, cx), dev0))
            if (cx or cy) and self.world.walk_blocked(px + cx * 5, py + cy * 5):
                cost += 40.0
            if not (cx or cy):
                cost += 3.0
            if cost < bestc:
                bestc, best = cost, (cx, cy, bst)
        return best

    # ------------------------------------------------------------------ aiming helpers
    def lead(self, tp, tv, speed, src):
        """Where to aim so the shot meets a constant-velocity target. The action only takes effect ~LAT s after the
        observation, so the target keeps moving (and we do too) before the shot leaves."""
        v = self.vel
        src = (src[0] + v[0] * AIM_LAT, src[1] + v[1] * AIM_LAT, src[2])
        t = math.dist(src, tp) / speed + AIM_LAT
        p = tp
        for _ in range(4):
            p = (tp[0] + tv[0] * t, tp[1] + tv[1] * t, tp[2] + tv[2] * t * 0.3)
            t = min(1.5, math.dist(src, p) / speed) + AIM_LAT
        return p, t

    def proj_aim(self, cy, cp, dt=0.1):
        """Where the torso will point after dt if we command (cy, cp): it slews at turn_rate / pitch_rate."""
        st = self.me["stats"]
        ry = st.get("turn_rate", 4.0) * dt
        rp = st.get("pitch_rate", 3.0) * dt
        y = self.aim[0] + max(-ry, min(ry, adiff(cy, self.aim[0])))
        p = self.aim[1] + max(-rp, min(rp, cp - self.aim[1]))
        return y, p

    def aim_at(self, p, src):
        dx, dy, dz = p[0] - src[0], p[1] - src[1], p[2] - src[2]
        h = math.hypot(dx, dy)
        st = self.me["stats"]
        pitch = max(st.get("min_pitch", -0.7), min(st.get("max_pitch", 1.3), math.atan2(dz, max(h, 0.01))))
        return math.atan2(dy, dx), pitch

    # ------------------------------------------------------------------ weapons
    def weapon_plan(self, target, mode):
        """Aim and trigger decisions for the current target. Works in every mode, so fleeing
        always shoots back and the torso stays facing the threat."""
        me = self.me
        weapons = me["weapons"]
        mz = self.muzzle()
        fire = [False, False]
        reload = False
        aim_yaw, aim_pitch = self.aim[0], self.aim[1]
        if target is None:
            return aim_yaw, aim_pitch, fire, reload
        tc = self.en_center(target)
        tv = target["vel"]
        d = target["_d"]
        gi, gw = self.weapon("gun")
        gspeed = gw["stats"]["speed"] if gw else 252.0
        lp, _ = self.lead(tc, tv, gspeed, mz)
        gun_yaw, gun_pitch = self.aim_at(lp, mz)
        aim_yaw, aim_pitch = gun_yaw, gun_pitch
        los_ok = target["_los"] and self.world.los(mz, tc)
        laser_charging = any(w and w["kind"] == "laser" and w.get("charge", 0) > 0 for w in weapons)
        laser_ok_mode = mode in ("peek", "cover", "brawl", "kite", "hunt", "perch", "flee", "drop") and self.hpf > 0.15
        aggro = self.aggro_state
        for i, w in enumerate(weapons):
            if not w:
                continue
            k = w["kind"]
            st = w.get("stats", {})
            if k == "laser":
                ly, lpit = self.aim_at((tc[0] + tv[0] * 0.15, tc[1] + tv[1] * 0.15, tc[2]), mz)
                py_, pp_ = self.proj_aim(ly, lpit)
                err = abs(adiff(ly, py_))
                perr = abs(lpit - pp_)
                tol = math.atan2(1.9, max(8.0, d))
                charge = w.get("charge", 0.0)
                full = st.get("charge", 0.75)
                hold_s = self.wcfg["laser"].get("hold_s", 2.0)
                rng = st.get("range", 280)
                ready = w["cooldown"] <= 0.1
                lightning_foe = "lightning" in (target.get("weapons") or []) and d < 70
                prep = (mode == "cover" and self.cs == "cover" and self.t >= self.next_peek - 1.0
                        and target.get("_d", 999) < rng - 15)
                if charge > 0:
                    aim_yaw, aim_pitch = ly, lpit
                    aligned = err < tol and perr < tol * 2.0
                    if los_ok and d < rng - 5:
                        if (charge >= full - 0.03 and aligned) or (self.force_release and aligned and charge > 0.25):
                            fire[i] = False          # release: beam goes out now
                        else:
                            fire[i] = charge < full + hold_s - 0.35
                    else:
                        fire[i] = charge < full + hold_s - 0.35   # keep it charged behind cover
                elif ready and laser_ok_mode and not lightning_foe and 22 < d < rng - 15 and (
                        (los_ok and err < 0.5) or prep):
                    fire[i] = True
                    aim_yaw, aim_pitch = ly, lpit
            elif k == "gun":
                py_, pp_ = self.proj_aim(gun_yaw, gun_pitch)
                err = abs(adiff(gun_yaw, py_))
                perr_g = abs(gun_pitch - pp_)
                tol = math.atan2(2.4, max(5.0, d)) + 0.01
                ptol = math.atan2(3.2, max(5.0, d)) + 0.015
                rng = min(st.get("range", 230) - 8, 215 if aggro else 190)
                ammo = w.get("ammo", 1)
                if los_ok and d < rng and err < tol and perr_g < ptol and ammo > 0 and w.get("reload", 0) <= 0:
                    if not (laser_charging and abs(adiff(aim_yaw, gun_yaw)) > tol):
                        fire[i] = True
                if ammo == 0 and w.get("reload", 0) == 0:
                    reload = True
            elif k == "lightning":
                ly, lpit = self.aim_at(tc, mz)
                if d < st.get("range", 45) - 2 and los_ok and me["energy"] > 6 and abs(adiff(ly, self.aim[0])) < 0.18:
                    fire[i] = True
                    if d < 40 and not laser_charging:
                        aim_yaw, aim_pitch = ly, lpit
        return aim_yaw, aim_pitch, fire, reload

    # ------------------------------------------------------------------ cover management
    def get_cover(self, thr, extra=(), force=False):
        t = self.t
        c = self.cov
        mid_peek = self.cs in ("peek", "retreat")
        if c is not None:
            fresh = (hyp(c["thr"], thr) < 16 and t - c["t"] < 6.0 and c["peeks"] < 5
                     and self.in_safe_zone(c["C"], 4))
            if fresh or (mid_peek and t - c["t"] < 9.0):
                return c
        if t < self.nocover_until or self.time_left() < 0.022:
            return c
        safe = lambda p: self.in_safe_zone(p, 6)
        plan = self.planner.plan(self.pos, thr, safe, budget_s=min(0.011, self.time_left() - 0.008),
                                 rng=self.rng, avoid=self.recent_cover, extra=extra)
        if plan is None and extra and self.time_left() > 0.02:
            plan = self.planner.plan(self.pos, thr, safe, budget_s=min(0.010, self.time_left() - 0.008),
                                     rng=self.rng, avoid=self.recent_cover)
        if plan is None:
            self.nocover_until = t + 1.2
            self.cov = None
            return None
        old = c
        self.cov = {"C": plan["C"], "P": plan["P"], "thr": (thr[0], thr[1]), "t": t, "peeks": 0}
        if old is not None:
            self.recent_cover.append(old["C"])
            self.recent_cover = self.recent_cover[-4:]
        return self.cov

    def peek_ready(self, target):
        """Is the loadout ready for a peek?  (mag, laser charge)"""
        _, gw = self.weapon("gun")
        _, lw = self.weapon("laser")
        t = self.t
        if gw and gw.get("reload", 0) > 0:
            return False
        if gw and gw.get("ammo", 12) < 3 and not lw:
            return False
        if lw:
            full = lw["stats"].get("charge", 0.75)
            pending = lw["cooldown"] <= 0.35 or lw.get("charge", 0) > 0
            if pending and lw.get("charge", 0) < full * 0.95 and t - self.cs_t < 1.6 and (
                    target is None or target["_d"] > 22):
                return False
        return True

    def peek_budget(self):
        if self.has("laser"):
            return PEEK_BUDGET["laser"]
        if self.has("lightning"):
            return PEEK_BUDGET["lightning"]
        return PEEK_BUDGET["gun"]

    def peek_cooldown(self):
        cd = 0.7 + self.rng.random() * 0.7
        if self.hpf < 0.6:
            cd += 2.0
        if self.hpf < 0.35:
            cd += 3.0
        _, lw = self.weapon("laser")
        if lw:
            cd = max(cd, lw["cooldown"] + 0.2)
        return cd

    def peek_move(self, target, thr_pos, exposed, extra=()):
        """The peek/hide state machine. Returns (move, boost, sneak, mode) or None if no cover exists."""
        t = self.t
        cov = self.get_cover(thr_pos, extra)
        if cov is None:
            return None
        C, P = cov["C"], cov["P"]
        dC = hyp(C, self.pos)
        if self.cs is None:
            self.cs = "retreat" if (exposed or dC > 3.0) else "cover"
            self.cs_t = t
            self.next_peek = t + (0.3 if self.cs == "cover" else 0.8)
            self.log("cover plan C=(%.0f,%.0f) P=%s d=%.0f" % (C[0], C[1], "yes" if P else "none", hyp(C, thr_pos)))
        cs = self.cs
        shot_at = bool(self.new_dmg) or (target is not None and target.get("charging") and target["_aimed"])
        boost = False
        sneak = False
        move = (0.0, 0.0)
        if cs == "cover":
            if dC > 1.8:
                move = self.path_dir(C)
                boost = shot_at and dC > 6 and self.me["energy"] > 30
                sneak = (not exposed) and (not shot_at) and dC < 12
            if exposed and dC <= 3.0 and self.hpf < 0.35 and t - self.reloc_t > 0.8:
                # nearly dead and seen: don't trade, find somewhere else to hide
                if self.cov is not None:
                    self.recent_cover.append(self.cov["C"])
                    self.recent_cover = self.recent_cover[-4:]
                self.cov, self.cs, self.reloc_t, self.nocover_until = None, None, t, 0.0
                self.log("seen at low hp -> relocate")
            elif exposed and dC <= 3.0 and target is not None and target["_d"] < MAX_ENGAGE and self.hpf >= 0.35:
                # they can see us here: shoot back for a bit, then replan
                self.cs, self.cs_t, self.expo, self.peek_hp0, self.peek_t0 = "peek", t, 0.0, self.hp, t
                self.cov["peeks"] += 1
                self.log("cover compromised -> fight back")
            elif (target is not None and target["_d"] < MAX_ENGAGE and t >= self.next_peek
                  and self.hpf >= 0.35 and self.peek_ready(target)):
                self.cs, self.cs_t, self.expo, self.peek_hp0, self.peek_t0 = "peek", t, 0.0, self.hp, t
                self.cov["peeks"] += 1
                self.peek_count += 1
                self.force_release = False
                self.log("PEEK #%d d=%.0f" % (self.peek_count, target["_d"]))
        elif cs == "peek":
            if exposed:
                self.expo += self.dt
            lost = max(0.0, self.peek_hp0 - self.hp)
            laser_in = target is not None and target.get("charging") and target["_aimed"] and exposed
            _, lw = self.weapon("laser")
            just_fired = lw is not None and lw["cooldown"] > 1.0 and lw.get("charge", 0) == 0
            _, gw = self.weapon("gun")
            gun_dry = gw is not None and gw.get("ammo", 1) <= 0 and not lw
            leave = (self.expo >= self.peek_budget() or lost >= PEEK_DMG_CAP * self.maxhp or laser_in
                     or t - self.peek_t0 > 1.9 or self.hpf < 0.3 or gun_dry
                     or (just_fired and self.expo > 0.15) or target is None)
            if leave:
                self.cs, self.cs_t = "retreat", t
                self.force_release = True
                self.log("retreat (expo %.2f lost %d%s)" % (self.expo, lost, " LASER" if laser_in else ""))
            else:
                tgt = P
                if tgt is None and target is not None:
                    # no known peek spot: sidestep perpendicular to the threat
                    ux, uy = norm(target["pos"][0] - self.pos[0], target["pos"][1] - self.pos[1])
                    tgt = (C[0] - uy * 4.0, C[1] + ux * 4.0)
                if tgt is not None and hyp(tgt, self.pos) > 1.2:
                    move = norm(tgt[0] - self.pos[0], tgt[1] - self.pos[1])
                    boost = (self.me["energy"] > 55 and target is not None and target["_aimed"]
                             and hyp(tgt, self.pos) > 4)
        if self.cs == "retreat":
            if dC <= 1.8 or (not exposed and dC <= 4.0 and t - self.cs_t > 0.4):
                self.cs, self.cs_t = "cover", t
                self.next_peek = t + self.peek_cooldown()
                self.force_release = False
                _, gw = self.weapon("gun")
                if gw and gw.get("ammo", 12) <= 4 and gw.get("reload", 0) <= 0:
                    self._want_reload = True
            else:
                move = self.path_dir(C) if dC > 6 else norm(C[0] - self.pos[0], C[1] - self.pos[1])
                boost = (shot_at or exposed) and self.me["energy"] > 28 and dC > 3
            if t - self.cs_t > 4.0:       # couldn't get back: pick a new spot
                self.cov = None
                self.cs = None
        return move, boost, sneak, self.cs or "cover"

    # ------------------------------------------------------------------ open-field fighting
    def brawl_move(self, target, aggressive):
        """Strafe, kick with thrusters, hold the preferred range. Used for open-field fights and for
        the late-game 'just beam them' mode."""
        t = self.t
        d = target["_d"]
        ux, uy = norm(target["pos"][0] - self.pos[0], target["pos"][1] - self.pos[1])
        tw = target.get("weapons") or []
        if self.has("lightning") and "laser" in tw:
            pref = 24.0
        elif self.has("lightning"):
            pref = 30.0
        elif self.has("laser"):
            pref = 110.0
        else:
            pref = 85.0
        if "lightning" in tw and not self.has("lightning"):
            pref = max(pref, 75.0)       # stay out of the arc
        if aggressive:
            pref *= 0.6 if not self.has("laser") else 0.8
        rad = 1.0 if d > pref + 10 else (-1.0 if d < pref - 18 else 0.0)
        flip_fast = target["_aimed"] or target.get("charging")
        if t > self.strafe_until:
            if self.rng.random() < 0.65:
                self.strafe = -self.strafe
            self.strafe_until = t + ((0.22 + self.rng.random() * 0.4) if flip_fast
                                     else (0.45 + self.rng.random() * 0.8))
            if (self.me["energy"] > (60 if self.has("lightning") else KICK_ENERGY) and t >= self.kick_ready
                    and (flip_fast or d < 90)):
                self.kick_until = t + 0.22
                self.kick_ready = t + 0.8
        tx, ty = -uy * self.strafe, ux * self.strafe
        move = norm(ux * rad * (1.0 if aggressive else 0.8) + tx, uy * rad * (1.0 if aggressive else 0.8) + ty)
        boost = t < self.kick_until or (aggressive and rad > 0 and d > 150 and self.me["energy"] > 85)
        return move, boost

    # ------------------------------------------------------------------ rooftops
    def roof_end(self, why, cd=25.0):
        self.log("ROOF end: %s" % why, True)
        self.roof = None
        self.roof_cd = self.t + cd

    def roof_step(self, target, zurgent, vis_en):
        """Returns (move, boost, mode) while a roof plan is active/started, else None."""
        t, me = self.t, self.me
        r = self.roof
        thr = target["pos"] if target is not None else None
        if r is None:
            if not ROOF_ENABLED:
                return None
            dur = float(self.cfg["match"].get("duration_s", 480))
            if (t > 0.30 * dur or me.get("cubes", 0) >= 8):
                return None                  # rooftops are an early-game tool only
            if (target is None or not target["_vis"] or zurgent or t < self.roof_cd or self.hpf < 0.6
                    or not (45 < target["_d"] < 230) or len([e for e in vis_en if e["_d"] < 150]) > 2
                    or me["energy"] < 60 or not me.get("grounded", True) or not self.rp.sites):
                return None
            ch = self.rp.choose(self.pos, thr, 45.0, me["energy"], lambda p: self.in_safe_zone(p, 12))
            if ch is None:
                self.roof_cd = t + 4.0
                return None
            site, B, E = ch
            self.roof = r = {"site": site, "B": B, "E": E, "ph": "approach", "t0": t, "tp": t, "drop_dir": None,
                              "last_thr": thr, "seen": t}
            self.log("ROOF start top=%.0f rise=%.0f walk=%.0f" % (site.top, site.rise, hyp(E, self.pos)), True)
        site = r["site"]
        if t - r["t0"] > (14.0 if r["ph"] in ("approach", "climb", "mount") else 40.0) and r["ph"] != "drop":
            if r["ph"] == "perch":
                r["ph"] = "drop"
            else:
                self.roof_end("timeout in %s" % r["ph"], 40.0)
                return None
        if r["ph"] != "drop" and (zurgent or self.hpf < 0.35):
            if r["ph"] == "perch":
                r["ph"] = "drop"
            else:
                self.roof_end("abort low hp/zone", 20.0)
                return None
        if r["ph"] in ("approach", "climb", "mount") and sum(x for tt, x in self._dmg_log if t - tt < 3.0) > 0.25 * self.maxhp:
            self.roof_end("shot up on the way", 30.0)
            return None
        ph = r["ph"]
        pz = self.pos[2]
        self._roof_jump = False
        if ph == "approach":
            if hyp(r["E"], self.pos) < 4.5:
                r["ph"] = ph = "climb"
            else:
                return self.path_dir(r["E"]), me["energy"] > 90, "roofgo"
        if ph == "climb":
            bx, by = r["B"]
            mv = norm(bx - self.pos[0], by - self.pos[1])
            if pz >= site.top - 0.3:
                r["ph"] = ph = "mount"
            else:
                if me["energy"] < 4 and not me.get("grounded", True):
                    self.roof_end("out of energy", 30.0)
                    return None
                self._roof_jump = bool(me.get("grounded", True))
                return mv, True, "climb"
        if ph == "mount":
            if pz < site.top - 1.5:
                r["ph"] = "climb"
                return norm(r["B"][0] - self.pos[0], r["B"][1] - self.pos[1]), True, "climb"
            if site.inside(self.pos[0], self.pos[1], 1.5) and me.get("grounded", True):
                r["ph"] = ph = "perch"
                r["tp"] = t
                self.log("ROOF perch", True)
            else:
                mv = norm(site.cx - self.pos[0], site.cy - self.pos[1])
                return mv, pz < site.top + 0.5 and me["energy"] > 3, "climb"
        if ph == "perch":
            hurt = sum(a for tt, a in self._dmg_log if t - tt < 2.5)
            if target is not None:
                r["last_thr"] = target["pos"]
                r["seen"] = t
            dur = float(self.cfg["match"].get("duration_s", 480))
            if (self.hpf < 0.5 or hurt > 0.2 * self.maxhp or t - r.get("seen", t) > 7.0
                    or pz < site.top - 14.0 or t > 0.38 * dur or me.get("cubes", 0) >= 10):
                r["ph"] = ph = "drop"
                lt = r.get("last_thr") or thr
                r["drop_dir"] = norm(self.pos[0] - lt[0], self.pos[1] - lt[1]) if lt else (1.0, 0.0)
                r["t_drop"] = t
                self.log("ROOF drop (hp %.0f%% hurt %.0f)" % (self.hpf * 100, hurt), True)
            else:
                # stand at the edge FAR from the threat (the building shields us), strafe along it, and hop up on the
                # thrusters just enough to see over the roof line when there is no line of sight.
                lt = r.get("last_thr") or thr
                if t - r.get("spot_t", -9) > 1.5 or r.get("spot") is None:
                    best, bd = None, -1.0
                    for bx, by in site.boundary_points(5.0):
                        ux, uy = site.cx - bx, site.cy - by
                        L = math.hypot(ux, uy) or 1.0
                        qx, qy = bx + ux / L * 4.0, by + uy / L * 4.0
                        if site.inside(qx, qy, 2.5):
                            dd_ = hyp((qx, qy), lt)
                            if dd_ > bd:
                                best, bd = (qx, qy), dd_
                    r["spot"] = best or (site.cx, site.cy)
                    r["spot_t"] = t
                spot = r["spot"]
                dd = hyp(spot, self.pos)
                if dd > 4.0:
                    return norm(spot[0] - self.pos[0], spot[1] - self.pos[1]), False, "perch"
                ux, uy = norm(lt[0] - self.pos[0], lt[1] - self.pos[1])
                if t > self.strafe_until:
                    self.strafe = -self.strafe
                    self.strafe_until = t + 0.5 + self.rng.random() * 0.8
                mv = (-uy * self.strafe, ux * self.strafe)
                if not site.inside(self.pos[0] + mv[0] * 4.0, self.pos[1] + mv[1] * 4.0, 1.8):
                    self.strafe = -self.strafe
                    mv = (-mv[0], -mv[1])
                tz = lt[2] + 4.5 if len(lt) > 2 else site.top - 5.0
                has_los = self.world.los(self.muzzle(), (lt[0], lt[1], tz))
                hop = (not has_los) and me["energy"] > 25 and pz < site.top + 12.0
                if hop:
                    self._roof_jump = bool(me.get("grounded", True))
                return mv, hop, "perch"
        if ph == "drop":
            dd = r["drop_dir"] or (1.0, 0.0)
            if (pz < site.top - 3.0 and me.get("grounded", True)) or t - r.get("t_drop", t) > 8.0:
                self.roof_end("dropped", 25.0)
                return None
            return dd, False, "drop"
        return None

    # ------------------------------------------------------------------ main
    def act(self, obs):
        self.t0 = time.perf_counter()
        self.ingest(obs)
        me = self.me
        t = self.t
        act = {"tick": obs["tick"]}
        if obs.get("offer"):
            best, bi = 99, 0
            rank = LASER_RANK + UPGRADE_RANK if self.has("laser") else UPGRADE_RANK
            for i, ch in enumerate(obs["offer"]["choices"]):
                r = rank.index(ch["id"]) if ch["id"] in rank else 50
                if r < best:
                    best, bi = r, i
            act["pick"] = bi
        weapons = me["weapons"]
        wk = self.my_weapons()
        recent_dmg = t - self.last_dmg_t < 1.5

        vis_en = [e for e in self.enemies if e["_vis"] and e["_d"] < 250]
        mem_en = [e for e in self.enemies if not e["_vis"] and e["_d"] < 230
                  and t - self.enemy_seen.get(e["id"], -99) < 5.0]
        target = None
        if vis_en:
            # focus fire: stick with the current victim (damage spread over several mechs just regenerates)
            target = min(vis_en, key=lambda e: e.get("hp", 1400) * (0.6 + e["_d"] / 250.0)
                         * (0.55 if e["id"] == self.focus_id else 1.0))
            self.focus_id = target["id"]
        elif mem_en:
            target = mem_en[0]
        # how exposed are we: can the nearest threat see us?
        exposed = any(e["_los"] and e["_d"] < 240 for e in vis_en + mem_en)
        pseudo = None
        if target is None and self.alert and self.alert[1] > t and (recent_dmg or self.alert[2] == "near"):
            b = self.alert[0]
            far = 55.0
            pseudo = (self.pos[0] + far * math.cos(b), self.pos[1] + far * math.sin(b), self.pos[2])

        known = {e["id"] for e in vis_en + mem_en}
        for ev in self.new_dmg:
            if ev.get("by") not in known and ev.get("bearing") is not None and ev.get("weapon") != "zone":
                b = ev["bearing"]
                self.ghosts.append((self.pos[0] + 70 * math.cos(b), self.pos[1] + 70 * math.sin(b), self.pos[2], t + 3.0))
        self.ghosts = [g for g in self.ghosts if g[3] > t][-3:]
        aggro = self.is_aggro(target)
        zgoal, zurgent = self.zone_goal()
        if self.aggro_state:
            for s in self.heard:
                dist = 35.0 if s["dist"] == "near" else 100.0
                self.hunt = (self.pos[0] + dist * math.cos(s["bearing"]),
                             self.pos[1] + dist * math.sin(s["bearing"]), t + 6.0)

        move, boost, sneak = (0.0, 0.0), False, False
        mode = "idle"
        speed_mult_override = None
        lasers = []
        for e in vis_en + mem_en:
            if e.get("charging") and e["_aimed"] and e["_d"] < 280:
                ox, oy = e["pos"][0], e["pos"][1]
                ux, uy = math.cos(e["aim"][0]), math.sin(e["aim"][0])
                lasers.append((ox, oy, ux, uy))

        engaged = target is not None or pseudo is not None
        try:
            handled = False
            if self.roof is not None or engaged:
                rr = self.roof_step(target, zurgent, vis_en)
                if rr is not None:
                    move, boost, mode = rr
                    handled = True
            if engaged and not handled:
                thr_pos = target["pos"] if target is not None else pseudo
                tooclose = target is not None and target["_d"] < 38
                if zurgent:
                    mode = "zone"
                    if zgoal is not None:
                        move = self.path_dir(zgoal)
                        boost = me["energy"] > 10
                    handled = True
                elif aggro and target is not None and target["_vis"]:
                    mode = "brawl"
                    move, boost = self.brawl_move(target, True)
                    handled = True
                elif aggro and target is not None:
                    mode = "hunt"             # lost sight: chase its tracked position
                    move = self.path_dir((target["pos"][0], target["pos"][1]))
                    boost = me["energy"] > 70 and target["_d"] > 60
                    handled = True
                elif target is None and aggro and self.hunt and self.hunt[2] > t:
                    handled = False           # fall through to the hunt logic below
                else:
                    if self.cs == "cover" and self.new_dmg and t - self.reloc_t > 1.2 and t - self.cs_t > 0.3:
                        # shot while "hidden": this cover does not work against whoever is shooting
                        if self.cov is not None:
                            self.recent_cover.append(self.cov["C"])
                            self.recent_cover = self.recent_cover[-4:]
                        self.cov, self.cs, self.reloc_t, self.nocover_until = None, None, t, 0.0
                        self.log("hit while hidden -> relocate")
                    extra = [e["pos"] for e in (vis_en + mem_en) if e is not target][:2]
                    extra += [(g[0], g[1], g[2]) for g in self.ghosts if g[3] > t][:2]
                    res = self.peek_move(target, thr_pos, exposed, extra)
                    if res is not None and (target is not None or pseudo is not None):
                        move, boost, sneak, mode = res
                        handled = True
                    elif target is not None:
                        # nowhere to hide: fight in the open, or run (facing and shooting) when losing
                        if self.hpf < 0.35 and target["_d"] > 25:
                            mode = "flee"
                            ax, ay = self.pos[0] - target["pos"][0], self.pos[1] - target["pos"][1]
                            move = norm(ax, ay)
                            if zgoal is not None:
                                zx, zy = norm(zgoal[0] - self.pos[0], zgoal[1] - self.pos[1])
                                move = norm(move[0] + zx * 0.7, move[1] + zy * 0.7)
                            sx, sy = -move[1] * self.strafe, move[0] * self.strafe
                            move = norm(move[0] + sx * 0.5, move[1] + sy * 0.5)
                            if t > self.strafe_until:
                                self.strafe = -self.strafe
                                self.strafe_until = t + 0.3 + self.rng.random() * 0.4
                            boost = me["energy"] > 30
                        else:
                            mode = "kite"
                            move, boost = self.brawl_move(target, False)
                        handled = True
            if not handled and aggro and self.hunt and self.hunt[2] > t and self.hpf > 0.55:
                if hyp(self.hunt, self.pos) < 14:
                    self.hunt = None
                else:
                    mode = "hunt"
                    move = self.path_dir((self.hunt[0], self.hunt[1]))
                    handled = True
            if not handled:
                self.cs = None
                self.cov = None
                move, mode_s, boost = self.idle_plan(zgoal, zurgent, vis_en, wk)
                mode = mode_s
        except Exception:
            import traceback
            mf.log("plan error: " + traceback.format_exc())
            move, mode = (0.0, 0.0), "error"

        # ---- weapons / aim
        weapon_target = target
        if weapon_target is None and self._aux_target is None and pseudo is not None:
            weapon_target = None
        aim_yaw, aim_pitch, fire, reload = self.weapon_plan(weapon_target, mode)
        if weapon_target is None:
            ct = self._aux_target
            if ct is not None:
                c, kind = ct
                cc = (c["pos"][0], c["pos"][1], c["pos"][2] + (1.2 if kind == "crate" else 1.7))
                mz = self.muzzle()
                ay_, ap_ = self.aim_at(cc, mz)
                aim_yaw, aim_pitch = ay_, ap_
                gi, gw = self.weapon("gun")
                if gw and self.world.los(mz, cc):
                    if abs(adiff(ay_, self.aim[0])) < 0.06 and math.dist(cc, mz) < 150 and gw.get("ammo", 1) > 0:
                        fire[gi] = True
            elif self.alert is not None and self.alert[1] > t:
                aim_yaw, aim_pitch = self.alert[0], 0.0
            elif mode in ("hunt",) and self.hunt:
                aim_yaw, aim_pitch = math.atan2(self.hunt[1] - self.pos[1], self.hunt[0] - self.pos[0]), 0.0
            elif (move[0] or move[1]) and mode != "hold":
                aim_yaw, aim_pitch = math.atan2(move[1], move[0]), 0.0
            else:
                self.spin = (self.spin + 0.33) % TAU
                aim_yaw, aim_pitch = adiff(self.aim[0] + 0.33, 0.0), 0.0
        self._aux_target = None
        # being shot in the back (x1.5): turn to whoever is doing it, unless we're already facing them
        if t - self.rear_t < 1.0 and not any(fire):
            tdir = None if target is None else math.atan2(target["pos"][1] - self.pos[1], target["pos"][0] - self.pos[0])
            if tdir is None or not target["_vis"] or abs(adiff(tdir, self.rear_bearing)) > 1.2:
                aim_yaw, aim_pitch = self.rear_bearing, 0.0
        # reload when safe
        gi, gw = self.weapon("gun")
        if gw and gw.get("reload", 0) <= 0:
            if getattr(self, "_want_reload", False) or (gw.get("ammo", 12) < 5 and (mode in ("cover", "idle", "hold", "heal") or not recent_dmg)):
                reload = True
        self._want_reload = False

        # ---- dodge (walk or thruster burst)
        charging = any(w and w["kind"] == "laser" and w.get("charge", 0) > 0 for w in weapons)
        smult = me["stats"].get("charge_move_mult", 0.5) if charging else 1.0
        if mode not in ("error", "climb", "roofgo_x") and (move[0] or move[1] or obs["projectiles"] or lasers) and self.time_left() > 0.012:
            mx, my, dboost = self.dodge(move[0], move[1], smult, allow_boost=not charging and mode != "perch", lasers=lasers)
            if mode == "perch" and self.roof is not None and not self.roof["site"].inside(self.pos[0] + mx * 5.0, self.pos[1] + my * 5.0, 1.5):
                mx, my, dboost = move[0], move[1], False
            if dboost:
                boost = True
            move = (mx, my)
        if charging and mode not in ("climb", "perch"):
            boost = False
        # stuck handling (not while deliberately holding still)
        if t < self.stuck_until:
            move = self.stuck_dir
            act["jump"] = True
            boost = me["energy"] > 20
        elif (move[0] or move[1]) and len(self.pos_hist) >= 8 and t - self.pos_hist[0][0] > 0.9 and mode not in ("peek", "climb", "perch", "drop"):
            h = self.pos_hist[0]
            if math.hypot(self.pos[0] - h[1], self.pos[1] - h[2]) < 1.5 and not any(fire):
                a = random.random() * TAU
                self.stuck_dir = (math.cos(a), math.sin(a))
                self.stuck_until = t + 0.8
                self.path = []
        tk = self.pickup_action(wk)
        if tk:
            act.update(tk)
        if not me.get("in_zone", True):
            boost = me["energy"] > 10
        if any(fire[i] and weapons[i] and weapons[i]["kind"] == "lightning" for i in range(2)) and me["energy"] < 35:
            boost = False
        if boost and me["energy"] < 6:
            boost = False
        sp = 0.42 if sneak else 1.0
        if self._roof_jump:
            act["jump"] = True
        act["move"] = [round(move[0] * sp, 3), round(move[1] * sp, 3)]
        act["boost"] = bool(boost)
        act["aim"] = [round(aim_yaw, 4), round(aim_pitch, 4)]
        act["fire"] = fire
        if reload:
            act["reload"] = True
        self.mode = mode
        if t >= self.next_beat:
            self.next_beat = t + 15.0
            self.log("beat mode=%s cs=%s alive=%s power=%.1f aggro=%s peeks=%d dealt=%d taken=%d weapons=%s" % (
                mode, self.cs, obs.get("alive"), self.power(), self.aggro_state, self.peek_count,
                self.stats_dmg_out, self.stats_dmg_in, ",".join(wk)), True)
        if mode != self.last_mode:
            self.log("mode %s -> %s%s" % (self.last_mode, mode, " (cs=%s)" % self.cs if self.cs else ""))
            self.last_mode = mode
        self.debug_draw(mode, target, pseudo, fire, charging)
        return act

    # ------------------------------------------------------------------ debug drawing (recorded for your team)
    def debug_draw(self, mode, target, pseudo, fire, charging):
        d = mf.draw
        me = self.me
        tag = {"cover": "COVER", "peek": "PEEK", "retreat": "RETREAT"}.get(mode, mode.upper())
        d.label("%s%s hp%d e%d c%d %s" % (tag, "*" if self.aggro_state else "", self.hp, me["energy"],
                                           me.get("cubes", 0), ",".join(k[0] for k in self.my_weapons())))
        z0 = self.pos[2] + 4.5
        col = {"peek": "#ff4040", "retreat": "#ffa040", "cover": "#4080ff", "brawl": "#ff2020",
               "kite": "#ff8080", "flee": "#ffb000", "hunt": "#c040ff"}.get(mode, "#a0a0a0")
        if target is not None:
            tc = self.en_center(target)
            d.line((self.pos[0], self.pos[1], z0), tc, col if target["_los"] else "#606060")
            d.text("%s d%d hp%d%s" % (target["id"], target["_d"], target.get("hp", 0),
                                      " CHG" if target.get("charging") else ""), tc, "#ffffff")
        elif pseudo is not None:
            d.point(pseudo, "#ffff00", 5)
        if self.cov is not None and self.cs:
            C, P = self.cov["C"], self.cov["P"]
            gC = self.world.ground(C[0], C[1])
            d.point((C[0], C[1], gC + 1), "#3070ff", 8)
            d.text("C", (C[0], C[1], gC + 3), "#3070ff")
            if P is not None:
                gP = self.world.ground(P[0], P[1])
                d.point((P[0], P[1], gP + 1), "#30ff70", 8)
                d.line((C[0], C[1], gC + 1), (P[0], P[1], gP + 1), "#30ff70")
        for p in self.obs["projectiles"][:12]:
            v = p["vel"]
            d.line(p["pos"], (p["pos"][0] + v[0] * 0.25, p["pos"][1] + v[1] * 0.25, p["pos"][2] + v[2] * 0.25), "#ff9020")
        if self.hunt and self.hunt[2] > self.t:
            d.point((self.hunt[0], self.hunt[1], self.pos[2] + 2), "#c040ff", 6)
        if any(fire):
            d.point((self.pos[0], self.pos[1], self.pos[2] + 10), "#ffffff", 3)

    # ------------------------------------------------------------------ stronghold search (incremental)
    def update_stronghold(self):
        z = self.obs["zone"]
        nc, nr = z["next_center"], z["next_radius"]
        if nr <= 45:
            return None
        key = (round(nc[0] / 5), round(nc[1] / 5), round(nr))
        w = self.world
        if key != self.sh_key:
            self.sh_key = key
            self.sh_best = None
            self.sh_i = 0
            self.sh_cands = []
            for _ in range(180):
                a = self.rng.random() * TAU
                r = (self.rng.random() ** 0.5) * 0.55 * nr
                self.sh_cands.append((nc[0] + r * math.cos(a), nc[1] + r * math.sin(a)))
        n = 0
        while self.sh_i < len(self.sh_cands) and n < 14 and self.time_left() > 0.02:
            x, y = self.sh_cands[self.sh_i]
            self.sh_i += 1
            n += 1
            if w.walk_blocked(x, y):
                continue
            pi, pj = w.pcell(x, y)
            c = pj * w.pnx + pi
            if w.pb[c] or not w.pnear[c]:
                continue
            cnt = 0
            for dj in range(-4, 5):
                for di in range(-4, 5):
                    a, b = pi + di, pj + dj
                    if 0 <= a < w.pnx and 0 <= b < w.pny and w.pb[b * w.pnx + a]:
                        cnt += 1
            if self.sh_best is None or cnt > self.sh_best[0]:
                self.sh_best = (cnt, x, y)
        return (self.sh_best[1], self.sh_best[2]) if self.sh_best else None

    # ------------------------------------------------------------------ idle planning
    def idle_plan(self, zgoal, zurgent, vis_en, wk):
        t = self.t
        me = self.me
        for d in self.obs.get("drops", []):
            if d["lands_in"] <= 4 and self.hpf > 0.5:
                dd = hyp(d["pos"], self.pos)
                busy = any(hyp(e["pos"], d["pos"]) < d["radius"] + 30 for e in self.enemies if e["_vis"])
                if dd < 160 and not busy and (d.get("holder") in (None, self.slot)):
                    if dd > d["radius"] - 3:
                        return self.path_dir((d["pos"][0], d["pos"][1])), "drop", me["energy"] > 50 and dd > 40
                    return (0.0, 0.0), "hold", False
        if zgoal is not None and (zurgent or t - self.last_dmg_t > 0.3):
            return self.path_dir(zgoal), "zone", zurgent and me["energy"] > 10
        if self.hpf < 0.9 and t - self.last_dmg_t < 25:
            return (0.0, 0.0), "heal", False
        want, bestv = None, 0.0
        have = set(wk)
        for pid, (kind, pp) in self.known_pickups.items():
            if self.bad_pickups.get(pid, 0) > t:
                continue
            v = 0.0
            if kind == "laser" and "laser" not in have:
                v = 12.0                     # the laser is by far the best weapon: take it even over lightning/mortar
            elif me["weapons"][1] is None:
                if kind == "lightning" and "lightning" not in have:
                    v = 2.6
            if v <= 0:
                continue
            d = hyp(pp, self.pos)
            if d > (300 if kind == "laser" else 260) or not self.in_safe_zone(pp, 15) or abs(pp[2] - self.pos[2]) > 14:
                continue
            if self.contested(pp, 0.55 if kind == "laser" else 0.7):
                continue
            sc = v / (d + 40.0)
            if sc > bestv:
                bestv, want = sc, pp
        if want is not None:
            return self.path_dir((want[0], want[1])), "loot", False
        if self.hpf > 0.7:
            for c in self.obs["cubes"]:
                if (hyp(c["pos"], self.pos) < 90 and self.in_safe_zone(c["pos"], 10) and abs(c["pos"][2] - self.pos[2]) < 8
                        and not self.contested(c["pos"])):
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
        # nothing to do: sit in a cover-rich spot near where the zone is going (peek-fight from there)
        sh = self.update_stronghold()
        if sh is not None and hyp(sh, self.pos) > 12 and self.hpf > 0.5 and self.in_safe_zone(sh, 8):
            return self.path_dir(sh), "stronghold", False
        return (0.0, 0.0), "hold", False

    # ------------------------------------------------------------------ pickups
    def pickup_action(self, wk):
        me = self.me
        have = set(wk)
        full = me["weapons"][1] is not None
        if full and "laser" in have:
            return None
        charging = any(w and w["kind"] == "laser" and w.get("charge", 0) > 0 for w in me["weapons"])
        best = None
        for pid, (kind, pp) in self.known_pickups.items():
            if self.bad_pickups.get(pid, 0) > self.t:
                continue
            if kind in have or kind in ("mortar", "gun"):
                continue
            if full and kind != "laser":
                continue
            if hyp(pp, self.pos) < 3.6 and abs(pp[2] - self.pos[2]) < 3.5:
                pr = 2 if kind == "laser" else 1
                if best is None or pr > best[0]:
                    best = (pr, pid)
        if best:
            r = {"take": best[1]}
            if full:
                r["drop"] = 1
            return r
        return None


if __name__ == "__main__":
    mf.run(Bot)
