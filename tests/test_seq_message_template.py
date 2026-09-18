"""Шаблон сообщения из Seq: почему текст ошибок не сохранялся.

Seq REST отдаёт шаблон РАЗОБРАННЫМ на токены, в `MessageTemplateTokens`:
чередование `{"Text": "..."}` и `{"PropertyName": "..."}`. Ключей
`MessageTemplate`, `RenderedMessage` и `Message`, которые искал прежний
код, в ответе нет вовсе — рекон живого события 18.09.2026 дал такой набор
полей:

    EventType, Exception, Id, Level, Links, MessageTemplateTokens,
    Properties, SpanKind, Timestamp

Цена дефекта: `sample_message` и `top_message_hash` не заполнялись ни у
одного наблюдения — 1726 записей за сутки, включая 88 Error и один Fatal.
Счётчики при этом верные, поэтому выглядело безобидно: видно, что у
GR.WO.Bot в prod-kingdom2 50 089 Warning, и не видно, каких именно.

Тот же класс, что был с полем `App`: искали `Application`, а сервис-тег
лежит в `Properties` под именем `App`, и это тоже выяснилось только
реконом по живому Seq.
"""
import pytest

from app.context.seq_client import SeqClient


def test_template_assembled_from_tokens():
    """Токены склеиваются в шаблон — именно этот формат отдаёт живой Seq."""
    event = {
        "MessageTemplateTokens": [
            {"Text": "export failed: DynUser="},
            {"PropertyName": "DynUserId"},
            {"Text": " MarchEntityId="},
            {"PropertyName": "MarchEntityId"},
        ]
    }
    assert SeqClient.extract_message_template(event) == (
        "export failed: DynUser={DynUserId} MarchEntityId={MarchEntityId}"
    )


def test_placeholders_are_names_not_values():
    """Плейсхолдер — имя свойства, а не подставленное значение.

    Шаблон должен быть ОДИНАКОВЫМ для всех событий одного вида: иначе хэш
    перестаёт группировать и `top_message_hash` становится уникальным на
    каждое событие, то есть бесполезным.
    """
    first = SeqClient.extract_message_template({
        "MessageTemplateTokens": [
            {"Text": "user "}, {"PropertyName": "UserId"}, {"Text": " failed"},
        ],
        "Properties": [{"Name": "UserId", "Value": "111"}],
    })
    second = SeqClient.extract_message_template({
        "MessageTemplateTokens": [
            {"Text": "user "}, {"PropertyName": "UserId"}, {"Text": " failed"},
        ],
        "Properties": [{"Name": "UserId", "Value": "999"}],
    })
    assert first == second == "user {UserId} failed"


def test_old_keys_still_work():
    """Старые ключи остаются в fallback: их отдают другие версии API."""
    assert SeqClient.extract_message_template(
        {"MessageTemplate": "явный шаблон"}
    ) == "явный шаблон"
    assert SeqClient.extract_message_template(
        {"RenderedMessage": "отрендеренное"}
    ) == "отрендеренное"


def test_tokens_take_priority_over_old_keys():
    """Если есть и то и другое — берём токены как более точный источник."""
    event = {
        "MessageTemplateTokens": [{"Text": "из токенов"}],
        "MessageTemplate": "из старого ключа",
    }
    assert SeqClient.extract_message_template(event) == "из токенов"


@pytest.mark.parametrize("tokens", [
    [],
    None,
    "не список",
    [{"Unknown": "поле"}],
    [{}],
])
def test_unusable_tokens_fall_through(tokens):
    """Негодные токены не должны давать пустой шаблон вместо fallback'а."""
    event = {"MessageTemplateTokens": tokens, "Message": "запасной"}
    assert SeqClient.extract_message_template(event) == "запасной"


def test_no_source_returns_empty_string():
    """Ничего нет — пустая строка, как и раньше.

    Вызывающий (`seq_logs_sync`) уже умеет с этим работать: пустой шаблон
    означает «хэш не считаем», а не «событий нет».
    """
    assert SeqClient.extract_message_template({}) == ""


