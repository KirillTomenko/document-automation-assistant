import copy
import json
from pathlib import Path

import pytest


@pytest.fixture
def scenarios():
    path = Path(__file__).parent / "fixtures" / "demo_contract.json"
    return json.loads(path.read_text(encoding="utf-8"))["scenarios"]


@pytest.fixture
def happy(scenarios):
    return copy.deepcopy(scenarios["A"]["result"])
