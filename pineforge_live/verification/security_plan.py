"""Choose verification auxiliary feeds from the pinned codegen's Pine AST.

This module does not parse Pine itself. Codegen is imported only when a
verification case requests a plan; the public live runner gains no dependency.
"""
from __future__ import annotations

from dataclasses import fields
import importlib
from pathlib import Path
import sys

from pineforge_live.bars.policy import tf_ms


def _member(node, namespace, name, nodes):
    return (isinstance(node, nodes.MemberAccess)
            and isinstance(node.object, nodes.Identifier)
            and node.object.name == namespace and node.member == name)


def _timeframe_identity(value):
    # Equal durations can have different partitions: 1W is Monday-anchored,
    # 7D is a daily multiple, and 1440 minute bars are not session daily bars.
    tf_ms(value)
    unit=value[-1] if value[-1] in 'DW' else 'minute'
    multiplier=value[:-1] if unit!='minute' else value
    return unit,int(multiplier or '1')


def _walk(root, nodes):
    """Walk the parser's dataclass AST, including calls nested in UDF bodies."""
    pending = [root]
    while pending:
        value = pending.pop()
        if isinstance(value, nodes.ASTNode):
            yield value
            pending.extend(reversed([
                getattr(value, field.name) for field in fields(value)
                if field.name not in ('loc', 'annotations')
            ]))
        elif isinstance(value, (list, tuple)):
            pending.extend(reversed(value))
        elif isinstance(value, dict):
            pending.extend(reversed(list(value.values())))


def classify_security_ast(program, script_tf, nodes):
    """Pure AST classification; unresolved timeframe expressions require 1m.

    ``nodes`` is the pinned ``pineforge_codegen.ast_nodes`` module. Only
    ordinary request.security is routed here. security_lower_tf retains
    the separately documented native-chart behavior.
    """
    if (getattr(program, 'annotations', None) or {}).get('parse_recovery_count'):
        raise ValueError('incomplete Pine AST after parser recovery')
    chart_identity = _timeframe_identity(script_tf)
    calls = []
    ignored_lower_tf = 0
    for node in _walk(program, nodes):
        if not isinstance(node, nodes.FuncCall):
            continue
        if _member(node.callee, 'request', 'security_lower_tf', nodes):
            ignored_lower_tf += 1
            continue
        if not _member(node.callee, 'request', 'security', nodes):
            continue
        timeframe = node.kwargs.get('timeframe')
        ambiguous = timeframe is not None and len(node.args) > 1
        if timeframe is None and len(node.args) > 1:
            timeframe = node.args[1]
        needed = True
        if ambiguous:
            reason = 'ambiguous-timeframe-arguments'
            spelling = None
        elif _member(timeframe, 'timeframe', 'period', nodes):
            needed = False
            reason = 'same-chart-timeframe.period'
            spelling = 'timeframe.period'
        elif isinstance(timeframe, nodes.StringLiteral):
            spelling = timeframe.value
            if spelling == '':
                needed = False
                reason = 'same-chart-empty-timeframe'
            else:
                try:
                    same = _timeframe_identity(spelling) == chart_identity
                except ValueError:
                    same = False
                needed = not same
                reason = 'same-chart-literal-timeframe' if same else 'different-or-unsupported-literal-timeframe'
        else:
            spelling = None
            reason = 'unresolved-timeframe-expression' if timeframe is not None else 'missing-timeframe-argument'
        loc = getattr(node, 'loc', None)
        calls.append({'line': getattr(loc, 'line', None),
                      'column': getattr(loc, 'col', None),
                      'timeframe': spelling, 'needed': needed, 'reason': reason})
    return {'needed': any(call['needed'] for call in calls),
            'reasons': sorted({call['reason'] for call in calls}) if calls else ['no-ordinary-security-calls'],
            'calls': calls, 'ignored_lower_tf_calls': ignored_lower_tf}


def security_feed_plan(source, script_tf, codegen_path):
    """Parse source with the requested codegen checkout and classify its calls.

    Refuse an already-imported different checkout instead of silently using
    a drifting parser. Syntax errors propagate; failure cannot become a
    claim that no auxiliary feed is required.
    """
    package_root = (Path(codegen_path).resolve() / 'pineforge_codegen')
    if not (package_root / 'parser.py').is_file():
        raise ValueError('pinned codegen parser is missing')
    for name, module in list(sys.modules.items()):
        if name == 'pineforge_codegen' or name.startswith('pineforge_codegen.'):
            filename = getattr(module, '__file__', None)
            if filename is None or not Path(filename).resolve().is_relative_to(package_root):
                raise ValueError('a different codegen checkout is already imported')
    sys.path.insert(0, str(package_root.parent))
    try:
        lexer = importlib.import_module('pineforge_codegen.lexer')
        parser = importlib.import_module('pineforge_codegen.parser')
        nodes = importlib.import_module('pineforge_codegen.ast_nodes')
    finally:
        sys.path.pop(0)
    program = parser.Parser(lexer.Lexer(source).tokenize(), source=source).parse()
    result = classify_security_ast(program, script_tf, nodes)
    result['parser'] = 'pinned-codegen-Lexer/Parser'
    return result
