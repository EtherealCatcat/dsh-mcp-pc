import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

import dshpc_exec

result = dshpc_exec.run_command(
    'Write-Output "中文输出：你好，世界"; [Console]::Out.WriteLine("mixed ascii")',
    shell="pwsh",
)
Path(".tmp/encoding.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print("stdout repr:", repr(result["stdout"]))
