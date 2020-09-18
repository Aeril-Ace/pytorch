import collections
import dataclasses
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from typing import List, Tuple

import torch


@dataclasses.dataclass(repr=False, eq=False, frozen=True)
class CallgrindStats(object):
    stmt: str
    setup: str
    number: int
    num_threads: int
    built_with_debug_symbols: bool
    baseline_inclusive_stats: List[Tuple[int, str]]
    baseline_exclusive_stats: List[Tuple[int, str]]
    stmt_inclusive_stats: List[Tuple[int, str]]
    stmt_exclusive_stats: List[Tuple[int, str]]

    def __repr__(self):
        # Pad lines after the first to align properly.
        stmt = self.stmt.replace('\n', '\n' + ' ' * 9)
        setup = self.setup.replace('\n', '\n' + ' ' * 9)
        baseline_ct = self._counts(
            self.baseline_exclusive_stats,include_lookdict_unicode=True)
        baseline_ct_no_unicode = self._counts(
            self.baseline_exclusive_stats, include_lookdict_unicode=False)
        lines = [
            f"{super().__repr__()}",
            f"  stmt:  {stmt}",
            f"  setup: {setup}",
            f"  {self.num_threads} thread{'s' if self.num_threads > 1 else ''}",
            f"{'':>25}All{'':>10}Noisy symbols removed",
            f"  Instructions: {self.counts():>12}{'':>15}{self.counts(include_lookdict_unicode=False):>12}",
            f"  Baseline:     {baseline_ct:>12}{'':>15}{baseline_ct_no_unicode:>12}",
        ]
        if not self.built_with_debug_symbols:
            lines.extend([
                "Warning: PyTorch was not built with debug symbols.",
                "         Source information may be limited. Rebuild with",
                "         REL_WITH_DEB_INFO=1 for more detailed results.",
            ])
        return "\n".join(lines)

    def stats(self, inclusive=False):
        if inclusive:
            first, second = self.stmt_inclusive_stats, self.baseline_inclusive_stats
        else:
            first, second = self.stmt_exclusive_stats, self.baseline_exclusive_stats
        return self._diff(first, second)

    def counts(self, include_lookdict_unicode=True):
        return self._counts(self.stmt_exclusive_stats, include_lookdict_unicode)

    @staticmethod
    def _counts(stats, include_lookdict_unicode):
        return sum(
            c for c, fn in stats
            if include_lookdict_unicode
            or "dictobject.c:lookdict_unicode" not in fn
        )

    def as_standardized(self):
        def strip_prefix(stats):
            counts = collections.defaultdict(int)

            # "Python" and "Objects" come from CPython.
            prefix_truncations = ("build/aten/", "Python/", "Objects/")
            for c, fn in stats:
                fn = re.sub(r"^.+build/\.\./", "build/../", fn)
                for new_prefix in prefix_truncations:
                    fn = re.sub(r"^.+" + new_prefix, new_prefix, fn)
                fn = re.sub(r"\s\[.+\]$", "", fn)
                counts[fn] += c
            return sorted([(c, fn) for fn, c in counts.items() if c], reverse=True)

        return CallgrindStats(
            stmt=self.stmt,
            setup=self.setup,
            number=self.number,
            num_threads=self.num_threads,
            built_with_debug_symbols=self.built_with_debug_symbols,
            baseline_inclusive_stats=strip_prefix(self.baseline_inclusive_stats),
            baseline_exclusive_stats=strip_prefix(self.baseline_exclusive_stats),
            stmt_inclusive_stats=strip_prefix(self.stmt_inclusive_stats),
            stmt_exclusive_stats=strip_prefix(self.stmt_exclusive_stats),
        )

    def delta(self, other, inclusive=False, subtract_baselines=True):
        # FIXME: Once 3.7 is the minimum version, type annotate `other` per PEP 563
        if subtract_baselines:
            first = self.stats(inclusive=inclusive)
            second = other.stats(inclusive=inclusive)
        else:
            if inclusive:
                first, second = self.stmt_inclusive_stats, other.stmt_inclusive_stats
            else:
                first, second = self.stmt_exclusive_stats, other.stmt_exclusive_stats
        return self._diff(first, second)

    @staticmethod
    def _diff(first: List[Tuple[int, str]], second: List[Tuple[int, str]]):
        counts = collections.defaultdict(int, {fn: c for c, fn in first})
        assert len(counts) == len(first)
        for c, fn in second:
            counts[fn] -= c

        return sorted([(c, fn) for fn, c in counts.items() if c], reverse=True)

