import unittest

import hypothesis

import test

lib = test.connect()


class PyGti2TestArrays(unittest.TestCase):
    def test_array_1(self):
        test_struct = lib._new("uint8_t [10]", addr=lib.gti2_memory._addr)
        test_struct[0] = 1
        self.assertEqual(test_struct[0], b"\x01")

        test_struct[0:5] = 5 * b"\xff"
        self.assertEqual(test_struct[0:5], 5 * b"\xff")

    def test_array_2(self):
        test_struct = lib._new("uint32_t [10]", addr=lib.gti2_memory._addr)
        test_struct[0] = 0x012345678
        self.assertEqual(test_struct[0], 0x012345678)

        test_struct[0:5] = 5 * [0x012345678]
        self.assertEqual(test_struct[0:5], 5 * [0x012345678])


class PyGti2TestStructs(unittest.TestCase):
    @hypothesis.given(
        a=hypothesis.strategies.integers(min_value=0, max_value=2**8 - 1),
    )
    @hypothesis.settings(max_examples=100)
    def test_structs_1(self, a):
        test_struct = lib._new("test_struct_1 *", addr=lib.gti2_memory._addr)
        test_struct.a = a
        self.assertEqual(test_struct.a, a)

    @hypothesis.given(
        a=hypothesis.strategies.integers(min_value=0, max_value=2**8 - 1),
        b=hypothesis.strategies.integers(min_value=0, max_value=2**32 - 1),
    )
    @hypothesis.settings(max_examples=100)
    def test_structs_2(self, a, b):
        test_struct = lib._new("test_struct_2 *", addr=lib.gti2_memory._addr)
        test_struct.a = a
        test_struct.b = b
        self.assertEqual(test_struct.a, a)
        self.assertEqual(test_struct.b, b)


class PyGti2TestStructsInFunctions(unittest.TestCase):
    @hypothesis.given(
        a=hypothesis.strategies.integers(min_value=0, max_value=2**8 - 1),
    )
    @hypothesis.settings(max_examples=100)
    def test_structs_1(self, a):
        test_struct = lib._new("test_struct_1 *", addr=lib.gti2_memory._addr)

        test_struct.a = a
        result = lib.test_structs_1(test_struct)
        self.assertEqual(result, a + 1)

    @hypothesis.given(
        a=hypothesis.strategies.integers(min_value=0, max_value=2**8 - 1),
    )
    @hypothesis.settings(max_examples=100)
    def test_structs_2(self, a):
        test_struct = lib._new("test_struct_1 *", addr=lib.gti2_memory._addr)
        result = lib.test_structs_2(test_struct)
        self.assertEqual(result._addr, test_struct._addr)
