# Three popular packages silently re-enable the GIL for the whole process

**Status:** ready to file upstream (lxml, grpcio, SQLAlchemy)
**Severity:** high — removes free-threading from every library in the program
**Found by:** `ftaudit gilcheck --installed`

## The failure mode

A C or Cython extension that does not declare
`Py_mod_gil = Py_MOD_GIL_NOT_USED` makes CPython switch the GIL **back on for
the entire process** at import time. The wheel installs fine, the import
succeeds, and nothing fails — free-threading just quietly stops happening for
every other library in the program.

CPython does emit a `RuntimeWarning`, but it is easy to miss in a normal
application startup, and the effect is whole-program rather than local.

## What was found

81 top-level modules were checked in a free-threaded 3.14.4 venv, each imported
in a fresh subprocess with `sys._is_gil_enabled()` sampled before and after.

| Package | Version | Extension that re-enables the GIL |
| --- | --- | --- |
| **lxml** | 6.1.2 | `lxml.etree`, `lxml._elementpath` |
| **grpcio** | 1.83.0 | `grpc._cython.cygrpc` |
| **SQLAlchemy** | 2.0.52 | all five `sqlalchemy.cyextension.*` modules |

All three ship `cp314t` wheels — they install cleanly on a free-threaded
interpreter and look supported.

CPython's own diagnosis:

```console
$ python3.14t -W error::RuntimeWarning -c "import lxml.etree"
RuntimeWarning: The global interpreter lock (GIL) has been enabled to load
module 'lxml.etree', which has not declared that it can run safely without
the GIL. To override this behavior and keep the GIL disabled (at your own
risk), run with PYTHON_GIL=0 or -Xgil=0.
```

## Blast radius

The cost is paid by anything that transitively imports these, not just direct
users. Measured in the same environment:

```console
$ python3.14t -c "import soupsieve; import sys; print(sys._is_gil_enabled())"
True
$ python3.14t -c "import bs4;        import sys; print(sys._is_gil_enabled())"
True
```

Neither `soupsieve` nor `bs4` contains any C code. They import `lxml`, and that
is enough to turn the GIL back on for the whole program. Any application that
uses BeautifulSoup, `pandas.read_html`, Scrapy, or a gRPC client alongside
genuinely parallel work loses free-threading without a single line of its own
code being at fault.

## What to ask for

The declaration is a statement about thread safety, so it should follow an
audit rather than precede one. The realistic upstream ask is a tracking issue
per project: audit the extension's global state, then either add the
declaration or document that the module forces the GIL on. For the Cython
modules (`grpcio`, `SQLAlchemy`, most of `lxml`) the mechanism is the
`# cython: freethreading_compatible = True` directive once the audit passes.

`ftaudit scan` detects the source-level half of this (rule **FT112**) before a
wheel is even built, and `ftaudit gilcheck` detects it in an installed
environment.

## Caveats

- "Re-enables the GIL" is a packaging/declaration fact, **not** evidence that
  the extension is actually thread-unsafe. It may well be safe and simply
  undeclared. Only an audit can tell, which is exactly why upstream has to do
  it rather than a drive-by PR adding the flag.
- Versions are those resolved on 2026-08-25; newer releases may already declare
  support.
