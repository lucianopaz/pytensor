import logging
import warnings
from collections.abc import Iterable
from typing import TYPE_CHECKING, Literal, Optional

import numpy as np
import numpy.typing as npt

import pytensor
from pytensor import scalar as ps
from pytensor.configdefaults import config
from pytensor.graph.basic import Variable
from pytensor.graph.type import HasDataType, HasShape
from pytensor.graph.utils import MetaType
from pytensor.link.c.type import CType
from pytensor.utils import apply_across_args


if TYPE_CHECKING:
    from numpy.typing import DTypeLike

    from pytensor.tensor.variable import TensorVariable


_logger = logging.getLogger("pytensor.tensor.type")


# Define common subsets of dtypes (as strings).
complex_dtypes = list(map(str, ps.complex_types))
continuous_dtypes = list(map(str, ps.continuous_types))
float_dtypes = list(map(str, ps.float_types))
integer_dtypes = list(map(str, ps.integer_types))
discrete_dtypes = list(map(str, ps.discrete_types))
all_dtypes = list(map(str, ps.all_types))
int_dtypes = list(map(str, ps.int_types))
uint_dtypes = list(map(str, ps.uint_types))

# TODO: add more type correspondences for e.g. int32, int64, float32,
# complex64, etc.
dtype_specs_map = {
    "float16": (float, "npy_float16", "NPY_FLOAT16"),
    "float32": (float, "npy_float32", "NPY_FLOAT32"),
    "float64": (float, "npy_float64", "NPY_FLOAT64"),
    "bool": (bool, "npy_bool", "NPY_BOOL"),
    "uint8": (int, "npy_uint8", "NPY_UINT8"),
    "int8": (int, "npy_int8", "NPY_INT8"),
    "uint16": (int, "npy_uint16", "NPY_UINT16"),
    "int16": (int, "npy_int16", "NPY_INT16"),
    "uint32": (int, "npy_uint32", "NPY_UINT32"),
    "int32": (int, "npy_int32", "NPY_INT32"),
    "uint64": (int, "npy_uint64", "NPY_UINT64"),
    "int64": (int, "npy_int64", "NPY_INT64"),
    "complex128": (complex, "pytensor_complex128", "NPY_COMPLEX128"),
    "complex64": (complex, "pytensor_complex64", "NPY_COMPLEX64"),
}


