"""Synthetic reader probes test rejection only; they are not historical binaries."""
import importlib.util
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('historical_reader',Path(__file__).resolve().parents[1]/'build-historical-reader.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

class ReaderProbe(unittest.TestCase):
    def write_schema(self, schema):
        def run(args, **kwargs):
            with sqlite3.connect(args[2]) as conn:conn.execute(f'PRAGMA user_version={schema}')
        return run
    def test_matching_schema_probe(self):
        for schema in (6,7,8):
            with self.subTest(schema=schema),patch.object(module.subprocess,'run',side_effect=self.write_schema(schema)):
                module.verify_reader(Path('/synthetic-reader'),schema)
    def test_current_schema_is_not_accepted_as_historical(self):
        with patch.object(module.subprocess,'run',side_effect=self.write_schema(9)):
            with self.assertRaisesRegex(RuntimeError,'schema mismatch'):
                module.verify_reader(Path('/synthetic-reader'),7)
    def test_absent_database_is_rejected(self):
        with patch.object(module.subprocess,'run'):
            with self.assertRaisesRegex(RuntimeError,'did not create'):
                module.verify_reader(Path('/synthetic-reader'),7)

if __name__=='__main__':unittest.main()
