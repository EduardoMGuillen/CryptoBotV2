import pytest

from solbot import config


def test_without_a_file_the_bot_runs_on_paper_with_the_backtest_pick(tmp_path):
    cfg = config.load(tmp_path / "no-existe.toml")
    assert cfg.mode == "paper"
    assert cfg.strategy.id == "trend_sma_50"
    assert cfg.live.min_sol_reserve == pytest.approx(0.03)


def test_file_values_replace_the_defaults(tmp_path):
    path = tmp_path / "solbot.toml"
    path.write_text(
        '[strategy]\nfamily = "vol_target"\nparams = { target = 0.4 }\n'
        '[bot]\nmode = "live"\n'
        '[live]\nbudget_usd = 40\nmax_trades_per_day = 1\n'
    )
    cfg = config.load(path)
    assert cfg.mode == "live"
    assert cfg.strategy.id == "vol_target_0.4_30"
    assert cfg.live.budget_usd == 40
    assert cfg.live.max_trades_per_day == 1
    assert cfg.live.max_trade_usd == 25  # lo que no se escribe queda por defecto


@pytest.mark.parametrize("text, problem", [
    ('[bot]\nmode = "turbo"\n', "mode"),
    ('[strategy]\nfamily = "martingala"\n', "desconocida"),
    ('[live]\nbudget_usd = -5\n', "budget_usd"),
    ('[live]\nmax_slippage_bps = 0\n', "max_slippage_bps"),
    ('[live]\nmin_sol_reserve = -1\n', "min_sol_reserve"),
])
def test_invalid_settings_are_rejected_with_the_field_name(tmp_path, text, problem):
    path = tmp_path / "solbot.toml"
    path.write_text(text)
    with pytest.raises(config.ConfigError, match=problem):
        config.load(path)
