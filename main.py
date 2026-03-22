import os

from app import create_app

app = create_app()


def _read_port(default: int = 5000) -> int:
    raw = os.getenv("PICCRAWLER_PORT", str(default)).strip()
    try:
        port = int(raw)
    except ValueError:
        return default
    if 1 <= port <= 65535:
        return port
    return default


if __name__ == "__main__":
    debug_mode = os.getenv("PICCRAWLER_DEBUG", "0") == "1"
    host = os.getenv("PICCRAWLER_HOST", "0.0.0.0").strip() or "0.0.0.0"
    port = _read_port(5000)
    app.run(host=host, port=port, debug=debug_mode)