class TensorType(CType[np.ndarray], HasDataType, HasShape):
    r"""Symbolic `Type` representing `numpy.ndarray`\s."""

    __props__: tuple[str, ...] = ("dtype", "shape")

    dtype_specs_map = dtype_specs_map
    context_name = "cpu"
    filter_checks_isfinite = False
    """
    When this is ``True``, strict filtering rejects data containing
    ``numpy.nan`` or ``numpy.inf`` entries. (Used in `DebugMode`)
    """

    def __init__(
        self,
        dtype: str | npt.DTypeLike,
        shape: Iterable[bool | int | None] | None = None,
        name: str | None = None,
        broadcastable: Iterable[bool] | None = None,
    ):
        r"""

        Parameters
        ----------
        dtype
            A NumPy dtype (e.g. ``"int64"``).
        shape
            The static shape information.  ``None``\s are used to indicate
            unknown shape values for their respective dimensions.
            If `shape` is a list of ``bool``\s, the ``True`` elements of are
            converted to ``1``\s and the ``False`` values are converted to
            ``None``\s.
        name
            Optional name for this type.

        """

        if broadcastable is not None:
            warnings.warn(
                "The `broadcastable` keyword is deprecated; use `shape`.",
                DeprecationWarning,
            )
            shape = broadcastable

        if str(dtype) == "floatX":
            self.dtype = config.floatX
        else:
            try:
                self.dtype = str(np.dtype(dtype))
            except TypeError:
                raise TypeError(f"Invalid dtype: {dtype}")

        def parse_bcast_and_shape(s):
            if isinstance(s, bool | np.bool_):
                return 1 if s else None
            elif isinstance(s, int | np.integer):
                return int(s)
            elif s is None:
                return s
            raise ValueError(
                f"TensorType broadcastable/shape must be a boolean, integer or None, got {type(s)} {s}"
            )

        self.shape = tuple(parse_bcast_and_shape(s) for s in shape)
        self.dtype_specs()  # error checking is done there
        self.name = name
        self.numpy_dtype = np.dtype(self.dtype)

    def __call__(self, *args, shape=None, **kwargs):
        if shape is not None:
            # Check if shape is compatible with the original type
            new_type = self.clone(shape=shape)
            if self.is_super(new_type):
                return new_type(*args, **kwargs)
            else:
                raise ValueError(
                    f"{shape=} is incompatible with original type shape {self.shape=}"
                )
        return super().__call__(*args, **kwargs)

    def clone(
        self, dtype=None, shape=None, broadcastable=None, **kwargs
    ) -> "TensorType":
        if broadcastable is not None:
            warnings.warn(
                "The `broadcastable` keyword is deprecated; use `shape`.",
                DeprecationWarning,
            )
            shape = broadcastable
        if dtype is None:
            dtype = self.dtype
        if shape is None:
            shape = self.shape
        return type(self)(dtype, shape, name=self.name)

    def filter(self, data, strict=False, allow_downcast=None) -> np.ndarray:
        """Convert `data` to something which can be associated to a `TensorVariable`.

        This function is not meant to be called in user code. It is for
        `Linker` instances to use when running a compiled graph.

        """
        # Explicit error message when one accidentally uses a Variable as
        # input (typical mistake, especially with shared variables).
        if isinstance(data, Variable):
            raise TypeError(
                "Expected an array-like object, but found a Variable: "
                "maybe you are trying to call a function on a (possibly "
                "shared) variable instead of a numeric array?"
            )

        if isinstance(data, np.memmap) and (data.dtype == self.numpy_dtype):
            # numpy.memmap is a "safe" subclass of ndarray,
            # so we can use it wherever we expect a base ndarray.
            # however, casting it would defeat the purpose of not
            # loading the whole data into memory
            pass
        elif isinstance(data, np.ndarray) and (data.dtype == self.numpy_dtype):
            if data.dtype.num != self.numpy_dtype.num:
                data = np.asarray(data, dtype=self.dtype)
            # -- now fall through to ndim check
        elif strict:
            # If any of the two conditions above was not met,
            # we raise a meaningful TypeError.
            if not isinstance(data, np.ndarray):
                raise TypeError(
                    f"{self} expected an ndarray object (got {type(data)})."
                )
            if data.dtype != self.numpy_dtype:
                raise TypeError(
                    f"{self} expected an ndarray with dtype={self.numpy_dtype} (got {data.dtype})."
                )
        else:
            if allow_downcast:
                # Convert to self.dtype, regardless of the type of data
                data = np.asarray(data).astype(self.dtype)
                # TODO: consider to pad shape with ones to make it consistent
                # with self.broadcastable... like vector->row type thing
            else:
                if isinstance(data, np.ndarray):
                    # Check if self.dtype can accurately represent data
                    # (do not try to convert the data)
                    up_dtype = ps.upcast(self.dtype, data.dtype)
                    if up_dtype == self.dtype:
                        # Bug in the following line when data is a
                        # scalar array, see
                        # http://projects.scipy.org/numpy/ticket/1611
                        # data = data.astype(self.dtype)
                        data = np.asarray(data, dtype=self.dtype)
                    if up_dtype != self.dtype:
                        err_msg = (
                            f"{self} cannot store a value of dtype {data.dtype} without "
                            "risking loss of precision. If you do not mind "
                            "this loss, you can: "
                            f"1) explicitly cast your data to {self.dtype}, or "
                            '2) set "allow_input_downcast=True" when calling '
                            f'"function". Value: "{data!r}"'
                        )
                        raise TypeError(err_msg)
                elif (
                    allow_downcast is None
                    and isinstance(data, float | np.floating)
                    and self.dtype == config.floatX
                ):
                    # Special case where we allow downcasting of Python float
                    # literals to floatX, even when floatX=='float32'
                    data = np.asarray(data, self.dtype)
                else:
                    # data has to be converted.
                    # Check that this conversion is lossless
                    converted_data = np.asarray(data, self.dtype)
                    # We use the `values_eq` static function from TensorType
                    # to handle NaN values.
                    if TensorType.values_eq(
                        np.asarray(data), converted_data, force_same_dtype=False
                    ):
                        data = converted_data
                    else:
                        # Do not print a too long description of data
                        # (ndarray truncates it, but it's not sure for data)
                        str_data = str(data)
                        if len(str_data) > 80:
                            str_data = str_data[:75] + "(...)"

                        err_msg = (
                            f"{self} cannot store accurately value {data}, "
                            f"it would be represented as {converted_data}. "
                            "If you do not mind this precision loss, you can: "
                            "1) explicitly convert your data to a numpy array "
                            f"of dtype {self.dtype}, or "
                            '2) set "allow_input_downcast=True" when calling '
                            '"function".'
                        )
                        raise TypeError(err_msg)

        if self.ndim != data.ndim:
            raise TypeError(
                f"Wrong number of dimensions: expected {self.ndim},"
                f" got {data.ndim} with shape {data.shape}."
            )
        if not data.flags.aligned:
            raise TypeError(
                "The numpy.ndarray object is not aligned."
                " PyTensor C code does not support that.",
            )

        # zip strict not specified because we are in a hot loop
        if not all(
            ds == ts if ts is not None else True
            for ds, ts in zip(data.shape, self.shape)
        ):
            raise TypeError(
                f"The type's shape ({self.shape}) is not compatible with the data's ({data.shape})"
            )

        if self.filter_checks_isfinite and not np.all(np.isfinite(data)):
            raise ValueError("Non-finite elements not allowed")
        return data

    def filter_variable(self, other, allow_convert=True):
        if not isinstance(other, Variable):
            # The value is not a Variable: we cast it into
            # a Constant of the appropriate Type.
            other = self.constant_type(type=self, data=other)

        if other.type == self:
            return other

        if allow_convert:
            other2 = self.convert_variable(other)
            if other2 is not None:
                return other2

        raise TypeError(
            f"Cannot convert Type {other.type} "
            f"(of Variable {other}) into Type {self}. "
            f"You can try to manually convert {other} into a {self}."
        )

    def dtype_specs(self):
        """
        Return a tuple (python type, c type, numpy typenum) that corresponds
        to self.dtype.

        This function is used internally as part of C code generation.

        """
        try:
            return self.dtype_specs_map[self.dtype]
        except KeyError:
            raise TypeError(
                f"Unsupported dtype for {self.__class__.__name__}: {self.dtype}"
            )

    def to_scalar_type(self):
        return ps.get_scalar_type(dtype=self.dtype)

    def in_same_class(self, otype):
        r"""Determine if `otype` is in the same class of fixed broadcastable types as `self`.

        A class of fixed broadcastable types is a set of `TensorType`\s that all have the
        same pattern of static ``1``\s in their shape.  For instance, `Type`\s with the
        shapes ``(2, 1)``, ``(3, 1)``, and ``(None, 1)`` all belong to the same class
        of fixed broadcastable types, whereas ``(2, None)`` does not belong to that class.
        Although the last dimension of the partial shape information ``(2, None)`` could
        technically be ``1`` (i.e. broadcastable), it's not *guaranteed* to be ``1``, and
        that's what prevents membership into the class.

        """
        if (
            isinstance(otype, TensorType)
            and otype.dtype == self.dtype
            and otype.broadcastable == self.broadcastable
        ):
            return True
        return False

    def is_super(self, otype):
        # zip strict not specified because we are in a hot loop
        if (
            isinstance(otype, type(self))
            and otype.dtype == self.dtype
            and otype.ndim == self.ndim
            # `otype` is allowed to be as or more shape-specific than `self`,
            # but not less
            and all(sb == ob or sb is None for sb, ob in zip(self.shape, otype.shape))
        ):
            return True

        return False

    def convert_variable(self, var):
        if self.is_super(var.type):
            # `var.type` is as specific as `self`, so we return `var` as-is
            return var

        if (self.ndim == var.type.ndim) and (self.dtype == var.type.dtype):
            # `var.type` only differs from `self` in that its shape is (at least partially)
            # less specific than `self`, so we convert `var` to `self`'s `Type`.
            # `specify_shape` will combine the more precise shapes of the two types
            return pytensor.tensor.specify_shape(var, self.shape)

    @staticmethod
    def values_eq(a, b, force_same_dtype=True):
        # TODO: check to see if the shapes must match; for now, we err on safe
        # side...
        if a.shape != b.shape:
            return False
        if force_same_dtype and a.dtype != b.dtype:
            return False
        a_eq_b = a == b
        r = np.all(a_eq_b)
        if r:
            return True
        # maybe the trouble is that there are NaNs
        a_missing = np.isnan(a)
        if a_missing.any():
            b_missing = np.isnan(b)
            return np.all(a_eq_b + (a_missing == b_missing))
        else:
            return False

    @staticmethod
    def values_eq_approx(
        a, b, allow_remove_inf=False, allow_remove_nan=False, rtol=None, atol=None
    ):
        return values_eq_approx(a, b, allow_remove_inf, allow_remove_nan, rtol, atol)

    def __eq__(self, other):
        if type(self) is not type(other):
            return NotImplemented

        return other.dtype == self.dtype and other.shape == self.shape

    def __hash__(self):
        return hash((type(self), self.dtype, self.shape))

    @property
    def broadcastable(self):
        """A boolean tuple indicating which dimensions have a shape equal to one."""
        return tuple(s == 1 for s in self.shape)

    @property
    def ndim(self):
        """The number of dimensions."""
        return len(self.shape)

    def __str__(self):
        if self.name:
            return self.name
        else:
            shape = self.shape
            len_shape = len(shape)
            formatted_shape = str(shape).replace("None", "?")

            if len_shape > 2:
                name = f"Tensor{len_shape}"
            else:
                name = ("Scalar", "Vector", "Matrix")[len_shape]
            return f"{name}({self.dtype}, shape={formatted_shape})"

    def __repr__(self):
        return f"TensorType({self.dtype}, shape={self.shape})"

    @staticmethod
    def may_share_memory(a, b):
        # This is a method of TensorType, so both a and b should be ndarrays
        if isinstance(a, np.ndarray) and isinstance(b, np.ndarray):
            return np.may_share_memory(a, b)
        else:
            return False

    def get_shape_info(self, obj):
        """Return the information needed to compute the memory size of `obj`.

        The memory size is only the data, so this excludes the container.
        For an ndarray, this is the data, but not the ndarray object and
        other data structures such as shape and strides.

        `get_shape_info` and `get_size` work in tandem for the memory
        profiler.

        `get_shape_info` is called during the execution of the function.
        So it is better that it is not too slow.

        `get_size` will be called on the output of this function
        when printing the memory profile.

        Parameters
        ----------
        obj
            The object that this Type represents during execution.

        Returns
        -------
        object
            Python object that can be passed to `get_size`.

        """
        return obj.shape

    def get_size(self, shape_info):
        """Number of bytes taken by the object represented by `shape_info`.

        Parameters
        ----------
        shape_info
            The output of the call to `get_shape_info`.

        Returns
        -------
        int
            The number of bytes taken by the object described by ``shape_info``.

        """
        if shape_info:
            return np.prod(shape_info) * np.dtype(self.dtype).itemsize
        else:  # a scalar
            return np.dtype(self.dtype).itemsize

    def c_element_type(self):
        return self.dtype_specs()[1]

    def c_declare(self, name, sub, check_input=True):
        if check_input:
            dtype = self.dtype_specs()[1]
            check = f"""
            typedef {dtype} dtype_{name};
            """
        else:
            check = ""
        declaration = f"""
        PyArrayObject* {name};
        """

        return declaration + check

    def c_init(self, name, sub):
        return f"""
        {name} = NULL;
        """

    def c_extract(self, name, sub, check_input=True, **kwargs):
        if check_input:
            fail = sub["fail"]
            type_num = self.dtype_specs()[2]
            check = f"""
            {name} = NULL;
            if (py_{name} == Py_None) {{
                // We can either fail here or set {name} to NULL and rely on Ops
                // using tensors to handle the NULL case, but if they fail to do so
                // they'll end up with nasty segfaults, so this is public service.
                PyErr_SetString(PyExc_ValueError, "expected an ndarray, not None");
                {fail}
            }}
            if (!PyArray_Check(py_{name})) {{
                PyErr_SetString(PyExc_ValueError, "expected an ndarray");
                {fail}
            }}
            // We expect {type_num}
            if (!PyArray_ISALIGNED((PyArrayObject*) py_{name})) {{
                PyArrayObject * tmp = (PyArrayObject*) py_{name};
                PyErr_Format(PyExc_NotImplementedError,
                             "expected an aligned array of type %ld "
                             "({type_num}), got non-aligned array of type %ld"
                             " with %ld dimensions, with 3 last dims "
                             "%ld, %ld, %ld"
                             " and 3 last strides %ld %ld, %ld.",
                             (long int) {type_num},
                             (long int) PyArray_TYPE((PyArrayObject*) py_{name}),
                             (long int) PyArray_NDIM(tmp),
                             (long int) (PyArray_NDIM(tmp) >= 3 ?
            PyArray_DIMS(tmp)[PyArray_NDIM(tmp)-3] : -1),
                             (long int) (PyArray_NDIM(tmp) >= 2 ?
            PyArray_DIMS(tmp)[PyArray_NDIM(tmp)-2] : -1),
                             (long int) (PyArray_NDIM(tmp) >= 1 ?
            PyArray_DIMS(tmp)[PyArray_NDIM(tmp)-1] : -1),
                             (long int) (PyArray_NDIM(tmp) >= 3 ?
            PyArray_STRIDES(tmp)[PyArray_NDIM(tmp)-3] : -1),
                             (long int) (PyArray_NDIM(tmp) >= 2 ?
            PyArray_STRIDES(tmp)[PyArray_NDIM(tmp)-2] : -1),
                             (long int) (PyArray_NDIM(tmp) >= 1 ?
            PyArray_STRIDES(tmp)[PyArray_NDIM(tmp)-1] : -1)
            );
                {fail}
            }}
            // This is a TypeError to be consistent with DEBUG_MODE
            // Note: DEBUG_MODE also tells the name of the container
            if (PyArray_TYPE((PyArrayObject*) py_{name}) != {type_num}) {{
                PyErr_Format(PyExc_TypeError,
                             "expected type_num %d ({type_num}) got %d",
                             {type_num}, PyArray_TYPE((PyArrayObject*) py_{name}));
                {fail}
            }}
            """
        else:
            check = ""
        return (
            check
            + f"""
        {name} = (PyArrayObject*)(py_{name});
        Py_XINCREF({name});
        """
        )

    def c_cleanup(self, name, sub):
        return f"""
        if ({name}) {{
            Py_XDECREF({name});
        }}
        """

    def c_sync(self, name, sub):
        fail = sub["fail"]
        return f"""
        {{Py_XDECREF(py_{name});}}
        if (!{name}) {{
            Py_INCREF(Py_None);
            py_{name} = Py_None;
        }}
        else if ((void*)py_{name} != (void*){name}) {{
            py_{name} = (PyObject*){name};
        }}

        {{Py_XINCREF(py_{name});}}

        if ({name} && !PyArray_ISALIGNED((PyArrayObject*) py_{name})) {{
            PyErr_Format(PyExc_NotImplementedError,
                         "c_sync: expected an aligned array, got non-aligned array of type %ld"
                         " with %ld dimensions, with 3 last dims "
                         "%ld, %ld, %ld"
                         " and 3 last strides %ld %ld, %ld.",
                         (long int) PyArray_TYPE((PyArrayObject*) py_{name}),
                         (long int) PyArray_NDIM({name}),
                         (long int) (PyArray_NDIM({name}) >= 3 ?
        PyArray_DIMS({name})[PyArray_NDIM({name})-3] : -1),
                         (long int) (PyArray_NDIM({name}) >= 2 ?
        PyArray_DIMS({name})[PyArray_NDIM({name})-2] : -1),
                         (long int) (PyArray_NDIM({name}) >= 1 ?
        PyArray_DIMS({name})[PyArray_NDIM({name})-1] : -1),
                         (long int) (PyArray_NDIM({name}) >= 3 ?
        PyArray_STRIDES({name})[PyArray_NDIM({name})-3] : -1),
                         (long int) (PyArray_NDIM({name}) >= 2 ?
        PyArray_STRIDES({name})[PyArray_NDIM({name})-2] : -1),
                         (long int) (PyArray_NDIM({name}) >= 1 ?
        PyArray_STRIDES({name})[PyArray_NDIM({name})-1] : -1)
        );
            {fail}
        }}
        """

    def c_headers(self, **kwargs):
        return ps.get_scalar_type(self.dtype).c_headers(**kwargs)

    def c_libraries(self, **kwargs):
        return ps.get_scalar_type(self.dtype).c_libraries(**kwargs)

    def c_compile_args(self, **kwargs):
        return ps.get_scalar_type(self.dtype).c_compile_args(**kwargs)

    def c_support_code(self, **kwargs):
        return ps.get_scalar_type(self.dtype).c_support_code(**kwargs)

    def c_init_code(self, **kwargs):
        return ps.get_scalar_type(self.dtype).c_init_code(**kwargs)

    def c_code_cache_version(self):
        scalar_version = ps.get_scalar_type(self.dtype).c_code_cache_version()
        if scalar_version:
            return (11, *scalar_version)
        else:
            return ()


