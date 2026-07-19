"""Pilot router — self-trained local router (MiniLM backbone).

Rev 4 (MiniLM-locked) of the Pilot router. This package hosts the bounded
feature builder, the model loader, the safety-net post-processing, and the
``PilotStrategy`` that assembles them into a ``RouterStrategy``. It deliberately
ships nothing OpenSquilla-derived.
"""

from agentos.agentos_router.pilot.strategy import PilotStrategy

__all__ = ["PilotStrategy"]
