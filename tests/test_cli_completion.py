from click.shell_completion import get_completion_class

from ilan.cli import _harden_fish_completion, main


def test_harden_splits_on_first_comma_only() -> None:
    src = 'set -l metadata (string split "," $completion);'
    out = _harden_fish_completion(src)
    assert 'string split --max 1 -- "," $completion' in out
    assert 'string split "," $completion' not in out


def test_harden_preserves_rest_of_script() -> None:
    src = 'before\nset -l metadata (string split "," $completion);\nafter'
    out = _harden_fish_completion(src)
    assert out.startswith("before\n")
    assert out.endswith("\nafter")


def test_installed_fish_script_keeps_comma_in_description() -> None:
    """An end-to-end check that the patched split keeps comma-laden help intact.

    `clear-everything`'s short help ("Remove ALL tasks, logs, and data.") is the
    canonical victim of the unpatched `string split ","`.
    """
    cls = get_completion_class("fish")
    assert cls is not None
    comp = cls(cli=main, ctx_args={}, prog_name="ilan", complete_var="_ILAN_COMPLETE")
    script = _harden_fish_completion(comp.source())
    assert 'string split --max 1 -- "," $completion' in script
    assert 'string split "," $completion' not in script
