# SPDX-License-Identifier: MIT
"""HedronOS shared library.

Everything that needs to be *correct* rather than merely *executed* lives here: the
GPU detection/decision engine, the package-set resolver, the app catalog, the
FlowState tuning model, the signed store index, and the verification checks.

Rules for this package (enforced by tools/hedronos-verify --strict):
  * stdlib only — no third-party imports (it ships on the ISO via python)
  * no side effects at import time
  * every public function must be testable offline with synthetic input
"""

__version__ = "1.0.0"
__all__ = ["gpus", "drivers", "pkgsets", "catalog", "tune", "storeindex", "units", "kconf", "util"]
