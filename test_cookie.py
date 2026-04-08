import sqlite3, shutil, tempfile
from pathlib import Path

base = Path.home() / "Library/Application Support/Google/Chrome"

for cf in base.glob("*/Cookies"):
    try:
        tmp = tempfile.mktemp(suffix=".db")
        shutil.copy2(cf, tmp)
        conn = sqlite3.connect(tmp)
        # 先看列名
        cols = [r[1] for r in conn.execute("PRAGMA table_info(cookies)").fetchall()]
        rows = conn.execute(
            "SELECT name, host_key FROM cookies "
            "WHERE host_key LIKE '%dmm%' OR host_key LIKE '%touken%'"
        ).fetchall()
        conn.close()
        Path(tmp).unlink()
        if rows:
            print(f"\n=== {cf.parent.name} ===")
            for name, host in rows:
                print(f"  {name} @ {host}")
        else:
            print(f"{cf.parent.name}: 无 dmm/touken cookie（列：{cols[:5]}...）")
    except Exception as e:
        print(f"{cf.parent.name}: {e}")
