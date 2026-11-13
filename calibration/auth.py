"""Admin gate for calibration lifecycle and run-mutation actions.

SATVIQ has no user authentication anywhere; the calibration spec
nevertheless requires that YAML activation/rollback and run controls
not be open to anyone who can reach the page. Those endpoints (later
phases) check a shared admin token from the backend environment —
``SATVIQ_CALIBRATION_ADMIN_TOKEN`` — compared in constant time.

Fail-closed: when no token is configured, nothing is authorized.
The token is read from the backend environment only and is never
sent to or stored by the frontend beyond the operator typing it.
"""

from __future__ import annotations

import hmac
import os
from typing import Mapping

ADMIN_TOKEN_ENV = "SATVIQ_CALIBRATION_ADMIN_TOKEN"


def admin_token_configured(
        env: Mapping[str, str] | None = None) -> bool:
    env = env if env is not None else os.environ
    return bool((env.get(ADMIN_TOKEN_ENV) or "").strip())


def check_admin_token(provided: str | None,
                      env: Mapping[str, str] | None = None) -> bool:
    env = env if env is not None else os.environ
    expected = (env.get(ADMIN_TOKEN_ENV) or "").strip()
    if not expected or not provided:
        return False
    return hmac.compare_digest(str(provided), expected)
