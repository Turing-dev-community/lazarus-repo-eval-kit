import csv
import sys
import os

def transpose_csv(input_path, output_path=None):
    with open(input_path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    if not rows:
        print("No data found.")
        return

    if output_path is None:
        base, ext = os.path.splitext(input_path)
        output_path = base + "_transposed" + ext

    with open(output_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(["column", "value"])
        for i, row in enumerate(rows):
            if len(rows) > 1:
                writer.writerow([f"--- row {i + 1} ---", ""])
            for key, value in row.items():
                writer.writerow([key, value])

    print(f"Transposed CSV saved to: {output_path}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python transpose_csv.py <input.csv> [output.csv]")
        sys.exit(1)

    input_path = sys.argv[1]
    output_path = sys.argv[2] if len(sys.argv) >= 3 else None
    transpose_csv(input_path, output_path)
