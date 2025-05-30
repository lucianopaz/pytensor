"""Objects that orchestrate graph construction, rewriting, and linking."""

import copy
import copyreg
import logging
import time
import warnings
from collections.abc import Sequence
from itertools import chain
from typing import TYPE_CHECKING

import numpy as np

import pytensor
import pytensor.compile.profiling
from pytensor.compile.io import In, SymbolicInput, SymbolicOutput
from pytensor.compile.ops import deep_copy_op, view_op
from pytensor.compile.profiling import ProfileStats
from pytensor.configdefaults import config
from pytensor.graph.basic import (
    Constant,
    Variable,
    ancestors,
    clone_get_equiv,
    graph_inputs,
)
from pytensor.graph.destroyhandler import DestroyHandler
from pytensor.graph.features import AlreadyThere, Feature, PreserveVariableAttributes
from pytensor.graph.fg import FunctionGraph
from pytensor.graph.op import HasInnerGraph
from pytensor.graph.utils import InconsistencyError, get_variable_trace_string
from pytensor.link.basic import Container
from pytensor.link.utils import raise_with_op


if TYPE_CHECKING:
    from pytensor.compile.mode import Mode
    from pytensor.link.vm import VM


_logger = logging.getLogger("pytensor.compile.function.types")


class UnusedInputError(Exception):
    """
    A symbolic input passed to function is not needed.

    """


def alias_root(v):
    """
    Return the variable to which v is aliased by view_maps and destroy_maps.

    """
    if v.owner is None:
        return v
    vmap = v.owner.op.view_map
    dmap = v.owner.op.destroy_map
    outpos = v.owner.outputs.index(v)
    v_views = vmap.get(outpos, []) + dmap.get(outpos, [])
    if len(v_views) > 1:
        raise NotImplementedError(
            f"{v} is a view/destroyed version of more then one inputs. "
            "Currently, we only support the case where an output is a view or "
            "a destroyed version of one input."
        )
    elif v_views:
        return alias_root(v.owner.inputs[v_views[0]])
    else:
        return v


def view_tree_set(fgraph, v, treeset):
    """
    Add to `treeset` all variables that are views of v, given that v is
    not a view.

    """
    treeset.add(v)
    for cl, v_input_pos_to_cl in fgraph.clients[v]:
        vmap = cl.op.view_map
        dmap = cl.op.destroy_map
        for opos, iposlist in chain(vmap.items(), dmap.items()):
            if v_input_pos_to_cl in iposlist:
                if cl.outputs[opos] not in treeset:
                    view_tree_set(fgraph, cl.outputs[opos], treeset)


def infer_reuse_pattern(fgraph, outputs_to_disown):
    """
    Given an fgraph and a list of variables, returns the list or set
    of all variables which may share the same underlying data storage
    as any of the specified variables. Used internally by function,
    FunctionMaker.

    This list (or set) is also referred to as no_recycling sometimes,
    especially by linker code.

    """
    rval = set()
    for o in outputs_to_disown:
        view_tree_set(fgraph, alias_root(o), rval)
    # remove from rval all of the inputs, constants, values.
    rval = {r for r in rval if r.owner is not None}

    return rval


def fgraph_updated_vars(fgraph, expanded_inputs):
    """
    Reconstruct the full "updates" dictionary, mapping from FunctionGraph input
    variables to the fgraph outputs that will replace their values.

    TODO: Get rid of all this `expanded_inputs` nonsense and use
    only `fgraph.update_mapping`.

    Returns
    -------
    dict variable -> variable

    """
    updated_vars = {}

    if len(expanded_inputs) != len(fgraph.inputs):
        raise ValueError("expanded_inputs must match len(fgraph.inputs)")

    for out_idx, in_idx in fgraph.update_mapping.items():
        assert expanded_inputs[in_idx].update is not None
        updated_vars[fgraph.inputs[in_idx]] = fgraph.outputs[out_idx]

    return updated_vars


class Supervisor(Feature):
    """
    Listener for FunctionGraph events which makes sure that no
    operation overwrites the contents of protected Variables. The
    outputs of the FunctionGraph are protected by default.

    """

    def __init__(self, protected):
        self.fgraph = None
        self.protected = list(protected)

    def clone(self):
        return type(self)(self.protected)

    def on_attach(self, fgraph):
        if hasattr(fgraph, "_supervisor"):
            raise AlreadyThere(f"A Supervisor is already attached to {fgraph}.")

        if self.fgraph is not None and self.fgraph != fgraph:
            raise Exception("This Feature is already associated with a FunctionGraph")

        fgraph._supervisor = self
        self.fgraph = fgraph

    def validate(self, fgraph):
        if config.cycle_detection == "fast" and hasattr(fgraph, "has_destroyers"):
            if fgraph.has_destroyers(self.protected):
                raise InconsistencyError("Trying to destroy protected variables.")
            return True
        if not hasattr(fgraph, "destroyers"):
            return True
        for r in self.protected + list(fgraph.outputs):
            if fgraph.destroyers(r):
                raise InconsistencyError(f"Trying to destroy a protected variable: {r}")


def add_supervisor_to_fgraph(
    fgraph: FunctionGraph,
    input_specs: Sequence[SymbolicInput],
    accept_inplace: bool = False,
) -> None:
    """Setup Supervisor Feature in a FunctionGraph, so that inplace rewrites can be used.

    Parameters
    ----------
    fgraph: FunctionGraph
        The FunctionGraph to setup the Supervisor Feature in.
    input_specs: Sequence of SymbolicInput
        The input specifications for the FunctionGraph.
        Inputs with the attribute `mutable=False` and which are not already destroyed by an inplace operation
        (if `accept_inplace` is True) will be protected from inplace operations.
        Otherwise, they will be allowed to be destroyed.
    accept_inplace: bool
        Whether to allow inplace operations to already be present in the graph.

    Raises
    ------
    TypeError
        If inplace operations are not allowed and the graph already contains inplace operations.

    """

    has_destroy_handler = hasattr(fgraph, "destroyers")
    if not (has_destroy_handler and accept_inplace):
        # Check if fgraph already contains destructive operations,
        # in which case we need to add a DestroyHandler or raise an error
        for node in fgraph.apply_nodes:
            if node.op.destroy_map:
                if not accept_inplace:
                    raise TypeError(
                        f"Graph must not contain inplace operations: {node}"
                    )
                else:
                    has_destroy_handler = True
                    fgraph.attach_feature(DestroyHandler())
                    break

    # Protect all immutable inputs from inplace operations.
    fgraph.attach_feature(
        Supervisor(
            input
            for spec, input in zip(input_specs, fgraph.inputs, strict=True)
            if not (
                spec.mutable or has_destroy_handler and fgraph.has_destroyers([input])
            )
        )
    )