class DenseTypeMeta(MetaType):
    def __instancecheck__(self, o):
        if type(o) is TensorType or isinstance(o, DenseTypeMeta):
            return True
        return False


class DenseTensorType(TensorType, metaclass=DenseTypeMeta):
    r"""A `Type` for dense tensors.

    Instances of this class and `TensorType`\s are considered dense `Type`\s.
    """


def values_eq_approx(
    a, b, allow_remove_inf=False, allow_remove_nan=False, rtol=None, atol=None
):
    """
    Parameters
    ----------
    allow_remove_inf
        If True, when there is an inf in a, we allow any value in b in
        that position. Event -inf
    allow_remove_nan
        If True, when there is a nan in a, we allow any value in b in
        that position. Event +-inf
    rtol
        Relative tolerance, passed to _allclose.
    atol
        Absolute tolerance, passed to _allclose.

    """
    if isinstance(a, np.ndarray) and isinstance(b, np.ndarray):
        if a.shape != b.shape:
            return False
        if a.dtype != b.dtype:
            return False
        if str(a.dtype) not in continuous_dtypes:
            return np.all(a == b)
        else:
            cmp = pytensor.tensor.math._allclose(a, b, rtol=rtol, atol=atol)
            if cmp:
                # Numpy claims they are close, this is good enough for us.
                return True
            # Numpy is unhappy, but it does not necessarily mean that a and
            # b are different. Indeed, Numpy does not like missing values
            # and will return False whenever some are found in a or b.
            # The proper way would be to use the MaskArray stuff available
            # in Numpy. However, it looks like it has been added to Numpy's
            # core recently, so it may not be available to everyone. Thus,
            # for now we use a home-made recipe, that should probably be
            # revisited in the future.
            a_missing = np.isnan(a)
            a_inf = np.isinf(a)

            if not (a_missing.any() or (allow_remove_inf and a_inf.any())):
                # There are no missing values in a, thus this is not the
                # reason why numpy.allclose(a, b) returned False.
                _logger.info(
                    f"numpy allclose failed for abs_err {np.max(abs(a - b))} and rel_err {np.max(abs(a - b) / (abs(a) + abs(b)))}"
                )
                return False
            # The following line is what numpy.allclose bases its decision
            # upon, according to its documentation.
            rtol = 1.0000000000000001e-05
            atol = 1e-8
            cmp_elemwise = np.absolute(a - b) <= (atol + rtol * np.absolute(b))
            # Find places where both a and b have missing values.
            both_missing = a_missing * np.isnan(b)

            # Find places where both a and b have inf of the same sign.
            both_inf = a_inf * np.isinf(b)

            # cmp_elemwise is weird when we have inf and -inf.
            # set it to False
            cmp_elemwise = np.where(both_inf & cmp_elemwise, a == b, cmp_elemwise)

            # check the sign of the inf
            both_inf = np.where(both_inf, (a == b), both_inf)

            if allow_remove_inf:
                both_inf += a_inf
            if allow_remove_nan:
                both_missing += a_missing

            # Combine all information.
            return (cmp_elemwise + both_missing + both_inf).all()

    return False


