"""Tests for the live-runtime fixture."""

import unittest

from calculator import add


class CalculatorTest(unittest.TestCase):
    """Exercise the public calculator behavior."""

    def test_add_returns_sum(self) -> None:
        """Addition must not subtract the right operand."""

        self.assertEqual(add(2, 3), 5)


if __name__ == '__main__':
    unittest.main()
