import os
import sys
from pathlib import Path

import uvicorn

# Ensure repository root is on sys.path when invoked as `python gateway/main.py`
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


def main():
    uvicorn.run(
        "gateway.app:create_gateway_app",
        factory=True,
        host=os.getenv("OTTY_GATEWAY_HOST", "0.0.0.0"),
        port=int(os.getenv("OTTY_GATEWAY_PORT", "8080")),
    )


if __name__ == "__main__":
    main()
