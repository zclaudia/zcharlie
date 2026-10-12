"""Shared test fixtures and constructors, grouped by domain.

Every module here holds definitions that more than one test module needs, so
no test module imports from another one (a single rename there used to break
every importer). Import what you need explicitly; pytest registers an
imported fixture in the importing test module's namespace exactly like a
locally defined one, so ``usefixtures(...)`` and parameter injection keep
working unchanged.
"""
