#!/usr/bin/env python3
"""Direct headless self-test of the frozen bundle, full output to file."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
INTERNAL = os.path.join(HERE, "dist", "NingBingJue", "_internal")
os.environ["NINGBING_ROOT"] = INTERNAL
if INTERNAL not in sys.path:
    sys.path.insert(0, INTERNAL)

import run
rc = run.run_headless(INTERNAL)
print("HEADLESS_RC=", rc)
sys.exit(rc if rc is not None else 0)
