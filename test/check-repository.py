#!/usr/bin/env python3
"""Check local documentation links and reject private-key blocks."""
from pathlib import Path
import re
import sys
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]


def main():
    errors = []
    for path in ROOT.rglob('*'):
        if not path.is_file() or any(x in path.parts for x in ('.git', '__pycache__', '.venv')):
            continue
        if path.suffix not in ('.md', '.py', '.sh', '.json', '.yml', '.yaml', '.txt'):
            continue
        text = path.read_text()
        if re.search(r'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----', text):
            errors.append(f'{path.relative_to(ROOT)}: private key block')
        if path.suffix != '.md':
            continue
        for target in re.findall(r'\]\(([^)]+)\)', text):
            target = target.split('#')[0].strip('<>')
            if not target or re.match(r'[a-z]+:', target):
                continue
            dest = (path.parent / unquote(target)).resolve()
            if not dest.is_relative_to(ROOT) or not dest.exists():
                errors.append(f'{path.relative_to(ROOT)}: missing/outside link {target}')
    if errors:
        print('\n'.join(errors), file=sys.stderr)
        return 1
    print('Local documentation links and private-key checks passed.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