def test_whitespace_only_template_is_treated_as_empty():
    """Шаблон из одних пробелов — то же, что отсутствие шаблона."""
    event = {"MessageTemplateTokens": [{"Text": "   "}], "Message": "запасной"}
    assert SeqClient.extract_message_template(event) == "запасной"


def test_aggregation_groups_identical_templates():
    """Одинаковые шаблоны складываются в одну группу.

    Это и есть смысл починки: на живых данных 18.09.2026 из семи событий
    GR.WO.Bot шесть пришли по одному шаблону — теперь они группируются, а
    не выглядят шестью разными ошибками.
    """
    tokens = [{"Text": "[BOT] Error processing active bot "},
              {"PropertyName": "BotUserId"}]
    events = [
        {"MessageTemplateTokens": tokens, "App": "GR.WO.Bot"}
        for _ in range(6)
    ] + [
        {"MessageTemplateTokens": [{"Text": "другая ошибка"}], "App": "GR.WO.Bot"}
    ]

    grouped = SeqClient.aggregate_by_service(events)
    total, counter = grouped["GR.WO.Bot"]

    assert total == 7
    assert counter.most_common(1)[0] == (
        "[BOT] Error processing active bot {BotUserId}", 6
    )


# --- находки ревью: токен богаче своего имени -----------------------------

def test_raw_text_keeps_destructuring():
    """`{@Error}` не превращается в `{Error}`.

    `@` перед именем — это деструктурирование: Seq разворачивает объект, а
    не пишет его `ToString()`. Живой рекон 18.09.2026 (4000 событий всех
    восьми Seq): 413 property-токенов из 10 207 несут `RawText`, и все —
    именно такие: `{@Error}`, `{@Ops}`, `{@Op}`, `{@StatesBefore}`.
    Собрать их из одного `PropertyName` значит записать в наблюдение
    шаблон, которого в Seq нет.
    """
    event = {
        "MessageTemplateTokens": [
            {"Text": "sync failed: "},
            {"PropertyName": "Error", "RawText": "{@Error}"},
        ]
    }
    assert SeqClient.extract_message_template(event) == "sync failed: {@Error}"


def test_formatted_token_does_not_collapse_into_plain_one():
    """`{Elapsed:0.000}` и `{Elapsed}` — разные шаблоны, разные группы.

    Для Seq это два разных шаблона, и схлопывать их в один
    `top_message_hash` значит складывать в одну кучу события, которые
    разошлись в коде.
    """
    formatted = SeqClient.extract_message_template({
        "MessageTemplateTokens": [
            {"Text": "done in "},
            {"PropertyName": "Elapsed", "RawText": "{Elapsed:0.000}"},
        ]
    })
    plain = SeqClient.extract_message_template({
        "MessageTemplateTokens": [
            {"Text": "done in "},
            {"PropertyName": "Elapsed"},
        ]
    })
    assert formatted == "done in {Elapsed:0.000}"
    assert plain == "done in {Elapsed}"
    assert formatted != plain


def test_property_name_used_when_raw_text_absent():
    """Без `RawText` плейсхолдер собирается по имени — как и раньше.

    `RawText` есть у 4% токенов; остальные 96% должны работать по-старому.
    """
    event = {"MessageTemplateTokens": [{"PropertyName": "BotUserId"}]}
    assert SeqClient.extract_message_template(event) == "{BotUserId}"


def test_edge_whitespace_is_preserved():
    """Крайние пробелы шаблона не срезаются.

    Обрезка склеивала бы два разных шаблона Seq в один хэш, а сохранённый
    `sample_message` переставал бы совпадать с тем, что написано в коде
    сервиса.
    """
    padded = SeqClient.extract_message_template({
        "MessageTemplateTokens": [{"Text": " bot stalled "}]
    })
    assert padded == " bot stalled "
    assert padded != "bot stalled"
