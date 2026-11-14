import pathlib, subprocess, sys

HERE = pathlib.Path(__file__).resolve().parent.parent
EXPECTED = """REGION     COUNT     TOTAL
east           1    300.00
north          2    160.50
south          2     99.99
--------------------------
all            5    560.49"""


def test_output_unchanged():
    out = subprocess.run([sys.executable, "report.py", "sales.csv"], cwd=HERE, capture_output=True, text=True)
    assert out.stdout.strip() == EXPECTED
