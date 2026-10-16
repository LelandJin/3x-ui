#!/usr/bin/env python3
"""Keep the fork resolver embedded in independently downloadable shell scripts."""
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
start = '# BEGIN FORK RELEASE SOURCE\n'
end = '# END FORK RELEASE SOURCE\n'
block = start + (root / 'deploy/release-source.sh').read_text() + end
for name in ('install.sh', 'update.sh', 'x-ui.sh'):
    path = root / name
    text = path.read_text()
    if start in text:
        before, rest = text.split(start, 1)
        _, after = rest.split(end, 1)
        updated = before + block + after
    else:
        updated = text.replace('#!/bin/bash\n', '#!/bin/bash\n\n' + block, 1)
    if '--check' in sys.argv:
        if updated != text:
            sys.exit(f'{name}: run python3 deploy/sync-release-source.py')
    else:
        path.write_text(updated)
