#!/usr/bin/python3
"""Reference invocation of the installed maintenance tool; no second backup engine."""
import runpy
runpy.run_path('/usr/libexec/kane-system/maintenance.py',run_name='__main__')