def std_fgraph(
    input_specs: list[SymbolicInput],
    output_specs: list[SymbolicOutput],
    accept_inplace: bool = False,
    fgraph: FunctionGraph | None = None,
    features: list[type[Feature]] = [PreserveVariableAttributes],
    force_clone=False,
) -> tuple[FunctionGraph, list[SymbolicOutput]]:
    """Make or set up `FunctionGraph` corresponding to the input specs and the output specs.

    Any `SymbolicInput` in the `input_specs`, if its `update` field is not
    ``None``, will add an output corresponding to that update to the
    `FunctionGraph`. The return value is the `FunctionGraph` as well as a list
    of `SymbolicOutput` instances corresponding to the updates.

    If `accept_inplace` is ``False``, the graph will be checked for in-place
    operations and an exception will be raised if it has any. If
    `accept_inplace` is ``True``, a `DestroyHandler` will be added to the
    `FunctionGraph` if there are any in-place operations.

    If `fgraph` is ``None``, the returned `FunctionGraph` is a clone of the
    graph between the provided inputs and outputs.

    """
    # Extract the updates and the mapping between update outputs and the
    # updated inputs
    updates = []
    update_mapping = {}
    out_idx = len(output_specs)
    for idx, input_spec in enumerate(input_specs):
        if input_spec.update is not None:
            updates.append(input_spec.update)
            update_mapping[out_idx] = idx
            out_idx += 1

    found_updates = []
    if fgraph and fgraph.update_mapping is None:
        fgraph.update_mapping = update_mapping
        for update in updates:
            fgraph.add_output(update, reason="std_fgraph")

        found_updates.extend(map(SymbolicOutput, updates))
    elif fgraph is None:
        # If one of the inputs is non-atomic (i.e. has a non-`None` `Variable.owner`),
        # then we need to create/clone the graph starting at these inputs.
        # The result will be atomic versions of the given inputs connected to
        # the same outputs.
        # Otherwise, when all the inputs are already atomic, there's no need to
        # clone the graph.
        input_vars = [spec.variable for spec in input_specs]
        clone = force_clone or any(var.owner is not None for var in input_vars)

        fgraph = FunctionGraph(
            input_vars,
            [spec.variable for spec in output_specs] + updates,
            update_mapping=update_mapping,
            clone=clone,
        )

        found_updates.extend(map(SymbolicOutput, updates))

    add_supervisor_to_fgraph(
        fgraph=fgraph, input_specs=input_specs, accept_inplace=accept_inplace
    )

    # If named nodes are replaced, keep the name
    for feature in features:
        fgraph.attach_feature(feature())

    return fgraph, found_updates


class AliasedMemoryError(Exception):
    """
    Memory is aliased that should not be.

    """


# A sentinel for duplicate entries
DUPLICATE = object()


