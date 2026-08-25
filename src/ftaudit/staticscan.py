"""AST-based detection of thread-safety hazards that the GIL used to hide.

The analyser runs in two passes per module:

1. :class:`ModuleIndex` records what lives at module scope -- which names hold
   mutable containers, which hold lock objects, which classes exist and what
   their class-level attributes are, and which module-level functions are
   generators.
2. :class:`_FunctionWalker` walks every function body.  Only code inside a
   function can run concurrently; module-level statements execute once during
   import, while the import lock is held, so mutation there is not a race.

Throughout, the walker tracks *lock depth*.  A hazard inside ``with self._lock:``
is downgraded rather than reported, which is what keeps the signal-to-noise
ratio usable on real libraries.
"""

from __future__ import annotations

import ast
import io
import os
import re
import time
import tokenize
from dataclasses import dataclass, field

from .model import Confidence, Finding, ScanResult, Severity
from .native import scan_native_sources
from .rules import (
    ATOMIC_METHODS,
    CONTAINER_CONSTRUCTORS,
    LOCK_CONSTRUCTORS,
    MUTATING_METHODS,
    PROCESS_GLOBAL_SUBSCRIPTS,
    PROCESS_GLOBALS,
    RULES,
    SCOPING_CONTEXT_MANAGERS,
)

_SUPPRESS_RE = re.compile(r"#\s*(?:ftaudit\s*:\s*ignore|noqa\s*:?\s*(?P<codes>[A-Z0-9, ]*))")

_SIDE_EFFECT_CALLS = frozenset(
    {"open", "Thread", "Process", "connect", "register", "mkdir", "makedirs",
     "Popen", "run", "compile", "spawn", "start", "acquire", "socket", "atexit"}
)

_MUTABLE_KINDS = {"mapping", "sequence", "set"}

_OP_SYMBOL = {
    "Add": "+", "Sub": "-", "Mult": "*", "Div": "/", "FloorDiv": "//", "Mod": "%",
    "Pow": "**", "LShift": "<<", "RShift": ">>", "BitOr": "|", "BitXor": "^",
    "BitAnd": "&", "MatMult": "@",
}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def dotted(node: ast.AST) -> str | None:
    """Render a Name/Attribute chain as a dotted string, else None."""
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    if isinstance(cur, ast.Call):
        inner = dotted(cur.func)
        if inner in ("type", "super"):
            parts.append(f"{inner}()")
            return ".".join(reversed(parts))
    return None


def root_name(node: ast.AST) -> str | None:
    """The leftmost Name of an attribute/subscript chain."""
    cur = node
    while True:
        if isinstance(cur, ast.Attribute):
            cur = cur.value
        elif isinstance(cur, ast.Subscript):
            cur = cur.value
        elif isinstance(cur, ast.Call):
            cur = cur.func
        else:
            break
    return cur.id if isinstance(cur, ast.Name) else None


def value_kind(node: ast.AST | None) -> str:
    """Classify the right-hand side of an assignment."""
    if node is None:
        return "unknown"
    if isinstance(node, (ast.Dict, ast.DictComp)):
        return "mapping"
    if isinstance(node, (ast.List, ast.ListComp)):
        return "sequence"
    if isinstance(node, (ast.Set, ast.SetComp)):
        return "set"
    if isinstance(node, ast.Tuple):
        return "immutable"
    if isinstance(node, ast.GeneratorExp):
        return "iterator"
    if isinstance(node, ast.Constant):
        if node.value is None:
            return "none"
        if isinstance(node.value, (int, float, complex, str, bytes, bool)):
            return "scalar"
        return "immutable"
    if isinstance(node, ast.Call):
        fn = dotted(node.func)
        if fn is None:
            return "unknown"
        if fn in LOCK_CONSTRUCTORS:
            return "lock"
        if fn in CONTAINER_CONSTRUCTORS:
            return "mapping" if CONTAINER_CONSTRUCTORS[fn] == "mapping" else CONTAINER_CONSTRUCTORS[fn]
        if fn in ("itertools.count", "count", "iter", "enumerate", "zip", "itertools.cycle", "cycle"):
            return "iterator"
        if fn in ("frozenset", "tuple", "object"):
            return "immutable"
        if fn.split(".")[-1] == "local" and "threading" in fn:
            return "threadlocal"
        return "unknown"
    return "unknown"


def is_none_test(test: ast.AST) -> str | None:
    """If `test` is a `X is None` / `not X` style sentinel check, return the operand source."""
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        op = test.ops[0]
        comp = test.comparators[0]
        if isinstance(op, (ast.Is, ast.Eq)) and isinstance(comp, ast.Constant) and comp.value is None:
            return "operand"
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return "not"
    if isinstance(test, ast.BoolOp):
        for v in test.values:
            if is_none_test(v):
                return "boolop"
    return None


def none_test_target(test: ast.AST) -> ast.AST | None:
    """The expression being sentinel-checked, for `X is None` / `not X` shapes."""
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        op, comp = test.ops[0], test.comparators[0]
        if isinstance(op, (ast.Is, ast.Eq)) and isinstance(comp, ast.Constant) and comp.value is None:
            return test.left
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return test.operand
    if isinstance(test, ast.BoolOp):
        for v in test.values:
            t = none_test_target(v)
            if t is not None:
                return t
    return None


