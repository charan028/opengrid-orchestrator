"""opengrid.core -- pure functions, no I/O. Single implementation of shared formulas (BUILD.md S1, 02b S12).

Every other opengrid package imports from here rather than re-deriving physics, limits, product
rounding, crypto or trace hashing. Enforced by orchestrator/tools/dupcheck.py.
"""
