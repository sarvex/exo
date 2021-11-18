import ast as pyast
import inspect
import types
from weakref import WeakKeyDictionary

from .API_types import ProcedureBase
from .LoopIR import LoopIR, T, UAST, LoopIR_Do
from .LoopIR_compiler import run_compile, compile_to_strings
from .LoopIR_interpreter import run_interpreter
from .LoopIR_scheduling import (Schedules, name_plus_count,
                                iter_name_to_pattern,
                                nested_iter_names_to_pattern)
from .LoopIR_unification import DoReplace, UnificationError
from .configs import Config
from .effectcheck import InferEffects, CheckEffects
from .memory import Memory
from .parse_fragment import parse_fragment
from .pattern_match import match_pattern, get_match_no
from .prelude import *
from .pyparser import get_ast_from_python, Parser, get_src_locals
from .reflection import LoopIR_to_QAST
from .typecheck import TypeChecker

# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
#   proc provenance tracking

# Moved to new file
from .proc_eqv import (decl_new_proc, derive_proc,
                       assert_eqv_proc, check_eqv_proc)

# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Top-level decorator


def proc(f, _instr=None, _testing=None):
    if not isinstance(f, types.FunctionType):
        raise TypeError("@proc decorator must be applied to a function")

    body, getsrcinfo = get_ast_from_python(f)
    assert isinstance(body, pyast.FunctionDef)

    parser = Parser(body, f.__globals__,
                    get_src_locals(depth=3 if _instr else 2),
                    getsrcinfo, instr=_instr, as_func=True)
    return Procedure(parser.result(), _testing=_testing)


def instr(instruction, _testing=None):
    if not isinstance(instruction, str):
        raise TypeError("@instr decorator must be @instr(<your instuction>)")

    def inner(f):
        if not isinstance(f, types.FunctionType):
            raise TypeError("@instr decorator must be applied to a function")

        return proc(f, _instr=instruction, _testing=_testing)

    return inner


def config(_cls=None, *, readwrite=True):
    def parse_config(cls):
        if not inspect.isclass(cls):
            raise TypeError("@config decorator must be applied to a class")

        body, getsrcinfo = get_ast_from_python(cls)
        assert isinstance(body, pyast.ClassDef)

        parser = Parser(body, {}, get_src_locals(depth=2), getsrcinfo,
                        as_config=True)
        return Config(*parser.result(), not readwrite)

    if _cls is None:
        return parse_config
    else:
        return parse_config(_cls)


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
#   iPython Display Object

class MarkDownBlob:
    def __init__(self, mkdwn_str):
        self.mstr = mkdwn_str

    def _repr_markdown_(self):
        return self.mstr

class FindBefore(LoopIR_Do):
    def __init__(self, proc, stmt):
        self.stmt = stmt
        self.result = None
        super().__init__(proc)

    def result(self):
        return self.result

    def do_stmts(self, stmts):
        prev = None
        for s in stmts:
            if s == self.stmt:
                self.result = prev
                return
            else:
                self.do_s(s)
                prev = s

class FindDup(LoopIR_Do):
    def __init__(self, proc):
        self.result = False
        self.env = []
        super().__init__(proc)

    def result(self):
        return self.result

    def do_s(self, s):
        for e in self.env:
            if s is e:
                self.result = True
                print(s)
        self.env.append(s)

        super().do_s(s)


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
#   Procedure Objects


def compile_procs(proc_list, path, c_file, h_file):
    assert isinstance(proc_list, list)
    assert all(isinstance(p, Procedure) for p in proc_list)
    run_compile([p._loopir_proc for p in proc_list], path, c_file, h_file)


