"""
main.py
Entry point — launches the Flask web dashboard.
Run: python main.py
Access: http://your-vps-ip:5000
"""

import argparse
from web_dashboard import run_server


def main():
    parser = argparse.ArgumentParser(description="TOS ORB Bot Web Dashboard")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=5000, help="Port (default: 5000)")
    parser.add_argument("--debug", action="store_true", help="Enable Flask debug mode")
    args = parser.parse_args()
    run_server(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
