# epsm_workflows

[![CI](https://img.shields.io/badge/CI-GitHub_Actions-2088FF?logo=githubactions&logoColor=white)](https://github.com/SB-Chalmers/epsm_workflows/actions/workflows/ci.yml) [![Releases](https://img.shields.io/badge/releases-GitHub-181717?logo=github&logoColor=white)](https://github.com/SB-Chalmers/epsm_workflows/releases) [![Issues](https://img.shields.io/badge/issues-GitHub-181717?logo=github&logoColor=white)](https://github.com/SB-Chalmers/epsm_workflows/issues)

Design repeatable EPSM experiments, sample parameters, explore renovation
alternatives and execute prepared cases locally or through SSH and Slurm on Vera.
Energy and climate calculations stay with their owning EPSM packages.

## Getting started

Requires Python 3.11+ and Git. Access to the EPSM dependency repositories is
required for the source installation below.

```sh
git clone https://github.com/SB-Chalmers/epsm_workflows.git
cd epsm_workflows
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install "epsm_data @ git+https://github.com/SB-Chalmers/epsm_data.git@main"
python -m pip install "epsm_core @ git+https://github.com/SB-Chalmers/epsm_core.git@main"
python -m pip install -e .
```

```python
from epsm_workflows import Parameter, latin_hypercube

samples = latin_hypercube([Parameter("window_fraction", 0.15, 0.35)], 20, seed=42)
print(samples[0])
```

The sample design runs without a simulation engine. To execute building studies,
install the compatible `epsm` package and selected engine, then supply a Building,
weather and explicit assessment settings as shown in the workflow guide.

## Documentation

- [Detailed guide](docs/guide.md)
- [Study authoring](docs/study-authoring.md)
- [Reusable workflows and execution](docs/reusable-workflows.md)
- [Worked examples](examples/README.md)
- [Release notes](RELEASE_NOTES.md)

## License

[MIT](LICENSE).