def values_eq_approx_remove_inf(a, b):
    return values_eq_approx(a, b, True)


def values_eq_approx_remove_nan(a, b):
    return values_eq_approx(a, b, False, True)


def values_eq_approx_remove_inf_nan(a, b):
    return values_eq_approx(a, b, True, True)


def values_eq_approx_always_true(a, b):
    return True


pytensor.compile.register_view_op_c_code(
    TensorType,
    """
    Py_XDECREF(%(oname)s);
    %(oname)s = %(iname)s;
    Py_XINCREF(%(oname)s);
    """,
    version=1,
)


pytensor.compile.register_deep_copy_op_c_code(
    TensorType,
    """
    int alloc = %(oname)s == NULL;
    for(int i=0; !alloc && i<PyArray_NDIM(%(oname)s); i++) {
       if(PyArray_DIMS(%(iname)s)[i] != PyArray_DIMS(%(oname)s)[i]) {
           alloc = true;
           break;
       }
    }
    if(alloc) {
        Py_XDECREF(%(oname)s);
        %(oname)s = (PyArrayObject*)PyArray_NewCopy(%(iname)s,
                                                    NPY_ANYORDER);
        if (!%(oname)s)
        {
            PyErr_SetString(PyExc_ValueError,
                            "DeepCopyOp: the copy failed!");
            %(fail)s;
        }
    } else {
        if(PyArray_CopyInto(%(oname)s, %(iname)s)){
            PyErr_SetString(PyExc_ValueError,
        "DeepCopyOp: the copy failed into already allocated space!");
            %(fail)s;
        }
    }
    """,
    version=2,
)

