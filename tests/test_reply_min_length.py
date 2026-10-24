"""Minimum reply length at the HTTP and CLI boundaries."""

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from ilan.cli import main
from ilan.server import IlanServer
from tests.helpers import get_json, post_json


@pytest.mark.parametrize("message", ["", " ", "\n\t ", "a", "ab", " \r\nab\t ", None, 123, [], {}])
@pytest.mark.parametrize("endpoint", ["reply", "reply-every"])
def test_short_reply_changes_nothing(
    ilan_server: IlanServer, message: object, endpoint: str,
) -> None:
    post_json(ilan_server, "/tasks", {"name": "min-reply", "prompt": "Build it"})
    before = get_json(ilan_server, "/tasks/min-reply")
    logs = get_json(ilan_server, "/tasks/min-reply/logs")
    with patch.object(ilan_server.runner, "reply_to_working") as resume:
        response = post_json(
            ilan_server, f"/tasks/min-reply/{endpoint}",
            {"message": message, "level": "max"},
        )
    assert "at least 3 characters" in response["error"]
    resume.assert_not_called()
    assert get_json(ilan_server, "/tasks/min-reply") == before
    assert get_json(ilan_server, "/tasks/min-reply/logs") == logs


@pytest.mark.parametrize("message", ["abc", " \nabc\r\n ", "你好吗", "a\nb"])
def test_three_characters_are_accepted(ilan_server: IlanServer, message: str) -> None:
    post_json(ilan_server, "/tasks", {"name": "min-reply", "prompt": "Build it"})
    with patch.object(ilan_server.runner, "reply_to_working") as resume:
        response = post_json(ilan_server, "/tasks/min-reply/reply", {"message": message})
    assert response["ok"]
    assert resume.call_args.args[1] == message


@pytest.mark.parametrize("base", [["reply"], ["re"], ["task", "reply"]])
@pytest.mark.parametrize("message", ["", " \n ", "a", " \nab\n "])
def test_cli_rejects_before_model_switch(tmp_config, base: list[str], message: str) -> None:
    client = MagicMock()
    with (
        patch("ilan.cli._client", return_value=client),
        patch("ilan.cli._do_max") as switch,
    ):
        result = CliRunner().invoke(main, [*base, "my-task", message, "--max"])
    assert result.exit_code == 1
    assert "at least 3 characters" in result.output
    switch.assert_not_called()
    client.reply.assert_not_called()
