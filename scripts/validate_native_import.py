"""Import the shared native VM test driver without duplicating it."""
import importlib.util
from pathlib import Path
spec=importlib.util.spec_from_file_location('kane_native_validator',Path(__file__).with_name('validate-native-os.py'))
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
Vm=module.Vm
ROOT=module.ROOT
