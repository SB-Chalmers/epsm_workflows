# Study distributions

`epsm_workflows.distributions` defines portable independent marginals. Constructing
a distribution does not draw samples, load a catalogue or contact a service.
Normal uses `mean` and `std`; Weibull uses positive `shape` and `scale`; lognormal
uses `mean_log` and `std_log`. Units follow the bound input. Triangular uses a
mode inside the declared bounds. Uniform, discrete and categorical distributions
use the input domain; category probabilities may be supplied explicitly.

```python
from epsm_workflows.distributions import normal, sample_quantile

value = sample_quantile(normal(mean=0, std=1), 0.5, lower=-1, upper=1)
assert abs(value) < 1e-12
```

`bounds_policy="truncate"` conditions the marginal inside the declared bounds.
`bounds_policy="validate"` draws from the full marginal and rejects an
out-of-domain value. Neither mode clips values or silently draws replacements.
Invalid scales, probabilities, bounds and unsupported definitions fail early.
Correlated sampling is outside this independent-marginal interface.

Existing numeric `Parameter`, `latin_hypercube` and sensitivity APIs retain their
current behavior. These definitions support the additive study authoring API.
