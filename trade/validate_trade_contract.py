#!/usr/bin/env python3
"""Static + protected-block validator for TRADE CONTRACT v1.

The active Trade bundle remains a single trade.py file. This validator belongs
to the reusable platform, not to the Trade bundle. It compares protected blocks
against the canonical protected blocks in trade.py and checks the public AST contract.

Dynamic self-tests should be run only in an isolated staging process/container.
"""
from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass, asdict
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Iterable

PROTECTED_BLOCKS = ("CONTRACT", "MARKET_DATA", "PAPER_TRUTH", "RUNTIME")

V2_ORIGINAL_PROTECTED_HASHES = {
    "CONTRACT": "2a599b5ec9e3486ec449a173df0d57347406fd96028793d1e40b5e824c7c6a50",
    "MARKET_DATA": "e2699bb955337561465eeff321126b51d28858e32dab37efdc10d9945fcd50dd",
    "PAPER_TRUTH": "60f1cb697cca47c1ee07ca26c2a00c69fe3105b5cb91ba807e40924e08de285a",
    "RUNTIME": "9b67458a62f799aad0c160e52d86aa0b1737fd0b10fce39d0555ffa6f3eac0c5"
}
V21_PROTECTED_HASHES = {
    "CONTRACT": "5da852382f97ab6c2eb18e82b2ecf5c19a0f65eacfd24cfc1a1119dbf0bb14fc",
    "MARKET_DATA": "65a19ad5cc45e3c454d0ed4672e9a4e0cda060f6f3110ce712e38480e6456137",
    "PAPER_TRUTH": "de18fab767c6700242e20570f0d792542a1b49aae198f048c45cd1895be2e31f",
    "RUNTIME": "51ca147ba02db05ff44246bcce754883dc0610abb2b7d5ee3924defa3ccfcc1b",
}
# 2.2: MARKET_DATA gains the tt_input source (labelled tt_input snapshot + exact exchange messages) and
# timestamp-tolerant sequence parsing; PAPER_TRUTH and RUNTIME are unchanged from 2.1.
V22_PROTECTED_HASHES = {
    "CONTRACT": "de6b5f4863bad393e8b55066fa1bdf402ebe6e0b4abae18adc0466915525a397",
    "MARKET_DATA": "988f8fd0ba1b82c3051d838169cbac6ee14b0662971725d5e5d5e0d66ffe7279",
    "PAPER_TRUTH": "de18fab767c6700242e20570f0d792542a1b49aae198f048c45cd1895be2e31f",
    "RUNTIME": "51ca147ba02db05ff44246bcce754883dc0610abb2b7d5ee3924defa3ccfcc1b",
}
# 2.3 (AI_test): CONTRACT adds position_id to PositionState/ExecutionRecord; PAPER_TRUTH has the costs as settings and
# keys positions by position_id (several at once); RUNTIME.initialize() calls ScientificCore.on_runtime_initialize().
# MARKET_DATA is unchanged from 2.2. Re-baseline these four when a protected block is edited on purpose.
V23_PROTECTED_HASHES = {
    "CONTRACT": "a8e95359e4479d5c38e9a42205de45b8428301aa83111e810190f46c5ad47da8",
    "MARKET_DATA": "988f8fd0ba1b82c3051d838169cbac6ee14b0662971725d5e5d5e0d66ffe7279",
    "PAPER_TRUTH": "ac0062a82a086f6057d7962ea740a82072e62949b90af6195bfdf8f4d30af7d2",
    "RUNTIME": "69aed1b5dfbb88b55059aca0290c56e3a84832eb4c3368a38f4243fac5aa4638",
}
CONTRACT_HASHES = {"2.0": V2_ORIGINAL_PROTECTED_HASHES, "2.1": V21_PROTECTED_HASHES, "2.2": V22_PROTECTED_HASHES, "2.3": V23_PROTECTED_HASHES}
REQUIRED_RUNTIME_METHODS = {
    "initialize",
    "on_l3_snapshot",
    "on_l3_update",
    "on_l3_checksum",
    "on_public_trade_snapshot",
    "on_public_trade",
    "run_live_market_data",
    "get_status",
    "get_positions",
    "get_recent_signals",
    "get_recent_executions",
    "get_metrics",
    "health",
    "get_ui_schema",
    "get_ui_snapshot",
}
REQUIRED_TOP_LEVEL = {
    "TradeDecision",
    "CleanMarketEvent",
    "TradeRuntime",
    "create_trade_system",
    "run_contract_self_test",
}


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str]
    warnings: list[str]
    protected_hashes: dict[str, str]


