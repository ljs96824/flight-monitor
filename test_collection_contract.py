import importlib
import inspect
import unittest


class TestCollectionContract(unittest.TestCase):
    def test_module_level_tests_are_exposed_to_unittest(self):
        for module_name in ("test_aggregator_merge", "test_price_policy_email"):
            with self.subTest(module=module_name):
                module = importlib.import_module(module_name)
                module_tests = [
                    function
                    for name, function in inspect.getmembers(module, inspect.isfunction)
                    if name.startswith("test_") and function.__module__ == module.__name__
                ]
                unittest_suite = unittest.defaultTestLoader.loadTestsFromModule(module)
                self.assertEqual(unittest_suite.countTestCases(), len(module_tests))


if __name__ == "__main__":
    unittest.main()
