import sys


def main(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        header = f.readline().strip().split(",")
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split(",")
            row = {}
            for i, h in enumerate(header):
                row[h] = parts[i]
            row["amount"] = float(row["amount"])
            rows.append(row)
    totals = {}
    counts = {}
    for r in rows:
        if r["region"] not in totals:
            totals[r["region"]] = 0.0
            counts[r["region"]] = 0
        totals[r["region"]] += r["amount"]
        counts[r["region"]] += 1
    out = []
    out.append("REGION     COUNT     TOTAL")
    grand = 0.0
    for region in sorted(totals, key=lambda k: -totals[k]):
        out.append(f"{region:<10} {counts[region]:>5} {totals[region]:>9.2f}")
        grand += totals[region]
    out.append("-" * 26)
    out.append(f"{'all':<10} {len(rows):>5} {grand:>9.2f}")
    return "\n".join(out)


if __name__ == "__main__":
    print(main(sys.argv[1]))
