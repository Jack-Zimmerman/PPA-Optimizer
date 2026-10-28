"""Dev-time automation: run the project's Claude Code agents headless (`claude -p --agent`).

Production replaces this with direct Anthropic API calls; the contract stays the same:
the RTL author sees only the (resolved) spec, never the verification suite.
"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
from pathlib import Path

from ..synth.openroad_flow import PROJECT_ROOT
from ..verify.interface import InterfaceError, extract_ports
from ..verify.spec import SpecError, parse_spec


class AgentError(RuntimeError):
    pass


def find_claude() -> str:
    """$CLAUDE_BIN, then `claude` on PATH, then the binary bundled with the VS Code extension."""
    if os.environ.get("CLAUDE_BIN"):
        return os.environ["CLAUDE_BIN"]
    if shutil.which("claude"):
        return shutil.which("claude")
    bundled = sorted(glob.glob(os.path.expanduser(
        "~/.vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude")))
    if bundled:
        return bundled[-1]
    raise AgentError("claude CLI not found: install Claude Code or set CLAUDE_BIN=/path/to/claude")


def run_agent(agent: str, prompt: str, *, deny_read: tuple[str, ...] = (), timeout_s: int = 900) -> str:
    cmd = [find_claude(), "-p", prompt, "--agent", agent, "--permission-mode", "acceptEdits",
           "--allowedTools", "Read", "Write", "Edit", "Bash(.venv/bin/python:*)"]
    if deny_read:
        cmd += ["--disallowedTools", *[f"Read({p})" for p in deny_read]]
    try:
        proc = subprocess.run(cmd, cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired as e:
        raise AgentError(f"{agent} timed out after {timeout_s} s") from e
    if proc.returncode != 0:
        tail = " | ".join(((proc.stderr or "") + (proc.stdout or "")).strip()[-1500:].splitlines())
        raise AgentError(f"{agent} failed (exit {proc.returncode}): "
                         + (tail or "the claude CLI printed nothing; a usage or rate limit, or a network problem, "
                            "looks like this (try `claude -p hi` to check)"))
    return proc.stdout.strip()


def _rel(p: Path) -> str:
    try:
        return str(p.resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return str(p.resolve())


def find_spec(arg: str) -> Path:
    """Accept a path, or a bare name such as `counter` or `fp16_multiply.resolved`, which is looked up
    as <name>.md in the current directory and examples/."""
    p = Path(arg)
    if p.is_file():
        return p
    for d in (Path("."), PROJECT_ROOT, PROJECT_ROOT / "examples"):
        for cand in (d / arg, d / f"{arg}.md"):
            if cand.is_file():
                return cand
    raise AgentError(f"spec not found: {arg} (tried it as a path, then {arg}.md in ., examples/)")


def resolve_spec(spec: Path, *, force: bool = False) -> Path:
    """Return a spec with a port table, running spec-resolver on a loose one."""
    spec = Path(spec)
    if not spec.exists():
        raise AgentError(f"spec not found: {spec}")
    try:
        parse_spec(spec.read_text())
        return spec
    except SpecError:
        pass
    resolved = spec.with_name(spec.stem + ".resolved.md")
    if resolved.exists() and not force:
        print(f"using existing {_rel(resolved)} (pass FORCE=1 to regenerate)")
    else:
        print(f"== spec-resolver: {_rel(spec)} has no port table, inferring one")
        print(run_agent("spec-resolver",
                        f"Resolve the informal description in {_rel(spec)} and write {_rel(resolved)}.",
                        deny_read=("./suites/**", "./examples/**/*.sv", "./examples/**/*.v")))
    try:
        parse_spec(resolved.read_text())
    except (SpecError, OSError) as e:
        raise AgentError(f"resolver did not produce a valid spec at {resolved}: {e}") from e
    return resolved


def generate_rtl(spec: Path, out: Path | None = None, *, force: bool = False) -> Path:
    """Spec (loose or resolved) -> RTL file. Resolves a loose spec first. Returns the RTL path."""
    resolved = resolve_spec(spec, force=force)
    top, ports = parse_spec(resolved.read_text())
    out = Path(out) if out else Path(spec).parent / f"{top}.sv"
    if out.exists() and not force:
        raise AgentError(f"{_rel(out)} already exists; pass FORCE=1 to overwrite it")

    print(f"== rtl-author: {_rel(resolved)} -> {_rel(out)}")
    print(run_agent("rtl-author",
                    f"Write the RTL for the spec {_rel(resolved)} to {_rel(out)} (top module `{top}`). "
                    "Do not look at suites/ or any other RTL.",
                    deny_read=("./suites/**",)))

    if not out.exists():
        raise AgentError(f"rtl-author did not write {out}")
    try:
        have = {(p.name, p.direction, p.width) for p in extract_ports([out], top)}
    except InterfaceError as e:
        raise AgentError(f"generated RTL does not elaborate:\n{e}") from e
    want = {(p.name, p.direction, p.width) for p in ports}
    if have != want:
        raise AgentError(f"generated ports differ from the spec: missing {sorted(want - have)}, "
                         f"unexpected {sorted(have - want)}")
    print(f"ok: {_rel(out)} elaborates and its ports match the spec")
    return out


# The suite author must not see any design. RTL lives next to specs in examples/ and in work/.
SUITE_DENY_READ = ("./examples/**/*.sv", "./examples/**/*.v", "./work/**")


def check_suite(suite_dir: Path, resolved: Path) -> None:
    """Static checks on a freshly written suite (no simulator): loads, ports match the spec,
    the model runs, cover points exist."""
    from ..verify.spec import check_suite_against_spec
    from ..verify.suite import SuiteError, load_suite

    try:
        suite = load_suite(suite_dir)
    except SuiteError as e:
        raise AgentError(f"suite is invalid: {e}") from e
    problems = check_suite_against_spec(suite, resolved)
    if problems:
        raise AgentError("suite ports differ from the spec: " + "; ".join(problems))
    try:
        zero = {n: 0 for n in suite.data_inputs}
        if suite.reset:
            zero[suite.reset[0]] = 0 if suite.reset[1] else 1     # reset asserted
        out = suite.module.Model().step(zero)
    except Exception as e:  # noqa: BLE001
        raise AgentError(f"suite Model fails on a reset step: {e!r}") from e
    missing = set(suite.outputs) - set(out)
    if missing:
        raise AgentError(f"suite Model.step does not return outputs {sorted(missing)}")
    print(f"ok: suite loads, ports match the spec, {len(suite.module.COVER)} cover points"
          + (", has assertions" if suite.sva else ", no assertions"))


def generate_suite(spec: Path, out_dir: Path | None = None, *, force: bool = False) -> Path:
    """Spec -> suites/<top>/ using the test-author agent, which never sees RTL."""
    resolved = resolve_spec(spec, force=force)
    top, _ = parse_spec(resolved.read_text())
    out_dir = Path(out_dir) if out_dir else PROJECT_ROOT / "suites" / top
    if (out_dir / "suite.py").exists() and not force:
        raise AgentError(f"{_rel(out_dir)} already has a suite; pass FORCE=1 to overwrite it")
    print(f"== test-author: {_rel(resolved)} -> {_rel(out_dir)}")
    print(run_agent("test-author",
                    f"Write the verification suite for the spec {_rel(resolved)} into {_rel(out_dir)} "
                    "(suite.py, and assertions.sv if the spec has per-cycle rules). Do not read any RTL.",
                    deny_read=SUITE_DENY_READ))
    check_suite(out_dir, resolved)
    return out_dir


def _feedback(q) -> str:
    """What the test author may learn from a weak qualification: coverage gaps and the kind of
    mutants that survived. No RTL text and no line numbers."""
    lines = ["The suite was qualified against an implementation and is not strong enough yet:"]
    lines += [f"- {r}" for r in q.reasons]
    if q.survivors:
        kinds: dict[str, int] = {}
        for s in q.survivors:
            kinds[s["label"]] = kinds.get(s["label"], 0) + 1
        lines.append("- implementations with these single-operator faults still passed: "
                     + ", ".join(f"{n}x '{k}'" for k, n in sorted(kinds.items())))
    lines.append("Strengthen the suite from the spec: add cover points/directed vectors/assertions for the "
                 "behaviours these gaps suggest. Some surviving faults may be harmless; do not weaken checks.")
    return "\n".join(lines)


def improve_suite(suite_dir: Path, rtl: Path, spec: Path, *, rounds: int = 2) -> bool:
    """Qualify, and while the suite is weak, send coverage/mutation feedback to test-author."""
    from ..synth.parallel import Candidate
    from ..verify.gate import qualify
    from ..verify.suite import load_suite

    resolved = resolve_spec(spec)
    top, _ = parse_spec(resolved.read_text())
    for i in range(rounds + 1):
        suite = load_suite(suite_dir)
        q = qualify(suite, Candidate(Path(rtl).stem, [Path(rtl)], suite.top))
        print(q.report())
        if q.verdict == "qualified":
            return True
        if q.verdict != "suite_weak":
            print(f"verdict {q.verdict}: not something the author can fix by adding tests; stopping")
            return False
        if i == rounds:
            break
        print(f"== test-author: improving the suite (round {i + 1}/{rounds})")
        print(run_agent("test-author",
                        f"The suite in {_rel(Path(suite_dir))} for spec {_rel(resolved)} needs strengthening.\n"
                        + _feedback(q) + "\nEdit the suite in place. Do not read any RTL.",
                        deny_read=SUITE_DENY_READ))
        check_suite(Path(suite_dir), resolved)
    return False


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Run the dev agents headless")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("rtl", help="generate RTL from a spec .md")
    r.add_argument("spec")
    r.add_argument("--out", help="output .sv (default: <spec dir>/<top module>.sv)")
    u = sub.add_parser("suite", help="generate the verification suite from a spec .md")
    u.add_argument("spec")
    u.add_argument("--out", help="suite directory (default: suites/<top module>)")
    i = sub.add_parser("improve", help="qualify a suite and feed gaps back to test-author")
    i.add_argument("spec")
    i.add_argument("--suite", required=True)
    i.add_argument("--rtl", required=True)
    i.add_argument("--rounds", type=int, default=2)
    for p in (r, u):
        p.add_argument("--force", action="store_true", help="overwrite existing output / regenerate the resolved spec")
    a = ap.parse_args()
    try:
        a.spec = find_spec(a.spec)
        if a.cmd == "rtl":
            generate_rtl(Path(a.spec), Path(a.out) if a.out else None, force=a.force)
        elif a.cmd == "suite":
            generate_suite(Path(a.spec), Path(a.out) if a.out else None, force=a.force)
        else:
            sys.exit(0 if improve_suite(Path(a.suite), Path(a.rtl), Path(a.spec), rounds=a.rounds) else 1)
    except AgentError as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