class Procedure(ProcedureBase):
    def __init__(self, proc, _testing=None, _provenance_eq_Procedure=None):
        super().__init__()

        if isinstance(proc, LoopIR.proc):
            self._loopir_proc = proc
        else:
            assert isinstance(proc, UAST.proc)

            self._uast_proc = proc
            if _testing != "UAST":
                self._loopir_proc = TypeChecker(proc).get_loopir()
                self._loopir_proc = InferEffects(self._loopir_proc).result()
                CheckEffects(self._loopir_proc)


        # add this procedure into the equivalence tracking mechanism
        if _testing != "UAST":
            if _provenance_eq_Procedure:
                derive_proc(_provenance_eq_Procedure._loopir_proc,
                            self._loopir_proc)
            else:
                decl_new_proc(self._loopir_proc)

    def __str__(self):
        if hasattr(self,'_loopir_proc'):
            return str(self._loopir_proc)
        else:
            return str(self._uast_proc)

    def _repr_markdown_(self):
        return ("```python\n"+self.__str__()+"\n```")

    def INTERNAL_proc(self):
        return self._loopir_proc

    # -------------------------------- #
    #     introspection operations
    # -------------------------------- #

    def check_effects(self):
        self._loopir_proc = InferEffects(self._loopir_proc).result()
        CheckEffects(self._loopir_proc)

    def name(self):
        return self._loopir_proc.name

    def show_effects(self):
        return str(self._loopir_proc.eff)

    def show_effect(self, stmt_pattern):
        stmt        = self._find_stmt(stmt_pattern)
        return str(stmt.eff)

    def is_instr(self):
        return self._loopir_proc.instr is not None

    def get_instr(self):
        return self._loopir_proc.instr

    def get_ast(self, pattern=None):
        if pattern is None:
            return LoopIR_to_QAST(self._loopir_proc).result()
        else:
            # do pattern matching
            body        = self._loopir_proc.body
            match_no    = get_match_no(pattern)
            match       = match_pattern(body, pattern, call_depth=1)

            # convert matched sub-trees to QAST
            assert isinstance(match, list)
            if len(match) == 0:
                return None
            elif isinstance(match[0], LoopIR.expr):
                results = [ LoopIR_to_QAST(e).result() for e in match ]
            elif isinstance(match[0], list):
                # statements
                assert all( isinstance(s, LoopIR.stmt)
                            for stmts in match
                            for s in stmts )
                results = [ LoopIR_to_QAST(stmts).result()
                            for stmts in match ]
            else:
                assert False, "bad case"

            # modulate the return type depending on whether this
            # was a query for a specific match or for all matches
            if match_no is None:
                return results
            else:
                assert len(results) == 1
                return results[0]

    # ---------------------------------------------- #
    #     execution / interpretation operations
    # ---------------------------------------------- #

    def show_c_code(self):
        return MarkDownBlob("```c\n"+self.c_code_str()+"\n```")

    def c_code_str(self):
        decls, defns = compile_to_strings("c_code_str", [self._loopir_proc])
        return decls + '\n' + defns

    def compile_c(self, directory, filename):
        run_compile([self._loopir_proc], directory,
                    (filename + ".c"), (filename + ".h"))

    def interpret(self, **kwargs):
        run_interpreter(self._loopir_proc, kwargs)

    # ------------------------------- #
    #     scheduling operations
    # ------------------------------- #

    def simplify(self):
        '''
        Simplify the code in the procedure body. Currently only performs
        constant folding
        '''
        p = self._loopir_proc
        p = Schedules.DoSimplify(p).result()
        return Procedure(p, _provenance_eq_Procedure=self)

    def rename(self, name):
        if not is_valid_name(name):
            raise TypeError(f"'{name}' is not a valid name")
        p = self._loopir_proc
        p = LoopIR.proc( name, p.args, p.preds, p.body,
                         p.instr, p.eff, p.srcinfo )
        return Procedure(p, _provenance_eq_Procedure=self)

    def has_dup(self):
        return FindDup(self._loopir_proc).result

    def make_instr(self, instr):
        if not isinstance(instr, str):
            raise TypeError("expected an instruction macro "
                            "(Python string with {} escapes "
                            "as an argument")
        p = self._loopir_proc
        p = LoopIR.proc(p.name, p.args, p.preds, p.body,
                        instr, p.eff, p.srcinfo)
        return Procedure(p, _provenance_eq_Procedure=self)

    def unsafe_assert_eq(self, other_proc):
        if not isinstance(other_proc, Procedure):
            raise TypeError("expected a procedure as argument")
        assert_eqv_proc(self._loopir_proc, other_proc._loopir_proc)
        return self

    def partial_eval(self, *args, **kwargs):
        if kwargs and args:
            raise ValueError("Must provide EITHER ordered OR named arguments")
        if not kwargs and not args:
            # Nothing to do if empty partial eval
            return self

        p = self._loopir_proc

        if args:
            if len(args) > len(p.args):
                raise TypeError(f"expected no more than {len(p.args)} "
                                f"arguments, but got {len(args)}")
            kwargs = {arg.name: val for arg, val in zip(p.args, args)}
        else:
            # Get the symbols corresponding to the names
            params_map = {sym.name.name(): sym.name for sym in p.args}
            kwargs = {params_map[k]: v for k, v in kwargs.items()}

        p = Schedules.DoPartialEval(p, kwargs).result()
        return Procedure(p)  # No provenance because signature changed

    def set_precision(self, name, typ_abbreviation):
        name, count = name_plus_count(name)
        _shorthand = {
            'R':    T.R,
            'f32':  T.f32,
            'f64':  T.f64,
            'i8':   T.int8,
            'i32':  T.int32,
        }
        if typ_abbreviation in _shorthand:
            typ = _shorthand[typ_abbreviation]
        else:
            raise TypeError("expected second argument to set_precision() "
                            "to be a valid primitive type abbreviation")

        loopir = self._loopir_proc
        loopir = Schedules.SetTypAndMem(loopir, name, count,
                                        basetyp=typ).result()
        return Procedure(loopir, _provenance_eq_Procedure=self)

    def set_window(self, name, is_window):
        name, count = name_plus_count(name)
        if not isinstance(is_window, bool):
            raise TypeError("expected second argument to set_window() to "
                            "be a boolean")

        loopir = self._loopir_proc
        loopir = Schedules.SetTypAndMem(loopir, name, count,
                                        win=is_window).result()
        return Procedure(loopir, _provenance_eq_Procedure=self)

    def set_memory(self, name, memory_type):
        name, count = name_plus_count(name)
        if not issubclass(memory_type, Memory):
            raise TypeError("expected second argument to set_memory() to "
                            "be a Memory object")

        loopir = self._loopir_proc
        loopir = Schedules.SetTypAndMem(loopir, name, count,
                                        mem=memory_type).result()
        return Procedure(loopir, _provenance_eq_Procedure=self)

    def _find_stmt(self, stmt_pattern, call_depth=2, default_match_no=0, body=None):
        body = self._loopir_proc.body if body is None else body
        stmt_lists  = match_pattern(body, stmt_pattern,
                                    call_depth=call_depth,
                                    default_match_no=default_match_no)
        if len(stmt_lists) == 0 or len(stmt_lists[0]) == 0:
            raise TypeError("failed to find statement")
        elif default_match_no is None:
            return [ s[0] for s in stmt_lists ]
        else:
            return stmt_lists[0][0]

    def _find_callsite(self, call_site_pattern):
        call_stmt   = self._find_stmt(call_site_pattern, call_depth=3)
        if not isinstance(call_stmt, LoopIR.Call):
            raise TypeError("pattern did not describe a call-site")

        return call_stmt


    def bind_config(self, var_pattern, config, field):
        # Check if config and field are valid here
        if not isinstance(config, Config):
            raise TypeError("Did not pass a config object")
        if not isinstance(field, str):
            raise TypeError("Did not pass a config field string")
        if not config.has_field(field):
            raise TypeError(f"expected '{field}' to be a field "
                            f"in config '{config.name()}'")

        body    = self._loopir_proc.body
        matches = match_pattern(body, var_pattern, call_depth=1)

        if not matches:
            raise TypeError("failed to find expression")
        # Can only bind single read atm. How to reason about scoping?
        if len(matches) != 1 or not isinstance(matches[0], LoopIR.Read):
            raise TypeError(f"expected a single Read")

        # Check that the type of config field and read are the same
        if matches[0].type != config.lookup(field)[1]:
            raise TypeError("types of config and a read variable does "
                            "not match")

        loopir = self._loopir_proc
        loopir = Schedules.DoBindConfig(loopir, config, field, matches[0]).result()

        return Procedure(loopir, _provenance_eq_Procedure=self)

    def data_reuse(self, buf_pattern, replace_pattern):
        if not isinstance(buf_pattern, str):
            raise TypeError("expected first argument to be alloc pattern")
        if not isinstance(replace_pattern, str):
            raise TypeError("expected second argument to be alloc that you want to replace")

        buf_s = self._find_stmt(buf_pattern)
        rep_s = self._find_stmt(replace_pattern)

        if not isinstance(buf_s, LoopIR.Alloc) or not isinstance(rep_s, LoopIR.Alloc):
            raise TypeError("expected both arguments to be alloc pattern,"+
                            f" got {type(buf_s)} and {type(rep_s)}")

        loopir = self._loopir_proc
        loopir = Schedules.DoDataReuse(loopir, buf_s, rep_s).result()

        return Procedure(loopir, _provenance_eq_Procedure=self)
    
    
    def configwrite_after(self, stmt_pattern, config, field, var_pattern):
        if not isinstance(config, Config):
            raise TypeError("Did not pass a config object")
        if not isinstance(field, str):
            raise TypeError("Did not pass a config field string")
        if not config.has_field(field):
            raise TypeError(f"expected '{field}' to be a field "
                            f"in config '{config.name()}'")

        if not isinstance(var_pattern, str):
            raise TypeError("expected second argument to be a string var")

        stmt     = self._find_stmt(stmt_pattern)
        loopir   = self._loopir_proc
        var_expr = parse_fragment(loopir, var_pattern, stmt)
        assert isinstance(var_expr, LoopIR.expr)
        loopir   = Schedules.DoConfigWriteAfter(loopir, stmt, config, field, var_expr).result()

        return Procedure(loopir, _provenance_eq_Procedure=self)

    def inline_window(self, stmt_pattern):
        if not isinstance(stmt_pattern, str):
            raise TypeError("Did not pass a stmt string")

        stmt   = self._find_stmt(stmt_pattern, default_match_no=None)
        loopir = self._loopir_proc
        loopir = Schedules.DoInlineWindow(loopir, stmt[0]).result()

        return Procedure(loopir, _provenance_eq_Procedure=self)


    def split(self, split_var, split_const, out_vars,
              tail='guard', perfect=False):
        if not isinstance(split_var, str):
            raise TypeError("expected first arg to be a string")
        if not is_pos_int(split_const):
            raise TypeError("expected second arg to be a positive integer")
        if split_const == 1:
            raise TypeError("why are you trying to split by 1?")
        if not isinstance(out_vars,list) and not isinstance(out_vars, tuple):
            raise TypeError("expected third arg to be a list or tuple")
        if len(out_vars) != 2:
            raise TypeError("expected third arg list/tuple to have length 2")
        if not all(is_valid_name(s) for s in out_vars):
            raise TypeError("expected third arg to be a list/tuple of two "
                            "valid name strings")
        if tail not in ('cut', 'guard', 'cut_and_guard'):
            raise ValueError(f'unknown tail strategy "{tail}"')

        pattern   = iter_name_to_pattern(split_var)
        stmts_len = len(self._find_stmt(pattern, default_match_no=None))
        loopir = self._loopir_proc
        for i in range(0, stmts_len):
            s = self._find_stmt(pattern, body=loopir.body)
            loopir  = Schedules.DoSplit(loopir, s, quot=split_const,
                                        hi=out_vars[0], lo=out_vars[1],
                                        tail=tail,
                                        perfect=perfect).result()

        return Procedure(loopir, _provenance_eq_Procedure=self)

    def expand_dim(self, stmt_pat, alloc_dim_pat, indexing_pat):
        if not isinstance(stmt_pat, str):
            raise TypeError("expected first arg to be a string")
        if not isinstance(alloc_dim_pat, str):
            raise TypeError("expected second arg to be a string")
        if not isinstance(indexing_pat, str):
            raise TypeError("expected second arg to be a string")

        stmts_len = len(self._find_stmt(stmt_pat, default_match_no=None))
        loopir = self._loopir_proc
        for i in range(0, stmts_len):
            s = self._find_stmt(stmt_pat, body=loopir.body, default_match_no=None)[i]
            alloc_dim = parse_fragment(loopir, alloc_dim_pat, s)
            indexing  = parse_fragment(loopir, indexing_pat, s)
            loopir = Schedules.DoExpandDim(loopir, s, alloc_dim, indexing).result()

        return Procedure(loopir, _provenance_eq_Procedure=self)

    def add_unsafe_guard(self, stmt_pat, var_pattern):
        if not isinstance(stmt_pat, str):
            raise TypeError("expected first arg to be a string")
        if not isinstance(var_pattern, str):
            raise TypeError("expected second arg to be a string")

        stmt = self._find_stmt(stmt_pat)
        loopir = self._loopir_proc
        var_expr = parse_fragment(loopir, var_pattern, stmt)

        loopir = Schedules.DoAddUnsafeGuard(loopir, stmt, var_expr).result()

        return Procedure(loopir, _provenance_eq_Procedure=self)

    def add_ifelse(self, stmt_pat, var_pattern):
        if not isinstance(stmt_pat, str):
            raise TypeError("expected first arg to be a string")
        if not isinstance(var_pattern, str):
            raise TypeError("expected second arg to be a string")

        stmt = self._find_stmt(stmt_pat)
        loopir = self._loopir_proc
        var_expr = parse_fragment(loopir, var_pattern, stmt)

        loopir = Schedules.DoAddIfElse(loopir, stmt, var_expr).result()

        return Procedure(loopir, _provenance_eq_Procedure=self)

    def add_guard(self, stmt_pat, iter_pat, value):
        if not isinstance(stmt_pat, str):
            raise TypeError("expected first arg to be a string")
        if not isinstance(iter_pat, str):
            raise TypeError("expected second arg to be a string")
        if not isinstance(value, int):
            raise TypeError("expected third arg to be an int")
        # TODO: refine this analysis or re-think the directive...
        #  this is making sure that the condition will guarantee that the
        #  guarded statement runs on the first iteration
        if value != 0:
            raise TypeError("expected third arg to be 0")

        iter_pat = iter_name_to_pattern(iter_pat)
        iter_pat = self._find_stmt(iter_pat)
        if not isinstance(iter_pat, LoopIR.Seq):
            raise TypeError("expected the loop to be sequential")
        stmts = self._find_stmt(stmt_pat, default_match_no=None)
        loopir = self._loopir_proc
        for s in stmts:
            loopir = Schedules.DoAddGuard(loopir, s, iter_pat, value).result()

        return Procedure(loopir, _provenance_eq_Procedure=self)

    def bound_and_guard(self, loop):
        """
        Replace
          for i in par(0, e): ...
        with
          for i in par(0, c):
            if i < e: ...
        where c is the tightest constant bound on e

        This currently only works when e is of the form x % n
        """
        if not isinstance(loop, str):
            raise TypeError("expected loop pattern")

        loop = self._find_stmt(loop)
        loopir = Schedules.DoBoundAndGuard(self._loopir_proc, loop).result()
        return Procedure(loopir, _provenance_eq_Procedure=self)


    def fuse_loop(self, loop1, loop2):
        if not isinstance(loop1, str):
            raise TypeError("expected first arg to be a string")
        if not isinstance(loop2, str):
            raise TypeError("expected second arg to be a string")

        loop1 = self._find_stmt(loop1)
        loop2 = self._find_stmt(loop2)

        if not isinstance(loop1, (LoopIR.ForAll, LoopIR.Seq)):
            raise TypeError("expected loop to be par or seq loop")
        if type(loop1) is not type(loop2):
            raise TypeError("expected loop type to match")
        
        loopir = self._loopir_proc
        loopir = Schedules.DoFuseLoop(loopir, loop1, loop2).result()
      
        return Procedure(loopir, _provenance_eq_Procedure=self)

    def add_loop(self, stmt, var, hi):
        if not isinstance(stmt, str):
            raise TypeError("expected first arg to be a string")
        if not isinstance(var, str):
            raise TypeError("expected second arg to be a string")
        if not isinstance(hi, int):
            raise TypeError("currently, only constant bound is supported")

        stmt = self._find_stmt(stmt)
        loopir = self._loopir_proc
        loopir = Schedules.DoAddLoop(loopir, stmt, var, hi).result()

        return Procedure(loopir, _provenance_eq_Procedure=self)


    def merge_guard(self, stmt1, stmt2):
        if not isinstance(stmt1, str):
            raise TypeError("expected first arg to be a string")
        if not isinstance(stmt2, str):
            raise TypeError("expected second arg to be a string")

        stmt1 = self._find_stmt(stmt1)
        stmt2 = self._find_stmt(stmt2)
        if not isinstance(stmt1, LoopIR.If):
            raise ValueError('stmt1 did not resolve to if stmt')
        if not isinstance(stmt2, LoopIR.If):
            raise ValueError('stmt2 did not resolve to if stmt')
        loopir = self._loopir_proc
        loopir = Schedules.DoMergeGuard(loopir, stmt1, stmt2).result()

        return Procedure(loopir, _provenance_eq_Procedure=self)

    def insert_pass(self, pat: str):
        if not isinstance(pat, str):
            raise TypeError('expected first argument to be a pattern in string')

        stmt = self._find_stmt(pat)
        loopir = Schedules.DoInsertPass(self._loopir_proc, stmt).result()
        return Procedure(loopir, _provenance_eq_Procedure=self)

    def delete_pass(self):
        loopir = self._loopir_proc
        loopir = Schedules.DoDeletePass(loopir).result()

        return Procedure(loopir, _provenance_eq_Procedure=self)

    def reorder_before(self, pat):
        if not isinstance(pat, str):
            raise TypeError("expected first arg to be a pattern in string")

        second_stmt = self._find_stmt(pat)

        loopir = self._loopir_proc
        first_stmt = FindBefore(loopir, second_stmt).result

        if first_stmt is None:
            raise TypeError("expected pattern to be after some statements")

        loopir = Schedules.DoReorderStmt(loopir, first_stmt, second_stmt).result()

        return Procedure(loopir, _provenance_eq_Procedure=self)

    def delete_config(self, stmt_pat):
        if not isinstance(stmt_pat, str):
            raise TypeError("expected first arg to be a pattern in string")

        stmt = self._find_stmt(stmt_pat, default_match_no=None)
        assert len(stmt) == 1 #Don't want to accidentally delete other configs

        loopir = self._loopir_proc
        loopir = Schedules.DoDeleteConfig(loopir, stmt[0]).result()

        return Procedure(loopir, _provenance_eq_Procedure=self)

    def reorder_stmts(self, first_pat, second_pat):
        if not isinstance(first_pat, str):
            raise TypeError("expected first arg to be a pattern in string")
        if not isinstance(second_pat, str):
            raise TypeError("expected second arg to be a pattern in string")

        first_stmt = self._find_stmt(first_pat, default_match_no=None)
        second_stmt = self._find_stmt(second_pat, default_match_no=None)

        if not first_stmt or not second_stmt:
            raise TypeError("failed to find stmt")
        if len(first_stmt) != 1 or len(second_stmt) != 1:
            raise TypeError(
                "expected stmt patterns to be specified s.t. it has "
                "only one matching")

        loopir = self._loopir_proc
        loopir = Schedules.DoReorderStmt(loopir, first_stmt[0], second_stmt[0]).result()

        return Procedure(loopir, _provenance_eq_Procedure=self)
        
    def lift_if(self, if_pattern, n_lifts=1):
        if not isinstance(if_pattern, str):
            raise TypeError("expected first arg to be a string")
        if not isinstance(n_lifts, int):
            raise TypeError("expected second arg to be a int")

        stmts_len = len(self._find_stmt(if_pattern, default_match_no=None))
        loopir = self._loopir_proc
        for i in range(0, stmts_len):
            s = self._find_stmt(if_pattern, body=loopir.body, default_match_no=None)[i]
            loopir = Schedules.DoLiftIf(loopir, s, n_lifts).result()

        return Procedure(loopir, _provenance_eq_Procedure=self)

    def assert_if(self, if_pattern, cond):
        if not isinstance(if_pattern, str):
            raise TypeError("expected first arg to be a string")
        if not isinstance(cond, bool):
            raise TypeError("expected second arg to be a bool")

        stmts_len = len(self._find_stmt(if_pattern, default_match_no=None))
        loopir = self._loopir_proc
        for i in range(0, stmts_len):
            s = self._find_stmt(if_pattern, body=loopir.body)
            loopir = Schedules.DoAssertIf(loopir, s, cond).result()

        return Procedure(loopir, _provenance_eq_Procedure=self)

    def partition_loop(self, var_pattern, num):
        if not isinstance(var_pattern, str):
            raise TypeError("expected first arg to be a string")
        if not isinstance(num, int):
            raise TypeError("expected second arg to be a int")

        pattern   = iter_name_to_pattern(var_pattern)
        stmts_len = len(self._find_stmt(pattern, default_match_no=None))
        loopir = self._loopir_proc
        for i in range(0, stmts_len):
            s = self._find_stmt(pattern, body=loopir.body)
            loopir  = Schedules.DoPartitionLoop(loopir, s, num).result()

        return Procedure(loopir, _provenance_eq_Procedure=self)

    def reorder(self, out_var, in_var):
        if not isinstance(out_var, str):
            raise TypeError("expected first arg to be a string")
        elif not is_valid_name(in_var):
            raise TypeError("expected second arg to be a valid name string")

        pattern     = nested_iter_names_to_pattern(out_var, in_var)
        stmts_len = len(self._find_stmt(pattern, default_match_no=None))
        loopir = self._loopir_proc
        for i in range(0, stmts_len):
            s = self._find_stmt(pattern, body=loopir.body)
            loopir  = Schedules.DoReorder(loopir, s).result()

        return Procedure(loopir, _provenance_eq_Procedure=self)

    def unroll(self, unroll_var):
        if not isinstance(unroll_var, str):
            raise TypeError("expected first arg to be a string")

        pattern   = iter_name_to_pattern(unroll_var)
        stmts_len = len(self._find_stmt(pattern, default_match_no=None))
        if stmts_len == 0:
            raise ValueError("failed to find Assign or Reduce")
        loopir = self._loopir_proc
        for i in range(0, stmts_len):
            s = self._find_stmt(pattern, body=loopir.body)
            loopir  = Schedules.DoUnroll(loopir, s).result()

        return Procedure(loopir, _provenance_eq_Procedure=self)

    def replace(self, subproc, pattern):
        if not isinstance(subproc, Procedure):
            raise TypeError("expected first arg to be a subprocedure")
        elif not isinstance(pattern, str):
            raise TypeError("expected second arg to be a string")

        body        = self._loopir_proc.body
        stmt_lists  = match_pattern(body, pattern, call_depth=1)
        if len(stmt_lists) == 0:
            raise TypeError("failed to find statement")

        loopir = self._loopir_proc
        for stmt_block in stmt_lists:
            loopir = DoReplace(loopir, subproc._loopir_proc,
                               stmt_block).result()

        return Procedure(loopir, _provenance_eq_Procedure=self)


    def replace_all(self, subproc):
        # TODO: this is a bad implementation, but necessary due to issues in the
        #       implementation of replace above: after a replacement, statements
        #       can be moved in memory, so matches are invalidated. Matching and
        #       replacing ought to be fused instead. This directive would reduce
        #       to a flag on the find/replace.
        assert isinstance(subproc, Procedure)
        assert len(subproc._loopir_proc.body) == 1, \
            "Compound statements not supported"

        patterns = {
            LoopIR.Assign     : '_ = _',
            LoopIR.Reduce     : '_ += _',
            LoopIR.WriteConfig: 'TODO',
            LoopIR.Pass       : 'TODO',
            LoopIR.If         : 'TODO',
            LoopIR.ForAll     : 'for _ in _: _',
            LoopIR.Seq        : 'TODO',
            LoopIR.Alloc      : 'TODO',
            LoopIR.Free       : 'TODO',
            LoopIR.Call       : 'TODO',
            LoopIR.WindowStmt : 'TODO',
        }

        pattern = patterns[type(subproc._loopir_proc.body[0])]

        proc = self
        i = 0
        while True:
            try:
                proc = proc.replace(subproc, f'{pattern} #{i}')
            except TypeError as e:
                if 'failed to find statement' in str(e):
                    return proc
                raise
            except UnificationError:
                i += 1

    def inline(self, call_site_pattern):
        call_stmt = self._find_callsite(call_site_pattern)

        loopir = self._loopir_proc
        loopir = Schedules.DoInline(loopir, call_stmt).result()
        return Procedure(loopir, _provenance_eq_Procedure=self)

    def is_eq(self, proc):
        eqv_set = check_eqv_proc(self._loopir_proc, proc._loopir_proc)
        return (eqv_set == frozenset())

    def call_eqv(self, other_Procedure, call_site_pattern):
        call_stmt = self._find_callsite(call_site_pattern)

        old_proc    = call_stmt.f
        new_proc    = other_Procedure._loopir_proc
        eqv_set     = check_eqv_proc(old_proc, new_proc)
        if eqv_set != frozenset():
            raise TypeError("the procedures were not equivalent")

        loopir      = self._loopir_proc
        loopir      = Schedules.DoCallSwap(loopir, call_stmt,
                                                   new_proc).result()
        return Procedure(loopir, _provenance_eq_Procedure=self)

    def bind_expr(self, new_name, expr_pattern, cse=False):
        if not is_valid_name(new_name):
            raise TypeError("expected first argument to be a valid name")
        body    = self._loopir_proc.body
        matches = match_pattern(body, expr_pattern, call_depth=1)

        if not matches:
            raise TypeError("failed to find expression")

        if any(not isinstance(m, LoopIR.expr) for m in matches):
            raise TypeError("pattern matched, but not to an expression")

        if any(not m.type.is_numeric() for m in matches):
            raise TypeError("only numeric (not index or size) expressions "
                            "can be targeted by bind_expr()")

        loopir = self._loopir_proc
        loopir = Schedules.DoBindExpr(loopir, new_name, matches, cse).result()
        return Procedure(loopir, _provenance_eq_Procedure=self)

    def stage_assn(self, new_name, stmt_pattern):
        if not is_valid_name(new_name):
            raise TypeError("expected first argument to be a valid name")

        stmts_len = len(self._find_stmt(stmt_pattern, default_match_no=None))
        if stmts_len == 0:
            raise ValueError("failed to find Assign or Reduce")
        loopir = self._loopir_proc
        for i in range(0, stmts_len):
            s = self._find_stmt(stmt_pattern, body=loopir.body)
            if not isinstance(s, (LoopIR.Assign, LoopIR.Reduce)):
                raise ValueError(f"expected Assign or Reduce, got {match}")
            loopir = Schedules.DoStageAssn(loopir, new_name, s).result()

        return Procedure(loopir, _provenance_eq_Procedure=self)

    def par_to_seq(self, par_pattern):
        if not self._find_stmt(par_pattern, default_match_no=None):
            raise TypeError('Matched no statements!')

        loopir = self._loopir_proc

        changed_any = True
        while changed_any:
            changed_any = False
            stmts = self._find_stmt(par_pattern, body=loopir.body,
                                    default_match_no=None)
            for s in stmts:
                if not isinstance(s, (LoopIR.ForAll, LoopIR.Seq)):
                    raise TypeError(f'Expected par loop. Got:\n{s}')

                if isinstance(s, LoopIR.ForAll):
                    loopir = Schedules.DoParToSeq(loopir, s).result()
                    changed_any = True
                    break

        return Procedure(loopir, _provenance_eq_Procedure=self)

    def lift_alloc(self, alloc_site_pattern, n_lifts=1, mode='row', size=None,
                   keep_dims=False):
        if not is_pos_int(n_lifts):
            raise TypeError("expected second argument 'n_lifts' to be "
                            "a positive integer")
        if not isinstance(mode, str):
            raise TypeError("expected third argument 'mode' to be "
                            "'row' or 'col'")
        if size and not isinstance(size, int):
            raise TypeError("expected fourth argument 'size' to be "
                            "an integer")

        stmts_len = len(self._find_stmt(alloc_site_pattern, default_match_no=None))
        loopir = self._loopir_proc
        for i in range(0, stmts_len):
            s = self._find_stmt(alloc_site_pattern, body=loopir.body, default_match_no=None)[i]
            if not isinstance(s, LoopIR.Alloc):
                raise TypeError("pattern did not describe an alloc statement")
            loopir  = Schedules.DoLiftAlloc( loopir, s, n_lifts, mode, size, keep_dims).result()

        return Procedure(loopir, _provenance_eq_Procedure=self)


    def double_fission(self, stmt_pat1, stmt_pat2, n_lifts=1):
        if not isinstance(stmt_pat1, str):
            raise TypeError("expected first arg to be a string")
        if not isinstance(stmt_pat2, str):
            raise TypeError("expected second arg to be a string")
        if not is_pos_int(n_lifts):
            raise TypeError("expected third argument 'n_lifts' to be "
                            "a positive integer")

        stmt1  = self._find_stmt(stmt_pat1)
        stmt2  = self._find_stmt(stmt_pat2)
        loopir = self._loopir_proc
        loopir = Schedules.DoDoubleFission(loopir, stmt1, stmt2, n_lifts).result()

        return Procedure(loopir, _provenance_eq_Procedure=self)


    def fission_after(self, stmt_pattern, n_lifts=1):
        if not is_pos_int(n_lifts):
            raise TypeError("expected second argument 'n_lifts' to be "
                            "a positive integer")

        stmts_len = len(self._find_stmt(stmt_pattern, default_match_no=None))
        loopir = self._loopir_proc
        for i in range(0, stmts_len):
            s = self._find_stmt(stmt_pattern, body=loopir.body, default_match_no=None)[i]
            loopir = Schedules.DoFissionLoops(loopir, s, n_lifts).result()

        return Procedure(loopir, _provenance_eq_Procedure=self)

    def extract_method(self, name, stmt_pattern):
        if not is_valid_name(name):
            raise TypeError("expected first argument to be a valid name")
        stmt        = self._find_stmt(stmt_pattern)

        loopir          = self._loopir_proc
        passobj         = Schedules.DoExtractMethod(loopir, name, stmt)
        loopir, subproc = passobj.result(), passobj.subproc()
        return ( Procedure(loopir, _provenance_eq_Procedure=self),
                 Procedure(subproc) )

# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
