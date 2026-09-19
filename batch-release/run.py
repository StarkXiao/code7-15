#!/usr/bin/env python3
"""启动入口：python run.py [--db PATH] [--port N]"""
import argparse
import os

from app.server import serve

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get(
        "BATCH_RELEASE_DB", os.path.join(os.path.dirname(__file__), "data", "app.db")))
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    args = ap.parse_args()
    serve(args.db, args.host, args.port)
