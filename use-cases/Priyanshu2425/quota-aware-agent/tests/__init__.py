"""Present so `tests` is a real package rather than a namespace portion.

Without it, `from tests.fake import FakeSuperDocs` -- which `backend/demo.py`
and every test module do -- resolves against any OTHER top-level `tests` package
installed in the interpreter, because a regular package anywhere on `sys.path`
beats a namespace directory at the front of it. A stray `tests` in
site-packages (some projects ship theirs) therefore broke the README's first
command and every one of the 106 tests, with a `ModuleNotFoundError` that names
`tests.fake` and not the package that shadowed it. Verified on 2026-08-27:
zero tests collected until this file existed.
"""
