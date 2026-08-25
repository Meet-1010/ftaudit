# A Cython declaration file. It compiles to no module of its own, so it must
# never be flagged for a missing freethreading directive.
cdef extern from "foo.h":
    int foo(int x)
