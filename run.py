import os
from app import create_app

app = create_app()

if __name__ == "__main__":
    print("🚀 VYDRA Backend Starting...")

    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", 10000)),
        debug=False,
    )