def membership_test(test: ast.AST) -> tuple[ast.AST, ast.AST, bool] | None:
    """For `k in C` / `k not in C`, return (key, container, negated)."""
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        op = test.ops[0]
        if isinstance(op, ast.In):
            return test.left, test.comparators[0], False
        if isinstance(op, ast.NotIn):
            return test.left, test.comparators[0], True
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        inner = membership_test(test.operand)
        if inner:
            return inner[0], inner[1], not inner[2]
    return None


def bound_names(nodes: list[ast.AST]) -> set[str]:
    """Every name bound by the given statements, not descending into nested scopes."""
    out: set[str] = set()

    def add_target(t: ast.AST) -> None:
        if isinstance(t, ast.Name):
            out.add(t.id)
        elif isinstance(t, (ast.Tuple, ast.List)):
            for e in t.elts:
                add_target(e)
        elif isinstance(t, ast.Starred):
            add_target(t.value)

    def walk(node: ast.AST, top: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                out.add(child.name)
                continue  # separate scope
            if isinstance(child, ast.Lambda):
                continue
            if isinstance(child, (ast.Assign,)):
                for t in child.targets:
                    add_target(t)
            elif isinstance(child, (ast.AugAssign, ast.AnnAssign)):
                add_target(child.target)
            elif isinstance(child, (ast.For, ast.AsyncFor)):
                add_target(child.target)
            elif isinstance(child, ast.NamedExpr):
                add_target(child.target)
            elif isinstance(child, (ast.With, ast.AsyncWith)):
                for item in child.items:
                    if item.optional_vars is not None:
                        add_target(item.optional_vars)
            elif isinstance(child, ast.ExceptHandler):
                if child.name:
                    out.add(child.name)
            elif isinstance(child, (ast.Import, ast.ImportFrom)):
                for alias in child.names:
                    out.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(child, (ast.comprehension,)):
                add_target(child.target)
            walk(child, False)

    class _Root(ast.AST):
        _fields = ("body",)

    root = _Root()
    root.body = nodes  # type: ignore[attr-defined]
    walk(root, True)
    return out


# --------------------------------------------------------------------------- #
# module index
# --------------------------------------------------------------------------- #

@dataclass
class GlobalInfo:
    name: str
    kind: str
    line: int
    reassigned_at_module_level: int = 1


@dataclass
class ClassInfo:
    name: str
    line: int
    attrs: dict[str, GlobalInfo] = field(default_factory=dict)
    init_assigned: set[str] = field(default_factory=set)
    methods: set[str] = field(default_factory=set)


class ModuleIndex:
    """What exists at module scope, and what shape it has."""

    def __init__(self, tree: ast.Module) -> None:
        self.globals: dict[str, GlobalInfo] = {}
        self.classes: dict[str, ClassInfo] = {}
        self.generators: set[str] = set()
        self.imported: set[str] = set()
        self._index(tree)

    # -- construction ------------------------------------------------------ #
    def _index(self, tree: ast.Module) -> None:
        for stmt in tree.body:
            self._index_stmt(stmt)

    def _index_stmt(self, stmt: ast.AST) -> None:
        if isinstance(stmt, ast.Assign):
            kind = value_kind(stmt.value)
            for t in stmt.targets:
                if isinstance(t, ast.Name):
                    self._bind(t.id, kind, stmt.lineno)
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            self._bind(stmt.target.id, value_kind(stmt.value), stmt.lineno)
        elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
            for alias in stmt.names:
                self.imported.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self._bind(stmt.name, "function", stmt.lineno)
            if _is_generator(stmt):
                self.generators.add(stmt.name)
        elif isinstance(stmt, ast.ClassDef):
            self._bind(stmt.name, "class", stmt.lineno)
            self.classes[stmt.name] = self._index_class(stmt)
        elif isinstance(stmt, (ast.If, ast.Try)):
            # `try: import x except ImportError:` and `if TYPE_CHECKING:` blocks still
            # bind module-level names.
            for sub in ast.iter_child_nodes(stmt):
                if isinstance(sub, ast.stmt):
                    self._index_stmt(sub)
            for attr in ("body", "orelse", "finalbody", "handlers"):
                for sub in getattr(stmt, attr, []) or []:
                    if isinstance(sub, ast.ExceptHandler):
                        for s2 in sub.body:
                            self._index_stmt(s2)
                    elif isinstance(sub, ast.stmt):
                        self._index_stmt(sub)

    def _index_class(self, node: ast.ClassDef) -> ClassInfo:
        info = ClassInfo(name=node.name, line=node.lineno)
        for stmt in node.body:
            if isinstance(stmt, ast.Assign):
                kind = value_kind(stmt.value)
                for t in stmt.targets:
                    if isinstance(t, ast.Name):
                        info.attrs[t.id] = GlobalInfo(t.id, kind, stmt.lineno)
            elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                info.attrs[stmt.target.id] = GlobalInfo(stmt.target.id, value_kind(stmt.value), stmt.lineno)
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                info.methods.add(stmt.name)
                if stmt.name in ("__init__", "__new__", "__post_init__"):
                    for sub in ast.walk(stmt):
                        if isinstance(sub, (ast.Assign, ast.AnnAssign)):
                            targets = sub.targets if isinstance(sub, ast.Assign) else [sub.target]
                            for t in targets:
                                if (
                                    isinstance(t, ast.Attribute)
                                    and isinstance(t.value, ast.Name)
                                    and t.value.id == "self"
                                ):
                                    info.init_assigned.add(t.attr)
        return info

    def _bind(self, name: str, kind: str, line: int) -> None:
        prev = self.globals.get(name)
        if prev is None:
            self.globals[name] = GlobalInfo(name, kind, line)
        else:
            prev.reassigned_at_module_level += 1
            if prev.kind in ("unknown", "none") and kind not in ("unknown",):
                prev.kind = kind

    # -- queries ----------------------------------------------------------- #
    def is_lock(self, name: str) -> bool:
        g = self.globals.get(name)
        return g is not None and g.kind == "lock"

    def kind_of(self, name: str) -> str | None:
        g = self.globals.get(name)
        return g.kind if g else None


def _is_generator(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, (ast.Yield, ast.YieldFrom)):
            return True
        if sub is not node and isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
    return False


# --------------------------------------------------------------------------- #
# function walker
# --------------------------------------------------------------------------- #

@dataclass
class _Scope:
    node: ast.AST
    qualname: str
    locals_: set[str]
    globals_declared: set[str] = field(default_factory=set)
    is_method: bool = False
    class_info: ClassInfo | None = None
    is_classmethod: bool = False
    is_finalizer: bool = False
    is_cached_property: bool = False


class _FunctionWalker:
    def __init__(self, path: str, source_lines: list[str], index: ModuleIndex, suppressed: dict[int, set[str]]):
        self.path = path
        self.lines = source_lines
        self.index = index
        self.suppressed = suppressed
        self.findings: list[Finding] = []
        self.lock_depth = 0
        self.scoped_depth = 0
        self.scopes: list[_Scope] = []
        # name -> the expression it was most recently assigned from, in the current function
        self.local_origin: dict[str, ast.AST] = {}
        # shared slot -> count of assignments seen in the current function
        self.slot_assigns: dict[str, list[tuple[int, bool]]] = {}
        self._dcl_ok: set[tuple[str, int]] = set()

    # -- emission ---------------------------------------------------------- #
    def emit(
        self,
        rule_id: str,
        node: ast.AST,
        *,
        symbol: str = "",
        message: str | None = None,
        severity: Severity | None = None,
        confidence: Confidence = Confidence.MEDIUM,
        extra: dict | None = None,
    ) -> None:
        rule = RULES[rule_id]
        line = getattr(node, "lineno", 0)
        codes = self.suppressed.get(line)
        if codes is not None and (not codes or rule_id in codes):
            return
        sev = severity or rule.severity
        if rule_id == "FT106" and self.scoped_depth > 0:
            # the setting is restored per-thread by the enclosing context manager
            return
        if self.lock_depth > 0:
            # Held under a lock: keep it, but as context rather than a defect.
            sev = Severity.INFO
            confidence = Confidence.LOW
        self.findings.append(
            Finding(
                rule=rule_id,
                name=rule.name,
                message=message or rule.summary,
                path=self.path,
                line=line,
                col=getattr(node, "col_offset", 0),
                end_line=getattr(node, "end_lineno", line) or line,
                severity=sev,
                confidence=confidence,
                symbol=symbol,
                function=self.scopes[-1].qualname if self.scopes else "<module>",
                snippet=self._snippet(line, getattr(node, "end_lineno", line) or line),
                why=rule.why,
                fix=rule.fix,
                under_lock=self.lock_depth > 0,
                extra=extra or {},
            )
        )

    def _snippet(self, start: int, end: int) -> str:
        end = min(end, start + 6)
        chunk = self.lines[start - 1 : end]
        if not chunk:
            return ""
        indent = min((len(l) - len(l.lstrip()) for l in chunk if l.strip()), default=0)
        return "\n".join(l[indent:].rstrip() for l in chunk)

    # -- shared-slot resolution -------------------------------------------- #
    def _scope(self) -> _Scope | None:
        return self.scopes[-1] if self.scopes else None

    def _is_local(self, name: str) -> bool:
        for sc in reversed(self.scopes):
            if name in sc.globals_declared:
                return False
            if name in sc.locals_:
                return True
        return False

    def shared_slot(self, node: ast.AST) -> tuple[str, str] | None:
        """Resolve an expression to a (slot-name, kind) pair if it names shared state.

        Shared means: a module-level binding, or an attribute on a class object
        (``Cls.attr``, ``cls.attr``, ``type(self).attr``), or a class-level
        attribute reached through ``self`` that ``__init__`` never shadows.
        """
        if isinstance(node, ast.Name):
            if self._is_local(node.id):
                return None
            g = self.index.globals.get(node.id)
            if g is None or g.kind in ("function", "class"):
                return None
            if node.id in self.index.imported:
                return None
            return node.id, g.kind

        if isinstance(node, ast.Attribute):
            base = node.value
            attr = node.attr
            sc = self._scope()
            # Cls.attr where Cls is a module-level class
            if isinstance(base, ast.Name) and base.id in self.index.classes:
                ci = self.index.classes[base.id]
                a = ci.attrs.get(attr)
                return f"{base.id}.{attr}", (a.kind if a else "unknown")
            # cls.attr inside a classmethod
            if isinstance(base, ast.Name) and base.id == "cls" and sc and sc.is_classmethod and sc.class_info:
                a = sc.class_info.attrs.get(attr)
                return f"{sc.class_info.name}.{attr}", (a.kind if a else "unknown")
            # type(self).attr / self.__class__.attr
            d = dotted(base)
            if d in ("type().", "type()") or (isinstance(base, ast.Call) and dotted(base.func) == "type"):
                if sc and sc.class_info:
                    a = sc.class_info.attrs.get(attr)
                    return f"{sc.class_info.name}.{attr}", (a.kind if a else "unknown")
            if isinstance(base, ast.Attribute) and base.attr == "__class__":
                if sc and sc.class_info:
                    a = sc.class_info.attrs.get(attr)
                    return f"{sc.class_info.name}.{attr}", (a.kind if a else "unknown")
            # self.attr where attr is a *class-level mutable* never rebound in __init__
            if isinstance(base, ast.Name) and base.id == "self" and sc and sc.class_info:
                ci = sc.class_info
                a = ci.attrs.get(attr)
                if a is not None and a.kind in _MUTABLE_KINDS and attr not in ci.init_assigned:
                    return f"{ci.name}.{attr}", a.kind
            # module.attr where module is imported -> not our state
            return None

        return None

    def is_lock_expr(self, node: ast.AST) -> bool:
        d = dotted(node)
        if d:
            last = d.split(".")[-1].lower()
            if any(k in last for k in ("lock", "mutex", "semaphore", "rlock", "critical_section")):
                return True
            root = d.split(".")[0]
            if self.index.is_lock(root) and len(d.split(".")) == 1:
                return True
        if isinstance(node, ast.Call):
            fn = dotted(node.func)
            if fn and fn.split(".")[-1] in ("acquire", "lock", "locked"):
                return True
            if fn and fn.split(".")[-1] in ("nullcontext",):
                return False
        return False

    # -- traversal --------------------------------------------------------- #
    def visit_module(self, tree: ast.Module) -> None:
        for stmt in tree.body:
            self._descend_top(stmt, prefix="")

    def _descend_top(self, node: ast.AST, prefix: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self.enter_function(node, prefix, class_info=None)
        elif isinstance(node, ast.ClassDef):
            ci = self.index.classes.get(node.name) or ClassInfo(node.name, node.lineno)
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    self.enter_function(sub, f"{prefix}{node.name}.", class_info=ci)
                elif isinstance(sub, ast.ClassDef):
                    self._descend_top(sub, f"{prefix}{node.name}.")
        elif isinstance(node, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
            for sub in ast.iter_child_nodes(node):
                if isinstance(sub, ast.stmt):
                    self._descend_top(sub, prefix)
                elif isinstance(sub, ast.ExceptHandler):
                    for s2 in sub.body:
                        self._descend_top(s2, prefix)

    def enter_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef, prefix: str, class_info: ClassInfo | None) -> None:
        decorators = {dotted(d) or "" for d in node.decorator_list}
        deco_last = {d.split(".")[-1] for d in decorators if d}
        locals_ = bound_names(list(node.body))
        for a in list(node.args.args) + list(node.args.kwonlyargs) + list(node.args.posonlyargs):
            locals_.add(a.arg)
        if node.args.vararg:
            locals_.add(node.args.vararg.arg)
        if node.args.kwarg:
            locals_.add(node.args.kwarg.arg)

        scope = _Scope(
            node=node,
            qualname=f"{prefix}{node.name}",
            locals_=locals_,
            is_method=class_info is not None,
            class_info=class_info,
            is_classmethod="classmethod" in deco_last,
            is_finalizer=node.name in ("__del__", "__exit__") or "finalize" in node.name,
            is_cached_property="cached_property" in deco_last,
        )
        for sub in ast.walk(node):
            if isinstance(sub, ast.Global):
                scope.globals_declared.update(sub.names)

        self.scopes.append(scope)
        saved_origin, saved_slots = self.local_origin, self.slot_assigns
        self.local_origin, self.slot_assigns = {}, {}

        # A whole-body `X.acquire()` makes the rest of the body effectively locked.
        self.walk_body(node.body)
        self._post_function(scope)

        self.local_origin, self.slot_assigns = saved_origin, saved_slots
        self.scopes.pop()

        # nested functions/classes
        for sub in node.body:
            self._descend_top(sub, prefix=f"{scope.qualname}.")

    def _post_function(self, scope: _Scope) -> None:
        """Checks that need the whole function body: save/mutate/restore."""
        for slot, entries in self.slot_assigns.items():
            if len(entries) < 2:
                continue
            in_finally = [e for e in entries if e[1]]
            if in_finally:
                first = min(e[0] for e in entries)
                self.emit(
                    "FT107",
                    _FakeNode(in_finally[0][0]),
                    symbol=slot,
                    message=f"`{slot}` is changed and then restored in a finally block",
                    confidence=Confidence.HIGH,
                    extra={"first_assignment_line": first},
                )

    def walk_body(self, body: list[ast.stmt]) -> None:
        for stmt in body:
            self.walk_stmt(stmt, in_finally=False)

    # -- statements -------------------------------------------------------- #
    def walk_stmt(self, node: ast.stmt, *, in_finally: bool) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return  # handled separately as its own scope

        if isinstance(node, (ast.With, ast.AsyncWith)):
            self.check_with(node)
            added = 0
            for item in node.items:
                if self.is_lock_expr(item.context_expr):
                    added += 1
            scoped = sum(
                1 for item in node.items
                if isinstance(item.context_expr, ast.Call)
                and (dotted(item.context_expr.func) or "") in SCOPING_CONTEXT_MANAGERS
            )
            self.lock_depth += added
            self.scoped_depth += scoped
            for sub in node.body:
                self.walk_stmt(sub, in_finally=in_finally)
            self.scoped_depth -= scoped
            self.lock_depth -= added
            return

        if isinstance(node, ast.If):
            self.check_if(node)
            for sub in node.body:
                self.walk_stmt(sub, in_finally=in_finally)
            for sub in node.orelse:
                self.walk_stmt(sub, in_finally=in_finally)
            self.check_expr(node.test)
            return

        if isinstance(node, ast.Try):
            for sub in node.body:
                self.walk_stmt(sub, in_finally=in_finally)
            for h in node.handlers:
                for sub in h.body:
                    self.walk_stmt(sub, in_finally=in_finally)
            for sub in node.orelse:
                self.walk_stmt(sub, in_finally=in_finally)
            for sub in node.finalbody:
                self.walk_stmt(sub, in_finally=True)
            return

        if isinstance(node, ast.Assign):
            self.check_assign(node, in_finally=in_finally)
        elif isinstance(node, ast.AugAssign):
            self.check_augassign(node, in_finally=in_finally)
        elif isinstance(node, ast.AnnAssign):
            if node.value is not None:
                self.check_assign_target(node.target, node.value, node, in_finally=in_finally)
        elif isinstance(node, ast.Delete):
            for t in node.targets:
                self.check_delete(t, node)
        elif isinstance(node, ast.Expr):
            self.check_expr(node.value)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            self.check_for(node)

        for sub in ast.iter_child_nodes(node):
            if isinstance(sub, ast.stmt):
                self.walk_stmt(sub, in_finally=in_finally)
            elif isinstance(sub, ast.ExceptHandler):
                for s2 in sub.body:
                    self.walk_stmt(s2, in_finally=in_finally)
            elif isinstance(sub, ast.expr) and not isinstance(node, (ast.Assign, ast.AugAssign, ast.Expr, ast.If)):
                self.check_expr(sub)

    # -- individual checks -------------------------------------------------- #
    def check_with(self, node: ast.With | ast.AsyncWith) -> None:
        for item in node.items:
            ctx = item.context_expr
            if isinstance(ctx, ast.Call):
                fn = dotted(ctx.func)
                if fn in LOCK_CONSTRUCTORS:
                    self.emit(
                        "FT111",
                        ctx,
                        symbol=fn or "",
                        message=f"`with {fn}():` builds a fresh lock, so no other thread can contend on it",
                        confidence=Confidence.HIGH,
                    )
                elif fn in ("warnings.catch_warnings", "catch_warnings"):
                    self.emit(
                        "FT106",
                        ctx,
                        symbol=fn or "",
                        message="warnings.catch_warnings() swaps the global warnings filter and is documented as not thread-safe",
                        confidence=Confidence.HIGH,
                    )

    def check_if(self, node: ast.If) -> None:
        target = none_test_target(node.test)
        if target is not None:
            slot = self.shared_slot(target)
            if slot is not None:
                self._check_lazy_init(node, slot[0], target)
            else:
                # local = SHARED.get(k) ; if local is None: ... SHARED[k] = ...
                if isinstance(target, ast.Name):
                    origin = self.local_origin.get(target.id)
                    if isinstance(origin, ast.Call):
                        fn = origin.func
                        if isinstance(fn, ast.Attribute) and fn.attr in ("get", "pop"):
                            cslot = self.shared_slot(fn.value)
                            if cslot and self._body_mutates(node.body, cslot[0]):
                                self.emit(
                                    "FT103",
                                    node,
                                    symbol=cslot[0],
                                    message=f"`{cslot[0]}.get(...)` miss followed by an unsynchronised store into `{cslot[0]}`",
                                    confidence=Confidence.HIGH,
                                )

        mem = membership_test(node.test)
        if mem is not None:
            key, container, negated = mem
            cslot = self.shared_slot(container)
            if cslot is not None and cslot[1] in _MUTABLE_KINDS:
                branch = node.body if negated else node.orelse
                if self._body_mutates(branch, cslot[0]) or self._body_mutates(node.body, cslot[0]):
                    self.emit(
                        "FT103",
                        node,
                        symbol=cslot[0],
                        message=f"`{'not ' if negated else ''}in {cslot[0]}` test followed by a mutation of `{cslot[0]}`",
                        confidence=Confidence.HIGH,
                    )

    def _check_lazy_init(self, node: ast.If, slot: str, target: ast.AST) -> None:
        # Is there a `with <lock>:` inside the branch?
        lock_with = None
        for sub in ast.walk(node):
            if isinstance(sub, (ast.With, ast.AsyncWith)) and any(
                self.is_lock_expr(i.context_expr) for i in sub.items
            ):
                lock_with = sub
                break
        assigns = self._assignments_to(node.body, slot)
        if not assigns:
            return
        if lock_with is not None:
            # double-checked locking: is the sentinel re-tested inside the lock?
            rechecked = False
            for sub in ast.walk(lock_with):
                if isinstance(sub, ast.If):
                    t = none_test_target(sub.test)
                    if t is not None:
                        s = self.shared_slot(t)
                        if s and s[0] == slot:
                            rechecked = True
                            break
            if rechecked:
                return  # correct DCL
            self.emit(
                "FT108",
                node,
                symbol=slot,
                message=f"`{slot}` is checked outside the lock and initialised inside it without a re-check",
                confidence=Confidence.HIGH,
            )
            return
        if self.lock_depth > 0:
            return
        self.emit(
            "FT102",
            node,
            symbol=slot,
            message=f"`{slot}` is lazily initialised with an unsynchronised check-then-act",
            confidence=Confidence.HIGH,
            extra={"assign_lines": assigns},
        )

    def _assignments_to(self, body: list[ast.stmt], slot: str) -> list[int]:
        out: list[int] = []
        for stmt in body:
            for sub in ast.walk(stmt):
                if isinstance(sub, ast.Assign):
                    for t in sub.targets:
                        s = self.shared_slot(t)
                        if s and s[0] == slot:
                            out.append(sub.lineno)
                elif isinstance(sub, (ast.AugAssign, ast.AnnAssign)):
                    s = self.shared_slot(sub.target)
                    if s and s[0] == slot:
                        out.append(sub.lineno)
        return out

    def _body_mutates(self, body: list[ast.stmt], slot: str) -> bool:
        for stmt in body:
            for sub in ast.walk(stmt):
                if isinstance(sub, ast.Assign):
                    for t in sub.targets:
                        if isinstance(t, ast.Subscript):
                            s = self.shared_slot(t.value)
                            if s and s[0] == slot:
                                return True
                        s = self.shared_slot(t)
                        if s and s[0] == slot:
                            return True
                elif isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
                    if sub.func.attr in MUTATING_METHODS:
                        s = self.shared_slot(sub.func.value)
                        if s and s[0] == slot:
                            return True
                elif isinstance(sub, ast.Delete):
                    for t in sub.targets:
                        if isinstance(t, ast.Subscript):
                            s = self.shared_slot(t.value)
                            if s and s[0] == slot:
                                return True
        return False

    def check_assign(self, node: ast.Assign, *, in_finally: bool) -> None:
        for t in node.targets:
            self.check_assign_target(t, node.value, node, in_finally=in_finally)
        # remember origins so later statements can reason about locals
        for t in node.targets:
            if isinstance(t, ast.Name):
                self.local_origin[t.id] = node.value
        self.check_expr(node.value)

    def check_assign_target(self, t: ast.AST, value: ast.AST, node: ast.stmt, *, in_finally: bool) -> None:
        sc = self._scope()

        # os.environ["X"] = ... / sys.modules[...] = ...
        if isinstance(t, ast.Subscript):
            base = dotted(t.value)
            if base in PROCESS_GLOBAL_SUBSCRIPTS:
                self.emit(
                    "FT106",
                    node,
                    symbol=base or "",
                    message=f"assigning into `{base}` mutates {PROCESS_GLOBAL_SUBSCRIPTS[base]}",
                    confidence=Confidence.HIGH,
                )
                self._record_slot(base or "", node.lineno, in_finally)
                return
            s = self.shared_slot(t.value)
            if s is not None:
                self._record_slot(s[0], node.lineno, in_finally)
                if self.lock_depth == 0:
                    self.emit(
                        "FT104",
                        node,
                        symbol=s[0],
                        message=f"unsynchronised item assignment into shared `{s[0]}`",
                        confidence=Confidence.MEDIUM,
                    )
            return

        s = self.shared_slot(t)
        if s is None:
            return
        self._record_slot(s[0], node.lineno, in_finally)

        # X = X + 1 style read-modify-write
        if self._reads_slot(value, s[0]):
            self.emit(
                "FT105",
                node,
                symbol=s[0],
                message=f"`{s[0]}` is read and written back without synchronisation",
                confidence=Confidence.HIGH,
            )
            return

        if isinstance(t, ast.Attribute) and sc and sc.class_info and not (isinstance(t.value, ast.Name) and t.value.id == "self"):
            self.emit(
                "FT110",
                node,
                symbol=s[0],
                message=f"method assigns to class-level `{s[0]}`, which every instance and thread shares",
                confidence=Confidence.HIGH if sc.is_method else Confidence.MEDIUM,
            )
            return

        if sc and s[0] in sc.globals_declared:
            self.emit(
                "FT101",
                node,
                symbol=s[0],
                message=f"`global {s[0]}` is rebound inside a function",
                confidence=Confidence.HIGH,
            )
        elif isinstance(t, ast.Name):
            self.emit(
                "FT101",
                node,
                symbol=s[0],
                message=f"module-level `{s[0]}` is rebound inside a function",
                confidence=Confidence.MEDIUM,
            )

    def check_augassign(self, node: ast.AugAssign, *, in_finally: bool) -> None:
        t = node.target
        if isinstance(t, ast.Subscript):
            s = self.shared_slot(t.value)
            if s is not None:
                self._record_slot(s[0], node.lineno, in_finally)
                self.emit(
                    "FT105",
                    node,
                    symbol=s[0],
                    message=f"`{ast.unparse(t)} {_OP_SYMBOL.get(type(node.op).__name__, '?')}=` is a non-atomic read-modify-write on shared state",
                    confidence=Confidence.HIGH,
                )
            return
        s = self.shared_slot(t)
        if s is None:
            return
        self._record_slot(s[0], node.lineno, in_finally)
        if s[1] in _MUTABLE_KINDS:
            self.emit(
                "FT104",
                node,
                symbol=s[0],
                message=f"in-place extension of shared container `{s[0]}`",
                confidence=Confidence.MEDIUM,
            )
        else:
            self.emit(
                "FT105",
                node,
                symbol=s[0],
                message=f"`{s[0]} {_OP_SYMBOL.get(type(node.op).__name__, '?')}=` is a non-atomic read-modify-write on shared state",
                confidence=Confidence.HIGH,
            )

    def check_delete(self, t: ast.AST, node: ast.stmt) -> None:
        if isinstance(t, ast.Subscript):
            base = dotted(t.value)
            if base in PROCESS_GLOBAL_SUBSCRIPTS:
                self.emit("FT106", node, symbol=base or "",
                          message=f"deleting from `{base}` mutates {PROCESS_GLOBAL_SUBSCRIPTS[base]}",
                          confidence=Confidence.HIGH)
                return
            s = self.shared_slot(t.value)
            if s is not None and self.lock_depth == 0:
                self.emit("FT104", node, symbol=s[0],
                          message=f"unsynchronised deletion from shared `{s[0]}`",
                          confidence=Confidence.MEDIUM)

    def check_for(self, node: ast.For | ast.AsyncFor) -> None:
        s_iter = node.iter
        if isinstance(s_iter, ast.Name) and not self._is_local(s_iter.id):
            if s_iter.id in self.index.generators or self.index.kind_of(s_iter.id) == "iterator":
                self.emit("FT109", node, symbol=s_iter.id,
                          message=f"iterating shared iterator `{s_iter.id}` from a function",
                          confidence=Confidence.MEDIUM)
                return
        s = self.shared_slot(s_iter)
        if s is not None and s[1] in _MUTABLE_KINDS and self.lock_depth == 0:
            # iterating a container another thread may mutate -> RuntimeError
            self.emit("FT104", node, symbol=s[0],
                      message=f"iterating shared `{s[0]}` while other threads may mutate it raises RuntimeError",
                      confidence=Confidence.LOW, severity=Severity.LOW)

    def check_expr(self, node: ast.AST) -> None:
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            fn = dotted(sub.func)
            if fn is None:
                continue
            if fn in PROCESS_GLOBALS:
                self.emit("FT106", sub, symbol=fn,
                          message=f"`{fn}()` mutates {PROCESS_GLOBALS[fn]}",
                          confidence=Confidence.HIGH)
                continue
            short = fn.split(".")[-1]
            if short in PROCESS_GLOBALS and "." not in fn:
                self.emit("FT106", sub, symbol=fn,
                          message=f"`{fn}()` mutates {PROCESS_GLOBALS[short]}",
                          confidence=Confidence.MEDIUM)
                continue
            if isinstance(sub.func, ast.Attribute) and sub.func.attr in MUTATING_METHODS:
                s = self.shared_slot(sub.func.value)
                if s is not None and s[1] in _MUTABLE_KINDS:
                    if sub.func.attr in ATOMIC_METHODS and s[1] != "sequence":
                        continue
                    sc = self._scope()
                    if sc and sc.is_finalizer:
                        self.emit("FT113", sub, symbol=s[0],
                                  message=f"finalizer mutates shared `{s[0]}`",
                                  confidence=Confidence.MEDIUM)
                    elif sc and sc.class_info and s[0].startswith(sc.class_info.name + "."):
                        self.emit("FT110", sub, symbol=s[0],
                                  message=f"`{s[0]}.{sub.func.attr}()` mutates a class-level container shared by every instance",
                                  confidence=Confidence.HIGH)
                    elif self.lock_depth == 0:
                        self.emit("FT104", sub, symbol=s[0],
                                  message=f"`{s[0]}.{sub.func.attr}()` mutates shared state without a lock",
                                  confidence=Confidence.MEDIUM if sub.func.attr not in ATOMIC_METHODS else Confidence.LOW)
            if short == "next" and fn == "next" and sub.args:
                a = sub.args[0]
                if isinstance(a, ast.Name) and not self._is_local(a.id):
                    if a.id in self.index.generators or self.index.kind_of(a.id) == "iterator":
                        self.emit("FT109", sub, symbol=a.id,
                                  message=f"`next({a.id})` advances a shared iterator",
                                  confidence=Confidence.HIGH)

    def _reads_slot(self, value: ast.AST, slot: str) -> bool:
        for sub in ast.walk(value):
            if isinstance(sub, (ast.Name, ast.Attribute)):
                s = self.shared_slot(sub)
                if s and s[0] == slot:
                    return True
        return False

    def _record_slot(self, slot: str, line: int, in_finally: bool) -> None:
        self.slot_assigns.setdefault(slot, []).append((line, in_finally))


class _FakeNode(ast.AST):
    """Carries a line number for whole-function findings."""

    _fields = ()

    def __init__(self, lineno: int) -> None:
        self.lineno = lineno
        self.col_offset = 0
        self.end_lineno = lineno


# --------------------------------------------------------------------------- #
# cached_property pass
# --------------------------------------------------------------------------- #

def _cached_property_findings(tree: ast.Module, path: str, lines: list[str]) -> list[Finding]:
    out: list[Finding] = []
    rule = RULES["FT114"]
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        decos = {(dotted(d) or "").split(".")[-1] for d in node.decorator_list}
        if "cached_property" not in decos:
            continue
        risky: list[str] = []
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                fn = dotted(sub.func)
                if fn and fn.split(".")[-1] in _SIDE_EFFECT_CALLS:
                    risky.append(fn)
            elif isinstance(sub, (ast.Assign,)):
                for t in sub.targets:
                    if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "self":
                        risky.append(f"self.{t.attr} = ...")
        if not risky:
            continue
        out.append(
            Finding(
                rule="FT114",
                name=rule.name,
                message=f"cached_property `{node.name}` has side effects ({', '.join(sorted(set(risky))[:3])}) and can run twice",
                path=path,
                line=node.lineno,
                col=node.col_offset,
                end_line=node.end_lineno or node.lineno,
                severity=rule.severity,
                confidence=Confidence.MEDIUM,
                symbol=node.name,
                function=node.name,
                snippet="\n".join(lines[node.lineno - 1 : min(node.lineno + 5, node.end_lineno or node.lineno)]),
                why=rule.why,
                fix=rule.fix,
            )
        )
    return out


# --------------------------------------------------------------------------- #
# entry points
# --------------------------------------------------------------------------- #

def _suppressions(source: str) -> dict[int, set[str]]:
    out: dict[int, set[str]] = {}
    try:
        toks = tokenize.generate_tokens(io.StringIO(source).readline)
        for tok in toks:
            if tok.type != tokenize.COMMENT:
                continue
            m = _SUPPRESS_RE.search(tok.string)
            if not m:
                continue
            codes = m.group("codes")
            if codes:
                found = {c.strip() for c in codes.split(",") if c.strip().startswith("FT")}
                if not found:
                    continue
                out.setdefault(tok.start[0], set()).update(found)
            else:
                out[tok.start[0]] = set()
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return out


def scan_source(source: str, path: str) -> list[Finding]:
    """Analyse one module's source text."""
    tree = ast.parse(source, filename=path)
    lines = source.splitlines()
    index = ModuleIndex(tree)
    walker = _FunctionWalker(path, lines, index, _suppressions(source))
    walker.visit_module(tree)
    findings = walker.findings + _cached_property_findings(tree, path, lines)
    findings = _subsume(findings)
    # de-duplicate: same rule+line+symbol
    seen: set[tuple] = set()
    unique: list[Finding] = []
    for f in findings:
        key = (f.rule, f.line, f.symbol, f.col)
        if key in seen:
            continue
        seen.add(key)
        unique.append(f)
    return unique


#: A precise diagnosis hides the generic one it already implies, so a single
#: racy cache lookup is reported once rather than three times.
_SUBSUMES: dict[str, frozenset[str]] = {
    "FT102": frozenset({"FT101", "FT104"}),
    "FT103": frozenset({"FT104", "FT101"}),
    "FT105": frozenset({"FT101", "FT104"}),
    "FT107": frozenset({"FT101", "FT104", "FT106"}),
    "FT108": frozenset({"FT101", "FT102", "FT104"}),
    "FT110": frozenset({"FT101", "FT104"}),
}


def _subsume(findings: list[Finding]) -> list[Finding]:
    ranges: list[tuple[frozenset[str], str, int, int]] = []
    for f in findings:
        covered = _SUBSUMES.get(f.rule)
        if covered:
            ranges.append((covered, f.symbol, f.line, max(f.end_line, f.line)))
    if not ranges:
        return findings
    out: list[Finding] = []
    for f in findings:
        hidden = any(
            f.rule in covered
            and f.symbol == symbol
            and start <= f.line <= end
            for covered, symbol, start, end in ranges
        )
        if not hidden:
            out.append(f)
    return out


def scan_file(path: str, root: str = "") -> tuple[list[Finding], str | None]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            source = fh.read()
    except OSError as exc:
        return [], str(exc)
    rel = os.path.relpath(path, root) if root else path
    try:
        return scan_source(source, rel), None
    except SyntaxError as exc:
        return [], f"SyntaxError: {exc}"
    except RecursionError:
        return [], "RecursionError while walking AST"


_SKIP_DIRS = {
    ".git", ".hg", "__pycache__", ".tox", ".nox", ".venv", "venv", "node_modules",
    ".mypy_cache", ".pytest_cache", "build", "dist", ".eggs", "site-packages",
}


def iter_python_files(root: str, include_tests: bool = False) -> list[str]:
    out: list[str] = []
    if os.path.isfile(root):
        return [root]
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in _SKIP_DIRS and not d.endswith(".egg-info")
            and (include_tests or d not in ("tests", "test", "testing", "_test"))
        ]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            if not include_tests and (fn.startswith("test_") or fn.endswith("_test.py") or fn == "conftest.py"):
                continue
            out.append(os.path.join(dirpath, fn))
    return sorted(out)


def scan_tree(root: str, include_tests: bool = False, min_severity: Severity = Severity.LOW) -> ScanResult:
    start = time.perf_counter()
    result = ScanResult(target=root)
    files = iter_python_files(root, include_tests)
    for path in files:
        findings, err = scan_file(path, root if os.path.isdir(root) else os.path.dirname(root))
        result.files_scanned += 1
        if err:
            result.files_failed.append((os.path.relpath(path, root) if os.path.isdir(root) else path, err))
            continue
        for f in findings:
            if f.severity.rank >= min_severity.rank:
                result.findings.append(f)
    native_findings, native_sources = scan_native_sources(root)
    result.native_sources = native_sources
    for f in native_findings:
        if f.severity.rank >= min_severity.rank:
            result.findings.append(f)
    result.duration_s = time.perf_counter() - start
    return result
