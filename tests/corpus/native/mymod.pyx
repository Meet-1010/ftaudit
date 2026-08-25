# A Cython module with no freethreading_compatible directive: importing it
# re-enables the GIL for the whole process.
cdef int _counter = 0

def bump():
    global _counter
    _counter += 1
    return _counter