class Function:
    r"""A class that wraps the execution of a `VM` making it easier for use as a "function".

    `Function` is the callable object that does computation.  It has the storage
    of inputs and outputs, performs the packing and unpacking of inputs and
    return values. It implements the square-bracket indexing so that you can
    look up the value of a symbolic node.

    Functions are copyable via `Function.copy` and the `copy.copy` interface.
    When a function is copied, this instance is duplicated. Contrast with
    self.maker (instance of `FunctionMaker`) that is shared between copies.
    The meaning of copying a function is that the containers and their current
    values will all be duplicated. This requires that mutable inputs be
    copied, whereas immutable inputs may be shared between copies.

    A Function instance is hashable, on the basis of its memory address (its
    id).
    A Function instance is only equal to itself.
    A Function instance may be serialized using the `pickle` or
    `cPickle` modules.  This will save all default inputs, the graph,
    and WRITEME to the pickle file.

    A `Function` instance has a `Function.trust_input` field that defaults to
    ``False``. When ``True``, the `Function` will skip all checks on the
    inputs.

    Attributes
    ----------
    finder
        Dictionary mapping several kinds of things to containers.

        We set an entry in finder for:
        - the index of the input
        - the variable instance the input is based on
        - the name of the input

        All entries map to the container or to DUPLICATE if an ambiguity
        is detected.
    inv_finder
        Reverse lookup of `finder`.  It maps containers to `SymbolicInput`\s.

    """

    pickle_aliased_memory_strategy = "warn"
    """
    How to deal with pickling finding aliased storage.

    Meaningful settings are: 'ignore', 'warn', 'raise'.

    If the value is 'warn', then a message will be printed to stderr
    if aliased storage is detected during pickle.dump.

    If the value is 'raise', then an AliasedMemoryError will be raised
    if aliased storage is detected during pickle.dump.
    """

    def __init__(
        self,
        vm: "VM",
        input_storage: list[Container],
        output_storage: list[Container],
        indices,
        outputs,
        defaults,
        unpack_single: bool,
        return_none: bool,
        output_keys,
        maker: "FunctionMaker",
        trust_input: bool = False,
        name: str | None = None,
    ):
        """
        Parameters
        ----------
        vm
            A `VM` instance that evaluates the graph when called.
        input_storage
            List of storage cells for each input.
        output_storage
            List of storage cells for each output.
        indices
            List of ``(SymbolicInput, indices, [SymbolicInput,...])``, one
            tuple for each input.  The first tuple element is the `SymbolicInput`
            object for the corresponding function input.  The second and third
            tuple elements are used only by Kits, which are deprecated.
        outputs
            TODO
        defaults
            List of 3-tuples, one 3-tuple for each input.
            Tuple element 0: ``bool``.  Is this input required at each function
            call?
            Tuple element 1: ``bool``.  Should this inputs value be reverted
            after each call?
            Tuple element 2: ``Any``.  The value associated with this input.
        unpack_single
            For outputs lists of length 1, should the 0'th element be
            returned directly?
        return_none
            Whether the function should return ``None`` or not.
        output_keys
            TODO
        maker
            The `FunctionMaker` that created this instance.
        trust_input : bool, default False
            If True, no input validation checks are performed when the function is
            called. This includes checking the number of inputs, their types and
            that multiple inputs are not aliased to each other. Failure to meet any
            of these conditions can lead to computational errors or to the
            interpreter crashing.
        name
            A string name.
        """
        self.vm = vm
        self.input_storage = input_storage
        self.output_storage = output_storage
        self.indices = indices
        self.outputs = outputs
        self.defaults = defaults
        self.unpack_single = unpack_single
        self.return_none = return_none
        self.maker = maker
        self.profile = None  # reassigned in FunctionMaker.create
        self.trust_input = trust_input  # If True, we don't check the input parameter
        self.name = name
        self.nodes_with_inner_function = []
        self.output_keys = output_keys

        if self.output_keys is not None:
            warnings.warn("output_keys is deprecated.", FutureWarning)

        assert len(self.input_storage) == len(self.maker.fgraph.inputs)
        assert len(self.output_storage) == len(self.maker.fgraph.outputs)

        self.has_defaults = any(refeed for _, refeed, _ in self.defaults)

        # Group indexes of inputs that are potentially aliased to each other
        # Note: Historically, we only worried about aliasing inputs if they belonged to the same type,
        #  even though there could be two distinct types that use the same kinds of underlying objects.
        potential_aliased_input_groups = []
        for inp in maker.inputs:
            # If the input is a shared variable, the memory region is under PyTensor control
            # and can't be aliased.
            if not (
                isinstance(inp, In)
                and inp.borrow
                and not inp.shared
                and hasattr(inp.variable.type, "may_share_memory")
            ):
                continue

            for group in potential_aliased_input_groups:
                # If one is super of the other, that means one could be replaced by the other
                if any(
                    inp.variable.type.is_super(other_inp.variable.type)
                    or other_inp.variable.type.is_super(inp.variable.type)
                    for other_inp in group
                ):
                    group.append(inp)
                    break
            else:  # no break
                # Input makes a new group
                potential_aliased_input_groups.append([inp])

        # Potential aliased inputs are those that belong to the same group
        self._potential_aliased_input_groups: tuple[tuple[int, ...], ...] = tuple(
            tuple(maker.inputs.index(inp) for inp in group)
            for group in potential_aliased_input_groups
            if len(group) > 1
        )

        # We will be popping stuff off this `containers` object.  It is a copy.
        containers = list(self.input_storage)
        finder = {}
        inv_finder = {}

        # Store the list of names of named inputs.
        named_inputs = []
        # Count the number of un-named inputs.
        n_unnamed_inputs = 0

        # Initialize the storage
        # this loop works by modifying the elements (as variable c) of
        # self.input_storage inplace.
        for i, ((input, indices, sinputs), (required, refeed, value)) in enumerate(
            zip(self.indices, defaults, strict=True)
        ):
            if indices is None:
                # containers is being used as a stack. Here we pop off
                # the next one.
                c = containers[0]
                c.strict = getattr(input, "strict", False)
                c.allow_downcast = getattr(input, "allow_downcast", None)

                if value is not None:
                    # Always initialize the storage.
                    if isinstance(value, Container):
                        # There is no point in obtaining the current value
                        # stored in the container, since the container is
                        # shared.
                        # For safety, we make sure 'refeed' is False, since
                        # there is no need to refeed the default value.
                        assert not refeed
                    else:
                        c.value = value
                c.required = required
                c.implicit = input.implicit
                # this is a count of how many times the input has been
                # provided (reinitialized to 0 on __call__)
                c.provided = 0
                finder[i] = c
                finder[input.variable] = c
                if input.name not in finder:
                    finder[input.name] = c
                else:
                    finder[input.name] = DUPLICATE
                if input.name is None:
                    n_unnamed_inputs += 1
                else:
                    named_inputs.append(input.name)
                inv_finder[c] = input
                containers[:1] = []

        self.finder = finder
        self.inv_finder = inv_finder

        # this class is important in overriding the square-bracket notation:
        #     fn.value[x]
        # self reference is available via the closure on the class
        class ValueAttribute:
            def __getitem__(self, item):
                try:
                    s = finder[item]
                except KeyError:
                    raise TypeError(f"Unknown input or state: {item}")
                if s is DUPLICATE:
                    raise TypeError(
                        f"Ambiguous name: {item} - please check the "
                        "names of the inputs of your function "
                        "for duplicates."
                    )
                if isinstance(s, Container):
                    return s.value
                else:
                    raise NotImplementedError

            def __setitem__(self, item, value):
                try:
                    s = finder[item]
                except KeyError:
                    # Print informative error message.
                    msg = get_info_on_inputs(named_inputs, n_unnamed_inputs)
                    raise TypeError(f"Unknown input or state: {item}. {msg}")
                if s is DUPLICATE:
                    raise TypeError(
                        f"Ambiguous name: {item} - please check the "
                        "names of the inputs of your function "
                        "for duplicates."
                    )
                if isinstance(s, Container):
                    s.value = value
                    s.provided += 1
                else:
                    s(value)

            def __contains__(self, item):
                return finder.__contains__(item)

        # this class is important in overriding the square-bracket notation:
        #     fn.container[x]
        # self reference is available via the closure on the class
        class ContainerAttribute:
            def __getitem__(self, item):
                return finder[item]

            def __contains__(self, item):
                return finder.__contains__(item)

            # You cannot set the container

        self._value = ValueAttribute()
        self._container = ContainerAttribute()

        update_storage = [
            container
            for inp, container in zip(
                self.maker.expanded_inputs, input_storage, strict=True
            )
            if inp.update is not None
        ]
        # Updates are the last inner outputs that are not returned by Function.__call__
        self.n_returned_outputs = len(self.output_storage) - len(update_storage)

        # Function.__call__ is responsible for updating the inputs, unless the vm promises to do it itself
        self.update_input_storage: tuple[int, Container] = ()
        if getattr(vm, "need_update_inputs", True):
            self.update_input_storage = tuple(
                zip(
                    range(self.n_returned_outputs, len(output_storage)),
                    update_storage,
                    strict=True,
                )
            )

        # In every function call we place inputs in the input_storage, and the vm places outputs in the output_storage
        # After the call, we want to erase (some of) these references, to allow Python to GC them if unused
        # Required input containers are the non-default inputs, must always be provided again, so we GC them
        self.clear_input_storage_data = tuple(
            container.storage for container in input_storage if container.required
        )
        # This is only done when `vm.allow_gc` is True, which can change at runtime.
        self.clear_output_storage_data = tuple(
            container.storage
            for container, variable in zip(
                self.output_storage, self.maker.fgraph.outputs, strict=True
            )
            if variable.owner is not None  # Not a constant output
        )

        for node in self.maker.fgraph.apply_nodes:
            if isinstance(node.op, HasInnerGraph):
                self.nodes_with_inner_function.append(node.op)

    def __contains__(self, item):
        return self.value.__contains__(item)

    def __getitem__(self, item):
        return self.value[item]

    def __setitem__(self, item, value):
        self.value[item] = value

    def __copy__(self):
        """
        Copy a function. Copied function have separate intermediate
        storages and output storages with original function
        """
        return self.copy()

    def copy(
        self,
        share_memory: bool = False,
        swap: dict | None = None,
        delete_updates: bool = False,
        name: str | None = None,
        profile: bool | str | ProfileStats | None = None,
    ):
        """
        Copy this function. Copied function will have separated maker and
        fgraph with original function. User can choose whether to separate
        storage by changing the share_memory arguments.

        Parameters
        ----------
        share_memory : boolean
            When True, two function share intermediate storages(storages except input and
            output storages). Otherwise two functions will only share partial
            storages and same maker. If two functions share memory and
            allow_gc=False, this will increase executing speed and save memory.

        swap : dict
            Dictionary that map old SharedVariables to new
            SharedVariables. Default is None.
            NOTE: The shared variable swap in only done in the new returned
            function, not in the user graph.

        delete_updates : boolean
            If True, Copied function will not have updates.
        name : string
            If provided, will be the name of the new
            Function. Otherwise, it will be old + " copy"

        profile : bool | str | ProfileStats | None
            as pytensor.function profile parameter

        Returns
        -------
        pytensor.Function
            Copied pytensor.Function
        """

        # helper function
        def checkSV(sv_ori, sv_rpl):
            """
            Assert two SharedVariable follow some restrictions:
                1. same type
                2. same shape or dim?
            """
            SharedVariable = pytensor.tensor.sharedvar.SharedVariable
            assert isinstance(sv_ori, SharedVariable), (
                "Key of swap should be SharedVariable, given:",
                sv_ori,
                " type",
                type(sv_ori),
            )
            assert isinstance(sv_rpl, SharedVariable), (
                "Value of swap should be SharedVariable, given:",
                sv_rpl,
                "type",
                type(sv_ori),
            )
            assert sv_rpl.type.in_same_class(sv_ori.type), (
                "Type of given SharedVariable conflicts with original one",
                "Type of given SharedVariable:",
                sv_rpl.type,
                "Type of original SharedVariable:",
                sv_ori.type,
            )

        maker = self.maker

        # Copy Ins and their storage.
        # so that they have different storage as their value
        ins = [copy.copy(input) for input in maker.inputs]

        # Delete update output in fgraph and updates In instances if needed
        if delete_updates:
            # The first len(maker.outputs) variables are original variables.
            # The rest are the updates.
            out_vars = maker.fgraph.outputs[: len(maker.outputs)]
        else:
            out_vars = maker.fgraph.outputs

        # Init new fgraph using copied variables and get memo
        # memo: a dict that map old variables to new variables
        memo = clone_get_equiv(maker.fgraph.inputs, out_vars)
        fg_cpy = FunctionGraph(
            [memo[i] for i in maker.fgraph.inputs],
            [memo[o] for o in out_vars],
            clone=False,
        )
        fg_cpy.update_mapping = maker.fgraph.update_mapping

        # Re initialize Outs and swap update and variable in Ins
        # By doing this, we can pass FunctionMaker.check_unused_inputs()
        if delete_updates:
            outs = list(map(SymbolicOutput, fg_cpy.outputs[: len(maker.outputs)]))
        else:
            outs = list(map(SymbolicOutput, fg_cpy.outputs))

        for out_ori, out_cpy in zip(maker.outputs, outs, strict=False):
            out_cpy.borrow = out_ori.borrow

        # swap SharedVariable
        if swap is not None:
            exist_svs = [i.variable for i in maker.inputs]

            # Check if given ShareVariables exist
            for sv in swap:
                if sv not in exist_svs:
                    raise ValueError(f"SharedVariable: {sv.name} not found")

            # Swap SharedVariable in fgraph and In instances
            for index, (i, in_v) in enumerate(zip(ins, fg_cpy.inputs, strict=True)):
                # Variables in maker.inputs are defined by user, therefore we
                # use them to make comparison and do the mapping.
                # Otherwise we don't touch them.
                var = maker.inputs[index].variable

                if var in swap:
                    swap_sv = swap[var]
                    checkSV(i.variable, swap_sv)

                    # swap variable and value of In instances
                    i.variable = swap_sv
                    i.value = swap_sv.container

                    # In the fgraph we use the cloned SharedVariable
                    swap_sv = swap_sv.clone()

                    # Swap SharedVariable in fgraph
                    # if inputs was replaced, change self.inputs
                    fg_cpy.inputs[index] = swap_sv
                    fg_cpy.replace(in_v, swap_sv, reason="Swap SV")

        # Delete update if needed
        rev_update_mapping = {v: k for k, v in fg_cpy.update_mapping.items()}
        for n, (inp, in_var) in enumerate(zip(ins, fg_cpy.inputs, strict=True)):
            inp.variable = in_var
            if not delete_updates and inp.update is not None:
                out_idx = rev_update_mapping[n]
                inp.update = fg_cpy.outputs[out_idx]
            else:
                inp.update = None

        if delete_updates:
            fg_cpy.update_mapping = {}

        # Construct new storage_map that map new variable to old storage,
        # so that the ensuing function shares storage with the original one
        storage_map = self.vm.storage_map
        new_storage_map = {}
        # TODO: We could share the output storage, but we must make sure
        # 2 different function call won't override each other values. This
        # is already done elsewhere, so to reuse it the user would need to
        # use Out(var, borrow=True) and maybe the mutable=True flag too.
        # But to be safe for now as it isn't documented and we aren't sure
        # it is well tested, we don't share the part of the storage_map.
        if share_memory:
            i_o_vars = maker.fgraph.inputs + maker.fgraph.outputs
            for key, val in storage_map.items():
                if key not in i_o_vars:
                    new_storage_map[memo[key]] = val

        if not name and self.name:
            name = self.name + " copy"

        input_storage = [i.value for i in ins]
        # reinitialize new maker and create new function
        if profile is None:
            profile = config.profile or config.print_global_stats
        if profile is True:
            profile = pytensor.compile.profiling.ProfileStats(message=name)
        elif isinstance(profile, str):
            profile = pytensor.compile.profiling.ProfileStats(message=profile)

        f_cpy = type(maker)(
            inputs=ins,
            outputs=outs,
            fgraph=fg_cpy,
            mode=maker.mode,
            profile=profile,
            # When removing updates containing variables
            # not used in the output function, copy
            # generates an unused implicit input.
            # We ignore the resulting errors,
            # but could change it to 'warn' if this might
            # cause problems.
            on_unused_input="ignore",
            function_builder=maker.function_builder,
            # As this is an rewritten graph, it can contain inplace. DebugMode
            # check that.
            accept_inplace=True,
            no_fgraph_prep=True,
            output_keys=maker.output_keys,
            name=name,
        ).create(input_storage, storage_map=new_storage_map)

        for in_ori, in_cpy, ori, cpy in zip(
            maker.inputs,
            f_cpy.maker.inputs,
            self.input_storage,
            f_cpy.input_storage,
            strict=True,
        ):
            # Share immutable ShareVariable and constant input's storage
            swapped = swap is not None and in_ori.variable in swap

            # Using the original storage if SharedVariable will not be updated
            # and is not swapped
            if not in_ori.mutable and not swapped:
                cpy.data = ori.data
                in_cpy.value = in_ori.value

            # Reconstruct Function.finder which map Variable defined by user
            # to container, to make Function.value and Function.data work well.
            # Replace variable in new maker.inputs by the original ones.
            # So that user can swap SharedVariable in a swapped function
            container = f_cpy.finder.pop(in_cpy.variable)
            if not swapped:
                f_cpy.finder[in_ori.variable] = container
                in_cpy.variable = in_ori.variable
            else:
                f_cpy.finder[swap[in_ori.variable]] = container
                in_cpy.variable = swap[in_ori.variable]

        f_cpy.trust_input = self.trust_input
        f_cpy.unpack_single = self.unpack_single
        return f_cpy

    def _restore_defaults(self):
        for i, (required, refeed, value) in enumerate(self.defaults):
            if refeed:
                if isinstance(value, Container):
                    value = value.storage[0]
                self[i] = value

    def __call__(self, *args, output_subset=None, **kwargs):
        """
        Evaluates value of a function on given arguments.

        Parameters
        ----------
        args : list
            List of inputs to the function. All inputs are required, even when
            some of them are not necessary to calculate requested subset of
            outputs.

        kwargs : dict
            The function inputs can be passed as keyword argument. For this, use
            the name of the input or the input instance as the key.

            Keyword argument ``output_subset`` is a list of either indices of the
            function's outputs or the keys belonging to the `output_keys` dict
            and represent outputs that are requested to be calculated. Regardless
            of the presence of ``output_subset``, the updates are always calculated
            and processed. To disable the updates, you should use the ``copy``
            method with ``delete_updates=True``.

        Returns
        -------
        list
            List of outputs on indices/keys from ``output_subset`` or all of them,
            if ``output_subset`` is not passed.
        """
        trust_input = self.trust_input
        input_storage = self.input_storage
        vm = self.vm
        profile = self.profile

        if profile:
            t0 = time.perf_counter()

        if output_subset is not None:
            warnings.warn("output_subset is deprecated.", FutureWarning)
            if self.output_keys is not None:
                output_subset = [self.output_keys.index(key) for key in output_subset]

        # Reinitialize each container's 'provided' counter
        if trust_input:
            # zip strict not specified because we are in a hot loop
            for arg_container, arg in zip(input_storage, args):
                arg_container.storage[0] = arg
        else:
            for arg_container in input_storage:
                arg_container.provided = 0

            if len(args) + len(kwargs) > len(input_storage):
                raise TypeError("Too many parameter passed to pytensor function")

            # Set positional arguments
            # zip strict not specified because we are in a hot loop
            for arg_container, arg in zip(input_storage, args):
                # See discussion about None as input
                # https://groups.google.com/group/theano-dev/browse_thread/thread/920a5e904e8a8525/4f1b311a28fc27e5
                if arg is None:
                    arg_container.storage[0] = arg
                else:
                    try:
                        arg_container.storage[0] = arg_container.type.filter(
                            arg,
                            strict=arg_container.strict,
                            allow_downcast=arg_container.allow_downcast,
                        )

                    except Exception as e:
                        i = input_storage.index(arg_container)
                        function_name = "pytensor function"
                        argument_name = "argument"
                        if self.name:
                            function_name += ' with name "' + self.name + '"'
                        if hasattr(arg, "name") and arg.name:
                            argument_name += ' with name "' + arg.name + '"'
                        where = get_variable_trace_string(self.maker.inputs[i].variable)
                        if len(e.args) == 1:
                            e.args = (
                                "Bad input "
                                + argument_name
                                + " to "
                                + function_name
                                + f" at index {int(i)} (0-based). {where}"
                                + e.args[0],
                            )
                        else:
                            e.args = (
                                "Bad input "
                                + argument_name
                                + " to "
                                + function_name
                                + f" at index {int(i)} (0-based). {where}"
                            ) + e.args
                        self._restore_defaults()
                        raise
                arg_container.provided += 1

        # Set keyword arguments
        if kwargs:  # for speed, skip the items for empty kwargs
            for k, arg in kwargs.items():
                self[k] = arg

        if not trust_input:
            # Collect aliased inputs among the storage space
            for potential_group in self._potential_aliased_input_groups:
                args_share_memory: list[list[int]] = []
                for i in potential_group:
                    i_type = self.maker.inputs[i].variable.type
                    i_val = input_storage[i].storage[0]

                    # Check if value is aliased with any of the values in one of the groups
                    for j_group in args_share_memory:
                        if any(
                            i_type.may_share_memory(input_storage[j].storage[0], i_val)
                            for j in j_group
                        ):
                            j_group.append(i)
                            break
                    else:  # no break
                        # Create a new group
                        args_share_memory.append([i])

                # Check for groups of more than one argument that share memory
                for group in args_share_memory:
                    if len(group) > 1:
                        # copy all but the first
                        for i in group[1:]:
                            input_storage[i].storage[0] = copy.copy(
                                input_storage[i].storage[0]
                            )

            # Check if inputs are missing, or if inputs were set more than once, or
            # if we tried to provide inputs that are supposed to be implicit.
            for arg_container in input_storage:
                if arg_container.required and not arg_container.provided:
                    self._restore_defaults()
                    raise TypeError(
                        f"Missing required input: {getattr(self.inv_finder[arg_container], 'variable', self.inv_finder[arg_container])}"
                    )
                if arg_container.provided > 1:
                    self._restore_defaults()
                    raise TypeError(
                        f"Multiple values for input: {getattr(self.inv_finder[arg_container], 'variable', self.inv_finder[arg_container])}"
                    )
                if arg_container.implicit and arg_container.provided > 0:
                    self._restore_defaults()
                    raise TypeError(
                        f"Tried to provide value for implicit input: {getattr(self.inv_finder[arg_container], 'variable', self.inv_finder[arg_container])}"
                    )

        # Do the actual work
        if profile:
            t0_fn = time.perf_counter()
        try:
            outputs = vm() if output_subset is None else vm(output_subset=output_subset)
        except Exception:
            self._restore_defaults()
            if hasattr(self.vm, "position_of_error"):
                # this is a new vm-provided function or c linker
                # they need this because the exception manipulation
                # done by raise_with_op is not implemented in C.
                thunk = None
                if hasattr(self.vm, "thunks"):
                    thunk = self.vm.thunks[self.vm.position_of_error]
                raise_with_op(
                    self.maker.fgraph,
                    node=self.vm.nodes[self.vm.position_of_error],
                    thunk=thunk,
                    storage_map=getattr(self.vm, "storage_map", None),
                )
            else:
                # old-style linkers raise their own exceptions
                raise

        if profile:
            dt_fn = time.perf_counter() - t0_fn
            self.maker.mode.fn_time += dt_fn
            profile.vm_call_time += dt_fn

        # Retrieve the values that were computed
        if outputs is None:
            outputs = [x.storage[0] for x in self.output_storage]

        # Set updates and filter them out from the returned outputs
        for i, input_storage in self.update_input_storage:
            input_storage.storage[0] = outputs[i]
        outputs = outputs[: self.n_returned_outputs]

        # Remove input and output values from storage data
        for storage_data in self.clear_input_storage_data:
            storage_data[0] = None
        if getattr(vm, "allow_gc", False):
            for storage_data in self.clear_output_storage_data:
                storage_data[0] = None

        # Put default values back in the storage
        if self.has_defaults:
            self._restore_defaults()

        if profile:
            dt_call = time.perf_counter() - t0
            pytensor.compile.profiling.total_fct_exec_time += dt_call
            self.maker.mode.call_time += dt_call
            profile.fct_callcount += 1
            profile.fct_call_time += dt_call
            if hasattr(vm, "update_profile"):
                vm.update_profile(profile)
            if profile.ignore_first_call:
                profile.reset()
                profile.ignore_first_call = False

        if self.return_none:
            return None

        if output_subset is not None:
            outputs = [outputs[i] for i in output_subset]

        if self.output_keys is None:
            if self.unpack_single:
                [out] = outputs
                return out
            else:
                return outputs
        else:
            output_keys = self.output_keys
            if output_subset is not None:
                output_keys = [output_keys[i] for i in output_subset]
            return dict(zip(output_keys, outputs, strict=True))

    value = property(
        lambda self: self._value,
        None,  # this property itself is not settable
        doc="dictionary-like access to the values associated with Variables",
    )
    container = property(
        lambda self: self._container,
        None,  # this property itself is not settable
        doc=("dictionary-like access to the containers associated with Variables"),
    )

    def free(self):
        """
        When allow_gc = False, clear the Variables in storage_map
        """
        # 1.no allow_gc return False
        # 2.has allow_gc, if allow_gc is False, return True
        if not getattr(self.vm, "allow_gc", True):
            storage_map = self.vm.storage_map
            for key, value in storage_map.items():
                if key.owner is not None:  # Not a constant
                    value[0] = None

            for node in self.nodes_with_inner_function:
                if hasattr(node.fn, "free"):
                    node.fn.free()

    def get_shared(self):
        """
        Return the shared variable read or updated by by this function.
        """
        return [i.variable for i in self.maker.inputs if i.implicit]

    def dprint(self, **kwargs):
        """Debug print itself

        Parameters
        ----------
        kwargs:
            Optional keyword arguments to pass to debugprint function.
        """
        from pytensor.printing import debugprint

        return debugprint(self, **kwargs)


