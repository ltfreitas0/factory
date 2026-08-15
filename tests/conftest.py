import os
from pathlib import Path

os.environ.setdefault("FACTORY_DB", str(Path(__file__).resolve().parent / "_tmp_factory.db"))
