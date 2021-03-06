import re
import unittest
import traceback
import os
import string
import sys
import ast
from typing import Tuple


# This file implements expect tests (also known as "golden" tests).
# Expect tests are a method of writing tests where instead of
# hard-coding the expected output of a test, you instead run the test to
# get the output, and the test framework automatically populates the
# expected output.  If the output of the test changes, you can rerun the
# test with EXPECTTEST_ACCEPT=1 environment variable to automatically
# update the expected output.
#
# Somewhat unusually, this file implements *inline* expect tests: that
# is to say, the expected output isn't save to an external file, it is
# saved directly in the Python file (and we modify your Python the file
# when updating the expect test.)
#
# The general recipe for how to use this is as follows:
#
#   1. Write your test and use assertExpectedInline() instead of
#      a normal assertEqual.  Leave the expected argument blank
#      with an empty string:
#
#       self.assertExpectedInline(some_func(), "")
#
#   2. Run your test.  It should fail, and you get an error message
#      about accepting the output with EXPECTTEST_ACCEPT=1
#
#   3. Rerun the test with EXPECTTEST_ACCEPT=1.  Now the previously
#      blank string literal will now contain the expected value of
#      the test.
#
#       self.assertExpectedInline(some_func(), "my_value")
#
# Some tips and tricks:
#
#   - Often, you will want to expect test on a multiline string.  This
#     framework understands triple-quoted strings, so you can just
#     write """my_value""" and it will turn into triple-quoted
#     strings.
#
#   - Take some time thinking about how exactly you want to design
#     the output format of the expect test.  It is often profitable
#     to design an output representation specifically for expect tests.
#


ACCEPT = os.getenv('EXPECTTEST_ACCEPT')

LINENO_AT_START = sys.version_info >= (3, 8)


def nth_line(src, lineno):
    """
    Compute the starting index of the n-th line (where n is 1-indexed)

    >>> nth_line("aaa\\nbb\\nc", 2)
    4
    """
    assert lineno >= 1
    pos = 0
    for _ in range(lineno - 1):
        pos = src.find('\n', pos) + 1
    return pos


def nth_eol(src, lineno):
    """
    Compute the ending index of the n-th line (before the newline,
    where n is 1-indexed)

    >>> nth_eol("aaa\\nbb\\nc", 2)
    6
    """
    assert lineno >= 1
    pos = -1
    for _ in range(lineno):
        pos = src.find('\n', pos + 1)
        if pos == -1:
            return len(src)
    return pos


def normalize_nl(t):
    return t.replace('\r\n', '\n').replace('\r', '\n')


def escape_trailing_quote(s, quote):
    if s and s[-1] == quote:
        return s[:-1] + '\\' + quote
    else:
        return s


class EditHistory(object):
    def __init__(self):
        self.state = {}

    def adjust_lineno(self, fn, lineno):
        if fn not in self.state:
            return lineno
        for edit_loc, edit_diff in self.state[fn]:
            if lineno > edit_loc:
                lineno += edit_diff
        return lineno

    def seen_file(self, fn):
        return fn in self.state

    def record_edit(self, fn, lineno, delta):
        self.state.setdefault(fn, []).append((lineno, delta))


EDIT_HISTORY = EditHistory()


def ok_for_raw_triple_quoted_string(s, quote):
    """
    Is this string representable inside a raw triple-quoted string?
    Due to the fact that backslashes are always treated literally,
    some strings are not representable.

    >>> ok_for_raw_triple_quoted_string("blah", quote="'")
    True
    >>> ok_for_raw_triple_quoted_string("'", quote="'")
    False
    >>> ok_for_raw_triple_quoted_string("a ''' b", quote="'")
    False
    """
    return quote * 3 not in s and (not s or s[-1] not in [quote, '\\'])


RE_EXPECT = re.compile(
    (
        r"(?P<raw>r?)"
        r"(?P<quote>'''|" r'""")'
        r"(?P<body>.*?)"
        r"(?P=quote)"
    ),
    re.DOTALL
)