def _extract_block(text: str, name: str) -> str:
    begin = f"# PROTECTED:{name} BEGIN"
    end = f"# PROTECTED:{name} END"
    start = text.find(begin)
    if start < 0:
        raise ValueError(f"missing protected marker: {begin}")
    finish = text.find(end, start)
    if finish < 0:
        raise ValueError(f"missing protected marker: {end}")
    finish += len(end)
    return text[start:finish].replace("\r\n", "\n")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _names(tree: ast.Module) -> set[str]:
    out: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            out.add(node.name)
    return out


def _runtime_methods(tree: ast.Module) -> set[str]:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "TradeRuntime":
            return {
                child.name
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
    return set()


def validate(candidate: Path, canonical: Path) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    hashes: dict[str, str] = {}
    try:
        candidate_text = candidate.read_text(encoding="utf-8")
        canonical_text = canonical.read_text(encoding="utf-8")
    except Exception as exc:
        return ValidationResult(False, [f"read_failed:{exc}"], [], {})

    def contract_version(source: str) -> str | None:
        try:
            tree = ast.parse(source)
            for node in tree.body:
                if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "TRADE_CONTRACT_VERSION" for t in node.targets):
                    return ast.literal_eval(node.value)
        except (SyntaxError, ValueError, TypeError):
            pass
        return None
    version = contract_version(candidate_text)
    canonical_version = contract_version(canonical_text)
    if version != canonical_version or version not in CONTRACT_HASHES:
        errors.append(f"unsupported_or_mismatched_contract_version:{version}:{canonical_version}")
    expected_hashes = CONTRACT_HASHES.get(version, {})

    for block in PROTECTED_BLOCKS:
        try:
            c = _extract_block(candidate_text, block)
            ref = _extract_block(canonical_text, block)
            hashes[block] = _sha(c)
            expected = expected_hashes.get(block)
            if hashes[block] != expected:
                errors.append(f"protected_block_hash_mismatch:{block}")
            if _sha(ref) != expected:
                errors.append(f"canonical_protected_block_hash_mismatch:{block}")
            if c != ref:
                errors.append(f"protected_block_modified:{block}")
        except ValueError as exc:
            errors.append(str(exc))

    try:
        tree = ast.parse(candidate_text, filename=str(candidate))
    except SyntaxError as exc:
        errors.append(f"syntax_error:{exc.msg}:line_{exc.lineno}")
        return ValidationResult(False, errors, warnings, hashes)

    present = _names(tree)
    for name in sorted(REQUIRED_TOP_LEVEL - present):
        errors.append(f"missing_required_symbol:{name}")

    methods = _runtime_methods(tree)
    for method in sorted(REQUIRED_RUNTIME_METHODS - methods):
        errors.append(f"missing_runtime_method:{method}")

    if "ScientificCore" not in present:
        warnings.append("ScientificCore missing; editable workspace may be unusually structured")

    return ValidationResult(not errors, errors, warnings, hashes)


def run_self_test(candidate: Path, timeout: int = 10) -> tuple[bool, str]:
    proc = subprocess.run(
        [sys.executable, "-I", str(candidate.resolve())],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        cwd=str(candidate.resolve().parent),
    )
    return proc.returncode == 0, proc.stdout[-4000:]


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("candidate", type=Path)
    parser.add_argument(
        "--canonical",
        type=Path,
        default=Path(__file__).with_name("trade.py"),
    )
    parser.add_argument("--run-self-test", action="store_true")
    args = parser.parse_args(argv)

    result = validate(args.candidate, args.canonical)
    payload = asdict(result)
    if args.run_self_test and result.ok:
        try:
            ok, output = run_self_test(args.candidate)
        except Exception as exc:
            ok, output = False, repr(exc)
        payload["self_test"] = {"ok": ok, "output": output}
        if not ok:
            payload["ok"] = False
            payload["errors"].append("self_test_failed")
    print(json.dumps(payload, indent=2))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
