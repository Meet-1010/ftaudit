# cython: freethreading_compatible = True
cdef int _n = 0

def get():
    return _n
