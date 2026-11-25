"""Browser regression helper: run real apply endpoint with in-memory registry only."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import loci.app as wb

data = json.load(sys.stdin)
data['save'] = False
with wb.app.test_client() as client:
    response = client.post('/api/desensitize/apply', json=data)
    print(json.dumps(response.get_json(), ensure_ascii=False))
