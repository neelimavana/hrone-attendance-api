"""
Sample data loader
==================

What this file is: a small helper script that came with the assignment kit.
What it does: wipes the `employees` and `attendance_logs` collections and loads the nine sample documents from
`sample_data/` (MongoDB Extended JSON), so you can try the API by hand.

Usage:  python sample_seed.py            (reads MONGO_URI / MONGO_DB from the environment or .env)

The samples only show the SHAPE of the data. The grader uses a much larger hidden dataset; the real checks live in
tests/test_api.py.
"""
import os
import pathlib

from bson import json_util
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()
here = pathlib.Path(__file__).parent / "sample_data"
opts = json_util.JSONOptions(tz_aware=True)
db = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)[os.getenv("MONGO_DB", "attendance_db")]

for name in ("employees", "attendance_logs"):
    docs = json_util.loads((here / f"{name}.json").read_text(), json_options=opts)
    db[name].delete_many({})
    db[name].insert_many(docs)
    print(f"loaded {len(docs):>2} documents into '{db.name}.{name}'")
