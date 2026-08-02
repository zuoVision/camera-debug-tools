#!/usr/bin/env python3
"""Split legacy single-file platform configs into module directories."""

import argparse
import json
import re
from pathlib import Path


MODULES = ("project", "target", "variables", "monitoring", "topology", "tests")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="+")
    parser.add_argument("--output", default="configs/profiles")
    args = parser.parse_args()
    output = Path(args.output)
    for filename in args.files:
        source = Path(filename)
        data = json.loads(source.read_text(encoding="utf-8"))
        profile_name = re.sub(r"-example$", "", source.stem)
        profile = output / profile_name
        profile.mkdir(parents=True, exist_ok=True)
        for module in MODULES:
            default = [] if module == "tests" else {}
            (profile / f"{module}.json").write_text(
                json.dumps(data.get(module, default), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        print(f"{source} -> {profile}")


if __name__ == "__main__":
    main()
