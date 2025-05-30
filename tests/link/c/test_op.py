import os
import string
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

import pytensor
from pytensor import scalar as ps
from pytensor.configdefaults import config
from pytensor.graph.basic import Apply
from pytensor.graph.utils import MethodNotDefined
from pytensor.link.c.op import COp


test_dir = Path(__file__).parent.absolute()

externalcop_test_code = f"""
from pytensor import tensor as pt
from pytensor.graph.basic import Apply
from pytensor.link.c.params_type import ParamsType
from pytensor.link.c.op import ExternalCOp
from pytensor.scalar import ScalarType
from pytensor.link.c.type import Generic
from pytensor.tensor.type import TensorType

tensor_type_0d = TensorType("float64", tuple())
scalar_type = ScalarType("float64")
generic_type = Generic()


class QuadraticCOpFunc(ExternalCOp):
    __props__ = ("a", "b", "c")
    params_type = ParamsType(a=tensor_type_0d, b=scalar_type, c=generic_type)

    def __init__(self, a, b, c):
        super().__init__(
            "{str(test_dir).replace(os.sep, "/")}/c_code/test_quadratic_function.c", "APPLY_SPECIFIC(compute_quadratic)"
        )
        self.a = a
        self.b = b
        self.c = c

    def make_node(self, x):
        x = pt.as_tensor_variable(x)
        return Apply(self, [x], [x.type()])

    def perform(self, node, inputs, output_storage, coefficients):
        x = inputs[0]
        y = output_storage[0]
        y[0] = coefficients.a * (x**2) + coefficients.b * x + coefficients.c


if __name__ == "__main__":
    qcop = QuadraticCOpFunc(1, 2, 3)

    print(qcop.c_code_cache_version())
    print("__success__")
"""


class StructOp(COp):
    __props__ = ()

    def do_constant_folding(self, fgraph, node):
        # we are not constant
        return False

    # The input only serves to distinguish thunks
    def make_node(self, i):
        return Apply(self, [i], [ps.uint64()])

    def c_support_code_struct(self, node, name):
        return f"npy_uint64 counter{name};"

    def c_init_code_struct(self, node, name, sub):
        return f"counter{name} = 0;"

    def c_code(self, node, name, input_names, outputs_names, sub):
        return f"""
{outputs_names[0]} = counter{name};
counter{name}++;
"""

    def c_code_cache_version(self):
        return (1,)

    def perform(self, *args, **kwargs):
        raise NotImplementedError("No Python implementation available.")


class TestCOp:
    @pytest.mark.skipif(
        not config.cxx, reason="G++ not available, so we need to skip this test."
    )
    def test_op_struct(self):
        sop = StructOp()
        c = sop(pytensor.tensor.constant(0))
        mode = None
        if config.mode == "FAST_COMPILE":
            mode = "FAST_RUN"
        f = pytensor.function([], c, mode=mode)
        rval = f()
        assert rval == 0
        rval = f()
        assert rval == 1

        c2 = sop(pytensor.tensor.constant(1))
        f2 = pytensor.function([], [c, c2], mode=mode)
        rval = f2()
        assert rval == [0, 0]


class TestMakeThunk:
    def test_no_c_code(self):
        class IncOnePython(COp):
            """An Op with only a Python (perform) implementation"""

            __props__ = ()

            def make_node(self, input):
                input = ps.as_scalar(input)
                output = input.type()
                return Apply(self, [input], [output])

            def perform(self, node, inputs, outputs):
                (input,) = inputs
                (output,) = outputs
                output[0] = input + 1

        i = ps.int32("i")
        o = IncOnePython()(i)

        # Check that the c_code function is not implemented
        with pytest.raises(NotImplementedError):
            o.owner.op.c_code(o.owner, "o", ["x"], "z", {"fail": ""})

        storage_map = {i: [np.int32(3)], o: [None]}
        compute_map = {i: [True], o: [False]}

        thunk = o.owner.op.make_thunk(
            o.owner, storage_map, compute_map, no_recycling=[]
        )

        required = thunk()
        # Check everything went OK
        assert not required  # We provided all inputs
        assert compute_map[o][0]
        assert storage_map[o][0] == 4

    def test_no_perform(self):
        class IncOneC(COp):
            """An Op with only a C (c_code) implementation"""

            __props__ = ()

            def make_node(self, input):
                input = ps.as_scalar(input)
                output = input.type()
                return Apply(self, [input], [output])

            def c_code(self, node, name, inputs, outputs, sub):
                (x,) = inputs
                (z,) = outputs
                return f"{z} = {x} + 1;"

            def perform(self, *args, **kwargs):
                raise NotImplementedError("No Python implementation available.")

        i = ps.int32("i")
        o = IncOneC()(i)

        # Check that the perform function is not implemented
        with pytest.raises((NotImplementedError, MethodNotDefined)):
            o.owner.op.perform(o.owner, 0, [None])

        storage_map = {i: [np.int32(3)], o: [None]}
        compute_map = {i: [True], o: [False]}

        thunk = o.owner.op.make_thunk(
            o.owner, storage_map, compute_map, no_recycling=[]
        )
        if config.cxx:
            required = thunk()
            # Check everything went OK
            assert not required  # We provided all inputs
            assert compute_map[o][0]
            assert storage_map[o][0] == 4
        else:
            with pytest.raises((NotImplementedError, MethodNotDefined)):
                thunk()


def get_hash(modname, seed=None):
    """From https://hg.python.org/cpython/file/5e8fa1b13516/Lib/test/test_hash.py#l145"""
    env = os.environ.copy()
    if seed is not None:
        env["PYTHONHASHSEED"] = str(seed)
    else:
        env.pop("PYTHONHASHSEED", None)
    cmd_line = [sys.executable, modname]
    p = subprocess.Popen(
        cmd_line,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    out, err = p.communicate()
    return out, err, p.returncode


def test_ExternalCOp_c_code_cache_version():
    """Make sure the C cache versions produced by `ExternalCOp` don't depend on `hash` seeding."""

    tmp = Path() / ("".join(np.random.choice(list(string.ascii_letters), 8)) + ".py")
    tmp.write_bytes(externalcop_test_code.encode())

    try:
        modname = tmp.name
        out_1, err1, returncode1 = get_hash(modname, seed=428)
        out_2, err2, returncode2 = get_hash(modname, seed=3849)
        assert returncode1 == 0
        assert returncode2 == 0
        assert err1 == err2

        hash_1, msg, _ = out_1.decode().split(os.linesep)
        assert msg == "__success__"
        hash_2, msg, _ = out_2.decode().split(os.linesep)
        assert msg == "__success__"

        assert hash_1 == hash_2
    finally:
        tmp.unlink()
