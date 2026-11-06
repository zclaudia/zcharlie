"""DB-managed config documents (docs/product/config-docs-plan.md).

* ``registry`` - the explicit list of document kinds (``ConfigDocSpec``:
  kind, title, baseline file, parse / validate callable, apply mode);
* ``store`` - ``ConfigDocStore``: read (DB ⊕ baseline default), validated
  whole-document write (change history + settings revision bump), history,
  one-time baseline import and drift status;
* ``loader`` - process start-up import + cache priming, the revision-keyed
  refresh hooks and the sync getters the factor consumers use.
"""
