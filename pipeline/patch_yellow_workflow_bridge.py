from __future__ import annotations

from pathlib import Path
import re


FANOUT = Path('.github/workflows/dce-fanout-v2.yml')
REPAIR = Path('.github/workflows/yellow-repair-dce.yml')


def patch_fanout(text: str) -> str:
    if 'workflows: ["Repair Open YELLOW DCEs"]' not in text:
        anchor = "  push:\n    branches: [main]\n    paths:\n      - 'benchmarks/dce-throughput-trigger.json'\n"
        if anchor not in text:
            raise RuntimeError('DCE fanout push anchor missing')
        text = text.replace(
            anchor,
            anchor + '  workflow_run:\n    workflows: ["Repair Open YELLOW DCEs"]\n    types: [completed]\n',
            1,
        )

    if '          EVENT_NAME: ${{ github.event_name }}' not in text:
        m = re.search(r'(?m)^(\s*SELECTION_PATH:.*)$', text)
        if not m:
            raise RuntimeError('DCE fanout SELECTION_PATH env anchor missing')
        text = text[:m.end()] + '\n          EVENT_NAME: ${{ github.event_name }}' + text[m.end():]

    if 'Workflow-run bridge: selection=' not in text:
        old = '          set -euo pipefail\n          source_tag="discovery-harvest-${SOURCE_RUN}"\n'
        if old not in text:
            raise RuntimeError('DCE fanout materialize shell anchor missing')
        new = '''          set -euo pipefail
          if [ "${EVENT_NAME:-}" = "workflow_run" ]; then
            SELECTION_PATH='control/yellow_repair_request.json'
            SOURCE_RUN="$(python - <<'PY2'
          import json
          x=json.load(open('control/yellow_repair_request.json',encoding='utf-8'))
          print(x.get('source_discovery_run') or '')
          PY2
          )"
            [[ "$SOURCE_RUN" =~ ^[0-9]+$ ]] || { echo 'Invalid YELLOW repair source run' >&2; exit 1; }
            echo "Workflow-run bridge: selection=$SELECTION_PATH source_run=$SOURCE_RUN"
          fi
          source_tag="discovery-harvest-${SOURCE_RUN}"
'''
        text = text.replace(old, new, 1)

    return text


def patch_repair(text: str) -> str:
    marker = '      - name: Dispatch fresh DCE repair fanout\n'
    if marker in text:
        pos = text.index(marker)
        text = text[:pos] + '''      - name: Finish via workflow-run bridge
        run: |
          echo 'Repair request persisted. DCE Fanout V2 starts from successful workflow completion without workflow-dispatch API usage.'
'''
    return text


def main() -> None:
    fanout = patch_fanout(FANOUT.read_text(encoding='utf-8'))
    repair = patch_repair(REPAIR.read_text(encoding='utf-8'))

    assert 'workflows: ["Repair Open YELLOW DCEs"]' in fanout
    assert 'EVENT_NAME: ${{ github.event_name }}' in fanout
    assert 'Workflow-run bridge: selection=' in fanout
    assert 'Finish via workflow-run bridge' in repair
    assert 'gh workflow run dce-fanout-v2.yml' not in repair

    FANOUT.write_text(fanout, encoding='utf-8')
    REPAIR.write_text(repair, encoding='utf-8')
    print('YELLOW workflow-run bridge patched successfully')


if __name__ == '__main__':
    main()