# pickling/deepcopy support for Function
def _pickle_Function(f):
    # copy of the input storage list
    ins = list(f.input_storage)
    input_storage = []

    # strict=False because we are in a hot loop
    for (input, indices, inputs), (required, refeed, default) in zip(
        f.indices, f.defaults, strict=False
    ):
        input_storage.append(ins[0])
        del ins[0]

    inputs_data = [x.data for x in f.input_storage]

    # HACK to detect aliased storage.
    # This is here because aliased relationships are not [currently]
    # preserved across the pickle operation
    if f.pickle_aliased_memory_strategy != "ignore":
        all_data = input_storage + inputs_data
        for i, d_i in enumerate(all_data):
            for j, d_j in enumerate(all_data):
                if (
                    (i < j)
                    and isinstance(d_i, np.ndarray)
                    and isinstance(d_j, np.ndarray)
                ):
                    if np.may_share_memory(d_i, d_j):
                        if f.pickle_aliased_memory_strategy == "warn":
                            warnings.warn(
                                "aliased relationship between "
                                f"Function arguments {d_i}, {d_j} "
                                "will not be preserved by "
                                "un-pickling operation"
                            )
                        else:
                            raise AliasedMemoryError(d_i, d_j)
    # The user can override trust_input. Our doc tell that.  We should
    # not do that anymore and make sure the Maker have all the
    # information needed.
    rval = (_constructor_Function, (f.maker, input_storage, inputs_data, f.trust_input))
    return rval


