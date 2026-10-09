"""Private replay cases for a deterministic company-state prototype."""
import copy
import importlib
import json
import sys
contract = json.load(sys.stdin)
module = importlib.import_module(contract["module"])
results = {}
for ticket, entry in contract["tickets"].items():
    passed = True
    for case in entry["cases"]:
        request = copy.deepcopy(case["request"])
        before = copy.deepcopy(request)
        try:
            actual = getattr(module, entry["operation"])(request)
            passed &= actual == case["expected"] and request == before
        except Exception:
            passed = False
    results[ticket] = bool(passed)
print(json.dumps(results, sort_keys=True))