# Valid static type entries
ST = int | None


def tensor(
    name: str | None = None,
    *,
    dtype: Optional["DTypeLike"] = None,
    shape: tuple[ST, ...] | None = None,
    **kwargs,
) -> "TensorVariable":
    if name is not None:
        try:
            # Help catching errors with the new tensor API
            # Many single letter strings are valid sctypes
            if str(name) == "floatX" or (len(str(name)) > 2 and np.dtype(name).type):
                raise ValueError(
                    f"The first and only positional argument of tensor is now `name`. Got {name}.\n"
                    "This name looks like a dtype, which you should pass as a keyword argument only."
                )
        except TypeError:
            pass

    if dtype is None:
        dtype = config.floatX

    return TensorType(dtype=dtype, shape=shape, **kwargs)(name=name)


cscalar = TensorType("complex64", ())
zscalar = TensorType("complex128", ())
fscalar = TensorType("float32", ())
dscalar = TensorType("float64", ())
bscalar = TensorType("int8", ())
wscalar = TensorType("int16", ())
iscalar = TensorType("int32", ())
lscalar = TensorType("int64", ())
ubscalar = TensorType("uint8", ())
uwscalar = TensorType("uint16", ())
uiscalar = TensorType("uint32", ())
ulscalar = TensorType("uint64", ())


def scalar(
    name: str | None = None,
    *,
    dtype: Optional["DTypeLike"] = None,
) -> "TensorVariable":
    """Return a symbolic scalar variable.

    Parameters
    ----------
    dtype: numeric
        None means to use pytensor.config.floatX.
    name
        A name to attach to this variable.

    """
    if dtype is None:
        dtype = config.floatX
    type = TensorType(dtype, ())
    return type(name)


