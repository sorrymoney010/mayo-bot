"""Launch only the read-only dashboard; never starts a trading monitor."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent / 'src'))
from dublin_bot.dashboard import BoundedDashboardHTTPServer, SimpleDashboardHandler

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--host', default='127.0.0.1')
    args = parser.parse_args()
    server = BoundedDashboardHTTPServer((args.host, args.port), SimpleDashboardHandler)
    print(f'Read-only dashboard: http://{args.host}:{args.port}/gunbot', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
