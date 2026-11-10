"""The turn harness: ``TurnHarness`` orchestrates a turn's stages (#3819).

``turn`` holds the orchestrator and its request; ``ports`` holds the stage
ports that need no runner state. ``TurnRunner`` implements the other ports
itself (the stages are built with ``port=self``).
"""
