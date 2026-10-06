"""Immediate looping-message delivery through every reply spelling."""

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from ilan.cli import main


@pytest.mark.parametrize("prefix", [["task", "reply"], ["reply"], ["re"]])
@pytest.mark.parametrize("flag", ["-g", "--go"])
def test_go_sends_without_fetching_a_stale_interval(
    tmp_config, prefix: list[str], flag: str,
) -> None:
    client = MagicMock()
    client.go_reply_every.return_value = {
        "ok": True, "name": "watch", "reply_every_seconds": 3600,
        "message": "Sent the looping prompt to watch. Agent resumed.",
    }
    with patch("ilan.cli._client", return_value=client):
        result = CliRunner().invoke(main, [*prefix, "alias", flag])
    assert result.exit_code == 0, result.output
    client.go_reply_every.assert_called_once_with("alias")
    client.get_task.assert_not_called()
    client.reply.assert_not_called()
    assert "Sent the looping prompt" in result.output
    assert "every 1h" in result.output


@pytest.mark.parametrize("prefix", [["task", "reply"], ["reply"], ["re"]])
@pytest.mark.parametrize("extra", [
    ["message"], ["-t", "1h"], ["--every", "30m"], ["-u"], ["-e"],
    ["--max"], ["--unmax"], ["--level", "max"], ["-n", "0"],
    ["-m"], ["--no-md"], ["--line-number"], ["--no-line-number"],
])
def test_go_refuses_messages_and_other_flags_before_any_request(
    tmp_config, prefix: list[str], extra: list[str],
) -> None:
    with patch("ilan.cli._client") as client:
        result = CliRunner().invoke(main, [*prefix, "watch", "-g", *extra])
    assert result.exit_code == 1, result.output
    assert "takes no message" in result.output or "cannot be combined" in result.output
    client.assert_not_called()


@pytest.mark.parametrize("error", [
    "Task watch is not looping: there is no reply -t cycle to change.",
    "Task watch not found",
])
def test_go_reports_server_refusals(tmp_config, error: str) -> None:
    client = MagicMock()
    client.go_reply_every.return_value = {"error": error}
    with patch("ilan.cli._client", return_value=client):
        result = CliRunner().invoke(main, ["re", "watch", "--go"])
    assert result.exit_code == 1
    assert " ".join(error.split()) in " ".join(result.output.split())
    assert "Will re-send" not in result.output


def test_go_does_not_accept_an_option_value(tmp_config) -> None:
    with patch("ilan.cli._client") as client:
        result = CliRunner().invoke(main, ["re", "watch", "--go=true"])
    assert result.exit_code == 2
    client.assert_not_called()