scalars, fscalars, dscalars, iscalars, lscalars = apply_across_args(
    scalar, fscalar, dscalar, iscalar, lscalar
)

int_types = bscalar, wscalar, iscalar, lscalar
float_types = fscalar, dscalar
complex_types = cscalar, zscalar
int_scalar_types = int_types
float_scalar_types = float_types
complex_scalar_types = complex_types

cvector = TensorType("complex64", shape=(None,))
zvector = TensorType("complex128", shape=(None,))
fvector = TensorType("float32", shape=(None,))
dvector = TensorType("float64", shape=(None,))
bvector = TensorType("int8", shape=(None,))
wvector = TensorType("int16", shape=(None,))
ivector = TensorType("int32", shape=(None,))
lvector = TensorType("int64", shape=(None,))


def _validate_static_shape(shape, ndim: int) -> tuple[ST, ...]:
    if not isinstance(shape, tuple):
        raise TypeError(f"Shape must be a tuple, got {type(shape)}")

    if len(shape) != ndim:
        raise ValueError(f"Shape must be a tuple of length {ndim}, got {shape}")

    if not all(sh is None or isinstance(sh, int) for sh in shape):
        raise TypeError(f"Shape entries must be None or integer, got {shape}")

    return shape


def vector(
    name: str | None = None,
    *,
    dtype: Optional["DTypeLike"] = None,
    shape: tuple[ST] | None = (None,),
) -> "TensorVariable":
    """Return a symbolic vector variable.

    Parameters
    ----------
    name
        A name to attach to this variable
    shape
        A tuple of static sizes for each dimension of the variable. By default, each dimension length is `None` which
        allows that dimension to change size across evaluations.
    dtype
        Data type of tensor variable. By default, it's pytensor.config.floatX.

    """
    if dtype is None:
        dtype = config.floatX

    shape = _validate_static_shape(shape, ndim=1)

    type = TensorType(dtype, shape=shape)
    return type(name)


vectors, fvectors, dvectors, ivectors, lvectors = apply_across_args(
    vector, fvector, dvector, ivector, lvector
)

int_vector_types = bvector, wvector, ivector, lvector
float_vector_types = fvector, dvector
complex_vector_types = cvector, zvector

cmatrix = TensorType("complex64", shape=(None, None))
zmatrix = TensorType("complex128", shape=(None, None))
fmatrix = TensorType("float32", shape=(None, None))
dmatrix = TensorType("float64", shape=(None, None))
bmatrix = TensorType("int8", shape=(None, None))
wmatrix = TensorType("int16", shape=(None, None))
imatrix = TensorType("int32", shape=(None, None))
lmatrix = TensorType("int64", shape=(None, None))


def matrix(
    name: str | None = None,
    *,
    dtype: Optional["DTypeLike"] = None,
    shape: tuple[ST, ST] | None = (None, None),
) -> "TensorVariable":
    """Return a symbolic matrix variable.

    Parameters
    ----------
    name
        A name to attach to this variable
    shape
        A tuple of static sizes for each dimension of the variable. By default, each dimension length is `None` which
        allows that dimension to change size across evaluations.
    dtype
        Data type of tensor variable. By default, it's pytensor.config.floatX.

    """
    if dtype is None:
        dtype = config.floatX
    shape = _validate_static_shape(shape, ndim=2)
    type = TensorType(dtype, shape=shape)
    return type(name)


matrices, fmatrices, dmatrices, imatrices, lmatrices = apply_across_args(
    matrix, fmatrix, dmatrix, imatrix, lmatrix
)

int_matrix_types = bmatrix, wmatrix, imatrix, lmatrix
float_matrix_types = fmatrix, dmatrix
complex_matrix_types = cmatrix, zmatrix

crow = TensorType("complex64", shape=(1, None))
zrow = TensorType("complex128", shape=(1, None))
frow = TensorType("float32", shape=(1, None))
drow = TensorType("float64", shape=(1, None))
brow = TensorType("int8", shape=(1, None))
wrow = TensorType("int16", shape=(1, None))
irow = TensorType("int32", shape=(1, None))
lrow = TensorType("int64", shape=(1, None))


def row(
    name: str | None = None,
    *,
    dtype: Optional["DTypeLike"] = None,
    shape: tuple[Literal[1], ST] | None = (1, None),
) -> "TensorVariable":
    """Return a symbolic row variable (i.e. shape ``(1, None)``).

    Parameters
    ----------
    name
        A name to attach to this variable
    shape
        A tuple of static sizes for each dimension of the variable. By default, each dimension length is `None` which
        allows that dimension to change size across evaluations.
    dtype
        Data type of tensor variable. By default, it's pytensor.config.floatX.

    """
    if dtype is None:
        dtype = config.floatX
    shape = _validate_static_shape(shape, ndim=2)

    if shape[0] != 1:
        raise ValueError(
            f"The first dimension of a `row` must have shape 1, got {shape[0]}"
        )

    type = TensorType(dtype, shape=shape)
    return type(name)


rows, frows, drows, irows, lrows = apply_across_args(row, frow, drow, irow, lrow)

ccol = TensorType("complex64", shape=(None, 1))
zcol = TensorType("complex128", shape=(None, 1))
fcol = TensorType("float32", shape=(None, 1))
dcol = TensorType("float64", shape=(None, 1))
bcol = TensorType("int8", shape=(None, 1))
wcol = TensorType("int16", shape=(None, 1))
icol = TensorType("int32", shape=(None, 1))
lcol = TensorType("int64", shape=(None, 1))


