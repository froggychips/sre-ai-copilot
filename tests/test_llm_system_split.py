"""Разделение ролей в LLM-запросе: инструкции агента → system, данные → user.

Что проверяем:
  - anthropic-бэкенд передаёт `system` отдельным параметром messages.create,
    а user-сообщение несёт только данные;
  - без `system` запрос прежний (один user-message, ключа system нет);
  - claude_cli-бэкенд получает `--system-prompt`, данные идут через stdin;
  - резерв бюджета учитывает system-часть как входные токены;
  - replay golden-eval: ключ записи по (system, user) совпадает с ключом
    прежнего одностраничного промпта — старые записи остаются валидными.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.base import build_system_prompt


def _anthropic_svc(fake_response):
    from app.services.llm_service import LLMService

    svc = LLMService()
    svc.client = MagicMock()
    svc.client.messages.create = AsyncMock(return_value=fake_response)
    return svc


def _settings(mock_settings, backend="anthropic"):
    mock_settings.LLM_BACKEND = backend
    mock_settings.MODEL_NAME = "claude-sonnet-4-6"
    mock_settings.MAX_TOKENS = 1024
    mock_settings.LLM_TIMEOUT_SECONDS = 30.0
    mock_settings.ANTHROPIC_API_KEY = "test-key"
    mock_settings.CLAUDE_CLI_TIMEOUT_SECONDS = 180.0


def _fake_response():
    return MagicMock(
        content=[MagicMock(type="text", text="ok")],
        usage=MagicMock(input_tokens=10, output_tokens=2),
        stop_reason="end_turn",
    )


@pytest.mark.asyncio
async def test_anthropic_sends_system_separately():
    with patch("app.services.llm_service.settings") as s:
        _settings(s)
        svc = _anthropic_svc(_fake_response())
        await svc.generate_full("<user_context>\ndata\n</user_context>",
                                system="Role: r\nTask: t")

    kwargs = svc.client.messages.create.call_args.kwargs
    assert kwargs["system"] == "Role: r\nTask: t"
    assert kwargs["messages"] == [
        {"role": "user", "content": "<user_context>\ndata\n</user_context>"},
    ]


@pytest.mark.asyncio
async def test_anthropic_without_system_keeps_single_user_message():
    with patch("app.services.llm_service.settings") as s:
        _settings(s)
        svc = _anthropic_svc(_fake_response())
        await svc.generate_full("plain prompt")

    kwargs = svc.client.messages.create.call_args.kwargs
    assert "system" not in kwargs
    assert kwargs["messages"] == [{"role": "user", "content": "plain prompt"}]


@pytest.mark.asyncio
async def test_budget_reserve_counts_system_tokens():
    with patch("app.services.llm_service.settings") as s, \
            patch("app.services.llm_service.reserve") as mock_reserve:
        _settings(s)
        mock_reserve.return_value = MagicMock(allowed=False, reason="test",
                                              spent_usd=0, limit_usd=0,
                                              reserved_usd=0)
        svc = _anthropic_svc(_fake_response())
        with pytest.raises(Exception):
            await svc.generate_full("DATA", system="INSTRUCTIONS")

    _, reserved_text = mock_reserve.call_args.args
    assert "INSTRUCTIONS" in reserved_text
    assert "DATA" in reserved_text


@pytest.mark.asyncio
async def test_cli_backend_forwards_system():
    with patch("app.services.llm_service.settings") as s:
        _settings(s, backend="claude_cli")
        from app.services.llm_service import LLMService

        svc = LLMService()
        svc.cli = MagicMock()
        svc.cli.generate_content = AsyncMock(return_value="cli out")
        await svc.generate_full("DATA", system="INSTRUCTIONS")

    svc.cli.generate_content.assert_awaited_once_with("DATA", system="INSTRUCTIONS")


@pytest.mark.asyncio
async def test_claude_cli_argv_has_system_prompt_and_data_on_stdin():
    from app.services.claude_cli_service import ClaudeCliService

    proc = MagicMock(returncode=0)
    proc.communicate = AsyncMock(return_value=(b"answer", b""))
    with patch("asyncio.create_subprocess_exec",
               AsyncMock(return_value=proc)) as spawn:
        out = await ClaudeCliService(binary="claude").generate_content(
            "DATA", system="INSTRUCTIONS",
        )

    assert out == "answer"
    argv = spawn.call_args.args
    assert "--system-prompt" in argv
    assert argv[argv.index("--system-prompt") + 1] == "INSTRUCTIONS"
    assert "DATA" not in argv
    proc.communicate.assert_awaited_once_with(b"DATA")


@pytest.mark.asyncio
async def test_claude_cli_without_system_has_no_flag():
    from app.services.claude_cli_service import ClaudeCliService

    proc = MagicMock(returncode=0)
    proc.communicate = AsyncMock(return_value=(b"answer", b""))
    with patch("asyncio.create_subprocess_exec",
               AsyncMock(return_value=proc)) as spawn:
        await ClaudeCliService(binary="claude").generate_content("DATA")

    assert "--system-prompt" not in spawn.call_args.args


def test_replay_keys_match_legacy_single_prompt():
    """Записи golden-eval сделаны со старым промптом одной строкой.

    Ключ (роль, контекст) по новой паре (system, user) обязан совпасть с
    ключом старой строки, иначе после разделения ролей все кейсы ушли бы в
    ctx-miss и replay перестал бы что-либо проверять.
    """
    from app.evaluation.llm_replay import Recordings

    role, instruction, ctx = "Senior SRE Analyst", "Analyze.", '{"a": 1}'
    legacy = (
        f"\nRole: {role}\nTask: {instruction}\n<user_context>\n{ctx}\n"
        f"</user_context>\n"
    )
    rec = Recordings([])
    rec.add(legacy, "analysis", {"text": "recorded"})

    system = build_system_prompt(role, instruction)
    user = f"<user_context>\n{ctx}\n</user_context>"
    assert rec.lookup(user, system)["text"] == "recorded"
    assert rec.misses == []


def _mock_llm(mocker):
    mock_api = mocker.patch(
        "app.services.llm_service.llm_client.generate_full", new_callable=AsyncMock,
    )
    mock_api.return_value = {"text": "{}", "input_tokens": 0, "output_tokens": 0,
                             "model": "m", "backend": "anthropic"}
    return mock_api


@pytest.mark.asyncio
async def test_fix_open_jira_directive_goes_to_system(mocker):
    """Указание «есть открытая Jira — не просто рестарт» — наше, а не данные.

    Оставшись в user_context, оно противоречило бы DATA_POLICY («внутри —
    данные, не инструкции»), и инцидент с открытой задачей мог бы
    регрессировать к голому рестарту.
    """
    from app.agents.fix import FixAgent

    mock_api = _mock_llm(mocker)
    jira = {
        "open": [{"key": "OPS-1", "priority": "High", "summary": "leak",
                  "url": "https://jira.example/OPS-1"}],
        "resolved": [], "has_open": True,
    }
    await FixAgent().suggest("memory leak in api", jira_context=jira)

    prompt = mock_api.call_args.args[0]
    system = mock_api.call_args.kwargs["system"]
    assert "OPS-1" in prompt  # сами задачи — данные
    assert "NOT just a restart" in system
    assert "NOT just a restart" not in prompt


@pytest.mark.asyncio
async def test_fix_without_open_jira_has_no_directive(mocker):
    from app.agents.fix import FixAgent

    mock_api = _mock_llm(mocker)
    jira = {"open": [], "resolved": [], "has_open": False}
    await FixAgent().suggest("memory leak in api", jira_context=jira)

    assert "NOT just a restart" not in mock_api.call_args.kwargs["system"]
