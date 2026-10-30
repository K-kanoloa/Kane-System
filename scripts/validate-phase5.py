#!/usr/bin/env python3
"""One immutable-source validation. Does not close Phase5 or publish anything."""
import importlib.util,os
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if __name__=='__main__':
    os.environ.update(KANE_PHASE3_TEST='1',KANE_PHASE4_TEST='1',KANE_PHASE5_TEST='1')
    spec=importlib.util.spec_from_file_location('phase4_validation',ROOT/'scripts/validate-phase4.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    raise SystemExit(module.main())
