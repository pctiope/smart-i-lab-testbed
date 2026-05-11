from __future__ import annotations

from zone5 import dataset as _dataset
from zone5 import feature_contract as _feature_contract
from zone5 import inference as _inference
from zone5 import training as _training

# Convenience surface for CLI entry points. New code should import from
# feature_contract, dataset, training, or inference directly.
for _module in (_feature_contract, _dataset, _training, _inference):
    for _name in dir(_module):
        if _name.startswith("__"):
            continue
        globals()[_name] = getattr(_module, _name)


def main() -> None:
    _training.main()


if __name__ == "__main__":
    main()
