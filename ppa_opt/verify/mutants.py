"""Source-level mutants of an RTL file, used to check that a suite can actually fail.

A suite that passes the RTL proves little if it would also pass a subtly broken RTL. Each
mutant changes one operator or constant; a good suite kills (fails) nearly all of them.
Mutants that do not build are discarded; survivors are reported for review since a few are
semantically equivalent to the original.
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass

# (regex for one operator occurrence, replacement, label). Lookarounds keep `<=`, `<<`, `==`,
# `->`, `++` etc. from being mistaken for the single-character operators.
RULES = [
    (r"(?<![+\w'])\+(?![+=])", "-", "+ -> -"),
    (r"(?<![-\w'>])-(?![-=>])", "+", "- -> +"),
    (r"(?<![&])&(?![&=])", "|", "& -> |"),
    (r"(?<![|])\|(?![|=])", "&", "| -> &"),
    (r"(?<![\^~])\^(?![~=])", "&", "^ -> &"),
    (r"&&", "||", "&& -> ||"),
    (r"\|\|", "&&", "|| -> &&"),
    (r"==", "!=", "== -> !="),
    (r"!=", "==", "!= -> =="),
    (r"(?<![<=!>])<(?![<=])", ">", "< -> >"),
    (r"(?<![<=!>-])>(?![>=])", "<", "> -> <"),
    (r"<<", ">>", "<< -> >>"),
    (r">>(?!>)", "<<", ">> -> <<"),
    (r"~(?!\^)", "", "drop ~"),
    (r"\b1'b0\b", "1'b1", "1'b0 -> 1'b1"),
    (r"\b1'b1\b", "1'b0", "1'b1 -> 1'b0"),
    (r"\bif\s*\((\w+)\)", lambda m: f"if (!{m.group(1)})", "negate if"),
    (r"(?<=<=)(\s*)0\b(?!')", lambda m: f"{m.group(1)}1", "reset value 0 -> 1"),
    (r"(?<![\w'\[:])([1-9]\d*)(?![\w'\]:])", lambda m: str(int(m.group(1)) + 1), "const + 1"),
]


@dataclass(frozen=True)
class Mutant:
    index: int
    label: str
    line: int
    text: str


def _code_mask(text: str) -> list[bool]:
    """True for characters that are code (not inside // or /* */ comments)."""
    mask = [True] * len(text)
    for m in re.finditer(r"//[^\n]*|/\*.*?\*/", text, re.S):
        for i in range(m.start(), m.end()):
            mask[i] = False
    return mask


def generate(text: str, limit: int = 24, seed: int = 1) -> list[Mutant]:
    mask = _code_mask(text)
    found: list[Mutant] = []
    for pat, rep, label in RULES:
        for m in re.finditer(pat, text):
            if not mask[m.start()]:
                continue
            new = rep(m) if callable(rep) else rep
            mutated = text[:m.start()] + new + text[m.end():]
            if mutated != text:
                found.append(Mutant(0, label, text.count("\n", 0, m.start()) + 1, mutated))
    random.Random(seed).shuffle(found)
    return [Mutant(i, m.label, m.line, m.text) for i, m in enumerate(found[:limit])]