def _constructor_Function(maker, input_storage, inputs_data, trust_input=False):
    if not config.unpickle_function:
        return None

    f = maker.create(input_storage)
    assert len(f.input_storage) == len(inputs_data)
    for container, x in zip(f.input_storage, inputs_data, strict=True):
        assert (
            (container.data is x)
            or (isinstance(x, np.ndarray) and (container.data == x).all())
            or (container.data == x)
        )
    f.trust_input = trust_input
    return f


copyreg.pickle(Function, _pickle_Function)


def insert_deepcopy(fgraph, wrapped_inputs, wrapped_outputs):
    """Insert deepcopy in the fgraph to break aliasing of outputs.

    This loop was inserted to remove aliasing between outputs when they all
    evaluate to the same value. Originally it was OK for outputs to be aliased,
    but some of the outputs can be shared variables, and is not good for shared
    variables to be aliased. It might be possible to rewrite this by making
    sure there is no aliasing only between shared variables.

    If some outputs are constant, we add deep copy to respect the memory
    contract

    We don't insert deep copy when :attr:`SymbolicOutput.borrow` is ``True``
    for all concerned outputs.
    """

    assert len(wrapped_inputs) == len(fgraph.inputs)
    assert len(wrapped_outputs) == len(fgraph.outputs)
    reason = "insert_deepcopy"
    updated_fgraph_inputs = {
        fgraph_i
        for i, fgraph_i in zip(wrapped_inputs, fgraph.inputs, strict=True)
        if getattr(i, "update", None) is not None
    }

    # We can't use fgraph.inputs as this don't include Constant Value.
    all_graph_inputs = list(graph_inputs(fgraph.outputs))
    has_destroyers_attr = hasattr(fgraph, "has_destroyers")

    for i in range(len(fgraph.outputs)):
        original_out = fgraph.outputs[i]
        output_client = fgraph.get_output_client(i)

        views_of_output_i = set()
        view_tree_set(fgraph, alias_root(original_out), views_of_output_i)
        copied = False
        # do not allow outputs to be aliased
        for j in range(i + 1, len(fgraph.outputs)):
            # We could don't put deep copy if both outputs have borrow==True
            # and not(wrapped_outputs[i].borrow and wrapped_outputs[j].borrow):
            if fgraph.outputs[j] in views_of_output_i:
                if wrapped_outputs[i].borrow and wrapped_outputs[j].borrow:
                    fgraph.change_node_input(
                        *output_client, view_op(original_out), reason=reason
                    )
                else:
                    fgraph.change_node_input(
                        *output_client, deep_copy_op(original_out), reason=reason
                    )
                copied = True
                break

        if not copied:  # no-break
            for input_j in all_graph_inputs:
                # do not allow outputs to be aliased to an inputs (j), unless
                # a) that j'th input has been 'destroyed' by
                #    e.g. in-place computations
                # b) that j'th input is a shared variable that is also
                #    being updated
                if input_j in updated_fgraph_inputs:
                    continue
                if input_j in views_of_output_i and not (
                    has_destroyers_attr and fgraph.has_destroyers([input_j])
                ):
                    # We don't put deep_copy_op if the input and the
                    # output have borrow==True
                    if input_j in fgraph.inputs:
                        j = fgraph.inputs.index(input_j)
                        if wrapped_outputs[i].borrow and wrapped_inputs[j].borrow:
                            fgraph.change_node_input(
                                *output_client,
                                view_op(original_out),
                                reason=reason,
                            )
                            break
                        else:
                            fgraph.change_node_input(
                                *output_client,
                                deep_copy_op(original_out),
                                reason=reason,
                            )
                            break
                    elif wrapped_outputs[i].borrow:
                        fgraph.change_node_input(
                            *output_client,
                            view_op(original_out),
                            reason=reason,
                        )
                        break
                    else:
                        fgraph.change_node_input(
                            *output_client,
                            deep_copy_op(original_out),
                            reason=reason,
                        )
                        break


