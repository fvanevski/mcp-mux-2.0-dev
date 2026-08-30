import argparse
import os

import uvicorn

from mcp_router.core.config_loader import SecurityConfig, load_router_config
from mcp_router.core.security import validate_bind_security
from mcp_router.server import CONFIG_PATH, app


def main() -> None:
    """Parse command line arguments and launch the orchestrator server."""
    parser = argparse.ArgumentParser(
        description="Launch the Dynamic Multi-Endpoint Python MCP Router"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8012,
        help="Port to run the HTTP transport server on (default: 8012)",
    )
    parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="Host interface to bind to (default: 127.0.0.1)",
    )
    args = parser.parse_args()

    security = (
        load_router_config(CONFIG_PATH).security
        if os.path.exists(CONFIG_PATH)
        else SecurityConfig()
    )
    validate_bind_security(args.host, security)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
