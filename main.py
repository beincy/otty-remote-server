import argparse
import asyncio
import logging
import os

import uvicorn

from otty_server.app import TRUTHY, Settings, create_app
from otty_server.tunnel import TunnelClient, load_or_create_credentials


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Otty Server: connect local Otty to Gateway over WebSocket "
        "(and optionally listen for local HTTP debugging)."
    )
    parser.add_argument(
        "--gateway",
        default=os.getenv("OTTY_GATEWAY_URL", ""),
        help="Gateway base URL, e.g. https://gateway.example.com (env: OTTY_GATEWAY_URL)",
    )
    parser.add_argument(
        "--register-key",
        default=(
            os.getenv("OTTY_GATEWAY_REGISTER_KEY")
            or os.getenv("GATEWAY_REGISTER_KEY")
            or ""
        ),
        help="Gateway registration key (env: OTTY_GATEWAY_REGISTER_KEY)",
    )
    parser.add_argument(
        "--listen",
        action="store_true",
        default=os.getenv("OTTY_SERVER_LISTEN", "").lower() in TRUTHY,
        help="Also start local HTTP server for LAN/local debugging (env: OTTY_SERVER_LISTEN=1)",
    )
    parser.add_argument(
        "--host",
        default=os.getenv("OTTY_SERVER_HOST", "127.0.0.1"),
        help="Local HTTP bind host when --listen is enabled (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("OTTY_SERVER_PORT", "8000")),
        help="Local HTTP bind port when --listen is enabled (default: 8000)",
    )
    return parser.parse_args(argv)


async def run(args: argparse.Namespace):
    creds = load_or_create_credentials()
    os.environ["OTTY_SERVER_TOKEN"] = creds.token
    settings = Settings.from_env()
    app = create_app(settings)

    print("=== Otty Server ===")
    print(f"Instance ID : {creds.instance_id}")
    print(f"Token       : {creds.token}")
    if creds.path:
        status = "created" if creds.created else "loaded"
        print(f"Credentials : {creds.path} ({status}, mode 0600)")

    tasks: list[asyncio.Task] = []
    gateway_url = args.gateway.strip()
    if gateway_url:
        tunnel = TunnelClient(
            app,
            gateway_url=gateway_url,
            register_key=args.register_key,
            token=creds.token,
            instance_id=creds.instance_id,
        )
        print(f"Gateway WS  : {tunnel.ws_url}")
        print(f"PWA Client  : {gateway_url.rstrip('/')}/#token={creds.token}")
        print(f"Client HTTP : {gateway_url.rstrip('/')}/api/v1/tabs")
        print(
            f"            : {gateway_url.rstrip('/')}/i/{creds.instance_id}/api/v1/tabs"
        )
        tasks.append(asyncio.create_task(tunnel.serve_forever()))

    if args.listen or not gateway_url:
        print(f"Local PWA   : http://{args.host}:{args.port}/#token={creds.token}")
        print(f"Local HTTP  : http://{args.host}:{args.port}/api/v1/tabs")
        config = uvicorn.Config(app, host=args.host, port=args.port, log_level="info")
        server = uvicorn.Server(config)
        tasks.append(asyncio.create_task(server.serve()))

    print("===================", flush=True)
    await asyncio.gather(*tasks)


def main():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
