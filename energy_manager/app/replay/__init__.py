"""Replay harness (stage 2): episode extraction and the plant.

The package implements ``docs/REPLAY_HARNESS.md``: :mod:`~app.replay.episode` freezes one recording
window into a writable scratch store without ever letting the replay read the future, and
:mod:`~app.replay.plant` serves that engine's batteries in ``recorded`` or ``simulated`` mode.
"""
