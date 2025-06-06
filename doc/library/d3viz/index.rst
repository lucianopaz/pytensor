.. _libdoc_d3viz:

===========================================================================
:mod:`d3viz` -- d3viz: Interactive visualization of PyTensor compute graphs
===========================================================================

.. module:: pytensor.d3viz
   :platform: Unix, Windows
   :synopsis: Allows to interactively visualize PyTensor compute graphs
.. moduleauthor:: Christof Angermueller


Guide
=====

Requirements
------------

``d3viz`` requires the `pydot <https://pypi.python.org/pypi/pydot>`__
package. Install it with pip::

    pip install pydot

Like PyTensor’s printing module, ``d3viz``
requires `graphviz <http://www.graphviz.org/>`__ binary to be available.

Overview
--------

``d3viz`` extends PyTensor’s printing module to interactively visualize compute
graphs. Instead of creating a static picture, it creates an HTML file, which can
be opened with current web-browsers. ``d3viz`` allows

-  to zoom to different regions and to move graphs via drag and drop,
-  to position nodes both manually and automatically,
-  to retrieve additional information about nodes and edges such as
   their data type or definition in the source code,
-  to edit node labels,
-  to visualizing profiling information, and
-  to explore nested graphs such as OpFromGraph nodes.

.. note::

    This userguide is also available as
    :download:`IPython notebook <index.ipynb>`.

As an example, consider the following multilayer perceptron with one
hidden layer and a softmax output layer.

.. code:: python

    import pytensor
    import pytensor.tensor as pt
    import numpy as np

    ninputs = 1000
    nfeatures = 100
    noutputs = 10
    nhiddens = 50

    rng = np.random.default_rng(0)
    x = pt.dmatrix('x')
    wh = pytensor.shared(rng.normal(0, 1, (nfeatures, nhiddens)), borrow=True)
    bh = pytensor.shared(np.zeros(nhiddens), borrow=True)
    h = pt.sigmoid(pt.dot(x, wh) + bh)

    wy = pytensor.shared(rng.normal(0, 1, (nhiddens, noutputs)))
    by = pytensor.shared(np.zeros(noutputs), borrow=True)
    y = pt.special.softmax(pt.dot(h, wy) + by)

    predict = pytensor.function([x], y)

The function ``predict`` outputs the probability of 10 classes. You can
visualize it with :py:func:`pytensor.printing.pydotprint` as follows:

.. code:: python

    from pytensor.printing import pydotprint
    from pathlib import Path

    Path("examples").mkdir(exist_ok=True)
    pydotprint(predict, 'examples/mlp.png')


.. parsed-literal::

    The output file is available at examples/mlp.png


.. code:: python

    from IPython.display import Image
    Image('./examples/mlp.png', width='80%')




.. image:: index_files/index_10_0.png



To visualize it interactively, import :py:func:`pytensor.d3viz.d3viz.d3viz` from
the the :py:mod:`pytensor.d3viz.d3viz` module, which can be called as before:

.. code:: python

    import pytensor.d3viz as d3v
    d3v.d3viz(predict, 'examples/mlp.html')

`Open visualization! <../../_static/mlp.html>`__

When you open the output file ``mlp.html`` in your web-browser, you will
see an interactive visualization of the compute graph. You can move the
whole graph or single nodes via drag and drop, and zoom via the mouse
wheel. When you move the mouse cursor over a node, a window will pop up
that displays detailed information about the node, such as its data type
or definition in the source code. When you left-click on a node and
select ``Edit``, you can change the predefined node label. If you are
dealing with a complex graph with many nodes, the default node layout
may not be perfect. In this case, you can press the ``Release node``
button in the top-left corner to automatically arrange nodes. To reset
nodes to their default position, press the ``Reset nodes`` button.

You can also display the interactive graph inline in
IPython using ``IPython.display.IFrame``:

.. code:: python

    from IPython.display import IFrame
    d3v.d3viz(predict, 'examples/mlp.html')
    IFrame('examples/mlp.html', width=700, height=500)

Currently if you use display.IFrame you still have to create a file,
and this file can't be outside notebooks root (e.g. usually it can't be
in /tmp/).

Profiling
---------

