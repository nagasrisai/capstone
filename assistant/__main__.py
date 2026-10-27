"""`python -m assistant` starts the webhook server."""

import os

import uvicorn

from .server import create_app

if __name__ == "__main__":
    uvicorn.run(create_app(), host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8000")))
