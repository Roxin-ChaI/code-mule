"""Local, model-free Phase 27 schema compatibility demonstration."""

import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from code_mule.state import (  # noqa: E402
    CURRENT_SCHEMA_VERSION,
    MIN_SUPPORTED_SCHEMA_VERSION,
    StateSchemaCompatibilityCode,
)
from code_mule.state.serialization import inspect_state_schema  # noqa: E402


def main() -> int:
    payload = {"schema_version": CURRENT_SCHEMA_VERSION}
    current = inspect_state_schema(json.loads(json.dumps(payload)))
    stale_max = CURRENT_SCHEMA_VERSION - 1
    stale_code = (
        StateSchemaCompatibilityCode.CLI_TOO_OLD
        if CURRENT_SCHEMA_VERSION > stale_max
        else StateSchemaCompatibilityCode.COMPATIBLE
    )
    print(
        "REPO CLI     "
        f"{current.code.value} · project {current.project_schema} · "
        f"supports {MIN_SUPPORTED_SCHEMA_VERSION}..{CURRENT_SCHEMA_VERSION}"
    )
    print(
        "STALE CLI    "
        f"{stale_code.value} · project {CURRENT_SCHEMA_VERSION} · "
        f"supports {MIN_SUPPORTED_SCHEMA_VERSION}..{stale_max}"
    )
    print("Update required · no state was modified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
