import os

from app import create_app

app = create_app()

if __name__ == "__main__":
    debug_mode = os.getenv("PICCRAWLER_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=5000, debug=debug_mode)
