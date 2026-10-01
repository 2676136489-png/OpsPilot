"""Python 容器不能出现在给操作员看的句子里。

这个文件守的是一类很具体的漏网：`f"必须是 {sorted(VALID_X)} 之一。"`。
它读起来没问题，跑起来也没问题 —— 但渲染出来是

    必须是 ['critical', 'high', 'low'] 之一。

方括号、引号、逗号都在。诊断没错，句子却是坏的，看的人会以为程序出毛病了。

容器本身不是问题，"把容器塞进散文"才是。同一批改动里还有一处版本号列表
（`v2.5.0, v2.4.0, v2.4.0`）被误报成英文散文，原因相同：写的人盯着的是值，
读的人看到的是表示法。

所以这里有两条独立的防线：
1. `join_values` / `_signals` 的**行为**测试 —— 连接符是「、」，且输出里绝不出现
   容器语法字符。
2. 一次 **AST 全量扫描** —— 任何 f-string 直接插值 `sorted()/list()/set()/dict()`
   或 `.keys()/.items()/.values()` 都判失败。行为测试只能盖住我发现的那几个点，
   扫描能盖住我还没写的那些。用 AST 而不是正则，是因为 `{sorted(x)[0]}`
   （插值单个元素，完全合法）会被正则误判。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

# 允许出现容器插值的文件：这是开发者断言，永远不会渲染给操作员，所以它
# 保持英文、保持 repr 反而更好 —— 出错时要把两个集合的差集看得很清楚。
ALLOWED = {
    # tools/registry.py: assert set(TOOL_REGISTRY) == set(HANDLERS)
    "tools/registry.py",
}

CONTAINER_CALLS = {"sorted", "list", "set", "dict", "tuple", "frozenset"}
CONTAINER_METHODS = {"keys", "items", "values"}

BACKEND_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "opspilot_backend"


def _interpolations(path: pathlib.Path) -> list[tuple[int, str]]:
    """(行号, 被插值的调用) —— 只针对直接插值整个容器的 f-string。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        for part in node.values:
            if not isinstance(part, ast.FormattedValue):
                continue
            expr = part.value
            # Subscript / 属性访问等一律放过：{sorted(x)[0]} 插的是一个元素。
            if not isinstance(expr, ast.Call):
                continue
            line = getattr(part, "lineno", node.lineno)
            fn = expr.func
            if isinstance(fn, ast.Name) and fn.id in CONTAINER_CALLS:
                found.append((line, f"{fn.id}(...)"))
            elif isinstance(fn, ast.Attribute) and fn.attr in CONTAINER_METHODS:
                found.append((line, f"...{fn.attr}()"))
    return found


def _python_files() -> list[pathlib.Path]:
    return sorted(p for p in BACKEND_SRC.rglob("*.py") if "__pycache__" not in p.parts)


def test_backend_source_has_no_container_interpolation() -> None:
    offenders: list[str] = []
    for path in _python_files():
        rel = path.relative_to(BACKEND_SRC).as_posix()
        if rel in ALLOWED:
            continue
        for line, what in _interpolations(path):
            offenders.append(f"{rel}:{line} 插值 {what}")
    assert not offenders, (
        "这些 f-string 把 Python 容器直接拼进了文案，渲染出来会带方括号和引号：\n  "
        + "\n  ".join(offenders)
        + "\n改成 join_values(...) 或 _signals(...)。"
    )


def test_scanner_can_actually_see_the_leak(tmp_path: pathlib.Path) -> None:
    """扫描器自身要立得住 —— 否则上面那条测试可能只是永远为真。

    刻意调用真正的 `_interpolations`（而不是在这里重写一遍遍历），否则扫描
    函数坏了、自检还是绿的。
    """
    leak = tmp_path / "leak.py"
    leak.write_text(
        'x = f"必须是 {sorted(values)} 之一。"\n'
        'y = f"允许 {set(registry)}"\n'
        'z = f"字段 {row.keys()}"\n',
        encoding="utf-8",
    )
    assert len(_interpolations(leak)) == 3

    ok = tmp_path / "ok.py"
    ok.write_text(
        # 插值元素、插值长度、插值 join 结果，都是合法的。
        'a = f"第一个是 {sorted(values)[0]}"\n'
        'b = f"共 {len(values)} 个"\n'
        'c = f"允许 {join_values(values)} 之一。"\n'
        'd = f"允许 {\"、\".join(values)} 之一。"\n',
        encoding="utf-8",
    )
    assert _interpolations(ok) == []


