"""opengrid.trace -- the only module that WRITES `og.trace` rows (02a S8, 02b S1.6, S12).

Built on `opengrid.core.tracehash` (JCS + SHA-256 chain). Provides `append()` (K10: no command is
signed unless its decision pre-image is durably traced), `exists_preimage()` (what guardian's G-14
check calls instead of recomputing a hash itself), `checkpoint()`/`prune()` (02a S8.3) and `verify()`
(02a S8.4, K11: verifiable after random pruning).
"""

from opengrid.trace.store import TraceStore

__all__ = ["TraceStore"]
