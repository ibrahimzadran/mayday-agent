"""Force every annotation to be evaluated, on any Python version.

Python 3.14 defers annotation evaluation (PEP 649), so a name used only in an
annotation can be missing entirely and the module still imports cleanly. On
3.12 the same code raises NameError at import. That gap let a dropped
`from typing import Optional` reach CI: it could not fail on the machine it was
written on.

get_type_hints resolves annotations eagerly, which reproduces the older
behaviour regardless of what interpreter this runs under.
"""

import importlib
import inspect
import sys
import typing

MODULES = [
    "mayday.agent",
    "mayday.airline_client",
    "mayday.policy_index",
    "mcp_server.server",
    "backend.main",
    "backend.seed",
    "web.app",
    "sms.app",
    "sms.simulate",
    "evals.runner",
    "evals.assertions",
    "evals.cases",
    "evals.judge",
    "evals.trace",
    "evals.case",
]

failures: list[str] = []
checked = 0

for name in MODULES:
    try:
        module = importlib.import_module(name)
    except Exception as exc:
        failures.append(f"{name}: import failed — {type(exc).__name__}: {exc}")
        continue

    for attr, obj in vars(module).items():
        if getattr(obj, "__module__", None) != name:
            continue
        if not (inspect.isfunction(obj) or inspect.isclass(obj)):
            continue
        try:
            typing.get_type_hints(obj)
            checked += 1
        except Exception as exc:
            failures.append(f"{name}.{attr}: {type(exc).__name__}: {exc}")

print(f"checked {checked} annotated objects across {len(MODULES)} modules")
for f in failures:
    print(f"  FAIL {f}")
print(f"\n{'0 problems' if not failures else str(len(failures)) + ' problems'}")
sys.exit(1 if failures else 0)
