"""Pure AST routing tests; no codegen installation or engine is required."""
from dataclasses import dataclass, field
from types import SimpleNamespace
import sys

import pytest

from pineforge_live.verification.security_plan import classify_security_ast, security_feed_plan


@dataclass
class ASTNode:
    loc: object = None
    annotations: object = None


@dataclass
class Program(ASTNode):
    body: list = field(default_factory=list)


@dataclass
class Identifier(ASTNode):
    name: str = ''


@dataclass
class MemberAccess(ASTNode):
    object: object = None
    member: str = ''


@dataclass
class StringLiteral(ASTNode):
    value: str = ''


@dataclass
class FuncCall(ASTNode):
    callee: object = None
    args: list = field(default_factory=list)
    kwargs: dict = field(default_factory=dict)


@dataclass
class FuncDef(ASTNode):
    body: list = field(default_factory=list)


NODES = SimpleNamespace(ASTNode=ASTNode, Identifier=Identifier, MemberAccess=MemberAccess,
                        StringLiteral=StringLiteral, FuncCall=FuncCall)


def member(namespace, name):
    return MemberAccess(object=Identifier(name=namespace), member=name)


def call(timeframe, *, named=False, name='security', expression=None):
    result = FuncCall(callee=member('request', name), loc=SimpleNamespace(line=7, col=4))
    if named:
        result.kwargs = {'symbol': Identifier(name='haTicker'), 'expression': expression,
                         'timeframe': timeframe}
    else:
        result.args = [Identifier(name='haTicker'), timeframe, expression]
    return result


def plan(*calls, script_tf='15'):
    return classify_security_ast(Program(body=list(calls)), script_tf, NODES)


@pytest.mark.parametrize('named', [False, True])
@pytest.mark.parametrize('timeframe', [member('timeframe', 'period'), StringLiteral(value=''), StringLiteral(value='15')])
def test_proven_same_chart_calls_do_not_attach_auxiliary(timeframe, named):
    result = plan(call(timeframe, named=named))
    assert not result['needed']
    assert result['calls'][0]['line'] == 7 and result['calls'][0]['column'] == 4


@pytest.mark.parametrize('literal,script_tf', [('D', '1D'), ('W', '1W'), ('60', '060')])
def test_literal_timeframe_aliases(literal, script_tf):
    assert not plan(call(StringLiteral(value=literal)), script_tf=script_tf)['needed']


@pytest.mark.parametrize('literal,script_tf',[('1440','1D'),('1W','7D')])
def test_equal_duration_is_not_proof_of_equal_calendar_partition(literal,script_tf):
    assert plan(call(StringLiteral(value=literal)),script_tf=script_tf)['needed']


@pytest.mark.parametrize('timeframe', [Identifier(name='tf'), StringLiteral(value='60'),
                                       StringLiteral(value='1M'), StringLiteral(value=' 15')])
def test_unresolved_or_different_timeframe_requires_auxiliary(timeframe):
    assert plan(call(timeframe))['needed']


def test_one_different_call_requires_auxiliary_for_entire_strategy():
    result = plan(call(member('timeframe', 'period')), call(StringLiteral(value='5')))
    assert result['needed'] and len(result['calls']) == 2
    assert [c['needed'] for c in result['calls']] == [False, True]


def test_nested_calls_are_visited_and_lower_tf_is_not_routed():
    nested = call(StringLiteral(value='5'))
    result = plan(call(member('timeframe', 'period'), expression=nested),
                  call(StringLiteral(value='1'), name='security_lower_tf'))
    assert result['needed'] and len(result['calls']) == 2
    assert result['ignored_lower_tf_calls'] == 1
    lower_only = plan(call(StringLiteral(value='1'), name='security_lower_tf'))
    assert not lower_only['needed'] and lower_only['calls'] == []


def test_unresolved_udf_body_call_is_conservatively_routed():
    result = plan(FuncDef(body=[call(Identifier(name='function_timeframe_parameter'))]))
    assert result['needed'] and len(result['calls']) == 1


def test_no_calls_and_lookalike_string_are_not_security_calls():
    result = plan(StringLiteral(value='request.security(s, "1", close)'))
    assert not result['needed'] and result['reasons'] == ['no-ordinary-security-calls']


def test_malformed_timeframe_arguments_fail_conservatively():
    missing = FuncCall(callee=member('request', 'security'), args=[Identifier(name='s')])
    ambiguous = call(StringLiteral(value='15'))
    ambiguous.kwargs['timeframe'] = StringLiteral(value='15')
    assert plan(missing)['calls'][0]['reason'] == 'missing-timeframe-argument'
    assert plan(ambiguous)['needed']


def test_source_parser_is_a_deferred_dependency(tmp_path):
    with pytest.raises(ValueError, match='parser is missing'):
        security_feed_plan('strategy("test")', '15', tmp_path)


def test_recovered_partial_ast_cannot_claim_no_auxiliary_is_needed():
    with pytest.raises(ValueError,match='parser recovery'):
        classify_security_ast(Program(annotations={'parse_recovery_count':1}),'15',NODES)


def test_source_parser_refuses_a_previously_loaded_different_checkout(tmp_path,monkeypatch):
    package=tmp_path/'pineforge_codegen'
    package.mkdir();(package/'parser.py').write_text('# fixture only')
    monkeypatch.setitem(sys.modules,'pineforge_codegen',SimpleNamespace(__file__='/different/pineforge_codegen/__init__.py'))
    with pytest.raises(ValueError,match='different codegen checkout'):
        security_feed_plan('strategy("test")','15',tmp_path)
