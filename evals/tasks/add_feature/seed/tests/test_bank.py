import bank


def test_balance():
    assert bank.balance([{"type": "deposit", "amount": 10}, {"type": "withdraw", "amount": 4}]) == 6


def test_plain_balance(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    bank.main(["deposit", "5"]); bank.main(["balance"])
    assert capsys.readouterr().out.strip() == "Balance: 5.00"