class FunctionMaker:
    """
    `FunctionMaker` is the class to `create` `Function` instances.

    This class has the fgraph, the rewriter, and the linker. When
    copying a `Function`, there is no need to duplicate the
    `FunctionMaker` instance. Deepcopy still copies both, which can
    variable in re-compilation.

    Parameters
    ----------
    inputs : list of SymbolicInput instances
    outputs : list of SymbolicOutput instances
        Outputs may also be a single Variable (not a list), in which case the
        functions produced by FunctionMaker will return their output value
        directly.
    mode : Mode instance
        Telling FunctionMaker how to rewrite and link. None means to use the
        `config.mode`.
    accept_inplace : bool
        True iff it is acceptable to have inplace operations in the graph from
        the inputs to the outputs.
    on_unused_input : {'raise', 'warn', 'ignore', None}
        What to do if a variable in the 'inputs' list is not used in the graph.
        Possible values are:
        - 'raise': raise an error
        - 'warn': log a warning
        - 'ignore': do not do anything
        - None: Use the value in the PyTensor flags on_unused_input.
    name : str
        An optional name for this function. If used, the profile mode will
        print the time spent in this function.
    trust_input : bool, default False
        If True, no input validation checks are performed when the function is
        called. This includes checking the number of inputs, their types and
        that multiple inputs are not aliased to each other. Failure to meet any
        of these conditions can lead to computational errors or to the
        interpreter crashing.
    """

    @staticmethod
    def wrap_in(input):
        if isinstance(input, (SymbolicInput)):
            return input
        elif isinstance(input, Variable):
            # r -> SymbolicInput(variable=r)
            return SymbolicInput(input)
        elif isinstance(input, list | tuple):
            # (r, u) -> SymbolicInput(variable=r, update=u)
            if len(input) == 2:
                return SymbolicInput(input[0], update=input[1])
            else:
                raise TypeError(
                    f"Expected two elements in the list or tuple; got {input}"
                )
        else:
            raise TypeError(
                f"Unknown input type: {type(input)} ({input}), expected Variable "
                "instance"
            )

    @staticmethod
    def wrap_out(output):
        if isinstance(output, SymbolicOutput):
            return output
        elif isinstance(output, Variable):
            return SymbolicOutput(output)
        else:
            raise TypeError(f"Unknown output type: {type(output)} ({output})")

    @staticmethod
    def check_unused_inputs(inputs, outputs, on_unused_input):
        if on_unused_input is None:
            on_unused_input = config.on_unused_input

        if on_unused_input == "ignore":
            return

        # There should be two categories of variables in inputs:
        #  - variables that have to be provided (used_inputs)
        #  - shared variables that will be updated
        used_inputs = list(
            ancestors(
                (
                    [o.variable for o in outputs]
                    + [
                        i.update
                        for i in inputs
                        if getattr(i, "update", None) is not None
                    ]
                ),
                blockers=[i.variable for i in inputs],
            )
        )

        msg = (
            "pytensor.function was asked to create a function computing "
            "outputs given certain inputs, but the provided input "
            "variable at index %i is not part of the computational graph "
            "needed to compute the outputs: %s.\n%s"
        )
        warn_msg = (
            "To make this warning into an error, you can pass the "
            "parameter on_unused_input='raise' to pytensor.function. "
            "To disable it completely, use on_unused_input='ignore'."
        )
        err_msg = (
            "To make this error into a warning, you can pass the "
            "parameter on_unused_input='warn' to pytensor.function. "
            "To disable it completely, use on_unused_input='ignore'."
        )

        for i in inputs:
            if (i.variable not in used_inputs) and (i.update is None):
                if on_unused_input == "warn":
                    warnings.warn(
                        msg % (inputs.index(i), i.variable, warn_msg), stacklevel=6
                    )
                elif on_unused_input == "raise":
                    raise UnusedInputError(msg % (inputs.index(i), i.variable, err_msg))
                else:
                    raise ValueError(
                        "Invalid value for keyword on_unused_input of pytensor.function: "
                        f"'{on_unused_input}'.\n"
                        "Valid values are 'raise', 'warn', and 'ignore'."
                    )

    @staticmethod
    def prepare_fgraph(
        inputs,
        outputs,
        additional_outputs,
        fgraph: FunctionGraph,
        mode: "Mode",
        profile,
    ):
        rewriter = mode.optimizer

        try:
            start_rewriter = time.perf_counter()

            rewriter_profile = None
            rewrite_time = None

            with config.change_flags(
                mode=mode,
                compute_test_value=config.compute_test_value_opt,
                traceback__limit=config.traceback__compile_limit,
            ):
                rewriter_profile = rewriter(fgraph)

                end_rewriter = time.perf_counter()
                rewrite_time = end_rewriter - start_rewriter
                _logger.debug(f"Rewriting took {rewrite_time:f} seconds")

                # Add deep copy to respect the memory interface
                insert_deepcopy(fgraph, inputs, outputs + additional_outputs)
        finally:
            # If the rewriter got interrupted
            if rewrite_time is None:
                end_rewriter = time.perf_counter()
                rewrite_time = end_rewriter - start_rewriter

            pytensor.compile.profiling.total_graph_rewrite_time += rewrite_time

            if profile:
                if rewriter_profile is None and hasattr(rewriter, "pre_profile"):
                    rewriter_profile = rewriter.pre_profile

                profile.rewriting_time += rewrite_time

                if config.profile_optimizer:
                    profile.rewriter_profile = (rewriter, rewriter_profile)
            elif config.profile_optimizer and profile is not False:
                # If False, it means the profiling for that function was
                # explicitly disabled
                warnings.warn(
                    (
                        "config.profile_optimizer requires config.profile to "
                        " be set to True as well"
                    ),
                    stacklevel=3,
                )

        if not hasattr(mode.linker, "accept"):
            raise ValueError(
                "'linker' parameter of FunctionMaker should be "
                f"a Linker with an accept method or one of {list(pytensor.compile.mode.predefined_linkers)}"
            )

    def __init__(
        self,
        inputs,
        outputs,
        mode=None,
        accept_inplace=False,
        function_builder=Function,
        profile=None,
        on_unused_input=None,
        fgraph=None,
        output_keys=None,
        name=None,
        no_fgraph_prep=False,
        trust_input=False,
    ):
        # Save the provided mode, not the instantiated mode.
        # The instantiated mode don't pickle and if we unpickle an PyTensor
        # function and it get re-compiled, we want the current rewriter to be
        # used, not the rewriter when it was saved.
        self.mode = mode
        mode = pytensor.compile.mode.get_mode(mode)

        # Assert old way of working isn't used
        if getattr(mode, "profile", None):
            raise TypeError("profile passed via 'mode'. This isn't supported anymore")
        self.profile = profile
        if profile and config.cxx:
            # This is very important:
            # 1) We preload the cache here to not have its timing
            #    included with the rewrites.
            # 2) Do not refresh the cache here by default. It cause
            #    too much execution time during testing as we compile
            #    much more functions then the number of compile c
            #    module.
            start_get_cache = time.perf_counter()
            pytensor.link.c.basic.get_module_cache().refresh()
            get_cache_time = time.perf_counter() - start_get_cache
            self.profile.linker_time += get_cache_time
            self.profile.preload_cache_time += get_cache_time

        # Handle the case where inputs and/or outputs is a single
        # Variable (not in a list)
        unpack_single = False
        return_none = False
        if outputs is None:
            return_none = True
            outputs = []
        if not isinstance(outputs, list | tuple):
            unpack_single = True
            outputs = [outputs]
        if not isinstance(inputs, list | tuple):
            inputs = [inputs]

        # Wrap them in In or Out instances if needed.
        inputs = [self.wrap_in(i) for i in inputs]
        outputs = [self.wrap_out(o) for o in outputs]

        # Check if some input variables are unused
        self.check_unused_inputs(inputs, outputs, on_unused_input)

        indices = [[input, None, [input]] for input in inputs]

        fgraph, found_updates = std_fgraph(
            inputs, outputs, accept_inplace, fgraph=fgraph
        )

        if fgraph.profile is None:
            fgraph.profile = profile

        self.fgraph = fgraph

        if not no_fgraph_prep:
            self.prepare_fgraph(inputs, outputs, found_updates, fgraph, mode, profile)

        assert len(fgraph.outputs) == len(outputs + found_updates)

        # The 'no_borrow' outputs are the ones for which that we can't
        # return the internal storage pointer.
        no_borrow = [
            output
            for output, spec in zip(
                fgraph.outputs, outputs + found_updates, strict=True
            )
            if not spec.borrow
        ]

        linker = copy.copy(mode.linker)

        if no_borrow:
            self.linker = linker.accept(
                fgraph,
                no_recycling=infer_reuse_pattern(fgraph, no_borrow),
                profile=profile,
            )
        else:
            self.linker = linker.accept(fgraph, profile=profile)

        if hasattr(linker, "accept_var_updates"):
            # TODO: This is a hack that makes `VMLinker` aware of updates;
            # clean this up.
            self.linker.accept_var_updates(fgraph_updated_vars(fgraph, inputs))

        fgraph.name = name
        self.indices = indices
        self.inputs = inputs

        # TODO: Get rid of all this `expanded_inputs` nonsense
        self.expanded_inputs = inputs
        self.outputs = outputs
        self.unpack_single = unpack_single
        self.return_none = return_none
        self.accept_inplace = accept_inplace
        self.function_builder = function_builder
        self.on_unused_input = on_unused_input  # Used for the pickling/copy
        self.output_keys = output_keys
        self.name = name
        self.trust_input = trust_input

        self.required = [(i.value is None) for i in self.inputs]
        self.refeed = [
            (
                i.value is not None
                and not isinstance(i.value, Container)
                and i.update is None
            )
            for i in self.inputs
        ]
        if any(self.refeed):
            warnings.warn("Inputs with default values are deprecated.", FutureWarning)

    def create(self, input_storage=None, storage_map=None):
        """
        Create a function.

        Parameters
        ----------
        input_storage
            A list matching the inputs list and providing default values if the
            default for an input is None, then that input is a required input.
            For an input with an update, the default acts as initialization.
        """

        if input_storage is None:
            input_storage = [None] * len(self.inputs)
        # list of independent one-element lists, will be passed to the linker
        input_storage_lists = []
        defaults = []

        # The following loop is to fill in the input_storage_lists and
        # defaults lists.
        assert len(self.indices) == len(input_storage)
        for i, ((input, indices, subinputs), input_storage_i) in enumerate(
            zip(self.indices, input_storage, strict=True)
        ):
            # Replace any default value given as a variable by its
            # container.  Note that this makes sense only in the
            # context of shared variables, but for now we avoid
            # dealing directly with them to avoid dependency on the
            # shared variables work-in-progress repository.
            if isinstance(input_storage_i, Variable):
                input_storage_i = input_storage_i.container

            if isinstance(input_storage_i, Container):
                # If the default is a Container, this means we want to
                # share the same storage. This is done by appending
                # input_storage_i.storage to input_storage_lists.
                if indices is not None:
                    raise TypeError(
                        "Cannot take a Container instance as "
                        "default for a SymbolicInputKit."
                    )
                input_storage_lists.append(input_storage_i.storage)

                storage = input_storage[i].storage[0]

            else:
                # Normal case: one new, independent storage unit
                input_storage_lists.append([input_storage_i])

                storage = input_storage_i

            required = self.required[i]
            refeed = self.refeed[i]
            # sanity check-- if an input is required it should not
            # need to be refed
            assert not (required and refeed)

            # shared variables need neither be input by the user nor refed
            if input.shared:
                assert not required
                assert not refeed
                storage = None

            # if an input is required, it never need be refed
            if required:
                storage = None

            # make sure that we only store a value if we actually need it
            if storage is not None:
                assert refeed or not required

            defaults.append((required, refeed, storage))

        # Get a function instance
        start_linker = time.perf_counter()
        start_import_time = pytensor.link.c.cmodule.import_time

        with config.change_flags(traceback__limit=config.traceback__compile_limit):
            _fn, _i, _o = self.linker.make_thunk(
                input_storage=input_storage_lists, storage_map=storage_map
            )

        end_linker = time.perf_counter()

        linker_time = end_linker - start_linker
        pytensor.compile.profiling.total_time_linker += linker_time
        _logger.debug(f"Linker took {linker_time:f} seconds")
        if self.profile:
            self.profile.linker_time += linker_time
            _fn.time_thunks = self.profile.flag_time_thunks
            import_time = pytensor.link.c.cmodule.import_time - start_import_time
            self.profile.import_time += import_time

        fn = self.function_builder(
            _fn,
            _i,
            _o,
            self.indices,
            self.outputs,
            defaults,
            self.unpack_single,
            self.return_none,
            self.output_keys,
            self,
            trust_input=self.trust_input,
            name=self.name,
        )

        fn.profile = self.profile
        return fn