def replace_string_literal(src : str, start_lineno : int, end_lineno : int,
                           new_string : str) -> Tuple[str, int]:
    r"""
    Replace a triple quoted string literal with new contents.
    Only handles printable ASCII correctly at the moment.  This
    will preserve the quote style of the original string, and
    makes a best effort to preserve raw-ness (unless it is impossible
    to do so.)

    Returns a tuple of the replaced string, as well as a delta of
    number of lines added/removed.

    >>> replace_string_literal("'''arf'''", 1, 1, "barf")
    ("'''barf'''", 0)
    >>> r = replace_string_literal("  moo = '''arf'''", 1, 1, "'a'\n\\b\n")
    >>> print(r[0])
      moo = '''\
    'a'
    \\b
    '''
    >>> r[1]
    3
    >>> replace_string_literal("  moo = '''\\\narf'''", 1, 2, "'a'\n\\b\n")[1]
    2
    >>> print(replace_string_literal("    f('''\"\"\"''')", 1, 1, "a ''' b")[0])
        f('''a \'\'\' b''')
    """
    # Haven't implemented correct escaping for non-printable characters
    assert all(c in string.printable for c in new_string)

    new_string = normalize_nl(new_string)

    delta = [new_string.count("\n")]
    if delta[0] > 0:
        delta[0] += 1  # handle the extra \\\n

    assert start_lineno <= end_lineno
    start = nth_line(src, start_lineno)
    end = nth_eol(src, end_lineno)
    assert start <= end

    def replace(m):
        s = new_string
        raw = m.group('raw') == 'r'
        if not raw or not ok_for_raw_triple_quoted_string(s, quote=m.group('quote')[0]):
            raw = False
            s = s.replace('\\', '\\\\')
            if m.group('quote') == "'''":
                s = escape_trailing_quote(s, "'").replace("'''", r"\'\'\'")
            else:
                s = escape_trailing_quote(s, '"').replace('"""', r'\"\"\"')

        new_body = "\\\n" + s if "\n" in s and not raw else s
        delta[0] -= m.group('body').count("\n")
        return ''.join(['r' if raw else '',
                        m.group('quote'),
                        new_body,
                        m.group('quote'),
                        ])

    return (src[:start] + RE_EXPECT.sub(replace, src[start:end], count=1) + src[end:], delta[0])


class TestCase(unittest.TestCase):
    longMessage = True

    def assertExpectedInline(self, actual, expect, skip=0):
        """
        Assert that actual is equal to expect.  The expect argument
        MUST be a string literal (triple-quoted strings OK), and will
        get updated directly in source when you run the test suite
        with EXPECTTEST_ACCEPT=1.

        If you want to write a helper function that makes use of
        assertExpectedInline (e.g., expect is not a string literal),
        set the skip argument to how many function calls we should
        skip to find the string literal to update.
        """
        if ACCEPT:
            if actual != expect:
                # current frame and parent frame, plus any requested skip
                tb = traceback.extract_stack(limit=2 + skip)
                fn, lineno, _, _ = tb[0]
                print("Accepting new output for {} at {}:{}".format(self.id(), fn, lineno))
                with open(fn, 'r+') as f:
                    old = f.read()
                    old_ast = ast.parse(old)

                    # NB: it's only the traceback line numbers that are wrong;
                    # we reread the file every time we write to it, so AST's
                    # line numbers are correct
                    lineno = EDIT_HISTORY.adjust_lineno(fn, lineno)

                    # Conservative assumption to start
                    start_lineno = lineno
                    end_lineno = lineno
                    # Try to give a more accurate bounds based on AST
                    # NB: this walk is in no specified order (in practice it's
                    # breadth first)
                    for n in ast.walk(old_ast):
                        if isinstance(n, ast.Expr):
                            if hasattr(n, 'end_lineno'):
                                assert LINENO_AT_START
                                if n.lineno == start_lineno:
                                    end_lineno = n.end_lineno  # type: ignore[attr-defined]
                            else:
                                if n.lineno == end_lineno:
                                    start_lineno = n.lineno

                    new, delta = replace_string_literal(old, start_lineno, end_lineno, actual)

                    assert old != new, f"Failed to substitute string at {fn}:{lineno}; did you use triple quotes?  " \
                        "If this is unexpected, please file a bug report at " \
                        "https://github.com/pytorch/pytorch/issues/new?labels=module:%20expecttest " \
                        f"with the contents of the source file near {fn}:{lineno}"

                    # Only write the backup file the first time we hit the
                    # file
                    if not EDIT_HISTORY.seen_file(fn):
                        with open(fn + ".bak", 'w') as f_bak:
                            f_bak.write(old)
                    f.seek(0)
                    f.truncate(0)

                    f.write(new)

                EDIT_HISTORY.record_edit(fn, lineno, delta)
        else:
            help_text = ("To accept the new output, re-run test with "
                         "envvar EXPECTTEST_ACCEPT=1 (we recommend "
                         "staging/committing your changes before doing this)")
            self.assertMultiLineEqualMaybeCppStack(expect, actual, msg=help_text)

    def assertExpectedRaisesInline(self, exc_type, callable, expect, *args, **kwargs):
        """
        Like assertExpectedInline, but tests the str() representation of
        the raised exception from callable.  The raised exeption must
        be exc_type.
        """
        try:
            callable(*args, **kwargs)
        except exc_type as e:
            self.assertExpectedInline(str(e), expect, skip=1)
            return
        # Don't put this in the try block; the AssertionError will catch it
        self.fail(msg="Did not raise when expected to")

    def assertMultiLineEqualMaybeCppStack(self, expect, actual, *args, **kwargs):
        self.assertGreaterEqual(len(actual), len(expect), *args, **kwargs)
        if hasattr(self, "assertMultiLineEqual"):
            self.assertMultiLineEqual(expect, actual[:len(expect)], *args, **kwargs)
        else:
            self.assertEqual(expect, actual[:len(expect)], *args, **kwargs)
        if len(actual) > len(expect):
            cpp_stacktrace_header = "\nException raised from"
            end_header = len(expect) + len(cpp_stacktrace_header)
            self.assertEqual(actual[len(expect): end_header], cpp_stacktrace_header)


if __name__ == "__main__":
    import doctest
    doctest.testmod()