def col(
    name: str | None = None,
    *,
    dtype: Optional["DTypeLike"] = None,
    shape: tuple[ST, Literal[1]] | None = (None, 1),
) -> "TensorVariable":
    """Return a symbolic column variable (i.e. shape ``(None, 1)``).

    Parameters
    ----------
    name
        A name to attach to this variable
    shape
        A tuple of static sizes for each dimension of the variable. By default, each dimension length is `None` which
        allows that dimension to change size across evaluations.
    dtype
        Data type of tensor variable. By default, it's pytensor.config.floatX.

    """
    if dtype is None:
        dtype = config.floatX
    shape = _validate_static_shape(shape, ndim=2)
    if shape[1] != 1:
        raise ValueError(
            f"The second dimension of a `col` must have shape 1, got {shape[1]}"
        )
    type = TensorType(dtype, shape=shape)
    return type(name)


cols, fcols, dcols, icols, lcols = apply_across_args(col, fcol, dcol, icol, lcol)

ctensor3 = TensorType("complex64", shape=((None,) * 3))
ztensor3 = TensorType("complex128", shape=((None,) * 3))
ftensor3 = TensorType("float32", shape=((None,) * 3))
dtensor3 = TensorType("float64", shape=((None,) * 3))
btensor3 = TensorType("int8", shape=((None,) * 3))
wtensor3 = TensorType("int16", shape=((None,) * 3))
itensor3 = TensorType("int32", shape=((None,) * 3))
ltensor3 = TensorType("int64", shape=((None,) * 3))


def tensor3(
    name: str | None = None,
    *,
    dtype: Optional["DTypeLike"] = None,
    shape: tuple[ST, ST, ST] | None = (None, None, None),
) -> "TensorVariable":
    """Return a symbolic 3D variable.

    Parameters
    ----------
    name
        A name to attach to this variable
    shape
        A tuple of static sizes for each dimension of the variable. By default, each dimension length is `None` which
        allows that dimension to change size across evaluations.
    dtype
        Data type of tensor variable. By default, it's pytensor.config.floatX.

    """
    if dtype is None:
        dtype = config.floatX
    shape = _validate_static_shape(shape, ndim=3)
    type = TensorType(dtype, shape=shape)
    return type(name)


tensor3s, ftensor3s, dtensor3s, itensor3s, ltensor3s = apply_across_args(
    tensor3, ftensor3, dtensor3, itensor3, ltensor3
)

ctensor4 = TensorType("complex64", shape=((None,) * 4))
ztensor4 = TensorType("complex128", shape=((None,) * 4))
ftensor4 = TensorType("float32", shape=((None,) * 4))
dtensor4 = TensorType("float64", shape=((None,) * 4))
btensor4 = TensorType("int8", shape=((None,) * 4))
wtensor4 = TensorType("int16", shape=((None,) * 4))
itensor4 = TensorType("int32", shape=((None,) * 4))
ltensor4 = TensorType("int64", shape=((None,) * 4))


def tensor4(
    name: str | None = None,
    *,
    dtype: Optional["DTypeLike"] = None,
    shape: tuple[ST, ST, ST, ST] | None = (None, None, None, None),
) -> "TensorVariable":
    """Return a symbolic 4D variable.

    Parameters
    ----------
    name
        A name to attach to this variable
    shape
        A tuple of static sizes for each dimension of the variable. By default, each dimension length is `None` which
        allows that dimension to change size across evaluations.
    dtype
        Data type of tensor variable. By default, it's pytensor.config.floatX.

    """
    if dtype is None:
        dtype = config.floatX
    shape = _validate_static_shape(shape, ndim=4)
    type = TensorType(dtype, shape=shape)
    return type(name)


tensor4s, ftensor4s, dtensor4s, itensor4s, ltensor4s = apply_across_args(
    tensor4, ftensor4, dtensor4, itensor4, ltensor4
)

ctensor5 = TensorType("complex64", shape=((None,) * 5))
ztensor5 = TensorType("complex128", shape=((None,) * 5))
ftensor5 = TensorType("float32", shape=((None,) * 5))
dtensor5 = TensorType("float64", shape=((None,) * 5))
btensor5 = TensorType("int8", shape=((None,) * 5))
wtensor5 = TensorType("int16", shape=((None,) * 5))
itensor5 = TensorType("int32", shape=((None,) * 5))
ltensor5 = TensorType("int64", shape=((None,) * 5))


def tensor5(
    name: str | None = None,
    *,
    dtype: Optional["DTypeLike"] = None,
    shape: tuple[ST, ST, ST, ST, ST] | None = (None, None, None, None, None),
) -> "TensorVariable":
    """Return a symbolic 5D variable.

    Parameters
    ----------
    name
        A name to attach to this variable
    shape
        A tuple of static sizes for each dimension of the variable. By default, each dimension length is `None` which
        allows that dimension to change size across evaluations.
    dtype
        Data type of tensor variable. By default, it's pytensor.config.floatX.

    """
    if dtype is None:
        dtype = config.floatX
    shape = _validate_static_shape(shape, ndim=5)
    type = TensorType(dtype, shape=shape)
    return type(name)


tensor5s, ftensor5s, dtensor5s, itensor5s, ltensor5s = apply_across_args(
    tensor5, ftensor5, dtensor5, itensor5, ltensor5
)

ctensor6 = TensorType("complex64", shape=((None,) * 6))
ztensor6 = TensorType("complex128", shape=((None,) * 6))
ftensor6 = TensorType("float32", shape=((None,) * 6))
dtensor6 = TensorType("float64", shape=((None,) * 6))
btensor6 = TensorType("int8", shape=((None,) * 6))
wtensor6 = TensorType("int16", shape=((None,) * 6))
itensor6 = TensorType("int32", shape=((None,) * 6))
ltensor6 = TensorType("int64", shape=((None,) * 6))


