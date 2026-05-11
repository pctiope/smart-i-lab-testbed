from __future__ import annotations

from air1_all_zones import dataset as _dataset
from air1_all_zones import feature_contract as _feature_contract
from air1_all_zones import inference as _inference
from air1_all_zones import training as _training

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