# ---------------------------------------------------------------------------
# 行为层
# ---------------------------------------------------------------------------


def test_join_values_uses_chinese_separator() -> None:
    from opspilot_backend.domain.enums import join_values

    assert join_values(["low", "critical", "high"]) == "critical、high、low"


@pytest.mark.parametrize("values", [set(), {"a"}, {"b", "a"}, {"x", "y", "z"}])
def test_join_values_never_emits_container_syntax(values: set[str]) -> None:
    from opspilot_backend.domain.enums import join_values

    out = join_values(values)
    for char in "[]{}'\"()":
        assert char not in out, f"{out!r} 里混进了容器语法字符 {char!r}"


def test_join_values_is_sorted_and_stable() -> None:
    from opspilot_backend.domain.enums import join_values

    assert join_values({"z", "a", "m"}) == join_values(["m", "z", "a"]) == "a、m、z"


def test_signal_rendering_has_no_brackets() -> None:
    from opspilot_backend.agent.analysis import _signals

    out = _signals(["oom", "deployment_recent"])
    assert out == "deployment_recent、oom"
    assert "[" not in out and "'" not in out


def test_signal_names_keep_their_wire_spelling() -> None:
    """信号名参与匹配，渲染时不能顺手翻译掉。"""
    from opspilot_backend.agent.analysis import _signals

    assert "deployment_recent" in _signals(["deployment_recent"])
    assert "db_pool_saturation" in _signals(["db_pool_saturation"])


def test_schema_validator_message_is_a_sentence() -> None:
    from opspilot_backend.schemas.incident import _validate_severity

    with pytest.raises(ValueError) as exc:
        _validate_severity("不存在的级别")
    message = str(exc.value)
    assert "必须是" in message and message.endswith("之一。")
    assert "[" not in message and "'critical'" not in message
    # 但值本身必须在，否则报错也帮不上人。
    for value in ("critical", "high", "medium", "low"):
        assert value in message


def test_service_error_message_is_a_sentence() -> None:
    from opspilot_backend.domain.errors import BadRequestError
    from opspilot_backend.services.incident import IncidentService

    with pytest.raises(BadRequestError) as exc:
        IncidentService._to_domain_severity("不存在的级别")
    message = str(exc.value)
    assert "必须是" in message and message.endswith("之一。")
    assert "[" not in message and "{" not in message


def test_deployment_status_message_is_a_sentence() -> None:
    from opspilot_backend.domain.enums import join_values
    from opspilot_backend.schemas.incident import VALID_DEPLOYMENT_STATUSES

    rendered = join_values(VALID_DEPLOYMENT_STATUSES)
    assert rendered.startswith("DEPLOYING、")
    assert "[" not in rendered


# ---------------------------------------------------------------------------
# 404：句子中文化，标识符保留
# ---------------------------------------------------------------------------


def test_not_found_message_is_chinese() -> None:
    from opspilot_backend.domain.errors import NotFoundError

    err = NotFoundError("Incident", "3f2504e0-4f89-11d3-9a0c-0305e82c3301")
    assert err.message.endswith("不存在"), err.message
    assert "not found" not in err.message


def test_not_found_keeps_the_wire_identifier_machine_readable() -> None:
    """`details.resource` 是给客户端匹配用的，不能跟着翻译掉。"""
    from opspilot_backend.domain.errors import NotFoundError

    err = NotFoundError("AgentRun", "abc")
    assert err.details["resource"] == "AgentRun"
    assert err.details["id"] == "abc"
    assert err.code == "NOT_FOUND" and err.http_status == 404


def test_not_found_without_an_id_still_reads_as_a_sentence() -> None:
    from opspilot_backend.domain.errors import NotFoundError

    err = NotFoundError("Service")
    assert "Service" not in err.message
    assert err.message == "服务不存在"
    assert "id" not in err.details


def test_unknown_resources_fall_back_to_their_own_name() -> None:
    """新资源忘了登记时退回英文名，而不是崩掉或吐出 `None`。"""
    from opspilot_backend.domain.errors import NotFoundError

    err = NotFoundError("Widget", 7)
    assert "Widget" in err.message and "7" in err.message
