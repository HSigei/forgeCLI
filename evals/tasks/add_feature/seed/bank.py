import json
import pathlib
import sys

DB = pathlib.Path("ledger.json")


def load():
    return json.loads(DB.read_text()) if DB.exists() else []


def save(entries):
    DB.write_text(json.dumps(entries))


def balance(entries):
    return sum(e["amount"] if e["type"] == "deposit" else -e["amount"] for e in entries)


def main(argv):
    entries = load()
    if not argv:
        print("usage: bank.py deposit|withdraw N | balance | history", file=sys.stderr)
        return 1
    cmd = argv[0]
    if cmd in ("deposit", "withdraw"):
        entries.append({"type": cmd, "amount": float(argv[1]) if "." in argv[1] else int(argv[1])})
        save(entries)
    elif cmd == "balance":
        print(f"Balance: {balance(entries):.2f}")
    elif cmd == "history":
        for e in entries:
            print(f"{e['type']:<9}{e['amount']:>8}")
    else:
        print("unknown command", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
