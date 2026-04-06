from pathlib import Path
import pandas as pd

# ===== CONFIG =====
input_folder = r"output/"      # change this
output_file = r"output/combined.csv"  # change this
add_source_file_column = True
# ==================

folder = Path(input_folder)

all_dataframes = []

for file_path in folder.iterdir():
    if file_path.is_file() and file_path.suffix.lower() == ".csv":
        try:
            df = pd.read_csv(file_path)

            if add_source_file_column:
                df["source_file"] = file_path.name

            all_dataframes.append(df)
            print(f"Loaded: {file_path.name}")
        except Exception as e:
            print(f"Skipped {file_path.name} due to error: {e}")

if all_dataframes:
    combined_df = pd.concat(all_dataframes, ignore_index=True, sort=False)
    combined_df.to_csv(output_file, index=False)
    print(f"\nDone. Combined {len(all_dataframes)} CSV files into:")
    print(output_file)
else:
    print("No CSV files found in the folder.")