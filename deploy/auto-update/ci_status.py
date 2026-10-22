"""GitHub の check-runs API の応答 (JSON) を標準入力から読み、テストの結果を1語で出す.

  success  すべてのテストが通った (この版を反映してよい)
  pending  まだ走っている / まだ始まっていない (次の回にもう一度見る)
  failure  どれかが失敗した (この版は反映しない)

自動更新係 (zatsumu-auto-update.sh) が使う。依存なし (python3 の標準ライブラリだけ)。
"""
import json
import sys

# skipped / neutral は「失敗ではない」扱い (条件付きで飛ばされたジョブなど)
_OK = {"success", "neutral", "skipped"}


def evaluate(data: dict) -> str:
    runs = data.get("check_runs") or []
    if not runs:
        return "pending"
    if any(r.get("status") == "completed" and r.get("conclusion") not in _OK for r in runs):
        return "failure"
    if any(r.get("status") != "completed" for r in runs):
        return "pending"
    return "success"


if __name__ == "__main__":
    print(evaluate(json.load(sys.stdin)))
