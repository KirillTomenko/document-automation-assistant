"""Frozen pre-v2 hashes protect legacy canonical bytes, not just round trips."""

import hashlib
import json

import pytest

from document_automation.schemas import ProcessingResult, result_fingerprint
from document_automation.workflows.work_request import demo_contract

LEGACY_HASHES = {
    "A": (
        "111709110c0f1e7a8c0e1189a9bcf29315c9ba6a4182dc5bfb3b6485d574d77b",
        "26df79a41137d49c794c549a91d991bec0920ce7235379f5d621b52be01bfb9e",
    ),
    "B": (
        "d5927ab3bd1df8c9323af09dae02f3badfb8da35a874047ab1e325cd9a2a858d",
        "dbf3e675364d963141ab5dc13021254defc598c9f03dd74df98eb35dc84744dc",
    ),
    "C": (
        "e082f454e99d1b5680e3865b665caa375879d2d8225d10f4f51924eef3bf8941",
        "a87910a5665f57f47167086476f598188b78ac43ce880dcad48bbf6613cb8b5a",
    ),
    "T1": (
        "f3b37a5cd80ad6be11dbd0a6d57dd35aa678068cac508c47c868f0e8f0fae27e",
        "8a52ee6adbb3cfa1a0f86a78759937a404223eaaf1c0761ec4f30860721ebe8a",
    ),
    "T2": (
        "de6d81fc35f126b957318eb966dc0d9495edbff8937fc88873ba351ae8317f3c",
        "d984fa7f3af486ed05fcdc097150712268339a921b944818d263d1dd1d8f3f98",
    ),
}


@pytest.mark.parametrize("scenario", LEGACY_HASHES)
def test_pre_v2_canonical_bytes(scenarios, scenario):
    result = ProcessingResult.model_validate_json(
        json.dumps(scenarios[scenario]["result"])
    )
    assert result.contract_version == "1.0"
    assert "contract_version" not in result.model_dump()
    assert (
        hashlib.sha256(result.model_dump_json().encode()).hexdigest()
        == LEGACY_HASHES[scenario][0]
    )
    assert result_fingerprint(result) == LEGACY_HASHES[scenario][1]


def test_explicit_v2_roundtrip_and_multiple_groups(happy):
    happy["contract_version"] = "2.0"
    happy["classifications"].append(
        {**happy["classifications"][0], "document_type": "work_description"}
    )
    result = ProcessingResult.model_validate_json(json.dumps(happy))
    assert result.contract_version == "2.0"
    assert ProcessingResult.model_validate_json(result.model_dump_json()) == result
    assert demo_contract("2.0")["version"] == "2.0"
    assert set(demo_contract()["fields"]) == set(demo_contract("2.0")["fields"])


def test_unknown_contract_rejected(happy):
    happy["contract_version"] = "3.0"
    with pytest.raises(ValueError):
        ProcessingResult.model_validate_json(json.dumps(happy))
