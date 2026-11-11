"""Runs the tests across several processes. Each test builds its own throwaway repository and spends
most of its time starting git and python, so they run side by side without sharing anything.

    python tests/run.py               every test
    python tests/run.py --fast        only the quick ones: none marked @slow in test_prx.py
    python tests/run.py -k retarget   only tests whose name contains the text (repeatable)

`python -m unittest discover -s tests` still runs every test, in one process.
"""
import argparse
import io
import os
import sys
import time
import unittest
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
UNMARKED = 2.0  # seconds: a --fast run names anything slower than this, so it gets marked @slow
WINDOWS_MOST = 61  # workers: Python's process pool refuses more than this on Windows


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def collect(start):
    """Every test under `start` as (id, marked slow, its class when that has a fixture of its own),
    and the loader's errors (a test file that doesn't import)."""
    loader = unittest.TestLoader()
    tests = []
    for test in flatten(loader.discover(str(start), top_level_dir=str(start))):
        cls = type(test)
        slow = getattr(cls, "slow", False) or getattr(getattr(test, test._testMethodName, None), "slow", False)
        shared = any(getattr(cls, name).__func__ is not getattr(unittest.TestCase, name).__func__  # its own or inherited
                     for name in ("setUpClass", "tearDownClass"))
        tests.append((test.id(), bool(slow), f"{cls.__module__}.{cls.__qualname__}" if shared else None))
    return tests, loader.errors


def units(tests):
    """What one process runs in one go: a class with a setUpClass stays whole, so its fixture is
    built once; every other test goes alone. Each is a list of test ids."""
    whole, out = {}, []
    for test_id, _, shared in tests:
        if shared is None:
            out.append([test_id])
        elif shared in whole:
            whole[shared].append(test_id)
        else:
            whole[shared] = [test_id]
            out.append(whole[shared])
    return out


def job_count(asked, fast, units, cpus=None, windows=None):
    """How many processes to use: what was asked for, else one for --fast and one per CPU. Never more
    than there are units to run, or than Windows' process pool takes (a 64-CPU machine would crash)."""
    windows = sys.platform == "win32" if windows is None else windows
    jobs = min(asked or (1 if fast else cpus or os.cpu_count() or 2), units)
    return max(1, min(jobs, WINDOWS_MOST) if windows else jobs)


def run_unit(start, ids):
    """Runs the tests named `ids` here, and reports in plain values (they cross a process boundary):
    how many ran, the seconds, the failures and errors as (id, traceback) and the skips as (id, why)."""
    if str(start) not in sys.path:
        sys.path.insert(0, str(start))
    began = time.perf_counter()
    result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(
        unittest.defaultTestLoader.loadTestsFromNames(ids))
    broken = [(str(test), trace) for test, trace in result.failures + result.errors]
    broken += [(str(test), "passed, though it's marked as an expected failure") for test in result.unexpectedSuccesses]
    return {"ids": ids, "ran": result.testsRun, "seconds": time.perf_counter() - began, "broken": broken,
            "skipped": [(str(test), why) for test, why in result.skipped]}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the tests across several processes.")
    parser.add_argument("--fast", action="store_true", help="leave out the tests marked @slow")
    parser.add_argument("-k", action="append", default=[], metavar="TEXT",
                        help="only tests whose name contains TEXT (repeatable)")
    parser.add_argument("-j", "--jobs", type=int, metavar="N",
                        help="processes to use (default: one per CPU; 1 for --fast)")
    parser.add_argument("-s", "--start-dir", default=str(HERE), metavar="DIR", help="where the tests are (default: here)")
    args = parser.parse_args(argv)
    start = Path(args.start_dir).resolve()
    sys.path.insert(0, str(start))

    tests, errors = collect(start)
    if errors:
        print("\n".join(errors))
        print("FAILED: a test file didn't load, so nothing was run")
        return 1
    chosen = [t for t in tests if not (args.fast and t[1]) and (not args.k or any(text in t[0] for text in args.k))]
    left_out = len(tests) - len(chosen)
    if not chosen:
        print(f"No test matches (of {len(tests)})")
        return 1
    todo = units(chosen)
    jobs = job_count(args.jobs, args.fast, len(todo))

    began, done = time.perf_counter(), []

    def finished(report):
        done.append(report)
        for name, _ in report["broken"]:
            print(f"FAIL  {name}", flush=True)

    if jobs == 1:
        for ids in todo:
            finished(run_unit(start, ids))
    else:
        with ProcessPoolExecutor(max_workers=jobs) as pool:
            for future in as_completed([pool.submit(run_unit, start, ids) for ids in todo]):
                finished(future.result())
    seconds = time.perf_counter() - began

    broken = [b for report in done for b in report["broken"]]
    for name, trace in broken:
        print(f"\n{'=' * 70}\n{name}\n{'-' * 70}\n{trace.rstrip()}")
    reasons = {}
    for report in done:
        for _, why in report["skipped"]:
            reasons[why] = reasons.get(why, 0) + 1
    for why, count in sorted(reasons.items()):
        print(f"skipped {count}: {why}")
    if args.fast:
        marked = {t[0] for t in tests if t[1]}
        for report in done:
            if report["seconds"] > UNMARKED and not marked.intersection(report["ids"]):
                what = report["ids"][0] if len(report["ids"]) == 1 else f"{report['ids'][0].rsplit('.', 1)[0]} (its {len(report['ids'])} tests)"
                print(f"slow, and not marked @slow: {what} took {report['seconds']:.1f}s")

    ran = sum(report["ran"] for report in done)
    summary = (f"Ran {ran} test{'' if ran == 1 else 's'} in {seconds:.1f}s on {jobs} process{'' if jobs == 1 else 'es'}"
               + (f" ({left_out} left out)" if left_out else ""))
    # Every chosen test must have run, once: a test that vanished between the count and the run is a failure
    if ran != len(chosen):
        print(f"{summary}\nFAILED: {len(chosen)} tests were chosen and {ran} ran")
        return 1
    print(f"{summary}\n{'FAILED (' + str(len(broken)) + ')' if broken else 'OK'}")
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
