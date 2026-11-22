# mechfight bot v1 ("peeker")
Python bot for the Mech Royale track (protocol mechfight/1). Standard library only.

- `bot.py`: decision logic: cover/peek state machine, thruster dodging, power-scaled aggression, loot, zone, drops
- `cover.py`: finds a hidden spot **C** plus the nearest spot **P** that sees the threat (the peek)
- `world.py`: map model (building raster, LOS, A*) — unchanged from v0
- `mechfight.py`: kit wrapper; `arena/`: bundled map fallback
- `tools/minisim.py`: local simulator + HTML replay (not the real server, see below)

## Behaviour
**Weak (early game): fight from cover.** `cover -> peek -> retreat -> cover`.
Hidden at C the torso stays pre-aimed at the enemy's tracked position (the server keeps real
positions for 5 s after they leave view), the gun reloads, a laser charges. At the peek time it steps to P,
shoots until the exposure budget / damage cap / enemy laser telegraph says go, and goes back. Cover must
hide the body edges from *every* tracked threat; being shot while "hidden" triggers a relocation.

**Strong (late game): beam them.** `power() = cubes + 2 (second weapon) + time bonus` crosses `AGGRO_SCORE`
-> the bot stops peeking, closes to its weapon's preferred range with strafing, and *hunts* hostile
sounds (near/mid gun, laser, lightning, mortar) by walking toward the bearing. Drops back to cover below 40% HP,
or if the enemy has far more cubes+HP, or when 3+ enemies are close.

**Dodging.** Predictive dodge over walk *and* boost candidates (bullets, mortar shells, charging-laser
lines). Boost is a short burst (19.5 vs 11.7 m/s, ~5 energy per 0.3 s). Strafe direction flips at random
intervals, faster when aimed at; the flip is often "kicked" with the thruster so a leading shooter misses.

**Flee while shooting.** The torso always tracks the primary target, in every mode, and the trigger logic
runs independently of movement, so retreating faces the enemy (also avoids the x1.5 rear damage). Taking
rear damage from an unseen shooter turns the torso toward it.

**Opening.** loot a laser (then lightning) -> take cubes/crates/NPCs near by -> walk to a *stronghold*
(cover-dense spot in the next zone circle; searched incrementally) -> hold silently (walking under 5 m/s makes
no sound) and peek-fight whatever comes. Supply drops are taken when nobody is near. Zone logic is v0's.

## Seeing what the bot did
1. **stderr** is shown to your team after each match: `HIT`/`KILL`/`AGGRO`/`beat` lines (15 s heartbeat) and
   mode changes with cover plans. Last `HIT` lines = how you died.
2. **Debug drawing** is recorded for your team: label (mode, hp, energy, cubes, weapons), line to target
   (grey = no LOS), blue **C**, green **P**, orange incoming bullets, purple hunt point.
3. **Local replay:** `python3 tools/minisim.py --seed 3 --html /tmp/replay.html` and open the file.

## Tuning knobs (top of bot.py)
`AGGRO_SCORE`, `AGGRO_HP_MIN`, `PEEK_BUDGET`, `PEEK_DMG_CAP`, `MAX_ENGAGE`, `KICK_ENERGY`, `UPGRADE_RANK`.

## Caveat
`minisim.py` has simplified physics, vision and sound and scripted opponents. It catches crashes,
timing and logic bugs; it cannot tell you win rates against the real built-in bots.
