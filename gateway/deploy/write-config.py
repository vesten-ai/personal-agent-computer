#!/usr/bin/env python3
"""Initialize gateway configuration without replacing an operator's existing config."""
import argparse
import json
from pathlib import Path
import tomllib


def initialize(template: Path, destination: Path, image: str):
    if destination.exists():
        return False
    text = template.read_text()
    default = 'image = "pac-desktop/dev"'
    if text.count(default) != 1:
        raise ValueError('expected one default Incus image in gateway template')
    text = text.replace(default, 'image = ' + json.dumps(image, ensure_ascii=True))
    if tomllib.loads(text)['incus']['image'] != image:
        raise ValueError('gateway image configuration did not round-trip')
    try:
        with destination.open('x') as stream:
            stream.write(text)
    except FileExistsError:
        return False
    destination.chmod(0o644)
    return True


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('template', type=Path)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--image', required=True)
    args = parser.parse_args()
    initialize(args.template, args.destination, args.image)
