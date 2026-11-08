"""The turn harness: ``TurnHarness`` orchestrates a turn's stages (#3819).

``turn`` holds the orchestrator and its request; ``ports`` holds the stage
port implementations (``TurnRunnerPorts``, which ``TurnRunner`` mixes in,
plus the few ports that need no runner state).
"""