def orig_function(
    inputs,
    outputs,
    mode=None,
    accept_inplace=False,
    name=None,
    profile=None,
    on_unused_input=None,
    output_keys=None,
    fgraph: FunctionGraph | None = None,
    trust_input: bool = False,
) -> Function:
    """
    Return a Function that will calculate the outputs from the inputs.

    Parameters
    ----------
    inputs : list of `SymbolicInput` or `In` instances
    outputs : a SymbolicOutput or a list of `SymbolicOutput` or `Out` instances
        The return value of the returned function will match the format of this
        argument (either the value itself or a list of one or more return
        values).
    mode : descriptive string or Mode instance
        Default of None means to use `config.mode` (see below for descriptive
        string list).
    name : str
        An optional name for this function. If used, the profile mode will print the
        time spent in this function.
    accept_inplace : bool
        True iff the graph can contain inplace operations prior to the
        rewrite phase (default is False).
    profile : None or ProfileStats instance
    on_unused_input : {'raise', 'warn', 'ignore', None}
        What to do if a variable in the 'inputs' list is not used in the graph.
    output_keys
        If the outputs were provided to pytensor.function as a list, then
        output_keys is None. Otherwise, if outputs were provided as a dict,
        output_keys is the sorted list of keys from the outputs.
    fgraph
        An existing `FunctionGraph` to use instead of constructing a new one
        from cloned `outputs`.
    trust_input : bool, default False
        If True, no input validation checks are performed when the function is
        called. This includes checking the number of inputs, their types and
        that multiple inputs are not aliased to each other. Failure to meet any
        of these conditions can lead to computational errors or to the
        interpreter crashing.
    """

    if profile:
        t1 = time.perf_counter()
    mode = pytensor.compile.mode.get_mode(mode)

    inputs = list(map(convert_function_input, inputs))

    if outputs is not None:
        if isinstance(outputs, list | tuple):
            outputs = list(map(FunctionMaker.wrap_out, outputs))
        else:
            outputs = FunctionMaker.wrap_out(outputs)

    defaults = [getattr(input, "value", None) for input in inputs]

    if isinstance(mode, list | tuple):
        raise ValueError("We do not support the passing of multiple modes")

    fn = None
    try:
        Maker = getattr(mode, "function_maker", FunctionMaker)
        m = Maker(
            inputs,
            outputs,
            mode,
            accept_inplace=accept_inplace,
            profile=profile,
            on_unused_input=on_unused_input,
            output_keys=output_keys,
            name=name,
            fgraph=fgraph,
            trust_input=trust_input,
        )
        with config.change_flags(compute_test_value="off"):
            fn = m.create(defaults)
    finally:
        if profile and fn:
            t2 = time.perf_counter()
            profile.compile_time += t2 - t1
            # TODO: append
            profile.nb_nodes = len(fn.maker.fgraph.apply_nodes)

    return fn


