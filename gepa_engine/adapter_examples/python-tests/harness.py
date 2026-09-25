"""Call a case's function with each test's arguments, from the solution.py of the working directory, and print what it returned as JSON.

Reads ``{"function": "<nombre>", "calls": [[<argumentos>], ...]}`` from standard input and prints one
line ``{"loaded": true, "results": [{"returned": <valor>} | {"raised": "<error>"} | {"unserializable": "<repr>"}]}``.
It never receives the expected values: the adapter compares them in its own process, which the code of
the candidate cannot reach.
"""

import importlib
import json
import os
import sys


def main() -> None:
    spec = json.loads(sys.stdin.read())
    sys.path.insert(0, os.getcwd())  # solution.py is in the case's workspace, not next to this file
    try:
        function = getattr(importlib.import_module("solution"), spec["function"])
    except BaseException as error:  # a syntax error, an exception at import or a missing function: the case fails, it is not infrastructure
        print(json.dumps({"loaded": False, "error": f"{type(error).__name__}: {error}"[:300]}, ensure_ascii=False))
        return
    results = []
    for arguments in spec["calls"]:
        try:
            returned = function(*arguments)
        except BaseException as error:
            results.append({"raised": f"{type(error).__name__}: {error}"[:300]})
            continue
        try:
            json.dumps(returned)
            results.append({"returned": returned})
        except (TypeError, ValueError):
            results.append({"unserializable": repr(returned)[:300]})
    print(json.dumps({"loaded": True, "results": results}, ensure_ascii=False))


if __name__ == "__main__":
    main()
