"""Engine-independent design/analysis example; no files or services required."""
from epsm_workflows import Parameter, sensitivity_design, analyse_sensitivity


def main():
    design = sensitivity_design([Parameter("x", 0, 1), Parameter("y", 0, 1)], 4096, seed=42)
    # Independent x and y contribute variance in ratio 1:4.
    values = [row["x"] + 2 * row["y"] for row in design.samples]
    for parameter, indices in analyse_sensitivity(design, values).items():
        print(parameter, round(indices["first_order"], 3), round(indices["total_order"], 3))


if __name__ == "__main__":
    main()
