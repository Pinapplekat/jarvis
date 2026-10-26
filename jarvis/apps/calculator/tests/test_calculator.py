import pytest

from jarvis.core import actions

CLI = actions.Origin("cli")


def calc(reg, expression):
    return actions.run(reg, "calculator__evaluate", {"expression": expression}, CLI)


@pytest.mark.parametrize("expr,expected", [
    ("2**10 + sqrt(16)", "1028"),
    ("2 * (3 + 4)", "14"),
    ("7 // 2 + 7 % 4", "6"),
    ("-3 + +5", "2"),
    ("10 / 4", "2.5"),
    ("0.1 + 0.2", "0.3"),
    ("2**100", str(2**100)),
    ("sqrt(2)", "1.41421356237"),
    ("pi", "3.14159265359"),
    ("round(2.567, 1)", "2.6"),
    ("floor(2.7) + ceil(2.1)", "5"),
    ("log(8, 2)", "3"),
    ("abs(-4)", "4"),
])
def test_results(real_apps, expr, expected):
    out = calc(real_apps, expr)
    assert isinstance(out, actions.Ran) and out.result.text == expected


@pytest.mark.parametrize("expr,fragment", [
    ("1/0", "divides by zero"),
    ("__import__('os')", "functions"),
    ("open('x')", "functions"),
    ("x + 1", "don't know 'x'"),
    ("(1).real", "arithmetic"),
    ("'a' * 3", "numbers"),
    ("True", "numbers"),
    ("2**1001", "too large"),
    ("10**1000", "digits"),
    ("(9**900)**900", "digits"),
    ("10**999 * 10**999", "digits"),
    ("sqrt(-1)", "isn't defined"),
    ("sqrt(1, 2)", "argument"),
    ("1 +", "valid expression"),
    ("5 ^ 2", "operator"),
])
def test_refusals(real_apps, expr, fragment):
    out = calc(real_apps, expr)
    assert isinstance(out, actions.Failed) and fragment in out.message


def test_precision_setting(settings, tmp_path):
    from jarvis.core.apps import load_apps

    settings.data_dir.mkdir(parents=True)
    (settings.data_dir / "settings.toml").write_text("[apps.calculator]\nprecision = 4\n")
    from jarvis.core.settings import load_settings

    reg = load_apps(load_settings())
    try:
        assert calc(reg, "sqrt(2)").result.text == "1.414"
    finally:
        reg.close()


def test_precision_bounds_come_from_the_manifest(settings):
    import pytest

    from jarvis.core.apps import load_apps
    from jarvis.core.settings import SettingsError, load_settings

    settings.data_dir.mkdir(parents=True)
    (settings.data_dir / "settings.toml").write_text("[apps.calculator]\nprecision = 21\n")
    with pytest.raises(SettingsError, match=r"calculator\] precision: must be ≤ 20"):
        load_apps(load_settings())