class _ValgrindWrapper(object):
    def __init__(self):
        self._valgrind_available = not subprocess.run(
            ["which", "valgrind"], capture_output=True).returncode

        self._build_type = None
        build_search = re.search("BUILD_TYPE=(.+),", torch.__config__.show())
        if build_search is not None:
            self._build_type = build_search.groups()[0].split(",")[0]

        self._baseline_cache = {}

    def _validate(self):
        if not self._valgrind_available:
            raise OSError("Could not find `valgrind`")

    def collect_callgrind(self, stmt: str, setup: str, number: int, num_threads: int):
        self._validate()
        cache_key = (number, num_threads)
        if cache_key not in self._baseline_cache:
            self._baseline_cache[cache_key] = self._invoke(
                stmt="pass", setup="pass", number=number, num_threads=num_threads)
        baseline_inclusive_stats, baseline_exclusive_stats = \
            self._baseline_cache[cache_key]

        stmt_inclusive_stats, stmt_exclusive_stats = self._invoke(
            stmt=stmt,
            setup=setup,
            number=number,
            num_threads=num_threads
        )

        return CallgrindStats(
            stmt=stmt,
            setup=setup,
            number=number,
            num_threads=num_threads,
            built_with_debug_symbols=self._build_type == "RelWithDebInfo",
            baseline_inclusive_stats=baseline_inclusive_stats,
            baseline_exclusive_stats=baseline_exclusive_stats,
            stmt_inclusive_stats=stmt_inclusive_stats,
            stmt_exclusive_stats=stmt_exclusive_stats,
        )

    def _invoke(self, stmt: str, setup: str, number: int, num_threads: int):
        working_dir = tempfile.mkdtemp()
        script_file = os.path.join(working_dir, "timer_callgrind.py")
        callgrind_out = os.path.join(working_dir, "callgrind.out")
        error_log = os.path.join(working_dir, "error.txt")
        stdout_stderr_log = os.path.join(working_dir, "stdout_stderr.log")
        f_stdout_stderr = open(stdout_stderr_log, "wt")

        try:
            with open(script_file, "wt") as f:
                f.write(self._construct_script(
                    stmt=stmt, setup=setup, number=number,
                    num_threads=num_threads, error_log=error_log))

            valgrind_invocation = subprocess.run(
                [
                    "valgrind",
                    "--tool=callgrind",
                    f"--callgrind-out-file={callgrind_out}",
                    "--dump-line=yes",
                    "--dump-instr=yes",
                    "--collect-jumps=yes",
                    "--instr-atstart=yes",
                    "--collect-atstart=no",
                    "python",
                    script_file,
                ],
                stdout=f_stdout_stderr,
                stderr=subprocess.STDOUT,
                capture_output=False,
            )
            if valgrind_invocation.returncode:
                error_report = ""
                if os.path.exists(error_log):
                    with open(error_log, "rt") as f:
                        error_report = f.read()
                if not error_report:
                    with open(stdout_stderr_log, "rt") as f:
                        error_report = "Unknown error.\n" + f.read()

                raise OSError(f"Failed to collect callgrind profile:\n{error_report}")

            def parse_output(inclusive: bool):
                annotate_invocation = subprocess.run(
                    [
                        "callgrind_annotate",
                        f"--inclusive={'yes' if inclusive else 'no'}",
                        callgrind_out
                    ],
                    capture_output=True,
                    check=True,
                )

                begin_collecting = False
                fn_counts = []
                for l in annotate_invocation.stdout.decode("utf-8").splitlines(keepends=False):
                    if not begin_collecting and re.match(r"Ir\s+file:function", l):
                        begin_collecting = True
                        continue

                    count_match = re.match(r"^\s*([0-9,]+)\s+(.+:.+)$", l)
                    if count_match:
                        ir, file_function = count_match.groups()
                        ir = int(ir.replace(",", ""))
                        fn_counts.append((ir, file_function))
                        continue

                    if begin_collecting and re.match(r"-+", l):
                        continue

                    begin_collecting = False

                return fn_counts
            return parse_output(inclusive=True), parse_output(inclusive=False)
        finally:
            f_stdout_stderr.close()
            shutil.rmtree(working_dir)

    @staticmethod
    def _construct_script(stmt: str, setup: str, number: int, num_threads: int, error_log: str):
        # The naive template looks something like:
        #   "for _ in range({number}): {stmt}"
        # However a loop in Python is surprisingly expensive, and significantly
        # increases the number of background Python instructions. So instead we
        # partially unroll the loops, with a block size of 100 chosen to keep
        # the instruction overhead from `range` low while also not ballooning
        # the size of the generated file.
        block_size = 100
        loop_count = number // block_size
        remainder = number - block_size * loop_count
        blocked_stmt = ""
        if loop_count:
            unrolled_stmts = textwrap.indent("\n".join([stmt] * block_size), " " * 4)
            blocked_stmt += f"for _ in range({loop_count}):\n{unrolled_stmts}\n"
        if remainder:
            blocked_stmt += "\n".join([stmt] * remainder)

        return textwrap.dedent(r"""
            import gc
            import os
            import subprocess
            import sys
            import time

            import torch
            from torch.utils._benchmark.utils.valgrind_wrapper import bindings
            torch.set_num_threads({num_threads})

            PID = os.getpid()

            def log_failure(msg):
                with open("{error_log}", "wt") as f:
                    f.write(msg)
                sys.exit(1)

            def check_result(completed_process):
                if completed_process.returncode:
                    log_failure(f"Command failed: {{' '.join(completed_process.args)}}")
                return completed_process

            # =============================================================================
            # == Check that subprocess matches parent =====================================
            # =============================================================================
            if sys.executable != "{parent_interpreter}":
                log_failure(
                    "Interpreter mismatch:\n"
                    f"  {{sys.executable}}\n    vs.\n  {parent_interpreter}"
                )

            if torch.__file__ != "{torch_file}":
                log_failure(
                    "PyTorch does not match expected file:\n"
                    f"  {{torch.__file__}}\n    vs.\n  {torch_file}"
                )

            # =============================================================================
            # == User specified setup =====================================================
            # =============================================================================
            {setup}

            for _ in range({warmup_number}):
            {indented_stmt}

            # =============================================================================
            # == Callgrind management =====================================================
            # =============================================================================
            callgrind_stat = check_result(subprocess.run(
                ["callgrind_control", "--stat"],
                capture_output=True,
            ))

            stat_lines = callgrind_stat.stdout.decode("utf-8").splitlines()
            if f"PID {{PID}}: python {{__file__}}" not in stat_lines:
                log_failure("Process does not appear to be running callgrind.")

            gc.collect()
            time.sleep(0.01)

            # =============================================================================
            # == User code block ==========================================================
            # =============================================================================
            bindings.toggle()
            {blocked_stmt}

            # Sleep is to allow the interpreter to catch up before we stop collecting in
            # order to reduce jitter.
            time.sleep(0.01)
            bindings.toggle()
        """).strip().format(
            indented_stmt=textwrap.indent(stmt, " " * 4),
            blocked_stmt=blocked_stmt,
            number=number,
            setup=setup,
            warmup_number=min(number, 10),
            num_threads=num_threads,
            error_log=error_log,
            parent_interpreter=sys.executable,
            torch_file=torch.__file__,
        )


CALLGRIND_SINGLETON = None
def wrapper_singleton():
    global CALLGRIND_SINGLETON
    if CALLGRIND_SINGLETON is None:
        CALLGRIND_SINGLETON = _ValgrindWrapper()
    return CALLGRIND_SINGLETON