def convert_function_input(input):
    """
    Upgrade a input shortcut to an In instance.

    The rules for upgrading are as follows:

    - a `Variable` instance r will be upgraded like `In`(r)

    - a tuple (name, r) will be `In`(r, name=name)

    - a tuple (r, val) will be `In`(r, value=value, autoname=True)

    - a tuple ((r,up), val) will be
      `In`(r, value=value, update=up, autoname=True)

    - a tuple (name, r, val) will be `In`(r, name=name, value=value)

    - a tuple (name, (r,up), val) will be
      `In`(r, name=name, value=val, update=up, autoname=True)

    """
    if isinstance(input, SymbolicInput):
        return input
    elif isinstance(input, Constant):
        raise TypeError(f"A Constant instance is not a legal function input: {input}")
    elif isinstance(input, Variable):
        return In(input)
    elif isinstance(input, list | tuple):
        orig = input
        if not input:
            raise TypeError(f"Nonsensical input specification: {input}")
        if isinstance(input[0], str):
            name = input[0]
            input = input[1:]
        else:
            name = None
        if isinstance(input[0], list | tuple):
            if len(input[0]) != 2 or len(input) != 2:
                raise TypeError(
                    f"Invalid input syntax: {orig} (check "
                    "documentation or use an In instance)"
                )
            (variable, update), value = input
        elif isinstance(input[0], Variable):
            if len(input) == 1:
                variable, update, value = input[0], None, None
            elif len(input) == 2:
                (variable, value), update = input, None
            else:
                raise TypeError(
                    f"Invalid input syntax: {orig} (check "
                    "documentation or use an In instance)"
                )
        elif isinstance(input[0], SymbolicInput):
            if len(input) == 1:
                return input[0]
            elif len(input) == 2:
                input, value = input
                if name is not None:
                    input.name = name
                input.value = value
                return input
        else:
            raise TypeError(f"The input specification is not valid: {input}")

        if not isinstance(variable, Variable):
            raise TypeError(
                f"Unknown input type: {type(variable)}, expected Variable instance"
            )
        if update is not None and not isinstance(update, Variable):
            raise TypeError(
                f"Unknown update type: {type(update)}, expected Variable instance"
            )
        if value is not None and isinstance(value, Variable | SymbolicInput):
            raise TypeError(
                f"The value for input {variable} should not be a Variable "
                f"or SymbolicInput instance (got: {value})"
            )

        return In(variable, name=name, value=value, update=update)
    else:
        raise TypeError(
            f"Unknown input type: {type(input)}, expected Variable instance"
        )


def get_info_on_inputs(named_inputs, n_unnamed_inputs):
    """
    Return a human-readable description of named and un-named inputs.

    """
    n_named_inputs = len(named_inputs)

    def get_plural(n):
        if n > 1:
            return "s"
        else:
            return ""

    if n_named_inputs == 0:
        if n_unnamed_inputs == 0:
            msg = "The function is supposed to have no input."
        else:
            if n_unnamed_inputs == 1:
                msg = (
                    "The function has a single input variable which has no "
                    "name, and thus cannot be assigned through a keyword"
                    " argument (use 'name=...' in a Variable's "
                    "constructor to give it a name)."
                )
            else:
                # Use plural.
                msg = (
                    f"The function has {n_unnamed_inputs} inputs, but none of them is named,"
                    " and thus they cannot be assigned through keyword "
                    "arguments (use 'name=...' in a Variable's "
                    "constructor to give it a name)."
                )
    else:
        if n_unnamed_inputs == 0:
            msg = f"The function has {n_named_inputs} named input{get_plural(n_named_inputs)} ({', '.join(named_inputs)})."
        else:
            msg = (
                f"The function has {n_named_inputs} named input{get_plural(n_named_inputs)} ({', '.join(named_inputs)}), and {n_unnamed_inputs} unnamed "
                f"input{get_plural(n_unnamed_inputs)} which thus cannot be accessed through keyword "
                f"argument{get_plural(n_unnamed_inputs)} (use 'name=...' in a variable's constructor "
                "to give it a name)."
            )
    return msg
