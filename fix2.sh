#!/data/data/com.termux/files/usr/bin/bash
# 修复 Serein「记忆未就绪时拒绝保存设置」的死锁，不重新构建
set -u
cd "$HOME/Serein" 2>/dev/null || { echo "找不到 ~/Serein"; exit 1; }

targets=""
V1="deploy/venv/lib/python3.14/site-packages/serein/api/settings.py"
[ -f "$V1" ] && targets="$targets $V1"
[ -f "src/serein/api/settings.py" ] && targets="$targets src/serein/api/settings.py"
[ -z "$targets" ] && { echo "!! 没找到 settings.py"; exit 1; }

for T in $targets; do
  echo "==> $T"
  cp "$T" "$T.bak.$(date +%s)"
  python3 - "$T" <<'PY'
import sys, ast
p = sys.argv[1]
s = open(p, encoding='utf-8').read()
if "patch['upstream']" in s:
    print("    已经是修好的版本，跳过")
    raise SystemExit
old = """        if body.upstream and body.upstream.memory_enabled and not memory_ready(settings):
            raise HTTPException(409, 'Configure embedding, reranker and semantic routes before enabling memory')
        save_settings(settings.database, body.model_dump(exclude_none=True))"""
new = """        patch = body.model_dump(exclude_none=True)
        if body.upstream and body.upstream.memory_enabled and not memory_ready(settings):
            patch['upstream'] = {**(patch.get('upstream') or {}), 'memory_enabled': False}
        save_settings(settings.database, patch)"""
if old not in s:
    print("    !! 没匹配到旧代码（版本不同），未修改")
    raise SystemExit(1)
s = s.replace(old, new, 1)
ast.parse(s)
open(p, 'w', encoding='utf-8').write(s)
print("    已替换，语法检查通过")
PY
done

echo
echo "下一步：在菜单里选 2 重启服务"
echo "  se  →  2 重启服务  →  等出现 18217 就绪"
