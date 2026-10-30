#!/usr/bin/env python3
"""Reuse locked Phase2 build/regression harness; add Phase3 native recovery cases.
Actual kernel power-cut/boot/ENOSPC validation is a separate marked offline VM.
This script never closes a stage or creates a source commit.
"""
import importlib.util
from pathlib import Path
if __name__=='__main__':
    path=Path(__file__).with_name('validate-phase2.py')
    spec=importlib.util.spec_from_file_location('phase2_validation_reused',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    raise SystemExit(module.main(phase=3))
