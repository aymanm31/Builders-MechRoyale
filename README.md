# mechfight bot (survivor-v0)
Python bot for the Mech Royale track (protocol mechfight/1). Standard library only.
- `bot.py`: decision logic (dodging, fight/flee, hide-and-heal, loot, zone, drops)
- `world.py`: map model (building raster, LOS, A* pathfinding)
- `mechfight.py`: kit wrapper; `arena/`: bundled map fallback (the match also mounts it at /arena)