PyTensor allows function profiling via the ``profile=True`` flag. After at least
one function call, the compute time of each node can be printed in text form
with ``debugprint``. However, analyzing complex graphs in this way can be
cumbersome.

``d3viz`` can visualize the same timing information graphically, and
hence help to spot bottlenecks in the compute graph more easily! To
begin with, we will redefine the ``predict`` function, this time by
using ``profile=True`` flag. Afterwards, we capture the runtime on
random data:

.. code:: python

    predict_profiled = pytensor.function([x], y, profile=True)

    x_val = rng.normal(0, 1, (ninputs, nfeatures))
    y_val = predict_profiled(x_val)

.. code:: python

    d3v.d3viz(predict_profiled, 'examples/mlp2.html')

`Open visualization! <../../_static/mlp2.html>`__

When you open the HTML file in your browser, you will find an additional
``Toggle profile colors`` button in the menu bar. By clicking on it,
nodes will be colored by their compute time, where red corresponds to a
high compute time. You can read out the exact timing information of a
node by moving the cursor over it.

Different output formats
------------------------

Internally, ``d3viz`` represents a compute graph in the `Graphviz DOT
language <http://www.graphviz.org/>`__, using the
`pydot <https://pypi.python.org/pypi/pydot>`__ package, and defines a
front-end based on the `d3.js <http://d3js.org/>`__ library to visualize
it. However, any other Graphviz front-end can be used, which allows to
export graphs to different formats.

.. code:: python

    formatter = d3v.formatting.PyDotFormatter()
    pydot_graph = formatter(predict_profiled)

    pydot_graph.write_png('examples/mlp2.png');
    pydot_graph.write_png('examples/mlp2.pdf');

.. code:: python

    Image('./examples/mlp2.png')


.. image:: index_files/index_24_0.png


Here, we used the :py:class:`pytensor.d3viz.formatting.PyDotFormatter` class to
convert the compute graph into a ``pydot`` graph, and created a
:download:`PNG <examples/mlp2.png>` and :download:`PDF <examples/mlp2.pdf>`
file. You can find all output formats supported by Graphviz `here
<http://www.graphviz.org/doc/info/output.html>`__.

OpFromGraph nodes
-----------------

An ``OpFromGraph`` node defines a new operation, which can be called with
different inputs at different places in the compute graph. Each ``OpFromGraph``
node defines a nested graph, which will be visualized accordingly by ``d3viz``.

.. code:: python

    x, y, z = pt.scalars('xyz')
    e = pt.sigmoid((x + y + z)**2)
    op = pytensor.compile.builders.OpFromGraph([x, y, z], [e])

    e2 = op(x, y, z) + op(z, y, x)
    f = pytensor.function([x, y, z], e2)

.. code:: python

    d3v.d3viz(f, 'examples/ofg.html')

`Open visualization! <../../_static/ofg.html>`__

In this example, an operation with three inputs is defined, which is
used to build a function that calls this operations twice, each time
with different input arguments.

In the ``d3viz`` visualization, you will find two OpFromGraph nodes,
which correspond to the two OpFromGraph calls. When you double click on
one of them, the nested graph appears with the correct mapping of its
input arguments. You can move it around by drag and drop in the shaded
area, and close it again by double-click.

An OpFromGraph operation can be composed of further OpFromGraph
operations, which will be visualized as nested graphs as you can see in
the following example.

.. code:: python

    x, y, z = pt.scalars('xyz')
    e = x * y
    op = pytensor.compile.builders.OpFromGraph([x, y], [e])
    e2 = op(x, y) + z
    op2 = pytensor.compile.builders.OpFromGraph([x, y, z], [e2])
    e3 = op2(x, y, z) + z
    f = pytensor.function([x, y, z], [e3])

.. code:: python

    d3v.d3viz(f, 'examples/ofg2.html')

`Open visualization! <../../_static/ofg2.html>`__

Feedback
--------

If you have any problems or great ideas on how to improve ``d3viz``,
please let me know!

-  Christof Angermueller
-  cangermueller@gmail.com
-  https://cangermueller.com


References
==========

d3viz module
------------

.. automodule:: pytensor.d3viz.d3viz
  :members:

PyDotFormatter
--------------

.. autoclass:: pytensor.d3viz.formatting.PyDotFormatter
  :members: __call__
  :special-members:
  :private-members:
