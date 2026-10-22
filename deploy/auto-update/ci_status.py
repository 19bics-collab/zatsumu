"""GitHub の check-runs API の応答 (JSON) を標準入力から読み、テストの結果を1語で出す.

  success  pytest がすべて通った (この版を反映してよい)
  pending  まだ走っている / まだ始まっていない (次の回にもう一度見る)
  missing  pytest の結果が1件もない (しばらく続くなら、テストが走っていない)
  failure  どれかが失敗した・飛ばされた (この版は反映しない)

数えるのは、GitHub Actions (app の slug が github-actions) が出した、名前が "pytest" で
始まるもの (.github/workflows/tests.yml の pytest ジョブ) だけ。ほかの仕組みが付けた
結果や、ほかのジョブの結果は見ない (誰でも付けられる結果で反映が決まらないように)。

自動更新係 (zatsumu-auto-update.sh) が使う。依存なし (python3 の標準ライブラリだけ)。
"""
import json
import sys


def _is_pytest(run: dict) -> bool:
    app = run.get("app") or {}
    return app.get("slug") == "github-actions" and str(run.get("name", "")).startswith("pytest")


def evaluate(data: dict) -> str:
    runs = [r for r in data.get("check_runs") or [] if _is_pytest(r)]
    if not runs:
        return "missing"
    if any(r.get("status") == "completed" and r.get("conclusion") != "success" for r in runs):
        return "failure"
    if any(r.get("status") != "completed" for r in runs):
        return "pending"
    return "success"


if __name__ == "__main__":
    print(evaluate(json.load(sys.stdin)))