def tensor6(
    name: str | None = None,
    *,
    dtype: Optional["DTypeLike"] = None,
    shape: tuple[ST, ST, ST, ST, ST, ST] | None = (
        None,
        None,
        None,
        None,
        None,
        None,
    ),
) -> "TensorVariable":
    """Return a symbolic 6D variable.

    Parameters
    ----------
    name
        A name to attach to this variable
    shape
        A tuple of static sizes for each dimension of the variable. By default, each dimension length is `None` which
        allows that dimension to change size across evaluations.
    dtype
        Data type of tensor variable. By default, it's pytensor.config.floatX.

    """
    if dtype is None:
        dtype = config.floatX
    shape = _validate_static_shape(shape, ndim=6)
    type = TensorType(dtype, shape=shape)
    return type(name)


tensor6s, ftensor6s, dtensor6s, itensor6s, ltensor6s = apply_across_args(
    tensor6, ftensor6, dtensor6, itensor6, ltensor6
)

ctensor7 = TensorType("complex64", shape=((None,) * 7))
ztensor7 = TensorType("complex128", shape=((None,) * 7))
ftensor7 = TensorType("float32", shape=((None,) * 7))
dtensor7 = TensorType("float64", shape=((None,) * 7))
btensor7 = TensorType("int8", shape=((None,) * 7))
wtensor7 = TensorType("int16", shape=((None,) * 7))
itensor7 = TensorType("int32", shape=((None,) * 7))
ltensor7 = TensorType("int64", shape=((None,) * 7))


def tensor7(
    name: str | None = None,
    *,
    dtype: Optional["DTypeLike"] = None,
    shape: tuple[ST, ST, ST, ST, ST, ST, ST] | None = (
        None,
        None,
        None,
        None,
        None,
        None,
        None,
    ),
) -> "TensorVariable":
    """Return a symbolic 7-D variable.

    Parameters
    ----------
    name
        A name to attach to this variable
    shape
        A tuple of static sizes for each dimension of the variable. By default, each dimension length is `None` which
        allows that dimension to change size across evaluations.
    dtype
        Data type of tensor variable. By default, it's pytensor.config.floatX.

    """
    if dtype is None:
        dtype = config.floatX
    shape = _validate_static_shape(shape, ndim=7)
    type = TensorType(dtype, shape=shape)
    return type(name)


tensor7s, ftensor7s, dtensor7s, itensor7s, ltensor7s = apply_across_args(
    tensor7, ftensor7, dtensor7, itensor7, ltensor7
)


__all__ = [
    "TensorType",
    "bcol",
    "bmatrix",
    "brow",
    "bscalar",
    "btensor3",
    "btensor4",
    "btensor5",
    "btensor6",
    "btensor7",
    "bvector",
    "ccol",
    "cmatrix",
    "col",
    "cols",
    "complex_matrix_types",
    "complex_scalar_types",
    "complex_types",
    "complex_vector_types",
    "crow",
    "cscalar",
    "ctensor3",
    "ctensor4",
    "ctensor5",
    "ctensor6",
    "ctensor7",
    "cvector",
    "dcol",
    "dcols",
    "dmatrices",
    "dmatrix",
    "drow",
    "drows",
    "dscalar",
    "dscalars",
    "dtensor3",
    "dtensor3s",
    "dtensor4",
    "dtensor4s",
    "dtensor5",
    "dtensor5s",
    "dtensor6",
    "dtensor6s",
    "dtensor7",
    "dtensor7s",
    "dvector",
    "dvectors",
    "fcol",
    "fcols",
    "float_matrix_types",
    "float_scalar_types",
    "float_types",
    "float_vector_types",
    "fmatrices",
    "fmatrix",
    "frow",
    "frows",
    "fscalar",
    "fscalars",
    "ftensor3",
    "ftensor3s",
    "ftensor4",
    "ftensor4s",
    "ftensor5",
    "ftensor5s",
    "ftensor6",
    "ftensor6s",
    "ftensor7",
    "ftensor7s",
    "fvector",
    "fvectors",
    "icol",
    "icols",
    "imatrices",
    "imatrix",
    "int_matrix_types",
    "int_scalar_types",
    "int_types",
    "int_vector_types",
    "irow",
    "irows",
    "iscalar",
    "iscalars",
    "itensor3",
    "itensor3s",
    "itensor4",
    "itensor4s",
    "itensor5",
    "itensor5s",
    "itensor6",
    "itensor6s",
    "itensor7",
    "itensor7s",
    "ivector",
    "ivectors",
    "lcol",
    "lcols",
    "lmatrices",
    "lmatrix",
    "lrow",
    "lrows",
    "lscalar",
    "lscalars",
    "ltensor3",
    "ltensor3s",
    "ltensor4",
    "ltensor4s",
    "ltensor5",
    "ltensor5s",
    "ltensor6",
    "ltensor6s",
    "ltensor7",
    "ltensor7s",
    "lvector",
    "lvectors",
    "matrices",
    "matrix",
    "row",
    "rows",
    "scalar",
    "scalars",
    "tensor",
    "tensor3",
    "tensor3s",
    "tensor4",
    "tensor4s",
    "tensor5",
    "tensor5s",
    "tensor6",
    "tensor6s",
    "tensor7",
    "tensor7s",
    "values_eq_approx_always_true",
    "vector",
    "vectors",
    "wcol",
    "wrow",
    "wscalar",
    "wtensor3",
    "wtensor4",
    "wtensor5",
    "wtensor6",
    "wtensor7",
    "wvector",
    "zcol",
    "zmatrix",
    "zrow",
    "zscalar",
    "ztensor3",
    "ztensor4",
    "ztensor5",
    "ztensor6",
    "ztensor7",
    "zvector",
]
